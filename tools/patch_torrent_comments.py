# -*- coding: utf-8 -*-
"""MusicHub 详情页 Comment 注释安全补丁工具 (PatchTorrentComments)

职责：
1. 为本地归档的 .torrent 文件补充站点详情页链接（如 https://hdtime.org/details.php?id=xxx）
2. 零修改 info 字典，InfoHash 恒定不变，做种分块校验 100% 保持一致
3. 支持指定站点/TID 补丁，或批量扫描补全

使用示例：
  # 1. 扫描并展示可补齐的本地种子（只读预览）
  python -m musichub.tools.patch_torrent_comments --site hdtime --scan

  # 2. 为指定种子补充详情页注释
  python -m musichub.tools.patch_torrent_comments --site hdtime --tid 238818 --apply
"""

import os
import re
import sys
import argparse
import logging
from typing import Dict, Any, List

from musichub.core.torrent_tools import bdecode, bencode, calculate_info_hash, inject_torrent_comment
from musichub.adapters.descr_core import SOURCE_SITES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("PatchTorrentComments")

# 站点域名映射
SITE_URL_MAP = {
    "hdtime": "https://hdtime.org",
    "uhddolby": "https://www.uhddolby.com",
    "ubits": "https://ubits.club",
    "lemonhd": "https://lemonhd.net",
    "soulvoice": "https://pt.soulvoice.club",
    "nanyangpt": "https://nanyangpt.com",
    "springsunday": "https://springsunday.net",
    "tangpt": "https://tangpt.top",
    "muxuege": "https://pt.muxuege.org",
    "crabpt": "https://crabpt.vip",
    "musopia": "https://www.musopia.vip",
    "pterclub": "https://pterclub.net",
    "xingwan": "https://xingwan.cc",
    "hdkylin": "https://www.hdkylin.top",
}


def get_details_url(site: str, tid: str) -> str:
    site_key = site.lower().strip()
    base = SITE_URL_MAP.get(site_key)
    if not base and site_key in SOURCE_SITES:
        base = SOURCE_SITES[site_key][1]
    base = base or f"https://{site_key}.org"
    return f"{base}/details.php?id={tid}"


def patch_torrent_file(torrent_path: str, details_url: str, apply: bool = False) -> bool:
    if not os.path.exists(torrent_path):
        logger.warning(f"文件不存在: {torrent_path}")
        return False

    with open(torrent_path, "rb") as f:
        data = f.read()

    try:
        d, _ = bdecode(data)
        current_comment = d.get(b"comment", b"").decode("utf-8", errors="ignore")
        if current_comment and current_comment.startswith("http"):
            logger.info(f"[SKIP] 已存在合法注释: {current_comment} ({torrent_path})")
            return True

        orig_hash = calculate_info_hash(data)
        new_data = inject_torrent_comment(data, details_url, force=True)
        new_hash = calculate_info_hash(new_data)
        assert orig_hash == new_hash, f"InfoHash 发生漂移: {orig_hash} != {new_hash}"

        if apply:
            with open(torrent_path, "wb") as f:
                f.write(new_data)
            logger.info(f"[APPLIED] 已注入注释 {details_url} -> {torrent_path}")
        else:
            logger.info(f"[DRY-RUN] 可注入注释 {details_url} -> {torrent_path}")
        return True
    except Exception as e:
        logger.error(f"处理种子失败 {torrent_path}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="为种子元数据补充详情页链接注释")
    parser.add_argument("--site", type=str, help="目标站点（如 hdtime, ubits）")
    parser.add_argument("--tid", type=str, help="种子数字 TID")
    parser.add_argument("--torrents-dir", type=str, default="/app/musichub/data/torrents", help="本地种子归档目录")
    parser.add_argument("--scan", action="store_true", help="扫描归档目录中缺失 comment 的种子")
    parser.add_argument("--apply", action="store_true", help="显式执行写入")
    args = parser.parse_args()

    torrents_dir = args.torrents_dir
    if not os.path.exists(torrents_dir):
        # 兼容本机路径
        local_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "torrents")
        if os.path.exists(local_dir):
            torrents_dir = local_dir

    if args.site and args.tid:
        details_url = get_details_url(args.site, args.tid)
        t_file = os.path.join(torrents_dir, f"{args.site.lower()}_{args.tid}.torrent")
        patch_torrent_file(t_file, details_url, apply=args.apply)
        return

    if args.scan:
        logger.info(f"开始扫描归档目录: {torrents_dir}")
        if not os.path.exists(torrents_dir):
            logger.error(f"目录不存在: {torrents_dir}")
            return
        patched = 0
        total = 0
        for fname in sorted(os.listdir(torrents_dir)):
            if not fname.endswith(".torrent"):
                continue
            m = re.match(r"^([a-z0-9]+)_(\d+)\.torrent$", fname, re.I)
            if not m:
                continue
            site, tid = m.group(1).lower(), m.group(2)
            if args.site and site != args.site.lower():
                continue
            total += 1
            fpath = os.path.join(torrents_dir, fname)
            url = get_details_url(site, tid)
            ok = patch_torrent_file(fpath, url, apply=args.apply)
            if ok:
                patched += 1
        logger.info(f"扫描完毕: 发现 {total} 个种子，处理 {patched} 个 (apply={args.apply})")


if __name__ == "__main__":
    main()
