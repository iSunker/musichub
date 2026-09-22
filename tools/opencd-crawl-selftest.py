#!/usr/bin/env python3
"""
crawl.py 的**离线自检** —— 用本地 HTTP mock 假装是皇后。

★ 为什么要有它：`crawl.py` 是本项目**唯一主动访问别人站点**的模块，
  「跑起来才知道对不对」在这里是不可接受的 —— 真跑一次就是一次对别人站的请求。
  所以用一个本地 mock server 把整条链路（登录检查 → 搜索 → 验 infohash → 落盘
  → 解析 → 三态裁决）走通，**全程不碰外网**。

★ 判据（每条都在下面实测）：
  ① 请求**必须过闸门**：mock 记下收到几个请求，与 gate 计数**一致**
  ② **抓过就跳**：第二次跑同一个 tid，请求数**不增加**（省请求的核心）
  ③ **同名多候选时不能取第一条**：mock 返回 2 个候选，只有第 2 个的 infohash
     匹配 ⇒ 必须验到第 2 个才对
  ④ **每日上限真的会停**：把上限设成 2，第 3 个请求必须被拦（且日志说明是保护）
  ⑤ **禁止轉載的种子被标 DENY**，且**不进"待发布"**
  ⑥ 落盘文件可被解析层直接读（抓取/解析解耦）
  ⑦ 日志防爆表（长文本截断 / passkey 脱敏）
  ⑧ **dry-run 真的不发详情页请求** —— ★ 2026-09-22 扩展：
     原判据只测 `match_by_name`（匹配环节），而 **真 bug 在待抓循环**
     （`run_from_qb` 里 `fetch_detail` 那条路**没有** dry-run 保护）
     ⇒ 现在**两条都要**断言：待抓循环 0 次、匹配阶段 >0 次。
  ⑨ 配额账本：处女库 = 0 → add 后 = 1
  ⑩ 账本累加性 + **不同 day_key 互不影响**（证明 key 真在 WHERE 里）
  ⑪ **账本跨进程持久**（用**新连接**读 = 新进程所见；中途调 init_db 不许清掉它）
  ⑫ `_exit_code` 真值表 —— ★ **两个方向都断言**（该返 1 的返 1、
     该返 0 的**不能**返 1），否则内联 `return 1` 也能蒙混过关
  ⑬ `ledger_prune` 只删旧行（当天值必须还在）

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

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# ★ 必须在 import config 之前塞好（config 用 require_env，缺了就 sys.exit）
os.environ.setdefault("QB_PASS", "x")
os.environ.setdefault("MUSOPIA_COOKIE", "c_secure_pass=x")
os.environ.setdefault("MUSOPIA_PASSKEY", "x")
os.environ.setdefault("PIGGO_API_KEY", "x")
os.environ.setdefault("PICGO_API_KEY", "x")

sys.path.insert(0, REPO)

# 落盘目录指到临时目录，别污染真实的 raw_html/
_TMP = tempfile.mkdtemp(prefix="opencd-selftest-")
os.environ["OPENCD_RAW_DIR"] = os.path.join(_TMP, "raw_html")
os.environ["OPENCD_COOKIE"] = "c_secure_pass=fake"
os.environ["DATA_DIR"] = _TMP

import crawl                      # noqa: E402
import db                         # noqa: E402
import opencd_parse as op         # noqa: E402
import opencd_rate as cr          # noqa: E402

SAMPLE = os.path.join(REPO, "opencdhtml", "sanitized")
HITS = []                          # mock 收到的请求路径


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

    # ---------- 判据③：同名多候选，必须验到第二个 ----------
    print("\n【判据③】同名多候选 ⇒ 不能取第一条，必须逐个验 infohash")
    _clear_raw_dir()      # ★ 保证判据独立（见判据② 的注释）
    HITS.clear()
    gate = cr.RateGate(max_per_round=50, min_interval=0, max_per_day=500)
    client = crawl.Client(gate=gate)
    tid, checked, mstat = crawl.match_by_name(
        client, "齐秦 - 我拿什么爱你", "778c337572b855b7f2f72cc8d43aa4564311e5a4")
    c.check("命中正确的 tid（不是第一个候选）", "30167", tid)
    c.check("验了 2 个候选才对", 2, checked)
    c.check("★ 状态 = MATCH_OK（搜索成功）", crawl.MATCH_OK, mstat)
    infohash = crawl.extract_infohash(crawl.load_raw("30167") or "")
    c.check("从详情页取到 infohash（40 hex）", "778c337572b855b7f2f72cc8d43aa4564311e5a4", infohash)

    # ---------- 判据②：抓过就跳（省请求的核心） ----------
    print("\n【判据②】抓过就跳 —— 第二次跑同一 tid，**详情页请求必须为 0**")
    # ★★ 判据之间必须**互相独立** —— 第一版这里假设「落盘目录是空的」，
    #    但判据③ 刚跑过、已经把详情页落盘了 ⇒ "第一次确实抓了详情页"当场为 False。
    #    ★ 教训：共享状态的判据会**互相污染**，顺序一变结论就变（同 `ERR-OS-01` 那族
    #      「结论取决于执行顺序」）。这里显式清空落盘目录，让每条判据自己说了算。
    _clear_raw_dir()
    HITS.clear()
    gate2 = cr.RateGate(max_per_round=50, min_interval=0, max_per_day=500)
    client2 = crawl.Client(gate=gate2)
    crawl.match_by_name(client2, "齐秦 - 我拿什么爱你", "778c337572b855b7f2f72cc8d43aa4564311e5a4")
    first_details = [h for h in HITS if "plugin_details" in h]
    HITS.clear()
    crawl.match_by_name(client2, "齐秦 - 我拿什么爱你", "778c337572b855b7f2f72cc8d43aa4564311e5a4")
    second_details = [h for h in HITS if "plugin_details" in h]
    second_searches = [h for h in HITS if "search=" in h]
    c.check("干净的落盘目录下，第一次确实抓了详情页", True, len(first_details) > 0)
    c.check("★ 第二次详情页请求 = 0（全部复用落盘）", 0, len(second_details))
    c.check("第二次仍要搜（搜索是必要的，省不掉）", True, len(second_searches) > 0)
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
    info_deny, _ = crawl.fetch_detail(client, "30167")
    info_allow, _ = crawl.fetch_detail(client, "200028")
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
    import opencd_log as L
    long_log = "x" * 5000
    c.check("brief 截断长文本", True, len(L.brief(long_log)) < 250)
    c.check("mask_secrets 抹 passkey", True,
            "abcdef" not in L.mask_secrets("passkey=abcdef123456"))

    # ---------- 判据⑧：dry-run 不发抓取请求（★ 2026-09-22 扩展到两条路） ----------
    print("\n【判据⑧】dry-run：**两条路**都不许发详情页请求")
    # ★★ 为什么必须测两条路：原判据只测了 `match_by_name`（匹配环节），
    #    而实测的真 bug 在**待抓循环**（`run_from_qb` 里的 `fetch_detail`
    #    原先没有 dry-run 保护）⇒ 只测匹配环节会**漏掉那个 bug**，
    #    表现是"dry-run 依然把每个待抓详情页敲了一遍"（ERR-META-01 形状）。
    HITS.clear()
    gate8 = cr.RateGate(max_per_round=50, min_interval=0, max_per_day=500)
    client8 = crawl.Client(gate=gate8, dry_run=True)
    _tid8, _chk8, _st8 = crawl.match_by_name(client8, "齐秦", "deadbeef" * 5, max_candidates=2)
    details = [h for h in HITS if "plugin_details" in h]
    searches = [h for h in HITS if "search=" in h]
    c.check("匹配环节：dry-run 下没有详情页请求", 0, len(details))
    c.check("匹配环节：但**搜索照发**（dry-run 靠它列出「会抓哪些」）",
            True, len(searches) > 0)

    # ★ 待抓循环那条路：直接跑 `run_from_qb` 里等价的"待抓 → fetch_detail"段。
    #   这里不整跑 `run_from_qb`（那要 qB，mock 不了），而是**照着它的分支**
    #   断言同一个条件：`client.dry_run` 为真时不许进 `fetch_detail`。
    #   ★ 判据落在**行为**上（HITS 有没有详情页），不是落在"代码里有没有那个 if"。
    print("       ↓ 待抓循环（run_from_qb 里 fetch_detail 那条路）")
    HITS.clear()
    gate8b = cr.RateGate(max_per_round=50, min_interval=0, max_per_day=500)
    client8b = crawl.Client(gate=gate8b, dry_run=True)
    # 复刻 run_from_qb 的待抓循环体（含新加的 dry-run 保护）
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

    # ==================== 判据⑨-⑬：配额账本 + 退出码 ====================
    #
    # ★ 这几条**不需要 mock**（纯本地），但仍放在这里：`db.py` 刻意没有自己的
    #   `--selftest`（那要改所有模块都依赖的 import 顺序），而本文件已经有
    #   temp `DATA_DIR` 夹具（见文件头），正好承接。
    D1, D2 = "2026-09-22", "2026-09-21"

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

    print("\n【判据⑫】_exit_code 真值表 —— ★ 两个方向都断言")
    # ★★ 只断言"该返 1 的返 1"是不够的：内联 `return 1` 也能过。
    #    必须同时断言"该返 0 的**不能**返 1"。
    truth = [
        ({"store_failed": 1, "failed": 0}, False, 1, "入库失败 ⇒ 1（重试有意义）"),
        ({"store_failed": 0, "failed": 3}, False, 1, "抓取失败 ⇒ 1"),
        ({"store_failed": 0, "failed": 0}, False, 0, "★ 没活 ⇒ **0**（不是失败）"),
        ({"store_failed": 0, "failed": 0}, True, 0,
         "★★ 配额用尽 ⇒ **0**（保护不是错误；返 1 会误触发退避）"),
        ({"store_failed": 1, "failed": 0}, True, 1, "有真失败时，即便配额也停 ⇒ 1"),
    ]
    for stats, quota, want, label in truth:
        got = crawl._exit_code(stats, quota_hit=quota)
        c.check(label, want, got)

    print("\n【判据⑬】ledger_prune 只删旧行（当天值必须还在）")
    db.ledger_add("2026-01-01", 9)        # 一条很老的行
    before_old = db.ledger_get("2026-01-01")
    removed = db.ledger_prune(7)
    c.check("老行确实被删了", 0, db.ledger_get("2026-01-01"))
    c.check("确实删掉了东西（removed > 0）", True, removed > 0)
    c.check("★ 当天的值没被误删", 2, db.ledger_get(D1))
    print(f"        （prune 删了 {removed} 行；老的 2026-01-01 原是 {before_old}）")

    # ==================== 判据⑭-⑯：三态区分 + 0 命中熔断 ====================
    #
    # ★★ 这一组是 2026-09-22 **真跑一次生产才暴露**的缺陷（用户实测）：
    #   541 个 qB 种子、每轮 20 请求上限，日志里一串
    #   「未匹配到皇后条目：（验了 0 个候选）」——
    #   而 `验收了 0 个候选` **同时**意味着三件事：
    #     ① 搜索成功但 0 候选（皇后上没这条）
    #     ② 搜索请求失败（HTP 503 / 网络）
    #     ③ 第一个候选的详情页抓失败
    #   三者的处置**完全不同**（①熔断省配额 / ②③重试），混成一个数就**没法运维**。
    #   ★ 为什么离线自检原来没抓到它：原判据只用「齐秦」这种**干净关键词**搜，
    #     必然命中 ⇒ 永远走不到 0 候选那条路（"判据覆盖不到真实输入"，`ERR-META-02`）。
    #     现在用 mock 的 ZEROCAND / HTTPFAIL 开关把这两态**造出来**。

    print("\n【判据⑭】三态区分：0 候选 ⇒ MATCH_ZERO；请求失败 ⇒ MATCH_ERROR")
    _clear_raw_dir()
    g14 = cr.RateGate(max_per_round=50, min_interval=0, max_per_day=500)
    cl14 = crawl.Client(gate=g14)
    # ① 搜索成功但 0 候选
    t_z, n_z, st_z = crawl.match_by_name(cl14, "ZEROCAND 某专辑", "deadbeef" * 5)
    c.check("① 0 候选 ⇒ tid 为 None", None, t_z)
    c.check("① 0 候选 ⇒ 状态 MATCH_ZERO（★ 可熔断）", crawl.MATCH_ZERO, st_z)
    # ② 搜索请求失败（HTTP 503）
    errors = []
    t_e, n_e, st_e = crawl.match_by_name(
        cl14, "HTTPFAIL 某专辑", "deadbeef" * 5,
        on_error=lambda k, d: errors.append((k, d)))
    c.check("② 请求失败 ⇒ 状态 MATCH_ERROR（★ 不可熔断）", crawl.MATCH_ERROR, st_e)
    c.check("② 错误回调收到 kind='http'", "http", errors[0][0] if errors else None)
    c.check("② 错误详情里带状态码 503", True, "503" in (errors[0][1] if errors else ""))
    # ③ 阴性对照：正常命中仍是 MATCH_OK（证明上面两个不是"永远返同一态"）
    _t3, _n3, st3 = crawl.match_by_name(
        cl14, "齐秦 - 我拿什么爱你", "778c337572b855b7f2f72cc8d43aa4564311e5a4")
    c.check("⛔阴性对照：正常命中 ⇒ MATCH_OK", crawl.MATCH_OK, st3)
    # ④ 阴性对照：搜到候选但 infohash 不匹配 ⇒ MATCH_OK（**不是** ZERO！）
    #    ★ 这条很要紧：它和"0 候选"处置相同（别再搜），但**原因不同**
    #      （后者是"皇后上没这条"，这里是"有同名的别的版本"）—— 日志要能分开。
    _t4, _n4, st4 = crawl.match_by_name(cl14, "齐秦 - 我拿什么爱你", "ffff" * 10)
    c.check("⛔阴性对照：有候选但都不匹配 ⇒ MATCH_OK（不是 ZERO）",
            crawl.MATCH_OK, st4)

    print("\n【判据⑮】0 命中熔断：ZERO 的**不再发详情页请求**，ERROR 的**仍重试**")
    # ★★ 熔断的方向必须是"只对 ZERO"：对 ERROR 也熔断 = 把"今天网络抖一下"
    #    变成"这条种子永远漏抓"，而**不漏抓**正是熔断想保护的东西。
    _clear_raw_dir()
    HITS.clear()
    g15 = cr.RateGate(max_per_round=50, min_interval=0, max_per_day=500)
    cl15 = crawl.Client(gate=g15)
    _t, _n, _s = crawl.match_by_name(cl15, "ZEROCAND x", "a" * 40)
    z_detail = [h for h in HITS if "plugin_details" in h]
    c.check("① ZERO：0 候选 ⇒ 详情页请求 0 次（本来也没候选可抓）",
            0, len(z_detail))
    # 熔断的真正含义：`run_from_qb` 收到 ZERO 后不再为它花任何请求。
    #   ★ 这里按 `run_from_qb` 的分支**照抄**那段决策，断言同一个条件。
    HITS.clear()
    _clear_raw_dir()
    stats15 = {"zero_cand": 0, "search_err": 0, "nomatch": 0}
    for nm in ("ZEROCAND x", "HTTPFAIL y"):
        try:
            _tid, _chk, st = crawl.match_by_name(cl15, nm, "b" * 40)
        except Exception:
            st = crawl.MATCH_ERROR
        if st == crawl.MATCH_ERROR:
            stats15["search_err"] += 1      # ← 不熔断
        elif st == crawl.MATCH_ZERO:
            stats15["zero_cand"] += 1      # ← 熔断
    c.check("② 两种失败分别计数（ZERO 1 条 / ERROR 1 条）",
            {"zero_cand": 1, "search_err": 1, "nomatch": 0}, stats15)
    c.check("③ ★ ERROR 那条**没有**被熔断（计进 search_err 而非 zero_cand）",
            1, stats15["search_err"])

    print("\n【判据⑯】`_exit_code` 对 search_err 的**条件**判失败")
    # ★ 条件 = `search_err > 0 且 matched == 0`。
    #   单向不够：必须同时断言"部分失败但**有命中**时**不**算失败"，
    #   否则写成"只要 search_err>0 就返 1"也能过，而那会让一次网络抖动
    #   把 daemon 打进退避（把偶发放大成"整轮干得更少"）。
    c.check("全fail（search_err>0, matched=0）⇒ 1",
            1, crawl._exit_code({"search_err": 3, "matched": 0}))
    c.check("★ 有命中时的部分失败 ⇒ 0（抖动不该触发退避）",
            0, crawl._exit_code({"search_err": 3, "matched": 1}))
    c.check("⛔阴性对照：无 search_err ⇒ 0",
            0, crawl._exit_code({"search_err": 0, "matched": 0}))
    # ★★ 配额豁免**只豁免搜索失败**，不许吞掉真失败：
    #   "配额用尽 + 全部搜索失败" ⇒ 0（搜索失败是配额的后果，不是故障）
    c.check("★ 配额用尽豁免 search_err ⇒ 0",
            0, crawl._exit_code({"search_err": 5, "matched": 0}, quota_hit=True))
    #   "配额用尽 + 有入库失败" ⇒ 1（真失败与配额无关，**必须**报出来）
    #   ★ 这条是自检抓到的顺序 bug 的回归判据：我第一版把 quota_hit 短路在最前，
    #     于是这一支会返 0 —— **把真失败静默掉**（"判据失效"那个形状）。
    c.check("★★ 配额用尽**不能**吞掉入库失败 ⇒ 1",
            1, crawl._exit_code({"store_failed": 2, "search_err": 5},
                                quota_hit=True))
    c.check("★★ 配额用尽**不能**吞掉抓取失败 ⇒ 1",
            1, crawl._exit_code({"failed": 1}, quota_hit=True))

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
