# -*- coding: utf-8 -*-
"""针对来源穿透、TID 清洗与封面防静默降级护栏的回归测试套件。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from musichub.adapters.descr_core import (
    resolve_canonical_source,
    build_source_link,
    _extract_clean_tid,
    SOURCE_SITES,
)
from musichub.core.models import CanonicalMusicItem
from musichub.adapters.sites.ubits import UBitsAdapter
from musichub.adapters.sites.soulvoice import SoulVoiceAdapter


class TestRemedyPipeline(unittest.TestCase):

    def test_clean_tid_extraction(self):
        # 纯数字
        self.assertEqual(_extract_clean_tid("205464"), "205464")
        # 带前缀
        self.assertEqual(_extract_clean_tid("opencd#205464"), "205464")
        # 完整 URL
        self.assertEqual(_extract_clean_tid("https://open.cd/details.php?id=205466"), "205466")
        # 40 位 hash 应当被过滤（不是 3~8 位合法 TID）
        self.assertEqual(_extract_clean_tid("a" * 40), "")

    def test_build_source_link_formatting(self):
        # OpenCD
        task_ocd = {"origin_site": "opencd", "origin_tid": "205464"}
        self.assertEqual(
            build_source_link(task_ocd),
            "转自OpenCD: https://open.cd/details.php?id=205464"
        )
        # 带 opencd# 前缀的 TID 依然能正确解析
        task_ocd_prefixed = {"origin_site": "opencd", "origin_tid": "opencd#205464"}
        self.assertEqual(
            build_source_link(task_ocd_prefixed),
            "转自OpenCD: https://open.cd/details.php?id=205464"
        )
        # JPopsuki
        task_jps = {"origin_site": "jpopsuki"}
        self.assertEqual(
            build_source_link(task_jps),
            "转自JPopsuki，感谢原制作者发布。"
        )
        # Local / Unknown
        self.assertEqual(build_source_link({"origin_site": "local"}), "")
        self.assertEqual(build_source_link({"origin_site": "unknown"}), "")

    def test_canonical_source_resolver_jpopsuki(self):
        # 模拟蟹黄堡 239379 的正文
        descr = (
            "引用\n"
            "转自JPopsuki，感谢原制作者发布。\n"
            "King & Prince 是日本男性偶像团体..."
        )
        site, tid, custom_link = resolve_canonical_source(descr, default_site="crabpt", default_tid="239379")
        self.assertEqual(site, "jpopsuki")
        self.assertEqual(custom_link, "转自JPopsuki，感谢原制作者发布。")

    def test_canonical_source_resolver_redacted(self):
        descr = "转自Redacted，感谢原发布者！"
        site, tid, custom_link = resolve_canonical_source(descr, default_site="tangpt", default_tid="12345")
        self.assertEqual(site, "redacted")
        self.assertIn("Redacted", custom_link)

    def test_canonical_source_resolver_default(self):
        descr = "普通专辑介绍，无任何外源声明。"
        site, tid, custom_link = resolve_canonical_source(descr, default_site="opencd", default_tid="99999")
        self.assertEqual(site, "opencd")
        self.assertEqual(tid, "99999")
        self.assertEqual(custom_link, "")

    def test_ubits_and_soulvoice_sites_registered(self):
        self.assertIn("ubits", SOURCE_SITES)
        self.assertIn("soulvoice", SOURCE_SITES)
        self.assertIn("jpopsuki", SOURCE_SITES)

    def test_physical_cover_guard_against_fake_no_cover_note(self):
        # 构造一个本地有图片的临时目录
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            cover_file = os.path.join(tmpdir, "cover.jpg")
            with open(cover_file, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100) # mock jpeg header

            item = CanonicalMusicItem(
                title="Test Album",
                album="Test Album",
                artist="Test Artist",
                year="2022",
                format="FLAC",
                medium="CD",
                standard="Lossless",
                origin_site="opencd",
                origin_torrent_id="205466",
                local_path=tmpdir,
                tracks=[],
            )

            adapter = UBitsAdapter()
            # 模拟 get_site_cover 失败（返回空）
            adapter.cover_service.get_site_cover = lambda *args, **kwargs: ("", "mock upload fail")
            body = adapter.build_body(item)
            # 护栏生效：绝对不能包含“源站和本地均无封面”
            self.assertNotIn("源站和本地均无封面", body)
            # 来源引用仍然完整保留
            self.assertIn("转自OpenCD: https://open.cd/details.php?id=205466", body)


if __name__ == '__main__':
    unittest.main()
