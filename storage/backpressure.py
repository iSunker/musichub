# -*- coding: utf-8 -*-
"""MusicHub 3.0 动态背压水位监控器

依据群晖机械盘 (/volume1，通过 SMB 挂载对应 \\iSunker-DS423\video) 与固态盘 (/volume2)
的实际物理可用空间动态决定是否放行、限速或熔断。

针对 56TB 机械盘现场事实（当前可用 978 GB）：
- 绿色安全区 (GREEN)：可用 >= 500 GB，全速满载吸纳新种 (<= 15 GB)
- 黄色预警区 (YELLOW)：300 GB <= 可用 < 500 GB，收紧单种 <= 5 GB，免下载硬链接直通优先
- 红色熔断区 (RED)：可用 < 300 GB，阻断新下载任务，触发安全孤儿 GC
"""

import shutil
import os
import logging
from typing import Tuple

logger = logging.getLogger("StorageBackpressure")


class StorageBackpressure:
    # 群晖卷路径与 UNC 共享名别名映射
    SMB_FALLBACK_MAP = {
        "/volume1": r"\\iSunker-DS423\video",
        "/volume2": r"\\iSunker-DS423\docker_ssd",
    }

    def __init__(
        self,
        mount_path: str = "/volume1",
        green_gb: float = 500.0,
        yellow_gb: float = 300.0,
        red_gb: float = 200.0
    ):
        self.mount_path = mount_path
        self.green_gb = green_gb
        self.yellow_gb = yellow_gb
        self.red_gb = red_gb

    def resolve_accessible_target(self) -> str:
        """解析当前环境真实可访问的探测路径（兼容 Linux 容器原生挂载与 Windows 本机开发调试）"""
        target = self.mount_path
        if os.path.exists(target):
            return target

        # 若处于 Windows 环境，优先尝试对应的群晖 UNC 路径
        for vol_prefix, smb_path in self.SMB_FALLBACK_MAP.items():
            if target.startswith(vol_prefix) or target == vol_prefix:
                if os.path.exists(smb_path):
                    return smb_path

        # 兜底：若前两者均不可达，回退至当前驱动器根目录
        split_drive = os.path.splitdrive(os.path.abspath("."))[0]
        fallback_drive = (split_drive + "\\") if split_drive else "."
        return fallback_drive

    def get_free_space_gb(self) -> float:
        """获取目标存储卷剩余物理可用空间（GB）"""
        target = self.resolve_accessible_target()
        try:
            usage = shutil.disk_usage(target)
            return usage.free / (1024 ** 3)
        except Exception as e:
            logger.warning(f"Failed to check disk usage on {target}: {e}")
            return 999.0

    def assess_watermark(self) -> Tuple[str, float]:
        """评估当前背压水位：GREEN, YELLOW, RED"""
        free_gb = self.get_free_space_gb()
        if free_gb < self.red_gb:
            return "RED", free_gb
        elif free_gb < self.yellow_gb:
            return "YELLOW", free_gb
        return "GREEN", free_gb

    def can_ingest_torrent(self, torrent_size_bytes: int, is_hardlink_matched: bool = False) -> Tuple[bool, str]:
        """准入决策：是否允许吸入新任务"""
        level, free_gb = self.assess_watermark()
        size_gb = torrent_size_bytes / (1024 ** 3)

        if level == "RED":
            return False, f"Backpressure RED: Disk free {free_gb:.1f}GB < {self.red_gb}GB, ingestion blocked"

        if level == "YELLOW":
            # 黄色警戒下，免下载（本地硬链接命中）直通允许放行；真实下载需限制单种 <= 5GB
            if is_hardlink_matched:
                return True, f"Backpressure YELLOW: Hardlink matched, bypass download ({free_gb:.1f}GB free)"
            if size_gb > 5.0:
                return False, f"Backpressure YELLOW: Torrent size {size_gb:.1f}GB > 5GB threshold ({free_gb:.1f}GB free)"
            return True, f"Backpressure YELLOW: Allowed with size {size_gb:.1f}GB ({free_gb:.1f}GB free)"

        # GREEN 正常放行，单种体积门禁为 15GB
        if size_gb > 15.0:
            return False, f"Backpressure GREEN: Torrent size {size_gb:.1f}GB > 15GB upper limit"

        return True, f"Backpressure GREEN: Normal ingestion allowed ({free_gb:.1f}GB free)"
