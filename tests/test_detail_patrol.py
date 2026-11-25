# -*- coding: utf-8 -*-
"""MusicHub 全矩阵 12 站点详情常态化抓取、标签入库与调度巡检单元测试。"""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from musichub.onsite.detail_archive import DetailArchive
from musichub.routing.graph import RoutingGraphManager


class TestDetailPatrolAndTags(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_patrol.db")
        self.graph = RoutingGraphManager(self.db_path)

    def tearDown(self):
        try:
            self.graph.close()
        except Exception:
            pass
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_migrate_detail_pages_tags_column(self):
        """验证 site_detail_pages 自动迁移并补全 tags_json 列"""
        cols = {r[1] for r in self.graph._conn.execute("PRAGMA table_info(site_detail_pages)")}
        self.assertIn("tags_json", cols)

        # 再次执行迁移，验证幂等性
        self.graph._migrate_detail_pages_columns()
        cols_after = {r[1] for r in self.graph._conn.execute("PRAGMA table_info(site_detail_pages)")}
        self.assertIn("tags_json", cols_after)

    def test_record_detail_page_with_explicit_tags(self):
        """验证 record_detail_page 将 tags_json 正确原子持久化到 site_detail_pages"""
        tags = [{"name": "华语", "id": "1"}, {"name": "流行", "id": "5"}]
        tags_json_str = json.dumps(tags, ensure_ascii=False)

        page = {
            "site": "uhddolby",
            "tid": "257139",
            "url": "https://www.uhddolby.com/details.php?id=257139",
            "http_status": 200,
            "fetch_status": "OK",
            "title": "刘欢-六十年代生人-无损版",
            "descr_text": "测试描述",
            "cover_urls_json": "[]",
            "tags_json": tags_json_str,
            "parsed_json": json.dumps({"tags": tags, "seeders": 12, "leechers": 1}),
            "html_gzip": b"fake_gzip",
            "html_sha256": "fake_sha256",
            "fetched_at": time.time(),
            "parser_version": "detail-v1",
            "error": "",
        }
        obs = {
            "page_status": "OK",
            "seeders": 12,
            "leechers": 1,
            "completed": 50,
            "html_sha256": "fake_sha256",
            "fetched_at": time.time(),
        }

        self.graph.record_detail_page(page, obs)

        # 查表核验
        row = self.graph.get_detail_page("uhddolby", "257139")
        self.assertIsNotNone(row)
        self.assertEqual(row["site"], "uhddolby")
        self.assertEqual(row["tid"], "257139")
        self.assertEqual(row["fetch_status"], "OK")
        self.assertEqual(row["tags_json"], tags_json_str)

        # 核验 observations
        history = self.graph.get_detail_history("uhddolby", "257139")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["seeders"], 12)
        self.assertEqual(history[0]["completed"], 50)

    def test_list_pending_detail_targets_priority(self):
        """验证待抓取目标调度算法：从未抓取 > 上次失败 > 最旧过期，且站点间均匀交替"""
        # 写入多个站点的已发布记录
        self.graph._conn.execute("""
            INSERT INTO site_publish_graph (content_fingerprint, origin_site, target_site, target_torrent_id, publish_status)
            VALUES 
                ('fp_1', 'opencd', 'uhddolby', '101', 'PUBLISHED'),
                ('fp_2', 'opencd', 'uhddolby', '102', 'PUBLISHED'),
                ('fp_3', 'opencd', 'musopia', '201', 'PUBLISHED'),
                ('fp_4', 'opencd', 'xingwan', '301', 'PUBLISHED')
        """)
        self.graph._conn.commit()

        # 此时全部 4 个均未抓取
        targets = self.graph.list_pending_detail_targets(limit=10)
        self.assertEqual(len(targets), 4)

        # 标记 uhddolby 101 为已抓取成功（新鲜时间）
        now = time.time()
        self.graph.record_detail_page({
            "site": "uhddolby", "tid": "101", "fetch_status": "OK", "fetched_at": now,
        }, {"seeders": 5})

        # 标记 uhddolby 102 为抓取失败
        self.graph.record_detail_page({
            "site": "uhddolby", "tid": "102", "fetch_status": "FETCH_ERROR", "fetched_at": now,
        }, {"seeders": 0})

        # 再次查询：未抓取的 musopia(201)、xingwan(301) 与 失败的 uhddolby(102) 必须优先被列出，成功新鲜的 101 不应出现
        pending = self.graph.list_pending_detail_targets(limit=10, max_age_seconds=3600.0)
        pending_keys = [(p["site"], p["tid"]) for p in pending]
        self.assertIn(("uhddolby", "102"), pending_keys)
        self.assertIn(("musopia", "201"), pending_keys)
        self.assertIn(("xingwan", "301"), pending_keys)
        self.assertNotIn(("uhddolby", "101"), pending_keys)

        # 验证站点交替打散 Round-Robin
        sites_sequence = [p["site"] for p in pending]
        # 不应所有 uhddolby 堆叠在前
        self.assertTrue(len(set(sites_sequence[:2])) > 1 or len(pending) <= 1)

    def test_topup_target_metadata_in_daemon(self):
        """验证 MusicHubDaemon.topup_target_metadata 的常态化抓取与反哺源种链路"""
        from musichub.daemon import MusicHubDaemon
        from musichub.core.config import MusicHubConfig

        # 准备数据：登记一个源种任务，并在 site_publish_graph 发布到 uhddolby
        info_hash = "a" * 40
        self.graph.upsert_source_task(
            info_hash=info_hash,
            source_site="opencd",
            source_tid="999",
            source_name="Test Album",
        )
        self.graph._conn.execute("""
            INSERT INTO site_publish_graph (content_fingerprint, origin_site, origin_torrent_id, target_site, target_torrent_id, publish_status)
            VALUES ('fp_test', 'opencd', '999', 'uhddolby', '257139', 'PUBLISHED')
        """)
        self.graph._conn.execute("""
            INSERT INTO publish_lineage (source_hash, source_site, source_tid, target_site, target_tid, status)
            VALUES (?, 'opencd', '999', 'uhddolby', '257139', 'PUBLISHED')
        """, (info_hash,))
        self.graph._conn.commit()

        # Mock Daemon 的外部依赖
        with patch("musichub.daemon.QBClient"), \
             patch("musichub.daemon.AudioInspector"), \
             patch("musichub.daemon.DeduplicationGateway"), \
             patch("musichub.daemon.SeedPatrolManager"), \
             patch("musichub.daemon.SpeedGovernor"), \
             patch("musichub.daemon.HandoffGovernor"), \
             patch("musichub.daemon.StockMatrixPump"), \
             patch("musichub.daemon.DelayedReaddQueue"), \
             patch("musichub.daemon.LifecycleRetirement"), \
             patch("musichub.daemon.StorageBackpressure"), \
             patch("time.sleep", return_value=None):

            cfg = MusicHubConfig()
            cfg.db_path = self.db_path
            daemon = MusicHubDaemon(config=cfg)

            # Mock DetailArchive.archive
            fake_tags = [{"name": "国语", "id": "10"}, {"name": "流行", "id": "20"}]
            fake_tags_json = json.dumps(fake_tags, ensure_ascii=False)

            daemon.detail_archive.archive = MagicMock(return_value={
                "page": {
                    "site": "uhddolby",
                    "tid": "257139",
                    "fetch_status": "OK",
                    "tags_json": fake_tags_json,
                    "seeders": 10,
                    "leechers": 0,
                    "completed": 88,
                },
                "observation": {"seeders": 10},
                "written": True,
            })

            done = daemon.topup_target_metadata(limit=5, save_html=False)
            self.assertEqual(done, 1)

            # 验证源种 source_tasks 收到反哺的 tags_json
            st = self.graph.get_source_task(info_hash)
            self.assertEqual(st.get("tags_json"), fake_tags_json)
            daemon.graph_mgr.close()


if __name__ == "__main__":
    unittest.main()
