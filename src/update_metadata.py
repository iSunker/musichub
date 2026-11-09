"""
批量编辑站点上已发布种子的页面：主标题、副标题、简介（封面图/曲目/抓轨日志）。

相比旧版的关键改动：
1. 走 musopia.MusopiaClient.edit_torrent()，先读 edit.php 的现有表单值当基线，
   只覆盖要改的字段。旧版硬编码整套字段并把 medium/audiocodec/team/tags 全填 0，
   每编辑一次就把这些站点属性抹掉一次。
2. 默认 dry-run。改站点上 200 多个页面属于不可逆操作，必须先看清要改什么。
3. 简介渲染统一走 descr.build_form_fields()，和 publish.py 共用同一份逻辑。
4. 只处理 site_torrent_id 非空的记录；缺 ID 的先跑 sync_site.py 回填。
5. 本地 cover_url 为空时，先读站点表单从现有简介里回收封面 URL：
   一是回写本地库修复历史数据，二是避免用空封面渲染 descr 把站点上
   仅剩的封面也抹掉（takeedit 是整表覆盖语义）。

用法:
    python update_metadata.py                      # 预览全部（不提交）
    python update_metadata.py --apply              # 实际提交全部
    python update_metadata.py 14866                # 预览单条
    python update_metadata.py 14866 --apply        # 提交单条
    python update_metadata.py --apply --limit 3    # 先拿 3 条试水
    python update_metadata.py --apply --only-cover # 只改有封面的
    python update_metadata.py --apply --verify     # 提交后回读详情页校验
"""
import argparse
import os
import sys
import time

import config

import db
import descr
from musopia import LoginExpired, MusopiaClient, SiteError

#   两处各写一份折空白/NFC 的逻辑，迟早会漂，而这里漂的后果是"版本不符"报错/漏报。
try:
    from site_verify import normalize_name as _norm_name
except Exception:                                    # noqa: BLE001 —— 兼容旧镜像
    def _norm_name(s):                               # 兜底：只去首尾空白
        return (s or "").strip()


def parse_args():
    ap = argparse.ArgumentParser(
        description="批量编辑 Musopia 已发布种子的标题/副标题/简介",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("torrent_id", nargs="?", help="只处理这一个站点种子 ID")
    ap.add_argument("--apply", action="store_true",
                    help="真正提交修改（不加此参数只预览，默认安全）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条，0 表示不限")
    ap.add_argument("--only-cover", action="store_true",
                    help="只处理本地已有封面 URL 的记录")
    ap.add_argument("--stale-only", action="store_true",
                    help="只处理未同步过或本地有更新的记录（定时任务用，避免每轮空转）")

    ap.add_argument("--no-autodetect", action="store_true",
                    help="不自动推断 载体/编码 下拉项，保留站点现值")
    ap.add_argument("--anonymous", action="store_true",
                    help="把种子改成匿名发布（默认不动这个开关，沿用站点现状）")

    #
    # 实际实现：`select_fields()` 做的是
    #     {k: v for k, v in full_fields.items() if k in keep}
    # 而 `keep` 来自把 `--fields` 按逗号切开 —— **没有任何键名白名单校验**，
    # ⇒ 它是**任意键**的过滤器，键名就是 `descr.build_form_fields()` 那份 dict 的键。
    #
    #   给什么键就只提交什么键，其余沿用**页面上现值**
    #   （`musopia.edit_torrent()` docstring: "overrides 里给什么就改什么"）。
    #   ⇒ 所以 `name`（会换成皇后标题）、`small_descr`（会清空站点副标题）、
    #     `descr` **不写进 `--fields` 就不会被碰** —— 这正是"只补不覆盖"的实现。
    #   实测：补 `tags[4][]` 等 6 个键之后回读，标题仍是我们自己的写法。
    #
    # ★ 但 **`id` 永远保留**（`select_fields` 的调用方 `full["id"]=…`），漏写它没关系。
    # ★ 完整键名清单：`--list-fields`（免得再去读 descr.py 猜）。
    ap.add_argument("--fields", default="",
                    help="逗号分隔的**任意键**白名单，只提交列出的字段；未列出的"
                         "字段沿用站点现值（所以不列 name/small_descr/descr 就"
                         "不会覆盖它们）。默认（空）＝提交全部。"
                         "完整键名见 --list-fields。"
                         "例：--fields 'tags[4][],custom_fields[4][1],team_sel[4]'")
    ap.add_argument("--verify", action="store_true", help="提交后回读详情页确认")
    # ★ 加这个开关的理由：`--fields` 的 help 要指向一份**真实**的键名清单，
    #   而清单的真值只在 `descr.build_form_fields()` 的返回 dict 里
    #   （这份表套搬过 `takeupload.php` 的字段名，手抄必然过时）。
    #   ⇒ 不要去读源码猜，**print 出来**。
    ap.add_argument("--list-fields", action="store_true",
                    help="只打印 --fields 可用的键名（真值来自"
                         " descr.build_form_fields()，不读源码猜），然后退出")
    ap.add_argument("--yes", action="store_true",
                    help="跳过批量二次确认。容器里没有 TTY，input() 会抛 EOFError，"
                         "定时任务必须加这个")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="两次 HTTP 请求之间的最小间隔秒数，默认 2.0")
    ap.add_argument("--rate", type=float, default=0,
                    help="限速：每分钟处理多少个种子（例如 10）。0 = 不限速。"
                         "按「种子」计，比 --interval 按「请求」计更贴合实际需求，"
                         "因为一个种子要发 2~3 个请求（读表单/提交/校验）")
    ap.add_argument("--selftest", action="store_true",
                    help="跑本文件的自检（#173 编辑身份护栏：判据 + 接线），然后退出")
    ap.add_argument("--json", action="store_true",
                    help="自检结果以 JSON 打到末行（给 tools/gen_counts.py 读）")
    return ap.parse_args()


def select_fields(full_fields, whitelist):
    """按 --fields 过滤要提交的覆盖项。id 永远保留。"""
    if not whitelist:
        return full_fields
    keep = {f.strip() for f in whitelist.split(",") if f.strip()}
    return {k: v for k, v in full_fields.items() if k in keep}


def describe(task):
    return (f"id={task.get('site_torrent_id')} | {task.get('formatted_title')}\n"
            f"       副标题: {task.get('small_descr') or '(空)'}\n"
            f"       封面  : {'有' if task.get('cover_url') else '无'}"
            f" | 曲目 {len((task.get('tracklist') or '').splitlines())} 行"
            f" | 日志 {len(task.get('log_content') or '')} 字符")


def selftest(verbose=True):
    """#173 编辑身份护栏的自检：判据（含阴性对照）+ **接线**。

    ★ 独立失效模式：护栏**写了但没接**（判据在 `site_verify` 里，`main()` 却不调它）
      ⇒ 仍是"假安全"。故分两半：
        ① 纯判据 `site_verify.edit_target_ok`（含阴性对照：名字不符必须拒）；
        ② 结构：`main()` 里必须在 `edit_torrent(` **之前**出现 `edit_target_ok(`。
    ★ 零网络、零库、零 .env（只用纯函数 + 读本文件源码）。
    """
    import ast
    import site_verify as sv

    fails = []
    detail = []

    def chk(label, cond):
        if cond:
            detail.append(f"  ✅ {label}")
        else:
            fails.append(f"❌ {label}")

    # ① 判据（含阴性对照）
    ok, _ = sv.edit_target_ok("Album X", "Album X")
    chk("同名 ⇒ 放行", ok is True)
    ok, why = sv.edit_target_ok("Album X", "CTRL.2024.1080p.NF.WEB-DL")
    chk("站侧是**别人的页**（名字不符）⇒ 拒改", ok is False)
    ok, _ = sv.edit_target_ok("Album X", None)
    chk("站侧取不到名 ⇒ 拒改（不可知≠是我的）", ok is False)
    ok, _ = sv.edit_target_ok("", "Album X")
    chk("本地名为空 ⇒ 拒改", ok is False)
    # 空白折叠加阴性对照（复用 normalize_name 口径）
    ok, _ = sv.edit_target_ok("Album  X", "Album X")
    chk("仅空白差异 ⇒ 放行（归一化后相等）", ok is True)

    # ② 接线（结构性，AST —— 不看文本，免得注释里的标识符骗过判据）
    src = open(__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    main_fn = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if main_fn is None:
        fails.append("❌ 找不到 main() ⇒ 判据没测到东西")
    else:
        call_lines = {}
        for n in ast.walk(main_fn):
            if isinstance(n, ast.Call):
                fn = n.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if name in ("edit_target_ok", "edit_torrent"):
                    call_lines.setdefault(name, []).append(n.lineno)
        chk("main() 调用 edit_target_ok（护栏接线）", bool(call_lines.get("edit_target_ok")))
        chk("main() 调用 edit_torrent（被测动作仍在）", bool(call_lines.get("edit_torrent")))
        if call_lines.get("edit_target_ok") and call_lines.get("edit_torrent"):
            chk("护栏在 edit_torrent **之前**（行号）",
                min(call_lines["edit_target_ok"]) < min(call_lines["edit_torrent"]))

    if verbose:
        print(f"{'✅' if not fails else '❌'} update_metadata 自检（#173 编辑身份护栏）")
        for d in detail:
            print(d)
        for f in fails:
            print(f)
    return (not fails), (fails or detail)


def main():
    args = parse_args()

    if args.selftest:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:                            # noqa: BLE001 —— 老 Python/无此属性
            pass
        ok, _info = selftest(verbose=not args.json)
        if args.json:
            import json as _json
            print(_json.dumps({"script": "update_metadata.py --selftest", "ok": bool(ok),
                               "total": 8, "exit": 0 if ok else 1}, ensure_ascii=False))
        sys.exit(0 if ok else 1)

    config.print_runtime_info()
    db.init_db()

    # ★ `--list-fields`：把 `--fields` 的可用键名**印出来**，然后退出。
    #   真值来自真跑一次 `build_form_fields()`，不是手抄清单
    #   （手抄的那份正是这次订正掉的错误来源）。
    if args.list_fields:
        tasks = db.get_published()
        if not tasks:
            sys.exit("❌ 库里没有已发布的记录，无法取样本渲染字段名。")
        sample = tasks[0]
        import publish as _pub
        _pub.merge_mapped(sample)
        full = descr.build_form_fields(sample, anonymous=args.anonymous,
                                       autodetect=not args.no_autodetect)
        full.pop("type", None)   # 编辑路永远不发这个（见下面 describe 之后的注释）
        print(f"\n📋 --fields 可用的键名（样本 id={sample.get('site_torrent_id')}，"
              f"共 {len(full)} 个）：\n")
        for k in sorted(full):
            v = full[k]
            sv = str(v)
            if len(sv) > 56:
                sv = sv[:53] + "..."
            print(f"  {k:<34} = {sv!r}")
        print("\n★ 未列出的键**不会**被提交 ⇒ 站点上那些格子沿用现值。"
              "\n★ 想保留站点的标题/副标题/简介，就**别把** name / small_descr / descr"
              " 放进 --fields。")
        return

    if args.torrent_id:
        #   理由：`--torrent_id` 不改"这条路会写站点"的性质。若为了"能手工改"而
        #   在这里放行，洞就还在（只是从批量路挪到了点名路）—— 而 `--torrent_id`
        #   正是最容易在不知情时敲到 DENY 行的那条路（人只看到一个 sid）。
        #   ★ 真要改某个 DENY 行的页面，**先把它标成 ALLOW**（这是一次有记录的动作），
        #     而不是在这里开一个静默的例外。
        tasks = db.get_published(torrent_id=args.torrent_id, require_allow=True)
        if not tasks:
            #   列出多个原因却不给分辨法，读的人只能猜，而猜错要花好几轮去追）。
            #   这里能分辨：库里查得到、但被闸门④ 拦住 ⇒ 与"库里根本没有"不同。
            _raw = db.get_published(torrent_id=args.torrent_id)
            if _raw:
                _r = _raw[0].get("transfer_rule")
                sys.exit(
                    f"⛔ site_torrent_id={args.torrent_id} 在库里存在，但"
                    f"`transfer_rule={_r!r}` ⇒ 被**闸门④（edit 路）**拦住，不会编辑。\n"
                    f"   ★ 闸门④ 只放行 `'ALLOW'`：空 / DENY / NEED_CONFIRM 一律拦"
                    f"（`transfer_rule` 空的语义是「**不知道**能不能转」，不是「允许」）。\n"
                    f"   ⇒ 若确认这条有权改，**先显式标 ALLOW**（一次有记录的写库动作），"
                    f"再重跑本命令。★ 不要绕过闸门。")
            sys.exit(f"❌ 数据库里找不到 site_torrent_id={args.torrent_id} 的记录。\n"
                     f"   如果这条是历史发布，先跑: python sync_site.py --apply")
    else:
        # ★★★ `require_allow=True` —— 本路**会把内容写到站点上**，必须过闸门④。
        #   ★ 探针 `db.count_stale()` 也必须带同一条闸门，否则会出现
        #     "探针说有活 → edit 选不出来 → 探针还说有活" 的高频空转。
        tasks = db.get_published(only_with_cover=args.only_cover,
                                 only_stale=args.stale_only,
                                 require_allow=True)

    st = db.stats()
    if st["no_site_id"]:
        print(f"⚠️  有 {st['no_site_id']} 条 EXISTS 记录还缺 site_torrent_id，"
              f"这次会被跳过。补齐请先跑: python sync_site.py --apply")

    if not tasks:
        print("💡 没有可编辑的任务。")
        return

    if args.limit:
        tasks = tasks[:args.limit]

    print("\n" + "=" * 60)
    print(f"📋 待编辑 {len(tasks)} 条" + ("（预览模式，不会提交）" if not args.apply else ""))
    print("=" * 60)
    for i, t in enumerate(tasks[:20], 1):
        print(f"[{i:>3}] {describe(t)}")
    if len(tasks) > 20:
        print(f"  ... 另有 {len(tasks) - 20} 条")
    print("=" * 60)

    if args.apply and len(tasks) > 1 and not args.yes:
        answer = input(f"\n确认要修改站点上这 {len(tasks)} 个页面吗？此操作不可撤销 (yes/N): ")
        if answer.strip().lower() not in ("yes", "y"):
            print("🚫 已取消，未向站点提交任何数据。")
            return

    client = MusopiaClient(request_interval=args.interval)
    try:
        uid = client.check_login()
        print(f"\n✅ Cookie 有效，user_id = {uid}\n")
    except SiteError as e:
        sys.exit(f"❌ {e}")

    ok = failed = skipped = warnings = refused = 0
    failures = []
    refused_rows = []

    # --rate 10 → 每个种子至少占 6 秒。注意这是「每分钟几个种子」，
    # 而 --interval 是「每次 HTTP 请求的间隔」：一个种子要发 2~3 个请求
    # （读 edit.php + 提交 takeedit + 可选的 details 校验），
    # 所以只调 --interval 是算不准每分钟处理量的。
    min_seconds_per_task = (60.0 / args.rate) if args.rate > 0 else 0
    if min_seconds_per_task:
        print(f"⏱️  限速：{args.rate:g} 个/分钟（每个至少 {min_seconds_per_task:.1f}s），"
              f"预计耗时 {len(tasks) * min_seconds_per_task / 60:.1f} 分钟\n")

    started_at = time.monotonic()

    for i, task in enumerate(tasks, 1):
        # 限速：等到这一条"该开始"的时刻。用绝对时间基准而不是每轮 sleep 固定值，
        # 这样站点慢的时候不会把延迟叠加上去，真实速率才稳定在设定值。
        if min_seconds_per_task:
            due = started_at + (i - 1) * min_seconds_per_task
            wait = due - time.monotonic()
            if wait > 0:
                time.sleep(wait)

        tid = task.get("site_torrent_id")
        title = task.get("formatted_title") or task.get("torrent_name")
        print(f"[{i}/{len(tasks)}] id={tid} {title}")

        # ★★★ `#173` 编辑护栏（**在动站点之前**）：核"这个 tid 真是我的页吗"。
        #   ★ 为什么必须有：`site_torrent_id` 可能是**错值**（#173 实测 21 条 crabpt
        #     行指向**别人的**电影/音乐页）⇒ 若照改，会把本地音乐任务的元数据
        #     **写进别人的页**（不可逆）。判据 = 站侧 `.torrent` 的 `info.name`
        #     == 本地 `.torrent` 的 `info.name`（`site_verify.edit_target_ok`）。
        #   ★ 放在**最前**（读表单/回收封面之前）：拒改是本地判据，不该先敲站点。
        #   ★ dry-run 也要拦：dry-run 打印的"将要改什么"若基于错页就是误导。
        _local_name = ""
        try:
            _tf = task.get("torrent_file") or ""
            if _tf and os.path.exists(_tf):
                import site_verify as _sv
                _local_name, _ = _sv.extract_info_name(open(_tf, "rb").read())
        except Exception:                                # noqa: BLE001 —— 取不到名 ⇒ 交由护栏拒改
            _local_name = ""
        try:
            _tb = client.download_torrent(tid)
            _hosted_name, _ = (_sv.extract_info_name(_tb) if _tb else (None, ""))
        except SiteError as e:
            print(f"    ❌ 取站侧 .torrent 失败 ⇒ 拒改：{e}")
            refused += 1
            refused_rows.append((tid, str(e)))
            continue
        _ok, _why = _sv.edit_target_ok(_local_name, _hosted_name)
        if not _ok:
            print(f"    🛑 拒改（#173 护栏）：{_why}")
            refused += 1
            refused_rows.append((tid, _why))
            continue

        # 本地封面为空（历史 bug：run.py 重做任务时重复上传封面，图床报
        # Duplicated upload 400，空值写回把 URL 抹掉了）——先读站点表单，
        # 从现有简介里把封面 URL 抠回来。抠到了就回写本地库并用于本次渲染；
        # 抠不到说明站点页面本来就没图，按无封面处理。
        # 表单只拉这一次，后面 edit_torrent(prefetched=...) 直接复用。
        prefetched = None
        if not (task.get("cover_url") or "").strip():
            try:
                prefetched = client.fetch_edit_form(tid)
            except LoginExpired as e:
                sys.exit(f"❌ {e}")
            except SiteError as e:
                print(f"    ❌ {e}")
                failed += 1
                failures.append((tid, str(e)))
                continue
            site_cover = descr.extract_cover_url(prefetched[0].get("descr") or "")
            if site_cover:
                task["cover_url"] = site_cover
                print(f"    🔁 本地无封面，从站点简介回收: {site_cover}")
                if args.apply:
                    # 回写修复本地库。set_cover 会刷新 updated_at，本条随即
                    # 重新满足 stale 条件，但下面编辑成功后 mark_synced 会
                    # 盖过去，不会造成下轮空转。
                    db.set_cover(task["info_hash"], site_cover)
            else:
                print("    ℹ️  本地与站点均无封面，简介将不含图片")

        # ★★ 皇后路：把 `mapped_json` 解回顶层键（`tags`/`medium_sel`/
        #    `audiocodec_sel`/`team_sel`）—— 否则那四格会退回 `detect_*()` **从标题猜**，
        #    而**编辑动作会把猜出来的值写回站点**（比发布路径更危险：编辑是覆盖）。
        #    ★ 复用 `publish.py` 的 `merge_mapped` —— **同一件事只维护一份**，
        #      免得两处漂移（「同一事实散在多处必然漂移」是本项目的成文纪律）。
        #    ★ 第一阶段的行没有 `mapped_json` ⇒ 什么都不改，行为与改动前一致。
        import publish as _pub
        _pub.merge_mapped(task)

        full = descr.build_form_fields(
            task,
            anonymous=args.anonymous,
            autodetect=not args.no_autodetect,
        )

        full.pop("type", None)
        overrides = select_fields(full, args.fields)

        try:
            success, msg = client.edit_torrent(
                tid, overrides, dry_run=not args.apply, verbose=True,
                prefetched=prefetched,
            )
        except LoginExpired as e:
            sys.exit(f"❌ {e}")
        except SiteError as e:
            print(f"    ❌ {e}")
            failed += 1
            failures.append((tid, str(e)))
            continue

        if success:
            if "无需修改" in msg:
                skipped += 1
                print(f"    ⏩ {msg}")
                # 站点内容已经和本地一致，也要标记同步过。
                # 否则这条永远满足 --stale-only 条件，每一轮定时任务都会重新
                # 拉一次 edit.php 做对比——254 条里绝大多数最终都是"无需修改"，
                # 不标记就等于每小时把整站页面重新爬一遍。
                if args.apply:
                    db.mark_synced(task["info_hash"])
            else:
                ok += 1
                print(f"    ✅ {msg}")
                if args.apply:
                    db.mark_synced(task["info_hash"])

                if args.apply and args.verify:
                    try:
                        info = client.fetch_details(tid)
                        if info["has_cover"]:
                            flag = "✅ 封面已生效"
                        elif task.get("cover_url"):
                            # 本地明明有封面 URL，页面却没检出 —— 这才是真异常
                            flag = "❌ 本地有封面但页面未检出，请人工检查"
                        else:
                            # 本地就没封面，页面没图是预期行为，不是编辑失败。
                            # backfill_covers 补齐后 set_cover 会刷新 updated_at，
                            # 记录重新变 stale，edit 阶段下轮自动把封面写进简介。
                            flag = "ℹ️ 本地无封面（backfill_covers 补齐后会自动重编辑）"
                        print(f"    🔎 校验: {flag} | 标题《{info['title'][:50]}》"
                              f" | 页面图片 {info['image_count']} 张")
                        #   为什么**必须**在这里断言：`edit_torrent` 只改**元数据**，
                        #   页面上那个 `h1`（= 站点渲染的种子名）反映的是**挂的种子**。
                        #   若 `h1` 与本地 `torrent_name` 对不上，说明这页**挂的是别的版本**
                        #   （`#136` 的多版本同名碰撞，见 #32 报告里 8 个 MISMATCH）
                        #   —— 那是 edit **修不了**的，必须**重发**。
                        #   ⇒ 让 edit 的"校验"把它喊出来，而不是让编辑**静默成功**。
                        db_name = (task.get("torrent_name") or "").strip()
                        h1 = (info.get("title") or "").strip()
                        if db_name and h1 and _norm_name(h1) != _norm_name(db_name):
                            print(f"    ❌ 版本不符（挂的可能是别的版本，edit 改不了，需重发）:\n"
                                  f"        DB  : {db_name}\n"
                                  f"        页面h1: {h1}")
                            warnings += 1
                    except SiteError as e:
                        print(f"    ⚠️  校验失败: {e}")
        else:
            failed += 1
            failures.append((tid, msg))
            print(f"    ❌ {msg}")

    print("\n" + "=" * 60)
    verb = "已提交" if args.apply else "将修改"
    print(f"🎯 完成：{verb} {ok} 条 | 无变化 {skipped} 条 | 失败 {failed} 条 | 🛑 拒改 {refused} 条"
          + (f" | ⚠️ 版本不符 {warnings} 条" if warnings else ""))
    if warnings:
        print("   ⚠️  有页面**挂的版本与 DB 不符**（edit 改不了，需重发；见 #32 报告 / #34）")
    if refused:
        print("   🛑 有页面**被 #173 身份护栏拒改**（`site_torrent_id` 可能指向别人的页）：")
        for tid, msg in refused_rows[:20]:
            print(f"      id={tid}: {msg}")
    if failures:
        print("失败明细：")
        for tid, msg in failures[:20]:
            print(f"  id={tid}: {msg}")
    if not args.apply:
        print("\n💡 以上为预览。确认无误后加 --apply 真正提交。")
        print("   建议先 --apply --limit 1 --verify 单条试水。")


if __name__ == "__main__":
    main()
