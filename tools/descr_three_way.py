"""★ 只读：看清皇后副标题在**三处**各是什么 —— 列 / 快照 / 站点。

═══════════════════════════════════════════════════════════════════════════
为什么需要它（`ERR-META-01`：判据必须覆盖到"真正要比的东西"）
═══════════════════════════════════════════════════════════════════════════
`tools/recall_opencd_descr.py --dry-run` 报「待改 0」时，它比的是**两份**：
    `tasks.small_descr`  vs  `mapped_json.small_descr`
⇒ 那只证明「**列 == 快照**」，**证明不了「站上是对的」**。

而用户的诉求是「站上和皇后一致」⇒ 真正要比的是**第三份**：站点现值。
这三份两两相等/不等，对应**完全不同的**处置：

    列 == 快照 == 站上          ⇒ 真的没事了（本巡检该报"无异常"）
    列 == 快照 ≠ 站上            ⇒ ★ 库里对、站上错 ⇒ **只差一次 edit 推送**
                                   （成因：回写后没标脏 ⇒ 从不满足 STALE_CONDITION）
    列 ≠ 快照                    ⇒ 回写工具能修（旧的那个洞）
    列 ≠ 快照 ≠ 站上，三者互不相等 ⇒ 得先搞清"对的"是哪一份，别乱推

★ 本脚本**只读**：`PRAGMA query_only=1`，**不** import 项目写库路径。
★ 站点现值**取不到**（不联网）时，明确报「未取」——**绝不**拿库里值冒充站上值
  （`ERR-META-14`：来源为空不许印成"映射成功"）。

用法:
    python tools/descr_three_way.py --db /data/data.db
    python tools/descr_three_way.py --db /data/data.db --limit 20
    python tools/descr_three_way.py --db /data/data.db --only-mismatch
"""
import argparse
import json
import os
import sqlite3
import sys

# 与 `recall_opencd_descr.py` 同一口径：有 mapped_json 的皇后行。
TARGET_WHERE = (
    "opencd_tid IS NOT NULL AND opencd_tid != '' "
    "AND mapped_json IS NOT NULL AND mapped_json != ''"
)


def resolve_db(explicit=None):
    if explicit:
        return explicit
    data_dir = os.getenv("DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "data.db")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data.db")


def _snap(mapped_json):
    """从快照里取 `small_descr`。★ 取不到与"快照里就是空串"**必须分清**。"""
    if not mapped_json:
        return None, "no-snapshot"
    try:
        d = json.loads(mapped_json)
    except (ValueError, TypeError):
        return None, "bad-json"
    if not isinstance(d, dict):
        return None, "not-dict"
    if "small_descr" not in d:
        # ★ 快照里**没有这个键** ≠ 快照里它是空串（`ERR-META-14` 同族）
        return None, "key-absent"
    return (d.get("small_descr") or ""), "ok"


def classify(col, snap, site):
    """三份值 → 一个结论。`site is None` 表示**没取到**，不参与判定。"""
    col_n = (col or "").strip()
    snap_n = (snap or "").strip()
    if site is None:
        if col_n == snap_n:
            return "列==快照（站上未取 ⇒ 无法判是否一致）"
        return "★ 列≠快照（站上未取）⇒ 回写工具能修"
    site_n = (site or "").strip()
    if col_n == snap_n == site_n:
        return "✅ 三处一致"
    if col_n == snap_n != site_n:
        return "★★ 库里对、站上错 ⇒ 只差一次 edit 推送（缺标脏）"
    if col_n != snap_n:
        return "★ 列≠快照 ⇒ 回写工具能修（旧洞）"
    return "⚠️ 三处互不相等 ⇒ 先弄清哪份是对的"


def main(argv=None):
    ap = argparse.ArgumentParser(description="皇后副标题三方对照（只读）")
    ap.add_argument("--db", default=None, help="库路径（默认 $DATA_DIR/data.db）")
    ap.add_argument("--limit", type=int, default=0, help="最多打印几条明细")
    ap.add_argument("--only-mismatch", action="store_true",
                    help="只打印「列≠快照」的行（站上现值本脚本不取）")
    args = ap.parse_args(argv)

    db_path = resolve_db(args.db)
    if not os.path.exists(db_path):
        print(f"✗ 找不到库：{db_path}", file=sys.stderr)
        return 1

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA query_only=1")
    have = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "tasks" not in have:
        print(f"✗ {db_path} 里没有 tasks 表（现有：{sorted(have) or '无'}）", file=sys.stderr)
        return 3

    rows = con.execute(
        f"SELECT info_hash, torrent_name, small_descr, mapped_json, "
        f"site_status, site_torrent_id FROM tasks WHERE {TARGET_WHERE}"
    ).fetchall()

    print("=" * 78)
    print(f"库        : {db_path}")
    print(f"目标行数  : {len(rows)}   （口径 = 有 mapped_json 的皇后行）")
    print("★ 站点现值**本脚本不取**（只读、不联网）—— 需要它请另跑抓取。")
    print("=" * 78)

    buckets = {}
    detail = []
    for ih, name, col, mj, sstat, sid in rows:
        snap, how = _snap(mj)
        if how != "ok":
            buckets.setdefault(f"⚠️ 快照异常({how})", []).append(name)
            continue
        verdict = classify(col, snap, None)   # site 恒为 None：本脚本不联网
        buckets.setdefault(verdict, []).append(name)
        if col is not None and (col or "").strip() != (snap or "").strip():
            detail.append((name, col, snap, sstat, sid))

    for k in sorted(buckets):
        print(f"  {k}")
        print(f"      共 {len(buckets[k])} 条")

    if detail:
        print("-" * 78)
        print("★ 列 ≠ 快照 的明细（这些才是回写工具能修的）：")
        show = detail[:args.limit] if args.limit else detail[:20]
        for name, col, snap, sstat, sid in show:
            print(f"  {name[:44]}  [{sstat}/{sid}]")
            print(f"      列  : {col!r}")
            print(f"      快照: {snap!r}")
        if len(detail) > len(show):
            print(f"  … 另有 {len(detail) - len(show)} 条")
    else:
        print("-" * 78)
        print("✓ 没有「列≠快照」的行 ⇒ 回写工具（--apply）对本库**无事可做**。")
        print("  ★ 这不等于「站上是对的」—— 站上现值本脚本没取。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
