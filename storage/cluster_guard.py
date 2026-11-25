# -*- coding: utf-8 -*-
"""MusicHub 3.0 音乐矩阵「同 info-hash 全集群单实例」守卫 (ClusterDeduper)

背景（2026-11-21 实盘核查，见 docs/nas_cluster/MusicHub多处做种告警与陈旧占位复盘-20261121.md）
--------------------------------------------------------------------------------
站点在 tracker 回包里给出 `You cannot seed the same torrent in the same location
from more than 1 client.` 时，有两种完全不同的成因，必须先分开再动手：

1. **真重复**：同一 info-hash 同时存在于两个 qB 实例、且两侧都在做种（active）。
   ⇒ 这是我们自己的编排问题，与 `site-reseed` 2026-11-12 事故同源，必须立刻收敛。
2. **陈旧占位（stale seat）**：集群内只有一个持有者，站点侧却仍留着我们上一轮
   会话（peer_id）的 peer 行。qB 每次重启都换 peer_id，站点在旧行被清理之前
   会一直拒收新会话 ⇒ 站点报"多处做种"，实际是我们自己的幽灵。
   实测：musopia 站点 peer 表里 iSunker 只有 1 行、最近汇报时间恰好等于重启时刻，
   本集群 26 个实例内该 hash 无第二份 ⇒ 判定 stale_seat，本地无从清理，只能等
   站点清理 TTL，并追究"为何重启"。

本模块只做三件事，全部可 dry-run：
- `scan()`   全实例快照 → 按 hash 分组 → 分类 live_duplicate / shadow_pair / all_paused；
- `plan()`   对 live_duplicate 选「保留者」，其余判为待下架（deleteFiles 恒 False）；
- `probe_seats()` 采样 tracker 回包，把站点告警分类成 real_duplicate / stale_seat。

安全铁律（与 AGENTS.md 一致）：
- 任何删除动作 deleteFiles 恒为 False，绝不级联删数据；
- 任一实例读取失败即整体放弃（fail-closed），不允许"读不到就当没有"；
- paused 影子种（交棒机制）永不参与"重复"判定，不清理；
- music5 一律不进目标集合。
"""

import os
import time
import logging
from typing import Any, Dict, List, Optional, Tuple

from musichub.core.qb_client import QBClient
from musichub.storage.pool_pairing import POOL_SPECS, PoolSpec

logger = logging.getLogger("ClusterGuard")

# 音乐矩阵受管实例：8 个保种池 + 发种端 3010
PUBLISH_NAME = "publish"
PUBLISH_PORT = 3010

# 可选纳管（跨实例重复会同时触发站点告警，默认不扫，需要时 --include-extra）
EXTRA_INSTANCES: List[Tuple[str, int]] = [
    ("others", 3180),
    ("reseed1", 3320),
    ("reseed2", 3330),
    ("reseed3", 3340),
    ("reseed4", 3350),
    ("reseed5", 3360),
    ("reseed6", 3370),
    ("reseed7", 3380),
]

FORBIDDEN_PATH_MARKERS = ("music5",)

# 站点"同账号同地点多处做种"类告警文案（NexusPHP 各语言/各分支说法）
SEAT_MESSAGE_MARKERS = (
    "more than 1 client",
    "same location",
    "only 1 seeding from one location",
    "同一个种子",
    "多处做种",
    "多个客户端",
)

KIND_LIVE_DUPLICATE = "live_duplicate"
KIND_SHADOW_PAIR = "shadow_pair"
KIND_ALL_PAUSED = "all_paused"

SEAT_REAL = "real_duplicate"
SEAT_STALE = "stale_seat"


def is_paused_state(state: Any) -> bool:
    """qB 状态是否「未向 tracker 公告」（paused/stopped 两种写法兼容）"""
    s = str(state or "").lower()
    return ("paused" in s) or ("stopped" in s)


def _is_complete(torrent: Dict[str, Any]) -> bool:
    try:
        if float(torrent.get("progress") or 0.0) >= 0.999:
            return True
    except (TypeError, ValueError):
        pass
    try:
        amount_left = torrent.get("amount_left")
        if amount_left is not None and int(amount_left) == 0 and int(torrent.get("size") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


class InstanceSnapshot:
    """单个实例的一次任务清单快照"""

    def __init__(self, name: str, port: int, host_dir: str = "", host: str = "192.168.0.7"):
        self.name = name
        self.port = int(port)
        self.host_dir = host_dir
        self.host = host
        self.torrents: List[Dict[str, Any]] = []
        self.error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def __repr__(self) -> str:
        return f"Snapshot({self.name},{self.port},n={len(self.torrents)},err={self.error!r})"


class ClusterDeduper:
    """音乐池矩阵的同 hash 单实例审计器 / 冲突收敛器"""

    def __init__(
        self,
        host: str = "192.168.0.7",
        specs: Optional[List[PoolSpec]] = None,
        include_publish: bool = True,
        include_extra: bool = False,
        qb_factory=None,
    ):
        self.host = host
        base: List[PoolSpec] = specs or [
            PoolSpec(name=name, port=port, host_dir=host_dir, host=host)
            for name, port, host_dir in POOL_SPECS
        ]
        self.specs = list(base)
        known_ports = {s.port for s in self.specs}
        # 按端口去重后追加：调用方可能已经显式传入 publish / 额外实例
        if include_publish and PUBLISH_PORT not in known_ports:
            self.specs.append(PoolSpec(name=PUBLISH_NAME, port=PUBLISH_PORT, host_dir="", host=host))
            known_ports.add(PUBLISH_PORT)
        if include_extra:
            for name, port in EXTRA_INSTANCES:
                if port not in known_ports:
                    self.specs.append(PoolSpec(name=name, port=port, host_dir="", host=host))
                    known_ports.add(port)
        self._factory = qb_factory or (lambda h, p: QBClient(host=h, port=p))
        self._clients: Dict[int, QBClient] = {}

    # ------------------------------------------------------------------ 读
    def client_for(self, spec: PoolSpec) -> QBClient:
        client = self._clients.get(spec.port)
        if client is None:
            client = self._factory(spec.host, spec.port)
            self._clients[spec.port] = client
        return client

    def collect(self) -> List[InstanceSnapshot]:
        """全实例任务清单快照（strict 读取：任一池失败即标记 error，由调用方 fail-closed）"""
        snaps: List[InstanceSnapshot] = []
        for spec in self.specs:
            snap = InstanceSnapshot(spec.name, spec.port, spec.host_dir, spec.host)
            try:
                snap.torrents = self.client_for(spec).get_torrents(strict=True) or []
            except Exception as e:  # 读不到 ≠ 没有：必须显式失败
                snap.error = str(e)
                logger.warning(f"[ClusterGuard] 读取 {spec.name}({spec.port}) 失败: {e}")
            snaps.append(snap)
        return snaps

    # -------------------------------------------------------------- 分类
    @staticmethod
    def _holders(snaps: List[InstanceSnapshot]) -> Dict[str, List[Dict[str, Any]]]:
        by_hash: Dict[str, List[Dict[str, Any]]] = {}
        for snap in snaps:
            for t in snap.torrents:
                h = str(t.get("hash") or "").lower()
                if not h:
                    continue
                by_hash.setdefault(h, []).append({
                    "pool": snap.name,
                    "port": snap.port,
                    "host": snap.host,
                    "host_dir": snap.host_dir,
                    "name": t.get("name") or "",
                    "size": int(t.get("size") or 0),
                    "state": t.get("state") or "",
                    "progress": float(t.get("progress") or 0.0),
                    "paused": is_paused_state(t.get("state")),
                    "complete": _is_complete(t),
                    "uploaded": int(t.get("uploaded") or 0),
                    "added_on": float(t.get("added_on") or 0.0),
                    "save_path": t.get("save_path") or "",
                    "tags": t.get("tags") or "",
                    "category": t.get("category") or "",
                })
        return by_hash

    @staticmethod
    def classify(holders: List[Dict[str, Any]]) -> str:
        """按持有者状态分类：只有「≥2 个都在做种」才算真重复"""
        active = [h for h in holders if not h["paused"]]
        if len(active) >= 2:
            return KIND_LIVE_DUPLICATE
        if len(active) == 1:
            return KIND_SHADOW_PAIR
        return KIND_ALL_PAUSED

    def scan(self) -> Dict[str, Any]:
        """全实例扫描，返回分组结果（不写任何东西）"""
        snaps = self.collect()
        failed = [s.name for s in snaps if not s.ok]
        by_hash = self._holders(snaps)

        groups: List[Dict[str, Any]] = []
        by_kind: Dict[str, int] = {KIND_LIVE_DUPLICATE: 0, KIND_SHADOW_PAIR: 0, KIND_ALL_PAUSED: 0}
        for h, holders in by_hash.items():
            if len(holders) < 2:
                continue
            kind = self.classify(holders)
            by_kind[kind] = by_kind.get(kind, 0) + 1
            groups.append({
                "hash": h,
                "kind": kind,
                "name": holders[0]["name"],
                "size": holders[0]["size"],
                "holders": holders,
            })

        return {
            "scanned_at": time.time(),
            "instances": {s.name: {"port": s.port, "count": len(s.torrents), "error": s.error}
                          for s in snaps},
            "failed_instances": failed,
            "total_tasks": sum(len(s.torrents) for s in snaps),
            "unique_hashes": len(by_hash),
            "duplicate_hashes": len(groups),
            "by_kind": by_kind,
            "groups": groups,
            "snapshots": snaps,
            "holders_index": by_hash,
        }

    # ------------------------------------------------------------ 冲突收敛
    @staticmethod
    def _owner_rank(h: Dict[str, Any]) -> Tuple:
        """保留者优先级（越大越该保留）

        1. 必须完整（不完整的那份本来就是废的）；
        2. 优先保留音乐池而非 3010（池是稳态，3010 只是过渡）；
        3. 池目录真实存在（能找到实体数据的池，交棒后不会幽灵做种）；
        4. 上传量更大（真实承载过流量的一方）；
        5. 加入更早（原始归属）。
        """
        pool_dir_exists = False
        if h.get("host_dir") and h.get("name"):
            pool_dir_exists = os.path.isdir(os.path.join(h["host_dir"], str(h["name"])))
        return (
            1 if h["complete"] else 0,
            0 if h["pool"] == PUBLISH_NAME else 1,
            1 if pool_dir_exists else 0,
            int(h["uploaded"]),
            -float(h["added_on"] or 0.0),
        )

    def plan(self, scan_result: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """只为 live_duplicate 生成收敛计划；shadow_pair（交棒机制）一律不动"""
        scan_result = scan_result or self.scan()
        actions: List[Dict[str, Any]] = []
        for g in scan_result["groups"]:
            if g["kind"] != KIND_LIVE_DUPLICATE:
                continue
            holders = sorted(g["holders"], key=self._owner_rank, reverse=True)
            owner, losers = holders[0], holders[1:]
            actions.append({
                "hash": g["hash"],
                "name": g["name"],
                "size": g["size"],
                "owner": {"pool": owner["pool"], "port": owner["port"],
                          "state": owner["state"], "uploaded": owner["uploaded"]},
                "losers": [{"pool": x["pool"], "port": x["port"], "state": x["state"],
                            "progress": x["progress"], "uploaded": x["uploaded"],
                            "tags": x["tags"]} for x in losers],
            })
        return actions

    def apply(self, actions: List[Dict[str, Any]], graph_manager=None,
              delete_files: bool = False) -> Dict[str, Any]:
        """执行收敛：先 stop 再 delete（deleteFiles 恒 False），逐条回查落地

        delete_files 形参保留但**强制 False**：任何调用都不得删数据文件。
        返回 {ok, failed, details}，调用方负责日志与退出码。
        """
        if delete_files:
            raise ValueError("拒绝执行：ClusterDeduper 永远不允许 deleteFiles=True")

        details: List[Dict[str, Any]] = []
        ok_cnt = 0
        for act in actions:
            h = act["hash"]
            row = {"hash": h, "name": act["name"], "owner": act["owner"], "removed": [], "errors": []}
            for loser in act["losers"]:
                spec = next((s for s in self.specs if s.port == int(loser["port"])), None)
                if spec is None:
                    row["errors"].append(f"{loser['pool']}: 未登记实例，跳过")
                    continue
                client = self.client_for(spec)
                try:
                    client.stop_torrent(h)
                except Exception as e:
                    logger.warning(f"[ClusterGuard] {loser['pool']} stop {h[:8]} 异常: {e}")
                deleted = False
                try:
                    deleted = bool(client.delete_torrent(h, delete_files=False))
                except Exception as e:
                    row["errors"].append(f"{loser['pool']}: delete 异常 {e}")
                    continue
                gone = False
                try:
                    gone = not (client.get_torrents(hashes=h, strict=True) or [])
                except Exception as e:
                    row["errors"].append(f"{loser['pool']}: 回查失败 {e}")
                if deleted and gone:
                    row["removed"].append(loser["pool"])
                    logger.info(f"[ClusterGuard] 已下架重复份 {h[:8]} @ {loser['pool']}（deleteFiles=false）")
                    if graph_manager:
                        try:
                            graph_manager.record_pool_event(
                                h, "CONFLICT", pool_name=loser["pool"], pool_port=loser["port"],
                                name=act["name"], size=act["size"],
                                actor="cluster_guard",
                                reason=f"同 hash 双活，保留 {act['owner']['pool']}；本份已停源下架",
                            )
                            graph_manager.record_pool_event(
                                h, "OUT", pool_name=loser["pool"], pool_port=loser["port"],
                                name=act["name"], size=act["size"],
                                actor="cluster_guard", reason="冲突收敛下架",
                            )
                        except Exception as e:
                            logger.warning(f"[ClusterGuard] 写 CONFLICT 流水失败（不影响收敛）: {e}")
                else:
                    row["errors"].append(f"{loser['pool']}: 下架未确认（deleted={deleted}, gone={gone}）")
            if row["removed"] and not row["errors"]:
                ok_cnt += 1
            details.append(row)

        return {"ok": ok_cnt, "total": len(actions),
                "failed": [d for d in details if d["errors"]], "details": details}

    # ------------------------------------------------------- 站点告警分诊
    def probe_seats(
        self,
        scan_result: Optional[Dict[str, Any]] = None,
        per_instance_limit: int = 0,
        only_hashes: Optional[List[str]] = None,
        markers: Tuple[str, ...] = SEAT_MESSAGE_MARKERS,
    ) -> Dict[str, Any]:
        """采样 tracker 回包，把"多处做种"告警分成 real_duplicate / stale_seat

        - real_duplicate：该 hash 在集群内确实双活（由 scan 结果判定），必须收敛；
        - stale_seat：集群内只有一份，站点仍留旧 peer 行（重启换 peer_id 导致），
          本地无从清理，只能等站点 TTL 或联系管理组。
        """
        scan_result = scan_result or self.scan()
        live_hashes = {g["hash"] for g in scan_result["groups"] if g["kind"] == KIND_LIVE_DUPLICATE}
        only = {h.lower() for h in (only_hashes or []) if h}

        result: Dict[str, Any] = {
            "checked": 0, "seat_hits": 0, "by_domain": {}, "by_kind": {SEAT_REAL: 0, SEAT_STALE: 0},
            "hits": [], "errors": [],
        }
        for snap in scan_result["snapshots"]:
            if only:
                picked = [t for t in snap.torrents if str(t.get("hash") or "").lower() in only]
            else:
                picked = list(snap.torrents)
                if per_instance_limit and len(picked) > per_instance_limit:
                    picked = picked[:per_instance_limit]
            if not picked:
                continue
            client = self.client_for(next(s for s in self.specs if s.port == snap.port))
            for t in picked:
                h = str(t.get("hash") or "").lower()
                if not h:
                    continue
                try:
                    trackers = client.get_trackers(h) or []
                except Exception as e:
                    result["errors"].append(f"{snap.name}/{h[:8]}: {e}")
                    continue
                result["checked"] += 1
                for tr in trackers:
                    msg = str(tr.get("msg") or "")
                    if not any(m in msg for m in markers):
                        continue
                    domain = _tracker_domain(tr.get("url") or "")
                    kind = SEAT_REAL if h in live_hashes else SEAT_STALE
                    result["seat_hits"] += 1
                    result["by_domain"][domain] = result["by_domain"].get(domain, 0) + 1
                    result["by_kind"][kind] = result["by_kind"].get(kind, 0) + 1
                    result["hits"].append({
                        "hash": h, "name": t.get("name") or "", "pool": snap.name,
                        "port": snap.port, "state": t.get("state") or "",
                        "domain": domain, "msg": msg, "kind": kind,
                    })
        return result


def _tracker_domain(url: str) -> str:
    """从 announce URL 提取域名；DHT/PeX/LSD 之类的伪 tracker 返回空串"""
    if not url or url.startswith("**"):
        return ""
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc or url[:40]
    except Exception:
        return url[:40]


def summarize_scan(scan_result: Dict[str, Any]) -> str:
    """一行摘要，供 daemon 周期日志使用"""
    kinds = scan_result.get("by_kind", {})
    return (
        f"pools={len(scan_result.get('instances', {}))} tasks={scan_result.get('total_tasks')} "
        f"dup={scan_result.get('duplicate_hashes')} "
        f"live={kinds.get(KIND_LIVE_DUPLICATE, 0)} shadow={kinds.get(KIND_SHADOW_PAIR, 0)} "
        f"paused={kinds.get(KIND_ALL_PAUSED, 0)} failed={scan_result.get('failed_instances')}"
    )
