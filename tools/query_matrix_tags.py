# -*- coding: utf-8 -*-
"""MusicHub 全矩阵详情页与源种标签入库状态查询工具。

用法示例：
  # 查看全矩阵各站点详情页归档数及标签覆盖情况
  python musichub/tools/query_matrix_tags.py --summary

  # 查看指定站点最新抓取到的带标签种子
  python musichub/tools/query_matrix_tags.py --site musopia --limit 5

  # 抽查源种任务表中的标签收录情况
  python musichub/tools/query_matrix_tags.py --source --limit 5
"""

import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from musichub.core.config import MusicHubConfig
from musichub.routing.graph import RoutingGraphManager, normalize_site_key


def show_summary(graph: RoutingGraphManager):
    print("=" * 65)
    print("【全矩阵 13 站点详情页归档与标签收录概况】")
    print("=" * 65)
    
    # 1. site_detail_pages 统计
    rows = graph._conn.execute("""
        SELECT site,
               count(*) as total,
               sum(case when fetch_status='OK' then 1 else 0 end) as ok_cnt,
               sum(case when tags_json != '' and tags_json != '[]' then 1 else 0 end) as tagged_cnt
        FROM site_detail_pages
        GROUP BY site
        ORDER BY total DESC
    """).fetchall()

    print(f"{'站点 (Site)':<15} | {'归档总数':<10} | {'抓取成功 (OK)':<14} | {'已解析标签 (Tagged)':<18}")
    print("-" * 65)
    if not rows:
        print("  (暂无 site_detail_pages 归档记录)")
    for r in rows:
        print(f"{r[0]:<15} | {r[1]:<10} | {r[2] or 0:<14} | {r[3] or 0:<18}")

    print("\n" + "=" * 65)
    print("【源种任务 (source_tasks) 标签收录概况】")
    print("=" * 65)
    st_rows = graph._conn.execute("""
        SELECT source_site,
               count(*) as total,
               sum(case when meta_status='OK' then 1 else 0 end) as ok_cnt,
               sum(case when tags_json != '' and tags_json != '[]' then 1 else 0 end) as tagged_cnt
        FROM source_tasks
        GROUP BY source_site
        ORDER BY total DESC
    """).fetchall()
    print(f"{'源站 (Site)':<15} | {'任务总数':<10} | {'元数据就绪 (OK)':<14} | {'已解析标签 (Tagged)':<18}")
    print("-" * 65)
    if not st_rows:
        print("  (暂无 source_tasks 记录)")
    for r in st_rows:
        print(f"{r[0]:<15} | {r[1]:<10} | {r[2] or 0:<14} | {r[3] or 0:<18}")


def query_site(graph: RoutingGraphManager, site: str, limit: int = 5):
    site = normalize_site_key(site)
    print(f"\n[*] 正在查询站点 [{site}] 最近收录的种子详情与标签 (LIMIT {limit}):\n")
    rows = graph._conn.execute("""
        SELECT site, tid, title, fetch_status, tags_json, fetched_at
        FROM site_detail_pages
        WHERE site=?
        ORDER BY fetched_at DESC
        LIMIT ?
    """, (site, limit)).fetchall()
    
    if not rows:
        print(f"  未找到站点 {site} 的归档记录。")
        return

    for idx, r in enumerate(rows, 1):
        print(f"[{idx}] {r[0]} TID: {r[1]} | 状态: {r[3]}")
        print(f"    标题: {r[2]}")
        tags_raw = r[4] or "[]"
        try:
            tags = json.loads(tags_raw)
            tags_display = " / ".join(f"{t.get('name')}({t.get('id')})" if t.get('id') else t.get('name', '') for t in tags) if tags else "(无)"
        except Exception:
            tags_display = tags_raw
        print(f"    标签: {tags_display}")
        print("-" * 60)


def query_sources(graph: RoutingGraphManager, limit: int = 5):
    print(f"\n[*] 正在查询最近的源种任务 (source_tasks) 详情与标签 (LIMIT {limit}):\n")
    rows = graph._conn.execute("""
        SELECT source_site, source_tid, source_name, meta_status, tags_json, created_at
        FROM source_tasks
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,)).fetchall()

    if not rows:
        print("  未找到源种任务记录。")
        return

    for idx, r in enumerate(rows, 1):
        print(f"[{idx}] {r[0]} TID: {r[1]} | 状态: {r[3]}")
        print(f"    标题: {r[2]}")
        tags_raw = r[4] or "[]"
        try:
            tags = json.loads(tags_raw)
            tags_display = " / ".join(f"{t.get('name')}({t.get('id')})" if t.get('id') else t.get('name', '') for t in tags) if tags else "(无)"
        except Exception:
            tags_display = tags_raw
        print(f"    标签: {tags_display}")
        print("-" * 60)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--summary", action="store_true", help="显示全矩阵详情页归档与源种标签收录统计")
    parser.add_argument("--site", help="查询指定站点的最新详情页与标签记录")
    parser.add_argument("--source", action="store_true", help="查询源种任务表中的标签收录记录")
    parser.add_argument("--limit", type=int, default=5, help="输出记录条数上限（默认 5）")
    args = parser.parse_args()

    cfg = MusicHubConfig.from_env()
    graph = RoutingGraphManager(cfg.db_path)

    if args.summary:
        show_summary(graph)
    elif args.site:
        query_site(graph, args.site, limit=args.limit)
    elif args.source:
        query_sources(graph, limit=args.limit)
    else:
        show_summary(graph)


if __name__ == "__main__":
    main()
