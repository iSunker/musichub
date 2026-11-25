# -*- coding: utf-8 -*-
"""MusicHub 源种任务回填工具 (backfill_source_tasks.py)

背景
----
`source_tasks`（源种任务表）只对上线之后新到达的 3016 任务自动建行。
历史上已在 3016 完成 / 3010 在册的种子没有任务行，缺准入判定与详情元数据。

本工具做一次性回填（默认 dry-run）：
1. 读取指定 qB 实例的任务列表（默认 3016 下载缓冲，可加 --include-publish 带上 3010）；
2. 用 Vertex RSS 账本反查源站与 tid（只读，不发任何请求）；
3. 有 tid 时抓一次详情页，把艺术家/专辑/年份/简介正文/日志/曲目/转载规则写进 source_tasks；
4. 抓不到的落 NEED_META，由 daemon 的补抓线程继续重试。

用法
----
    python musichub/tools/backfill_source_tasks.py --dry-run
    python musichub/tools/backfill_source_tasks.py --apply --limit 50
    python musichub/tools/backfill_source_tasks.py --apply --include-publish
"""

import os
import sys
import time
import argparse
import logging
from typing import Dict, Optional

# 确保加载根路径（与 tools/backfill_pool_pairing.py 一致）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from musichub.core.config import MusicHubConfig
from musichub.core.qb_client import QBClient
from musichub.routing.graph import RoutingGraphManager
from musichub.collector.rss_watcher import RSSWatcher
from musichub.collector.source_detail import (
    SourceDetailFetcher, detect_source_site, extract_tid,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SourceTaskBackfill")


def _guess_format(result: Dict) -> str:
    raw = f"{result.get('format', '')} {result.get('title', '')}".upper()
    for codec in ("FLAC", "APE", "WAV", "AAC", "MP3", "DSD", "DSF", "DFF", "ALAC"):
        if codec in raw:
            return "DSD" if codec in ("DSD", "DSF", "DFF") else codec
    return "FLAC"


def backfill_one(
    graph: RoutingGraphManager,
    watcher: RSSWatcher,
    fetcher: SourceDetailFetcher,
    torrent: Dict,
    apply: bool = False,
) -> str:
    """回填单个任务；返回结果标记（供汇总统计）"""
    info_hash = (torrent.get("hash") or "").strip().lower()
    name = torrent.get("name") or ""
    if not info_hash:
        return "no_hash"

    existing = graph.get_source_task(info_hash)
    if existing and existing.get("meta_status") == "OK":
        return "already_ok"

    v_meta = watcher.get_vertex_origin_metadata(info_hash) or {}
    tracker_text = v_meta.get("tracker") or torrent.get("tracker") or ""
    site = detect_source_site(tracker_text)
    tid = extract_tid(v_meta.get("link") or "") or extract_tid(torrent.get("comment") or "")
    if not site or not tid:
        logger.info(f"[skip] {info_hash} {name} 源站/ tid 未识别（site={site or '-'} tid={tid or '-'}）")
        return "unresolved"

    logger.info(f"[plan] {info_hash} {name} -> {site} tid={tid}")
    if not apply:
        return "would_backfill"

    graph.upsert_source_task(
        info_hash=info_hash,
        source_site=site,
        source_tid=tid,
        source_name=v_meta.get("name") or name,
    )
    result = fetcher.fetch(site, tid)
    if not result.get("ok"):
        logger.warning(f"[meta] {info_hash} 详情抓取失败: {result.get('error')}")
        return "meta_failed"

    graph.update_source_meta(
        info_hash,
        source_site=site,
        source_tid=str(result.get("source_tid") or tid or "").strip(),
        artist=result.get("artist", ""),
        album=result.get("album", ""),
        year=result.get("year", ""),
        format=result.get("format") or _guess_format(result),
        medium=result.get("medium") or "CD",
        cover_url=result.get("cover_url", ""),
        description=result.get("description", ""),
        log_text=result.get("log_text", ""),
        tracklist_json=result.get("tracklist", ""),
        genre_json=result.get("genre", ""),
        tags_json=result.get("tags_json", ""),
        meta_status="OK",
    )
    rule = result.get("transfer_rule") or ("NEED_CONFIRM" if site == "opencd" else "ALLOW")
    graph.set_transfer_rule(info_hash, rule, note=f"backfill={site}#{tid}")
    return "backfilled"


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 musichub source_tasks 源种任务表")
    parser.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="显式 dry-run（与默认一致）")
    parser.add_argument("--limit", type=int, default=100, help="最多处理多少个任务")
    parser.add_argument("--include-publish", action="store_true", help="同时扫描 3010 发种实例")
    args = parser.parse_args()
    apply = bool(args.apply) and not args.dry_run

    config = MusicHubConfig.from_env()
    graph = RoutingGraphManager(db_path=config.db_path)

    clients = [("musicdownload", QBClient(
        host=config.musicdownload_qb.host,
        port=config.musicdownload_qb.port,
        username=config.musicdownload_qb.username,
        password=config.musicdownload_qb.password,
    ))]
    if args.include_publish:
        clients.append(("publish", QBClient(
            host=config.publish_qb.host,
            port=config.publish_qb.port,
            username=config.publish_qb.username,
            password=config.publish_qb.password,
        )))

    watcher = RSSWatcher(qb_download=clients[0][1])
    fetcher = SourceDetailFetcher()
    stats: Dict[str, int] = {}
    processed = 0

    for client_name, client in clients:
        for torrent in client.get_torrents() or []:
            if processed >= args.limit:
                break
            outcome = backfill_one(graph, watcher, fetcher, torrent, apply=apply)
            stats[outcome] = stats.get(outcome, 0) + 1
            processed += 1
            if apply and outcome in ("backfilled", "meta_failed"):
                time.sleep(1.0)

    logger.info(f"回填完成（apply={apply}，实例={[c[0] for c in clients]}）：{stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
