# -*- coding: utf-8 -*-
"""MusicHub 3.0 全站跨期平滑发布调度器 (PublishScheduler)

核心职责：
1. 实现同一个种子在 1~2 天（24~48 小时）内平滑分发到全站。
2. 避免瞬间向所有站点集中轰炸造成 Tracker 触发限流或上行拥塞。
3. 自动将 7 大站点打散为多个阶段批次：
   - Batch 1 (Day 1 上午 0h): 种子出炉首发站 (CrabPT, HDKylin)
   - Batch 2 (Day 1 下午 6h~12h): 第二梯队扩展 (XingWan, MuXueGe)
   - Batch 3 (Day 2 24h~36h): 第三梯队补充 (TangPT, PTerClub, Musopia)
4. 持久化存储在 SQLite 中，记录各站的 scheduled_at、status (SCHEDULED/PUBLISHED/FAILED/SKIPPED)。
"""

import os
import time
import json
import sqlite3
import logging
import threading
from typing import Dict, List, Optional, Tuple, Any

from musichub.core.models import CanonicalMusicItem
from musichub.routing.graph import RoutingGraphManager

logger = logging.getLogger("PublishScheduler")

# 站点分发计划表（相对发布时间的偏移秒数）
DEFAULT_SCHEDULE_SLOTS = {
    "CrabPT": 0,           # 立即
    "HDKylin": 1800,       # 30 分钟后
    "XingWan": 21600,      # 6 小时后
    "MuXueGe": 43200,      # 12 小时后
    "TangPT": 86400,       # 24 小时后 (第二天)
    "PTerClub": 108000,    # 30 小时后 (第二天下午)
    "Musopia": 129600,     # 36 小时后 (第二天晚间)
}


class PublishScheduler:
    def __init__(self, db_path: str, graph_manager: RoutingGraphManager):
        self.db_path = db_path
        self.graph_mgr = graph_manager
        self._lock = threading.Lock()
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self._lock:
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS multi_site_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_fingerprint TEXT NOT NULL,
                album_name TEXT NOT NULL,
                item_json TEXT NOT NULL,
                target_site TEXT NOT NULL,
                scheduled_at REAL NOT NULL,
                status TEXT DEFAULT 'SCHEDULED',
                attempts INTEGER DEFAULT 0,
                last_error TEXT,
                published_at REAL,
                UNIQUE(content_fingerprint, target_site)
            );
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sched_status_time 
            ON multi_site_schedules(status, scheduled_at);
            """)
            self._conn.commit()

    def schedule_album_across_all_sites(
        self,
        item: CanonicalMusicItem,
        slot_offsets: Optional[Dict[str, int]] = None,
        base_time: Optional[float] = None
    ) -> List[str]:
        """为一张新专辑排期全站 1~2 天发布计划"""
        if slot_offsets is None:
            slot_offsets = DEFAULT_SCHEDULE_SLOTS

        base_t = base_time if base_time is not None else time.time()
        content_fp = item.calculate_content_fingerprint()

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

        scheduled_sites = []
        with self._lock:
            for site, offset in slot_offsets.items():
                # 检查防环和已发布
                if self.graph_mgr.is_loop_or_already_published(item, site):
                    logger.info(f"[Schedule] Album {item.album} already published or origin loop for {site}, skipping schedule")
                    continue

                sched_t = base_t + offset
                try:
                    self._conn.execute("""
                    INSERT OR IGNORE INTO multi_site_schedules 
                    (content_fingerprint, album_name, item_json, target_site, scheduled_at, status)
                    VALUES (?, ?, ?, ?, ?, 'SCHEDULED');
                    """, (content_fp, item.album, item_json, site, sched_t))
                    scheduled_sites.append(site)
                except Exception as e:
                    logger.warning(f"Failed to schedule for {site}: {e}")

            self._conn.commit()

        logger.info(f"Scheduled album '{item.album}' across {len(scheduled_sites)} sites over 1-2 days")
        return scheduled_sites

    def get_due_schedules(self, max_tasks: int = 5) -> List[Dict[str, Any]]:
        """获取当前已到期的发布计划"""
        now = time.time()
        with self._lock:
            cur = self._conn.execute("""
            SELECT id, content_fingerprint, album_name, item_json, target_site, scheduled_at, attempts
            FROM multi_site_schedules
            WHERE status = 'SCHEDULED' AND scheduled_at <= ?
            ORDER BY scheduled_at ASC
            LIMIT ?;
            """, (now, max_tasks))
            return [dict(row) for row in cur.fetchall()]

    def mark_published(self, schedule_id: int, torrent_id: str = ""):
        with self._lock:
            self._conn.execute("""
            UPDATE multi_site_schedules
            SET status = 'PUBLISHED', published_at = ?, last_error = NULL
            WHERE id = ?;
            """, (time.time(), schedule_id))
            self._conn.commit()

    def mark_failed(self, schedule_id: int, error_msg: str, retry_delay: int = 300):
        with self._lock:
            cur = self._conn.execute("SELECT attempts FROM multi_site_schedules WHERE id = ?", (schedule_id,))
            row = cur.fetchone()
            attempts = (row["attempts"] + 1) if row else 1
            status = 'FAILED' if attempts >= 3 else 'SCHEDULED'
            next_t = time.time() + retry_delay
            self._conn.execute("""
            UPDATE multi_site_schedules
            SET attempts = ?, last_error = ?, status = ?, scheduled_at = ?
            WHERE id = ?;
            """, (attempts, error_msg, status, next_t, schedule_id))
            self._conn.commit()

    def get_schedule_summary(self) -> Dict[str, Any]:
        with self._lock:
            cur = self._conn.execute("""
            SELECT target_site, status, COUNT(*) as cnt 
            FROM multi_site_schedules 
            GROUP BY target_site, status;
            """)
            summary = {}
            for row in cur.fetchall():
                st = row["target_site"]
                if st not in summary:
                    summary[st] = {}
                summary[st][row["status"]] = row["cnt"]
            return summary

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
