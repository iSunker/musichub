"""★ 只读：把一批皇后行的**副标题源头**一次性查清（`smalltitle` 到底空不空）。

═══════════════════════════════════════════════════════════════════════════
它要回答的那个问题（2026-09-26 用户问的）
═══════════════════════════════════════════════════════════════════════════
现象：**站点页面上，有些种子的「副标题」栏是空的。**

库里查实（纯读库，已定案）——**站上且副标题空着的只有 4 条**：

    sid=17264  opencd_tid=198601  黄清元 - 金钻名曲珍藏
    sid=17265  opencd_tid=198827  群星 - 醉民乐
    sid=17294  opencd_tid=199963  群星 - 爱是我们共同的声音
    sid=15974  opencd_tid=200028  齐秦 - 我拿什么爱你

★ 这 4 条的 `tasks.small_descr` **和** `mapped_json.small_descr` **都是空串**。
★ ⇒ 若 bug 在"没回写"，那**该有快照**；两处都空 ⇒ **嫌疑指向源头**（解析层就没拿到）。

★★ 而 `descr.py:314` 的注释**早点点名了齐秦那条**：
     「齐秦那条**本来就是空的**（`small_descr_present=False`）⇒ 留空，不硬造」
★ ⇒ 假设：**这 4 条不是 bug，是「留空不硬造」的刻意行为**（`descr.opencd_small_descr`）。

═══════════════════════════════════════════════════════════════════════════
★ 为什么必须实地查，不能靠推理（我今晚已经栽过 4 次）
═══════════════════════════════════════════════════════════════════════════
「皇后面本来就没写副标题」这件事，**唯一权威判据是皇后那个页面本身的
`<div class="smalltitle">` 内容**。库里两处都空**只是"我们没拿到"的证据**，
它**不能区分**：
    (a) 皇后面**确实没写**          ⇒ 空是对的（留空不硬造）
    (b) 页面**有写、我们没解析到**  ⇒ 是真 bug（在 `opencd_parse` 里）
★ 两者修法**完全相反**：(a) 什么都不能做（硬造 = 往站上推假信息），
  (b) 要修解析层。**⇒ 所以必须看页面**（`ERR-META-01`：判据要先验一次）。

═══════════════════════════════════════════════════════════════════════════
★★ 配额：这脚本**花 OPENCD_MAX_PER_DAY 的额度**（默认 50/天，共享同一个账本）
═══════════════════════════════════════════════════════════════════════════
★ 每个 tid = **1 个 GET**，且**过 `RateGate`**（与 `crawl.py` 同一个出口纪律、
  同一个 `data.db:opencd_quota` 账本 —— 不是另开一套）。
★★ 所以：**配额用完时它会直接抛 `DailyQuotaExceeded`**（那是闸门在正常工作，
  不是脚本坏了）。⇒ 跑之前先数清要几个请求，别把一天 50 次额度一次烧光。

★ 顺序很重要（本脚本刻意这么排）：**先花请求、后查库**。
  因为库是本地免费的，页面是稀缺的；先查库再发现"该查的没查"就白跑了。

═══════════════════════════════════════════════════════════════════════════
只读保证
═══════════════════════════════════════════════════════════════════════════
- 站点侧：**只发 GET**（`/plugin_details.php`），**过 RateGate**。
- 库侧：`PRAGMA query_only=1`，**只 SELECT**（只为算出"该查哪些 tid"）。
- 落盘：默认**不落盘**（`--save` 才落，落 `crawl.save_raw` 那个位置 ⇒
  后续 crawl 能复用、**省一个请求**）。

用法（在 NAS 上，compose 目录）:
    # ① 先看要花几个请求（★ 不联网、不花配额）
    sudo docker compose run --rm tools python /data/tools/probe_subtitles.py --list

    # ② 真查（每个 tid 1 个 GET；配额满会明确报 DailyQuotaExceeded）
    sudo docker compose run --rm tools python /data/tools/probe_subtitles.py

    # ③ 只查点名的几个（逗号分隔的 opencd_tid）
    sudo docker compose run --rm tools python /data/tools/probe_subtitles.py --tids 198601,198827
"""
import argparse
import os
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.environ.get("MUSOPIA_APP_DIR", "/app"))

import config        # noqa: E402
import crawl         # noqa: E402  ★ 用它的 Client / _make_gate / _DbLedger / save_raw
import opencd as opencd_parse  # noqa: E402  ★ 解析口径**只此一份**，不自己写


# ── 「站上且副标题空」的目标口径 ──────────────────────────────────────────
#
# ★ 这是**库里已定案**的那个集合，不是新猜的：
#     site_status='EXISTS'（真的在站上）∧ 是皇后行（有 opencd_tid）
#     ∧ mapped_json.small_descr 为空（快照里没副标题）
#
# ★★ 为什么用 `json_valid()` 守卫（照抄 `recall_opencd_descr.py` 的同族写法）：
#    `json_extract` 遇到**坏 JSON** 会抛错，把整条查询带崩 ⇒ 那会让
#    "读不到"变成"脚本炸了"，而不是"如实报出哪几行坏"（`ERR-META-01`）。
TARGET_SQL = """
SELECT opencd_tid, site_torrent_id, status, substr(torrent_name, 1, 40) AS name
  FROM tasks
 WHERE site_status = 'EXISTS'
   AND opencd_tid IS NOT NULL AND opencd_tid != ''
   AND (mapped_json IS NULL OR mapped_json = ''
        OR json_valid(mapped_json) = 0
        OR json_extract(mapped_json, '$.small_descr') IS NULL
        OR json_extract(mapped_json, '$.small_descr') = '')
 ORDER BY site_torrent_id
"""


def load_targets(db_path, tids):
    """算出"该查哪些 opencd_tid"。★ 只读、纯 SELECT。"""
    con = sqlite3.connect(db_path, timeout=30)
    con.execute("PRAGMA query_only=1")            # ★ 本脚本对库只读，永远
    try:
        rows = list(con.execute(TARGET_SQL))
    finally:
        con.close()
    if tids:
        want = {str(t) for t in tids}
        rows = [r for r in rows if str(r[0]) in want]
    return rows


def make_gate_and_client():
    """★ 建 `RateGate` + `Client` —— **逐行照抄** `_probe_queen_page.py` 的做法。

    ★★ 为什么不自己写：配额/间隔/账本三者必须与 `crawl.py` **同一套**，
      否则会绕过配额（`ERR-META-03`：同一判据两处实现必然漂移）。
    ★ 关键：必须传 `ledger=crawl._DbLedger()` —— `crawl.py:224-235` 专门为此
      删掉了兜底并抛 ValueError，就是为了防"跨进程配额失效"。
    """
    gate = crawl._make_gate() if hasattr(crawl, "_make_gate") else None
    if gate is None:
        day_key = time.strftime("%Y-%m-%d")
        gate = crawl.opencd_rate.RateGate(
            max_per_round=int(os.getenv("OPENCD_MAX_PER_ROUND", "20")),
            min_interval=float(os.getenv("OPENCD_MIN_INTERVAL", "120")),
            max_per_day=int(os.getenv("OPENCD_MAX_PER_DAY", "50")),
            day_key=day_key,
            log=lambda m: None,
            ledger=crawl._DbLedger(),
        )
        gate.roll_day(day_key)
    return gate, crawl.Client(gate=gate)


def probe_one(client, tid, save=False):
    """查一个 tid。返回 (verdict_key, dict) —— **不打印**（打印在 main 里统一排）。"""
    r = client.get("/plugin_details.php", **{"id": tid, "hit": 1})
    html, code = r.text, r.status_code

    if "takelogin" in html or "登錄" in html[:4000]:
        return "login", {"tid": tid, "http": code, "bytes": len(html)}

    if save:
        crawl.save_raw(tid, html)   # ★ 与 crawl.py 同目录同命名 ⇒ 下次能复用省请求

    info = opencd_parse.parse_detail(html, source_file=f"{tid}.html")
    return "ok", {
        "tid": tid,
        "http": code,
        "bytes": len(html),
        # ★★ 这两个键就是判据（见模块头）：
        #    `small_descr`         = 解析出的副标题原文
        #    `small_descr_present` = **页面到底有没有这个字段**（区分"真没有" vs "没取到"）
        "small_descr": info.get("small_descr"),
        "present": info.get("small_descr_present"),
        "name": info.get("name"),
        "missing": info.get("missing") or [],
    }


def read_quota_used(gate=None):
    """今天用了多少次请求 —— **从账本读**（`data.db:opencd_quota`）。

    ★★ 为什么不从 `gate` 上读：`RateGate` 的公开面里没有"今天用了多少"这个方法
      （我原先 `hasattr(gate,'used_today')` 的写法恒假 ⇒ 恒印 `?`）。
      **账本才是权威**（`crawl._DbLedger` 写进去的就是它）。
    ★ 读不到 ⇒ 返回 `None`，**绝不当成 0**（`ERR-SQL-05`）。
    """
    try:
        con = sqlite3.connect(config.DB_PATH, timeout=30)
        con.execute("PRAGMA query_only=1")
        try:
            r = con.execute(
                "SELECT used FROM opencd_quota WHERE day_key = ? AND kind = 'request'",
                (time.strftime("%Y-%m-%d"),)).fetchone()
            return r[0] if r else 0
        finally:
            con.close()
    except sqlite3.Error:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="★ 只读：查皇后页面的 smalltitle，定位「站上副标题空着」的真因")
    ap.add_argument("--list", action="store_true",
                    help="★ 只列举要查哪些 tid 和要花几个请求（**不联网、不花配额**）")
    ap.add_argument("--tids", default="",
                    help="只查这些 opencd_tid（逗号分隔）；不给 = 查全部目标行")
    ap.add_argument("--save", action="store_true",
                    help="把页面落盘到 crawl.save_raw 的位置（后续 crawl 可复用、省请求）")
    ap.add_argument("--db", default=config.DB_PATH)
    args = ap.parse_args(argv)

    tids = [s.strip() for s in args.tids.split(",") if s.strip()]
    rows = load_targets(args.db, tids)

    print("=" * 74)
    print("★ 查皇后页面的 smalltitle —— 「站上副标题空着」的真因")
    print("=" * 74)
    print(f"  库     = {args.db}")
    print(f"  皇后   = {os.getenv('OPENCD_URL', 'https://open.cd')}")
    print(f"  有 Cookie = {'是' if os.getenv('OPENCD_COOKIE') else '★ 否（会被踢到登录页）'}")
    print()
    print("【目标】站上（site_status=EXISTS）∧ 皇后行 ∧ 快照里没有副标题")
    print(f"  共 {len(rows)} 条 ⇒ ★ 真查要花 **{len(rows)} 个请求**"
          f"（各 1 个 GET，过 RateGate）")
    if not rows:
        print("  （没有匹配的行 —— 说明站上已经没有'副标题空着'的皇后行）")
        return 0
    for t, sid, st, nm in rows:
        print(f"    opencd_tid={t:<8} sid={sid:<7} {st:<16} {nm}")
    print()

    if args.list:
        print("💡 --list：**没有联网、没有花配额**。去掉 --list 才会真查。")
        return 0

    gate, client = make_gate_and_client()
    results, failures = [], []
    try:
        for i, (t, sid, st, nm) in enumerate(rows, 1):
            print(f"  ▶ [{i}/{len(rows)}] opencd_tid={t}  {nm}")
            try:
                kind, d = probe_one(client, t, save=args.save)
            except Exception as e:                          # noqa: BLE001
                # ★ 配额满/网络错 —— **如实报出是哪一类**，不许静默跳过
                #   （`DailyQuotaExceeded` 是**闸门在正常工作**，不是脚本坏）
                kind, d = "error", {"tid": t, "err": f"{type(e).__name__}: {e}"}
            if kind == "ok":
                results.append(d)
                print(f"      HTTP={d['http']} {d['bytes']}字节  "
                      f"smalltitle={d['small_descr']!r}  present={d['present']}")
            elif kind == "login":
                failures.append(("login", d))
                print(f"      ❌ 被踢到登录页 ⇒ Cookie 不对/失效（停止）")
                break
            else:
                failures.append((kind, d))
                print(f"      ★★ {d['err']}")
                if "Quota" in d["err"]:
                    print("          ⇒ ★ 当日配额用完 —— 这是 **RateGate 在正常工作**，"
                          "不是脚本坏")
                    print("          ⇒ 明天（本地日期换天后）自动重置；"
                          "账本在 data.db:opencd_quota")
                    break
    finally:
        # ★ 报配额：**从账本读真值**（`data.db:opencd_quota`），不猜方法名。
        #   ★ 原先那行写的是 `gate.used_today() if hasattr(...) else '?'` ——
        #     `RateGate` **没有**这个方法 ⇒ 恒印 `?`，等于**没报**
        #     （又一个"看着像报了、其实什么都没说"）。dev. 判据：读库。
        try:
            print()
            _u = read_quota_used(gate)
            if _u is None:
                print("  配额：❔ **读不到**（账本不在/读失败）⇒ 不当成 0")
            else:
                print(f"  配额：今天已用 {_u} / "
                      f"{os.getenv('OPENCD_MAX_PER_DAY', '50')}"
                      f"   (账本 = data.db:opencd_quota, day_key=本地日期)")
        except Exception:                                   # noqa: BLE001
            pass

    # ══════════════════════════════════════════════════════════════════
    # ★★ 定案：把每条归到 (a) 还是 (b) —— **判据由本次读数算出**，
    #    不是模板句（`ERR-META-11`：脚本里写死的结论不算证据）。
    # ══════════════════════════════════════════════════════════════════
    print()
    print("=" * 74)
    print("★★ 定案（判据 = 本次实测的 `smalltitle` + `present`）")
    print("=" * 74)
    # ★★★ 空集守卫（2026-09-26 当晚实测踩到，**本脚本自己犯的**）：
    #   第一次真跑时配额已满 ⇒ 4 条一条都没查成 ⇒ `results` 是**空列表**，
    #   而它照样打印了「⇒ (a) 留空是对的 : 0 条　(b) 真 bug : 0 条
    #              ⇒ ★★ 全部是 (a) ⇒ 站上那几条空副标题不是 bug」
    #   ⇒ ★★ **在 0 个样本上宣布了一个全称结论** —— 这正是
    #      `ERR-META-11`（脚本里写死的结论不算证据）与 `ERR-META-14`
    #      （"来源为空"不许印成"映射成功"）的**同一个形状**，而这次是
    #      **我的脚本自己在犯**：`0 == 0` 让"全部是 (a)"看着成立。
    #   ⇒ 修法：**样本数为 0 时，一个字的结论都不许下。**
    if not results:
        print(f"  ❌ **本次一条都没查成** ⇒ ★ 样本数 = 0，**什么结论都没有得出**。")
        if failures:
            print(f"     （{len(failures)} 条失败，原因见上）")
        print()
        print("  ★★ 特别提醒：此时**不许**把「0 条 (a) / 0 条 (b)」读成")
        print("     「没有问题」或「全是 (a)」—— 那是**空集上的全称结论**。")
        print("  ★ 最常见的成因：**当日配额已满**（`day_key` 按本地日期分组，")
        print("     要在本地时间 **00:00 之后**才重置，不是'睡一觉就好'）。")
        print()
        print("★ 本脚本没有修改任何东西（站点只读、库只读）。")
        return 3
    n_a = n_b = 0
    for d in results:
        if d["present"] and (d["small_descr"] or "").strip():
            n_b += 1
            verdict = ("★ (b) 页面**有**副标题而我们没拿到 ⇒ **真 bug 在解析层**"
                       "（`opencd_parse`），要修")
        elif not d["present"]:
            n_a += 1
            verdict = ("✅ (a) 页面**没有** smalltitle 字段（present=False）"
                       " ⇒ 空是对的，「留空不硬造」，**什么都不该做**")
        else:
            n_a += 1
            verdict = ("✅ (a) 页面**有**字段但内容是空 ⇒ 空是对的"
                       "（与「齐秦那条」同一形状），**什么都不该做**")
        print(f"  opencd_tid={d['tid']}  smalltitle={d['small_descr']!r}  "
              f"present={d['present']}")
        print(f"      {verdict}")
    print()
    print(f"  ⇒ (a) 留空是对的 : {n_a} 条      (b) 真 bug : {n_b} 条")
    if n_b:
        print("  ★★ 有 (b) ⇒ **下一步是修 `opencd_parse` 的解析**，"
              "不是改库、更不是硬造副标题")
    else:
        print("  ★★ 全部是 (a) ⇒ **站上那几条空副标题不是 bug**。")
        print("      ★ 强烈建议：**不要去'修'它们** —— 硬造一个副标题就是往站上推假信息")
        print("        （`descr.opencd_small_descr` 的「留空不硬造」是刻意设计，")
        print("          见 `descr.py:314`）。")
    if failures:
        print()
        print(f"  ⚠️  有 {len(failures)} 条**没查成** ⇒ 上面结论**不覆盖**它们：")
        for k, d in failures:
            print(f"      {k}: {d}")
        print("      ⇒ ★ 判据只对**查成了**的那些成立（`ERR-SQL-05`：")
        print("         '没读到'不许并进'没有'）")
    print()
    print("★ 本脚本没有修改任何东西（站点只读、库只读；除非给了 --save 才落页面缓存）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
