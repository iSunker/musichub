"""
容器化编排器：常驻进程，按各自周期依次调度
scan / publish / sync / covers / edit / vtid / crawl。

调度特性与设计：
1. 阶段串行调度与互斥控制，避免多进程并发读写导致 SQLite 数据库锁定。
2. 每个阶段作为独立子进程执行，单个阶段异常不影响编排器主循环。
3. 阶段执行时间戳持久化存储，避免重启时重复全量触发。
4. 连续失败采用指数退避机制，降低异常状态下的无效请求频率。
5. 优雅停机：捕获 SIGTERM 信号后等待当前子进程完成后安全退出。
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

import config

STATE_FILE = os.path.join(config.DATA_DIR, "daemon_state.json")
HEARTBEAT_FILE = os.path.join(config.DATA_DIR, "daemon_heartbeat")

# 收到 SIGTERM/SIGINT 后置位，主循环与子进程等待处都会检查
_shutdown = False


def _on_signal(signum, frame):
    global _shutdown
    _shutdown = True
    log(f"收到信号 {signum}，等当前阶段结束后退出（不会中途打断正在提交的请求）")


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def env_int(key, default):
    raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        log(f"⚠️  {key}={raw!r} 不是数字，用默认值 {default}")
        return default


def env_bool(key, default):
    raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


# ==================== 阶段定义 ====================

def build_stages():
    """
    按执行优先级从上到下排列。同一轮里多个阶段同时到期时，就按这个顺序串行跑，
    保证"扫描 -> 发布 -> 回填 ID -> 补封面 -> 编辑页面"的依赖不被打乱。
    """
    import db   # 只为一件事：给 edit 阶段提供"待办数"探针（见下面的 pending）
    dry_run = env_bool("DRY_RUN", False)

    def apply_flag(*flags):
        """DRY_RUN=true 时移除 `--apply`/`--yes`/`--auto` 等生效参数，仅保留只读检查。"""
        if dry_run:
            stripped = [f for f in flags if f in ("--yes", "--auto")]
            if stripped:
                log(f"⚠️  DRY_RUN=true ⇒ 摘掉 {stripped}（只读模式，不执行真实写操作）")
        return [] if dry_run else list(flags)

    stages = []

    # 1) 扫描 qB，抽元数据、生成种子。只读音乐目录 + 写本地库，不碰站点。
    if env_bool("ENABLE_SCAN", True):
        cmd = ["python", "run.py"]
        n = env_int("SCAN_MAX_PER_CYCLE", 0)
        if n:
            cmd += ["--limit", str(n)]
        stages.append({
            "name": "scan",
            "cmd": cmd,
            "interval": env_int("SCAN_INTERVAL", 1800),
        })

    # 2) 发布新种子到站点（--yes 跳过终端交互确认，--auto 声明无人值守路径并校验运行口令）
    if env_bool("ENABLE_PUBLISH", True):
        cmd = ["python", "publish.py"] + apply_flag("--yes", "--auto")
        n = env_int("PUBLISH_MAX_PER_CYCLE", 0)
        if n:
            cmd += ["--limit", str(n)]
        stages.append({
            "name": "publish",
            "cmd": cmd,
            "interval": env_int("PUBLISH_INTERVAL", 1800),
        })

    # 2b) 发布到 CrabPT 站点（独立队列与配置）
    if env_bool("ENABLE_PUBLISH_CRABPT", False):
        cmd = (["python", "publish.py", "--site", "crabpt"]
               + apply_flag("--yes", "--auto"))
        n = env_int("CRABPT_MAX_PER_CYCLE", 1)
        if n:
            cmd += ["--limit", str(n)]
        stages.append({
            "name": "publish_crabpt",
            "cmd": cmd,
            "interval": env_int("CRABPT_INTERVAL", 1800),
        })

    # 猫站（PTerClub）：第三发布站点
    if env_bool("ENABLE_PUBLISH_PTER", False):
        cmd = (["python", "publish.py", "--site", "pter"]
               + apply_flag("--yes", "--auto"))
        n = env_int("PTER_MAX_PER_CYCLE", 1)
        if n:
            cmd += ["--limit", str(n)]
        stages.append({
            "name": "publish_pter",
            "cmd": cmd,
            "interval": env_int("PTER_INTERVAL", 1800),
        })

    # 星湾（xingwan.cc）：第四发布站点，仅每天 00:00-10:00 参与发种
    if env_bool("ENABLE_PUBLISH_XINGWAN", False):
        cmd = (["python", "publish.py", "--site", "xingwan"]
               + apply_flag("--yes", "--auto"))
        n = env_int("XINGWAN_MAX_PER_CYCLE", 1)
        if n:
            cmd += ["--limit", str(n)]
        stages.append({
            "name": "publish_xingwan",
            "cmd": cmd,
            "interval": env_int("XINGWAN_INTERVAL", 1800),
        })

    # 3) 回填 site_torrent_id。编辑的前置条件，频率可以很低。
    if env_bool("ENABLE_SYNC", True):
        stages.append({
            "name": "sync",
            "cmd": ["python", "sync_site.py"] + apply_flag("--apply"),
            "interval": env_int("SYNC_INTERVAL", 86400),
        })

    # 4) 补缺失封面。读取音乐目录并上传图床
    if env_bool("ENABLE_COVERS", True):
        cmd = ["python", "backfill_covers.py"] + apply_flag("--apply")
        n = env_int("COVERS_MAX_PER_CYCLE", 20)
        if n:
            cmd += ["--limit", str(n)]
        stages.append({
            "name": "covers",
            "cmd": cmd,
            "interval": env_int("COVERS_INTERVAL", 21600),
        })

    # 5) 编辑站点页面。--stale-only 只处理本地比站点新的记录
    if env_bool("ENABLE_EDIT", True):
        cmd = ["python", "update_metadata.py", "--stale-only"] + apply_flag("--apply", "--yes")
        n = env_int("EDIT_MAX_PER_CYCLE", 100)
        if n:
            cmd += ["--limit", str(n)]
        if env_bool("EDIT_VERIFY", True):
            cmd.append("--verify")
        cmd += ["--interval", str(env_int("EDIT_REQUEST_INTERVAL", 2))]
        # 每分钟处理请求上限，0 表示不限
        rate = env_int("EDIT_RATE_PER_MIN", 10)
        if rate > 0:
            cmd += ["--rate", str(rate)]
        stages.append({
            "name": "edit",
            "cmd": cmd,
            "interval": env_int("EDIT_INTERVAL", 21600),
            # 探针调度：有待办时使用 cooldown 间隔快速重跑，清零后恢复常规周期
            "pending": db.count_stale,
            "cooldown": env_int("EDIT_COOLDOWN", 60),
        })

    # 5b) 星湾页面编辑（独立站点、独立 cookie；默认关闭）
    if env_bool("ENABLE_EDIT_XINGWAN", False):
        cmd = (["python", "update_metadata.py", "--site", "xingwan", "--stale-only"]
               + apply_flag("--apply", "--yes"))
        n = env_int("EDIT_XINGWAN_MAX_PER_CYCLE", 100)
        if n:
            cmd += ["--limit", str(n)]
        if env_bool("EDIT_VERIFY", True):
            cmd.append("--verify")
        cmd += ["--interval", str(env_int("EDIT_REQUEST_INTERVAL", 2))]
        rate = env_int("EDIT_RATE_PER_MIN", 10)
        if rate > 0:
            cmd += ["--rate", str(rate)]
        stages.append({
            "name": "edit_xingwan",
            "cmd": cmd,
            "interval": env_int("EDIT_XINGWAN_INTERVAL", 21600),
        })

    # 6) VT 回填：从外部数据库按 infohash 同步详情页 ID（tasks.opencd_tid）
    if env_bool("ENABLE_VTID", True):
        stages.append({
            "name": "vtid",
            "cmd": ["python", "sync_opencd_tid.py", "--apply"],
            "interval": env_int("VTID_INTERVAL", 86400),
        })

    # 6b) RSS 反查补 tid：从站点 RSS 按标题搜索并比对 infohash 补充缺失的 opencd_tid
    if env_bool("ENABLE_RSS_TID", False):
        rlimit = env_int("RSS_TID_LIMIT_PER_ROUND", 5)
        cmd = ["python", "rss_backfill_tid.py", "--limit", str(rlimit)]
        if env_bool("RSS_TID_DRY_RUN", True):
            cmd.append("--dry-run")
        else:
            cmd.append("--apply")
        stages.append({
            "name": "rss_tid",
            "cmd": cmd,
            "interval": env_int("RSS_TID_INTERVAL", 86400),
        })

    # 7) 按 tid 抓取站点详情页补齐页面元数据字段
    if env_bool("ENABLE_CRAWL", False):
        limit = env_int("CRAWL_LIMIT_PER_ROUND", 20)
        cmd = ["python", "topup_opencd_fields.py", "--limit", str(limit)]
        if env_bool("OPENCD_CRAWL_DRY_RUN", True):
            cmd.append("--dry-run")
        else:
            cmd.append("--apply")
        stages.append({
            "name": "crawl",
            "cmd": cmd,
            "interval": env_int("CRAWL_INTERVAL", 1800),
        })

    # 8) 免费窗口调度：在免费时段恢复下载，窗口结束后暂停
    if env_bool("ENABLE_FREE_WINDOW", False):
        cmd = ["python", "free_window.py"]
        limit = env_int("FREE_WINDOW_LIMIT", 20)
        if limit:
            cmd += ["--limit", str(limit)]
        if not env_bool("FREE_WINDOW_DRY_RUN", True):
            cmd.append("--apply")
        stages.append({
            "name": "free_window",
            "cmd": cmd,
            "interval": env_int("FREE_WINDOW_INTERVAL", 3600),
        })

    return stages, dry_run

# ==================== 启动前安全检查 ====================

def preflight(stages):
    """
    检查"数据库是空的，但 qBittorrent 里有一堆种子"这种组合。

    这正是自动化最危险的场景：/data 卷挂错或被重建后库是空的，run.py 会把 qB 里
    250 多个早就发布过的种子当成新任务重新处理，publish.py 紧接着把它们重复发一遍。
    人工操作时有确认提示挡着，无人值守下没有，所以必须在这里拦死。

    返回 True 表示可以继续。
    """
    import db

    if getattr(config, "QB_HOST_FROM_DEFAULT", False):
        log(f"⚠️  QB_HOST 未从环境变量读取，正在使用默认值 {config.QB_HOST}")
    else:
        log(f"🔀 QB_HOST = {config.QB_HOST}（来自环境变量）")

    st = db.stats()
    if st["total"] > 0:
        log(f"✅ 预检通过：库内已有 {st['total']} 行记录")
        if st["no_site_id"]:
            log(f"   ⚠️  其中 {st['no_site_id']} 条已发布但缺 site_torrent_id，"
                f"sync 阶段会自动补")
        return True

    # 库是空的。如果 publish 阶段没开，那顶多是白扫一遍，不会污染站点，放行。
    if not any(s["name"] == "publish" for s in stages):
        log("ℹ️  库是空的，但 publish 阶段未启用，继续。")
        return True

    if env_bool("ALLOW_EMPTY_DB", False):
        log("⚠️  库是空的，但 ALLOW_EMPTY_DB=true，按你的要求继续。")
        return True

    log("=" * 60)
    log("🛑 拒绝启动：数据库是空的，同时 publish 阶段已启用。")
    log("")
    log("   这种组合极可能是 /data 卷挂错或被重建了。继续跑的话，run.py 会把")
    log("   qBittorrent 里所有种子当成新任务，publish.py 再把已经发布过的资源")
    log("   重复发一遍——站点上会出现几百个重复种子，且无法批量撤回。")
    log("")
    log("   请先确认 /data 指向的是那份真实的数据库：")
    log(f"     当前 DB_PATH = {config.DB_PATH}")
    log("     docker compose run --rm tools python check_db.py")
    log("")
    log("   如果确实是全新部署、qB 里的种子都还没发过，再显式放行：")
    log("     ALLOW_EMPTY_DB=true")
    log("=" * 60)
    return False


# ==================== 状态持久化 ====================


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)   # 原子替换，避免断电留下半个 JSON
    except OSError as e:
        log(f"⚠️  写状态文件失败: {e}")


# 环境变量白名单快照：将容器内生效的开关配置持久化至 data 目录供巡检观测
ENV_SNAPSHOT_FILE = os.path.join(config.DATA_DIR, "daemon_env.json")
ENV_SNAPSHOT_KEYS = (
    "DRY_RUN",
    "ENABLE_SCAN", "ENABLE_PUBLISH", "ENABLE_SYNC",
    "ENABLE_COVERS", "ENABLE_EDIT", "ENABLE_CRAWL",
    "ENABLE_VTID",
    "ENABLE_RSS_TID", "RSS_TID_DRY_RUN", "RSS_TID_INTERVAL", "RSS_TID_LIMIT_PER_ROUND",
    "OPENCD_CRAWL_DRY_RUN", "ALLOW_EMPTY_DB",
    "ENABLE_FREE_WINDOW", "FREE_WINDOW_DRY_RUN", "FREE_WINDOW_INTERVAL", "FREE_WINDOW_LIMIT",
    "QB_READY_TIMEOUT", "QB_READY_INTERVAL",
    "ENABLE_HANDOFF", "HANDOFF_DRY_RUN", "HANDOFF_SEEDERS_MIN",
    "HANDOFF_SEED_DAYS", "HANDOFF_THREAD_TICK", "HANDOFF_MAX_PER_ROUND",
    "QB_HOST_OPENCD",
    "QB_HOST",
    "ENABLE_SEED_PATROL", "SEED_PATROL_DRY_RUN",
    "SEED_PATROL_THREAD_TICK", "SEED_PATROL_MAX_PER_ROUND",
    "ENABLE_COUNTS_SNAPSHOT", "COUNTS_SNAPSHOT_THREAD_TICK",
    "PUBLISH_I_AM_HERE",
    "PUBLISH_MAX_PER_CYCLE", "PUBLISH_MIN_GAP",
    "PUBLISH_WINDOW",
    "RUN_ON_START",
    "SCAN_INTERVAL", "PUBLISH_INTERVAL", "SYNC_INTERVAL", "COVERS_INTERVAL",
    "EDIT_INTERVAL", "VTID_INTERVAL", "RSS_TID_INTERVAL", "CRAWL_INTERVAL",
    "ENABLE_PUBLISH_CRABPT", "CRABPT_INTERVAL", "CRABPT_MAX_PER_CYCLE",
    "ENABLE_PUBLISH_PTER", "PTER_INTERVAL", "PTER_MAX_PER_CYCLE",
    "ENABLE_PUBLISH_XINGWAN", "XINGWAN_INTERVAL", "XINGWAN_MAX_PER_CYCLE",
    "XINGWAN_PUBLISH_WINDOW", "XINGWAN_URL",
    "ENABLE_EDIT_XINGWAN", "EDIT_XINGWAN_INTERVAL", "EDIT_XINGWAN_MAX_PER_CYCLE",
)


def save_env_snapshot():
    """将生效环境变量白名单持久化为 JSON 文件（原子替换写入）。"""
    snap = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pid": os.getpid(),
        "keys": sorted(ENV_SNAPSHOT_KEYS),
        "values": {k: os.environ[k] for k in ENV_SNAPSHOT_KEYS
                   if k in os.environ},
    }
    try:
        tmp = ENV_SNAPSHOT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2, ensure_ascii=False)
        os.replace(tmp, ENV_SNAPSHOT_FILE)
    except OSError as e:
        log(f"⚠️  写开关快照失败: {e}")


def beat():
    """心跳文件给 docker healthcheck 用。"""
    try:
        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


# ==================== 阶段执行 ====================

def run_stage(stage, timeout):
    """执行单个调度阶段子进程，返回包含执行状态、退出码与耗时的字典。"""
    name, cmd = stage["name"], stage["cmd"]
    log("─" * 60)
    log(f"▶️  阶段 [{name}] 开始: {' '.join(cmd)}")
    started = time.time()
    started_iso = datetime.now().isoformat(timespec="seconds")

    def _res(ok, code, took):
        return {"ok": ok, "code": code, "took": round(took, 1),
                "started_at": started_iso}

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
            stdout=sys.stdout,
            stderr=sys.stderr,
            stdin=subprocess.DEVNULL,   # 容器里没 TTY，任何 input() 立刻拿到 EOF 而不是永久卡住
        )
    except OSError as e:
        log(f"❌ 阶段 [{name}] 无法启动: {e}")
        # ★ 起不来用 code=None（**不是 0**）：`None` 与"退 0 的正常收工"
        #   必须分得开，否则又回到"两件事同形"的老问题。
        return _res(False, None, time.time() - started)

    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"⏰ 阶段 [{name}] 超过 {timeout}s 仍未结束，终止它")
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        # ★ 超时也是 code=None：进程的退出码在这里**没有意义**
        #   （是被我们杀掉的），编一个数字出来只会误导。
        return _res(False, None, time.time() - started)
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait(timeout=30)
        raise

    took = time.time() - started
    if code == 0:
        log(f"✅ 阶段 [{name}] 完成，耗时 {took:.1f}s")
        return _res(True, 0, took)

    log(f"❌ 阶段 [{name}] 退出码 {code}，耗时 {took:.1f}s")
    return _res(False, code, took)


def stage_result_ok(res):
    """`run_stage` 的返回值 → 成功与否。抽出来是为了**调用点不必知道结构**。"""
    return bool(res.get("ok"))


def _skip_reason(age, wait):
    """生成阶段跳过原因描述，用于记录状态。"""
    return f"未到期：距上次 {age:.0f}s < 周期 {wait:.0f}s"


# 发种时间窗口控制（PUBLISH_WINDOW 格式为 "HH:MM-HH:MM"，支持跨零点，未配置默认全天）
def _parse_publish_window(spec):
    """解析 `"HH:MM-HH:MM"` ⇒ `(start_min, end_min)`（分钟）；无效/空 ⇒ None（=全天）。"""
    if not spec:
        return None
    s = str(spec).strip()
    if not s:
        return None
    try:
        a, b = s.split("-", 1)
        sh, sm = a.strip().split(":", 1)
        eh, em = b.strip().split(":", 1)
        start = int(sh) * 60 + int(sm)
        end = int(eh) * 60 + int(em)
        if not (0 <= start < 1440 and 0 <= end < 1440):
            raise ValueError("超出 00:00-23:59")
        return (start, end)
    except Exception as e:
        log(f"⚠️  PUBLISH_WINDOW={spec!r} 解析失败（{e}），按『全天』处理")
        return None


def in_publish_window(now=None, extra_spec=None):
    """当前是否处于允许发种的时段内。缺省/非法 ⇒ 永远 True。

    全局 `PUBLISH_WINDOW` 生效后，再叠加 `extra_spec`（如星湾的 00:00-10:00）——
    两者都放行才放行；`extra_spec` 为 None 时行为与旧版完全一致。
    """
    specs = [os.getenv("PUBLISH_WINDOW")]
    if extra_spec is not None:
        specs.append(extra_spec)

    dt = datetime.fromtimestamp(now if now is not None else time.time())
    cur = dt.hour * 60 + dt.minute
    for spec in specs:
        win = _parse_publish_window(spec)
        if win is None:
            continue
        start, end = win
        if start <= end:
            ok = start <= cur < end
        else:
            # 跨零点：如 22:00-06:00
            ok = cur >= start or cur < end
        if not ok:
            return False
    return True


def _wait_seconds(stage, st, n_fail, max_backoff, pending_now):
    """
    计算阶段下次执行所需等待秒数。
    优先级：1. 连续失败指数退避；2. 存在待办且上轮有推进时使用短冷却；3. 默认使用标准周期。
    """
    if n_fail:
        return min(stage["interval"] * (2 ** min(n_fail, 6)), max_backoff)
    if pending_now and not st.get("no_progress"):
        return max(1, stage.get("cooldown", 60))
    return stage["interval"]


def sleep_interruptible(seconds):
    """按秒切片睡，好让 SIGTERM 能及时生效，而不是睡满一个周期才响应。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _shutdown:
            return
        time.sleep(min(1.0, max(0.0, deadline - time.time())))


def _pending(stage):
    """
    该阶段当前的待办数。**没有探针的阶段返回 None**，调用方据此区分
    "这个阶段不看活"和"看了，结果是 0"。

    ★ 探针出错绝不能带走调度器。主循环每 SCHED_TICK（默认 30s）就会问一次，
    一次 `database is locked` 或者别的 OperationalError 就让整个 daemon 退出，
    代价远大于收益。出错一律按 0（"没活"）处理 —— 这是唯一安全的降级方向：
    退回到常规长周期，最坏情况不过是回到改动前的行为。
    """
    fn = stage.get("pending")
    if not fn:
        return None
    try:
        return fn()
    except Exception as e:
        log(f"⚠️  [{stage['name']}] 待办探针出错，本轮按『没活』处理: {e}")
        return 0


# 延迟回加参数配置
READD_MAX_ATTEMPTS = 20          # 单轮最多处理条数
READD_GIVE_UP = 96               # 最大重试次数上限
READD_CONFLICT_IS_SUCCESS = env_bool("READD_CONFLICT_IS_SUCCESS", True)  # 409 冲突视为已在做种
READD_RETRY_BASE = env_int("READD_RETRY_BASE", 300)      # 重试基础退避间隔（秒）
READD_RETRY_CAP = env_int("READD_RETRY_CAP", 21600)      # 重试退避上限（秒）
READY_TIMEOUT = env_int("QB_READY_TIMEOUT", 120)     # 下载器就绪等待超时（秒）
READY_INTERVAL = env_int("QB_READY_INTERVAL", 5)     # 探活检测间隔（秒）


def _readd_backoff(attempts):
    """计算第 attempts 次失败后的退避重试延迟（秒）。"""
    return min(READD_RETRY_BASE * (2 ** max(0, attempts - 1)), READD_RETRY_CAP)


# 稀疏日志打印尝试点，避免失败重试刷屏
READD_LOG_AT = (1, 2, 3, 5, 8, 13, 21, 34, 55, 89)


def _readd_should_log(attempts):
    """判断当前重试次数是否需要输出日志（仅首试、阶段里程碑及触顶时输出）。"""
    if attempts <= 1:
        return True
    if attempts in READD_LOG_AT:
        return True
    return (_readd_backoff(attempts) >= READD_RETRY_CAP
            and _readd_backoff(attempts - 1) < READD_RETRY_CAP)


# 定周期存活摘要输出间隔
ALIVE_LOG_EVERY = env_int("ALIVE_LOG_EVERY", 600)        # 存活行间隔（秒）


def _alive_line(state, stages, now):
    """拼一行存活摘要。★ 纯函数 ⇒ 可单测（断言含每个阶段名 + 状态）。

    形如：`[存活] 阶段: scan 跳过(距上次 610s<1800s) · publish 上次OK(3.4s) · …`
    """
    parts = []
    for stage in stages:
        name = stage["name"]
        st = state.get(name) or {}
        last = st.get("last_run") or 0
        if st.get("skip_reason"):
            parts.append(f"{name} 跳过({st['skip_reason']})")
        elif last:
            ok = "OK" if st.get("last_ok") else "FAIL"
            took = st.get("last_took")
            took_s = f"{took:.1f}s" if isinstance(took, (int, float)) else "?"
            parts.append(f"{name} 上次{ok}({took_s})")
        else:
            parts.append(f"{name} 未跑过")
    return "[存活] " + " · ".join(parts)


# 缺失数据台账告警阈值
MISSING_ALERT_TAIL = env_int("MISSING_ALERT_TAIL", 3)   # 摘要中显示的专辑数量


def _missing_seed_alert(limit=None):
    """读漏做种账本，拼一句告警（无账 ⇒ 返回 ""）。★ 纯函数 ⇒ 可单测。"""
    import json
    import os as _os
    limit = MISSING_ALERT_TAIL if limit is None else limit
    try:
        path = getattr(handoff, "MISSING_LOG", "")
    except Exception:                                             # noqa: BLE001
        try:
            path = _os.path.join(config.DATA_DIR, "handoff_missing_data.jsonl")
        except Exception:                                         # noqa: BLE001
            return ""
    if not path or not _os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            rows = [ln for ln in (s.strip() for s in f) if ln]
    except OSError:
        return ""
    if not rows:
        return ""
    albums = []
    for ln in rows[-limit:]:
        try:
            rec = json.loads(ln)
            name = rec.get("album") or (str(rec.get("infohash") or "")[:12] + "…")
        except Exception:                                         # noqa: BLE001
            name = "?"
        albums.append(str(name)[:28])
    tail = " · ".join(albums)
    return f"⚠️ 漏做种: {len(rows)} 条（近 {len(albums)}: {tail}）"



def _qb_ready_once(host, timeout=3):
    """只读探一次 qB 是否就绪。返回 `(ok, detail)`。只读探查一次下载器是否就绪。

    ★ 保留本名：`tools/test_qb_ready.py`（#7，15/15）按此名注入假 `urlopen` 测三支。
    """
    import qb_client
    return qb_client.qb_ready_once(host, timeout=timeout)


def _wait_qb_ready(host, *, timeout, interval, clock=time.monotonic, sleep=time.sleep,
                   probe=None, log=log):
    """等 qB 就绪，最多 `timeout` 秒。等待下载器就绪，最多等待 timeout 秒。

    ★ 保留本签名（含可注入 `clock`/`sleep`/`probe`/`log`）：`tools/test_qb_ready.py` 用它们
      做**零等待**单测。★ 逻辑改动请改 `qb_client`（两个调用方共用一份，见那里的注释）。
    """
    import qb_client
    return qb_client.wait_qb_ready(host, timeout=timeout, interval=interval,
                                   clock=clock, sleep=sleep, probe=probe, log=log)


def _readd_area_ready(item):
    """
    核对回加目录数据是否就位，返回 (ok, why)。
    优先校验 src_path 与 album 目录完整性；若已知 album 则深入专辑目录核对，避免空数据入队。
    """
    src = item.get("src_path") or ""
    album = item.get("album") or ""
    if src and album:
        try:
            import handoff
            return handoff.verify_publish_area_complete(src, album)
        except Exception as e:                                    # noqa: BLE001
            return False, f"核对发种区异常: {type(e).__name__}: {e}"
    sp = item.get("save_path") or ""
    if sp and album:
        target = os.path.join(sp, album)
        if not os.path.exists(target):
            return False, f"发种区专辑不存在: {target}"
        if os.path.isdir(target):
            ok = bool(os.listdir(target))
            return ok, f"发种区专辑目录{'非空' if ok else '为空'}: {target}"
        ok = os.path.getsize(target) > 0
        return ok, f"发种区单文件{'非空' if ok else '为零字节'}: {target}"
    if sp:
        ok = bool(os.path.isdir(sp) and os.listdir(sp))
        return ok, f"发种区目录{'在' if ok else '不在/空'}: {sp}"
    return False, "条目既无 src_path/album 也无 save_path，无从核对"


def _drain_pending_readds():
    """到点的延迟回加条目 → `torrents_add`。**绝不 raise**（主循环的一步）。"""
    try:
        import readd_queue
    except Exception as e:                                      # noqa: BLE001
        log(f"⚠️  [readd] 无法 import readd_queue，跳过: {e}")
        return
    try:
        due, rest = readd_queue.pop_due()
    except Exception as e:                                      # noqa: BLE001
        log(f"⚠️  [readd] 读队列出错，跳过本轮: {e}")
        return
    if not due:
        return

    # 连接 qB 之前先等待就绪探针，超时则顺延至下一轮
    if not _wait_qb_ready(config.QB_HOST, timeout=READY_TIMEOUT, interval=READY_INTERVAL):
        log(f"ℹ️  [readd] qB 未就绪，{len(due)} 条延迟回加顺延（不改队列）")
        return

    # ★ 只在**真有到点条目**时才连 qB（没有待办就一条连接都不建）
    try:
        # 统一客户端构造：优先 API Key，回退账号密码
        import qb_client
        qb = qb_client.build(config)
    except Exception as e:                                      # noqa: BLE001
        # qB 连不上 ⇒ **不改队列**（due 条目原样留到 rest 里，下轮重试）。
        # ★ 这里显式把 due 并回 rest 再写盘，否则 pop 出来的会因为"没 ack"
        #   而在**盘上**依然是旧内容 —— 其实不必写，因为 pop_due 不落盘。
        log(f"⚠️  [readd] qB 连不上，{len(due)} 条延迟回加顺延: {type(e).__name__}: {e}")
        return

    import base64
    done, failed = [], []
    for it in due[:READD_MAX_ATTEMPTS]:
        note = it.get("note", "?")
        try:
            # 回加前核对发种区数据是否就位
            _ok136, _why136 = _readd_area_ready(it)
            if not _ok136:
                log(f"❌ [readd] {note} 发种区数据未就位 ⇒ 跳过回加: {_why136}")
                try:
                    import handoff as _ho136b
                    _ho136b.record_missing_data(it.get("infohash", ""),
                                                it.get("album") or "",
                                                "readd-delayed", _why136)
                except Exception:                                 # noqa: BLE001
                    pass
                done.append(it)                                   # 出队（不重试无源条目）
                continue
            content = base64.b64decode(it["torrent_b64"])
            # 前置做种状态闸门：若已处于正常做种状态则跳过重复回加
            b_ih = str(it.get("infohash") or "").strip().lower()
            if not b_ih and it.get("torrent_b64"):
                try:
                    import io as _io136
                    import torf as _torf136
                    b_ih = str(_torf136.Torrent.read_stream(_io136.BytesIO(content)).infohash or "").strip().lower()
                except Exception:
                    b_ih = ""
            if b_ih:
                try:
                    _cur_info = qb.torrents_info(torrent_hashes=b_ih)
                    if _cur_info:
                        _t0 = _cur_info[0]
                        _prog0 = float(getattr(_t0, "progress", 0) or 0)
                        _st0 = str(getattr(_t0, "state", "") or "")
                        _seed_sts = {"stalledUP", "uploading", "forcedUP", "queuedUP", "checkingUP"}
                        if _prog0 >= 1.0 and _st0 in _seed_sts:
                            log(f"ℹ️  [readd] {note} 已在正常做种（state={_st0} progress={_prog0:.2f}）"
                                f"⇒ 幂等拦截跳过重复回加并出队")
                            done.append(it)
                            continue
                except Exception as _e_chk:
                    log(f"⚠️  [readd] {note} 预查做种状态失败（继续回加流程）: {type(_e_chk).__name__}: {_e_chk}")

            qb.torrents_add(
                torrent_files=content,
                save_path=it.get("save_path") or "",
                is_paused=False,
                category=it.get("category") or "",
                tags=it.get("tags") or None,
            )
            done.append(it)
            log(f"✅ [readd] 延迟到点，已回加 qB（{note}）")
        except Exception as e:                                  # noqa: BLE001
            # HTTP 409 Conflict 表示下载器已存在该种子，视为成功出队
            if type(e).__name__ == "Conflict409Error" and READD_CONFLICT_IS_SUCCESS:
                done.append(it)                                 # 从队列移除（已就绪）
                log(f"ℹ️  [readd] {note} qB 返回 409（重复）= 视为已回加，移出队列")
                continue
            it["attempts"] = it.get("attempts", 0) + 1
            if it.get("first_failed_at") is None:
                it["first_failed_at"] = time.time()
            if it["attempts"] >= READD_GIVE_UP:
                log(f"❌ [readd] {note} 连续 {it['attempts']} 次回加失败，**放弃**"
                    f"（种子已上站、不会做种，需人工介入）: {type(e).__name__}: {e}")
                done.append(it)                                 # 从队列移除（放弃）
            else:
                # ★ 退避：写 `next_retry_at` ⇒ `pop_due` 只在**到期**时才再取出它
                #   （不再每 30s 重试）。见 `readd_queue.enqueue` 的字段说明。
                backoff = _readd_backoff(it["attempts"])
                it["next_retry_at"] = time.time() + backoff
                failed.append(it)
                # ★ 只在**状态变化**时打印（首次 / 里程碑 / 触顶）——
                #   否则一条永久失败的条目会把日志刷满（这正是本轮的起因）。
                if _readd_should_log(it["attempts"]):
                    log(f"⚠️  [readd] {note} 回加失败（第 {it['attempts']} 次，"
                        f"{int(backoff)}s 后再试）: {type(e).__name__}: {e}")

    # 没在本轮处理的（超过 READD_MAX_ATTEMPTS 的）原样留下
    leftover = due[READD_MAX_ATTEMPTS:]
    try:
        readd_queue.ack(
            [it.get("infohash") for it in done],
            rest + failed + leftover,
        )
    except Exception as e:                                      # noqa: BLE001
        log(f"⚠️  [readd] 写回队列出错（成功的那几条可能下轮重复 add，无害）: {e}")


# 延迟回加后台独立工作线程配置
READD_THREAD_TICK = 30           # 线程自跑间隔（秒），与主循环 SCHED_TICK 独立


def _readd_thread_loop():
    """独立线程：每 `READD_THREAD_TICK` 秒排空一次延迟回加队列。**绝不退出到线程外**。"""
    log(f"🧵 [readd] 延迟回加线程启动（每 {READD_THREAD_TICK}s 扫一次队列）")
    while not _shutdown:
        try:
            _drain_pending_readds()
        except Exception as e:                                  # noqa: BLE001
            # 兜底：`_drain_pending_readds` 本身已"绝不 raise"，这里是双保险。
            # 线程**绝不能**因未捕获异常而死（死了 = 延迟做种静默失效，且没人知道）。
            log(f"⚠️  [readd] 线程本轮异常（吞掉，继续）: {type(e).__name__}: {e}")
        # 分片睡，好让 SIGTERM 及时生效
        slept = 0.0
        while slept < READD_THREAD_TICK and not _shutdown:
            time.sleep(min(1.0, READD_THREAD_TICK - slept))
            slept += 1.0
    log("🧵 [readd] 延迟回加线程退出")


# ==================== 发种交棒后台线程 ====================
# 将满足做种门槛的种子任务从发种实例交棒至归档做种实例，并清理暂存副本

HANDOFF_HOST_3020 = os.getenv("QB_HOST_OPENCD", "http://127.0.0.1:3020")
HANDOFF_THREAD_TICK = env_int("HANDOFF_THREAD_TICK", 300)      # 5min：慢判断，不必高频
HANDOFF_MAX_PER_ROUND = env_int("HANDOFF_MAX_PER_ROUND", 5)


def _handoff_dry_run():
    return env_bool("HANDOFF_DRY_RUN", True)


def _handoff_build_two(target_host=None):
    """build (3040_src, qb_dst) 两个 qB 连接。若未指定 target_host，则从 HANDOFF_MUSIC_HOSTS 中负载均衡挑选。"""
    import qb_client
    import types
    try:
        qb_src = qb_client.build(config)               # 3010（config.QB_HOST）
    except Exception as e:                                          # noqa: BLE001
        return None, None, "", f"连 3010 失败: {type(e).__name__}: {e}"

    hosts = getattr(config, "HANDOFF_MUSIC_HOSTS", None) or [HANDOFF_HOST_3020]
    if target_host:
        chosen_host = target_host
    else:
        # 负载均衡：查询各个候选做种实例当前的种子总数，选最小者
        best_host = None
        min_torrents = float("inf")
        for h in hosts:
            try:
                cfg_candidate = types.SimpleNamespace(
                    QB_HOST=h,
                    QB_USER=getattr(config, "QB_USER", "admin"),
                    QB_PASS=config.QB_PASS,
                    QB_API_KEY=getattr(config, "OPENCD_QB_API_KEY", ""),
                )
                cli = qb_client.build(cfg_candidate)
                t_count = len(cli.torrents_info())
                if t_count < min_torrents:
                    min_torrents = t_count
                    best_host = h
            except Exception:
                continue
        chosen_host = best_host or hosts[0]

    try:
        cfg_dst = types.SimpleNamespace(
            QB_HOST=chosen_host,
            QB_USER=getattr(config, "QB_USER", "admin"),
            QB_PASS=config.QB_PASS,
            QB_API_KEY=getattr(config, "OPENCD_QB_API_KEY", ""),
        )
        qb_dst = qb_client.build(cfg_dst)
    except Exception as e:                                          # noqa: BLE001
        return None, None, "", f"连做种实例 {chosen_host} 失败: {type(e).__name__}: {e}"
    return qb_src, qb_dst, chosen_host, ""


def _handoff_due(item, now=None):
    """判断交棒条目当前轮次是否到期且尝试次数未超限。"""
    now = time.time() if now is None else now
    cap = int(getattr(config, "HANDOFF_MAX_ATTEMPTS", 20) or 20)
    if int(item.get("attempts", 0) or 0) >= cap:
        return False
    nxt = item.get("next_check_at")
    if nxt:
        try:
            if now < float(nxt):
                return False
        except (TypeError, ValueError):
            pass          # 坏值 ⇒ 视为"已到"（宁多试一次，也别永久卡死）
    return True


def _drain_handoff():
    """推进 handoff 队列里所有**未完成**条目。**绝不 raise**（线程的一步）。"""
    try:
        import handoff
    except Exception as e:                                          # noqa: BLE001
        log(f"⚠️  [handoff] 无法 import handoff，跳过: {e}")
        return
    try:
        pending = handoff.items_by_state(
            "PENDING", "HANDOFF_ADDED", "HANDOFF_RECHECK", "FAILED")
    except Exception as e:                                          # noqa: BLE001
        log(f"⚠️  [handoff] 读队列出错，跳过本轮: {e}")
        return
    if not pending:
        return
    now = time.time()
    due = [it for it in pending if _handoff_due(it, now)]
    if not due:
        log(f"ℹ️  [handoff] {len(pending)} 条待处理均在退避/达上限，本轮跳过")
        return
    dry = _handoff_dry_run()
    if not dry and not _wait_qb_ready(config.QB_HOST,
                                      timeout=READY_TIMEOUT, interval=READY_INTERVAL):
        log(f"ℹ️  [handoff] 3040 未就绪，{len(due)} 条交棒顺延")
        return
    qb_src, qb_dst, chosen_host, err = _handoff_build_two()
    if qb_src is None or qb_dst is None:
        log(f"⚠️  [handoff] {err} ⇒ {len(due)} 条交棒顺延")
        return

    # 优先级排序：在途/重试状态（HANDOFF_ADDED, HANDOFF_RECHECK, FAILED）优先推进；
    # PENDING 条目中，未被标记为"未达/等够格"的优先，其次按 enqueued_at 排序
    def _due_priority(it):
        st = it.get("state")
        if st in ("HANDOFF_ADDED", "HANDOFF_RECHECK"):
            return 0
        if st == "FAILED":
            return 1
        err = it.get("last_error") or ""
        if "未达" in err or "等够格" in err or "等 3040 就绪" in err:
            return 3
        return 2

    due.sort(key=lambda it: (_due_priority(it), float(it.get("enqueued_at") or 0)))

    done = 0
    for it in due[:HANDOFF_MAX_PER_ROUND]:
        before = it.get("state")
        after = handoff.process_one(qb_src, qb_dst, it, dry_run=dry, dst_host=chosen_host)
        if after != before:
            # 从队列状态中读取最新错误信息
            _err_after = ""
            try:
                _fresh = {x.get("infohash"): x for x in handoff.items_by_state()}
                _err_after = (_fresh.get(it.get("infohash")) or {}).get("last_error") or ""
            except Exception:                                    # noqa: BLE001
                _err_after = ""
            log(f"🔀 [handoff] {it.get('infohash', '')[:12]}… {before} → {after}"
                + (f"（{_err_after}）" if after == "FAILED" else ""))
        if after == "DONE":
            done += 1
    if done:
        log(f"✅ [handoff] 本轮完成 {done} 条（交棒+清盘）")


def _handoff_thread_loop():
    """独立线程：每 `HANDOFF_THREAD_TICK` 秒推进一次交棒状态机。**绝不退出到线程外**。"""
    if os.getenv("ENABLE_HANDOFF", "false").strip().lower() not in ("1", "true", "yes", "y", "on"):
        log("🧵 [handoff] ENABLE_HANDOFF 未开 ⇒ 交棒线程不起（新流程不接管）")
        return
    log(f"🧵 [handoff] 交棒线程启动（每 {HANDOFF_THREAD_TICK}s 扫一次；"
        f"dry_run={_handoff_dry_run()}）")
    while not _shutdown:
        try:
            _drain_handoff()
        except Exception as e:                                      # noqa: BLE001
            # 双保险：`_drain_handoff` 本身已"绝不 raise"。
            log(f"⚠️  [handoff] 线程本轮异常（吞掉，继续）: {type(e).__name__}: {e}")
        slept = 0.0
        while slept < HANDOFF_THREAD_TICK and not _shutdown:
            time.sleep(min(1.0, HANDOFF_THREAD_TICK - slept))
            slept += 1.0
    log("🧵 [handoff] 交棒线程退出")


# ==================== 做种状态巡检后台线程 ====================
# 定期巡检已发布记录在下载器中的做种状态，针对丢失做种的条目触发重新回加

SEED_PATROL_THREAD_TICK = env_int("SEED_PATROL_THREAD_TICK", 3600)   # 1h：慢巡检，不必高频
SEED_PATROL_MAX_PER_ROUND = env_int("SEED_PATROL_MAX_PER_ROUND", 300)  # 一轮最多巡多少行


def _seed_patrol_dry_run():
    return env_bool("SEED_PATROL_DRY_RUN", True)      # ★ 默认 dry-run（要真修须显式 false）


def _seed_patrol_build_clients():
    """build `(qb_3020, qb_3040, sites, cats)`。任一失败 ⇒ 抛（调用方记日志跳过本轮）。"""
    import types
    import db
    import qb_client
    from musopia import MusopiaClient
    from crabpt_site import CrabptClient
    from pter_site import PterClient
    from xingwan_site import XingwanClient
    cfg3020 = types.SimpleNamespace(
        QB_HOST=HANDOFF_HOST_3020,
        QB_USER=getattr(config, "QB_USER", "admin"),
        QB_PASS=config.QB_PASS,
        # 目标做种实例使用独立的 API Key 配置
        QB_API_KEY=getattr(config, "OPENCD_QB_API_KEY", ""),
    )
    qb_3020 = qb_client.build(cfg3020)
    qb_3040 = qb_client.build(config)
    sites = {db.SITE_MUSOPIA: MusopiaClient(), db.SITE_CRABPT: CrabptClient(),
             db.SITE_PTER: PterClient(), db.SITE_XINGWAN: XingwanClient()}
    cats = {db.SITE_MUSOPIA: getattr(config, "PUBLISH_CATEGORY", None),
            db.SITE_CRABPT: getattr(config, "CRABPT_PUBLISH_CATEGORY", None),
            db.SITE_PTER: getattr(config, "PTER_PUBLISH_CATEGORY", None),
            db.SITE_XINGWAN: None}          # ★ 星湾不建分类，只打标签（见 tags）
    tags = {db.SITE_XINGWAN: getattr(config, "XINGWAN_PUBLISH_TAG", None)}
    return qb_3020, qb_3040, sites, cats, tags


def _seed_patrol_retry(fn, *, tries=3, delay=2):
    """小重试（站点请求偶抖）。**绝不 raise**，返回 `(ok, val)`。"""
    last = None
    for _ in range(tries):
        try:
            return True, fn()
        except Exception as e:                                      # noqa: BLE001
            last = e
            time.sleep(delay)
    return False, last


def _seed_patrol_build_extra():
    """按环境变量 SEED_PATROL_EXTRA_INSTANCES 构建扩展只读客户端实例。
    ★ 默认纳管常规保种库 music1:3220,music2:3240，防止已交棒保种的种子被误判为 MISSING。
    """
    raw = (os.getenv("SEED_PATROL_EXTRA_INSTANCES", "music1:3220,music2:3240") or "").strip()
    if not raw:
        return []
    import types
    import qb_client
    out = []
    for part in raw.split(","):
        label, sep, port = part.strip().partition(":")
        if not sep or not label or not port.isdigit():
            log(f"⚠️  [patrol] 扩展实例格式应为 label:port，忽略 {part!r}")
            continue
        try:
            cfg = types.SimpleNamespace(
                QB_HOST=f"http://127.0.0.1:{port}",
                QB_USER=getattr(config, "QB_USER", "admin"),
                QB_PASS=getattr(config, "QB_PASS", ""),
                QB_API_KEY="",          # 本机 localhost 白名单免密（与 3020 同口径）
            )
            out.append((label, qb_client.build(cfg)))
        except Exception as e:                                      # noqa: BLE001
            log(f"⚠️  [patrol] 构建扩展实例 {label}:{port} 失败: {type(e).__name__}: {e}")
    if out:
        log("🩺 [patrol] 扩展纳管实例: " + ", ".join(f"{l}:{getattr(c, 'base_url', '')}"
                                                     for l, c in out))
    return out


def _drain_seed_patrol():
    """推进一轮做种状态巡检，对检测到 MISSING 状态的条目重新入队回加。"""
    import db
    try:
        import site_seed_patrol as ssp
        import site_readd as sr
        import readd_queue
    except Exception as e:                                          # noqa: BLE001
        log(f"⚠️  [patrol] 无法 import 巡检/加回库，跳过本轮: {e}")
        return

    try:
        qb_3020, qb_3040, sites, cats, tags = _seed_patrol_build_clients()
    except Exception as e:                                          # noqa: BLE001
        log(f"⚠️  [patrol] build 客户端失败，跳过本轮: {type(e).__name__}: {e}")
        return

    try:
        conn = db.connect()
    except Exception as e:                                          # noqa: BLE001
        log(f"⚠️  [patrol] 连库失败，跳过本轮: {type(e).__name__}: {e}")
        return
    try:
        rows = ssp.iter_exists_rows(conn)
    except Exception as e:                                          # noqa: BLE001
        log(f"⚠️  [patrol] 取待巡行失败: {type(e).__name__}: {e}")
        return
    finally:
        conn.close()

    rows = rows[:SEED_PATROL_MAX_PER_ROUND]
    dry = _seed_patrol_dry_run()
    qb_extra = _seed_patrol_build_extra()

    # 客户端单轮全表缓存，避免循环逐行请求下载器
    _list_cache = {}

    def _lister(qb):
        key = id(qb)
        if key in _list_cache:
            return _list_cache[key]
        ok, val = _seed_patrol_retry(lambda: qb.torrents_info(), tries=5, delay=3)
        if not ok:
            raise RuntimeError(f"取 qB 全表失败: {val}")
        _list_cache[key] = val
        return val

    from collections import Counter
    verdicts = Counter()
    missing = []
    ambiguous = []                                    # ★ #50：同 B 多处持有 ⇒ 本地去重候选
    for row in rows:
        cli = sites.get(row.get("site"))
        if cli is None:
            continue
        ok, raw = _seed_patrol_retry(lambda: cli.download_torrent(row.get("torrent_id")),
                                     tries=3, delay=2)
        raw = raw if ok else None
        try:
            r = ssp.patrol_row(row, download=lambda _t, _r=raw: _r,
                               qb_3020=qb_3020, qb_3040=qb_3040,
                               own_categories=set(c for c in cats.values() if c),
                               qb_list=_lister, qb_extra=qb_extra)
        except RuntimeError as e:
            # ★ qB 连不上 = 环境问题，**不是**"这条种丢了" ⇒ 本轮整体不可信，跳过本轮
            log(f"⚠️  [patrol] 巡检中 qB 连接失败，本轮作废（不写任何队列）: {e}")
            return
        verdicts[r["verdict"]] += 1
        if r["verdict"] == ssp.MISSING:
            r["_raw"] = raw            # 带上 B 的字节（加回要用）
            missing.append(r)
        elif r["verdict"] == ssp.AMBIGUOUS:
            r["_raw"] = raw
            ambiguous.append(r)

    _order = [ssp.SEEDING, ssp.PRESENT, ssp.MISSING, ssp.AMBIGUOUS, ssp.NO_SITE_TORRENT]
    summ = "  ".join(f"{k}={verdicts[k]}" for k in _order if verdicts.get(k))
    log(f"🩺 [patrol] 巡 {len(rows)} 行: {summ}  dry_run={dry}")

    # AMBIGUOUS 状态处理：同一种子在多个实例中存在，仅做本地去重删除冗余任务
    for r in ambiguous:
        note = (f"{r['site']} id={r['torrent_id']} B={str(r.get('b_ih'))[:12]}… "
                f"found_in={r.get('found_in')}")
        dup_3040 = any(w == "3040" for w in (r.get("found_in") or []))
        if dry:
            log(f"  · [patrol][dry-run] AMBIGUOUS（same-location 隐患）{note}"
                + ("  ⇒ 将删 3040 重复实例（仅去重、不删文件）" if dup_3040 else ""))
            continue
        if not dup_3040:
            # 3020 内部重复（同一 qB 两条同 hash，qB 一般不允许）⇒ 只能记，无法选删
            log(f"  ⚠️ [patrol] AMBIGUOUS 但不在 3040 ⇒ 只记录（本地无从下手）{note}")
            continue
        try:
            qb_3040.torrents_delete(delete_files=False, torrent_hashes=str(r.get("b_ih")))
            log(f"  🧹 [patrol] 已删 3040 重复实例（去重，不删文件）{note}")
        except Exception as e:                                      # noqa: BLE001
            log(f"  ⚠️ [patrol] 删 3040 重复失败 {note}: {type(e).__name__}: {e}")

    if not missing:
        return

    # 缺失条目回加必须先做源数据完整性核对与发种区准备，严禁盲目以 /downloads 加回导致重新网络下载
    enq = 0
    import handoff
    for r in missing:
        note = f"{r['site']} id={r['torrent_id']} B={str(r.get('b_ih'))[:12]}…"
        ih_a = r.get("info_hash") or ""
        album = r.get("info_name") or ""
        if dry:
            log(f"  · [patrol][dry-run] 将加回 {note}  {album!r}")
            continue

        # 1) 定位成品真实源文件路径
        src_path, why = "", ""
        if ih_a:
            try:
                import publish
                src_path, why = publish._resolve_opencd_content_path(ih_a, return_reason=True)
            except Exception as _e_src:
                src_path, why = "", str(_e_src)

        if not src_path or not os.path.exists(src_path):
            log(f"  ❌ [patrol] 无法定位或缺失源文件，拒绝加回 3010（防变网络下载）{note}: {why}")
            try:
                handoff.record_missing_data(ih_a, album, r.get("site") or "patrol",
                                            f"patrol-missing: {why or '源文件不存在'}")
            except Exception:
                pass
            continue

        # 2) 确保发种区副本已就位（无副本则复制并逐字节校验）
        try:
            ok_ready, why_ready = handoff.verify_publish_area_complete(src_path, album)
            if not ok_ready:
                handoff.copy_to_publish_area(src_path, album, dry_run=False)
                ok_ready, why_ready = handoff.verify_publish_area_complete(src_path, album)
            if not ok_ready:
                log(f"  ❌ [patrol] 发种区数据未就位，拒绝加回 3010: {why_ready}")
                handoff.record_missing_data(ih_a, album, r.get("site") or "patrol",
                                            f"patrol-area-not-ready: {why_ready}")
                continue
        except Exception as e_copy:
            log(f"  ❌ [patrol] 准备发种区异常，跳过加回: {type(e_copy).__name__}: {e_copy}")
            continue

        # 3) 核对无误后入队，带上完整 src_path 与 album
        cat = cats.get(r["site"]) or ""
        site_tag = tags.get(r["site"]) or ""
        try:
            readd_queue.enqueue(r["_raw"], config.PUBLISH_SAVE_ROOT, cat, 1,
                                note=f"patrol 加回 {r['site']} id={r['torrent_id']}",
                                src_path=src_path, album=album,
                                tags=[site_tag] if site_tag else None)
            enq += 1
        except Exception as e:                                      # noqa: BLE001
            log(f"  ⚠️ [patrol] 入队失败 {note}: {type(e).__name__}: {e}")
    if not dry:
        log(f"  ↪ [patrol] 已把 {enq}/{len(missing)} 条 MISSING 入队（readd 线程将加回发种实例 3010）")
    _ = sr  # （`sr` 仅作 import 存在性护栏：加回逻辑复用 readd_queue，不在此直接 add）

def _seed_patrol_thread_loop():
    """独立线程：每 `SEED_PATROL_THREAD_TICK` 秒巡一次 C 类护栏。**绝不退出到线程外**。"""
    if os.getenv("ENABLE_SEED_PATROL", "false").strip().lower() not in ("1", "true", "yes", "y", "on"):
        log("🧵 [patrol] ENABLE_SEED_PATROL 未开 ⇒ 巡检线程不起（C 类护栏不接管）")
        return
    log(f"🧵 [patrol] C 类护栏巡检线程启动（每 {SEED_PATROL_THREAD_TICK}s 巡一次；"
        f"dry_run={_seed_patrol_dry_run()}）")
    while not _shutdown:
        try:
            _drain_seed_patrol()
        except Exception as e:                                      # noqa: BLE001
            # 双保险：`_drain_seed_patrol` 本身已"绝不 raise"。
            log(f"⚠️  [patrol] 线程本轮异常（吞掉，继续）: {type(e).__name__}: {e}")
        slept = 0.0
        while slept < SEED_PATROL_THREAD_TICK and not _shutdown:
            time.sleep(min(1.0, SEED_PATROL_THREAD_TICK - slept))
            slept += 1.0
    log("🧵 [patrol] C 类护栏巡检线程退出")


# ==================== 运行状态快照后台线程 ====================
# 定期生成 counts.json 状态快照供外部监控采集

COUNTS_SNAPSHOT_THREAD_TICK = env_int("COUNTS_SNAPSHOT_THREAD_TICK", 3600)   # 1h：一天一生成绰绰有余


def counts_snapshot_path() -> str:
    """counts.json 落点（`/data/counts.json`，与 `daemon_heartbeat` 同目录）。"""
    return os.path.join(config.DATA_DIR, "counts.json")


def _counts_snapshot_day_path() -> str:
    """「今天已生成」标记文件（原子写，照 pt-notice `DigestScheduler`）。"""
    return os.path.join(config.DATA_DIR, ".counts-snapshot.last_day")


def counts_snapshot_due(now=None) -> bool:
    """今天是否还没生成过 counts.json。**纯函数式**（读一个文件，可测）。

    ★ 读不到/坏 ⇒ 返回 True（**宁多不少**：没标记就重生成，而不是"以为生成过了"）。
    """
    today = time.strftime("%Y-%m-%d", time.localtime(now if now else time.time()))
    try:
        with open(_counts_snapshot_day_path(), "r", encoding="utf-8") as f:
            return f.read().strip() != today
    except OSError:
        return True


def _mark_counts_snapshot_sent(now=None) -> None:
    """标记「今天已生成」。**只在真写成功后调用**；写不了也不抛（下轮会重试）。"""
    today = time.strftime("%Y-%m-%d", time.localtime(now if now else time.time()))
    path = _counts_snapshot_day_path()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(today)
        os.replace(tmp, path)     # 原子写
    except OSError as e:
        log(f"⚠️  [counts] 写快照标记失败（下次会重试）: {e}")


def _write_counts_snapshot() -> bool:
    """跑一次 `tools/gen_counts_online.py` 生成 `/data/counts.json`。**绝不抛**。

    返回是否成功（rc==0 且文件存在）。★ 失败**不标已生成** ⇒ 下轮重试。
    """
    import subprocess
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "tools", "gen_counts_online.py")
    # ★ 镜像内 `/app/tools/gen_counts_online.py`；本机 `<repo>/src/../tools/…`。
    if not os.path.exists(script):
        # 兜底：本机布局 `<repo>/tools/`（src 的兄弟）
        alt = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "tools", "gen_counts_online.py")
        script = alt if os.path.exists(alt) else script
    if not os.path.exists(script):
        log(f"⚠️  [counts] 找不到生成器 {script} ⇒ 跳过（不标已生成）")
        return False
    out = counts_snapshot_path()
    try:
        r = subprocess.run([sys.executable, script, "--out", out,
                            "--dst", config.DATA_DIR],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
    except Exception as e:                                        # noqa: BLE001
        log(f"⚠️  [counts] 生成器异常（吞掉）：{type(e).__name__}: {e}")
        return False
    if r.returncode != 0 or not os.path.exists(out):
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
        log(f"⚠️  [counts] 生成失败 rc={r.returncode}：{tail[0][:200]}")
        return False
    return True


def _counts_thread_loop():
    """独立线程：每天生成一次 `counts.json`。**绝不退出到线程外**。"""
    if os.getenv("ENABLE_COUNTS_SNAPSHOT", "false").strip().lower() not in ("1", "true", "yes", "y", "on"):
        log("🧵 [counts] ENABLE_COUNTS_SNAPSHOT 未开 ⇒ counts.json 快照线程不起")
        return
    log(f"🧵 [counts] counts.json 快照线程启动（每 {COUNTS_SNAPSHOT_THREAD_TICK}s 查一次；"
        f"每天只生成一次）")
    while not _shutdown:
        try:
            if counts_snapshot_due():
                if _write_counts_snapshot():
                    _mark_counts_snapshot_sent()
                    log(f"📸 [counts] 已生成 {counts_snapshot_path()}")
                else:
                    log("⚠️  [counts] 本轮未生成（下轮重试）")
        except Exception as e:                                    # noqa: BLE001
            # 双保险：上面各步已"绝不 raise"。
            log(f"⚠️  [counts] 线程本轮异常（吞掉，继续）: {type(e).__name__}: {e}")
        slept = 0.0
        while slept < COUNTS_SNAPSHOT_THREAD_TICK and not _shutdown:
            time.sleep(min(1.0, COUNTS_SNAPSHOT_THREAD_TICK - slept))
            slept += 1.0
    log("🧵 [counts] counts.json 快照线程退出")


def main():
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    stages, dry_run = build_stages()

    tick = env_int("SCHED_TICK", 30)
    stage_timeout = env_int("STAGE_TIMEOUT", 3600)
    run_on_start = env_bool("RUN_ON_START", True)
    max_backoff = env_int("MAX_BACKOFF", 21600)

    config.print_runtime_info()
    log("=" * 60)
    log("🚀 Musopia 编排器启动")
    log(f"   DRY_RUN     = {dry_run}" + ("（只预览，不会改站点）" if dry_run else ""))
    log(f"   RUN_ON_START= {run_on_start}")
    log(f"   调度间隔    = {tick}s，阶段超时 = {stage_timeout}s")
    if not stages:
        log("❌ 所有阶段都被禁用了（ENABLE_* 全为 false），没有任何事可做，退出。")
        sys.exit(1)
    for s in stages:
        extra = f"   有活时 {s['cooldown']}s 重跑" if s.get("cooldown") else ""
        log(f"   · {s['name']:<8} 每 {s['interval']:>6}s   {' '.join(s['cmd'])}{extra}")
    log("=" * 60)

    # 建表放在这里做一次，后面各阶段的 init_db() 都是幂等的补刀
    import db
    db.init_db()

    # ★ 落一份**容器内生效的**开关快照，给本机巡检脚本读（见 save_env_snapshot）
    save_env_snapshot()

    # 空库 + 启用发布 = 可能重复发布几百个种子，必须拦在跑起来之前
    if not preflight(stages):
        sys.exit(2)

    state = load_state()

    if run_on_start:
        # 首轮立刻全跑一遍：把 last_run 清成 0 即视为已到期
        for s in stages:
            state.setdefault(s["name"], {})["last_run"] = 0

    # ★ 退出语在 `run_main_loop()` 末尾打（那里才是循环真正结束的地方）
    run_main_loop(stages, state, max_backoff)


def run_main_loop(stages, state, max_backoff, tick=None, stage_timeout=None):
    """调度主循环，按配置周期驱动各阶段执行。"""
    tick = env_int("SCHED_TICK", 30) if tick is None else tick
    stage_timeout = env_int("STAGE_TIMEOUT", 3600) if stage_timeout is None else stage_timeout
    fails = {s["name"]: 0 for s in stages}
    last_alive_log = 0.0  # 上次输出存活摘要行的时间戳

    # 启动后台独立工作线程
    threading.Thread(target=_readd_thread_loop, name="readd", daemon=True).start()
    threading.Thread(target=_handoff_thread_loop, name="handoff", daemon=True).start()
    threading.Thread(target=_seed_patrol_thread_loop, name="patrol", daemon=True).start()
    threading.Thread(target=_counts_thread_loop, name="counts", daemon=True).start()

    while not _shutdown:
        beat()
        now = time.time()
        # 记录本轮是否有阶段被跳过，用于决定循环结束时是否持久化状态
        skipped = False

        for stage in stages:
            if _shutdown:
                break

            name = stage["name"]
            st = state.setdefault(name, {"last_run": 0})
            last = st.get("last_run", 0) or 0

            # 发种时间窗口拦截：仅对发布阶段生效，窗口外不更新 last_run
            # ★ 星湾额外叠加自己的 XINGWAN_PUBLISH_WINDOW（默认 00:00-10:00），
            #   其它三站不受影响 —— 全局 PUBLISH_WINDOW 仍照旧叠加。
            _win_kw = {}
            if name == "publish_xingwan":
                _win_kw["extra_spec"] = getattr(config, "XINGWAN_PUBLISH_WINDOW", "00:00-10:00")
            if name in ("publish", "publish_crabpt", "publish_pter", "publish_xingwan") \
                    and not in_publish_window(now, **_win_kw):
                st["last_skip_at"] = datetime.now().isoformat(timespec="seconds")
                if name == "publish_xingwan":
                    st["skip_reason"] = (
                        f"窗口外：XINGWAN_PUBLISH_WINDOW="
                        f"{getattr(config, 'XINGWAN_PUBLISH_WINDOW', '00:00-10:00')}")
                else:
                    st["skip_reason"] = f"窗口外：PUBLISH_WINDOW={os.getenv('PUBLISH_WINDOW')}"
                skipped = True
                continue

            # 连续失败退避：1x, 2x, 4x... 上限 MAX_BACKOFF
            n_fail = fails.get(name, 0)
            pending_now = _pending(stage)      # None = 这个阶段不看活
            wait = _wait_seconds(stage, st, n_fail, max_backoff, pending_now)

            if now - last < wait:
                # 阶段未到期跳过：记录跳过时间戳与原因，保持 last_finish 不变
                st["last_skip_at"] = datetime.now().isoformat(timespec="seconds")
                st["skip_reason"] = _skip_reason(now - last, wait)
                skipped = True
                continue

            before = pending_now
            res = run_stage(stage, stage_timeout)
            ok = stage_result_ok(res)
            after = _pending(stage) if before is not None else None

            st["last_run"] = time.time()
            st["last_ok"] = ok
            # ★ `last_finish` 只在这条分支（= 真跑了）里写。见上面跳过分支的注释。
            st["last_finish"] = datetime.now().isoformat(timespec="seconds")
            # ★★ 退出码与耗时**落盘**（原先只有 bool，见 `run_stage` 的 docstring）。
            #    `code=0` + `took` 很小 + `last_ok=true` ⇒ 一眼看出是
            #    「零请求的正常收工」（如 `topup` 的"今日额度用完"），
            #    而不是"真抓成功若干条"。这两件事以前在盘上同形。
            st["last_code"] = res.get("code")
            st["last_took"] = res.get("took")
            # 跑过就不再有"跳过"的痕迹，清掉免得读者以为它还在跳
            st.pop("last_skip_at", None)
            st.pop("skip_reason", None)
            if before is not None:
                # ★ 防呆：这一轮"跑完了"不等于"推进了"。
                # 像 14749 那种永远失败（找不到 takeedit 表单）的记录会一直挂在待办里，
                # 若判据只是"还有活就短冷却"，就会变成每 60s 敲一次站点 —— 比改动前更糟。
                # 真正的判据是"待办数有没有下降"：没降就退回常规周期，不再快跑。
                #
                # `before > 0` 这个前置不能省：本轮本来就没活的话，`0 >= 0` 会被误判成
                # "没推进"，刷出一条毫无意义的日志，还把 no_progress 白白置 True ——
                # 而那个标志是用来标记"卡住了"的。
                # 该标志随状态文件持久化，跑出一次进展就会自动清掉。
                st["no_progress"] = before > 0 and after >= before
                if st["no_progress"] and ok:
                    log(f"⏸️  [{name}] 本轮未推进（待办 {before} → {after}），"
                        f"退回常规周期 {stage['interval']}s")
            if ok:
                fails[name] = 0
            else:
                fails[name] = n_fail + 1
                nxt = min(stage["interval"] * (2 ** min(fails[name], 6)), max_backoff)
                log(f"↩️  [{name}] 连续失败 {fails[name]} 次，下次至少等 {nxt}s")
            save_state(state)
            # ★ 开关快照**每轮重写一次**（不是只在启动时写一次）：这样它的 mtime
            #   本身就是一个"daemon 还在跑"的旁证，而且万一有人在运行时改了
            #   容器环境变量（`docker exec` 改不了，但换 `.env` 后重建容器会），
            #   快照会在重建后的第一轮就跟着变 —— 不必等下一次重启才对上。
            save_env_snapshot()
            beat()

        # ★ 本轮有阶段被跳过 ⇒ 把 `last_skip_at`/`skip_reason` 落盘（见上面
        #   `skipped = False` 处那段注释：跳过支自己 `continue` 了，
        #   走不到上面那次 `save_state`）。真跑了阶段的轮次在上面已经存过，
        #   这里再存一次是幂等的（内容相同），不会写坏。
        if skipped:
            save_state(state)

        # 定期输出阶段存活摘要与缺失数据告警
        if now - last_alive_log >= ALIVE_LOG_EVERY:
            log(_alive_line(state, stages, now))
            _alert = _missing_seed_alert()
            if _alert:
                log(_alert + "  ← 见 data/handoff_missing_data.jsonl（人工补做种）")
            last_alive_log = now

        if _shutdown:
            break
        sleep_interruptible(tick)

    log("👋 编排器已退出。")


if __name__ == "__main__":
    main()
