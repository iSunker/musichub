# -*- coding: utf-8 -*-
"""MusicHub 本机至群晖 NAS 代码与资源安全同步工具。

规避 NAS SSH 缺少 SFTP 子系统、PowerShell 引号剥离及命令行长度限制问题，
通过 SSH stdin 二进制字节流管道将文件准确无误传输至 NAS 生产端。

用法示例：
  # 同步核心治理代码与工具
  python musichub/tools/sync_to_nas.py

  # 同步指定文件
  python musichub/tools/sync_to_nas.py --files musichub/routing/graph.py
"""

import argparse
import os
import subprocess
import sys
from typing import List

DEFAULT_FILES = [
    "musichub/adapters/tag_matcher.py",
    "musichub/adapters/sites.py",
    "musichub/collector/source_detail.py",
    "musichub/onsite/detail_archive.py",
    "musichub/routing/graph.py",
    "musichub/daemon.py",
    "musichub/tools/fetch_detail_page.py",
    "musichub/tools/query_matrix_tags.py",
    "musichub/tests/test_detail_patrol.py",
    "musichub/tests/test_tag_matcher.py",
]

NAS_SSH_USER = "iSunker"
NAS_SSH_HOST = "192.168.0.7"
NAS_SSH_PORT = "36688"
NAS_ROOT_DIR = "/volume2/docker_ssd/musichub/src/musichub"


def sync_file(local_path: str, remote_path: str) -> bool:
    if not os.path.exists(local_path):
        print(f"[Skip] 本地文件不存在: {local_path}")
        return False

    with open(local_path, "rb") as fh:
        data = fh.read()

    cmd = ["ssh", "-p", NAS_SSH_PORT, f"{NAS_SSH_USER}@{NAS_SSH_HOST}", f"cat > {remote_path}"]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = p.communicate(input=data)

    if p.returncode == 0:
        print(f"[OK] {os.path.basename(local_path)} -> {remote_path} ({len(data)} bytes)")
        return True
    else:
        err = stderr.decode("utf-8", errors="replace")
        print(f"[Fail] {local_path} 同步失败: {err}")
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files", nargs="*", help="指定要同步的文件相对路径列表")
    args = parser.parse_args()

    target_files = args.files or DEFAULT_FILES
    workspace_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    print(f"[*] 开始同步 {len(target_files)} 个文件到 NAS ({NAS_SSH_HOST}:{NAS_SSH_PORT})...\n")
    success_cnt = 0
    for rel_path in target_files:
        local_path = os.path.join(workspace_root, rel_path)
        sub_path = rel_path.replace("musichub/", "", 1) if rel_path.startswith("musichub/") else rel_path
        remote_path = f"{NAS_ROOT_DIR}/{sub_path.replace(os.sep, '/')}"
        if sync_file(local_path, remote_path):
            success_cnt += 1

    print(f"\n[*] 同步完成: {success_cnt}/{len(target_files)} 个文件成功。")


if __name__ == "__main__":
    main()
