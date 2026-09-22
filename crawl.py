"""
OpenCD（皇后）抓取层 —— ★ **本项目唯一主动访问别人站点的模块**。

═══════════════════════════════════════════════════════════════════════════
为什么是「qB 反查」而不是「爬列表页」（2026-09-19 定，实测依据）
═══════════════════════════════════════════════════════════════════════════
用户在本机 qB（3020）上**已配好自动下载皇后的新种子** ⇒ qB 就是增量信号源：

    皇后新种子 → qB 自动下载 → 扫 qB 拿 infohash/名字 → 去皇后**匹配**出 tid
        → 抓那一个详情页 → 落盘 raw_html/<tid>.html

★ 为什么不用列表页：**实测列表页不按发帖时间排序**（页1 前几个 id=200611…
  200607 是 8~19 小时前，后几个 id=200709…200705 却是 2 小时前）⇒ 想靠它发现
  新种子就得翻遍所有页。而 qB 里有的就是你要的，且天然是"白名单"。

★ 同时保留**主动抓**接口（用户要求）：`--tid` / `--url` / `--search`。

═══════════════════════════════════════════════════════════════════════════
频率控制（★ 用户反复强调；三道闸门见 `opencd_rate.py`）
═══════════════════════════════════════════════════════════════════════════
  · 阶段周期（多久跑一轮）→ `daemon.py` 管，本模块不管
  · 每轮预算 / 每日上限 / 最小间隔 → 全部过 `RateGate`（**每个请求都要过**）
  · 详情页**抓过就跳** —— 这才是真正省请求的机制（不是把间隔调大）

★ 2026-09-19 调整（用户要求）：每请求最小间隔 **2s → 120s**；
  每日上限 **300 → 50**（目标是「每天 50 个种子」，但闸门是**请求**量纲 —— 见下）。
✅ 2026-09-22：**跨轮持久计数已接线** —— 主流程构造 `RateGate` 时传 `ledger=_DbLedger()`，
  计数落在 `data.db` 的 `opencd_quota` 表里 ⇒ 「每日 50」从"每轮 50"变回**真的每日 50**。
  ★ 剩下的量纲问题（请求 vs 种子）见 `opencd_rate.py` 模块头「已知缺口」①，**仍未解决**。

═══════════════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════════════
    python crawl.py --from-qb              # 主路：扫 qB → 匹配 → 抓新种子
    python crawl.py --tid 200610           # 主动抓：指定 id
    python crawl.py --url <详情页URL>       # 主动抓：指定 URL
    python crawl.py --search "齐秦"         # 主动抓：按名字搜（只列候选，不抓）
    python crawl.py --from-qb --dry-run    # 只列会抓哪些，不发请求（★ 见下）

★★ 关于 `--dry-run` 的**准确**语义（2026-09-22 修）：
    原 docstring 写的是"不发请求"，而**实测不是** —— 待抓循环里的
    `fetch_detail()` 没有任何 dry-run 保护，于是"只列会抓哪些"的演练会
    **真的把每个待抓详情页都敲一遍**（`ERR-META-01` 的形状：说的和做的不一样）。
    现在已修，语义是：
      · **不发**详情页请求（`fetch_detail` 那条路整条跳过）
      · **不发**入库写入（`client.dry_run` 已有保护）
      · ⚠️ **仍会发 1 次 cookie 探活**（`GET /torrents.php`）+ 匹配阶段的搜索/验证请求
        ⇒ 留这两处是**刻意的**：没有它们就分不清"cookie 死了"与"dry-run 在正常跳过"，
          而匹配请求本来就是 dry-run 想让你**看到**的东西（"会抓哪些"要靠匹配得出）。
          一轮的量在预算内（1 次探活），且量纲仍走 `RateGate`。
      ★ 判据见 `tools/opencd-crawl-selftest.py` 判据⑧（dry-run 下详情页命中 **0**、
        而匹配阶段命中 **> 0**）。

★★ 匹配的三态与「0 命中熔断」（2026-09-22 加，★ 真跑一次生产才暴露的缺陷）：
    用户实测（541 个 qB 种子）日志里一串「未匹配到皇后条目（验了 0 个候选）」，
    而 `验了 0 个候选` **同时**意味着三件事，处置完全不同：

    | 情况 | `match_by_name` 返回 | 处置 |
    |---|---|---|
    | 搜索成功、**0 个候选** | `MATCH_ZERO` | **熔断**：皇后上没这条，别再花配额 |
    | 搜索**请求失败**（HTTP/网络） | `MATCH_ERROR` | **不熔断**：下轮要重试 |
    | 搜到候选但 infohash 都不匹配 | `MATCH_OK`（tid=None） | 别再搜（有同名的别的版本） |

    · ★ 熔断**只对 ZERO**。对 ERROR 熔断 = 把"今天网络抖一下"变成"这条种子永远漏抓"，
      而"不漏抓"正是熔断要保护的东西。
    · ★ 根因：`search_candidates()` 原先 `if status != 200: return []`
      —— **把请求失败和真的搜不到混成同一个空列表**（本项目最忌讳的形状）。
      现在失败一律**抛异常**（空列表是合法结果，不能兼职表示失败）。
    · ★★ 熔断**不等于**问题解决 —— 真实命中率低的原因是**qB 名字 ≠ 皇后标题**：
      qB 里是第一阶段为 Musopia 生成的名字（带 `[FLAC]` / `(2001)` / `{EU・…}`），
      而皇后标题是 `艺人 - 专辑 年份 - 格式 分軌/整軌`，搜索是**子串匹配** ⇒
      整串拿去搜几乎必然 0 命中。**提高命中率要改搜索策略**（提关键词），
      那是独立的一步 —— 本次只做"把事实报出来 + 别浪费配额"。
    · ★ 「饿死」也在摘要里可见：遍历顺序固定 + 每轮请求预算有限 ⇒
      靠后的种子永远轮不到。`run_from_qb` 结尾会在预算用尽时显式提示。
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

import requests

import config
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


# ==================== 退出码（★ 抽成纯函数是为了能穷举测）====================
#
# ★★ 为什么必须说真话：daemon 判断阶段成败**只看退出码**（`run_stage` 那段），
#   于是"失败却返回 0"会**静默地废掉指数退避** —— 站点被按常规周期反复敲，
#   而日志上一切正常（`ERRATA.md` 的 `ERR-META-01`：判据失效比不判更危险）。
#   `run_from_qb` 原先**总是返回 0**（连 `store_failed > 0` 也是），这就是那个形状。
#
# ★ 为什么抽成纯函数：真值表要能**两个方向**都断言（该返 1 的真返 1、
#   该返 0 的**不能**返 1）。写成内联的 `return 1` 也能让"该返 1"那条过，
#   必须能把"不该返 1"也测出来，所以参数化。

def _exit_code(stats, *, quota_hit=False):
    """
    本轮 crawl 的退出码。真值表见 `tools/opencd-crawl-selftest.py` 判据⑬。

    | 情况 | 退出码 | 理由 |
    |---|---|---|
    | `store_failed > 0`（入库失败）| **1** | 重试有意义；静默就是"判据失效"那个形状 |
    | `failed > 0`（详情页抓取失败）| **1** | 网络/HTTP 失败，下轮还会 pending |
    | 配额用尽（`quota_hit`）| **0** | ★ **闸门在保护，不是错误** |
    | `search_err > 0` 且**命中 0** | **1** | ★ 见下：搜索**全fail**是故障，不是"没活" |
    | 其余（没活 / 全部 NOT_SEEDED）| **0** | "没事干" ≠ "失败" |

    ★★ `quota_hit` 判 0 是本函数最要紧的一条：配额用尽时返回 1 会让 daemon 记
      "连续失败"并**指数退避** —— 于是次日配额一恢复，daemon 反而在退避期里
      按更长的周期跑，把用户要的"每天 50"变成远低于 50。**把保护当故障**，
      方向正好反了。
    ★ 登录失败 / qB 不可达：调用方**直接 return 1**（不经过本函数）—— 那两条
      在 `run_from_qb` 里是 `check_login()` / `qb_torrents()` 的早退，与 stats 无关。

    ★★ `search_err` 为什么**有条件**地算失败（2026-09-22 加，★ 这个"条件"是关键）：
      · 单条搜索失败 ⇒ **不算**失败：那是"这一条没验成"，下轮自然重试；
        算失败会让 daemon 退避，把偶然一次网络抖动放大成"整轮干得更少"。
      · **全部搜索都失败且一条都没命中** ⇒ **算**失败：那不是抖动，是系统性故障
        （cookie 失效 / 被限流 / 站点改版）。此时若返回 0，daemon 会**按常规周期
        一直空转**，而日志摘要里只有"搜不到 N 条"，**没有任何错误** ——
        正是本项目最忌讳的静默形状（cookie 失效告警就靠这个信号）。
      判据：`search_err > 0 and matched == 0` —— 用"命中 0"兜住"抖动"那一支
      （有命中的话说明搜索链路是通的，那几条失败就只是个别）。

    ★★ 顺序（2026-09-22 自检判据⑯ 抓到过一次顺序 bug）：
      · `store_failed` / `failed`（**真失败**）**先判** —— 它们是"这轮真出了问题"，
        与配额无关，任何情况下都要如实返回 1（否则静默 = "判据失效"那个形状）。
      · `quota_hit` **只**用来豁免它自己引起的那些情况（见下），
        所以它**不能**写在最前面把真失败一起吞掉 ——
        我第一版把它短路在最前，那会让"3 条入库失败 + 随后配额用尽"返 0，
        **把真失败静默掉**，方向正好和这条规则的初衷相反。
      · ★ 教训：**优先级必须在代码里体现**，且"豁免"只该豁免它该豁免的东西。
    """
    # ★ 真失败优先 —— 与配额无关，不许被豁免。
    if stats.get("store_failed", 0) > 0:
        return 1
    if stats.get("failed", 0) > 0:
        return 1
    # ★ 配额用尽 ⇒ 0（**只豁免"搜索失败"这一条**，因为配额用尽时
    #   搜索失败往往是"配额刚好吃在搜索上"的直接后果，不是故障）。
    if quota_hit:
        return 0
    if stats.get("search_err", 0) > 0 and stats.get("matched", 0) == 0:
        return 1
    return 0

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ==================== 默认频率参数（可用 .env 覆盖） ====================
# ★ 2026-09-19 调整（用户要求）：最小间隔 2s → **120s**；每日上限 300 → **50**。
# ★★ 量纲警告：`OPENCD_MAX_PER_DAY` 是**请求**量纲，用户要的是「每天最多 **50 个种子**」
#    （一种子 ≈ 2~3 请求：搜索 1 + 验候选 1~5 + 详情页 0~1）。
#    取 50 即**把请求上限压到种子数之下** —— 结果是偏保守（实际约 17~25 种子/天），
#    而不是超量。★ 「跨轮持久」那半步 2026-09-22 已做完（账本，见 `_DbLedger`），
#    但**量纲仍是请求** —— 想按种子卡要在入库那步记 kind='seed'（见 `opencd_rate.py`
#    模块头「已知缺口」①，**别读成已解决**）。
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
        self.sess = requests.Session()
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


def raw_exists(tid):
    return os.path.exists(os.path.join(RAW_DIR, f"{tid}.html"))


def load_raw(tid):
    p = os.path.join(RAW_DIR, f"{tid}.html")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ==================== 匹配：qB 的种子 → 皇后的 tid ====================
#
# ★★ 实测踩到的两个坑（2026-09-19）：
#   ① **搜索结果页没有 infohash**（0 处 40 位十六进制）⇒ 从名字搜出的候选
#      **必须逐个进详情页验 infohash**。实测危险：搜「齐秦 - 我拿什么爱你」
#      返回 **2 条**（FLAC 分轨 / WAV 整轨，名字几乎相同）⇒ **取第一条会发错种子**。
#   ② 详情页的「Hash碼」就是 info_hash（40 hex = 20 字节）⇒ 可与 qB 的 `.hash` 直接比。

HASH_RE = re.compile(r"Hash碼</b>\s*([0-9a-fA-F]{40})", re.I)


def extract_infohash(html):
    """从详情页 HTML 里取 infohash（40 hex）。取不到返回 ''。"""
    m = HASH_RE.search(html or "")
    return m.group(1).lower() if m else ""


def search_candidates(client, name, limit=5, *, on_error=None):
    """
    按名字搜皇后，返回候选 [ListRow, ...]（最多 limit 条）。
    URL 形态抄自参考实现 `repos/auto_feed_js/auto_feed.user.js:4395`。

    ★★ 2026-09-22 订正：**不再把"请求失败"和"真的搜不到"混成同一个空列表**。
      原先的实现是：
          if r.status_code != 200: return []      # ← 失败也返回 []
          ...
          return rows[:limit]                     # ← 搜不到也返回 []
      两者都返回 `[]` ⇒ 调用方只能报「验了 0 个候选」，
      **分不清是"皇后上没这条"还是"我们被拒了"**（本项目最忌讳的形状：
      把失败当成功/把两种事混成一种，见 `ERRATA.md` 的 `ERR-META-02`）。
      ★ 而这两件事的**处置完全相反**：搜不到 ⇒ 这条种子**别再来搜了**（熔断，
        省配额）；请求失败 ⇒ **下轮还要重试**（熔断会让漏抓永久化）。

    ★ 失败一律用**异常**表达（`RuntimeError`），让调用方**无法**默默忽略：
      · HTTP != 200  ⇒ 抛（带状态码）
      · 网络异常      ⇒ 由 `client.get()` 抛出（`requests` 的异常直接冒上来）
      ★ 不用返回值表达失败，是因为空列表是个**合法**结果（真的没搜到），
        用同一个值表示两种含义就必然重演上面那个 bug。

    `on_error`：可选的诊断回调 `on_error(kind, detail)`，`kind` 取
      `"http"` / `"net"`。★ 加上它是为了让**调用方**能按普通流程记日志+计数，
      而不必"抛了再抓"（异常用于强制注意，回调用于记录，两者不冲突）。
    """
    try:
        r = client.get("/torrents.php", **{
            "incldead": 0, "spstate": 0, "inclbookmarked": 0,
            "search": name, "search_area": 0, "search_mode": 0,
        })
    except Exception as e:                       # 网络层失败（超时/连接被拒/TLS…）
        if on_error:
            on_error("net", L.brief(e))
        raise RuntimeError(f"搜索请求失败（网络）: {L.brief(e)}") from e

    if r.status_code != 200:
        if on_error:
            on_error("http", f"HTTP {r.status_code}")
        raise RuntimeError(f"搜索请求失败（HTTP {r.status_code}）")

    rows = opencd_parse.parse_list(r.text)
    return rows[:limit]


# 匹配结果的三态 —— ★ 与"0 命中熔断"配对使用，别把后两个混起来：
#   MATCH_OK     找到并验过 infohash（tid 非空）
#   MATCH_ZERO   搜索**成功**、但皇后上**没有**候选 ⇒ 这条别再搜了（熔断）
#   MATCH_ERROR  搜索**失败**（HTTP/网络）⇒ 下轮**该重试**，不许熔断
MATCH_OK = "ok"
MATCH_ZERO = "zero"
MATCH_ERROR = "error"


def match_by_name(client, qb_name, want_hash, *, max_candidates=5, on_error=None):
    """
    用名字搜候选、逐个进详情页验 infohash，直到命中。
    返回 `(tid, rows_checked, status)`：

      · 命中          ⇒ `(tid, n, MATCH_OK)`
      · 搜到但都没命中 ⇒ `(None, n, MATCH_OK)`  ★ 注意：状态是 OK（搜索本身成功）
      · 搜索返回 0 候选 ⇒ `(None, 0, MATCH_ZERO)` ← 可熔断
      · 搜索请求失败    ⇒ `(None, 0, MATCH_ERROR)` ← **不可**熔断，要重试

    ★ **绝不取第一条** —— 同名不同格式的种子很常见（实测）。
    ★★ 三态是 2026-09-22 加的：此前只有 `(tid, checked)`，调用方从
      `checked == 0` 推断"没搜到"，但 `checked == 0` 同时意味着
      「0 候选」和「第一个候选的详情页抓失败」和「搜索失败」——
      三种情况的处置完全不同（熔断 / 重试 / 重试）。见 `search_candidates` 的注释。
    """
    try:
        cands = search_candidates(client, qb_name, limit=max_candidates,
                                  on_error=on_error)
    except RuntimeError:
        return None, 0, MATCH_ERROR

    checked = 0
    for row in cands:
        tid = row["torrent_id"]
        # ★ 抓过就跳：本地已有该 tid 的落盘 ⇒ 直接读，省一个请求
        html = load_raw(tid)
        if html is None:
            if client.dry_run:
                log(f"    [dry-run] 会抓详情页 id={tid} 验 infohash")
                continue
            r = client.get("/plugin_details.php", **{"id": tid, "hit": 1})
            if r.status_code != 200:
                continue
            html = r.text
            save_raw(tid, html)
        checked += 1
        got = extract_infohash(html)
        if got and got == want_hash.lower():
            return tid, checked, MATCH_OK

    # 走到这里：搜索成功（没抛异常），但没命中。
    # ★ 区分「0 个候选」与「验了 n 个都没中」—— 前者是"皇后上没这条"，
    #   后者是"有同名的但不是我们的 infohash"，处置都是"这条别再搜"，
    #   但**日志和计数要分得开**（前者往往是 qB 名字格式不对，后者是真有同名版）。
    return None, checked, (MATCH_ZERO if not cands else MATCH_OK)


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


# ==================== qB 侧 ====================

def qb_torrents(category=None):
    """从 qB 拉种子列表。★ 只读，不发任何对皇后的请求。"""
    from qbittorrentapi import Client as QB
    qb = QB(host=config.QB_HOST, username=config.QB_USER, password=config.QB_PASS)
    qb.auth_log_in()
    return qb.torrents_info(category=category or config.OPENCD_CATEGORY) \
        if category or config.OPENCD_CATEGORY else qb.torrents_info()


# ==================== 主流程 ====================

def run_from_qb(client, args):
    """
    主路：扫 qB → 匹配皇后 → 抓新种子的详情页 → **入库**（本步不发 Musopia）。

    ★ 落库走 `opencd_ingest`（**纯本地、零请求**）—— 抓取与入库是两件事：
      本函数负责出网，`opencd_ingest` 负责把解析结果写进 `tasks`。
    """
    log("=" * 62)
    log("📡 从 qB 反查皇后（主路）")
    log("=" * 62)

    ok, why = client.check_login()
    if not ok:
        log(f"❌ {why}")
        return 1
    log("✅ 皇后 Cookie 有效")

    try:
        torrents = qb_torrents()
    except Exception as e:
        log(f"❌ 连不上 qBittorrent（{L.brief(e)}）—— 本机通常如此："
            f"QB_HOST 指向 NAS 的 127.0.0.1:3020，本机不可达")
        return 1
    log(f"📥 qB 里 {len(torrents)} 个种子（分类 {config.OPENCD_CATEGORY}）")

    # ★ 已抓过的 tid 集合（从落盘目录推出来，不新开表）
    known = {f[:-5] for f in os.listdir(RAW_DIR) if f.endswith(".html")} \
        if os.path.isdir(RAW_DIR) else set()
    log(f"🗂️  本地已落盘 {len(known)} 个详情页（抓过就跳）")

    stats = {"matched": 0, "fetched": 0, "cached": 0, "denied": 0, "nomatch": 0,
             "failed": 0, "stored": 0, "not_seeded": 0, "no_log": 0,
             "no_file": 0, "store_failed": 0,
             # ★ 2026-09-22 新增：把"没命中"拆成两类（此前它们都是 nomatch）
             "zero_cand": 0,      # 搜索成功但 0 候选 ⇒ 皇后上没这条 ⇒ 熔断
             "search_err": 0}     # 搜索请求失败 ⇒ 下轮要重试 ⇒ **不**熔断
    pending = []
    # ★ 配额闸门提前收工（匹配阶段 或 抓取阶段）—— 与"没活了"必须分开计。
    #   不加这个的话，摘要里"本轮请求 20"与"待抓 0"看起来像"没事干"，
    #   而真相是"被自己的每日上限拦住了"（运维上完全不同的两件事）。
    quota_stopped = False

    for t in torrents:
        h = (t.hash or "").lower()
        name = t.name or ""
        try:
            tid, checked, mstat = match_by_name(
                client, name, h, max_candidates=args.max_candidates)
        except opencd_rate.DailyQuotaExceeded as e:
            log(f"🛑 {e} —— 本轮提前收工（这是保护，不是错误）")
            quota_stopped = True
            break

        if mstat == MATCH_ERROR:
            # ★★ 搜索**请求失败** —— 这条**不熔断**（下轮还会重试）。
            #   熔断它会把"今天网络抖了一下"变成"这条种子永远漏掉"。
            stats["search_err"] += 1
            log(f"  ❌ 搜索失败（下轮会重试，不熔断）：{L.brief(name)}")
            continue
        if not tid:
            if mstat == MATCH_ZERO:
                # ★★ 搜索**成功但 0 候选** —— 这是"皇后上没有这条"。
                #   熔断：这条不再消耗后续请求。理由见下面那段注释。
                stats["zero_cand"] += 1
                log(f"  ⚠️  皇后上搜不到（0 个候选，本条熔断）：{L.brief(name)}")
            else:
                # 搜到了候选，但逐个验过 infohash 都不匹配（可能有同名的别的版本）
                stats["nomatch"] += 1
                log(f"  ⚠️  验了 {checked} 个候选，infohash 都不匹配：{L.brief(name)}")
            continue
        stats["matched"] += 1

        if tid in known:
            stats["cached"] += 1
            continue
        # ★ 带上整个 torrent 对象：入库要用 `hash`（upsert 主键）与
        #   `torrent_file_path`（发布必需的 .torrent）。放这里比事后重查省事。
        pending.append((tid, t))

    log(f"🔍 匹配完成：命中 {stats['matched']} · 同名不匹配 {stats['nomatch']} · "
        f"搜不到 {stats['zero_cand']} · 搜索失败 {stats['search_err']} · "
        f"已抓过 {stats['cached']} · 待抓 {len(pending)}")
    # ★★ 「饿死」可见化（2026-09-22）：本轮请求预算被 __前 N 条__ 吃光时，
    #   后面的种子**永远轮不到**（遍历顺序固定、无游标持久化）。
    #   不报出来的话，表现是"跑了几天，raw_html 一直是 0"而**没有任何错误**
    #   （用户 2026-09-22 实测：541 条、每轮 20 请求 ⇒ 第 21 条起重来没被搜过）。
    #   ★ 这条日志是**诊断**，不是修复 —— 真修复要么改搜索策略（提高命中率、
    #     降低每条消耗），要么引入游标。先把事实报出来，别再静默。
    if len(pending) == 0 and client.gate.budget_left_round() == 0:
        log(f"⚠️  本轮请求预算（{client.gate.max_per_round}）已用尽，"
            f"qB 里还有种子**本轮没轮到**（遍历顺序固定 ⇒ 靠后的会被饿死）。")

    # 抓取待办
    for tid, t in pending:
        name = t.name or ""
        # ★★ dry-run 保护（2026-09-22 修）：原先这里**没有任何保护** ⇒
        #    `--dry-run` 会把每个待抓详情页**真的抓一遍**，与 docstring 承诺的
        #    "不发请求"矛盾（`ERR-META-01`：把失败/副作用当成功）。
        #    ★ 写法照抄 `match_by_name()` 里那处已有分支，保持两处一致：
        #      只打日志 + `continue`，不进 `fetch_detail()` 也就不会 `client.get()`。
        if client.dry_run:
            log(f"    [dry-run] 会抓详情页 id={tid}（{L.brief(name, 60)}）")
            continue
        try:
            info, cached = fetch_detail(client, tid)
        except opencd_rate.DailyQuotaExceeded as e:
            log(f"🛑 {e} —— 本轮提前收工")
            quota_stopped = True
            break
        except Exception as e:
            stats["failed"] += 1
            log(f"  ❌ id={tid} 抓取失败：{L.brief(e)}")
            continue
        stats["fetched" if not cached else "cached"] += 1

        if info.rule == opencd_parse.RULE_DENY:
            stats["denied"] += 1
            log(f"  🚫 id={tid} 禁止轉載 ⇒ DENY（永不发布）：{L.brief(info['name'])}")
        else:
            log(f"  ✅ id={tid} {info.rule}：{L.brief(info['name'])}")

        # ★★ 入库（纯本地、零请求）—— 这一步此前**根本不存在**，
        #    抓到的解析结果曾被直接丢掉（见 `opencd_ingest` 模块头）。
        #    ★ `qb_seed_days` 本步传 `None` ⇒ 闸门①判「不满足」⇒ 落 `NOT_SEEDED`
        #      不进发布队列。这是**刻意的**：查 qB 做种时长是独立的一步，
        #      而闸门①的纪律是「宁可晚抓，不可早发」。
        #      ⇒ 在真接上 `seeding_time` 之前，**不会有皇后行进入发布队列**。
        #    ★ 入库失败**不改变抓取退出码**（两件事），但要显式打日志 + 计数。
        if client.dry_run:
            log(f"     [dry-run] 会入库 id={tid}")
        else:
            try:
                st, why = opencd_ingest.ingest(
                    info, raw_tid=tid, info_hash=(t.hash or "").lower(),
                    torrent_file=getattr(t, "torrent_file_path", None),
                    qb_seed_days=None, now=datetime.now(),
                )
                log("     " + opencd_ingest.explain(st, why))
                if st == opencd_ingest.STATUS_OK:
                    stats["stored"] += 1
                elif st == opencd_ingest.STATUS_NOT_SEEDED:
                    stats["not_seeded"] += 1
                elif st == opencd_ingest.STATUS_NO_LOG:
                    stats["no_log"] += 1
                elif st == opencd_ingest.STATUS_NO_TORRENT_FILE:
                    stats["no_file"] += 1
            except Exception as e:
                stats["store_failed"] += 1
                log(f"     ❌ 入库失败（**不影响抓取结果**）：{L.brief(e)}")

    L.summary(log, "crawl",
              命中=stats["matched"], 待抓=len(pending), 已抓=stats["fetched"],
              禁转=stats["denied"],
              # ★ 2026-09-22：把原来笼统的「未命中」拆开 —— 它们的处置不同
              #   （搜不到=熔断、搜索失败=重试），混成一个数就没法运维。
              同名不匹配=stats["nomatch"], 搜不到=stats["zero_cand"],
              搜索失败=stats["search_err"], 失败=stats["failed"],
              入库=stats["stored"], 未做种=stats["not_seeded"],
              无log=stats["no_log"], 缺种子文件=stats["no_file"],
              入库失败=stats["store_failed"],
              配额停=("是" if quota_stopped else "否"),
              本轮请求=client.gate.used_round, 当日请求=client.gate.used_day)
    code = _exit_code(stats, quota_hit=quota_stopped)
    if code:
        log(f"⚠️  本轮有失败（抓取失败 {stats['failed']} · 入库失败 "
            f"{stats['store_failed']}）⇒ 退出码 {code}，daemon 会按连续失败退避重试")
    elif quota_stopped:
        log("ℹ️  本轮被配额闸门提前收工 ⇒ 退出码 0（**保护不是错误**，"
            "记成失败会让 daemon 退避、反而跑得更少）")
    # ★ 搜索失败单独提示，但它**不进退出码**（见 `_exit_code` 的注释：
    #   它是"这一条没验成"，不是"本轮干不成活"；而且熔断逻辑已保证
    #   不会因此反复消耗 —— 下轮自然会重试）。
    if stats["search_err"]:
        log(f"ℹ️  本轮有 {stats['search_err']} 条搜索请求失败（已标记重试、未熔断）"
            f"⇒ **不影响退出码**；若持续出现，多半是 cookie 或站点限流。")
    return code


def run_manual(client, args):
    """
    主动抓：--tid / --url / --search。

    ★★ 退出码**故意不接 `_exit_code()`**（2026-09-22 注明，免得像疏漏）：
      本路不由 daemon 驱动，**没有退避语义** —— 退出码只对"人和脚本"有意义。
      改成返回 `_exit_code()` 会让手工跑一次失败反而打乱习惯（原本 0=成功、
      1=入库失败、2=参数错），而收益是零（没人靠它退避）。
      daemon 只看 `--from-qb` 那条路的退出码。
    """
    if args.search is not None:
        # ★ `search_candidates` 现在失败会**抛**（不再返回空列表当"搜不到"），
        #   手工路要把它变成人话 + 一个能区分的退出码，否则 traceback 直接糊脸。
        try:
            rows = search_candidates(client, args.search, limit=args.max_candidates)
        except RuntimeError as e:
            log(f"❌ 搜索失败：{L.brief(e)}")
            log("   （★ 这与「搜到 0 个候选」是两件事：那是「皇后上没这条」，"
                "这是「请求没成功」）")
            return 1
        log(f"🔍 搜「{args.search}」→ {len(rows)} 个候选（**只列候选，不抓**）")
        if not rows:
            log("   ⚠️ 0 个候选：皇后上搜不到这条。★ 最常见的原因是"
                "**qB 的种子名格式与皇后标题不同**（qB 名字带 `[FLAC]`/`(年份)`"
                " 这类后缀，而搜索是子串匹配）—— 换个更短的关键词再试。")
        L.detail(log, rows, lambda r: f"    id={r['torrent_id']} {L.brief(r['title'], 70)}")
        log("   ★ 候选**不能取第一条**：同名不同格式很常见，必须验 infohash")
        return 0

    tid = args.tid
    if args.url:
        m = re.search(r"id=(\d+)", args.url)
        if not m:
            log(f"❌ 从 URL 里取不到 id：{L.brief(args.url)}")
            return 2
        tid = m.group(1)
    if not tid:
        log("❌ 需要 --from-qb / --tid / --url / --search 之一")
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
            )
            log("    " + opencd_ingest.explain(st, why))
        except Exception as e:
            log(f"    ❌ 入库失败：{L.brief(e)}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="OpenCD（皇后）抓取层")
    ap.add_argument("--from-qb", action="store_true", help="主路：扫 qB → 匹配 → 抓")
    ap.add_argument("--tid", help="主动抓：指定详情页 id")
    ap.add_argument("--url", help="主动抓：指定详情页 URL")
    ap.add_argument("--search", help="主动抓：按名字搜（只列候选）")
    ap.add_argument("--force", action="store_true", help="即使本地已有也重抓")
    ap.add_argument("--info-hash", dest="info_hash",
                    help="入库用（主键）。★ 手工路（--tid/--url）**必须显式给**，"
                         "否则只落盘不入库 —— 见 run_manual 里的理由")
    ap.add_argument("--torrent-file", dest="torrent_file",
                    help="入库用的本地 .torrent 路径（发布必需；缺了会落 "
                         "NO_TORRENT_FILE，**不会**自动生成 —— 生成会换 passkey "
                         "导致 infohash 变）")
    ap.add_argument("--dry-run", action="store_true", help="只列会做什么，不发请求")
    ap.add_argument("--max-candidates", type=int, default=5, help="每个种子最多验几个候选")
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
    # ★ 显式打出「用户要的是 50 个种子」与其真实的保守含义 —— 免得日志里
    #   `每日上限 50` 被读成「每天 50 个种子已经做到了」（那是量纲混淆）。
    log(f"   ★ 目标：每天最多 50 个**种子**（一种子 ≈ 2~3 请求）⇒ 折合约 "
        f"{gate.max_per_day // 3}~{gate.max_per_day // 2} 个种子/天。")
    # ★ 这两行原先写的是"每轮归 0 / 每日上限退化成每轮上限"—— 现在**已经是假的**，
    #   留着会让运维误判（`ERR-META-04`：改了行为却不改文档）。改成报账本的真实状态。
    log(f"   ✅ 跨轮持久已启用：当日已用 {gate.used_day}/{gate.max_per_day}（账本 "
        f"data.db:{db.LEDGER_TABLE} day_key={day_key}）⇒ 这是**真的**每日上限。")
    log(f"   ⚠️  量纲仍是**请求**（不是种子）—— 见 opencd_rate.py 模块头「已知缺口」①。")

    client = Client(gate=gate, dry_run=args.dry_run)
    if not client.cookie:
        log("❌ 缺 OPENCD_COOKIE（在 .env 里配）—— 皇后的 Cookie，不是 Musopia 的")
        return 2

    if args.from_qb:
        return run_from_qb(client, args)
    return run_manual(client, args)


if __name__ == "__main__":
    sys.exit(main())
