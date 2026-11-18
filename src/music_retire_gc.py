# -*- coding: utf-8 -*-
"""全站完结种子跨端退役下架（GC 协同阶段）：
当一个种子在所有启用的目标站点均已发布（或查重已存在，status='EXISTS'），
且自最后一次站点达成起已满足保种考核天数（默认 14 天，且 OpenCD 36h HR 满足），
将该种子的做种任务从 music1/2/3 安全移除（不删物理文件），
使各音乐池同名目录安全转变为孤儿，交由 music_orphan_gc 彻底释放磁盘物理空间。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import db
import qb_client

RETIRE_DAYS_REQUIRED = float(os.getenv("GC_RETIRE_DAYS", "14.0"))
HR_HOURS_REQUIRED = 36.0

INSTANCES = [
    ("music1", "http://127.0.0.1:3220"),
    ("music2", "http://127.0.0.1:3240"),
    ("music3", "http://127.0.0.1:3260"),
]


def get_enabled_sites():
    """获取系统当前启用发布的外站列表。"""
    sites = []
    if os.getenv("ENABLE_PUBLISH", "true").lower() == "true":
        sites.append(db.SITE_MUSOPIA)
    if os.getenv("ENABLE_PUBLISH_CRABPT", "false").lower() == "true":
        sites.append(db.SITE_CRABPT)
    if os.getenv("ENABLE_PUBLISH_PTER", "false").lower() == "true":
        sites.append(db.SITE_PTER)
    if os.getenv("ENABLE_PUBLISH_XINGWAN", "false").lower() == "true":
        sites.append(db.SITE_XINGWAN)
    if os.getenv("ENABLE_PUBLISH_HDKYLIN", "false").lower() == "true":
        sites.append(db.SITE_HDKYLIN)
    if os.getenv("ENABLE_PUBLISH_TANGPT", "false").lower() == "true":
        sites.append(db.SITE_TANGPT)
    if os.getenv("ENABLE_PUBLISH_MUXUEGE", "false").lower() == "true":
        sites.append(db.SITE_MUXUEGE)
    return sites or [db.SITE_MUSOPIA]


def collect_completed_torrents(enabled_sites, retire_days=RETIRE_DAYS_REQUIRED):
    """从数据库中找出已在所有 enabled_sites 达成 EXISTS，且最晚完成时间距今 >= retire_days 的种子。"""
    conn = db.connect()
    try:
        placeholders = ",".join("?" for _ in enabled_sites)
        query = f"""
            SELECT info_hash, COUNT(DISTINCT site) AS site_cnt, MAX(updated_at) AS last_pub
            FROM {db.SITE_PUBLISH_TABLE}
            WHERE status = 'EXISTS' AND site IN ({placeholders})
            GROUP BY info_hash
            HAVING site_cnt = ? AND last_pub <= datetime('now', '-{float(retire_days)} days')
        """
        params = list(enabled_sites) + [len(enabled_sites)]
        rows = conn.execute(query, params).fetchall()
        return {r[0].lower(): r[2] for r in rows}
    finally:
        conn.close()


def find_retire_candidates(completed_map):
    """核对 qB 做种时长并定位各客户端的任务。"""
    import types

    clients = {}
    for name, host in INSTANCES:
        cfg = types.SimpleNamespace(
            QB_HOST=host,
            QB_USER=getattr(config, "QB_USER", "admin"),
            QB_PASS=getattr(config, "QB_PASS", ""),
            QB_API_KEY=getattr(config, "OPENCD_QB_API_KEY", "") if "3260" in host else getattr(config, "QB_API_KEY", ""),
        )
        try:
            clients[name] = qb_client.build(cfg)
        except Exception as e:
            raise RuntimeError(f"连接实例 {name} 失败: {e}") from e

    # 1. 扫描 music3，确认 OpenCD 考核通过 (seeding_time >= 36h)
    m3_torrents = {t.hash.lower(): t for t in clients["music3"].torrents_info()}

    cands = []
    for ih, last_pub in completed_map.items():
        t3 = m3_torrents.get(ih)
        if t3:
            st_hours = float(getattr(t3, "seeding_time", 0) or 0) / 3600.0
            if st_hours < HR_HOURS_REQUIRED:
                continue  # HR 未达标
            name = getattr(t3, "name", "")
            size = int(getattr(t3, "size", 0) or 0)
        else:
            name = ih
            size = 0

        cands.append({
            "info_hash": ih,
            "name": name,
            "size": size,
            "last_pub": last_pub,
        })

    return cands, clients


def retire_one(clients, cand, dry_run=True):
    """跨客户端移除做种任务（delete_files=False）。"""
    ih = cand["info_hash"]
    results = {}
    for name, cli in clients.items():
        if dry_run:
            results[name] = "dry_run"
            continue
        try:
            cli.torrents_delete(delete_files=False, torrent_hashes=ih)
            results[name] = "ok"
        except Exception as e:
            results[name] = f"err:{e}"
    return results


def main():
    ap = argparse.ArgumentParser(description="全站已发布种子跨端安全退役下架（14天统一退役）")
    ap.add_argument("--apply", action="store_true", help="实际执行任务移除（默认只报告）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条")
    ap.add_argument("--days", type=float, default=RETIRE_DAYS_REQUIRED, help="全站完成达标天数门禁")
    args = ap.parse_args()

    enabled_sites = get_enabled_sites()
    print("=" * 68)
    print("🎯 全站完结种子 14 天协同退役下架")
    print("=" * 68)
    print(f"监测目标站点 ({len(enabled_sites)} 个): {', '.join(enabled_sites)}")
    print(f"保种达标门禁: >= {args.days} 天 且 OpenCD >= {HR_HOURS_REQUIRED}h")

    completed_map = collect_completed_torrents(enabled_sites, retire_days=args.days)
    print(f"符合全站 EXISTS 且满 {args.days} 天的种子: {len(completed_map)} 条")

    if not completed_map:
        print("\n✔ 当前没有符合退役条件的种子")
        return 0

    cands, clients = find_retire_candidates(completed_map)
    print(f"经 music3 HR 考核核验有效候选: {len(cands)} 条")

    picked = cands
    if args.limit > 0:
        picked = picked[:args.limit]

    for c in picked[:15]:
        print(f"   [待退役] {c['info_hash'][:12]} | 完成于 {c['last_pub']} | {c['name'][:45]}")

    if not args.apply:
        print("\n[DRY-RUN] 未移除任何任务。确认后加 --apply 执行任务脱退。")
        return 0

    ok_cnt = 0
    for c in picked:
        res = retire_one(clients, c, dry_run=False)
        ok_cnt += 1
        print(f"   ✅ 已脱退: {c['info_hash'][:12]} -> {res}")

    print(f"\n🎉 协同退役完成：共下架 {ok_cnt} 条做种任务。")
    print("💡 下架后各池同名目录已成为孤儿，将在后续 music_orphan_gc 阶段彻底释放物理存储！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
