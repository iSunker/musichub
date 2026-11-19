# -*- coding: utf-8 -*-
"""MusicHub 3.0 跨站多目标发种决策网关 (DeduplicationGateway)

负责遍历目标站点适配器，结合本地持久图（RoutingGraphManager）与站内实时搜索，
生成最终派发清单。
"""

import logging
from typing import Dict, List, Tuple
from musichub.core.models import CanonicalMusicItem
from musichub.adapters.base import BaseSiteAdapter
from musichub.routing.graph import RoutingGraphManager

logger = logging.getLogger("DeduplicationGateway")


class DeduplicationGateway:
    def __init__(self, adapters: List[BaseSiteAdapter], graph_mgr: RoutingGraphManager):
        self.adapters = adapters
        self.graph_mgr = graph_mgr

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
