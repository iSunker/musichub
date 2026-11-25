# -*- coding: utf-8 -*-
"""HDTime (高清时间) 站点适配器

遵循 NexusPHP 实测契约：
1. 音乐分类: type=408 (音乐)
2. 媒介 medium_sel[4]: CD=8, Track=9, WEB-DL=10, Other=10
3. 音频编码 audiocodec_sel[4]: FLAC=1, APE=2, MP3=4, OGG=5, AAC=6, Other=7 (WAV 归入 Other)
4. 视频编码 codec_sel[4]: Other=5
5. 制作组 team_sel[4]: Other=5
6. uplver="yes": 匿名发布
★ 直连光猫，IPv4 优先，脱钩外部环境变量代理 (trust_env=False)
"""

from typing import Dict, Any, Tuple
from musichub.adapters.nexusphp_base import NexusPHPBaseAdapter
from musichub.core.models import CanonicalMusicItem
from musichub.adapters.tag_matcher import TagMatcher


class HDTimeAdapter(NexusPHPBaseAdapter):
    def __init__(self, cookie_client=None, cover_service=None):
        super().__init__(
            site_name="HDTime",
            domain="hdtime.org",
            base_url="https://hdtime.org",
            cookie_client=cookie_client,
            cover_service=cover_service
        )

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        if not item.tracks:
            return False, "HDTime requires at least one track"
        return True, "HDTime check passed"

    def _map_medium(self, med: str) -> str:
        """媒介 medium_sel[4]: CD=8, Track=9, WEB-DL=10"""
        m = (med or "").upper()
        if "WEB" in m:
            return "10"
        elif "CD" in m:
            return "8"
        elif "TRACK" in m:
            return "9"
        return "8"

    def _map_codec(self, fmt: str) -> str:
        """音频编码 audiocodec_sel[4]: FLAC=1, APE=2, MP3=4, OGG=5, AAC=6, Other=7"""
        f = (fmt or "").upper()
        if "FLAC" in f:
            return "1"
        elif "APE" in f:
            return "2"
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
            "type": "408",                                   # 音乐分类实测 408
            "medium_sel[4]": self._map_medium(item.medium),
            "audiocodec_sel[4]": self._map_codec(item.format),
            "codec_sel[4]": "5",                             # Other 视频编码
            "team_sel[4]": "5",                              # Other 制作组
            "descr": self.build_body(item),
            "uplver": "yes",                                 # 匿名发布
        }
        TagMatcher.inject_form_tags("hdtime", item, fields)
        return fields
