# -*- coding: utf-8 -*-
"""把「下载未完成残影」标脏出队（`status='FAILED'`）。

═══════════════════════════════════════════════════════════════════════════
它解决的那个具体问题
═══════════════════════════════════════════════════════════════════════════
2026-11-13 实测：蟹黄堡直发队列 978 条里 **183 条** `torrent_file` 为空
（不是路径写错，是整列为空串），拆开看是两批完全不同的东西：

    · **15 条**  `content_path = /downloads/<专辑>` 且**目录存在**
                ⇒ 真·没制种，文件还在，补制种就能发。
                ★ 本脚本**不碰**这一批（`--incomplete-only` 默认开）。

    · **168 条** `content_path = /downloads/incomplete/<专辑>` 且**不存在**
                ⇒ qB 下载没跑完就被删掉的残影，文件根本不在。
                ⇒ 它们**永远不可能**发布，但每轮都进候选池、占发布配额、
                   抬高失败率 ⇒ 本脚本把这批标 `FAILED`。

═══════════════════════════════════════════════════════════════════════════
为什么标的是 `FAILED` 而不是新造一个状态
═══════════════════════════════════════════════════════════════════════════
· 蟹黄堡 / 星湾 / 麒麟 三站队列的 WHERE 都是
  `transfer_rule='ALLOW' AND (status IS NULL OR status != 'FAILED')`
  ⇒ **只有 `FAILED` 能让这些行真正出队**。标成 `NO_TORRENT_FILE` 或任何
  新状态，它们**照样在队列里**（实测踩过：改了状态但队列数纹丝不动）。
· `FAILED` 在 `db.PROTECTED_STATUSES` 里 ⇒ `run.py` 重扫**不会**把它推翻，
  不会今天标脏、明天又被改回 `SUCCESS`。
· `pending_*_direct()` 的排序注释已写明这套组合的用意：
  「没有 `status` 这层，一条坏种子会**恒在队首**而被每周期重试」。

═══════════════════════════════════════════════════════════════════════════
判据（四条同时成立才标）
═══════════════════════════════════════════════════════════════════════════
  ① `torrent_file` 为空（None 或空串）—— 从没制过种
  ② `content_path` 落在 `/downloads/incomplete/` 下 —— 未完成下载区
  ③ `content_path` 在盘上**不存在** —— 文件确实没了
  ④ `status != 'FAILED'` 且 `updated_at` 早于 `--older-than` 天（默认 7）
     ★ ④ 是防误伤：正在下载的任务路径可能瞬时不存在，给 7 天缓冲。

═══════════════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════════════
    python prune_stale.py                  # 只报告，不写库
    python prune_stale.py --apply          # 真写库（逐条 UPDATE，带 status 复检）
    python prune_stale.py --older-than 3   # 放宽到 3 天
    python prune_stale.py --all            # 不限 /downloads/incomplete/ 前缀

★ daemon 里的定期调度见 `daemon.py` 的 `prune` 阶段（默认 14 天一轮）。
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db

INCOMPLETE_PREFIX = "/downloads/incomplete/"
REASON = "下载未完成残影（incomplete 路径已不存在，无种子文件）"


def _cutoff(days):
    return (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def collect(older_than, incomplete_only, allow_only=False):
    """返回待标脏的行（dict 列表）。"""
    conn = db.connect()
    cur = conn.cursor()
    sql = (
        "SELECT info_hash, torrent_name, content_path, status, updated_at, transfer_rule "
        "  FROM tasks "
        " WHERE (torrent_file IS NULL OR torrent_file = '') "
        "   AND content_path IS NOT NULL AND content_path != '' "
        "   AND (status IS NULL OR status != 'FAILED')"
    )
    if allow_only:
        sql += " AND transfer_rule = 'ALLOW'"
    cur.execute(sql)
    rows = cur.fetchall()
    conn.close()

    cut = _cutoff(older_than)
    out = []
    skipped_exists = 0
    skipped_prefix = 0
    skipped_recent = 0

    for info_hash, name, cp, status, updated, rule in rows:
        if incomplete_only and not str(cp).startswith(INCOMPLETE_PREFIX):
            skipped_prefix += 1
            continue
        if os.path.exists(str(cp)):
            skipped_exists += 1
            continue
        if updated and str(updated) > cut:
            skipped_recent += 1
            continue
        out.append({
            "info_hash": info_hash,
            "torrent_name": name,
            "content_path": cp,
            "status": status,
            "updated_at": updated,
            "transfer_rule": rule,
        })

    stats = {
        "scanned": len(rows),
        "skipped_exists": skipped_exists,
        "skipped_prefix": skipped_prefix,
        "skipped_recent": skipped_recent,
    }
    return out, stats


def apply(rows):
    conn = db.connect()
    cur = conn.cursor()
    n = 0
    for r in rows:
        cur.execute(
            "UPDATE tasks SET status='FAILED', reason=?, updated_at=CURRENT_TIMESTAMP "
            " WHERE info_hash=? AND (status IS NULL OR status != 'FAILED')",
            (REASON, r["info_hash"]),
        )
        n += cur.rowcount
    conn.commit()
    conn.close()
    return n


def main():
    ap = argparse.ArgumentParser(description="把下载未完成残影标脏出队（status=FAILED）")
    ap.add_argument("--apply", action="store_true", help="真写库；不加则只报告")
    ap.add_argument("--older-than", type=int, default=7, metavar="N",
                    help="只处理 updated_at 早于 N 天的行（默认 7，防误伤正在下载的）")
    ap.add_argument("--all", action="store_true",
                    help="不限 /downloads/incomplete/ 前缀（默认只处理该前缀）")
    ap.add_argument("--allow-only", action="store_true",
                    help="只处理 transfer_rule='ALLOW' 的行（DENY/待确认的不动）")
    ap.add_argument("--limit", type=int, default=0, help="最多打印多少行样本（默认 10）")
    args = ap.parse_args()

    db.init_db()
    rows, stats = collect(args.older_than, not args.all, allow_only=args.allow_only)

    print("=" * 62)
    print("🧹 prune_stale：把「下载未完成残影」标 FAILED 出队")
    print("=" * 62)
    print(f"扫描候选（torrent_file 为空且 content_path 非空）：{stats['scanned']} 条")
    print(f"  ↳ 非 {INCOMPLETE_PREFIX} 前缀跳过：{stats['skipped_prefix']}")
    print(f"  ↳ 目录仍在（真·待制种，保留）：    {stats['skipped_exists']}")
    print(f"  ↳ 更新时间在 {args.older_than} 天内（保护）：{stats['skipped_recent']}")
    print(f"==> 命中待标脏：{len(rows)} 条")

    dist = {}
    for r in rows:
        dist[r.get("transfer_rule") or "(空)"] = dist.get(r.get("transfer_rule") or "(空)", 0) + 1
    if dist:
        print("    转载判定分布：" + "，".join(f"{k}={v}" for k, v in sorted(dist.items(), key=lambda x: -x[1])))

    show = args.limit or 10
    for r in rows[:show]:
        print(f"   {r['info_hash'][:12]} | {str(r['torrent_name'])[:46]} | status={r['status']}")
    if len(rows) > show:
        print(f"   …… 其余 {len(rows) - show} 条略")

    if not rows:
        print("\n✔ 没有需要标脏的行")
        return 0

    if not args.apply:
        print("\n[DRY-RUN] 未写库。确认无误后加 --apply 执行。")
        return 0

    n = apply(rows)
    print(f"\n✅ 已标脏 {n} 条（status=FAILED，reason={REASON}）")

    conn = db.connect()
    left = conn.execute(
        "SELECT COUNT(*) FROM tasks "
        " WHERE (torrent_file IS NULL OR torrent_file = '') "
        "   AND content_path LIKE ? "
        "   AND (status IS NULL OR status != 'FAILED')",
        (INCOMPLETE_PREFIX + "%",),
    ).fetchone()[0]
    conn.close()
    print(f"   复查：incomplete 前缀且仍非 FAILED 的行 = {left} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
