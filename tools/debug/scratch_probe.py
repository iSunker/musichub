# -*- coding: utf-8 -*-
import subprocess

cmd = [
    "ssh", "-p", "36688", "-i", "C:/Users/艾森克/.ssh/id_ed25519",
    "iSunker@192.168.0.7",
    "/usr/local/bin/docker logs --since 16h opencd2musopia"
]

res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
lines = res.stdout.splitlines()

for i, line in enumerate(lines):
    if "阶段 [scan] 超过 3600s" in line:
        print(f"=== Scan timeout at index {i} ===")
        for k in range(max(0, i-20), min(len(lines), i+1)):
            print(lines[k])
        break
