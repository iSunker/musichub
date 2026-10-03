"""★ 只读探针：在**这台 qB 上**实地核对 `qbtune.py` 赖以工作的 API 面。

═══════════════════════════════════════════════════════════════════════════
它要回答的四个问题（都要**实测**，不许照抄文档）
═══════════════════════════════════════════════════════════════════════════
`tools/qbtune.py` 是 qB 下载调度器，靠四个调用干活：

    qb.torrents_info(...)              读
    qb.torrents_top_priority(...)      写（升队列）
    qb.torrents_bottom_priority(...)   写（降队列）
    qb.torrents_set_download_limit(...) 写（每种子下载限速）

这些**在 `qbittorrent-api==2024.9.67` 的源码里都有**（已从包源码核实），
但那**不等于这台 qB 运行期真的有** —— `ERR-META-01` 家族：**"文档里写着" ≠ "现场成立"**。
本脚本就是在**现场**把它们逐条确认，缺一个就报出来，绝不假设。

★ 四个必须现场落定的点（写 `qbtune.py` 前**必须**先有答案）：
  ① **qB / WebAPI 版本** —— 若哪天变了，之前"已核实"的字段假设**全部作废**。
  ② **四个方法是否存在** —— `getattr` 取，打印 ✅/❌，**不抛** AttributeError。
  ③ **`queueing_enabled` / `max_active_downloads`** —— top/bottom priority **要求队列开着**，
     而 qbtune 的整个"选哪 10 个"机制**依赖**它。关着 ⇒ qbtune 必须**拒绝运行**。
  ④ **字段存在性** —— 逐字段 `getattr` 一条候选，缺哪些照实印。
     ★ `dlspeed / num_seeds / num_complete / amount_left / priority / download_limit`
       —— 打分与选集全押在这几个上，缺一个分数就是错的（`ERR-META-01`）。

★ 还有一件它**顺手量化**的事：**下载集里到底几个有源**。
  2026-10-03 实测是 **397 个里只有 5 个 `num_seeds>0`** —— 这才是"带宽拉不满"的**真瓶颈**
  （不是队列顺序）。本脚本每次都重新数一遍，**不缓存、不手抄**（`ERR-META-03`）。

═══════════════════════════════════════════════════════════════════════════
只读保证
═══════════════════════════════════════════════════════════════════════════
只调**查询**类 API：`torrents_info()` / `app_preferences()` / `app_version()` /
`app_web_api_version()` / `app_build_info()`。
**不写任何东西** —— 不 top/bottom、不限速、不加/删/停种子、不碰 `data.db`。
`--check-api` 与 `--active` 都是只读。

用法（在 NAS 上，一行）:
    sudo docker compose run --rm tools python tools/qbtune_probe.py --check-api
    sudo docker compose run --rm tools python tools/qbtune_probe.py --active
"""
import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.environ.get("MUSOPIA_APP_DIR", "/app"))

import config  # noqa: E402

# ★ 打分与选集**押注**的字段 —— 缺一个，`qbtune.py` 的分数就是错的。
#   逐个 `getattr` 核，**不假设**（ERR-META-01：文档有 ≠ 现场有）。
REQUIRED_FIELDS = (
    "hash", "name", "state", "category",
    "dlspeed", "upspeed", "progress",
    "num_seeds", "num_leechs", "num_complete", "num_incomplete",
    "amount_left", "size", "eta",
    "priority", "force_start",
    "upload_limit", "download_limit",
)

# ★ qbtune 要写的三个方法 —— 缺任何一个都必须在**开写之前**发现。
REQUIRED_METHODS = (
    "torrents_info",
    "torrents_top_priority",
    "torrents_bottom_priority",
    "torrents_set_download_limit",
)

# 下载相关 state 的口径（与 `qbtune.py` 的分池保持一致）。
DL_STATES = ("downloading", "forcedDL", "stalledDL", "queuedDL", "metaDL")


def connect_qb():
    """建一个已登录连接。连不上返回 `(None, 原因)` —— 不抛。"""
    try:
        from qbittorrentapi import Client
    except ImportError as e:
        return None, f"没装 qbittorrentapi（{e}）"
    try:
        qb = Client(host=config.QB_HOST, username=config.QB_USER,
                    password=config.QB_PASS)
        qb.auth_log_in()
        return qb, None
    except Exception as e:                                   # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def cmd_check_api(qb):
    print("=" * 74)
    print("★ 现场核对 qbtune 依赖的 API 面（只读）")
    print("=" * 74)
    print(f"  QB_HOST = {config.QB_HOST}")
    print()

    # ① 版本
    print("【① 版本】★ 若这里变了，之前所有字段假设作废")
    try:
        print(f"  qB app      = {qb.app_version()}")
        print(f"  qB WebAPI   = {qb.app_web_api_version()}")
        bi = qb.app_build_info()
        print(f"  libtorrent  = {bi.get('libtorrent')}   qt={bi.get('qt')}")
    except Exception as e:                                   # noqa: BLE001
        print(f"  ⚠️ 取版本失败：{type(e).__name__}: {e}")
    print()

    # ② 方法存在性（getattr，不抛）
    print("【② 方法存在性】★ 缺任何一个 ⇒ qbtune 无法工作")
    missing = []
    for m in REQUIRED_METHODS:
        ok = callable(getattr(qb, m, None))
        print(f"  {'✅' if ok else '❌'} {m}")
        if not ok:
            missing.append(m)
    print()

    # ③ 队列开关（top/bottom priority 的前提）
    print("【③ 队列开关】★ top/bottom priority 要求 queueing_enabled=true")
    try:
        p = qb.app_preferences()
        q = p.get("queueing_enabled")
        mad = p.get("max_active_downloads")
        mau = p.get("max_active_uploads")
        mat = p.get("max_active_torrents")
        print(f"  queueing_enabled       = {q}  {'✅' if q else '❌ 必须为 true'}")
        print(f"  max_active_downloads   = {mad}  (qbtune 的 cap 必须 ≤ 这个)")
        print(f"  max_active_uploads     = {mau}")
        print(f"  max_active_torrents    = {mat}")
        print(f"  up_limit / dl_limit    = {p.get('up_limit')} / {p.get('dl_limit')}")
    except Exception as e:                                   # noqa: BLE001
        print(f"  ⚠️ 取 prefs 失败：{type(e).__name__}: {e}")
    print()

    # ④ 字段存在性（拿一条已知种子核）
    print("【④ 字段存在性】★ 打分/选集全押在这几个上")
    try:
        ts = list(qb.torrents_info())
        if not ts:
            print("  ⚠️ qB 里一个种子都没有？无法核对字段。")
        else:
            t = ts[0]
            missing_f = []
            for f in REQUIRED_FIELDS:
                v = getattr(t, f, "<MISSING>")
                mark = "✅" if v != "<MISSING>" else "❌"
                if v == "<MISSING>":
                    missing_f.append(f)
                # 值可能很长，截断打印
                sv = repr(v)
                if len(sv) > 40:
                    sv = sv[:37] + "..."
                print(f"  {mark} {f:16} = {sv}")
            print()
            print(f"  缺字段：{missing_f if missing_f else '（无）✅'}")
    except Exception as e:                                   # noqa: BLE001
        print(f"  ⚠️ 取 torrents_info 失败：{type(e).__name__}: {e}")
    print()

    # 顺手量化：下载集里几个有源（不缓存、不手抄）
    try:
        ts = list(qb.torrents_info())
        dl = [t for t in ts if t.state in DL_STATES]
        live = [t for t in dl if (t.num_seeds or 0) > 0]
        print("【⑤ 下载集 swarm 现状】★ 这才是『拉不满带宽』的真瓶颈")
        print(f"  下载相关种子总数 = {len(dl)}")
        print(f"    num_seeds>0（能下）= {len(live)}")
        print(f"    num_seeds=0（不动）= {len(dl) - len(live)}")
        tot = sum((t.dlspeed or 0) for t in ts)
        print(f"  当前总下载速度 = {tot / 1e6:.3f} MB/s")
    except Exception as e:                                   # noqa: BLE001
        print(f"  ⚠️ 数下载集失败：{type(e).__name__}: {e}")
    print()

    print("=" * 74)
    if missing:
        print(f"❌ 缺方法：{missing} —— qbtune 会拒绝运行（return 3）。")
        return 3
    print("✅ 四个方法都在；继续按上面的字段/开关核对，再决定是否开启 qbtune。")
    print("★ 本脚本没有修改任何东西（qB 只查询；本地库完全没碰）。")
    return 0


def cmd_active(qb):
    """打出 qB 当前认为的 active 下载集（回读用；ERR-META-08）。"""
    print("=" * 74)
    print("★ 当前 qB 认为的 active 下载集（只读回读）")
    print("=" * 74)
    ts = list(qb.torrents_info())
    dl = [t for t in ts if t.state in DL_STATES]
    active = [t for t in dl if t.state in ("downloading", "forcedDL")]
    print(f"  下载相关总数 = {len(dl)}")
    print(f"  active（downloading/forcedDL）= {len(active)}")
    print()
    print("  逐个 active（按 dlspeed 降序）：")
    for t in sorted(active, key=lambda x: -(x.dlspeed or 0)):
        print(f"    dl={t.dlspeed / 1024:9.1f}KB/s seeds={t.num_seeds} "
              f"prio={t.priority} prog={t.progress * 100:5.1f}% "
              f"(t.name or '')[:40]")
    from collections import Counter
    c = Counter(t.state for t in dl)
    print()
    print(f"  下载集 state 分布：{dict(c)}")
    print()
    print("★ 本脚本没有修改任何东西（qB 只查询；本地库完全没碰）。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="qbtune 的只读 API 探针")
    ap.add_argument("--active", action="store_true",
                    help="只打当前 active 下载集（回读用）；默认走 --check-api")
    args = ap.parse_args(argv)

    qb, reason = connect_qb()
    if qb is None:
        print(f"✗ 连不上 qB（{config.QB_HOST}）：{reason}", file=sys.stderr)
        print("  ★ QB_HOST 是 127.0.0.1 ⇒ 只有**在容器内 / NAS 本机**才连得上。",
              file=sys.stderr)
        return 2

    if args.active:
        return cmd_active(qb)
    return cmd_check_api(qb)


if __name__ == "__main__":
    sys.exit(main())
