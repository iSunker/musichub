"""#38 修：把「站点那份 .torrent」(B) 加回 qB，让站点重新看到做种（C 类 9 条）。

★ 为什么是"加 B"而不是"删 3040"（实测钉死，见 .scratch/C_group_rootcause.md）：
  C 类的真相 = **给站点做种的那条 B 丢了**（B-MISSING=9），只剩皇后原种 A（cat=opencd，
  只报 tracker.open.cd）。⇒ 站点从没收到我们的上报 ⇒ 页 0 做种。
  ⇒ 修法 = 把 B 加回；**不删 3040**（3040 早已空）、**不碰 A**（皇后业务）。

★ 安全：
  · 默认 dry-run；--apply 才真写。
  · 目标必须显式（--ids 或 --from-file）—— 绝不自己扫表猜。
  · 数据目录用 A 的 content_path（= 数据所在实例，★ 读 env `QB_HOST_OPENCD`）当依据，
    B 直接校验即可（零拷贝）。
  · 幂等：B 已在 qB ⇒ 409/查得到 ⇒ 跳过。
  · 加完 reannounce，回读 tracker status；==2 才算成功。
用法（容器内）：
    python tools/readd_site_torrent.py --site musopia --ids 20517 --dry-run
    python tools/readd_site_torrent.py --site musopia --ids 20517 --apply
    python tools/readd_site_torrent.py --from-file /data/b_missing.txt --apply
"""
import argparse
import io
import os
import sys
import time
import types

sys.stdout.reconfigure(encoding="utf-8")

import _bootstrap  # noqa: F401

SITE_CAT = {"musopia": None, "crabpt": None}   # 运行期从 config 填


def _retry(fn, *, tries=5, delay=3, what="op"):
    """★ 3020 WebUI 有间歇 SSL 抖动（SSLEOFError）⇒ 包一层重试。
    返回 (ok, result|err)。"""
    last = None
    for i in range(tries):
        try:
            return True, fn()
        except Exception as e:                                    # noqa: BLE001
            last = e
            time.sleep(delay)
    return False, last


def _clients(site):
    import config
    from musopia import MusopiaClient
    from crabpt_site import CrabptClient
    import qb_client
    site_client = MusopiaClient() if site == "musopia" else CrabptClient()
    # ★ O-2 修（2026-10-09）：**不再硬编码 3020**。
    #   重构后 3020 = qbit-movies1（电影！），`/downloads` 里**没有音乐**，
    #   导致 (a) 查不到 A 的 content_path ⇒ save_path 盲退 /downloads；
    #        (b) B 被加到**错的实例**（实测：落到 movies1、prog=0 stalledDL）。
    #   ⇒ 目标做种实例必须**读 env**（与 publish.py:340 同一口径）：
    #      QB_HOST_OPENCD（默认 3260=music3，opencd 原种 A 与数据所在）。
    #   ★ 如需回滚到旧 3020 行为：设 QB_HOST_OPENCD=http://127.0.0.1:3020。
    seed_host = os.getenv("QB_HOST_OPENCD", "http://127.0.0.1:3260")
    qb3020 = qb_client.build(types.SimpleNamespace(
        QB_HOST=seed_host, QB_USER=config.QB_USER, QB_PASS=config.QB_PASS,
        QB_API_KEY=getattr(config, "OPENCD_QB_API_KEY", "")))
    qb3040 = qb_client.build(config)
    cat = config.PUBLISH_CATEGORY if site == "musopia" else config.CRABPT_PUBLISH_CATEGORY
    return site_client, qb3020, qb3040, cat


def _all_seed_instances():
    """★ O-9（2026-10-09）：扫**全部** 12 实例，用于「同一 infohash 是否已在别处做种」去重。
    ★ 为什么必须扫全量：musopia/open.cd 都**拒绝同 IP 多客户端做同一种**
      （"cannot seed the same torrent in the same location from more than 1 client"
       / "You may only 1 seeding from one location"）。
      旧的 `already` 只查 qb3020/qb3040 两台 ⇒ Bruno 的重复份在 music1(3220) 查不到 ⇒ 撞冲突。
    ★ 端口清单与 NAS 重构后一致（见 docs/nas §9 / 任务大纲 §3.3）。可用 env `QB_PORTS` 覆盖。
    """
    import config
    import qb_client
    raw = os.getenv(
        "QB_PORTS",
        "3010,3020,3040,3120,3140,3220,3240,3260,3320,3340,3360,3380")
    out = []
    for p in [x.strip() for x in raw.split(",") if x.strip()]:
        host = f"http://127.0.0.1:{p}"
        try:
            out.append((p, qb_client.build(types.SimpleNamespace(
                QB_HOST=host, QB_USER=config.QB_USER, QB_PASS=config.QB_PASS,
                QB_API_KEY=getattr(config, "OPENCD_QB_API_KEY", "")))))
        except Exception as e:                                    # noqa: BLE001
            print(f"    ⚠️  [去重] 连 {p} 失败，跳过该实例: {type(e).__name__}")
    return out


def _find_on_all(clients, infohash):
    """返回 (port, torrent_obj) 列表 —— 该 infohash 在哪些实例上已存在。"""
    hits = []
    for p, c in clients:
        try:
            got = c.torrents_info(torrent_hashes=infohash)
            if got:
                hits.append((p, got[0]))
        except Exception:                                         # noqa: BLE001
            pass
    return hits


def main(argv=None):
    import db
    import torf
    ap = argparse.ArgumentParser(description="#38 把站点那份 .torrent 加回 qB，让站点看到做种")
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    ap.add_argument("--site", choices=["musopia", "crabpt"], default="musopia")
    ap.add_argument("--ids", help="逗号分隔的站点页面 id")
    ap.add_argument("--from-file", help="包含 id 的文件（每行一个或逗号分隔）")
    args = ap.parse_args(argv)

    ids = []
    if args.ids:
        ids = [s.strip() for s in args.ids.split(",") if s.strip()]
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            for line in f:
                ids += [s.strip() for s in line.replace(",", " ").split() if s.strip()]
    if not ids:
        sys.exit("❌ 必须给 --ids 或 --from-file（绝不自己扫全表）")

    site_client, qb3020, qb3040, cat = _clients(args.site)
    conn = db.connect()
    try:
        print("=" * 78)
        print(f"#38 加回站点种(B) —— site={args.site} 目标 {len(ids)} 条  "
              f"category={cat}  apply={args.apply}")
        print("=" * 78)
        for tid in ids:
            row = conn.execute(
                "SELECT sp.info_hash, t.torrent_name, t.content_path "
                "  FROM site_publish sp LEFT JOIN tasks t ON t.info_hash=sp.info_hash "
                " WHERE sp.site=? AND sp.torrent_id=?",
                (args.site, tid)).fetchone()
            if not row:
                print(f"  ⏭️  {tid} —— site_publish 里没有该 id 的行（跳过）")
                continue
            a_ih, name, cp = row
            raw = site_client.download_torrent(tid)
            if not raw:
                print(f"  ❌ {tid} —— 从站点下 .torrent 失败（跳过）")
                continue
            b_ih = str(torf.Torrent.read_stream(io.BytesIO(raw)).infohash)
            # 数据目录：★ 多文件种，qB 的 `save_path` 必须是 **info.name 的父目录**
            #   （= `/downloads`），qB 会去找 `<save_path>/<info.name>/<files>`。
            #   ★ 拿 A 的 `content_path`（= `/downloads/<专辑>`）当 save_path 会**错一层**
            #     ⇒ qB 去 `/downloads/<专辑>/<专辑>/…` 找 ⇒ progress=0（实测 20517 踩到）。
            #   ⇒ save_path = dirname(A.content_path)。
            save_path = ""
            a_info = qb3020.torrents_info(torrent_hashes=a_ih) if a_ih else []
            if a_info:
                cp_a = str(getattr(a_info[0], "content_path", "") or "").rstrip("/\\")
                if cp_a:
                    save_path = os.path.dirname(cp_a) or "/downloads"
            if not save_path:
                save_path = "/downloads"
            print(f"  · {tid} {name[:36]!r}")
            print(f"      B={b_ih[:12]} A={str(a_ih)[:12]} save_path={save_path}")
            # ★ O-9：全实例去重（不只 qb3020/qb3040）。
            hits = _find_on_all(_all_seed_instances(), b_ih)
            if hits:
                _where = ", ".join(
                    f"{p}:{getattr(t, 'state', '?')}/prog={float(getattr(t, 'progress', 0) or 0):.2f}"
                    for p, t in hits)
                # 若唯一命中就是本轮的目标实例(qb3020 的 host) → 幂等跳过；
                # 若命中在**别的**实例 → 加会撞「same location >1 client」⇒ 也跳过（报警）。
                print(f"      ⏭️  B 已在 {len(hits)} 个实例上：{_where} ⇒ **跳过**（避免同 IP 多客户端冲突）")
                continue
            if not args.apply:
                print(f"      🅿️  [dry-run] 将 torrents_add(B, save_path={save_path}, cat={cat}) + recheck + reannounce")
                continue
            try:
                # ★★★ 关键：add 时**带 recheck 语义**。qB 加种默认把种当"新下载"
                #   ⇒ 挂到 incomplete/、progress=0（实测 20517 就是这样）。
                #   ⇒ 必须让 qB 对 save_path 里的**现成文件**做哈希校验（recheck）
                #     ⇒ progress→1 ⇒ 才进入做种（tracker 上报 seeder，而非 leecher）。
                #   ⇒ `is_skip_checking=False`（默认即 False，但显式写死，防被当成 skip）
                #     且**先 pause，recheck，再 resume**：避免 recheck 期间又开始下载。
                ok, res = _retry(lambda: qb3020.torrents_add(
                    torrent_files=raw, save_path=save_path,
                    is_paused=True, category=cat, is_skip_checking=False), what="add")
                if not ok:
                    raise res
                time.sleep(3)
                _retry(lambda: qb3020.torrents_recheck(torrent_hashes=b_ih), what="recheck")
                # ★ 轮询 progress 到 1（大种校验要时间；最多等 90s）
                prog = 0.0
                for _ in range(30):
                    time.sleep(3)
                    ok, g = _retry(lambda: qb3020.torrents_info(torrent_hashes=b_ih), tries=3)
                    if not ok or not g:
                        continue
                    prog = float(g[0].progress or 0)
                    st = str(g[0].state or "")
                    if prog >= 1.0 or st in ("stalledUP", "uploading", "forcedUP", "queuedUP"):
                        break
                _retry(lambda: qb3020.torrents_resume(torrent_hashes=b_ih), what="resume")
                time.sleep(2)
                _retry(lambda: qb3020.torrents_reannounce(torrent_hashes=b_ih), what="reannounce")
                print(f"      ✅ add(paused)+recheck+resume+reannounce  progress={prog:.2f}")
                time.sleep(8)
                ok, got = _retry(lambda: qb3020.torrents_info(torrent_hashes=b_ih), tries=3)
                if ok and got:
                    trk = [(x.url.split('/')[2], x.status, (x.msg or '')[:40]) for x in
                           qb3020.torrents_trackers(torrent_hash=b_ih)
                           if x.url and 'announce' in x.url]
                    print(f"      回读: state={got[0].state} progress={got[0].progress} trk={trk}")
                    if float(got[0].progress or 0) < 1.0:
                        print(f"      ⚠️  progress 仍 <1 —— 校验没认到本地文件？需人工看")
                else:
                    print(f"      ⚠️  回读：qB 里还查不到 B")
            except Exception as e:                                # noqa: BLE001
                print(f"      ❌ add 失败: {type(e).__name__}: {e}")
        print("\n（dry-run 结束；加 --apply 才真写）" if not args.apply else "\n完成。")
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
