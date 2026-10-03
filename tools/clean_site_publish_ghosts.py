"""清洗 `site_publish` 里的**幽灵行**：站上已删、表里还说 `EXISTS`（#23）。

═══════════════════════════════════════════════════════════════════════════
背景（2026-10-04 实测定性）
═══════════════════════════════════════════════════════════════════════════
站点（musopia.vip）**批量删除了**我们发的一大批种子（**资料不全**，用户原话
"之前不是洗了库了么，200 多个种子都因为资料不全被删除了"）。

`tasks.site_status` **已经**在 2026-09-26 用 `reset_deleted_site_status.py`
对齐过（那次 `--apply` 改了 277 条，其中误伤 10 条 PUBLISHED —— 见该工具 docstring）。
⇒ 现在 `tasks.site_status='EXISTS'` 只剩 57 条（56 PUBLISHED + 1 NO_TORRENT_FILE），
   这些都**是对的**（真还在站上 / 真已发布），无需再动。

**但** `site_publish` 这张表 **2026-09-25 才加**，`reset_deleted_site_status.py`
**管不到它** ⇒ 它里面还留着一批**从没被更新过**的 `status='EXISTS'`：
    · `site_publish(site='musopia', status='EXISTS')` = **280** 条
    · 其中 **223** 条的 `tasks.site_status != 'EXISTS'`（= 站上已删的幽灵）
    · 余 **57** 条与 `tasks.site_status='EXISTS'` **一致**（真还在站上，保留）

实测（2026-10-04，只读）：
    223 条幽灵全部 `tasks.status='SUCCESS'`（没有 PUBLISHED 被误伤进集合）；
    214 条有 `cover_url`、222 条有 `synced_at`；
    `site_publish(site='crabpt')` **无幽灵、无孤儿**（19 条都是真的）。

═══════════════════════════════════════════════════════════════════════════
★ 为什么要洗（这不是"整洁强迫症"，是**有后果的**）
═══════════════════════════════════════════════════════════════════════════
`site_publish(site='musopia', status='EXISTS')` 是**"源站有这条"的判据**，
被 `db.transferable_to(crabpt)`（转种队列）与 `db.cross_site_dedup`（跨站去重）读。
⇒ 幽灵行让**蟹黄堡转种队列从 36 条虚增到 106 条**（多 70 条源已被删的候选）。
  那些要么转失败、要么转出个空的/重复的（发布**不可逆**）。
★ 这正是 `ERR-SQL-07` 一族（"汇总口径与实际会写的集合必须同口径"）：
  表说 EXISTS、站上已删 ⇒ 拿它当判据就是**静默多发**。

═══════════════════════════════════════════════════════════════════════════
本脚本做什么（用户 2026-10-04 选定：改回「空 / UNKNOWN」）
═══════════════════════════════════════════════════════════════════════════
对每条幽灵行（`site='musopia'` ∧ `status='EXISTS'` ∧ `tasks.site_status!='EXISTS'`）：
    · `torrent_id`  -> `''`        （id 在站上已无意义）
    · `status`      -> `'UNKNOWN'` （★ 不是 `''`：显式表达"本地不知道站上状态"，
                                     与 `tasks.site_status` 的用词同源）
**不碰** `cover_url` / `synced_at`（历史痕迹，无害；清它们是**另一个决定**）。
**不删行**（行本身还挂着历史；删了将来无法区分"从没发过"与"发过又被删"）。

★ 为什么 `UNKNOWN` 而不是直接删行 / 置空字符串：
  `db.site_publish_get()` 的判据是 —— `status is None` ⇒ 压根没记录；
  `status is not None and not torrent_id` ⇒ **记录坏了，要报出来**。
  ⇒ 若我们把 `status` 也置 `''`，就落进第二种、变成"**看起来坏了**"，
    而它其实是"**知道的已知状态**：站上已删"。`'UNKNOWN'` 语义准确、不装坏。

═══════════════════════════════════════════════════════════════════════════
纪律
═══════════════════════════════════════════════════════════════════════════
1. **默认只读**（`--dry-run` 是默认行为）；真正写要 `--apply`。
2. ★ **不需要停容器** —— 本脚本走 `db.connect()`（`config.DB_PATH` + WAL +
   30s busy timeout），**与 daemon 是同一个写者协议** ⇒ 并发安全。
   （`reset_deleted_site_status.py` 要停容器，是因为它**绕过** `db.connect()`
     直接开 sqlite3 连接，见 `ERR-DOCKER-03`。这里不重蹈。）
3. **单事务** —— 223 条要么全成、要么全不动（中途失败不能留半洗状态）。
4. **不删行、不碰 cover 列**（见上）。
5. 洗前洗后都**打印转种队列条数**（36↔106 那个数字），让效果可见。

用法（容器内）:
    python tools/clean_site_publish_ghosts.py            # 只读预览
    python tools/clean_site_publish_ghosts.py --apply    # 真洗
"""
import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8")

SITE = "musopia"          # 只洗 musopia；crabpt 实测无幽灵
GHOST_STATUS = "UNKNOWN"  # 幽灵行的新 status


def _ghost_rows(conn):
    """幽灵集合 = site_publish 说 EXISTS、而 tasks 说**不在站上**。

    ★ 判据必须用 `tasks.site_status != 'EXISTS'`（= **乌托邦那一侧**的真话，
      见 `db.py` 「`site_status` 语义收窄为乌托邦那一侧」）—— 不能用 `tasks.status`。
      2026-10-04 实测：223 条幽灵恰好都 `status='SUCCESS'`，但**那是巧合**，
      不是判据（拿巧合当判据 = `ERR-META-03` 的漂移形状）。
    """
    return list(conn.execute(
        f"SELECT sp.info_hash, sp.torrent_id, sp.status "
        f"  FROM site_publish sp JOIN tasks t ON sp.info_hash = t.info_hash "
        f" WHERE sp.site = ? AND sp.status = 'EXISTS' "
        f"   AND (t.site_status IS NULL OR t.site_status != 'EXISTS')",
        (SITE,)))


def _transfer_queue_count(conn):
    """当前"可转种到蟹黄堡"的条数（= `db.transferable_to` 的口径）。

    ★ 这里**刻意重写**一遍判据而不是 import `db.transferable_to`：
      因为本脚本要在**只读预览**阶段就报这个数（拿它当"效果可见"的对照），
      而 `transferable_to` 会开自己的连接。两处口径若漂了，本脚本报的数字会失真
      ⇒ 故这里**逐字照抄** `db.TRANSFERABLE_WHERE` 的形状，并在 docstring 里
        点明"改了那边这里要跟"（`ERR-META-03`）。
    """
    return conn.execute(
        "SELECT COUNT(*) FROM tasks t "
        "JOIN site_publish mp ON mp.info_hash = t.info_hash "
        "     AND mp.site = 'musopia' AND mp.status = 'EXISTS' "
        "LEFT JOIN site_publish cp ON cp.info_hash = t.info_hash "
        "     AND cp.site = 'crabpt' "
        "WHERE t.transfer_rule = 'ALLOW' "
        "  AND (cp.info_hash IS NULL OR cp.status != 'EXISTS')").fetchone()[0]


def main():
    ap = argparse.ArgumentParser(
        description="清洗 site_publish 幽灵行（站上已删、表里还 EXISTS）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只读预览）")
    ap.add_argument("--limit", type=int, default=0,
                    help="只处理前 N 条（调试用；0=全部）")
    args = ap.parse_args()

    import db

    # ---- 只读连接（预览用）----
    conn = db.connect()
    ghosts = _ghost_rows(conn)
    total_ghosts = len(ghosts)
    before_q = _transfer_queue_count(conn)
    kept = conn.execute(
        "SELECT COUNT(*) FROM site_publish WHERE site=? AND status='EXISTS'",
        (SITE,)).fetchone()[0] - total_ghosts

    print("=" * 70)
    print(f"#23 清洗 site_publish 幽灵行  site={SITE!r}")
    print("=" * 70)
    print(f"  幽灵行（站上已删、表里 EXISTS） : {total_ghosts}")
    print(f"  真行（与 tasks.site_status 一致）: {kept}")
    print(f"  转种队列条数（洗前）            : {before_q}")
    if args.limit:
        print(f"  ★ --limit {args.limit}：只处理前 {args.limit} 条")
    if not ghosts:
        print("✅ 没有幽灵行，无需清洗。")
        conn.close()
        return 0

    print("\n前 8 条示例（info_hash, id, status）：")
    for r in ghosts[:8]:
        print(f"    {r[0][:16]}…  id={r[1]!r}  status={r[2]!r}")

    if not args.apply:
        print(f"\n(只读预览；加 --apply 才真写。将把 {total_ghosts} 条改为 "
              f"id='' / status={GHOST_STATUS!r}，不删行、不碰 cover/synced_at)")
        conn.close()
        return 0

    # ---- 真写：单事务 ----
    targets = ghosts[: args.limit] if args.limit else ghosts
    hashes = [r[0] for r in targets]
    try:
        conn.execute("BEGIN")
        cur = conn.cursor()
        for ih in hashes:
            cur.execute(
                "UPDATE site_publish SET torrent_id = '', status = ? "
                " WHERE info_hash = ? AND site = ? AND status = 'EXISTS'",
                (GHOST_STATUS, ih, SITE))
        conn.commit()
    except Exception as e:                          # noqa: BLE001
        conn.rollback()
        print(f"❌ 写入失败，已回滚，库未动：{e}")
        conn.close()
        return 4
    print(f"\n✅ 已清洗 {len(hashes)} 条（单事务提交）。")

    # ---- 回读确认 ----
    remaining = len(_ghost_rows(conn))
    after_q = _transfer_queue_count(conn)
    print(f"  幽灵行剩余（应为 0）            : {remaining}")
    print(f"  转种队列条数（洗后）            : {after_q}")
    conn.close()
    if remaining != 0:
        print("⚠️  还有幽灵行剩余 —— 请检查！")
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
