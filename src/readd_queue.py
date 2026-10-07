"""延迟回加 qB 的**待办队列** —— publish 写、daemon 读，**一份实现两边共用**。

═══════════════════════════════════════════════════════════════════════
它解决什么问题
═══════════════════════════════════════════════════════════════════════
用户 2026-10-03 要求：**种子发布成功后，等 N 分钟再往 qB 里加**，好让
「别人 RSS 到这条种子的时差」都过去、尽量多的人**同时**开始下载
（用户原话：「15 分钟能确保最多人 rss 到」；15 是暂定值，可调）。

所以把「发布」与「回加 qB（=开始做种）」这两件事**在时间上拆开**：
  · `publish.py` 发布成功后，**不立即** `torrents_add`，而是把这条
    「待回加」记下来（含 `.torrent` 字节、save_path、category、到期时刻）。
  · `daemon.py` 的主循环每 `SCHED_TICK`（默认 30s）扫一次，**到点了**才
    `torrents_add` —— qB 收到后**自己**校验、自己做种（不需要再调一次）。

═══════════════════════════════════════════════════════════════════════
★ 为什么单独一个模块（而不是 publish 和 daemon 各写一份）
═══════════════════════════════════════════════════════════════════════
写队列的（publish）与读队列的（daemon）**必须对文件格式达成一致**。
本项目反复吃过「同一事实两份实现」的亏（见 ERRATA：`mesh_mapped` 下移、
`_published_where` 只能有一份）。⇒ 写/读**同源**，就是本模块存在的全部理由。
★ 它**不 import** publish / daemon（避免成环）—— 两边都 import 它。

═══════════════════════════════════════════════════════════════════════
★★ 落盘位置：`/data`（= DATA_DIR），不是内存
═══════════════════════════════════════════════════════════════════════
容器可能在这 15 分钟窗口里被重启（本项目 2026-10-03 当天就重建过一次镜像）。
⇒ 待办**必须**在盘上（`/data` 是 bind mount，重建容器不丢）。
写用 `.tmp` + `os.replace` 原子替换（照抄 `daemon.py::save_state`），
断电不留半个 JSON。

═══════════════════════════════════════════════════════════════════════
★★★ 存的是什么：`.torrent` **字节**（base64）
═══════════════════════════════════════════════════════════════════════
到点回加时，**不**回站点重新下载种子 —— 而是发布当场就把 `.torrent` 字节
存进队列（base64 进 JSON）。理由：15 分钟后**站点不一定还可达**、Cookie
不一定还有效；而且「发布后站点那份 .torrent」是**不可变**的，存下来即自包含。
代价：每条几 KB ~ 几十 KB，队列短（发布是限速的，同时不会积很多），可接受。

★★ 已知干扰（用户 2026-10-03 说「先忽略，记录一下」）：
  这 15 分钟里，NAS 上**别的容器**（reseed/iyuu 那套辅种工具）若看到站点
  有新种、自动辅种，可能会**抢先把它加进 qB** ⇒ 延迟就失效了。
  本项目**不拦**、**不检测**这个（用户的决定）；此处如实记录，供将来排查
  「延迟没生效」时第一顺位怀疑。
"""

import base64
import json
import os
import time

import config

# ★ 队列文件与 `data.db`/`daemon_state.json` 同目录（= `/data`，bind mount）
QUEUE_FILE = os.path.join(config.DATA_DIR, "readd_queue.json")

# 队列格式版本 —— 将来改结构时，读到旧版本直接**放弃**而不是猜（ERR-META-01）
QUEUE_VERSION = 1


def _now():
    return time.time()


def load_queue():
    """读待办。**任何**异常都返回空队列（不让坏文件带走 daemon 主循环）。

    ★ 为什么吞异常：这是 daemon 主循环每 30s 会读的东西。一个半截 JSON
      若抛出去，会打断整个调度（所有阶段都停）。⇒ 坏文件 = 空队列 + 打日志，
      不 = 崩。
    """
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"version": QUEUE_VERSION, "items": []}
    except Exception as e:                                      # noqa: BLE001
        print(f"⚠️  [readd_queue] 读队列失败，按空队列处理: {type(e).__name__}: {e}")
        return {"version": QUEUE_VERSION, "items": []}

    if not isinstance(data, dict) or data.get("version") != QUEUE_VERSION:
        print(f"⚠️  [readd_queue] 队列版本不认识"
              f"（读到 {data.get('version') if isinstance(data, dict) else type(data).__name__}，"
              f"期望 {QUEUE_VERSION}），按空队列处理（不猜旧格式）。")
        return {"version": QUEUE_VERSION, "items": []}
    data.setdefault("items", [])
    return data


def save_queue(q):
    """原子写队列（`.tmp` + `os.replace`，照抄 `daemon.py::save_state`）。"""
    try:
        tmp = QUEUE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(q, f, indent=2)
        os.replace(tmp, QUEUE_FILE)
        return True
    except OSError as e:
        print(f"⚠️  [readd_queue] 写队列失败: {e}")
        return False


def enqueue(torrent_bytes, save_path, category, delay_min, note="",
            src_path="", album=""):
    """把一条「待回加」记进队列。**发布成功后调用**（此时种子已上站、不可逆）。

    :param torrent_bytes: `.torrent` 的**原始字节**（站点那份，带正确 announce）。
    :param save_path:     回加时的 `save_path`（沿用原种的做种路径）。
    :param category:      回加时的分类（乌托邦 = `PUBLISH_CATEGORY`；
                          蟹黄堡 = `CRABPT_PUBLISH_CATEGORY`）。
    :param delay_min:     延迟多少分钟（<=0 时**不入队**，调用方应直接 add）。
    :param note:          人看的备注（哪条种子、哪个站），进日志用。
    :param src_path:      ★ #136：成品 HDD 源（`/volume1/video/music/<专辑>`）。
                          回加前用它**逐字节核对**发种区数据是否就位（见 daemon drain）。
    :param album:         ★ #136：专辑名（核对发种区时拼落点用）。

    :returns: 入队成功返回 True。
    """
    if delay_min <= 0:
        # ★ 调用方**不该**在延迟=0 时走到这里；但真走到了也只说不入队（不静默）
        print("⚠️  [readd_queue] delay_min<=0，不排队（调用方应直接 torrents_add）")
        return False

    b64 = base64.b64encode(torrent_bytes).decode("ascii")

    # ★ infohash：从字节里读（与 publish 的复核同一来源），作为**幂等键**
    #   —— 同一条种子重复入队时用于去重（见 daemon 侧 drain 的说明）。
    try:
        import io
        import torf
        infohash = torf.Torrent.read_stream(io.BytesIO(torrent_bytes)).infohash
    except Exception as e:                                      # noqa: BLE001
        print(f"⚠️  [readd_queue] 读 infohash 失败（仍入队，但去重能力降级）: "
              f"{type(e).__name__}: {e}")
        infohash = ""

    item = {
        "infohash": infohash,
        "due": _now() + delay_min * 60.0,
        "enqueued_at": _now(),
        "save_path": save_path,
        "category": category,
        "note": note,
        "torrent_b64": b64,
        # ★ #136（2026-10-07）：成品 HDD 源 + 专辑名 —— 回加前用它核对发种区数据就位。
        #   ★ 为什么（`20639` 实测）：延迟回加**另一条异步路径**，原先直接 `torrents_add`，
        #     不核对发种区 ⇒ 数据缺失时照样把种推上 3040 ⇒ 变下载（`stalledDL prog=0`）。
        #   ★ 加字段是**兼容变更**，不 bump 版本；老条目没有这俩键 ⇒ 回加处退化为
        #     "目录存在且非空"（同 handoff 的兜底），见 daemon `_drain_pending_readds`。
        "src_path": src_path,
        "album": album,
        # ★ 试了几次：到点 add 失败时 +1（见 daemon drain）。超过上限就放弃并报警。
        "attempts": 0,
        # ★★ 2026-10-04（#24）：回加失败后**退避重试**用的字段。
        #   `next_retry_at` = 下次可以再试的 epoch；**0/缺失 = 立即可试**
        #   ⇒ **老条目天然兼容**（盘上已有的条目没有这个键，一律当 0 读，见 `pop_due`）。
        #   ★ 为什么加它：原先失败的条目被 daemon 线程**每 30s** 重试一次，一条**永久性**
        #     失败（如 qB 对已存在种子的 `Conflict409Error`）会一路刷到 `READD_GIVE_UP`
        #     （≈2 天）⇒ 日志被这条刷屏、还白敲 qB（2026-10-04 现场：某条 `attempts=39`）。
        #     ⇒ 失败后写 `next_retry_at`，`pop_due` 只在**到期**时才把它取出。
        #   ★ 不 bump `QUEUE_VERSION`：**加字段是兼容变更**；bump 会让 `load_queue`
        #     把整队**丢弃**（见上面 81 行），那才是灾难。
        "next_retry_at": 0.0,
        # ★ 首次失败的时刻（epoch），只用于日志/告警里算"卡了多久"。None = 从没失败过。
        "first_failed_at": None,
    }

    q = load_queue()
    # ★ 幂等：同 infohash 已在队列里 ⇒ 不重复加（覆盖旧的到期时刻会**提前**，
    #   那违背"延迟"本意；保留先到的那条）。
    if infohash:
        for it in q["items"]:
            if it.get("infohash") == infohash:
                print(f"    ⏳ 已在延迟队列中，不重复入队: {note}")
                return True
    q["items"].append(item)
    ok = save_queue(q)
    if ok:
        due_s = int(delay_min * 60)
        print(f"    ⏳ 已排入延迟队列：{delay_min} 分钟后回加 qB"
              f"（{note}；用途: {save_path}；cat: {category}）")
    return ok


def pop_due(now=None):
    """取出**到点**的条目（返回 (due_items, remaining_items)，**不改盘**）。

    ★ 为什么拆成 pop_due + ack（两步）而不是"取出来就删"：
      取出后要去 qB 做 I/O，可能失败。若"取出即删"，一次网络抖动就**丢**了
      这条待办（种子已上站、却永远不回加 ⇒ 静默不做种）。
      ⇒ 先取出、I/O 成功后再 `ack` 删；失败则原样留在盘上，下轮重试。

    ★★ 2026-10-04（#24）：「到点」现在是**两个都到点** ——
      `due <= now` **且** `next_retry_at <= now`。
      · `next_retry_at` = 上次失败后设的退避截止（见 `enqueue` 的字段说明与
        `daemon._drain_pending_readds` 的失败支）。
      · ★ 用 `it.get("next_retry_at") or 0` 兜底 ⇒ **缺这个键的老条目当 0 = 立即可试**
        （向后兼容；否则盘上已有的条目会被永远挡住）。
      ⇒ 首次回加（从没失败过）不受影响：`next_retry_at` 是 0。
    """
    if now is None:
        now = _now()
    q = load_queue()
    due, rest = [], []
    for it in q["items"]:
        ready = (it.get("due", 0) <= now
                 and (it.get("next_retry_at") or 0) <= now)
        (due if ready else rest).append(it)
    return due, rest


def ack(done_infohashes, remaining_items):
    """把成功回加的条目**移出**队列，写回剩余的。"""
    done = set(done_infohashes)
    keep = [it for it in remaining_items if it.get("infohash") not in done]
    save_queue({"version": QUEUE_VERSION, "items": keep})
