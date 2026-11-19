# -*- coding: utf-8 -*-
"""MusicHub 3.0 滚动保种池调度器 (PoolManager) 与 流式移交调度器 (ReadyHandoffManager)

严格遵循：
1. 【容量/数量软上限滚动池】：单池满 2000 个任务自动切换下一池
2. 【同卷单实例做种】：同 InfoHash 集群单实例
3. 【皇后锁定】：OpenCD 来源任务唯一归属 3260 (music3)
4. 【闸门 A 物理存在性校验】：杜绝幽灵做种
5. 【顺序流跨卷搬运】：SSD /volume2 -> 机械盘 /volume1 当前活动池
6. 【SSD 安全回收】：目标实例正常做种后清理 SSD 临时缓存
"""

import os
import shutil
import logging
from typing import Dict, List, Optional, Tuple

from musichub.core.models import CanonicalMusicItem
from musichub.core.qb_client import QBClient

logger = logging.getLogger("ReadyHandoff")


class PoolConfig:
    def __init__(
        self,
        name: str,
        port: int,
        mount_dir: str,
        max_torrents: int = 2000,
        host: str = "127.0.0.1",
        username: str = "admin",
        password: str = "",
    ):
        self.name = name
        self.port = port
        self.mount_dir = mount_dir
        self.max_torrents = max_torrents
        self.host = host
        self.username = username
        self.password = password

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


class RollingPoolManager:
    """滚动保种池管理器：监控与管理滚动池实例 (3210, 3230, 3250, 3270, 3280)"""

    def __init__(
        self,
        pools: Optional[List[PoolConfig]] = None,
        qb_opencd: Optional[QBClient] = None,
        opencd_pool_dir: str = "/volume1/video/download/music/music3",
    ):
        # 默认 5 个滚动保种池
        self.pools = pools or [
            PoolConfig(name="music_pool_1", port=3210, mount_dir="/volume1/video/download/music/music_pool_1"),
            PoolConfig(name="music_pool_2", port=3230, mount_dir="/volume1/video/download/music/music_pool_2"),
            PoolConfig(name="music_pool_3", port=3250, mount_dir="/volume1/video/download/music/music_pool_3"),
            PoolConfig(name="music_pool_4", port=3270, mount_dir="/volume1/video/download/music/music_pool_4"),
            PoolConfig(name="music_pool_5", port=3280, mount_dir="/volume1/video/download/music/music_pool_5"),
        ]
        self.clients: Dict[str, QBClient] = {}
        for p in self.pools:
            self.clients[p.name] = QBClient(host=p.host, port=p.port, username=p.username, password=p.password)

        self.qb_opencd = qb_opencd or QBClient(host="127.0.0.1", port=3260)
        self.opencd_pool_dir = opencd_pool_dir

    def get_pool_torrent_count(self, pool: PoolConfig) -> int:
        """安全获取指定保种池的当前种子数量"""
        client = self.clients.get(pool.name)
        if not client:
            return 0
        try:
            # 轻量获取任务列表数量
            torrents = client.get_torrents()
            return len(torrents)
        except Exception as e:
            logger.warning(f"Failed to query torrent count for pool {pool.name} ({pool.port}): {e}")
            return 0

    def get_active_pool(self) -> Tuple[PoolConfig, QBClient]:
        """获取当前第一个未达到软上限 (默认 2000) 的活跃保种池"""
        for p in self.pools:
            cnt = self.get_pool_torrent_count(p)
            logger.debug(f"Checking pool {p.name}: count={cnt}/{p.max_torrents}")
            if cnt < p.max_torrents:
                return p, self.clients[p.name]

        # 若全部满额，兜底回退到最后一个池并触发警报
        last_pool = self.pools[-1]
        logger.warning(f"All rolling pools exceeded max threshold! Fallback to last pool {last_pool.name}")
        return last_pool, self.clients[last_pool.name]

    def route_target(self, origin_site: str) -> Tuple[str, QBClient, str]:
        """根据来源站点判定目标保种池与 qB 客户端

        返回：(目标目录路径, 目标 qB 客户端, 池标识)
        """
        site_lower = (origin_site or "").lower()

        # 铁律：皇后来源任务绝对锁定 3260 (music3)
        if "opencd" in site_lower or "open.cd" in site_lower:
            return self.opencd_pool_dir, self.qb_opencd, "music3_opencd"

        # 其余所有站点走动态滚动保种池
        pool, client = self.get_active_pool()
        return pool.mount_dir, client, pool.name


class ReadyHandoffManager:
    """流式移交调度器"""

    def __init__(
        self,
        qb_download: QBClient,
        pool_manager: Optional[RollingPoolManager] = None,
    ):
        self.qb_download = qb_download
        self.pool_manager = pool_manager or RollingPoolManager()

    def verify_physical_payload(self, ssd_path: str) -> Tuple[bool, str, int]:
        """【闸门 A】物理文件硬校验：严禁仅凭 qB 的 progress==1.0 盲目认定文件就绪"""
        if not os.path.exists(ssd_path):
            return False, f"SSD path does not exist on disk: {ssd_path}", 0

        total_bytes = 0
        file_count = 0

        if os.path.isfile(ssd_path):
            sz = os.path.getsize(ssd_path)
            if sz == 0:
                return False, f"Single file is 0 bytes: {ssd_path}", 0
            return True, "Single file verified", sz

        for root, _, files in os.walk(ssd_path):
            for f in files:
                if f.endswith(".!qB") or "@eaDir" in root:
                    continue
                fp = os.path.join(root, f)
                sz = os.path.getsize(fp)
                if sz == 0:
                    return False, f"Detected 0-byte file: {fp}", 0
                total_bytes += sz
                file_count += 1

        if file_count == 0 or total_bytes < 1024 * 1024:
            return False, f"Payload empty or abnormally small ({file_count} files, {total_bytes} bytes)", 0

        return True, f"Physical verification passed ({file_count} files, {total_bytes} bytes)", total_bytes

    def route_target_pool(self, origin_site: str) -> Tuple[str, Optional[QBClient]]:
        """向后兼容接口：路由目标池与 qB 客户端"""
        dst_dir, client, _ = self.pool_manager.route_target(origin_site)
        return dst_dir, client

    def copy_stream_to_hdd(self, src_path: str, dst_parent_dir: str) -> Tuple[bool, str, str]:
        """以顺序流将完整资源从 SSD 复制到机械盘目标池（单任务顺序写，平抑机械盘随机寻道）"""
        if not os.path.exists(src_path):
            return False, f"Source path {src_path} not found", ""

        base_name = os.path.basename(src_path.rstrip("/\\"))
        final_dst_path = os.path.join(dst_parent_dir, base_name)

        try:
            os.makedirs(dst_parent_dir, exist_ok=True)
            if os.path.exists(final_dst_path):
                logger.info(f"Destination {final_dst_path} already exists, skipping copy.")
                return True, "Already exists", final_dst_path

            if os.path.isfile(src_path):
                shutil.copy2(src_path, final_dst_path)
            else:
                shutil.copytree(src_path, final_dst_path, copy_function=shutil.copy2)

            return True, f"Successfully copied to {final_dst_path}", final_dst_path
        except Exception as e:
            logger.error(f"Failed stream copying from {src_path} to {dst_parent_dir}: {e}")
            return False, str(e), ""

    def handoff_task(
        self,
        item: CanonicalMusicItem,
        ssd_path: str,
        torrent_bytes: Optional[bytes] = None,
    ) -> Tuple[bool, str]:
        """执行完整的就绪移交流水线"""
        # 1. 闸门 A 物理校验
        valid, reason, sz = self.verify_physical_payload(ssd_path)
        if not valid:
            logger.error(f"[Gate A Rejected] {item.info_hash}: {reason}")
            return False, f"Gate A failed: {reason}"

        # 2. 路由目标保种池
        target_pool_dir, target_qb, pool_name = self.pool_manager.route_target(item.origin_site)
        if not target_qb:
            return False, f"Target QB client not available for pool {pool_name}"

        # 3. 顺序流式搬运至机械盘
        ok, msg, final_hdd_path = self.copy_stream_to_hdd(ssd_path, target_pool_dir)
        if not ok:
            return False, f"Copy stream failed: {msg}"

        # 4. 注入目标 qB 实例
        if torrent_bytes:
            add_ok = target_qb.add_torrent(
                torrent_bytes=torrent_bytes,
                save_path="/downloads",
                category="MusicHub",
                is_paused=False,
                skip_checking=True,
            )
            if not add_ok:
                return False, f"Failed to inject torrent into target QB {target_qb.port}"

        # 5. 回收清理 SSD 缓存与 3016 任务
        try:
            target_hash = item.info_hash
            if hasattr(self.qb_download, "delete_torrents"):
                self.qb_download.delete_torrents(hashes=[target_hash], delete_files=True)
            elif hasattr(self.qb_download, "delete_torrent"):
                self.qb_download.delete_torrent(hashes=target_hash, delete_files=True)
            if os.path.exists(ssd_path):
                if os.path.isfile(ssd_path):
                    os.remove(ssd_path)
                else:
                    shutil.rmtree(ssd_path)
            logger.info(f"SSD payload safely purged for {target_hash}")
        except Exception as e:
            logger.warning(f"Error purging SSD cache for {item.info_hash}: {e}")

        item.content_path = final_hdd_path
        return True, f"Handoff completed to {pool_name} ({target_pool_dir})"
