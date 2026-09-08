"""
扫描 qBittorrent 里指定分类的种子，提取元数据（标题/副标题/曲目/日志/封面），
生成 Musopia 用的种子文件，写入数据库等待 publish.py 发布。

改动要点：
- schema 与查询走 db.py，封面/日志/CUE 解析走 assets.py，副标题走 descr.py。
- 记录 content_path，这样 backfill_covers.py 之后不必再去问 qBittorrent。
- 封面查找换成 assets.find_cover()，比原来的单条正则能多捞回不少图。
"""
import argparse
import json
import os
import re

from torf import Torrent

import assets
import config
import db
import descr


def parse_args():
    ap = argparse.ArgumentParser(description="扫描 qB 并生成 Musopia 种子")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少个种子")
    ap.add_argument("--recheck", action="store_true",
                    help="忽略缓存，重新处理所有种子（用于修补历史数据）")
    ap.add_argument("--no-upload-cover", action="store_true",
                    help="不上传封面到图床（仅本地解析，便于快速试跑）")
    return ap.parse_args()


def parse_title(raw_title, folder_path):
    """
    从原始种子名拆出 歌手/专辑/年份/格式/整轨分轨，拼出规整主标题和副标题。
    这套拆分本身就不可能百分之百准，拆不出来时保底用原名，不要产出残缺标题。
    """
    year_match = re.search(r"\(?((?:19|20)\d{2})\)?", raw_title)
    year = year_match.group(1) if year_match else ""

    track_type = "整轨" if assets.has_cue(folder_path) else "分轨"

    upper = raw_title.upper()
    if "FLAC" in upper:
        fmt = "FLAC"
    elif "APE" in upper:
        fmt = "APE"
    elif "WAV" in upper:
        fmt = "WAV"
    else:
        fmt = "FLAC"

    clean = re.sub(r"\(?(?:19|20)\d{2}\)?|\[.*?\]", "", raw_title).strip()
    parts = [p.strip() for p in clean.split("-") if p.strip()]
    if len(parts) >= 2:
        artist, album = parts[0], parts[1]
    elif parts:
        artist, album = parts[0], parts[0]
    else:
        artist, album = "", clean

    title_parts = [p for p in (artist, album) if p]
    core = " - ".join(dict.fromkeys(title_parts)) if title_parts else raw_title
    formatted_title = f"{core} {year} - {fmt} {track_type} - OpenCD"
    formatted_title = re.sub(r"\s+", " ", formatted_title).strip()

    small_descr = descr.build_small_descr(
        artist=artist, album=album, year=year, fmt=fmt,
        track_type=track_type, fallback=raw_title,
    )

    return formatted_title, small_descr, fmt, track_type


def create_musopia_torrent(content_path, save_path):
    announce = f"{config.MUSOPIA_URL}/announce.php?passkey={config.MUSOPIA_PASSKEY}"
    t = Torrent(path=content_path, trackers=[announce], private=True)
    t.generate()
    if os.path.exists(save_path):
        os.remove(save_path)
    t.write(save_path)


def main():
    args = parse_args()

    config.print_runtime_info()
    db.init_db()

    from qbittorrentapi import Client
    qb = Client(host=config.QB_HOST, username=config.QB_USER, password=config.QB_PASS)
    qb.auth_log_in()
    print("✅ 已连通 qBittorrent")

    torrents = qb.torrents_info(category=config.OPENCD_CATEGORY)
    print(f"找到 {len(torrents)} 个分类为 [{config.OPENCD_CATEGORY}] 的种子\n")

    if args.limit:
        torrents = torrents[:args.limit]

    # 一次查出已完成的任务，避免每个种子都开一次数据库连接。
    # 多带出 site_status / cover_url：已发布的任务不需要本地种子文件（站点上有），
    # 也不该重传封面（图床会报 Duplicated upload）。
    conn = db.connect()
    cur = conn.cursor()
    cur.execute("SELECT info_hash, status, torrent_file, site_status, cover_url FROM tasks")
    known = {row[0]: (row[1], row[2], row[3], row[4]) for row in cur.fetchall()}
    conn.close()

    stats = {"skipped": 0, "no_log": 0, "ok": 0, "failed": 0}

    for torrent in torrents:
        info_hash = torrent.hash
        name = torrent.name
        content_path = torrent.content_path

        if not args.recheck and info_hash in known:
            status, tfile, site_status, known_cover = known[info_hash]
            # 已发布到站点的任务：站点上已有种子和页面，本地 .torrent 文件
            # 有没有都无所谓，直接跳过。原先的跳过条件要求本地文件存在，
            # 于是每轮 scan 都把已发布的任务整个重做一遍——重传封面触发
            # 图床 Duplicated upload 400，空值再写回库，把已有 cover_url
            # 覆盖成空，站点页面的封面随后也被 edit 阶段抹掉。
            if status == "SUCCESS" and site_status == "EXISTS":
                stats["skipped"] += 1
                continue
            if status == "SUCCESS" and tfile and os.path.exists(tfile):
                stats["skipped"] += 1
                continue
            if status == "SUCCESS":
                print(f"⚠️  库里标记已完成但种子文件不在了，重做: {name}")
                if known_cover:
                    print(f"   （已有封面 URL，重做时复用，不再重传图床）")

        print("-" * 60)
        print(f"扫描: {name}")

        log_content, log_filename = assets.read_log(content_path)
        if not log_filename:
            print("❌ 跳过：目录里没有 .log 抓轨日志")
            db.save_task(info_hash=info_hash, torrent_name=name, status="NO_LOG_FILE",
                         reason="目录里没有 .log 抓轨日志", content_path=content_path)
            stats["no_log"] += 1
            continue

        formatted_title, small_descr, fmt, track_type = parse_title(name, content_path)
        tracklist = assets.read_tracklist(content_path)

        print(f"✔ 主标题: {formatted_title}")
        print(f"✔ 副标题: {small_descr}")
        print(f"📄 日志: {log_filename} ({len(log_content)} 字符)")
        print(f"🎵 曲目: {len(tracklist.splitlines())} 行")

        # 封面：库里已有 URL 就直接复用（重做场景），绝不重复上传。
        # 同一张图传第二遍图床会报 Duplicated upload 400，历史上这个 400
        # 让 cover_url 被写成空串，再连锁把站点页面上的封面抹掉。
        cover_url = known[info_hash][3] if info_hash in known else ""
        if not cover_url:
            cover_path = assets.find_cover(content_path)
            if cover_path:
                if args.no_upload_cover:
                    print(f"📷 找到封面（未上传）: {os.path.basename(cover_path)}")
                else:
                    cover_url = assets.upload_to_picgo(cover_path)
                    if cover_url:
                        print(f"📷 图床上传成功: {cover_url}")
            else:
                print("📷 未找到封面图")
        else:
            print(f"📷 复用库内已有封面: {cover_url}")

        safe_name = re.sub(r'[\\/*?:"<>|]', "_", formatted_title)
        torrent_file = os.path.join(config.OUTPUT_TORRENT_DIR, f"{safe_name}.torrent")

        print("🔨 生成种子中...")
        try:
            create_musopia_torrent(content_path, torrent_file)
            print(f"✅ 种子已生成: {torrent_file}")
        except Exception as e:
            print(f"❌ 种子生成失败: {e}")
            db.save_task(info_hash=info_hash, torrent_name=name, status="FAILED",
                         reason=str(e), content_path=content_path)
            stats["failed"] += 1
            continue

        db.save_task(
            info_hash=info_hash,
            torrent_name=name,
            status="SUCCESS",
            reason="",
            formatted_title=formatted_title,
            cover_url=cover_url,
            torrent_file=torrent_file,
            small_descr=small_descr,
            tracklist=tracklist,
            log_content=log_content,
            log_filename=log_filename,
            content_path=content_path,
        )
        stats["ok"] += 1

        # 配套 JSON，供油猴脚本或手动发布时一键取用
        meta = {
            "info_hash": info_hash,
            "torrent_name": name,
            "formatted_title": formatted_title,
            "small_descr": small_descr,
            "cover_url": cover_url,
            "tracklist": tracklist,
            "log_filename": log_filename,
            "log_content": log_content,
            "torrent_file": os.path.basename(torrent_file),
            "content_path": content_path,
        }
        try:
            with open(os.path.join(config.OUTPUT_TORRENT_DIR, f"{safe_name}.json"),
                      "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️  JSON 写入失败: {e}")

    print("\n" + "=" * 60)
    print(f"🎯 扫描完成：新处理 {stats['ok']} 个 | 缓存跳过 {stats['skipped']} 个 | "
          f"无日志 {stats['no_log']} 个 | 失败 {stats['failed']} 个")
    st = db.stats()
    print(f"📊 库内共 {st['total']} 行，待发布可用 python publish.py 查看")


if __name__ == "__main__":
    main()
