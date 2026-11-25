# -*- coding: utf-8 -*-
"""PTerClub (猫站) 站点适配器

遵循老版实测契约 (pter_site.py)：
1. 音乐分类: 406
2. 媒介 source_sel: FLAC=8, WAV=9, WEB-DL=5, ISO=10, Other=15（无独立音频编码下拉，由 source_sel 区分）
3. 地区 team_sel: 大陆=1, 香港=2, 台湾=3, 欧美=4, 韩国=5, 日本=6, 印度=7, 其它=8
4. 转载铁律：严禁勾选禁转 (jinzhuan)、原创 (pr)、自购 (bim)
"""

from typing import Dict, Any, Tuple
from musichub.adapters.nexusphp_base import NexusPHPBaseAdapter
from musichub.core.models import CanonicalMusicItem
from musichub.adapters.tag_matcher import TagMatcher


class PTerClubAdapter(NexusPHPBaseAdapter):
    def __init__(self, cookie_client=None, cover_service=None):
        super().__init__(
            site_name="PTerClub",
            domain="pterclub.net",
            base_url="https://pterclub.net",
            cookie_client=cookie_client,
            cover_service=cover_service
        )

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        if not item.tracks:
            return False, "PTer requires at least one track"
        return True, "PTer check passed"

    def _map_source(self, item: CanonicalMusicItem) -> str:
        """猫站 source_sel 选项（音乐）：FLAC:8, WAV:9, WEB-DL:5, ISO:10, Other:15"""
        fmt = (item.format or "").upper()
        med = (item.medium or "").upper()
        if "FLAC" in fmt:
            return "8"
        elif "WAV" in fmt:
            return "9"
        elif "ISO" in fmt or "SACD" in med:
            return "10"
        elif "WEB" in med or "WEB-DL" in fmt:
            return "5"
        return "15"

    def _map_region_team(self, item: CanonicalMusicItem) -> str:
        """猫站 team_sel（产地）：大陆=1, 香港=2, 台湾=3, 欧美=4, 韩国=5, 日本=6, 印度=7, 其它=8"""
        combined = f"{item.artist} {' '.join(item.genre)} {item.description}".lower()
        if any(k in combined for k in ("大陆", "国语", "内地", "mainland")):
            return "1"
        elif any(k in combined for k in ("香港", "hk", "hkg")):
            return "2"
        elif any(k in combined for k in ("台湾", "港台", "tw", "twn")):
            return "3"
        elif any(k in combined for k in ("欧美", "western", "us", "uk")):
            return "4"
        elif any(k in combined for k in ("韩国", "korea", "kor")):
            return "5"
        elif any(k in combined for k in ("日本", "japan", "jpn", "日韩")):
            return "6"
        return "8"

    def format_title(self, item: CanonicalMusicItem) -> Tuple[str, str]:
        """猫站 (PTerClub) 严格遵循 Wiki 上传规则：艺术家 - 专辑 发行年份 - 格式 位深 采样率 [- 制作组]"""
        import musichub.adapters.descr_core as descr_core
        task = {
            "formatted_title": item.origin_title or item.title,
            "torrent_name": item.origin_title or item.title,
            "artist": item.artist,
            "album": item.album,
            "year": item.year,
            "format": item.format,
            "medium": item.medium,
            "standard": item.standard,
            "small_descr": item.edition or "",
            "tracklist": "\n".join(f"{t.track_number:02d}. {t.title}" for t in item.tracks) if item.tracks else "",
            "origin_site": item.origin_site or "",
            "origin_tid": item.origin_torrent_id or "",
            "opencd_tid": item.origin_torrent_id if item.origin_site == "opencd" else "",
        }
        main_title = descr_core.normalize_music_title(task)
        small_descr = descr_core.build_music_small_descr(task)
        return main_title, small_descr

    def build_form_fields(self, item: CanonicalMusicItem) -> Dict[str, Any]:
        main_title, sub_title = self.format_title(item)

        fields = {
            "name": main_title,
            "small_descr": sub_title,
            "type": "406",                                    # 音乐分类
            "source_sel": self._map_source(item),
            "team_sel": self._map_region_team(item),
            "descr": self.build_body(item),
            "uplver": "yes",                                  # 匿名发布
        }
        TagMatcher.inject_form_tags("pterclub", item, fields)
        return fields
