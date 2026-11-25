# -*- coding: utf-8 -*-
"""
NAS 音乐池目录属主与权限一键修复工具

背景：
在群晖 NAS 上，容器或历史进程可能以 root:root 生成了部分音乐目录。
非 root 用户 (iSunker) 在执行跨池重命名 (rename/move) 时会触发 PermissionError [Errno 13]。
本工具借助 musichub 容器的 root 权限与卷挂载，安全将属主修复为 1026:100 (iSunker:users)。

红线约束 (AGENTS.md)：
绝对严禁触碰 /volume1/video/download/music/music5！
"""

import sys
import subprocess
import argparse

FORBIDDEN = ("music5",)

TARGET_DIRS = [
    "/volume1/video/download/music/music1",
    "/volume1/video/download/music/music2",
    "/volume1/video/download/music/music3",
    "/volume1/video/download/music/music_pool_1",
    "/volume1/video/download/music/music_pool_2",
    "/volume1/video/download/music/music_pool_3",
    "/volume1/video/download/music/music_pool_4",
    "/volume1/video/download/music/music_pool_5",
]


def run_ssh(cmd: str):
    ssh_cmd = [
        "ssh", "-p", "36688", "iSunker@192.168.0.7",
        cmd
    ]
    p = subprocess.run(ssh_cmd, capture_output=True, text=True, encoding="utf-8")
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def fix_permissions(dry_run: bool = False):
    for d in TARGET_DIRS:
        for f in FORBIDDEN:
            if f in d:
                raise RuntimeError(f"红线阻止！试图操作禁限目录: {d}")

    dirs_str = " ".join(TARGET_DIRS)
    print(f"[*] 准备修复属主为 1026:100 (iSunker:users):")
    for d in TARGET_DIRS:
        print(f"  - {d}")

    if dry_run:
        print("[DRY-RUN] 模拟完成，未执行远端命令。")
        return

    cmd = f"/usr/local/bin/docker exec musichub chown -R 1026:100 {dirs_str}"
    print(f"[*] 正在通过 musichub 容器执行全量修复...")
    rc, out, err = run_ssh(cmd)
    if rc == 0:
        print("[✓] 属主全量修复成功！")
    else:
        print(f"[!] 修复过程中有警告或部分失败 (exit={rc}):\n{err or out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NAS 音乐池权限修复工具")
    parser.add_argument("--dry-run", action="store_true", help="模拟干跑")
    args = parser.parse_args()

    fix_permissions(dry_run=args.dry_run)
