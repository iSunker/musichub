# -*- coding: utf-8 -*-
"""MusicHub 3.0 全局防环与发布溯源图治理模块

严格执行：
1. 双重指纹识别（内容规格指纹 + 音轨流指纹）
2. 溯源追踪：杜绝 Mesh 互转闭环（A -> B -> A 或 A -> B -> C -> A）
3. 全集群 InfoHash 查重拦截：同 InfoHash 全集群单实例
4. 站点硬约束：OpenCD 来源任务唯一归属 3260，严禁发布回源站
"""

import sqlite3
import os
import threading
from typing import List, Optional, Set, Dict, Any
from musichub.core.models import CanonicalMusicItem


class RoutingGraphManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self._lock:
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS site_publish_graph (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_fingerprint TEXT NOT NULL,
                stream_fingerprint TEXT,
                origin_site TEXT NOT NULL,
                origin_torrent_id TEXT,
                target_site TEXT NOT NULL,
                target_torrent_id TEXT,
                published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(content_fingerprint, target_site)
            );
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_graph_fingerprint 
            ON site_publish_graph(content_fingerprint);
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_graph_stream 
            ON site_publish_graph(stream_fingerprint);
            """)
            
            # 全集群 InfoHash 登记表（支持同 hash 单实例排查）
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cluster_infohash_registry (
                info_hash TEXT PRIMARY KEY,
                instance_name TEXT NOT NULL,
                instance_port INTEGER NOT NULL,
                site_name TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            self._conn.commit()

    def is_loop_or_already_published(self, item: CanonicalMusicItem, target_site: str) -> bool:
        """检查发布到目标站点是否构成回环或已发布"""
        tgt = target_site.lower()
        orig = item.origin_site.lower()

        # 规则 1：目标站点与原始抓取站点相同 -> 直接构成回环
        if tgt == orig or (tgt == "pterclub" and orig == "pter") or (tgt == "pter" and orig == "pterclub"):
            return True

        content_fp = item.calculate_content_fingerprint()
        stream_fp = item.calculate_stream_fingerprint()

        with self._lock:
            # 规则 2：内容指纹已在目标站记录
            cursor = self._conn.execute(
                "SELECT 1 FROM site_publish_graph WHERE content_fingerprint = ? AND target_site = ?",
                (content_fp, tgt)
            )
            if cursor.fetchone():
                return True

            # 规则 3：音轨物理流指纹命中目标站记录
            if stream_fp:
                cursor = self._conn.execute(
                    "SELECT 1 FROM site_publish_graph WHERE stream_fingerprint = ? AND target_site = ?",
                    (stream_fp, tgt)
                )
                if cursor.fetchone():
                    return True

        return False

    def check_infohash_exists_in_cluster(self, info_hash: str) -> Optional[Dict[str, Any]]:
        """检查 InfoHash 是否已登记在集群的某一实例中"""
        clean_hash = (info_hash or "").strip().lower()
        if not clean_hash:
            return None

        with self._lock:
            cursor = self._conn.execute(
                "SELECT instance_name, instance_port, site_name, registered_at FROM cluster_infohash_registry WHERE info_hash = ?",
                (clean_hash,)
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
        return None

    def register_cluster_infohash(self, info_hash: str, instance_name: str, instance_port: int, site_name: str = ""):
        """在集群注册表中登记 InfoHash"""
        clean_hash = (info_hash or "").strip().lower()
        if not clean_hash:
            return

        with self._lock:
            self._conn.execute("""
            INSERT OR REPLACE INTO cluster_infohash_registry (info_hash, instance_name, instance_port, site_name)
            VALUES (?, ?, ?, ?);
            """, (clean_hash, instance_name, instance_port, site_name))
            self._conn.commit()

    def record_publish(
        self,
        item: CanonicalMusicItem,
        target_site: str,
        target_torrent_id: str = "",
    ):
        """记录一条成功发布，写入溯源图"""
        content_fp = item.calculate_content_fingerprint()
        stream_fp = item.calculate_stream_fingerprint()
        tgt = target_site.lower()

        with self._lock:
            self._conn.execute("""
            INSERT OR REPLACE INTO site_publish_graph 
            (content_fingerprint, stream_fingerprint, origin_site, origin_torrent_id, target_site, target_torrent_id)
            VALUES (?, ?, ?, ?, ?, ?);
            """, (
                content_fp,
                stream_fp,
                item.origin_site,
                item.origin_torrent_id,
                tgt,
                target_torrent_id,
            ))
            self._conn.commit()

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
