# -*- coding: utf-8 -*-
"""MusicHub 3.0 今日双站特快专列 (ExpressPump: MuXueGe & TangPT)

目标与策略：
1. 锁定目标：MuXueGe (100) + TangPT (100) = 总计 200 个。
2. 调度节奏：
   - 黄金窗口期 (00:00 - 10:00)：两站交替轮流发布，每次间隔 90 秒（单站等效 180 秒）。
   - 日间平稳期 (10:00 - 24:00)：两站交替轮流发布，每次间隔 150 秒（单站等效 300 秒）。
3. 防御护栏：
   - 物理文件就绪自验 (verify_physical_data_readiness)
   - 站点专属前置检查 (pre_publish_check)
   - 站点在线查重 (check_exists)
   - 450 秒异步延迟回加 3010
"""

import os
import sys
import time
import datetime
import logging
from typing import List, Dict, Tuple, Optional, Any

from musichub.core.models import CanonicalMusicItem
from musichub.metadata.audio_inspector import AudioInspector
from musichub.routing.graph import RoutingGraphManager
from musichub.publisher.gateway import DeduplicationGateway
from musichub.publisher.engine import PublisherEngine
from musichub.publisher.delayed_queue import DelayedReaddQueue
from musichub.core.qb_client import QBClient
from musichub.adapters.sites import TangPTAdapter, MuXueGeAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ExpressPump")


class ExpressPump:
    TARGET_QUOTA = {
        "muxuege": 100,
        "tangpt": 100,
    }

    def __init__(
        self,
        db_path: str = "/app/musichub/data/musichub.db",
        pools: Optional[List[str]] = None,
    ):
        self.db_path = db_path
        self.pools = pools or [
            "/volume1/video/download/music/music3",
            "/volume1/video/download/music/music1",
            "/volume1/video/download/music/music2",
            "/volume1/video/download/music/music_pool_1",
        ]
        self.graph_mgr = RoutingGraphManager(db_path=self.db_path)
        self.publish_qb = QBClient("192.168.0.7", 3010)
        self.engine = PublisherEngine(self.publish_qb, self.graph_mgr)
        self.delayed_queue = DelayedReaddQueue(self.db_path, self.engine)

        self.adapters = {
            "muxuege": MuXueGeAdapter(),
            "tangpt": TangPTAdapter(),
        }
        self.inspector = AudioInspector()
        self.today_counts = {
            "muxuege": 0,
            "tangpt": 0,
        }

    def get_sleep_interval(self) -> int:
        """根据当前时间段决定发种间隔"""
        now = datetime.datetime.now()
        # 黄金窗口期 00:00 - 10:00：90 秒交替
        if 0 <= now.hour < 10:
            return 90
        # 日间平稳期：150 秒交替
        return 150

    def get_candidate_for_site(self, target_site: str) -> Optional[Dict[str, Any]]:
        adapter = self.adapters.get(target_site)
        if not adapter:
            return None

        for pool in self.pools:
            if not os.path.exists(pool):
                continue
            try:
                entries = sorted(os.listdir(pool))
            except Exception as e:
                logger.warning(f"Failed to list pool {pool}: {e}")
                continue

            for e in entries:
                if e.startswith(".") or e.startswith("@") or "incomplete" in e or "music5" in e:
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

                item = CanonicalMusicItem(
                    title=e,
                    album=album,
                    artist=artist,
                    year=year,
                    format=fmt,
                    medium=medium,
                    standard=standard,
                    origin_site="opencd",
                    origin_torrent_id=e,
                    local_path=full_p,
                    tracks=tracks,
                )

                # 1. 本地图查重
                if self.graph_mgr.is_loop_or_already_published(item, target_site):
                    continue

                # 2. 站点专属护栏校验
                chk_ok, chk_msg = adapter.pre_publish_check(item)
                if not chk_ok:
                    continue

                # 3. 物理数据就绪反验
                ready, reason = self.engine.verify_physical_data_readiness(item)
                if not ready:
                    continue

                # 4. 站点在线搜索查重
                exists, exists_why = adapter.check_exists(item)
                if exists:
                    self.graph_mgr.record_publish(item, target_site, target_torrent_id="EXISTING")
                    continue

                return {
                    "item": item,
                    "adapter": adapter,
                    "target_site": target_site,
                    "album_name": album,
                    "local_path": full_p,
                }
        return None

    def run_forever(self):
        logger.info("=== Starting Express Pump for MuXueGe & TangPT ===")
        logger.info(f"Targets: MuXueGe {self.TARGET_QUOTA['muxuege']} | TangPT {self.TARGET_QUOTA['tangpt']}")

        turn = 0
        site_list = ["muxuege", "tangpt"]

        while True:
            # 检查今日配额是否完成
            all_done = all(self.today_counts[s] >= self.TARGET_QUOTA[s] for s in site_list)
            if all_done:
                logger.info(f"🎉 Today's quota reached! Total published: {self.today_counts}. Mission accomplished.")
                break

            # 轮询站点
            site = site_list[turn % len(site_list)]
            turn += 1

            if self.today_counts[site] >= self.TARGET_QUOTA[site]:
                # 该站已满，尝试另一站
                continue

            logger.info(f"Looking for next candidate for {site} (Progress: {self.today_counts[site]}/{self.TARGET_QUOTA[site]})...")
            cand = self.get_candidate_for_site(site)
            if not cand:
                logger.warning(f"No candidate found for {site}. Skipping turn.")
                time.sleep(30)
                continue

            item = cand["item"]
            adapter = cand["adapter"]
            album_name = cand["album_name"]

            try:
                logger.info(f"[Publishing] '{album_name}' -> {site}")
                tracker_url = getattr(adapter, "tracker_url", "")
                ok, msg, torrent_path = self.engine.create_torrent_safe(item, site, tracker_url=tracker_url)
                if not ok or not torrent_path:
                    logger.error(f"Failed to create torrent for {site}: {msg}")
                    continue

                pub_ok, tid, pub_torrent_bytes = adapter.publish(item, torrent_path)
                if pub_ok and tid:
                    logger.info(f"✅ Successfully published '{album_name}' to {site}! TID: {tid}")
                    self.graph_mgr.record_publish(item, site, target_torrent_id=str(tid))
                    self.today_counts[site] += 1

                    # 读取种子字节，进入延迟回加队列（450s 保护，防偷跑）
                    with open(torrent_path, "rb") as tf:
                        local_bytes = tf.read()
                    readd_bytes = pub_torrent_bytes if pub_torrent_bytes else local_bytes
                    self.delayed_queue.add_task(
                        site_name=site,
                        torrent_id=str(tid),
                        item=item,
                        torrent_bytes=readd_bytes,
                        delay_seconds=450
                    )
                else:
                    logger.error(f"Failed to publish to {site}: {tid}")
            except Exception as e:
                logger.exception(f"Unexpected error publishing {album_name} to {site}: {e}")

            interval = self.get_sleep_interval()
            logger.info(f"Progress: MuXueGe {self.today_counts['muxuege']}/{self.TARGET_QUOTA['muxuege']}, TangPT {self.today_counts['tangpt']}/{self.TARGET_QUOTA['tangpt']}")
            logger.info(f"Waiting {interval}s before next express publish...")
            time.sleep(interval)


if __name__ == "__main__":
    pump = ExpressPump()
    pump.run_forever()
