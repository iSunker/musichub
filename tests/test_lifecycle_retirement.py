# -*- coding: utf-8 -*-
"""MusicHub 3.0 生命周期退役与 14+10 天门禁单元测试套件"""

import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
_mh = Path(__file__).resolve().parent.parent
if str(_mh) not in sys.path:
    sys.path.insert(0, str(_mh))

import unittest
import sqlite3
from unittest.mock import MagicMock

from musichub.routing.graph import RoutingGraphManager
from musichub.storage.safe_gc import SafeGarbageCollector
from musichub.storage.lifecycle import LifecycleRetirement


class TestLifecycleRetirement(unittest.TestCase):
    def setUp(self):
        # 内存数据库
        self.graph_mgr = RoutingGraphManager(db_path=":memory:")
        self.safe_gc = SafeGarbageCollector()
        self.mock_client = MagicMock()
        self.retirement = LifecycleRetirement(
            graph_mgr=self.graph_mgr,
            safe_gc=self.safe_gc,
            target_clients={"test_pool": self.mock_client},
            min_source_days=14.0,
            min_latest_days=10.0,
        )

    def test_forbidden_keywords_in_safe_gc(self):
        """测试 SafeGarbageCollector 对 music5 与系统保护目录的绝对拦截"""
        self.assertFalse(self.safe_gc.is_safe_target("/volume1/video/download/music/music5/AlbumA")[0])
        self.assertFalse(self.safe_gc.is_safe_target("/volume1/video/download/music/music1/incomplete/AlbumB")[0])
        self.assertFalse(self.safe_gc.is_safe_target("/volume1/video/download/music/music1/@eaDir")[0])
        self.assertFalse(self.safe_gc.is_safe_target("/volume1/video/download/music/music1/.RecycleBin")[0])
        self.assertTrue(self.safe_gc.is_safe_target("/volume1/video/download/music/music1/ValidAlbum")[0])

    def test_matrix_saturation_gate(self):
        """门禁 1：矩阵扩散充分度测试（未发满保护，全发满放行）"""
        fp = "test_fp_001"
        # 仅发布了 2 个站
        is_sat, reason, remaining = self.retirement.check_matrix_saturation(
            fp, {"musopia", "crabpt"}, title="中文专辑"
        )
        self.assertFalse(is_sat)
        self.assertIn("PENDING_EXPANSION", reason)
        self.assertTrue(len(remaining) > 0)

        # 发布了所有 11 个站
        all_sites = set(self.retirement.ALL_TARGET_SITES)
        is_sat_all, reason_all, remaining_all = self.retirement.check_matrix_saturation(
            fp, all_sites, title="中文专辑"
        )
        self.assertTrue(is_sat_all)
        self.assertEqual(reason_all, "ALL_SITES_PUBLISHED")
        self.assertEqual(len(remaining_all), 0)

    def test_ten_days_latest_site_protection(self):
        """门禁 2：最新发布站点未满 10 天时受到保护，绝不退役"""
        fp = "test_fp_002"
        # 模拟插入数据：首发满 20 天，但最新站点才发布 3 天
        conn = self.graph_mgr._conn
        conn.execute("""
            INSERT INTO site_publish_graph 
            (content_fingerprint, origin_site, origin_torrent_id, target_site, target_torrent_id, published_at)
            VALUES 
            (?, 'opencd', '1001', 'musopia', '2001', datetime('now', '-20 days')),
            (?, 'opencd', '1001', 'lemonhd', '2002', datetime('now', '-3 days'))
        """, (fp, fp))
        conn.commit()

        # 查询候选（即使关闭饱和度检查）
        cands = self.retirement.find_retireable_candidates(require_saturation=False)
        self.assertEqual(len(cands), 0, "最新发布站仅 3 天 (<10天)，必须被拦截！")

        # 模拟最新站点推进到 11 天前
        conn.execute("""
            UPDATE site_publish_graph 
            SET published_at = datetime('now', '-11 days') 
            WHERE content_fingerprint = ? AND target_site = 'lemonhd'
        """, (fp,))
        conn.commit()

        cands_after = self.retirement.find_retireable_candidates(require_saturation=False)
        self.assertEqual(len(cands_after), 1, "最新发布站满 11 天 (>=10天) 且源种满 20 天，应准入退役候选！")

    def test_fourteen_days_source_protection(self):
        """门禁 3：源种全局时间未满 14 天时拦截"""
        fp = "test_fp_003"
        conn = self.graph_mgr._conn
        conn.execute("""
            INSERT INTO site_publish_graph 
            (content_fingerprint, origin_site, origin_torrent_id, target_site, target_torrent_id, published_at)
            VALUES 
            (?, 'opencd', '1002', 'musopia', '3001', datetime('now', '-12 days'))
        """, (fp,))
        conn.commit()

        cands = self.retirement.find_retireable_candidates(require_saturation=False)
        self.assertEqual(len(cands), 0, "源种首发仅 12 天 (<14天)，必须被拦截！")

    def test_client_detach_delete_files_false_invariant(self):
        """铁律测试：客户端脱退时 delete_files 恒为 False"""
        self.mock_client.get_torrents.return_value = [
            {"hash": "abc12345", "name": "AlbumTest", "comment": "test_fp_safe"}
        ]
        self.mock_client.delete_torrent.return_value = True

        res = self.retirement.retire_album_across_clients("test_fp_safe", dry_run=False)
        self.assertEqual(res["detached_tasks"], 1)
        # 断言调用参数恒为 delete_files=False
        self.mock_client.delete_torrent.assert_called_with("abc12345", delete_files=False)


if __name__ == "__main__":
    unittest.main()
