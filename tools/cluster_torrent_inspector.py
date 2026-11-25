# -*- coding: utf-8 -*-
"""MusicHub 3.0 集群种子深度穿透巡检器 (ClusterTorrentInspector)

功能：
1. 【精确哈希查重】：跨全集群 26 个 qBittorrent 实例秒查指定 InfoHash，定位活动实例、幽灵席位与多处做种；
2. 【专辑/关键词模糊排查】：输入专辑名或关键字，跨所有实例列出各站点对应版本、哈希与完成度；
3. 【Tracker 状态与回执提取】：精准抓取 Tracker 报错（如 NexusPHP "more than 1 client", "already leeching"）；
4. 【保种池健康概览】：一键输出 8 大音乐池与下载端的当前下载/做种/异常统计。
"""

import argparse
import json
import logging
import sys
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ClusterInspector")

HOST = "192.168.0.7"

INSTANCES: List[Tuple[str, int]] = [
    ("publish", 3010),
    ("vertex", 3005),
    ("download", 3015),
    ("musicdownload", 3016),
    ("movies1", 3020),
    ("movies2", 3040),
    ("movies3", 3060),
    ("movies4", 3080),
    ("tv1", 3120),
    ("tv2", 3140),
    ("others", 3180),
    ("music_pool_1", 3210),
    ("music_pool_2", 3220),
    ("music_pool_3", 3230),
    ("music_pool_4", 3240),
    ("music_pool_5", 3250),
    ("music_pool_6", 3260),
    ("music_pool_7", 3270),
    ("music_pool_8", 3280),
    ("reseed1", 3320),
    ("reseed2", 3330),
    ("reseed3", 3340),
    ("reseed4", 3350),
    ("reseed5", 3360),
    ("reseed6", 3370),
    ("reseed7", 3380),
]

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def qb_get(port: int, path: str, timeout: float = 6.0) -> Optional[Any]:
    url = f"http://{HOST}:{port}{path}"
    try:
        req = urllib.request.Request(url)
        with _opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return None


def inspect_trackers(port: int, info_hash: str) -> List[Dict[str, Any]]:
    res = qb_get(port, f"/api/v2/torrents/trackers?hash={info_hash}")
    if not res or not isinstance(res, list):
        return []
    return [tr for tr in res if tr.get("url", "").startswith("http")]


def scan_hash(target_hash: str) -> None:
    target_hash = target_hash.strip().lower()
    print(f"\n=======================================================")
    print(f" 正在对全集群 26 实例穿透扫描 InfoHash: {target_hash}")
    print(f"=======================================================")
    hits = []
    for inst_name, port in INSTANCES:
        torrents = qb_get(port, f"/api/v2/torrents/info?hashes={target_hash}")
        if torrents and len(torrents) > 0:
            for t in torrents:
                hits.append((inst_name, port, t))

    if not hits:
        print("[-] 全集群 26 实例均未发现该 InfoHash。")
        return

    print(f"[+] 发现 {len(hits)} 处实例命中：")
    for inst_name, port, t in hits:
        state = t.get("state")
        prog = t.get("progress", 0.0) * 100
        size_mb = t.get("size", 0) / (1024 * 1024)
        sp = t.get("save_path")
        tags = t.get("tags")
        print(f"\n* 实例: {inst_name} (端口 {port})")
        print(f"  - 名称: {t.get('name')}")
        print(f"  - 状态: {state} | 进度: {prog:.2f}% | 大小: {size_mb:.2f} MB")
        print(f"  - 路径: {sp}")
        print(f"  - 标签: {tags}")

        # 取 tracker 回执
        trackers = inspect_trackers(port, target_hash)
        if trackers:
            print(f"  - Tracker 状态:")
            for tr in trackers:
                msg = tr.get("msg", "").strip()
                status = tr.get("status")
                print(f"    * URL: {tr.get('url')[:60]}...")
                print(f"      Status: {status} | 消息: '{msg}' | 做种数: {tr.get('num_seeds')} | 下载数: {tr.get('num_peers')}")

    if len(hits) > 1:
        print(f"\n⚠️ 警告：检测到该 InfoHash 在 {len(hits)} 个实例同时存在！违反集群单实例铁律（AGENTS.md 第 9 条）！")
    else:
        print(f"\n✅ 校验通过：该 InfoHash 仅存在于单实例，无跨实例双活冲突。")


def scan_keyword(keyword: str) -> None:
    keyword = keyword.strip()
    print(f"\n=======================================================")
    print(f" 正在对全集群 26 实例模糊检索关键词: '{keyword}'")
    print(f"=======================================================")
    matches = []
    for inst_name, port in INSTANCES:
        torrents = qb_get(port, "/api/v2/torrents/info?filter=all")
        if not torrents:
            continue
        for t in torrents:
            name = t.get("name", "")
            if keyword.lower() in name.lower():
                matches.append((inst_name, port, t))

    if not matches:
        print(f"[-] 全集群未找到包含 '{keyword}' 的种子。")
        return

    print(f"[+] 共检索到 {len(matches)} 个匹配任务：\n")
    print(f"{'实例:端口':<18} | {'进度':<7} | {'状态':<12} | {'Hash':<16} | {'名称'}")
    print("-" * 90)
    for inst_name, port, t in matches:
        loc = f"{inst_name}:{port}"
        prog = f"{t.get('progress', 0.0) * 100:.1f}%"
        state = str(t.get("state"))
        h_short = str(t.get("hash"))[:12]
        nm = str(t.get("name"))[:45]
        print(f"{loc:<18} | {prog:<7} | {state:<12} | {h_short:<16} | {nm}")


def pool_summary() -> None:
    print(f"\n=======================================================")
    print(f" 8 大音乐池 (3210~3280) 与下载端 (3016) 运行体检")
    print(f"=======================================================")
    pools = [("musicdownload", 3016)] + [
        (f"music_pool_{i}", 3200 + i * 10) for i in range(1, 9)
    ]
    print(f"{'池实例':<16} | {'总任务':<6} | {'100%做种':<8} | {'下载中':<6} | {'影子配对':<8} | {'非标隔离':<8}")
    print("-" * 75)
    for name, port in pools:
        torrents = qb_get(port, "/api/v2/torrents/info?filter=all")
        if torrents is None:
            print(f"{name:<16} | [连接超时/无法访问]")
            continue
        total = len(torrents)
        complete = sum(1 for t in torrents if t.get("progress", 0) >= 0.9999)
        downloading = sum(1 for t in torrents if t.get("state") in ("downloading", "stalledDL", "forcedDL") and "paired" not in t.get("tags", ""))
        paired = sum(1 for t in torrents if "paired" in t.get("tags", ""))
        mismatch = sum(1 for t in torrents if "reseed-mismatch" in t.get("tags", "") or "issue" in t.get("tags", ""))
        print(f"{name:<16} | {total:<6} | {complete:<8} | {downloading:<6} | {paired:<8} | {mismatch:<8}")


def main():
    parser = argparse.ArgumentParser(description="MusicHub 全集群种子深度穿透巡检工具")
    parser.add_argument("--hash", type=str, help="指定精确 InfoHash 进行 26 实例穿透查重与 Tracker 诊断")
    parser.add_argument("--album", type=str, help="输入专辑名称或关键字进行全集群跨站模糊搜索")
    parser.add_argument("--summary", action="store_true", help="输出 8 大音乐保种池与 3016 的健康概览")

    args = parser.parse_args()
    if not any([args.hash, args.album, args.summary]):
        parser.print_help()
        sys.exit(0)

    if args.summary:
        pool_summary()
    if args.hash:
        scan_hash(args.hash)
    if args.album:
        scan_keyword(args.album)


if __name__ == "__main__":
    main()
