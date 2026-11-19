# -*- coding: utf-8 -*-
"""MusicHub 3.0 详情页快照与本地离线回放缓存管理器 (RawHtmlSnapshotManager)

核心定位：
1. 【抓取与解析彻底解耦】：网络请求拉取的原始详情页 HTML 完整落盘至 data/raw_html/<site>_<tid>.html；
2. 【零网络重放验证】：测试、重构提取器规则或修复解析 Bug 时，直接重放本地快照，杜绝频繁请求站点触发 429 或封禁；
3. 【只读 Vertex TID 反查】：支持只读连接 Vertex sql.db 按 info_hash 反查原始发布页面与 TID。
"""

import os
import sqlite3
import logging
from typing import Optional, Tuple

logger = logging.getLogger("RawHtmlSnapshotManager")


class RawHtmlSnapshotManager:
    def __init__(self, raw_html_dir: Optional[str] = None):
        if raw_html_dir:
            self.raw_html_dir = raw_html_dir
        else:
            self.raw_html_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "raw_html"
            )
        os.makedirs(self.raw_html_dir, exist_ok=True)

    def save_snapshot(self, site: str, torrent_id: str, html_content: str) -> str:
        """保存原始页面快照"""
        if not site or not torrent_id or not html_content:
            return ""
        filename = f"{site.lower()}_{torrent_id}.html"
        filepath = os.path.join(self.raw_html_dir, filename)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(html_content)
            logger.debug(f"Saved snapshot to {filepath}")
            return filepath
        except Exception as e:
            logger.warning(f"Failed to save snapshot for {site} tid={torrent_id}: {e}")
            return ""

    def load_snapshot(self, site: str, torrent_id: str) -> Optional[str]:
        """读取本地快照"""
        filename = f"{site.lower()}_{torrent_id}.html"
        filepath = os.path.join(self.raw_html_dir, filename)
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Failed to load snapshot from {filepath}: {e}")
            return None

    def find_tid_from_vertex_db(self, vertex_db_path: str, info_hash: str) -> Optional[Tuple[str, str]]:
        """只读接入 Vertex sql.db，按 info_hash 反查原始站点与 torrent_id"""
        if not os.path.exists(vertex_db_path) or not info_hash:
            return None

        clean_hash = info_hash.strip().lower()
        # 严格使用 mode=ro 只读连接
        uri = f"file:{os.path.abspath(vertex_db_path)}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            # 常见 vertex 历史记录结构
            cur.execute(
                "SELECT site, link, torrent_name FROM rss_history WHERE info_hash = ? LIMIT 1",
                (clean_hash,),
            )
            row = cur.fetchone()
            if row:
                site = row["site"]
                link = row["link"] or ""
                # 从链接提取 id=xxx
                import re
                m = re.search(r"id=(\d+)", link)
                tid = m.group(1) if m else ""
                return site, tid
            return None
        except Exception as e:
            logger.warning(f"Error querying Vertex db ({vertex_db_path}): {e}")
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass
