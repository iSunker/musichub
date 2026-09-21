"""
把 run.py 生成好的种子发布到 Musopia，并把新种子回加到 qBittorrent 做种。

改动要点：建表/查询走 db.py，简介渲染走 descr.py，HTTP 走 musopia.py。
发布成功后立刻用 edit.php 回读一次，确认站点上的内容跟本地一致——发布接口
拿到 302 只代表种子进库了，简介有没有正确落地是另一回事。
"""
import argparse
import json
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


def merge_mapped(task):
    """
    把 `task["mapped_json"]`（`opencd_map.map_all()` 的**证据快照**）解回**顶层键**。

    ★★ 为什么必须有这一步（这是最容易漏、且**漏了不报错**的一环）：
      `descr.build_form_fields()` 读的是 `task["tags"] / ["medium_sel"] /
      ["audiocodec_sel"] / ["team_sel"]` 这些**顶层键**，而
      `db.get_pending_publish()` 只 SELECT `db.TASK_FIELDS`（没有这四列 ——
      它们存在 `mapped_json` 里）。
      ⇒ 不合并 ⇒ 那四个键是 `None` ⇒ `descr` 退回 `detect_medium()` /
        `detect_tags()` **从标题猜** ⇒ 页面看着"有值"，**值是猜的**。
        这正是 `SUMMARY #10` 那个 bug 的形状（静默、不报错）。

    ★ 用**快照**而不是当场重算：`TAG_NAME_MAP` / `MEDIUM` 会变（44 项标签表刚改过、
      WAV 编号未定）⇒ 发布要用的就是三天前抓页面时做的那个决定。
    ★ 第一阶段（qB 本地抓轨）的行没有 `mapped_json` ⇒ `_load_mapped` 返回 `{}`，
      这里**什么都不改**，行为与改动前完全一致。
    ★ 解析失败会**抛出**（`db._load_mapped` 的契约）—— 响亮地坏，不静默当空。
    """
    mapped = db._load_mapped(task)
    for key in ("tags", "medium_sel", "audiocodec_sel", "team_sel"):
        if key in mapped and mapped[key] not in (None, ""):
            task[key] = mapped[key]
    return task


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

        # ★★ 没有 log 就不发布 —— **最初的规矩**（2026-09-21 用户重申并确认）。
        #   ★ 为什么必须在这里也拦一道：`run.py` 的 `NO_LOG_FILE` 短路只覆盖
        #     **qB 本地抓轨那条路**（它读目录找 `.log`）。皇后转载那条路的日志
        #     来自**页面里的 NFO/LOG 字段**，那是**另一条来源** ——
        #     页面本来就没有（`<div id="divnfo"><pre></pre></div>`，实测三个
        #     Web 来源页面都是空的）⇒ 它**不会**被 `run.py` 判断到。
        #   ★ 而且乌托邦的 `custom_fields[4][3]`（抓轨日志）是**必填**：
        #     发一条空的出去，站点大概率退回，症状正是 `ERR-SITE-01` 那个
        #     「HTTP 200 + 空 body」—— **失败还看不出原因**。
        #   ⇒ 拦在发布前，进邮件提醒，让人自己判断。
        has_log = bool((t.get("log_content") or "").strip())
        if not has_log:
            # 多碟日志走 logs_json，也要认（否则会误拦）
            try:
                lj = json.loads(t.get("logs_json") or "null")
                has_log = bool(lj) and any(
                    (x.get("content") or "").strip()
                    for x in lj if isinstance(x, dict))
            except (ValueError, TypeError):
                pass
        if not has_log:
            reasons.append("无 log（最初的规矩：没有 log 不发布）")

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
        # ★ 与真实发布走**同一个 merge_mapped**，否则预览与实际不一致
        fields = descr.build_form_fields(merge_mapped(valid[0]),
                                         anonymous=args.anonymous)

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

        fields = descr.build_form_fields(merge_mapped(task),
                                         anonymous=args.anonymous)
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
        #
        # ★★ 2026-09-21 扩到**表单字段**（原先只查简介里的封面 URL）：
        #    为什么必须扩：2026-09-19 那 5 条发布，程序全报成功、
        #    但站点上「艺术家 / 发行年份」整行都不存在 —— 因为那些值在
        #    `custom_fields[4][*]` 里是空的，而**回读校验从不看这些字段**
        #    （`ERR-META-01`：检测方法只覆盖了一部分，于是"全绿"是假的）。
        #    ★ 判据是「**回读到的值 == 我们发出去的值**」，逐字段比，不是看有没有。
        try:
            got, _ = client.fetch_edit_form(torrent_id)
            site_descr = got.get("descr") or ""
            expect_cover = (task.get("cover_url") or "").strip()
            if expect_cover and expect_cover not in site_descr:
                print("    ⚠️  站点简介里没找到封面 URL，建议随后跑 "
                      f"update_metadata.py {torrent_id} --apply 修补")
            else:
                print(f"    🔎 简介已确认落地（{len(site_descr)} 字符）")

            # ---- 表单字段逐项回读（★ 比的是「发出去的 vs 回读到的」）----
            # tags[4][] 是**多值**：两边都排好序再比，否则顺序不同会假报差异。
            sent_tags = sorted(str(x) for x in (fields.get("tags[4][]") or []))
            got_tags = sorted(str(x) for x in (got.get("tags[4][]") or []))
            if sent_tags != got_tags:
                print(f"    ⚠️  标签回读不一致：发出 {sent_tags} / 站点 {got_tags}")

            # 编码/媒介：发出 "0" = 我们**没指定**，那不比对（比对会假报"不一致"）。
            for key, label in (("audiocodec_sel[4]", "音频编码"),
                               ("medium_sel[4]", "媒介")):
                sent = str(fields.get(key) or "")
                if sent and sent != "0":
                    back = str(got.get(key) or "")
                    if sent != back:
                        print(f"    ⚠️  {label}回读不一致：发出 {sent} / 站点 {back}")

            # ★ 已知缺口（见 SUMMARY #10）：custom_fields[4][*] 我们目前一律发空，
            #   所以这里**刻意只提示、不判失败** —— 一旦开始填它们，就该升级成硬校验。
            empties = [k for k in ("custom_fields[4][1]", "custom_fields[4][2]",
                                  "custom_fields[4][3]", "custom_fields[4][4]")
                       if not (fields.get(k) or "").strip()]
            if len(empties) == 4:
                print("    ℹ️  自定义字段（艺术家/发行年份等）仍为空 —— 已知缺口，"
                      "见 SUMMARY #10（修完这条提示应消失）")
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
