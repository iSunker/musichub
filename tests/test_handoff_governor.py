# -*- coding: utf-8 -*-
"""交棒器身份、时长与回滚顺序单元测试（不触网）"""

import time
import unittest

from musichub.storage.handoff_governor import HandoffGovernor


class FakeGovernor(HandoffGovernor):
    def __init__(self, resume_ok=True, graph_manager=None):
        super().__init__(
            qb_publish_host="127.0.0.1", qb_publish_port=1,
            ssd_publish_dir="", graph_manager=graph_manager,
        )
        self.resume_ok = resume_ok
        self.calls = []

    def _api_post(self, base_url, endpoint, data):
        self.calls.append((base_url, endpoint, data.get("hashes")))
        return "Ok."

    def _stop_publish_task(self, info_hash):
        self.calls.append((self.publish_url, "/api/v2/torrents/stop", info_hash))
        return True

    def _resume_task(self, base_url, info_hash, timeout=8.0):
        self.calls.append((base_url, "/api/v2/torrents/start", info_hash))
        return self.resume_ok


class FakeGraph:
    def __init__(self):
        self.events = []

    def record_pool_event(self, info_hash, event, **kwargs):
        self.events.append((info_hash, event, kwargs))


class TestHandoffGovernorResume(unittest.TestCase):
    def _torrent(self):
        return {
            "hash": "a" * 40,
            "name": "Album",
            "ratio": 1.2,
            "size": 100,
            "progress": 1.0,
        }

    def _lib(self, info_hash="b" * 40):
        return {
            "host": "http://127.0.0.1:3260",
            "hash": info_hash,
            "name": "Album",
            "size": 100,
            "state": "pausedUP",
        }

    def test_name_size_match_never_deletes_3010(self):
        gov = FakeGovernor()
        ok, msg = gov.execute_handoff_one(
            self._torrent(), self._lib(), match_kind="name+size", dry_run=False,
        )
        self.assertFalse(ok)
        self.assertIn("同 infohash", msg)
        self.assertEqual(gov.calls, [], "弱匹配必须在任何 stop/resume/delete 之前拒绝")

    def test_hash_match_resumes_same_b_hash_then_deletes_3010(self):
        gov = FakeGovernor()
        b_hash = "a" * 40
        ok, msg = gov.execute_handoff_one(
            self._torrent(), self._lib(b_hash), match_kind="hash", dry_run=False,
        )
        self.assertTrue(ok, msg)
        resumed = [c for c in gov.calls if c[0] == "http://127.0.0.1:3260" and c[1].endswith("start")]
        self.assertEqual(resumed[0][2], b_hash)
        deleted = [c for c in gov.calls if c[0] == gov.publish_url and c[1].endswith("delete")]
        self.assertEqual(deleted[0][2], b_hash)

    def test_resume_failure_keeps_3010(self):
        gov = FakeGovernor(resume_ok=False)
        b_hash = "a" * 40
        ok, msg = gov.execute_handoff_one(
            self._torrent(), self._lib(b_hash), match_kind="hash", dry_run=False,
        )
        self.assertFalse(ok)
        self.assertIn("唤醒失败", msg)
        self.assertEqual([c for c in gov.calls if c[1].endswith("delete")], [])
        restarted = [c for c in gov.calls if c[0] == gov.publish_url and c[1].endswith("start")]
        self.assertTrue(restarted, "放弃交棒前必须恢复 3010 做种")

    def test_batch_rejects_name_size_before_any_qb_write(self):
        gov = FakeGovernor()
        count, details = gov._execute_batch([
            (self._torrent(), "达标", "name+size", self._lib()),
        ])
        self.assertEqual(count, 0)
        self.assertFalse(details[0]["success"])
        self.assertIn("同 infohash", details[0]["handoff_result"])
        self.assertEqual(gov.calls, [])

    def test_run_round_never_readies_name_size(self):
        gov = FakeGovernor()
        torrent = self._torrent()
        torrent.update(seeding_time=22000, num_complete=0)
        gov.get_publish_torrents = lambda: [torrent]
        gov.get_library_index = lambda: ({}, {"Album": self._lib()})
        result = gov.run_handoff_round(dry_run=True)
        self.assertEqual(result["ready_count"], 0)
        self.assertEqual(result["handed_off_count"], 0)

    def test_eligibility_uses_real_seeding_time_not_added_on(self):
        gov = FakeGovernor()
        now = time.time()
        torrent = self._torrent()
        torrent.update(added_on=now - 100000, seeding_time=0, num_complete=0)
        eligible, _ = gov.evaluate_eligible(torrent, now)
        self.assertFalse(eligible, "加入很久但从未实际做种不得保底交棒")
        torrent["seeding_time"] = 21601
        eligible, _ = gov.evaluate_eligible(torrent, now)
        self.assertTrue(eligible)

    def test_handoff_event_records_current_b_hash(self):
        graph = FakeGraph()
        gov = FakeGovernor(graph_manager=graph)
        gov._record_handoff_event(self._torrent(), self._lib())
        self.assertEqual(graph.events[0][0], "a" * 40)
        self.assertIn("match_kind=hash", graph.events[0][2]["reason"])


if __name__ == "__main__":
    unittest.main()
