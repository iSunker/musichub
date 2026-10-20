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
    # ★★ 闸门（2026-09-23 改判）：
    #   闸门① `qb_seed_days` —— **已退役**（`opencd_gate.py` 模块头有完整理由）。
    #          ★ 列留着不删是为了让旧数据可读，但它**已无写入方、也已无读者**。
    #   闸门② `opencd_added_at` + 7 天 —— **皇后发布那条种子的时刻**
    #          （外部事实，见下面 `opencd_added_at` 的注解）。
    #          ★ 它**取代**了旧的 `fetched_at` + 3 天口径，见 `PENDING_PUBLISH_WHERE`。
    ("qb_seed_days", "REAL"),
    # ★★ 皇后的**发布日期**（2026-09-23 补）—— 新闸门②的输入。
    #
    #   来源：vertex2 库 `torrents.add_time`（unix 秒 = 该种子被加进 tracker 的时刻），
    #   由 `sync_opencd_tid.py` 按 infohash 配对时**顺带**回填。
    #
    #   ★★ 为什么存**原始发布日期**、而不是存算好的「+7 天」结果：
    #     算成结果（比如塞进 `publish_after = add_time + 7d`）看起来更省一列，
    #     但那是把**事实**加工掉了 ——
    #       · 改门槛（3 天 / 14 天）就得**重算全库**，而重算要重连外部库；
    #       · 一旦重算，**原始日期就没了**，以后再也回答不了"这条是几号发布的"；
    #       · 而 `publish_after` 是**派生值**：派生的东西不该盖掉它的来源
    #         （`ERR-META-03`：不要手工复制事实来源的口径）。
    #     ⇒ 存事实，门槛在 WHERE 里**现算**（见 `PENDING_PUBLISH_WHERE` 闸门②）。
    #
    #   ★★ 时区（★ 这个坑本机测不出来，见 `PENDING_PUBLISH_WHERE` 里那段警告）：
    #     写入侧 (`sync_opencd_tid.py`) **显式**用东八区把 unix 秒转成字符串，
    #     **不依赖容器 TZ**（本机 UTC / 生产 CST+8 会差 8 小时）。
    #     与 `publish_after` 同规矩：两边**同源同时区**才敢直接字符串比较。
    #
    #   ★ 与 `fetched_at` 的区别（别混）：
    #     `fetched_at`   = **我们**抓页面的时刻（我们自己造的时间）
    #     `opencd_added_at` = **皇后**发布那条种子的时刻（外部事实，不会因为我们晚抓而变）
    #     ⇒ 闸门②原先用 `fetched_at + 3d` 有个内在毛病：**我们什么时候去抓，
    #       反悔窗口就从什么时候起算** —— 晚抓 = 白等。改用 `opencd_added_at`
    #       后，窗口锚在**真正该锚的地方**。
    ("opencd_added_at", "TIMESTAMP"),
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
    # ★★ 皇后的**转载规则**（2026-09-23 补）—— 三态，来自 `opencd_parse`：
    #      `ALLOW` / `NEED_CONFIRM`（拿不准）/ `DENY`（禁止轉載）。
    #   ★★ 为什么必须**落库**：这条判据**只存在于皇后页面上**（`轉載規則` 那一格）。
    #      不抓页面就**无从判断** —— 实测 `id=200982` 的规则是「禁止轉載」，
    #      而它当时**已经排进了发布队列**（`db.PENDING_PUBLISH_WHERE` 不知道
    #      有这回事）⇒ 差一步就把"禁止轉載"的种子转发出去。
    #   ★ 为什么不并进 `mapped_json`：`mapped_json` 是 `opencd_map.map_all()` 的
    #      产出（**渲染**用的字段快照），而转载规则是**能不能发**的判据。
    #      两件事：一个决定"发什么"，一个决定"许不许发"。混在一个 JSON 里，
    #      查询就没法用它做过滤（而它将来很可能要进 WHERE）。
    #   ★ 它**不参与** `PENDING_PUBLISH_WHERE` 的默认口径（见那里的注释理由）：
    #      `NEED_CONFIRM` 该不该拦是**产品决定**，没定之前不进 WHERE 直接改变行为。
    ("transfer_rule", "TEXT"),
    # ★★ 皇后的**简介正文**（2026-09-26 补）—— `divdescr` 原文，**已剥图**。
    #
    #   ★ 为什么必须**落库**（这是本次的第四条洞）：这个值一直被抓下来
    #     （`opencd_parse.parse_detail` 从 `<div id="divdescr">` 读），
    #     但 `opencd_map.map_all()` 原先**不返回它** ⇒ `build_row()` 拿不到
    #     ⇒ **从未落库**（实测 58 条 `mapped_json` 里 `descr` 键出现 **0** 次）。
    #     ⇒ 站上的简介一直是 `descr.build_descr()` **现渲染**（封面+曲目+日志）。
    #     ★★ 形状还是那句：**抓到了、没往外传**（`ERR-DATA-01` 同族）。
    #
    #   ★ 为什么**单独一列**而不只放进 `mapped_json`：
    #     两者都要 —— `mapped_json` 是**快照**（发布时读它、不重算，见上面那条），
    #     本列是**渲染读取口**（`merge_mapped` 从快照提升到顶层用的键名）。
    #     `small_descr` 当初的教训正是"只在快照里、渲染读不到"。
    #     ⚠️ 但**别**把它当成"两个真相"：权威值仍是 `mapped_json.descr`，
    #       本列是它的**副本**（回写工具 `tools/recall_opencd_descr.py` 的同款分工）。
    ("opencd_descr", "TEXT"),
    # ★★ 版本标签 `整轨` / `分轨`（2026-10-14 补，`ERR-PUBLISH-20` 修复②）。
    #
    #   ★ 为什么必须**单独一列**、且**单一判据**：
    #     本项目的"整轨 vs 分轨"以前**没有一处是权威的** —— 它被**隐式**编进
    #     两个字段里，而两个字段各有各的算法：
    #       · `torrent_name` = qB 原始种名（皇后原文 / 抓轨原始名）里带的词；
    #       · `torrent_file` = `parse_title()` 从**原始种名二次拆分**后拼出的标题
    #         （`… - FLAC 整轨`）算出来的输出文件名。
    #     ⇒ 同一行的两个字段可以**各说各的版本**（实测 21662：`torrent_name` 偏
    #       "分轨"、`torrent_file` 偏 "整轨"）—— 这正是 `ERRATA:576` 那族
    #       "同一事实两处写、各写各的"的形状（`ERR-DATA-07`）。
    #   ★ 修法：版本**只有一个出处** `assets.cue_track_type()`（强判据：cue 的
    #     `FILE` 目标必须真存在才算整轨），**同时**写进本列；`torrent_file` 的
    #     标题、发布侧站点的版本核对，一律读**本列**或它派生的标题，
    #     **不再**各自去猜种名。
    ("track_type", "TEXT"),
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
LEDGER_KIND_REQUEST = "request"          # 出网请求量纲（crawl 的 RateGate 用；站点保护）
LEDGER_KIND_SEED = "seed"                # 种子量纲（入库成功记 1；运维口径，见 opencd_ingest）
#   ★★ 两个 kind 是**两本独立的账**（同表、同 day_key、不同 kind ⇒ 独立行，见 LEDGER_PK）。
#      · request = "今天向皇后发了几次请求"（站点保护，硬刹车）
#      · seed    = "今天有几条皇后种子入库成功"（人看的产能口径）
#      ★ 为什么要分开而不是共用一个计数：**量纲不同**（一条种子 = 1 个请求，但"跳过已抓过的"
#        与"入库失败"会让两者偏离）。混成一个数就没法回答"今天到底补了几条"（ERR-META-10 同族）。
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


# ==================== 多站点发布身份（`site_publish`）====================
#
# ★★★ 为什么需要这张表（2026-09-25 加，起因是加蟹黄堡第二个发布站）：
#
#   `tasks.site_status` / `tasks.site_torrent_id` 各**只有一列**，语义是
#   **"在乌托邦上"**。它写在列名底下是隐形的 —— 列名里**没有站点**
#   （`ERR-META-06` 那句"单看一个数字无法判断它属于谁"的又一次实例）。
#   ⇒ 一旦第二个站（蟹黄堡）也往这两列写，会**污染四个真实判据**：
#       ① 发布队列        `PENDING_PUBLISH_WHERE`（上面）
#       ② 已发布集合      `_published_where()` / `get_missing_cover()`
#       ③ ★ scan 跳过     `run.py` 的 `status=='SUCCESS' and site_status=='EXISTS'`
#                         ⇒ 本地抓轨不再重做，而这条在乌托邦上根本不存在
#       ④ 计数/巡检       `stats()` / `tools/nas_health.py` / `tools/gen_counts.py`
#
#   ★★ 最危险的不是"会互相覆盖"，是**判据看着还能用** —— 加站那天差点
#      拿 `site_status` 去筛蟹黄堡（那是拿乌托邦的结论否掉另一个站）。
#
#   ⇒ 判据：**"发到哪个站"是 (种子, 站点) 这个二元组上的属性**，
#     所以它该是一张**关联表**，不是 `tasks` 上的一列。
#     ★ 用复合主键而不是"每站一列"：**加第三个站只需 INSERT 一行，不改 schema**
#       （加一列的做法每来一个站就要改表 + 改所有查询）。
#
#   ★★ 与旧两列的关系（**刻意如此**）：
#     `site_status` / `site_torrent_id` **保留、继续写**，语义收窄为
#     **"乌托邦那一侧"**。⇒ 15 个文件里的旧读点**一个都不用改**，回归面 ≈ 0。
#     新代码不许再读那两列去判别的站，改用本节的 `site_*_of()` 函数。
#
#   ★★ 为什么**不**做成 SQL 视图：SQLite 视图**只读**、且**不能替代表名**，
#     而写入路径写的是表 ⇒ 读写会分叉；生产库又是 daemon 在写的活库（WAL），
#     加视图有并发风险。⇒ 保留真实列 + 只加读函数。
SITE_PUBLISH_TABLE = "site_publish"

# ★ 站点名**只有这两个常量是唯一真值源**，别在别处手抄字符串
#   （`ERR-META-03`：两处各写一份必然漂移，而这里的漂移会让"查不到"长得像"没发过"）。
SITE_MUSOPIA = "musopia"
SITE_CRABPT = "crabpt"

SITE_PUBLISH_COLUMNS = [
    ("info_hash", "TEXT NOT NULL"),
    ("site", "TEXT NOT NULL"),          # SITE_MUSOPIA / SITE_CRABPT
    ("torrent_id", "TEXT"),             # 该站的页面 id（details.php?id=）
    ("status", "TEXT"),                 # 'EXISTS' / ...
    # ★★ 封面是**每站一份**的 —— 蟹黄堡用专用图床（Lsky Pro，见 `assets.py`），
    #    乌托邦用 picgo。若两站共用一个 `cover_url`（`tasks` 上那列），
    #    后发的那个会把先发的顶掉 ⇒ 与上面 site_status 是**同一个形状**的问题，
    #    所以它必须一起进这张表。
    #   ★ 也正因为它是"每站一份"的，`cover_url` 留在 `tasks` 上的那一列
    #     语义同样收窄为"乌托邦那一侧"。
    ("cover_url", "TEXT"),
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

    # ★★ 回填：把 `site_status='EXISTS'` 的行灌成 `site='musopia'` 的行。
    #
    #   为什么可以直接拷贝、不求人确认（2026-09-25 实测）：
    #     `site_status='EXISTS'` 与 `site_torrent_id` 非空**完全一致**（各 278 条、
    #     不一致 0 条）⇒ 没有"有状态没 id"这种需要人工判断的行。
    #   为什么放在 `init_db()`：每个脚本启动都调它，**幂等** ⇒ 谁先跑都行。
    #
    #   ★★ 幂等靠的是 `NOT EXISTS` 子查询，**不是**"只在表为空时回填" ——
    #      后者会让"表非空但漏了几行"永远修不好（`ERR-SQL-07`：汇总口径与
    #      "实际会写的集合"必须同口径）。
    #   ★ `status` 沿用 `site_status` 的值（都是 'EXISTS'）；`synced_at` 沿用
    #      `site_synced_at`，让新表一出生就带着"上次同步"的历史，而不是从空开始。
    #   ★ `torrent_id` 用 `!= ''` 判非空：`site_torrent_id` 的空值是 `''` 不是 NULL
    #     （实测 `EXISTS 但 id 空` = 0 条，但判据要写对，将来才拦得住）。
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
    # ★★ 以下 6 列 2026-09-21 补 —— **不补它们 = 写进库但发布时读不到**。
    #    `get_pending_publish()` 只 SELECT 这个字符串，而 `descr.build_form_fields()`
    #    要读 `opencd_tid`（决定皇后路分流）与 `artist`/`year`；
    #    皇后行的 `tags`/`medium_sel`/… 则要从 `mapped_json` 解出来。
    #    ★ 这个坑本项目**已经栽过三次**（"数据在手边、就是没传进 task"⇒ 静默变空），
    #      所以这里逐列写明它给谁用：
    "opencd_tid, artist, year, unmapped_json, mapped_json, publish_after, "
    # ★ 皇后简介正文（2026-09-26 补）：`merge_mapped` 要把它从快照提升上来，
    #   `descr.build_descr()` 要读它 ⇒ **不在这里 SELECT = 渲染永远读不到**
    #   （与上面那 6 列栽的是同一个坑）。
    "opencd_descr, "
    # ★ 版本标签（2026-10-14 补）：发布前的"版本一致性闸门"（`publish.py`）要拿它
    #   与站点实挂版本对照 ⇒ 同规矩，不在这里 SELECT = 闸门读不到。
    "track_type"
)

# ── 「待发布」的口径（★ 唯一来源，别在别处再写一份）────────────────────────
#
# ★ 抽成模块常量的理由同 `STALE_CONDITION`：将来若有第二个查询要按同一口径
#   **数**待办（比如 daemon 的探针），两处各写一份必然漂移，而漂移的表现是
#   「探针说有活 → 一条都选不出来 → 探针还说有活」的**静默空转**，不报任何错。
PENDING_PUBLISH_WHERE = (
    "status = 'SUCCESS' AND (site_status IS NULL OR site_status != 'EXISTS') "
    # ★★ 闸门②（**皇后的发布日期 + 7 天** = 可发布时刻）—— 2026-09-23 **改判**。
    #
    #    ★★ 从 `publish_after`（= 抓取时刻 + 3 天）改成 `opencd_added_at`（= 皇后
    #      发布那条种子的时刻 + 7 天）。用户 2026-09-23 定：「按皇后的添加日期算」。
    #
    #    ★ 为什么必须换（旧口径的内在毛病，不是"改个数字"）：
    #      `publish_after` 锚在 `fetched_at` —— **我们**什么时候去抓页面，
    #      反悔窗口就从什么时候起算。⇒ **晚抓 = 白等三天**；那些早就上站半年、
    #      直到今天才补页面的行，反而要再干等三天（与"这条新不新"完全脱钩）。
    #      `opencd_added_at` 是**外部事实**：皇后什么时候发的，不会因为我们
    #      晚抓而改变。⇒ 窗口锚在真正该锚的地方。
    #
    #    ★ 时区：写入侧（`sync_opencd_tid.py`）**显式**按东八区把 unix 秒转成
    #      UTC 字符串，与 `datetime('now')` **同源同时区** ⇒ 这里能直接字符串比较。
    #      ⚠️ 本机 `TZ=UTC` 而生产容器是 CST(+8) ⇒ 这个坑**本机测不出来**，
    #         只能靠 `sync_opencd_tid.py --selftest` 的时区判据钉住。
    #
    #    ★★ `IS NULL` / `= ''` 这一支**改成「拦」**（旧口径是「放行」）：
    #      旧注释的理由是"给第一阶段（qB 本地抓轨）用 —— 那批 publish_after
    #      本来就是 NULL，语义是没有反悔窗口、照旧发"。
    #      ⇒ 那个理由**已经被实测推翻**（同闸门④ 的判据反转）：
    #        `opencd_added_at` 空 的语义是「**不知道**皇后几号发的」，
    #        而「不知道」**不是**「放了很久了」。与闸门④ 一个道理：
    #        把"不知道"当成"允许"，就是每一次都在赌 —— 今晚刚因为同样的
    #        形状差点发出 6 条禁转种子。
    #      ⇒ 新判据：**拿不到发布日期 ⇒ 不发**（等 `vtid` 阶段把它填上再说）。
    #
    #    ★ 影响面（2026-09-23 换闸门时实测）：
    #      · 有 tid 的 438 行 **全部**能配对到 `add_time`（0 条为空）⇒ 这条
    #        "空 ⇒ 拦"**今天一条也拦不到**；但纪律要写进 SQL，将来 VT 那边
    #        空了才有人管。
    #      · 分布：中位 36.9 天 / 最小 0.0 / 最大 91.0 ⇒ 7 天门槛放行 346、拦 92。
    "AND (opencd_added_at IS NOT NULL AND opencd_added_at != '' "
    "     AND opencd_added_at <= datetime('now', '-7 days'))"
    # ★★ 闸门③（**皇后行必须先抓到页面**）—— 2026-09-23 补。
    #
    #    ★ 起因（实测）：`opencd_tid` 已被回填（430 条），但 `mapped_json` 仍是空的
    #      ⇒ 站上发出去的内容**六个字段全错**：副标题是「全 N 曲」而非皇后原文、
    #        标签只剩一个 `OpenCD`（皇后的「港台/流行」丢了）、媒介/製作組是猜的、
    #        艺术家/年份是空的。实测 `descr.py:621` 的 `if declared:` 不成立时
    #        会退回 `detect_tags()`，而它**自己注释警告过**对 OpenCD 标题返回 `[]`
    #        （`[]` 与"没有标签"长得一模一样）。
    #      ⇒ 更重的一条：`transfer_rule`（皇后的**转载规则**，如「禁止轉載」）
    #        **只存在于页面解析结果里**（`opencd_ingest.STATUS_DENIED`）。
    #        不抓页面就**无从判断**，等于可能把「禁止轉載」的种子转发出去。
    #
    #    ⚠️ 此前这一条**完全不存在**：这些行是**第一阶段的行**
    #       （`status='SUCCESS'`，来自 qB 本地抓轨），所以它们走的是
    #       「第一阶段照旧发」那条豁免 —— **闸门②也被一起绕过**
    #       （`publish_after` 为空）。这正是 `db.py:189` 注释担心过的"被完全绕过"。
    #
    #    ★ 判据的**分寸**（不能一刀切要求 `mapped_json` 非空）：
    #      · `opencd_tid` 空      = **本地抓轨的行**，本来就没有皇后页面 ⇒ **照旧发**
    #        （★ 这是承重的：一刀切会把第一阶段全挡下来，是大回归）
    #      · `opencd_tid` 非空但 `mapped_json` 空 = **想去读皇后却没读** ⇒ **不许发**
    #      · 两者都非空           = 页面已抓、字段已落库 ⇒ 放行
    #
    #    ★ 实测（2026-09-23）：加这条前队列 32 条 → 加后 **6 条**
    #      （那 6 条是本地抓轨行，不受影响；26 条皇后行被挡回）。
    "AND (opencd_tid IS NULL OR opencd_tid = '' "
    "     OR (mapped_json IS NOT NULL AND mapped_json != ''))"
    # ★★ 闸门④（**禁止轉載的一律不发**）—— 2026-09-23 补，用户明确定的要求。
    #
    #    ★ 皇后的 `轉載規則` 三态（`opencd_parse` / `opencd_gate`）：
    #        `ALLOW` 允许 / `NEED_CONFIRM` 拿不准 / `DENY` 禁止轉載。
    #      用户 2026-09-23 点名：`id=200982` 是**禁止转载**，「**不要发！！**」。
    #
    #    ★★ 为什么它比闸门③更硬：闸门③只保证"抓过页面"，而抓过页面之后
    #       **还得看规则说不说能转**。`opencd_ingest.STATUS_DENIED` 挡的是
    #       **皇后路 ingest 的那一批**；但**第一阶段的行**（qB 本地抓轨）从来
    #       不过 ingest，所以 `DENIED` 对它们**完全没有约束力** —— 这道闸门
    #       就是补那个洞。
    #
    #    ★★★ 2026-09-23 **判据反转**：从「空 ⇒ 放行」改成「**空 ⇒ 拦**」
    #
    #    ★ 起因（实测，用户两次点名）：队列里 **6 条全是禁止转发**
    #      —— 3 条 `中國名家名曲` + 3 条 `周杰伦`（太阳之子/八度空间/Jay）。
    #      而它们 6 条的 `transfer_rule` **全是 NULL** ⇒ 旧的「空 ⇒ 放行」
    #      **把 6 条禁转全放了过去**（只差一步就发出去了）。
    #
    #    ★ 结论：错的不是"漏标了 6 行"，而是**这个默认值本身** ——
    #        `transfer_rule` 空 的语义是「**不知道**能不能转」，**不是**「允许」。
    #        把"不知道"当成"允许" ⇒ 每一次都在赌，而实测**连赌错 6 次**。
    #
    #    ★ 为什么"本地抓轨行"不能作为豁免理由（旧注释的理由已被推翻）：
    #      旧注释写"空 = 没抓过页面（本地抓轨行）⇒ 不拦"，把
    #      **「本地抓轨」与「有权转发」当成了同一件事**。
    #      实测反例：那 6 条禁转**正是**本地/手工加的行（`opencd_tid` 空、
    #      `content_path` 指 `/downloads/…`），而它们**一条都不许发**。
    #      ⇒ 「没有皇后页面」**不等于**「随便转」。
    #
    #    ★ 新判据（**默认拒绝 + 显式授权**）：
    #      · `transfer_rule = 'ALLOW'` ⇒ **唯一**放行的值
    #      · 空 / `DENY` / `NEED_CONFIRM` / 其它任何值 ⇒ **拦**
    #        ★ `NEED_CONFIRM` 现在也**拦**了（它的语义是"拿不准"，
    #          而"拿不准就不发"安全；此前"暂时放行"那条注释已作废）。
    #
    #    ★ 影响面（2026-09-23 实测，逐闸门拆开数）：
    #        ①status=SUCCESS 299 → ①+② site_status 32 → ①+②+③ mapped 6
    #        → ①+②+③+④(旧) **6**  → ①+②+③+④(**新**) **0**
    #      ⇒ 队列 6 → 0：那 6 条禁转**全部被拦住**。
    #      ⇒ ★ **不影响任何已在站上的行**：`site_status='EXISTS'` 的 269 条
    #         本来就不在发布队列里（闸门② 早已把它们排除）。
    #      ⇒ ★ 代价：第一阶段（本地抓轨）今后要发，必须先**显式**标 `ALLOW`。
    #         这是**刻意的**（"默认允许 + 显式禁止"在此场景是反的：禁转是少数，
    #         但"不知道"更常见，所以**授权才该是需要动作的那一方**）。
    #
    #    ⚠️ 已知的**不完整之处**（未修，别当成已解决）：
    #      本闸门只保证"我们**自己**判断过能不能转"，而 `ALLOW` 目前
    #      **只来自皇后页面的自动解析**（+ 手工标注）。
    #      ⇒ 若某个本地行确实有权发，得**人工**标 `ALLOW`（这正是设计意图）。
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


# ★★ 「把 mapped_json 铺回顶层键」这一步**必须只有一份实现**（2026-09-24 从 publish.py 下移）。
#
#   原先它叫 `publish.merge_mapped()`。下移的理由是**新增了第二个发布站**：
#   蟹黄堡的适配层（`crabpt_site.py`）也需要这一步，而它**不能 import publish**
#   （publish 会 import 它，成环）⇒ 各写一份就又是两个口径。
#   ★ 而这一步的 docstring 原话就说了"漏了不报错" —— 正因如此，**更不该有两份**。
#
# ★★ 2026-09-26 补 `small_descr`（`ERR-DATA-01` 最后一环）——
#   原清单只有上面四个键，于是**副标题**这一格漏在外面：
#     · `descr.build_form_fields()` 皇后路读 `opencd_small_descr(task["small_descr"])`
#       = **读列**；而 `tasks.small_descr` 列长期是「全 N 曲」（补页面那步只写了
#       `mapped_json` 没回写列）⇒ 每次 publish/edit 都把「全 N 曲」推上站。
#     实测（本机模拟一条：列='全 15 曲'、`mapped_json.small_descr`='專輯 | VP Records / VPCD 1793'）：
#       `build_form_fields()` 提交的 `small_descr` == `'全 15 曲'`  ★ 错。
#     ★ 症状的误导性：皇后原文**一直躺在库里**，只是从没被读过 ——
#       "抓到了"≠"用上了"，这正是 `ERR-DATA-01` 的形状。
#
# ★ 为什么加进这张表就够（而不是去回写列）：本表的语义是**"渲染输入优先取证据快照"**，
#   `small_descr` 与 `tags`/`medium_sel` 等完全同构（都由 `map_all()` 透传、
#   都存在于 `mapped_json`）⇒ 它本来就**该**在这张表里，缺席是**遗漏**不是设计。
MAPPED_TOP_KEYS = ("tags", "medium_sel", "audiocodec_sel", "team_sel",
                   # ★★ 副标题：`mapped.small_descr` = 皇后 `<div class="smalltitle">` 原文。
                   #    见 `descr.opencd_small_descr()` / `ERR-DATA-01`。
                   "small_descr",
                   # ★★ 简介正文（2026-09-26 加）：`mapped.descr` = 皇后 `divdescr` 原文
                   #    （解析层已剥图，见 `opencd_parse._descr_text_only`）。
                   #    ★ 与 `small_descr` **完全同构**：都由 `map_all()` 透传、
                   #      都存在于 `mapped_json` ⇒ 缺席同样是**遗漏**不是设计。
                   #    ★ 落位：`descr.build_descr()` 里作为正文块（见那里的分流）。
                   "descr",
                   # ★★ 曲目列表（2026-09-26 加）：`mapped.tracklist` = 皇后
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
                   #       ★ 这正是 `ERR-DATA-01`（"同一列被两条语义不同的路写"）的形状，
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
        这正是 `ERR-META-01` 那个 bug 的形状（静默、不报错）。

    ★ 用**快照**而不是当场重算：`TAG_NAME_MAP` / `MEDIUM` 会变（44 项标签表刚改过、
      WAV 编号未定）⇒ 发布要用的就是三天前抓页面时做的那个决定。
    ★ 第一阶段（qB 本地抓轨）的行没有 `mapped_json` ⇒ `_load_mapped` 返回 `{}`，
      这里**什么都不改**，行为与改动前完全一致。
    ★ 解析失败会**抛出**（`_load_mapped` 的契约）—— 响亮地坏，不静默当空。

    ★★ 守卫的**确切语义**（2026-09-27 更正，别按旧文档读）：
      · 快照**有值**（`mapped[key] not in (None, "")`）⇒ 取快照，**盖过列**。
      · 快照**是空**：分两种键，**语义相反**，必须分开处理 —— 见下面的
        `EMPTY_SNAPSHOT_IS_AUTHORITATIVE`。

    ★★★ 为什么旧守卫是**错的**（`ERR-DATA-05`，2026-09-27 实测更正）：
      旧实现一律是 `if key in mapped and mapped[key] not in (None, "")` ⇒
      **快照为空时保留列值**。它的理由是"要区分'皇后本来就没写'与'列里是错值'"。
      ⇒ 但那个理由**不成立**，因为：对**皇后行**（`opencd_tid` 有值），
        `mapped_json` 就是**那次抓页面的权威快照** ⇒ 快照里空 = **皇后当时确实没写**。
        "列里是错值"**正是要修的那件事**，不是要保护的对象。
      ⇒ 旧守卫的实际后果：列里若存着「全 15 曲」这种错值，而皇后**真的没写**副标题，
        `merge_mapped` 会**保留错值**并把它提交上站 —— 也就是把"错值"当成"皇后的意思"
        推出去。那正是 `ERR-META-14` 的形状（空来源不许印成成功）。
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
            #   见上面 `ERR-DATA-05`：快照空 = 皇后没写，是结论不是未知。
            task[key] = val if val is not None else ""
    return task


# ── 「重扫不许覆盖」的 status 集合（★ 唯一来源，别在别处再写一份）──────────
#
# ★★ 起因（实测，2026-09-23）：`scan` 阶段每 30 分钟重扫一遍 qB，而**已发布的
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


# ── ★★★ 闸门④ 的 edit 路副本（2026-09-27 补）────────────────────────────────
# 与 `PENDING_PUBLISH_WHERE` 里那句 `AND transfer_rule = 'ALLOW'` **同一判据**，
# 只是**服务另一条路**：那条路的消费者是 `update_metadata.py`（页面**编辑**），
# 不是 `publish.py`（新种子**发布**）。
#
# ★ 为什么要补（2026-09-27 实测的洞）：
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
#   （`ERR-META-03`：同一件事只能有一处实现；这里"同一件事"= **判据**，不是查询）。
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
        ⇒ 它们既进不了 edit、又在对账里"凭空消失"（ERR-META-10 的形状）。
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
        补闸门时**两边都得改**，只改一边比不改更糟（`ERR-META-03`）。
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
      只给出一个看着合理、其实不同口径的错数字（`ERR-META-03`）。

    ★ 为什么连 `updated_at` 一起取：C' 第 4 步的判据是"**标题没被动**"，
      而 `updated_at` 只能当**旁证**（`scan` 写别的列也会刷它）——
      ⇒ 二者都要印出来，**不许**拿 `updated_at` 代替"标题的值"（`ERR-META-05`）。
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

    ★ 为什么必须有（`ERR-DATA-08`）：
      `CROSS_SITE_DEDUP_WHERE` 上面的注释曾写"所以 `run_crabpt()` 里另有一条
      **可执行**的断言，见那里的 `rule_gaps`（`crabpt_rule_gaps()`）"。
      **实测那两个函数都不存在** ⇒ 那条断言**从没被写出来**，
      "`EXISTS` ⇒ ALLOW"至今只是一句**推理**。⇒ 这里不留同样的洞。

    返回 `(exists_total, queen_total, exists_without_tid)`。
      `exists_without_tid > 0` ⇒ 已上站的行里**混着抓轨路** ⇒
      第 4 步的判据**必须分栏报**（抓轨路本来就会被 `scan` 写），
      不许把两栏合并成一个数（`ERR-META-05`：不同口径不能混减）。
      `== 0` ⇒ 才可以简化成一条判据。

    ★ 三个数**并排返回**，不是只返回差 —— 差是"算出来的"，
      三个原始数才是"读出来的"（`ERR-META-01`：判据要能被人自己复核）。
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
      而手写必踩 `ERR-META-03`：口径与 `PENDING_PUBLISH_WHERE` 漂移后
      **不报错**，只给出一个看着合理、其实不同口径的错数字。
      今晚就是这么被绊住的（现场拼 `python -c` 撞终端换行，拼一次断一次）。
      ⇒ 把"逐级数"做成**一个函数**，口径**直接从 `PENDING_PUBLISH_WHERE` 派生**，
        永久消除"手写一份"这条路。

    ★★ 每一级都是**累加**的（`WHERE` = 前面所有级的 AND 叠加）——
      这样相邻两级的**差**就是"被这一级拦掉多少"，而那正是要看的。
      ⚠️ **别把各级单独的数相减来推结论**（`ERR-META-05`：不同口径不能混减）。
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
    #   与 `g4_allow` **不相等** ⇒ 当场暴露漂移（ERR-META-03 的自动哨兵）。
    out["queue"] = conn.execute(
        f"SELECT count(*) FROM tasks WHERE {PENDING_PUBLISH_WHERE}").fetchone()[0]
    # 闸门② 的分母（有多少行**真的**有发布日期）—— 区分"日期缺"与"日期太新"
    out["added_notnull"] = conn.execute(
        "SELECT count(*) FROM tasks WHERE opencd_added_at IS NOT NULL "
        "AND opencd_added_at != ''").fetchone()[0]
    # ★ 这两个是"数据现状"，不进闸门链，但要和上面分开报（ERR-META-05）
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
      而这里丢的是"这条发到哪个站了"这个**事实**（同 `ERR-META-07`：
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
      （`ERR-SQL-06`：回填一列时绝不能顺带刷 `updated_at`）。
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
         而那种异常是必须看得见的（`ERR-META-14`：兜底值与真实结果要分开报）。
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
      （`ERR-META-14`）。

    ★★ 为什么要有这个窄函数（而不是每次手写一条 `SELECT`）：
      闸门④ 现在有**两处** Python 侧消费者 —— `db.is_transferable_to()`
      （转种解释）与 `db.edit_rule_ok()` 一族（`EDIT_RULE_GUARD`）。
      ★ 手抄 SQL 必然漂移（`ERR-META-03`）⇒ 取数落成**有名字的一处**。

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


# ── ★★★ 跨站去重：这条已经在**别的**站上发过了吗（2026-09-27 用户拍板）────
#
# ★ 用户原话：「需求是【转种】，一条种子不是每个站都要有，是**目标站缺、
#   而源站有**的，才转过去」。⇒ 蟹黄堡的发布队列**不该**由
#   `PENDING_PUBLISH_WHERE` 单独决定 —— 那条 WHERE 里
#   `site_status != 'EXISTS'` 问的是「**乌托邦**发过没有」，
#   而蟹黄堡要问的是「**蟹黄堡**发过没有」，两者**本来就不是同一个问题**。
#
# ★★ 为什么必须写成判据而不是靠 WHERE（这是关键，别简化掉）：
#   `PENDING_PUBLISH_WHERE` 被 `get_pending_publish()` 用，而它的消费者
#   **有两个**：`publish.py` 的**乌托邦**路径（`main()`）与**蟹黄堡**路径
#   （`run_crabpt()`）。★ 两侧需要互斥的集合：
#     · 乌托邦：要 `tasks.site_status != 'EXISTS'`（**乌托邦**还没发）
#     · 蟹黄堡：要 **`tasks.site_status = 'EXISTS'`**（**乌托邦已发**＝有源可转）
#       **且** `site_publish(site='crabpt')` 无记录
#   ⇒ 若为了蟹黄堡去改那句 WHERE，乌托邦队列立刻**全空**（它要靠那句拿新活）。
#     **动的是一条被两个消费者共用的判据 ⇒ 改它 = 静默改掉另一个消费者的行为。**
#   这和 `ERR-META-03` 里"两处各写一份 SQL 必然漂移"是**同一形状的反面**：
#   共写一份**判据**没错，错在**两个消费者要的集合不同** ⇒ 分叉点必须显式落
#   在**消费侧**，而且**判据本身**要有名字（就是本函数），不能内联进 `for`。
#   ★★ 更要命的是：忘记这个筛选**不报错**。去重忘了写，表现是"蟹黄堡照发"，
#      而 crabpt 的 `site_publish` 判重**拦不住** —— 它判的是"**蟹黄堡**发过没有"，
#      对"这条在**乌托邦**发过"一无所知。⇒ Jellyfin 种子里乌托邦已发过的那批
#      会被**重复搬**到蟹黄堡。这是**静默多发**，不是静默少发 —— 多发不可撤销。
#
# ★ 为什么本常量**不**并进 `transfer_rule='ALLOW'`（2026-09-28 订正 —— 原先这段
#   写的是"不需要 ALLOW 白名单，因为能上乌托邦本身就已经过过闸门④"，
#   **那段推理已被实测推翻**，逐条如下）：
#   ① 推理**不成立**：实测 `EXISTS` 的 28 行里**有 1 行 `transfer_rule='DENY'`**
#      （`site_torrent_id=15972`）⇒ "能上乌托邦 ⇒ 已过闸门④"是**假的**。
#      ★ 正是本条注释下面那句"但这是**推理**，不是本常量自己保证的" ——
#        实测证明它连**结论**都不成立。
#   ② 所以**转种路**必须自己把闸门④ 补上 ⇒ 那是**另一个常量**：
#      `TRANSFERABLE_WHERE = 本常量 + " AND transfer_rule = 'ALLOW'"`（定义在下面）。
#   ③ ★★ 为什么**不在这里加**（本次最容易做错的一步）：本常量还有**别的**消费者，
#      其中 `publish.py` 里那条"**应当恒为空**的差集断言"问的正是
#      「已上站 ∧ **非** ALLOW 的有哪些」⇒ 把 ALLOW 并进来会让它**恒返回 0 行**
#      = **检测器静默失效**（`ERR-META-14` 里最贵的一种：比不检测更危险，
#        因为你会以为检查过了）。
#      ⇒ **一条 WHERE 同时服务两个方向相反的问题 ⇒ 必须分叉成两个常量，各司其职。**
#
# ★★★ 2026-09-28 **订正**：这句注释原先写的是"所以 `run_crabpt()` 里另有一条
#   **可执行**的断言，见那里的 `rule_gaps`"（还提到 `crabpt_rule_gaps()`）。
#   **实测：那两个函数在全仓库都不存在**（`grep -rn "def crabpt_rule_gaps"` 无输出，
#   只有上面那行注释在引用它）⇒ **注释承诺了一条判据，而那条判据从没被写出来**
#   （`ERR-META-04` 的形状）。⇒ 本条注释**不再指向一个不存在的函数**。
#   ★ 2026-09-28 追记：那条"应当恒为空"的断言**现在有了** —— 它在 `publish.py`
#     的 `run_crabpt()` 里（查 `CROSS_SITE_DEDUP_WHERE` ∧ 非 ALLOW）。
#     ★★ 它**仍然用本常量**（不是 `TRANSFERABLE_WHERE`）—— 见上面 ③：
#        它就是靠"事实口径 vs ALLOW 要求"的差集来发现漏洞的，
#        换成 `TRANSFERABLE_WHERE` 它就成了恒空废码。
CROSS_SITE_DEDUP_WHERE = (
    "site_status = 'EXISTS' AND site_torrent_id IS NOT NULL AND site_torrent_id != ''"
)

# ── ★★★ C' 的判据口径（2026-09-28）：**皇后路 + 已上站** ────────────────────
#
#   `run.py` 的 C' 改的是"**皇后路**（`opencd_tid` 非空）的行不再由 `scan` 写
#   `formatted_title`"（那一列归 `opencd_ingest.build_row()` = `mapped.title`）。
#   ⇒ 要**验**它，就得能精确取出这批行 —— 这个常量就是这个口径的**唯一真源**。
#
#   ★ 为什么是**新**常量，而不是复用现成的两个（逐条说明，否则会被问"为什么不复用"）：
#     · `_published_where(only_stale=True)` —— **多了 `stale`**。而 C' 的判据是
#       "**不管 stale 与否，这批行的标题都不许变**"（`scan` 根本不挑 stale）
#       ⇒ 带上 `stale` 会**漏掉**"本地没更新过"的行，而那些行正该被判。
#     · `CROSS_SITE_DEDUP_WHERE` —— 只封了 `site_status='EXISTS'` + id 非空，
#       **缺 `opencd_tid` 那一层**（它服务的是"转种"，不是"标题"）⇒ 会把
#       抓轨路的已上站行也捞进来，而那些行**本来就该被 scan 写**（假失败）。
#   ⇒ 两个都不对。而"在调用点当场手拼一条"正是 `ERR-META-03` 的原始动作
#     （口径活在脑子里、漂移了也不报错）⇒ 所以落成常量。
#
#   ★★ 纪律（与 `CROSS_SITE_DEDUP_WHERE` 上面那段同源）：`EXISTS` ⇒ 皇后路
#     **是推理、不是约束** —— 数据库**不保证**"已上站的行都带 `opencd_tid`"。
#     ⇒ 消费方**不许**假定"取出来的必然都是皇后路"，必须**先数一遍**
#       `EXISTS` 里 `opencd_tid` 为空的有几条并报出来（见 `queen_published_gap()`）。
#
#   ★ 与 `CROSS_SITE_DEDUP_WHERE` 的**共同前缀**由 `run.py --selftest` ⑦e 钉住：
#     两者要么共用一份，要么被一条判据钉住 —— "两处各写一份、谁也不看着谁"
#     是不允许的（`ERR-META-03`）。
QUEEN_PUBLISHED_WHERE = (
    "site_status = 'EXISTS' AND site_torrent_id IS NOT NULL AND site_torrent_id != ''"
    " AND opencd_tid IS NOT NULL AND opencd_tid != ''"
)

# ── ★★★ 转种口径（2026-09-28）：**已上站 ∧ 过闸门④** ──────────────────────
#
#   ★★★ 为什么必须**另立**常量，而不是把 `transfer_rule` 那条加进
#     `CROSS_SITE_DEDUP_WHERE`（这是本次改动的**全部要点**，别简化掉）：
#
#     ① `CROSS_SITE_DEDUP_WHERE` **不**是转种专用的，它还有别处消费者；
#        最要紧的一处是 `publish.py` 里那条"**应当恒为空**的差集断言"
#        —— 它问的正是「已上站、却没标 ALLOW 的有哪些」：
#            SELECT ... WHERE {CROSS_SITE_DEDUP_WHERE}
#                       AND (transfer_rule IS NULL OR transfer_rule != 'ALLOW')
#     ② ⇒ 若把 `transfer_rule='ALLOW'` 并进 `CROSS_SITE_DEDUP_WHERE`，
#        那条断言**恒返回 0 行**：它想找的就是"非 ALLOW 的已上站行"，
#        而那批行**恰好**被合并后的 WHERE 排除掉了。
#        **断言不会报错，它会永远显示"通过"** —— 这是 `ERR-META-14` 里最贵的
#        一种：检测器本身静默失效，比不检测更危险（你会以为检查过了）。
#     ③ 所以两层**各司其职**：
#          · `CROSS_SITE_DEDUP_WHERE` —— 保持原样（"乌托邦上有"这个**事实**），
#            供差集断言与 `queen_published_gap()` 等使用；
#          · `TRANSFERABLE_WHERE`（本常量）—— "**有源 ∧ 允许转**"这个**判据**，
#            供转种路使用。
#        ★ 多出来的一层是 `transfer_rule` ⇒ 两者必须**逐字共享前缀**，
#          由 `run.py --selftest` ⑦e 钉住（与 `QUEEN_PUBLISHED_WHERE` 同款纪律：
#          "两处各写一份、谁也不看着谁" = `ERR-META-03`）。
#
#   ★★★ 为什么这条修法是**必要**的（2026-09-28 实测的洞，不是假设）：
#     `transferable_to()` 原先只按 `CROSS_SITE_DEDUP_WHERE` 筛 ⇒ 它筛的是
#     **"乌托邦上有没有"**，而**不是**"允不允许转"。
#     实测：`EXISTS` 的 28 行里有 **27 ALLOW / 1 DENY**，那条 DENY 是
#     `site_torrent_id=15972`（`opencd_tid='200002'`）⇒ **它会进转种队列，
#     会被搬到蟹黄堡**，而 `transfer_rule='DENY'` 的语义正是"**不许再分发**"。
#     ⇒ 而**转种不可逆**（蟹黄堡没有批量删种）⇒ 这是个"一旦跑到就无法收拾"的洞。
#
#   ★ 为什么**不是**"反正 `publish.py` 里有断言会报出来"就够了：
#     那条断言是 `print` **非阻断**的（注释自己写了"拦住会把整批卡住"）
#     ⇒ 它把安全**寄托在"有人会看日志"**上。而 `run_crabpt` 是**无人值守路径**
#       —— 一条警告会淹没在整轮日志里。★ 安全判据不能靠"读日志的人"。
#
#   ★★ 为什么**不**顺手把 `run_crabpt` 那条 `_gaps` 断言删掉：
#     改完之后队列里**不可能**再有非 ALLOW 的行 ⇒ 那条断言**恒空**，
#     看起来成了"废码"。★ 但它是**故意的**：它现在是一条**回归探针**
#     —— 若将来有人再让某行绕过闸门④ 上站（手工 `set_site_id`、或新写一条
#       不查闸门的路径），差集**会重新变非空**，而它**正好**指着那句话。
#     删掉它 = 把"将来会坏的那一天"变成静默。⇒ 断言留着，且**不该**跟着改口径
#     （它就是"拿事实口径问一个应当恒为空的问题"，改了口径它就没用了）。
#
#   ★★★ 为什么这里**把前缀字面值抄了一遍**（看起来是"与 `CROSS_SITE_DEDUP_WHERE`
#     重复"，而本项目的纪律一向是"不许两处各写一份"）—— 这是**刻意的重复**，
#     理由是一条**可执行的判据**，不是懒：
#
#     · 本仓库有 **4 处工具**靠「AST 抽取 `db.py` 里的常量字符串」拿到这些口径
#       （`tools/fix_onsite_titles_and_subtitles.py`、`tools/fix_no_torrent_file_status.py`、
#        `tools/_probe_a3_risk.py`、`tools/_probe_280_vs_16.py`）。
#     · 它们**不能** `import db` —— 那会连 `config` 一起拉，而 `config` 在 import 期
#       就 `require_env()`（`QB_PASS` 等）⇒ 没有 `.env` 的机器上**连工具都起不来**。
#     · 而 `ast.literal_eval` **不认 `Name` 节点** ⇒ 若本常量写成
#       `CROSS_SITE_DEDUP_WHERE + " AND transfer_rule = 'ALLOW'"`，那 4 处工具会
#       **在运行时**才 `ValueError`（不是在构建期）—— 而它们正是用来"半夜在 NAS 上
#       查一个紧急问题"的。
#     · ⇒ 所以 `run.py --selftest` ⑦e-3 **规定**：这几个常量必须能被
#       `literal_eval` 直接解出。**本处的重复就是为满足那条判据。**
#     ★ 重复带来的漂移风险由 ⑦e 兜住：它断言本常量必须以 `CROSS_SITE_DEDUP_WHERE`
#       **逐字开头** ⇒ 前缀若改了、这里没跟着改，`--selftest` **当场红**。
#       判据 > 记忆：重复一段**被钉住**的字符串，比"省略掉但会让 4 个工具静默坏掉"安全。
TRANSFERABLE_WHERE = (
    "site_status = 'EXISTS' AND site_torrent_id IS NOT NULL AND site_torrent_id != ''"
    " AND transfer_rule = 'ALLOW'"
)

# ── ★★★ 蟹黄堡**发布**队列口径（2026-10-04 加，#23）──────────────────────────
#
#   ★ 为什么必须**另立**常量、不能直接改 `TRANSFERABLE_WHERE`：
#     `run.py --selftest` ⑦e-2 把 `TRANSFERABLE_WHERE` 的尾巴**逐字**钉成
#     `"AND transfer_rule = 'ALLOW'"` —— 往里追加 `status != 'FAILED'` 会**当场红**。
#     （⑦e-2 不是刁难：它防的是"有人在闸门④ 那一层偷偷加条件而不报"。）
#   ⇒ 于是按本仓库既有的"分叉常量"手法（同 `QUEEN_PUBLISHED_WHERE`）另立一份。
#
#   ★★ 为什么**不**复用 `status` 的语义、而要新口径（这才是本常量的存在理由）：
#     乌托邦的发布队列 `PENDING_PUBLISH_WHERE` **首条件就是 `status='SUCCESS'`**
#     ⇒ 一旦某条被标 `status='FAILED'`，它**立刻出队**（这是 #21 的关键机制）。
#     但 `TRANSFERABLE_WHERE` **完全不看 `status`** ⇒ 对蟹黄堡"标 FAILED 出队"
#     **不成立**（标了它下一轮还在队首，`ORDER BY updated_at` 又让它**总被先选中**
#     ⇒ 一条坏种子把整条转种队列**永久卡住**，`run_crabpt` 每周期重试同一条）。
#     ⇒ `CRABPT_QUEUE_WHERE = TRANSFERABLE_WHERE + " AND (status IS NULL OR status != 'FAILED')"`，
#       让"被确证拒掉的种子"从**发布**队列消失（但它仍是 tasks 里的行，可查、可人工翻案）。
#
#   ★★ 为什么是 `(status IS NULL OR status != 'FAILED')` 而不是更短的
#     `status != 'FAILED'`：**SQL 的三值逻辑**——`NULL != 'FAILED'` 求值为
#     `NULL`（不是 `TRUE`）⇒ 会把 `status` 为空的行**一并排掉**。而"空 status"
#     的语义是"还没标过"（**不是**"被拒了"）⇒ 必须与 Python 侧
#     `(status or '').strip() == 'FAILED'`（`is_transferable_to` 里那条）**等价**。
#     ★ 这不是理论洁癖：`selftest_crabpt_explain_same_source` 的 fixture 行**没有**
#       `status` 列值 ⇒ 写成 `status != 'FAILED'` 会让队列排除它、而解释口径放行它
#       ——两条判据对同一行给出相反答案，**正是那条自检要防的形状**（实测当场报红）。
#   ★ 逐字写全（**不**引用 `TRANSFERABLE_WHERE`）—— 理由同上面那段：4 处工具靠
#     `ast.literal_eval` 静态抽常量（`run.py` ⑦e-3）；引用拼接会让它们在**运行时**才炸。
#     ★ 前缀漂移风险由 ⑦e 兜住：它断言本常量必须以 `TRANSFERABLE_WHERE` **逐字开头**。
#   ★★ 与 `is_transferable_to()` **必须同源**（选行 / 计数 / 给人看的解释三者口径一致）——
#     否则重现 2026-09-28 那次"工具说在队列里、实际不会发"的分叉事故（`selftest_crabpt_*`）。
CRABPT_QUEUE_WHERE = (
    "site_status = 'EXISTS' AND site_torrent_id IS NOT NULL AND site_torrent_id != ''"
    " AND transfer_rule = 'ALLOW'"
    " AND (status IS NULL OR status != 'FAILED')"
)

# ── ★★★ 蟹黄堡**直发**口径（2026-10-20 用户拍板）──────────────────────────────
#
#   用户原话：「**蟹黄堡跟乌托邦没有逻辑关系**，完整语义是：皇后里只要**不是禁止转载**，
#   **有无 log 都可以发**。」
#
#   ★★ 原口径错在哪（不是"少一条"，是**用错了列**）：
#     `CRABPT_QUEUE_WHERE` 以 `site_status = 'EXISTS'` 开头 —— 而 `site_status` 是
#     **乌托邦那一侧的旧镜像列**（`site_publish` 里 `site='musopia'` 那条）。
#     ⇒ 它表达的是「**乌托邦已经发过**」，而蟹黄堡要问的是「**蟹黄堡自己发过没有**」。
#     ⇒ 后果（2026-10-20 实测）：乌托邦被 log 闸门卡掉 65% ⇒ 蟹黄堡**被连坐**，
#       日发量掉到 0，而适配层（`crabpt_site.py:788` 必填项不含 log）**本来就允许无 log**。
#
#   ★ 为什么**另立**常量、而不是改 `CRABPT_QUEUE_WHERE` 那一行：
#     ① `run.py` ⑦e 把 `CRABPT_QUEUE_WHERE` 钉成"必须以 `TRANSFERABLE_WHERE` 逐字开头、
#        尾巴只准谈 `status`" —— 改它**当场红**；
#     ② `CRABPT_QUEUE_WHERE` 还被 `publish.py:2471/2628` 当"出队判据"的口径引用，
#        且 `transferable_to()` / `count_transferable_to()` 仍是**乌托邦→蟹黄堡**那条
#        旧路的选行函数（保留不动，将来若要做"真·转种"还用得上）。
#     ⇒ 与 `QUEEN_PUBLISHED_WHERE` / `TRANSFERABLE_WHERE` 一样，按本仓库既有的
#       "**分叉常量**"手法另立一份。
#
#   ★ 为什么**写全字面值**（不像别处那样引用前缀常量拼接）：
#     本仓库有 4 处工具（`tools/fix_onsite_titles_and_subtitles.py` 等）靠
#     `ast.literal_eval` 静态抽 `db.py` 的常量 —— 它们**不能** `import db`（会连
#     `config` 一起拉、触发 import 期的 `require_env()`）。`literal_eval` 不认 `Name`
#     节点 ⇒ 引用拼接会让它们在**运行时**才炸。本常量眼下**不在** ⑦e-3 的清单里，
#     但仍写全，免得将来有人加进清单时踩雷。
#
#   ★ 保留 `status != 'FAILED'`（闸门⑤）—— 理由与 `CRABPT_QUEUE_WHERE` 那段**逐字相同**：
#     SQL 三值逻辑下 `NULL != 'FAILED'` 求值为 `NULL` ⇒ 必须显式写
#     `(status IS NULL OR status != 'FAILED')`，否则"还没标过 status"的行会被**静默排掉**。
#
#   ★★ 本常量**不管**"蟹黄堡发过没有" —— 那一层在 `pending_crabpt_direct()` 的
#     `site_publish` 子查询里（按站去重）。两个口径**必须**分开写，理由见该函数。
CRABPT_DIRECT_WHERE = (
    "transfer_rule = 'ALLOW'"
    " AND (status IS NULL OR status != 'FAILED')"
)


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
    if (get_status(info_hash) or "").strip() == "FAILED":
        return False, ("本地已把这条标 `status='FAILED'`（发布被站点明确拒）⇒ "
                       "已从直发队列出队（#23）；如需重发先人工把它改回")
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
      ★ 危害形状（`ERR-META-14`）：不是"多发一条"（数据风险），而是
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
             各写一份判据（`ERR-META-03` 的漂移形状）。
      ② ★ **过闸门④**：`transfer_rule == 'ALLOW'`（只有 `ALLOW` 放行；
         空 / `NEED_CONFIRM` / `DENY` **一律拦**）。
         ★★ 这一条是 2026-09-28 补的 —— 本函数**原先没有它**，于是它会对
           `15972`（`EXISTS` + `DENY`）回答"该转"，而那条**明明被闸门④ 拦着**。
         ★ 它必须与 `TRANSFERABLE_WHERE` 的尾巴**语义**一致（逐字由
           `run.py --selftest` ⑦e-2 钉住）；★ 注意**不许**写成
           `!= 'DENY'` —— 那会把空规则/`NEED_CONFIRM` 判成**放行**，
           与闸门④ **语义相反**（`ERR-META-30` 的形状："提到了对的词、
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
    #   ★ 但**报文**必须区分（`ERR-META-14`：兜底值不许和真实结果混着报）。
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
    #     成 `''` 好在报文里说话（`ERR-META-14`：兜底值不许冒充真结果 ——
    #     所以 `None` 报成 `''` 之后还要**说清**"空规则也拦"）。
    if (rule or "").strip() != "ALLOW":
        # ★ 报文分档：`DENY` 是"上游明令不许"（要人看一眼），
        #   空 / 其它是"没标过 ALLOW"（规则缺失）。两者**拦法相同、含义不同**
        #   ⇒ 不许压成一句（`ERR-SITE-06` 的形状：报错文案把多种原因
        #     压成一句，读的人只能猜）。
        if rule == "DENY":
            return False, ("transfer_rule='DENY' ⇒ 闸门④ 拦下（只有 'ALLOW' 放行）"
                           "—— 上游明令**不许再分发**")
        return False, (f"transfer_rule={rule!r} ⇒ 闸门④ 拦下（只有 'ALLOW' 放行）"
                       f"{'（规则为空：没标过）' if not (rule or '').strip() else ''}")
    # ★★★ 闸门⑤（`CRABPT_QUEUE_WHERE` 的尾巴，2026-10-04 #23）——
    #   ★ 与 `transferable_to()` 的 SQL 里 `AND (status IS NULL OR status != 'FAILED')`
    #     **等价**：`get_status()` 对**不存在/NULL** 返 `None` ⇒ `None or ""` ⇒ `""`
    #     ⇒ `!= "FAILED"` ⇒ **放行**，与 SQL 的 `status IS NULL` 那一支**同判**；
    #     `""` / `"SUCCESS"` / `"PUBLISHED"` ⇒ 放行；只有**恰好** `"FAILED"` ⇒ 拦。
    #     ★ 归一化（`.strip()`）是为了与 SQL 里那种"列可能带空格"的写坏数据一致 ——
    #       两站同一个纪律（见 `transfer_rule` 那段"归一化必须在一处"的说明）。
    #   ★ 为什么它必须在这里也判：本函数是"给人看的解释"，**必须**与实际选行
    #     同源 —— 否则会出现"解释器说在队列里、队列实际不选它"的分叉
    #     （2026-09-28 事故，见本函数 docstring 开头）。
    #   ★ 报文讲清"是被发布拒了、不是不能转"（`ERR-META-14`：别让两种原因
    #     压成一句）—— `FAILED` 是**本地的发布结果**，与上游的转载规则无关。
    if (get_status(info_hash) or "").strip() == "FAILED":
        return False, ("本地已把这条标 `status='FAILED'`（发布被站点明确拒）⇒ "
                       "已从蟹黄堡发布队列出队（#23）；如需重发先人工把它改回")
    dst_status = site_status_of(info_hash, site)
    if dst_status is not None:
        # ★★ 这里**必须**区分"记录在但 id 空"（写坏了）与"正常已发"，
        #   否则一条写坏的记录会被当成"已发"而**永远拦着**这条种子
        #   （`ERR-META-14` 的经典形状：兜底值与真实结果混在一起）。
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
      而"再查一次"就会出现**两份 WHERE**（`ERR-META-03`）。

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
        那正是 `ERR-META-03` 的形状。⇒ 一律引用常量。
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
      而"再查一次"就会出现**两份 WHERE**（`ERR-META-03`）。

    ★★ 排序：`updated_at` 升序（老的最先）—— 与 `get_published()` / `transferable_to()`
      **同口径**。★ 这条排序与 `status != 'FAILED'` 的组合才是 #23 的要点：没有
      `status` 这层，一条坏种子会**恒在队首**而被每周期重试。

    ★★★ 去重**只走** `site_publish(site=?)`，**不许**用 `tasks.site_status`：
      `site_status` 只记乌托邦（见上）。用错列 ⇒ 要么漏发（乌托邦没有就永远不发），
      要么**静默多发**（蟹黄堡已发过的被当成没发过 ⇒ 重复上站，**不可撤销**）。

    ★ 为什么 WHERE 引用常量而不是手写：手抄必然与常量漂移（`ERR-META-03`）。
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
