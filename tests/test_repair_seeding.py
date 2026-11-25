# -*- coding: utf-8 -*-
"""B 种恢复工具的 fail-closed 与真实文件校验测试。"""

import io
import os
import shutil
import tempfile
import unittest

import torf

from musichub.routing.graph import RoutingGraphManager
from musichub.storage.pool_pairing import PoolPairingManager, PoolSpec
from musichub.tools.repair_seeding import RepairRunner


def make_album(root):
    album = os.path.join(root, "Album")
    os.makedirs(os.path.join(album, "CD1"), exist_ok=True)
    with open(os.path.join(album, "01.flac"), "wb") as fh:
        fh.write(b"a" * 4096)
    with open(os.path.join(album, "CD1", "02.flac"), "wb") as fh:
        fh.write(b"b" * 2048)
    return album


def make_torrent(album):
    torrent = torf.Torrent(path=album, trackers=["http://tracker.example/announce"], private=True)
    torrent.generate()
    out = io.BytesIO()
    torrent.write_stream(out)
    return out.getvalue()


class FakeQB:
    def __init__(self, rows=None):
        self.torrents = list(rows or [])
        self.ops = []

    def get_torrents(self, strict=False, hashes=None, **_kwargs):
        if hashes:
            return [dict(r) for r in self.torrents if r.get("hash") == hashes]
        return [dict(r) for r in self.torrents]

    def add_torrent(self, torrent_bytes, is_paused=True, is_skip_checking=False, **kwargs):
        name, _files, size, info_hash = PoolPairingManager.parse_torrent(torrent_bytes)
        self.ops.append(("add", is_paused, is_skip_checking))
        self.torrents.append({
            "hash": info_hash, "name": name, "size": size, "amount_left": 0,
            "progress": 1.0, "state": "stoppedUP" if is_paused else "stalledUP",
            "save_path": "/downloads", "seeding_time": 0, "uploaded": 0,
        })
        return True

    def resume_torrent(self, info_hash):
        self.ops.append(("resume", info_hash))
        for row in self.torrents:
            if row.get("hash") == info_hash:
                row["state"] = "stalledUP" if float(row.get("progress") or 0) >= 0.999 else "stalledDL"
        return True

    def stop_torrent(self, info_hash):
        self.ops.append(("stop", info_hash))
        for row in self.torrents:
            if row.get("hash") == info_hash:
                row["state"] = "stoppedUP"
        return True

    def get_trackers(self, info_hash):
        return [{"url": "http://tracker.example/announce", "status": 2,
                 "msg": "working", "num_incomplete": 0}]


class TestRepairSeeding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pool = os.path.join(self.tmp, "pool")
        self.album = make_album(self.pool)
        self.data = make_torrent(self.album)
        self.info_hash = PoolPairingManager.parse_torrent(self.data)[3]
        self.torrents_dir = os.path.join(self.tmp, "torrents")
        os.makedirs(self.torrents_dir)
        with open(os.path.join(self.torrents_dir, "xingwan_4365.torrent"), "wb") as fh:
            fh.write(self.data)
        self.db = os.path.join(self.tmp, "musichub.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def row(self, state="stoppedUP"):
        return {"hash": self.info_hash, "name": "Album", "size": 6144, "amount_left": 0,
                "progress": 1.0, "state": state, "save_path": "/downloads",
                "seeding_time": 0, "uploaded": 0}

    def runner(self, pool_qb, apply=False, graph=None):
        manager = PoolPairingManager(
            specs=[PoolSpec("music_pool_1", 3210, self.pool, "127.0.0.1")],
            qb_factory=lambda _h, _p: pool_qb, verify_timeout=0.2, poll_interval=0.01,
            graph_manager=graph,
        )
        return RepairRunner(
            db_path=self.db, torrents_dir=self.torrents_dir, host="127.0.0.1", apply=apply,
            verify_timeout=0.2, publish_dir=os.path.join(self.tmp, "publish"),
            manager=manager, graph=graph, publish_client=FakeQB(),
        )

    def target(self):
        return {"target_site": "xingwan", "target_tid": "4365", "source_site": "opencd",
                "source_tid": "123", "source_name": "Album"}

    def test_missing_dry_run_never_writes(self):
        qb = FakeQB()
        rows = self.runner(qb).run([self.target()])
        self.assertEqual(rows[0]["status"], "READY_INJECT")
        self.assertEqual(qb.ops, [])

    def test_missing_apply_injects_paused_then_resumes_and_records(self):
        qb = FakeQB()
        graph = RoutingGraphManager(self.db)
        try:
            rows = self.runner(qb, apply=True, graph=graph).run([self.target()])
            self.assertEqual(rows[0]["status"], "INJECTED", rows[0])
            self.assertTrue(rows[0]["seed_verified"])
            self.assertTrue(rows[0]["tracker_verified"])
            self.assertEqual([op[0] for op in qb.ops], ["add", "resume"])
            self.assertTrue(qb.ops[0][1])
            self.assertTrue(qb.ops[0][2])
            lineage = graph.find_lineage(target_site="xingwan", target_tid="4365")
            self.assertEqual(lineage[0]["b_hash"], self.info_hash)
            self.assertTrue(lineage[0]["note"].startswith("recovery:"))
            event = graph._conn.execute(
                "SELECT event FROM pool_event_log WHERE info_hash=? ORDER BY id DESC LIMIT 1",
                (self.info_hash,),
            ).fetchone()
            self.assertEqual(event[0], "RECOVER")
        finally:
            graph.close()

    def test_unique_paused_resumes_only_after_exact_payload(self):
        qb = FakeQB([self.row()])
        graph = RoutingGraphManager(self.db)
        try:
            rows = self.runner(qb, apply=True, graph=graph).run([self.target()])
            self.assertEqual(rows[0]["status"], "RESUMED", rows[0])
            self.assertEqual([op[0] for op in qb.ops], ["resume"])
        finally:
            graph.close()

    def test_paused_size_mismatch_is_no_data_and_never_resumed(self):
        with open(os.path.join(self.album, "01.flac"), "ab") as fh:
            fh.write(b"corrupt")
        qb = FakeQB([self.row()])
        graph = RoutingGraphManager(self.db)
        try:
            rows = self.runner(qb, apply=True, graph=graph).run([self.target()])
            self.assertEqual(rows[0]["status"], "NO_DATA")
            self.assertEqual(qb.ops, [])
        finally:
            graph.close()

    def test_known_lineage_hash_mismatch_is_blocked(self):
        qb = FakeQB()
        target = self.target()
        target["b_hash"] = "0" * 40
        rows = self.runner(qb).run([target])
        self.assertEqual(rows[0]["status"], "IDENTITY_MISMATCH")
        self.assertEqual(qb.ops, [])

    def test_active_incomplete_is_not_reported_as_seeding(self):
        row = self.row(state="stalledDL")
        row.update({"progress": 0.0, "amount_left": 6144})
        qb = FakeQB([row])
        runner = self.runner(qb)
        name, files, size, info_hash = runner.manager.parse_torrent(self.data)
        result = runner.resume_unique(self.target(), self.data, "local_archive", name,
                                      files, size, info_hash, "resume")
        self.assertEqual(result["status"], "B_ACTIVE_INCOMPLETE")
        self.assertFalse(result["seed_verified"])
        self.assertEqual(qb.ops, [])

    def test_checking_resume_data_is_waited_without_reinject(self):
        class CheckingQB(FakeQB):
            def get_torrents(self, strict=False, hashes=None, **kwargs):
                rows = super().get_torrents(strict=strict, hashes=hashes, **kwargs)
                if hashes and any(r.get("state") == "checkingResumeData" for r in rows):
                    for source in self.torrents:
                        if source.get("hash") == hashes:
                            source["state"] = "stalledUP"
                return rows

        qb = CheckingQB([self.row(state="checkingResumeData")])
        runner = self.runner(qb)
        name, files, size, info_hash = runner.manager.parse_torrent(self.data)
        result = runner.resume_unique(self.target(), self.data, "local_archive", name,
                                      files, size, info_hash, "inject")
        self.assertEqual(result["status"], "INJECTED", result)
        self.assertTrue(result["seed_verified"])
        self.assertEqual(qb.ops, [])


if __name__ == "__main__":
    unittest.main()
