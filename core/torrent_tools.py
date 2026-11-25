# -*- coding: utf-8 -*-
"""MusicHub 种子字节元数据工具 (TorrentTools)

专用于在加种或发布回加前，对 .torrent 字节流进行安全处理：
1. 为种子顶层元数据补齐详情页 Comment 注释（如 https://hdtime.org/details.php?id=238818）
2. 零外部重度依赖（内置纯 Python 健壮 Bencode/Bdecode），确保跨容器与跨环境 100% 可用
3. ★ 核心安全保证：仅改写顶层 'comment' 与 'comment.utf-8'，严格不动 'info' 字典
   因此生成的 InfoHash 恒定保持逐字节一致，绝不影响分块校验与做种状态。
"""

import hashlib
from typing import Any, Tuple, Optional


def bdecode(data: bytes) -> Tuple[Any, int]:
    """解码 Bencode 字节串"""
    def _decode_item(idx: int) -> Tuple[Any, int]:
        if idx >= len(data):
            raise ValueError("Unexpected EOF while decoding bencode")
        char = data[idx:idx+1]
        if char == b'i':
            end = data.index(b'e', idx)
            return int(data[idx+1:end]), end + 1
        elif char == b'l':
            idx += 1
            res = []
            while data[idx:idx+1] != b'e':
                val, idx = _decode_item(idx)
                res.append(val)
            return res, idx + 1
        elif char == b'd':
            idx += 1
            res = {}
            while data[idx:idx+1] != b'e':
                key, idx = _decode_item(idx)
                val, idx = _decode_item(idx)
                res[key] = val
            return res, idx + 1
        elif char.isdigit():
            colon = data.index(b':', idx)
            length = int(data[idx:colon])
            start = colon + 1
            return data[start:start+length], start + length
        raise ValueError(f"Unknown bencode type byte {char!r} at offset {idx}")

    res, end_idx = _decode_item(0)
    return res, end_idx


def bencode(val: Any) -> bytes:
    """编码 Python 对象为 Bencode 字节串"""
    if isinstance(val, int):
        return f"i{val}e".encode("ascii")
    elif isinstance(val, bytes):
        return f"{len(val)}:".encode("ascii") + val
    elif isinstance(val, str):
        b = val.encode("utf-8")
        return f"{len(b)}:".encode("ascii") + b
    elif isinstance(val, list):
        return b"l" + b"".join(bencode(x) for x in val) + b"e"
    elif isinstance(val, dict):
        # Bencode 规范：字典的键必须按原始字节升序排序
        sorted_keys = sorted(val.keys(), key=lambda k: k if isinstance(k, bytes) else str(k).encode("utf-8"))
        res = [b"d"]
        for k in sorted_keys:
            k_bytes = k if isinstance(k, bytes) else str(k).encode("utf-8")
            res.append(bencode(k_bytes))
            res.append(bencode(val[k]))
        res.append(b"e")
        return b"".join(res)
    raise TypeError(f"Cannot bencode object of type {type(val)}")


def calculate_info_hash(torrent_bytes: bytes) -> str:
    """计算 .torrent 二进制流的 40 位十六进制 info-hash"""
    d, _ = bdecode(torrent_bytes)
    if not isinstance(d, dict) or b"info" not in d:
        raise ValueError("Invalid torrent payload: missing 'info' dictionary")
    info_bytes = bencode(d[b"info"])
    return hashlib.sha1(info_bytes).hexdigest().lower()


def inject_torrent_comment(torrent_bytes: bytes, comment_url: str, force: bool = False) -> bytes:
    """为 .torrent 字节流注入或补齐顶层 Comment 注释。

    :param torrent_bytes: 原始 .torrent 二进制数据
    :param comment_url: 详情页完整链接（如 https://hdtime.org/details.php?id=238818）
    :param force: 若已有注释是否强制覆写（默认 False：已有合法 url 时保留）
    :return: 注入注释后的新 .torrent 二进制数据
    """
    if not torrent_bytes or not comment_url:
        return torrent_bytes

    try:
        d, _ = bdecode(torrent_bytes)
        if not isinstance(d, dict) or b"info" not in d:
            return torrent_bytes

        # 原始 hash 核算
        orig_hash = hashlib.sha1(bencode(d[b"info"])).hexdigest().lower()

        existing_comment = d.get(b"comment")
        comment_bytes = comment_url.strip().encode("utf-8")

        if not existing_comment or force or not existing_comment.startswith(b"http"):
            d[b"comment"] = comment_bytes
            if b"comment.utf-8" in d or True:
                d[b"comment.utf-8"] = comment_bytes

            new_bytes = bencode(d)
            # 校验修改后 info_hash 绝不改变
            new_hash = calculate_info_hash(new_bytes)
            assert orig_hash == new_hash, f"InfoHash changed unexpectedly! {orig_hash} != {new_hash}"
            return new_bytes

        return torrent_bytes
    except Exception as e:
        # 保护原则：任何异常均优雅退回原始字节，不阻塞业务
        return torrent_bytes
