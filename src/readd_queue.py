"""延迟回加 qB 待办队列管理模块。

在发布成功后将种子任务延迟指定时间（如 15 分钟）再注入 qBittorrent 做种，
实现错峰与平滑做种流转。支持原子持久化读写与到期出队重试机制。
"""

import base64
import json
import os
import time

import config

QUEUE_FILE = os.path.join(config.DATA_DIR, "readd_queue.json")
QUEUE_VERSION = 1


def _now():
    return time.time()


def load_queue():
    """读取待办队列。文件损坏或不存在时返回空队列结构。"""
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"version": QUEUE_VERSION, "items": []}
    except Exception as e:
        print(f"⚠️  [readd_queue] 读队列失败，按空队列处理: {type(e).__name__}: {e}")
        return {"version": QUEUE_VERSION, "items": []}

    if not isinstance(data, dict) or data.get("version") != QUEUE_VERSION:
        print(f"⚠️  [readd_queue] 队列版本不识别，按空队列处理。")
        return {"version": QUEUE_VERSION, "items": []}
    data.setdefault("items", [])
    return data


def save_queue(q):
    """原子写回队列文件。"""
    try:
        tmp = QUEUE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(q, f, indent=2)
        os.replace(tmp, QUEUE_FILE)
        return True
    except OSError as e:
        print(f"⚠️  [readd_queue] 写队列失败: {e}")
        return False


def enqueue(torrent_bytes, save_path, category, delay_min, note="",
            src_path="", album=""):
    """将一条待回加任务存入延迟队列。

    :param torrent_bytes: .torrent 文件二进制数据。
    :param save_path: 做种目标目录。
    :param category: qB 分类标签。
    :param delay_min: 延迟等待时长（分钟）。
    :param note: 任务备注信息。
    :param src_path: 原始媒体文件路径。
    :param album: 专辑名称。
    :returns: 入队是否成功。
    """
    if delay_min <= 0:
        print("⚠️  [readd_queue] delay_min<=0，不排队（调用方应直接 torrents_add）")
        return False

    b64 = base64.b64encode(torrent_bytes).decode("ascii")

    try:
        import io
        import torf
        infohash = torf.Torrent.read_stream(io.BytesIO(torrent_bytes)).infohash
    except Exception as e:
        print(f"⚠️  [readd_queue] 读 infohash 失败（仍入队，去重降级）: {type(e).__name__}: {e}")
        infohash = ""

    item = {
        "infohash": infohash,
        "due": _now() + delay_min * 60.0,
        "enqueued_at": _now(),
        "save_path": save_path,
        "category": category,
        "note": note,
        "torrent_b64": b64,
        "src_path": src_path,
        "album": album,
        "attempts": 0,
        "next_retry_at": 0.0,
        "first_failed_at": None,
    }

    q = load_queue()
    if infohash:
        for it in q["items"]:
            if it.get("infohash") == infohash:
                print(f"    ⏳ 已在延迟队列中，不重复入队: {note}")
                return True
    q["items"].append(item)
    ok = save_queue(q)
    if ok:
        print(f"    ⏳ 已排入延迟队列：{delay_min} 分钟后回加 qB（{note}；用途: {save_path}；cat: {category}）")
    return ok


def pop_due(now=None):
    """筛选到期的回加任务，返回 (due_items, remaining_items)，不修改磁盘。"""
    if now is None:
        now = _now()
    q = load_queue()
    due, rest = [], []
    for it in q["items"]:
        ready = (it.get("due", 0) <= now
                 and (it.get("next_retry_at") or 0) <= now)
        (due if ready else rest).append(it)
    return due, rest


def ack(done_infohashes, remaining_items):
    """确认已完成任务并写回剩余待办列表。"""
    done = set(done_infohashes)
    keep = [it for it in remaining_items if it.get("infohash") not in done]
    save_queue({"version": QUEUE_VERSION, "items": keep})
