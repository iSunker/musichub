# -*- coding: utf-8 -*-
"""下载队列清道夫 (DownloadQueueJanitor)

职责：
1. 定期扫描 3016 下载队列，删除「停滞且无数据来源」的任务，释放并发下载槽；
2. 判定条件必须同时成立，杜绝误删正在下载或有源可下的任务：
   - state == 'stalledDL'（已激活但无速率，未在 downloading）
   - progress == 0（尚未拿到任何数据）
   - num_complete <= 0（tracker 未报告任何做种者）
   - age >= min_age_hours（避免误删刚加入、尚在握手的种子）
3. 删除一律 deleteFiles=false（数据保留，可随时从站点重新取种）。

背景：RSS 会持续灌入站点侧 0 做种的音乐种，这些任务以 stalledDL 占住
MaxActiveDownloads 名额，导致同时进来的有源种子只能 queuedDL 排队。
"""
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("DownloadQueueJanitor")


class DownloadQueueJanitor:
    def __init__(
        self,
        qb_client: Any,
        interval_seconds: int = 3600,
        min_age_hours: float = 2.0,
        max_delete_per_round: int = 20,
        stall_states: tuple = ("stalledDL",),
    ):
        self.qb_client = qb_client
        self.interval_seconds = max(300, int(interval_seconds))
        self.min_age_hours = float(min_age_hours)
        self.max_delete_per_round = max(1, int(max_delete_per_round))
        self.stall_states = tuple(s.lower() for s in stall_states)

    def collect_candidates(self, torrents: List[Dict], now: Optional[float] = None) -> List[Dict]:
        """筛选可清理的停滞无源任务，按加入时间升序返回（先清最老的）"""
        now = time.time() if now is None else now
        min_age = self.min_age_hours * 3600.0
        out: List[Dict] = []
        for t in torrents or []:
            state = str(t.get("state") or "").lower()
            if state not in self.stall_states:
                continue
            if float(t.get("progress") or 0.0) != 0.0:
                continue
            if int(t.get("num_complete") or 0) > 0:
                continue
            if (now - float(t.get("added_on") or now)) < min_age:
                continue
            out.append(t)
        out.sort(key=lambda x: float(x.get("added_on") or 0.0))
        return out

    def run_once(self, dry_run: bool = False) -> Dict[str, Any]:
        """执行一轮清理，返回统计与明细"""
        result: Dict[str, Any] = {
            "scanned": 0,
            "matched": 0,
            "deleted": 0,
            "dry_run": dry_run,
            "items": [],
        }
        torrents = self.qb_client.get_torrents() or []
        result["scanned"] = len(torrents)

        candidates = self.collect_candidates(torrents)
        result["matched"] = len(candidates)
        if not candidates:
            return result

        batch = candidates[: self.max_delete_per_round]
        result["truncated"] = len(candidates) - len(batch)

        if dry_run:
            for t in batch:
                result["items"].append(self._brief(t))
            return result

        hashes = [str(t.get("hash") or "").lower() for t in batch if t.get("hash")]
        if not hashes:
            return result

        ok = False
        try:
            ok = bool(self.qb_client.delete_torrent("|".join(hashes), delete_files=False))
        except Exception as e:
            logger.error(f"[QueueJanitor] 删除失败: {e}")
            return result

        if ok:
            result["deleted"] = len(hashes)
            for t in batch:
                result["items"].append(self._brief(t))
            logger.info(
                "[QueueJanitor] 已清理 %d 个无源停滞任务（deleteFiles=false），剩余候选 %d"
                % (len(hashes), result.get("truncated") or 0)
            )
        else:
            logger.warning("[QueueJanitor] qB 删除接口返回失败，本轮不计数")
        return result

    @staticmethod
    def _brief(t: Dict) -> Dict[str, Any]:
        tracker = str(t.get("tracker") or "")
        host = tracker.split("/")[2] if "//" in tracker else ""
        return {
            "hash": str(t.get("hash") or "")[:8],
            "name": str(t.get("name") or "")[:60],
            "site": host,
            "age_hours": round((time.time() - float(t.get("added_on") or time.time())) / 3600.0, 1),
        }
