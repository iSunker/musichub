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

INTERVALS = {"scan": 1800, "publish": 1800, "sync": 86400,
             "covers": 21600, "edit": 21600}
EDIT_COOLDOWN = 60
HEARTBEAT_FRESH = 120        # 秒；超过就认为 daemon 不活着
DISABLED_STAGES = {"publish"}  # ENABLE_PUBLISH=false：last_run 不动是预期内的


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


def read_state():
    return json.loads((DST / "daemon_state.json").read_text(encoding="utf-8"))


def read_heartbeat():
    """返回 (内容里的 epoch, 文件 mtime 的 epoch)。两个都看 —— 单一采样不足以判活。"""
    p = DST / "daemon_heartbeat"
    raw = p.read_text(encoding="utf-8").strip()
    content = int(raw) if raw.isdigit() else None
    return content, p.stat().st_mtime


def read_db():
    con = sqlite3.connect(str(DST / "data.db"))
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
    args = ap.parse_args()

    now = time.time()
    if args.reset and SNAP.exists():
        SNAP.unlink()

    state = read_state()
    hb_content, hb_mtime = read_heartbeat()
    db = read_db()

    # ---------- 精确计算 ----------
    rows = []
    for stage, iv in INTERVALS.items():
        st = state.get(stage)
        if not st or st.get("last_run") is None:
            rows.append((stage, None, iv, None, "❔ state 里没有这个阶段"))
            continue
        age = now - st["last_run"]
        if stage in DISABLED_STAGES:
            verdict = "⏹️ 已禁用（预期不动）"
        elif age <= iv:
            verdict = "✅ 在周期内"
        else:
            verdict = f"★ 超过周期 {fmt_age(age - iv)}"
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
