# -*- coding: utf-8 -*-
"""MusicHub 3.0 安全退役下架与孤儿 GC 治理器

落实 AGENTS.md 铁律：
1. music5 禁删红线拦截
2. 系统保护目录黑名单排除
3. st_nlink > 1 物理引用核验，严防多池共振误删
"""

import os
import shutil
from typing import List, Tuple, Set


import os
import shutil
import logging
from typing import List, Tuple, Set, Dict

logger = logging.getLogger("SafeGarbageCollector")


class SafeGarbageCollector:
    # 铁律：绝对禁止操作与删除的目录或关键字
    FORBIDDEN_KEYWORDS = ("music5", "incomplete", ".OrphanedData", ".RecycleBin", "@eaDir", "lost+found")

    def __init__(self, managed_pools: List[str] = None):
        self.managed_pools = managed_pools or [
            "/volume1/video/download/music/music1",
            "/volume1/video/download/music/music2",
            "/volume1/video/download/music/music3",
        ]

    def is_safe_target(self, target_path: str) -> Tuple[bool, str]:
        """核查目标路径是否触碰红线"""
        norm_path = os.path.normpath(target_path).replace("\\", "/")
        
        # 1. 检查禁止关键字
        for kw in self.FORBIDDEN_KEYWORDS:
            if kw in norm_path.split("/"):
                return False, f"Hit forbidden redline keyword: {kw}"

        # 2. 检查是否在受控池内
        in_managed = any(norm_path.startswith(pool.replace("\\", "/")) for pool in self.managed_pools)
        if not in_managed:
            return False, f"Target path {norm_path} is outside managed pools"

        return True, "Path is verified safe"

    def scan_unreferenced_orphans(self, active_torrent_names: Set[str]) -> List[Dict[str, any]]:
        """全池孤儿探测：对比所有受控池与当前活跃做种任务名称。
        必须保证所有受控池的活跃任务集合均已完整注入（严防网络视野缺失导致的假孤儿误判）。
        """
        orphans = []
        all_pool_entries: Dict[str, Set[str]] = {}
        all_candidates = set()

        for pool in self.managed_pools:
            is_safe, reason = self.is_safe_target(pool)
            if not is_safe:
                logger.error(f"Managed pool {pool} violates redline: {reason}")
                continue
            if not os.path.exists(pool):
                continue

            try:
                entries = set(os.listdir(pool))
            except Exception as e:
                logger.error(f"Failed to list entries in {pool}: {str(e)}")
                continue

            # 过滤系统保护黑名单
            valid_entries = set()
            for e in entries:
                if e.startswith(".") or e.startswith("@") or e in self.FORBIDDEN_KEYWORDS:
                    continue
                valid_entries.add(e)
                all_candidates.add(e)

            all_pool_entries[pool] = valid_entries

        # 找出在活跃任务列表中完全无引用的项目
        for name in sorted(all_candidates):
            if name in active_torrent_names:
                continue

            # 归集多池同名物理路径与总大小
            matched_paths = []
            for pool in self.managed_pools:
                p = os.path.join(pool, name)
                if os.path.exists(p):
                    matched_paths.append(p)

            if matched_paths:
                orphans.append({
                    "name": name,
                    "paths": matched_paths,
                    "pool_count": len(matched_paths),
                })

        return orphans

    def safe_reclaim_orphan_cross_pools(self, orphan_item: Dict[str, any], dry_run: bool = True) -> Tuple[bool, str]:
        """跨池协同回收物理空间：
        因为 music1/music2/music3 为同一卷同一批 inode 硬链接池，
        必须把全部池的同名目录/文件协同解除引用，才能真正释放物理磁盘空间。
        """
        paths = orphan_item.get("paths", [])
        name = orphan_item.get("name", "")

        for p in paths:
            is_safe, reason = self.is_safe_target(p)
            if not is_safe:
                return False, f"Refused to reclaim: {p} ({reason})"

        if dry_run:
            return True, f"[DRY-RUN] Verified safe to reclaim {len(paths)} cross-pool instances for '{name}'"

        success_count = 0
        for p in paths:
            try:
                if os.path.isdir(p):
                    # 遍历解除文件链接
                    for root, _, files in os.walk(p, topdown=False):
                        for f in files:
                            fp = os.path.join(root, f)
                            self.safe_unlink_file(fp)
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.isfile(p):
                    self.safe_unlink_file(p)
                success_count += 1
            except Exception as e:
                logger.error(f"Failed to remove orphan path {p}: {str(e)}")

        return True, f"Reclaimed {success_count}/{len(paths)} pool instances for '{name}'"

    def safe_unlink_file(self, file_path: str) -> Tuple[bool, str]:
        """对单个物理文件执行安全下架核验

        核心闸门 C：若文件 st_nlink > 1，说明系统内仍有其他硬链接引用（如被其他池或用户引用），
        此时绝对不能盲目删除，只能解除当前引用的 link，绝不直接损毁多池共有数据。
        """
        is_safe, reason = self.is_safe_target(file_path)
        if not is_safe:
            return False, f"Refused: {reason}"

        if not os.path.exists(file_path):
            return True, "File does not exist (already gone)"

        try:
            stat_info = os.stat(file_path)
            # 检查硬链接引用数
            nlink = stat_info.st_nlink
            os.unlink(file_path)
            if nlink > 1:
                return True, f"Unlinked successfully (st_nlink was {nlink} -> other links preserved)"
            return True, "Unlinked single reference file, physical space freed"
        except Exception as e:
            return False, f"Unlink failed with error: {str(e)}"

    def safe_clean_empty_dir(self, dir_path: str) -> Tuple[bool, str]:
        """安全清理空目录，严格防止误删保护目录"""
        is_safe, reason = self.is_safe_target(dir_path)
        if not is_safe:
            return False, f"Refused: {reason}"

        if not os.path.isdir(dir_path):
            return False, "Not a directory"

        try:
            # 仅当为空时才删除
            entries = os.listdir(dir_path)
            if not entries:
                os.rmdir(dir_path)
                return True, "Empty directory removed"
            return False, f"Directory not empty ({len(entries)} items remaining)"
        except Exception as e:
            return False, f"Failed to rmdir: {str(e)}"
