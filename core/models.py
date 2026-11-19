# -*- coding: utf-8 -*-
"""MusicHub 3.0 规范化音乐元数据模型 (Canonical Music Item)"""

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class AudioTrackInfo:
    track_number: int
    title: str
    artist: str
    duration_seconds: float = 0.0
    bit_depth: int = 16
    sample_rate: int = 44100
    channels: int = 2
    codec: str = "FLAC"  # FLAC, APE, WAV, DSF, DFF
    file_path: str = ""
    file_size: int = 0
    stream_md5: str = ""


@dataclass
class CanonicalMusicItem:
    # 核心元数据（归一化）
    title: str = ""
    artist: str = ""
    album: str = ""
    year: str = ""
    format: str = "FLAC"             # FLAC, APE, WAV, DSD
    medium: str = "CD"              # CD, Vinyl, WEB-DL, Blu-ray Audio
    standard: str = "Lossless"      # Lossless, Hi-Res (>=24bit or >=88.2kHz)
    
    # 扩展属性
    edition: Optional[str] = None   # First Edition, Remaster, Deluxe
    catalog_number: Optional[str] = None
    barcode: Optional[str] = None
    genre: List[str] = field(default_factory=list)
    description: str = ""
    cover_url: str = ""
    
    # 抓轨与合规信息
    has_log: bool = False
    log_score: int = 0
    has_cue: bool = False
    is_split_track: bool = True     # True: 分轨, False: 整轨
    
    # 文件组织
    local_path: str = ""
    total_size: int = 0
    tracks: List[AudioTrackInfo] = field(default_factory=list)
    
    # 溯源与防环属性
    origin_site: str = ""
    origin_torrent_id: str = ""
    origin_info_hash: str = ""
    content_path: str = ""

    @property
    def info_hash(self) -> str:
        return self.origin_info_hash or self.origin_torrent_id
    
    def calculate_content_fingerprint(self) -> str:
        """生成内容结构指纹 (Content Fingerprint)
        格式：artist_album_year_medium_format_standard
        """
        raw = f"{self.artist.strip().lower()}|{self.album.strip().lower()}|{self.year.strip()}|{self.medium.strip().lower()}|{self.format.strip().lower()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def calculate_stream_fingerprint(self) -> str:
        """生成物理音轨特征指纹"""
        if not self.tracks:
            return ""
        # 提取各音轨的特征采样组合
        stream_keys = [t.stream_md5 for t in self.tracks if t.stream_md5]
        if not stream_keys:
            # 回退到音轨文件名与大小组合
            stream_keys = [f"{t.track_number}_{t.file_size}" for t in self.tracks]
        raw = "|".join(stream_keys)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
