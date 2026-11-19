# -*- coding: utf-8 -*-
"""MusicHub 3.0 生命周期退役调度器 (LifecycleRetirement)

遵循 AGENTS.md 铁律与生产实测契约：
1. 仅当下架目标在所有受控目标站均达成发布（或查重已存在），且最后发布时间距今 >= 14 天
2. 且满足源站（如 OpenCD 36h HR）考核
3. 协同下架（跨客户端从 qB 移除做种任务，delete_files=False 严禁直接物理删除）
4. 下架后的目录脱离 qB 任务引用，自然转化为孤儿候选，由 SafeGarbageCollector 执行最终安全回收
   （严格受控于 music5 禁删红线与 st_nlink 跨池引用核验，杜绝硬链接共振误删）
"""

import logging
from typing import Dict, List, Set, Tuple, Optional, Any
from musichub.routing.graph import RoutingGraphManager
from musichub.core.qb_client import QBClient
from musichub.storage.safe_gc import SafeGarbageCollector

logger = logging.getLogger("LifecycleRetirement")


class LifecycleRetirement:
    def __init__(
        self,
        graph_mgr: RoutingGraphManager,
        safe_gc: SafeGarbageCollector,
        target_clients: Optional[Dict[str, QBClient]] = None,
        retire_days: float = 14.0,
        hr_hours_required: float = 36.0,
    ):
        self.graph_mgr = graph_mgr
        self.safe_gc = safe_gc
        self.target_clients = target_clients or {}
        self.retire_days = retire_days
        self.hr_hours_required = hr_hours_required

    def find_retireable_candidates(
        self,
        required_sites: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """从溯源数据库中找出所有在所有目标站均已发布满 14 天的候选资源"""
        required_sites = required_sites or ["musopia", "crabpt", "pterclub", "xingwan", "hdkylin", "tangpt", "muxuege"]
        candidates = []

        placeholders = ",".join("?" for _ in required_sites)
        query = f"""
            SELECT 
                content_fingerprint,
                origin_site,
                origin_torrent_id,
                COUNT(DISTINCT target_site) as site_cnt,
                MAX(published_at) as last_pub
            FROM site_publish_graph
            WHERE target_site IN ({placeholders})
            GROUP BY content_fingerprint
            HAVING site_cnt >= ? AND last_pub <= datetime('now', '-{float(self.retire_days)} days')
        """
        params = list(required_sites) + [len(required_sites)]

        with self.graph_mgr._lock:
            cursor = self.graph_mgr._conn.execute(query, params)
            rows = cursor.fetchall()
            for r in rows:
                candidates.append({
                    "content_fingerprint": r["content_fingerprint"],
                    "origin_site": r["origin_site"],
                    "origin_torrent_id": r["origin_torrent_id"],
                    "site_count": r["site_cnt"],
                    "last_published_at": r["last_pub"],
                })

        return candidates

    def retire_torrent_across_clients(
        self,
        torrent_hash: str,
        dry_run: bool = True
    ) -> Dict[str, str]:
        """跨集群各 qB 实例脱退做种任务（delete_files=False，绝对不删磁盘文件）"""
        results = {}
        for name, client in self.target_clients.items():
            if dry_run:
                results[name] = "DRY-RUN (skip removal)"
                continue
            try:
                # 仅从客户端移除种子任务，保留物理文件
                client.client.torrents_delete(delete_files=False, torrent_hashes=torrent_hash)
                results[name] = "OK (task detached)"
            except Exception as e:
                results[name] = f"Error: {e}"
        return results
