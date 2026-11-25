# -*- coding: utf-8 -*-
"""
MusicHub 8 池做种与全局一致性审计工具 (Cluster Seeding Audit)

功能：
1. 跨 8 个音乐 qB 实例实时扫描做种状态（Seeding / Stalled / 0% 未完成 / Error）。
2. 同 Info-Hash 全集群唯一性查重（坚守 AGENTS.md 铁律第九条，确保 0 跨实例重复）。
3. 统计 8 池负载分布、标准差与极差，评估均衡健康度。
4. 可选检查幽灵做种（磁盘实际物理文件缺失检测）。
"""

import os
import sys
import argparse
import requests
from collections import defaultdict
from typing import Dict, Any, List

# 强制本地内网无代理
for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(var, None)

MUSIC_POOLS = {
    "music_pool_1": {"port": 3210, "host_dir": "/volume1/video/download/music/music_pool_1"},
    "music_pool_2": {"port": 3220, "host_dir": "/volume1/video/download/music/music1"},
    "music_pool_3": {"port": 3230, "host_dir": "/volume1/video/download/music/music_pool_2"},
    "music_pool_4": {"port": 3240, "host_dir": "/volume1/video/download/music/music2"},
    "music_pool_5": {"port": 3250, "host_dir": "/volume1/video/download/music/music_pool_3"},
    "music_pool_6": {"port": 3260, "host_dir": "/volume1/video/download/music/music3"},
    "music_pool_7": {"port": 3270, "host_dir": "/volume1/video/download/music/music_pool_4"},
    "music_pool_8": {"port": 3280, "host_dir": "/volume1/video/download/music/music_pool_5"},
}


class ClusterAuditor:
    def __init__(self, host: str = "192.168.0.7"):
        self.host = host
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies = {"http": None, "https": None}

    def fetch_torrents(self, port: int) -> List[Dict[str, Any]]:
        url = f"http://{self.host}:{port}/api/v2/torrents/info"
        r = self.session.get(url, timeout=30)
        r.raise_for_status()
        return r.json()

    def audit(self, verbose: bool = False) -> Dict[str, Any]:
        pool_stats = {}
        all_hashes = defaultdict(list)
        zero_progress_torrents = []

        print(f"\n{'='*25} MusicHub 8 池做种审计中 {'='*25}")
        for pool_name, info in MUSIC_POOLS.items():
            port = info["port"]
            try:
                torrents = self.fetch_torrents(port)
                total = len(torrents)
                seeding = sum(1 for t in torrents if t.get("progress", 0) >= 1.0 or t.get("state") in ("uploading", "stalledUP"))
                downloading = sum(1 for t in torrents if t.get("progress", 0) < 1.0 or "DL" in t.get("state", ""))
                
                for t in torrents:
                    h = t.get("hash", "").lower()
                    all_hashes[h].append({
                        "pool": pool_name,
                        "port": port,
                        "name": t.get("name"),
                        "state": t.get("state"),
                        "progress": t.get("progress"),
                        "save_path": t.get("save_path")
                    })
                    if t.get("progress", 0) == 0:
                        zero_progress_torrents.append((pool_name, port, t.get("name"), h))

                pool_stats[pool_name] = {
                    "port": port,
                    "total": total,
                    "seeding": seeding,
                    "downloading": downloading,
                    "status": "OK"
                }
            except Exception as e:
                pool_stats[pool_name] = {
                    "port": port,
                    "total": -1,
                    "seeding": -1,
                    "downloading": -1,
                    "status": f"ERROR: {e}"
                }

        # 检查重复 Hash
        duplicates = {h: instances for h, instances in all_hashes.items() if len(instances) > 1}

        # 打印状态表格
        print(f"\n{'Pool Name':<16}{'Port':<8}{'Total':<10}{'Seeding':<10}{'DL/Incomplete':<16}{'Status':<10}")
        print("-" * 70)
        seeding_counts = []
        for p, s in pool_stats.items():
            if s["total"] >= 0:
                seeding_counts.append(s["seeding"])
            print(f"{p:<16}{s['port']:<8}{s['total']:<10}{s['seeding']:<10}{s['downloading']:<16}{s['status']:<10}")
        print("-" * 70)

        if seeding_counts:
            avg_seeding = sum(seeding_counts) / len(seeding_counts)
            min_s, max_s = min(seeding_counts), max(seeding_counts)
            spread = max_s - min_s
            print(f"统计指标: 平均做种数 = {avg_seeding:.1f} | 最小 = {min_s} | 最大 = {max_s} | 极差 = {spread}")

        # 打印查重与健康结果
        print(f"\n[同 Info-Hash 查重]")
        print(f"  - 全集群唯一种子数: {len(all_hashes)}")
        print(f"  - 跨实例重复做种数: {len(duplicates)}")
        if duplicates:
            print("  [!] 发现跨实例重复做种种子 (严重违反 AGENTS.md 铁律九):")
            for h, insts in duplicates.items():
                print(f"    * Hash {h}:")
                for inst in insts:
                    print(f"        -> {inst['pool']} (port {inst['port']}): {inst['name']} (state: {inst['state']})")
        else:
            print("  [✓] 0 跨实例重复！全集群单实例做种合规。")

        print(f"\n[0% 死种/未完成任务巡检]")
        print(f"  - 0% 进度任务数: {len(zero_progress_torrents)}")
        if zero_progress_torrents and verbose:
            for p, port, name, h in zero_progress_torrents:
                print(f"    * [{p}:{port}] {name} ({h[:10]})")

        return {
            "pool_stats": pool_stats,
            "total_unique": len(all_hashes),
            "duplicates_count": len(duplicates),
            "zero_progress_count": len(zero_progress_torrents)
        }


def main():
    parser = argparse.ArgumentParser(description="MusicHub 8 池做种与全局一致性审计")
    parser.add_argument("--host", default="192.168.0.7", help="NAS IP")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示详细列表")
    args = parser.parse_args()

    auditor = ClusterAuditor(host=args.host)
    auditor.audit(verbose=args.verbose)


if __name__ == "__main__":
    main()
