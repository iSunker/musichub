# -*- coding: utf-8 -*-
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock

from musichub.routing.graph import RoutingGraphManager
from musichub.tools.backfill_matrix import BackfillMatrixPump, ALL_TARGET_SITES


class TestBackfillMatrix(unittest.TestCase):
    def test_least_missing_first_and_music5_forbidden_and_backpressure(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            mgr = RoutingGraphManager(db_path=db_path)

            pool1 = os.path.join(tmp, "music_pool_1")
            pool5 = os.path.join(tmp, "music5")
            os.makedirs(os.path.join(pool1, "AlbumFewMissing"))
            os.makedirs(os.path.join(pool1, "AlbumManyMissing"))
            os.makedirs(os.path.join(pool5, "AlbumMusic5"))

            # Insert 3 source_tasks
            mgr.upsert_source_task("1" * 40, "opencd", "1001", "AlbumFewMissing")
            mgr.update_source_meta("1" * 40, artist="A1", album="Few", meta_status="OK")

            mgr.upsert_source_task("2" * 40, "opencd", "1002", "AlbumManyMissing")
            mgr.update_source_meta("2" * 40, artist="A2", album="Many", meta_status="OK")

            mgr.upsert_source_task("3" * 40, "opencd", "1003", "AlbumMusic5")
            mgr.update_source_meta("3" * 40, artist="A3", album="M5", meta_status="OK")

            # Make AlbumFewMissing already published to 10 sites (only missing 2)
            conn = sqlite3.connect(db_path)
            for s in ALL_TARGET_SITES[:10]:
                conn.execute(
                    "INSERT INTO publish_lineage (source_hash, source_site, source_tid, source_name, target_site, target_tid, status) VALUES (?, 'opencd', '1001', 'AlbumFewMissing', ?, '999', 'PUBLISHED')",
                    ("1" * 40, s),
                )
            conn.commit()
            conn.close()
            mgr.close()

            mock_stock = MagicMock()
            mock_stock.pools = [pool1, pool5]
            mock_stock.graph_mgr = RoutingGraphManager(db_path=db_path)
            mock_stock.adapters_map = {s: MagicMock() for s in ALL_TARGET_SITES}
            mock_stock.publish_qb.get_torrents.return_value = [{} for _ in range(10)]

            pump = BackfillMatrixPump(db_path=db_path, stock_pump=mock_stock, max_3010_torrents=200)
            albums, missing_by_site = pump.plan_backfill_queue()

            # music5 excluded
            names = [a["row"]["source_name"] for a in albums]
            self.assertNotIn("AlbumMusic5", names)
            # AlbumFewMissing has fewer missing sites -> must come first
            self.assertEqual(names[0], "AlbumFewMissing")
            self.assertLess(albums[0]["missing_count"], albums[1]["missing_count"])

            # Dry-run picks
            res = pump.run_backfill_round(max_per_round=12, dry_run=True)
            self.assertFalse(res["skipped_backpressure"])
            self.assertGreaterEqual(res["published"], 1)

            # Backpressure check when 3010 >= 200 (apply mode)
            mock_stock.publish_qb.get_torrents.return_value = [{} for _ in range(200)]
            res_bp = pump.run_backfill_round(max_per_round=12, dry_run=False)
            self.assertTrue(res_bp["skipped_backpressure"])
            mock_stock.graph_mgr.close()


if __name__ == "__main__":
    unittest.main()
