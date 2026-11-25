# -*- coding: utf-8 -*-
"""简介修整与来源链接合法性单测（纯离线）"""

import gzip
import hashlib
import io
import shutil
from unittest.mock import Mock, patch

from musichub.metadata.cover_service import (
    CoverImageService, image_bytes_evidence, image_file_evidence,
)
from musichub.onsite.cover_identity import compare_site_cover, resolve_cover_identity
from musichub.storage.pool_pairing import PoolPairingManager
import os
import tempfile
import unittest

from musichub.onsite.detail_archive import (
    load_published_targets, parse_detail_html, sanitize_html, sanitize_url,
)
from musichub.routing.graph import RoutingGraphManager
from musichub.onsite.editor import (
    normalize_descr, NO_COVER_NOTE, first_cover_url, has_bad_source_quote,
    rebuild_source_quote, replace_cover_block,
)
from musichub.adapters.descr_core import build_source_link, build_descr, NO_COVER_NOTE as CORE_NOTE


class TestNormalizeDescr(unittest.TestCase):
    def test_removes_empty_source_quote(self):
        bad = "[quote]转自OpenCD: [/quote]\n\n[quote][b]曲目列表：[/b]\n01. a[/quote]"
        new, changes = normalize_descr(bad)
        self.assertIn("移除空转载引用块", changes)
        self.assertNotIn("转自OpenCD", new)
        self.assertIn("曲目列表", new)

    def test_keeps_valid_source_quote(self):
        good = ("[quote]转自OpenCD: https://open.cd/details.php?id=88151[/quote]\n\n"
                "[center][img]https://img.example/c.jpg[/img][/center]")
        new, changes = normalize_descr(good)
        self.assertEqual(changes, [])
        self.assertEqual(new, good.strip())

    def test_adds_no_cover_note_after_quote(self):
        descr = "[quote]转自OpenCD: Album[/quote]\n\n[quote][b]曲目列表：[/b]\n01. a[/quote]"
        new, changes = normalize_descr(descr)
        self.assertIn("补无封面说明", changes)
        self.assertIn(NO_COVER_NOTE, new)
        self.assertLess(new.index("转自OpenCD"), new.index(NO_COVER_NOTE))
        self.assertLess(new.index(NO_COVER_NOTE), new.index("曲目列表"))

    def test_idempotent(self):
        once, _ = normalize_descr("[quote][b]曲目列表：[/b]\n01. a[/quote]")
        twice, changes2 = normalize_descr(once)
        self.assertEqual(changes2, [])
        self.assertEqual(once, twice)
        self.assertEqual(once.count(NO_COVER_NOTE), 1)

    def test_no_note_when_cover_present(self):
        descr = "[center][img]https://img.example/c.jpg[/img][/center]\n\n正文"
        new, changes = normalize_descr(descr)
        self.assertEqual(changes, [])
        self.assertNotIn(NO_COVER_NOTE, new)

    def test_collapses_excess_blank_lines(self):
        descr = "[quote]转自OpenCD: [/quote]\n\n\n\n正文"
        new, _ = normalize_descr(descr)
        self.assertNotIn("\n\n\n", new)


class TestSourceLinkAndNote(unittest.TestCase):
    def test_infohash_tid_dropped(self):
        link = build_source_link({"origin_site": "opencd",
                                  "origin_tid": "a" * 40, "torrent_name": "Album"})
        self.assertNotIn("details.php", link)
        self.assertEqual(link, "转自OpenCD: Album")

    def test_numeric_tid_kept(self):
        link = build_source_link({"origin_site": "opencd", "origin_tid": "88151"})
        self.assertEqual(link, "转自OpenCD: https://open.cd/details.php?id=88151")

    def test_local_not_declared(self):
        self.assertEqual(build_source_link({"origin_site": "local", "origin_tid": "1"}), "")

    def test_build_descr_note_only_without_cover(self):
        self.assertIn(CORE_NOTE, build_descr(tracklist="01. a"))
        self.assertNotIn(CORE_NOTE, build_descr(cover_url="https://x/c.jpg", tracklist="01. a"))


class TestRebuildSourceQuote(unittest.TestCase):
    BAD = ("[quote]转自OpenCD: " + "a" * 40 + "[/quote]\n\n"
           "[center][img]https://x/c.jpg[/img][/center]\n\n正文")

    def test_hash_replaced_by_details_link(self):
        new, changes = rebuild_source_quote(self.BAD, "opencd", "207025")
        self.assertTrue(changes)
        self.assertIn("https://open.cd/details.php?id=207025", new)
        self.assertNotIn("a" * 40, new)
        self.assertIn("[img]https://x/c.jpg", new)

    def test_hash_without_tid_falls_back_to_name(self):
        new, changes = rebuild_source_quote(self.BAD, "opencd", "", "Artist - Album")
        self.assertTrue(changes)
        self.assertIn("转自OpenCD: Artist - Album", new)

    def test_hash_without_tid_or_name_drops_quote(self):
        new, changes = rebuild_source_quote(self.BAD, "opencd", "", "")
        self.assertIn("删除 infohash 来源死链引用", changes)
        self.assertNotIn("转自OpenCD", new)
        self.assertIn("正文", new)

    def test_good_link_untouched(self):
        good = "[quote]转自OpenCD: https://open.cd/details.php?id=88151[/quote]\n\n正文"
        new, changes = rebuild_source_quote(good, "opencd", "207025")
        self.assertEqual(changes, [])
        self.assertEqual(new, good)

    def test_detector(self):
        self.assertTrue(has_bad_source_quote(self.BAD))
        self.assertFalse(has_bad_source_quote(
            "[quote]转自OpenCD: https://open.cd/details.php?id=1[/quote]"))

    def test_title_junk_trimmed(self):
        junk = "[quote]转自OpenCD: Artist - Album (FLAC) [ 免费 ] 剩余时间： 6天16时[/quote]\n正文"
        new, changes = rebuild_source_quote(junk, "opencd", "", "")
        self.assertTrue(changes)
        self.assertIn("转自OpenCD: Artist - Album (FLAC)", new)
        self.assertNotIn("剩余时间", new)

    def test_fake_details_link_with_hash(self):
        bad = "[quote]转自OpenCD: https://open.cd/details.php?id=" + "b" * 40 + "[/quote]"
        new, changes = rebuild_source_quote(bad, "opencd", "207025")
        self.assertTrue(changes)
        self.assertIn("details.php?id=207025", new)
        self.assertNotIn("b" * 40, new)

    def test_quote_with_name_upgraded_to_link(self):
        sample = "[quote]转自OpenCD: 070 Shake - Petrichor[/quote]\n\n[center][img]https://img/folder.jpg[/img][/center]"
        new, changes = rebuild_source_quote(sample, "opencd", "207056", "070 Shake - Petrichor")
        self.assertIn("升级来源引用为源站直链 (tid=207056)", changes)
        self.assertIn("[quote]转自OpenCD: https://open.cd/details.php?id=207056[/quote]", new)
        new2, changes2 = rebuild_source_quote(new, "opencd", "207056", "070 Shake - Petrichor")
        self.assertEqual(changes2, [])
        self.assertEqual(new2, new)


class TestDetailArchive(unittest.TestCase):
    def test_parse_counts_covers_and_sensitive_url(self):
        html = """<html><h1 id='top'>Artist - Album</h1>
        <div id='kdescr'><img src='https://img.example/c.jpg?token=secret'>
        <a href='https://open.cd/details.php?id=88151&passkey=secret'>来源</a></div>
        <table><tr><td>做种者</td><td><b>7</b></td></tr>
        <tr><td>下载者</td><td>2</td></tr><tr><td>完成数</td><td>19</td></tr></table></html>"""
        row = parse_detail_html(html, "https://xingwan.cc", "4365")
        self.assertEqual((row["seeders"], row["leechers"], row["completed"]), (7, 2, 19))
        self.assertNotIn("secret", row["cover_urls"][0])
        self.assertNotIn("secret", row["source_links"][0])
        self.assertNotIn("secret", sanitize_url("https://x/details.php?id=1&auth=abc"))
        self.assertNotIn("secret", sanitize_html("download.php?id=1&passkey=secret&amp;x=1"))

    def test_unknown_counts_stay_null(self):
        row = parse_detail_html("<h1>Album</h1><p>暂无计数</p>")
        self.assertIsNone(row["seeders"])
        self.assertIsNone(row["leechers"])
        self.assertIsNone(row["completed"])

    def test_permission_error_is_not_a_valid_detail_page(self):
        row = parse_detail_html("<h1>F.A.Q.</h1><p>错误 你没有该权限！</p>")
        self.assertEqual(row["page_status"], "ACCESS_DENIED")
        self.assertIsNone(row["seeders"])

    def test_latest_and_append_only_history_with_keyset(self):
        graph = RoutingGraphManager(":memory:")
        for fetched, seeders in ((10.0, 1), (20.0, None)):
            page = {
                "site": "xingwan", "tid": "4365", "fetch_status": "OK",
                "fetched_at": fetched, "parser_version": "test", "html_gzip": gzip.compress(b"html"),
                "html_sha256": str(fetched), "url": "https://xingwan.cc/details.php?id=4365",
            }
            graph.record_detail_page(page, {"seeders": seeders, "page_status": "OK"})
        latest = graph.get_detail_page("xingwan", "4365")
        self.assertEqual(latest["fetched_at"], 20.0)
        history = graph.get_detail_history("xingwan", "4365", limit=0)
        self.assertEqual(len(history), 2)
        self.assertIsNone(history[0]["seeders"])
        first = graph.list_detail_pages(limit=1)
        self.assertEqual(len(first), 1)
        rest = graph.list_detail_pages(after=(first[0]["fetched_at"], "xingwan", "4365"), limit=0)
        self.assertEqual(rest, [])
        graph._conn.close()

    def test_targets_merge_before_dedup_without_source_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "m.db")
            graph = RoutingGraphManager(db)
            with graph._conn:
                graph._conn.execute("""INSERT INTO publish_lineage
                    (target_site,target_tid,source_site,source_tid,b_hash,status)
                    VALUES ('xingwan','4365','opencd','123','abc','PUBLISHED')""")
                graph._conn.execute("""INSERT INTO publish_attempt_log
                    (album_name,target_site,result,target_torrent_id) VALUES
                    ('Album','xingwan','PUBLISHED','4365')""")
                graph._conn.execute("""INSERT INTO site_publish_graph
                    (content_fingerprint,origin_site,origin_torrent_id,target_site,target_torrent_id)
                    VALUES ('f2','opencd','124','xingwan','4319')""")
                graph._conn.execute("""INSERT INTO site_publish_graph
                    (content_fingerprint,origin_site,origin_torrent_id,target_site,target_torrent_id)
                    VALUES ('f3','opencd','125','tangpt','72455')""")
            rows = load_published_targets(db)
            graph._conn.close()
            self.assertCountEqual([(r["target_site"], r["target_tid"]) for r in rows],
                                  [("xingwan", "4319"), ("xingwan", "4365"), ("tangpt", "72455")])
            hit = next(r for r in rows if r["target_tid"] == "4365")
            self.assertEqual(hit["b_hash"], "abc")
            self.assertEqual(hit["source_tid"], "123")
            filtered = load_published_targets(db, site="xingwan", tid="4319", days=1)
            self.assertEqual([(r["target_site"], r["target_tid"]) for r in filtered],
                             [("xingwan", "4319")])


def make_image_bytes(color=(255, 0, 0), image_format="JPEG") -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(out, format=image_format)
    return out.getvalue()


def make_torrent_bytes(path: str) -> bytes:
    import torf
    torrent = torf.Torrent(path=path, trackers=["http://tracker.example/announce"], private=True)
    torrent.generate()
    out = io.BytesIO()
    torrent.write_stream(out)
    return out.getvalue()


class TestImageEvidenceAndFirstCover(unittest.TestCase):
    def test_valid_and_invalid_image_bytes(self):
        payload = make_image_bytes()
        evidence = image_bytes_evidence(payload)
        self.assertTrue(evidence["ok"])
        self.assertEqual(evidence["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(evidence["format"], "JPEG")
        self.assertFalse(image_bytes_evidence(b"\xff\xd8\xffnot-a-jpeg")["ok"])
        self.assertFalse(image_bytes_evidence(b"<html>not image</html>")["ok"])

    def test_file_evidence_reads_actual_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cover.jpg")
            payload = make_image_bytes()
            with open(path, "wb") as fh:
                fh.write(payload)
            evidence = image_file_evidence(path)
            self.assertTrue(evidence["ok"])
            self.assertEqual(evidence["path"], path)
            self.assertEqual(evidence["size"], len(payload))

    def test_only_first_image_is_cover_and_replace_preserves_rest(self):
        descr = "[center][img]https://img/old.jpg[/img][/center]\n正文\n[img]https://img/second.jpg[/img]"
        self.assertEqual(first_cover_url(descr), "https://img/old.jpg")
        new = replace_cover_block(descr, "https://img/new.jpg")
        self.assertIn("[center][img]https://img/new.jpg[/img][/center]", new)
        self.assertIn("[img]https://img/second.jpg[/img]", new)
        self.assertNotIn("old.jpg", new)

    def test_compare_first_cover_match_mismatch_and_broken(self):
        expected = make_image_bytes()
        other = make_image_bytes((0, 0, 255))
        identity = {"ok": True, "expected_sha256": hashlib.sha256(expected).hexdigest()}
        evidence = {
            "https://img/expected.jpg": image_bytes_evidence(expected),
            "https://img/other.jpg": image_bytes_evidence(other),
            "https://img/broken.jpg": {"ok": False, "error": "HTTP 404"},
        }
        match = compare_site_cover(
            identity, ["https://img/expected.jpg", "https://img/other.jpg"], evidence.get)
        self.assertEqual(match["status"], "MATCH")
        mismatch = compare_site_cover(
            identity, ["https://img/other.jpg", "https://img/expected.jpg"], evidence.get)
        self.assertEqual(mismatch["status"], "COVER_MISMATCH")
        broken = compare_site_cover(identity, ["https://img/broken.jpg"], evidence.get)
        self.assertEqual(broken["status"], "COVER_BROKEN")
        self.assertEqual(compare_site_cover(identity, [], evidence.get)["status"], "COVER_MISSING")

    def test_strict_cache_url_mismatch_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cover.jpg")
            expected = make_image_bytes()
            with open(path, "wb") as fh:
                fh.write(expected)
            svc = CoverImageService(cache_dir=os.path.join(tmp, "cache"),
                                    db_path=os.path.join(tmp, "cache.db"))
            with patch.object(svc, "fetch_image_evidence",
                              return_value=image_bytes_evidence(make_image_bytes((0, 0, 255)))), \
                    patch.object(svc, "_resolve_expected_cover",
                                 return_value=("https://img/expected.jpg", "strict")) as resolve:
                url, _ = svc.get_site_cover(
                    "xingwan", cached_site_covers={"xingwan": "https://img/wrong.jpg"},
                    expected_image_path=path,
                )
            self.assertEqual(url, "https://img/expected.jpg")
            resolve.assert_called_once()
            svc.close()

    def test_strict_uploaded_url_must_match_before_cache_or_site_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cover.jpg")
            expected = make_image_bytes()
            with open(path, "wb") as fh:
                fh.write(expected)
            svc = CoverImageService(cache_dir=os.path.join(tmp, "cache"),
                                    db_path=os.path.join(tmp, "cache.db"))
            try:
                with patch.object(svc, "upload_picgo",
                                  return_value="https://img/transformed.jpg") as upload, \
                        patch.object(svc, "fetch_image_evidence",
                                     return_value=image_bytes_evidence(make_image_bytes((0, 0, 255)))), \
                        patch.object(svc, "_db_set_cover") as set_cover:
                    url, why = svc.get_site_cover("xingwan", expected_image_path=path)
                self.assertEqual(url, "")
                self.assertIn("提交前 SHA256", why)
                upload.assert_called_once_with(
                    path, allow_reencode=False,
                    expected_sha256=hashlib.sha256(expected).hexdigest(),
                )
                set_cover.assert_not_called()
            finally:
                svc.close()

    def test_strict_picgo_duplicate_never_reencodes_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cover.jpg")
            with open(path, "wb") as fh:
                fh.write(make_image_bytes())
            svc = CoverImageService(cache_dir=os.path.join(tmp, "cache"),
                                    db_path=os.path.join(tmp, "cache.db"))
            response = Mock(status_code=400, text="duplicate image")
            response.json.return_value = {}
            try:
                with patch("musichub.metadata.cover_service.requests.post",
                           return_value=response), \
                        patch.object(svc, "_reencode_bytes") as reencode:
                    url = svc._chevereto_upload(
                        path, "https://www.picgo.net/api/1/upload", "secret",
                        retries=0, allow_reencode=False,
                    )
                self.assertIsNone(url)
                reencode.assert_not_called()
            finally:
                svc.close()

    def test_strict_picgo_bad_cache_is_bypassed_before_recaching(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cover.jpg")
            expected = make_image_bytes()
            with open(path, "wb") as fh:
                fh.write(expected)
            svc = CoverImageService(cache_dir=os.path.join(tmp, "cache"),
                                    db_path=os.path.join(tmp, "cache.db"))
            try:
                with patch.dict(os.environ, {"PICGO_API_KEY": "secret"}), \
                        patch.object(svc, "_cache_url_on_host",
                                     return_value="https://img/transformed.jpg"), \
                        patch.object(svc, "_chevereto_upload",
                                     return_value="https://img/original.jpg") as upload, \
                        patch.object(svc, "fetch_image_evidence",
                                     side_effect=[image_bytes_evidence(make_image_bytes((0, 0, 255))),
                                                  image_bytes_evidence(expected)]), \
                        patch.object(svc, "_save_url_cache") as save_cache:
                    url = svc.upload_picgo(
                        path, allow_reencode=False,
                        expected_sha256=hashlib.sha256(expected).hexdigest(),
                    )
                self.assertEqual(url, "https://img/original.jpg")
                upload.assert_called_once_with(
                    path, "https://www.picgo.net/api/1/upload", "secret",
                    allow_reencode=False,
                )
                save_cache.assert_called_once()
            finally:
                svc.close()

    def test_strict_lsky_never_shrinks_oversize_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cover.jpg")
            expected = make_image_bytes()
            with open(path, "wb") as fh:
                fh.write(expected)
            svc = CoverImageService(cache_dir=os.path.join(tmp, "cache"),
                                    db_path=os.path.join(tmp, "cache.db"))
            try:
                with patch("musichub.metadata.cover_service.LSKY_MAX_BYTES", 1), \
                        patch.object(svc, "_get_lsky_token", return_value="token"), \
                        patch.object(svc, "_shrink_for_lsky") as shrink, \
                        patch.object(svc, "_lsky_post_upload") as post:
                    url = svc.upload_lsky(
                        path, expected_sha256=hashlib.sha256(expected).hexdigest(),
                    )
                self.assertIsNone(url)
                self.assertIn("禁止缩图", svc.last_lsky_error)
                shrink.assert_not_called()
                post.assert_not_called()
            finally:
                svc.close()


class TestCoverIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory()
        self.tmp = self.tmp_obj.name
        self.root1 = os.path.join(self.tmp, "music1")
        self.root2 = os.path.join(self.tmp, "music2")
        self.album = os.path.join(self.root1, "Album")
        os.makedirs(self.album)
        with open(os.path.join(self.album, "01.flac"), "wb") as fh:
            fh.write(b"fLaC" + b"a" * 128)
        self.cover_path = os.path.join(self.album, "cover.jpg")
        with open(self.cover_path, "wb") as fh:
            fh.write(make_image_bytes())
        self.db_path = os.path.join(self.tmp, "musichub.db")
        os.makedirs(os.path.join(self.tmp, "torrents"))
        payload = make_torrent_bytes(self.album)
        self.torrent_path = os.path.join(self.tmp, "torrents", "xingwan_4365.torrent")
        with open(self.torrent_path, "wb") as fh:
            fh.write(payload)
        _name, _files, _size, self.b_hash = PoolPairingManager.parse_torrent(payload)
        self.target = {
            "target_site": "xingwan", "target_tid": "4365",
            "source_site": "opencd", "source_tid": "123", "b_hash": self.b_hash,
        }
        self.service = CoverImageService(cache_dir=os.path.join(self.tmp, "cache"),
                                         db_path=os.path.join(self.tmp, "cache.db"))

    def tearDown(self):
        self.service.close()
        self.tmp_obj.cleanup()

    def resolve(self, roots=None, target=None):
        return resolve_cover_identity(
            target or self.target, self.db_path,
            pool_roots=roots or [("music1", self.root1)], cover_service=self.service,
        )

    def test_exact_payload_and_cover_identity(self):
        result = self.resolve()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["b_hash"], self.b_hash)
        self.assertEqual(result["local_path"], os.path.realpath(self.album))
        self.assertEqual(result["expected_sha256"], hashlib.sha256(make_image_bytes()).hexdigest())

    def test_wrong_lineage_path_fails_closed(self):
        target = {**self.target, "local_path": os.path.join(self.root1, "Another Album")}
        result = self.resolve(target=target)
        self.assertEqual(result["status"], "LOCAL_PATH_MISMATCH")

    def test_payload_size_mismatch_is_not_identity(self):
        with open(os.path.join(self.album, "01.flac"), "ab") as fh:
            fh.write(b"changed")
        result = self.resolve()
        self.assertEqual(result["status"], "PAYLOAD_UNRESOLVED")

    def test_multiple_exact_pools_require_identical_cover_bytes(self):
        album2 = os.path.join(self.root2, "Album")
        shutil.copytree(self.album, album2)
        with open(os.path.join(album2, "cover.jpg"), "wb") as fh:
            fh.write(make_image_bytes((0, 0, 255)))
        result = self.resolve(roots=[("music1", self.root1), ("music2", self.root2)])
        self.assertEqual(result["status"], "POOL_COVER_CONFLICT")
        self.assertEqual(len(result["candidates"]), 2)

    def test_b_hash_mismatch_and_missing_lineage_fail_closed(self):
        mismatch = self.resolve(target={**self.target, "b_hash": "0" * 40})
        self.assertEqual(mismatch["status"], "B_TORRENT_UNRESOLVED")
        no_lineage = self.resolve(target={"target_site": "xingwan", "target_tid": "4365"})
        self.assertEqual(no_lineage["status"], "LINEAGE_UNRESOLVED")

    def test_music5_is_hard_blocked(self):
        result = self.resolve(roots=[("music5", self.root1)])
        self.assertEqual(result["status"], "FORBIDDEN_ROOT")


class TestOriginMusicTitleCleaning(unittest.TestCase):
    def test_clean_origin_titles(self):
        from musichub.adapters.descr_core import clean_origin_music_title

        cases = [
            ("070 Shake - Petrichor 2024 - FLAC 分軌", "070 Shake - Petrichor 2024 - FLAC"),
            ("上官萍 - 怀念经典-1 2017 - FLAC 分轨 - Yuekui", "上官萍 - 怀念经典-1 2017 - FLAC"),
            ("齐秦 - 我拿什么爱你 1998 - FLAC 分轨", "齐秦 - 我拿什么爱你 1998 - FLAC"),
            ("JJ Lin - Lost N Found 2011 - FLAC - PTerMUSIC", "JJ Lin - Lost N Found 2011 - FLAC - PTerMUSIC"),
            ("Various Artists - 2024 Grammy Nominees [FLAC 整轨]", "Various Artists - 2024 Grammy Nominees [FLAC]"),
            ("群星 - 华语流行金曲 2023 (WAV/分轨)", "群星 - 华语流行金曲 2023 (WAV)"),
            ("Taylor Swift - Midnights 2022 - FLAC 24bit - Deluxe", "Taylor Swift - Midnights 2022 - FLAC 24bit - Deluxe"),
            ("周杰伦 - 范特西 2001 - FLAC - alin13788", "周杰伦 - 范特西 2001 - FLAC"),
            ("Various - Eurobeat [整軌版]", "Various - Eurobeat"),
        ]
        for raw, expected in cases:
            cleaned = clean_origin_music_title(raw)
            self.assertEqual(cleaned, expected, f"Failed on {raw}: got {cleaned}, expected {expected}")

    def test_fallback_when_empty(self):
        from musichub.adapters.descr_core import clean_origin_music_title
        task = {
            "formatted_title": "070 Shake - Petrichor",
            "artist": "070 Shake",
            "album": "Petrichor",
            "year": "2024",
            "format": "FLAC",
        }
        res = clean_origin_music_title("", fallback_task=task)
        self.assertIn("070 Shake - Petrichor 2024 - FLAC", res)

    def test_adapter_title_divergence(self):
        from musichub.core.models import CanonicalMusicItem
        from musichub.adapters.sites.tangpt import TangPTAdapter
        from musichub.adapters.sites.pterclub import PTerClubAdapter

        item = CanonicalMusicItem(
            title="070 Shake - Petrichor",
            origin_title="070 Shake - Petrichor 2024 - FLAC 分軌",
            artist="070 Shake",
            album="Petrichor",
            year="2024",
            format="FLAC",
        )

        tangpt_adapter = TangPTAdapter()
        main_gen, _ = tangpt_adapter.format_title(item)
        # 通用站点：回归原种风格（仅去分轨，保留 2024 与 FLAC，不强推 16bit 44.1kHz）
        self.assertEqual(main_gen, "070 Shake - Petrichor 2024 - FLAC")

        pter_adapter = PTerClubAdapter()
        main_pter, _ = pter_adapter.format_title(item)
        # 猫站：必须包含 16bit 44.1kHz 格式规范
        self.assertEqual(main_pter, "070 Shake - Petrichor 2024 - FLAC 16bit 44.1kHz")


if __name__ == "__main__":
    unittest.main()
