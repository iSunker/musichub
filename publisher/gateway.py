# -*- coding: utf-8 -*-
"""MusicHub 3.0 跨站多目标发种决策网关 (DeduplicationGateway)

负责遍历目标站点适配器，结合本地持久图（RoutingGraphManager）与站内实时搜索，
生成最终派发清单。
"""

import time
import logging
from typing import Dict, List, Optional, Tuple
from musichub.core.models import CanonicalMusicItem
from musichub.adapters.base import BaseSiteAdapter
from musichub.routing.graph import RoutingGraphManager

logger = logging.getLogger("DeduplicationGateway")


def evaluate_source_policy(
    graph_mgr: RoutingGraphManager,
    item: CanonicalMusicItem,
    target_site: str,
    adapter: Optional[BaseSiteAdapter] = None,
) -> Tuple[bool, str]:
    """源种准入闸门（查重之前的第一道闸）

    判定顺序固定：
    1. 源站转载规则：DENY 永久拦；NEED_CONFIRM 拦下待人工
    2. publish_after 等待期（默认 0，即不生效）
    3. 自有「源站 × 目标站」矩阵
    4. 元数据缺失（NEED_META）且目标站要求严格时拦下

    没有 source_tasks 记录（本地存量/极速通道）视为 `POLICY_NO_TASK` 放行，
    避免旧数据因缺任务行被整体卡死。
    """
    task = graph_mgr.get_source_task(getattr(item, "info_hash", "") or "")
    if not task:
        return True, "POLICY_NO_TASK(local)"

    rule = str(task.get("transfer_rule") or "NEED_CONFIRM").upper()
    if rule == "DENY":
        return False, "POLICY_RULE_DENY"
    if rule != "ALLOW":
        return False, "POLICY_NEED_CONFIRM"

    publish_after = float(task.get("publish_after") or 0.0)
    if publish_after > time.time():
        return False, f"POLICY_DELAY(until={int(publish_after)})"

    source_site = task.get("source_site") or "unknown"
    if not graph_mgr.is_target_allowed(source_site, target_site):
        return False, f"POLICY_MATRIX_BLOCK({source_site}->{target_site})"

    if str(task.get("meta_status") or "") == "NEED_META" and getattr(adapter, "metadata_strict", False):
        return False, "POLICY_NEED_META"

    return True, "POLICY_ALLOW"



class DeduplicationGateway:
    def __init__(self, adapters: List[BaseSiteAdapter], graph_mgr: RoutingGraphManager):
        self.adapters = adapters
        self.graph_mgr = graph_mgr

    def evaluate_source_policy(self, item: CanonicalMusicItem, target_site: str, adapter=None) -> Tuple[bool, str]:
        """实例方法包装：便于 daemon / 存量泵直接调用"""
        return evaluate_source_policy(self.graph_mgr, item, target_site, adapter)

    def evaluate_targets(self, item: CanonicalMusicItem) -> Dict[str, Tuple[bool, str]]:
        """为规范化音乐资源生成目标站发布决策字典

        返回格式：
        {
            "Musopia": (True, "Ready to publish"),
            "CrabPT": (False, "Exists on target site"),
            "PTerClub": (False, "Blocked by routing graph (already published)"),
            ...
        }
        """
        decision_map: Dict[str, Tuple[bool, str]] = {}

        for adapter in self.adapters:
            site = adapter.site_name

            # 第零层：源种准入（转载规则 / 等待期 / 站点矩阵 / 元数据分级）
            policy_ok, policy_reason = evaluate_source_policy(self.graph_mgr, item, site, adapter)
            if not policy_ok:
                decision_map[site] = (False, policy_reason)
                continue

            # 第一层：防环与持久图查重
            if self.graph_mgr.is_loop_or_already_published(item, site):
                decision_map[site] = (False, f"Blocked: loop routing or already published to {site}")
                continue

            # 第二层：目标站实时搜索比对
            try:
                exists, reason = adapter.check_exists(item)
                if exists:
                    decision_map[site] = (False, f"Exists on site: {reason}")
                    # 同步记录进图库，防止下轮重复搜索
                    self.graph_mgr.record_publish(item, site, target_torrent_id="EXISTING")
                else:
                    decision_map[site] = (True, "Target clear, allow publish")
            except Exception as e:
                logger.error(f"Failed to check exists on {site}: {str(e)}")
                decision_map[site] = (False, f"Check error: {str(e)}")

        return decision_map
