# -*- coding: utf-8 -*-
"""MusicHub 已发布种子详情页归档与纯解析函数。

只归档发布账本中出现的站点 tid，不抓取站点全站。网络层复用 adapter 的
CookieCloud、Session 与逐站代理；数据库不保存 Cookie、请求头或响应头。
"""

import gzip
import hashlib
import json
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from musichub.routing.graph import RoutingGraphManager, normalize_site_key

PARSER_VERSION = "detail-v1"
SENSITIVE_KEYS = {"passkey", "token", "auth", "authkey", "secret", "key", "apikey", "api_key", "sid"}
SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:passkey|token|auth|authkey|secret|key|apikey|api_key|sid)=)[^&\s\"'<>]+"
)
COUNT_LABELS = {
    "seeders": ("seeders", "seeder", "做种者", "做種者", "种子数", "種子數"),
    "leechers": ("leechers", "leecher", "下载者", "下載者", "下载中", "下載中"),
    "completed": ("completed", "complete", "finished", "snatched", "完成数", "完成數", "完成"),
}


def sanitize_url(url: str) -> str:
    """移除 URL 中可能鉴别账号的查询参数值，保留可复查的页面地址。"""
    try:
        parts = urlsplit(url or "")
        query = urlencode([
            (k, "[redacted]" if k.lower() in SENSITIVE_KEYS else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    except Exception:
        return SENSITIVE_QUERY_RE.sub(r"\1[redacted]", str(url or ""))


def sanitize_html(html: str) -> str:
    return SENSITIVE_QUERY_RE.sub(r"\1[redacted]", html or "")


def _integer(value: str) -> Optional[int]:
    m = re.search(r"(?<!\d)(\d[\d,]*)(?!\d)", value or "")
    return int(m.group(1).replace(",", "")) if m else None


def _label_kind(label: str) -> str:
    low = (label or "").strip().lower()
    for kind, labels in COUNT_LABELS.items():
        if any(x in low for x in labels):
            return kind
    return ""


def parse_detail_html(html: str, base_url: str = "", current_tid: str = "") -> Dict[str, Any]:
    """解析 NexusPHP 详情页；未知计数保持 None，绝不拿 0 填充。"""
    soup = BeautifulSoup(html or "", "html.parser")
    title_node = soup.select_one("h1#top") or soup.select_one("h1")
    title = title_node.get_text(" ", strip=True) if title_node else ""
    descr_node = soup.select_one("#kdescr") or soup.select_one("#divdescr") or soup.select_one("div.divdescr")
    descr_text = descr_node.get_text("\n", strip=True) if descr_node else ""

    cover_urls: List[str] = []
    nodes = soup.select("div.cover img, #kdescr img, #divdescr img, div.divdescr img, img.cover")
    for node in nodes:
        raw = node.get("src") or node.get("data-src") or ""
        if raw:
            url = sanitize_url(urljoin((base_url or "").rstrip("/") + "/", raw))
            if url not in cover_urls:
                cover_urls.append(url)

    counts: Dict[str, Optional[int]] = {"seeders": None, "leechers": None, "completed": None}
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) < 2:
            continue
        kind = _label_kind(cells[0].get_text(" ", strip=True))
        if kind and counts[kind] is None:
            counts[kind] = _integer(" ".join(c.get_text(" ", strip=True) for c in cells[1:]))
    page_text = soup.get_text(" ", strip=True)
    for kind, labels in COUNT_LABELS.items():
        if counts[kind] is not None:
            continue
        for label in labels:
            m = re.search(r"(?i)(?:\b" + re.escape(label) + r"\b)\s*[:：]?\s*(\d[\d,]*)", page_text)
            if not m:
                m = re.search(r"(\d[\d,]*)\s*(?:个|位|名)?\s*" + re.escape(label), page_text, re.I)
            if m:
                counts[kind] = int(m.group(1).replace(",", ""))
                break

    source_links: List[str] = []
    base_host = urlsplit(base_url or "").netloc.lower()
    for a in soup.find_all("a", href=True):
        href = urljoin((base_url or "").rstrip("/") + "/", a["href"])
        if "details.php" not in href or "id=" not in href:
            continue
        parts = urlsplit(href)
        ids = dict(parse_qsl(parts.query)).get("id", "")
        if current_tid and ids == str(current_tid) and parts.netloc.lower() == base_host:
            continue
        if parts.netloc.lower() == base_host:
            continue
        clean = sanitize_url(href)
        if clean not in source_links:
            source_links.append(clean)

    low = (html or "").lower()
    login = ("<title>login" in low or "name=\"password\"" in low) and "details.php" not in low
    missing = any(x in page_text.lower() for x in (
        "torrent not found", "torrent does not exist", "种子不存在", "種子不存在",
        "不存在或已被删除", "不存在或已被刪除", "没有此种子", "沒有此種子",
    ))
    denied = any(x in page_text.lower() for x in (
        "你没有该权限", "你沒有該權限", "permission denied", "access denied", "not authorized",
    ))
    from musichub.collector.source_detail import extract_tags
    tags = extract_tags(soup)
    return {
        "title": title or None,
        "descr_text": descr_text or None,
        "cover_urls": cover_urls,
        "source_links": source_links,
        "tags": tags,
        "seeders": counts["seeders"],
        "leechers": counts["leechers"],
        "completed": counts["completed"],
        "page_status": "LOGIN_EXPIRED" if login else ("PAGE_MISSING" if missing else ("ACCESS_DENIED" if denied else "OK")),
    }


def build_adapter(site: str):
    from musichub.adapters.sites import (
        MusopiaAdapter, CrabPTAdapter, PTerClubAdapter, XingwanAdapter, HDKylinAdapter,
        TangPTAdapter, MuXueGeAdapter, LemonHDAdapter, UHDDolbyAdapter, HDTimeAdapter,
        OpenCDAdapter, UBitsAdapter, SoulVoiceAdapter, NanyangPTAdapter, SpringSundayAdapter,
    )
    mapping = {
        "lemonhd": LemonHDAdapter, "musopia": MusopiaAdapter, "crabpt": CrabPTAdapter,
        "pterclub": PTerClubAdapter, "xingwan": XingwanAdapter, "hdkylin": HDKylinAdapter,
        "tangpt": TangPTAdapter, "muxuege": MuXueGeAdapter, "uhddolby": UHDDolbyAdapter,
        "hdtime": HDTimeAdapter, "opencd": OpenCDAdapter, "ubits": UBitsAdapter,
        "soulvoice": SoulVoiceAdapter, "nanyangpt": NanyangPTAdapter, "springsunday": SpringSundayAdapter,
    }
    cls = mapping.get(normalize_site_key(site))
    if not cls:
        raise ValueError(f"未登记的站点: {site}")
    return cls()


def load_published_targets(
    db_path: str, site: str = "", tid: str = "", days: int = 0,
) -> List[Dict[str, Any]]:
    """合并三张发布表后全局去重排序；各源表不做 LIMIT，避免尾部永久漏扫。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    queries = [
        ("""SELECT target_site, target_tid, source_site, source_tid, source_name,
                   source_hash, b_hash, local_path, target_url, created_at, 3 AS priority
            FROM publish_lineage WHERE status='PUBLISHED'""", "created_at", "target_tid"),
        ("""SELECT target_site, target_torrent_id AS target_tid, '' AS source_site,
                   '' AS source_tid, album_name AS source_name, '' AS source_hash,
                   '' AS b_hash, '' AS local_path, '' AS target_url, created_at, 1 AS priority
            FROM publish_attempt_log WHERE result='PUBLISHED'""", "created_at", "target_torrent_id"),
        ("""SELECT target_site, target_torrent_id AS target_tid, origin_site AS source_site,
                   origin_torrent_id AS source_tid, '' AS source_name, '' AS source_hash,
                   '' AS b_hash, '' AS local_path, '' AS target_url,
                   published_at AS created_at, 2 AS priority
            FROM site_publish_graph""", "published_at", "target_torrent_id"),
    ]
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    try:
        for sql, time_col, tid_col in queries:
            where, args = [], []
            if days and days > 0:
                where.append(f"{time_col} >= datetime('now', ?)")
                args.append(f"-{int(days)} days")
            if site and site != "all":
                where.append("LOWER(target_site)=LOWER(?)")
                args.append(site)
            if tid:
                where.append(tid_col + "=?")
                args.append(str(tid))
            if where:
                connector = " AND " if " WHERE " in sql else " WHERE "
                sql += connector + " AND ".join(where)
            for row in conn.execute(sql, args):
                item = dict(row)
                key = (normalize_site_key(item["target_site"]), str(item["target_tid"] or ""))
                if not key[0] or not key[1].isdigit():
                    continue
                item["target_site"], item["target_tid"] = key
                old = merged.get(key)
                if old is None:
                    merged[key] = item
                    continue
                # 血缘信息优先；时间只取最新，不被后合并的低优先级空字段覆盖。
                if str(item.get("created_at") or "") > str(old.get("created_at") or ""):
                    old["created_at"] = item.get("created_at")
                for field in ("source_site", "source_tid", "source_name", "source_hash", "b_hash", "local_path", "target_url"):
                    if item.get(field) and (not old.get(field) or item["priority"] > old["priority"]):
                        old[field] = item[field]
                old["priority"] = max(old["priority"], item["priority"])
    finally:
        conn.close()
    return sorted(merged.values(), key=lambda r: (
        str(r.get("created_at") or ""), r["target_site"], int(r["target_tid"]),
    ))


class DetailArchive:
    def __init__(self, graph: RoutingGraphManager, adapters: Optional[Dict[str, Any]] = None,
                 delay: float = 0.8, timeout: int = 20):
        self.graph = graph
        self.adapters = adapters or {}
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self._last_request = 0.0

    def adapter(self, site: str):
        key = normalize_site_key(site)
        if key not in self.adapters:
            self.adapters[key] = build_adapter(key)
        return self.adapters[key]

    def fetch(
        self,
        site: str,
        tid: str,
        save_html: bool = False,
        html_dir: str = "data/detail_pages",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        site, tid = normalize_site_key(site), str(tid)
        now = time.time()
        page: Dict[str, Any] = {
            "site": site, "tid": tid, "fetched_at": now, "parser_version": PARSER_VERSION,
            "fetch_status": "FETCH_ERROR", "url": "", "http_status": None,
        }
        observation = {"page_status": "FETCH_ERROR", "fetched_at": now,
                       "seeders": None, "leechers": None, "completed": None}
        try:
            adapter = self.adapter(site)
            if site == "opencd":
                url = f"{adapter.base_url}/plugin_details.php?id={tid}"
            else:
                url = f"{adapter.base_url}/details.php?id={tid}"
            page["url"] = sanitize_url(url)
            wait = self.delay - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            resp = adapter.session.get(
                url, headers=adapter._prepare_headers(), timeout=self.timeout, verify=False,
            )
            page["http_status"] = resp.status_code
            page["url"] = sanitize_url(resp.url or url)
            html = resp.text or ""
            clean_html = sanitize_html(html)

            # 可选本地保存脱敏后的原始 HTML 文件
            if save_html:
                try:
                    os.makedirs(html_dir, exist_ok=True)
                    target_path = os.path.join(html_dir, f"{site}_{tid}.html")
                    with open(target_path, "w", encoding="utf-8") as fh:
                        fh.write(clean_html)
                    page["html_path"] = os.path.abspath(target_path)
                except Exception as exc:
                    page["error"] = f"save_html_error: {exc}"

            raw = clean_html.encode("utf-8", errors="replace")
            page["html_gzip"] = gzip.compress(raw, mtime=0)
            page["html_sha256"] = hashlib.sha256(raw).hexdigest()
            parsed = parse_detail_html(html, resp.url or url, current_tid=tid)
            status = parsed.pop("page_status")
            if resp.status_code == 404:
                status = "PAGE_MISSING"
            elif resp.status_code != 200 and status == "OK":
                status = "FETCH_ERROR"
            tags = parsed.get("tags", [])
            page.update({
                "fetch_status": status, "title": parsed["title"], "descr_text": parsed["descr_text"],
                "cover_urls_json": json.dumps(parsed["cover_urls"], ensure_ascii=False),
                "tags_json": json.dumps(tags, ensure_ascii=False),
                "parsed_json": json.dumps({
                    "source_links": parsed["source_links"], "seeders": parsed["seeders"],
                    "leechers": parsed["leechers"], "completed": parsed["completed"],
                    "tags": tags,
                }, ensure_ascii=False),
                "error": "" if status == "OK" else status,
            })
            observation.update({
                "page_status": status, "html_sha256": page["html_sha256"],
                "seeders": parsed["seeders"], "leechers": parsed["leechers"],
                "completed": parsed["completed"],
            })
        except Exception as exc:  # 单站/单页失败隔离，调用方继续后续目标
            page["error"] = f"{type(exc).__name__}: {exc}"[:1000]
        return page, observation

    def archive(
        self,
        site: str,
        tid: str,
        apply: bool = False,
        save_html: bool = False,
        html_dir: str = "data/detail_pages",
    ) -> Dict[str, Any]:
        page, observation = self.fetch(site, tid, save_html=save_html, html_dir=html_dir)
        if apply:
            self.graph.record_detail_page(page, observation)
        return {"page": page, "observation": observation, "written": bool(apply)}
