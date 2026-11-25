# -*- coding: utf-8 -*-
"""MusicHub 3.0 存量矩阵补种调度器 (StockMatrixPump)

架构与调度职责：
1. 【存量矩阵补齐】：
   - 扫描本地存量物理做种目录（music3 / music1 / music2 / music_pool_1）
   - 比对 site_publish_graph 既有记录
   - 识别出“未发满 7 站”的优质存量专辑
2. 【平铺缺额优先策略】：
   - 按照当前各站点收录最少的优先级排序：
     MuXueGe -> TangPT -> PTerClub -> HDKylin -> Xingwan -> Musopia -> CrabPT
3. 【频控与发种窗口约束】：
   - 严格遵守 00:00 - 10:00 黄金发种期
   - 单轮批次：每轮最多挑选 3 个符合条件的任务
   - 两次发种之间保持 60 秒平滑间隔，保护防爬与站点 IOPS
4. 【发种闭环】：
   - 物理完整性自检 (verify_physical_data_readiness)
   - 站点专属护栏校验 (pre_publish_check)
   - torf 安全制种与分块反验 (create_torrent_safe)
   - 站点发布 (publish) 并写入 site_publish_graph
   - 450s 延迟入库队列 (DelayedReaddQueue) 注入 3010
"""

import os
import sys
import time
import logging
import sqlite3
import re
from typing import List, Dict, Tuple, Optional, Any

from musichub.core.models import CanonicalMusicItem
from musichub.metadata.audio_inspector import AudioInspector
from musichub.routing.graph import RoutingGraphManager, torrent_infohash, digit_tid
from musichub.publisher.gateway import DeduplicationGateway
from musichub.publisher.engine import PublisherEngine
from musichub.publisher.delayed_queue import DelayedReaddQueue
from musichub.core.qb_client import QBClient
from musichub.adapters.sites import (
    MusopiaAdapter, CrabPTAdapter, PTerClubAdapter,
    XingwanAdapter, HDKylinAdapter, TangPTAdapter, MuXueGeAdapter,
    LemonHDAdapter, UHDDolbyAdapter, HDTimeAdapter, UBitsAdapter,
    SoulVoiceAdapter, NanyangPTAdapter
)
from musichub.adapters.nexusphp_base import is_duplicate_name_result, extract_existing_tid

logger = logging.getLogger("StockMatrixPump")


class StockMatrixPump:
    TARGET_SITES_PRIORITY = [
        "nanyangpt",
        "soulvoice",
        "ubits",
        "lemonhd",
        "hdtime",
        "uhddolby",
        "muxuege",
        "tangpt",
        "pterclub",
        "hdkylin",
        "xingwan",
        "musopia",
        "crabpt",
    ]

    def __init__(
        self,
        db_path: str = "/app/musichub/data/musichub.db",
        pools: Optional[List[str]] = None,
        publish_qb: Optional[QBClient] = None,
        engine: Optional[PublisherEngine] = None,
        delayed_queue: Optional[DelayedReaddQueue] = None,
    ):
        self.db_path = db_path
        self.pools = pools or [
            f"/volume1/video/download/music/music_pool_{i}" for i in range(1, 9)
        ] + [
            "/volume1/video/download/music/music1",
            "/volume1/video/download/music/music2",
            "/volume1/video/download/music/music3",
        ]
        self.graph_mgr = RoutingGraphManager(db_path=self.db_path)
        self.publish_qb = publish_qb or QBClient("192.168.0.7", 3010)
        self.engine = engine or PublisherEngine(self.publish_qb, self.graph_mgr)
        self.delayed_queue = delayed_queue or DelayedReaddQueue(self.db_path, self.engine)

        self.adapters_map = {
            "nanyangpt": NanyangPTAdapter(),
            "soulvoice": SoulVoiceAdapter(),
            "ubits": UBitsAdapter(),
            "lemonhd": LemonHDAdapter(),
            "uhddolby": UHDDolbyAdapter(),
            "hdtime": HDTimeAdapter(),
            "musopia": MusopiaAdapter(),
            "crabpt": CrabPTAdapter(),
            "pterclub": PTerClubAdapter(),
            "xingwan": XingwanAdapter(),
            "hdkylin": HDKylinAdapter(),
            "tangpt": TangPTAdapter(),
            "muxuege": MuXueGeAdapter(),
        }
        self.gateway = DeduplicationGateway(adapters=list(self.adapters_map.values()), graph_mgr=self.graph_mgr)
        self.inspector = AudioInspector()

    def _resolve_stock_origin(self, folder_name: str, album: str, artist: str, full_path: str) -> Dict[str, Any]:
        """为音乐池本地目录反查真实源站、源 TID、源 Hash 与元数据（含标签与流派）。"""
        from musichub.publisher.origin_resolver import resolve_stock_origin_detail
        return resolve_stock_origin_detail(
            folder_name=folder_name,
            album=album,
            artist=artist,
            full_path=full_path,
            conn=getattr(self.graph_mgr, "_conn", None),
            db_path=self.db_path,
        )

    def find_stock_candidates_for_site(self, target_site: str, limit: int = 3) -> List[Dict[str, Any]]:
        """为特定目标站寻找最适合补齐的本地存量专辑"""
        target_site = target_site.lower()
        adapter = self.adapters_map.get(target_site)
        if not adapter:
            return []

        candidates = []
        for pool in self.pools:
            if not os.path.exists(pool):
                continue
            try:
                entries = sorted(os.listdir(pool))
            except Exception as e:
                logger.warning(f"Failed to list pool {pool}: {e}")
                continue

            for e in entries:
                if len(candidates) >= limit:
                    break
                if e.startswith(".") or e.startswith("@") or "incomplete" in e or "music5" in e or "18禁" in e:
                    continue
                full_p = os.path.join(pool, e)
                if not os.path.isdir(full_p):
                    continue

                # 快速音轨校验
                tracks = self.inspector.scan_album_directory(full_p)
                if not tracks or len(tracks) < 1:
                    continue

                meta = AudioInspector.parse_name_metadata(e)
                artist = meta.get("artist") or (tracks[0].artist if tracks else "Various Artists")
                album = meta.get("album") or e
                year = meta.get("year") or "2020"
                fmt = meta.get("format") or "FLAC"
                medium = meta.get("medium") or "CD"
                standard = meta.get("standard") or "Lossless"

                o_meta = self._resolve_stock_origin(e, album, artist, full_p)

                item = CanonicalMusicItem(
                    title=e,
                    origin_title=o_meta["origin_name"],
                    album=album,
                    artist=artist,
                    year=year,
                    format=fmt,
                    medium=medium,
                    standard=standard,
                    origin_site=o_meta["origin_site"],
                    origin_torrent_id=o_meta["origin_tid"],
                    origin_info_hash=o_meta["origin_hash"],
                    cover_url=o_meta["cover_url"],
                    description=o_meta["descr"],
                    tags=o_meta.get("tags") or [],
                    genre=o_meta.get("genre") or [],
                    local_path=full_p,
                    tracks=tracks,
                )

                # 0. 源种准入（本地存量按 local 源放行，但受站点矩阵约束）
                from musichub.publisher.gateway import evaluate_source_policy
                policy_ok, policy_reason = evaluate_source_policy(self.graph_mgr, item, target_site, adapter)
                if not policy_ok:
                    continue

                # 1. 第一层：防环与 site_publish_graph 查重
                if self.graph_mgr.is_loop_or_already_published(item, target_site):
                    continue

                # 2. 第二层：前置护栏校验（如麒麟纯拉丁标题）
                chk_ok, chk_msg = adapter.pre_publish_check(item)
                if not chk_ok:
                    continue

                # 3. 第三层：物理就绪反验
                ready, reason = self.engine.verify_physical_data_readiness(item)
                if not ready:
                    continue

                # 4. 第四层：站点在线搜索查重
                exists, exists_why, matched_tid = adapter.check_exists_detail(item)
                if exists:
                    clean_m_tid = digit_tid(matched_tid or "")
                    self.graph_mgr.record_publish(
                        item, target_site,
                        target_torrent_id=clean_m_tid,
                        publish_status="EXISTING",
                        status="EXISTING",
                        note=exists_why
                    )
                    self.graph_mgr.record_publish_attempt(
                        album_name=album,
                        target_site=target_site,
                        result="SKIPPED_EXISTING",
                        reason=exists_why,
                        target_torrent_id=clean_m_tid,
                        origin_path=full_p,
                    )
                    continue

                candidates.append({
                    "item": item,
                    "adapter": adapter,
                    "target_site": target_site,
                    "album_name": album,
                    "local_path": full_p,
                })

            if len(candidates) >= limit:
                break

        return candidates

    def run_stock_round(self, max_per_round: Optional[int] = None, dry_run: bool = False) -> int:
        """执行单轮补种：采用矩阵轮转平铺机制 (Round-Robin Matrix Interleaving)
        
        ★ 核心调度：
        每轮依次轮询 11 个目标站点，每个站点单轮最多发布 1 个（每轮 11 站各发 1 个），
        确保各站点交替平铺、雨露均沾。
        """
        published_count = 0
        limit_total = max_per_round if max_per_round is not None else len(self.TARGET_SITES_PRIORITY)
        logger.info(f"=== Starting Stock Matrix Pump Round (Target Sites: {len(self.TARGET_SITES_PRIORITY)}, Max Total: {limit_total}, Dry-Run: {dry_run}, Policy: Matrix Interleaving) ===")

        # 单轮内每个站点至多发布 1 个，循环轮转
        for site in self.TARGET_SITES_PRIORITY:
            if published_count >= limit_total:
                break

            # 每个站点只挑 1 个最佳候选，确保各站点交替雨露均沾
            cands = self.find_stock_candidates_for_site(site, limit=1)
            if not cands:
                continue

            cand = cands[0]
            item = cand["item"]
            adapter = cand["adapter"]
            album_name = cand["album_name"]

            if dry_run:
                logger.info(f"[Dry-Run] Would publish '{album_name}' to {site}")
                published_count += 1
                continue

            # 执行制种与自反验
            logger.info(f"Publishing stock album '{album_name}' to {site}...")
            ok, msg, t_path = self.engine.create_torrent_safe(item, site, getattr(adapter, "tracker_url", ""))
            if not ok or not t_path:
                logger.error(f"Failed to create torrent for {site}: {msg}")
                self.graph_mgr.record_publish_attempt(
                    album_name=album_name,
                    target_site=site,
                    result="FAILED",
                    reason=f"制种失败: {msg}",
                    origin_path=cand["local_path"],
                )
                continue

            try:
                pub_ok, tid, pub_torrent_bytes = adapter.publish(item, t_path)
                if pub_ok and tid:
                    logger.info(f"Published '{album_name}' to {site} successfully! tid={tid}")
                    # 读取种子字节，进入延迟回加队列（450s 保护，防偷跑）
                    with open(t_path, "rb") as tf:
                        local_bytes = tf.read()
                    readd_bytes = pub_torrent_bytes if pub_torrent_bytes else local_bytes
                    # 来龙去脉台账：源种 → 目标站 tid → B 种 hash → 交棒实例
                    self.graph_mgr.record_publish(
                        item,
                        site,
                        target_torrent_id=tid,
                        b_hash=torrent_infohash(readd_bytes),
                        target_url=f"{getattr(adapter, 'base_url', '')}/details.php?id={tid}",
                        qb_instance="publish",
                        qb_port=3010,
                        save_path="/downloads",
                        status="PUBLISHED",
                        publish_status="PUBLISHED",
                        note=album_name,
                    )
                    self.graph_mgr.record_publish_attempt(
                        album_name=album_name,
                        target_site=site,
                        result="PUBLISHED",
                        target_torrent_id=str(tid),
                        origin_path=cand["local_path"],
                    )

                    self.delayed_queue.add_task(
                        site_name=site,
                        torrent_id=str(tid),
                        item=item,
                        torrent_bytes=readd_bytes,
                        delay_seconds=450
                    )
                    published_count += 1
                    time.sleep(15)  # 站点间隔避峰
                else:
                    if is_duplicate_name_result(tid):
                        # 站点已有同 infohash / 同 info.name 条目：按「已在站」登记，避免每轮重复投喂
                        ex_tid = extract_existing_tid(tid)
                        logger.info(f"'{album_name}' 在 {site} 已有同种/同名条目 (TID: {ex_tid or 'unknown'})，登记为 EXISTING 并跳过")
                        self.graph_mgr.record_publish(
                            item, site,
                            target_torrent_id=ex_tid,
                            publish_status="EXISTING",
                            status="EXISTING",
                            note=str(tid)
                        )
                        self.graph_mgr.record_publish_attempt(
                            album_name=album_name,
                            target_site=site,
                            result="SKIPPED_EXISTING",
                            reason=str(tid),
                            target_torrent_id=ex_tid,
                            origin_path=cand["local_path"],
                        )
                    else:
                        logger.warning(f"Publish '{album_name}' to {site} failed: {tid}")
                        self.graph_mgr.record_publish_attempt(
                            album_name=album_name,
                            target_site=site,
                            result="REJECTED",
                            reason=str(tid),
                            origin_path=cand["local_path"],
                        )
            except Exception as ex:
                logger.exception(f"Unexpected error publishing '{album_name}' to {site}: {ex}")
                self.graph_mgr.record_publish_attempt(
                    album_name=album_name,
                    target_site=site,
                    result="FAILED",
                    reason=f"异常: {ex}",
                    origin_path=cand["local_path"],
                )
            finally:
                if t_path and os.path.exists(t_path):
                    try:
                        os.remove(t_path)
                    except Exception:
                        pass

        logger.info(f"=== Stock Matrix Pump Round Finished. Total Published: {published_count} ===")
        return published_count


if __name__ == "__main__":
    pump = StockMatrixPump()
    pump.run_stock_round(max_per_round=3, dry_run=False)
