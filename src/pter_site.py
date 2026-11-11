#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""猫站（PTerClub，pterclub.net）发布适配层 —— 把一条 task 渲染成 `takeupload.php` 的表单字段。

═══════════════════════════════════════════════════════════════════════════
为什么单独一个文件（与乌托邦 musopia.py、蟹黄堡 crabpt_site.py 并列）
═══════════════════════════════════════════════════════════════════════════
★ 猫站的 NexusPHP 表单结构与乌托邦、蟹黄堡均存在显著差异：

    语义         乌托邦                      蟹黄堡                      猫站 (PTerClub)
    --------     ------------------------    --------------------------- ---------------------------
    分类 (type)  401(音乐)                   408(音乐)                   ★ 406(音乐)
    媒介         `medium_sel`                `source_sel[4]`             ★ `source_sel`（FLAC=8, WAV=9, Other=15）
    音频编码     `audiocodec_sel`(FLAC=1)    `audiocodec_sel[4]`(FLAC=10) ★ 无独立下拉（直接由 source_sel 区分）
    视频编码     `codec_sel`                 `codec_sel[4]`              ★ 音乐分类无视频编码下拉
    地区/产地    在 `tags[4][]` 数组里       `processing_sel[4]`         ★ 独立下拉 `team_sel`（大陆=1, 欧美=4...）
    标签体系     `tags[4][]` 下拉多选        `tags[4][]` 下拉多选        ★ 独立 checkbox 字段（guoyu=yes, yueyu=yes）
    Log/日志     [spoiler=LOG][code]...      无折叠代码（整块不保留）     ★ 必须保留且使用 [hide=Log] 语法
    来源引用     无强制要求                  无强制要求                  ★ 必须在 quote 中声明转载出处

⇒ 字段集与标签体系完全不同，独立成单独文件可确保老站逻辑（已稳定运行数百天）不受任何干扰。

★★ 安全铁律：
    作为转载方（从 OpenCD 转载），**严禁勾选** `jinzhuan`（禁转）、`pr`（原创）、`bim`（自购）！
    我们是转载方，无权也无理由主张禁转与原创；勾选即写假数据。
"""
import os
import re
import time

import requests

import config
import net   # 统一网络出口（强制 IPv4）——新 Session 一律走 net.new_session()
import db
import descr
import opencd

# 复用乌托邦那份 UA / 错误提取 —— 两站同属 NexusPHP
from musopia import SiteError, UA, _extract_error, _dump_takeupload

# ==================== 分类 id ====================
# ★ 音乐 = 406（实测自 PTNexus configs/pterclub.yaml）
MUSIC_TYPE = config.PTER_MUSIC_CATEGORY

# ==================== `source_sel`：媒介 ====================
#
# 猫站 source_sel 选项（音乐相关）：
#   8: FLAC
#   9: WAV
#   5: WEB-DL
#   10: ISO
#   15: Other（兜底）
SOURCE_FLAC = 8
SOURCE_WAV = 9
SOURCE_WEB_DL = 5
SOURCE_ISO = 10
SOURCE_OTHER = 15

# ==================== `team_sel`：地区/产地 ====================
#
# 猫站 team_sel 选项（产地）：
#   1: 大陆 (Mainland,CHN)
#   2: 香港 (HKG,CHN)
#   3: 台湾 (TWN,CHN)
#   4: 欧美 (Western)
#   5: 韩国 (KOR)
#   6: 日本 (JPN)
#   7: 印度 (IND)
#   8: 其它 (Other)
TEAM_MAINLAND = 1
TEAM_HK = 2
TEAM_TW = 3
TEAM_WESTERN = 4
TEAM_KOREA = 5
TEAM_JAPAN = 6
TEAM_INDIA = 7
TEAM_OTHER = 8


def _norm(v):
    """把编号归一成字符串，便于跨模块比较。"""
    if v is None:
        return None
    return str(v).strip()


def map_source(musopia_medium=None, format_name=None, audiocodec=None):
    """根据乌托邦媒介编号、音频编码编号或格式名称映射为猫站 `source_sel`。

    猫站 source_sel 选项（音乐）：
      8: FLAC
      9: WAV
      5: WEB-DL
      10: ISO
      15: Other（兜底）
    """
    fmt = (format_name or "").strip().upper()
    ac_str = str(audiocodec or "").strip().upper()

    # 1. 优先由格式名称/音频编码字符串直接判
    if "FLAC" in fmt or "FLAC" in ac_str:
        return SOURCE_FLAC
    if "WAV" in fmt or "WAV" in ac_str:
        return SOURCE_WAV
    if "ISO" in fmt:
        return SOURCE_ISO
    if "WEB" in fmt:
        return SOURCE_WEB_DL

    # 2. 尝试从 audiocodec 编号反查
    ac_want = _norm(audiocodec)
    if ac_want:
        for k, v in descr.AUDIOCODEC.items():
            if _norm(v) == ac_want:
                if k.upper() == "FLAC":
                    return SOURCE_FLAC
                if k.upper() == "WAV":
                    return SOURCE_WAV

    # 3. 尝试从 musopia_medium 反查名字
    med_want = _norm(musopia_medium)
    if med_want:
        name = None
        for k, v in descr.MEDIUM.items():
            if _norm(v) == med_want:
                name = k.upper()
                break
        if name:
            if "FLAC" in name:
                return SOURCE_FLAC
            if "WAV" in name:
                return SOURCE_WAV
            if "WEB" in name:
                return SOURCE_WEB_DL
            if "ISO" in name:
                return SOURCE_ISO

    return SOURCE_OTHER


_PROCESSING_BY_MUSOPIA_TAG = {
    "大陆": TEAM_MAINLAND,
    "港台": TEAM_TW,        # 皇后合并项，默认落台湾(3)
    "香港": TEAM_HK,
    "台湾": TEAM_TW,
    "欧美": TEAM_WESTERN,
    "日韩": TEAM_JAPAN,     # 皇后合并项，与蟹黄堡一致约定落日本(6)，显式说明
    "日本": TEAM_JAPAN,
    "韩国": TEAM_KOREA,
    "其它地区": TEAM_OTHER,
    "其他地区": TEAM_OTHER,
}

_PROCESSING_SPLIT_LOST = {
    "日韩": "皇后只给合并值 ⇒ 猫站分日本(6)/韩国(5)，拆不出 ⇒ 约定落 6 日本（已知不精确）",
    "港台": "皇后合并为港台 ⇒ 猫站分香港(2)/台湾(3) ⇒ 约定落 3 台湾",
}


def _extract_text_for_inference(task):
    """从 task 汇集用于产地/语言推断的文本。"""
    if not isinstance(task, dict):
        return ""
    parts = [
        task.get("formatted_title") or "",
        task.get("torrent_name") or "",
        task.get("small_descr") or "",
        task.get("artist") or "",
        task.get("descr") or task.get("opencd_descr") or "",
        task.get("tracklist") or "",
    ]
    return " ".join(str(p) for p in parts if p)


def map_processing(musopia_tags, task=None):
    """乌托邦/皇后标签编号列表 + task 元数据 → (猫站 `team_sel` 值, 说明或 None)。

    产地映射规则（根据猫站发种规范）：
      1: 大陆 (Mainland,CHN)
      2: 香港 (HKG,CHN)
      3: 台湾 (TWN,CHN)
      4: 欧美 (Western)
      5: 韩国 (KOR)
      6: 日本 (JPN)
      7: 印度 (IND)
      8: 其它 (Other)
    """
    tag_by_value = descr.TAGS
    notes = []
    text = _extract_text_for_inference(task)

    for v in musopia_tags or []:
        want = _norm(v)
        name = None
        for k, val in tag_by_value.items():
            if _norm(val) == want:
                name = k
                break
        if name is None:
            continue

        # 针对合并标签尝试智能细分
        if name == "港台":
            if text:
                if re.search(r"香港|HK|Hong\s*Kong|粤语|粵語|粤曲|粵曲|TVB|红馆", text, re.I):
                    notes.append("皇后标签「港台」⇒ 文本命中香港/粤语特征，精确落为 2 香港")
                    return TEAM_HK, ("；".join(notes) if notes else None)
                if re.search(r"台湾|台灣|Taiwan|台北|金曲|台语|臺語", text, re.I):
                    notes.append("皇后标签「港台」⇒ 文本命中台湾特征，精确落为 3 台湾")
                    return TEAM_TW, ("；".join(notes) if notes else None)
            notes.append(f"皇后标签「{name}」：{_PROCESSING_SPLIT_LOST[name]}")
            return TEAM_TW, ("；".join(notes) if notes else None)

        if name == "日韩":
            if text:
                if re.search(r"[ぁ-んァ-ヶー]|日本|Japan|J-Pop|Anime|Oricon", text, re.I):
                    notes.append("皇后标签「日韩」⇒ 文本命中日文假名/日本关键词，精确落为 6 日本")
                    return TEAM_JAPAN, ("；".join(notes) if notes else None)
                if re.search(r"[가-힣]|韩国|韓國|Korea|K-Pop|Melon", text, re.I):
                    notes.append("皇后标签「日韩」⇒ 文本命中韩文谚文/韩国关键词，精确落为 5 韩国")
                    return TEAM_KOREA, ("；".join(notes) if notes else None)
            notes.append(f"皇后标签「{name}」：{_PROCESSING_SPLIT_LOST[name]}")
            return TEAM_JAPAN, ("；".join(notes) if notes else None)

        if name in _PROCESSING_BY_MUSOPIA_TAG:
            val = _PROCESSING_BY_MUSOPIA_TAG[name]
            notes.append(f"命中地区标签「{name}」({val})")
            return val, ("；".join(notes) if notes else None)

    # 若标签中无地区信息，尝试从元数据特征推断保底
    if text:
        if re.search(r"[ぁ-んァ-ヶー]", text):
            return TEAM_JAPAN, "标签无地区 ⇒ 文本含日文假名，推断为 6 日本"
        if re.search(r"[가-힣]", text):
            return TEAM_KOREA, "标签无地区 ⇒ 文本含韩文谚文，推断为 5 韩国"
        if re.search(r"香港|Hong\s*Kong", text, re.I):
            return TEAM_HK, "标签无地区 ⇒ 文本提及香港，推断为 2 香港"
        if re.search(r"台湾|台灣|Taiwan", text, re.I):
            return TEAM_TW, "标签无地区 ⇒ 文本提及台湾，推断为 3 台湾"
        if re.search(r"大陆|內地|内地|Mainland", text, re.I):
            return TEAM_MAINLAND, "标签无地区 ⇒ 文本提及大陆，推断为 1 大陆"

        # 检查是否为纯英文/西方古典或流行（无任何 CJK 字符）
        artist = (task.get("artist") or "").strip() if task else ""
        title = (task.get("formatted_title") or task.get("torrent_name") or "") if task else ""
        probe = artist or title
        if probe and not re.search(r"[\u4e00-\u9fa5\u3040-\u30ff\uac00-\ud7af]", probe):
            return TEAM_WESTERN, f"标签无地区 ⇒ '{probe[:30]}' 为纯西文字符，推断为 4 欧美"

    return TEAM_OTHER, ("；".join(notes) if notes else None)


def map_checkbox_tags(musopia_tags, task=None):
    """将通用标签集合与 task 文本特征映射为猫站独立 checkbox 字段字典。

    返回 `(checkbox_dict, audit_notes)`。
    勾选传 "yes"，未勾选不传。

    根据猫站《How to fill out the Upload Page》与 Wiki《从零开始：如何发种》：
    - 音乐类独立语言勾选框为 `guoyu`（国语）与 `yueyu`（粤语）。
    - 严禁勾选 `jinzhuan`（禁转）、`pr`（原创）、`bim`（自购）（安全红线：转载方无权主张）。
    """
    values = [str(x) for x in (musopia_tags or [])]
    kept, dropped = descr.filter_forward_tags(values)

    tag_by_value = descr.TAGS
    names = set()
    for v in kept:
        want = _norm(v)
        for k, val in tag_by_value.items():
            if _norm(val) == want:
                names.add(k)
                break

    checkboxes = {}
    audit = []

    text = _extract_text_for_inference(task)

    # 1. 粤语检测
    is_yueyu = False
    if "粤语" in names or (text and re.search(r"粤语|粵語|粤曲|粵曲|广东话|廣東話|Cantonese", text, re.I)):
        is_yueyu = True
        checkboxes["yueyu"] = "yes"
        audit.append("yueyu=yes (粤语)")

    # 2. 国语/普通话检测
    if "国语" in names or (text and re.search(r"国语|國語|普通话|普通話|华语|華語|Mandarin", text, re.I)):
        checkboxes["guoyu"] = "yes"
        audit.append("guoyu=yes (国语)")
    elif not is_yueyu and task:
        # 智能推断：如果产地为大陆，且标题/艺术家包含汉字，且非纯乐器/古典
        proc_val, _ = map_processing(musopia_tags, task=task)
        if proc_val == TEAM_MAINLAND:
            artist_title = f"{task.get('artist') or ''} {task.get('formatted_title') or ''}"
            has_chinese = bool(re.search(r"[\u4e00-\u9fa5]", artist_title))
            is_instrumental = bool(re.search(r"器乐|純音樂|纯音乐|Instrumental|古典|Classical|原声|OST|Soundtrack|Piano|Violin|钢琴|小提琴", text, re.I))
            if has_chinese and not is_instrumental:
                checkboxes["guoyu"] = "yes"
                audit.append("guoyu=yes (大陆中文声乐专辑推断)")

    # 严禁勾选的黑名单说明与安全铁律防线
    for v, nm, why in dropped:
        audit.append(f"未勾选 {nm}（方向过滤：{why}）")
    for forbidden in ("jinzhuan", "pr", "bim"):
        if forbidden in checkboxes:
            del checkboxes[forbidden]
            audit.append(f"安全拦截：强制移除 {forbidden}")

    return checkboxes, audit


def _as_list(v):
    """将 tags 转换为整数列表。"""
    if v is None or v == "":
        return []
    if isinstance(v, list):
        out = []
        for x in v:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                pass
        return out
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("["):
            try:
                import json
                return [int(x) for x in json.loads(s)]
            except (ValueError, TypeError):
                pass
        out = []
        for part in re.split(r"[,\s]+", s):
            try:
                out.append(int(part))
            except (TypeError, ValueError):
                pass
        return out
    try:
        return [int(v)]
    except (TypeError, ValueError):
        return []


# ==================== 猫站命名规范与制作组白名单 ====================
#
# ★ 口径实现已统一到 `descr.normalize_music_title` / `descr.build_music_small_descr`
#   （三站共用一份；本文件只保留薄封装，避免两个口径各自漂移）。
RECOGNIZED_GROUPS = descr.RECOGNIZED_GROUPS


def normalize_pter_title(task):
    """按猫站规范标准化主标题 —— 实现见 `descr.normalize_music_title`。"""
    return descr.normalize_music_title(task)


def build_pter_small_descr(task):
    """按猫站规范组装合规副标题 —— 实现见 `descr.build_music_small_descr`。"""
    return descr.build_music_small_descr(task)


def build_fields(task, *, anonymous=True):
    """把一条 task 渲染成猫站 `takeupload.php` 的表单字段。

    站点强制匿名发布（NexusPHP 标准表单字段 `uplver="yes"`）。
    `anonymous` 默认为 True。若显式传 False，则不带 `uplver` 字段。
    """
    task = db.merge_mapped(dict(task))
    audit = []

    # 1. 标题：根据猫站 Wiki 规范全面重构并补齐采样规格
    title = normalize_pter_title(task)
    audit.append(f"name（主标题）← {title}")

    # 2. 副标题：根据猫站 Wiki 规范构建（艺术家 - 专辑 | 发行类别 | 介质规格）
    small_descr = build_pter_small_descr(task)
    audit.append(f"small_descr（副标题·合规组装）← {small_descr}")

    # 3. 来源声明（转载引用块，四站统一文案 —— 均由 `descr.build_source_link` 生成）
    source_link = descr.build_source_link(task)
    audit.append(f"source_link（出处置顶引用）← {source_link}")

    # 4. 简介正文
    logs = None
    raw_logs = task.get("logs_json")
    if raw_logs:
        try:
            import json
            parsed = json.loads(raw_logs)
            if isinstance(parsed, list) and parsed:
                logs = parsed
        except (ValueError, TypeError):
            logs = None

    descr_body = descr.build_descr(
        cover_url=task.get("cover_url") or "",
        tracklist=task.get("tracklist") or "",
        log_content=task.get("log_content") or "",
        log_filename=task.get("log_filename") or "",
        logs=logs,
        opencd_descr=task.get("descr") or task.get("opencd_descr") or "",
        site=db.SITE_PTER,
        source_link=source_link,
    )
    audit.append("descr（简介）← descr.build_descr()（含 [hide=Log] 语法与出处引用）")

    # 5. 质量与产地
    musopia_medium = task.get("medium_sel")
    format_hint = task.get("torrent_name") or ""
    audiocodec = task.get("audiocodec_sel")
    source = map_source(musopia_medium, format_name=format_hint, audiocodec=audiocodec)
    proc, proc_note = map_processing(_as_list(task.get("tags")), task=task)
    checkboxes, tag_audit = map_checkbox_tags(_as_list(task.get("tags")), task=task)

    audit.append(f"source_sel（媒介）← medium_sel={musopia_medium}, audiocodec={audiocodec} ⇒ {source}")
    audit.append(f"team_sel（产地）← tags/metadata ⇒ {proc}" + (f"　★ {proc_note}" if proc_note else ""))
    for ta in tag_audit:
        audit.append(f"tag_checkbox ← {ta}")

    fields = {
        "type": MUSIC_TYPE,
        "name": title,
        "small_descr": small_descr,
        "descr": descr_body,
        "source_sel": source,
        "team_sel": proc,
    }
    # 合并独立 checkbox 字段
    for k, v in checkboxes.items():
        fields[k] = v

    if anonymous:
        fields["uplver"] = "yes"
        audit.append("uplver=yes (匿名发布)")

    return fields, audit


# ============================================================================
# 猫站客户端
# ============================================================================

class PterClient:
    """猫站（pterclub.net）客户端。只读 + POST takeupload，不碰 qB、不碰库。"""

    def __init__(self, base_url=None, cookie=None, request_interval=1.5):
        self.base = (base_url or config.PTER_URL).rstrip("/")
        self.cookie = cookie if cookie is not None else config.PTER_COOKIE
        self.interval = request_interval
        self._last_request = 0.0

        self.sess = net.new_session()
        self.sess.headers.update({
            "User-Agent": UA,
            "Cookie": self.cookie,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    def _throttle(self):
        elapsed = time.time() - self._last_request
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last_request = time.time()

    def _url(self, path):
        return path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"

    def get(self, path, **kw):
        self._throttle()
        kw.setdefault("timeout", 30)
        return self.sess.get(self._url(path), **kw)

    def post(self, path, **kw):
        self._throttle()
        kw.setdefault("timeout", 60)
        return self.sess.post(self._url(path), **kw)

    def download_torrent(self, torrent_id):
        """下载猫站服务端重写 tracker 后的 .torrent 文件，返回 bytes 或 None。"""
        r = self.get("/download.php", params={"id": torrent_id})
        if r.status_code == 200 and r.content.startswith(b"d"):
            return r.content
        return None

    @staticmethod
    def SEARCH_PARAMS(name):
        """猫站（PTerClub）列表页搜索参数。

        基于猫站 NexusPHP 与 easy-upload PTer 配置：
        - search_area=0 默认按主标题搜索，精准锁定已有条目；
        - incldead=0 排除死种。
        """
        return {
            "search": name,
            "incldead": 0,
            "spstate": 0,
            "inclbookmarked": 0,
            "search_area": 0,
            "search_mode": 0,
        }

    def find_existing_by_name(self, name, *, local_name, base_path="/torrents.php",
                              max_candidates=None, diag=None):
        """在猫站全站范围搜 name，比对 local_name，防止发重。复用 musopia.search_and_compare。"""
        import musopia
        return musopia.search_and_compare(self, name, local_name=local_name,
                                          base_path=base_path,
                                          max_candidates=max_candidates,
                                          diag=diag)

    def have_cookie(self):
        return bool((self.cookie or "").strip())

    def check_login(self):
        """验证 Cookie 有效性，返回 user_id 或 None。"""
        r = self.get("/torrents.php")
        if r.status_code == 401 or "WWW-Authenticate" in (r.headers or {}):
            raise SiteError(f"猫站拒绝访问 (HTTP {r.status_code})，PTER_COOKIE 已失效或未配置。")
        if r.status_code != 200:
            raise SiteError(f"访问猫站 torrents.php 失败，HTTP {r.status_code}")
        body = r.text or ""
        if "takelogin.php" in body or "login.php?returnto" in body:
            raise SiteError("猫站 Cookie 已失效（页面退回登录页），请更新 PTER_COOKIE。")
        m = re.search(r"userdetails\.php\?id=(\d+)", body)
        return m.group(1) if m else None

    def fetch_upload_form(self):
        """GET /upload.php 读取空白表单，获取 action 与必要字段校验。"""
        r = self.get("/upload.php")
        if r.status_code != 200:
            raise SiteError(f"获取猫站发布页失败，HTTP {r.status_code}")
        body = r.text or ""
        if "takelogin.php" in body:
            raise SiteError("猫站 Cookie 已失效（发布页退回登录页）")

        bs = __import__("bs4").BeautifulSoup(body, "html.parser")
        form = bs.find("form", id="compose") or bs.find("form", attrs={"name": "upload"})
        if form is None:
            # 尝试兜底找 action 包含 takeupload 的 form
            for f in bs.find_all("form"):
                if "takeupload" in (f.get("action") or "").lower():
                    form = f
                    break
        if form is None:
            raise SiteError("猫站发布页中未找到发布表单（可能是账号无发种权限或页面结构变更）。")

        actions = {i.get("name"): i for i in form.find_all(["input", "select", "textarea"])
                   if i.get("name")}
        csrf = {k: v for k, v in actions.items()
                if k.lower() in ("csrf_token", "csrf", "_token")}
        return {
            "form": form,
            "actions": actions,
            "action": form.get("action") or "/takeupload.php",
            "csrf": csrf,
            "enctype": (form.get("enctype") or "").lower(),
        }

    def upload_torrent(self, torrent_path, fields, form_info=None):
        """向猫站提交发布新种子，返回 (torrent_id 或 None, 消息文本)。"""
        import torf

        if not os.path.exists(torrent_path):
            raise SiteError(f"种子文件不存在: {torrent_path}")
        try:
            with open(torrent_path, "rb") as fh:
                raw = fh.read()
            torf.Torrent.read_stream(__import__("io").BytesIO(raw))
        except Exception as e:
            raise SiteError(f"种子文件损坏，非有效 .torrent: {torrent_path}: {e}") from e

        field_name = "file"
        if form_info:
            cand = [k for k in (form_info.get("actions") or {}) if k in ("file", "torrent")]
            if cand:
                field_name = cand[0]

        files = {
            field_name: (
                os.path.basename(torrent_path),
                open(torrent_path, "rb"),
                "application/x-bittorrent",
            )
        }

        # 补全 CSRF
        data = dict(fields)
        if form_info and form_info.get("csrf"):
            for k, el in form_info["csrf"].items():
                if k not in data and el.get("value"):
                    data[k] = el["value"]

        action = (form_info.get("action") if form_info else None) or "/takeupload.php"
        try:
            r = self.post(action, data=data, files=files, allow_redirects=False)
        finally:
            try:
                files[field_name][1].close()
            except Exception:
                pass

        _dump_takeupload(r.text or "", torrent_path, kind="pter-takeupload")

        if r.status_code in (401, 403):
            raise SiteError(f"猫站发布被拒 (HTTP {r.status_code})")
        if r.status_code >= 500:
            raise SiteError(f"猫站服务端故障 (HTTP {r.status_code})")

        # NexusPHP 正常发布成功后返回 302 重定向：Location: details.php?id=XXXX&uploaded=1
        if r.status_code in (301, 302, 303):
            loc = r.headers.get("Location") or ""
            if "login.php" in loc.lower():
                raise SiteError("猫站发布后被重定向至登录页，Cookie 可能已失效")
            m = re.search(r"[?&]id=(\d+)", loc)
            if m:
                tid = m.group(1)
                return tid, f"SUCCESS: 种子发布成功 (id={tid})"
            return None, f"UNKNOWN: 重定向 Location 未解析出种子 ID: {loc}"

        # 若返回 200，先检查是否掉登录
        if "takelogin.php" in (r.text or "") or "login.php" in (r.url or ""):
            raise SiteError("猫站发布返回登录页，Cookie 可能已失效")

        err = _extract_error(r.text or "")
        if err:
            lower_err = err.lower()
            if "already exists" in lower_err or "已存在" in err or "重复" in err:
                return None, f"REJECT: 种子已存在: {err}"
            return None, f"REJECT: 站点拒绝: {err}"

        # 尝试从最终 URL 或页面内容中提取 tid
        tid = None
        m = re.search(r"[?&]id=(\d+)", r.url or "")
        if m:
            tid = m.group(1)
        if not tid:
            # 严格匹配 details.php?id=XXXX&uploaded=1 或详情链接，避免误匹配页面静态广告 ID (如 2257)
            m = re.search(r"details\.php\?id=(\d+)&uploaded=1", r.text or "")
            if m:
                tid = m.group(1)
            else:
                m = re.search(r'href=["\']details\.php\?id=(\d+)["\']', r.text or "")
                if m:
                    tid = m.group(1)

        if tid:
            return tid, f"SUCCESS: 种子发布成功 (id={tid})"
        return None, "UNKNOWN: 提交未报错但未解析出种子 ID，请核查页面"


    def fetch_edit_form(self, torrent_id):
        """GET /edit.php?id=N，把 takeedit.php 表单解析成 (fields, current_snapshot)。"""
        r = self.get("/edit.php", params={"id": torrent_id})
        if r.status_code != 200:
            raise SiteError(f"打开 edit.php?id={torrent_id} 失败，HTTP {r.status_code}")
        body = r.text or ""
        if "takelogin.php" in body:
            raise SiteError("猫站 Cookie 已失效（打开编辑页退回登录页）")

        bs = __import__("bs4").BeautifulSoup(body, "html.parser")
        form = None
        for fm in bs.find_all("form"):
            if "takeedit" in (fm.get("action") or "").lower():
                form = fm
                break
        if form is None:
            raise SiteError(f"猫站 id={torrent_id} 编辑页未找到 takeedit 表单")

        fields = {}
        for el in form.find_all(["input", "select", "textarea"]):
            name = el.get("name")
            if not name:
                continue
            if el.name == "input":
                typ = (el.get("type") or "text").lower()
                if typ in ("submit", "button", "reset", "file", "image"):
                    continue
                val = el.get("value") or ""
                if typ in ("checkbox", "radio"):
                    if not el.has_attr("checked"):
                        continue
                    if name.endswith("[]"):
                        fields.setdefault(name, []).append(val)
                    else:
                        fields[name] = val
                else:
                    fields[name] = val
            elif el.name == "textarea":
                fields[name] = __import__("html").unescape(el.get_text() or "")
            elif el.name == "select":
                selected = [o.get("value") or "" for o in el.find_all("option") if o.has_attr("selected")]
                if not selected:
                    opts = el.find_all("option")
                    selected = [opts[0].get("value") or ""] if opts else [""]
                fields[name] = selected[0]

        fields["id"] = str(torrent_id)
        return fields, dict(fields)

    def edit_torrent(self, torrent_id, overrides, *, dry_run=False, verbose=True):
        """提交编辑种子页面。overrides 覆盖目标字段，其余沿用站点现值。

        返回 (ok: bool, message: str)。
        """
        fields, before = self.fetch_edit_form(torrent_id)
        changes = []
        for k, v in overrides.items():
            old = before.get(k)
            if isinstance(v, (list, tuple)):
                v = [str(x) for x in v]
            else:
                v = "" if v is None else str(v)

            if isinstance(old, list) and isinstance(v, list):
                same = sorted(old) == sorted(v)
            else:
                same = (old == v)
            if not same:
                changes.append((k, old, v))
            fields[k] = v

        if verbose or dry_run:
            if not changes:
                print(f"    ℹ️  id={torrent_id} 无字段变化")
            for k, old, new in changes:
                if k == "descr":
                    short_old = (old[:60] + "...") if len(old) > 60 else old
                    short_new = (new[:60] + "...") if len(new) > 60 else new
                    print(f"    · descr:\n        [原值]: {short_old!r}\n        [新值]: {short_new!r}")
                else:
                    print(f"    · {k}: {old!r} -> {new!r}")

        if dry_run:
            return True, f"dry-run: {len(changes)} 个字段将被修改"

        if not changes:
            return True, "无需修改"

        # multipart/form-data 构造
        parts = []
        for k, v in fields.items():
            if isinstance(v, list):
                for item in v:
                    parts.append((k, (None, item)))
            else:
                parts.append((k, (None, str(v))))

        r = self.post(
            "/takeedit.php",
            files=parts,
            headers={"Referer": self._url(f"/edit.php?id={torrent_id}"),
                     "Origin": self.base},
            allow_redirects=False,
        )

        if r.status_code in (301, 302, 303):
            loc = r.headers.get("Location", "")
            if "details.php" in loc or f"id={torrent_id}" in loc:
                return True, f"SUCCESS: 编辑成功 (HTTP {r.status_code})"
            if "login.php" in loc:
                raise SiteError("Cookie 失效（被重定向到登录页）")

        if r.status_code == 200:
            if "takelogin.php" in (r.text or ""):
                raise SiteError("Cookie 失效")
            err = _extract_error(r.text or "")
            if err:
                return False, f"REJECT: 站点拒绝: {err}"
            refresh = r.headers.get("refresh", "") or r.headers.get("Refresh", "")
            if "details.php" in refresh or f"id={torrent_id}" in refresh:
                return True, "SUCCESS: 编辑成功"
            if "details.php" in (r.url or "") or f"id={torrent_id}" in (r.text or ""):
                return True, "SUCCESS: 编辑成功"

        return False, f"UNKNOWN: 编辑返回 HTTP {r.status_code}"


# ============================================================================
# 离线自检
# ============================================================================

def selftest():
    """猫站适配层纯本地离线自检（零请求、零写库）。"""
    print("=" * 60)
    print("pter_site 离线自检（纯离线，零请求）")
    print("=" * 60)

    ok = [True]

    def chk(name, cond, msg=""):
        if cond:
            print(f"  ✅ {name}")
        else:
            print(f"  ❌ {name} -- {msg}")
            ok[0] = False

    # 1. 媒介映射
    chk("FLAC 媒介映射为 8", map_source(None, "FLAC") == 8)
    chk("WAV 媒介映射为 9", map_source(None, "WAV") == 9)
    chk("WEB-DL 媒介映射为 5", map_source(None, "WEB-DL") == 5)
    chk("ISO 媒介映射为 10", map_source(None, "ISO") == 10)
    chk("audiocodec=1 (FLAC) 映射为 8", map_source(audiocodec=1) == 8)
    chk("format_name 含 WAV 映射为 9", map_source(format_name="Album - WAV 整轨") == 9)
    chk("未知媒介兜底为 15 (Other)", map_source(None, "MP3") == 15)

    # 2. 产地映射与智能推断
    proc_cn, _ = map_processing(["4"])
    chk("大陆标签映射为 1", proc_cn == 1)
    proc_eu, _ = map_processing(["5"])
    chk("欧美标签映射为 4", proc_eu == 4)
    proc_jp_kr, note = map_processing(["7"])
    chk("日韩标签默认映射为 6 (日本)", proc_jp_kr == 6 and "已知不精确" in (note or ""))
    proc_hk, _ = map_processing(["6"], task={"formatted_title": "郭凤女 - 魂断乌江（粤曲对唱）"})
    chk("港台标签命中粤语特征细分为 2 (香港)", proc_hk == 2)
    proc_tw, _ = map_processing(["6"], task={"formatted_title": "齐秦 - 台湾金曲精选"})
    chk("港台标签命中台湾特征细分为 3 (台湾)", proc_tw == 3)
    proc_jp, _ = map_processing([], task={"formatted_title": "ベートーヴェン 2026"})
    chk("无标签文本含日文假名推断为 6 (日本)", proc_jp == 6)
    proc_west, _ = map_processing([], task={"artist": "Julian Leaper", "formatted_title": "Beethoven Violin Sonatas"})
    chk("无标签纯西文艺术家推断为 4 (欧美)", proc_west == 4)

    # 3. Checkbox 标签、语言识别与安全铁律
    cb_sec, _ = map_checkbox_tags(["5", "6", "1", "2"])  # 含国语/港台及禁转/首发
    chk("禁转、原创、自购未进 checkbox", "jinzhuan" not in cb_sec and "pr" not in cb_sec and "bim" not in cb_sec)
    cb_yue, _ = map_checkbox_tags([], task={"descr": "郭凤女、何华栈 - 魂断乌江（粤曲对唱）"})
    chk("文本命中粤曲识别 yueyu=yes", cb_yue.get("yueyu") == "yes")
    cb_guo, _ = map_checkbox_tags([4], task={"formatted_title": "齐秦 - 我拿什么爱你 1998", "tags": [4]})
    chk("大陆中文声乐专辑推断 guoyu=yes", cb_guo.get("guoyu") == "yes")
    cb_inst, _ = map_checkbox_tags([], task={"formatted_title": "Beethoven Piano Sonatas", "tags": [15], "descr": "纯音乐器乐演奏"})
    chk("纯古典/器乐不妄加国语/粤语", "guoyu" not in cb_inst and "yueyu" not in cb_inst)

    # 4. build_fields 构造与标题/副标题规范验证
    dummy_task = {
        "formatted_title": "Artist - Album 2026 - FLAC 分轨 - Yuekui",
        "smalltitle": "",
        "artist": "Artist",
        "album": "Album",
        "year": "2026",
        "opencd_tid": "88888",
        "tags": [4],
        "medium_sel": 1,
        "torrent_name": "Artist - Album 2026 FLAC",
        "log_content": "EAC extraction logfile from 1. May 2026",
    }
    fields, audit_lines = build_fields(dummy_task)
    chk("主分类为 406 (Music)", fields["type"] == "406")
    chk("主标题剥除非法分轨与压制者名", "分轨" not in fields["name"] and "Yuekui" not in fields["name"])
    chk("主标题自动补充 16bit 44.1kHz 规格", "FLAC 16bit 44.1kHz" in fields["name"])
    chk("副标题符合三段式规范", "Artist - Album | 专辑 | CD / Lossless / Log / Cue" in fields["small_descr"])
    chk("媒介映射正确", fields["source_sel"] == 8)
    chk("产地映射正确", fields["team_sel"] == 1)
    chk("正文包含 [hide=Log]", "[hide=Log]" in fields["descr"])
    chk("正文开头包含置顶的转自 OpenCD 引用", fields["descr"].startswith("[quote]转自OpenCD: https://open.cd/details.php?id=88888[/quote]"))

    # 4b. 纯中文标题与合辑 (VA) 规范化验证
    va_task = {
        "formatted_title": "VA - 说唱巅峰对决2026 第九期 2026 - FLAC 分轨 - Yuekui",
        "artist": "VA",
        "album": "说唱巅峰对决2026 第九期",
        "year": "2026年",
        "format": "FLAC 分轨",
        "opencd_tid": "12345",
        "tags": [4],
    }
    fields_va, _ = build_fields(va_task)
    chk("VA 合辑艺术家转为 Various Artists", fields_va["name"].startswith("Various Artists - "))
    chk("合辑标题无分轨与压制者后缀", "分轨" not in fields_va["name"] and "Yuekui" not in fields_va["name"])
    chk("合辑标题补充采样规格", "FLAC 16bit 44.1kHz" in fields_va["name"])

    # 4c. 多碟日志折叠标签验证
    multi_log_task = {
        "formatted_title": "Test Multi Disc Album",
        "logs_json": '[{"label": "CD1 LOG", "content": "disc 1 log"}, {"label": "CD2 LOG", "content": "disc 2 log"}]',
        "opencd_tid": "99999",
        "tags": [4],
    }
    fields_multi, _ = build_fields(multi_log_task)
    chk("多碟日志正确渲染独立 [hide=CD1 LOG]", "[hide=CD1 LOG]" in fields_multi["descr"])
    chk("多碟日志正确渲染独立 [hide=CD2 LOG]", "[hide=CD2 LOG]" in fields_multi["descr"])

    # 4d. 强制匿名发布验证
    chk("猫站 build_fields 默认强制匿名（uplver='yes'）", fields["uplver"] == "yes")
    fields_named, _ = build_fields(dummy_task, anonymous=False)
    chk("显式 anonymous=False 时猫站不含 uplver 键", "uplver" not in fields_named)

    print("=" * 60)
    print(f"自检结果: {'✅ 全部通过' if ok[0] else '❌ 存在失败项'}")
    return ok[0]


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
