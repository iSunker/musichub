"""
补齐历史任务缺失的封面：回原目录找图 -> 上传图床 -> 写回 cover_url。

库里有 49 条已发布记录没有 cover_url。原因是 run.py 的封面匹配正则只认
`(cover|folder|front|artwork|poster).*\\.(jpg|jpeg|png|webp)$`，像 `00.jpg`、
`Scans/IMG_001.jpg`、`封面.jpg` 这种全都漏掉了。assets.find_cover() 用分级匹配
加"退而取最大的图"兜底，能捞回大部分。

原目录从两个来源获得：数据库里的 content_path（新任务会记录），或实时问 qBittorrent
（历史任务没有 content_path，得按 info_hash 查）。

用法:
    python backfill_covers.py                 # 只报告哪些能找到图
    python backfill_covers.py --apply         # 上传图床并写回数据库
    python backfill_covers.py --apply --limit 5
"""
import argparse
import os
import sys

import assets
import config
import db


def resolve_paths(tasks):
    """给每条任务确定本地内容目录。优先用库里的 content_path，否则问 qB。"""
    need_qb = [t for t in tasks if not (t.get("content_path")
                                        and os.path.exists(t["content_path"]))]
    if not need_qb:
        return

    try:
        from qbittorrentapi import Client
        qb = Client(host=config.QB_HOST, username=config.QB_USER, password=config.QB_PASS)
        qb.auth_log_in()
    except Exception as e:
        print(f"⚠️  连不上 qBittorrent ({e})，只能依赖库里已有的 content_path")
        return

    by_hash = {}
    try:
        for t in qb.torrents_info():
            by_hash[t.hash.lower()] = t.content_path
    except Exception as e:
        print(f"⚠️  拉取 qB 种子列表失败: {e}")
        return

    hit = 0
    for t in need_qb:
        p = by_hash.get((t.get("info_hash") or "").lower())
        if p:
            t["content_path"] = p
            hit += 1
    print(f"🔗 从 qBittorrent 解析出 {hit}/{len(need_qb)} 条任务的本地路径")


def main():
    ap = argparse.ArgumentParser(description="补齐缺失的封面图")
    ap.add_argument("--apply", action="store_true", help="实际上传图床并写库")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条")
    ap.add_argument("--all", action="store_true",
                    help="包括未发布的任务（默认只处理 site_status=EXISTS）")
    args = ap.parse_args()

    config.print_runtime_info()
    db.init_db()

    tasks = db.get_missing_cover(published_only=not args.all)
    if not tasks:
        print("💡 没有缺封面的任务。")
        return

    print(f"📋 缺封面任务 {len(tasks)} 条")
    if args.limit:
        tasks = tasks[:args.limit]

    resolve_paths(tasks)

    found, no_path, no_image = [], [], []
    for t in tasks:
        path = t.get("content_path")
        if not path or not os.path.exists(path):
            no_path.append(t)
            continue
        cover = assets.find_cover(path)
        if cover:
            found.append((t, cover))
        else:
            no_image.append(t)

    print("\n" + "=" * 60)
    print(f"✅ 找到可用封面 : {len(found)}")
    print(f"❌ 目录不可访问 : {len(no_path)}")
    print(f"❌ 目录里没有图 : {len(no_image)}")
    print("=" * 60)

    for t, cover in found[:15]:
        size_kb = os.path.getsize(cover) // 1024
        print(f"  [{t.get('site_torrent_id') or '-'}] {t.get('formatted_title')[:50]}")
        print(f"       -> {os.path.basename(cover)} ({size_kb} KB)")
    if len(found) > 15:
        print(f"  ... 另有 {len(found) - 15} 条")

    for t in no_path[:5]:
        print(f"  [无路径] {t.get('formatted_title')[:60]}")
    for t in no_image[:5]:
        print(f"  [无图片] {t.get('formatted_title')[:60]}")

    if not args.apply:
        print("\n💡 预览模式。确认后加 --apply 上传图床并写回数据库。")
        return

    if not found:
        print("\n没有可上传的封面，退出。")
        return

    print(f"\n📤 开始上传 {len(found)} 张封面到图床...")
    ok = fail = 0
    for i, (t, cover) in enumerate(found, 1):
        title = (t.get("formatted_title") or "")[:45]
        print(f"[{i}/{len(found)}] {title}")
        url = assets.upload_to_picgo(cover)
        if url:
            db.set_cover(t["info_hash"], url)
            # 顺手把 content_path 也存下来，下次不用再问 qB
            if t.get("content_path"):
                db.save_task(info_hash=t["info_hash"], content_path=t["content_path"])
            ok += 1
            print(f"    ✅ {url}")
        else:
            fail += 1

    print("\n" + "=" * 60)
    print(f"🎯 封面回填完成：成功 {ok} 条，失败 {fail} 条")
    st = db.stats()
    print(f"📊 现在已发布但仍缺封面：{st['no_cover']} 条")
    if ok:
        print("\n💡 下一步：把新封面推到站点页面上")
        print("   python update_metadata.py --apply --only-cover")


if __name__ == "__main__":
    main()
