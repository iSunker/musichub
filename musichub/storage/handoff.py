# -*- coding: utf-8 -*-
"""MusicHub 3.0 流式移交调度器 (Ready-Handoff)

实现：
1. 监听/承接 musicdownload (3016) 完成的 SSD 任务
2. 闸门 A：本地物理文件真实性与完整性硬校验（杜绝幽灵做种）
3. 顺序流式复制（SSD /volume2 -> 机械盘 /volume1 各保种池）
4. 目标保种 qB 注入与秒做种校验 (skip_checking=True)
5. 安全清理 SSD 缓存与 3016 任务
"""

import os
import shutil
import logging
from typing import Dict, List, Optional, Tuple

from musichub.core.models import CanonicalMusicItem
from musichub.core.qb_client import QBClient

logger = logging.getLogger("ReadyHandoff")


class ReadyHandoffManager:
    def __init__(
        self,
        qb_download: QBClient,
        target_qb_map: Optional[Dict[str, QBClient]] = None,
        default_target_pool: str = "/volume1/video/download/music/music3",
    ):
        self.qb_download = qb_download
        self.target_qb_map = target_qb_map or {}
        self.default_target_pool = default_target_pool

    def verify_physical_payload(self, ssd_path: str) -> Tuple[bool, str, int]:
        """【闸门 A】物理文件硬校验：严禁仅凭 qB 的 progress==1.0 盲目认定文件就绪"""
        if not os.path.exists(ssd_path):
            return False, f"SSD path does not exist on disk: {ssd_path}", 0

        total_bytes = 0
        file_count = 0

        if os.path.isfile(ssd_path):
            sz = os.path.getsize(ssd_path)
            if sz == 0:
                return False, f"Single file is 0 bytes: {ssd_path}", 0
            return True, "Single file verified", sz

        for root, _, files in os.walk(ssd_path):
            for f in files:
                if f.endswith(".!qB") or "@eaDir" in root:
                    continue
                fp = os.path.join(root, f)
                sz = os.path.getsize(fp)
                if sz == 0:
                    return False, f"Detected 0-byte file: {fp}", 0
                total_bytes += sz
                file_count += 1

        if file_count == 0 or total_bytes < 1024 * 1024:
            return False, f"Payload empty or abnormally small ({file_count} files, {total_bytes} bytes)", 0

        return True, f"Physical verification passed ({file_count} files, {total_bytes} bytes)", total_bytes

    def route_target_pool(self, origin_site: str) -> Tuple[str, Optional[QBClient]]:
        """根据来源站点与 Tracker 路由至目标保种池与对应 qB 客户端"""
        site_lower = (origin_site or "").lower()
        if "opencd" in site_lower:
            pool = "/volume1/video/download/music/music3"
            client = self.target_qb_map.get("music3") or self.target_qb_map.get("3260")
        elif any(s in site_lower for s in ("chd", "mteam", "m-team", "spring")):
            pool = "/volume1/video/download/music/music2"
            client = self.target_qb_map.get("music2") or self.target_qb_map.get("3240")
        else:
            pool = self.default_target_pool
            client = self.target_qb_map.get("music1") or self.target_qb_map.get("3220")

        return pool, client

    def copy_stream_to_hdd(self, src_path: str, dst_parent_dir: str) -> Tuple[bool, str, str]:
        """以顺序流将完整资源从 SSD 复制到机械盘目标池（单任务顺序写，平抑机械盘随机寻道）"""
        try:
            os.makedirs(dst_parent_dir, exist_ok=True)
            base_name = os.path.basename(src_path.rstrip("/\\"))
            dst_final_path = os.path.join(dst_parent_dir, base_name)

            if os.path.exists(dst_final_path):
                # 目标已存在同名数据，核验大小是否匹配
                src_sz = sum(os.path.getsize(os.path.join(r, f)) for r, _, files in os.walk(src_path) for f in files) if os.path.isdir(src_path) else os.path.getsize(src_path)
                dst_sz = sum(os.path.getsize(os.path.join(r, f)) for r, _, files in os.walk(dst_final_path) for f in files) if os.path.isdir(dst_final_path) else os.path.getsize(dst_final_path)
                if src_sz == dst_sz and src_sz > 0:
                    logger.info(f"Target already exists with matching size ({src_sz} bytes): {dst_final_path}")
                    return True, "Target already exists and matched size", dst_final_path

            logger.info(f"Streaming data from SSD {src_path} -> HDD {dst_final_path}...")
            if os.path.isdir(src_path):
                shutil.copytree(src_path, dst_final_path, dirs_exist_ok=True)
            else:
                shutil.copy2(src_path, dst_final_path)

            return True, "Streaming copy completed", dst_final_path
        except Exception as e:
            logger.error(f"Failed to stream copy {src_path} -> {dst_parent_dir}: {str(e)}")
            return False, f"Copy error: {str(e)}", ""

    def handoff_task(
        self,
        torrent_hash: str,
        torrent_name: str,
        ssd_path: str,
        origin_site: str = "",
        torrent_file_bytes: Optional[bytes] = None,
    ) -> Tuple[bool, str, str]:
        """执行完整的就绪移交流水线：
        1. 闸门 A 物理校验
        2. 路由目标保种池
        3. 顺序流复制至 HDD
        4. (可选) 注入目标保种 qB
        5. 安全回收 SSD 物理文件与 3016 任务
        """
        # 1. 闸门 A 校验
        ok, msg, total_bytes = self.verify_physical_payload(ssd_path)
        if not ok:
            logger.error(f"Handoff Gate A rejected for '{torrent_name}': {msg}")
            return False, f"Gate A rejection: {msg}", ""

        # 2. 路由目标
        dst_pool, target_qb = self.route_target_pool(origin_site)

        # 3. 复制流移交
        copy_ok, copy_msg, hdd_path = self.copy_stream_to_hdd(ssd_path, dst_pool)
        if not copy_ok or not hdd_path:
            return False, f"Failed streaming copy: {copy_msg}", ""

        # 4. 若提供了目标保种 qB 与种子，注入做种
        if target_qb and torrent_file_bytes:
            inject_ok = target_qb.add_torrent_file(
                torrent_bytes=torrent_file_bytes,
                save_path="/downloads",  # 统一遵守 /downloads 一级目录契约
                skip_checking=True,
                paused=False,
                tags="musichub,reseed",
            )
            if not inject_ok:
                logger.warning(f"Target qB inject failed for {torrent_name}, keep HDD copy but suspend SSD deletion")
                return True, "Copied to HDD but qB inject failed", hdd_path

        # 5. 回收 SSD 临时文件并从 3016 移除任务
        try:
            self.qb_download.delete_torrent(torrent_hash, delete_files=False)
            if os.path.isdir(ssd_path):
                shutil.rmtree(ssd_path, ignore_errors=True)
            elif os.path.isfile(ssd_path):
                os.remove(ssd_path)
            logger.info(f"SSD buffer cleared for '{torrent_name}' at {ssd_path}")
        except Exception as e:
            logger.warning(f"Failed to delete SSD cache for '{torrent_name}': {str(e)}")

        return True, "Handoff successfully completed", hdd_path
