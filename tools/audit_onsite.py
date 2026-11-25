# -*- coding: utf-8 -*-
"""MusicHub 已发布详情页归档与健康审计（默认不写生产）。

  python musichub/tools/audit_onsite.py --sync --site xingwan --tid 4365
  python musichub/tools/audit_onsite.py --sync --apply --site xingwan --days 2
  python musichub/tools/audit_onsite.py --audit --site xingwan --days 2 --format json

--sync 不带 --apply 只打印归档计划；--audit 不写 SQLite/qB，也不会缓存远程种子。
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from musichub.core.config import MusicHubConfig
from musichub.core.qb_client import QBClient
from musichub.onsite.detail_archive import DetailArchive, load_published_targets
from musichub.onsite.cover_identity import compare_site_cover, resolve_cover_identity
from musichub.routing.graph import RoutingGraphManager, normalize_site_key, torrent_infohash
from musichub.storage.pool_pairing import POOL_SPECS

PAUSED_STATES = {"stoppedup", "pausedup", "stoppeddl", "pauseddl"}
SEEDING_STATES = {"stalledup", "uploading", "forcedup", "queuedup", "checkingup"}


def archived_pages(db_path: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """只读加载归档；表不存在表示尚未启用，不为只读审计创建生产表。"""
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='site_detail_pages'"
        ).fetchone()
        if not exists:
            return {}
        return {
            (r["site"], r["tid"]): dict(r)
            for r in conn.execute("SELECT * FROM site_detail_pages")
        }
    finally:
        conn.close()


def cluster_snapshot(host: str):
    clients = {"publish": QBClient(host=host, port=3010)}
    clients.update({name: QBClient(host=host, port=port) for name, port, _ in POOL_SPECS})
    torrents: Dict[str, List[Dict[str, Any]]] = {}
    errors: Dict[str, str] = {}
    for name, client in clients.items():
        try:
            rows = client.get_torrents(strict=True)
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
            continue
        for row in rows:
            info_hash = str(row.get("hash") or "").lower()
            if info_hash:
                item = dict(row)
                item["_instance"] = name
                item["_client"] = client
                torrents.setdefault(info_hash, []).append(item)
    if len(errors) == len(clients):
        raise RuntimeError(f"全部 qB 实例查询失败: {errors}")
    return torrents, errors


def local_torrent_path(db_path: str, site: str, tid: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "torrents", f"{site}_{tid}.torrent")


def resolve_b_hash(target: Dict[str, Any], archive: DetailArchive, db_path: str) -> Tuple[str, str]:
    known = str(target.get("b_hash") or "").lower()
    if len(known) == 40 and all(c in "0123456789abcdef" for c in known):
        return known, "publish_lineage"
    site, tid = target["target_site"], target["target_tid"]
    path = local_torrent_path(db_path, site, tid)
    payload = b""
    source = ""
    try:
        with open(path, "rb") as fh:
            payload = fh.read()
        source = "local_archive"
    except OSError:
        try:
            payload = archive.adapter(site).download_torrent(tid) or b""
            source = "site_download"
        except Exception:
            return "", "FETCH_ERROR"
    if len(payload) <= 100 or payload[:4096].lstrip().lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return "", "FETCH_ERROR"
    info_hash = torrent_infohash(payload)
    return (info_hash, source) if info_hash else ("", "FETCH_ERROR")


def tracker_evidence(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    evidence = []
    for hit in hits:
        client = hit["_client"]
        for tracker in client.get_trackers(hit["hash"]):
            url = str(tracker.get("url") or "")
            if not url or url.startswith("**"):
                continue
            evidence.append({
                "instance": hit["_instance"], "url_host": url.split("/")[2] if "://" in url else url,
                "status": tracker.get("status"), "msg": str(tracker.get("msg") or "")[:300],
                "num_incomplete": tracker.get("num_incomplete"),
            })
    return evidence


def audit_rows(targets: List[Dict[str, Any]], pages: Dict[Tuple[str, str], Dict[str, Any]],
               archive: DetailArchive, db_path: str, host: str) -> List[Dict[str, Any]]:
    torrents, client_errors = cluster_snapshot(host)
    output = []
    for target in targets:
        site, tid = target["target_site"], target["target_tid"]
        page = pages.get((site, tid), {})
        issues: List[str] = []
        if not page:
            issues.append("PAGE_NOT_ARCHIVED")
        elif page.get("fetch_status") != "OK":
            issues.append(page.get("fetch_status") or "FETCH_ERROR")
        try:
            covers = json.loads(page.get("cover_urls_json") or "[]")
        except (TypeError, ValueError):
            covers = []
        try:
            parsed = json.loads(page.get("parsed_json") or "{}")
        except (TypeError, ValueError):
            parsed = {}
        source_tid = str(target.get("source_tid") or "")
        if source_tid.isdigit() and page.get("fetch_status") == "OK":
            links = parsed.get("source_links") or []
            if not any(f"id={source_tid}" in str(link) for link in links):
                issues.append("SOURCE_LINK_BAD")

        identity = resolve_cover_identity(target, db_path, archive=archive)
        b_hash = str(identity.get("b_hash") or "")
        identity_source = str(identity.get("torrent_source") or identity.get("status") or "")
        cover_check = (compare_site_cover(identity, covers)
                       if page and page.get("fetch_status") == "OK" else {"status": "NOT_CHECKED"})
        if cover_check.get("status") not in ("MATCH", "NOT_CHECKED"):
            issues.append(cover_check["status"])
        hits = torrents.get(b_hash, []) if b_hash else []
        instances = [h["_instance"] for h in hits]
        seeding_time = max((int(h.get("seeding_time") or 0) for h in hits), default=0)
        uploaded = max((int(h.get("uploaded") or 0) for h in hits), default=0)
        trackers = tracker_evidence(hits) if hits else []
        if not b_hash:
            issues.append("IDENTITY_UNRESOLVED")
        elif not hits:
            issues.append("B_MISSING")
        elif len(hits) > 1:
            issues.append("B_AMBIGUOUS")
        else:
            state = str(hits[0].get("state") or "").lower()
            if state in PAUSED_STATES:
                issues.append("B_PAUSED")
            elif not (float(hits[0].get("progress") or 0) >= 1.0 and state in SEEDING_STATES):
                issues.append("B_PRESENT")
            elif any(int(t.get("status") or 0) == 4 for t in trackers):
                issues.append("B_TRACKER_ERROR")
            else:
                qb_incomplete = hits[0].get("num_incomplete")
                if parsed.get("leechers") == 0 or (
                    qb_incomplete is not None and int(qb_incomplete) == 0
                ):
                    issues.append("NO_DOWNLOADERS")
        output.append({
            "site": site, "tid": tid, "title": page.get("title"),
            "page_status": page.get("fetch_status") or "PAGE_NOT_ARCHIVED",
            "seeders": parsed.get("seeders"), "leechers": parsed.get("leechers"),
            "completed": parsed.get("completed"), "cover_urls": covers,
            "source_links": parsed.get("source_links") or [], "b_hash": b_hash,
            "identity_source": identity_source, "cover_identity": identity,
            "cover_check": cover_check, "instances": instances,
            "qb_states": [h.get("state") for h in hits],
            "qb_progress": [h.get("progress") for h in hits],
            "qb_num_incomplete": [h.get("num_incomplete") for h in hits],
            "seeding_time": seeding_time,
            "uploaded": uploaded, "trackers": trackers, "issues": issues,
            "qb_client_errors": client_errors,
        })
    return output


def public_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return row


def print_table(rows: List[Dict[str, Any]]) -> None:
    for row in rows:
        counts = f"s={row.get('seeders')}/l={row.get('leechers')}/c={row.get('completed')}"
        print(f"{row['site']}#{row['tid']:<8} {','.join(row['issues']) or 'OK':<38} "
              f"{counts:<18} seed={row['seeding_time']}s up={row['uploaded']} "
              f"inst={','.join(row['instances']) or '-'} hash={row['b_hash'][:10] or '-'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sync", action="store_true", help="归档详情页；写库还需 --apply")
    parser.add_argument("--audit", action="store_true", help="只读审计页面、来源、封面字段与 B 种做种")
    parser.add_argument("--apply", action="store_true", help="允许 --sync 写详情页数据库")
    parser.add_argument("--site", default="", help="站点短名，缺省为全部已发布站点")
    parser.add_argument("--tid", default="", help="单个数字 tid")
    parser.add_argument("--days", type=int, default=0, help="只选近 N 天发布；0 为全部")
    parser.add_argument("--limit", type=int, default=0, help="显式限制本次目标数；0 不截断")
    parser.add_argument("--force", action="store_true", help="忽略 24h 增量窗口，重新抓取")
    parser.add_argument("--save-html", action="store_true", help="抓取时同步将脱敏后的 HTML 保存到本地磁盘")
    parser.add_argument("--html-dir", default="data/detail_pages", help="本地 HTML 保存目录（默认 data/detail_pages）")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args()
    if not args.sync and not args.audit:
        parser.error("至少指定 --sync 或 --audit")
    if args.apply and not args.sync:
        parser.error("--apply 只能与 --sync 连用")

    cfg = MusicHubConfig.from_env()
    db_path = os.path.abspath(cfg.db_path)
    targets = load_published_targets(db_path, args.site, args.tid, args.days)
    pages = archived_pages(db_path)
    now = time.time()
    if args.sync and not args.force:
        targets = [r for r in targets if now - float(pages.get(
            (r["target_site"], r["target_tid"]), {}).get("fetched_at") or 0) >= 86400]
    total_eligible = len(targets)
    truncated = bool(args.limit > 0 and len(targets) > args.limit)
    if args.limit > 0:
        targets = targets[:args.limit]
    if not targets:
        print(json.dumps({"targets": 0, "eligible": total_eligible, "truncated": truncated}, ensure_ascii=False))
        return 0

    graph = RoutingGraphManager(db_path) if args.apply else None
    archive = DetailArchive(graph, delay=0.8)
    sync_results = []
    if args.sync:
        if not args.apply:
            sync_results = [{"site": r["target_site"], "tid": r["target_tid"], "planned": True} for r in targets]
        else:
            for index, target in enumerate(targets, 1):
                result = archive.archive(
                    target["target_site"], target["target_tid"], apply=True,
                    save_html=args.save_html, html_dir=args.html_dir,
                )
                page = result["page"]
                pages[(target["target_site"], target["target_tid"])] = {
                    **page, "cover_urls_json": page.get("cover_urls_json", "[]"),
                    "parsed_json": page.get("parsed_json", "{}"),
                }
                sync_results.append({
                    "site": target["target_site"], "tid": target["target_tid"],
                    "status": page["fetch_status"], "http_status": page.get("http_status"),
                    "error": page.get("error") or "", "written": True,
                    "html_path": page.get("html_path", ""),
                })
                print(f"[{index}/{len(targets)}] {target['target_site']}#{target['target_tid']} "
                      f"{page['fetch_status']}", file=sys.stderr)

    audit_results = audit_rows(targets, pages, archive, db_path, cfg.publish_qb.host) if args.audit else []
    status_counts = Counter(x["status"] for x in sync_results if x.get("status"))
    issue_counts = Counter(issue for row in audit_results for issue in row["issues"])
    result = {
        "mode": "apply" if args.apply else "read-only",
        "eligible": total_eligible, "selected": len(targets), "truncated": truncated,
        "sync": [public_row(x) for x in sync_results], "sync_counts": dict(status_counts),
        "audit": [public_row(x) for x in audit_results], "issue_counts": dict(issue_counts),
    }
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if args.sync:
            for row in sync_results:
                print(f"SYNC {row['site']}#{row['tid']} {row.get('status', 'PLANNED')} {row.get('error', '')}")
            print(f"归档目标={len(targets)} eligible={total_eligible} truncated={truncated} counts={dict(status_counts)}")
        if args.audit:
            print_table(audit_results)
            print(f"审计目标={len(audit_results)} issue_counts={dict(issue_counts)}")
    failed = any(x.get("status") not in (None, "OK") for x in sync_results)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
