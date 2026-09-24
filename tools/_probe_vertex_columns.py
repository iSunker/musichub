"""★ 只读：vertex 库里到底存了什么？—— 「有没有不用爬网页就拿到的字段」

用户（2026-09-23）问了一句要害话：
    「这 248 个不是在 vertex 里面匹配到了种子 id 么？怎么又要你去 crawl 搜网页呢？」

★ 这个问题的实质是：**「匹配到 tid」和「拿到页面内容」是两件事。**
  `sync_opencd_tid.py` 只从 vertex 的 `torrents.link` 里抠出了
  `details.php?id=NNN`（正则 `/details\\.php\\?id=(\\d+)/`）——
  **一个页面编号，不是页面内容。**

⇒ 但"链接里没有"不等于"库里没有"。本脚本把 vertex `torrents` 表**每一列**都列出来，
  逐列报「非空几条 / 样本值」，让"有没有可以直接用的规则字段"这个问题**由数据回答**。

纪律：只读（`query_only=1`）、不 cp（`ERR-SQL-01`）、零请求。

用法:
    python tools/_probe_vertex_columns.py
"""
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

VERTEX_DB = r"//iSunker-DS423/docker_ssd/vertex2/vertex/db/sql.db"
QUEEN_TRACKER = "tracker.open.cd"


def main():
    print("=" * 78)
    print("★ 只读：vertex `torrents` 表的每一列（找「不用爬网页就能拿到的字段」）")
    print("=" * 78)
    print(f"  库: {VERTEX_DB}")
    if not os.path.exists(VERTEX_DB):
        print("  ❌ 读不到（挂载/路径问题）⇒ 非零退出，不静默（ERR-SQL-05）")
        return 1

    con = sqlite3.connect(VERTEX_DB, timeout=60)
    con.execute("PRAGMA query_only=1")
    con.row_factory = sqlite3.Row

    tabs = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print(f"\n  表: {tabs}")

    cols = [r[1] for r in con.execute("PRAGMA table_info(torrents)")]
    n_q = con.execute("SELECT count(*) FROM torrents WHERE tracker = ?",
                      (QUEEN_TRACKER,)).fetchone()[0]
    n_all = con.execute("SELECT count(*) FROM torrents").fetchone()[0]
    print(f"  torrents: {n_all} 行（其中 tracker={QUEEN_TRACKER} 的 {n_q} 行）")

    print("\n" + "-" * 78)
    print("① 每一列的「非空条数」+ 样本值（★ 只看皇后那批）")
    print("-" * 78)
    for c in cols:
        try:
            n = con.execute(
                f"SELECT count(*) FROM torrents WHERE tracker = ? "
                f"  AND [{c}] IS NOT NULL AND CAST([{c}] AS TEXT) <> ''",
                (QUEEN_TRACKER,)).fetchone()[0]
        except sqlite3.OperationalError as e:
            print(f"   {c:<20} ⚠️ 读不了: {e}")
            continue
        samples = []
        for r in con.execute(
                f"SELECT [{c}] FROM torrents WHERE tracker = ? "
                f"  AND [{c}] IS NOT NULL AND CAST([{c}] AS TEXT) <> '' LIMIT 2",
                (QUEEN_TRACKER,)):
            v = str(r[0])
            samples.append(v[:70])
        flag = "★" if n else " "
        print(f"  {flag} {c:<20} n={n:<6} {samples}")

    con.close()
    print("\n" + "=" * 78)
    print("★ 结论要看这张表：**有哪一列是「转载规则」或「页面正文」？**")
    print("  ★★ 若一列都没有 ⇒ 「匹配到 tid」确实只给了 id，规则仍必须读页面。")
    print("     那是**结构性**的，不是我选的路绕。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
