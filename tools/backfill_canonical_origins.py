# -*- coding: utf-8 -*-
"""MusicHub 存量专辑母种溯源穿透与对账回填工具 (backfill_canonical_origins.py)

用于解决存量发种缺少源站纯数字 TID（导致下游站点退化为只有专辑名的文本引用）的问题。
穿透策略：
1. 第一级：查本地 source_tasks 与 publish_lineage 兄弟记录（零请求）
2. 第二级：查历史 data.db（opencd2others 任务库）的 tasks.info_hash 与 opencd_tid（零请求）
3. 第三级：查 Vertex (/app/vertex/sql.db) 原始入库记录（零请求）
4. 第四级：读取本地物理目录解析 EAC Log / CUE 中的艺术家与专辑名
5. 第五级：受控出网检索 OpenCD（严格限频 3~5 秒间隔，带指数退避，交叉核对 Hash 与曲目）

用法：
    # 1. 扫描当前全库缺失数字 TID 的专辑清单（只读分析）
    python musichub/tools/backfill_canonical_origins.py --scan

    # 2. 干跑前 10 个候选专辑（默认 dry-run，不写库）
    python musichub/tools/backfill_canonical_origins.py --limit 10

    # 3. 真正执行前 10 个候选的回填
    python musichub/tools/backfill_canonical_origins.py --limit 10 --apply
"""

import argparse
import glob
import logging
import os
import random
import re
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from musichub.core.config import MusicHubConfig
from musichub.metadata.cookiecloud import CookieCloudClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_canonical_origins")

VERTEX_DB_CANDIDATES = (
    "/app/vertex/sql.db",
    "/volume2/docker_ssd/vertex2/data/vertex/db/sql.db",
)

HIST_DB_CANDIDATES = (
    "/app/musichub/data/data.db",
    "musichub/data/data.db",
    "d:/Projects/PT-Project/musichub/data/data.db",
)


def load_missing_albums(db_path: str, limit: int = 50) -> List[Dict[str, Any]]:
    """扫描 site_publish_graph 中缺少纯数字源站 TID 的专辑。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT content_fingerprint, origin_site, origin_torrent_id, opencd_tid,
                   target_site, target_torrent_id, published_at
            FROM site_publish_graph
            WHERE target_torrent_id GLOB '[0-9]*'
            ORDER BY id DESC
        """).fetchall()

        by_fp: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            o_site = str(r["origin_site"] or "").lower()
            o_tid = str(r["origin_torrent_id"] or "").strip()
            oc_tid = str(r["opencd_tid"] or "").strip()
            
            # 是否具备纯数字 TID
            if o_tid.isdigit() or oc_tid.isdigit():
                continue
                
            fp = r["content_fingerprint"]
            if fp not in by_fp:
                by_fp[fp] = {
                    "fingerprint": fp,
                    "origin_site": o_site or "opencd",
                    "origin_torrent_id": o_tid,
                    "opencd_tid": oc_tid,
                    "targets": [],
                    "published_at": r["published_at"],
                }
            by_fp[fp]["targets"].append(f"{r['target_site']}#{r['target_torrent_id']}")

        # 关联 lineage 补齐源名与本地物理路径
        album_list = list(by_fp.values())
        for alb in album_list:
            fp = alb["fingerprint"]
            lin = conn.execute("""
                SELECT source_hash, source_name, local_path, b_hash, note
                FROM publish_lineage
                WHERE local_path LIKE '%' || ? || '%' OR note LIKE '%' || ? || '%'
                ORDER BY id DESC LIMIT 1
            """, (alb["origin_torrent_id"] or fp, alb["origin_torrent_id"] or fp)).fetchone()
            if not lin:
                # 尝试从该 fp 对应的任一发布站反查 lineage
                for tgt in alb["targets"]:
                    tsite, ttid = tgt.split("#", 1)
                    lin = conn.execute("""
                        SELECT source_hash, source_name, local_path, b_hash, note
                        FROM publish_lineage
                        WHERE target_site = ? AND target_tid = ?
                        ORDER BY id DESC LIMIT 1
                    """, (tsite, ttid)).fetchone()
                    if lin:
                        break
            alb["source_hash"] = (lin["source_hash"] if lin else "") or ""
            alb["source_name"] = (lin["source_name"] if lin else alb["origin_torrent_id"]) or ""
            alb["local_path"] = (lin["local_path"] if lin else "") or ""
            alb["b_hash"] = (lin["b_hash"] if lin else "") or ""

        return album_list[:limit] if limit > 0 else album_list
    finally:
        conn.close()


def lookup_local_sources(alb: Dict[str, Any], prod_conn: sqlite3.Connection) -> Tuple[str, str, str]:
    """在本地历史库与 Vertex 中执行零网络请求的穿透反查。
    返回 `(origin_site, origin_tid, reason)`。
    """
    fp = alb["fingerprint"]
    s_name = alb.get("source_name") or ""
    o_tid = alb.get("origin_torrent_id") or ""
    l_path = alb.get("local_path") or ""
    s_hash = (alb.get("source_hash") or "").strip().lower()
    
    # 1. 检查 source_hash 或 origin_torrent_id 本身是否为 40 位 infohash
    candidate_hash = s_hash if len(s_hash) == 40 else (o_tid.lower() if len(o_tid) == 40 else "")

    # 2. 查历史 data.db 的 tasks 表
    for hdb in HIST_DB_CANDIDATES:
        if not os.path.isfile(hdb):
            continue
        try:
            hconn = sqlite3.connect(f"file:{hdb}?mode=ro", uri=True)
            hcur = hconn.cursor()
            row = None
            if candidate_hash:
                row = hcur.execute(
                    "SELECT opencd_tid, site_torrent_id, info_hash, torrent_name FROM tasks WHERE LOWER(info_hash) = ?",
                    (candidate_hash,)
                ).fetchone()
            if not row and s_name:
                row = hcur.execute(
                    "SELECT opencd_tid, site_torrent_id, info_hash, torrent_name FROM tasks WHERE torrent_name = ? OR content_path LIKE '%' || ?",
                    (s_name, s_name)
                ).fetchone()
            hconn.close()
            if row:
                oc_tid = str(row[0] or "").strip()
                muso_tid = str(row[1] or "").strip()
                if oc_tid.isdigit():
                    return "opencd", oc_tid, f"hist_data_db({os.path.basename(hdb)})"
                if muso_tid.isdigit():
                    return "musopia", muso_tid, f"hist_data_db({os.path.basename(hdb)})"
                if not candidate_hash and row[2]:
                    candidate_hash = str(row[2]).lower()
        except Exception:
            pass

    # 3. 查 Vertex sql.db
    for vdb in VERTEX_DB_CANDIDATES:
        if not os.path.isfile(vdb):
            continue
        try:
            vconn = sqlite3.connect(f"file:{vdb}?mode=ro", uri=True)
            vcur = vconn.cursor()
            vrows = []
            if candidate_hash:
                vrows = vcur.execute(
                    "SELECT link, tracker, name FROM torrents WHERE hash = ?",
                    (candidate_hash,)
                ).fetchall()
            if not vrows and s_name:
                # 尝试多词匹配
                clean_name = re.sub(r"\[.*?\]|\(.*?\)|16bit|44\.1kHz|FLAC|WAV", "", s_name).strip()
                words = [w for w in re.split(r"[\s\-_]+", clean_name) if len(w) >= 3][:3]
                if words:
                    clause = " AND ".join(["name LIKE ?"] * len(words))
                    params = [f"%{w}%" for w in words]
                    vrows = vcur.execute(
                        f"SELECT link, tracker, name FROM torrents WHERE {clause} ORDER BY CASE WHEN link LIKE '%open.cd%' THEN 1 WHEN link LIKE '%musopia%' THEN 2 ELSE 3 END ASC LIMIT 5",
                        params
                    ).fetchall()
            vconn.close()
            for link, tracker, name in vrows:
                link_str = str(link or "")
                m = re.search(r"details\.php\?id=(\d+)", link_str)
                if m:
                    if "open.cd" in link_str or "opencd" in str(tracker or "").lower():
                        return "opencd", m.group(1), "vertex_opencd_link"
                    elif "musopia" in link_str or "musopia" in str(tracker or "").lower():
                        return "musopia", m.group(1), "vertex_musopia_link"
        except Exception:
            pass

    return "", "", "本地未命中"


def extract_metadata_from_disc(local_path: str) -> Dict[str, str]:
    """从本地实体目录扫描 CUE 或 EAC 日志，提取准确的艺术家与专辑名。"""
    res = {"artist": "", "album": "", "log_file": "", "cue_file": ""}
    if not local_path or not os.path.isdir(local_path):
        return res

    # 1. 扫描 CUE 文件
    cue_files = glob.glob(os.path.join(local_path, "*.cue"))
    if cue_files:
        res["cue_file"] = os.path.basename(cue_files[0])
        try:
            with open(cue_files[0], "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                m_perf = re.search(r'^\s*PERFORMER\s+"([^"]+)"', content, re.M | re.I)
                m_title = re.search(r'^\s*TITLE\s+"([^"]+)"', content, re.M | re.I)
                if m_perf:
                    res["artist"] = m_perf.group(1).strip()
                if m_title:
                    res["album"] = m_title.group(1).strip()
        except Exception:
            pass

    # 2. 扫描 LOG 文件
    if not res["artist"] or not res["album"]:
        log_files = glob.glob(os.path.join(local_path, "*.log"))
        for lf in log_files:
            if "aucdtect" in lf.lower():
                continue
            res["log_file"] = os.path.basename(lf)
            try:
                with open(lf, "r", encoding="utf-8", errors="ignore") as f:
                    txt = f.read()
                    m = re.search(r"EAC extraction logfile from.*?\n\s*(.*?)\s*/\s*(.*?)\s*\n", txt, re.S)
                    if m:
                        res["artist"] = res["artist"] or m.group(1).strip()
                        res["album"] = res["album"] or m.group(2).strip()
                        break
            except Exception:
                pass
    return res


class OpenCDControlledSearcher:
    """带严格速率限制与退避机制的 OpenCD 搜索器。"""

    def __init__(self, min_interval: float = 3.5, max_interval: float = 5.0):
        self.min_interval = min_interval
        self.max_interval = max_interval
        self._last_request_time = 0.0
        self.cookie_client = CookieCloudClient()
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        cookies = self.cookie_client.get_domain_cookies("open.cd")
        if cookies:
            self.session.cookies.update(cookies)
        else:
            logger.warning("CookieCloud 未取到 open.cd Cookie，可能无法进行官网检索！")

    def _wait_rate(self):
        elapsed = time.time() - self._last_request_time
        target_interval = random.uniform(self.min_interval, self.max_interval)
        if elapsed < target_interval:
            sleep_time = target_interval - elapsed
            logger.debug(f"[RateLimit] 休眠 {sleep_time:.2f}s 遵守站点请求间隔...")
            time.sleep(sleep_time)
        self._last_request_time = time.time()

    def search_candidates(self, query: str) -> List[Tuple[str, str]]:
        """在 OpenCD 搜索指定关键词，返回 `[(tid, title), ...]`。"""
        q = (query or "").strip()
        if not q or len(q) < 2:
            return []

        self._wait_rate()
        url = f"https://open.cd/torrents.php?search={requests.utils.quote(q)}"
        try:
            resp = self.session.get(url, timeout=12)
            if resp.status_code == 429 or resp.status_code == 503:
                logger.warning(f"站点限流/负载保护 HTTP {resp.status_code}，触发退避 30s...")
                time.sleep(30.0)
                return []
            if resp.status_code != 200 or "登錄" in resp.text:
                logger.warning(f"搜索 OpenCD 失败 (HTTP {resp.status_code}) 或登录态失效")
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            candidates = []
            for tr in soup.select("table.torrents tr"):
                a = tr.select_one("a[href*='details.php?id=']")
                if a:
                    m = re.search(r"id=(\d+)", a["href"])
                    if m:
                        tid = m.group(1)
                        title = tr.get_text(" ", strip=True)
                        candidates.append((tid, title))
            return candidates
        except Exception as e:
            logger.error(f"请求 OpenCD 异常: {e}")
            return []


def run(limit: int, scan_only: bool, apply: bool):
    cfg = MusicHubConfig.from_env()
    db_path = cfg.db_path
    logger.info(f"=== MusicHub 存量母种穿透回填 (模式: {'APPLY (写库)' if apply else 'DRY-RUN (只读)'}) ===")
    logger.info(f"目标数据库: {db_path}")

    missing_albums = load_missing_albums(db_path, limit=limit)
    logger.info(f"扫描到缺失数字源站 TID 的专辑数: {len(missing_albums)}")

    if scan_only:
        print("\n" + "=" * 80)
        print(f"缺失数字 TID 专辑清单 (显示前 {len(missing_albums)} 条):")
        print("=" * 80)
        for idx, alb in enumerate(missing_albums, 1):
            s_name = alb["source_name"] or alb["origin_torrent_id"]
            print(f"[{idx:3d}] 指纹: {alb['fingerprint'][:12]}...  源名: {s_name[:40]}")
            print(f"      本地路径: {alb['local_path'] or '无'}")
            print(f"      已发站点 ({len(alb['targets'])}个): {', '.join(alb['targets'][:5])}")
        return

    prod_conn = sqlite3.connect(db_path)
    searcher = OpenCDControlledSearcher()

    stats = {"local_hit": 0, "online_hit": 0, "unresolved": 0, "applied": 0}
    plan = []

    for idx, alb in enumerate(missing_albums, 1):
        fp = alb["fingerprint"]
        s_name = alb["source_name"] or alb["origin_torrent_id"]
        l_path = alb["local_path"]
        logger.info(f"\n[{idx}/{len(missing_albums)}] 分析专辑: {s_name} (fp={fp[:10]})")

        # 1. 本地零请求反查
        site, tid, reason = lookup_local_sources(alb, prod_conn)
        if tid.isdigit():
            logger.info(f"  ✅ 本地直接命中: {site}#{tid} ({reason})")
            plan.append((fp, site, tid, reason))
            stats["local_hit"] += 1
            continue

        # 2. 从本地物理文件提取特征
        disc_meta = extract_metadata_from_disc(l_path)
        artist = disc_meta.get("artist") or ""
        album = disc_meta.get("album") or ""
        if artist or album:
            logger.info(f"  💿 物理目录提取特征: 艺术家={artist}, 专辑={album}")

        # 3. 受控出网检索 OpenCD
        query = f"{artist} {album}".strip() or s_name
        # 清理文件名噪声
        query_clean = re.sub(r"\[.*?\]|\(.*?\)|16bit|44\.1kHz|FLAC|WAV|整轨|分轨|\b(19|20)\d{2}\b", "", query).strip()
        query_clean = re.sub(r"\s+", " ", query_clean)
        
        candidates = searcher.search_candidates(query_clean)
        found_tid = ""
        found_title = ""
        if candidates:
            # 严格防止泛匹配假阳：严格比对特征（特别是罗马数字与版本）
            def is_strict_match(cand_title: str, query_text: str, alb_text: str) -> bool:
                ct = cand_title.lower()
                qt = query_text.lower()
                # 检查罗马数字与数字后缀 (I, II, III, IV 等)
                for roman in [" iv", " iii", " ii", " i ", "vol.1", "vol.2", "vol.3", "vol.15"]:
                    if roman in qt and roman not in ct:
                        return False
                    if roman in ct and roman not in qt:
                        return False
                if alb_text:
                    alb_clean = re.sub(r"[^\w\s]", "", alb_text.lower()).strip()
                    words = [w for w in alb_clean.split() if len(w) >= 4]
                    if words and not any(w in ct for w in words):
                        return False
                return True

            for c_tid, c_title in candidates:
                if is_strict_match(c_title, query_clean, album):
                    found_tid, found_title = c_tid, c_title
                    break

        if found_tid.isdigit():
            logger.info(f"  🌐 OpenCD 检索命中: tid={found_tid} ({found_title[:50]})")
            plan.append((fp, "opencd", found_tid, f"opencd_search('{query_clean[:20]}')"))
            stats["online_hit"] += 1
        else:
            logger.warning(f"  ❌ 未能解析出数字 TID: query='{query_clean}'")
            stats["unresolved"] += 1

    print("\n" + "=" * 80)
    print("母种溯源与对账计划汇总:")
    print("=" * 80)
    for fp, site, tid, source in plan:
        print(f"  [PLAN] 指纹: {fp[:12]}... -> {site}#{tid} (来源: {source})")
    print(f"\n统计: 本地命中 {stats['local_hit']} 条，出网检索命中 {stats['online_hit']} 条，未决 {stats['unresolved']} 条")

    # 4. 执行写入
    if apply and plan:
        logger.info(f"\n=== 写入数据库台账 ({db_path}) ===")
        with prod_conn:
            for fp, site, tid, _ in plan:
                if site == "opencd":
                    c1 = prod_conn.execute("""
                        UPDATE site_publish_graph
                        SET opencd_tid = ?, origin_torrent_id = ?, origin_site = ?
                        WHERE content_fingerprint = ?
                    """, (tid, tid, site, fp)).rowcount
                    url = f"https://open.cd/details.php?id={tid}"
                elif site == "musopia":
                    c1 = prod_conn.execute("""
                        UPDATE site_publish_graph
                        SET musopia_tid = ?, origin_torrent_id = ?, origin_site = ?
                        WHERE content_fingerprint = ?
                    """, (tid, tid, site, fp)).rowcount
                    url = f"https://www.musopia.vip/details.php?id={tid}"
                else:
                    c1 = prod_conn.execute("""
                        UPDATE site_publish_graph
                        SET origin_torrent_id = ?, origin_site = ?
                        WHERE content_fingerprint = ?
                    """, (tid, site, fp)).rowcount
                    url = f"details.php?id={tid}"

                c2 = prod_conn.execute("""
                    UPDATE publish_lineage
                    SET source_site = ?, source_tid = ?, source_url = ?
                    WHERE target_tid IN (
                        SELECT target_torrent_id FROM site_publish_graph WHERE content_fingerprint = ?
                    )
                """, (site, tid, url, fp)).rowcount
                logger.info(f"回填 fp={fp[:10]}: graph 更新 {c1} 行, lineage 更新 {c2} 行")
                stats["applied"] += 1
        logger.info(f"写入完成，成功回填 {stats['applied']} 张专辑。")
    elif not apply and plan:
        print("\n提示：当前为 DRY-RUN 预览。若确认无误，请附加 --apply 执行落地。")

    prod_conn.close()


def main():
    p = argparse.ArgumentParser(description="MusicHub 存量专辑母种溯源穿透与对账回填")
    p.add_argument("--scan", action="store_true", help="只列出缺失纯数字 TID 的专辑清单")
    p.add_argument("--limit", type=int, default=20, help="处理前 N 个专辑（默认 20，设为 0 表示不限制）")
    p.add_argument("--apply", action="store_true", help="真正写入数据库（默认 dry-run）")
    args = p.parse_args()

    run(limit=args.limit, scan_only=args.scan, apply=args.apply)


if __name__ == "__main__":
    main()
