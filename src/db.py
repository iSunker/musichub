"""数据库持久化与查询层。

统一管理 SQLite tasks 表、opencd_quota 配额表与 site_publish 多站点状态表。
提供初始化与迁移（init_db）、待发布队列过滤以及各站点发布状态查询接口。
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

    # 原始来源页面 ID
    ("opencd_tid", "TEXT"),
    # 做种天数（保留供历史兼容）
    ("qb_seed_days", "REAL"),
    # 原始发布日期时间戳（东八区字符串）
    ("opencd_added_at", "TIMESTAMP"),
    ("fetched_at", "TIMESTAMP"),
    ("publish_after", "TIMESTAMP"),
    # 映射未匹配的标签 JSON
    ("unmapped_json", "TEXT"),
    # 艺术家与发行年份
    ("artist", "TEXT"),
    ("year", "TEXT"),
    # 元数据映射快照 JSON（包含 tags/medium/codec 等渲染参数）
    ("mapped_json", "TEXT"),
    # 转载规则：ALLOW / NEED_CONFIRM / DENY
    ("transfer_rule", "TEXT"),
    # 详情描述正文（已剥离图片）
    ("opencd_descr", "TEXT"),
    # 音轨类型：整轨 / 分轨
    ("track_type", "TEXT"),
]


# 配额账本表：跨进程持久化记录每日站点请求数与入库种子数
LEDGER_TABLE = "opencd_quota"
LEDGER_KIND_REQUEST = "request"          # 请求量纲（crawl RateGate 限制使用）
LEDGER_KIND_SEED = "seed"                # 种子量纲（入库成功计数）

LEDGER_COLUMNS = [
    ("day_key", "TEXT NOT NULL"),        # 本地日期字符串 'YYYY-MM-DD'
    ("kind", "TEXT NOT NULL"),
    ("used", "INTEGER NOT NULL DEFAULT 0"),
    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
]
LEDGER_PK = "PRIMARY KEY (day_key, kind)"


# 多站点发布关系表：维护 (info_hash, site) 二元组状态
SITE_PUBLISH_TABLE = "site_publish"

SITE_MUSOPIA = "musopia"
SITE_CRABPT = "crabpt"
SITE_PTER = "pter"
SITE_XINGWAN = "xingwan"
SITE_HDKYLIN = "hdkylin"

SITE_PUBLISH_COLUMNS = [
    ("info_hash", "TEXT NOT NULL"),
    ("site", "TEXT NOT NULL"),          # SITE_MUSOPIA / SITE_CRABPT / SITE_PTER / SITE_XINGWAN
    ("torrent_id", "TEXT"),             # 对应站点的种子详情页 ID
    ("status", "TEXT"),                 # 'EXISTS' / 'FAILED' 等
    ("cover_url", "TEXT"),              # 各站点独立的封面 URL
    ("synced_at", "TIMESTAMP"),
    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
]
SITE_PUBLISH_PK = "PRIMARY KEY (info_hash, site)"


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

    # ---- 多站点发布身份表（见上面 SITE_PUBLISH_* 那一节的理由）----
    _sp_cols = ",\n            ".join(f"{n} {d}" for n, d in SITE_PUBLISH_COLUMNS)
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {SITE_PUBLISH_TABLE} (\n"
        f"            {_sp_cols},\n            {SITE_PUBLISH_PK}\n        )"
    )
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_site_publish_site "
        f"ON {SITE_PUBLISH_TABLE}(site, status)"
    )

    # 幂等回填：将历史 tasks 表中 site_status='EXISTS' 且已具有 ID 的数据同步至 site_publish 表
    cur.execute(
        f"INSERT INTO {SITE_PUBLISH_TABLE} "
        f"(info_hash, site, torrent_id, status, cover_url, synced_at) "
        f"SELECT t.info_hash, ?, t.site_torrent_id, t.site_status, "
        f"       t.cover_url, t.site_synced_at "
        f"  FROM tasks t "
        f" WHERE t.site_status = 'EXISTS' "
        f"   AND t.site_torrent_id IS NOT NULL AND t.site_torrent_id != '' "
        f"   AND NOT EXISTS (SELECT 1 FROM {SITE_PUBLISH_TABLE} p "
        f"                    WHERE p.info_hash = t.info_hash AND p.site = ?)",
        (SITE_MUSOPIA, SITE_MUSOPIA),
    )
    if cur.rowcount:
        print(f"🔧 {SITE_PUBLISH_TABLE}：回填 musopia 行 {cur.rowcount} 条")

    conn.commit()
    conn.close()

    if added:
        print(f"🔧 数据库自动迁移，新增列: {', '.join(added)}")


# ==================== 查询 ====================

TASK_FIELDS = (
    "info_hash, torrent_name, formatted_title, small_descr, cover_url, "
    "tracklist, log_content, log_filename, logs_json, torrent_file, content_path, "
    "site_status, site_torrent_id, "
    "opencd_tid, artist, year, unmapped_json, mapped_json, publish_after, "
    "opencd_descr, "
    "track_type"
)

# 待发布记录查询条件：
#   1. status 为 SUCCESS 且未在目标站发布（site_status IS NULL 或 != 'EXISTS'）；
#   2. 发布窗口（闸门②）：记录了原始发布时间 opencd_added_at，且距今已满 7 天；
#   3. 元数据完备（闸门③）：对于来源为皇后的记录（opencd_tid 非空），必须已抓取 mapped_json；
#   4. 转载规则（闸门④）：transfer_rule 必须显式为 'ALLOW'。
PENDING_PUBLISH_WHERE = (
    "status = 'SUCCESS' AND (site_status IS NULL OR site_status != 'EXISTS') "
    "AND (opencd_added_at IS NOT NULL AND opencd_added_at != '' "
    "     AND opencd_added_at <= datetime('now', '-7 days'))"
    "AND (opencd_tid IS NULL OR opencd_tid = '' "
    "     OR (mapped_json IS NOT NULL AND mapped_json != ''))"
    "AND transfer_rule = 'ALLOW'"
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
      （正是  那个 bug 的形状：判据只覆盖了一部分，于是"全绿"是假的）。
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


    # 提取 mapped_json 中的对应字段
#
#   原先它叫 `publish.merge_mapped()`。下移的理由是**新增了第二个发布站**：
#   蟹黄堡的适配层（`crabpt_site.py`）也需要这一步，而它**不能 import publish**
#   （publish 会 import 它，成环）⇒ 各写一份就又是两个口径。
#   ★ 而这一步的 docstring 原话就说了"漏了不报错" —— 正因如此，**更不该有两份**。
#
    # 提取 small_descr 简介字段
#   原清单只有上面四个键，于是**副标题**这一格漏在外面：
#     · `descr.build_form_fields()` 皇后路读 `opencd_small_descr(task["small_descr"])`
#       = **读列**；而 `tasks.small_descr` 列长期是「全 N 曲」（补页面那步只写了
#       `mapped_json` 没回写列）⇒ 每次 publish/edit 都把「全 N 曲」推上站。
#     实测（本机模拟一条：列='全 15 曲'、`mapped_json.small_descr`='專輯 | VP Records / VPCD 1793'）：
#       `build_form_fields()` 提交的 `small_descr` == `'全 15 曲'`  ★ 错。
#     ★ 症状的误导性：皇后原文**一直躺在库里**，只是从没被读过 ——
#
# ★ 为什么加进这张表就够（而不是去回写列）：本表的语义是**"渲染输入优先取证据快照"**，
#   `small_descr` 与 `tags`/`medium_sel` 等完全同构（都由 `map_all()` 透传、
#   都存在于 `mapped_json`）⇒ 它本来就**该**在这张表里，缺席是**遗漏**不是设计。
MAPPED_TOP_KEYS = ("tags", "medium_sel", "audiocodec_sel", "team_sel",
                   # ★★ 副标题：`mapped.small_descr` = 皇后 `<div class="smalltitle">` 原文。
    # 提取 small_descr 简介字段
                   "small_descr",
    # 提取 descr 详情字段
                   #    （解析层已剥图，见 `opencd_parse._descr_text_only`）。
                   #    ★ 与 `small_descr` **完全同构**：都由 `map_all()` 透传、
                   #      都存在于 `mapped_json` ⇒ 缺席同样是**遗漏**不是设计。
                   #    ★ 落位：`descr.build_descr()` 里作为正文块（见那里的分流）。
                   "descr",
    # 提取 tracklist 曲目列表字段
                   #    `<div id="divtracklist">` 的**完整**列表（**带时长、带 feat.**）。
                   #
                   #    ★★ 为什么必须加（这是用户明确要求的"曲目列表也用皇后的"）：
                   #       `tasks.tracklist` 这个**列**是"残废版"。实测 tid=199947：
                   #         列   : "Can't Hold Me"                 ← 时长、feat. 全丢
                   #         快照 : "1. Can't Hold Me (02:37) …"     ← 完整
                   #       ⇒ 站上曲目列表一直少了时长与客串信息。
                   #
                   #    ★ 为什么列会是残废版（不是随手猜的）：两个来源**本就不是一份**
                   #       · `tracklist` 列 —— 由**本地抓轨路**（`run.py` → `assets.
                   #         read_tracklist`）或早期写入填的，只有曲名；
                   #       · `mapped.tracklist` —— 由**皇后路**（`opencd_parse.
                   #         _extract_tracklist`）从页面读的，`<br/>` 分行、原样保留。
                   #       两条路写的是**同一个列名** ⇒ 皇后行的列被谁后写谁说了算。
                   #         与 `small_descr`/`descr` **完全同构** ⇒ 同样该走这张表。
                   "tracklist")


# ★★ 哪些键的**空快照**是"权威结论"（而不是"没抓到"）—— 见 `merge_mapped` 的长注释。
#
# ★ 判据（怎么判断一个键该不该进这张表）：问一句 ——
#   **"皇后页面里这个字段是空的"这件事，快照能不能确定地告诉我？**
#     · 能（该字段是页面上的**一个明确位置**，抓到空就是空）⇒ 进表。
#     · 不能（该字段由解析层**多处拼**出来，空更可能是"这次没覆盖到"）⇒ 不进表。
#   ⇒ `small_descr` / `descr` 是**一个 div**（`<div class="smalltitle">` /
#     `<div class="divdescr">`）⇒ 能确定 ⇒ 进表。
#   ⇒ `tags` / `medium_sel` / `audiocodec_sel` / `team_sel` / `tracklist` 是
#     **映射/多行解析**的产物，空更可能是"映射表里没这一项" ⇒ **不进表**。
#
# ★ 只对**皇后行**（`opencd_tid` 有值）生效；非皇后行的 `mapped` 恒为 `{}`。
EMPTY_SNAPSHOT_IS_AUTHORITATIVE = ("small_descr", "descr")


def merge_mapped(task):
    """
    把 `task["mapped_json"]`（`opencd_map.map_all()` 的**证据快照**）解回**顶层键**。

    ★★ 为什么必须有这一步（这是最容易漏、且**漏了不报错**的一环）：
      `descr.build_form_fields()` 读的是 `task["tags"] / ["medium_sel"] /
      ["audiocodec_sel"] / ["team_sel"]` 这些**顶层键**，而
      `db.get_pending_publish()` 只 SELECT `db.TASK_FIELDS`（没有这四列 ——
      它们存在 `mapped_json` 里）。
      ⇒ 不合并 ⇒ 那四个键是 `None` ⇒ `descr` 退回 `detect_medium()` /
        `detect_tags()` **从标题猜** ⇒ 页面看着"有值"，**值是猜的**。
        这正是  那个 bug 的形状（静默、不报错）。

    ★ 用**快照**而不是当场重算：`TAG_NAME_MAP` / `MEDIUM` 会变（44 项标签表刚改过、
      WAV 编号未定）⇒ 发布要用的就是三天前抓页面时做的那个决定。
    ★ 第一阶段（qB 本地抓轨）的行没有 `mapped_json` ⇒ `_load_mapped` 返回 `{}`，
      这里**什么都不改**，行为与改动前完全一致。
    ★ 解析失败会**抛出**（`_load_mapped` 的契约）—— 响亮地坏，不静默当空。

    ★★ 守卫的**确切语义**（2026-09-27 更正，别按旧文档读）：
      · 快照**有值**（`mapped[key] not in (None, "")`）⇒ 取快照，**盖过列**。
      · 快照**是空**：分两种键，**语义相反**，必须分开处理 —— 见下面的
        `EMPTY_SNAPSHOT_IS_AUTHORITATIVE`。

    ★★★ 为什么旧守卫是**错的**：
      旧实现一律是 `if key in mapped and mapped[key] not in (None, "")` ⇒
      **快照为空时保留列值**。它的理由是"要区分'皇后本来就没写'与'列里是错值'"。
      ⇒ 但那个理由**不成立**，因为：对**皇后行**（`opencd_tid` 有值），
        `mapped_json` 就是**那次抓页面的权威快照** ⇒ 快照里空 = **皇后当时确实没写**。
        "列里是错值"**正是要修的那件事**，不是要保护的对象。
      ⇒ 旧守卫的实际后果：列里若存着「全 15 曲」这种错值，而皇后**真的没写**副标题，
        `merge_mapped` 会**保留错值**并把它提交上站 —— 也就是把"错值"当成"皇后的意思"
        推出去。那正是  的形状（空来源不许印成成功）。
      ⇒ 判据：**"快照为空"对皇后行是一个**结论**（皇后没写），不是一个"不知道"。**
        把它当成"不知道"去退回列值，等于用一个错值去填一个已知的空。

    ★ `EMPTY_SNAPSHOT_IS_AUTHORITATIVE` 只对**这两列**成立，且只对**皇后行**成立：
      · `small_descr` —— 皇后 `<div class="smalltitle">` 没写 ⇒ 就该空（实测 4 条：
        15974 齐秦 / 17264 黄清元 / 17265 醉民乐 / 17294 爱是我们共同的声音，
        三者列/快照/站点**同为 `''`** ⇒ 正确留空）。
        ★ 但若列里是「全 N 曲」（`scan` 旧路径的产物），**必须被空快照盖掉**。
      · `descr`（简介正文）—— 同上：皇后的 `divdescr` 没正文 ⇒ 该空。
        ★ 注意 `descr` 的**渲染**有 `task.get("descr") or task.get("opencd_descr") or ""`
          兜底（`descr.py:630`），所以这里盖成 `''` 是**有意的**：它表达"快照说没有"。
      ⇒ 别的键（`tags`/`medium_sel`/`audiocodec_sel`/`team_sel`/`tracklist`）
        **不适用**：那些是"快照没抓到"更可能等于"解析没覆盖到"，空值不该盖掉列
        （旧的保守行为对它们仍然是对的）。⇒ 只白名单这两列，不做通用化。

    ★ 对**非皇后行**（`opencd_tid` 空、qB 本地抓轨路）`mapped` 恒为 `{}`
      ⇒ 下面的 `key in mapped` 为假 ⇒ **一律不动**，行为与改动前完全一致。
    """
    mapped = _load_mapped(task)
    is_opencd = bool((task or {}).get("opencd_tid"))
    for key in MAPPED_TOP_KEYS:
        if key not in mapped:
            continue
        val = mapped[key]
        if val not in (None, ""):
            task[key] = val
        elif is_opencd and key in EMPTY_SNAPSHOT_IS_AUTHORITATIVE:
            # ★ 皇后行 + 权威空 ⇒ **盖成空**（不是"保留列值"）。
            task[key] = val if val is not None else ""
    return task


# ── 「重扫不许覆盖」的 status 集合（★ 唯一来源，别在别处再写一份）──────────
#
#    种子仍在 qB 里** ⇒ `run.py` 那次**无条件**的 `save_task(..., status="SUCCESS")`
#    会把 `publish.py` 写下的 `status='PUBLISHED'` **冲回 `SUCCESS`**。
#    实测：08:56 发布后是 `PUBLISHED`（2 条），09:2x 再看已是 `SUCCESS`。
#
#    `save_task()` 的 upsert 只更新**显式传入**的列，所以 `site_status` /
#    `site_torrent_id` 没被冲掉 ⇒ **当时没造成重复发布**。但"已发布"这个
#    **事实**丢了 ⇒ 任何靠 `status='PUBLISHED'` 判事的逻辑都静默失效。
#
# ★ 这三类值都是**别的阶段下的结论**，扫描阶段没有资格推翻它们：
#     · `PUBLISHED`      —— `publish.py` 写的"已在站上"
#     · `NO_TORRENT_FILE`—— `crawl.py` 手工路写的"没有 .torrent"（重建会改
#                           passkey ⇒ 改 infohash，所以是**刻意**不重建）
#     · `FAILED`         —— 失败结论；重扫想推翻它得**显式**说（`--recheck`），
#                           不能靠"顺手重扫一遍"把它抹掉
#
# ★ 注意 `NO_LOG_FILE` **不在**这个集合里：`run.py` 对它有**专门**的短路分支
#   （`status == "NO_LOG_FILE"` 直接 continue，压根走不到 save_task），
#   而且 `--recheck` 的语义就是"无视这个缓存重做一遍"。
#   ⇒ 若把 `NO_LOG_FILE` 也塞进来，`--recheck` 就废了（那正是它的用途）。
PROTECTED_STATUSES = ("PUBLISHED", "NO_TORRENT_FILE", "FAILED")


def get_status(info_hash):
    """读一行的 `status`（**只读**，给"写之前先看看"用）。行不存在 ⇒ `None`。"""
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT status FROM tasks WHERE info_hash = ?", (info_hash,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_content_path(info_hash):
    """读一行的 `content_path`（只读）。行不存在或为空 ⇒ None。"""
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT content_path FROM tasks WHERE info_hash = ?", (info_hash,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


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


# 与 `PENDING_PUBLISH_WHERE` 里那句 `AND transfer_rule = 'ALLOW'` **同一判据**，
# 只是**服务另一条路**：那条路的消费者是 `update_metadata.py`（页面**编辑**），
# 不是 `publish.py`（新种子**发布**）。
#
#   闸门④ 原先**只装在发布队列上**。而 `edit` 走的是 `get_published()`，
#   它的 WHERE 里只有 `site_status='EXISTS'` + `site_torrent_id` 非空 + stale
#   —— **完全不知道 `transfer_rule` 这回事**。
#   ⇒ 结果：一条 `DENY`（禁止轉載）的行，只要**已在站上**且**本地更新过**，
#     `edit` 就会照改不误（实测 15972：16 条同批 stale 里 15 条 ALLOW、
#     1 条 DENY，那 1 条被 `edit` 按本地内容重写了站点页面）。
#
# ★★ 为什么不再走一遍"发布队列那道闸门"（两处各写一份 SQL 的诱惑）：
#   两条路**选的集合本来就不同** —— 发布队列要 `site_status != 'EXISTS'`（还没发），
#   edit 要 `site_status = 'EXISTS'`（已在站上）。**两者互斥**，不可能共用一条 WHERE。
#   但"能不能动这条 DENY 行"**是同一个判断** ⇒ 抽成常量，两边都引它
#
# ★ 为什么单位是「这个值才放行」而不是「排除 DENY」：
#   与闸门④ 同一套理由（见 `PENDING_PUBLISH_WHERE` 里那段长注释）：
#   `transfer_rule` 空的语义是「**不知道**能不能转」，**不是**「允许」。
#   ⇒ 白名单（`= 'ALLOW'`），不是黑名单（`!= 'DENY'`）。
#   `NEED_CONFIRM`（拿不准）同样拦 —— 拿不准就不动。
#
# ⚠️ 代价（与发布侧同一条，这里再说一次因为它现在也影响 edit）：
#   第一阶段（本地抓轨）行若确实有权改，必须先**显式**标 `ALLOW`。
#   这是刻意的：授权该是需要动作的那一方。
EDIT_RULE_GUARD = "transfer_rule = 'ALLOW'"


def _published_where(torrent_id=None, only_missing_id=False,
                     only_with_cover=False, only_stale=False,
                     require_allow=False):
    """
    get_published() 与 count_stale() 共用的 WHERE 构造，返回 (where, params)。

    这两个函数**必须选出同一个集合** —— 抽到这里就是为了让它们没法跑偏。

    ★★★ `require_allow`（2026-09-27 加）：是否要求 `transfer_rule = 'ALLOW'`。
      **edit 路必须传 True**（否则就是上面那个洞）；**其它调用方传 False**：
        · `sync_site.py`       —— 它只是去站点**读**现状回填 `site_torrent_id`，
                                  对 DENY 行做这件事**不违反**"禁止轉載"（没有写任何东西）
        · `check_db.py`        —— 诊断（数还缺哪些 id），同上
        · `--list-fields`      —— 只为取一个**样本**渲染字段名，不提交
        · `gen_counts.py`      —— 只统计
      ⇒ 判据：**问"这条路会不会把内容写到站点上"** —— 会 ⇒ 必须 require_allow；
        不会 ⇒ 不要（多拦只会让诊断/对账看不到真实全貌）。
      ★ 这也解释了为什么**不能**把 `require_allow` 写死成 True：那会让
        `sync_site` 漏掉 DENY 行的 id 回填 ⇒ 那些行的 `site_torrent_id` 永远空
        ⇒ 它们既进不了 edit、又在对账里"凭空消失"。
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

    if require_allow:
        where.append(EDIT_RULE_GUARD)

    if only_with_cover:
        where.append("cover_url IS NOT NULL AND cover_url != ''")

    if only_stale:
        where.append(STALE_CONDITION)

    return where, params


def get_published(only_missing_id=False, torrent_id=None, only_with_cover=False,
                  only_stale=False, require_allow=False):
    """
    已在站点上存在的任务。

    only_stale=True 时只返回"本地内容比站点新"的记录：从没同步过
    （site_synced_at IS NULL），或同步之后本地又变过（updated_at > site_synced_at）。
    定时任务必须用这个，否则每轮都要把 250+ 个页面全部拉一遍 edit.php 做对比，
    纯属拿站点带宽换空转。

    ★★★ `require_allow`（2026-09-27 加）：见 `_published_where` 的长注释。
      **凡是会往站点写内容的路，必须传 True**（`update_metadata.py` 的 edit 路）；
      只读的路（sync_site / check_db / --list-fields / 统计）传 False。
      ★ 默认值是 `False` —— 因为本函数**也被只读用途调**，而"默认拦住"会让
        `sync_site` 静默漏掉一批 id 回填。⇒ **是否拦由调用方声明**，
        而每个调用方都要按"我这条路会不会写站点"来选（判据写在
        `_published_where` 里，别只写在这里）。
    """
    where, params = _published_where(torrent_id=torrent_id,
                                     only_missing_id=only_missing_id,
                                     only_with_cover=only_with_cover,
                                     only_stale=only_stale,
                                     require_allow=require_allow)

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

    ★★★ `require_allow=True`（2026-09-27 加，**这是修洞的另一半**）：
      这个函数**是 edit 阶段的待办探针**（`daemon.py` 的 `"pending": db.count_stale`），
      所以它**必须**带上闸门④ —— 否则会出现最坏的一种错配：
        探针**算进了** DENY 行（说有活）→ daemon 按 60s 冷却重跑 edit →
        edit **选不出来**（它带了 guard）→ 探针**还说有活**
      ⇒ 就是本文件上面警告过的"高频空转，而且不报任何错，只表现为站点被反复敲"。
      ★ 判据：**探针和它探的那条路必须共用同一个 WHERE** —— 这正是本函数存在的理由，
        补闸门时**两边都得改**，只改一边比不改更糟。
      ⚠️ `_published_where` 的 `require_allow` 默认是 False，这里**显式传 True**。
        默认保持 False 是为了让只读调用方（sync_site/check_db）不受影响，
        而本函数的**唯一消费者就是 edit 探针** ⇒ 这里没有"只读用途"要照顾。
    """
    where, params = _published_where(only_stale=True, require_allow=True)

    conn = connect()
    n = conn.execute(
        f"SELECT count(*) FROM tasks WHERE {' AND '.join(where)}", params
    ).fetchone()[0]
    conn.close()
    return n


def queen_published_rows():
    """★ C' 第 4 步的判据取样：**皇后路 + 已上站**这批行（**只读**）。

    返回完整 dict 列表（含 `formatted_title`），供"拍基线 / 比对"用。

    ★ 口径**唯一真源** = `QUEEN_PUBLISHED_WHERE`（本函数**不手写** WHERE）。
      逐字同 `count_stale()` 的理由：两处各写一份必然漂移，而漂移**不报错**，
      只给出一个看着合理、其实不同口径的错数字。

    ★ 为什么连 `updated_at` 一起取：C' 第 4 步的判据是"**标题没被动**"，
      而 `updated_at` 只能当**旁证**（`scan` 写别的列也会刷它）——
      ⇒ 二者都要印出来，**不许**拿 `updated_at` 代替"标题的值"。
    """
    conn = connect()
    rows = _rows_to_dicts(conn.execute(
        f"SELECT info_hash, site_torrent_id, formatted_title, torrent_name,"
        f"       mapped_json, site_synced_at, updated_at, transfer_rule"
        f"  FROM tasks WHERE {QUEEN_PUBLISHED_WHERE}"
        f" ORDER BY info_hash"))
    conn.close()
    return rows


def queen_published_gap():
    """★ `EXISTS` ⇒ 皇后路 **是不是**推理 —— 用一条**可执行**判据回答。

    ★ 为什么必须有：
      `CROSS_SITE_DEDUP_WHERE` 上面的注释曾写"所以 `run_crabpt()` 里另有一条
      **可执行**的断言，见那里的 `rule_gaps`（`crabpt_rule_gaps()`）"。
      **实测那两个函数都不存在** ⇒ 那条断言**从没被写出来**，
      "`EXISTS` ⇒ ALLOW"至今只是一句**推理**。⇒ 这里不留同样的洞。

    返回 `(exists_total, queen_total, exists_without_tid)`。
      `exists_without_tid > 0` ⇒ 已上站的行里**混着抓轨路** ⇒
      第 4 步的判据**必须分栏报**（抓轨路本来就会被 `scan` 写），
      不许把两栏合并成一个数。
      `== 0` ⇒ 才可以简化成一条判据。

    ★ 三个数**并排返回**，不是只返回差 —— 差是"算出来的"，
      三个原始数才是"读出来的"。
    """
    conn = connect()
    exists_total = conn.execute(
        "SELECT count(*) FROM tasks WHERE site_status = 'EXISTS'").fetchone()[0]
    queen_total = conn.execute(
        f"SELECT count(*) FROM tasks WHERE {QUEEN_PUBLISHED_WHERE}").fetchone()[0]
    conn.close()
    return exists_total, queen_total, exists_total - queen_total


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


def publish_funnel():
    """
    发布队列**逐级拆开**的行数 —— 回答"为什么队列是 0/为什么这条没进队列"。

    ★★ 为什么必须有这个函数（2026-09-23 的实测教训）：
      队列变成 0 时，`publish.py --dry-run` 只会说"没有需要发布的任务" ——
      它**不告诉你卡在哪一级**。于是 diagnose 只能靠**手写 SQL**，
      而手写必踩 ：口径与 `PENDING_PUBLISH_WHERE` 漂移后
      **不报错**，只给出一个看着合理、其实不同口径的错数字。
      今晚就是这么被绊住的（现场拼 `python -c` 撞终端换行，拼一次断一次）。
      ⇒ 把"逐级数"做成**一个函数**，口径**直接从 `PENDING_PUBLISH_WHERE` 派生**，
        永久消除"手写一份"这条路。

    ★★ 每一级都是**累加**的（`WHERE` = 前面所有级的 AND 叠加）——
      这样相邻两级的**差**就是"被这一级拦掉多少"，而那正是要看的。
      ⚠️ **别把各级单独的数相减来推结论**。
         这里**同口径**（都在累加链上），所以**差是合法的** —— 但只在同一条链内。

    ★ 返回 dict，键名即判据；`queue` = 最终 `PENDING_PUBLISH_WHERE` 的行数，
      与 `count_pending_publish()` **必然相等**（同一个常量拼出来的）。
    """
    # ★ 逐级条件：**按闸门顺序**列出，且**每一级都包含前面所有级**。
    #   直接从 PENDING_PUBLISH_WHERE 里数会得到 0，而我要的是"每加一级剩多少"。
    levels = [
        ("g1_status_success",      "status = 'SUCCESS'"),
        ("g2b_site_not_exists",
         "status = 'SUCCESS' AND (site_status IS NULL OR site_status != 'EXISTS')"),
        # 闸门②：皇子发布日期 + 7 天（★ 与 PENDING_PUBLISH_WHERE 里那句**逐字同源**）
        ("g2_added_7d",
         "status = 'SUCCESS' AND (site_status IS NULL OR site_status != 'EXISTS') "
         "AND (opencd_added_at IS NOT NULL AND opencd_added_at != '' "
         "     AND opencd_added_at <= datetime('now', '-7 days'))"),
        # 闸门③：皇后行必须抓过页面（tid 空 = 本地抓轨行，豁免）
        ("g3_mapped",
         "status = 'SUCCESS' AND (site_status IS NULL OR site_status != 'EXISTS') "
         "AND (opencd_added_at IS NOT NULL AND opencd_added_at != '' "
         "     AND opencd_added_at <= datetime('now', '-7 days')) "
         "AND (opencd_tid IS NULL OR opencd_tid = '' "
         "     OR (mapped_json IS NOT NULL AND mapped_json != ''))"),
        # 闸门④：transfer_rule 必须显式 ALLOW
        ("g4_allow",
         "status = 'SUCCESS' AND (site_status IS NULL OR site_status != 'EXISTS') "
         "AND (opencd_added_at IS NOT NULL AND opencd_added_at != '' "
         "     AND opencd_added_at <= datetime('now', '-7 days')) "
         "AND (opencd_tid IS NULL OR opencd_tid = '' "
         "     OR (mapped_json IS NOT NULL AND mapped_json != '')) "
         "AND transfer_rule = 'ALLOW'"),
    ]
    conn = connect()
    out = {}
    for name, where in levels:
        out[name] = conn.execute(
            f"SELECT count(*) FROM tasks WHERE {where}").fetchone()[0]
    # ★ 最终队列：**用真常量**（不是上面那条手抄的链）—— 这样若哪天
    #   `PENDING_PUBLISH_WHERE` 改了而上面 levels 没跟着改，下面 `queue`
    out["queue"] = conn.execute(
        f"SELECT count(*) FROM tasks WHERE {PENDING_PUBLISH_WHERE}").fetchone()[0]
    # 闸门② 的分母（有多少行**真的**有发布日期）—— 区分"日期缺"与"日期太新"
    out["added_notnull"] = conn.execute(
        "SELECT count(*) FROM tasks WHERE opencd_added_at IS NOT NULL "
        "AND opencd_added_at != ''").fetchone()[0]
    out["tid_notnull"] = conn.execute(
        "SELECT count(*) FROM tasks WHERE opencd_tid IS NOT NULL "
        "AND opencd_tid != ''").fetchone()[0]
    out["rule_allow"] = conn.execute(
        "SELECT count(*) FROM tasks WHERE transfer_rule = 'ALLOW'").fetchone()[0]
    out["rule_null"] = conn.execute(
        "SELECT count(*) FROM tasks WHERE transfer_rule IS NULL "
        "OR transfer_rule = ''").fetchone()[0]
    conn.close()
    return out


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


# ==================== 多站点发布身份（读写）====================
#
# 与 `ledger_*` 同规矩：**无状态**，每次自己开连接、自己关。
# 理由同上：调用方是短命进程，持有长连接只会留下没关的 WAL。

def site_publish_set(info_hash, site, torrent_id=None, status="EXISTS",
                     cover_url=None, synced=False):
    """写/更新**某一个站点**上这条种子的记录。返回 `(torrent_id, status, cover_url)`。

    ★★ 必须是**单条原子 UPSERT**，不能"先 SELECT 再 UPDATE"：
      后者中间留着窗口 —— 两个写者都读到旧值、都写回，**后写的赢**，
      而这里丢的是"这条发到哪个站了"这个**事实**（同 ：
      落库的值必须记它的来源判据；写丢了就等于没记）。
      故用 `ON CONFLICT(info_hash, site) DO UPDATE`，与 `save_task`/`ledger_add`
      **同一套写法**。

    ★★ `cover_url=None` 表示**不动这一格**（`COALESCE` 保住旧值）：
      绝不能"因为这次没传就把已存的封面抹成空" —— 那是本项目**真的踩过**的事故
      （`assets.upload_to_picgo` 的 docstring 记着：重做任务时把 `cover_url`
      覆盖成空，连锁导致站点页面封面被抹掉）。★ 要清空就显式传 `cover_url=""`。

    ★ `status` 默认 `'EXISTS'`；`synced=True` 时同时把 `synced_at` 刷成现在 ——
      ★ 但这个刷新是**判据相关**的：`STALE_CONDITION` 就是
      `site_synced_at IS NULL OR updated_at > site_synced_at`，
      它决定"这条要不要重推到站点"。⇒ **发布/回填时才刷**，别在只读路径上顺手刷
      。
    """
    conn = connect()
    conn.execute(
        f"INSERT INTO {SITE_PUBLISH_TABLE} "
        f"(info_hash, site, torrent_id, status, cover_url, synced_at) "
        f"VALUES (?, ?, ?, ?, ?, "
        f"        CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END) "
        f"ON CONFLICT(info_hash, site) DO UPDATE SET "
        # ★ torrent_id/status：**显式传了才更新**（None 表示"这次不声明这一格"）。
        #   与下面 cover_url 同一个道理 —— 别让"没传"变成"清空"。
        f"  torrent_id = COALESCE(excluded.torrent_id, {SITE_PUBLISH_TABLE}.torrent_id), "
        f"  status     = COALESCE(excluded.status,     {SITE_PUBLISH_TABLE}.status), "
        f"  cover_url  = COALESCE(excluded.cover_url,  {SITE_PUBLISH_TABLE}.cover_url), "
        f"  synced_at  = COALESCE(excluded.synced_at,  {SITE_PUBLISH_TABLE}.synced_at), "
        f"  updated_at = CURRENT_TIMESTAMP",
        (str(info_hash), str(site),
         None if torrent_id is None else str(torrent_id),
         status,
         None if cover_url is None else str(cover_url),
         1 if synced else 0),
    )
    conn.commit()
    row = conn.execute(
        f"SELECT torrent_id, status, cover_url FROM {SITE_PUBLISH_TABLE} "
        f" WHERE info_hash = ? AND site = ?",
        (str(info_hash), str(site)),
    ).fetchone()
    conn.close()
    return tuple(row) if row else (None, None, None)


def site_publish_get(info_hash, site=SITE_MUSOPIA):
    """取某一行在某站上的 `(torrent_id, status, cover_url)`。

    ★★ **查不到返回 `(None, None, None)`，而不是抛、也不是 `("", ...)`**：
      "这个站上没有记录"是**正常状态**（还没发过去），不是错误。
      ★ 但调用方**必须**分清它与"记录在、但 id 为空" —— 后者是**异常**
        （说明写坏了）。⇒ 判据：`status is None` ⇒ 压根没这条记录；
        `status is not None and not torrent_id` ⇒ 记录坏了，要报出来。
    """
    conn = connect()
    row = conn.execute(
        f"SELECT torrent_id, status, cover_url FROM {SITE_PUBLISH_TABLE} "
        f" WHERE info_hash = ? AND site = ?",
        (str(info_hash), str(site)),
    ).fetchone()
    conn.close()
    return tuple(row) if row else (None, None, None)


def site_publishes(info_hash):
    """取该行在**所有**站点上的记录，返回 `{site: {"torrent_id","status","cover_url"}}`。

    ★ 给报告/诊断用（"这条到底发过哪些站"）。★ 返回 dict 而不是列表：
      调用方几乎总是按站点名取，而列表会逼他们写 `for` + `if`，
      那正是"漏掉一个站也不报错"的形状。
    """
    conn = connect()
    rows = conn.execute(
        f"SELECT site, torrent_id, status, cover_url FROM {SITE_PUBLISH_TABLE} "
        f" WHERE info_hash = ? ORDER BY site",
        (str(info_hash),),
    ).fetchall()
    conn.close()
    return {r[0]: {"torrent_id": r[1], "status": r[2], "cover_url": r[3]}
            for r in rows}


def site_torrent_id_of(info_hash, site=SITE_MUSOPIA):
    """只要 id（`site_publish_get` 的窄版）。★ 没记录 / id 为空 ⇒ 都返 `""`。

    ★ 这个"都返 `''`"是刻意的、也**必须**如此：本函数的调用方是
      "拿 id 去站点操作"的路径，它的判据只能是"有没有 id"。
      ⇒ ★ **判"这条发过没有"不许用本函数**，要用 `site_publish_get`
         —— 两者在"记录在但 id 为空"这种**异常**上给出相同结果（都是 `''`），
         而那种异常是必须看得见的。
    """
    return site_publish_get(info_hash, site)[0] or ""


def site_status_of(info_hash, site=SITE_MUSOPIA):
    """只要 status。★ 没记录 ⇒ `None`（不是 `'UNKNOWN'` —— 那会与"记录说它是 UNKNOWN"混）。"""
    return site_publish_get(info_hash, site)[1]


def transfer_rule_of(info_hash):
    """皇后的**转载规则**（`tasks.transfer_rule`）—— 闸门④ 的输入。

    ★ 返回**原始值**（可能是 `None` / `''` / `'ALLOW'` / `'DENY'` /
      `'NEED_CONFIRM'` …）—— **不做**归一化、**不说**"能不能转"：
      判"能不能转"是调用方的判据（`is_transferable_to()` / `EDIT_RULE_GUARD`），
      本函数只负责**取数**。★ 两件事混在一起 = 兜底值与真实结果混报
      。

    ★★ 为什么要有这个窄函数（而不是每次手写一条 `SELECT`）：
      闸门④ 现在有**两处** Python 侧消费者 —— `db.is_transferable_to()`
      （转种解释）与 `db.edit_rule_ok()` 一族（`EDIT_RULE_GUARD`）。
      ★ 手抄 SQL 必然漂移⇒ 取数落成**有名字的一处**。

    ★ 注意"空规则"的语义（这条是本项目反复踩的坑，别按直觉改）：
      `NULL` / `''` 的含义是「**不知道**能不能转」，**不是**「允许」
      ⇒ 只有 `'ALLOW'` 放行（见 `PENDING_PUBLISH_WHERE` 附近那段长注释）。
    """
    if not info_hash:
        return None
    conn = connect()
    row = conn.execute(
        "SELECT transfer_rule FROM tasks WHERE info_hash = ?", (info_hash,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def site_cover_url_of(info_hash, site=SITE_MUSOPIA):
    """该站**自己那份**封面 URL。★ 没记录 ⇒ `""`（不是 None，调用方一律当"没有"）。

    ★★ 为什么必须按站取：两站的图床**不同**（乌托邦 picgo / 蟹黄堡 Lsky Pro）。
      拿乌托邦那份去发蟹黄堡 ⇒ 蟹黄堡页面上挂着别的图床的链
      （用户 2026-09-25 明确否掉了这个做法）。
    """
    return site_publish_get(info_hash, site)[2] or ""


# 跨站去重查询条件：乌托邦已发布且具备有效 site_torrent_id
CROSS_SITE_DEDUP_WHERE = (
    "site_status = 'EXISTS' AND site_torrent_id IS NOT NULL AND site_torrent_id != ''"
)

# 皇后已发布条件：以 CROSS_SITE_DEDUP_WHERE 为前缀，且包含非空 opencd_tid
QUEEN_PUBLISHED_WHERE = (
    "site_status = 'EXISTS' AND site_torrent_id IS NOT NULL AND site_torrent_id != ''"
    " AND opencd_tid IS NOT NULL AND opencd_tid != ''"
)

# 可转种候选条件：以 CROSS_SITE_DEDUP_WHERE 为前缀，且 transfer_rule 必须为 ALLOW
# 注意：为兼容离线 AST 字面量解析，显式保留完整展开前缀
TRANSFERABLE_WHERE = (
    "site_status = 'EXISTS' AND site_torrent_id IS NOT NULL AND site_torrent_id != ''"
    " AND transfer_rule = 'ALLOW'"
)

# 蟹黄堡发布队列条件：以 TRANSFERABLE_WHERE 为前缀，且排除发布失败状态（status != 'FAILED'）
CRABPT_QUEUE_WHERE = (
    "site_status = 'EXISTS' AND site_torrent_id IS NOT NULL AND site_torrent_id != ''"
    " AND transfer_rule = 'ALLOW'"
    " AND (status IS NULL OR status != 'FAILED')"
)

# 蟹黄堡直发队列条件：允许转载且排除失败状态，不依赖源站发布状态
CRABPT_DIRECT_WHERE = (
    "transfer_rule = 'ALLOW'"
    " AND (status IS NULL OR status != 'FAILED')"
)

# 猫站直发队列条件：允许转载且必须包含抓轨日志（status 必须为 SUCCESS）
PTER_DIRECT_WHERE = (
    "transfer_rule = 'ALLOW'"
    " AND status = 'SUCCESS'"
)

# 星湾直发队列条件：允许转载且排除失败状态（与蟹黄堡同口径，不强制 log）
XINGWAN_DIRECT_WHERE = (
    "transfer_rule = 'ALLOW'"
    " AND (status IS NULL OR status != 'FAILED')"
)

# 麒麟直发队列条件：★ 与星湾**同口径**（不强制 log、不强制 status=SUCCESS），
# 直接复用星湾那份 WHERE —— 避免两站各写一份 SQL 漂移（`ERR-META-03`）。
HDKYLIN_DIRECT_WHERE = XINGWAN_DIRECT_WHERE


def is_direct_publishable_to(info_hash, site=SITE_CRABPT):
    """★ **直发判据**：这条能不能**直接**发到 `site`（不经源站）。

    ★★★ 本函数**必须**与 `pending_crabpt_direct()`（实际选行）**同口径** ——
      两者对**同一行**给出相反答案是**实测发生过**的事故（2026-09-28，见
      `is_transferable_to` 的 docstring）：工具说"在队列里"、实际永远不发。

    ★ 与 `is_transferable_to()` 的**全部差别**只有一条：
      那条要求「源站（乌托邦）已上站」（①②），本函数**不看源站**。
      用户 2026-10-20 定的语义是两站**没有逻辑关系** ⇒ 源站有没有与本判据无关。

    判据（全部成立才放行）：
      ① 过闸门④：`transfer_rule == 'ALLOW'`（空 / `DENY` / `NEED_CONFIRM` 一律拦）；
         ★ 空值的语义是「**不知道**能不能转」，**不是**「允许」（`db.py` 闸门④ 那段铁律）。
      ② 过闸门⑤：`status != 'FAILED'`（发布被站点明确拒 ⇒ 出队，#23）；
      ③ `site` 上**没有**这条（有记录但 `torrent_id` 空 ⇒ 记录坏了，单独报）。
    """
    if not info_hash:
        return False, "info_hash 为空"
    rule = transfer_rule_of(info_hash)
    # ★ 归一化必须在**一处**（与 `is_transferable_to` 同纪律）：
    #   库里这一列可能是 `NULL` / 空串 / 带空格。与 SQL 侧
    #   `transfer_rule = 'ALLOW'` **等价**（NULL 与 'DENY' 都不等于 'ALLOW' ⇒ 都拦）。
    if (rule or "").strip() != "ALLOW":
        if rule == "DENY":
            return False, ("transfer_rule='DENY' ⇒ 闸门④ 拦下（只有 'ALLOW' 放行）"
                           "—— 上游明令**不许再分发**")
        return False, (f"transfer_rule={rule!r} ⇒ 闸门④ 拦下（只有 'ALLOW' 放行）"
                       f"{'（规则为空：没标过 ⇒ 按「不知道」拦）' if not (rule or '').strip() else ''}")
    cur_status = (get_status(info_hash) or "").strip()
    if cur_status == "FAILED":
        return False, ("本地已把这条标 `status='FAILED'`（发布被站点明确拒）⇒ "
                       "已从直发队列出队（#23）；如需重发先人工把它改回")
    if site == SITE_PTER and cur_status != "SUCCESS":
        return False, (f"猫站要求必须带 Log（当前 status={cur_status!r}，非 SUCCESS）⇒ 拦截")
    dst_status = site_status_of(info_hash, site)
    if dst_status is not None:
        if not site_torrent_id_of(info_hash, site):
            return False, (f"目标站（{site}）**有记录但 torrent_id 为空** ⇒ "
                           f"记录坏了，需人工核（状态 {dst_status!r}）")
        return False, f"目标站（{site}）上已有（id={site_torrent_id_of(info_hash, site)}）"
    return True, (f"过闸门④（`transfer_rule='ALLOW'`）、"
                  f"目标站（{site}）没有 ⇒ 可直发")


def is_transferable_to(info_hash, site=SITE_CRABPT):
    """★ **转种判据**：这条能不能从**别的**站转去 `site`。

    ★★★ 本函数**必须**与 `transferable_to()`（实际选行）**同口径** ——
      两者对**同一行**给出相反答案是**实测发生过**的事故（2026-09-28）：

        publish.py --site crabpt --dry-run --only 144f017a
          ❌ 144f017a 不在转种队列里                      ← 队列实际筛选（**对**）
          ✅ 它**在**转种队列里（源站有、目标站没有 ⇒ 该转）← 本函数的 why（**错**）

      ★ 根因：当时本函数只判 ①②（源有 ∧ 目标没有），**缺**闸门④；而
        `transferable_to()` 已改用 `TRANSFERABLE_WHERE`（含 `ALLOW`）。
        ⇒ 队列口径与"给人看的解释"口径**分叉**。
      ★ 危害形状：不是"多发一条"（数据风险），而是
        **工具告诉你"它在队列里"、实际不会发** ⇒ **误导人做决定**。
      ★ 一般化：**给人看的判据必须与实际判据同源**；否则同一命令里会
        自己跟自己矛盾，而**读的人只能猜哪句是真的**。

    返回 `(ok, why)`：
      · `ok=True`  ⇒ 该转，且**必须**转（`site` 上还没有）
      · `ok=False` ⇒ 不该转，`why` 是给**人**看的原因（`run_crabpt()` 的
        `--only` 零交集报文要用它，见 `explain_not_queued` 的同款纪律）

    ★★ 判据**逐条可查**（`why` 说的就是哪一条没过），不是"筛完算数"：
      ① 源站（乌托邦）上**有**这条 —— 转种的前提是"有源"。
         ★ 用 `tasks.site_status='EXISTS'` 判而不是 `site_publish(site='musopia')`
           —— 两者**现在**同义（`sync_opencd_tid.py` 只往 `site_publish` 写，
           而 `site_status` 是它的**旧镜像列**），但只有 `tasks.site_status`
           是 `CROSS_SITE_DEDUP_WHERE` 唯一能放进**同一条 SQL** 的形态。
           ★ 代价：`why` 里只能写"源站没有"，说不出更细的（哪个列空）。
             这是刻意的 —— 细化到列名会让**两个**消费者（SQL 与这里的报文）
             各写一份判据。
      ② ★ **过闸门④**：`transfer_rule == 'ALLOW'`（只有 `ALLOW` 放行；
         空 / `NEED_CONFIRM` / `DENY` **一律拦**）。
         ★★ 这一条是 2026-09-28 补的 —— 本函数**原先没有它**，于是它会对
           `15972`（`EXISTS` + `DENY`）回答"该转"，而那条**明明被闸门④ 拦着**。
         ★ 它必须与 `TRANSFERABLE_WHERE` 的尾巴**语义**一致（逐字由
           `run.py --selftest` ⑦e-2 钉住）；★ 注意**不许**写成
           `!= 'DENY'` —— 那会把空规则/`NEED_CONFIRM` 判成**放行**，
           与闸门④ **语义相反**（ 的形状："提到了对的词、
           却问了个错的问题"）。
         ★ 为什么在这里查而不是"复用 `transferable_to()` 的返回集"：
           那要**再拉一遍全队列**（O(队列) 次查询只为解释一行）；
           而口径同源的保证**不靠**复用代码路径，靠的是 ⑦e 的**常量前缀判据**
           + 本函数与 `TRANSFERABLE_WHERE` 同读一个 `tasks` 列。
      ③ `site` 上**没有**这条 —— 已经有了就不叫"转种"叫"重复"。
    """
    if not info_hash:
        return False, "info_hash 为空"
    src = site_status_of(info_hash, SITE_MUSOPIA)
    # ★ 注意用 `!= 'EXISTS'` 而不是 `is None`：`UNKNOWN` 与 `None` **都**表示
    #   "源站上没这条"（`None` = 没记录，`UNKNOWN` = 记录说不知道）——
    #   两者在"有没有源"这个问题上**答案相同**，所以这里可以合并。
    if src != "EXISTS":
        got = site_publish_get(info_hash, SITE_MUSOPIA)
        if got[1] is None:
            return False, "源站（乌托邦）没有这条的记录（`site_publish` 里查无此行）"
        return False, f"源站（乌托邦）上不是 EXISTS 而是 {src!r} ⇒ 没有可转的源"
    # ★★★ 闸门④（`TRANSFERABLE_WHERE` 的尾巴）——
    #   ★ 放在 ① 之后、③ 之前：与 `transferable_to()` 的 SQL 里
    #     `WHERE <CROSS> AND transfer_rule = 'ALLOW'` 的**求值顺序无关**
    #     （三条都是 AND，全部成立才转），放这里只是为了让 `why` **先**报
    #     "源站没有"这种更根本的原因。
    rule = transfer_rule_of(info_hash)
    # ★★ 归一化必须在**一处**：库里这一列可能是 `NULL` / 空串 / 带空格。
    #   ★ 与 SQL 侧 `transfer_rule = 'ALLOW'` **等价**（SQL 里 `NULL` 与
    #     `'DENY'` 都不等于 `'ALLOW'` ⇒ 都拦）⇒ 语义一致，只是这里要**归一**
    #     所以 `None` 报成 `''` 之后还要**说清**"空规则也拦"）。
    if (rule or "").strip() != "ALLOW":
        # ★ 报文分档：`DENY` 是"上游明令不许"（要人看一眼），
        #   空 / 其它是"没标过 ALLOW"（规则缺失）。两者**拦法相同、含义不同**
        #     压成一句，读的人只能猜）。
        if rule == "DENY":
            return False, ("transfer_rule='DENY' ⇒ 闸门④ 拦下（只有 'ALLOW' 放行）"
                           "—— 上游明令**不许再分发**")
        return False, (f"transfer_rule={rule!r} ⇒ 闸门④ 拦下（只有 'ALLOW' 放行）"
                       f"{'（规则为空：没标过）' if not (rule or '').strip() else ''}")
    #   ★ 与 `transferable_to()` 的 SQL 里 `AND (status IS NULL OR status != 'FAILED')`
    #     **等价**：`get_status()` 对**不存在/NULL** 返 `None` ⇒ `None or ""` ⇒ `""`
    #     ⇒ `!= "FAILED"` ⇒ **放行**，与 SQL 的 `status IS NULL` 那一支**同判**；
    #     `""` / `"SUCCESS"` / `"PUBLISHED"` ⇒ 放行；只有**恰好** `"FAILED"` ⇒ 拦。
    #     ★ 归一化（`.strip()`）是为了与 SQL 里那种"列可能带空格"的写坏数据一致 ——
    #       两站同一个纪律（见 `transfer_rule` 那段"归一化必须在一处"的说明）。
    #   ★ 为什么它必须在这里也判：本函数是"给人看的解释"，**必须**与实际选行
    #     同源 —— 否则会出现"解释器说在队列里、队列实际不选它"的分叉
    #     压成一句）—— `FAILED` 是**本地的发布结果**，与上游的转载规则无关。
    if (get_status(info_hash) or "").strip() == "FAILED":
        return False, ("本地已把这条标 `status='FAILED'`（发布被站点明确拒）⇒ "
                       "已从蟹黄堡发布队列出队（#23）；如需重发先人工把它改回")
    dst_status = site_status_of(info_hash, site)
    if dst_status is not None:
        # ★★ 这里**必须**区分"记录在但 id 空"（写坏了）与"正常已发"，
        #   否则一条写坏的记录会被当成"已发"而**永远拦着**这条种子
        if not site_torrent_id_of(info_hash, site):
            return False, (f"目标站（{site}）**有记录但 torrent_id 为空** ⇒ "
                           f"记录坏了，需人工核（状态 {dst_status!r}）")
        return False, f"目标站（{site}）上已有（id={site_torrent_id_of(info_hash, site)}）"
    return True, (f"源站有（乌托邦）、过闸门④（`transfer_rule='ALLOW'`）、"
                  f"目标站（{site}）没有 ⇒ 该转")


def transferable_to(site=SITE_CRABPT):
    """★ 转种队列：**有源（乌托邦已上站）∧ 过闸门④（`ALLOW`）∧ 目标站没有** ∧
    **没被标 `FAILED` 出队**的行。

    ★ 返回**完整 task 行**（`TASK_FIELDS`），因为调用方（`publish.py`）
      接着要用它渲染表单 —— 只给 hash 会逼调用方再查一次，
      而"再查一次"就会出现**两份 WHERE**。

    ★ 排序：`updated_at` 升序（与 `get_published()` 同口径 —— 老的先转，
      不因为新行插队而永远轮不到）。
      ★★ 注意这条排序与 `status != 'FAILED'` 的**组合**才是 #23 的要点：
        没有 `status` 这层，一条坏种子会**恒在队首**（确定性最老优先）而被每周期重试。

    ★★★ 口径 = `CRABPT_QUEUE_WHERE`（2026-10-04 起）。它的前缀 = `TRANSFERABLE_WHERE`
      （**不是** `CROSS_SITE_DEDUP_WHERE`，2026-09-28 修）。差别逐条：
      ① 相对 `CROSS_SITE_DEDUP_WHERE`：多一条 `transfer_rule = 'ALLOW'`。
         ★ 为什么这条差别**必须有**（实测，不是推理）：见 `TRANSFERABLE_WHERE`
           上面那段 —— `15972`（`EXISTS` + `DENY`）原先会被选进队列、
           被搬到蟹黄堡，而那是**不可逆**的。
      ② 相对 `TRANSFERABLE_WHERE`：多一条 `status != 'FAILED'`（#23）。
         ★ 为什么**必须有**：`run_crabpt` 对"被站点明确拒"的种子标 `status='FAILED'`，
           指望它出队；而转种口径**原先不看 `status`** ⇒ 标了不出队 ⇒ 队列永久卡住。
           详见 `CRABPT_QUEUE_WHERE` 上面那段。
      ★ 为什么**不能**内联这些 WHERE（手写进本函数）：手抄必然与常量漂移，
        那正是  的形状。⇒ 一律引用常量。
      ★ 为什么**不能**并进 `CROSS_SITE_DEDUP_WHERE`：会把 `publish.py` 里
        那条"应当恒为空"的差集断言变成**恒空**（检测器静默失效）。
        见 `TRANSFERABLE_WHERE` 上面 ①②③ 逐条。
    """
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        f"SELECT {TASK_FIELDS} FROM tasks "
        f" WHERE {CRABPT_QUEUE_WHERE} "
        f"   AND info_hash NOT IN "
        f"       (SELECT info_hash FROM {SITE_PUBLISH_TABLE} WHERE site = ?) "
        f" ORDER BY updated_at",
        (str(site),),
    )
    rows = _rows_to_dicts(cur)
    conn.close()
    return rows


def count_transferable_to(site=SITE_CRABPT):
    """★ 转种队列的**计数**。★★ 必须与 `transferable_to()` 用**同一个**
    `CRABPT_QUEUE_WHERE` 字符串 —— 理由逐字同 `count_stale()` 那段：
    一旦探针（计数）和实际筛选漂移，就会变成"探针说有活 → 一条都选不出来 →
    探针还说有活"的**静默空转**，不报任何错。
    ★★★ 2026-09-28：这里原先写的是 `CROSS_SITE_DEDUP_WHERE` —— 那正是
      "探针与实际筛选**口径不同**"的形态（探针数 28，实际该发 27）。
      现在两处**同一个常量**，且该常量就是实际筛选用的那一个。
    ★★ 2026-10-04（#23）：常量从 `TRANSFERABLE_WHERE` 换成 `CRABPT_QUEUE_WHERE`
      （多了 `status != 'FAILED'`）—— 与 `transferable_to()` **同一步改**，
      否则又正好构成上面警告的那个"计数 28 / 实发 27"漂移。
    """
    conn = connect()
    n = conn.execute(
        f"SELECT COUNT(*) FROM tasks "
        f" WHERE {CRABPT_QUEUE_WHERE} "
        f"   AND info_hash NOT IN "
        f"       (SELECT info_hash FROM {SITE_PUBLISH_TABLE} WHERE site = ?)",
        (str(site),),
    ).fetchone()[0]
    conn.close()
    return n


def pending_crabpt_direct(site=SITE_CRABPT):
    """★ **蟹黄堡直发队列**：过闸门④（`ALLOW`）∧ 没被标 `FAILED` ∧ 目标站没有。

    ★★★ 与 `transferable_to()` 的**唯一**差别：**不要求** `site_status='EXISTS'`。
      理由（用户 2026-10-20 拍板）：「蟹黄堡跟乌托邦没有逻辑关系」——
      `site_status` 那一列记的是**乌托邦**，拿它当蟹黄堡的门 = 让乌托邦的 log 闸门
      连坐蟹黄堡（实测：乌托邦卡掉 65%，蟹黄堡日发量掉到 0）。见 `CRABPT_DIRECT_WHERE`。

    ★★ 返回**完整 task 行**（`TASK_FIELDS`）—— 理由逐字同 `transferable_to()`：
      调用方（`publish.py`）接着要用它渲染表单，只给 hash 会逼调用方再查一次，
      而"再查一次"就会出现**两份 WHERE**。

    ★★ 排序：`updated_at` 升序（老的最先）—— 与 `get_published()` / `transferable_to()`
      **同口径**。★ 这条排序与 `status != 'FAILED'` 的组合才是 #23 的要点：没有
      `status` 这层，一条坏种子会**恒在队首**而被每周期重试。

    ★★★ 去重**只走** `site_publish(site=?)`，**不许**用 `tasks.site_status`：
      `site_status` 只记乌托邦（见上）。用错列 ⇒ 要么漏发（乌托邦没有就永远不发），
      要么**静默多发**（蟹黄堡已发过的被当成没发过 ⇒ 重复上站，**不可撤销**）。

    ★ 为什么 WHERE 引用常量而不是手写：手抄必然与常量漂移。
    """
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        f"SELECT {TASK_FIELDS} FROM tasks "
        f" WHERE {CRABPT_DIRECT_WHERE} "
        f"   AND info_hash NOT IN "
        f"       (SELECT info_hash FROM {SITE_PUBLISH_TABLE} WHERE site = ?) "
        f" ORDER BY updated_at",
        (str(site),),
    )
    rows = _rows_to_dicts(cur)
    conn.close()
    return rows


def count_pending_crabpt_direct(site=SITE_CRABPT):
    """★ 直发队列的**计数**。★★ 必须与 `pending_crabpt_direct()` 用**同一个**
    `CRABPT_DIRECT_WHERE` 字符串 —— 理由逐字同 `count_transferable_to()` 那段：
    一旦探针（计数）和实际筛选漂移，就会变成"探针说有活 → 一条都选不出来 →
    探针还说有活"的**静默空转**，不报任何错。
    """
    conn = connect()
    n = conn.execute(
        f"SELECT COUNT(*) FROM tasks "
        f" WHERE {CRABPT_DIRECT_WHERE} "
        f"   AND info_hash NOT IN "
        f"       (SELECT info_hash FROM {SITE_PUBLISH_TABLE} WHERE site = ?)",
        (str(site),),
    ).fetchone()[0]
    conn.close()
    return n


def pending_pter_direct(site=SITE_PTER):
    """★ 猫站直发队列：过闸门④（`ALLOW`）∧ status='SUCCESS'（必须含Log）∧ 目标站没有。

    ★★ 排序：`updated_at` 升序（老的最先），与 crabpt/musopia 同口径。
    """
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        f"SELECT {TASK_FIELDS} FROM tasks "
        f" WHERE {PTER_DIRECT_WHERE} "
        f"   AND info_hash NOT IN "
        f"       (SELECT info_hash FROM {SITE_PUBLISH_TABLE} WHERE site = ?) "
        f" ORDER BY updated_at",
        (str(site),),
    )
    rows = _rows_to_dicts(cur)
    conn.close()
    return rows


def count_pending_pter_direct(site=SITE_PTER):
    """★ 猫站直发队列计数。必须与 `pending_pter_direct()` 用同一个 PTER_DIRECT_WHERE。"""
    conn = connect()
    n = conn.execute(
        f"SELECT COUNT(*) FROM tasks "
        f" WHERE {PTER_DIRECT_WHERE} "
        f"   AND info_hash NOT IN "
        f"       (SELECT info_hash FROM {SITE_PUBLISH_TABLE} WHERE site = ?)",
        (str(site),),
    ).fetchone()[0]
    conn.close()
    return n


def pending_xingwan_direct(site=SITE_XINGWAN):
    """★ 星湾直发队列：过闸门④（`ALLOW`）∧ 未被标 `FAILED` ∧ 星湾没有。

    口径与蟹黄堡一致（不强制 log），排序同样 `updated_at` 升序。
    """
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        f"SELECT {TASK_FIELDS} FROM tasks "
        f" WHERE {XINGWAN_DIRECT_WHERE} "
        f"   AND info_hash NOT IN "
        f"       (SELECT info_hash FROM {SITE_PUBLISH_TABLE} WHERE site = ?) "
        f" ORDER BY updated_at",
        (str(site),),
    )
    rows = _rows_to_dicts(cur)
    conn.close()
    return rows


def count_pending_xingwan_direct(site=SITE_XINGWAN):
    """★ 星湾直发队列计数。必须与 `pending_xingwan_direct()` 用同一个 WHERE。"""
    conn = connect()
    n = conn.execute(
        f"SELECT COUNT(*) FROM tasks "
        f" WHERE {XINGWAN_DIRECT_WHERE} "
        f"   AND info_hash NOT IN "
        f"       (SELECT info_hash FROM {SITE_PUBLISH_TABLE} WHERE site = ?)",
        (str(site),),
    ).fetchone()[0]
    conn.close()
    return n


def pending_hdkylin_direct(site=SITE_HDKYLIN):
    """★ 麒麟直发队列：过闸门④（`ALLOW`）∧ 未被标 `FAILED` ∧ 麒麟没有。

    口径与星湾一致（`HDKYLIN_DIRECT_WHERE = XINGWAN_DIRECT_WHERE`，不强制 log），
    排序同样 `updated_at` 升序。
    """
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        f"SELECT {TASK_FIELDS} FROM tasks "
        f" WHERE {HDKYLIN_DIRECT_WHERE} "
        f"   AND info_hash NOT IN "
        f"       (SELECT info_hash FROM {SITE_PUBLISH_TABLE} WHERE site = ?) "
        f" ORDER BY updated_at",
        (str(site),),
    )
    rows = _rows_to_dicts(cur)
    conn.close()
    return rows


def count_pending_hdkylin_direct(site=SITE_HDKYLIN):
    """★ 麒麟直发队列计数。必须与 `pending_hdkylin_direct()` 用同一个 WHERE。"""
    conn = connect()
    n = conn.execute(
        f"SELECT COUNT(*) FROM tasks "
        f" WHERE {HDKYLIN_DIRECT_WHERE} "
        f"   AND info_hash NOT IN "
        f"       (SELECT info_hash FROM {SITE_PUBLISH_TABLE} WHERE site = ?)",
        (str(site),),
    ).fetchone()[0]
    conn.close()
    return n


def get_published_for_site(site, *, only_stale=False, require_allow=True, limit=0,
                           torrent_id=None):
    """按站点读取 `site_publish` 中仍为 EXISTS 的已发布种子（编辑链路用）。

    返回的每行带 `site_torrent_id`（该站点的种子 ID）与 `sp_synced_at`。
    `only_stale=True` 时只返回从未同步过、或源任务在同步后又被更新过的行。
    """
    where = [
        "sp.site = ?",
        "sp.status = 'EXISTS'",
        "sp.torrent_id IS NOT NULL",
        "sp.torrent_id != ''",
    ]
    params = [str(site)]
    if require_allow:
        where.append("t.transfer_rule = 'ALLOW'")
    if torrent_id is not None:
        where.append("sp.torrent_id = ?")
        params.append(str(torrent_id))
    if only_stale:
        where.append(
            "(sp.synced_at IS NULL OR t.updated_at IS NULL OR t.updated_at > sp.synced_at)"
        )
    sql = (
        f"SELECT t.*, sp.torrent_id AS sp_torrent_id, sp.synced_at AS sp_synced_at "
        f"  FROM tasks t JOIN {SITE_PUBLISH_TABLE} sp ON sp.info_hash = t.info_hash "
        f" WHERE {' AND '.join(where)} "
        f" ORDER BY t.updated_at"
    )
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    conn = connect()
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = _rows_to_dicts(cur)
    conn.close()
    for row in rows:
        row["site_torrent_id"] = row.get("sp_torrent_id")
    return rows
