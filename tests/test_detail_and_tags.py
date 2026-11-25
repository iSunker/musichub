# -*- coding: utf-8 -*-
"""测试详情页抓取、全量标签提取与数据库留存。"""

import json
import os
import sqlite3
import unittest
from bs4 import BeautifulSoup

from musichub.collector.source_detail import extract_tags, parse_detail_html
from musichub.onsite.detail_archive import build_adapter, parse_detail_html as parse_archive_detail
from musichub.routing.graph import RoutingGraphManager


class TestDetailAndTags(unittest.TestCase):
    def test_extract_tags_opencd(self):
        html = """
        <table class="main">
            <tr>
                <td class="rowtitle">標籤列表：</td>
                <td>
                    <i><a href="torrents.php?tags=3">港台</a></i>
                    <i><a href="torrents.php?tags=6">流行(Pop)</a></i>
                    <i><a href="torrents.php?tags=18">原声(OST)</a></i>
                </td>
            </tr>
        </table>
        """
        soup = BeautifulSoup(html, "html.parser")
        tags = extract_tags(soup)
        self.assertEqual(len(tags), 3)
        self.assertEqual(tags[0], {"name": "港台", "id": "3"})
        self.assertEqual(tags[1], {"name": "流行(Pop)", "id": "6"})
        self.assertEqual(tags[2], {"name": "原声(OST)", "id": "18"})

    def test_extract_tags_uhddolby(self):
        html = """
        <table>
            <tr>
                <td align="right" class="rowhead nowrap" valign="top">标签</td>
                <td align="left" class="rowfollow" valign="top">
                    <span class="tags tgf" title="官方资源">官方</span>
                    <span class="tags tgy" title="国语音轨">国语</span>
                    <span class="tags tzz" title="中文字幕">中字</span>
                </td>
            </tr>
        </table>
        """
        soup = BeautifulSoup(html, "html.parser")
        tags = extract_tags(soup)
        self.assertEqual(len(tags), 3)
        self.assertEqual(tags[0], {"name": "官方", "id": "gf", "title": "官方资源"})
        self.assertEqual(tags[1], {"name": "国语", "id": "gy", "title": "国语音轨"})
        self.assertEqual(tags[2], {"name": "中字", "id": "zz", "title": "中文字幕"})

    def test_extract_tags_musopia(self):
        html = """
        <table>
            <tr>
                <td align="right" class="rowhead nowrap" valign="top">标签</td>
                <td align="left" class="rowfollow" valign="top">
                    <span style="background-color:#6C3365;color:#ffffff;" title="">原声(OST)</span>
                    <span style="background-color:#FF5809;color:#ffffff;" title="">古典(Classical)</span>
                    <span style="background-color:#00FFFF;color:#ffffff;" title="">电子(Electronic)</span>
                </td>
            </tr>
        </table>
        """
        soup = BeautifulSoup(html, "html.parser")
        tags = extract_tags(soup)
        self.assertEqual(len(tags), 3)
        self.assertEqual(tags[0]["name"], "原声(OST)")
        self.assertEqual(tags[1]["name"], "古典(Classical)")
        self.assertEqual(tags[2]["name"], "电子(Electronic)")

    def test_extract_tags_sub_elems_and_text(self):
        html_font = """
        <table>
            <tr><td class="rowhead">标签</td><td class="rowfollow"><font color="blue">无损音乐</font> <b title="首发资源">首发</b></td></tr>
        </table>
        """
        tags_font = extract_tags(BeautifulSoup(html_font, "html.parser"))
        self.assertEqual(len(tags_font), 2)
        self.assertEqual(tags_font[0]["name"], "无损音乐")
        self.assertEqual(tags_font[1]["name"], "首发")
        self.assertEqual(tags_font[1]["title"], "首发资源")

        html_text = """
        <table>
            <tr><td class="rowhead">Tags</td><td class="rowfollow">国风音乐, 珍宝楼 / 高码率</td></tr>
        </table>
        """
        tags_text = extract_tags(BeautifulSoup(html_text, "html.parser"))
        self.assertEqual(len(tags_text), 3)
        self.assertEqual([t["name"] for t in tags_text], ["国风音乐", "珍宝楼", "高码率"])

    def test_parse_detail_html_with_tags_and_genre_autofill(self):
        html = """
        <html>
            <head><title>OpenCD :: 種子詳情 "王菲 - 寓言 2000 - FLAC"</title></head>
            <body>
                <h1 id="top">王菲 - 寓言 2000 - FLAC</h1>
                <table>
                    <tr><td class="rowtitle">標籤列表：</td><td><i><a href="torrents.php?tags=3">港台</a></i><i><a href="torrents.php?tags=6">流行(Pop)</a></i></td></tr>
                    <tr><td class="rowtitle">Hash碼：</td><td>1122334455667788990011223344556677889900</td></tr>
                </table>
                <div id="kdescr"><p>专辑介绍内容</p></div>
            </body>
        </html>
        """
        res = parse_detail_html(html)
        self.assertEqual(res["info_hash"], "1122334455667788990011223344556677889900")
        self.assertEqual(res["artist"], "王菲")
        self.assertEqual(res["album"], "寓言")
        self.assertEqual(res["year"], "2000")
        self.assertEqual(len(res["tags"]), 2)
        self.assertIn("流行(Pop)", res["genre"])
        self.assertIn('"name": "港台"', res["tags_json"])
        self.assertEqual(res["tags_str"], "港台, 流行(Pop)")

    def test_routing_graph_tags_json(self):
        # 内存数据库测试
        mgr = RoutingGraphManager(":memory:")
        info_hash = "aabbccddeeff00112233445566778899aabbccdd"
        mgr.upsert_source_task(
            info_hash=info_hash,
            source_site="opencd",
            source_tid="198601",
            source_name="Test Album",
        )
        sample_tags = [{"id": "3", "name": "港台"}, {"id": "6", "name": "流行(Pop)"}]
        sample_json = json.dumps(sample_tags, ensure_ascii=False)
        mgr.update_source_meta(
            info_hash,
            artist="Test Artist",
            album="Test Album",
            year="2022",
            tags_json=sample_json,
        )
        task = mgr.get_source_task(info_hash)
        self.assertIsNotNone(task)
        self.assertEqual(task["tags_json"], sample_json)
        self.assertEqual(task["artist"], "Test Artist")

    def test_daemon_parse_task_tags_and_genre(self):
        from musichub.daemon import parse_task_tags_and_genre
        task = {
            "tags_json": '[{"id": "3", "name": "港台"}, {"id": "6", "name": "流行(Pop)"}]',
            "genre_json": "流行(Pop)/发烧(HiFi)"
        }
        tags, genre = parse_task_tags_and_genre(task)
        self.assertEqual(tags, ["港台", "流行(Pop)"])
        self.assertEqual(genre, ["流行(Pop)", "发烧(HiFi)"])

    def test_build_adapter_coverage(self):
        sites = [
            "opencd", "musopia", "crabpt", "pterclub", "xingwan", "hdkylin",
            "tangpt", "muxuege", "lemonhd", "uhddolby", "hdtime", "ubits", "soulvoice"
        ]
        for s in sites:
            ad = build_adapter(s)
            self.assertIsNotNone(ad)
            self.assertTrue(len(ad.base_url) > 0)


if __name__ == "__main__":
    unittest.main()
