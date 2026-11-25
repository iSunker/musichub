# -*- coding: utf-8 -*-
"""历史源种回填工具：按 source_tasks 重建池内做种任务（默认 dry-run 只读）

背景
----
2026-11-21 之前的 handoff_task() 从不传 torrent_bytes，源种数据被搬到机械盘后
没有任何目标池任务接管，随后 3016 任务还被 delete_files=True 下架，最终留下
「有文件、无任务」的孤儿（实测 38 条源种 0/38 在集群做种）。新链路只对
「下一个完成」的源种负责，存量必须单独回填。

只读阶段（默认）逐条核四件事
1. 8 池 + 3010 是否已有该 info-hash（已存在就不能再注，单实例铁律）；
2. torrent 字节能不能拿到（data/torrents 归档 → 3016 export → 各池 export）；
3. 物理数据实际落在哪个池（按种子清单逐文件校验存在 + size 一致）；
4. OpenCD 来源是否只在 music3（其它池有数据即判冲突，不搬不注）。

只有显式 `--apply` 才注入：paused=False + skip_checking=True，回读 progress≈1，
成功后写 ingest_log；失败立即撤回池任务（deleteFiles=false）。

用法
----
  python musichub/tools/rebuild_source_seeds.py                 # 只读体检
  python musichub/tools/rebuild_source_seeds.py --limit 5
  python musichub/tools/rebuild_source_seeds.py --apply --hash <infohash>
"""

import argparse
import os
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from musichub.core.qb_client import QBClient
from musichub.storage.handoff import (
    OPENCD_LOCK_POOL,
    OPENCD_LOCK_PORT,
    POOL_CONTAINER_SAVE_PATH,
    SOURCE_SEED_TAG,
    is_opencd_origin,
)
from musichub.storage.pool_pairing import POOL_SPECS, PoolPairingManager

PUBLISH_NAME = "publish"
PUBLISH_PORT = 3010

STATUS_ALREADY = "ALREADY_SEEDING"
STATUS_READY = "READY"
STATUS_NO_TORRENT = "NO_TORRENT"
STATUS_NO_DATA = "NO_DATA"
STATUS_OPENCD_CONFLICT = "OPENCD_CONFLICT"
STATUS_INJECTED = "INJECTED"
STATUS_INJECT_FAILED = "INJECT_FAILED"


def load_source_tasks(db_path: str, limit: int = 0,
                      only_hash: str = "", only_site: str = "") -> List[Dict[str, object]]:
    """只读取出 source_tasks（不建表、不迁移，避免 dry-run 产生副作用）"""
    if not os.path.isfile(db_path):
        raise SystemExit(f"数据库不存在: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT info_hash, source_site, source_tid, source_name, created_at FROM source_tasks"
        where: List[str] = []
        params: List[object] = []
        if only_hash:
            where.append("LOWER(info_hash) = ?")
            params.append(only_hash.strip().lower())
        if only_site:
            where.append("LOWER(source_site) = ?")
            params.append(only_site.strip().lower())
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at ASC"
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def build_archive_index(archive_dir: str) -> Dict[str, bytes]:
    """把 data/torrents 建成 infohash -> 种子字节 的索引

    注意：infohash 不在 .torrent 里以原始字节出现（它是 info 字典的 SHA1），
    必须解析后计算，不能用「字节包含」匹配。
    """
    index: Dict[str, bytes] = {}
    if not os.path.isdir(archive_dir):
        return index
    for fname in sorted(os.listdir(archive_dir)):
        if not fname.endswith(".torrent"):
            continue
        try:
            with open(os.path.join(archive_dir, fname), "rb") as fh:
                data = fh.read()
            _name, _files, _size, ih = PoolPairingManager.parse_torrent(data)
        except Exception:
            continue
        if ih:
            index.setdefault(ih.lower(), data)
    return index


def fetch_from_site(site: str, tid: str, expected_hash: str) -> Tuple[Optional[bytes], str]:
    """按源站 tid 从站点 download.php 取回源种字节，并校验 infohash 一致"""
    from musichub.adapters.sites import (
        CrabPTAdapter, HDKylinAdapter, HDTimeAdapter, LemonHDAdapter, MuXueGeAdapter,
        MusopiaAdapter, PTerClubAdapter, TangPTAdapter, UHDDolbyAdapter, XingwanAdapter,
        OpenCDAdapter,
    )

    mapping = {
        "lemonhd": LemonHDAdapter, "musopia": MusopiaAdapter, "crabpt": CrabPTAdapter,
        "pterclub": PTerClubAdapter, "xingwan": XingwanAdapter, "hdkylin": HDKylinAdapter,
        "tangpt": TangPTAdapter, "muxuege": MuXueGeAdapter, "uhddolby": UHDDolbyAdapter,
        "hdtime": HDTimeAdapter, "opencd": OpenCDAdapter,
    }
    cls = mapping.get((site or "").strip().lower())
    if not cls or not str(tid or "").isdigit():
        return None, "站点未登记或 tid 非数字"
    try:
        data = cls().download_torrent(str(tid))
    except Exception as e:
        return None, f"下载异常: {e}"
    if not data:
        return None, "站点未返回种子"
    try:
        _name, _files, _size, ih = PoolPairingManager.parse_torrent(data)
    except Exception as e:
        return None, f"解析失败: {e}"
    if expected_hash and ih.lower() != expected_hash:
        return None, f"infohash 不一致（站点 {ih[:10]} != 台账 {expected_hash[:10]}）"
    return data, ""


def snapshot_instance(host: str, name: str, port: int) -> Optional[Dict[str, Dict[str, object]]]:
    """读取单实例全部任务；读不到返回 None（fail-closed，不当作空）"""
    client = QBClient(host=host, port=port)
    try:
        rows = client.get_torrents(strict=True) or []
    except Exception:
        return None
    return {
        (r.get("hash") or "").lower(): r
        for r in rows
        if r.get("hash")
    }


class RebuildRunner:
    def __init__(self, db_path: str, archive_dir: str, host: str,
                 verify_timeout: float = 30.0, apply: bool = False,
                 allow_site_fetch: bool = True):
        self.db_path = db_path
        self.archive_dir = archive_dir
        self.host = host
        self.verify_timeout = verify_timeout
        self.apply = apply
        self.allow_site_fetch = allow_site_fetch
        self._archive_index: Optional[Dict[str, bytes]] = None
        self._clients: Dict[int, QBClient] = {}
        self._fetch_ts = 0.0
        # 各池当前任务数（build_snapshot 时一并算出）
        self.loads: Dict[str, int] = {}
        # 只读快照：hash -> (实例名, 端口, 任务实况)
        self.snapshot: Dict[str, Tuple[str, int, Dict[str, object]]] = {}

    def client(self, port: int) -> QBClient:
        client = self._clients.get(port)
        if client is None:
            client = QBClient(host=self.host, port=port)
            self._clients[port] = client
        return client

    def build_snapshot(self) -> List[str]:
        """一次读全 8 池 + 3010，同时算出各池任务数（避免重复拉取）"""
        failed: List[str] = []
        for name, port, _ in POOL_SPECS:
            inst = snapshot_instance(self.host, name, port)
            if inst is None:
                failed.append(f"{name}({port})")
                self.loads[name] = -1
                continue
            self.loads[name] = len(inst)
            for h, row in inst.items():
                self.snapshot.setdefault(h, (name, port, row))
        inst = snapshot_instance(self.host, PUBLISH_NAME, PUBLISH_PORT)
        if inst is None:
            failed.append(f"{PUBLISH_NAME}({PUBLISH_PORT})")
        else:
            for h, row in inst.items():
                self.snapshot.setdefault(h, (PUBLISH_NAME, PUBLISH_PORT, row))
        return failed

    # ------------------------------------------------------------------
    def locate_payload(self, torrent_bytes: bytes) -> Tuple[str, List[Tuple[str, int]], int, List[Tuple[str, str]]]:
        """返回 (种子名, 文件清单, 总大小, [(池名, 'OK'|说明)])"""
        name, files, total_size, _ = PoolPairingManager.parse_torrent(torrent_bytes)
        detail: List[Tuple[str, str]] = []
        for pool_name, _port, host_dir in POOL_SPECS:
            missing, mismatch, sample = 0, 0, ""
            for rel, size in files:
                path = os.path.join(host_dir, *[p for p in rel.split("/") if p])
                if not os.path.isfile(path):
                    missing += 1
                    sample = sample or f"{rel}(缺)"
                elif os.path.getsize(path) != size:
                    mismatch += 1
                    sample = sample or (
                        f"{rel}(期望{size}B/实际{os.path.getsize(path)}B)")
            if not missing and not mismatch:
                detail.append((pool_name, "OK"))
            else:
                detail.append((pool_name, f"缺{missing}/不符{mismatch} 例:{sample}"))
        return name, files, total_size, detail

    def resolve_target(self, origin_site: str, detail: List[Tuple[str, str]],
                       loads: Dict[str, int]) -> Tuple[Optional[str], int, str]:
        """定目标池：8 池平权，取「有数据且任务最少」的承接池"""
        ok_pools = [p for p, msg in detail if msg == "OK"]
        if not ok_pools:
            return None, 0, "所有池都找不到与种子清单一致的实体数据"
        target = min(ok_pools, key=lambda p: loads.get(p, 0))
        port = next(port for name, port, _ in POOL_SPECS if name == target)
        return target, port, f"按负载选池 {loads}"

    # ------------------------------------------------------------------
    def run(self, limit: int = 0, only_hash: str = "", only_site: str = "") -> int:
        tasks = load_source_tasks(self.db_path, limit, only_hash, only_site)
        if not tasks:
            print("source_tasks 无匹配记录")
            return 0

        failed = self.build_snapshot()
        if failed:
            print(f"[FAIL-CLOSED] 以下实例读取失败，禁止任何注入判定：{', '.join(failed)}")
            if self.apply:
                return 2
        loads = self.loads

        counts: Dict[str, int] = {}
        print(f"模式: {'APPLY（会注入）' if self.apply else 'DRY-RUN（只读）'}  "
              f"待核 {len(tasks)} 条  db={self.db_path}\n")
        print(f"{'info_hash':40} {'站点':9} {'状态':16} 说明")
        print("-" * 118)

        for task in tasks:
            h = str(task["info_hash"]).lower()
            site = str(task["source_site"] or "").lower()
            status, note = self._process(h, site, task, loads)
            counts[status] = counts.get(status, 0) + 1
            print(f"{h:40} {site:9} {status:16} {note}")

        print("-" * 118)
        print("汇总: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        return 0

    def resolve_torrent_bytes(self, h: str, site: str,
                              task: Dict[str, object]) -> Tuple[Optional[bytes], str, str]:
        """取源种字节：本地归档 → 各实例 export → 源站 download.php 兜底

        回到站点重下必须校验 infohash 与台账一致，避免拿错版本去喂一份旧数据。
        """
        if self._archive_index is None:
            self._archive_index = build_archive_index(self.archive_dir)
        data = self._archive_index.get(h)
        if data:
            return data, "data/torrents", ""

        data = self.client(PUBLISH_PORT).export_torrent(h)
        if data:
            return data, "3016 export", ""
        for name, port, _ in POOL_SPECS:
            data = self.client(port).export_torrent(h)
            if data:
                return data, f"{name} export", ""

        if not self.allow_site_fetch:
            return None, "", "归档/实例都无此 hash（--no-fetch 已禁止回站下载）"

        tid = str(task.get("source_tid") or "")
        # 站间限流：连续 38 次 download.php 不打间隔容易被 429
        gap = time.time() - self._fetch_ts
        if gap < 1.5:
            time.sleep(1.5 - gap)
        self._fetch_ts = time.time()
        data, err = fetch_from_site(site, tid, h)
        if not data:
            return None, "", f"归档/实例无此 hash，回站下载失败: {err}"
        return data, f"站点 download.php#{tid}", ""

    def _process(self, h: str, site: str, task: Dict[str, object],
                 loads: Dict[str, int]) -> Tuple[str, str]:
        existed = self.snapshot.get(h)
        if existed:
            name, port, row = existed
            state = str(row.get("state") or "")
            progress = float(row.get("progress") or 0.0)
            return STATUS_ALREADY, f"已在 {name}({port}) state={state} progress={progress:.0%}"

        torrent_bytes, source, fetch_note = self.resolve_torrent_bytes(h, site, task)
        if torrent_bytes is None:
            return STATUS_NO_TORRENT, fetch_note

        try:
            title, _files, total_size, detail = self.locate_payload(torrent_bytes)
        except Exception as e:
            return STATUS_NO_TORRENT, f"种子解析失败: {e}"

        ok_pools = [p for p, msg in detail if msg == "OK"]
        if is_opencd_origin(site) and ok_pools and OPENCD_LOCK_POOL not in ok_pools:
            return STATUS_OPENCD_CONFLICT, f"OpenCD 数据只在 {ok_pools}（不在 music3），需人工决策"

        target, port, why = self.resolve_target(site, detail, loads)
        if target is None:
            summary = "; ".join(f"{p}:{m}" for p, m in detail if m != "OK")
            return STATUS_NO_DATA, f"{why} [{summary}]"

        if not self.apply:
            return STATUS_READY, (
                f"{title[:34]} {total_size/1024/1024:.1f}MB 源={source} -> {target}({port})；{why}"
            )

        # ---- 注入 ----
        client = self.client(port)
        tags = f"musichub,{SOURCE_SEED_TAG},{site or 'unknown'}"
        added = client.add_torrent(
            torrent_bytes=torrent_bytes,
            save_path=POOL_CONTAINER_SAVE_PATH,
            is_paused=False,
            is_skip_checking=True,
            tags=tags,
            auto_tmm=False,
        )
        client.resume_torrent(h)
        seeded, progress, state = self._wait_seeded(client, h)
        if not seeded:
            client.delete_torrent(h, delete_files=False)
            self._log(h, task, target, port, "FAILED", progress,
                      f"注入后 {self.verify_timeout:.0f}s 未达 100%（state={state}），已撤回")
            return STATUS_INJECT_FAILED, f"{target}({port}) 未达 100%（state={state}），已撤回任务"

        self._log(h, task, target, port, "SEEDING", progress,
                  f"回填做种（add={added}, state={state}, 源={source}）")
        return STATUS_INJECTED, f"{title[:30]} -> {target}({port}) progress={progress:.0%}"

    def _wait_seeded(self, client: QBClient, h: str) -> Tuple[bool, float, str]:
        deadline = time.time() + self.verify_timeout
        progress, state = 0.0, "unknown"
        while time.time() < deadline:
            try:
                rows = client.get_torrents(hashes=h) or []
            except Exception:
                rows = []
            if rows:
                progress = float(rows[0].get("progress") or 0.0)
                state = str(rows[0].get("state") or "")
                if progress >= 0.999:
                    return True, progress, state
            time.sleep(2.0)
        return False, progress, state

    def _log(self, h: str, task: Dict[str, object], pool: str, port: int,
             status: str, progress: float, reason: str) -> None:
        try:
            from musichub.routing.graph import RoutingGraphManager

            gm = RoutingGraphManager(self.db_path)
            gm.log_ingest(
                info_hash=h,
                source_site=str(task.get("source_site") or ""),
                source_tid=str(task.get("source_tid") or ""),
                source_name=str(task.get("source_name") or ""),
                target_instance=pool,
                target_port=port,
                target_save_path=POOL_CONTAINER_SAVE_PATH,
                host_path=os.path.join(
                    next(d for n, _p, d in POOL_SPECS if n == pool),
                    str(task.get("source_name") or ""),
                ),
                status=status,
                progress=progress,
                reason=reason,
                move_mode="rebuild",
            )
        except Exception as e:
            print(f"  [warn] 写 ingest_log 失败 {h}: {e}")


def main() -> int:
    project_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    parser = argparse.ArgumentParser(description="历史源种回填（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="真正注入（默认只读）")
    parser.add_argument("--hash", default="", help="只处理指定 infohash")
    parser.add_argument("--site", default="", help="只处理指定源站")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 条")
    parser.add_argument("--verify-timeout", type=float, default=30.0)
    parser.add_argument("--no-fetch", action="store_true",
                        help="禁止回源站 download.php 取种子（只用本地归档/实例导出）")
    parser.add_argument("--export-torrent", metavar="INFO_HASH",
                        help="把指定 hash 的种子字节导出到 --out（诊断/补单文件用）")
    parser.add_argument("--out", default="", help="配合 --export-torrent 的输出路径")
    parser.add_argument("--db", default=os.environ.get(
        "MUSICHUB_DB", os.path.join(project_dir, "data", "musichub.db")))
    parser.add_argument("--torrents-dir", default=os.environ.get(
        "MUSICHUB_TORRENT_DIR", os.path.join(project_dir, "data", "torrents")))
    parser.add_argument("--host", default=os.environ.get("QB_HOST", "192.168.0.7"))
    args = parser.parse_args()

    runner = RebuildRunner(
        db_path=args.db,
        archive_dir=args.torrents_dir,
        host=args.host,
        verify_timeout=args.verify_timeout,
        apply=args.apply,
        allow_site_fetch=not args.no_fetch,
    )

    if args.export_torrent:
        h = args.export_torrent.strip().lower()
        tasks = load_source_tasks(args.db, only_hash=h)
        if not tasks:
            print(f"source_tasks 无此 hash: {h}")
            return 1
        data, src, note = runner.resolve_torrent_bytes(
            h, str(tasks[0]["source_site"] or ""), tasks[0])
        if not data:
            print(f"导出失败: {note}")
            return 1
        out = args.out or os.path.join(args.torrents_dir, f"{h}.torrent")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as fh:
            fh.write(data)
        print(f"已导出 {h} -> {out}（源: {src}, {len(data)}B）")
        return 0

    return runner.run(limit=args.limit, only_hash=args.hash, only_site=args.site)


if __name__ == "__main__":
    raise SystemExit(main())
