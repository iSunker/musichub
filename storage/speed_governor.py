# -*- coding: utf-8 -*-
"""MusicHub 3.0 / qb3010 阶梯调速器 (SpeedGovernor)

核心目标与机制：
1. 【五级阶梯生命周期】：
   - 0.00 <= ratio < 0.05: 预热阶段 (基础权重 0.20，新种防抢额度上限 100 KB/s)
   - 0.05 <= ratio < 0.20: 爬坡加速 (基础权重 0.50)
   - 0.20 <= ratio < 0.80: 全速稳定做种 (基础权重 1.00，优先保障出种)
   - 0.80 <= ratio < 0.95: 梯级降速 (基础权重 0.50)
   - 0.95 <= ratio < 1.05: 极低速收尾 (基础权重 0.20)
   - ratio >= 1.05: 达标完成，限速保底 (50 KB/s)，打上 handoff_ready 标签并联动交棒

2. 【出种优先保护与防抢占机制 (Anti-Preemption Protection)】：
   - 痛点：后发布的种子连接性好，抢占大量带宽，导致前一个已稳定上传但未完成出种的种子失速。
   - 机制：
     * 识别正在稳定上传的做种中任务（upspeed >= 50 KB/s 且 0.05 <= ratio < 1.05）：
       赋予「高优先级保障」，确保其至少获得历史稳定带宽或充足的上传份额，不受新种冲击；
     * 对新加入任务（ratio < 0.05）：在有活跃主力上传种子时，其预热期最高上限严格钳制在 100 KB/s，
       杜绝新种并发暴涌反客为主；
     * 动态分配：总有效带宽扣除保底与活跃保护额后，再按剩余权重分配，整体严格受控于 5 MB/s 红线。

3. 【分层交棒联动】：
   - ratio >= 1.05 时自动标记 `handoff_ready`，由 HandoffGovernor 定时接入交棒。
"""

import math
import time
import json
import logging
import urllib.request
import urllib.parse
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger("SpeedGovernor")


class LadderTier:
    """阶梯层级配置"""
    def __init__(self, min_ratio: float, max_ratio: float, weight: float, desc: str):
        self.min_ratio = min_ratio
        self.max_ratio = max_ratio
        self.weight = weight
        self.desc = desc

    def matches(self, ratio: float) -> bool:
        return self.min_ratio <= ratio < self.max_ratio


class SpeedGovernor:
    """qb3010 阶梯调速与带宽平滑治理器"""

    TIERS = [
        LadderTier(0.00, 0.05, 0.20, "预热起步 (Warm-up)"),
        LadderTier(0.05, 0.20, 0.50, "加速爬坡 (Ramp-up)"),
        LadderTier(0.20, 0.80, 1.00, "全速巡航 (Full-speed)"),
        LadderTier(0.80, 0.95, 0.50, "缓降减速 (Ramp-down)"),
        LadderTier(0.95, 1.05, 0.20, "收尾过渡 (Cooldown)"),
    ]

    HANDOFF_RATIO = 1.05       # 达标分享率阈值（可交棒）
    COMPLETED_LIMIT_BPS = 51200 # 达标后保底保种速度 50 KB/s（等交棒，不抢带宽）
    NEWCOMER_CAP_BPS = 262144  # 新种预热防抢占上限 256 KB/s（旧值 100KB 会让新种永远跑不动）
    FULL_SPEED_CAP_BPS = 4194304  # 全速巡航段单种上限 4 MiB/s：单种上限给足，靠 qB 全局 5MB 兜底
    RAMP_CAP_BPS = 2097152     # 爬坡段单种上限 2 MiB/s
    COOLDOWN_CAP_BPS = 1048576 # 减速/收尾段单种上限 1 MiB/s
    STABLE_UPLOAD_THRESHOLD_BPS = 51200 # 判定稳定上传的速率阈值 (50 KB/s)
    PREF_ACTIVE_UPLOADS = 200  # 3010 上传并发槽：旧值 5 会把 200+ 种子压在 queuedUP 饿死带宽
    PREF_CONNEC_PER_TORRENT = 8
    PREF_UPLOADS_PER_TORRENT = 8

    def __init__(
        self,
        qb_host: str = "192.168.0.7",
        qb_port: int = 3010,
        total_bandwidth_bps: int = 5120000, # 5 MB/s
        check_interval: float = 7.0,         # 巡检周期 7 秒
        manage_handoff_tag: bool = True,
    ):
        self.qb_host = qb_host
        self.qb_port = qb_port
        self.total_bandwidth = total_bandwidth_bps
        self.check_interval = check_interval
        self.manage_handoff_tag = manage_handoff_tag
        self.base_url = f"http://{qb_host}:{qb_port}"
        
        # 禁用本地代理
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, endpoint: str, data: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{endpoint}"
        encoded_data = urllib.parse.urlencode(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=encoded_data)
        with self.opener.open(req, timeout=5) as resp:
            content = resp.read().decode("utf-8")
            if content and (content.startswith("{") or content.startswith("[")):
                return json.loads(content)
            return content

    def get_torrents(self) -> List[Dict[str, Any]]:
        """获取 3010 做种中/发布中的任务清单"""
        try:
            return self._request("/api/v2/torrents/info") or []
        except Exception as e:
            logger.error(f"Failed to fetch torrents from {self.base_url}: {e}")
            return []

    def set_torrent_upload_limit(self, info_hash: str, limit_bps: int):
        """设定单种上传限速 (0 为无限制)"""
        try:
            self._request("/api/v2/torrents/setUploadLimit", {"hashes": info_hash, "limit": limit_bps})
        except Exception as e:
            logger.warning(f"Failed to set upload limit for {info_hash[:8]}: {e}")

    def add_torrent_tag(self, info_hash: str, tag: str):
        """为种子打上指定标签"""
        try:
            self._request("/api/v2/torrents/addTags", {"hashes": info_hash, "tags": tag})
        except Exception as e:
            logger.warning(f"Failed to add tag {tag} to {info_hash[:8]}: {e}")

    def set_upload_limit_bulk(self, hashes: List[str], limit_bps: int):
        """批量设定多个种子的同一上传限速（qB 用 `|` 分隔，一次请求）"""
        if not hashes:
            return
        try:
            self._request("/api/v2/torrents/setUploadLimit",
                          {"hashes": "|".join(hashes), "limit": limit_bps})
        except Exception as e:
            logger.warning(f"Failed to bulk set upload limit ({len(hashes)} torrents): {e}")

    def ensure_queue_prefs(self, dry_run: bool = False) -> Dict[str, Any]:
        """确保 3010 的上传并发槽与连接数不成为带宽瓶颈

        实测：`max_active_uploads=5` 会把 200+ 个种子压在 `queuedUP`，即使整体
        `up_limit=5MB/s`，也只有 5 个种能同时上传，总上传长期 1.4MB/s 吃不满。
        """
        try:
            prefs = self._request("/api/v2/app/preferences") or {}
        except Exception as e:
            logger.warning(f"读取 3010 preferences 失败: {e}")
            return {}

        changes: Dict[str, Any] = {}
        if int(prefs.get("max_active_uploads", 0)) < self.PREF_ACTIVE_UPLOADS:
            changes["max_active_uploads"] = self.PREF_ACTIVE_UPLOADS
        if int(prefs.get("max_connec_per_torrent", 0)) < self.PREF_CONNEC_PER_TORRENT:
            changes["max_connec_per_torrent"] = self.PREF_CONNEC_PER_TORRENT
        if int(prefs.get("max_uploads_per_torrent", 0)) < self.PREF_UPLOADS_PER_TORRENT:
            changes["max_uploads_per_torrent"] = self.PREF_UPLOADS_PER_TORRENT

        if not changes:
            return {}
        if dry_run:
            return changes
        try:
            self._request("/api/v2/app/setPreferences", {"json": json.dumps(changes)})
            logger.info(f"3010 队列参数已调整: {changes}")
        except Exception as e:
            logger.error(f"下发 3010 队列参数失败: {e}")
            return {}
        return changes

    def get_tier_and_weight(self, ratio: float) -> Tuple[float, str]:
        """根据当前分享率获取分配权重与阶梯描述"""
        if ratio >= self.HANDOFF_RATIO:
            return 0.0, f"已达标完成 (Ratio>={self.HANDOFF_RATIO:.2f})"
        for tier in self.TIERS:
            if tier.matches(ratio):
                return tier.weight, tier.desc
        return 0.20, "预热兜底"

    def calculate_distribution(self, torrents: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """核心调速算法：按阶梯给「单种上限」，再由 qB 全局 5MB 做最终切分

        旧实现的根本缺陷是拿「预留额度」直接扣减总预算：
        42 个达标种×50KB + 206 个新种×100KB = 22MB 预约 >> 5MB 预算，
        剩余带宽被钳到最小值，真正有 peer 的种拿不到上限，总上传长期只有 1.4MB/s。

        新策略（三分法，不做总预算切分）：
        - 达标种（ratio>=1.05，等交棒）：50 KB/s 保活，绝不抢带宽；
        - 新种（ratio<0.05）：256 KB/s 上限，防瞬间抢占但不至于跑不动；
        - 未达标活跃种（0.05<=ratio<1.05）：按阶梯给足单种上限（全速 4MB / 爬坡 2MB / 收尾 1MB），
          由 qB 的全局 `up_limit=5MB` 负责公平分配——单种上限给足才不会成为瓶颈。
        """
        decisions: Dict[str, Dict[str, Any]] = {}

        seeding_candidates = []
        for t in torrents:
            state = t.get("state", "").lower()
            # 暂停/停止/错误/校验中不参与调速（校验中限速无意义）
            if state in ("pausedup", "stoppedup", "error", "missingfiles",
                         "checkingup", "checkingresumedata"):
                continue
            seeding_candidates.append(t)

        if not seeding_candidates:
            return decisions

        for t in seeding_candidates:
            h = t.get("hash")
            if not h:
                continue
            ratio = float(t.get("ratio", 0.0))
            current_limit = int(t.get("up_limit", 0))

            if ratio >= self.HANDOFF_RATIO:
                target = self.COMPLETED_LIMIT_BPS
                desc = f"达标保底 (Ratio={ratio:.2f}>={self.HANDOFF_RATIO})"
                handoff_ready = True
            elif ratio < 0.05:
                target = self.NEWCOMER_CAP_BPS
                desc = f"预热起步 (上限 {target//1024}KB/s)"
                handoff_ready = False
            else:
                weight, tier_desc = self.get_tier_and_weight(ratio)
                if ratio < 0.20:
                    target = self.RAMP_CAP_BPS
                elif ratio < 0.80:
                    target = self.FULL_SPEED_CAP_BPS
                else:
                    target = self.COOLDOWN_CAP_BPS
                desc = f"{tier_desc} (上限 {target//1024}KB/s)"
                handoff_ready = False

            decisions[h] = {
                "name": t.get("name", ""),
                "ratio": ratio,
                "target_limit": target,
                "current_limit": current_limit,
                "tier_desc": desc,
                "handoff_ready": handoff_ready,
            }

        return decisions

    def apply_step(self, dry_run: bool = False) -> Dict[str, Any]:
        """执行单轮调速步进"""
        torrents = self.get_torrents()
        if not torrents:
            return {"status": "no_torrents", "adjusted": 0}

        decisions = self.calculate_distribution(torrents)
        adjusted_count = 0
        handoff_tagged_count = 0
        # 按 target_limit 分组批量下发，避免 300+ 个种子逐个发请求拖慢 7s 节拍
        limit_groups: Dict[int, List[str]] = {}
        handoff_hashes: List[str] = []

        for h, plan in decisions.items():
            target_limit = plan["target_limit"]
            curr_limit = plan["current_limit"]

            # 上限变化 >= 50KB/s 才下发（避免抖动期反复写入）
            if curr_limit == 0 or abs(target_limit - curr_limit) >= 51200:
                limit_groups.setdefault(target_limit, []).append(h)

            if plan["handoff_ready"] and self.manage_handoff_tag:
                handoff_hashes.append(h)

        adjusted_count = sum(len(v) for v in limit_groups.values())
        handoff_tagged_count = len(handoff_hashes)

        if not dry_run:
            for target_limit, hashes in limit_groups.items():
                self.set_upload_limit_bulk(hashes, target_limit)
            for h in handoff_hashes:
                self.add_torrent_tag(h, "handoff_ready")

        return {
            "status": "success",
            "total_torrents": len(torrents),
            "governed_torrents": len(decisions),
            "adjusted_count": adjusted_count,
            "handoff_ready_count": handoff_tagged_count,
            "dry_run": dry_run,
            "decisions": decisions,
        }

    def run_forever(self):
        """常驻守护循环"""
        logger.info(f"SpeedGovernor started on {self.base_url}, Total BW: {self.total_bandwidth/1024/1024:.2f} MB/s, Interval: {self.check_interval}s")
        while True:
            try:
                res = self.apply_step(dry_run=False)
                logger.info(f"SpeedGovernor tick: Governed={res['governed_torrents']}, Adjusted={res['adjusted_count']}, HandoffReady={res['handoff_ready_count']}")
            except Exception as e:
                logger.error(f"Error in SpeedGovernor loop: {e}")
            time.sleep(self.check_interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    governor = SpeedGovernor(qb_host="192.168.0.7", qb_port=3010)
    print("Executing dry-run step on 3010...")
    result = governor.apply_step(dry_run=True)
    print(f"Total Torrents: {result['total_torrents']}, Governed: {result['governed_torrents']}")
    for h, p in list(result['decisions'].items())[:10]:
        print(f"[{h[:8]}] {p['name'][:25]} | Ratio: {p['ratio']:.2f} | {p['tier_desc']} -> Target Limit: {p['target_limit']/1024:.1f} KB/s")
