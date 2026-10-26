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


def map_source(musopia_medium, format_name=None):
    """根据乌托邦媒介编号或格式名称映射为猫站 `source_sel`。

    - FLAC -> 8
    - WAV -> 9
    - WEB-DL -> 5
    - ISO -> 10
    - 其他 -> 15
    """
    fmt = (format_name or "").strip().upper()
    if "FLAC" in fmt:
        return SOURCE_FLAC
    if "WAV" in fmt:
        return SOURCE_WAV
    if "ISO" in fmt:
        return SOURCE_ISO
    if "WEB" in fmt:
        return SOURCE_WEB_DL

    # 尝试从 musopia_medium 反查名字
    want = _norm(musopia_medium)
    name = None
    for k, v in descr.MEDIUM.items():
        if _norm(v) == want:
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

    # 尝试从 audiocodec 反查
    for k, v in descr.AUDIOCODEC.items():
        if _norm(v) == want:
            if k.upper() == "FLAC":
                return SOURCE_FLAC
            if k.upper() == "WAV":
                return SOURCE_WAV

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


def map_processing(musopia_tags):
    """乌托邦/皇后标签编号列表 → (猫站 `team_sel` 值, 说明或 None)。"""
    tag_by_value = descr.TAGS
    notes = []
    for v in musopia_tags or []:
        want = _norm(v)
        name = None
        for k, val in tag_by_value.items():
            if _norm(val) == want:
                name = k
                break
        if name is None:
            continue
        if name in _PROCESSING_SPLIT_LOST:
            notes.append(f"皇后标签「{name}」：{_PROCESSING_SPLIT_LOST[name]}")
        if name in _PROCESSING_BY_MUSOPIA_TAG:
            val = _PROCESSING_BY_MUSOPIA_TAG[name]
            if len(notes) and not notes[-1].startswith(f"皇后标签「{name}」"):
                notes.append(f"同时命中多个地区标签 ⇒ 取「{name}」({val})")
            return val, ("；".join(notes) if notes else None)
    return TEAM_OTHER, ("；".join(notes) if notes else None)


def map_checkbox_tags(musopia_tags):
    """将通用标签集合改写为猫站独立 checkbox 字段字典。

    返回 `(checkbox_dict, audit_notes)`。
    勾选传 "yes"，未勾选不传。
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
    if "国语" in names:
        checkboxes["guoyu"] = "yes"
        audit.append("guoyu=yes (国语)")
    if "粤语" in names:
        checkboxes["yueyu"] = "yes"
        audit.append("yueyu=yes (粤语)")

    # 严禁勾选的黑名单说明
    for v, nm, why in dropped:
        audit.append(f"未勾选 {nm}（方向过滤：{why}）")

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


def build_fields(task, *, anonymous=False):
    """把一条 task 渲染成猫站 `takeupload.php` 的表单字段。

    返回 `(fields, audit)`：
      · `fields` = 供 `data=` 提交的 dict
      · `audit`  = 人读的字段来源说明
    """
    task = db.merge_mapped(dict(task))
    audit = []

    # 1. 标题：原样透传 formatted_title
    title = task.get("formatted_title") or task.get("torrent_name") or ""
    audit.append(f"name（主标题）← {title}")

    # 2. 副标题：复用 descr.build_small_descr_for_task
    small_descr = descr.build_small_descr_for_task(task)
    audit.append(f"small_descr（副标题）← {small_descr}")

    # 3. 来源声明（猫站强制要求 quote 出处）
    opencd_tid = task.get("opencd_tid")
    source_link = ""
    if opencd_tid:
        source_link = f"转自OpenCD: https://open.cd/details.php?id={opencd_tid}"
        audit.append(f"source_link（出处引用）← {source_link}")

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
    source = map_source(musopia_medium, format_name=format_hint)
    proc, proc_note = map_processing(_as_list(task.get("tags")))
    checkboxes, tag_audit = map_checkbox_tags(_as_list(task.get("tags")))

    audit.append(f"source_sel（媒介）← medium_sel={musopia_medium} ⇒ {source}")
    audit.append(f"team_sel（产地）← tags ⇒ {proc}" + (f"　★ {proc_note}" if proc_note else ""))
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
            r = self.post(action, data=data, files=files, allow_redirects=True)
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

        err = _extract_error(r.text or "")
        if err:
            lower_err = err.lower()
            if "already exists" in lower_err or "已存在" in err or "重复" in err:
                return None, f"REJECT: 种子已存在: {err}"
            return None, f"REJECT: 站点拒绝: {err}"

        # 从最终 URL 或页面内容中提取 tid
        tid = None
        m = re.search(r"[?&]id=(\d+)", r.url or "")
        if m:
            tid = m.group(1)
        if not tid:
            m = re.search(r"details\.php\?id=(\d+)", r.text or "")
            if m:
                tid = m.group(1)

        if tid:
            return tid, f"SUCCESS: 种子发布成功 (id={tid})"
        return None, "UNKNOWN: 提交未报错但未解析出种子 ID，请核查页面"


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
    chk("未知媒介兜底为 15 (Other)", map_source(None, "MP3") == 15)

    # 2. 产地映射
    proc_cn, _ = map_processing(["4"])
    chk("大陆标签映射为 1", proc_cn == 1)
    proc_eu, _ = map_processing(["5"])
    chk("欧美标签映射为 4", proc_eu == 4)
    proc_jp_kr, note = map_processing(["7"])
    chk("日韩标签约定映射为 6 (日本)", proc_jp_kr == 6 and "已知不精确" in (note or ""))

    # 3. Checkbox 标签与安全铁律
    cb, audit = map_checkbox_tags(["5", "6", "1", "2"])  # 含国语/港台及禁转/首发
    chk("禁转、原创、自购未进 checkbox", "jinzhuan" not in cb and "pr" not in cb and "bim" not in cb)

    # 4. build_fields 构造
    dummy_task = {
        "formatted_title": "Artist - Album 2026 - FLAC 16bit 44.1kHz",
        "smalltitle": "艺术家 - 专辑 | CD / Lossless / Log",
        "opencd_tid": "88888",
        "tags": [4],
        "medium_sel": 1,
        "torrent_name": "Artist - Album 2026 FLAC",
        "log_content": "EAC extraction logfile from 1. May 2026",
    }
    fields, audit_lines = build_fields(dummy_task)
    chk("主分类为 406 (Music)", fields["type"] == "406")
    chk("标题正确渲染", fields["name"] == dummy_task["formatted_title"])
    chk("媒介映射正确", fields["source_sel"] == 8)
    chk("产地映射正确", fields["team_sel"] == 1)
    chk("正文包含 [hide=Log]", "[hide=Log]" in fields["descr"])
    chk("正文包含转自 OpenCD 引用", "转自OpenCD: https://open.cd/details.php?id=88888" in fields["descr"])

    print("=" * 60)
    print(f"自检结果: {'✅ 全部通过' if ok[0] else '❌ 存在失败项'}")
    return ok[0]


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
