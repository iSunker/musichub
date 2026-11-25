# -*- coding: utf-8 -*-
import unittest
from musichub.adapters.sites.lemonhd import LemonHDAdapter
from musichub.adapters.sites.uhddolby import UHDDolbyAdapter
from musichub.adapters.sites.hdtime import HDTimeAdapter
from musichub.adapters.sites.ubits import UBitsAdapter
from musichub.adapters.sites.soulvoice import SoulVoiceAdapter
from musichub.adapters.sites.nanyangpt import NanyangPTAdapter
from musichub.adapters.sites.springsunday import SpringSundayAdapter
from musichub.adapters.sites.musopia import MusopiaAdapter
from musichub.adapters.descr_core import TAGS
from musichub.core.models import CanonicalMusicItem, AudioTrackInfo


class TestNewSiteAdapters(unittest.TestCase):
    def setUp(self):
        self.track = AudioTrackInfo(
            file_path="/volume1/video/download/music/test/01.flac",
            track_number=1,
            title="Track 1",
            artist="Artist A",
            duration_seconds=180.0
        )
        self.item = CanonicalMusicItem(
            title="Artist A - Album A (2020) [FLAC]",
            album="Album A",
            artist="Artist A",
            year="2020",
            format="FLAC",
            medium="CD",
            standard="Lossless",
            origin_site="opencd",
            origin_torrent_id="12345",
            local_path="/volume1/video/download/music/test",
            tracks=[self.track]
        )

    def test_lemonhd_adapter(self):
        adapter = LemonHDAdapter()
        ok, msg = adapter.pre_publish_check(self.item)
        self.assertTrue(ok)
        self.assertIn("192.168.0.7:7894", adapter.session.proxies.get("http", ""))

        fields = adapter.build_form_fields(self.item)
        self.assertEqual(fields["type"], "402")
        self.assertEqual(fields["medium_sel[4]"], "9")       # CD=9
        self.assertEqual(fields["audiocodec_sel[4]"], "10")   # FLAC=10
        self.assertEqual(fields["codec_sel[4]"], "5")        # Other
        self.assertEqual(fields["team_sel[4]"], "5")         # Other
        self.assertNotIn("uplver", fields)                   # 具名发布

    def test_uhddolby_adapter(self):
        adapter = UHDDolbyAdapter()
        ok, msg = adapter.pre_publish_check(self.item)
        self.assertTrue(ok)

        fields = adapter.build_form_fields(self.item)
        self.assertEqual(fields["type"], "408")
        self.assertEqual(fields["medium_sel"], "9")          # CD=9
        self.assertEqual(fields["audiocodec_sel"], "7")      # FLAC=7
        self.assertEqual(fields["codec_sel"], "7")           # Other
        self.assertEqual(fields["standard_sel"], "5")        # Others
        self.assertEqual(fields["team_sel"], "8")            # Other
        self.assertEqual(fields["uplver"], "yes")            # 匿名发布

        # 针对华语种子的标签测试（如刘欢例种）
        ch_item = CanonicalMusicItem(
            title="[刘欢-六十年代生人-无损版] Huan Liu 2023-FLAC-QHstudIo",
            album="六十年代生人",
            artist="刘欢",
            year="2003",
            format="FLAC",
            medium="CD",
            origin_site="opencd",
            origin_torrent_id="257139",
            local_path="/volume1/video/download/music/test",
            tracks=[self.track],
            description="国语经典 中文字幕歌词",
            genre=["Pop", "流行"]
        )
        ch_fields = adapter.build_form_fields(ch_item)
        self.assertIn("tags[]", ch_fields)
        self.assertIn("gy", ch_fields["tags[]"])             # 国语
        self.assertIn("zz", ch_fields["tags[]"])             # 中字

    def test_musopia_adapter_tags(self):
        adapter = MusopiaAdapter()
        ok, msg = adapter.pre_publish_check(self.item)
        self.assertTrue(ok)

        # 默认 item (origin_site="opencd") 必定包含 OpenCD 标签
        fields = adapter.build_form_fields(self.item)
        self.assertIn("tags[4][]", fields)
        self.assertIn(TAGS["OpenCD"], fields["tags[4][]"])

        # 原声大碟/古典/爵士测试（对齐花样年华例种）
        soundtrack_item = CanonicalMusicItem(
            title="群星 (Various Artists) - In The Mood For Love 2000 - FLAC 分轨",
            album="In The Mood For Love",
            artist="Various Artists",
            year="2000",
            format="FLAC",
            medium="CD",
            origin_site="opencd",
            origin_torrent_id="29642",
            local_path="/volume1/video/download/music/test",
            tracks=[self.track],
            description="花样年华 电影原声大碟 OST 经典爵士与古典",
            genre=["OST", "Soundtrack", "Jazz", "Classical"]
        )
        st_fields = adapter.build_form_fields(soundtrack_item)
        self.assertIn(TAGS["原声"], st_fields["tags[4][]"])
        self.assertIn(TAGS["古典"], st_fields["tags[4][]"])
        self.assertIn(TAGS["爵士"], st_fields["tags[4][]"])

    def test_hdtime_adapter(self):
        adapter = HDTimeAdapter()
        ok, msg = adapter.pre_publish_check(self.item)
        self.assertTrue(ok)

        fields = adapter.build_form_fields(self.item)
        self.assertEqual(fields["type"], "408")
        self.assertEqual(fields["medium_sel[4]"], "8")       # CD=8
        self.assertEqual(fields["audiocodec_sel[4]"], "1")   # FLAC=1
        self.assertEqual(fields["codec_sel[4]"], "5")        # Other
        self.assertEqual(fields["team_sel[4]"], "5")         # Other
        self.assertEqual(fields["uplver"], "yes")            # 匿名发布

    def test_ubits_adapter(self):
        adapter = UBitsAdapter()
        ok, msg = adapter.pre_publish_check(self.item)
        self.assertTrue(ok)

        fields = adapter.build_form_fields(self.item)
        self.assertEqual(fields["type"], "406")
        self.assertEqual(fields["medium_sel[4]"], "8")       # Lossless Music=8
        self.assertEqual(fields["audiocodec_sel[4]"], "1")   # FLAC=1
        self.assertEqual(fields["codec_sel[4]"], "5")        # Other
        self.assertEqual(fields["standard_sel[4]"], "4")     # SD
        self.assertEqual(fields["team_sel[4]"], "5")         # Other
        self.assertEqual(fields["source_sel[4]"], "11")      # 其它地区=11
        self.assertEqual(fields["uplver"], "yes")            # 匿名发布

    def test_soulvoice_adapter(self):
        adapter = SoulVoiceAdapter()
        ok, msg = adapter.pre_publish_check(self.item)
        self.assertTrue(ok)

        fields = adapter.build_form_fields(self.item)
        self.assertEqual(fields["type"], "408")              # 音乐=408
        self.assertEqual(fields["medium_sel[4]"], "10")      # APE/FLAC=10
        self.assertEqual(fields["codec_sel[4]"], "5")        # Other=5
        self.assertEqual(fields["standard_sel[4]"], "4")     # Other=4
        self.assertEqual(fields["team_sel[4]"], "5")         # Other=5
        self.assertEqual(fields["hr[4]"], "0")               # NO
        self.assertEqual(fields["uplver"], "yes")            # 匿名发布

    def test_nanyangpt_adapter(self):
        adapter = NanyangPTAdapter()
        ok, msg = adapter.pre_publish_check(self.item)
        self.assertTrue(ok)

        fields = adapter.build_form_fields(self.item)
        self.assertEqual(fields["type"], "407")              # 音乐=407
        self.assertEqual(fields["music_time"], "2020")
        self.assertEqual(fields["music_type"], "专辑")
        self.assertEqual(fields["music_author"], "Artist A")
        self.assertEqual(fields["music_name"], "Album A")
        self.assertEqual(fields["music_form"], "FLAC")
        self.assertEqual(fields["music_qual"], "无损")
        self.assertEqual(fields["uplver"], "yes")            # 匿名发布

    def test_springsunday_adapter(self):
        adapter = SpringSundayAdapter()
        adapter._cached_offer_required = True
        adapter._fetch_offer_stats = lambda: {}              # 单测不依赖线上候选区实况
        ok, msg = adapter.pre_publish_check(self.item)
        self.assertTrue(ok)

        # 候选配额闸门：待定 ≥3 或积分 <10 之外的禁发条件必须提前拦截
        blocked = SpringSundayAdapter()
        blocked._fetch_offer_stats = lambda: {"points": 0, "pending": 3}
        ok2, msg2 = blocked.pre_publish_check(self.item)
        self.assertFalse(ok2)
        self.assertIn("候选待定已达上限", msg2)

        fields = adapter.build_form_fields(self.item)
        self.assertEqual(fields["type"], "508")
        self.assertEqual(fields["medium_sel"], "11")         # CD=11
        self.assertEqual(fields["audiocodec_sel"], "7")      # FLAC=7
        self.assertEqual(fields["codec_sel"], "99")          # Other
        self.assertEqual(fields["standard_sel"], "99")       # Other
        self.assertEqual(fields["offer"], "yes")             # 候选模式
        self.assertIn("General", fields["Media_BDInfo"])
        self.assertEqual(fields["uplver"], "yes")
        # 标签：SSD 独立布尔复选，普通专辑不勾 pack；mandarin 是「国配」影视属性，严禁勾
        self.assertNotIn("pack", fields)
        self.assertNotIn("mandarin", fields)
        self.assertNotIn("selfcraft", fields)
        self.assertNotIn("selfpurchase", fields)

        boxset_item = CanonicalMusicItem(
            title=self.item.title, album="Album A 合集", artist="Artist A", year="2020",
            format="FLAC", medium="CD", standard="Lossless", origin_site="opencd",
            origin_torrent_id="12345", local_path=self.item.local_path, tracks=[self.track],
        )
        boxset_fields = adapter.build_form_fields(boxset_item)
        self.assertEqual(boxset_fields["pack"], "1")         # 合集才勾 pack
        self.assertNotIn("mandarin", boxset_fields)

    def test_reject_error_extraction_prefers_body(self):
        """失败页标题恒为「上传失败！」，必须取正文真实原因而不是标题"""
        from musichub.adapters.sites.springsunday import SpringSundayAdapter as A
        adapter = A()
        html = ("<html><head><title>SSD :: 上传失败！ - Powered by NexusPHP</title></head><body>"
                "<div>上传失败！ 你没有自由上传的权限！</div></body></html>")
        err = adapter._extract_site_error(html)
        self.assertIn("没有自由上传的权限", err)
        self.assertEqual(adapter.diagnose_reject_reason(err).split("]")[0] + "]", "[权限配额限制]")
        self.assertFalse(adapter.diagnose_reject_reason(err).startswith("[同名冲突]"))


if __name__ == "__main__":
    unittest.main()
