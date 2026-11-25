# -*- coding: utf-8 -*-
"""MusicHub 乌托邦 & 皇后 H&R 每日保底巡检与补种守护 (hr_guard.py)

职责：
1. 读取乌托邦 (Musopia `myhr.php`) 与皇后 (OpenCD `torrents.php?option-torrents=4/8`)
   上处于 H&R 考察期或存在 H&R 风险的种子列表（基于 CookieCloud，绝不硬编码凭据）。
2. 解析每个 (site, tid) 的真实 info_hash（本地缓存/台账优先，缺失时下载 .torrent 并缓存）。
3. 扫描全集群 qB（3010 + 3016 + 8 音乐池 + reseed1~7）：
   - 已在线做种/下载中 -> HEALTHY
   - 已存在但处于暂停态且 progress>=1.0 -> 原地唤醒 (RESUMED)
   - 全集群缺失 -> 精确匹配本地音乐池实体数据，通过安全注入恢复做种 (REPAIRED)；
     严格遵守同 hash 单实例与 music5 禁区红线；无数据时告警 (MISSING_DATA)。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from musichub.collector.source_detail import SourceDetailFetcher
from musichub.core.qb_client import QBClient
from musichub.core.torrent_tools import bdecode, bencode
from musichub.storage.handoff import PoolConfig


def extract_torrent_payload(torrent_bytes: bytes) -> Tuple[str, List[Tuple[str, int]]]:
    """从 .torrent 字节提取 (root_name, [(rel_path, size), ...])。"""
    meta, _ = bdecode(torrent_bytes)
    info = meta.get(b"info") or {}
    root_name = (info.get(b"name") or b"").decode("utf-8", errors="replace")
    files: List[Tuple[str, int]] = []
    if b"files" in info:
        for f in info[b"files"]:
            parts = [p.decode("utf-8", errors="replace") for p in (f.get(b"path") or [])]
            files.append((os.path.join(*parts) if parts else root_name, int(f.get(b"length") or 0)))
    elif b"length" in info:
        files.append((root_name, int(info[b"length"])))
    return root_name, files

logger = logging.getLogger("HRGuard")

FORBIDDEN_POOLS = ("music5",)
ACTIVE_STATES = {
    "uploading", "stalledup", "forcedup", "queuedup", "checkingup",
    "checkingresumedata", "allocating", "downloading", "stalleddl",
    "forceddl", "queueddl", "metadl",
}
PAUSED_DONE_STATES = {"pausedup", "stoppedup"}


def parse_musopia_myhr_html(html: str) -> List[Dict[str, str]]:
    """解析乌托邦 myhr.php 页面中的 H&R 种子行。"""
    soup = BeautifulSoup(html or "", "html.parser")
    items: List[Dict[str, str]] = []
    seen = set()
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 8:
            continue
        hr_id = tds[0].get_text(strip=True)
        if not hr_id.isdigit():
            continue
        a = tds[1].find("a", href=True)
        if not a:
            continue
        m = re.search(r"details\.php\?id=(\d+)", a.get("href", ""))
        if not m:
            continue
        tid = m.group(1)
        if tid in seen:
            continue
        seen.add(tid)
        items.append({
            "site": "musopia",
            "tid": tid,
            "hr_id": hr_id,
            "title": a.get_text(" ", strip=True),
            "need_time": tds[5].get_text(" ", strip=True) if len(tds) > 5 else "",
            "rem_time": tds[7].get_text(" ", strip=True) if len(tds) > 7 else "",
        })
    return items


def parse_opencd_torrents_html(html: str, kind: str = "hr_risk") -> List[Dict[str, str]]:
    """解析 OpenCD torrents.php 列表页中的种子 ID 与标题。"""
    soup = BeautifulSoup(html or "", "html.parser")
    items: List[Dict[str, str]] = []
    seen = set()
    for tbl in soup.select("table.torrents"):
        for a in tbl.select("a[href*='details.php?id=']"):
            href = a.get("href", "")
            m = re.search(r"details\.php\?id=(\d+)", href)
            if not m:
                continue
            tid = m.group(1)
            if tid in seen:
                continue
            seen.add(tid)
            title = a.get("title") or a.get_text(" ", strip=True)
            items.append({
                "site": "opencd",
                "tid": tid,
                "kind": kind,
                "title": title,
            })
    return items


def _has_next_page(html: str, current_page: int) -> bool:
    """检查 NexusPHP 列表页是否存在下一页。"""
    soup = BeautifulSoup(html or "", "html.parser")
    next_p = current_page + 1
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if f"page={next_p}" in href:
            return True
    return False


class HRGuard:
    """乌托邦 & 皇后 H&R 保底检查与自动补种恢复引擎。"""

    def __init__(
        self,
        db_path: str = "/app/musichub/data/musichub.db",
        torrents_dir: str = "/app/musichub/data/torrents",
        host: str = "192.168.0.7",
        pools: Optional[List[PoolConfig]] = None,
        fetcher: Optional[SourceDetailFetcher] = None,
    ):
        self.db_path = db_path
        self.torrents_dir = torrents_dir
        self.host = host
        self.pools = pools or [
            PoolConfig(name=f"music_pool_{i}", port=3200 + i * 10, mount_dir=f"/volume1/video/download/music/music_pool_{i}", host=host)
            for i in range(1, 9)
        ]
        self.fetcher = fetcher or SourceDetailFetcher()

    # ---------- 1. 抓取站点 H&R 列表 ----------

    def fetch_musopia_hr(self, max_pages: int = 20) -> List[Dict[str, str]]:
        cookie = self.fetcher.cookie_client.get_cookie_header("www.musopia.vip")
        if not cookie:
            logger.warning("[HRGuard] Musopia Cookie 为空，跳过 myhr.php 抓取")
            return []
        all_items: List[Dict[str, str]] = []
        seen = set()
        for page in range(max_pages):
            url = "https://www.musopia.vip/myhr.php" if page == 0 else f"https://www.musopia.vip/myhr.php?page={page}"
            try:
                resp = self.fetcher.session.get(url, headers={"Cookie": cookie}, timeout=20, verify=False)
                if resp.status_code != 200 or "login.php" in (resp.url or ""):
                    logger.warning(f"[HRGuard] Musopia myhr.php page={page} HTTP {resp.status_code}")
                    break
                page_items = parse_musopia_myhr_html(resp.text)
                for it in page_items:
                    if it["tid"] not in seen:
                        seen.add(it["tid"])
                        all_items.append(it)
                if not page_items or not _has_next_page(resp.text, page):
                    break
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"[HRGuard] Musopia myhr.php page={page} 异常: {e}")
                break
        return all_items

    def fetch_opencd_hr(self, max_pages: int = 20) -> List[Dict[str, str]]:
        cookie = self.fetcher.cookie_client.get_cookie_header("open.cd")
        if not cookie:
            logger.warning("[HRGuard] OpenCD Cookie 为空，跳过 H&R 列表抓取")
            return []
        all_items: List[Dict[str, str]] = []
        seen = set()
        for opt, kind in ((4, "hr_risk"), (8, "hr_hit")):
            for page in range(max_pages):
                url = (
                    f"https://open.cd/torrents.php?option-torrents={opt}"
                    if page == 0
                    else f"https://open.cd/torrents.php?option-torrents={opt}&page={page}"
                )
                try:
                    resp = self.fetcher.session.get(url, headers={"Cookie": cookie}, timeout=20, verify=False)
                    if resp.status_code != 200 or "login.php" in (resp.url or ""):
                        logger.warning(f"[HRGuard] OpenCD opt={opt} page={page} HTTP {resp.status_code}")
                        break
                    page_items = parse_opencd_torrents_html(resp.text, kind=kind)
                    for it in page_items:
                        if it["tid"] not in seen:
                            seen.add(it["tid"])
                            all_items.append(it)
                    if not page_items or not _has_next_page(resp.text, page):
                        break
                    time.sleep(0.5)
                except Exception as e:
                    logger.warning(f"[HRGuard] OpenCD opt={opt} page={page} 异常: {e}")
                    break
        return all_items

    # ---------- 2. 取得 .torrent 字节与 info_hash ----------

    def _load_source_task_hashes(self) -> Dict[Tuple[str, str], str]:
        mapping: Dict[Tuple[str, str], str] = {}
        if not os.path.exists(self.db_path):
            return mapping
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10.0)
            for row in conn.execute("SELECT source_site, source_tid, info_hash FROM source_tasks WHERE source_tid != ''"):
                s = str(row[0] or "").strip().lower()
                t = str(row[1] or "").strip()
                h = str(row[2] or "").strip().lower()
                if s and t and len(h) == 40:
                    mapping[(s, t)] = h
            conn.close()
        except Exception as e:
            logger.debug(f"[HRGuard] 读取 source_tasks 失败: {e}")
        return mapping

    def _torrent_cache_path(self, site: str, tid: str) -> str:
        return os.path.join(self.torrents_dir, f"{site.lower()}_{tid}.torrent")

    def get_torrent_bytes(self, site: str, tid: str) -> Optional[bytes]:
        cache_path = self._torrent_cache_path(site, tid)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "rb") as fh:
                    data = fh.read()
                if data and not data.lstrip().startswith(b"<"):
                    return data
            except OSError:
                pass

        domain = "www.musopia.vip" if site == "musopia" else "open.cd"
        base = f"https://{domain}"
        cookie = self.fetcher.cookie_client.get_cookie_header(domain)
        if not cookie:
            return None
        try:
            resp = self.fetcher.session.get(
                f"{base}/download.php?id={tid}",
                headers={"Cookie": cookie},
                timeout=25,
                verify=False,
            )
            data = resp.content or b""
            if resp.status_code == 200 and len(data) > 100 and not data.lstrip().startswith(b"<"):
                os.makedirs(self.torrents_dir, exist_ok=True)
                with open(cache_path, "wb") as fh:
                    fh.write(data)
                return data
        except Exception as e:
            logger.warning(f"[HRGuard] 下载 {site}:{tid} .torrent 失败: {e}")
        return None

    @staticmethod
    def compute_info_hash(torrent_bytes: bytes) -> str:
        try:
            meta, _ = bdecode(torrent_bytes)
            if isinstance(meta, dict) and b"info" in meta:
                # extract_torrent_payload already validates info dict; compute raw info sha1
                # Locate raw bencoded info slice via re-encode or simple scan
                from musichub.core.torrent_tools import bencode as _bencode
                return hashlib.sha1(_bencode(meta[b"info"])).hexdigest().lower()
        except Exception:
            pass
        return ""

    # ---------- 3. 全集群 qB 扫描与实体数据匹配 ----------

    def _build_qb_clients(self) -> List[Tuple[str, int, QBClient, str]]:
        """返回 [(instance_name, port, QBClient, mount_dir), ...]"""
        clients: List[Tuple[str, int, QBClient, str]] = []
        host = self.host
        clients.append(("publish", 3010, QBClient(host, 3010), ""))
        clients.append(("musicdownload", 3016, QBClient(host, 3016), ""))
        for pool in self.pools:
            clients.append((pool.name, pool.port, QBClient(pool.host, pool.port), pool.mount_dir))
        for port in range(3320, 3390, 10):
            idx = (port - 3310) // 10
            clients.append((f"reseed{idx}", port, QBClient(host, port), ""))
        return clients

    def scan_cluster_torrents(self, clients: List[Tuple[str, int, QBClient, str]]) -> Dict[str, List[Dict[str, Any]]]:
        index: Dict[str, List[Dict[str, Any]]] = {}
        for name, port, qb, mount_dir in clients:
            try:
                torrents = qb.get_torrents() or []
            except Exception:
                continue
            for t in torrents:
                h = str(t.get("hash") or "").strip().lower()
                if not h:
                    continue
                index.setdefault(h, []).append({
                    "instance": name,
                    "port": port,
                    "qb": qb,
                    "mount_dir": mount_dir,
                    "state": str(t.get("state") or "").lower(),
                    "progress": float(t.get("progress") or 0.0),
                    "save_path": str(t.get("save_path") or ""),
                    "name": str(t.get("name") or ""),
                })
        return index

    def _find_matching_pool_data(self, torrent_bytes: bytes) -> Optional[Tuple[str, int, QBClient, str, str]]:
        """在受管音乐池目录中精确匹配载荷文件清单，返回 (pool_name, port, qb, mount_dir, root_name)。"""
        try:
            root_name, files = extract_torrent_payload(torrent_bytes)
        except Exception:
            return None
        if not root_name or not files:
            return None

        for pool in self.pools:
            mdir = pool.mount_dir
            if not mdir or any(f in mdir for f in FORBIDDEN_POOLS):
                continue
            cand_root = os.path.join(mdir, root_name)
            if not os.path.exists(cand_root):
                continue
            matched = True
            for rel_path, expected_size in files:
                fpath = cand_root if rel_path == root_name and os.path.isfile(cand_root) else os.path.join(cand_root, rel_path)
                if not os.path.isfile(fpath) or os.path.getsize(fpath) != expected_size:
                    matched = False
                    break
            if matched:
                qb = QBClient(pool.host, pool.port)
                return (pool.name, pool.port, qb, mdir, root_name)
        return None

    # ---------- 4. 主执行流程 ----------

    def run(self, sites: Tuple[str, ...] = ("musopia", "opencd"), apply: bool = False) -> Dict[str, Any]:
        hr_items: List[Dict[str, str]] = []
        if "musopia" in sites:
            hr_items.extend(self.fetch_musopia_hr())
        if "opencd" in sites:
            hr_items.extend(self.fetch_opencd_hr())

        st_hashes = self._load_source_task_hashes()
        clients = self._build_qb_clients()
        cluster_idx = self.scan_cluster_torrents(clients)

        summary: Dict[str, Any] = {
            "apply": apply,
            "total_hr": len(hr_items),
            "healthy": 0,
            "resumed": 0,
            "repaired": 0,
            "missing_data": 0,
            "hash_unknown": 0,
            "details": [],
        }

        for item in hr_items:
            site = item["site"]
            tid = item["tid"]
            title = item.get("title", "")
            ih = st_hashes.get((site, tid), "")
            tbytes: Optional[bytes] = None

            if not ih:
                tbytes = self.get_torrent_bytes(site, tid)
                if tbytes:
                    ih = self.compute_info_hash(tbytes)
                time.sleep(0.3)

            if not ih:
                summary["hash_unknown"] += 1
                summary["details"].append({"site": site, "tid": tid, "status": "HASH_UNKNOWN", "title": title})
                continue

            hits = cluster_idx.get(ih, [])
            # 1) 检查是否已有活跃做种/下载
            active_hits = [h for h in hits if h["state"] in ACTIVE_STATES]
            if active_hits:
                summary["healthy"] += 1
                continue

            # 2) 检查是否有 paused/stopped 完成任务（优先唤醒物理池，避开 3010）
            paused_hits = [h for h in hits if h["state"] in PAUSED_DONE_STATES and h["progress"] >= 1.0]
            if paused_hits:
                target_hit = next((h for h in paused_hits if h["instance"] != "publish"), paused_hits[0])
                if apply:
                    ok = target_hit["qb"].resume_torrent(ih)
                    if ok:
                        summary["resumed"] += 1
                        summary["details"].append({
                            "site": site, "tid": tid, "hash": ih,
                            "status": "RESUMED", "instance": target_hit["instance"], "title": title,
                        })
                        continue
                else:
                    summary["resumed"] += 1
                    summary["details"].append({
                        "site": site, "tid": tid, "hash": ih,
                        "status": "WOULD_RESUME", "instance": target_hit["instance"], "title": title,
                    })
                    continue

            # 3) 全集群均不存在该 hash -> 查实体数据安全注入恢复
            if hits:
                # 存在于某实例但未完成/异常状态，暂报 MISSING_DATA 避免重复注入
                summary["missing_data"] += 1
                summary["details"].append({
                    "site": site, "tid": tid, "hash": ih,
                    "status": "INCOMPLETE_IN_QB", "hits": [(x["instance"], x["state"], x["progress"]) for x in hits], "title": title,
                })
                continue

            if not tbytes:
                tbytes = self.get_torrent_bytes(site, tid)
            match = self._find_matching_pool_data(tbytes) if tbytes else None
            if not match:
                summary["missing_data"] += 1
                summary["details"].append({
                    "site": site, "tid": tid, "hash": ih,
                    "status": "MISSING_DATA", "title": title,
                })
                continue

            pool_name, pool_port, qb, mdir, root_name = match
            if not apply:
                summary["repaired"] += 1
                summary["details"].append({
                    "site": site, "tid": tid, "hash": ih,
                    "status": "WOULD_REPAIR", "pool": pool_name, "title": title,
                })
                continue

            # 安全注入：paused=True + skip_checking=True，回读 progress=1.0 再 start
            ok_add = qb.add_torrent_file(
                torrent_bytes=tbytes,
                save_path="/downloads",
                paused=True,
                skip_checking=True,
                tags="musichub,hr-guard",
                auto_tmm=False,
            )
            if ok_add:
                verified = False
                for _ in range(10):
                    time.sleep(0.5)
                    t_list = [t for t in (qb.get_torrents() or []) if str(t.get("hash") or "").lower() == ih]
                    if t_list and float(t_list[0].get("progress") or 0.0) >= 1.0:
                        verified = True
                        break
                if verified:
                    qb.resume_torrent(ih)
                    summary["repaired"] += 1
                    summary["details"].append({
                        "site": site, "tid": tid, "hash": ih,
                        "status": "REPAIRED", "pool": pool_name, "title": title,
                    })
                else:
                    qb.delete_torrent(ih, delete_files=False)
                    summary["missing_data"] += 1
                    summary["details"].append({
                        "site": site, "tid": tid, "hash": ih,
                        "status": "VERIFY_FAILED_ROLLED_BACK", "pool": pool_name, "title": title,
                    })
            else:
                summary["missing_data"] += 1
                summary["details"].append({
                    "site": site, "tid": tid, "hash": ih,
                    "status": "ADD_FAILED", "pool": pool_name, "title": title,
                })

        return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="乌托邦 & 皇后 H&R 保底巡检与补种")
    parser.add_argument("--site", choices=["all", "musopia", "opencd"], default="all")
    parser.add_argument("--apply", action="store_true", help="真正执行唤醒/补种（默认 dry-run）")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    sites = ("musopia", "opencd") if args.site == "all" else (args.site,)
    res = HRGuard().run(sites=sites, apply=args.apply)
    if args.format == "json":
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(
            f"[HRGuard] apply={res['apply']} total={res['total_hr']} "
            f"healthy={res['healthy']} resumed={res['resumed']} "
            f"repaired={res['repaired']} missing_data={res['missing_data']} "
            f"hash_unknown={res['hash_unknown']}"
        )
        for d in res["details"]:
            print(" ", d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
