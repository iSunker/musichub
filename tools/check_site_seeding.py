"""★ 只读 CLI：巡检「每条 site_publish(EXISTS) 行的站点种(B) 有没有在做种」（#39）。

═══════════════════════════════════════════════════════════════════════════
它回答的问题（一句话）
═══════════════════════════════════════════════════════════════════════════
**"站点上每个我们挂过种的页面，那条给站点上报做种的 B 现在还活着吗？"**
（C 类"页面 0 做种"的根因就是 B 丢了 —— 见 `src/site_seed_patrol.py` 的长注释。）

★ 判据用**站点那份 .torrent(B)** 的 infohash + info.name 去 qB(3020/3040) 查 ——
  **不是** DB 的 `info_hash`(A)。用 A 查会把**健康**的种全报成 MISSING（`#45` 的教训）。

═══════════════════════════════════════════════════════════════════════════
★★ 本工具**只读**（`#39`）
═══════════════════════════════════════════════════════════════════════════
不写库、不改 qB、不发种、不删页。输出缺失/在做种两态 + 缺失清单；有 MISSING/AMBIGUOUS
⇒ **退出码非 0**（便于 CI/巡检）。
★ "自动加回丢失的 B" 是**另一件事**（`#49`，会写 qB）—— 本工具**只**负责照出来，
  好让它成为 `#49` 的判据来源（同一份 `site_seed_patrol` 库函数，不各写一份判据）。

用法（容器内；只读）:
    python tools/check_site_seeding.py                     # 两站全量
    python tools/check_site_seeding.py --site crabpt
    python tools/check_site_seeding.py --id 20517 --site musopia
    python tools/check_site_seeding.py --info-hash 9e9ed78a
    python tools/check_site_seeding.py --only-defective    # 只打缺失/歧义
    python tools/check_site_seeding.py --json /data/c_seeding_report.json
退出码：0 = 全部在做种；1 = 有 MISSING/AMBIGUOUS；2 = 环境/登录问题（无法开始）。
"""
import argparse
import json
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import _bootstrap  # noqa: F401

# ★ `config`/`db`/站点客户端**延迟到 main() 里 import**：它们在 import 期读 `.env`
#   ⇒ 顶层 import 会让 `--help` 也因缺 .env 而挂（`verify_hosted_edition.py` 同款理由）。
import site_seed_patrol as ssp  # noqa: E402  （纯模块，无 config 依赖）


def _retry(fn, *, tries=5, delay=3, what="op"):
    """★ 3020 WebUI 有间歇 SSL 抖动（SSLEOFError）⇒ 包一层（同 `readd_site_torrent.py`）。"""
    last = None
    for _ in range(tries):
        try:
            return True, fn()
        except Exception as e:                                    # noqa: BLE001
            last = e
            time.sleep(delay)
    return False, last


def _build_site_client(site):
    import db
    if site == db.SITE_MUSOPIA:
        from musopia import MusopiaClient
        return MusopiaClient()
    if site == db.SITE_CRABPT:
        from crabpt_site import CrabptClient
        return CrabptClient()
    raise SystemExit(f"未知站点 {site!r}")


def _build_qbs():
    """3020 = open.cd 通用 qB；3040 = 发种 qB（与 `readd_site_torrent.py` 同款构造）。"""
    import types
    import config
    import qb_client
    qb3020 = qb_client.build(types.SimpleNamespace(
        QB_HOST="http://127.0.0.1:3020", QB_USER=config.QB_USER, QB_PASS=config.QB_PASS,
        QB_API_KEY=getattr(config, "OPENCD_QB_API_KEY", "")))
    qb3040 = qb_client.build(config)
    return qb3020, qb3040


def _probe_one(row, *, clients, qb3020, qb3040, own_cats, interval):
    site = row["site"]
    cli = clients.get(site)
    if cli is None:
        cli = _build_site_client(site)
        clients[site] = cli
    cli.interval = interval

    def _download(tid):
        ok, res = _retry(lambda: cli.download_torrent(tid), tries=3, delay=2, what="dl")
        return res if ok else None

    qb_list = _make_qb_list()
    return ssp.patrol_row(row, download=_download, qb_3020=qb3020, qb_3040=qb3040,
                          own_categories=own_cats, qb_list=qb_list)


def _make_qb_list():
    """带重试的 qB 全表取数器（★ 3020 WebUI 有间歇 SSL 抖动，`#43` 实测）。

    ★ 重试仍失败 ⇒ 抛（由 `main` 收成退出码 2）—— 绝不退回空表
      （空表 = "什么种都没有" ⇒ 把**健康**的行全假报成 MISSING，最坏的谎）。
    """
    def _lister(qb):
        ok, res = _retry(lambda: qb.torrents_info(), tries=6, delay=4, what="qb-list")
        if not ok:
            raise RuntimeError(f"qB torrents_info 重试仍失败：{type(res).__name__}: {res}")
        return res or []
    return _lister


def main(argv=None):
    import db
    ap = argparse.ArgumentParser(
        description="★ 只读：巡检 site_publish(EXISTS) 行的站点种(B) 是否在做种")
    ap.add_argument("--site", choices=[db.SITE_MUSOPIA, db.SITE_CRABPT],
                    help="只巡一个站（默认两站）")
    ap.add_argument("--id", dest="torrent_id", help="只巡这个站点页面 id")
    ap.add_argument("--info-hash", help="只巡这个 info_hash（前缀匹配，指 DB 的 A）")
    ap.add_argument("--limit", type=int, default=0, help="只巡前 N 条（0=全部）")
    ap.add_argument("--only-defective", action="store_true",
                    help="输出只列 MISSING/AMBIGUOUS（汇总仍全量）")
    ap.add_argument("--json", dest="json_out", help="把结果写到这个 JSON 文件")
    ap.add_argument("--interval", type=float, default=1.5, help="站点请求间隔秒（默认 1.5）")
    args = ap.parse_args(argv)

    conn = db.connect()
    try:
        rows = ssp.iter_exists_rows(conn, site=args.site, torrent_id=args.torrent_id,
                                    info_hash=args.info_hash, limit=args.limit)
    finally:
        conn.close()
    if not rows:
        print("（没有匹配的 `site_publish` EXISTS 行 —— 无可巡检。）")
        return 0

    try:
        import config
        own_cats = set(config.HANDOFF_OWN_CATEGORIES)
        qb3020, qb3040 = _build_qbs()
    except Exception as e:                                        # noqa: BLE001
        print(f"❌ 无法建立 qB/配置（环境/登录问题）：{type(e).__name__}: {e}")
        return 2

    print("=" * 78)
    print(f"★ 只读巡检：site_publish(EXISTS) 行的 B 是否在做种 —— 共 {len(rows)} 行"
          f"  本项目分类={sorted(own_cats)}")
    print("=" * 78)

    results, clients = [], {}
    for row in rows:
        try:
            res = _probe_one(row, clients=clients, qb3020=qb3020, qb3040=qb3040,
                             own_cats=own_cats, interval=args.interval)
        except Exception as e:                                    # noqa: BLE001
            # ★ 环境/连接问题（如 qB 抖动重试仍失败）⇒ 显式退出码 2，**不**折成行判定
            print(f"\n❌ 巡检中断（环境/连接问题）：{type(e).__name__}: {e}")
            print("   ★ 这是**环境**问题，不是「某条种丢了」 —— 退出码 2。")
            return 2
        results.append(res)
        show = (not args.only_defective) or res["verdict"] in ssp.DEFECTIVE
        if show:
            flag = {ssp.SEEDING: "  ", ssp.PRESENT: "⚠️ ", ssp.MISSING: "❌",
                    ssp.AMBIGUOUS: "❌", ssp.NO_SITE_TORRENT: "❔"}.get(res["verdict"], "??")
            print(f"{flag} {res['site']:<8} id={res['torrent_id']:<7} "
                  f"A={str(res['info_hash'])[:10]}… B={str(res['b_ih'])[:10]}…  "
                  f"{res['verdict']}")
            print(f"      名字: {res.get('info_name')!r}   命中: {res.get('found_in') or '∅'}"
                  f"  how={res.get('how') or '-'}")
            if res["note"]:
                print(f"      说明: {res['note']}")

    from collections import Counter
    cnt = Counter(r["verdict"] for r in results)
    order = [ssp.SEEDING, ssp.PRESENT, ssp.MISSING, ssp.AMBIGUOUS, ssp.NO_SITE_TORRENT]
    print("-" * 78)
    print("汇总: " + "  ".join(f"{k}={cnt[k]}" for k in order if cnt.get(k)))
    defective = [r for r in results if r["verdict"] in ssp.DEFECTIVE]
    if defective:
        print(f"\n❌ 有缺陷 {len(defective)} 行（B 丢了 / 歧义）：")
        for r in defective:
            print(f"   · {r['site']} id={r['torrent_id']}  {r['verdict']}  "
                  f"B={str(r['b_ih'])[:12]}…  {r.get('info_name')!r}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"results": results, "summary": dict(cnt)}, f,
                      ensure_ascii=False, indent=2)
        print(f"\n结果已写入 {args.json_out}")

    return 1 if defective else 0


if __name__ == "__main__":
    sys.exit(main())
