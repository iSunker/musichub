"""★ 只读：把「皇后页面 id」映射到本地 tasks.opencd_tid（**默认不写库**）。

═══════════════════════════════════════════════════════════════════════════
为什么要有它
═══════════════════════════════════════════════════════════════════════════
`opencd_tid` 全库为 0 ⇒ `descr.py` 的三处「皇后路」分流（`:443` 副标题、
`:639` 的 `OpenCD` 标签、`:539/623/634` 的媒介/製作組/标签）**一次都走不到**，
而那四个字段（`opencd_tid`/`artist`/`year`/`mapped_json`）也从没被写过。
⇒ 发布出去的内容六个字段全错，且是**静默退回猜**（见 `descr.py:617-621` 自己的警告）。

正规补法本该是打开 `crawl` 抓皇后页面。但**现在有一条不需要抓取的权威来源**：
另一个项目（vertex2）的库里存着皇后的 `details.php?id=`，而它与我们**按 infohash 可配对**。

★ 为什么用 infohash 而不是标题：
  · infohash 是**内容哈希**，同一份内容在两边必然同值 —— 绑定不可伪造；
  · 标题会被站点改写（我们的 `黄清元 - 金钻名曲珍藏 FLAC 1998 - FLAC 整轨 - OpenCD`
    在皇后是 `黄清元 - 金钻名曲珍藏 1998 - FLAC 分轨`）⇒ **按标题匹配必错**。
  · 这正是 `ERR-SITE-03`：同一个 infohash 在两个站点上是**两个号段**的页面编号，
    按 id 配对更是必然错位（实测两边号段无交集）。

═══════════════════════════════════════════════════════════════════════════
三条纪律
═══════════════════════════════════════════════════════════════════════════
1. **默认只读** —— 不带 `--apply` 绝不写库。预览会逐条打印「本地名 / 皇后名 / 两个号段」，
   让人**肉眼过一遍**再决定。`--apply` 要同时给 `--i-have-read-the-preview`。

2. **只 UPDATE 一列** —— 回填只碰 `opencd_tid`。**绝不**调 `save_task()`
   （那会 upsert 整行、冲掉第一阶段的 `content_path` / `log_content`，见 `opencd_ingest` 的 ⑥ 号测试）。
   同理**不碰** `site_torrent_id` / `site_status`（那一列的可信度另案，见 `ERR-SITE-03`）。

3. **配不上就报出来，绝不编** —— 用标题去凑、用号段去猜，都会凭空造出一个**看起来对**的 id，
   而 `descr.py` 读了它会静默走错分支。⇒ 未命中就是未命中，逐条打印，不进库。

═══════════════════════════════════════════════════════════════════════════
★ 与生产模块的分工（`sync_opencd_tid.py`，根目录）—— 两者语义**刻意不同**
═══════════════════════════════════════════════════════════════════════════
| | 本文件（`tools/`） | `sync_opencd_tid.py`（根目录） |
|---|---|---|
| 定位 | **人监督的一次性 / 取证工具** | **每日自动**（daemon 第 7 阶段） |
| 进生产？ | ❌ `tools/` 既不在 Dockerfile 也不在 `deploy.sh` 白名单 | ✅ 打进镜像 |
| WHERE | **只填空白**（2026-09-23 收紧） | **只填空白**（同） |
| 预览 | 逐条打印本地名/皇后名/两个号段 | 只打汇总 |
★ 为什么两份：`tools/` 的脚本**上不了生产**（会把部署面搞乱），所以生产必须有自己的实现。
★ **同一套 join 有两份实现 ⇒ 会漂移**，而漂移是静默的（`ERR-META-03` 一族）。
★ **交叉核对（改 join 后必须做）**：两边应报出相同的
  「可配对 / 需回填 / 已是该值 / 未命中 / 冲突」五个数。

用法（本机只读）:
    python tools/backfill_opencd_tid.py                       # 预览（默认）
    python tools/backfill_opencd_tid.py --json out.json       # 预览 + 存映射
    python tools/backfill_opencd_tid.py --apply --i-have-read-the-preview
"""
import argparse
import json
import re
import sqlite3
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

# ★ 两个库都在 NAS 上，走 SMB **只读**直连。
#   ★★ 不 cp —— 见 ERRATA.md 的 ERR-SQL-01（cp 会丢 WAL 里未 checkpoint 的数据）。
VERTEX_DB = r"//iSunker-DS423/docker_ssd/vertex2/vertex/db/sql.db"
LOCAL_DB = r"//iSunker-DS423/docker_ssd/opencd2others/data/data.db"

# 只认皇后这一家的记录（vertex 库里还有 ourbits / hdhome 等别的站）
QUEEN_TRACKER = "tracker.open.cd"
TID_RE = re.compile(r"details\.php\?id=(\d+)")


def load_queen_index(path=VERTEX_DB):
    """
    读 vertex 库 → {infohash: {"tid","name","size","add"}}, {infohash: [候选 tid...]}。

    ★ 同一 hash 出现在多条记录里时（实测 26/557 如此，都是同一资源的重复入库），
      **不静默挑一条**：全收进 `conflicts` 交给人看。若所有候选 tidy 一致，
      那就不算冲突（取谁都一样）。
    """
    con = sqlite3.connect(path)
    con.execute("PRAGMA query_only=1")          # ★ 只读，绝不写别人家的库
    idx, cand, conflicts = {}, {}, {}
    rows = con.execute(
        "SELECT link, hash, name, add_time, size FROM torrents WHERE tracker = ?",
        (QUEEN_TRACKER,)).fetchall()
    con.close()

    total = 0
    for link, h, name, add, size in rows:
        m = TID_RE.search(link or "")
        if not m:
            continue                     # 没有详情页链接 = 映射不到，跳过（不编）
        total += 1
        key = (h or "").lower()
        if not key:
            continue
        tid = int(m.group(1))
        cand.setdefault(key, set()).add(tid)
        # 同 hash 重复入库时留 add_time 最新的那条当展示用
        if key not in idx or (add or 0) > (idx[key]["add"] or 0):
            idx[key] = {"tid": tid, "name": name, "size": size, "add": add}

    for key, tids in cand.items():
        if len(tids) > 1:
            conflicts[key] = sorted(tids)
    return idx, conflicts, len(rows), total


def load_local(path=LOCAL_DB):
    con = sqlite3.connect(path)
    con.execute("PRAGMA query_only=1")
    rows = con.execute(
        "SELECT info_hash, status, site_status, site_torrent_id, opencd_tid, "
        "formatted_title, torrent_name, content_path, log_content, artist, year, "
        "mapped_json FROM tasks").fetchall()
    con.close()
    return rows


def build(rows, idx):
    """切成 (可回填, 已是同值, 未命中) 三堆。冲突单独返回。"""
    todo, same, miss = [], [], []
    for r in rows:
        key = (r[0] or "").lower()
        if key not in idx:
            miss.append(r)
            continue
        want = str(idx[key]["tid"])
        if (r[4] or "").strip() == want:
            same.append(r)
        else:
            todo.append((r, want))
    return todo, same, miss


def preview(todo, same, miss, idx, conflicts, diag):
    n_raw, n_link = diag
    print("=" * 78)
    print("★ 只读预览：皇后页面 id → tasks.opencd_tid（**本模式不写任何东西**）")
    print("=" * 78)
    print(f"  皇后库记录数              : {n_raw}（其中带详情页链接 {n_link}）")
    print(f"  可配对（按 infohash）     : {len(todo) + len(same)}")
    print(f"  ├ 需回填                  : {len(todo)}")
    print(f"  └ 已是该值（幂等，跳过）  : {len(same)}")
    print(f"  未命中                    : {len(miss)}")
    print(f"  ★ infohash 冲突（多个 tid）: {len(conflicts)}")

    if todo:
        print(f"\n【按 site_status 拆·需回填】")
        for k, n in sorted(Counter((r[2] or "NULL") for r, _ in todo).items()):
            print(f"    {k:<10} {n}")
        print(f"\n【按 status 拆·需回填】")
        for k, n in sorted(Counter(r[1] for r, _ in todo).items()):
            print(f"    {k:<14} {n}")

        print(f"\n【opencd_tid 现状·需回填的行】")
        c = Counter("空" if not r[4] else "有别的值" for r, _ in todo)
        print(f"    {dict(c)}")

        # ★ 这两条是「回填会不会闯祸」的判据
        with_stage1 = [r for r, _ in todo if r[7] or r[8]]
        wrong = [(r, w) for r, w in todo if (r[4] or "").strip()]
        print(f"\n★ 覆盖风险：需回填的行里带 content_path/log_content 的 {len(with_stage1)} 条"
              f"（本工具只 UPDATE 一列 ⇒ 不受影响）")
        if wrong:
            print(f"★ ★ opencd_tid 已有值且与皇后不一致的 {len(wrong)} 条 ——"
                  f"**这次不覆盖**，先报出来：")
            for r, w in wrong[:10]:
                print(f"      {r[0][:12]} 现值={r[4]!r} 皇后={w}  {(r[5] or '')[:44]}")

    print(f"\n【样本：需回填的前 12 条（本地名 vs 皇后名 vs 两个号段）】")
    for r, want in todo[:12]:
        print(f"  {r[0][:12]} {r[2]:<8} site_torrent_id={str(r[3] or '-'):<6} → opencd_tid={want}")
        print(f"       本地: {(r[5] or r[6] or '')[:62]}")
        print(f"       皇后: {(idx[(r[0] or '').lower()]['name'] or '')[:62]}")

    print(f"\n【未命中 {len(miss)} 条（**不回填**，逐条报出）】")
    for r in miss[:15]:
        print(f"  {r[0][:12]} site={r[2]:<8} sid={str(r[3] or '-'):<6} {(r[5] or r[6] or '')[:52]}")
    if len(miss) > 15:
        print(f"  ... 另有 {len(miss) - 15} 条")
    print(f"  ★ 其中带 site_torrent_id 的 {sum(1 for r in miss if r[3])} 条")

    if conflicts:
        print(f"\n【★ infohash 冲突明细（**这 {len(conflicts)} 条不自动选**，需人工定夺）】")
        for k, tids in list(conflicts.items())[:10]:
            print(f"   {k[:16]} 候选 tid={tids}")


def apply_backfill(todo, local_db=LOCAL_DB):
    """
    ★ 只 UPDATE `opencd_tid` 一列。

    刻意**不**用 `db.save_task()`：那是 upsert，会按 `info_hash` 整体覆盖，
    把第一阶段的 `content_path` / `log_content` 冲掉
    （`opencd_ingest.py` 的 ⑥ 号自测正是钉这个）。
    也**不**碰 `site_torrent_id` / `site_status` —— 那两列的可信度是另一个案子。

    ★★ **不碰 `updated_at`**（2026-09-23 定）：`db.STALE_CONDITION` 是
       `(site_synced_at IS NULL OR updated_at > site_synced_at)`，而它是 `edit` 阶段的
       探针（`db.count_stale()`）。顺手刷 `updated_at` ⇒ 这 430 条**全部**变 stale
       ⇒ `edit` 每轮把整站页面重拉一遍（实测过的空转）。
       ⇒ 想让改动**在站点上生效**是**另一件事**：那要显式地把该重推的行标 stale，
         然后让 `edit` 正常跑。**那是独立的、可计数的一步，不是回填的副作用。**

    ★★ **只填空白**（2026-09-23 收紧，原为 `OR opencd_tid != ?`）：
       生产模块 `sync_opencd_tid.py` 是**每日自动**跑的，必须幂等且绝不覆盖；
       本文件保留为**人监督的一次性/取证工具**，所以两者语义**刻意不同** ——
       见本文件头部「与生产模块的分工」一节。
    """
    import db as project_db          # 借用它的 connect()（路径/超时口径一致）
    conn = project_db.connect()
    n = 0
    try:
        for r, want in todo:
            cur = conn.execute(
                "UPDATE tasks SET opencd_tid = ? WHERE info_hash = ? AND "
                "(opencd_tid IS NULL OR opencd_tid = '')",
                (want, r[0]))
            n += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return n


def main():
    ap = argparse.ArgumentParser(description="只读预览 / 回填 tasks.opencd_tid（皇后页面 id）")
    ap.add_argument("--apply", action="store_true",
                    help="★ 真正写库（只 UPDATE opencd_tid 一列）")
    ap.add_argument("--i-have-read-the-preview", action="store_true",
                    help="按纪律要求，--apply 必须显式声明已看过预览")
    ap.add_argument("--json", metavar="PATH", help="把映射写成 JSON（供人工核对/留档）")
    ap.add_argument("--vertex-db", default=VERTEX_DB)
    ap.add_argument("--local-db", default=LOCAL_DB)
    args = ap.parse_args()

    # ★★ 写库前的守卫一律放在**最前面** —— 顺序错了就成了「先写、再报拒」。
    #    （这个 bug 是提交前的自检抓到的：当时守卫写在 apply_backfill() 之后。）
    if args.apply and not args.i_have_read_the_preview:
        sys.exit("❌ 拒绝写库：必须同时给 --i-have-read-the-preview（纪律 1）")

    idx, conflicts, n_raw, n_link = load_queen_index(args.vertex_db)
    rows = load_local(args.local_db)
    todo, same, miss = build(rows, idx)

    preview(todo, same, miss, idx, conflicts, (n_raw, n_link))

    if args.json:
        payload = {
            "source": args.vertex_db,
            "queen_tracker": QUEEN_TRACKER,
            "todo": [{"info_hash": r[0], "opencd_tid": w,
                      "local_title": r[5] or r[6],
                      "queen_title": idx[(r[0] or "").lower()]["name"]} for r, w in todo],
            "unmatched": [{"info_hash": r[0], "title": r[5] or r[6]} for r in miss],
            "conflicts": conflicts,
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"\n💾 映射已写出：{args.json}")

    if not args.apply:
        print("\n💡 预览模式，未写库。确认无误后："
              "`--apply --i-have-read-the-preview`")
        return

    if not todo:
        print("\n✅ 无需回填（全部已是目标值）。")
        return
    n = apply_backfill(todo, args.local_db)
    print(f"\n💾 已回填 opencd_tid：{n} 条")


if __name__ == "__main__":
    main()
