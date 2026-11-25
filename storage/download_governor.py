# -*- coding: utf-8 -*-
"""MusicHub 3.0 / 8 个音乐 QB 实例多池下载阶梯调速器 (MultiPoolDownloadGovernor)

核心目标与机制：
1. 【纳管范围】：
   - 全面纳管 8 个核心音乐 qB 实例：
     * music3 (3260): OpenCD 物理主库
     * music1 (3220)
     * music2 (3240)
     * music_pool_1 (3210)
     * music_pool_2 (3230)
     * music_pool_3 (3250)
     * music_pool_4 (3270)
     * music_pool_5 (3280)

2. 【五档位精细阶梯下载调速模型】（契合用户调速参考方案）：
   - 档位 1 [0% ~ 10%]:  初始预热低速 -> 1.0 MiB/s (1048576 B/s，平稳握手建连，防握手暴涌抢占带宽)
   - 档位 2 [10% ~ 20%]: 爬坡加速低速 -> 2.5 MiB/s (2621440 B/s，前 20% 低速过渡)
   - 档位 3 [20% ~ 70%]: 主力冲刺极速 -> 5.0 MiB/s (5242880 B/s，最高 5 兆单种，主力吃满下行带宽)
   - 档位 4 [70% ~ 90%]: 高位稳态高速 -> 4.0 MiB/s (4194304 B/s，20%~90% 高速区间后半程)
   - 档位 5 [90% ~ 100%]: 尾盘减速防抖 -> 1.5 MiB/s (1572864 B/s，最后 10% 减速收敛，防磁盘寻道与写缓冲区阻塞)

3. 【并发槽位管理与母种保护铁律】：
   - 严格只治理未完成下载任务 (progress < 1.0)；
   - 对 progress >= 1.0 的海量已完成母种做种任务绝对不暂停、不限速、不删除；
   - 每个实例默认并发上限 20 个（小包优先 size_asc），超出槽位的未完成任务默认保持 paused/stopped，防机械盘寻道风暴；
   - 槽位任务 100% 完工后立即转正做种，空出槽位即时自动唤醒排队新任务。
"""

import time
import json
import logging
import urllib.request
import urllib.parse
from typing import Dict, List, Optional, Tuple, Set, Any

from musichub.storage.pool_pairing import POOL_SPECS

logger = logging.getLogger("DownloadGovernor")


class SinglePoolDownloadGovernor:
    """针对单个 qB 实例的下载阶梯调速与并发治理器"""

    TIER_1_LIMIT_BPS = 1048576    # 1.0 MiB/s (0% ~ 10%)
    TIER_2_LIMIT_BPS = 2621440    # 2.5 MiB/s (10% ~ 20%)
    TIER_3_LIMIT_BPS = 5242880    # 5.0 MiB/s (20% ~ 70%, 峰值 5MB/s)
    TIER_4_LIMIT_BPS = 4194304    # 4.0 MiB/s (70% ~ 90%)
    TIER_5_LIMIT_BPS = 1572864    # 1.5 MiB/s (90% ~ 100%)

    COMPLETED_STATES = {
        "uploading",
        "stalledUP",
        "forcedUP",
        "checkingUP",
        "queuedUP",
    }

    @staticmethod
    def _is_shadow_paired(torrent: Dict) -> bool:
        """判定是否为发布期影子配对任务（必须恒定保持暂停，严禁调速器唤醒下载）"""
        tags = [x.strip().lower() for x in (torrent.get("tags") or "").split(",") if x.strip()]
        return "paired" in tags

    def __init__(
        self,
        name: str,
        port: int,
        mount_path: str,
        host: str = "192.168.0.7",
        concurrency: int = 20,
        stalled_timeout: int = 300,
        sort_by: str = "size_asc",
    ):
        self.name = name
        self.port = port
        self.mount_path = mount_path
        self.host = host
        self.concurrency = max(1, concurrency)
        self.stalled_timeout = stalled_timeout
        self.sort_by = sort_by.lower()
        self.base_url = f"http://{host}:{port}/api/v2"

        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor()
        )

        # 状态记录账本
        self.dispatched_hashes: Set[str] = set()
        self.active_trackers: Dict[str, Dict] = {}  # hash -> {started_at, last_progress, last_change_ts, name, size}
        self.stalled_quarantine: Set[str] = set()
        self.completed_hashes: Set[str] = set()
        self._prefs_tuned = False

    def _post(self, endpoint: str, data_dict: Dict[str, Any], timeout: int = 25) -> bool:
        url = f"{self.base_url}{endpoint}"
        data = urllib.parse.urlencode(data_dict).encode("utf-8")
        try:
            with self.opener.open(urllib.request.Request(url, data=data), timeout=timeout) as resp:
                return resp.getcode() == 200
        except Exception as e:
            logger.warning(f"[{self.name}:{self.port}] POST {endpoint} 失败: {e}")
            return False

    def _get(self, endpoint: str, timeout: int = 40) -> Any:
        url = f"{self.base_url}{endpoint}"
        try:
            with self.opener.open(urllib.request.Request(url), timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"[{self.name}:{self.port}] GET {endpoint} 失败: {e}")
            return None

    def ensure_queue_and_peer_prefs(self):
        """自动调优连接数与并发槽位配置（防排队与慢速）"""
        try:
            prefs = self._get("/app/preferences", timeout=15)
            if not prefs or not isinstance(prefs, dict):
                return
            changes = {}
            if int(prefs.get("max_connec_per_torrent", 0)) < 25:
                changes["max_connec_per_torrent"] = 25
            if int(prefs.get("max_connec", 0)) < 1000:
                changes["max_connec"] = 1000
            if int(prefs.get("max_active_downloads", 0)) < 60:
                changes["max_active_downloads"] = 60

            if changes:
                ok = self._post("/app/setPreferences", {"json": json.dumps(changes)})
                if ok:
                    logger.info(f"[{self.name}:{self.port}] ★ 连接与并发配置调优完成: {changes}")
            self._prefs_tuned = True
        except Exception as e:
            logger.warning(f"[{self.name}:{self.port}] 自动调优偏好设置跳过: {e}")

    def calculate_limits(self, active_torrents: List[Dict]) -> Dict[str, Tuple[int, str]]:
        """计算每个活跃下载种子的目标阶梯限速与档位标签"""
        decisions: Dict[str, Tuple[int, str]] = {}
        for t in active_torrents:
            h = t["hash"]
            p = t.get("progress", 0.0)
            if p < 0.10:
                decisions[h] = (self.TIER_1_LIMIT_BPS, "1档·预热(1.0M)")
            elif p < 0.20:
                decisions[h] = (self.TIER_2_LIMIT_BPS, "2档·爬坡(2.5M)")
            elif p < 0.70:
                decisions[h] = (self.TIER_3_LIMIT_BPS, "3档·极速(5.0M)")
            elif p < 0.90:
                decisions[h] = (self.TIER_4_LIMIT_BPS, "4档·高速(4.0M)")
            else:
                decisions[h] = (self.TIER_5_LIMIT_BPS, "5档·收尾(1.5M)")
        return decisions

    def apply_governance_limits(self, active_torrents: List[Dict]):
        """批量下发限速（仅在变动 >= 50KB/s 时写入，避免高频写入 qB）"""
        if not active_torrents:
            return

        decisions = self.calculate_limits(active_torrents)
        t_map = {t["hash"]: t for t in active_torrents}

        groups: Dict[int, List[str]] = {}
        for h, (target_limit, _) in decisions.items():
            curr_limit = int(t_map.get(h, {}).get("dl_limit", 0))
            if curr_limit == 0 or abs(target_limit - curr_limit) >= 51200:
                groups.setdefault(target_limit, []).append(h)

        for limit_bps, hashes in groups.items():
            self._post("/torrents/setDownloadLimit", {
                "hashes": "|".join(hashes),
                "limit": limit_bps
            })

    def resume_torrents(self, hashes: List[str]) -> bool:
        if not hashes:
            return True
        h_str = "|".join(hashes)
        for ep in ["start", "resume"]:
            if self._post(f"/torrents/{ep}", {"hashes": h_str}):
                return True
        return False

    def stop_torrents(self, hashes: List[str]) -> bool:
        if not hashes:
            return True
        h_str = "|".join(hashes)
        for ep in ["stop", "pause"]:
            if self._post(f"/torrents/{ep}", {"hashes": h_str}):
                return True
        return False

    def _sort_candidates(self, candidates: List[Dict]) -> List[Dict]:
        """按排序策略对待下载任务排序"""
        if self.sort_by == "size_asc":
            return sorted(candidates, key=lambda x: x.get("size", 0))
        elif self.sort_by == "size_desc":
            return sorted(candidates, key=lambda x: -x.get("size", 0))
        elif self.sort_by == "fifo":
            return sorted(candidates, key=lambda x: x.get("added_on", 0))
        return sorted(candidates, key=lambda x: x.get("size", 0))

    def step(self) -> Dict[str, Any]:
        """执行单次巡检与调速"""
        now = time.time()
        if not self._prefs_tuned:
            self.ensure_queue_and_peer_prefs()

        all_torrents = self._get("/torrents/info")
        if not all_torrents or not isinstance(all_torrents, list):
            return {"active_dl": 0, "total_incomplete": 0, "completed": 0, "speed_bps": 0}

        t_map = {t["hash"]: t for t in all_torrents}
        all_incomplete = [t for t in all_torrents if float(t.get("progress", 0.0)) < 1.0]

        # 识别并收敛被误唤醒的发布期影子配对任务：必须恒定保持暂停，严禁调速器下载
        shadow_active_hashes = [
            t["hash"] for t in all_incomplete
            if self._is_shadow_paired(t) and t.get("state") not in ("stoppedDL", "pausedDL")
        ]
        if shadow_active_hashes:
            logger.warning(
                f"[{self.name}:{self.port}] ⚠️ [影子隔离] 检测到 {len(shadow_active_hashes)} 个发布期影子配对任务处于活跃态，"
                f"强制恢复暂停防偷跑下载"
            )
            self.stop_torrents(shadow_active_hashes)

        # 过滤掉所有影子配对任务，不参与调速器槽位与唤醒排队
        all_incomplete = [t for t in all_incomplete if not self._is_shadow_paired(t)]

        # 若无未完成任务，快速返回
        if not all_incomplete:
            if self.dispatched_hashes:
                self.dispatched_hashes.clear()
                self.active_trackers.clear()
            return {"active_dl": 0, "total_incomplete": 0, "completed": len(all_torrents), "speed_bps": 0}

        # 自愈同步：若在途集合为空但已有下载态任务，纳入在途管理（受并发上限约束）
        if not self.dispatched_hashes:
            active_cands = [t for t in all_incomplete if t.get("state") not in ("stoppedDL", "pausedDL")]
            if active_cands:
                # 若已有活跃任务超过并发限制，执行并发收敛：保留最优小包，暂停超额任务
                if len(active_cands) > self.concurrency:
                    sorted_cands = self._sort_candidates(active_cands)
                    keep_cands = sorted_cands[:self.concurrency]
                    excess_cands = sorted_cands[self.concurrency:]
                    excess_hashes = [t["hash"] for t in excess_cands]
                    logger.warning(
                        f"[{self.name}:{self.port}] ⚠️ [并发收敛] 检测到 {len(active_cands)} 个未暂停任务超出槽位 ({self.concurrency})，"
                        f"自动暂停 {len(excess_hashes)} 个后备任务，保留最优 {len(keep_cands)} 个小包优先下载"
                    )
                    self.stop_torrents(excess_hashes)
                    active_cands = keep_cands

                for t in active_cands:
                    h = t["hash"]
                    self.dispatched_hashes.add(h)
                    self.active_trackers[h] = {
                        "started_at": now,
                        "last_progress": float(t.get("progress", 0.0)),
                        "last_change_ts": now,
                        "name": t.get("name", "Unknown"),
                        "size": t.get("size", 0),
                    }

        freed_hashes = []
        current_active_torrents = []
        total_dlspeed = 0

        for h in list(self.dispatched_hashes):
            t = t_map.get(h)
            if not t or self._is_shadow_paired(t):
                freed_hashes.append(h)
                continue

            prog = float(t.get("progress", 0.0))
            st = t.get("state", "")
            dlspeed = int(t.get("dlspeed", 0))
            total_dlspeed += dlspeed

            # 100% 完工转正做种判定
            if prog >= 1.0 or st in self.COMPLETED_STATES:
                nm = t.get("name", "Unknown")[:35]
                logger.info(f"[{self.name}] ★ [下载完成] '{nm}' 已转正做种，释放槽位")
                self.completed_hashes.add(h)
                freed_hashes.append(h)
            else:
                current_active_torrents.append(t)
                if h in self.active_trackers:
                    tr = self.active_trackers[h]
                    if prog > tr["last_progress"] + 0.001:
                        tr["last_progress"] = prog
                        tr["last_change_ts"] = now

                    # 僵死超时检测 (0 速度无源停滞)
                    time_since_change = now - tr["last_change_ts"]
                    if time_since_change >= self.stalled_timeout and dlspeed < 10240:
                        nm = t.get("name", "Unknown")[:35]
                        logger.warning(f"[{self.name}] ⚠️ [无源超时] '{nm}' 已停滞 {int(time_since_change)}s，自动暂停让位")
                        self.stop_torrents([h])
                        self.stalled_quarantine.add(h)
                        freed_hashes.append(h)

        for h in freed_hashes:
            self.dispatched_hashes.discard(h)
            self.active_trackers.pop(h, None)

        # 唤醒空闲槽位
        slots_free = self.concurrency - len(self.dispatched_hashes)
        if slots_free > 0:
            candidates = [
                t for t in all_incomplete
                if t["hash"] not in self.dispatched_hashes and t["hash"] not in self.stalled_quarantine
            ]
            if not candidates and self.stalled_quarantine:
                self.stalled_quarantine.clear()
                candidates = [
                    t for t in all_incomplete
                    if t["hash"] not in self.dispatched_hashes
                ]

            if candidates:
                sorted_cands = self._sort_candidates(candidates)
                to_wake = sorted_cands[:slots_free]
                wake_hashes = []
                for t in to_wake:
                    h = t["hash"]
                    self.dispatched_hashes.add(h)
                    self.active_trackers[h] = {
                        "started_at": now,
                        "last_progress": float(t.get("progress", 0.0)),
                        "last_change_ts": now,
                        "name": t.get("name", "Unknown"),
                        "size": t.get("size", 0),
                    }
                    wake_hashes.append(h)
                    current_active_torrents.append(t)

                self.resume_torrents(wake_hashes)

        # 执行五档位阶梯限速批量治理
        self.apply_governance_limits(current_active_torrents)

        return {
            "active_dl": len(self.dispatched_hashes),
            "total_incomplete": len(all_incomplete),
            "completed": len(all_torrents) - len(all_incomplete),
            "speed_bps": total_dlspeed,
        }


class MultiPoolDownloadGovernor:
    """纳管全矩阵 8 个音乐 QB 实例与 3015 下载缓冲实例的多池下载阶梯治理器"""

    def __init__(
        self,
        host: str = "192.168.0.7",
        concurrency_per_pool: int = 20,
        stalled_timeout: int = 300,
        check_interval: float = 12.0,
        govern_download_pool: bool = True,
    ):
        self.host = host
        self.concurrency_per_pool = concurrency_per_pool
        self.stalled_timeout = stalled_timeout
        self.check_interval = check_interval
        self.govern_download_pool = govern_download_pool

        # 初始化 8 个音乐保种物理池治理器
        self.governors: Dict[str, SinglePoolDownloadGovernor] = {}
        for name, port, mount_path in POOL_SPECS:
            self.governors[name] = SinglePoolDownloadGovernor(
                name=name,
                port=port,
                mount_path=mount_path,
                host=self.host,
                concurrency=self.concurrency_per_pool,
                stalled_timeout=self.stalled_timeout,
            )

        # 纳管 3015 与 3016 下载缓冲池治理器
        if self.govern_download_pool:
            self.governors["download"] = SinglePoolDownloadGovernor(
                name="download",
                port=3015,
                mount_path="/volume1/video/download/.downloading",
                host=self.host,
                concurrency=self.concurrency_per_pool,
                stalled_timeout=self.stalled_timeout,
            )
            self.governors["musicdownload"] = SinglePoolDownloadGovernor(
                name="musicdownload",
                port=3016,
                mount_path="/volume2/qb_temp/completed_musicdownload",
                host=self.host,
                concurrency=self.concurrency_per_pool,
                stalled_timeout=self.stalled_timeout,
            )

    def apply_all_pools_step(self) -> Dict[str, Dict[str, Any]]:
        """全池推进单步治理"""
        results: Dict[str, Dict[str, Any]] = {}
        for name, gov in self.governors.items():
            try:
                res = gov.step()
                results[name] = res
            except Exception as e:
                logger.error(f"[{name}] 下载阶梯调速治理异常: {e}")
                results[name] = {"error": str(e)}
        return results
