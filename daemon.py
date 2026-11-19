# -*- coding: utf-8 -*-
"""MusicHub 3.0 总编排调度守护进程 (MusicHubDaemon)

核心架构与调度纪律：
1. 【分时段避让】：
   - 凌晨 00:00 - 10:00：PT 站点黄金发种期，将全部 IO 与带宽让位于发种与做种，跳过 GC/重度全库扫描；
   - 白天 10:00 - 23:30：常态化扫描、断种巡检（SeedPatrol）、安全退役（SafeGC）与孤儿清理。
2. 【自适应 Cooldown 待办感知】：
   - 待办就绪感知：当检测到 3016 有完成下载或待发布条目时，进入 30s 短周期快速连跑；
   - 队列清空时：自适应切回常规长周期（默认 300s）休眠，降低 CPU 与磁盘轮询消耗。
3. 【全链路防御闭环】：
   - 动态背压水位（StorageBackpressure）
   - 物理真实性硬校验（防幽灵做种）
   - 全局制种 IO 互斥锁与 t.verify 反验
   - 3010 回加与保活探针
   - 长期断种只读巡检（SeedPatrolManager）
"""

import time
import os
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from musichub.core.config import MusicHubConfig
from musichub.core.models import CanonicalMusicItem
from musichub.core.qb_client import QBClient
from musichub.storage.backpressure import StorageBackpressure
from musichub.storage.safe_gc import SafeGarbageCollector
from musichub.storage.handoff import ReadyHandoffManager, RollingPoolManager, PoolConfig
from musichub.routing.graph import RoutingGraphManager
from musichub.metadata.audio_inspector import AudioInspector
from musichub.patrol.seed_patrol import SeedPatrolManager
from musichub.adapters.sites import (
    MusopiaAdapter, CrabPTAdapter, PTerClubAdapter,
    XingwanAdapter, HDKylinAdapter, TangPTAdapter, MuXueGeAdapter,
    OpenCDAdapter
)
from musichub.publisher.gateway import DeduplicationGateway
from musichub.publisher.engine import PublisherEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("MusicHubDaemon")


def is_in_time_window(start_hour: int, start_minute: int, end_hour: int, end_minute: int, now: Optional[datetime] = None) -> bool:
    """判断当前时间是否落在指定的时间窗口内"""
    dt = now or datetime.now()
    now_min = dt.hour * 60 + dt.minute
    s_min = start_hour * 60 + start_minute
    e_min = end_hour * 60 + end_minute

    if s_min <= e_min:
        return s_min <= now_min <= e_min
    else:  # 跨午夜
        return now_min >= s_min or now_min <= e_min


class MusicHubDaemon:
    def __init__(self, config: MusicHubConfig):
        self.config = config
        
        # 1. 存储与安全治理
        self.backpressure = StorageBackpressure(
            mount_path=config.storage.disk_mount_path,
            green_gb=config.storage.watermark_green_gb,
            yellow_gb=config.storage.watermark_yellow_gb,
            red_gb=config.storage.watermark_red_gb,
        )
        self.safe_gc = SafeGarbageCollector(managed_pools=config.storage.library_pools)
        self.graph_mgr = RoutingGraphManager(db_path=config.db_path)

        # 2. qB 客户端集群接入
        self.qb_download = QBClient(
            host=config.musicdownload_qb.host,
            port=config.musicdownload_qb.port,
            username=config.musicdownload_qb.username,
            password=config.musicdownload_qb.password,
        )
        self.qb_publish = QBClient(
            host=config.publish_qb.host,
            port=config.publish_qb.port,
            username=config.publish_qb.username,
            password=config.publish_qb.password,
        )

        # 3. 移交与发种引擎（纳管滚动池与 3260 专用客户端）
        self.qb_opencd = QBClient(
            host=config.publish_qb.host,
            port=3260,
            username=config.publish_qb.username,
            password=config.publish_qb.password,
        )
        self.pool_mgr = RollingPoolManager(
            pools=[
                PoolConfig(name="music_pool_1", port=3210, mount_dir="/volume1/video/download/music/music_pool_1", host=config.publish_qb.host),
                PoolConfig(name="music_pool_2", port=3230, mount_dir="/volume1/video/download/music/music_pool_2", host=config.publish_qb.host),
                PoolConfig(name="music_pool_3", port=3250, mount_dir="/volume1/video/download/music/music_pool_3", host=config.publish_qb.host),
                PoolConfig(name="music_pool_4", port=3270, mount_dir="/volume1/video/download/music/music_pool_4", host=config.publish_qb.host),
                PoolConfig(name="music_pool_5", port=3280, mount_dir="/volume1/video/download/music/music_pool_5", host=config.publish_qb.host),
            ],
            qb_opencd=self.qb_opencd,
            opencd_pool_dir="/volume1/video/download/music/music3",
        )
        self.handoff_mgr = ReadyHandoffManager(
            qb_download=self.qb_download,
            pool_manager=self.pool_mgr,
        )
        self.publisher_engine = PublisherEngine(
            publish_qb_client=self.qb_publish,
            graph_manager=self.graph_mgr,
        )

        # 4. 8 大目标站点适配器
        self.adapters = [
            MusopiaAdapter(),
            CrabPTAdapter(),
            PTerClubAdapter(),
            XingwanAdapter(),
            HDKylinAdapter(),
            TangPTAdapter(),
            MuXueGeAdapter(),
            OpenCDAdapter(),
        ]
        self.adapters_map = {a.site_name.lower(): a for a in self.adapters}
        self.gateway = DeduplicationGateway(adapters=self.adapters, graph_mgr=self.graph_mgr)

        # 5. 断种常态化巡检管理器
        self.patrol_mgr = SeedPatrolManager(
            db_path=config.db_path,
            publish_qb_client=self.qb_publish,
            site_adapters=self.adapters_map,
        )

        self._running = False
        self._last_patrol_time = 0.0

    def is_golden_publish_window(self) -> bool:
        """东八区 00:00 - 10:00 黄金发种窗口判断"""
        return is_in_time_window(0, 0, 10, 0)

    def check_download_buffer_readiness(self, dry_run: bool = False) -> int:
        """检查 3016 缓冲完成下载任务，返回成功移交的任务数量"""
        try:
            # 查询 3016 客户端中 progress >= 1.0 的完成种
            all_torrents = self.qb_download.get_torrents() or []
            completed_torrents = [t for t in all_torrents if float(t.get("progress", 0.0)) >= 1.0]
            if not completed_torrents:
                logger.debug("No completed torrents in musicdownload (3016).")
                return 0

            logger.info(f"Found {len(completed_torrents)} completed torrents in buffer. Starting ingestion...")
            processed_count = 0
            for t in completed_torrents:
                content_path = t.get("content_path")
                name = t.get("name")
                info_hash = t.get("hash")
                size = t.get("size", 0)

                # 路径转换：qB 3016 内部汇报路径为 /downloads/...，宿主与 musichub 映射路径为 /volume2/qb_temp/completed_musicdownload/...
                if content_path and content_path.startswith("/downloads"):
                    rel_p = content_path[len("/downloads"):].lstrip("/\\")
                    real_local_path = os.path.join(self.config.storage.download_dir, rel_p)
                else:
                    real_local_path = content_path

                if not real_local_path or not os.path.exists(real_local_path):
                    logger.warning(f"Torrent '{name}' content_path does not exist on disk: {real_local_path} (raw: {content_path})")
                    continue

                # 解析来源站点（优先根据 tracker 判断）
                origin_tracker = t.get("tracker", "").lower()
                origin_site = "unknown"
                for s_key in ["opencd", "musopia", "crabpt", "pterclub", "xingwan", "btschool", "muxuege", "hdkylin", "tangpt"]:
                    if s_key in origin_tracker:
                        origin_site = s_key
                        break

                item_obj = CanonicalMusicItem(
                    album=name,
                    artist="Various Artists",
                    year="2026",
                    format="FLAC",
                    origin_site=origin_site,
                    origin_torrent_id=info_hash,
                    local_path=real_local_path,
                )

                if dry_run:
                    logger.info(f"[Dry-Run] Would handoff {name} ({real_local_path}) origin={origin_site}")
                    processed_count += 1
                    continue

                ok, msg = self.handoff_mgr.handoff_task(
                    item=item_obj,
                    ssd_path=real_local_path,
                )
                logger.info(f"Handoff {name}: {msg}")
                if ok and item_obj.content_path:
                    processed_count += 1
                    # 触发规范化构建与跨站派发流程
                    self._dispatch_item_pipeline(item_obj.content_path, name, info_hash, dry_run=dry_run)
            return processed_count
        except Exception as e:
            logger.error(f"Error checking download buffer: {str(e)}", exc_info=True)
            return 0

    def _dispatch_item_pipeline(self, local_path: str, album_name: str, origin_hash: str, dry_run: bool = False):
        """流式构建规范化对象并向目标站多向分发"""
        logger.info(f"Pipeline dispatch starting for: {album_name} ({local_path})")
        # 1. 音频 Header 解析
        insp = AudioInspector()
        tracks = insp.scan_album_directory(local_path)
        if not tracks:
            logger.warning(f"No valid audio tracks found in {local_path}, skipping dispatch.")
            return

        # 2. 构造规范化对象
        item = CanonicalMusicItem(
            album=album_name,
            artist=tracks[0].artist or "Various Artists",
            year="2026",
            format="FLAC",
            origin_site="opencd",
            origin_torrent_id=origin_hash,
            local_path=local_path,
            tracks=tracks,
        )

        # 3. 防幽灵做种硬校验
        ready, reason = self.publisher_engine.verify_physical_data_readiness(item)
        if not ready:
            logger.error(f"Physical readiness failed for {album_name}: {reason}")
            return

        # 4. 网关去重与目标站决策
        decisions = self.gateway.evaluate_targets(item)
        for adapter in self.adapters:
            site = adapter.site_name
            allowed, dec_reason = decisions.get(site, (False, "Not evaluated"))
            if not allowed:
                logger.info(f"Skip site {site} for {album_name}: {dec_reason}")
                continue

            if dry_run:
                logger.info(f"[Dry-Run] Target clear for {site}. Would publish {album_name}.")
                continue

            # 5. 执行安全打种并发布
            logger.info(f"Publishing {album_name} to {site}...")
            # 严格受控打种与校验
            created, c_msg, t_path = self.publisher_engine.create_torrent_safe(
                item=item,
                target_site=site,
                tracker_url=adapter.tracker_url,
            )
            if not created or not t_path:
                logger.error(f"Failed to create torrent for {site}: {c_msg}")
                continue

            try:
                with open(t_path, "rb") as f:
                    t_bytes = f.read()

                # NexusPHP 发布提交（传入本地 .torrent 文件路径）
                pub_ok, tid, pub_torrent_bytes = adapter.publish(item, t_path)
                if pub_ok and tid:
                    logger.info(f"Published successfully to {site}! Assigned torrent_id={tid}")
                    self.graph_mgr.record_publish(item, site, target_torrent_id=tid)

                    # 回加做种保活探针（优先使用发布后从站点拉回带有 passkey 的 B 种）
                    readd_bytes = pub_torrent_bytes if pub_torrent_bytes else t_bytes
                    readd_ok, r_msg = self.publisher_engine.readd_publish_with_keepalive_probe(
                        torrent_bytes=readd_bytes,
                        item=item,
                    )
                    logger.info(f"Keepalive probe for {site}: {r_msg}")
                else:
                    logger.warning(f"Publish to {site} failed: {pub_msg}")
            finally:
                if t_path and os.path.exists(t_path):
                    try:
                        os.remove(t_path)
                    except Exception:
                        pass

    def run_lifecycle_gc_check(self, dry_run: bool = True):
        """14 天生命周期退役与跨池安全 GC"""
        # 避让检查：东八区 00:00 - 10:00 黄金发种期避让，不做重型扫描
        if self.is_golden_publish_window():
            logger.info("[Window Evasion] 00:00 - 10:00 黄金发种窗口期，跳过日常孤儿扫描与 GC，保护磁盘 IO。")
            return

        logger.info(f"Starting lifecycle & GC patrol (dry_run={dry_run})...")
        active_names = set()
        try:
            torrents = self.qb_publish.get_torrents() or []
            for t in torrents:
                name = t.get("name")
                if name:
                    active_names.add(name)
        except Exception as e:
            logger.warning(f"Failed to fetch active torrents for GC from publish (3010): {str(e)}")

        # 调用 SafeGarbageCollector 扫描全池孤儿
        try:
            orphans = self.safe_gc.scan_unreferenced_orphans(active_names)
            logger.info(f"Found {len(orphans)} unreferenced orphan items across managed pools.")
            for orphan in orphans:
                ok, msg = self.safe_gc.safe_reclaim_orphan_cross_pools(orphan, dry_run=dry_run)
                logger.info(f"Orphan GC: {msg}")
        except Exception as e:
            logger.error(f"Error during orphan cross-pool scan: {str(e)}")

    def run_seed_patrol_check(self, limit: int = 50):
        """常态化断种只读巡检（每 2 小时或白天按需调度）"""
        if self.is_golden_publish_window():
            logger.info("[Window Evasion] 00:00 - 10:00 黄金发种窗口期，跳过全库断种巡检。")
            return

        now = time.time()
        # 至少间隔 1 小时巡检一次
        if now - self._last_patrol_time < 3600:
            return

        self._last_patrol_time = now
        logger.info("Executing periodic Seed Patrol audit...")
        try:
            records, stats = self.patrol_mgr.run_patrol(limit=limit)
            logger.info(f"Seed Patrol finished: Total={stats.get('TOTAL')}, SEEDING={stats.get('SEEDING')}, "
                        f"MISSING={stats.get('MISSING')}, PRESENT={stats.get('PRESENT')}, NO_TORRENT={stats.get('NO_SITE_TORRENT')}")
            missing_records = [r for r in records if r.status == "MISSING"]
            if missing_records:
                logger.warning(f"CRITICAL: Found {len(missing_records)} missing site torrents! Details:")
                for mr in missing_records[:5]:
                    logger.warning(f"  - Site: {mr.target_site}, TID: {mr.target_torrent_id}, Note: {mr.note}")
        except Exception as e:
            logger.error(f"Error during Seed Patrol audit: {e}")

    def run_once(self, dry_run: bool = False) -> int:
        """单次主循环，返回处理的有待办事项数"""
        # 1. 背压门禁
        level, free_gb = self.backpressure.assess_watermark()
        logger.info(f"Storage watermark: {level} ({free_gb:.1f} GB free)")
        if level == "RED":
            logger.warning("RED watermark triggered! Suspending new ingestion and running safe GC.")
            self.run_lifecycle_gc_check(dry_run=dry_run)
            return 0

        # 2. 检查 3016 缓冲完成并流式移交
        pending_handled = self.check_download_buffer_readiness(dry_run=dry_run)

        # 3. 运行日常安全退役考核（默认干跑模式防误删）
        self.run_lifecycle_gc_check(dry_run=True)

        # 4. 常态化断种只读巡检
        self.run_seed_patrol_check(limit=50)

        return pending_handled

    def start(self, default_interval: int = 300, cooldown_interval: int = 30):
        """【自适应 Cooldown 循环调度】
        有活时（pending_handled > 0）以 cooldown_interval(30s) 快速连跑；
        清零后自动切回 default_interval(300s) 休眠。
        """
        self._running = True
        logger.info(f"MusicHub 3.0 Daemon started with adaptive cooldown ({cooldown_interval}s active / {default_interval}s idle).")
        while self._running:
            try:
                handled = self.run_once()
                if handled > 0:
                    logger.info(f"Pending tasks processed ({handled}). Entering fast cooldown ({cooldown_interval}s)...")
                    sleep_time = cooldown_interval
                else:
                    sleep_time = default_interval
            except Exception as e:
                logger.error(f"Error in daemon cycle: {str(e)}", exc_info=True)
                sleep_time = default_interval

            time.sleep(sleep_time)

    def stop(self):
        self._running = False
        logger.info("MusicHub 3.0 Daemon stopped.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MusicHub 3.0 Daemon")
    parser.add_argument("--dry-run", action="store_true", help="Run in dry-run mode (read-only, no publish or file modifications)")
    parser.add_argument("--loop", action="store_true", help="Run daemon continuously in loop")
    parser.add_argument("--interval", type=int, default=300, help="Idle loop interval in seconds")
    parser.add_argument("--cooldown", type=int, default=30, help="Fast cooldown interval in seconds")
    args = parser.parse_args()

    cfg = MusicHubConfig.from_env()
    daemon = MusicHubDaemon(cfg)

    if args.loop:
        daemon.start(default_interval=args.interval, cooldown_interval=args.cooldown)
    else:
        daemon.run_once(dry_run=args.dry_run)
