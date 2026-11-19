# -*- coding: utf-8 -*-
"""MusicHub 3.0 全生命周期常态化断种巡检器 (SeedPatrolManager)

核心定位：
1. 【只读事前巡检】：全面对账 site_publish_graph 与做种端 qB (默认 publish 3010 / 纳管主库)；
2. 【严格身份判据】：B != A。判断在做种的唯一依据是 info_hash(B) 精确相等；
3. 【细粒度健康态分类】：
   - SEEDING: progress >= 1.0 且 state in 做种族 (stalledUP, uploading, forcedUP, queuedUP, checkingUP)
   - PRESENT: 存在但 progress < 1.0 (正在校验或处于异常状态)
   - MISSING: 数据库声称已发布，但 qB 中完全缺失该 B 种 -> 需自愈加回
   - AMBIGUOUS: 命中多条重复记录或跨实例冲突
   - NO_SITE_TORRENT: 缺 tid / 种子未归档 / 无法获取 B 种元数据
4. 【安全模式】：只读不写，生成结构化巡检审计清单。
"""

import os
import io
import time
import logging
import sqlite3
from typing import Dict, List, Optional, Tuple, Any, Callable
from dataclasses import dataclass, field

logger = logging.getLogger("SeedPatrolManager")

# 巡检状态常量定义
SEEDING = "SEEDING"          # 健康：在做种且进度 100%
PRESENT = "PRESENT"          # 异常：在客户端但未完成校验 (progress < 1.0)
MISSING = "MISSING"          # 丢失：qB 中完全不存在该站点种 B
AMBIGUOUS = "AMBIGUOUS"      # 冲突：多条相同 B 种并存
NO_SITE_TORRENT = "NO_SITE_TORRENT"  # 无法获取站点种 B

DEFECTIVE = (MISSING, AMBIGUOUS, PRESENT)
SEEDING_STATES = {"stalledup", "uploading", "forcedup", "queuedup", "checkingup"}


@dataclass
class PatrolRecord:
    graph_id: int
    target_site: str
    target_torrent_id: str
    origin_site: str
    b_info_hash: Optional[str] = None
    album_name: Optional[str] = None
    status: str = NO_SITE_TORRENT
    qb_state: str = ""
    progress: float = 0.0
    note: str = ""


class SeedPatrolManager:
    def __init__(
        self,
        db_path: str,
        publish_qb_client: Any,
        site_torrents_dir: Optional[str] = None,
        site_adapters: Optional[Dict[str, Any]] = None,
    ):
        self.db_path = db_path
        self.publish_qb = publish_qb_client
        self.site_adapters = site_adapters or {}
        
        # 默认站点种子缓存目录
        if site_torrents_dir:
            self.site_torrents_dir = site_torrents_dir
        else:
            self.site_torrents_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "torrents")
        os.makedirs(self.site_torrents_dir, exist_ok=True)

    def extract_info_hash_from_torrent(self, torrent_bytes: bytes) -> Optional[str]:
        """从种子二进制流解析真实的 info_hash (v1)"""
        try:
            import torf
            t = torf.Torrent.read_stream(io.BytesIO(torrent_bytes))
            ih = getattr(t, "infohash", None) or getattr(t, "infohash_v1", None)
            if ih:
                return str(ih).lower()
        except Exception:
            pass

        # 降级纯轻量 bencode 解析
        try:
            import hashlib
            import bencodepy
            decoded = bencodepy.decode(torrent_bytes)
            if b"info" in decoded:
                raw_info = bencodepy.encode(decoded[b"info"])
                return hashlib.sha1(raw_info).hexdigest().lower()
        except Exception as e:
            logger.debug(f"Failed to extract infohash from bytes: {e}")
        return None

    def get_site_torrent_bytes(self, target_site: str, target_torrent_id: str) -> Optional[bytes]:
        """按策略获取站点种 B 的二进制流：
        优先从本地缓存提取；本地缺失时若有 adapter 且具备 download_torrent 能力则在线拉取并缓存。
        """
        if not target_torrent_id or target_torrent_id == "EXISTING":
            return None

        # 1. 优先查本地缓存文件
        cache_filename = f"{target_site.lower()}_{target_torrent_id}.torrent"
        cache_path = os.path.join(self.site_torrents_dir, cache_filename)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "rb") as f:
                    return f.read()
            except Exception as e:
                logger.warning(f"Error reading cached torrent {cache_path}: {e}")

        # 2. 降级：通过站点适配器联网从 download.php 获取
        adapter = self.site_adapters.get(target_site.lower())
        if adapter and hasattr(adapter, "download_torrent"):
            try:
                logger.info(f"Downloading site torrent B online for {target_site} tid={target_torrent_id}...")
                data = adapter.download_torrent(target_torrent_id)
                if data and len(data) > 100:
                    try:
                        with open(cache_path, "wb") as f:
                            f.write(data)
                    except Exception as e:
                        logger.warning(f"Failed to cache downloaded torrent {cache_path}: {e}")
                    return data
            except Exception as e:
                logger.warning(f"Failed to download torrent from {target_site} tid={target_torrent_id}: {e}")

        return None

    def audit_single_record(
        self,
        record_dict: Dict[str, Any],
        qb_torrents_map: Dict[str, Dict[str, Any]],
    ) -> PatrolRecord:
        """巡检单个发布条目"""
        graph_id = record_dict.get("id", 0)
        target_site = (record_dict.get("target_site") or "").lower()
        target_tid = str(record_dict.get("target_torrent_id") or "")
        origin_site = record_dict.get("origin_site") or ""

        record = PatrolRecord(
            graph_id=graph_id,
            target_site=target_site,
            target_torrent_id=target_tid,
            origin_site=origin_site,
        )

        if not target_tid or target_tid == "EXISTING":
            record.status = NO_SITE_TORRENT
            record.note = "未记录站点具体 torrent_id"
            return record

        # 解析获取 B 种的 info_hash
        torrent_bytes = self.get_site_torrent_bytes(target_site, target_tid)
        if not torrent_bytes:
            record.status = NO_SITE_TORRENT
            record.note = "本地缓存与站点下载均无法获取 .torrent(B)"
            return record

        b_ih = self.extract_info_hash_from_torrent(torrent_bytes)
        if not b_ih:
            record.status = NO_SITE_TORRENT
            record.note = "解析 .torrent(B) 提取 infohash 失败"
            return record

        record.b_info_hash = b_ih

        # 铁律：唯一判据是 qB.hash == info_hash(B)
        t_info = qb_torrents_map.get(b_ih)
        if not t_info:
            record.status = MISSING
            record.note = f"在做种客户端中未检索到站点种 B (ih={b_ih})"
            return record

        # 提取做种状态
        state = str(t_info.get("state", "")).lower()
        progress = float(t_info.get("progress", 0.0))
        record.album_name = t_info.get("name")
        record.qb_state = state
        record.progress = progress

        if progress >= 1.0 and state in SEEDING_STATES:
            record.status = SEEDING
            record.note = f"正常做种中 (state={state})"
        else:
            record.status = PRESENT
            record.note = f"存在于客户端但未正常做种 (progress={progress:.1%}, state={state})"

        return record

    def run_patrol(
        self,
        site_filter: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[List[PatrolRecord], Dict[str, int]]:
        """执行全量/分站只读断种巡检"""
        stats = {
            SEEDING: 0,
            PRESENT: 0,
            MISSING: 0,
            AMBIGUOUS: 0,
            NO_SITE_TORRENT: 0,
            "TOTAL": 0,
        }

        # 1. 获取客户端种子全量映射 (info_hash -> torrent_dict)
        qb_torrents_map: Dict[str, Dict[str, Any]] = {}
        try:
            torrents = self.publish_qb.get_torrents() or []
            for t in torrents:
                ih = (t.get("hash") or t.get("infohash_v1") or "").lower()
                if ih:
                    qb_torrents_map[ih] = t
        except Exception as e:
            logger.error(f"Failed to query qB client torrents: {e}")
            raise RuntimeError(f"qB client query error: {e}")

        # 2. 从 site_publish_graph 查出需要巡检的任务
        records: List[PatrolRecord] = []
        if not os.path.exists(self.db_path):
            logger.warning(f"Database not found at {self.db_path}")
            return records, stats

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            if site_filter:
                cur.execute(
                    "SELECT id, origin_site, target_site, target_torrent_id "
                    "FROM site_publish_graph WHERE target_site = ? ORDER BY id DESC LIMIT ?",
                    (site_filter.lower(), limit),
                )
            else:
                cur.execute(
                    "SELECT id, origin_site, target_site, target_torrent_id "
                    "FROM site_publish_graph ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

        for r in rows:
            patrol_res = self.audit_single_record(r, qb_torrents_map)
            records.append(patrol_res)
            stats[patrol_res.status] = stats.get(patrol_res.status, 0) + 1
            stats["TOTAL"] += 1

        return records, stats
