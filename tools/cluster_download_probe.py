# -*- coding: utf-8 -*-
"""
cluster_download_probe.py
-------------------------
全集群 26 实例做种异常与下载中种子状态探针。
能够穿透排查 QUI 聚合界面中显示的"正在下载/非100%"种子，精确定位所在实例、
保存路径、下载进度、tracker 状态与错误信息。
"""

import sys
import argparse
import urllib.request
import json
from collections import Counter


INSTANCES = {
    # 影视
    'movies1': 3020, 'movies2': 3040, 'movies3': 3060, 'movies4': 3080,
    'tv1': 3120, 'tv2': 3140,
    # 音乐 8 大平权池与下载器
    'musicdownload': 3016,
    'music_pool_1': 3210, 'music_pool_2': 3220, 'music_pool_3': 3230, 'music_pool_4': 3240,
    'music_pool_5': 3250, 'music_pool_6': 3260, 'music_pool_7': 3270, 'music_pool_8': 3280,
    # 辅种池
    'reseed1': 3320, 'reseed2': 3330, 'reseed3': 3340, 'reseed4': 3350,
    'reseed5': 3360, 'reseed6': 3370, 'reseed7': 3380,
    # 核心与杂项
    'others': 3180, 'download': 3015, 'publish': 3010, 'vertex': 3005
}


def probe_cluster(host: str = "192.168.0.7", show_details: bool = True):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    
    non_seeding = []
    print(f"[*] Probing {len(INSTANCES)} instances on {host}...")

    for name, port in INSTANCES.items():
        url = f"http://{host}:{port}/api/v2/torrents/info?filter=all"
        try:
            req = urllib.request.Request(url)
            with opener.open(req, timeout=4) as resp:
                torrents = json.loads(resp.read().decode('utf-8'))
                for t in torrents:
                    state = t.get('state', '')
                    progress = t.get('progress', 1.0)
                    # 识别非正常做种状态
                    if 'DL' in state or state in ('downloading', 'metaDL', 'allocating', 'missingFiles', 'error') or (progress < 1.0 and state != 'pausedUP'):
                        t['instance_name'] = name
                        t['instance_port'] = port
                        non_seeding.append(t)
        except Exception as e:
            # 个别实例超时容错
            pass

    print(f"\n[+] Total non-complete / abnormal torrents in cluster: {len(non_seeding)}")
    
    # 按实例与状态分组
    by_inst = {}
    for t in non_seeding:
        inst = t['instance_name']
        by_inst.setdefault(inst, Counter())[t['state']] += 1

    print("\n--- Breakdown by Instance and State ---")
    for inst, st_map in sorted(by_inst.items()):
        print(f"  {inst:15s} ({INSTANCES[inst]}): {dict(st_map)}")

    if show_details and non_seeding:
        print("\n--- Detailed Torrent List (Top 30) ---")
        for idx, t in enumerate(non_seeding[:30], 1):
            name = t.get('name', 'N/A')
            progress_pct = t.get('progress', 0.0) * 100
            print(f"  {idx:2d}. [{t['instance_name']}:{t['instance_port']}] [{t.get('state')}] {progress_pct:.1f}%")
            print(f"      Name: {name}")
            print(f"      Hash: {t.get('hash')}")
            print(f"      Save: {t.get('save_path')}")
            tracker = t.get('tracker')
            if tracker:
                print(f"      Tracker: {tracker}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Probe non-seeding or downloading torrents across 26 cluster instances.")
    parser.add_argument("--host", default="192.168.0.7", help="Target NAS host IP (default: 192.168.0.7)")
    parser.add_argument("--quiet", action="store_true", help="Do not print detailed list")
    args = parser.parse_args()
    probe_cluster(args.host, not args.quiet)
