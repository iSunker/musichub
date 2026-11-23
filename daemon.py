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
import sqlite3
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

from musichub.core.config import MusicHubConfig
from musichub.core.models import CanonicalMusicItem
from musichub.core.qb_client import QBClient
from musichub.collector.rss_watcher import RSSWatcher
from musichub.collector.source_detail import (
    SourceDetailFetcher, detect_source_site, extract_tid,
)
from musichub.storage.backpressure import StorageBackpressure
from musichub.storage.safe_gc import SafeGarbageCollector
from musichub.storage.lifecycle import LifecycleRetirement
from musichub.storage.handoff import ReadyHandoffManager, RollingPoolManager, PoolConfig
from musichub.storage.speed_governor import SpeedGovernor
from musichub.storage.download_governor import MultiPoolDownloadGovernor
from musichub.storage.handoff_governor import HandoffGovernor
from musichub.storage.pool_pairing import PoolPairingManager
from musichub.routing.graph import RoutingGraphManager, torrent_infohash
from musichub.metadata.audio_inspector import AudioInspector
from musichub.patrol.seed_patrol import SeedPatrolManager
from musichub.adapters.sites import (
    MusopiaAdapter, CrabPTAdapter, PTerClubAdapter,
    XingwanAdapter, HDKylinAdapter, TangPTAdapter, MuXueGeAdapter,
    OpenCDAdapter, UBitsAdapter, SoulVoiceAdapter,
    LemonHDAdapter, HDTimeAdapter
)
from musichub.adapters.nexusphp_base import is_duplicate_name_result
from musichub.publisher.gateway import DeduplicationGateway
from musichub.publisher.engine import PublisherEngine
from musichub.publisher.delayed_queue import DelayedReaddQueue
from musichub.publisher.queue_janitor import DownloadQueueJanitor
from musichub.publisher.stock_pump import StockMatrixPump

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
        # 启动即从 DB 重建一次 Vertex 防环账本：账本文件丢失/被覆盖时能自愈，
        # 不必等下一次发种才恢复（2026-11-21 账本被清空导致 3016 回环）。
        try:
            self.graph_mgr.export_vertex_blocklist()
        except Exception as e:
            logger.warning(f"[Init] 启动导出 Vertex 防环账本失败: {e}")

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

        # 3. 移交与发种引擎（纳管统一 8 实例保种池，完全平权负载均衡）
        self.pool_mgr = RollingPoolManager(
            pools=[
                PoolConfig(name="music_pool_1", port=3210, mount_dir="/volume1/video/download/music/music_pool_1", host=config.publish_qb.host),
                PoolConfig(name="music_pool_2", port=3220, mount_dir="/volume1/video/download/music/music_pool_2", host=config.publish_qb.host),
                PoolConfig(name="music_pool_3", port=3230, mount_dir="/volume1/video/download/music/music_pool_3", host=config.publish_qb.host),
                PoolConfig(name="music_pool_4", port=3240, mount_dir="/volume1/video/download/music/music_pool_4", host=config.publish_qb.host),
                PoolConfig(name="music_pool_5", port=3250, mount_dir="/volume1/video/download/music/music_pool_5", host=config.publish_qb.host),
                PoolConfig(name="music_pool_6", port=3260, mount_dir="/volume1/video/download/music/music_pool_6", host=config.publish_qb.host),
                PoolConfig(name="music_pool_7", port=3270, mount_dir="/volume1/video/download/music/music_pool_7", host=config.publish_qb.host),
                PoolConfig(name="music_pool_8", port=3280, mount_dir="/volume1/video/download/music/music_pool_8", host=config.publish_qb.host),
            ]
        )
        self.handoff_mgr = ReadyHandoffManager(
            qb_download=self.qb_download,
            pool_manager=self.pool_mgr,
            # 摄取台账：每次搬移/注入/回滚写 ingest_log，可反查「源种在哪做种」
            graph_manager=self.graph_mgr,
        )
        # 发布期音乐池配对：发种成功即向承载实体数据的池注入同 hash 的 paused 影子种，
        # 交棒器才能按 hash 命中并安全下架 3010。
        self.pool_pairing = PoolPairingManager(
            publish_host=config.publish_qb.host,
            graph_manager=self.graph_mgr,
        )
        self.publisher_engine = PublisherEngine(
            publish_qb_client=self.qb_publish,
            graph_manager=self.graph_mgr,
            pool_pairing=self.pool_pairing,
        )

        # 4. 目标站点适配器（全矩阵目标站）
        self.adapters = [
            MusopiaAdapter(),
            CrabPTAdapter(),
            PTerClubAdapter(),
            XingwanAdapter(),
            HDKylinAdapter(),
            TangPTAdapter(),
            MuXueGeAdapter(),
            OpenCDAdapter(),
            UBitsAdapter(),
            SoulVoiceAdapter(),
            LemonHDAdapter(),
            HDTimeAdapter(),
        ]
        self.adapters_map = {a.site_name.lower(): a for a in self.adapters}
        self.gateway = DeduplicationGateway(adapters=self.adapters, graph_mgr=self.graph_mgr)

        # 4b. 源种溯源与详情元数据（多源准入的前置依赖）
        self.source_fetcher = SourceDetailFetcher()
        self.rss_watcher = RSSWatcher(qb_download=self.qb_download)
        self._last_meta_topup_time = 0.0

        # 5. 断种常态化巡检管理器
        self.patrol_mgr = SeedPatrolManager(
            db_path=config.db_path,
            publish_qb_client=self.qb_publish,
            pool_qb_clients={
                spec.name: self.pool_pairing.client_for(spec)
                for spec in self.pool_pairing.specs
            },
            site_adapters=self.adapters_map,
        )

        # 5b. 14+10 天生命周期安全退役调度器
        all_clients = {
            spec.name: self.pool_pairing.client_for(spec)
            for spec in self.pool_pairing.specs
        }
        all_clients["publish"] = self.qb_publish
        self.lifecycle = LifecycleRetirement(
            graph_mgr=self.graph_mgr,
            safe_gc=self.safe_gc,
            target_clients=all_clients,
            min_source_days=14.0,
            min_latest_days=10.0,
            hr_hours_required=36.0,
        )

        # 6. 存量矩阵补种调度器 (StockMatrixPump)
        self.delayed_queue = DelayedReaddQueue(db_path=config.db_path, engine=self.publisher_engine)
        self.stock_pump = StockMatrixPump(
            db_path=config.db_path,
            pools=self.config.storage.library_pools,
            publish_qb=self.qb_publish,
            engine=self.publisher_engine,
            delayed_queue=self.delayed_queue,
        )
        self._last_stock_pump_time = self._init_last_stock_pump_time()

        # 7. qb3010 阶梯调速器与常驻交棒调度器
        self.speed_governor = SpeedGovernor(
            qb_host=config.publish_qb.host,
            qb_port=config.publish_qb.port,
            total_bandwidth_bps=5120000,
            check_interval=7.0,
        )
        self.handoff_governor = HandoffGovernor(
            qb_publish_host=config.publish_qb.host,
            qb_publish_port=config.publish_qb.port,
            min_ratio=1.05,
            min_seed_hours=2.0,
            min_seeders=3,
            max_seed_hours_fallback=6.0,
            ssd_publish_dir=config.storage.publish_dir,
            graph_manager=self.graph_mgr,
            index_ttl=1800.0,
        )
        self._last_handoff_check_time = 0.0
        self._speed_governor_thread = None
        self._readd_thread = None
        self._handoff_thread = None

        # 8. 3016 下载队列清道夫：清理无做种者且零进度的 stalledDL，释放并发下载槽
        self.queue_janitor = DownloadQueueJanitor(
            qb_client=self.qb_download,
            interval_seconds=3600,
            min_age_hours=2.0,
            max_delete_per_round=20,
        )
        self._janitor_thread = None

        # 9. 8 个核心音乐 QB 实例多池阶梯下载治理器 (MultiPoolDownloadGovernor)
        self.download_governor = MultiPoolDownloadGovernor(
            host=config.publish_qb.host,
            concurrency_per_pool=20,
            stalled_timeout=300,
            check_interval=15.0,
        )
        self._download_governor_thread = None

        self._running = False
        self._last_patrol_time = 0.0

    def _init_last_stock_pump_time(self) -> float:
        """从发布流水账本恢复最近存量补种发种时间，防止容器重启立刻早熟连发"""
        try:
            with sqlite3.connect(self.config.db_path) as conn:
                r = conn.execute(
                    "SELECT created_at FROM publish_attempt_log WHERE result='PUBLISHED' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if r and r[0]:
                    dt_utc = datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    last_ts = dt_utc.timestamp()
                    logger.info(f"[Init] 从账本恢复最近存量补种时间戳: {last_ts} ({datetime.fromtimestamp(last_ts)})")
                    return last_ts
        except Exception as e:
            logger.warning(f"[Init] 恢复最近发种时间戳异常: {e}")
        return 0.0

    def is_golden_publish_window(self) -> bool:
        """东八区 00:00 - 10:00 黄金发种窗口判断"""
        return is_in_time_window(0, 0, 10, 0)

    def check_download_buffer_readiness(self, dry_run: bool = False, max_batch: int = 5) -> int:
        """检查 3016 缓冲完成下载任务，返回成功移交的任务数量

        max_batch 限制单轮条数：每条移交都要「mv + 注入 + 最多 30s 进度回读」，
        不设上限时一次积压几十条会把主循环节拍拖到十几分钟。
        """
        try:
            # 查询 3016 客户端中 progress >= 1.0 的完成种
            all_torrents = self.qb_download.get_torrents() or []
            completed_torrents = [t for t in all_torrents if float(t.get("progress", 0.0)) >= 1.0]
            if not completed_torrents:
                logger.debug("No completed torrents in musicdownload (3016).")
                return 0

            # 先完成先处理，避免新完成的任务挤掉长期待移交的
            completed_torrents.sort(
                key=lambda x: float(x.get("completion_on") or x.get("added_on") or 0.0)
            )
            batch = completed_torrents[: max(1, int(max_batch or 1))]

            logger.info(
                f"Found {len(completed_torrents)} completed torrents in buffer, "
                f"processing {len(batch)} this round. Starting ingestion..."
            )
            processed_count = 0
            for t in batch:
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

                # 登记源种任务并补齐详情元数据（源站/ tid / 转载规则 / 简介正文）
                task = self._ensure_source_task(t, info_hash, name)
                origin_site = task.get("source_site") or "unknown"
                source_tid = str(task.get("source_tid") or "")

                item_obj = CanonicalMusicItem(
                    title=task.get("source_name") or name,
                    album=task.get("album") or name,
                    artist=task.get("artist") or "Various Artists",
                    year=task.get("year") or "",
                    format=task.get("format") or "FLAC",
                    medium=task.get("medium") or "CD",
                    standard=task.get("standard") or "Lossless",
                    origin_site=origin_site,
                    origin_torrent_id=source_tid,
                    origin_info_hash=info_hash,
                    local_path=real_local_path,
                )

                if dry_run:
                    logger.info(f"[Dry-Run] Would handoff {name} ({real_local_path}) origin={origin_site}")
                    processed_count += 1
                    continue

                # 移交内部会从 3016 导出 .torrent、mv 到机械盘、在目标池注入并转正做种，
                # 校验进度 100% 后才下架 3016 任务（delete_files=False）。
                # 调用方不再需要自己传 torrent_bytes（2026-11-21 修掉「复制完即删源」缺口）。
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

    def _ensure_source_task(self, torrent: Dict, info_hash: str, name: str) -> Dict:
        """登记源种任务（3016 完成任务 -> source_tasks），并按需补齐详情元数据

        源站识别顺序：Vertex RSS 账本的 tracker -> 3016 任务自带 tracker；
        tid 来源：Vertex `link` 的 details.php?id -> 任务 comment 里的详情页链接。
        识别不出的落 `unknown`，任务表默认 `NEED_CONFIRM`，不会误发。
        """
        if not info_hash:
            return {}
        v_meta = self.rss_watcher.get_vertex_origin_metadata(info_hash) or {}
        tracker_text = v_meta.get("tracker") or torrent.get("tracker") or ""
        source_site = detect_source_site(tracker_text)
        source_tid = extract_tid(v_meta.get("link") or "") or extract_tid(torrent.get("comment") or "")
        self.graph_mgr.upsert_source_task(
            info_hash=info_hash,
            source_site=source_site or "unknown",
            source_tid=source_tid,
            source_name=v_meta.get("name") or name or "",
        )
        task = self.graph_mgr.get_source_task(info_hash) or {}
        if task.get("meta_status") == "NEED_META":
            self._refresh_source_meta(task)
            task = self.graph_mgr.get_source_task(info_hash) or task
        return task

    def _refresh_source_meta(self, task: Dict) -> bool:
        """抓取单个源种详情页并落库；失败保持 NEED_META 交由补抓线程重试"""
        info_hash = task.get("info_hash") or ""
        site = str(task.get("source_site") or "").lower()
        tid = str(task.get("source_tid") or "").strip()
        if not info_hash or site in ("", "unknown") or not tid:
            return False

        result = self.source_fetcher.fetch(site, tid)
        if not result.get("ok"):
            logger.info(f"[SourceMeta] {site} tid={tid} 抓取失败: {result.get('error')}")
            return False

        # 有页面转载规则的以页面为准；没有该字段的站按站点默认（OpenCD 保守为待确认）
        rule = result.get("transfer_rule") or ("NEED_CONFIRM" if site == "opencd" else "ALLOW")
        self.graph_mgr.update_source_meta(
            info_hash,
            source_site=site,
            source_tid=str(result.get("source_tid") or tid or "").strip(),
            artist=result.get("artist", ""),
            album=result.get("album", ""),
            year=result.get("year", ""),
            format=result.get("format") or self._guess_format(result),
            medium=result.get("medium") or "CD",
            standard="Lossless",
            cover_url=result.get("cover_url", ""),
            description=result.get("description", ""),
            log_text=result.get("log_text", ""),
            tracklist_json=result.get("tracklist", ""),
            genre_json=result.get("genre", ""),
            meta_status="OK",
        )
        self.graph_mgr.set_transfer_rule(info_hash, rule, note=f"source={site}#{tid}")
        logger.info(f"[SourceMeta] {site} tid={tid} 元数据已补齐（rule={rule}）")
        return True

    @staticmethod
    def _guess_format(result: Dict) -> str:
        """从详情页「格式」字段猜测音频编码，猜不出按 FLAC"""
        raw = f"{result.get('format', '')} {result.get('title', '')}".upper()
        for codec in ("FLAC", "APE", "WAV", "AAC", "MP3", "DSD", "DSF", "DFF", "ALAC"):
            if codec in raw:
                return "DSD" if codec in ("DSD", "DSF", "DFF") else codec
        return "FLAC"

    def topup_source_metadata(self, limit: int = 20) -> int:
        """补抓线程/主循环调用：逐条重试 NEED_META 任务（逐条 1s 间隔，避免打爆站点）"""
        done = 0
        try:
            tasks = self.graph_mgr.list_source_tasks(meta_status="NEED_META", limit=limit)
        except Exception as e:
            logger.error(f"[SourceMeta] 读取待补任务失败: {e}")
            return 0
        for task in tasks:
            try:
                if self._refresh_source_meta(task):
                    done += 1
            except Exception as e:
                logger.warning(f"[SourceMeta] 补抓异常 {task.get('info_hash')}: {e}")
            time.sleep(1.0)
        if tasks:
            logger.info(f"[SourceMeta] 本轮补抓 {done}/{len(tasks)} 条")
        return done

    def _dispatch_item_pipeline(self, local_path: str, album_name: str, origin_hash: str, dry_run: bool = False):
        """流式构建规范化对象并向目标站多向分发"""
        logger.info(f"Pipeline dispatch starting for: {album_name} ({local_path})")
        # 1. 音频 Header 解析
        insp = AudioInspector()
        tracks = insp.scan_album_directory(local_path)
        if not tracks:
            logger.warning(f"No valid audio tracks found in {local_path}, skipping dispatch.")
            return

        # 2. 构造规范化对象（元数据优先取源种任务表，缺项退回音频标签/目录名解析）
        task = self.graph_mgr.get_source_task(origin_hash) or {}
        name_meta = AudioInspector.parse_name_metadata(album_name)
        artist = task.get("artist") or name_meta.get("artist") or (
            tracks[0].artist if tracks else "Various Artists"
        )
        item = CanonicalMusicItem(
            title=task.get("source_name") or album_name,
            # 专辑名以目录名解析为准（种子名校验最可靠），详情页字段作补充
            album=name_meta.get("album") or task.get("album") or album_name,
            artist=artist,
            year=task.get("year") or name_meta.get("year") or "",
            format=task.get("format") or name_meta.get("format") or "FLAC",
            medium=task.get("medium") or name_meta.get("medium") or "CD",
            standard=task.get("standard") or name_meta.get("standard") or "Lossless",
            origin_site=task.get("source_site") or "unknown",
            origin_torrent_id=str(task.get("source_tid") or ""),
            origin_info_hash=origin_hash,
            description=task.get("description") or "",
            cover_url=task.get("cover_url") or "",
            log_text=task.get("log_text") or "",
            has_log=bool(task.get("log_text")),
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
                self.graph_mgr.record_publish_attempt(
                    album_name=album_name,
                    target_site=site,
                    result="SKIPPED",
                    reason=dec_reason,
                    origin_path=local_path,
                )
                continue

            if dry_run:
                logger.info(f"[Dry-Run] Target clear for {site}. Would publish {album_name}.")
                continue

            # 5. 执行安全打种并发布
            logger.info(f"Publishing {album_name} to {site}...")
            # 严格受控打种与校验
            tracker_url = getattr(adapter, "tracker_url", "")
            created, c_msg, t_path = self.publisher_engine.create_torrent_safe(
                item=item,
                target_site=site,
                tracker_url=tracker_url,
            )
            if not created or not t_path:
                logger.error(f"Failed to create torrent for {site}: {c_msg}")
                self.graph_mgr.record_publish_attempt(
                    album_name=album_name,
                    target_site=site,
                    result="FAILED",
                    reason=f"制种失败: {c_msg}",
                    origin_path=local_path,
                )
                continue

            try:
                with open(t_path, "rb") as f:
                    t_bytes = f.read()

                # NexusPHP 发布提交（传入本地 .torrent 文件路径）
                pub_ok, tid, pub_torrent_bytes = adapter.publish(item, t_path)
                if pub_ok and tid:
                    logger.info(f"Published successfully to {site}! Assigned torrent_id={tid}")
                    # 来龙去脉台账：源种 → 目标站 tid → B 种 hash → 交棒实例
                    readd_bytes = pub_torrent_bytes if pub_torrent_bytes else t_bytes
                    b_hash = torrent_infohash(readd_bytes)
                    self.graph_mgr.record_publish(
                        item,
                        site,
                        target_torrent_id=tid,
                        b_hash=b_hash,
                        target_url=f"{getattr(adapter, 'base_url', '')}/details.php?id={tid}",
                        qb_instance="publish",
                        qb_port=3010,
                        save_path="/downloads",
                        status="PUBLISHED",
                        note=album_name,
                    )
                    self.graph_mgr.record_publish_attempt(
                        album_name=album_name,
                        target_site=site,
                        result="PUBLISHED",
                        target_torrent_id=str(tid),
                        origin_path=local_path,
                    )

                    # 回加做种保活探针（优先使用发布后从站点拉回带有 passkey 的 B 种）
                    readd_ok, r_msg = self.publisher_engine.readd_publish_with_keepalive_probe(
                        torrent_bytes=readd_bytes,
                        item=item,
                        timeout_seconds=120,
                    )
                    logger.info(f"Keepalive probe for {site}: {r_msg}")
                else:
                    if is_duplicate_name_result(tid):
                        logger.info(f"'{album_name}' 在 {site} 已有同种/同名条目，登记为 EXISTING 并跳过")
                        self.graph_mgr.record_publish(
                            item, site, target_torrent_id="EXISTING", status="EXISTING", note=str(tid)
                        )
                        self.graph_mgr.record_publish_attempt(
                            album_name=album_name,
                            target_site=site,
                            result="SKIPPED_EXISTING",
                            reason=str(tid),
                            target_torrent_id="EXISTING",
                            origin_path=local_path,
                        )
                    else:
                        logger.warning(f"Publish to {site} failed: {tid}")
                        self.graph_mgr.record_publish_attempt(
                            album_name=album_name,
                            target_site=site,
                            result="REJECTED",
                            reason=str(tid),
                            origin_path=local_path,
                        )
            except Exception as ex:
                logger.exception(f"Unexpected error publishing {album_name} to {site}: {ex}")
                self.graph_mgr.record_publish_attempt(
                    album_name=album_name,
                    target_site=site,
                    result="FAILED",
                    reason=f"异常: {ex}",
                    origin_path=local_path,
                )
            finally:
                if t_path and os.path.exists(t_path):
                    try:
                        os.remove(t_path)
                    except Exception:
                        pass

    def _collect_pool_active_names(self) -> Optional[set]:
        """汇总全部受管音乐池当前任务名（交棒后 3010 已无该任务，池内仍在做种）

        fail-closed：任一池读取失败即返回 None，调用方必须放弃本轮真删。
        qB 客户端默认把异常吞成空列表，这里必须用 strict=True 才能识别失败。
        """
        names = set()
        for spec in self.pool_pairing.specs:
            try:
                for t in self.pool_pairing.client_for(spec).get_torrents(strict=True) or []:
                    name = t.get("name")
                    if name:
                        names.add(name)
            except Exception as e:
                logger.error(f"[GC] 读取池 {spec.name} 任务列表失败，本轮放弃真删（fail-closed）: {e}")
                return None
        return names

    def run_lifecycle_gc_check(self, dry_run: bool = True):
        """14+10 天生命周期安全退役与跨池安全 GC"""
        # 避让检查：东八区 00:00 - 10:00 黄金发种期避让，不做重型扫描
        if self.is_golden_publish_window():
            logger.info("[Window Evasion] 00:00 - 10:00 黄金发种窗口期，跳过日常孤儿扫描与 GC，保护磁盘 IO。")
            return

        logger.info(f"Starting lifecycle & GC patrol (dry_run={dry_run})...")

        # 1. 阶段一：审查 14+10 天退役候选（源种>=14天、最晚站>=10天、矩阵充分扩散）
        try:
            candidates = self.lifecycle.find_retireable_candidates(limit=5)
            if candidates:
                logger.info(f"[Lifecycle] 发现 {len(candidates)} 个满足 14+10 天与矩阵扩散门禁的退役候选专辑。")
                if not dry_run:
                    for c in candidates:
                        fp = c["content_fingerprint"]
                        logger.info(f"[Lifecycle] 正在对专辑 {fp[:16]} 执行跨池安全脱退任务 (delete_files=False)...")
                        res = self.lifecycle.retire_album_across_clients(fp, dry_run=False)
                        logger.info(f"[Lifecycle] 脱退完成: {res.get('detached_tasks')} 个任务已从 qB 下架。")
                else:
                    for c in candidates:
                        logger.info(f"[Lifecycle][DRY-RUN] 候选专辑 {c['content_fingerprint'][:16]} (发了 {c['site_count']} 站，最晚: {c['last_published_at']})")
        except Exception as e:
            logger.error(f"[Lifecycle] 检查退役候选失败: {e}")

        # 2. 阶段二：全池孤儿扫描与跨池协同 GC
        active_names = set()
        try:
            # 真删场景必须严格取数；3010 读不到就不能认定任何目录是孤儿
            torrents = self.qb_publish.get_torrents(strict=not dry_run) or []
            for t in torrents:
                name = t.get("name")
                if name:
                    active_names.add(name)
        except Exception as e:
            if dry_run:
                logger.warning(f"Failed to fetch active torrents for GC from publish (3010): {str(e)}")
            else:
                logger.error(f"[GC] 读取 3010 任务列表失败，本轮放弃真删（fail-closed）: {e}")
                return

        # 调用 SafeGarbageCollector 扫描全池孤儿
        try:
            orphans = self.safe_gc.scan_unreferenced_orphans(active_names)
            logger.info(f"Found {len(orphans)} unreferenced orphan items across managed pools.")

            # 真删前必须叠加池内任务引用，防止把「已交棒、正在池内做种」的目录当孤儿回收。
            # 只在确有孤儿且非干跑时查询，避免每轮 8 个实例的全量列表开销。
            if orphans and not dry_run:
                pool_names = self._collect_pool_active_names()
                if pool_names is None:
                    logger.error("[GC] 池任务名汇总不完整，本轮放弃真删（fail-closed）")
                    return
                before = len(orphans)
                orphans = [o for o in orphans if o.get("name") not in pool_names]
                if before != len(orphans):
                    logger.info(f"[GC] 已排除 {before - len(orphans)} 个仍被池内任务引用的目录")

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
            logger.info(
                f"Seed Patrol finished: Total={stats.get('TOTAL')}, SEEDING={stats.get('SEEDING')}, "
                f"MISSING={stats.get('MISSING')}, PAUSED={stats.get('PAUSED')}, "
                f"AMBIGUOUS={stats.get('AMBIGUOUS')}, PRESENT={stats.get('PRESENT')}, "
                f"NO_TORRENT={stats.get('NO_SITE_TORRENT')}, FETCH_ERROR={stats.get('FETCH_ERROR')}, "
                f"QB_CLIENT_ERROR={stats.get('QB_CLIENT_ERROR')}"
            )
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

        # 2. 检查 3016 缓冲完成并移交（单轮最多 5 条，避免长等待拖垮主循环）
        pending_handled = self.check_download_buffer_readiness(dry_run=dry_run, max_batch=5)

        # 2a. 源种元数据补抓：10 分钟一轮，每轮最多 20 条（抓不到的保持 NEED_META 被严格站拦住）
        now_meta = time.time()
        if now_meta - self._last_meta_topup_time >= 600.0:
            self.topup_source_metadata(limit=20)
            self._last_meta_topup_time = now_meta

        # 2b. 黄金发种期存量补种泵：00:00 - 10:00 期间，每 30 分钟 (1800s) 启动一轮，每轮 11 站各发 1 个
        now_ts = time.time()
        if self.is_golden_publish_window():
            if now_ts - self._last_stock_pump_time >= 1800.0:
                logger.info("[StockMatrixPump] 处于黄金发种窗口期 (00:00 - 10:00)，启动半小时存量补种轮询（11站每站各发1个）...")
                pumped = self.stock_pump.run_stock_round(max_per_round=len(self.stock_pump.TARGET_SITES_PRIORITY), dry_run=dry_run)
                self._last_stock_pump_time = now_ts
                pending_handled += pumped

        # 3. 运行日常安全退役考核（默认干跑模式防误删）
        self.run_lifecycle_gc_check(dry_run=True)

        # 4. 常态化断种只读巡检
        self.run_seed_patrol_check(limit=50)

        # 5. 3010 达标交棒已迁移至独立线程 _handoff_loop（300s/轮、max_batch=30），
        #    不再占用主循环节拍，也不再被 900s 门限限制。

        return pending_handled

    def start(self, default_interval: int = 300, cooldown_interval: int = 30):
        """【自适应 Cooldown 循环调度】
        有活时（pending_handled > 0）以 cooldown_interval(30s) 快速连跑；
        清零后自动切回 default_interval(300s) 休眠。
        """
        import threading
        self._running = True

        # 启动常驻高频阶梯调速器独立守护线程 (7秒/次)
        def _speed_loop():
            logger.info("[SpeedGovernor] 独立调速守护线程已启动 (周期 7s，红线 5MB/s)...")
            # 先确保 3010 的上传并发槽足够，否则 200+ 种子会被压在 queuedUP 吃不满带宽
            try:
                self.speed_governor.ensure_queue_prefs()
            except Exception as ex:
                logger.error(f"[SpeedGovernor] 队列参数自愈失败: {ex}")
            while self._running:
                try:
                    self.speed_governor.apply_step(dry_run=False)
                except Exception as ex:
                    logger.error(f"[SpeedGovernor] 调速异常: {ex}")
                time.sleep(self.speed_governor.check_interval)
            logger.info("[SpeedGovernor] 独立调速守护线程已停止。")

        self._speed_governor_thread = threading.Thread(target=_speed_loop, daemon=True, name="SpeedGovernorThread")
        self._speed_governor_thread.start()

        # 启动延迟回加队列消费线程 (30s/轮，每轮最多 3 条；独立线程避免 120s 保活探针阻塞主循环)
        def _readd_loop():
            logger.info("[DelayedReadd] 回加队列消费线程已启动 (周期 30s，每轮最多 3 条)...")
            while self._running:
                try:
                    res = self.delayed_queue.process_pending_readds(max_tasks=3)
                    if res:
                        ok = sum(1 for r in res if r.get("success"))
                        logger.info(f"[DelayedReadd] 本轮处理 {len(res)} 条，成功回加 {ok} 条")
                except Exception as ex:
                    logger.error(f"[DelayedReadd] 队列消费异常: {ex}")
                time.sleep(30)
            logger.info("[DelayedReadd] 回加队列消费线程已停止。")

        self._readd_thread = threading.Thread(target=_readd_loop, daemon=True, name="DelayedReaddThread")
        self._readd_thread.start()

        # 启动下载队列清道夫线程 (3600s/轮，每轮最多 20 条；只清「零进度 + 无做种者」的 stalledDL)
        def _janitor_loop():
            logger.info("[QueueJanitor] 无源停滞任务清理线程已启动 (周期 3600s，每轮最多 20 条)...")
            while self._running:
                try:
                    res = self.queue_janitor.run_once()
                    if res.get("deleted"):
                        logger.info(
                            "[QueueJanitor] 本轮清理 %d 个无源停滞任务，扫描 %d 命中 %d"
                            % (res["deleted"], res["scanned"], res["matched"])
                        )
                        for it in res.get("items", [])[:10]:
                            logger.info(
                                "[QueueJanitor]   - %s %s %s (age=%.1fh)"
                                % (it["hash"], it["site"], it["name"], it["age_hours"])
                            )
                except Exception as ex:
                    logger.error(f"[QueueJanitor] 清理异常: {ex}")
                time.sleep(self.queue_janitor.interval_seconds)
            logger.info("[QueueJanitor] 无源停滞任务清理线程已停止。")

        self._janitor_thread = threading.Thread(target=_janitor_loop, daemon=True, name="QueueJanitorThread")
        self._janitor_thread.start()

        # 启动 3010 达标交棒独立线程 (120s/轮、每轮最多 60 个；批量停源/唤醒/下架，单轮 3~5s 收口)
        def _handoff_loop():
            logger.info("[HandoffGovernor] 达标交棒独立线程已启动 (周期 120s，每轮最多 60 个，批量流水线)...")
            last_reconcile = 0.0
            last_dedupe = 0.0
            deduper = None
            while self._running:
                try:
                    # 每 6h 做一次「同 info-hash 单实例」只读巡检：只报警不自动删，
                    # 真重复的交由 tools/cluster_dedupe.py --apply 人工/定时收敛。
                    if time.time() - last_dedupe > 21600:
                        try:
                            if deduper is None:
                                from musichub.storage.cluster_guard import ClusterDeduper
                                deduper = ClusterDeduper(host=self.config.publish_qb.host)
                            scan = deduper.scan()
                            from musichub.storage.cluster_guard import (
                                summarize_scan, KIND_LIVE_DUPLICATE,
                            )
                            logger.info(f"[ClusterGuard] 单实例巡检: {summarize_scan(scan)}")
                            live_groups = [g for g in scan["groups"]
                                           if g["kind"] == KIND_LIVE_DUPLICATE]
                            for g in live_groups[:5]:
                                logger.warning(
                                    "[ClusterGuard] 双活冲突 %s %s <- %s（须收敛）"
                                    % (g["hash"][:12], str(g["name"])[:40],
                                       ", ".join(h["pool"] for h in g["holders"]))
                                )
                        except Exception as dex:
                            logger.error(f"[ClusterGuard] 单实例巡检异常: {dex}")
                        last_dedupe = time.time()

                    # 每日对账一次：全量拉取实况覆盖在册快照，并记录漂移流水
                    if time.time() - last_reconcile > 86400:
                        rec = self.handoff_governor.reconcile_pool_index()
                        last_reconcile = time.time()
                        logger.info(f"[HandoffGovernor] 池索引对账完成: {rec}")
                    res = self.handoff_governor.run_handoff_round(dry_run=False, max_batch=60)
                    logger.info(
                        "[HandoffGovernor] 巡检结果: 3010任务数=%s, 够格=%s, 可交棒=%s, 成功交棒=%s, "
                        "库未就绪=%s, 同名不同版本=%s, 截断=%s"
                        % (res.get("total_3010"), res.get("eligible_count"), res.get("ready_count"),
                           res.get("handed_off_count"), res.get("skipped_no_lib"),
                           res.get("skipped_size_mismatch"), res.get("truncated"))
                    )
                except Exception as ex:
                    logger.error(f"[HandoffGovernor] 交棒巡检异常: {ex}")
                time.sleep(120)
            logger.info("[HandoffGovernor] 达标交棒独立线程已停止。")

        self._handoff_thread = threading.Thread(target=_handoff_loop, daemon=True, name="HandoffGovernorThread")
        self._handoff_thread.start()

        # 启动 8 个音乐 QB 多池下载阶梯治理独立线程 (15s/轮，纳管全矩阵 8 个音乐实例，五档控速 + 20并发)
        def _download_governor_loop():
            logger.info("[DownloadGovernor] 8 个音乐 QB 多池下载阶梯治理线程已启动 (周期 15s，20 并发，五档控速)...")
            while self._running:
                try:
                    res = self.download_governor.apply_all_pools_step()
                    # 仅在有活跃下载或未完成任务时输出汇总日志，避免日志刷屏
                    active_info = []
                    for name, stats in res.items():
                        if stats.get("active_dl", 0) > 0 or stats.get("total_incomplete", 0) > 0:
                            spd = stats.get("speed_bps", 0) / 1048576.0
                            active_info.append(f"{name}(在途:{stats.get('active_dl')}/{stats.get('total_incomplete')}, 速度:{spd:.2f}MB/s)")
                    if active_info:
                        logger.info(f"[DownloadGovernor] 多池下载巡检: {', '.join(active_info)}")
                except Exception as ex:
                    logger.error(f"[DownloadGovernor] 多池调速治理异常: {ex}")
                time.sleep(self.download_governor.check_interval)
            logger.info("[DownloadGovernor] 8 个音乐 QB 多池下载阶梯治理线程已停止。")

        self._download_governor_thread = threading.Thread(target=_download_governor_loop, daemon=True, name="DownloadGovernorThread")
        self._download_governor_thread.start()

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
