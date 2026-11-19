# -*- coding: utf-8 -*-
"""CrabPT (大闸蟹/蟹黄堡) 站点适配器

遵循老版实测契约 (crabpt_site.py)：
1. 音乐分类: 408
2. 媒介 source_sel[4]: 仅 WEB 数字发行落 WEB-DL(6)，其余（含 CD 抓轨）一律落 Other(1)
3. 音频编码 audiocodec_sel[4]: FLAC=10, WAV=11, APE=12, MP3(MPEG)=9, AAC=2, DTS=5, OGG=21, Other=1
4. 地区 processing_sel[4]: 大陆=2, 港台=3, 欧美=4, 日韩=5 (韩国条目落 5 约定), 其他=1
5. 安全铁律：严禁勾选禁转/原创/自购类虚假标签；图床必须由 CoverImageService 走 Lsky Pro 专线
"""

from typing import Dict, Any, Tuple
from musichub.adapters.nexusphp_base import NexusPHPBaseAdapter
from musichub.core.models import CanonicalMusicItem


class CrabPTAdapter(NexusPHPBaseAdapter):
    def __init__(self, cookie_client=None, cover_service=None):
        super().__init__(
            site_name="CrabPT",
            domain="crabpt.vip",
            base_url="https://crabpt.vip",
            cookie_client=cookie_client,
            cover_service=cover_service
        )

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        # 蟹黄堡基础护栏：必须包含音频音轨
        if not item.tracks:
            return False, "CrabPT requires at least one audio track in item"
        return True, "CrabPT pre-publish check passed"

    def _map_source(self, medium: str) -> str:
        """媒介 source_sel[4]：实测四条样本证实，仅 WEB 数字发行为 6，其余（包括 CD 抓轨）落 1"""
        med = (medium or "").upper()
        if "WEB" in med:
            return "6"
        return "1"

    def _map_audiocodec(self, fmt: str) -> str:
        """音频编码 audiocodec_sel[4] 严格映射表"""
        f = (fmt or "").upper()
        if "FLAC" in f:
            return "10"
        elif "WAV" in f:
            return "11"
        elif "APE" in f:
            return "12"
        elif "MP3" in f:
            return "9"   # MPEG
        elif "AAC" in f:
            return "2"
        elif "DTS" in f:
            return "5"
        elif "OGG" in f:
            return "21"
        return "1"       # Other

    def _map_processing_region(self, item: CanonicalMusicItem) -> str:
        """地区 processing_sel[4]: 大陆=2, 港台=3, 欧美=4, 日韩=5, 其他=1"""
        combined = f"{item.artist} {' '.join(item.genre)} {item.description}".lower()
        if any(k in combined for k in ("大陆", "国语", "内地", "mainland")):
            return "2"
        elif any(k in combined for k in ("港台", "香港", "台湾", "粤语")):
            return "3"
        elif any(k in combined for k in ("欧美", "western", "us", "uk")):
            return "4"
        elif any(k in combined for k in ("日韩", "日本", "韩国", "japan", "korea")):
            return "5"
        return "1"

    def build_form_fields(self, item: CanonicalMusicItem) -> Dict[str, Any]:
        main_title, sub_title = self.format_title(item)

        fields = {
            "name": main_title,
            "small_descr": sub_title,
            "type": "408",                                   # 大闸蟹音乐专区
            "source_sel[4]": self._map_source(item.medium),  # 媒介
            "standard_sel[4]": "1",                           # 分辨率: Other(1) (实测契约)
            "codec_sel[4]": "1",                              # 视频编码: Other(1) (实测契约)
            "audiocodec_sel[4]": self._map_audiocodec(item.format),
            "team_sel[4]": "1",                               # 制作组: Other(1) (实测契约)
            "processing_sel[4]": self._map_processing_region(item),
            "descr": self.build_body(item),
            "uplver": "yes",                                 # 匿名发布
        }
        return fields
