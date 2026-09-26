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

    # 副标题不在这里算：它依赖曲目/碟数，**还要按皇后/抓轨分流**，由 main() 拿到
    # tracklist/logs 后调 `descr.scan_small_descr()` 生成。
    # ★ 2026-09-26 订正：这里原先写「用 descr.build_small_descr 生成」—— 那句
    #   既是错的**叫法**（`build_small_descr` 只算曲目数、不看皇后），
    #   也是**真 bug 的出处**（每轮 scan 把皇后原文覆盖成「全 N 曲」）。
    #   ⇒ 别照着这行注释去"恢复"那个调用（`ERR-META-04`：改了行为要同时改文档）。
    #   ★ 渲染侧由 `build_form_fields()` 再按 `opencd_tid` 分流重算一次。
    return formatted_title, fmt, track_type


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
    #
    # ★★ 还要带出 `opencd_tid` / `mapped_json`（2026-09-26 加）——
    #    副标题要按它们分流：皇后转载路要用**皇后原文**，不能算「全 N 曲」。
    #    ★ 缺了这两列 = 下面 `scan_small_descr()` 拿不到分流依据 ⇒ 静默退回
    #      「全 N 曲」⇒ 每轮 scan 把皇后原文覆盖掉（`ERR-DATA-01` 的真根因）。
    #      ★★ 这正是本项目栽过三次的形状：**数据在手边、就是没传进来**
    #         （见 `db.TASK_FIELDS` 里那段同样的话）。
    conn = db.connect()
    cur = conn.cursor()
    cur.execute("SELECT info_hash, status, torrent_file, site_status, cover_url, "
                "opencd_tid, mapped_json FROM tasks")
    known = {row[0]: (row[1], row[2], row[3], row[4], row[5], row[6])
             for row in cur.fetchall()}
    conn.close()

    stats = {"skipped": 0, "no_log": 0, "ok": 0, "failed": 0, "skipped_no_log": 0}

    for torrent in torrents:
        info_hash = torrent.hash
        name = torrent.name
        content_path = torrent.content_path

        if not args.recheck and info_hash in known:
            (status, tfile, site_status, known_cover,
             known_opencd_tid, known_mapped) = known[info_hash]
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

            # ★ 已知无抓轨日志的种子：直接短路，**不再读目录、不再逐条打日志**。
            #
            # 为什么：qB 的 opencd 分类里约 43% 的种子本来就没有 .log
            # （WEB 下载 / 非抓轨来源），它们**永远不会被发布**（第一代的纪律：
            # 必须有 .log 才发，见本文件下面的 NO_LOG_FILE）。但原先是**每轮
            # 重扫一遍**：read_logs() 走一遍目录 + 打一行「❌ 跳过：…」，
            # 505 条里 216 条是这种，日志被这些**必然重复**的行占掉将近一半。
            #
            # ★ 判据用库内 `status`（这是上一次扫描的**结论**，已持久化），
            # 不是重新去读目录判断 —— 那才是"省掉重复劳动"这件事本身。
            # ★ `--recheck` 时**不短路**：那个开关的语义就是"无视缓存重做一遍"，
            # 若它也被短路，就没法用它来复核"这些真的还是没日志吗"了。
            if status == "NO_LOG_FILE":
                stats["skipped_no_log"] += 1
                continue

        print("-" * 60)
        print(f"扫描: {name}")

        logs = assets.read_logs(content_path)
        if not logs:
            print("❌ 跳过：目录里没有 .log 抓轨日志")
            db.save_task(info_hash=info_hash, torrent_name=name, status="NO_LOG_FILE",
                         reason="目录里没有 .log 抓轨日志", content_path=content_path)
            stats["no_log"] += 1
            continue

        # log_content/log_filename 仍存首个日志，兼容旧的显示/预览；多碟完整日志走 logs_json。
        log_content, log_filename = logs[0]["content"], logs[0]["filename"]

        formatted_title, fmt, track_type = parse_title(name, content_path)
        tracklist = assets.read_tracklist(content_path)

        # ★★ 副标题（2026-09-26 改）：**必须**按皇后/抓轨分流，不能一律算「全 N 曲」。
        #
        # 原先这里是 `descr.build_small_descr(tracklist, …)` = 数曲目算「全 N 曲」，
        # 它不看 `opencd_tid`/`mapped_json` ⇒ **每轮 scan 都把皇后原文覆盖掉**。
        # 而 scan 每 30 分钟跑一遍、已发布的种子仍在 qB 里 ⇒ `tools/recall_opencd_descr.py`
        # 那种一次性回写**跑完就被盖回去**（实测：回写 56 条后，下一轮变成"待改 8"，
        # 且每次数字都不同 —— 被扫到的退回、没扫到的保留原文）。
        #
        # ★ 走 `descr.scan_small_descr()` 而不是 `build_small_descr_for_task(task)`：
        #   后者读的是**列**（`task["small_descr"]`），而 scan 正要覆盖那一列
        #   ⇒ 会读到自己上一轮的旧值、再写回去，是**假修**。scan 手里只有
        #   `mapped_json` 这份**证据快照**，所以要用专门为它写的入口。
        #   （见 `descr.scan_small_descr` 的 docstring，那里写清了三个调用点的差别。）
        oid = known[info_hash][4] if info_hash in known else None
        mj = known[info_hash][5] if info_hash in known else None
        small_descr = descr.scan_small_descr(
            tracklist, disc_count=len(logs), fallback=name,
            mapped_json=mj, is_opencd=bool(oid))

        # ★ 把"来源"印出来（`ERR-META-14`：来源为空不许印成"映射成功"）——
        #   否则「全 N 曲」既是"本来就没有皇后副标题"、又是"没取到皇后信息"，
        #   两种相反的结论排出来一模一样。
        if oid:
            _src = "皇后路, 取 mapped_json" if mj else "皇后路, ★ 无 snapshot ⇒ 退回曲目数"
        else:
            _src = "抓轨路(曲目数)"
        print(f"✔ 主标题: {formatted_title}")
        print(f"✔ 副标题: {small_descr or '(空)'}   ← 来源: {_src}")
        print(f"📄 日志: {len(logs)} 个 [" + ", ".join(x['label'] for x in logs) + "]")
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

        # ★★ 重扫**不许**推翻别的阶段下的结论（`db.PROTECTED_STATUSES`）。
        #
        # 起因（实测，2026-09-23）：这里原先**无条件**写 `status="SUCCESS"`，
        # 而 `scan` 每 30 分钟重扫一遍 qB、**已发布的种子仍在 qB 里** ⇒
        # 把 `publish.py` 写的 `status='PUBLISHED'` 冲回 `SUCCESS`
        # （实测：08:56 是 `PUBLISHED`，09:2x 已是 `SUCCESS`）。
        # `save_task()` 只更新显式传入的列，所以 `site_status`/`site_torrent_id`
        # 没被冲掉 —— **没造成重复发布**，但"已发布"这个事实**丢了**。
        #
        # ★ 判据要**读库现况**，不能靠"这一轮 scan 里它是什么"（那是上一轮的结论）。
        # ★ `--recheck` 是**显式**要重做 ⇒ 它有权推翻；默认路径无权。
        if not args.recheck:
            prev = db.get_status(info_hash)
            if prev in db.PROTECTED_STATUSES:
                print(f"⏭️  已有结论 status={prev!r}（非 scan 的管辖）⇒ 只刷新元数据，不动 status")
                db.save_task(
                    info_hash=info_hash,
                    torrent_name=name,
                    formatted_title=formatted_title,
                    cover_url=cover_url,
                    torrent_file=torrent_file,
                    small_descr=small_descr,
                    tracklist=tracklist,
                    log_content=log_content,
                    log_filename=log_filename,
                    logs_json=json.dumps(logs, ensure_ascii=False),
                    content_path=content_path,
                )
                stats["ok"] += 1
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
            logs_json=json.dumps(logs, ensure_ascii=False),
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
            "logs": logs,
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
          f"无日志 {stats['no_log']} 个 | 已知无日志(短路) {stats['skipped_no_log']} 个 | "
          f"失败 {stats['failed']} 个")
    # ★★ 口径提醒（**别把两组数相减/相加**，它们是两个量纲）——
    #    2026-09-22 实测踩过，用假 qB + 假库跑 `run.main()` 复现：
    #      · 「无日志 N 个」  = **本轮**扫到几个没 .log 的 ⇒ 是**本轮增量**（qB 口径）
    #      · 「已知无日志(短路) M 个」= 库内**历史累积**、本轮连扫都没扫的
    #        ⇒ 但它是「**在 qB 里** 且 库里是 NO_LOG_FILE」的那部分（qB ∩ 库）
    #    ⇒ 库里的 NO_LOG_FILE 总数（= qB∩库 + **只在库里**的部分）**一般 > M**，
    #      也在**一般 ≠ N + M**。而 qB 里的种数又**一般 ≠** 这两者的任何组合。
    #    ★ 实测反例：qB 5 个种子、库 4 行（其中 1 行不在 qB 里）⇒ 报「无日志 2 + 短路 3」。
    #      2 + 3 = 5 正好等于 qB 种数 —— **看起来自洽**，
    #      但真值一个是 4（库）、一个是 5（qB）⇒ **数对得上，口径对不上**。
    #      这正是 `ERR-META-01` 一族的形状：一个看着很合理的错数字。
    #    ⇒ 想知道"库里到底多少条 NO_LOG_FILE"，**直接查库**（下面这行就是）：
    #      `db.stats()` 的 `groups` 是 `[(status, site_status, count), ...]`，
    #      按 status 汇总即可 —— **不从上面那两个数推**。
    st = db.stats()
    n_nolog_db = sum(g[2] for g in st["groups"] if g[0] == "NO_LOG_FILE")
    print("📊 库内共 {} 行，其中 status=NO_LOG_FILE 的 **{}** 行"
          "（★ 这才是「库里有多少没日志」的唯一真值，不是上面两个数之和）"
          .format(st["total"], n_nolog_db))
    print("   待发布可用 python publish.py 查看")


if __name__ == "__main__":
    main()
