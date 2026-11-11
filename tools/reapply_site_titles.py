#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把已发布种子的**主标题 + 副标题**按猫站口径回改到站点页面（乌托邦 / 蟹黄堡）。

背景（2026-11-10 用户拍板）：
  · 乌托邦、蟹黄堡页面上的标题是皇后原文（带「分轨」和个人压制者后缀），
    既长又脏，和猫站页面不是一套写法；
  · 三站统一走 `descr.normalize_music_title()` / `descr.build_music_small_descr()`；
  · 存量页面也要回改（乌托邦 328 条、蟹黄堡 293 条量级）。

实现要点：
  · 先 `edit.php` 读**站点现表单**当基线，只覆盖 `name` / `small_descr`
    ⇒ 站点上的分类、编码、tags、简介、nfo 等一律沿用现值；
  · `edit_torrent()` 会先比较字段，**没有变化就不提交**（省请求、也避免无意义编辑）；
  · 默认 dry-run；真正提交必须显式 `--apply`；
  · 默认只处理 `transfer_rule='ALLOW'` 的行（与 update_metadata 的编辑护栏一致），
    非 ALLOW 的行会被跳过并打印出来。

用法（在容器里跑，或用 docker exec 传进来跑）：
    python reapply_site_titles.py --site all                 # 预览两站
    python reapply_site_titles.py --site musopia --limit 5   # 预览前 5 条
    python reapply_site_titles.py --site crabpt --id 233917  # 预览单条
    python reapply_site_titles.py --site all --apply --yes --interval 1.2
    python reapply_site_titles.py --site crabpt --apply --verify --limit 3
"""
import argparse
import sys
import time

import config
import db
import descr
from musopia import LoginExpired, MusopiaClient, SiteError

SITE_CONFIG = {
    db.SITE_MUSOPIA: ("MUSOPIA_URL", "MUSOPIA_COOKIE"),
    db.SITE_CRABPT: ("CRABPT_URL", "CRABPT_COOKIE"),
}

TASK_COLUMNS = (
    "t.info_hash, t.formatted_title, t.torrent_name, t.artist, t.year, t.small_descr, "
    "t.track_type, t.log_content, t.logs_json, t.opencd_descr, t.opencd_tid, "
    "t.mapped_json, t.transfer_rule"
)


def parse_args():
    ap = argparse.ArgumentParser(
        description="按猫站口径回改已发布种子的主标题/副标题（默认 dry-run）")
    ap.add_argument("--site", choices=["musopia", "crabpt", "all"], default="all")
    ap.add_argument("--apply", action="store_true", help="真正提交（默认只预览）")
    ap.add_argument("--limit", type=int, default=0, help="每个站点最多处理多少条，0=不限")
    ap.add_argument("--id", dest="torrent_id", default="", help="只处理这一个站点种子 ID")
    ap.add_argument("--interval", type=float, default=1.0, help="两次处理之间的间隔秒数")
    ap.add_argument("--verify", action="store_true", help="提交后回读 edit.php 校验")
    ap.add_argument("--include-deny", action="store_true",
                    help="连 transfer_rule 非 ALLOW 的行也改（默认跳过）")
    ap.add_argument("--yes", action="store_true", help="跳过批量二次确认")
    return ap.parse_args()


def pick_sites(site):
    if site == "all":
        return [db.SITE_MUSOPIA, db.SITE_CRABPT]
    return [site]


def load_rows(conn, site, torrent_id="", include_deny=False):
    where = ["sp.site = ?", "sp.status = 'EXISTS'",
             "sp.torrent_id IS NOT NULL", "sp.torrent_id != ''"]
    params = [site]
    if torrent_id:
        where.append("sp.torrent_id = ?")
        params.append(str(torrent_id))
    if not include_deny:
        where.append("(t.transfer_rule = 'ALLOW' OR t.transfer_rule IS NULL "
                     "OR t.transfer_rule = '')")
    sql = (f"SELECT {TASK_COLUMNS}, sp.torrent_id AS site_tid "
           f"  FROM site_publish sp JOIN tasks t ON t.info_hash = sp.info_hash "
           f" WHERE {' AND '.join(where)} "
           f" ORDER BY sp.updated_at DESC, sp.torrent_id DESC")
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def build_client(site):
    url_attr, cookie_attr = SITE_CONFIG[site]
    url = getattr(config, url_attr)
    cookie = getattr(config, cookie_attr)
    if not cookie:
        raise SystemExit(f"❌ {cookie_attr} 为空，无法编辑 {site}")
    return MusopiaClient(base_url=url, cookie=cookie)


def wanted_fields(task):
    title = descr.normalize_music_title(task)
    small = descr.build_music_small_descr(task)
    return title, small


def process_one(client, site, task, dry_run, verify):
    tid = str(task["site_tid"])
    title, small = wanted_fields(task)
    old_title = (task.get("formatted_title") or "").strip()

    fields, before = client.fetch_edit_form(tid)
    cur_name = (before.get("name") or "").strip()
    cur_small = (before.get("small_descr") or "").strip()

    changed = (cur_name != title) or (cur_small != small)
    print(f"\n[{site}] id={tid}  {old_title}")
    print(f"  标题  : {cur_name!r}")
    print(f"       →  {title!r}")
    print(f"  副标题: {cur_small!r}")
    print(f"       →  {small!r}")

    if not changed:
        print("  ℹ️  站点已是新口径，跳过")
        return "same"

    if dry_run:
        print("  🔎 dry-run：未提交")
        return "planned"

    ok, msg = client.edit_torrent(
        tid, {"name": title, "small_descr": small}, prefetched=(fields, before))
    print(f"  {'✅' if ok else '❌'} {msg}")
    if not ok:
        return "failed"

    if verify:
        time.sleep(0.5)
        _, after = client.fetch_edit_form(tid)
        got_name = (after.get("name") or "").strip()
        got_small = (after.get("small_descr") or "").strip()
        ok_name = got_name == title
        ok_small = got_small == small
        print(f"  {'✅' if ok_name else '❌'} 回读标题: {got_name!r}")
        print(f"  {'✅' if ok_small else '❌'} 回读副标题: {got_small!r}")
        if not (ok_name and ok_small):
            return "verify_failed"
    return "edited"


def confirm(site, count):
    print(f"\n即将修改 {site} 的 {count} 条页面（不可逆）。")
    try:
        ans = input("确认继续？输入 yes 回车：").strip().lower()
    except EOFError:
        print("❌ 无 TTY，需加 --yes 才能批量提交")
        return False
    return ans == "yes"


def run_site(site, args):
    conn = db.connect()
    try:
        rows = load_rows(conn, site, args.torrent_id, args.include_deny)
    finally:
        conn.close()

    if not rows:
        print(f"[{site}] 没有符合条件的已发布记录")
        return {}

    print(f"\n{'=' * 72}\n[{site}] 待处理 {len(rows)} 条"
          f"{'（dry-run）' if not args.apply else ''}\n{'=' * 72}")

    client = build_client(site)
    stats = {"same": 0, "planned": 0, "edited": 0, "failed": 0, "verify_failed": 0}
    targets = rows[:args.limit] if args.limit else rows

    if args.apply and not args.yes:
        if not confirm(site, len(targets)):
            print("已取消")
            return stats

    for i, row in enumerate(targets, 1):
        task = db.merge_mapped(dict(row))
        print(f"\n--- [{site}] {i}/{len(targets)} ---")
        try:
            result = process_one(client, site, task, dry_run=not args.apply,
                                 verify=args.verify)
        except LoginExpired as e:
            print(f"  ❌ 登录失效：{e}")
            stats["failed"] += 1
            break
        except SiteError as e:
            print(f"  ❌ 站点错误：{e}")
            result = "failed"
        except Exception as e:                          # noqa: BLE001
            print(f"  ❌ 异常：{type(e).__name__}: {e}")
            result = "failed"
        stats[result] += 1
        if args.interval and i < len(targets):
            time.sleep(args.interval)

    print(f"\n[{site}] 汇总：{stats}")
    return stats


def main():
    args = parse_args()
    db.init_db()
    all_stats = {}
    for site in pick_sites(args.site):
        all_stats[site] = run_site(site, args)
    print("\n全部完成：", all_stats)


if __name__ == "__main__":
    sys.exit(0 if main() is None else 1)
