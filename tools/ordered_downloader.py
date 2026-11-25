# -*- coding: utf-8 -*-
"""MusicHub 3.0 有序并发下载与阶梯调速器 (Ordered Downloader & Speed Governor)

设计定位：
针对 qB 实例（如 3260 OpenCD 主库）中批量推入的大量默认暂停任务（如 532 个 stoppedDL 种子），
参考 3010 SpeedGovernor 的阶梯调速与带宽治理架构，实施受控、平滑、有序的下行并发流水线调度。

核心机制与特色能力：
1. 【尽量吃满带宽 · 动态自适应并发 (Adaptive Bandwidth Saturation)】：
   - 突破固定 3 并发的吞吐瓶颈：基线 5 并发，若总下载速度未达目标带宽（默认 50 MB/s），
     自动动态爬坡扩容至 10~15 并发，从小包优先队列多路并发拉取，彻底吃透下行带宽；
   - 一旦总速度打满目标带宽（>= 90%），自动稳态维持，保护机械盘寻道不过载。
2. 【对标 3010 的阶梯调速模型 (Ladder Download Tiers)】：
   - 预热起步 (0% <= progress < 5%)：单种上限 4 MiB/s，防新种暴涌争抢连接；
   - 主力冲刺 (5% <= progress < 85%)：单种上限 16 MiB/s，给足上限，让高速优质 peer 极速下载；
   - 收尾出种 (85% <= progress < 100%)：单种上限 24 MiB/s，最高优先级保障，尽快补齐最后几块；
   - 批量分组下发 setDownloadLimit，网络接口零冗余。
3. 【底层连接数自动调优 (ensure_prefs)】：
   - 自动将 3260 的单种连接数从保守的 3 提升至 15 (max_connec_per_torrent=15)，
     彻底根治“单种只能连 3 个 peer 导致速度跑不起来”的历史硬瓶颈；
   - 放开并发下载槽位 (max_active_downloads=50)。
4. 【无源/僵死超时防死锁 (Anti-Deadlock Timeout)】：
   - 0 速度无源种子超过 5 分钟自动 stop 并移入暂缓队列，绝不浪费宝贵并发槽位。
5. 【严格保护做种】：
   - 绝不触碰任何已完成（progress >= 1.0 / stalledUP）的 2600+ 个在保母种。
"""

import sys
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import time
import signal
import logging
import argparse
import urllib.request
import urllib.parse
import json
import re
from typing import Dict, List, Optional, Set, Tuple, Any

from musichub.storage.pool_pairing import POOL_SPECS
from musichub.storage.download_governor import MultiPoolDownloadGovernor

POOL_PORT_MAP = {spec[0]: spec[1] for spec in POOL_SPECS}
POOL_PORT_MAP["download"] = 3015
POOL_PORT_MAP["musicdownload"] = 3016
POOL_PORT_MAP["3015"] = 3015
POOL_PORT_MAP["3016"] = 3016

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("OrderedDownloader")


def parse_bandwidth_str(val: str) -> int:
    """将 '50MB', '40M', '500KB', '1048576' 等字符串解析为 bytes/s"""
    if not val:
        return 0
    s = str(val).strip().upper()
    if s.endswith("GB") or s.endswith("G"):
        num = float(re.sub(r"[^\d.]", "", s))
        return int(num * 1024 ** 3)
    elif s.endswith("MB") or s.endswith("M"):
        num = float(re.sub(r"[^\d.]", "", s))
        return int(num * 1024 ** 2)
    elif s.endswith("KB") or s.endswith("K"):
        num = float(re.sub(r"[^\d.]", "", s))
        return int(num * 1024)
    else:
        try:
            return int(float(s))
        except Exception:
            return 0


def format_bytes(size_bytes: float) -> str:
    """人类可读的文件大小格式化"""
    if size_bytes < 1024:
        return f"{size_bytes:.0f} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / (1024 ** 2):.1f} MB"
    else:
        return f"{size_bytes / (1024 ** 3):.2f} GB"


def format_speed(speed_bytes: float) -> str:
    """人类可读的速率格式化"""
    return f"{format_bytes(speed_bytes)}/s"


def format_duration(seconds: float) -> str:
    """时分秒格式化"""
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    elif m > 0:
        return f"{m}m {s}s"
    else:
        return f"{s}s"


class SafeQBClient:
    """带高超时、直连与调速机制的增强型 qB 客户端"""
    def __init__(self, host: str = "192.168.0.7", port: int = 3260, timeout: int = 40):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.base_url = f"http://{host}:{port}/api/v2"
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor()
        )

    def _post(self, endpoint: str, data_dict: Dict[str, Any]) -> bool:
        url = f"{self.base_url}{endpoint}"
        data = urllib.parse.urlencode(data_dict).encode("utf-8")
        try:
            with self.opener.open(urllib.request.Request(url, data=data), timeout=25) as resp:
                return resp.getcode() == 200
        except Exception as e:
            logger.warning(f"POST {endpoint} failed: {e}")
            return False

    def get_torrents(self, filter_status: Optional[str] = None) -> List[Dict]:
        url = f"{self.base_url}/torrents/info"
        if filter_status:
            url += f"?filter={filter_status}"
        req = urllib.request.Request(url)
        with self.opener.open(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_preferences(self) -> Dict[str, Any]:
        url = f"{self.base_url}/app/preferences"
        with self.opener.open(urllib.request.Request(url), timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def set_preferences(self, prefs_dict: Dict[str, Any]) -> bool:
        return self._post("/app/setPreferences", {"json": json.dumps(prefs_dict)})

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

    def set_download_limit_bulk(self, hashes: List[str], limit_bps: int) -> bool:
        """批量设定下载限速（对齐 3010 调速器 set_upload_limit_bulk）"""
        if not hashes:
            return True
        return self._post("/torrents/setDownloadLimit", {
            "hashes": "|".join(hashes),
            "limit": limit_bps
        })


class DownloadSpeedGovernor:
    """五档位精细化阶梯下载调速器 (精准对齐用户调速模型)
    
    5 级档位分布设计：
    - 档位 1 [0% ~ 10%]:  初始预热低速 -> 1.0 MiB/s (平稳握手建连，防握手突发)
    - 档位 2 [10% ~ 20%]: 爬坡加速低速 -> 2.5 MiB/s (前 20% 低速过渡)
    - 档位 3 [20% ~ 70%]: 主力峰值极速 -> 5.0 MiB/s (最高 5 兆单种，拉满带宽)
    - 档位 4 [70% ~ 90%]: 高位稳态高速 -> 4.0 MiB/s (20%~90% 高速区间)
    - 档位 5 [90% ~ 100%]: 尾盘减速防抖 -> 1.5 MiB/s (最后 10% 低速收尾，防磁盘寻道与写缓冲区阻塞)
    """
    TIER_1_LIMIT_BPS = 1048576    # 1.0 MiB/s (0% ~ 10%)
    TIER_2_LIMIT_BPS = 2621440    # 2.5 MiB/s (10% ~ 20%)
    TIER_3_LIMIT_BPS = 5242880    # 5.0 MiB/s (20% ~ 70%, 峰值 5MB/s)
    TIER_4_LIMIT_BPS = 4194304    # 4.0 MiB/s (70% ~ 90%)
    TIER_5_LIMIT_BPS = 1572864    # 1.5 MiB/s (90% ~ 100%)

    def __init__(self, qb_client: SafeQBClient):
        self.qb = qb_client

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

    def apply_governance(self, active_torrents: List[Dict]):
        """批量下发限速（仅在变动 >= 50KB/s 时写入，避免高频打扰 qB）"""
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
            self.qb.set_download_limit_bulk(hashes, limit_bps)


class OrderedDownloader:
    COMPLETED_STATES = {
        "uploading",
        "stalledUP",
        "forcedUP",
        "checkingUP",
        "queuedUP",
    }

    def __init__(
        self,
        host: str = "192.168.0.7",
        port: int = 3260,
        concurrency: int = 20,
        concurrency_min: int = 10,
        concurrency_max: int = 20,
        target_bandwidth: str = "100MB",
        sort_by: str = "size_asc",
        check_interval: int = 12,
        stalled_timeout: int = 300,
        max_total_downloads: int = 0,
    ):
        self.host = host
        self.port = port
        if concurrency > 0:
            self.concurrency_min = concurrency
            self.concurrency_max = concurrency
            self.current_concurrency_cap = concurrency
        else:
            self.concurrency_min = max(1, concurrency_min)
            self.concurrency_max = max(self.concurrency_min, concurrency_max)
            self.current_concurrency_cap = self.concurrency_min
        self.target_bandwidth_bps = parse_bandwidth_str(target_bandwidth) or 104857600  # 默认 100MB/s

        self.sort_by = sort_by.lower()
        self.check_interval = max(5, check_interval)
        self.stalled_timeout = stalled_timeout
        self.max_total_downloads = max_total_downloads

        self.qb = SafeQBClient(host=self.host, port=self.port, timeout=40)
        self.governor = DownloadSpeedGovernor(self.qb)
        self.running = True

        # 核心跟踪账本
        self.dispatched_hashes: Set[str] = set()
        self.active_trackers: Dict[str, Dict] = {}  # hash -> {started_at, last_progress, last_change_ts, name, size}
        self.stalled_quarantine: Set[str] = set()    # 0 速度超时的无源死种暂缓集合
        self.completed_hashes: Set[str] = set()

    def ensure_queue_and_peer_prefs(self):
        """自动调优队列并发槽位与单种连接数上限（保障 20 并发与 5MB/s 稳定吞吐）"""
        try:
            prefs = self.qb.get_preferences()
            changes = {}
            # 提升单种最大连接数：提升到 25，确保单种在极速档位能连上足够 peer 冲到 5MB/s
            if int(prefs.get("max_connec_per_torrent", 0)) < 25:
                changes["max_connec_per_torrent"] = 25
            # 全局最大连接数放开到 1000
            if int(prefs.get("max_connec", 0)) < 1000:
                changes["max_connec"] = 1000
            # 放开 qB 内置下载并发槽位到 60，避免 20 个活跃外部唤醒被内部排队机制 queuedDL 拦截
            if int(prefs.get("max_active_downloads", 0)) < 60:
                changes["max_active_downloads"] = 60

            if changes:
                self.qb.set_preferences(changes)
                logger.info(f"★ {self.port} 连接与并发配置已完成最优调优: {changes}")
        except Exception as e:
            logger.warning(f"自动调优 {self.port} 偏好设置跳过: {e}")

    def inspect_status(self) -> Dict:
        """只读体检：统计当前做种与下载概况"""
        try:
            all_torrents = self.qb.get_torrents()
        except Exception as e:
            logger.error(f"无法获取任务列表: {e}")
            return {}

        total = len(all_torrents)
        completed = [t for t in all_torrents if t.get("progress", 0) >= 1.0]
        incomplete = [t for t in all_torrents if t.get("progress", 0) < 1.0]

        states: Dict[str, int] = {}
        for t in all_torrents:
            st = t.get("state", "unknown")
            states[st] = states.get(st, 0) + 1

        active_dl = [t for t in incomplete if t.get("state") not in ("stoppedDL", "pausedDL")]
        stopped_dl = [t for t in incomplete if t.get("state") in ("stoppedDL", "pausedDL")]
        total_inc_size = sum(t.get("size", 0) for t in incomplete)

        print("\n" + "=" * 68)
        print(f"★ qBittorrent 下载调度看板 [{self.host}:{self.port}]")
        print("=" * 68)
        print(f"总任务数:          {total} 个")
        print(f"已完成 (做种中):   {len(completed)} 个")
        print(f"待完成 (下载中):   {len(incomplete)} 个 (总数据量: {format_bytes(total_inc_size)})")
        print(f"  ├─ 正在活跃下载:  {len(active_dl)} 个")
        print(f"  └─ 处于暂停队列: {len(stopped_dl)} 个")
        print("-" * 68)
        print("状态细分分布:")
        for st, cnt in sorted(states.items(), key=lambda x: -x[1]):
            print(f"  - {st:<18}: {cnt} 个")
        print("=" * 68 + "\n")

        return {
            "total": total,
            "completed": len(completed),
            "incomplete": len(incomplete),
            "active_dl": len(active_dl),
            "stopped_dl": len(stopped_dl),
        }

    def emergency_stop_all(self):
        """紧急制动：暂停所有未完成的下载任务"""
        try:
            all_torrents = self.qb.get_torrents()
        except Exception as e:
            logger.error(f"紧急制动获取列表失败: {e}")
            return

        incomplete = [t for t in all_torrents if t.get("progress", 0) < 1.0 and t.get("state") not in ("stoppedDL", "pausedDL")]
        if not incomplete:
            logger.info("当前没有活跃下载任务。")
            return

        logger.warning(f"正在对 {len(incomplete)} 个活跃下载任务下发暂停 (stop)...")
        hashes = [t["hash"] for t in incomplete]
        ok = self.qb.stop_torrents(hashes)
        logger.info(f"紧急制动完毕: {'成功' if ok else '部分失败'}")

    def _sort_candidates(self, candidates: List[Dict]) -> List[Dict]:
        """按策略对待下载任务进行排序"""
        if self.sort_by == "size_asc":
            return sorted(candidates, key=lambda x: x.get("size", 0))
        elif self.sort_by == "size_desc":
            return sorted(candidates, key=lambda x: -x.get("size", 0))
        elif self.sort_by == "fifo":
            return sorted(candidates, key=lambda x: x.get("added_on", 0))
        else:
            return sorted(candidates, key=lambda x: x.get("size", 0))

    def run(self, dry_run: bool = False):
        """执行主循环流水线"""
        logger.info(f"=== 启动有序并发与阶梯调速流水线 [{self.host}:{self.port}] ===")
        logger.info(
            f"目标吞吐带宽: {format_speed(self.target_bandwidth_bps)} | "
            f"并发动态自适应区间: [{self.concurrency_min} ~ {self.concurrency_max}] | "
            f"排序策略: {self.sort_by} | 轮询周期: {self.check_interval}s | 僵死超时: {self.stalled_timeout}s | Dry-Run: {dry_run}"
        )

        def _sig_handler(sig, frame):
            logger.warning("\n接收到中断信号 (Ctrl+C)，正在安全退出调度器...")
            self.running = False
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)

        if not dry_run:
            self.ensure_queue_and_peer_prefs()

        try:
            initial_torrents = self.qb.get_torrents()
        except Exception as e:
            logger.error(f"初始连接失败: {e}")
            return

        initial_incomplete = [t for t in initial_torrents if t.get("progress", 0) < 1.0]
        initial_count = len(initial_incomplete)
        initial_size = sum(t.get("size", 0) for t in initial_incomplete)

        if initial_count == 0:
            logger.info("当前实例中所有种子均已完成，无待下载任务！")
            return

        logger.info(f"待下载种子总数: {initial_count} 个，总数据量: {format_bytes(initial_size)}")

        if dry_run:
            print("\n" + "=" * 70)
            print("【Dry-Run 计划预览】待唤醒顺序前 15 个种子:")
            print("=" * 70)
            sorted_init = self._sort_candidates(initial_incomplete)
            for idx, t in enumerate(sorted_init[:15]):
                sz = format_bytes(t.get("size", 0))
                nm = t.get("name", "Unknown")[:45]
                prog = t.get("progress", 0) * 100
                st = t.get("state", "unknown")
                print(f"[{idx+1:2d}] {nm:<45} | 大小: {sz:<10} | 当前进度: {prog:5.1f}% | 状态: {st}")
            if len(sorted_init) > 15:
                print(f"... 其余 {len(sorted_init) - 15} 个种子依序排队")
            print("=" * 70 + "\n")
            return

        # 启动前自愈检查与超额并发收敛
        active_inits = [t for t in initial_incomplete if t.get("state") not in ("stoppedDL", "pausedDL")]
        if len(active_inits) > self.current_concurrency_cap:
            sorted_active = self._sort_candidates(active_inits)
            keep_active = sorted_active[:self.current_concurrency_cap]
            excess_active = sorted_active[self.current_concurrency_cap:]
            excess_hashes = [t["hash"] for t in excess_active]
            logger.warning(
                f"⚠️ [并发收敛] 检测到 {len(active_inits)} 个未暂停任务超出当前槽位 ({self.current_concurrency_cap})，"
                f"自动安全暂停 {len(excess_hashes)} 个后备任务，释放磁盘与排队压力，保留最优 {len(keep_active)} 个小包优先下载"
            )
            self.qb.stop_torrents(excess_hashes)
            active_inits = keep_active

        for t in active_inits:
            self.dispatched_hashes.add(t["hash"])
            self.active_trackers[t["hash"]] = {
                "started_at": time.time(),
                "last_progress": t.get("progress", 0),
                "last_change_ts": time.time(),
                "name": t.get("name", "Unknown"),
                "size": t.get("size", 0),
            }

        session_completed = 0
        round_idx = 0

        while self.running:
            round_idx += 1
            now = time.time()

            # 1. 安全拉取任务列表
            try:
                torrents = self.qb.get_torrents()
            except Exception as ex:
                logger.warning(f"拉取 qB 列表抖动/超时，{self.check_interval}s 后重试: {ex}")
                time.sleep(self.check_interval)
                continue

            # 容错防抖
            if not torrents:
                logger.warning(f"获取到的任务列表为空，跳过本轮判定...")
                time.sleep(self.check_interval)
                continue

            t_map = {t["hash"]: t for t in torrents}
            all_incomplete = [t for t in torrents if t.get("progress", 0) < 1.0]

            # 2. 检查在途任务的完成状态与僵死超时
            freed_hashes = []
            current_active_torrents = []
            for h in list(self.dispatched_hashes):
                t = t_map.get(h)
                if not t:
                    freed_hashes.append(h)
                    continue

                prog = t.get("progress", 0)
                st = t.get("state", "")

                # 完成判据：进度 100% 或进入做种态
                if prog >= 1.0 or st in self.COMPLETED_STATES:
                    elapsed = now - self.active_trackers.get(h, {}).get("started_at", now)
                    sz = format_bytes(t.get("size", 0))
                    nm = t.get("name", "Unknown")[:40]
                    session_completed += 1
                    logger.info(f"★ [下载完成] #{session_completed} '{nm}' (大小: {sz}, 耗时: {format_duration(elapsed)})")
                    self.completed_hashes.add(h)
                    freed_hashes.append(h)
                else:
                    current_active_torrents.append(t)
                    # 更新进度与活跃记录
                    if h in self.active_trackers:
                        tr = self.active_trackers[h]
                        if prog > tr["last_progress"] + 0.001:
                            tr["last_progress"] = prog
                            tr["last_change_ts"] = now

                        # 僵死超时检测 (0 速度无进度超时)
                        time_since_change = now - tr["last_change_ts"]
                        cur_speed = t.get("dlspeed", 0)
                        if time_since_change >= self.stalled_timeout and cur_speed < 10240:
                            nm = t.get("name", "Unknown")[:40]
                            logger.warning(f"⚠️ [无源超时] '{nm}' 已停滞 {format_duration(time_since_change)} 无有效速度，自动暂停并腾出槽位")
                            self.qb.stop_torrents([h])
                            self.stalled_quarantine.add(h)
                            freed_hashes.append(h)

            for h in freed_hashes:
                self.dispatched_hashes.discard(h)
                self.active_trackers.pop(h, None)

            if self.max_total_downloads > 0 and session_completed >= self.max_total_downloads:
                logger.info(f"已达成指定的最大下载数量配额 ({self.max_total_downloads} 个)，调度任务结束。")
                break

            if not all_incomplete:
                logger.info("🎉 恭喜！当前实例中所有种子均已完成下载，流水线圆满完成！")
                break

            # 3. 统计当前总下行速度并实施【动态自适应并发扩缩容】
            current_total_speed = sum(t_map.get(h, {}).get("dlspeed", 0) for h in self.dispatched_hashes)

            # 核心扩容机制：吃满带宽
            # 若当前总速度未达到目标带宽的 75%，且当前并发上限未触及 concurrency_max，并且当前在途已占满 -> 扩容 1~2 个槽位
            if current_total_speed < self.target_bandwidth_bps * 0.75:
                if len(self.dispatched_hashes) >= self.current_concurrency_cap and self.current_concurrency_cap < self.concurrency_max:
                    self.current_concurrency_cap = min(self.concurrency_max, self.current_concurrency_cap + 2)
                    logger.info(f"🚀 [带宽未满升档] 当前总速度 {format_speed(current_total_speed)} < 目标 75%，动态提升并发上限 -> {self.current_concurrency_cap}")
            elif current_total_speed >= self.target_bandwidth_bps * 0.95:
                # 带宽已打满，保持或微调收敛
                pass

            # 4. 计算空闲槽位并唤醒新任务
            slots_free = self.current_concurrency_cap - len(self.dispatched_hashes)
            if slots_free > 0:
                candidates = [
                    t for t in all_incomplete
                    if t["hash"] not in self.dispatched_hashes and t["hash"] not in self.stalled_quarantine
                ]

                # 若常规候选耗尽但有暂缓项，重置重试
                if not candidates and self.stalled_quarantine:
                    logger.info("常规候选已用尽，重新放开之前超时的暂缓种子尝试补速...")
                    self.stalled_quarantine.clear()
                    candidates = [
                        t for t in all_incomplete
                        if t["hash"] not in self.dispatched_hashes
                    ]

                if candidates:
                    sorted_cands = self._sort_candidates(candidates)
                    to_wake = sorted_cands[:slots_free]

                    wake_hashes = []
                    logger.info(f"唤醒 {len(to_wake)} 个新任务填补槽位 (当前在途: {len(self.dispatched_hashes)}/{self.current_concurrency_cap}, 目标带宽: {format_speed(self.target_bandwidth_bps)}):")
                    for t in to_wake:
                        h = t["hash"]
                        nm = t.get("name", "Unknown")[:40]
                        sz = format_bytes(t.get("size", 0))
                        logger.info(f"  ▶ 启动下载: '{nm}' ({sz})")
                        self.dispatched_hashes.add(h)
                        self.active_trackers[h] = {
                            "started_at": now,
                            "last_progress": t.get("progress", 0),
                            "last_change_ts": now,
                            "name": t.get("name", "Unknown"),
                            "size": t.get("size", 0),
                        }
                        wake_hashes.append(h)
                        current_active_torrents.append(t)

                    self.qb.resume_torrents(wake_hashes)

            # 5. 对标 3010：执行【阶梯下载限速批量治理】
            self.governor.apply_governance(current_active_torrents)

            # 6. 打印每轮看板
            remaining_count = len(all_incomplete)
            pct = ((initial_count - remaining_count) / initial_count * 100) if initial_count > 0 else 100.0
            quarantine_info = f" | 暂缓死种: {len(self.stalled_quarantine)}" if self.stalled_quarantine else ""
            bw_util_pct = (current_total_speed / self.target_bandwidth_bps * 100) if self.target_bandwidth_bps > 0 else 100.0

            logger.info(
                f"[轮次 {round_idx}] 总体进度: 已完成 {initial_count - remaining_count}/{initial_count} ({pct:.1f}%) "
                f"| 在途并发: {len(self.dispatched_hashes)}/{self.current_concurrency_cap} (上限 {self.concurrency_max}) "
                f"| 总下载速度: {format_speed(current_total_speed)} (带宽利用率: {bw_util_pct:.1f}%)"
                f"{quarantine_info}"
            )

            # 打印当前在途下载任务详情
            for h in sorted(self.dispatched_hashes):
                t = t_map.get(h, {})
                nm = t.get("name", self.active_trackers.get(h, {}).get("name", "Unknown"))[:32]
                p = t.get("progress", 0) * 100
                spd = format_speed(t.get("dlspeed", 0))
                eta_s = t.get("eta", 0)
                eta_str = format_duration(eta_s) if 0 < eta_s < 86400 * 30 else "未知"
                sz = format_bytes(t.get("size", 0))
                if p < 10.0:
                    tier_str = "1档·预热(1M)"
                elif p < 20.0:
                    tier_str = "2档·爬坡(2.5M)"
                elif p < 70.0:
                    tier_str = "3档·极速(5M)"
                elif p < 90.0:
                    tier_str = "4档·高速(4M)"
                else:
                    tier_str = "5档·收尾(1.5M)"
                logger.info(f"    ↓ [{nm}] {tier_str} | 进度: {p:5.1f}% | 大小: {sz} | 速度: {spd:<10} | 剩余: {eta_str}")

            # 7. 周期休眠
            for _ in range(self.check_interval):
                if not self.running:
                    break
                time.sleep(1)

        logger.info(f"=== 有序下载流水线已停止 | 本次运行共完成: {session_completed} 个任务 ===")


def main():
    parser = argparse.ArgumentParser(description="MusicHub 3.0 有序并发下载与阶梯调速器 (支持 8 大音乐 QB 实例与 3015 download 实例)")
    parser.add_argument("--pool", type=str, default="download", help="指定实例名称 (如 download/3015/music3/music1/music2/music_pool_1~5/all, 默认 download)")
    parser.add_argument("--host", type=str, default="192.168.0.7", help="qB 宿主 IP (默认 192.168.0.7)")
    parser.add_argument("--port", type=int, default=0, help="qB 端口 (若指定了 --pool 则自动匹配，亦可手动覆盖)")
    parser.add_argument("--concurrency", "-c", type=int, default=20, help="同时下载并发种子数 (默认 20 个)")
    parser.add_argument("--concurrency-min", type=int, default=10, help="自适应最小并发底线")
    parser.add_argument("--concurrency-max", "-m", type=int, default=20, help="自适应最大并发上限")
    parser.add_argument("--bandwidth", "-b", type=str, default="100MB", help="目标下载带宽 (默认 100MB)")
    parser.add_argument("--sort", type=str, default="size_asc", choices=["size_asc", "size_desc", "fifo"], help="下载排序策略 (默认 size_asc 小包优先)")
    parser.add_argument("--interval", "-i", type=int, default=12, help="状态轮询与调速周期秒数 (默认 12)")
    parser.add_argument("--stalled-timeout", type=int, default=300, help="0 速度无源僵死超时秒数 (默认 300 秒/5分钟)")
    parser.add_argument("--limit", type=int, default=0, help="总下载完成数量限制 (0 为不限制下载完全部)")
    parser.add_argument("--dry-run", action="store_true", help="仅预览排队计划，不实际启动任务")
    parser.add_argument("--status", action="store_true", help="仅只读体检当前实例做种与下载概况并退出")
    parser.add_argument("--stop-all", action="store_true", help="紧急制动：暂停所有未完成任务")

    args = parser.parse_args()

    # 若指定了 --pool all，直接驱动 MultiPoolDownloadGovernor
    if args.pool.lower() == "all":
        logger.info(f"=== 启动全矩阵 8 大音乐 QB 多池下载阶梯治理看板 ===")
        multi_gov = MultiPoolDownloadGovernor(
            host=args.host,
            concurrency_per_pool=args.concurrency,
            stalled_timeout=args.stalled_timeout,
            check_interval=float(args.interval),
        )
        try:
            while True:
                results = multi_gov.apply_all_pools_step()
                summary_lines = []
                for name, res in results.items():
                    act = res.get("active_dl", 0)
                    tot = res.get("total_incomplete", 0)
                    spd = res.get("speed_bps", 0) / 1048576.0
                    summary_lines.append(f"{name:<13}: 在途下载 {act:2d}/{tot:3d} | 速度 {spd:5.2f} MB/s")
                logger.info("多池状态: " + " | ".join(summary_lines[:4]))
                logger.info("          " + " | ".join(summary_lines[4:]))
                time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("全池治理已手动终止。")
        return

    # 单实例端口解析
    target_port = args.port
    if target_port == 0:
        pool_key = args.pool.lower()
        if pool_key in POOL_PORT_MAP:
            target_port = POOL_PORT_MAP[pool_key]
        else:
            target_port = 3260  # 默认 music3

    downloader = OrderedDownloader(
        host=args.host,
        port=target_port,
        concurrency=args.concurrency,
        concurrency_min=args.concurrency_min,
        concurrency_max=args.concurrency_max,
        target_bandwidth=args.bandwidth,
        sort_by=args.sort,
        check_interval=args.interval,
        stalled_timeout=args.stalled_timeout,
        max_total_downloads=args.limit,
    )

    if args.status:
        downloader.inspect_status()
        return

    if args.stop_all:
        downloader.emergency_stop_all()
        return

    downloader.run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
