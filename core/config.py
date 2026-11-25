# -*- coding: utf-8 -*-
"""MusicHub 3.0 全局配置模型与环境加载模块

严格遵循 AGENTS.md 基础设施基线与防爆盘/防偷跑铁律。
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class QBInstanceConfig:
    name: str
    host: str = "192.168.0.7"
    port: int = 3016
    username: str = ""
    password: str = ""
    save_path: str = "/downloads"
    add_stopped: bool = True  # 严格铁律：默认暂停


@dataclass
class StorageConfig:
    disk_mount_path: str = "/volume1/video"
    # 宿主机上 3016 缓冲完成下载目录（实测挂载点 /volume2/qb_temp/completed_musicdownload）
    download_dir: str = "/volume2/qb_temp/completed_musicdownload"
    # 宿主机上 3010 发种区目录（实测挂载点 /volume2/docker_ssd/qbit-all/data/publish/Download）
    publish_dir: str = "/volume2/docker_ssd/qbit-all/data/publish/Download"
    library_pools: List[str] = field(default_factory=lambda: [
        "/volume1/video/download/music/music_pool_1",
        "/volume1/video/download/music/music_pool_2",
        "/volume1/video/download/music/music_pool_3",
        "/volume1/video/download/music/music_pool_4",
        "/volume1/video/download/music/music_pool_5",
        "/volume1/video/download/music/music_pool_6",
        "/volume1/video/download/music/music_pool_7",
        "/volume1/video/download/music/music_pool_8",
    ])
    forbidden_pools: List[str] = field(default_factory=lambda: [
        "music5",  # 铁律：music5 禁删红线
    ])
    # 动态背压水位（GB）
    watermark_green_gb: float = 500.0
    watermark_yellow_gb: float = 300.0
    watermark_red_gb: float = 50.0


@dataclass
class MusicHubConfig:
    # 实例配置
    musicdownload_qb: QBInstanceConfig = field(default_factory=lambda: QBInstanceConfig(
        name="musicdownload",
        port=3016,
        save_path="/downloads",
        add_stopped=True
    ))
    publish_qb: QBInstanceConfig = field(default_factory=lambda: QBInstanceConfig(
        name="publish",
        port=3010,
        save_path="/downloads",
        add_stopped=True
    ))
    
    storage: StorageConfig = field(default_factory=StorageConfig)
    
    # 代理配置
    http_proxy: str = "http://192.168.0.7:7894"
    
    # 数据库路径
    db_path: str = os.path.join(os.path.dirname(__file__), "..", "data", "musichub.db")
    
    # 运行与并发参数
    max_active_downloads: int = 3
    stalled_timeout_hours: int = 48
    retire_days: int = 14
    
    @classmethod
    def from_env(cls) -> "MusicHubConfig":
        """从环境变量或本地环境装配配置"""
        cfg = cls()
        cfg.musicdownload_qb.host = os.getenv("QB_MUSICDOWNLOAD_HOST", cfg.musicdownload_qb.host)
        cfg.musicdownload_qb.port = int(os.getenv("QB_MUSICDOWNLOAD_PORT", str(cfg.musicdownload_qb.port)))
        cfg.musicdownload_qb.username = os.getenv("QB_MUSICDOWNLOAD_USER", "")
        cfg.musicdownload_qb.password = os.getenv("QB_MUSICDOWNLOAD_PASS", "")
        
        cfg.publish_qb.host = os.getenv("QB_PUBLISH_HOST", cfg.publish_qb.host)
        cfg.publish_qb.port = int(os.getenv("QB_PUBLISH_PORT", str(cfg.publish_qb.port)))
        cfg.publish_qb.username = os.getenv("QB_PUBLISH_USER", "")
        cfg.publish_qb.password = os.getenv("QB_PUBLISH_PASS", "")

        cfg.storage.download_dir = os.getenv("STORAGE_DOWNLOAD_DIR", cfg.storage.download_dir)
        cfg.storage.publish_dir = os.getenv("STORAGE_PUBLISH_DIR", cfg.storage.publish_dir)
        cfg.storage.watermark_green_gb = float(os.getenv("WATERMARK_GREEN_GB", str(cfg.storage.watermark_green_gb)))
        cfg.storage.watermark_yellow_gb = float(os.getenv("WATERMARK_YELLOW_GB", str(cfg.storage.watermark_yellow_gb)))
        cfg.storage.watermark_red_gb = float(os.getenv("WATERMARK_RED_GB", str(cfg.storage.watermark_red_gb)))
        
        cfg.http_proxy = os.getenv("HTTP_PROXY", os.getenv("http_proxy", cfg.http_proxy))
        return cfg
