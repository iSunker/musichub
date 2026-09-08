"""
数据库层：schema 定义、自动迁移、通用查询。

为什么单独抽出来：原先 run.py / publish.py 各写了一份 init_db()，字段定义还不一致
（publish.py 的建表语句里没有 log_content / log_filename）。谁先跑谁建表，于是
update_metadata.py 查 log_content 时直接 OperationalError。schema 只能有一个来源。
"""
import sqlite3

import config

# 权威 schema：新库按这个建，旧库按这个补列。
COLUMNS = [
    ("info_hash", "TEXT PRIMARY KEY"),
    ("torrent_name", "TEXT"),
    ("status", "TEXT"),
    ("reason", "TEXT"),
    ("formatted_title", "TEXT"),
    ("cover_url", "TEXT"),
    ("torrent_file", "TEXT"),
    ("small_descr", "TEXT"),
    ("tracklist", "TEXT"),
    ("log_content", "TEXT"),
    ("log_filename", "TEXT"),
    ("content_path", "TEXT"),
    ("site_status", "TEXT DEFAULT 'UNKNOWN'"),
    ("site_torrent_id", "TEXT"),
    ("site_synced_at", "TIMESTAMP"),
    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
]


def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """建表 + 补列。任何脚本启动时都应该先调它，幂等。"""
    conn = connect()
    cur = conn.cursor()

    cols_sql = ",\n            ".join(f"{name} {decl}" for name, decl in COLUMNS)
    cur.execute(f"CREATE TABLE IF NOT EXISTS tasks (\n            {cols_sql}\n        )")

    cur.execute("PRAGMA table_info(tasks)")
    existing = {row[1] for row in cur.fetchall()}

    added = []
    for name, decl in COLUMNS:
        if name in existing:
            continue
        # PRIMARY KEY 不能通过 ALTER 加，但那只会发生在表本来就不存在的情况，
        # 而那时上面的 CREATE TABLE 已经建全了，所以这里安全跳过。
        if "PRIMARY KEY" in decl:
            continue
        cur.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
        added.append(name)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_site_id ON tasks(site_torrent_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, site_status)")

    conn.commit()
    conn.close()

    if added:
        print(f"🔧 数据库自动迁移，新增列: {', '.join(added)}")


# ==================== 查询 ====================

TASK_FIELDS = (
    "info_hash, torrent_name, formatted_title, small_descr, cover_url, "
    "tracklist, log_content, log_filename, torrent_file, content_path, "
    "site_status, site_torrent_id"
)


def _rows_to_dicts(cur):
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def get_pending_publish():
    """待发布：本地处理成功，但站点上还没有。"""
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        f"SELECT {TASK_FIELDS} FROM tasks "
        "WHERE status = 'SUCCESS' AND (site_status IS NULL OR site_status != 'EXISTS')"
    )
    rows = _rows_to_dicts(cur)
    conn.close()
    return rows


def get_published(only_missing_id=False, torrent_id=None, only_with_cover=False,
                  only_stale=False):
    """
    已在站点上存在的任务。

    only_stale=True 时只返回"本地内容比站点新"的记录：从没同步过
    （site_synced_at IS NULL），或同步之后本地又变过（updated_at > site_synced_at）。
    定时任务必须用这个，否则每轮都要把 250+ 个页面全部拉一遍 edit.php 做对比，
    纯属拿站点带宽换空转。
    """
    where = ["site_status = 'EXISTS'"]
    params = []

    if torrent_id is not None:
        where.append("site_torrent_id = ?")
        params.append(str(torrent_id))
    elif only_missing_id:
        where.append("(site_torrent_id IS NULL OR site_torrent_id = '')")
    else:
        where.append("site_torrent_id IS NOT NULL AND site_torrent_id != ''")

    if only_with_cover:
        where.append("cover_url IS NOT NULL AND cover_url != ''")

    if only_stale:
        where.append("(site_synced_at IS NULL OR updated_at > site_synced_at)")


    conn = connect()
    cur = conn.cursor()
    cur.execute(
        f"SELECT {TASK_FIELDS} FROM tasks WHERE {' AND '.join(where)} ORDER BY updated_at",
        params,
    )
    rows = _rows_to_dicts(cur)
    conn.close()
    return rows


def get_missing_cover(published_only=True):
    """缺封面的任务。发封面要回原目录找图，所以顺带把 content_path 带出来。"""
    where = ["(cover_url IS NULL OR cover_url = '')"]
    if published_only:
        where.append("site_status = 'EXISTS'")
    else:
        where.append("status IN ('SUCCESS', 'PUBLISHED')")

    conn = connect()
    cur = conn.cursor()
    cur.execute(f"SELECT {TASK_FIELDS} FROM tasks WHERE {' AND '.join(where)}")
    rows = _rows_to_dicts(cur)
    conn.close()
    return rows


# ==================== 写入 ====================

def save_task(**kw):
    """UPSERT 一条任务。只更新显式传入的字段，不会把没传的字段冲成空。"""
    info_hash = kw.pop("info_hash")
    if not info_hash:
        raise ValueError("save_task 必须提供 info_hash")

    valid = {name for name, _ in COLUMNS}
    unknown = set(kw) - valid
    if unknown:
        raise ValueError(f"save_task 收到未知字段: {unknown}")

    cols = ["info_hash"] + list(kw.keys())
    vals = [info_hash] + list(kw.values())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in kw.keys())

    # 关键：只在真的有字段变化时才更新（含 updated_at）。
    #
    # 原先无条件写 updated_at=CURRENT_TIMESTAMP，于是 scan 阶段每半小时重扫一遍，
    # 即使内容一个字都没变，254 条记录的 updated_at 也全被刷新 —— 它们随即全部
    # 满足 --stale-only 的 "updated_at > site_synced_at"，edit 阶段每轮都要把整站
    # 页面重新拉一遍做对比。按 10 个/分钟算，每 30 分钟里有 25 分钟在做无用请求。
    #
    # SQLite 的 upsert 支持在 DO UPDATE 后跟 WHERE；IS NOT 能正确处理 NULL
    # （普通 != 遇到 NULL 会得到 NULL 而被当假，导致 NULL->有值 的变化被漏掉）。
    diff_cond = " OR ".join(f"tasks.{c} IS NOT excluded.{c}" for c in kw.keys())

    sql = (
        f"INSERT INTO tasks ({', '.join(cols)}, updated_at) "
        f"VALUES ({placeholders}, CURRENT_TIMESTAMP) "
        f"ON CONFLICT(info_hash) DO UPDATE SET {updates}, updated_at=CURRENT_TIMESTAMP "
        f"WHERE {diff_cond}"
    )


    conn = connect()
    conn.execute(sql, vals)
    conn.commit()
    conn.close()


def set_site_id(info_hash, torrent_id, site_status="EXISTS", status=None, reason=None):
    sets = ["site_torrent_id = ?", "site_status = ?", "updated_at = CURRENT_TIMESTAMP"]
    params = [str(torrent_id), site_status]
    if status:
        sets.append("status = ?")
        params.append(status)
    if reason is not None:
        sets.append("reason = ?")
        params.append(reason)
    params.append(info_hash)

    conn = connect()
    conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE info_hash = ?", params)
    conn.commit()
    conn.close()


def set_cover(info_hash, cover_url):
    conn = connect()
    conn.execute(
        "UPDATE tasks SET cover_url = ?, updated_at = CURRENT_TIMESTAMP WHERE info_hash = ?",
        (cover_url, info_hash),
    )
    conn.commit()
    conn.close()


def mark_synced(info_hash):
    conn = connect()
    conn.execute(
        "UPDATE tasks SET site_synced_at = CURRENT_TIMESTAMP WHERE info_hash = ?",
        (info_hash,),
    )
    conn.commit()
    conn.close()


def stats():
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM tasks")
    total = cur.fetchone()[0]
    cur.execute("SELECT status, site_status, count(*) FROM tasks GROUP BY status, site_status")
    groups = cur.fetchall()
    cur.execute(
        "SELECT count(*) FROM tasks WHERE site_status='EXISTS' "
        "AND (site_torrent_id IS NULL OR site_torrent_id='')"
    )
    no_id = cur.fetchone()[0]
    cur.execute(
        "SELECT count(*) FROM tasks WHERE site_status='EXISTS' "
        "AND (cover_url IS NULL OR cover_url='')"
    )
    no_cover = cur.fetchone()[0]
    conn.close()
    return {"total": total, "groups": groups, "no_site_id": no_id, "no_cover": no_cover}
