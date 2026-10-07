#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 3040 设置 BT 监听端口（照 3020=56882 / 3060=56883 的口径，3040=56884）。
幂等：存在则改值，不存在则在 [BitTorrent] 段插入。用法：python3 set_3040_port.py <conf> <port>"""
import sys, re

path, port = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    lines = f.read().splitlines()

KEY = r"Session\Port"
out, done, in_bt = [], False, False
for ln in lines:
    if ln.strip() == "[BitTorrent]":
        in_bt = True
        out.append(ln)
        continue
    if ln.startswith("[") and ln.strip() != "[BitTorrent]":
        # 离开 BitTorrent 段前，若还没插，补上
        if in_bt and not done:
            out.append(f"{KEY}={port}")
            done = True
        in_bt = False
        out.append(ln)
        continue
    if ln.split("=", 1)[0] == KEY:
        out.append(f"{KEY}={port}")
        done = True
        continue
    out.append(ln)
if not done and in_bt:
    out.append(f"{KEY}={port}")
    done = True

if not done:
    sys.exit("ERR: 没找到 [BitTorrent] 段，未改动")

with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(out) + "\n")

chk = open(path, encoding="utf-8").read()
print("WROTE ok; Session\\Port 现在是:", re.search(r"^Session\\Port=(.*)$", chk, re.M).group(1))
