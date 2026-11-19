# -*- coding: utf-8 -*-
"""唐门 / 不可躺 (TangPT) 站点适配器

遵循老版实测契约 (tangpt_site.py)：
1. 音乐分类: 409
2. 媒介 medium_sel[4]: CD=8, WEB-DL=11, Other=9
3. 音频编码 audiocodec_sel[4]: FLAC=1, WAV=13, APE=2, MP3=4, Other=7
4. 视频编码/分辨率占位: codec_sel[4]=5 (Other), standard_sel[4]=7 (Other)
5. 制作组 team_sel[4]: 若来源为 OpenCD 填 24，否则填 5 (Other)
"""

from typing import Dict, Any, Tuple
from musichub.adapters.nexusphp_base import NexusPHPBaseAdapter
from musichub.core.models import CanonicalMusicItem


class TangPTAdapter(NexusPHPBaseAdapter):
    def __init__(self, cookie_client=None, cover_service=None):
        super().__init__(
            site_name="TangPT",
            domain="www.tangpt.top",
            base_url="https://www.tangpt.top",
            cookie_client=cookie_client,
            cover_service=cover_service
        )
        # 唐门受 Cloudflare 保护，直连偶发握手超时，走 NAS 本地 mihomo 代理
        self.session.proxies = {
            "http": "http://192.168.0.7:7894",
            "https": "http://192.168.0.7:7894"
        }

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        if not item.tracks:
            return False, "TangPT requires at least one track"
        return True, "TangPT check passed"

    def _map_medium(self, med: str) -> str:
        """媒介 medium_sel[4]: CD=8, WEB-DL=11, Other=9"""
        m = (med or "").upper()
        if "WEB" in m:
            return "11"
        elif "CD" in m:
            return "8"
        return "9"

    def _map_codec(self, fmt: str) -> str:
        """音频编码 audiocodec_sel[4]: FLAC=1, WAV=13, APE=2, MP3=4, Other=7"""
        f = (fmt or "").upper()
        if "FLAC" in f:
            return "1"
        elif "WAV" in f:
            return "13"
        elif "APE" in f:
            return "2"
        elif "MP3" in f:
            return "4"
        return "7"

    def build_form_fields(self, item: CanonicalMusicItem) -> Dict[str, Any]:
        main_title, sub_title = self.format_title(item)

        # 制作组映射: 若来源包含 OpenCD 填 24，否则填 5 (Other)
        orig = (item.origin_site or "").lower()
        team_val = "24" if "opencd" in orig else "5"

        fields = {
            "name": main_title,
            "small_descr": sub_title,
            "type": "409",                                   # 音乐分类实测 409
            "medium_sel[4]": self._map_medium(item.medium),
            "codec_sel[4]": "5",                             # Other 视频编码占位
            "standard_sel[4]": "7",                          # Other 分辨率占位
            "audiocodec_sel[4]": self._map_codec(item.format),
            "team_sel[4]": team_val,
            "descr": self.build_body(item),
            "uplver": "yes",                                 # 匿名发布
        }
        return fields
