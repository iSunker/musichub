# -*- coding: utf-8 -*-
"""OpenCD (皇后) 站点适配器

基于 OpenCD 官方发布页 (plugin_upload.php) 与油猴工具箱实测提取：
- 提交地址: plugin_upload_save.php (multipart/form-data)
- 分类 type: 408(音乐), 402(演唱会), 405(戏剧), 409(其他)
- 类别/流派 source: 2(流行), 3(古典), 11(器乐), 4(原声), 5(摇滚), 8(爵士), 12(新世纪), 14(电子), 15(民谣), 17(嘻哈), 9(其他)
- 格式 standard: 1(镜像), 2(WAV), 4(FLAC), 15(DTS), 17(DFF), 18(DSF), 10(其它)
- 媒介 medium: 1(CD), 2(24KCD), 3(DSD), 6(SACD), 20(Vinyl), 21(Web), 22(HI-RES), 15(Other)
- 轨数 audio_mode: single(整轨), multi(分轨)
- 来源 boardid: 1(原创), 2(转载)
- 转载规则 share_rule: 3(欢迎转载), 1(禁止转载), 2(未经允许不得转载)
"""

import os
import re
import logging
from typing import Dict, Any, Tuple, Optional
from musichub.adapters.nexusphp_base import NexusPHPBaseAdapter
from musichub.core.models import CanonicalMusicItem

logger = logging.getLogger("OpenCDAdapter")


class OpenCDAdapter(NexusPHPBaseAdapter):
    def __init__(self, cookie_client=None):
        super().__init__(
            site_name="OpenCD",
            domain="open.cd",
            base_url="https://open.cd",
            cookie_client=cookie_client
        )

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        """【皇后前置护栏】
        1. 艺术家、专辑名、发行年份必须完整存在
        2. 曲目列表不可为空（皇后强制校验 track_list）
        """
        if not item.artist or item.artist.strip() == "":
            return False, "OpenCD requires non-empty artist"
        if not item.album or item.album.strip() == "":
            return False, "OpenCD requires non-empty album name"
        if not item.year or not re.search(r"^\d{4}$", str(item.year).strip()):
            return False, f"OpenCD requires valid 4-digit year, got '{item.year}'"
        return True, "OpenCD pre-publish check passed"

    def format_title(self, item: CanonicalMusicItem) -> Tuple[str, str]:
        """皇后标题由系统生成预览，但主标题通常用于备查或种子名"""
        main_title = f"{item.artist} - {item.album} ({item.year}) [{item.format}]"
        
        sub_parts = []
        if item.edition:
            sub_parts.append(item.edition)
        if item.medium:
            sub_parts.append(item.medium)
        if item.standard and item.standard != "Lossless":
            sub_parts.append(item.standard)
        if item.has_log:
            sub_parts.append(f"Log {item.log_score}%" if item.log_score else "Log")
        if item.has_cue:
            sub_parts.append("Cue")
        sub_title = " | ".join(sub_parts) if sub_parts else f"{item.artist} - {item.album}"
        return main_title, sub_title

    def _map_standard_format(self, fmt: str) -> str:
        """映射 standard: 4=FLAC, 2=WAV, 18=DSF, 17=DFF, 15=DTS, 10=其它"""
        f = (fmt or "").upper()
        if "FLAC" in f:
            return "4"
        elif "WAV" in f:
            return "2"
        elif "DSF" in f:
            return "18"
        elif "DFF" in f:
            return "17"
        elif "DTS" in f:
            return "15"
        return "10"

    def _map_medium(self, med: str, standard: str) -> str:
        """映射 medium: 1=CD, 6=SACD, 20=Vinyl, 21=Web, 22=HI-RES, 3=DSD, 15=Other"""
        m = (med or "").upper()
        s = (standard or "").upper()
        if "SACD" in m:
            return "6"
        elif "VINYL" in m or "LP" in m:
            return "20"
        elif "WEB" in m or "DIGITAL" in m:
            return "21"
        elif "HI-RES" in s or "HI-RES" in m:
            return "22"
        elif "DSD" in m:
            return "3"
        elif "CD" in m:
            return "1"
        return "15"

    def _map_genre(self, genre_list: list) -> str:
        """映射流派 source: 2=流行, 3=古典, 4=原声, 5=摇滚, 8=爵士, 12=新世纪, 14=电子, 15=民谣, 17=嘻哈, 9=其他"""
        text = " ".join(genre_list).lower() if genre_list else ""
        if any(k in text for k in ("pop", "流行")):
            return "2"
        elif any(k in text for k in ("classical", "古典")):
            return "3"
        elif any(k in text for k in ("ost", "soundtrack", "原声")):
            return "4"
        elif any(k in text for k in ("rock", "摇滚")):
            return "5"
        elif any(k in text for k in ("jazz", "爵士")):
            return "8"
        elif any(k in text for k in ("newage", "新世纪")):
            return "12"
        elif any(k in text for k in ("electronic", "电子")):
            return "14"
        elif any(k in text for k in ("folk", "民谣")):
            return "15"
        elif any(k in text for k in ("hip", "rap", "嘻哈", "说唱")):
            return "17"
        return "2"  # 默认流行

    def build_track_list(self, item: CanonicalMusicItem) -> str:
        """构建皇后专用的 track_list 文本域内容"""
        if not item.tracks:
            return f"01. {item.album}"
        lines = []
        for t in item.tracks:
            dur = f" ({int(t.duration_seconds // 60)}:{int(t.duration_seconds % 60):02d})" if t.duration_seconds else ""
            lines.append(f"{t.track_number:02d}. {t.title}{dur}")
        return "\n".join(lines)

    def build_form_fields(self, item: CanonicalMusicItem) -> Dict[str, Any]:
        """严格匹配 OpenCD plugin_upload.php 表单字段规范"""
        main_title, sub_title = self.format_title(item)
        
        # 来源判定：若本系统转载则勾选转载并注明出处
        is_repost = bool(item.origin_site and "opencd" not in item.origin_site.lower())
        board_id = "2" if is_repost else "1"
        fr_name = item.origin_site if is_repost else ""

        # 音轨模式 single / multi
        audio_mode = "multi" if item.is_split_track else "single"

        fields = {
            "type": "408",                                               # 408: 音乐 (Music)
            "source": self._map_genre(item.genre),                      # 类别 (流派)
            "standard": self._map_standard_format(item.format),         # 格式 (FLAC/WAV...)
            "medium": self._map_medium(item.medium, item.standard),     # 介质 (CD/Web...)
            "audio_mode": audio_mode,                                   # 整轨 / 分轨
            "team": "5",                                                # Other (非官方首发组)
            "boardid": board_id,                                        # 1: 原创, 2: 转载
            "frname": fr_name,                                          # 转载来源文字
            "share_rule": "3",                                          # 3: 欢迎转载
            "artist": item.artist,                                      # 艺术家
            "resource_name": item.album,                                # 专辑名 (注意不是 name)
            "year": str(item.year).strip(),                             # 发行年份
            "small_descr": sub_title,                                   # 副标题
            "cover": item.cover_url or "",                              # 封面图床直链
            "descr": self.build_body(item),                             # 详细介绍 BBCode
            "track_list": self.build_track_list(item),                   # 详细曲目列表
            "uplver": "yes",                                            # 匿名发布
        }
        return fields

    def publish(self, item: CanonicalMusicItem, torrent_path: str) -> Tuple[bool, str, Optional[bytes]]:
        """OpenCD 特有发布路由：表单提交至 plugin_upload_save.php"""
        chk_ok, chk_msg = self.pre_publish_check(item)
        if not chk_ok:
            logger.warning(f"Blocked by OpenCD pre-publish check: {chk_msg}")
            return False, f"Pre-publish check failed: {chk_msg}", None

        if not os.path.exists(torrent_path) or os.path.getsize(torrent_path) == 0:
            return False, f"Invalid torrent file on path: {torrent_path}", None

        if ".!qB" in torrent_path or any(".!qB" in t.file_path for t in item.tracks):
            return False, "Blocked by .!qB guardrail (download not fully completed)", None

        headers = self._prepare_headers()
        if "Cookie" not in headers:
            return False, f"Missing CookieCloud credentials for {self.domain}", None

        fields = self.build_form_fields(item)
        upload_url = f"{self.base_url}/plugin_upload_save.php"

        try:
            with open(torrent_path, "rb") as f:
                files = {"file": (os.path.basename(torrent_path), f, "application/x-bittorrent")}
                resp = self.session.post(
                    upload_url,
                    data=fields,
                    files=files,
                    headers=headers,
                    timeout=45,
                    verify=False,
                    allow_redirects=True
                )

            final_url = resp.url
            resp_text = resp.text

            # 抓取重定向生成的种子 ID: details.php?id=(\d+)
            tid_match = re.search(r"id=(\d+)", final_url)
            if not tid_match:
                err_match = re.search(r"(?:错误|Error|提示|Upload failed)[:\s]*([^<]+)", resp_text, re.IGNORECASE)
                err_msg = err_match.group(1).strip() if err_match else "Upload did not redirect to details.php"
                return False, f"Publish rejected by OpenCD: {err_msg}", None

            torrent_id = tid_match.group(1)
            logger.info(f"Published to OpenCD successfully, torrent_id={torrent_id}")

            # 下载发布后的官方种子
            download_url = f"{self.base_url}/download.php?id={torrent_id}"
            dl_resp = self.session.get(download_url, headers=headers, timeout=30, verify=False)
            pub_torrent_bytes = dl_resp.content if dl_resp.status_code == 200 else None

            return True, torrent_id, pub_torrent_bytes

        except Exception as e:
            logger.error(f"Publish failed on OpenCD: {str(e)}")
            return False, f"Publish request error: {str(e)}", None
