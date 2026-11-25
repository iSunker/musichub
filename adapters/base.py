# -*- coding: utf-8 -*-
"""MusicHub 3.0 适配器抽象基类与通用契约 (BaseSiteAdapter)"""

from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple
from musichub.core.models import CanonicalMusicItem


class BaseSiteAdapter(ABC):
    def __init__(self, site_name: str, config: Dict[str, any]):
        self.site_name = site_name
        self.config = config

    @abstractmethod
    def check_exists(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        """查重契约：在目标站点搜索，若存在同规格返回 (True, reason)"""
        pass

    def check_exists_detail(self, item: CanonicalMusicItem) -> Tuple[bool, str, Optional[str]]:
        """查重并返回详细信息契约：返回 (exists, reason, matched_tid)
        默认回退调用 check_exists(item)，以保持向下兼容。
        """
        exists, why = self.check_exists(item)
        return exists, why, None

    @abstractmethod
    def format_title(self, item: CanonicalMusicItem) -> Tuple[str, str]:
        """标题与副标题格式化契约：生成符合该站规范的 (main_title, sub_title)"""
        pass

    @abstractmethod
    def build_body(self, item: CanonicalMusicItem) -> str:
        """简介生成契约：生成符合该站 BBCode/Markdown 规范的正文简介"""
        pass

    @abstractmethod
    def build_form_fields(self, item: CanonicalMusicItem) -> Dict[str, any]:
        """表单枚举映射：将标准 Medium/Format/Standard 转为该站 <select> 的具体 value"""
        pass

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        """发种前置护栏钩子：各站定制化防御检查（如特定字符拦截、必要元数据核验）。
        默认放行，子类覆盖重写以拦截不合规发种。
        返回: (True, "OK") 或 (False, "拦截原因")
        """
        return True, "Pre-publish check passed"

    @abstractmethod
    def publish(self, item: CanonicalMusicItem, torrent_path: str) -> Tuple[bool, str, Optional[bytes]]:
        """发种契约：提交 takeupload，成功返回 (True, target_torrent_id, torrent_bytes)"""
        pass
