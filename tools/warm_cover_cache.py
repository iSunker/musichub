"""把**已上站那批**的封面**抓进本地缓存**，只做这一件事（不写库、不上传、不碰皇后配额）。

═══════════════════════════════════════════════════════════════════════════
为什么需要它（2026-09-28 实测）—— 蟹黄堡发不出去的**真根因**
═══════════════════════════════════════════════════════════════════════════
`publish.py:_crabpt_cover()` 取蟹黄堡专用封面只有三步，**每一步都要"本地有个图文件"**：

    ① `site_publish(site='crabpt').cover_url` 已有 ⇒ 复用（空的，从没发过）
    ② `cover_fetch._cached_file(tasks.cover_url)` 查**本地缓存** ⇒ ★ 实测返回 `""`
    ③ `assets.upload_to_lsky(本地文件)` ⇒ 也要一个本地文件

★ 而 `cover_fetch.COVER_DIR = /data/covers` —— **这个目录从来没被建过**
  （实测 `os.path.isdir('/data/covers') == False`）⇒ **②③ 两条路同时断**。
  命令行上的症状就是那句：

      封面 ← ★ 本地封面缓存里没有这张图 ⇒ 没有可上传的源
      ★★ 注意：按现在的状态真发布会在这里停下（不发）

★ 且**不能退回 picgo 的链**（用户 2026-09-25 明确要求"两站封面各一份"，
  跨站图链已否掉）⇒ 所以**唯一的修法就是"先把图抓进缓存"**。

★★ 为什么**不能**用现成的 `backfill_covers_opencd.py --apply`：
  ① 它的口径是 `db.get_missing_cover(published_only=False)` = `cover_url` **为空**的行
     ⇒ 而转种队列那 26 条**全都有** `cover_url` ⇒ **一条都不在它的范围里**；
  ② 它是"抓 + 传图床 + **写库**"一条龙 ⇒ 会调 `db.set_cover()`，而本步**什么都不该写**。

═══════════════════════════════════════════════════════════════════════════
它**只**做三件事，其余一概不做
═══════════════════════════════════════════════════════════════════════════
    · 读 `tasks.cover_url`（那是**乌托邦那侧**的图，picgo 上那张）
    · 把那张图下到 `cover_fetch.COVER_DIR`（**外站、不扣皇后配额**）
    · 打一行"缓存在哪、多大"

★★★ **明确不做**（每条都有理由，别"顺手加上"）：
    ✗ **不写库** —— 不调 `db.set_cover()`、不动 `cover_url`、不动任何列。
      （本步的产物是**磁盘上的一个文件**，不是数据库状态。）
    ✗ **不上传图床** —— 不调 `upload_to_picgo` / `upload_to_lsky`。
      上传是**不可撤销的外部写**，且 Lsky 只有 500MB ⇒ 由发布那步按需去做
      （`_crabpt_cover` ③），本步只把"源"备好。
    ✗ **不碰皇后** —— `cover_url` 指向的是 picgo（`origin.picgo.net` 等外站），
      `cover_fetch.is_external()` 为真 ⇒ 走 `requests`，**不过 RateGate**。
      ★ 若某条的 `cover_url` 居然是**站内附件**（`attachments/`），本工具**跳过它
        并报出来** —— 那会扣皇后配额，属于"该由 crawl 那轮决定"的事，不在这里偷跑。
    ✗ **不删缓存**、不清理、不碰 `output_torrents/`。

═══════════════════════════════════════════════════════════════════════════
口径（为什么是"有 cover_url 的行"而不是"转种队列"）
═══════════════════════════════════════════════════════════════════════════
★ 本工具的口径**故意比转种队列宽**：**任何**有 `cover_url` 且图上还没缓存的行都值得
  暖一遍（乌托邦的 `edit` 阶段、将来别的站，都会用这份缓存）。
  ⇒ 所以**不要**在这里写 `site_status='EXISTS'` 那种窄判据 ——
    窄判据会让"同一件事"在下游各写一份（`ERR-META-03` 的漂移形状）。
  ★ 默认 `--limit` 只跑 1 条（见下），所以"宽口径"不会变成"一次抓一大批"。

用法（★ 一条一块，别贴一起）:
    python tools/warm_cover_cache.py --db /data/data.db                    # 只报告
    python tools/warm_cover_cache.py --db /data/data.db --apply --limit 1  # 先暖 1 条
    python tools/warm_cover_cache.py --db /data/data.db --apply            # 全部
    python tools/warm_cover_cache.py --db /data/data.db --apply --only e43884787e3a
★ 默认走本机 NAS 生产库（SMB）。**容器内**请显式传 `--db /data/data.db`。
★ 本工具**不需要停容器**（它只下载、只写缓存目录，不与 daemon 抢数据库）。
"""
import argparse
import os
import pathlib
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

# ★★ 仓库根目录进 sys.path（2026-10-01 实测踩过，ERR- 同族）：
#   `python tools/warm_cover_cache.py` 时 `sys.path[0]` 是**脚本所在目录**
#   `/app/tools`，**不是** cwd `/app` ⇒ `import cover_fetch` 报
#   `ModuleNotFoundError`（`cover_fetch.py` 在 `/app`）。
#   ★ 容器里 `docker compose run --rm tools python tools/xxx.py` 走的正是这条路，
#     所以**每一个不在 `/app` 根目录下的 `tools/*.py` 都受影响**。
#   ★ 别指望"cd /app 就行"：`sys.path[0]` 由**脚本路径**决定，与 cwd 无关
#     （实测 cwd=/app、`sys.path[0]=''` 是因为走了 `-c`；换成脚本路径就不再是空串）。
#   ★ 这里用**脚本自身位置**推根目录，因此无论从哪调、cwd 是什么都对。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

LOCAL_DB = r"//iSunker-DS423/docker_ssd/opencd2others/data/data.db"


def _rows(conn, only):
    q = ("SELECT info_hash, cover_url, site_status, formatted_title "
         "  FROM tasks WHERE cover_url IS NOT NULL AND cover_url != '' "
         " ORDER BY updated_at")
    rows = list(conn.execute(q))
    if only:
        want = tuple(s.strip().lower() for s in only.split(",") if s.strip())
        rows = [r for r in rows
                if any((r[0] or "").lower().startswith(w) for w in want)]
    return rows


def main():
    ap = argparse.ArgumentParser(description="把 cover_url 指向的图抓进本地缓存（不写库）")
    ap.add_argument("--apply", action="store_true", help="真的下载（不带则只报告）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0 = 全部）")
    ap.add_argument("--only", default="", help="只处理这些 info_hash 前缀（逗号分隔）")
    ap.add_argument("--db", default=LOCAL_DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA query_only=1")          # ★★ 本工具只读库 —— 写库是这个库最不该发生的事

    rows = _rows(conn, args.only)
    total = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    n_have_cover = len(rows)
    conn.close()

    print("=" * 78)
    print("★ 暖封面缓存（**只下载、只写缓存目录**：不写库 / 不上传 / 不碰皇后）")
    print("=" * 78)
    print(f"  库                  : {args.db}")
    print(f"  总行数              : {total}")
    print(f"  有 cover_url 的行    : {n_have_cover}")
    if args.only:
        print(f"  ★ --only            : {args.only}")
    if not rows:
        print("\n💡 没有有 cover_url 的行 ⇒ 无事可做。")
        return 0

    import cover_fetch                            # ★ 放在这里：它会拉 config（要 QB_PASS）

    print(f"  COVER_DIR           : {cover_fetch.COVER_DIR}")
    print(f"  ★ 该目录存在吗       : {os.path.isdir(cover_fetch.COVER_DIR)}"
          f"   ← 实测这里曾经是从未建过的（= 缓存全空）")

    # ── 分类：已缓存 / 外站可抓 / 站内(跳过) / 空 ──
    todo, cached, station, blank = [], [], [], []
    for h, url, ss, title in rows:
        u = (url or "").strip()
        if not u:
            blank.append((h, ss, title))
            continue
        if cover_fetch.is_external(u):
            hit = cover_fetch._cached_file(u)
            (cached if hit else todo).append((h, u, ss, title, hit))
        else:
            station.append((h, u, ss, title))

    if args.limit:
        todo = todo[:args.limit]

    print("\n" + "─" * 78)
    print("★ 分类（★ 站内那些**跳过**：会扣皇后配额，属于 crawl 那轮的管辖）")
    print("─" * 78)
    print(f"  ✅ 已缓存（0 次请求）        : {len(cached)}")
    print(f"  📥 外站可抓（不扣皇后配额）  : {len(todo)}" +
          (f"   ← ★ 本次被 --limit 截到这么多" if args.limit else ""))
    print(f"  ⏭️  站内附件（**跳过**）      : {len(station)}")
    print(f"  ⬜ cover_url 为空            : {len(blank)}")
    for h, u, ss, title in station[:5]:
        print(f"       [站内] {h[:12]} site={ss} {u[:56]}")

    if not args.apply:
        print("\n" + "─" * 78)
        print("★ 将要抓的（旧 → 缓存文件名由 sha1(url)[:12] 决定，抓完才知道扩展名）")
        print("─" * 78)
        for h, u, ss, title, _ in todo:
            print(f"  · {h[:12]}  site={ss}")
            print(f"      url   : {u[:70]}")
            print(f"      标题  : {(title or '')[:60]}")
        print("\n💡 预览模式：**一个请求都没发**。确认后加 --apply。")
        print("   ★ 先只跑 1 条：--apply --limit 1")
        return 0

    # ── 真抓 ──
    print("\n📥 开始下载（外站，不经 RateGate ⇒ 不扣皇后配额）...")
    ok = fail = 0
    for i, (h, u, ss, title, _) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {h[:12]}  site={ss}  {(title or '')[:44]}")
        # ★ logger=print 让它把「已缓存 / 已下载 / 失败原因」逐条打出来
        #   （失败原因必须看得见 —— ERR-META-14：兜底值不许冒充真结果）
        p = cover_fetch.download(u, logger=lambda m: print(f"    {m}"))
        if p and os.path.exists(p):
            ok += 1
            print(f"    ✅ 缓存: {p}  ({os.path.getsize(p) // 1024} KB)")
        else:
            fail += 1
            print("    ❌ 抓不到 ⇒ **本工具就此结束这一条**"
                  "（★ 不写空值、不留半成品文件 —— download() 是原子替换）")

    print("\n" + "=" * 78)
    print(f"🎯 暖缓存完成：成功 {ok} 条 | 失败 {fail} 条 | "
          f"已缓存跳过 {len(cached)} 条 | 站内跳过 {len(station)} 条")
    print("=" * 78)
    print("★ 本工具**到此为止** —— 库里一个字节都没改。")
    print("  下一步（人工，另跑）：确认发布那条路能拿到封面了：")
    print("    sudo docker exec -it opencd2musopia python publish.py "
          "--site crabpt --only <hash> --limit 1 --dry-run")
    print("  ★★ 别把 --dry-run 去掉之前，先看那行「封面 ←」是不是变成了本地路径。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
