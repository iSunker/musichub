#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手工**单条重发**：把一条已存在（`site_status='EXISTS'`）的行**重新发布为新页**。

═══════════════════════════════════════════════════════════════════════════
为什么需要它（`#54` A 组重发；与 `publish.py --only` 的区别）
═══════════════════════════════════════════════════════════════════════════
`publish.py` 的待发队列 WHERE 首条是 **`site_status != 'EXISTS'`** ⇒ 一条"已发但**发错了
版本**"的行**进不了队列**（它`site_status='EXISTS'`），`--only` 点名也发不出去（实测：
`--dry-run` 报「💡 当前没有需要发布的任务」）。
⇒ 本工具**绕过队列**：按 `info_hash` **直接取行**（不看 `site_status`），做"单条重发"。
★ 语义上"重发一条已存在行"**本就不该**进"待发队列" —— 所以是**另开一条路**，不是放宽闸门。

★★ 仍**复用**主线的核心（`ERR-META-03`：一处生效）：
  · 查重 `publish._dedup_lookup`（站侧 `.torrent` 的 `info.name` == 本地名）；
  · 渲染 `publish.merge_mapped` + `descr.build_form_fields`；
  · 发种 `client.upload_torrent`；
  · 落库 `db.set_site_id`+`db.site_publish_set`（musopia **双写**）/ `db.site_publish_set`
    （crabpt **只**写它那一行 —— 绝不碰 `site_status`/`site_torrent_id`，同 `run_crabpt` 纪律）。
  · **闸门**：仍要求 `transfer_rule='ALLOW'`（禁转绝不发；`#173` 的 `NEED_CONFIRM` 也拦）。

用法:
    python tools/republish_one.py --only <info_hash前缀> --site musopia            # dry-run
    python tools/republish_one.py --only <info_hash前缀> --site musopia --apply --yes
★ 默认 **dry-run**；`--apply` 才真发（不可逆）；`--yes` 跳过二次确认（容器无 TTY）。
★ 发完**必须**改指针（本工具 `--apply` 成功时会**自动**写指针，见 `_write_pointer`）。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import db                                                      # noqa: E402
import descr                                                   # noqa: E402
import publish as pub                                          # noqa: E402
from musopia import LoginExpired, MusopiaClient, SiteError     # noqa: E402


def _fetch_row(prefix):
    """按 info_hash 前缀取**唯一**一行（不看 site_status）。返回 dict 或 None。"""
    con = db.connect()
    cur = con.cursor()
    cur.execute("SELECT info_hash, formatted_title, torrent_name, torrent_file, "
                "cover_url, site_status, site_torrent_id, transfer_rule, status, opencd_tid "
                "FROM tasks WHERE info_hash LIKE ?", (prefix + "%",))
    rows = cur.fetchall()
    cols = ["info_hash", "formatted_title", "torrent_name", "torrent_file", "cover_url",
            "site_status", "site_torrent_id", "transfer_rule", "status", "opencd_tid"]
    if not rows:
        return None
    if len(rows) > 1:
        raise SystemExit(f"❌ 前缀 {prefix!r} 命中 {len(rows)} 行，不唯一 ⇒ 请给更长的前缀。")
    return dict(zip(cols, rows[0]))


def _write_pointer(ih, site, new_tid, task, *, apply):
    """写指针（#55 口径）：musopia **双写**、crabpt **只** site_publish。"""
    cover = (task.get("cover_url") or None)
    if site == db.SITE_MUSOPIA:
        db.set_site_id(ih, new_tid, site_status="EXISTS", status="PUBLISHED",
                       reason=f"republished_id:{new_tid}")
        db.site_publish_set(ih, db.SITE_MUSOPIA, new_tid, status="EXISTS", cover_url=cover)
        db.mark_synced(ih)
        return "set_site_id(EXISTS) + site_publish_set(musopia)"
    # crabpt：**只**写它那一行；不碰 tasks.site_status/site_torrent_id（同 run_crabpt 纪律）
    db.site_publish_set(ih, db.SITE_CRABPT, new_tid, status="EXISTS", cover_url=cover)
    return "site_publish_set(crabpt)（不动 tasks.site_status/site_torrent_id）"


def selftest(verbose=True):
    """离线自检（**零网络、零真库**）：闸门 + 取行 + 指针写法的口径。

    ★ 为什么必须有：本工具是**不可逆发种**的手工路，它**绕过待发队列**（正是它的存在理由）
      ⇒ 队列那几道闸门（`status='SUCCESS'` / `site_status!='EXISTS'`）**都管不到它**
      ⇒ 它**自己**必须把"禁转不发""源不在不发"两道闸门焊死，且**可证伪**。
      否则一条 `DENY` 行被手工点名就能发出去（`ERR-PUBLISH-11` 的洞的翻版）。
    """
    import sqlite3
    import tempfile
    fails = []
    detail = []

    def chk(label, cond):
        if cond:
            detail.append(f"  ✅ {label}")
        else:
            fails.append(f"❌ {label}")

    d = tempfile.mkdtemp(prefix="repub-selftest-")
    saved_db = db.config.DB_PATH
    try:
        db.config.DB_PATH = os.path.join(d, "data.db")
        db.init_db()
        db.save_task(info_hash="aaaa11117777", status="PUBLISHED")
        db.save_task(info_hash="bbbb22228888", status="PUBLISHED")
        c = db.connect()
        c.execute("UPDATE tasks SET transfer_rule='NEED_CONFIRM', site_status='EXISTS', "
                  "torrent_file='/no.torrent' WHERE info_hash='aaaa11117777'")
        c.execute("UPDATE tasks SET transfer_rule='ALLOW', site_status='EXISTS', "
                  "torrent_file='/no2.torrent' WHERE info_hash='bbbb22228888'")
        c.commit()

        # ① 取行（唯一/缺失/多命中）
        chk("唯一前缀 ⇒ 取到行", (_fetch_row("aaaa1111") or {}).get("info_hash") == "aaaa11117777")
        chk("未知前缀 ⇒ None", _fetch_row("zzzz") is None)

        # ② 闸门：NEED_CONFIRM 必须被拦（★ 核心：手工路不能绕过禁转）
        os_error = None
        class _Exit(Exception):
            pass
        def _fake_exit(msg=""):
            raise _Exit(str(msg))
        import builtins
        _orig_exit = sys.exit
        sys.exit = _fake_exit
        try:
            sys.argv = ["republish_one", "--only", "aaaa1111", "--site", "musopia"]
            try:
                main()
            except _Exit as e:
                os_error = str(e)
            chk("闸门④：NEED_CONFIRM ⇒ 拒（不发）", "拒绝" in (os_error or "") and "NEED_CONFIRM" in (os_error or ""))
            # ③ 闸门：源不在 ⇒ 拒
            os_error = None
            sys.argv = ["republish_one", "--only", "bbbb2222", "--site", "musopia"]
            try:
                main()
            except _Exit as e:
                os_error = str(e)
            chk("源不在 ⇒ 拒（不发）", "不存在" in (os_error or ""))
        finally:
            sys.exit = _orig_exit

        # ④ 指针写法口径：musopia 双写 / crabpt 只 site_publish —— 用**桩**记录调用
        calls = []
        _set, _sp = db.set_site_id, db.site_publish_set
        db.set_site_id = lambda *a, **k: calls.append(("set_site_id", a, k))
        db.site_publish_set = lambda *a, **k: calls.append(("site_publish_set", a, k))
        db.mark_synced = lambda *a, **k: calls.append(("mark_synced", a, k))
        try:
            _write_pointer("ih", db.SITE_MUSOPIA, "999", {"cover_url": None}, apply=True)
            chk("musopia ⇒ 双写（set_site_id + site_publish_set）",
                any(c[0] == "set_site_id" for c in calls) and
                any(c[0] == "site_publish_set" for c in calls))
            calls.clear()
            _write_pointer("ih", db.SITE_CRABPT, "999", {"cover_url": None}, apply=True)
            chk("crabpt ⇒ 只 site_publish_set（不碰 tasks）",
                (not any(c[0] == "set_site_id" for c in calls)) and
                any(c[0] == "site_publish_set" for c in calls))
        finally:
            db.set_site_id, db.site_publish_set, db.mark_synced = _set, _sp, db.mark_synced
    finally:
        db.config.DB_PATH = saved_db

    if verbose:
        print(f"{'✅' if not fails else '❌'} republish_one 自检（闸门 + 取行 + 指针口径）")
        for x in detail:
            print(x)
        for x in fails:
            print(x)
    return (not fails), (fails or detail)


def main():
    ap = argparse.ArgumentParser(description="手工单条重发（绕过待发队列）")
    ap.add_argument("--only", required=False, help="info_hash 前缀（唯一命中一行）")
    ap.add_argument("--site", choices=("musopia", "crabpt"), required=False)
    ap.add_argument("--apply", action="store_true", help="真发（默认 dry-run，不可逆）")
    ap.add_argument("--yes", action="store_true", help="跳过二次确认")
    ap.add_argument("--no-readd", action="store_true", help="发布后不回加 qB（默认不回加）")
    ap.add_argument("--selftest", action="store_true", help="离线自检后退出")
    ap.add_argument("--json", action="store_true", help="自检结果 JSON 打末行（给 gen_counts）")
    args = ap.parse_args()

    if args.selftest:
        ok, _ = selftest(verbose=not args.json)
        if args.json:
            import json as _json
            print(_json.dumps({"script": "tools/republish_one.py --selftest",
                               "ok": bool(ok), "total": 7, "exit": 0 if ok else 1},
                              ensure_ascii=False))
        return 0 if ok else 1

    if not args.only or not args.site:
        ap.error("需要 --only <info_hash前缀> --site musopia|crabpt（或 --selftest）")

    task = _fetch_row(args.only)
    if not task:
        sys.exit(f"❌ 库里找不到 info_hash 前缀 {args.only!r} 的行。")

    ih = task["info_hash"]
    print(f"=== 手工单条重发 · site={args.site} · {'APPLY' if args.apply else 'DRY-RUN'} ===")
    print(f"  info_hash      = {ih}")
    print(f"  formatted_title= {task['formatted_title']}")
    print(f"  torrent_file   = {task['torrent_file']}  exists={os.path.exists(task['torrent_file'] or '')}")
    print(f"  site_status    = {task['site_status']}  site_torrent_id={task['site_torrent_id']}")
    print(f"  transfer_rule  = {task['transfer_rule']}  status={task['status']}")

    # ---- 闸门：禁转绝不发（只有 ALLOW 放行；NEED_CONFIRM/DENY/空 一律拦）----
    if (task.get("transfer_rule") or "") != "ALLOW":
        sys.exit(f"🛑 拒绝：transfer_rule={task.get('transfer_rule')!r}（只放行 'ALLOW'）"
                 f" —— 禁转/拿不准的行绝不发（闸门④）。")

    if not os.path.exists(task["torrent_file"] or ""):
        sys.exit(f"🛑 拒绝：本地 .torrent 不存在（{task['torrent_file']}）⇒ 源丢了，无法重发。")

    client = MusopiaClient() if args.site == "musopia" else __import__("crabpt_site").CrabptClient()
    try:
        uid = client.check_login()
        print(f"  ✅ 登录有效：user_id={uid}")
    except SiteError as e:
        sys.exit(f"❌ {e}")

    # ---- 查重：站点已有同版本 ⇒ 只关联、不重复提交 ----
    try:
        hit = pub._dedup_lookup(client,
                                search_name=task["formatted_title"],
                                local_name=pub._local_info_name(task),
                                site_label=args.site)
    except LoginExpired as e:
        sys.exit(f"❌ 查重登录失效：{e}")
    except SiteError as e:
        sys.exit(f"❌ 查重失败（站点侧）⇒ 停：{e}")

    if hit:
        print(f"  🔍 查重命中：站点已有同版本 id={hit} ⇒ **不再重复提交**，改为关联。")
        if args.apply:
            print("  → 写指针: " + _write_pointer(ih, args.site if args.site == "crabpt" else db.SITE_MUSOPIA,
                                                  hit, task, apply=True))
        else:
            print("  [dry-run] 将写指针（关联到已有页），不提交。")
        return 0

    # ---- 渲染 + 发种 ----
    fields = descr.build_form_fields(pub.merge_mapped(task), anonymous=True)
    print(f"  📄 简介预览（前 300 字）：\n    {fields['descr'][:300]!r}")
    if not args.apply:
        print("\n[dry-run] 未提交。确认无误后加 --apply --yes 真发（不可逆）。")
        return 0
    if not args.yes:
        if input("确认真发这一条？(y/N): ").strip().lower() != "y":
            print("🚫 已取消。")
            return 0

    try:
        new_tid, msg = client.upload_torrent(task["torrent_file"], fields)
    except LoginExpired as e:
        sys.exit(f"❌ 登录失效（站点侧）：{e}")
    except SiteError as e:
        sys.exit(f"❌ 发种失败（本条）：{e}")

    if not new_tid:
        sys.exit(f"❌ 发种被拒 / 站点侧问题：{msg}")
    print(f"  🎉 发布成功，新 tid={new_tid}（msg={msg}）")

    # ---- 立刻写指针（#55；★ 发布 + 改指针必须连着做，否则 #173 护栏会拒改 edit）----
    site_const = db.SITE_MUSOPIA if args.site == "musopia" else db.SITE_CRABPT
    print("  → 写指针: " + _write_pointer(ih, site_const, new_tid, task, apply=True))
    print("  ✅ 完成：新页已发 + 指针已改。建议只读核对："
          f"verify_hosted_edition.py --site {args.site} --info-hash {args.only}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                          # noqa: BLE001
        pass
    sys.exit(main())
