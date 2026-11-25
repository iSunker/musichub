# -*- coding: utf-8 -*-
"""SoulVoice (聆音Club) 站点适配器

遵循 NexusPHP 真实实测契约 (https://pt.soulvoice.club)：
1. 音乐分类: type=408 (音乐)
2. 媒介 medium_sel[4]: APE/FLAC=10, DSD=11, Other=12
3. 编码 codec_sel[4]: Other=5
4. 分辨率 standard_sel[4]: Other=4
5. 制作组 team_sel[4]: Other=5
6. H&R hr[4]: 0 (NO)
7. 匿名发布: uplver=yes
★ 直连光猫，IPv4 优先，脱钩外部环境变量代理 (trust_env=False)
"""

from typing import Dict, Any, Tuple
from musichub.adapters.nexusphp_base import NexusPHPBaseAdapter
from musichub.core.models import CanonicalMusicItem
from musichub.adapters.tag_matcher import TagMatcher


class SoulVoiceAdapter(NexusPHPBaseAdapter):
    def __init__(self, cookie_client=None, cover_service=None):
        super().__init__(
            site_name="SoulVoice",
            domain="pt.soulvoice.club",
            base_url="https://pt.soulvoice.club",
            cookie_client=cookie_client,
            cover_service=cover_service
        )

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        if not item.tracks:
            return False, "SoulVoice requires at least one track"
        return True, "SoulVoice check passed"

    def _map_medium(self, fmt: str, med: str) -> str:
        """媒介 medium_sel[4]: 10=APE/FLAC, 11=DSD, 12=Other"""
        f = (fmt or "").upper()
        m = (med or "").upper()
        if any(k in f or k in m for k in ("DSD", "SACD", "DSF", "DFF")):
            return "11"
        if any(k in f or k in m for k in ("FLAC", "APE")):
            return "10"
        return "12"

    def build_form_fields(self, item: CanonicalMusicItem) -> Dict[str, Any]:
        main_title, sub_title = self.format_title(item)

        fields = {
            "name": main_title,
            "small_descr": sub_title,
            "type": "408",                                   # 408: 音乐
            "medium_sel[4]": self._map_medium(item.format, item.medium),
            "codec_sel[4]": "5",                             # Other
            "standard_sel[4]": "4",                          # Other
            "team_sel[4]": "5",                              # Other
            "hr[4]": "0",                                    # NO H&R
            "descr": self.build_body(item),
            "uplver": "yes",                                 # 匿名发布
        }
        TagMatcher.inject_form_tags("soulvoice", item, fields)
        return fields
