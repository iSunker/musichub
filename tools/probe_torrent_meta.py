# -*- coding: utf-8 -*-
"""种子元数据只读探针 (probe_torrent_meta.py)

用途
----
排查「某个做种中的种子到底来自哪个站、有没有内嵌详情页 TID」时，
`.torrent` 顶层的 `announce` / `comment` / `source` 是唯一离线证据。
注意：`comment` 常常是**跨站辅种时留下的原制种者备注**，不代表真正的源站，
必须与 `announce`（真正的 tracker）区分开（见专题 17 的 AK79 误判教训）。

不做任何写操作，纯零依赖（自带极简 bencode 解析，不依赖 torf）。

用法
----
    python musichub/tools/probe_torrent_meta.py --file /path/to/x.torrent
    python musichub/tools/probe_torrent_meta.py --hash <info_hash> --port 3260
"""

import os
import re
import sys
import argparse
import hashlib
from typing import Any, Dict, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from musichub.core.config import MusicHubConfig
from musichub.core.qb_client import QBClient

INTEREST = (b"announce", b"announce-list", b"comment", b"source", b"publisher",
            b"publisher-url", b"url-list", b"title", b"created by")


def redact(text: str) -> str:
    """凭据不外泄：输出前把 announce 里的 passkey/token 打码（AGENTS.md 第 2 节第 6 条）"""
    text = re.sub(r"(passkey=)[^&\s'\"]+", r"\1***", text or "")
    return re.sub(r"((?:token|authkey|auth)=)[^&\s'\"]+", r"\1***", text)


def bdecode(data: bytes, i: int = 0) -> Tuple[Any, int]:
    t = data[i:i + 1]
    if t == b"d":
        i += 1
        d: Dict[bytes, Any] = {}
        while data[i:i + 1] != b"e":
            k, i = bdecode(data, i)
            v, i = bdecode(data, i)
            d[k] = v
        return d, i + 1
    if t == b"l":
        i += 1
        l: list = []
        while data[i:i + 1] != b"e":
            v, i = bdecode(data, i)
            l.append(v)
        return l, i + 1
    if t == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    j = data.index(b":", i)
    n = int(data[i:j])
    return data[j + 1:j + 1 + n], j + 1 + n


def infohash_of(data: bytes) -> str:
    """infohash = info 字典原文的 SHA1；直接按偏移切片，避免重编码引入差异"""
    d, _ = bdecode(data)
    start = data.index(b"4:info") + len(b"4:info")
    _, end = bdecode(data, start)
    return hashlib.sha1(data[start:end]).hexdigest()


def load_bytes(args) -> Tuple[bytes, str]:
    if args.file:
        with open(args.file, "rb") as f:
            return f.read(), f"file:{args.file}"
    if not args.hash:
        raise SystemExit("需要 --file 或 --hash")
    port = args.port or 3260
    client = QBClient(host=MusicHubConfig.from_env().publish_qb.host, port=port)
    client.login()  # qB 5.x 成功返回 204，login() 会假阴性，忽略返回值
    data = client.export_torrent(args.hash)
    if data:
        return data, f"qb:{port}"
    for base in ("/app/musichub/data/torrents", "/app/musichub/data/torrents_backup"):
        p = os.path.join(base, f"{args.hash.lower()}.torrent")
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read(), f"archive:{p}"
    raise SystemExit(f"拿不到 {args.hash} 的种子字节（qB export 与本地归档都没有）")


def main() -> int:
    ap = argparse.ArgumentParser(description="种子顶层元数据只读探针")
    ap.add_argument("--file", default="", help="本地 .torrent 路径")
    ap.add_argument("--hash", default="", help="从 qB/归档按 info-hash 取种子")
    ap.add_argument("--port", type=int, default=0, help="配合 --hash 指定 qB 实例端口")
    args = ap.parse_args()

    data, src = load_bytes(args)
    d, _ = bdecode(data)

    print(f"来源: {src}")
    print(f"info_hash: {infohash_of(data)}")
    info = d.get(b"info") or {}
    print(f"info.name: {(info.get(b'name') or b'').decode('utf-8', 'replace')}")
    print("顶层键:", [k.decode("latin-1") for k in d.keys()])
    print("---")
    for k in INTEREST:
        if k not in d:
            continue
        v = d[k]
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        print(f"{k.decode()}: {redact(str(v))}")

    # 从所有顶层字符串里抽取可能的详情页 TID，便于判断该去哪站补源
    blob = " ".join(
        (v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v))
        for v in d.values() if isinstance(v, (bytes, str))
    )
    tids = re.findall(r"details\.php\?id=(\d+)|music_details\.php\?torrentid=(\d+)", blob)
    if tids:
        flat = sorted({a or b for a, b in tids})
        print(f"内嵌 TID 候选: {flat}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
