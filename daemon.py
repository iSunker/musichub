"""
容器化编排器：常驻进程，按各自周期依次调度 scan / publish / sync / covers / edit。

为什么不用 cron：
1. 这五个阶段有严格的先后依赖（先扫出种子才有东西可发，先回填 ID 才能编辑），
   cron 各自定时会出现"发布还没跑完就开始编辑"的交叉执行，两个进程同时写
   同一个 SQLite 就会 database is locked。这里串行调度，天然互斥。
2. cron 在容器里还得额外解决 PID 1、环境变量继承、日志重定向三个老问题。

设计要点：
- 每个阶段起子进程跑。某个阶段崩了不会带走整个 daemon，下轮照常继续。
- 上次运行时间持久化到 /data，容器重启不会把所有阶段重新触发一遍
  （否则 docker 自动重启 + restart:always 会变成对站点的高频轰炸）。
- 连续失败指数退避。Cookie 过期这类问题会让每个阶段都失败，
  不退避就是每 30 分钟无意义地敲站点一次。
- 收到 SIGTERM 先停止调度新阶段，给当前子进程留出时间收尾，再退出。
  docker stop 默认只等 10 秒，compose 里把 stop_grace_period 放宽了。
"""
import json
import os
import signal
import subprocess
import sys
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
    dry_run = env_bool("DRY_RUN", False)

    def apply_flag(*flags):
        """DRY_RUN=true 时把 --apply/--yes 这类写操作参数摘掉，只保留预览。"""
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

    # 2) 发布新种子到站点
    if env_bool("ENABLE_PUBLISH", True):
        cmd = ["python", "publish.py"] + apply_flag("--yes")
        if dry_run:
            cmd.append("--dry-run")
        n = env_int("PUBLISH_MAX_PER_CYCLE", 0)
        if n:
            cmd += ["--limit", str(n)]
        stages.append({
            "name": "publish",
            "cmd": cmd,
            "interval": env_int("PUBLISH_INTERVAL", 1800),
        })

    # 3) 回填 site_torrent_id。编辑的前置条件，频率可以很低。
    if env_bool("ENABLE_SYNC", True):
        stages.append({
            "name": "sync",
            "cmd": ["python", "sync_site.py"] + apply_flag("--apply"),
            "interval": env_int("SYNC_INTERVAL", 86400),
        })

    # 4) 补历史缺失封面。要读音乐目录 + 传图床，限量跑。
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

    # 5) 编辑站点页面。--stale-only 只处理"本地比站点新"的记录：
    #    没有它每轮都要把 250+ 个页面全拉一遍 edit.php 做对比，纯空转。
    if env_bool("ENABLE_EDIT", True):
        cmd = ["python", "update_metadata.py", "--stale-only"] + apply_flag("--apply", "--yes")
        n = env_int("EDIT_MAX_PER_CYCLE", 20)
        if n:
            cmd += ["--limit", str(n)]
        if env_bool("EDIT_VERIFY", True):
            cmd.append("--verify")
        cmd += ["--interval", str(env_int("EDIT_REQUEST_INTERVAL", 2))]
        # 每分钟处理多少个种子。0 = 不限（此时只受 --interval 的逐请求间隔约束）
        rate = env_int("EDIT_RATE_PER_MIN", 10)
        if rate > 0:
            cmd += ["--rate", str(rate)]
        stages.append({
            "name": "edit",
            "cmd": cmd,
            "interval": env_int("EDIT_INTERVAL", 21600),
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
    log("     docker compose run --rm musopia python check_db.py")
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


def beat():
    """心跳文件给 docker healthcheck 用。"""
    try:
        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


# ==================== 阶段执行 ====================

def run_stage(stage, timeout):
    """
    起子进程跑一个阶段，子进程输出直接透传到 docker logs。
    返回 True 表示成功（exit code 0）。
    """
    name, cmd = stage["name"], stage["cmd"]
    log("─" * 60)
    log(f"▶️  阶段 [{name}] 开始: {' '.join(cmd)}")
    started = time.time()

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
        return False

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
        return False
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait(timeout=30)
        raise

    took = time.time() - started
    if code == 0:
        log(f"✅ 阶段 [{name}] 完成，耗时 {took:.1f}s")
        return True

    log(f"❌ 阶段 [{name}] 退出码 {code}，耗时 {took:.1f}s")
    return False


def sleep_interruptible(seconds):
    """按秒切片睡，好让 SIGTERM 能及时生效，而不是睡满一个周期才响应。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _shutdown:
            return
        time.sleep(min(1.0, max(0.0, deadline - time.time())))


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
        log(f"   · {s['name']:<8} 每 {s['interval']:>6}s   {' '.join(s['cmd'])}")
    log("=" * 60)

    # 建表放在这里做一次，后面各阶段的 init_db() 都是幂等的补刀
    import db
    db.init_db()

    # 空库 + 启用发布 = 可能重复发布几百个种子，必须拦在跑起来之前
    if not preflight(stages):
        sys.exit(2)

    state = load_state()

    if run_on_start:
        # 首轮立刻全跑一遍：把 last_run 清成 0 即视为已到期
        for s in stages:
            state.setdefault(s["name"], {})["last_run"] = 0

    fails = {s["name"]: 0 for s in stages}

    while not _shutdown:
        beat()
        now = time.time()

        for stage in stages:
            if _shutdown:
                break

            name = stage["name"]
            st = state.setdefault(name, {"last_run": 0})
            last = st.get("last_run", 0) or 0

            # 连续失败退避：1x, 2x, 4x... 上限 MAX_BACKOFF
            n_fail = fails.get(name, 0)
            wait = stage["interval"]
            if n_fail:
                wait = min(stage["interval"] * (2 ** min(n_fail, 6)), max_backoff)

            if now - last < wait:
                continue

            ok = run_stage(stage, stage_timeout)

            st["last_run"] = time.time()
            st["last_ok"] = ok
            st["last_finish"] = datetime.now().isoformat(timespec="seconds")
            if ok:
                fails[name] = 0
            else:
                fails[name] = n_fail + 1
                nxt = min(stage["interval"] * (2 ** min(fails[name], 6)), max_backoff)
                log(f"↩️  [{name}] 连续失败 {fails[name]} 次，下次至少等 {nxt}s")
            save_state(state)
            beat()

        if _shutdown:
            break
        sleep_interruptible(tick)

    log("👋 编排器已退出。")


if __name__ == "__main__":
    main()
