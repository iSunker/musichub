# -*- coding: utf-8 -*-
"""慕雪阁 (MuXueGe) 站点适配器

遵循老版实测契约 (muxuege_site.py)：
1. 音乐分类: 408
2. 音频编码 audiocodec_sel[4]: APE=2, DTS=3, MP3=4, OGG=5, AAC=6, Other=7
   ★ 慕雪阁无独立 FLAC / WAV 选项，FLAC / WAV 统一映射为 Other(7)
3. 年代 processing_sel[4]: 90年代之前=32, 90年代=3, 2000=4 ... 2026=30, 2027=31
4. 媒介 medium_sel[4]: WEB-DL=10, 其它填 10 或留空
5. 制作组 team_sel[4]: Other=52
6. 视频编码 codec_sel[4]: Other=5
★ 慕雪阁页面无 uplver 匿名复选框，使用登录账号具名发布
"""

from typing import Dict, Any, Tuple
from musichub.adapters.nexusphp_base import NexusPHPBaseAdapter
from musichub.core.models import CanonicalMusicItem


class MuXueGeAdapter(NexusPHPBaseAdapter):
    def __init__(self, cookie_client=None, cover_service=None):
        super().__init__(
            site_name="MuXueGe",
            domain="muxuege.org",
            base_url="https://pt.muxuege.org",
            cookie_client=cookie_client,
            cover_service=cover_service
        )

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        if not item.tracks:
            return False, "MuXueGe requires at least one track"
        return True, "MuXueGe check passed"

    def _map_year(self, year_str: str) -> str:
        """年份 processing_sel[4] 映射 (实测自 muxuege_site.py)"""
        try:
            y = int(year_str)
            if y < 1990:
                return "32"
            elif 1990 <= y < 2000:
                return "3"
            elif 2000 <= y <= 2027:
                return str(y - 2000 + 4)
            else:
                return "30"
        except (ValueError, TypeError):
            return "30"

    def _map_codec(self, fmt: str) -> str:
        """音频编码 audiocodec_sel[4]: APE=2, MP3=4, FLAC/WAV -> Other(7)"""
        f = (fmt or "").upper()
        if "APE" in f:
            return "2"
        elif "MP3" in f:
            return "4"
        return "7"  # 包含 FLAC 与 WAV

    def build_form_fields(self, item: CanonicalMusicItem) -> Dict[str, Any]:
        main_title, sub_title = self.format_title(item)

        fields = {
            "name": main_title,
            "small_descr": sub_title,
            "type": "408",                                   # 慕雪阁音乐分类为 408
            "processing_sel[4]": self._map_year(item.year),
            "audiocodec_sel[4]": self._map_codec(item.format),
            "medium_sel[4]": "10",                           # 默认 10 (WEB-DL/Other)
            "codec_sel[4]": "5",                             # Other
            "team_sel[4]": "52",                             # Other 制作组
            "descr": self.build_body(item),
            # 无 uplver 复选框
        }
        return fields
