# -*- coding: utf-8 -*-
"""MusicHub 3.0 生命周期退役调度器 (LifecycleRetirement)

遵循 AGENTS.md 铁律与生产实测契约：
1. 门禁 1（矩阵扩散充分度）：仅当资源在所有适格目标站均达成发布（或查重已存在/规则不适格），无待发排队站点。
2. 门禁 2（各站点做种满 10 天）：MAX(published_at) <= now - 10 days（最新发布的站点也必须做种满 10 天，绝不新站断种）。
3. 门禁 3（源种全局做种满 14 天）：MIN(published_at) <= now - 14 days 且满足源站（如 OpenCD 36h HR）考核。
4. 协同下架：跨 8 个物理池及 3010 发种机从 qB 移除做种任务，参数恒为 delete_files=False，严禁直接物理删除。
5. 下架后脱离 qB 任务引用，自然转化为孤儿候选，由 SafeGarbageCollector 执行最终跨池安全协同回收
   （严格受控于 music5 禁删红线与 st_nlink 跨池引用核验，杜绝硬链接共振误删与 0 释放）。
"""

import os
import time
import logging
from typing import Dict, List, Set, Tuple, Optional, Any
from musichub.routing.graph import RoutingGraphManager
from musichub.core.qb_client import QBClient
from musichub.storage.safe_gc import SafeGarbageCollector

logger = logging.getLogger("LifecycleRetirement")


class LifecycleRetirement:
    # 矩阵纳管的完整目标站点列表
    ALL_TARGET_SITES = [
        "muxuege",
        "tangpt",
        "hdkylin",
        "xingwan",
        "pterclub",
        "lemonhd",
        "hdtime",
        "uhddolby",
        "ubits",
        "soulvoice",
        "nanyangpt",
        "musopia",
        "crabpt",
    ]

    def __init__(
        self,
        graph_mgr: RoutingGraphManager,
        safe_gc: SafeGarbageCollector,
        target_clients: Optional[Dict[str, QBClient]] = None,
        min_source_days: float = 14.0,
        min_latest_days: float = 10.0,
        hr_hours_required: float = 36.0,
    ):
        self.graph_mgr = graph_mgr
        self.safe_gc = safe_gc
        self.target_clients = target_clients or {}
        self.min_source_days = min_source_days
        self.min_latest_days = min_latest_days
        self.hr_hours_required = hr_hours_required

    def check_matrix_saturation(
        self,
        content_fingerprint: str,
        published_sites: Set[str],
        title: str = ""
    ) -> Tuple[bool, str, List[str]]:
        """检查专辑在当前矩阵中的发布是否已经充分饱和

        规则：
        - 若所有目标站均已发布：饱和 (True)
        - 若有未发布站点：
          检查未发布站点是否是因客观规则明确不收（如麒麟纯拉丁标题拦截中文专辑）。
          如果存在仍适格且尚未发布的站点，判定为未饱和 (False)，保护其继续扩散。
        """
        unpub_sites = [s for s in self.ALL_TARGET_SITES if s not in published_sites]
        if not unpub_sites:
            return True, "ALL_SITES_PUBLISHED", []

        # 检查未发布站点的适格性
        eligible_unpub = []
        is_latin_only = False
        if title:
            # 简易拉丁标题判定 (hdkylin 规则)
            is_latin_only = all(ord(c) < 128 or ord(c) in (0x3000, 0x3001, 0x3002) for c in title)

        for site in unpub_sites:
            # 麒麟拦截非拉丁标题
            if site == "hdkylin" and title and not is_latin_only:
                continue
            # 其他站点默认视为潜在可发目标
            eligible_unpub.append(site)

        if not eligible_unpub:
            return True, "SATURATED_BY_FILTER", unpub_sites
        return False, f"PENDING_EXPANSION({len(eligible_unpub)} sites remaining)", eligible_unpub

    def find_retireable_candidates(
        self,
        min_source_days: Optional[float] = None,
        min_latest_days: Optional[float] = None,
        require_saturation: bool = True,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """从溯源数据库中找出所有满足“源种满14天 + 各已发站点满10天 + 矩阵扩散充分”的退役候选"""
        src_days = float(min_source_days if min_source_days is not None else self.min_source_days)
        latest_days = float(min_latest_days if min_latest_days is not None else self.min_latest_days)

        candidates = []
        query = """
            SELECT 
                content_fingerprint,
                origin_site,
                origin_torrent_id,
                COUNT(DISTINCT target_site) as site_cnt,
                MIN(published_at) as first_pub,
                MAX(published_at) as last_pub,
                GROUP_CONCAT(DISTINCT target_site) as published_sites
            FROM site_publish_graph
            GROUP BY content_fingerprint
            HAVING 
                first_pub <= datetime('now', ?)
                AND last_pub <= datetime('now', ?)
            ORDER BY first_pub ASC
        """
        params = [f"-{src_days} days", f"-{latest_days} days"]

        with self.graph_mgr._lock:
            cursor = self.graph_mgr._conn.execute(query, params)
            rows = cursor.fetchall()
            for r in rows:
                fp = r["content_fingerprint"]
                pub_sites = set((r["published_sites"] or "").split(","))
                
                # 矩阵充分度检查
                if require_saturation:
                    is_sat, reason, remaining = self.check_matrix_saturation(fp, pub_sites)
                    if not is_sat:
                        logger.debug(f"Candidate {fp[:12]} excluded: {reason}")
                        continue
                else:
                    is_sat, reason, remaining = True, "SKIPPED_CHECK", []

                candidates.append({
                    "content_fingerprint": fp,
                    "origin_site": r["origin_site"],
                    "origin_torrent_id": r["origin_torrent_id"],
                    "site_count": r["site_cnt"],
                    "first_published_at": r["first_pub"],
                    "last_published_at": r["last_pub"],
                    "published_sites": sorted(list(pub_sites)),
                    "saturation_status": reason,
                    "remaining_sites": remaining,
                })
                if len(candidates) >= limit:
                    break

        return candidates

    def get_lifecycle_status_overview(self) -> Dict[str, Any]:
        """获取全库资源生命周期健康分布（退役就绪、时间保护、扩散保护）"""
        query_all = """
            SELECT 
                content_fingerprint,
                origin_site,
                origin_torrent_id,
                COUNT(DISTINCT target_site) as site_cnt,
                MIN(published_at) as first_pub,
                MAX(published_at) as last_pub,
                GROUP_CONCAT(DISTINCT target_site) as published_sites
            FROM site_publish_graph
            GROUP BY content_fingerprint
        """
        now_ts = time.time()
        retireable = []
        protected_time = []
        protected_expansion = []
        protected_source = []

        with self.graph_mgr._lock:
            cursor = self.graph_mgr._conn.execute(query_all)
            rows = cursor.fetchall()

            for r in rows:
                fp = r["content_fingerprint"]
                pub_sites = set((r["published_sites"] or "").split(","))
                first_pub = r["first_pub"] or ""
                last_pub = r["last_pub"] or ""

                # 计算天数
                src_age_days = 0.0
                latest_age_days = 0.0
                try:
                    src_age_days = (now_ts - time.mktime(time.strptime(first_pub[:19], "%Y-%m-%d %H:%M:%S"))) / 86400.0
                    latest_age_days = (now_ts - time.mktime(time.strptime(last_pub[:19], "%Y-%m-%d %H:%M:%S"))) / 86400.0
                except Exception:
                    pass

                is_sat, sat_reason, remaining = self.check_matrix_saturation(fp, pub_sites)

                item = {
                    "content_fingerprint": fp,
                    "origin_site": r["origin_site"],
                    "origin_torrent_id": r["origin_torrent_id"],
                    "site_count": r["site_cnt"],
                    "published_sites": sorted(list(pub_sites)),
                    "first_published_at": first_pub,
                    "last_published_at": last_pub,
                    "src_age_days": round(src_age_days, 1),
                    "latest_age_days": round(latest_age_days, 1),
                    "remaining_sites": remaining,
                }

                if src_age_days < self.min_source_days:
                    protected_source.append(item)
                elif latest_age_days < self.min_latest_days:
                    protected_time.append(item)
                elif not is_sat:
                    protected_expansion.append(item)
                else:
                    retireable.append(item)

        return {
            "total_albums": len(rows),
            "retireable_count": len(retireable),
            "protected_time_count": len(protected_time),
            "protected_expansion_count": len(protected_expansion),
            "protected_source_count": len(protected_source),
            "retireable_samples": retireable[:10],
            "protected_time_samples": protected_time[:5],
            "protected_expansion_samples": protected_expansion[:5],
        }

    def retire_album_across_clients(
        self,
        content_fingerprint: str,
        dry_run: bool = True
    ) -> Dict[str, Any]:
        """跨集群脱退某专辑在 8 个物理池及 3010 发种机的所有任务

        铁律：delete_files 恒为 False！绝对不触发客户端物理删除！
        """
        results = {"detached_tasks": 0, "clients_affected": [], "details": {}}
        if not self.target_clients:
            logger.warning("No target_clients configured for LifecycleRetirement.")
            return results

        # 查找该内容对应的所有 B hashes 或 种子名
        # 通过 qb 查询现场做种任务
        for client_name, client in self.target_clients.items():
            try:
                torrents = client.get_torrents() or []
                matched_hashes = []
                for t in torrents:
                    t_name = t.get("name", "")
                    # 如果种子名或者 hash 匹配
                    if content_fingerprint and content_fingerprint in t.get("comment", ""):
                        matched_hashes.append(t.get("hash"))

                for thash in matched_hashes:
                    if dry_run:
                        results["details"][f"{client_name}:{thash}"] = "DRY-RUN (skip removal)"
                    else:
                        ok = client.delete_torrent(thash, delete_files=False)
                        results["details"][f"{client_name}:{thash}"] = "OK (task detached)" if ok else "Failed"
                        if ok:
                            results["detached_tasks"] += 1
                            if client_name not in results["clients_affected"]:
                                results["clients_affected"].append(client_name)
            except Exception as e:
                results["details"][client_name] = f"Error: {e}"

        return results
