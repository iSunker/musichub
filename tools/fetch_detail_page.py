# -*- coding: utf-8 -*-
"""MusicHub 详情页全矩阵通用抓取、全量标签提取与本地留存工具。

支持全矩阵 13 站点（OpenCD, UHDDolby, Musopia, PTerClub, CrabPT, LemonHD,
HDTime, HDKylin, Xingwan, TangPT, MuXueGe, UBits, SoulVoice）。

功能特性：
1. 抓取并解析种子详情页：提取标题、艺术家、专辑、年份、流派风格、媒介、格式、做种/下载数、以及「全量标签」；
2. 本地落盘留存：脱敏 passkey/token 后保存为本地 HTML 文件（默认存至 data/detail_pages/{site}_{tid}.html）；
3. 数据库同步：可选写入 site_detail_pages 归档表与 source_tasks 源种任务表（含 tags_json 列）；
4. 支持单种、逗号分隔多种列表、或从本地/NAS 数据库历史中批量提取未抓取目标。

示例用法：
  # 抓取 OpenCD 种子详情页与全部标签，并保存 HTML 到本地
  python musichub/tools/fetch_detail_page.py --site opencd --tid 198601 --save-html

  # 抓取 UHDDolby 种子并同步写入详情页数据库与本地文件
  python musichub/tools/fetch_detail_page.py --site uhddolby --tid 257139 --save-html --to-db

  # 抓取乌托邦 (Musopia) 种子详情页
  python musichub/tools/fetch_detail_page.py --site musopia --tid 29642 --save-html

  # 批量抓取 OpenCD 数据库中尚未留存或未解析标签的种子（限 10 条）
  python musichub/tools/fetch_detail_page.py --site opencd --from-db --limit 10 --save-html --to-db
"""

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from musichub.collector.source_detail import (
    SITE_INFO,
    SourceDetailFetcher,
    extract_tags,
    parse_detail_html as parse_source_detail_html,
)
from musichub.core.config import MusicHubConfig
from musichub.onsite.detail_archive import (
    DetailArchive,
    build_adapter,
    parse_detail_html as parse_archive_detail_html,
    sanitize_html,
    sanitize_url,
)
from musichub.routing.graph import RoutingGraphManager, normalize_site_key


DEFAULT_HTML_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "detail_pages")


def fetch_and_process_one(
    site: str,
    tid: str,
    adapter: Any,
    save_html: bool = False,
    html_dir: str = DEFAULT_HTML_DIR,
    graph: Optional[RoutingGraphManager] = None,
    to_db: bool = False,
    delay: float = 0.8,
) -> Dict[str, Any]:
    """抓取单个站点的单个种子详情页，解析标签并按需保存 HTML 和入库。"""
    site = normalize_site_key(site)
    tid = str(tid).strip()
    if not site or not tid.isdigit():
        return {"ok": False, "site": site, "tid": tid, "error": "invalid_site_or_tid"}

    if site == "opencd":
        url = f"{adapter.base_url}/plugin_details.php?id={tid}"
    else:
        url = f"{adapter.base_url}/details.php?id={tid}"

    headers = adapter._prepare_headers()
    try:
        resp = adapter.session.get(url, headers=headers, timeout=20, verify=False)
    except Exception as exc:
        return {"ok": False, "site": site, "tid": tid, "url": url, "error": f"net:{type(exc).__name__}: {exc}"}

    if resp.status_code != 200:
        return {"ok": False, "site": site, "tid": tid, "url": url, "http_status": resp.status_code, "error": f"http_{resp.status_code}"}

    raw_html = resp.text or ""
    if "未登錄" in raw_html or "未登录" in raw_html:
        return {"ok": False, "site": site, "tid": tid, "url": url, "error": "login_required"}

    # 敏感信息脱敏
    clean_html = sanitize_html(raw_html)

    # 1. 保存本地 HTML
    saved_path = ""
    if save_html:
        try:
            os.makedirs(html_dir, exist_ok=True)
            saved_path = os.path.join(html_dir, f"{site}_{tid}.html")
            with open(saved_path, "w", encoding="utf-8") as fh:
                fh.write(clean_html)
            saved_path = os.path.abspath(saved_path)
        except Exception as exc:
            saved_path = f"save_failed: {exc}"

    # 2. 解析多维元数据
    # 结合 source_detail 解析（偏媒体元数据）与 detail_archive 解析（偏同伴与做种状态）
    source_parsed = parse_source_detail_html(clean_html)
    archive_parsed = parse_archive_detail_html(clean_html, base_url=resp.url or url, current_tid=tid)

    # 提取全量标签（字典列表）
    tags = source_parsed.get("tags") or archive_parsed.get("tags") or []
    tags_json = json.dumps(tags, ensure_ascii=False) if tags else ""

    summary = {
        "ok": True,
        "site": site,
        "tid": tid,
        "url": sanitize_url(resp.url or url),
        "title": source_parsed.get("title") or archive_parsed.get("title") or "",
        "artist": source_parsed.get("artist") or "",
        "album": source_parsed.get("album") or "",
        "year": source_parsed.get("year") or "",
        "genre": source_parsed.get("genre") or "",
        "format": source_parsed.get("format") or "",
        "medium": source_parsed.get("medium") or "",
        "transfer_rule": source_parsed.get("transfer_rule") or "",
        "seeders": archive_parsed.get("seeders"),
        "leechers": archive_parsed.get("leechers"),
        "completed": archive_parsed.get("completed"),
        "tags": tags,
        "tags_count": len(tags),
        "tags_json": tags_json,
        "html_saved": saved_path,
        "info_hash": source_parsed.get("info_hash") or "",
    }

    # 3. 数据库同步（需显式 --to-db 且传入 graph）
    db_actions = []
    if to_db and graph:
        try:
            # A. 归档到 site_detail_pages
            import gzip
            import hashlib
            raw_bytes = clean_html.encode("utf-8", errors="replace")
            now = time.time()
            page_record = {
                "site": site,
                "tid": tid,
                "url": summary["url"],
                "http_status": resp.status_code,
                "fetch_status": archive_parsed.get("page_status") or "OK",
                "title": summary["title"],
                "descr_text": archive_parsed.get("descr_text") or source_parsed.get("description"),
                "cover_urls_json": json.dumps(archive_parsed.get("cover_urls") or [], ensure_ascii=False),
                "tags_json": tags_json or "[]",
                "parsed_json": json.dumps({
                    "source_links": archive_parsed.get("source_links") or [],
                    "seeders": archive_parsed.get("seeders"),
                    "leechers": archive_parsed.get("leechers"),
                    "completed": archive_parsed.get("completed"),
                    "tags": tags,
                }, ensure_ascii=False),
                "html_gzip": gzip.compress(raw_bytes, mtime=0),
                "html_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "fetched_at": now,
                "parser_version": "detail-v1",
                "error": "",
            }
            obs_record = {
                "site": site,
                "tid": tid,
                "seeders": archive_parsed.get("seeders"),
                "leechers": archive_parsed.get("leechers"),
                "completed": archive_parsed.get("completed"),
                "page_status": archive_parsed.get("page_status") or "OK",
                "html_sha256": page_record["html_sha256"],
                "fetched_at": now,
            }
            graph.record_detail_page(page_record, obs_record)
            db_actions.append("site_detail_pages")

            # B. 如果在 source_tasks 里存在对应任务（或带有 info_hash），同步更新 source_tasks
            task_hash = summary.get("info_hash")
            if not task_hash:
                # 尝试从库中反查
                existing = graph._conn.execute(
                    "SELECT info_hash FROM source_tasks WHERE source_site=? AND source_tid=?",
                    (site, tid)
                ).fetchone()
                if existing:
                    task_hash = existing[0]

            if task_hash:
                graph.upsert_source_task(
                    info_hash=task_hash,
                    source_site=site,
                    source_tid=tid,
                    source_name=summary["title"],
                )
                graph.update_source_meta(
                    task_hash,
                    source_site=site,
                    source_tid=tid,
                    artist=summary["artist"],
                    album=summary["album"],
                    year=summary["year"],
                    format=summary["format"],
                    medium=summary["medium"],
                    genre_json=summary["genre"],
                    tags_json=tags_json,
                    cover_url=source_parsed.get("cover_url", ""),
                    description=source_parsed.get("description", ""),
                    log_text=source_parsed.get("log_text", ""),
                    tracklist_json=source_parsed.get("tracklist", ""),
                    meta_status="OK",
                )
                if summary["transfer_rule"]:
                    graph.set_transfer_rule(task_hash, summary["transfer_rule"], note=f"fetch={site}#{tid}")
                db_actions.append(f"source_tasks({task_hash[:8]}...)")
        except Exception as exc:
            db_actions.append(f"db_error:{exc}")

    summary["db_synced"] = db_actions
    return summary


def query_tids_from_db(graph: RoutingGraphManager, site: str, limit: int = 20) -> List[str]:
    """从本地数据库中查询候选待抓取 TID"""
    site = normalize_site_key(site)
    tids = []
    # 1. 尝试从 source_tasks 查
    rows = graph._conn.execute(
        "SELECT source_tid FROM source_tasks WHERE source_site=? AND source_tid != '' ORDER BY fetched_at ASC LIMIT ?",
        (site, limit),
    ).fetchall()
    for r in rows:
        tid = str(r[0] or "").strip()
        if tid.isdigit() and tid not in tids:
            tids.append(tid)

    # 2. 若不足，再从 publish_lineage 查
    if len(tids) < limit:
        rows = graph._conn.execute(
            "SELECT target_tid FROM publish_lineage WHERE target_site=? AND target_tid != '' LIMIT ?",
            (site, limit - len(tids)),
        ).fetchall()
        for r in rows:
            tid = str(r[0] or "").strip()
            if tid.isdigit() and tid not in tids:
                tids.append(tid)

    # 3. 再从 site_publish_graph 查
    if len(tids) < limit:
        col = f"{site}_tid"
        cols = {r[1] for r in graph._conn.execute("PRAGMA table_info(site_publish_graph)")}
        if col in cols:
            rows = graph._conn.execute(
                f"SELECT {col} FROM site_publish_graph WHERE {col} IS NOT NULL AND {col} != '' LIMIT ?",
                (limit - len(tids),),
            ).fetchall()
            for r in rows:
                tid = str(r[0] or "").strip()
                if tid.isdigit() and tid not in tids:
                    tids.append(tid)

    return tids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--site", required=True, help="站点短名（如 opencd, uhddolby, musopia, crabpt, pterclub 等）")
    parser.add_argument("--tid", help="单个数字 TID，或逗号分隔的 TID 列表（例如: 198601 或 198601,198602）")
    parser.add_argument("--from-db", action="store_true", help="从本地数据库自动读取待抓取的候选 TID")
    parser.add_argument("--limit", type=int, default=10, help="--from-db 时的最大处理数（默认 10）")
    parser.add_argument("--save-html", action="store_true", help="将清洗脱敏后的详情页 HTML 保存到本地磁盘")
    parser.add_argument("--html-dir", default=DEFAULT_HTML_DIR, help="本地 HTML 存放目录（默认 musichub/data/detail_pages）")
    parser.add_argument("--to-db", action="store_true", help="将解析结果与标签同步写入数据库（site_detail_pages 与 source_tasks）")
    parser.add_argument("--delay", type=float, default=0.8, help="多任务之间的礼貌延迟秒数（默认 0.8）")
    parser.add_argument("--format", choices=("pretty", "json"), default="pretty", help="输出格式（pretty 或 json）")
    args = parser.parse_args()

    site_raw = (args.site or "").strip().lower()
    cfg = MusicHubConfig.from_env()
    db_path = os.path.abspath(cfg.db_path)
    graph = RoutingGraphManager(db_path)

    if site_raw == "all":
        # 全矩阵模式：支持跨站点批处理
        if not args.from_db:
            parser.error("--site all 必须配合 --from-db 使用")
        pending_targets = graph.list_pending_detail_targets(limit=args.limit)
        if not pending_targets:
            print("[Info] 数据库中未找到任何站点的候选待更新目标", file=sys.stderr)
            return 0
        print(f"[*] 全矩阵模式启动，找到 {len(pending_targets)} 条待抓取目标...", file=sys.stderr)
        adapters_cache: Dict[str, Any] = {}
        results = []
        for idx, item in enumerate(pending_targets, 1):
            cur_site = item["site"]
            cur_tid = item["tid"]
            if cur_site not in adapters_cache:
                try:
                    adapters_cache[cur_site] = build_adapter(cur_site)
                except Exception as exc:
                    print(f"[Warning] 跳过未知站点 {cur_site}: {exc}", file=sys.stderr)
                    continue
            if idx > 1 and args.delay > 0:
                time.sleep(args.delay)
            res = fetch_and_process_one(
                site=cur_site,
                tid=cur_tid,
                adapter=adapters_cache[cur_site],
                save_html=args.save_html,
                html_dir=args.html_dir,
                graph=graph,
                to_db=args.to_db,
                delay=args.delay,
            )
            results.append(res)
            status_text = "成功" if res.get("ok") else f"失败({res.get('error')})"
            print(f"  [{idx}/{len(pending_targets)}] {cur_site} tid={cur_tid} -> {status_text}, tags={res.get('tags_count', 0)}", file=sys.stderr)

        if args.format == "json":
            print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    site = normalize_site_key(args.site)
    try:
        adapter = build_adapter(site)
    except Exception as exc:
        print(f"[Error] 无法初始化站点适配器: {exc}", file=sys.stderr)
        return 1

    # 确定候选 TID 列表
    candidate_tids: List[str] = []
    if args.tid:
        for t in args.tid.split(","):
            t = t.strip()
            if t.isdigit() and t not in candidate_tids:
                candidate_tids.append(t)
    elif args.from_db:
        candidate_tids = query_tids_from_db(graph, site, limit=args.limit)
        if not candidate_tids:
            print(f"[Info] 数据库中未找到站点 {site} 的候选 TID", file=sys.stderr)
            return 0
    else:
        parser.error("必须指定 --tid 或 --from-db")

    print(f"[*] 准备抓取站点: {site} ({adapter.base_url}), 目标 TID 数: {len(candidate_tids)}", file=sys.stderr)
    if args.save_html:
        print(f"[*] 本地 HTML 存储目录: {os.path.abspath(args.html_dir)}", file=sys.stderr)

    results = []
    for idx, tid in enumerate(candidate_tids, 1):
        if idx > 1 and args.delay > 0:
            time.sleep(args.delay)
        res = fetch_and_process_one(
            site=site,
            tid=tid,
            adapter=adapter,
            save_html=args.save_html,
            html_dir=args.html_dir,
            graph=graph,
            to_db=args.to_db,
            delay=args.delay,
        )
        results.append(res)
        status_flag = "OK" if res.get("ok") else f"FAIL({res.get('error')})"
        tag_str = ", ".join(t.get("name", "") for t in res.get("tags", [])) or "无"
        print(f"[{idx}/{len(candidate_tids)}] {site}#{tid} -> {status_flag} | 标签[{res.get('tags_count', 0)}]: {tag_str}", file=sys.stderr)

    if args.format == "json":
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print("\n" + "=" * 80)
        print(f"抓取结果汇总（共 {len(results)} 条）：")
        print("=" * 80)
        for r in results:
            if not r.get("ok"):
                print(f"[-] {r['site']}#{r['tid']} 抓取失败: {r.get('error')}")
                continue
            print(f"[+] 种子详情: {r['site']}#{r['tid']}")
            print(f"    标题: {r['title']}")
            if r.get("artist") or r.get("album"):
                print(f"    艺人/专辑: {r.get('artist')} / {r.get('album')} ({r.get('year')})")
            if r.get("format") or r.get("medium"):
                print(f"    规格: {r.get('format')} | {r.get('medium')} | 流派: {r.get('genre') or '-'}")
            print(f"    做种/下载/完成: {r.get('seeders')}/{r.get('leechers')}/{r.get('completed')}")
            print(f"    全量标签 ({r.get('tags_count', 0)} 项):")
            for t in r.get("tags", []):
                id_info = f" (id={t['id']})" if t.get("id") else ""
                title_info = f" [{t['title']}]" if t.get("title") else ""
                print(f"      - {t['name']}{id_info}{title_info}")
            if r.get("html_saved"):
                print(f"    本地 HTML: {r['html_saved']}")
            if r.get("db_synced"):
                print(f"    数据库写入: {', '.join(r['db_synced'])}")
            print("-" * 80)

    failed = any(not r.get("ok") for r in results)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
