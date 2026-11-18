# -*- coding: utf-8 -*-
"""皇后原种回收（GC 阶段 1）：把「HR 已满足 且 无转发价值 / 外站已承接」的原种从 music3 清掉。

═══════════════════════════════════════════════════════════════════════════
两阶段渐进式清理：阶段 1（music3 减负）
═══════════════════════════════════════════════════════════════════════════
OpenCD 抓轨原种在 `qbit-music3`(3260) 做种，落地 `/volume1/video/download/music/music3`。
本模块负责阶段 1：
  1. 保护未满 36h HR 考核的种子（零风险防 H&R）；
  2. 保护待转发且独占 music3 的健康新种（保留用于后续发种）；
  3. 清理已在别站做种的原种（R1：外站承接，删 music3 链接不影响外站做种）；
  4. 清理禁转/失败/死库存（R2/R3：独占原种直接释放物理空间）。

═══════════════════════════════════════════════════════════════════════════
三条清理规则（必须先过「HR 36h 保护」）
═══════════════════════════════════════════════════════════════════════════
  R1 已承接：`site_publish` 至少 1 个站 `status='EXISTS'`，或外站常规做种库（music1/2）已有同名副本
             ⇒ 外站已承接做种，底层硬链接共享，删 music3 任务和链接不影响外站做种。
  R2 禁转/坏种：`transfer_rule='DENY'` 或 `status IN ('FAILED', 'NEED_REVIEW')`
             ⇒ 页面规则禁止转载或不可逆失败，留着无发种价值，直接删任务与物理文件。
  R3 死库存：`transfer_rule` 空 + 无 `opencd_tid` + `mapped_json` 空 + 做种 > 30 天
             ⇒ 无 tid 无法抓取页面与识别规则，永远无法发布，且外站无副本，直接删任务与物理文件。

═══════════════════════════════════════════════════════════════════════════
硬红线与系统防护
═══════════════════════════════════════════════════════════════════════════
  · FORBIDDEN = ("music5",)：严禁扫描或清理 music5（用户独立下载池）；
  · 系统目录黑名单：排除 . 和 @ 前缀目录，以及 incomplete / .OrphanedData / .RecycleBin / @eaDir / lost+found；
  · 先删 qB 任务（delete_files=False），再由 safe_rmtree 安全 rm -rf；
  · 删前强制导出 .torrent 备份至 /data/gc_trash/<日期>/，逐条写入 gc_log.csv 台账并立即 flush。
"""
import argparse
import csv
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import db

# 红线：严禁任何涉及 music5 的删除
FORBIDDEN = ("music5",)

# 系统目录与临时目录黑名单，绝不可误删
SYSTEM_BLACKLIST = {
    ".OrphanedData",
    ".RecycleBin",
    "@eaDir",
    "lost+found",
    "incomplete",
}

PATH_MAP = [("/downloads/", "/volume1/video/download/music/music3/")]
GC_ROOT = PATH_MAP[0][1]

HR_SEED_HOURS = 36.0        # 30 天内需做种 36 小时
HR_WINDOW_DAYS = 30.0       # H&R 考核窗口
HR_CLEAR_HOURS = 300.0      # 90 天内做满 300 小时可消除
HR_CLEAR_DAYS = 90.0

SEED_STATES = ("stalledUP", "uploading", "queuedUP", "pausedUP", "forcedUP")
TRASH_DIR = "/data/gc_trash"


def _now():
    return time.time()


def hr_satisfied(hours, days_since_complete):
    """HR 是否已无风险。`days_since_complete` 为负表示未知。"""
    if hours >= HR_SEED_HOURS:
        return True
    if days_since_complete > HR_WINDOW_DAYS and hours >= HR_CLEAR_HOURS and days_since_complete <= HR_CLEAR_DAYS:
        return True
    return False


def _get_external_copies():
    """获取外站常规做种池（music1, music2）中的同名目录集合与活跃任务集合。"""
    m1_root = "/volume1/video/download/music/music1"
    m2_root = "/volume1/video/download/music/music2"
    m1_dirs = set(os.listdir(m1_root)) if os.path.exists(m1_root) else set()
    m2_dirs = set(os.listdir(m2_root)) if os.path.exists(m2_root) else set()
    return m1_dirs | m2_dirs


def collect(qb, rules):
    """返回 (候选列表, 统计)。只读，不改任何状态。"""
    ts = [t for t in qb.torrents_info()
          if "open.cd" in (getattr(t, "tracker", "") or "")]
    now = _now()

    conn = db.connect()
    try:
        pub = {}
        for h, st in conn.execute(
                "SELECT info_hash, status FROM site_publish WHERE status='EXISTS'"):
            pub.setdefault(str(h).lower(), set()).add(1)
        tinfo = {}
        for h, status, rule, tid, mj, tfile in conn.execute(
                "SELECT info_hash, status, transfer_rule, opencd_tid, mapped_json, torrent_file "
                "  FROM tasks"):
            tinfo[str(h).lower()] = (status, (rule or "").upper(), tid, mj, tfile)
    finally:
        conn.close()

    ext_dirs = _get_external_copies()

    cands = []
    stats = {
        "total": len(ts),
        "skip_hr": 0,
        "skip_protect_allow": 0,
        "skip_state": 0,
        "skip_path": 0,
        "skip_system": 0,
        "R1": 0,
        "R2": 0,
        "R3": 0,
    }

    for t in ts:
        h = str(getattr(t, "hash", "") or "").lower()
        hours = float(getattr(t, "seeding_time", 0) or 0) / 3600.0
        co = float(getattr(t, "completion_on", 0) or 0)
        days = (now - co) / 86400.0 if co > 0 else -1.0
        state = str(getattr(t, "state", "") or "")
        size = int(getattr(t, "size", 0) or 0)
        sp = str(getattr(t, "save_path", "") or "")
        nm = str(getattr(t, "name", "") or "")
        cp = getattr(t, "content_path", "") or (os.path.join(sp, nm) if sp and nm else "")

        # 护栏 1: 系统黑名单
        if nm in SYSTEM_BLACKLIST or nm.startswith(".") or nm.startswith("@"):
            stats["skip_system"] += 1
            continue

        # 护栏 2: 状态保护 (必须在做种态)
        if state not in SEED_STATES:
            stats["skip_state"] += 1
            continue

        # 护栏 3: 路径安全
        if not cp or not cp.startswith("/downloads/"):
            stats["skip_path"] += 1
            continue

        # 护栏 4: 红线拦截
        for f in FORBIDDEN:
            if f in cp:
                print(f"❌ 触发禁删红线 ({f} in {cp})，程序紧急中止！")
                sys.exit(2)

        # 硬前置闸门: 做种未满 36h 坚决不碰
        if not hr_satisfied(hours, days):
            stats["skip_hr"] += 1
            continue

        status, rule, tid, mj, tfile = tinfo.get(h, (None, "", None, None, None))
        has_ext_copy = nm in ext_dirs
        is_published = h in pub

        hit = None
        # R1: 已发布且外站已承接（或外站有副本）
        if "R1" in rules and (is_published or has_ext_copy):
            hit = "R1"
        # R2: 禁转 或 明确不可逆失败/审核拒绝
        elif "R2" in rules and (rule == "DENY" or status in ("FAILED", "NEED_REVIEW")):
            hit = "R2"
        # R3: 死库存（无 tid、无 mapped 且独占 music3，入库满 30 天）
        elif "R3" in rules and not rule and not tid and not mj and not has_ext_copy and (days > 30.0 or days < 0):
            hit = "R3"
        # 保护拦截: 待转种且独占 music3 的健康种子，保留用于后续发布
        elif rule == "ALLOW" and not has_ext_copy:
            stats["skip_protect_allow"] += 1
            continue

        if not hit:
            continue

        stats[hit] += 1
        cands.append({
            "info_hash": h,
            "name": nm,
            "size": size,
            "content_path": cp,
            "hours": hours,
            "days": days,
            "rule": hit,
            "transfer_rule": rule,
            "status": status or "",
            "torrent_file": tfile or "",
            "has_ext_copy": has_ext_copy,
        })

    return cands, stats


def _host_path(cp):
    for src, dst in PATH_MAP:
        if cp.startswith(src):
            return dst + cp[len(src):]
    return None


def backup_torrent(qb, cand, day_dir):
    """把 .torrent 导出/备份到归档目录。返回路径或空串。"""
    os.makedirs(day_dir, exist_ok=True)
    dst = os.path.join(day_dir, cand["info_hash"] + ".torrent")
    # 优先拷贝本地记录的 torrent_file
    if cand.get("torrent_file") and os.path.exists(cand["torrent_file"]):
        try:
            shutil.copy2(cand["torrent_file"], dst)
            return dst
        except OSError:
            pass
    # 兜底：尝试从 qB API 导出
    try:
        content = qb.torrents_export(torrent_hash=cand["info_hash"])
        if content:
            with open(dst, "wb") as f:
                f.write(content)
            return dst
    except Exception:
        pass
    return ""


def safe_rmtree(host_path, timeout=120):
    """在宿主路径上安全 rm -rf。只允许 GC_ROOT 之下的单层目录。"""
    for f in FORBIDDEN:
        if f in host_path:
            return False, f"拒绝删除: 命中红线关键词 {f}"
    if not host_path or not host_path.startswith(GC_ROOT):
        return False, "路径不在 GC_ROOT 之下，拒绝删除"
    rest = host_path[len(GC_ROOT):].strip("/")
    if not rest or "/" in rest or "\\" in rest:
        return False, f"路径层级异常 ({rest})，拒绝删除"
    if rest in SYSTEM_BLACKLIST or rest.startswith(".") or rest.startswith("@"):
        return False, f"命中系统目录黑名单 ({rest})，拒绝删除"

    try:
        p = subprocess.run(["rm", "-rf", "--", host_path], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
    except Exception as e:
        return False, "rm 异常: %s" % str(e)[:60]
    if p.returncode != 0:
        return False, "rm rc=%d %s" % (p.returncode, p.stderr.decode("utf-8", "replace")[:60])
    return True, ""


def apply_one(qb, cand, day_dir):
    """删一条：备份 .torrent → 删 qB 任务（不删文件）→ safe_rmtree 删文件。"""
    bak = backup_torrent(qb, cand, day_dir)
    try:
        qb.torrents_delete(delete_files=False, torrent_hashes=cand["info_hash"])
    except Exception as e:
        return False, "删任务失败: %s" % str(e)[:80]

    hp = _host_path(cand["content_path"])
    if not hp:
        return True, "任务已删（路径未映射，盘未动）"
    if not os.path.exists(hp):
        return True, "任务+文件已删" + ("（备份 ok）" if bak else "")
    ok, why = safe_rmtree(hp)
    if not ok:
        return False, "任务已删，删盘失败: %s" % why
    return True, "任务+文件已删" + ("（备份 ok）" if bak else "")


def selftest():
    """离线安全自检，验证护栏与规则。"""
    assert hr_satisfied(36.0, 10.0) is True
    assert hr_satisfied(35.9, 10.0) is False
    assert hr_satisfied(300.0, 40.0) is True

    # 路径安全测试
    assert safe_rmtree("/etc/passwd")[0] is False
    assert safe_rmtree("/volume1/video/download/music/music3/")[0] is False
    assert safe_rmtree("/volume1/video/download/music/music3/a/b")[0] is False
    assert safe_rmtree("/volume1/video/download/music/music5/album")[0] is False
    assert safe_rmtree("/volume1/video/download/music/music3/incomplete")[0] is False
    assert safe_rmtree("/volume1/video/download/music/music3/.OrphanedData")[0] is False
    print("✅ opencd_gc selftest 全部通过！")
    return 0


def main():
    ap = argparse.ArgumentParser(description="皇后原种回收（默认只报告）")
    ap.add_argument("--apply", action="store_true", help="真删；不加只报告")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=不限）")
    ap.add_argument("--max-gb", type=float, default=0.0, help="容量硬上限 GB（0=不限）")
    ap.add_argument("--rules", default="R1,R2,R3", help="要跑的规则，逗号分隔")
    ap.add_argument("--show", type=int, default=15, help="打印多少条样本")
    ap.add_argument("--selftest", action="store_true", help="运行离线自检")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    rules = {r.strip().upper() for r in args.rules.split(",") if r.strip()}

    import qb_client
    config.QB_HOST = getattr(config, "QB_HOST_OPENCD", "http://127.0.0.1:3260")
    config.QB_API_KEY = getattr(config, "OPENCD_QB_API_KEY", "")
    qb = qb_client.build()

    cands, stats = collect(qb, rules)

    print("=" * 68)
    print("🧹 opencd_gc：皇后原种回收（阶段 1：music3 减负）")
    print("=" * 68)
    print("扫描 open.cd 原种: %d 条" % stats["total"])
    print("  跳过 · HR 未满足 (做种<36h) : %d" % stats["skip_hr"])
    print("  跳过 · 待转发独占新种保护   : %d" % stats["skip_protect_allow"])
    print("  跳过 · 非做种状态            : %d" % stats["skip_state"])
    print("  跳过 · 路径不合规            : %d" % stats["skip_path"])
    print("  跳过 · 系统目录黑名单        : %d" % stats["skip_system"])
    print("-" * 68)
    print("  候选 R1 已承接(外站做种中)   : %d" % stats["R1"])
    print("  候选 R2 禁转/失败(DENY/FAIL) : %d" % stats["R2"])
    print("  候选 R3 无tid死库存          : %d" % stats["R3"])
    total_bytes = sum(c["size"] for c in cands)
    print("  合计候选                    : %d 条 / %.2f GB" %
          (len(cands), total_bytes / 1024 ** 3))

    picked = cands
    if args.limit > 0:
        picked = picked[:args.limit]
    if args.max_gb > 0:
        cap = args.max_gb * 1024 ** 3
        acc, out = 0, []
        for c in picked:
            if acc + c["size"] > cap:
                break
            acc += c["size"]
            out.append(c)
        picked = out
    if args.limit > 0 or args.max_gb > 0:
        print("  本轮选取                    : %d 条 / %.2f GB" %
          (len(picked), sum(c["size"] for c in picked) / 1024 ** 3))

    for c in picked[:args.show]:
        ext_tag = "已交棒" if c["has_ext_copy"] else "独占"
        print("   [%s|%s] %s | %.0f天 %.0fh | %s" % (
            c["rule"], ext_tag, c["info_hash"][:12], c["days"], c["hours"], c["name"][:48]))
    if len(picked) > args.show:
        print("   …… 其余 %d 条略" % (len(picked) - args.show))

    if not picked:
        print("\n✔ 没有命中任何规则")
        return 0
    if not args.apply:
        print("\n[DRY-RUN] 未删任何东西。确认后加 --apply 执行。")
        return 0

    day_dir = os.path.join(TRASH_DIR, time.strftime("%Y%m%d"))
    log_path = os.path.join(day_dir, "gc_log.csv")
    os.makedirs(day_dir, exist_ok=True)
    new_file = not os.path.exists(log_path)

    ok_n = 0
    freed = 0
    # ★ 用 with 包住整个写入循环：apply_one 抛异常时也能正常 close + flush 台账
    with open(log_path, "a", encoding="utf-8-sig", newline="") as fh:
        wr = csv.writer(fh)
        if new_file:
            wr.writerow(["time", "info_hash", "rule", "ext_copy", "size_gb", "days", "hours", "ok", "note", "name"])

        for c in picked:
            ok, note = apply_one(qb, c, day_dir)
            if ok:
                ok_n += 1
                freed += c["size"]
            wr.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), c["info_hash"], c["rule"],
                         "Y" if c["has_ext_copy"] else "N",
                         round(c["size"] / 1024 ** 3, 3), round(c["days"], 1), round(c["hours"], 1),
                         "Y" if ok else "N", note, c["name"]])
            fh.flush()
            print("   %s %s %s" % ("✅" if ok else "❌", c["info_hash"][:12], note), flush=True)

    print("\n✅ 完成：成功 %d / %d 条，涉及名义体积约 %.2f GB" %
          (ok_n, len(picked), freed / 1024 ** 3))
    print("   台账: %s" % log_path)
    print("   .torrent 备份: %s" % day_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
