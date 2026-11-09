"""数据库状态速览工具。用于查看任务统计、发布队列筛选漏斗及异常数据。"""
import argparse

import config
import db


def main():
    ap = argparse.ArgumentParser(description="查看任务库状态")
    ap.add_argument("--list", action="store_true", help="逐行列出所有任务")
    ap.add_argument("--problems", action="store_true",
                    help="只列出有问题的记录（缺 ID / 缺封面 / 失败）")
    args = ap.parse_args()

    config.print_runtime_info()
    db.init_db()

    st = db.stats()
    print(f"📊 总行数: {st['total']}")

    # status 与 site_status 联合分布
    print("\n[status / site_status 联合分布]")
    for status, site_status, count in st["groups"]:
        print(f"   status={status or '-':<14} site_status={site_status or '-':<10} {count:>5}")

    print("\n[闸门单列基础计数]")
    try:
        _conn = db.connect()
        _exists = _conn.execute(
            "SELECT count(*) FROM tasks WHERE site_status = 'EXISTS'").fetchone()[0]
        _not_exists = _conn.execute(
            "SELECT count(*) FROM tasks WHERE site_status IS NULL "
            "OR site_status != 'EXISTS'").fetchone()[0]
        _conn.close()
        print(f"   site_status='EXISTS'      {_exists:>5}")
        print(f"   不是 EXISTS               {_not_exists:>5}")
    except Exception as e:
        print(f"   （读取失败：{type(e).__name__}: {e}）")

    print(f"\n⚠️  已发布但缺 site_torrent_id : {st['no_site_id']}"
          f"   {'→ 跑 sync_site.py --apply' if st['no_site_id'] else ''}")
    print(f"⚠️  已发布但缺封面             : {st['no_cover']}"
          f"   {'→ 跑 backfill_covers.py --apply' if st['no_cover'] else ''}")

    # 发布队列逐级漏斗
    fn = db.publish_funnel()
    print("\n[发布队列逐级漏斗]")
    _chain = [
        ("① status=SUCCESS", fn["g1_status_success"]),
        ("①+② 未上站", fn["g2b_site_not_exists"]),
        ("①+②+②b 发布日期≤7天前", fn["g2_added_7d"]),
        ("①+②+②b+③ 皇后行已抓页面", fn["g3_mapped"]),
        ("①+②+②b+③+④ rule=ALLOW", fn["g4_allow"]),
    ]
    _prev = None
    for label, n in _chain:
        delta = "" if _prev is None else f"   （本级拦截 {_prev - n}）"
        print(f"   {label:<26} {n:>5}{delta}")
        _prev = n
    print(f"   {'★ 最终待发布队列':<26} {fn['queue']:>5}")

    # 闸门② 拦截细分
    print("\n[闸门② 拦截原因细分]")
    try:
        _conn = db.connect()
        _base = ("status = 'SUCCESS' AND (site_status IS NULL OR site_status != 'EXISTS')")
        _no_date = _conn.execute(
            f"SELECT count(*) FROM tasks WHERE {_base} "
            f"AND (opencd_added_at IS NULL OR opencd_added_at = '')").fetchone()[0]
        _too_new = _conn.execute(
            f"SELECT count(*) FROM tasks WHERE {_base} "
            f"AND opencd_added_at IS NOT NULL AND opencd_added_at != '' "
            f"AND opencd_added_at > datetime('now', '-7 days')").fetchone()[0]
        _conn.close()
        print(f"   ├ 无发布日期（缺失 opencd_added_at）: {_no_date:>5}")
        print(f"   └ 处于 7 天保护窗口内                 : {_too_new:>5}")
    except Exception as e:
        print(f"   （读取失败：{type(e).__name__}: {e}）")

    # 全库现状指标
    print("\n[全库元数据现状]")
    print(f"   有发布日期 opencd_added_at : {fn['added_notnull']:>5}")
    print(f"   有来源 ID  opencd_tid      : {fn['tid_notnull']:>5}")
    print(f"   transfer_rule='ALLOW'      : {fn['rule_allow']:>5}")
    print(f"   transfer_rule 为空         : {fn['rule_null']:>5}")

    # 详情抓取待办
    try:
        import topup_opencd_fields as _tp
        conn = db.connect()
        n_all = conn.execute(
            f"SELECT count(*) FROM tasks WHERE {_tp.TARGET_WHERE}").fetchone()[0]
        n_pick = conn.execute(
            f"SELECT count(*) FROM tasks WHERE {_tp.PICK_WHERE}").fetchone()[0]
        conn.close()
        print("\n[详情抓取待办统计]")
        print(f"   有 tid 且未抓取详情（总计）: {n_all:>5}")
        print(f"   ├ 未上站                   : {n_pick:>5}")
        print(f"   └ 已上站                   : {n_all - n_pick:>5}")
    except Exception as e:
        print(f"\n⚠️  抓取待办统计读取失败（{type(e).__name__}: {e}）")

    if fn["queue"] != fn["g4_allow"]:
        print(f"\n   ⚠️⚠️ 队列与漏斗末端计数不一致：队列 {fn['queue']} != 链末 {fn['g4_allow']}")

    if args.problems:
        print("\n[缺 site_torrent_id]")
        for t in db.get_published(only_missing_id=True)[:30]:
            print(f"   {t['formatted_title']}")
        print("\n[缺封面]")
        for t in db.get_missing_cover()[:30]:
            print(f"   [{t['site_torrent_id'] or '-'}] {t['formatted_title']}")

    if args.list:
        conn = db.connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT site_torrent_id, status, site_status, formatted_title, "
            "site_synced_at FROM tasks ORDER BY updated_at DESC"
        )
        print("\nsite_id | status | site_status | synced_at | title")
        print("-" * 100)
        for sid, status, sst, title, synced in cur.fetchall():
            print(f"{sid or '-':>7} | {status or '-':<11} | {sst or '-':<8} | "
                  f"{(synced or '-')[:19]:<19} | {(title or '')[:50]}")
        conn.close()


if __name__ == "__main__":
    main()
