"""★ 把「其实什么都齐了、只差一个 `status`」的行改回 `SUCCESS`。

═══════════════════════════════════════════════════════════════════════════
它存在的理由：`status` 是**历史残留**，不是当前事实
═══════════════════════════════════════════════════════════════════════════
`topup_opencd_fields.py` 补完皇后页面后调 `opencd_ingest.ingest(...)`，
而它**刻意不传 `torrent_file`**（纪律 5：重建 `.torrent` 会换 passkey ⇒
**改 infohash**，严禁）⇒ `opencd_ingest.build_row` 的第 ④ 条判据
（`elif not torrent_file:`，**2026-09-27 已拆成两支**）就命中
⇒ 落 `status='NO_TORRENT_FILE'`。

★ 2026-09-27 已修的那个坑：那一支原先写的是
  `elif not torrent_file or not os.path.exists(str(torrent_file))`，文案是
  `f"拿不到本地种子文件（{torrent_file!r}）"` —— 把「写入侧没传路径（`None`）」
  和「传了路径但盘上没有」**压成同一句话** ⇒ `reason` 印成
  `拿不到本地种子文件（None）`，让人以为文件丢了（实际文件都在）。
  ⇒ 现在两支分开，`None` 支明说「**调用方未传**」。★ 状态码**没变**（仍是
  `NO_TORRENT_FILE`）⇒ **本脚本的判据与用法一个字都不用改**。

★★ 可是 `save_task` 的 upsert **只更新显式传进来的列** ——
   `build_row` **刻意不写** `torrent_file` / `log_content` / `content_path`
   ⇒ **原来那些值一个都没被抹掉**。
   ⇒ 于是库里出现这个形状（★ 2026-09-23 深夜 `qb_probe.py` 实测，27 条全是）：

       status = NO_TORRENT_FILE     ← 名字说"没有种子文件"
       torrent_file = /data/output_torrents/….torrent   ← 但文件**就在**
       log_content  = <3130~15579 字符>                 ← 而且有 log
       mapped_json  = 有                                ← 而且补过页面了
       updated_at   = 今天 10:34 / 12:15                ← 被那轮 topup 盖过

   ★ 结论：**`NO_TORRENT_FILE` 这个名字是错的。** 对第一阶段行它不代表
     "没有 `.torrent`"，它代表"**刚给这条补完了皇后页面**"
     （`publish.py` 预检 ③ 与 `readiness_probe.py` 都按**文件在不在**判，
      所以它们报"预检全过"是对的 —— **那两个没有错**）。

═══════════════════════════════════════════════════════════════════════════
它做什么、不做什么
═══════════════════════════════════════════════════════════════════════════
**做**：只 `UPDATE tasks SET status='SUCCESS' WHERE info_hash=?`
      且**逐条**要求「现在这一行的 `status` 是 `NO_TORRENT_FILE`」
      （`WHERE` 里再钉一次 ⇒ 并发改了别的行也影响不到）。

**不做**（★ 每一条都是承重的）：
  · **不碰** `torrent_file` / `log_content` / `content_path` / `mapped_json`
    / `site_status` / `site_torrent_id` / `updated_at` —— 一个字都不动。
  · **不发任何请求**（零出网）—— 这一步**一分配额都不花**，
    因为要的东西（`.`torrent` 路径 + log）**本来就在库里**。
  · **不新建 `.torrent`** —— 那会换 passkey ⇒ 改 infohash（严禁）。
  · **不自动发** —— 改完只是让它**够得着**发布队列；
    真发仍归 `publish.py`，且 `ENABLE_PUBLISH=false` 时 daemon 不会发。

═══════════════════════════════════════════════════════════════════════════
★★ 口径：**从 `publish.py` 源码里抠**，绝不手抄
═══════════════════════════════════════════════════════════════════════════
本脚本判定"能不能发"用的**每一道预检**都从 `publish.py` 抠出来，
按**抠到的顺序**、用**抠到的条件**逐条算 —— 与 `readiness_probe.py` 同一套做法：

  · 缺封面          ← 抠出 `if not (t.get("cover_url") or "").strip()`
  · 种子文件缺失    ← 抠出 `if not tf or not os.path.exists(tf)`
  · 无 log          ← 抠出 `has_log` 的赋值 + `log_content` / `logs_json` 两处
  · `site_status='EXISTS'` ← `publish.py` 的队列口径里排掉它

★ **抠不到就报错退出**，绝不猜。
★ 判据**另有一条独立哨兵**：本脚本算出的"预检全过的集合"必须与
  `publish.py` **同一份源码**算出的结果一致 —— 见 `--selftest`。

★★ 三条**绝不碰**的行（用户 2026-09-23 手动标记，**不是**判据能推出来的）：
     國名家名曲 ×3（`205965ff4591` / `00475fec155c` / `6966e95913d7`）
     周杰伦       ×3（`e76e695b6332` / `2201d9c0e204` / `7c96605ab39b`）
  ⇒ 它们**即使预检全过也不在候选里**（下面 `MANUAL_BLOCK` 硬钉）。

═══════════════════════════════════════════════════════════════════════════
三条纪律
═══════════════════════════════════════════════════════════════════════════
1. **默认只读**（`--dry-run` 是默认）；真写要 `--apply` **且**
   `--i-have-stopped-the-container`（两个写者写同一个 SQLite 是 ）。
2. **只改一列**：`status`。别的列写前后**逐条比对**，不一致就报出来。
3. **先出逐条清单**（默认就出）—— 改哪些、为什么、卡在哪，全打给人看。

═══════════════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════════════
本机（★ 生产库只读打开；写要停容器）:
    python flip_status.py                                  # 只读预览（默认）
    python flip_status.py --tids 198601,196878             # 只看点名的
    python flip_status.py --apply --i-have-stopped-the-container
容器内（`torrent_file` 是容器内路径，`os.path.exists` 只有在容器里才判得准）:
    sudo docker compose run --rm tools python flip_status.py
"""
import argparse
import ast
import json
import os
import pathlib
import sqlite3
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config

# ★ 生产库（本机经 SMB；容器内是 /data/data.db —— 由 config.DB_PATH 决定）
LOCAL_DB = r"//iSunker-DS423/docker_ssd/opencd2others/data/data.db"

# ★★ 用户**手动**标记的"绝不发"（不是判据能推出来的 —— 所以硬钉在这里）。
#   国名家名曲 3 条（禁止转发，用户手动加的）+ 周杰伦 3 条（★ 用户强调：不能发）
MANUAL_BLOCK = {
    "205965ff4591": "中國名家名曲（用户手动标：禁止转发）",
    "00475fec155c": "中國名家名曲（用户手动标：禁止转发）",
    "6966e95913d7": "中國名家名曲（用户手动标：禁止转发）",
    "e76e695b6332": "周杰伦（★ 用户强调：禁止转发，不能发）",
    "2201d9c0e204": "周杰伦（★ 用户强调：禁止转发，不能发）",
    "7c96605ab39b": "周杰伦（★ 用户强调：禁止转发，不能发）",
}

# ★ 库里的列清单：**一个元组，SELECT 与 zip 共用**（列名只写一次）。
#   ⇒ 报出"缺封面 4 条"那种**看着像真的**假数。这里从形状上杜绝。
COLS = ("info_hash", "opencd_tid", "formatted_title", "torrent_name",
        "status", "site_status", "transfer_rule", "cover_url",
        "torrent_file", "log_content", "logs_json", "mapped_json",
        "updated_at")


# ==================== 一、从 publish.py 抠判据（不手抄）====================

def read_publish_source():
    """拿到 `publish.py` 的源码文本与 AST。★ 抠不到 ⇒ 报错退出。"""
    p = pathlib.Path(__file__).resolve().parent / "publish.py"
    if not p.exists():
        raise SystemExit(f"❌ 找不到 {p} —— 本脚本的判据**必须**从它抠，拒绝猜")
    src = p.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise SystemExit(f"❌ publish.py 解析不了（{e}）—— 拒绝用一份猜的判据")
    return src, tree


def extract_checks(tree):
    """
    抠出 `publish.py` 预检的**整段循环体源码**（不是只抠 `if` 的条件）。

    ★★ 为什么必须是**整段循环体**而不是三条 `if` 的条件：
      `publish.py` 的条件里用了**中间变量** ——
          tf = t.get("torrent_file")
          if not tf or not os.path.exists(tf):
      `has_log` 更是**先算一次、再在 except 里重算**的多步赋值。
      ⇒ 只抠条件、自己造 `tf`/`has_log` 就是**人肉翻译**，
        而人肉翻译正是  要消灭的东西（会漂、且不报错）。
      ★ 实测踩过：第一版只抠条件 ⇒ `NameError: name 'tf' is not defined`。
      本脚本因此改成**把源码原文搬过来跑** —— 中间变量、except、注释逻辑
      全都和 `publish.py` **同一份**，没有翻译这一步。

    ★★ **必须排除的**（口径只许有一份）：`publish.py` 里有**第二个**
      `reasons.append(...)` 带（`explain_not_queued()`，行 ~127-141）。
      那是**闸门链的逐条解释器**，**不是**发布预检 —— 而且它今天已经**漂了**：
      里面写的是 `publish_after`（闸门②的**旧**口径，已被
      `opencd_added_at + 7 天` 取代，见 `db.py:226-260`）。
      ⇒ 混进来就是拿一个作废的口径去改行。
      ★ 认出来的办法（**按形状，不按行号**）：真预检那段的 `if` 之前有
        `reasons = []` 的重置；解释器那段没有。

    ★ 哨兵：抠到的 `reasons.append` **恰好 3 条**。数量不对 ⇒ **报错退出**。
      ★ 这条哨兵**真的救过场**：我按印象以为"3 条"，第一次抠出来是 4 条
        —— 多出的那条正是 `explain_not_queued`，是自检当场报出来的。
    """
    # 找 `explain_not_queued` 的节点行号（按函数名，不按行号）
    excluded_lines = set()
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "explain_not_queued"):
            for sub in ast.walk(node):
                if hasattr(sub, "lineno"):
                    excluded_lines.add(sub.lineno)

    # 定位"真预检循环体"：函数里紧跟在 `reasons = []` 之后的那段
    def reset_before(ifnode, owner):
        """`owner.body` 里这个 `if` 之前，**按语句顺序**有没有 `reasons = []`。"""
        found = []

        def scan(stmts):
            for stmt in stmts:
                if stmt is ifnode:
                    return True
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    tgt = stmt.targets[0]
                    if (isinstance(tgt, ast.Name) and tgt.id == "reasons"
                            and isinstance(stmt.value, ast.List)
                            and not stmt.value.elts):
                        found.append(True)
                        return False
                for attr in ("body", "orelse", "finalbody"):
                    inner = getattr(stmt, attr, None)
                    if inner and not scan(inner):
                        return False
            return True

        scan(owner.body)
        return bool(found)

    # 收集所有真预检的 `if`（同一函数、`reasons = []` 之后）
    pre_if = []
    for owner in ast.walk(tree):
        if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for stmt in ast.walk(owner):
            if not isinstance(stmt, ast.If) or stmt.lineno in excluded_lines:
                continue
            if not any(isinstance(sub, ast.Call)
                       and isinstance(sub.func, ast.Attribute)
                       and sub.func.attr == "append"
                       and isinstance(sub.func.value, ast.Name)
                       and sub.func.value.id == "reasons"
                       for sub in ast.walk(stmt)):
                continue
            if reset_before(stmt, owner):
                pre_if.append((stmt, owner))

    if not pre_if:
        raise SystemExit("❌ 在 publish.py 里找不到发布预检那一段 —— 拒绝继续")

    # ★ 取**第一条**真预检 `if` 所在的那个 `for` 循环体 —— 整段搬过来。
    #   三条预检都在同一个循环体里（实测：publish.py:198-227），
    #   所以搬一段就够，且**天然**带上了 `reasons = []` 的重置与 `tf`/`has_log`。
    first_if, owner = pre_if[0]
    loop = None
    for stmt in ast.walk(owner):
        if isinstance(stmt, (ast.For, ast.While)):
            if any(sub is first_if for sub in ast.walk(stmt)):
                loop = stmt
                break
    if loop is None:
        raise SystemExit("❌ 发布预检不在任何循环里？形状变了 —— 拒绝继续")

    body_lines = [ast.unparse(s) for s in loop.body]

    # ★ 挑出**只负责判定**的那几条语句（`reasons = []` / 往 reasons append 的 if /
    #   `has_log` 那个 try-except / 中间变量 `tf`）—— 把 `print` 与
    #   `valid/blocked` 那些副作用留在 publish.py 里。
    #   ★ 判据：**这段代码必须自己跑得起来**（不留任何未定义名字）。
    #     （它在 `try` 前，是个普通 Assign）⇒ 抽出 `if not has_log:` 而
    #     `has_log` 从未定义 ⇒ `NameError`。自检当场报出来。
    #   ⇒ 现在改成**白名单 + 自检兜底**：凡是给 `reasons`/`has_log`/`tf`/`lj`
    #     这些判定用到的名字赋值的语句，一律带上。
    KEEP_NAMES = {"reasons", "has_log", "tf", "lj"}

    def assigns_needed(node):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                for tgt in sub.targets:
                    if isinstance(tgt, ast.Name) and tgt.id in KEEP_NAMES:
                        return True
        return False

    keep = []

    def is_reasons_append(stmt):
        """`reasons.append("…")` 这种语句（`ast.Expr` 包着 `Call`）—— 必须保留。"""
        return (isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Attribute)
                and stmt.value.func.attr == "append"
                and isinstance(stmt.value.func.value, ast.Name)
                and stmt.value.func.value.id == "reasons")

    def want(stmt):
        """
        这段语句要不要带进"判定代码"里。

        ★ 必须是**递归**的：`has_log` 的多碟日志重算那一段（`try/except`）
          **嵌在** `if not has_log:` 里面，不是循环体的直接子语句
          ⇒ 平铺扫 `loop.body` 会漏掉它（实测：`has_log` 又变未定义）。
        ★ 但"保留 if 外壳"时，body 里**只有** `reasons.append` / 重算 `has_log`
          的语句该留 —— 所以 `want` 对 `Expr` 也要回答 True（见 `is_reasons_append`）。
          ★ 实测踩过：`want` 漏了 `Expr` ⇒ 三条 `reasons.append` 全被滤掉，
            抽出 0 处 append ⇒ 哨兵报错（幸好有哨兵，不然会静默放行每一行）。
        """
        if is_reasons_append(stmt):
            return True
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            return False                      # 注释被 unparse 成常量 ⇒ 丢
        if isinstance(stmt, (ast.Try, ast.Import, ast.ImportFrom, ast.Pass)):
            return True                       # 多碟日志重算 / 它用到的 import
        if isinstance(stmt, ast.Assign) and assigns_needed(stmt):
            return True
        if isinstance(stmt, ast.If):
            return any(want(sub) for sub in stmt.body)
        return False

    def collect(stmts):
        """按原顺序收集需要的语句；对保留下来的 `if` 递归下钻进它的 body。"""
        for s in stmts:
            if not want(s):
                continue
            if isinstance(s, ast.If):
                body = [x for x in s.body if want(x)]
                if not body:
                    continue
                keep.append(ast.If(test=s.test, body=body, orelse=[]))
            else:
                keep.append(s)

    collect(loop.body)

    code = "\n".join(ast.unparse(s) for s in keep)
    if code.count("reasons.append") != 3:
        raise SystemExit(
            f"❌ 抽出来的预检代码里 `reasons.append` 有 "
            f"{code.count('reasons.append')} 处（**应为 3**）—— 拒绝继续。\n"
            f"--- 抽出来的源码 ---\n{code}")
    return code



def make_precheck(code):
    """
    把抠出来的**预检源码**编译成 `f(t) -> [原因文案, ...]`。

    ★ 为什么编译、不"照着写一遍 if"：编译用的就是 `publish.py` 的
      **源码原文**（`extract_checks` 抽出来的那段，含 `tf` / `has_log` /
      `try/except`）⇒ 中间没有"人肉翻译"这一步，也就没法漂。
    ★ 允许的名字：`t`（行 dict）、`os`、`json` —— 就是那几行用到的。
      编译失败 ⇒ **报错退出**（否则会得到一个"永不点亮"的假判据）。
    ★ 函数**不短路**：三条预检全算，返回全部原因（好给"缺哪几样"的分布）。
    """
    src = ("def _precheck(t):\n"
           + "\n".join("    " + ln for ln in code.splitlines())
           + "\n    return reasons\n")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise SystemExit(f"❌ 预检源码拼出来语法就不对：{e}\n---\n{src}")
    env = {"os": os, "json": json}
    exec(compile(tree, "<publish.py 抽出的预检>", "exec"), env)   # noqa: S102
    return env["_precheck"]


def _precheck_lines(code):
    """把抽出来的预检源码按行返回 —— 给打印"判据从哪来"用。"""
    return [ln for ln in code.splitlines() if ln.strip()]


# ==================== 二、候选 ====================

def load_rows(conn, tids=()):
    """
    候选池 = 有 `opencd_tid` 的行（与 `readiness_probe.py` 同池，便于对照）。

    ★ 为什么**不**直接用队列（`db.PENDING_PUBLISH_WHERE`）：这些行 `status`
      还不是 `SUCCESS` ⇒ **根本不在队列里** ⇒ 用队列口径**问不到**它们。
      本脚本要问的正是"把它们改回 `SUCCESS` 之后会怎样"。
    """
    sel = ", ".join(COLS)
    sql = f"SELECT {sel} FROM tasks WHERE opencd_tid IS NOT NULL AND opencd_tid != ''"
    params = ()
    if tids:
        sql += f" AND opencd_tid IN ({','.join('?' * len(tids))})"
        params = tuple(str(t) for t in tids)
    return [dict(zip(COLS, r)) for r in conn.execute(sql, params)]


def blocking_of(r, precheck):
    """
    一条行**卡在哪几道预检**上。返回 `[文案, ...]`（空 = 全过）。

    ★ 判据**全部**来自 `publish.py`（`precheck` 是抠出来编译的）。
    ★ `site_status='EXISTS'` **不在这里判** —— 它不是发布预检，而是
      **队列口径**（`db.PENDING_PUBLISH_WHERE`）在排队之前就排掉。
      ⇒ 分开报：否则会把"进不了队列"说成"发布预检没过"（两件事）。
    """
    return precheck(r)


# ==================== 三、主流程 ====================

def main():
    ap = argparse.ArgumentParser(
        description="把只差一个 status 的行改回 SUCCESS（默认只读）")
    ap.add_argument("--apply", action="store_true", help="真正写库（★ 只改 status 一列）")
    ap.add_argument("--i-have-stopped-the-container", action="store_true",
                    help="★ 按纪律：写库前必须显式声明容器已停")
    ap.add_argument("--tids", default="",
                    help="只处理这几个 opencd_tid（逗号分隔）—— 「先试发两条」用这个")
    ap.add_argument("--limit", type=int, default=40, help="最多打印几条（默认 40）")
    ap.add_argument("--db", default=None,
                    help=f"数据库路径（默认 {LOCAL_DB}；容器内用 config.DB_PATH）")
    ap.add_argument("--selftest", action="store_true",
                    help="★ 自检：证明判据确实从 publish.py 抠出来的、且没走样")
    args = ap.parse_args()

    _src, tree = read_publish_source()

    if args.selftest:
        return selftest(tree)

    precheck = make_precheck(extract_checks(tree))

    # ★★ 写库前的守卫**一律放最前面**（顺序错了就成了"先写、再报拒"）
    if args.apply and not args.i_have_stopped_the_container:
        sys.exit("❌ 拒绝写库：必须同时给 --i-have-stopped-the-container（纪律 1）")

    db_path = args.db or (LOCAL_DB if os.path.exists(LOCAL_DB) else config.DB_PATH)
    tids = tuple(s.strip() for s in args.tids.split(",") if s.strip())

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA query_only=1" if not args.apply else "PRAGMA query_only=0")

    print("=" * 74)
    print("★ 把「只差一个 status」的行改回 SUCCESS（★ 默认只读）")
    print("=" * 74)
    print(f"  库          : {db_path}")
    print(f"  模式        : {'★ 写（--apply）' if args.apply else '只读预览'}")
    print("\n[判据：★ 从 publish.py 源码**原文抽出来编译**的，不是手写的]")
    for ln in _precheck_lines(extract_checks(tree)):
        print(f"   · {ln}")

    rows = load_rows(conn, tids)
    print(f"\n候选（有 tid 的行）: {len(rows)} 条"
          + (f"  （--tids 点名 {list(tids)}）" if tids else ""))

    # ★★★ 判据自检：`torrent_file` / `content_path` 是**容器内路径**
    #   ⇒ `os.path.exists` **只在容器里**才判得准。
    #
    #     `torrent_file` 非空、而 `os.path.exists` 对它们**全部 False**
    #     （本机没有 `/data/output_torrents/`）⇒ 34 条"其实齐全"的行被误判成
    #     "种子文件缺失" ⇒ 打印出 `预检不过 214`。
    #     ★ 而在**容器里**跑同一条命令，正确数是 `34 / 180 / 224`。
    #     ⇒ 两个数**都"看着像真的"**，而我只差一步就把本机那个当结论用。
    #
    #     解法不是"记得在容器里跑"（那是人的纪律，会忘 —— 我已忘过一次），
    #     而是**让判据自己宣告失效**：
    #       非空 ⇒ 一条都不存在   ⇒ **报错退出**
    #     因为"258 条路径一条都不存在"在真实世界里**不可能**
    #     （要么池子空，要么全空 ⇒ 一定是判据坏了）。
    #   ★ 为什么阈值是"**一条都不存在**"而不是"很多条不存在"：
    #     生产里**确实**有失效路径（`qb_probe` 实测 `content_path` 有 30 条
    #     指向已删目录）⇒ "一些不存在"是**正常**的，不能拦。
    #     但"**全部**不存在"只能是路径世界不对（跑错地方）。
    if rows:
        tf_nonempty = [r for r in rows if (r["torrent_file"] or "").strip()]
        if tf_nonempty:
            n_ex = sum(1 for r in tf_nonempty if os.path.exists(r["torrent_file"]))
            print(f"\n[判据自检] `torrent_file` 非空 {len(tf_nonempty)} 条，"
                  f"其中**存在**的 {n_ex} 条")
            if n_ex == 0:
                raise SystemExit(
                    f"❌ **判据在此处失效** —— 拒绝出数。\n"
                    f"   `torrent_file` 非空 {len(tf_nonempty)} 条，"
                    f"而 `os.path.exists` 对它们**全部**返回 False。\n"
                    f"   ★ 真实世界里这**不可能** ⇒ 不是数据坏，是**判据坏**。\n"
                    f"   ★ 最常见的原因：**你在本机（或别的机器）跑**，而\n"
                    f"     `torrent_file` 是**容器内路径**（`{tf_nonempty[0]['torrent_file']}`），\n"
                    f"     本机没有它。\n"
                    f"   ⇒ 请在 **NAS 上**跑：\n"
                    f"        sudo docker compose run --rm tools python flip_status.py\n"
                    f"   ★ 若你已经是在容器里跑，那说明路径挂载变了 —— "
                    f"先查 docker-compose.yml 的 volumes。\n"
                    f"   （本自检存在的理由：不拦的话会打印出一个**看着像真的**的"
                    f"“预检不过 N 条”，而那 N 里有一半是错的 —— 。）")
            print(f"   ✅ 非空即存在（或部分存在）⇒ 判据在本环境**成立**")

    # ── 分类 ────────────────────────────────────────────────────────────
    to_flip, already, blocked, on_site, manual = [], [], [], [], []
    for r in rows:
        h = (r["info_hash"] or "").lower()
        if h[:12] in MANUAL_BLOCK:
            manual.append(r)
            continue
        why = blocking_of(r, precheck)
        if (r["site_status"] or "") == "EXISTS":
            on_site.append((r, why))
            continue
        if why:
            blocked.append((r, why))
        elif r["status"] == "SUCCESS":
            already.append(r)
        else:
            to_flip.append(r)

    print(f"\n[分类（★ 不同口径，**不许相减** —— ）]")
    print(f"   ★ 要改回 SUCCESS 的（预检全过 + 不在站上 + status≠SUCCESS）: "
          f"{len(to_flip):>4}")
    print(f"   ├ 已经是 SUCCESS（不用动）                                  : "
          f"{len(already):>4}")
    print(f"   ├ 预检**不过**（★ 改 status 也没用）                        : "
          f"{len(blocked):>4}")
    print(f"   ├ 已上站 EXISTS（★ 补了也进不了队列）                       : "
          f"{len(on_site):>4}")
    print(f"   └ 手动标记禁止转发（★ 硬钉，不进候选）                      : "
          f"{len(manual):>4}")

    if blocked:
        print(f"\n[预检不过的卡点分布（★ 一条行可能同时犯几样，**不能相加**）]")
        dist = {}
        for _r, why in blocked:
            for w in why:
                dist[w] = dist.get(w, 0) + 1
        for w, n in sorted(dist.items(), key=lambda kv: -kv[1]):
            print(f"   {n:>5} 条  {w}")

    if manual:
        print(f"\n[★ 手动标记禁止转发（用户的选择 —— 本脚本**永不**碰）]")
        for r in manual:
            print(f"   ⛔ {(r['info_hash'] or '')[:12]}  tid={r['opencd_tid']:<9} "
                  f"{(r['formatted_title'] or r['torrent_name'] or '')[:40]}")
            print(f"        {MANUAL_BLOCK[(r['info_hash'] or '').lower()[:12]]}")

    # ★★ 对账：`readiness_probe.py` 报的数为什么和这里不一样？
    #
    #     而 `readiness_probe` 报 **22**、本脚本报 **0**。差 22 是不能放着不管的
    #     "看着像真的"的错数字，而它**不报错**）。
    #
    #   ★★ 本节的**唯一目的**：把差额**算出来**，而不是猜。
    #     办法 = 把本脚本**排掉**的那部分拆开，看它等不等于 22：
    #       · `status≠SUCCESS 且预检全过` 的行，按 `site_status` 分成
    #         「未上站」（→ 本脚本要改的）与「已上站 EXISTS」（→ 本脚本排掉的）
    #     ⇒ 若「已上站 EXISTS 且预检全过 且 status≠SUCCESS」**正好 = 22**，
    #       两个探针就**对齐**了：22 只是同一个集合减去"够不着"的那部分。
    #     ⇒ 若不是 22，那是**真的有一边算错了** ⇒ 本脚本把两边都打出来，
    #       让人拿着两个数去 `readiness_probe.py` 逐条对（★ 不擅自改对方的数）。
    #
    #   ★★★ 这一段**必须放在 `if not to_flip: return` 之前** ——
    #     它的用处**恰恰**是"要改的是 0"的时候（那才需要解释差额）。
    ready_off_site = [r for r, why in on_site if not why and r["status"] != "SUCCESS"]
    ready_on_site_already = [r for r, why in on_site
                             if not why and r["status"] == "SUCCESS"]
    print(f"\n{'=' * 74}")
    print("★ 与 readiness_probe.py 对账（★ 差额必须算出来，不许猜）")
    print("=" * 74)
    print("   −− 两边都自称：「预检全过 且 status≠SUCCESS」")
    print(f"     本脚本报（未上站 ⇒ 真能改）            : {len(to_flip):>4}")
    print(f"\n   ★ 本脚本**排掉**的那部分（`site_status='EXISTS'` ⇒ 进不了队列）：")
    print(f"     ├ 已上站 且 预检全过 且 status≠SUCCESS : {len(ready_off_site):>4}"
          f"   ← ★ 若这个数 = readiness_probe 报的数，两边就对齐了")
    print(f"     └ 已上站 且 预检全过 且 status=SUCCESS : {len(ready_on_site_already):>4}")
    print(f"\n     ⇒ 两格相加 = {len(ready_off_site) + len(ready_on_site_already)}"
          f"   （★ 应 = 「已上站 EXISTS」那一格 {len(on_site)}"
          f"，两数不等就是本节的分类有漏）")

    if ready_off_site:
        print(f"\n   [那 {len(ready_off_site)} 条的明细（★ 它们就是差额的候选）]")
        for i, r in enumerate(ready_off_site[:max(args.limit, 25)], 1):
            print(f"     [{i:>2}] tid={r['opencd_tid']:<9} status={r['status']:<16} "
                  f"{(r['formatted_title'] or r['torrent_name'] or '')[:38]}")

    if not to_flip:
        print("\n✅ 没有要改的 ⇒ **不写库**（本节的对账已在上方打出）。")
        conn.close()
        return 0

    print(f"\n{'=' * 74}")
    print(f"★ 逐条清单（要改的 {len(to_flip)} 条，前 {args.limit} 条）")
    print("=" * 74)
    for i, r in enumerate(to_flip[:args.limit], 1):
        print(f"  [{i:>3}] tid={r['opencd_tid']:<9} "
              f"status: {r['status']} → SUCCESS")
        print(f"        {(r['formatted_title'] or r['torrent_name'] or '')[:62]}")
        print(f"        site_status={r['site_status'] or '-'}  "
              f"transfer_rule={r['transfer_rule'] or '-'}  "
              f"mapped_json={'有' if (r['mapped_json'] or '').strip() else '空'}")
        tf = (r["torrent_file"] or "").strip()
        print(f"        torrent_file : {'文件在' if (tf and os.path.exists(tf)) else ('文件不在' if tf else '**空**')}")
        print(f"        log_content  : {'有' if (r['log_content'] or '').strip() else '空'}"
              f"   updated_at={r['updated_at']}")

    # ★★ 闸门④ 的提醒：`transfer_rule` 非 ALLOW 的行**即使 status 改对了**
    #   也过不了 `db.PENDING_PUBLISH_WHERE` 的第四句 ⇒ 改 status 帮不到它。
    #     只把现状**报出来**让人知道后果。
    n_rule = sum(1 for r in to_flip if (r["transfer_rule"] or "") != "ALLOW")
    print(f"\n[★ 闸门④ 提醒（这一条不属于发布预检，是队列口径的第四句）]")
    print(f"   要改的 {len(to_flip)} 条里，`transfer_rule != 'ALLOW'` 的有 {n_rule} 条")
    if n_rule:
        print(f"   ⇒ ★ 它们**即使 status 改成 SUCCESS 也进不了队列**"
              f"（闸门④ 要求 rule='ALLOW'）")
        print(f"   ⇒ 那要**先补页面**（`topup_opencd_fields.py`，★ 花配额）——")
        print(f"     本脚本**不做**那件事，它只改 status。")
    else:
        print(f"   ✅ 全部 rule='ALLOW' ⇒ 改完 status 就能进队列。")

    if not args.apply:
        print("\n💡 预览模式，未写库。确认后：")
        print("     python flip_status.py --apply --i-have-stopped-the-container")
        conn.close()
        return 0

    # ── 写（★ 只改 status 一列）──────────────────────────────────────────
    print(f"\n{'─' * 74}")
    print(f"→ 开始写（★ 只 UPDATE 一列：status）")
    n = 0
    try:
        for r in to_flip:
            cur = conn.execute(
                "UPDATE tasks SET status='SUCCESS' "
                " WHERE info_hash=? AND status='NO_TORRENT_FILE'",
                (r["info_hash"],))
            n += cur.rowcount
            if cur.rowcount != 1:
                print(f"   ⚠️  {r['info_hash'][:12]} rowcount={cur.rowcount}"
                      f"（status 已被别人改过？⇒ **没写**，正确行为）")
        conn.commit()
        print(f"💾 已改：{n} 条")

        # ── 复核（★ 逐条比对"别的列一个字没变"）────────────────────────
        print("\n[复核]")
        after = {x[0]: x[1] for x in conn.execute(
            "SELECT info_hash, status FROM tasks")}
        ok = sum(1 for r in to_flip if after.get(r["info_hash"]) == "SUCCESS")
        print(f"   status='SUCCESS' 的条数（应 = {n}）: {ok}")
        cols = ("torrent_file", "log_content", "content_path" if
                "content_path" in COLS else "mapped_json")
        bad = []
        for r in to_flip:
            cur = conn.execute(
                f"SELECT status, torrent_file, log_content, mapped_json, "
                f"       site_status, updated_at FROM tasks WHERE info_hash=?",
                (r["info_hash"],)).fetchone()
            if cur is None:
                bad.append((r["info_hash"], "行不见了"))
                continue
            if (cur[1] or "") != (r["torrent_file"] or ""):
                bad.append((r["info_hash"], "torrent_file 变了"))
            if (cur[2] or "") != (r["log_content"] or ""):
                bad.append((r["info_hash"], "log_content 变了"))
            if (cur[3] or "") != (r["mapped_json"] or ""):
                bad.append((r["info_hash"], "mapped_json 变了"))
            if (cur[4] or "") != (r["site_status"] or ""):
                bad.append((r["info_hash"], "site_status 变了"))
            if (cur[5] or "") != (r["updated_at"] or ""):
                bad.append((r["info_hash"], "updated_at 变了"))
        if bad:
            print(f"   ❌ 有 {len(bad)} 条**别的列被改了**（不该发生）：")
            for h, w in bad[:10]:
                print(f"      {h[:12]}  {w}")
        else:
            print(f"   ✅ torrent_file / log_content / mapped_json / site_status / "
                  f"updated_at **逐条一字未变**")
    finally:
        conn.close()
    return 0


def selftest(tree):
    """
    ★ 判据自检：证明"抠出来的判据"与 `publish.py` 是**同一份**，且没走样。

    判据：
      ① 抠到的文案**逐字**出现在 `publish.py` 源码里（不是我们编的）
      ② 抠到的**恰好 3 条**，且分别含 `cover_url` / `os.path.exists` /
         `log_content` —— 即真的是那三道预检
      ③ **负向**：`site_status` / `publish_after` **都不在**这几条里
         （前者的口径已被闸门②b 取代、后者是作废的旧闸门②
          —— 混进来就是拿一个漂移的口径去改行，）
      ④ 抓一个"什么都有"的假行 ⇒ 三条预检**全过**；再各去掉一样 ⇒ 各**恰好**
         亮一条。★ 这一条是防"条件编译出来恒真/恒假"的。
    """
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        print(f"   {'✅' if cond else '❌'} {name}" + (f"  {extra}" if extra else ""))
        if not cond:
            ok = False

    src = (pathlib.Path(__file__).resolve().parent / "publish.py").read_text(
        encoding="utf-8")
    code = extract_checks(tree)
    precheck = make_precheck(code)
    print("=" * 74)
    print("★ flip_status.py 自检（★ 判据必须来自 publish.py，不许手抄）")
    print("=" * 74)
    print(f"  从 publish.py 抽出的预检源码（{code.count('reasons.append')} 条 append）：")
    for ln in _precheck_lines(code):
        print(f"    {ln}")

    # 判据① 三条文案逐字都在 publish.py 源码里
    for text in ("缺封面", "种子文件缺失",
                 "无 log（最初的规矩：没有 log 不发布）"):
        chk(f"文案逐字出现在 publish.py 里: {text[:24]}", text in src)

    # 判据② 恰好 3 条 append，且三道预检都抽到了
    chk("★ 恰好 3 条 `reasons.append`（不是 4 —— 多的那条是 explain_not_queued）",
        code.count("reasons.append") == 3, f"实得 {code.count('reasons.append')}")
    chk("抽到 `cover_url`（缺封面那一道）", "cover_url" in code)
    chk("抽到 `os.path.exists`（种子文件那一道）", "os.path.exists" in code)
    chk("抽到 `has_log` / `log_content`（无 log 那一道）",
        "has_log" in code and "log_content" in code)

    # 判据③ 负向：漂移/作废的口径不许混进来
    chk("★ `site_status` **不在**发布预检里（它属队列口径，混进来就是错位）",
        "site_status" not in code)
    chk("★★ `publish_after` **不在**（那是作废的旧闸门②，"
        "现行是 `opencd_added_at` + 7 天）",
        "publish_after" not in code)
    chk("★ **没有** `print` / `valid.append` / `blocked.append` 等副作用混进来",
        "print(" not in code and "blocked.append" not in code)

    # 判据④ 真值表：全有 ⇒ 全过；各缺一样 ⇒ 恰好亮一条
    base = {"cover_url": "http://x/y.jpg", "torrent_file": __file__,
            "log_content": "log!", "logs_json": "null"}
    chk("整整齐齐的假行 ⇒ 预检**全过**", blocking_of(base, precheck) == [],
        f"{blocking_of(base, precheck)}")

    for key, val, want in (("cover_url", "", "缺封面"),
                           ("torrent_file", "", "种子文件缺失"),
                           ("log_content", "", "无 log")):
        bad = dict(base)
        bad[key] = val
        got = blocking_of(bad, precheck)
        chk(f"去掉 `{key}` ⇒ 恰好亮『{want}』一条",
            len(got) == 1 and want in got[0], f"实得 {got}")

    # 判据⑤ 不存在的路径也要亮（`os.path.exists` 那支真的在跑）
    bad = dict(base)
    bad["torrent_file"] = "/definitely/not/here.torrent"
    got = blocking_of(bad, precheck)
    chk("种子路径**不存在**（非空但假）⇒ 照样亮『种子文件缺失』",
        len(got) == 1 and "种子文件缺失" in got[0], f"实得 {got}")

    # 判据⑥ 多碟日志走 logs_json 时**不许误拦**
    bad = dict(base)
    bad["log_content"] = ""
    bad["logs_json"] = json.dumps([{"content": "disc1 log"}])
    got = blocking_of(bad, precheck)
    chk("`log_content` 空但 `logs_json` 有内容 ⇒ **不拦**（多碟日志）",
        got == [], f"实得 {got}")

    # 判据⑦ 手动禁止转发的名单是**硬钉**的
    chk("★ 手动禁转名单 6 条（3 中國名家名曲 + 3 周杰伦）", len(MANUAL_BLOCK) == 6,
        f"{sorted(MANUAL_BLOCK)}")

    print("=" * 74)
    print("✅ 自检全部通过" if ok else "❌ 自检**有失败** —— 别信本脚本的任何输出")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
