"""
皇后转载路的封面回填：本地优先 → 页面封面位 → 下载 → 传图床 → 写回 `cover_url`。

★ 为什么单独一个命令（不塞进 `crawl.py`）：
  `crawl` 那轮的请求配额是**共享**的（`max_per_round=20`），而它的抓取循环在
  配额用尽时是 **`break`**（本轮收工）⇒ 封面若把预算耗光，**后面的种子连详情页
  都抓不到**，而且**不报错**（只表现为"这轮没抓完"）。
  ⇒ 封面走**独立一趟**，**不可能饿死详情页**；形状照 `backfill_covers.py`
    （`--apply` / `--limit` / 预览先报告）。

★ 与 `backfill_covers.py` 的分工（**别混**）：
    `backfill_covers.py`        → 第一阶段（qB 本地抓轨）：靠 `content_path` 找封面
    `backfill_covers_opencd.py` → 第二阶段（皇后转载）：**本地优先，再回皇后取**
  ⇒ 本命令只处理 `opencd_tid` 非空的行，**不碰第一阶段**。

★★ 最要紧的一条纪律：**绝不写空 `cover_url`**。
  `assets.upload_to_picgo` 的 docstring 记着历史事故：把"上传失败"当成"没有封面"
  ⇒ 把库里已有的 `cover_url` **覆盖成空** ⇒ 站点页面封面被抹掉。
  而 `db.set_cover()` 是**无条件 UPDATE**（无"仅当为空才写"的护栏）
  ⇒ **这条只能由这里守住**：**只有拿到非空 URL 才调 `db.set_cover`**。

用法：
    python backfill_covers_opencd.py                    # 只报告（★ 不发任何请求）
    python backfill_covers_opencd.py --apply            # 取封面 → 传图床 → 写库
    python backfill_covers_opencd.py --apply --limit 1  # 先试一条
    python backfill_covers_opencd.py --no-qb            # 不问 qB（只靠库里的 content_path）
"""
import argparse
import os
import sys

import assets
import config
import cover_fetch
import db


def _qb_paths(tasks):
    """
    给缺 `content_path` 的任务补上本地目录（**问 qB 按 info_hash 查**）。

    ★ **复用** `backfill_covers.resolve_paths` 的同款做法，不重写逻辑。
    ★ 连不上 qB 是**非致命**的：只打印警告，剩下的仍能靠页面候选走。
    """
    need = [t for t in tasks if not (t.get("content_path")
                                     and os.path.exists(t["content_path"]))]
    if not need:
        return 0
    try:
        import qb_client
        qb = qb_client.build(config)
        by_hash = {}
        for t in qb.torrents_info():
            by_hash[t.hash.lower()] = t.content_path
    except Exception as e:
        print(f"⚠️  连不上 qBittorrent ({e}) —— 跳过本地查找，只走页面封面")
        return 0
    hit = 0
    for t in need:
        p = by_hash.get((t.get("info_hash") or "").lower())
        if p:
            t["content_path"] = p
            hit += 1
    print(f"🔗 从 qB 解析出 {hit}/{len(need)} 条的本地内容目录")
    return hit


def main():
    ap = argparse.ArgumentParser(description="皇后转载路的封面回填")
    ap.add_argument("--apply", action="store_true", help="实际取封面、传图床、写库")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条")
    ap.add_argument("--no-qb", action="store_true", help="不连 qB（只靠库里的 content_path）")
    args = ap.parse_args()

    config.print_runtime_info()
    db.init_db()

    # ★ 只处理**皇后路**的行（`opencd_tid` 非空）；第一阶段的封面归 backfill_covers.py
    tasks = [t for t in db.get_missing_cover(published_only=False)
             if (t.get("opencd_tid") or "").strip()]
    if not tasks:
        print("💡 没有缺封面的**皇后**任务。")
        print("   （第一阶段(qB 本地抓轨)的封面请跑：python backfill_covers.py）")
        return

    print(f"📋 缺封面的皇后任务 {len(tasks)} 条")
    if args.limit:
        tasks = tasks[:args.limit]

    # ── ① 补本地目录（用来做"本地优先"）──
    if not args.no_qb:
        _qb_paths(tasks)

    # ── ② 预览：先报告每条会走哪条路（★ 不发任何请求）──
    import opencd as P
    from crawl import load_raw          # 只用来读已落盘的详情页（**零请求**）

    print("\n" + "=" * 62)
    print("预览：每条会从哪里取封面")
    print("=" * 62)
    plan = []
    for t in tasks:
        tid = t["opencd_tid"]
        cp = t.get("content_path")
        # 本地有没有图？
        local = ""
        if cp and os.path.exists(cp):
            try:
                local = assets.find_cover(cp)
            except Exception:
                local = ""
        # 页面候选：**优先读已落盘的 HTML**（零请求）；没有落盘就跳过（不去抓）
        cands = []
        html = load_raw(tid)
        if html:
            try:
                cands = P.parse_detail(html).get("cover_candidates") or []
            except Exception:
                cands = []
        if local:
            src = f"本地 {os.path.basename(local)}"
        elif cands:
            a, u = cands[0]
            src = f"页面 {a or '(无alt)'} → {'外站' if cover_fetch.is_external(u) else '站内(1次请求)'}"
        else:
            src = "❌ 没有候选（本地无图、也没有已落盘的详情页）"
        plan.append((t, local, cands))
        print(f"  [{tid}] {(t.get('formatted_title') or '')[:44]}")
        print(f"        → {src}")

    n_local = sum(1 for _, l, _ in plan if l)
    n_page = sum(1 for _, l, c in plan if not l and c)
    n_none = sum(1 for _, l, c in plan if not l and not c)
    print("\n" + "=" * 62)
    print(f"✅ 本地可取（0 次皇后请求）：{n_local}")
    print(f"📄 需回皇后/外站取（站内占 1 次请求）：{n_page}")
    print(f"❌ 无候选：{n_none}")
    print("=" * 62)

    if not args.apply:
        print("\n💡 预览模式（**未发任何请求**）。确认后加 --apply。")
        return

    if not (n_local + n_page):
        print("\n没有可取的封面，退出。")
        return

    # ── ③ 真正取封面 ──
    # ★ 站内附件下载要过 `crawl.Client` 的 RateGate ⇒ 需要一个 client。
    #   只在**确实有站内候选**时才建（外站图与本地图都不需要配额）。
    needs_station = any(not l and c and not cover_fetch.is_external(c[0][1])
                        for _, l, c in plan)
    client = None
    if needs_station:
        import crawl
        import opencd as opencd_rate  # 原 `import opencd_rate`；已并入 opencd
        gate = opencd_rate.RateGate(
            max_per_round=int(os.getenv("OPENCD_MAX_PER_ROUND", "20")),
            min_interval=float(os.getenv("OPENCD_MIN_INTERVAL", "120")),
            max_per_day=int(os.getenv("OPENCD_MAX_PER_DAY", "50")),
            day_key=__import__("time").strftime("%Y-%m-%d"),
            log=print,
        )
        client = crawl.Client(gate=gate)
        print(f"\n⏱️  站内附件要过限速：最小间隔 {gate.min_interval:g}s")

    print(f"\n📤 开始取封面（{n_local} 条走本地、{n_page} 条走页面）...")
    ok = fail = 0
    for i, (t, local, cands) in enumerate(plan, 1):
        if not local and not cands:
            continue
        print(f"[{i}/{len(plan)}] {(t.get('formatted_title') or '')[:45]}")
        # ★ 用 `info` 承载候选：`fetch_cover` 的签名要 info（便于复用解析层形状）
        info = {"cover_candidates": cands}
        path, source, why = cover_fetch.fetch_cover(
            info, client=client,
            content_path=t.get("content_path"),
            logger=print,
        )
        if not path:
            fail += 1
            print(f"    ❌ 取不到封面：{why}")
            continue

        url = assets.upload_to_picgo(path)
        if not url:
            fail += 1
            # ★★ **绝不在失败时写库** —— 写了空串会抹掉站点上已有的封面
            print("    ❌ 图床失败 ⇒ **不写库**（避免把已有 cover_url 抹成空）")
            continue

        db.set_cover(t["info_hash"], url)
        ok += 1
        print(f"    ✅ [{source}] {url}")

    print("\n" + "=" * 62)
    print(f"🎯 封面完成：成功 {ok} 条，失败 {fail} 条")
    if ok:
        print("\n💡 下一步：把新封面推到站点页面上（编辑阶段会读 cover_url）")
        print("   python update_metadata.py --apply --only-cover")


if __name__ == "__main__":
    main()
