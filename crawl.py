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

═══════════════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════════════
    python crawl.py --from-qb              # 主路：扫 qB → 匹配 → 抓新种子
    python crawl.py --tid 200610           # 主动抓：指定 id
    python crawl.py --url <详情页URL>       # 主动抓：指定 URL
    python crawl.py --search "齐秦"         # 主动抓：按名字搜（只列候选，不抓）
    python crawl.py --from-qb --dry-run    # 只列会抓哪些，不发请求（除必要匹配）
"""
import argparse
import json
import os
import re
import sys
import time

import requests

import config
import opencd_log as L
import opencd_parse
import opencd_rate

sys.stdout.reconfigure(encoding="utf-8")

# 落盘目录（★ 解析层永远读它，不重复请求 —— 这是"抓取与解析解耦"的落地）
RAW_DIR = os.getenv("OPENCD_RAW_DIR", os.path.join(config.DATA_DIR, "raw_html"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ==================== 默认频率参数（可用 .env 覆盖） ====================
# ★ 这几个数都**先给保守值**，跑一周看真实量再调（`SUMMARY §12` 第 3 步）。
DEFAULT_MAX_PER_ROUND = int(os.getenv("OPENCD_MAX_PER_ROUND", "20"))
DEFAULT_MIN_INTERVAL = float(os.getenv("OPENCD_MIN_INTERVAL", "2"))
DEFAULT_MAX_PER_DAY = int(os.getenv("OPENCD_MAX_PER_DAY", "300"))
# 2026-09-19 定的：阶段周期由 daemon 传，默认 1800s（30 min）
DEFAULT_INTERVAL = int(os.getenv("OPENCD_INTERVAL", "1800"))

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
        self.gate = gate or opencd_rate.RateGate(
            max_per_round=DEFAULT_MAX_PER_ROUND,
            min_interval=DEFAULT_MIN_INTERVAL,
            max_per_day=DEFAULT_MAX_PER_DAY,
            log=log,
        )
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


def search_candidates(client, name, limit=5):
    """
    按名字搜皇后，返回候选 [ListRow, ...]（最多 limit 条）。
    URL 形态抄自参考实现 `repos/auto_feed_js/auto_feed.user.js:4395`。
    """
    r = client.get("/torrents.php", **{
        "incldead": 0, "spstate": 0, "inclbookmarked": 0,
        "search": name, "search_area": 0, "search_mode": 0,
    })
    if r.status_code != 200:
        return []
    rows = opencd_parse.parse_list(r.text)
    return rows[:limit]


def match_by_name(client, qb_name, want_hash, *, max_candidates=5):
    """
    用名字搜候选、逐个进详情页验 infohash，直到命中。
    返回 (tid, rows_checked) 或 (None, rows_checked)。

    ★ **绝不取第一条** —— 同名不同格式的种子很常见（实测）。
    """
    cands = search_candidates(client, qb_name, limit=max_candidates)
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
            return tid, checked
    return None, checked


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
    主路：扫 qB → 匹配皇后 → 抓新种子的详情页 → 入库（本步**只落盘**，不发 Musopia）。
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

    stats = {"matched": 0, "fetched": 0, "cached": 0, "denied": 0, "nomatch": 0, "failed": 0}
    pending = []

    for t in torrents:
        h = (t.hash or "").lower()
        name = t.name or ""
        try:
            tid, checked = match_by_name(client, name, h, max_candidates=args.max_candidates)
        except opencd_rate.DailyQuotaExceeded as e:
            log(f"🛑 {e} —— 本轮提前收工（这是保护，不是错误）")
            break
        if not tid:
            stats["nomatch"] += 1
            log(f"  ⚠️  未匹配到皇后条目：{L.brief(name)}（验了 {checked} 个候选）")
            continue
        stats["matched"] += 1

        if tid in known:
            stats["cached"] += 1
            continue
        pending.append((tid, name))

    log(f"🔍 匹配完成：命中 {stats['matched']} · 未命中 {stats['nomatch']} · "
        f"已抓过 {stats['cached']} · 待抓 {len(pending)}")

    # 抓取待办
    results = []
    for tid, name in pending:
        try:
            info, cached = fetch_detail(client, tid)
        except opencd_rate.DailyQuotaExceeded as e:
            log(f"🛑 {e} —— 本轮提前收工")
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
        results.append((tid, info))

    L.summary(log, "crawl",
              命中=stats["matched"], 待抓=len(pending), 已抓=stats["fetched"],
              禁转=stats["denied"], 未命中=stats["nomatch"], 失败=stats["failed"],
              本轮请求=client.gate.used_round, 当日请求=client.gate.used_day)
    return 0


def run_manual(client, args):
    """主动抓：--tid / --url / --search。"""
    if args.search is not None:
        rows = search_candidates(client, args.search, limit=args.max_candidates)
        log(f"🔍 搜「{args.search}」→ {len(rows)} 个候选（**只列候选，不抓**）")
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
    return 0


def main():
    ap = argparse.ArgumentParser(description="OpenCD（皇后）抓取层")
    ap.add_argument("--from-qb", action="store_true", help="主路：扫 qB → 匹配 → 抓")
    ap.add_argument("--tid", help="主动抓：指定详情页 id")
    ap.add_argument("--url", help="主动抓：指定详情页 URL")
    ap.add_argument("--search", help="主动抓：按名字搜（只列候选）")
    ap.add_argument("--force", action="store_true", help="即使本地已有也重抓")
    ap.add_argument("--dry-run", action="store_true", help="只列会做什么，不发请求")
    ap.add_argument("--max-candidates", type=int, default=5, help="每个种子最多验几个候选")
    args = ap.parse_args()

    config.print_runtime_info()
    os.makedirs(RAW_DIR, exist_ok=True)
    log(f"🗂️  OPENCD_RAW_DIR = {RAW_DIR}")

    gate = opencd_rate.RateGate(
        max_per_round=int(os.getenv("OPENCD_MAX_PER_ROUND", DEFAULT_MAX_PER_ROUND)),
        min_interval=float(os.getenv("OPENCD_MIN_INTERVAL", DEFAULT_MIN_INTERVAL)),
        max_per_day=int(os.getenv("OPENCD_MAX_PER_DAY", DEFAULT_MAX_PER_DAY)),
        day_key=time.strftime("%Y-%m-%d"),
        log=log,
    )
    log(f"⏱️  频率：每轮 {gate.max_per_round} 个请求 · 最小间隔 {gate.min_interval}s · "
        f"每日上限 {gate.max_per_day}")

    client = Client(gate=gate, dry_run=args.dry_run)
    if not client.cookie:
        log("❌ 缺 OPENCD_COOKIE（在 .env 里配）—— 皇后的 Cookie，不是 Musopia 的")
        return 2

    if args.from_qb:
        return run_from_qb(client, args)
    return run_manual(client, args)


if __name__ == "__main__":
    sys.exit(main())
