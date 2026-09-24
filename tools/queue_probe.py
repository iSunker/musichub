"""★ 只读：把**发布队列**当前有哪几条打出来。口径**从 db.py 里取**，不手抄。

★ 为什么要有它：`db.PENDING_PUBLISH_WHERE` 是四个闸门的**唯一来源**。
  手抄一遍到命令行，抄漏一条就得到一个"看着像真的"的错数字
  （`ERR-META-03`：不许手抄事实源的 WHERE）。本脚本**从 db.py 的源码里
  把那个常量抠出来用** —— 不是 import（import 会连带 `config`，而 `config`
  在**本机**缺 `QB_PASS`/`MUSOPIA_COOKIE` ⇒ `sys.exit`），而是 `ast` 解析。

★★ 为什么不是 `python -c "print(db.PENDING_PUBLISH_WHERE)"` 然后粘贴：
  那正是 `ERR-META-03` 的形状（人肉传递一个判据）—— 中间任何一次编辑都会漂，
  而漂移**不报错**。本脚本每次运行都**重新从文件里读** ⇒ 用它的人不可能用到旧值。

★ 它**只读**：URI `mode=ro` + `PRAGMA query_only=1`，不写库、不发任何请求。
★ 它**不改库**：改 `ENABLE_PUBLISH` 要动 **NAS 上的** `.env`，那是**用户**的事
  （`ERR-ENV-02`：本机 `.env` ≠ NAS `.env`，所以本机连"它现在是什么"都看不到）。
"""
import argparse
import ast
import pathlib
import sqlite3
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
DST_DEFAULT = pathlib.Path("//iSunker-DS423/docker_ssd/musopia_script2")


def resolve_dst():
    """NAS 部署目录。优先级与 `deploy.sh` / `tools/nas_health.py` 一致
    （外部 DST → 同目录 `.deploy.nasrc` 的 `DST=` → 硬编码兜底）。"""
    import os
    v = os.environ.get("DST")
    if v:
        return pathlib.Path(v)
    nrc = ROOT / ".deploy.nasrc"
    if nrc.exists():
        for ln in nrc.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith("DST="):
                return pathlib.Path(ln[4:].strip())
    return DST_DEFAULT


def read_pending_where():
    """★★ 从 `db.py` **源码**里取 `PENDING_PUBLISH_WHERE` 的**字面量拼接结果**。

    ★ 为什么用 `ast` 而不是 import：`db.py` 顶部 `import config`，而 `config`
      构造函数里对缺的键 `sys.exit`（本机缺 QB_PASS / MUSOPIA_COOKIE）
      ⇒ import 根本走不到常量定义那行。
    ★ 为什么取的是"字面量拼接"而不是"求值"：这个常量是若干个相邻字符串
      **隐式拼接**（Python 的特性），外加注释。`ast` 会把它们合成一个
      `BinOp`/`Constant` —— 我们只要 `Constant` 的值，注释天然不在其中。
    ★ 取不到就**报错退出**，绝不返回一个空的 WHERE（那会数出 547 条，
      是一个**看着像真的**错数字 —— `ERR-META-01` 的形状）。
    """
    src = (ROOT / "db.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "PENDING_PUBLISH_WHERE":
                    val = _literal(node.value)
                    if not isinstance(val, str) or not val.strip():
                        raise SystemExit(
                            "❌ 取到的 PENDING_PUBLISH_WHERE 不是非空字符串 "
                            f"（得到 {val!r}）—— 拒绝继续")
                    return val
    raise SystemExit("❌ 在 db.py 里找不到 PENDING_PUBLISH_WHERE（常量改名了？）")


def _literal(node):
    """把 `Constant` / 相邻字符串隐式拼接（`BinOp(Add)`）折成一个字符串。"""
    if isinstance(node, ast.Constant):
        if node.value is None:
            return None
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal(node.left) + _literal(node.right)
    if isinstance(node, ast.JoinedStr):      # f-string：本常量里不该有
        raise SystemExit("❌ PENDING_PUBLISH_WHERE 里出现了 f-string，无法静态求值")
    raise SystemExit(f"❌ 无法静态求值的节点类型：{type(node).__name__}")


def main():
    ap = argparse.ArgumentParser(description="打印当前发布队列（只读）")
    ap.add_argument("--dst", default=None, help="NAS 部署目录（默认自动定位）")
    args = ap.parse_args()
    dst = pathlib.Path(args.dst) if args.dst else resolve_dst()
    dbf = dst / "data.db"

    where = read_pending_where()

    print("=" * 74)
    print("★ 发布队列（口径 = db.PENDING_PUBLISH_WHERE，从 db.py 源码实读）")
    print("=" * 74)
    print(f"  库        : {dbf}")
    print(f"  存在      : {dbf.exists()}")
    print()
    print("  四道闸门（这就是队列的全部判据）:")
    print(f"    {where}")
    print()

    if not dbf.exists():
        sys.exit("❌ 库不在 ⇒ 不做任何判断（'读不到' ≠ '队列是空的' —— ERR-SQL-05）")

    # ★ 只读打开：`mode=ro` 的 URI 形式在 **UNC 路径**上会炸
    #   （`sqlite3.OperationalError: invalid uri authority: iSunker-DS423`
    #    —— `//server/share` 被 URI 解析器当成了 authority，实测踩到）。
    #   ⇒ UNC 上退到 `query_only` 这一道，并**把只读性说出来**
    #   （`ERR-META-12`：别把"没拦住"当成"不会发生"）。
    try:
        conn = sqlite3.connect(
            f"file:{dbf.as_posix()}?mode=ro", uri=True, timeout=30)
        how = "URI mode=ro + query_only"
    except sqlite3.OperationalError:
        conn = sqlite3.connect(str(dbf), timeout=30)
        how = "★ 只靠 query_only（URI 只读在 UNC 上不可用）"
    conn.execute("PRAGMA query_only=1")
    print(f"  只读方式  : {how}")
    try:
        n = conn.execute(
            f"SELECT count(*) FROM tasks WHERE {where}").fetchone()[0]
        print(f"  ★ 队列条数 = {n}")
        if not n:
            print("\n  （空 —— 没有待发布的行）")
            return 0

        cols = ("info_hash", "torrent_name", "status", "site_status",
                "transfer_rule", "opencd_tid", "publish_after",
                "site_torrent_id")
        print("\n" + "-" * 74)
        print("逐条")
        print("-" * 74)
        for r in conn.execute(
                f"SELECT {', '.join(cols)} FROM tasks WHERE {where}"):
            d = dict(zip(cols, r))
            print(f"  {d['info_hash'][:12]}  {(d['torrent_name'] or '')[:48]}")
            print(f"      status={d['status']} site_status={d['site_status']} "
                  f"rule={d['transfer_rule']} tid={d['opencd_tid']}")
            print(f"      publish_after={d['publish_after']} "
                  f"site_torrent_id={d['site_torrent_id']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
