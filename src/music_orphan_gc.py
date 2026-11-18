# -*- coding: utf-8 -*-
"""跨池孤儿目录清理（GC 阶段 2）：清理 music1/music2/music3 中全无任务引用的孤儿目录，释放真实物理空间。

═══════════════════════════════════════════════════════════════════════════
两阶段渐进式清理：阶段 2（跨池孤儿物理空间回收）
═══════════════════════════════════════════════════════════════════════════
背景与硬链接机制（2026-11-13 / 2026-11-14 实测证实）：
  · music1 / music2 / music3 是同一文件系统卷（/volume1）下的硬链接副本池；
  · 单独删一侧只减少 inode 引用计数，空间分文不放；
  · 只有当一个专辑在所有活跃做种实例（music1, music2, music3）中均无任何任务引用时，
    跨池将其同名目录全部安全删除，物理数据块才被真正回收释放。

═══════════════════════════════════════════════════════════════════════════
硬红线与防误删护栏
═══════════════════════════════════════════════════════════════════════════
  · FORBIDDEN = ("music5",)：严禁扫描或删除 music5（用户独立下载池）；
  · 实例必须全连通：music1, music2, music3 三端必须全部成功获取活跃任务列表，
    任一实例不可达时立即中止，绝不可在视野缺失下进行「无任务引用」判定（防假孤儿）；
  · 系统目录黑名单：排除 . 和 @ 开头，以及 incomplete / .OrphanedData / .RecycleBin / @eaDir / lost+found；
  · 路径严格限定在白名单根之下的单层子目录；
  · 逐条记录 orphan_gc_log.csv 台账，默认只报告（DRY-RUN）。
"""
import argparse
import csv
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

FORBIDDEN = ("music5",)

SYSTEM_BLACKLIST = {
    ".OrphanedData",
    ".RecycleBin",
    "@eaDir",
    "lost+found",
    "incomplete",
}

POOL_ROOTS = [
    "/volume1/video/download/music/music1",
    "/volume1/video/download/music/music2",
    "/volume1/video/download/music/music3",
]

INSTANCES = [
    ("music1", "http://127.0.0.1:3220"),
    ("music2", "http://127.0.0.1:3240"),
    ("music3", "http://127.0.0.1:3260"),
]

TRASH_DIR = "/data/gc_trash"


def _safe_rmtree(target_path, timeout=120):
    """安全删除单层孤儿目录。"""
    for f in FORBIDDEN:
        if f in target_path:
            return False, f"拒绝删除: 命中红线关键词 {f}"

    matched_root = None
    for r in POOL_ROOTS:
        if target_path.startswith(r.rstrip("/") + "/"):
            matched_root = r.rstrip("/") + "/"
            break

    if not matched_root:
        return False, "路径不在合法 POOL_ROOTS 白名单内，拒绝删除"

    rest = target_path[len(matched_root):].strip("/\\")
    if not rest or "/" in rest or "\\" in rest:
        return False, f"路径层级异常 ({rest})，拒绝删除"
    if rest in SYSTEM_BLACKLIST or rest.startswith(".") or rest.startswith("@"):
        return False, f"命中系统目录黑名单 ({rest})，拒绝删除"

    try:
        p = subprocess.run(["rm", "-rf", "--", target_path], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
    except Exception as e:
        return False, "rm 异常: %s" % str(e)[:60]
    if p.returncode != 0:
        return False, "rm rc=%d %s" % (p.returncode, p.stderr.decode("utf-8", "replace")[:60])
    return True, ""


def collect_active_names():
    """从各音乐做种实例拉取全部活跃任务的目录名称集合。

    ★ 任一实例失败直接抛异常退出，绝不在信息不全时误删。
    """
    import types
    import qb_client

    active_names = set()
    for name, host in INSTANCES:
        cfg = types.SimpleNamespace(
            QB_HOST=host,
            QB_USER=getattr(config, "QB_USER", "admin"),
            QB_PASS=getattr(config, "QB_PASS", ""),
            QB_API_KEY=getattr(config, "OPENCD_QB_API_KEY", "") if "3260" in host else getattr(config, "QB_API_KEY", ""),
        )
        try:
            cli = qb_client.build(cfg)
            torrents = cli.torrents_info()
        except Exception as e:
            raise RuntimeError(f"连接实例 {name}({host}) 获取任务列表失败: {e}") from e

        for t in torrents:
            nm = getattr(t, "name", "")
            if nm:
                active_names.add(nm)
            # content_path 一级目录作为双重保险
            cp = getattr(t, "content_path", "")
            if cp:
                base = os.path.basename(cp.rstrip("/\\"))
                if base:
                    active_names.add(base)

    return active_names


def scan_orphans(active_names):
    """扫描三池物理目录，找出全池无任务引用的孤儿。"""
    pool_dirs = {}
    all_names = set()

    for root in POOL_ROOTS:
        for f in FORBIDDEN:
            if f in root:
                raise RuntimeError(f"红线阻止: POOL_ROOTS 包含 {f}")
        if not os.path.exists(root):
            continue
        try:
            entries = os.listdir(root)
        except OSError as e:
            raise RuntimeError(f"读取物理目录 {root} 失败: {e}") from e

        valid = set()
        for e in entries:
            if e in SYSTEM_BLACKLIST or e.startswith(".") or e.startswith("@"):
                continue
            valid.add(e)
            all_names.add(e)
        pool_dirs[root] = valid

    orphans = []
    for nm in sorted(all_names):
        if nm in active_names:
            continue
        # 属于全池无人引用的孤儿
        paths = []
        sizes = []
        for root in POOL_ROOTS:
            p = os.path.join(root, nm)
            if os.path.exists(p):
                paths.append(p)
                # 计算大小估算
                sz = 0
                try:
                    if os.path.isdir(p):
                        for f in os.listdir(p):
                            fp = os.path.join(p, f)
                            if os.path.isfile(fp):
                                sz += os.path.getsize(fp)
                    else:
                        sz = os.path.getsize(p)
                except OSError:
                    pass
                sizes.append(sz)

        max_sz = max(sizes) if sizes else 0
        orphans.append({
            "name": nm,
            "paths": paths,
            "pool_count": len(paths),
            "size": max_sz,
        })

    return orphans


def apply_orphan(orphan):
    """跨池删除一个孤儿的所有物理副本。"""
    notes = []
    all_ok = True
    for p in orphan["paths"]:
        ok, why = _safe_rmtree(p)
        if not ok:
            all_ok = False
            notes.append(f"{os.path.basename(os.path.dirname(p))}: 失败({why})")
        else:
            notes.append(f"{os.path.basename(os.path.dirname(p))}: ok")
    return all_ok, "; ".join(notes)


def selftest():
    assert _safe_rmtree("/etc/passwd")[0] is False
    assert _safe_rmtree("/volume1/video/download/music/music1")[0] is False
    assert _safe_rmtree("/volume1/video/download/music/music1/a/b")[0] is False
    assert _safe_rmtree("/volume1/video/download/music/music5/album")[0] is False
    assert _safe_rmtree("/volume1/video/download/music/music1/incomplete")[0] is False
    assert _safe_rmtree("/volume1/video/download/music/music2/.OrphanedData")[0] is False
    print("✅ music_orphan_gc selftest 全部通过！")
    return 0


def main():
    ap = argparse.ArgumentParser(description="跨池孤儿目录物理清理（阶段 2）")
    ap.add_argument("--apply", action="store_true", help="真删；不加只报告")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=不限）")
    ap.add_argument("--max-gb", type=float, default=0.0, help="容量硬上限 GB（0=不限）")
    ap.add_argument("--show", type=int, default=15, help="打印多少条样本")
    ap.add_argument("--selftest", action="store_true", help="运行离线自检")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    print("=" * 68)
    print("🧹 music_orphan_gc：跨池孤儿目录清理（阶段 2：空间彻底回收）")
    print("=" * 68)

    print("正在拉取 music1 / music2 / music3 活跃任务...")
    active_names = collect_active_names()
    print(f"  各做种实例活跃任务名合计: {len(active_names)} 个")

    print("正在扫描跨池物理目录...")
    orphans = scan_orphans(active_names)
    total_sz = sum(o["size"] for o in orphans)
    print(f"  发现跨池无人引用孤儿: {len(orphans)} 个 | 预估物理体积: {total_sz / 1024**3:.2f} GB")

    picked = orphans
    if args.limit > 0:
        picked = picked[:args.limit]
    if args.max_gb > 0:
        cap = args.max_gb * 1024 ** 3
        acc, out = 0, []
        for o in picked:
            if acc + o["size"] > cap:
                break
            acc += o["size"]
            out.append(o)
        picked = out

    if args.limit > 0 or args.max_gb > 0:
        print(f"  本轮选取: {len(picked)} 条 / {sum(o['size'] for o in picked) / 1024**3:.2f} GB")

    for o in picked[:args.show]:
        print(f"   [{o['pool_count']}池副本] {o['size']/1024**2:6.1f}MB | {o['name'][:55]}")
    if len(picked) > args.show:
        print(f"   …… 其余 {len(picked) - args.show} 条略")

    if not picked:
        print("\n✔ 没有发现任何孤儿目录")
        return 0

    if not args.apply:
        print("\n[DRY-RUN] 未删任何东西。确认后加 --apply 执行。")
        return 0

    day_dir = os.path.join(TRASH_DIR, time.strftime("%Y%m%d"))
    log_path = os.path.join(day_dir, "orphan_gc_log.csv")
    os.makedirs(day_dir, exist_ok=True)
    new_file = not os.path.exists(log_path)

    ok_n = 0
    freed = 0
    # ★ 用 with 包住整个写入循环：apply_orphan 抛异常时也能正常 close + flush 台账
    with open(log_path, "a", encoding="utf-8-sig", newline="") as fh:
        wr = csv.writer(fh)
        if new_file:
            wr.writerow(["time", "name", "pool_count", "size_mb", "ok", "note"])

        for o in picked:
            ok, note = apply_orphan(o)
            if ok:
                ok_n += 1
                freed += o["size"]
            wr.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), o["name"], o["pool_count"],
                         round(o["size"] / 1024 ** 2, 2), "Y" if ok else "N", note])
            fh.flush()
            print(f"   {'✅' if ok else '❌'} {o['name'][:45]} {note}", flush=True)

    print(f"\n✅ 完成：成功清理 {ok_n} / {len(picked)} 个孤儿专辑，释放约 {freed / 1024**3:.2f} GB 物理空间")
    print(f"   台账: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
