"""
数据库层：schema 定义、自动迁移、通用查询。

为什么单独抽出来：原先 run.py / publish.py 各写了一份 init_db()，字段定义还不一致
（publish.py 的建表语句里没有 log_content / log_filename）。谁先跑谁建表，于是
update_metadata.py 查 log_content 时直接 OperationalError。schema 只能有一个来源。
"""
import json
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
    # 多 CD 的每碟日志（[{"label","filename","content"}, ...]）序列化后存这里。
    # log_content / log_filename 仍保留首个日志，供旧显示/预览兼容。
    ("logs_json", "TEXT"),
    ("content_path", "TEXT"),
    ("site_status", "TEXT DEFAULT 'UNKNOWN'"),
    ("site_torrent_id", "TEXT"),
    ("site_synced_at", "TIMESTAMP"),
    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),

    # ==================== OpenCD 转载流水线（第二阶段）====================
    # ★ 与第一阶段的记录**共用同一张表**，靠 `source` 区分来源。
    #   为什么不新起一张表：`daemon.preflight()` 的空库保护认的是 `tasks` 计数
    #   （`db.py` 的 stats()），新表不并入这个口径的话，「库是空的」这句判断就
    #   **看不见新数据** —— 保护会在该响时不响（ERR-META-03 家族：口径不一致）。
    #   而且 COLUMNS + init_db() 本来就能给旧库自动补列，白拿一套迁移。
    #
    # 皇后侧的 tid（页面 id）。★ 与 `site_torrent_id` 是**两件事**：
    #   同一个 infohash 在皇后和 Musopia 上是两个不同的页面编号，别混成一列。
    ("opencd_tid", "TEXT"),
    # 两个 3 天闸门（闸门①=做种时长，闸门②=抓页面后 3 天）：
    #   闸门① `qb_seed_days` —— 在 qB 做种满几天（★ 它证明种子真的下完可用，
    #          不只是"延迟了"）。没满 3 天**根本不抓**，更省请求。
    #   闸门② `fetched_at`  →  `publish_after`
    #          抓页面的时刻 + 3 天 = 可以发布的时刻。含义是
    #          "我已经看过页面内容、反悔还来得及"，所以是**串联**不是"做种满6天"。
    ("qb_seed_days", "REAL"),
    ("fetched_at", "TIMESTAMP"),
    ("publish_after", "TIMESTAMP"),
    # 映射不上的标签等 —— 要显式列出、进每日邮件，**不许静默丢**
    ("unmapped_json", "TEXT"),
    # ★★ 艺术家 / 发行年份（2026-09-21 补）—— 皇后的「藝術家名」/「發行年份」。
    #   ★ 为什么必须**落库**、不能在发布那一刻现取：
    #     发布路径（`publish.py`）拿的是**库里的行**，它**不回头抓皇后页面**。
    #     原先这两列不存在 ⇒ `task.get("artist")` 恒为 None ⇒
    #     乌托邦表单的 `custom_fields[4][1]` / `[4][2]` **永远是空的**，
    #     哪怕 `descr.py` 那边写得再对（这是同一个缺口的**下半段**，
    #     只改渲染层修不好 —— 实测就是先改了渲染层、④ 判据仍报 MISS 才发现的）。
    #   ★ `year` 存**原样**（如 `'1998年'`，带「年」字）：站点要不要纯数字**未确认**，
    #     存原值才不会在入库时就把证据洗掉（要转就在发送前转，转法可回读校验）。
    ("artist", "TEXT"),
    ("year", "TEXT"),
    # ★★ `map_all()` 的**证据快照**（2026-09-21 补，配套写入侧 `opencd_ingest.py`）。
    #
    #   为什么是**一个 JSON 列**而不是 4 个真列（`tags`/`medium_sel`/`audiocodec_sel`/`team_sel`）：
    #   ① 这四个值**只在发布时被消费一次**，没有任何 SQL 查询价值
    #      （不像 `publish_after` 要进 WHERE）⇒ 加真列就是"只写不读"的列；
    #   ② `save_task` 是按 `COLUMNS` 白名单校验的，加 4 列 = 给白名单开 4 个口子；
    #   ③ 一个 JSON 还**天然装下 `unmapped`**（映射不上的标签必须显式落盘、不许静默丢）。
    #
    #   ★ 它是**快照**不是缓存：`TAG_NAME_MAP` / `MEDIUM` / `TEAM_NAME_MAP` 都会变
    #     （44 项标签表刚改过、WAV 编号未定），所以**发布时要用的就是三天前
    #     抓页面时做的那个决定**，不能到时候重算（重算结果会漂）。
    #   ★ 读它一律走 `_load_mapped()`：解析失败**抛出**（见那里的注释）。
    ("mapped_json", "TEXT"),
]


# ==================== 配额账本（跨轮持久）====================
#
# 为什么需要它：`opencd_rate.RateGate` 的「每日上限」原先**每轮归 0** ——
#   daemon 每个周期用 `subprocess.Popen` 起一个**新进程**跑 crawl，而 gate 是
#   进程内对象 ⇒ `_count_day` 随进程一起消失 ⇒ 那个「每日 50」实际是「每轮 20」。
#   要让「每天 N 个请求」成为**真上限**，计数必须落在进程之外。
#
# ★ 为什么落在 `data.db` 而不是新开一个 JSON 文件：
#   ① `init_db()` 已经是**唯一的幂等迁移点**（建表 + 补列），新表白拿一套；
#      新文件则要重做一遍"建/迁移/原子写"。
#   ② `deploy.sh` 的 `NEVER` 名单护着 `data.db`（不会被本地覆盖），
#      新文件得再往名单里加一条 —— 而**漏加的那次是静默的**。
#   ③ compose 已经把 `/data` bind mount 出来了，SMB 上直接可查可备份。
#
# ★ 为什么不并进 `tasks` 的某一列：它是**全局计数器**，不是"某行的属性"。
#   没有 info_hash 可挂 —— 塞进 tasks 要么给 544 行各重复存一份、要么全 NULL，
#   两种都不是"这个数字属于哪一行"能表达的关系。
#
# ★ 表再小也要独立：将来若要记「按 kind 分账」（如 `seed` 与 `request` 各一份），
#   加一个 kind 值即可，不动任何已有列。
LEDGER_TABLE = "opencd_quota"
LEDGER_KIND_REQUEST = "request"          # 目前只有这一种；种子量纲的账将来加 kind
LEDGER_COLUMNS = [
    # ★★ `day_key` 是**本地日期字符串**（'YYYY-MM-DD'），**故意不是 UTC**。
    #   用户的心里模型是"本地日历日"（跨零点就该重置），所以本地日才是对的口径。
    #   ★ 而**存字符串**（不是 timestamp）就没东西可混 —— `ERR-SQL-03` 的教训是
    #     两套时钟口径（库内 UTC / 日志 CST）混在一起得出"今晚没写入"的错结论。
    #     这里从根上避免：是字符串，就没人会拿它去 `datetime('now')` 比。
    #   ★ **不要**"顺手修正"成 UTC —— 那会让重置时刻与用户的日历对不上。
    ("day_key", "TEXT NOT NULL"),
    ("kind", "TEXT NOT NULL"),
    ("used", "INTEGER NOT NULL DEFAULT 0"),
    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
]
LEDGER_PK = "PRIMARY KEY (day_key, kind)"


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

    # 配额账本（见上面 LEDGER_* 那一节的理由）。★ 与 tasks 同一个连接、同一次
    # commit 建出来 —— daemon 启动时只调一次 init_db()，表就一定在。
    _led_cols = ",\n            ".join(f"{n} {d}" for n, d in LEDGER_COLUMNS)
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (\n"
        f"            {_led_cols},\n            {LEDGER_PK}\n        )"
    )

    conn.commit()
    conn.close()

    if added:
        print(f"🔧 数据库自动迁移，新增列: {', '.join(added)}")


# ==================== 查询 ====================

TASK_FIELDS = (
    "info_hash, torrent_name, formatted_title, small_descr, cover_url, "
    "tracklist, log_content, log_filename, logs_json, torrent_file, content_path, "
    "site_status, site_torrent_id, "
    # ★★ 以下 6 列 2026-09-21 补 —— **不补它们 = 写进库但发布时读不到**。
    #    `get_pending_publish()` 只 SELECT 这个字符串，而 `descr.build_form_fields()`
    #    要读 `opencd_tid`（决定皇后路分流）与 `artist`/`year`；
    #    皇后行的 `tags`/`medium_sel`/… 则要从 `mapped_json` 解出来。
    #    ★ 这个坑本项目**已经栽过三次**（"数据在手边、就是没传进 task"⇒ 静默变空），
    #      所以这里逐列写明它给谁用：
    "opencd_tid, artist, year, unmapped_json, mapped_json, publish_after"
)

# ── 「待发布」的口径（★ 唯一来源，别在别处再写一份）────────────────────────
#
# ★ 抽成模块常量的理由同 `STALE_CONDITION`：将来若有第二个查询要按同一口径
#   **数**待办（比如 daemon 的探针），两处各写一份必然漂移，而漂移的表现是
#   「探针说有活 → 一条都选不出来 → 探针还说有活」的**静默空转**，不报任何错。
PENDING_PUBLISH_WHERE = (
    "status = 'SUCCESS' AND (site_status IS NULL OR site_status != 'EXISTS') "
    # ★★ 闸门②（抓取时刻 + 3 天 = 可发布时刻）—— 2026-09-21 补。
    #    ★ 它此前是个**死列**：`publish_after` 写进去也没人查 ⇒ 皇后行一旦
    #      `status='SUCCESS'` 就会**当轮立刻进发布队列**，3 天反悔窗口
    #      **被完全绕过**，而且**不报错**。这条过滤就是让闸门②真的生效。
    #    ★ `IS NULL` / `= ''` 这一支是**给第一阶段（qB 本地抓轨）用的**：
    #      那批行的 `publish_after` 本来就是 NULL，语义是「没有反悔窗口、照旧发」
    #      ⇒ 加这条**不改变第一阶段行为**（可用现存 SUCCESS 行回归验证）。
    #    ★ 时区：写入侧（`opencd_ingest`）刻意用 **UTC** 生成 `publish_after`，
    #      与 `datetime('now')` **同源同时区**，所以这里能直接字符串比较。
    #      ⚠️ 本机 `TZ=UTC` 而生产容器是 CST(+8) ⇒ 这个坑**本机测不出来**，
    #         只能靠 `opencd_ingest --selftest` 的「恰好 3 天」判据钉住。
    "AND (publish_after IS NULL OR publish_after = '' "
    "     OR publish_after <= datetime('now'))"
)


def _rows_to_dicts(cur):
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _load_mapped(task):
    """
    把 `task["mapped_json"]` 解回 dict（`opencd_map.map_all()` 的产出快照）。

    ★ **解析失败就抛出，不静默返回空 dict**：
      否则「JSON 坏了」会长得和「这个字段本来就没有」一模一样 ⇒ 渲染层会
      退回 `detect_*()` 从标题猜，页面看着"有值"而值是猜的
      （正是 `ERR-META-01` 那个 bug 的形状：判据只覆盖了一部分，于是"全绿"是假的）。
    ★ 没有 `mapped_json`（第一阶段的行）⇒ 返回 `{}`，那是**正常的**，
      调用方据此走原有的 `detect_*` 兜底，不是错误。
    """
    raw = (task or {}).get("mapped_json")
    if not raw or not str(raw).strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"mapped_json 不是合法 JSON（info_hash={task.get('info_hash')!r}）: {e}"
        ) from e
    if not isinstance(data, dict):
        raise ValueError(
            f"mapped_json 必须是 JSON 对象，实得 {type(data).__name__}"
            f"（info_hash={task.get('info_hash')!r}）"
        )
    return data


def get_pending_publish():
    """待发布：本地处理成功、站点上还没有、**且过了闸门②的反悔窗口**。"""
    conn = connect()
    cur = conn.cursor()
    cur.execute(f"SELECT {TASK_FIELDS} FROM tasks WHERE {PENDING_PUBLISH_WHERE}")
    rows = _rows_to_dicts(cur)
    conn.close()
    return rows


# ── 「本地比站点新」的判据 ────────────────────────────────────────────────
# get_published(only_stale=True) 的**筛选**和 count_stale() 的**计数**必须
# 用同一个字符串。★ 别在两处各写一份：
#
# daemon 的 edit 调度探针就是拿 count_stale() 判断"还有没有活"。一旦它和
# 实际筛选漂移（比如 get_published 这边加了条件、count_stale 那边没跟），
# 就会变成「探针说有活 → 60s 后重跑 → update_metadata 一条都选不出来 →
# 探针还说有活」的高频空转，而且**不会报任何错**，只表现为站点被反复敲。
STALE_CONDITION = "(site_synced_at IS NULL OR updated_at > site_synced_at)"


def _published_where(torrent_id=None, only_missing_id=False,
                     only_with_cover=False, only_stale=False):
    """
    get_published() 与 count_stale() 共用的 WHERE 构造，返回 (where, params)。

    这两个函数**必须选出同一个集合** —— 抽到这里就是为了让它们没法跑偏。
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
        where.append(STALE_CONDITION)

    return where, params


def get_published(only_missing_id=False, torrent_id=None, only_with_cover=False,
                  only_stale=False):
    """
    已在站点上存在的任务。

    only_stale=True 时只返回"本地内容比站点新"的记录：从没同步过
    （site_synced_at IS NULL），或同步之后本地又变过（updated_at > site_synced_at）。
    定时任务必须用这个，否则每轮都要把 250+ 个页面全部拉一遍 edit.php 做对比，
    纯属拿站点带宽换空转。
    """
    where, params = _published_where(torrent_id=torrent_id,
                                     only_missing_id=only_missing_id,
                                     only_with_cover=only_with_cover,
                                     only_stale=only_stale)

    conn = connect()
    cur = conn.cursor()
    cur.execute(
        f"SELECT {TASK_FIELDS} FROM tasks WHERE {' AND '.join(where)} ORDER BY updated_at",
        params,
    )
    rows = _rows_to_dicts(cur)
    conn.close()
    return rows


def count_stale():
    """
    现在有多少条会被 `--stale-only` 选中。给 daemon 的 edit 调度探针用。

    只数不取：待办里的记录带 log_content / logs_json 这些大文本，而调度 tick
    每 30s 就要问一次，把 13 列全拉一遍纯属浪费 —— 这里只回一个整数。

    口径与 get_published(only_stale=True) 严格一致（共用 _published_where）。
    daemon 的 edit 命令不带 --only-cover、也不指定 torrent_id，所以这里用
    默认参数选出的就是同一个集合。
    """
    where, params = _published_where(only_stale=True)

    conn = connect()
    n = conn.execute(
        f"SELECT count(*) FROM tasks WHERE {' AND '.join(where)}", params
    ).fetchone()[0]
    conn.close()
    return n


def count_pending_publish():
    """
    现在有多少条会进发布队列。给 daemon 探针 + `opencd_ingest --selftest` 用。

    只数不取（同 `count_stale()` 的理由：待发布里有 `log_content`/`mapped_json`
    这些大文本，而调度 tick 会频繁问）。

    ★★ 口径与 `get_pending_publish()` **严格同源**（共用 `PENDING_PUBLISH_WHERE`）
      —— 两处各写一份必然漂移，而漂移**不报错**，只表现为反复空转。
    """
    conn = connect()
    n = conn.execute(
        f"SELECT count(*) FROM tasks WHERE {PENDING_PUBLISH_WHERE}"
    ).fetchone()[0]
    conn.close()
    return n


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


# ==================== 配额账本（读写）====================
#
# 三个函数都是**无状态**的：每次自己开连接、自己关。理由同 save_task ——
# 调用方是短命进程（每个 crawl 轮一个新进程），持有长连接只会留下没关的 WAL。

def ledger_get(day_key, kind=LEDGER_KIND_REQUEST):
    """
    取某天某种用途的**已用**计数。★ **没有记录返回 0，不是 None**。

    ★ 这个"返 0 而不是 None"是刻意的：调用方（`opencd_rate.RateGate` 构造）会拿它
      直接当 `_count_day` 用。若返回 None，`_count_day` 就成了 None ⇒
      `None >= max_per_day` 在 py3 里**抛 TypeError**（不是静默放假，算走运），
      但更糟的是有的调用点写成 `used or 0`，那就把"查不到"和"真的是 0"混成一坨
      （"缺值不留默认值"是 `config.py` 的纪律，但**这里是"确实没有"= 0** 的正当语义）。
    """
    conn = connect()
    row = conn.execute(
        f"SELECT used FROM {LEDGER_TABLE} WHERE day_key = ? AND kind = ?",
        (str(day_key), str(kind)),
    ).fetchone()
    conn.close()
    return int(row[0]) if row else 0


def ledger_add(day_key, delta=1, kind=LEDGER_KIND_REQUEST):
    """
    累加并返回**累加后**的值。

    ★★ 必须是**单条原子 UPSERT**，不能"先 SELECT 再 UPDATE"：
      后者中间留着窗口 —— 两个写者都读到 49、都写回 50，**等于白记一次**。
      而配额少记一次 = 超出用户定的上限，正是这条流水线最不能出的错。
      故用 `ON CONFLICT(day_key, kind) DO UPDATE SET used = used + excluded.used`，
      **与 `save_task` 同一套写法**（那里也是靠 ON CONFLICT 保证原子）。
    ★ 用 `used = used + excluded.used` 而不是 `= excluded.used` 是全部关键：
      后者是**覆盖**，第一次写 1 之后再写就永远 1。
    """
    delta = int(delta)
    conn = connect()
    conn.execute(
        f"INSERT INTO {LEDGER_TABLE} (day_key, kind, used) VALUES (?, ?, ?) "
        f"ON CONFLICT(day_key, kind) DO UPDATE SET "
        f"used = used + excluded.used, updated_at = CURRENT_TIMESTAMP",
        (str(day_key), str(kind), delta),
    )
    conn.commit()
    row = conn.execute(
        f"SELECT used FROM {LEDGER_TABLE} WHERE day_key = ? AND kind = ?",
        (str(day_key), str(kind)),
    ).fetchone()
    conn.close()
    return int(row[0]) if row else 0


def ledger_prune(keep_days=7, kind=None):
    """
    删掉**旧日期**的账本行，保留最近 `keep_days` 天（含今天）。返回删除行数。

    ★ 为什么必须存在：账本一天一行，不清理就是无限增长 —— 虽然**极慢**
      （一天一行、一年 365 行），但"永远不会自己停的增长"就是泄漏，
      而这里清它的代价是零（旧日期的计数已经没人读了）。
    ★★ **判据必须按字符串比较，不能按 timestamp**：`day_key` 是
      `'YYYY-MM-DD'`，字典序 == 时间序（定长、零填充），所以
      `day_key < '2026-09-15'` 就是"早于那天"，不需要任何日期解析 ——
      一旦这里改成 `date(day_key) < ...` 就把"纯字符串"这个安全属性丢了。
    ★ `kind=None` 表示所有 kind 一起清：调用方（crawl）不关心 kind，
      漏传 kind 只清一半才是坑。
    """
    keep = max(1, int(keep_days))
    conn = connect()
    cutoff = conn.execute(
        "SELECT date('now', 'localtime', ?)", (f"-{keep - 1} days",)
    ).fetchone()[0]
    sql = f"DELETE FROM {LEDGER_TABLE} WHERE day_key < ?"
    params = [str(cutoff)]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(str(kind))
    cur = conn.execute(sql, params)
    n = cur.rowcount
    conn.commit()
    conn.close()
    return int(n or 0)
