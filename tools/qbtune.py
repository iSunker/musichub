"""qB 下载调度器：有源优先 + 死种轮转（A）× 公平轮转 + 带宽加权（B）。

═══════════════════════════════════════════════════════════════════════════
它要解的那个瓶颈（2026-10-03 实测，不是推测）
═══════════════════════════════════════════════════════════════════════════
用户把 qB（3020）设成"最多 10 个并发下载"，解决了卡 IO，但**总速度只有 ~0.02 MB/s**
（500M 宽带近乎闲置）。实测原因**不是队列顺序**：

    下载相关种子 397 个，其中 num_seeds>0 的只有 5 个，num_seeds=0 的 392 个。

⇒ 带宽是被"**几乎没有活的 swarm**"卡住的，不是被"10 个槽位不够"。
   再花哨的排序也不能从零个源里榨出速度。

★ 但**队列顺序确实在浪费**（2026-10-03 实测当前 top-10）：prio 1~5 与 7 全是
  **`stalledDL` / 0 源**的死种，它们**占着活动槽却一动不动**，而真正有源的种子
  排在后面。qB **不会**自动把 `stalledDL` 的死种换下去 ⇒ 这正是本工具要修的东西。

═══════════════════════════════════════════════════════════════════════════
两套策略合在一个工具里（用户 2026-10-03 明确要求，**不是二选一**）
═══════════════════════════════════════════════════════════════════════════
  (A) 有源优先 + 死种轮转：活动槽只给"此刻有活 swarm"的种子；
      零源种子挪出，但按老化计数**定期回访**（源可能随时上线）。
  (B) 公平轮转 + 带宽加权：活动集内部用 wait_credit（防饿死）
      + log1p(dlspeed/seeds/swarm)（好种子多分）决定谁上谁下。

★★ A 与 B 不是并列的两套，而是**分工**：
     A 决定"**谁配进名单**"（准入门槛：dead 的排除）；
     B 决定"**名单里怎么排、管道怎么分**"（在标内打分排序）。
   少了 A ⇒ 10 个槽被 392 个没 peer 的种子按"谁等得久"平均分掉（等于缩成 0.5 个）；
   少了 B ⇒ 一个 2MB/s 的种子永久霸榜、其余饿死。

═══════════════════════════════════════════════════════════════════════════
现场实测核过的机制（2026-10-03，NAS 上，见 tools/qbtune_probe.py）
═══════════════════════════════════════════════════════════════════════════
  · qB v5.0.4 / WebAPI 2.11.2 / libtorrent 2.0.11。
  · `torrents_top_priority` **实测有效**：一个 prio=397 的种子置顶后 prio→1、
    state `queuedDL → stalledDL`（即**被调度进了活动集**）。
  · `stalledDL`（0 源）的死种**会占着活动槽不被自动换下** ⇒ 对它们
    `torrents_bottom_priority` **足以腾出槽位**（实测当前 top-10 里 6 个是死种）。
    ⇒ **不需要 `force_start`**（force 会绕过 `max_active_downloads`，反而破 10 上限）。
  · `torrents_set_download_limit(limit=-1)` = **无限**；**回读时 qB 把无限报成 `0`**
    （写 123456 回读 123456；写 -1 回读 0）。⇒ 代码里"无限"**一律写 -1**，
    **绝不写 0**（0 语义含糊且危险）。
  · **`max_active_uploads=0` ≠ 不限**（qB 不认）—— 这是另一个坑，但本工具**不碰上传**。

═══════════════════════════════════════════════════════════════════════════
纪律（与仓库一致）
═══════════════════════════════════════════════════════════════════════════
  · **只读为默认**：不传 `--apply` 只打印计划；`QBTUNE_ENABLE` 默认 false。
    两者**都**满足才写 qB（双保险）。`--dry-run` 与 `--apply` 同传时 dry-run 胜。
  · **qB 只做三类写**：`top_priority` / `bottom_priority` / `set_download_limit`。
    不 add / 不 delete / 不 pause / 不 force_start。
  · **不写 `data.db`**（compose 顶部纪律：共享 SQLite 不许两容器并发写，ERR-DOCKER-03）。
    状态只落 `/data/qbtune_state.json`，心跳落 `/data/qbtune_heartbeat`。
  · **每写必回读**（ERR-META-08：跨进程的动作，判据只能是回读）。
  · **不碰上传限速**（每种子 500KiB/s 是**做种/分享率**策略，不是下载调度的目标；
    刻意留钩子 QBTUNE_UP_LIMIT，默认不设 = 不碰）。

用法（NAS 上，一行）：
    查看计划（只读）:  docker compose run --rm tools python tools/qbtune.py --explain 40
    单跑一轮写一次 :  docker compose run --rm tools python tools/qbtune.py --apply
    常驻（compose 用）: docker compose run --rm tools python tools/qbtune.py --loop
    健康检查        :  python tools/qbtune.py --healthcheck
    复位下载限速    :  python tools/qbtune.py --restore --apply
"""
import argparse
import json
import math
import os
import signal
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.environ.get("MUSOPIA_APP_DIR", "/app"))

import config  # noqa: E402  （只为 QB_HOST/QB_USER/QB_PASS/DATA_DIR）

STATE_FILE = os.path.join(config.DATA_DIR, "qbtune_state.json")
HEARTBEAT_FILE = os.path.join(config.DATA_DIR, "qbtune_heartbeat")

# ★ 永远不写这个值当"无限"（qB 回读把无限报成 0，写 0 语义含糊且危险）。
UNLIMITED = -1

# 下载相关的 state（决定种子是否参与调度）。
DL_STATES = ("downloading", "forcedDL", "stalledDL", "queuedDL", "metaDL")
# ★ 豁免：这些 state 本来就在"动"（取元数据/校验），不能判死、不该被轮换掉。
#   metaDL 尤其关键：它的 num_seeds 常为 0，误判为 dead 会把它挤出去、永远拿不到元数据。
EXEMPT_STATES = ("metaDL", "checkingDL", "checkingResumeData", "checkingUP", "allocating")
# ★ queuedDL 必须排除出"判死"：那是"被 cap 挡在门外"，不是"没 swarm"。
#   算成 dead 会形成自我实现的死循环（被挤出去 → 排队 → 判死 → 更不回来）。
ACTIVE_STATES = ("downloading", "forcedDL")


# ==================== 基础工具（照抄 daemon.py 的同义函数）====================

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
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


def env_float(key, default):
    raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log(f"⚠️  {key}={raw!r} 不是数，用默认值 {default}")
        return default


def env_bool(key, default):
    raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


# ==================== 参数（每轮从环境读一次，便于改 .env 后重建生效）====================

class Cfg:
    def __init__(self):
        self.interval = env_int("QBTUNE_INTERVAL", 60)
        self.cap = env_int("QBTUNE_ACTIVE_CAP", 10)
        # 迟滞：进死难、复活易（两个阈值必须不同，否则退化成无迟滞）
        self.stall_enter = env_int("QBTUNE_STALL_ENTER", 5)
        self.live_exit = env_int("QBTUNE_LIVE_EXIT", 2)
        self.trickle_bps = env_int("QBTUNE_TRICKLE_BPS", 8192)
        self.trickle_enter = env_int("QBTUNE_TRICKLE_ENTER", 10)
        self.trickle_penalty = env_float("QBTUNE_TRICKLE_PENALTY", 0.5)
        self.retry_ticks = env_int("QBTUNE_RETRY_TICKS", 30)
        self.retry_boost = env_float("QBTUNE_RETRY_BOOST", 4.0)
        # 公平轮转积分
        self.credit_rate = env_float("QBTUNE_CREDIT_RATE", 1.0)
        self.credit_cap = env_float("QBTUNE_CREDIT_CAP", 1800.0)
        self.credit_decay = env_float("QBTUNE_CREDIT_DECAY", 0.5)
        # thrash 控制
        self.min_dwell_ticks = env_int("QBTUNE_MIN_DWELL_TICKS", 2)
        self.displace_margin = env_float("QBTUNE_DISPLACE_MARGIN", 0.5)
        self.write_cooldown_ticks = env_int("QBTUNE_WRITE_COOLDOWN_TICKS", 1)
        self.max_promote = env_int("QBTUNE_MAX_PROMOTE", 3)
        self.max_writes = env_int("QBTUNE_MAX_WRITES_PER_TICK", 8)
        # 带宽（限速）
        self.bw_mode = os.getenv("QBTUNE_BW_MODE", "cap_greedy").strip()
        self.greedy_k = env_int("QBTUNE_GREEDY_K", 2)
        self.greedy_headroom = env_float("QBTUNE_GREEDY_HEADROOM", 1.5)
        self.min_per_torrent_bps = env_int("QBTUNE_MIN_PER_TORRENT_BPS", 16384)
        self.budget_bps = env_int("QBTUNE_BUDGET_BPS", 0)
        self.set_limits = env_bool("QBTUNE_SET_LIMITS", True)
        # 打分权重
        self.w_speed = env_float("W_SPEED", 2.0)
        self.w_seeds = env_float("W_SEEDS", 1.5)
        self.w_swarm = env_float("W_SWARM", 1.0)
        self.w_meta = env_float("W_META", 0.3)
        self.w_wait = env_float("W_WAIT", 1.2)
        self.w_left = env_float("W_LEFT", 0.5)
        self.w_boost = env_float("W_BOOST", 3.0)
        # ★ "有源优先"硬项：无任何 swarm 信号的种子扣这个分（默认足够大，
        #   让它排到所有"有 swarm 信号"的种子之后）。
        self.no_swarm_penalty = env_float("W_NO_SWARM_PENALTY", 20.0)
        # 其它
        self.max_consec_conn_fail = env_int("QBTUNE_MAX_CONSEC_CONN_FAIL", 10)
        self.restore_on_exit = env_bool("QBTUNE_RESTORE_ON_EXIT", True)
        self.enable = env_bool("QBTUNE_ENABLE", False)


# ==================== 纯函数区（tools/test_qbtune.py 直接测，不需要 qB）====================

def _log1p(x):
    return math.log1p(max(0.0, float(x)))


def swarm_tier(t):
    """策略 A 的**准入分层**：数字越小越该先占活动槽。

    ★★ 为什么要有它（2026-10-03 实测踩到）：光靠 score 里的 log 项 + `no_swarm_penalty`
      不够 —— `fairness`（等待积分）会让一个**等得久的零源种子**的分数**超过**一个
      正在下载的种子（实测：零源 queuedDL 8.6 分 > 有源 downloading 19KB/s 5.2 分）。
      那等于"公平"把"有源优先"压倒了，与 A 的初衷相反。
      ⇒ 用**分层**先卡死："在传 > 有种子 > 有 swarm 记录 > 啥都没有"，
         分数只在**同一层内**比较。这样"有源优先"是结构性的，不会被公平项翻盘。

      0 = 正在传输（dlspeed>0）—— 最强信号
      1 = 有种子连接（num_seeds>0）
      2 = 有 swarm 记录（num_complete/num_incomplete 有值）—— tracker 说存在，可能连上
      3 = 啥信号都没有 —— 物理上大概率不动
    ★ 豁免 state（metaDL 等）单独给 -1（最优先），保证它们能进来把元数据/校验做完。
    """
    st = getattr(t, "state", "") or ""
    if st in EXEMPT_STATES:
        return -1
    if (getattr(t, "dlspeed", 0) or 0) > 0:
        return 0
    if (getattr(t, "num_seeds", 0) or 0) > 0:
        return 1
    if ((getattr(t, "num_complete", 0) or 0) > 0
            or (getattr(t, "num_incomplete", 0) or 0) > 0
            or (getattr(t, "num_leechs", 0) or 0) > 0):
        return 2
    return 3


def is_stalled(t):
    """一帧是否判为 stalled（**判死**用）。判据（全部满足）：

      · dlspeed == 0 且 num_seeds == 0（两者同时，才算"真没在动"）
      · 不是豁免 state（metaDL/checking 本来就在动）
      · 不是 queuedDL（那是被 cap 挡着，不是没 swarm）

    ★ 为什么 queuedDL 必须排除：把它算成 dead 会形成自我实现的死循环。
    """
    st = getattr(t, "state", "") or ""
    if st in EXEMPT_STATES:
        return False
    if st == "queuedDL":
        return False
    return (getattr(t, "dlspeed", 0) or 0) == 0 and (getattr(t, "num_seeds", 0) or 0) == 0


def is_trickling(t, trickle_bps):
    """一帧是否是"僵尸 swarm"：有 peer 有速度，但速度极低。"""
    st = getattr(t, "state", "") or ""
    if st in EXEMPT_STATES or st == "queuedDL":
        return False
    dl = getattr(t, "dlspeed", 0) or 0
    return 0 < dl < trickle_bps


def update_hysteresis(rec, stalled_now, trickle_now, cfg):
    """施密特迟滞：改 rec 里的 consec_stall / consec_live / consec_trickle / dead。

    ★ 进死难（stall_enter 帧）、复活易（live_exit 帧）—— 两数必须不同。
    返回 (dead, changed)：changed 表示 dead 值本轮翻转过。
    """
    if stalled_now:
        rec["consec_stall"] = rec.get("consec_stall", 0) + 1
        rec["consec_live"] = 0
    else:
        rec["consec_live"] = rec.get("consec_live", 0) + 1
        rec["consec_stall"] = 0

    if trickle_now:
        rec["consec_trickle"] = rec.get("consec_trickle", 0) + 1
    else:
        rec["consec_trickle"] = 0

    old_dead = bool(rec.get("dead", False))
    dead = old_dead
    if rec["consec_stall"] >= cfg.stall_enter:
        dead = True
    elif rec["consec_live"] >= cfg.live_exit:
        dead = False
    rec["dead"] = dead
    rec["trickle"] = rec.get("consec_trickle", 0) >= cfg.trickle_enter
    return dead, (dead != old_dead)


def promise_score(t, cfg, trickle):
    """策略 B 的"带宽加权"部分：越有前景分越高。全部走 log1p（加权而非赢者通吃）。

    ★ 另加一个**硬项**（策略 A 的核心）：**完全没有 swarm 信号**的种子
      （`num_seeds==0 且 num_complete==0 且 num_leechs==0`）扣 `no_swarm_penalty`。
      理由：这种种子**物理上下不动**（实测 397 个里 392 个如此），不该跟那些
      "至少有 tracker 报的 swarm"的种子同分排队 —— 这正是"有源优先"。
      ★ 它是**惩罚**不是**排除**：当活种子不足 K 时，仍要有人填满名额，
        此时惩罚小的（有 swarm 的）先上，惩罚大的（真没源）垫底。
    """
    dl_kb = (getattr(t, "dlspeed", 0) or 0) / 1024.0
    seeds = getattr(t, "num_seeds", 0) or 0
    ncomp = getattr(t, "num_complete", 0) or 0
    ninc = getattr(t, "num_incomplete", 0) or 0
    leech = getattr(t, "num_leechs", 0) or 0
    s = (cfg.w_speed * _log1p(dl_kb)
         + cfg.w_seeds * _log1p(seeds)
         + cfg.w_swarm * _log1p(ncomp / 10.0 + ninc / 10.0)
         + cfg.w_meta * _log1p(leech))
    # ★ "有源优先"硬项：无任何 swarm 信号 ⇒ 重罚
    st = getattr(t, "state", "") or ""
    if st not in EXEMPT_STATES and seeds == 0 and ncomp == 0 and leech == 0:
        s -= cfg.no_swarm_penalty
    if trickle:
        s *= cfg.trickle_penalty
    return s


def fairness_score(rec, t, cfg):
    """策略 B 的"公平轮转"部分：等得越久、剩得越多，越该上位。"""
    credit = rec.get("wait_credit", 0.0)
    size = getattr(t, "size", 0) or 0
    left = getattr(t, "amount_left", 0) or 0
    frac_left = (left / size) if size else 0.0
    return cfg.w_wait * _log1p(credit) + cfg.w_left * frac_left


def desirability(t, rec, cfg, exempt, rotation_boost):
    """复合期望分。exempt=True 给保底（保证 metaDL 能进 top-K 拿到元数据）。"""
    trickle = bool(rec.get("trickle", False))
    s = promise_score(t, cfg, trickle) + fairness_score(rec, t, cfg)
    if exempt:
        s += cfg.w_boost
    s += rotation_boost
    return s


def split_limits(active, cfg):
    """策略 B 的"分管道"：返回 {hash: download_limit}（只对 active 集算）。

    cap_greedy 模式（默认，保守）：只给"最贪"的 GREEDY_K 个套一个 headroom 倍的软帽，
    其余不限（-1）。目的：防止一个种子把连接/带宽吃光而其余饿死。
    budget 模式：按总预算 BUDGET_BPS 按 speed 比例分；B<=0 则全部无限。
    ★ "无限"一律写 -1；绝不写 0。
    """
    out = {}
    if not active:
        return out
    for t in active:
        out[t.hash] = UNLIMITED  # 默认不限

    if cfg.bw_mode == "budget":
        b = cfg.budget_bps
        if b <= 0:
            return out
        # 按当前 dlspeed 比例分（speed 全 0 则均分）
        speeds = [(t.hash, max(0, getattr(t, "dlspeed", 0) or 0)) for t in active]
        tot = sum(s for _, s in speeds)
        n = len(speeds)
        for h, s in speeds:
            share = b * (s / tot) if tot > 0 else b / n
            out[h] = max(cfg.min_per_torrent_bps, int(share))
        # ★ 地板可能让总和超过预算（多个慢种都触地板）—— 若超了，把超过的部分
        #   从"有余量的"按比例回收，避免实际分配的比预算还多。
        total = sum(out.values())
        if total > b:
            over = total - b
            room = {h: out[h] - cfg.min_per_torrent_bps for h in out}
            room_sum = sum(room.values())
            if room_sum > 0:
                for h in out:
                    cut = int(over * room[h] / room_sum)
                    out[h] = max(cfg.min_per_torrent_bps, out[h] - cut)
            # ★ 整数取整可能让总和仍比预算多 1~n（每个 int() 都向下取整，理论上不会超，
            #   但地板 max() 可能把它顶回去）—— 兜底：若仍超，从"余量最大"的逐个减 1。
            guard = 0
            while sum(out.values()) > b and guard < 1000:
                h_max = max(out, key=lambda x: out[x])
                if out[h_max] <= cfg.min_per_torrent_bps:
                    break
                out[h_max] -= 1
                guard += 1
        return out

    # cap_greedy：最贪的 K 个套帽
    greedy = sorted(active, key=lambda t: -(getattr(t, "dlspeed", 0) or 0))[:cfg.greedy_k]
    for t in greedy:
        dl = getattr(t, "dlspeed", 0) or 0
        if dl <= 0:
            continue
        out[t.hash] = max(cfg.min_per_torrent_bps, int(dl * cfg.greedy_headroom))
    return out


def select_active(scored, cfg, tick, state, qb_active):
    """选活动集（策略 A 的准入 + 策略 B 的排序 + thrash 控制）。

    输入 scored：list of (t, score, exempt, dead).
    输入 qb_active：**qB 当前实际**排在队首（会被 cap 选中）的 download 哈希集。
        ★★ 这是本函数**最重要的一处修正**（2026-10-03 实测踩到）：
          一开始用工具**自己记得**的上一轮 active 来算 promote/demote，
          结果 qB 实际只跑 5 个、而工具以为已跑 10 个 ⇒ promote 恒为 0、永不生效。
          判据必须是 **qB 的现场队列顺序**，不是工具的脑内记忆（ERR-META-01 同族）。
    返回 (active_hashes, promote, demote, note)。
    """
    prev_active = set(qb_active)          # ← 用 qB 现场，不用 state["_active"]
    torrents = state.get("torrents", {})

    # 切分为 活/死（死但轮转到期的当活处理）
    alive, dead = [], []
    for item in scored:
        t, score, exempt, d = item
        rec = torrents.get(t.hash, {})
        boost = rec.get("_rotation_boost", 0.0)
        if d and boost <= 0:
            dead.append(item)
        else:
            alive.append(item)

    # ★ 排序键：**(swarm_tier, -score, hash)** —— 分层优先，分数只在层内比。
    #   这样"有源优先"（tier 小）是结构性的，不会被 fairness 翻盘。
    alive.sort(key=lambda x: (swarm_tier(x[0]), -x[1], x[0].hash))
    dead.sort(key=lambda x: (swarm_tier(x[0]),
                             -torrents.get(x[0].hash, {}).get("wait_credit", 0.0),
                             x[0].hash))

    # ★ min-dwell 保护要把这件事做**对**：受保护的种子必须**占住名额**，
    #   而不是"先选完再塞进去、然后被按分截断掉"。
    #   ⇒ 先把"上轮在 active、本轮未变 dead、且驻留未满"的种子**锁定**进 sel，
    #     再从剩下的（活种子优先、死种子填）里挑满剩余名额。
    protected = []
    for item in scored:
        t, score, exempt, d = item
        if t.hash in prev_active and not d:
            # ★ 只保护"还有点动静"的（tier<=2）——tier3（连 swarm 记录都没有）
            #   不配被驻留保护：它占着槽却毫无信号，早点让位给别的。
            if swarm_tier(t) > 2:
                continue
            rec = torrents.get(t.hash, {})
            if tick - rec.get("admitted_tick", 0) < cfg.min_dwell_ticks:
                protected.append(item)
    prot_hashes = {it[0].hash for it in protected}

    rest_alive = [it for it in alive if it[0].hash not in prot_hashes]
    rest_dead = [it for it in dead if it[0].hash not in prot_hashes]

    sel = list(protected[:cfg.cap])
    room = cfg.cap - len(sel)
    if room > 0:
        sel += rest_alive[:room]
        room = cfg.cap - len(sel)
    if room > 0:
        sel += rest_dead[:room]

    # 若保护项 + 高分项超过 cap（保护项自己就超了），按分截断——
    # 但保护项优先：先放保护项，剩余才轮别人。
    sel.sort(key=lambda x: (swarm_tier(x[0]), -x[1], x[0].hash))
    if len(sel) > cfg.cap:
        # 保证 protected 优先占位
        keep, others = [], []
        for it in sel:
            (keep if it[0].hash in prot_hashes else others).append(it)
        sel = (keep[:cfg.cap] + others[:max(0, cfg.cap - len(keep))])[:cfg.cap]
        sel.sort(key=lambda x: (swarm_tier(x[0]), -x[1], x[0].hash))
    new_active = [it[0].hash for it in sel]

    # 计算 promote / demote（相对上轮 active）
    new_set = set(new_active)
    promote = [h for h in new_active if h not in prev_active][:cfg.max_promote]
    demote = [h for h in prev_active if h not in new_set][:cfg.max_promote]

    # 写回 admitted_tick（谁这一轮是新的）
    for h in new_active:
        rec = torrents.setdefault(h, {})
        if h not in prev_active:
            rec["admitted_tick"] = tick

    note = f"活 {len(alive)} / 死 {len(dead)} / 选 {len(new_active)}"
    return new_active, promote, demote, note


# ==================== 状态持久化 ====================

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log(f"⚠️  写状态文件失败: {e}")


def beat():
    try:
        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


# ==================== qB I/O ====================

def qb_connect():
    """连 qB。返回 (client, reason)。"""
    try:
        from qbittorrentapi import Client
    except ImportError as e:
        return None, f"没装 qbittorrentapi（{e}）"
    try:
        qb = Client(host=config.QB_HOST, username=config.QB_USER,
                    password=config.QB_PASS)
        qb.auth_log_in()
        return qb, None
    except Exception as e:                                   # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def check_preconditions(qb):
    """返回 (ok, code, msg)。queueing 关 或 cap<=0 ⇒ 拒绝写（ERR-META-01：失效的检测更危险）。"""
    try:
        p = qb.app_preferences()
    except Exception as e:                                   # noqa: BLE001
        return False, 2, f"取 app_preferences 失败：{type(e).__name__}: {e}"
    if not p.get("queueing_enabled"):
        return False, 2, ("queueing_enabled=false —— top/bottom priority 需要队列开着，"
                          "且下载 cap 依赖它。拒绝运行，绝不静默 no-op。")
    mad = p.get("max_active_downloads")
    if not mad or int(mad) <= 0:
        return False, 2, f"max_active_downloads={mad} —— 队列机制关掉了，用 top/bottom 无意义。"
    return True, 0, f"queueing_enabled=True, max_active_downloads={mad}"


def check_methods(qb):
    """返回 (ok, missing)。★ 缺方法具名报错，不 AttributeError 裸崩（ERR-META-05）。"""
    need = ("torrents_info", "torrents_top_priority", "torrents_bottom_priority",
            "torrents_set_download_limit")
    missing = [m for m in need if not callable(getattr(qb, m, None))]
    return (not missing), missing


def build_plan(qb, cfg, state):
    """读一次 qB，算分、选集。返回 plan dict（不改 qB，不落盘）。"""
    all_ts = list(qb.torrents_info())
    # 候选池：未完成 且 state 在下载相关集合里
    pool = [t for t in all_ts
            if (getattr(t, "progress", 0) or 0) < 0.999
            and (getattr(t, "state", "") or "") in DL_STATES]

    # ★★ qB **现场**会跑的下载集：判据取 qB 的**队列顺序**（priority 数字小的在前）
    #    与"正在跑"的 state，**不用工具自己的记忆**（实测踩过：工具记忆与 qB 现场不符
    #    ⇒ promote 恒 0、永不生效。ERR-META-01 同族）。
    #    口径：state ∈ {downloading,forcedDL} ∪ 队列数字最小的 cap 个。
    #    （qB 的 active 下载 = 队列顺序前 cap 个里"愿意跑"的；这里取并集近似，
    #     偏保守——多算进来的会被 demote，而多算等于没漏掉。）
    # ★ 只看**下载池**的队列数字：做种种子在另一个队列，别混进来。
    pool_hashes = {t.hash for t in pool}
    by_queue = sorted([t for t in pool],
                      key=lambda t: (getattr(t, "priority", 999999) or 999999))
    qb_active = {t.hash for t in pool
                 if (getattr(t, "state", "") or "") in ACTIVE_STATES}
    for t in by_queue[:cfg.cap]:
        qb_active.add(t.hash)

    tick = state.get("tick_count", 0) + 1
    tick_cfg = cfg
    torrents = state.setdefault("torrents", {})

    scored = []
    for t in pool:
        rec = torrents.setdefault(t.hash, {})
        rec["name"] = (getattr(t, "name", "") or "")[:80]
        rec["cat"] = getattr(t, "category", "") or ""
        st = getattr(t, "state", "") or ""
        exempt = st in EXEMPT_STATES
        stalled_now = is_stalled(t)
        trickle_now = is_trickling(t, tick_cfg.trickle_bps)
        if exempt:
            stalled_now = False
        dead, _changed = update_hysteresis(rec, stalled_now, trickle_now, tick_cfg)

        # 轮转 boost：dead 且距上次"活"过了 RETRY_TICKS ⇒ 临时加分，挤进去碰运气一次
        last_live = rec.get("last_live_tick", 0)
        boost = 0.0
        if dead and (tick - last_live) >= tick_cfg.retry_ticks:
            boost = tick_cfg.retry_boost
        if not dead:
            rec["last_live_tick"] = tick
        rec["_rotation_boost"] = boost

        # wait_credit：不在 active 时攒（active 用 qB 现场口径）
        left = getattr(t, "amount_left", 0) or 0
        size = getattr(t, "size", 0) or 0
        frac_left = (left / size) if size else 0.0
        if t.hash in qb_active:
            rec["wait_credit"] = rec.get("wait_credit", 0.0) * tick_cfg.credit_decay
        else:
            rec["wait_credit"] = min(tick_cfg.credit_cap,
                                     rec.get("wait_credit", 0.0)
                                     + tick_cfg.credit_rate * tick_cfg.interval
                                     * (1.0 + frac_left))

        score = desirability(t, rec, tick_cfg, exempt, boost)
        rec["last_score"] = round(score, 3)
        scored.append((t, score, exempt, dead))

    active_hashes, promote, demote, note = select_active(scored, tick_cfg, tick, state, qb_active)

    # 限速计划：只对 active 集
    active_set = set(active_hashes)
    active_ts = [item[0] for item in scored if item[0].hash in active_set]
    limits = split_limits(active_ts, tick_cfg) if tick_cfg.set_limits else {}

    # 记分表（给 --explain 用）
    scores = []
    for (t, score, exempt, dead) in scored:
        scores.append({
            "hash": t.hash[:16], "name": (getattr(t, "name", "") or "")[:44],
            "state": getattr(t, "state", ""), "dlspeed_kb": round((getattr(t, "dlspeed", 0) or 0) / 1024.0, 1),
            "seeds": getattr(t, "num_seeds", 0), "score": round(score, 3),
            "tier": swarm_tier(t),
            "exempt": exempt, "dead": dead,
            "in_active": t.hash in set(active_hashes),
        })
    # ★ 按**真实排序键**（tier, -score）排，与 select_active 一致 ——
    #   否则 --explain 会按纯分数排，显示出一个**与实际选集不同**的顺序（误导）。
    scores.sort(key=lambda d: (d["tier"], -d["score"]))

    return {
        "tick": tick, "pool": len(pool), "active": active_hashes,
        "promote": promote, "demote": demote, "limits": limits,
        "note": note, "scores": scores,
        "active_full": [t.hash for t in active_ts],
    }


def apply_plan(qb, plan, cfg, dry):
    """把 plan 写到 qB。dry=True 只打印。返回 (report, wrote_any)。"""
    report = {"promote": [], "demote": [], "limits": [], "verify": []}
    wrote = False

    promote = plan["promote"]
    demote = plan["demote"]
    limits = plan["limits"]

    # 写上限（总写 <= max_writes）
    budget = cfg.max_writes

    if not dry and promote:
        qb.torrents_top_priority(torrent_hashes=promote)
        report["promote"] = promote
        wrote = True
        budget -= len(promote)
    if not dry and demote and budget > 0:
        demote = demote[:budget]
        qb.torrents_bottom_priority(torrent_hashes=demote)
        report["demote"] = demote
        wrote = True
        budget -= len(demote)
    if not dry and limits and budget > 0:
        # 只写"与当前不同"的
        cur = {t.hash: (t.download_limit or 0) for t in plan["_cur_ts"]}
        to_write = {}
        for h, lim in limits.items():
            want = lim
            have = cur.get(h, 0)
            # qB 回读把无限报成 0/-1；统一判等
            have_norm = 0 if have in (0, -1) else have
            want_norm = 0 if want in (0, -1) else want
            if have_norm != want_norm:
                to_write[h] = want
        if to_write:
            # ★ 集中断言：绝不写 0
            for h, lim in to_write.items():
                assert lim != 0, f"拒绝写 download_limit=0（语义含糊）= {h}"
            for h, lim in to_write.items():
                qb.torrents_set_download_limit(limit=lim, torrent_hashes=[h])
                report["limits"].append((h[:12], lim))
            wrote = True

    return report, wrote


def verify_cap(qb, cap):
    """写后断言：active 数 <= cap（ERR-META-08：跨进程动作必回读）。"""
    ts = list(qb.torrents_info())
    active = [t for t in ts if (getattr(t, "state", "") or "") in ACTIVE_STATES]
    return len(active), len(active) <= cap


# ==================== 各模式 ====================

def run_once(qb, cfg, state, dry, explain, consec_fail):
    ok, code, msg = check_preconditions(qb)
    if not ok:
        log(f"❌ 拒绝运行：{msg}")
        return code, consec_fail
    mok, missing = check_methods(qb)
    if not mok:
        log(f"❌ qbittorrentapi 版本不匹配：缺方法 {missing}")
        return 3, consec_fail

    plan = build_plan(qb, cfg, state)
    # 需要当前 active 的 TorrentInfo 供限速比对
    plan["_cur_ts"] = list(qb.torrents_info())

    log(f"tick={plan['tick']} 候选={plan['pool']} {plan['note']}")
    log(f"  计划提升={len(plan['promote'])} 降级={len(plan['demote'])} "
        f"限速项={len(plan['limits'])}")

    if explain:
        n = explain if isinstance(explain, int) else 40
        log(f"  ── 打分表前 {n} 名 ──")
        for d in plan["scores"][:n]:
            flag = "★ACT" if d["in_active"] else "    "
            extra = " [豁免]" if d["exempt"] else (" [死]" if d["dead"] else "")
            log(f"    {flag} t{d['tier']} {d['score']:7.3f} dl={d['dlspeed_kb']:8.1f}KB/s "
                f"seeds={d['seeds']:3} {d['state']:11}{extra} {d['name']}")

    # ★★ 冷启动保护：`dead` 迟滞需要 `stall_enter` 轮才认，前几轮**没有一条**会被判死
    #    ⇒ 此时"按分选 10 个"会选出 10 个还没判死的（可能全是 0 源）种子，
    #      写下去**比不动更糟**（把有源的挤掉）。所以历史不足时**只采样、不写**。
    #   判据：tick 数 < stall_enter ⇒ 还没攒够判死所需的历史。
    cold = plan["tick"] <= cfg.stall_enter
    if cold and not dry:
        log(f"  ⏳ 冷启动（tick={plan['tick']} <= stall_enter={cfg.stall_enter}）："
            f"只采样不写（判死要有历史，否则会误伤）。")
        dry = True

    # 写后冷却：上轮写过则本轮跳过写
    last_write = state.get("last_write_tick", 0)
    cooldown = cfg.write_cooldown_ticks
    in_cooldown = (plan["tick"] - last_write) <= cooldown and last_write > 0
    if in_cooldown and not dry:
        log(f"  ⏸ 写后冷却中（上轮 tick={last_write}），本轮只读。")

    report, wrote = apply_plan(qb, plan, cfg, dry or in_cooldown)

    if dry:
        log("★ 本命令没有写任何东西（--dry-run / 默认只读）。")
    elif wrote:
        state["last_write_tick"] = plan["tick"]
        log(f"  ✍️ 已写：升 {len(report['promote'])} 降 {len(report['demote'])} "
            f"限速 {len(report['limits'])}")
        n_active, ok_cap = verify_cap(qb, cfg.cap)
        if ok_cap:
            log(f"  ✅ 回读 active={n_active} <= cap={cfg.cap}")
        else:
            log(f"  ❌ 回读 active={n_active} > cap={cfg.cap} —— 违反上限，告警")
    else:
        log("  （无变化，未写）")

    # 落盘：active 集 + tick
    state["_active"] = plan["active"]
    state["tick_count"] = plan["tick"]
    state["generated_at"] = int(time.time())
    # 裁剪：只留本轮见过的 + active 的（防 2294 条无限增长）
    seen = set(t.hash for t in plan["_cur_ts"]) | set(plan["active"])
    state["torrents"] = {h: r for h, r in state.get("torrents", {}).items() if h in seen}
    save_state(state)
    beat()
    return 0, 0


def healthcheck_main():
    """读自己的心跳，容忍 max(interval*3, 180s)。"""
    cfg = Cfg()
    limit = max(cfg.interval * 3, 180)
    try:
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            last = float(f.read().strip())
    except FileNotFoundError:
        print(f"UNHEALTHY: 心跳文件不存在 ({HEARTBEAT_FILE})")
        return 1
    except (ValueError, OSError) as e:
        print(f"UNHEALTHY: 读心跳失败: {e}")
        return 1
    age = time.time() - last
    if age > limit:
        print(f"UNHEALTHY: 心跳已停 {age:.0f}s（上限 {limit}s）")
        return 1
    print(f"OK: 心跳 {age:.0f}s 前")
    return 0


def restore_limits(qb, state, dry):
    """把所有候选的 download_limit 复位为无限（-1）。"""
    ts = list(qb.torrents_info())
    pool = [t for t in ts if (getattr(t, "progress", 0) or 0) < 0.999]
    n = 0
    for t in pool:
        if (t.download_limit or 0) not in (0, -1):
            if not dry:
                qb.torrents_set_download_limit(limit=UNLIMITED, torrent_hashes=[t.hash])
            n += 1
    log(f"{'（dry）会复位' if dry else '已复位'} {n} 个种子的 download_limit → 无限")
    return 0


_shutdown = {"flag": False}


def _on_signal(signum, frame):
    _shutdown["flag"] = True


def main(argv=None):
    ap = argparse.ArgumentParser(description="qB 下载调度器（默认只读）")
    ap.add_argument("--apply", action="store_true", help="真正写 qB（还需 QBTUNE_ENABLE=true）")
    ap.add_argument("--dry-run", action="store_true", help="强制只读（与 --apply 同传时胜出）")
    ap.add_argument("--loop", action="store_true", help="常驻，每 QBTUNE_INTERVAL 秒一轮")
    ap.add_argument("--once", action="store_true", help="跑一轮就退出（默认）")
    ap.add_argument("--explain", nargs="?", const=40, type=int, default=0,
                    help="打印打分表前 N 名（默认 40）")
    ap.add_argument("--healthcheck", action="store_true", help="健康检查（读心跳）")
    ap.add_argument("--restore", action="store_true", help="把所有 download_limit 复位为无限")
    args = ap.parse_args(argv)

    if args.healthcheck:
        return healthcheck_main()

    cfg = Cfg()
    # ★ 只读/写入的判定（2026-10-03 修：sidecar 用 --loop，不带 --apply，
    #   导致 QBTUNE_ENABLE=true 却仍 dry=True、永不写 —— 实测踩到）：
    #   · `--dry-run` 永远胜出（最安全）。
    #   · `--loop`（常驻 sidecar 模式）：**由 QBTUNE_ENABLE 决定** —— true 才写。
    #   · 单轮（--once/默认/--explain）：**要显式 `--apply`** 才写（防手滑）。
    #   · 缺一不可：写了要 `QBTUNE_ENABLE=true` **且**（loop 或 apply）。
    if args.dry_run:
        dry = True
    elif args.loop:
        dry = not cfg.enable          # loop 下由开关决定
    else:
        dry = not args.apply          # 单轮要显式 --apply
    if not cfg.enable:
        if (args.apply or args.loop) and not args.dry_run:
            log("⚠️  QBTUNE_ENABLE=false —— 只读。"
                "要真跑请在 NAS 的 .env 里设 QBTUNE_ENABLE=true。")
        dry = True

    state = load_state()

    if args.restore:
        qb, reason = qb_connect()
        if qb is None:
            log(f"✗ 连不上 qB：{reason}")
            return 2
        if dry:
            log("★ --restore 未加 --apply（或 QBTUNE_ENABLE=false）：只预览。")
        return restore_limits(qb, state, dry)

    if args.loop:
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)
        consec_fail = 0
        log(f"🔁 qbtune 常驻启动：interval={cfg.interval}s cap={cfg.cap} "
            f"enable={cfg.enable} dry={dry}")
        while not _shutdown["flag"]:
            qb, reason = qb_connect()
            if qb is None:
                consec_fail += 1
                log(f"⏭ 连不上 qB（{reason}），本轮跳过（第 {consec_fail} 次）")
                if consec_fail >= cfg.max_consec_conn_fail:
                    log(f"❌ 连续 {consec_fail} 轮连不上 ⇒ 非零退出")
                    return 1
            else:
                consec_fail = 0
                code, consec_fail = run_once(qb, cfg, state, dry, args.explain, consec_fail)
            # 可中断睡眠
            deadline = time.time() + cfg.interval
            while time.time() < deadline and not _shutdown["flag"]:
                time.sleep(min(1.0, max(0.0, deadline - time.time())))
            state = load_state()   # 重读（防外部改）
        log("👋 qbtune 退出。")
        return 0

    # 单轮
    qb, reason = qb_connect()
    if qb is None:
        log(f"⏭ 连不上 qB（{reason}）—— 本机连不上是常态，退出 0。")
        beat()
        return 0
    code, _ = run_once(qb, cfg, state, dry, args.explain, 0)
    return code


if __name__ == "__main__":
    sys.exit(main())
