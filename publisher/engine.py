# -*- coding: utf-8 -*-
"""MusicHub 3.0 发种调度引擎 (PublisherEngine)

落实核心防御闸门与工业级安全防线：
1. 闸门 A：物理文件真实性、存在性与大小硬校验（防幽灵做种）
2. 全局打种互斥锁（保护群晖机械盘 IOPS，防寻道雪崩）
3. 严格制种后分块物理自校验 (t.verify)：杜绝早产、脏块、文件篡改或截断的种子流向外网
4. 闸门 B：回加 publish (3010) 保活探针与主动 resume，确保出种稳定
5. 发种区 (_safe_rmtree) 严格护栏：带根目录逃逸防护，安全回收发种区 SSD 缓存，保护主库物理安全
"""

import os
import shutil
import threading
import time
import logging
import hashlib
from typing import Dict, List, Optional, Tuple
from musichub.core.models import CanonicalMusicItem
from musichub.adapters.base import BaseSiteAdapter
from musichub.routing.graph import RoutingGraphManager
from musichub.core.qb_client import QBClient

logger = logging.getLogger("PublisherEngine")


class PublisherEngine:
    # 全局打种互斥锁：同一时刻全系统仅允许 1 个任务打种和做 Hash 校验
    _TORF_IO_LOCK = threading.Lock()

    def __init__(self, publish_qb_client: QBClient, graph_manager: RoutingGraphManager):
        self.publish_qb = publish_qb_client
        self.graph_mgr = graph_manager

    def verify_physical_data_readiness(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        """【防御闸门 A】物理文件硬校验：杜绝盲信 qB 的 progress==1.0 引发幽灵做种"""
        if not item.local_path or not os.path.exists(item.local_path):
            return False, f"Physical path does not exist on disk: {item.local_path}"

        if not item.tracks:
            return False, "Canonical item has no track information"

        total_bytes = 0
        for track in item.tracks:
            if not track.file_path or not os.path.exists(track.file_path):
                return False, f"Track file missing on disk: {track.file_path}"
            sz = os.path.getsize(track.file_path)
            if sz == 0:
                return False, f"Track file is 0 bytes (corrupted/empty): {track.file_path}"
            total_bytes += sz

        if total_bytes < 1024 * 1024:  # 小于 1MB
            return False, f"Total payload size {total_bytes} bytes is abnormally small"

        return True, f"Physical verification passed ({len(item.tracks)} tracks, {total_bytes} bytes)"

    def create_torrent_safe(self, item: CanonicalMusicItem, target_site: str, tracker_url: str = "") -> Tuple[bool, str, Optional[str]]:
        """安全打种：受全局 IO 锁保护，自动过滤系统垃圾文件，打种后立即执行分块物理自反验 (t.verify)"""
        with self._TORF_IO_LOCK:
            output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "torrents")
            os.makedirs(output_dir, exist_ok=True)
            output_torrent = os.path.join(
                output_dir,
                f"{item.calculate_content_fingerprint()[:12]}_{target_site}.torrent"
            )
            try:
                # 必须先通过闸门 A
                ok, reason = self.verify_physical_data_readiness(item)
                if not ok:
                    return False, f"Gate A failed: {reason}", None

                # 调用 torf 打种
                try:
                    import torf
                    t = torf.Torrent(
                        path=item.local_path,
                        trackers=[tracker_url] if tracker_url else [],
                        source=target_site,
                        private=True,
                        exclude_regexs=[r".*@eaDir.*", r".*\.DS_Store", r".*Thumbs\.db", r".*\.!qB.*", r".*\.url$", r".*\.nfo$"]
                    )
                    t.generate()

                    # 【重点 1：制种后立即分块物理自校验】杜绝早产、篡改或截断种子
                    try:
                        if not t.verify(item.local_path):
                            return False, f"Piece-by-piece physical verification failed for {item.local_path}", None
                    except Exception as e_v:
                        return False, f"Piece verification error (corrupted payload): {e_v}", None

                    t.write(output_torrent)
                    logger.info(f"Torrent successfully generated & verified with Torf: {output_torrent}")
                    return True, "Torrent created safely with IO lock & verified", output_torrent
                except ImportError:
                    err_msg = "Dependency 'torf' is not installed; refusing to create mock torrent for safety"
                    logger.error(err_msg)
                    return False, err_msg, None
            except Exception as e:
                return False, f"Torf creation failed: {str(e)}", None

    def safe_clean_publish_area(self, target_path: str) -> bool:
        """【重点 2：发种区目录安全清理护栏 (_safe_rmtree)】
        只在 publish_dir 之下清理具体子目录或软链接，具备多重安全防线：
        1. target 必须严格在 publish_dir 之下，严禁等于发种区根目录
        2. 若为软链接，直接 unlink 软链接本身，绝不跟随软链接递归删除
        3. 若为真实目录，递归删除发种区副本，绝对不污染主库 /volume1 物理数据
        """
        publish_root = getattr(self.publish_qb, "publish_dir", "/volume2/docker_ssd/qbit-all/data/publish/Download")
        root = os.path.abspath(publish_root)
        target = os.path.abspath(target_path)

        if not os.path.exists(target):
            return True

        if target == root:
            logger.error(f"[PublishGC] 拒删：目标是发种区根目录本身 ({target})")
            return False

        if not target.startswith(root + os.sep):
            logger.error(f"[PublishGC] 拒删：目标不在发种区之下 ({target} ∉ {root})")
            return False

        try:
            if os.path.islink(target):
                os.unlink(target)
                logger.info(f"[PublishGC] 已安全移除发种区软链接: {target}")
            elif os.path.isdir(target):
                shutil.rmtree(target)
                logger.info(f"[PublishGC] 已安全清理发种区目录: {target}")
            elif os.path.isfile(target):
                os.remove(target)
                logger.info(f"[PublishGC] 已安全移除发种区文件: {target}")
            return True
        except Exception as e:
            logger.warning(f"[PublishGC] 清理发种区路径失败 ({target}): {e}")
            return False

    def readd_publish_with_keepalive_probe(
        self,
        torrent_bytes: bytes,
        item: CanonicalMusicItem,
        timeout_seconds: int = 300,
        probe_interval: int = 15,
    ) -> Tuple[bool, str]:
        """【防御闸门 B】回加 publish (3010) 并启动做种保活探针
        处理 3010 默认暂停 (AddTorrentStopped=true) 或 Tracker 延迟，确保出种稳定。
        """
        try:
            # 0. 物理前置：将成品数据流式复制到 3010 宿主发种区
            publish_root = getattr(self.publish_qb, "publish_dir", "/volume2/docker_ssd/qbit-all/data/publish/Download")
            if os.path.exists(publish_root) and item.local_path and os.path.exists(item.local_path):
                target_album_dir = os.path.join(publish_root, os.path.basename(item.local_path.rstrip("/\\")))
                if not os.path.exists(target_album_dir):
                    logger.info(f"Handoff copying payload to 3010 publish area: {target_album_dir}...")
                    if os.path.isdir(item.local_path):
                        shutil.copytree(item.local_path, target_album_dir, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item.local_path, target_album_dir)

            # 1. 注入种子到 publish (3010)
            added = self.publish_qb.add_torrent_file(
                torrent_bytes=torrent_bytes,
                save_path="/downloads",  # 统一遵守 /downloads 一级目录契约
                skip_checking=True,
                paused=False,
                tags=f"musichub,{item.origin_site}",
            )
            if not added:
                return False, "Failed to inject published torrent into publish (3010)"

            logger.info(f"Injected torrent to publish (3010) for {item.album}, starting keepalive probe...")

            # 2. 轮询保活探针
            start_time = time.time()
            target_hash = ""

            while time.time() - start_time < timeout_seconds:
                torrents = self.publish_qb.get_torrents() or []
                matched = None
                for t in torrents:
                    if t.get("name") == item.album or item.album in t.get("name", ""):
                        matched = t
                        break

                if matched:
                    target_hash = matched.get("hash")
                    state = matched.get("state", "").lower()
                    progress = matched.get("progress", 0.0)

                    # 若处于暂停或停止状态，主动唤醒
                    if "paused" in state or "stopped" in state:
                        logger.info(f"Torrent '{matched.get('name')}' is paused in 3010, explicitly resuming...")
                        self.publish_qb.resume_torrent(target_hash)

                    # 只要进入上传做种状态 (uploading / stalledUP / forcedUP)
                    if any(s in state for s in ("upload", "stalledup", "forcedup")):
                        logger.info(f"Keepalive probe succeeded: '{matched.get('name')}' state={state}, progress={progress:.1%}")
                        return True, f"Seeding verified in publish (3010), state={state}"

                time.sleep(probe_interval)

            if target_hash:
                return False, f"Keepalive timeout ({timeout_seconds}s): Torrent remains in non-seeding state"
            return False, "Keepalive probe failed: Torrent was not found in 3010 list"

        except Exception as e:
            logger.error(f"Keepalive probe exception: {str(e)}")
            return False, f"Keepalive exception: {str(e)}"
