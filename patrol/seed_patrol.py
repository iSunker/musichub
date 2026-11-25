# -*- coding: utf-8 -*-
"""MusicHub 3.0 全生命周期常态化断种巡检器 (SeedPatrolManager)

核心定位：
1. 【只读事前巡检】：全面对账 site_publish_graph 与做种端 qB (默认 publish 3010 / 纳管主库)；
2. 【严格身份判据】：B != A。判断在做种的唯一依据是 info_hash(B) 精确相等；
3. 【细粒度健康态分类】：
   - SEEDING: progress >= 1.0 且 state in 做种族 (stalledUP, uploading, forcedUP, queuedUP, checkingUP)
   - PAUSED: B 种仅存在于 stopped/paused 状态，未向 tracker 公告
   - PRESENT: 存在但 progress < 1.0 (正在校验或处于异常状态)
   - MISSING: 数据库声称已发布，但已查询的 qB 实例中完全缺失该 B 种
   - AMBIGUOUS: 命中多条重复记录或跨实例冲突
   - NO_SITE_TORRENT: 缺 tid / 种子未归档 / 无可用站点取种途径
   - FETCH_ERROR: 站点取种请求失败，或返回登录页、错误页等非种子内容
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
PAUSED = "PAUSED"            # 异常：唯一副本处于 stopped/paused，未公告
PRESENT = "PRESENT"          # 异常：在客户端但未完成校验 (progress < 1.0)
MISSING = "MISSING"          # 丢失：已查询实例中不存在该站点种 B
AMBIGUOUS = "AMBIGUOUS"      # 冲突：多条相同 B 种并存
NO_SITE_TORRENT = "NO_SITE_TORRENT"  # 本地无归档且没有站点取种途径
FETCH_ERROR = "FETCH_ERROR"  # 站点请求失败或返回非 torrent 内容

DEFECTIVE = (MISSING, AMBIGUOUS, PRESENT, PAUSED, FETCH_ERROR)
SEEDING_STATES = {"stalledup", "uploading", "forcedup", "queuedup", "checkingup"}
PAUSED_STATES = {"stoppedup", "pausedup", "stoppeddl", "pauseddl"}


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
    qb_instance: str = ""
    qb_instances: List[str] = field(default_factory=list)
    progress: float = 0.0
    seeding_time: int = 0
    uploaded: int = 0
    note: str = ""


class SeedPatrolManager:
    def __init__(
        self,
        db_path: str,
        publish_qb_client: Any,
        site_torrents_dir: Optional[str] = None,
        site_adapters: Optional[Dict[str, Any]] = None,
        pool_qb_clients: Optional[Dict[str, Any]] = None,
    ):
        self.db_path = db_path
        self.publish_qb = publish_qb_client
        self.pool_qbs = pool_qb_clients or {}
        self.site_adapters = site_adapters or {}
        self.client_errors: Dict[str, str] = {}
        self._last_torrent_fetch_error: Optional[str] = None
        self._last_torrent_fetch_remote = False

        # 默认站点种子缓存目录
        if site_torrents_dir:
            self.site_torrents_dir = site_torrents_dir
        else:
            self.site_torrents_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "torrents")
        os.makedirs(self.site_torrents_dir, exist_ok=True)

    @staticmethod
    def _looks_like_html(payload: bytes) -> bool:
        head = payload[:4096].lstrip().lower()
        return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body")) or b"<title>login" in head

    @staticmethod
    def _get_torrents_strict(client: Any) -> List[Dict[str, Any]]:
        """真实客户端严格取数；兼容旧测试替身的无 strict 参数签名。"""
        try:
            return client.get_torrents(strict=True) or []
        except TypeError:
            return client.get_torrents() or []

    @staticmethod
    def _instance_label(name: str, client: Any) -> str:
        port = getattr(client, "port", None)
        return f"{name}:{port}" if port else name

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
        """按策略获取站点种 B；在线错误页不写入 torrent 缓存。"""
        self._last_torrent_fetch_error = None
        self._last_torrent_fetch_remote = False
        if not target_torrent_id or target_torrent_id == "EXISTING":
            return None

        cache_filename = f"{target_site.lower()}_{target_torrent_id}.torrent"
        cache_path = os.path.join(self.site_torrents_dir, cache_filename)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "rb") as f:
                    return f.read()
            except Exception as e:
                self._last_torrent_fetch_error = f"读取本地种子归档失败: {e}"
                logger.warning(f"Error reading cached torrent {cache_path}: {e}")

        adapter = self.site_adapters.get(target_site.lower())
        if adapter and hasattr(adapter, "download_torrent"):
            self._last_torrent_fetch_remote = True
            try:
                logger.info(f"Downloading site torrent B online for {target_site} tid={target_torrent_id}...")
                data = adapter.download_torrent(target_torrent_id)
                if not data or len(data) <= 100 or self._looks_like_html(data):
                    self._last_torrent_fetch_error = "站点返回空响应、登录页或 HTML 错误页"
                    return None
                if not self.extract_info_hash_from_torrent(data):
                    self._last_torrent_fetch_error = "站点响应无法解析为 .torrent(B)"
                    return None
                self._last_torrent_fetch_error = None
                try:
                    with open(cache_path, "wb") as f:
                        f.write(data)
                except Exception as e:
                    logger.warning(f"Failed to cache downloaded torrent {cache_path}: {e}")
                return data
            except Exception as e:
                self._last_torrent_fetch_error = str(e)
                logger.warning(f"Failed to download torrent from {target_site} tid={target_torrent_id}: {e}")

        return None

    def audit_single_record(
        self,
        record_dict: Dict[str, Any],
        qb_torrents_map: Dict[str, Any],
    ) -> PatrolRecord:
        """按 B info-hash 巡检单个发布条目，保留所有命中实例证据。"""
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

        torrent_bytes = self.get_site_torrent_bytes(target_site, target_tid)
        if not torrent_bytes:
            if self._last_torrent_fetch_error:
                record.status = FETCH_ERROR
                record.note = self._last_torrent_fetch_error
            else:
                record.status = NO_SITE_TORRENT
                record.note = "本地无种子归档，且没有可用站点取种途径"
            return record

        b_ih = self.extract_info_hash_from_torrent(torrent_bytes)
        if not b_ih:
            if self._last_torrent_fetch_remote or self._looks_like_html(torrent_bytes):
                record.status = FETCH_ERROR
                record.note = "站点响应不是可解析的 .torrent(B)"
            else:
                record.status = NO_SITE_TORRENT
                record.note = "本地归档解析 .torrent(B) 提取 infohash 失败"
            return record

        record.b_info_hash = b_ih
        raw_hits = qb_torrents_map.get(b_ih)
        if not raw_hits:
            hits: List[Dict[str, Any]] = []
        elif isinstance(raw_hits, dict):
            hits = [raw_hits]
        else:
            hits = list(raw_hits)

        if not hits:
            record.status = MISSING
            record.note = f"在已查询做种实例中未检索到站点种 B (ih={b_ih})"
            if self.client_errors:
                record.note += f"；未查询实例: {','.join(sorted(self.client_errors))}"
            return record

        record.qb_instances = sorted({
            str(t.get("_patrol_instance") or "unknown") for t in hits
        })
        record.qb_instance = ",".join(record.qb_instances)
        if len(hits) > 1:
            record.status = AMBIGUOUS
            record.qb_state = ",".join(sorted({str(t.get("state", "")).lower() for t in hits}))
            record.progress = max(float(t.get("progress", 0.0)) for t in hits)
            record.album_name = hits[0].get("name")
            record.note = f"同一 B hash 命中 {len(hits)} 份任务: {record.qb_instance}"
            return record

        t_info = hits[0]
        state = str(t_info.get("state", "")).lower()
        progress = float(t_info.get("progress", 0.0))
        record.album_name = t_info.get("name")
        record.qb_state = state
        record.progress = progress
        record.seeding_time = int(t_info.get("seeding_time") or 0)
        record.uploaded = int(t_info.get("uploaded") or 0)

        if state in PAUSED_STATES:
            record.status = PAUSED
            record.note = f"唯一副本处于暂停态 (progress={progress:.1%}, state={state}, instance={record.qb_instance})"
        elif progress >= 1.0 and state in SEEDING_STATES:
            record.status = SEEDING
            record.note = f"正常做种中 (state={state}, instance={record.qb_instance})"
        else:
            record.status = PRESENT
            record.note = f"存在于客户端但未正常做种 (progress={progress:.1%}, state={state}, instance={record.qb_instance})"

        return record

    def run_patrol(
        self,
        site_filter: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[List[PatrolRecord], Dict[str, int]]:
        """执行全量/分站只读断种巡检"""
        stats = {
            SEEDING: 0,
            PAUSED: 0,
            PRESENT: 0,
            MISSING: 0,
            AMBIGUOUS: 0,
            NO_SITE_TORRENT: 0,
            FETCH_ERROR: 0,
            "QB_CLIENT_ERROR": 0,
            "TOTAL": 0,
        }

        # 1. 汇总 3010 + 8 个音乐池；保留同 hash 的全部实例，单池失败不拖垮整轮。
        clients = {"publish": self.publish_qb, **self.pool_qbs}
        qb_torrents_map: Dict[str, List[Dict[str, Any]]] = {}
        self.client_errors = {}
        queried_clients = 0
        for name, client in clients.items():
            label = self._instance_label(name, client)
            try:
                torrents = self._get_torrents_strict(client)
                queried_clients += 1
            except Exception as e:
                self.client_errors[label] = str(e)
                logger.error(f"Failed to query qB client {label}: {e}")
                continue
            for torrent in torrents:
                ih = (torrent.get("hash") or torrent.get("infohash_v1") or "").lower()
                if not ih:
                    continue
                item = dict(torrent)
                item["_patrol_instance"] = label
                qb_torrents_map.setdefault(ih, []).append(item)

        stats["QB_CLIENT_ERROR"] = len(self.client_errors)
        if not queried_clients:
            raise RuntimeError(f"全部 qB 实例查询失败: {self.client_errors}")

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
