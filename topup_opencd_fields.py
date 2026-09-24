"""★ 按 tid 直取详情页，**只抓一小批**，把「皇后行」的内容补完整并入库。

═══════════════════════════════════════════════════════════════════════════
它解决的那个具体问题
═══════════════════════════════════════════════════════════════════════════
库里 430 条 `opencd_tid` 非空的行里，**428 条 `mapped_json` 是空的** ⇒
它们发出去的内容**六个字段全错**（副标题是「全 N 曲」、标签只剩 `OpenCD`、
媒介/製作組是猜的、artist/year 空），而且 `db.PENDING_PUBLISH_WHERE` 的**闸门③**
正好会拦它们（"想去读皇后却没读"）。

要补就得**读页面**。但 `crawl.py` **不能**用来干这件事：
  · 它只有"**我已知 tid，抓那一个**"这一种用法（`--tid` / `--url`），
    **不认批**、也不自己选哪几条 ⇒ 430 条得人肉敲 430 次命令行；
  · 它按单条落盘（`raw_html/<tid>.html`），**没有台账** ——
    重跑一次就是"再抓一遍已经抓过的"，而抓页面**花配额**（不可退）。

★★ 2026-09-23 订正：上面这两条原先写的是「`crawl.py --from-qb` 的 `known` 判据」云云 ——
  那条路（扫 qB → **按种子名称搜索**皇后）**已整条删除**（用户决定；按 infohash 从
  VT 库拿 id 实测命中 430/547，按名字只有 1/547）。但这两条缺点**换到新入口上依然成立**
  （单条 vs 成批、无台账），所以本脚本仍然是必要的 —— 它才是那个"选哪几条 + 台账"的层。

本脚本走的是 `crawl.py` 已有的**手工路**：`fetch_detail()` → `opencd_ingest.ingest()`。
那两步**本来就是分开的**（模块头写明"抓取与入库是两件事"），本脚本只是把它们
**按批串起来**，并且把"抓哪几条"变成一个**可控的**决定。

═══════════════════════════════════════════════════════════════════════════
八条纪律
═══════════════════════════════════════════════════════════════════════════
1. **默认只读**（`--dry-run` 是默认值）：不带 `--apply` 只打印"会抓哪几条"。
   ★ 这一条在**这里**格外重要：抓页面会**花配额**（不可退），入库会**改库**。

2. ★★ **每个请求都过同一个 `RateGate`** —— 复用 `crawl.Client`，
   **不自己写 `requests`**。`crawl.Client.get()` 是**唯一**发请求的出口，
   配额账本（`data.db:opencd_quota`）与最小间隔都在那儿。
   ★ 绕过它的请求 = 违规（`opencd_rate.py` 模块头成文纪律）。

3. ★★ **持久游标**（本脚本存在的主要理由）：
   选批按 `(fetched_at 为空的最先, opencd_tid)` 排序，而**入过库的行 `fetched_at`
   就有值了** ⇒ 这个序**天然自推进**，不会每轮重抓同一批。
   ★ 但**没有落盘的行**（比如抓到了但入库失败）不会被任何字段记住 ⇒
     用一个**只增的小台账文件** `raw_html/.topup_done.jsonl` 记"这次想抓哪些"，
     下一轮把已经试过的**跳过**（除非 `--retry-failed`）。
   ★ 为什么不复用 `opencd_quota` 那张表：它是**配额账本**（一行一天一个数），
     语义是"发了几个请求"；把"抓过哪个 tid"塞进去会让两个口径混在一列里
     （`ERR-META-05`：不同口径的数不许放在一起）。

4. **配额与"没活"必须分开报**（`crawl.py` 同款纪律）：
   `RateGate` 抛 `DailyQuotaExceeded` 是**保护**，不是错误；
   摘要里"本轮放入 N 条"与"被配额截断到 M 条"要**分别可见**。

5. ★ **绝不碰 `status` 之外的东西** —— 入库走 `opencd_ingest.build_row()`，
   它**刻意不写** `content_path` / `cover_url` / `torrent_file`
   （见它的 docstring：`content_path` 是"第一阶段行的标志字段，永不可碰"）。
   ★ 本脚本**连 `torrent_file` 都不传**：那些行的 `.torrent` 在 qB 那边才有，
     而**重建 `.torrent` 会换 announce passkey ⇒ 改 infohash**（严禁）。
     ⇒ 不传 `torrent_file` ⇒ 落 `NO_TORRENT_FILE`。**这是预期行为**，不是失败。

6. ★ **`status` 会变成 `NO_TORRENT_FILE`，这是本脚本的已知副作用**：
   原因见纪律 5。⇒ 要发这些行时得**显式**把 `status` 改回 `SUCCESS`
   （那是**另一件独立的事**，且**需要用户决定发哪些**）。本脚本不做那件事。
   ★ 好处：内容（`mapped_json`/`artist`/`year`/`transfer_rule`）**已经补全了**，
     将来要发只需改一个 `status`，**不用再花请求**。

7. **单条失败不拖垮整批** —— 每条 `try/except`，失败只计数 + 打日志 + 进台账。

8. ★★ **只补「补了有用」的行**（2026-09-23 深夜新增，见 `TARGET_WHERE` 上方那段）：
   批量路**排除 `site_status='EXISTS'`**（已上站）—— 它们到不了发布队列，
   补 `mapped_json` 是**无效功**，而请求**不可退**。
   ⇒ 实测的教训：改之前，399 条里**排最前的绝大多数是 EXISTS**，
     每天 50 个请求全花在够不着的行上，**队列因此长期是 0**。
   ★ 要**重推已上站的行**：用 `--tids` 显式点名（默认拒绝 + 显式授权）。
   ★ 两个口径（全部 / 可补）在摘要里**分开报**，不许混着减（`ERR-META-05`）。

═══════════════════════════════════════════════════════════════════════════
在哪跑
═══════════════════════════════════════════════════════════════════════════
★ **容器内**（皇后 Cookie 在 NAS 的 `.env` 里，本机读不到 —— `ERR-ENV-02`）。
  ⇒ 用法（NAS 上，**先停掉常驻 daemon 或至少确认它不在跑 crawl**）：
      sudo docker compose run --rm tools python topup_opencd_fields.py --dry-run
      sudo docker compose run --rm tools python topup_opencd_fields.py --apply --limit 5

★★ `--limit` 的**兜底默认是 5**，不是 50：用户明确要"手动发十来个**看看效果**"
   ⇒ 默认值该偏向**小**。（`OPENCD_MAX_PER_DAY=50` 是硬刹车，不是目标。）

用法:
    python topup_opencd_fields.py --dry-run                # 只看会抓哪几条（默认）
    python topup_opencd_fields.py --apply --limit 5        # 真抓 5 条
    python topup_opencd_fields.py --apply --limit 0        # 不限（**只受配额**）
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
import db
import opencd_ingest
import opencd_parse
import opencd_rate

import crawl  # ★ 复用它的 Client / fetch_detail / RAW_DIR —— **不重造一条出网路**

# ── 台账（只增，落在落盘目录旁边，不进镜像、不进 deploy 白名单）──────────
TRIED_FILE = "topup_tried.jsonl"

# ★ 目标口径（**唯一来源**，别在别处再写一份 —— 本项目的成文纪律）。
#   · `opencd_tid` 非空  ⇒ 有页面可读（`.torrent` / 封面都不需要）
#   · `mapped_json` 空   ⇒ 还没补过（补过的 2 条不在内）
#   ★ **故意不按 `status='SUCCESS'` 筛**：用户先要"有 log 的 248 条"，
#     但那 248 条已经 `SUCCESS` ⇒ 入库后仍会**原样保持 SUCCESS**；
#     而没 log 的 180 条会落 `NO_LOG`（第一代规矩：不发）。
#     ⇒ 一次跑完两类，各自的结论由 `opencd_ingest` 分别落库，不必分两次排队。
#
# ★★ 2026-09-23 深夜修：**"还没补过" ≠ "补了有用"** —— 加 `site_status != 'EXISTS'`。
#
#   ★ 起因（实测，2026-09-23 23:0x 的 `--dry-run` 输出）：本口径当时选中 **399 条**，
#     而**真正花请求的前 8 条 `site_status` 全是 `EXISTS`**（已上站）。
#     ⇒ **它们在给"已在站上、永远不会重进队列"的行花配额。**
#
#   ★ 为什么 EXISTS 补了也没用（不是"觉得"）：
#     `db.PENDING_PUBLISH_WHERE` 的第二句就是
#     `(site_status IS NULL OR site_status != 'EXISTS')` ⇒ **已上站的行走不到队列**。
#     而补 `mapped_json` 唯一的**目的**就是让行能过闸门③ 进队列
#     （见本模块头纪律 5/6：补完还要**另一步**才能发）。
#     ⇒ 对 `EXISTS` 的行，这个目的是**不可能达成**的 ⇒ 请求**纯浪费**。
#
#   ★★ 更要命的是**排序的连带效应**：选批排序键把 `status='SUCCESS'` 排前
#     （用户点名的"有 log 优先"），而 `SUCCESS` 与 `EXISTS` **高度重合**
#     ⇒ 排在最前的那 219 条里绝大多数是 EXISTS ⇒ **真正该补的
#     （未上站、有 tid、没页面，实测只有几十条）被挤到 399 条的最后**
#     ⇒ **每天的 50 个请求全花在够不着的行上，队列永远是 0。**
#     ★ 这是典型的 `ERR-META-01` 形状：脚本报告"在干活、选了 399 条"，
#       而**干的全是无效功** —— 比没在跑更糟，因为它看起来在跑。
#
#   ★★ 要**重推已上站的行**怎么办（不是禁止，是**必须显式**）：
#       用 `--tids` **点名**（它本来就是"收窄"设计）⇒ 下面 `PICK_WHERE` 只在
#       **批量路**上排除 EXISTS；点名路仍按 `TARGET_WHERE` 判（可点 EXISTS 的行）。
#       ⇒ 「默认不浪费配额」+「要重推必须自己点名」，与闸门④ 的
#         "默认拒绝 + 显式授权"是同一个分寸。
#
# ★ 两个口径**都保留、分开报**（`ERR-META-05`：不同口径的数不许混着减）：
#     `TARGET_WHERE`    = 还没补过的**全部**（含已上站）→ 摘要里作"还剩多少活"
#     `PICK_WHERE`      = 其中**补了有用**的（未上站）    → 真正会进选批的池子
TARGET_WHERE = ("(opencd_tid IS NOT NULL AND opencd_tid != '') "
                "AND (mapped_json IS NULL OR mapped_json = '')")
# ★ 批量路的池子：在上面基础上**再排除已上站**（理由见上）。
#   ★ 别把它写进 `TARGET_WHERE` —— 那样 `--tids` 点名一条 EXISTS 的行时
#     会被判成"不在目标集里"，用户看到"打错了"，而其实是**口径拦的**
#     （这正是 `--tids` 注释里警告过的"三种原因要自己核"那种混淆）。
PICK_WHERE = (TARGET_WHERE + " AND (site_status IS NULL OR site_status != 'EXISTS')")


def load_tried():
    """读台账（试过的 tid 集合）。★ 读不到 = 空集，不是异常（文件本来就可能没有）。"""
    p = os.path.join(crawl.RAW_DIR, TRIED_FILE)
    if not os.path.exists(p):
        return {}
    out = {}
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
                out[str(o.get("tid"))] = o
            except ValueError:
                continue          # ★ 坏行跳过但**不静默**：下面会报行数差
    return out


def append_tried(rec):
    """追加一行台账（只增）。★ 追加写，不重写整个文件 ⇒ 崩了也不丢已记的。"""
    os.makedirs(crawl.RAW_DIR, exist_ok=True)
    p = os.path.join(crawl.RAW_DIR, TRIED_FILE)
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def select_batch(limit, tried, retry_failed=False, tids=()):
    """
    选批。★ 排序就是游标（★ 实测过的教训：第一版漏了「有 log 优先」，
    选出来 5 条里 4 条是没 log 的 —— 因为 `opencd_tid` 号段恰好把 NO_LOG 排在了前面，
    而用户明确要求「**先补 428 里面有 log 的**」）：

      ① ★★ **未上站的优先**（`site_status != 'EXISTS'`）—— **2026-09-23 深夜补**，
         ★ **这一条现在是第一优先级**（此前没有它，是那个"浪费配额"bug 的根源）。
         理由见 `TARGET_WHERE` 上方那段：已上站的行**到不了发布队列**
         （`PENDING_PUBLISH_WHERE` 排掉 EXISTS）⇒ 给它补 `mapped_json` 是**无效功**。
      ② ★ **有 log 的优先**（`status='SUCCESS'`）—— 用户点名的批次。
         判据用 `status` 而不是 `log_content <> ''`：实测两者在这批上**等价**
         （248 vs 248），而 `status` 不用把 248 份日志读出来比字符串。
      ③ 有落盘 HTML 的优先（**零请求**，先把白拿的吃完）
      ④ 再按 `opencd_tid` 升序 —— **确定性**（不依赖行序/时间戳漂移），
         而「补完就写 `fetched_at`」会让下一轮的 `mapped_json` 判据把它排除
         ⇒ **天然自推进**

      ★★ 关于①②的**次序**（为什么未上站要排在"有 log"**前面**）：
        两者冲突时该听谁的？——**听"补了有没有用"**。
        实测（2026-09-23）：`SUCCESS` 与 `EXISTS` **高度重合**，所以旧次序
        （log 优先）会把 EXISTS 全排前面 ⇒ 真正该补的排到 399 条末尾。
        ⇒ 「有用」是**前提**，「有 log」是**在有用的里面再挑**。
        ★ 这不违背用户"先补有 log 的"：有 log **且未上站**的仍在最前。

    ★★ `tids`（`--tids`）：**精确点名**，非空时**只在这个集合里选**。

      ★ 它是**收窄**，不是**绕过** —— 与 `publish.py --only` 同源设计：
        点名 ∧ 仍满足 `TARGET_WHERE` ⇒ 才处理。
        点名一条 `mapped_json` 已经非空的 ⇒ **它根本不在 `rows` 里** ⇒ 不会被
        "顺手重抓一遍"（这正是要防的形状：点名一个已补好的，结果白花一个请求）。
      ⇒ 返回的第三个值 `n_target` 仍是**全库目标行数**（`TARGET_WHERE` 口径，
        不受 `tids` 影响）—— 摘要里那个数是"还剩多少活"，不是"这轮点了几条"
        （`ERR-META-05`：两个口径不能混）。

      ★ 为什么需要它：批量路的游标是 `tid` 升序，**天然选不到指定行**；
        而"精确补这 3 条"和"每天补一批"是**两个不同的需求**
        （2026-09-23 实测：用户要补站上还在的那 3 条，`--limit 3` 只会取
         tid 最小的另外 3 条 —— 看着跑了，补的是别的行）。
    """
    conn = db.connect()
    try:
        # ★ `opencd_added_at` 必须一起选出来 —— 否则入库时传不了发布日期，
        #   那些行就会因为闸门② 的"空 ⇒ 拦"而**永远发不出去**（见 db.py 闸门②）。
        #
        # ★★ 两个口径**都要**（`ERR-META-05`：不同口径分开报，别混着减）：
        #   · `n_target_all` = `TARGET_WHERE`（**全部**还没补过的，含已上站）
        #     ⇒ 摘要里"还剩多少活"用这个 —— 它与用户脑子里的那个数一致。
        #   · `rows`         = 批量路用的池子：
        #        点名（`tids` 非空）⇒ **仍是 `TARGET_WHERE`**（点名可点 EXISTS 的行，
        #                              因为"显式点名"正是重推的正当路径）
        #        批量（`tids` 空）  ⇒ `PICK_WHERE`（**排除已上站**，不浪费配额）
        _cols = ("info_hash, opencd_tid, status, site_status, torrent_name, "
                 "opencd_added_at")
        n_target_all = conn.execute(
            f"SELECT count(*) FROM tasks WHERE {TARGET_WHERE}").fetchone()[0]
        n_pick_all = conn.execute(
            f"SELECT count(*) FROM tasks WHERE {PICK_WHERE}").fetchone()[0]
        _where = TARGET_WHERE if tids else PICK_WHERE
        _sql = f"SELECT {_cols} FROM tasks WHERE {_where}"
        rows = [dict(zip([d[0] for d in conn.execute(_sql).description], r))
                for r in conn.execute(_sql)]
    finally:
        conn.close()

    have = set(f[:-5] for f in os.listdir(crawl.RAW_DIR)
               if f.endswith(".html")) if os.path.isdir(crawl.RAW_DIR) else set()

    # ★ 点名收窄。必须在 sort/游标**之前**做 —— 否则 `limit` 会先被非点名的行占满。
    #   · `wanted_set` 收下点名集合，用于**报出"没命中"**的那些（不静默）
    #   · `rows` 之外的（已补好 / 无 tid）也算"没命中"，摘要里分开报
    wanted_absent = []
    if tids:
        want = {str(t).strip() for t in tids if str(t).strip()}
        hit = [r for r in rows if str(r["opencd_tid"]) in want]
        got = {str(r["opencd_tid"]) for r in hit}
        wanted_absent = sorted(want - got)
        rows = hit

    def key(r):
        tid = str(r["opencd_tid"])
        # ★★ 四级排序，**顺序就是优先级**（见 docstring）：
        #    ① 未上站（补了**有用**）—— 2026-09-23 深夜新增，现在是第一优先级
        #    ② 有 log（用户点名的批次）  ③ 已落盘（零请求）  ④ tid 确定性升序
        return (0 if (r["site_status"] or "") != "EXISTS" else 1,
                0 if r["status"] == "SUCCESS" else 1,
                0 if tid in have else 1,
                tid)

    rows.sort(key=key)

    picked, skipped = [], []
    for r in rows:
        tid = str(r["opencd_tid"])
        prev = tried.get(tid)
        # ★ 试过且**没落盘**的才跳过（落盘了就说明拿到了，走"零请求"那条）
        if prev and tid not in have and not retry_failed:
            if prev.get("ok"):
                continue          # 成功过但页面不见了 —— 罕见，留给 --retry-failed
            skipped.append((tid, prev.get("why", "")))
            continue
        picked.append(r)
        if limit and len(picked) >= limit:
            break
    return picked, skipped, n_target_all, n_pick_all, have, wanted_absent


def main():
    ap = argparse.ArgumentParser(
        description="按 tid 直取详情页、补全皇后行内容（分小批，过 RateGate）")
    ap.add_argument("--apply", action="store_true",
                    help="★ 真抓真入库。不加 = 只读预览（默认）")
    ap.add_argument("--limit", type=int, default=5,
                    help="本轮最多抓几条（默认 **5**；0 = 不限，只受配额约束）。"
                         "★ 抓一条 = 1 个皇后请求。")
    ap.add_argument("--tids", default="",
                    help="★ 精确点名这些 opencd_tid（逗号分隔），只补它们。"
                         "默认空 = 按优先级批量（现状）。"
                         "★★ 是**收窄**不是绕过：点名的行仍须满足目标判据"
                         "（有 tid 且 mapped_json 空）；已补好的点名也不会重抓。"
                         "与 --limit 可叠加：先收窄，再取前 --limit 条。")
    ap.add_argument("--retry-failed", action="store_true",
                    help="把台账里「试过但没成」的也重新排队（默认跳过它们）")
    args = ap.parse_args()

    os.makedirs(crawl.RAW_DIR, exist_ok=True)
    db.init_db()

    print("=" * 74)
    print("★ 按 tid 补全皇后行内容（分小批 · 每个请求过 RateGate）")
    print("=" * 74)
    print(f"  落盘目录  : {crawl.RAW_DIR}")
    print(f"  模式      : {'★ 真抓真入库' if args.apply else '只读预览（不发请求、不写库）'}")
    print(f"  本轮上限  : {args.limit if args.limit else '不限（只受每日配额约束）'}")

    # ★ `--tids` 解析：逗号分隔的 tid（**只用 tid**，不用 info_hash ——
    #   本脚本的选批与抓取全程按 tid 走，用 hash 会多一层不必要的映射）。
    want_tids = tuple(s.strip() for s in args.tids.split(",") if s.strip()) \
        if args.tids else ()

    tried = load_tried()
    picked, skipped, n_target, n_pick, have, absent = select_batch(
        args.limit, tried, args.retry_failed, tids=want_tids)

    print(f"\n  目标行数  : {n_target}（口径 = {TARGET_WHERE}）")
    # ★★ 两个口径**分开报**（`ERR-META-05`：不许混着减）。
    #   ★ 这一行就是那个"浪费配额"bug 的**可见化** —— 修之前，选批池子
    #     等于 `n_target`（399），而其中绝大多数是已上站的无效功。
    print(f"  ★ 其中可补（未上站·补了有用）: {n_pick}"
          f"（口径 = 目标 ∧ site_status != 'EXISTS'）")
    print(f"     └ 已上站（EXISTS）        : {n_target - n_pick}"
          f"   ← 这些**补了也进不了队列**（闸门② 排掉 EXISTS）⇒ 批量路**不选**它们；"
          f"要重推请用 `--tids` 点名")
    print(f"  已落盘页面: {len(have)}  ← ★ 这些**零请求**，排在最前")
    print(f"  台账试过  : {len(tried)}（本轮跳过 {len(skipped)} 条）")
    if want_tids:
        print(f"  ★ --tids  : 点名 {len(want_tids)} 个 ⇒ 命中 {len(picked)} 条")
        for t in absent:
            print(f"     ❌ tid={t} 不在目标集里"
                  f"（已补好 / 没有 tid / 打错了 —— 三者都要自己核，别默认是哪种）")
    print(f"  ★ 本轮选中: {len(picked)}")

    if skipped:
        print(f"\n  ★ 台账里「试过没成」而跳过的（要重试加 --retry-failed）：")
        for tid, why in skipped[:8]:
            print(f"     tid={tid:<9} {why[:60]}")
        if len(skipped) > 8:
            print(f"     ... 另有 {len(skipped) - 8} 条")

    if not picked:
        print("\n💡 没有要抓的（目标已清空 / 全在台账里）⇒ 什么都不做。")
        return 0

    # ★ 逐条打印"会抓什么"——这是预览的**主要内容**
    print("\n" + "-" * 74)
    print("① 本轮会处理的（逐条）")
    print("-" * 74)
    for i, r in enumerate(picked, 1):
        tid = str(r["opencd_tid"])
        free = "★ 零请求（已落盘）" if tid in have else "1 个请求"
        print(f"  [{i:>3}] tid={tid:<9} {free:<18} "
              f"{(r['torrent_name'] or '')[:46]}")
        print(f"        {r['info_hash'][:16]}  status={r['status']} "
              f"site_status={r['site_status']}")

    if not args.apply:
        print("\n" + "=" * 74)
        print("★ 只读预览到此（**没发任何请求、没写库**）。要真跑：")
        # ★★ 2026-09-23 修：提示行必须**回显用户实际给的全部参数**。
        #   原先写死 `--apply --limit N` —— 用户带 `--tids` 时，照抄这行会
        #   **丢掉点名**、退化成"按优先级批量"，于是**补的是别的行**（静默选错批，
        #   与 `select_batch` 排序键那个坑同族：工具跑得好好的，选中的集合不是要的）。
        #   ⇒ 施工纪律：**提示行由真参数拼出来**，不写死。
        _argv = ["python", "topup_opencd_fields.py"]
        if want_tids:
            _argv.append("--tids " + ",".join(want_tids))
        _argv.append("--apply")
        if args.retry_failed:
            _argv.append("--retry-failed")
        _argv.append(f"--limit {args.limit}")
        print("    " + " ".join(_argv))
        print("=" * 74)
        return 0

    # ── 真抓 ────────────────────────────────────────────────────────────────
    day_key = time.strftime("%Y-%m-%d")
    ledger = crawl._DbLedger()
    gate = opencd_rate.RateGate(
        max_per_round=int(os.getenv("OPENCD_MAX_PER_ROUND",
                                    crawl.DEFAULT_MAX_PER_ROUND)),
        min_interval=float(os.getenv("OPENCD_MIN_INTERVAL",
                                     crawl.DEFAULT_MIN_INTERVAL)),
        max_per_day=int(os.getenv("OPENCD_MAX_PER_DAY",
                                  crawl.DEFAULT_MAX_PER_DAY)),
        day_key=day_key, log=crawl.log, ledger=ledger)
    gate.roll_day(day_key)
    used_before = gate.used_day

    client = crawl.Client(gate=gate, dry_run=False)
    if not client.cookie:
        sys.exit("❌ 缺 OPENCD_COOKIE（在容器里的 .env 配）")

    # ★★ 2026-09-23 深夜修：**配额满要在发第一个请求之前就优雅退出**。
    #
    #   ★ 起因（实测）：今日已用 51/50 时跑 `--apply`，`check_login()` 里
    #     `gate.acquire()` 抛 `DailyQuotaExceeded` ⇒ 用户看到的是**一坨 traceback**，
    #     而真实语义是「今天额度用完了，明天再来」——**是保护，不是故障**。
    #     ★ 这是 `ERR-META-01` 的形状：**表现的语义与真实语义不符**。
    #       traceback 会让人以为脚本坏了、去查代码，而正确动作是"明天再跑"。
    #   ⇒ 分两步：
    #     ① 开工前先看**还有没有额度**（`used_day` 与账本比，不发任何请求）
    #     ② 若为 0 ⇒ 打印"明天再来" + **退出码 0**（不是错误）
    if gate.used_day >= gate.max_per_day:
        print(f"\n🛑 今日请求额度已用完（{gate.used_day}/{gate.max_per_day}）"
              f"—— **这是保护，不是错误**。")
        print(f"   配额账本在 data.db:{db.LEDGER_TABLE}，day_key={day_key}；"
              f"**明天（本地日期换天后）会自动重置**。")
        print(f"   ★ 不必改任何配置 —— 直接明天再跑同一条命令即可"
              f"（台账/游标会接着往下走）。")
        return 0

    try:
        ok, why = client.check_login()
    except opencd_rate.DailyQuotaExceeded as e:
        # ★ 兜底：开工前那个检查与本行之间**理论上**不会变（同一进程、无并发），
        #   但 `acquire()` 是唯一出口、它自己也会抛 ⇒ 这里**必须**接住，
        #   否则又变回 traceback（"响亮地坏"用在这里是**错的**：这不是坏，
        #   是正常的一天结束）。
        print(f"\n🛑 {e} —— 今日额度已用完（**保护，非错误**）。明天再跑即可。")
        return 0
    if not ok:
        sys.exit(f"❌ {why}")

    print(f"\n✅ 皇后 Cookie 有效 · 今日已用 {used_before}/{gate.max_per_day}"
          f"（★ 请求量纲）")

    stats = {"stored": 0, "not_seeded": 0, "no_log": 0, "denied": 0,
             "no_file": 0, "hash_mismatch": 0, "failed": 0, "cached": 0}
    tri = {}
    quota_hit = False

    print("\n" + "-" * 74)
    print("② 逐条抓取 + 入库")
    print("-" * 74)
    for i, r in enumerate(picked, 1):
        tid = str(r["opencd_tid"])
        title = (r["torrent_name"] or "")[:50]
        print(f"\n[{i}/{len(picked)}] tid={tid} {title}")
        try:
            info, cached = crawl.fetch_detail(client, tid)
        except opencd_rate.DailyQuotaExceeded as e:
            print(f"    🛑 {e} —— 本轮提前收工（这是**保护**，不是错误）")
            quota_hit = True
            break
        except Exception as e:
            stats["failed"] += 1
            print(f"    ❌ 抓取失败：{type(e).__name__}: {str(e)[:120]}")
            append_tried({"tid": tid, "ok": False, "why": f"fetch:{type(e).__name__}",
                          "at": datetime.now().isoformat(timespec="seconds")})
            continue

        if cached:
            stats["cached"] += 1
            print("    📦 本地已有落盘（零请求）")

        raw = info.get("transfer_rule_raw") or ""
        rule = info.get("transfer_rule") or opencd_parse.RULE_UNKNOWN
        tri[rule] = tri.get(rule, 0) + 1
        print(f"    转载规则 = {raw!r} → {rule}")

        # ★ 入库（纯本地、零请求）。**不传 torrent_file**（见纪律 5/6）
        try:
            st, reason = opencd_ingest.ingest(
                info, raw_tid=tid, info_hash=(r["info_hash"] or "").lower(),
                torrent_file=None, qb_seed_days=None, now=datetime.now(),
                # ★ 把库里已有的发布日期**原样带过去** —— `build_row` 在它为空时
                #   不会进 kwargs ⇒ `save_task` 的 upsert **不碰这一列** ⇒
                #   已有值不会被抹掉。（它是 `vtid` 阶段的产物，本步只负责别弄丢。）
                opencd_added_at=(r.get("opencd_added_at") or None))
        except Exception as e:
            stats["failed"] += 1
            print(f"    ❌ 入库失败：{type(e).__name__}: {str(e)[:120]}")
            append_tried({"tid": tid, "ok": False,
                          "why": f"ingest:{type(e).__name__}",
                          "at": datetime.now().isoformat(timespec="seconds")})
            continue

        print("    " + opencd_ingest.explain(st, reason))
        key = {opencd_ingest.STATUS_OK: "stored",
               opencd_ingest.STATUS_NOT_SEEDED: "not_seeded",
               opencd_ingest.STATUS_NO_LOG: "no_log",
               opencd_ingest.STATUS_DENIED: "denied",
               opencd_ingest.STATUS_NO_TORRENT_FILE: "no_file",
               opencd_ingest.STATUS_HASH_MISMATCH: "hash_mismatch"}.get(st)
        if key:
            stats[key] += 1
        append_tried({"tid": tid, "ok": True, "status": st,
                      "rule": rule,
                      "at": datetime.now().isoformat(timespec="seconds")})

    # ── 摘要 ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 74)
    print("③ ★ 摘要（各口径分开，不混着减 —— ERR-META-05）")
    print("=" * 74)
    print(f"  本轮选中      : {len(picked)}")
    print(f"  ├ 本地已有    : {stats['cached']}（零请求）")
    print(f"  ├ 新抓        : {len(picked) - stats['cached']}")
    print(f"  └ 抓取失败    : {stats['failed']}")
    print(f"  今日请求已用  : {gate.used_day}/{gate.max_per_day}"
          f"（本轮开始前 {used_before}）")
    if quota_hit:
        print("  ★★ 本轮**被每日配额截断**（不是「没活了」）—— 明天再跑，"
              "台账会让你接着往下走")
    print("\n  入库结论分布（★ 由 `opencd_ingest` 判，本脚本不改判）:")
    for k in ("stored", "no_file", "no_log", "not_seeded", "denied",
              "hash_mismatch"):
        if stats[k]:
            print(f"     {k:<14} {stats[k]}")
    print("\n  ★ 页面规则三态（**这是决定能不能发的真数据**）:")
    for k in (opencd_parse.RULE_ALLOW, opencd_parse.RULE_DENY,
              opencd_parse.RULE_UNKNOWN):
        print(f"     {k:<14} {tri.get(k, 0)}")

    print("\n" + "-" * 74)
    print("④ ★★ 下一步（**本脚本刻意不做**）")
    print("-" * 74)
    print("""  · 入库后这些行的 `status` 会变成 `NO_TORRENT_FILE`（原因见模块头纪律 5/6）
    ⇒ **想发它们必须显式把 `status` 改回 `SUCCESS`**，那是**另一件事**、
      且**需要你决定发哪些**（本脚本不会替你改 status）。
  · `mapped_json`/`artist`/`year`/`transfer_rule` 已经补好了 ⇒
     将来要发**不用再花请求**。
  · 发布队列的闸门④ 要求 `transfer_rule='ALLOW'` ⇒
     只有 ③ 里 `ALLOW` 的那批进得了队列；`NEED_CONFIRM`/`DENY` 的要你定。""")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
