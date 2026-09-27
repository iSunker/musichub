"""只读探针：`site_publish(musopia)` 有 280 行 EXISTS，而 `tasks.site_status`
只有 16 行 EXISTS —— 这 264 行的差异到底是**什么**。

★ 只读：`PRAGMA query_only=1`，绝不写库（ERR-SQL-01 那一族的纪律）。
★ 不自己另写一套判据：能复用 `db.py` 的常量就复用；只是这里要**按列**拆开看，
   所以必须自己 SELECT —— 那就把口径写清楚，并同时打印两列各自的分布。
"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

DB = "//iSunker-DS423/docker_ssd/musopia_script2/data.db"

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
print(f"  tasks.site_status='EXISTS'            : {tsk_exists}")
print(f"  且 site_torrent_id 非空               : "
      f"{one('SELECT count(*) FROM tasks WHERE site_status=?'
             ' AND site_torrent_id IS NOT NULL AND site_torrent_id != ?',
             'EXISTS', '')}")
print(f"  且 site_publish(crabpt) 无行(=队列)   : "
      f"{one('SELECT count(*) FROM tasks t WHERE t.site_status=?'
             '   AND t.site_torrent_id IS NOT NULL AND t.site_torrent_id != ?'
             '   AND NOT EXISTS (SELECT 1 FROM site_publish sp'
             '                    WHERE sp.info_hash=t.info_hash AND sp.site=?)',
             'EXISTS', '', 'crabpt')}")

conn.close()
