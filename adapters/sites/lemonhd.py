# -*- coding: utf-8 -*-
"""LemonHD (柠檬) 站点适配器

遵循 NexusPHP 实测契约：
1. 音乐分类: type=402 (Muisc(音乐))
2. 媒介 medium_sel[4]: CD=9, WEB-DL=4, Other=10
3. 音频编码 audiocodec_sel[4]: FLAC=10, WAV=11, APE=12, Other=13
4. 视频编码 codec_sel[4]: Other=5
5. 分辨率 standard_sel[4]: Other=5
6. 制作组 team_sel[4]: Other=5
★ 页面无 uplver 复选框，使用当前登录账号具名发布
★ 直连被墙，强制通过 NAS 本地 mihomo 代理访问 (http://192.168.0.7:7894)
"""

from typing import Dict, Any, Tuple
from musichub.adapters.nexusphp_base import NexusPHPBaseAdapter
from musichub.core.models import CanonicalMusicItem
from musichub.adapters.tag_matcher import TagMatcher


class LemonHDAdapter(NexusPHPBaseAdapter):
    def __init__(self, cookie_client=None, cover_service=None):
        super().__init__(
            site_name="LemonHD",
            domain="lemonhd.net",
            base_url="https://lemonhd.net",
            cookie_client=cookie_client,
            cover_service=cover_service
        )
        # LemonHD 必须走本地代理
        self.session.proxies = {
            "http": "http://192.168.0.7:7894",
            "https": "http://192.168.0.7:7894"
        }

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        if not item.tracks:
            return False, "LemonHD requires at least one track"
        return True, "LemonHD check passed"

    def _map_medium(self, med: str) -> str:
        """媒介 medium_sel[4]: CD=9, WEB-DL=4, Other=10"""
        m = (med or "").upper()
        if "WEB" in m:
            return "4"
        elif "CD" in m:
            return "9"
        return "10"

    def _map_codec(self, fmt: str) -> str:
        """音频编码 audiocodec_sel[4]: FLAC=10, WAV=11, APE=12, Other=13"""
        f = (fmt or "").upper()
        if "FLAC" in f:
            return "10"
        elif "WAV" in f:
            return "11"
        elif "APE" in f:
            return "12"
        return "13"

    def build_form_fields(self, item: CanonicalMusicItem) -> Dict[str, Any]:
        main_title, sub_title = self.format_title(item)

        fields = {
            "name": main_title,
            "small_descr": sub_title,
            "type": "402",                                   # 音乐分类实测 402
            "medium_sel[4]": self._map_medium(item.medium),
            "audiocodec_sel[4]": self._map_codec(item.format),
            "codec_sel[4]": "5",                             # Other 视频编码
            "standard_sel[4]": "5",                          # Other 分辨率
            "team_sel[4]": "5",                              # Other 制作组
            "descr": self.build_body(item),
            # 无 uplver 复选框 (具名发布)
        }
        TagMatcher.inject_form_tags("lemonhd", item, fields)
        return fields
