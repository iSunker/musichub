"""★ 只读：核查**站点上**的副标题有没有被 `edit` 写空，并与 `mapped_json` 对照。

═══════════════════════════════════════════════════════════════════════════
为什么要先跑这个（而不是直接改渲染层）
═══════════════════════════════════════════════════════════════════════════
`x/HANDOFF-20260926.md` 明确写着「站点上**实际**有没有被写空、哪几条 —— **未查**」。
而那次要改的是 `descr.build_form_fields()`（影响**两站**提交的字段）——
★ 在没有现场证据的情况下动渲染层 = 用"代码里看着会这样"代替"站点上真的是这样"，
  正是 `ERR-META-01`（判据测了错东西）与本轮已栽两次的那个形状。

⇒ 先测出**站点现状**：
    ① 站点 `edit.php` 上那一条的 `small_descr` 现值是什么（空 / 有值）
    ② 本地 `mapped_json.small_descr` 是什么（皇后原文）
    ③ 本地**列** `small_descr` 是什么
  三者两两对照 ⇒ 才知道"写空"到底发生过没、是哪种形态。

═══════════════════════════════════════════════════════════════════════════
★★ 只读保证（逐条）
═══════════════════════════════════════════════════════════════════════════
  站点：只 `client.fetch_edit_form(tid)` —— 它**只 GET `edit.php`**、
        **不提交** `takeedit.php`（见 `musopia.py:227` 的契约）。
        ★ 不走 `--apply` 那条路，绝不 POST。
  库：  `PRAGMA query_only=1`，只 SELECT（不 cp 生产库，`ERR-SQL-01`）。
  qB：  ★ 完全不碰。
  磁盘：★ 完全不碰。
  写：  ★ 零。

用法（NAS，compose 目录）:
    sudo docker compose run --rm tools python /data/tools/verify_small_descr_onsite.py
    # 只看某一条
    sudo docker compose run --rm tools python /data/tools/verify_small_descr_onsite.py --sid 15972
    # 全量（默认只查 EXISTS 里有 mapped_json 的）
    sudo docker compose run --rm tools python /data/tools/verify_small_descr_onsite.py --all
"""
import argparse
import json
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.environ.get("MUSOPIA_APP_DIR", "/app"))

import config  # noqa: E402


def load_rows(db_path, only_sid=None, include_all=False):
    """★ 只读。取要核的对象。

    默认口径 = **`site_status='EXISTS'` 且有 `site_torrent_id`** —— 那是唯一
    `edit` 会去动的集合（`db.PENDING_PUBLISH`/`STALE_CONDITION` 的前提）。
    ★ 不加 `EXISTS` 会把"还没上站"的行也拉进来 ⇒ 它们在站点上**不存在**，
      `edit.php` 必然打不开 ⇒ 报出一堆假的"找不到表单"（`ERR-META-03` 的形状）。
    """
    con = sqlite3.connect(db_path, timeout=30)
    con.execute("PRAGMA query_only=1")
    con.row_factory = sqlite3.Row
    try:
        sql = ("SELECT info_hash, formatted_title, torrent_name, site_torrent_id, "
               "       site_status, small_descr, mapped_json, updated_at, "
               "       site_synced_at, opencd_tid "
               "  FROM tasks ")
        params = []
        if only_sid:
            sql += " WHERE site_torrent_id = ?"
            params.append(str(only_sid))
        elif not include_all:
            sql += (" WHERE site_status = 'EXISTS' "
                    "   AND site_torrent_id IS NOT NULL AND site_torrent_id != ''")
        sql += " ORDER BY CAST(site_torrent_id AS INTEGER)"
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def mapped_small(row):
    """从 `mapped_json` 里取 `small_descr`（★ 解析失败**如实报错**，不当空）。"""
    raw = row.get("mapped_json")
    if not raw or not str(raw).strip():
        return None, "无 mapped_json"
    try:
        d = json.loads(raw)
    except (ValueError, TypeError) as e:                        # noqa: BLE001
        return None, f"mapped_json 非法 JSON：{e}"
    if not isinstance(d, dict):
        return None, f"mapped_json 不是 dict 而是 {type(d).__name__}"
    return d.get("small_descr"), ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="★ 只读：核查站点副标题有没有被 edit 写空")
    ap.add_argument("--sid", default=None, help="只查这一个 site_torrent_id")
    ap.add_argument("--all", action="store_true",
                    help="★ 不筛 EXISTS（会包含未上站的行 ⇒ edit.php 打不开是正常的）")
    ap.add_argument("--limit", type=int, default=0, help="最多查 N 条（0 = 不限）")
    ap.add_argument("--db", default=config.DB_PATH)
    args = ap.parse_args(argv)

    print("=" * 78)
    print("★ 只读核查：**站点上的副标题** vs `mapped_json` vs 本地列")
    print("=" * 78)
    print("  ★ 全程只 GET `edit.php`（**不提交 takeedit.php**）、库只读、qB/磁盘不碰")
    print()

    rows = load_rows(args.db, args.sid, args.all)
    if args.limit:
        rows = rows[:args.limit]
    print(f"待核对象 = {len(rows)} 条"
          f"（口径：{'指定的 sid' if args.sid else ('全部行' if args.all else 'site_status=EXISTS 且有 site_torrent_id')}）")
    print()

    from musopia import MusopiaClient, SiteError, LoginExpired

    client = MusopiaClient()
    try:
        uid = client.check_login()
    except (SiteError, LoginExpired) as e:
        sys.exit(f"❌ 登录失败：{e}")
    print(f"  站点 = {config.MUSOPIA_URL}   check_login() uid = {uid}")
    print()

    n_empty = 0        # 站点副标题为空
    n_diff = 0         # 站点 != mapped_json
    n_col_diff = 0     # 本地列 != mapped_json
    n_err = 0
    report = []

    for r in rows:
        sid = r["site_torrent_id"]
        title = (r["formatted_title"] or r["torrent_name"] or "")[:52]
        mval, mwhy = mapped_small(r)
        col = r["small_descr"]
        try:
            fields, _snap = client.fetch_edit_form(sid)
            site = fields.get("small_descr")
            err = None
        except LoginExpired as e:
            sys.exit(f"❌ Cookie 失效：{e}")
        except SiteError as e:
            site, err = None, str(e)[:70]
        except Exception as e:                                   # noqa: BLE001
            site, err = None, f"{type(e).__name__}: {e}"

        if err:
            n_err += 1
            tag = "打不开"
        else:
            s = "" if site is None else str(site).strip()
            m = "" if mval is None else str(mval).strip()
            c = "" if col is None else str(col).strip()
            if s == "":
                n_empty += 1
            if s != m:
                n_diff += 1
            if c != m:
                n_col_diff += 1
            tag = "★站点空" if s == "" else ("≠快照" if s != m else "一致")

        report.append((sid, title, col, mval, site, err, tag, mwhy))
        print(f"── sid={sid}  {title}")
        print(f"   本地**列**      = {col!r}")
        print(f"   mapped_json    = {mval!r}" + (f"  ⚠️ {mwhy}" if mwhy else ""))
        print(f"   ★站点 edit.php = "
              + (f"❌ {err}" if err else f"{site!r}")
              + f"   ⇒ {tag}")
        print()

    print("=" * 78)
    print("★ 汇总（精确计算）")
    print("=" * 78)
    print(f"  对象数          = {len(rows)}")
    print(f"  ★ 站点副标题为空 = **{n_empty}**")
    print(f"  站点 ≠ mapped   = **{n_diff}**")
    print(f"  本地列 ≠ mapped = **{n_col_diff}**")
    print(f"  edit.php 打不开 = {n_err}")
    print()
    print("★ 怎么读这张表")
    print("  · 「站点空」> 0 ⇒ **写空真发生过**，且有现场名单（这是改渲染层的前提）")
    print("  · 「站点空」= 0 且「站点 ≠ mapped」> 0 ⇒ 站上是**别的错值**（如「全 N 曲」），")
    print("     那是 `scan` 覆盖/回填没推的形态，与'写空'是**两条不同的路**")
    print("  · 「站点 == mapped」⇒ 那一条是好的，别动它")
    print()
    print("★ 本脚本没有修改任何东西（站点只 GET、库只 SELECT、qB 与磁盘不碰）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
