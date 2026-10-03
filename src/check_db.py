"""数据库状态速览。诊断"为什么这条没被编辑/发布"时先跑这个。"""
import argparse

import config
import db


def main():
    ap = argparse.ArgumentParser(description="查看任务库状态")
    ap.add_argument("--list", action="store_true", help="逐行列出所有任务")
    ap.add_argument("--problems", action="store_true",
                    help="只列出有问题的记录（缺 ID / 缺封面 / 失败）")
    args = ap.parse_args()

    config.print_runtime_info()
    db.init_db()

    st = db.stats()
    print(f"📊 总行数: {st['total']}")
    # ★★ 这一段的列是 `(status, site_status)` 的**两列联合分布**，不是"发布队列的某一级"。
    #
    #   ★ 为什么要专门写这句、还要补最后一行（2026-09-24）：
    #     原先这里只有 5 行原始分组，读的人**极其自然地**把它当成"逐级表的第一级"，
    #     于是得出「`SUCCESS/EXISTS 242` = 被闸门② 拦掉 242」——
    #     ★ **量级偶然相等，口径完全不同**：
    #       · 这里 242     是 `status='SUCCESS' AND site_status='EXISTS'`（**两列都限定**）
    #       · 闸门② 真正拦的是 `site_status='EXISTS'` = **269**
    #         （另 27 条是 `NO_TORRENT_FILE`，它们**进不了闸门①**，与闸门② 无关）
    #     ⇒ 两个数凑巧对上（242 + 34 = 276 也是同一个巧合），最危险的一类错：
    #       **它让人以为口径已经核过了。**
    #   ★ 同理这里的 `34` 漏了"不是 EXISTS"这一半条件 ⇒ 真值 **33**
    #     （多算了 1 条已上站的 `DENIED`）。
    #   ⇒ 现在：标题写明这是"两列联合分布"、每行标 `status=` / `site_status=`，
    #     并在末尾**直接印**那两个"闸门用的极简单列数"，让读者不必自己推。
    print("\n[status / site_status 分布 —— 两列**联合**口径，不是发布队列的某一级]")
    for status, site_status, count in st["groups"]:
        print(f"   status={status or '-':<14} site_status={site_status or '-':<10} {count:>5}")

    # ★ 真正的"极简单列数"：下面这两行**没有 status 条件**，就是闸门①② 用的那两个数。
    #   印出来是为了让"分布表"与"逐级表"对账时**不用手推**（ERR-META-03）。
    print("\n[★ 闸门用的极简单列数（**不带 status 条件**，与上面那段的口径不同）]")
    try:
        _conn = db.connect()
        _exists = _conn.execute(
            "SELECT count(*) FROM tasks WHERE site_status = 'EXISTS'").fetchone()[0]
        _not_exists = _conn.execute(
            "SELECT count(*) FROM tasks WHERE site_status IS NULL "
            "OR site_status != 'EXISTS'").fetchone()[0]
        _conn.close()
        print(f"   site_status='EXISTS'      {_exists:>5}   ← ★ 闸门② **拦**的就是这些"
              f"（≠ 上面那个 SUCCESS/EXISTS 的数）")
        print(f"   不是 EXISTS               {_not_exists:>5}   ← ★ 闸门① 的输入里"
              f"**只有** `status='SUCCESS'` 那部分能往下走")
    except Exception as e:                                     # noqa: BLE001
        print(f"   （读不到：{type(e).__name__}: {e}）—— 不影响下面的逐级表")

    print(f"\n⚠️  已发布但缺 site_torrent_id : {st['no_site_id']}"
          f"   {'→ 跑 sync_site.py --apply' if st['no_site_id'] else ''}")
    print(f"⚠️  已发布但缺封面             : {st['no_cover']}"
          f"   {'→ 跑 backfill_covers.py --apply' if st['no_cover'] else ''}")

    # ★★ 发布队列逐级（2026-09-23 补）—— 回答"为什么队列是 0 / 卡在哪一级"。
    #   ★ 口径**唯一来源** = `db.publish_funnel()`（它自己从 PENDING_PUBLISH_WHERE
    #     派生）⇒ 这里**不写任何 SQL**（ERR-META-03：手写查询必然与真口径漂移）。
    #   ★ 为什么需要它：`publish.py --dry-run` 队列为 0 时只说"没有需要发布的任务"，
    #     **不告诉你卡在哪级**；而现场手拼 `python -c` 会在 NAS 终端换行处断掉。
    fn = db.publish_funnel()
    print("\n[发布队列逐级（★ 累加口径：每行 = 加这一级后还剩多少）]")
    _chain = [
        ("① status=SUCCESS", fn["g1_status_success"]),
        ("①+② 未上站", fn["g2b_site_not_exists"]),
        ("①+②+②b 发布日期≤7天前", fn["g2_added_7d"]),
        ("①+②+②b+③ 皇后行已抓页面", fn["g3_mapped"]),
        ("①+②+②b+③+④ rule=ALLOW", fn["g4_allow"]),
    ]
    _prev = None
    for label, n in _chain:
        delta = "" if _prev is None else f"   （本级拦掉 {_prev - n}）"
        print(f"   {label:<26} {n:>5}{delta}")
        _prev = n
    print(f"   {'★ 队列（真口径）':<26} {fn['queue']:>5}")

    # ★★ 闸门② 那 24 条**不是一种原因**（2026-09-24 补）：
    #   · `opencd_added_at` **为空** ⇒ 口径「**不知道**几号发的」⇒ 拦（**永远**卡在这，
    #     除非 `vtid` 阶段把日期填上）
    #   · 日期**太新**（> 7 天窗口）⇒ 拦，但**只是还没到点**，到期自己会放开
    #   ★ 两者修法完全不同（一个要补数据、一个只要等），合成一个"拦掉 24"会读错。
    #   ★ 这就是"**同一格里的两个不同原因必须拆开**"（ERR-META-03 的同族）——
    #     和闸门④ 把空/DENY/NEED_CONFIRM 分开报是同一个道理。
    print("\n[闸门② 拦掉的那些：**两个原因分开看**]")
    try:
        _conn = db.connect()
        _base = ("status = 'SUCCESS' AND (site_status IS NULL OR site_status != 'EXISTS')")
        _no_date = _conn.execute(
            f"SELECT count(*) FROM tasks WHERE {_base} "
            f"AND (opencd_added_at IS NULL OR opencd_added_at = '')").fetchone()[0]
        _too_new = _conn.execute(
            f"SELECT count(*) FROM tasks WHERE {_base} "
            f"AND opencd_added_at IS NOT NULL AND opencd_added_at != '' "
            f"AND opencd_added_at > datetime('now', '-7 days')").fetchone()[0]
        _conn.close()
        print(f"   ├ **没有发布日期**（不知道 ⇒ 拦，要补数据） : {_no_date:>5}")
        print(f"   └ 日期太新（>7 天窗口 ⇒ 拦，**到期自己放开**） : {_too_new:>5}")
        print(f"   （两者相加 = 逐级表里 ②b 那格「本级拦掉」的数）")
    except Exception as e:                                     # noqa: BLE001
        print(f"   （读不到：{type(e).__name__}: {e}）")

    # ★ 下面这几个是**数据现状**，不在闸门链上 ⇒ 分开报（ERR-META-05：
    #   不同口径的数不许放在一起减）。用户点名的"还剩多少活"就是 tid_notnull。
    print("\n[数据现状（★ **全库**口径、不在闸门链上 —— 别与上面相减，"
          "也别当『能发多少』用）]")
    print(f"   有发布日期 opencd_added_at : {fn['added_notnull']:>5}")
    print(f"   有皇后 id  opencd_tid      : {fn['tid_notnull']:>5}")
    print(f"   transfer_rule='ALLOW'      : {fn['rule_allow']:>5}")
    print(f"   transfer_rule 为空         : {fn['rule_null']:>5}"
          f"   ← 空 = 「不知道能不能转」⇒ 拦（闸门④）")

    # ★★ 抓页面的待办（`topup_opencd_fields.py` 的池子）—— 2026-09-23 深夜补。
    #   ★ 为什么要在这里报：这两个数**决定明天 50 个请求该不该花**
    #     （实测教训：改前批量路把已上站的行排最前 ⇒ 请求全打水漂 ⇒ 队列长期 0）。
    #   ★ 口径直接从 `topup_opencd_fields.TARGET_WHERE` / `PICK_WHERE` 拿 ——
    #     **不在这里重写一份 SQL**（`ERR-META-03`：手写必与真口径漂移）。
    try:
        import topup_opencd_fields as _tp
        conn = db.connect()
        n_all = conn.execute(
            f"SELECT count(*) FROM tasks WHERE {_tp.TARGET_WHERE}").fetchone()[0]
        n_pick = conn.execute(
            f"SELECT count(*) FROM tasks WHERE {_tp.PICK_WHERE}").fetchone()[0]
        conn.close()
        print("\n[抓页面待办（topup_opencd_fields.py 的池子）]")
        print(f"   有 tid 但没页面（全部）    : {n_all:>5}")
        print(f"   ├ 未上站（★ 补了有用）     : {n_pick:>5}   ← 批量路只选这些")
        print(f"   └ 已上站（补了进不了队列） : {n_all - n_pick:>5}"
              f"   ← 要重推请用 --tids 点名")
    except Exception as e:
        print(f"\n⚠️  抓页面待办数读不到（{type(e).__name__}: {e}）—— "
              f"不影响上面的队列数")
    # ★ 一条**自动哨兵**：`queue` 与链末 `g4_allow` 必须相等，否则说明
    #   有人改了 PENDING_PUBLISH_WHERE 而 publish_funnel 的 levels 没跟着改
    #   （＝ 口径漂移，正是 ERR-META-03）。不相等就响亮地报出来，别静默。
    if fn["queue"] != fn["g4_allow"]:
        print(f"\n   ⚠️⚠️ 口径漂移：队列 {fn['queue']} != 链末 {fn['g4_allow']} —— "
              f"`PENDING_PUBLISH_WHERE` 与 `publish_funnel` 的 levels 不同步，"
              f"去 db.py 对齐（**别信这里的数**）")

    if args.problems:
        print("\n[缺 site_torrent_id]")
        for t in db.get_published(only_missing_id=True)[:30]:
            print(f"   {t['formatted_title']}")
        print("\n[缺封面]")
        for t in db.get_missing_cover()[:30]:
            print(f"   [{t['site_torrent_id'] or '-'}] {t['formatted_title']}")

    if args.list:
        conn = db.connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT site_torrent_id, status, site_status, formatted_title, "
            "site_synced_at FROM tasks ORDER BY updated_at DESC"
        )
        print("\nsite_id | status | site_status | synced_at | title")
        print("-" * 100)
        for sid, status, sst, title, synced in cur.fetchall():
            print(f"{sid or '-':>7} | {status or '-':<11} | {sst or '-':<8} | "
                  f"{(synced or '-')[:19]:<19} | {(title or '')[:50]}")
        conn.close()


if __name__ == "__main__":
    main()
