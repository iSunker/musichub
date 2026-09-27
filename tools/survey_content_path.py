"""★ 只读测绘：算出「做种中」的种子**其 `content_path` 在盘上存不存在**的**基线**。

═══════════════════════════════════════════════════════════════════════════
它要回答的那个问题（2026-09-26，用户点头后写）
═══════════════════════════════════════════════════════════════════════════
起因：`tools/diag_17304.py` 在 `17304` 上量到 **两条** `content_path` 都不存在，
而**两条都是 `stalledUP`（在做种）**：

    69c5ee1b…  stalledUP 100%   content_path='/downloads/[260909] 緑黄色社会…'  → 实测不存在
    02afb787…  stalledDL 35%    content_path='/downloads/incomplete/[260909]…'  → 实测不存在

★ 我当时的第一反应是「100% 却不做种 ⇒ **假做种**、文件没了」。
★★ **那个结论我撤回** —— 因为**判据没验过**（`ERR-META-01`）：

    「多文件种子的 `content_path`」**到底该不该等于盘上那个目录**？
    `publish.py::readd_to_qb()` 的 docstring 记着一个**同一枚硬币的反面**：
    `contentLayout` 决定 qB **要不要把种子内的目录名补回文件路径前**
      · `NoSubfolder` ⇒ 不补 ⇒ qB 去 `/downloads/01.flac` 找，文件却在
        `/downloads/<专辑名>/01.flac` ⇒ `missingFiles`（那是**真错**）
      · `Original`    ⇒ 补   ⇒ 路径带上那层目录
    ⇒ ★★ 所以 **`content_path` 与盘上路径的关系，取决于那个种子的 layout** ——
      「不存在」**既可能是异常、也可能是正常形态**。**没测过就不能当判据。**

    ★ 这与 `ERR-META-22` 同族：**我拿一个"看着合理"的路径去量，却没验它是不是
      该量的那个**。本脚本就是为了**先把这个判据验一遍**。

═══════════════════════════════════════════════════════════════════════════
★★★ 本脚本**只测绘、不判定** —— 它的输出是**基线**，不是"异常清单"
═══════════════════════════════════════════════════════════════════════════
它会打印：

    【A】在「在做种」的种子（`stalledUP/uploading/forcedUP`）里**随机抽 N 条**
        逐条实测 `content_path` / `save_path` 在不在、里面几项、多大
    【B】★ **汇总基线**：抽样里 content_path 不存在的**比例**
    【C】★ 把**已知的两条**（17304 的两个 hash）**用同一套判据**量一遍，
        放在基线**旁边**对照 —— 这才是"它到底算不算异常"的答案

★ 为什么必须**抽样**而不是全扫 1444 条：
  全扫 = 对 1444 个路径做 `os.walk` —— 那要读遍**整个 2 TiB 库的目录树**，
  在 NAS 上是**几十分钟的 io 风暴**，而且**没有必要**：我们只需要一个比例。

★ 为什么必须**随机**而不是"挑几条看着可疑的"：
  挑可疑的 = 自己选样本 ⇒ 基线的**分母**是假的（`ERR-META-21` 的形状：
  在**被筛选过的集合**上算比例）。⇒ 用 `random.Random(seed)` **可复现地**抽。

═══════════════════════════════════════════════════════════════════════════
只读保证（逐条列出，便于核对）
═══════════════════════════════════════════════════════════════════════════
  qB 侧：只 `torrents_info()`（查询）—— ✗ 不 add ✗ 不 delete ✗ 不 recheck
         ✗ 不 pause ✗ 不改 category ✗ 不改 save_path
  库侧：★ **完全不读**（不 import db、不连 data.db）—— 同 `qb_state_probe.py` 纪律
  站点侧：★ **不联网**
  磁盘：只 `os.path.exists` / `os.path.isdir` / `os.scandir` / `os.stat`（只读）
         ✗ 不 mkdir ✗ 不 rename ✗ 不 rm

用法（在 NAS 上，compose 目录）:
    # ① 默认：抽 20 条做种中的，实测后给基线（★ 只读、零出网）
    sudo docker compose run --rm tools python /data/tools/survey_content_path.py

    # ② 换样本量（越大越准、越慢）
    sudo docker compose run --rm tools python /data/tools/survey_content_path.py --sample 50

    # ③ 换随机种子（换个样本看结论稳不稳 —— ★ 基线要"换个样本也成立"才算数）
    sudo docker compose run --rm tools python /data/tools/survey_content_path.py --seed 7

    # ④ 只测绘、不做对照（不查 17304 那两个 hash）
    sudo docker compose run --rm tools python /data/tools/survey_content_path.py --no-compare
"""
import argparse
import os
import random
import sys

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.environ.get("MUSOPIA_APP_DIR", "/app"))

import config  # noqa: E402

# ★ 做种中的 state（与 `readd_qb.py` / `qb_state_probe.py` 同口径，别各写一套）
SEEDING_STATES = ("stalledUP", "uploading", "forcedUP")

# ★★ 17304 的两个 hash（**已知**那一对）—— 用来把"抽样基线"和"已知样本"并排看
#   ★ 这里**写死常量**是有意的：本脚本**不读库**，所以不能从库里取。
#     它们是从 `diag_17304.py` 的输出里抄来的（那次的库读数在报告里）。
KNOWN = {
    "02afb7878cd831377da482a686835ae58ebaaeef": "17304 站点产物（stalledDL 35.46%）",
    "69c5ee1b969ce7d4f8dd5b32b39f6e63edbfaed4": "17304 皇后原种（stalledUP 100%）",
}


def human(n):
    """字节 → 人话。★ 只用 1024 进制（与 qB 的数字同量纲）。"""
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if x < 1024 or unit == "TiB":
            return f"{x:.2f} {unit}"
        x /= 1024
    return f"{x:.2f} TiB"


def du(path):
    """★ 只读：目录占多少字节、几个文件（os.walk 一遍）。"""
    total = nfiles = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.stat(os.path.join(root, f)).st_size
                nfiles += 1
            except OSError:
                pass
    return total, nfiles


def measure(t):
    """★ 对一条任务**只读**实测，返回一个结果字典。

    ⚠️ 三态纪律（`ERR-SQL-05`）：**"读不到"绝不并进"没有"**。
        · exists=False  ⇒ 判据成立，值就是"不存在"
        · 抛 OSError    ⇒ 判据**失效** ⇒ 标 `error`，**不许**算作"不存在"
    """
    r = {"hash": t.hash, "name": t.name, "state": t.state,
         "progress": t.progress * 100, "qsize": t.size,
         "content_path": t.content_path, "save_path": t.save_path}
    cp = (t.content_path or "").rstrip("/")
    r["cp"] = cp
    if not cp:
        r["verdict"] = "cp空"
        return r
    try:
        if not os.path.exists(cp):
            # ★★ 再探一层：`content_path` 不在，但**父目录/同名兄弟**可能在
            #    （这正是要区分的："路径形态不同" vs "文件真没了"）
            parent = os.path.dirname(cp)
            r["parent"] = parent
            r["parent_exists"] = os.path.exists(parent) if parent else False
            r["verdict"] = "cp不存在"
            return r
        if not os.path.isdir(cp):
            r["verdict"] = "cp不是目录"
            return r
        n_top = sum(1 for _ in os.scandir(cp))
        size, nfiles = du(cp)
        r["n_top"] = n_top
        r["nfiles"] = nfiles
        r["dsize"] = size
        r["verdict"] = "cp在"
        return r
    except OSError as e:
        # ★ 读不到 != 没有
        r["verdict"] = "读不到"
        r["err"] = f"{type(e).__name__}: {e}"
        return r


def one_line(r, width=58):
    """把一条结果打成人话一行。★ 名优先保 @Musopia（照抄 readd_qb 的教训）。"""
    nm = (r["name"] or "")[:width]
    mark = {"cp在": "✅", "cp不存在": "★★", "cp不是目录": "⚠️ ",
            "读不到": "❔", "cp空": "❔"}.get(r["verdict"], "?")
    print(f"  {mark} {r['hash'][:12]}…  {r['state']:<10} "
          f"{r['progress']:6.2f}%  {r['verdict']}")
    print(f"       name={nm!r}")
    print(f"       qB 说 size = {human(r['qsize'])}")
    print(f"       content_path = {r['cp']!r}")
    if r["verdict"] == "cp在":
        print(f"       磁盘实测     = {r.get('nfiles')} 个文件 / "
              f"{human(r.get('dsize', 0))}（顶层 {r.get('n_top')} 项）")
        # ★★ 关键对照：磁盘实测**是否 ≈** qB 说的大小
        qs, ds = r["qsize"] or 0, r.get("dsize") or 0
        if qs > 0:
            ratio = ds / qs
            if 0.95 <= ratio <= 1.05:
                print(f"       ⇒ ✅ 磁盘 ≈ qB 说的（{ratio * 100:.1f}%）")
            else:
                print(f"       ⇒ ★★ 磁盘 / qB = **{ratio * 100:.1f}%**"
                      f" —— 明显对不上（这是**内容**层面的判据）")
    elif r["verdict"] == "cp不存在":
        print(f"       ★ 父目录 {r.get('parent')!r} 存在？{r.get('parent_exists')}")
        # ★★ 再看一眼**父目录下有没有名字相近的兄弟** —— 这是区分
        #    「路径形态不同（正常）」与「文件真没了（异常）」的**唯一**现场办法。
        #    ★ 只列同级名字，不下钻（下钻可能撞大目录，见 --warn-du 的理由）。
        p = r.get("parent")
        if p and r.get("parent_exists"):
            base = os.path.basename(r["cp"])
            key = base.split("[")[0].strip()[:12] or base[:8]
            try:
                sibs = [e.name for e in os.scandir(p) if key and key in e.name]
            except OSError as e:
                print(f"       ❔ 列兄弟失败：{type(e).__name__}: {e}")
                sibs = None
            if sibs:
                print(f"       ★★ 父目录下有 {len(sibs)} 个名字相近的 ⇒ "
                      f"**路径形态不同，文件很可能在**：")
                for s in sorted(sibs)[:6]:
                    print(f"            · {s}")
            elif sibs is not None:
                print("       ★ 父目录下**没有**名字相近的项 ⇒ "
                      "★ 更像'文件真不在这一层'（但也可能改了名 ⇒ 别当定论）")
        print("       ★★ 注意：`content_path` 不存在**未必是异常** —— 见模块头那段")
        print("          （多文件种子的 layout 会让它不指到盘上那个目录）")
    elif r["verdict"] == "读不到":
        print(f"       ❔ {r.get('err')} ⇒ **判据失效，不许算作'不存在'**")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="★ 只读测绘：做种中的种子其 content_path 在盘上的**基线**")
    ap.add_argument("--sample", type=int, default=20,
                    help="抽多少条做种中的（默认 20）")
    ap.add_argument("--seed", type=int, default=20260926,
                    help="随机种子（默认 20260926；换它=换样本，看基线稳不稳）")
    ap.add_argument("--no-compare", action="store_true",
                    help="不做 17304 那两个 hash 的对照")
    ap.add_argument("--deep", action="store_true",
                    help="★ 对 content_path 存在的也做 os.walk 统计（慢；默认只数顶层项）")
    ap.add_argument("--warn-du", type=float, default=3000.0,
                    help="★ 抽样路径超过这么多个顶层项就跳过实测（防止撞上大目录；默认 3000）")
    args = ap.parse_args(argv)

    print("=" * 74)
    print("★ 只读测绘：做种中的种子 —— `content_path` 在盘上的**基线**")
    print("=" * 74)
    print("  ★ 本脚本**完全不写**：qB 只查询 / 磁盘只读 / **不读库** / **不联网**")
    print("  ★★ 它的输出是**基线**（一个比例），**不是**'异常清单'")
    print()

    try:
        from qbittorrentapi import Client
    except ImportError as e:                                   # noqa: BLE001
        print(f"  ✗ 没装 qbittorrentapi（{e}）")
        return 1
    try:
        qb = Client(host=config.QB_HOST, username=config.QB_USER,
                    password=config.QB_PASS)
        qb.auth_log_in()
    except Exception as e:                                     # noqa: BLE001
        print(f"  ✗ 连不上 qB（{config.QB_HOST}）：{type(e).__name__}: {e}")
        print("    ★ QB_HOST=127.0.0.1 ⇒ 只有**在容器内 / NAS 本机**连得上。")
        return 2

    try:
        all_t = list(qb.torrents_info())
    except Exception as e:                                     # noqa: BLE001
        print(f"  ✗ 查询失败：{type(e).__name__}: {e}")
        return 2

    seeding = [t for t in all_t if (t.state or "") in SEEDING_STATES]
    print(f"  qB 里共 {len(all_t)} 条；其中**在做种** {len(seeding)} 条"
          f"（state ∈ {SEEDING_STATES}）")

    # ── 分层的分母：★ 抽样**只在做种的那批**里抽（那才是"健康"参照）──────
    rnd = random.Random(args.seed)
    n = min(args.sample, len(seeding))
    sample = rnd.sample(seeding, n) if n else []
    print(f"  ★ 随机抽 {n} 条（seed={args.seed}，**可复现**）")
    print()

    print("─" * 74)
    print("【A】抽样明细（★ 这些是**在做种**的 —— 它们就是'健康'的参照）")
    print("─" * 74)
    results = []
    for i, t in enumerate(sample, 1):
        print(f"\n  [{i}/{n}]")
        # ★ 防撞大目录：先数顶层项，超阈值就只报数、不 os.walk
        cp = (t.content_path or "").rstrip("/")
        if cp and os.path.isdir(cp) and not args.deep:
            try:
                ntop = sum(1 for _ in os.scandir(cp))
                if ntop > args.warn_du:
                    print(f"  ⏭️  {t.hash[:12]}… 顶层项 {ntop} > {args.warn_du:g}"
                          f" ⇒ **跳过 os.walk**（太大，会变成 io 风暴）")
                    r = {"hash": t.hash, "name": t.name, "state": t.state,
                         "progress": t.progress * 100, "qsize": t.size,
                         "cp": cp, "verdict": "cp在(未称重)", "n_top": ntop}
                    # ★★ 跳过称重≠跳过报告：**判据（cp 存在）已经拿到了**，
                    #    所以该印的还是要印 —— 否则这一条在明细里成了空白，
                    #    人没法核（`ERR-META-22`：判据要在屏幕上）。
                    print(f"        {t.hash[:12]}…  {t.state:<10} "
                          f"{t.progress * 100:6.2f}%  cp在(未称重)")
                    print(f"       name={(t.name or '')[:58]!r}")
                    print(f"       qB 说 size = {human(t.size)}")
                    print(f"       content_path = {cp!r}")
                    print(f"       ✅ `content_path` **存在**（顶层 {ntop} 项；"
                          f"未称重 —— 大小判据对这一条**不适用**）")
                    results.append(r)
                    continue
            except OSError:
                pass
        r = measure(t)
        results.append(r)
        one_line(r)

    # ── 【B】基线汇总：★ 这就是本脚本的**产物** ──────────────────────────
    print()
    print("=" * 74)
    print("【B】★★ 基线汇总（这是本脚本要算的那个数）")
    print("=" * 74)
    tot = len(results)
    cnt = {}
    for r in results:
        cnt[r["verdict"]] = cnt.get(r["verdict"], 0) + 1
    for k in ("cp在", "cp在(未称重)", "cp不存在", "cp不是目录", "读不到", "cp空"):
        if k in cnt:
            print(f"    {k:<14} : {cnt[k]:>4} 条  （{cnt[k] / tot * 100:.1f}%）")
    print()
    bad = cnt.get("cp不存在", 0) + cnt.get("cp不是目录", 0)
    err = cnt.get("读不到", 0) + cnt.get("cp空", 0)
    if err:
        # ★ 三态：读不到**不算进任何一边**
        print(f"    ⚠️  {err} 条**判据失效**（读不到/路径空）⇒ 它们**既不进分子也不进分母**")
    denom = tot - err
    if denom <= 0:
        print("    ★★ 有效样本 = 0 ⇒ **什么基线都算不出来**（`ERR-META-11`：")
        print("       空集上不许下全称结论）")
        return 3
    print(f"  ⇒ ★★ 有效样本 {denom} 条里，「content_path **不存在**」= "
          f"**{bad} 条（{bad / denom * 100:.1f}%）**")
    print()
    if bad == 0:
        print("  ★ 基线 ≈ 0 ⇒ 「content_path 不存在」在**健康做种的种子**里**不出现**")
        print("    ⇒ ★★ 那么 17304 那条的'不存在'**是真异常**，判据**成立**，可以写巡检")
    elif bad / denom >= 0.5:
        print("  ★★ 基线**很高** ⇒ 「content_path 不存在」是这批种子的**常见/正常形态**")
        print("    ⇒ ★★ 这个判据**不能用**（会大面积误报）⇒ 换判据（比如比 size）")
    else:
        print(f"  ★ 基线在中间（{bad / denom * 100:.1f}%）⇒ 说明**两种形态都存在**"
              f" ⇒ 不能只看'exists 与否'，要看**别的东西**（size 对照 / 父目录）")
    print()

    # ── 【C】已知两条并排对照 ─────────────────────────────────────────
    if not args.no_compare:
        print("─" * 74)
        print("【C】★★ 已知那两条（17304 的两个 hash）—— **同一套判据**量一遍")
        print("─" * 74)
        print("  ★ 放在【B】的基线**旁边**看，才知道它们算不算异常")
        print()
        for h, label in KNOWN.items():
            got = [t for t in all_t if t.hash == h]
            print(f"  ── {label}")
            if not got:
                print(f"     ❌ qB 里**没有** {h[:12]}…")
                continue
            r = measure(got[0])
            one_line(r)
            print()

    print("=" * 74)
    print("★ 本脚本没有修改任何东西（qB 只查询；磁盘只读；**没读库**；**没联网**）。")
    print("  ⇒ 下一步：拿【B】的基线做**判据成立与否**的决定 —— 见【B】底部那三句")
    return 0


if __name__ == "__main__":
    sys.exit(main())
