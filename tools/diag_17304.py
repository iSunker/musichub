"""★ 只读：把 sid=17304（`緑黄色社会 - あたまご`）的**现场**一次性查清。

═══════════════════════════════════════════════════════════════════════════
为什么要专门为这一条写脚本（而不是"顺手"跑别的）
═══════════════════════════════════════════════════════════════════════════
用户问：「皇后在 100% 做种，为什么乌托邦校验到 35% 就暂停了？有没有加种后
自动校验的功能？」

★ 定性（`readd_qb.py:18-19`）：
      `緑黄色社会 - あたまご   site_id=17304  本地文件**已丢失**`
      `⇒ qB 里在，但 stalledDL 35.5%（下载卡住，与回加无关）`

★★ 但那是**上一轮的读数**，不是现场证据（`ERR-META-01`：判据要先验一次）。
   而用户已倾向「② 从皇后下原种补文件，再 recheck」—— 那是个**有代价**的动作
   （几百 GB 下载 + 首次校验读一遍盘）。⇒ **动手前必须先把下面这几条测出来**：

   1. **qB 里这条现在是什么 state** —— `stalledDL`？还是已经变了？
      ★ 这决定走哪条路（见 `qb_state_probe.STATE_MEANING`）。
   2. **它在 qB 里的两个 hash 各是什么** —— 库里的 `info_hash`（皇后原种）
      与站点产物的 hash **不是一回事**（`readd_qb.py` 模块头 ③）。
      ⇒ 两个都要查：前者是"本地在跑的老种"，后者才是"现在这条"。
   3. **`content_path` / `save_path` 指着哪** —— 补文件就是往那里补。
   4. **那个目录在不在、里面有几项、占多大** ——
      ★★ 这是"文件到底丢没丢"的**唯一现场判据**。35.5% **推不出**丢没丢：
         35.5% 既可能是"文件全没了"，也可能是"文件**在**、只是 qB 没认到"
         （后者才是 `--recheck` 能救的）。**两者修法完全相反。**
   5. **qB 自己说这条"要多少"** —— `t.size` / `t.completed` / `t.amount_left`。
      ★ 与第 4 条磁盘实测**对照**：磁盘远大于 qB 说的 size ⇒ 大概率文件在。
   6. **同 save_path 下有没有别的 `緑黄色` 相关目录** —— 文件可能**改了名**
      （qB 按 metainfo 里的文件名找 ⇒ 改名 = 找不到 = 从 0 重下）。

═══════════════════════════════════════════════════════════════════════════
★★ 本脚本**一个写操作都没有**（逐条列出来，便于核对）
═══════════════════════════════════════════════════════════════════════════
  qB 侧：只调 `torrents_info()` / `torrents_files()`（**都是查询**）
         ✗ 不 add ✗ 不 delete ✗ 不 recheck ✗ 不 pause ✗ 不改 category/save_path
  站点侧：**完全联网**。只从本地 `data.db` 读 `opencd_tid` 用来显示。
  库侧：  `PRAGMA query_only=1`，只 SELECT 一行（`tasks` 表）。
  磁盘：  只 `os.path.isdir` / `os.scandir` / `os.stat`（**只读**）。
          ★ 不 mkdir / 不 rename / 不 rm。

★ 本脚本**不 import `publish.py`**（它 import 期就连站点 + 有别的副作用），
  也不 import `musopia`。⇒ 可在纯只读场景独立跑。

用法（在 NAS 上，compose 目录）:
    sudo docker compose run --rm tools python /data/tools/diag_17304.py

    # 换一条 sid（万一要看别的）
    sudo docker compose run --rm tools python /data/tools/diag_17304.py --sid 17288

    # 顺便把那条**站点种子**也拉下来读真 infohash（★ 这会花 1 个配额）
    sudo docker compose run --rm tools python /data/tools/diag_17304.py --with-site
"""
import argparse
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.environ.get("MUSOPIA_APP_DIR", "/app"))

import config  # noqa: E402

# ★★ sid=17304 的**两个可能 hash** 都要能查：
#   · 库里的 `info_hash` = **皇后原种**（本地在跑的老种，若有）
#   · 站点产物的 hash  = `run.py` 用新 announce 重生成的
#     ★ 后者**只能**从站点种子字节里读出来（`readd_qb.py` 模块头 ③），
#       所以本脚本**不猜**它 —— 要么 `--with-site` 去读，要么从
#       `qB 里名字匹配的任务` 兜（后者会**明确标成"兜到的，不是铁证"**）。


def load_row(db_path, sid):
    """★ 只读。按 `site_torrent_id` 取那一行（就是 `readd_qb.py` 的 SELECT_SQL 口径）。"""
    con = sqlite3.connect(db_path, timeout=30)
    con.execute("PRAGMA query_only=1")
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT info_hash, formatted_title, torrent_name, status, site_status, "
            "       site_torrent_id, content_path, opencd_tid "
            "  FROM tasks WHERE site_torrent_id = ?", (str(sid),)).fetchone()
    finally:
        con.close()


def du(path):
    """★ 只读：目录占多少字节（走一遍 os.scandir，不 stat 每个文件的 mtime）。"""
    total = nfiles = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.stat(fp).st_size
                nfiles += 1
            except OSError:
                pass
    return total, nfiles


def human(n):
    """字节 → 人话。★ 只用 1024 进制，别混着来（免得和 qB 的数字差一个量级）。"""
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if x < 1024 or unit == "TiB":
            return f"{x:.2f} {unit}"
        x /= 1024
    return f"{x:.2f} TiB"


def dir_report(label, path):
    """★ 现场判据：那个目录**到底在不在**、里面有什么（只读）。

    ★★ 绝不把"读不到"并进"没有"（`ERR-SQL-05`）：两种情况分开印。
    """
    print(f"  【{label}】{path!r}")
    if not path:
        print("      ❔ 路径为空 ⇒ **无法判断**（不许当成'没有'）")
        return None
    if not os.path.exists(path):
        print("      ★★ **不存在**（os.path.exists = False）")
        return None
    if not os.path.isdir(path):
        print("      ⚠️ 存在但**不是目录**（是个文件？）")
        return None
    try:
        entries = list(os.scandir(path))
    except OSError as e:
        print(f"      ❔ 读目录失败：{type(e).__name__}: {e} ⇒ **读不到，不等于空**")
        return None
    size, nfiles = du(path)
    print(f"      ✅ 目录存在：**{len(entries)} 个顶层项 / {nfiles} 个文件 / "
          f"{human(size)}**")
    for e in sorted(entries, key=lambda x: x.name)[:12]:
        try:
            if e.is_dir(follow_symlinks=False):
                s, n = du(e.path)
                print(f"          [目录] {e.name}/   ({n} 文件, {human(s)})")
            else:
                print(f"          [文件] {e.name}   ({human(e.stat().st_size)})")
        except OSError as ex:
            print(f"          [?]    {e.name}   （读失败 {type(ex).__name__}）")
    if len(entries) > 12:
        print(f"          … 另有 {len(entries) - 12} 项")
    return size


def siblings(save_path, needle):
    """★ 同 save_path 下有没有**名字相近**的目录 —— 文件可能被改名/搬走过。

    ★ 只列**同级的兄弟目录名**，不下钻内容（下钻交给 `dir_report`）。
    """
    print(f"  【同级兄弟】在 {save_path!r} 下找名字含 {needle!r} 的项")
    if not save_path or not os.path.isdir(save_path):
        print("      ❔ save_path 不存在/为空 ⇒ **跳过**（读不到 ≠ 没有）")
        return
    try:
        hits = [e.name for e in os.scandir(save_path) if needle.lower() in e.name.lower()]
    except OSError as e:
        print(f"      ❔ 读失败：{type(e).__name__}: {e}")
        return
    if not hits:
        print("      （一个都没有 —— ★ 注意这**也是**'此处没有'的结论，")
        print("        但仅限**这一层**：别的地方有同名目录看不出来）")
    for h in sorted(hits):
        print(f"      · {h}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="★ 只读：把 sid=17304 的现场查清（qB state / 两个 hash / 磁盘实况）")
    ap.add_argument("--sid", default="17304", help="site_torrent_id（默认 17304）")
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--with-site", action="store_true",
                    help="★ 从站点下载那个种子读真 infohash（**会花 1 个配额**）")
    ap.add_argument("--needle", default="緑黄色",
                    help="在同 save_path 下找兄弟目录用的关键词（默认 緑黄色）")
    args = ap.parse_args(argv)

    print("=" * 74)
    print(f"★ 只读现场诊断：sid={args.sid}")
    print("=" * 74)
    print("  ★ 本脚本**一个写操作都没有**：qB 只查询 / 库只读 / 磁盘只读 /")
    print("    站点默认不联网（除非 --with-site）")
    print()

    # ---------- ① 库 ----------
    print("─" * 74)
    print("【① 库里的那一行】（只 SELECT，`PRAGMA query_only=1`）")
    print("─" * 74)
    row = load_row(args.db, args.sid)
    if row is None:
        print(f"  ✗ 库里**没有** site_torrent_id={args.sid} 的行")
        print("    ★ 这本身是个结论：**先别动 qB** —— 得先搞清这条该怎么定位")
        return 1
    db_hash = row["info_hash"] or ""
    for k in row.keys():
        v = row[k]
        if k == "info_hash":
            v = f"{v}   ← ★ 库里记的（= **皇后原种**，不是站点产物的 hash）"
        print(f"  {k:<16} = {v!r}" if not isinstance(v, str) or "←" not in str(v) else f"  {k:<16} = {v}")
    print()

    # ---------- ② qB ----------
    print("─" * 74)
    print("【② qB 里这条的现状】（只 `torrents_info` / `torrents_files`）")
    print("─" * 74)
    try:
        from qbittorrentapi import Client
    except ImportError as e:                                   # noqa: BLE001
        print(f"  ✗ 没装 qbittorrentapi（{e}）")
        return 2
    try:
        qb = Client(host=config.QB_HOST, username=config.QB_USER,
                    password=config.QB_PASS)
        qb.auth_log_in()
    except Exception as e:                                     # noqa: BLE001
        print(f"  ✗ 连不上 qB（{config.QB_HOST}）：{type(e).__name__}: {e}")
        print("    ★ QB_HOST=127.0.0.1 ⇒ 只有**在容器内 / NAS 本机**连得上。")
        return 2

    # ★ 两个 hash 都查：库里的（皇后原种）+ 名字兜到的（站点产物）
    got_db = list(qb.torrents_info(torrent_hashes=db_hash)) if db_hash else []
    print(f"  按**库里的 info_hash** 查（{db_hash[:16]}…）："
          f"{'✅ 找到' if got_db else '❌ 不在 qB 里（这个原种没在跑）'}")
    for t in got_db:
        print(f"      name         = {t.name!r}")
        print(f"      state        = {t.state}   progress={t.progress * 100:.2f}%")
        print(f"      save_path    = {t.save_path!r}")
        print(f"      content_path = {t.content_path!r}")
        print(f"      category     = {t.category!r}   hash={t.hash}")
    print()

    # ★★ 名字兜：**站点产物**的 hash 读不出来时只能这样找，但必须标清"不是铁证"
    all_t = list(qb.torrents_info())
    needle = (row["formatted_title"] or "")[:10] or ""
    keys = [x for x in (needle, "あたまご", "緑黄色") if x]
    hits = [t for t in all_t if any(k.lower() in (t.name or "").lower() for k in keys)]
    print(f"  ★ 名字兜（关键词 {keys}）—— **兜到的不算铁证**（`readd_qb.py` 教训乙）：")
    if not hits:
        print("      一条都没兜到 ⇒ ★ 可能在 qB 里**根本不叫这个名字**（站点产物的")
        print("      name 与库里 formatted_title 不一定同前缀）⇒ 别据此下'没有'的结论")
    for t in hits:
        print(f"      hash={t.hash}")
        print(f"        name         = {t.name!r}")
        print(f"        state        = {t.state}   progress={t.progress * 100:.2f}%")
        print(f"        size         = {human(t.size)}   completed={human(t.completed)}"
              f"   left={human(t.amount_left)}")
        print(f"        save_path    = {t.save_path!r}")
        print(f"        content_path = {t.content_path!r}")
        print(f"        category     = {t.category!r}")
        if getattr(t, "error", 0):
            print(f"        ⚠️ error     = {t.error}")
        if getattr(t, "message", ""):
            print(f"        message      = {t.message!r}")
        # ★ 逐个文件列出 qB 自己认到的进度：这是"丢没丢文件"的 **qB 侧**判据
        try:
            fl = list(qb.torrents_files(torrent_hash=t.hash))
            print(f"        【qB 认到的文件】共 {len(fl)} 个")
            prio = {0: "跳过", 1: "普通", 6: "★不下载", 7: "★★仅种子"}

            # ★★★ 先算**汇总**，再看明细（2026-09-26 修）
            #   原先只印前 20 个，然后「… 另有 60 个」—— 而**没印出来的那 60 个
            #   恰恰是可能藏着'没下完'的地方**。结果是：给人看的那一眼
            #   **看不出"到底有几个文件 < 100%"**（`ERR-META-22` 同族：
            #   判据落在被截断的那一段里）。
            #   ⇒ 汇总必须**算出来**，不许靠人扫 80 行（`ERR-META-01`：精确计算）。
            done = [f for f in fl if f.progress >= 0.9999]
            part = [f for f in fl if 0 < f.progress < 0.9999]
            zero = [f for f in fl if f.progress <= 0]
            skipped = [f for f in fl if getattr(f, "priority", None) in (0, 6, 7)]
            print(f"            ⇒ ★★ 100% 的 = **{len(done)}**   "
                  f"部分(<100%) = **{len(part)}**   0% 的 = **{len(zero)}**")
            if skipped:
                print(f"            ⇒ ⚠️ 其中**不该下/仅种子**的 = {len(skipped)}"
                      f"（优先级 跳过/不下载/仅种子）★ 那些**永远到不了 100%**，"
                      f"别把它们当成'没下完'")
            if part or zero:
                print("            ★★ **有文件不满** —— 这就是 progress 上不去的直接原因：")
                for f in sorted(part + zero, key=lambda x: x.progress)[:25]:
                    pr = prio.get(getattr(f, "priority", None),
                                  f"?prio={getattr(f,'priority',None)}")
                    print(f"                {f.progress * 100:6.2f}%  {pr:<8} "
                          f"{human(getattr(f,'size',0)):>10}  {f.name}")
                if len(part) + len(zero) > 25:
                    print(f"                … 另有 {len(part) + len(zero) - 25} 个不满的")
            else:
                print("            ✅ **一个不满的都没有**（全部 100%）")
                print("               ⇒ ★★ 若此时 state 仍是 stalledDL / 报 missingFiles，")
                print("                  那**不是文件内容问题** ⇒ 是 qB 认不到**路径**")
                print("                  （见 ③ 的实测路径 vs content_path）")

            # ★ 明细只印**前 8 个**（够看形状即可；结论已由上面的汇总给出）
            print(f"            【前 8 个示例】")
            for f in sorted(fl, key=lambda x: x.name)[:8]:
                pr = prio.get(getattr(f, "priority", None),
                              f"?prio={getattr(f,'priority',None)}")
                print(f"                {f.progress * 100:6.1f}%  {pr:<8} "
                      f"{human(getattr(f,'size',0)):>10}  {f.name}")
        except Exception as e:                                 # noqa: BLE001
            print(f"        ❔ 取文件列表失败：{type(e).__name__}: {e}")
    print()

    # ---------- ③ 磁盘实况 ----------
    print("─" * 74)
    print("【③ 磁盘实况】★ **这条才是'文件丢没丢'的现场判据**")
    print("─" * 74)
    targets = []
    for t in got_db + hits:
        for p in (t.content_path, t.save_path):
            if p and p not in [x[1] for x in targets]:
                targets.append((f"qB 报的 {t.state}", p))
    if not targets:
        cp = (row["content_path"] or "").rstrip("/")
        if cp:
            targets.append(("库里 content_path", cp))
    if not targets:
        print("  ❔ qB 里查不到、库里 content_path 也空 ⇒ **无路径可查**")
        print("     ★ 不许把这读成'文件没了'（`ERR-SQL-05`）")
    for label, p in targets:
        dir_report(label, p)
        print()
    want = (row["content_path"] or "").rstrip("/")
    if want:
        siblings(os.path.dirname(want) or want, args.needle)
    print()

    # ---------- ④ 可选：站点种子的真 hash ----------
    if args.with_site:
        print("─" * 74)
        print("【④ 站点种子的真 infohash】（★ 花 1 个配额，过 RateGate）")
        print("─" * 74)
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import readd_qb                                     # noqa: E402
            import torf                                         # noqa: F401,E402
            _g, client = readd_qb.make_gate_and_client()
            content = client.download_torrent(row["site_torrent_id"])
            if not content:
                print("  ✗ 从站点下载种子失败")
            else:
                nh, why = readd_qb.torrent_infohash(content)
                if nh:
                    print(f"  ✅ 站点产物真 infohash = {nh}")
                    print(f"  ★ 与库里的（{db_hash[:16]}…）**不同**"
                          f" ⇒ 说明确实是重生成的（模块头 ③）")
                    g2 = list(qb.torrents_info(torrent_hashes=nh))
                    print(f"  按这个 hash 查 qB：{'✅ 找到（这才是现在这条）' if g2 else '❌ 不在 qB 里'}")
                    for t in g2:
                        print(f"      state={t.state} progress={t.progress * 100:.2f}% "
                              f"name={t.name!r}")
                else:
                    print(f"  ✗ 读不出 infohash：{why}")
        except Exception as e:                                 # noqa: BLE001
            print(f"  ✗ 查站点时出错：{type(e).__name__}: {e}")
        print()

    print("=" * 74)
    print("★ 本脚本没有修改任何东西（qB 只查询 / 库只读 / 磁盘只读）。")
    print("  ⇒ 下一步该走哪条路由**上面这些读数**决定，不由'35.5%'这个数字决定。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
