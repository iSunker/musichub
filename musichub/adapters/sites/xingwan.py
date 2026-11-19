# -*- coding: utf-8 -*-
"""星湾 (Xingwan) 站点适配器

遵循老版实测契约 (xingwan_site.py)：
1. 音乐分类: 408 (HQ Audio)
2. 媒介 medium_sel[4]: CD=19, Track/Soundtrack=20, Other=21
3. 标签 tags[4][]: 无损音乐=17, 国语=5, 粤语=31, 英语=35
4. 安全铁律：严禁勾选禁转/原创/自购类虚假标签；视频编码与分辨率必须留空
"""

from typing import Dict, Any, Tuple
from musichub.adapters.nexusphp_base import NexusPHPBaseAdapter
from musichub.core.models import CanonicalMusicItem


class XingwanAdapter(NexusPHPBaseAdapter):
    def __init__(self, cookie_client=None, cover_service=None):
        super().__init__(
            site_name="Xingwan",
            domain="xingwan.cc",
            base_url="https://xingwan.cc",
            cookie_client=cookie_client,
            cover_service=cover_service
        )

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        if not item.tracks:
            return False, "Xingwan requires at least one track"
        return True, "Xingwan check passed"

    def format_title(self, item: CanonicalMusicItem) -> Tuple[str, str]:
        main_title = f"[{item.format}] {item.artist} - {item.album} ({item.year})"
        if item.standard and item.standard != "Lossless":
            main_title += f" [{item.standard}]"

        sub_parts = []
        if item.edition:
            sub_parts.append(item.edition)
        if item.has_log:
            sub_parts.append(f"Log {item.log_score}%" if item.log_score else "Log")
        if item.has_cue:
            sub_parts.append("Cue")
        if item.medium:
            sub_parts.append(item.medium)
        sub_title = " | ".join(sub_parts) if sub_parts else f"{item.artist} 专辑"

        return main_title, sub_title

    def _map_medium(self, med: str) -> str:
        """媒介 medium_sel[4]: CD:19, Track:20, Other:21"""
        m = (med or "").upper()
        if "CD" in m:
            return "19"
        elif any(k in m for k in ("OST", "TRACK", "SOUNDTRACK")):
            return "20"
        return "21"

    def _map_tags(self, item: CanonicalMusicItem) -> list:
        """标签映射 tags[4][]: 基础无损音乐=17"""
        tags = ["17"]
        combined = f"{item.artist} {' '.join(item.genre)} {item.description}".lower()
        if any(k in combined for k in ("国语", "mandarin", "国", "大陆")):
            tags.append("5")
        elif any(k in combined for k in ("粤语", "cantonese", "粤", "香港")):
            tags.append("31")
        elif any(k in combined for k in ("英语", "english", "英文", "欧美")):
            tags.append("35")
        return tags

    def build_form_fields(self, item: CanonicalMusicItem) -> Dict[str, Any]:
        main_title, sub_title = self.format_title(item)

        fields = {
            "name": main_title,
            "small_descr": sub_title,
            "type": "408",                                   # 408: HQ Audio
            "medium_sel[4]": self._map_medium(item.medium),
            "tags[4][]": self._map_tags(item),
            "descr": self.build_body(item),
            "uplver": "yes",                                 # 匿名发布
        }
        return fields
