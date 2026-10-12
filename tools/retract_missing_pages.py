"""把"页面根本不存在"的**假 EXISTS 行**从 `site_publish` 撤下（#34 / B 组）。

═══════════════════════════════════════════════════════════════════════════
它治什么（#32 只读核对的结论）
═══════════════════════════════════════════════════════════════════════════
`tools/verify_hosted_edition.py` 全量核对（#32）查出 **21 行 `site_publish` 说
`EXISTS`、而站点详情页**根本不存在**（站点对不存在的 id 回 HTTP 200 **通用壳页**，
无 `<h1>`；同假 id `99999999` 一模一样）。这些 id 全是**较早**的（`15972…19350`），
是历史上被批量删除的（同 #23 记的"资料不全被删"），**但**：
    `site_publish` 这张表 **2026-09-25 才加**，`#23` 那次清洗（`clean_site_publish_ghosts.py`）
    **只覆盖了 `tasks.site_status` 一侧** ⇒ `site_publish` 里这些假 `EXISTS` **残留至今**。

★ 为什么必须撤（`ERR-SQL-07` 一族：拿假 EXISTS 当判据 = 静默多发）：
   `site_publish(site='musopia', status='EXISTS')` 是**"源站有这条"的判据**，被
   `db.transferable_to(crabpt)`（转种队列）等读 ⇒ 假行让队列**虚增**，那些候选
   要么转失败、要么转出个空的/重复的（发布**不可逆**）。

═══════════════════════════════════════════════════════════════════════════
本脚本做什么（与 `clean_site_publish_ghosts.py` 同一套写入口径）
═══════════════════════════════════════════════════════════════════════════
对每条**明确判为 MISSING-PAGE** 的行：
    · `site_publish.torrent_id` -> `''`
    · `site_publish.status`     -> `'UNKNOWN'`   （★ 显式表达"本地不知道站上状态"，
                                        与 `tasks.site_status` 用词同源；**不装坏**）
**不碰** `cover_url` / `synced_at`（历史痕迹，无害）。
**不删行**（行还挂着历史；删了将来无法区分"从没发过"与"发过又被删"）。
★ 为什么 `UNKNOWN` 而不是置 `''`：`db.site_publish_get()` 的判据是 ——
  `status is None` ⇒ 压根没记录；`status is not None and not torrent_id` ⇒ **记录坏了要报**。
  置 `''` 会落进第二种、**看起来像坏了**，而它其实是"知道的已知状态：站上已无"。

═══════════════════════════════════════════════════════════════════════════
★★ 安全设计（这是**不可逆**数据的写路径）
═══════════════════════════════════════════════════════════════════════════
1. **默认只读**（`--dry-run` 是默认）；真正写要 `--apply`。
2. ★★ **目标必须显式给出**（`--ids` 或 `--from-json`）—— **绝不**自己扫全表猜。
   为什么：本脚本动的是"某站上这条还有没有"这个事实；若它自作主张地按某个
   宽泛条件扫全表，一个判据写错就会**成批撤掉真行**（那比不收更坏）。⇒ 目标集
   只来自 (`verify_hosted_edition.py` 判为 `MISSING-PAGE` 的) **明确清单**。
3. **`--from-json` 会二次核对**：读 verify 的 JSON，**只取 `verdict == 'MISSING-PAGE'`**
   的行（不是"JSON 里所有行"）—— 防止把 MATCH/MISMATCH 也误撤。
4. **不删行、不碰 cover/synced_at**（见上）。
5. **单事务**（要么全成、要么全不动）。
6. ★ **不需要停容器** —— 走 `db.connect()`（`config.DB_PATH` + WAL + 30s busy timeout），
   与 daemon 是**同一个写者协议**（同 `clean_site_publish_ghosts.py`，不重蹈 `ERR-DOCKER-03`）。

用法（容器内）:
    # 1) 先出 JSON（只读）
    python tools/verify_hosted_edition.py --json /tmp/vhe.json
    # 2) 只读预览（默认）
    python tools/retract_missing_pages.py --from-json /tmp/vhe.json
    # 3) 真撤
    python tools/retract_missing_pages.py --from-json /tmp/vhe.json --apply
    # 或点名
    python tools/retract_missing_pages.py --site musopia --ids 18984,19069 --apply
"""
import argparse
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

import _bootstrap  # noqa: F401  —— 把 <repo> 与 <repo>/src 放进 sys.path

NEW_STATUS = "UNKNOWN"


def _targets_from_json(path):
    """从 verify JSON 里取**只**判为 MISSING-PAGE 的行 → `[(site, info_hash, torrent_id)]`。

    ★ 严格只认 `verdict == 'MISSING-PAGE'`（不是"结果里的全部"）—— 撤错一行就是
      把一条**真的还挂着**的种子记录抹掉（不可逆）。见本模块纪律 2/3。
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for r in data.get("results", []):
        if r.get("verdict") == "MISSING-PAGE":
            out.append((r["site"], r["info_hash"], str(r["torrent_id"])))
    return out


def _targets_from_ids(site, ids):
    return [(site, None, str(i)) for i in ids]


def _retract(conn, targets, *, apply=True, out=print):
    """★ 抽出来的**核心写逻辑**（`main` 与自检共用同一份 —— `ERR-META-03`）。

    :param conn: 一个**已连好**的 sqlite 连接（`main` 给 `db.connect()`；自检给临时库）。
    :param targets: `[(site, info_hash|None, torrent_id_str), ...]`。
    :param apply: False ⇒ 只读预览（回读现状、不写）。
    :return: `(exit_code, 处理行数)`。exit code 同本模块约定
      （0=成功/无目标，4=写失败已回滚，5=回读校验不符）。
    ★ 纪律（与 `main` 版逐条一致）：
      · **只读预览也回读当前真实状态**（绝不凭 JSON 旧快照直接写）。
      · 目标行**现在**必须 `status='EXISTS'` 且 id 对得上，才纳入（否则跳过）。
      · UPDATE 的 WHERE 带 `status='EXISTS'`（只改该改的，绝不误伤已 UNKNOWN 的行）。
      · **单事务** + 失败回滚；**不删行**；回读校验。
    """
    rows = []
    for site, ih, tid in targets:
        cur = conn.execute(
            "SELECT info_hash, torrent_id, status FROM site_publish "
            " WHERE site = ? AND torrent_id = ?",
            (site, tid)).fetchall()
        if ih:
            cur = [c for c in cur if c[0] == ih]
        if not cur:
            out(f"  ⏭️  {site} id={tid} —— 现在查不到该 id 的行（跳过）")
            continue
        for info_hash, torrent_id, status in cur:
            rows.append((site, info_hash, torrent_id, status))
            out(f"  · {site:<8} id={tid:<7} ih={info_hash[:12]}…  现 status={status!r}")

    if not apply:
        out(f"\n(只读预览；加 --apply 才真写。将把上面 {len(rows)} 行改为 "
            f"id='' / status={NEW_STATUS!r}，不删行、不碰 cover/synced_at)")
        return 0, len(rows)

    if not rows:
        out("（没有可改的行 —— 目标们现在都不是 EXISTS / 查不到。）")
        return 0, 0

    hashes = [(site, ih) for site, ih, _tid, _st in rows]
    try:
        conn.execute("BEGIN")
        cur = conn.cursor()
        for site, ih in hashes:
            cur.execute(
                "UPDATE site_publish SET torrent_id = '', status = ? "
                " WHERE info_hash = ? AND site = ? AND status = 'EXISTS'",
                (NEW_STATUS, ih, site))
        conn.commit()
    except Exception as e:                              # noqa: BLE001
        conn.rollback()
        out(f"❌ 写入失败，已回滚，库未动：{e}")
        return 4, 0
    out(f"\n✅ 已撤回 {len(hashes)} 行（单事务提交）。")

    bad = 0
    for site, ih in hashes:
        r = conn.execute(
            "SELECT torrent_id, status FROM site_publish WHERE info_hash=? AND site=?",
            (ih, site)).fetchone()
        if not r or (r[0] or "") != "" or r[1] != NEW_STATUS:
            bad += 1
            out(f"  ⚠️  回读不符: {site} {ih[:12]}… -> {r}")
    out(f"  回读校验：{len(hashes)-bad}/{len(hashes)} 行符合预期")
    return (5 if bad else 0), len(hashes)


def main(argv=None):
    import db
    ap = argparse.ArgumentParser(
        description="撤回『页面不存在』的假 EXISTS 行（site_publish：id->'' / status->UNKNOWN）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只读预览）")
    ap.add_argument("--from-json", help="verify_hosted_edition.py 的 --json 输出（只取 MISSING-PAGE 行）")
    ap.add_argument("--site", choices=[db.SITE_MUSOPIA, db.SITE_CRABPT], help="配合 --ids 用的站点")
    ap.add_argument("--ids", help="逗号分隔的站点页面 id（必须配 --site）")
    args = ap.parse_args(argv)

    if bool(args.from_json) == bool(args.ids):
        sys.exit("❌ 必须**恰好给一个**目标来源：--from-json 或 (--site + --ids)")
    if args.ids and not args.site:
        sys.exit("❌ --ids 必须配 --site")

    if args.from_json:
        targets = _targets_from_json(args.from_json)
    else:
        targets = _targets_from_ids(args.site, [s.strip() for s in args.ids.split(",") if s.strip()])

    if not targets:
        print("（没有 MISSING-PAGE 目标 —— 无可撤回。）")
        return 0

    conn = db.connect()
    try:
        print("=" * 74)
        print(f"#34/B 撤回假 EXISTS 行  —— 目标 {len(targets)} 条")
        print("=" * 74)
        code, _n = _retract(conn, targets, apply=args.apply, out=print)
        return code
    finally:
        conn.close()


def _selftest():
    """★ 离线自检（**不碰生产库 / 不碰 .env / 不发请求**）。返回 0=全绿。

    ★★ 阴性对照（本自检的**全部价值**）：`_retract` 是一段**不可逆**的写库路径，
      若自检只证明"能改"，那它连"会不会误改别的行"都没测到。⇒ 必须配：
        · **MATCH 行**（同表里别的 id）⇒ 一键都得**原封不动**（不许被误撤）；
        · 目标行**现在不是 EXISTS**（已 UNKNOWN）⇒ 不许改（幂等/不误伤）；
        · **只读预览**（apply=False）⇒ 一行都不许变。
    """
    import os
    import sqlite3
    import tempfile
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass
    fails = []

    def chk(label, got, want):
        ok = got == want
        print(f"  {'✅' if ok else '❌'} {label}  got={got!r} want={want!r}")
        if not ok:
            fails.append(label)

    def mkdb():
        """建临时库 + `site_publish` 表 + 几行 fixture。返回 (conn, path, M, C)。"""
        fd, path = tempfile.mkstemp(prefix="retract-selftest-", suffix=".db")
        os.close(fd)
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE site_publish ("
                  "info_hash TEXT, site TEXT, torrent_id TEXT, status TEXT, "
                  "cover_url TEXT, synced_at TEXT)")
        M, C, U = "musopia", "crabpt", "UNKNOWN"
        # 目标行（假 EXISTS，页面不存在）：改成 id='' / UNKNOWN
        c.executemany("INSERT INTO site_publish VALUES (?,?,?,?,?,?)", [
            ("h_ghost1", M, "18984", "EXISTS", "covG1", "t1"),
            ("h_ghost2", M, "19069", "EXISTS", "covG2", "t2"),
            # ★ 阴性对照①：真 MATCH 行（页面在、挂对了）—— 绝不许被碰
            ("h_match1", M, "20667", "EXISTS", "covM1", "t3"),
            ("h_match2", C, "216915", "EXISTS", "covM2", "t4"),
            # ★ 阴性对照②：已经撤过的行（非 EXISTS）—— 再点也不许动
            ("h_done",   M, "",      "", "covD", "t5"),
            # 同 ih 跨站：撤 musopia 侧不许动 crabpt 侧
            ("h_cross",  M, "19333", "EXISTS", "covX1", "t6"),
            ("h_cross",  C, "19333", "EXISTS", "covX2", "t7"),
        ])
        c.commit()
        return c, path, M, C

    def snap(c):
        return {(r[1], r[0]): (r[2], r[3], r[4], r[5])
                for r in c.execute("SELECT info_hash, site, torrent_id, status, "
                                   "cover_url, synced_at FROM site_publish")}

    # ── ① 纯函数：`_targets_from_json` **只取** MISSING-PAGE ──────────
    fd, jp = tempfile.mkstemp(prefix="retract-selftest-", suffix=".json")
    os.close(fd)
    with open(jp, "w", encoding="utf-8") as f:
        json.dump({"results": [
            {"site": "musopia", "info_hash": "h_ghost1", "torrent_id": "18984",
             "verdict": "MISSING-PAGE"},
            {"site": "musopia", "info_hash": "h_match1", "torrent_id": "20667",
             "verdict": "MATCH"},                       # ★ 不许被取
            {"site": "crabpt", "info_hash": "h_match2", "torrent_id": "216915",
             "verdict": "MISMATCH"},                    # ★ 不许被取
            {"site": "musopia", "info_hash": "h_ghost2", "torrent_id": "19069",
             "verdict": "MISSING-PAGE"},
        ]}, f, ensure_ascii=False)
    try:
        tgt = _targets_from_json(jp)
        chk("① 只取 MISSING-PAGE（2 条）", len(tgt), 2)
        chk("① MATCH/MISMATCH 未被取", {t[2] for t in tgt}, {"18984", "19069"})
    finally:
        os.unlink(jp)

    # ── ② 只读预览（apply=False）⇒ 一行不改 ──────────────────────────
    c, _path, M, C = mkdb()
    before = snap(c)
    code, n = _retract(c, [(M, "h_ghost1", "18984")], apply=False, out=lambda *_: None)
    chk("② 预览 exit=0", code, 0)
    chk("② 预览命中 1 行", n, 1)
    chk("② ★ 预览没有改动库", snap(c), before)

    # ── ③ apply ⇒ 目标行改对，**阴性对照行原封不动** ─────────────────
    code, n = _retract(c, [(M, "h_ghost1", "18984"), (M, "h_ghost2", "19069")],
                       apply=True, out=lambda *_: None)
    chk("③ apply exit=0", code, 0)
    chk("③ 改了 2 行", n, 2)
    r1 = c.execute("SELECT torrent_id, status FROM site_publish "
                   "WHERE site=? AND info_hash='h_ghost1'", (M,)).fetchone()
    chk("③ ghost1 -> id='' / UNKNOWN", (r1[0], r1[1]), ("", "UNKNOWN"))
    # ★ 阴性对照①：MATCH 行一字未动
    m1 = c.execute("SELECT torrent_id, status, cover_url, synced_at FROM site_publish "
                   "WHERE info_hash='h_match1'").fetchone()
    chk("③ ★ 阴性对照：MATCH 行原封不动", m1, ("20667", "EXISTS", "covM1", "t3"))
    # ★ 阴性对照：cover/synced_at 不许被碰（ghost 行的历史痕迹）
    g1 = c.execute("SELECT cover_url, synced_at FROM site_publish "
                   "WHERE info_hash='h_ghost1'").fetchone()
    chk("③ ★ ghost 行 cover/synced_at 保留", g1, ("covG1", "t1"))

    # ── ④ 阴性对照：目标行**现在不是 EXISTS**（id 已空/已撤）⇒ 不许改 ──
    #   ★ 点它旧 id 15972：该行现在 id='' ⇒ 按 id 查不到 ⇒ 跳过（幂等，不误伤）
    code, n = _retract(c, [(M, "h_done", "15972")], apply=True, out=lambda *_: None)
    d = c.execute("SELECT torrent_id, status FROM site_publish WHERE info_hash='h_done'").fetchone()
    chk("④ ★ 非 EXISTS 行未被改（保持 id='' / 空 status）", d, ("", ""))
    chk("④ 非 EXISTS 行未被计入", n, 0)
    # ★ 再加一层：即使**用现在的 id 空串**点名，也不许把 status 从 '' 提成 UNKNOWN
    code, n = _retract(c, [(M, "h_done", "")], apply=True, out=lambda *_: None)
    d2 = c.execute("SELECT torrent_id, status FROM site_publish WHERE info_hash='h_done'").fetchone()
    chk("④ ★ 点名空 id 不许把它改坏", d2, ("", ""))

    # ── ⑤ 跨站同 ih：撤 M 侧不许动 C 侧 ──────────────────────────────
    code, n = _retract(c, [(M, "h_cross", "19333")], apply=True, out=lambda *_: None)
    xm = c.execute("SELECT torrent_id, status FROM site_publish "
                   "WHERE info_hash='h_cross' AND site=? ", (M,)).fetchone()
    xc = c.execute("SELECT torrent_id, status FROM site_publish "
                   "WHERE info_hash='h_cross' AND site=?", (C,)).fetchone()
    chk("⑤ M 侧已撤", xm, ("", "UNKNOWN"))
    chk("⑤ ★ C 侧未被误撤", xc, ("19333", "EXISTS"))

    # ── ⑥ 目标查不到该 id ⇒ 跳过、不报错 ────────────────────────────
    code, n = _retract(c, [(M, None, "99999999")], apply=True, out=lambda *_: None)
    chk("⑥ 查不到的 id ⇒ 0 行、exit=0", (code, n), (0, 0))

    c.close()
    os.unlink(_path)
    print()
    if fails:
        print(f"❌ 自检失败 {len(fails)} 项：{fails}")
        return 1
    print("✅ retract_missing_pages 自检全绿")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(main())
