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
        from musichub.metadata.cover_service import CoverImageService
        cover_svc = CoverImageService(cache_dir=os.path.join(self.test_dir, "covers"))
        # 针对猫站/星湾/乌托邦：公网图片直接复用
        c_url, c_desc = cover_svc.get_site_cover("PTerClub", "https://picgo.net/test.jpg")
        self.assertEqual(c_url, "https://picgo.net/test.jpg")
        self.assertIn("Directly reuse", c_desc)

        # 针对蟹黄堡：公网 PicGo 图片不会被直接当作 Lsky 链接透传
        c_crab, c_crab_desc = cover_svc.get_site_cover("CrabPT", "https://picgo.net/test.jpg")
        self.assertNotEqual(c_crab, "https://picgo.net/test.jpg")

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


if __name__ == "__main__":
    unittest.main()
