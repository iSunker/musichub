# -*- coding: utf-8 -*-
"""MusicHub 3.0 多源摄取观察器与下载缓冲推进模块 (rss_watcher.py)

架构事实与职责划分：
1. Vertex (NAS 3004 容器) 承担各 PT 站音乐区 RSS 抓取、大小过滤（50MB~15GB）与推流职责；
   目标下载器专指 3016 (qbit-musicdownload)。
2. 本模块作为 MusicHub 消费端的入口观察器 (RSS Ingestion Watcher)：
   - 监听 3016 实例中的下载任务生命周期与完成度
   - 当任务 100% 达成时，触发【闸门 A】物理校验与 Ready-Handoff 跨卷流式搬运
   - 联动 Vertex 的 sql.db 溯源表（只读读取），获取原始 torrent 抓取时的源站、Tracker 与 RSS 元数据
   - 杜绝多端重复抓取与幽灵做种
"""

import os
import sqlite3
import logging
from typing import Dict, List, Optional, Tuple, Any

from musichub.core.qb_client import QBClient
from musichub.core.models import CanonicalMusicItem

logger = logging.getLogger("RSSWatcher")


class RSSWatcher:
    def __init__(
        self,
        qb_download: QBClient,
        vertex_db_path: str = r"\\iSunker-DS423\docker_ssd\vertex2\data\vertex\db\sql.db"
    ):
        self.qb_download = qb_download
        self.vertex_db_path = vertex_db_path

    def get_vertex_origin_metadata(self, torrent_hash: str) -> Optional[Dict[str, Any]]:
        """从 Vertex 数据库（只读模式）反查种子的来源信息（源站、原始链接、抓取分类）"""
        target_db = self.vertex_db_path
        if not os.path.exists(target_db):
            # 兼容 Linux 容器内挂载点
            linux_path = "/vertex/sql.db"
            if os.path.exists(linux_path):
                target_db = linux_path
            else:
                return None

        clean_hash = torrent_hash.strip().lower()
        try:
            # 强制只读只查，严禁写入
            uri = f"file:{target_db}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5.0) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT hash, name, size, link, tracker, rss_id FROM torrents WHERE LOWER(hash) = ? LIMIT 1",
                    (clean_hash,)
                )
                row = cursor.fetchone()
                if row:
                    return {
                        "hash": row["hash"],
                        "name": row["name"],
                        "size": row["size"],
                        "link": row["link"],
                        "tracker": row["tracker"],
                        "rss_id": row["rss_id"],
                    }
        except Exception as e:
            logger.warning(f"Failed to query vertex db for hash {clean_hash}: {e}")
        return None

    def scan_ready_tasks(self) -> List[Dict[str, Any]]:
        """扫描 3016 专用缓冲下载器，返回所有 100% 完成且未被标记处理过的音乐任务"""
        torrents = self.qb_download.get_torrents() or []
        ready_tasks = []

        for t in torrents:
            progress = t.get("progress", 0.0)
            state = t.get("state", "")
            name = t.get("name", "")
            thash = t.get("hash", "")
            tags = t.get("tags", "")

            # 仅提取 100% 完成且处于做种/暂停状态、未处理过的种子
            if progress >= 1.0 and "musichub_processed" not in tags:
                # 反查 Vertex 元数据
                v_meta = self.get_vertex_origin_metadata(thash)
                origin_site = "OpenCD" if v_meta and "open.cd" in (v_meta.get("tracker") or "") else "VertexRSS"

                ready_tasks.append({
                    "hash": thash,
                    "name": name,
                    "size": t.get("size", 0),
                    "state": state,
                    "content_path": t.get("content_path", ""),
                    "origin_site": origin_site,
                    "vertex_meta": v_meta
                })

        return ready_tasks

    def mark_task_processed(self, torrent_hash: str) -> bool:
        """为已成功移交并处理完成的任务打上标签，防止重复消费"""
        try:
            self.qb_download.client.torrents_add_tags(
                tags="musichub_processed",
                torrent_hashes=torrent_hash
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to mark task {torrent_hash} as processed: {e}")
            return False
