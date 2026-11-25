# -*- coding: utf-8 -*-
"""
query_pools_status.py
---------------------
快速审计 MusicHub 8 大音乐池（3210~3280 / qbit-music-1~8）任务总数、做种数与状态分布。
可在本机执行，也可在 NAS 宿主机或容器内直接执行。
"""

import sys
import argparse
import urllib.request
import json
from collections import Counter


def query_pools(host: str = "192.168.0.7"):
    ports = [3210, 3220, 3230, 3240, 3250, 3260, 3270, 3280]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    
    results = []
    for idx, p in enumerate(ports, start=1):
        url = f"http://{host}:{p}/api/v2/torrents/info?filter=all"
        try:
            req = urllib.request.Request(url)
            with opener.open(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                states = Counter(t.get('state', 'unknown') for t in data)
                seeding = sum(1 for t in data if t.get('progress') == 1.0)
                missing = sum(1 for t in data if t.get('state') == 'missingFiles')
                results.append({
                    'pool': f"music_pool_{idx}",
                    'port': p,
                    'container': f"qbit-music-{idx}",
                    'total': len(data),
                    'seeding': seeding,
                    'missing': missing,
                    'states': dict(states)
                })
        except Exception as e:
            results.append({
                'pool': f"music_pool_{idx}",
                'port': p,
                'container': f"qbit-music-{idx}",
                'error': str(e)
            })

    print(f"{'Pool':14s} | {'Port':5s} | {'Total':5s} | {'Seeding':7s} | {'Missing':7s} | {'States'}")
    print("-" * 85)
    total_all = 0
    seeding_all = 0
    missing_all = 0
    counts = []

    for r in results:
        if 'error' in r:
            print(f"{r['pool']:14s} | {r['port']} | ERROR: {r['error']}")
            continue
        total_all += r['total']
        seeding_all += r['seeding']
        missing_all += r['missing']
        counts.append(r['total'])
        print(f"{r['pool']:14s} | {r['port']} | {r['total']:5d} | {r['seeding']:7d} | {r['missing']:7d} | {r['states']}")

    print("-" * 85)
    spread = (max(counts) - min(counts)) if counts else 0
    print(f"Total: {total_all} | Seeding: {seeding_all} | Missing: {missing_all} | Max Spread: {spread}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query status across 8 music pools.")
    parser.add_argument("--host", default="192.168.0.7", help="Target NAS host IP or hostname (default: 192.168.0.7)")
    args = parser.parse_args()
    query_pools(args.host)
