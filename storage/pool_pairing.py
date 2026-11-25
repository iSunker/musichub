# -*- coding: utf-8 -*-
"""MusicHub 3.0 发布期音乐池配对器 (PoolPairingManager)

一、要解决的问题
----------------
发布到外站的 B 种（含各站 tracker/passkey）此前只回加 3010 (publish) 做种，
音乐保种池 music1/music2/music3/music_pool_1~5 里只有 OpenCD 的 A 种。
A 种与 B 种的 info-hash 天然不同（tracker/source 不同），名称与总大小也可能
不同，因此交棒器 HandoffGovernor.match_library() 的 `hash` 与 `name+size`
两条判据恒不命中，3010 上的达标任务永远交不出去。

二、解法
--------
发布成功并且 3010 保活探针确认 100% 做种后，把「同一份 B 种字节」再以
**暂停态**注入到物理承载该专辑的保种池实例：

1. 池内因此出现与 3010 完全一致的 info-hash，交棒器命中 `hash` 判据；
2. 注入 `paused=True + skip_checking=True`，不会向 tracker 公告，
   避免同 hash 在 3010 与池内同时做种触发站点重复客户端告警；
3. 交棒时只需「resume 池任务 + 删除 3010 任务」，不再依赖名称/大小猜测；
4. 注入前按种子文件的相对路径与 size 做物理硬校验；池内目录缺失或不一致时，
   仅在**目标不存在**的前提下用同卷硬链接补齐（零额外空间），绝不复写已有数据。

三、数据路径约定
----------------
torf 生成种子时 `name == basename(local_path)`，且 qB 池实例的 `save_path`
恒为 `/downloads`（映射到该池的宿主数据根）。因此种子内文件相对路径
（如 `Album/CD1/01.flac`）直接拼到宿主池根目录即可命中实体文件。
"""

import io
import os
import time
import logging
from typing import Dict, List, Optional, Tuple

from musichub.core.qb_client import QBClient

logger = logging.getLogger("PoolPairing")

# 铁律：music5 为用户专属保种区，任何配对/清理动作都不得纳入。
FORBIDDEN_PATH_MARKERS = ("music5",)

# 2026-11-23 架构重构：8 实例完全平权，清空排除集合
EXCLUDED_LOAD_POOLS = frozenset()

# (池名, WebUI 端口, 宿主数据根目录) - 规范化 8 池严格升序对齐
POOL_SPECS: List[Tuple[str, int, str]] = [
    ("music_pool_1", 3210, "/volume1/video/download/music/music_pool_1"),
    ("music_pool_2", 3220, "/volume1/video/download/music/music_pool_2"),
    ("music_pool_3", 3230, "/volume1/video/download/music/music_pool_3"),
    ("music_pool_4", 3240, "/volume1/video/download/music/music_pool_4"),
    ("music_pool_5", 3250, "/volume1/video/download/music/music_pool_5"),
    ("music_pool_6", 3260, "/volume1/video/download/music/music_pool_6"),
    ("music_pool_7", 3270, "/volume1/video/download/music/music_pool_7"),
    ("music_pool_8", 3280, "/volume1/video/download/music/music_pool_8"),
]


class PoolSpec:
    """单个音乐保种池的实例与数据根描述"""

    def __init__(self, name: str, port: int, host_dir: str, host: str = "192.168.0.7"):
        self.name = name
        self.port = port
        self.host_dir = host_dir
        self.host = host

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def __repr__(self) -> str:
        return f"PoolSpec({self.name}, {self.port}, {self.host_dir})"


class PoolPairingManager:
    """把「发布到外站的 B 种」同步配对进物理承载它的音乐保种池"""

    def __init__(
        self,
        publish_host: str = "192.168.0.7",
        specs: Optional[List[PoolSpec]] = None,
        qb_factory=None,
        verify_timeout: float = 20.0,
        poll_interval: float = 2.0,
        graph_manager=None,
    ):
        self.publish_host = publish_host
        # 可选：入栈流水与在册快照的写入方（RoutingGraphManager）。为空时只做配对不记账。
        self.graph_manager = graph_manager
        self.specs: List[PoolSpec] = specs or [
            PoolSpec(name=name, port=port, host_dir=host_dir, host=publish_host)
            for name, port, host_dir in POOL_SPECS
        ]
        self.verify_timeout = verify_timeout
        self.poll_interval = poll_interval
        self._factory = qb_factory or (
            lambda host, port: QBClient(host=host, port=port)
        )
        self._clients: Dict[int, QBClient] = {}
        # 选池用的池清单快照缓存：批量配对时避免每次都拉 7 池全量任务
        self._snap_cache: Optional[Dict[str, List[Dict[str, object]]]] = None
        self._snap_ts: float = 0.0
        # 查重用的全池快照（含 music3），仅用于 find_hash_pool()
        self._snap_all_cache: Optional[Dict[str, List[Dict[str, object]]]] = None
        self._snap_all_ts: float = 0.0
        self.snapshot_ttl: float = 60.0

    def pool_for_path(self, local_path: str) -> Optional[PoolSpec]:
        """根据物理路径判定其归属的保种池"""
        return self.find_owning_pool(local_path)

    # ------------------------------------------------------------------ qB
    def client_for(self, spec: PoolSpec) -> QBClient:
        client = self._clients.get(spec.port)
        if client is None:
            client = self._factory(spec.host, spec.port)
            self._clients[spec.port] = client
        return client

    # -------------------------------------------------------------- 池判定
    def find_owning_pool(self, local_path: str) -> Optional[PoolSpec]:
        """返回物理承载 local_path 的受控池；路径含 music5 或不在池内返回 None"""
        if not local_path:
            return None

        real = os.path.realpath(local_path).replace("\\", "/")
        parts = [p for p in real.split("/") if p]
        for marker in FORBIDDEN_PATH_MARKERS:
            if marker in parts:
                logger.warning(f"[PoolPairing] 拒绝处理禁区路径: {local_path}")
                return None

        best: Optional[Tuple[int, PoolSpec]] = None
        for spec in self.specs:
            root = os.path.realpath(spec.host_dir).replace("\\", "/").rstrip("/")
            if real == root or real.startswith(root + "/"):
                depth = len(root)
                if best is None or depth > best[0]:
                    best = (depth, spec)
        return best[1] if best else None

    # ---------------------------------------------------------------- 选池
    def get_pool_snapshot(self, force: bool = False,
                          include_excluded: bool = False) -> Dict[str, List[Dict[str, object]]]:
        """一次拉取池内任务清单，带短 TTL 缓存

        include_excluded=False（默认）只拉「承接池」，用于负载均衡选池；
        include_excluded=True 额外包含 music3 等不承接新负载的池，**只用于
        同 info-hash 查重**——否则 music3 里已有的同 hash 会被漏判，
        再往别的池注入就变成跨实例重复（违反单实例铁律）。
        """
        now = time.time()
        cache = self._snap_all_cache if include_excluded else self._snap_cache
        cache_ts = self._snap_all_ts if include_excluded else self._snap_ts
        if not force and cache is not None and now - cache_ts < self.snapshot_ttl:
            return cache

        snap: Dict[str, List[Dict[str, object]]] = {}
        for spec in self.specs:
            if not include_excluded and spec.name in EXCLUDED_LOAD_POOLS:
                continue
            try:
                snap[spec.name] = self.client_for(spec).get_torrents(strict=False) or []
            except Exception as e:
                logger.warning(f"[PoolPairing] 读取 {spec.name} 任务清单失败: {e}")
        if include_excluded:
            self._snap_all_cache = snap
            self._snap_all_ts = now
        else:
            self._snap_cache = snap
            self._snap_ts = now
        return snap

    def get_pool_loads(self) -> Dict[str, int]:
        return {name: len(tasks) for name, tasks in self.get_pool_snapshot().items()}

    def find_hash_pool(self, info_hash: str) -> Optional[PoolSpec]:
        """全池（含 music3）查同一 info-hash 的既有持有者

        这是「同 info-hash 全集群单实例」铁律在配对链路上的落点：只要任一受管池
        已有该 hash，就复用该池，绝不换池再注一份。
        """
        h = (info_hash or "").lower()
        if not h:
            return None
        for name, tasks in self.get_pool_snapshot(include_excluded=True).items():
            if any((t.get("hash") or "").lower() == h for t in tasks):
                return next((s for s in self.specs if s.name == name), None)
        return None

    def select_target_pool(
        self,
        local_path: str = "",
        prefer_pool: str = "",
        info_hash: str = "",
    ) -> Optional[PoolSpec]:
        """选择配对目标池（music3 不承接）

        规则（2026-11-21 用户指定，且必须遵守同 info-hash 全集群单实例铁律）：
        1. 该 info-hash 已存在于**任一受管池（含 music3）** -> 直接复用该池，
           杜绝重复注入（旧实现只查 7 个承接池，music3 里的同 hash 会被漏判，
           再换池注入就是跨实例双活）；
        2. 全局无同 hash 时才认显式 prefer_pool（人工指池补种路径）；
        3. 否则按当前任务数选最空的承接池（负载均衡）。

        ★ 2026-11-21 修正：原实现把 prefer_pool 放在查重之前直接 return，
          使「显式指定池」成为绕开单实例铁律的后门（同一 hash 可被再注一份）。
          现在查重**先于** prefer_pool；两者冲突时按铁律走既有归属池并告警。
        """
        # 1. 全池查重（含 music3）：已有同 hash 就复用，绝不换池再注一份
        existed = self.find_hash_pool(info_hash or "")
        if existed is not None:
            if prefer_pool and prefer_pool != existed.name:
                logger.warning(
                    f"[PoolPairing] 指定池 {prefer_pool} 与该 hash 现有归属 {existed.name} 冲突，"
                    f"按单实例铁律复用 {existed.name}（拒绝再注一份）"
                )
            else:
                logger.info(f"[PoolPairing] info-hash 已存在于 {existed.name}，复用该池")
            return existed

        # 2. 全局无同 hash：此时才认显式指定的池
        if prefer_pool:
            spec = next((s for s in self.specs if s.name == prefer_pool), None)
            if spec:
                return spec

        # 3. 核心优化：同专辑多站做种归一（零硬链接）
        # 若 local_path 物理数据已明确位于某个池内，直接注入该池同目录，杜绝跨池硬链接！
        if local_path:
            path_pool = self.pool_for_path(local_path)
            if path_pool is not None:
                logger.info(f"[PoolPairing] local_path 归属于 {path_pool.name}，同专辑归一做种（免跨池硬链接）")
                return path_pool

        snap = self.get_pool_snapshot()
        if not snap:
            return None

        target = min(snap, key=lambda k: len(snap[k]))
        spec = next((s for s in self.specs if s.name == target), None)
        if spec:
            logger.info(f"[PoolPairing] 负载选池 -> {spec.name}({spec.port}) loads={ {k: len(v) for k, v in snap.items()} }")
        return spec

    # ---------------------------------------------------------- 种子元数据
    @staticmethod
    def parse_torrent(torrent_bytes: bytes) -> Tuple[str, List[Tuple[str, int]], int, str]:
        """解析种子：返回 (name, [(相对路径, size)...], 总大小, infohash)

        相对路径已包含种子根目录名，可直接拼接到池数据根之下。
        分隔符统一归一化为 `/`，兼容 Windows 侧解析与 Linux 生产端。
        """
        import torf

        t = torf.Torrent.read_stream(io.BytesIO(torrent_bytes))
        files: List[Tuple[str, int]] = []
        for f in t.files:
            rel = str(f).replace("\\", "/").lstrip("/")
            files.append((rel, int(f.size)))
        info_hash = (getattr(t, "infohash", None) or getattr(t, "infohash_v1", None) or "")
        return str(t.name), files, int(t.size), str(info_hash).lower()

    @staticmethod
    def _abs_path(root: str, rel: str) -> str:
        return os.path.join(root, *[p for p in rel.split("/") if p])

    def verify_payload(self, pool_root: str, files: List[Tuple[str, int]]) -> Tuple[bool, Dict[str, object]]:
        """按种子清单逐文件校验「存在且 size 一致」，防止跳过校验造成幽灵做种"""
        missing: List[str] = []
        mismatch: List[str] = []
        for rel, size in files:
            p = self._abs_path(pool_root, rel)
            if not os.path.isfile(p):
                missing.append(rel)
            elif os.path.getsize(p) != size:
                mismatch.append(f"{rel} ({os.path.getsize(p)}!={size})")
        detail = {
            "checked": len(files),
            "missing": missing[:5],
            "mismatch": mismatch[:5],
            "missing_count": len(missing),
            "mismatch_count": len(mismatch),
            "ok": not missing and not mismatch,
        }
        return detail["ok"], detail

    # ------------------------------------------------------------ 物理补齐
    def _build_hardlink_payload(
        self,
        pool_root: str,
        source_path: str,
        files: List[Tuple[str, int]],
    ) -> Tuple[bool, str]:
        """目标目录缺失时，用同卷硬链接按种子清单补齐（绝不覆盖已存在文件）"""
        if not os.path.exists(pool_root):
            return False, f"池数据根不存在: {pool_root}"

        created_files: List[str] = []
        created_dirs: List[str] = []
        try:
            for rel, size in files:
                parts = [p for p in rel.split("/") if p]
                inner = parts[1:] if len(parts) > 1 else []
                dst = self._abs_path(pool_root, rel)
                src = os.path.join(source_path, *inner) if inner else source_path

                if os.path.exists(dst):
                    if os.path.getsize(dst) == size:
                        continue
                    raise RuntimeError(f"目标已存在且 size 不一致，拒绝覆盖: {dst}")
                if not os.path.isfile(src):
                    raise RuntimeError(f"源文件缺失，无法建立硬链接: {src}")
                if os.path.getsize(src) != size:
                    raise RuntimeError(f"源文件 size 与种子不符: {src}")

                parent = os.path.dirname(dst)
                if not os.path.isdir(parent):
                    missing_dirs: List[str] = []
                    probe = parent
                    while probe and probe != pool_root and not os.path.exists(probe):
                        missing_dirs.append(probe)
                        probe = os.path.dirname(probe)
                    os.makedirs(parent, exist_ok=True)
                    created_dirs.extend(missing_dirs)

                os.link(src, dst)
                created_files.append(dst)

            ok, detail = self.verify_payload(pool_root, files)
            if not ok:
                raise RuntimeError(f"硬链接补齐后校验仍未通过: {detail}")
            return True, f"已建立 {len(created_files)} 个同卷硬链接"
        except Exception as e:
            for p in created_files:
                try:
                    if os.path.exists(p):
                        os.unlink(p)
                except OSError:
                    pass
            for d in sorted(created_dirs, key=len, reverse=True):
                try:
                    os.rmdir(d)
                except OSError:
                    pass
            return False, str(e)

    def _record_paired(self, spec: "PoolSpec", info_hash: str) -> None:
        """配对成功后把该 hash 记入两个池清单缓存，使同批次后续配对/查重立即看到"""
        if not info_hash:
            return
        h = info_hash.lower()
        for cache in (self._snap_cache, self._snap_all_cache):
            if cache is None:
                continue
            bucket = cache.get(spec.name)
            if bucket is None:
                continue
            if any((t.get("hash") or "").lower() == h for t in bucket):
                continue
            bucket.append({"hash": h, "progress": 1.0, "state": "pausedUP",
                           "size": 1, "amount_left": 0})

    def _record_landed(self, spec: "PoolSpec", info_hash: str,
                       torrent: Dict[str, object]) -> None:
        """注入已落地但进度未确认时，按 qB 实况记入两个池清单缓存。

        与 `_record_paired` 的区别：**不改写 progress/state**，保留真实值。
        目的只有一个——让后续 `find_hash_pool()` 立刻看到这个 hash，否则
        60s 快照窗口内下一轮会换池再注一份，变成跨实例双活。
        """
        if not info_hash:
            return
        h = info_hash.lower()
        for cache in (self._snap_cache, self._snap_all_cache):
            if cache is None:
                continue
            bucket = cache.get(spec.name)
            if bucket is None:
                continue
            if any((t.get("hash") or "").lower() == h for t in bucket):
                continue
            bucket.append(dict(torrent))

    def _record_in_stack(
        self,
        spec: "PoolSpec",
        info_hash: str,
        name: str = "",
        size: int = 0,
        reason: str = "",
    ) -> None:
        """把一次成功配对写入在册快照与入栈流水；记账失败不影响配对结果"""
        gm = self.graph_manager
        if not gm or not info_hash:
            return
        try:
            gm.register_cluster_infohash(
                info_hash, spec.name, spec.port,
                name=name, size=size, progress=1.0,
                state="pausedUP", save_path="/downloads",
            )
            gm.record_pool_event(
                info_hash, "PAIR", pool_name=spec.name, pool_port=spec.port,
                name=name, size=size, save_path="/downloads",
                actor="pool_pairing", reason=reason,
            )
        except Exception as e:
            logger.warning(f"[PoolPairing] 写 {spec.name} 入栈流水失败（不影响配对）: {e}")

    @staticmethod
    def _is_complete(torrent: Dict[str, object]) -> bool:
        """判定任务是否已完整：兼容部分 qB 版本 skip_checking 后 progress 上报滞后"""
        try:
            if float(torrent.get("progress", 0.0)) >= 0.999:
                return True
        except (TypeError, ValueError):
            pass
        try:
            if "amount_left" in torrent and int(torrent.get("amount_left") or 0) == 0:
                return int(torrent.get("size") or 0) > 0
        except (TypeError, ValueError):
            pass
        return False

    # ---------------------------------------------------------------- 配对
    def pair_published_torrent(
        self,
        torrent_bytes: bytes,
        local_path: str,
        expected_hash: str = "",
        tags: str = "musichub,paired",
        dry_run: bool = False,
        target_pool_name: str = "",
    ) -> Tuple[bool, str]:
        """把已发布 B 种以暂停态配对进「按负载选出的」音乐池

        返回 (成功, 说明)。任何失败都只影响配对，不得回滚 3010 做种。
        target_pool_name 为空时由 select_target_pool() 自动决策（music3 不承接）。
        """
        if not torrent_bytes:
            return False, "种子字节为空，无法配对"
        if not local_path or not os.path.exists(local_path):
            return False, f"本地实体数据不存在: {local_path}"

        real = os.path.realpath(local_path).replace("\\", "/")
        if any(m in [p for p in real.split("/") if p] for m in FORBIDDEN_PATH_MARKERS):
            return False, f"本地实体位于禁区，拒绝配对: {local_path}"

        try:
            name, files, total_size, info_hash = self.parse_torrent(torrent_bytes)
        except Exception as e:
            return False, f"解析种子失败: {e}"

        target_hash = (expected_hash or info_hash or "").lower()
        spec = self.select_target_pool(local_path=local_path, prefer_pool=target_pool_name, info_hash=target_hash)
        if spec is None:
            return False, f"无可用承接池，跳过配对: {local_path}"

        pool_root = spec.host_dir
        ok, detail = self.verify_payload(pool_root, files)
        if not ok:
            if dry_run:
                # 干跑必须只读：不得在池内建立任何硬链接或目录
                return True, (
                    f"[Dry-Run] {name} 在 {spec.name} 需补齐 "
                    f"{detail['missing_count']} 个缺失文件 / {detail['mismatch_count']} 个 size 不符，"
                    f"补齐后可以 paused 态配对"
                )
            logger.warning(
                f"[PoolPairing] {spec.name} 现有数据与种子清单不一致，尝试硬链接补齐 "
                f"(missing={detail['missing']}, mismatch={detail['mismatch']})"
            )
            ok2, msg2 = self._build_hardlink_payload(pool_root, local_path, files)
            if not ok2:
                return False, f"池内数据无法与种子对齐 ({detail['missing']}): {msg2}"

        if dry_run:
            return True, (
                f"[Dry-Run] 可将 {name} ({len(files)} 文件 / {total_size} B) "
                f"以 paused 态配对到 {spec.name}"
            )

        client = self.client_for(spec)
        existing = client.get_torrents() or []
        match = next(
            (t for t in existing if (t.get("hash") or "").lower() == target_hash),
            None,
        ) if target_hash else None

        if match:
            if self._is_complete(match):
                self._record_in_stack(spec, target_hash, name, total_size, "池内已存在同 hash 任务")
                return True, f"{spec.name} 已存在同 hash 任务 (state={match.get('state')})"
            return False, (
                f"{spec.name} 已存在同 hash 但未完成 (state={match.get('state')}, "
                f"progress={float(match.get('progress', 0.0)):.1%})"
            )

        added = client.add_torrent(
            torrent_bytes=torrent_bytes,
            save_path="/downloads",
            is_paused=True,
            is_skip_checking=True,
            tags=tags,
            auto_tmm=False,
        )
        if not added:
            # 409/重复添加时按 hash 二次核对
            time.sleep(1)
            existing = client.get_torrents() or []
            match = next(
                (t for t in existing if (t.get("hash") or "").lower() == target_hash),
                None,
            ) if target_hash else None
            if not match:
                return False, f"注入 {spec.name} 失败（无同 hash 任务落地）"

        deadline = time.time() + self.verify_timeout
        while time.time() < deadline:
            torrents = client.get_torrents() or []
            match = next(
                (t for t in torrents if (t.get("hash") or "").lower() == target_hash),
                None,
            ) if target_hash else None
            if match and self._is_complete(match):
                self._record_paired(spec, target_hash)
                self._record_in_stack(spec, target_hash, name, total_size, "发布后配对注入")
                return True, (
                    f"已配对到 {spec.name}（paused，progress={float(match.get('progress', 0)):.0%}，"
                    f"state={match.get('state')}）"
                )
            time.sleep(self.poll_interval)

        # ★ 超时也要记账：任务已注入（磁盘排队/recheck 未完成），若此刻不登记，
        #   60s 快照缓存里就没有这个 hash，下一轮会换池再注一份 ⇒ 跨实例双活。
        #   记账保留 qB 实况进度，不伪造成 100%（否则会被当成"已完成"不再复核）。
        landed = None
        if target_hash:
            try:
                torrents = client.get_torrents() or []
                landed = next(
                    (t for t in torrents if (t.get("hash") or "").lower() == target_hash),
                    None,
                )
            except Exception as e:
                logger.warning(f"[PoolPairing] 超时后复核 {spec.name} 落地状态失败: {e}")
        if landed is not None:
            self._record_landed(spec, target_hash, landed)
            self._record_in_stack(
                spec, target_hash, name, total_size,
                f"配对注入后 {self.verify_timeout:.0f}s 未达 100%，按已落地记账（禁止换池重注）",
            )
            try:
                progress = float(landed.get("progress", 0.0))
            except (TypeError, ValueError):
                progress = 0.0
            return False, (
                f"注入 {spec.name} 后 {self.verify_timeout:.0f}s 内未达到 100%"
                f"（state={landed.get('state')}, progress={progress:.0%}）；"
                f"已按『已落地』记账，后续配对会复用该池、不再另注一份"
            )
        if target_hash:
            return False, f"注入 {spec.name} 后 {self.verify_timeout:.0f}s 内未达到 100%"
        return True, f"已注入 {spec.name}（未取得 infohash，未做落地核验）"
