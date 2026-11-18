"""★ CLI：按只读巡检结果，把"丢了站点种(B)"的页面自动加回 qB（`#49`）。

═══════════════════════════════════════════════════════════════════════════
它在做的一件事（一句话）
═══════════════════════════════════════════════════════════════════════════
跑 `site_seed_patrol`（只读）拿到每条 EXISTS 行的判定 ⇒ 对 **`MISSING`**（B 丢了）的行，
取站点 B 的字节、加回 3020、recheck、reannounce、**回读 tracker `msg`**（判 `same-location`）。
★ 这是 `#39`（只读眼睛）之后的**执行臂**；daemon 线程（`#51`）用**同一份**库逻辑。

★ 安全：**默认 dry-run**；`--apply` 才真写。只动 `MISSING`；`AMBIGUOUS`（→`#50`）和
  `NO_SITE_TORRENT`（没 B 可加）**一律不碰**。
用法（容器内）:
    python tools/readd_missing_from_patrol.py                 # dry-run 全量巡检→列出将加回
    python tools/readd_missing_from_patrol.py --site musopia
    python tools/readd_missing_from_patrol.py --id 20889 --apply
"""
import argparse
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import _bootstrap  # noqa: F401

import site_seed_patrol as ssp  # noqa: E402
import site_readd as sr  # noqa: E402


def _retry(fn, *, tries=5, delay=3, what="op"):
    last = None
    for _ in range(tries):
        try:
            return True, fn()
        except Exception as e:                                    # noqa: BLE001
            last = e
            time.sleep(delay)
    return False, last


def _clients():
    import types
    import config
    import db
    import qb_client
    from musopia import MusopiaClient
    from crabpt_site import CrabptClient
    from pter_site import PterClient
    from xingwan_site import XingwanClient
    from hdkylin_site import HdkylinClient
    from tangpt_site import TangptClient
    from muxuege_site import MuxuegeClient
    qb3020 = qb_client.build(types.SimpleNamespace(
        QB_HOST="http://127.0.0.1:3020", QB_USER=config.QB_USER, QB_PASS=config.QB_PASS,
        QB_API_KEY=getattr(config, "OPENCD_QB_API_KEY", "")))
    qb3040 = qb_client.build(config)
    sites = {
        db.SITE_MUSOPIA: MusopiaClient(),
        db.SITE_CRABPT: CrabptClient(),
        db.SITE_PTER: PterClient(),
        db.SITE_XINGWAN: XingwanClient(),
        db.SITE_HDKYLIN: HdkylinClient(),
        db.SITE_TANGPT: TangptClient(),
        db.SITE_MUXUEGE: MuxuegeClient(),
    }
    cats = {
        db.SITE_MUSOPIA: getattr(config, "PUBLISH_CATEGORY", None),
        db.SITE_CRABPT: getattr(config, "CRABPT_PUBLISH_CATEGORY", None),
        db.SITE_PTER: getattr(config, "PTER_PUBLISH_CATEGORY", None),
        db.SITE_XINGWAN: None,
        db.SITE_HDKYLIN: getattr(config, "HDKYLIN_PUBLISH_CATEGORY", None),
        db.SITE_TANGPT: getattr(config, "TANGPT_PUBLISH_CATEGORY", None),
        db.SITE_MUXUEGE: getattr(config, "MUXUEGE_PUBLISH_CATEGORY", None),
    }
    return qb3020, qb3040, sites, cats


def main(argv=None):
    import db
    ap = argparse.ArgumentParser(description="#49 按巡检结果把丢失的站点种(B)加回 qB")
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    ap.add_argument("--site", choices=getattr(db, "ALL_SITES", [
        db.SITE_MUSOPIA, db.SITE_CRABPT, db.SITE_PTER, db.SITE_XINGWAN,
        db.SITE_HDKYLIN, db.SITE_TANGPT, db.SITE_MUXUEGE
    ]), help="只巡一个站")
    ap.add_argument("--id", dest="torrent_id", help="只处理这个站点页面 id")
    ap.add_argument("--interval", type=float, default=1.5, help="站点请求间隔秒")
    args = ap.parse_args(argv)

    qb3020, qb3040, sites, cats = _clients()
    conn = db.connect()
    try:
        rows = ssp.iter_exists_rows(conn, site=args.site, torrent_id=args.torrent_id)
    finally:
        conn.close()

    print("=" * 78)
    print(f"#49 巡检→加回丢失的 B —— 共 {len(rows)} 行  apply={args.apply}")
    print("=" * 78)

    # ① 只读巡检（B 的 hash/名字 + 判定）
    results = []
    for row in rows:
        cli = sites[row["site"]]
        cli.interval = args.interval
        ok, raw = _retry(lambda: cli.download_torrent(row["torrent_id"]), tries=3, delay=2)
        raw = raw if ok else None
        results.append(ssp.patrol_row(row, download=lambda _t, _r=raw: _r,
                                      qb_3020=qb3020, qb_3040=qb3040,
                                      own_categories=set(cats.values()),
                                      qb_list=lambda q: q.torrents_info()))

    from collections import Counter
    cnt = Counter(r["verdict"] for r in results)
    print("巡检汇总: " + "  ".join(f"{k}={cnt[k]}" for k in
          [ssp.SEEDING, ssp.PRESENT, ssp.MISSING, ssp.AMBIGUOUS, ssp.NO_SITE_TORRENT] if cnt.get(k)))

    missing = [r for r in results if r["verdict"] == ssp.MISSING]
    print(f"\n待加回（MISSING）{len(missing)} 条：")
    for r in missing:
        print(f"  · {r['site']} id={r['torrent_id']}  B={str(r['b_ih'])[:12]}…  {r.get('info_name')!r}")
    if not missing:
        print("（无 —— 全部都在做种。）")
        return 0

    def download_for(site, tid):
        cli = sites[site]
        ok, raw = _retry(lambda: cli.download_torrent(tid), tries=3, delay=2)
        return raw if ok else None

    # ★ 目标发种实例为 qb3040（即 config.QB_HOST = 3010）；原种路径由 qb3020（原种库）解析
    outcomes = sr.readd_missing(missing, qb_target=qb3040, qb_state_client=qb3020,
                                download_for=download_for,
                                category_for=lambda s: cats.get(s),
                                dry_run=not args.apply)

    print("\n" + "-" * 78)
    bad = 0
    for row, ok, detail in outcomes:
        flag = "✅" if ok else "❌"
        print(f"{flag} {row['site']} id={row['torrent_id']}  {detail}")
        if not ok:
            bad += 1
    print(("\n（dry-run 结束；加 --apply 才真写）" if not args.apply else "\n完成。"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
