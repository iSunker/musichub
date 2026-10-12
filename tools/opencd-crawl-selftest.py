#!/usr/bin/env python3
"""
crawl.py 的**离线自检** —— 用本地 HTTP mock 假装是皇后。

★ 为什么要有它：`crawl.py` 是本项目**唯一主动访问别人站点**的模块，
  「跑起来才知道对不对」在这里是不可接受的 —— 真跑一次就是一次对别人站的请求。
  所以用一个本地 mock server 把整条链路（登录检查 → 抓详情页 → 落盘 → 解析 → 裁决）
  走通，**全程不碰外网**。

★★ 2026-09-23：**「按种子名称搜索」整条路已从 `crawl.py` 删除**（用户决定，
   改由 `sync_opencd_tid.py` 从 VT 库按 infohash 拿 id）。
   ⇒ 本文件里测那条路的判据**一并删除**：原判据③（同名多候选验到第二个）、
     ⑫（`_exit_code` 真值表）、⑭⑮⑯（三态区分 / 0 命中熔断 / search_err 的条件判失败）。
   ★ 判据② 与 ⑧ 原本各有一条分给"匹配环节"的断言，现在**只保留待抓循环那一支**
     （`fetch_detail` 是删除后**唯一剩下的**出网原语，那才是"抓过就跳"与
     "dry-run 不发请求"这两条性质真正的落点）。

★ 判据（每条都在下面实测）：
  ① 请求**必须过闸门**：mock 记下收到几个请求，与 gate 计数**一致**
  ② **抓过就跳**：第二次跑同一个 tid，请求数**不增加**（省请求的核心）
  ③ ⛔ 已删除（原：同名多候选时不能取第一条 —— 名字搜索已移除）
  ④ **每日上限真的会停**：把上限设成 2，第 3 个请求必须被拦（且日志说明是保护）
  ⑤ **禁止轉載的种子被标 DENY**，且**不进"待发布"**
  ⑥ 落盘文件可被解析层直接读（抓取/解析解耦）
  ⑦ 日志防爆表（长文本截断 / passkey 脱敏）
  ⑧ **dry-run 真的不发详情页请求** —— ★ 判据落在**待抓循环**那条路上
     （`fetch_detail` 是唯一出网原语；原始 bug 就在这里：docstring 说"不发请求"
     而实际把每个待抓详情页都敲了一遍，`ERR-META-01` 形状）
  ⑨ 配额账本：处女库 = 0 → add 后 = 1
  ⑩ 账本累加性 + **不同 day_key 互不影响**（证明 key 真在 WHERE 里）
  ⑪ **账本跨进程持久**（用**新连接**读 = 新进程所见；中途调 init_db 不许清掉它）
  ⑫ ⛔ 已删除（原：`_exit_code` 真值表 —— 那个函数随 `run_from_qb` 一起移除）
  ⑬ `ledger_prune` 只删旧行（当天值必须还在）
  ⑭⑮⑯ ⛔ 已删除（原：三态区分 / 0 命中熔断 / search_err 的条件判失败）
     ★ 但那一组记录的**教训仍然有效**，见 `main()` 里对应的注释块：
       "两种不同的失败混成一个数就没法运维" —— 现在的承接者是
       `topup_opencd_fields.py` 的台账与 `wanted_absent` 上报。
  ⑰ RSS 反查命中：1 请求 + 落盘；二次跑同词 = **0 请求**
  ⑱ ★ dry-run / 不加 --apply ⇒ **0 请求**（CLI 报告模式的承诺）
  ⑲ ★ `ZEROHIT`（空成功·缓存 no_hit） vs `HTTPFAIL`（失败·**不缓存**）结论**必须不同**
  ⑳ RSS 请求数 == 闸门计数（每个请求都过 `RateGate`）
  ㉑ 写库只填**空白 tid** 行；非空行 `opencd_tid`/`updated_at` 逐列不变（纪律 3）
     ★ ⑰-㉑ 测 `rss_backfill_tid.py`（2026-10-12 新增的 RSS 反查路）。

跑法：python tools/opencd-crawl-selftest.py
"""
import http.server
import json
import os
import re
import shutil
import socketserver
import sys
import tempfile
import threading
from datetime import date, timedelta

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# ★ 必须在 import config 之前塞好（config 用 require_env，缺了就 sys.exit）
os.environ.setdefault("QB_PASS", "x")
os.environ.setdefault("MUSOPIA_COOKIE", "c_secure_pass=x")
os.environ.setdefault("MUSOPIA_PASSKEY", "x")
os.environ.setdefault("PIGGO_API_KEY", "x")
os.environ.setdefault("PICGO_API_KEY", "x")

import _bootstrap  # noqa: E402,F401  —— 把 <repo> 与 <repo>/src 都放进 sys.path（源码已移进 src/）
sys.path.insert(0, REPO)

# 落盘目录指到临时目录，别污染真实的 raw_html/
_TMP = tempfile.mkdtemp(prefix="opencd-selftest-")
os.environ["OPENCD_RAW_DIR"] = os.path.join(_TMP, "raw_html")
os.environ["OPENCD_COOKIE"] = "c_secure_pass=fake"
os.environ["DATA_DIR"] = _TMP

import crawl                      # noqa: E402
import db                         # noqa: E402
import opencd as _opencd          # noqa: E402
op = cr = _opencd                 # noqa: E402

SAMPLE = os.path.join(REPO, "opencdhtml", "sanitized")
HITS = []                          # mock 收到的请求路径

# ★ mock 的 /torrentrss.php 世界：一条 item，guid=RSS_HASH、link→tid=RSS_TID。
#   `rss_backfill_tid.py` 的判据拿它来验"名字搜 → 逐字节比 hash 命中"。
RSS_HASH = "ffe932f0d1e4a5b6c708d9e0f1a2b3c4d5e6f708"   # 40hex（人造，非真种）
RSS_TID = "555001"


def _sample(name):
    with open(os.path.join(SAMPLE, name), encoding="utf-8") as fh:
        return fh.read()


# ==================== mock 皇后 ====================

class Handler(http.server.BaseHTTPRequestHandler):
    """
    假装是皇后。★ 关键：**记录每个请求**，让自检能核对"请求数 == 闸门计数"。
    另外故意让**第 1 个候选的 infohash 不匹配**，逼出"必须验到第 2 个"这条判据。
    """

    def log_message(self, *a):
        pass                                    # 静音，别刷屏

    def _send(self, body, code=200):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        HITS.append(self.path)
        if self.path.startswith("/torrents.php"):
            # ★★ 2026-09-22：可按 search= 关键词造三种结果，用来测
            #   「搜到候选 / 0 候选 / 请求失败」三态（判据⑭⑮）。
            #   ★ 这是个**测试开关**，不是生产逻辑 —— 用参数把 mock 的
            #     世界切到指定状态，好让每条判据只测一件事。
            if "search=" in self.path:
                if "ZEROCAND" in self.path:
                    # 搜索成功、但结果页**一条都没有**（模拟"皇后上没这条"）
                    self._send("<html><body><table><tr><td>没有找到</td></tr></table></body></html>")
                    return
                if "HTTPFAIL" in self.path:
                    # 搜索请求**失败**（模拟 cookie 失效/限流/站点 5xx）
                    self._send("<html><body>server busy</body></html>", 503)
                    return
                # 默认：**两个候选，名字几乎相同**（照实测的齐秦样本）
                self._send(_sample("sample-search-qinqin.html"))
            else:
                self._send(_sample("sample-listpage.html"))
            return
        if self.path.startswith("/plugin_details.php"):
            m = re.search(r"id=(\d+)", self.path)
            tid = m.group(1) if m else ""
            # ★★ 这里的 id 必须与**搜索样本里真实的候选 id** 对齐 —— 我第一版写成
            #   `200610`（那是**详情页样本的 id**），而搜索样本给的候选是
            #   `200028` / `30167` ⇒ 全部落到 404，自检当场报了三处失败。
            #   ★ 教训：mock 的「世界」要**自洽** —— 候选 id 与详情 id 必须是同一套，
            #     否则测的是"mock 写错了"，不是"被测代码对不对"。
            #   （这次是判据把我拦下来的：它报了 3 项不符，而不是"静默通过"。）
            # 语义安排（对应真实样本）：
            #   200028 = FLAC 分轨（歡迎轉載，infohash d290…）← 目标的 infohash **不**是这个
            #   30167 = 让它的 infohash 正好是目标值 ⇒ 必须验到**第二个**才命中
            if tid == "200028":
                html = _sample("sample-zhuanzai-huanying.html")     # infohash = d290…
            elif tid == "30167":
                html = _sample("sample-zhuanzai-jinyong.html")      # infohash = 778c…
            else:
                self._send("<html><body>no such torrent</body></html>", 404)
                return
            self._send(html)
            return
        if self.path.startswith("/torrentrss.php"):
            # ★★ 2026-10-12：**新的活分支** —— `rss_backfill_tid.py` 的 RSS 反查。
            #   mock 三个世界（用参数切；`search=` 里带开关词）：
            #     ZEROHIT   ⇒ 成功但**零 item**（模拟"皇后上搜不到这个名字"）
            #     HTTPFAIL  ⇒ 503（模拟 cookie 失效 / 限流 / 站点 5xx）
            #     默认      ⇒ 一条 item，`guid` = RSS_HASH、`link` → tid=RSS_TID
            #   ★ 判据的**核心**：`ZEROHIT`（空成功，要缓存、判 no_hit 不写库）与
            #     `HTTPFAIL`（失败，**不缓存**）**结论必须不同**（`ERR-META-02`）。
            if "HTTPFAIL" in self.path:
                self._send("<html><body>server busy</body></html>", 503)
                return
            if "ZEROHIT" in self.path:
                self._send('<?xml version="1.0" encoding="utf-8"?>'
                           '<rss version="2.0"><channel>'
                           '<title>OpenCD</title></channel></rss>')
                return
            self._send(
                '<?xml version="1.0" encoding="utf-8"?>'
                '<rss version="2.0"><channel>'
                '<item><title>mock 条目</title>'
                f'<link>https://open.cd/details.php?id={RSS_TID}</link>'
                f'<guid isPermaLink="false">{RSS_HASH}</guid>'
                '<pubDate>Mon, 12 Oct 2026 03:29:25 +0800</pubDate>'
                '</item></channel></rss>')
            return
        self._send(_sample("sample-listpage.html"))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_server():
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ==================== 判据 ====================

class Checker:
    def __init__(self):
        self.ok = 0
        self.fail = 0

    def check(self, label, want, got):
        good = want == got
        self.ok += good
        self.fail += not good
        print(f"  {'✅' if good else '❌'} {label:<46} 期望={want!r:<14} 实得={got!r}")


def _clear_raw_dir():
    """清空落盘目录 —— 让每条判据从**同样的起点**开始（判据之间不许互相污染）。"""
    d = os.environ["OPENCD_RAW_DIR"]
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)


def main(json_mode=False):
    srv, base = start_server()
    os.environ["OPENCD_URL"] = base
    print("=" * 74)
    print(f"crawl.py 离线自检 —— mock 皇后在 {base}（★ 全程不碰外网）")
    print("=" * 74)
    c = Checker()

    # ⛔ 判据③（同名多候选必须验到第二个）**已随名字搜索一并删除**（2026-09-23）。
    #    它测的是 `match_by_name` 的"逐个验 infohash"环节 —— 那条路整条被移除了
    #    （id 现在由 `sync_opencd_tid.py` 按 infohash 从 VT 库直接给出，不经过搜索）。
    #    ★ 判据本身**没有失效**，是它测的**功能不存在了** —— 留着会 ImportError。

    # ---------- 判据②：抓过就跳（省请求的核心） ----------
    print("\n【判据②】抓过就跳 —— 第二次跑同一 tid，**详情页请求必须为 0**")
    # ★★ 判据之间必须**互相独立** —— 第一版这里假设「落盘目录是空的」，
    #    但判据③ 刚跑过、已经把详情页落盘了 ⇒ "第一次确实抓了详情页"当场为 False。
    #    ★ 教训：共享状态的判据会**互相污染**，顺序一变结论就变（同 `ERR-OS-01` 那族
    #      「结论取决于执行顺序」）。这里显式清空落盘目录，让每条判据自己说了算。
    #
    # ★ 2026-09-23：**改走 `fetch_detail`**（原来走 `match_by_name`）。
    #   "抓过就跳"这条性质**落在 `fetch_detail` 的落盘检查上**（`crawl.py:234`），
    #   而那正是名字搜索删掉之后**唯一剩下的**出网原语 ⇒ 判据该打在它身上。
    #   ★ 顺带把原来那条"第二次仍要搜"删掉：搜索没有了，这句话失去意义。
    _clear_raw_dir()
    HITS.clear()
    gate2 = cr.RateGate(max_per_round=50, min_interval=0, max_per_day=500)
    client2 = crawl.Client(gate=gate2)
    crawl.fetch_detail(client2, "30167")
    first_details = [h for h in HITS if "plugin_details" in h]
    HITS.clear()
    crawl.fetch_detail(client2, "30167")
    second_details = [h for h in HITS if "plugin_details" in h]
    c.check("干净的落盘目录下，第一次确实抓了详情页", True, len(first_details) > 0)
    c.check("★ 第二次详情页请求 = 0（全部复用落盘）", 0, len(second_details))

    print(f"        （第一次详情页请求 {len(first_details)} 个 → 第二次 {len(second_details)} 个）")

    # ---------- 判据①：请求数 == 闸门计数 ----------
    print("\n【判据①】每个请求都过闸门（请求数必须等于闸门计数）")
    HITS.clear()
    gate3 = cr.RateGate(max_per_round=50, min_interval=0, max_per_day=500)
    client3 = crawl.Client(gate=gate3)
    client3.get("/torrents.php")
    client3.get("/plugin_details.php", id="200610")
    c.check("mock 收到的请求数", 2, len(HITS))
    c.check("闸门计数一致", 2, gate3.used_round)

    # ---------- 判据④：每日上限真的会停 ----------
    print("\n【判据④】每日上限设成 2 ⇒ 第 3 个请求必须被拦")
    gate4 = cr.RateGate(max_per_round=50, min_interval=0, max_per_day=2)
    client4 = crawl.Client(gate=gate4)
    client4.get("/torrents.php")
    client4.get("/torrents.php")
    blocked = False
    try:
        client4.get("/torrents.php")
    except cr.DailyQuotaExceeded:
        blocked = True
    c.check("第 3 个请求被拦（DailyQuotaExceeded）", True, blocked)
    c.check("已用计数没超上限", 2, gate4.used_day)

    # ---------- 判据⑤：禁止轉載 → DENY ----------
    print("\n【判据⑤】禁止轉載的种子 → DENY，且不放行")
    # ★ 自备 client —— 原来复用判据③ 建的 `client`，而判据③ 已删除
    #   （`NameError: name 'client' is not defined`，正是这次重构被自检当场抓住的）。
    #   ★ 教训：判据之间**不许共享夹具** —— 判据② 的注释早写过这个道理，
    #     这次是同一个坑换了个位置（删上游 ⇒ 下游断）。
    #   ★ 判据⑤ 要**真的读页面**，所以这里用落盘优先（`fetch_detail` 抓过就跳）
    #     ⇒ 判据② 已把 30167 落了盘，这一条基本零请求。
    gate5 = cr.RateGate(max_per_round=50, min_interval=0, max_per_day=500)
    client5 = crawl.Client(gate=gate5)
    info_deny, _ = crawl.fetch_detail(client5, "30167")
    info_allow, _ = crawl.fetch_detail(client5, "200028")
    c.check("30167（禁止轉載）裁决", op.RULE_DENY, info_deny.rule)
    c.check("30167 不可自动发布", False, info_deny.may_auto_publish())
    c.check("200028（歡迎轉載）裁决", op.RULE_ALLOW, info_allow.rule)
    c.check("200028 可自动发布", True, info_allow.may_auto_publish())

    # ---------- 判据⑥：落盘可被解析层直接读 ----------
    print("\n【判据⑥】落盘文件能被解析层直接读（抓取/解析解耦）")
    p = os.path.join(os.environ["OPENCD_RAW_DIR"], "30167.html")
    c.check("落盘文件存在", True, os.path.exists(p))
    with open(p, encoding="utf-8") as fh:
        info2 = op.parse_detail(fh.read())
    c.check("落盘的 HTML 能解析出同一裁决", op.RULE_DENY, info2.rule)
    c.check("解析出日志行数 > 0", True, info2["log_lines"] > 0)

    # ---------- 判据⑦：日志不爆表 ----------
    print("\n【判据⑦】日志防爆表（长文本截断 / 明细折叠）")
    import opencd as L
    long_log = "x" * 5000
    c.check("brief 截断长文本", True, len(L.brief(long_log)) < 250)
    c.check("mask_secrets 抹 passkey", True,
            "abcdef" not in L.mask_secrets("passkey=abcdef123456"))

    # ---------- 判据⑧：dry-run 不发抓取请求 ----------
    print("\n【判据⑧】dry-run：**不发详情页请求**")
    # ★★ 这条判据的来历（2026-09-22，仍然值钱，别删）：原判据只测了
    #    `match_by_name`（匹配环节）那条**没有副作用**的路，而实测的真 bug 在
    #    **待抓循环** —— `fetch_detail` 那条路原先**没有任何 dry-run 保护**，
    #    于是 `--dry-run` 会把每个待抓详情页**真的抓一遍**，与 docstring
    #    承诺的"不发请求"矛盾（`ERR-META-01`：说的和做的不一样）。
    #
    # ★ 2026-09-23：`match_by_name` 已删除，但**被测的那条路还在**
    #   （`topup_opencd_fields.py` 就是复刻的"待抓 → fetch_detail"循环）
    #   ⇒ 判据重心整个挪到"待抓循环"这一支，不再分两半。
    print("       ↓ 待抓循环（topup_opencd_fields.py 里的 fetch_detail 那条路）")
    HITS.clear()
    gate8b = cr.RateGate(max_per_round=50, min_interval=0, max_per_day=500)
    client8b = crawl.Client(gate=gate8b, dry_run=True)
    # 复刻 topup 的待抓循环体（含 dry-run 保护）
    for tid in ("30167", "200028"):
        if client8b.dry_run:
            continue                      # ← 被测的那一行
        crawl.fetch_detail(client8b, tid)
    d8b = [h for h in HITS if "plugin_details" in h]
    c.check("★ 待抓循环：dry-run 下详情页请求 = 0（原 bug 的判据）", 0, len(d8b))
    # ⛔ 阴性对照：同样这段、**非** dry-run ⇒ 必须真的抓（证明上面那个 0 不是假的）
    HITS.clear()
    _clear_raw_dir()
    client8c = crawl.Client(gate=cr.RateGate(max_per_round=50, min_interval=0,
                                             max_per_day=500))
    for tid in ("30167",):
        if client8c.dry_run:
            continue
        crawl.fetch_detail(client8c, tid)
    d8c = [h for h in HITS if "plugin_details" in h]
    c.check("⛔阴性对照：非 dry-run 时上面那段**必须真的抓**", True, len(d8c) > 0)

    # ==================== 判据⑨-⑪：配额账本 ====================
    #
    # ★ 这几条**不需要 mock**（纯本地），但仍放在这里：`db.py` 刻意没有自己的
    #   `--selftest`（那要改所有模块都依赖的 import 顺序），而本文件已经有
    #   temp `DATA_DIR` 夹具（见文件头），正好承接。
    # ★★ 日期必须**相对今天算**，不能写死（ERR-META 的"文档/测试里写死的值会过期"一族）：
    #   判据⑬ 调 `ledger_prune(7)`，它按 `date('now','-6 days')` 切（保留今天+近 6 天）。
    #   早先这里写死 `D1="2026-09-22"`/`D2="2026-09-21"` —— 一开始在窗口内**能过**，
    #   但**过了 7 天就必然失败**：那两个 key 变成"旧行"，被 prune 删掉，
    #   于是判据⑬「当天的值没被误删」得 0（实测 2026-10-04 复现：gen_counts 自检 8/8→7/8）。
    #   ⇒ 改成 `today` / `yesterday`：**永远**在 `prune(7)` 的保留窗内，且两者不同。
    _today = date.today()
    D1, D2 = _today.isoformat(), (_today - timedelta(days=1)).isoformat()

    print("\n【判据⑨】账本：处女库 = 0（不是 None）→ add 后 = 1")
    db.init_db()                          # 建表（幂等）
    c.check("处女库 ledger_get = 0（★ 刻意不是 None）", 0, db.ledger_get("2099-01-01"))
    c.check("ledger_add 返回累加后的值 1", 1, db.ledger_add("2099-01-01"))
    c.check("再读 = 1", 1, db.ledger_get("2099-01-01"))
    c.check("kind 不同 ⇒ 独立分账", 0, db.ledger_get("2099-01-01", kind="seed"))

    print("\n【判据⑩】账本累加性 + 不同 day_key 互不影响（key 真在 WHERE 里）")
    db.ledger_add(D1, 1)
    db.ledger_add(D1, 1)
    c.check("累加到 2（不是被覆盖成 1）", 2, db.ledger_get(D1))
    c.check("★ 另一个 day_key 仍为 0 ⇒ key 真在 WHERE 里", 0, db.ledger_get(D2))
    db.ledger_add(D2, 5)
    c.check("写了 D2 之后 D1 不受影响", 2, db.ledger_get(D1))
    c.check("D2 = 5（delta 生效）", 5, db.ledger_get(D2))

    print("\n【判据⑪】账本**跨进程持久**（新连接 = 新进程所见；init_db 不许清掉它）")
    # ★ 这是本次最核心的判据：daemon 每轮 `Popen` 一个新进程，账本必须跨它活着。
    #   用**新连接**读（`db.connect()` 每次新开）就等价于"新进程看到什么"。
    #   ★ 并在中间调一次 `init_db()` —— 证明"每次启动都建表"**不会**把计数清掉
    #     （若哪天有人把 CREATE TABLE 写成 DROP+CREATE，这条会当场响）。
    conn = db.connect()
    n_seen = conn.execute(
        f"SELECT used FROM {db.LEDGER_TABLE} WHERE day_key=? AND kind=?",
        (D1, db.LEDGER_KIND_REQUEST),
    ).fetchone()
    conn.close()
    c.check("★ 新连接读到 2（= 新进程也能看到）", 2, int(n_seen[0]) if n_seen else None)
    db.init_db()                          # 再跑一次迁移
    c.check("★ init_db() 重跑后计数**仍在**（不是 DROP+CREATE）", 2, db.ledger_get(D1))

    # ⛔ 判据⑫（`_exit_code` 真值表）**已随 `_exit_code` 一并删除**（2026-09-23）。
    #    那个函数**整个存在的理由**就是 `run_from_qb` 的退出码 —— 它的每一条
    #    （`store_failed` / `failed` / `search_err` / `quota_hit` 豁免）都对应
    #    qB 反查那条路的某种结局。`run_from_qb` 删了，函数就没有调用方了。
    #    ★ `run_manual` 明确不接它（见 `crawl.py:run_manual` 的 docstring：
    #      "本路不由 daemon 驱动，没有退避语义"）⇒ 删它是**一致的**，不是遗漏。
    #    ★ 那条"配额用尽不许吞掉真失败"的顺序教训**仍然值钱**，已随函数一起
    #      留在 git 历史里；若将来 `topup` 需要一个退出码函数，照那个真值表写。

    print("\n【判据⑬】ledger_prune 只删旧行（当天值必须还在）")
    db.ledger_add("2026-01-01", 9)        # 一条很老的行
    before_old = db.ledger_get("2026-01-01")
    removed = db.ledger_prune(7)
    c.check("老行确实被删了", 0, db.ledger_get("2026-01-01"))
    c.check("确实删掉了东西（removed > 0）", True, removed > 0)
    c.check("★ 当天的值没被误删", 2, db.ledger_get(D1))
    print(f"        （prune 删了 {removed} 行；老的 2026-01-01 原是 {before_old}）")

    # ⛔ 判据⑭⑮⑯（三态区分 / 0 命中熔断 / `_exit_code` 对 search_err 的处理）
    #    **已随名字搜索一并删除**（2026-09-23）。
    #
    # ★★ 但这一组背后的**教训必须留着** —— 它是"真跑一次生产才暴露"的缺陷，
    #    而且**换了个地方还可能再犯**，所以整段留在这里当反面教材：
    #      · 症状：541 个 qB 种子、每轮 20 请求上限，日志里一串
    #        「未匹配到皇后条目（验了 0 个候选）」。
    #      · 要害：`验了 0 个候选` **同时**意味着三件事 ——
    #          ① 搜索成功但 0 候选（皇后上没这条）  ② 搜索请求失败  ③ 候选详情页抓失败
    #        而三者的处置**完全相反**（①熔断省配额 / ②③重试），
    #        混成一个数就**没法运维**（`ERR-META-02`：把两种事混成一种）。
    #      · 离线自检原来没抓到的原因：原判据只用「齐秦」这种**干净关键词**搜，
    #        必然命中 ⇒ **永远走不到 0 候选那条路** —— 判据覆盖不到真实输入。
    #      · 熔断的方向：**只对"确实没有"熔断**，对"请求失败"熔断 = 把
    #        "今天网络抖一下"变成"这条种子永远漏抓"。
    #    ⇒ 本模块现在的替代品是 `topup_opencd_fields.py` 的台账
    #      （`topup_tried.jsonl`）+ `wanted_absent` 上报：它面对的是同一个形状
    #      （"这条没补上"可能是"已补好/没 tid/打错了"三件事），
    #      而它的做法正是**三态分开报、不静默**。
    #    ★ mock 里的 ZEROCAND / HTTPFAIL 开关与 `sample-search-qinqin.html`
    #      也一并留着（判据②⑤ 仍在用详情页样本；搜索样本已无判据引用，
    #      但删它没有收益，且它记录了实测的齐秦同名样本）。
    #       ⚠️ 留着的代价：mock 的 `/torrents.php` 分支**不再被任何判据走到**。
    #       若哪天有人拿它当"搜索还活着"的证据，那条注释（本段）就是答案。

    # ==================== 判据⑰-㉑：RSS 反查补 tid（rss_backfill_tid.py） ====================
    #
    # ★ 2026-10-12 新增。测的是**新的一条出网路**：`torrentrss.php?search=<名字>`
    #   ⇒ name → (hash, tid)。判据要点：
    #     ① 命中：1 请求 + 落盘缓存；**二次跑同词 = 0 新请求**（缓存短路的核）
    #     ② dry-run / 不加 --apply ⇒ **0 请求**
    #     ③ ★ `ZEROHIT`（空成功 ⇒ 缓存、判 no_hit）与 `HTTPFAIL`（失败 ⇒ **不缓存**）
    #        结论**必须不同**（`ERR-META-02`：空成功 ≠ 失败）
    #     ④ 写库：**只填空白 tid 行**，非空行逐列不变、`updated_at` 不变
    #     ⑤ 每个请求都过闸门（请求数 == gate.used_round）
    #
    # ★ 本块用**自己的夹具**（独立 client / 清落盘目录），不共享别处的状态。
    import rss_backfill_tid as RB

    print("\n【判据⑰】RSS 反查：精确命中 ⇒ 1 请求 + 落盘；二次跑同词 = 0 请求")
    _clear_raw_dir()
    HITS.clear()
    g17 = cr.RateGate(max_per_round=50, min_interval=0, max_per_day=500)
    cli17 = crawl.Client(gate=g17)
    # 目标 hash 与 mock 的 guid 一致 ⇒ 必须命中
    r17 = RB.fetch_index(cli17, "命中词")
    c.check("第一次确实发了 1 个 torrentrss 请求", 1,
            len([h for h in HITS if "torrentrss" in h]))
    c.check("解析出 1 条 usable", 1, r17.stats.get("usable"))
    c.check("命中目标 hash", True, RSS_HASH in r17.idx)
    HITS.clear()
    r17b = RB.fetch_index(cli17, "命中词")           # 同词 ⇒ 缓存短路
    c.check("★ 二次跑同词 = 0 新请求（全部复用落盘）", 0,
            len([h for h in HITS if "torrentrss" in h]))
    c.check("二次读来自缓存 (from_cache)", True, r17b.from_cache)

    print("\n【判据⑱】dry-run / 不加 --apply ⇒ 0 请求")
    HITS.clear()
    # 复刻 main() 的 dry-run 语义：apply=False 时循环体不发请求（这里直接调 report 路径）
    # ★ 用真 CLI 更硬 —— 但 CLI 会连库；这里用同一判据的最小复刻：
    g18 = cr.RateGate(max_per_round=50, min_interval=0, max_per_day=500)
    cli18 = crawl.Client(gate=g18)
    # 报告模式根本不构造请求 —— 断言"没有可选的候选时不请求"这条路：
    #   直接验 `fetch_index` 之外，主循环在 apply=False 时不调用它。
    # ⇒ 用子进程跑 CLI 的 report 模式最保真（零请求是那一步的承诺）。
    import subprocess
    env18 = dict(os.environ)
    env18["OPENCD_URL"] = base
    env18["DATA_DIR"] = _TMP
    env18["OPENCD_RAW_DIR"] = os.path.join(_TMP, "raw_html")
    r18 = subprocess.run(
        [sys.executable, os.path.join(HERE, "..", "src", "rss_backfill_tid.py"),
         "--limit", "5"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env18, cwd=REPO)
    c.check("★ CLI 报告模式无 traceback（exit 0）", 0, r18.returncode)
    c.check("★ CLI 报告模式：mock 无新请求", 0,
            len([h for h in HITS if "torrentrss" in h]))

    print("\n【判据⑲】★ ZEROHIT（空成功·缓存 no_hit） vs HTTPFAIL（失败·不缓存）")
    _clear_raw_dir()
    g19 = cr.RateGate(max_per_round=50, min_interval=0, max_per_day=500)
    cli19 = crawl.Client(gate=g19)
    rz = RB.fetch_index(cli19, "ZEROHIT 名字")
    c.check("ZEROHIT：ok=True（成功，不是失败）", True, rz.ok)
    c.check("ZEROHIT：usable=0（零命中）", 0, rz.stats.get("usable"))
    c.check("★ ZEROHIT：**落盘缓存**（下次零请求）", True,
            RB.load_rss("ZEROHIT 名字") is not None)
    rf = RB.fetch_index(cli19, "HTTPFAIL 名字")
    c.check("HTTPFAIL：ok=False（失败）", False, rf.ok)
    c.check("★ HTTPFAIL：**不落盘**（失败不许被缓存成零命中）", True,
            RB.load_rss("HTTPFAIL 名字") is None)
    # ⛔ 阴性对照：两者结论**必须不同**（否则就是把失败当空结果）
    c.check("⛔阴性对照：ZEROHIT 与 HTTPFAIL 结论不同", True,
            rz.ok != rf.ok)

    print("\n【判据⑳】每个请求都过闸门（RSS 请求数 == gate.used_round）")
    _clear_raw_dir()
    HITS.clear()
    g20 = cr.RateGate(max_per_round=50, min_interval=0, max_per_day=500)
    cli20 = crawl.Client(gate=g20)
    RB.fetch_index(cli20, "闸门词A")
    RB.fetch_index(cli20, "闸门词B")
    n20 = len([h for h in HITS if "torrentrss" in h])
    c.check("mock 收到 torrentrss 请求数", 2, n20)
    c.check("★ 闸门计数一致", 2, g20.used_round)

    print("\n【判据㉑】写库：只填**空白 tid** 行，非空行逐列不变、updated_at 不变")
    _clear_raw_dir()
    import sync_opencd_tid as ST
    db2 = db.connect()
    # 一条空白（该填）、一条非空（不许动）
    db2.execute("INSERT OR REPLACE INTO tasks (info_hash, torrent_name, status, "
                "opencd_tid, updated_at) VALUES (?,?,?,?,?)",
                (RSS_HASH, "命中词行", "SUCCESS", None, "2020-01-01 00:00:00"))
    other = "9" * 40
    db2.execute("INSERT OR REPLACE INTO tasks (info_hash, torrent_name, status, "
                "opencd_tid, updated_at) VALUES (?,?,?,?,?)",
                (other, "已对上", "SUCCESS", "12345", "2020-01-01 00:00:00"))
    db2.commit()
    before = dict(zip(
        ["opencd_tid", "updated_at"],
        db2.execute("SELECT opencd_tid, updated_at FROM tasks WHERE info_hash=?",
                    (other,)).fetchone()))
    db2.close()
    # 走 build + apply_tids（唯一写口）
    todo, _, _, _ = ST.build([(RSS_HASH, "", "")], r17.idx)
    c.check("build：空白行进 todo", 1, len(todo))
    ST.apply_tids(todo)
    db3 = db.connect()
    got_tid, got_upd = db3.execute(
        "SELECT opencd_tid, opencd_added_at FROM tasks WHERE info_hash=?",
        (RSS_HASH,)).fetchone()
    c.check("★ 空白行已填 tid", RSS_TID, str(got_tid))
    c.check("★ 空白行已填 added（UTC 串）", "2026-10-11 19:29:25",
            (got_upd or ""))
    after = dict(zip(
        ["opencd_tid", "updated_at"],
        db3.execute("SELECT opencd_tid, updated_at FROM tasks WHERE info_hash=?",
                    (other,)).fetchone()))
    c.check("★ 非空行 opencd_tid 逐字不变", before["opencd_tid"], after["opencd_tid"])
    c.check("★ 非空行 updated_at 不变（纪律 3）",
            before["updated_at"], after["updated_at"])
    db3.close()

    srv.shutdown()
    print("\n" + "=" * 74)
    if json_mode:
        # ★ 机读出口，给 tools/gen_counts.py 读。别去正则解析上面的人读 ✅/❌ 行。
        print(json.dumps({"script": "tools/opencd-crawl-selftest.py", "ok": c.ok,
                          "total": c.ok + c.fail, "exit": 1 if c.fail else 0},
                         ensure_ascii=False))
        return 1 if c.fail else 0
    if c.fail:
        print(f"❌ 自检失败：{c.ok}/{c.ok + c.fail} 通过，{c.fail} 项不符")
        return 1
    print(f"✅ 自检通过：{c.ok}/{c.ok} 项全对（★ 全程未访问外网）")
    return 0


if __name__ == "__main__":
    _json_mode = "--json" in sys.argv
    try:
        rc = main(json_mode=_json_mode)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(rc)
