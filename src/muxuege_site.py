# -*- coding: utf-8 -*-
"""慕雪阁（muxuege.org）发布适配层 —— 把一条 task 渲染成 `takeupload.php` 的表单字段。

═══════════════════════════════════════════════════════════════════════════
架构与定位
═══════════════════════════════════════════════════════════════════════════
慕雪阁是 NexusPHP 站点，音乐分类 id = 408。
表单字段结构：
    · 分类: type=408
    · 音频编码: audiocodec_sel[4]（APE=2 / DTS=3 / MP3=4 / OGG=5 / AAC=6 / Other=7）
      ★ 注意：该站无独立 FLAC / WAV 下拉选项，FLAC / WAV 统一映射至 Other(7)
    · 年代: processing_sel[4]（手写映射表：90年代之前=32 / 90年代=3 / 2000=4 ... 2027=31）
    · 媒介: medium_sel[4]（WEB-DL=10 / 抓轨 CD 留空）
    · 编码: codec_sel[4]=5 (Other)
    · 分辨率: standard_sel[4]（留空或根据表单要求）
    · 制作组: team_sel[4]=52 (Other)
    · 匿名: 页面无 uplver 选框（使用当前登录账号具名发布）
    · 简介: BBCode，由 descr.build_descr(site=db.SITE_MUXUEGE) 组装
    · 标题/副标题: 复用 descr.normalize_music_title / descr.build_music_small_descr
"""
import os
import re
import time
import requests

import config
import db
import descr
import net

from musopia import SiteError, UA, _extract_error, _dump_takeupload

MUSIC_TYPE = str(getattr(config, "MUXUEGE_MUSIC_CATEGORY", "408"))

# 音频编码映射
AUDIO_APE = "2"
AUDIO_DTS = "3"
AUDIO_MP3 = "4"
AUDIO_OGG = "5"
AUDIO_AAC = "6"
AUDIO_OTHER = "7"

# 制作组映射
TEAM_OTHER = "52"

# 视频编码固定 Other
CODEC_OTHER = "5"


def map_year(task):
    """年代映射 processing_sel[4]：
    '32:90 年代之前', '3:90 年代', '4:2000', '5:2001' ... '30:2026', '31:2027'
    """
    raw_year = task.get("year")
    if not raw_year:
        title = str(task.get("torrent_name") or task.get("formatted_title") or "")
        m = re.search(r"\b(19\d{2}|20\d{2})\b", title)
        if m:
            raw_year = m.group(1)
    try:
        y = int(raw_year)
    except (TypeError, ValueError):
        return "0"

    if y < 1990:
        return "32"
    if 1990 <= y <= 1999:
        return "3"
    if 2000 <= y <= 2027:
        # 2000 -> 4, 2001 -> 5, 2026 -> 30, offset = 4 - 2000 = -1996
        return str(y - 1996)
    return "0"


def map_medium(task):
    """媒介映射。若是 WEB 资源返回 10 (WEB-DL)，否则不强行填。"""
    src = str(task.get("source") or "").lower()
    title = str(task.get("torrent_name") or task.get("formatted_title") or "").lower()
    if "web" in src or "web" in title:
        return "10"
    return "0"


def map_audiocodec(task):
    """根据音频格式返回 audiocodec_sel[4]。"""
    fmt = str(task.get("audio_format") or task.get("format") or "").upper()
    if not fmt:
        title = str(task.get("torrent_name") or task.get("formatted_title") or "").upper()
        for cand in ("APE", "DTS", "MP3", "OGG", "AAC", "FLAC", "WAV"):
            if cand in title:
                fmt = cand
                break
    if "APE" in fmt:
        return AUDIO_APE
    if "DTS" in fmt:
        return AUDIO_DTS
    if "MP3" in fmt:
        return AUDIO_MP3
    if "OGG" in fmt:
        return AUDIO_OGG
    if "AAC" in fmt:
        return AUDIO_AAC
    return AUDIO_OTHER


def map_tags(task):
    """标签白名单。转种不勾选自购/禁转/乐府，默认留空。"""
    return []


def build_fields(task, *, anonymous=True):
    """组装慕雪阁 takeupload.php 的 POST 表单字典。"""
    audit = {}

    # 1. 标题与副标题
    title = descr.normalize_music_title(task)
    small_descr = descr.build_music_small_descr(task)

    fields = {
        "type": MUSIC_TYPE,
        "name": title,
        "small_descr": small_descr,
        "codec_sel[4]": CODEC_OTHER,
        "team_sel[4]": TEAM_OTHER,
        "audiocodec_sel[4]": map_audiocodec(task),
        "processing_sel[4]": map_year(task),
    }

    med = map_medium(task)
    if med != "0":
        fields["medium_sel[4]"] = med

    tags = map_tags(task)
    if tags:
        fields["tags[4][]"] = tags

    # 简介组装
    c_url = task.get("cover_url") or ""
    s_link = f"https://open.cd/details.php?id={task.get('opencd_tid')}" if task.get("opencd_tid") else ""
    descr_text = descr.build_descr(
        cover_url=c_url,
        tracklist=task.get("tracklist") or "",
        log_content=task.get("log_content") or "",
        opencd_descr=task.get("descr") or task.get("opencd_descr") or "",
        site=getattr(db, "SITE_MUXUEGE", "muxuege"),
        source_link=s_link
    )
    fields["descr"] = descr_text

    audit["name"] = f"← {title}"
    audit["small_descr"] = f"← {small_descr}"
    audit["type"] = f"← {MUSIC_TYPE}"
    audit["audiocodec_sel[4]"] = f"← {fields['audiocodec_sel[4]']}"
    audit["processing_sel[4]"] = f"← {fields['processing_sel[4]']}"
    audit["team_sel[4]"] = f"← {fields['team_sel[4]']}"

    return fields, audit


class MuxuegeClient:
    """慕雪阁客户端（只读 + 发种/回加，不碰本地库）。"""
    def __init__(self, base_url=None, cookie=None):
        self.base_url = (base_url or getattr(config, "MUXUEGE_URL", "https://pt.muxuege.org")).rstrip("/")
        self.cookie = cookie if cookie is not None else getattr(config, "MUXUEGE_COOKIE", "")
        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent": UA,
            "Cookie": self.cookie,
        })

    def get(self, path, **kwargs):
        url = self.base_url + ("" if path.startswith("/") else "/") + path
        return self.sess.get(url, timeout=kwargs.pop("timeout", 20), **kwargs)

    def post(self, path, **kwargs):
        url = self.base_url + ("" if path.startswith("/") else "/") + path
        return self.sess.post(url, timeout=kwargs.pop("timeout", 45), **kwargs)

    def check_login(self):
        """检查登录态，返回 (True, uid) 或 (False, msg)。"""
        try:
            r = self.get("/index.php")
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            m = re.search(r"userdetails\.php\?id=(\d+)", r.text)
            if m:
                return True, m.group(1)
            if "login.php" in r.url or "未登录" in r.text or "游客" in r.text:
                return False, "未登录 (重定向至 login)"
            return True, "unknown_uid"
        except Exception as e:
            return False, str(e)

    def fetch_upload_form(self):
        """拉取发布页校验表单存在。"""
        r = self.get("/upload.php")
        if r.status_code == 200 and "takeupload.php" in r.text:
            return True, "ok"
        return False, f"HTTP {r.status_code}"

    def upload_torrent(self, torrent_path_or_bytes, fields, form_info=None):
        """上传种子到慕雪阁，返回 (tid, msg)。成功时 tid 为数字字符串。"""
        if isinstance(torrent_path_or_bytes, (bytes, bytearray)):
            file_bytes = bytes(torrent_path_or_bytes)
        else:
            with open(torrent_path_or_bytes, "rb") as f:
                file_bytes = f.read()

        files = {
            "file": ("torrent.torrent", file_bytes, "application/x-bittorrent"),
        }

        data = []
        for k, v in fields.items():
            if k == "file":
                continue
            if isinstance(v, (list, tuple)):
                for item in v:
                    data.append((k, str(item)))
            else:
                data.append((k, str(v) if v is not None else ""))

        r = self.post("/takeupload.php", files=files, data=data, allow_redirects=False)

        loc = r.headers.get("Location") or ""
        m = re.search(r"details\.php\?id=(\d+)", loc)
        if m:
            return m.group(1), "success"

        m2 = re.search(r"details\.php\?id=(\d+)", r.text)
        if m2:
            return m2.group(1), "success"

        err = _extract_error(r.text)
        _dump_takeupload(r.text, "muxuege")
        return None, err or f"HTTP {r.status_code}"

    def download_torrent(self, torrent_id):
        """下载慕雪阁站点带有用户 passkey 的 .torrent 文件。"""
        r = self.get("/download.php", params={"id": torrent_id})
        if r.status_code == 200 and r.content.startswith(b"d"):
            return r.content
        return None

    def SEARCH_PARAMS(self, name):
        """慕雪阁查重参数：精准按标题搜索，绑定音乐分类 408。"""
        return {
            "search": name,
            "cat": MUSIC_TYPE,
            "incldead": 0,
            "spstate": 0,
            "inclbookmarked": 0,
            "search_area": 0,
            "search_mode": 0,
        }

    def find_existing_by_name(self, name, *, local_name=None, max_candidates=16, diag=None):
        """全站搜索防止撞车。"""
        import musopia
        return musopia.search_and_compare(
            self, name, local_name=local_name, base_path="/torrents.php",
            max_candidates=max_candidates, diag=diag
        )


def selftest():
    """纯离线自检，验证字段映射与表单构造。"""
    dummy_task = {
        "formatted_title": "Beyond - 真的Beyond 1993 FLAC - OpenCD",
        "torrent_name": "Beyond - 真的Beyond (1993) [FLAC]",
        "year": "1993",
        "source": "CD",
        "audio_format": "FLAC",
        "tracklist": "1. 海阔天空",
        "opencd_tid": "12346",
        "cover_url": "https://img.test/pic2.jpg",
    }
    fields, audit = build_fields(dummy_task, anonymous=True)
    assert fields["type"] == "408", f"type error: {fields['type']}"
    assert fields["processing_sel[4]"] == "3", f"year error: {fields['processing_sel[4]']}"
    assert fields["audiocodec_sel[4]"] == AUDIO_OTHER, f"audio error: {fields['audiocodec_sel[4]']}"
    assert fields["team_sel[4]"] == TEAM_OTHER, f"team error: {fields['team_sel[4]']}"
    assert fields["codec_sel[4]"] == CODEC_OTHER, "codec error"
    assert "Beyond" in fields["name"], "name error"
    print("✅ muxuege_site selftest PASSED")


if __name__ == "__main__":
    selftest()
