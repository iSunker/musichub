# -*- coding: utf-8 -*-
"""MusicHub 3.0 只读审查与健康巡检工具 (audit_probe.py)

阶段式渐进演进（方案 C）：
作为只读后台探针工具，执行：
1. NAS 机械盘与固态盘物理剩余空间与动态背压（Watermark）评估
2. 3016 (musicdownload) 与 3010 (publish) 在线连通性与活跃任务数核验
3. 8 大 PT 站点 CookieCloud 登录凭据存活状态诊断
4. musichub.db 历史溯源记录与防环索引对账统计
5. 音乐受控池 (music1~3) 孤儿候选与软链接安全健康度检测（纯只读，绝不修改与删除任何文件）
"""

import os
import sys
import json
import logging
from typing import Dict, Any

# 确保加载根路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from musichub.core.config import MusicHubConfig
from musichub.core.qb_client import QBClient
from musichub.storage.backpressure import StorageBackpressure
from musichub.storage.safe_gc import SafeGarbageCollector
from musichub.routing.graph import RoutingGraphManager
from musichub.adapters.sites import (
    MusopiaAdapter, CrabPTAdapter, PTerClubAdapter,
    XingwanAdapter, HDKylinAdapter, TangPTAdapter, MuXueGeAdapter,
    OpenCDAdapter
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MusicHubAuditProbe")


def run_comprehensive_audit(config: MusicHubConfig) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "storage": {},
        "qb_instances": {},
        "sites_status": {},
        "graph_db": {},
        "orphan_preview": {}
    }

    print("=" * 60)
    print("      MusicHub 3.0 系统健康度与只读审计报告 (Audit Probe)     ")
    print("=" * 60)

    # 1. 存储与动态背压
    bp = StorageBackpressure(
        mount_path=config.storage.disk_mount_path,
        green_gb=config.storage.watermark_green_gb,
        yellow_gb=config.storage.watermark_yellow_gb,
        red_gb=config.storage.watermark_red_gb,
    )
    level, free_gb = bp.assess_watermark()
    report["storage"] = {
        "mount": config.storage.disk_mount_path,
        "free_gb": round(free_gb, 2),
        "watermark_level": level,
        "green_threshold": config.storage.watermark_green_gb,
        "yellow_threshold": config.storage.watermark_yellow_gb,
        "red_threshold": config.storage.watermark_red_gb,
    }
    print(f"[*] 存储水位: {level} (可用空间: {free_gb:.1f} GB, 挂载: {config.storage.disk_mount_path})")

    # 2. qB 实例健康度
    qb_down = QBClient(
        host=config.musicdownload_qb.host,
        port=config.musicdownload_qb.port,
        username=config.musicdownload_qb.username,
        password=config.musicdownload_qb.password,
    )
    qb_pub = QBClient(
        host=config.publish_qb.host,
        port=config.publish_qb.port,
        username=config.publish_qb.username,
        password=config.publish_qb.password,
    )

    t_down = qb_down.get_torrents()
    down_online = t_down is not None
    down_count = len(t_down) if down_online else 0
    t_pub = qb_pub.get_torrents()
    pub_online = t_pub is not None
    pub_count = len(t_pub) if pub_online else 0

    report["qb_instances"] = {
        "musicdownload_3016": {"online": down_online, "torrent_count": down_count},
        "publish_3010": {"online": pub_online, "torrent_count": pub_count},
    }
    print(f"[*] qB 3016 (musicdownload): {'ONLINE' if down_online else 'OFFLINE'} (任务数: {down_count})")
    print(f"[*] qB 3010 (publish):        {'ONLINE' if pub_online else 'OFFLINE'} (任务数: {pub_count})")

    # 3. 站点登录态诊断
    adapters = [
        MusopiaAdapter(), CrabPTAdapter(), PTerClubAdapter(),
        XingwanAdapter(), HDKylinAdapter(), TangPTAdapter(),
        MuXueGeAdapter(), OpenCDAdapter()
    ]
    for adp in adapters:
        cookies = adp.get_auth_cookies()
        has_cookie = bool(cookies)
        report["sites_status"][adp.site_name] = {
            "domain": adp.domain,
            "has_credentials": has_cookie,
            "cookie_keys": list(cookies.keys()) if has_cookie else []
        }
        print(f"[*] 站点 {adp.site_name:10} ({adp.domain}): {'CREDENTIALS_OK' if has_cookie else 'NO_COOKIES'}")

    # 4. 溯源数据库统计
    graph = RoutingGraphManager(db_path=config.db_path)
    with graph._lock:
        cursor = graph._conn.execute("SELECT target_site, count(*) FROM site_publish_graph GROUP BY target_site")
        counts = {row[0]: row[1] for row in cursor.fetchall()}
        total = sum(counts.values())

    report["graph_db"] = {
        "db_path": config.db_path,
        "total_published_records": total,
        "by_site": counts
    }
    print(f"[*] 溯源数据库 (musichub.db): 总计 {total} 条发布溯源记录")
    for s, c in sorted(counts.items()):
        print(f"    - {s:10}: {c} 条")

    # 5. 受控保种池孤儿预览（纯只读）
    gc = SafeGarbageCollector(managed_pools=config.storage.library_pools)
    active_torrents = set()
    if t_pub:
        for t in t_pub:
            name = t.get("name")
            if name:
                active_torrents.add(name)

    orphans = gc.scan_unreferenced_orphans(active_torrents)
    report["orphan_preview"] = {
        "orphan_count": len(orphans),
        "sample_orphans": [o["name"] for o in orphans[:5]]
    }
    print(f"[*] 孤儿数据只读扫描: 发现 {len(orphans)} 个无引用候选项目")
    if orphans:
        print(f"    前 3 个示例: {[o['name'] for o in orphans[:3]]}")
    print("=" * 60)

    return report


if __name__ == "__main__":
    cfg = MusicHubConfig.from_env()
    run_comprehensive_audit(cfg)
