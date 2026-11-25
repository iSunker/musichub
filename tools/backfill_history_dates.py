# -*- coding: utf-8 -*-
"""MusicHub 历史发布时间与做种时长回填工具 (BackfillHistoryDates)

解决 2026-11-19 数据库重构迁移导致的 published_at 被统一截断为 2026-11-19 11:54:31 的问题。
从历史 data.db 中提取真实 site_publish.synced_at 与 tasks.opencd_added_at/updated_at，
精准回填到 site_publish_graph 表中，激活老种合法的 14+10 天退役判定资格。
"""

import os
import sys
import sqlite3
import argparse
import logging
from typing import Dict, Tuple, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("BackfillHistoryDates")


def backfill_history_dates(
    db_path: str = "d:/Projects/PT-Project/musichub/data/musichub.db",
    old_db_path: str = "d:/Projects/PT-Project/musichub/data/data.db",
    dry_run: bool = True
) -> Dict[str, int]:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"musichub.db not found at: {db_path}")
    if not os.path.exists(old_db_path):
        raise FileNotFoundError(f"data.db not found at: {old_db_path}")

    conn_mh = sqlite3.connect(db_path)
    conn_mh.row_factory = sqlite3.Row
    conn_old = sqlite3.connect(old_db_path)
    conn_old.row_factory = sqlite3.Row

    logger.info(f"Loading historical records from {old_db_path}...")
    
    # 1. 提取历史已发布记录 (site, target_tid) -> synced_at
    cur_old = conn_old.cursor()
    cur_old.execute(
        "SELECT site, torrent_id, synced_at, updated_at "
        "FROM site_publish WHERE status='EXISTS' AND torrent_id != ''"
    )
    old_target_pubs = {}
    for r in cur_old.fetchall():
        site = (r["site"] or "").strip().lower()
        tid = str(r["torrent_id"]).strip()
        pub_time = r["synced_at"] or r["updated_at"]
        if site and tid and pub_time:
            old_target_pubs[(site, tid)] = pub_time

    # 2. 提取历史源任务 opencd_tid -> opencd_added_at / updated_at
    cur_old.execute(
        "SELECT opencd_tid, opencd_added_at, updated_at "
        "FROM tasks WHERE opencd_tid IS NOT NULL AND opencd_tid != ''"
    )
    old_origin_tasks = {}
    for r in cur_old.fetchall():
        otid = str(r["opencd_tid"]).strip()
        otime = r["opencd_added_at"] or r["updated_at"]
        if otid and otime:
            old_origin_tasks[otid] = otime

    logger.info(f"Loaded {len(old_target_pubs)} target publishes and {len(old_origin_tasks)} origin tasks from data.db")

    # 3. 扫描 musichub.db 中需要修正的记录
    cur_mh = conn_mh.cursor()
    cur_mh.execute(
        "SELECT id, origin_site, origin_torrent_id, target_site, target_torrent_id, published_at "
        "FROM site_publish_graph "
        "WHERE published_at LIKE '2026-11-19%'"
    )
    candidates = cur_mh.fetchall()
    logger.info(f"Found {len(candidates)} records in site_publish_graph with 2026-11-19 timestamp.")

    updated_by_target = 0
    updated_by_origin = 0
    skipped_count = 0
    updates = []

    for row in candidates:
        rid = row["id"]
        tgt_site = (row["target_site"] or "").strip().lower()
        tgt_tid = str(row["target_torrent_id"] or "").strip()
        orig_tid = str(row["origin_torrent_id"] or "").strip()

        matched_time = None
        # 优先通过 (target_site, target_tid) 匹配精准发布时间
        if (tgt_site, tgt_tid) in old_target_pubs:
            matched_time = old_target_pubs[(tgt_site, tgt_tid)]
            updated_by_target += 1
        # 兜底通过 origin_torrent_id 匹配源站入库时间
        elif orig_tid in old_origin_tasks:
            matched_time = old_origin_tasks[orig_tid]
            updated_by_origin += 1
        else:
            skipped_count += 1
            continue

        if matched_time:
            updates.append((matched_time, rid))

    stats = {
        "total_candidates": len(candidates),
        "matched_by_target": updated_by_target,
        "matched_by_origin": updated_by_origin,
        "total_updates": len(updates),
        "skipped": skipped_count,
        "dry_run": dry_run,
    }

    logger.info(
        f"Backfill evaluation: {len(updates)}/{len(candidates)} to update "
        f"(target_match={updated_by_target}, origin_match={updated_by_origin}, skipped={skipped_count})"
    )

    if not dry_run and updates:
        logger.info(f"Applying {len(updates)} timestamp updates to site_publish_graph...")
        cur_mh.executemany(
            "UPDATE site_publish_graph SET published_at = ? WHERE id = ?",
            updates
        )
        conn_mh.commit()
        logger.info("Successfully committed updates to database.")
    elif dry_run:
        logger.info("[DRY-RUN] No database modifications written. Pass --apply to execute.")

    conn_mh.close()
    conn_old.close()
    return stats


def main():
    parser = argparse.ArgumentParser(description="Backfill historical publish dates from data.db to musichub.db")
    parser.add_argument("--db", default=r"d:\Projects\PT-Project\musichub\data\musichub.db", help="Path to musichub.db")
    parser.add_argument("--old-db", default=r"d:\Projects\PT-Project\musichub\data\data.db", help="Path to data.db")
    parser.add_argument("--apply", action="store_true", help="Apply updates to database")
    args = parser.parse_args()

    stats = backfill_history_dates(
        db_path=args.db,
        old_db_path=args.old_db,
        dry_run=not args.apply
    )
    print("\nSummary:", stats)


if __name__ == "__main__":
    main()
