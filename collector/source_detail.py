# -*- coding: utf-8 -*-
"""MusicHub 源种详情页抓取（多源）

职责：
1. 把 3016 任务的 tracker / 链接映射到源站短名（8 个 NexusPHP 站）。
2. 用 CookieCloud 的登录态抓取 `details.php?id=` 页面，解析艺术家、专辑、发行年、
   简介正文、抓轨日志、曲目列表、封面与「轉載規則」。
3. 解析结果只返回字典，由 daemon 写入 `source_tasks`；本模块不接触发种。

解析思路来自已退役的 opencd2others/src/opencd.py（parse_detail / _descr_text_only /
_extract_tracklist / _extract_nfo_log），在本项目内重写为站点无关的通用版本：
- 字段表：按 td.rowhead 标签名匹配（繁體/简体都认）
- 正文：divdescr / #kdescr 优先，剥掉 <img>
- 日志：divnfo 内的 <pre>
- 曲目：divtracklist，按 <br> 分行
"""

import json
import os
import re
import socket
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlsplit

import requests
from bs4 import BeautifulSoup

from musichub.metadata.cookiecloud import CookieCloudClient

logger = logging.getLogger("SourceDetail")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 源站登记表：短名 -> 域名 / 显示名 / 站点根 / 详情页路径
# ★ OpenCD 的详情页是 `plugin_details.php`（`details.php` 对登录态也返回「未登錄!」）
SITE_INFO = {
    "opencd": {"domain": "open.cd", "display": "OpenCD", "base": "https://open.cd",
               "detail_path": "plugin_details.php"},
    "musopia": {"domain": "musopia.vip", "display": "乌托邦", "base": "https://www.musopia.vip",
                "detail_path": "details.php"},
    "crabpt": {"domain": "crabpt.vip", "display": "蟹黄堡", "base": "https://crabpt.vip",
               "detail_path": "details.php"},
    "pterclub": {"domain": "pterclub.net", "display": "猫站", "base": "https://pterclub.net",
                 "detail_path": "details.php"},
    "xingwan": {"domain": "xingwan.cc", "display": "星湾", "base": "https://xingwan.cc",
                "detail_path": "details.php"},
    "hdkylin": {"domain": "hdkylin.top", "display": "红豆麒麟", "base": "https://www.hdkylin.top",
                "detail_path": "details.php"},
    "tangpt": {"domain": "tangpt.top", "display": "躺平", "base": "https://tangpt.top",
               "detail_path": "details.php"},
    "muxuege": {"domain": "muxuege.org", "display": "牧雪阁", "base": "https://pt.muxuege.org",
                "detail_path": "details.php"},
    "uhddolby": {"domain": "uhddolby.com", "display": "UHDDolby", "base": "https://www.uhddolby.com",
                 "detail_path": "details.php"},
    "ubits": {"domain": "ubits.club", "display": "UBits", "base": "https://ubits.club",
              "detail_path": "details.php"},
    "soulvoice": {"domain": "pt.soulvoice.club", "display": "聆音", "base": "https://pt.soulvoice.club",
                  "detail_path": "details.php"},
    "nanyangpt": {"domain": "nanyangpt.com", "display": "南洋PT", "base": "https://nanyangpt.com",
                  "detail_path": "details.php"},
    "springsunday": {"domain": "springsunday.net", "display": "SSD", "base": "https://springsunday.net",
                     "detail_path": "details.php"},
}

# 转载规则三态（对齐 opencd2others RULE_ALLOW/DENY/UNKNOWN）
RULE_ALLOW = "ALLOW"
RULE_DENY = "DENY"
RULE_NEED_CONFIRM = "NEED_CONFIRM"


def detect_source_site(tracker_or_url: str) -> str:
    """从 tracker 地址或详情页链接识别源站短名；识别不出返回空串"""
    text = (tracker_or_url or "").lower()
    if not text:
        return ""
    for site, info in SITE_INFO.items():
        if info["domain"] in text:
            return site
    return ""


def site_display_name(site: str) -> str:
    """源站短名 -> 展示名（详情页「转自 X」文案用）"""
    info = SITE_INFO.get((site or "").strip().lower())
    return info["display"] if info else (site or "")


def site_base_url(site: str) -> str:
    info = SITE_INFO.get((site or "").strip().lower())
    return info["base"] if info else ""


def extract_tid(link: str) -> str:
    """从详情页链接里取 tid（details.php?id=123 或 plugin_details.php?id=123）"""
    match = re.search(r"(?:details|plugin_details)\.php\?id=(\d+)", link or "", re.IGNORECASE)
    return match.group(1) if match else ""


def map_transfer_rule(text: str) -> Optional[str]:
    """把站点「轉載規則」文案映射成三态；空白返回 None（表示该站没有此字段）"""
    raw = (text or "").strip()
    if not raw:
        return None
    if any(k in raw for k in ("歡迎轉載", "欢迎转载", "允许转载", "允許轉載")):
        return RULE_ALLOW
    if any(k in raw for k in ("禁止轉載", "禁止转载", "不允许转载", "不允許轉載", "謝絕轉載")):
        return RULE_DENY
    if any(k in raw for k in ("未經允許", "未经允许", "不得轉載", "不得转载", "保留")):
        return RULE_NEED_CONFIRM
    return RULE_NEED_CONFIRM


_LABEL_MAP = {
    "藝術家": "artist", "艺术家": "artist", "藝術家名": "artist", "艺术家名": "artist",
    "artist": "artist", "歌手": "artist",
    "專輯": "album", "专辑": "album", "album": "album", "名稱": "album", "名称": "album",
    "專輯名稱": "album", "专辑名称": "album",
    "發行年份": "year", "发行年份": "year", "year": "year", "年份": "year",
    "類別": "genre", "类别": "genre", "genre": "genre", "风格": "genre",
    "格式": "format", "format": "format",
    "媒介": "medium", "medium": "medium",
    "製作組": "team", "制作组": "team",
    "轉載規則": "transfer_rule", "转载规则": "transfer_rule",
}


def _clean_text(text: str) -> str:
    return re.sub(r"[ \t\u3000]+", " ", (text or "").replace("\r", "")).strip()


def extract_tags(soup: BeautifulSoup) -> List[Dict[str, str]]:
    """提取详情页中的全部标签（支持 OpenCD、UHDDolby、乌托邦及通用 NexusPHP 版式）。

    返回字典列表：[{"name": "...", "id": "...", "title": "..."}]
    """
    tags: List[Dict[str, str]] = []
    seen = set()

    def add_tag(name: str, tid: str = "", title: str = ""):
        name = name.strip()
        if not name or len(name) > 30:
            return
        key = (name, str(tid or ""))
        if key in seen:
            return
        seen.add(key)
        item = {"name": name}
        if tid:
            item["id"] = str(tid)
        if title and title != name:
            item["title"] = title
        tags.append(item)

    # 1. 查找标签单元格
    for cell in soup.find_all(["td", "th"]):
        txt = cell.get_text(" ", strip=True).rstrip(":：").strip()
        if txt in ("标签", "標籤", "Tags", "Tag", "標籤列表", "标签列表") or "標籤列表" in txt or "标签列表" in txt:
            sib = cell.find_next_sibling(["td", "th"])
            if not sib:
                continue
            # 优先看 a 标签（如 OpenCD <i><a href="torrents.php?tags=3">港台</a></i>）
            links = sib.find_all("a")
            if links:
                for a in links:
                    name = a.get_text(" ", strip=True)
                    href = a.get("href", "")
                    tid = ""
                    if href:
                        qs = dict(parse_qsl(urlsplit(href).query))
                        tid = qs.get("tags", "") or qs.get("tag", "") or qs.get("search_tags", "")
                    add_tag(name, tid=tid, title=a.get("title", ""))
            # 再看 span 标签（如 UHDDolby <span class="tags tgf" title="官方资源">官方</span>, Musopia <span style="...">原声(OST)</span>）
            spans = sib.find_all("span")
            if spans:
                for s in spans:
                    name = s.get_text(" ", strip=True) or s.get("title", "").strip()
                    cls_list = s.get("class", [])
                    if isinstance(cls_list, str):
                        cls_list = cls_list.split()
                    tid = ""
                    for c in cls_list:
                        if c == "tags":
                            continue
                        if c.startswith("tg") and len(c) >= 3:
                            tid = c[1:]  # tgf -> gf, tgy -> gy
                            break
                        elif c.startswith("t") and len(c) >= 2:
                            tid = c[1:]  # tzz -> zz, tgf -> gf
                            break
                    add_tag(name, tid=tid, title=s.get("title", ""))

            # 若无 a 和 span，检查其它行内标签（如 font, b, i, em, label, badge）
            if not tags:
                sub_elems = sib.find_all(["font", "b", "i", "em", "label", "button"])
                if sub_elems:
                    for el in sub_elems:
                        name = el.get_text(" ", strip=True) or el.get("title", "").strip()
                        add_tag(name, title=el.get("title", ""))
                else:
                    # 纯文本按常见分隔符切分
                    raw_txt = sib.get_text(" ", strip=True)
                    direct_tokens = [t.strip() for t in re.split(r"[\s,，|/、]+", raw_txt) if t.strip()]
                    for tok in direct_tokens:
                        if len(tok) <= 20 and not tok.startswith(("http", "www", "pic/")):
                            add_tag(tok)
            if tags:
                return tags

    # 2. 兜底全局 a[href*="tags="]
    for a in soup.find_all("a", href=re.compile(r"[?&](?:tags?|search_tags?)=")):
        name = a.get_text(" ", strip=True)
        href = a.get("href", "")
        qs = dict(parse_qsl(urlsplit(href).query))
        tid = qs.get("tags", "") or qs.get("tag", "") or qs.get("search_tags", "")
        add_tag(name, tid=tid, title=a.get("title", ""))

    return tags


def _field_rows(soup: BeautifulSoup) -> Dict[str, str]:
    """抓取详情页字段对（取首次出现）

    两种版式都支持：
    1. 标准 NexusPHP：`td.rowhead` 标签 + `td.rowfollow` 值（猫站/蟹黄堡等）；
    2. 插件详情页（OpenCD `plugin_details.php`、乌托邦）：没有 rowhead，
       字段以「藝術家名：」这类标签行排版，退回按文本行「标签：-> 值」配对。
    """
    fields: Dict[str, str] = {}
    for cell in soup.select("td.rowhead"):
        label = _clean_text(cell.get_text(" ", strip=True)).rstrip(":：")
        key = _LABEL_MAP.get(label.lower()) or _LABEL_MAP.get(label)
        if not key or key in fields:
            continue
        sibling = cell.find_next_sibling("td")
        if not sibling:
            continue
        fields[key] = _clean_text(sibling.get_text(" ", strip=True))

    lines = [ln.strip() for ln in soup.get_text("\n", strip=True).split("\n") if ln.strip()]
    for idx, line in enumerate(lines):
        if not line.endswith(("：", ":")):
            continue
        label = line.rstrip("：:")
        key = _LABEL_MAP.get(label.lower()) or _LABEL_MAP.get(label)
        if key and key not in fields and idx + 1 < len(lines):
            fields[key] = _clean_text(lines[idx + 1])
    return fields


def _extract_log_by_label(soup: BeautifulSoup) -> str:
    """乌托邦等站点把抓轨日志放在「抓轨 Log（共 N 碟）」行的右格，而不是 divnfo"""
    for cell in soup.select("td.rowhead"):
        label = _clean_text(cell.get_text(" ", strip=True)).lower()
        if "log" not in label and "抓轨" not in label:
            continue
        sibling = cell.find_next_sibling("td")
        if sibling is not None:
            text = _clean_text(sibling.get_text("\n", strip=True))
            if text:
                return text
    return ""


def _strip_images(node) -> str:
    """正文剥图：站点简介里的外链图不搬运，只留文字"""
    if node is None:
        return ""
    for img in node.find_all("img"):
        img.decompose()
    return _clean_text(node.get_text("\n", strip=True))


def _first_node(soup: BeautifulSoup, selectors):
    for selector in selectors:
        node = soup.select_one(selector)
        if node is not None:
            return node
    return None


def parse_detail_html(html: str) -> Dict[str, str]:
    """解析详情页 HTML，返回统一字段字典（不做任何网络动作，便于单测）"""
    soup = BeautifulSoup(html or "", "html.parser")
    fields = _field_rows(soup)

    title_node = _first_node(soup, ["h1#top", "h1", "title"])
    title = _clean_text(title_node.get_text(" ", strip=True)) if title_node else ""
    if title:
        # 标题常写成 `OpenCD :: 種子詳情 "艺人 - 专辑 年份 - 格式"`：
        # 取 `::` 分段里最长的一段（种子名），再剥引号
        parts = [p.strip() for p in re.split(r"\s*::\s*", title) if p.strip()]
        title = max(parts, key=len) if parts else ""
        title = re.sub(r"^(種子詳情|种子详情|種子|种子)\s*", "", title)
        title = title.strip('"“”「」 ')

    # 封面必须在剥图之前取，否则 #kdescr 里的 img 已被清掉
    cover_url = ""
    cover_node = _first_node(soup, ["div.cover img", "#kdescr img", "#divdescr img", "img.cover"])
    if cover_node is not None:
        cover_url = cover_node.get("src") or cover_node.get("data-src") or ""

    descr_node = _first_node(soup, ["#kdescr", "#divdescr", "div.divdescr", "td#descr"])
    description = _strip_images(descr_node)

    log_node = _first_node(soup, ["#divnfo pre", "#klog pre", "pre#log", "#divnfo"])
    log_text = ""
    if log_node is not None:
        pre = log_node.find("pre") if log_node.name != "pre" else log_node
        log_text = _clean_text((pre or log_node).get_text("\n", strip=True))
    if not log_text:
        log_text = _extract_log_by_label(soup)

    track_node = _first_node(soup, ["#divtracklist", "div.divtracklist", "#tracklist"])
    tracklist = ""
    if track_node is not None:
        for br in track_node.find_all("br"):
            br.replace_with("\n")
        tracklist = _clean_text(track_node.get_text("\n", strip=True))
        tracklist = re.sub(r"^曲目列表[:：]?\s*", "", tracklist)

    # 提取页面内的种子 TID（优先 download.php 下载链接或 canonical，避免被页头公告 details.php?id= 误导）
    page_tid = ""
    for pat in (
        r"download\.php\?id=(\d+)",
        r"<link[^>]+rel=[\"']canonical[\"'][^>]+id=(\d+)",
        r"(?:plugin_details|details)\.php\?id=(\d+)",
    ):
        m_tid = re.search(pat, html or "", re.I)
        if m_tid:
            page_tid = m_tid.group(1)
            break

    # 提取页面内的 40 位 InfoHash（NexusPHP 标准 Hash碼 / Hash码，兼容同一单元格或跨单元格）
    page_hash = ""
    m_hash = re.search(r"Hash[碼码].{0,100}?([0-9a-fA-F]{40})", html or "", re.I | re.S)
    if m_hash:
        page_hash = m_hash.group(1).lower()

    # 提取标签列表（多站点通用版式）
    tags = extract_tags(soup)
    tags_json = json.dumps(tags, ensure_ascii=False) if tags else ""
    tags_str = ", ".join(t["name"] for t in tags) if tags else ""

    result = {
        "title": title,
        "tid": page_tid,
        "info_hash": page_hash,
        "artist": fields.get("artist", ""),
        "album": fields.get("album", ""),
        "year": fields.get("year", ""),
        "genre": fields.get("genre", ""),
        "format": fields.get("format", ""),
        "medium": fields.get("medium", ""),
        "description": description,
        "log_text": log_text,
        "tracklist": tracklist,
        "cover_url": cover_url,
        "tags": tags,
        "tags_json": tags_json,
        "tags_str": tags_str,
    }
    result["transfer_rule"] = map_transfer_rule(fields.get("transfer_rule", ""))
    # 发行年只留 4 位数字（部分站点写「2001-05-01」）
    year_match = re.search(r"(19|20)\d{2}", result["year"] or "")
    result["year"] = year_match.group(0) if year_match else ""

    # OpenCD 等站点把流派风格放在「標籤列表」里，若正文 genre 为空则自动从标签回填风格
    if not result["genre"] and tags:
        genre_kws = ("流行", "摇滚", "古典", "爵士", "原声", "纯音", "民谣", "金属", "嘻哈", "电子", "ACG", "古风", "轻音乐", "节奏布鲁斯", "乡村", "世界", "民族", "器乐", "OST", "Rock", "Pop", "Jazz")
        matched = [t["name"] for t in tags if any(k in t["name"] for k in genre_kws)]
        if matched:
            result["genre"] = "/".join(matched)

    # 页面没有艺术家/专辑/年份行（乌托邦、OpenCD 插件页）时，退回从种子名解析
    if not result["artist"] or not result["album"] or not result["year"]:
        try:
            from musichub.metadata.audio_inspector import AudioInspector
            name_meta = AudioInspector.parse_name_metadata(title)
            result["artist"] = result["artist"] or name_meta.get("artist", "")
            result["album"] = result["album"] or name_meta.get("album", "")
            result["year"] = result["year"] or name_meta.get("year", "")
            result["format"] = result["format"] or name_meta.get("format", "")
        except Exception:
            pass
    return result


class SourceDetailFetcher:
    """按站点抓取详情页；失败只返回错误码，不抛异常（由调用方决定重试/降级）"""

    def __init__(self, cookie_client: Optional[CookieCloudClient] = None, timeout: int = 15):
        self.cookie_client = cookie_client or CookieCloudClient()
        self.timeout = timeout
        self.session = self._build_session()

    @staticmethod
    def _build_session() -> requests.Session:
        try:
            import urllib3.util.connection as u3c
            u3c.allowed_gai_family = lambda: socket.AF_INET  # 强制 IPv4，避免 IPv6 解析跑飞
        except Exception:
            pass
        session = requests.Session()
        session.trust_env = False  # 脱钩系统代理
        session.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        return session

    def fetch(
        self,
        site: str,
        tid: str,
        save_html: bool = False,
        html_dir: str = "data/detail_pages",
    ) -> Dict[str, Any]:
        """抓取并解析详情页；返回带 ok/error 的字典，可选保存脱敏后的 HTML 到本地"""
        info = SITE_INFO.get((site or "").strip().lower())
        if not info:
            return {"ok": False, "error": "unknown_site"}
        if not str(tid or "").strip():
            return {"ok": False, "error": "no_tid"}

        url = f"{info['base']}/{info.get('detail_path') or 'details.php'}?id={tid}"
        headers = {}
        cookie_header = self.cookie_client.get_cookie_header(info["domain"])
        if cookie_header:
            headers["Cookie"] = cookie_header

        try:
            resp = self.session.get(url, headers=headers, timeout=self.timeout, verify=False)
        except Exception as e:
            logger.warning(f"[{site}] fetch detail failed tid={tid}: {e}")
            return {"ok": False, "error": f"net:{type(e).__name__}"}

        if resp.status_code != 200:
            return {"ok": False, "error": f"http_{resp.status_code}"}
        if "未登錄" in resp.text or "未登录" in resp.text:
            return {"ok": False, "error": "login_required"}

        # 可选落盘保存脱敏后的原始 HTML 文件
        if save_html:
            try:
                from musichub.onsite.detail_archive import sanitize_html
                clean_html = sanitize_html(resp.text)
                os.makedirs(html_dir, exist_ok=True)
                target_path = os.path.join(html_dir, f"{site}_{tid}.html")
                with open(target_path, "w", encoding="utf-8") as fh:
                    fh.write(clean_html)
            except Exception as e:
                logger.warning(f"[{site}] 保存本地 HTML 失败: {e}")

        parsed = parse_detail_html(resp.text)
        if not parsed.get("title") and not parsed.get("description"):
            return {"ok": False, "error": "empty_page"}
        parsed["ok"] = True
        parsed["url"] = url
        parsed["source_site"] = (site or "").strip().lower()
        # 以调用方传入的请求 tid 为第一权威，严禁被 HTML 页头公告链接覆盖
        parsed["source_tid"] = str(tid or parsed.get("tid") or "").strip()
        if not parsed.get("info_hash"):
            parsed["info_hash"] = ""
        if save_html:
            parsed["html_path"] = os.path.abspath(target_path)
        return parsed
