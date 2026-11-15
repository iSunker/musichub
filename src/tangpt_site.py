# -*- coding: utf-8 -*-
"""不可躺（tangpt.top）发布适配层 —— 把一条 task 渲染成 `takeupload.php` 的表单字段。

═══════════════════════════════════════════════════════════════════════════
架构与定位
═══════════════════════════════════════════════════════════════════════════
不可躺是 NexusPHP 站点，音乐分类 id = 409。
表单字段结构：
    · 分类: type=409
    · 媒介: medium_sel[4]（CD=8 / WEB-DL=11 / Other=9）
    · 编码: codec_sel[4]=5 (Other)
    · 分辨率: standard_sel[4]=7 (Other)
    · 音频编码: audiocodec_sel[4]（FLAC=1 / WAV=13 / APE=2 / MP3=4 / Other=7）
    · 制作组: team_sel[4]（OpenCD=24 / Other=5）
    · 匿名: uplver=yes
    · 简介: BBCode，由 descr.build_descr(site=db.SITE_TANGPT) 组装
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

MUSIC_TYPE = str(getattr(config, "TANGPT_MUSIC_CATEGORY", "409"))

# 媒介映射
MEDIUM_CD = "8"
MEDIUM_WEBDL = "11"
MEDIUM_OTHER = "9"

# 音频编码映射
AUDIO_FLAC = "1"
AUDIO_WAV = "13"
AUDIO_APE = "2"
AUDIO_MP3 = "4"
AUDIO_OTHER = "7"

# 制作组映射
TEAM_OPENCD = "24"
TEAM_OTHER = "5"

# 固定 Other 字段（不可躺音乐类表单需要占位）
CODEC_OTHER = "5"
STANDARD_OTHER = "7"


def map_medium(task):
    """根据 task['source'] / 标题 / 格式推断媒介。"""
    src = str(task.get("source") or "").lower()
    title = str(task.get("torrent_name") or task.get("formatted_title") or "").lower()
    if "web" in src or "web-dl" in title or "web" in title:
        return MEDIUM_WEBDL
    if "cd" in src or "cd" in title:
        return MEDIUM_CD
    return MEDIUM_OTHER


def map_audiocodec(task):
    """根据音频格式返回 audiocodec_sel[4]。"""
    fmt = str(task.get("audio_format") or task.get("format") or "").upper()
    if not fmt:
        title = str(task.get("torrent_name") or task.get("formatted_title") or "").upper()
        for cand in ("FLAC", "WAV", "APE", "MP3"):
            if cand in title:
                fmt = cand
                break
    if "FLAC" in fmt:
        return AUDIO_FLAC
    if "WAV" in fmt:
        return AUDIO_WAV
    if "APE" in fmt:
        return AUDIO_APE
    if "MP3" in fmt:
        return AUDIO_MP3
    return AUDIO_OTHER


def map_team(task):
    """制作组映射。来源是 OpenCD 则勾选 OpenCD(24)，否则 Other(5)。"""
    all_text = " ".join([
        str(task.get("torrent_name") or ""),
        str(task.get("formatted_title") or ""),
        str(task.get("source") or "")
    ]).lower()
    if "opencd" in all_text or task.get("opencd_tid"):
        return TEAM_OPENCD
    return TEAM_OTHER


def map_tags(task):
    """标签白名单。转种绝不冒充首发/自制/禁转，默认留空。"""
    return []


def build_fields(task, *, anonymous=True):
    """组装不可躺 takeupload.php 的 POST 表单字典。"""
    audit = {}

    # 1. 标题与副标题（严格复用全局统一口径）
    title = descr.normalize_music_title(task)
    small_descr = descr.build_music_small_descr(task)

    fields = {
        "type": MUSIC_TYPE,
        "name": title,
        "small_descr": small_descr,
        "medium_sel[4]": map_medium(task),
        "codec_sel[4]": CODEC_OTHER,
        "standard_sel[4]": STANDARD_OTHER,
        "audiocodec_sel[4]": map_audiocodec(task),
        "team_sel[4]": map_team(task),
    }

    if anonymous:
        fields["uplver"] = "yes"

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
        site=getattr(db, "SITE_TANGPT", "tangpt"),
        source_link=s_link
    )
    fields["descr"] = descr_text

    audit["name"] = f"← {title}"
    audit["small_descr"] = f"← {small_descr}"
    audit["type"] = f"← {MUSIC_TYPE}"
    audit["medium_sel[4]"] = f"← {fields['medium_sel[4]']}"
    audit["audiocodec_sel[4]"] = f"← {fields['audiocodec_sel[4]']}"
    audit["team_sel[4]"] = f"← {fields['team_sel[4]']}"
    audit["uplver"] = "yes" if anonymous else "no"

    return fields, audit


class TangptClient:
    """不可躺客户端（只读 + 发种/回加，不碰本地库）。"""
    def __init__(self, base_url=None, cookie=None):
        self.base_url = (base_url or getattr(config, "TANGPT_URL", "https://www.tangpt.top")).rstrip("/")
        self.cookie = cookie if cookie is not None else getattr(config, "TANGPT_COOKIE", "")
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
        """上传种子到不可躺，返回 (tid, msg)。成功时 tid 为数字字符串。"""
        if isinstance(torrent_path_or_bytes, (bytes, bytearray)):
            file_bytes = bytes(torrent_path_or_bytes)
        else:
            with open(torrent_path_or_bytes, "rb") as f:
                file_bytes = f.read()

        files = {
            "file": ("torrent.torrent", file_bytes, "application/x-bittorrent"),
        }

        # 整理表单数据（排除 file，处理 list 字段）
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

        # 检查是否成功重定向至 details.php?id=xxx
        loc = r.headers.get("Location") or ""
        m = re.search(r"details\.php\?id=(\d+)", loc)
        if m:
            return m.group(1), "success"

        # 如果直接返回 200，检查 body 里的 URL 或报错
        m2 = re.search(r"details\.php\?id=(\d+)", r.text)
        if m2:
            return m2.group(1), "success"

        err = _extract_error(r.text)
        _dump_takeupload(r.text, "tangpt")
        return None, err or f"HTTP {r.status_code}"

    def download_torrent(self, torrent_id):
        """下载不可躺站点带有用户 passkey 的 .torrent 文件。"""
        r = self.get("/download.php", params={"id": torrent_id})
        if r.status_code == 200 and r.content.startswith(b"d"):
            return r.content
        return None

    def SEARCH_PARAMS(self, name):
        """不可躺查重参数：精准按标题搜索，绑定音乐分类 409。"""
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
        "formatted_title": "Michael Jackson - Thriller 1982 FLAC - OpenCD",
        "torrent_name": "Michael Jackson - Thriller (1982) [FLAC]",
        "source": "CD",
        "audio_format": "FLAC",
        "tracklist": "1. Thriller",
        "opencd_tid": "12345",
        "cover_url": "https://img.test/pic.jpg",
    }
    fields, audit = build_fields(dummy_task, anonymous=True)
    assert fields["type"] == "409", f"type error: {fields['type']}"
    assert fields["medium_sel[4]"] == MEDIUM_CD, f"medium error: {fields['medium_sel[4]']}"
    assert fields["audiocodec_sel[4]"] == AUDIO_FLAC, f"audio error: {fields['audiocodec_sel[4]']}"
    assert fields["team_sel[4]"] == TEAM_OPENCD, f"team error: {fields['team_sel[4]']}"
    assert fields["codec_sel[4]"] == CODEC_OTHER, "codec error"
    assert fields["standard_sel[4]"] == STANDARD_OTHER, "standard error"
    assert fields["uplver"] == "yes", "anonymous error"
    assert "Michael Jackson - Thriller" in fields["name"], "name error"
    print("✅ tangpt_site selftest PASSED")


if __name__ == "__main__":
    selftest()
