# -*- coding: utf-8 -*-
import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from musichub.storage.handoff import PoolConfig
from musichub.core.torrent_tools import bencode
from musichub.tools.hr_guard import (
    HRGuard,
    parse_musopia_myhr_html,
    parse_opencd_torrents_html,
)


def _make_torrent(name: str, data: bytes) -> tuple[bytes, str]:
    info = {
        b"length": len(data),
        b"name": name.encode("utf-8"),
        b"piece length": 16384,
        b"pieces": hashlib.sha1(data).digest(),
    }
    raw = bencode({b"announce": b"https://example.com/announce", b"info": info})
    ih = hashlib.sha1(bencode(info)).hexdigest().lower()
    return raw, ih


class TestHRGuard(unittest.TestCase):
    def test_parse_musopia_myhr_html(self):
        html = """
        <table>
          <tr><th>H&R ID</th><th>种子名称</th><th>上传</th><th>下载</th><th>分享率</th><th>还需做种时间</th><th>完成时间</th><th>剩余考察时间</th></tr>
          <tr>
            <td>309117</td>
            <td><a href="details.php?id=21628">张学友 - 吻别 1993 WAV</a></td>
            <td>0.00 KB</td><td>970 MB</td><td>0.000</td><td>5天09:19:38</td><td>2026-11-24</td><td>14天03:53:04</td>
          </tr>
        </table>
        """
        items = parse_musopia_myhr_html(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tid"], "21628")
        self.assertEqual(items[0]["hr_id"], "309117")
        self.assertEqual(items[0]["need_time"], "5天09:19:38")

    def test_parse_opencd_torrents_html(self):
        html = """
        <table class="torrents">
          <tr><td><a href="details.php?id=207266" title="Album A">Album A</a></td></tr>
          <tr><td><a href="details.php?id=207266">Album A</a></td></tr>
          <tr><td><a href="details.php?id=207265" title="Album B">Album B</a></td></tr>
        </table>
        """
        items = parse_opencd_torrents_html(html, kind="hr_risk")
        self.assertEqual([x["tid"] for x in items], ["207266", "207265"])

    def test_run_resume_and_repair_and_music5_forbidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool1 = os.path.join(tmp, "music_pool_1")
            pool5 = os.path.join(tmp, "music5")
            os.makedirs(pool1)
            os.makedirs(pool5)

            # 1) Album for repair in pool1
            t_bytes_repair, ih_repair = _make_torrent("AlbumRepair.flac", b"hello-repair")
            with open(os.path.join(pool1, "AlbumRepair.flac"), "wb") as f:
                f.write(b"hello-repair")

            # 2) Album only in forbidden music5 -> must NOT be repaired
            t_bytes_forb, ih_forb = _make_torrent("AlbumMusic5.flac", b"hello-forb")
            with open(os.path.join(pool5, "AlbumMusic5.flac"), "wb") as f:
                f.write(b"hello-forb")

            # 3) Album paused in pool1
            t_bytes_paused, ih_paused = _make_torrent("AlbumPaused.flac", b"hello-paused")

            pools = [
                PoolConfig(name="music_pool_1", port=3210, mount_dir=pool1, host="127.0.0.1"),
                PoolConfig(name="music_pool_5", port=3250, mount_dir=pool5, host="127.0.0.1"),
            ]
            guard = HRGuard(db_path=os.path.join(tmp, "none.db"), torrents_dir=tmp, host="127.0.0.1", pools=pools)
            guard.fetch_musopia_hr = MagicMock(return_value=[
                {"site": "musopia", "tid": "101", "title": "Repair"},
                {"site": "musopia", "tid": "102", "title": "Forbidden"},
                {"site": "musopia", "tid": "103", "title": "Paused"},
            ])
            guard.fetch_opencd_hr = MagicMock(return_value=[])

            bytes_map = {
                ("musopia", "101"): t_bytes_repair,
                ("musopia", "102"): t_bytes_forb,
                ("musopia", "103"): t_bytes_paused,
            }
            guard.get_torrent_bytes = lambda site, tid: bytes_map.get((site, tid))

            mock_qb = MagicMock()
            mock_qb.resume_torrent.return_value = True
            mock_qb.add_torrent_file.return_value = True
            mock_qb.get_torrents.return_value = [{"hash": ih_repair, "progress": 1.0}]

            guard._build_qb_clients = lambda: [("music_pool_1", 3210, mock_qb, pool1)]
            guard.scan_cluster_torrents = lambda clients: {
                ih_paused: [{"instance": "music_pool_1", "port": 3210, "qb": mock_qb, "state": "stoppedup", "progress": 1.0}]
            }

            with patch("musichub.tools.hr_guard.QBClient", return_value=mock_qb):
                res = guard.run(sites=("musopia",), apply=True)

            self.assertEqual(res["total_hr"], 3)
            self.assertEqual(res["resumed"], 1)
            self.assertEqual(res["repaired"], 1)
            self.assertEqual(res["missing_data"], 1)
            statuses = {d["tid"]: d["status"] for d in res["details"]}
            self.assertEqual(statuses["101"], "REPAIRED")
            self.assertEqual(statuses["102"], "MISSING_DATA")
            self.assertEqual(statuses["103"], "RESUMED")

    def test_daemon_1010_window(self):
        from musichub.daemon import MusicHubDaemon
        tz8 = timezone(timedelta(hours=8))
        dummy = MagicMock()
        dummy._last_hr_guard_date = ""
        dummy.config.db_path = ":memory:"

        with patch("musichub.tools.hr_guard.HRGuard") as MockGuard:
            MockGuard.return_value.run.return_value = {"total_hr": 0, "healthy": 0, "resumed": 0, "repaired": 0, "missing_data": 0}
            # 10:09 -> should not run
            r1 = MusicHubDaemon._maybe_run_daily_hr_guard(dummy, datetime(2026, 11, 25, 10, 9, tzinfo=tz8))
            self.assertIsNone(r1)
            # 10:12 -> should run once
            r2 = MusicHubDaemon._maybe_run_daily_hr_guard(dummy, datetime(2026, 11, 25, 10, 12, tzinfo=tz8))
            self.assertIsNotNone(r2)
            # 10:25 same day -> should not run again
            r3 = MusicHubDaemon._maybe_run_daily_hr_guard(dummy, datetime(2026, 11, 25, 10, 25, tzinfo=tz8))
            self.assertIsNone(r3)


if __name__ == "__main__":
    unittest.main()
