# -*- coding: utf-8 -*-
"""UHDDolby (杜比) 站点适配器

遵循 NexusPHP 实测契约：
1. 音乐分类: type=408 (HQ Audio音乐)
2. 媒介 medium_sel: CD=9, WEB-DL=6, Encode=10, Remux=3, Other=11 (注意：字段无 [4] 下标数组)
3. 音频编码 audiocodec_sel: FLAC=7, APE=8, WAV=9, MP3=10, M4A=11, Other=12
4. 视频编码 codec_sel: Other=7
5. 分辨率 standard_sel: Others=5
6. 制作组 team_sel: Other=8
7. uplver="yes": 匿名发布
★ 直连光猫，IPv4 优先，脱钩外部环境变量代理 (trust_env=False)
"""

from typing import Dict, Any, Tuple, List
import re
from musichub.adapters.nexusphp_base import NexusPHPBaseAdapter
from musichub.core.models import CanonicalMusicItem
from musichub.adapters.tag_matcher import TagMatcher


class UHDDolbyAdapter(NexusPHPBaseAdapter):
    def __init__(self, cookie_client=None, cover_service=None):
        super().__init__(
            site_name="UHDDolby",
            domain="uhddolby.com",
            base_url="https://www.uhddolby.com",
            cookie_client=cookie_client,
            cover_service=cover_service
        )

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        if not item.tracks:
            return False, "UHDDolby requires at least one track"
        return True, "UHDDolby check passed"

    def _map_medium(self, med: str) -> str:
        """媒介 medium_sel: CD=9, WEB-DL=6, Other=11"""
        m = (med or "").upper()
        if "WEB" in m:
            return "6"
        elif "CD" in m:
            return "9"
        return "11"

    def _map_codec(self, fmt: str) -> str:
        """音频编码 audiocodec_sel: FLAC=7, APE=8, WAV=9, MP3=10, Other=12"""
        f = (fmt or "").upper()
        if "FLAC" in f:
            return "7"
        elif "APE" in f:
            return "8"
        elif "WAV" in f:
            return "9"
        elif "MP3" in f:
            return "10"
        return "12"

    def _detect_tags(self, item: CanonicalMusicItem) -> List[str]:
        """UHDDolby 音乐标签识别：委托 TagMatcher 匹配 gy(国语), yy(粤语), ja(日语), ko(韩语), zz(中字)"""
        return TagMatcher.match_tags_for_site("uhddolby", item)

    def build_form_fields(self, item: CanonicalMusicItem) -> Dict[str, Any]:
        main_title, sub_title = self.format_title(item)

        fields = {
            "name": main_title,
            "small_descr": sub_title,
            "type": "408",                                   # 音乐分类实测 408
            "medium_sel": self._map_medium(item.medium),
            "audiocodec_sel": self._map_codec(item.format),
            "codec_sel": "7",                                # Other 视频编码
            "standard_sel": "5",                             # Others 分辨率
            "team_sel": "8",                                 # Other 制作组
            "descr": self.build_body(item),
            "uplver": "yes",                                 # 匿名发布
        }
        TagMatcher.inject_form_tags("uhddolby", item, fields)
        return fields
