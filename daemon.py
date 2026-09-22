"""
容器化编排器：常驻进程，按各自周期依次调度
scan / publish / sync / covers / edit / crawl。

为什么不用 cron：
1. 这些阶段有严格的先后依赖（先扫出种子才有东西可发，先回填 ID 才能编辑），
   cron 各自定时会出现"发布还没跑完就开始编辑"的交叉执行，两个进程同时写
   同一个 SQLite 就会 database is locked。这里串行调度，天然互斥。
   ★ 例外：crawl（扫 qB 反查皇后）与上面五个是**两条独立流水线**，只共用
     `tasks` 表、不互为前置 —— 它默认还是关的（`ENABLE_CRAWL=false`）。
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
    import db   # 只为一件事：给 edit 阶段提供"待办数"探针（见下面的 pending）
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
        # 兜底默认值必须与 .env / README 承诺的一致（都是 100）。若这里写成别的数，
        # .env 一旦丢掉这行键就会**静默**按兜底值跑，慢几倍且不报错。
        n = env_int("EDIT_MAX_PER_CYCLE", 100)
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
            # ★ 探针式调度：edit 原先只看 EDIT_INTERVAL 这个钟（默认 6h），
            # 于是 recheck 刚标脏的记录最多要等 6 小时才被推上站点 ——
            # "想立刻生效"就只能手动开一次性容器，而那正是数据风险的来源。
            # 现在改成看**有没有活**：有待办就按 cooldown 重跑，清零才回长周期。
            # 探测口径见 db.count_stale()（与 --stale-only 共用同一份 WHERE）。
            "pending": db.count_stale,
            "cooldown": env_int("EDIT_COOLDOWN", 60),
        })

    # 6) 从 qB 反查皇后（OpenCD），抓新种子的详情页并入库。
    #    ★ 与 scan 是**两条独立流水线**，只共用 `tasks` 表，互不阻塞
    #      ⇒ 放最后纯粹因为它是新增的、**最可能被关掉**的那个，与依赖无关。
    #    ★★ 它**不会**导致任何新发布：`crawl.py` 刻意传 `qb_seed_days=None`
    #      ⇒ 每行落 `NOT_SEEDED` ⇒ 而发布队列要求 `status='SUCCESS'`
    #      （见 `db.PENDING_PUBLISH_WHERE`）⇒ 一条都进不去。这是**刻意的**，
    #      不是没做完（闸门①的纪律是「宁可晚抓，不可早发」，理由见 `crawl.py`）。
    #    ★ 为什么默认关（`ENABLE_CRAWL` 默认 **False**）：它是**第一个会主动
    #      出网访问别人站点**的阶段。默认关 = 升级镜像不会改变对外行为，
    #      要开必须显式加一行 .env。安全默认。
    if env_bool("ENABLE_CRAWL", False):
        cmd = ["python", "crawl.py", "--from-qb"]
        # ★★ dry-run 用**专用**变量 `OPENCD_CRAWL_DRY_RUN`（默认 **True**），
        #    不复用上面那个 `DRY_RUN`，两个理由：
        #    ① `DRY_RUN=false` 会**同时**打开 publish 与 edit 的写操作
        #       （见 `apply_flag`），而"我想抓皇后页面"与"我想往 Musopia 发种"
        #       是**两个站、两个风险面**，必须各自可控；
        #    ② 极性相反：`DRY_RUN` 默认 false（=写），而 crawl 要求默认 true
        #       （=不写，"部署时先观察"），一个变量不可能有两个默认值。
        #    ★ 顺带：所以这里**不能用 `apply_flag`**（它是 opt-out 语义：
        #      DRY_RUN=true 时把 flag 摘掉）；crawl 的 `--dry-run` 是 opt-in，
        #      要**加**上去。别为了"统一"改成 apply_flag，那会正好反过来。
        if env_bool("OPENCD_CRAWL_DRY_RUN", True):
            cmd.append("--dry-run")
        stages.append({
            "name": "crawl",
            "cmd": cmd,
            # ★ 1800s（与 scan 同周期；用户 2026-09-22 定，从原定 300s 改来）。
            #   为什么从 300 改到 1800：300s 下每天 ~288 轮，而每日请求上限只有
            #   50 ⇒ 配额**每轮都用不完就撞日上限**，`roll_day` 与账本变成装饰；
            #   1800s 下每天 ~48 轮，「每天 50 请求」才成为**真正的紧约束**。
            "interval": env_int("CRAWL_INTERVAL", 1800),
            # ★★ **故意不加 `pending` 探针**（别的短周期阶段都有）。
            #    探针的用途是"有积压就比周期跑得更勤"，但 crawl 的积压信号
            #    （qB 里出现了新种子）**在调度进程里看不到** —— 要么把
            #    `qbittorrentapi` 引进调度器（重依赖 + 每 30s tick 就问一次），
            #    要么猜。**更糟的是它有害**：配额用尽时 `pending` 会连续几小时
            #    为 >0 ⇒ 每 `cooldown` 秒敲一次皇后的 **cookie 探活** ——
            #    正是 `_wait_seconds()` docstring 警告的"站点被悄悄敲得更勤"。
            #    ⇒ 宁可用固定周期；"该不该跑"由周期决定，不由探针决定。
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


def _wait_seconds(stage, st, n_fail, max_backoff, pending_now):
    """
    这个阶段下次到期前要等多少秒。抽成纯函数是为了能单独测 ——
    这里的优先级搞反的话，表现不是报错，而是"站点被悄悄敲得更勤"或"该干的没干"。

    优先级：
      1. 连续失败退避 —— 失败时一定拉长，且**压过**下面的短冷却。
         不能因为"还有待办"就在失败后继续 60s 一次地试。
      2. 有活 + 上轮推进过 → 短冷却。
      3. 其余（没探针 / 没活 / 上轮没推进）→ 常规周期。
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
            pending_now = _pending(stage)      # None = 这个阶段不看活
            wait = _wait_seconds(stage, st, n_fail, max_backoff, pending_now)

            if now - last < wait:
                continue

            before = pending_now
            ok = run_stage(stage, stage_timeout)
            after = _pending(stage) if before is not None else None

            st["last_run"] = time.time()
            st["last_ok"] = ok
            st["last_finish"] = datetime.now().isoformat(timespec="seconds")
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
            beat()

        if _shutdown:
            break
        sleep_interruptible(tick)

    log("👋 编排器已退出。")


if __name__ == "__main__":
    main()
