# -*- coding: utf-8 -*-
"""MusicHub 3.0 PT 站点优惠与全站免费窗口嗅探感知器 (SiteFreeWindowDetector)

核心定位：
1. 【精准解析 NexusPHP 优惠】：
   - 全站横幅 (Banner)：解析全站 Free、2xFree 活动倒计时区间；
   - 种子条目 (Row)：解析具体种子的优惠图标 (pro_free2up, pro_free, 2up, 50pct 等) 与到期时间；
2. 【规避防爬与风控】：
   - 支持从本地 raw_html 快照或适配器已获取页面直接解析，无需单独频繁发起网络探测；
3. 【调度赋能】：
   - 当目标站点处于全站 Free 或优惠期时，提升发布优先级或放宽每日配额。
"""

import re
import html as html_module
import logging
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger("SiteFreeWindowDetector")

# 优惠 Class 映射字典
PROMO_BY_CLASS = {
    "pro_free2up": "free2up",
    "pro_free": "free",
    "pro_50pctdown2up": "50pct2up",
    "pro_50pctdown": "50pct",
    "pro_30pctdown": "30pct",
    "pro_2up": "2up",
}
FREE_PROMOS = {"free", "free2up"}

# 正则匹配表达式
_IMG_TAG_RE = re.compile(r'<img\b[\s\S]*?/>', re.I)
_CLASS_ATTR_RE = re.compile(r'<img\b[^>]*?\bclass\s*=\s*"([^"]*)"', re.I)
_TITLE_ATTR_RE = re.compile(r'title=(?:"|&quot;)(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:"|&quot;)')

# 全局横幅活动正则表达式
_BANNER_TIME_RE1 = re.compile(r"北京时间\s*(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s*至\s*(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
_BANNER_TIME_RE2 = re.compile(r"全站\s*\[?Free\]?\s*生效中.{0,60}?时间：\s*(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s*~\s*(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", re.S)
_BANNER_KEYWORD_RE = re.compile(r"全站\s*(?:\[?Free\]?|免費|免费)\s*(?:活动|活動)?", re.I)


@dataclass
class FreeWindowStatus:
    is_free: bool = False
    promo_type: str = "none"       # free2up / free / 50pct / 2up / none
    expire_at: str = ""            # 到期时刻 'YYYY-MM-DD HH:MM:SS'
    is_global_free: bool = False   # 是否属于全站级别优惠
    source: str = "unknown"


class SiteFreeWindowDetector:
    @staticmethod
    def detect_global_free_from_html(html_content: str) -> FreeWindowStatus:
        """从站点首页或列表页顶部横幅提取全站 Free 活动"""
        if not html_content:
            return FreeWindowStatus()

        unescaped = html_module.unescape(html_content)

        # 1. 尝试匹配明确区间横幅
        m1 = _BANNER_TIME_RE1.search(unescaped)
        if m1:
            return FreeWindowStatus(
                is_free=True,
                promo_type="free",
                expire_at=m1.group(2),
                is_global_free=True,
                source="banner_range_1",
            )

        m2 = _BANNER_TIME_RE2.search(unescaped)
        if m2:
            return FreeWindowStatus(
                is_free=True,
                promo_type="free",
                expire_at=m2.group(2),
                is_global_free=True,
                source="banner_range_2",
            )

        # 2. 匹配裸标题关键字
        if _BANNER_KEYWORD_RE.search(unescaped):
            return FreeWindowStatus(
                is_free=True,
                promo_type="free",
                expire_at="",  # 裸标题无区间
                is_global_free=True,
                source="banner_keyword",
            )

        return FreeWindowStatus()

    @staticmethod
    def detect_torrent_promo_from_row_html(row_html: str) -> FreeWindowStatus:
        """从种子列表页的具体 TR 行中解析该种子的专属优惠标签"""
        if not row_html:
            return FreeWindowStatus()

        unescaped = html_module.unescape(row_html)
        promo_type = "none"
        expire_at = ""

        for img in _IMG_TAG_RE.finditer(unescaped):
            tag_text = img.group(0)
            cm = _CLASS_ATTR_RE.search(tag_text)
            if not cm:
                continue
            classes = cm.group(1).split()
            hit_promo = None
            for c in classes:
                if c in PROMO_BY_CLASS:
                    hit_promo = PROMO_BY_CLASS[c]
                    break

            if hit_promo:
                promo_type = hit_promo
                tm = _TITLE_ATTR_RE.search(tag_text)
                if tm:
                    expire_at = tm.group(1)
                break

        is_free = promo_type in FREE_PROMOS
        return FreeWindowStatus(
            is_free=is_free,
            promo_type=promo_type,
            expire_at=expire_at,
            is_global_free=False,
            source="row_html",
        )
