"""
把 run.py 生成好的种子发布到 Musopia，并把新种子回加到 qBittorrent 做种。

改动要点：建表/查询走 db.py，简介渲染走 descr.py，HTTP 走 musopia.py。
发布成功后立刻用 edit.php 回读一次，确认站点上的内容跟本地一致——发布接口
拿到 302 只代表种子进库了，简介有没有正确落地是另一回事。
"""
import argparse
import os
import sys

import config
import db
import descr
from musopia import LoginExpired, MusopiaClient, SiteError


def parse_args():
    ap = argparse.ArgumentParser(description="发布待上传的种子到 Musopia")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认（用于定时任务）")
    ap.add_argument("--limit", type=int, default=0, help="最多发布多少条")
    ap.add_argument("--no-readd", action="store_true", help="发布后不回加到 qBittorrent")
    ap.add_argument("--dry-run", action="store_true", help="只列出将要发布的内容，不提交")
    # ★ 2026-09-17：匿名改为**默认开启** —— 想具名发布要显式 --no-anonymous。
    #   两个参数共用 dest="anonymous"，后加的那个覆盖前者（argparse 以最后一次为准）。
    ap.add_argument("--anonymous", action="store_true", default=True,
                    help="匿名发布（**默认**；保留此参数只为向后兼容，脚本里若显式传它也无害）")
    ap.add_argument("--no-anonymous", dest="anonymous", action="store_false",
                    help="★ 具名发布（覆盖默认的匿名）")

    return ap.parse_args()


def readd_to_qb(qb, client, info_hash, torrent_id):
    """把站点上的新种子下载回来加进 qB，做种路径沿用原种子的。"""
    if qb is None:
        return
    try:
        orig = qb.torrents_info(torrent_hashes=info_hash)
        if not orig:
            print("    ⚠️  qB 里找不到原始种子，无法确定做种路径，跳过回加")
            return
        save_path = orig[0].save_path

        content = client.download_torrent(torrent_id)
        if not content:
            print("    ❌ 从站点下载种子文件失败，跳过回加")
            return

        qb.torrents_add(
            torrent_files=content,
            save_path=save_path,
            is_skip_checking=True,
            is_paused=False,
            is_root_folder=False,
            category=config.PUBLISH_CATEGORY,
        )
        print(f"    ✅ 已回加至 qB（做种路径: {save_path}）")
    except Exception as e:
        print(f"    ❌ 回加 qB 出错: {e}")


def main():
    args = parse_args()

    config.print_runtime_info()
    db.init_db()

    tasks = db.get_pending_publish()
    if not tasks:
        print("💡 当前没有需要发布的任务。")
        return

    print("\n" + "=" * 60)
    print(f"📋 发现 {len(tasks)} 条待发布任务，预检清单：")
    print("=" * 60)

    valid, blocked = [], []
    for i, t in enumerate(tasks, 1):
        reasons = []
        if not (t.get("cover_url") or "").strip():
            reasons.append("缺封面")
        tf = t.get("torrent_file")
        if not tf or not os.path.exists(tf):
            reasons.append("种子文件缺失")

        flag = "✅ 可发布" if not reasons else "❌ " + "、".join(reasons)
        print(f"[{i}] {t.get('formatted_title')}")
        print(f"    副标题: {t.get('small_descr') or '(空)'}")
        print(f"    {flag}")

        (valid if not reasons else blocked).append(t)

    print("=" * 60)
    if blocked:
        print(f"⚠️  {len(blocked)} 条因缺封面或种子文件被拦下。"
              f"缺封面的可以先跑: python backfill_covers.py --apply --all")
    if not valid:
        print("没有符合发布条件的任务，退出。")
        return

    if args.limit:
        valid = valid[:args.limit]

    if args.dry_run:
        print(f"\n🔍 dry-run：以下 {len(valid)} 条会被发布，简介预览（第一条）：")
        fields = descr.build_form_fields(valid[0], anonymous=args.anonymous)

        print("-" * 60)
        print(fields["descr"][:1200])
        print("-" * 60)
        return

    if not args.yes:
        answer = input(f"\n确认向站点发布这 {len(valid)} 条吗？(y/N): ").strip().lower()
        if answer != "y":
            print("🚫 已取消，未向站点提交任何数据。")
            return

    client = MusopiaClient()
    try:
        uid = client.check_login()
        print(f"✅ Cookie 有效，user_id = {uid}")
    except SiteError as e:
        sys.exit(f"❌ {e}")

    qb = None
    if not args.no_readd:
        try:
            from qbittorrentapi import Client
            qb = Client(host=config.QB_HOST, username=config.QB_USER, password=config.QB_PASS)
            qb.auth_log_in()
        except Exception as e:
            print(f"⚠️  连不上 qBittorrent ({e})，本次不做回加")

    ok = fail = 0
    for i, task in enumerate(valid, 1):
        title = task.get("formatted_title")
        print(f"\n[{i}/{len(valid)}] 🚀 发布: {title}")

        fields = descr.build_form_fields(task, anonymous=args.anonymous)
        try:
            torrent_id, msg = client.upload_torrent(task["torrent_file"], fields)
        except (SiteError, LoginExpired) as e:
            print(f"    ❌ {e}")
            fail += 1
            continue

        if not torrent_id:
            print(f"    ❌ 发布失败: {msg}")
            fail += 1
            continue

        ok += 1
        print(f"    🎉 发布成功，站点 ID = {torrent_id}")
        db.set_site_id(task["info_hash"], torrent_id, site_status="EXISTS",
                       status="PUBLISHED", reason=f"published_id:{torrent_id}")
        db.mark_synced(task["info_hash"])

        # 回读确认简介真的落地了。takeupload.php 返回 302 只说明种子进库，
        # 简介字段有没有被正确接收要另外验证。
        try:
            got, _ = client.fetch_edit_form(torrent_id)
            site_descr = got.get("descr") or ""
            expect_cover = (task.get("cover_url") or "").strip()
            if expect_cover and expect_cover not in site_descr:
                print("    ⚠️  站点简介里没找到封面 URL，建议随后跑 "
                      f"update_metadata.py {torrent_id} --apply 修补")
            else:
                print(f"    🔎 简介已确认落地（{len(site_descr)} 字符）")
        except SiteError as e:
            print(f"    ⚠️  回读校验失败: {e}")

        readd_to_qb(qb, client, task["info_hash"], torrent_id)

    print("\n" + "=" * 60)
    print(f"🎯 发布完成：成功 {ok} 条，失败 {fail} 条")

    # ★ 说真话的退出码（2026-09-18）。
    #
    # daemon 判一个阶段成败**只看退出码**（`daemon.py:280` 的 `if code == 0`），
    # 而这里原先**一条路都不 sys.exit** —— 于是「成功 0 条，失败 5 条」也以 0 退出，
    # daemon 每轮把 `fails["publish"]` 清零 ⇒ **指数退避分支根本进不去**
    # ⇒ 按 `PUBLISH_INTERVAL` 原样重来一次。
    #
    # 后果不是"保险没生效"，是**反向**的：`daemon.py:14-15` 明写「Cookie 过期这类
    # 问题会让每个阶段都失败，不退避就是每 30 分钟无意义地敲站点一次」，
    # 而实测发种权限是**会抖**的（`ENVIRONMENT.md` `ERR-SITE-01`：16:19 全败、
    # 18:50 全成）⇒ 这个「一天 240 次无效 POST」的窗口会**反复**出现。
    #
    # ★ 判据分层，别混成一个数：
    #   - 有失败（重试有意义，多半是权限/站点抖动）→ 非 0，让退避生效；
    #   - 有成功、也有失败 → 同样非 0。**不能因为"成功了几条"就当成功** ——
    #     那正是原先那条静默路径的形状。
    #   - 失败一条都没有 → 0，正常。
    #   - 一条都没试（没待办 / 全被拦 / dry-run / 用户取消）→ 0。这些**不是**失败，
    #     退避它们只会让"没活干"被误记成"连续失败"。
    #
    # ★ 只退出码，**不 sys.exit(msg)** —— 上面那句汇总已经在 stdout 里了，
    #   再往 stderr 打一遍是重复信息（且容器里两条流都会进 docker logs）。
    if fail:
        # ★ 不在这里写死「每 30 分钟」—— 周期由 `PUBLISH_INTERVAL` 定，写死就等于
        #   制造一个必然过时的"事实"（README 首页那条纪律）。只报事实：失败了几条。
        print(f"↩️  以非 0 退出：有 {fail} 条失败，让 daemon 的退避生效"
              f"（不再按 PUBLISH_INTERVAL 原样重来）")
        sys.exit(1)


if __name__ == "__main__":
    main()
