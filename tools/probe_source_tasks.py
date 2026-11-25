# -*- coding: utf-8 -*-
"""源种台账与做种健康只读探针 (probe_source_tasks.py)

背景
----
`source_tasks` 是矩阵发种的准入台账：无 `source_tid` 就没法抓详情页，
详情页缺失又会产出发种正文半截来源声明。历史上有 87 条 3016 早期残行
`source_tid` 为空且 `meta_status=NEED_META`，`daemon._refresh_source_meta`
遇到 `not tid` 直接 return，永远补不上（见专题 17）。

本工具只读，不做任何写操作，做三件事：
1. `--stats`（默认）     盘点 TID / 正文 / 封面缺口；
2. `--backfilled`        列出带回填标记的行，审计回填是否真落库；
3. `--name/--hash`       在 qB 全集群里定位种子，打印实例、状态、tracker 明细。

用法
----
    python musichub/tools/probe_source_tasks.py
    python musichub/tools/probe_source_tasks.py --backfilled
    python musichub/tools/probe_source_tasks.py --name "五月蠅"
    python musichub/tools/probe_source_tasks.py --hash 095848387be9eb5578da6baf85ff7ffc85028ff3
"""

import os
import re
import sys
import sqlite3
import argparse
import logging
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from musichub.core.config import MusicHubConfig
from musichub.core.qb_client import QBClient
from musichub.storage.cluster_guard import EXTRA_INSTANCES, PUBLISH_NAME, PUBLISH_PORT
from musichub.storage.pool_pairing import POOL_SPECS

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

# tracker 状态码（qB API）：0 禁用 / 1 未联系 / 2 工作 / 3 更新中 / 4 不工作
TRACKER_STATUS = {0: "disabled", 1: "not_contacted", 2: "WORKING", 3: "updating", 4: "NOT_WORKING"}


def redact(text: str) -> str:
    """凭据不外泄：输出前把 passkey/token 打码（AGENTS.md 第 2 节第 6 条）"""
    text = re.sub(r"(passkey=)[^&\s'\"]+", r"\1***", text or "")
    return re.sub(r"((?:token|authkey|auth)=)[^&\s'\"]+", r"\1***", text)


def open_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def show_stats(con: sqlite3.Connection) -> None:
    c = con.cursor()
    total = c.execute("SELECT COUNT(*) n FROM source_tasks").fetchone()["n"]
    empty_tid = c.execute(
        "SELECT COUNT(*) n FROM source_tasks WHERE source_tid IS NULL OR TRIM(source_tid)=''"
    ).fetchone()["n"]
    empty_desc = c.execute(
        "SELECT COUNT(*) n FROM source_tasks WHERE description IS NULL OR TRIM(description)=''"
    ).fetchone()["n"]
    no_cover = c.execute(
        "SELECT COUNT(*) n FROM source_tasks WHERE cover_url IS NULL OR TRIM(cover_url)=''"
    ).fetchone()["n"]
    print(f"total={total} empty_tid={empty_tid} desc_empty={empty_desc} cover_empty={no_cover}")

    print("\n--- 仍无 TID ---")
    for r in c.execute(
        "SELECT info_hash, source_site, source_name, meta_status FROM source_tasks "
        "WHERE source_tid IS NULL OR TRIM(source_tid)='' ORDER BY rowid"
    ):
        print(f"  {r['info_hash']}  {r['source_site']:<10} {r['source_name']}")

    print("\n--- 有 TID 但正文为空（排查详情页抓取是否真的写进去了）---")
    rows = c.execute(
        "SELECT info_hash, source_site, source_tid, source_name, meta_status, "
        "LENGTH(COALESCE(tracklist_json,'')) tlen FROM source_tasks "
        "WHERE source_tid IS NOT NULL AND TRIM(source_tid)<>'' "
        "AND (description IS NULL OR TRIM(description)='') ORDER BY rowid"
    ).fetchall()
    for r in rows:
        note = "（站点页面本身无简介，只有曲目）" if r["tlen"] else "（曲目也为空，需重抓）"
        print(f"  {r['source_site']:<10} tid={r['source_tid']:<8} {r['source_name'][:52]} {note}")


def show_backfilled(con: sqlite3.Connection) -> None:
    rows = con.cursor().execute(
        "SELECT source_site, source_tid, source_name, meta_status, transfer_rule, "
        "LENGTH(COALESCE(description,'')) dlen, LENGTH(COALESCE(tracklist_json,'')) tlen, "
        "CASE WHEN cover_url IS NULL OR TRIM(cover_url)='' THEN 0 ELSE 1 END cover, note "
        "FROM source_tasks WHERE note LIKE 'backfill=%' ORDER BY rowid"
    ).fetchall()
    print(f"回填行共 {len(rows)} 条")
    bad = 0
    for r in rows:
        ok = r["dlen"] > 0 or r["tlen"] > 0
        bad += 0 if ok else 1
        print(f"  tid={r['source_tid']:>7} dlen={r['dlen']:>5} tlen={r['tlen']:>4} "
              f"cover={r['cover']} {r['meta_status']:<9} {r['source_name'][:50]:<50} {r['note']}")
    print(f"\n正文与曲目同时为空 = {bad}")


def iter_clients(config: MusicHubConfig, include_extra: bool):
    """按 cluster_guard 的实例登记表遍历，避免在工具里重复硬编码端口"""
    specs = [(name, port) for name, port, _dir in POOL_SPECS]
    specs.append((PUBLISH_NAME, PUBLISH_PORT))
    if include_extra:
        specs.extend(EXTRA_INSTANCES)
    for name, port in specs:
        yield name, port, QBClient(host=config.publish_qb.host, port=port)


def locate(config: MusicHubConfig, name: str = "", info_hash: str = "", include_extra: bool = True) -> None:
    key = (name or "").strip().upper()
    target_hash = (info_hash or "").strip().lower()
    hits = 0
    for inst, port, client in iter_clients(config, include_extra):
        # qB 5.x 登录成功返回 204，QBClient.login() 按 "Ok." 判定会假阴性，不据其返回值短路
        client.login()
        try:
            torrents: List[Dict[str, Any]] = client.get_torrents()
        except Exception as e:
            print(f"[{inst}:{port}] 读取失败: {e}")
            continue
        for t in torrents:
            th = (t.get("hash") or "").lower()
            tn = (t.get("name") or "")
            matched = (target_hash and th == target_hash) or (key and key in tn.upper())
            if not matched:
                continue
            hits += 1
            print(f"\n[{inst}:{port}] {th}")
            print(f"  name={tn}")
            print(f"  state={t.get('state')} progress={t.get('progress')} ratio={t.get('ratio')} "
                  f"uploaded={t.get('uploaded')} downloaded={t.get('downloaded')}")
            print(f"  save_path={t.get('save_path')} tags={t.get('tags')}")
            print(f"  announce={redact(t.get('tracker') or '')}")
            for tr in client.get_trackers(th):
                st = tr.get("status")
                print(f"    TR {redact(tr.get('url') or '')} status={st}({TRACKER_STATUS.get(st, '?')}) "
                      f"msg={tr.get('msg')!r} seeds={tr.get('num_seeds')} peers={tr.get('num_peers')}")
    if not hits:
        print("未在集群中命中；检查 --name/--hash 或加 --include-extra")


def main() -> int:
    ap = argparse.ArgumentParser(description="source_tasks 台账与做种健康只读探针")
    ap.add_argument("--db", default="", help="musichub.db 路径（默认取 MusicHubConfig.db_path）")
    ap.add_argument("--stats", action="store_true", help="盘点 TID/正文/封面缺口（默认行为）")
    ap.add_argument("--backfilled", action="store_true", help="审计 note LIKE 'backfill=%%' 的行")
    ap.add_argument("--name", default="", help="按种子名关键词在 qB 全集群定位")
    ap.add_argument("--hash", default="", help="按 info-hash 在 qB 全集群定位")
    ap.add_argument("--no-extra", action="store_true", help="定位时不扫 others/reseed1~7")
    args = ap.parse_args()

    config = MusicHubConfig.from_env()
    db = args.db or config.db_path
    con = open_db(db)

    if args.name or args.hash:
        locate(config, args.name, args.hash, include_extra=not args.no_extra)
        if not args.backfilled:
            return 0
    if args.backfilled:
        show_backfilled(con)
        return 0
    show_stats(con)
    return 0


if __name__ == "__main__":
    sys.exit(main())
