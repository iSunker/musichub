# -*- coding: utf-8 -*-
"""MusicHub 发种来龙去脉查询工具 (find_lineage.py)

回答两个问题：
1. 「这个源种/RSS 任务发到哪些站点去了？」→ 按源种 hash / 专辑名查。
2. 「站点上这个 tid 的种子，本地是哪个 B 种、该在哪个池补种？」→ 按目标站 tid 查。

数据来源
--------
- `publish_lineage`（2026-11-21 新增）：源种 → 目标站 tid → B 种 infohash → 交棒实例/目录；
- `site_publish_graph` / `publish_attempt_log`：历史行兜底（早于台账上线，无 b_hash）；
- `cluster_infohash_registry` / `pool_event_log`：当前在册与入栈流水；
- 现场 qB 探针（3010 + 8 个受管池）：确认该 B 种现在是否真的在跑、进度多少。

用法
----
    # 站点链接里的 tid（lemonhd 48704）
    python musichub/tools/find_lineage.py --tid 48704 --site lemonhd
    # 源种 infohash（3016 任务）
    python musichub/tools/find_lineage.py --source-hash <infohash>
    # 按专辑名模糊找
    python musichub/tools/find_lineage.py --album "Brown Spirits"
    # 台账没记 b_hash 时，从站点下载 B 种反查
    python musichub/tools/find_lineage.py --tid 48704 --site lemonhd --fetch
"""

import os
import sys
import json
import argparse
import logging
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from musichub.core.config import MusicHubConfig
from musichub.core.qb_client import QBClient
from musichub.routing.graph import RoutingGraphManager
from musichub.storage.pool_pairing import PoolPairingManager, POOL_SPECS

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("FindLineage")


def probe_instances(b_hash: str, host: str) -> List[Dict[str, Any]]:
    """现场探针：该 B 种当前在哪些 qB 实例里、进度与目录"""
    hits: List[Dict[str, Any]] = []
    if not b_hash:
        return hits
    insts = [("publish", 3010)] + [(name, port) for name, port, _ in POOL_SPECS]
    for name, port in insts:
        try:
            rows = QBClient(host=host, port=port).get_torrents()
        except Exception as e:
            hits.append({"instance": name, "port": port, "error": str(e)[:120]})
            continue
        for t in rows:
            if (t.get("hash") or "").lower() == b_hash:
                hits.append({
                    "instance": name,
                    "port": port,
                    "name": t.get("name"),
                    "progress": t.get("progress"),
                    "state": t.get("state"),
                    "save_path": t.get("save_path"),
                    "content_path": t.get("content_path"),
                    "size": t.get("size"),
                })
    return hits


def fetch_b_torrent(site: str, tid: str) -> Optional[bytes]:
    """从站点下载 B 种（需要 CookieCloud 登录态），用于台账缺 b_hash 的历史行"""
    from musichub.adapters.sites import (
        MusopiaAdapter, CrabPTAdapter, PTerClubAdapter, XingwanAdapter, HDKylinAdapter,
        TangPTAdapter, MuXueGeAdapter, LemonHDAdapter, UHDDolbyAdapter, HDTimeAdapter,
    )

    mapping = {
        "lemonhd": LemonHDAdapter, "musopia": MusopiaAdapter, "crabpt": CrabPTAdapter,
        "pterclub": PTerClubAdapter, "xingwan": XingwanAdapter, "hdkylin": HDKylinAdapter,
        "tangpt": TangPTAdapter, "muxuege": MuXueGeAdapter, "uhddolby": UHDDolbyAdapter,
        "hdtime": HDTimeAdapter,
    }
    cls = mapping.get((site or "").strip().lower())
    if not cls:
        print(f"!! 未登记的目标站适配器: {site}")
        return None
    return cls().download_torrent(str(tid))


def print_row(row: Dict[str, Any], host: str) -> None:
    print("-" * 78)
    print(f"[台账 id={row.get('id')}] {row.get('created_at')}  状态={row.get('status')}")
    print(f"  源种: site={row.get('source_site') or '-'} tid={row.get('source_tid') or '-'} "
          f"hash={row.get('source_hash') or '-'}")
    print(f"  源名: {row.get('source_name') or '-'}")
    print(f"  本地: {row.get('local_path') or '-'}")
    print(f"  目标: {row.get('target_site')} tid={row.get('target_tid') or '-'}")
    if row.get("target_url"):
        print(f"  页面: {row['target_url']}")
    print(f"  B 种: {row.get('b_hash') or '(未记录)'}  实例={row.get('qb_instance') or '-'}"
          f"({row.get('qb_port') or '-'}) save_path={row.get('save_path') or '-'}")
    if row.get("note"):
        print(f"  备注: {row.get('note')}")
    hits = probe_instances(row.get("b_hash") or "", host)
    if not row.get("b_hash"):
        print("  现场: 台账无 b_hash，无法探针（可用 --fetch 从站点取 B 种）")
    elif not hits:
        print("  ⚠ 现场: 该 B 种不在任何 qB 实例中 ⇒ 未做种，需要补种")
    else:
        for h in hits:
            if h.get("error"):
                print(f"  现场: {h['instance']}({h['port']}) 读取失败 {h['error']}")
            else:
                print(f"  现场: {h['instance']}({h['port']}) progress={h['progress']} "
                      f"state={h['state']} save_path={h['save_path']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="MusicHub 发种来龙去脉查询")
    parser.add_argument("--tid", default="", help="目标站种子 ID（details.php?id=）")
    parser.add_argument("--site", default="", help="目标站短名，如 lemonhd")
    parser.add_argument("--hash", default="", help="B 种 infohash")
    parser.add_argument("--source-hash", default="", help="源种 infohash（3016 任务）")
    parser.add_argument("--album", default="", help="专辑名/目录名模糊匹配")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--fetch", action="store_true", help="台账无 b_hash 时从站点下载 B 种并定位")
    args = parser.parse_args()

    cfg = MusicHubConfig.from_env()
    host = cfg.publish_qb.host
    mgr = RoutingGraphManager(db_path=cfg.db_path)

    rows = mgr.find_lineage(
        target_tid=args.tid,
        target_site=args.site,
        b_hash=args.hash,
        source_hash=args.source_hash,
        album=args.album,
        limit=args.limit,
    )

    if rows:
        print(f"=== publish_lineage 命中 {len(rows)} 条 ===")
        for row in rows:
            print_row(row, host)
    else:
        print("=== publish_lineage 无记录，回退历史表 ===")

    # 历史兜底：site_publish_graph（只有指纹与站点 tid，没有 b_hash）
    if not rows and args.tid:
        with mgr._lock:  # noqa: SLF001 - 只读查询，沿用模块内联 SQL
            cur = mgr._conn.execute(
                "SELECT id, origin_site, origin_torrent_id, target_site, target_torrent_id, published_at "
                "FROM site_publish_graph WHERE target_torrent_id = ? ORDER BY id DESC LIMIT ?",
                (str(args.tid), args.limit),
            )
            graph_rows = [dict(r) for r in cur]
        for r in graph_rows:
            print(f"[graph id={r['id']}] {r['published_at']} 源={r['origin_site']} "
                  f"源tid/名={r['origin_torrent_id']} 目标={r['target_site']}#{r['target_torrent_id']} "
                  f"（历史行，无 B 种记录）")

    if not rows and args.album:
        with mgr._lock:  # noqa: SLF001
            cur = mgr._conn.execute(
                "SELECT id, album_name, target_site, result, target_torrent_id, reason, created_at "
                "FROM publish_attempt_log WHERE album_name LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{args.album}%", args.limit),
            )
            for r in cur:
                d = dict(r)
                print(f"[attempt id={d['id']}] {d['created_at']} {d['album_name']} -> "
                      f"{d['target_site']} {d['result']} tid={d['target_torrent_id']} "
                      f"reason={d['reason']}")

    # --fetch：台账无 b_hash 时，从站点下载 B 种后现场定位
    if args.fetch and args.site:
        tids = [args.tid] if args.tid else [r.get("target_tid") for r in rows if r.get("target_tid")]
        for tid in [t for t in tids if t and str(t).isdigit()]:
            data = fetch_b_torrent(args.site, str(tid))
            if not data:
                print(f"\n!! 无法从 {args.site} 下载 tid={tid} 的 B 种（Cookie/网络）")
                continue
            name, files, size, info_hash = PoolPairingManager.parse_torrent(data)
            print(f"\n=== 站点 B 种 {args.site}#{tid} ===")
            print(f"  name={name} size={size} files={len(files)} infohash={info_hash}")
            out = os.path.join("/tmp", f"{args.site}_{tid}.torrent")
            try:
                with open(out, "wb") as f:
                    f.write(data)
                print(f"  已暂存: {out}")
            except Exception as e:
                print(f"  暂存失败: {e}")
            hits = probe_instances(info_hash, host)
            if not hits:
                print("  ⚠ 该 B 种不在任何 qB 实例 ⇒ 未做种，需要补种")
            for h in hits:
                print(f"  现场: {h.get('instance')}({h.get('port')}) progress={h.get('progress')} "
                      f"state={h.get('state')} save_path={h.get('save_path')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
