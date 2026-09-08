"""数据库状态速览。诊断"为什么这条没被编辑/发布"时先跑这个。"""
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
    print("\n[status / site_status 分布]")
    for status, site_status, count in st["groups"]:
        print(f"   {status or '-':<14} {site_status or '-':<10} {count:>5}")

    print(f"\n⚠️  已发布但缺 site_torrent_id : {st['no_site_id']}"
          f"   {'→ 跑 sync_site.py --apply' if st['no_site_id'] else ''}")
    print(f"⚠️  已发布但缺封面             : {st['no_cover']}"
          f"   {'→ 跑 backfill_covers.py --apply' if st['no_cover'] else ''}")

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
