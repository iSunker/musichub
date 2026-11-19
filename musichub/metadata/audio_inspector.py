# -*- coding: utf-8 -*-
"""MusicHub 3.0 音频探测器与 CUE 自愈模块

负责：
1. 深入本地音频（FLAC, APE, WAV, DSD）提取 Header 元数据
2. 多编码容错与乱码探测修复（UTF-8, GB18030, Big5, Shift-JIS）
3. CUE 结构验证与内部 FILE 路径自愈更正
"""

import os
import re
from typing import Dict, List, Optional, Tuple
from musichub.core.models import AudioTrackInfo, CanonicalMusicItem


class AudioInspector:
    AUDIO_EXTENSIONS = {".flac", ".ape", ".wav", ".dsf", ".dff", ".m4a"}

    @classmethod
    def parse_name_metadata(cls, text: str) -> Dict[str, str]:
        """从种子名或目录名中解析结构化音乐元数据 (Year, Artist, Album, Medium, Format, Standard, Edition)
        例如:
        - "[2003] Jay Chou - Ye Hui Mei [FLAC] [CD]"
        - "Adele - 30 (2021) [FLAC 24-96] [WEB-DL] [Deluxe]"
        """
        result: Dict[str, str] = {
            "year": "",
            "artist": "",
            "album": "",
            "medium": "CD",
            "format": "FLAC",
            "standard": "Lossless",
            "edition": ""
        }
        if not text:
            return result

        clean_text = text.strip()

        # 1. 提取年份 (19xx 或 20xx)
        year_match = re.search(r"\b(19\d{2}|20\d{2})\b", clean_text)
        if year_match:
            result["year"] = year_match.group(1)

        # 2. 提取介质 (Medium)
        med_pattern = re.search(r"\b(SACD|Vinyl|LP|WEB-DL|WEB|Blu-ray|BD|DVD-Audio|CD|Cassette)\b", clean_text, re.IGNORECASE)
        if med_pattern:
            raw_med = med_pattern.group(1).upper()
            if raw_med in ("WEB", "WEB-DL"):
                result["medium"] = "WEB-DL"
            elif raw_med in ("VINYL", "LP"):
                result["medium"] = "Vinyl"
            elif "SACD" in raw_med:
                result["medium"] = "SACD"
            elif "BLU-RAY" in raw_med or raw_med == "BD":
                result["medium"] = "Blu-ray Audio"
            else:
                result["medium"] = "CD"

        # 3. 提取音频编码 (Format)
        fmt_match = re.search(r"\b(FLAC|APE|WAV|DSD|DSF|DFF|MP3|AAC|ALAC)\b", clean_text, re.IGNORECASE)
        if fmt_match:
            result["format"] = fmt_match.group(1).upper()

        # 4. 提取规格 (Standard)
        if re.search(r"\b(24bit|24-bit|24/\d+|24-\d+|Hi-Res|DSD\d+|88\.2kHz|96kHz|192kHz)\b", clean_text, re.IGNORECASE):
            result["standard"] = "Hi-Res"

        # 5. 提取版本 (Edition)
        ed_match = re.search(r"\b(Remaster(?:ed)?|Deluxe(?: Edition)?|First Edition|Limited(?: Edition)?|Anniversary|Boxset)\b", clean_text, re.IGNORECASE)
        if ed_match:
            result["edition"] = ed_match.group(1).strip()

        # 6. 提取 Artist 与 Album (尝试 Artist - Album 格式)
        # 去除方括号标签与圆括号标签后分析主体
        stripped = re.sub(r"\[.*?\]|\(.*?\)", " ", clean_text).strip()
        parts = re.split(r"\s+-\s+", stripped)
        if len(parts) >= 2:
            result["artist"] = parts[0].strip()
            raw_album = " - ".join(parts[1:]).strip()
        elif stripped:
            raw_album = stripped
        else:
            raw_album = ""

        # 清洗 raw_album 中可能残留的尾部属性与标记（如 "1998", "FLAC", "分轨", "整轨", "CD", "WEB-DL" 等）
        cleaned_album = re.sub(
            r"\b(19\d{2}|20\d{2})\b|\b(FLAC|APE|WAV|DSD|MP3|CD|WEB-DL|WEB|SACD)\b|(?:分轨|整轨|分軌|整軌)",
            "",
            raw_album,
            flags=re.IGNORECASE
        )
        cleaned_album = re.sub(r"\s+-\s*$", "", cleaned_album)
        cleaned_album = re.sub(r"^\s*-\s+", "", cleaned_album)
        cleaned_album = re.sub(r"\s+", " ", cleaned_album).strip()

        result["album"] = cleaned_album if cleaned_album else raw_album

        return result

    @classmethod
    def calculate_audio_stream_hash(cls, file_path: str) -> str:
        """快速提取物理音频流的核心指纹（对首部、中部、尾部进行分块哈希采样，抵御标签修改带来的误判）"""
        try:
            import hashlib
            sz = os.path.getsize(file_path)
            if sz == 0:
                return ""
            # 若文件小于 128KB 则全量计算
            if sz <= 131072:
                with open(file_path, "rb") as f:
                    return hashlib.md5(f.read()).hexdigest()
            # 针对大音频文件取头、中、尾三段各 64KB 采样
            hasher = hashlib.md5()
            chunk_size = 65536
            with open(file_path, "rb") as f:
                # 头部
                hasher.update(f.read(chunk_size))
                # 中部
                f.seek(sz // 2)
                hasher.update(f.read(chunk_size))
                # 尾部
                f.seek(max(0, sz - chunk_size))
                hasher.update(f.read(chunk_size))
            return hasher.hexdigest()
        except Exception:
            return ""

    @classmethod
    def scan_directory(cls, dir_path: str) -> Tuple[List[AudioTrackInfo], Dict[str, any]]:
        """扫描本地音乐目录，提取音轨列表与通用属性（支持 CD1/CD2/Disc 1 等多碟目录）"""
        if not os.path.exists(dir_path):
            raise FileNotFoundError(f"Directory not found: {dir_path}")

        tracks: List[AudioTrackInfo] = []
        has_cue = False
        has_log = False
        log_score = 0
        cue_files = []
        audio_files = []

        # 遍历搜集，按子目录排序确保 Disc 1, Disc 2 顺序正确
        for root, _, files in os.walk(dir_path):
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                full_path = os.path.join(root, file)
                if ext in cls.AUDIO_EXTENSIONS:
                    audio_files.append(full_path)
                elif ext == ".cue":
                    has_cue = True
                    cue_files.append(full_path)
                elif ext == ".log":
                    has_log = True
                    log_score = max(log_score, cls._extract_log_score(full_path))

        # 自然排序：路径层级与文件名
        def _natural_sort_key(p: str):
            rel = os.path.relpath(p, dir_path)
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', rel)]

        audio_files.sort(key=_natural_sort_key)

        # 尝试修复/自愈 CUE
        for cue_path in cue_files:
            cls.heal_cue_file(cue_path, audio_files)

        # 提取音轨元数据
        for idx, af in enumerate(audio_files, start=1):
            info = cls.inspect_file(af, default_track_num=idx)
            tracks.append(info)

        summary = {
            "has_cue": has_cue,
            "has_log": has_log,
            "log_score": log_score,
            "is_split_track": len(audio_files) > 1,
            "total_size": sum(t.file_size for t in tracks),
        }
        return tracks, summary

    @classmethod
    def inspect_file(cls, file_path: str, default_track_num: int = 1) -> AudioTrackInfo:
        """分析单个音频文件 Header"""
        file_size = os.path.getsize(file_path)
        ext = os.path.splitext(file_path)[1].lower()
        title = os.path.splitext(os.path.basename(file_path))[0]
        artist = "Unknown Artist"
        sample_rate = 44100
        bit_depth = 16
        duration = 0.0

        try:
            import mutagen
            audio = mutagen.File(file_path)
            if audio is not None:
                if hasattr(audio, "info"):
                    sample_rate = getattr(audio.info, "sample_rate", sample_rate)
                    bit_depth = getattr(audio.info, "bits_per_sample", bit_depth)
                    duration = getattr(audio.info, "length", duration)
                
                # 尝试多编码读取 tags
                if audio.tags:
                    title_tag = audio.tags.get("title") or audio.tags.get("TITLE")
                    artist_tag = audio.tags.get("artist") or audio.tags.get("ARTIST")
                    if title_tag:
                        title = cls._decode_tag(title_tag[0] if isinstance(title_tag, list) else title_tag)
                    if artist_tag:
                        artist = cls._decode_tag(artist_tag[0] if isinstance(artist_tag, list) else artist_tag)
        except Exception:
            # mutagen 未安装或解析异常时降级读取
            pass

        return AudioTrackInfo(
            track_number=default_track_num,
            title=title,
            artist=artist,
            duration_seconds=duration,
            bit_depth=bit_depth,
            sample_rate=sample_rate,
            channels=2,
            codec=ext.lstrip(".").upper(),
            file_path=file_path,
            file_size=file_size,
            stream_md5=cls.calculate_audio_stream_hash(file_path),
        )

    @classmethod
    def _decode_tag(cls, raw_val: any) -> str:
        """对可能存在乱码的 tag 进行编码探测与纠正"""
        if not isinstance(raw_val, str):
            return str(raw_val)
        # 检测常见乱码特征（如 latin1 错当 utf-8 / gbk）
        try:
            # 常见：latin1 被错误解码，尝试逆转还原
            latin_bytes = raw_val.encode("latin1")
            for enc in ("utf-8", "gb18030", "big5", "shift_jis"):
                try:
                    return latin_bytes.decode(enc)
                except UnicodeDecodeError:
                    continue
        except Exception:
            pass
        return raw_val

    @classmethod
    def heal_cue_file(cls, cue_path: str, audio_files: List[str]) -> bool:
        """CUE 自愈更正：确保 FILE "..." 指向实际存在的音频文件名"""
        if not os.path.exists(cue_path) or not audio_files:
            return False

        try:
            with open(cue_path, "rb") as f:
                content_bytes = f.read()

            encoding = "utf-8"
            for enc in ("utf-8", "gb18030", "big5", "shift_jis"):
                try:
                    content_str = content_bytes.decode(enc)
                    encoding = enc
                    break
                except UnicodeDecodeError:
                    continue
            else:
                content_str = content_bytes.decode("latin1", errors="ignore")

            # 寻找 FILE 行: FILE "filename" WAVE/MP3/FLAC
            file_pattern = re.compile(r'FILE\s+["\']([^"\']+)["\']\s+(\w+)', re.IGNORECASE)
            matches = file_pattern.findall(content_str)
            if not matches:
                return False

            changed = False
            base_dir = os.path.dirname(cue_path)
            for old_file, file_type in matches:
                full_target = os.path.join(base_dir, old_file)
                if not os.path.exists(full_target):
                    # 匹配策略 1：同目录下的同扩展名音频文件
                    old_base, old_ext = os.path.splitext(old_file)
                    matched_actual = None

                    # 优先看同名仅扩展名不同的（例如原为 .wav 实际被转为 .flac）
                    for af in audio_files:
                        if os.path.dirname(af) == base_dir:
                            af_name = os.path.basename(af)
                            af_base, af_ext = os.path.splitext(af_name)
                            if af_base.lower() == old_base.lower():
                                matched_actual = af_name
                                break

                    # 若无，则按同目录下唯一的音频文件自愈（整轨单音频场景）
                    if not matched_actual:
                        same_dir_audios = [os.path.basename(af) for af in audio_files if os.path.dirname(af) == base_dir]
                        if len(same_dir_audios) == 1:
                            matched_actual = same_dir_audios[0]

                    if matched_actual and matched_actual != old_file:
                        # 替换该行，并根据新文件后缀更新类型标识（如 FLAC/WAVE）
                        new_type = "WAVE" if matched_actual.lower().endswith(".wav") else "FLAC"
                        content_str = re.sub(
                            r'FILE\s+["\']' + re.escape(old_file) + r'["\']\s+\w+',
                            f'FILE "{matched_actual}" {new_type}',
                            content_str,
                            flags=re.IGNORECASE
                        )
                        changed = True

            if changed:
                with open(cue_path, "w", encoding=encoding) as f:
                    f.write(content_str)
                return True
        except Exception:
            return False
        return False

    @classmethod
    def _extract_log_score(cls, log_path: str) -> int:
        """从 EAC/XLD 抓轨日志中提取质量评分"""
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            match = re.search(r"EAC\s+extraction\s+log\s+file.*", content, re.I)
            score_match = re.search(r"Log\s+checksum\s+([A-Fa-f0-9]+)|Exact\s+Audio\s+Copy", content, re.I)
            if "100%" in content:
                return 100
            if score_match:
                return 99
        except Exception:
            pass
        return 0
