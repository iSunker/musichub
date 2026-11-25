#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# cluster_tracker_audit.py - qBittorrent 全集群 0-Tracker 哑种巡检与批量修复工具
#
# 背景与原理:
#   1. qB 的 BT_backup/<hash>.torrent 不包含 announce URL（保存在 fastresume 中）。
#   2. 跨实例注入 (add) 后若立即下发 addTrackers，由于 qB 种子异步初始化未就绪，
#      可能会被 qB 静默丢弃，导致目标实例出现「无 Tracker (0-Tracker)」私有哑种。
#   3. 本脚本提供全集群 26 实例巡检、与下载分流日志 (dispatch.log) 自动溯源比对、
#      以及批量安全补全与回读校验功能。
#
# 用法:
#   1) 纯巡检扫描（只读，默认打印全集群 0-Tracker 清单与分布）:
#        python QUI/scripts/cluster_tracker_audit.py --scan
#   2) 指定实例巡检（如 3220）:
#        python QUI/scripts/cluster_tracker_audit.py --port 3220
#   3) 干跑模式（显示将要修复的任务与 Tracker 映射，不发起写请求）:
#        python QUI/scripts/cluster_tracker_audit.py --repair --dry-run
#   4) 执行批量安全修复（带重试与回读校验）:
#        python QUI/scripts/cluster_tracker_audit.py --repair
# ==============================================================================

import argparse
import base64
import json
import logging
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("cluster_tracker_audit")

# 直连 opener（规避本机环境变量中的 HTTP 代理拦截 192.168.0.7）
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

NAS_HOST = "192.168.0.7"
NAS_SSH_PORT = 36688
NAS_SSH_USER = "iSunker"
NAS_DISPATCH_LOG = "/volume2/docker_ssd/qbit-all/data/download/scripts/dispatch.log"

INSTANCES = {
    # 业务与下载端
    "publish": 3010,
    "download": 3015,
    "musicdownload": 3016,
    "vertex": 3005,
    "others": 3180,
    # 电影库
    "movies1": 3020,
    "movies2": 3040,
    "movies3": 3060,
    "movies4": 3080,
    # 剧集库
    "tv1": 3120,
    "tv2": 3140,
    # 8 音乐平权保种网格
    "music_pool_1": 3210,
    "music_pool_2": 3220,
    "music_pool_3": 3230,
    "music_pool_4": 3240,
    "music_pool_5": 3250,
    "music_pool_6": 3260,
    "music_pool_7": 3270,
    "music_pool_8": 3280,
    # 7 辅种保种池
    "reseed1": 3320,
    "reseed2": 3330,
    "reseed3": 3340,
    "reseed4": 3350,
    "reseed5": 3360,
    "reseed6": 3370,
    "reseed7": 3380,
}

def get_torrents(port: int, host: str = NAS_HOST) -> Optional[List[dict]]:
    """读取指定实例全部种子"""
    url = f"http://{host}:{port}/api/v2/torrents/info"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ClusterTrackerAudit/1.0"})
        with opener.open(req, timeout=4) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        logger.warning(f"无法连接实例 {host}:{port}: {e}")
        return None

def get_real_trackers(port: int, infohash: str, host: str = NAS_HOST) -> List[str]:
    """读取种子真实 Tracker announce 列表（排除 DHT/PeX/LSD 等伪 tracker）"""
    url = f"http://{host}:{port}/api/v2/torrents/trackers?hash={infohash}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ClusterTrackerAudit/1.0"})
        with opener.open(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            return [
                x["url"].strip() for x in data
                if x.get("url") and not x["url"].strip().startswith("** [")
            ]
    except Exception:
        return []

def add_trackers(port: int, infohash: str, urls: List[str], host: str = NAS_HOST) -> bool:
    """向指定实例种子补全 Announce URL"""
    url = f"http://{host}:{port}/api/v2/torrents/addTrackers"
    data = urllib.parse.urlencode({"hash": infohash, "urls": "\n".join(urls)}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "ClusterTrackerAudit/1.0"})
    try:
        with opener.open(req, timeout=5) as resp:
            return True
    except Exception as e:
        logger.error(f"向 {host}:{port} ({infohash[:10]}) 添加 tracker 失败: {e}")
        return False

def resume_torrent(port: int, infohash: str, host: str = NAS_HOST) -> None:
    """唤醒种子以触发 Tracker Announce 握手"""
    url = f"http://{host}:{port}/api/v2/torrents/start"
    data = urllib.parse.urlencode({"hashes": infohash}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "ClusterTrackerAudit/1.0"})
    try:
        with opener.open(req, timeout=3) as resp:
            pass
    except Exception:
        pass

def fetch_dispatch_map_from_nas() -> Dict[str, str]:
    """通过 SSH 从 NAS 读取 dispatch.log，解析 infohash -> tracker announce URL 映射表"""
    remote_py = f"""
import re, json
results = {{}}
try:
    with open('{NAS_DISPATCH_LOG}', 'r', encoding='utf-8', errors='ignore') as f:
        curr_h = None
        for line in f:
            m_h = re.search(r'Hash:\\s+([a-fA-F0-9]{{40}})', line)
            if m_h:
                curr_h = m_h.group(1).lower()
            m_t = re.search(r'Tracker:\\s+(https?://\\S+)', line)
            if m_t and curr_h:
                results[curr_h] = m_t.group(1).strip()
    print(json.dumps(results))
except Exception as e:
    print(json.dumps({{}}))
"""
    b64_code = base64.b64encode(remote_py.encode("utf-8")).decode("ascii")
    ssh_cmd = f"echo '{b64_code}' | base64 -d | python3"
    try:
        res = subprocess.run(
            ["ssh", "-p", str(NAS_SSH_PORT), f"{NAS_SSH_USER}@{NAS_HOST}", ssh_cmd],
            capture_output=True, text=True, timeout=20
        )
        if res.stdout:
            data = json.loads(res.stdout.strip())
            logger.info(f"成功从 NAS 加载 {len(data)} 条 dispatch 历史溯源映射")
            return data
    except Exception as e:
        logger.warning(f"读取 NAS dispatch.log 异常: {e}")
    return {}

def scan_cluster(target_instances: Dict[str, int]) -> Tuple[Dict[str, List[dict]], int]:
    """扫描目标实例列表，提取所有 0-Tracker 哑种"""
    zero_map = {}
    total_torrents = 0
    for name, port in target_instances.items():
        torrents = get_torrents(port)
        if torrents is None:
            continue
        total_torrents += len(torrents)
        zero_list = []
        for t in torrents:
            # 种子列表中的 tracker 字段为空，进一步调用 API 确认真实 announce 列表
            if not t.get("tracker", "").strip():
                h = t["hash"].lower()
                real_trks = get_real_trackers(port, h)
                if not real_trks:
                    zero_list.append({
                        "hash": h,
                        "name": t.get("name", "Unknown"),
                        "category": t.get("category", ""),
                        "tags": t.get("tags", ""),
                        "size": t.get("size", 0),
                        "added_on": t.get("added_on", 0),
                    })
        if zero_list:
            zero_map[name] = zero_list
    return zero_map, total_torrents

def main():
    parser = argparse.ArgumentParser(description="qBittorrent 全集群 0-Tracker 哑种巡检与批量修复工具")
    parser.add_argument("--scan", action="store_true", default=True, help="扫描并展示 0-Tracker 种子（默认）")
    parser.add_argument("--repair", action="store_true", help="根据 dispatch.log 执行批量补全修复")
    parser.add_argument("--dry-run", action="store_true", help="干跑演练（不实际调用 addTrackers）")
    parser.add_argument("--port", type=int, help="仅针对指定 WebUI 端口的实例执行（如 3220）")
    args = parser.parse_args()

    targets = INSTANCES
    if args.port:
        matched = {k: v for k, v in INSTANCES.items() if v == args.port}
        if not matched:
            targets = {f"custom_{args.port}": args.port}
        else:
            targets = matched

    logger.info("=" * 70)
    logger.info(f"开始扫描目标实例集合: {list(targets.keys())}")
    zero_map, total_count = scan_cluster(targets)
    total_zero = sum(len(lst) for lst in zero_map.values())
    logger.info(f"扫描完毕！纳管种子总数: {total_count}，发现 0-Tracker 哑种: {total_zero} 个")

    if not total_zero:
        logger.info("全集群状态极其健康，未发现任何 0-Tracker 哑种！")
        return

    # 展示分布
    for name, lst in zero_map.items():
        logger.info(f"  实例 [{name:<14} :{INSTANCES.get(name, 'custom')}]: {len(lst)} 个哑种")
        for item in lst[:3]:
            logger.info(f"    - {item['hash'][:10]}... | {item['category']:<8} | {item['name'][:40]}")
        if len(lst) > 3:
            logger.info(f"    ... 以及其余 {len(lst)-3} 个任务")

    if not args.repair:
        logger.info("-" * 70)
        logger.info("提示: 传入 --repair 参数即可自动从 dispatch.log 溯源并安全补全上述哑种。")
        return

    # 执行修复流程
    logger.info("=" * 70)
    logger.info("正在加载溯源数据准备修复...")
    dispatch_map = fetch_dispatch_map_from_nas()

    mappable_count = 0
    unmappable_count = 0
    for lst in zero_map.values():
        for item in lst:
            if item["hash"] in dispatch_map:
                mappable_count += 1
            else:
                unmappable_count += 1

    logger.info(f"溯源分析: 可自动修复 {mappable_count} 个，无法溯源（历史旧种） {unmappable_count} 个")

    if args.dry_run:
        logger.info("[DRY-RUN] 干跑模式：以下任务将被注入修复:")
        for name, lst in zero_map.items():
            port = targets[name]
            for item in lst:
                h = item["hash"]
                if h in dispatch_map:
                    logger.info(f"  [WILL-FIX] {name}:{port} -> {h[:10]} -> {dispatch_map[h][:50]}...")
        return

    # 正式注入与校验
    success_total = 0
    fail_total = 0
    for name, lst in zero_map.items():
        port = targets[name]
        logger.info(f"开始修复实例 [{name}:{port}]...")
        for item in lst:
            h = item["hash"]
            if h not in dispatch_map:
                continue
            trk_url = dispatch_map[h]
            # 补加 Tracker
            add_trackers(port, h, [trk_url])
            time.sleep(1.0)
            verified = get_real_trackers(port, h)
            if not verified:
                # 重试一次
                time.sleep(1.2)
                add_trackers(port, h, [trk_url])
                time.sleep(1.2)
                verified = get_real_trackers(port, h)

            if verified:
                resume_torrent(port, h)
                success_total += 1
                logger.info(f"  [SUCCESS] {h[:10]} ({item['name'][:30]}) -> {len(verified)} 真实 Tracker")
            else:
                fail_total += 1
                logger.error(f"  [FAILED] {h[:10]} ({item['name'][:30]}) 注入后回读仍为 0")

    logger.info("=" * 70)
    logger.info(f"批量修复结束！成功校验: {success_total}，失败: {fail_total}")

if __name__ == "__main__":
    main()
