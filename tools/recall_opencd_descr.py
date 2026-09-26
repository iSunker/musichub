"""★ 零配额数据清洗：把 `mapped_json` 里的皇后原文**回写**到渲染真正读的那几列。

═══════════════════════════════════════════════════════════════════════════
它解决的那个具体问题（ERR-DATA-01）
═══════════════════════════════════════════════════════════════════════════
皇后页面的字段被抓下来后，落进了 `mapped_json`（**证据快照**），
但**渲染读的是真列**（`descr.build_small_descr_for_task` 读 `task["small_descr"]`）。

⇒ 补页面那一步**只写了 `mapped_json`，没回写列** ⇒ 站上副标题长期是「全 N 曲」，
  而皇后原文（`專輯 | VP Records / VPCD 1793`）**一直躺在库里没被用过**。

实测（2026-09-26）：有 `mapped_json` 的 58 条里，**56 条**的
`tasks.small_descr` 与 `mapped_json.small_descr` 不一致。

═══════════════════════════════════════════════════════════════════════════
为什么这个脚本**不花一个请求**
═══════════════════════════════════════════════════════════════════════════
皇后原文**已经在 `mapped_json` 里**了（那就是"抓过页面"的凭证）。
本脚本**纯本地、只读 `mapped_json`、只写回列** ⇒ 不 import requests、
不碰 `crawl.Client`、不碰 `RateGate`、不碰 `opencd_quota`。

★ 与 `topup_opencd_fields.py` 的分工（**别混**）：
  · `topup_opencd_fields.py` → `mapped_json` **空**的行 ⇒ **必须抓页面**（花配额）。
  · **本脚本**               → `mapped_json` **非空**的行 ⇒ **零配额**，只是回写。

═══════════════════════════════════════════════════════════════════════════
六条纪律
═══════════════════════════════════════════════════════════════════════════
1. **默认只读**（`--dry-run` 是默认值）：不带 `--apply` 只打印"会改哪几条、改成什么"。

2. ★★ **只回写「从 `mapped_json` 能确定推导出来」的列** —— 这一条是本脚本的
   **安全边界**，不是保守。可推导的只有：
     · `small_descr` ← `mapped_json.small_descr`（皇后副标题原文，**直接取**）
   ★ **不碰 `formatted_title`**：它**不是**从 `mapped_json` 推导的 ——
     实测 `mapped.title` 与 `formatted_title` **56/58 不相等**，两者是**不同的东西**
     （`mapped.title` 是皇后原文标题，`formatted_title` 是站点侧的形态）。
     ⇒ 拿 `mapped.title` 去覆盖 `formatted_title` 会**改坏标题**（`ERR-META-03` 的味道：
       两处各写一份口径，必漂移）。**要修标题是另一件独立的事**，不在本脚本。

3. ★★ **绝不碰 `updated_at`**（照抄 `sync_opencd_tid.py` 纪律 3 的理由）：
   `db.STALE_CONDITION` 由 `updated_at` 驱动，而那是 `edit` 阶段的待办探针。
   顺手刷 `updated_at` ⇒ 这几百条**全变 stale** ⇒ `edit` 每轮把整站页面重拉一遍。
   ⇒ 本脚本**只 UPDATE `small_descr` 一列**，自检里有一条专门钉它。
   ★ 代价（必须知情，与 `sync_opencd_tid.py` 同一个）：
     不刷 `updated_at` ⇒ **已上站的行不会自动推上去**。
     要重推是**另一件独立的事**（显式标 stale + 让 `edit` 跑），本脚本不做。
   ★★ 2026-09-26 补充：上面那条"重推是另一件事"现在有了**显式开关** ——
      `--mark-stale`（见纪律 7）。**默认仍然不碰** `updated_at`；
      只有显式敲那个开关、且那正是本次的意图时才标。两条要点：
        · 不标脏 ⇒ 站点**永远**不会更新（这曾经是"回写了 56 条、站上却好像没变"
          的成因之一，与 `merge_mapped` 漏 `small_descr` 是**两个**独立的洞）；
        · 一律标脏 ⇒ 整站被 `edit` 重拉一遍（纪律 3 要防的正是这个）。
      ⇒ **判据：标脏的范围必须 = 本次真的改了的行**（不是全表、也不是全不标）。

4. ★ **只改"确实不一致"的行**（`WHERE ... IS NOT`）：幂等 ⇒ 跑第二遍是空操作。

5. **SQL 侧算、Python 侧只决策**：`--dry-run` 打印的明细就是 SQL 选出来的同一批，
   **不另写一套查询**（`ERR-META-03`：同一判据两处实现必然漂移）。

6. **写库前必须确认容器已停**（`ERR-DOCKER-03`）：
   `--i-have-stopped-the-container` 是**声明，不是验证**（脚本无法检查容器停没停）。
   ⇒ 真停容器要**用命令确认到 `exited`** 再往下，别靠"用户说停了"。

7. ★★ **标脏是显式的、且范围 = 本次真改了的行**（2026-09-26 加）：
   不带 `--mark-stale` ⇒ 行为与改动前**一字不差**（`updated_at` 一个都不碰）。
   带 `--mark-stale` ⇒ 只对**本次 `apply_rows` 真的 UPDATE 成功**的那些 info_hash
   刷 `updated_at`，让它们满足 `db.STALE_CONDITION` ⇒ `edit` 阶段才会推上站。
   ★ 为什么范围要这么窄：`edit` 的待办探针就是 `db.count_stale()`，
     而它按 `updated_at > site_synced_at` 数 ⇒ **标多宽，edit 就重拉多宽的页面**。
     标全表 = 把整站重拉一遍（纪律 3 要防的）；一条不标 = 站点永不更新。
   ★ 为什么不做成默认：默认改行为会**静默**把 280 条变成 `edit` 的待办 ——
     那正是本项目反复吃亏的"顺手多做了没人要求的事"（见 `descr.build_form_fields`
     里 `anonymous` 那条同族注释）。

用法:
    python tools/recall_opencd_descr.py --dry-run          # 只看会改哪几条（默认）
    python tools/recall_opencd_descr.py --apply --i-have-stopped-the-container
    python tools/recall_opencd_descr.py --apply --i-have-stopped-the-container \
        --mark-stale                                       # ★ 并让 edit 把它推上站
"""
import argparse
import json
import os
import sqlite3
import sys

# ★ 解析层/渲染层都用这一份口径，别自己再写一遍（`ERR-META-03`）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.getenv("DATA_DIR", "/data")
DB_NAME = "data.db"

# ★ 目标口径：**有** `mapped_json`（= 抓过页面）的皇后行。
#   `opencd_tid` 非空是"这是皇后行"的判据（与 `descr.py` 三处分流同一个开关）。
TARGET_WHERE = (
    "opencd_tid IS NOT NULL AND opencd_tid != '' "
    "AND mapped_json IS NOT NULL AND mapped_json != ''"
)

# ★ 只回写这一列。**加列之前先读纪律 2** ——
#   这列的取值必须能从 `mapped_json` **直接取**，不需要再猜/再算。
COLUMN_SOURCE = {
    "small_descr": "small_descr",
}


def resolve_db(explicit=None):
    """定库路径。**只负责"算出路径"，不负责"路径对不对"** —— 后者在 `main()` 里。

    ★★ 为什么这里要特意写一段（2026-09-26 实测踩到）：
      原先直接 `os.path.join(os.getenv("DATA_DIR", "/data"), "data.db")` ——
      本机（**没设** `DATA_DIR`）会得到 `/data/data.db`，而那是**容器内**的路径，
      本机根本没有 ⇒ `sqlite3.connect` **会自己建一个空库** ⇒
      脚本报「目标行数 0 / 待改行数 0」，**看着像"数据已经干净了"**。
      ⇒ 这正是本项目反复吃亏的形状：**判据失效却给出一个合理的结论**
        （`ERR-META-01`；与 `ERR-DATA-01` 同族）。

    ★ 修法分**两处**，别把两处说成一件事（`ERR-META-13`：判据要说清它在哪个位置）：
      · **本函数**：把"本机没设 `DATA_DIR`"这一支从容器路径 `/data`
        改成**脚本所在项目的根目录** ⇒ 不再指向一个本机必然不存在的目录。
      · **`main()`**（真正的守卫在这）：
        ①`os.path.exists` 不通过 ⇒ 退出码 1；
        ② 库里**没有 `tasks` 表** ⇒ 退出码 3。
        ②才是最后一道闸 —— 即使路径存在，也可能是**空的/拿错的库**。

    ⇒ 所以：**本函数不做校验**；只看它返回的字符串，得不出"这个库能用"。
    """
    if explicit:
        return explicit
    data_dir = os.getenv("DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, DB_NAME)
    # 兜底：脚本所在项目的根目录（`tools/xxx.py` → 上一级）。
    #   ★ 容器内 `DATA_DIR=/data` 已经命中上面那支；这一支是给**本机**用的。
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, DB_NAME)


def load_candidates(con):
    """选出"确实不一致"的行。返回值里同时带上**旧值**和**新值**（dry-run 要用）。"""
    src_cols = ", ".join(
        f"json_extract(mapped_json, '$.{src}') AS _new_{col}"
        for col, src in COLUMN_SOURCE.items()
    )
    # ★★ 当前列值**也要 SELECT**（2026-09-26 修）—— 原先只 SELECT `_new_*`，
    #    于是 dry-run 打印的 `old` 恒为 `None`（`r.get(col)` 取不到键），
    #    实测显示成 `small_descr: None → '專輯 | VP Records / VPCD 1793'`，
    #    而库里**其实是** `'全 15 曲'`。
    #    ⇒ 危害：这是**写库工具**的预览，而预览把"旧值"印成 None ⇒
    #      人看到的是"这列本来是空的、要填上"，**看不到**它其实在覆盖一个错值
    #      （`ERR-META-01`：检测/预览失效比不预览更危险 —— 它会给出一个
    #       看着合理的错结论，而这次错的是"改了什么"这个最该看清的东西）。
    cur_cols = ", ".join(f"tasks.{col}" for col in COLUMN_SOURCE)
    where = " OR ".join(
        f"tasks.{col} IS NOT json_extract(tasks.mapped_json, '$.{src}')"
        for col, src in COLUMN_SOURCE.items()
    )
    sql = (f"SELECT info_hash, torrent_name, {cur_cols}, {src_cols} "
           f"FROM tasks WHERE {TARGET_WHERE} AND ({where})")
    cur = con.execute(sql)
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r)) for r in cur.fetchall()]

    # ★ 解析 `mapped_json` 失败的样本要**显式报出来**，不能静默当成"没问题"。
    #   （`ERR-META-01`：检测方法失效比不检测更危险。）
    broken = []
    for r in rows:
        for col in COLUMN_SOURCE:
            if r.get(f"_new_{col}") is None:
                broken.append((r["torrent_name"], col))
    return rows, broken


def count_targets(con):
    """全部目标行数（`TARGET_WHERE` 口径）—— 摘要里与"待改行数"**分开报**。"""
    return con.execute(
        f"SELECT count(*) FROM tasks WHERE {TARGET_WHERE}").fetchone()[0]


def apply_rows(con, rows):
    """只 UPDATE `COLUMN_SOURCE` 里的列，**绝不碰 `updated_at`**。

    返回**真的被改动的 info_hash 列表**（不是「试了几条」）——
    `--mark-stale` 要按这个集合标脏（纪律 7：标脏范围必须 = 真改了的行）。
    ★ 用 `cur.rowcount` 判"真的改了"而不是"跑过了"：`WHERE` 里的
      `mapped_json IS NOT NULL` 会让已删/已改的行落空，那些**不该**被标脏。
    """
    sets = ", ".join(f"{col} = ?" for col in COLUMN_SOURCE)
    sql = (f"UPDATE tasks SET {sets} "
           f"WHERE info_hash = ? AND mapped_json IS NOT NULL "
           f"AND mapped_json != ''")
    changed = []
    for r in rows:
        vals = [r.get(f"_new_{col}") for col in COLUMN_SOURCE]
        cur = con.execute(sql, vals + [r["info_hash"]])
        if cur.rowcount:
            changed.append(r["info_hash"])
    con.commit()
    return changed


def mark_stale_targets(con):
    """把**全部目标行**刷 `updated_at` ⇒ 让 `edit` 把它们**全部**重推一遍。

    ★ 为什么需要它（与 `mark_stale` 的区别，**别混**）：
      · `mark_stale(con, changed)` —— 标"**本次改动的**"行。适合"刚回写完"。
      · **本函数** —— 标"**全部目标**"行。适合"列**早就**对了、但站点从没被推过"。
      2026-09-26 生产实测正是后一种：`目标 58 / 待改 0` ⇒ `changed` 为空
      ⇒ `mark_stale` 一条都不标 ⇒ 站点永不更新。
    ★ 范围仍是 `TARGET_WHERE`（**只**皇后的、有 mapped_json 的行），
      **不含**抓轨路的行 —— 那些的副标题是本地算的，与皇后无关。
    ★ 返回真的被更新的行数（供打印；`rowcount` 是 SQLite 的真值，不是估算）。
    """
    cur = con.execute(
        f"UPDATE tasks SET updated_at = CURRENT_TIMESTAMP WHERE {TARGET_WHERE}")
    con.commit()
    return cur.rowcount


def mark_stale(con, info_hashes):
    """把**本次真的改过的**那些行刷 `updated_at` ⇒ 满足 `db.STALE_CONDITION`。

    ★ 只动 `updated_at`，不动 `site_synced_at` —— 判据是 `updated_at > site_synced_at`，
      我们靠"把左边推新"达标，而不是"把右边推旧"（后者会**伪造**"从没同步过"，
      那是另一件事，且会让 `edit` 认为这条从没被推上过站）。
    ★ 这里**内联** `db.STALE_CONDITION` 的驱动列名（`updated_at`）而不是 import `db`：
      本脚本的纪律是"不 import 项目的写库路径"（它只 UPDATE 一列 + 这一处标脏），
      且它要能在**容器外**、给定任意 `--db` 路径时跑。⇒ 若哪天 `STALE_CONDITION`
      换了驱动列，这里必须跟着改 —— 见文件顶部的纪律 7。
    """
    n = 0
    for ih in info_hashes:
        cur = con.execute(
            "UPDATE tasks SET updated_at = CURRENT_TIMESTAMP WHERE info_hash = ?",
            (ih,))
        n += cur.rowcount
    con.commit()
    return n


def verify(con):
    """回写后再数一次 —— 必须是 0（幂等 + 改对了）。"""
    where = " OR ".join(
        f"tasks.{col} IS NOT json_extract(tasks.mapped_json, '$.{src}')"
        for col, src in COLUMN_SOURCE.items()
    )
    return con.execute(
        f"SELECT count(*) FROM tasks WHERE {TARGET_WHERE} AND ({where})"
    ).fetchone()[0]


def main(argv=None):
    ap = argparse.ArgumentParser(description="皇后副标题回写（零配额）")
    ap.add_argument("--db", default=None, help=f"库路径（默认 {DATA_DIR}/{DB_NAME}）")
    # ★ `--dry-run` **显式声明**：它是默认模式，但必须能被明确敲出来 ——
    #   否则文档里写的用法会 `unrecognized arguments` 退出码 2（`ERR-META-18`：
    #   判据/接口要真跑一次，别只看它"应该能用"）。
    ap.add_argument("--dry-run", action="store_true", help="只打印（**默认**，可省略）")
    ap.add_argument("--apply", action="store_true", help="真写库（默认只打印）")
    ap.add_argument("--i-have-stopped-the-container", action="store_true",
                    help="★ 声明容器已停 —— **这是声明，不是验证**（ERR-DOCKER-03）")
    ap.add_argument("--mark-stale", action="store_true",
                    help="★ 把**本次真的改过的行**刷 updated_at ⇒ 满足 "
                         "db.STALE_CONDITION ⇒ edit 阶段会把它推上站。"
                         "**默认不标**（站点不会自动更新）；范围严格 = 本次改动的行。")
    # ★★ 2026-09-26 实测新增：`--mark-stale` **修不了**"库已经对了、只是从没推过"
    #    那种局面 —— 因为它只标"本次 rowcount=1"的行，而那种局面 `--apply` 是
    #    空操作（`待改 0`）⇒ `changed` 为空 ⇒ **一条都不标** ⇒ 站点**永远**不更新。
    #    ★ 这不是假设：2026-09-26 在生产库上实测到 `目标 58 / 待改 0` ——
    #      列**已经**被之前某次回写修对了，但那些行**从没标脏**过 ⇒
    #      用户看到的"站上副标题还是错的"**正是这个**。
    #    ⇒ 判据：**"需要标脏的集合"≠"本次改动的集合"**，两者只在"本次确实改了"
    #      时才重合。要覆盖后者，必须有一个**显式点名**的开关。
    ap.add_argument("--mark-stale-targets", action="store_true",
                    help="★ 把**全部目标行**（有 mapped_json 的皇后行）刷 updated_at "
                         "⇒ 让 edit 把它们**全部**重推一遍。"
                         "用在「列已经对了、但站点从没被推过」的局面。"
                         "★ 范围 = 全部目标行，**比 --mark-stale 宽** —— 会拉高 "
                         "edit 待办数（预期），但只影响皇后行、不动抓轨路。")
    ap.add_argument("--limit", type=int, default=0, help="最多改几条（0=不限）")
    args = ap.parse_args(argv)

    db_path = resolve_db(args.db)
    if not os.path.exists(db_path):
        print(f"✗ 找不到库：{db_path}", file=sys.stderr)
        return 1

    if args.apply and not args.i_have_stopped_the_container:
        print("✗ 拒绝写库：缺 `--i-have-stopped-the-container`。", file=sys.stderr)
        print("  ★ 那是**声明**；请先用命令确认容器 `exited`（ERR-DOCKER-03）。",
              file=sys.stderr)
        return 2

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA query_only=1") if not args.apply else None

    # ★★ 表存在性**必须**查（`ERR-META-01`）：一个空库 / 拿错的库
    #    会让下面所有 count 都返回 0 ⇒ 打印「目标 0 / 待改 0」⇒
    #    **看着像"数据已经干净了"**。那是假阴性，比报错危险得多。
    have = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "tasks" not in have:
        print(f"✗ {db_path} 里没有 `tasks` 表（现有表：{sorted(have) or '无'}）",
              file=sys.stderr)
        print("  ★ 这可能是个空库或拿错了路径 —— 不报 0 行，直接退出，"
              "免得把'读不到'当成'没问题'。", file=sys.stderr)
        return 3

    n_target = count_targets(con)
    rows, broken = load_candidates(con)

    # ★ 两个口径**分开报**，不许混着减（`ERR-META-05`）。
    print("=" * 72)
    print(f"库            : {db_path}")
    print(f"模式          : {'★ APPLY（真写）' if args.apply else 'dry-run（只读）'}")
    print(f"回写列        : {', '.join(f'{c} ← mapped.{s}' for c, s in COLUMN_SOURCE.items())}")
    # ★ 标脏是**另一个动作**，必须在摘要里单独说清它开没开 ——
    #   否则"回写成功"会被读成"站点也会更新"，而这两件事本脚本分得很开（纪律 3/7）。
    if args.mark_stale:
        _stale_note = "★ 开（只标本次改动的行）"
    elif args.mark_stale_targets:
        _stale_note = "★★ 开（标**全部目标行**）"
    else:
        _stale_note = "关（站点不会自动更新）"
    print(f"标脏(updated_at): {_stale_note}")
    print("-" * 72)
    print(f"目标行数      : {n_target}   （口径 = 有 mapped_json 的皇后行）")
    print(f"待改行数      : {len(rows)}   （口径 = 上面这批里确实不一致的）")
    if broken:
        print(f"⚠ 解析异常    : {len(broken)} 处 —— ★ 这不是'没问题'，是要查的")
        for name, col in broken[:10]:
            print(f"     {name[:56]}  [{col}]")
    print("-" * 72)

    if args.limit and len(rows) > args.limit:
        rows = rows[:args.limit]
        print(f"（--limit {args.limit} ⇒ 本轮只处理前 {len(rows)} 条）")

    for r in rows[:40]:
        old = {c: r.get(c) for c in COLUMN_SOURCE}
        new = {c: r.get(f"_new_{c}") for c in COLUMN_SOURCE}
        print(f"  {r['torrent_name'][:52]}")
        for c in COLUMN_SOURCE:
            print(f"     {c}: {old[c]!r} → {new[c]!r}")
    if len(rows) > 40:
        print(f"  … 另有 {len(rows) - 40} 条（同上形态）")

    if not args.apply:
        print("\n[dry-run] 未写库。真写：--apply --i-have-stopped-the-container")
        return 0

    changed = apply_rows(con, rows)
    left = verify(con)
    print(f"\n✅ 已写 {len(changed)} 行；回写后仍不一致 = {left}（应为 0）")

    # ★★ 标脏**范围**必须 = 上面**真的改动**的那些行（纪律 7）。
    #    不是 `rows`（那是"选中要改的"，可能含 rowcount=0 的），更不是全表。
    if args.mark_stale:
        n_stale = mark_stale(con, changed)
        if not changed:
            # ★ 这个分支**必须**明说，否则"已标脏 0 行"会被读成"没问题"
            #   —— 而它真正的含义是"**一条都没推**"（`ERR-META-01` 同族）。
            #   ★ 措辞上**不能**先印"将把它们推上站"再说 0 行 —— 那两句自相矛盾，
            #     而矛盾的话会被读成前者（乐观的那半）。⇒ 先判、再印。
            print("★ 已标脏 0 行 —— ⚠️ **站点不会更新**。")
            print("   原因：本次 `待改` 为 0 ⇒ 没有行被回写 ⇒ `--mark-stale` "
                  "（只管本次改动的行）标不到任何行。")
            print("   ⇒ 若局面是「列已对、只是从没推过」（生产实测正是这种），"
                  "请改用 `--mark-stale-targets`。")
        else:
            print(f"★ 已标脏 {n_stale} 行（updated_at）⇒ edit 阶段将把它们推上站")
    elif args.mark_stale_targets:
        n_stale = mark_stale_targets(con)
        print(f"★ 已标脏**全部目标行** {n_stale} 行（updated_at）"
              f"⇒ edit 阶段会把它们重推一遍")
        print("   ★ 这会拉高 edit 待办数（预期），范围只限有 mapped_json 的皇后行。")
    else:
        print("ℹ️  未标脏（默认）⇒ 站点**不会**自动更新。"
              "要推上站：列对了用 `--mark-stale-targets`，刚回写完用 `--mark-stale`。")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    # ★ 照抄 `ERR-META-18` 的教训：**看退出码**，别只看打印。
    sys.exit(main())
