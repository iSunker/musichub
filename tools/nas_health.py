"""NAS musopia daemon 健康巡检（只读）。

★ 纪律（本脚本的存在就是为了防 2026-09-19 那天犯过两次的错）：
  - 判据一律**精确计算**，绝不心算 —— 当天把 20.6h 当成「超过 24h」、
    把构建上下文的增量字节当成「文件没传过去」，都是 ERR-META-01 一族。
  - 判据是「**同一份 state 文件里该动的有没有动**」，
    不是「日志里有没有」、不是「我觉得该跑了」。
  - ★ 不 cp 生产库（ERR-SQL-01）：SMB 只读直连，PRAGMA query_only=1。

用法（本机，不需要连容器）:
    python tools/nas_health.py            # 正常巡检
    python tools/nas_health.py --reset    # 清掉上次快照（只报一次全量）
"""
import argparse
import json
import os
import pathlib
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

DST = pathlib.Path("//iSunker-DS423/docker_ssd/musopia_script2")
SNAP = pathlib.Path(__file__).with_name(".nas_health_last.json")

# 时间口径：state 里的 last_run 是 epoch（UTC 无歧义）；
# 但 last_finish 字符串与 data.db 的 updated_at 是**两个不同口径**（ERR-SQL-03）：
#   last_finish → 本地 CST；data.db.updated_at → UTC。
CST = 8 * 3600

# ★★ 必须与 `daemon.build_stages()` 的 6 个阶段**一一对应**（2026-09-22 补齐 crawl）。
#    这里**曾经漏了 crawl**，后果是它从不进入下面的检查循环 —— 一个被门控关掉的阶段
#    看起来"正常"，因为你根本没查它。而"没查"和"查了没事"在报告里长得一模一样
#    （ERR-META-01 一族：检测方法失效比不检测更危险）。
#    ★ 判据来源是**源码**（`daemon.build_stages()` 里 6 个 `env_bool("ENABLE_*")`），
#      不是 startup banner —— banner 只列**门控后**进来的阶段，两个 false 就被它吃掉，
#      拿它当"阶段列表"的判据**已经错过一次**（publish 和 crawl 当时都"不在"）。
INTERVALS = {"scan": 1800, "publish": 1800, "sync": 86400,
             "covers": 21600, "edit": 21600, "crawl": 1800}
EDIT_COOLDOWN = 60
HEARTBEAT_FRESH = 120        # 秒；超过就认为 daemon 不活着

# ★★ 哪些阶段"关着"（last_run 长期不动是**预期内**的）。
#
# ⚠️ 这里曾经硬编码 `{"publish"}` 并据此打印「已禁用（预期不动）」—— **那是错的**，
#    而且是个危险的错：它把"publish 66 小时没动"这种**真异常**说成了"预期"。
#    （2026-09-22 巡检实测：publish.last_run 停在 66h 前，而它本该每 30 分钟一跑。）
#
# ★ 为什么本机**不能**静态知道生产开着什么：真正决定的是 NAS 上那份 `.env`
#   （`ENABLE_*` 开关），而 `.env` 在 deploy.sh 的 NEVER 名单里、本机读不到（ERR-ENV-02）。
#   `.env.example` 只是**模板**，两者可以不一致（实测模板 ENABLE_PUBLISH=true 而生产 false）。
#
# ★★ 2026-09-22 解法：让**容器自己**把生效的开关落成 `/data/daemon_env.json`
#    （见 `daemon.save_env_snapshot()`；`/data` 是 bind mount，本机 SMB 看得到）。
#    ⇒ 本脚本现在**读那份快照**来判"哪些阶段关着"，**不再猜、也不再要人手传**。
#    优先级：`--disabled` 显式声明 > 快照文件 > 空集（空集 = 每个阶段都按"该动就得动"判，
#    宁可多报一个待查，不可把真异常说成"预期不动"—— 漏报比误报危险得多）。
DISABLED_STAGES: set = set()
ENV_SNAPSHOT = "daemon_env.json"
# 快照超过这么久没刷新 ⇒ **不作为判据**（一份停在上个月的快照是 ERR-META-04 说的"传说"）
#   阈值取 1 小时：daemon 每轮都会重写它（tick 默认 30s），1 小时没动基本就是 daemon 停了。
SNAPSHOT_MAX_AGE = 3600
# 快照里 `ENABLE_<阶段>` 与阶段名的对应（**必须显式列出**，不靠猜大小写）
STAGE_ENABLE_KEY = {"scan": "ENABLE_SCAN", "publish": "ENABLE_PUBLISH",
                    "sync": "ENABLE_SYNC", "covers": "ENABLE_COVERS",
                    "edit": "ENABLE_EDIT", "crawl": "ENABLE_CRAWL"}


def read_env_snapshot(dst=None):
    """读容器落下的开关快照，返回 (values: dict, age_s: float, note: str)。

    ★ 判"真假"的口径**照抄 `daemon.env_bool`**（`strip().lower() in ("1","true","yes","on")`），
      只有一处实现错误的地方就全错，所以这里宁可多写一行也不"差不多就行"。
    ★ 任何一步失败都**返回空 dict + 原因**，绝不返回一个"看着正常的默认值" ——
      默认值会被下游当成事实用（ERR-META-01 一族）。
    """
    p = (dst or DST) / ENV_SNAPSHOT
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None, "快照不存在（容器还没跑过新版 daemon，或 /data 挂点不对）"
    except Exception as e:                                    # noqa: BLE001
        return {}, None, f"快照读取失败：{type(e).__name__}: {e}"

    age = time.time() - p.stat().st_mtime
    vals = {}
    for k, v in (raw.get("values") or {}).items():
        vals[k] = str(v).strip().lower() in ("1", "true", "yes", "on")
    if age > SNAPSHOT_MAX_AGE:
        return {}, age, (f"快照已 {age / 3600:.1f}h 未刷新（阈值 "
                         f"{SNAPSHOT_MAX_AGE / 3600:.1f}h）⇒ 不拿它当判据")
    return vals, age, ""


def cst(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch + CST))


def fmt_age(sec):
    """把秒数写成人类可读，但**同时保留精确秒数** —— 不心算。"""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m or h:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return f"{' '.join(parts)}  ({sec}s)"


def resolve_dst():
    """NAS 部署目录。优先级与 deploy.sh 一致（见 ERR-DEPLOY-01）：
    外部环境变量 DST → 同目录 .deploy.nasrc 里的 DST= → 硬编码兜底。
    ★ .deploy.nasrc 的定位是**默认值**，不是强制值。"""
    v = os.environ.get("DST")
    if v:
        return pathlib.Path(v)
    nrc = pathlib.Path(__file__).parent.parent / ".deploy.nasrc"
    if nrc.exists():
        for ln in nrc.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith("DST="):
                return pathlib.Path(ln[4:].strip())
    return DST


def read_state(dst=None):
    return json.loads(((dst or DST) / "daemon_state.json").read_text(encoding="utf-8"))


def read_heartbeat(dst=None):
    """返回 (内容里的 epoch, 文件 mtime 的 epoch)。两个都看 —— 单一采样不足以判活。"""
    p = (dst or DST) / "daemon_heartbeat"
    raw = p.read_text(encoding="utf-8").strip()
    content = int(raw) if raw.isdigit() else None
    return content, p.stat().st_mtime


def read_db(dst=None):
    con = sqlite3.connect(str((dst or DST) / "data.db"))
    con.execute("PRAGMA query_only=1")
    cur = con.cursor()

    # ★ stale 的口径与 db.py 的 STALE_CONDITION 严格一致：
    #   site_status='EXISTS' AND (site_synced_at IS NULL OR updated_at > site_synced_at)
    stale = cur.execute(
        "SELECT count(*) FROM tasks WHERE site_status='EXISTS' "
        "AND (site_synced_at IS NULL OR updated_at > site_synced_at)"
    ).fetchone()[0]
    exists = cur.execute(
        "SELECT count(*) FROM tasks WHERE site_status='EXISTS'"
    ).fetchone()[0]
    total = cur.execute("SELECT count(*) FROM tasks").fetchone()[0]
    mx = cur.execute("SELECT max(updated_at) FROM tasks").fetchone()[0]
    con.close()
    return {"stale": stale, "exists": exists, "total": total, "max_updated_at": mx}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--dst", default=None,
                    help="NAS 部署目录（默认按 DST 环境变量 → .deploy.nasrc → 硬编码）")
    ap.add_argument("--disabled", default="",
                    help="逗号分隔：**显式**声明哪些阶段确实关着（它们的 last_run 不动算预期）。"
                         "不给就读 NAS 上的 daemon_env.json 快照；快照也读不到则按空集判"
                         "（= 每个阶段都按'该动就得动'，宁可多报一个待查）")
    args = ap.parse_args()

    dst = pathlib.Path(args.dst) if args.dst else resolve_dst()
    # ★ disabled 的来源分层：显式声明 > 容器快照 > 空集。**每一层都打印来源**，
    #   免得"这次为什么没报 publish"变成一个要翻代码才知道的事。
    snap_vals, snap_age, snap_note = read_env_snapshot(dst)
    if args.disabled:
        disabled = {s.strip() for s in args.disabled.split(",") if s.strip()}
        missing = set()
        disabled_src = "命令行 --disabled"
    elif snap_vals:
        # ★★ 三态，**不是**两态（2026-09-22 修）：`True`=开着 / `False`=明确关着
        #    / **`None`=快照里没有这个键 ⇒ 「未声明」，不是「没关」**。
        #
        #    原先写成 `if snap_vals.get(key) is False` ⇒ 键不存在时是 `None`，
        #    `None is False` 为假 ⇒ 那个阶段被当成"开着"。**这是错的**：
        #    `ENABLE_CRAWL` 默认 False 且生产 `.env` 里压根没这一行（ERR-ENV-02）
        #    ⇒ 它不在 `os.environ` ⇒ 快照（刻意按白名单 `if k in os.environ` 写，
        #    见 `daemon.save_env_snapshot`）**正确地略过它** ⇒ 下游拿到的"缺位"
        #    被 `is False` 吞成了"没关"。
        #    ★ 后果不是"少报一个"而是"报一个假的"：crawl 其实没在跑，却被当开着
        #      ⇒ 补上 INTERVALS 里的 crawl 之后，它会每轮报「★ 超过周期」。
        #    ⇒ 缺位必须如实传播：**未知的一律按"该动就得动"判**（宁可多报一个待查）。
        disabled = {st for st, key in STAGE_ENABLE_KEY.items()
                    if snap_vals.get(key) is False}
        missing = {st for st, key in STAGE_ENABLE_KEY.items()
                   if snap_vals.get(key) is None}
        disabled_src = (f"容器快照 {ENV_SNAPSHOT}（{snap_age:.0f}s 前；"
                        f"ENABLE_PUBLISH="
                        f"{snap_vals.get('ENABLE_PUBLISH')}）")
    else:
        disabled = set()
        missing = set(STAGE_ENABLE_KEY)
        disabled_src = f"**空集**（{snap_note or '无快照'}）⇒ 每个阶段都按'该动就得动'判"
    if missing:
        # ★ 把"快照里没有这个键"显式报出来 —— 它**不是**一个可以静默忽略的细节：
        #   要么是那个阶段真没配（如 crawl 的生产 `.env`），要么是快照机制/键名漂了。
        disabled_src += (f"  ⚠ 快照里没有这些键 ⇒ 按'该动就得动'判（未声明≠关着）："
                         f"{'、'.join(sorted(missing))}")

    now = time.time()
    if args.reset and SNAP.exists():
        SNAP.unlink()

    state = read_state(dst)
    hb_content, hb_mtime = read_heartbeat(dst)
    db = read_db(dst)

    # ---------- 精确计算 ----------
    rows = []
    for stage, iv in INTERVALS.items():
        st = state.get(stage)
        if not st or st.get("last_run") is None:
            rows.append((stage, None, iv, None, "❔ state 里没有这个阶段"))
            continue
        age = now - st["last_run"]
        if stage in disabled:
            # ★ 这里说"**据快照/显式声明**关闭"，不说"已禁用" —— 因为它的依据是
            #   容器自己落的快照，而快照可能是过期的（过期时 read_env_snapshot
            #   会返回空 dict，根本走不到这个分支）。措辞带上来源，读的人才知道
            #   这条判定的可信度。
            verdict = "⏹️ 关着（据上方来源；last_run 不动是预期）"
        elif age <= iv:
            verdict = "✅ 在周期内"
        else:
            verdict = f"★ 超过周期 {fmt_age(age - iv)}"
        # ★ 快照里**没有这个键** ⇒ 未声明。此时 `age > iv` 是**真异常**
        #   （不是"它关着所以不动"），但也要标注依据弱 —— 我们只知道"没声明"，
        #   不知道它到底是关的还是漏配了。别让读的人把"未声明"当成"已确认开着"。
        if stage in missing and age > iv:
            verdict += " 「未声明」⇒ 该动就得动，此超期为真异常（快照里无此键）"
        # edit 的例外：有活时按 cooldown 连跑
        if stage == "edit" and st.get("no_progress"):
            verdict += " · no_progress=true（待办清不掉，退回长周期，正常）"
        rows.append((stage, age, iv, st, verdict))

    hb_age = now - max(hb_mtime, hb_content or 0)
    alive = hb_age < HEARTBEAT_FRESH

    # ---------- 与上次比对：只报变化 ----------
    cur_snap = {
        "heartbeat_alive": alive,
        "state_last_run": {k: (state.get(k) or {}).get("last_run")
                           for k in INTERVALS},
        "db": db,
    }
    prev = json.loads(SNAP.read_text(encoding="utf-8")) if SNAP.exists() else None

    changes = []
    if prev is None:
        changes.append("（首次运行，无基线可比 —— 下面是全量）")
    else:
        if prev.get("heartbeat_alive") != alive:
            changes.append(f"心跳存活状态翻转：{prev.get('heartbeat_alive')} → {alive}")
        for k in INTERVALS:
            a = (prev.get("state_last_run") or {}).get(k)
            b = cur_snap["state_last_run"][k]
            if a != b:
                changes.append(f"{k}.last_run 推进了：{cst(a) if a else '(无)'} → "
                               f"{cst(b) if b else '(无)'}")
        for k in ("stale", "exists", "total"):
            if (prev.get("db") or {}).get(k) != db[k]:
                changes.append(f"库 {k}：{prev['db'].get(k)} → {db[k]}")

    # ---------- 输出 ----------
    print("=" * 72)
    print(f"巡检时刻   {cst(now)} CST   (epoch {now:.0f})")
    print("=" * 72)

    print("\n【心跳】daemon_heartbeat")
    print(f"  内容 epoch   {hb_content}  → {cst(hb_content) if hb_content else '(非数字)'}")
    print(f"  文件 mtime   {cst(hb_mtime)}")
    print(f"  年龄         {fmt_age(hb_age)}   (阈值 {HEARTBEAT_FRESH}s)")
    print(f"  ⇒ {'✅ daemon 活着' if alive else '★★ daemon 可能已停/卡住'}")

    print("\n【各阶段】state 里该动的有没有动")
    print(f"  「关着」的判据来源：{disabled_src}")
    print(f"  {'阶段':<9}{'间隔':>8}  {'last_run (CST)':<21}{'年龄':<26}判定")
    for stage, age, iv, st, verdict in rows:
        ivs = f"{iv}s" if iv < 3600 else f"{iv // 3600}h"
        lr = cst(st["last_run"]) if st and st.get("last_run") else "-"
        ages = fmt_age(age) if age is not None else "-"
        print(f"  {stage:<9}{ivs:>8}  {lr:<21}{ages:<26}{verdict}")

    print("\n【库】SMB 只读直连 (PRAGMA query_only=1)")
    print(f"  总记录        {db['total']}")
    print(f"  site EXISTS   {db['exists']}")
    print(f"  stale 待办    {db['stale']}   (口径 = db.py 的 STALE_CONDITION)")
    print(f"  max(updated_at)  {db['max_updated_at']}  ← ★ 这是 **UTC**，加 8h = "
          f"{cst(_utc_to_epoch(db['max_updated_at'])) if db['max_updated_at'] else '-'} CST")

    print("\n【与上次相比的变化】")
    if changes:
        for c in changes:
            print(f"  • {c}")
    else:
        print("  无变化")

    SNAP.write_text(json.dumps(cur_snap, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _utc_to_epoch(s):
    """把 data.db 里的 'YYYY-MM-DD HH:MM:SS'(UTC) 转 epoch。"""
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S")) - time.timezone
    except Exception:
        return None


if __name__ == "__main__":
    main()
