# -*- coding: utf-8 -*-
r"""MusicHub 3.0 发种真伪判定、纯数字 TID 抢救与存量数据清洗工具

定位与职责：
1. 结构平滑升级：确保 site_publish_graph 表包含 publish_status 列与对应复合索引；
2. 脏文本 TID 抢救与去脏：救回历史存量中的 'EXISTING' 脏文本占位符，正则解析 details.php?id=(\d+)，
   未找到的置空，杜绝下游 int(tid) 崩溃；
3. 四级高精度证据链转正：仅且仅对命中 publish_lineage(status='PUBLISHED')、
   publish_attempt_log(result='PUBLISHED') 或 data.db(tasks.*_published_id) 确凿凭据的记录
   标记为 publish_status = 'PUBLISHED'，其余全量划归 'EXISTING'；
4. 站点 TID 列重新级联回填：调用 RoutingGraphManager.backfill_site_tids()；
5. 可选重导 Vertex 3016 防环清单。

遵循 AGENTS.md 规范：
- 默认 --dry-run 只读预览；
- 显式 --apply 执行落库；
- 输出清晰的清洗前后各站点对账对比表。
"""

import os
import sys
import re
import sqlite3
import argparse
import logging
from typing import Dict, Set, Tuple, List, Optional, Any

# 加入项目根路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from musichub.routing.graph import RoutingGraphManager, SITE_TID_COLUMNS, SITE_ALIASES, digit_tid

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ReconcilePublishTruth")


def normalize_site(site: str) -> str:
    s = (site or "").strip().lower()
    return SITE_ALIASES.get(s, s)


def rescue_existing_tid(
    conn: sqlite3.Connection,
    graph_id: int,
    target_site: str,
    origin_torrent_id: str,
    content_fingerprint: str
) -> Optional[str]:
    """尝试从 lineage 或 attempt_log 中抢救出真实的纯数字 TID"""
    tsite = normalize_site(target_site)
    
    # 1. 尝试从 publish_lineage 查找
    cur = conn.execute(
        """
        SELECT target_tid, note 
        FROM publish_lineage 
        WHERE LOWER(target_site) = LOWER(?) 
          AND (source_tid = ? OR source_hash = ?)
        ORDER BY id DESC LIMIT 1
        """,
        (tsite, origin_torrent_id, content_fingerprint)
    )
    row = cur.fetchone()
    if row:
        target_tid = str(row[0] or "").strip()
        note = str(row[1] or "").strip()
        if target_tid.isdigit():
            return target_tid
        m = re.search(r"details\.php\?id=(\d+)", note) or re.search(r"\[EXISTING_TID:(\d+)\]", note)
        if m:
            return m.group(1)

    # 2. 尝试从 publish_attempt_log 查找
    cur = conn.execute(
        """
        SELECT target_torrent_id, reason 
        FROM publish_attempt_log 
        WHERE LOWER(target_site) = LOWER(?) 
          AND (reason LIKE '%details.php?id=%' OR target_torrent_id GLOB '[0-9]*')
        ORDER BY id DESC LIMIT 10
        """,
        (tsite,)
    )
    for t_tid, reason in cur.fetchall():
        t_tid_str = str(t_tid or "").strip()
        reason_str = str(reason or "").strip()
        if origin_torrent_id and origin_torrent_id in reason_str:
            if t_tid_str.isdigit():
                return t_tid_str
            m = re.search(r"details\.php\?id=(\d+)", reason_str) or re.search(r"\[EXISTING_TID:(\d+)\]", reason_str)
            if m:
                return m.group(1)

    return None


def collect_evidence_published_tids(
    conn_musichub: sqlite3.Connection,
    conn_data: Optional[sqlite3.Connection]
) -> Set[Tuple[str, str]]:
    """收集所有确凿的已发布 (site, tid) 证据链"""
    evidence: Set[Tuple[str, str]] = set()

    # 1. publish_lineage 证据
    cur = conn_musichub.execute(
        "SELECT LOWER(target_site), target_tid FROM publish_lineage WHERE status = 'PUBLISHED'"
    )
    for site, tid in cur.fetchall():
        s = normalize_site(site)
        t = digit_tid(tid)
        if s and t:
            evidence.add((s, t))
    logger.info(f"[证据采集] 来自 publish_lineage(status='PUBLISHED') 的证据数: {len(evidence)}")

    # 2. publish_attempt_log 证据
    cur = conn_musichub.execute(
        "SELECT LOWER(target_site), target_torrent_id FROM publish_attempt_log WHERE result = 'PUBLISHED'"
    )
    attempt_cnt = 0
    for site, tid in cur.fetchall():
        s = normalize_site(site)
        t = digit_tid(tid)
        if s and t:
            evidence.add((s, t))
            attempt_cnt += 1
    logger.info(f"[证据采集] 来自 publish_attempt_log(result='PUBLISHED') 的证据数: {attempt_cnt}")

    # 3. data.db 历史 tasks 证据
    if conn_data:
        tasks_cnt = 0
        cur = conn_data.execute("SELECT reason FROM tasks WHERE reason LIKE '%published_id%'")
        for (reason,) in cur.fetchall():
            r = reason or ""
            # 严格排除 dup_existing / dedup_hit
            if "dup_existing" in r or "dedup_hit" in r or "同名冲突" in r:
                continue
            for m in re.finditer(r"(?:([a-zA-Z0-9]+)_)?published_id:(\d+)", r):
                prefix = (m.group(1) or "").lower()
                tid = m.group(2)
                site = "musopia" if not prefix else prefix
                norm_s = normalize_site(site)
                if norm_s and tid.isdigit():
                    evidence.add((norm_s, tid))
                    tasks_cnt += 1
        logger.info(f"[证据采集] 来自 data.db tasks(*_published_id) 的证据数: {tasks_cnt}")

    logger.info(f"[证据采集] 汇总去重后的有效真发 (site, tid) 凭据总数: {len(evidence)}")
    return evidence


def reconcile(
    db_path: str,
    legacy_db_path: Optional[str] = None,
    dry_run: bool = True,
    blocklist_path: Optional[str] = None
) -> Dict[str, Any]:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn_musichub = sqlite3.connect(db_path)
    conn_data = sqlite3.connect(legacy_db_path) if legacy_db_path and os.path.exists(legacy_db_path) else None

    # 确保 publish_status 列与索引存在
    cols = {r[1] for r in conn_musichub.execute("PRAGMA table_info(site_publish_graph)")}
    if "publish_status" not in cols:
        if not dry_run:
            conn_musichub.execute(
                "ALTER TABLE site_publish_graph ADD COLUMN publish_status TEXT NOT NULL DEFAULT 'EXISTING'"
            )
            conn_musichub.commit()
            logger.info("已新增 publish_status 列 (DEFAULT 'EXISTING')")
        else:
            logger.info("[Dry-run] 检测到 publish_status 列缺失，执行时将自动新增")

    if not dry_run:
        conn_musichub.execute("""
            CREATE INDEX IF NOT EXISTS idx_graph_site_status
            ON site_publish_graph(target_site, publish_status);
        """)
        conn_musichub.commit()

    # 统计清洗前现状
    cur = conn_musichub.execute("SELECT LOWER(target_site), count(*) FROM site_publish_graph GROUP BY LOWER(target_site)")
    pre_counts = dict(cur.fetchall())
    total_pre = sum(pre_counts.values())

    # 抢救与去脏 target_torrent_id = 'EXISTING'
    cur = conn_musichub.execute(
        """
        SELECT id, target_site, origin_torrent_id, content_fingerprint 
        FROM site_publish_graph 
        WHERE target_torrent_id = 'EXISTING' OR target_torrent_id GLOB '*[^0-9]*'
        """
    )
    dirty_rows = cur.fetchall()
    rescued_count = 0
    cleared_count = 0
    tid_updates: List[Tuple[str, int]] = []

    for gid, tsite, otid, fp in dirty_rows:
        rescued_tid = rescue_existing_tid(conn_musichub, gid, tsite, otid, fp)
        if rescued_tid:
            tid_updates.append((rescued_tid, gid))
            rescued_count += 1
        else:
            tid_updates.append(("", gid))
            cleared_count += 1

    logger.info(f"[TID抢救] 共处理脏值占位符 {len(dirty_rows)} 行: 成功救回 {rescued_count} 个数字TID, 置空 {cleared_count} 个")

    if not dry_run and tid_updates:
        conn_musichub.executemany(
            "UPDATE site_publish_graph SET target_torrent_id = ? WHERE id = ?",
            tid_updates
        )
        conn_musichub.commit()

    # 收集真发证据链并判定 publish_status
    evidence = collect_evidence_published_tids(conn_musichub, conn_data)

    cur = conn_musichub.execute(
        "SELECT id, LOWER(target_site), target_torrent_id FROM site_publish_graph"
    )
    all_graph_rows = cur.fetchall()

    status_updates: List[Tuple[str, int]] = []
    post_published: Dict[str, int] = {}
    post_existing: Dict[str, int] = {}

    for gid, raw_site, t_tid in all_graph_rows:
        # 如果当前行在 tid_updates 中，以更新后的 tid 为准
        eff_tid = digit_tid(t_tid)
        norm_s = normalize_site(raw_site)

        if (norm_s, eff_tid) in evidence and eff_tid:
            new_status = "PUBLISHED"
            post_published[norm_s] = post_published.get(norm_s, 0) + 1
        else:
            new_status = "EXISTING"
            post_existing[norm_s] = post_existing.get(norm_s, 0) + 1

        status_updates.append((new_status, gid))

    if not dry_run:
        conn_musichub.executemany(
            "UPDATE site_publish_graph SET publish_status = ? WHERE id = ?",
            status_updates
        )
        conn_musichub.commit()
        logger.info(f"[状态更新] 已更新 {len(status_updates)} 行记录的状态")

        # 级联更新 SITE_TID_COLUMNS
        graph_mgr = RoutingGraphManager(db_path=db_path)
        backfill_stats = graph_mgr.backfill_site_tids()
        logger.info(f"[TID级联] 站点专有列回填完成: {backfill_stats}")

        # 导出 Vertex 3016 防环黑名单
        if blocklist_path:
            try:
                # 若传入的是文件路径，取其所在目录
                bdir = os.path.dirname(blocklist_path) if blocklist_path.endswith(".json") else blocklist_path
                cnt = graph_mgr.export_vertex_blocklist(bdir)
                logger.info(f"[Vertex防环] 成功导出 {cnt} 条防环规则至 {bdir}/published_music_ids.json")
            except Exception as e:
                logger.warning(f"[Vertex防环] 导出失败: {e}")

    # 生成对账结果表
    all_sites = sorted(set(list(pre_counts.keys()) + list(post_published.keys()) + list(post_existing.keys())))
    
    print("\n" + "=" * 80)
    print(" " * 22 + "MusicHub 3.0 存量真伪清洗对账报告")
    print("=" * 80)
    mode_str = "【DRY-RUN 预览模式】(未改动数据库)" if dry_run else "【APPLY 生产写入模式】(已完成清洗与更新)"
    print(f"模式: {mode_str}")
    print(f"目标库: {db_path}")
    print(f"总记录: {total_pre} 行 | 抢救脏TID: {rescued_count} 个 | 置空脏TID: {cleared_count} 个\n")

    header = f"{'站点 (Site)':<15} | {'清洗前总记录':<12} | {'清洗后真发(PUBLISHED)':<22} | {'清洗后已存(EXISTING)':<20}"
    print(header)
    print("-" * len(header))

    tot_pub = 0
    tot_ext = 0
    for s in all_sites:
        pre = pre_counts.get(s, 0)
        pub = post_published.get(s, 0)
        ext = post_existing.get(s, 0)
        tot_pub += pub
        tot_ext += ext
        print(f"{s:<15} | {pre:<12} | {pub:<22} | {ext:<20}")

    print("-" * len(header))
    print(f"{'合计 (TOTAL)':<15} | {total_pre:<12} | {tot_pub:<22} | {tot_ext:<20}")
    print("=" * 80 + "\n")

    conn_musichub.close()
    if conn_data:
        conn_data.close()

    return {
        "total": total_pre,
        "rescued_count": rescued_count,
        "cleared_count": cleared_count,
        "total_published": tot_pub,
        "total_existing": tot_ext,
        "pre_counts": pre_counts,
        "post_published": post_published,
        "post_existing": post_existing,
    }


def main():
    parser = argparse.ArgumentParser(description="MusicHub 3.0 发种真伪判定与存量清洗工具")
    parser.add_argument("--db", type=str, default="", help="musichub.db 路径 (默认自动探测)")
    parser.add_argument("--legacy-db", type=str, default="", help="老系统 data.db 路径 (默认自动探测)")
    parser.add_argument("--apply", action="store_true", help="真实落库执行 (默认仅 dry-run)")
    parser.add_argument("--blocklist", type=str, default="", help="Vertex 防环 published_music_ids.json 导出路径")
    args = parser.parse_args()

    # 自动探测 DB 路径
    db_path = args.db
    if not db_path:
        candidates = [
            "/app/musichub/data/musichub.db",
            "D:/Projects/PT-Project/musichub/data/musichub.db",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "musichub.db")
        ]
        for c in candidates:
            if os.path.exists(c):
                db_path = c
                break

    legacy_db = args.legacy_db
    if not legacy_db:
        candidates = [
            "/app/musichub/data/data.db",
            "D:/Projects/PT-Project/musichub/data/data.db",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "data.db")
        ]
        for c in candidates:
            if os.path.exists(c):
                legacy_db = c
                break

    blocklist = args.blocklist
    if not blocklist:
        candidate = "/volume2/docker_ssd/vertex2/data/vertex/data/published_music_ids.json"
        if os.path.exists(os.path.dirname(candidate)):
            blocklist = candidate

    dry_run = not args.apply
    reconcile(db_path=db_path, legacy_db_path=legacy_db, dry_run=dry_run, blocklist_path=blocklist)


if __name__ == "__main__":
    main()
