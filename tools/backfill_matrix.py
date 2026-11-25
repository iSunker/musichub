# -*- coding: utf-8 -*-
"""MusicHub 3.0 历史缺口矩阵补发调度器 (11:00 - 18:00)

核心策略：
1. 数据库驱动（DB-First）：直接基于 source_tasks + publish_lineage + site_publish_graph
   计算每张专辑在 12 目标站的已覆盖情况，秒级找出「缺口未发」集合。
2. 缺口最少优先：优先补齐只差 1~2 站的专辑，加速达成 12 站全矩阵退役门禁。
3. 5 分钟节拍 + 站点交叉轮询：每轮每站最多发 1 个，24h 失败退避，3010 队列背压保护。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from musichub.core.models import CanonicalMusicItem
from musichub.metadata.audio_inspector import AudioInspector
from musichub.publisher.stock_pump import StockMatrixPump
from musichub.routing.graph import RoutingGraphManager, torrent_infohash
from musichub.adapters.nexusphp_base import is_duplicate_name_result, extract_existing_tid

logger = logging.getLogger("BackfillMatrix")

FORBIDDEN_POOLS = ("music5",)
ALL_TARGET_SITES = list(StockMatrixPump.TARGET_SITES_PRIORITY)


class BackfillMatrixPump:
    """11:00 - 18:00 历史缺口矩阵补发引擎（复用 StockMatrixPump 发布链路）。"""

    def __init__(
        self,
        db_path: str = "/app/musichub/data/musichub.db",
        stock_pump: Optional[StockMatrixPump] = None,
        max_3010_torrents: int = 200,
        backoff_hours: int = 24,
    ):
        self.db_path = db_path
        self.stock_pump = stock_pump or StockMatrixPump(db_path=db_path)
        self.graph_mgr = self.stock_pump.graph_mgr
        self.max_3010_torrents = max_3010_torrents
        self.backoff_hours = backoff_hours
        self.inspector = self.stock_pump.inspector

    def _find_local_dir(self, source_name: str, lineage_paths: Dict[str, str], pool_dirs: Dict[str, str]) -> str:
        """定位专辑在 8 大音乐池中的实体目录（严禁触碰 music5）。"""
        lp = lineage_paths.get(source_name, "")
        if lp and not any(f in lp for f in FORBIDDEN_POOLS) and os.path.isdir(lp):
            return lp
        if source_name in pool_dirs:
            return pool_dirs[source_name]
        return ""

    def _index_pool_dirs(self) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for pool in self.stock_pump.pools:
            if any(f in pool for f in FORBIDDEN_POOLS) or not os.path.isdir(pool):
                continue
            try:
                for entry in os.listdir(pool):
                    if entry.startswith((".", "@")) or "incomplete" in entry:
                        continue
                    full = os.path.join(pool, entry)
                    if os.path.isdir(full) and entry not in mapping:
                        mapping[entry] = full
            except OSError:
                continue
        return mapping

    def plan_backfill_queue(self) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """计算缺口最少优先的补发计划，返回 (ordered_albums, missing_by_site)。"""
        if not os.path.exists(self.db_path):
            return [], {}

        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1. 源种清单
        st_rows = cur.execute("""
            SELECT info_hash, source_site, source_tid, source_name, artist, album, year,
                   format, medium, standard, cover_url, description, log_text, tracklist_json,
                   genre_json, tags_json, transfer_rule, updated_at
            FROM source_tasks
            WHERE meta_status = 'OK'
              AND (transfer_rule IS NULL OR transfer_rule != 'DENY')
              AND source_name != ''
        """).fetchall()

        # 2. 已发布/已存在记录（按 source_hash 与 source_name 聚合）
        covered_by_hash: Dict[str, Set[str]] = {}
        lineage_paths: Dict[str, str] = {}
        for r in cur.execute("""
            SELECT LOWER(source_hash) AS sh, source_name, target_site, local_path, status
            FROM publish_lineage
            WHERE status IN ('PUBLISHED', 'EXISTING')
        """):
            sh = r["sh"] or ""
            ts = (r["target_site"] or "").strip().lower()
            sn = r["source_name"] or ""
            lp = r["local_path"] or ""
            if sh and ts:
                covered_by_hash.setdefault(sh, set()).add(ts)
            if sn and lp and sn not in lineage_paths:
                lineage_paths[sn] = lp

        # 补充 site_publish_graph 中按 origin_site + origin_torrent_id 的覆盖
        covered_by_origin: Dict[Tuple[str, str], Set[str]] = {}
        for r in cur.execute("""
            SELECT origin_site, origin_torrent_id, target_site
            FROM site_publish_graph
            WHERE publish_status IN ('PUBLISHED', 'EXISTING')
              AND origin_site != '' AND origin_torrent_id != ''
        """):
            key = (str(r["origin_site"]).lower(), str(r["origin_torrent_id"]).strip())
            covered_by_origin.setdefault(key, set()).add(str(r["target_site"]).lower())

        # 3. 24h 内失败退避集合
        backoff: Set[Tuple[str, str]] = set()
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - self.backoff_hours * 3600))
        try:
            for r in cur.execute("""
                SELECT album_name, origin_path, target_site
                FROM publish_attempt_log
                WHERE result IN ('FAILED', 'REJECTED')
                  AND created_at >= ?
            """, (cutoff,)):
                ts = (r["target_site"] or "").strip().lower()
                if r["album_name"]:
                    backoff.add((r["album_name"], ts))
                if r["origin_path"]:
                    backoff.add((r["origin_path"], ts))
        except sqlite3.OperationalError:
            pass

        conn.close()

        pool_dirs = self._index_pool_dirs()
        albums: List[Dict[str, Any]] = []
        missing_by_site: Dict[str, int] = {s: 0 for s in ALL_TARGET_SITES}

        for r in st_rows:
            sh = (r["info_hash"] or "").strip().lower()
            src_site = (r["source_site"] or "").strip().lower()
            src_tid = str(r["source_tid"] or "").strip()
            src_name = r["source_name"] or ""

            local_dir = self._find_local_dir(src_name, lineage_paths, pool_dirs)
            if not local_dir:
                continue

            covered = set(covered_by_hash.get(sh, set()))
            if src_site and src_tid:
                covered.update(covered_by_origin.get((src_site, src_tid), set()))

            # 源站自身不作为补发目标
            covered.add(src_site)

            missing_sites = []
            for s in ALL_TARGET_SITES:
                if s in covered:
                    continue
                if not self.graph_mgr.is_target_allowed(src_site, s):
                    continue
                if (src_name, s) in backoff or (local_dir, s) in backoff:
                    continue
                missing_sites.append(s)
                missing_by_site[s] = missing_by_site.get(s, 0) + 1

            if not missing_sites:
                continue

            albums.append({
                "row": dict(r),
                "local_dir": local_dir,
                "missing_sites": missing_sites,
                "missing_count": len(missing_sites),
                "updated_at": float(r["updated_at"] or 0.0),
            })

        # 缺口最少优先，其次按入库时间升序
        albums.sort(key=lambda x: (x["missing_count"], x["updated_at"]))
        return albums, missing_by_site

    def _build_item(self, row: Dict[str, Any], local_dir: str) -> Optional[CanonicalMusicItem]:
        tracks = self.inspector.scan_album_directory(local_dir)
        if not tracks:
            return None

        tags: List[str] = []
        try:
            raw_tags = row.get("tags_json") or ""
            if raw_tags:
                parsed = json.loads(raw_tags)
                if isinstance(parsed, list):
                    tags = [t["name"] if isinstance(t, dict) else str(t) for t in parsed if t]
        except Exception:
            pass

        genre: List[str] = []
        try:
            raw_genre = row.get("genre_json") or ""
            if raw_genre:
                if raw_genre.startswith("["):
                    genre = json.loads(raw_genre)
                else:
                    genre = [g.strip() for g in raw_genre.replace("/", ",").split(",") if g.strip()]
        except Exception:
            pass

        return CanonicalMusicItem(
            title=row.get("source_name") or os.path.basename(local_dir),
            origin_title=row.get("source_name") or "",
            artist=row.get("artist") or (tracks[0].artist if tracks else "Various Artists"),
            album=row.get("album") or os.path.basename(local_dir),
            year=str(row.get("year") or "2020"),
            format=row.get("format") or "FLAC",
            medium=row.get("medium") or "CD",
            standard=row.get("standard") or "Lossless",
            edition="",
            local_path=local_dir,
            cover_url=row.get("cover_url") or "",
            description=row.get("description") or "",
            log_text=row.get("log_text") or "",
            tracks=tracks,
            origin_site=row.get("source_site") or "opencd",
            origin_torrent_id=str(row.get("source_tid") or ""),
            origin_info_hash=row.get("info_hash") or "",
            has_log=bool(row.get("log_text")),
            tags=tags,
            genre=genre,
        )

    def run_backfill_round(self, max_per_round: int = 12, dry_run: bool = False) -> Dict[str, Any]:
        """执行一轮 11:00-18:00 缺口补发（每站最多 1 个，缺口最少优先）。"""
        # 3010 队列背压检查
        try:
            cnt_3010 = len(self.stock_pump.publish_qb.get_torrents() or [])
        except Exception:
            cnt_3010 = 0

        if cnt_3010 >= self.max_3010_torrents and not dry_run:
            logger.warning(f"[BackfillMatrix] 3010 在种任务数={cnt_3010} >= {self.max_3010_torrents}，暂停本轮等待交棒消化")
            return {"skipped_backpressure": True, "cnt_3010": cnt_3010, "published": 0}

        albums, missing_by_site = self.plan_backfill_queue()
        if not albums:
            return {"skipped_backpressure": False, "cnt_3010": cnt_3010, "eligible_albums": 0, "published": 0, "missing_by_site": missing_by_site}

        published_count = 0
        used_albums: Set[str] = set()
        planned_picks: List[Dict[str, Any]] = []

        for site in ALL_TARGET_SITES:
            if published_count >= max_per_round:
                break
            adapter = self.stock_pump.adapters_map.get(site)
            if not adapter:
                continue

            # 挑选该站点缺口最少且本轮尚未分配的专辑
            chosen = None
            for alb in albums:
                sh = alb["row"]["info_hash"]
                if sh in used_albums:
                    continue
                if site in alb["missing_sites"]:
                    chosen = alb
                    break
            if not chosen:
                continue

            row = chosen["row"]
            local_dir = chosen["local_dir"]
            album_name = row["source_name"]
            used_albums.add(row["info_hash"])

            if dry_run:
                planned_picks.append({
                    "site": site,
                    "album": album_name,
                    "missing_count": chosen["missing_count"],
                    "missing_sites": chosen["missing_sites"],
                })
                published_count += 1
                continue

            item = self._build_item(row, local_dir)
            if not item:
                continue

            pass_chk, chk_msg = adapter.pre_publish_check(item)
            if not pass_chk:
                logger.debug(f"[BackfillMatrix] {album_name} 未通过 {site} 预检: {chk_msg}")
                continue

            ok, msg, t_path = self.stock_pump.engine.create_torrent_safe(item, site, getattr(adapter, "tracker_url", ""))
            if not ok or not t_path:
                self.graph_mgr.record_publish_attempt(
                    album_name=album_name, target_site=site, result="FAILED",
                    reason=f"制种失败: {msg}", origin_path=local_dir,
                )
                continue

            try:
                pub_ok, tid, pub_torrent_bytes = adapter.publish(item, t_path)
                if pub_ok and tid:
                    with open(t_path, "rb") as tf:
                        local_bytes = tf.read()
                    readd_bytes = pub_torrent_bytes if pub_torrent_bytes else local_bytes
                    self.graph_mgr.record_publish(
                        item, site,
                        target_torrent_id=tid,
                        b_hash=torrent_infohash(readd_bytes),
                        target_url=f"{getattr(adapter, 'base_url', '')}/details.php?id={tid}",
                        qb_instance="publish", qb_port=3010, save_path="/downloads",
                        status="PUBLISHED", publish_status="PUBLISHED", note=album_name,
                    )
                    self.graph_mgr.record_publish_attempt(
                        album_name=album_name, target_site=site, result="PUBLISHED",
                        target_torrent_id=str(tid), origin_path=local_dir,
                    )
                    self.stock_pump.delayed_queue.add_task(
                        site_name=site, torrent_id=str(tid), item=item,
                        torrent_bytes=readd_bytes, delay_seconds=450,
                    )
                    published_count += 1
                    logger.info(f"[BackfillMatrix] 补发成功 '{album_name}' -> {site}:{tid} (原缺口={chosen['missing_count']})")
                    time.sleep(5)
                else:
                    if is_duplicate_name_result(tid):
                        ex_tid = extract_existing_tid(tid)
                        self.graph_mgr.record_publish(
                            item, site, target_torrent_id=ex_tid,
                            publish_status="EXISTING", status="EXISTING", note=str(tid),
                        )
                        self.graph_mgr.record_publish_attempt(
                            album_name=album_name, target_site=site, result="SKIPPED_EXISTING",
                            reason=str(tid), target_torrent_id=ex_tid, origin_path=local_dir,
                        )
                    else:
                        self.graph_mgr.record_publish_attempt(
                            album_name=album_name, target_site=site, result="REJECTED",
                            reason=str(tid), origin_path=local_dir,
                        )
            except Exception as ex:
                logger.exception(f"[BackfillMatrix] 补发 '{album_name}' -> {site} 异常: {ex}")
                self.graph_mgr.record_publish_attempt(
                    album_name=album_name, target_site=site, result="FAILED",
                    reason=f"异常: {ex}", origin_path=local_dir,
                )
            finally:
                if t_path and os.path.exists(t_path):
                    try:
                        os.remove(t_path)
                    except OSError:
                        pass

        return {
            "skipped_backpressure": cnt_3010 >= self.max_3010_torrents,
            "cnt_3010": cnt_3010,
            "eligible_albums": len(albums),
            "published": published_count,
            "missing_by_site": missing_by_site,
            "planned_picks": planned_picks,
        }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="11:00-18:00 历史缺口矩阵补发（缺口最少优先）")
    parser.add_argument("--apply", action="store_true", help="真正执行补发（默认 dry-run）")
    parser.add_argument("--max", type=int, default=12, help="单轮最大补发数量（默认 12 站各 1 个）")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    res = BackfillMatrixPump().run_backfill_round(max_per_round=args.max, dry_run=not args.apply)
    if args.format == "json":
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(
            f"[BackfillMatrix] dry_run={not args.apply} cnt_3010={res.get('cnt_3010')} "
            f"eligible_albums={res.get('eligible_albums')} published={res.get('published')}"
        )
        print("  missing_by_site:", res.get("missing_by_site"))
        for p in res.get("planned_picks") or []:
            print(f"  -> [{p['site']}] (缺{p['missing_count']}站) {p['album']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
