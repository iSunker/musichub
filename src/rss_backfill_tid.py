#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""★ 用皇后 **RSS 反查**补 `tasks.opencd_tid`（name → hash → tid），过同一个 RateGate。

═══════════════════════════════════════════════════════════════════════════
它解决的那个具体问题
═══════════════════════════════════════════════════════════════════════════
全库 2212 行里 **1651 行没有 `opencd_tid`**（SUCCESS 335 / NO_LOG_FILE 1316）。
`opencd_tid` 是 `descr.py` 三处「皇后路」分流的唯一开关，也是 `crawl`/`topup`
阶段「有 tid 才有页面可抓」的前提。目前 tid **只**来自 `sync_opencd_tid.py`
（读 vertex2 外库按 infohash 反查，命中 430/547）——
**vertex2 没收的种子就永远拿不到 tid**，抓不了页面、发不出或发错。

═══════════════════════════════════════════════════════════════════════════
为什么是 RSS（而不是"按 infohash 搜"）
═══════════════════════════════════════════════════════════════════════════
★ 用户原话：「一天能搜 500 个，把限制改一下；把『按 infohash 反查页面补 tid』补上。」
  但 2026-10-12 实测（`.scratch/B12-tid-reverse-lookup.md`）：
    · ❌ 皇后**搜索页没有 hash 字段**（40hex 搜返回无关空结果页）⇒「按 infohash 搜」不存在。
    · ✅ **突破口**：`GET torrentrss.php?...&search=<名字>&rows=100` 每条 item 自带
        `<guid>`   = **真 infohash**（40hex）
        `<link>`   = `https://open.cd/details.php?id=<TID>`
        `<pubDate>`= 皇后 add time
      ⇒ **1 请求**即可得「名字 → (hash, tid, 发布日期)」映射。
  命中率实测（25 条真样本）：精确名字搜 **8/25 = 32%**；
  加「模糊退避」（剥尾部年份/`[FLAC]`/`vol.N`/分轨）→ **13/25 = 52%**。
  ⇒ 两段式（先精确、不中再模糊）均摊 ≈ 1.3~1.5 请求/条。

═══════════════════════════════════════════════════════════════════════════
三条边界（守死）
═══════════════════════════════════════════════════════════════════════════
1. **纯解析在 `opencd.py`**（`parse_rss_index` / `fuzzy_name`），它是**纯函数、无网络**。
   本模块只做"出网 + 编排 + 写库"，不自己写解析、不自己写 `requests`。
2. ★★ **每个请求都过同一个 `RateGate`** —— 复用 `crawl.Client`（`get()` 是**唯一**
   发请求的出口，配额账本 `data.db:opencd_quota` 与最小间隔都在那儿）。
   绕过它 = 违规（`opencd_rate.py` 模块头成文纪律）。
3. **写库复用 `sync_opencd_tid.apply_tids()`** —— **不写第二个 writer**。
   它只填空白、绝不覆盖非空、**不碰 `updated_at`**（纪律 2/3）。
   ★ 本模块**不 import `sync_opencd_tid` 的 parse 部分**，只借它的 `build/apply_tids`
     这两个"配对 + 写"的函数 —— 因为它俩是**唯一**的 tid 写口，重造会分叉口径。

═══════════════════════════════════════════════════════════════════════════
安全属性（★ 绝不发错）
═══════════════════════════════════════════════════════════════════════════
· 命中 = RSS item 的 `guid` 与目标行 `info_hash` **逐字节相等**（local 比，case-lowered）。
· 写库只填 `opencd_tid` 空白的行（`apply_tids` 的 WHERE 兜底），非空异值**报而不写**。
· 模糊搜索**只是"换个词再搜一次"**，命中判据仍逐字节比 hash ⇒ **不因模糊而放宽**。

用法（默认**只读报告**，零请求零写库）：
    python rss_backfill_tid.py                    # 报告：会搜哪些行、发几个请求
    python rss_backfill_tid.py --apply --limit 20 # 真搜真写库（20 行）
    python rss_backfill_tid.py --apply --limit 0  # 不限（只受每日配额）
    python rss_backfill_tid.py --dry-run          # ≡ 不加 --apply（daemon 阶段会传它）
    python rss_backfill_tid.py --selftest --json  # 离线自检
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime

# ★★ 必须在 `import db`/`import config` 之前塞好环境变量（照抄 `opencd.py` 文件头那段）：
#   `db`/`config` 用 `require_env`（缺了 sys.exit），且 `config.DB_PATH` 在 import 时
#   就由 `DATA_DIR` 算出来了。★ 只在**跑自检**时把 `DATA_DIR` 指向临时目录 ——
#   这样"跑一次自检"绝不会碰到真实库（本项目对"自检不能有副作用"有成文纪律）。
#   ★ 用 `setdefault`：若调用方（生产容器）已设好，**绝不覆盖**。
if "--selftest" in sys.argv:
    import tempfile as _tempfile
    os.environ.setdefault("QB_PASS", "x")
    os.environ.setdefault("MUSOPIA_COOKIE", "c_secure_pass=x")
    os.environ.setdefault("MUSOPIA_PASSKEY", "x")
    os.environ.setdefault("PICGO_API_KEY", "x")
    os.environ["DATA_DIR"] = _tempfile.mkdtemp(prefix="rss-backfill-selftest-")

import config
import db
import opencd as opencd_parse            # ★ 纯解析（parse_rss_index / fuzzy_name / RateGate）
import crawl                            # ★ 复用 Client（出网出口）/ _DbLedger / RAW_DIR
import sync_opencd_tid                  # ★ 复用 build()/apply_tids()（唯一 tid 写口）

TRIED_FILE = "rss_tried.jsonl"

# RSS 缓存子目录：`raw_html/rss/<sha1(term)[:16]>.xml`
# ★ 为什么**不**复用 `crawl.save_raw`：它按 **tid** 命名（`raw_html/<tid>.html`），
#   而本模块的键是**搜索词** ⇒ 会与详情页缓存撞名/混淆，且两者"命中即零请求"的
#   语义不同（详情页按 tid，RSS 按词）。⇒ 独立子目录，各管各的。
RSS_SUBDIR = "rss"


def rss_dir():
    return os.path.join(crawl.RAW_DIR, RSS_SUBDIR)


def _term_key(term):
    """搜索词 → 稳定文件名（sha1 前 16 hex）。★ 明文词另存 `.meta.json`。"""
    return hashlib.sha1(term.encode("utf-8")).hexdigest()[:16]


def save_rss(term, xml, *, n_items, ok):
    """
    落盘原始 RSS + 元数据（原子替换，仿 `crawl.save_raw`）。
    ★ 这层是**故意的**：解析层永不重复请求，改版时重放即可（零请求复算）。
    """
    d = rss_dir()
    os.makedirs(d, exist_ok=True)
    key = _term_key(term)
    xp = os.path.join(d, f"{key}.xml")
    tmp = xp + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(xml)
    os.replace(tmp, xp)
    mp = os.path.join(d, f"{key}.meta.json")
    meta = {"term": term, "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "n_items": n_items, "ok": ok}
    mtmp = mp + ".tmp"
    with open(mtmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(meta, fh, ensure_ascii=False)
    os.replace(mtmp, mp)
    return xp


def load_rss(term):
    """读落盘 RSS（命中 ⇒ 零请求）。没有 ⇒ None。"""
    p = os.path.join(rss_dir(), f"{_term_key(term)}.xml")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def load_tried():
    """
    读台账（词 → 记录）。★ 读不到 = 空集，不是异常（文件本来就可能没有）。
    ★ 键是 `sha1(term)[:16]`（与缓存同名），值含明文 term 便于人工查。
    """
    p = os.path.join(crawl.RAW_DIR, TRIED_FILE)
    if not os.path.exists(p):
        return {}
    out = {}
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue                       # ★ 坏行跳过
            if "term" in o:
                out[_term_key(o["term"])] = o
    return out


def append_tried(rec):
    """追加一行台账（只增）。★ 追加写，不重写整个文件 ⇒ 崩了也不丢已记的。"""
    os.makedirs(crawl.RAW_DIR, exist_ok=True)
    p = os.path.join(crawl.RAW_DIR, TRIED_FILE)
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def select_candidates(limit, tried, retry_failed=False):
    """
    选批（**只读**）：`opencd_tid` 空白的行。

    ★ 排序（确定性，不依赖行序）：① 未在台账里试过的优先；② `status='SUCCESS'`
      （有 log、有 content_path ⇒ 可判做种，不会落 `NOT_SEEDED`）优先；
      ③ 有落盘 RSS 的优先（零请求，先把白拿的吃完）；④ `info_hash` 升序。
    ★ 返回 `(picked, skipped, n_target)`：
      · `picked`  = 本轮要处理的（≤ limit）
      · `skipped` = 台账里"试过没成"而跳过的 [(term, why)]（要重试加 --retry-failed）
      · `n_target`= 目标行总数（用于对照）
    """
    conn = db.connect()
    try:
        cur = conn.execute(
            "SELECT info_hash, torrent_name, status, opencd_tid "
            "FROM tasks WHERE opencd_tid IS NULL OR opencd_tid = ''")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur]
    finally:
        conn.close()

    cached = set()
    if os.path.isdir(rss_dir()):
        cached = {f[:-4] for f in os.listdir(rss_dir()) if f.endswith(".xml")}

    n_target = len(rows)
    picked, skipped = [], []
    for r in rows:
        term = (r.get("torrent_name") or "").strip()
        if not term:
            continue                           # 没名字 ⇒ 搜不了（记进 absent 之外）
        key = _term_key(term)
        rec = tried.get(key)
        if rec is not None and not rec.get("ok") and not retry_failed:
            skipped.append((term, rec.get("why", "试过没成")))
            continue
        r["_term"] = term
        r["_cached"] = key in cached
        picked.append(r)

    # ★ 排序：缓存优先（零请求）> SUCCESS 优先 > info_hash 稳定序
    picked.sort(key=lambda r: (not r["_cached"],
                               r.get("status") != "SUCCESS",
                               r["info_hash"]))
    if limit and limit > 0:
        picked = picked[:limit]
    return picked, skipped, n_target


class FetchResult:
    """★ 三态：成功(ok=True) / 失败(ok=False, why=…)。★ 空结果与失败**必须分开**
    （`ERR-META-02`：把"零命中"当成"请求失败"会让人误判站点挂了）。"""

    def __init__(self, ok, idx=None, stats=None, why="", from_cache=False,
                 n_requests=0):
        self.ok = ok
        self.idx = idx or {}
        self.stats = stats or {}
        self.why = why
        self.from_cache = from_cache
        self.n_requests = n_requests


def fetch_index(client, term):
    """
    取一个搜索词的 RSS 索引（**cache-first**：命中磁盘 ⇒ 零请求）。

    ★ 请求走 `crawl.Client.get()`（唯一出口，过 RateGate）。参数与实测一致：
      `?linktype=dl&rows=100&search=<term>&search_area=0&search_mode=0`
      ★ `passkey` 由 `crawl.Client` 的 base/cookie 体系处理 —— 这里**不**碰凭据；
        `torrentrss.php` 实测**不要求登录 cookie**（用 passkey 即可），
        但走同一个 session 无妨，且 cookie 在则由 Client 自动带上。
    """
    cached = load_rss(term)
    if cached is not None:
        try:
            idx, stats = opencd_parse.parse_rss_index(cached)
            return FetchResult(True, idx, stats, from_cache=True, n_requests=0)
        except Exception:                     # ★ 落盘坏了 ⇒ 当作没缓存，重抓
            pass

    params = {"linktype": "dl", "rows": 100, "search": term,
              "search_area": 0, "search_mode": 0}
    r = client.get("/torrentrss.php", **params)
    if r.status_code != 200:
        # ★ 失败 ⇒ **不落盘**（否则"失败"会被缓存成"零命中"，`ERR-META-02`）
        return FetchResult(False, why=f"HTTP {r.status_code}", n_requests=1)
    try:
        idx, stats = opencd_parse.parse_rss_index(r.text)
    except Exception as e:                    # XML 坏了 ⇒ 也当失败，不落盘
        return FetchResult(False, why=f"parse:{type(e).__name__}", n_requests=1)
    # ★ 空但成功（零命中）⇒ **要落盘**（这是有效结果，别每次重搜）
    save_rss(term, r.text, n_items=stats.get("items", 0), ok=True)
    return FetchResult(True, idx, stats, n_requests=1)


def _match_rows(picked, idx, info_hash):
    """
    用 `sync_opencd_tid.build` 对**单行**判配对 —— ★ 不重造配对逻辑。
    返回 `build()` 的 `todo` 项（命中即非空）。
    """
    rows = [(info_hash, "", "")]
    todo, same, diff, miss = sync_opencd_tid.build(rows, idx)
    return todo


def main():
    ap = argparse.ArgumentParser(
        description="用皇后 RSS 反查补 tasks.opencd_tid（按名字搜 → 逐字节比 hash）")
    ap.add_argument("--apply", action="store_true",
                    help="★ 真搜真写库。不加 = 只读报告（默认）")
    ap.add_argument("--dry-run", action="store_true",
                    help="≡ 不加 --apply（daemon 阶段会传它）。显式提供以免 arg 报错。")
    ap.add_argument("--limit", type=int, default=5,
                    help="本轮最多处理几行（默认 **5**；0 = 不限，只受每日配额约束）。"
                         "★ 一行 ≈ 1~2 个皇后请求。")
    ap.add_argument("--no-fuzzy", action="store_true",
                    help="只做精确搜索，不做模糊退避（默认会做）")
    ap.add_argument("--retry-failed", action="store_true",
                    help="把台账里「试过但没成」的也重新排队（默认跳过它们）")
    ap.add_argument("--selftest", action="store_true", help="离线自检")
    ap.add_argument("--json", nargs="?", const="-", metavar="PATH",
                    help="把结果以 JSON 输出（不带路径=打到末行）")
    args = ap.parse_args()

    if args.selftest:
        return _selftest(as_json=args.json)

    apply = args.apply and not args.dry_run       # ★ --dry-run 显式压过 --apply

    db.init_db()
    print("=" * 74)
    print("★ RSS 反查补 opencd_tid（名字 → (hash, tid) · 每个请求过 RateGate）")
    print("=" * 74)
    print(f"  落盘目录  : {rss_dir()}")
    print(f"  模式      : {'★ 真搜真写库' if apply else '只读报告（不发请求、不写库）'}")
    print(f"  本轮上限  : {args.limit if args.limit else '不限（只受每日配额约束）'}")
    print(f"  模糊退避  : {'关（--no-fuzzy）' if args.no_fuzzy else '开（精确不中再模糊）'}")

    tried = load_tried()
    picked, skipped, n_target = select_candidates(args.limit, tried, args.retry_failed)
    print(f"\n  目标行数  : {n_target}（口径 = opencd_tid 为空）")
    print(f"  台账已试  : {len(tried)}（本轮跳过 {len(skipped)} 条）")
    print(f"  ★ 本轮选中: {len(picked)}")

    if skipped:
        print("\n  ★ 台账里「试过没成」而跳过的（要重试加 --retry-failed）：")
        for term, why in skipped[:8]:
            print(f"     {term[:44]:<44} {why[:40]}")
        if len(skipped) > 8:
            print(f"     ... 另有 {len(skipped) - 8} 条")

    if not picked:
        print("\n💡 没有要搜的（目标已清空 / 全在台账里）⇒ 什么都不做。")
        return 0

    print("\n" + "-" * 74)
    print("① 本轮会处理的（逐条）")
    print("-" * 74)
    fuzzy_plan = []
    for i, r in enumerate(picked, 1):
        term = r["_term"]
        fz = "" if args.no_fuzzy else opencd_parse.fuzzy_name(term)
        fuzzy_plan.append((term, fz))
        free = "★ 零请求（已缓存）" if r["_cached"] else "1~2 请求"
        print(f"  [{i:>3}] {free:<18} {(term or '')[:46]}")
        print(f"        {r['info_hash'][:16]}  status={r['status']}")
        if fz and fz != term:
            print(f"        模糊档: {fz[:50]}")

    if not apply:
        print("\n" + "=" * 74)
        print("★ 只读报告到此（**没发任何请求、没写库**）。要真跑：")
        _argv = ["python", "rss_backfill_tid.py", "--apply"]
        if args.no_fuzzy:
            _argv.append("--no-fuzzy")
        if args.retry_failed:
            _argv.append("--retry-failed")
        _argv.append(f"--limit {args.limit}")
        print("    " + " ".join(_argv))
        print("=" * 74)
        return 0

    # ── 真搜 ────────────────────────────────────────────────────────────────
    day_key = time.strftime("%Y-%m-%d")
    ledger = crawl._DbLedger()
    gate = opencd_parse.RateGate(
        max_per_round=int(os.getenv("OPENCD_MAX_PER_ROUND",
                                    crawl.DEFAULT_MAX_PER_ROUND)),
        min_interval=float(os.getenv("OPENCD_MIN_INTERVAL",
                                     crawl.DEFAULT_MIN_INTERVAL)),
        max_per_day=int(os.getenv("OPENCD_MAX_PER_DAY",
                                  crawl.DEFAULT_MAX_PER_DAY)),
        day_key=day_key, log=crawl.log, ledger=ledger)
    gate.roll_day(day_key)

    # ★★ 配额满 ⇒ 开工前优雅退出（**保护，不是错误**），照抄 topup 的护栏。
    if gate.used_day >= gate.max_per_day:
        print(f"\n🛑 今日请求额度已用完（{gate.used_day}/{gate.max_per_day}）"
              f"—— **这是保护，不是错误**。明天再跑同一条命令即可。")
        return 0

    client = crawl.Client(gate=gate, dry_run=False)

    n_req = n_hit = n_miss = n_fail = n_blank = 0
    adopted = []
    quota_hit = False

    print("\n" + "-" * 74)
    print("② 逐条：精确搜 → (未中) 模糊搜 → 逐字节比 hash")
    print("-" * 74)
    for i, r in enumerate(picked, 1):
        info_hash = r["info_hash"]
        term = r["_term"]
        fz = fuzzy_plan[i - 1][1]
        print(f"\n[{i}/{len(picked)}] {term[:50]}")
        try:
            res = fetch_index(client, term)
            n_req += res.n_requests
        except (opencd_parse.DailyQuotaExceeded, Exception) as e:
            if isinstance(e, opencd_parse.DailyQuotaExceeded):
                print(f"    🛑 {e} —— 本轮提前收工（这是**保护**，不是错误）")
                quota_hit = True
                break
            n_fail += 1
            print(f"    ❌ 请求失败：{type(e).__name__}: {str(e)[:100]}")
            continue

        if not res.ok:
            n_fail += 1
            print(f"    ❌ 抓取失败：{res.why}")
            append_tried({"term": term, "ok": False, "why": res.why,
                          "at": datetime.now().isoformat(timespec="seconds")})
            continue
        src = "📦 缓存（零请求）" if res.from_cache else "1 请求"
        print(f"    {src} · items={res.stats.get('items', 0)} "
              f"usable={res.stats.get('usable', 0)}")

        todo = _match_rows(picked, res.idx, info_hash)
        if todo:
            n_hit += 1
            print(f"    ✅ 精确命中 hash={info_hash[:16]}… → tid={todo[0][1]}"
                  f" added={todo[0][2]}")
        elif fz and fz != term:
            # ★ 模糊第二段（只在精确未中、且模糊词不同时才发）
            try:
                res2 = fetch_index(client, fz)
                n_req += res2.n_requests
            except opencd_parse.DailyQuotaExceeded as e:
                print(f"    🛑 {e} —— 本轮提前收工（**保护**，非错误）")
                quota_hit = True
                break
            except Exception as e:            # noqa: BLE001
                n_fail += 1
                print(f"    ❌ 模糊请求失败：{type(e).__name__}")
                continue
            if res2.ok:
                print(f"    ↳ 模糊档 {fz[:40]} · items={res2.stats.get('items', 0)}")
                todo = _match_rows(picked, res2.idx, info_hash)
            if todo:
                n_hit += 1
                print(f"    ✅ 模糊命中 hash={info_hash[:16]}… → tid={todo[0][1]}")
        if todo:
            # ★ 写库走唯一写口：只填空白、不碰 updated_at
            sync_opencd_tid.apply_tids(todo)
            adopted.append({"info_hash": info_hash, "opencd_tid": todo[0][1],
                            "opencd_added_at": todo[0][2], "term": term})
            append_tried({"term": term, "ok": True, "tid": todo[0][1],
                          "at": datetime.now().isoformat(timespec="seconds")})
        else:
            n_miss += 1
            print(f"    ○ 未命中（RSS 里没有这个 hash）")
            append_tried({"term": term, "ok": False, "why": "no_hit",
                          "at": datetime.now().isoformat(timespec="seconds")})

    print("\n" + "=" * 74)
    print(f"📊 本轮：请求 {n_req} · 命中 {n_hit} · 未中 {n_miss} · 失败 {n_fail}"
          + (" · 🛑配额截断" if quota_hit else ""))
    print(f"   今日已用 {gate.used_day}/{gate.max_per_day}（请求量纲）")
    # ★★ 「配额截断」与「没活了」**分开报**（`ERR-META-05`：不许混着减）
    if quota_hit:
        print("   ★ 本轮因**配额**提前收工（不是没活了）⇒ 明天接着跑即可。")
    elif n_hit == 0 and n_miss > 0:
        print("   ★ 本轮全未中 —— 可能是名字差异大（下一步可考虑更激进的退化档）。")
    if adopted:
        print("   ★ 已写回 opencd_tid（只填空白，未碰 updated_at）：")
        for a in adopted[:10]:
            print(f"     {a['info_hash'][:16]}… → tid={a['opencd_tid']}"
                  f" added={a['opencd_added_at']}")
        if len(adopted) > 10:
            print(f"     ... 另有 {len(adopted) - 10} 条")

    if args.json:
        payload = {"requests": n_req, "hit": n_hit, "miss": n_miss,
                   "failed": n_fail, "quota_hit": quota_hit,
                   "used_day": gate.used_day, "max_per_day": gate.max_per_day,
                   "adopted": adopted}
        if args.json == "-":
            print(json.dumps(payload, ensure_ascii=False))
        else:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            print(f"💾 已写出：{args.json}")
    return 0


def _selftest(as_json=None):
    """
    离线自检：**纯解析层**的口径已在 `opencd.py parse --selftest` 里；
    这里再钉**本编排层**的两条契约：
      ① 缓存键稳定（同词同 key；异词异 key）；② `_match_rows` 只认逐字节等于的 hash。
    ★ **不发任何请求**（用喂进 `parse_rss_index` 的人造 XML）。
    """
    xml = ("<rss><channel>"
           "<item><link>https://open.cd/details.php?id=777</link>"
           "<guid>%s</guid><title>T</title>"
           "<pubDate>Mon, 12 Oct 2026 03:29:25 +0800</pubDate></item>"
           "</channel></rss>" % ("c" * 40))
    idx, stats = opencd_parse.parse_rss_index(xml)
    # 命中：目标 hash == item hash
    hit = _match_rows([], idx, "C" * 40)          # 大小写不同也应命中
    # 未命中：换了 hash
    miss = _match_rows([], idx, "d" * 40)
    checks = [
        ("同名同 key", _term_key("群星 - X") == _term_key("群星 - X")),
        ("异名异 key", _term_key("群星 - X") != _term_key("群星 - Y")),
        ("★ 命中：hash 逐字节等（大小写不敏感）", len(hit) == 1),
        ("命中 tid 正确", hit and hit[0][1] == "777"),
        ("★ 未命中：hash 不等 ⇒ 不写", len(miss) == 0),
    ]
    fails = 0
    if as_json:
        for label, ok in checks:
            fails += 0 if ok else 1
        print(json.dumps({"script": "rss_backfill_tid.py --selftest",
                          "ok": len(checks) - fails, "total": len(checks),
                          "exit": 1 if fails else 0}, ensure_ascii=False))
        return 1 if fails else 0
    for label, ok in checks:
        fails += 0 if ok else 1
        print(f"    {'✅' if ok else '❌'} {label}")
    print(f"{'✅ 自检通过' if not fails else '❌ 自检失败'}：{len(checks) - fails}/{len(checks)} 项")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
