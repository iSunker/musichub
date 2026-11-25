# -*- coding: utf-8 -*-
"""NanyangPT (南洋PT) 站点适配器

遵循 NYPT Project (NexusPHP 变体) 实测契约 (https://nanyangpt.com)：
1. 音乐分类: type=407 (音乐)
2. 音乐专属动态表单 (ajaxdealing.php?action=changetorrenttype&type=407):
   - music_time: 年份 (支持 YYYY 或 YYYY-MM-DD)
   - music_type: 单曲 / 专辑 / 合集 / MV / 演唱会 / 音乐会 / 戏剧 / 其它
   - music_author: 作者 (多人填 群星)
   - music_name: 名称
   - music_form: FLAC / APE / WAV / MP3 / AAC / 其它
   - music_qual: 无损 / 320Kbps / 256Kbps / 192Kbps / VBR / 其它
3. 无 tags 控件，无独立封面字段 (has_cover_field=False)
4. 匿名发布: uplver=yes
★ 直连光猫，IPv4 优先，脱钩外部环境变量代理 (trust_env=False)
"""

import re
from typing import Dict, Any, Tuple, Optional
from musichub.adapters.nexusphp_base import NexusPHPBaseAdapter
from musichub.core.models import CanonicalMusicItem


class NanyangPTAdapter(NexusPHPBaseAdapter):
    def __init__(self, cookie_client=None, cover_service=None):
        super().__init__(
            site_name="NanyangPT",
            domain="nanyangpt.com",
            base_url="https://nanyangpt.com",
            cookie_client=cookie_client,
            cover_service=cover_service,
        )

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        if not item.tracks:
            return False, "NanyangPT requires at least one track"
        return True, "NanyangPT check passed"

    def _map_music_form(self, fmt: str) -> str:
        """格式 music_form: FLAC / APE / WAV / MP3 / AAC / 其它"""
        f = (fmt or "").upper()
        for cand in ("FLAC", "APE", "WAV", "MP3", "AAC"):
            if cand in f:
                return cand
        return "其它"

    def _map_music_qual(self, fmt: str, standard: str = "") -> str:
        """质量 music_qual: 无损 / 320Kbps / 256Kbps / 192Kbps / VBR / 其它"""
        f = (fmt or "").upper()
        s = (standard or "").upper()
        combined = f"{f} {s}"
        if any(k in combined for k in ("FLAC", "APE", "WAV", "DSD", "DSF", "DFF", "ALAC", "AIFF", "LOSSLESS", "24BIT", "16BIT")):
            return "无损"
        if "320" in combined:
            return "320Kbps"
        if "256" in combined:
            return "256Kbps"
        if "192" in combined:
            return "192Kbps"
        if "VBR" in combined:
            return "VBR"
        return "其它"

    def _map_music_type(self, item: CanonicalMusicItem) -> str:
        """类型 music_type: 专辑 / 合集 / 单曲 等"""
        text = f"{item.edition or ''} {item.title or ''} {item.album or ''}".lower()
        if any(k in text for k in ("boxset", "box set", "discography", "anthology", "合集")):
            return "合集"
        return "专辑"

    def _format_music_time(self, year: int) -> str:
        """年份 music_time: 优先提取 4 位年份，缺失时兜底 2000"""
        if year and 1900 <= int(year) <= 2100:
            return str(int(year))
        return "2000"

    def _format_music_author(self, artist: str) -> str:
        a = re.sub(r"\s+", " ", (artist or "").strip())
        if not a or a.lower() in ("various artists", "va", "various"):
            return "群星"
        return a

    def build_form_fields(self, item: CanonicalMusicItem) -> Dict[str, Any]:
        main_title, sub_title = self.format_title(item)
        music_time = self._format_music_time(item.year)
        music_type = self._map_music_type(item)
        music_author = self._format_music_author(item.artist)
        music_name = (item.album or "").strip() or main_title
        music_form = self._map_music_form(item.format)
        music_qual = self._map_music_qual(item.format, item.standard)
        # 对齐南洋PT common.js checkForm() 拼接规则
        nypt_name = f"[{music_time}][{music_type}][{music_author}][{music_name}][{music_form}][{music_qual}]"

        fields = {
            "cite_site": "",
            "type": "407",                                   # 407: 音乐
            "name": nypt_name,
            "small_descr": sub_title or music_name,
            "url": "",
            "dburl": "",
            "color": "0",
            "font": "0",
            "size": "0",
            "music_time": music_time,
            "music_type": music_type,
            "music_author": music_author,
            "music_name": music_name,
            "music_form": music_form,
            "music_qual": music_qual,
            "descr": self.build_body(item),
            "uplver": "yes",                                 # 匿名发布
        }
        return fields

    def publish(self, item: CanonicalMusicItem, torrent_path: str) -> Tuple[bool, str, Optional[bytes]]:
        """NYPT takeupload.php 强校验 .torrent 顶层必须含 announce 键（否则报「目录缺少值」）"""
        try:
            import torf
            t = torf.Torrent.read(torrent_path)
            if not t.trackers:
                t.trackers = ["https://nanyangpt.com/announce.php"]
                t.write(torrent_path, overwrite=True)
        except Exception as e:
            raise RuntimeError(f"Failed to inject announce into {torrent_path}: {e}")
        return super().publish(item, torrent_path)
