# -*- coding: utf-8 -*-
"""MusicHub 3.0 存量 B 种音乐池配对回填工具 (backfill_pool_pairing.py)

背景
----
发布期配对（storage/pool_pairing.py）只对「之后新发布」的种子生效。
历史上已发布并停在 3010 的 B 种，音乐池里没有同 infohash 副本，交棒器
永远命中不了，3010 任务永久滞留。

本工具做一次性回填，只做三件事（全部可 dry-run）：
1. 从 3010 拉取已 100% 的任务列表；
2. 用本地归档 `musichub/data/torrents/*.torrent` 按 infohash 反查种子字节
   （缺失时尝试 qB `/torrents/export`，再不行就跳过并报告）；
3. 在受管音乐池中按种子 `name` 定位实体目录，执行「物理校验 + paused 注入」。

安全约束
--------
- 绝不触碰 music5（配对器内部已硬拦截）；
- 目标文件不存在才建同卷硬链接，已存在且 size 一致直接复用，不一致拒绝覆盖；
- 注入为 paused + skip_checking，不向 tracker 公告，不影响 3010 现有做种；
- 默认 dry-run，必须显式 `--apply` 才写入 qB。

用法
----
    python musichub/tools/backfill_pool_pairing.py --dry-run
    python musichub/tools/backfill_pool_pairing.py --apply
    python musichub/tools/backfill_pool_pairing.py --apply --hash <infohash>
"""

import os
import sys
import glob
import argparse
import logging
from typing import Dict, List, Optional, Tuple

# 确保加载根路径（与 tools/audit_probe.py 一致）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from musichub.core.config import MusicHubConfig
from musichub.core.qb_client import QBClient
from musichub.storage.pool_pairing import PoolPairingManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("PoolPairingBackfill")

ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "torrents")


def build_archive_index(manager: PoolPairingManager, archive_dir: str) -> Dict[str, bytes]:
    """按 infohash 建立本地归档 B 种索引"""
    index: Dict[str, bytes] = {}
    for path in glob.glob(os.path.join(archive_dir, "*.torrent")):
        try:
            with open(path, "rb") as f:
                data = f.read()
            _name, _files, _size, info_hash = manager.parse_torrent(data)
            if info_hash:
                index[info_hash] = data
        except Exception as e:
            logger.debug(f"跳过无法解析的归档种子 {path}: {e}")
    return index


def locate_local_path(manager: PoolPairingManager, name: str) -> Optional[str]:
    """在受管池中按种子根目录名定位实体数据目录"""
    candidates: List[str] = []
    for spec in manager.specs:
        p = os.path.join(spec.host_dir, name)
        if os.path.isdir(p):
            candidates.append(p)
    if not candidates:
        return None
    # 多池同名时优先取「物理校验能通过」的那个
    for cand in candidates:
        spec = manager.find_owning_pool(cand)
        if spec is None:
            continue
        return cand
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="存量 B 种音乐池配对回填")
    parser.add_argument("--apply", action="store_true", help="实际写入 qB（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="显式干跑（默认行为）")
    parser.add_argument("--hash", default="", help="只处理指定 infohash")
    parser.add_argument("--port", type=int, default=0, help="3010 端口，默认读配置")
    parser.add_argument("--archive-dir", default="", help="本地 B 种归档目录，默认 musichub/data/torrents")
    parser.add_argument("--limit", type=int, default=0, help="最多实际处理 N 个（0=不限），便于分批执行")
    parser.add_argument("--only-missing", action="store_true",
                        help="跳过池内已存在同 infohash 的任务，便于多轮批量推进")
    args = parser.parse_args()

    dry_run = not args.apply

    cfg = MusicHubConfig.from_env()
    publish_port = args.port or cfg.publish_qb.port
    publish = QBClient(host=cfg.publish_qb.host, port=publish_port,
                       username=cfg.publish_qb.username, password=cfg.publish_qb.password)
    manager = PoolPairingManager(publish_host=cfg.publish_qb.host)

    archive_dir = args.archive_dir or ARCHIVE_DIR
    archive = build_archive_index(manager, archive_dir)
    logger.info(f"归档 B 种索引: {len(archive)} 个 infohash（目录 {archive_dir}）")

    torrents = publish.get_torrents() or []
    target_hash = (args.hash or "").strip().lower()
    rows: List[Tuple[str, str, str, bool, str]] = []

    pool_hashes = set()
    if args.only_missing:
        for spec in manager.specs:
            try:
                for t in manager.client_for(spec).get_torrents() or []:
                    h = (t.get("hash") or "").lower()
                    if h:
                        pool_hashes.add(h)
            except Exception as e:
                logger.warning(f"读取池 {spec.name} 已有 hash 失败: {e}")
        logger.info(f"池内已有 infohash: {len(pool_hashes)}")

    processed = 0
    for t in torrents:
        info_hash = (t.get("hash") or "").lower()
        name = t.get("name") or ""
        if target_hash and info_hash != target_hash:
            continue
        if float(t.get("progress", 0.0)) < 0.999:
            continue
        if args.only_missing and info_hash in pool_hashes:
            continue
        if args.limit and processed >= args.limit:
            logger.info(f"已达到 --limit {args.limit}，提前结束")
            break
        processed += 1

        torrent_bytes = archive.get(info_hash)
        if not torrent_bytes:
            exported = publish.export_torrent(info_hash)
            if exported:
                torrent_bytes = exported
        if not torrent_bytes:
            rows.append((info_hash, name, "-", False, "缺少种子字节（归档与 3010 导出均失败）"))
            continue

        local_path = locate_local_path(manager, name)
        if not local_path:
            rows.append((info_hash, name, "-", False, "受管池内未找到同名实体目录"))
            continue

        ok, msg = manager.pair_published_torrent(
            torrent_bytes=torrent_bytes,
            local_path=local_path,
            expected_hash=info_hash,
            dry_run=dry_run,
        )
        rows.append((info_hash, name, os.path.basename(local_path), ok, msg))

    print("=" * 100)
    print(f"{'HASH':10} | {'OK':2} | {'ALBUM':40} | 说明")
    print("-" * 100)
    for info_hash, name, _loc, ok, msg in rows:
        print(f"{info_hash[:10]:10} | {'Y' if ok else 'N':2} | {name[:40]:40} | {msg}")
    print("=" * 100)
    ok_cnt = sum(1 for r in rows if r[3])
    print(f"模式: {'DRY-RUN' if dry_run else 'APPLY'} | 处理 {len(rows)} 个 | 成功 {ok_cnt} 个")
    return 0 if ok_cnt == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
