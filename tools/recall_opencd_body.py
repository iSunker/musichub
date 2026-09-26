"""★ 零配额：把 `raw_html/<tid>.html` 里的**皇后简介正文**补进库（`opencd_descr`）。

═══════════════════════════════════════════════════════════════════════════
它解决的那个具体问题
═══════════════════════════════════════════════════════════════════════════
`descr`（皇后 `divdescr` 正文）**从未落库** —— `opencd_parse.parse_detail()`
一直从页面读到它（`info["descr"]`），但 `opencd_map.map_all()` 原先不返回它，
`build_row()` 也因此写不进去。实测（2026-09-26）：58 条 `mapped_json` 里
`descr` 键出现 **0** 次（连 `unmapped` 都没有它）。

⇒ 站上的简介一直是 `descr.build_descr()` **现渲染**的（封面+曲目+日志），
  与皇后正文无关。用户要的是"和皇后一致"。

★ 代码侧已修（`map_all` 已透传 `descr`），但那只对**以后新抓的**行生效。
  已经抓过的 58 条**不会自己变** —— 需要本脚本回填一次。

═══════════════════════════════════════════════════════════════════════════
为什么这个脚本**不花一个请求**
═══════════════════════════════════════════════════════════════════════════
原始页面**已经在本机** `raw_html/<tid>.html` 里（实测 58/58 全部还在，
`topup_opencd_fields.py` 落盘时就存了）。⇒ 本脚本**只读本机文件**：
不 import requests、不 import crawl、不碰 RateGate / opencd_quota。

★ 与 `recall_opencd_descr.py` 的分工（**别混**）：
  · `recall_opencd_descr.py` → 从 **`mapped_json` 快照**里取 `small_descr` 回写列。
    （**副标题**那条路；快照里已经有值了。）
  · **本脚本**               → 从 **`raw_html/*.html`** 里重新解析 `divdescr`。
    （**简介正文**那条路；快照里**没有**它，只能回页面重新解析。）
  ⇒ 判据：**"快照里有没有"决定了从哪儿取** —— 有快照就别碰页面，没快照才回页面。

═══════════════════════════════════════════════════════════════════════════
五条纪律
═══════════════════════════════════════════════════════════════════════════
1. **默认只读**（`--dry-run` 是默认值）：不带 `--apply` 只打印"会补哪几条"。

2. ★★ **只写 `opencd_descr` 一列**（外加可选的 `updated_at` 标脏）。
   绝不写 `status` / `site_status` / `mapped_json` ——
   本脚本是**补字段**，不是**重判**。尤其 `status`：那是闸门的结论，
   回填一列正文没有资格推翻它（`db.PROTECTED_STATUSES` 的同一条道理）。

3. ★★ **不用 `db.save_task()`** —— 那是 upsert，会按 `info_hash` 覆盖。
   本脚本直接 `UPDATE ... WHERE info_hash = ?`，与
   `tools/backfill_opencd_tid.py` / `tools/mark_no_forward.py` 同一纪律。

4. ★ **幂等**：`WHERE opencd_descr IS NOT ?`（SQLite 的 `IS NOT` 能正确处理
   NULL）⇒ 跑第二遍是空操作，不会白刷 `updated_at`。

5. **解析走 `opencd_parse.parse_detail()`**，**不自己写正则** ——
   否则就是"同一判据两处实现"，必然漂移（`ERR-META-03`）。
   本脚本只负责"读文件 → 调它 → 写库"这三件杂事。

6. ★★ **标脏是显式的、且范围 = 本次真改了的行**（同 `recall_opencd_descr.py` 纪律 7）：
   不带 `--mark-stale` ⇒ `updated_at` 一个都不碰 ⇒ **站点不会更新**。
   带 `--mark-stale` ⇒ 只标本次 `rowcount=1` 的行。

7. **写库前必须确认容器已停**（`ERR-DOCKER-03`）：
   `--i-have-stopped-the-container` 是**声明，不是验证**。

用法:
    python tools/recall_opencd_body.py --dry-run                    # 只看（默认）
    python tools/recall_opencd_body.py --apply --i-have-stopped-the-container
    python tools/recall_opencd_body.py --apply --i-have-stopped-the-container \\
        --mark-stale                                                # ★ 并让 edit 推上站
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.getenv("DATA_DIR", "/data")
DB_NAME = "data.db"

# ★ 目标口径：**有** `mapped_json`（= 抓过页面）且有 `opencd_tid` 的皇后行。
#   ⚠️ 与 `recall_opencd_descr.py` 的 `TARGET_WHERE` **故意一致** ——
#      两者都是"皇后行 + 抓过页面"，只是补的列不同。若哪天要改口径，
#      **两处一起改**（或抽成公共常量）—— 已在此显式记下这个义务。
TARGET_WHERE = (
    "opencd_tid IS NOT NULL AND opencd_tid != '' "
    "AND mapped_json IS NOT NULL AND mapped_json != ''"
)


def resolve_raw_dir(explicit=None):
    """`raw_html` 目录。★ 默认按 `crawl.RAW_DIR` 的同一口径算，别自己拍一个。"""
    if explicit:
        return explicit
    data_dir = os.getenv("DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "raw_html")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "raw_html")


def resolve_db(explicit=None):
    if explicit:
        return explicit
    data_dir = os.getenv("DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, DB_NAME)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, DB_NAME)


def load_targets(con):
    """有快照的皇后行：`info_hash` / `opencd_tid` / `torrent_name` / 现有 `opencd_descr`。

    ★ 现有值**也 SELECT** —— 预览要能显示"旧值 → 新值"。这与
      `recall_opencd_descr.py` 里那个"预览恒显示 None"的 bug 是同一处教训：
      **写库工具的预览把旧值印错，比不预览更危险**（`ERR-META-01`）。
    """
    cur = con.execute(
        f"SELECT info_hash, opencd_tid, torrent_name, opencd_descr "
        f"FROM tasks WHERE {TARGET_WHERE} ORDER BY torrent_name"
    )
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def collect(con, raw_dir):
    """读本机 HTML → 解析出正文。返回 `(待改行, 缺文件, 解析空, 坏文件)`。

    ★ 四类结果**分开报**，不合并成"有问题 N 条"——它们的处置完全不同：
        缺文件   ⇒ 得重新抓（花配额）或放弃
        解析空   ⇒ 该页本来就没写正文（正常，不是错误）
        坏文件   ⇒ HTML 损坏 / 不是详情页 ⇒ **要查**
    """
    import opencd_parse

    todo, no_file, empty, broken = [], [], [], []
    rows = load_targets(con)
    for r in rows:
        tid = str(r["opencd_tid"])
        p = os.path.join(raw_dir, f"{tid}.html")
        if not os.path.exists(p):
            no_file.append(r)
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                html = f.read()
            info = opencd_parse.parse_detail(html, source_file=p)
            body = (info.get("descr") or "").strip()
        except Exception as e:                      # noqa: BLE001
            broken.append((r, f"{type(e).__name__}: {e}"))
            continue
        if not body:
            empty.append(r)
            continue
        if (r.get("opencd_descr") or "").strip() == body:
            continue                                # ★ 幂等：已经是这个值，不动
        r["_new"] = body
        todo.append(r)
    return todo, no_file, empty, broken


def apply_rows(con, rows):
    """只 UPDATE `opencd_descr`。返回真的被改动的 info_hash 列表。

    ★ 用 `cur.rowcount` 判"真的改了"而不是"跑过了"（`recall_opencd_descr.py`
      纪律 7 的同一条理由：标脏范围必须 = 真改了的行）。
    ★ `WHERE ... IS NOT ?` 让本函数**自身**也幂等（万一 `collect` 的过滤被绕过）。
    """
    changed = []
    for r in rows:
        cur = con.execute(
            "UPDATE tasks SET opencd_descr = ? "
            "WHERE info_hash = ? AND opencd_descr IS NOT ?",
            (r["_new"], r["info_hash"], r["_new"]))
        if cur.rowcount:
            changed.append(r["info_hash"])
    con.commit()
    return changed


def mark_stale(con, info_hashes):
    """把**本次真的改过的**行刷 `updated_at` ⇒ 满足 `db.STALE_CONDITION`。

    ★ 只动 `updated_at`，不动 `site_synced_at`（理由见
      `recall_opencd_descr.py` 的 `mark_stale()`：后者会**伪造**"从没同步过"）。
    """
    n = 0
    for ih in info_hashes:
        cur = con.execute(
            "UPDATE tasks SET updated_at = CURRENT_TIMESTAMP WHERE info_hash = ?",
            (ih,))
        n += cur.rowcount
    con.commit()
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="皇后简介正文回填（零配额，读本机 HTML）")
    ap.add_argument("--db", default=None, help=f"库路径（默认 {DATA_DIR}/{DB_NAME}）")
    ap.add_argument("--raw-dir", default=None, help="raw_html 目录（默认 $DATA_DIR/raw_html）")
    ap.add_argument("--dry-run", action="store_true", help="只打印（**默认**，可省略）")
    ap.add_argument("--apply", action="store_true", help="真写库（默认只打印）")
    ap.add_argument("--i-have-stopped-the-container", action="store_true",
                    help="★ 声明容器已停 —— **这是声明，不是验证**（ERR-DOCKER-03）")
    ap.add_argument("--mark-stale", action="store_true",
                    help="★ 把**本次真的改过的行**刷 updated_at ⇒ edit 会把它推上站。"
                         "**默认不标**（站点不会自动更新）。")
    ap.add_argument("--show", type=int, default=15, help="预览里最多打印几条正文")
    args = ap.parse_args(argv)

    db_path = resolve_db(args.db)
    raw_dir = resolve_raw_dir(args.raw_dir)
    if not os.path.exists(db_path):
        print(f"✗ 找不到库：{db_path}", file=sys.stderr)
        return 1
    if not os.path.isdir(raw_dir):
        print(f"✗ 找不到 raw_html 目录：{raw_dir}", file=sys.stderr)
        print("  ★ 没有它，本脚本无法工作（它的全部依据就是那些 .html）。", file=sys.stderr)
        return 1

    if args.apply and not args.i_have_stopped_the_container:
        print("✗ 拒绝写库：缺 `--i-have-stopped-the-container`。", file=sys.stderr)
        print("  ★ 那是**声明**；请先用命令确认容器 `exited`（ERR-DOCKER-03）。",
              file=sys.stderr)
        return 2

    con = sqlite3.connect(db_path)
    if not args.apply:
        con.execute("PRAGMA query_only=1")

    # ★ 表存在性必须查（`ERR-META-01`：空库会让所有 count 返回 0，看着像"没事了"）
    have = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "tasks" not in have:
        print(f"✗ {db_path} 里没有 `tasks` 表（现有：{sorted(have) or '无'}）", file=sys.stderr)
        return 3
    cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
    if "opencd_descr" not in cols:
        print("✗ tasks 里没有 `opencd_descr` 列 —— 先让新代码跑一次 `db.init_db()`",
              file=sys.stderr)
        print("  （它会自动 ALTER TABLE 补列）。", file=sys.stderr)
        return 3

    todo, no_file, empty, broken = collect(con, raw_dir)

    print("=" * 72)
    print(f"库            : {db_path}")
    print(f"raw_html      : {raw_dir}")
    print(f"模式          : {'★ APPLY（真写）' if args.apply else 'dry-run（只读）'}")
    print(f"标脏(updated_at): {'★ 开（只标本次改动的行）' if args.mark_stale else '关（站点不会自动更新）'}")
    print("-" * 72)
    print(f"待补行数      : {len(todo)}   （有快照的皇后行里，正文与库内不一致的）")
    print(f"缺 HTML 文件  : {len(no_file)}   （★ 这些补不了，要重抓=花配额）")
    print(f"页面本来无正文: {len(empty)}   （正常 —— 皇后那条就没写）")
    if broken:
        print(f"⚠ 解析异常    : {len(broken)}   （★ 这不是'没问题'，是要查的）")
        for r, err in broken[:10]:
            print(f"     {str(r.get('torrent_name'))[:44]}  {err}")
    print("-" * 72)

    for r in todo[:args.show]:
        old = (r.get("opencd_descr") or "").strip()
        new = r["_new"]
        print(f"  {str(r.get('torrent_name'))[:56]}")
        print(f"     旧: {(old[:60] + '…') if len(old) > 60 else (old or '(空)')!r}")
        print(f"     新: {(new[:60] + '…') if len(new) > 60 else new!r}  ({len(new)} 字符)")
    if len(todo) > args.show:
        print(f"  … 另有 {len(todo) - args.show} 条")

    if not args.apply:
        print("\n[dry-run] 未写库。真写：--apply --i-have-stopped-the-container")
        return 0

    changed = apply_rows(con, todo)
    left = con.execute(
        f"SELECT count(*) FROM tasks WHERE {TARGET_WHERE} "
        f"AND (opencd_descr IS NULL OR opencd_descr = '')"
    ).fetchone()[0]
    print(f"\n✅ 已写 {len(changed)} 行")
    # ★ 这个数**不是**"还剩多少没补"的完整口径：它包含"页面本来就没正文"的那些。
    #   两个数别相减（`ERR-META-05`）。
    print(f"   库内 `opencd_descr` 仍为空 = {left} 条"
          f"（含「页面本来就没有正文」的 {len(empty)} 条 + 缺文件的 {len(no_file)} 条）")

    if args.mark_stale:
        n = mark_stale(con, changed)
        if not changed:
            # ★ 必须明说，否则"已标脏 0 行"会被读成"没问题"（`ERR-META-01` 同族）
            print("★ 已标脏 0 行 —— ⚠️ **站点不会更新**（本次没有行被改动）。")
        else:
            print(f"★ 已标脏 {n} 行（updated_at）⇒ edit 阶段将把它们推上站")
    else:
        print("ℹ️  未标脏（默认）⇒ 站点**不会**自动更新。"
              "要推上站请加 `--mark-stale`。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
