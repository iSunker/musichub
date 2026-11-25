# -*- coding: utf-8 -*-
import unittest
import os
import tempfile
import shutil
import sqlite3

from musichub.core.models import CanonicalMusicItem, AudioTrackInfo
from musichub.routing.graph import RoutingGraphManager
from musichub.publisher.gateway import DeduplicationGateway
from musichub.adapters.base import BaseSiteAdapter
from musichub.storage.backpressure import StorageBackpressure
from musichub.storage.safe_gc import SafeGarbageCollector
from musichub.storage.handoff import ReadyHandoffManager
from musichub.publisher.engine import PublisherEngine


class DummyAdapter(BaseSiteAdapter):
    def __init__(self, site_name="Musopia"):
        super().__init__(site_name=site_name, config={})

    def check_exists(self, item):
        return False, "Not exists"

    def format_title(self, item):
        return f"[FLAC] {item.artist} - {item.album}", "sub"

    def build_body(self, item):
        return "body"

    def build_form_fields(self, item):
        return {"type": "401"}

    def publish(self, item, torrent_path):
        return True, "12345", b"d8:announce0:e"


class DummyQB:
    def __init__(self):
        self.torrents = []
        self.resumed = []
        self.deleted = []

    def get_torrents(self):
        return self.torrents

    def add_torrent_file(self, torrent_bytes, **kw):
        return True

    def resume_torrent(self, h):
        self.resumed.append(h)
        return True

    def delete_torrent(self, h, delete_files=False):
        self.deleted.append((h, delete_files))
        return True


class HandoffFakeQB:
    """源种做种链测试桩：记录注入/删除/启停，可按需返回 torrent 字节与任务清单"""

    def __init__(self, port=3016, torrent_bytes=b"", torrents=None):
        self.port = port
        self.torrent_bytes = torrent_bytes
        self.torrents = torrents or []
        self.added = []
        self.deleted = []
        self.stopped = []
        self.resumed = []

    def export_torrent(self, info_hash):
        return self.torrent_bytes

    def get_torrents(self, hashes=None, **kwargs):
        return self.torrents

    def add_torrent(self, torrent_bytes, save_path="/downloads", is_paused=True,
                    is_skip_checking=False, tags=None, **kwargs):
        self.added.append({
            "save_path": save_path,
            "is_paused": is_paused,
            "skip_checking": is_skip_checking,
            "tags": tags or "",
        })
        return True

    def add_torrent_file(self, torrent_bytes, **kwargs):
        return self.add_torrent(
            torrent_bytes,
            save_path=kwargs.get("save_path", "/downloads"),
            is_paused=kwargs.get("paused", True),
            is_skip_checking=kwargs.get("skip_checking", False),
            tags=kwargs.get("tags"),
        )

    def stop_torrent(self, hashes):
        self.stopped.append(hashes)
        return True

    def resume_torrent(self, hashes):
        self.resumed.append(hashes)
        return True

    def delete_torrent(self, hashes, delete_files=False):
        self.deleted.append((hashes, delete_files))
        return True


class FakePoolManager:
    """固定路由的池管理器替身（route_target 直接返回给定目标）"""

    def __init__(self, dst_dir, client, pool_name):
        self.dst_dir = dst_dir
        self.client = client
        self.pool_name = pool_name

    def route_target(self, origin_site):
        return self.dst_dir, self.client, self.pool_name


class TestMusicHub(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.graph_mgr = RoutingGraphManager(db_path=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_canonical_fingerprint(self):
        item = CanonicalMusicItem(
            title="Jay Chou - Fantasy",
            artist="Jay Chou",
            album="Fantasy",
            year="2001",
            format="FLAC",
            medium="CD"
        )
        fp = item.calculate_content_fingerprint()
        self.assertTrue(len(fp) == 64)

    def test_routing_graph_loop_prevention(self):
        item = CanonicalMusicItem(
            title="Jay Chou - Fantasy",
            artist="Jay Chou",
            album="Fantasy",
            year="2001",
            origin_site="OpenCD"
        )
        adapter = DummyAdapter(site_name="Musopia")
        gateway = DeduplicationGateway(adapters=[adapter], graph_mgr=self.graph_mgr)

        # 首次决策：允许发布
        decisions = gateway.evaluate_targets(item)
        self.assertTrue(decisions["Musopia"][0])

        # 模拟发布成功记录
        self.graph_mgr.record_publish(item, "Musopia", target_torrent_id="9999")

        # 再次决策：防环/防重生效，不允许重复发布
        decisions2 = gateway.evaluate_targets(item)
        self.assertFalse(decisions2["Musopia"][0])
        self.assertIn("already published", decisions2["Musopia"][1].lower())

    def test_publish_lineage_recording(self):
        item = CanonicalMusicItem(
            title="Brown Spirits - Solitary Transmissions",
            artist="Brown Spirits",
            album="Solitary Transmissions",
            year="2023",
            origin_site="opencd",
            origin_torrent_id="207002",
            origin_info_hash="a" * 40,
            local_path="/volume1/video/download/music/music2/(2023) Brown Spirits",
        )
        self.graph_mgr.record_publish(
            item, "lemonhd", target_torrent_id="48704",
            b_hash="d1da9e885c04be01425cb1ed8265272a1f1218c7",
            target_url="https://lemonhd.net/details.php?id=48704",
            qb_instance="publish", qb_port=3010, save_path="/downloads",
        )
        rows = self.graph_mgr.find_lineage(target_tid="48704", target_site="lemonhd")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["b_hash"], "d1da9e885c04be01425cb1ed8265272a1f1218c7")
        self.assertEqual(row["source_hash"], "a" * 40)
        self.assertEqual(row["source_tid"], "207002")
        self.assertEqual(row["target_url"], "https://lemonhd.net/details.php?id=48704")
        # 按源种 hash 与 B 种 hash 都能反查
        self.assertEqual(len(self.graph_mgr.find_lineage(source_hash="a" * 40)), 1)
        self.assertEqual(len(self.graph_mgr.find_lineage(b_hash="D1DA9E885C04BE01425CB1ED8265272A1F1218C7")), 1)

    def test_site_tid_columns_on_publish(self):
        """每个站点各记一列 tid；origin_torrent_id 保持原值不动"""
        item = CanonicalMusicItem(
            title="Brown Spirits - Solitary Transmissions",
            artist="Brown Spirits",
            album="Solitary Transmissions",
            year="2023",
            origin_site="OpenCD",
            origin_torrent_id="207002",
        )
        fp = item.calculate_content_fingerprint()
        self.graph_mgr.record_publish(item, "musopia", target_torrent_id="28125", b_hash="b" * 40)
        self.graph_mgr.record_publish(item, "crabpt", target_torrent_id="51234", b_hash="c" * 40)
        # 第三站登记为 EXISTING：没有 tid，但不得冲掉既有站点 tid
        self.graph_mgr.record_publish(item, "xingwan", target_torrent_id="EXISTING", status="EXISTING")

        tids = self.graph_mgr.get_site_tids(fp)
        self.assertEqual(tids.get("opencd"), "207002")
        self.assertEqual(tids.get("musopia"), "28125")
        self.assertEqual(tids.get("crabpt"), "51234")
        self.assertNotIn("xingwan", tids)

        row = self.graph_mgr._conn.execute(  # noqa: SLF001 - 校验落库列
            "SELECT origin_torrent_id, opencd_tid, musopia_tid, crabpt_tid "
            "FROM site_publish_graph WHERE content_fingerprint = ? AND target_site = 'musopia'",
            (fp,),
        ).fetchone()
        # 原定义字段逐字节保留
        self.assertEqual(row["origin_torrent_id"], "207002")
        self.assertEqual(row["opencd_tid"], "207002")
        self.assertEqual(row["musopia_tid"], "28125")
        # 同内容的其他站行也补齐（不靠记录顺序）
        self.assertEqual(row["crabpt_tid"], "51234")

        # infohash / 专辑名形态的源 tid 不得写进站点列
        item2 = CanonicalMusicItem(
            title="X - Y", artist="X", album="Y", year="2020",
            origin_site="opencd", origin_torrent_id="d" * 40,
        )
        fp2 = item2.calculate_content_fingerprint()
        self.graph_mgr.record_publish(item2, "musopia", target_torrent_id="9999")
        self.assertNotIn("opencd", self.graph_mgr.get_site_tids(fp2))

    def test_site_tid_columns_migration_backfills_legacy_db(self):
        """老库（无 tid 列）打开时自动 ALTER + 回填，且不改 origin_torrent_id"""
        legacy_db = os.path.join(self.test_dir, "legacy.db")
        conn = sqlite3.connect(legacy_db)
        conn.execute("""
        CREATE TABLE site_publish_graph (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_fingerprint TEXT NOT NULL,
            stream_fingerprint TEXT,
            origin_site TEXT NOT NULL,
            origin_torrent_id TEXT,
            target_site TEXT NOT NULL,
            target_torrent_id TEXT,
            published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(content_fingerprint, target_site)
        )
        """)
        # 同一内容的两种目标站行：一行 origin_torrent_id 是数字，一行是专辑名
        conn.execute(
            "INSERT INTO site_publish_graph (content_fingerprint, origin_site, origin_torrent_id, "
            "target_site, target_torrent_id) VALUES ('fp1', 'opencd', '198601', 'musopia', '28125')"
        )
        conn.execute(
            "INSERT INTO site_publish_graph (content_fingerprint, origin_site, origin_torrent_id, "
            "target_site, target_torrent_id) VALUES ('fp1', 'opencd', "
            "'(2023) Brown Spirits - Solitary Transmissions [FLAC]', 'crabpt', '51234')"
        )
        # infohash 形态永远不写进站点列
        conn.execute(
            "INSERT INTO site_publish_graph (content_fingerprint, origin_site, origin_torrent_id, "
            "target_site, target_torrent_id) VALUES ('fp2', 'opencd', ?, 'xingwan', '777')",
            ("e" * 40,),
        )
        conn.commit()
        conn.close()

        mgr = RoutingGraphManager(db_path=legacy_db)
        try:
            cols = {r[1] for r in mgr._conn.execute("PRAGMA table_info(site_publish_graph)")}
            self.assertIn("opencd_tid", cols)
            self.assertIn("crabpt_tid", cols)
            self.assertIn("hdtime_tid", cols)

            tids = mgr.get_site_tids("fp1")
            self.assertEqual(tids.get("opencd"), "198601")
            self.assertEqual(tids.get("musopia"), "28125")
            self.assertEqual(tids.get("crabpt"), "51234")

            self.assertNotIn("opencd", mgr.get_site_tids("fp2"))
            self.assertEqual(mgr.get_site_tids("fp2").get("xingwan"), "777")

            raw = mgr._conn.execute(
                "SELECT origin_torrent_id FROM site_publish_graph "
                "WHERE content_fingerprint = 'fp1' AND target_site = 'crabpt'"
            ).fetchone()[0]
            self.assertEqual(raw, "(2023) Brown Spirits - Solitary Transmissions [FLAC]")
        finally:
            mgr.close()

    def test_safe_gc_redline_music5(self):
        gc = SafeGarbageCollector(managed_pools=[
            "/volume1/video/download/music/music1",
            "/volume1/video/download/music/music2"
        ])
        # 1. 尝试操作 music5，必须被拦截
        safe, reason = gc.is_safe_target("/volume1/video/download/music/music5/my_album")
        self.assertFalse(safe)
        self.assertIn("music5", reason)

        # 2. 尝试操作非受控池，必须被拦截
        safe, reason = gc.is_safe_target("/volume1/video/download/movies/movie1")
        self.assertFalse(safe)
        self.assertIn("outside managed pools", reason)

        # 3. 操作白名单正常目录
        safe, reason = gc.is_safe_target("/volume1/video/download/music/music1/album_abc")
        self.assertTrue(safe)

        # 4. 跨池孤儿探测与安全回收测试
        pool1 = os.path.join(self.test_dir, "music1")
        pool2 = os.path.join(self.test_dir, "music2")
        os.makedirs(os.path.join(pool1, "orphan_album"), exist_ok=True)
        os.makedirs(os.path.join(pool2, "orphan_album"), exist_ok=True)
        os.makedirs(os.path.join(pool1, "active_album"), exist_ok=True)

        gc_test = SafeGarbageCollector(managed_pools=[pool1, pool2])
        # 假设 active_album 在活跃做种集合中
        active_set = {"active_album"}
        orphans = gc_test.scan_unreferenced_orphans(active_set)
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["name"], "orphan_album")
        self.assertEqual(orphans[0]["pool_count"], 2)

        # Dry-run 模拟回收
        ok_dry, msg_dry = gc_test.safe_reclaim_orphan_cross_pools(orphans[0], dry_run=True)
        self.assertTrue(ok_dry)
        self.assertIn("DRY-RUN", msg_dry)
        self.assertTrue(os.path.exists(os.path.join(pool1, "orphan_album")))

        # 真实回收
        ok_real, msg_real = gc_test.safe_reclaim_orphan_cross_pools(orphans[0], dry_run=False)
        self.assertTrue(ok_real)
        self.assertFalse(os.path.exists(os.path.join(pool1, "orphan_album")))
        self.assertFalse(os.path.exists(os.path.join(pool2, "orphan_album")))

    def test_ready_handoff_and_gate_a(self):
        qb_dummy = DummyQB()
        handoff = ReadyHandoffManager(qb_download=qb_dummy)

        # 测试闸门 A：空路径或 0 字节文件被拦截
        fake_ssd = os.path.join(self.test_dir, "fake_album")
        os.makedirs(fake_ssd, exist_ok=True)
        ok, msg, sz = handoff.verify_physical_payload(fake_ssd)
        self.assertFalse(ok)
        self.assertIn("Payload empty", msg)

        # 放入正常文件（>1MB）
        test_file = os.path.join(fake_ssd, "01.flac")
        with open(test_file, "wb") as f:
            f.write(b"0" * (1024 * 1024 + 10))

        ok, msg, sz = handoff.verify_physical_payload(fake_ssd)
        self.assertTrue(ok)
        self.assertGreater(sz, 1024 * 1024)

        # 测试搬运
        target_dir = os.path.join(self.test_dir, "target_pool")
        copy_ok, copy_msg, final_path = handoff.copy_stream_to_hdd(fake_ssd, target_dir)
        self.assertTrue(copy_ok)
        self.assertTrue(os.path.exists(final_path))
        self.assertTrue(os.path.exists(os.path.join(final_path, "01.flac")))

    def test_audio_inspector_name_metadata_parsing(self):
        from musichub.metadata.audio_inspector import AudioInspector

        # 测试案例 1：完整标准命名
        meta1 = AudioInspector.parse_name_metadata("[2003] Jay Chou - Ye Hui Mei [FLAC] [CD]")
        self.assertEqual(meta1["year"], "2003")
        self.assertEqual(meta1["artist"], "Jay Chou")
        self.assertEqual(meta1["album"], "Ye Hui Mei")
        self.assertEqual(meta1["format"], "FLAC")
        self.assertEqual(meta1["medium"], "CD")

        # 测试案例 2：带 Hi-Res / Deluxe 版本与括号年份
        meta2 = AudioInspector.parse_name_metadata("Adele - 30 (2021) [FLAC 24-96] [WEB-DL] [Deluxe]")
        self.assertEqual(meta2["year"], "2021")
        self.assertEqual(meta2["artist"], "Adele")
        self.assertEqual(meta2["album"], "30")
        self.assertEqual(meta2["format"], "FLAC")
        self.assertEqual(meta2["medium"], "WEB-DL")
        self.assertEqual(meta2["standard"], "Hi-Res")
        self.assertIn("Deluxe", meta2["edition"])

    def test_audio_inspector_multidisc_and_cue_healing(self):
        from musichub.metadata.audio_inspector import AudioInspector

        # 1. 模拟多碟目录 CD1, CD2
        album_root = os.path.join(self.test_dir, "MultiDiscAlbum")
        cd1 = os.path.join(album_root, "CD1")
        cd2 = os.path.join(album_root, "CD2")
        os.makedirs(cd1, exist_ok=True)
        os.makedirs(cd2, exist_ok=True)

        track1_path = os.path.join(cd1, "01.Track01.flac")
        track2_path = os.path.join(cd2, "01.Track01.flac")
        with open(track1_path, "wb") as f:
            f.write(b"RIFF" + b"A" * 150000)
        with open(track2_path, "wb") as f:
            f.write(b"RIFF" + b"B" * 150000)

        # 模拟 CUE 文件（故意指向错误的 .wav 文件，测试自愈为 .flac）
        cue_path = os.path.join(cd1, "album.cue")
        with open(cue_path, "w", encoding="utf-8") as f:
            f.write('FILE "01.Track01.wav" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n')

        tracks, summary = AudioInspector.scan_directory(album_root)
        self.assertEqual(len(tracks), 2)
        # 验证自然排序：CD1 在前，CD2 在后
        self.assertIn("CD1", tracks[0].file_path)
        self.assertIn("CD2", tracks[1].file_path)
        # 验证提取到的分块音轨哈希存在且不为空
        self.assertTrue(bool(tracks[0].stream_md5))
        self.assertNotEqual(tracks[0].stream_md5, tracks[1].stream_md5)

        # 验证 CUE 自愈效果：应被自动纠正为 "01.Track01.flac" FLAC
        with open(cue_path, "r", encoding="utf-8") as f:
            cue_content = f.read()
        self.assertIn('FILE "01.Track01.flac" FLAC', cue_content)

    def test_qb_client_add_torrent_file_signature(self):
        from musichub.core.qb_client import QBClient
        client = QBClient()
        # 验证方法存在并可被调用（通过 mock 或检查签名）
        self.assertTrue(hasattr(client, "add_torrent_file"))
        self.assertTrue(hasattr(client, "add_torrent"))


    def test_adapters_pre_publish_check_and_form_fields(self):
        from musichub.adapters.sites import (
            MusopiaAdapter, CrabPTAdapter, PTerClubAdapter,
            XingwanAdapter, HDKylinAdapter, TangPTAdapter, MuXueGeAdapter,
            OpenCDAdapter
        )
        from musichub.core.models import CanonicalMusicItem, AudioTrackInfo

        # 构造测试音频实体
        item = CanonicalMusicItem(
            title="Jay Chou - Fantasy (2001) [FLAC]",
            artist="Jay Chou",
            album="Fantasy",
            year="2001",
            format="FLAC",
            medium="CD",
            standard="Lossless",
            genre=["Pop", "流行"],
            tracks=[AudioTrackInfo(track_number=1, title="Track 1", artist="Jay Chou", duration_seconds=180.0, file_path="dummy.flac", file_size=1024*1024*20)]
        )

        # 1. 验证 HDKylin 的 Latin-1 前置护栏
        kylin = HDKylinAdapter()
        # 英文通过
        ok, msg = kylin.pre_publish_check(item)
        self.assertTrue(ok)
        
        # 中文被拒发护栏拦截
        item_cn = CanonicalMusicItem(
            title="周杰伦 - 范特西 (2001) [FLAC]",
            artist="周杰伦",
            album="范特西",
            year="2001",
            format="FLAC",
            medium="CD"
        )
        ok_cn, msg_cn = kylin.pre_publish_check(item_cn)
        self.assertFalse(ok_cn)
        self.assertIn("rejects non-Latin-1", msg_cn)

        # 2. 验证 OpenCD 适配器表单生成与前置护栏
        ocd = OpenCDAdapter()
        ok_ocd, msg_ocd = ocd.pre_publish_check(item)
        self.assertTrue(ok_ocd)

        fields = ocd.build_form_fields(item)
        self.assertEqual(fields["type"], "408")
        self.assertEqual(fields["standard"], "4")  # FLAC=4
        self.assertEqual(fields["medium"], "1")    # CD=1
        self.assertEqual(fields["audio_mode"], "multi")
        self.assertEqual(fields["artist"], "Jay Chou")
        self.assertEqual(fields["resource_name"], "Fantasy")
        self.assertIn("01. Track 1", fields["track_list"])

        # OpenCD 拦截非法年份
        item_bad_year = CanonicalMusicItem(
            title="Test", artist="A", album="B", year="bad_year"
        )
        ok_bad, _ = ocd.pre_publish_check(item_bad_year)
        self.assertFalse(ok_bad)

        # 3. 验证其余适配器构建正常与精准映射
        for adapter in [MusopiaAdapter(), CrabPTAdapter(), PTerClubAdapter(), XingwanAdapter(), TangPTAdapter(), MuXueGeAdapter()]:
            f = adapter.build_form_fields(item)
            self.assertIn("type", f)
            self.assertIn("descr", f)

        # 4. 验证 CrabPT 独走 Lsky Pro 图床策略
        from unittest import mock
        from musichub.metadata.cover_service import CoverImageService
        cover_svc = CoverImageService(cache_dir=os.path.join(self.test_dir, "covers"))
        # 源站图链有效 ⇒ 直接复用（离线：mock 探测结果）
        with mock.patch.object(CoverImageService, "is_valid_image_url",
                               return_value=(True, "OK")):
            c_url, c_desc = cover_svc.get_site_cover("PTerClub", "https://picgo.net/test.jpg")
            self.assertEqual(c_url, "https://picgo.net/test.jpg")
            self.assertIn("复用", c_desc)

            # 针对蟹黄堡：公网 PicGo 图片不会被直接当作 Lsky 链接透传
            c_crab, c_crab_desc = cover_svc.get_site_cover("CrabPT", "https://picgo.net/test.jpg")
            self.assertNotEqual(c_crab, "https://picgo.net/test.jpg")

    def test_musopia_logtexts_and_descr(self):
        """乌托邦改版：日志走 logtexts[]，简介不再写 [spoiler]/[code]"""
        from musichub.adapters.sites.musopia import MusopiaAdapter, split_log_texts

        single = ("Exact Audio Copy V1.0 beta 3 from 29. August 2011\n"
                  "EAC 抓取日志文件从13. 三月 2015, 16:12\n01. Track\n")
        self.assertEqual(len(split_log_texts(single)), 1)
        self.assertEqual(split_log_texts("   "), [])

        multi = ("Log CD 1\nExact Audio Copy V1.0\nEAC 抓取日志文件…\nA\n"
                 "Exact Audio Copy V1.0\nEAC 抓取日志文件…\nB\n")
        parts = split_log_texts(multi)
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(p.startswith("Exact Audio Copy") for p in parts))

        item = CanonicalMusicItem(
            title="Test - Album", artist="Test", album="Album", year="2020",
            log_text=multi,
        )
        fields = MusopiaAdapter().build_form_fields(item)
        self.assertEqual(fields["logtexts[]"], parts)
        self.assertNotIn("[spoiler=", fields["descr"])
        self.assertNotIn("[code]", fields["descr"])

    def test_patrol_and_auto_readd(self):
        """测试断种常态化巡检 (SeedPatrol) 与自愈 (AutoReadd)"""
        from musichub.patrol.seed_patrol import SeedPatrolManager, SEEDING, MISSING, NO_SITE_TORRENT
        from musichub.patrol.auto_readd import SeedAutoReadd

        # 构造测试 sqlite DB
        db_path = os.path.join(self.test_dir, "test_patrol.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
        CREATE TABLE site_publish_graph (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_fingerprint TEXT,
            stream_fingerprint TEXT,
            origin_site TEXT,
            origin_torrent_id TEXT,
            target_site TEXT,
            target_torrent_id TEXT,
            published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # 插入两条记录
        conn.execute("INSERT INTO site_publish_graph (origin_site, target_site, target_torrent_id) VALUES ('opencd', 'musopia', '1001')")
        conn.execute("INSERT INTO site_publish_graph (origin_site, target_site, target_torrent_id) VALUES ('opencd', 'musopia', '1002')")
        conn.commit()
        conn.close()

        # 生成一个合法假种子并在磁盘缓存
        torrents_dir = os.path.join(self.test_dir, "torrents")
        os.makedirs(torrents_dir, exist_ok=True)
        # 用 torf 构造合法种子
        import torf
        dummy_file = os.path.join(self.test_dir, "dummy.flac")
        with open(dummy_file, "wb") as f:
            f.write(b"fLaC" + b"\x00" * 4096)
        t = torf.Torrent(path=dummy_file, trackers=["http://tracker.example.com/announce"])
        t.generate()
        fake_ih = str(t.infohash).lower()
        fake_torrent = t.dump()
        
        with open(os.path.join(torrents_dir, "musopia_1001.torrent"), "wb") as f:
            f.write(fake_torrent)

        # 客户端中只包含该种子，且处于 uploading (做种中)
        qb = DummyQB()
        qb.torrents = [
            {"hash": fake_ih, "name": "TestAlbum", "state": "uploading", "progress": 1.0}
        ]

        patrol = SeedPatrolManager(db_path=db_path, publish_qb_client=qb, site_torrents_dir=torrents_dir)
        records, stats = patrol.run_patrol(limit=10)

        self.assertEqual(stats["TOTAL"], 2)
        # 1001 命中，判定为 SEEDING
        rec_1001 = [r for r in records if r.target_torrent_id == "1001"][0]
        self.assertEqual(rec_1001.status, SEEDING)
        self.assertEqual(rec_1001.b_info_hash, fake_ih)

        # 1002 缺失种子文件，判定为 NO_SITE_TORRENT
        rec_1002 = [r for r in records if r.target_torrent_id == "1002"][0]
        self.assertEqual(rec_1002.status, NO_SITE_TORRENT)

        # 测试自愈加回
        readd = SeedAutoReadd(publish_qb_client=qb, patrol_manager=patrol)
        # 模拟 1001 丢失的情形
        rec_1001.status = MISSING
        qb.torrents = []  # 清空客户端
        ok, msg = readd.readd_single(rec_1001, dry_run=True)
        self.assertTrue(ok)
        self.assertIn("[Dry-Run]", msg)

    def test_patrol_multi_instance_states(self):
        """3010 与音乐池共同巡检，区分池内做种、重复、暂停和站点取种失败。"""
        import torf
        from musichub.patrol.seed_patrol import (
            SeedPatrolManager, SEEDING, PAUSED, MISSING, AMBIGUOUS, FETCH_ERROR,
        )

        torrents_dir = os.path.join(self.test_dir, "multi_patrol_torrents")
        os.makedirs(torrents_dir, exist_ok=True)
        hashes = {}
        for tid in ("2001", "2002", "2003", "2004", "2006"):
            payload_path = os.path.join(self.test_dir, f"album_{tid}.flac")
            with open(payload_path, "wb") as f:
                f.write(b"fLaC" + tid.encode("ascii") * 1024)
            torrent = torf.Torrent(path=payload_path, trackers=["http://tracker.example.com/announce"])
            torrent.generate()
            hashes[tid] = str(torrent.infohash).lower()
            with open(os.path.join(torrents_dir, f"musopia_{tid}.torrent"), "wb") as f:
                f.write(torrent.dump())

        conn = sqlite3.connect(self.db_path)
        for tid in ("2001", "2002", "2003", "2004", "2005", "2006"):
            conn.execute(
                "INSERT INTO site_publish_graph (content_fingerprint, origin_site, target_site, target_torrent_id) VALUES (?, 'opencd', 'musopia', ?)",
                (f"fp_{tid}", tid),
            )
        conn.commit()
        conn.close()

        class FailingAdapter:
            def download_torrent(self, tid):
                raise RuntimeError(f"HTTP 503 for {tid}")

        class BrokenQB(DummyQB):
            def get_torrents(self, strict=False):
                raise RuntimeError("connection refused")

        publish = DummyQB()
        publish.torrents = [
            {"hash": hashes["2001"], "name": "publish", "state": "uploading", "progress": 1.0, "seeding_time": 120},
            {"hash": hashes["2003"], "name": "duplicate-a", "state": "stalledUP", "progress": 1.0},
        ]
        pool = DummyQB()
        pool.torrents = [
            {"hash": hashes["2002"], "name": "pool", "state": "stalledUP", "progress": 1.0, "seeding_time": 3600},
            {"hash": hashes["2003"], "name": "duplicate-b", "state": "stoppedUP", "progress": 1.0},
            {"hash": hashes["2004"], "name": "paused", "state": "stoppedUP", "progress": 1.0, "seeding_time": 0},
        ]
        patrol = SeedPatrolManager(
            db_path=self.db_path,
            publish_qb_client=publish,
            pool_qb_clients={"music_pool_1": pool, "music_pool_2": BrokenQB()},
            site_torrents_dir=torrents_dir,
            site_adapters={"musopia": FailingAdapter()},
        )
        records, stats = patrol.run_patrol(limit=20)
        by_tid = {r.target_torrent_id: r for r in records}

        self.assertEqual(by_tid["2001"].status, SEEDING)
        self.assertEqual(by_tid["2001"].qb_instance, "publish")
        self.assertEqual(by_tid["2002"].status, SEEDING)
        self.assertEqual(by_tid["2002"].qb_instance, "music_pool_1")
        self.assertEqual(by_tid["2002"].seeding_time, 3600)
        self.assertEqual(by_tid["2003"].status, AMBIGUOUS)
        self.assertEqual(len(by_tid["2003"].qb_instances), 2)
        self.assertEqual(by_tid["2004"].status, PAUSED)
        self.assertEqual(by_tid["2005"].status, FETCH_ERROR)
        self.assertEqual(by_tid["2006"].status, MISSING)
        self.assertIn("music_pool_2", by_tid["2006"].note)
        self.assertEqual(stats["QB_CLIENT_ERROR"], 1)

    def test_free_window_and_snapshot(self):
        """测试全站优惠检测与页面快照管理"""
        from musichub.collector.free_window import SiteFreeWindowDetector
        from musichub.metadata.snapshot_manager import RawHtmlSnapshotManager

        # 1. 验证全站 Free 横幅匹配
        sample_banner_html = '''
        <div id="nav_block">
            北京时间 2026-11-20 00:00:00 至 2026-11-22 23:59:59 开启全站Free。
        </div>
        '''
        res = SiteFreeWindowDetector.detect_global_free_from_html(sample_banner_html)
        self.assertTrue(res.is_free)
        self.assertEqual(res.expire_at, "2026-11-22 23:59:59")
        self.assertTrue(res.is_global_free)

        # 2. 验证行内优惠解析
        sample_row_html = '''
        <tr><td><img class="pro_free2up" src="pic/trans.gif" title="2026-11-25 12:00:00" /></td></tr>
        '''
        res_row = SiteFreeWindowDetector.detect_torrent_promo_from_row_html(sample_row_html)
        self.assertTrue(res_row.is_free)
        self.assertEqual(res_row.promo_type, "free2up")
        self.assertEqual(res_row.expire_at, "2026-11-25 12:00:00")

        # 3. 验证本地快照读写
        snap_dir = os.path.join(self.test_dir, "raw_html")
        snap_mgr = RawHtmlSnapshotManager(raw_html_dir=snap_dir)
        path = snap_mgr.save_snapshot("musopia", "9999", "<html>content</html>")
        self.assertTrue(os.path.exists(path))
        loaded = snap_mgr.load_snapshot("musopia", "9999")
        self.assertEqual(loaded, "<html>content</html>")


    def test_source_detail_parsing(self):
        """源种详情页：字段/正文/日志/曲目/转载规则解析（不联网）"""
        from musichub.collector.source_detail import parse_detail_html, detect_source_site, extract_tid

        html = """
        <html><head><title>Jay Chou - Fantasy :: OpenCD</title></head><body>
        <h1 id="top">Jay Chou - Fantasy</h1>
        <table>
          <tr><td class="rowhead">藝術家</td><td class="rowfollow">Jay Chou</td></tr>
          <tr><td class="rowhead">專輯</td><td class="rowfollow">Fantasy</td></tr>
          <tr><td class="rowhead">發行年份</td><td class="rowfollow">2001-09-14</td></tr>
          <tr><td class="rowhead">格式</td><td class="rowfollow">FLAC</td></tr>
          <tr><td class="rowhead">媒介</td><td class="rowfollow">CD</td></tr>
          <tr><td class="rowhead">轉載規則</td><td class="rowfollow">歡迎轉載</td></tr>
        </table>
        <div id="kdescr"><img src="https://img.example.com/cover.jpg"/>专辑简介正文</div>
        <div id="divnfo"><pre>Exact Audio Copy log</pre></div>
        <div id="divtracklist">曲目列表：<br/>01. 爱在西元前<br/>02. 简单爱</div>
        </body></html>
        """
        parsed = parse_detail_html(html)
        self.assertEqual(parsed["artist"], "Jay Chou")
        self.assertEqual(parsed["album"], "Fantasy")
        self.assertEqual(parsed["year"], "2001")
        self.assertEqual(parsed["format"], "FLAC")
        self.assertEqual(parsed["description"], "专辑简介正文")
        self.assertEqual(parsed["log_text"], "Exact Audio Copy log")
        self.assertEqual(parsed["tracklist"], "01. 爱在西元前\n02. 简单爱")
        self.assertEqual(parsed["cover_url"], "https://img.example.com/cover.jpg")
        self.assertEqual(parsed["transfer_rule"], "ALLOW")

        # 无「轉載規則」字段 => None（由调用方取站点默认）
        self.assertIsNone(parse_detail_html("<html><body>x</body></html>")["transfer_rule"])

        # 站点识别与 tid 提取
        self.assertEqual(detect_source_site("https://tracker.open.cd/announce"), "opencd")
        self.assertEqual(detect_source_site("https://tracker.hdkylin.top/announce"), "hdkylin")
        self.assertEqual(detect_source_site("https://unknown.example/announce"), "")
        self.assertEqual(extract_tid("https://open.cd/details.php?id=123456"), "123456")

    # ------------------------------------------------------------------
    # 2026-11-21 源种做种链：mv 搬运 / 目标池注入转正 / OpenCD 锁定 / ingest_log
    # ------------------------------------------------------------------
    def _make_album_dir(self, root_name: str, size: int = 1024 * 1024 + 2048) -> str:
        album_dir = os.path.join(self.test_dir, root_name)
        os.makedirs(album_dir, exist_ok=True)
        with open(os.path.join(album_dir, "01.flac"), "wb") as f:
            f.write(b"fLaC" + b"0" * size)
        return album_dir

    def _handoff_manager(self, source_qb, pool_dir, pool_qb, pool_name="music_pool_2", verify_timeout=0):
        from musichub.storage.handoff import ReadyHandoffManager
        pm = FakePoolManager(pool_dir, pool_qb, pool_name)
        return ReadyHandoffManager(
            qb_download=source_qb,
            pool_manager=pm,
            graph_manager=self.graph_mgr,
            verify_timeout=verify_timeout,
            archive_dir=os.path.join(self.test_dir, "empty_archive"),
        )

    def test_handoff_injects_source_seed_and_logs_ingest(self):
        """源种搬进池后必须在目标池建任务做种，并写 ingest_log 台账"""
        info_hash = "b" * 40
        album = self._make_album_dir("src_album")
        pool_dir = os.path.join(self.test_dir, "pool_2")
        source_qb = HandoffFakeQB(port=3016, torrent_bytes=b"d8:announce0:e")
        pool_qb = HandoffFakeQB(port=3230)
        mgr = self._handoff_manager(source_qb, pool_dir, pool_qb)

        item = CanonicalMusicItem(
            title="Album X", album="Album X", artist="Artist",
            origin_site="musopia", origin_torrent_id="123",
            origin_info_hash=info_hash,
        )
        ok, msg = mgr.handoff_task(item, album)
        self.assertTrue(ok, msg)

        # mv：SSD 侧消失，机械盘侧就位（不是复制）
        self.assertFalse(os.path.exists(album))
        self.assertTrue(os.path.exists(os.path.join(pool_dir, "src_album", "01.flac")))

        # 目标池真的建了任务，且直接转正做种（paused=False + skip_checking）
        self.assertEqual(len(pool_qb.added), 1)
        added = pool_qb.added[0]
        self.assertEqual(added["save_path"], "/downloads")
        self.assertFalse(added["is_paused"])
        self.assertTrue(added["skip_checking"])
        self.assertIn("source-seed", added["tags"])

        # 3016 任务下架，但绝不 delete_files=True
        self.assertEqual(source_qb.deleted, [(info_hash, False)])

        # 台账可回答「源种在哪做种」
        rec = self.graph_mgr.get_latest_ingest(info_hash)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "SEEDING")
        self.assertEqual(rec["target_port"], 3230)
        self.assertEqual(rec["target_instance"], "music_pool_2")
        self.assertEqual(rec["move_mode"], "mv")
        self.assertTrue(os.path.exists(rec["host_path"]))
        self.assertEqual(item.content_path, rec["host_path"])

    def test_opencd_handoff_routes_dynamically(self):
        """2026-11-23 重构：8 实例完全平权，OpenCD 来源可动态移交至任意最空保种池"""
        info_hash = "c" * 40
        album = self._make_album_dir("opencd_album")
        pool_dir = os.path.join(self.test_dir, "pool_target")
        source_qb = HandoffFakeQB(port=3016, torrent_bytes=b"d8:announce0:e")
        target_qb = HandoffFakeQB(port=3230)
        mgr = self._handoff_manager(source_qb, pool_dir, target_qb, pool_name="music_pool_3")

        item = CanonicalMusicItem(
            title="CD Album", album="CD Album", artist="Artist",
            origin_site="OpenCD", origin_torrent_id="9527",
            origin_info_hash=info_hash,
        )
        ok, msg = mgr.handoff_task(item, album)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(album))
        self.assertEqual(len(target_qb.added), 1)
        self.assertEqual(source_qb.deleted, [(info_hash, False)])
        rec = self.graph_mgr.get_latest_ingest(info_hash)
        self.assertEqual(rec["status"], "SEEDING")

    def test_rolling_pool_least_loaded_routing(self):
        """路由层：8 池平权，按当前任务数动态选最空池（OpenCD 与其他站点统一）"""
        from musichub.storage.handoff import RollingPoolManager, PoolConfig

        p1 = PoolConfig(name="music_pool_1", port=3210, mount_dir="/tmp/p1")
        p2 = PoolConfig(name="music_pool_2", port=3220, mount_dir="/tmp/p2")
        pm = RollingPoolManager(pools=[p1, p2])
        # p1 有 10 个任务，p2 只有 2 个任务
        pm.clients["music_pool_1"] = HandoffFakeQB(port=3210, torrents=[{"hash": f"h{i}"} for i in range(10)])
        pm.clients["music_pool_2"] = HandoffFakeQB(port=3220, torrents=[{"hash": f"h{i}"} for i in range(2)])

        for site in ("opencd", "OpenCD", "open.cd", "musopia", "ubits"):
            dst, client, pool_name = pm.route_target(site)
            self.assertEqual(pool_name, "music_pool_2")
            self.assertEqual(client.port, 3220)
            self.assertIn("p2", dst)

    def test_handoff_aborts_without_torrent_bytes(self):
        """拿不到源种 .torrent 时必须中止，不得留下「有文件无任务」的孤儿"""
        info_hash = "d" * 40
        album = self._make_album_dir("no_meta_album")
        pool_dir = os.path.join(self.test_dir, "pool_3")
        source_qb = HandoffFakeQB(port=3016, torrent_bytes=None)
        pool_qb = HandoffFakeQB(port=3250)
        mgr = self._handoff_manager(source_qb, pool_dir, pool_qb, pool_name="music_pool_3")

        item = CanonicalMusicItem(
            title="NoMeta", album="NoMeta", artist="Artist",
            origin_site="hdkylin", origin_info_hash=info_hash,
        )
        ok, msg = mgr.handoff_task(item, album)
        self.assertFalse(ok)
        self.assertIn("字节缺失", msg)
        self.assertTrue(os.path.exists(album))
        self.assertEqual(pool_qb.added, [])
        self.assertEqual(source_qb.deleted, [])

    def test_handoff_rollback_when_target_never_seeds(self):
        """目标池注入后进度始终不到 100%：撤回池任务、把文件搬回 SSD、源任务保持可重试"""
        info_hash = "e" * 40
        album = self._make_album_dir("bad_target_album")
        pool_dir = os.path.join(self.test_dir, "pool_4")
        source_qb = HandoffFakeQB(port=3016, torrent_bytes=b"d8:announce0:e")
        pool_qb = HandoffFakeQB(port=3270, torrents=[])  # 注入后永远查不到该任务
        mgr = self._handoff_manager(source_qb, pool_dir, pool_qb, pool_name="music_pool_4",
                                    verify_timeout=0.05)

        item = CanonicalMusicItem(
            title="BadTarget", album="BadTarget", artist="Artist",
            origin_site="crabpt", origin_info_hash=info_hash,
        )
        ok, msg = mgr.handoff_task(item, album)
        self.assertFalse(ok)
        self.assertIn("未达 100%", msg)

        # 文件回滚到 SSD，3016 任务未下架（下一轮可重试）
        self.assertTrue(os.path.exists(os.path.join(album, "01.flac")))
        self.assertFalse(os.path.exists(os.path.join(pool_dir, "bad_target_album")))
        self.assertEqual(source_qb.deleted, [])
        self.assertEqual(pool_qb.deleted, [(info_hash, False)])
        rec = self.graph_mgr.get_latest_ingest(info_hash)
        self.assertEqual(rec["status"], "ROLLED_BACK")

    def test_source_policy_gates(self):
        """源种准入闸门：规则 / 等待期 / 矩阵 / 元数据分级"""
        import time as _time
        from musichub.publisher.gateway import evaluate_source_policy

        strict_adapter = DummyAdapter(site_name="OpenCD")
        strict_adapter.metadata_strict = True
        loose_adapter = DummyAdapter(site_name="Musopia")

        def item():
            return CanonicalMusicItem(
                title="A - B", artist="A", album="B", year="2001", origin_info_hash="hash001"
            )

        # 无任务行：本地存量/极速通道放行
        ok, reason = evaluate_source_policy(self.graph_mgr, item(), "Musopia", loose_adapter)
        self.assertTrue(ok)
        self.assertIn("POLICY_NO_TASK", reason)

        # 登记任务行（默认 NEED_CONFIRM）
        self.graph_mgr.upsert_source_task("hash001", source_site="opencd", source_tid="123")
        ok, reason = evaluate_source_policy(self.graph_mgr, item(), "Musopia", loose_adapter)
        self.assertFalse(ok)
        self.assertEqual(reason, "POLICY_NEED_CONFIRM")

        # DENY 永久拦
        self.graph_mgr.set_transfer_rule("hash001", "DENY")
        ok, reason = evaluate_source_policy(self.graph_mgr, item(), "Musopia", loose_adapter)
        self.assertFalse(ok)
        self.assertEqual(reason, "POLICY_RULE_DENY")

        # ALLOW 放行
        self.graph_mgr.set_transfer_rule("hash001", "ALLOW")
        ok, reason = evaluate_source_policy(self.graph_mgr, item(), "Musopia", loose_adapter)
        self.assertTrue(ok)
        self.assertEqual(reason, "POLICY_ALLOW")

        # 等待期未到 => 拦
        self.graph_mgr.set_publish_after("hash001", _time.time() + 3600)
        ok, reason = evaluate_source_policy(self.graph_mgr, item(), "Musopia", loose_adapter)
        self.assertFalse(ok)
        self.assertIn("POLICY_DELAY", reason)

        # 到期后放行
        self.graph_mgr.set_publish_after("hash001", _time.time() - 1)
        ok, _ = evaluate_source_policy(self.graph_mgr, item(), "Musopia", loose_adapter)
        self.assertTrue(ok)

        # 站点矩阵关闭 => 拦
        self.graph_mgr.set_matrix_rule("opencd", "Musopia", allow=False, note="测试")
        ok, reason = evaluate_source_policy(self.graph_mgr, item(), "Musopia", loose_adapter)
        self.assertFalse(ok)
        self.assertIn("POLICY_MATRIX_BLOCK", reason)

        # 元数据缺失：严格站拦、宽松站放行
        self.graph_mgr.set_matrix_rule("opencd", "Musopia", allow=True)
        self.graph_mgr.update_source_meta("hash001", meta_status="NEED_META")
        ok, reason = evaluate_source_policy(self.graph_mgr, item(), "OpenCD", strict_adapter)
        self.assertFalse(ok)
        self.assertEqual(reason, "POLICY_NEED_META")
        ok, _ = evaluate_source_policy(self.graph_mgr, item(), "Musopia", loose_adapter)
        self.assertTrue(ok)

    def test_build_source_link_multi_source(self):
        """转载声明按源站生成；本地/未知源不声明，绝不谎称 OpenCD"""
        from musichub.adapters.descr_core import build_source_link

        self.assertEqual(
            build_source_link({"origin_site": "opencd", "origin_tid": "123"}),
            "转自OpenCD: https://open.cd/details.php?id=123",
        )
        self.assertEqual(
            build_source_link({"origin_site": "musopia", "origin_tid": "456"}),
            "转自乌托邦: https://www.musopia.vip/details.php?id=456",
        )
        self.assertEqual(build_source_link({"origin_site": "local"}), "")
        self.assertEqual(build_source_link({"origin_site": "unknown"}), "")
        self.assertIn("转自猫站", build_source_link({"origin_site": "pterclub", "torrent_name": "X"}))


class TestOnsiteEditor(unittest.TestCase):
    """详情页编辑器的离线解析/封面拼接契约（不联网）"""

    def test_parse_edit_form_baseline(self):
        from musichub.onsite.editor import parse_edit_form
        html = """
        <html><body>
        <form action="takelogin.php" method="post"><input name="username" value="x"></form>
        <form action="takeedit.php" method="post">
          <input type="hidden" name="id" value="48728">
          <input name="name" value="A - B (2020)">
          <input name="small_descr" value="">
          <input type="checkbox" name="tags[4][]" value="10" checked>
          <input type="checkbox" name="tags[4][]" value="31" checked>
          <input type="checkbox" name="tags[4][]" value="44">
          <input type="checkbox" name="uplver" value="yes">
          <select name="medium_sel[4]"><option value="8">CD</option>
            <option value="10" selected>DVD</option></select>
          <textarea name="descr">[quote]转自OpenCD[/quote]</textarea>
          <input type="submit" name="submit" value="提交">
        </form></body></html>
        """
        fields, action = parse_edit_form(html)
        self.assertEqual(action, "takeedit.php")
        self.assertEqual(fields["id"], "48728")
        self.assertEqual(fields["name"], "A - B (2020)")
        self.assertEqual(fields["medium_sel[4]"], "10")
        self.assertEqual(fields["descr"], "[quote]转自OpenCD[/quote]")
        self.assertEqual(fields["tags[4][]"], ["10", "31"])
        self.assertNotIn("uplver", fields)
        self.assertNotIn("submit", fields)
        self.assertNotIn("username", fields)

    def test_parse_edit_form_missing(self):
        from musichub.onsite.editor import parse_edit_form
        fields, action = parse_edit_form("<html><body>没有表单</body></html>")
        self.assertIsNone(fields)
        self.assertEqual(action, "")

    def test_insert_cover_block(self):
        from musichub.onsite.editor import insert_cover_block, has_cover
        quote = "[quote][b]转自OpenCD[/b]: https://open.cd/details.php?id=1[/quote]\n\n正文"
        got = insert_cover_block(quote, "https://img.example/a.jpg")
        self.assertTrue(got.startswith("[quote][b]转自OpenCD[/b]"))
        self.assertLess(got.index("[center][img]"), got.index("正文"))
        self.assertIn("[center][img]https://img.example/a.jpg[/img][/center]", got)
        self.assertTrue(has_cover(got))
        # 已有封面时幂等
        self.assertEqual(insert_cover_block(got, "https://img.example/b.jpg"), got)
        # 空简介直接放封面
        self.assertEqual(insert_cover_block("", "https://img.example/a.jpg"),
                         "[center][img]https://img.example/a.jpg[/img][/center]")

    def test_absolutize_relative_cover(self):
        from musichub.onsite.editor import absolutize
        self.assertEqual(absolutize("attachments/pic/1.jpg", "https://open.cd"),
                         "https://open.cd/attachments/pic/1.jpg")
        self.assertEqual(absolutize("/x.jpg", "https://open.cd/"), "https://open.cd/x.jpg")
        self.assertEqual(absolutize("https://cdn/x.jpg", "https://open.cd"),
                         "https://cdn/x.jpg")

    def test_resolve_cover_url_explicit(self):
        from unittest import mock
        from musichub.onsite.editor import resolve_cover_url
        from musichub.metadata.cover_service import CoverImageService
        # 显式 URL 必须**校验有效**才复用（离线：mock 探测结果）
        with mock.patch.object(CoverImageService, "is_valid_image_url",
                               return_value=(True, "OK")):
            url, why = resolve_cover_url("https://cdn/x.jpg")
            self.assertEqual(url, "https://cdn/x.jpg")
            url, why = resolve_cover_url("attachments/x.jpg",
                                         source_base_url="https://img.example.com")
            self.assertEqual(url, "https://img.example.com/attachments/x.jpg")
        with mock.patch.object(CoverImageService, "is_valid_image_url",
                               return_value=(False, "HTTP 404")), \
             mock.patch.object(CoverImageService, "upload_picgo", return_value=None):
            url, why = resolve_cover_url("")
            self.assertEqual(url, "")
            self.assertTrue(why)

    def test_parse_detail_page_cover(self):
        from musichub.onsite.editor import parse_detail_page
        html = """
        <h1 id="top">A - B (2020) [FLAC]</h1>
        <div id="kdescr"><img src="attachments/cover.jpg" class="cover"><br>正文</div>
        """
        d = parse_detail_page(html)
        self.assertIn("A - B", d["title"])
        self.assertEqual(d["cover_url"], "attachments/cover.jpg")

    def test_find_local_album_dir(self):
        import os
        import tempfile
        from musichub.onsite.editor import find_local_album_dir
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "2003 - And Then"))
        os.makedirs(os.path.join(root, "@eaDir"))
        got = find_local_album_dir("2003 - And Then - FLAC 16bit 44.1kHz", roots=[root])
        self.assertTrue(got.endswith("2003 - And Then"))
        self.assertEqual(find_local_album_dir("不存在", roots=[root]), "")

        # 合辑：目录名与标题只有部分词元重合，且顶层无图、封面在子目录
        sub = os.path.join(root, "刘若英[1995-2022]录音室专辑合集")
        os.makedirs(os.path.join(sub, "1995-雨季"))
        with open(os.path.join(sub, "1995-雨季", "front.jpg"), "wb") as f:
            f.write(b"x")
        self.assertEqual(
            find_local_album_dir("刘若英 - 刘若英 录音室专辑合集 1995 - FLAC 16bit", roots=[root]),
            sub)
        from musichub.onsite.editor import find_nested_cover
        self.assertTrue(find_nested_cover(sub).endswith("front.jpg"))

    def test_site_rules_registry(self):
        from musichub.onsite.site_rules import SITE_RULES, cover_capable_sites, get_rule
        self.assertGreaterEqual(len(SITE_RULES), 10)
        for site in cover_capable_sites():
            self.assertTrue(get_rule(site)["edit_url"].endswith("edit.php"), site)
        self.assertFalse(any("[4]" in f for f in get_rule("uhddolby")["fields"]))
        self.assertEqual(get_rule("crabpt")["cover_host_required"], "img.crabpt.vip")
        self.assertTrue(get_rule("hdkylin")["title_latin1_only"])
        with self.assertRaises(KeyError):
            get_rule("no_such_site")


class _FakeHttpResp:
    """给 `is_valid_image_url` 用的最小假响应（支持 with / iter_content）。"""

    def __init__(self, status=200, headers=None, chunks=()):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size=8192):
        for chunk in self._chunks:
            yield chunk


class TestCoverPolicy(unittest.TestCase):
    """封面策略：源站优先、失效才本地补图、落库复用、站点图床路由（全离线）"""

    def _tmpdir(self):
        path = tempfile.mkdtemp(prefix="mh-cover-")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def _svc(self, tmp, name="c.db"):
        from musichub.metadata.cover_service import CoverImageService
        svc = CoverImageService(cache_dir=tmp, db_path=os.path.join(tmp, name))
        self.addCleanup(svc.close)
        return svc

    @staticmethod
    def _album_with_cover(tmp, name="album"):
        album = os.path.join(tmp, name)
        os.makedirs(album, exist_ok=True)
        with open(os.path.join(album, "cover.jpg"), "wb") as f:
            f.write(b"x" * 64)
        return album

    def test_source_valid_reused_without_upload(self):
        tmp = self._tmpdir()
        svc = self._svc(tmp)
        svc.is_valid_image_url = lambda url, timeout=20: (True, "OK")
        svc.upload_picgo = lambda p: self.fail("源链有效时不应上传 PicGo")
        svc.upload_lsky = lambda p: self.fail("源链有效时不应上传 Lsky")
        url, why = svc.get_site_cover(
            "lemonhd", "https://src/ok.jpg", None,
            source_site="opencd", source_tid="1", source_name="A")
        self.assertEqual(url, "https://src/ok.jpg")
        self.assertIn("复用", why)

    def test_broken_source_uploads_then_reuses_db_cache(self):
        tmp = self._tmpdir()
        album = self._album_with_cover(tmp)
        db = os.path.join(tmp, "c.db")
        svc = self._svc(tmp)
        svc.is_valid_image_url = lambda url, timeout=20: (False, "HTTP 404")
        calls = []
        svc.upload_picgo = lambda p: calls.append(p) or "https://img/p/1.jpg"

        url, why = svc.get_site_cover(
            "lemonhd", "https://src/dead.jpg", album,
            source_site="opencd", source_tid="2", source_name="B")
        self.assertEqual(url, "https://img/p/1.jpg")
        self.assertEqual(len(calls), 1)
        row = svc._db().get_cover_cache("opencd:2")
        self.assertEqual(row.get("source_cover_status"), "BROKEN")

        from musichub.metadata.cover_service import CoverImageService
        svc2 = CoverImageService(cache_dir=tmp, db_path=db)
        self.addCleanup(svc2.close)
        svc2.is_valid_image_url = lambda *a, **k: self.fail("命中缓存不应再探测")
        svc2.upload_picgo = lambda p: self.fail("命中缓存不应重复上传")
        url2, why2 = svc2.get_site_cover(
            "lemonhd", "https://src/dead.jpg", album,
            source_site="opencd", source_tid="2", source_name="B")
        self.assertEqual(url2, "https://img/p/1.jpg")
        self.assertIn("已落库", why2)

    def test_no_source_no_local_returns_empty_and_no_empty_url(self):
        tmp = self._tmpdir()
        svc = self._svc(tmp)
        svc.upload_picgo = lambda p: self.fail("没有本地图不应上传")
        url, why = svc.get_site_cover(
            "lemonhd", "", None,
            source_site="opencd", source_tid="3", source_name="C")
        self.assertEqual(url, "")
        self.assertTrue(why)
        row = svc._db().get_cover_cache("opencd:3")
        self.assertEqual(row.get("status"), "FAILED")
        self.assertEqual(svc._db().get_site_cover_url("opencd:3", "lemonhd"), "")

    def test_site_host_routing(self):
        tmp = self._tmpdir()
        album = self._album_with_cover(tmp)

        # crabpt → Lsky，禁止 PicGo
        svc = self._svc(tmp, "a.db")
        svc.is_valid_image_url = lambda *a, **k: (False, "HTTP 404")
        lsky_calls = []
        svc.upload_lsky = lambda p: lsky_calls.append(p) or "https://img.crabpt.vip/i/1.jpg"
        svc.upload_picgo = lambda p: self.fail("crabpt 不应走 PicGo")
        url, _ = svc.get_site_cover(
            "crabpt", "https://www.picgo.net/x.jpg", album,
            source_site="opencd", source_tid="4", source_name="D")
        self.assertTrue(lsky_calls)
        self.assertTrue(url.startswith("https://img.crabpt.vip/"))

        # pterclub + PTER_IMG_API_KEY → 猫站图床
        from unittest import mock
        with mock.patch.dict(os.environ, {"PTER_IMG_API_KEY": "k"}):
            svc2 = self._svc(tmp, "b.db")
            svc2.is_valid_image_url = lambda *a, **k: (False, "HTTP 404")
            pter_calls = []
            svc2.upload_pter_img = lambda p: pter_calls.append(p) or "https://img.pterclub.net/i/2.jpg"
            svc2.upload_picgo = lambda p: self.fail("pterclub 配了 key 不应走 PicGo")
            url2, _ = svc2.get_site_cover(
                "pterclub", "", album,
                source_site="opencd", source_tid="5", source_name="E")
            self.assertTrue(pter_calls)
            self.assertIn("pterclub", url2)

        # 其余站点 → PicGo
        svc3 = self._svc(tmp, "c2.db")
        svc3.is_valid_image_url = lambda *a, **k: (False, "HTTP 404")
        picgo_calls = []
        svc3.upload_picgo = lambda p: picgo_calls.append(p) or "https://www.picgo.net/i/3.jpg"
        url3, _ = svc3.get_site_cover(
            "lemonhd", "", album,
            source_site="opencd", source_tid="6", source_name="F")
        self.assertTrue(picgo_calls)
        self.assertIn("picgo", url3)

    def test_lsky_error_levels(self):
        from musichub.metadata.cover_service import lsky_error_is_item_level
        self.assertTrue(lsky_error_is_item_level("图片大小超出限制"))
        self.assertTrue(lsky_error_is_item_level("unsupported mime"))
        self.assertFalse(lsky_error_is_item_level("HTTP 429"))
        self.assertFalse(lsky_error_is_item_level("网络异常：Timeout"))

    def test_is_valid_image_url_magic_and_probe_cache(self):
        from unittest import mock
        jpeg = b"\xff\xd8\xff" + b"0" * 100
        tmp = self._tmpdir()
        svc = self._svc(tmp)
        ok_resp = _FakeHttpResp(200, {"Content-Type": "image/jpeg"}, [jpeg])
        with mock.patch("musichub.metadata.cover_service.requests.get",
                        return_value=ok_resp) as m:
            ok, reason = svc.is_valid_image_url("https://img/a.jpg")
            self.assertTrue(ok, reason)
            # 第二次命中 7 天探测缓存，不再发请求
            ok2, _ = svc.is_valid_image_url("https://img/a.jpg")
            self.assertTrue(ok2)
            self.assertEqual(m.call_count, 1)

        html_resp = _FakeHttpResp(200, {"Content-Type": "text/html"},
                                  [b"<!DOCTYPE html><html>"])
        with mock.patch("musichub.metadata.cover_service.requests.get",
                        return_value=html_resp):
            ok, reason = svc.is_valid_image_url("https://img/b.jpg")
            self.assertFalse(ok)
            self.assertIn("非图片", reason)

    def test_find_local_cover_grading_and_nested(self):
        tmp = self._tmpdir()
        # 同目录：cover.jpg 命名优先于更大的无关大图
        with open(os.path.join(tmp, "cover.jpg"), "wb") as f:
            f.write(b"x" * 10)
        with open(os.path.join(tmp, "random.jpg"), "wb") as f:
            f.write(b"x" * 1000)
        svc = self._svc(tmp)
        self.assertTrue(svc.find_local_cover(tmp).endswith("cover.jpg"))

        tmp2 = self._tmpdir()
        # 顶层无图 → 一层子目录 front.jpg（合辑）
        nested = os.path.join(tmp2, "disc1")
        os.makedirs(nested)
        with open(os.path.join(nested, "front.jpg"), "wb") as f:
            f.write(b"x" * 10)
        svc2 = self._svc(tmp2, "d.db")
        self.assertTrue((svc2.find_local_cover(tmp2) or "").endswith("front.jpg"))


if __name__ == "__main__":
    unittest.main()
