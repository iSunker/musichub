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
               "       site_synced_at, opencd_tid, "
               # ★★ 2026-09-28 加：`transfer_rule` —— 第 6 步的**真正闸门**。
               #   `update_metadata.py` 的点名路带 `require_allow=True`
               #   ⇒ 规则非 `'ALLOW'` 的行**推不动**（实测 `15972` 是 `None` 被拦）。
               #   ★ 为什么在这里补印、而不另写查询：本工具**已经**逐条遍历这 28 条、
               #     已经连着 `edit.php` —— 再写一个查询就是**第二份"已上站行"口径**，
               #     两份迟早对不上（`ERR-META-03`）。⇒ 同一段代码里多印一格。
               "       transfer_rule "
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
    # ★★★ 2026-09-28 加：**主标题** `name` 的对照计数（第 6 步）。
    #   ★ 与 `small_descr` **分开计数、分开印** —— 两者量纲不同、结论也不同，
    #     合并成一个数就是 `ERR-META-05`（那次把 6 报成 31 的形状）。
    n_name_diff = 0
    # ★★ 逐条留档：`name` 是否一致 + 该行的 `transfer_rule` —— 汇总段要按
    #   **"标题不一致 ∧ rule=ALLOW"** 这一对判据分档，两个都是逐行的值，
    #   汇总时不能再查一次库（那就成了第二次口径，`ERR-META-03`）。
    _name_ok_by_sid = {}
    _rule_by_sid = {}
    report = []

    for r in rows:
        sid = r["site_torrent_id"]
        title = (r["formatted_title"] or r["torrent_name"] or "")[:52]
        mval, mwhy = mapped_small(r)
        col = r["small_descr"]
        try:
            fields, _snap = client.fetch_edit_form(sid)
            site = fields.get("small_descr")
            # ★★★ 2026-09-28 加：**同一个表单**里就有 `name`（主标题），
            #   而本工具只印了 `small_descr` ⇒ 第 6 步（"站上 name 与库现值一致吗"）
            #   本来要另写一个工具、再抓一遍 `edit.php`。
            #   ★ 为什么不另写：那是**同一个 GET**，只是**少印了一格**——
            #     多写一个工具就多一份"站点 vs 库"的口径，两份迟早对不上
            #     （`ERR-META-03` 的形状）。⇒ 在这里补印，判据共用同一段代码。
            #   ★ 对照物与 `small_descr` 同源：本地**列** `formatted_title`
            #     （不是 `torrent_name` —— 那是**皇后原始串**，与站点上的值不是一回事；
            #      见 `fix_onsite_titles_and_subtitles.py` 头部的实测）。
            site_name = fields.get("name")
            err = None
        except LoginExpired as e:
            sys.exit(f"❌ Cookie 失效：{e}")
        except SiteError as e:
            site, site_name, err = None, None, str(e)[:70]
        except Exception as e:                                   # noqa: BLE001
            site, site_name, err = None, None, f"{type(e).__name__}: {e}"

        if err:
            n_err += 1
            tag = "打不开"
            name_tag, name_ok = "打不开", False
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
            # ── 主标题（第 6 步的判据）──
            sn = "" if site_name is None else str(site_name).strip()
            lt = "" if r["formatted_title"] is None else str(r["formatted_title"]).strip()
            name_ok = (sn == lt)
            if sn != lt:
                n_name_diff += 1
            name_tag = "一致" if name_ok else ("★站点空" if sn == "" else "≠库值")

        report.append((sid, title, col, mval, site, err, tag, mwhy))
        print(f"── sid={sid}  {title}")
        print(f"   本地**列**      = {col!r}")
        print(f"   mapped_json    = {mval!r}" + (f"  ⚠️ {mwhy}" if mwhy else ""))
        print(f"   ★站点 edit.php = "
              + (f"❌ {err}" if err else f"{site!r}")
              + f"   ⇒ {tag}")
        # ★ 主标题行（第 6 步）：库值 vs 站点值。**只印，不判"该不该推"**
        #   —— 推不推由人拍板（`update_metadata.py` 是写动作）。
        print(f"   ── 主标题 `name`（第 6 步）──")
        print(f"      本地列 formatted_title = {r['formatted_title']!r}")
        print(f"      ★站点 name            = "
              + (f"❌ {err}" if err else f"{site_name!r}")
              + f"   ⇒ {name_tag}")
        # ★ `transfer_rule` —— **能不能推**由它决定（点名路带 `require_allow=True`）。
        #   ★ 与"标题一致不一致"**分开印**：两者是不同的判据（一个是值、一个是权限），
        #     合并就会让"标题对了"看起来像"可以推"（`ERR-META-05` 的形状）。
        _rule = r["transfer_rule"]
        _name_ok_by_sid[sid] = name_ok
        _rule_by_sid[sid] = _rule
        _pushable = (str(_rule).strip() == "ALLOW") and (not name_ok)
        print(f"      transfer_rule = {_rule!r}   ⇒ "
              + ("**可推**（rule=ALLOW 且标题不一致）" if _pushable else
                 ("标题已一致 ⇒ 无需推" if name_ok else
                  "★ **推不动**：规则不是 'ALLOW'（点名路会被闸门④ 拦）")))
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
    # ★★★ 第 6 步的判据（2026-09-28 加）——**单独一段**，不与上面混。
    print("─" * 78)
    print("★ 主标题 `name`（**第 6 步**：站上 name 与库现值一致吗）")
    print("─" * 78)
    if n_err:
        print(f"  ⚠️  有 {n_err} 条 `edit.php` 打不开 ⇒ 这批**没测到**"
              f"（不许当'一致' —— ERR-META-14）")
    print(f"  站点 `name` ≠ 库 `formatted_title` = **{n_name_diff}** / {len(rows)}")
    # ★★ 第 6 步的**可推性**分档（2026-09-28 加）——
    #   标题不一致 **不等于** 能推：点名路带 `require_allow=True`，
    #   规则非 ALLOW 的行**推不动**。两者必须分开报，否则"1 条不一致"
    #   会被读成"推 1 条就行"（实测 `15972` 正是被规则拦下的那一条）。
    _n_push = sum(1 for sid, _t, _c, _m, _s, _e, _tag, _w in report
                  if _e is None and not _name_ok_by_sid.get(sid, True)
                  and str(_rule_by_sid.get(sid)).strip() == "ALLOW")
    _n_blocked = sum(1 for sid in _name_ok_by_sid
                     if not _name_ok_by_sid[sid]
                     and str(_rule_by_sid.get(sid)).strip() != "ALLOW")
    print(f"  ├ 其中「rule=ALLOW 且标题不一致」⇒ **可推** = **{_n_push}**")
    print(f"  └ 其中「标题不一致但 rule 非 ALLOW」⇒ **推不动** = **{_n_blocked}**"
          f"   ★ 要推得先**显式**标 ALLOW（一次有记录的写库动作），别绕过闸门④")
    for sid in _name_ok_by_sid:
        if not _name_ok_by_sid[sid] and str(_rule_by_sid.get(sid)).strip() != "ALLOW":
            print(f"       拦住的: sid={sid}  rule={_rule_by_sid.get(sid)!r}")
    if n_name_diff == 0 and not n_err:
        print("  ✅ **全部一致** ⇒ 第 6 步（推 `name`）**不用做**：站上已是库里的值。")
    elif _n_push:
        print(f"  ⇒ 有 {_n_push} 条**可推**（站是旧值 + rule=ALLOW）⇒ 用：")
        print(f"     `update_metadata.py <sid> --fields 'name' --apply --verify`"
              f"（★ `torrent_id` 是**位置参数**；★ 必带 name）")
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
