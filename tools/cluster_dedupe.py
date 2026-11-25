# -*- coding: utf-8 -*-
"""MusicHub 3.0 音乐矩阵同 infra-hash 单实例审计与冲突收敛 (cluster_dedupe.py)

用途
----
站点回包出现 `You cannot seed the same torrent in the same location from more
than 1 client.` 时，先用本工具把「真重复」与「陈旧占位」分开，再决定动作：

- `--scan`（默认，只读）：全实例拉清单，按 hash 分组分类
    * live_duplicate  同一 hash 在两个实例都 active  -> 必须收敛（我们自己的错）
    * shadow_pair     一份 active + 一份 paused      -> 交棒机制的正常影子，不动
    * all_paused      全部 paused                    -> 只提示
- `--seats [N]`（只读）：采样 tracker 回包，把站点告警分类
    * real_duplicate  该 hash 在集群内确实双活
    * stale_seat      集群内只有一份，站点仍留旧 peer 行（重启换 peer_id 导致）
- `--apply`（写）：仅对 live_duplicate 执行「保留者留下、其余 stop+下架」，
  删除恒为 deleteFiles=false，绝不删数据；并写 CONFLICT/OUT 流水。

安全约束
--------
- 任一实例读取失败 -> 拒绝 --apply（fail-closed），避免"读不到当没有"误删；
- 影子种、music5、3010 的过渡配对一律不参与收敛；
- 默认 dry-run，必须显式 `--apply`。

用法
----
    python musichub/tools/cluster_dedupe.py
    python musichub/tools/cluster_dedupe.py --seats 0
    python musichub/tools/cluster_dedupe.py --apply --fail-on-live
    python musichub/tools/cluster_dedupe.py --scan --only-hash <infohash>
"""

import os
import sys
import json
import argparse
import logging
from typing import Dict, Any, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from musichub.core.config import MusicHubConfig
from musichub.routing.graph import RoutingGraphManager
from musichub.storage.cluster_guard import (
    ClusterDeduper, KIND_LIVE_DUPLICATE, KIND_SHADOW_PAIR, KIND_ALL_PAUSED,
    SEAT_REAL, SEAT_STALE, summarize_scan,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ClusterDedupe")


def print_scan(scan: Dict[str, Any], show_groups: int = 20) -> None:
    print("=" * 104)
    print("实例清单")
    print("-" * 104)
    for name, info in scan["instances"].items():
        err = info.get("error") or ""
        flag = "  " if not err else "!!"
        print(f"  {flag} {name:14} :{info['port']:<5} 任务={info['count']:<6} {err[:60]}")
    print("-" * 104)
    print(f"汇总: 任务总数={scan['total_tasks']} 唯一hash={scan['unique_hashes']} "
          f"重复hash={scan['duplicate_hashes']} | "
          f"双活={scan['by_kind'].get(KIND_LIVE_DUPLICATE, 0)} "
          f"影子(正常)={scan['by_kind'].get(KIND_SHADOW_PAIR, 0)} "
          f"全pause={scan['by_kind'].get(KIND_ALL_PAUSED, 0)}")
    if scan["failed_instances"]:
        print(f"⚠ 读取失败的实例: {scan['failed_instances']}（--apply 会被拒绝）")

    groups = [g for g in scan["groups"] if g["kind"] == KIND_LIVE_DUPLICATE]
    if groups:
        print("-" * 104)
        print(f"双活（违反单实例铁律）明细，最多显示 {show_groups} 条：")
        for g in groups[:show_groups]:
            owners = ", ".join(
                f"{h['pool']}({h['state']},up={h['uploaded']})" for h in g["holders"]
            )
            print(f"  {g['hash'][:12]} {str(g['name'])[:44]:44} <- {owners}")
        if len(groups) > show_groups:
            print(f"  ... 其余 {len(groups) - show_groups} 条省略")
    print("=" * 104)


def print_seats(seats: Dict[str, Any], show: int = 20) -> None:
    print("=" * 104)
    print(f"站点告警分诊: 检查任务={seats['checked']} 命中告警={seats['seat_hits']} "
          f"| 真重复={seats['by_kind'].get(SEAT_REAL, 0)} "
          f"陈旧占位={seats['by_kind'].get(SEAT_STALE, 0)}")
    if seats["by_domain"]:
        top = sorted(seats["by_domain"].items(), key=lambda x: -x[1])[:8]
        print(f"  告警站点分布: {top}")
    for hit in seats["hits"][:show]:
        print(f"  [{hit['kind']:14}] {hit['hash'][:12]} {hit['pool']:14} "
              f"{hit['domain']:22} {str(hit['name'])[:36]}")
    if len(seats["hits"]) > show:
        print(f"  ... 其余 {len(seats['hits']) - show} 条省略")
    if seats["errors"]:
        print(f"  ⚠ 查询异常 {len(seats['errors'])} 条，示例: {seats['errors'][:3]}")
    print("=" * 104)
    if seats["by_kind"].get(SEAT_STALE, 0):
        print("说明: 陈旧占位 = 站点仍保留我们上一轮会话（peer_id）的 peer 行，")
        print("      本集群只有一份任务、本地无从清理，只能等站点清理 TTL；")
        print("      请核对是否发生过容器重启/重建（每次重启都会换 peer_id）。")


def main() -> int:
    parser = argparse.ArgumentParser(description="音乐矩阵同 info-hash 单实例审计/收敛")
    parser.add_argument("--apply", action="store_true", help="对双活执行收敛（默认只读）")
    parser.add_argument("--seats", nargs="?", const="0", default=None, metavar="N",
                        help="采样 tracker 回包做告警分诊，N=每实例最多检查任务数（0=全部）")
    parser.add_argument("--only-hash", action="append", default=[], help="只处理指定 infohash（可重复）")
    parser.add_argument("--include-extra", action="store_true", help="额外纳入 others/reseed1~7 一起查")
    parser.add_argument("--no-publish", action="store_true", help="不纳入 3010 发种端")
    parser.add_argument("--show", type=int, default=20, help="明细最多显示条数")
    parser.add_argument("--json", default="", help="把完整结果写入指定 JSON 文件")
    parser.add_argument("--fail-on-live", action="store_true", help="发现双活时以退出码 2 结束（供定时巡检）")
    args = parser.parse_args()

    cfg = MusicHubConfig.from_env()
    guard = ClusterDeduper(
        host=cfg.publish_qb.host,
        include_publish=not args.no_publish,
        include_extra=args.include_extra,
    )

    scan = guard.scan()
    print_scan(scan, show_groups=args.show)

    seats = None
    if args.seats is not None:
        try:
            limit = int(args.seats or 0)
        except ValueError:
            limit = 0
        seats = guard.probe_seats(
            scan_result=scan,
            per_instance_limit=limit,
            only_hashes=args.only_hash or None,
        )
        print_seats(seats, show=args.show)

    actions: List[Dict[str, Any]] = []
    applied = None
    if args.apply:
        if scan["failed_instances"]:
            logger.error(f"存在读取失败的实例 {scan['failed_instances']}，为安全起见拒绝 --apply")
            return 3
        actions = guard.plan(scan)
        if args.only_hash:
            only = {h.lower() for h in args.only_hash}
            actions = [a for a in actions if a["hash"] in only]
        if not actions:
            print("[apply] 无双活冲突，无需收敛。")
        else:
            print(f"[apply] 计划收敛 {len(actions)} 条双活（删除恒为 deleteFiles=false）")
            graph = None
            try:
                graph = RoutingGraphManager(db_path=cfg.db_path)
            except Exception as e:
                logger.warning(f"打开 musichub.db 失败，本次不写流水: {e}")
            applied = guard.apply(actions, graph_manager=graph)
            print(f"[apply] 成功 {applied['ok']}/{applied['total']} 条；失败 {len(applied['failed'])} 条")
            for d in applied["failed"]:
                print(f"  !! {d['hash'][:12]} {d['errors']}")

    if args.json:
        payload = {"scan_summary": summarize_scan(scan),
                   "scan": {k: v for k, v in scan.items() if k not in ("snapshots", "holders_index")},
                   "seats": seats, "plan": actions, "applied": applied}
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print(f"[json] 结果已写入 {args.json}")

    live = scan["by_kind"].get(KIND_LIVE_DUPLICATE, 0)
    if args.apply and applied and applied["failed"]:
        return 1
    if args.fail_on_live and live:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
