# -*- coding: utf-8 -*-
"""MusicHub 3.0 生命周期退役与多站做种审查工具 (RetireMatrix)

功能：
1. --status：全景审查全库专辑的生命周期与门禁状态（已达标、时间保护期、矩阵扩散保护期、源站保护期）
2. --candidates：列出当前满足“源种满14天 + 最晚发布站点满10天 + 矩阵扩散充分”的退役候选
3. --dry-run：模拟下架脱退与跨池 GC 回收空间估算
4. --apply：受控执行指定批次脱退（delete_files=False 铁律）
"""

import os
import sys
from pathlib import Path

# 确保项目根路径进入 sys.path
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
_mh = Path(__file__).resolve().parent.parent
if str(_mh) not in sys.path:
    sys.path.insert(0, str(_mh))

import argparse
import logging
from typing import Dict, Any

from musichub.routing.graph import RoutingGraphManager
from musichub.storage.safe_gc import SafeGarbageCollector
from musichub.storage.lifecycle import LifecycleRetirement
from musichub.core.qb_client import QBClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("RetireMatrix")


def get_default_clients() -> Dict[str, QBClient]:
    """根据集群拓扑构建 8 个音乐池 + 3010 发种机客户端连接"""
    host = "192.168.0.7"
    clients = {
        "publish": QBClient(host, 3010),
        "music_pool_1": QBClient(host, 3210),
        "music1": QBClient(host, 3220),
        "music_pool_2": QBClient(host, 3230),
        "music2": QBClient(host, 3240),
        "music_pool_3": QBClient(host, 3250),
        "music3": QBClient(host, 3260),
        "music_pool_4": QBClient(host, 3270),
        "music_pool_5": QBClient(host, 3280),
    }
    return clients


def main():
    parser = argparse.ArgumentParser(description="MusicHub 3.0 14+10 Days Lifecycle Retirement CLI")
    parser.add_argument("--db", default=r"d:\Projects\PT-Project\musichub\data\musichub.db", help="Path to musichub.db")
    parser.add_argument("--status", action="store_true", help="Show full lifecycle distribution status")
    parser.add_argument("--candidates", action="store_true", help="List retireable candidates meeting 14+10d & saturation")
    parser.add_argument("--min-source-days", type=float, default=14.0, help="Minimum days for source/first publish (default 14)")
    parser.add_argument("--min-latest-days", type=float, default=10.0, help="Minimum days for latest published site (default 10)")
    parser.add_argument("--limit", type=int, default=20, help="Limit number of output rows")
    parser.add_argument("--apply", action="store_true", help="Apply controlled retirement (detach from qB with delete_files=False)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate retirement process")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        logger.error(f"Database not found at: {args.db}")
        sys.exit(1)

    graph_mgr = RoutingGraphManager(db_path=args.db)
    safe_gc = SafeGarbageCollector()
    clients = get_default_clients()

    retirement = LifecycleRetirement(
        graph_mgr=graph_mgr,
        safe_gc=safe_gc,
        target_clients=clients,
        min_source_days=args.min_source_days,
        min_latest_days=args.min_latest_days
    )

    if args.status or (not args.candidates and not args.apply and not args.dry-run):
        print("\n=======================================================")
        print("   MusicHub 3.0 机械盘生命周期与退役状态全景审查")
        print("=======================================================")
        overview = retirement.get_lifecycle_status_overview()
        total = overview["total_albums"]
        retireable = overview["retireable_count"]
        p_time = overview["protected_time_count"]
        p_exp = overview["protected_expansion_count"]
        p_src = overview["protected_source_count"]

        print(f"全库专辑总数 (去重指纹): {total}")
        print(f"🟢 [可安全退役] RETIREABLE:             {retireable:4d} 个 ({retireable/total*100:.1f}%) -> 源种>=14天 且 最晚站>=10天 且 矩阵充分扩散")
        print(f"🟡 [各站保护期] PROTECTED_TIME:         {p_time:4d} 个 ({p_time/total*100:.1f}%) -> 存在站点发布 < 10天 (最新站做种保护)")
        print(f"🔵 [矩阵扩散期] PROTECTED_EXPANSION:    {p_exp:4d} 个 ({p_exp/total*100:.1f}%) -> 尚有适格站点排队未发 (尽可能多发保护)")
        print(f"🔴 [源站保护期] PROTECTED_SOURCE:       {p_src:4d} 个 ({p_src/total*100:.1f}%) -> 源种/总周期 < 14天")
        print("-------------------------------------------------------")

        if overview["retireable_samples"]:
            print(f"\n【可安全退役专辑样例 (前 {len(overview['retireable_samples'])} 个)】:")
            for s in overview["retireable_samples"][:5]:
                print(f"  * 指纹: {s['content_fingerprint'][:16]}... | 源站: {s['origin_site']}:{s['origin_torrent_id']}")
                print(f"    - 已发站点 ({s['site_count']}): {','.join(s['published_sites'])}")
                print(f"    - 源种发布: {s['first_published_at']} ({s['src_age_days']} 天前)")
                print(f"    - 最晚发布: {s['last_published_at']} ({s['latest_age_days']} 天前)")

        if overview["protected_time_samples"]:
            print(f"\n【各站保护期样例 (最晚发布 < 10 天，绝不抽水断种)】:")
            for s in overview["protected_time_samples"][:3]:
                print(f"  * 指纹: {s['content_fingerprint'][:16]}... | 已发 {s['site_count']} 站 | 最晚发布: {s['last_published_at']} ({s['latest_age_days']} 天前 < 10天)")

        if overview["protected_expansion_samples"]:
            print(f"\n【矩阵扩散保护期样例 (尚有适格目标站未发，继续保留物理数据)】:")
            for s in overview["protected_expansion_samples"][:3]:
                print(f"  * 指纹: {s['content_fingerprint'][:16]}... | 已发: {','.join(s['published_sites'])}")
                print(f"    - 待扩散站点: {','.join(s['remaining_sites'])}")
        print("=======================================================\n")

    if args.candidates or args.dry_run or args.apply:
        candidates = retirement.find_retireable_candidates(
            min_source_days=args.min_source_days,
            min_latest_days=args.min_latest_days,
            require_saturation=True,
            limit=args.limit
        )
        print(f"\n[Retire Candidates] 找到 {len(candidates)} 个满足全部门禁的候选专辑 (Limit={args.limit}):")
        for i, c in enumerate(candidates, 1):
            print(f"[{i:02d}] FP: {c['content_fingerprint'][:16]} | 站点数: {c['site_count']} | 最晚发布: {c['last_published_at']} | 饱和状态: {c['saturation_status']}")

        if args.dry_run:
            print("\n[DRY-RUN 模拟] 验证跨集群脱退（仅模拟，不发下架指令）：")
            for c in candidates[:3]:
                res = retirement.retire_album_across_clients(c["content_fingerprint"], dry_run=True)
                print(f"  - FP {c['content_fingerprint'][:12]}: {res}")

        if args.apply:
            confirm = input(f"\n⚠️ 确定要对上述 {len(candidates)} 个候选专辑执行跨池脱退做种任务吗？(delete_files=False, y/N): ")
            if confirm.strip().lower() == "y":
                for c in candidates:
                    res = retirement.retire_album_across_clients(c["content_fingerprint"], dry_run=False)
                    logger.info(f"Retire execution for {c['content_fingerprint'][:16]}: {res}")
                print("脱退任务执行完毕，后续由 SafeGarbageCollector 跨池 GC 回收物理空间。")
            else:
                print("操作已取消。")


if __name__ == "__main__":
    main()
