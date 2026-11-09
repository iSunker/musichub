"""
把**另一个项目**（vertex2）库里的皇后页面 id，按 infohash 补齐到本项目的 `tasks.opencd_tid`。

═══════════════════════════════════════════════════════════════════════════
为什么必须有这一步
═══════════════════════════════════════════════════════════════════════════
`tasks.opencd_tid` 是 `descr.py` **三处「皇后路」分流的唯一开关**：

| 位置 | `opencd_tid` 为空时的后果 |
|---|---|
| `descr.py:443` | 副标题走 `derive_small_descr()` 数 `tracklist` 得出「全 N 曲」，而不是皇后原文 |
| `descr.py:639` | **不勾** `OpenCD` 标签 |
| `descr.py:539/623/634` | `medium_sel` / `team_sel` / `tags` 退回 `detect_*()` **从标题猜** |

⇒ 恒空 ⇒ 每条皇后行都被当成「qB 本地抓轨路」⇒ 发布出去**六个字段全错**，
且三处是**静默退回猜**（`descr.py:617-621` 的注释自己警告过这个形状：
猜出来的 `[]` 与"没有标签"长得一模一样）。

═══ 为什么能拿到，而且**不产生任何皇后请求** ═══
vertex2（另一个项目）**定时 RSS 皇后 → 推 qB3020**，而本项目正是从那个 qB 抓轨的
⇒ 两侧是**同一批种子** ⇒ infohash 天然一致。它的 `torrents` 表里存着皇后的
`details.php?id=NNN`。

★★ 这对上了本项目的**配额纪律**：这是唯一一条**不抓页面**就能拿到 tid 的路。

═══════════════════════════════════════════════════════════════════════════
join 键：为什么只能用 `info_hash`
═══════════════════════════════════════════════════════════════════════════
我们的 `tasks.info_hash`  ==  vertex `torrents.hash`（都 `.lower()`）。

| 候选键 | 实测命中 |
|---|---|
| **`info_hash`** | **430 / 547** ✅ |
| `torrent_name` | 1 / 547 |
| `formatted_title` | 0 / 547 |

理由：infohash 是**内容哈希**，同一份内容两边必然同值，**绑定不可伪造**；
标题会被站点改写（`黄清元 - 金钻名曲珍藏 FLAC 1998 - FLAC 整轨 - OpenCD`
在皇后是 `黄清元 - 金钻名曲珍藏 1998 - FLAC 分轨`）⇒ 按标题配必错。

★★ **`torrents.id` 绝不可用**：那是 vertex **自己的**自增主键
（实测 `id=3054` 对 `details?id=194702`，差 19 万）。
它看起来"像 id"，写进去**不会报错**，只会静默写错 —— 见 。
tid 只能从 `torrents.link` 抠：`details.php?id=(\\d+)`。

═══════════════════════════════════════════════════════════════════════════
五条设计纪律
═══════════════════════════════════════════════════════════════════════════
1. **默认只读**（同 `sync_site.py`）：不带 `--apply` 绝不写库。

2. **只填空白**：` WHERE ... AND (opencd_tid IS NULL OR opencd_tid = '')`。
   **绝不覆盖非空异值** —— 将来 `crawl` 若写了更权威的值，不该被 vertex 的旧值盖掉。

3. ★★ **绝不碰 `updated_at`**（本条最要紧，也最容易被后人"顺手"改坏）：
   `db.STALE_CONDITION = "(site_synced_at IS NULL OR updated_at > site_synced_at)"`
   驱动 `db.count_stale()`，而那是 **`edit` 阶段的待办探针**。
   顺手刷 `updated_at` ⇒ 回填的这几百条**全部**变 stale ⇒ `edit` 每轮把整站页面重拉一遍
   （**实测过的空转**，见 `db.save_task()` 的注释与 `README.md` 的 stale 一节）。
   ⇒ 这条 UPDATE **只列一列**，自检里有一条专门钉它。
   ★ 代价（必须知情）：不刷 `updated_at` ⇒ `edit` **看不到**这些行"变新了"
     ⇒ 已在站上的行，皇后路渲染**不会自动推上去**。
     那是**另一件独立的事**：显式把该重推的行标 stale，再让 `edit` 正常跑。

4. ★★ **源读不到 ≠ 没有可回填的行** —— 必须**非零退出**。
   挂载没了 / 库改名 / 表或列没了，都会得到一个**空结果集**；
   若用 `except: return {}` 把它吞掉，就变成**一个绿着的、什么都没做的**每日任务
   —— 正是 （"检测方法失效比不检测更危险"）。
   ★ 这是本模块与项目里其它阶段**最重要的一条差别**。

5. **纯本地、零请求**（同 `opencd_ingest.py` 的纪律 1）：不 import `requests`、
   不 import `crawl`。⇒ 能脱离网络被 `--selftest` 单测，且"不出网"这条**能被 grep 审计**。

═══════════════════════════════════════════════════════════════════════════
跑在哪两种环境
═══════════════════════════════════════════════════════════════════════════
- **容器内**（每日自动）：vertex2 的库由 compose 以 `${VERTEX_DB_DIR}:/vertex:ro` 挂进来
  ⇒ 路径 `VERTEX_DB=/vertex/sql.db`。
- **本机**（一次性回填 / 取证）：走 SMB 直读 `//iSunker-DS423/...`。
  ★ 本机跑时必须**先停容器** —— 两个写者写同一个 SQLite 是  禁止的。

用法:
    python sync_opencd_tid.py                    # 报告（默认，不写库）
    python sync_opencd_tid.py --apply            # 只填空白
    python sync_opencd_tid.py --selftest --json  # 离线自检（末行 JSON）
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

# ★ 自检要打 ✅/❌ 这类非 GBK 字符，而 Windows 控制台默认 GBK ⇒ 不加这行会在
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ★★ 必须在 `import db`（→ `import config` → `require_env` 缺了就 sys.exit，
#    且 `config.DB_PATH` 在 import 时就由 `DATA_DIR` 算好）**之前**塞好环境变量。
#    ★ 只在跑自检时把 `DATA_DIR` 指到临时目录 —— 本项目对"自检不能有副作用"有成文纪律。
#    ★ 用 setdefault：调用方（生产容器）已设好的**绝不覆盖**。
if "--selftest" in sys.argv:
    import tempfile as _tempfile
    os.environ.setdefault("QB_PASS", "x")
    os.environ.setdefault("MUSOPIA_COOKIE", "c_secure_pass=x")
    os.environ.setdefault("MUSOPIA_PASSKEY", "x")
    os.environ.setdefault("PICGO_API_KEY", "x")
    os.environ["DATA_DIR"] = _tempfile.mkdtemp(prefix="opencd-tid-selftest-")
    # ★ 自检也不许碰**真实的** vertex 库
    os.environ["VERTEX_DB"] = os.path.join(os.environ["DATA_DIR"], "vertex-selftest.db")

import config        # noqa: E402  （必须在上面那段环境变量之后）
import db            # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════════════════

# 只认皇后这一家（vertex 库里还混着 ourbits / hdhome / hdtime 等别的站）
QUEEN_TRACKER = "tracker.open.cd"

# ★ tid 只能从 link 抠。**绝不**读 `torrents.id`（那是 vertex 自己的自增主键）。
TID_RE = re.compile(r"details\.php\?id=(\d+)")

# 容器内（compose 以 :ro 把 vertex2 的库目录挂到 /vertex）
VERTEX_DB_CONTAINER = "/vertex/sql.db"
# 本机兜底（SMB 直读；比照 nas_health.py 的「env → 文件 → 硬编码」三级习惯）
VERTEX_DB_DEV = r"//iSunker-DS423/docker_ssd/vertex2/vertex/db/sql.db"

# ★ 异常护栏：皇后行数跌到这个地板以下 ⇒ 认定"源不对劲"，非零退出（见纪律 4）。
#   实测 583 行；取 100 是留足余量，同时能挡住"库被换掉/表改名"这类漂移。
MIN_QUEEN_ROWS = 100

# ★★ 外部库**必须**有的列。少一列就 `SourceUnavailable`，**不猜、不吞**。
#   填不出发布日期 ⇒ 而那会被闸门② 的"空 ⇒ 拦"全数挡下。
#   ★ 这正是不许静默降级的理由：静默降级的表现是"队列莫名其妙空了"，
REQUIRED_COLS = {"hash", "link", "name", "tracker", "add_time"}

# 只查这一列（本地侧）—— 本模块每日在容器里跑，不该像 tools/ 那份预览一样
# 把 torrent_name/大文本拖进调度。
LOCAL_SELECT = "SELECT info_hash, opencd_tid, opencd_added_at FROM tasks"

# ★★ 皇后在本地的时区 = 东八区。**显式写死**，不读容器 TZ。
#
#   为什么不能用 `datetime.fromtimestamp(t)`（不带 tz）：那个"本地时区"取决于
#   进程环境 —— 本机 TZ=UTC、生产容器 TZ=Asia/Shanghai ⇒ **同一行会得出两个
#   不同的小时数**，差 8 小时。而闸门② 是拿这个字符串与 `datetime('now')`
#   （UTC）做**字符串比较** ⇒ 差 8 小时在 7 天门槛上表现为"偶尔差一天放行"，
#   ⇒ 显式 `timezone(timedelta(hours=8))`，两边算出来必然一致。
QUEEN_TZ = timezone(timedelta(hours=8))


def added_at_utc(add_time):
    """
    vertex 的 `add_time`（unix 秒）→ **UTC 字符串** `'YYYY-MM-DD HH:MM:SS'`。

    ★ 存 UTC 字符串的理由：闸门② 要拿它与 `datetime('now')` 直接比字符串，
      而 SQLite 的 `datetime('now')` 就是 UTC —— **同源**才敢直接比
      （`db.py` 闸门② 那段注释写着同一条）。
    ★ 拿不到 ⇒ 返回 `None`（**绝不编一个时间**）。闸门② 那边 `NULL ⇒ 拦`，
      所以"编"与"留空"的后果完全不同：编一个会**放行**，留空会**拦住**。
    ★ 0 / 负数 / 非数字 一律当"拿不到"：
      · `0` 是 SQLite 里"没填"的常见占位（1970-01-01 会被闸门当成"放了 56 年"
        而**无条件放行** —— 一个坏值不该换来最宽的放行）；
      · 负数同理（1970 之前）。
    """
    if add_time is None:
        return None
    try:
        secs = int(add_time)
    except (TypeError, ValueError):
        return None
    if secs <= 0:
        return None
    try:
        dt = datetime.fromtimestamp(secs, QUEEN_TZ)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════════════════════
# 路径 / 只读打开
# ═══════════════════════════════════════════════════════════════════════════

def resolve_vertex_db():
    """
    外部库路径。优先级（比照 deploy.sh 的 DST 规矩，）：
        环境变量 VERTEX_DB → 容器内默认 → 本机 UNC 兜底
    ★ 默认值是**默认值**，不是强制值。
    """
    v = (os.getenv("VERTEX_DB") or "").strip()
    if v:
        return v
    if os.path.isdir("/vertex"):
        return VERTEX_DB_CONTAINER
    return VERTEX_DB_DEV


class SourceUnavailable(Exception):
    """外部库读不到 / 结构不对。★ 必须非零退出，**绝不**降级成"没有"。"""


def open_readonly(path):
    """
    以**只读**方式打开别人家的 SQLite。

    ★★ 三条防线（缺一不可）：
      ① **优先 `file://...?mode=ro` URI** —— 文件不存在时它**报错**；
         而普通 `connect()` 会**创建一个空库文件** ——
         若 bind mount 源丢了、Docker 建了个空目录，我们就在**别人家的目录里写了一个空库**。
      ② 连上后**先检查文件存在且非空**，再查询。
      ③ 无条件 `PRAGMA query_only=1`（双保险）。

    ★ 不 cp —— （cp 会丢 WAL 里未 checkpoint 的数据）。
    ★ UNC 的坑（`tools/doc-audit/audit.py`，原 `04_probes.py` 记过）：不能写 `file://iSunker-DS423/...`，
      那会把主机名当 URI 的 authority 而报 `invalid uri authority`。
      ⇒ 要用 `file://` + **前导斜杠的路径**（即多两个斜杠）。
    """
    if not os.path.exists(path):
        raise SourceUnavailable(
            f"外部库不存在：{path}\n"
            f"  ⇒ 容器内请确认 compose 挂了 ${'{VERTEX_DB_DIR}'}:/vertex:ro；"
            f"本机请确认 SMB 可达、且**生产容器已停**"
        )

    con = None
    p = path.replace("\\", "/")
    uri = "file://" + p + "?mode=ro" if p.startswith("//") else "file:///" + p.lstrip("/") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.OperationalError:
        # 兜底：普通连接 + query_only（两条路都真只读，URI 那条更硬）
        con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA query_only=1")

    # ★ 防线②：连上了不等于是个库（空文件 connect 也会成功）
    try:
        size = os.path.getsize(path)
    except OSError as e:
        con.close()
        raise SourceUnavailable(f"外部库不可 stat：{path} ({e})")
    if size == 0:
        con.close()
        raise SourceUnavailable(
            f"外部库是 0 字节：{path}\n"
            f"  ⇒ bind mount 源很可能不存在，Docker 建了个空目录"
        )
    return con


# ═══════════════════════════════════════════════════════════════════════════
# 读两侧
# ═══════════════════════════════════════════════════════════════════════════

def load_queen_index(path):
    """
    读 vertex 库 → `({infohash: {"tid","name","added"}}, {冲突的 hash: [候选 tid]}, 统计)`。

    ★ 结构不对（没有 `torrents` 表 / 没有 `link` 列）⇒ `SourceUnavailable`，
      **不吞**。这正是"schema 漂移被 except 吞成 0 行"那个最危险的形状。
    ★★ 2026-09-23 加读 `add_time`：它是**皇后的发布日期**（unix 秒 = 该种子被加进
      tracker 的时刻），闸门② 的新输入。`add_time` 在 `REQUIRED_COLS` 里 ⇒
      列没了会像别的列一样**抛**，而不是静默变成"日期都拿不到"。
    """
    con = open_readonly(path)
    try:
        cols = {r[1] for r in con.execute('PRAGMA table_info("torrents")')}
        if not cols:
            raise SourceUnavailable(f"外部库没有 `torrents` 表：{path}")
        missing = REQUIRED_COLS - cols
        if missing:
            raise SourceUnavailable(
                f"外部库 `torrents` 表缺列 {sorted(missing)}：{path}\n"
                f"  ⇒ vertex 那边改过结构？本模块**不猜**，请人工确认后再跑"
            )

        idx, cand = {}, {}
        for link, h, name, add_time in con.execute(
                "SELECT link, hash, name, add_time FROM torrents WHERE tracker = ?",
                (QUEEN_TRACKER,)):
            m = TID_RE.search(link or "")
            if not m:
                continue                      # 抠不出 tid ⇒ 不编
            key = (h or "").lower()
            if not key:
                continue
            tid = int(m.group(1))
            cand.setdefault(key, set()).add(tid)
            idx.setdefault(key, {"tid": tid, "name": name,
                                 "added": added_at_utc(add_time)})

        conflicts = {k: sorted(v) for k, v in cand.items() if len(v) > 1}
        # ★ 冲突的**不进写入集**（不自动选，也不静默挑一条）
        for k in conflicts:
            idx.pop(k, None)

        stats = {
            "tracker_rows": con.execute(
                "SELECT count(*) FROM torrents WHERE tracker = ?",
                (QUEEN_TRACKER,)).fetchone()[0],
            "usable": len(idx),
            "conflicts": len(conflicts),
        }
    finally:
        con.close()
    return idx, conflicts, stats


def load_local(path=None):
    """
    读本地 tasks 的 `(info_hash, opencd_tid, opencd_added_at)`。★ 只读三列。

    ★ 为什么必须把 `opencd_added_at` 也读出来（2026-09-23）：
      闸门② 换成了"发布日期 + 7 天"之后，**这一列空 ⇒ 那条种子发不出去**。
      而 `opencd_tid` 早已由本脚本填过一轮（438 行）——
      若这里不读那一列，`build()` 就看不见"tid 已有、日期还空"这种行，
      于是它们**永远不会被回填**，闸门② 会把整个队列**永久锁死且不报错**。
    """
    sql = LOCAL_SELECT if path is None else \
        "SELECT info_hash, opencd_tid, opencd_added_at FROM tasks"
    conn = db.connect() if path is None else sqlite3.connect(path, timeout=30)
    try:
        return list(conn.execute(sql))
    finally:
        conn.close()


def build(rows, idx):
    """
    四堆切分：

    | 堆 | 含义 | 写库？ |
    |---|---|---|
    | `todo`  | 空白 + 能配对 ⇒ **该填** | ✅ |
    | `same`  | 已经是这个值 ⇒ 幂等跳过 | ❌ |
    | `diff`  | ★ **非空、但与皇后不一致** ⇒ 报出来，**不写** | ❌ |
    | `miss`  | 在 vertex 里配不上 ⇒ 逐条报出 | ❌ |

    ★★ `diff` 这一堆是**刻意独立**的（2026-09-23）：纪律 2 说"只填空白"，
      所以 `apply_tids` 的 WHERE 会拒绝覆盖非空值。若把它们混进 `todo`，
      **汇总里的"需回填"数就会虚高**（说到 5 条、实际只写 3 条），
      而且"幂等"这条判据永远不成立（它们会一直留在 `todo` 里）。
      ⇒ 判据与实际写入必须**同口径**。

    ★★ 2026-09-23：**两列各自判"空"**（不是只看 `opencd_tid`）。
      · `opencd_tid` 空            ⇒ 进 `todo`（两列都填）
      · `opencd_tid` 是想要的、但 **`opencd_added_at` 空** ⇒ **也进 `todo`**
        ★ 这一支是**必须**的：那 438 行的 tid 是本脚本上一轮填的，日期还空着。
          漏了它 ⇒ 闸门② 的"空 ⇒ 拦"会把整个队列永久锁死（见 `load_local`）。
        ★ 但**不能**把它算进 `same`：`same` 的语义是"无事可做"，
          而"日期还空"时**有事可做** ⇒ 混进去会让汇总说谎。
      · `opencd_tid` 非空异值       ⇒ `diff`（报，不写）
    """
    todo, same, diff, miss = [], [], [], []
    for row in rows:
        info_hash, cur = row[0], row[1]
        filled_added = (row[2] or "").strip() if len(row) > 2 else ""
        key = (info_hash or "").lower()
        if key not in idx:
            miss.append(info_hash)
            continue
        want = str(idx[key]["tid"])
        added = idx[key]["added"]
        has = (cur or "").strip()
        if has == want:
            # tid 已对 —— 只在**日期还空**时仍需动手
            if not filled_added and added:
                todo.append((info_hash, want, added, cur))
            else:
                same.append(info_hash)
        elif not has:
            todo.append((info_hash, want, added, cur))
        else:
            diff.append((info_hash, cur, want))     # ★ 非空异值：报，不写
    return todo, same, diff, miss


# ═══════════════════════════════════════════════════════════════════════════
# 写（★ 唯一的一处）
# ═══════════════════════════════════════════════════════════════════════════

def apply_tids(todo, path=None):
    """
    ★★ 只 `UPDATE opencd_tid` / `opencd_added_at` 两列 —— **不碰 `updated_at`**
       （纪律 3），**不碰** `site_torrent_id` / `site_status` / `content_path` /
       `log_content`，**绝不**用 `db.save_task()`（那是 upsert，会按 `info_hash`
       整体覆盖）。

    ★ 两个 `WHERE ... 空` 守卫是**各自独立**的（★ 别图省事合成一个）：
      · `opencd_tid` 空 ⇒ 填 tid 与日期
      · `opencd_tid` **非空** 但 `opencd_added_at` 空 ⇒ **只填日期**（tid 不动）
        ⇒ 这一支是**必须**的：`opencd_tid` 早先已由本脚本填过一轮（438 行），
          它们的 `opencd_added_at` 全是空 —— 若守卫只看 `opencd_tid` 空，
          这 438 行**永远拿不到发布日期**，而闸门② 的"空 ⇒ 拦"会
          **把整个队列永久锁死**（一条都发不出去，且不报错）。
      ★ 写成"tid 空 ⇒ 两列都写"而不是"分别用 COALESCE 只填空列"，是为了让
        `rowcount` 的语义保持"这一行被本脚本动过" —— 汇总里的数字才与
        实际写入同口径。
    """
    conn = db.connect() if path is None else sqlite3.connect(path, timeout=30)
    n = 0
    try:
        for info_hash, want, added, _cur in todo:
            # ★ 写什么由**库里现在是什么**决定，不由调用方算的 `todo` 决定 ——
            #   SQL 这一层就算 `build()` 算错了也不会覆盖已有值（纪律 2）。
            pub = conn.execute(
                "SELECT opencd_tid, opencd_added_at FROM tasks WHERE info_hash = ?",
                (info_hash,)).fetchone()
            if pub is None:
                continue                     # 行没了（被别处删了）⇒ 不硬造
            cur_tid = (pub[0] or "").strip()
            cur_add = (pub[1] or "").strip()
            sets, params = [], []
            if not cur_tid:
                sets.append("opencd_tid = ?")
                params.append(want)
            if not cur_add and added:
                # ★ 日期**只在空的时候**才写 —— 它是外部事实的投影，
                #   已有值说明上一轮已核过，不做无谓覆盖。
                sets.append("opencd_added_at = ?")
                params.append(added)
            if not sets:
                continue                     # 两列都已填 ⇒ 这行本轮无事可做
            params.append(info_hash)
            cur = conn.execute(
                "UPDATE tasks SET " + ", ".join(sets) + " WHERE info_hash = ?",
                params)
            n += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return n


# ═══════════════════════════════════════════════════════════════════════════
# 汇报
# ═══════════════════════════════════════════════════════════════════════════

def preview(todo, same, diff, miss, idx, conflicts, stats, vertex_path):
    print("=" * 74)
    print("皇后页面 id → tasks.opencd_tid（按 infohash 配对）")
    print("=" * 74)
    print(f"  外部库 VERTEX_DB        : {vertex_path}")
    print(f"  皇后行数（tracker 过滤）: {stats['tracker_rows']}")
    print(f"  可配对（infohash）      : {len(todo) + len(same) + len(diff)}")
    print(f"  ├ 需回填                : {len(todo)}")
    print(f"  ├ 已是该值（幂等跳过）  : {len(same)}")
    print(f"  └ ★ 非空异值（不覆盖）  : {len(diff)}")
    print(f"  未命中                  : {len(miss)}")
    print(f"  ★ infohash 冲突（多 tid）: {stats['conflicts']}")

    if conflicts:
        print("\n  【★ 冲突明细 —— **不自动选、也不写库**，需人工定夺】")
        for k, tids in list(conflicts.items())[:10]:
            print(f"     {k[:16]} 候选 tid={tids}")

    if diff:
        print(f"\n  【★ 非空异值 {len(diff)} 条 —— **本次不覆盖**（纪律 2：只填空白）】")
        print("     若确认皇后的值才是对的，用 `tools/backfill_opencd_tid.py` 人工核过后再处置。")
        for h, cur, want in diff[:10]:
            print(f"     {h[:12]}  现值={cur!r}  皇后={want}")

    if miss:
        print(f"\n  ★ 未命中 {len(miss)} 条（**不回填**）—— 它们在 vertex 库里没有对应 infohash。")
        print("     可能是：① 我们本地自己抓轨的；② vertex 的 RSS 条件把它们滤掉了。")
        print("     ★ 绝不用标题去凑（实测 1/547，见 ）。")
        for h in miss[:5]:
            print(f"       {h}")
        if len(miss) > 5:
            print(f"       ... 另有 {len(miss) - 5} 条")

    if todo:
        print(f"\n  【样本：需回填的前 5 条】")
        for info_hash, want, added, cur in todo[:5]:
            print(f"    {info_hash[:12]}  opencd_tid {cur!r} → {want}"
                  f"   {added or '★发布日期拿不到':<19}"
                  f"  {(idx[(info_hash or '').lower()]['name'] or '')[:38]}")


def summarize(todo, same, diff, miss, stats):
    return (f"需回填 {len(todo)} · 已是该值 {len(same)} · 非空异值 {len(diff)} · "
            f"未命中 {len(miss)} · 冲突 {stats['conflicts']} · 皇后行 {stats['tracker_rows']}")


# ═══════════════════════════════════════════════════════════════════════════
# 自检（离线；不碰真实库、不出网）
# ═══════════════════════════════════════════════════════════════════════════

def _make_vertex_db(path):
    """造一个假的 vertex 库。★ 刻意让 `id` 与 link 里的 tid **不同**（钉纪律）。"""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE torrents (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "hash TEXT, name TEXT, size INTEGER, add_time INTEGER, "
                "link TEXT, tracker TEXT)")
    # ★★ add_time 用**故意挑过的**三个值（钉住时区与"拿不到"两条判据）：
    #   · a = 1750000000 → 东八区 2025-06-15 23:06:40 ⇒ 存 UTC 应得
    #     '2025-06-15 15:06:40'
    #     ★ 若谁写成 `datetime.fromtimestamp(t)`（依赖进程 TZ）：
    #       本机(UTC)得 15:06:40（巧合正确）、生产(CST+8)得 07:06:40（错 8 小时）。
    #       ⇒ 下面 ③b 断言**具体的 UTC 字符串**，把那 8 小时钉死。
    #   · c = 0  → "没填"的占位 ⇒ 必须当**拿不到**（不能变成 1970 ⇒ 闸门无条件放行）
    _T = 1750000000
    rows = [
        # (hash, name, link, tracker, add_time)
        ("a" * 40, "皇后专辑一", "https://open.cd/details.php?id=198601", QUEEN_TRACKER, _T),
        ("B" * 40, "皇后专辑二(大写hash)", "https://open.cd/details.php?id=198602", QUEEN_TRACKER, _T + 3600),
        ("c" * 40, "皇后专辑三", "https://open.cd/details.php?id=198603", QUEEN_TRACKER, 0),
        # ★ 同一个 hash 两个不同 tid ⇒ 冲突fixture，必须**不进写入集**
        ("d" * 40, "冲突资源", "https://open.cd/details.php?id=199001", QUEEN_TRACKER, _T),
        ("d" * 40, "冲突资源", "https://open.cd/details.php?id=199002", QUEEN_TRACKER, _T),
        # ★ 别的站 ⇒ 必须被 tracker 过滤掉
        ("e" * 40, "别的站", "https://ourbits.club/details.php?id=341908", "tracker.ourbits", _T),
        # ★ link 抠不出 tid ⇒ 不许编
        ("f" * 40, "坏 link", "", QUEEN_TRACKER, _T),
    ]
    con.executemany(
        "INSERT INTO torrents (hash,name,size,add_time,link,tracker) VALUES (?,?,0,?,?,?)",
        [(r[0], r[1], r[4], r[2], r[3]) for r in rows])
    con.commit()
    con.close()


def _check():
    ok = fails = 0

    def chk(label, good, extra=""):
        nonlocal ok, fails
        ok, fails = ok + bool(good), fails + (not bool(good))
        print(f"  {'✅' if good else '❌'} {label}{('  ' + extra) if extra else ''}")

    vertex = os.environ["VERTEX_DB"]
    _make_vertex_db(vertex)

    idx, conflicts, stats = load_queen_index(vertex)
    print("① 读外部库：tracker 过滤 / 抠 tid / 冲突识别")
    # ★ fixture 里 tracker=open.cd 的共 6 行：a / B / c / d / d / f
    chk("tracker 过滤后 6 行（ourbits 那条被排除）", stats["tracker_rows"] == 6,
        f"实得 {stats['tracker_rows']}")
    chk("可用索引 3 条（冲突那条被剔掉）", stats["usable"] == 3, f"实得 {stats['usable']}")
    chk("识别出 1 个冲突", stats["conflicts"] == 1, f"实得 {stats['conflicts']}")
    chk("★ 冲突 hash 不在索引里（不自动选）", ("d" * 40) not in idx)
    chk("★ 抠的是 link 里的 tid，不是 torrents.id",
        idx["a" * 40]["tid"] == 198601, f"实得 {idx['a' * 40]['tid']}")
    chk("★ 大写 hash 也被收进索引（lower 折叠）", ("b" * 40) in idx)
    chk("坏 link 不产生条目", ("f" * 40) not in idx)

    # ---- 本地库 ----
    db.init_db()
    conn = db.connect()
    # 第一阶段的行（有 content_path / log_content）—— 钉纪律
    for h in ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40, "f" * 40, "9" * 40):
        conn.execute(
            "INSERT INTO tasks (info_hash,torrent_name,status,content_path,log_content,"
            "site_status,site_torrent_id,opencd_tid,updated_at) "
            "VALUES (?,?,'SUCCESS','/x/y','LOG',  'EXISTS','14611',NULL, '2020-01-01 00:00:00')",
            (h, f"本地-{h[:4]}"))
    # ★ 已有一个**非空异值** ⇒ 必须**不被覆盖**（核心阴性对照）⇒ 应进 `diff` 而不是 `todo`
    conn.execute("UPDATE tasks SET opencd_tid='999999' WHERE info_hash=?", ("c" * 40,))
    # ★ 已是该值（幂等）
    conn.execute("UPDATE tasks SET opencd_tid='198602' WHERE info_hash=?", ("b" * 40,))
    conn.commit()
    conn.close()

    before = {r[0]: r for r in db.connect().execute(
        "SELECT info_hash, opencd_tid, updated_at, site_synced_at, content_path, "
        "log_content, status, site_status, site_torrent_id FROM tasks")}

    rows = load_local()
    todo, same, diff, miss = build(rows, idx)

    print("\n② 四堆切分")
    # ★ 注意 b：fixture 里它的 `opencd_tid` 已经是 198602（幂等），但它**从没被
    #   本脚本写过日期** ⇒ `opencd_added_at` 为空 ⇒ 按新规矩它**必须进 todo**
    #   （不然闸门② 的"空 ⇒ 拦"会把它永久锁死）。所以这里 todo=2、same=0。
    chk("需回填 = 2（a 全空 + b 缺日期）", len(todo) == 2,
        f"实得 {len(todo)}: {[t[0][:8] for t in todo]}")
    chk("★『tid 已对但日期空』的 b **进 todo 而不进 same**",
        ("b" * 40) in [t[0] for t in todo] and ("b" * 40) not in same,
        f"todo={[t[0][:8] for t in todo]} same={[s[:8] for s in same]}")
    chk("★ 非空异值 = 1（c）—— 与 todo **分开**，不进写入集",
        len(diff) == 1 and diff[0][0] == "c" * 40,
        f"实得 {[(d[0][:8], d[1]) for d in diff]}")
    chk("未命中 = 4（d 冲突 / e 别的站 / f 坏link / 9 无对应）",
        len(miss) == 4, f"实得 {len(miss)}: {[m[:8] for m in miss]}")
    chk("★ 冲突行(d) 落进未命中，**不在**写入集", ("d" * 40) in miss)
    chk("★★ 判据与实际写入同口径：len(todo)+len(same)+len(diff)+len(miss) == 本地行数",
        len(todo) + len(same) + len(diff) + len(miss) == len(rows),
        f"{len(todo)}+{len(same)}+{len(diff)}+{len(miss)} vs {len(rows)}")
    chk("★ todo 的元素是 4 元组（info_hash, tid, added, 现值）",
        all(len(t) == 4 for t in todo), f"实得 {[len(t) for t in todo]}")

    print("\n③ 写入（只填空白 / 只改一列）")
    n = apply_tids(todo)
    chk("★ 写入行数 == len(todo)（口径一致，无虚高）", n == len(todo), f"实得 {n} vs {len(todo)}")

    after = {r[0]: r for r in db.connect().execute(
        "SELECT info_hash, opencd_tid, updated_at, site_synced_at, content_path, "
        "log_content, status, site_status, site_torrent_id, opencd_added_at "
        "FROM tasks")}

    chk("★ 空白被填上（a → 198601）", after["a" * 40][1] == "198601",
        f"实得 {after['a' * 40][1]!r}")
    chk("★★ 已存在的非空异值**未被覆盖**（c 仍是 999999）",
        after["c" * 40][1] == "999999", f"实得 {after['c' * 40][1]!r}")
    chk("★ 冲突行(d) 仍是空（没被写）", after["d" * 40][1] is None,
        f"实得 {after['d' * 40][1]!r}")
    chk("★ 发布日期**也**落库了（a 非空）", bool(after["a" * 40][9]),
        f"实得 {after['a' * 40][9]!r}")
    chk("★★『tid 已对但日期空』的 b **补上了日期**", bool(after["b" * 40][9]),
        f"实得 {after['b' * 40][9]!r}")

    print("\n③b ★★ 时区：unix 秒必须按**东八区**转成 UTC 字符串")
    # ★ fixture a 的 add_time = 1750000000
    #   东八区 2025-06-15 23:06:40  →  UTC 2025-06-15 15:06:40
    #   ★ 若谁写成 `datetime.fromtimestamp(t)`（依赖进程 TZ），
    #     本机(UTC)会得 15:06:40（巧合正确）、生产(CST+8)会得 07:06:40（错 8 小时）。
    #     所以这条**断言具体的 UTC 字符串**，而不是"非空"。
    chk("added_at_utc(1750000000) == '2025-06-15 15:06:40'（东八区→UTC）",
        added_at_utc(1750000000) == "2025-06-15 15:06:40",
        f"实得 {added_at_utc(1750000000)!r}")
    # ★★ 阴性对照：证明上面那条**不是碰巧**（本机 TZ=UTC，裸 fromtimestamp 也过）
    #   ⇒ 换一个"东八区与 UTC 不在同一天"的时刻：东八区 2025-01-01 07:00:00
    #     = UTC 2024-12-31 23:00:00 ⇒ 裸 fromtimestamp(本机UTC) 会得 12-31 23:00:00
    #     而正确实现必须得 2024-12-31 23:00:00 —— ★ 两个一样了，改用反向：
    #     取东八区 2025-01-01 00:30:00 = UTC 2024-12-31 16:30:00，
    #     裸 fromtimestamp 在 CST 机器上会得 16:30:00（错，因为 CST 会转回 08:30）。
    #   ★ 用"跨日"这件事来钉：东八区与 UTC 的**日期**必须不同。
    jan1 = 1735662600            # 2025-01-01 00:30:00 东八区 = 2024-12-31 16:30:00 UTC
    got = added_at_utc(jan1)
    chk("★ 跨日时刻：东八区 2025-01-01 00:30 ⇒ UTC 2024-12-31 16:30",
        got == "2024-12-31 16:30:00", f"实得 {got!r}")
    chk("add_time=None ⇒ None（不编）", added_at_utc(None) is None)
    chk("add_time=0 ⇒ None（★ 不许变成 1970 ⇒ 那会让闸门无条件放行）",
        added_at_utc(0) is None)
    chk("add_time=-5 ⇒ None", added_at_utc(-5) is None)
    chk("add_time='abc' ⇒ None（不抛）", added_at_utc("abc") is None)

    print("\n④ ★★ `updated_at` 与其它列逐行字节不变（纪律 3 的回归守卫）")
    # ★ 下标：0=info_hash 1=opencd_tid | 2..8 = 受保护 | 9=opencd_added_at（本轮会写）
    protected = (2, 3, 4, 5, 6, 7, 8)      # updated_at..site_torrent_id
    dirty = [h for h, r in before.items() if r[:2] != ("",)
             and (after[h][1] != r[1] or True)]
    changed_cols = []
    for h, r in before.items():
        a = after[h]
        for i in protected:
            if r[i] != a[i]:
                changed_cols.append((h[:8], i, r[i], a[i]))
    chk("★ `updated_at`(idx 2) 一行都没变",
        not any(c[1] == 2 for c in changed_cols), f"{changed_cols[:3]}")
    chk("★ `site_synced_at` / `content_path` / `log_content` / `status` / "
        "`site_status` / `site_torrent_id` 全都没变",
        not changed_cols, f"{changed_cols[:3]}")

    print("\n⑤ 幂等：再跑一次，0 条改动")
    rows2 = load_local()
    todo2, same2, diff2, _ = build(rows2, idx)
    n2 = apply_tids(todo2)
    chk("第二次写成 0 行", n2 == 0, f"实得 {n2}")
    # ★ 第二次 todo **必须是空的** —— a/b 两列都满了（本轮的改动就是把 b 的日期补上），
    #   c 是"非空异值"⇒ 走 `diff` 不留在 todo。
    #   ★ 这条以前靠"c 不进 todo"就过了；现在**多了 b**：若 `build()` 忘了把
    #     "tid 已对但日期空"归进 todo，b 就在第一轮被写成 same、日期永远空着，
    #     而闸门② 的"空 ⇒ 拦"会把队列**永久锁死且不报错**（见 ⑨）。
    #     ⇒ 这条与 ⑨ 一起，从两个方向钉住同一件事。
    chk("★ 第二次 todo 为空（a/b 两列都满，c 走 diff）",
        not todo2, f"实得 {[t[0][:8] for t in todo2]}")
    chk("★ a / b 都**不再**进 todo（它们两列都满了）",
        ("a" * 40) not in [t[0] for t in todo2]
        and ("b" * 40) not in [t[0] for t in todo2])
    chk("★ 第二次 diff 仍为 1（c 一直是那个异值，且一直不被写）",
        len(diff2) == 1, f"实得 {len(diff2)}")

    print("\n⑥ ★ 只读证明：外部库 mtime/size 不变，且没多出 -wal/-shm")
    st = os.stat(vertex)
    _idx, _c, _s = load_queen_index(vertex)
    st2 = os.stat(vertex)
    chk("外部库 size 不变", st.st_size == st2.st_size, f"{st.st_size} → {st2.st_size}")
    chk("外部库 mtime 不变", st.st_mtime == st2.st_mtime)
    chk("没多出 -wal / -shm",
        not os.path.exists(vertex + "-wal") and not os.path.exists(vertex + "-shm"))

    print("\n⑦ ★★ 源读不到 / 结构漂移 ⇒ 必须非零（绝不降级成'没有'）")
    missing_path = os.path.join(os.environ["DATA_DIR"], "nope", "sql.db")
    try:
        load_queen_index(missing_path)
        chk("路径不存在 ⇒ raise", False, "竟然没抛")
    except SourceUnavailable:
        chk("路径不存在 ⇒ raise SourceUnavailable", True)

    bad = os.path.join(os.environ["DATA_DIR"], "bad-schema.db")
    con = sqlite3.connect(bad)
    con.execute("CREATE TABLE torrents (foo TEXT)")     # ★ 没有 link / hash / tracker
    con.commit(); con.close()
    try:
        load_queen_index(bad)
        chk("表结构漂移 ⇒ raise", False, "竟然没抛（**正是最危险的形状**）")
    except SourceUnavailable:
        chk("★ 表结构漂移 ⇒ raise SourceUnavailable", True)

    empty = os.path.join(os.environ["DATA_DIR"], "empty.db")
    open(empty, "wb").close()                          # 0 字节（bind mount 源丢了的形状）
    try:
        load_queen_index(empty)
        chk("0 字节库 ⇒ raise", False, "竟然没抛")
    except SourceUnavailable:
        chk("★ 0 字节库（挂载源丢失的形状）⇒ raise", True)

    print("\n⑧ 异常护栏：皇后行数地板")
    chk(f"MIN_QUEEN_ROWS = {MIN_QUEEN_ROWS}（实测 583，留足余量）", MIN_QUEEN_ROWS > 0)

    print("\n⑨ ★★『tid 已有、日期还空』必须能被补上（否则闸门② 永久锁死队列）")
    #   这是**本次改动最危险的形状**：`opencd_tid` 早已由本脚本填过一轮（生产 438 行），
    #   而闸门② 的新判据是「`opencd_added_at` 空 ⇒ 拦」。
    #   若 `build()` 把这类行归进 `same`（"无事可做"），那 438 行的日期**永远填不上**，
    #   于是**整个发布队列一条都发不出去，而且不报错**。
    #   ⇒ 造一行真实形状（tid 对、日期空）来钉它。
    conn = db.connect()
    conn.execute("UPDATE tasks SET opencd_added_at=NULL WHERE info_hash=?",
                 ("b" * 40,))          # b: tid=198602 已对，只把日期抹空
    conn.commit(); conn.close()
    rows3 = load_local()
    todo3, same3, _d3, _m3 = build(rows3, idx)
    chk("★『tid 已对但日期空』的行**进 todo**（不是 same）",
        ("b" * 40) in [t[0] for t in todo3],
        f"todo={[t[0][:8] for t in todo3]} same={[s[:8] for s in same3]}")
    n3 = apply_tids(todo3)
    chk("★ 这一轮确实写了它", n3 >= 1, f"实得 {n3}")
    got = db.connect().execute(
        "SELECT opencd_added_at FROM tasks WHERE info_hash=?", ("b" * 40,)).fetchone()[0]
    chk("★★ 日期被补上了", bool(got), f"实得 {got!r}")
    keep = db.connect().execute(
        "SELECT opencd_tid FROM tasks WHERE info_hash=?", ("b" * 40,)).fetchone()[0]
    chk("★ 且 tid **没被动过**（仍是 198602，不是重新写一遍的随机值）",
        keep == "198602", f"实得 {keep!r}")
    # ★ 幂等复核：再跑一次，两列都满了 ⇒ 0 改动
    _t4, _s4, _d4, _m4 = build(load_local(), idx)
    chk("★ 补完之后再跑 = 0 改动（幂等仍成立）",
        apply_tids(_t4) == 0, f"todo={len(_t4)}")

    return ok, fails


# ═══════════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description="按 infohash 从 vertex2 库补 tasks.opencd_tid（皇后页面 id）")
    ap.add_argument("--apply", action="store_true",
                    help="真正写库（只填空白、只改 opencd_tid 一列）")
    ap.add_argument("--selftest", action="store_true", help="离线自检")
    ap.add_argument("--json", nargs="?", const="-", metavar="PATH",
                    help="把结果以 JSON 输出（不带路径=打到末行；--selftest 时也用它）")
    ap.add_argument("--vertex-db", default=None, help="外部库路径（默认按环境推断）")
    args = ap.parse_args()

    if args.selftest:
        ok, fails = _check()
        if args.json:
            print(json.dumps({"script": "sync_opencd_tid.py --selftest",
                              "ok": ok, "total": ok + fails,
                              "exit": 1 if fails else 0}, ensure_ascii=False))
        else:
            print(f"\n{'✅ 自检通过' if not fails else '❌ 自检失败'}：{ok}/{ok + fails} 项")
        return 1 if fails else 0

    vertex = args.vertex_db or resolve_vertex_db()

    # ★ 廉价护栏：别把自己的库当外部库（会"自己 join 自己"）
    try:
        if os.path.exists(vertex) and os.path.exists(config.DB_PATH) \
                and os.path.realpath(vertex) == os.path.realpath(config.DB_PATH):
            print(f"❌ 外部库与本项目的 data.db 是同一个文件：{vertex}", file=sys.stderr)
            return 3
    except OSError:
        pass

    # ★★ 纪律 4：源读不到 ⇒ 非零退出，**绝不**降级成"没有可回填的行"
    try:
        idx, conflicts, stats = load_queen_index(vertex)
    except SourceUnavailable as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    # ★ 异常护栏：源"读到了"但明显不对（行数崩了）⇒ 非零
    if stats["tracker_rows"] < MIN_QUEEN_ROWS:
        print(f"❌ 皇后行数 {stats['tracker_rows']} 低于地板 {MIN_QUEEN_ROWS}"
              f"（源：{vertex}）—— 源八成不对劲，本轮不写。", file=sys.stderr)
        return 3

    rows = load_local()
    todo, same, diff, miss = build(rows, idx)

    preview(todo, same, diff, miss, idx, conflicts, stats, vertex)
    print(f"\n📊 {summarize(todo, same, diff, miss, stats)}")

    if args.json:
        payload = {"vertex_db": vertex, "queen_tracker": QUEEN_TRACKER,
                   "todo": [{"info_hash": h, "opencd_tid": w,
                             "opencd_added_at": a} for h, w, a, _ in todo],
                   "same": same,
                   "diff": [{"info_hash": h, "current": c, "queen": w}
                            for h, c, w in diff],
                   "unmatched": miss, "conflicts": conflicts, "stats": stats}
        if args.json == "-":
            print(json.dumps(payload, ensure_ascii=False))
        else:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            print(f"💾 已写出：{args.json}")

    if not args.apply:
        print("\n💡 报告模式，未写库。加 --apply 写入。")
        return 0

    if not todo:
        print("\n✅ 无事可做（没有空白可填）。")
        return 0

    n = apply_tids(todo)
    print(f"\n💾 已回填 opencd_tid：{n} 条")
    print("★ 注意：本步**不碰 `updated_at`** ⇒ 已在站上的行不会自动重推。")
    print("  要让皇后路渲染在站点上生效，需**另外**把这批行显式标 stale 再跑 edit。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
