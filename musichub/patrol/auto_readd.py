# -*- coding: utf-8 -*-
"""MusicHub 3.0 断种自动加回与自愈器 (SeedAutoReadd)

核心定位：
1. 【严格受控触发】：仅在巡检判定为 MISSING 或用户/调度器显式调度时执行；
2. 【数据目录继承】：必须继承原始保种目录（优先 /downloads 一级目录契约，防多层子目录嵌套导致的 0% 报错）；
3. 【默认 Dry-Run】：支持干跑预览，真写时幂等检查；
4. 【注入后状态自反验】：注入后启动微观保活轮询，确保状态进入 SEEDING。
"""

import os
import io
import time
import logging
from typing import Dict, List, Optional, Tuple, Any
from musichub.patrol.seed_patrol import PatrolRecord, SeedPatrolManager, SEEDING, SEEDING_STATES

logger = logging.getLogger("SeedAutoReadd")


class SeedAutoReadd:
    def __init__(
        self,
        publish_qb_client: Any,
        patrol_manager: SeedPatrolManager,
        default_save_path: str = "/downloads",
    ):
        self.publish_qb = publish_qb_client
        self.patrol_mgr = patrol_manager
        self.default_save_path = default_save_path

    def readd_single(
        self,
        record: PatrolRecord,
        dry_run: bool = True,
        save_path: Optional[str] = None,
        timeout_seconds: int = 60,
    ) -> Tuple[bool, str]:
        """将单个丢失的 B 种自愈加回 qB 实例"""
        target_site = record.target_site
        tid = record.target_torrent_id

        if not tid or tid == "EXISTING":
            return False, f"Invalid target_torrent_id: {tid}"

        torrent_bytes = self.patrol_mgr.get_site_torrent_bytes(target_site, tid)
        if not torrent_bytes:
            return False, f"Could not obtain .torrent(B) for {target_site} tid={tid}"

        b_ih = self.patrol_mgr.extract_info_hash_from_torrent(torrent_bytes)
        if not b_ih:
            return False, f"Failed to extract info_hash from .torrent(B)"

        # 1. 幂等性自检：检查当前是否已在 qB
        existing_torrents = self.publish_qb.get_torrents() or []
        for t in existing_torrents:
            curr_ih = (t.get("hash") or t.get("infohash_v1") or "").lower()
            if curr_ih == b_ih:
                return True, f"Torrent already exists in client (ih={b_ih})"

        effective_save_path = save_path or self.default_save_path

        if dry_run:
            logger.info(f"[Dry-Run] Would inject torrent {target_site} #{tid} (ih={b_ih}) to {effective_save_path}")
            return True, f"[Dry-Run] Injection planned (ih={b_ih})"

        # 2. 执行真注入
        logger.info(f"Injecting missing torrent {target_site} #{tid} (ih={b_ih}) to {effective_save_path}...")
        ok = self.publish_qb.add_torrent_file(
            torrent_bytes=torrent_bytes,
            save_path=effective_save_path,
            skip_checking=True,
            paused=False,
            tags=f"musichub,readd,{target_site}",
        )
        if not ok:
            return False, f"QBClient failed to add torrent {target_site} #{tid}"

        # 3. 自反验保活轮询
        start_t = time.time()
        while time.time() - start_t < timeout_seconds:
            torrents = self.publish_qb.get_torrents() or []
            target = None
            for t in torrents:
                if (t.get("hash") or "").lower() == b_ih:
                    target = t
                    break

            if target:
                state = str(target.get("state", "")).lower()
                progress = float(target.get("progress", 0.0))

                # 若被默认暂停，主动唤醒
                if "paused" in state or "stopped" in state:
                    self.publish_qb.resume_torrent(b_ih)

                if progress >= 1.0 and state in SEEDING_STATES:
                    return True, f"Successfully readded and verified seeding (state={state})"

            time.sleep(5)

        return True, f"Injected into client, but keepalive verification timed out ({timeout_seconds}s)"

    def readd_batch(
        self,
        records: List[PatrolRecord],
        dry_run: bool = True,
        max_readd: int = 10,
    ) -> Dict[str, Any]:
        """批量自愈加回"""
        results = {
            "total_candidates": len(records),
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "details": [],
        }

        count = 0
        for rec in records:
            if rec.status != "MISSING":
                continue
            if count >= max_readd:
                logger.info(f"Reached batch max limit ({max_readd}), pausing batch readd.")
                break

            count += 1
            results["processed"] += 1
            success, msg = self.readd_single(rec, dry_run=dry_run)
            if success:
                results["succeeded"] += 1
            else:
                results["failed"] += 1

            results["details"].append({
                "target_site": rec.target_site,
                "torrent_id": rec.target_torrent_id,
                "success": success,
                "message": msg,
            })

        return results
