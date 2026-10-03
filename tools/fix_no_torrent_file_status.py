"""把「其实有 `.torrent` 文件」的行从 `NO_TORRENT_FILE` 改回 `SUCCESS`。

═══════════════════════════════════════════════════════════════════════════
它修的到底是什么（2026-09-28 实测）
═══════════════════════════════════════════════════════════════════════════
`db.PENDING_PUBLISH_WHERE` 的**第一句**就是 `status = 'SUCCESS'`。
⇒ 一行只要不是 `SUCCESS`，**无论内容多齐全**，都进不了发布队列。

而 `topup_opencd_fields.py` 早年**刻意不传 `torrent_file`**（它模块头纪律 5 的
旧版：怕"重建 `.torrent` 会换 announce passkey ⇒ 改 infohash"）⇒
`opencd_ingest.build_row()` 落 `NO_TORRENT_FILE` ⇒ **补一行就把一行踢出队列**。

实测现场（2026-09-28）：
    · 全库发布队列 `PENDING_PUBLISH_WHERE` = **0 条**
    · 其中 **84 行** `status='NO_TORRENT_FILE'`、但 `torrent_file` **非空**、
      盘上文件**在**、`mapped_json` 也**已补全**（72 条 `transfer_rule='ALLOW'`）
    ⇒ **一个"补数据"的工具，副作用是把行从队列里踢出去，且不报任何错。**

★ 那个"怕改 infohash"的顾虑**在本脚本里根本不存在**：本脚本**不读 qB、
  不重建任何 `.torrent`、不写任何文件**，只把一个**已经写在库里**的路径
  交给 `build_row` 去 `os.path.exists()` 核一下（在容器里核，见下）。

★★ 本脚本是**一次性数据修复**，不是常驻工具。
   根因已在上游修掉（`topup_opencd_fields.py` 现在会带回库里已有路径）
   ⇒ 修完这 84 行之后，本脚本**应当再也不需要跑**。

═══════════════════════════════════════════════════════════════════════════
★★★ 为什么必须在**容器内**跑（本机跑会**误判**，且方向是"多报可修"）
═══════════════════════════════════════════════════════════════════════════
`torrent_file` 存的是**容器内路径**（如 `/data/output_torrents/…torrent`）。
本机（Windows）跑 `os.path.exists('/data/…')` **恒为 False** ——
那不是"文件不在"，是**本机看不见那个命名空间**（与 SMB 挂载点是两码事）。

★ 本项目**已经栽过一次同类**（ERRATA：宿主机查 `torrent_file` 全部
  `exist=False`，进容器一查 16/16 全在）。⇒ 判据必须**在能看见那套路径的地方**跑。

⇒ 本脚本默认**核盘**（`--verify-files` 默认开）；核不过的行**不算可修**、
  且**明确报出来**（`ERR-META-14`：兜底值与真实结果分开报）。

═══════════════════════════════════════════════════════════════════════════
三条纪律
═══════════════════════════════════════════════════════════════════════════
1. **默认只读**（不带 `--apply` 只预览）；真正写要 `--apply`
   **且** `--i-have-stopped-the-container`。
   ★ 必须**先停容器** —— 两个写者写同一个 SQLite 是 `ERR-DOCKER-03`。
   ★★ 那两条声明**不是验证**：脚本**无从检查**容器是否真停。
      必须先用命令确认 `exited`（README 的停止纪律）。

2. ★★ **只改 `status` 这一列**。绝不碰 `torrent_file` / `content_path` /
   `mapped_json` / `transfer_rule` / `log_content` / `opencd_*` / `site_status`。
   ★ 也绝**不写** `site_publish` —— 那张表记的是"发过哪个站"，本步与此无关。

3. ★★★ **判据必须窄到"改了真有用"**，不许"看着像就改"：
   只收 `status='NO_TORRENT_FILE'` ∧ `torrent_file` 非空 ∧ **盘上确实在**
   ∧ `site_status != 'EXISTS'`（已上站的改了不会重发，是**无效功**）。
   ★ `reason` 一列**同时清掉**（它记的是旧结论"没种子文件"，已不成立）——
     留着就是一句**看着可信的错话**（`ERR-META-03`）。

用法（**容器内**；容器已停）:
    python tools/fix_no_torrent_file_status.py --db /data/data.db        # 只读预览
    python tools/fix_no_torrent_file_status.py --db /data/data.db \\
        --apply --i-have-stopped-the-container
    python tools/fix_no_torrent_file_status.py --db /data/data.db --no-verify-files
        # ★ 跳过核盘（不推荐；只在盘不可达时用，且会**多报**可修条数）
"""
import argparse
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

# ★ 默认走 NAS 上的生产库（SMB）。容器内请显式传 `--db /data/data.db`。
LOCAL_DB = r"//iSunker-DS423/docker_ssd/opencd2others/data/data.db"

# ★ 唯一允许改的列。其余一概不碰 —— 尤其 `torrent_file` / `site_status`。
SET_COLS = ("status", "reason")

TARGET_WHERE = (
    # ① 只收这一个状态 —— 别的状态有别的成因，不归本脚本管
    "status = 'NO_TORRENT_FILE' "
    # ② 库里必须记着一个路径（空 = 如实说"不知道"，不能凭"盘上可能有"去改）
    "AND torrent_file IS NOT NULL AND torrent_file != '' "
    # ③ 未上站 —— 已上站的改了到不了队列（`PENDING_PUBLISH_WHERE` 排掉 EXISTS），
    #    那是**无效功**（同 `topup_opencd_fields.PICK_WHERE` 的分寸）
    "AND (site_status IS NULL OR site_status != 'EXISTS') "
    # ④ 有内容 —— 没补过内容的行即使 status 对了，闸门③ 也会拦，
    #    改了就是"看着像修了、其实还是发不出去"。★ 这一条是**防误报**的。
    "AND mapped_json IS NOT NULL AND mapped_json != ''"
)

# ★ `reason` 列在改 status 时**必须一起清**：它记的是旧结论
#   （"调用方未传 torrent_file（None）⇒ …"）。留着它与新 `status` 矛盾。
NEW_REASON = ""


def _cols(cur, table):
    return [r[1] for r in cur.execute(f"PRAGMA table_info({table})")]


def main():
    ap = argparse.ArgumentParser(
        description="把其实有 .torrent 的 NO_TORRENT_FILE 行改回 SUCCESS")
    ap.add_argument("--apply", action="store_true", help="真正写库")
    ap.add_argument("--i-have-stopped-the-container", action="store_true",
                    help="★ 按纪律：必须显式声明容器已停（ERR-DOCKER-03）")
    ap.add_argument("--db", default=LOCAL_DB)
    ap.add_argument("--only", default="",
                    help="★ 只处理这些 info_hash（逗号分隔）——「先试两条」用")
    ap.add_argument("--no-verify-files", action="store_true",
                    help="★ 跳过核盘（不推荐；会**多报**可修条数）")
    args = ap.parse_args()

    only = tuple(s.strip().lower() for s in args.only.split(",") if s.strip())

    # ★ 写库前的守卫一律放最前面（顺序错了就成了"先写、再报拒"）
    if args.apply and not args.i_have_stopped_the_container:
        sys.exit("❌ 拒绝写库：必须同时给 --i-have-stopped-the-container（纪律 1）")

    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA query_only=0" if args.apply else "PRAGMA query_only=1")

    cols = _cols(conn, "tasks")
    for c in ("status", "reason", "torrent_file", "mapped_json", "site_status",
              "transfer_rule", "opencd_tid"):
        if c not in cols:
            sys.exit(f"❌ tasks 表缺列 {c!r} —— 库不对，停止")

    sql = (f"SELECT info_hash, status, torrent_file, transfer_rule, opencd_tid, "
           f"       formatted_title "
           f"  FROM tasks WHERE {TARGET_WHERE}")
    rows = list(conn.execute(sql))
    if only:
        rows = [r for r in rows if str(r[0]).lower() in only]

    # ── 核盘（★ 必须在容器内跑才有意义）──
    n_missing, n_ok = 0, []
    missing_samples = []
    for r in rows:
        tf = str(r[2])
        if args.no_verify_files:
            n_ok.append(r)
            continue
        if os.path.exists(tf):
            n_ok.append(r)
        else:
            n_missing += 1
            if len(missing_samples) < 5:
                missing_samples.append(tf)

    total = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    n_notf = conn.execute(
        "SELECT count(*) FROM tasks WHERE status='NO_TORRENT_FILE'").fetchone()[0]

    print("=" * 74)
    print("★ 把「其实有 .torrent」的 NO_TORRENT_FILE 行改回 SUCCESS（**只改 status**）")
    print("=" * 74)
    print(f"  库              : {args.db}")
    if only:
        print(f"  ★ 只处理(--only) : {list(only)}")
    print(f"  总行数          : {total}")
    print(f"  status='NO_TORRENT_FILE' 总行数 : {n_notf}")
    print(f"  符合目标口径(TARGET_WHERE)     : {len(rows)}")
    if not args.no_verify_files:
        print(f"  ├ 盘上**确实在**  ⇒ 可改       : {len(n_ok)}")
        print(f"  └ 盘上**找不到**  ⇒ **不改**   : {n_missing}")
        for tf in missing_samples:
            print(f"       {tf}")
        if n_missing:
            print("    ★★ 这批**不碰** —— 它们是真的没有可用 .torrent，"
                  "改 status 是**谎报**（ERR-META-14）")
    else:
        print("  ⚠️ 已跳过核盘 ⇒ 上面那个数**未经盘上核对**，可能多报")

    if not n_ok:
        print("\n✅ 没有要改的。")
        conn.close()
        return 0

    print(f"\n【改动内容（**只这两列**）】")
    print(f"    status : 'NO_TORRENT_FILE' → 'SUCCESS'")
    print(f"    reason : <旧值> → ''   ★ 必须同清（旧 reason 说「没种子文件」，已不成立）")
    print(f"\n【★ 明确**不碰**的列】")
    print(f"    torrent_file / content_path / mapped_json / transfer_rule /")
    print(f"    log_content / opencd_tid / opencd_added_at / site_status / updated_at")

    # ★ 改完会发生什么 —— 按**唯一来源**（db.PENDING_PUBLISH_WHERE）算，不手抄
    import ast
    import pathlib
    pw = None
    for f in ("db.py", "db.py"):
        if not pathlib.Path(f).exists():
            continue
        for n in ast.parse(pathlib.Path(f).read_text(encoding="utf-8")).body:
            if isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "PENDING_PUBLISH_WHERE"
                    for t in n.targets):
                try:
                    pw = ast.literal_eval(n.value)
                except Exception:
                    pw = None
    print("\n" + "─" * 74)
    if pw is None:
        print("★★ 读不到 `db.PENDING_PUBLISH_WHERE` ⇒ **本条不猜**（ERR-META-14）：")
        print("   「改完会有几条进队列」请用 `python check_db.py` 的队列口径自己核。")
    else:
        ihs = [str(r[0]) for r in n_ok]
        ph = ",".join("?" * len(ihs))
        pw_t = (pw.replace("site_status", "t.__S__").replace("status", "t.status")
                  .replace("opencd_added_at", "t.opencd_added_at")
                  .replace("transfer_rule", "t.transfer_rule")
                  .replace("opencd_tid", "t.opencd_tid")
                  .replace("mapped_json", "t.mapped_json")
                  .replace("t.__S__", "t.site_status"))
        # ★★ 「改后」必须**把 status 那一格按改后的值代进去**再算 ——
        #   否则拿改前的行（status='NO_TORRENT_FILE'）去套队列口径，
        #   第一句 `status='SUCCESS'` 恒为假 ⇒ **恒得 0**，
        #   而那是个**看着像结论的假判据**（2026-09-28 本工具的初版就犯了这个错：
        #   盘上查到 79/79 全在，却报"改完 0 条进队列"，与手算的 72 条矛盾）。
        #   ⇒ 唯一的改法：把口径里那一句 `t.status = 'SUCCESS'` 换成**恒真**。
        #     ★ 不许"猜"还有哪一句要改 —— 只动这一句，其余**逐字保留**。
        pw_after = pw_t.replace("t.status = 'SUCCESS'", "1=1")
        if pw_after == pw_t:
            print("★★ 口径里没找到 `t.status = 'SUCCESS'` ⇒ **本条不猜**（ERR-META-14）：")
            print("   说明 `PENDING_PUBLISH_WHERE` 已改过形状，请人工核。")
            n_after = None
        else:
            n_after = conn.execute(
                f"SELECT count(*) FROM tasks t WHERE t.info_hash IN ({ph}) "
                f"  AND ({pw_after})", ihs).fetchone()[0]
        n_allow = sum(1 for r in n_ok if (r[3] or "") == "ALLOW")
        if n_after is not None:
            print(f"★★ 连带后果：这 {len(n_ok)} 条里 **{n_after} 条**改完会满足发布队列口径")
            print(f"   （口径 = db.PENDING_PUBLISH_WHERE，**从源码抽的**；"
                  f"只把 `status='SUCCESS'` 按改后值代入，其余逐字未动）")
            print(f"   其中 transfer_rule='ALLOW' 的 {n_allow} 条；")
            print(f"   其余会被闸门④ 拦下（DENY / 空）——**不发**，但 status 更真实。")
            print(f"   ⇒ publish 会按 PUBLISH_MAX_PER_CYCLE 每轮发一批上乌托邦。")
        # ★ 交叉校验：n_after 应当 == ALLOW 且过其余闸门的条数。
        #   不等就**说明还有别的闸门在拦**，必须报出来（不合并成一个数）。
        if n_after is not None and n_after != n_allow:
            print(f"   ★ 注意：{n_after} ≠ ALLOW 的 {n_allow} ⇒ 还有闸门在拦：")
            for lbl, c in [("闸门② added_at 缺/不足7天",
                            "t.opencd_added_at IS NULL OR t.opencd_added_at='' "
                            "OR t.opencd_added_at > datetime('now','-7 days')"),
                           ("闸门③ tid有但mapped_json空",
                            "t.opencd_tid IS NOT NULL AND t.opencd_tid!='' "
                            "AND (t.mapped_json IS NULL OR t.mapped_json='')")]:
                k = conn.execute(
                    f"SELECT count(*) FROM tasks t "
                    f" WHERE t.info_hash IN ({ph}) AND t.transfer_rule='ALLOW' "
                    f"   AND ({c})", ihs).fetchone()[0]
                print(f"       {k} 条 被 {lbl} 拦下")
    print("─" * 74)

    if not args.apply:
        print("\n💡 预览模式，未写库。确认后：")
        print(f"     python tools/fix_no_torrent_file_status.py --db {args.db} \\")
        print("         --apply --i-have-stopped-the-container")
        conn.close()
        return 0

    # ---- 写 ----
    n = 0
    try:
        for r in n_ok:
            cur = conn.execute(
                "UPDATE tasks SET status='SUCCESS', reason=? "
                " WHERE info_hash=? AND status='NO_TORRENT_FILE'",
                (NEW_REASON, r[0]))
            n += cur.rowcount
        conn.commit()
        print(f"\n💾 已改：{n} 条")
        after_notf = conn.execute(
            "SELECT count(*) FROM tasks WHERE status='NO_TORRENT_FILE'").fetchone()[0]
        print(f"   查后复核（应相等）:")
        print(f"     NO_TORRENT_FILE : {n_notf} → {after_notf}"
              f"   （应 = {n_notf} - {n} = {n_notf - n}）")
        print(f"     总行数仍为      : "
              f"{conn.execute('SELECT count(*) FROM tasks').fetchone()[0]}"
              f"   （应 = {total}，**一行都没删**）")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
