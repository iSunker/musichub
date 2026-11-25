# -*- coding: utf-8 -*-
"""站点 tid 回填与核对工具 (backfill_site_tids.py)

背景
----
`site_publish_graph.origin_torrent_id` 历史上混存三种形态：数字 tid、40 位
infohash、专辑名。该字段定义保持不变，另按站点各开一列 `<site>_tid` 专记数字 tid
（列清单见 `musichub/routing/graph.py` 的 `SITE_TID_COLUMNS`）。

本工具做两件事：
1. 默认只读体检：统计各站点 tid 列的空/满情况，不写库。
2. `--apply`：建列 + 回填（只补空值，幂等）。回填来源
   `RoutingGraphManager._backfill_site_tids_locked`：本行 target tid → 本行 origin tid
   → 同内容指纹兄弟行 → `publish_lineage` 台账兜底。

用法
----
    python musichub/tools/backfill_site_tids.py                 # 只读体检
    python musichub/tools/backfill_site_tids.py --apply         # 建列并回填
    python musichub/tools/backfill_site_tids.py --db <path>     # 指定库
"""

import os
import sys
import sqlite3
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from musichub.core.config import MusicHubConfig
from musichub.routing.graph import RoutingGraphManager, SITE_TID_COLUMNS


def report(db_path: str) -> None:
    """只读体检：不建列、不写库"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        total = conn.execute("SELECT COUNT(*) FROM site_publish_graph").fetchone()[0]
        cols = {r[1] for r in conn.execute("PRAGMA table_info(site_publish_graph)")}
        print(f"site_publish_graph 行数: {total}")
        print(f"{'站点':<10}{'列名':<16}{'已记':>6}{'空':>6}  状态")
        for site, col in SITE_TID_COLUMNS.items():
            if col not in cols:
                print(f"{site:<10}{col:<16}{'-':>6}{'-':>6}  列未创建")
                continue
            filled = conn.execute(
                f"SELECT COUNT(*) FROM site_publish_graph WHERE {col} IS NOT NULL AND {col} != ''"
            ).fetchone()[0]
            print(f"{site:<10}{col:<16}{filled:>6}{total - filled:>6}  OK")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="站点 tid 列回填")
    parser.add_argument("--db", default=MusicHubConfig().db_path, help="musichub.db 路径")
    parser.add_argument("--apply", action="store_true", help="建列并回填（默认只读体检）")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"数据库不存在: {args.db}")
        return 1

    print("=== 回填前 ===")
    report(args.db)

    if not args.apply:
        print("\n[dry-run] 未写库；确认无误后加 --apply")
        return 0

    # 构造 manager 即完成 ALTER + 首次回填（_init_db → _migrate_site_tid_columns）
    graph_mgr = RoutingGraphManager(db_path=args.db)
    try:
        stats = graph_mgr.backfill_site_tids()
    finally:
        graph_mgr.close()

    print(f"\n=== 本次新增/补齐: {sum(stats.values())} 个值 ===")
    for site, n in stats.items():
        if n:
            print(f"  {site}: +{n}")
    print("\n=== 回填后 ===")
    report(args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
