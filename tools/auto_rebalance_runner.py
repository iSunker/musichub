# -*- coding: utf-8 -*-
import os
import sys
import time
import logging

# 强制本地内网无代理
for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(var, None)

sys.path.insert(0, r"d:\Projects\PT-Project")
from musichub.tools.consolidate_and_rebalance import Consolidator, POOL_SPECS, QBInstanceAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(r"d:\Projects\PT-Project\scratch\auto_rebalance.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("AutoRebalance")

def get_cluster_status():
    status = {}
    for p, spec in POOL_SPECS.items():
        try:
            api = QBInstanceAPI(spec["port"])
            torrents = api.get_torrents()
            seeding = sum(1 for t in torrents if t.get("progress", 0) >= 1.0 or t.get("state") in ("uploading", "stalledUP"))
            status[p] = {"port": spec["port"], "total": len(torrents), "seeding": seeding}
        except Exception as e:
            status[p] = {"port": spec["port"], "total": -1, "seeding": -1, "error": str(e)}
    return status

def print_cluster_status(status, round_num=0):
    logger.info(f"\n{'='*20} 批次 #{round_num} 集群做种状态 {'='*20}")
    logger.info(f"{'Pool':<16}{'Port':<7}{'Total Torrents':<16}{'Seeding':<10}")
    logger.info("-" * 50)
    for p, s in status.items():
        logger.info(f"{p:<16}{s['port']:<7}{s['total']:<16}{s['seeding']:<10}")
    logger.info("=" * 55 + "\n")

def main():
    scratch = r"C:\Users\艾森克\.gemini\antigravity\brain\1b0e9463-8379-46cf-b67b-db69c41dcb53\scratch"
    plan_p = os.path.join(scratch, "album_assignments.json")
    ledger_p = os.path.join(scratch, "album_assignments_ledger.jsonl")

    round_idx = 1
    batch_size = 500

    while True:
        c = Consolidator(plan_p, ledger_p)
        remaining = len(c.plan) - len(c.completed_albums)
        logger.info(f"\n>>> 开始第 {round_idx} 轮迁移 (总计划 {len(c.plan)}, 已完成 {len(c.completed_albums)}, 待处理 {remaining}, 本轮批次 {batch_size})")

        if remaining <= 0:
            logger.info(">>> 所有专辑均已迁移并收拢完毕！任务圆满结束。")
            break

        c.run(limit=batch_size, dry_run=False)

        # 检查各池状态
        st = get_cluster_status()
        print_cluster_status(st, round_num=round_idx)

        # 如果剩余任务小于 batch_size 且处理完毕，则可能下一次循环就会退出
        round_idx += 1
        time.sleep(5)

    final_status = get_cluster_status()
    print_cluster_status(final_status, round_num="最终完成")

if __name__ == "__main__":
    main()
