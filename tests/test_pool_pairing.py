# -*- coding: utf-8 -*-
"""发布期音乐池配对器单元测试 (PoolPairingManager)"""

import io
import os
import shutil
import tempfile
import unittest

import torf

from musichub.storage.pool_pairing import PoolPairingManager, PoolSpec


def _make_album(root: str, album: str = "Album") -> str:
    album_dir = os.path.join(root, album)
    os.makedirs(os.path.join(album_dir, "CD1"), exist_ok=True)
    with open(os.path.join(album_dir, "01.flac"), "wb") as f:
        f.write(b"a" * 4096)
    with open(os.path.join(album_dir, "CD1", "02.flac"), "wb") as f:
        f.write(b"b" * 2048)
    return album_dir


def _make_torrent(album_dir: str) -> bytes:
    t = torf.Torrent(path=album_dir, trackers=["http://tracker.example/announce"], private=True)
    t.generate()
    buf = io.BytesIO()
    t.write_stream(buf)
    return buf.getvalue()


class FakeQB:
    def __init__(self, info_hash: str):
        self.info_hash = info_hash
        self.torrents = []
        self.added = []

    def get_torrents(self, strict=True):
        return self.torrents

    def add_torrent(self, torrent_bytes, save_path="/downloads", is_paused=True,
                    is_skip_checking=False, tags=None, **kwargs):
        self.added.append({
            "save_path": save_path,
            "paused": is_paused,
            "skip_checking": is_skip_checking,
            "tags": tags,
        })
        self.torrents.append({
            "hash": self.info_hash,
            "progress": 1.0,
            "state": "pausedUP",
            "name": "Album",
        })
        return True


class SlowLandingQB(FakeQB):
    """注入成功但进度迟迟不到 100%（模拟磁盘排队 / recheck 未完成）"""

    def __init__(self, info_hash: str, progress: float = 0.4):
        super().__init__(info_hash)
        self.progress = progress

    def add_torrent(self, *args, **kwargs):
        super().add_torrent(*args, **kwargs)
        self.torrents[0]["progress"] = self.progress
        self.torrents[0]["state"] = "stalledDL"
        self.torrents[0]["amount_left"] = 1024
        return True


class TestPoolPairing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _manager(self, pool_root, fake=None):
        specs = [PoolSpec(name="music_pool_1", port=3210, host_dir=pool_root, host="127.0.0.1")]
        factory = (lambda host, port: fake) if fake else (lambda host, port: FakeQB(""))
        return PoolPairingManager(specs=specs, qb_factory=factory,
                                  verify_timeout=0.5, poll_interval=0.05)

    def test_parse_torrent_lists_files_with_root_prefix(self):
        album_dir = _make_album(self.tmp)
        data = _make_torrent(album_dir)
        mgr = self._manager(self.tmp)

        name, files, total, info_hash = mgr.parse_torrent(data)
        self.assertEqual(name, "Album")
        rels = [r for r, _ in files]
        self.assertIn("Album/01.flac", rels)
        self.assertIn("Album/CD1/02.flac", rels)
        self.assertEqual(total, 4096 + 2048)
        self.assertEqual(len(info_hash), 40)

    def test_find_owning_pool_and_music5_redline(self):
        pool = os.path.join(self.tmp, "pool")
        album = _make_album(pool)
        mgr = self._manager(pool)

        spec = mgr.find_owning_pool(album)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.name, "music_pool_1")

        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside, exist_ok=True)
        self.assertIsNone(mgr.find_owning_pool(outside))

        forbidden = os.path.join(self.tmp, "music5", "Album")
        os.makedirs(forbidden, exist_ok=True)
        self.assertIsNone(mgr.find_owning_pool(forbidden))

    def test_build_hardlink_payload_is_zero_copy(self):
        pool = os.path.join(self.tmp, "pool")
        os.makedirs(pool, exist_ok=True)
        src = _make_album(os.path.join(self.tmp, "src"))
        data = _make_torrent(src)
        mgr = self._manager(pool)
        _name, files, _total, _h = mgr.parse_torrent(data)

        ok, msg = mgr._build_hardlink_payload(pool, src, files)
        self.assertTrue(ok, msg)

        dst = os.path.join(pool, "Album", "CD1", "02.flac")
        org = os.path.join(src, "CD1", "02.flac")
        self.assertTrue(os.path.isfile(dst))
        self.assertEqual(os.stat(dst).st_ino, os.stat(org).st_ino)
        self.assertGreaterEqual(os.stat(dst).st_nlink, 2)

        verified, detail = mgr.verify_payload(pool, files)
        self.assertTrue(verified, detail)

    def test_pair_injects_paused_same_hash(self):
        pool = os.path.join(self.tmp, "pool")
        album = _make_album(pool)
        data = _make_torrent(album)
        info_hash = self._manager(pool).parse_torrent(data)[3]

        fake = FakeQB(info_hash)
        mgr = self._manager(pool, fake=fake)
        ok, msg = mgr.pair_published_torrent(data, album, expected_hash=info_hash)
        self.assertTrue(ok, msg)
        self.assertEqual(len(fake.added), 1)
        self.assertEqual(fake.added[0]["save_path"], "/downloads")
        self.assertTrue(fake.added[0]["paused"], "配对注入必须为 paused，避免同 hash 双实例同时公告")
        self.assertTrue(fake.added[0]["skip_checking"])

        # 二次配对幂等：已存在同 hash 且 100% 时直接成功，不重复注入
        ok2, msg2 = mgr.pair_published_torrent(data, album, expected_hash=info_hash)
        self.assertTrue(ok2, msg2)
        self.assertEqual(len(fake.added), 1)

    def test_pair_dry_run_does_not_touch_qb(self):
        pool = os.path.join(self.tmp, "pool")
        album = _make_album(pool)
        data = _make_torrent(album)
        fake = FakeQB("x" * 40)
        mgr = self._manager(pool, fake=fake)
        ok, msg = mgr.pair_published_torrent(data, album, dry_run=True)
        self.assertTrue(ok, msg)
        self.assertEqual(fake.added, [])

    def test_timeout_records_landing_and_blocks_cross_pool_reinject(self):
        """注入后 20s 内没到 100%（磁盘排队/recheck）也必须记账，否则下一轮换池再注 = 双活"""
        root_a = os.path.join(self.tmp, "music1")
        root_b = os.path.join(self.tmp, "music2")
        os.makedirs(root_a, exist_ok=True)
        os.makedirs(root_b, exist_ok=True)
        src = _make_album(os.path.join(self.tmp, "src"))
        data = _make_torrent(src)
        info_hash = self._manager(root_a).parse_torrent(data)[3]

        client_a = SlowLandingQB(info_hash)
        client_b = FakeQB("")
        specs = [PoolSpec("music1", 3220, root_a, "127.0.0.1"),
                 PoolSpec("music2", 3240, root_b, "127.0.0.1")]
        clients = {3220: client_a, 3240: client_b}
        mgr = PoolPairingManager(specs=specs, qb_factory=lambda h, p: clients[p],
                                 verify_timeout=0.3, poll_interval=0.05)

        # 预热两个快照缓存（真实链路里批量配对前就已拉过一次，缓存 60s 内不刷新）
        mgr.get_pool_snapshot()
        mgr.get_pool_snapshot(include_excluded=True)

        ok, msg = mgr.pair_published_torrent(data, src, expected_hash=info_hash)
        self.assertFalse(ok, "进度没到 100% 必须判失败，不能谎报成功")
        self.assertIn("已落地", msg)
        self.assertEqual(len(client_a.added), 1)

        # 关键断言：缓存里已有这个 hash ⇒ 下一轮选池必须复用 music1，绝不再注一份
        spec = mgr.select_target_pool(info_hash=info_hash)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.name, "music1")
        self.assertEqual(client_b.added, [], "不得把同 hash 再注入 music2")

        ok2, msg2 = mgr.pair_published_torrent(data, src, expected_hash=info_hash)
        self.assertFalse(ok2)
        self.assertIn("未完成", msg2, f"同池已存在但未完成时应如实报未完成: {msg2}")
        self.assertEqual(len(client_a.added), 1, "不得重复注入同一实例")


if __name__ == "__main__":
    unittest.main()
