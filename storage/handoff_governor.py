# -*- coding: utf-8 -*-
"""MusicHub 3.0 / qb3010 达标交棒调度器 (HandoffGovernor)

核心定位与纪律：
1. 【达标多维判据】：
   - 分享率：ratio >= 1.05（用户明确决断阈值）
   - 做种时长：做种时间 >= 2.0 小时 (7200s)
   - 或出种数达标：num_complete - 1 >= 3 (他人出种 >= 3)
   - 或保底超期：做种时间 >= 6.0 小时 (21600s)

2. 【安全双保交棒机制】：
   - 常规库接管判据只接受同 infohash。A 种与目标站 B 种的 infohash 天然不同，
     同名同大小只能用于诊断，绝不能作为下架 3010 当前 B 种的依据。
   - 巡检采用「先预过滤再执行」：只有已就绪任务才占用 max_batch 名额，
     杜绝旧实现中未就绪任务挤掉后续可交棒种子的队头阻塞。
   - 同 hash 副本由发布期配对（storage/pool_pairing.py）提前注入：发种成功且
     3010 保活探针通过后，同一份 B 种以 paused 态写入承载实体数据的音乐池，
     因此交棒器这里的 `hash` 判据天然可命中（不再依赖名称/大小猜测）。
   - 常规库就绪后：
     * 向 3010 发送 stop（优雅汇报 tracker 注销 peer 避免冲突）；
     * 唤醒池内接管任务（配对时是 paused，必须显式 start/resume）；
     * 唤醒失败则保留 3010 任务并放弃本次交棒，避免出现「无人做种」空窗；
     * 安全移除 3010 中的做种任务（delete_files=False，绝对不删磁盘文件）；
     * 安全清理 SSD 发种区软链接或临时缓存副本（严格受限在发种区目录内）。
"""

import os
import time
import json
import logging
import urllib.request
import urllib.parse
from typing import Dict, List, Optional, Tuple, Any

from musichub.storage.pool_pairing import POOL_SPECS

logger = logging.getLogger("HandoffGovernor")


class HandoffGovernor:
    def __init__(
        self,
        qb_publish_host: str = "192.168.0.7",
        qb_publish_port: int = 3010,
        regular_hosts: Optional[List[str]] = None,
        min_ratio: float = 1.05,
        min_seed_hours: float = 2.0,
        min_seeders: int = 3,
        max_seed_hours_fallback: float = 6.0,
        ssd_publish_dir: str = "/volume2/docker_ssd/qbit-all/data/publish/Download",
        graph_manager=None,
        index_ttl: float = 1800.0,
    ):
        self.publish_url = f"http://{qb_publish_host}:{qb_publish_port}"
        # 交棒目标库必须覆盖全部 8 个受管音乐池（music1~3 + music_pool1~5）。
        # 只扫 music1~3 会漏掉配对落在 music_pool_* 的任务，导致 ready 恒为 0。
        self.regular_hosts = regular_hosts or [
            f"http://{qb_publish_host}:{port}" for _name, port, _dir in POOL_SPECS
        ]
        self.min_ratio = min_ratio
        self.min_seed_seconds = min_seed_hours * 3600.0
        self.min_seeders = min_seeders
        self.max_seed_seconds_fallback = max_seed_hours_fallback * 3600.0
        self.ssd_publish_dir = ssd_publish_dir

        # 在册快照（musichub.db）。为空时退化为「每轮全量拉取」的旧行为。
        self.graph_mgr = graph_manager
        self.index_ttl = index_ttl
        # host -> (池名, 端口)：全量刷新时用来把实况写回对应池的快照
        self._host_pool: Dict[str, Tuple[str, int]] = {}
        for name, port, _dir in POOL_SPECS:
            self._host_pool[f"http://{qb_publish_host}:{port}"] = (name, port)
        for host in self.regular_hosts:
            if host not in self._host_pool:
                try:
                    port = int(host.rstrip("/").rsplit(":", 1)[-1])
                except (TypeError, ValueError):
                    continue
                matched = next((n for n, p, _d in POOL_SPECS if p == port), "")
                self._host_pool[host] = (matched or str(port), port)

        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _pool_of_host(self, host: str) -> Tuple[str, int]:
        return self._host_pool.get(host, (host, 0))

    def _api_get(self, base_url: str, endpoint: str) -> Any:
        url = f"{base_url}{endpoint}"
        req = urllib.request.Request(url)
        with self.opener.open(req, timeout=10) as resp:
            data = resp.read().decode("utf-8")
            if data and (data.startswith("{") or data.startswith("[")):
                return json.loads(data)
            return data

    def _api_post(self, base_url: str, endpoint: str, data: dict) -> Any:
        url = f"{base_url}{endpoint}"
        encoded = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=encoded)
        with self.opener.open(req, timeout=10) as resp:
            return resp.read().decode("utf-8")

    def _api_post_ok(self, base_url: str, endpoint: str, data: dict) -> bool:
        """调用 qB POST 并判定是否被接受（qB 失败会返回 Fails. 而非非 200）"""
        try:
            text = self._api_post(base_url, endpoint, data)
        except Exception as e:
            logger.warning(f"POST {endpoint} on {base_url} 失败: {e}")
            return False
        return (text or "").strip() in ("", "Ok.")

    def _task_snapshot(self, base_url: str, info_hash: str) -> Optional[Dict[str, Any]]:
        """按 hash 精确查询单个任务当前状态；查询失败返回 None（区别于任务不存在）"""
        try:
            data = self._api_get(
                base_url,
                f"/api/v2/torrents/info?hashes={urllib.parse.quote(info_hash)}",
            )
        except Exception as e:
            logger.warning(f"查询任务状态失败 {base_url} {info_hash[:8]}: {e}")
            return None
        if isinstance(data, list) and data:
            return data[0]
        return {}

    @staticmethod
    def _is_stopped(task: Optional[Dict[str, Any]]) -> bool:
        state = str((task or {}).get("state", "")).lower()
        return any(s in state for s in ("paused", "stopped"))

    @staticmethod
    def _is_active(task: Optional[Dict[str, Any]]) -> bool:
        if not task:
            return False
        state = str(task.get("state", "")).lower()
        return not any(s in state for s in ("paused", "stopped", "error", "missing"))

    def _stop_publish_task(self, info_hash: str) -> bool:
        """停源并回查状态；qB v5 用 stop、v4 用 pause

        qB 对已进入队列的任务可能返回 `Fails.` 但实际已停源，因此以
        「回查状态确为暂停/停止」为唯一判据；stop 成功时轮询等待状态落地，
        仅在 stop 失败时才回退 pause（qB5 无该接口，404 属预期）。
        """
        ok = self._api_post_ok(self.publish_url, "/api/v2/torrents/stop", {"hashes": info_hash})
        if not ok:
            self._api_post_ok(self.publish_url, "/api/v2/torrents/pause", {"hashes": info_hash})
        deadline = time.time() + 5.0
        while time.time() < deadline:
            time.sleep(1)
            task = self._task_snapshot(self.publish_url, info_hash)
            if task and self._is_stopped(task):
                return True
        return False

    def _resume_task(self, base_url: str, info_hash: str, timeout: float = 8.0) -> bool:
        """唤醒任务并轮询回查状态，杜绝 qB 对不存在 hash 也返回 200 的假成功

        qB 唤醒后状态刷新有秒级延迟（实测 1s 回查仍是 stoppedUP），
        必须轮询等待，否则会把「已唤醒」误判为失败并放弃交棒。
        """
        deadline = time.time() + timeout
        for endpoint in ("/api/v2/torrents/start", "/api/v2/torrents/resume"):
            self._api_post_ok(base_url, endpoint, {"hashes": info_hash})
            while time.time() < deadline:
                time.sleep(1)
                task = self._task_snapshot(base_url, info_hash)
                if self._is_active(task):
                    return True
        return False

    # ------------------------------------------------------------------
    # 批量操作原语：一轮交棒只发常数级 API 调用，避免逐任务串行等待拖慢整轮
    # ------------------------------------------------------------------
    def _bulk_action(self, base_url: str, endpoint: str, hashes: List[str],
                     extra: Optional[dict] = None) -> bool:
        """一次 POST 批量操作多个 hash（qB 用 `|` 分隔）"""
        if not hashes:
            return True
        data = {"hashes": "|".join(hashes)}
        if extra:
            data.update(extra)
        try:
            text = self._api_post(base_url, endpoint, data)
        except Exception as e:
            logger.warning(f"批量 POST {endpoint} on {base_url} 失败: {e}")
            return False
        return (text or "").strip() in ("", "Ok.")

    def _bulk_snapshot(self, base_url: str, hashes: List[str]) -> Dict[str, Dict[str, Any]]:
        """一次查询多个 hash 的状态，返回 {hash: task}"""
        if not hashes:
            return {}
        q = urllib.parse.quote("|".join(hashes))
        try:
            data = self._api_get(base_url, f"/api/v2/torrents/info?hashes={q}") or []
        except Exception as e:
            logger.warning(f"批量查询状态失败 {base_url}: {e}")
            return {}
        return {(t.get("hash") or "").lower(): t for t in data if t.get("hash")}

    def _bulk_wait(self, base_url: str, hashes: List[str], predicate, timeout: float = 6.0) -> set:
        """轮询批量状态直到全部满足 predicate 或超时；返回已满足的 hash 集合"""
        pending = set(h.lower() for h in hashes)
        ok: set = set()
        deadline = time.time() + timeout
        while pending and time.time() < deadline:
            snap = self._bulk_snapshot(base_url, list(pending))
            for h in list(pending):
                if predicate(snap.get(h)):
                    ok.add(h)
                    pending.discard(h)
            if pending:
                time.sleep(0.8)
        return ok

    def get_publish_torrents(self) -> List[Dict[str, Any]]:
        try:
            return self._api_get(self.publish_url, "/api/v2/torrents/info") or []
        except Exception as e:
            logger.error(f"Failed to query 3010 torrents: {e}")
            return []

    def _index_from_snapshot(self) -> Optional[Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]]:
        """用 musichub.db 的在册快照构造索引；任一池快照过期/缺失即放弃（返回 None）

        快照只做「快路缓存」：过期、缺池、读库异常都回退到全量拉取，
        绝不把 DB 当成唯一真相（qB 侧人工删任务/幽灵做种会让库漂移）。
        """
        gm = self.graph_mgr
        if not gm:
            return None
        try:
            meta = gm.get_pool_index_meta()
            now = time.time()
            for host in self.regular_hosts:
                _name, port = self._pool_of_host(host)
                row = meta.get(int(port))
                if not row or (now - float(row.get("synced_at") or 0.0)) > self.index_ttl:
                    return None
            rows = gm.get_pool_index_rows()
        except Exception as e:
            logger.warning(f"[HandoffGovernor] 读取在册快照失败，回退全量拉取: {e}")
            return None

        by_hash: Dict[str, Dict[str, Any]] = {}
        by_name: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            if not int(r.get("is_complete") or 0):
                continue
            port = int(r.get("instance_port") or 0)
            host = next((h for h in self.regular_hosts if self._pool_of_host(h)[1] == port), "")
            info = {
                "host": host,
                "hash": (r.get("info_hash") or "").lower(),
                "name": r.get("name"),
                "size": int(r.get("size") or 0),
                "state": r.get("state"),
                "progress": r.get("progress"),
            }
            if info["hash"]:
                by_hash[info["hash"]] = info
            if info["name"]:
                by_name.setdefault(info["name"], info)
        return by_hash, by_name

    def get_library_index(self) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """获取全部受管音乐池（music1~3 + music_pool1~5）中 100% 任务的索引

        优先读 musichub.db 在册快照（微秒级、零接口开销）；快照过期才全量
        拉取 8 池并回写快照。返回 (by_hash, by_name)：
        - by_hash：info-hash 完全一致，最可靠的「同一份种」判据；
        - by_name：仅用于诊断同名候选；hash 不同一律不得交棒。

        完整判定兼容 amount_left==0：发布期配对注入的是 paused 任务，个别 qB 版本
        skip_checking 后 progress 上报会滞后，只看 progress 会漏掉配对种。
        """
        cached = self._index_from_snapshot()
        if cached is not None:
            return cached
        return self._refresh_library_index()

    def _refresh_library_index(
        self,
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """全量拉取 8 池重建索引，并把差异写回在册快照与入栈/出栈流水"""
        by_hash: Dict[str, Dict[str, Any]] = {}
        by_name: Dict[str, Dict[str, Any]] = {}
        seen_ports: Dict[str, List[str]] = {}
        for host in self.regular_hosts:
            try:
                torrents = self._api_get(host, "/api/v2/torrents/info") or []
            except Exception as e:
                logger.warning(f"Failed to query regular host {host}: {e}")
                continue

            pool_name, pool_port = self._pool_of_host(host)
            if self.graph_mgr:
                try:
                    diff = self.graph_mgr.sync_pool_index(pool_name, pool_port, torrents)
                    for h in diff["added"]:
                        self.graph_mgr.record_pool_event(
                            h, "IN", pool_name=pool_name, pool_port=pool_port,
                            actor="index_sync", reason="全量刷新发现池内新增",
                        )
                    for h in diff["removed"]:
                        self.graph_mgr.record_pool_event(
                            h, "OUT", pool_name=pool_name, pool_port=pool_port,
                            actor="index_sync", reason="全量刷新发现池内消失",
                        )
                except Exception as e:
                    logger.warning(f"[HandoffGovernor] 回写在册快照失败 {pool_name}: {e}")

            for t in torrents:
                info_hash = (t.get("hash") or "").lower()
                if info_hash:
                    seen_ports.setdefault(info_hash, []).append(pool_name)

                progress = float(t.get("progress", 0.0))
                amount_left = t.get("amount_left")
                is_complete = progress >= 1.0 or (
                    amount_left is not None and int(amount_left) == 0 and int(t.get("size") or 0) > 0
                )
                if not is_complete:
                    continue
                info = {
                    "host": host,
                    "hash": info_hash,
                    "name": t.get("name"),
                    "size": int(t.get("size") or 0),
                    "state": t.get("state"),
                    "progress": t.get("progress"),
                }
                if info["hash"]:
                    by_hash[info["hash"]] = info
                if info["name"]:
                    by_name.setdefault(info["name"], info)

        # 同一 hash 出现在多个池 = 违反「同 infohash 全集群单实例」铁律，必须留痕
        if self.graph_mgr:
            for info_hash, pools in seen_ports.items():
                if len(set(pools)) > 1:
                    self.graph_mgr.record_pool_event(
                        info_hash, "CONFLICT", pool_name=",".join(sorted(set(pools))),
                        actor="index_sync",
                        reason=f"同 hash 同时存在于 {len(set(pools))} 个池",
                    )
        return by_hash, by_name

    def reconcile_pool_index(self, dry_run: bool = False) -> Dict[str, Any]:
        """对账：强制全量拉取一次，用实况覆盖在册快照并记录漂移

        每日跑一次即可；平时的 120s 交棒轮次只走快照快路，不再全量拉接口。
        """
        if dry_run:
            return {"dry_run": True, "pools": len(self.regular_hosts)}
        if not self.graph_mgr:
            return {"skipped": "未接入 RoutingGraphManager"}
        self._refresh_library_index()
        meta = self.graph_mgr.get_pool_index_meta()
        return {
            "dry_run": False,
            "pools": len(meta),
            "torrents": sum(int(m.get("torrent_count") or 0) for m in meta.values()),
        }

    def match_library(
        self,
        torrent: Dict[str, Any],
        by_hash: Dict[str, Dict[str, Any]],
        by_name: Dict[str, Dict[str, Any]],
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """在常规库中查找接管者，返回 (match_kind, lib_info)

        match_kind：
          - 'hash'              ：同 infohash，可直接交棒
          - 'name+size'         ：同名且 size 一致，但 hash 不同，仅诊断、绝不交棒
          - 'name_size_mismatch'：同名但 size 不一致，疑似不同版本，仅告警不交棒
          - None                ：常规库完全未收录
        """
        h = (torrent.get("hash") or "").lower()
        if h and h in by_hash:
            return "hash", by_hash[h]
        name = torrent.get("name")
        lib = by_name.get(name) if name else None
        if not lib:
            return None, None
        size = int(torrent.get("size") or 0)
        if size and lib.get("size") and int(lib["size"]) == size:
            return "name+size", lib
        return "name_size_mismatch", lib

    def evaluate_eligible(self, torrent: Dict[str, Any], now: float) -> Tuple[bool, str]:
        """根据多维判据判定 3010 上的种子是否够格交棒"""
        progress = float(torrent.get("progress", 0.0))
        if progress < 1.0:
            return False, f"未完成做种 (progress={progress:.2f})"

        ratio = float(torrent.get("ratio", 0.0))
        num_complete = int(torrent.get("num_complete", 0))
        others_seeding = max(0, num_complete - 1)
        # added_on 是任务加入时间，暂停、缺失附件和从未公告也会被计入；只能采用 qB
        # 实际累计的 seeding_time。字段缺失按 0 处理，保守不交棒。
        seeding_time = max(0.0, float(torrent.get("seeding_time") or 0.0))

        reasons = []
        if ratio >= self.min_ratio and seeding_time >= self.min_seed_seconds:
            reasons.append(f"分享率达标 ({ratio:.2f}>={self.min_ratio:.2f}) 且做种满2h ({seeding_time/3600:.1f}h)")
        elif others_seeding >= self.min_seeders and seeding_time >= self.min_seed_seconds:
            reasons.append(f"他人出种数达标 ({others_seeding}>={self.min_seeders}) 且做种满2h")
        elif seeding_time >= self.max_seed_seconds_fallback:
            reasons.append(f"保底做种时长达标 ({seeding_time/3600:.1f}h>={self.max_seed_seconds_fallback/3600:.1f}h)")

        if reasons:
            return True, " | ".join(reasons)
        return False, f"未满足交棒条件 (Ratio={ratio:.2f}, 时长={seeding_time/3600:.1f}h, 出种={others_seeding})"

    def execute_handoff_one(
        self,
        torrent: Dict[str, Any],
        lib_match: Optional[Dict[str, Any]],
        match_kind: str = "",
        dry_run: bool = True
    ) -> Tuple[bool, str]:
        """对单个「已确认常规库接管」的种子执行交棒

        调用方必须先经 match_library() 预过滤，本方法不再自行判断库就绪，
        避免旧实现的队头阻塞（未就绪任务占满 max_batch 名额）。
        """
        h = torrent.get("hash")
        name = torrent.get("name")
        ratio = float(torrent.get("ratio", 0.0))

        if not lib_match:
            logger.info(f"[{name[:25]}] 常规库暂未检索到接管做种，保持 3010 保种，跳过本次交棒。")
            return False, "常规库未就绪"

        lib_hash = str(lib_match.get("hash") or "").lower()
        if match_kind != "hash" or lib_hash != str(h or "").lower():
            return False, "仅允许同 infohash 交棒；名称/大小匹配不证明目标站 B 种已接管"

        if dry_run:
            return True, (f"[Dry-Run] 满足交棒条件 (Ratio={ratio:.2f})，"
                          f"常规库 {lib_match['host']} 已 100% 做种（{match_kind}），可安全交棒并下架 3010")

        lib_host = (lib_match or {}).get("host", "")
        if not lib_host or not lib_hash:
            return False, "接管任务信息不完整（缺 host/hash），放弃交棒"

        # 2. 优雅停源并回查状态；停不下来就不动池、不下架，避免同 hash 双实例同时公告
        if not self._stop_publish_task(h):
            return False, "3010 停源失败或未确认停止，放弃本次交棒"

        # 3. 唤醒池内接管任务（发布期配对注入时为 paused，必须显式 resume）
        if lib_host:
            if not self._resume_task(lib_host, lib_hash):
                logger.warning(
                    f"池任务 {lib_hash[:8]} 唤醒失败（{lib_host}），恢复 3010 做种并放弃本次交棒"
                )
                if not self._resume_task(self.publish_url, h):
                    logger.error(f"3010 任务 {h[:8]} 恢复做种失败，需人工介入！")
                return False, f"池任务唤醒失败: {lib_host}"

        # 4. 从 3010 安全移除做种任务（delete_files=False，绝对不删磁盘文件）
        try:
            self._api_post(self.publish_url, "/api/v2/torrents/delete", {"hashes": h, "deleteFiles": "false"})
            logger.info(f"Successfully detached task {h[:8]} from 3010 (delete_files=False).")
        except Exception as e:
            return False, f"Failed to delete torrent from 3010: {e}"

        # 4. 安全清理 SSD 副本或软链接（若存在）
        if self.ssd_publish_dir and os.path.exists(self.ssd_publish_dir):
            ssd_payload = os.path.join(self.ssd_publish_dir, name)
            if os.path.islink(ssd_payload):
                try:
                    os.unlink(ssd_payload)
                    logger.info(f"Safely unlinked SSD symlink: {ssd_payload}")
                except Exception as e:
                    logger.warning(f"Failed to unlink {ssd_payload}: {e}")
            elif os.path.isdir(ssd_payload):
                # 如果是真实目录，仅在非系统目录时安全清理
                logger.info(f"SSD payload exists at {ssd_payload}, leaving for lifecycle/safe GC.")

        self._record_handoff_event(torrent, lib_match)
        return True, f"已安全交棒至 {lib_match['host']}"

    def _execute_batch(
        self,
        batch: List[Tuple[Dict[str, Any], str, str, Dict[str, Any]]],
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """批量执行交棒：停源 -> 唤醒 -> 下架，三个阶段各自只发一次批量请求

        逐任务串行版本单任务要 stop 轮询 + resume 轮询 2~13s，一轮 30 个最坏可
        拖到数分钟；批量化后 60 个任务通常 3~5s 内收口。任何阶段失败都会回退
        （3010 恢复做种），绝不留下无人做种的空窗。
        """
        if not batch:
            return 0, []

        results: Dict[str, Dict[str, Any]] = {}
        valid_batch = []
        for t, reason, kind, info in batch:
            h = str(t.get("hash") or "").lower()
            lib_hash = str(info.get("hash") or "").lower()
            identity_ok = kind == "hash" and h and lib_hash == h and bool(info.get("host"))
            results[h] = {
                "hash": t.get("hash"),
                "name": t.get("name"),
                "ratio": float(t.get("ratio", 0.0)),
                "reason": reason,
                "match_kind": kind,
                "handoff_result": "" if identity_ok else "仅允许同 infohash 交棒，已拒绝下架 3010",
                "success": False,
                "_item": (t, info),
            }
            if identity_ok:
                valid_batch.append((t, reason, kind, info))

        batch = valid_batch
        if not batch:
            details = list(results.values())
            for item in details:
                item.pop("_item", None)
            return 0, details

        pub_hashes = [t["hash"] for t, *_ in batch]

        # 阶段 1：批量停源（失败的任务一律不动，避免同 hash 双实例同时公告）
        self._bulk_action(self.publish_url, "/api/v2/torrents/stop", pub_hashes)
        stopped = self._bulk_wait(self.publish_url, pub_hashes, self._is_stopped, timeout=6.0)
        for h, item in results.items():
            if h not in stopped:
                item["handoff_result"] = "3010 停源失败或未确认停止，放弃本次交棒"

        pending = [(t, reason, kind, info) for t, reason, kind, info in batch
                   if t["hash"].lower() in stopped]
        if not pending:
            return 0, list(results.values())

        # 阶段 2：按池 host 分组批量唤醒（配对注入的池任务初始为 paused）
        groups: Dict[str, List[Tuple[Dict[str, Any], str, str, Dict[str, Any]]]] = {}
        for entry in pending:
            lib_host = entry[3].get("host") or ""
            groups.setdefault(lib_host, []).append(entry)

        resumed_lib: set = set()
        for host, entries in groups.items():
            if not host:
                continue
            lib_hashes = [e[3].get("hash") for e in entries if e[3].get("hash")]
            self._bulk_action(host, "/api/v2/torrents/start", lib_hashes)
            resumed_lib |= self._bulk_wait(host, lib_hashes, self._is_active, timeout=6.0)

        # 阶段 3：唤醒失败的恢复 3010 做种，成功的批量下架
        failed_pub: List[str] = []
        success_items: List[Tuple[Dict[str, Any], str, str, Dict[str, Any]]] = []
        for t, reason, kind, info in pending:
            lib_hash = (info.get("hash") or "").lower()
            if lib_hash and lib_hash in resumed_lib:
                success_items.append((t, reason, kind, info))
            else:
                failed_pub.append(t["hash"])
                results[t["hash"].lower()]["handoff_result"] = (
                    f"池任务唤醒失败({info.get('host')})，已恢复 3010 做种并放弃本次交棒"
                )

        if failed_pub:
            self._bulk_action(self.publish_url, "/api/v2/torrents/start", failed_pub)

        if success_items:
            ok_hashes = [t["hash"] for t, *_ in success_items]
            self._bulk_action(
                self.publish_url, "/api/v2/torrents/delete", ok_hashes,
                extra={"deleteFiles": "false"},
            )
            for t, _r, _k, info in success_items:
                logger.info(f"Successfully handed off {t['hash'][:8]} -> {info.get('host')} (delete_files=False).")
                results[t["hash"].lower()]["success"] = True
                results[t["hash"].lower()]["handoff_result"] = f"已安全交棒至 {info.get('host')}"
                self._record_handoff_event(t, info)
                # 清理 SSD 发种区软链（严格限制在发种区目录内）
                self._clean_ssd_payload(t.get("name") or "")

        details = []
        for item in results.values():
            item.pop("_item", None)
            details.append(item)
        return sum(1 for d in details if d["success"]), details

    def _record_handoff_event(self, torrent: Dict[str, Any], lib_info: Dict[str, Any]) -> None:
        """交棒成功即记一条出栈流水（3010 下架、池任务转正做种）"""
        gm = self.graph_mgr
        if not gm:
            return
        pool_name, pool_port = self._pool_of_host(lib_info.get("host") or "")
        try:
            gm.record_pool_event(
                torrent.get("hash") or "",
                "HANDOFF",
                pool_name=pool_name,
                pool_port=pool_port,
                name=torrent.get("name") or "",
                size=int(torrent.get("size") or 0),
                save_path="/downloads",
                actor="handoff_governor",
                reason="match_kind=hash；同 infohash 池任务已唤醒，3010 B 种下架 (delete_files=false)",
            )
        except Exception as e:
            logger.warning(f"[HandoffGovernor] 写交棒流水失败（不影响交棒）: {e}")

    def _clean_ssd_payload(self, name: str) -> None:
        """清理 SSD 发种区对应的软链接；真实目录留给 lifecycle GC，绝不在此递归删除"""
        if not name or not self.ssd_publish_dir or not os.path.exists(self.ssd_publish_dir):
            return
        ssd_payload = os.path.join(self.ssd_publish_dir, name)
        if os.path.islink(ssd_payload):
            try:
                os.unlink(ssd_payload)
                logger.info(f"Safely unlinked SSD symlink: {ssd_payload}")
            except Exception as e:
                logger.warning(f"Failed to unlink {ssd_payload}: {e}")

    def run_handoff_round(self, dry_run: bool = True, max_batch: int = 60) -> Dict[str, Any]:
        """执行一轮达标交棒巡检

        关键改造（2026-11-21）：
        1. 先按 match_library() 预过滤，只对「常规库已接管」的任务执行，
           消除旧实现中「未就绪任务占满 max_batch 名额」的队头阻塞；
        2. 同名但 size 不一致判为不同版本，仅统计告警，不下架 3010；
        3. 真跑走批量流水线（_execute_batch），一轮只发常数级 API 调用；
        4. 结果新增 ready_count / skipped_no_lib / skipped_size_mismatch / truncated。
        """
        now = time.time()
        torrents = self.get_publish_torrents()
        if not torrents:
            return {"status": "no_torrents", "total_3010": 0, "handed_off_count": 0}

        by_hash, by_name = self.get_library_index()

        eligible_items: List[Tuple[Dict[str, Any], str]] = []
        ready_items: List[Tuple[Dict[str, Any], str, str, Dict[str, Any]]] = []
        skipped_no_lib = 0
        skipped_size_mismatch: List[Dict[str, Any]] = []

        for t in torrents:
            ok, reason = self.evaluate_eligible(t, now)
            if not ok:
                continue
            eligible_items.append((t, reason))
            kind, info = self.match_library(t, by_hash, by_name)
            if kind == "hash":
                ready_items.append((t, reason, kind, info))
            elif kind == "name_size_mismatch":
                skipped_size_mismatch.append({
                    "name": t.get("name"),
                    "lib_host": (info or {}).get("host", ""),
                    "reason": "同名但 size 不一致，疑似不同版本",
                })
            else:
                skipped_no_lib += 1

        batch = ready_items[:max_batch]

        if dry_run:
            details = [{
                "hash": t.get("hash"),
                "name": t.get("name"),
                "ratio": float(t.get("ratio", 0.0)),
                "reason": reason,
                "match_kind": kind,
                "handoff_result": (f"[Dry-Run] 常规库 {info['host']} 已 100% 做种（{kind}），可安全交棒并下架 3010"),
                "success": True,
            } for t, reason, kind, info in batch]
            handed_off_count = len(batch)
        else:
            handed_off_count, details = self._execute_batch(batch)

        for it in skipped_size_mismatch[:5]:
            logger.warning(f"[HandoffGovernor] 跳过同名不同版本: {str(it['name'])[:40]} (常规库 {it['lib_host']})")

        return {
            "status": "success",
            "total_3010": len(torrents),
            "eligible_count": len(eligible_items),
            "ready_count": len(ready_items),
            "handed_off_count": handed_off_count,
            "skipped_no_lib": skipped_no_lib,
            "skipped_size_mismatch": len(skipped_size_mismatch),
            "truncated": max(0, len(ready_items) - len(batch)),
            "dry_run": dry_run,
            "details": details,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    gov = HandoffGovernor()
    print("Testing HandoffGovernor dry-run...")
    res = gov.run_handoff_round(dry_run=True, max_batch=30)
    print(f"Total 3010 Torrents: {res['total_3010']}, Eligible: {res['eligible_count']}, "
          f"Ready: {res.get('ready_count')}, Handed Off: {res['handed_off_count']}, "
          f"NoLib: {res.get('skipped_no_lib')}, SizeMismatch: {res.get('skipped_size_mismatch')}")
    for d in res['details']:
        print(f" - [{d['hash'][:8]}] {d['name'][:25]} | Ratio: {d['ratio']:.2f} | "
              f"{d.get('match_kind')} | {d['reason']} -> {d['handoff_result']}")
