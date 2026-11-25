# -*- coding: utf-8 -*-
"""按目标站 tid 安全恢复 MusicHub B 种做种（默认 dry-run）。

  python musichub/tools/repair_seeding.py --target xingwan:4365
  python musichub/tools/repair_seeding.py --target tangpt:72455 --apply
  python musichub/tools/repair_seeding.py --target xingwan:4365 --format json

安全顺序：取得并解析 B 种 -> 3010+8 池全量查重 -> 按 B 种文件清单逐文件
校验精确实体路径 -> 唯一 paused 原地唤醒，或完全缺失时 paused 配对后唤醒。
任何实例读不到、hash 不一致、文件缺失/size 不符、多个 paused 持有者均 fail-closed。
不删除任务或文件，不自动下载，不触碰 music5，不重启 qB。
"""

import argparse
import json
import os
import sys
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from musichub.core.config import MusicHubConfig
from musichub.core.qb_client import QBClient
from musichub.onsite.detail_archive import build_adapter, load_published_targets, sanitize_html
from musichub.routing.graph import RoutingGraphManager, normalize_site_key
from musichub.storage.cluster_guard import is_paused_state
from musichub.storage.pool_pairing import EXCLUDED_LOAD_POOLS, PoolPairingManager

SEEDING_STATES = {"stalledup", "uploading", "forcedup", "queuedup", "checkingup"}
DOWNLOAD_STATES = {"stalleddl", "downloading", "queueddl", "checkingdl", "missingfiles", "error"}
HEX40 = frozenset("0123456789abcdef")


def parse_target(value: str) -> Tuple[str, str]:
    site, sep, tid = (value or "").partition(":")
    site, tid = normalize_site_key(site), tid.strip()
    if not sep or not site or not tid.isdigit():
        raise ValueError(f"目标必须写成 site:数字tid，收到: {value!r}")
    return site, tid


def valid_hash(value: Any) -> str:
    h = str(value or "").strip().lower()
    return h if len(h) == 40 and set(h) <= HEX40 else ""


def complete(row: Dict[str, Any]) -> bool:
    try:
        if float(row.get("progress") or 0.0) >= 0.999:
            return True
    except (TypeError, ValueError):
        pass
    try:
        return int(row.get("amount_left") or 0) == 0 and int(row.get("size") or 0) > 0
    except (TypeError, ValueError):
        return False


def active_seeding(row: Dict[str, Any]) -> bool:
    return complete(row) and str(row.get("state") or "").lower() in SEEDING_STATES


def public_hit(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "instance": row.get("_instance"), "port": row.get("_port"),
        "state": row.get("state"), "progress": row.get("progress"),
        "seeding_time": row.get("seeding_time"), "uploaded": row.get("uploaded"),
        "save_path": row.get("save_path"), "name": row.get("name"),
    }


class RepairRunner:
    def __init__(
        self,
        db_path: str,
        torrents_dir: str,
        host: str,
        apply: bool = False,
        verify_timeout: float = 30.0,
        publish_dir: str = "",
        manager: Optional[PoolPairingManager] = None,
        graph: Optional[RoutingGraphManager] = None,
        publish_client: Optional[QBClient] = None,
    ):
        self.db_path = db_path
        self.torrents_dir = torrents_dir
        self.host = host
        self.apply = apply
        self.verify_timeout = max(2.0, float(verify_timeout))
        self.publish_dir = publish_dir
        self.graph = graph or (RoutingGraphManager(db_path) if apply else None)
        self.manager = manager or PoolPairingManager(publish_host=host, graph_manager=self.graph)
        self.publish_client = publish_client or QBClient(host=host, port=3010)
        self.adapters: Dict[str, Any] = {}
        self.snapshot: Dict[str, List[Dict[str, Any]]] = {}
        self.errors: Dict[str, str] = {}
        self.loads: Dict[str, int] = {}
        self._last_fetch = 0.0

    def _instances(self) -> List[Tuple[str, int, QBClient]]:
        return [("publish", 3010, self.publish_client)] + [
            (spec.name, spec.port, self.manager.client_for(spec)) for spec in self.manager.specs
        ]

    @staticmethod
    def _error(exc: Exception) -> str:
        return sanitize_html(f"{type(exc).__name__}: {exc}")[:500]

    def collect(self) -> None:
        """一次读取 3010+8 池；保留同 hash 的全部持有者，不能用 setdefault 吞掉重复。"""
        self.snapshot, self.errors, self.loads = {}, {}, {}
        for name, port, client in self._instances():
            try:
                rows = client.get_torrents(strict=True) or []
            except Exception as exc:
                self.errors[name] = self._error(exc)
                continue
            self.loads[name] = len(rows)
            for source in rows:
                h = valid_hash(source.get("hash"))
                if not h:
                    continue
                row = dict(source)
                row.update({"_instance": name, "_port": port, "_client": client})
                self.snapshot.setdefault(h, []).append(row)

    def probe_hash(self, info_hash: str) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        hits: List[Dict[str, Any]] = []
        errors: Dict[str, str] = {}
        for name, port, client in self._instances():
            try:
                rows = client.get_torrents(hashes=info_hash, strict=True) or []
            except Exception as exc:
                errors[name] = self._error(exc)
                continue
            for source in rows:
                if valid_hash(source.get("hash")) != info_hash:
                    continue
                row = dict(source)
                row.update({"_instance": name, "_port": port, "_client": client})
                hits.append(row)
        return hits, errors

    def adapter(self, site: str):
        if site not in self.adapters:
            self.adapters[site] = build_adapter(site)
        return self.adapters[site]

    def resolve_torrent(self, target: Dict[str, Any]) -> Tuple[bytes, str, str]:
        site, tid = target["target_site"], target["target_tid"]
        expected = valid_hash(target.get("b_hash"))
        path = os.path.join(self.torrents_dir, f"{site}_{tid}.torrent")
        local_error = ""
        if os.path.isfile(path):
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
                _name, _files, _size, found = self.manager.parse_torrent(data)
                if expected and found.lower() != expected:
                    return b"", "local_archive", f"本地 B 种 hash 与血缘不一致: {found[:10]} != {expected[:10]}"
                return data, "local_archive", ""
            except Exception as exc:
                local_error = self._error(exc)
        try:
            wait = 1.0 - (time.monotonic() - self._last_fetch)
            if wait > 0:
                time.sleep(wait)
            self._last_fetch = time.monotonic()
            data = self.adapter(site).download_torrent(tid) or b""
            if not data:
                return b"", "site_download", "站点未返回种子字节"
            _name, _files, _size, found = self.manager.parse_torrent(data)
            if expected and found.lower() != expected:
                return b"", "site_download", f"站点 B 种 hash 与血缘不一致: {found[:10]} != {expected[:10]}"
            return data, "site_download", ""
        except Exception as exc:
            note = "站点取回内容不是可解析 torrent（可能为 HTML/登录/错误页）: " + self._error(exc)
            if local_error:
                note += f"；本地归档也不可用: {local_error}"
            return b"", "site_download", note

    @staticmethod
    def safe_file_list(name: str, files: List[Tuple[str, int]]) -> bool:
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            return False
        for rel, size in files:
            parts = [p for p in str(rel).replace("\\", "/").split("/") if p]
            if not parts or ".." in parts or parts[0] != name or int(size) < 0:
                return False
        return True

    def payload_candidates(self, name: str, files: List[Tuple[str, int]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        good: List[Dict[str, Any]] = []
        rejected: Dict[str, Any] = {}
        for spec in self.manager.specs:
            ok, detail = self.manager.verify_payload(spec.host_dir, files)
            if ok:
                good.append({"spec": spec, "local_path": os.path.join(spec.host_dir, name), "detail": detail})
            else:
                rejected[spec.name] = detail
        return good, rejected

    def verify_existing_payload(self, hit: Dict[str, Any], files: List[Tuple[str, int]]) -> Tuple[bool, str, str]:
        instance = hit["_instance"]
        if str(hit.get("save_path") or "").rstrip("/") != "/downloads":
            return False, "", f"BAD_SAVE_PATH: {hit.get('save_path')}"
        if instance == "publish":
            root = self.publish_dir
        else:
            spec = next((s for s in self.manager.specs if s.name == instance), None)
            root = spec.host_dir if spec else ""
        if not root:
            return False, "", "找不到该实例的精确实体根目录"
        ok, detail = self.manager.verify_payload(root, files)
        return ok, root, ("精确文件校验通过" if ok else f"NO_DATA: {detail}")

    def _wait_active(self, client: QBClient, info_hash: str) -> Tuple[bool, Dict[str, Any], str]:
        deadline = time.monotonic() + self.verify_timeout
        latest: Dict[str, Any] = {}
        note = "等待 active/progress=100% 超时"
        while time.monotonic() < deadline:
            try:
                rows = client.get_torrents(hashes=info_hash, strict=True) or []
            except Exception as exc:
                return False, latest, self._error(exc)
            latest = next((r for r in rows if valid_hash(r.get("hash")) == info_hash), {})
            if latest and active_seeding(latest):
                return True, latest, "active 且 progress=100%"
            state = str(latest.get("state") or "").lower() if latest else ""
            if state in DOWNLOAD_STATES:
                note = f"唤醒后落入 {state or '未知'} 且未达到 100%，立即停回 paused"
                client.stop_torrent(info_hash)
                return False, latest, note
            time.sleep(1.0)
        return False, latest, note

    def trackers(self, client: QBClient, info_hash: str, wait: bool = True) -> Tuple[List[Dict[str, Any]], bool]:
        deadline = time.monotonic() + min(self.verify_timeout, 15.0)
        evidence: List[Dict[str, Any]] = []
        while True:
            evidence = []
            try:
                rows = client.get_trackers(info_hash) or []
            except Exception as exc:
                return [{"status": "FETCH_ERROR", "msg": self._error(exc)}], False
            for tracker in rows:
                url = str(tracker.get("url") or "")
                if not url or url.startswith("**"):
                    continue
                evidence.append({
                    "url_host": url.split("/")[2] if "://" in url else url,
                    "status": tracker.get("status"), "msg": str(tracker.get("msg") or "")[:300],
                    "num_incomplete": tracker.get("num_incomplete"),
                })
            if any(int(t.get("status") or 0) == 2 for t in evidence):
                return evidence, True
            if any(int(t.get("status") or 0) == 4 for t in evidence) or not wait:
                return evidence, False
            if time.monotonic() >= deadline:
                return evidence, False
            time.sleep(1.0)

    def record_recovery(self, target: Dict[str, Any], info_hash: str, name: str, size: int,
                        hit: Dict[str, Any], local_path: str, note: str) -> str:
        if not self.graph:
            return ""
        try:
            self.graph.record_pool_event(
                info_hash, "RECOVER", pool_name=hit["_instance"], pool_port=hit["_port"],
                name=name, size=size, save_path=hit.get("save_path") or "/downloads",
                actor="repair_seeding", reason=note,
            )
            self.graph.record_seed_recovery(
                target_site=target["target_site"], target_tid=target["target_tid"], b_hash=info_hash,
                qb_instance=hit["_instance"], qb_port=hit["_port"],
                save_path=hit.get("save_path") or "/downloads",
                source_site=target.get("source_site") or "", source_tid=target.get("source_tid") or "",
                source_hash=target.get("source_hash") or "", source_name=target.get("source_name") or name,
                local_path=local_path, target_url=target.get("target_url") or "", note=note,
            )
            return ""
        except Exception as exc:
            return self._error(exc)

    def resume_unique(self, target: Dict[str, Any], data: bytes, source: str, name: str,
                      files: List[Tuple[str, int]], size: int, info_hash: str,
                      action: str) -> Dict[str, Any]:
        hits, errors = self.probe_hash(info_hash)
        base = self.row(target, info_hash, name, source, hits)
        if errors:
            return self.finish(base, "QB_CLIENT_ERROR", "恢复前全集群复核失败", errors=errors)
        active = [h for h in hits if not is_paused_state(h.get("state"))]
        if len(active) > 1:
            return self.finish(base, "LIVE_DUPLICATE", "恢复前发现多个 active，禁止继续")
        if len(active) == 1:
            hit = active[0]
            if active_seeding(hit):
                trackers, tracker_ok = self.trackers(hit["_client"], info_hash, wait=False)
                base.update({"trackers": trackers, "tracker_verified": tracker_ok,
                             "seed_verified": True, "payload_pool": hit["_instance"]})
                issues = [] if tracker_ok else ["B_TRACKER_ERROR"]
                return self.finish(base, "ALREADY_SEEDING",
                                   f"已存在于 {hit['_instance']} state={hit.get('state')}", issues=issues)
            if str(hit.get("state") or "").lower() != "checkingresumedata":
                trackers, tracker_ok = self.trackers(hit["_client"], info_hash, wait=False)
                base.update({"trackers": trackers, "tracker_verified": tracker_ok,
                             "seed_verified": False, "payload_pool": hit["_instance"]})
                return self.finish(base, "B_ACTIVE_INCOMPLETE",
                                   f"已存在于 {hit['_instance']} state={hit.get('state')}",
                                   issues=[] if tracker_ok else ["B_TRACKER_ERROR"])
            # skip_checking 后 qB 仍可能短暂 checkingResumeData；等待既有任务收口，禁止重注。
            seeded, latest, wait_note = self._wait_active(hit["_client"], info_hash)
            if not seeded:
                base["hits"] = [public_hit({**hit, **latest})]
                return self.finish(base, "RESUME_FAILED", wait_note)
            ok, root, detail = self.verify_existing_payload(hit, files)
            if not ok:
                return self.finish(base, "NO_DATA", detail)
        else:
            if len(hits) != 1:
                return self.finish(base, "AMBIGUOUS_PAUSED", f"存在 {len(hits)} 个 paused 持有者，拒绝选择")
            hit = hits[0]
            if not complete(hit):
                return self.finish(base, "B_PAUSED_INCOMPLETE", "唯一 paused 任务未达到 100%，拒绝唤醒下载")
            ok, root, detail = self.verify_existing_payload(hit, files)
            if not ok:
                return self.finish(base, "NO_DATA", detail)
            if not hit["_client"].resume_torrent(info_hash):
                return self.finish(base, "RESUME_FAILED", "qB resume/start API 失败")
            seeded, latest, wait_note = self._wait_active(hit["_client"], info_hash)
            if not seeded:
                base["hits"] = [public_hit({**hit, **latest})]
                return self.finish(base, "RESUME_FAILED", wait_note)
        final_hits, final_errors = self.probe_hash(info_hash)
        base["hits"] = [public_hit(h) for h in final_hits]
        if final_errors:
            return self.finish(base, "QB_CLIENT_ERROR", "恢复后全集群复核失败", errors=final_errors)
        final_active = [h for h in final_hits if not is_paused_state(h.get("state"))]
        if len(final_active) != 1:
            return self.finish(base, "LIVE_DUPLICATE" if final_active else "RESUME_FAILED",
                               f"恢复后 active 实例数={len(final_active)}")
        current = final_active[0]
        trackers, tracker_ok = self.trackers(current["_client"], info_hash)
        issues = [] if tracker_ok else ["B_TRACKER_ERROR" if any(int(t.get("status") or 0) == 4 for t in trackers) else "TRACKER_PENDING"]
        local_path = os.path.join(root, name)
        note = f"{action} {current['_instance']}；{wait_note}；tracker={'working' if tracker_ok else 'not_working'}"
        ledger_error = self.record_recovery(target, info_hash, name, size, current, local_path, note)
        if ledger_error:
            issues.append("LEDGER_ERROR")
        base.update({
            "trackers": trackers, "tracker_verified": tracker_ok, "seed_verified": True,
            "action": action, "payload_pool": current["_instance"], "ledger_error": ledger_error,
        })
        return self.finish(base, "RESUMED" if action == "resume" else "INJECTED", note, issues=issues)

    @staticmethod
    def row(target: Dict[str, Any], info_hash: str = "", name: str = "", source: str = "",
            hits: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        return {
            "site": target["target_site"], "tid": target["target_tid"], "b_hash": info_hash,
            "name": name, "torrent_source": source, "action": "",
            "hits": [public_hit(h) for h in hits or []], "trackers": [],
            "seed_verified": False, "tracker_verified": False, "issues": [], "errors": {},
        }

    @staticmethod
    def finish(row: Dict[str, Any], status: str, detail: str, issues: Optional[List[str]] = None,
               errors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        row.update({"status": status, "detail": detail})
        if issues:
            row["issues"] = issues
        if errors:
            row["errors"] = errors
        return row

    def process(self, target: Dict[str, Any]) -> Dict[str, Any]:
        site, tid = target["target_site"], target["target_tid"]
        if self.errors:
            return self.finish(self.row(target), "QB_CLIENT_ERROR", "全集群快照不完整，禁止恢复判定", errors=self.errors)
        data, source, error = self.resolve_torrent(target)
        if not data:
            return self.finish(self.row(target, name=target.get("source_name") or "", source=source),
                               "IDENTITY_MISMATCH" if "不一致" in error else "NO_TORRENT", error)
        try:
            name, files, size, info_hash = self.manager.parse_torrent(data)
        except Exception as exc:
            return self.finish(self.row(target, source=source), "NO_TORRENT", self._error(exc))
        hits = self.snapshot.get(info_hash, [])
        base = self.row(target, info_hash, name, source, hits)
        if not self.safe_file_list(name, files):
            return self.finish(base, "NO_TORRENT", "种子根目录或相对文件路径不安全")
        active = [h for h in hits if not is_paused_state(h.get("state"))]
        if len(active) > 1:
            return self.finish(base, "LIVE_DUPLICATE", "同一 B hash 已有多个 active，禁止增删改")
        if len(active) == 1:
            current = active[0]
            trackers, tracker_ok = self.trackers(current["_client"], info_hash, wait=False)
            base.update({"trackers": trackers, "tracker_verified": tracker_ok,
                         "seed_verified": active_seeding(current)})
            issues = [] if tracker_ok else ["B_TRACKER_ERROR"]
            return self.finish(base, "ALREADY_SEEDING" if active_seeding(current) else "B_ACTIVE_INCOMPLETE",
                               f"已存在于 {current['_instance']} state={current.get('state')}", issues=issues)
        if hits:
            if len(hits) != 1:
                return self.finish(base, "AMBIGUOUS_PAUSED", f"存在 {len(hits)} 个 paused 持有者，拒绝选择")
            hit = hits[0]
            if not complete(hit):
                return self.finish(base, "B_PAUSED_INCOMPLETE", "唯一 paused 任务未达到 100%，拒绝唤醒下载")
            ok, _root, detail = self.verify_existing_payload(hit, files)
            if not ok:
                return self.finish(base, "NO_DATA", detail)
            if not self.apply:
                base["action"] = "resume"
                base["payload_pool"] = hit["_instance"]
                return self.finish(base, "READY_RESUME", f"精确载荷通过，可原地唤醒 {hit['_instance']}")
            return self.resume_unique(target, data, source, name, files, size, info_hash, "resume")

        candidates, rejected = self.payload_candidates(name, files)
        eligible = [c for c in candidates if c["spec"].name not in EXCLUDED_LOAD_POOLS]
        if not eligible:
            if candidates:
                return self.finish(base, "NO_ELIGIBLE_POOL", "精确数据仅在不承接新 B 种的 music3，拒绝注入")
            return self.finish(base, "NO_DATA", f"8 池均无精确文件载荷: {rejected}")
        preferred = ""
        lineage_path = str(target.get("local_path") or "")
        for candidate in eligible:
            if lineage_path and os.path.realpath(candidate["local_path"]) == os.path.realpath(lineage_path):
                preferred = candidate["spec"].name
                break
        chosen = next((c for c in eligible if c["spec"].name == preferred), None)
        if chosen is None:
            chosen = min(eligible, key=lambda c: (self.loads.get(c["spec"].name, 0), c["spec"].name))
        spec = chosen["spec"]
        base["action"], base["payload_pool"] = "inject", spec.name
        if not self.apply:
            return self.finish(base, "READY_INJECT", f"精确载荷通过，可 paused 配对到 {spec.name} 后唤醒")
        # 写入前必须再用 strict 全集群查重；不能用流程开头的旧快照授权注入。
        appeared, probe_errors = self.probe_hash(info_hash)
        if probe_errors:
            return self.finish(base, "QB_CLIENT_ERROR", "注入前全集群复核失败", errors=probe_errors)
        if appeared:
            return self.resume_unique(target, data, source, name, files, size, info_hash, "resume")
        ok, message = self.manager.pair_published_torrent(
            torrent_bytes=data, local_path=chosen["local_path"], expected_hash=info_hash,
            dry_run=False, target_pool_name=spec.name, tags=f"musichub,recovery,{site}",
        )
        if not ok:
            return self.finish(base, "INJECT_FAILED", message)
        return self.resume_unique(target, data, source, name, files, size, info_hash, "inject")

    def run(self, targets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        self.collect()
        return [self.process(target) for target in targets]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", action="append", required=True, help="site:tid，可重复")
    parser.add_argument("--apply", action="store_true", help="实际配对/唤醒；默认 dry-run")
    parser.add_argument("--limit", type=int, default=0, help="显式限制本次目标数；0 不截断")
    parser.add_argument("--verify-timeout", type=float, default=30.0)
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--db", default="", help="覆盖 MusicHub SQLite 路径")
    parser.add_argument("--torrents-dir", default="", help="覆盖 B 种归档目录")
    args = parser.parse_args()

    cfg = MusicHubConfig.from_env()
    db_path = os.path.abspath(args.db or cfg.db_path)
    torrents_dir = os.path.abspath(args.torrents_dir or os.path.join(os.path.dirname(db_path), "torrents"))
    try:
        selected = [parse_target(value) for value in args.target]
    except ValueError as exc:
        parser.error(str(exc))
    selected = list(dict.fromkeys(selected))
    if args.limit > 0:
        selected = selected[:args.limit]
    published = {
        (r["target_site"], r["target_tid"]): r
        for r in load_published_targets(db_path)
    }
    targets = []
    for site, tid in selected:
        target = dict(published.get((site, tid), {}))
        target.update({"target_site": site, "target_tid": tid})
        targets.append(target)

    runner = RepairRunner(
        db_path=db_path, torrents_dir=torrents_dir, host=cfg.publish_qb.host,
        apply=args.apply, verify_timeout=args.verify_timeout,
        publish_dir=cfg.storage.publish_dir,
    )
    try:
        rows = runner.run(targets)
    finally:
        if runner.graph:
            runner.graph.close()
    counts = Counter(row["status"] for row in rows)
    result = {"mode": "apply" if args.apply else "dry-run", "selected": len(rows),
              "status_counts": dict(counts), "results": rows}
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"模式: {result['mode']} 目标={len(rows)}")
        for row in rows:
            print(f"{row['site']}#{row['tid']:<8} {row['status']:<22} "
                  f"hash={row['b_hash'][:10] or '-':10} pool={row.get('payload_pool') or '-':<12} {row['detail']}")
        print("汇总: " + json.dumps(dict(counts), ensure_ascii=False))
    bad = {"NO_TORRENT", "IDENTITY_MISMATCH", "NO_DATA", "NO_ELIGIBLE_POOL", "QB_CLIENT_ERROR",
           "LIVE_DUPLICATE", "AMBIGUOUS_PAUSED", "B_PAUSED_INCOMPLETE", "B_ACTIVE_INCOMPLETE",
           "RESUME_FAILED", "INJECT_FAILED"}
    return 1 if any(row["status"] in bad for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
