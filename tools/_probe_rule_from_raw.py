"""★ 只读：把「皇后頁面 → `transfer_rule`」这一列**对着真页面**逐字段拆开看。

═══════════════════════════════════════════════════════════════════════════
为什么需要它
═══════════════════════════════════════════════════════════════════════════
用户（2026-09-23）决定：**248 条要标 `ALLOW`**（那批是 `status='SUCCESS'`、
有 `log_content`、`opencd_tid` 非空、`mapped_json` 空的行）。

但 `ALLOW` **不能平白无故打上去** —— `opencd_parse.RULE_ALLOW` 的语义是
「页面上的『轉載規則』字段**逐字**写着 `歡迎轉載`」(`opencd_parse.py:50`)。
⇒ 所以先要回答一件事：**那 248 条页面上到底写的是什么？**

本脚本**不猜**：它取一批行 → 按 `tid` 从**容器落盘的 `raw_html/<tid>.html`** 读真页面
（没有就明说"没落盘"，**不发请求**）→ 跑真解析器 `opencd_parse.parse_detail()` →
按三态分堆。

★★ 三条纪律（同 `tools/backfill_opencd_tid.py`）
  1. **纯本地零请求** —— 不 import `requests` / `crawl`，只读盘上的 HTML。
  2. **不写任何东西** —— 连 `db.connect()` 都不调，用 `sqlite3` + `query_only=1`。
  3. **取不到就说取不到** —— 没落盘的行单独报一堆，绝不当成 `NEED_CONFIRM`。
     （★ 把它们混进三态会让"没抓页面"长得和"页面说拿不准"一模一样 —— `ERR-META-01`。）

★★★ 承重判据：**只有真的读得出「歡迎轉載」才该标 `ALLOW`**。
   本脚本的任务就是把这个数**算出来**，而不是把 248 当成一个已经成立的结论。

用法（本机；只读）:
    set DATA_DIR=//iSunker-DS423/docker_ssd/opencd2others/data
    python tools/_probe_rule_from_raw.py                    # 汇总（默认）
    python tools/_probe_rule_from_raw.py --json out.json    # 连逐条结果一起存
"""
import argparse
import collections
import json
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

# ★ 必须**在 import 项目模块之前**把环境变量塞好：`config.require_env` 缺了会 sys.exit。
#   用 setdefault ⇒ 调用方（生产容器）已设好的**绝不覆盖**。
os.environ.setdefault("QB_PASS", "x")
os.environ.setdefault("MUSOPIA_COOKIE", "c_secure_pass=x")
os.environ.setdefault("MUSOPIA_PASSKEY", "x")
os.environ.setdefault("PICGO_API_KEY", "x")

LOCAL_DB = r"//iSunker-DS423/docker_ssd/opencd2others/data/data.db"
# ★ 落盘目录的真值来自 `crawl.RAW_DIR`（= `OPENCD_RAW_DIR` 或 `${DATA_DIR}/raw_html`）。
#   配置：本机跑时把 `OPENCD_RAW_DIR` 指到 NAS 上的那一个。
LOCAL_RAW = os.path.join(os.environ.get("DATA_DIR", ""), "raw_html")


def load_targets(db_path, limit=0):
    """
    ★ 目标集合的口径**与用户的决定逐字对齐**（不自己发明）：
        `status='SUCCESS'`  ⇒  ★ 唯一一批**有 `log_content`** 的（248 条）
        `opencd_tid` 非空    ⇒  真的有皇后页面可读
        `mapped_json` 空     ⇒  还没补过（补过的 2 条不在内）
      ★ 按 `status='SUCCESS'` 筛而不是 `log_content <> ''`：
        两条判据在库里**实测等价**（248 vs 248），而 `status` 是**免费的**
        （不用把 248 份日志读出来比字符串）。
    """
    con = sqlite3.connect(db_path, timeout=60)
    con.execute("PRAGMA query_only=1")
    con.row_factory = sqlite3.Row
    sql = """SELECT info_hash, opencd_tid, status, site_status, torrent_name
               FROM tasks
              WHERE status = 'SUCCESS'
                AND (opencd_tid IS NOT NULL AND opencd_tid <> '')
                AND (mapped_json IS NULL OR mapped_json = '')
              ORDER BY opencd_tid"""
    rows = [dict(r) for r in con.execute(sql)]
    con.close()
    return rows[:limit] if limit else rows


def main():
    ap = argparse.ArgumentParser(description="只读：从落盘的真页面回算 transfer_rule")
    ap.add_argument("--db", default=LOCAL_DB)
    ap.add_argument("--raw", default=LOCAL_RAW)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", dest="out")
    args = ap.parse_args()

    print("=" * 78)
    print("★ 只读：把 248 条的 transfer_rule 对着**真页面**算出来（不发任何请求）")
    print("=" * 78)
    print(f"  库        : {args.db}")
    print(f"  页面落盘  : {args.raw}")

    rows = load_targets(args.db, args.limit)
    print(f"  目标行数  : {len(rows)}（口径 status='SUCCESS' + tid 非空 + mapped 空）")

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import opencd as P


    if not os.path.isdir(args.raw):
        print("\n" + "!" * 78)
        print(f"★ 落盘目录不存在：{args.raw}")
        print("  ⇒ 一个页面都读不到。这**不是**结论，是「工具没接上源」。")
        print("  ★ 解法：把 OPENCD_RAW_DIR / DATA_DIR 指到 NAS 上那个 raw_html，")
        print("    或先用 crawl.py 把页面抓下来落盘。")
        print("!" * 78)
        return 2

    have = set(f[:-5] for f in os.listdir(args.raw) if f.endswith(".html"))
    print(f"  落盘页面数: {len(have)}")

    # ---- 逐条回算 ----
    tri = collections.Counter()          # ALLOW / DENY / NEED_CONFIRM
    no_file = []                         # ★ 没落盘 ⇒ **不猜**，单独一堆
    detail = []
    for r in rows:
        tid = str(r["opencd_tid"])
        path = os.path.join(args.raw, f"{tid}.html")
        if tid not in have or not os.path.exists(path):
            no_file.append(r)
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            info = P.parse_detail(fh.read(), source_file=f"{tid}.html")
        raw = info.get("transfer_rule_raw") or ""
        rule = info.get("transfer_rule") or P.RULE_UNKNOWN
        tri[rule] += 1
        detail.append({"tid": tid, "info_hash": r["info_hash"],
                       "site_status": r["site_status"],
                       "rule_raw": raw, "rule": rule,
                       "name": r["torrent_name"]})

    print("\n" + "-" * 78)
    print("① ★ 真页面算出来的三态分布（这是**结论**）")
    print("-" * 78)
    for k in (P.RULE_ALLOW, P.RULE_DENY, P.RULE_UNKNOWN):
        print(f"   {k:<14} {tri[k]}")
    print(f"   {'★ 没落盘':<14} {len(no_file)}   ← 这一堆**不算进任何一态**")
    print(f"   {'合计':<14} {sum(tri.values()) + len(no_file)}")

    print("\n" + "-" * 78)
    print("② ★ `transfer_rule_raw` 的**原文**分布（看 ALLOW 是怎么来的）")
    print("-" * 78)
    raw_counter = collections.Counter(d["rule_raw"] for d in detail)
    for v, n in raw_counter.most_common():
        print(f"   {v!r:<20} n={n}")

    print("\n" + "-" * 78)
    print("③ ★★ 承重判据：这三态**是不是真从页面读出来的**（抽样逐字段回显）")
    print("-" * 78)
    for want in (P.RULE_ALLOW, P.RULE_DENY, P.RULE_UNKNOWN):
        sample = [d for d in detail if d["rule"] == want][:2]
        for d in sample:
            print(f"   tid={d['tid']:<8} rule={d['rule']:<14} "
                  f"raw={d['rule_raw']!r:<12} {(d['name'] or '')[:40]}")

    if trans := [d for d in detail if d["rule"] == P.RULE_UNKNOWN]:
        print(f"\n   ★ 其中 NEED_CONFIRM 的（页面规则字段缺失或认不出的）样例：")
        for d in trans[:6]:
            print(f"      tid={d['tid']:<8} raw={d['rule_raw']!r:<12} {(d['name'] or '')[:44]}")

    if no_file:
        print(f"\n   ★ 没落盘的样例（**这些行本次拿不到结论**）：")
        for r in no_file[:8]:
            print(f"      tid={r['opencd_tid']:<8} {(r['torrent_name'] or '')[:50]}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"counts": dict(tri), "no_file": no_file,
                       "detail": detail}, fh, ensure_ascii=False, indent=2)
        print(f"\n   逐条结果 → {args.out}")

    print("\n" + "=" * 78)
    print("★ 只读预览到此（没写任何东西）。")
    print("  ★★ 若 ① 里 `ALLOW` 的条数 < 248，那 `ALLOW` **不能**按 248 打 ——")
    print("     只有页面逐字写着『歡迎轉載』的才该标 ALLOW，其余要你定。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
