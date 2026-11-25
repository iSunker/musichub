# -*- coding: utf-8 -*-
"""单元测试：发种标签匹配引擎 (TagMatcher) 与 12 站点发种表单标签注入"""

import unittest
from typing import Dict, Any

from musichub.core.models import CanonicalMusicItem, AudioTrackInfo
from musichub.adapters.tag_matcher import TagMatcher, FORBIDDEN_TAG_KEYWORDS
from musichub.adapters.sites import (
    MusopiaAdapter, UHDDolbyAdapter, CrabPTAdapter, XingwanAdapter,
    MuXueGeAdapter, TangPTAdapter, HDKylinAdapter, LemonHDAdapter,
    HDTimeAdapter, UBitsAdapter, SoulVoiceAdapter, PTerClubAdapter
)


class TestTagMatcher(unittest.TestCase):

    def setUp(self):
        self.dummy_track = AudioTrackInfo(
            track_number=1,
            title="Track 1",
            artist="Artist",
            duration_seconds=200.0,
            bit_depth=16,
            sample_rate=44100,
            codec="FLAC",
            file_path="/dummy/01.flac"
        )

    def test_forbidden_tags_blacklist(self):
        """测试黑名单拦截：观众、首发、自购、原创、禁转等绝对不勾选"""
        forbidden_samples = [
            "Audiences", "audience", "观众", "觀眾", "观众群", "观众组",
            "首发", "自购", "自制", "原创", "禁转", "官方", "官种", "驻站", "珍宝楼"
        ]
        for tag in forbidden_samples:
            self.assertTrue(TagMatcher.is_forbidden_tag(tag), f"Failed to block forbidden tag: {tag}")

        allowed_samples = ["流行", "古典", "国语", "粤语", "中字", "无损音乐", "乐府", "ASMR"]
        for tag in allowed_samples:
            self.assertFalse(TagMatcher.is_forbidden_tag(tag), f"Incorrectly blocked allowed tag: {tag}")

    def test_mandarin_pop_opencd_to_all_sites(self):
        """测试母种为 OpenCD 港台/华语流行专辑，向全矩阵 12 站发种的标签匹配与注入"""
        item = CanonicalMusicItem(
            title="周杰伦 - 范特西 2001 - FLAC 分轨",
            artist="周杰伦",
            album="范特西",
            year="2001",
            format="FLAC",
            medium="CD",
            standard="Lossless",
            origin_site="opencd",
            origin_torrent_id="123456",
            tags=["港台", "流行(Pop)"],
            genre=["流行(Pop)"],
            tracks=[self.dummy_track]
        )

        # 1. Musopia: tags[4][] 应有 31(OpenCD), 6(港台), 10(流行)
        musopia_tags = TagMatcher.match_tags_for_site("musopia", item)
        self.assertIn("31", musopia_tags)  # OpenCD
        self.assertIn("6", musopia_tags)   # 港台
        self.assertIn("10", musopia_tags)  # 流行
        self.assertNotIn("1", musopia_tags)  # 禁转
        self.assertNotIn("2", musopia_tags)  # 首发

        # 2. UHDDolby: tags[] 应有 yy(粤语/港台) 或 gy(国语), 以及 zz(中字)
        uhd_tags = TagMatcher.match_tags_for_site("uhddolby", item)
        self.assertTrue("yy" in uhd_tags or "gy" in uhd_tags)
        self.assertIn("zz", uhd_tags)

        # 3. Xingwan: tags[4][] 必选 17(无损音乐), 31(粤语/港台) 或 5(国语), 6(中字)
        xw_tags = TagMatcher.match_tags_for_site("xingwan", item)
        self.assertIn("17", xw_tags)  # 必选无损
        self.assertTrue("31" in xw_tags or "5" in xw_tags)
        self.assertIn("6", xw_tags)

        # 4. MuXueGe: tags[4][] 必选 30(乐府), 41(高码率), 38(国语)
        mxg_tags = TagMatcher.match_tags_for_site("muxuege", item)
        self.assertIn("30", mxg_tags)  # 必选乐府
        self.assertIn("41", mxg_tags)  # 高码率
        self.assertNotIn("33", mxg_tags)  # 严禁珍宝楼
        self.assertNotIn("45", mxg_tags)  # 严禁自购

        # 5. TangPT: tags[4][] 应有 10(粤语/港台) 或 5(国语), 6(中字)
        tang_tags = TagMatcher.match_tags_for_site("tangpt", item)
        self.assertTrue("10" in tang_tags or "5" in tang_tags)
        self.assertIn("6", tang_tags)
        self.assertNotIn("45", tang_tags)  # 严禁首发
        self.assertNotIn("39", tang_tags)  # 严禁禁转

        # 6. CrabPT: tags[4][] 应有 6(粤语) 或 5(国语), 7(中字)，★★ 绝对禁止勾选 23 (Audiences)
        crab_tags = TagMatcher.match_tags_for_site("crabpt", item)
        self.assertTrue("6" in crab_tags or "5" in crab_tags)
        self.assertIn("7", crab_tags)
        self.assertNotIn("23", crab_tags, "CrabPT 严禁勾选 23 (Audiences / 观众)！")
        self.assertNotIn("1", crab_tags)   # 严禁禁转
        self.assertNotIn("2", crab_tags)   # 严禁自购

        # 7. HDKylin: tags[4][] 应有 5(国语), 6(中字)
        kylin_tags = TagMatcher.match_tags_for_site("hdkylin", item)
        self.assertIn("5", kylin_tags)
        self.assertIn("6", kylin_tags)
        self.assertNotIn("3", kylin_tags)  # 严禁官种

        # 8. LemonHD: tags[4][] 应有 5(国语), 6(中字)
        lemon_tags = TagMatcher.match_tags_for_site("lemonhd", item)
        self.assertIn("5", lemon_tags)
        self.assertIn("6", lemon_tags)

        # 9. HDTime: tags[4][] 应有 5(国语), 6(中字)
        hdtime_tags = TagMatcher.match_tags_for_site("hdtime", item)
        self.assertIn("5", hdtime_tags)
        self.assertIn("6", hdtime_tags)

        # 10. UBits: tags[4][] 应有 11(粤语/港台) 或 5(国语), 6(中字)
        ubits_tags = TagMatcher.match_tags_for_site("ubits", item)
        self.assertTrue("11" in ubits_tags or "5" in ubits_tags)
        self.assertIn("6", ubits_tags)

        # 11. SoulVoice: tags[4][] 应有 5(国语), 6(中字)
        sv_tags = TagMatcher.match_tags_for_site("soulvoice", item)
        self.assertIn("5", sv_tags)
        self.assertIn("6", sv_tags)

        # 12. PTerClub: 无 tags 控件，应返回空列表
        pter_tags = TagMatcher.match_tags_for_site("pterclub", item)
        self.assertEqual(len(pter_tags), 0)

    def test_western_classical_hires_tags(self):
        """测试欧美古典 Hi-Res 音乐标签推断"""
        item = CanonicalMusicItem(
            title="Berliner Philharmoniker - Beethoven: Symphonies 2020 - FLAC",
            artist="Berliner Philharmoniker",
            album="Beethoven: Symphonies",
            year="2020",
            format="FLAC",
            medium="CD",
            standard="Hi-Res",
            origin_site="opencd",
            tags=["欧美", "古典(Classical)", "发烧(HiFi)"],
            genre=["古典(Classical)"],
            tracks=[self.dummy_track]
        )

        # Musopia: 欧美(5), 古典(15), 发烧(17), OpenCD(31)
        m_tags = TagMatcher.match_tags_for_site("musopia", item)
        self.assertIn("5", m_tags)   # 欧美
        self.assertIn("15", m_tags)  # 古典
        self.assertIn("17", m_tags)  # 发烧
        self.assertIn("31", m_tags)  # OpenCD

        # Xingwan: 无损音乐(17), 英语(35)
        xw_tags = TagMatcher.match_tags_for_site("xingwan", item)
        self.assertIn("17", xw_tags)
        self.assertIn("35", xw_tags)

        # TangPT: 玄学(35 HiFi/发烧)
        tang_tags = TagMatcher.match_tags_for_site("tangpt", item)
        self.assertIn("35", tang_tags)

        # MuXueGe: 乐府(30), 高码率(41)
        mxg_tags = TagMatcher.match_tags_for_site("muxuege", item)
        self.assertIn("30", mxg_tags)
        self.assertIn("41", mxg_tags)

        # CrabPT: 绝对不含 Audiences (23)
        crab_tags = TagMatcher.match_tags_for_site("crabpt", item)
        self.assertNotIn("23", crab_tags)

    def test_japanese_acg_ost_tags(self):
        """测试日漫原声 OST 标签推断"""
        item = CanonicalMusicItem(
            title="久石譲 - 千と千尋の神隠し サウンドトラック 2001 - FLAC",
            artist="久石譲",
            album="千と千尋の神隠し サウンドトラック",
            year="2001",
            format="FLAC",
            medium="CD",
            tags=["日韩", "原声(OST)"],
            tracks=[self.dummy_track]
        )

        m_tags = TagMatcher.match_tags_for_site("musopia", item)
        self.assertIn("7", m_tags)   # 日韩
        self.assertIn("39", m_tags)  # japanese
        self.assertIn("16", m_tags)  # 原声(OST)

        uhd_tags = TagMatcher.match_tags_for_site("uhddolby", item)
        self.assertIn("ja", uhd_tags)  # 日语

    def test_live_concert_tags(self):
        """测试现场演唱会标签推断"""
        item = CanonicalMusicItem(
            title="蔡琴 - 蔡琴2007新经典演唱会Live 2007 - FLAC",
            artist="蔡琴",
            album="蔡琴2007新经典演唱会Live",
            year="2007",
            format="FLAC",
            medium="CD",
            description="蔡琴 2007 新经典 演唱会现场 Live",
            tracks=[self.dummy_track]
        )

        # 唐门: 演唱会(1)
        tang_tags = TagMatcher.match_tags_for_site("tangpt", item)
        self.assertIn("1", tang_tags)

        # 麒麟: LIVE现场(10)
        kylin_tags = TagMatcher.match_tags_for_site("hdkylin", item)
        self.assertIn("10", kylin_tags)

    def test_adapters_build_form_fields_integration(self):
        """集成测试：全矩阵 12 站点适配器 build_form_fields 注入标签字段正确性验证"""
        item = CanonicalMusicItem(
            title="群星 - 华语发烧经典 2022 - FLAC",
            artist="群星",
            album="华语发烧经典",
            year="2022",
            format="FLAC",
            medium="CD",
            standard="Lossless",
            origin_site="opencd",
            origin_torrent_id="998877",
            tags=["大陆", "流行(Pop)", "发烧(HiFi)"],
            tracks=[self.dummy_track]
        )

        adapters = [
            MusopiaAdapter(), UHDDolbyAdapter(), CrabPTAdapter(), XingwanAdapter(),
            MuXueGeAdapter(), TangPTAdapter(), HDKylinAdapter(), LemonHDAdapter(),
            HDTimeAdapter(), UBitsAdapter(), SoulVoiceAdapter(), PTerClubAdapter()
        ]

        for adapter in adapters:
            site = adapter.site_name.lower()
            fields = adapter.build_form_fields(item)
            self.assertIsInstance(fields, dict)

            if site == "musopia":
                self.assertIn("tags[4][]", fields)
                self.assertIn("31", fields["tags[4][]"])  # OpenCD
                self.assertIn("4", fields["tags[4][]"])   # 大陆
                self.assertIn("10", fields["tags[4][]"])  # 流行
            elif site == "uhddolby":
                self.assertIn("tags[]", fields)
                self.assertIn("gy", fields["tags[]"])     # 国语
                self.assertIn("zz", fields["tags[]"])     # 中字
            elif site == "crabpt":
                self.assertIn("tags[4][]", fields)
                self.assertIn("5", fields["tags[4][]"])   # 国语
                self.assertNotIn("23", fields["tags[4][]"], "CrabPT 必须排除 23 (Audiences)！")
            elif site == "xingwan":
                self.assertIn("tags[4][]", fields)
                self.assertIn("17", fields["tags[4][]"])  # 必选无损
                self.assertIn("5", fields["tags[4][]"])   # 国语
            elif site == "muxuege":
                self.assertIn("tags[4][]", fields)
                self.assertIn("30", fields["tags[4][]"])  # 必选乐府
            elif site == "tangpt":
                self.assertIn("tags[4][]", fields)
                self.assertIn("5", fields["tags[4][]"])   # 国语
            elif site == "hdkylin":
                self.assertIn("tags[4][]", fields)
                self.assertIn("5", fields["tags[4][]"])   # 国语
            elif site == "lemonhd":
                self.assertIn("tags[4][]", fields)
                self.assertIn("5", fields["tags[4][]"])   # 国语
            elif site == "hdtime":
                self.assertIn("tags[4][]", fields)
                self.assertIn("5", fields["tags[4][]"])   # 国语
            elif site == "ubits":
                self.assertIn("tags[4][]", fields)
                self.assertIn("5", fields["tags[4][]"])   # 国语
            elif site == "soulvoice":
                self.assertIn("tags[4][]", fields)
                self.assertIn("5", fields["tags[4][]"])   # 国语
            elif site == "pterclub":
                self.assertNotIn("tags[4][]", fields)
                self.assertNotIn("tags[]", fields)


if __name__ == "__main__":
    unittest.main()
