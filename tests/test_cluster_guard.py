# -*- coding: utf-8 -*-
"""音乐矩阵同 infohash 单实例守卫单元测试 (ClusterDeduper, 不触网)"""

import os
import shutil
import tempfile
import unittest

from musichub.storage.cluster_guard import (
    ClusterDeduper, is_paused_state, KIND_LIVE_DUPLICATE, KIND_SHADOW_PAIR,
    KIND_ALL_PAUSED, SEAT_REAL, SEAT_STALE,
)
from musichub.storage.pool_pairing import PoolSpec, PoolPairingManager


def _task(h, name="Album", state="stalledUP", progress=1.0, uploaded=0,
          added_on=1000.0, save_path="/downloads", size=4096):
    return {
        "hash": h, "name": name, "state": state, "progress": progress,
        "uploaded": uploaded, "added_on": added_on, "save_path": save_path,
        "size": size, "amount_left": 0 if progress >= 1 else size,
        "tags": "musichub", "category": "",
    }


class FakeClient:
    def __init__(self, torrents=None, tracker_msgs=None):
        self.torrents = list(torrents or [])
        self.deleted = []
        self.stopped = []
        self.tracker_msgs = tracker_msgs or {}

    def get_torrents(self, strict=False, hashes=None, **kwargs):
        if hashes:
            hs = {x.strip().lower() for x in str(hashes).split("|")}
            return [t for t in self.torrents if (t.get("hash") or "").lower() in hs]
        return list(self.torrents)

    def get_trackers(self, info_hash):
        return self.tracker_msgs.get(info_hash.lower(), [])

    def stop_torrent(self, hashes):
        self.stopped.append(hashes)
        hs = {x.strip().lower() for x in str(hashes).split("|")}
        self.torrents = [t for t in self.torrents if (t.get("hash") or "").lower() not in hs]
        return True

    def delete_torrent(self, hashes, delete_files=False):
        self.deleted.append({"hashes": hashes, "delete_files": delete_files})
        hs = {x.strip().lower() for x in str(hashes).split("|")}
        self.torrents = [t for t in self.torrents if (t.get("hash") or "").lower() not in hs]
        return True


class FailingClient(FakeClient):
    def get_torrents(self, strict=False, hashes=None, **kwargs):
        raise RuntimeError("connection refused")


class TestClusterGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.h1 = "a" * 40
        self.h2 = "b" * 40

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 状态判定
    def test_is_paused_state(self):
        for s in ("pausedUP", "stoppedUP", "pausedDL"):
            self.assertTrue(is_paused_state(s), s)
        for s in ("stalledUP", "uploading", "downloading", "checkingUP"):
            self.assertFalse(is_paused_state(s), s)

    def test_classify_three_kinds(self):
        g = ClusterDeduper.classify
        self.assertEqual(g([{"paused": False}, {"paused": False}]), KIND_LIVE_DUPLICATE)
        self.assertEqual(g([{"paused": False}, {"paused": True}]), KIND_SHADOW_PAIR)
        self.assertEqual(g([{"paused": True}, {"paused": True}]), KIND_ALL_PAUSED)

    # ---------------------------------------------------------------- 扫描
    def test_scan_counts_and_shadow_pair_not_conflict(self):
        pool_root = os.path.join(self.tmp, "music1")
        os.makedirs(os.path.join(pool_root, "Album"), exist_ok=True)
        specs = [PoolSpec("music1", 3220, pool_root, "127.0.0.1"),
                 PoolSpec("publish", 3010, "", "127.0.0.1")]
        clients = {
            3220: FakeClient([_task(self.h1, state="pausedUP")]),
            3010: FakeClient([_task(self.h1, state="stalledUP"), _task(self.h2, state="stalledUP")]),
        }
        guard = ClusterDeduper(host="127.0.0.1", specs=specs,
                               qb_factory=lambda h, p: clients[p])
        scan = guard.scan()

        self.assertEqual(scan["duplicate_hashes"], 1)
        self.assertEqual(scan["by_kind"][KIND_SHADOW_PAIR], 1)
        self.assertEqual(scan["by_kind"][KIND_LIVE_DUPLICATE], 0)
        self.assertEqual(guard.plan(scan), [], "交棒影子种不得进入收敛计划")

    def test_scan_marks_read_failure(self):
        specs = [PoolSpec("music1", 3220, self.tmp, "127.0.0.1"),
                 PoolSpec("music2", 3240, self.tmp, "127.0.0.1")]
        clients = {3220: FakeClient([_task(self.h1)]), 3240: FailingClient()}
        guard = ClusterDeduper(host="127.0.0.1", specs=specs,
                               qb_factory=lambda h, p: clients[p])
        scan = guard.scan()
        self.assertIn("music2", scan["failed_instances"])
        self.assertIn("connection refused", scan["instances"]["music2"]["error"])

    # ------------------------------------------------------------ 收敛计划
    def test_plan_prefers_pool_owner_over_publish(self):
        pool_root = os.path.join(self.tmp, "music2")
        os.makedirs(os.path.join(pool_root, "Album"), exist_ok=True)
        specs = [PoolSpec("music2", 3240, pool_root, "127.0.0.1"),
                 PoolSpec("publish", 3010, "", "127.0.0.1")]
        clients = {
            3240: FakeClient([_task(self.h1, uploaded=10)]),
            3010: FakeClient([_task(self.h1, uploaded=999)]),
        }
        guard = ClusterDeduper(host="127.0.0.1", specs=specs,
                               qb_factory=lambda h, p: clients[p])
        actions = guard.plan()
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["owner"]["pool"], "music2")
        self.assertEqual([x["pool"] for x in actions[0]["losers"]], ["publish"])

    def test_plan_prefers_complete_holder(self):
        specs = [PoolSpec("music1", 3220, self.tmp, "127.0.0.1"),
                 PoolSpec("music2", 3240, self.tmp, "127.0.0.1")]
        clients = {
            3220: FakeClient([_task(self.h1, progress=0.4, state="downloading")]),
            3240: FakeClient([_task(self.h1, progress=1.0)]),
        }
        guard = ClusterDeduper(host="127.0.0.1", specs=specs,
                               qb_factory=lambda h, p: clients[p])
        actions = guard.plan()
        self.assertEqual(actions[0]["owner"]["pool"], "music2")

    # ------------------------------------------------------------ 执 行
    def test_apply_deletes_without_files(self):
        specs = [PoolSpec("music1", 3220, self.tmp, "127.0.0.1"),
                 PoolSpec("music2", 3240, self.tmp, "127.0.0.1")]
        c1 = FakeClient([_task(self.h1, added_on=1.0)])
        c2 = FakeClient([_task(self.h1, added_on=500.0)])
        clients = {3220: c1, 3240: c2}
        guard = ClusterDeduper(host="127.0.0.1", specs=specs,
                               qb_factory=lambda h, p: clients[p])
        actions = guard.plan()
        res = guard.apply(actions)
        self.assertEqual(res["ok"], 1)
        loser = 3240 if actions[0]["owner"]["port"] == 3220 else 3220
        self.assertEqual(len(clients[loser].deleted), 1)
        self.assertFalse(clients[loser].deleted[0]["delete_files"])
        self.assertEqual(len(clients[loser].stopped), 1)
        # 保留者仍在
        self.assertTrue(clients[actions[0]["owner"]["port"]].torrents)

    def test_apply_refuses_delete_files_true(self):
        guard = ClusterDeduper(host="127.0.0.1", specs=[PoolSpec("music1", 3220, self.tmp)],
                               qb_factory=lambda h, p: FakeClient())
        with self.assertRaises(ValueError):
            guard.apply([], delete_files=True)

    # ------------------------------------------------------- 站点告警分诊
    def test_probe_seats_splits_real_duplicate_and_stale(self):
        specs = [PoolSpec("music1", 3220, self.tmp, "127.0.0.1"),
                 PoolSpec("music2", 3240, self.tmp, "127.0.0.1")]
        warn = [{"url": "https://tracker.example/announce/x", "status": 2,
                 "msg": "You cannot seed the same torrent in the same location from more than 1 client."}]
        clients = {
            3220: FakeClient([_task(self.h1), _task(self.h2)], tracker_msgs={self.h1: warn, self.h2: warn}),
            3240: FakeClient([_task(self.h1)], tracker_msgs={}),  # h1 双活；h2 只有一份 => 陈旧占位
        }
        guard = ClusterDeduper(host="127.0.0.1", specs=specs,
                              qb_factory=lambda h, p: clients[p])
        seats = guard.probe_seats()
        self.assertEqual(seats["by_kind"][SEAT_REAL], 1, seats)
        self.assertEqual(seats["by_kind"][SEAT_STALE], 1, seats)
        self.assertEqual(seats["by_domain"].get("tracker.example"), 2)


class TestPairingFullPoolDedupe(unittest.TestCase):
    """配对器必须对 music3（不承接新负载的池）也做同 hash 查重"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.h = "c" * 40

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reuses_hash_in_music3_instead_of_new_pool(self):
        root1 = os.path.join(self.tmp, "music1")
        root3 = os.path.join(self.tmp, "music3")
        os.makedirs(root1, exist_ok=True)
        os.makedirs(root3, exist_ok=True)
        specs = [PoolSpec("music3", 3260, root3, "127.0.0.1"),
                 PoolSpec("music1", 3220, root1, "127.0.0.1")]
        clients = {3260: FakeClient([_task(self.h)]), 3220: FakeClient([])}
        mgr = PoolPairingManager(specs=specs, qb_factory=lambda h_, p: clients[p])

        spec = mgr.select_target_pool(info_hash=self.h)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.name, "music3", "music3 已有同 hash 时必须复用，禁止换池再注一份")

    def test_prefer_pool_cannot_bypass_hash_dedupe(self):
        """显式指定池不得成为绕开「同 hash 单实例」的后门（2026-11-21 修正）"""
        root1 = os.path.join(self.tmp, "music1")
        root3 = os.path.join(self.tmp, "music3")
        os.makedirs(root1, exist_ok=True)
        os.makedirs(root3, exist_ok=True)
        specs = [PoolSpec("music3", 3260, root3, "127.0.0.1"),
                 PoolSpec("music1", 3220, root1, "127.0.0.1")]
        clients = {3260: FakeClient([_task(self.h)]), 3220: FakeClient([])}
        mgr = PoolPairingManager(specs=specs, qb_factory=lambda h_, p: clients[p])

        spec = mgr.select_target_pool(prefer_pool="music1", info_hash=self.h)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.name, "music3",
                         "该 hash 已在 music3 时，即使显式指定 music1 也必须复用 music3")

        # 全局确实没有该 hash 时，显式指定池仍然生效
        spec2 = mgr.select_target_pool(prefer_pool="music1", info_hash="f" * 40)
        self.assertEqual(spec2.name, "music1")

    def test_picks_lightest_load_pool_when_absent(self):
        root1 = os.path.join(self.tmp, "music1")
        root3 = os.path.join(self.tmp, "music3")
        os.makedirs(root1, exist_ok=True)
        os.makedirs(root3, exist_ok=True)
        specs = [PoolSpec("music3", 3260, root3, "127.0.0.1"),
                 PoolSpec("music1", 3220, root1, "127.0.0.1")]
        clients = {3260: FakeClient([_task("d" * 40)]), 3220: FakeClient([])}
        mgr = PoolPairingManager(specs=specs, qb_factory=lambda h_, p: clients[p])

        spec = mgr.select_target_pool(info_hash=self.h)
        self.assertEqual(spec.name, "music1", "music3 不承接新负载，应选最空的承接池")


if __name__ == "__main__":
    unittest.main()
