"""NAS musopia daemon 健康巡检（只读）。

★ 纪律（本脚本的存在就是为了防 2026-09-19 那天犯过两次的错）：
  - 判据一律**精确计算**，绝不心算 —— 当天把 20.6h 当成「超过 24h」、
    把构建上下文的增量字节当成「文件没传过去」，都是 ERR-META-01 一族。
  - 判据是「**同一份 state 文件里该动的有没有动**」，
    不是「日志里有没有」、不是「我觉得该跑了」。
  - ★ 不 cp 生产库（ERR-SQL-01）：SMB 只读直连，PRAGMA query_only=1。

用法（本机，不需要连容器）:
    python tools/nas_health.py            # 正常巡检
    python tools/nas_health.py --reset    # 清掉上次快照（只报一次全量）
"""
import argparse
import json
import os
import pathlib
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

DST = pathlib.Path("//iSunker-DS423/docker_ssd/musopia_script2")
SNAP = pathlib.Path(__file__).with_name(".nas_health_last.json")

# 时间口径：state 里的 last_run 是 epoch（UTC 无歧义）；
# 但 last_finish 字符串与 data.db 的 updated_at 是**两个不同口径**（ERR-SQL-03）：
#   last_finish → 本地 CST；data.db.updated_at → UTC。
CST = 8 * 3600

# ★★ 心跳阈值：**它不是"daemon 的生死判据"，只是"该不该往下看阶段表"的提示**。
#
#   ★ 为什么 120s 这个数**必然误报**（2026-09-24 实测）：
#     `daemon.py` 主循环是  beat() → 遍历阶段（`run_stage()` **同步阻塞**）→ sleep(tick)。
#     ⇒ 心跳间隔 = tick(30s) **+ 本轮到期阶段的耗时**，而单个阶段的
#       `STAGE_TIMEOUT` 可达 3600s ⇒ 阶段运行期间心跳**本就不写**。
#     实测：`scan` 一轮跑了 ~40 分钟，期间心跳静默 640s ⇒ 被报成
#     「★★ daemon 可能已停/卡住」，而进程完全正常（下一采样就跳到 3s 前）。
#     ★ 这个数（640s）**既不是"故障"也不是"正常"** —— 它只是"距上次心跳多久"，
#       而那次心跳之后的静默是**运行阶段的代价**。
#
#   ★★ 那为什么不把阈值调大（如 3600）？因为那会**把真停瞒一小时** —— 是拿
#      假阳性换假阴性，两个都不对。
#
#   ★★★ 真正的判据只有一条（`ERR-SQL-04`）：**看它有没有在*前进*** ——
#     即"两次采样之间心跳值有没有变化"。而**单次巡检给不出这个结论**
#     （单点只是一个数，不包含"变没变"）。⇒ 本脚本改用两次采样（`SNAP`），
#     并且**在拿不到上一次采样时如实说不判**，不再用一个阈值假装能判。
HEARTBEAT_FRESH = 120        # 秒；仅用于"是否需要提醒"，**不是**生死判据

# ★★★ 2026-09-27 **订正 + 改判据来源**（这条被漂了整整一天才被发现）。
#
#   原表写的是 `"publish": 1800`，而 NAS 上的真值 `PUBLISH_INTERVAL=5400`
#   （实测 `docker exec opencd2musopia env`）⇒ **周期判据少了 3 倍**，
#   于是「★ 超过周期」这条会**凭空报到 3 小时**（好在它当前是空转，没人被误伤）。
#
#   ★ 根因与 `ERR-DOCKER-09` **同族但更硬**：本机**读不到 NAS 的 `.env`**
#     （deploy.sh 的 NEVER 名单，`ERR-ENV-02`）⇒ 这张硬编码表**必然是过期副本**。
#     上面 `INTERVALS` 的注释自己就写着"判据来源是**源码**"—— 而源码里的
#     `env_int("PUBLISH_INTERVAL", 1800)` 那个 `1800` 只是**默认值**，
#     真实生效值在 `.env` 里。**把"代码默认值"当成"部署真值"**就是这张表的病根。
#
#   ★ 修法（与 `ENV_KEY_TYPE` 表的思路一致：**把口径搬进数据结构，别靠人记得改**）：
#     · 容器现在会把 `*_INTERVAL` / `RUN_ON_START` 落进 `daemon_env.json` 快照
#       （`daemon.ENV_SNAPSHOT_KEYS`，2026-09-27 加）；
#     · `INTERVALS` 降级为**兜底**（快照读不到时用），每次运行都印出**数据来源**；
#     · ★ 兜底值**必须与代码默认值一致**，且若快照读到了就**以快照为准** ——
#       两处不一致时**报出来**、不静默取一个（否则又是两处来源，`ERR-META-03`）。
#
#   ★ 表里**仍必须**与 `daemon.build_stages()` 的 7 个阶段一一对应
#     （2026-09-22 补 crawl、2026-09-23 补 vtid）。
#     ★ 这里**曾经**漏了 crawl：后果是它从不进入下面的检查循环 —— 一个被门控
#       关掉的阶段看起来"正常"，因为你根本没查它。而"没查"和"查了没事"在报告里
#     长得一模一样（`ERR-META-01` 一族：检测方法失效比不检测更危险）。
#     ★ 也曾漏过 `vtid`（`STAGE_ENABLE_KEY` 里有 `ENABLE_VTID`，但 INTERVALS 里没有
#       ⇒ 它同样不进入循环）。⇒ 判据来源是**源码**（`daemon.build_stages()` 里
#       每个 `env_bool("ENABLE_*")`），**不是 startup banner** —— banner 只列
#       门控后进来的阶段，两个 false 就被它吃掉（拿它当"阶段列表"已经错过一次）。
INTERVALS = {"scan": 1800, "publish": 5400, "sync": 86400,
             "covers": 21600, "edit": 21600, "vtid": 86400, "crawl": 1800}
# 阶段名 → 快照里那个周期键（**显式列出**，不靠 `f"{stage.upper()}_INTERVAL"` 拼 ——
# 拼法在改名时不会报错，只会静默查不到，那种"静默"正是本文件反复记的那类坑）
STAGE_INTERVAL_KEY = {"scan": "SCAN_INTERVAL", "publish": "PUBLISH_INTERVAL",
                      "sync": "SYNC_INTERVAL", "covers": "COVERS_INTERVAL",
                      "edit": "EDIT_INTERVAL", "vtid": "VTID_INTERVAL",
                      "crawl": "CRAWL_INTERVAL"}
EDIT_COOLDOWN = 60
# ★ 旧的 `HEARTBEAT_FRESH = 120` 挪到上面（连同"为什么它不是生死判据"的实测依据）。

# ★★ 哪些阶段"关着"（last_run 长期不动是**预期内**的）。
#
# ⚠️ 这里曾经硬编码 `{"publish"}` 并据此打印「已禁用（预期不动）」—— **那是错的**，
#    而且是个危险的错：它把"publish 66 小时没动"这种**真异常**说成了"预期"。
#    （2026-09-22 巡检实测：publish.last_run 停在 66h 前，而它本该每 30 分钟一跑。）
#
# ★ 为什么本机**不能**静态知道生产开着什么：真正决定的是 NAS 上那份 `.env`
#   （`ENABLE_*` 开关），而 `.env` 在 deploy.sh 的 NEVER 名单里、本机读不到（ERR-ENV-02）。
#   `.env.example` 只是**模板**，两者可以不一致（实测模板 ENABLE_PUBLISH=true 而生产 false）。
#
# ★★ 2026-09-22 解法：让**容器自己**把生效的开关落成 `/data/daemon_env.json`
#    （见 `daemon.save_env_snapshot()`；`/data` 是 bind mount，本机 SMB 看得到）。
#    ⇒ 本脚本现在**读那份快照**来判"哪些阶段关着"，**不再猜、也不再要人手传**。
#    优先级：`--disabled` 显式声明 > 快照文件 > 空集（空集 = 每个阶段都按"该动就得动"判，
#    宁可多报一个待查，不可把真异常说成"预期不动"—— 漏报比误报危险得多）。
DISABLED_STAGES: set = set()
ENV_SNAPSHOT = "daemon_env.json"
# 快照超过这么久没刷新 ⇒ **不作为判据**（一份停在上个月的快照是 ERR-META-04 说的"传说"）
#   阈值取 1 小时：daemon 每轮都会重写它（tick 默认 30s），1 小时没动基本就是 daemon 停了。
SNAPSHOT_MAX_AGE = 3600
# 快照里 `ENABLE_<阶段>` 与阶段名的对应（**必须显式列出**，不靠猜大小写）
STAGE_ENABLE_KEY = {"scan": "ENABLE_SCAN", "publish": "ENABLE_PUBLISH",
                    "sync": "ENABLE_SYNC", "covers": "ENABLE_COVERS",
                    "edit": "ENABLE_EDIT", "crawl": "ENABLE_CRAWL"}
# ★★ 自动发布的第二把钥匙（2026-09-24）。**必须与 `publish.AUTO_KEY_ENV` 同名** ——
#    写死一处会漂（ERR-META-03）。这里不 import（本脚本刻意不依赖项目代码：
#    它要在**本机**跑，而 `config` 缺密钥就 `sys.exit`，见 ERR-ENV-01 一族）。
AUTO_KEY_ENV = "PUBLISH_I_AM_HERE"
# ★ 约定值同样**必须与 `publish.AUTO_KEY_VALUE` 一致**。★ 判据是**字符串相等**，
#   不是布尔真假 —— 把它按 `env_bool` 转会得到假 False（2026-09-24 实测踩过）。
AUTO_KEY_VALUE = "i-am-here"

# ★★★ 键的**类型表**（2026-09-24 第三次踩同类错后加的，见 ERR-META-13）。
#   为什么要有这张表：`read_env_snapshot` 返回两个视图（`vals` 布尔 / `raw` 字符串），
#   而"哪个键该用哪个视图"原先**只写在注释里**。注释不会被执行 ——
#   2026-09-24 实测连续两轮踩错（先 `PUBLISH_I_AM_HERE` 当布尔 ⇒ 假 False；
#   再 `PUBLISH_MAX_PER_CYCLE` / `PUBLISH_MIN_GAP` 当布尔 ⇒ `"120"` 印成 `True`）。
#   ⇒ 把口径**从注释搬进数据结构**：下游按 `ENV_KEY_TYPE` 取视图，
#     而不是靠读注释记得住（`ERR-META-01` 的同一个形状：判据要可执行，不能靠记忆）。
#   取值： "bool"（开关，走 vals） / "str" / "int"（走 raw，再按自身类型解析）
ENV_KEY_TYPE = {
    # ---- 阶段开关：布尔 ----
    "ENABLE_SCAN": "bool", "ENABLE_PUBLISH": "bool", "ENABLE_SYNC": "bool",
    "ENABLE_COVERS": "bool", "ENABLE_EDIT": "bool", "ENABLE_CRAWL": "bool",
    "DRY_RUN": "bool",
    # ---- 第二把钥匙：**字符串**（按字符串相等判，绝不转布尔）----
    "PUBLISH_I_AM_HERE": "str",
    # ---- 速率/时长旋钮：**整数** ----
    "PUBLISH_INTERVAL": "int", "PUBLISH_MAX_PER_CYCLE": "int",
    "PUBLISH_MIN_GAP": "int",
    # ---- 调度语义（2026-09-27 加）----
    #   ★ `RUN_ON_START` 是**布尔**（它决定启动是否清空 last_run，见 daemon.main()）；
    #     其余 `*_INTERVAL` 是**整数秒**。
    #   ★ 把 `RUN_ON_START` 落在**这里**（而不是只落进 `STAGE_ENABLE_KEY` 那种
    #     "门控开关"循环）是因为它**不是门控**：阶段开关决定"跑不跑"，
    #     它决定"**什么时候开始算时间**"。两者的读法不同，别混。
    "RUN_ON_START": "bool",
    "SCAN_INTERVAL": "int", "SYNC_INTERVAL": "int", "COVERS_INTERVAL": "int",
    "EDIT_INTERVAL": "int", "VTID_INTERVAL": "int", "CRAWL_INTERVAL": "int",
}


def resolve_intervals(snap_vals, snap_raw, snap_note="", snap_age=None):
    """★ 每个阶段的周期：**快照优先，硬编码兜底**，并返回每个值的**来源**以便印出来。

    返回 (intervals: dict[str, int], sources: dict[str, str], warns: list[str])。

    ★★ 为什么不能简单"快照没有就用兜底"就完事：那样两处不一致时会**静默**取一个，
      而"静默取一个"正是 `ERR-META-03` 的形状（一个量两处来源，读的人不知道信哪份）。
      ⇒ 凡快照**读到了**却与兜底**不等**，一律记一条 warn 印出来 —— 让"哪个是真"是个
      **看得见的决定**，而不是一个碰巧的取值顺序。

    ★ 判据必须是 `typed()`（按 `ENV_KEY_TYPE` 取正确视图），**不能**直接从 `vals`
      （布尔视图）读整数 —— 那会把 `"5400"` 转成 `True`（`ERR-META-13` 一族，
      本文件 2026-09-24 为 `PUBLISH_MAX_PER_CYCLE` 踩过一次）。
    """
    out, src, warns = {}, {}, []
    for stage, fallback in INTERVALS.items():
        key = STAGE_INTERVAL_KEY.get(stage)
        val = typed(snap_vals, snap_raw, key) if key else None
        if isinstance(val, bool):                 # typed() 对 int 键不该返回 bool
            warns.append(f"{key} 从快照读出了布尔值（类型表写错了？）⇒ 改用兜底")
            val = None
        if isinstance(val, int) and val > 0:
            n = val
            src[stage] = f"快照 {key}={n}"
            if n != fallback:
                warns.append(f"★ {stage} 周期**两处不一致**：快照 {key}={n}s，"
                             f"本文件兜底 {fallback}s ⇒ **以快照为准**（容器才是真跑的）")
        else:
            n = fallback
            src[stage] = (f"⚠ 兜底 {fallback}（快照里没有 {key} —— "
                          f"{snap_note or '键未落盘'}）")
        out[stage] = n
    # ★ 兜底值本身也要能被审计：表里漏一个阶段 ⇒ 它根本不进循环（见 INTERVALS 注释）
    _missing = set(STAGE_INTERVAL_KEY) - set(INTERVALS)
    if _missing:
        warns.append(f"★ STAGE_INTERVAL_KEY 里有 INTERVALS 不知道的阶段：{_missing}")
    return out, src, warns


def typed(snap_vals, snap_raw, key):
    """按 `ENV_KEY_TYPE` 取**正确视图**的值；类型未声明 ⇒ 返回 None 并**不猜**。

    ★ 这是"先问这个键是什么类型，再决定用哪个视图"的**可执行版本**。
    ★★ 类型表里没有的键 ⇒ 返回 `None`（= 未声明），**绝不**默认按布尔取 ——
      默认按布尔正是那两次实测踩错的成因。调用方必须显式处理 None。
    """
    t = ENV_KEY_TYPE.get(key)
    if t == "bool":
        return None if not snap_vals else snap_vals.get(key)
    if t in ("str", "int"):
        return None if not snap_raw else snap_raw.get(key)
    return None


def read_env_snapshot(dst=None):
    """读容器落下的开关快照，返回 (values: dict, raw: dict, age_s, note: str)。

    ★ 判"真假"的口径**照抄 `daemon.env_bool`**（`strip().lower() in ("1","true","yes","on")`），
      只有一处实现错误的地方就全错，所以这里宁可多写一行也不"差不多就行"。
    ★★ 2026-09-24 订正：`env_bool` 的口径**只对布尔开关成立**。快照里还有
      **字符串**键（`PUBLISH_I_AM_HERE` = `i-am-here`），把它也按布尔转
      ⇒ 得到一个**假 False** ⇒ 下游会把"**值完全正确**"读成"**配错了**"。
      （★ 这就是"用别人的口径量自己"—— 2026-09-24 实测踩过一次：
       快照里明明是 `'i-am-here'`，`【自动发布】`段却报「已配但值不对」。）
    ★ 现在同时返回**原始字符串** `raw`（键名 → 未转换的值），
      字符串键**必须**从 `raw` 读，布尔键再走 `vals`。两个视图并存，各按各的口径。
    ★★★ 2026-09-24 **第二次**踩同一个错（这次是**整数**，不是字符串）：
      新增的 `PUBLISH_MAX_PER_CYCLE` / `PUBLISH_MIN_GAP` 是**整数**，
      从 `vals`（布尔视图）读 ⇒ `"1"` 印成 `True`、`"120"` 被当成 `1`
      ⇒ `【发布速率】` 段报出**看着像真的**错数。
      ★ 一般化（比"字符串要特殊处理"更准）：**`vals` 只对"开关"成立**；
        凡是**非布尔**的键（字符串 / 整数 / 时长）**一律从 `raw` 读**，
        再按它**自己的**类型解析。
      ⇒ 判据：**先问这个键是什么类型，再决定用哪个视图**；
        不要因为"它也是从 env 来的"就套布尔口径（`ERR-META-03` 的味道）。
    ★ 任何一步失败都**返回空 dict + 原因**，绝不返回一个"看着正常的默认值" ——
      默认值会被下游当成事实用（ERR-META-01 一族）。
    ★★★ 2026-09-24 第三次同形错后**落成 `ENV_KEY_TYPE` 表**（见上）：用哪个视图
      由**数据类型**决定，不再由"读的人记不记得注释"决定。下游一律走 `typed()`；
      直接摸 `vals` / `rawvals` 只允许出现在这一段的三个例外处（阶段开关循环、
      `_as_int` 兜底、`key_raw` 回显），且每处都要写明**为什么**这里是安全的。
    """
    p = (dst or DST) / ENV_SNAPSHOT
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, {}, None, "快照不存在（容器还没跑过新版 daemon，或 /data 挂点不对）"
    except Exception as e:                                    # noqa: BLE001
        return {}, {}, None, f"快照读取失败：{type(e).__name__}: {e}"

    age = time.time() - p.stat().st_mtime
    rawvals = {k: v for k, v in (raw.get("values") or {}).items()}
    vals = {}
    for k, v in rawvals.items():
        vals[k] = str(v).strip().lower() in ("1", "true", "yes", "on")
    if age > SNAPSHOT_MAX_AGE:
        return {}, {}, age, (f"快照已 {age / 3600:.1f}h 未刷新（阈值 "
                             f"{SNAPSHOT_MAX_AGE / 3600:.1f}h）⇒ 不拿它当判据")
    return vals, rawvals, age, ""


def cst(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch + CST))


def fmt_age(sec):
    """把秒数写成人类可读，但**同时保留精确秒数** —— 不心算。"""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m or h:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return f"{' '.join(parts)}  ({sec}s)"


def resolve_dst():
    """NAS 部署目录。优先级与 deploy.sh 一致（见 ERR-DEPLOY-01）：
    外部环境变量 DST → 同目录 .deploy.nasrc 里的 DST= → 硬编码兜底。
    ★ .deploy.nasrc 的定位是**默认值**，不是强制值。"""
    v = os.environ.get("DST")
    if v:
        return pathlib.Path(v)
    nrc = pathlib.Path(__file__).parent.parent / ".deploy.nasrc"
    if nrc.exists():
        for ln in nrc.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith("DST="):
                return pathlib.Path(ln[4:].strip())
    return DST


def read_state(dst=None):
    return json.loads(((dst or DST) / "daemon_state.json").read_text(encoding="utf-8"))


def read_heartbeat(dst=None):
    """返回 (内容里的 epoch, 文件 mtime 的 epoch)。两个都看 —— 单一采样不足以判活。"""
    p = (dst or DST) / "daemon_heartbeat"
    raw = p.read_text(encoding="utf-8").strip()
    content = int(raw) if raw.isdigit() else None
    return content, p.stat().st_mtime


def read_db(dst=None):
    con = sqlite3.connect(str((dst or DST) / "data.db"))
    con.execute("PRAGMA query_only=1")
    cur = con.cursor()

    # ★ stale 的口径与 db.py 的 STALE_CONDITION 严格一致：
    #   site_status='EXISTS' AND (site_synced_at IS NULL OR updated_at > site_synced_at)
    stale = cur.execute(
        "SELECT count(*) FROM tasks WHERE site_status='EXISTS' "
        "AND (site_synced_at IS NULL OR updated_at > site_synced_at)"
    ).fetchone()[0]
    exists = cur.execute(
        "SELECT count(*) FROM tasks WHERE site_status='EXISTS'"
    ).fetchone()[0]
    total = cur.execute("SELECT count(*) FROM tasks").fetchone()[0]
    mx = cur.execute("SELECT max(updated_at) FROM tasks").fetchone()[0]

    # ★★ `PUBLISHED` 口径（2026-09-24 加）—— 补上「publish **真的**推进了没」这条判据。
    #
    #    ★ 为什么需要它：`publish.last_run` 只说明**阶段跑过**，不说明**发出去东西**。
    #      实测踩过：阶段 6.2 秒"成功 1 条"，而站上是否发出、qB 是否真做种，
    #      `last_finish` 一个字都没说。⇒ 现在把"已上站的条数"当独立判据报出来。
    #
    #    ★ `status='PUBLISHED'` 是 `run.py`/`publish.py` 写回的状态（见 db.py）。
    #      它**单调增**，所以两次巡检之差 = **本轮真发出去的条数**。
    #
    #    ★★ 但它**只到"上站"为止，到不了"做种"**：qB 侧 `state=missingFiles` 这种
    #      坏种**照样是 `PUBLISHED`**（实测：修 `readd_to_qb` 前那 6 条全是）。
    #      ⇒ 本文件**不连 qB**（纯 SMB 只读）⇒ 那一条**只能另测**：
    #         cd /volume2/docker_ssd/musopia_script2
    #         sudo docker compose run --rm tools python -c "..."   # 查 qB 里的 state
    #      或（延迟后）按 infohash 查 `content_path` / `state`。
    #      ★ 别把这条 `PUBLISHED` 数**当成**"做种正常" —— 那是两码事（ERR-META-01）。
    published = cur.execute(
        "SELECT count(*) FROM tasks WHERE status='PUBLISHED'"
    ).fetchone()[0]

    # ══════════════════════════════════════════════════════════════════
    # ★★★ 2026-09-26 加：`crawl` 的「在做功吗」判据
    #
    #   起因（当晚实测踩到，且**骗过了本脚本**）：`crawl.last_run` 每 30 分钟
    #   推进一次、`nas_health.py` 报「✅ 在周期内」—— 而它**一个请求都没发**：
    #       · 当天配额 50/50 已在 19:58 CST 用满（账本是 data.db:opencd_quota）；
    #       · 配额满后 `topup` 第 51 个请求被 `RateGate` 拒 ⇒ 立刻返回；
    #       · 于是阶段耗时 **1.4 秒**、报「✅ 完成」、`last_run` 照常推进。
    #   ⇒ ★★ **`last_run` 新鲜 ≠ 阶段在做功**（这正是 ERR-META-01 一族：
    #      判据看着合理，量的却是另一个对象 —— 这里量的是"调度到了"，
    #      不是"抓到了")。
    #
    #   真判据 = 「**目标行数有没有减少**」（与 edit 那条 `no_progress` 同款思路）：
    #       目标 = 有 opencd_tid 但 mapped_json 空的皇后行（= crawl 要补的那些）
    #     配额 = 当天已用 / 上限
    #   ⇒ 「**配额已满 且 目标没减少**」⇒ 这才叫空转，必须报出来。
    #
    #   ★ 为什么把两个数**都**报：单看"目标没减少"无法区分
    #       (i) 配额空了（= 明天会自己好） 与
    #      (ii) 抓了但没落库（= 真 bug，明天也不会好）
    #     —— 两者的**修法完全不同**（ERR-META-13/14：不许把两种情形印成同一行）。
    crawl_todo = cur.execute(
        "SELECT count(*) FROM tasks "
        " WHERE opencd_tid IS NOT NULL AND opencd_tid != '' "
        "   AND (mapped_json IS NULL OR mapped_json = '')"
    ).fetchone()[0]
    # ★ 配额账本：`opencd_quota` 表按 (day_key, kind) 记账。★ 表可能不存在
    #   （早期库）⇒ **读不到就说读不到**，绝不当成 0（ERR-SQL-05）。
    try:
        qrow = cur.execute(
            "SELECT used FROM opencd_quota WHERE day_key = ? AND kind = 'request'",
            (time.strftime("%Y-%m-%d"),)).fetchone()
        quota_used = qrow[0] if qrow else 0
        quota_known = True
    except sqlite3.Error:
        quota_used, quota_known = None, False
    con.close()
    return {"stale": stale, "exists": exists, "total": total,
            "max_updated_at": mx, "published": published,
            "crawl_todo": crawl_todo, "quota_used": quota_used,
            "quota_known": quota_known}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--dst", default=None,
                    help="NAS 部署目录（默认按 DST 环境变量 → .deploy.nasrc → 硬编码）")
    ap.add_argument("--disabled", default="",
                    help="逗号分隔：**显式**声明哪些阶段确实关着（它们的 last_run 不动算预期）。"
                         "不给就读 NAS 上的 daemon_env.json 快照；快照也读不到则按空集判"
                         "（= 每个阶段都按'该动就得动'，宁可多报一个待查）")
    args = ap.parse_args()

    dst = pathlib.Path(args.dst) if args.dst else resolve_dst()
    # ★ disabled 的来源分层：显式声明 > 容器快照 > 空集。**每一层都打印来源**，
    #   免得"这次为什么没报 publish"变成一个要翻代码才知道的事。
    snap_vals, snap_raw, snap_age, snap_note = read_env_snapshot(dst)
    if args.disabled:
        disabled = {s.strip() for s in args.disabled.split(",") if s.strip()}
        missing = set()
        disabled_src = "命令行 --disabled"
    elif snap_vals:
        # ★★ 三态，**不是**两态（2026-09-22 修）：`True`=开着 / `False`=明确关着
        #    / **`None`=快照里没有这个键 ⇒ 「未声明」，不是「没关」**。
        #
        #    原先写成 `if snap_vals.get(key) is False` ⇒ 键不存在时是 `None`，
        #    `None is False` 为假 ⇒ 那个阶段被当成"开着"。**这是错的**：
        #    `ENABLE_CRAWL` 默认 False 且生产 `.env` 里压根没这一行（ERR-ENV-02）
        #    ⇒ 它不在 `os.environ` ⇒ 快照（刻意按白名单 `if k in os.environ` 写，
        #    见 `daemon.save_env_snapshot`）**正确地略过它** ⇒ 下游拿到的"缺位"
        #    被 `is False` 吞成了"没关"。
        #    ★ 后果不是"少报一个"而是"报一个假的"：crawl 其实没在跑，却被当开着
        #      ⇒ 补上 INTERVALS 里的 crawl 之后，它会每轮报「★ 超过周期」。
        #    ⇒ 缺位必须如实传播：**未知的一律按"该动就得动"判**（宁可多报一个待查）。
        #    ⇒ **这里直接摸 `snap_vals` 是安全的、且必须的**：本循环的键**全部**来自
        #      `STAGE_ENABLE_KEY`，而它们**每一个**在 `ENV_KEY_TYPE` 里都是 `"bool"`。
        #      `typed()` 对它们只会返回同一个值 ⇒ 多绕一层反而看不出"三态"。
        #      ★ 若将来往 `STAGE_ENABLE_KEY` 加**非布尔**的阶段开关，这里就会漂 ——
        #        所以下面加一句**断言式校验**，让漂移当场报错而不是静默出错（ERR-META-01）。
        _bad = {k for k in STAGE_ENABLE_KEY.values()
                if ENV_KEY_TYPE.get(k) != "bool"}
        assert not _bad, f"STAGE_ENABLE_KEY 里出现了非布尔键，请改用 typed()：{_bad}"
        disabled = {st for st, key in STAGE_ENABLE_KEY.items()
                    if snap_vals.get(key) is False}
        missing = {st for st, key in STAGE_ENABLE_KEY.items()
                   if snap_vals.get(key) is None}
        disabled_src = (f"容器快照 {ENV_SNAPSHOT}（{snap_age:.0f}s 前；"
                        f"ENABLE_PUBLISH="
                        f"{typed(snap_vals, snap_raw, 'ENABLE_PUBLISH')}）")
    else:
        disabled = set()
        missing = set(STAGE_ENABLE_KEY)
        disabled_src = f"**空集**（{snap_note or '无快照'}）⇒ 每个阶段都按'该动就得动'判"
    if missing:
        # ★ 把"快照里没有这个键"显式报出来 —— 它**不是**一个可以静默忽略的细节：
        #   要么是那个阶段真没配（如 crawl 的生产 `.env`），要么是快照机制/键名漂了。
        disabled_src += (f"  ⚠ 快照里没有这些键 ⇒ 按'该动就得动'判（未声明≠关着）："
                         f"{'、'.join(sorted(missing))}")

    now = time.time()
    if args.reset and SNAP.exists():
        SNAP.unlink()

    state = read_state(dst)
    hb_content, hb_mtime = read_heartbeat(dst)
    db = read_db(dst)

    # ---------- 精确计算 ----------
    # ★★ 2026-09-26：把 `prev_snap`（上次快照）**提前到这里**读 —— 因为下面的
    #    `crawl` 空转判据要用它拿"上一轮的目标行数"。原先它在心跳那一节才读，
    #    那会让本循环引用一个**尚未赋值**的名字（`NameError`）。
    #    ★ 它是**只读一次**的：心跳那节复用它，不再自己读一遍（同一个量只取一处）。
    prev_snap = json.loads(SNAP.read_text(encoding="utf-8")) if SNAP.exists() else None
    # ★★★ 2026-09-27：周期**不再直接用硬编码的 `INTERVALS`** —— 改成"快照优先、
    #   硬编码兜底"，并把每个值**从哪来**记下来印出去（见 `resolve_intervals`）。
    #   ★ 为什么：本机读不到 NAS 的 `.env`（ERR-ENV-02）⇒ 硬编码表**必然是过期副本**；
    #     实测它写着 `publish 1800` 而真值 `5400`。⇒ 判据来源必须换成**容器自己落的快照**。
    iv_map, iv_src, iv_warns = resolve_intervals(snap_vals, snap_raw, snap_note, snap_age)
    rows = []
    for stage, iv in iv_map.items():
        st = state.get(stage)
        if not st or st.get("last_run") is None:
            rows.append((stage, None, iv, None, "❔ state 里没有这个阶段"))
            continue
        age = now - st["last_run"]
        if stage in disabled:
            # ★ 这里说"**据快照/显式声明**关闭"，不说"已禁用" —— 因为它的依据是
            #   容器自己落的快照，而快照可能是过期的（过期时 read_env_snapshot
            #   会返回空 dict，根本走不到这个分支）。措辞带上来源，读的人才知道
            #   这条判定的可信度。
            verdict = "⏹️ 关着（据上方来源；last_run 不动是预期）"
        elif age <= iv:
            verdict = "✅ 在周期内"
        else:
            verdict = f"★ 超过周期 {fmt_age(age - iv)}"
        # ★ 快照里**没有这个键** ⇒ 未声明。此时 `age > iv` 是**真异常**
        #   （不是"它关着所以不动"），但也要标注依据弱 —— 我们只知道"没声明"，
        #   不知道它到底是关的还是漏配了。别让读的人把"未声明"当成"已确认开着"。
        if stage in missing and age > iv:
            verdict += " 「未声明」⇒ 该动就得动，此超期为真异常（快照里无此键）"
        # edit 的例外：有活时按 cooldown 连跑
        #
        # ★★ 2026-09-24：这里**只解释 `edit` 自己的调度状态**，
        #    **不评价「stale 待办」那个数**。两者很容易被读成同一件事，摊开写清：
        #      · `no_progress` 是 `daemon.py` 的**调度**标志（`before > 0 and after >= before`）
        #        ⇒ 含义是"本轮跑了、但待办没降" ⇒ 退回 `EDIT_INTERVAL`(6h) 长周期。
        #        **这是设计内的降级**（改成"有活就 60s 冷却"会变成每 60s 敲站点，更糟），
        #        不是故障 —— 所以这里说"正常"是对的。
        #      · 而下面 `【库】` 的 `stale 待办` 是**库的口径**（`db.STALE_CONDITION`），
        #        跟 `no_progress` **不是同一个判据**，两者**不必同步**：
        #        实测 2026-09-24（43 = 1 条 `site_synced_at IS NULL` + 42 条
        #        `updated_at > site_synced_at`，差值 0.01~14 天 = **持续灌注**，非同源批次）。
        # ★★★ 因此：**`stale 待办` 的"数量本身不是判据"** ——
        #    它大不等于坏、小不等于好。要看的是**差值分布**与**成因拆解**
        #    （是 site_synced_at 为 NULL，还是 updated_at 更新过 —— **两个方向的修法不同**）。
        #    ★ 而且它与「补了但没推上去」那件事**方向相反**（那件是漏判 stale，
        #      因为写库工具**刻意不碰 `updated_at`** ⇒ `STALE_CONDITION` 读不到"我变新了"）；
        #      ⇒ 看到 `stale` 先问是哪个方向，别直接归因（`SESSION-NOTES.md` 有整节）。
        # ★ 定性已写进 `SESSION-NOTES.md`「2026-09-24（夜·续）」；此处只留**判读纪律**。
        if stage == "edit" and st.get("no_progress"):
            verdict += (" · no_progress=true（edit 调度退回长周期，正常；"
                        "★ 与下方 stale 待办**不是同一判据**）")
        # ══════════════════════════════════════════════════════════════
        # ★★★ 2026-09-26 加：`crawl` 的「空转」判据（防它再骗过本脚本一次）
        #
        #   上面那条 `age <= iv ⇒ ✅ 在周期内` 只说明**调度到了**，
        #   不说明**抓到了** —— 实测它每轮 1.4 秒、零产出的那晚，
        #   本脚本报的就是 ✅。这里补上真判据：
        #       「配额已满 且 目标行数没减少」⇒ 空转（★ 明确说出来）
        #   ★ 只有**两个数都拿到**时才下这个结论；任一读不到 ⇒ 如实说"判不了"
        #     （ERR-SQL-05 / ERR-META-14：读不到不许印成"没问题"）。
        # ══════════════════════════════════════════════════════════════
        if stage == "crawl":
            qt = db.get("crawl_todo")
            qu, qk = db.get("quota_used"), db.get("quota_known")
            _max = os.getenv("OPENCD_MAX_PER_DAY", "50")
            try:
                _maxn = int(float(_max))
            except ValueError:
                _maxn = None
            # ★ 上一轮快照里的那个数（没有上一轮 ⇒ 不判"没减少"）
            _prev_todo = (prev_snap or {}).get("db_crawl_todo") \
                if isinstance(prev_snap, dict) else None
            _fat = (qk and qu is not None and _maxn is not None and qu >= _maxn)
            _stuck = (_prev_todo is not None and qt is not None
                      and qt >= _prev_todo)
            if _fat and _stuck:
                verdict += (f" · ★★ **空转**：配额 {qu}/{_maxn} 已满，"
                            f"而目标 {_prev_todo}→{qt} 没减少 ⇒ "
                            f"本轮**零产出**（明天换天自动恢复；★ 若明天配额未满而目标仍不动，"
                            f"那才是真 bug）")
            elif _fat:
                verdict += (f" · ★ 配额 {qu}/{_maxn} 已满 ⇒ 本轮**发不出请求**"
                            f"（目标 {qt} 条待补；明天换天恢复）")
            elif not qk:
                verdict += " · ❔ 配额账本**读不到** ⇒ 无法判'在不在做功'（不当成 0）"
            elif _prev_todo is None:
                verdict += f" · （目标 {qt} 条待补；配额 {qu}/{_maxn}。首次采样，不判增减）"
            elif qt < _prev_todo:
                verdict += (f" · ✅ **在推进**：目标 {_prev_todo}→{qt}"
                            f"（配额 {qu}/{_maxn}）")
            else:
                verdict += (f" · ★★ **目标没减少**（{_prev_todo}→{qt}）"
                            f"而配额只用 {qu}/{_maxn} ⇒ ★ 疑似**抓了没落库**（真 bug）")
        rows.append((stage, age, iv, st, verdict))

    hb_age = now - max(hb_mtime, hb_content or 0)

    # ---------- ★★★ 心跳判活：两次采样比「有没有前进」 ----------
    #
    # ★ 为什么不用"年龄 < 阈值"当判据（2026-09-24 实测踩过，见文件头）：
    #   阶段运行期间心跳**不写**（beat() 在循环头、run_stage() 同步阻塞）
    #   ⇒ 年龄大**不蕴含**进程停。实测误报过一次：640s 被报成"可能已停/卡住"，
    #   而下一采样 3s 前刚跳。
    #
    # ★★ 所以判据是 `ERR-SQL-04` 的原话：**看它有没有在*前进*** ——
    #   与上一次采样（`SNAP`）比 `hb_content` 有没有**变化**：
    #     · 变了        ⇒ ✅ 活着（**这是唯一正向判据**）
    #     · 没变、且已经过了足够长的窗口 ⇒ ★★ 可疑（不再是"可能"，是"两次采样间没动"）
    #     · 没有上一次采样 ⇒ ❔ **不判**（单点给不出这个结论，不自称能判）
    #
    # ★ 窗口：`SCHED_TICK`(30s) 是心跳的**下限**间隔，但一轮可能被阶段阻塞很久
    #   ⇒ 保守取 `HEARTBEAT_FRESH`(120s) 作为"没变就值得说一声"的窗口。
    #   ★ 窗口**长于**心跳周期是必须的（`ERR-META-09`：窗口短于静默期，
    #     "没动"就是个假信号）。
    prev_hb = (prev_snap or {}).get("heartbeat_epoch")
    prev_at = (prev_snap or {}).get("checked_at")
    if hb_content is None:
        hb_verdict = ("⚠️ 心跳内容不是数字（读到 " + repr(hb_content) + "）"
                      " ⇒ 无法判前进，只看文件 mtime")
        alive = None
    elif prev_hb is None or prev_at is None:
        hb_verdict = ("❔ 不判：没有上一次采样的心跳值可比"
                      "（★ 单次采样给不出「有没有前进」，不要用一个阈值假装能判）")
        alive = None
    else:
        # ★★ 判据只用**两个同源的量**：心跳值「有没有前进」+ 心跳**自身**的年龄。
        #
        #   ★ 第一版拿 `now - prev_at`（距上次采样的间隔）当窗口，是**量错了对象**：
        #     那个间隔只说明"**我**多久没来采样"，与 daemon 状态**无关**。
        #     实测它造出一句自相矛盾的话：「600s 内心跳没前进（距今 21s）」——
        #     一个 21 秒前刚写心跳的进程不可能同时"静默了 600s"。
        #     第二版改成 `max(gap, hb_age)` 也没用：gap=600 一乘就把 hb_age=21 盖住了。
        #     ⇒ 根因不是阈值，是**判据里混进了一个与结论无关的量**（ERR-META-13）。
        #
        #   ★★ 正确的三态（全部只用心跳自己的时间戳）：
        #     前进过                     → ✅ 活着（唯一正向判据）
        #     没前进，且 hb_age 已够大   → ★★ 可疑（它**真的**静默了这么久）
        #     没前进，但 hb_age 还小     → ❔ 判不了（只是我没等到；它刚动过）
        #
        #   ★ 阈值 `HEARTBEAT_FRESH`(120s) 相对"单阶段最长耗时"（STAGE_TIMEOUT 可达
        #     3600s）**明显偏小** ⇒ 它是"宁可多报一次待查"的保守值，不是精确边界。
        #     拿不到 STAGE_TIMEOUT（不在快照白名单）⇒ 只能如实标注这个保守性。
        if hb_content > prev_hb:
            hb_verdict = (f"✅ 活着：心跳前进过 {hb_content - prev_hb}s"
                          f"（上次 {cst(prev_hb)} → 本次 {cst(hb_content)}）")
            alive = True
        elif hb_age < HEARTBEAT_FRESH:
            hb_verdict = (f"❔ 判不了：心跳与上次采样相同，但它**刚刚**写过"
                          f"（距今 {hb_age:.0f}s < {HEARTBEAT_FRESH}s）"
                          f"⇒ 只是本次没等到它动")
            alive = None
        else:
            hb_verdict = (f"★★ 可疑：心跳没前进（停在 {cst(hb_content)}，"
                          f"距今 {fmt_age(hb_age)}）且已超 {HEARTBEAT_FRESH}s"
                          f" ⇒ 两次采样间它没动过。★ 阈值偏保守，"
                          f"若恰有长阶段在跑（STAGE_TIMEOUT 可达 3600s）会误报，"
                          f"请隔 2 分钟再采一次定论")
            alive = False

    # ---------- 与上次比对：只报变化 ----------
    cur_snap = {
        # ★★ 存心跳值**本身**（不是一个布尔）—— 布尔把"有没有前进"这个信息丢了，
        #    而那正是唯一的正向判据（ERR-SQL-04）。留 `heartbeat_alive` 只为兼容
        #    **旧快照**的比对（旧文件里没有 `heartbeat_epoch`）。
        "checked_at": now,
        "heartbeat_epoch": hb_content,
        "heartbeat_alive": alive,
        "state_last_run": {k: (state.get(k) or {}).get("last_run")
                           for k in INTERVALS},
        "db": db,
        # ★★ 2026-09-26：把 crawl 的两个做功判据**单独存成顶层键**（而不是只埋在
        #    `db` 里）—— 因为 `crawl` 那行的判定要按**顶层键**读上一轮的值
        #    （`db_crawl_todo`）。存进 `db` 会让下一轮得先判"上一轮的 db 里有没有
        #    这个子键"，多一层三态判断 ⇒ 直接放顶层，缺失即 None，语义最清楚。
        "db_crawl_todo": db["crawl_todo"],
        "db_quota_used": db["quota_used"],
    }
    prev = prev_snap          # ★ 上面判心跳时已读过一次，不重复读（同一次运行内必须同一份）

    changes = []
    if prev is None:
        changes.append("（首次运行，无基线可比 —— 下面是全量）")
    else:
        # ★ 只在**两次都能判**时才报翻转；`None`（不判）不参与"翻转"叙事 ——
        #   否则会把"从不判 → 活着"说成一次状态翻转，那是凭空制造事件。
        if alive is not None and prev.get("heartbeat_alive") is not None \
                and prev.get("heartbeat_alive") != alive:
            changes.append(f"心跳「有没有前进」的判定翻转："
                           f"{prev.get('heartbeat_alive')} → {alive}"
                           f"（★ 判据是两次采样的心跳值比较，不是年龄阈值）")
        for k in INTERVALS:
            a = (prev.get("state_last_run") or {}).get(k)
            b = cur_snap["state_last_run"][k]
            if a != b:
                changes.append(f"{k}.last_run 推进了：{cst(a) if a else '(无)'} → "
                               f"{cst(b) if b else '(无)'}")
        for k in ("stale", "exists", "total"):
            # ★ 取值到局部变量，条件与消息体**共用同一个值**。
            #   原写法是 `if (prev.get("db") or {}).get(k) != db[k]` 配
            #   `prev['db'].get(k)` —— 条件用了安全取值、消息体却裸下标
            #   ⇒ 只要快照**缺 `db` 键**（旧 schema / 文件被截断）：
            #     条件里 `{} != db[k]` 几乎必真 ⇒ 进分支 ⇒ `KeyError` 崩溃。
            #   ★ 崩在一个**只有一条错信息**的地方，而错信息的两个来源不同源
            #     —— 这正是不该用两种写法取同一个值的原因（ERR-META-13 一族）。
            prev_db = (prev.get("db") or {})
            old_v = prev_db.get(k, "(快照无此键)")
            if old_v != db[k]:
                changes.append(f"库 {k}：{old_v} → {db[k]}")

    # ---------- 输出 ----------
    print("=" * 72)
    print(f"巡检时刻   {cst(now)} CST   (epoch {now:.0f})")
    print("=" * 72)

    print("\n【心跳】daemon_heartbeat")
    print(f"  内容 epoch   {hb_content}  → {cst(hb_content) if hb_content else '(非数字)'}")
    print(f"  文件 mtime   {cst(hb_mtime)}")
    # ★ 年龄照报，但**不把它当判据** —— 它的含义是"距上次心跳多久"，
    #   而阶段运行期间心跳本就不写。判据在下一行（两次采样比前进）。
    print(f"  年龄         {fmt_age(hb_age)}   (参考值：> {HEARTBEAT_FRESH}s "
          f"只说明'可能有阶段在跑'，**不**等于 daemon 停了)")
    print(f"  ⇒ {hb_verdict}")

    print("\n【各阶段】state 里该动的有没有动")
    print(f"  「关着」的判据来源：{disabled_src}")
    # ★★★ 2026-09-27：**周期的来源必须印出来** —— 上面这行判定的每一格都要用它
    #   （`age <= iv` 里的 `iv` 就是它）。不印来源 ⇒ 读的人不知道"周期"是**实测**
    #   还是**过期副本**，而这正是 2026-09-27 那次误导的来源（`publish 1800` vs 真值 `5400`）。
    _sc = sorted({v for v in iv_src.values()})
    print(f"  「周期」的判据来源：{' ／ '.join(_sc) if len(_sc) <= 3 else '逐阶段如下'}")
    for _w in iv_warns:
        print(f"      {_w}")
    if len(_sc) > 3:
        for _st in iv_map:
            print(f"      · {_st:<8} {iv_src.get(_st)}")
    print(f"  {'阶段':<9}{'间隔':>8}  {'last_run (CST)':<21}{'年龄':<26}判定")
    for stage, age, iv, st, verdict in rows:
        # ★★★ 2026-09-27 修：原先写成 `iv < 3600 → 秒 : 否则 iv//3600 小时`
        #   ⇒ `5400` 被印成 `1h`（**整数除法静默截断**）。而 `5400` 恰好是
        #   publish 的真值，也就是说**这条打印错误正好落在这个值上** ——
        #   一个"看着像 1 小时"的 90 分钟，比不打印更坏（`ERR-META-01` 一族：
        #   显示口径自己失真，读的人却拿它当判据用）。
        #   实测症状：publish 那行印 `1h`，而它实际比 `sync 24h` 的量级判断还少一位。
        # ★ 修法：能整除就除以 3600，不能就**如实写分钟+秒**，绝不截断。
        if iv % 3600 == 0:
            ivs = f"{iv // 3600}h"
        elif iv >= 3600:
            ivs = f"{iv // 3600}h{(iv % 3600) // 60}m"
        elif iv % 60 == 0:
            ivs = f"{iv // 60}m"
        else:
            ivs = f"{iv}s"
        lr = cst(st["last_run"]) if st and st.get("last_run") else "-"
        ages = fmt_age(age) if age is not None else "-"
        print(f"  {stage:<9}{ivs:>8}  {lr:<21}{ages:<26}{verdict}")

    # ---------- ★★ 自动发布的第二把钥匙（2026-09-24） ----------
    #
    # ★ 为什么巡检要专门报这一条：`publish.py` 的 `guard_auto_key()` 只在
    #   **无人值守路径**（`--auto`）上校验 `PUBLISH_I_AM_HERE`。
    #   ⇒ 两个开关**都为真**才会自动发。而"巡检看不见的开关 = 会被当成没配"
    #     —— 那正是 `ENABLE_PUBLISH` 被连续多轮写反的同一个形状（ERR-META-04）。
    #
    # ★ 三态，**不是**两态（照抄上面 `missing` 那段的教训）：
    #     键在且为真  → ✅ 自动发布会真发（★ 这句要显眼）
    #     键在且为假  → ⛔ 钥匙在但值是错的 ⇒ **每轮都会被拒**（值得报）
    #     键**不在**  → ❔ 未声明 —— ★ 这**不等于**"关了"：变量不存在时
    #                   `guard_auto_key()` 走的是"根本没配"那一支 ⇒ 同样**不发**，
    #                   但原因不同（"还没配" vs "配错了"，修法不一样）。
    #   ★ 一句话：**三种状态里只有第一种会真发**，所以这一栏必须逐字说清是哪种。
    # ★ 三态，**不是**两态（照抄上面 `missing` 那段的教训）：
    #     键在且**字符串等于约定值** → ✅ 放行
    #     键在但值不对               → ⛔ 每轮被拒（值得报）
    #     键**不在**                 → ❔ 未声明 —— ★ 这**不等于**"关了"：
    #                   变量不存在时 `guard_auto_key()` 走"根本没配"那一支 ⇒
    #                   同样**不发**，但原因不同（"还没配" vs "配错了"，修法不一样）。
    # ★★ 判"对不对"**必须比字符串**，不能借 `snap_vals`（那是布尔口径）——
    #    这条键的值是 `i-am-here`，按布尔转会得到假 False（2026-09-24 实测踩过）。
    #    ★ 现在走 `typed()`：`ENV_KEY_TYPE['PUBLISH_I_AM_HERE'] == "str"` ⇒
    #      取 `raw` 视图。**口径由表决定，不靠这段注释**（第三次踩错后的改动）。
    key_raw = typed(snap_vals, snap_raw, AUTO_KEY_ENV)
    key_ok = (key_raw is not None
              and str(key_raw).strip() == AUTO_KEY_VALUE)
    if not snap_raw:
        key_state = "不可用（快照没读到）"
    elif key_ok:
        key_state = "✅ 已配且正确 ⇒ ★ 自动发布会真发"
    elif key_raw is None:
        key_state = "❔ 未声明（变量不存在）⇒ 按『根本没配』处理，不发"
    else:
        # ★ 不回显值（哪怕它不是密钥）—— 回显就会写进巡检记录；只报"不对"。
        key_state = "⛔ 已配但值不对 ⇒ 每轮都会被拒（退出码 3）"

    pub_on = typed(snap_vals, snap_raw, "ENABLE_PUBLISH") if snap_vals else None
    if pub_on is None:
        auto_pub = "❔ 判不了：ENABLE_PUBLISH 读不到（见上方快照来源）"
    elif not pub_on:
        auto_pub = "⏹️ 不会自动发：ENABLE_PUBLISH=false（阶段压根不排）"
    elif key_ok:
        auto_pub = "★★ 会自动发：两把钥匙都成立 —— 下一轮 publish 到期即提交"
    else:
        auto_pub = "⛔ 不发：ENABLE_PUBLISH=true 但第二把钥匙没通过（每轮退避重试）"

    # ---------- ★★ 调度语义：`RUN_ON_START`（2026-09-27 加） ----------
    #
    # ★ 为什么要**单独报**这一条：它**不是门控开关**，它改变的是**"时间"的含义**：
    #     · `true`  ⇒ 启动时把**所有** `last_run` 清 0 = 立刻全跑一轮（daemon.main()）
    #     · `false` ⇒ **沿用** state 里的旧 `last_run`，按 `now - last < interval` 判到期
    #   ⇒ 容器刚重建完"为什么某阶段没跑"**完全由它决定**。
    # ★ 实测代价（2026-09-27）：这个键原先**不在快照白名单里** ⇒ 本机看不见它
    #   ⇒ 我把"日志里没有 scan"读成了「重启后停摆」并建议改代码，真因正是
    #   `RUN_ON_START=false`（沿用旧 `last_run`，scan **尚未到期**）。
    #   ⇒ 判据不存在时，结论只能靠猜（`ERR-META-01`：判据要可执行，不能靠记忆）。
    # ★ 三态，照抄上面的教训：键在且真 / 键在且假 / **键不在**（= 未落盘，
    #   旧版 daemon 的旧快照）—— 第三种**不许**当成 `false`，那会把"看不见"说成"没配"。
    print("\n【调度语义】RUN_ON_START —— 决定「启动算不算时间起点」")
    _ros = typed(snap_vals, snap_raw, "RUN_ON_START") if snap_raw else None
    if not snap_raw:
        print("  ❔ 读不到快照 ⇒ **判不了**（不许按 false 猜；登 NAS 用 "
              "`docker exec opencd2musopia env` 问）")
    elif _ros is None:
        print("  ❔ 快照里**没有这个键** ⇒ 本快照是 2026-09-27 之前落的（该键当天才进白名单）"
              "；⇒ **判不了**，等容器重建后重跑本巡检")
    else:
        print(f"  RUN_ON_START = {_ros}"
              f"   ⇒ {'启动**立刻全跑一轮**（清空全部 last_run）' if _ros else '启动**不补跑**，沿用 state 里的旧 last_run（按周期判到期）'}")
        if not _ros:
            print("     ★ 读法：所以「刚重建完某阶段没跑」**可能完全正常** —— 先算 "
                  "`last_run + 该阶段周期` 到没到，**别**拿「日志里有没有」当判据")

    print("\n【自动发布】两把钥匙都成立才会真提交（不可逆，见 ERR-META-12）")
    print(f"  ENABLE_PUBLISH      = "
          f"{pub_on if snap_vals else '(读不到)'}"
          f"   （布尔口径）")
    # ★ 这一行**直接报"与约定值是否相等"**，不把原始值当布尔使 ——
    #   两个口径分开印，读的人一眼能看出"哪个键按哪个口径判"。
    print(f"  {AUTO_KEY_ENV:<19} = "
          f"{'(读不到)' if not snap_raw else ('与约定值相等 ✅' if key_ok else '与约定值不等')}"
          f"   （**字符串**口径，不作布尔转换）")
    print(f"  第二把钥匙   {key_state}")
    print(f"  ⇒ {auto_pub}")

    # ---------- ★★ 发布**速率**：两道旋钮 + 由此推出的每日上限 ----------
    #
    # ★ 为什么把这段**算出来**印在这里：2026-09-24 用户问「现在每天最多发布多少条」，
    #   而那个答案当时**必须靠人脑算**（`86400 / PUBLISH_INTERVAL × MAX_PER_CYCLE`）
    #   —— 而人算正是本项目反复吃亏的地方（`ERR-META-03`：算错的数字**不报错**，
    #   只给出一个看着合理的值）。⇒ 让巡检**直接报结论**，且把它**依据的两个原始值
    #   一起印出来**，读的人能自己复核，不必信这个乘积。
    #
    # ★★ 两道旋钮**分工不同**，所以分两行报、**不合成一个"限速"数**：
    #     · `PUBLISH_MAX_PER_CYCLE` × 周期 ⇒ **日上限**（量）
    #     · `PUBLISH_MIN_GAP`             ⇒ **突发形态**（节奏）；它**不影响**日上限
    #   把两者说成同一个数，就是本轮 `.env.example` 里专门警告过的那处**最易读错**的坑。
    #
    # ★ 读法：这两个键 2026-09-24 才进 `ENV_SNAPSHOT_KEYS` 白名单 ⇒
    #   **旧快照里没有它们**（那正是白名单的可见性设计：缺了看得见）。
    #   缺键时**不许**按兜底值假装算出一个日上限 —— 那会给出一个**看着精确的错数**
    #   （`PUBLISH_MAX_PER_CYCLE` 的兜底还是 `0 = 不限`，套进来会算出"无限"，荒谬）。
    # ★★★ 必须从 `snap_raw`（**字符串**视图）读，**不能**用 `snap_vals` ——
    #   那是**布尔**口径（`read_env_snapshot` 的 docstring 明写：
    #   "字符串键必须从 raw 读"）。这两个键是**整数**，不是开关：
    #     `"1"`   → 布尔视图 `True`  ⇒ 印出来是 `True` 而不是 `1`（**2026-09-24 实测踩过**）
    #     `"120"` → 布尔视图 `True`  ⇒ 拿它算"间隔"就得到一个**假的 1**
    #   ⇒ 与 `PUBLISH_I_AM_HERE` 被按布尔转是**同一个错**，只是这次错的是整数。
    #   ★ 现在走 `typed()`：这三个键在 `ENV_KEY_TYPE` 里都是 `"int"` ⇒ 取 `raw` 视图。
    iv_raw = typed(snap_vals, snap_raw, "PUBLISH_INTERVAL")
    per_cycle_raw = typed(snap_vals, snap_raw, "PUBLISH_MAX_PER_CYCLE")
    gap_raw = typed(snap_vals, snap_raw, "PUBLISH_MIN_GAP")

    def _as_int(v):
        """字符串 → int；None/空/非数字 ⇒ None（**不猜默认值**，见下）。"""
        if v is None or str(v).strip() == "":
            return None
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None

    iv_s = _as_int(iv_raw)
    per_cycle = _as_int(per_cycle_raw)
    gap_s = _as_int(gap_raw)
    # ★ `PUBLISH_INTERVAL` 不在快照白名单里（它是周期，不是开关）⇒
    #   退到本文件**自己的** `INTERVALS`（同一份周期口径，见文件头）。
    #   ★ 不静默用兜底假装算：拿不到就报"判不了"（另见下面那句注释）。
    if iv_s is None and INTERVALS.get("publish"):
        iv_s = INTERVALS["publish"]

    print("\n【发布速率】日上限 = (86400 / PUBLISH_INTERVAL) × PUBLISH_MAX_PER_CYCLE")
    if per_cycle is None or iv_s is None:
        miss = []
        if per_cycle is None:
            miss.append("PUBLISH_MAX_PER_CYCLE")
        if iv_s is None:
            miss.append("PUBLISH_INTERVAL")
        print(f"  ❔ 判不了：{'、'.join(miss)} 读不到"
              f"（后者不在快照白名单，取自 state 的间隔）")
        print("     ★ 不按兜底值硬算 —— 兜底是 0 = 不限，会算出『无限』这种假数")
    else:
        try:
            n_rounds = 86400 / int(iv_s)
            cap = int(n_rounds) * int(per_cycle)
            print(f"  PUBLISH_INTERVAL      = {iv_s}s "
                  f"（{n_rounds:.0f} 轮/天）")
            print(f"  PUBLISH_MAX_PER_CYCLE = {per_cycle}")
            if int(per_cycle) == 0:
                print(f"  ⇒ ★★ 每日上限 = **不限**（每轮 0 = 不限）← 注意这是最危险的值")
            else:
                print(f"  ⇒ ★★ 每日上限 ≈ **{cap} 条/天**")
        except (ValueError, TypeError):
            print(f"  ❔ 读到的值不是整数：interval={iv_s!r} max_per_cycle={per_cycle!r}")
    # ★ 条间隔单独一行，并**显式说明它不改变上面的日上限** ——
    #   这是本文件最容易读错的一处，必须写在数字旁边而不是别处。
    if gap_s is None:
        print(f"  PUBLISH_MIN_GAP       = ❔ 读不到（旧快照 / 未配）")
    else:
        try:
            g = int(float(gap_s))
        except (ValueError, TypeError):
            g = None
        if g is None:
            print(f"  PUBLISH_MIN_GAP       = ⚠️ 值不是数字：{gap_s!r} ⇒ 按 0 处理")
        elif g <= 0:
            print(f"  PUBLISH_MIN_GAP       = {g}（0 = 不限 ⇒ 条与条之间只有 1.5s 地板）")
        else:
            print(f"  PUBLISH_MIN_GAP       = {g}s ⇒ 一批 N 条要摊开 (N-1)×{g}s")
            print(f"     ★ 它**不改变**上面的日上限：{g}s×5 = {g * 5 / 60:.0f} 分钟"
                  f"仍塞得进一轮 ⇒ 只管突发形态，管不了总量")

    print("\n【库】SMB 只读直连 (PRAGMA query_only=1)")
    print(f"  总记录        {db['total']}")
    print(f"  site EXISTS   {db['exists']}")
    # ★ 已上站条数：**单调增** ⇒ 两次巡检之差 = 本轮真发出去的条数。
    #   ★ 与下面那个变化检测配合看（它会在差值 >0 时报出来）。
    print(f"  PUBLISHED     {db['published']}   (已上站的条数；★ 只到"
          f"「上站」为止，**不**代表 qB 里在做种)")
    print(f"  stale 待办    {db['stale']}   (口径 = db.py 的 STALE_CONDITION)")
    # ★★ 2026-09-26 加：crawl 的**做功**判据（配额 + 目标行数）。
    #   ★ 单看任何一个都不够：见 read_db() 里那段长注释（两种情形的修法不同）。
    _q = db["quota_used"]
    _qn = db["quota_known"]
    print(f"  crawl 目标    {db['crawl_todo']}   (有 opencd_tid 但 mapped_json 空"
          f"的皇后行 ⇒ ★ 这是 crawl 的**待办**；★ 上面阶段表里 crawl 那行会判它在不在推进)")
    print(f"  配额(今天)    "
          + (f"{_q} / {os.getenv('OPENCD_MAX_PER_DAY', '50')}"
             f"   (账本 = data.db:opencd_quota；★ 满 ⇒ crawl 本轮出不了网)"
             if _qn else
             "❔ **读不到**（opencd_quota 表不在 / 读失败）⇒ ★ 不许当成 0"))
    print(f"  max(updated_at)  {db['max_updated_at']}  ← ★ 这是 **UTC**，加 8h = "
          f"{cst(_utc_to_epoch(db['max_updated_at'])) if db['max_updated_at'] else '-'} CST")

    print("\n【与上次相比的变化】")
    if changes:
        for c in changes:
            print(f"  • {c}")
    else:
        print("  无变化")

    SNAP.write_text(json.dumps(cur_snap, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _utc_to_epoch(s):
    """把 data.db 里的 'YYYY-MM-DD HH:MM:SS'(UTC) 转 epoch。"""
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S")) - time.timezone
    except Exception:
        return None


if __name__ == "__main__":
    main()
