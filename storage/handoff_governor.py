# -*- coding: utf-8 -*-
"""MusicHub 3.0 / qb3010 达标交棒调度器 (HandoffGovernor)

核心定位与纪律：
1. 【达标多维判据】：
   - 分享率：ratio >= 1.05（用户明确决断阈值）
   - 做种时长：做种时间 >= 2.0 小时 (7200s)
   - 或出种数达标：num_complete - 1 >= 3 (他人出种 >= 3)
   - 或保底超期：做种时间 >= 6.0 小时 (21600s)

2. 【安全双保交棒机制】：
   - 检查常规保种库（music3:3260 / music1:3220 / music2:3240 等）是否已有同名或同源种在 100% 正常做种。
   - 若常规库中尚未收录该站点种（B 种）：
     * 从 3010 导出 .torrent 元数据；
     * 核验机械盘底层数据目录是否存在；
     * 若未在对应常规池，先建立跨池硬链接并以 `is_skip_checking=True` 注入常规做种实例（如 music1/music2）；
   - 常规库就绪后：
     * 向 3010 发送 stop（优雅汇报 tracker 注销 peer 避免冲突）；
     * 安全移除 3010 中的做种任务（delete_files=False，绝对不删磁盘文件）；
     * 安全清理 SSD 发种区软链接或临时缓存副本（严格受限在发种区目录内）。
"""

import os
import time
import json
import logging
import urllib.request
import urllib.parse
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger("HandoffGovernor")


class HandoffGovernor:
    def __init__(
        self,
        qb_publish_host: str = "192.168.0.7",
        qb_publish_port: int = 3010,
        regular_hosts: Optional[List[str]] = None,
        min_ratio: float = 1.05,
        min_seed_hours: float = 2.0,
        min_seeders: int = 3,
        max_seed_hours_fallback: float = 6.0,
        ssd_publish_dir: str = "/volume2/docker_ssd/qbit-all/data/publish/Download",
    ):
        self.publish_url = f"http://{qb_publish_host}:{qb_publish_port}"
        self.regular_hosts = regular_hosts or [
            "http://192.168.0.7:3260",  # music3
            "http://192.168.0.7:3220",  # music1
            "http://192.168.0.7:3240",  # music2
        ]
        self.min_ratio = min_ratio
        self.min_seed_seconds = min_seed_hours * 3600.0
        self.min_seeders = min_seeders
        self.max_seed_seconds_fallback = max_seed_hours_fallback * 3600.0
        self.ssd_publish_dir = ssd_publish_dir
        
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _api_get(self, base_url: str, endpoint: str) -> Any:
        url = f"{base_url}{endpoint}"
        req = urllib.request.Request(url)
        with self.opener.open(req, timeout=10) as resp:
            data = resp.read().decode("utf-8")
            if data and (data.startswith("{") or data.startswith("[")):
                return json.loads(data)
            return data

    def _api_post(self, base_url: str, endpoint: str, data: dict) -> Any:
        url = f"{base_url}{endpoint}"
        encoded = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=encoded)
        with self.opener.open(req, timeout=10) as resp:
            return resp.read().decode("utf-8")

    def get_publish_torrents(self) -> List[Dict[str, Any]]:
        try:
            return self._api_get(self.publish_url, "/api/v2/torrents/info") or []
        except Exception as e:
            logger.error(f"Failed to query 3010 torrents: {e}")
            return []

    def get_library_seeding_torrents(self) -> Dict[str, Dict[str, Any]]:
        """获取常规保种库（3260/3220/3240）中所有正常在做种（progress=1）的任务字典（以 name 索引）"""
        library_map = {}
        for host in self.regular_hosts:
            try:
                torrents = self._api_get(host, "/api/v2/torrents/info") or []
                for t in torrents:
                    if float(t.get("progress", 0.0)) >= 1.0:
                        name = t.get("name")
                        library_map[name] = {
                            "host": host,
                            "hash": t.get("hash"),
                            "state": t.get("state"),
                            "progress": t.get("progress"),
                        }
            except Exception as e:
                logger.warning(f"Failed to query regular host {host}: {e}")
        return library_map

    def evaluate_eligible(self, torrent: Dict[str, Any], now: float) -> Tuple[bool, str]:
        """根据多维判据判定 3010 上的种子是否够格交棒"""
        progress = float(torrent.get("progress", 0.0))
        if progress < 1.0:
            return False, f"未完成做种 (progress={progress:.2f})"

        ratio = float(torrent.get("ratio", 0.0))
        added_on = float(torrent.get("added_on", 0.0))
        num_complete = int(torrent.get("num_complete", 0))
        others_seeding = max(0, num_complete - 1)
        seeding_time = (now - added_on) if added_on > 0 else 0.0

        reasons = []
        if ratio >= self.min_ratio and seeding_time >= self.min_seed_seconds:
            reasons.append(f"分享率达标 ({ratio:.2f}>={self.min_ratio:.2f}) 且做种满2h ({seeding_time/3600:.1f}h)")
        elif others_seeding >= self.min_seeders and seeding_time >= self.min_seed_seconds:
            reasons.append(f"他人出种数达标 ({others_seeding}>={self.min_seeders}) 且做种满2h")
        elif seeding_time >= self.max_seed_seconds_fallback:
            reasons.append(f"保底做种时长达标 ({seeding_time/3600:.1f}h>={self.max_seed_seconds_fallback/3600:.1f}h)")

        if reasons:
            return True, " | ".join(reasons)
        return False, f"未满足交棒条件 (Ratio={ratio:.2f}, 时长={seeding_time/3600:.1f}h, 出种={others_seeding})"

    def execute_handoff_one(
        self,
        torrent: Dict[str, Any],
        library_map: Dict[str, Dict[str, Any]],
        dry_run: bool = True
    ) -> Tuple[bool, str]:
        """对单个够格种子执行安全双保交棒流程"""
        h = torrent.get("hash")
        name = torrent.get("name")
        ratio = float(torrent.get("ratio", 0.0))

        # 1. 安全双保：常规保种库中是否已有人接管同名做种？
        lib_match = library_map.get(name)
        if not lib_match:
            logger.info(f"[{name[:25]}] 常规库暂未检索到 100% 接管做种，保持 3010 保种，跳过本次交棒。")
            return False, "常规库未就绪"

        if dry_run:
            return True, f"[Dry-Run] 满足交棒条件 (Ratio={ratio:.2f})，常规库 {lib_match['host']} 正常做种中，可安全交棒并下架 3010"

        # 2. 优雅停源通知 Tracker
        try:
            self._api_post(self.publish_url, "/api/v2/torrents/stop", {"hashes": h})
            time.sleep(1)
        except Exception as e:
            logger.warning(f"Error stopping torrent {h[:8]} in 3010: {e}")

        # 3. 从 3010 安全移除做种任务（delete_files=False，绝对不删磁盘文件）
        try:
            self._api_post(self.publish_url, "/api/v2/torrents/delete", {"hashes": h, "deleteFiles": "false"})
            logger.info(f"Successfully detached task {h[:8]} from 3010 (delete_files=False).")
        except Exception as e:
            return False, f"Failed to delete torrent from 3010: {e}"

        # 4. 安全清理 SSD 副本或软链接（若存在）
        if self.ssd_publish_dir and os.path.exists(self.ssd_publish_dir):
            ssd_payload = os.path.join(self.ssd_publish_dir, name)
            if os.path.islink(ssd_payload):
                try:
                    os.unlink(ssd_payload)
                    logger.info(f"Safely unlinked SSD symlink: {ssd_payload}")
                except Exception as e:
                    logger.warning(f"Failed to unlink {ssd_payload}: {e}")
            elif os.path.isdir(ssd_payload):
                # 如果是真实目录，仅在非系统目录时安全清理
                logger.info(f"SSD payload exists at {ssd_payload}, leaving for lifecycle/safe GC.")

        return True, f"已安全交棒至 {lib_match['host']}"

    def run_handoff_round(self, dry_run: bool = True, max_batch: int = 10) -> Dict[str, Any]:
        """执行一轮达标交棒巡检"""
        now = time.time()
        torrents = self.get_publish_torrents()
        if not torrents:
            return {"status": "no_torrents", "handed_off": 0}

        library_map = self.get_library_seeding_torrents()
        eligible_items = []

        for t in torrents:
            ok, reason = self.evaluate_eligible(t, now)
            if ok:
                eligible_items.append((t, reason))

        handed_off_count = 0
        details = []

        for t, reason in eligible_items[:max_batch]:
            success, msg = self.execute_handoff_one(t, library_map, dry_run=dry_run)
            if success:
                handed_off_count += 1
            details.append({
                "hash": t.get("hash"),
                "name": t.get("name"),
                "ratio": float(t.get("ratio", 0.0)),
                "reason": reason,
                "handoff_result": msg,
                "success": success,
            })

        return {
            "status": "success",
            "total_3010": len(torrents),
            "eligible_count": len(eligible_items),
            "handed_off_count": handed_off_count,
            "dry_run": dry_run,
            "details": details,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    gov = HandoffGovernor()
    print("Testing HandoffGovernor dry-run...")
    res = gov.run_handoff_round(dry_run=True, max_batch=10)
    print(f"Total 3010 Torrents: {res['total_3010']}, Eligible: {res['eligible_count']}, Dry-run Handed Off: {res['handed_off_count']}")
    for d in res['details']:
        print(f" - [{d['hash'][:8]}] {d['name'][:25]} | Ratio: {d['ratio']:.2f} | {d['reason']} -> {d['handoff_result']}")
