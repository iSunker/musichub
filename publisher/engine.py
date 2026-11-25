# -*- coding: utf-8 -*-
"""MusicHub 3.0 发种调度引擎 (PublisherEngine)

落实核心防御闸门与工业级安全防线：
1. 闸门 A：物理文件真实性、存在性与大小硬校验（防幽灵做种）
2. 全局打种互斥锁（保护群晖机械盘 IOPS，防寻道雪崩）
3. 严格制种后分块物理自校验 (t.verify)：杜绝早产、脏块、文件篡改或截断的种子流向外网
4. 闸门 B：回加 publish (3010) 保活探针与主动 resume，确保出种稳定
5. 发种区 (_safe_rmtree) 严格护栏：带根目录逃逸防护，安全回收发种区 SSD 缓存，保护主库物理安全
"""

import io
import os
import shutil
import tempfile
import threading
import time
import logging
import hashlib
import torf
from typing import Dict, List, Optional, Tuple
from musichub.core.models import CanonicalMusicItem
from musichub.adapters.base import BaseSiteAdapter
from musichub.routing.graph import RoutingGraphManager
from musichub.core.qb_client import QBClient
from musichub.storage.pool_pairing import PoolPairingManager

logger = logging.getLogger("PublisherEngine")


class PublisherEngine:
    # 全局打种互斥锁：同一时刻全系统仅允许 1 个任务打种和做 Hash 校验
    _TORF_IO_LOCK = threading.Lock()

    def __init__(
        self,
        publish_qb_client: QBClient,
        graph_manager: RoutingGraphManager,
        pool_pairing: Optional[PoolPairingManager] = None,
    ):
        self.publish_qb = publish_qb_client
        self.graph_mgr = graph_manager
        # 发布期音乐池配对：把同一份 B 种以 paused 态同步注入承载实体数据的池，
        # 让交棒器可以按同 infohash 命中（详见 storage/pool_pairing.py）。
        if pool_pairing is not None:
            self.pool_pairing = pool_pairing
        elif os.getenv("MUSICHUB_POOL_PAIRING", "1").lower() not in ("0", "false", "off"):
            self.pool_pairing = PoolPairingManager(
                publish_host=getattr(publish_qb_client, "host", "192.168.0.7")
            )
        else:
            self.pool_pairing = None

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

                    t.write(output_torrent, overwrite=True)
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

    @staticmethod
    def _extract_infohash(torrent_bytes: bytes) -> Optional[str]:
        """从种子二进制解析 info_hash(v1)，用于精确定位刚注入的 B 种（避免同名多站串号）"""
        try:
            import torf
            t = torf.Torrent.read_stream(io.BytesIO(torrent_bytes))
            ih = getattr(t, "infohash", None) or getattr(t, "infohash_v1", None)
            if ih:
                return str(ih).lower()
        except Exception as e:
            logger.debug(f"infohash extraction failed: {e}")
        return None

    @staticmethod
    def _dir_stats(path: str) -> Tuple[int, int]:
        """返回 (总字节数, 文件数)；断链返回 (0, 0)"""
        if os.path.islink(path) and not os.path.exists(path):
            return 0, 0
        total, count = 0, 0
        try:
            for root, _dirs, files in os.walk(path, followlinks=True):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                        count += 1
                    except OSError:
                        pass
        except OSError:
            return 0, 0
        return total, count

    def _ensure_data_ready(self, local_path: str, target_dir: str) -> bool:
        """确保 target_dir 是 local_path 的完整可用映射：
        - 有效软链接直接复用；断链先清理再重建；
        - 已存在的真实目录若与源大小/文件数不一致（残缺/错版），先清理；
        - 软链接失败时回退为「校验过的完整拷贝」，拷贝不完整则清理半成品。
        返回 False 表示无法准备完整数据，调用方不得注入种子（避免僵尸 stalledDL）。
        """
        # 1) 已存在软链接
        if os.path.islink(target_dir):
            if os.path.exists(target_dir):
                return True
            logger.warning(f"[Readd] 断链，移除后重建: {target_dir}")
            try:
                os.unlink(target_dir)
            except OSError as e:
                logger.error(f"[Readd] 移除断链失败: {e}")
                return False

        # 2) 已存在真实目录/文件 -> 与源比对，不一致先清理
        elif os.path.exists(target_dir):
            src_size, src_cnt = self._dir_stats(local_path)
            dst_size, dst_cnt = self._dir_stats(target_dir)
            if src_size > 0 and src_size == dst_size and src_cnt == dst_cnt:
                return True
            logger.warning(
                f"[Readd] 目标残留不一致(实际 {dst_size}B/{dst_cnt} 文件 vs 源 {src_size}B/{src_cnt} 文件)，清理重建: {target_dir}"
            )
            if not self.safe_clean_publish_area(target_dir):
                return False

        # 3) 重建：软链接优先（同卷零拷贝）
        try:
            os.symlink(local_path, target_dir)
            logger.info(f"[Readd] 已建立软链接: {target_dir} -> {local_path}")
            return True
        except OSError as e_link:
            logger.warning(f"[Readd] 软链接失败({e_link})，回退为完整拷贝并校验...")

        # 4) 拷贝 + 完整性校验
        try:
            if os.path.isdir(local_path):
                shutil.copytree(local_path, target_dir, dirs_exist_ok=True)
            else:
                shutil.copy2(local_path, target_dir)
        except Exception as e_copy:
            logger.error(f"[Readd] 拷贝失败: {e_copy}")
            self.safe_clean_publish_area(target_dir)
            return False

        src_size, src_cnt = self._dir_stats(local_path)
        dst_size, dst_cnt = self._dir_stats(target_dir)
        if src_size > 0 and src_size == dst_size and src_cnt == dst_cnt:
            logger.info(f"[Readd] 拷贝完整性校验通过 ({dst_size}B/{dst_cnt} 文件)")
            return True
        logger.error(
            f"[Readd] 拷贝完整性校验失败(实际 {dst_size}B/{dst_cnt} vs 源 {src_size}B/{src_cnt})，清理半成品"
        )
        self.safe_clean_publish_area(target_dir)
        return False

    def _rollback(self, target_hash: str):
        """撤回注入失败的种子（不删文件），杜绝 stalledDL 僵尸残留"""
        if not target_hash:
            return
        try:
            ok = self.publish_qb.delete_torrent(target_hash, delete_files=False)
            logger.warning(f"[Readd] 已撤回缺陷种子 {target_hash[:8]} (ok={ok}, deleteFiles=false)")
        except Exception as e:
            logger.error(f"[Readd] 撤回失败 {target_hash[:8]}: {e}")

    def _payload_matches_torrent(self, torrent_bytes: bytes, data_path: str) -> Tuple[bool, str]:
        """分块级校验本地实体与 B 种是否完全一致

        存在的必要性：`_ensure_data_ready` 只比对「源 vs 发种区副本」的尺寸/数量，
        当源本身残缺或换了版本时仍会放行；一旦放行进 3010，qB 唤醒后就会按
        tracker 补齐缺块（实测单种泄漏 304MB 下行）。此处在注入前用 torf 重算
        全部分片，数据与种子不完全一致时直接判缺陷，绝不触达 qB。
        """
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as tf:
                tf.write(torrent_bytes)
                tmp_path = tf.name
            torrent = torf.Torrent.read(tmp_path)
            if torrent.verify(data_path, threads=2):
                return True, ""
            return False, f"piece verification failed for {os.path.basename(data_path)}"
        except Exception as e:
            return False, f"payload verify error: {e}"
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _pair_to_library(self, torrent_bytes: bytes, item: CanonicalMusicItem, expected_hash: str = ""):
        """3010 保活成功后，把同一份 B 种以 paused 态配对进音乐池（失败不影响 3010 做种）"""
        if not self.pool_pairing:
            return
        try:
            ok, msg = self.pool_pairing.pair_published_torrent(
                torrent_bytes=torrent_bytes,
                local_path=item.local_path,
                expected_hash=expected_hash or self._extract_infohash(torrent_bytes) or "",
            )
        except Exception as e:
            logger.warning(f"[PoolPairing] {item.album} 配对异常（3010 做种不受影响）: {e}")
            return
        if ok:
            logger.info(f"[PoolPairing] {item.album}: {msg}")
        else:
            logger.warning(f"[PoolPairing] {item.album} 未配对（3010 继续做种）: {msg}")

    def readd_publish_with_keepalive_probe(
        self,
        torrent_bytes: bytes,
        item: CanonicalMusicItem,
        timeout_seconds: int = 300,
        probe_interval: int = 15,
    ) -> Tuple[bool, str]:
        """【防御闸门 B】回加 publish (3010) 并启动做种保活探针

        本次加固（根治 stalledDL 僵尸）：
        1. 注入前确保 /downloads/<name> 数据完整可用（软链接优先，拷贝必校验），数据不就绪不注入；
        2. 用 B 种真实 info_hash 精确定位刚注入任务，杜绝同专辑多站同名串号；
        3. 取消 skip_checking 盲信，交由 qB 真实校验分块；progress<1.0 且进入 DL/缺失态立即判失败；
        4. 任一失败/异常路径都 deleteFiles=false 撤回，不留僵尸。
        """
        target_hash = ""
        saw_torrent = False
        try:
            # 0. 物理前置：确保发种区数据完整
            publish_root = getattr(self.publish_qb, "publish_dir", None) or os.getenv(
                "STORAGE_PUBLISH_DIR", "/volume2/docker_ssd/qbit-all/data/publish/Download"
            )
            if not item.local_path or not os.path.exists(item.local_path):
                return False, f"Source payload missing on disk: {item.local_path}"
            if os.path.exists(publish_root):
                target_album_dir = os.path.join(
                    publish_root, os.path.basename(item.local_path.rstrip("/\\"))
                )
                if not self._ensure_data_ready(item.local_path, target_album_dir):
                    return False, "Failed to prepare complete payload (symlink/copy verify failed)"

            # 1.4 B 种分块级前置校验：本地实体与 .torrent 完全一致才允许注入，
            #     否则 qB 一旦唤醒就会下行补齐缺块（实测泄漏 304MB 流量）
            payload_ok, payload_msg = self._payload_matches_torrent(torrent_bytes, item.local_path)
            if not payload_ok:
                return False, f"Defective payload before injection ({payload_msg}); skipped without download"

            # 2. 注入种子（skip_checking=False：交由 qB 真实校验，杜绝假 100%）
            added = self.publish_qb.add_torrent_file(
                torrent_bytes=torrent_bytes,
                save_path="/downloads",  # 统一遵守 /downloads 一级目录契约
                skip_checking=False,
                paused=False,
                tags=f"musichub,{item.origin_site}",
                auto_tmm=False,  # 显式锁定 savepath，防止类目/默认路径改写
            )
            target_hash = self._extract_infohash(torrent_bytes) or ""

            # 409 Conflict：可能已存在，二次核对
            if not added:
                time.sleep(1)
                existing = self.publish_qb.get_torrents() or []
                if target_hash:
                    found_dup = any(t.get("hash", "").lower() == target_hash for t in existing)
                else:
                    found_dup = any(item.album in t.get("name", "") or t.get("name") == item.album for t in existing)
                if not found_dup:
                    return False, "Failed to inject published torrent into publish (3010)"
                logger.info(f"Torrent '{item.album}' already existed in 3010 (409 Conflict handled).")

            logger.info(f"Injected torrent to publish (3010) for {item.album}, starting keepalive probe...")

            # 2. 轮询保活探针（progress 为 qB 校验后的真实值）
            start_time = time.time()
            while time.time() - start_time < timeout_seconds:
                torrents = self.publish_qb.get_torrents() or []
                matched = None
                for t in torrents:
                    if target_hash and t.get("hash", "").lower() == target_hash:
                        matched = t
                        break
                if matched is None and not target_hash:
                    for t in torrents:
                        if t.get("name") == item.album or item.album in t.get("name", ""):
                            matched = t
                            target_hash = t.get("hash", "")
                            break

                if matched:
                    saw_torrent = True
                    state = str(matched.get("state", "")).lower()
                    progress = float(matched.get("progress", 0.0))

                    # 暂停/停止 -> 主动唤醒
                    if "paused" in state or "stopped" in state:
                        logger.info(f"Torrent '{matched.get('name')}' is paused in 3010, explicitly resuming...")
                        self.publish_qb.resume_torrent(target_hash)
                        time.sleep(probe_interval)
                        continue

                    # 校验中 -> 继续等待
                    if "checking" in state:
                        time.sleep(probe_interval)
                        continue

                    # 成功：做种态且数据完整
                    if progress >= 0.999 and any(
                        s in state for s in ("upload", "stalledup", "forcedup", "queuedup")
                    ):
                        logger.info(f"Keepalive probe succeeded: '{matched.get('name')}' state={state}, progress={progress:.1%}")
                        # 3010 已真实校验 100%：把同一份 B 种配对进承载实体数据的音乐池，
                        # 保证池内出现同 infohash，交棒器才可能命中 hash 判据。
                        self._pair_to_library(torrent_bytes, item, target_hash)
                        return True, f"Seeding verified in publish (3010), state={state}"

                    # 明确失败：校验完成但数据不完整/缺文件/错误/仍在下载 -> 立即撤回
                    if progress < 0.999 and state in ("stalleddl", "missingfiles", "error", "downloading"):
                        logger.warning(
                            f"Keepalive probe detected defective payload: '{matched.get('name')}' state={state}, progress={progress:.1%}; rolling back"
                        )
                        self._rollback(target_hash)
                        return False, f"Defective payload (state={state}, progress={progress:.1%}); rolled back"

                time.sleep(probe_interval)

            # 超时：只有确实观测到任务时才撤回；一次都没读到通常是 3010 API 抖动，
            # 盲目 rollback 会误删刚回加成功的做种任务。
            if not saw_torrent:
                return False, (
                    f"Keepalive probe unavailable ({timeout_seconds}s): 未观测到任务 "
                    f"{target_hash[:8] if target_hash else ''}，跳过回滚避免误删"
                )
            self._rollback(target_hash)
            return False, f"Keepalive timeout ({timeout_seconds}s); rolled back"

        except Exception as e:
            logger.error(f"Keepalive probe exception: {str(e)}")
            if saw_torrent:
                self._rollback(target_hash)
            else:
                logger.warning(f"Keepalive exception before observing task, skip rollback: {target_hash[:8]}")
            return False, f"Keepalive exception: {str(e)}"
