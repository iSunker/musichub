"""把「站点上已被删除」的种子的 `site_status` 改回 `UNKNOWN`（**不删行**）。

═══════════════════════════════════════════════════════════════════════════
背景（2026-09-23 实测定性）
═══════════════════════════════════════════════════════════════════════════
`tasks.site_torrent_id` 有 269 条落在 14611–17100。只读探针（`_probe_site_ids.py`）
实测其中 5 个 id 在站点上**全部**「找不到 takeedit 表单」；用户确认
**之前上传到 musopia 的种子都被删除了**，只剩 3 个还在：
    details.php?id=15972 / 15974 / 17099

⇒ 结论：那 269 条里 **266 条**的 `site_status='EXISTS'` 是**错的**
   （种子已被删，站点上没有了）。
★ 同时这也**结掉了** SESSION-NOTES 的未解 #1（"14611–17100 属于哪个站"）：
   它就是 **musopia.vip 的真实种子 id**，只是东西被删了。

═══════════════════════════════════════════════════════════════════════════
★★ 为什么**不删行**
═══════════════════════════════════════════════════════════════════════════
这 269 行**每条都同时是两样东西**：

| | 含义 | 列 |
|---|---|---|
| ① | 「站点已发布」的记录 | `site_status='EXISTS'` + `site_torrent_id` |
| ② | 「本地抓轨成果」 | `status='SUCCESS'` + `log_content` + `content_path` + `tracklist` |

`DELETE` 会**连 ② 一起丢掉** —— 实测 269/269 都有 `log_content`（3130~15579 字符）、
都有 `content_path`，其中 224 条还有刚回填的 `opencd_tid`。
⇒ 所以只把 ① 改掉（`site_status='UNKNOWN'` + 清空 `site_torrent_id`），行与成果全留。

═══════════════════════════════════════════════════════════════════════════
★ 连带后果（用户已知情并选择）
═══════════════════════════════════════════════════════════════════════════
改回 `UNKNOWN` 后，这些行满足 `db.PENDING_PUBLISH_WHERE`
（`status='SUCCESS' AND (site_status IS NULL OR site_status != 'EXISTS')`）
⇒ **它们会重新落进「待发布」队列 ⇒ publish 会把它们重新发到站点。**
（按 `.env` 的 `PUBLISH_MAX_PER_CYCLE` 每轮一批；速率见 README 的「发布限速」节。
★ 这里**不写死数字** —— 上限在 2026-09-24 从 `5` 改成了 `1`，
写死就会立刻变成一句**看着可信的错话**，`ERR-META-03`。）

★ 若**不想**重发，必须同时让它不满足队列条件 —— 那是**另一个决定**，
  本脚本不擅自做（`--dry-run` 会把这个后果**明确打出来**）。

═══════════════════════════════════════════════════════════════════════════
三条纪律
═══════════════════════════════════════════════════════════════════════════
1. **默认只读**（`--dry-run`）；真正写要 `--apply` **且** `--i-have-stopped-the-container`。
   ★ 必须**先停容器** —— 两个写者写同一个 SQLite 是 `ERR-DOCKER-03`。
2. **绝不删行** —— 只 UPDATE 两列（`site_status` / `site_torrent_id`）。
   **不碰** `log_content` / `content_path` / `opencd_tid` / `status`。
3. **保留 `--keep` 列出的 id** —— 实测还在站点上的那些不许动。

用法（本机；**容器停着**）:
    python tools/reset_deleted_site_status.py                        # 只读预览
    python tools/reset_deleted_site_status.py --apply --i-have-stopped-the-container
    python tools/reset_deleted_site_status.py --keep 15972,15974,17099
"""
import argparse
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

LOCAL_DB = r"//iSunker-DS423/docker_ssd/opencd2others/data/data.db"

# ★ 实测**还在**站点上的（用户 2026-09-23 确认）⇒ 保留
DEFAULT_KEEP = ("15972", "15974", "17099")

# 只改这两列。★ 其余一概不碰（尤其 log_content / content_path / opencd_tid / status）
SET_COLS = ("site_status", "site_torrent_id")


def select_targets(conn, keep, only=()):
    """
    要处理的 = `site_status='EXISTS'` **且 `status='SUCCESS'`** 且 id 不在 keep 里。

    ★★★ 为什么必须加 `status='SUCCESS'`（2026-09-26 修，**本脚本真出过这个事**）

    原先口径**只看 `site_status='EXISTS'`** ⇒ 把 `status` 不是 `SUCCESS` 的行
    也一并重置了。而"哪些行会重发"是由 `db.PENDING_PUBLISH_WHERE`
    （`status='SUCCESS' AND (site_status IS NULL OR site_status != 'EXISTS')`）
    决定的 —— **只改 `site_status` 的这些行根本不进队列**。

    ⇒ 后果：那 10 条 `status='PUBLISHED'` 的**站点记录被无谓地抹掉**
      （`site_status` → `UNKNOWN`、`site_torrent_id` → `''`），
      而它们**既不会重发**（不是 SUCCESS）、**又再也不能被 `edit` 更新**
      （`edit` 的取数口径要求 `site_status='EXISTS'`）⇒ 变成**孤儿**。
      ★ 实测（2026-09-26）：一次 `--apply` 改掉 277 条，其中 **10 条是
        `PUBLISHED`** —— 它们的 `site_torrent_id` 旧值**已从库里消失、找不回来**。

    ★ 教训的形状（`ERR-META-05` 一族）：**"改哪几行"必须与"这个改动为谁服务"
      同口径**。本脚本的目的是"让被删的种子重新进发布队列" ⇒ 那么它的目标集
      就**必须是队列口径的子集**；只按 `site_status` 取，是把"状态不对的行"
      当成了"要重新排队的行"，二者不是一回事。
      ★ 当时我**算出了**"277 条里只有 242 条会进队列"，却没接着问
        "那另外 35 条**还该不该改**" —— 差的就是这一问。

    ★ `only` 非空时**只处理列出的那几个 id**（「先试发两条」用）；
      它也**同样**受 `status='SUCCESS'` 约束（否则"试发"会连带改掉一个
      不会发的行，试出来的结果就不代表真实批量行为）。
    """
    if only:
        ph = ",".join("?" * len(only))
        sql = (f"SELECT info_hash, site_status, site_torrent_id, status, "
               f"       length(log_content), content_path, opencd_tid, formatted_title "
               f"  FROM tasks "
               f" WHERE site_status = 'EXISTS' "
               f"   AND status = 'SUCCESS' "
               f"   AND site_torrent_id IN ({ph})")
        return list(conn.execute(sql, tuple(only)))

    ph = ",".join("?" * len(keep)) if keep else "''"
    sql = (f"SELECT info_hash, site_status, site_torrent_id, status, "
           f"       length(log_content), content_path, opencd_tid, formatted_title "
           f"  FROM tasks "
           f" WHERE site_status = 'EXISTS' "
           f"   AND status = 'SUCCESS' "
           f"   AND site_torrent_id IS NOT NULL AND site_torrent_id != '' "
           f"   AND site_torrent_id NOT IN ({ph})")
    return list(conn.execute(sql, tuple(keep)))


def main():
    ap = argparse.ArgumentParser(
        description="把站点上已删除的种子的 site_status 改回 UNKNOWN（不删行）")
    ap.add_argument("--apply", action="store_true", help="真正写库")
    ap.add_argument("--i-have-stopped-the-container", action="store_true",
                    help="★ 按纪律：必须显式声明容器已停（ERR-DOCKER-03）")
    ap.add_argument("--keep", default=",".join(DEFAULT_KEEP),
                    help="保留的 site_torrent_id（逗号分隔；这些还在站点上）")
    ap.add_argument("--only", default="",
                    help="★ 只处理列出的这几个 site_torrent_id（逗号分隔）。"
                         "「先试发两条」用这个 —— 不设它就会改全部 266 条，"
                         "那 266 条会一次性进发布队列。")
    ap.add_argument("--db", default=LOCAL_DB)
    args = ap.parse_args()

    keep = tuple(s.strip() for s in args.keep.split(",") if s.strip())
    only = tuple(s.strip() for s in args.only.split(",") if s.strip())

    # ★★ 写库前的守卫一律放最前面（顺序错了就成了"先写、再报拒"）
    if args.apply and not args.i_have_stopped_the_container:
        sys.exit("❌ 拒绝写库：必须同时给 --i-have-stopped-the-container（纪律 1）")

    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA query_only=1" if not args.apply else "PRAGMA query_only=0")

    total = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    exists = conn.execute(
        "SELECT count(*) FROM tasks WHERE site_status='EXISTS'").fetchone()[0]
    # ★ 口径必须与 `select_targets` 一致，否则下面那个"= EXISTS − 保留"是错的减法。
    #   （2026-09-26 加 `status='SUCCESS'` 后，`EXISTS` 与"要改的"之间多了一层过滤。）
    exists_success = conn.execute(
        "SELECT count(*) FROM tasks WHERE site_status='EXISTS' "
        "AND status='SUCCESS'").fetchone()[0]
    targets = select_targets(conn, keep, only)

    print("=" * 74)
    print("★ 把「站点上已被删除」的 site_status 改回 UNKNOWN（**不删行**）")
    print("=" * 74)
    print(f"  库              : {args.db}")
    if only:
        print(f"  ★ 只处理(--only) : {list(only)}")
    else:
        print(f"  保留（站点还在）: {list(keep)}")
    print(f"  总行数          : {total}")
    print(f"  site_status=EXISTS: {exists}")
    print(f"  ├ 其中 status=SUCCESS: {exists_success}"
          f"  ← ★ 只有这部分会进发布队列")
    print(f"  └ 其余 {exists - exists_success} 条 status 不是 SUCCESS"
          f"  ← ★ **本次不碰**（改了也不会重发，只会白丢站点记录）")
    print(f"  ★ 本次要改        : {len(targets)}"
          + (f"  （--only 指定）"
             if only else f"  （= {exists_success} − 保留 {len(keep)}）"))

    if not targets:
        print("\n✅ 没有要改的。")
        conn.close()
        return 0

    print(f"\n【改动内容（**只这两列**）】")
    print(f"    site_status      : 'EXISTS' → 'UNKNOWN'")
    print(f"    site_torrent_id  : <旧值>  → ''")
    print(f"\n【★ 明确**不碰**的列】")
    print(f"    status / log_content / content_path / opencd_tid / updated_at …")
    with_log = sum(1 for t in targets if (t[4] or 0) > 0)
    with_cp = sum(1 for t in targets if t[5])
    with_tid = sum(1 for t in targets if t[6])
    print(f"\n【这些行上还有什么（= 为什么不能 DELETE）】")
    print(f"    有 log_content 的 : {with_log}/{len(targets)}")
    print(f"    有 content_path 的: {with_cp}/{len(targets)}")
    print(f"    有 opencd_tid 的  : {with_tid}/{len(targets)}  ← 刚回填的")

    print(f"\n【样本 5 条】")
    for t in targets[:5]:
        print(f"    {t[0][:12]} sid={t[2]:<6} {t[7][:40]}")

    # ★★ 把连带后果**明确打出来**
    print("\n" + "─" * 74)
    # ★ 2026-09-26：加 `status='SUCCESS'` 过滤后，**目标集本身就是发布队列的
    #   子集**（只差 `site_status` 这一条）⇒ 下面这句话现在是**精确的**：
    #   要改的 N 条 == 会重发的 N 条。改口径之前这里会**夸大**（把 PUBLISHED
    #   也算进去），而用户正是拿这句话决定要不要按 `--apply`。
    print("★★ 连带后果：改完后这 %d 条会满足 PENDING_PUBLISH_WHERE" % len(targets))
    print("   （status='SUCCESS' AND (site_status IS NULL OR site_status != 'EXISTS')）")
    print("   ★ 已按同口径过滤 ⇒ **要改的条数 = 会重发的条数**（不再夸大）")
    print("   ⇒ **publish 会把它们重新发到站点**（每轮 PUBLISH_MAX_PER_CYCLE 条）")
    # ★★ 这里**按生效的真的值**报，不写死 —— 2026-09-24 上限从 5 改成 1，
    #   原来的 `if len(targets) > 5:` / `一天可发 240 条` 就是**必然过时的**两处
    #   （`ERR-META-03`：写死的数字不报错，只给出一句看着可信的错话）。
    #   ★ 读法与 `daemon.py` 一致（`env_int`，兜底 0 = 不限）——
    #     不在这里另定义一套口径。读不到（如本机跑）就**报"读不到"**，不猜。
    _raw = os.getenv("PUBLISH_MAX_PER_CYCLE")
    try:
        _per_cycle = int(float(_raw)) if _raw not in (None, "") else None
    except ValueError:
        _per_cycle = None
    _iv_raw = os.getenv("PUBLISH_INTERVAL", "1800")
    try:
        _rounds = int(86400 / int(float(_iv_raw)))
    except (ValueError, ZeroDivisionError):
        _rounds = None

    if _per_cycle is None:
        # ★★ 2026-09-26 修：这里原先写「按 daemon 的兜底 **0 = 不限** 理解：
        #    **整批一轮里全发出去**」—— **那是替用户猜了一个数，而且猜错了**。
        #    实测（本机跑）`PUBLISH_MAX_PER_CYCLE` 读不到，而**生产真值是 1**
        #    （在容器快照 `/data/daemon_env.json` 里，本机 `os.getenv` 当然读不到）
        #    ⇒ 提示把「跨 ~5 天慢慢发」说成了「一轮全发」，**两个相反的结论**。
        #    ★ 用户正是拿这句话决定要不要按 `--apply` 的 ⇒ 猜错在这里代价最大。
        #    ★ 纪律（`ERR-META-14`：来源为空不许印成"映射成功"）：
        #      **读不到就说读不到**，并指出**去哪读真值**，绝不给一个默认值当事实。
        _why = ("值不是数字" if _raw not in (None, "") else "未配 / 本进程读不到")
        print(f"   ★★ 每轮上限**读不出**（{_why}）⇒ **本条无法计算发完要多久。**")
        print("       ★ 别把「读不出」当成 0（不限）—— 那是**另一个值的含义**：")
        print("         0 = 一轮全发；读不出 = 本进程没拿到这个变量。")
        print("       ★ 真值在**容器快照** /data/daemon_env.json（daemon 每轮重写）：")
        print("         `python tools/nas_health.py` 的「【发布速率】」节会读它并算日上限。")
    elif _per_cycle == 0:
        print("   ⚠️ 每轮上限 = 0（不限）⇒ **整批一轮里全发出去**")
    elif len(targets) > _per_cycle:
        _cap = f"，一天约 {_rounds * _per_cycle} 条" if _rounds else ""
        print(f"   ★★ 条数 > 每轮上限({_per_cycle}) ⇒ 会**跨多轮连续发出去**{_cap}！")
        print("       想只试发几条，请用 `--only` **只改那几条**。")
    else:
        print(f"   ★ 条数 ≤ 每轮上限({_per_cycle}) ⇒ 一轮就会全发出去。")
    print("   ★ 若不想重发，那是**另一个决定**，本脚本不擅自做。")
    print("─" * 74)

    if not args.apply:
        print("\n💡 预览模式，未写库。确认后：")
        print("     python tools/reset_deleted_site_status.py --apply "
              "--i-have-stopped-the-container")
        conn.close()
        return 0

    # ---- 写 ----
    n = 0
    try:
        for t in targets:
            cur = conn.execute(
                "UPDATE tasks SET site_status='UNKNOWN', site_torrent_id='' "
                " WHERE info_hash=? AND site_status='EXISTS'", (t[0],))
            n += cur.rowcount
        conn.commit()
        print(f"\n💾 已改：{n} 条")
        print("   查后复核（应相等）:")
        after_exists = conn.execute(
            "SELECT count(*) FROM tasks WHERE site_status='EXISTS'").fetchone()[0]
        print(f"     site_status='EXISTS' : {exists} → {after_exists}"
              f"   （应 = {exists} - {n} = {exists - n}）")
        print(f"     总行数仍为          : "
              f"{conn.execute('SELECT count(*) FROM tasks').fetchone()[0]}"
              f"   （应 = {total}，**一行都没删**）")
        # ★ 复核 protected 列
        still = conn.execute(
            "SELECT count(*) FROM tasks WHERE info_hash=? AND log_content IS NOT NULL "
            "AND log_content != ''", (targets[0][0],)).fetchone()[0]
        print(f"     log_content 仍保留  : {'✅' if still else '❌'}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
