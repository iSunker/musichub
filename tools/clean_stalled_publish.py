#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/clean_stalled_publish.py —— 发种实例(3010)孤儿/无源 fake-seeding 假做种任务清理工具。

═══════════════════════════════════════════════════════════════════════════
功能与业务价值
═══════════════════════════════════════════════════════════════════════════
当种子被推入发种实例（3010 / qbit-publish）做种时，如果 SSD 发种区实际上并没有
该专辑的物理音频数据（未经过 handoff 复制或数据被提前清理），qB 在本地找不到文件，
哈希校验失败，任务会直接退化为 `state='stalledDL'` 且 `progress=0.0`。
作为该种子的全网首发者，外站此时根本没有其他 seeder，导致任务永久卡在停滞下载状态，
占用 qB 连接、产生无效 Tracker 上报。

本工具用于扫描目标发种实例，按以下三道防线严格判定孤儿任务并安全摘除：
  ① 状态闸门：处于非做种族（stalledDL / downloading / metaDL / stoppedDL / missingFiles / error 等）；
  ② 进度闸门：progress < 1.0（通常精确为 0.0）；
  ③ 发种区物理数据闸门：严格检查本地磁盘发种区（save_path/name 或 content_path）。
     - 若物理文件实际存在：绝不删除！提示需要 recheck；
     - 若物理文件缺失/为空：确认为无源假做种，列入摘除候选。

★ 安全铁律：
  · 默认 DRY-RUN 只读检查；必须显式加 `--apply` 才会执行删除。
  · 删除时严格设置 `delete_files=False`，绝不删除磁盘上的任何物理文件。

用法：
  # 1. 只读检查（默认）
  python tools/clean_stalled_publish.py
  # 2. 确认删除发种区无数据的假做种任务
  python tools/clean_stalled_publish.py --apply
  # 3. 指定 qB 地址（默认从 config.QB_HOST 获取，即 3010）
  python tools/clean_stalled_publish.py --host http://127.0.0.1:3010 --apply
"""

import argparse
import os
import sys

# 编码防护
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import _bootstrap  # noqa: F401
import config
import qb_client

# 正常做种族状态集合
SEEDING_STATES = {"stalledUP", "uploading", "forcedUP", "queuedUP", "checkingUP"}

# 异常下载/假做种族状态集合
STALLED_DL_STATES = {
    "stalledDL", "downloading", "metaDL", "stoppedDL",
    "pausedDL", "queuedDL", "missingFiles", "error", "unknown"
}


def _check_disk_data(t):
    """检查种子在本地发种区是否实际存在物理文件。

    :param t: qB torrent 对象
    :return: (exists: bool, reason: str, check_path: str)
    """
    cp = str(getattr(t, "content_path", "") or "").rstrip("/\\")
    sp = str(getattr(t, "save_path", "") or "").rstrip("/\\")
    name = str(getattr(t, "name", "") or "").strip()

    # 优先使用 content_path；若为空则用 save_path + name 拼接
    target = cp if cp else (os.path.join(sp, name) if (sp and name) else "")
    if not target:
        return False, "无法从任务获取 content_path 或 save_path+name", ""

    if not os.path.exists(target):
        return False, f"物理路径不存在: {target}", target

    if os.path.isdir(target):
        try:
            items = os.listdir(target)
            if not items:
                return False, f"物理目录为空: {target}", target
            return True, f"物理目录正常存在({len(items)}项): {target}", target
        except OSError as e:
            return False, f"访问物理目录异常: {type(e).__name__}: {e}", target

    try:
        size = os.path.getsize(target)
        if size <= 0:
            return False, f"物理单文件为零字节: {target}", target
        return True, f"物理单文件正常({size}字节): {target}", target
    except OSError as e:
        return False, f"访问物理文件异常: {type(e).__name__}: {e}", target


def scan_and_clean(qb, *, apply_delete=False, category=None):
    """扫描并清理无源假做种任务。"""
    try:
        if category:
            torrents = qb.torrents_info(category=category)
        else:
            torrents = qb.torrents_info()
    except Exception as e:
        print(f"❌ 获取 qB 种子列表失败: {type(e).__name__}: {e}")
        return 1

    total_count = len(torrents)
    seeding_count = 0
    recheck_needed = []
    fake_stalled_list = []

    for t in torrents:
        h = str(getattr(t, "hash", "") or getattr(t, "infohash_v1", "")).lower()
        name = str(getattr(t, "name", "") or "unknown")
        st = str(getattr(t, "state", "") or "")
        prog = float(getattr(t, "progress", 0) or 0)
        cat = str(getattr(t, "category", "") or "")

        # 1. 正常做种任务
        if prog >= 1.0 and st in SEEDING_STATES:
            seeding_count += 1
            continue

        # 2. 处于非做种状态且进度小于 1.0
        if prog < 1.0 or st in STALLED_DL_STATES:
            has_data, why, path = _check_disk_data(t)
            if has_data:
                # 本地磁盘实际有数据，说明只是任务未完成校验，绝不误删
                recheck_needed.append((h, name, st, prog, cat, why))
            else:
                # 本地发种区根本无物理数据，确认为假做种孤儿任务
                fake_stalled_list.append((h, name, st, prog, cat, why))

    print("=" * 76)
    print("📊 发种实例(3010)做种状态体检报告")
    print("=" * 76)
    print(f"  实例总种子数            : {total_count}")
    print(f"  ✅ 正常做种中 (progress=1.0) : {seeding_count}")
    print(f"  ⚠️  有数据但未做种 (需recheck) : {len(recheck_needed)}")
    print(f"  ❌ 无数据假做种 (stalledDL/无源): {len(fake_stalled_list)}")
    print("=" * 76)

    if recheck_needed:
        print("\n⚠️  【建议触发 recheck 的任务】(发种区有物理文件但未正常做种):")
        for h, name, st, prog, cat, why in recheck_needed[:10]:
            print(f"  · {h[:12]}… [{cat or '无分类'}] state={st} prog={prog:.2f} {name[:36]}")
            print(f"      {why}")
        if len(recheck_needed) > 10:
            print(f"    ... 还有 {len(recheck_needed) - 10} 项未列出")

    if not fake_stalled_list:
        print("\n🎉 未发现无数据假做种孤儿任务，系统健康！")
        return 0

    print(f"\n❌ 【待摘除的无物理数据假做种任务】共 {len(fake_stalled_list)} 条:")
    for h, name, st, prog, cat, why in fake_stalled_list:
        print(f"  · {h[:12]}… [{cat or '无分类'}] state={st} prog={prog:.2f} {name[:36]}")
        print(f"      原因: {why}")

    if not apply_delete:
        print("\n💡 当前处于 [DRY-RUN 只读预览] 模式，未对 qB 执行任何删除。")
        print("   若确认摘除上述无数据假做种任务，请添加 `--apply` 参数执行：")
        print("   python tools/clean_stalled_publish.py --apply")
        return 0

    print("\n▶ 开始从 qB 摘除无数据假做种任务（严格设置 delete_files=False）...")
    deleted_ok = 0
    deleted_fail = 0

    for h, name, _, _, _, _ in fake_stalled_list:
        try:
            # 严格保留 delete_files=False，仅从客户端移除，不碰磁盘
            qb.torrents_delete(delete_files=False, torrent_hashes=h)
            deleted_ok += 1
            print(f"  🗑️  已摘除: {h[:12]}… {name[:40]}")
        except Exception as e:
            deleted_fail += 1
            print(f"  ❌ 摘除失败 {h[:12]}…: {type(e).__name__}: {e}")

    print("=" * 76)
    print(f"✅ 清理完成：成功摘除 {deleted_ok} 条，失败 {deleted_fail} 条。")
    print("=" * 76)
    return 0 if deleted_fail == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="清理发种实例(3010)发种区无数据的假做种任务")
    parser.add_argument("--host", default=None,
                        help="目标 qB WebUI 地址（默认使用 config.QB_HOST = 3010）")
    parser.add_argument("--category", default=None,
                        help="仅检查指定分类（如 Musopia、CrabPT）")
    parser.add_argument("--apply", action="store_true",
                        help="真写模式：确认从 qB 中摘除这些无数据任务（默认只读预览）")
    args = parser.parse_args(argv)

    target_host = args.host or config.QB_HOST
    print(f"🔗 正在连接目标发种实例: {target_host} ...")

    # 构建带指定 host 的轻量 config
    import types
    cfg = types.SimpleNamespace(
        QB_HOST=target_host,
        QB_USER=config.QB_USER,
        QB_PASS=config.QB_PASS,
        QB_API_KEY=getattr(config, "QB_API_KEY", ""),
    )

    try:
        qb = qb_client.build(cfg)
    except Exception as e:
        print(f"❌ 连不上目标 qB ({target_host}): {type(e).__name__}: {e}")
        return 2

    return scan_and_clean(qb, apply_delete=args.apply, category=args.category)


if __name__ == "__main__":
    sys.exit(main())
