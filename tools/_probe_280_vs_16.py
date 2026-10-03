"""只读探针：`site_publish(musopia)` 有 280 行 EXISTS，而 `tasks.site_status`
只有 16 行 EXISTS —— 这 264 行的差异到底是**什么**。

★ 只读：`PRAGMA query_only=1`，绝不写库（ERR-SQL-01 那一族的纪律）。
★ 不自己另写一套判据：能复用 `db.py` 的常量就复用；只是这里要**按列**拆开看，
   所以必须自己 SELECT —— 那就把口径写清楚，并同时打印两列各自的分布。
"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

DB = "//iSunker-DS423/docker_ssd/opencd2others/data/data.db"

# ★★★ 2026-09-28：本文件上方那句"不自己另写一套判据"原先**没做到** —— ⑨ 节
#   手写了一遍转种口径，而且写的是**过时**的那一份（漏了闸门④）。
#   ⇒ 这里从 `db.py` 的**源码**用 AST 取常量（★ 不 `import db`：那会连 `config`
#     一起拉，而 `config` 在 import 期就要求 `QB_PASS` 等环境变量 ⇒ 在没有 `.env`
#     的机器上**连探针都起不来**；成例见 `tools/fix_onsite_titles_and_subtitles.py`
#     的 `_edit_rule_guard()`）。取不到就**如实报错退出**，不退回手写一份
#     （`ERR-META-14`：兜底值不许冒充真结果）。
def _where_const(name):
    """从 `db.py` 源码取一个**字符串常量**（能解引用同文件里更早的常量）。

    ★★★ 为什么不直接用 `ast.literal_eval`：`TRANSFERABLE_WHERE` 的值是
      `CROSS_SITE_DEDUP_WHERE + " AND transfer_rule = 'ALLOW'"`，而
      `literal_eval` **不认 `Name` 节点** ⇒ 实测直接
      `ValueError: malformed node or string … Name(id='CROSS_SITE_DEDUP_WHERE')`。
      ⇒ 自己走一遍赋值**按出现顺序**求值（常量都在同一个模块顶部、顺序有保证）。
    ★★ 为什么**不** `import db` 取常量：那会连 `config` 一起拉，而 `config` 在
      import 期就 `require_env()`（`QB_PASS` 等）⇒ 没有 `.env` 的机器上**连探针都
      起不来**（成例：`tools/fix_onsite_titles_and_subtitles.py` 的
      `_edit_rule_guard()`）。
    ★ 取不到就**如实报错退出**，不退回手写一份口径（`ERR-META-14`）。
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "db.py"
    ns = {}
    for n in ast.parse(src.read_text(encoding="utf-8")).body:
        if not isinstance(n, ast.Assign):
            continue
        for t in n.targets:
            if not isinstance(t, ast.Name):
                continue
            try:
                # eval 只在这份源码的顶层赋值上跑，且用**空** builtins、
                # 只允许引用前面已解出的常量 ⇒ 不是任意代码执行。
                ns[t.id] = eval(  # noqa: S307
                    compile(ast.Expression(n.value), "<db.py>", "eval"),
                    {"__builtins__": {}}, dict(ns))
            except Exception:                                # noqa: BLE001
                ns.pop(t.id, None)   # 不是可静态求值的赋值 ⇒ 记不住，跳过
    if not isinstance(ns.get(name), str):
        raise SystemExit(
            f"❌ 拿不到 db.{name}（解出来是 {type(ns.get(name)).__name__}）"
            f"—— 拒绝在这里手写一份口径（手抄必然漂移）")
    return ns[name]


TRANSFERABLE_WHERE = _where_const("TRANSFERABLE_WHERE")

conn = sqlite3.connect(DB, timeout=30)
conn.execute("PRAGMA query_only=1")
q = lambda sql, *a: conn.execute(sql, a).fetchall()
one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]


def hdr(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


hdr("① 两个「EXISTS」各自的数（先确认题面）")
pub_exists = one("SELECT count(*) FROM site_publish WHERE site='musopia' AND status='EXISTS'")
pub_all = one("SELECT count(*) FROM site_publish WHERE site='musopia'")
tsk_exists = one("SELECT count(*) FROM tasks WHERE site_status='EXISTS'")
print(f"  site_publish(musopia) status='EXISTS' : {pub_exists}")
print(f"  site_publish(musopia) 总行数          : {pub_all}")
print(f"  tasks.site_status='EXISTS'            : {tsk_exists}")

hdr("② 两表按 info_hash 全外连接 —— 差异的四种形状（★ 关键）")
print("  行 = 互斥的四种情况，合计应为 tasks 与 site_publish 各自的总数")
rows = q("""
SELECT
  (SELECT count(*) FROM site_publish sp
     JOIN tasks t ON t.info_hash = sp.info_hash
    WHERE sp.site='musopia' AND sp.status='EXISTS' AND t.site_status='EXISTS')  AS both_exists,
  (SELECT count(*) FROM site_publish sp
     JOIN tasks t ON t.info_hash = sp.info_hash
    WHERE sp.site='musopia' AND sp.status='EXISTS' AND t.site_status!='EXISTS') AS pub_only,
  (SELECT count(*) FROM site_publish sp
     LEFT JOIN tasks t ON t.info_hash = sp.info_hash
    WHERE sp.site='musopia' AND sp.status='EXISTS' AND t.info_hash IS NULL)     AS pub_orphan,
  (SELECT count(*) FROM tasks t
     WHERE t.site_status='EXISTS'
       AND NOT EXISTS (SELECT 1 FROM site_publish sp
                        WHERE sp.info_hash=t.info_hash AND sp.site='musopia'
                          AND sp.status='EXISTS'))                              AS tsk_only
""")[0]
labels = ["两列都是 EXISTS", "site_publish=EXISTS 但 tasks 不是",
          "site_publish 有、tasks 没这行（孤儿）",
          "tasks=EXISTS 但 site_publish 没有"]
for lb, v in zip(labels, rows):
    print(f"    {v:>6}  {lb}")

hdr("③ 那 264 行在 tasks 侧的真实分布（按 site_status 分组）")
print("  口径：site_publish(musopia).status='EXISTS' 且 tasks.site_status != 'EXISTS'")
for st, n in q("""
SELECT COALESCE(t.site_status,'<NULL>'), count(*)
  FROM site_publish sp JOIN tasks t ON t.info_hash=sp.info_hash
 WHERE sp.site='musopia' AND sp.status='EXISTS' AND t.site_status!='EXISTS'
 GROUP BY t.site_status ORDER BY count(*) DESC"""):
    print(f"    {n:>6}  tasks.site_status = {st!r}")

hdr("④ 那 264 行在 tasks 侧还有没有 site_torrent_id（= 记录是否完整）")
for n, sid in q("""
SELECT count(*),
       CASE WHEN t.site_torrent_id IS NULL OR t.site_torrent_id=''
            THEN 'id 也是空/无' ELSE 'id 还在' END
  FROM site_publish sp JOIN tasks t ON t.info_hash=sp.info_hash
 WHERE sp.site='musopia' AND sp.status='EXISTS' AND t.site_status!='EXISTS'
 GROUP BY 2"""):
    print(f"    {n:>6}  {sid}")

hdr("⑤ 那 264 行在 site_publish 侧的 torrent_id 是否为空（异常形状）")
for n, sid in q("""
SELECT count(*),
       CASE WHEN sp.torrent_id IS NULL OR sp.torrent_id=''
            THEN '★ torrent_id 空（记录坏了）' ELSE 'torrent_id 有值' END
  FROM site_publish sp JOIN tasks t ON t.info_hash=sp.info_hash
 WHERE sp.site='musopia' AND sp.status='EXISTS' AND t.site_status!='EXISTS'
 GROUP BY 2"""):
    print(f"    {n:>6}  {sid}")

hdr("⑥ 差异行怎么来的：那 264 行里 status 是什么（谁写过它们）")
for st, n in q("""
SELECT t.status, count(*)
  FROM site_publish sp JOIN tasks t ON t.info_hash=sp.info_hash
 WHERE sp.site='musopia' AND sp.status='EXISTS' AND t.site_status!='EXISTS'
 GROUP BY t.status ORDER BY count(*) DESC"""):
    print(f"    {n:>6}  tasks.status = {st!r}")

hdr("⑦ 时间线：site_publish 的 updated_at 与 tasks 的 updated_at 谁更新")
for n, a, b in [q("""
SELECT count(*),
  CASE WHEN sp.updated_at > t.updated_at THEN 'site_publish 更晚'
       WHEN sp.updated_at < t.updated_at THEN 'tasks 更晚'
       ELSE '相同' END,
  CASE WHEN sp.synced_at IS NULL OR sp.synced_at='' THEN 'synced_at 空'
       ELSE 'synced_at 有' END
  FROM site_publish sp JOIN tasks t ON t.info_hash=sp.info_hash
 WHERE sp.site='musopia' AND sp.status='EXISTS' AND t.site_status!='EXISTS'
 GROUP BY 2,3 ORDER BY count(*) DESC""")][0]:
    print(f"    {n:>6}  {a} / {b}")

hdr("⑧ 会不会是 reset_deleted_site_status.py 干的？")
print("  它改 site_status 'EXISTS'→'UNKNOWN' 且**不碰** site_publish")
print("  口径：site_status='UNKNOWN' 且有 opencd_tid（那时回填过 tid 的 224 条）")
n_un = one("""
SELECT count(*) FROM site_publish sp JOIN tasks t ON t.info_hash=sp.info_hash
 WHERE sp.site='musopia' AND sp.status='EXISTS'
   AND t.site_status='UNKNOWN'
   AND t.opencd_tid IS NOT NULL AND t.opencd_tid != ''""")
print(f"    site_status='UNKNOWN' 且 opencd_tid 非空 : {n_un}")
print(f"    那 264 行里 opencd_tid 非空的            : "
      f"{one('SELECT count(*) FROM site_publish sp JOIN tasks t ON t.info_hash=sp.info_hash'
             ' WHERE sp.site=? AND sp.status=? AND t.site_status!=?'
             '   AND t.opencd_tid IS NOT NULL AND t.opencd_tid != ?',
             'musopia', 'EXISTS', 'EXISTS', '')}")

hdr("⑨ 转种队列的门槛落在哪（闸门① 是 tasks.site_status）")
# ★★★ 2026-09-28 订正：这一节原先把"队列"写成了 `EXISTS + id 非空 + 蟹黄堡无行`
#   —— 那是 `CROSS_SITE_DEDUP_WHERE`（**事实**口径），**漏了闸门④
#   `transfer_rule='ALLOW'`**。转种路实际用的是 `db.TRANSFERABLE_WHERE`
#   （见 `ERR-DATA-08`：实测 `15972` 是 `EXISTS` + `DENY`，旧口径会把它转出去）。
#   ★ 为什么这里改成**从 db 取常量**而不是再手写一遍：手写必然漂移，
#     而漂移的表现是"探针数出 29、实际发 26"，不报错（`ERR-META-03`）。
print(f"  tasks.site_status='EXISTS'            : {tsk_exists}")
print(f"  且 site_torrent_id 非空               : "
      f"{one('SELECT count(*) FROM tasks WHERE site_status=?'
             ' AND site_torrent_id IS NOT NULL AND site_torrent_id != ?',
             'EXISTS', '')}")
print(f"  且 transfer_rule='ALLOW'（闸门④）      : "
      f"{one(f'SELECT count(*) FROM tasks WHERE {TRANSFERABLE_WHERE}')}")
print(f"  且 site_publish(crabpt) 无行(=转种队列) : "
      f"{one(f'SELECT count(*) FROM tasks t WHERE {TRANSFERABLE_WHERE}'
             '   AND NOT EXISTS (SELECT 1 FROM site_publish sp'
             '                    WHERE sp.info_hash=t.info_hash AND sp.site=?)',
             'crabpt')}")
print(f"  （对照·旧口径 CROSS_SITE_DEDUP_WHERE）  : "
      f"{one('SELECT count(*) FROM tasks t WHERE t.site_status=?'
             '   AND t.site_torrent_id IS NOT NULL AND t.site_torrent_id != ?'
             '   AND NOT EXISTS (SELECT 1 FROM site_publish sp'
             '                    WHERE sp.info_hash=t.info_hash AND sp.site=?)',
             'EXISTS', '', 'crabpt')}   ★ 这个数含未过闸门④ 的行，别再拿它当队列")

conn.close()
