# -*- coding: utf-8 -*-
"""
auto_heal_pools.py
------------------
MusicHub 8 大音乐池做种文件跨池硬链接补齐与自动 recheck 修复工具。
当某个池中的做种任务因历史跨池或者数据缺失报 missingFiles 或进度不足 100% 时，
本工具在其余 7 个池的物理路径中搜索同名专辑/文件，通过安全硬链接（同属 volume1）补齐缺失文件，
并自动对 qB 实例发送 /api/v2/torrents/recheck 触发重新校验，秒级自愈。

注意：本脚本操作物理文件系统硬链接，需在具有 /volume1/video/download/music 访问权限的环境（如 NAS 宿主或 musichub 容器）执行。
"""

import os
import sys
import argparse
import urllib.request
import json


def heal_pools(base_dir: str = "/volume1/video/download/music", host: str = "127.0.0.1", dry_run: bool = False):
    pools = [f"music_pool_{i}" for i in range(1, 9)]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    print(f"[*] Scanning missing files across 8 music pools on {host}...")
    all_missing = []
    for idx, pool in enumerate(pools, start=1):
        port = 3200 + idx * 10
        url = f"http://{host}:{port}/api/v2/torrents/info?filter=all"
        try:
            req = urllib.request.Request(url)
            with opener.open(req, timeout=8) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                for t in data:
                    if t.get('state') == 'missingFiles' or (t.get('progress', 1.0) < 1.0 and 'DL' in t.get('state', '')):
                        all_missing.append({
                            'pool': pool,
                            'port': port,
                            'hash': t.get('hash'),
                            'name': t.get('name'),
                            'state': t.get('state')
                        })
        except Exception as e:
            print(f"[-] Failed to query {pool} ({port}): {e}")

    print(f"[*] Found {len(all_missing)} torrents with missing files or stalledDL.")
    if not all_missing:
        print("[+] All pools are 100% healthy! No healing needed.")
        return

    healed = 0
    for item in all_missing:
        target_pool = item['pool']
        name = item['name']
        target_path = os.path.join(base_dir, target_pool, name)

        # 查找来源
        src_found = None
        for p in pools:
            if p == target_pool:
                continue
            candidate = os.path.join(base_dir, p, name)
            if os.path.exists(candidate):
                src_found = candidate
                break

        if not src_found:
            print(f"[-] Source not found in any other pool for: {name} ({item['hash'][:10]})")
            continue

        print(f"[+] Found source for '{name}' in {src_found}")
        if dry_run:
            print(f"    [DRY-RUN] Would hardlink to: {target_path}")
            print(f"    [DRY-RUN] Would trigger recheck on port {item['port']}")
            healed += 1
            continue

        # 递归补齐硬链接
        try:
            if os.path.isdir(src_found):
                os.makedirs(target_path, exist_ok=True)
                for root, _, files in os.walk(src_found):
                    rel = os.path.relpath(root, src_found)
                    dst_sub = os.path.join(target_path, rel) if rel != "." else target_path
                    os.makedirs(dst_sub, exist_ok=True)
                    for f in files:
                        sf = os.path.join(root, f)
                        df = os.path.join(dst_sub, f)
                        if not os.path.exists(df):
                            try:
                                os.link(sf, df)
                            except Exception as le:
                                print(f"    [!] Link err {f}: {le}")
            else:
                if not os.path.exists(target_path):
                    os.link(src_found, target_path)

            # 触发 qB recheck
            recheck_url = f"http://{host}:{item['port']}/api/v2/torrents/recheck"
            post_data = f"hashes={item['hash']}".encode('utf-8')
            req = urllib.request.Request(recheck_url, data=post_data)
            with opener.open(req, timeout=5) as _:
                pass
            print(f"    [OK] Linked files and triggered recheck on port {item['port']} for {item['hash'][:10]}")
            healed += 1
        except Exception as e:
            print(f"    [!] Error healing {name}: {e}")

    print(f"\n[*] Healing complete: {healed} / {len(all_missing)} processed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-heal missing files across music pools via hardlinks.")
    parser.add_argument("--base-dir", default="/volume1/video/download/music", help="Base directory of music pools")
    parser.add_argument("--host", default="127.0.0.1", help="qB host IP (default: 127.0.0.1)")
    parser.add_argument("--dry-run", action="store_true", help="Perform a trial run with no changes made")
    args = parser.parse_args()
    heal_pools(args.base_dir, args.host, args.dry_run)
