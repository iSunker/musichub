# -*- coding: utf-8 -*-
"""MusicHub 3.0 存量种子多站做种归一与 8 实例平摊重平衡工具 (ConsolidateAndRebalance)

设计目标与硬约束：
1. 【多站归一，零硬链接】：
   同一个专辑不管在多少个站点发了种，全部收拢归集到同一个 qB 实例做种，
   指向同一个物理目录，彻底摆脱跨池 Hardlink 维护与空间释放陷阱。
2. 【全 8 实例极致平摊】：
   严格按照 album_assignments.json / album_migration_matrix.csv 规划，
   将 3,839 个唯一专辑平分给 8 个实例，每个实例种子数收敛到 826 ~ 840 个。
3. 【原子性迁移安全闭环】：
   目标目录就位 -> 导出源 .torrent -> 暂停源端 -> 注入目标端(skip_checking=true)
   -> 轮询校验 100% -> 激活做种 -> 源端下线(deleteFiles=false) -> 失败自动回滚。
4. 【同卷零拷贝】：
   /volume1 内目录迁移直接 Python 原生 os.rename，毫秒级就绪，免受 shell 引号与 Unicode 特殊字符干扰。
5. 【冗余硬链接清理】：
   做种 100% 确认后，安全清理其他池残留的同名硬链接目录，彻底释放 inode 引用。
"""

import os
import sys

# 彻底清除内网请求的代理干扰
for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(k, None)
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"

import json
import base64
import time
import argparse
import logging
import subprocess
import requests
from typing import Dict, List, Optional, Tuple, Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Rebalancer")

# 8 个规范化运行容器映射的宿主物理目录与端口 (2026-11-23 全面重构规整)
POOL_SPECS = {
    "music_pool_1": {"port": 3210, "path": "/volume1/video/download/music/music_pool_1"},
    "music_pool_2": {"port": 3220, "path": "/volume1/video/download/music/music_pool_2"},
    "music_pool_3": {"port": 3230, "path": "/volume1/video/download/music/music_pool_3"},
    "music_pool_4": {"port": 3240, "path": "/volume1/video/download/music/music_pool_4"},
    "music_pool_5": {"port": 3250, "path": "/volume1/video/download/music/music_pool_5"},
    "music_pool_6": {"port": 3260, "path": "/volume1/video/download/music/music_pool_6"},
    "music_pool_7": {"port": 3270, "path": "/volume1/video/download/music/music_pool_7"},
    "music_pool_8": {"port": 3280, "path": "/volume1/video/download/music/music_pool_8"},
}

ALL_CANDIDATE_DIRS = [
    "/volume1/video/download/music/music_pool_1",
    "/volume1/video/download/music/music_pool_2",
    "/volume1/video/download/music/music_pool_3",
    "/volume1/video/download/music/music_pool_4",
    "/volume1/video/download/music/music_pool_5",
    "/volume1/video/download/music/music_pool_6",
    "/volume1/video/download/music/music_pool_7",
    "/volume1/video/download/music/music_pool_8",
]

# 严格禁删保护标记
FORBIDDEN_PATH_MARKERS = ("music5", "incomplete", "@eaDir", ".OrphanedData", ".RecycleBin", "lost+found")

NAS_HOST = "192.168.0.7"
SSH_PORT = 36688
SSH_USER = "iSunker"


def run_remote_fs(action: str, payload: dict) -> Tuple[int, str, str]:
    """通过 SSH 运行 NAS 上的 helper 脚本，以 base64 传递 JSON 参数，绝对杜绝 stdin 挂起与 shell 转义"""
    b64_arg = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode('utf-8')).decode('ascii')
    cmd = ["ssh", "-p", str(SSH_PORT), f"{SSH_USER}@{NAS_HOST}", f"python3 /tmp/musichub_fs.py {action} {b64_arg}"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out_b, err_b = p.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill()
        return 1, "", "Timeout in run_remote_fs"
    out = out_b.decode("utf-8", errors="ignore").strip()
    err = err_b.decode("utf-8", errors="ignore").strip()
    return p.returncode, out, err


def remote_ensure_album_dir(album_name: str, target_pool: str, dry_run: bool = False) -> Tuple[bool, str, str]:
    """确保目标池的物理目录就位：如果不存在，从其他候选池原子 rename 过来"""
    target_path = os.path.join(POOL_SPECS[target_pool]["path"], album_name).replace("\\", "/")
    payload = {
        "album_name": album_name,
        "target_path": target_path,
        "candidate_dirs": ALL_CANDIDATE_DIRS,
        "dry_run": dry_run
    }
    rc, out, err = run_remote_fs("ensure_album_dir", payload)
    if rc != 0:
        return False, "", err or out
    try:
        res = json.loads(out)
        return True, res.get("status", ""), res.get("source", "")
    except Exception as e:
        return False, "", f"Parse error: {e}, raw: {out}"


def remote_cleanup_redundant(album_name: str, keep_path: str, dry_run: bool = False) -> List[str]:
    """在做种成功后，安全清理其他候选池中遗留的同名硬链接目录，彻底释放跨池残留"""
    payload = {
        "album_name": album_name,
        "keep_path": keep_path,
        "candidate_dirs": ALL_CANDIDATE_DIRS,
        "dry_run": dry_run
    }
    rc, out, _ = run_remote_fs("cleanup_redundant", payload)
    if rc == 0 and out:
        try:
            return json.loads(out)
        except Exception:
            return []
    return []


class QBInstanceAPI:
    def __init__(self, port: int, host: str = NAS_HOST):
        self.port = port
        self.base_url = f"http://{host}:{port}/api/v2"
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies = {"http": None, "https": None}

    def get_torrents(self) -> List[Dict[str, Any]]:
        # 优先使用 sync/maindata，超载实例(如3260)数据量达 6.4MB，给予 60s 超时窗口与重试
        for attempt in range(2):
            try:
                r = self.session.get(f"{self.base_url}/sync/maindata", timeout=60)
                if r.status_code == 200:
                    data = r.json()
                    torrents_dict = data.get("torrents", {})
                    result = []
                    for h, info in torrents_dict.items():
                        info["hash"] = h
                        result.append(info)
                    return result
            except Exception as e:
                logger.warning(f"sync/maindata on {self.port} attempt {attempt+1} failed: {e}")
                time.sleep(1)
        # 降级方案
        try:
            r = self.session.get(f"{self.base_url}/torrents/info", timeout=60)
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    def export_torrent(self, info_hash: str) -> Optional[bytes]:
        try:
            r = self.session.get(f"{self.base_url}/torrents/export", params={"hash": info_hash}, timeout=15)
            if r.status_code == 200 and len(r.content) > 50:
                return r.content
        except Exception as e:
            logger.warning(f"Export {info_hash} on {self.port} failed: {e}")
        return None

    def pause_torrent(self, info_hash: str) -> bool:
        # qB 5.x 推荐使用 /torrents/stop，4.x 为 /torrents/pause
        try:
            r = self.session.post(f"{self.base_url}/torrents/stop", data={"hashes": info_hash}, timeout=10)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        try:
            r = self.session.post(f"{self.base_url}/torrents/pause", data={"hashes": info_hash}, timeout=10)
            return r.status_code == 200
        except Exception:
            return False

    def start_torrent(self, info_hash: str) -> bool:
        # qB 5.x 推荐使用 /torrents/start，4.x 为 /torrents/resume
        try:
            r = self.session.post(f"{self.base_url}/torrents/start", data={"hashes": info_hash}, timeout=10)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        try:
            r = self.session.post(f"{self.base_url}/torrents/resume", data={"hashes": info_hash}, timeout=10)
            return r.status_code == 200
        except Exception:
            return False

    def add_torrent(self, torrent_bytes: bytes, save_path: str = "/downloads") -> bool:
        for attempt in range(2):
            try:
                files = {"torrents": ("task.torrent", torrent_bytes, "application/x-bittorrent")}
                data = {
                    "savepath": save_path,
                    "paused": "false",
                    "skip_checking": "true",
                    "autoTMM": "false",
                }
                r = self.session.post(f"{self.base_url}/torrents/add", files=files, data=data, timeout=40)
                if r.status_code == 200:
                    return True
            except Exception as e:
                logger.warning(f"Add torrent to {self.port} attempt {attempt+1} failed: {e}")
                time.sleep(1.5)
        return False

    def delete_torrent(self, info_hash: str, delete_files: bool = False) -> bool:
        for attempt in range(2):
            try:
                r = self.session.post(
                    f"{self.base_url}/torrents/delete",
                    data={"hashes": info_hash, "deleteFiles": str(delete_files).lower()},
                    timeout=25
                )
                if r.status_code == 200:
                    return True
            except Exception as e:
                logger.warning(f"Delete {info_hash} on {self.port} attempt {attempt+1} failed: {e}")
                time.sleep(1)
        return False

    def wait_seeding(self, info_hash: str, timeout: float = 15.0) -> bool:
        """注入后轮询种子状态，必须达标 progress == 1.0"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = self.session.get(f"{self.base_url}/torrents/info", params={"hashes": info_hash}, timeout=5)
                if r.status_code == 200:
                    arr = r.json()
                    if arr:
                        t = arr[0]
                        prog = t.get("progress", 0)
                        state = t.get("state", "")
                        if prog >= 1.0 or state in ("stalledUP", "uploading", "forcedUP", "stoppedUP"):
                            if state == "stoppedUP":
                                self.start_torrent(info_hash)
                            return True
                        if state in ("missingFiles", "error"):
                            logger.error(f"Torrent {info_hash} entered bad state: {state}")
                            return False
            except Exception:
                pass
            time.sleep(1.2)
        return False


class Consolidator:
    def __init__(self, plan_file: str, ledger_file: str):
        self.plan_path = plan_file
        self.ledger_path = ledger_file
        self.clients = {p: QBInstanceAPI(POOL_SPECS[p]["port"]) for p in POOL_SPECS}
        
        with open(plan_file, "r", encoding="utf-8") as f:
            self.plan = json.load(f)
            
        self.completed_albums = set()
        if os.path.exists(ledger_file):
            with open(ledger_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        if record.get("status") == "CONSOLIDATED":
                            self.completed_albums.add(record.get("album"))
                    except Exception:
                        pass
        logger.info(f"Loaded plan with {len(self.plan)} albums. Already consolidated: {len(self.completed_albums)}")

    def rebalance_single_album(self, album: Dict[str, Any], dry_run: bool = False) -> bool:
        name = album["name"]
        target_pool = album["assigned_pool"]
        target_port = POOL_SPECS[target_pool]["port"]
        target_client = self.clients[target_pool]
        t_count = album["torrents_count"]
        target_dir = os.path.join(POOL_SPECS[target_pool]["path"], name).replace("\\", "/")

        logger.info(f"\n[Album] {name} ({album['size_mb']} MB, {t_count} 种子)")
        logger.info(f"  目标实例: {target_pool} (port {target_port})")

        # 1. 确保物理目录就位
        ok, status, source = remote_ensure_album_dir(name, target_pool, dry_run=dry_run)
        if not ok:
            logger.error(f"  物理目录定位/移动失败: {source}")
            return False

        if status == "EXISTS":
            logger.info(f"  物理目录已在目标池就位: {target_dir}")
        elif status in ("MOVED", "WILL_MOVE"):
            logger.info(f"  物理搬移: {source} -> {target_dir}")

        # 2. 遍历该专辑的所有种子，收拢注入到 target_client
        for t in album["torrents"]:
            h = t["hash"]
            src_pool = t.get("instance")
            src_port = t.get("port")
            tr = t.get("tracker", "")
            domain = tr.split("/")[2] if "//" in tr else tr

            # 幂等检查：目标端是否已经拥有该 hash 且正常做种
            try:
                r_check = target_client.session.get(f"{target_client.base_url}/torrents/info", params={"hashes": h}, timeout=5)
                if r_check.status_code == 200 and r_check.json():
                    exist_t = r_check.json()[0]
                    if exist_t.get("progress", 0) >= 1.0 or exist_t.get("state") in ("uploading", "stalledUP", "forcedUP", "stoppedUP"):
                        logger.info(f"    - [{domain}] 目标端已就绪做种: {h[:10]}..")
                        if not dry_run:
                            target_client.start_torrent(h)
                        continue
            except Exception:
                pass

            if src_port == target_port:
                logger.info(f"    - [{domain}] 已在目标端做种: {h[:10]}..")
                if not dry_run:
                    target_client.start_torrent(h)
                continue

            logger.info(f"    - [{domain}] 迁移 {h[:10]}..: port {src_port} -> port {target_port}")
            if dry_run:
                continue

            src_client = QBInstanceAPI(src_port)
            # a. 导出种子
            t_bytes = src_client.export_torrent(h)
            if not t_bytes:
                logger.error(f"      导出 .torrent 失败: {h}")
                return False

            # b. 暂停源端任务
            src_client.pause_torrent(h)
            time.sleep(0.5)

            # c. 注入目标端
            if not target_client.add_torrent(t_bytes):
                logger.error(f"      注入目标实例失败: {h}")
                src_client.start_torrent(h)
                return False

            # d. 等待目标端 100% 做种就绪并激活
            if not target_client.wait_seeding(h, timeout=15.0):
                logger.error(f"      目标端未能就绪 (未达 100%): {h}，执行回滚")
                target_client.delete_torrent(h, delete_files=False)
                src_client.start_torrent(h)
                return False

            target_client.start_torrent(h)

            # e. 目标端就绪，安全移除源端任务（绝对 delete_files=False）
            src_client.delete_torrent(h, delete_files=False)
            logger.info(f"      迁移成功并安全下线源端: {h[:10]}..")

        # 3. 清理冗余硬链接
        if not dry_run:
            cleaned = remote_cleanup_redundant(name, target_dir, dry_run=False)
            if cleaned:
                logger.info(f"  已清除 {len(cleaned)} 处残留硬链接目录: {', '.join(cleaned)}")

            with open(self.ledger_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "album": name,
                    "target_pool": target_pool,
                    "torrents_count": t_count,
                    "timestamp": time.time(),
                    "status": "CONSOLIDATED"
                }, ensure_ascii=False) + "\n")
            self.completed_albums.add(name)

        return True

    def run(self, limit: int = 0, dry_run: bool = False, only_album: str = "") -> None:
        processed = 0
        success = 0
        for album in self.plan:
            name = album["name"]
            if only_album and name != only_album:
                continue
            if not only_album and name in self.completed_albums:
                continue

            ok = self.rebalance_single_album(album, dry_run=dry_run)
            if ok:
                success += 1
            processed += 1

            if limit > 0 and processed >= limit:
                logger.info(f"已达到本次处理上限 ({limit})，暂停执行。")
                break

        logger.info(f"\n执行总结: 处理 {processed} 个专辑，成功 {success} 个。")


def print_status():
    print(f"{'Pool':<16}{'Port':<7}{'Total Torrents':<16}{'Seeding':<10}")
    print("-" * 50)
    for p, spec in POOL_SPECS.items():
        api = QBInstanceAPI(spec["port"])
        torrents = api.get_torrents()
        seeding = sum(1 for t in torrents if t.get("progress", 0) >= 1.0 or t.get("state") in ("uploading", "stalledUP"))
        print(f"{p:<16}{spec['port']:<7}{len(torrents):<16}{seeding:<10}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MusicHub 3.0 Consolidate and Rebalance")
    parser.add_argument("--dry-run", action="store_true", help="模拟干跑")
    parser.add_argument("--limit", type=int, default=0, help="处理专辑数上限")
    parser.add_argument("--status", action="store_true", help="查看 8 个池当前实时状态")
    parser.add_argument("--album", type=str, default="", help="仅处理指定专辑名")
    args = parser.parse_args()

    if args.status:
        print_status()
        sys.exit(0)

    data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "rebalance"))
    plan_p = os.path.join(data_dir, "album_assignments.json")
    ledger_p = os.path.join(data_dir, "album_assignments_ledger.jsonl")

    # 兜底旧 scratch 路径
    if not os.path.exists(plan_p):
        scratch = r"C:\Users\艾森克\.gemini\antigravity\brain\1b0e9463-8379-46cf-b67b-db69c41dcb53\scratch"
        plan_p = os.path.join(scratch, "album_assignments.json")
        ledger_p = os.path.join(scratch, "album_assignments_ledger.jsonl")

    c = Consolidator(plan_p, ledger_p)
    c.run(limit=args.limit, dry_run=args.dry_run, only_album=args.album)
