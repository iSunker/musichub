"""只读探针：执行 A3（把 site_publish(musopia) 那 264 行改 DELETED）之前的**风险评估**。

★ 这是 A3 的前置检查，不是执行。只读：PRAGMA query_only=1。

★★ 为什么必须先跑这个（A3 的雷）：
   清掉台账 ⇒ 264 条重新走 `db.PENDING_PUBLISH_WHERE`
   ⇒ 该 WHERE 里的闸门④ 是 `transfer_rule = 'ALLOW'`。
   ⇒ 若这 264 条里有 DENY / NULL 的，它们**会被闸门④拦下** ——
      结果是"台账说 DELETED、程序永不重发、站上也没有" = **静默丢失**。
   ⇒ 必须先把"清完会真发出去"与"清完会丢失"两个集合数出来。
"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

DB = "//iSunker-DS423/docker_ssd/opencd2others/data/data.db"
SITE = "musopia"

conn = sqlite3.connect(DB, timeout=30)
conn.execute("PRAGMA query_only=1")
q = lambda sql, *a: conn.execute(sql, a).fetchall()
one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]


def hdr(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


hdr("① 清台账前的题面（复核上次的 280 / 264）")
pub_exists = one("SELECT count(*) FROM site_publish WHERE site=? AND status='EXISTS'", SITE)
tsk_exists = one("SELECT count(*) FROM tasks WHERE site_status='EXISTS'")
print(f"  site_publish({SITE}).status='EXISTS' : {pub_exists}")
print(f"  tasks.site_status='EXISTS'         : {tsk_exists}")
print("  ⇒ A3 要改的是 **两列都是 EXISTS 之外** 的那些 site_publish 行")
n_target = one("""
SELECT count(*) FROM site_publish sp JOIN tasks t ON t.info_hash=sp.info_hash
 WHERE sp.site=? AND sp.status='EXISTS' AND t.site_status!='EXISTS'""", SITE)
print(f"  ⇒ A3 目标行数                       : {n_target}")

hdr("② ★★ 那 264 行的 transfer_rule 分布（决定清完会不会丢种子）")
print("  口径：A3 目标行 JOIN tasks.transfer_rule")
for rule, n in q("""
SELECT COALESCE(NULLIF(t.transfer_rule,''), '<NULL/空>'), count(*)
  FROM site_publish sp JOIN tasks t ON t.info_hash=sp.info_hash
 WHERE sp.site=? AND sp.status='EXISTS' AND t.site_status!='EXISTS'
 GROUP BY 1 ORDER BY count(*) DESC""", SITE):
    print(f"    {n:>6}  transfer_rule = {rule!r}")

hdr("③ ★★ 拆成两个集合：清完「会真发」 vs 「会静默丢失」")
for label, cond, n in [
    ("闸门④放行 ⇒ 会重发（status=SUCCESS 且 ALLOW 且其余闸门过）", "ALLOW", None),
]:
    pass
res = q("""
SELECT
  SUM(CASE WHEN t.transfer_rule='ALLOW' THEN 1 ELSE 0 END),
  SUM(CASE WHEN t.transfer_rule!='ALLOW' THEN 1 ELSE 0 END)
  FROM site_publish sp JOIN tasks t ON t.info_hash=sp.info_hash
 WHERE sp.site=? AND sp.status='EXISTS' AND t.site_status!='EXISTS'""", SITE)[0]
print(f"    transfer_rule='ALLOW'  ⇒ 闸门④过 : {res[0]}")
print(f"    transfer_rule!='ALLOW' ⇒ 会被拦下: {res[1]}   ← ★ 这批清完台账就丢了")

hdr("④ 闸门④过的那批，再逐条查其余闸门（PENDING_PUBLISH_WHERE 全链）")
# ★ 判据不手抄 —— 但 `import db` 会连 `config` 一起拉，而 config 要求 QB_PASS
#   等环境变量（本机没有）。⇒ 用 **AST 抽取源码里的常量字符串**，
#   既不手抄、也不触发 import 的副作用。
import ast  # noqa: E402
import pathlib  # noqa: E402
# ★ 2026-10-06：源码已移进 `src/`。原先这里是**裸相对路径** `Path("db.py")`
#   （依赖 cwd = 仓库根）—— 移动后必断，且断得**静默**（cwd 恰好对就"看起来正常"）。
#   ⇒ 改成基于 `__file__` 定位，与 cwd 无关。
_src = (pathlib.Path(__file__).resolve().parent.parent / "src" / "db.py").read_text(encoding="utf-8")
_pw = None
for _n in ast.parse(_src).body:
    if isinstance(_n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PENDING_PUBLISH_WHERE"
            for t in _n.targets):
        _pw = ast.literal_eval(_n.value)
assert _pw, "没从 db.py 抽到 PENDING_PUBLISH_WHERE"
print(f"  PENDING_PUBLISH_WHERE（AST 抽取） =\n    {_pw}")
sql = (f"SELECT t.status, t.site_status, count(*) FROM tasks t "
       f" WHERE t.info_hash IN (SELECT sp.info_hash FROM site_publish sp "
       f"   WHERE sp.site=? AND sp.status='EXISTS' AND t.info_hash=sp.info_hash) "
       f" GROUP BY 1,2 ORDER BY 3 DESC")
# 上面那句在子查询里引外部列会报错 —— 用显式 JOIN 写
sql = (f"SELECT t.status, COALESCE(t.site_status,'<NULL>'), count(*) "
       f"  FROM tasks t JOIN site_publish sp ON sp.info_hash=t.info_hash "
       f" WHERE sp.site=? AND sp.status='EXISTS' GROUP BY 1,2 ORDER BY 3 DESC")
for st, ss, n in q(sql, SITE):
    print(f"    {n:>6}  status={st!r:<20} site_status={ss!r}")

hdr("⑤ 那 264 行里，真正会走完 PENDING_PUBLISH_WHERE 的有几条")
# ★ 裸列名在 JOIN 里歧义 ⇒ 把 `PENDING_PUBLISH_WHERE` 里的列名限定到 `t.`
#   （只加表别名，不动判据本身：先 __ 再 status，避免把 site_status 改坏）
_pw_t = (_pw.replace("site_status", "t.__SS__")
             .replace("status", "t.status")
             .replace("opencd_added_at", "t.opencd_added_at")
             .replace("t.__SS__", "t.site_status"))
print(f"  限定后的判据 =\n    {_pw_t}")
nfull = one(f"SELECT count(*) FROM tasks t JOIN site_publish sp ON sp.info_hash=t.info_hash "
            f" WHERE sp.site=? AND sp.status='EXISTS' AND {_pw_t} "
            f"   AND t.site_status!='EXISTS'", SITE)
print(f"    {nfull}   ← ★ 这就是 A3 清完台账后**会进乌托邦队列**的条数")
print("       （其余的被闸门②/③④拦下 ⇒ 需要单独看，见 ⑥）")

hdr("⑥ 被拦下的都是哪一条闸门（逐条判据，不合并报）")
rows = q(f"SELECT t.info_hash, t.status, t.transfer_rule, t.opencd_tid, "
         f"       t.opencd_added_at, t.mapped_json, t.site_status "
         f"  FROM tasks t JOIN site_publish sp ON sp.info_hash=t.info_hash "
         f" WHERE sp.site=? AND sp.status='EXISTS' AND t.site_status!='EXISTS'", SITE)
g4 = g2 = g3 = 0
for h, st, tr, tid, added, mapped, ss in rows:
    if tr != "ALLOW":
        g4 += 1
        continue
    if not (added and added <= (one("SELECT datetime('now','-7 days')"))):
        g2 += 1
        continue
    if tid and not (mapped or "").strip():
        g3 += 1
print(f"    闸门④ transfer_rule!='ALLOW'     拦下 : {g4}")
print(f"    闸门② opencd_added_at 缺/不足 7 天  拦下 : {g2}")
print(f"    闸门③ opencd_tid 有但 mapped_json 空 拦下 : {g3}")
print(f"    ⇒ 合计拦下 {g4+g2+g3}，与 264 对得上吗: {g4+g2+g3 == len(rows)}")

conn.close()
