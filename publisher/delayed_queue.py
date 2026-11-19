# -*- coding: utf-8 -*-
"""MusicHub 3.0 延迟回加做种队列 (DelayedReaddQueue)

核心职责：
1. 种子发布成功后，暂不立即添加做种，而是进入延迟回加队列。
2. 设定 delay_seconds（默认 450 秒 = 7.5 分钟）。
3. 支持持久化到 SQLite 数据库或本地 JSON 文件，服务重启不丢失。
4. 提供 process_pending_readds()：轮询到期的待做种任务，调用 3010 注入，并自动设置连接数限制为 3。
"""

import os
import time
import json
import sqlite3
import logging
import threading
from typing import Dict, List, Optional, Tuple, Any

from musichub.core.models import CanonicalMusicItem
from musichub.publisher.engine import PublisherEngine
from musichub.core.qb_client import QBClient

logger = logging.getLogger("DelayedReaddQueue")


class DelayedReaddQueue:
    def __init__(self, db_path: str, engine: PublisherEngine):
        self.db_path = db_path
        self.engine = engine
        self._lock = threading.Lock()
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self._lock:
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS delayed_readd_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_name TEXT NOT NULL,
                torrent_id TEXT NOT NULL,
                album_name TEXT NOT NULL,
                item_json TEXT NOT NULL,
                torrent_bytes BLOB NOT NULL,
                added_at REAL NOT NULL,
                readd_after REAL NOT NULL,
                status TEXT DEFAULT 'PENDING',
                attempts INTEGER DEFAULT 0,
                last_error TEXT,
                processed_at REAL
            );
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_readd_status_time 
            ON delayed_readd_tasks(status, readd_after);
            """)
            self._conn.commit()

    def add_task(
        self,
        site_name: str,
        torrent_id: str,
        item: CanonicalMusicItem,
        torrent_bytes: bytes,
        delay_seconds: int = 450
    ) -> int:
        """把发布的种子加入延迟回加队列（默认 450 秒后回加）"""
        now = time.time()
        readd_time = now + delay_seconds

        # 将 item 序列化为简易字典
        item_dict = {
            "title": item.title,
            "album": item.album,
            "artist": item.artist,
            "year": item.year,
            "format": item.format,
            "medium": item.medium,
            "standard": item.standard,
            "origin_site": item.origin_site,
            "origin_torrent_id": item.origin_torrent_id,
            "local_path": item.local_path,
        }
        item_json = json.dumps(item_dict, ensure_ascii=False)

        with self._lock:
            cur = self._conn.execute("""
            INSERT INTO delayed_readd_tasks 
            (site_name, torrent_id, album_name, item_json, torrent_bytes, added_at, readd_after, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING');
            """, (site_name, str(torrent_id), item.album, item_json, torrent_bytes, now, readd_time))
            self._conn.commit()
            task_id = cur.lastrowid
            logger.info(f"Enqueued delayed readd task #{task_id} for {site_name}:{torrent_id} ({item.album}), will readd at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(readd_time))}")
            return task_id

    def process_pending_readds(self, max_tasks: int = 5) -> List[Dict[str, Any]]:
        """检查并执行所有到期的回加任务"""
        now = time.time()
        ready_tasks = []

        with self._lock:
            cursor = self._conn.execute("""
            SELECT id, site_name, torrent_id, album_name, item_json, torrent_bytes, added_at, readd_after, attempts
            FROM delayed_readd_tasks
            WHERE status = 'PENDING' AND readd_after <= ?
            ORDER BY readd_after ASC
            LIMIT ?;
            """, (now, max_tasks))
            ready_tasks = [dict(row) for row in cursor.fetchall()]

        results = []
        for task in ready_tasks:
            t_id = task["id"]
            site = task["site_name"]
            alb = task["album_name"]
            torrent_bytes = task["torrent_bytes"]

            item_dict = json.loads(task["item_json"])
            item = CanonicalMusicItem(
                title=item_dict.get("title", alb),
                album=item_dict.get("album", alb),
                artist=item_dict.get("artist", ""),
                year=item_dict.get("year", ""),
                format=item_dict.get("format", "FLAC"),
                medium=item_dict.get("medium", "CD"),
                standard=item_dict.get("standard", "Lossless"),
                origin_site=item_dict.get("origin_site", "opencd"),
                origin_torrent_id=item_dict.get("origin_torrent_id", ""),
                local_path=item_dict.get("local_path", ""),
            )

            logger.info(f"Executing delayed readd for #{t_id} [{site}:{task['torrent_id']}] {alb} (delayed {int(now - task['added_at'])}s)...")

            # 调用 engine 执行 3010 注入与保活探针
            ok, msg = self.engine.readd_publish_with_keepalive_probe(torrent_bytes, item, timeout_seconds=120)

            with self._lock:
                if ok:
                    self._conn.execute("""
                    UPDATE delayed_readd_tasks
                    SET status = 'SUCCESS', processed_at = ?, last_error = NULL
                    WHERE id = ?;
                    """, (time.time(), t_id))
                    logger.info(f"Delayed readd #{t_id} succeeded: {msg}")
                else:
                    attempts = task["attempts"] + 1
                    status = 'FAILED' if attempts >= 3 else 'PENDING'
                    retry_time = time.time() + 60  # 1分钟后重试
                    self._conn.execute("""
                    UPDATE delayed_readd_tasks
                    SET attempts = ?, last_error = ?, status = ?, readd_after = ?
                    WHERE id = ?;
                    """, (attempts, msg, status, retry_time, t_id))
                    logger.warning(f"Delayed readd #{t_id} failed (attempt {attempts}): {msg}")

                self._conn.commit()

            results.append({"id": t_id, "site": site, "album": alb, "success": ok, "msg": msg})

        return results

    def get_stats(self) -> Dict[str, int]:
        with self._lock:
            cur = self._conn.execute("SELECT status, COUNT(*) as cnt FROM delayed_readd_tasks GROUP BY status")
            stats = {row["status"]: row["cnt"] for row in cur.fetchall()}
            return stats

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
