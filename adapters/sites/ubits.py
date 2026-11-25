# -*- coding: utf-8 -*-
"""UBits (幼哔) 站点适配器

遵循 NexusPHP (Blade) 真实实测契约：
1. 音乐分类: type=406 (音乐CD(Music CD Tracker))
2. 媒介 medium_sel[4]: Lossless Music=8, Track=9
3. 音频编码 audiocodec_sel[4]: FLAC=1, APE=2, MP3=4, OGG=5, AAC=6, Other=7
4. 视频编码 codec_sel[4]: Other=5
5. 分辨率 standard_sel[4]: SD=4
6. 制作组 team_sel[4]: Other=5
7. 地区 source_sel[4]: 大陆=1, 香港=2, 台湾=3, 欧美=4, 日本=5, 韩国=6, 其它=11
8. 表单带 CSRF Token: _token (从 upload.php 动态提取)
9. 支持匿名发布: uplver=yes
"""

import os
import re
from typing import Dict, Any, Tuple, Optional
from bs4 import BeautifulSoup
import requests

from musichub.adapters.nexusphp_base import NexusPHPBaseAdapter, DUPLICATE_NAME_PREFIX
from musichub.core.models import CanonicalMusicItem
from musichub.adapters.tag_matcher import TagMatcher


class UBitsAdapter(NexusPHPBaseAdapter):
    def __init__(self, cookie_client=None, cover_service=None):
        super().__init__(
            site_name="UBits",
            domain="ubits.club",
            base_url="https://ubits.club",
            cookie_client=cookie_client,
            cover_service=cover_service
        )

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        if not item.tracks:
            return False, "UBits requires at least one track"
        return True, "UBits check passed"

    def _fetch_upload_token(self) -> str:
        """从 upload.php 动态提取最新的 _token (Blade CSRF Token)"""
        headers = self._prepare_headers()
        try:
            resp = self.session.get(f"{self.base_url}/upload.php", headers=headers, timeout=15, verify=False)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                token_input = soup.find("input", {"name": "_token"})
                if token_input:
                    return token_input.get("value", "")
        except Exception:
            pass
        return ""

    def _map_medium(self, med: str) -> str:
        """媒介 medium_sel[4]: 8=Lossless Music, 9=Track"""
        m = (med or "").upper()
        if any(k in m for k in ("TRACK", "OST", "SINGLE")):
            return "9"
        return "8"

    def _map_audiocodec(self, fmt: str) -> str:
        """音频编码 audiocodec_sel[4]: 1=FLAC, 2=APE, 4=MP3, 5=OGG, 6=AAC, 7=Other"""
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

    def _map_source(self, item: CanonicalMusicItem) -> str:
        """地区 source_sel[4]: 1=大陆, 2=香港, 3=台湾, 4=欧美, 5=日本, 6=韩国, 11=其它"""
        text = f"{item.artist} {item.edition} {' '.join(item.genre)} {item.description}".lower()
        if any(k in text for k in ("中国大陆", "内地", "国语", "mandarin", "大陆")):
            return "1"
        if any(k in text for k in ("香港", "粤语", "cantonese")):
            return "2"
        if any(k in text for k in ("台湾", "台语", "taiwan")):
            return "3"
        if any(k in text for k in ("欧美", "english", "美国", "英国", "欧洲", "western")):
            return "4"
        if any(k in text for k in ("日本", "japanese", "jpop", "acg", "アニメ")):
            return "5"
        if any(k in text for k in ("韩国", "korean", "kpop")):
            return "6"
        return "11"

    def build_form_fields(self, item: CanonicalMusicItem) -> Dict[str, Any]:
        main_title, sub_title = self.format_title(item)
        token = self._fetch_upload_token()

        fields = {
            "_token": token,
            "name": main_title,
            "small_descr": sub_title,
            "type": "406",                                    # 406: 音乐CD
            "medium_sel[4]": self._map_medium(item.medium),
            "audiocodec_sel[4]": self._map_audiocodec(item.format),
            "codec_sel[4]": "5",                              # 5: Other
            "standard_sel[4]": "4",                           # 4: SD
            "team_sel[4]": "5",                               # 5: Other
            "source_sel[4]": self._map_source(item),
            "descr": self.build_body(item),
            "uplver": "yes",                                  # 匿名发布
        }
        TagMatcher.inject_form_tags("ubits", item, fields)
        return fields
