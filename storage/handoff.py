# -*- coding: utf-8 -*-
"""MusicHub 3.0 滚动保种池调度器 (PoolManager) 与 就绪移交调度器 (ReadyHandoffManager)

严格遵循：
1. 【容量/数量软上限滚动池】：单池满 2000 个任务自动切换下一池
2. 【同卷单实例做种】：同 InfoHash 集群单实例
3. 【8池完全平权】：OpenCD 与各源站平权平铺，同专辑归一免跨池硬链接
4. 【闸门 A 物理存在性校验】：杜绝幽灵做种
5. 【顺序流跨卷搬运】：SSD /volume2 -> 机械盘 /volume1 当前活动池，**移动**（mv）不是复制
6. 【源种必须做种】：搬到池目录后必须在目标池建任务并转正，禁止「有文件、无任务」的孤儿
7. 【SSD 安全回收】：目标实例正常做种后才清理 SSD 临时缓存与 3016 源任务
8. 【摄取台账】：每次搬移/注入/回滚都写 ingest_log，可反查「源种在哪做种」
"""

import os
import shutil
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from musichub.core.models import CanonicalMusicItem
from musichub.core.qb_client import QBClient

logger = logging.getLogger("ReadyHandoff")



# 池容器内统一挂载点（docker-compose.pools.yml: host_dir -> /downloads）
POOL_CONTAINER_SAVE_PATH = "/downloads"

# 源种做种任务的标签，便于与 B 种（musichub,<源站>）区分与巡检
SOURCE_SEED_TAG = "source-seed"


def is_opencd_origin(origin_site: str) -> bool:
    """来源站点是否属于 OpenCD（兼容 short name 与域名写法）"""
    s = (origin_site or "").strip().lower()
    if not s:
        return False
    return "opencd" in s or "open.cd" in s


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


# ---------------------------------------------------------------------------
# 规范化 8 池严格升序对齐（2026-11-23 重构：8 池完全平权，解除 3260 硬锁）
# ---------------------------------------------------------------------------
DEFAULT_MUSIC_POOLS = [
    PoolConfig(name="music_pool_1", port=3210, mount_dir="/volume1/video/download/music/music_pool_1"),
    PoolConfig(name="music_pool_2", port=3220, mount_dir="/volume1/video/download/music/music_pool_2"),
    PoolConfig(name="music_pool_3", port=3230, mount_dir="/volume1/video/download/music/music_pool_3"),
    PoolConfig(name="music_pool_4", port=3240, mount_dir="/volume1/video/download/music/music_pool_4"),
    PoolConfig(name="music_pool_5", port=3250, mount_dir="/volume1/video/download/music/music_pool_5"),
    PoolConfig(name="music_pool_6", port=3260, mount_dir="/volume1/video/download/music/music_pool_6"),
    PoolConfig(name="music_pool_7", port=3270, mount_dir="/volume1/video/download/music/music_pool_7"),
    PoolConfig(name="music_pool_8", port=3280, mount_dir="/volume1/video/download/music/music_pool_8"),
]


class RollingPoolManager:
    """保种池调度管理器：管控 8 实例完全平权保种池 (3210 ~ 3280)"""

    def __init__(
        self,
        pools: Optional[List[PoolConfig]] = None,
        qb_opencd: Optional[QBClient] = None,
        opencd_pool_dir: str = "",
    ):
        # 默认 8 个完全平权的保种池
        self.pools = pools or [
            PoolConfig(name=p.name, port=p.port, mount_dir=p.mount_dir, max_torrents=p.max_torrents, host=p.host)
            for p in DEFAULT_MUSIC_POOLS
        ]
        self.clients: Dict[str, QBClient] = {}
        for p in self.pools:
            self.clients[p.name] = QBClient(host=p.host, port=p.port, username=p.username, password=p.password)

        self.qb_opencd = qb_opencd
        self.opencd_pool_dir = opencd_pool_dir

    def get_pool_torrent_count(self, pool: PoolConfig) -> int:
        """安全获取指定保种池的当前种子数量"""
        client = self.clients.get(pool.name)
        if not client:
            return 0
        try:
            torrents = client.get_torrents()
            return len(torrents)
        except Exception as e:
            logger.warning(f"Failed to query torrent count for pool {pool.name} ({pool.port}): {e}")
            return 0

    def get_least_loaded_pool(self) -> Tuple[PoolConfig, QBClient]:
        """按当前任务数获取负荷最低（最空）的保种池"""
        counts = [(p, self.get_pool_torrent_count(p)) for p in self.pools]
        counts.sort(key=lambda x: x[1])
        best_pool, count = counts[0]
        logger.debug(f"Least loaded pool: {best_pool.name}({best_pool.port}) with {count} torrents")
        return best_pool, self.clients[best_pool.name]

    def get_active_pool(self) -> Tuple[PoolConfig, QBClient]:
        """获取当前第一个未达到软上限 (默认 2000) 的活跃保种池"""
        for p in self.pools:
            cnt = self.get_pool_torrent_count(p)
            logger.debug(f"Checking pool {p.name}: count={cnt}/{p.max_torrents}")
            if cnt < p.max_torrents:
                return p, self.clients[p.name]

        return self.get_least_loaded_pool()

    def route_target(self, origin_site: str = "") -> Tuple[str, QBClient, str]:
        """根据来源站点判定目标保种池与 qB 客户端（全 8 池动态最空负载均衡）
        返回：(目标宿主目录路径, 目标 qB 客户端, 池标识)
        """
        pool, client = self.get_least_loaded_pool()
        return pool.mount_dir, client, pool.name


class ReadyHandoffManager:
    """就绪移交调度器：SSD 完成任务 -> 机械盘保种池 -> 目标实例真做种"""

    def __init__(
        self,
        qb_download: QBClient,
        pool_manager: Optional[RollingPoolManager] = None,
        graph_manager: Optional[Any] = None,
        verify_timeout: float = 30.0,
        archive_dir: Optional[str] = None,
    ):
        self.qb_download = qb_download
        self.pool_manager = pool_manager or RollingPoolManager()
        # RoutingGraphManager（可选，测试可注入替身；缺省则不写台账）
        self.graph_mgr = graph_manager
        self.verify_timeout = float(verify_timeout)
        # 本地 .torrent 归档目录（默认 musichub/data/torrents，测试可指向临时目录）
        self.archive_dir = archive_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "torrents"
        )

    # ------------------------------------------------------------------
    # 闸门 A
    # ------------------------------------------------------------------
    @staticmethod
    def _payload_stats(path: str) -> Tuple[int, int]:
        """统计有效载荷（跳过 qB 临时文件与群晖缩略图目录）"""
        if os.path.isfile(path):
            return os.path.getsize(path), 1
        total_bytes = 0
        file_count = 0
        for root, _, files in os.walk(path):
            if "@eaDir" in root:
                continue
            for f in files:
                if f.endswith(".!qB"):
                    continue
                total_bytes += os.path.getsize(os.path.join(root, f))
                file_count += 1
        return total_bytes, file_count

    def verify_physical_payload(self, ssd_path: str) -> Tuple[bool, str, int]:
        """【闸门 A】物理文件硬校验：严禁仅凭 qB 的 progress==1.0 盲目认定文件就绪"""
        if not os.path.exists(ssd_path):
            return False, f"SSD path does not exist on disk: {ssd_path}", 0

        if os.path.isfile(ssd_path):
            sz = os.path.getsize(ssd_path)
            if sz == 0:
                return False, f"Single file is 0 bytes: {ssd_path}", 0
            return True, "Single file verified", sz

        total_bytes, file_count = self._payload_stats(ssd_path)
        if file_count == 0 or total_bytes < 1024 * 1024:
            return False, f"Payload empty or abnormally small ({file_count} files, {total_bytes} bytes)", 0

        return True, f"Physical verification passed ({file_count} files, {total_bytes} bytes)", total_bytes

    def route_target_pool(self, origin_site: str) -> Tuple[str, Optional[QBClient]]:
        """向后兼容接口：路由目标池与 qB 客户端"""
        dst_dir, client, _ = self.pool_manager.route_target(origin_site)
        return dst_dir, client

    # ------------------------------------------------------------------
    # 搬运（mv：跨卷时 shutil.move 内部 copy+delete，落盘后 SSD 立即释放）
    # ------------------------------------------------------------------
    def move_payload_to_hdd(self, src_path: str, dst_parent_dir: str) -> Tuple[bool, str, str]:
        """把完整资源从 SSD **移动**到机械盘目标池

        改为移动而非复制的原因（2026-11-21）：
        - 复制后源目录仍在 SSD，必须再依赖 delete_files=True 才释放，且一旦注入失败
          就会出现「机械盘一份 + SSD 一份 + 3016 任务一份」的三份叠加；
        - 移动语义天然幂等：搬完源路径消失，3016 任务用 delete_files=False 下架，
          不会再触发级联物理删除。
        """
        if not os.path.exists(src_path):
            return False, f"Source path {src_path} not found", ""

        base_name = os.path.basename(src_path.rstrip("/\\"))
        final_dst_path = os.path.join(dst_parent_dir, base_name)

        try:
            os.makedirs(dst_parent_dir, exist_ok=True)
            if os.path.exists(final_dst_path):
                src_bytes, _ = self._payload_stats(src_path)
                dst_bytes, _ = self._payload_stats(final_dst_path)
                if src_bytes == dst_bytes and src_bytes > 0:
                    logger.info(f"Destination {final_dst_path} already holds same payload, skip move.")
                    return True, "Already exists (same payload)", final_dst_path
                return (
                    False,
                    f"目标同名目录已存在且大小不符（{dst_bytes} vs {src_bytes}），拒绝覆盖: {final_dst_path}",
                    final_dst_path,
                )

            shutil.move(src_path, final_dst_path)
            return True, f"Successfully moved to {final_dst_path}", final_dst_path
        except Exception as e:
            logger.error(f"Failed moving payload from {src_path} to {dst_parent_dir}: {e}")
            return False, str(e), ""

    def copy_stream_to_hdd(self, src_path: str, dst_parent_dir: str) -> Tuple[bool, str, str]:
        """历史兼容入口（2026-11-21 起语义已改为移动，见 move_payload_to_hdd）"""
        return self.move_payload_to_hdd(src_path, dst_parent_dir)

    # ------------------------------------------------------------------
    # 源种字节与目标实例注入
    # ------------------------------------------------------------------
    def _resolve_torrent_bytes(self, info_hash: str, provided: Optional[bytes] = None) -> Optional[bytes]:
        """取源种 .torrent 字节

        顺序：调用方显式传入 -> 3016 `/torrents/export` -> 本地归档 `data/torrents/*.torrent`。
        三条都拿不到就返回 None，调用方必须中止搬移（否则必产生孤儿）。
        """
        if provided:
            return provided
        if not info_hash:
            return None

        export_fn = getattr(self.qb_download, "export_torrent", None)
        if callable(export_fn):
            try:
                data = export_fn(info_hash)
                if data:
                    return data
            except Exception as e:
                logger.warning(f"[Ingest] export torrent {info_hash} from 3016 failed: {e}")

        # infohash 是 info 字典的 SHA1，**不会**以原始 20 字节出现在 .torrent 里，
        # 因此必须解析种子算 infohash 再比对（旧实现按字节包含匹配，永远命不中）。
        if os.path.isdir(self.archive_dir):
            import io

            try:
                import torf
            except ImportError:
                logger.warning("[Ingest] 缺少 torf，无法解析归档种子")
                return None
            try:
                for fname in os.listdir(self.archive_dir):
                    if not fname.endswith(".torrent"):
                        continue
                    with open(os.path.join(self.archive_dir, fname), "rb") as fh:
                        data = fh.read()
                    try:
                        torrent = torf.Torrent.read_stream(io.BytesIO(data))
                    except Exception:
                        continue
                    archived_hash = str(getattr(torrent, "infohash", "") or "").lower()
                    if archived_hash == info_hash:
                        return data
            except Exception as e:
                logger.warning(f"[Ingest] 本地归档检索失败: {e}")
        return None

    @staticmethod
    def _client_port(client: Any) -> int:
        try:
            return int(getattr(client, "port", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _add_seed_torrent(
        self,
        client: Any,
        torrent_bytes: bytes,
        pool_name: str,
        origin_site: str,
    ) -> bool:
        """在目标池建任务并**直接转正做种**

        与铁律「新增任务默认暂停」不冲突：这里注入的是本地已完整落盘的数据
        （闸门 A 通过 + skip_checking），不存在偷跑下行的可能；
        反而必须先跑起来，才能兑现「源种也要做种」。
        """
        tags = f"musichub,{SOURCE_SEED_TAG},{origin_site or 'unknown'}"
        kwargs = dict(
            save_path=POOL_CONTAINER_SAVE_PATH,
            tags=tags,
            skip_checking=True,
            paused=False,
        )
        add_fn = getattr(client, "add_torrent", None)
        if callable(add_fn):
            try:
                return bool(
                    add_fn(
                        torrent_bytes=torrent_bytes,
                        save_path=POOL_CONTAINER_SAVE_PATH,
                        is_paused=False,
                        is_skip_checking=True,
                        tags=tags,
                        auto_tmm=False,
                    )
                )
            except TypeError:
                # 兼容旧签名
                return bool(add_fn(torrent_bytes, **kwargs))
        add_file_fn = getattr(client, "add_torrent_file", None)
        if callable(add_file_fn):
            return bool(add_file_fn(torrent_bytes, **kwargs))
        logger.error(f"[Ingest] 目标实例不支持 add_torrent/add_torrent_file: {client!r}")
        return False

    def _wait_seeded(self, client: Any, info_hash: str) -> Tuple[bool, float, str]:
        """等待目标池任务回读进度 100%（skip_checking 下通常数秒内到位）"""
        if self.verify_timeout <= 0:
            return True, 1.0, "verify skipped"

        get_fn = getattr(client, "get_torrents", None)
        if not callable(get_fn):
            return True, 1.0, "client has no get_torrents"

        deadline = time.time() + self.verify_timeout
        last_progress = 0.0
        last_state = "unknown"
        while time.time() < deadline:
            try:
                rows = get_fn(hashes=info_hash)
            except TypeError:
                try:
                    rows = get_fn()
                except Exception as e:
                    logger.warning(f"[Ingest] 读取目标池任务失败: {e}")
                    rows = None
            except Exception as e:
                logger.warning(f"[Ingest] 读取目标池任务失败: {e}")
                rows = None

            for t in rows or []:
                if (t.get("hash") or "").lower() != info_hash.lower():
                    continue
                last_progress = float(t.get("progress", 0.0) or 0.0)
                last_state = t.get("state") or "unknown"
                if last_progress >= 0.999:
                    return True, last_progress, last_state
                if last_state in ("missingFiles", "error") and last_progress < 0.01:
                    return False, last_progress, last_state
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(3.0, remaining))

        return False, last_progress, last_state

    # ------------------------------------------------------------------
    # 回滚与清理
    # ------------------------------------------------------------------
    def _rollback_move(self, hdd_path: str, ssd_path: str) -> bool:
        """注入/校验失败时把数据搬回 SSD，避免占用机械盘且下一轮可重试"""
        if not hdd_path or not os.path.exists(hdd_path):
            return True
        if os.path.exists(ssd_path):
            logger.warning(f"[Rollback] SSD 目标已存在，跳过回搬: {ssd_path}")
            return False
        try:
            os.makedirs(os.path.dirname(ssd_path) or ".", exist_ok=True)
            shutil.move(hdd_path, ssd_path)
            logger.info(f"[Rollback] 已回搬 {hdd_path} -> {ssd_path}")
            return True
        except Exception as e:
            logger.error(f"[Rollback] 回搬失败 {hdd_path} -> {ssd_path}: {e}")
            return False

    def _call(self, client: Any, method: str, hashes: str) -> bool:
        fn = getattr(client, method, None)
        if not callable(fn):
            return False
        try:
            return bool(fn(hashes))
        except Exception as e:
            logger.warning(f"[Ingest] 调用 {method}({hashes}) 失败: {e}")
            return False

    def _detach_source_task(self, info_hash: str) -> bool:
        """3016 源任务下架：文件已 mv 走，绝不 delete_files=True。"""
        delete_many = getattr(self.qb_download, "delete_torrents", None)
        if callable(delete_many):
            try:
                return bool(delete_many(hashes=[info_hash], delete_files=False))
            except TypeError:
                pass
            except Exception as e:
                logger.warning(f"[Ingest] delete_torrents 失败 {info_hash}: {e}")
        return self._call_delete_single(info_hash)

    def _call_delete_single(self, info_hash: str) -> bool:
        fn = getattr(self.qb_download, "delete_torrent", None)
        if not callable(fn):
            return False
        try:
            return bool(fn(info_hash, delete_files=False))
        except TypeError:
            try:
                return bool(fn(hashes=info_hash, delete_files=False))
            except Exception as e:
                logger.warning(f"[Ingest] delete_torrent 失败 {info_hash}: {e}")
                return False
        except Exception as e:
            logger.warning(f"[Ingest] delete_torrent 失败 {info_hash}: {e}")
            return False

    def _remove_ssd_residue(self, ssd_path: str) -> None:
        """删除 SSD 残留（mv 已成功、数据确认在机械盘之后才调用）"""
        if not ssd_path or not os.path.exists(ssd_path):
            return
        try:
            if os.path.isfile(ssd_path):
                os.remove(ssd_path)
            else:
                shutil.rmtree(ssd_path)
            logger.info(f"[Ingest] SSD 残留已清理: {ssd_path}")
        except Exception as e:
            logger.warning(f"[Ingest] SSD 残留清理失败 {ssd_path}: {e}")

    def _log_ingest(self, **fields) -> None:
        if not self.graph_mgr:
            return
        log_fn = getattr(self.graph_mgr, "log_ingest", None)
        if not callable(log_fn):
            return
        try:
            log_fn(**fields)
        except Exception as e:
            logger.warning(f"[Ingest] 写 ingest_log 失败: {e}")

    # ------------------------------------------------------------------
    # 主流水线
    # ------------------------------------------------------------------
    def handoff_task(
        self,
        item: CanonicalMusicItem,
        ssd_path: str,
        torrent_bytes: Optional[bytes] = None,
    ) -> Tuple[bool, str]:
        """执行完整的就绪移交流水线

        1. 闸门 A 物理校验
        2. 路由目标池（8 池完全平权动态均衡）
        3. 取源种字节（缺失即中止，不搬文件）
        4. 停 3016 源任务 -> mv 到机械盘 -> 目标池注入并转正做种 -> 校验进度
        5. 校验通过后下架 3016 任务（delete_files=False）并清 SSD 残留
        6. 全程写 ingest_log，失败自动回滚
        """
        info_hash = (item.info_hash or "").strip().lower()
        origin_site = (item.origin_site or "unknown").strip().lower()
        source_tid = str(item.origin_torrent_id or "")
        source_name = item.title or item.album or ""

        def log(**extra):
            payload = dict(
                info_hash=info_hash,
                source_site=origin_site,
                source_tid=source_tid,
                source_name=source_name,
            )
            payload.update(extra)
            self._log_ingest(**payload)

        # 1. 闸门 A
        valid, reason, sz = self.verify_physical_payload(ssd_path)
        if not valid:
            logger.error(f"[Gate A Rejected] {info_hash}: {reason}")
            log(status="FAILED", reason=f"Gate A failed: {reason}")
            return False, f"Gate A failed: {reason}"
        _, file_count = self._payload_stats(ssd_path)

        # 2. 路由目标保种池
        target_pool_dir, target_qb, pool_name = self.pool_manager.route_target(item.origin_site)
        if not target_qb:
            log(status="FAILED", reason=f"Target QB client not available for pool {pool_name}")
            return False, f"Target QB client not available for pool {pool_name}"

        target_port = self._client_port(target_qb)

        # 3. 取源种字节（必须在 3016 任务下架前完成）
        payload = self._resolve_torrent_bytes(info_hash, torrent_bytes)
        if not payload:
            msg = f"源种 torrent 字节缺失，拒绝搬移以避免「有文件无任务」孤儿: {info_hash}"
            logger.error(f"[Ingest] {msg}")
            log(status="FAILED", target_instance=pool_name, target_port=target_port,
                target_save_path=POOL_CONTAINER_SAVE_PATH, reason=msg)
            return False, msg

        # 4a. 停源：mv 前先让 3016 停止读写
        self._call(self.qb_download, "stop_torrent", info_hash)

        # 4b. 移动至机械盘
        ok, msg, final_hdd_path = self.move_payload_to_hdd(ssd_path, target_pool_dir)
        if not ok:
            self._call(self.qb_download, "resume_torrent", info_hash)
            log(status="FAILED", target_instance=pool_name, target_port=target_port,
                host_path=final_hdd_path, reason=f"Move failed: {msg}")
            return False, f"Move failed: {msg}"

        # 4c. 注入目标池并转正做种
        add_ok = self._add_seed_torrent(target_qb, payload, pool_name, origin_site)
        if not add_ok:
            reason_msg = f"目标池 {pool_name}({target_port}) 注入源种失败"
            rolled = self._rollback_move(final_hdd_path, ssd_path)
            if rolled:
                self._call(self.qb_download, "resume_torrent", info_hash)
            log(status="ROLLED_BACK" if rolled else "FAILED",
                target_instance=pool_name, target_port=target_port,
                target_save_path=POOL_CONTAINER_SAVE_PATH,
                host_path=final_hdd_path, size_bytes=sz, file_count=file_count,
                reason=reason_msg)
            return False, reason_msg

        # 4d. 确保任务处于运行态（兼容 qB 全局 AddTorrentStopped=true）
        self._call(target_qb, "resume_torrent", info_hash)

        # 4e. 回读校验：进度必须到 100%，否则撤回并回滚
        seeded, progress, state = self._wait_seeded(target_qb, info_hash)
        if not seeded:
            reason_msg = (
                f"目标池任务未达 100%（progress={progress:.3f}, state={state}），"
                f"判定数据未就位并撤回"
            )
            logger.error(f"[Ingest] {info_hash}: {reason_msg}")
            self._call_delete_target(target_qb, info_hash)
            rolled = self._rollback_move(final_hdd_path, ssd_path)
            if rolled:
                self._call(self.qb_download, "resume_torrent", info_hash)
            log(status="ROLLED_BACK" if rolled else "FAILED",
                target_instance=pool_name, target_port=target_port,
                target_save_path=POOL_CONTAINER_SAVE_PATH, host_path=final_hdd_path,
                size_bytes=sz, file_count=file_count, progress=progress, reason=reason_msg)
            return False, reason_msg

        # 5. 源种已在目标池做种 -> 下架 3016 任务（不删文件）并清 SSD 残留
        self._detach_source_task(info_hash)
        self._remove_ssd_residue(ssd_path)

        log(status="SEEDING", target_instance=pool_name, target_port=target_port,
            target_save_path=POOL_CONTAINER_SAVE_PATH, host_path=final_hdd_path,
            size_bytes=sz, file_count=file_count, progress=progress,
            reason=f"seeding in {pool_name}({target_port}), state={state}")

        item.content_path = final_hdd_path
        return True, f"Handoff completed to {pool_name}({target_port}) ({target_pool_dir})"

    def _call_delete_target(self, client: Any, info_hash: str) -> bool:
        """撤回目标池任务（delete_files=False，数据保留在原地交由回滚处理）"""
        fn = getattr(client, "delete_torrent", None)
        if not callable(fn):
            return False
        try:
            return bool(fn(info_hash, delete_files=False))
        except TypeError:
            try:
                return bool(fn(hashes=info_hash, delete_files=False))
            except Exception as e:
                logger.warning(f"[Ingest] 撤回目标池任务失败 {info_hash}: {e}")
                return False
        except Exception as e:
            logger.warning(f"[Ingest] 撤回目标池任务失败 {info_hash}: {e}")
            return False
