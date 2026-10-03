"""`readd_queue.py` 的纯逻辑单测 —— 用**临时目录**，不碰生产 `/data`、不碰 qB。

★ 为什么必须测它：延迟回加这条链路的**故障模式全是"静默"**——
    · 队列写了但读不回（版本不认识、路径不对）⇒ 种子永不做种，**没人知道**
    · 到点没写回剩余 ⇒ 条目**重复 add**（还好）或**丢**（很坏）
    · 坏文件让 daemon 主循环抛异常 ⇒ **所有阶段停摆**
  这些都不会在"发布成功"的日志里体现（`ERR-META-01`：没报错 ≠ 成立）。
  ⇒ 把队列的**读写一致 / 幂等 / 崩溃安全**钉死在单测里。

★ 判据必须**能失败**（`ERR-META-32`）：每条 assert 都配了"反例"注释。

★★ 在 Windows 本机跑要带 UTF-8，否则 emoji 打印会 GBK 报错（**只是本机控制台**，
   容器里 `sys.stdout.encoding == 'utf-8'`，无此问题）：
       PYTHONIOENCODING=utf-8 python tools/test_readd_queue.py

跑：python tools/test_readd_queue.py
"""
import base64
import json
import os
import sys
import tempfile
import types

import _bootstrap  # noqa: E402,F401  —— 把 <repo> 与 <repo>/src 都放进 sys.path（源码已移进 src/）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ★ `readd_queue` 顶部 `import config`，而 config 要 QB_PASS 等必填项
#   （本机多半没有）⇒ 用假 config 顶替，并把 DATA_DIR 指到临时目录。
#   本测试**只**测队列逻辑，不碰 config 的真实值。
_TMP = tempfile.mkdtemp(prefix="readd_queue_test_")
_fake = types.ModuleType("config")
_fake.DATA_DIR = _TMP
_fake.QB_HOST = "http://127.0.0.1:3020"
_fake.QB_USER = "admin"
_fake.QB_PASS = "x"
sys.modules["config"] = _fake

# torf 读 infohash：本机若无 torf，enqueue 会降级（infohash=""）——对队列逻辑仍成立。
# 为了让"幂等"那条能测，这里造一个**假 torf**，从字节前 20 字节当 infohash。
_fake_torf = types.ModuleType("torf")


class _FakeTorrent:
    def __init__(self, h):
        self.infohash = h

    @staticmethod
    def read_stream(stream):
        b = stream.read()
        # 取字节的 sha1 前 40 hex 当 infohash（确定性、可复现）
        import hashlib
        return _FakeTorrent(hashlib.sha1(b).hexdigest())


_fake_torf.Torrent = _FakeTorrent
sys.modules["torf"] = _fake_torf

import readd_queue as RQ  # noqa: E402

FAILS = []


def check(name, cond, why):
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} —— 反例：{why}")
        FAILS.append(name)


def _reset():
    try:
        os.remove(RQ.QUEUE_FILE)
    except FileNotFoundError:
        pass


def test_roundtrip():
    print("[往返] 入队 → 读回，字节逐字节相同")
    _reset()
    payload = b"d8:announce4:test4:infod4:name3:abc e"   # 任意字节
    ok = RQ.enqueue(payload, "/downloads", "Musopia", 15, note="t1")
    check("入队成功", ok, "enqueue 返回 False")
    q = RQ.load_queue()
    check("队列有 1 条", len(q["items"]) == 1, f"应有 1 条，得 {len(q['items'])}")
    it = q["items"][0]
    got = base64.b64decode(it["torrent_b64"])
    check("字节逐字节相同", got == payload, "★ base64 往返丢/改了字节 ⇒ 回加的 .torrent 是坏的")
    check("save_path 保留", it["save_path"] == "/downloads", "save_path 没存对")
    check("category 保留", it["category"] == "Musopia", "category 没存对")
    check("due 在未来", it["due"] > it["enqueued_at"], "due 没在入队时刻之后")


def test_dedup():
    print("[幂等] 同 infohash 不重复入队")
    _reset()
    p = b"same-bytes"
    RQ.enqueue(p, "/a", "X", 15)
    RQ.enqueue(p, "/a", "X", 15)          # 同字节 ⇒ 同 infohash
    q = RQ.load_queue()
    check("只 1 条", len(q["items"]) == 1,
          "★同一种子重复入队 ⇒ 到点回加两次（虽 qB 幂等，但队列不该重复）")


def test_delay_zero_noop():
    print("[delay<=0] 不入队")
    _reset()
    ok = RQ.enqueue(b"x", "/a", "X", 0)
    check("delay=0 返回 False", ok is False, "delay=0 却入队了")
    check("队列仍空", len(RQ.load_queue()["items"]) == 0,
          "★delay=0 不该入队（调用方应直接 add）")


def test_pop_due():
    print("[到点] pop_due 只取 due<=now 的，且不改盘")
    _reset()
    RQ.enqueue(b"a", "/a", "X", 15)      # due 在 15 分钟后
    import time
    due, rest = RQ.pop_due(now=time.time())
    check("未到点不选出", len(due) == 0 and len(rest) == 1,
          "★没到点的被选走了 ⇒ 提前回加，违背延迟本意")
    # 假装到了未来
    due2, rest2 = RQ.pop_due(now=time.time() + 3600)
    check("到点选出", len(due2) == 1, "到点了却没选出")
    # ★ pop_due 不落盘：盘上仍是 1 条
    check("pop_due 不落盘", len(RQ.load_queue()["items"]) == 1,
          "★pop_due 改了盘 ⇒ 取出与 ack 之间的失败会丢条目")


def test_ack_removes_done():
    print("[ack] 成功条目移出，剩余保留")
    _reset()
    RQ.enqueue(b"a", "/a", "X", 15)
    q = RQ.load_queue()
    ih = q["items"][0]["infohash"]
    RQ.ack([ih], [])                     # 全部成功，剩余为空
    check("ack 后队列空", len(RQ.load_queue()["items"]) == 0, "ack 没删掉成功条目")
    # 失败保留
    _reset()
    RQ.enqueue(b"b", "/b", "X", 15)
    q = RQ.load_queue()
    RQ.ack([], q["items"])               # 没成功，全部保留
    check("失败保留", len(RQ.load_queue()["items"]) == 1, "★失败的被误删 ⇒ 种子永不做种")


def test_corrupt_file_safe():
    print("[崩溃安全] 半截/坏 JSON ⇒ 当空队列，不抛")
    _reset()
    with open(RQ.QUEUE_FILE, "w", encoding="utf-8") as f:
        f.write('{"version": 1, "items": [{"due": ')   # 截断的 JSON
    try:
        q = RQ.load_queue()
        check("坏文件不抛", True, "")
        check("坏文件当空队列", q["items"] == [], "★坏文件没降级成空队列")
    except Exception as e:                                  # noqa: BLE001
        check("坏文件不抛", False, f"★抛了 {type(e).__name__} ⇒ 会带走 daemon 主循环")


def test_version_mismatch_safe():
    print("[版本] 不认识的版本 ⇒ 空队列（不猜旧格式）")
    _reset()
    with open(RQ.QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump({"version": 999, "items": [{"due": 1}]}, f)
    q = RQ.load_queue()
    check("版本不符当空", q["items"] == [], "★猜了旧格式 ⇒ 可能读出错的东西")


def main():
    test_roundtrip()
    test_dedup()
    test_delay_zero_noop()
    test_pop_due()
    test_ack_removes_done()
    test_corrupt_file_safe()
    test_version_mismatch_safe()
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败：{FAILS}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
