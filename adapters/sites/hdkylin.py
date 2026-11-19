# -*- coding: utf-8 -*-
"""麒麟 (HDKylin) 站点适配器

遵循老版实测契约 (hdkylin_site.py)：
1. 官方现行域名: www.hdkyl.in (原 hdkylin.top)
2. 音乐分类: 408 (HQ Audio/音乐)
3. 年份 processing_sel[4]: 2026=12 ... 2016=8, Earlier=9
4. 媒介 medium_sel[4]: CD=33, Track=32, WEB-DL=31, Other=34
5. 音频编码 audiocodec_sel[4]: FLAC=1, WAV=13, APE=12, MP3=4
6. 视频编码/分辨率: Other (codec_sel[4]=5, standard_sel[4]=9)
7. 地区 source_sel[4]: CN=15, Other=18
8. 制作组 team_sel[4]: Other=5

★★ 核心防拒发护栏：
麒麟站内审查脚本 HDKylin-Torrent-Assistant 规则：
`/[^\x00-\xff]+/.test(title)` 如果主标题包含非 Latin-1 字符（中文/全角/假名等）将直接判审核不通过！
因此 pre_publish_check 必须硬拦截主标题含有中文的情况。
"""

import re
from typing import Dict, Any, Tuple
from musichub.adapters.nexusphp_base import NexusPHPBaseAdapter
from musichub.core.models import CanonicalMusicItem


class HDKylinAdapter(NexusPHPBaseAdapter):
    def __init__(self, cookie_client=None, cover_service=None):
        super().__init__(
            site_name="HDKylin",
            domain="hdkyl.in",
            base_url="https://www.hdkyl.in",
            cookie_client=cookie_client,
            cover_service=cover_service
        )

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        """【麒麟专属护栏】主标题必须纯 Latin-1，否则线上审核脚本必拦"""
        main_title, _ = self.format_title(item)
        if re.search(r"[^\x00-\xff]", main_title):
            return False, f"HDKylin auto-audit rejects non-Latin-1 characters in main title: '{main_title}'"
        return True, "HDKylin pre-publish check passed"

    def format_title(self, item: CanonicalMusicItem) -> Tuple[str, str]:
        # 麒麟标题格式：艺术家 - 专辑 年份 编码
        import musichub.adapters.descr_core as descr_core
        task = {
            "formatted_title": item.title,
            "torrent_name": item.title,
            "artist": item.artist,
            "album": item.album,
            "year": item.year,
            "format": item.format,
            "medium": item.medium,
            "standard": item.standard,
            "small_descr": item.edition or "",
        }
        main_title = descr_core.build_kylin_title(task)
        sub_title = descr_core.build_music_small_descr(task)
        return main_title, sub_title

    def _map_year(self, year_str: str) -> str:
        """年份 processing_sel[4] 映射 (实测自 hdkylin_site.py)"""
        try:
            y = int(year_str)
            if y >= 2026:
                return "12"
            elif y >= 2016:
                mapping = {
                    2025: "11", 2024: "10", 2023: "2", 2022: "3",
                    2021: "4", 2020: "5", 2019: "6", 2018: "7", 2017: "1", 2016: "8"
                }
                return mapping.get(y, "9")
            else:
                return "9"  # Earlier
        except (ValueError, TypeError):
            return "9"

    def build_form_fields(self, item: CanonicalMusicItem) -> Dict[str, Any]:
        main_title, sub_title = self.format_title(item)

        # 媒介 medium_sel[4]: CD=33, Track=32, WEB-DL=31, Other=34
        med_upper = (item.medium or "").upper()
        if "CD" in med_upper:
            medium_val = "33"
        elif "WEB" in med_upper:
            medium_val = "31"
        else:
            medium_val = "34"

        # 音频编码 audiocodec_sel[4]: FLAC=1, WAV=13, APE=12, MP3=4
        fmt_upper = (item.format or "").upper()
        if fmt_upper == "FLAC":
            codec_val = "1"
        elif fmt_upper == "WAV":
            codec_val = "13"
        elif fmt_upper == "APE":
            codec_val = "12"
        else:
            codec_val = "4"

        fields = {
            "name": main_title,
            "small_descr": sub_title,
            "type": "408",                         # HQ Audio/音乐
            "processing_sel[4]": self._map_year(item.year),
            "medium_sel[4]": medium_val,
            "codec_sel[4]": "5",                   # Other
            "audiocodec_sel[4]": codec_val,
            "standard_sel[4]": "9",                # Other
            "source_sel[4]": "18",                 # Other地区
            "team_sel[4]": "5",                    # Other制作组
            "descr": self.build_body(item),
            "uplver": "yes",                       # 匿名发布
        }
        return fields
