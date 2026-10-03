"""★ 只读：为 `mv` 做**搬前核对** —— 目标目录名必须与 metainfo 的 `name` 完全一致。

═══════════════════════════════════════════════════════════════════════════
为什么必须先把 `name` 从 metainfo 里读出来（不能从路径反推）
═══════════════════════════════════════════════════════════════════════════
`17304` 的站点产物（`02afb787…`）数据完整躺在：

    /downloads/incomplete/[260909] 緑黄色社会 - あたまご [初回生産限定盤A][ESCL-6291~2][FLAC+CUE+LOG+PNG+BDMV]/
        ⇒ 80 个文件 / 22.61 GiB（已实测，与 qB 报的 size 精确相符）

而 qB 期望它在：

    /downloads/[260909] 緑黄色社会 - あたまご [初回生産限定盤A][ESCL-6291~2][FLAC+CUE+LOG+PNG+BDMV]/

★ 两个路径**看起来**只差一个 `incomplete/` —— 但**"看起来"不算判据**（`ERR-META-01`）。
  qB 是按 **metainfo 里的 `info.name`** 去找目录的。⇒ 动手搬之前必须证明：

      ① metainfo 的 `info.name` == 目标目录名（**逐字节**，含空格/全角/波浪号）
      ② metainfo 是**多文件**结构还是**单文件**（决定 `mv` 搬的是"目录"还是"文件"）
      ③ 搬的目标位置**当前不存在**（否则 `mv` 会把源塞进去变成子目录 —— 静默错）

  ★★ 三者任一不成立，`mv` 就会把一次"零风险搬移"变成"把好文件搬乱"。

  ★★★ 2026-09-26 实测：**①+② 初版都是错的**，两条都是"静默给错答案"：
    · ① 用了 `torf` 的 `t.name` —— 单文件种子里它是**文件名**，不是 `info.name`
      （`ERR-META-24`）⇒ 会去找一个不存在的目录、还报"源没找到"。
    · ② 用"数 `t.files`"判单/多文件 —— 单文件时也有 1 项 ⇒ 必然误判。
    ⇒ 现在只认 `t.metainfo['info']['name']` 与 `info` 里**有没有 `files` 键**。

═══════════════════════════════════════════════════════════════════════════
★ 本脚本**纯只读**（逐条列出）
═══════════════════════════════════════════════════════════════════════════
  站点：**会联网 1 次**（`download.php?id=17304`）⇒ **花 1 个配额**（乌托邦站自己的）
        —— 因为 `name` 的权威来源是**站点那个种子的 metainfo**
        （qB 里的任务就是用它加的）。★ 本站是**乌托邦**（`MusopiaClient`），
        **不走皇后站的 RateGate**（两个站、两套配额账本，别混）。
  qB：  只 `torrents_info` / `torrents_files`（查询）
  磁盘：只 `os.path.isdir` / `os.path.exists` / `os.scandir` / `os.stat`
  库：  ★ **完全不读**
  写：  ★ **零** —— ✗ 不 mv ✗ 不 rename ✗ 不 mkdir ✗ 不 pause ✗ 不 recheck

★ 它只**打印**搬移方案（源 / 目标 / 命令），**由人来执行**。

用法（在 NAS 上，compose 目录）:
    sudo docker compose run --rm tools python /data/tools/plan_mv_17304.py
    # 换一条 sid
    sudo docker compose run --rm tools python /data/tools/plan_mv_17304.py --sid 17288
    # 不从站点下种子（★ 那就读不到权威 name ⇒ 只报"读不到"，不猜）
    sudo docker compose run --rm tools python /data/tools/plan_mv_17304.py --no-site
"""
import argparse
import io
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

import _bootstrap  # noqa: E402,F401  —— 把 <repo> 与 <repo>/src 都放进 sys.path（源码已移进 src/）
sys.path.insert(0, os.environ.get("MUSOPIA_APP_DIR", "/app"))

import config  # noqa: E402


def human(n):
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if x < 1024 or unit == "TiB":
            return f"{x:.2f} {unit}"
        x /= 1024
    return f"{x:.2f} TiB"


def read_metainfo(content):
    """★ 从站点种子的字节里读 metainfo（`torf` 已在镜像里，见 Dockerfile）。

    返回 (info_dict, why)。★ 读不出来 ⇒ **返回 None + 原因，绝不猜**（`ERR-META-14`）。

    ★★★ 2026-09-26 实测踩到（**第二类**错，与"客户端接错"无关）：
      `torf.Torrent.name` **不等于** `info.name` —— 它是 torf 自己的"这个名字"：
        · multi-file  (info 里有 `files`)  ⇒ `.name` == info.name（顶层目录名）✔
        · single-file (info 里只有 `length`) ⇒ `.name` == **文件名** ✘
      上面"用 `t.name`"的写法，在**单文件**种子上会把「文件名」当成「目录名」，
      于是 ② 段会去 `incomplete/` 里找**一个不存在的目录**，还理直气壮
      报"源没找到" —— 假阴性，且**看起来像发现**（`ERR-META-01` 的形状）。
      ⇒ 权威来源只有一个：**`metainfo['info']['name']`**（原样取出，不经过 torf
      的任何"加工"）。单/多文件也只由 `info` 里**有没有 `files` 键**决定 ——
      不数 `t.files`（单文件时它也有 1 项 ⇒ 数出来会误判成多文件）。
    """
    try:
        import torf
    except ImportError as e:                                    # noqa: BLE001
        return None, f"缺 torf 库（{e}）"
    try:
        t = torf.Torrent.read_stream(io.BytesIO(content))
    except Exception as e:                                      # noqa: BLE001
        return None, f"解析种子失败：{type(e).__name__}: {e}"
    # ★★ 只认 `info` 里的原样字节，不做任何推断
    try:
        info = t.metainfo.get("info") or {}
        name = info.get("name")
        if not isinstance(name, str) or not name:
            return None, "metainfo 里没有 `info.name`（★ 不许猜成别的字段）"
        # ★ 单/多文件判据 = info 里有没有 `files` 键（不是数 t.files）
        is_multi = "files" in info
        return {"name": name,
                "is_multi": is_multi,
                "file_count": len(info["files"]) if is_multi else 1,
                "infohash": str(t.infohash)}, ""
    except Exception as e:                                      # noqa: BLE001
        return None, f"取 info.name 失败：{type(e).__name__}: {e}"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="★ 只读：为 mv 做搬前核对（metainfo 的 name vs 目标目录名）")
    ap.add_argument("--sid", default="17304", help="site_torrent_id（默认 17304）")
    ap.add_argument("--no-site", action="store_true",
                    help="★ 不从站点下种子（省 1 个配额，但读不到权威 name ⇒ 不猜）")
    ap.add_argument("--incomplete", default="/downloads/incomplete",
                    help="qB 的未完成区（默认 /downloads/incomplete）")
    ap.add_argument("--dest-root", default="/downloads",
                    help="完成区（默认 /downloads）")
    args = ap.parse_args(argv)

    print("=" * 74)
    print(f"★ 搬前核对：sid={args.sid}（**只读**；除 --site 那 1 个请求外不联网）")
    print("=" * 74)

    # ── ① 从站点种子读权威 name ────────────────────────────────────────
    meta = None
    if args.no_site:
        print("【① metainfo】★ --no-site ⇒ **跳过**（读不到权威 name ⇒ 不猜）")
    else:
        print("【① metainfo】从站点种子的字节里读权威 `info.name`")
        print("     ★ 本站在乌托邦（`/download.php`），**不走皇后站的 RateGate**"
              "（那两个是不同的站、不同的配额账本）")
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            # ══════════════════════════════════════════════════════════════
            # ★★★ 2026-09-26 实测**连踩两次**——两次都是"客户端接错了"，记下来
            #      免得再犯：
            #   ① 我先写 `readd_qb.make_gate_and_client()` ⇒ `AttributeError`（脚本本已合并入 `tools/readd.py`）
            #      （那函数**不在** readd_qb 里，在 `probe_subtitles.py` 里）；
            #   ② 改成 `probe_subtitles.make_gate_and_client()` 后 ⇒ 又报
            #      `'Client' object has no attribute 'download_torrent'`。
            #  根因：这个项目里有**两个不同的 `Client`**：
            #      · `crawl.Client`            = **皇后站**（只有 `.get()`）
            #      · `musopia.MusopiaClient`   = **乌托邦站**（有
            #        `.download_torrent()`，自己管 cookie，**不共用** RateGate）
            #  而 sid=17304 是**乌托邦站**的种子 ⇒ 该用 `MusopiaClient`，
            #  权威依据不是"哪个名字看着对"，而是 `tools/readd.py` 的 qb 段里**干这
            #  件事的那一处**用的就是 `MusopiaClient`（`ERR-META-03`：同一件事
            #  只能有一处实现 ⇒ 照抄那一处，不另造）。
            # ★ 「照抄哪一处」的判据是**它做的是同一件事**（下乌托邦的种子），
            #   不是"函数名像"—— 这正是前两次错的原因。
            # ══════════════════════════════════════════════════════════════
            from musopia import MusopiaClient, SiteError, LoginExpired
            client = MusopiaClient()
            try:
                uid = client.check_login()
            except (SiteError, LoginExpired) as e:
                sys.exit(f"  ✗ 登录失败（要下载站点种子）：{e}\n"
                         "     ⇒ ★ 更新 .env 里的 MUSOPIA_COOKIE 后重跑")
            print(f"     check_login() uid = {uid}")
            content = client.download_torrent(args.sid)
            if not content:
                print("  ✗ 从站点下载种子失败 ⇒ **读不到 name，不许猜**")
            else:
                meta, why = read_metainfo(content)
                if meta:
                    print(f"  ✅ info.name = {meta['name']!r}")
                    print(f"     infohash  = {meta['infohash']}")
                    print(f"     结构      = "
                          f"{'多文件（info 有 `files` 键）' if meta['is_multi'] else '★ 单文件（info 只有 `length`）'}"
                          f"　文件数 = {meta['file_count']}")
                else:
                    print(f"  ✗ {why}")
        except Exception as e:                                  # noqa: BLE001
            print(f"  ✗ 出错：{type(e).__name__}: {e}")
            print("     （配额满会报 DailyQuotaExceeded —— 那是闸门在工作，不是坏了）")
    print()

    # ── ② 磁盘现状：源 / 目标 ─────────────────────────────────────────
    print("【② 磁盘现状】")
    inc = args.incomplete
    # ★★ 源**是什么形状**由 metainfo 决定，不是"总是一个目录"：
    #     · 多文件 ⇒ 源是**目录** `incomplete/<info.name>/`
    #     · 单文件 ⇒ 源是**文件** `incomplete/<info.name>`（info.name 就是文件名）
    #    两种都按"名字与 info.name **逐字节相同**"来找（`ERR-META-01`：看着像不算）。
    src = None
    if not os.path.isdir(inc):
        print(f"  ❔ {inc!r} **不是目录/不存在** ⇒ 读不到 = 不许当成'没有'")
    else:
        want_dir = (meta["is_multi"] if meta else True)
        try:
            if meta and not want_dir:
                # 单文件：源是个**文件**
                cand = os.path.join(inc, meta["name"])
                entries = [e for e in os.scandir(inc) if e.is_file(follow_symlinks=False)]
                print(f"  ★ metainfo 是**单文件**种子 ⇒ 源应为**文件** "
                      f"{inc!r}/{meta['name']!r}")
            else:
                cand = None
                entries = [e for e in os.scandir(inc) if e.is_dir(follow_symlinks=False)]
                if meta:
                    print(f"  ★ metainfo 是**多文件**种子 ⇒ 源应为**目录** "
                          f"{inc!r}/{meta['name']!r}")
        except OSError as e:
            entries = []
            print(f"  ❔ 读 {inc!r} 失败：{type(e).__name__}: {e}")
        print(f"  {inc!r} 下有 {len(entries)} 个{'文件' if (meta and not want_dir) else '目录'}:")
        for e in sorted(entries, key=lambda x: x.name):
            print(f"      · {e.name}")
        # ★ 源 = name 与 metainfo 相符的那个（**逐字节比较**，不是"看着像"）
        if meta:
            for e in entries:
                if e.name == meta["name"]:
                    src = e.path
            if src:
                print("  ✅ **源找到了**（名字与 metainfo 的 info.name **逐字节相同**）：")
                print(f"      {src!r}")
            else:
                print("  ★★ **源没找到**：`incomplete/` 下没有任何一项的名字 == "
                      "metainfo 的 info.name")
                print("      ⇒ ★ 说明磁盘上的名字与 metainfo **不一致**")
                print("      ⇒ ★★ 这本身是个发现：先弄清差在哪，**别急着 mv**")
    print()

    # ── ③ 目标路径 ───────────────────────────────────────────────────
    dest = os.path.join(args.dest_root, meta["name"]) if meta else None
    print("【③ 目标路径】")
    if dest:
        print(f"  目标 = {dest!r}")
        print(f"  ★ 现在存在吗？ {os.path.exists(dest)}")
        if os.path.exists(dest):
            # ★★ 目标已存在 ⇒ `mv 源 目标` 会把源**塞进目标里面**（静默错！）
            print("  ★★ **目标已存在** ⇒ 直接 `mv 源 目标` 会变成 "
                  "`目标/源/`（**塞进去**）—— 这是静默错误，绝不能这么搬")
            print("      ⇒ ★ 先看清目标里是什么（空目录？还是同一批文件的一份拷贝？）")
            try:
                n = sum(1 for _ in os.scandir(dest))
                print(f"        目标里现有 {n} 个顶层项")
            except OSError as e:
                print(f"        ❔ 读目标失败：{type(e).__name__}: {e}")
        else:
            print("  ✅ 目标**不存在** ⇒ `mv 源 目标` 语义正确（重命名式搬移）")
    else:
        print("  ★ 读不到 metainfo ⇒ **目标路径定不出来**（不猜）")
    print()

    # ── ④ 搬移方案（★ 只打印，由人执行）────────────────────────────────
    print("=" * 74)
    print("【④ 搬移方案】★ 本脚本**没有**执行任何一步 —— 下面是给人执行的")
    print("=" * 74)
    # ★★ 三条**允许给方案**的必要条件，缺一不可（`ERR-META-12` 的形状：
    #    有副作用的事，前提不齐就**不许**给"照着做"，否则等于变相授权一个错动作）：
    #      ① 源找到了（名字逐字节相符）
    #      ② 目标定得出来
    #      ③ ★ 目标**不存在** —— 否则 `mv 源 目标` 会塞进去
    dest_exists = bool(dest) and os.path.exists(dest)
    if src and dest and not dest_exists:
        print("★★ 顺序很重要：**必须先让 qB 停止这个任务**，再搬文件。")
        print("   理由：否则 qB 正在读写那批文件，两个写者同时动"
              "（`ERR-DOCKER-03` 的形状）")
        print()
        print(f"  源   = {src}")
        print(f"  目标 = {dest}")
        print(f"  ★ 单文件种子 ⇒ 搬的是**文件**（不是目录）："
              f"{'是' if (meta and not meta['is_multi']) else '否'}")
        print()
        print("  ① 停任务（只读查状态，确认到不是 running 再做下一步）")
        print(f"     → 用 tools/readd.py qb 的 --recheck-only 预览确认 hash：")
        print(f"       sudo docker compose run --rm tools python "
              f"tools/readd.py qb --verify-hashes <02afb787…>")
        print("  ② 停止该任务（★ 这是 qB 写操作，要显式做）")
        _what = "目录" if meta["is_multi"] else "文件"
        print(f"  ③ 搬**{_what}**（★ 用 mv，同文件系统内 ⇒ 秒级、不复制字节）")
        print(f"       mv {src} {dest}")
        print("  ④ 起任务 + recheck ⇒ 应立刻 100% 做种")
    elif dest_exists:
        # ★★ 目标已存在 ⇒ **绝不给 mv 方案**（那条命令正是静默错的形状）
        print("  ★★ **不给方案**：目标**已经存在** ⇒ `mv 源 目标` 会变成 "
              "`目标/源/`（塞进去）")
        print("      ⇒ ★ 先弄清目标里那 N 项是什么：")
        print("          · 若它是**同一批文件的另一份** ⇒ 不需要搬（要弄清哪个是准的）")
        print("          · 若它是**空目录/残留** ⇒ 删掉它再搬（★ 删之前先看清楚）")
    else:
        print("  ★★ 条件不齐（源 或 目标 定不出来）⇒ **不给方案**。")
        print("     先解决上面标的那个问题（读不到 name / 源没找到 / 目标已存在）。")
    print()
    print("★ 本脚本没有修改任何东西（磁盘只读、qB 只查询、库没读）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
