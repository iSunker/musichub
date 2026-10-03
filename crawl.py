"""
OpenCD（皇后）抓取层 —— ★ **本项目唯一主动访问别人站点的模块**。

═══════════════════════════════════════════════════════════════════════════
★ 本模块现在只做一件事：**按已知的 tid 抓那一个详情页**
═══════════════════════════════════════════════════════════════════════════
      tid（从 VT 库来） → 抓详情页 → 落盘 raw_html/<tid>.html → opencd_ingest 入库

★★ 2026-09-23：**「按种子名称搜索皇后」这条路已被整条删除**（用户决定）。
   删除的是：`search_candidates()` / `match_by_name()` / `run_from_qb()` /
   `qb_torrents()` / `--from-qb` / `--search` / `--max-candidates`。

   **为什么删**（三条都是实测，不是"觉得不好"）：
     ① **贵** —— 一条 2~3 请求（搜索 1 + 验候选 1~5 + 详情页 0~1），
        而 `OPENCD_MAX_PER_DAY` 只有 50（**请求**量纲）。
     ② **会搜错** —— 实测搜「齐秦 - 我拿什么爱你」返回 **2 条**
        （FLAC 分轨 / WAV 整轨），名字几乎相同 ⇒ 取第一条就发错种子。
        根因：搜索是**子串匹配**，而 qB 名字是第一阶段为 Musopia 生成的
        （带 `[FLAC]` / `(2001)`），与皇后标题 `艺人 - 专辑 年份 - 格式 分軌`
        **天然不同** ⇒ 命中率极低而误配风险高。
     ③ **抓了也发不出去** —— 那条路传 `qb_seed_days=None` ⇒ 每行落
        `NOT_SEEDED` ⇒ 卡在发布队列闸门①（`status='SUCCESS'`）。

   **现在的 id 从哪来**：`sync_opencd_tid.py` —— 读**另一个项目**（vertex2）的库，
   按 **`info_hash`**（内容哈希，不可伪造）配对出皇后的 `details.php?id=`。
   实测命中 **430/547**，而按 `torrent_name` 只有 **1/547**。
   ★ 它**零请求**（不 import `requests`、不 import 本模块），是唯一"不抓页面就能拿到 id"的路。

   ★★ **id 和页面内容是两件事**（别混）：VT 只给**页面编号**，不给页面内容。
      而摘要里那些字段（转载规则 / 标签 / 媒介 / 製作組 / 艺术家 / 年份）**只存在于页面上**，
      `db.PENDING_PUBLISH_WHERE` 的闸门③④ 正是因此存在。
      ⇒ 拿到 id 之后**仍然必须抓页面**，那件事由 `topup_opencd_fields.py` 做。

═══════════════════════════════════════════════════════════════════════════
频率控制（★ 用户反复强调；三道闸门见 `opencd_rate.py`）
═══════════════════════════════════════════════════════════════════════════
  · 阶段周期（多久跑一轮）→ `daemon.py` 管，本模块不管
  · 每轮预算 / 每日上限 / 最小间隔 → 全部过 `RateGate`（**每个请求都要过**）
  · 详情页**抓过就跳** —— 这才是真正省请求的机制（不是把间隔调大）

★ 2026-09-19 调整（用户要求）：每请求最小间隔 **2s → 120s**；
  每日上限 **300 → 50**。
✅ 2026-09-22：**跨轮持久计数已接线** —— 主流程构造 `RateGate` 时传 `ledger=_DbLedger()`，
  计数落在 `data.db` 的 `opencd_quota` 表里 ⇒ 「每日 50」从"每轮 50"变回**真的每日 50**。
★ 2026-09-23 量纲订正：删掉名称搜索后，**一条种子 = 1 个请求**（只抓详情页）
  ⇒ `OPENCD_MAX_PER_DAY=50` 现在约等于 **50 个页面/天**，不再是旧的
  "2~3 请求/种子 ⇒ 约 17~25 个/天"。（`opencd_rate.py` 的「已知缺口」① 仍记着
  "请求 vs 种子"这个量纲问题本身 —— 那是账本层面的事，与本条订正不冲突。）

═══════════════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════════════
    python crawl.py --tid 200610           # 主动抓：指定 id
    python crawl.py --url <详情页URL>       # 主动抓：指定 URL
    python crawl.py --tid 200610 --info-hash <40hex>   # 指定 id 并入库

★★ **批量抓由 `topup_opencd_fields.py` 做**，不是本模块 —— 它负责"选哪几条、
   台账去重、把 `fetch_detail` + `opencd_ingest` 按批串起来"。本模块只提供
   **一个出网原语**（`fetch_detail`）。

★★ 关于 `--dry-run`：本模块的 `--tid` / `--url` 路**本就不写库**（除非显式给
   `--info-hash`），`client.dry_run=True` 时会跳过入库并只打日志。
   ★ 历史教训（`ERR-META-01`）：`run_from_qb` 那条已删除的路上，`--dry-run`
   曾**真的把每个待抓详情页都敲一遍** —— docstring 说"不发请求"而实际发了。
   现在剩下的这条路没有"待抓循环"，不存在那个形状。
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime

import requests

import config
import net   # ★ 统一网络出口（强制 IPv4）——新 Session 一律走 net.new_session()
import db
import opencd_ingest
import opencd_log as L
import opencd_parse
import opencd_rate

sys.stdout.reconfigure(encoding="utf-8")

# 落盘目录（★ 解析层永远读它，不重复请求 —— 这是"抓取与解析解耦"的落地）
RAW_DIR = os.getenv("OPENCD_RAW_DIR", os.path.join(config.DATA_DIR, "raw_html"))


# ==================== 配额账本适配器 ====================

class _DbLedger:
    """
    把 `db.ledger_get/ledger_add` 适配成 `RateGate` 要的鸭子类型接口。

    ★ 为什么适配器在这里、不在 `opencd_rate.py`：
      `opencd_rate.py` 刻意保持**零项目内依赖**（只 import stdlib），这样它的
      闸门逻辑能被脱离 `.env`/`DATA_DIR` 单独测（`python opencd_rate.py --selftest`）。
      在那边 `import db` 会把 `config.require_env(...)` 的副作用（缺 .env 直接
      `sys.exit`）带进去 ⇒ 那条自检就再也跑不起来了。
    ★ 为什么不在 `db.py` 里直接做成一个类：`db.py` 是**纯数据层**，不该知道
      `RateGate` 的接口长什么样。放这里 = 依赖方向正确（crawl 同时知道两边）。
    """

    def __init__(self, kind=db.LEDGER_KIND_REQUEST):
        self.kind = kind

    def get(self, day_key):
        return db.ledger_get(day_key, kind=self.kind)

    def add(self, day_key, delta=1):
        return db.ledger_add(day_key, delta=delta, kind=self.kind)


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ==================== 默认频率参数（可用 .env 覆盖） ====================
# ★ 2026-09-19 调整（用户要求）：最小间隔 2s → **120s**；每日上限 300 → **50**。
# ★★ 量纲（2026-09-23 订正）：`OPENCD_MAX_PER_DAY` 是**请求**量纲。
#    删除名称搜索后**一条种子 = 1 个请求**（只抓详情页，抓过的还跳过）
#    ⇒ `OPENCD_MAX_PER_DAY=50` 现在约等于 **50 个页面/天**。
#    ★ 旧的"2~3 请求/种子 ⇒ 约 17~25 个/天"是**名称搜索时代**的比例，已作废。
#    ★ 但"请求 vs 种子"这个**账面**量纲问题本身仍在 —— 见 `opencd_rate.py`
#      模块头「已知缺口」①（想按种子卡要在入库那步记 kind='seed'），
#      **别读成已解决**。
DEFAULT_MAX_PER_ROUND = int(os.getenv("OPENCD_MAX_PER_ROUND", "20"))
DEFAULT_MIN_INTERVAL = float(os.getenv("OPENCD_MIN_INTERVAL", "120"))
DEFAULT_MAX_PER_DAY = int(os.getenv("OPENCD_MAX_PER_DAY", "50"))
# ⚠️ 原来这里还有个 DEFAULT_INTERVAL（OPENCD_INTERVAL，阶段周期）—— 已删。
#   ★ 它**零消费者**：抓取节奏是 `daemon.py` 的事（本模块 :20-21 自己就这么写），
#     而 crawl 还没接进 daemon ⇒ 这个值从来没人读过。
#     留一个"调了没效果"的旋钮，比没有这个旋钮更糟（`opencd_rate.py` 的 reset_round()
#     那条注释是同一个道理）。

_HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    from datetime import datetime
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {L.mask_secrets(msg)}",
          flush=True)


class Client:
    """
    皇后站点的最小客户端。**所有请求都过 RateGate**（`acquire` 是唯一的出口）。
    """

    def __init__(self, cookie=None, gate=None, dry_run=False):
        self.base = (os.getenv("OPENCD_URL", "https://open.cd")).rstrip("/")
        self.cookie = cookie or os.getenv("OPENCD_COOKIE", "")
        # ★ 2026-09-19：**删掉**原来这里的 `gate or RateGate(...)` 兜底。
        #   那个兜底造的 gate **没有 day_key** ⇒ 「每日上限」静默失效
        #   （`roll_day()` 永远不会清零，`_day_key=None` 与任何日期都不等）。
        #   「有个看起来能跑的兜底」比「必须显式传」危险得多 ——
        #   同"密钥不留默认值"的推理（见 `config.py`：「不留看起来能跑的默认值」）。
        #   ★ 零影响已核实：自检 5 处 `crawl.Client(...)` 全都显式传 `gate=`。
        if gate is None:
            raise ValueError(
                "Client 必须显式传 gate=（RateGate 实例）。"
                "不传时闸门没有 day_key，「每日上限」会静默失效 —— "
                "频率控制是本条流水线最要紧的纪律，不允许有绕过它的路径。"
            )
        self.gate = gate
        self.dry_run = dry_run
        self.sess = net.new_session()
        self.sess.headers.update({
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        if self.cookie:
            self.sess.headers["Cookie"] = self.cookie

    def get(self, path, **params):
        """★ **唯一**发请求的地方 —— 必须先过闸门。"""
        url = path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"
        self.gate.acquire()                       # ← 配额/间隔/预算都在这里
        r = self.sess.get(url, params=params or None, timeout=30)
        return r

    # ---------- 登录态 ----------

    def check_login(self):
        r = self.get("/torrents.php")
        if r.status_code != 200:
            return False, f"访问失败 HTTP {r.status_code}"
        body = r.text or ""
        if "takelogin.php" in body or "login.php" in body:
            return False, "Cookie 已失效，请更新 .env 里的 OPENCD_COOKIE"
        return True, ""


# ==================== 落盘（解析层的输入） ====================

def save_raw(tid, html):
    """落盘原始 HTML。★ 这层是**故意的**：解析层永不重复请求，改版时重放即可。"""
    os.makedirs(RAW_DIR, exist_ok=True)
    p = os.path.join(RAW_DIR, f"{tid}.html")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    os.replace(tmp, p)                            # 原子替换（同 daemon.save_state）
    return p


def load_raw(tid):
    p = os.path.join(RAW_DIR, f"{tid}.html")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ==================== infohash 提取 ====================
#
# ★★ 实测（2026-09-19）：详情页的「Hash碼」就是 info_hash（40 hex = 20 字节）
#    ⇒ 可与 qB 的 `.hash` 直接比。
# ★ 用途（2026-09-23 后）：**只用于展示与核对**（`run_manual` 打印页面 Hash碼，
#    方便人工确认"这条 tid 是不是我们要的那个种子"）。
#    ★ 它**不再**用于"按名字搜出的候选逐个验 hash" —— 那条路（`match_by_name`）
#      已删除，因为 id 现在由 `sync_opencd_tid.py` 按 infohash 从 VT 库直接给出。

HASH_RE = re.compile(r"Hash碼</b>\s*([0-9a-fA-F]{40})", re.I)


def extract_infohash(html):
    """从详情页 HTML 里取 infohash（40 hex）。取不到返回 ''。"""
    m = HASH_RE.search(html or "")
    return m.group(1).lower() if m else ""


# ==================== 抓取 ====================

def fetch_detail(client, tid, *, force=False):
    """
    抓一个详情页并落盘。已有落盘且非 force 时**直接返回本地**（★ 省请求的核心）。
    返回 (info, from_cache)。
    """
    html = None if force else load_raw(tid)
    if html is not None:
        return opencd_parse.parse_detail(html, source_file=f"{tid}.html"), True

    r = client.get("/plugin_details.php", **{"id": tid, "hit": 1})
    if r.status_code != 200:
        raise RuntimeError(f"抓详情页 id={tid} 失败，HTTP {r.status_code}")
    html = r.text
    save_raw(tid, html)
    return opencd_parse.parse_detail(html, source_file=f"{tid}.html"), False


def run_manual(client, args):
    """
    主动抓：`--tid` / `--url`。

    ★★ 退出码**故意不接任何 `_exit_code()` 式的纯函数**（2026-09-22 注明，
      免得像疏漏；2026-09-23 删除 `_exit_code` 时这条理由仍成立）：
      本路不由 daemon 驱动，**没有退避语义** —— 退出码只对"人和脚本"有意义。
      约定：0=成功、1=入库失败、2=参数错。
      ★ daemon 驱动的**批量路**是 `topup_opencd_fields.py`，不是本函数。

    ★★ 2026-09-23：`--search` 分支已删除（按种子名称搜索整条路被移除）。
      现在只有"**我已知 tid，去抓那一个页面**"这一种用法。
    """
    tid = args.tid
    if args.url:
        m = re.search(r"id=(\d+)", args.url)
        if not m:
            log(f"❌ 从 URL 里取不到 id：{L.brief(args.url)}")
            return 2
        tid = m.group(1)
    if not tid:
        log("❌ 需要 --tid / --url 之一")
        log("   ★ 批量抓（按 tid 补一批页面）用 topup_opencd_fields.py，"
            "不用本模块")
        return 2

    info, cached = fetch_detail(client, tid, force=args.force)
    log(f"{'📦 本地已有' if cached else '📥 新抓'} id={tid}")
    for k in ("name", "small_descr", "album", "artist", "year", "size",
              "category", "format", "medium", "team", "source"):
        log(f"    {k:<12} = {L.brief(info.get(k))}")
    log(f"    标签         = {info.get('tags')}")
    log(f"    转载规则     = {info['transfer_rule_raw']!r} → {info.rule}")
    log(f"    可自动发布   = {info.may_auto_publish()}")
    log(f"    日志         = {info['log_lines']} 行")
    log(f"    曲目         = {info['tracklist_lines']} 行")

    # ★★ 入库（手工路）
    #   ★★ **必须显式给 `--info-hash` 才写库**：`save_task` 以 `info_hash` 为
    #      upsert 主键，而手工跑时我们**无法核对**页面 Hash碼 与 qB 里那份是不是
    #      同一条种子（本项目实测过「同名不同格式」：齐秦那份 2 个候选名字几乎一样）。
    #      拿一条**没有独立见证**的 hash 去写主键，可能凭空多出一行、甚至覆盖
    #      第一阶段的记录 ⇒ 所以**不给 hash 就只落盘、只打日志，不写库**。
    #   ★ 这是「取不到就不写，绝不编」的具体落地。
    if client.dry_run:
        log("    [dry-run] 不写库")
    elif not args.info_hash:
        log("    ⏹️ 未入库：手工路必须显式传 --info-hash（不能凭空写主键）")
        log("       若确认过它与 qB 里那条是同一个种子，重跑时加上该参数即可")
        log(f"       （页面里写的 Hash碼 = {L.brief(extract_infohash(load_raw(tid) or '') or '取不到')}）")
    else:
        try:
            st, why = opencd_ingest.ingest(
                info, raw_tid=tid, info_hash=args.info_hash,
                torrent_file=args.torrent_file,
                qb_seed_days=None, now=datetime.now(),
                # ★ 不传 `opencd_added_at`（= 留空）。理由：本路是**手工**单条，
                #   手上只有 tid，**没有库里的行**可读发布日期。
                #   ★ 留空的后果是闸门② 拦住它（"空 ⇒ 拦"）—— 那是对的：
                #     发布日期是 `vtid` 阶段的产物，跑一次 `sync_opencd_tid.py --apply`
                #     （零请求）就会补上，然后它自然可发。
                #   ★★ **绝不**在这里现编一个日期 —— 编出来的日期会**放行**它，
                #      而"不知道皇后几号发的"不该被当成"放了很久了"。
            )
            log("    " + opencd_ingest.explain(st, why))
        except Exception as e:
            log(f"    ❌ 入库失败：{L.brief(e)}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="OpenCD（皇后）抓取层")
    ap.add_argument("--tid", help="主动抓：指定详情页 id")
    ap.add_argument("--url", help="主动抓：指定详情页 URL")
    ap.add_argument("--force", action="store_true", help="即使本地已有也重抓")
    ap.add_argument("--info-hash", dest="info_hash",
                    help="入库用（主键）。★ 手工路（--tid/--url）**必须显式给**，"
                         "否则只落盘不入库 —— 见 run_manual 里的理由")
    ap.add_argument("--torrent-file", dest="torrent_file",
                    help="入库用的本地 .torrent 路径（发布必需；缺了会落 "
                         "NO_TORRENT_FILE，**不会**自动生成 —— 生成会换 passkey "
                         "导致 infohash 变）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列会做什么；★ 本模块的 --tid/--url 路本就不写库"
                         "（除非显式给 --info-hash）")
    args = ap.parse_args()

    config.print_runtime_info()
    # ★ 建表（幂等）—— 配额账本表必须在构造 gate 之前就存在，否则 gate 的
    #   `ledger.get()` 会 OperationalError（`__init__` 里吞掉降级为 0，
    #   但那样"每日上限"就悄悄失效了 —— 宁可在最前面显式建一次）。
    db.init_db()
    os.makedirs(RAW_DIR, exist_ok=True)
    log(f"🗂️  OPENCD_RAW_DIR = {RAW_DIR}")

    # ★★ 跨轮持久账本（2026-09-22）。`day_key` 用**本地日期**，与账本同口径
    #    （`db.LEDGER_COLUMNS` 注释里有理由；**别改成 UTC**）。
    day_key = time.strftime("%Y-%m-%d")
    ledger = _DbLedger()
    gate = opencd_rate.RateGate(
        max_per_round=int(os.getenv("OPENCD_MAX_PER_ROUND", DEFAULT_MAX_PER_ROUND)),
        min_interval=float(os.getenv("OPENCD_MIN_INTERVAL", DEFAULT_MIN_INTERVAL)),
        max_per_day=int(os.getenv("OPENCD_MAX_PER_DAY", DEFAULT_MAX_PER_DAY)),
        day_key=day_key,
        log=log,
        ledger=ledger,
    )
    # ★ 显式跨天检查：本进程的 day_key 与账本里的日期不同就按账本重载
    #   （`roll_day` 在 `__init__` 之后调用才有意义 —— 构造时已按当天载入，
    #    这里是为了"进程活着跨过零点"以及"手动重跑时对齐"这两种情况）。
    #   ★ 正常路径（daemon 每轮新进程）下它是个 no-op，但**契约承重**：
    #     删掉它，"跨天"就只剩构造那一刻的语义，再也无法在长跑进程里纠正。
    gate.roll_day(day_key)

    log(f"⏱️  频率：每轮 {gate.max_per_round} 个请求 · 最小间隔 {gate.min_interval:g}s · "
        f"每日上限 {gate.max_per_day}（★ **请求**量纲）")
    # ★ 2026-09-23 订正：旧的"一种子 ≈ 2~3 请求 ⇒ 折合约 17~25 个种子/天"是
    #   **名称搜索时代**的比例。删除名称搜索后**一条 = 1 个请求**（只抓详情页，
    #   抓过的还跳过）⇒ 必须重报，否则运维照旧比例估产能会**低估一倍**
    #   （`ERR-META-04`：改了行为却不改文档）。
    log(f"   ★ 折算：一条种子 = **1 个请求**（只抓详情页）⇒ "
        f"约 {gate.max_per_day} 个页面/天。")
    log(f"   ✅ 跨轮持久已启用：当日已用 {gate.used_day}/{gate.max_per_day}（账本 "
        f"data.db:{db.LEDGER_TABLE} day_key={day_key}）⇒ 这是**真的**每日上限。")
    log(f"   ⚠️  量纲仍是**请求**（不是种子）—— 见 opencd_rate.py 模块头「已知缺口」①。")
    # ★ 种子量纲的账（2026-10-04 加）：与"请求"分开报，来源是 `opencd_ingest.ingest()`
    #   在"真的新入库一条"时记的 `kind='seed'`（**只读展示，不改闸门**）。
    try:
        _seeds = db.ledger_get(day_key, kind=db.LEDGER_KIND_SEED)
        log(f"   📦 今日**种子**入库：{_seeds} 条"
            f"（kind='seed'，与上面的请求账**分开**；ERR-META-05）")
    except Exception as e:                                      # noqa: BLE001
        log(f"   📦 今日种子入库：读账本失败（{type(e).__name__}）")

    client = Client(gate=gate, dry_run=args.dry_run)
    if not client.cookie:
        log("❌ 缺 OPENCD_COOKIE（在 .env 里配）—— 皇后的 Cookie，不是 Musopia 的")
        return 2

    return run_manual(client, args)


if __name__ == "__main__":
    sys.exit(main())
