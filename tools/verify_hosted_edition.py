"""★ 只读：逐行核对**站点实挂的 `.torrent` 版本**与 DB 声称是否一致（#31/#32）。

═══════════════════════════════════════════════════════════════════════════
它在做的一件事（一句话）
═══════════════════════════════════════════════════════════════════════════
对每条"DB 说发过"的行，**回读站点**（详情页 + `download.php`），把站点**实际**挂的
`.torrent` 的 `info.name` 与 DB 的 `tasks.torrent_name` 比，判：

    MATCH / MISMATCH / MISSING-PAGE / DOWNLOAD-FAILED / NO-DB-NAME

★ 为什么必须回读站点、不能信本地（`#136` 的根因）：
  站点收下种子后会**重写** announce/passkey ⇒ infohash 变 ⇒ 本地那份对不上；
  而且 `#136` 让不同版本**共用一个 basename**、互相覆盖 ⇒ **只有**站上那份才是
  "到底发的是哪个版本"的**唯一**真话（见 `site_verify` 模块 docstring）。

★ 本工具**只读**：不写库、不发种、不删页。**有缺陷 ⇒ 退出码非 0**（便于 CI/巡检）。

═══════════════════════════════════════════════════════════════════════════
选行（★ 不手写 SQL —— 走 `db` 的公开函数，口径与生产同源，`ERR-META-03`）
═══════════════════════════════════════════════════════════════════════════
默认：遍历 **两站** 的 `site_publish` 里所有 `status='EXISTS'` 且 `torrent_id` 非空的行
      （那是"DB 认为这个站上真有这条"的唯一集合 —— 别的都是"没发/不确定"）。

用法（容器内；只读）:
    python tools/verify_hosted_edition.py                      # 两站全量
    python tools/verify_hosted_edition.py --site musopia
    python tools/verify_hosted_edition.py --id 20667 --site musopia
    python tools/verify_hosted_edition.py --info-hash 9e9ed78a
    python tools/verify_hosted_edition.py --limit 20 --json /tmp/vhe.json
退出码：0 = 全部 MATCH；1 = 有缺陷行（MISMATCH/MISSING-PAGE/DOWNLOAD-FAILED）；
        2 = 环境/登录问题（无法开始核对）。
"""
import argparse
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

import _bootstrap  # noqa: F401  —— 把 <repo> 与 <repo>/src 放进 sys.path

import site_verify as sv  # noqa: E402

# ★ `db` / 站点客户端**延迟到 main() 里 import**：它们会 import `config`，而 config
#   在 import 期就校验 `.env`（缺密钥直接抛）。⇒ 若在模块顶层 import，`--help`
#   都会因缺 .env 而挂掉。延迟后 `--help` 无需密钥即可用。


def _select_rows(conn, *, site=None, torrent_id=None, info_hash=None, limit=0):
    """挑出待核对的 (site, info_hash, torrent_id, db_name) 四元组。

    ★ 判据来自 `site_publish`（不是 `tasks.site_status`）：
      `site_status` 语义**只**指乌托邦侧，拿它去核蟹黄堡会串（`db.py` 那段警告）。
      ⇒ 按站点分别取 `site_publish` 的行，各站用各自的 `torrent_id`。
    ★ `db_name` 取 `tasks.torrent_name`（DB 声称的版本名）。
    """
    import db
    sites = [site] if site else [db.SITE_MUSOPIA, db.SITE_CRABPT]
    out = []
    for st in sites:
        sql = (
            "SELECT sp.site, sp.info_hash, sp.torrent_id, t.torrent_name "
            "  FROM site_publish sp LEFT JOIN tasks t ON t.info_hash = sp.info_hash "
            " WHERE sp.site = ? AND sp.status = 'EXISTS' AND sp.torrent_id IS NOT NULL "
            "   AND sp.torrent_id != ''")
        params = [st]
        if torrent_id:
            sql += " AND sp.torrent_id = ?"
            params.append(str(torrent_id))
        if info_hash:
            sql += " AND sp.info_hash LIKE ?"
            params.append(str(info_hash) + "%")
        sql += " ORDER BY CAST(sp.torrent_id AS INTEGER)"
        for r in conn.execute(sql, params):
            out.append({"site": r[0], "info_hash": r[1],
                        "torrent_id": r[2], "db_name": r[3]})
    if limit:
        out = out[:limit]
    return out


def _build_client(site):
    import db
    if site == db.SITE_MUSOPIA:
        from musopia import MusopiaClient
        return MusopiaClient()
    if site == db.SITE_CRABPT:
        from crabpt_site import CrabptClient
        return CrabptClient()
    raise SystemExit(f"未知站点 {site!r}")


def _probe_one(client, row):
    """只读探一条：拉详情页判存在 → 下种 → 比对。返回结果 dict（不抛）。"""
    tid = row["torrent_id"]
    res = dict(row)
    res["hosted_name"] = None
    res["note"] = ""
    try:
        r = client.get("/details.php", params={"id": tid})
        page_ok = sv.page_exists(r.text or "")
    except Exception as e:                          # noqa: BLE001
        res["verdict"] = sv.DOWNLOAD_FAILED
        res["note"] = f"取详情页失败：{type(e).__name__}: {e}"
        return res
    torrent_bytes = None
    if page_ok:
        try:
            torrent_bytes = client.download_torrent(tid)
        except Exception as e:                      # noqa: BLE001
            res["verdict"] = sv.DOWNLOAD_FAILED
            res["note"] = f"下载种子失败：{type(e).__name__}: {e}"
            return res
    verdict, hosted, note = sv.classify(
        page_ok=page_ok, torrent_bytes=torrent_bytes, db_name=res.get("db_name"))
    res["verdict"] = verdict
    res["hosted_name"] = hosted
    res["note"] = note
    return res


def main(argv=None):
    import db
    ap = argparse.ArgumentParser(
        description="★ 只读：核对站点实挂 .torrent 版本 vs DB 声称")
    ap.add_argument("--site", choices=[db.SITE_MUSOPIA, db.SITE_CRABPT],
                    help="只核一个站（默认两站）")
    ap.add_argument("--id", dest="torrent_id", help="只核这个站点页面 id")
    ap.add_argument("--info-hash", help="只核这个 info_hash（前缀匹配）")
    ap.add_argument("--limit", type=int, default=0, help="只核前 N 条（0=全部）")
    ap.add_argument("--json", dest="json_out", help="把结果写到这个 JSON 文件")
    ap.add_argument("--interval", type=float, default=1.5,
                    help="每次站点请求间隔秒（默认 1.5，站点保护）")
    args = ap.parse_args(argv)

    conn = db.connect()
    try:
        rows = _select_rows(conn, site=args.site, torrent_id=args.torrent_id,
                            info_hash=args.info_hash, limit=args.limit)
    finally:
        conn.close()

    if not rows:
        print("（没有匹配的 `site_publish` EXISTS 行 —— 无可核对。）")
        return 0

    print("=" * 74)
    print(f"★ 只读核对：站点实挂版本 vs DB   —— 共 {len(rows)} 行")
    print("=" * 74)

    results = []
    clients = {}
    for row in rows:
        site = row["site"]
        if site not in clients:
            clients[site] = _build_client(site)
        clients[site].interval = args.interval
        res = _probe_one(clients[site], row)
        results.append(res)
        flag = {"MATCH": "  ", "MISMATCH": "❌", "MISSING-PAGE": "❌",
                "DOWNLOAD-FAILED": "❌", "NO-DB-NAME": "⚠️ "}.get(res["verdict"], "??")
        print(f"{flag} {site:<8} id={res['torrent_id']:<7} "
              f"ih={str(res['info_hash'])[:10]}…  {res['verdict']}")
        print(f"      DB  : {res.get('db_name')!r}")
        if res["hosted_name"] is not None:
            print(f"      站点: {res['hosted_name']!r}")
        if res["note"]:
            print(f"      说明: {res['note']}")

    # 汇总
    from collections import Counter
    cnt = Counter(r["verdict"] for r in results)
    print("-" * 74)
    print("汇总: " + "  ".join(f"{k}={cnt[k]}" for k in
          [sv.MATCH, sv.MISMATCH, sv.MISSING_PAGE, sv.DOWNLOAD_FAILED, sv.NO_DB_NAME]
          if cnt.get(k)))
    defective = [r for r in results if r["verdict"] in sv.DEFECTIVE]
    if defective:
        print(f"\n❌ 有缺陷 {len(defective)} 行：")
        for r in defective:
            print(f"   · {r['site']} id={r['torrent_id']}  {r['verdict']}  "
                  f"DB={r.get('db_name')!r} 站点={r['hosted_name']!r}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"results": results, "summary": dict(cnt)}, f,
                      ensure_ascii=False, indent=2)
        print(f"\n结果已写入 {args.json_out}")

    return 1 if defective else 0


if __name__ == "__main__":
    sys.exit(main())
