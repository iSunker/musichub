"""发布就绪状态诊断模块（只读）。

用于逐条核查待发布记录的完备性与阻断原因：
  1. 漏斗闸门链核对：基于 db.publish_funnel 统计各阶段筛选量；
  2. 预检规则匹配：通过 AST 动态解析 publish.py 中的预检条件文本；
  3. 物理文件与数据项核查：校验封面（cover_url）、种子文件物理存在性（torrent_file）与抓轨日志（log_content / logs_json）。

纯只读执行，不发起网络请求，不修改数据库记录。
"""
import argparse
import ast
import json
import os
import pathlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
import db

TRIED_FILE = "topup_tried.jsonl"


def _read_publish_checks():
    """
    从 `publish.py` **源码**里抠出全部预检的判据字符串（用 ast，不 import）。

    ★ 为什么要抠、不 import：`publish.py` 顶部会 `import config`，而 `config`
      在缺 `.env` 的机器上直接 `sys.exit`。更要紧的是 —— **抠**才能保证
      这里用的判据与 `publish.py` **同一份**（改了一处另一处必跟着变）。
    ★ 抠什么：那些 `reasons.append(...)` 的**文案**。它们就是
      "预检没过"的**可读结论**，比抠条件表达式更稳（条件可能被重构，文案不会）。
    ★ 抠不到 ⇒ **报错退出**（不是"跳过这一节"）。
    """
    # ★ 本模块在**仓库根**（与 publish.py 同级）—— 它在**容器内**跑，
    #   因为 `torrent_file` 是**容器内路径**，`os.path.exists` 只有在容器里才判得准
    #   （本机跑会被"文件不在本机"骗到，那正是  的形状）。
    p = pathlib.Path(__file__).resolve().parent / "publish.py"
    tree = ast.parse(p.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "reasons"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            found.append(node.args[0].value)
    if not found:
        raise SystemExit(
            "❌ 在 publish.py 里抠不到任何 `reasons.append(...)` —— "
            "**拒绝继续**（宁可不报，也不报一个猜的判据）")
    return found


def _row_blocking(r):
    """
    一条行**能不能发**，以及**卡在什么上**。返回 (ok, [原因...])。

    ★★ 这里逐条**实测"文件在不在"**（`os.path.exists`），不只是看列非空 ——
      "库里有路径" ≠ "文件真的在"（本项目成文纪律： 同族）。
    """
    reasons = []
    if not (r.get("cover_url") or "").strip():
        reasons.append("缺封面")
    tf = (r.get("torrent_file") or "").strip()
    if not tf:
        reasons.append("种子文件缺失（列是空的）")
    elif not os.path.exists(tf):
        reasons.append(f"种子文件缺失（路径不存在：{tf[:40]}）")
    # log：先看 log_content，再看 logs_json（与 publish.py 的判据**同序**）
    has_log = bool((r.get("log_content") or "").strip())
    if not has_log:
        try:
            lj = json.loads(r.get("logs_json") or "null")
            has_log = bool(lj) and any(
                (x.get("content") or "").strip()
                for x in lj if isinstance(x, dict))
        except (ValueError, TypeError):
            pass
    if not has_log:
        reasons.append("无 log（最初的规矩：没有 log 不发布）")
    return (not reasons), reasons


def main():
    ap = argparse.ArgumentParser(description="逐条拆开：这批行能不能发（只读）")
    ap.add_argument("--topped", action="store_true",
                    help="只看 topup 台账里试过的 tid（默认 = 整池）")
    ap.add_argument("--limit", type=int, default=40, help="最多打印几条（默认 40）")
    args = ap.parse_args()

    db.init_db()
    print("=" * 74)
    print("★ 能不能发（逐条拆开 · 只读 · 不发请求 · 不写库）")
    print("=" * 74)

    # ── 一、闸门链的年轮（口径来自 db.publish_funnel）────────────────────
    fn = db.publish_funnel()
    print("\n[闸门链（★ 累加口径，来自 db.publish_funnel）]")
    for label, key in (("① status=SUCCESS", "g1_status_success"),
                       ("①+② 未上站", "g2b_site_not_exists"),
                       ("①+②+②b 发布日期≤7天前", "g2_added_7d"),
                       ("①+②+②b+③ 皇后行已抓页面", "g3_mapped"),
                       ("①+②+②b+③+④ rule=ALLOW", "g4_allow")):
        print(f"   {label:<26} {fn[key]:>5}")
    print(f"   {'★ 队列（真口径）':<26} {fn['queue']:>5}")

    # ── 二、publish.py 自己的四道预检（口径从 publish.py 源码抠）──────────
    checks = _read_publish_checks()
    print("\n[publish.py 发布前预检（★ 比闸门链更硬；口径从 publish.py 源码抠）]")
    for c in checks:
        print(f"   · {c}")

    # ── 三、候选池 = 有 tid 的行（不管上没上站、status 是什么）────────────
    #   ★ 为什么是"有 tid"而不是"在队列里"：用户问的是
    #     "**今天补的**能不能发" —— 那批行**根本不在队列里**
    #     （它们 status 多半是 NO_TORRENT_FILE）⇒ 用队列口径**问不到**它们。
    conn = db.connect()
    # ★★ 列清单是**一个元组**，`SELECT` 与 `zip` **共用它** ——
    #   2026-09-23 深夜踩过：原先 `cols` 里漏了 `site_status`，于是
    #   ① `r["status"]` 直接 KeyError；② 更隐蔽的是 `zip` **按位置**对齐，
    #      漏一列会让**后面每一列都错位一格**（"缺封面 4 条"其实就是错位的产物 ——
    #      库里的 `cover_url` 明明是有的）。
    #   ⇒ 纪律：**列名只写一次**，`SELECT ', '.join(cols)` 与 `zip(cols, row)` 同源，
    #     从形状上消灭"位置对齐"这类错（同 `db.TASK_FIELDS` 的做法）。
    cols = ("info_hash", "torrent_name", "status", "site_status",
            "transfer_rule", "opencd_tid", "cover_url", "torrent_file",
            "log_content", "logs_json", "mapped_json")
    sel = ", ".join(cols)
    rows = [dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {sel} FROM tasks WHERE opencd_tid IS NOT NULL "
        f"AND opencd_tid != ''")]
    conn.close()

    if args.topped:
        p = os.path.join(config.DATA_DIR, "raw_html", TRIED_FILE)
        tried = set()
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if ln:
                        try:
                            tried.add(str(json.loads(ln).get("tid")))
                        except ValueError:
                            pass
        before = len(rows)
        rows = [r for r in rows if str(r["opencd_tid"]) in tried]
        print(f"\n[--topped] 台账 {len(tried)} 个 tid ⇒ 从 {before} 条里筛出 "
              f"{len(rows)} 条")

    print(f"\n{'=' * 74}")
    print(f"候选（有 tid 的行）: {len(rows)} 条 —— 逐条看它们缺什么")
    print("=" * 74)

    # ★★★ 判据自检：`torrent_file` 是**容器内路径** ⇒ `os.path.exists`
    #   只在容器里才判得准。★ 本模块顶部的注释早就写了这件事，但
    #   **注释不是判据** —— 2026-09-23 实测踩过：本机跑 ⇒ 258 条
    #   `torrent_file` 非空而 `os.path.exists` 全 False ⇒ 报出一个
    #   "看着像真的"的错结论（把齐全的行说成"种子文件缺失"）。
    #   ⇒ 让判据**自己宣告失效**。
    #   ★ 阈值是"**一条都不存在**"：生产里**确实**有失效路径
    #     （`qb_probe` 实测 `content_path` 有 30 条指向已删目录）
    #     ⇒ "部分不存在"正常、不许拦；"**全部**不存在"只可能是跑错地方。
    _tf = [r for r in rows if (r.get("torrent_file") or "").strip()]
    if _tf:
        _n_ex = sum(1 for r in _tf if os.path.exists(r["torrent_file"]))
        print(f"\n[判据自检] `torrent_file` 非空 {len(_tf)} 条，"
              f"其中**存在**的 {_n_ex} 条")
        if _n_ex == 0:
            raise SystemExit(
                f"❌ **判据在此处失效** —— 拒绝出数。\n"
                f"   `torrent_file` 非空 {len(_tf)} 条，而 `os.path.exists` "
                f"对它们**全部**返回 False。\n"
                f"   ★ 真实世界里这**不可能** ⇒ 不是数据坏，是**判据坏**。\n"
                f"   ★ 最常见的原因：你在**本机**跑，而 `torrent_file` 是"
                f"**容器内路径**\n"
                f"     （例：`{_tf[0]['torrent_file']}`）—— 本机没有它。\n"
                f"   ⇒ 请在 **NAS 上**跑：\n"
                f"        sudo docker compose run --rm tools python readiness_probe.py\n"
                f"   （本自检存在的理由：不拦的话会打印出一个**看着像真的**的"
                f"“N 条种子文件缺失”，而那是错的 —— 。）")
        print("   ✅ 非空即存在（或部分存在）⇒ 判据在本环境**成立**")

    ready, bucketed = [], {}
    for r in rows:
        ok, why = _row_blocking(r)
        if ok:
            ready.append(r)
        else:
            for w in why:
                bucketed[w] = bucketed.get(w, 0) + 1

    print("\n[卡点分布（★ 一条行可能同时缺几样，所以**不能相加** —— ）]")
    for w, n in sorted(bucketed.items(), key=lambda kv: -kv[1]):
        print(f"   {n:>5} 条  {w}")
    print(f"\n   ★ 预检**全过**的（上面四道都不犯）: {len(ready)} 条")

    # ★★ 关键对照：**status 是不是拦路虎**？
    #   用户的问题隐含一个假设："改了 status 就能发"。
    #   这一节**当场证伪或证实**它 —— 分开两批数：
    #     · status=SUCCESS       且预检全过   ⇒ 现在就能发
    #     · status≠SUCCESS       但预检全过   ⇒ ★ 只差 status 这一步（改了就发）
    #     · status 无所谓        但预检不过   ⇒ ★ 改 status 也**没用**
    now_ok, only_status, no_use = [], [], []
    for r in rows:
        ok, why = _row_blocking(r)
        if r["status"] == "SUCCESS":
            (now_ok if ok else no_use).append((r, why))
        else:
            (only_status if ok else no_use).append((r, why))
    print("\n[★ status 是不是拦路虎（回答「改 status 就能发吗」）]")
    print(f"   status=SUCCESS 且预检全过  ⇒ **现在就能发**      : {len(now_ok)}")
    print(f"   status≠SUCCESS 但预检全过  ⇒ **改 status 就能发** : {len(only_status)}")
    print(f"   预检不过（★ 改 status 也没用）                    : {len(no_use)}")

    if args.limit:
        print(f"\n[明细（前 {args.limit} 条）]")
        for i, r in enumerate(rows[:args.limit], 1):
            ok, why = _row_blocking(r)
            mark = "✅ 预检全过" if ok else "❌ " + "、".join(why)
            print(f"  [{i:>3}] tid={r['opencd_tid']:<9} "
                  f"status={r['status']:<16} site={r['site_status'] or '-':<8} "
                  f"rule={r['transfer_rule'] or '-'}")
            print(f"        {(r['torrent_name'] or '')[:60]}")
            print(f"        {mark}")

    print("\n" + "=" * 74)
    print("★ 本脚本**只报事实**，不给发布建议 —— 发不发是你的决定。")
    print("  ★ 特别是：`transfer_rule` 非 ALLOW 的行，闸门④ 也会拦"
          "（本脚本只看 publish 预检，**不代替闸门④**）。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
