"""把「站点上有、qB 里没有」的种子补进 qB 做种（**只加 qB，不重发、不碰站点写口**）。

═══════════════════════════════════════════════════════════════════════════
它填补的那个缺口（2026-09-26 实测定性）
═══════════════════════════════════════════════════════════════════════════
`publish.py` 的回加函数 `readd_to_qb()` **只在【发布成功后】被调用**
（`publish.py:1348`，`ok += 1` 之后）。⇒ 于是有**一整类状态无人能修**：

    站点上【已经在】（`site_status='EXISTS'` + `site_torrent_id` 有值）
    但 qB 里【没有】它的产物 ⇒ **不做种**

这类行**不会再被发布**（它们不满足 `PENDING_PUBLISH_WHERE`），
所以 `readd_to_qb()` **永远不会**为它们跑。★ 这就是本项目"没做种"的根因形状。

实测样本（2026-09-26）：
    Cesaria Evora - L'essentiel   site_id=17288  本地文件**完整**（318MB/11 flac）
        ⇒ qB `Musopia` 分类里**查不到** ⇒ 从未回加
    ★★ 緑黄色社会 - あたまご      site_id=17304  ← ★ 这句**先前写错了，已订正**
        先前写的是「本地文件**已丢失** ⇒ stalledDL 35.5%」。
        2026-09-26 用 `tools/diag_17304.py` **现场实测**推翻了它：
            磁盘 /downloads/incomplete/[260909] 緑黄色社会… 实测
                = **80 个文件 / 22.61 GiB** = qB 说的 `size`，一个字节不差
        ⇒ ★★ 文件**一个都没丢**，只是**堆在 `incomplete/` 里**，
          而 qB 的 `progress=35.46%` 是**它自己记账本的旧数**，不是现场读数。
        ⇒ 正解是 **`--recheck-only`**（只让 qB 重扫一遍盘），**不是下载**。
        ★ 教训形状（`ERR-META-01` 同族）：**「库/日志里记着的结论」不等于
          「现场」** —— 我照抄了本文件自己上一轮写的字，差点让用户白下 22.61 GiB。

═══════════════════════════════════════════════════════════════════════════
★★★ 判据一律从 `publish.py` 抠，不许手抄（ERR-META-03）
═══════════════════════════════════════════════════════════════════════════
`readd_to_qb()` 的 docstring 里有**三条承重结论**，本脚本**逐条照做**：

 ① **不传 `contentLayout` / `is_root_folder`** —— 传 `is_root_folder=False`
    会被库改写成 `contentLayout=NoSubfolder` ⇒ qB 去 `/downloads/01.flac` 找，
    而文件在 `/downloads/<专辑名>/01.flac` ⇒ `missingFiles` ⇒ **从不做种**
    （2026-09-24 实测 5 条如此）。

 ② **不传 `is_skip_checking`** —— 它是**唯一**能发现"文件对不上"的机制；
    关掉它，错位会**静默**通过。本项目成文纪律「**没报错 ≠ 成立**」。

 ③ **站点那个种子 != 库里的 `info_hash`** —— 站点种子是 `run.py` 用**新 announce**
    **重新生成**的，它的 infohash ≠ 库里 `info_hash`（那是**皇后的原种**）。
    ⇒ 复核必须用 `torf` 从**刚下载的字节**里读出真 hash，不能拿库里的 hash 去查。

═══════════════════════════════════════════════════════════════════════════
★ 本脚本【额外】做的一件事：`recheck`
═══════════════════════════════════════════════════════════════════════════
`readd_to_qb()` 里**没有** recheck —— 因为它加进去的是**站点刚生成的**种子文件，
那种情况下 qB 必然要下载（本地没有）。而**本脚本的目标场景不同**：

    本地文件**已经在** `save_path` 下（`content_path` 指着的那个目录）
    ⇒ 加进 qB 后必须让它**扫一遍**，否则 qB 会当成"新任务"从头下
      （`緑黄色社会` 就是这么卡在 35.5% 的）
    ⇒ `recheck` 让 qB 按 metainfo 校验已有文件：对得上 ⇒ **立刻做种**。

★ 所以 `--recheck` 是**本脚本的核心价值**，不是可选项（但仍保留开关：
  大种校验慢，且首次校验要读一遍文件）。

═══════════════════════════════════════════════════════════════════════════
两条纪律（写库/写 qB 的边界）
═══════════════════════════════════════════════════════════════════════════
1. **绝不碰本地库的任何列** —— 本脚本对 `data.db` **只读**
   （`PRAGMA query_only=1`）。它不改 `status` / `site_status` / `site_torrent_id`
   / `updated_at` —— 那些是**发布**与**抓取**的事（`ERR-SQL-06`）。
   ⇒ 它只读库里的 `site_torrent_id`（用来下载站点种子）与 `content_path`（用来判"文件在不在"）。

2. **qB 只做两件写操作**：`torrents_add`（加）与 `recheck`（校验）。
   **不 delete / 不 pause / 不改 save_path / 不动已有任务。**

═══════════════════════════════════════════════════════════════════════════
★★★ 2026-09-26 第一次真跑后的**实测教训**（本脚本据此改过，别再退回去）
═══════════════════════════════════════════════════════════════════════════
第一次跑 `--tids <14 条> --apply --recheck` 后：qB 任务数 `266 → 273`，
而 14 条里**只有 6 条真进了 qB**。诊断见 `--all` 全库扫描：

    全库 1446 个种子 = 1444 stalledUP + 1 stalledDL + 1 checkingDL
    ⇒ **一个 `missingFiles` 都没有** ⇒ 那 8 条**根本不在 qB 里**
    ⇒ 不是"延迟入库"，是 **`torrents_add()` 被 qB 拒绝了**。

★★ 三条由此订正的纪律（每一条都是血债）：

 **（甲）`torrents_add()` 的返回值必须检查。**
   它**返回 qB 的原话**（`"Ok."` / `"Fails."`），而**不是**抛异常。
   ⇒ 原来的 `print("✅ 已提交至 qB")` **只证明"没抛异常"**，
     和 `readd_to_qb()` docstring 里那条警告（"只证明 `torrents_add()`
     没抛，不证明做种成立"）**是同一个坑，我又踩了一次**。
   ⇒ 现在：**先读返回值，再决定打 ✅ 还是 ★★**；`Fails.` 必须**响亮地报**。

 **（乙）复核必须按 `new_hash` 查 —— 不许用标题粗匹配。**
   原实现用 `title in t.name`，而 qB 里的任务名是**站点种子的 name**
   （`[<sid>] <album>` 之类），与库里的 `formatted_title` **不一定同前缀**
   ⇒ 实测**报了 4 条 `❔ 查不到`，而那 4 条其实做种成功了**（假阴性）。
   ★ 与 `ERR-META-01` 同族：**判据看着合理，却量错了对象。**

 **（丙）`--verify-hashes` 必须是【只读】的。**
   要回答"那 8 条到底进没进"，需要一个**不改 qB、不碰站点、不碰库**的入口。
   ⇒ 有了它，"投递成功没有"这个问题**可以事后复查**，不必靠日志（日志会滚）。

═══════════════════════════════════════════════════════════════════════════
默认只读；真写要显式 --apply
═══════════════════════════════════════════════════════════════════════════
用法（NAS 上，compose 目录）:
    # ① ★ 先看 qB 里有没有这些原种（只读、零出网）
    sudo docker compose run --rm tools python tools/readd_qb.py --probe

    # ② ★★ 按站点种子 infohash 查 qB（只读；判"投递到底成没成"）
    sudo docker compose run --rm tools python tools/readd_qb.py \
        --verify-hashes 7aec0411...,865c0bff...

    # ③ 预览：准备加哪几条、save_path 是哪、文件在不在（只读）
    sudo docker compose run --rm tools python tools/readd_qb.py --tids 17288

    # ④ 真加（★ 会写 qB，但不碰站点、不碰本地库）
    sudo docker compose run --rm tools python tools/readd_qb.py --tids 17288 --apply --recheck

    # ⑤ ★★ 只对**已在 qB 里**的任务触发校验（**不 add**）—— 默认只读预览
    #    ★ 用在「文件其实在盘上、qB 却以为没下完」（如 17304：incomplete/ 里
    #      80 文件 / 22.61 GiB，却报 stalledDL 35.46%）
    sudo docker compose run --rm tools python tools/readd_qb.py \
        --recheck-only 02afb7878cd831377da482a686835ae58ebaaeef
    #    ⇒ 预览无误后，再加 --apply 真写（会读一遍盘、会改 qB 的 progress）

★ 依赖 `torf`：用来从**站点种子的字节**里读出真 infohash（模块头 ③）。
  容器里若没有它，`--apply` 与 `--verify-hashes` 都会**响亮地报缺**，不静默降级。
"""
import argparse
import io
import os
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.environ.get("MUSOPIA_APP_DIR", "/app"))

import config      # noqa: E402
from musopia import MusopiaClient, SiteError, LoginExpired  # noqa: E402


def qb_client():
    """连 qB。★ 连不上就**报错退出**，不降级（ERR-SQL-05 同族：读不到 != 没有）。"""
    from qbittorrentapi import Client
    c = Client(host=config.QB_HOST, username=config.QB_USER,
               password=config.QB_PASS)
    c.auth_log_in()
    return c


# ==================== ★ 站点种子 → 真 infohash（模块头 ③）====================

def torrent_infohash(content):
    """从**站点下载来的字节**里读出真 infohash。

    ★ 必须这样做（`readd_to_qb` docstring ③）：站点那个种子是 `run.py` 用
      **新 announce** 重新生成的，它的 infohash **不是**库里的 `info_hash`。
    ⚠️ 读不出来 ⇒ 返回 `(None, 原因)`，**绝不返回一个猜的值**
      （`ERR-META-14`：来源为空不许印成"成功了"）。
    """
    try:
        import torf
    except ImportError as e:                                # noqa: BLE001
        return None, f"缺 torf 库（{e}）⇒ 读不出真 infohash"
    try:
        return torf.Torrent.read_stream(io.BytesIO(content)).infohash, ""
    except Exception as e:                                  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


# ==================== ★★ qB 的"这算不算投递成功"判据（只一处口径）============

# ★ 做种中的 state（照抄 qB 自己的状态名）
SEEDING_STATES = ("stalledUP", "uploading", "forcedUP")


def classify_task(t):
    """把一个 qB 任务分类成 (标记, 一句话, 是否在前进)。

    ★ 判据**只在这里写一份**（`ERR-META-03`：别在多处各写一套）。
      `missingFiles` 是**最坏**的：它意味着"加了、但文件对不上 ⇒ 永远不做种"
      （`readd_to_qb` docstring ① 的后果）。
    """
    st = t.state
    pct = t.progress * 100
    if st in SEEDING_STATES:
        return "✅", f"在做种（state={st} progress={pct:.1f}%）", True
    if st == "missingFiles":
        return "★★", (f"**不会做种**：state=missingFiles"
                      f" ⇒ 文件没对上（见 readd_to_qb docstring ①）"), False
    if st in ("checkingDL", "checkingUP", "checkingResumeData", "allocating",
              "moving"):
        return "⏳", f"正在校验/分配/搬移（state={st} progress={pct:.1f}%）", False
    if st == "stalledDL":
        return "★", (f"卡在下载（state=stalledDL progress={pct:.1f}%）"
                     f" ⇒ 本地文件没被认到，或站点没做种者"), False
    if st in ("metaDL", "forcedMetaDL", "queuedDL", "queuedUP",
              "downloading", "forcedDL"):
        # ★ 这些意味着 qB **在往外下** ⇒ 本脚本的目标场景（本地已有文件）
        #   下不该出现；出现即"文件没被认到"。
        return "★", (f"qB 在下载/排队（state={st} progress={pct:.1f}%）"
                     f" ⇒ ★ 本地文件没被认到（应 recheck）"), False
    if st in ("pausedDL", "pausedUP", "stoppedDL", "stoppedUP"):
        return "⏸", f"已暂停（state={st} progress={pct:.1f}%）⇒ 不做种", False
    if st in ("error", "errored"):
        return "★★", f"**出错**（state={st}）⇒ 不会做种", False
    # ★ qB 自己就有个字面值是 "unknown"（从 qbittorrentapi 源码实测）
    #   ⇒ 它落到这里**不是"分类表漏了"，而是 qB 也没认出这个任务**。
    #   如实报出、不猜（ERR-META-14）。
    return "❔", f"qB 没识别出的 state={st!r} progress={pct:.1f}%（如实报，不猜）", False


def report_task(prefix, h, t):
    """按 classify_task 打一行，返回 (标记, 是否在前进)。"""
    mark, why, good = classify_task(t)
    print(f"  {mark} {prefix}{h[:12]}…  {why}")
    print(f"        content_path={t.content_path!r}")
    print(f"        save_path   ={t.save_path!r}   category={t.category!r}")
    if mark == "★★":
        print("        ★★ 这条**永远不会做种** —— 文件对不上（这就是本脚本要防的形状）")
    elif mark == "★":
        print("        ★ 本地文件没被认到 ⇒ 若文件其实在，需要 recheck")
    return mark, good


# ==================== ① --probe：qB 里有没有这些原种 ====================

def probe(qb, rows):
    """★ 按**库里的 `info_hash`**（= 皇后的原种）查 qB。

    ⚠️ 这条**只对"原种"有意义** —— 它是本地在跑的那个老种子。
       站点产物的 infohash **不是**它（见模块头 ③）。
    """
    print("=" * 74)
    print("★ --probe：按库里的 info_hash（皇后原种）查 qB")
    print("=" * 74)
    print("  ★ 意义：回加的 `save_path` = **原种的 save_path**（照抄 readd_to_qb）")
    print("     若原种不在 qB ⇒ 那条路拿不到路径 ⇒ 只能靠 content_path 的父目录")
    print()
    found, missing = 0, []
    for r in rows:
        h = r["info_hash"]
        got = qb.torrents_info(torrent_hashes=h)
        title = (r["formatted_title"] or "")[:38]
        if got:
            t = got[0]
            found += 1
            print(f"  ✅ {title}")
            print(f"       save_path   = {t.save_path!r}")
            print(f"       content_path= {t.content_path!r}")
            print(f"       state       = {t.state}  progress={t.progress * 100:.1f}%")
            print(f"       category    = {t.category!r}")
        else:
            missing.append(r)
            print(f"  ❌ {title}   ← qB 里**没有**这个原种")
    print()
    print("─" * 74)
    print(f"  原种在 qB: {found} / {len(rows)}    不在: {len(missing)}")
    print("─" * 74)
    return found, missing


# ==================== ② save_path 怎么定（照抄口径 + 显式退路）====================

def resolve_save_path(qb, row):
    """定 `save_path`。★ 两级，**必须把用了哪一级打出来**（ERR-META-13/14）。

    第 1 级（照抄 `readd_to_qb`）: 原种的 `save_path`。
    第 2 级（退路）: `content_path` 的**父目录** —— 文件就在那里，
                    所以那是 qB 必须落到的位置。

    ★ 为什么要退路：原种可能**已经不在 qB 里**（本地文件却还在），
      而 `readd_to_qb` 遇到这种情况是**直接跳过回加** ——
      本脚本的场景不同（目标就是"补一个从没加过的"）⇒ 必须能继续。
    ★★ 但用了退路这件事**必须显式可见**，因为它是**新引入的判据**，
       不是 `publish.py` 里那一套（两处口径不同 ⇒ 必须标出来）。
    """
    got = qb.torrents_info(torrent_hashes=row["info_hash"])
    if got:
        return got[0].save_path, "原种.save_path（照抄 readd_to_qb）"
    cp = (row["content_path"] or "").rstrip("/")
    if cp:
        return os.path.dirname(cp) or None, "content_path 的父目录（★ 退路，非 publish.py 口径）"
    return None, "★ 两条都没有 ⇒ 无法确定"


def local_files_ok(row):
    """★ 本地文件在不在 —— 判据是 `content_path` 那个目录**真的存在**
    （不是"列非空"，`ERR-META-03`：`publish.py` 的预检用的是 `os.path.exists`）。"""
    cp = (row["content_path"] or "").rstrip("/")
    if not cp:
        return False, "content_path 为空"
    if not os.path.isdir(cp):
        return False, f"目录不存在: {cp}"
    try:
        n = sum(1 for _ in os.scandir(cp))
    except OSError as e:
        return False, f"读目录失败: {e}"
    return n > 0, f"{cp}（{n} 项）"


# ==================== ④ --verify-hashes：只读，按 infohash 查 qB ====================

def parse_hashes(raw):
    """★ 把 `--verify-hashes` 的值解析成 hash 列表。支持三种写法：

      · `H1,H2,…`          —— 命令行直接给（★ 只适合**短**的，见下）
      · `@/data/hashes.txt` —— **★ 从文件读，每行一个**（推荐）
      · 上面两种混写（逗号分隔的每一项各自判）

    ═══════════════════════════════════════════════════════════════════
    ★★ 为什么必须有 `@文件` 这一路（2026-09-26 实测踩了两次）
    ═══════════════════════════════════════════════════════════════════
    8 个 infohash 串在一条命令行上是 **360+ 字符** ⇒ 终端**折行**，而折行
    发生在**命令中间**时，后半截会被 shell 当成**下一条命令**（实测两次）：
        第一次：折在 `429a3013b7e5…e3` 处 ⇒ 后 6 个 hash **静默消失**，
                而脚本照样打印「待查 = 3 个 hash」（★ 假性成功）
        第二次：`printf` 在折行处收尾 ⇒ `> hashes.txt` 那截跑丢，
                文件根本没建，而报错是 `command not found`
    ⇒ 判据：**长参数不许走命令行**。hash 这种"定长、无空格、怕截断"的东西
      一律走文件 —— 文件里的内容**不经过 shell 解析**，折行无从下手。

    ★ 顺带做了**形状校验**：infohash 必须是 40 位十六进制。
      不对就**响亮地报出那一行**（`ERR-META-14`：别让"没读对"冒充"没这条"）。
    """
    items = [s.strip() for s in raw.split(",") if s.strip()]
    out, bad = [], []
    for it in items:
        if it.startswith("@"):
            path = it[1:].strip()
            try:
                # ★ `errors="replace"` + 逐行判形状 ⇒ 编码坏掉的那几行会被
                #   `_is_hash` 剔出来报给用户，而不是抛 `UnicodeDecodeError`
                #   甩一个 traceback（本机实测：写个非 UTF-8 文件就复现，
                #   而"文件读不动"和"文件里没有"是两件事）。
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
            except OSError as e:
                # ★ 读不到 != 没有（ERR-SQL-05）⇒ 非零退出，绝不当成空列表
                sys.exit(f"❌ 读不到 hash 文件 {path!r}：{e}\n"
                         f"   ★ 不许把'读不到'当成'没有 hash'")
            n_before = len(lines)
            lines = [ln.strip() for ln in lines
                     if ln.strip() and not ln.strip().startswith("#")]
            print(f"  ★ 从文件读 {path!r}：{n_before} 行 ⇒ 有内容 {len(lines)} 行")
            for ln in lines:
                (out if _is_hash(ln) else bad).append(ln)
        else:
            (out if _is_hash(it) else bad).append(it)
    if bad:
        print(f"  ⚠️  {len(bad)} 项**形状不对**（不是 40 位十六进制）⇒ 已剔除：")
        for b in bad:
            print(f"        {b!r}")
        print("      ★ 这多半是**命令行折行截断**的残留 —— 请改用 @文件 传")
    return out


def _is_hash(s):
    """infohash = 恰好 40 位十六进制。★ 折行截断会产生 13/26 位这种长度。"""
    return len(s) == 40 and all(c in "0123456789abcdefABCDEF" for c in s)


def verify_hashes(qb, raw):
    """★ 只读：按给定的 infohash 逐个查 qB，报 state。

    用途（模块头「实测教训」丙）：回答「我提交的那些**到底进没进 qB**」。
    ★ 这个问题**必须能事后复查** —— 因为 `torrents_add` 的返回值只在
      执行那一刻打印一次，而日志会滚（`ERR-META-01` 一族：判据会自己消失）。
    ★ qB 才是权威：`torrents_info(torrent_hashes=h)`。

    ⚠️ 两种情况**必须分开报**（`ERR-META-14`：来源为空不许印成"没有"）：
        · 查不到 ⇒ "qB 里没有这个 hash"（判据成立、值就是"没有"）
        · 查得动但报错 ⇒ "查不了"（判据**失效**，不许当成"没有"）
    """
    hashes = parse_hashes(raw)
    if not hashes:
        print("  （--verify-hashes 没给出任何**形状正确**的 hash）")
        return 2
    print("=" * 74)
    print("★ --verify-hashes：按 infohash 查 qB（**只读**，不 add / 不改任何东西）")
    print("=" * 74)
    print(f"  qB      = {config.QB_HOST}")
    print(f"  待查    = {len(hashes)} 个 hash")
    print()

    # ★ 逐条查（批量也支持，但批量时"哪一个不存在"不好分辨；几十条逐条最清楚）
    n_ok, n_miss, n_bad = 0, 0, 0
    for h in hashes:
        try:
            got = qb.torrents_info(torrent_hashes=h)
        except Exception as e:                              # noqa: BLE001
            # ★ 查不了 != 没有（ERR-SQL-05）
            n_bad += 1
            print(f"  ⚠️  {h[:12]}… **查不动**（{type(e).__name__}: {e}）")
            print("      ⇒ ★ 判据失效，**不许**读成「qB 里没有」")
            continue
        if not got:
            n_miss += 1
            print(f"  ❌ {h[:12]}…  qB 里**没有**这个 hash"
                  f"  ⇒ 这条**没入库**（被 torrents_add 拒绝，或提交时就抛了）")
            continue
        n_ok += 1
        report_task("", h, got[0])

    print()
    print("─" * 74)
    print(f"  在 qB 里 : {n_ok}    不在 : {n_miss}    查不动 : {n_bad}")
    if n_miss:
        print("  ★★ 「不在」的那些 ⇒ **投递没成功**。下一步查为什么：")
        print("      用 --tids <sid> --apply --recheck 单独跑一条，")
        print("      ★ **看 qB 返回的原话**（'Ok.' / 'Fails.' —— 现在会打出来）")
        print("      ★ 常见原因：该 hash 已在 qB 里（重复加，qB 会回 Fails.）")
    print("─" * 74)
    print()
    print("★ 本脚本没有修改任何东西（qB 只查询；本地库只读；站点没碰）。")
    return 0


# ==================== ⑤ --recheck-only：只对**已在 qB 里**的任务触发校验 ========
#
# ═══════════════════════════════════════════════════════════════════════════
# ★★★ 为什么必须新开一个入口（2026-09-26，用户点名的 sid=17304）
# ═══════════════════════════════════════════════════════════════════════════
# 现场（`tools/diag_17304.py` 实测，**推翻了先前的结论**）：
#
#     hash=02afb787…  stalledDL  35.46%   category='Musopia'  ← 站点产物（要救的）
#         qB 说: size=22.61 GiB  completed=8.02 GiB  left=14.59 GiB
#     hash=69c5ee1b…  stalledUP 100.00%   category='opencd'   ← 皇后原种
#
#     ★★ 磁盘实测 /downloads/incomplete/[260909] 緑黄色社会…：
#         ✅ 17 个顶层项 / **80 个文件 / 22.61 GiB**
#         = **qB 说的 size**，一个字节不差；80 个文件 = qB 认到的文件数
#
# ⇒ ★★★ `left=14.59 GiB` 是**假的**：那 14.59 GiB **就在 incomplete/ 里**。
#   旧的「本地文件已丢失」结论（本文件模块头那行）是**错的**，已被现场推翻。
#
# ═══════════════════════════════════════════════════════════════════════════
# 为什么 `--tids 17304 --apply --recheck` **不能**用在这条上
# ═══════════════════════════════════════════════════════════════════════════
# 那个分支的语义是「**把站上有的、qB 里没有的**补进来」⇒ 它先 `torrents_add`
# 再 `torrents_recheck`。而 17304 **已经在 qB 里**了 ⇒ 走那条路会：
#     ① 先 `torrents_add` —— qB 大概率返回 `Fails.`（它认为这条已存在），
#        而那次 add 是**多余的写**（哪怕被拒，也不是我们想做的事）；
#     ② 然后才 recheck —— 顺序对，但中间夹了一个**没必要的写操作**。
# ⇒ ★ 这条不需要 add 任何东西，**只需要 recheck**。所以单开一个入口。
#
# ═══════════════════════════════════════════════════════════════════════════
# 判据：**recheck 是 qB 写操作，但它不改内容**
# ═══════════════════════════════════════════════════════════════════════════
# `torrents_recheck` 只让 qB **按已有 metainfo 重新逐块核对磁盘**：
#     · 文件对得上 ⇒ `progress` 推到真值（这里是 100%）⇒ **做种**
#     · 对不上     ⇒ 变成 `missingFiles`（**响亮地坏**，正是我们要的）
# ★ 它**不下载 / 不删除 / 不移动文件 / 不改 category / 不改 save_path**。
# ★★ 但它**确实会读一遍盘**（22.61 GiB ⇒ 几分钟），且**会改 qB 里这条的
#    `progress`** —— 所以它是**写**，必须显式 `--apply` 才做（同 `--fix-category`
#    的纪律：不可逆/有副作用的动作默认只读预览）。
#
# ★ 传进来的 hash 是**哪一个**很要紧：**站点产物**那个（`02afb787…`），
#   不是库里 `info_hash`（那是皇后原种，它在做种、不用救）。
#   ⇒ 本入口**只认 hash、不读库**（同 `--verify-hashes` 的理由：
#     它与库里那一行无关，不该在库读不到时没意义地失败）。


def recheck_only(qb, raw, apply=False, wait=20):
    """★ 只对**已在 qB 里**的任务触发 `torrents_recheck`（**不 add**）。

    ★★ 默认 `apply=False` ⇒ **只预览**（列出这些 hash 现在是什么 state、
       文件在不在、recheck 会有意义还是没意义）。**加 `--apply` 才真写。**

    ★ 为什么默认只读：`recheck` 会改 qB 的 `progress`（那是它的目的），
      是**实打实的写**。按本项目纪律，有副作用的动作**默认不准执行**
      （`ERR-META-12`），必须有人显式点头。
    """
    hashes = parse_hashes(raw)
    if not hashes:
        print("  （--recheck-only 没给出任何**形状正确**的 hash）")
        return 2

    print("=" * 74)
    print("★ --recheck-only：对**已在 qB 里**的任务触发校验（不 add / 不 delete）")
    print("=" * 74)
    print(f"  qB      = {config.QB_HOST}")
    print(f"  模式    = {'★★ 真写 qB（触发 recheck）' if apply else '💡 只读预览（不写任何东西）'}")
    print(f"  待处理  = {len(hashes)} 个 hash")
    print()
    print("  ★ 语义：`torrents_recheck` 让 qB 按 metainfo 重扫磁盘 ——")
    print("     对得上 ⇒ progress 推到真值 ⇒ 做种；对不上 ⇒ missingFiles（响亮地坏）")
    print("  ★★ 它**不下载 / 不删除 / 不移动文件 / 不改 category / 不改 save_path**")
    print("     但**会读一遍盘**，且**会改 qB 的 progress** ⇒ 是写操作。")
    print()

    # ── 先只读体检：这条在不在、现在什么 state、文件在不在 ──────────────
    targets = []
    n_miss = n_bad = 0
    for h in hashes:
        try:
            got = qb.torrents_info(torrent_hashes=h)
        except Exception as e:                              # noqa: BLE001
            n_bad += 1
            print(f"  ⚠️  {h[:12]}… **查不动**（{type(e).__name__}: {e}）")
            print("      ⇒ ★ 判据失效，**不许**读成「qB 里没有」")
            continue
        if not got:
            n_miss += 1
            print(f"  ❌ {h[:12]}…  qB 里**没有**这个 hash")
            print("      ⇒ ★ 无事可做：recheck 只能作用于**已经在 qB 里**的任务。")
            print("        （若这条本该在，那问题在'加种'，不在'校验' —— 别在这里修）")
            continue
        t = got[0]
        mark, why, _good = classify_task(t)
        print(f"  {mark} {h[:12]}…  {why}")
        print(f"        name         = {_show_name(t.name, 60)}")
        print(f"        save_path    = {t.save_path!r}")
        print(f"        content_path = {t.content_path!r}")
        print(f"        category     = {t.category!r}")
        # ★★ 现场判据：磁盘上到底有没有东西（**这才是"文件丢没丢"的答案**）
        #    ★ 绝不把"读不到"并进"没有"（ERR-SQL-05）。
        cp = (t.content_path or "").rstrip("/")
        if not cp:
            print("        ❔ content_path 为空 ⇒ **无法判断**（不许当成'没有'）")
        elif not os.path.exists(cp):
            print(f"        ★★ 磁盘上**不存在** {cp!r}")
            print("            ⇒ recheck 大概率报 missingFiles（文件不在这儿）")
            print("            ⇒ ★ 此时 recheck **仍然值得做**（它把「猜」变成「实测」）：")
            print("               真丢 ⇒ 报 missingFiles（响亮地坏，我们就知道了）；")
            print("               其实在别处 ⇒ 也报 missingFiles，但**那是有信息的**。")
            print("            ★ 但**先别下任何东西** —— 先确认文件是不是在")
            print("               `incomplete/`（qB 未完成区）或改了名。")
        elif not os.path.isdir(cp):
            print(f"        ⚠️ {cp!r} 存在但**不是目录**")
        else:
            try:
                n = sum(1 for _ in os.scandir(cp))
                print(f"        ✅ 磁盘上**在**：{cp!r}（{n} 个顶层项）")
                print("            ⇒ ★ recheck **有意义** —— 它会把 progress 推到真值")
            except OSError as e:
                print(f"        ❔ 读目录失败：{type(e).__name__}: {e} ⇒ 读不到不等于空")
        targets.append((h, t))
    print()

    if not targets:
        print("  ⇒ ★ 没有任何可处理的目标（上面各条的'没有/查不动'原因见上）")
        print("★ 本脚本没有修改任何东西。")
        return 0 if n_miss == 0 else 2

    # ── 预览就到此为止 ──────────────────────────────────────────────
    if not apply:
        print("─" * 74)
        print(f"  💡 预览到此为止（{len(targets)} 条**将**被 recheck）。")
        print("     ★ 要真做：再加 `--apply`（那是写操作：会读盘、会改 qB 的 progress）")
        print(f"     ★ 做完隔 {wait} 秒会**回读复核**（同判据：state + progress）")
        print("─" * 74)
        print()
        print("★ 本脚本没有修改任何东西（qB 只查询；本地库只读；站点没碰）。")
        return 0

    # ── 真写 ────────────────────────────────────────────────────────
    print("─" * 74)
    print(f"  ★★ 真写：对 {len(targets)} 条触发 `torrents_recheck`")
    print("─" * 74)
    done, failed = [], []
    for h, t in targets:
        before = (t.state, t.progress * 100)
        try:
            qb.torrents_recheck(torrent_hashes=h)
            print(f"  ✅ 已触发 {h[:12]}…  （前: state={before[0]} progress={before[1]:.2f}%）")
            done.append((h, before))
        except Exception as e:                              # noqa: BLE001
            # ★ 抛异常 = "没触发成功"，**不许**当成成功（教训甲：返回值/异常都要看）
            print(f"  ★★ {h[:12]}… 触发**失败**：{type(e).__name__}: {e}")
            failed.append((h, f"{type(e).__name__}: {e}"))

    # ── ★★ 回读复核：**同判据**，不靠"我下过命令"（ERR-META-08）────────────
    if done and wait > 0:
        print()
        print(f"  ⏳ 等 {wait} 秒后回读复核（qB 的 recheck 是**异步**的 —— "
              f"触发那一刻查到的还是旧 progress）...")
        time.sleep(wait)
        print()
        print("  ★★ 回读复核（同判据）：")
        improved = 0
        for h, before in done:
            try:
                got = qb.torrents_info(torrent_hashes=h)
            except Exception as e:                          # noqa: BLE001
                print(f"  ❔ {h[:12]}… 复核时查不动（{type(e).__name__}: {e}）")
                continue
            if not got:
                print(f"  ❔ {h[:12]}… 复核时**查不到** ⇒ 判据失效，不敢下结论")
                continue
            t = got[0]
            after = (t.state, t.progress * 100)
            mark, why, _ = classify_task(t)
            print(f"  {mark} {h[:12]}…  前 state={before[0]} {before[1]:.2f}%"
                  f"  →  后 state={after[0]} {after[1]:.2f}%")
            print(f"        ⇒ {why}")
            if after[1] > before[1] + 0.005 or after[0] != before[0]:
                improved += 1
        print()
        print(f"  ★ 复核：有变化的 = {improved} / {len(done)}")
        print("  ★★ 若**一条都没变**：常见于 recheck **还在跑**（22.61 GiB 要读几分钟）")
        print("     ⇒ **不是失败**。请**再等一会儿**用 --verify-hashes 复查，")
        print("       别据此判定'recheck 没用'（那是把'还没跑完'读成'坏了'）。")

    print()
    print("─" * 74)
    print(f"  已触发 : {len(done)}    触发失败 : {len(failed)}")
    for h, err in failed:
        print(f"      ★★ {h[:12]}…: {err}")
    print("─" * 74)
    print()
    print("★ 本脚本**只**触发了 recheck —— 没有 add / 没有 delete / 没有移动文件 /")
    print("  没有改 category / 没有改 save_path / 本地库只读 / 站点没碰。")
    return 0


# ==================== ⑥ --check-category：分类对不上（只读）====================
#
# ═══════════════════════════════════════════════════════════════════════════
# ★★★ 2026-09-26 第二次实测：**「分类对不上」这个口径本身是错的**
# ═══════════════════════════════════════════════════════════════════════════
# 第一次我写这个功能时的口径是：
#     off = [t for t in seeding if (t.category or "") != want]
# 真跑一次，报出来 **1173 条**。当时我的第一反应是"这么多都该改成 Musopia" ——
# ★★ 那会是一次**不可逆的批量写入**，而且**几乎全错**。拆开看那 1173 条：
#
#     category='opencd'   ≈500 条   ← **皇后站的种子**（我们从站上下载的，不是我们发的）
#     category=''         ≈600 条   ← ★ **名字里带 `@Musopia`** ⇒ 这些**是我们的**
#     category='我想听'   ≈30  条   ← ★ **用户自己的下载**（中岛美雪 / 工藤静香…）
#
# ★ 决定性证据（空 category 那批的名字）：
#     王靖雯 - 天空 1994 FLAC 分轨 - ZHENGLIFAN@Musopia
#     Michael Jackson - Bad (Special Edition) 1987 FLAC 分轨 - ZHENGLIFAN@Musopia
#   `@Musopia` 就是**我们的发种账号**。
#
# ★★ 而且同一张专辑**两条**：`hash=7aec0411…`(空) 与 `hash=d290596d…`(opencd)
#     ⇒ 我们发了一份（该是 Musopia）、也从站上下了别人那份（该留 opencd）。
#     齐秦那条 `category=''` 不是孤例 —— 它是**这批 ≈600 条**里的一条。
#
# ⇒ 结论：**"在做种"根本不是一个能用来判"归谁"的集合。**
#   qB 的 category 是个纯标签，谁都可以是它，而 qB 里同时躺着
#   **我们发的**、**我们下载的**、**用户自己下的**三类任务。
# ★ 所以判据必须**窄到只吃铁证**：**名字里带 `@Musopia`**
#   （那是发种时写进种子名的，第三者不会带；`ERR-META-03`：判据要能证伪）。
#
# ★★ 我犯的错形（今天的**第二次**，同一天）：
#   把「按某个筛选条件数出来的集合」当成了「全量」（`ERR-META-20`）。
#   第一次是拿 `category='Musopia'` 的计数去推"投递成没成"，
#   第二次是拿 `category != 'Musopia'` 去推"哪些该改" —— **同一天、同一形状、两次**。
#   ⇒ 已写 `ERR-META-21`：**"筛选集合"与"全量集合"必须由同一次测量给出**，
#     而且中间**不许换条件**；换条件 = 换问题 = 结论作废。
#
# ⚠️ 附带的事实（**留作悬案，不在本脚本解决**）：
#   那 ≈600 条 `category=''` 里，**有一部分名字不带 `@Musopia`**
#   （例：`BIGBANG JAPAN DOME TOUR 2013-2014 [2Blu-ray+2CD] [DLX]`），
#   却有**同名同内容**的一条带 `@Musopia`。
#   ⇒ 那说明**同一个种子存在两个 infohash**（种子内容/announce 不同），
#     不是"标签丢了"。**这件事没有现场证据前不许猜**（`ERR-META-01`）。
#     ⇒ 本脚本只在**交叠面**（带 `@Musopia` 却没有 Musopia 标签）上动作。

# ★★★ 窄口径：**唯一**被允许改的判据（见上面那段）
MUSOPIA_MARK = "@Musopia"


def _our_marked(t):
    """这条任务是不是**我们自己发的** —— 判据 = 种子名里带 `@Musopia`。

    ★★ 为什么不看 `category`：见上面「第二次实测」那段 ——
      qB 里同时躺着三类任务，`category` 判不出归谁。
    ★ 为什么不看 `content_path` / `save_path`：那是**存放位置**，
      与"谁发的"无关（别人的种子也可能被放进同一个目录）。
    ★ 判据可证伪：`@Musopia` 是**发种时写进种子名**的，第三者不会带
      （`ERR-META-03`：好判据要能说出"什么情况下它为假"）。
    """
    return MUSOPIA_MARK in (t.name or "")


def _category_scan(qb):
    """★ 只读：一次 `torrents_info()`，按**类别**把"在做种"拆开报。

    ★★ 这个函数存在的唯一理由，是让**差额可见**，而不是让差额"被修"：
      去年我拿 `category='Musopia'` 的计数当"我们的全部"（`ERR-META-20`），
      今天差点拿 `category != 'Musopia'` 当"该改的清单"（`ERR-META-21`）。
      ⇒ 所以这里**先把 qB 里到底有几类人摊开**，再谈交叠面。
    """
    all_t = qb.torrents_info()
    seeding = [t for t in all_t if t.state in SEEDING_STATES]
    buckets = {}
    for t in seeding:
        buckets.setdefault(t.category or "", []).append(t)
    return all_t, seeding, buckets


def _show_name(name, width=70):
    """★ 显示种子名时**优先保住铁证 `@Musopia`**。

    ★★ 为什么需要它（2026-09-26 实测踩到，`ERR-META-11` 的形状）：
      原先一律 `name[:50]` / `name[:70]` 截断，而
        `Michael Jackson - Bad (Special Edition) 1987 FLAC 分轨 - ZHENGLIFAN@Musopia`
      的 `@Musopia` 正好落在**第 50 字符之后** ⇒ 在"要不要点这个头"的那一步，
      屏幕上显示的名字**看不到判据本身** —— 打印出来的东西不足以支撑它宣称的结论。
    ⇒ 修法不是"把截断加长"（那只是把边界推远，换个更长的名字还会撞上），
      而是**让截断点为铁证让路**：铁证不在窗口内时，砍掉中段、把它接出来。
    """
    nm = name or ""
    i = nm.find(MUSOPIA_MARK)
    if i < 0 or i + len(MUSOPIA_MARK) <= width:
        return nm[:width]                    # 铁证在窗口内（或压根没有）⇒ 照常截
    head = nm[:max(0, width - len(MUSOPIA_MARK) - 1)]
    return f"{head}…{MUSOPIA_MARK}"


def check_category(qb, rows=None):
    """★ 只读：把「在做种」按 category 摊开 + 列出**铁证交叠面**（带 `@Musopia` 却没标签）。

    ═══════════════════════════════════════════════════════════════════
    为什么需要它（2026-09-26 实测踩到，**我因此得出过两次相反结论**）
    ═══════════════════════════════════════════════════════════════════
    实测 sid=15974 那条：**在做种、`state=stalledUP`，而 `category=''`（空串）**。
    ★ 后果**不是"没做种"**（它做种好好的），而是：
        **任何按 `category='Musopia'` 的统计都会漏掉它**
    ⇒ 我正是拿 `category='Musopia'` 的计数（266→273）去推"回加了 14 条、
      只进了 7 条"，于是把一条**一直在做种**的任务判成"没进 qB"
      （见 `ERRATA.md` 的 `ERR-META-20`）。

    ★★ 但"差额 = 1173 条"**不是"1173 条都该修"** —— 那 1173 条里绝大多数
      **不是我们的**（详见本段上方「第二次实测」）。⇒ 本函数因此分两段报：
        【甲】按 category 摊开（**只为了看见差额**，不构成任何动作判据）
        【乙】交叠面：**带 `@Musopia` 却没有 Musopia 标签**（**唯一**可动作的集合）
    """
    want = config.PUBLISH_CATEGORY
    print("=" * 74)
    print(f"★ --check-category：把「在做种」按 category 摊开，并找出铁证交叠面")
    print("=" * 74)
    print("  ★ 只读（不 add / 不改 category / 不 pause）—— 只列，不动")
    print()
    all_t, seeding, buckets = _category_scan(qb)
    print(f"  全库任务      : {len(all_t)}")
    print(f"  其中在做种    : {len(seeding)}")
    print()
    print("  【甲】做种任务按 `category` 分组（★ 这一栏**只为了看见差额**，")
    print("         **不构成「该改」的判据** —— qB 里同时躺着三类任务）：")
    for cat in sorted(buckets, key=lambda c: (-len(buckets[c]), c)):
        mine = len([t for t in buckets[cat] if _our_marked(t)])
        label = cat if cat else "（空串 ''）"
        print(f"      category={cat!r:<16} → {len(buckets[cat]):>5} 条"
              f"   其中名字带 '{MUSOPIA_MARK}' 的 : {mine}")
    print(f"      ⇒ 合计 {len(seeding)} 条（= 在做种总数，没有漏项）")
    print()

    # ---------- 【乙】交叠面：唯一可以动作的判据 ----------
    # ★★ 注意这里**不是**"category 对不上"（那会捞进 ~500 条别人的
    #    + ~30 条用户自己的），而是**必须有铁证 `@Musopia`**。
    overlap = [t for t in seeding
               if _our_marked(t) and (t.category or "") != want]
    print("  【乙】★★ 交叠面 = 名字带 '%s' 但 category != %r" % (MUSOPIA_MARK, want))
    print("        ⇒ **这 才是**可以改成 %r 的集合（判据 = 种子名里的铁证）" % want)
    print(f"        {len(overlap)} 条")
    print()
    if not overlap:
        print("  ✅ 交叠面为空 ⇒ 我们发的种子**都带对了标签**。")
    else:
        for t in sorted(overlap, key=lambda x: (x.category or "~", x.name or "")):
            print(f"  ★ hash={t.hash[:12]}…  category={t.category!r}  "
                  f"state={t.state} progress={t.progress * 100:.1f}%")
            print(f"      name={_show_name(t.name, 70)!r}")
        print()
        print("─" * 74)
        print(f"★ 这 {len(overlap)} 条**在做种**，只是标签不对 ⇒ 不影响做种，只影响统计。")
        print("  要修（把它们改成 %r）是**一次 qB 写操作**：" % want)
        print("      --fix-category            （★ 需显式给；默认只列不动）")
        print(f"  ★ 判据已窄到**只吃 '{MUSOPIA_MARK}' 铁证** ——")
        print("    别人的种子（category='opencd'）和用户自己的下载（'我想听'）")
        print("    **都不会**被它碰到。")
        print("─" * 74)
    print()
    print("★ 本脚本没有修改任何东西（qB 只查询；本地库只读；站点没碰）。")
    return 0


def fix_category(qb, rows=None):
    """★ 写：把**交叠面**（带 `@Musopia` 却没有 `PUBLISH_CATEGORY` 标签）改过来。

    ★★ 这是一次**不可逆的 qB 写操作** ⇒ 必须显式 `--fix-category` 才做
      （`ERR-META-12`：不可撤销的动作上，"不知道"按"不许"处理）。
    ★★ 判据**只吃 `@Musopia` 铁证**（见 `_our_marked`）—— **不是**"category 对不上"。
      这一点是 2026-09-26 第二次实测改过来的：原先那口径会一次改坏
      ~500 条别人的（`category='opencd'`）+ ~30 条用户自己的（`'我想听'`）。
    ★ 只改 `category` 一列，**不动** state / save_path / 文件 / 种子内容。
    """
    want = config.PUBLISH_CATEGORY
    off = [t for t in qb.torrents_info()
           if t.state in SEEDING_STATES
           and _our_marked(t) and (t.category or "") != want]
    if not off:
        print(f"  ✅ 交叠面为空（没有「带 {MUSOPIA_MARK} 却没标签」的做种任务）。")
        return 0
    print(f"  ★ 准备把 {len(off)} 条**带 {MUSOPIA_MARK} 铁证**的做种任务"
          f"的 category 改成 {want!r}：")
    for t in off:
        # ★★ 2026-09-26 实测踩到：原先这里是 `{t.name[:50]!r}` —— 而
        #    `Michael Jackson - Bad (Special Edition) 1987 FLAC 分轨 - ZHENGLIFAN@Musopia`
        #    的铁证 `@Musopia` **正好落在第 50 字符之后** ⇒ 在"要用户点头"的
        #    那一步，**显示的恰好是判据看不见的部分**（`ERR-META-11`）。
        #    ⇒ 改用 `_show_name`：截断为铁证让路（详见它的 docstring）。
        print(f"      hash={t.hash[:12]}…  {t.category!r} → {want!r}   "
              f"{_show_name(t.name, 50)!r}")
    print(f"  ★ 判据 = 名字含 {MUSOPIA_MARK!r} ⇒ **别人的种子不会被碰**")
    n = 0
    for t in off:
        try:
            qb.torrents_set_category(torrent_hashes=t.hash, category=want)
            n += 1
        except Exception as e:                              # noqa: BLE001
            print(f"      ★★ 改失败 hash={t.hash[:12]}…：{type(e).__name__}: {e}")
    print(f"\n  ✅ 已改 {n} / {len(off)} 条")
    # ★ 回读确认（ERR-META-08：跨进程动作做完必须回读，不能靠"我下过命令"）
    # ★★ 回读**必须用同一个判据**（带 @Musopia 且标签不对），不能换成"全部对不上"
    #    —— 换了判据就换了问题，回读会报一堆与本次动作无关的剩余（ERR-META-21）。
    after = [t for t in qb.torrents_info()
             if t.state in SEEDING_STATES
             and _our_marked(t) and (t.category or "") != want]
    print(f"  ★ 回读复核（同判据）：交叠面仍剩 = {len(after)} 条（应为 0）")
    if after:
        print("      ★★ 仍有剩余 ⇒ 上面那些没改成功（逐条看报错）")
    return 0


# ==================== ③ 主流程 ====================

SELECT_SQL = ("SELECT info_hash, formatted_title, torrent_name, status, "
              "       site_status, site_torrent_id, content_path, opencd_tid "
              "  FROM tasks WHERE site_status='EXISTS' "
              "   AND site_torrent_id IS NOT NULL AND site_torrent_id != ''")


def load_rows(db_path, tids):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA query_only=1")          # ★ 本脚本对库只读，永远
    conn.row_factory = sqlite3.Row
    if tids:
        ph = ",".join("?" * len(tids))
        sql = SELECT_SQL + f" AND site_torrent_id IN ({ph}) ORDER BY site_torrent_id"
        try:
            return list(conn.execute(sql, tuple(tids)))
        finally:
            conn.close()
    # ★ 不点名时只列候选（**不自动全加**：那是不可逆的批量动作）
    try:
        return list(conn.execute(SELECT_SQL + " ORDER BY site_torrent_id"))
    finally:
        conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把「站点上有、qB 里没有」的种子补进 qB（只加 qB，不重发）")
    ap.add_argument("--probe", action="store_true",
                    help="★ 只读：查 qB 里有没有这些行的**原种**，并看它的 save_path")
    ap.add_argument("--tids", default="",
                    help="★ 点名 site_torrent_id（逗号分隔）。不点名 ⇒ 只列候选，不写")
    ap.add_argument("--apply", action="store_true",
                    help="真加进 qB（默认只读预览）")
    ap.add_argument("--recheck", action="store_true",
                    help="★ 加完后触发 hash 校验 —— 让 qB 认到**本地已有文件**"
                         "（不 recheck 它会从头下载，2026-09-26 实测）")
    ap.add_argument("--verify-hashes", default="",
                    help="★★ 只读：按这些 infohash（逗号分隔）查 qB —— "
                         "判「投递到底成没成」。不碰库、不碰站点、不写 qB。")
    ap.add_argument("--recheck-only", default="",
                    help="★★ 只对**已在 qB 里**的任务触发校验（不 add！）。"
                         "默认**只读预览**，加 --apply 才真写。"
                         "★ 用在「文件其实在盘上、qB 却以为没下完」这类 "
                         "（如 sid=17304：incomplete/ 里 80 文件/22.61GiB 却报 "
                         "stalledDL 35%%）—— 它只让 qB 重扫，不下载、不移动文件。")
    ap.add_argument("--check-category", action="store_true",
                    help="★ 只读：把「在做种」按 category 摊开（看见差额），并找出"
                         "「名字带 @Musopia 却没 Musopia 标签」的**交叠面** "
                         "（★ 只有交叠面可以改；见 ERR-META-20/21）")
    ap.add_argument("--fix-category", action="store_true",
                    help="★★ 写 qB：把**交叠面**（带 @Musopia 铁证却没标签）改成"
                         " PUBLISH_CATEGORY（不可逆 ⇒ 必须显式给；不改种子/不改 state；"
                         "★ 别人的种子与用户自己的下载**都不会**被碰）")
    ap.add_argument("--delay", type=int, default=20,
                    help="recheck 后隔多少秒复核（默认 20）")
    ap.add_argument("--db", default=config.DB_PATH)
    args = ap.parse_args(argv)

    tids = [s.strip() for s in args.tids.split(",") if s.strip()]

    print("=" * 74)
    print("★ 把「站点上有、qB 里没有」的种子补进 qB")
    print("=" * 74)
    print(f"  库        = {args.db}")
    print(f"  qB        = {config.QB_HOST}")
    print(f"  分类      = {config.PUBLISH_CATEGORY}")
    print(f"  模式      = {'★★ 真写 qB' if args.apply else '💡 只读预览（不写任何东西）'}")
    print(f"  点名 tids = {tids or '（无 ⇒ 仅列候选）'}")
    print()

    # ---------- ★★ --verify-hashes：最早分流（连库都不读）----------
    # ★ 它的输入是**站点种子的 hash**，与库里的行无关 ⇒ 必须在 load_rows **之前**，
    #   否则会在库为空 / 库读不到时**没意义地失败**（而这功能本该独立可用）。
    if args.verify_hashes:
        try:
            qb = qb_client()
        except Exception as e:                              # noqa: BLE001
            print(f"  ✗ 连不上 qB（{config.QB_HOST}）：{type(e).__name__}: {e}")
            print("    ★ QB_HOST 是 127.0.0.1 ⇒ 只**在容器内 / NAS 本机**连得上。")
            return 2
        return verify_hashes(qb, args.verify_hashes)

    # ---------- ★★ --recheck-only：也不读库（同 --verify-hashes 的理由）--------
    # ★ 它的输入是**已在 qB 里的 hash**，与库里的行无关 ⇒ 同样最早分流。
    # ★★ 为什么要有它（不是"再写一遍 --recheck"）：见上面 ⑤ 那段长注释 ——
    #    `--tids … --apply --recheck` 会**先 add 再 recheck**，而 17304 这类
    #    已经在 qB 里了 ⇒ 多一次没必要的写。本入口**只 recheck，不 add**。
    if args.recheck_only:
        try:
            qb = qb_client()
        except Exception as e:                              # noqa: BLE001
            print(f"  ✗ 连不上 qB（{config.QB_HOST}）：{type(e).__name__}: {e}")
            print("    ★ QB_HOST 是 127.0.0.1 ⇒ 只**在容器内 / NAS 本机**连得上。")
            return 2
        return recheck_only(qb, args.recheck_only, apply=args.apply,
                            wait=args.delay)

    # ---------- ★ --check-category / --fix-category：也不需要读库 ----------
    # ★ 它们问的是 qB 自己的标签，与库里的行无关 ⇒ 同样在 load_rows 之前分流。
    # ★★ 但**必须自己连 qB**（2026-09-26 实测踩到：原先直接用了下面那个
    #    `qb`，而它在 `load_rows` 之后才赋值 ⇒ `UnboundLocalError`）。
    #    教训：**分流到"更早"的代码，不能引用"更晚"才建立的变量** ——
    #    而且要**真跑一条**才算测过（我当时只测了 `--help`，`ERR-META-18`）。
    if args.check_category or args.fix_category:
        try:
            qb = qb_client()
        except Exception as e:                              # noqa: BLE001
            print(f"  ✗ 连不上 qB（{config.QB_HOST}）：{type(e).__name__}: {e}")
            print("    ★ QB_HOST 是 127.0.0.1 ⇒ 只**在容器内 / NAS 本机**连得上。")
            return 2
        if args.fix_category:
            return fix_category(qb, None)
        return check_category(qb, None)

    rows = load_rows(args.db, tids)
    if not rows:
        print("  （没有匹配的行）")
        return 0

    print(f"  候选行 = {len(rows)} 条（口径：site_status='EXISTS' 且 site_torrent_id 非空）")
    print()

    # ---------- 连 qB ----------
    try:
        qb = qb_client()
    except Exception as e:                                  # noqa: BLE001
        print(f"  ✗ 连不上 qB（{config.QB_HOST}）：{type(e).__name__}: {e}")
        print("    ★ QB_HOST 是 127.0.0.1 ⇒ 只**在容器内 / NAS 本机**连得上。")
        return 2

    if args.probe:
        probe(qb, rows)
        print()
        print("★ 本脚本没有修改任何东西（qB 只查询；本地库只读）。")
        return 0

    # ---------- 逐条决策 ----------
    print("─" * 74)
    print("【逐条】")
    print("─" * 74)
    todo = []
    for r in rows:
        sid = r["site_torrent_id"]
        title = (r["formatted_title"] or r["torrent_name"] or "")[:44]
        ok_files, why_files = local_files_ok(r)
        save_path, why_sp = resolve_save_path(qb, r)
        mark = "✅" if (ok_files and save_path) else "★"
        print(f"  {mark} sid={sid:<7} status={str(r['status']):<16} {title}")
        print(f"       本地文件 : {'在' if ok_files else '**不在**'}  {why_files}")
        print(f"       save_path: {save_path!r}   ← {why_sp}")
        if ok_files and save_path:
            todo.append((r, save_path))
        else:
            print("       ⇒ ★ 跳过（本地文件不在 ⇒ 加进去也只会从头下载，"
                  "与本脚本目的相反）")
    print()
    print("─" * 74)
    print(f"【可加进 qB】{len(todo)} 条")
    print("─" * 74)

    if not args.apply:
        print()
        print("💡 预览模式，未写 qB。确认后：")
        print("     sudo docker compose run --rm tools python tools/readd_qb.py "
              f"--tids {','.join(r['site_torrent_id'] for r, _ in todo) or '<tids>'} "
              "--apply --recheck")
        print()
        print("★ 本脚本没有修改任何东西（qB 只查询；本地库只读）。")
        return 0

    if not tids:
        # ★ 不点名却要写 ⇒ 拒绝（ERR-META-12：不可逆动作必须显式授权）
        sys.exit("❌ 拒绝写 qB：不点名时可能是几十条 ⇒ 必须用 --tids 显式点名")

    # ---------- 写 qB ----------
    client = MusopiaClient()
    try:
        uid = client.check_login()
    except (SiteError, LoginExpired) as e:
        sys.exit(f"❌ 登录失败（要下载站点种子）：{e}")
    print(f"  check_login() uid = {uid}")
    print()

    added = 0
    rejects = []          # ★★ qB 明说 Fails. 的那些（必须响亮报出）
    no_hash = []          # 读不出 infohash 的（无法复核 ⇒ 必须说清）
    pending = []          # [(row, new_hash)] —— 用于按 hash 复核
    for r, save_path in todo:
        sid = r["site_torrent_id"]
        title = (r["formatted_title"] or "")[:40]
        print(f"  ▶ sid={sid}  {title}")
        content = client.download_torrent(sid)
        if not content:
            print("      ❌ 从站点下载种子文件失败，跳过")
            continue
        new_hash, why = torrent_infohash(content)
        if not new_hash:
            print(f"      ⚠️  读不出站点种子的 infohash（{why}）")
            print("          ⇒ ★ 加进去也**无法复核**（复核只能按 hash 查）")
            no_hash.append(sid)
        # ★ 参数**只有这三个**：save_path / is_paused / category（照抄 readd_to_qb）
        #   ★★ 绝不传 contentLayout / is_root_folder（docstring ①）
        #   ★★ 绝不传 is_skip_checking（docstring ②）
        try:
            ret = qb.torrents_add(torrent_files=content, save_path=save_path,
                                  is_paused=False,
                                  category=config.PUBLISH_CATEGORY)
        except Exception as e:                              # noqa: BLE001
            # ★ 抛异常也是一种"没加成功"，**不能**当成提交成功
            print(f"      ★★ 提交时抛异常：{type(e).__name__}: {e}")
            rejects.append((sid, title, f"抛异常 {type(e).__name__}"))
            continue
        # ══════════════════════════════════════════════════════════════
        # ★★★ 本脚本第一次跑时漏掉的一步（见模块头「实测教训」甲）
        #      `torrents_add()` **返回 qB 的原话**，不是靠抛异常报错：
        #        成功 = "Ok."     失败 = "Fails."
        #      ⇒ 只 print("✅ 已提交") 是**把没抛异常当成加成功了**。
        # ══════════════════════════════════════════════════════════════
        ret_s = str(ret).strip()
        ok_add = ret_s == "Ok."
        if ok_add:
            added += 1
            print(f"      ✅ qB 收下了（返回 {ret_s!r}）"
                  f"  save_path={save_path!r} category={config.PUBLISH_CATEGORY!r}")
        else:
            # ★★ 必须打得**刺眼**：这正是第一次跑丢掉 8 条时的静默形状
            print(f"      ★★★ qB **拒绝**了这条！返回 {ret_s!r}"
                  f"（不是 'Ok.' ⇒ 没入库）")
            print(f"          save_path={save_path!r} category={config.PUBLISH_CATEGORY!r}")
            rejects.append((sid, title, f"返回 {ret_s!r}"))
            continue
        if new_hash:
            print(f"      ★ 站点种子的真 infohash = {new_hash}（≠ 库里的 "
                  f"{r['info_hash'][:12]}…，见模块头 ③）")
            pending.append((r, new_hash, save_path))
            if args.recheck:
                try:
                    qb.torrents_recheck(torrent_hashes=new_hash)
                    print(f"      ★ 已触发 recheck（让 qB 认到本地已有文件）")
                except Exception as e:                      # noqa: BLE001
                    print(f"      ⚠️  recheck 失败：{type(e).__name__}: {e}")

    print()
    print("─" * 74)
    print(f"  ★ qB 收下（返回 'Ok.'）: {added} 条")
    if rejects:
        print(f"  ★★ qB 拒绝            : {len(rejects)} 条  ← **这就是第一次跑丢的 8 条的形状**")
        for sid, title, why in rejects:
            print(f"      sid={sid:<7} {why}   {title[:40]}")
    if no_hash:
        print(f"  ⚠️  读不出 hash（无法复核）: {len(no_hash)} 条 {no_hash}")
    print("─" * 74)

    # ---------- 复核（★ torrents_add 是异步的；★ 一律按 new_hash 查）----------
    if pending:
        import time
        print()
        print("【复核】★ 按**新增的 infohash** 逐个查 qB"
              "（不用标题粗匹配 —— 那会假阴性，见模块头 乙）")
        print(f"  … 等 {args.delay}s 后开始（torrents_add/recheck 都是异步的）")
        time.sleep(args.delay)
        print()
        for r, h, _ in pending:
            sid = r["site_torrent_id"]
            title = (r["formatted_title"] or "")[:34]
            got = qb.torrents_info(torrent_hashes=h)
            if not got:
                print(f"  ❔ sid={sid} {title}")
                print(f"       按 hash {h[:12]}… **此刻查不到**")
                print("       ★ 分辨法：**再等一会儿重查**（用 --verify-hashes，只读）")
                print("         ⇒ 若几分钟后仍查不到 ⇒ 判定**没入库**（异步不会这么久）")
                continue
            report_task(f"sid={sid} {title}  ", h, got[0])

    print()
    print("★ 本脚本没有修改本地库（只读）与站点；qB 只做了 add + recheck。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
