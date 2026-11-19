# -*- coding: utf-8 -*-
"""一次性数据迁移与初始化脚本：将老系统 data.db 既有发布记录迁移至 musichub.db

治理规则：
1. 从 data.db 中读取所有已发布的任务 (site_publish.status = 'EXISTS')
2. 规范化站点名称映射（如 pter -> pterclub）
3. 计算规范化内容指纹与流指纹
4. 批量灌入 site_publish_graph，实现发布溯源图与历史数据的完全对齐
"""

import os
import sys
import sqlite3
import logging
from typing import Dict, Any

# 加入父级路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from musichub.core.models import CanonicalMusicItem
from musichub.metadata.audio_inspector import AudioInspector

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MigrateHistory")

SITE_NORM_MAP = {
    "musopia": "musopia",
    "crabpt": "crabpt",
    "pter": "pterclub",
    "pterclub": "pterclub",
    "xingwan": "xingwan",
    "hdkylin": "hdkylin",
    "tangpt": "tangpt",
    "muxuege": "muxuege",
    "opencd": "opencd",
}


def migrate_history(source_db_path: str, target_db_path: str) -> int:
    if not os.path.exists(source_db_path):
        raise FileNotFoundError(f"Source DB not found: {source_db_path}")

    logger.info(f"Connecting to source DB: {source_db_path}")
    src_conn = sqlite3.connect(source_db_path)
    src_conn.row_factory = sqlite3.Row

    logger.info(f"Connecting to target DB: {target_db_path}")
    os.makedirs(os.path.dirname(os.path.abspath(target_db_path)), exist_ok=True)
    dst_conn = sqlite3.connect(target_db_path)
    dst_conn.row_factory = sqlite3.Row

    # 初始化目标表结构
    dst_conn.execute("""
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
    dst_conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_fingerprint ON site_publish_graph(content_fingerprint);")
    dst_conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_stream ON site_publish_graph(stream_fingerprint);")

    # 查询所有已发布的记录
    query = """
    SELECT 
        sp.site,
        sp.torrent_id AS target_torrent_id,
        sp.info_hash,
        sp.b_info_hash,
        t.artist,
        t.year,
        t.formatted_title,
        t.torrent_name,
        t.opencd_tid
    FROM site_publish sp
    JOIN tasks t ON sp.info_hash = t.info_hash
    WHERE sp.status = 'EXISTS'
    """
    rows = src_conn.execute(query).fetchall()
    logger.info(f"Found {len(rows)} published records in source DB.")

    migrated_count = 0
    skipped_count = 0

    for r in rows:
        raw_site = (r["site"] or "").lower()
        target_site = SITE_NORM_MAP.get(raw_site, raw_site)

        # 解析与提取元数据
        name_for_meta = r["formatted_title"] or r["torrent_name"] or ""
        parsed = AudioInspector.parse_name_metadata(name_for_meta)

        artist = r["artist"] or parsed.get("artist") or "Various Artists"
        # 专辑名提取
        album = parsed.get("album") or name_for_meta
        # 年份清洗（去除“年”等中文字符）
        raw_year = str(r["year"] or parsed.get("year") or "2024").replace("年", "").strip()
        year = raw_year if raw_year else "2024"
        medium = parsed.get("medium") or "CD"
        fmt = parsed.get("format") or "FLAC"

        # 组装规范项以计算内容指纹
        item = CanonicalMusicItem(
            title=name_for_meta,
            artist=artist,
            album=album,
            year=year,
            format=fmt,
            medium=medium,
            origin_site="OpenCD",
            origin_torrent_id=r["opencd_tid"] or "",
            origin_info_hash=r["info_hash"] or "",
        )
        content_fp = item.calculate_content_fingerprint()

        try:
            dst_conn.execute("""
            INSERT OR REPLACE INTO site_publish_graph
            (content_fingerprint, stream_fingerprint, origin_site, origin_torrent_id, target_site, target_torrent_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (
                content_fp,
                r["b_info_hash"] or "",  # 使用 b_info_hash 作为物理特征关联
                "opencd",
                r["opencd_tid"] or "",
                target_site,
                r["target_torrent_id"] or ""
            ))
            migrated_count += 1
        except Exception as e:
            logger.warning(f"Failed to insert record for {name_for_meta}: {str(e)}")
            skipped_count += 1

    dst_conn.commit()
    src_conn.close()
    dst_conn.close()

    logger.info(f"Migration completed: {migrated_count} records migrated, {skipped_count} skipped.")
    return migrated_count


if __name__ == "__main__":
    src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "data_nas.db")
    dst_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "musichub.db")
    migrate_history(src_path, dst_path)
