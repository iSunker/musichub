# -*- coding: utf-8 -*-
"""MusicHub 3.0 存量本地音乐池母种溯源解析器 (OriginResolver)

负责从物理磁盘目录反查母种（A 种）的源站、源 TID、源 Hash 与元数据：
1. 第一级：source_tasks 权威源种台账（精确 source_name 或 (artist, album)）
2. 第二级：site_publish_graph + publish_lineage 兄弟发布记录
3. 第三级：Vertex 原始抓取凭据库 (/app/vertex/sql.db)，优先源站 (OpenCD/Musopia) 倒查
"""

import os
import re
import json
import sqlite3
import logging
from typing import Tuple, Optional, List, Dict, Any

logger = logging.getLogger("OriginResolver")

VERTEX_DB_PATHS = [
    "/app/vertex/sql.db",
    "/volume2/docker_ssd/vertex2/data/vertex/db/sql.db",
]


def resolve_stock_origin_detail(
    folder_name: str,
    album: str = "",
    artist: str = "",
    full_path: str = "",
    conn: Optional[sqlite3.Connection] = None,
    db_path: str = "/app/musichub/data/musichub.db",
) -> Dict[str, Any]:
    """反查本地存量目录的源站母种完整信息（包含标签与流派）。"""
    origin_site = "opencd"
    origin_tid = ""
    origin_hash = ""
    cover_url = ""
    descr = ""
    origin_name = ""
    tags: List[str] = []
    genre: List[str] = []

    # 0. 规范化与清洗搜索关键词
    clean_artist = re.sub(r"^\s*(?:19\d{2}|20\d{2})\s*[-_ ]*\s*", "", str(artist or "").strip()).strip()
    clean_album = re.sub(
        r"\b(19\d{2}|20\d{2})\b|\b(FLAC|APE|WAV|DSD|MP3|CD|WEB-DL|WEB|SACD)\b|(?:分轨|整轨|分軌|整軌)|\b(Qobuz|Apple(?:\s*Music)?|Tidal|Deezer|Amazon(?:\s*Music)?|Spotify|Lossless|Hi-Res|Edition|Deluxe|Anniversary|Remaster(?:ed)?)\b|\b\d+Bit(?:-\d+(?:\.\d+)?(?:k|K)?Hz)?\b",
        "",
        str(album or "").strip(),
        flags=re.IGNORECASE
    ).strip()
    clean_album = re.sub(r"\s+-\s*$", "", clean_album)
    clean_album = re.sub(r"^\s*-\s+", "", clean_album)
    clean_album = re.sub(r"\s+", " ", clean_album).strip()

    # 1. 查询 source_tasks 或 site_publish_graph
    need_close = False
    if conn is None and os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            need_close = True
        except Exception as e:
            logger.debug(f"连接 musichub.db 失败: {e}")

    if conn is not None:
        try:
            cur = conn.cursor()
            # 1.1 先查 source_tasks
            q_st = (
                "SELECT * FROM source_tasks "
                "WHERE (source_name = ? OR (artist = ? AND album = ?) OR album = ? OR (artist = ? AND album = ?) OR album = ?) "
                "AND source_site != 'unknown' "
                "ORDER BY CASE WHEN source_site = 'opencd' THEN 1 ELSE 2 END ASC, rowid DESC LIMIT 1"
            )
            row = cur.execute(q_st, (folder_name, artist, album, album, clean_artist, clean_album, clean_album)).fetchone()
            if row and row["source_site"]:
                raw_tid = str(row["source_tid"] or "").strip()
                if raw_tid.isdigit():
                    origin_site = row["source_site"]
                    origin_tid = raw_tid
                    origin_hash = row["info_hash"] or ""
                    cover_url = row["cover_url"] or ""
                    descr = row["description"] or ""
                    origin_name = str(row["source_name"] or "").strip()

                    # 解析 tags_json
                    try:
                        raw_tags = row["tags_json"] if "tags_json" in row.keys() else None
                        if raw_tags:
                            parsed_tags = json.loads(raw_tags)
                            if isinstance(parsed_tags, list):
                                for pt in parsed_tags:
                                    if isinstance(pt, dict) and "name" in pt:
                                        tags.append(pt["name"])
                                    elif isinstance(pt, str):
                                        tags.append(pt)
                            elif isinstance(parsed_tags, dict) and "items" in parsed_tags:
                                for pt in parsed_tags["items"]:
                                    if isinstance(pt, dict) and "name" in pt:
                                        tags.append(pt["name"])
                    except Exception as te:
                        logger.debug(f"解析 source_tasks.tags_json 失败: {te}")

                    # 解析 genre_json
                    try:
                        raw_genre = row["genre_json"] if "genre_json" in row.keys() else None
                        if raw_genre:
                            if raw_genre.startswith("["):
                                genre = json.loads(raw_genre)
                            else:
                                genre = [g.strip() for g in raw_genre.replace("/", ",").split(",") if g.strip()]
                    except Exception as ge:
                        logger.debug(f"解析 source_tasks.genre_json 失败: {ge}")

            # 1.2 查 publish_lineage 兄弟记录
            if not origin_tid:
                q_pl = (
                    "SELECT source_site, source_tid, source_hash, source_name FROM publish_lineage "
                    "WHERE (source_name = ? OR local_path = ? OR note = ? OR note = ?) "
                    "AND source_tid GLOB '[0-9]*' "
                    "ORDER BY id DESC LIMIT 1"
                )
                pl_row = cur.execute(q_pl, (folder_name, full_path, album, clean_album)).fetchone()
                if pl_row:
                    s_site = str(pl_row[0] or "").lower()
                    s_tid = str(pl_row[1] or "").strip()
                    if s_tid.isdigit():
                        origin_site = s_site or origin_site
                        origin_tid = s_tid
                        origin_hash = str(pl_row[2] or "")
                        origin_name = str(pl_row[3] or "").strip()
        except Exception as e:
            logger.debug(f"反查 source_tasks/lineage 异常: {e}")
        finally:
            if need_close:
                conn.close()

    # 2. 查 Vertex sql.db 原始抓取记录
    if not origin_tid:
        for vdb in VERTEX_DB_PATHS:
            if not os.path.exists(vdb):
                continue
            try:
                vconn = sqlite3.connect(f"file:{vdb}?mode=ro", uri=True, timeout=10.0)
                vcur = vconn.cursor()
                q_v = (
                    "SELECT name, link, hash FROM torrents "
                    "WHERE (name = ? OR name LIKE ? OR (name LIKE ? AND name LIKE ?) "
                    "       OR (name LIKE ? AND name LIKE ?)) "
                    "ORDER BY "
                    "CASE "
                    "  WHEN link LIKE '%open.cd%' THEN 1 "
                    "  WHEN link LIKE '%musopia%' THEN 2 "
                    "  ELSE 3 "
                    "END ASC, id ASC"
                )
                search_artist = clean_artist or artist
                search_album = clean_album or album
                vrows = vcur.execute(
                    q_v,
                    (
                        folder_name,
                        f"%{search_album}%" if len(search_album) >= 3 else folder_name,
                        f"%{artist}%", f"%{album}%",
                        f"%{search_artist}%", f"%{search_album}%",
                    )
                ).fetchall()
                for vrow in vrows:
                    v_name, v_link, v_hash = vrow[0], vrow[1] or "", vrow[2] or ""
                    m_tid = re.search(r"details\.php\?id=(\d+)", v_link)
                    if m_tid:
                        tid_str = m_tid.group(1)
                        if "open.cd" in v_link:
                            origin_site = "opencd"
                            origin_tid = tid_str
                            origin_name = str(v_name or "").strip()
                            if not origin_hash and v_hash:
                                origin_hash = v_hash
                            break
                        elif "musopia" in v_link and not origin_tid:
                            origin_site = "musopia"
                            origin_tid = tid_str
                            origin_name = str(v_name or "").strip()
                            if not origin_hash and v_hash:
                                origin_hash = v_hash
                vconn.close()
                if origin_tid:
                    break
            except Exception as ve:
                logger.debug(f"反查 Vertex db 异常: {ve}")

    return {
        "origin_site": origin_site,
        "origin_tid": origin_tid,
        "origin_hash": origin_hash,
        "cover_url": cover_url,
        "descr": descr,
        "origin_name": origin_name,
        "tags": tags,
        "genre": genre,
    }


def resolve_stock_origin(
    folder_name: str,
    album: str = "",
    artist: str = "",
    full_path: str = "",
    conn: Optional[sqlite3.Connection] = None,
    db_path: str = "/app/musichub/data/musichub.db",
) -> Tuple[str, str, str, str, str, str]:
    """反查本地存量目录的源站母种信息（返回兼容的6元组）。"""
    res = resolve_stock_origin_detail(
        folder_name=folder_name,
        album=album,
        artist=artist,
        full_path=full_path,
        conn=conn,
        db_path=db_path,
    )
    return (
        res["origin_site"],
        res["origin_tid"],
        res["origin_hash"],
        res["cover_url"],
        res["descr"],
        res["origin_name"],
    )
