"""把已上站那批的 `formatted_title` 改成**皇后原文**（= `mapped_json.title`）。

═══════════════════════════════════════════════════════════════════════════
它修的到底是什么（2026-09-28 实测）
═══════════════════════════════════════════════════════════════════════════
`opencd_ingest.build_row()` 早已把 `formatted_title` 写成 `mapped.title`
（= **皇后原名**）。但**早期入库**的行（那时 `build_row` 还没写这个字段）
先由 `run.py:64` 占了位 —— 而那一行做的是**二次拆分**：
`parse_title()` 按 `-` 切分、并用 `re.sub(r"\\(?(?:19|20)\\d{2}\\)?|\\[.*?\\]", "")`
**吞掉括号与年份** ⇒ 产出畸形标题。实测最典型的一条：

    皇后原名 : milet - Made of Glass  {2026.08.19} {Sony Music Labels Inc.,SECL-3339, CD} {FLAC}
    run.py 产物: milet - Made of Glass {.08.19} {Sony Music Labels Inc.,SECL 2026 - FLAC 整轨 - OpenCD
                              ^^^^^^^^ 年份被咬掉一半      ^^^^^^^ 括号没闭、SECL 粘住年份

★ 所以本脚本**不是**"去掉 ` - OpenCD` 那么简单 —— 那个尾缀是**皇后自己写的**
  （`mapped.title` 里就带着），run.py 又拼了一遍。真正要修的是**整条标题**。

★★ 本脚本**只改一列**：`formatted_title`。值**只从 `mapped_json.title` 取**
   （那份快照是"抓页面时做的那个决定"，是权威）—— **不从 `torrent_name` 取**：
   实测 `mapped.title` 与 `torrent_name` 57/58 **不相等**（前者是解析后的形态，
   后者是原始串），两者是**不同的东西**。拿错的去覆盖标题 = 改坏标题
   （`ERR-META-03` 的味道）。

★★★ 写库**不是终点**：站上那 25 个页面要另跑 `update_metadata.py` 推。
    本脚本只负责把**本地库**改成真值。推站上的命令见文件末尾。

═══════════════════════════════════════════════════════════════════════════
闸门④（`transfer_rule = 'ALLOW'`）—— 本脚本**不越权**
═══════════════════════════════════════════════════════════════════════════
`db.EDIT_RULE_GUARD` 会让 `update_metadata.py` **拒绝**编辑非 `'ALLOW'` 的行
（2026-09-27 实测的洞：一条 `DENY` 行被 edit 路改过页面）。
⇒ 本脚本按 `transfer_rule` **分两档报**，而且**不替用户决定**：
  · `ALLOW` —— 主集，改完就能推站上；
  · 非 `ALLOW` —— **单独列出来**（不合并进总数）。用户若确认有权改，
    要**显式**把它标成 `ALLOW`（一次有记录的写库动作），而不是在这里开静默例外。

★ 说明：`formatted_title` 是**我们自己的标记**，把它对齐到皇后原文**不改任何内容**，
  所以"禁转"的语义（不许把**内容**转发出去）与本步不冲突 —— 但闸门是**闸门**，
  过不过由用户显式决定，不由本脚本代劳。

═══════════════════════════════════════════════════════════════════════════
三条纪律
═══════════════════════════════════════════════════════════════════════════
1. **默认只读**（不带 `--apply` 只预览）；真正写要 `--apply`
   **且** `--i-have-stopped-the-container`。
   ★ 必须**先停容器** —— 两个写者写同一个 SQLite 是 `ERR-DOCKER-03`。
   ★★ 那两条声明**不是验证**：本脚本**无从检查**容器是否真停。
      必须先用命令确认 `exited`（见文末）。

2. ★★ **只改 `formatted_title` 这一列**。绝不碰 `small_descr` / `opencd_descr` /
   `descr` / `content_path` / `torrent_file` / `log_content` / `opencd_*` /
   `status` / `reason` / `transfer_rule` / `site_status` / `site_torrent_id`。
   ★ 也绝**不写** `site_publish` —— 那张表记的是"发过哪个站"，本步与此无关。

3. ★★★ **判据必须窄到"改了真有用"**，不许"看着像就改"：
   只收**已上站**（`site_status='EXISTS'`）∧ **有 `site_torrent_id`**
   （没 id 的推不了，改了是**无效功**）∧ **有 `mapped_json`** ∧
   **`mapped.title` 非空** ∧ **新值 ≠ 旧值**（逐字节相同的不发 —— 那正是
   已知「HTTP 200 + 空 body」的触发条件，`ERR-SITE-01`）。
   ★ 空 `mapped.title` = **如实说"我不知道皇后原文"**，不许拿 `torrent_name`
     或旧值凑一个（`ERR-META-14`：空来源不许印成结论）。

用法（**容器已停**；`--db` 默认走 NAS 生产库，容器内请显式传 `--db /data/data.db`）:
    python tools/fix_onsite_titles_and_subtitles.py --db /data/data.db          # 只读预览
    python tools/fix_onsite_titles_and_subtitles.py --db /data/data.db \\
        --apply --i-have-stopped-the-container
    python tools/fix_onsite_titles_and_subtitles.py --db /data/data.db --only 18887
        # ★ 只处理点名的那几条（「先试一条」用）

═══════════════════════════════════════════════════════════════════════════
写库之后：推站上（**本脚本不做**，另跑 —— 命令要一条一块，别贴一起）
═══════════════════════════════════════════════════════════════════════════
    sudo docker stop -t 30 opencd2musopia
    sudo docker inspect --format '{{.State.Status}} exit={{.State.ExitCode}}' opencd2musopia
    python update_metadata.py <sid> --fields 'name' --verify
    python update_metadata.py <sid> --fields 'name' --apply --verify
    sudo docker start opencd2musopia
★ ★★ `torrent_id` 是**位置参数**（`ap.add_argument("torrent_id", nargs="?")`），
  **不是** `--torrent_id` —— 本文件先前两行写的是 `--torrent_id <sid>`，
  实测会 `error: unrecognized arguments: --torrent_id`（2026-09-28 踩过）。
  （`ERR-META-04`：注释里的命令也是文档，写错了就会被人照着跑。）
★ ★★ 那个 `<sid>` 是 **`site_torrent_id`**（**站点页面 id**，如 `15972`），
  **不是 `info_hash`** —— 两个标识长得很不一样，混用会报出一个**误导性**的错
  （`publish.py --only` 传站点 id 时**修前**会报"库里没有这个 hash"，
  它把"你传错了标识类型"说成"这条不存在"，见 `ERR-SITE-06` 一族）。
  ★ 一般化：**同一条种子有两个标识（info_hash / site_torrent_id），
    报错文案必须说清它在问哪一个**，否则读的人只能猜。
★ `--fields 'name'` 是**承重的**：不列 `small_descr` / `descr` ⇒ 站点上那两格
  沿用现值，不会被覆盖（`musopia.edit_torrent` 的契约：给什么改什么）。
★ `--fields` **必须带 `name`**：否则 `edit_torrent` 的 `changes` 里只剩 `id`
  ⇒ 载荷近乎空 ⇒ 撞上已知的「HTTP 200 + 空 body」被拒（`ERR-SITE-01`）。
"""
import argparse
import ast
import json
import os
import pathlib
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

# ★ 默认走 NAS 上的生产库（SMB）。容器内请显式传 `--db /data/data.db`。
LOCAL_DB = r"//iSunker-DS423/docker_ssd/musopia_script2/data.db"

# ★★ 唯一允许改的列。其余一概不碰 —— 尤其 `small_descr` / `torrent_file`。
SET_COLS = ("formatted_title",)

# ★ 目标口径（判据 3，逐句都在上面写了理由）。
TARGET_WHERE = (
    # ① 已上站 —— 没上站的标题改了也没人看得到（且它们会走 publish 那条路）
    "site_status = 'EXISTS' "
    # ② 有站点 id —— 没 id 的推不了 `update_metadata`（`--torrent-id` 按 id 选）
    "AND site_torrent_id IS NOT NULL AND site_torrent_id != '' "
    # ③ 有快照 —— 值**只**从 `mapped_json.title` 取，没快照就无从取
    "AND mapped_json IS NOT NULL AND mapped_json != ''"
)


def _cols(cur, table):
    return [r[1] for r in cur.execute(f"PRAGMA table_info({table})")]


def _mapped_title(mapped_json):
    """`mapped_json` → `title`。★ 解析失败**返回 None**（不静默当空）。

    ★ 解析失败与"title 本来就是空"是**两件相反的事** ⇒ 调用方要分得开
      （`ERR-META-14`：兜底值与真实结果分开报）。
    """
    try:
        got = json.loads(mapped_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(got, dict):
        return None
    return got.get("title")


def _edit_rule_guard():
    """从 `db.py` **源码**抽 `EDIT_RULE_GUARD` —— 不 `import db`。

    ★ 为什么用 AST 而不是 `import db`：`db` 会连 `config` 一起拉，而 `config`
      要求 `QB_PASS` 等环境变量（本机/容器外没有）⇒ `import db` 会**在拿到判据之前**
      就 `SystemExit`。而判据**不能手抄**（手抄必然漂移，`ERR-META-03`）。
    ★★ 抽不到就**返回 None** ⇒ 调用方**明说"本条不猜"**，不退回一个近似判据。
    """
    for f in ("db.py",):
        p = pathlib.Path(f)
        if not p.exists():
            continue
        for n in ast.parse(p.read_text(encoding="utf-8")).body:
            if isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "EDIT_RULE_GUARD"
                    for t in n.targets):
                try:
                    return ast.literal_eval(n.value)
                except Exception:
                    return None
    return None


def main():
    ap = argparse.ArgumentParser(
        description="把已上站的 formatted_title 改成皇后原文（mapped_json.title）")
    ap.add_argument("--apply", action="store_true", help="真正写库")
    ap.add_argument("--i-have-stopped-the-container", action="store_true",
                    help="★ 按纪律：必须显式声明容器已停（ERR-DOCKER-03）")
    ap.add_argument("--db", default=LOCAL_DB)
    ap.add_argument("--only", default="",
                    help="★ 只处理这些 site_torrent_id（逗号分隔）——「先试一条」用")
    args = ap.parse_args()

    only = tuple(s.strip() for s in args.only.split(",") if s.strip())

    # ★ 写库前的守卫一律放最前面（顺序错了就成了"先写、再报拒"）
    if args.apply and not args.i_have_stopped_the_container:
        sys.exit("❌ 拒绝写库：必须同时给 --i-have-stopped-the-container（纪律 1）")

    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA query_only=0" if args.apply else "PRAGMA query_only=1")

    cols = _cols(conn, "tasks")
    for c in ("formatted_title", "mapped_json", "site_status", "site_torrent_id",
              "transfer_rule", "torrent_name", "opencd_tid"):
        if c not in cols:
            sys.exit(f"❌ tasks 表缺列 {c!r} —— 库不对，停止")

    rows = list(conn.execute(
        f"SELECT info_hash, site_torrent_id, formatted_title, mapped_json, "
        f"       transfer_rule, torrent_name, opencd_tid "
        f"  FROM tasks WHERE {TARGET_WHERE} ORDER BY site_torrent_id"))
    if only:
        rows = [r for r in rows if str(r[1]) in only]

    # ── 逐条算新值（★ 与"能不能改"分开判，两种原因分开报）──
    todo = []            # 可改：(row, 新标题)
    n_same = 0           # 新旧**逐字节相同** ⇒ 不改（已知会撞空 body）
    n_badjson, n_empty = 0, 0
    bad_samples, empty_samples = [], []

    for r in rows:
        ih, sid, cur_title, mj_raw, rule, tname, tid = r
        new_title = _mapped_title(mj_raw)
        if new_title is None:
            n_badjson += 1
            if len(bad_samples) < 5:
                bad_samples.append((sid, "mapped_json 解析失败/不是 dict"))
            continue
        new_title = str(new_title).strip()
        if not new_title:
            n_empty += 1
            if len(empty_samples) < 5:
                empty_samples.append((sid, tname or ""))
            continue
        if new_title == (cur_title or "").strip():
            n_same += 1
            continue
        todo.append((r, new_title))

    total = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    n_exists = conn.execute(
        "SELECT count(*) FROM tasks WHERE site_status='EXISTS'").fetchone()[0]

    print("=" * 78)
    print("★ 把已上站的 formatted_title 改成皇后原文（**只改这一列**）")
    print("=" * 78)
    print(f"  库                       : {args.db}")
    if only:
        print(f"  ★ 只处理(--only)         : {list(only)}")
    print(f"  总行数                   : {total}")
    print(f"  tasks.site_status='EXISTS': {n_exists}")
    print(f"  符合目标口径(TARGET_WHERE): {len(rows)}")
    print(f"  ├ ★ 可改（新值非空且≠旧） : {len(todo)}")
    print(f"  ├ 新旧**逐字节相同**⇒ 跳过: {n_same}   ★ 改了也不会发（撞空 body，ERR-SITE-01）")
    print(f"  ├ `mapped.title` 为空⇒跳过: {n_empty}   ★ 如实说'不知道皇后原文'，不硬凑")
    print(f"  └ `mapped_json` 解析失败   : {n_badjson}   ★ 解析失败 ≠ title 为空，两者分开")
    for sid, tn in empty_samples:
        print(f"       [空] sid={sid} 皇后原名={tn[:60]!r}")
    for sid, why in bad_samples:
        print(f"       [坏] sid={sid} {why}")

    if not todo:
        print("\n✅ 没有要改的。")
        conn.close()
        return 0

    # ── 按 `transfer_rule` 分档（★ 不合并成一个数）──
    allow = [(r, t) for r, t in todo if (r[4] or "") == "ALLOW"]
    other = [(r, t) for r, t in todo if (r[4] or "") != "ALLOW"]

    print("\n" + "─" * 78)
    print(f"★ 按闸门④（`transfer_rule`）分档 —— `update_metadata.py` 的 edit 路会拦非 ALLOW")
    print("─" * 78)
    print(f"  `ALLOW`    ⇒ 改完可推站上 : {len(allow)}")
    print(f"  非 `ALLOW` ⇒ **本脚本不代劳**: {len(other)}   "
          f"← ★ 这些要你**显式**标 ALLOW（一次有记录的写库动作），不是在这里开例外")
    for r, t in other:
        print(f"       sid={r[1]} rule={r[4]!r}  {str(r[2])[:56]}")

    rule_guard = _edit_rule_guard()
    print(f"\n  `db.EDIT_RULE_GUARD`（AST 从源码抽）= {rule_guard!r}")
    if rule_guard is None:
        print("  ★★ 抽不到 ⇒ **本条不猜**（ERR-META-14）：非 ALLOW 那批能不能推，请人工核。")

    # ── 逐条列旧 → 新（★ 用户拍板前要看到这个）──
    print("\n" + "─" * 78)
    print("★ 旧标题 → 新标题（逐条）")
    print("─" * 78)
    for r, t in todo:
        mark = "★扩" if len(t) > len(r[2] or "") else "  "
        print(f" {mark} sid={r[1]:>6}  {len(r[2] or ''):>3}字 → {len(t):>3}字")
        print(f"        旧: {r[2]}")
        print(f"        新: {t}")

    # ★ 长度**变长**的单独拎出来：站点 `name` 上限**未实测**，变长的才有截断风险
    longer = [(r, t) for r, t in todo if len(t) > len(r[2] or "")]
    print("\n" + "─" * 78)
    print(f"★ 标题**变长**的 {len(longer)} 条（超站点 `name` 上限的风险只在这里）")
    print("─" * 78)
    if not longer:
        print("  无。")
    else:
        longest = max(len(t) for _, t in longer)
        print(f"  最长的新标题 {longest} 字  ← ★ 站点 `name` 上限**未实测**，")
        print(f"     判据拿这个数去比；**不许**'批量发完再看'（截断/拒收不可逆）")
        for r, t in sorted(longer, key=lambda x: -len(x[1])):
            print(f"     sid={r[1]:>6} {len(r[2] or ''):>3} → {len(t):>3}  {t}")

    print("\n" + "─" * 78)
    print(f"【改动内容（**只这一列**）】")
    print(f"    formatted_title : <旧> → mapped_json.title（皇后原文）")
    print(f"【★ 明确**不碰**的列】")
    print(f"    small_descr / opencd_descr / descr / content_path / torrent_file /")
    print(f"    log_content / opencd_tid / opencd_added_at / status / reason /")
    print(f"    transfer_rule / site_status / site_torrent_id / updated_at")
    print(f"【★ 也不写】site_publish（那张表记'发过哪个站'，与本步无关）")
    print("─" * 78)

    if not args.apply:
        print("\n💡 预览模式，未写库。确认后：")
        print(f"     python tools/fix_onsite_titles_and_subtitles.py --db {args.db} \\")
        print("         --apply --i-have-stopped-the-container")
        print("\n★ 写库**不是终点**：站上那批页面要另跑 update_metadata.py 推")
        print("   （命令见本文件模块 docstring 末尾；`--fields 'name'` 是承重的）")
        conn.close()
        return 0

    # ---- 写 ----
    n = 0
    try:
        for r, t in todo:
            cur = conn.execute(
                "UPDATE tasks SET formatted_title=? "
                " WHERE info_hash=? AND site_status='EXISTS'",
                (t, r[0]))
            n += cur.rowcount
        conn.commit()
        print(f"\n💾 已改：{n} 条")
        print(f"   查后复核：")
        print(f"     总行数仍为        : "
              f"{conn.execute('SELECT count(*) FROM tasks').fetchone()[0]}"
              f"   （应 = {total}，**一行都没删**）")
        left = conn.execute(
            f"SELECT count(*) FROM tasks WHERE {TARGET_WHERE}").fetchone()[0]
        print(f"     仍符合 TARGET_WHERE: {left}（★ 不等于 0 是**正常的** —— "
              f"其中含'逐字节相同'与'mapped.title 为空'那些**不该改**的）")
    finally:
        conn.close()
    print("\n★ 下一步（人工）：推站上 —— 命令见本文件模块 docstring 末尾。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
