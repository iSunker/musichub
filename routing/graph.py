# -*- coding: utf-8 -*-
"""MusicHub 3.0 全局防环与发布溯源图治理模块

严格执行：
1. 双重指纹识别（内容规格指纹 + 音轨流指纹）
2. 溯源追踪：杜绝 Mesh 互转闭环（A -> B -> A 或 A -> B -> C -> A）
3. 全集群 InfoHash 查重拦截：同 InfoHash 全集群单实例
4. 站点硬约束：OpenCD 来源任务唯一归属 3260，严禁发布回源站
"""

import sqlite3
import os
import time
import json
import logging
import threading
from typing import List, Optional, Set, Dict, Any
from musichub.core.models import CanonicalMusicItem

logger = logging.getLogger(__name__)


# 站点 → 独立 tid 列（2026-11-21 新增）
#
# 背景：`site_publish_graph.origin_torrent_id` 历史取值混着三种形态——
# 数字 tid（正确）、40 位 infohash、以及专辑名。这个字段的语义被既有迁移脚本
# 与外部账本依赖，**不改造、不清洗**；改为每个站点各开一列，专存该站的数字 tid。
# 一行仍代表「某内容 → 某目标站」，行内 `<origin_site>_tid` 记源站 tid、
# `<target_site>_tid` 记目标站 tid；同一内容的所有行通过 content_fingerprint
# 互相补齐，所以任一行都能还原「这个内容在各站的 tid」。
SITE_TID_COLUMNS: Dict[str, str] = {
    "opencd": "opencd_tid",
    "musopia": "musopia_tid",
    "crabpt": "crabpt_tid",
    "pterclub": "pterclub_tid",
    "xingwan": "xingwan_tid",
    "hdkylin": "hdkylin_tid",
    "tangpt": "tangpt_tid",
    "muxuege": "muxuege_tid",
    "lemonhd": "lemonhd_tid",
    "uhddolby": "uhddolby_tid",
    "hdtime": "hdtime_tid",
    "ubits": "ubits_tid",
    "soulvoice": "soulvoice_tid",
    "nanyangpt": "nanyangpt_tid",
    "springsunday": "springsunday_tid",
}

# 站点别名归一（适配器 site_name 大小写不一、老数据用 pter）
SITE_ALIASES: Dict[str, str] = {"pter": "pterclub", "sv": "soulvoice", "nypt": "nanyangpt", "ssd": "springsunday"}


def normalize_site_key(site: str) -> str:
    """站点名归一为小写键；未知站点原样小写返回"""
    key = (site or "").strip().lower()
    return SITE_ALIASES.get(key, key)


def digit_tid(value) -> str:
    """只接受纯数字 tid；infohash / 专辑名 / 'EXISTING' 一律返回空串"""
    v = str(value or "").strip()
    return v if v.isdigit() else ""


def site_tid_column(site: str) -> str:
    """站点 → 该站 tid 列名；未登记站点返回空串（杜绝任意输入拼 SQL）"""
    return SITE_TID_COLUMNS.get(normalize_site_key(site), "")


def torrent_infohash(torrent_bytes) -> str:
    """从种子二进制算 infohash（台账用）；解析失败返回空串，绝不影响发种主流程"""
    if not torrent_bytes:
        return ""
    try:
        import io
        import torf

        t = torf.Torrent.read_stream(io.BytesIO(torrent_bytes))
        return str(getattr(t, "infohash", None) or getattr(t, "infohash_v1", "") or "").lower()
    except Exception:
        return ""



class RoutingGraphManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL + 30s busy_timeout：池索引全量回写是几千行的大事务，默认 rollback journal
        # 会把读事务和发种记账的写事务互相阻塞，实测触发 "database is locked"（2026-11-21）。
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
        except sqlite3.Error as e:
            logger.warning(f"[RoutingGraph] 设置 WAL/busy_timeout 失败: {e}")
        self._init_db()

    def _init_db(self):
        with self._lock:
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS site_publish_graph (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_fingerprint TEXT NOT NULL,
                stream_fingerprint TEXT,
                origin_site TEXT NOT NULL,
                origin_torrent_id TEXT,
                target_site TEXT NOT NULL,
                target_torrent_id TEXT,
                publish_status TEXT NOT NULL DEFAULT 'EXISTING',
                published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(content_fingerprint, target_site)
            );
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_graph_fingerprint 
            ON site_publish_graph(content_fingerprint);
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_graph_stream 
            ON site_publish_graph(stream_fingerprint);
            """)
            
            # 全集群 InfoHash 在册快照（支持同 hash 单实例排查 + 交棒索引快路）
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cluster_infohash_registry (
                info_hash TEXT NOT NULL,
                instance_name TEXT NOT NULL,
                instance_port INTEGER NOT NULL,
                site_name TEXT,
                name TEXT,
                size INTEGER,
                progress REAL,
                state TEXT,
                is_complete INTEGER DEFAULT 0,
                save_path TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at REAL,
                PRIMARY KEY (info_hash, instance_port)
            );
            """)
            self._migrate_registry()

            # 池入栈/出栈流水（append-only 审计；只增不改，用于复盘与对账）
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS pool_event_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                info_hash TEXT NOT NULL,
                pool_name TEXT NOT NULL,
                pool_port INTEGER NOT NULL,
                event TEXT NOT NULL,
                name TEXT,
                size INTEGER,
                save_path TEXT,
                actor TEXT,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pool_event_hash
            ON pool_event_log(info_hash);
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pool_event_created
            ON pool_event_log(created_at);
            """)

            # 发种尝试流水：成功/失败/跳过全部留痕，回答「哪些站点发了、哪些没发、为什么」
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS publish_attempt_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                album_name TEXT,
                target_site TEXT NOT NULL,
                result TEXT NOT NULL,
                target_torrent_id TEXT,
                reason TEXT,
                origin_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_attempt_site
            ON publish_attempt_log(target_site, created_at);
            """)

            # 源种任务表：等价老项目 opencd2others 的 tasks 语义
            # 回答「这个源种允不允许转、什么时候可转、元数据齐不齐」
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS source_tasks (
                info_hash TEXT PRIMARY KEY,
                source_site TEXT NOT NULL DEFAULT 'unknown',
                source_tid TEXT DEFAULT '',
                source_name TEXT DEFAULT '',
                transfer_rule TEXT NOT NULL DEFAULT 'NEED_CONFIRM',
                publish_after REAL DEFAULT 0,
                meta_status TEXT NOT NULL DEFAULT 'NEED_META',
                artist TEXT DEFAULT '',
                album TEXT DEFAULT '',
                year TEXT DEFAULT '',
                format TEXT DEFAULT 'FLAC',
                medium TEXT DEFAULT 'CD',
                standard TEXT DEFAULT 'Lossless',
                cover_url TEXT DEFAULT '',
                description TEXT DEFAULT '',
                log_text TEXT DEFAULT '',
                tracklist_json TEXT DEFAULT '',
                genre_json TEXT DEFAULT '',
                tags_json TEXT DEFAULT '',
                note TEXT DEFAULT '',
                fetched_at REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at REAL
            );
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_source_tasks_site
            ON source_tasks(source_site, meta_status);
            """)

            # 自有「源站 × 目标站」矩阵：只写显式关闭的组合，缺省一律放行
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS publish_matrix (
                source_site TEXT NOT NULL,
                target_site TEXT NOT NULL,
                allow INTEGER NOT NULL DEFAULT 1,
                note TEXT DEFAULT '',
                updated_at REAL,
                PRIMARY KEY (source_site, target_site)
            );
            """)

            # 发种来龙去脉台账：一条记录回答「源种是谁 → 发到哪站哪个 tid → 本地是哪个 B 种 →
            # 落在哪个 qB 实例/目录」。site_publish_graph 只有指纹与站点 ID，无法补种定位。
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS publish_lineage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_hash TEXT DEFAULT '',
                source_site TEXT DEFAULT '',
                source_tid TEXT DEFAULT '',
                source_url TEXT DEFAULT '',
                source_name TEXT DEFAULT '',
                local_path TEXT DEFAULT '',
                target_site TEXT NOT NULL,
                target_tid TEXT DEFAULT '',
                target_url TEXT DEFAULT '',
                b_hash TEXT DEFAULT '',
                qb_instance TEXT DEFAULT '',
                qb_port INTEGER DEFAULT 0,
                save_path TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'PUBLISHED',
                note TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_lineage_target
            ON publish_lineage(target_site, target_tid);
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_lineage_bhash
            ON publish_lineage(b_hash);
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_lineage_source
            ON publish_lineage(source_hash);
            """)

            # 封面缓存：回答「这个专辑在各站用的是哪条封面链」，避免重复上传图床。
            # 策略：源站图链有效 ⇒ 直接复用；仅无图/失效才扫本地补图并上传。
            # site_covers_json 按目标站存各自图链（crabpt 的 Lsky 链不得发给其他站）。
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cover_cache (
                album_key TEXT PRIMARY KEY,
                source_site TEXT DEFAULT '',
                source_tid TEXT DEFAULT '',
                source_name TEXT DEFAULT '',
                source_cover_url TEXT DEFAULT '',
                source_cover_status TEXT DEFAULT '',
                source_cover_checked_at REAL DEFAULT 0,
                local_dir TEXT DEFAULT '',
                local_image TEXT DEFAULT '',
                site_covers_json TEXT DEFAULT '{}',
                status TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_cover_cache_source
            ON cover_cache(source_site, source_tid);
            """)

            # 已发布种子详情页：每站每 tid 仅保留最后一次抓取结果；HTML 经 URL 敏感参数
            # 净化后 gzip 压缩。历史计数变化写入 observations，避免反复堆整页 HTML。
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS site_detail_pages (
                site TEXT NOT NULL,
                tid TEXT NOT NULL,
                url TEXT DEFAULT '',
                http_status INTEGER,
                fetch_status TEXT NOT NULL,
                title TEXT,
                descr_text TEXT,
                cover_urls_json TEXT DEFAULT '[]',
                parsed_json TEXT DEFAULT '{}',
                tags_json TEXT DEFAULT '[]',
                html_gzip BLOB,
                html_sha256 TEXT DEFAULT '',
                fetched_at REAL NOT NULL,
                parser_version TEXT NOT NULL,
                error TEXT DEFAULT '',
                PRIMARY KEY (site, tid)
            );
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_detail_pages_fetched
            ON site_detail_pages(fetched_at, site, tid);
            """)
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS site_detail_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT NOT NULL,
                tid TEXT NOT NULL,
                seeders INTEGER,
                leechers INTEGER,
                completed INTEGER,
                page_status TEXT NOT NULL,
                html_sha256 TEXT DEFAULT '',
                fetched_at REAL NOT NULL
            );
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_detail_observations_target
            ON site_detail_observations(site, tid, fetched_at, id);
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_detail_observations_fetched
            ON site_detail_observations(fetched_at, id);
            """)

            # 各池快照同步水位：只有全部池都新鲜时才允许走「查库」快路
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS pool_index_meta (
                pool_port INTEGER PRIMARY KEY,
                pool_name TEXT,
                synced_at REAL NOT NULL,
                torrent_count INTEGER NOT NULL
            );
            """)

            # 摄取流水台账：回答「这条 3016 源种（A 种）搬去了哪个实例 / 哪个目录 /
            # 什么时候删的源任务 / 有没有在目标池真做种」。
            # 2026-11-21 之前缺这张表，38 条完成任务只能靠 ls 反查落点，
            # 且「复制完成即删源」导致有文件无任务。
            # 只增不改（重试会多行），取最新一行即当前状态。
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS ingest_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                info_hash TEXT NOT NULL,
                source_site TEXT DEFAULT '',
                source_tid TEXT DEFAULT '',
                source_name TEXT DEFAULT '',
                target_instance TEXT DEFAULT '',
                target_port INTEGER DEFAULT 0,
                target_save_path TEXT DEFAULT '',
                host_path TEXT DEFAULT '',
                size_bytes INTEGER DEFAULT 0,
                file_count INTEGER DEFAULT 0,
                move_mode TEXT DEFAULT 'mv',
                status TEXT NOT NULL DEFAULT 'SEEDING',
                progress REAL DEFAULT 0,
                reason TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at REAL
            );
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ingest_hash
            ON ingest_log(info_hash, created_at);
            """)
            self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ingest_target
            ON ingest_log(target_instance, status);
            """)
            self._conn.commit()
            self._migrate_publish_status_column()
            self._migrate_site_tid_columns()
            self._migrate_source_tasks_columns()
            self._migrate_detail_pages_columns()

    def _migrate_detail_pages_columns(self):
        """给 site_detail_pages 补上 tags_json 列（幂等）。

        调用方需已持有 `self._lock`（`_init_db` 内部调用）。
        """
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(site_detail_pages)")}
        if "tags_json" not in cols:
            self._conn.execute(
                "ALTER TABLE site_detail_pages ADD COLUMN tags_json TEXT DEFAULT '[]'"
            )
            self._conn.commit()
            logger.info("[RoutingGraph] site_detail_pages 新增 tags_json 列 (DEFAULT '[]')")

    def _migrate_source_tasks_columns(self):
        """给 source_tasks 补上 tags_json 列（幂等）。

        调用方需已持有 `self._lock`（`_init_db` 内部调用）。
        """
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(source_tasks)")}
        if "tags_json" not in cols:
            self._conn.execute(
                "ALTER TABLE source_tasks ADD COLUMN tags_json TEXT DEFAULT ''"
            )
            self._conn.commit()
            logger.info("[RoutingGraph] source_tasks 新增 tags_json 列 (DEFAULT '')")

    def _migrate_publish_status_column(self):
        """给 site_publish_graph 补上 publish_status 列与索引（幂等）。

        调用方需已持有 `self._lock`（`_init_db` 内部调用）。
        """
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(site_publish_graph)")}
        if "publish_status" not in cols:
            self._conn.execute(
                "ALTER TABLE site_publish_graph ADD COLUMN publish_status TEXT NOT NULL DEFAULT 'EXISTING'"
            )
            self._conn.commit()
            logger.info("[RoutingGraph] site_publish_graph 新增 publish_status 列 (DEFAULT 'EXISTING')")
        self._conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_graph_site_status
        ON site_publish_graph(target_site, publish_status);
        """)
        self._conn.commit()

    def _migrate_site_tid_columns(self):
        """给 site_publish_graph 补上「每站点一列 tid」，并回填存量（幂等）。

        `origin_torrent_id` 原定义不动；新列只承担「某站的数字 tid」。
        仅在列真的缺失时才 ALTER + 回填，老库升级只付一次代价。
        调用方需已持有 `self._lock`（`_init_db` 内部调用）。
        """
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(site_publish_graph)")}
        missing = [s for s, c in SITE_TID_COLUMNS.items() if c not in cols]
        if not missing:
            return
        for site in missing:
            self._conn.execute(
                f"ALTER TABLE site_publish_graph ADD COLUMN {SITE_TID_COLUMNS[site]} TEXT"
            )
        self._conn.commit()
        stats = self._backfill_site_tids_locked()
        total = sum(stats.values())
        logger.info(
            f"[RoutingGraph] site_publish_graph 新增站点 tid 列 {len(missing)} 个，"
            f"回填 {total} 个值: {stats}"
        )

    def _fill_site_tid_locked(self, content_fingerprint: str, column: str, value: str):
        """把某站点的 tid 写进该内容指纹下的所有行（幂等覆盖；调用方持锁）"""
        if not column or not value:
            return 0
        cursor = self._conn.execute(
            f"UPDATE site_publish_graph SET {column} = ? WHERE content_fingerprint = ?",
            (value, content_fingerprint),
        )
        return cursor.rowcount or 0

    def _backfill_site_tids_locked(self) -> Dict[str, int]:
        """回填各站点 tid 列（只填空值，幂等；调用方持锁）。

        四路来源，优先级从高到低：
          ① 本行就是该站：`target_torrent_id`（纯数字）
          ② 本行源站就是该站：`origin_torrent_id`（纯数字；hash/专辑名跳过）
          ③ 同 content_fingerprint 的兄弟行已记过该站 tid → 传播过来
          ④ `publish_lineage` 的 (目标站, 目标 tid) → 源站 source_tid 兜底
        """
        stats: Dict[str, int] = {}
        for site, col in SITE_TID_COLUMNS.items():
            aliases = [site] + [a for a, s in SITE_ALIASES.items() if s == site]
            marks = ",".join("?" for _ in aliases)
            filled = 0

            cursor = self._conn.execute(
                f"UPDATE site_publish_graph SET {col} = TRIM(target_torrent_id) "
                f"WHERE LOWER(TRIM(target_site)) IN ({marks}) "
                f"AND ({col} IS NULL OR {col} = '') "
                "AND TRIM(COALESCE(target_torrent_id, '')) != '' "
                "AND TRIM(target_torrent_id) NOT GLOB '*[^0-9]*'",
                aliases,
            )
            filled += cursor.rowcount or 0

            cursor = self._conn.execute(
                f"UPDATE site_publish_graph SET {col} = TRIM(origin_torrent_id) "
                f"WHERE LOWER(TRIM(origin_site)) IN ({marks}) "
                f"AND ({col} IS NULL OR {col} = '') "
                "AND TRIM(COALESCE(origin_torrent_id, '')) != '' "
                "AND TRIM(origin_torrent_id) NOT GLOB '*[^0-9]*'",
                aliases,
            )
            filled += cursor.rowcount or 0

            cursor = self._conn.execute(
                f"UPDATE site_publish_graph AS g SET {col} = ("
                f"  SELECT MAX(x.{col}) FROM site_publish_graph x "
                f"  WHERE x.content_fingerprint = g.content_fingerprint "
                f"    AND x.{col} IS NOT NULL AND x.{col} != ''"
                f") WHERE (g.{col} IS NULL OR g.{col} = '') AND EXISTS ("
                f"  SELECT 1 FROM site_publish_graph x "
                f"  WHERE x.content_fingerprint = g.content_fingerprint "
                f"    AND x.{col} IS NOT NULL AND x.{col} != ''"
                f")"
            )
            filled += cursor.rowcount or 0

            lineage_where = (
                "LOWER(TRIM(l.target_site)) = LOWER(TRIM(g.target_site)) "
                "AND TRIM(l.target_tid) = TRIM(g.target_torrent_id) "
                f"AND LOWER(TRIM(l.source_site)) IN ({marks}) "
                "AND TRIM(COALESCE(l.source_tid, '')) != '' "
                "AND TRIM(l.source_tid) NOT GLOB '*[^0-9]*'"
            )
            try:
                cursor = self._conn.execute(
                    f"UPDATE site_publish_graph AS g SET {col} = ("
                    f"  SELECT l.source_tid FROM publish_lineage l "
                    f"  WHERE {lineage_where} ORDER BY l.id DESC LIMIT 1"
                    f") WHERE (g.{col} IS NULL OR g.{col} = '') AND EXISTS ("
                    f"  SELECT 1 FROM publish_lineage l WHERE {lineage_where}"
                    f")",
                    aliases + aliases,
                )
                filled += cursor.rowcount or 0
            except sqlite3.Error as e:
                logger.warning(f"[RoutingGraph] 台账兜底回填 {col} 失败: {e}")

            stats[site] = filled
        self._conn.commit()
        return stats

    def backfill_site_tids(self) -> Dict[str, int]:
        """公开入口：重跑站点 tid 回填（只补空值，可重复执行）"""
        with self._lock:
            return self._backfill_site_tids_locked()

    def get_site_tids(self, content_fingerprint: str) -> Dict[str, str]:
        """取同一内容在各站已登记的 tid：`{站点: tid}`（无记录返回 `{}`）"""
        fp = (content_fingerprint or "").strip()
        if not fp:
            return {}
        select_parts = ", ".join(
            f"MAX({c}) AS {c}" for c in SITE_TID_COLUMNS.values()
        )
        with self._lock:
            row = self._conn.execute(
                f"SELECT {select_parts} FROM site_publish_graph WHERE content_fingerprint = ?",
                (fp,),
            ).fetchone()
        if row is None:
            return {}
        return {
            site: str(row[col])
            for site, col in SITE_TID_COLUMNS.items()
            if row[col] not in (None, "")
        }

    def set_site_tid(self, content_fingerprint: str, site: str, tid: str) -> bool:
        """补写某站在该内容下的 tid（如 EXISTING 转正）；非数字 tid 一律拒写"""
        fp = (content_fingerprint or "").strip()
        col = site_tid_column(site)
        val = digit_tid(tid)
        if not fp or not col or not val:
            return False
        with self._lock:
            n = self._fill_site_tid_locked(fp, col, val)
            self._conn.commit()
        return n > 0

    # ==================== 已发布详情页归档 ====================

    def record_detail_page(self, page: Dict[str, Any], observation: Dict[str, Any]) -> None:
        """原子写入最新详情页与一次历史观测；数值未知必须保持 NULL。"""
        site = normalize_site_key(str(page.get("site") or ""))
        tid = digit_tid(page.get("tid"))
        if not site or not tid:
            raise ValueError("详情页归档要求已登记的站点与纯数字 tid")
        fetched_at = float(page.get("fetched_at") or time.time())
        tags_json = page.get("tags_json")
        if not tags_json and page.get("parsed_json"):
            try:
                pj = json.loads(page["parsed_json"])
                if "tags" in pj and isinstance(pj["tags"], list):
                    tags_json = json.dumps(pj["tags"], ensure_ascii=False)
            except Exception:
                pass
        if not tags_json:
            tags_json = "[]"
        with self._lock:
            self._conn.execute("""
            INSERT INTO site_detail_pages
                (site, tid, url, http_status, fetch_status, title, descr_text,
                 cover_urls_json, parsed_json, tags_json, html_gzip, html_sha256, fetched_at,
                 parser_version, error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(site, tid) DO UPDATE SET
                url=excluded.url, http_status=excluded.http_status,
                fetch_status=excluded.fetch_status, title=excluded.title,
                descr_text=excluded.descr_text, cover_urls_json=excluded.cover_urls_json,
                parsed_json=excluded.parsed_json, tags_json=excluded.tags_json,
                html_gzip=excluded.html_gzip,
                html_sha256=excluded.html_sha256, fetched_at=excluded.fetched_at,
                parser_version=excluded.parser_version, error=excluded.error
            """, (
                site, tid, page.get("url") or "", page.get("http_status"),
                page.get("fetch_status") or "FETCH_ERROR", page.get("title"),
                page.get("descr_text"), page.get("cover_urls_json") or "[]",
                page.get("parsed_json") or "{}", tags_json, page.get("html_gzip"),
                page.get("html_sha256") or "", fetched_at,
                page.get("parser_version") or "unknown", (page.get("error") or "")[:1000],
            ))
            self._conn.execute("""
            INSERT INTO site_detail_observations
                (site, tid, seeders, leechers, completed, page_status, html_sha256, fetched_at)
            VALUES (?,?,?,?,?,?,?,?)
            """, (
                site, tid, observation.get("seeders"), observation.get("leechers"),
                observation.get("completed"), observation.get("page_status") or page.get("fetch_status") or "FETCH_ERROR",
                observation.get("html_sha256") or page.get("html_sha256") or "",
                float(observation.get("fetched_at") or fetched_at),
            ))
            self._conn.commit()

    def get_detail_page(self, site: str, tid: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM site_detail_pages WHERE site=? AND tid=?",
                (normalize_site_key(site), digit_tid(tid)),
            ).fetchone()
            return dict(row) if row else {}

    def list_detail_pages(
        self, site: str = "", limit: int = 100,
        after: Optional[tuple] = None,
    ) -> List[Dict[str, Any]]:
        """按 (fetched_at, site, tid) 稳定 keyset 分页；limit<=0 表示不截断。"""
        where, args = [], []
        if site and site != "all":
            where.append("site=?")
            args.append(normalize_site_key(site))
        if after:
            where.append("(fetched_at, site, tid) > (?,?,?)")
            args.extend([float(after[0]), normalize_site_key(str(after[1])), digit_tid(after[2])])
        sql = "SELECT * FROM site_detail_pages"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY fetched_at, site, tid"
        if limit and limit > 0:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args)]

    def get_detail_history(self, site: str, tid: str, limit: int = 100) -> List[Dict[str, Any]]:
        sql = """SELECT * FROM site_detail_observations WHERE site=? AND tid=?
                 ORDER BY fetched_at DESC, id DESC"""
        args: List[Any] = [normalize_site_key(site), digit_tid(tid)]
        if limit and limit > 0:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args)]

    def list_pending_detail_targets(
        self,
        limit: int = 20,
        sites: Optional[List[str]] = None,
        max_age_seconds: float = 86400.0,
    ) -> List[Dict[str, Any]]:
        """挑选全矩阵待抓取详情页的目标（包含 12 个目标站已发布的种子与源站种子）。

        优先级：
        1. 优先选择从未抓取（site_detail_pages 中无记录）或上一次抓取失败（fetch_status != 'OK'）的目标；
        2. 其次选择已抓取但 fetched_at 距今超过 max_age_seconds 的目标（按 fetched_at ASC 排序）；
        3. 结果按站点交替打散，保证多站点均衡采样。
        """
        now = time.time()
        target_sites = [normalize_site_key(s) for s in sites] if sites else []

        with self._lock:
            candidates_map: Dict[Tuple[str, str], Dict[str, Any]] = {}

            # 1. 来自 site_publish_graph
            cur = self._conn.execute("""
                SELECT target_site, target_torrent_id, origin_site, origin_torrent_id
                FROM site_publish_graph
                WHERE publish_status = 'PUBLISHED'
                  AND target_torrent_id GLOB '[0-9]*'
                  AND length(target_torrent_id) > 0
            """)
            for row in cur.fetchall():
                s = normalize_site_key(row[0])
                tid = str(row[1]).strip()
                if not s or not tid.isdigit():
                    continue
                if target_sites and s not in target_sites:
                    continue
                candidates_map[(s, tid)] = {
                    "site": s,
                    "tid": tid,
                    "origin_site": row[2] or "",
                    "origin_tid": row[3] or "",
                }

            # 2. 补充来自 publish_lineage
            cur = self._conn.execute("""
                SELECT target_site, target_tid, source_site, source_tid, source_hash
                FROM publish_lineage
                WHERE status = 'PUBLISHED'
                  AND target_tid GLOB '[0-9]*'
                  AND length(target_tid) > 0
            """)
            for row in cur.fetchall():
                s = normalize_site_key(row[0])
                tid = str(row[1]).strip()
                if not s or not tid.isdigit():
                    continue
                if target_sites and s not in target_sites:
                    continue
                if (s, tid) not in candidates_map:
                    candidates_map[(s, tid)] = {
                        "site": s,
                        "tid": tid,
                        "origin_site": row[2] or "",
                        "origin_tid": row[3] or "",
                        "source_hash": row[4] or "",
                    }
                elif row[4]:
                    candidates_map[(s, tid)]["source_hash"] = row[4]

            if not candidates_map:
                return []

            # 3. 查 site_detail_pages 的当前状态
            detail_pages = {
                (r[0], r[1]): (r[2], float(r[3] or 0.0))
                for r in self._conn.execute("SELECT site, tid, fetch_status, fetched_at FROM site_detail_pages")
            }

            never_fetched: List[Dict[str, Any]] = []
            stale_fetched: List[Dict[str, Any]] = []

            for key, cand in candidates_map.items():
                if key not in detail_pages:
                    never_fetched.append(cand)
                else:
                    status, fetched_at = detail_pages[key]
                    if status != "OK":
                        never_fetched.append(cand)
                    elif (now - fetched_at) >= max_age_seconds:
                        cand_copy = dict(cand)
                        cand_copy["fetched_at"] = fetched_at
                        stale_fetched.append(cand_copy)

            stale_fetched.sort(key=lambda x: x.get("fetched_at", 0.0))

            pool = never_fetched + stale_fetched
            if not pool:
                return []

            # 4. 按站点均匀轮询交替采样（Round-Robin）
            by_site: Dict[str, List[Dict[str, Any]]] = {}
            for item in pool:
                by_site.setdefault(item["site"], []).append(item)

            result: List[Dict[str, Any]] = []
            site_keys = list(by_site.keys())
            idx = 0
            while len(result) < limit and any(by_site.values()):
                s = site_keys[idx % len(site_keys)]
                idx += 1
                if by_site[s]:
                    result.append(by_site[s].pop(0))

            return result

    def _migrate_registry(self):
        """把旧版单主键 registry 升级为「hash+实例」复合主键的完整快照表

        旧表历史上从未被任何代码写入（零行），因此重建不丢数据；若意外非空，
        先读出四列再重建回填，保证可重复执行。
        """
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(cluster_infohash_registry)")]
        if "is_complete" in cols:
            return
        legacy = []
        if cols:
            legacy = self._conn.execute(
                "SELECT info_hash, instance_name, instance_port, site_name FROM cluster_infohash_registry"
            ).fetchall()
            self._conn.execute("DROP TABLE cluster_infohash_registry")
        self._conn.execute("""
        CREATE TABLE cluster_infohash_registry (
            info_hash TEXT NOT NULL,
            instance_name TEXT NOT NULL,
            instance_port INTEGER NOT NULL,
            site_name TEXT,
            name TEXT,
            size INTEGER,
            progress REAL,
            state TEXT,
            is_complete INTEGER DEFAULT 0,
            save_path TEXT,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at REAL,
            PRIMARY KEY (info_hash, instance_port)
        );
        """)
        for row in legacy:
            self._conn.execute(
                "INSERT OR IGNORE INTO cluster_infohash_registry "
                "(info_hash, instance_name, instance_port, site_name) VALUES (?,?,?,?)",
                tuple(row),
            )

    def is_loop_or_already_published(self, item: CanonicalMusicItem, target_site: str) -> bool:
        """检查发布到目标站点是否构成回环或已发布"""
        tgt = normalize_site_key(target_site)
        orig = normalize_site_key(item.origin_site)

        # 规则 1：目标站点与原始抓取站点相同 -> 直接构成回环
        if tgt == orig or (tgt == "pterclub" and orig == "pter") or (tgt == "pter" and orig == "pterclub"):
            return True

        content_fp = item.calculate_content_fingerprint()
        stream_fp = item.calculate_stream_fingerprint()

        with self._lock:
            # 规则 2：内容指纹已在目标站记录
            cursor = self._conn.execute(
                "SELECT 1 FROM site_publish_graph WHERE content_fingerprint = ? AND target_site = ?",
                (content_fp, tgt)
            )
            if cursor.fetchone():
                return True

            # 规则 3：音轨物理流指纹命中目标站记录
            if stream_fp:
                cursor = self._conn.execute(
                    "SELECT 1 FROM site_publish_graph WHERE stream_fingerprint = ? AND target_site = ?",
                    (stream_fp, tgt)
                )
                if cursor.fetchone():
                    return True

        return False

    def check_infohash_exists_in_cluster(self, info_hash: str) -> Optional[Dict[str, Any]]:
        """检查 InfoHash 是否已登记在集群的某一实例中"""
        clean_hash = (info_hash or "").strip().lower()
        if not clean_hash:
            return None

        with self._lock:
            cursor = self._conn.execute(
                "SELECT instance_name, instance_port, site_name, name, size, is_complete, "
                "registered_at, updated_at FROM cluster_infohash_registry WHERE info_hash = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (clean_hash,)
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
        return None

    def register_cluster_infohash(
        self,
        info_hash: str,
        instance_name: str,
        instance_port: int,
        site_name: str = "",
        name: str = "",
        size: int = 0,
        progress: float = 1.0,
        state: str = "",
        save_path: str = "",
        is_complete: bool = True,
    ):
        """登记 / 刷新一条在册记录（配对注入等主动性入栈动作直接写这里）"""
        clean_hash = (info_hash or "").strip().lower()
        if not clean_hash:
            return

        with self._lock:
            self._conn.execute("""
            INSERT INTO cluster_infohash_registry
                (info_hash, instance_name, instance_port, site_name, name, size,
                 progress, state, is_complete, save_path, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(info_hash, instance_port) DO UPDATE SET
                instance_name = excluded.instance_name,
                name = excluded.name,
                size = excluded.size,
                progress = excluded.progress,
                state = excluded.state,
                is_complete = excluded.is_complete,
                save_path = excluded.save_path,
                updated_at = excluded.updated_at;
            """, (
                clean_hash, instance_name, int(instance_port), site_name or "",
                name or "", int(size or 0), float(progress), state or "",
                1 if is_complete else 0, save_path or "", time.time(),
            ))
            self._conn.commit()

    def sync_pool_index(
        self,
        pool_name: str,
        pool_port: int,
        torrents: List[Dict[str, Any]],
        synced_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        """用一次全量拉取的实况覆盖某池的在册快照，返回本次增删差异

        差异即为「入栈 / 出栈」事实，调用方据此写 pool_event_log。
        """
        now = float(synced_at if synced_at is not None else time.time())
        port = int(pool_port)
        live: Dict[str, Dict[str, Any]] = {}
        for t in torrents or []:
            h = (t.get("hash") or "").strip().lower()
            if h:
                live[h] = t

        with self._lock:
            cursor = self._conn.execute(
                "SELECT info_hash FROM cluster_infohash_registry WHERE instance_port = ?", (port,)
            )
            known = {r[0] for r in cursor}
            added = sorted(set(live) - known)
            removed = sorted(known - set(live))

            if removed:
                self._conn.executemany(
                    "DELETE FROM cluster_infohash_registry WHERE info_hash = ? AND instance_port = ?",
                    [(h, port) for h in removed],
                )

            rows = []
            for h, t in live.items():
                size = int(t.get("size") or 0)
                progress = float(t.get("progress") or 0.0)
                amount_left = t.get("amount_left")
                complete = progress >= 1.0 or (
                    amount_left is not None and int(amount_left) == 0 and size > 0
                )
                rows.append((
                    h, pool_name, port, t.get("name") or "", size, progress,
                    t.get("state") or "", 1 if complete else 0,
                    t.get("save_path") or "", now,
                ))
            # 分块提交：单池 2600+ 行的大事务会长时间持有写锁，
            # 把发种记账的 INSERT 堵到 5s 超时（2026-11-21 database is locked）。
            chunk = 300
            for i in range(0, len(rows), chunk):
                self._conn.executemany("""
                INSERT INTO cluster_infohash_registry
                    (info_hash, instance_name, instance_port, name, size, progress,
                     state, is_complete, save_path, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(info_hash, instance_port) DO UPDATE SET
                    instance_name = excluded.instance_name,
                    name = excluded.name,
                    size = excluded.size,
                    progress = excluded.progress,
                    state = excluded.state,
                    is_complete = excluded.is_complete,
                    save_path = excluded.save_path,
                    updated_at = excluded.updated_at;
                """, rows[i:i + chunk])
                self._conn.commit()

            self._conn.execute("""
            INSERT INTO pool_index_meta (pool_port, pool_name, synced_at, torrent_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(pool_port) DO UPDATE SET
                pool_name = excluded.pool_name,
                synced_at = excluded.synced_at,
                torrent_count = excluded.torrent_count;
            """, (port, pool_name, now, len(live)))
            self._conn.commit()

        return {"added": added, "removed": removed, "total": len(live)}

    def get_pool_index_rows(self) -> List[Dict[str, Any]]:
        """读取全部在册快照行（供交棒器构造 hash / name 索引）"""
        with self._lock:
            cursor = self._conn.execute("""
                SELECT info_hash, instance_name, instance_port, name, size,
                       progress, state, is_complete, save_path, updated_at
                FROM cluster_infohash_registry
            """)
            return [dict(r) for r in cursor]

    def get_pool_index_meta(self) -> Dict[int, Dict[str, Any]]:
        """读取各池同步水位，键为端口"""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT pool_port, pool_name, synced_at, torrent_count FROM pool_index_meta"
            )
            return {int(r["pool_port"]): dict(r) for r in cursor}

    def record_publish_attempt(
        self,
        album_name: str,
        target_site: str,
        result: str,
        reason: str = "",
        target_torrent_id: str = "",
        origin_path: str = "",
    ):
        """记录一次发种尝试（成功/失败/跳过都写）

        result 取值：PUBLISHED / SKIPPED_EXISTING / SKIPPED / REJECTED / FAILED
        """
        if not target_site or not result:
            return
        try:
            with self._lock:
                self._conn.execute("""
                INSERT INTO publish_attempt_log
                    (album_name, target_site, result, target_torrent_id, reason, origin_path)
                VALUES (?, ?, ?, ?, ?, ?);
                """, (
                    album_name or "", target_site, result, str(target_torrent_id or ""),
                    (reason or "")[:500], origin_path or "",
                ))
                self._conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"[RoutingGraph] 写发种流水失败: {e}")

    # ==================== 封面缓存 ====================

    def get_cover_cache(self, album_key: str) -> Dict[str, Any]:
        """读取一条封面缓存；无记录返回 `{}`（不是 None）。"""
        key = (album_key or "").strip()
        if not key:
            return {}
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cover_cache WHERE album_key = ?", (key,)
            ).fetchone()
            return dict(row) if row else {}

    def upsert_cover_cache(self, album_key: str, **fields) -> None:
        """按 album_key 写入/更新封面缓存（只覆盖传入字段）。

        `site_covers_json` 特殊处理：传入的 JSON 会与库中已有值**合并**，
        避免写一个站点的封面时把其他站点已上传的图链冲掉。
        """
        key = (album_key or "").strip()
        if not key:
            return
        payload = {k: v for k, v in fields.items() if k in {
            "source_site", "source_tid", "source_name", "source_cover_url",
            "source_cover_status", "source_cover_checked_at", "local_dir",
            "local_image", "site_covers_json", "status", "reason",
        }}
        if not payload:
            return
        try:
            with self._lock:
                existing = self._conn.execute(
                    "SELECT site_covers_json FROM cover_cache WHERE album_key = ?", (key,)
                ).fetchone()
                if "site_covers_json" in payload:
                    try:
                        merged = json.loads((existing[0] if existing else "") or "{}")
                    except (ValueError, TypeError):
                        merged = {}
                    try:
                        merged.update(json.loads(payload["site_covers_json"] or "{}"))
                    except (ValueError, TypeError):
                        pass
                    payload["site_covers_json"] = json.dumps(merged, ensure_ascii=False)

                cols = ", ".join(payload.keys())
                marks = ", ".join("?" for _ in payload)
                self._conn.execute(
                    f"INSERT INTO cover_cache (album_key, {cols}) VALUES (?, {marks}) "
                    f"ON CONFLICT(album_key) DO UPDATE SET "
                    + ", ".join(f"{k} = excluded.{k}" for k in payload.keys())
                    + ", updated_at = CURRENT_TIMESTAMP",
                    (key, *payload.values()),
                )
                self._conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"[RoutingGraph] 写封面缓存失败: {e}")

    def get_site_cover_url(self, album_key: str, target_site: str) -> str:
        """取该专辑在目标站已落库的封面 URL；没有返回 `""`。"""
        row = self.get_cover_cache(album_key)
        if not row:
            return ""
        try:
            mapping = json.loads(row.get("site_covers_json") or "{}")
        except (ValueError, TypeError):
            return ""
        return str(mapping.get((target_site or "").strip().lower()) or "")

    def set_site_cover_url(self, album_key: str, target_site: str, url: str) -> None:
        """登记该专辑在目标站的封面 URL（**空 URL 一律拒写**，避免抹掉已有记录）。"""
        site = (target_site or "").strip().lower()
        u = (url or "").strip()
        if not site or not u:
            return
        self.upsert_cover_cache(
            album_key, site_covers_json=json.dumps({site: u}, ensure_ascii=False)
        )

    def record_pool_event(
        self,
        info_hash: str,
        event: str,
        pool_name: str = "",
        pool_port: int = 0,
        name: str = "",
        size: int = 0,
        save_path: str = "",
        actor: str = "",
        reason: str = "",
    ):
        """写一条入栈/出栈流水（只增不改）

        event 取值约定：
          PAIR      配对注入（3010 的 B 种以 paused 态写入音乐池）
          HANDOFF   交棒完成（池任务转正做种、3010 下架）
          IN        索引同步发现池内新增任务
          OUT       索引同步发现池内任务消失（被删/被移走）
          CONFLICT  同一 info-hash 同时出现在多个池（违反单实例铁律）
        """
        clean_hash = (info_hash or "").strip().lower()
        if not clean_hash or not event:
            return

        with self._lock:
            self._conn.execute("""
            INSERT INTO pool_event_log
                (info_hash, pool_name, pool_port, event, name, size, save_path, actor, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                clean_hash, pool_name or "", int(pool_port or 0), event,
                name or "", int(size or 0), save_path or "", actor or "", reason or "",
            ))
            self._conn.commit()

    # ------------------------------------------------------------------
    # 摄取流水台账（ingest_log）：3016 完成源种 → 机械盘池的落点与做种状态
    # ------------------------------------------------------------------
    INGEST_STATUSES = ("SEEDING", "VERIFYING", "FAILED", "ROLLED_BACK")

    def log_ingest(
        self,
        info_hash: str,
        target_instance: str = "",
        target_port: int = 0,
        source_site: str = "",
        source_tid: str = "",
        source_name: str = "",
        target_save_path: str = "",
        host_path: str = "",
        size_bytes: int = 0,
        file_count: int = 0,
        move_mode: str = "mv",
        status: str = "SEEDING",
        progress: float = 0.0,
        reason: str = "",
    ):
        """写一条摄取流水（只增不改，取最新一行即当前状态）

        status 约定：
          SEEDING       目标池任务已注入并在做种
          VERIFYING     已注入，等待进度回读到 100%
          FAILED        注入/校验失败，文件已回滚或原地保留（需人工介入）
          ROLLED_BACK   失败后已把文件搬回 SSD、恢复 3016 任务，下一轮重试
        """
        clean_hash = (info_hash or "").strip().lower()
        if not clean_hash:
            return
        clean_status = (status or "SEEDING").strip().upper()
        if clean_status not in self.INGEST_STATUSES:
            clean_status = "FAILED"
        with self._lock:
            self._conn.execute("""
            INSERT INTO ingest_log
                (info_hash, source_site, source_tid, source_name,
                 target_instance, target_port, target_save_path, host_path,
                 size_bytes, file_count, move_mode, status, progress, reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                clean_hash, (source_site or "").strip().lower(), str(source_tid or ""),
                source_name or "", target_instance or "", int(target_port or 0),
                target_save_path or "", host_path or "", int(size_bytes or 0),
                int(file_count or 0), move_mode or "mv", clean_status,
                float(progress or 0.0), (reason or "")[:500], time.time(),
            ))
            self._conn.commit()

    def get_latest_ingest(self, info_hash: str) -> Optional[Dict[str, Any]]:
        """取该源种最新一条摄取流水（找不到返回 None）"""
        clean_hash = (info_hash or "").strip().lower()
        if not clean_hash:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ingest_log WHERE info_hash = ? ORDER BY id DESC LIMIT 1",
                (clean_hash,),
            ).fetchone()
            return dict(row) if row else None

    def list_ingest_logs(self, status: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        """按状态倒序列出摄取流水（排查「源种在哪做种」用）"""
        limit = max(1, int(limit or 200))
        sql = "SELECT * FROM ingest_log"
        params: List[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status.strip().upper())
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------
    # 源种任务表（source_tasks）与自有站点矩阵（publish_matrix）
    # ------------------------------------------------------------------
    SOURCE_META_FIELDS = (
        "source_site", "source_tid", "source_name", "artist", "album", "year",
        "format", "medium", "standard", "cover_url", "description", "log_text",
        "tracklist_json", "genre_json", "tags_json", "meta_status", "note", "publish_after",
    )

    def upsert_source_task(
        self,
        info_hash: str,
        source_site: str = "unknown",
        source_tid: str = "",
        source_name: str = "",
        publish_after: Optional[float] = None,
    ):
        """登记/刷新一条源种任务；已存在时不覆盖抓取到的元数据与判定结果"""
        clean_hash = (info_hash or "").strip().lower()
        if not clean_hash:
            return
        with self._lock:
            self._conn.execute("""
            INSERT INTO source_tasks
                (info_hash, source_site, source_tid, source_name, publish_after, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(info_hash) DO UPDATE SET
                source_site = CASE
                    WHEN excluded.source_site NOT IN ('', 'unknown') THEN excluded.source_site
                    ELSE source_tasks.source_site END,
                source_tid = CASE
                    WHEN excluded.source_tid != '' THEN excluded.source_tid
                    ELSE source_tasks.source_tid END,
                source_name = CASE
                    WHEN excluded.source_name != '' THEN excluded.source_name
                    ELSE source_tasks.source_name END,
                updated_at = excluded.updated_at;
            """, (
                clean_hash, (source_site or "unknown").strip().lower(),
                str(source_tid or ""), source_name or "",
                float(publish_after or 0.0), time.time(),
            ))
            self._conn.commit()

    def get_source_task(self, info_hash: str) -> Optional[Dict[str, Any]]:
        """按 infohash 读取源种任务；不存在返回 None（区别于字段为空）"""
        clean_hash = (info_hash or "").strip().lower()
        if not clean_hash:
            return None
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM source_tasks WHERE info_hash = ?", (clean_hash,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_source_meta(self, info_hash: str, **fields):
        """写入抓取结果（只接受白名单列），并刷新 fetched_at/updated_at"""
        clean_hash = (info_hash or "").strip().lower()
        if not clean_hash:
            return
        updates = {k: v for k, v in fields.items() if k in self.SOURCE_META_FIELDS}
        if not updates:
            return
        now = time.time()
        updates.setdefault("meta_status", "OK")
        cols = ", ".join(f"{k} = ?" for k in updates)
        with self._lock:
            self._conn.execute(
                f"UPDATE source_tasks SET {cols}, fetched_at = ?, updated_at = ? "
                "WHERE info_hash = ?",
                (*updates.values(), now, now, clean_hash),
            )
            self._conn.commit()

    def set_transfer_rule(self, info_hash: str, rule: str, note: str = ""):
        """写入转载规则三态：ALLOW / DENY / NEED_CONFIRM"""
        clean_hash = (info_hash or "").strip().lower()
        clean_rule = (rule or "").strip().upper()
        if not clean_hash or clean_rule not in ("ALLOW", "DENY", "NEED_CONFIRM"):
            return
        with self._lock:
            self._conn.execute(
                "UPDATE source_tasks SET transfer_rule = ?, note = ?, updated_at = ? "
                "WHERE info_hash = ?",
                (clean_rule, (note or "")[:300], time.time(), clean_hash),
            )
            self._conn.commit()

    def set_publish_after(self, info_hash: str, publish_after: float):
        """设置等待期到期时间戳（0 表示立即放行）"""
        clean_hash = (info_hash or "").strip().lower()
        if not clean_hash:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE source_tasks SET publish_after = ?, updated_at = ? WHERE info_hash = ?",
                (float(publish_after or 0.0), time.time(), clean_hash),
            )
            self._conn.commit()

    def is_target_allowed(self, source_site: str, target_site: str) -> bool:
        """查询「源站 × 目标站」矩阵；未配置的组合一律放行"""
        src = (source_site or "").strip().lower()
        tgt = (target_site or "").strip().lower()
        if not src or not tgt:
            return True
        with self._lock:
            cursor = self._conn.execute(
                "SELECT allow FROM publish_matrix WHERE source_site = ? AND target_site = ?",
                (src, tgt),
            )
            row = cursor.fetchone()
        return True if row is None else bool(int(row[0]))

    def set_matrix_rule(self, source_site: str, target_site: str, allow: bool, note: str = ""):
        """写入一条站点矩阵规则（allow=False 即禁止该源站发往该目标站）"""
        src = (source_site or "").strip().lower()
        tgt = (target_site or "").strip().lower()
        if not src or not tgt:
            return
        with self._lock:
            self._conn.execute("""
            INSERT INTO publish_matrix (source_site, target_site, allow, note, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_site, target_site) DO UPDATE SET
                allow = excluded.allow,
                note = excluded.note,
                updated_at = excluded.updated_at;
            """, (src, tgt, 1 if allow else 0, (note or "")[:300], time.time()))
            self._conn.commit()

    def list_source_tasks(self, meta_status: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        """按元数据状态批量取任务（补抓线程与巡检用），按更新时间升序"""
        sql = "SELECT * FROM source_tasks"
        params: List[Any] = []
        if meta_status:
            sql += " WHERE meta_status = ?"
            params.append(meta_status)
        sql += " ORDER BY COALESCE(fetched_at, 0) ASC LIMIT ?"
        params.append(int(limit or 200))
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return [dict(r) for r in cursor]

    def find_lineage(
        self,
        target_tid: str = "",
        target_site: str = "",
        b_hash: str = "",
        source_hash: str = "",
        album: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """按目标站 tid / B 种 hash / 源种 hash / 专辑名查来龙去脉（补种定位入口）"""
        where: List[str] = []
        params: List[Any] = []
        if target_tid:
            where.append("target_tid = ?")
            params.append(str(target_tid).strip())
        if target_site:
            where.append("LOWER(target_site) = ?")
            params.append(target_site.strip().lower())
        if b_hash:
            where.append("LOWER(b_hash) = ?")
            params.append(b_hash.strip().lower())
        if source_hash:
            where.append("LOWER(source_hash) = ?")
            params.append(source_hash.strip().lower())
        if album:
            where.append("(source_name LIKE ? OR local_path LIKE ?)")
            params.extend([f"%{album}%", f"%{album}%"])
        sql = "SELECT * FROM publish_lineage"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit or 50))
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return [dict(r) for r in cursor]

    def record_seed_recovery(
        self,
        target_site: str,
        target_tid: str,
        b_hash: str,
        qb_instance: str,
        qb_port: int,
        save_path: str = "/downloads",
        source_site: str = "",
        source_tid: str = "",
        source_hash: str = "",
        source_name: str = "",
        local_path: str = "",
        target_url: str = "",
        note: str = "",
    ) -> None:
        """补写 B 种恢复血缘；不改发布图、不触发 Vertex 账本导出。"""
        with self._lock:
            self._conn.execute("""
            INSERT INTO publish_lineage
            (source_hash, source_site, source_tid, source_name, local_path,
             target_site, target_tid, target_url, b_hash, qb_instance, qb_port,
             save_path, status, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PUBLISHED', ?);
            """, (
                (source_hash or "").strip().lower(), normalize_site_key(source_site),
                str(source_tid or ""), source_name or "", local_path or "",
                normalize_site_key(target_site), str(target_tid or ""), target_url or "",
                (b_hash or "").strip().lower(), qb_instance or "", int(qb_port or 0),
                save_path or "", ("recovery: " + (note or ""))[:500],
            ))
            self._conn.commit()

    def record_publish(
        self,
        item: CanonicalMusicItem,
        target_site: str,
        target_torrent_id: str = "",
        b_hash: str = "",
        target_url: str = "",
        qb_instance: str = "",
        qb_port: int = 0,
        save_path: str = "",
        status: str = "PUBLISHED",
        publish_status: str = "PUBLISHED",
        note: str = "",
    ):
        """记录一条发布，写入溯源图 + 来龙去脉台账

        `b_hash` 是上传到目标站后从站点拉回的 B 种 infohash（与 3016 源种 A 种不同），
        补种时必须靠它 + `qb_instance` 才能定位「本地哪个任务该在哪个池做种」。

        状态与 TID 双轨解耦保护铁律：
        1. target_torrent_id 严格收敛为纯数字 TID，杜绝写入 'EXISTING' 文本；
        2. publish_status 区分 'PUBLISHED'（真发）与 'EXISTING'（在站存量/辅种）；
        3. 单向不可逆跃迁规则：已为 PUBLISHED 的行严禁被任何写入降级为 EXISTING。
        """
        content_fp = item.calculate_content_fingerprint()
        stream_fp = item.calculate_stream_fingerprint()
        tgt = normalize_site_key(target_site)
        origin_key = normalize_site_key(getattr(item, "origin_site", "") or "")
        clean_tid = digit_tid(target_torrent_id)

        with self._lock:
            # 站点 tid 列：先继承本行已有值（EXISTING/重发不得把已记的 tid 冲成 NULL），
            # 再写入本次可确定的两处——源站 tid 与目标站 tid（都只认纯数字）。
            site_tids: Dict[str, str] = {}
            existing = self._conn.execute(
                "SELECT * FROM site_publish_graph WHERE content_fingerprint = ? AND target_site = ?",
                (content_fp, tgt),
            ).fetchone()

            final_status = publish_status
            final_tid = clean_tid
            if existing is not None:
                for site, col in SITE_TID_COLUMNS.items():
                    val = existing[col]
                    if val not in (None, ""):
                        site_tids[site] = str(val)

                # 单向跃迁保护：PUBLISHED (权重2) > EXISTING (权重1)
                try:
                    old_status = existing["publish_status"] or "EXISTING"
                except Exception:
                    old_status = "EXISTING"
                if old_status == "PUBLISHED" and publish_status == "EXISTING":
                    final_status = "PUBLISHED"
                
                old_tid = digit_tid(str(existing["target_torrent_id"] or ""))
                if not final_tid and old_tid:
                    final_tid = old_tid

            src_tid = digit_tid(getattr(item, "origin_torrent_id", "") or "")
            if origin_key in SITE_TID_COLUMNS and src_tid:
                site_tids[origin_key] = src_tid
            if tgt in SITE_TID_COLUMNS and final_tid:
                site_tids[tgt] = final_tid

            cols = [
                "content_fingerprint", "stream_fingerprint", "origin_site",
                "origin_torrent_id", "target_site", "target_torrent_id", "publish_status",
            ]
            vals: List[Any] = [
                content_fp,
                stream_fp,
                item.origin_site,
                item.origin_torrent_id,
                tgt,
                final_tid,
                final_status,
            ]
            for site, val in site_tids.items():
                cols.append(SITE_TID_COLUMNS[site])
                vals.append(val)

            self._conn.execute(
                f"INSERT OR REPLACE INTO site_publish_graph ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)});",
                vals,
            )
            # 同一内容的其他目标站行同步补齐，保证任一行都能回答「各站 tid」
            for site, val in site_tids.items():
                self._fill_site_tid_locked(content_fp, SITE_TID_COLUMNS[site], val)

            self._conn.execute("""
            INSERT INTO publish_lineage
            (source_hash, source_site, source_tid, source_url, source_name, local_path,
             target_site, target_tid, target_url, b_hash, qb_instance, qb_port, save_path, status, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                (getattr(item, "origin_info_hash", "") or "").strip().lower(),
                origin_key,
                src_tid or str(getattr(item, "origin_torrent_id", "") or ""),
                (getattr(item, "source_url", "") or ""),
                (getattr(item, "title", "") or ""),
                (getattr(item, "local_path", "") or ""),
                tgt,
                str(final_tid or ""),
                target_url or "",
                (b_hash or "").strip().lower(),
                qb_instance or "",
                int(qb_port or 0),
                save_path or "",
                status or final_status,
                (note or "")[:500],
            ))
            self._conn.commit()

        # 触发导出已发布 ID 账本到 Vertex 共享目录，供 Vertex 3016 RSS 规则防环
        try:
            self.export_vertex_blocklist()
        except Exception as e:
            logger.warning(f"[RoutingGraph] 自动同步 Vertex 账本失败: {e}")

    def export_vertex_blocklist(self, vertex_data_dir: Optional[str] = None):
        """将 site_publish_graph 中的 target_torrent_id 与各站专有列按站点汇聚导出为 JSON 账本供 Vertex 规则防环

        安全护栏（2026-11-21 事故后加）：
        - 只写显式传入的目录，或容器内写死的 NAS 生产路径；
        - **移除 UNC 兜底**：本机（Windows）跑单测时 UNC 共享可达，会把 :memory: 里的
          测试数据覆盖到生产账本，实测曾导致 1834 个已发 ID 全部丢失；
        - 内存库（":memory:"）一律不导出。
        - 兼顾直接目标站 target_torrent_id 与跨站传播的 SITE_TID_COLUMNS，实现 100% 免疫回环。
        """
        db_path = str(getattr(self, "db_path", "") or "")
        if db_path in (":memory:", "", "file::memory:"):
            return

        target_dirs = []
        if vertex_data_dir:
            target_dirs.append(vertex_data_dir)
        else:
            # 仅容器内生产路径；本机/开发环境必须显式传参
            prod_dir = "/volume2/docker_ssd/vertex2/data/vertex/data"
            if os.path.exists(prod_dir):
                target_dirs.append(prod_dir)

        if not target_dirs:
            return

        with self._lock:
            # 1. 抓取直接目标站 target_torrent_id
            cursor = self._conn.execute("""
                SELECT target_site, target_torrent_id
                FROM site_publish_graph
                WHERE target_torrent_id IS NOT NULL
                  AND target_torrent_id != ''
                  AND target_torrent_id != 'EXISTING';
            """)
            rows = list(cursor.fetchall())

            # 2. 抓取各站点专有列（跨站矩阵传播学习到的 TID）
            for site, col in SITE_TID_COLUMNS.items():
                try:
                    c_rows = self._conn.execute(f"""
                        SELECT '{site}', {col}
                        FROM site_publish_graph
                        WHERE {col} IS NOT NULL
                          AND {col} != ''
                          AND {col} != 'EXISTING';
                    """).fetchall()
                    rows.extend(c_rows)
                except Exception:
                    pass

        published_map: Dict[str, List[int]] = {}
        for site, tid in rows:
            site_clean = str(site).strip().lower()
            tid_clean = str(tid).strip()
            if tid_clean.isdigit():
                published_map.setdefault(site_clean, []).append(int(tid_clean))

        for s in published_map:
            published_map[s] = sorted(list(set(published_map[s])))

        payload = {
            "updated_at": int(time.time()),
            "published": published_map
        }

        for out_dir in target_dirs:
            try:
                out_path = os.path.join(out_dir, "published_music_ids.json")
                tmp_path = out_path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                if os.path.exists(out_path):
                    os.remove(out_path)
                os.rename(tmp_path, out_path)
            except Exception as e:
                logger.warning(f"[RoutingGraph] 写入 Vertex 账本到 {out_dir} 异常: {e}")

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
