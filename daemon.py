"""
容器化编排器：常驻进程，按各自周期依次调度
scan / publish / sync / covers / edit / vtid / crawl。

为什么不用 cron：
1. 这些阶段有严格的先后依赖（先扫出种子才有东西可发，先拿到页面 id 才能抓页面），
   cron 各自定时会出现"发布还没跑完就开始编辑"的交叉执行，两个进程同时写
   同一个 SQLite 就会 database is locked。这里串行调度，天然互斥。
   ★ 例外：crawl（按 tid 抓皇后详情页）与上面几个只共用 `tasks` 表、
     不互为前置 —— 它默认还是关的（`ENABLE_CRAWL=false`）。
   ★ 但 `vtid`（从 vertex2 库拿皇后页面 id）**必须**排在 crawl 之前：先有 id 才有页面可抓。
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
        """DRY_RUN=true 时把 `--apply`/`--yes`/`--auto` 这类"真动手"参数摘掉。

        ★★ 2026-09-24 加了对 `--auto` 的**告警**（这条注释比代码重要）：
          摘掉 `--yes` 的后果是"**静默降级成交互确认**" —— 而容器里没有 TTY，
          `input()` 立刻拿到 EOF ⇒ 它**看起来像"被拒绝"、实际是"没人问过"**。
          摘掉 `--auto` 的后果**更重**：`publish.py` 的第二把钥匙只在
          `--auto` 为真时校验 ⇒ 摘掉它 = **把那道保险关掉**。
          ⇒ 所以这里对 `--auto` 只**打印**、若真被摘掉要说清楚，
            绝不能让它变成一个"安静的成功"。
          ★ 但注意：**真正的防线在 `publish.py` 自己**（钥匙只在 `--auto` 时校验）。
            下面这段只是"把静默变成响声"，**不是**保险本身 ——
            别把它当成"有它就不用管 `--auto` 传没传了"（`ERR-META-01`）。
        """
        if dry_run:
            stripped = [f for f in flags if f in ("--yes", "--auto")]
            if stripped:
                log(f"⚠️  DRY_RUN=true ⇒ 摘掉 {stripped}（不会真动手；"
                    f"★ 若其中含 --auto，publish 阶段的第二把钥匙随之不校验）")
            # ★★ 2026-09-24 实测补的一条：摘掉 `--yes` 看起来像"少一层保险"，
            #   但它在**真实路径**上的后果是**这个阶段直接崩** —— `publish.py`
            #   会走进 `input()`，容器里没有 stdin ⇒ `EOFError` ⇒ 退出码 1
            #   （2026-09-24 17:02 实测，见 publish 阶段的日志）。
            #   ★ 但本函数**只在 DRY_RUN=true 时**摘参数，而 DRY_RUN=true 时
            #     `publish.py` 会在 `args.dry_run` 那一支**先 return**（早于
            #     `input()`）⇒ **实际不会崩**。
            #   ⇒ 所以这里**只告警**，不为了"看起来更安全"去改摘参数的行为
            #     （把 `--yes` 留下来 = 让 dry-run 真去发，方向正好反了）。
            #   ★ 留这段注释是为了把"为什么 DRY_RUN 下不会崩"写清楚 ——
            #     否则下一个人看到"摘掉了 --yes"会以为又踩了同一个坑。
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
        # ★★ 2026-09-24：从 `publish.py --yes` 改为 `publish.py --auto`。
        #    **不是改名字，是把"确认"换了个人按。**
        #
        #    ★ 原先这一行传的是 `--yes` —— 而 `--yes` 的语义是**跳过交互确认**，
        #      也就是「**没有**人确认」。而发布是本项目**唯一不可逆**的动作。
        #      ⇒ 原设计里，"自动发"与"人发的"共用同一条路径，两者唯一的区别
        #        只是"有没有 TTY" —— 那不是保险，那是**没人**。
        #
        #    ★ 现在把两件事**分开**：
        #      · `--yes`   = 跳过**交互**确认（人手工跑时用）
        #      · `--auto`  = 声明"这是无人值守路径" ⇒ `publish.py` 里那道
        #                    `guard_auto_key()` **必须**通过（`PUBLISH_I_AM_HERE`）。
        #      两把钥匙（`ENABLE_PUBLISH` + `PUBLISH_I_AM_HERE`）放在**两处**：
        #      前者是容器的长期开关，后者要在**每次执行时**都成立。
        #      ★ 细节见 `publish.py` 的模块 docstring（"每个保险里放一把钥匙"）。
        #
        #    ★★ 为什么**两个都要传**（`--yes` 与 `--auto`）—— 2026-09-24 实测踩过：
        #
        #      `--yes` 与 `--auto` 管的是**两件不同的事**，少一个就崩：
        #        · `--yes`  = 跳过**交互确认**（`main()` 里那个 `input()`）
        #        · `--auto` = 声明"无人值守" ⇒ `publish.py` 校验第二把钥匙
        #      ★ 上一次改动把 `--yes` 换成 `--auto`（以为"确认换个人按"），
        #        结果：daemon 不带 `--yes` ⇒ `main()` 走进 `input()` ⇒
        #        **容器里没有 stdin ⇒ EOFError ⇒ 整个阶段崩**（实测退出码 1）：
        #          File "/app/publish.py", line 512, in main
        #              answer = input("确认向站点发布这 1 条吗？(y/N): ")
        #          EOFError: EOF when reading a line
        #      ★★ 当时之所以没被发现，是因为那段注释里写着
        #        "`run_auto()` 自己补上 `--yes`，且**只**走那一条路" ——
        #        而 **daemon 根本不调 `run_auto()`**（它直接起 `publish.py` 子进程，
        #        走的是 `main()`）。`run_auto()` 是个**没人调用的死函数**，
        #        却让理由看起来很完整（`ERR-META-01`：判据看着对，但覆盖的不是那条路）。
        #      ⇒ 教训：**改"谁传什么参数"时，必须去查真实调用链**，
        #        不能信某个函数的名字或它自己文档里的说法。
        cmd = ["python", "publish.py"] + apply_flag("--yes", "--auto")
        n = env_int("PUBLISH_MAX_PER_CYCLE", 0)
        if n:
            cmd += ["--limit", str(n)]
        stages.append({
            "name": "publish",
            "cmd": cmd,
            "interval": env_int("PUBLISH_INTERVAL", 1800),
        })

    # 2b) 发布到**蟹黄堡**（第二个站点，2026-09-27 用户拍板接线）。
    #
    #   ★★ 为什么是**独立阶段**而不是给上面那个 publish 加个参数：
    #      两站的**待办队列、判重、封面列全都独立**（`site_publish` 是
    #      `(info_hash, site)` 联合主键）⇒ 一个阶段失败**不该拖累**另一个。
    #      合成一个阶段的话，"蟹黄堡的 Cookie 过期"会让**乌托邦也停发**
    #      —— 那是两台站点的故障耦合，没人要求过。
    #
    #   ★★★ 默认 **False**（`ENABLE_PUBLISH_CRABPT` 未设 = 关）。
    #      理由：它是**第二个发布目标**，而发布是本项目**唯一不可逆**的动作。
    #      ★ 与 `ENABLE_CRAWL` 的默认关**理由不同**（那个是"唯一会主动出网
    #        访问别人站点"），但**方向一致**：新增的对外写操作默认关，
    #        要开必须显式在 `.env` 加一行（`ERR-ENV-02`：缺键时默认值必须中性）。
    #
    #   ★★ 三处**必须**与乌托邦那个 publish 阶段不同（逐条都有理由）：
    #      ① `--site crabpt` —— 走 `run_crabpt()` 那条独立路径
    #      ② 周期 `CRABPT_INTERVAL`（默认 1800s）—— **单独可调**。
    #         ★ 用户 2026-09-27 明确说"节奏为什么要错开？各发各的又不影响"
    #           ⇒ 所以**不是**为了错开，纯粹是"两个独立的阶段各有一个可调的钟"。
    #           默认值与 `PUBLISH_INTERVAL` 一样是 1800s，但**改一个不动另一个**。
    #      ③ 上限 `CRABPT_MAX_PER_CYCLE`（默认 1）—— **独立于**
    #         `PUBLISH_MAX_PER_CYCLE`。★ 蟹黄堡对元数据要求比乌托邦低
    #         （用户 2026-09-27），但它毕竟是第二站 ⇒ 默认仍是 1，要提速显式配。
    #
    #   ★ `--limit` 是**承重的**：`run_crabpt()` 在**没有** `--limit` 时
    #     一次只发 1 条（不可逆操作的保守默认）。daemon 传它 = 显式声明批量意图。
    #
    #   ★★ `--auto` 也是**承重的**：`run_crabpt()` 里 `if args.auto:` 那一支
    #      校验第二把钥匙（`PUBLISH_I_AM_HERE`）。**不传 `--auto` ⇒ 那把钥匙
    #      根本不校验** ⇒ 无人值守路径上少一道保险（`ERR-META-12`）。
    if env_bool("ENABLE_PUBLISH_CRABPT", False):
        # ★★ `--yes` 与 `--auto` **都要**（2026-10-02 修）：
        #   `--yes` 跳过交互确认；`--auto` 校验第二把钥匙。
        #   ★ 原先这里只传了 `--auto`、**漏了 `--yes`** ⇒ 容器里没有 TTY，
        #     `input()` 立刻 EOF ⇒ 蟹黄堡发布阶段**每次都以 EOFError 崩掉**、
        #     一条都发不出去（而乌托邦那条（161 行）一直是 `--yes --auto` 两个都传）。
        #   ⇒ 这正是 ERRATA 里"保险看起来在、其实没生效"那族（第二把钥匙在 `--auto`
        #     为真时才校验，所以 `--auto` 必须在；而少了 `--yes` 则连门都进不去）。
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

    # 6) ★★ VT 回填：从 vertex2 的库按 infohash 拿皇后页面 id（`tasks.opencd_tid`）。
    #    ★ 必须排在 crawl **之前** —— `build_stages()` 的注释定了规矩：
    #      "按执行优先级从上到下排列"。先拿到 tid，crawl 才有页面可抓。
    #
    #    ★★ 为什么它是**新的唯一 tid 来源**（2026-09-23）：用户决定删除
    #      「按种子名称搜索皇后」那条路（原来在 `crawl.py --from-qb` 里）。
    #      对比：按名字搜 实测命中 1/547 且会误配；按 infohash 从 VT 库配 **430/547**。
    #
    #    ★ 为什么默认 **True**（与 crawl 的默认关正好相反）：
    #      它是**纯本地、零请求**的 —— `sync_opencd_tid.py` 纪律 5 明写
    #      "不 import requests、不 import crawl"，且只读打开别人的库
    #      （`mode=ro` + `query_only=1`）。⇒ 默认开着**不改变对外行为**。
    #    ★★ 但它**会写本项目的库**（`--apply` 只填 `opencd_tid` 空白列）。
    #      `DRY_RUN` **不**管它 —— 同 crawl 的理由：`DRY_RUN` 管的是
    #      "往 Musopia 写"，而这个阶段动的是本地库的**一个空白列**。
    #
    #    ★★ 退出码是**承重的**：`sync_opencd_tid.py` 纪律 4 规定
    #      "源读不到 ⇒ 非零退出，**绝不**降级成'没有可回填的行'"。
    #      而 daemon 的"连续失败 ⇒ 指数退避"正是**想要的行为**：
    #      VT 挂载丢了 / 库改名 / 表结构漂了 ⇒ 阶段失败 ⇒ 退避 ⇒ 日志可见。
    #      ⇒ **不要**给它加"失败也返回 0"的豁免（那会把
    #      `ERR-META-01`「检测方法失效比不检测更危险」请回来）。
    if env_bool("ENABLE_VTID", True):
        stages.append({
            "name": "vtid",
            "cmd": ["python", "sync_opencd_tid.py", "--apply"],
            # ★ 24h 与 sync 同周期。它没有"每天都变"的东西 ——
            #   新种子靠 vertex2 那边的 RSS 周期进库，一天一次足够。
            "interval": env_int("VTID_INTERVAL", 86400),
            # ★★ **故意不加 `pending` 探针**：它的"积压"= "还有多少 tid 没回填"，
            #    而那要**同时读两个库**才知道（本地 tasks × 外部 vertex）。
            #    每 30s tick 去 join 一次别人家的库，代价与收益完全不成比例。
        })

    # 7) 按 tid 抓皇后详情页（补 `mapped_json` 等页面内容）。
    #
    #    ★★ 2026-09-23 改造：原来这里跑 `crawl.py --from-qb`
    #      （扫 qB → 按名字搜皇后 → 抓页面）。名字搜索删除后那条命令**没有
    #      匹配手段了**，⇒ 本阶段改成跑 `topup_opencd_fields.py`：
    #      它从库里选"有 tid 但 mapped_json 为空"的行，逐条抓详情页。
    #
    #    ★ 为什么仍然要抓页面（VT 不替代它）：`details.php` 摘要是那些字段的
    #      **唯一来源** —— 转载规则 / 标签 / 媒介 / 製作組 / 艺术家 / 年份。
    #      VT 库只给页面**编号**，不给正文（实测：`tools/_probe_vertex_columns.py`
    #      把每一列都列过，没有规则字段）。`db.PENDING_PUBLISH_WHERE` 的
    #      闸门③④ 正是因此存在。
    #
    #    ★ 为什么默认关（`ENABLE_CRAWL` 默认 **False**，保持不变）：
    #      它是**唯一会主动出网访问别人站点**的阶段。默认关 = 升级镜像不会
    #      改变对外行为，要开必须显式加一行 .env。安全默认。
    #      ★ 用户 2026-09-23 明确说"先不开，手动看看效果" ⇒ 保持 False。
    if env_bool("ENABLE_CRAWL", False):
        limit = env_int("CRAWL_LIMIT_PER_ROUND", 20)
        cmd = ["python", "topup_opencd_fields.py", "--limit", str(limit)]
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
        #    ★ 真跑时才加 `--apply`（`topup` 的默认是**只读预览**）。
        if env_bool("OPENCD_CRAWL_DRY_RUN", True):
            cmd.append("--dry-run")
        else:
            cmd.append("--apply")
        stages.append({
            "name": "crawl",
            "cmd": cmd,
            # ★ 1800s（与 scan 同周期；用户 2026-09-22 定，从原定 300s 改来）。
            #   为什么从 300 改到 1800：300s 下每天 ~288 轮，而每日请求上限只有
            #   50 ⇒ 配额**每轮都用不完就撞日上限**，`roll_day` 与账本变成装饰；
            #   1800s 下每天 ~48 轮，「每天 50 请求」才成为**真正的紧约束**。
            "interval": env_int("CRAWL_INTERVAL", 1800),
            # ★★ **故意不加 `pending` 探针**（别的短周期阶段都有）。
            #    探针的用途是"有积压就比周期跑得更勤"，但本阶段的积压信号
            #    （"还有多少行没补页面"）虽然**在调度进程里算得出**，
            #    却有一个更硬的理由不加：**配额用尽时 `pending` 会连续几小时
            #    为 >0** ⇒ 每 `cooldown` 秒就起一轮 ⇒ 每轮都发一次皇后的
            #    **cookie 探活** —— 正是 `_wait_seconds()` docstring 警告的
            #    "站点被悄悄敲得更勤"。⇒ 宁可用固定周期。
            #    ★ 幂等性由 `topup_tried.jsonl` 台账保证（试过的不再选），
            #      不靠探针（见 `topup_opencd_fields.py` 纪律 3）。
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


# ★★ 容器内**生效的**开关快照 —— 给本机的巡检脚本读。
#
# 为什么需要它：真正决定"哪些阶段关着"的是**容器内的环境变量**，而本机
# 既读不到 NAS 上那份 `.env`（它在 deploy.sh 的 NEVER 名单里，ERR-ENV-02），
# 也进不去容器（SSH 未开）。于是本机只能**猜** —— 而猜错的代价已经实测过：
# 2026-09-22 的巡检把 `ENABLE_PUBLISH=false` 当成了 `true`，
# 于是「publish 停在 67h」这个**预期内**的现象被报成了「真异常」。
#
# ★ 为什么要由**容器自己**写：手抄一份值到 NAS 目录，就是又做了一个"投影"，
#   它必然会漂（ERR-META-03）。让容器把 `os.environ` 原样落盘 ⇒
#   **只有一个来源**，且它天然是"生效值"而不是"配置文件里写的值"
#   （两者可以不一致：比如 compose 的 ${VAR} 插值、或 `.env` 被漏挂）。
# ★ 为什么写 `/data`（bind mount 到宿主机 compose 目录）：那是**唯一**
#   本机能看到的容器内产物落点。写镜像里的 `/app` 本机看不到。
# ★ 为什么只挑这几个键：只写**判据要用的**，不把 `os.environ` 整个倒出去 ——
#   环境里有 `MUSOPIA_COOKIE` / `QB_PASS` / 图床 key 这类凭据，
#   整个 dump 到 SMB 上等于把生产密钥写在共享盘里（本项目 `.env` 都还没上过盘）。
#   ★★ 这是**白名单**，不是黑名单：新加开关要**显式**加进来。
#      选白名单的理由：漏加一个键 = 那个键在快照里"不存在"（看得见、会被当成空），
#      而黑名单漏屏蔽一个键 = **凭据静默泄漏**。两害相权，宁可见地缺。
ENV_SNAPSHOT_FILE = os.path.join(config.DATA_DIR, "daemon_env.json")
ENV_SNAPSHOT_KEYS = (
    "DRY_RUN",
    "ENABLE_SCAN", "ENABLE_PUBLISH", "ENABLE_SYNC",
    "ENABLE_COVERS", "ENABLE_EDIT", "ENABLE_CRAWL",
    # ★ 2026-09-23 加的 vtid 阶段。**白名单漏加 = 巡检看不见它是开是关**
    #   （见上面的选白名单理由：宁可看得见地缺，不要静默地漏）。
    "ENABLE_VTID",
    "OPENCD_CRAWL_DRY_RUN", "ALLOW_EMPTY_DB",
    # ★★ 2026-09-24 加的自动发布钥匙。**它必须进白名单**，理由与 ENABLE_PUBLISH
    #   完全相同 —— 而恰恰是"判据要用的"：
    #   · 巡检要能回答"**自动发布现在能不能发**"，而不必去读容器日志；
    #   · 它的值**不是密钥**（`i-am-here` 是个约定短语，写在源码里），
    #     所以放进选择性落盘的白名单没有凭据外溢的问题；
    #   · ★ 反过来说：**不进白名单就看不见它** ⇒ 那正是 `ENABLE_PUBLISH`
    #     被巡检连续多轮写反的同一个形状（"看不见的开关 = 会被当成没配"）。
    "PUBLISH_I_AM_HERE",
    # ★★ 2026-09-24 加的两道**发布限速**旋钮。它们必须进白名单，理由与前一条
    #   完全相同（"看不见的开关 = 会被当成没配"），而这里还多一层：
    #   ★ 本机**读不到 NAS 的 `.env`**（它在 deploy.sh 的 NEVER 名单里，ERR-ENV-02）
    #     也 SSH 不进去 ⇒ **"新的上限到底生效没有"这件事，唯一能问的就是这份快照**。
    #     不落盘 ⇒ 本轮改动的**全部意图无法从本机验证**。
    #   ★ 两个值都是**普通整数**（不是凭据），放进选择性落盘的白名单没有外溢问题。
    #   ★ 它们**不参与真假判断**，只做"报数"用：巡检会把
    #     `每日上限 = 86400/PUBLISH_INTERVAL × PUBLISH_MAX_PER_CYCLE` 直接印出来
    #     —— 本轮用户问"现在每天最多发多少条"，那个数字不该靠人算。
    "PUBLISH_MAX_PER_CYCLE", "PUBLISH_MIN_GAP",
    # ══════════════════════════════════════════════════════════════════════
    # ★★★ 2026-09-27 加的：**调度语义**那两个族 —— `RUN_ON_START` + 全部 `*_INTERVAL`。
    #
    #   为什么必须进白名单（与上面 `PUBLISH_INTERVAL` 同一条理由，但这里更硬）：
    #   ★ `RUN_ON_START` **不是普通开关，它改变的是"时间"的含义**：
    #       · `true`  ⇒ 启动时把**所有** `last_run` 清 0 = 立刻全跑一轮（见 main()）
    #       · `false` ⇒ **沿用** state 里的旧 `last_run`，按 `now - last < interval` 判到期
    #     所以"容器刚重建完，为什么 scan 没跑"这个问题的答案，
    #     **完全由这个键决定**，而它原先**看不见** ⇒ 只能登 NAS `docker exec env` 问。
    #   ★ 实测代价（2026-09-27 本机巡检）：我把"日志里没有 scan"读成了「重启后停摆」，
    #     还据此建议改代码 —— 真因就是 `RUN_ON_START=false`（沿用旧 last_run，
    #     scan 尚未到期）。★ 判据本身不存在，结论只能靠猜（`ERR-META-01`）。
    #
    #   ★ 同理带上全部 `*_INTERVAL`：巡检脚本要回答"这个阶段**该不该动**"，
    #     就必须知道它的周期；而周期**只有容器知道**（`.env` 在 NEVER 名单里，
    #     本机读不到，`ERR-ENV-02`）。不落盘 ⇒ 巡检只能用**硬编码的过期副本**
    #     去判 —— 2026-09-27 实测那份副本写着 `publish 1800`，真值是 `5400`
    #     （同形错的第二次：**一个量两处来源**，`ERR-META-03`）。
    #
    #   ★ 安全：这些值**全是普通整数/布尔**，不含任何凭据（`.env` 里的密钥
    #     没有一个在这里）⇒ 放进选择性落盘的白名单没有外溢问题。
    # ══════════════════════════════════════════════════════════════════════
    "RUN_ON_START",
    "SCAN_INTERVAL", "PUBLISH_INTERVAL", "SYNC_INTERVAL", "COVERS_INTERVAL",
    "EDIT_INTERVAL", "VTID_INTERVAL", "CRAWL_INTERVAL",
    # ══════════════════════════════════════════════════════════════════════
    # ★★★ 2026-09-27：**蟹黄堡（第二站）**那三个键。
    #
    #   为什么必须一起进白名单 —— 三条**各自的**理由，不是"顺带加的"：
    #   ① `ENABLE_PUBLISH_CRABPT`：它是**第二个不可逆的对外写操作**。
    #      巡检要能回答"蟹黄堡现在会不会自动发"，而 .env 本机读不到
    #      （NEVER 名单，`ERR-ENV-02`）⇒ 不进白名单就**看不见它开没开**，
    #      这正是 `ENABLE_PUBLISH` 被连续多轮写反的那个形状（`ERR-META-04`）。
    #   ② `CRABPT_INTERVAL`：用户 2026-09-27 明确要它与乌托邦**各自独立可调**。
    #      而"独立可调"若在本机**看不出来**，就等于没实现（判据必须可观测）。
    #   ③ `CRABPT_MAX_PER_CYCLE`：它是**发布速率**的一半（乘数）。
    #      ★ 上面 `PUBLISH_MAX_PER_CYCLE` 那段注释已经写明"不落盘 ⇒
    #        本轮改动的全部意图无法从本机验证" —— 蟹黄堡这里**一字同理**。
    #
    #   ★ 安全：三个值都是**普通整数/布尔**，不含凭据
    #     （`CRABPT_COOKIE` 是凭据，**不在**白名单里 —— 见 `CRABPT_URL` 那类
    #      也刻意不落盘的既有做法）。
    # ══════════════════════════════════════════════════════════════════════
    "ENABLE_PUBLISH_CRABPT", "CRABPT_INTERVAL", "CRABPT_MAX_PER_CYCLE",
)


def save_env_snapshot():
    """把容器内**生效的**白名单开关写成 JSON（原子替换）。

    ★ 每次都带 `generated_at` 与 `pid`：快照的**新鲜度**是它自己要说清的事
      （一份停在月前的快照就是 ERR-META-04 说的"传说"）。
    ★ 值一律存**字符串原样**，不在这里转 bool ——
      转义规则在 `env_bool()`，抄一份到这里就会漂（ERR-META-03）。
      读的人要判真假就自己按同一套规则判，或者只看"缺键 vs 有键"。
    """
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
    """
    起子进程跑一个阶段，子进程输出直接透传到 docker logs。

    ★★ 返回一个 **dict**（不是 bool）—— 见 `stage_result_ok()`：
      `{"ok", "code", "took", "started_at"}`。

    ★ 2026-09-27 为什么从 bool 改成 dict：
      原先只回 `True`/`False`，于是 `save_state` 落盘的 `last_ok` 是**布尔**，
      **退出码和耗时全丢了**。而 `topup_opencd_fields.py` 的"今日额度用完"
      是**退出码 0 的正常收工**（它自己叫"保护，不是错误"）⇒ 盘上只留下
      `last_ok=true`、无 `↩️`、日志里一行没有，**与"真抓成功若干条"完全同形**。
      2026-09-27 我就据此把一次"配额满、零请求收工"读成了"看不见的异常"。
      ⇒ 退出码与耗时是**承重信息**，必须落盘（`ERR-META-01` 家族：
        观测手段分辨不出的两件事，在盘上就成了同一件事）。
    """
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
    """把"为什么这轮跳过"写成人能读的一行，随 state 落盘。

    ★ 只描述**算法自己的判据**，不做任何推测 —— 例如不写"配额满了"：
      调度器**无从知道**配额（那是 crawl 阶段内部的事，要连库/出网才知道）。
      写上去就是把"我猜的"当成"我查到的"（`ERR-META-14`）。
    """
    return f"未到期：距上次 {age:.0f}s < 周期 {wait:.0f}s"


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


# ★★ 延迟回加队列的"排空"（2026-10-03 加）—— 延迟做种的执行者。
#
# 干什么：`publish.py` 发布成功后，若 `SEED_DELAY_MIN>0`，**不立即**把种子加进
#   qB，而是写进 `/data/readd_queue.json`（含 `.torrent` 字节）。本函数在主循环
#   里被调用，到点了才 `torrents_add` —— qB 收到后**自己**校验、自己做种。
#
# ★ 为什么塞进 `opencd2musopia` 的主循环（用户 2026-10-03 拍板）：
#   不新开容器、不塞进 qbtune（那个是下载调度、可停）；复用本容器已有的 30s
#   主循环节奏即可，天生满足"到点±30s 内回加"的精度要求。
#
# ★★ 为什么放在 `for stage in stages` **之外**、主循环末尾：
#   它是**事件驱动**（到点必须做），不是按 interval 跑的阶段 ⇒ 塞进 stage 框架
#   会被 interval/退避/pending 语义扭曲。主循环末尾一小步最干净。
#
# ★★★ 为什么失败要**留在队列里重试**、且**绝不 raise**：
#   这是 daemon 主循环里的一步 —— 抛出去会打断**所有**阶段。qB 连不上是常态
#   （它现在还在崩），必须降级：本轮跳过、下轮再来，且 attempts 有上限防死循环。
#
# ★ 幂等：`torrents_add` 对**已在 qB 里**的 infohash 是安全的（qB 会忽略重复）。
#   即使"别的容器抢先加了"（已知干扰），这里再 add 一次也无害 ⇒ 不额外检测。
#   见 `readd_queue.py` 模块 docstring 的"已知干扰"。
READD_MAX_ATTEMPTS = 20          # 每轮最多处理几条（别让一次排空卡住主循环）
READD_GIVE_UP = 96               # 一条试了这么多次还失败 ⇒ 放弃并报警（≈2 天）


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

    # ★ 只在**真有到点条目**时才连 qB（没有待办就一条连接都不建）
    try:
        from qbittorrentapi import Client
        qb = Client(host=config.QB_HOST, username=config.QB_USER,
                    password=config.QB_PASS)
        qb.auth_log_in()
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
            content = base64.b64decode(it["torrent_b64"])
            qb.torrents_add(
                torrent_files=content,
                save_path=it.get("save_path") or "",
                is_paused=False,
                category=it.get("category") or "",
            )
            done.append(it)
            log(f"✅ [readd] 延迟到点，已回加 qB（{note}）")
        except Exception as e:                                  # noqa: BLE001
            it["attempts"] = it.get("attempts", 0) + 1
            if it["attempts"] >= READD_GIVE_UP:
                log(f"❌ [readd] {note} 连续 {it['attempts']} 次回加失败，**放弃**"
                    f"（种子已上站、不会做种，需人工介入）: {type(e).__name__}: {e}")
                done.append(it)                                 # 从队列移除（放弃）
            else:
                failed.append(it)
                log(f"⚠️  [readd] {note} 回加失败（第 {it['attempts']} 次），下轮重试: "
                    f"{type(e).__name__}: {e}")

    # 没在本轮处理的（超过 READD_MAX_ATTEMPTS 的）原样留下
    leftover = due[READD_MAX_ATTEMPTS:]
    try:
        readd_queue.ack(
            [it.get("infohash") for it in done],
            rest + failed + leftover,
        )
    except Exception as e:                                      # noqa: BLE001
        log(f"⚠️  [readd] 写回队列出错（成功的那几条可能下轮重复 add，无害）: {e}")


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
    """调度主循环。★ 从 `main()` 里抽出来，**只为一件事：能被测**。

    ★★ 为什么值得抽（2026-09-27）：这次的 bug 是「跳过的阶段在盘上冒充自己跑过」，
      而它**只在"真的驱动一轮循环 + 真的读回文件"时才暴露** ——
      测 `save_state` 之类的单元函数是测不到的（问题不在它内部，
      而在**谁调用它、什么时候**）。`tools/test_daemon_state.py` 直接调本函数，
      用假阶段驱动真代码，再读回 `STATE_FILE`。
      ⇒ 抽它是为了让那条判据**可执行**，不是为了"结构好看"。
    """
    tick = env_int("SCHED_TICK", 30) if tick is None else tick
    stage_timeout = env_int("STAGE_TIMEOUT", 3600) if stage_timeout is None else stage_timeout
    fails = {s["name"]: 0 for s in stages}

    while not _shutdown:
        beat()
        now = time.time()
        # ★★ 2026-09-27：**本轮有没有阶段被跳过**。用来决定循环末尾要不要落盘。
        #
        #   为什么需要它（我第一版改动就在这里静默失效了）：`save_state(state)`
        #   原先只在**真跑了**阶段之后调用（在 for 内部、`continue` 之后）。
        #   跳过支是纯 `continue` ⇒ 我在那里写的 `last_skip_at` **只进了内存，
        #   从不落盘** ⇒ 加了跟没加一样。
        #   ★ 这正是 `ERR-META-01` 的形状："改动看起来对，但它测的东西不落盘"。
        #     ⇒ 判据必须钉住**文件内容**，不是"我写了这行代码"。
        #
        #   在**循环末尾**（而不是每个跳过支里）落盘还有一个理由：一轮里 7 个
        #   阶段全跳过时，每阶段都写一次 = 每 30s 写 7 次盘，纯属浪费 SD 卡寿命。
        skipped = False

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
                # ★★★ 2026-09-27：跳过时**必须留下痕迹**。
                #
                #   原先这一支是**纯 `continue`**，而 `last_finish` 与 `last_run`
                #   在**同一时刻、同一行**写入 ⇒ 盘上出现了这个歧义：
                #     「这一轮跑了 7 个阶段」与「这一轮**一个都没跑**」
                #     留下的 `last_finish` **一模一样**（都是上一轮的时刻）。
                #   2026-09-27 我就据此把一次「首轮只有 crawl 到期、其余 6 个跳过」
                #   读成了「22:27:06 跑了 edit」。
                #
                #   ⇒ 两处改动：
                #     ① 跳过时写 `last_skip_at`（**不动** `last_run`/`last_finish`）
                #     ② `last_finish` 只在**真跑了**的分支里写
                #   ⇒ 之后「`last_finish` < 容器启动时刻」= **从没跑过**，
                #     不再依赖"我记不记得启动时间"这种脑内状态。
                #
                #   ★ 这里**不额外 log**：本循环每 30s 跑一次，7 个阶段全跳过时
                #     每 30s 刷 7 行会让日志彻底没法看（真正的异常会被埋掉）。
                #     "跳过"是**盘上的事实**，不是日志事件 —— 用 state 文件表达。
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

        # ★ 延迟回加队列排空（2026-10-03）：到点的"延迟做种"在这里真正 add 进 qB。
        #   放在 stage 循环**之外**（事件驱动，非 interval 阶段）；绝不 raise。
        if not _shutdown:
            _drain_pending_readds()

        if _shutdown:
            break
        sleep_interruptible(tick)

    log("👋 编排器已退出。")


if __name__ == "__main__":
    main()
