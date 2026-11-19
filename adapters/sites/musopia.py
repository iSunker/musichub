# -*- coding: utf-8 -*-
"""Musopia (乌托邦) 站点适配器

遵循老版实测契约 (musopia.py / descr.py)：
1. 音乐分类: 401
2. 媒介 medium_sel[4]: CD=8, SACD=9, DVD=10, VCD=11, OTHER=12
3. 音频编码 audiocodec_sel[4]: FLAC=1, APE=2, DTS=3, MP3=4, OGG=5, AAC=6, OTHER=7
4. 制作组 team_sel[4]: MUSOPIA=6, OTHER=5, HDS=1, CHD=2, MYSILU=3, WIKI=4
5. 标签体系 tags[4][]: 必须严格过滤转发黑名单（禁转、首发、友站标记等）
"""

from typing import Dict, Any, Tuple
from musichub.adapters.nexusphp_base import NexusPHPBaseAdapter
from musichub.core.models import CanonicalMusicItem


class MusopiaAdapter(NexusPHPBaseAdapter):
    def __init__(self, cookie_client=None, cover_service=None):
        super().__init__(
            site_name="Musopia",
            domain="musopia.vip",
            base_url="https://www.musopia.vip",
            cookie_client=cookie_client,
            cover_service=cover_service
        )

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        # 乌托邦对音频音轨完整性要求严格
        if not item.tracks:
            return False, "Musopia requires valid tracks"
        return True, "Musopia pre-publish check passed"

    def _map_medium(self, med: str) -> str:
        """乌托邦媒介映射: CD:8, SACD:9, DVD:10, VCD:11, OTHER:12"""
        m = (med or "").upper()
        if "CD" in m and "SACD" not in m:
            return "8"
        elif "SACD" in m:
            return "9"
        elif "DVD" in m:
            return "10"
        elif "VCD" in m:
            return "11"
        return "12"

    def _map_codec(self, fmt: str) -> str:
        """乌托邦编码映射: FLAC:1, APE:2, DTS:3, MP3:4, OGG:5, AAC:6, OTHER:7"""
        f = (fmt or "").upper()
        if "FLAC" in f:
            return "1"
        elif "APE" in f:
            return "2"
        elif "DTS" in f:
            return "3"
        elif "MP3" in f:
            return "4"
        elif "OGG" in f:
            return "5"
        elif "AAC" in f:
            return "6"
        return "7"

    def build_form_fields(self, item: CanonicalMusicItem) -> Dict[str, Any]:
        main_title, sub_title = self.format_title(item)

        fields = {
            "name": main_title,
            "small_descr": sub_title,
            "type": "408",                                      # 音乐分类（408 为 Musopia 正确音乐区分类）
            "nfoaction": "keep",
            "medium_sel[4]": self._map_medium(item.medium),
            "audiocodec_sel[4]": self._map_codec(item.format),
            "team_sel[4]": "5",                                 # Other
            "descr": self.build_body(item),
            "uplver": "yes",                                    # 匿名发布
            "custom_fields[4][1]": (item.artist or "").strip(),
            "custom_fields[4][2]": (item.year or "").strip(),
            "custom_fields[4][3]": "",                          # 抓轨日志
            "custom_fields[4][4]": "",                          # 唱片厂牌
            "custom_fields[4][5]": "",                          # 抓轨日志得分
        }
        return fields
