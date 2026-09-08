"""
把站点上"我的上传"列表拉回来，按标题匹配本地任务，回填 site_torrent_id。

为什么必须有这一步：数据库里 255 条 site_status='EXISTS' 的记录，site_torrent_id
全是 NULL。原因是历史上先手工/旧脚本发布、再回填状态，而 update_publish_status()
只在"本次运行发布成功"的路径上才写 ID。没有 ID 就没法调 edit.php，自动化编辑
根本无从下手。所以先补 ID，再谈编辑。

用法:
    python sync_site.py              # 只报告，不写库
    python sync_site.py --apply      # 写回 site_torrent_id
"""
import argparse
import sys

import config
import db
from musopia import MusopiaClient, SiteError, normalize_title


def build_site_index(client):
    """{归一化标题: [id, ...]}，同时返回 id->标题 的原始映射。"""
    by_title = {}
    id_to_title = {}
    for tid, title in client.iter_my_uploads():
        id_to_title[tid] = title
        by_title.setdefault(normalize_title(title), []).append(tid)
    return by_title, id_to_title


def match_tasks(tasks, by_title):
    """
    返回 (matched, ambiguous, unmatched)。
    matched: [(task, torrent_id)]
    ambiguous: [(task, [id...])]  同名多条，不敢自动认领
    """
    matched, ambiguous, unmatched = [], [], []
    for t in tasks:
        for key in (normalize_title(t.get("formatted_title")),
                    normalize_title(t.get("torrent_name"))):
            if not key:
                continue
            ids = by_title.get(key)
            if not ids:
                continue
            if len(ids) == 1:
                matched.append((t, ids[0]))
            else:
                ambiguous.append((t, ids))
            break
        else:
            unmatched.append(t)
    return matched, ambiguous, unmatched


def main():
    ap = argparse.ArgumentParser(description="回填 site_torrent_id")
    ap.add_argument("--apply", action="store_true", help="实际写入数据库（默认只报告）")
    ap.add_argument("--all", action="store_true",
                    help="对所有 EXISTS 记录重新匹配（默认只处理缺 ID 的）")
    args = ap.parse_args()

    config.print_runtime_info()
    db.init_db()

    st = db.stats()
    print(f"📊 本地库：共 {st['total']} 行，其中 EXISTS 缺 ID 的 {st['no_site_id']} 条，"
          f"缺封面 {st['no_cover']} 条")

    client = MusopiaClient()
    try:
        uid = client.check_login()
    except SiteError as e:
        sys.exit(f"❌ {e}")
    print(f"✅ Cookie 有效，user_id = {uid}")

    print("🔍 正在拉取站点上传列表...")
    by_title, id_to_title = build_site_index(client)
    print(f"📥 站点共有 {len(id_to_title)} 个上传条目")

    tasks = db.get_published(only_missing_id=not args.all)
    if args.all:
        tasks = db.get_published(only_missing_id=False) + db.get_published(only_missing_id=True)
    print(f"📋 待匹配本地任务 {len(tasks)} 条")

    matched, ambiguous, unmatched = match_tasks(tasks, by_title)

    print("\n" + "=" * 60)
    print(f"✅ 唯一匹配 : {len(matched)}")
    print(f"⚠️  同名多条 : {len(ambiguous)}")
    print(f"❌ 未匹配   : {len(unmatched)}")
    print("=" * 60)

    for t, ids in ambiguous[:10]:
        print(f"  [多条] {t.get('formatted_title')}  -> {ids}")
    for t in unmatched[:15]:
        print(f"  [未匹配] {t.get('formatted_title')}")
    if len(unmatched) > 15:
        print(f"  ... 另有 {len(unmatched) - 15} 条未匹配")

    if not args.apply:
        print("\n💡 这是预览模式。确认无误后加 --apply 写入数据库。")
        return

    written = 0
    for t, tid in matched:
        old = t.get("site_torrent_id")
        if str(old or "") == str(tid):
            continue
        db.set_site_id(t["info_hash"], tid, site_status="EXISTS")
        written += 1
    print(f"\n💾 已回填 site_torrent_id：{written} 条")

    st = db.stats()
    print(f"📊 现在 EXISTS 仍缺 ID 的：{st['no_site_id']} 条")


if __name__ == "__main__":
    main()
