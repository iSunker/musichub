# -*- coding: utf-8 -*-
"""MusicHub 3.0 存量矩阵平滑批量发种运行器 (StockBatchRunner)

按照用户指令执行：
1. 目标站点：muxuege, tangpt, hdkylin, xingwan, pterclub, musopia, crabpt, lemonhd, hdtime, ubits, soulvoice
2. 配额控制：各站目标 20 个 (QUOTA_PER_SITE = 20)
3. 严格护栏：
   - 源种准入：必须 transfer_rule == 'ALLOW'，绝不绕过 DENY 与 NEED_CONFIRM
   - 站点规则：必须通过 adapter.pre_publish_check(item)（如麒麟纯拉丁标题、乌托邦有效音轨）
   - 站内查重：必须通过 adapter.check_exists(item) 与 site_publish_graph 查重
   - 物理就绪：必须通过 verify_physical_data_readiness(item)
4. 频率与节奏：
   - 多站轮转交织 (Round-Robin Interleaving)，每站单轮最多 1 个
   - 成功发布后强制休眠 60 秒，保护站点 IOPS 与防反爬风控
   - 延迟回加：成功后通过 DelayedReaddQueue 进入 450s 冷却，防偷跑回加 3010 做种
"""

import os
import sys
import time
import logging
from typing import Dict, List, Optional, Any

from musichub.core.models import CanonicalMusicItem
from musichub.metadata.audio_inspector import AudioInspector
from musichub.routing.graph import RoutingGraphManager, torrent_infohash, digit_tid
from musichub.publisher.gateway import DeduplicationGateway, evaluate_source_policy
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("StockBatchRunner")


class StockBatchRunner:
    # 待补齐与未发站点列表，按缺额和优先级排序
    TARGET_SITES = [
        "muxuege",
        "tangpt",
        "hdkylin",
        "xingwan",
        "pterclub",
        "lemonhd",
        "hdtime",
        "uhddolby",
        "ubits",
        "soulvoice",
        "nanyangpt",
        "musopia",
        "crabpt",
    ]

    def __init__(
        self,
        db_path: str = "/app/musichub/data/musichub.db",
        pools: Optional[List[str]] = None,
        quota_per_site: int = 20,
        gap_seconds: int = 60,
        target_sites: Optional[List[str]] = None,
    ):
        self.db_path = db_path
        self.pools = pools or [
            "/volume1/video/download/music/music3",
            "/volume1/video/download/music/music1",
            "/volume1/video/download/music/music2",
            "/volume1/video/download/music/music_pool_1",
        ]
        if target_sites:
            self.TARGET_SITES = [s.strip().lower() for s in target_sites if s.strip()]
        self.quota_per_site = quota_per_site
        self.gap_seconds = gap_seconds

        self.graph_mgr = RoutingGraphManager(db_path=self.db_path)
        self.publish_qb = QBClient("192.168.0.7", 3010)
        self.engine = PublisherEngine(self.publish_qb, self.graph_mgr)
        self.delayed_queue = DelayedReaddQueue(self.db_path, self.engine)

        self.adapters_map = {
            "nanyangpt": NanyangPTAdapter(),
            "soulvoice": SoulVoiceAdapter(),
            "ubits": UBitsAdapter(),
            "lemonhd": LemonHDAdapter(),
            "hdtime": HDTimeAdapter(),
            "muxuege": MuXueGeAdapter(),
            "tangpt": TangPTAdapter(),
            "hdkylin": HDKylinAdapter(),
            "xingwan": XingwanAdapter(),
            "pterclub": PTerClubAdapter(),
            "musopia": MusopiaAdapter(),
            "crabpt": CrabPTAdapter(),
            "uhddolby": UHDDolbyAdapter(),
        }
        self.inspector = AudioInspector()
        self.success_counts: Dict[str, int] = {site: 0 for site in self.TARGET_SITES}
        self.exhausted_sites = set()

    def find_one_candidate_for_site(self, target_site: str) -> Optional[Dict[str, Any]]:
        """为特定站点寻找 1 个完全合规且未发布的最佳存量候选"""
        target_site = target_site.lower()
        adapter = self.adapters_map.get(target_site)
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

                # 穿透源头元数据与转载规则
                from musichub.publisher.origin_resolver import resolve_stock_origin_detail
                o_meta = resolve_stock_origin_detail(
                    folder_name=e,
                    album=album,
                    artist=artist,
                    full_path=full_p,
                    conn=getattr(self.graph_mgr, "_conn", None),
                    db_path=self.db_path,
                )

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

                # 闸门 1: 源种准入校验 (transfer_rule 必须为 ALLOW，严禁绕过)
                policy_ok, policy_reason = evaluate_source_policy(self.graph_mgr, item, target_site, adapter)
                if not policy_ok:
                    continue

                # 闸门 2: 防环与持久拓扑查重
                if self.graph_mgr.is_loop_or_already_published(item, target_site):
                    continue

                # 闸门 3: 目标站点专属护栏 (如麒麟纯拉丁标题、乌托邦有效音轨等)
                chk_ok, chk_msg = adapter.pre_publish_check(item)
                if not chk_ok:
                    continue

                # 闸门 4: 物理数据完备性反验
                ready, reason = self.engine.verify_physical_data_readiness(item)
                if not ready:
                    continue

                # 闸门 5: 站点在线搜索查重
                try:
                    exists, exists_why, matched_tid = adapter.check_exists_detail(item)
                except Exception as ex:
                    logger.warning(f"[{target_site}] check_exists 发生异常，跳过该项: {ex}")
                    continue

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

                return {
                    "item": item,
                    "adapter": adapter,
                    "target_site": target_site,
                    "album_name": album,
                    "local_path": full_p,
                }

        return None

    def run_batch(self, max_rounds: int = 25, dry_run: bool = False):
        """执行交织轮转批量发种"""
        logger.info(f"=== 开始存量矩阵批量发种 ===")
        logger.info(f"目标站点: {self.TARGET_SITES}")
        logger.info(f"单站目标配额: {self.quota_per_site} 个 | 站点间平滑间隔: {self.gap_seconds} 秒 | Dry-Run: {dry_run}")

        for round_idx in range(1, max_rounds + 1):
            active_sites = [s for s in self.TARGET_SITES if self.success_counts[s] < self.quota_per_site and s not in self.exhausted_sites]
            if not active_sites:
                logger.info("所有目标站点均已达成配额或候选耗尽，任务圆满完成！")
                break

            logger.info(f"\n--- 第 {round_idx} 轮交织轮转开始 (当前在途站点: {len(active_sites)}) ---")
            logger.info(f"当前进度表: {self.success_counts}")

            round_published = 0
            for site in active_sites:
                if self.success_counts[site] >= self.quota_per_site:
                    continue

                logger.info(f"[{site}] 寻找第 {self.success_counts[site] + 1}/{self.quota_per_site} 个合格候选...")
                cand = self.find_one_candidate_for_site(site)
                if not cand:
                    logger.warning(f"[{site}] 未能找到更多合格候选，标记为候选耗尽")
                    self.exhausted_sites.add(site)
                    continue

                item = cand["item"]
                adapter = cand["adapter"]
                album_name = cand["album_name"]

                if dry_run:
                    logger.info(f"[Dry-Run] 命中合格候选: '{album_name}' -> {site}")
                    self.success_counts[site] += 1
                    round_published += 1
                    time.sleep(1)
                    continue

                # 真实制种与自反验
                logger.info(f"[{site}] 正在制种并校验: '{album_name}'...")
                ok, msg, t_path = self.engine.create_torrent_safe(item, site, getattr(adapter, "tracker_url", ""))
                if not ok or not t_path:
                    logger.error(f"[{site}] 制种失败: {msg}")
                    self.graph_mgr.record_publish_attempt(
                        album_name=album_name,
                        target_site=site,
                        result="FAILED",
                        reason=f"制种失败: {msg}",
                        origin_path=cand["local_path"],
                    )
                    continue

                try:
                    logger.info(f"[{site}] 正在向站点提交发布: '{album_name}'...")
                    pub_ok, tid, pub_torrent_bytes = adapter.publish(item, t_path)

                    if pub_ok and tid:
                        logger.info(f"★ [{site}] 发布成功！tid={tid} | '{album_name}'")
                        self.success_counts[site] += 1
                        round_published += 1

                        with open(t_path, "rb") as tf:
                            local_bytes = tf.read()
                        readd_bytes = pub_torrent_bytes if pub_torrent_bytes else local_bytes

                        # 记录血缘与图谱
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

                        # 进入 450s 延迟回加队列（防偷跑）
                        self.delayed_queue.add_task(
                            site_name=site,
                            torrent_id=str(tid),
                            item=item,
                            torrent_bytes=readd_bytes,
                            delay_seconds=450
                        )

                        # 平滑冷却间隔
                        logger.info(f"等待 {self.gap_seconds} 秒后推进下一站点...")
                        time.sleep(self.gap_seconds)
                    else:
                        if is_duplicate_name_result(tid):
                            ex_tid = extract_existing_tid(tid)
                            logger.info(f"[{site}] 站点已有同种/同名条目 (TID: {ex_tid or 'unknown'}): {tid}")
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
                            time.sleep(5)
                        else:
                            logger.warning(f"[{site}] 发布被拒: {tid}")
                            self.graph_mgr.record_publish_attempt(
                                album_name=album_name,
                                target_site=site,
                                result="REJECTED",
                                reason=str(tid),
                                origin_path=cand["local_path"],
                            )
                            time.sleep(5)
                except Exception as ex:
                    logger.exception(f"[{site}] 发布异常: {ex}")
                    self.graph_mgr.record_publish_attempt(
                        album_name=album_name,
                        target_site=site,
                        result="FAILED",
                        reason=f"异常: {ex}",
                        origin_path=cand["local_path"],
                    )
                    time.sleep(5)
                finally:
                    if t_path and os.path.exists(t_path):
                        try:
                            os.remove(t_path)
                        except Exception:
                            pass

        logger.info(f"=== 存量矩阵批量发种执行完毕 ===")
        logger.info(f"最终进度统计: {self.success_counts}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MusicHub 3.0 存量平滑批量发种")
    parser.add_argument("--quota", type=int, default=20, help="每站目标配额 (默认 20)")
    parser.add_argument("--gap", type=int, default=45, help="发种间隔秒数 (默认 45)")
    parser.add_argument("--rounds", type=int, default=25, help="最大轮次 (默认 25)")
    parser.add_argument("--sites", type=str, default="", help="指定目标站点，逗号分隔，如 lemonhd,soulvoice,ubits (默认全部)")
    parser.add_argument("--dry-run", action="store_true", help="干跑测试")
    args = parser.parse_args()

    chosen_sites = [s.strip() for s in args.sites.split(",") if s.strip()] if args.sites else None
    runner = StockBatchRunner(quota_per_site=args.quota, gap_seconds=args.gap, target_sites=chosen_sites)
    runner.run_batch(max_rounds=args.rounds, dry_run=args.dry_run)
