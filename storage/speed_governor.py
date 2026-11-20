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
    COMPLETED_LIMIT_BPS = 51200 # 达标后保底保种速度 50 KB/s
    MIN_ALLOC_BPS = 102400     # 活动种子最小分配带宽 100 KB/s
    NEWCOMER_CAP_BPS = 102400  # 新种预热防抢占上限 100 KB/s
    STABLE_UPLOAD_THRESHOLD_BPS = 51200 # 判定稳定上传的速率阈值 (50 KB/s)

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

    def get_tier_and_weight(self, ratio: float) -> Tuple[float, str]:
        """根据当前分享率获取分配权重与阶梯描述"""
        if ratio >= self.HANDOFF_RATIO:
            return 0.0, f"已达标完成 (Ratio>={self.HANDOFF_RATIO:.2f})"
        for tier in self.TIERS:
            if tier.matches(ratio):
                return tier.weight, tier.desc
        return 0.20, "预热兜底"

    def calculate_distribution(self, torrents: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """核心调速算法：带「防新种挤占+出种优先保护」的动态加权分配"""
        decisions: Dict[str, Dict[str, Any]] = {}
        
        # 1. 筛选处于做种或上传生命周期的种子
        seeding_candidates = []
        for t in torrents:
            state = t.get("state", "").lower()
            if state in ("pausedup", "stoppedup", "error", "missingfiles"):
                continue
            seeding_candidates.append(t)

        if not seeding_candidates:
            return decisions

        # 2. 分类：达标待交棒 / 正在出种保护中 / 常规活跃阶段
        active_candidates = []
        protected_active = []
        newcomers = []
        
        allocated_reserved_bw = 0

        for t in seeding_candidates:
            h = t.get("hash")
            ratio = float(t.get("ratio", 0.0))
            upspeed = int(t.get("upspeed", 0))
            current_limit = int(t.get("up_limit", 0))

            if ratio >= self.HANDOFF_RATIO:
                # 达标种子：限制在保底 50 KB/s
                decisions[h] = {
                    "name": t.get("name", ""),
                    "ratio": ratio,
                    "target_limit": self.COMPLETED_LIMIT_BPS,
                    "current_limit": current_limit,
                    "tier_desc": f"达标保底 (Ratio={ratio:.2f}>={self.HANDOFF_RATIO})",
                    "weight": 0.0,
                    "handoff_ready": True,
                }
                allocated_reserved_bw += self.COMPLETED_LIMIT_BPS
            else:
                weight, desc = self.get_tier_and_weight(ratio)
                item_info = {
                    "hash": h,
                    "name": t.get("name", ""),
                    "ratio": ratio,
                    "upspeed": upspeed,
                    "current_limit": current_limit,
                    "weight": weight,
                    "tier_desc": desc,
                }
                
                # 判断是否为「出种保护对象」：已经爬坡或全速，且正在稳定上传 (upspeed >= 50KB/s)
                if ratio >= 0.05 and upspeed >= self.STABLE_UPLOAD_THRESHOLD_BPS:
                    protected_active.append(item_info)
                elif ratio < 0.05:
                    newcomers.append(item_info)
                else:
                    active_candidates.append(item_info)

        # 3. 执行「防抢占保护」带宽切分：
        #    如果有正在全力出种的任务，必须给予优先权和保护份额
        has_active_seeding = (len(protected_active) > 0)
        
        # 3.1 对新种（Ratio < 0.05）：若有活跃主力正在出种，限制其抢占带宽上限
        newcomer_target_bw = self.NEWCOMER_CAP_BPS if has_active_seeding else self.MIN_ALLOC_BPS * 2
        for nc in newcomers:
            h = nc["hash"]
            decisions[h] = {
                "name": nc["name"],
                "ratio": nc["ratio"],
                "target_limit": newcomer_target_bw,
                "current_limit": nc["current_limit"],
                "tier_desc": f"预热起步 (防抢占限速 {newcomer_target_bw//1024}KB/s)",
                "weight": nc["weight"],
                "handoff_ready": False,
            }
            allocated_reserved_bw += newcomer_target_bw

        # 3.2 剩余带宽全量倾斜给「正在出种保护任务」与「常规活跃任务」
        remaining_bw = max(102400, self.total_bandwidth - allocated_reserved_bw)
        
        main_candidates = protected_active + active_candidates
        total_main_weight = 0.0
        
        # 保护任务权重加成：赋予保护任务 2.0x 动态加成，确保不失速
        for item in main_candidates:
            if item in protected_active:
                item["effective_weight"] = item["weight"] * 2.0
                item["is_protected"] = True
            else:
                item["effective_weight"] = item["weight"]
                item["is_protected"] = False
            total_main_weight += item["effective_weight"]

        for item in main_candidates:
            h = item["hash"]
            ew = item["effective_weight"]
            if total_main_weight > 0:
                raw_limit = int(remaining_bw * (ew / total_main_weight))
            else:
                raw_limit = int(remaining_bw / len(main_candidates))

            # 保护任务确保下限不低于 250 KB/s (在总预算允许下)
            min_floor = 256000 if item["is_protected"] else self.MIN_ALLOC_BPS
            target_limit = max(min_floor, raw_limit)
            
            p_tag = "【出种保护】" if item["is_protected"] else ""
            decisions[h] = {
                "name": item["name"],
                "ratio": item["ratio"],
                "target_limit": target_limit,
                "current_limit": item["current_limit"],
                "tier_desc": f"{p_tag}{item['tier_desc']} (动态限速 {target_limit//1024}KB/s)",
                "weight": ew,
                "handoff_ready": False,
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

        for h, plan in decisions.items():
            target_limit = plan["target_limit"]
            curr_limit = plan["current_limit"]
            handoff_ready = plan["handoff_ready"]

            diff = abs(target_limit - curr_limit)
            if curr_limit == 0 or diff >= 51200:
                adjusted_count += 1
                if not dry_run:
                    self.set_torrent_upload_limit(h, target_limit)

            if handoff_ready and self.manage_handoff_tag:
                handoff_tagged_count += 1
                if not dry_run:
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
