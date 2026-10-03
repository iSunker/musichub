"""C' **第 4 步**的只读验证：`scan` 跑过一轮之后，皇后路已上站行的标题**不许被动**。

═══════════════════════════════════════════════════════════════════════════
它在验什么（以及**不**在验什么 —— 这条最容易被读错）
═══════════════════════════════════════════════════════════════════════════
C' = "`run.py` 的 `scan` 不再给**皇后路**（`opencd_tid` 非空）的行写
`formatted_title`"（那一列归 `opencd_ingest.build_row()` = `mapped.title`）。

★ 本工具判的是「**没被动**」，**不是**「变得对了」：
    · "没被动"   = C' 生效 ✅  ← 本工具
    · "变得对了" = 第 5 步（`tools/fix_onsite_titles_and_subtitles.py`）的活
  ★★ 这两件事**必须分开读**，否则会把"`scan` 没动它"误读成"`scan` 修好了它"。

═══════════════════════════════════════════════════════════════════════════
三条纪律（缺一条这个工具就不可信）
═══════════════════════════════════════════════════════════════════════════
① **只读**：`PRAGMA query_only=1`，照 `nas_health.py` 的成例。**不 cp 生产库**
   （`ERR-SQL-01`）。★ 本文件里没有一处 `INSERT` / `UPDATE` / `DELETE`。
② **口径不手写**：那批行由 `db.queen_published_rows()` 取，而它内部只用
   `db.QUEEN_PUBLISHED_WHERE`（**唯一真源**）。⇒ `ERR-META-03` 的根被拔掉。
   ★ 本文件里**没有任何一处** WHERE 字符串是现拼的。
③ **判据是"值"，不是时间戳**：
   `formatted_title` 的**值本身**才判"有没有被动"。
   `updated_at` 只能当**旁证**（`scan` 写别的列也会刷它）——
   ⇒ 二者都印，**绝不**拿 `updated_at` 代替列值（`ERR-META-05`：量纲不同）。

═══════════════════════════════════════════════════════════════════════════
用法（★ **先拍基线，再等 scan，再比对**）
═══════════════════════════════════════════════════════════════════════════
    # ① scan 跑之前：拍基线
    python tools/verify_cprime_step4.py --baseline-out .scratch/q4_before.json

    # ② 等一轮 scan 跑完（用 python tools/nas_health.py 看 scan.last_run 推进）

    # ③ 比对
    python tools/verify_cprime_step4.py --baseline .scratch/q4_before.json

★ 第 ① 步若已经晚了（`scan` 已经跑过），工具会**自己说出来**（见「时序自检」）——
  它不假装基线是干净的。**"基线不干净"也要如实报，不许静默当干净。**
"""
import argparse
import json
import os
import pathlib
import re
import sqlite3
import sys
import time

# ★★★ `config` 在 **import 期**就 `require_env()` 这些键 ⇒ `import db` 会直接抛异常。
#   而它们在生产上只存在于 `.env`（`deploy.sh` 的 `NEVER` 名单**故意不同步**它）
#   ⇒ **NAS 宿主 shell 里没有**，本工具就起不来：
#         ❌ 缺少必需的环境变量 QB_PASS，请检查 .env 文件
#
#   ★★ 但本工具**不需要这些值是真的** —— 它一个网络请求都不发
#      （`_snapshot()` 只走 `PRAGMA query_only=1` 的只读连接）。
#      ⇒ 填**占位假值**即可（成例：`tools/verify_cprime_run.py` 的 `_DUMMY_ENV`）。
#
#   ★★★ 两条纪律：
#     ① **只在没设时才填**（`setdefault`）—— 绝**覆盖**真值：
#        容器内跑时这些是真的，覆盖它们会改变被测行为。
#     ② **值绝不打印**（同 `CRABPT_COOKIE`/`OPENCD_COOKIE` 的纪律）；
#        但**填了哪几个键**要印出来 ⇒ "我用的是假值"这件事**始终可见**。
_DUMMY_ENV = {
    "QB_PASS": "dummy", "QB_USER": "dummy", "QB_HOST": "dummy",
    "MUSOPIA_URL": "http://dummy.invalid", "MUSOPIA_PASSKEY": "dummy",
    "MUSOPIA_COOKIE": "dummy", "PICGO_API_KEY": "dummy",
    "OPENCD_CATEGORY": "opencd-verify-fixture",
}


def _fill_dummy_env():
    """填占位假值（**只填没设的**），返回**填了哪些键**（不返回值）。"""
    filled = [k for k, v in _DUMMY_ENV.items() if not os.environ.get(k)]
    for k in filled:
        os.environ[k] = _DUMMY_ENV[k]
    return filled


# ★ 必须在 `import db` **之前**执行（`config` 的检查在 import 期）。
_FILLED_ENV = _fill_dummy_env()
#   ★ 实测踩过：`python /app/tools/verify_cprime_step4.py` 时 `sys.path[0]`
#     是 **`/app/tools`**，不是 `/app` ⇒ `ModuleNotFoundError: No module named 'config'`。
#     而**本机跑**时因为 cwd = 仓库根、`sys.path` 里有 `''` ⇒ **恰好能用** ⇒
#     "本地绿、容器里崩"（`ERR-DOCKER-09` 的同族形状：**从"本机能跑"推不出"容器里能跑"**）。
#   ⇒ 显式插进去，两种环境都成立。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

CST = 8 * 3600

# ★★ 与 `nas_health.py` 用**同一个** SMB 路径 / 同一份 state 文件。
#   ★ 为什么不 import `nas_health`：那会把它的 `--check-drift` 等一堆逻辑拉进来，
#     且本工具有自己要印的判据。**共享的是"路径与口径"，不是代码。**
#     ★★ 但路径**不许**在这儿再写一份 —— 见下面 `_smb_root()`：
#       它**读 `nas_health.py` 的源码**取出 `DST` 字面量 ⇒ 改那边这里自动跟。
_THIS = pathlib.Path(__file__).resolve()
_REPO = _THIS.parent.parent


def _smb_root():
    """★ `nas_health.py` 里 `DST` 的**唯一真源**（Windows/SMB 视角的路径）。

    ★ 若两处各写一份 SMB 路径，改了 NAS 挂载点就会"一处改一处漏" ——
      而表现是"读不到库"（看着像 SMB 抖动，`ERR-SMB-02` 的形状）⇒ 难查。
    """
    src = (_THIS.parent / "nas_health.py").read_text(encoding="utf-8")
    m = re.search(r'^DST\s*=\s*pathlib\.Path\((.+?)\)\s*$', src, re.M)
    if not m:
        sys.exit("❌ 无法从 nas_health.py 里定位 DST 字面量 ⇒ 拒绝继续"
                 "（宁可报错也不在这里另写一份路径）")
    return pathlib.Path(eval(m.group(1)))                     # noqa: S307


def resolve_root(cli_root):
    """定出"库在哪" —— 返回 `(root, 来源说明)`。★ **来源必须可见，不靠推断。**

    ★★★ 为什么需要这层（2026-09-28 实测踩过）：
      `nas_health.py` 的 `DST` 是 **Windows/SMB 视角**的路径
      （`//iSunker-DS423/docker_ssd/musopia_script2`）。
      在 **NAS 宿主上**跑本工具时，那个路径**不存在** ⇒ 报
      「❌ 读不到库: //iSunker-DS423/…/data.db」，**看着像 SMB 抖动**（`ERR-SMB-02`），
      实际是**把 A 环境的常量拿到 B 环境用**（`ERR-META-01` 的形状）。
      NAS 宿主上的真路径是 `/volume2/docker_ssd/musopia_script2`。

    ★★ 为什么不"先试 DST、读不到再退到本地路径"：
      那就是**兜底值与真实结果混着报**（`ERR-META-14`）——
      "用了哪条路径"必须**一开始就明确**，不能靠"哪条碰巧读到了"。
      ⇒ 显式优先，且**永远把来源印出来**：

        ① 给了 `--root`          ⇒ 用它（来源：命令行）
        ② 否则 `MUSOPIA_DIR` 环境变量 ⇒ 用它（来源：环境变量）
        ③ 否则 `DST` 存在        ⇒ 用它（来源：nas_health.py 的 DST）
        ④ 否则                    ⇒ **报错退出**，不猜
      ★ 判据：**四档每一档都印出来源**，所以"它读的是哪份库"永远可见。
    """
    if cli_root:
        return pathlib.Path(cli_root), "--root（命令行显式指定）"
    env = os.environ.get("MUSOPIA_DIR", "")
    if env:
        return pathlib.Path(env), "MUSOPIA_DIR 环境变量"
    dst = _smb_root()
    if dst.exists():
        # ★ 标签**不许写死**（第一版写的是"（本机 SMB 视角）"—— 而它在 POSIX 上
        #   也会这么印，那就成了一句**不成立的话**：标签必须描述**实际读到的东西**，
        #   不是我以为的。⇒ 这里按**平台**生成，且把**探到的平台**印出来。）
        _plat = f"{sys.platform}"
        return dst, f"nas_health.py 的 DST（平台 {_plat}）"
    sys.exit(
        f"❌ 无法定出库的位置（**拒绝猜**）。\n"
        f"     `nas_health.py` 的 DST = {dst!r}，本机**读不到**。\n"
        f"     ★ 若你在 **NAS 宿主**上跑：加 `--root /volume2/docker_ssd/musopia_script2`\n"
        f"     ★ 若你在**容器**里跑：加 `--root /data`\n"
        f"     ★ 若你在**本机**跑：确认 NAS 的 SMB 共享开着（ERR-SMB-02）\n"
        f"   （本节原先没有这条分支 ⇒ 会拿一个不存在的路径去连，报成"
        f"「读不到库」而看不出是**路径口径**错。）")



def _ro_conn(db_path):
    """**只读**连接（`PRAGMA query_only=1`）。★ 绝不 cp、绝不写。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=1")
    return conn


def _state_scan_last_run(state_path):
    """读 `daemon_state.json` 里 `scan` 的 `last_run`（epoch）。读不到返回 `None`。

    ★ 为什么本工具要自己读它、而不是让人去看 `nas_health.py`：
      判"基线干不干净"必须知道 **scan 跑没跑**，这是本工具的**前提**，
      前提不能靠人肉从别处抄 —— 抄错了整个结论就废了（`ERR-META-01`）。
    """
    try:
        d = json.loads(pathlib.Path(state_path).read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return None
    for k in ("scan", "SCAN", "stage_scan"):
        v = d.get(k) if isinstance(d, dict) else None
        if isinstance(v, dict) and isinstance(v.get("last_run"), (int, float)):
            return float(v["last_run"])
    return None


def _fmt_epoch(ep):
    if ep is None:
        return "（读不到）"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ep)) + " CST"


def _snapshot(root):
    """取一批行 + 三个计数（全部走 `db.` 的唯一真源，不手写 SQL）。"""
    db_path = root / "data.db"
    state_path = root / "daemon_state.json"
    if not db_path.exists():
        sys.exit(f"❌ 读不到库: {db_path}（★ SMB 瞬时抖动先重跑一次，ERR-SMB-02）")
    # ★ 让 `db` 指到**只读**连接上：`db.connect()` 内部读 `config.DB_PATH`，
    #   而我们**不能**让它以读写方式打开生产库（那正是 ERR-SQL-01 要防的）。
    #   ⇒ 做法：把 `db.connect` 换成返回只读连接的 lambda。
    #   ★★ 这样做的代价：`db` 里所有函数都走只读连接 ⇒ 万一哪天有个函数会写，
    #      它会在 `query_only=1` 上**抛异常**而不是静默写坏 —— 那正是我要的。
    import config
    config.DB_PATH = str(db_path)
    import db
    _orig_connect = db.connect
    db.connect = lambda: _ro_conn(db_path)
    try:
        rows = db.queen_published_rows()
        exists_total, queen_total, without_tid = db.queen_published_gap()
    finally:
        db.connect = _orig_connect

    maxupd = None
    conn = _ro_conn(db_path)
    maxupd = conn.execute("SELECT max(updated_at) FROM tasks").fetchone()[0]
    conn.close()

    snap = {
        "taken_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "taken_at_epoch": int(time.time()),
        "scan_last_run": _state_scan_last_run(state_path),
        "max_updated_at_raw": maxupd,
        "exists_total": exists_total,
        "queen_total": queen_total,
        "exists_without_tid": without_tid,
        "titles": {r["info_hash"]: r["formatted_title"] for r in rows},
        "updated_at": {r["info_hash"]: r["updated_at"] for r in rows},
        "site_torrent_id": {r["info_hash"]: r["site_torrent_id"] for r in rows},
    }
    return snap, rows


def main():
    ap = argparse.ArgumentParser(description="C' 第 4 步只读验证（皇后路已上站行标题不许被动）")
    ap.add_argument("--baseline-out", default="",
                    help="把本次快照写到这个文件（拍基线用）")
    ap.add_argument("--baseline", default="",
                    help="与这个基线文件比对（判据在这一步才印）")
    # ★★★ 必须能显式指定库在哪 —— 见 `resolve_root()` 里那段（两种环境的路径口径不同）。
    ap.add_argument("--root", default="",
                    help="库所在目录（NAS 宿主: /volume2/docker_ssd/musopia_script2；"
                         "容器: /data）。不给则按 MUSOPIA_DIR 环境变量 → "
                         "nas_health.py 的 DST 依次取，且**来源会印出来**。")
    args = ap.parse_args()

    root, root_why = resolve_root(args.root)
    print("=" * 78)
    print("C' 第 4 步验证 —— **只读**（PRAGMA query_only=1；不 cp 生产库）")
    print("=" * 78)
    print(f"  库目录     : {root}")
    print(f"  路径来源   : {root_why}   ★ 来源必须可见，不靠推断（ERR-META-01）")
    print(f"  口径       : db.QUEEN_PUBLISHED_WHERE（唯一真源；本工具不手写 WHERE）")
    # ★ 环境凭据的来源**必须印出来**（不印值）：本工具不发请求，所以假值无害，
    #   但"我用的是假值"这件事不能让读者去猜（ERR-META-14：兜底值不许与真值混着报）。
    if _FILLED_ENV:
        print(f"  环境       : 填了 **{len(_FILLED_ENV)} 个占位假值**（本工具零请求，"
              f"值不打印、也不发出去）: {', '.join(sorted(_FILLED_ENV))}")
    else:
        print(f"  环境       : 需要的键**都已存在**（未填任何占位值）")

    snap, _rows = _snapshot(root)

    # ── ★★★ 早退闸门：判据模式（`--baseline`）下**先问到点没有，再决定要不要印表** ──
    #
    #   ★ 2026-09-28 实测教训：本工具原先**总是**把整张表印出来（时序自检 / EXISTS 断言 /
    #     取样行数……），只在**最后**才说 `❌ 否`。⇒ 那一大屏输出制造了
    #     「我跑了一条有意义的命令」的错觉 —— 实测**同一条命令被连跑 8 次**，
    #     每次都印满一屏、每次结论都是"没测"。
    #   ★★ 根因**不在**使用者：是工具**没有把"时机"当成前置条件**。
    #     "基线是否已过期到可以被比对" 是这份比对的**前提**，前提不成立时
    #     正确行为是**报前提、别印判据**（同 `_published_where` 那条：
    #      前提不满足时给出的不是答案，是噪音）。
    #
    #   ★ 判据用**基线里记的** `scan_last_run`（不是当前的）—— 要问的是
    #     "从拍这次基线到现在，scan 跑过一轮没有"。基线的 `last_run` 之后的
    #     一整轮若已完成，当前的 `last_run` 必然与之不同。
    #   ★ 只算**差值**、印**还差几分钟** ⇒ 使用者一眼知道"现在别跑"。
    if args.baseline and not args.baseline_out:
        try:
            _base0 = json.loads(pathlib.Path(args.baseline).read_text(encoding="utf-8"))
        except Exception:                                    # noqa: BLE001
            _base0 = None
        if isinstance(_base0, dict) and _base0.get("scan_last_run"):
            _bl = float(_base0["scan_last_run"])
            _elapsed = time.time() - _bl
            # ★ 一轮 = 周期 + 它自己的耗时（实测上一轮 298s ≈ 5 分钟）。
            #   阈值取 1980s ⇒ 若刚过 1800s 但 scan 还在跑，也**不当已到点**
            #   （"开跑" ≠ "跑完"，`last_run` 要跑完才写新值 —— 本工具原先的
            #    `age >= 1800` 提示正是漏了这一层，2026-09-28 因此白跑一轮）。
            _READY_AFTER = 1980.0
            if _elapsed < _READY_AFTER:
                _left = (_READY_AFTER - _elapsed) / 60.0
                print("=" * 78)
                print("⏳ **还没到点，本条不印判据**（判据：`scan` 跑完一整轮）")
                print("=" * 78)
                print(f"  基线采样时 scan.last_run : {_fmt_epoch(_bl)}")
                print(f"  现在                     : {_fmt_epoch(time.time())}")
                print(f"  距那一轮起跑             : {_elapsed / 60:.1f} 分钟")
                print(f"  ★ 一轮 = 周期 1800s + 它自己的耗时（实测 ~298s）"
                      f" ⇒ **{_READY_AFTER / 60:.0f} 分钟**后才算跑完")
                print(f"  ⇒ **还差约 {_left:.1f} 分钟**。现在跑必然报「没测」"
                      f"（`ERR-META-14`），所以**本工具拒绝印任何判据**。")
                print(f"\n  ★ 等约 {_left:.0f} 分钟后再跑**同一条命令**即可"
                      f"（基线不用重拍）。")
                print(f"\n★ 本工具**只读**：全程 `query_only=1`，没有 cp、没有写任何东西。")
                return 3
        # ★ 读不到基线的 `scan_last_run` ⇒ **不拦**（读数缺失是另一回事，
        #   下面比对段会把它报成 `❌ 否` 并给出处置）—— 拦它就成了"猜"。

    # ── 时序自检：基线到底干不干净 ──
    print("\n" + "─" * 78)
    print("★ 时序自检（判「基线干不干净」的前提）")
    print("─" * 78)
    print(f"  scan.last_run      : {_fmt_epoch(snap['scan_last_run'])}"
          f"  (epoch {snap['scan_last_run']})")
    print(f"  本次采样时刻       : {snap['taken_at']} CST")
    if snap["scan_last_run"]:
        age = time.time() - snap["scan_last_run"]
        print(f"  距 scan 上次起跑   : {age / 60:.1f} 分钟")
        print(f"  ★ 读法：scan 周期 1800s。若 `age < 1800` ⇒ 它**还没跑**"
              f"⇒ 这份快照是**干净基线**。")
        if age >= 1800:
            print("  ⚠️  `age >= 1800` ⇒ scan **可能已经跑过一轮** ⇒ "
                  "★ 若你**现在**才拍第一份基线，它**不干净**，"
                  "「没被动」这个结论**不成立**（不许静默当干净）。")
            # ★★ 2026-09-28 实测补：`age >= 1800` 只能说明"**该开跑了**"，
            #   说明不了"**跑完了**"。`last_run` 是**跑完**才写新值，
            #   而一轮要 周期 1800s + 耗时（实测 298s）。
            #   ⇒ 实测因此白跑一轮：在开跑前 2 分钟去比对，必然 `❌ 否`。
            #   ★ 这条提示必须把这个差值说出来，否则读者会拿"刚过 30 分钟"
            #     当成"到点了"（正是我犯的那个错）。
            print(f"     ★ 注意：`age` 是**距上一次起跑**，而一轮要"
                  f" **1800s + 它自己的耗时**（实测 ~298s ⇒ 约 35 分钟）。")
            print(f"     ⇒ **跑完**才是「到点」；刚过 30 分钟时它多半还在跑。")
    else:
        print("  ⚠️  读不到 scan.last_run ⇒ **判不了基线干不干净**"
              "（★ 不猜；请重跑一次，SMB 抖动见 ERR-SMB-02）")
    print(f"  max(updated_at)    : {snap['max_updated_at_raw']}  ← ★ **UTC**，+8h = CST")

    # ── ERR-DATA-08：`EXISTS` ⇒ 皇后路 是不是推理 ──
    print("\n" + "─" * 78)
    print("★ `EXISTS` ⇒ 皇后路 的**可执行**断言（ERR-DATA-08）")
    print("─" * 78)
    print(f"  site_status='EXISTS' 总数            : {snap['exists_total']}")
    print(f"  其中「皇后路」（QUEEN_PUBLISHED）    : {snap['queen_total']}")
    print(f"  ⇒ 差额（EXISTS 但 opencd_tid 空）    : {snap['exists_without_tid']}")
    if snap["exists_without_tid"]:
        print("  ⚠️  差额 ≠ 0 ⇒ 已上站的行里**混着抓轨路**。"
              "★ 那些行**本来就该被 scan 写** ⇒ 下面的判据**只对皇后路那批**成立，"
              "**不许**把两栏合并（ERR-META-05）。")
        print("     ★ 同时这也证实了：`EXISTS ⇒ 皇后路` **只是推理**，不是约束"
              "（db.py 里那句注释引用的 crabpt_rule_gaps() 至今不存在）。")
    else:
        print("  ✔ 差额 = 0 ⇒ 已上站的全是皇后路（这条推理**在本库当前状态下**成立）")

    print(f"\n  取样行数           : {len(snap['titles'])}")

    # ── 拍基线 ──
    if args.baseline_out:
        out = pathlib.Path(args.baseline_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"\n★ 基线已写: {out}")
        print("  ★ 下一步：等一轮 scan 跑完（看 nas_health.py 的 scan.last_run），"
              "再用 --baseline 指回这个文件。")

    # ── 比对（判据只在这一步印）──
    if args.baseline:
        base = json.loads(pathlib.Path(args.baseline).read_text(encoding="utf-8"))
        print("\n" + "=" * 78)
        print(f"★ 判据 —— 与基线比对（{args.baseline}）")
        print("=" * 78)
        print(f"  基线采样: {base['taken_at']}   scan.last_run(基线) = "
              f"{_fmt_epoch(base['scan_last_run'])}")
        print(f"  本次采样: {snap['taken_at']}   scan.last_run(本次) = "
              f"{_fmt_epoch(snap['scan_last_run'])}")
        progressed = (base.get("scan_last_run") != snap.get("scan_last_run"))
        print(f"  scan 推进过吗: {'✅ 是' if progressed else '❌ 否'}")
        # ★★★ 2026-09-28 实测修：原先"没推进"只是**多印一句 ⚠️**，
        #   然后**继续往下印**「✅ 28 条标题全部没被动 ⇒ C' 生效」——
        #   于是两条**并列**出现，读者自然读成"✅ 生效"。
        #   ★ 而那是**错的**：scan 没跑 ⇒ 这两次采样之间**根本没有 scan 写过库**
        #     ⇒ "标题没被动"是**必然**的，不构成 C' 生效的任何证据。
        #   ★★ 这是 `ERR-META-14` 的形状（"没测"被读成"通过"），
        #     而且**是本工具自己犯的**：我加了提示，却没**让后续判据闭嘴**。
        #   ⇒ 修法：没推进 ⇒ **印一条红、立刻 return 2**，后面一条判据都不印。
        #     判据：**"没测"与"通过"不许同时出现**。
        if not progressed:
            print("  ❌★ scan **没有推进** ⇒ 本次比对**一条判据都不成立**"
                  "（不是「通过」，是「**没测**」）—— `ERR-META-14` 的形状。")
            print("     ★ 下面**故意什么都不印** —— 上一次这里接着印了"
                  "「✅ 标题全部没被动 ⇒ C' 生效」，"
                  "把「没测」误导成「通过」。")
            print("     ★ 正确做法：等 `scan.last_run` 往前推（周期 1800s）**之后**"
                  "再跑这条命令。")
            print("\n★ 本工具**只读**：全程 `query_only=1`，没有 cp、没有写任何东西。")
            return 2

        changed = []
        for h, old in base["titles"].items():
            if h not in snap["titles"]:
                continue
            new = snap["titles"][h]
            if new != old:
                changed.append((h, old, new,
                                base["site_torrent_id"].get(h),
                                base["updated_at"].get(h),
                                snap["updated_at"].get(h)))

        missing = [h for h in base["titles"] if h not in snap["titles"]]
        _verdict_bad = bool(changed) or bool(missing)
        # ★★★ 2026-09-28 实测修：原先判据印了红、`main()` 却仍以 `return 0` 收尾
        #   ⇒ **"C' 没生效"退出码是 0**。任何 `&&` 链 / CI / 包装脚本读它
        #   都会把**失败读成成功** —— 这是 `ERR-META-14` 里最贵的一种：
        #   判据本身是对的，**它对外说的话是错的**。
        #   ⇒ 判据：**凡是印了 ❌ 的路径，退出码必须 ≠ 0**。
        #     （与上面"没推进 ⇒ return 2"同一条纪律：让机器读到的与眼睛看到的一致。）
        if missing:
            print(f"\n  ❌ 基线里有、现在取不到的行（{len(missing)} 条）⇒ "
                  f"`site_status`/`site_torrent_id`/`opencd_tid` **变了**，"
                  f"这不是「没被动」：{missing}")
        if changed:
            print(f"\n  ❌★ 皇后路已上站行里，有 **{len(changed)} 条**标题**变了**"
                  f" ⇒ **C' 没生效**（逐条如下）：")
            for h, old, new, sid, u0, u1 in changed:
                print(f"     · {h}  (site id {sid})")
                print(f"         旧: {old!r}")
                print(f"         新: {new!r}")
                print(f"         updated_at: {u0} → {u1}")
        else:
            print(f"\n  ✅ 皇后路已上站行 **{len(base['titles'])} 条标题全部没被动**"
                  f" ⇒ **C' 生效**。")
            print("     ★★ 读法提醒：这只是「**没被动**」，**不是**「变得对了」——"
                  "改对是第 5 步的活。")
        # ★ 旁证单列：`updated_at` 动没动（**不**做判据，只做旁证）
        upd_moved = [h for h in base["updated_at"]
                     if h in snap["updated_at"]
                     and base["updated_at"][h] != snap["updated_at"][h]]
        print(f"\n  （旁证）这批行里 `updated_at` 动过的: {len(upd_moved)} 条")
        print("     ★ `scan` 写**别的列**也会刷 `updated_at` ⇒ 它**不是**标题的判据"
              "（ERR-META-05：量纲不同不许混用）。")

    if not args.baseline_out and not args.baseline:
        print("\n★ 你既没给 --baseline-out 也没给 --baseline ⇒ 上面只是**一张快照**，"
              "没有任何判据被印出来。")

    print("\n★ 本工具**只读**：全程 `query_only=1`，没有 cp、没有写任何东西。")
    # ★ 退出码：印了 ❌ 就**不许**是 0（见上面 `_verdict_bad` 那段）。
    return 1 if _verdict_bad else 0


if __name__ == "__main__":
    sys.exit(main())
