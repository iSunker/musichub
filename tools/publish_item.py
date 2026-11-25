# -*- coding: utf-8 -*-
"""MusicHub 矩阵单专发布与候选提交运维工具 (publish_item.py)

功能定位
--------
用于单专辑对指定站点的精准测试、补发、或候选提交（如 SSD 候选模式）。
支持通过 --hash、--name 或 --path 定位音乐资产，完整打通：
1. 本地音频与元数据扫描（AudioInspector / source_tasks / site_detail_pages）
2. 站点前置校验（pre_publish_check，如麒麟纯拉丁标题、南洋自动拼接等）
3. 站内查重与图谱防重（check_exists / site_publish_graph）
4. SSD 自动候选模式探测与内嵌附件图床转存
5. 成功后防环账本写入（published_music_ids.json）与 3010 延迟回加做种队列入队

用法示例
--------
    # 1. 干跑排版与查重验证（不真正发种）
    python musichub/tools/publish_item.py --site nanyangpt --hash 095848387be9eb5578da6baf85ff7ffc85028ff3 --dry-run
    python musichub/tools/publish_item.py --site springsunday --name "五月蠅" --dry-run

    # 2. 正式发布 / 提交候选
    python musichub/tools/publish_item.py --site nanyangpt --name "Darkness at Noon"
    python musichub/tools/publish_item.py --site springsunday --hash <info_hash>

    # 3. 指定物理路径发布
    python musichub/tools/publish_item.py --site nanyangpt --path "/volume1/video/download/music/music3/Artist - Album"
"""

import os
import sys
import json
import sqlite3
import argparse
import logging
from typing import Optional, Dict, Any, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from musichub.core.models import CanonicalMusicItem, AudioTrackInfo
from musichub.metadata.audio_inspector import AudioInspector
from musichub.routing.graph import RoutingGraphManager, digit_tid
from musichub.publisher.gateway import DeduplicationGateway, evaluate_source_policy
from musichub.publisher.engine import PublisherEngine
from musichub.publisher.delayed_queue import DelayedReaddQueue
from musichub.core.qb_client import QBClient
from musichub.core.config import MusicHubConfig
from musichub.adapters.sites import (
    MusopiaAdapter, CrabPTAdapter, PTerClubAdapter, XingwanAdapter,
    HDKylinAdapter, TangPTAdapter, MuXueGeAdapter, OpenCDAdapter,
    UBitsAdapter, SoulVoiceAdapter, NanyangPTAdapter, SpringSundayAdapter,
    LemonHDAdapter, HDTimeAdapter, UHDDolbyAdapter
)
from musichub.adapters.nexusphp_base import is_duplicate_name_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("PublishItem")

ADAPTERS_MAP = {
    "nanyangpt": NanyangPTAdapter,
    "nypt": NanyangPTAdapter,
    "springsunday": SpringSundayAdapter,
    "ssd": SpringSundayAdapter,
    "soulvoice": SoulVoiceAdapter,
    "ubits": UBitsAdapter,
    "lemonhd": LemonHDAdapter,
    "hdtime": HDTimeAdapter,
    "muxuege": MuXueGeAdapter,
    "tangpt": TangPTAdapter,
    "hdkylin": HDKylinAdapter,
    "xingwan": XingwanAdapter,
    "pterclub": PTerClubAdapter,
    "musopia": MusopiaAdapter,
    "crabpt": CrabPTAdapter,
    "opencd": OpenCDAdapter,
    "uhddolby": UHDDolbyAdapter,
}


def get_default_db_path() -> str:
    candidates = [
        "/app/musichub/data/musichub.db",
        "d:/Projects/PT-Project/musichub/data/musichub.db",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "musichub.db"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]


def find_item_from_db(
    db_path: str,
    info_hash: Optional[str] = None,
    name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not os.path.exists(db_path):
        return None
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    row = None
    if info_hash:
        row = cur.execute(
            "SELECT * FROM source_tasks WHERE LOWER(info_hash) = ? ORDER BY rowid DESC LIMIT 1",
            (info_hash.strip().lower(),),
        ).fetchone()
    elif name:
        pattern = f"%{name.strip()}%"
        row = cur.execute(
            "SELECT * FROM source_tasks WHERE source_name LIKE ? ORDER BY rowid DESC LIMIT 1",
            (pattern,),
        ).fetchone()
    con.close()
    return dict(row) if row else None


def resolve_local_path(db_path: str, info_hash: str, pool_dirs: List[str]) -> Optional[str]:
    # 1. 尝试从 DB site_publish_graph 取
    if os.path.exists(db_path):
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = con.cursor()
        r = cur.execute("SELECT local_path FROM site_publish_graph WHERE info_hash = ?", (info_hash,)).fetchone()
        con.close()
        if r and r[0] and os.path.exists(r[0]):
            return r[0]

    # 2. 尝试从 qB 3010/3210~3280 探测
    config = MusicHubConfig.from_env()
    for port in [3010, 3210, 3220, 3230, 3240, 3250, 3260, 3270, 3280]:
        try:
            qb = QBClient(config.publish_qb.host, port)
            torrents = qb.torrents_info(hashes=info_hash)
            if torrents:
                t = torrents[0]
                save_path = t.get("save_path", "")
                t_name = t.get("name", "")
                full = os.path.join(save_path, t_name)
                if os.path.exists(full):
                    return full
                # 兼容容器内 /downloads 映射
                for p in pool_dirs:
                    cand = os.path.join(p, t_name)
                    if os.path.exists(cand):
                        return cand
        except Exception:
            continue
    return None


def sync_vertex_ids(tid: int) -> None:
    """发种成功后，写入 Vertex 防环账本"""
    vpaths = [
        "/volume2/docker_ssd/vertex2/data/vertex/data/published_music_ids.json",
        "/volume2/docker_ssd/vertex/data/published_music_ids.json",
        "/vertex/data/published_music_ids.json",
    ]
    for p in vpaths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                s = set(data) if isinstance(data, list) else set()
                if tid not in s:
                    s.add(tid)
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(sorted(list(s)), f, indent=2, ensure_ascii=False)
                    logger.info("已同步写入 Vertex 防环账本 %s (TID: %s)", p, tid)
            except Exception as e:
                logger.warning("写入 Vertex 防环账本 %s 失败: %s", p, e)


def main() -> int:
    parser = argparse.ArgumentParser(description="MusicHub 单专发种/候选提交统一运维工具")
    parser.add_argument("--site", required=True, help="目标站点名称 (如 nanyangpt, springsunday, muxuege 等)")
    parser.add_argument("--hash", default="", help="指定源种 info_hash")
    parser.add_argument("--name", default="", help="按名称模糊匹配源种")
    parser.add_argument("--path", default="", help="直接指定本地音乐目录路径")
    parser.add_argument("--db-path", default="", help="数据库路径")
    parser.add_argument("--dry-run", action="store_true", help="干跑演练（不实际发种）")
    parser.add_argument("--force", action="store_true", help="忽略查重报警强推")
    parser.add_argument("--skip-readd", action="store_true", help="成功后不入延迟回加做种队列")

    args = parser.parse_args()
    site_key = args.site.strip().lower()
    if site_key in ("springsunday", "ssd"):
        logger.error("站点侧不允许非官种音乐组发布音乐种，SSD 已从发布矩阵中彻底下线。")
        return 1
    if site_key not in ADAPTERS_MAP:
        logger.error("不支持的站点: %s。支持的站点列表: %s", site_key, list(ADAPTERS_MAP.keys()))
        return 1

    adapter = ADAPTERS_MAP[site_key]()
    db_path = args.db_path or get_default_db_path()

    pools = [
        "/volume1/video/download/music/music3",
        "/volume1/video/download/music/music1",
        "/volume1/video/download/music/music2",
        "/volume1/video/download/music/music_pool_1",
        "/volume1/video/download/music/music_pool_2",
        "/volume1/video/download/music/music_pool_3",
        "/volume1/video/download/music/music_pool_4",
        "/volume1/video/download/music/music_pool_5",
        "/volume1/video/download/music/music_pool_6",
        "/volume1/video/download/music/music_pool_7",
        "/volume1/video/download/music/music_pool_8",
    ]

    target_path = args.path
    st = None
    ih = args.hash.strip().lower() if args.hash else ""

    if not target_path:
        st = find_item_from_db(db_path, info_hash=ih, name=args.name)
        if not st:
            logger.error("在 DB 中未找到匹配的源种任务 (--hash=%s, --name=%s)", ih, args.name)
            return 1
        ih = st.get("info_hash", "").lower()
        target_path = resolve_local_path(db_path, ih, pools)
        if not target_path:
            # 尝试在各 pool 查找同名目录
            for p in pools:
                cand = os.path.join(p, st.get("source_name", ""))
                if os.path.exists(cand):
                    target_path = cand
                    break

    if not target_path or not os.path.exists(target_path):
        logger.error("无法定位物理数据路径: %s", target_path)
        return 1

    logger.info("目标物理路径: %s", target_path)
    inspector = AudioInspector()
    tracks = inspector.scan_album_directory(target_path)
    if not tracks:
        logger.error("未扫描到有效音频文件: %s", target_path)
        return 1

    title = os.path.basename(target_path)
    artist = tracks[0].artist or "Various Artists"
    album = tracks[0].album or title
    year = tracks[0].year or "2020"
    fmt = tracks[0].format or "FLAC"

    # 若有 source_task 记录，从记录补齐更多元数据
    desc = ""
    cover_url = ""
    tags = []
    if st:
        title = st.get("source_name") or title
        desc = st.get("description") or ""
        cover_url = st.get("cover_url") or ""
        tags_raw = st.get("tags_json")
        if tags_raw:
            try:
                tags = json.loads(tags_raw)
            except Exception:
                pass

    item = CanonicalMusicItem(
        title=title,
        album=album,
        artist=artist,
        year=year,
        format=fmt,
        medium="CD",
        standard="Lossless",
        origin_site=st.get("source_site", "local") if st else "local",
        origin_torrent_id=st.get("source_tid", "") if st else "",
        local_path=target_path,
        tracks=tracks,
        description=desc,
        cover_url=cover_url,
        tags=tags,
    )

    # 1. 适配器前置检查
    chk_ok, chk_msg = adapter.pre_publish_check(item)
    if not chk_ok and not args.force:
        logger.error("站点前置校验不通过: %s", chk_msg)
        return 1
    logger.info("站点前置校验通过: %s", chk_msg)

    # 2. 查重
    exists, ex_info = adapter.check_exists(item)
    if exists:
        logger.warning("站内已存在相同种子: %s", ex_info)
        if not args.force:
            logger.error("中止发布（可加 --force 强制推）")
            return 1

    # 3. 如果是 SSD，探测是否为候选模式
    is_offer = False
    if hasattr(adapter, "needs_offer_mode") and callable(adapter.needs_offer_mode):
        is_offer = adapter.needs_offer_mode()
        logger.info("SSD 模式探测: %s", "【候选区提交 (Offer Mode)】" if is_offer else "【正式发布 (Normal Upload)】")

    # 4. 干跑演练
    if args.dry_run:
        logger.info("=== DRY-RUN 干跑演练 ===")
        print(f"站点: {adapter.site_name} ({adapter.base_url})")
        print(f"模式: {'候选区 (Offer)' if is_offer else '正式发种 (Publish)'}")
        print(f"主标题: {title}")
        print(f"专辑/艺术家: {album} / {artist} ({year})")
        print(f"音轨数: {len(tracks)} 首")
        print(f"封面图: {cover_url or '无'}")
        print(f"描述长度: {len(desc)} 字符")
        print(f"前置检查: {chk_msg}")
        return 0

    # 5. 执行真正发布
    config = MusicHubConfig.from_env()
    publish_qb = QBClient(config.publish_qb.host, 3010)
    graph_mgr = RoutingGraphManager(db_path=db_path)
    engine = PublisherEngine(publish_qb, graph_mgr)

    logger.info("开始向 %s 提交发布/候选...", adapter.site_name)
    success, res_info = adapter.publish(item)
    if not success:
        logger.error("发布失败: %s", res_info)
        return 1

    tid = digit_tid(res_info)
    logger.info("发布成功! 返回信息/TID: %s (数字 TID: %s)", res_info, tid)

    # 6. 同步防环账本与图谱
    if tid:
        sync_vertex_ids(tid)
        col_name = f"{adapter.site_name.lower()}_tid"
        try:
            graph_mgr.record_published(ih, adapter.site_name, tid)
        except Exception as e:
            logger.warning("更新图谱状态异常: %s", e)

    # 7. 延迟回加 3010 做种队列
    if not args.skip_readd and tid:
        logger.info("将种子加入 450s 延迟回加做种队列...")
        delayed_queue = DelayedReaddQueue(db_path, engine)
        delayed_queue.enqueue(
            item=item,
            site_name=adapter.site_name,
            torrent_id=str(tid),
            delay_seconds=450,
        )
        logger.info("已入队延迟回加做种队列。")

    logger.info("全流程执行完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
