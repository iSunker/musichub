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
    ap.add_argument("--selftest", action="store_true",
                    help="跑离线自检（C' 三态判据；不连 qB、不连库、零请求）")
    return ap.parse_args()


def parse_title(raw_title, folder_path):
    """
    从原始种子名拆出 歌手/专辑/年份/格式/整轨分轨，拼出规整主标题和副标题。
    这套拆分本身就不可能百分之百准，拆不出来时保底用原名，不要产出残缺标题。
    """
    year_match = re.search(r"\(?((?:19|20)\d{2})\)?", raw_title)
    year = year_match.group(1) if year_match else ""

    # ★ 2026-10-14（`ERR-PUBLISH-20` 修复①）：版本标签只认 `assets.cue_track_type()`
    #   这一个**强判据**出口（cue 的 FILE 目标必须真存在才算整轨）。
    #   旧写法 `assets.has_cue()` 只看"有没有 .cue 文件"⇒ 被"有 cue 但 cue 指向
    #   文件缺失"的假整轨骗过（实测 21662）⇒ 站点标题/版本挂错。
    track_type = assets.cue_track_type(folder_path)

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
    # ★★ 2026-09-28：**尾部不再拼 ` - OpenCD`**（用户拍板）。
    #
    #   ★ 起因（实测，25 条已上站的标题逐条核对）：
    #     · ` - OpenCD` 是**皇后自己写的** —— 它在 `mapped.title` 里就带着
    #       （如 `Mary Black - Babes In The Wood … - FLAC Multi File - OpenCD`）。
    #       原先这里再拼一次，等于**同一个标记写两遍**，而且拼出来的那份
    #       与皇后原文**逐条不同**（库里只有 1/25 与 `mapped.title` 相同）。
    #     · 这条支对皇后行**本该是死代码**：`opencd_ingest.build_row()` 已经把
    #       `formatted_title` 写成 `mapped.title`（皇后原文）。但**早期入库**的行
    #       （那时 `build_row` 还没这个字段）先由这里占了位 ⇒ 现在 24/25 是
    #       **本函数的产物**，而不是皇后原文。
    #
    #   ★★ 为什么是"删尾缀"而不是"改成用皇后原文"：本函数**拿不到** `opencd_tid`
    #      （它在 `main()` 里从 `known` 查出来，见下面 `oid`），而 `parse_title()` 是
    #      纯函数。而真正对齐皇后的那条路**已经存在**（`opencd_ingest`）⇒
    #      在这里改是**补一条重复的路**，正是本项目反复栽的形状（`ERR-META-03`）。
    #
    #   ★★★ 2026-09-28 **续改（C'）**：删尾缀**不够** —— 实测（16:19 / 16:50 两轮
    #      scan 的日志）**新代码产出的标题照样畸形**：
    #         `Vaundy & yama - 飛ぶ時 ／ 飛ぼうよ - FLAC 整轨`  ← **年份没了**
    #         `9Lana - 螺旋 - FLAC 整轨`                       ← **年份没了**
    #         `齐秦 - 我拿什么爱你 FLAC 1998 - FLAC 整轨`        ← 格式跑到年份前
    #         `★lynchULTILDEf - FLAC 分轨`                      ← 歌手名 = 原始种子名
    #      ⇒ **本函数（`parse_title` 的二次拆分）本身就是畸形的源头**，
    #        删尾缀只治了症状。真正的修法在 `main()`：见下面 `mt_override` ——
    #        皇后路**根本不把这里的产物写进库**。
    #      ★ 但本函数的**返回值仍要留着**（`fmt`/`track_type` 是渲染侧要用的，
    #        且抓轨路仍靠它出标题）⇒ 这里**不删代码**，只把"谁写库"改掉。
    #
    #   ★★ 连带后果（已核对，**不是** `ERR-DATA-06` 那种副作用）：
    #     · 本行只影响**标题字符串**；`create_musopia_torrent()` 把内容目录做成
    #       `.torrent`，**标题不进 info dict** ⇒ **不改 infohash**。
    #     · `torrent_file` 的文件名由 `output_sidecar_paths()` 算（看下面）⇒ 库里那个
    #       路径会在下一轮 `scan` 里被重算，`save_task` 会连 `updated_at` 一起刷新。
    #       ★ 但重算**稳定**：消歧只看 `info_hash`（重扫不变）⇒ 名字不漂移。
    #       （#136 之前是 `safe_name` 只按标题算 ⇒ 同名版本互踏，见 `ERR-PUBLISH-11`。）
    #       这与"入库后 status 被改成 NO_TORRENT_FILE 导致踢出队列"是**两件事**：
    #       后者的根因已修（`topup_opencd_fields.py` 现在会带回库里已有路径）。
    formatted_title = f"{core} {year} - {fmt} {track_type}"
    formatted_title = re.sub(r"\s+", " ", formatted_title).strip()

    # 副标题不在这里算：它依赖曲目/碟数，**还要按皇后/抓轨分流**，由 main() 拿到
    # tracklist/logs 后调 `descr.scan_small_descr()` 生成。
    # ★ 2026-09-26 订正：这里原先写「用 descr.build_small_descr 生成」—— 那句
    #   既是错的**叫法**（`build_small_descr` 只算曲目数、不看皇后），
    #   也是**真 bug 的出处**（每轮 scan 把皇后原文覆盖成「全 N 曲」）。
    #   ⇒ 别照着这行注释去"恢复"那个调用（`ERR-META-04`：改了行为要同时改文档）。
    #   ★ 渲染侧由 `build_form_fields()` 再按 `opencd_tid` 分流重算一次。
    return formatted_title, fmt, track_type


def _json_title(mapped_json):
    """从 `mapped_json` 快照里取皇后**原文标题**（`mapped.title`），取不到返回 `""`。

    ★ 单独抽出来是为了**只做这一件事**：不兜底、不猜、不退回 `torrent_name`。
      （`build_row` 写的 `formatted_title` = `m["title"]` ⇒ 这里取的是**同一个来源**，
      所以"不传这一列"与"写 `build_row` 的值"是**等价的**，不是"放弃了这一列"。）

    ★ 解析失败**不抛**：调用方要能区分"没快照"与"有快照但解析坏了"，
      所以这里把异常也收敛成 `""`，由调用方按 `mj` 是否为真来分因报出。
    """
    if not mapped_json:
        return ""
    try:
        m = json.loads(mapped_json)
    except (TypeError, ValueError):
        return ""
    if not isinstance(m, dict):
        return ""
    return str(m.get("title") or "").strip()


def _title_write_plan(oid, mapped_json, parsed_title):
    """★ C' 的核心判据（**纯函数 = 可脱库单测**）：决定这一行**写不写** `formatted_title`、写什么。

    返回 `(cols, src, why_missing)`：
      · `cols`        —— 要注入 `db.save_task(**cols)` 的字段（空 dict = **不写这一列**）
      · `src`         —— 人类可读的"来源"（打印用；**不拿 `cols` 的真假当判据**）
      · `why_missing` —— `None` = 正常；否则是"皇后路却拿不到原文"的**具体原因字符串**

    ★★ 三态（**这是本函数存在的全部理由**，别把它并回调用点）：
        抓轨路（无 oid）        → 写 `parsed_title`（`parse_title()` 的产物）
        皇后路 + 拿到原文        → 写**皇后原文**（`mapped_json.title`）
        皇后路 + 拿不到          → **不写**（保留库内原值），并回报原因

    ★★ 为什么必须是**三个互斥分支**而不是"一个 `None` 兼任两义"：
      2026-09-28 实测过第一版 —— 用 `mt_override = None` 表示"照旧写"，
      于是 `oid` 为空时保持 `None` ⇒ `cols = {}` ⇒ **抓轨路的标题从此不再被写**，
      而且**一声不响**（`ERR-META-14` 的形状）。三态显式化就是那一课的产物。
    """
    if not oid:
        return ({"formatted_title": parsed_title}, "抓轨路(parse_title)", None)

    title = _json_title(mapped_json)
    if title:
        return ({"formatted_title": title}, "皇后原文（本列不归 scan 管）", None)

    # ★ 两条子因的**处置完全不同** ⇒ 不能合并成一句"没拿到"：
    if not mapped_json:
        why = ("皇后路但库内无 `mapped_json` 快照 ⇒ **未写** `formatted_title`"
               "（不猜、不覆盖）。修法：跑 `crawl` 补快照，或 `opencd_ingest` 重导该行。")
    else:
        why = ("皇后路、有快照但 `mapped_json.title` 是空的 ⇒ **未写** `formatted_title`"
               "（不猜）。修法：核对皇后的标题字段是否被改名。")
    return ({}, "★ 未写（见下面的原因）", why)


def selftest():
    """C' 的**离线**自检（纯本地：不连 qB、不连库、不发请求）。返回 `(是否通过, 明细)`。

    ★ 为什么它必须进仓库、而不是留在我跑过的一次性脚本里：
      这条改动的判据（"三态各自写什么"）是**行为契约**，而行为契约活在聊天记录里
      就等于不存在（`ERR-META-01` 一族的形状：判据在哪儿，没人知道）。

    ★★ 阴性对照（**没有它，这个自检可能是"测了个寂寞"**）：
      每一条"期望不写"的用例，都配一条"期望真的写"的用例（抓轨路 / 皇后路有快照）。
      否则把 `_title_write_plan` 改成 `return ({}, "", None)` 也能全绿。
    """
    detail = []
    ok = [True]

    def chk(name, got, want):
        if got != want:
            ok[0] = False
            detail.append(f"✗ {name}\n     got = {got!r}\n     want= {want!r}")
        return got == want

    MJ = json.dumps({"title": "皇后原文 2026 - FLAC 整轨 - OpenCD"}, ensure_ascii=False)
    MJ_NOTITLE = json.dumps({"no_title_here": 1}, ensure_ascii=False)
    PARSED = "原始种子名 - FLAC 整轨"

    # ── ① 抓轨路：**必须写** `parse_title()` 的产物（返工前这里是 bug：写不进去）──
    cols, src, why = _title_write_plan(None, None, PARSED)
    chk("①抓轨路·cols", cols, {"formatted_title": PARSED})
    chk("①抓轨路·无缺失原因", why, None)

    # ── ② 皇后路 + 有快照：写**皇后原文**，不写自己拆的 ──
    cols, src, why = _title_write_plan("12345", MJ, PARSED)
    chk("②皇后路·有快照·cols", cols, {"formatted_title": "皇后原文 2026 - FLAC 整轨 - OpenCD"})
    chk("②皇后路·有快照·不写自家产物", cols.get("formatted_title") == PARSED, False)
    chk("②皇后路·有快照·无缺失原因", why, None)

    # ── ③ 皇后路 + **无快照**：不写，且给出原因 ──
    cols, src, why = _title_write_plan("12345", None, PARSED)
    chk("③皇后路·无快照·不写", cols, {})
    chk("③皇后路·无快照·有原因", why is not None and "无 `mapped_json`" in why, True)

    # ── ④ 皇后路 + **有快照但给不出 title** ⇒ 与③**同判据、不同原因** ──
    #   ★ 这里**只能**喂"解析得出、但里面没有 title"的快照。`""`/`None` 属于
    #     **"无快照"**（③），不是"有快照" —— 第一版把这两个混进来，于是期望值
    #     写错、自检报假红（`ERR-META-01` 的形状：判据错了，看着像代码错了）。
    for bad in (MJ_NOTITLE, json.dumps({"title": ""}), json.dumps({"title": "   "})):
        cols, src, why = _title_write_plan("12345", bad, PARSED)
        chk(f"④皇后路·快照给不出原文({bad[:22]!r})·不写", cols, {})
        chk(f"④皇后路·快照给不出原文({bad[:22]!r})·原因指向 title",
            why is not None and "`mapped_json.title`" in why, True)
    #   ★ 反向对照：`""`/`None` 必须落进**"无快照"**那条原因（否则③形同虚设）
    for nosnap in ("", None):
        cols, src, why = _title_write_plan("12345", nosnap, PARSED)
        chk(f"④反向·{nosnap!r} 算'无快照'", why is not None and "无 `mapped_json`" in why, True)

    # ── ⑤ 损坏的 JSON 不许抛异常，只许"拿不到" ──
    for junk in ("{不是json", "[1,2,3]", '"就是个字符串"', "123"):
        try:
            cols, src, why = _title_write_plan("12345", junk, PARSED)
        except Exception as e:                                  # noqa: BLE001
            ok[0] = False
            detail.append(f"✗ ⑤损坏快照 {junk!r} 抛了异常: {type(e).__name__}: {e}")
            continue
        chk(f"⑤损坏快照({junk!r})·不写且不抛", cols, {})
        # ★ `[1,2,3]` / `"字符串"` / `123` 是**合法 JSON 但不是 dict** ⇒ 也不同原因
        chk(f"⑤损坏快照({junk!r})·原因指向 title", why is not None and "`mapped_json.title`" in why, True)

    # ── ⑥ 快照里 title 是"数字/非字符串"不许冒充真标题（`ERR-META-14`）──
    cols, src, why = _title_write_plan("12345", json.dumps({"title": 0}), PARSED)
    chk("⑥快照 title=0 不许当原文", cols, {})

    # ── ⑦ ★ 结构性判据：两个 `save_task` 调用点都必须带 `**mt_col` ──
    #   （否则"算出来了却没注入"—— 就是返工前 B 用例那个 bug 的**另一种写法**）
    #
    #   ★★★ 判据必须走 **AST**，**不许** `"**mt_col" in 源码文本` ——
    #     第一版就是这么写的，实测**报假红**：真的注入只有 2 处在调里，
    #     但注释里也提到了 4 次 ⇒ 数出 **6 处**。
    #     这**正是** `assets.py:878` 那段注释警告过的同一个坑（子串搜索命中注释/
    #     文档串 ⇒ 判据是假的），我第一版又踩了一遍 ⇒ 所以这里落成 AST 判据。
    import ast as _ast
    _tree = _ast.parse(open(__file__, encoding="utf-8").read())

    def _starred_kwargs(fn_name):
        """数 `fn_name(...)` 调用里 `**xxx` 这种展开的 kwarg（只数真调用）。"""
        hits = []
        for node in _ast.walk(_tree):
            if (isinstance(node, _ast.Call)
                    and getattr(node.func, "attr", None) == fn_name):
                for k in node.keywords:
                    if k.arg is None and getattr(k.value, "id", "") == "mt_col":
                        hits.append(node.lineno)
        return hits

    _sites = _starred_kwargs("save_task")
    if len(_sites) != 2:
        ok[0] = False
        detail.append(f"✗ ⑦`**mt_col` 注入点应为 2 处（短路跳过支 + 普通写支），"
                      f"实测 {len(_sites)} 处（AST 判据，行号 {_sites}）")

    # ★★★ ⑦b **数据流**判据 —— 上面那条只查"`**mt_col` 这个符号出现了"，
    #   而符号出现 ≠ 值从 `_title_write_plan` 来。实测**两个变异漏网**：
    #     · 变异 4：`mt_col, ... = {}, 'x', None`（注入点还是 2 处、纯函数也没坏）
    #     · 变异 6：`mt_col, ... = {'formatted_title': formatted_title}, 'x', None`
    #       （= C' 被绕过，皇后路又写自家产物）
    #   ⇒ 必须断言**右值就是那个函数调用**，且**三个目标名/序都对**。
    #
    # ★★ 踩坑记录（本判据**第一版是恒红**的，别照抄那个写法）：
    #   `mt_col, mt_src, mt_why = ...` 在 AST 里是**一条** `Assign`，其 `targets`
    #   是**一个 `Tuple` 节点**（`Tuple.elts = [Name('mt_col'), …]`），
    #   **不是三个 `Name`**。第一版写 `[getattr(t,"id","") for t in n.targets]`
    #   ⇒ 拿到 `[""]` ⇒ `!= ["mt_col", …]` ⇒ **每次都 continue** ⇒ 找不到 ⇒ 恒红。
    #   实测（基线也报红）立刻暴露 —— 所以下面**两种形态都展开**。
    def _target_names(node):
        out = []
        for t in node.targets:
            if isinstance(t, _ast.Tuple):
                out += [getattr(e, "id", None) for e in t.elts]
            else:
                out.append(getattr(t, "id", None))
        return out

    _assign_ok = False
    for n in _ast.walk(_tree):
        if not isinstance(n, _ast.Assign):
            continue
        if _target_names(n) != ["mt_col", "mt_src", "mt_why"]:
            continue
        if isinstance(n.value, _ast.Call) and getattr(n.value.func, "id", "") == "_title_write_plan":
            _assign_ok = True
            break
    if not _assign_ok:
        ok[0] = False
        detail.append(
            "✗ ⑦b `mt_col, mt_src, mt_why` **不是** `_title_write_plan(...)` 的返回值"
            "（AST：找不到 `mt_col, mt_src, mt_why = _title_write_plan(...)` 这条赋值）。\n"
            "     ⇒ 这会让三态**算出但没接线**（变异 4/6 的形状）：注入点还在、纯函数也没坏，"
            "但库收到的是别的东西。★ 符号出现 ≠ 值来自这里。")

    # ★ 判"一变量两义"回归 —— 用 AST 查**赋值**，不看文本（注释里提到它不算数）
    _bad_assign = [n.lineno for n in _ast.walk(_tree)
                   if isinstance(n, _ast.Assign)
                   and "mt_override" in _target_names(n)]
    if _bad_assign:
        ok[0] = False
        detail.append(f"✗ ⑦第一版的 `mt_override`（一变量两义）又回来了（行 {_bad_assign}）"
                      " —— 它会静默吞掉抓轨路的标题，见 `_title_write_plan` 的 docstring")

    # ★ 判"抽出来了但没被调用"（定义了不算数）
    _calls = [n.lineno for n in _ast.walk(_tree)
              if isinstance(n, _ast.Call) and getattr(n.func, "id", "") == "_title_write_plan"]
    if len(_calls) < 1:
        ok[0] = False
        detail.append("✗ ⑦`_title_write_plan()` 定义了但**没有任何调用点** ⇒ 三态没生效")

    # ★★ ⑦c 判**两处 `save_task` 都在同一作用域**（防止"只改了短路支、忘了普通写支"）
    #   上面只数"总数 = 2"，所以把两处都塞进**同一个** `save_task` 调用里也能骗过去
    #   （`**mt_col, **mt_col` 会在运行时 TypeError，但那是运行期的事）。
    _scoped = {n.lineno for n in _ast.walk(_tree)
               if isinstance(n, _ast.Call) and getattr(n.func, "attr", None) == "save_task"}
    _lines = sorted(set(_sites))
    if len(_lines) != 2 or not all(any(abs(ln - sl) <= 12 for ln in _lines) for sl in _scoped):
        # ★ 只是**软判据**：两处必须在**不同**的 save_task 调用里。用行号邻近度近似，
        #   因为 `_ast` 不给"调用归属"，而这对本文件（间距 < 12 行）足够。
        _near = {}
        for ln in _lines:
            near = [sl for sl in _scoped if abs(ln - sl) <= 12]
            _near[ln] = near
        if len({tuple(v) for v in _near.values()}) < 2:
            ok[0] = False
            detail.append(f"✗ ⑦c 两处 `**mt_col` 疑似落在**同一个** `save_task` 调用里"
                          f"（近似归属 {_near}）⇒ 另一个调用点漏了注入")

    # ★★★ ⑦d 判 `mt_col` **不许被裸下标取键** —— `mt_col["…"]` 在"不写这一列"
    #   （= 空 dict）时抛 **KeyError** ⇒ **整个 scan 阶段崩掉**。
    #   实测（变异 X2）：打印的**取值**读 `mt_col`、**判据**读 `mt_src`，
    #   两者一旦不一致（如 `mt_src` 说"抓轨路"而 `mt_col` 是空的）⇒ 崩。
    #   ★ 生产里那不是"测试红"，是"一轮 scan 什么都不写"。
    #   ⇒ 只许 `.get('formatted_title', '(保留库内原值)')`。
    _bad_sub = [n.lineno for n in _ast.walk(_tree)
                if isinstance(n, _ast.Subscript)
                and getattr(n.value, "id", "") == "mt_col"]
    if _bad_sub:
        ok[0] = False
        detail.append(f"✗ ⑦d `mt_col` 被**裸下标**取键（行 {_bad_sub}）⇒ "
                      "`mt_col` 为空（= 不写这一列）时会 KeyError 崩掉整个 scan。"
                      "改用 `mt_col.get('formatted_title', '(保留库内原值)')`。")

    # ★★★ ⑦e 判两个 WHERE 常量**不许漂移**（2026-09-28 加）。
    #
    #   `db.QUEEN_PUBLISHED_WHERE`（C' 的判据口径）与 `db.CROSS_SITE_DEDUP_WHERE`
    #   （转种口径）**共用同一个前缀**（`site_status='EXISTS'` + id 非空），
    #   `QUEEN_PUBLISHED_WHERE` 只多出 `opencd_tid` 那一条。
    #   ⇒ 若哪天有人改了其中一个而没改另一个，两处就"各写一份、谁也不看着谁"
    #      —— 那正是 `ERR-META-03` 的形状。
    #
    #   ★ 判据（机械可判，不靠人读）—— ★★ 但**不能**按 " AND " 切了数个数：
    #     第一版就是这么写的，实测**当场红**，而`QUEEN`/`CROSS` 其实**没漂移**:
    #         QUEEN[:-1] = [... 'site_torrent_id != \'\'', 'opencd_tid IS NOT NULL']
    #         CROSS      = [... 'site_torrent_id != \'\'']
    #     ⇒ 因为 `opencd_tid IS NOT NULL AND opencd_tid != ''` 是**两条** AND 条件，
    #       "只多一条"这个说法是我**数的层级搞错了**（数的是"条件件数"，
    #       心里想的却是"多一个 opencd_tid 判据"）。
    #     ★ 判据改成**看字符串前缀**：`QUEEN` 必须以 `CROSS` **逐字开头**，
    #       且**多出来的尾巴**必须只谈 `opencd_tid`。这样与"多几条 AND"无关。
    #
    # ★★★ 2026-09-28 加：`db.TRANSFERABLE_WHERE`（转种实际口径）也要钉。
    #   ★ 为什么它是**独立一层**而不是把 `transfer_rule` 并进 `CROSS`：
    #     见 `db.TRANSFERABLE_WHERE` 上面的长注释 —— 并进去会让 `publish.py`
    #     那条"应当恒为空"的差集断言**恒空**（检测器静默失效）。
    #   ⇒ 判据：它必须以 `CROSS` **逐字开头**，且尾巴**正是** `transfer_rule='ALLOW'`。
    #
    #   ★★ 判据一律走**同一段代码**（一张"子常量 → 尾巴必须谈什么"的表）——
    #     不许为 `TRANSFERABLE` 复制一遍上面那三段 if：那就是"两处各写一份判据"，
    #     正是本条自检自己**要防**的那个形状。⇒ 用循环。
    _prefix_checks = [
        # (子常量名, 父常量名, 尾巴必须谈的词, 尾巴**不许**重复的词)
        ("QUEEN_PUBLISHED_WHERE", "CROSS_SITE_DEDUP_WHERE", "opencd_tid",
         ("site_status", "site_torrent_id")),
        ("TRANSFERABLE_WHERE", "CROSS_SITE_DEDUP_WHERE", "transfer_rule",
         ("site_status", "site_torrent_id", "opencd_tid")),
        # ★★★ 2026-10-04（#23 蟹黄堡发布队列）：`CRABPT_QUEUE_WHERE` 必须
        #   以 `TRANSFERABLE_WHERE` **逐字开头**，多出来的尾巴只谈 `status`。
        #   ★ 为什么是前缀判据而不是又一条 ⑦e-2 式的"逐字相等"：这里**允许**
        #     追加（`status != 'FAILED'` 正是要加的那条），只要求"加的内容是
        #     `status`、且不重复前缀里已有条件"—— 这正是 ⑦e 前缀族的设计。
        ("CRABPT_QUEUE_WHERE", "TRANSFERABLE_WHERE", "status",
         ("site_status", "site_torrent_id", "transfer_rule")),
    ]
    try:
        import db as _db
        for _kid, _dad, _must, _dup in _prefix_checks:
            _kv = getattr(_db, _kid, None)
            _dv = getattr(_db, _dad, None)
            if not isinstance(_kv, str) or not isinstance(_dv, str):
                ok[0] = False
                detail.append(f"✗ ⑦e `db.{_kid}` / `db.{_dad}` 不是字符串"
                              f"（拿到 {type(_kv).__name__} / {type(_dv).__name__}）"
                              f"⇒ 前缀判据**没测到东西**（ERR-META-14）")
                continue
            _tail = _kv[len(_dv):] if _kv.startswith(_dv) else None
            if _tail is None:
                ok[0] = False
                detail.append(f"✗ ⑦e `db.{_kid}` 不以 `db.{_dad}` **逐字开头**：\n"
                              f"          {_kid} = {_kv!r}\n"
                              f"          {_dad} = {_dv!r}\n"
                              f"     ⇒ 两个口径漂移了（ERR-META-03）。")
            elif _must not in _tail:
                ok[0] = False
                detail.append(f"✗ ⑦e `{_kid}` 比 `{_dad}` 多出来的尾巴不谈"
                              f" `{_must}`：{_tail!r}（这一层必须正是 `{_must}` 那条判据）")
            elif any(d in _tail for d in _dup):
                ok[0] = False
                detail.append(f"✗ ⑦e `{_kid}` 多出来的尾巴**重复**了前缀里已有的条件："
                              f"{_tail!r}（同一条判据写两遍 ⇒ 改一处漏一处）")
        # ★ ⑦e-2：`TRANSFERABLE_WHERE` 的尾巴必须**恰好**是闸门④ 那一条
        #   —— 光查"提到 transfer_rule"不够：`transfer_rule != 'DENY'`
        #   也会通过上面那条，而它与闸门④（只放行 `ALLOW`）**语义相反**
        #   （空 / NEED_CONFIRM 在闸门④ 是**拦**，在 `!= 'DENY'` 下是**放**）。
        #   ★ 这是本条自检最该防的形态：**判据提到了对的词，却问了个错的问题**。
        _t = getattr(_db, "TRANSFERABLE_WHERE", None)
        _c = getattr(_db, "CROSS_SITE_DEDUP_WHERE", None)
        if isinstance(_t, str) and isinstance(_c, str) and _t.startswith(_c):
            _tail2 = _t[len(_c):].strip()
            if _tail2 != "AND transfer_rule = 'ALLOW'":
                ok[0] = False
                detail.append(
                    f"✗ ⑦e-2 `db.TRANSFERABLE_WHERE` 的尾巴必须是**逐字**"
                    f" `AND transfer_rule = 'ALLOW'`（闸门④ 只放行 ALLOW），"
                    f"实测 = {_tail2!r}\n"
                    f"     ★ 注意：`transfer_rule != 'DENY'` 之类**也会**含"
                    f"`transfer_rule` 这个词而通过 ⑦e ⇒ 所以这一条必须**逐字**比。")

        # ★★★ ⑦e-3（2026-09-28 加）：**把"AST 能不能抽到这些常量"变成判据。**
        #
        #   ★ 起因（实测，就发生在加 `TRANSFERABLE_WHERE` 的当天）：
        #     本仓库有**三处**工具用「AST 抽取 `db.py` 里的常量字符串」这一招
        #     （`tools/fix_onsite_titles_and_subtitles.py`、`tools/fix_no_torrent_file_status.py`、
        #      `tools/_probe_a3_risk.py`、`tools/_probe_280_vs_16.py`），
        #     理由一致：**`import db` 会连 `config` 一起拉**，而 `config` 在 import
        #     期就 `require_env()`（`QB_PASS` 等）⇒ 没有 `.env` 的机器上连工具都起不来。
        #   ★★ 而 `TRANSFERABLE_WHERE` 的定义是
        #        `CROSS_SITE_DEDUP_WHERE + " AND transfer_rule = 'ALLOW'"`
        #      ⇒ `ast.literal_eval` **不认 `Name` 节点**，实测直接
        #        `ValueError: malformed node or string … Name(id='CROSS_SITE_DEDUP_WHERE')`。
        #   ⇒ 于是出现一个**很坏的组合**：常量在**运行时**（`import db`）完全可用，
        #     而在**静态抽取**时抽不到 ⇒ 坏掉的是**工具**，不是被改的功能。
        #     而工具坏的方式是"跑到那一步才 `ValueError`"（或 `assert` 失败），
        #     **不是构建期报错** —— 也就是说，它会在**半夜、在 NAS 上、在你要用它
        #     查一个紧急问题时**才炸。
        #   ★ 判据：**这几个常量的字面值必须能被 `ast.literal_eval` 直接解出来**
        #     （即：不许写成对另一个常量的引用拼接）。
        #     ⇒ 这样"谁能被静态抽"就有了一条**构建期的**判据，而不是靠每个工具的
        #       作者自己去踩。
        #   ★ 为什么**不是**"把所有工具改成能解引用"：那要改 4 个文件、且每个都会
        #     再长一段 `eval` 逻辑（`_probe_280_vs_16.py` 里那段就是旁证：它已经
        #     不得不自己实现一遍顺序求值）。**把复杂度摁在一个地方**（把字面值写全）
        #     比让它扩散到 4 个工具里更安全。
        #   ★★ 为什么**不**干脆用 `import db` 取常量：见上 —— `config` 的 import 期
        #      检查会让工具在没有 `.env` 的机器上起不来。这条**不是**风格问题。
        _literals = [
            "PENDING_PUBLISH_WHERE", "CROSS_SITE_DEDUP_WHERE",
            "QUEEN_PUBLISHED_WHERE", "TRANSFERABLE_WHERE",
            "CRABPT_QUEUE_WHERE",
            "STALE_CONDITION", "EDIT_RULE_GUARD",
        ]
        import ast as _ast2
        import pathlib as _pl
        _db_src = _pl.Path(__file__).resolve().parent / "db.py"
        if not _db_src.exists():
            ok[0] = False
            detail.append(f"✗ ⑦e-3 找不到 db.py（{_db_src}）⇒ 抽不到东西"
                          f"（这条判据**没测**，不是通过 —— ERR-META-14）")
        else:
            _assigns = {}
            for _n in _ast2.parse(_db_src.read_text(encoding="utf-8")).body:
                if isinstance(_n, _ast2.Assign):
                    for _tg in _n.targets:
                        if isinstance(_tg, _ast2.Name):
                            _assigns[_tg.id] = _n.value
            _unextractable = []
            for _nm in _literals:
                _node = _assigns.get(_nm)
                if _node is None:
                    _unextractable.append((_nm, "db.py 里找不到这个赋值"))
                    continue
                try:
                    _ast2.literal_eval(_node)
                except Exception as _e:                          # noqa: BLE001
                    _unextractable.append((_nm, f"{type(_e).__name__}: {str(_e)[:60]}"))
            if _unextractable:
                ok[0] = False
                detail.append(
                    "✗ ⑦e-3 这些常量**不能**被 `ast.literal_eval` 静态解出：\n"
                    + "\n".join(f"        · {n}: {why}" for n, why in _unextractable)
                    + "\n     ⇒ ★ 本仓库有 3~4 处**工具**正是靠 AST 抽 `db.py` 的常量"
                      "（因为 `import db` 会触发 `config` 的 import 期环境变量检查）。"
                      "常量一旦写成'对另一个常量的引用拼接'，那些工具就会在**运行时**"
                      "（而不是构建期）才炸。\n"
                      "     ★ 修法：**把字面值写全**（哪怕与前缀常量重复一段），"
                      "并在旁边注释里说明'重复是刻意的、理由是 ⑦e-3'。")
    except Exception as e:                                        # noqa: BLE001
        ok[0] = False
        detail.append(f"✗ ⑦e 读 `db` 的 WHERE 常量失败：{type(e).__name__}: {e}")

    # ★★★ ⑧（2026-10-04 加）：**qB 构造点必须唯一** —— `qb_client.py` 之外
    #   零 `Client(` 构造、零 `auth_log_in()`。判据走 **AST**（不看文本，见 ⑦ 的坑）。
    #
    #   ★ 起因（现场，`ERR-QB-08`）：qB 5.x 对**登录接口**做**失败计数**，
    #     而本项目原本有 **13 处**各自 `Client(host=…) … auth_log_in()` ——
    #     每起一个进程 / 每次 daemon 扫到待回加，都在**再登录一次**。
    #     于是"凭据一错"这个单点，被**重试**放大成"IP 被封"（`身份认证失败次数过多`，
    #     登录接口 HTTP 403）。⇒ **重试本身可以是故障的成因**。
    #   ⇒ 把构造收敛成**一个**工厂 `qb_client.build(config)`（key 优先 / 回退账号密码），
    #     本判据就是**不让"第 14 处"从复制粘贴里长回来**（那正是 13 处的病根）。
    #
    #   ★ 为什么是 AST 而不是 `"Client(" in 源码`：注释 / 文档串里提到它
    #     就会**假红**（行 245-248 记着第一版正是这么栽的）。符号出现 ≠ 真调用。
    #   ★★ 阴性对照（本判据**必须**能报红，否则是"测了个寂寞"）：
    #     临时在第 14 个文件里塞回一行裸 `Client(...)` 或 `auth_log_in()`，
    #     本自检**必须**红 —— 加判据那天手工验过一次（塞 → 红 → 撤）。
    #
    #   ★ 判据**扫全仓库**（不维护一张手写文件名列表）：列表会**漏掉**新加的文件，
    #     那正是"第 14 处"藏身的地方（`ERR-META-21` 的形状：在被筛过的集合上算）。
    #   ★ 但**跳过非源码目录**（`.git` / 数据 / 备份 / scratch / 缓存）——
    #     那些是**另一份**代码（可能是老版本），扫它们会报**假红**，
    #     而假红会逼着人把判据关掉（比没有判据更坏）。
    #
    #   ★★★ 判据必须**精确到"真的在构造一个 qB 客户端"**，否则会**大面积假红**。
    #     实测（加判据那天）第一版只查 `X.Client(...)` ⇒ 报了 **11 处**，
    #     其中**10 处是皇后站点的客户端**（`crawl.Client`，见 `crawl.py:145`，
    #     它**根本不用 qbittorrentapi**），只有那 1 处 `repos\Pt-Autoseed\…` 是
    #     另一个项目（Transmission）—— **一个真目标都没有**。
    #     ⇒ 假红的代价不是"多看几眼"，而是**判据会被关掉**（`ERR-META` 一族的形状）。
    #   ⇒ 收紧成**两条都必须成立**（AST 层面，不看文本）：
    #       ① 该文件**真的 import 了 `qbittorrentapi`**（`import qbittorrentapi` /
    #          `from qbittorrentapi import …`）；否则它的 `.Client(` 与 qB 无关。
    #       ② 命中的调用其**被调用者名**是那个 `qbittorrentapi` 模块别名下的 `Client`
    #          （如 `qbittorrentapi.Client(...)`，或 `from qbittorrentapi import Client`
    #           之后的裸 `Client(...)`）。 `crawl.Client` / `transmissionrpc.Client` 名字对不上。
    #     ★ `auth_log_in` 同理：只在该文件确实用 `qbittorrentapi` 时才算数。
    import pathlib as _pl2
    # ★★ 2026-10-06：源码已移进 `src/`，但本判据要扫的是**整个仓库**
    #   （⑧ 抓的是 `qb_client.py` 之外的裸构造点，⑧b 抓旧模块名 —— 两边都可能在
    #   `tools/` 里出现，例如 `tools/readd.py` 就真的 import qb_client）。
    #   ★ 若这里继续用 `Path(__file__).parent`（= `src/`），`rglob` 会**静默缩到
    #     `src/` 一棵子树**、`tools/` 整棵不再被审、**而判据照样显示绿** ——
    #     这是"判据测了个寂寞"（ERR-META-14 / ERR-META-21 一族），比报红坏得多。
    #   ⇒ 锚到**仓库根**：`src/run.py` 的上一级。
    #   ★ 兜底：容器里代码在 `/app` 根平铺（来源是 `COPY src/*.py ./`），此时
    #     `Path(__file__).parent` 就是仓库根；`MUSOPIA_REPO` 可显式覆盖（同
    #     `tools/nas_health.py` 的四级候选思路）。
    import os as _os3
    _HERE = _pl2.Path(__file__).resolve().parent
    _REPO_ENV = _os3.environ.get("MUSOPIA_REPO")
    if _REPO_ENV and _pl2.Path(_REPO_ENV).is_dir():
        _ROOT = _pl2.Path(_REPO_ENV).resolve()
    elif (_HERE / "tools").is_dir():
        _ROOT = _HERE                      # 容器内 /app：与 tools/ 同级
    else:
        _ROOT = _HERE.parent               # 本机：src/ 的上一级 = 仓库根
    # ★ 自检这条锚点本身：扫到的根必须能看见 `tools/` 与 `src/`（本机）或 `tools/`（容器）。
    #   看不见 ⇒ 说明 _ROOT 算错了，判据会静默缩范围 —— 当场报红，别让它伪装成通过。
    if not (_ROOT / "tools").is_dir():
        ok[0] = False
        detail.append(
            f"✗ ⑧ 扫描根算错：{_ROOT} 下没有 `tools/` ⇒ 本判据会**静默缩范围**"
            f"（ERR-META-14：没测到 = 不是通过）。检查 run.py 的 _ROOT 推导。")
    # ★ 要跳过的目录名（出现在路径的**任意层级**即跳过该文件）。
    #   ★★ 故意写得**宽**（宁可跳过也不误报）：判据的价值在"挡住新写的构造点"，
    #     不在"审第三方的 .venv / 别的项目源码"。
    _SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
                  ".scratch", ".deploy-backup", ".claude", ".mypy_cache",
                  ".pytest_cache", ".idea", ".vscode", "repos"}
    # ★ 根下这些**代际/备份**后缀的目录也跳过（DNA 是另一份快照，非本次改名范围）。
    _SKIP_DIR_SUFFIX = ("_bak", "_backup", "_old", "-bak", "-backup")
    # ★ 2026-10-06：工厂文件现在可能在 `src/`（本机）或 `/app` 根（容器）。
    #   两处都试；都找不到 ⇒ 下面 `if not _FACTORY.exists()` 报红（不会静默）。
    _FACTORY = next(
        (p for p in (_ROOT / "qb_client.py", _ROOT / "src" / "qb_client.py")
         if p.exists()),
        _ROOT / "qb_client.py")
    # ★ 命中一个就记 (相对路径, 行号, 类别)。类别分开报，便于一眼看出是哪种回归。
    _qbhits = []

    def _qb_import_info(tree):
        """返回 (是否有 `import qbittorrentapi`, 该模块的**本地别名**集合)。"""
        has = False
        mods = set()
        for n in _ast.walk(tree):
            if isinstance(n, _ast.Import):
                for a in n.names:
                    if a.name == "qbittorrentapi":
                        has = True
                        mods.add(a.asname or "qbittorrentapi")
            elif isinstance(n, _ast.ImportFrom) and n.module == "qbittorrentapi":
                has = True
        return has, mods

    def _qb_client_bare_names(tree):
        """`from qbittorrentapi import Client` ⇒ 裸 `Client(...)` 也是真构造。"""
        names = set()
        for n in _ast.walk(tree):
            if isinstance(n, _ast.ImportFrom) and n.module == "qbittorrentapi":
                for a in n.names:
                    names.add(a.asname or a.name)
        return names

    if not _FACTORY.exists():
        ok[0] = False
        detail.append(
            f"✗ ⑧ 找不到工厂 {_FACTORY.name} ⇒ 本判据**没测到东西**（ERR-META-14），"
            f"不是通过。⇒ 先建 `qb_client.py` 再指望这条护栏。")
    else:
        for _p in sorted(_ROOT.rglob("*.py")):
            if _p.resolve() == _FACTORY.resolve():
                continue                      # ★ 工厂自己**应当**有 `Client(` / `auth_log_in`
            _parts = set(_p.relative_to(_ROOT).parts)
            if _parts & _SKIP_DIRS:
                continue
            if any(part.endswith(_SKIP_DIR_SUFFIX) for part in _parts):
                continue
            try:
                _src = _p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue                      # 读不到 = 判据失效，**不并进"没有"**（ERR-SQL-05）
            try:
                _t2 = _ast.parse(_src)
            except SyntaxError:
                continue                      # 语法坏的文件由 py_compile 管，这里不重复报
            # ★★ 只有**真的用 qbittorrentapi** 的文件才算数（否则 `crawl.Client` 假红）
            _has_qbapi, _qbmods = _qb_import_info(_t2)
            if not _has_qbapi:
                continue
            _bare = _qb_client_bare_names(_t2)
            for _n in _ast.walk(_t2):
                if not isinstance(_n, _ast.Call):
                    continue
                _f = _n.func
                # (a) `qbittorrentapi.Client(...)` 形式的**真构造调用**
                if (getattr(_f, "attr", None) == "Client"
                        and getattr(getattr(_f, "value", None), "id", "") in _qbmods):
                    _qbhits.append((str(_p.relative_to(_ROOT)), _n.lineno, "qbittorrentapi.Client(...)"))
                # (a') `from qbittorrentapi import Client` 之后的**裸** `Client(...)`
                elif (getattr(_f, "id", None) == "Client" and "Client" in _bare):
                    _qbhits.append((str(_p.relative_to(_ROOT)), _n.lineno, "Client(...)  # 裸名（from qbittorrentapi）"))
                # (b) 任何 `.auth_log_in(...)` 调用（该文件确用 qbittorrentapi ⇒ 必是 qB 的）
                elif getattr(_f, "attr", None) == "auth_log_in":
                    _qbhits.append((str(_p.relative_to(_ROOT)), _n.lineno, "auth_log_in()"))
        if _qbhits:
            ok[0] = False
            _show = "\n".join(f"        · {f}:{ln}  {kind}" for f, ln, kind in _qbhits[:12])
            _more = f"\n        … 另有 {len(_qbhits) - 12} 处" if len(_qbhits) > 12 else ""
            detail.append(
                "✗ ⑧ qB 构造点**不唯一**：`qb_client.py` 之外还有 "
                f"**{len(_qbhits)} 处**直接构造 / 登录：\n{_show}{_more}\n"
                "     ⇒ ★ 这正是 `ERR-QB-08` 的病根：****每一处都自己登录一次****"
                "，凭据一错就被**重试**放大成 `IP 已被封禁`（qB 5.x 登录失败计数）。\n"
                "     ★ 修法：换成 `import qb_client` + `qb = qb_client.build(config)`"
                "（key 优先 / 回退账号密码），把构造收敛回那个**唯一**工厂。")

    # ★★★ ⑧b（2026-10-04 加）：**六个 opencd_* 旧模块名不许再被 import** ——
    #   它们已合并进 `opencd.py`（子命令 parse/rate/log/map/gate/ingest）。
    #   ★ 为什么需要这条：合并是**一次性**动作，但"从旧文件复制粘贴一行 import"
    #     会在**运行时**才炸（`ModuleNotFoundError: opencd_parse`），而那时可能
    #     在容器里跑了几轮 daemon 才有人看日志（`ERR-DOCKER` 一族：缺文件直到用才报）。
    #     ⇒ 把"合并到底做干净了没有"钉成一条**离线可跑**的判据。
    #   ★ 判据走 AST 的 `import` 节点（不看文本）：注释里提到旧名（本文件就到处是）
    #     不该算数 —— 同 ⑧ 的理由（文本匹配会假红，假红会逼人把判据关掉）。
    _OLD_OPENCD = {"opencd_parse", "opencd_rate", "opencd_log",
                   "opencd_map", "opencd_gate", "opencd_ingest"}
    _stale_imports = []

    def _imported_top_names(tree):
        names = set()
        for n in _ast.walk(tree):
            if isinstance(n, _ast.Import):
                for a in n.names:
                    names.add(a.name.split(".")[0])
            elif isinstance(n, _ast.ImportFrom) and n.module:
                names.add(n.module.split(".")[0])
        return names

    for _p in sorted(_ROOT.rglob("*.py")):
        _parts = set(_p.relative_to(_ROOT).parts)
        if _parts & _SKIP_DIRS:
            continue
        if any(part.endswith(_SKIP_DIR_SUFFIX) for part in _parts):
            continue
        try:
            _t3 = _ast.parse(_p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        _hitnames = _imported_top_names(_t3) & _OLD_OPENCD
        if _hitnames:
            _stale_imports.append((str(_p.relative_to(_ROOT)), sorted(_hitnames)))

    if _stale_imports:
        ok[0] = False
        _show2 = "\n".join(f"        · {f}: import {'/'.join(ns)}" for f, ns in _stale_imports[:12])
        detail.append(
            "✗ ⑧b 仍有文件 import **已合并**的旧模块名（`opencd_parse` 等六个）：\n"
            f"{_show2}\n"
            "     ⇒ 这些模块已并入 `opencd.py`。改成 `import opencd as <别名>`"
            "（或 `import opencd as P` 等，保留原别名，调用体不动）。\n"
            "     ★ 不改成 opencd ⇒ 运行到那一步才 `ModuleNotFoundError`。")

    # ★★★ ⑨（2026-10-07 加，#92 发种交棒）：**交棒判据 + 清盘护栏**的离线判据。
    #
    #   ★ 为什么必须有（`ERR-META-01`：失效的检测比不检测更危险）：
    #     `handoff.judge()` 的"出种≥N"口径**极易差一**（num_complete 含自己），
    #     而 `_safe_rmtree()` 的护栏一旦失效就是**删错目录**（`rm` 的边界是本流程
    #     唯一的不可逆动作）。这两点都不能靠"我读过代码"担保 ⇒ 落成可跑的判据。
    #
    #   ★★ 全部**纯本地**：`judge` / `pick_handoff_candidates` / `_safe_rmtree` 都是
    #     纯函数或只碰临时目录，不连 qB、不连库、不发请求。用一个临时 DATA_DIR + 临时
    #     PUBLISH 根来试 `_safe_rmtree`，试完清掉（不碰真实 `/publish`）。
    try:
        import tempfile as _tf
        import shutil as _sh
        import handoff as _ho

        # ── ⑨a 判据口径：**必须减掉自己**（num_complete 含本机做种者）──
        #   阴性对照：'只有自己做种'(num_complete=1) 必须**不**交棒。
        _NOW = 1_700_000_000.0
        _fresh = _NOW                                  # added_on = 现在 ⇒ 做种 0 天
        _old = _NOW - 4 * 86400                        # 做种 4 天 ⇒ 天数够

        # num_complete=1（只有自己）⇒ others=0 ⇒ 不够，且天数 0 ⇒ False（阴性对照）
        _r, _w = _ho.judge(1, _fresh, now=_NOW, seeders_min=3, seed_days=3)
        chk("⑨a 只自己做种(num_complete=1)·不交棒", _r, False)
        # num_complete=3（自己+2）⇒ others=2 < 3 ⇒ 不够（**这正是差一的坑**）
        _r, _w = _ho.judge(3, _fresh, now=_NOW, seeders_min=3, seed_days=3)
        chk("⑨a 自己+2(num_complete=3)·出种2<3·不交棒", _r, False)
        # num_complete=4（自己+3）⇒ others=3 ⇒ 够
        _r, _w = _ho.judge(4, _fresh, now=_NOW, seeders_min=3, seed_days=3)
        chk("⑨a 自己+3(num_complete=4)·出种3≥3·交棒", _r, True)
        # 天数够（4 天）但出种不够 ⇒ 仍然交棒（二选一）
        _r, _w = _ho.judge(1, _old, now=_NOW, seeders_min=3, seed_days=3)
        chk("⑨a 做种4天(出种不够)·天数够·交棒", _r, True)
        # 都不够 ⇒ False
        _r, _w = _ho.judge(2, _fresh, now=_NOW, seeders_min=3, seed_days=3)
        chk("⑨a 出种1<3 且 0天<3天·不交棒", _r, False)
        # added_on 缺失/坏值不许抛（走天数=0 分支）
        try:
            _r, _w = _ho.judge(1, None, now=_NOW, seeders_min=3, seed_days=3)
            chk("⑨a added_on=None·不抛且不交棒", _r, False)
        except Exception as _e:                                   # noqa: BLE001
            ok[0] = False
            detail.append(f"✗ ⑨a judge(added_on=None) 抛了异常: {type(_e).__name__}: {_e}")

        # ── ⑨b pick_handoff_candidates：只挑 progress=1 的，且判据口径一致 ──
        _ts = [
            {"hash": "aa", "progress": 1.0, "num_complete": 4, "added_on": _fresh},   # 够
            {"hash": "bb", "progress": 0.5, "num_complete": 9, "added_on": _old},     # 未下完 ⇒ 跳过
            {"hash": "cc", "progress": 1.0, "num_complete": 1, "added_on": _fresh},   # 不够
        ]
        _picked = _ho.pick_handoff_candidates(_ts, now=_NOW, seeders_min=3, seed_days=3)
        chk("⑨b 只挑够格的 1 条", len(_picked), 1)
        chk("⑨b 挑中的是 aa", _ho._torrent_field(_picked[0][0], "hash"), "aa")

        # ── ⑨c `_safe_rmtree` 护栏：**只许删 PUBLISH 根之下** ──
        #   ★★ 阴性对照（本条的**全部价值**）：给一个**根之外**的目录，
        #     `_safe_rmtree` 必须**拒删**且目录还在。否则"护栏"是假的。
        _tmp = _tf.mkdtemp(prefix="handoff-selftest-")
        _pub_root = os.path.join(_tmp, "publish")
        _outside = os.path.join(_tmp, "source_do_not_touch")
        os.makedirs(os.path.join(_pub_root, "albumA"))
        os.makedirs(_outside)
        with open(os.path.join(_outside, "keep.txt"), "w", encoding="utf-8") as _f:
            _f.write("original")
        _saved_pub = _ho.config.PUBLISH_CONTAINER_DIR
        try:
            _ho.config.PUBLISH_CONTAINER_DIR = _pub_root
            # ① 根之下的合法子目录 ⇒ 删掉
            _ok_sub = _ho._safe_rmtree(os.path.join(_pub_root, "albumA"))
            chk("⑨c 根之下子目录·删成功", _ok_sub, True)
            chk("⑨c 根之下子目录·真的没了",
                os.path.exists(os.path.join(_pub_root, "albumA")), False)
            # ② 根本身 ⇒ **拒**删（否则会清空整个发种区）
            _ok_root = _ho._safe_rmtree(_pub_root)
            chk("⑨c 发种区根本身·拒删", _ok_root, False)
            chk("⑨c 发种区根还在", os.path.isdir(_pub_root), True)
            # ③ 根**之外**的目录（阴性对照）⇒ **拒**删、文件原封不动
            _ok_out = _ho._safe_rmtree(_outside)
            chk("⑨c 根之外的目录·拒删（阴性对照）", _ok_out, False)
            chk("⑨c 根之外的目录·文件还在（没被误删）",
                os.path.exists(os.path.join(_outside, "keep.txt")), True)
        finally:
            _ho.config.PUBLISH_CONTAINER_DIR = _saved_pub
            _sh.rmtree(_tmp, ignore_errors=True)

        # ── ⑨d publish_target_dir 防 `..` 逃逸 ──
        _saved_pub2 = _ho.config.PUBLISH_CONTAINER_DIR
        try:
            _ho.config.PUBLISH_CONTAINER_DIR = _pub_root if os.path.isdir(_pub_root) else _tmp
            _root_pub = _ho.config.PUBLISH_CONTAINER_DIR
            _tgt = _ho.publish_target_dir("../../etc")
            chk("⑨d `..` 逃逸被 basename 挡住",
                _tgt, os.path.join(_root_pub, "etc"))
            _raised = False
            try:
                _ho.publish_target_dir("..")
            except ValueError:
                _raised = True
            chk("⑨d 专辑名='..' 直接拒（抛 ValueError）", _raised, True)
        finally:
            _ho.config.PUBLISH_CONTAINER_DIR = _saved_pub2
            _sh.rmtree(_tmp, ignore_errors=True)

        # ── ⑨e 队列幂等（同 infohash 不重复入队）——用临时 DATA_DIR ──
        _saved_data = _ho.config.DATA_DIR
        _saved_qf = _ho.QUEUE_FILE
        _tmp2 = _tf.mkdtemp(prefix="handoff-queue-selftest-")
        try:
            _ho.config.DATA_DIR = _tmp2
            _ho.QUEUE_FILE = os.path.join(_tmp2, "handoff_queue.json")
            _ho.enqueue("deadbeef", "n", "/publish/n", "/downloads/n", note="t")
            _ho.enqueue("deadbeef", "n", "/publish/n", "/downloads/n", note="t")  # 幂等
            chk("⑨e 同 infohash 入队两次·队列里只 1 条",
                len(_ho.items_by_state()), 1)
            _ho.update_item("deadbeef", state="HANDOFF_ADDED")
            chk("⑨e 状态推进生效",
                _ho.items_by_state("HANDOFF_ADDED")[0]["state"], "HANDOFF_ADDED")
            _ho.remove_item("deadbeef")
            chk("⑨e 移除后队列空", len(_ho.items_by_state()), 0)
        finally:
            _ho.config.DATA_DIR = _saved_data
            _ho.QUEUE_FILE = _saved_qf
            _sh.rmtree(_tmp2, ignore_errors=True)

        # ── ⑨g ★★★ 隔离闸门：只有"本项目音乐种"可交棒/清盘（用户 2026-10-07 指令）──
        #   ★★ 为什么必须有：用户明确「3020 上还会发别的种，不能被本项目误碰」
        #     ⇒ 判据必须能**说不**。阴性对照是这一条的全部价值（`ERR-META-01`）。
        _own = _ho.config.HANDOFF_OWN_CATEGORIES
        # 阳性：白名单内的分类 ⇒ True
        chk("⑨g Musopia 分类·认得（本项目）", _ho.is_own_project("Musopia"), True)
        chk("⑨g CrabPT 分类·认得（本项目）", _ho.is_own_project("CrabPT"), True)
        # 阴性对照①：未分类（用户手加的种常见）⇒ False
        chk("⑨g 空分类·不认（阴性对照）", _ho.is_own_project(""), False)
        # 阴性对照②：别的分类（用户在 3020 手发种）⇒ False
        chk("⑨g 其它分类·不认（阴性对照）", _ho.is_own_project("SomeOther"), False)
        # 阴性对照③：None ⇒ False（不许抛）
        try:
            chk("⑨g None 分类·不认且不抛", _ho.is_own_project(None), False)
        except Exception as _e:                                   # noqa: BLE001
            ok[0] = False
            detail.append(f"✗ ⑨g is_own_project(None) 抛了: {type(_e).__name__}: {_e}")
        # 白名单可注入（判据参数化生效）
        chk("⑨g 注入白名单 ['X']·X 认得", _ho.is_own_project("X", own_categories=["X"]), True)
        chk("⑨g 注入白名单 ['X']·Musopia 不认", _ho.is_own_project("Musopia", own_categories=["X"]), False)
        # ★ 结构性判据：handoff_to_3020 / finalize_source 必须**真的调用** is_own_project
        #   （否则"定义了闸门但没接" = ERR-ENV-03 形状：看着配好了，其实没用它）
        import ast as _ast9g
        import pathlib as _pl9g
        _ho_py = _pl9g.Path(__file__).resolve().parent / "handoff.py"
        _ho_src = _ho_py.read_text(encoding="utf-8")
        _ht = _ast9g.parse(_ho_src)

        def _uses_gate(func_name):
            """AST：func_name 的函数体里是否出现 `is_own_project(` 调用。"""
            for _fn in _ast9g.walk(_ht):
                if isinstance(_fn, _ast9g.FunctionDef) and _fn.name == func_name:
                    for _c in _ast9g.walk(_fn):
                        if (isinstance(_c, _ast9g.Call)
                                and isinstance(_c.func, _ast9g.Name)
                                and _c.func.id == "is_own_project"):
                            return True
            return False

        if not _uses_gate("handoff_to_3020"):
            ok[0] = False
            detail.append("✗ ⑨g handoff_to_3020 **没有**调用 is_own_project（闸门没接）")
        if not _uses_gate("finalize_source"):
            ok[0] = False
            detail.append("✗ ⑨g finalize_source **没有**调用 is_own_project（闸门没接）")

        # ── ⑨f 结构性判据：daemon 必须真的接了两个线程（否则"写了不接"）──
        #   照 ⑦ 的教训走 **AST**（不看文本：注释里提到 Thread 不算数）。
        #   ★ 2026-10-18（#16）：把 `_counts_thread_loop` 也纳进来 —— 它当初就是
        #     "写了没接"的形状（`ERR-META-01`）：函数定义好了、注释也写得很全，
        #     但 `run_main_loop()` 里没有 `Thread(target=...)` ⇒ 永远不跑、且**不报错**。
        #     ⇒ 接线断言必须覆盖**每一个**后台线程，不是"当初想到的那三个"。
        import ast as _ast9
        import pathlib as _pl9
        _WANT_THREADS = ("_readd_thread_loop", "_handoff_thread_loop",
                         "_seed_patrol_thread_loop", "_counts_thread_loop")
        _daemon_py = _pl9.Path(__file__).resolve().parent / "daemon.py"
        if not _daemon_py.exists():
            ok[0] = False
            detail.append(f"✗ ⑨f 找不到 daemon.py（{_daemon_py}）⇒ 判据没测到东西（ERR-META-14）")
        else:
            _dtree = _ast9.parse(_daemon_py.read_text(encoding="utf-8"))
            # 收集所有 `threading.Thread(target=X, ...)` 的 target 名
            _targets = set()
            for _n in _ast9.walk(_dtree):
                if (isinstance(_n, _ast.Call)
                        and getattr(_n.func, "attr", None) == "Thread"):
                    for _k in _n.keywords:
                        if _k.arg == "target" and isinstance(_k.value, _ast.Name):
                            _targets.add(_k.value.id)
            for _want in _WANT_THREADS:
                if _want not in _targets:
                    ok[0] = False
                    detail.append(f"✗ ⑨f daemon.py 没有 `Thread(target={_want}...)` 调用点 "
                                  f"⇒ 该线程**定义了但没接线**（实测 target={sorted(_targets)}）")
            # ★ 且这些函数真的**有定义**（防止 target 指向不存在的名字：只在运行期炸）
            _defs = {_n.name for _n in _ast9.walk(_dtree)
                     if isinstance(_n, _ast.FunctionDef)}
            for _want in _WANT_THREADS:
                if _want not in _defs:
                    ok[0] = False
                    detail.append(f"✗ ⑨f `{_want}` 在 daemon.py 里**没有定义** "
                                  f"（接线会 NameError）")

        # ── ⑨h ★★★ #134：数据没就位 ⇒ 绝不回加 3040（两个实测种的病根）──
        #   ★★ 为什么必须有（本条的全部价值 = **阴性对照**）：
        #     实测踩到：SSD 发种区**没有** `☆ac8.13f` 目录，任务却推上 3040 变 `stalledDL`；
        #     `Beyond` 复制与回加并发 ⇒ qB 在**半截数据**上校验 ⇒ progress=1.4% 变下载。
        #     根因是 `begin_handoff_for_task` 不核对复制结果、调用方丢弃返回值。
        #   ⇒ 判据：`verify_publish_area_complete` 必须能**逐字节**判"没到位"，
        #     且 `begin_handoff_for_task` 在**复制不完整**时必须返回 False（拒回加）。
        _tmp3 = _tf.mkdtemp(prefix="handoff-134-selftest-")
        _pub_root3 = os.path.join(_tmp3, "publish")
        _saved_pub3 = _ho.config.PUBLISH_CONTAINER_DIR
        _saved_handoff3 = _ho.config.ENABLE_HANDOFF
        _saved_dry3 = _ho.config.HANDOFF_DRY_RUN
        # ★★★ 副作用隔离（2026-10-07 实测踩到）：⑨h 会真的调 `begin_handoff_for_task`
        #   ⇒ 它会 `enqueue` ⇒ 写 **真** 的 `/data/handoff_queue.json`！本机跑一次自检，
        #   就把测试种（`cafebabe`/`deadbeef`）塞进**生产队列**、daemon 还会把它们标 FAILED
        #   （NAS 实测：`data/handoff_queue.json` 里多了一条 `cafebabe AlbumY FAILED`）。
        #   ⇒ 自检**绝不许**碰真实状态（`ERR-META-14`：测试不许有真副作用）。把队列文件
        #   重定向到临时目录，跑完恢复。
        _saved_qf3 = _ho.QUEUE_FILE
        try:
            _ho.QUEUE_FILE = os.path.join(_tmp3, "handoff_queue.json")
            _ho.config.PUBLISH_CONTAINER_DIR = _pub_root3
            _ho.config.ENABLE_HANDOFF = "true"
            # ★ 关 dry-run：本节的判据是"复制不完整必须拒回加"，走的是**真实分组逻辑**
            #   （dry-run 下不落盘、不核对，属另一条分支）。
            _ho.config.HANDOFF_DRY_RUN = "false"
            # 源成品（HDD 侧）：两文件
            _src = os.path.join(_tmp3, "hdd", "AlbumX")
            os.makedirs(_src)
            with open(os.path.join(_src, "a.wav"), "wb") as _f:
                _f.write(b"A" * 1000)
            with open(os.path.join(_src, "b.cue"), "wb") as _f:
                _f.write(b"B" * 50)

            # ① 发种区**空**（= `☆ac8.13f` 的实况）⇒ 必须判"未到位"
            _okc, _whyc = _ho.verify_publish_area_complete(_src, "AlbumX")
            chk("⑨h 发种区无目录·判未就位（阴性对照）", _okc, False)

            # ② 发种区**半截**（只拷了 1 个文件 / 字节不符）⇒ 必须判"未到位"
            _dst = os.path.join(_pub_root3, "AlbumX")
            os.makedirs(_dst)
            with open(os.path.join(_dst, "a.wav"), "wb") as _f:
                _f.write(b"A" * 500)                 # ★ 半截：只有 500/1000
            _okc, _whyc = _ho.verify_publish_area_complete(_src, "AlbumX")
            chk("⑨h 发种区半截(a.wav 500/1000)·判未就位（阴性对照）", _okc, False)

            # ③ 发种区**完整**（两文件、字节全对）⇒ 判"已就位"
            with open(os.path.join(_dst, "a.wav"), "wb") as _f:
                _f.write(b"A" * 1000)
            with open(os.path.join(_dst, "b.cue"), "wb") as _f:
                _f.write(b"B" * 50)
            _okc, _whyc = _ho.verify_publish_area_complete(_src, "AlbumX")
            chk("⑨h 发种区完整·判已就位", _okc, True)

            # ④ ★★ 关键闸门：复制**不完整**时 `begin_handoff_for_task` 必须返回 False
            #   —— 这是让 publish 侧"拒绝回加"的依据。
            #   构造：源存在但发种区被清空 ⇒ 复制会重做……故直接测"源不存在"这一必然失败态。
            _okb, _whyb = _ho.begin_handoff_for_task(
                os.path.join(_tmp3, "hdd", "NoSuchAlbum"), "NoSuchAlbum", "feedbeef",
                source_category="Musopia", target_category="Musopia")
            chk("⑨h 源不存在·begin_handoff 返回 False（拒回加，阴性对照）", _okb, False)

            # ⑤ ★★★ 核心回归（就是 `Beyond` 的病根）：发种区**已有目录、但内容半截**
            #   （qB 边下边写留下的），**旧代码**用"目录非空"当"已复制"⇒ 跳过复制 ⇒
            #   坏数据留在那 ⇒ 变下载。**新代码**必须**补齐**（copytree 覆盖）并核对通过。
            #   ★ 判据 = 复制后逐字节完整（True），且**发种区那份被真正改写**（不是跳过）。
            _src2 = os.path.join(_tmp3, "hdd", "AlbumY")
            os.makedirs(_src2)
            with open(os.path.join(_src2, "x.flac"), "wb") as _f:
                _f.write(b"X" * 4096)
            _dst2 = os.path.join(_pub_root3, "AlbumY")
            os.makedirs(_dst2)
            with open(os.path.join(_dst2, "x.flac"), "wb") as _f:
                _f.write(b"X" * 1024)                 # 半截：目录非空，内容不全
            _okb, _whyb = _ho.begin_handoff_for_task(
                _src2, "AlbumY", "cafebabe",
                source_category="Musopia", target_category="Musopia")
            chk("⑨h 半截副本(目录非空)·被**补齐**并判已就位",
                _okb, True)
            # ★ 强判据：发种区那份 x.flac 必须已被补齐到 4096（证明**真重拷**、没"非空跳过"）。
            _dst2_sz = os.path.getsize(os.path.join(_dst2, "x.flac")) if os.path.exists(
                os.path.join(_dst2, "x.flac")) else -1
            chk("⑨h 半截副本被补齐到源大小(4096)", _dst2_sz, 4096)
            # ★★★ 回归护栏：源码里**不许**再出现"目录非空 ⇒ 跳过复制"的旧判据。
            import pathlib as _pl9h
            _ho_py = _pl9h.Path(__file__).resolve().parent / "handoff.py"
            _ho_src = _ho_py.read_text(encoding="utf-8") if _ho_py.exists() else ""
            if "os.listdir(dst)" in _ho_src and "跳过复制" in _ho_src:
                ok[0] = False
                detail.append("✗ ⑨h handoff.py 仍是旧判据「目录非空 ⇒ 跳过复制」"
                              "（`Beyond` 的病根，ERR-PUBLISH-06）")

            # ── ⑨h-2 ★★★（S6 / ERR-PUBLISH-23）逐片校验：文件数+字节数全对，仍必须能报错 ──
            #   ★★ 为什么要这一节：`21811` 实测 —— 站点种 44 文件、盘上 44 文件、
            #     **逐文件字节数一个不差**、`du -sb` 两边同为 `954748123`，
            #     而按 torrent 自己的顺序逐片校验是 **ok=49 / bad=407**。
            #     ⇒ "文件数+字节数"是**必要条件不是充分条件**；只有逐片比 hash 才够。
            #   ★★★ 阴性对照的构造（缺了它本节就是"测了个寂寞"）：
            #     造一个**两个文件**的样本，用 A|B 的顺序制种，
            #     再把发种区写成 **B|A** —— 文件集合、每个文件字节数**全等**，
            #     只有**拼接顺序**不同 ⇒ 一级判据必绿、二级判据**必红**。
            #     这正是在验证"一级判据挡不住的东西，二级挡得住"。
            import torf as _torf9h
            _pp = os.path.join(_tmp3, "pieces")
            _srcP = os.path.join(_pp, "hdd", "AlbumP")
            os.makedirs(_srcP)
            # ★★ 两个文件的内容**必须真的不同**（不能用 `A*N` / `B*N` 里的 `A`/`B`
            #   这种单字节重复 —— 那样"对调"之后整体字节流几乎一样，阴性对照会**假绿**）。
            #   实测踩到一次：`b"A"*20480` 与 `b"B"*20480` 对调后分片 hash 竟然一致。
            _A = bytes(range(256)) * 80        # 20 KiB，逐字节各不同
            _B = bytes(reversed(range(256))) * 80  # 20 KiB，与 A 逐字节不同
            with open(os.path.join(_srcP, "a.bin"), "wb") as _f:
                _f.write(_A)
            with open(os.path.join(_srcP, "b.bin"), "wb") as _f:
                _f.write(_B)
            # 制种（★ 片长必须是 16 KiB 的倍数 —— `torf` 会抛 `PieceSizeError`。
            #   16 KiB ⇒ 2000 字节只有 1 片，跨不了文件 ⇒ 样本要更大才能验"跨文件拼接"。
            #   ⇒ 用 16 KiB 片长 + 每文件 20 KiB（共 40 KiB = 3 片，第 2 片横跨两文件）。
            _tP = _torf9h.Torrent(path=_srcP, trackers=["http://x/announce"],
                                  private=True, piece_size=16384)
            _tP.generate()
            _torfP = os.path.join(_pp, "AlbumP.torrent")
            _tP.write(_torfP)

            # ① 阳性对照：发种区**就是**那份数据（顺序正确）⇒ 逐片必须通过
            _dstP = os.path.join(_pub_root3, "AlbumP")
            os.makedirs(_dstP)
            for _n in ("a.bin", "b.bin"):
                with open(os.path.join(_srcP, _n), "rb") as _s, \
                        open(os.path.join(_dstP, _n), "wb") as _d:
                    _d.write(_s.read())
            _ok1, _why1 = _ho.verify_publish_area_complete(_srcP, "AlbumP",
                                                           torrent_file=_torfP)
            chk("⑨h-2 顺序正确的副本·逐片校验通过", _ok1, True)

            # ② 阴性对照（本节的核心）：**文件集合与字节数全等**，只有**内容/顺序不同**
            #    ⇒ 一级判据（文件数+总字节）必绿；二级（逐片）**必红**。
            #   ★★ 关键：`verify_pieces` 按 torrent 的 `info['name']`（= `AlbumP`）去找目录
            #     ⇒ 必须**就地**把 `_dstP` 里的内容换成对调版（另建 `AlbumQ` 没用 —— 它不在
            #     torrent 的路径上，测的还是 `AlbumP`，于是阴性对照**假绿**。实测踩到）。
            _orig_a = open(os.path.join(_dstP, "a.bin"), "rb").read()
            _orig_b = open(os.path.join(_dstP, "b.bin"), "rb").read()
            with open(os.path.join(_dstP, "a.bin"), "wb") as _f:
                _f.write(_orig_b)          # a ← 原 b 的内容
            with open(os.path.join(_dstP, "b.bin"), "wb") as _f:
                _f.write(_orig_a)          # b ← 原 a 的内容
            _lvl1_bytes = _ho._dir_total_bytes(_dstP)
            chk("⑨h-2 对调样本·字节数与源**相同**（一级判据挡不住，前提成立）",
                _lvl1_bytes, _ho._dir_total_bytes(_srcP))
            _okq, _whyq = _ho.verify_pieces(os.path.dirname(_dstP),
                                            open(_torfP, "rb").read())
            chk("⑨h-2 ★对调样本（字节数全等）·逐片校验**必须报错**（阴性对照）", _okq, False)
            if _okq:
                detail.append("✗ ⑨h-2 逐片校验对'同字节数、不同内容/顺序'的样本判了通过 "
                              "⇒ 它退化成了'文件数+字节数'，ERR-PUBLISH-23 的坑重开")

            # ③ 缺一个文件 ⇒ 逐片必须报错（且理由是"缺文件"，不是"读不出"）
            os.remove(os.path.join(_dstP, "b.bin"))
            _okr, _whyr = _ho.verify_pieces(os.path.dirname(_dstP),
                                            open(_torfP, "rb").read())
            chk("⑨h-2 缺文件·逐片校验报错", _okr, False)

            # ④ 恢复正确内容后，用 max_pieces 限流：理由里必须带"前 N 片"
            with open(os.path.join(_dstP, "a.bin"), "wb") as _f:
                _f.write(_orig_a)
            with open(os.path.join(_dstP, "b.bin"), "wb") as _f:
                _f.write(_orig_b)
            _okn, _whyn = _ho.verify_pieces(os.path.dirname(_dstP),
                                            open(_torfP, "rb").read(), max_pieces=2)
            chk("⑨h-2 max_pieces=2 时理由写明'前 2 片'", "前 2 片" in _whyn, True)

        finally:
            _ho.config.PUBLISH_CONTAINER_DIR = _saved_pub3
            _ho.config.ENABLE_HANDOFF = _saved_handoff3
            _ho.config.HANDOFF_DRY_RUN = _saved_dry3
            _ho.QUEUE_FILE = _saved_qf3
            _sh.rmtree(_tmp3, ignore_errors=True)

        # ── ⑨i ★★★ #134：回加复核判据必须能对"下载中"报红 ──
        #   ★★ 为什么：原判据 `state != "missingFiles"` 把 `stalledDL/progress=0`
        #     （实测的 `☆ac8.13f`）当**成功** ⇒ 静默不做种（`ERR-META-01` 形状）。
        #   ★ 判据 = **旧的坏写法必须消失**（回归护栏：谁把它改回去就报红）+
        #     两条路径都接了下载族判断（乌托邦 + 蟹黄堡）。
        import pathlib as _pl9i
        _pub_py = _pl9i.Path(__file__).resolve().parent / "publish.py"
        _pub_src = _pub_py.read_text(encoding="utf-8") if _pub_py.exists() else ""
        if not _pub_src:
            ok[0] = False
            detail.append(f"✗ ⑨i 找不到 publish.py（{_pub_py}）⇒ 判据没测到东西（ERR-META-14）")
        # 旧坏写法：把"非 missingFiles"当成功（本条回归护栏的核心）
        if 'ok = t.state != "missingFiles"' in _pub_src:
            ok[0] = False
            detail.append("✗ ⑨i publish.py 回加复核**仍是旧判据**"
                          "（`ok = t.state != \"missingFiles\"`）⇒ 会放过 stalledDL（#134）")
        # 下载族判据必须两条路径都有（乌托邦 + 蟹黄堡）
        if _pub_src.count("_dl_states = {") < 2:
            ok[0] = False
            detail.append(f"✗ ⑨i 下载族判据 `_dl_states = {{` 出现 "
                          f"{_pub_src.count('_dl_states = {')} 处（应 ≥2：乌托邦+蟹黄堡两条路）")
        # progress 门槛必须接上
        if _pub_src.count("and prog >= 1.0") < 2:
            ok[0] = False
            detail.append("✗ ⑨i 回加复核缺少 `progress >= 1.0` 门槛（应两条路径都有）")

        # ── ⑨j ★★★ B2：**"漏做种"必须入账**（不许静默跳过）──
        #   ★★ 为什么：`not _ok` / 拿不到源 / 复制抛异常 ⇒ `return`（正确地拒回加），
        #     但**只 print 一行**（日志被重启冲掉就没了）⇒ "已上站却不会做种"**静默消失**。
        #     这正是「#134 五条坏种」被发现前**无任何账**可查的原因。
        #   ★ 判据 = 两站的 3 个跳过点（× 2 站 = 6 处）都必须调 `record_missing_data`，
        #     且账本函数本身必须在 `handoff.py` 里存在（否则接线是"接了个空"）。
        if _pub_src.count("record_missing_data(") < 6:
            ok[0] = False
            detail.append(f"✗ ⑨j publish.py 里 `record_missing_data(` 只出现 "
                          f"{_pub_src.count('record_missing_data(')} 处（应 ≥6："
                          f"乌托邦+蟹黄堡 各 3 个跳过点）⇒ 漏做种会**静默**")
        # 两站都要有（防"只接了一半"）
        if _pub_src.count('handoff.record_missing_data(info_hash, album, "musopia"') < 3:
            ok[0] = False
            detail.append("✗ ⑨j 乌托邦路跳过点未全部入账（应 3 处：未就位/无源/异常）")
        if _pub_src.count('handoff.record_missing_data(info_hash, album, "crabpt"') < 3:
            ok[0] = False
            detail.append("✗ ⑨j 蟹黄堡路跳过点未全部入账（应 3 处：未就位/无源/异常）")
        # 账本函数必须真存在于 handoff.py（防"接线接了个空引用"）
        _ho_py = _pl9i.Path(__file__).resolve().parent / "handoff.py"
        _ho_src = _ho_py.read_text(encoding="utf-8") if _ho_py.exists() else ""
        if "def record_missing_data(" not in _ho_src:
            ok[0] = False
            detail.append("✗ ⑨j handoff.py 里没有 `def record_missing_data(` ⇒ 接线是空引用")

        # ── ⑨k ★★★ #136（2026-10-07 `217848` 实测）：**FAILED 条目绝不许直接清盘** ──
        #   ★★ 为什么：`process_one` 原代码的交棒块只认 `("PENDING","HANDOFF_ADDED")`；
        #     一条 `FAILED` 条目会**跳过交棒**、直落 `verify_3020_seeding` + `finalize_source`。
        #     若此刻 3020 恰好已有该种在做种（辅种工具抢先加了）⇒ verify 判就绪 ⇒
        #     `finalize_source` 把**3040 还没回加、发种区副本还在用**的 SSD 目录**删掉**
        #     ⇒ 15 分钟后的延迟回加指向一个已删目录 ⇒ 变下载（`queuedDL progress=0`）。
        #   ★ 判据（阴性对照，必须能对**旧行为**报红）：
        #     构造 `state=FAILED` 的队列条目 + SSD 副本**确实在位** + 假 qb_dst 报"3020 在做种"
        #     + 假 qb_src 报"3040 找不到该种"（= 回加还没发生）。调 `process_one` 后断言：
        #       (a) **SSD 副本仍在**（没被 `_safe_rmtree` 删）—— 这是核心；
        #       (b) 结果**不是 DONE**（没被误判成功）；
        #       (c) 条目**留在队列里**（可重试，不是被 remove_item 抹掉）。
        _tmp4 = _tf.mkdtemp(prefix="handoff-136-selftest-")
        _pub_root4 = os.path.join(_tmp4, "publish")
        _saved_pub4 = _ho.config.PUBLISH_CONTAINER_DIR
        _saved_qf4 = _ho.QUEUE_FILE
        _saved_h2 = _ho.handoff_to_3020
        _saved_v2 = _ho.verify_3020_seeding
        try:
            _ho.QUEUE_FILE = os.path.join(_tmp4, "handoff_queue.json")
            _ho.config.PUBLISH_CONTAINER_DIR = _pub_root4
            # 成品源（HDD 侧）+ 发种区副本，**逐字节完整**（所以"发种区副本在位"成立）。
            _src4 = os.path.join(_tmp4, "hdd", "AlbumZ")
            os.makedirs(_src4)
            with open(os.path.join(_src4, "z.flac"), "wb") as _f:
                _f.write(b"Z" * 2048)
            _dst4 = os.path.join(_pub_root4, "AlbumZ")
            os.makedirs(_dst4)
            with open(os.path.join(_dst4, "z.flac"), "wb") as _f:
                _f.write(b"Z" * 2048)
            # 入队 + 人为把它置成 FAILED（模拟"上一轮某步没过"）。
            _ho.enqueue("beefcafe", "AlbumZ", _dst4, "/downloads", state="PENDING",
                        category="CrabPT", target_category="CrabPT",
                        src_path=_src4, album="AlbumZ")
            _ho.update_item("beefcafe", state="FAILED", last_error="上一轮失败")

            # 假 qb_dst：3020 **已有该种在做种**（触发旧代码的 finalize）。
            class _FakeDst:
                def torrents_info(self, **kw):
                    return [{"hash": "beefcafe", "progress": 1.0, "state": "uploading"}]
                def torrents_add(self, **kw):
                    return None
                def torrents_recheck(self, **kw):
                    return None

            # 假 qb_src：3040 **找不到该种**（= 回加还没发生）⇒ `finalize_source` 的
            #   隔离闸门拿 "找不到该种"（旧代码把它当"已删⇒安全可清"⇒ 删盘！）。
            class _FakeSrc:
                def torrents_info(self, **kw):
                    return []
                def torrents_delete(self, **kw):
                    return None

            # ★ 忠实复刻 `217848` 现场：19:02 交棒**失败**（`PENDING → FAILED`），
            #   19:07 重试时 state 已是 FAILED。**旧代码**在 FAILED 这一步**跳过交棒**、
            #   直落 verify+finalize；**新代码**必须让 FAILED **重走交棒**——交棒若**再失败**
            #   ⇒ 提前 return FAILED（不落 finalize）。故 mock `handoff_to_3020` **失败**。
            _ho.handoff_to_3020 = lambda *a, **k: (False, "fake 交棒失败（复刻 217848）")
            _ho.verify_3020_seeding = lambda *a, **k: (True, "fake 3020 在做种")

            _after4 = _ho.process_one(_FakeSrc(), _FakeDst(),
                                      {"infohash": "beefcafe", "state": "FAILED",
                                       "save_path": _dst4, "target_save_path": "/downloads",
                                       "target_category": "CrabPT",
                                       "src_path": _src4, "album": "AlbumZ"},
                                      dry_run=False)
            # (a) ★ 核心：SSD 副本**必须还在**（旧代码此处在 finalize 里把它删了）。
            _z_still = os.path.exists(os.path.join(_dst4, "z.flac"))
            chk("⑨k FAILED 条目·SSD 副本未被误删（核心，217848 病根）", _z_still, True)
            # (b) 不得被判成 DONE（旧代码返回 DONE）。
            chk("⑨k FAILED 条目·结果不是 DONE", _after4 != "DONE", True)
            # (c) 条目要留下（可重试），不许被 remove_item 抹掉。
            _still_q = any(it.get("infohash") == "beefcafe" for it in _ho.items_by_state())
            chk("⑨k FAILED 条目·仍在队列（可重试）", _still_q, True)
        finally:
            _ho.handoff_to_3020 = _saved_h2
            _ho.verify_3020_seeding = _saved_v2
            _ho.config.PUBLISH_CONTAINER_DIR = _saved_pub4
            _ho.QUEUE_FILE = _saved_qf4
            _sh.rmtree(_tmp4, ignore_errors=True)

        # ── ⑨m ★★★ #136（2026-10-07 `20639` 实测）：**延迟回加前必须核对发种区就位** ──
        #   ★★ 为什么：延迟回加（`daemon._drain_pending_readds`）是与 publish 同步回加
        #     并列的**另一条异步路径**，原先直接 `torrents_add` ⇒ 数据缺失也照推 3040
        #     ⇒ 变下载（`20639` 形状）。修法 = 抽出纯函数 `daemon._readd_area_ready(item)`，
        #     回加前据此裁决。判据分两条：
        #       (a) 行为判据（纯本地）：带 src_path/album 且发种区**完整** ⇒ True；
        #           发种区**空** ⇒ False（阴性对照）；拿不到任何路径 ⇒ False。
        #       (b) 源码护栏：`_drain_pending_readds` 里 `torrents_add` 之前**必须**出现
        #           `_readd_area_ready(`（防有人把闸门拆了 ⇒ 回归到"直接推"）。
        try:
            import importlib as _il9m
            _dmn = _il9m.import_module("daemon")
            _tmp5 = _tf.mkdtemp(prefix="readd-136-selftest-")
            _pub_root5 = os.path.join(_tmp5, "publish")
            _saved_pub5 = _ho.config.PUBLISH_CONTAINER_DIR
            try:
                _ho.config.PUBLISH_CONTAINER_DIR = _pub_root5
                _src5 = os.path.join(_tmp5, "hdd", "AlbumR")
                os.makedirs(_src5)
                with open(os.path.join(_src5, "r.flac"), "wb") as _f:
                    _f.write(b"R" * 1024)
                # ① 发种区**空** ⇒ 判不就位（阴性对照 —— 这就是 20639 的现场）
                _r5a, _ = _dmn._readd_area_ready(
                    {"src_path": _src5, "album": "AlbumR", "save_path": "/downloads"})
                chk("⑨m 延迟回加·发种区空 ⇒ 判不就位（拒回加，阴性对照）", _r5a, False)
                # ② 发种区**完整** ⇒ 判就位
                _dst5 = os.path.join(_pub_root5, "AlbumR")
                os.makedirs(_dst5)
                with open(os.path.join(_dst5, "r.flac"), "wb") as _f:
                    _f.write(b"R" * 1024)
                _r5b, _ = _dmn._readd_area_ready(
                    {"src_path": _src5, "album": "AlbumR", "save_path": "/downloads"})
                chk("⑨m 延迟回加·发种区完整 ⇒ 判就位", _r5b, True)
                # ③ 老条目（无 src_path/album）+ save_path 目录存在非空 ⇒ 兜底判就位
                _r5c, _ = _dmn._readd_area_ready({"save_path": _dst5})
                chk("⑨m 老条目·save_path 目录非空 ⇒ 兜底判就位", _r5c, True)
                # ④ 啥都拿不到 ⇒ 判不就位（绝不因缺字段放行）
                _r5d, _ = _dmn._readd_area_ready({})
                chk("⑨m 无任何路径 ⇒ 判不就位（不放行）", _r5d, False)
            finally:
                _ho.config.PUBLISH_CONTAINER_DIR = _saved_pub5
                _sh.rmtree(_tmp5, ignore_errors=True)
            # ⑤ 源码护栏：`torrents_add` 必须被 `_readd_area_ready` 挡在前面
            _dmn_src = (_pl9i.Path(__file__).resolve().parent / "daemon.py").read_text(
                encoding="utf-8") if (_pl9i.Path(__file__).resolve().parent
                                      / "daemon.py").exists() else ""
            if "_readd_area_ready(it)" not in _dmn_src:
                ok[0] = False
                detail.append("✗ ⑨m daemon._drain_pending_readds **没有**调 `_readd_area_ready(it)`"
                              " ⇒ 延迟回加又变「不核对直接推」（#136 病根）")
            if "def _readd_area_ready(" not in _dmn_src:
                ok[0] = False
                detail.append("✗ ⑨m daemon.py 里没有 `def _readd_area_ready(` ⇒ 护栏是空引用")
            # ⑥ ★ #136：FAILED 日志不得再读**过期快照** `it.get('last_error')`
            #   （现场 `PENDING → FAILED（）` 括号恒空 = 排障时看不到失败原因）。
            if "f\"（{it.get('last_error')}）\"" in _dmn_src:
                ok[0] = False
                detail.append("✗ ⑨m [handoff] FAILED 日志仍读**过期** `it.get('last_error')`"
                              " ⇒ 括号恒空（应改读队列真值）")
        except Exception as _e9m:                                # noqa: BLE001
            ok[0] = False
            detail.append(f"✗ ⑨m 执行失败: {type(_e9m).__name__}: {_e9m}")

        # ── ⑨l ★★★ #136：**"够格才交棒"必须真生效**（出种≥N 或 做种≥D 天）──
        #   ★★ 为什么：`judge`/`pick_handoff_candidates` 原先**只在 selftest 被调**，
        #     生产 daemon 从不引用 ⇒ "出种≥3 才交棒"是**死代码**（实测 `☆ac8.13f` 20 个
        #     别人做种仍不交棒、容器内 `已交棒到 3020` 计数=0）。用户 2026-10-07 拍板
        #     语义 = **够格才交棒** ⇒ `process_one` 的 PENDING 分支必须先过 `eligible_on_3040`。
        #   ★ 判据：(a) 行为 —— `eligible_on_3040` 三态正确（缺种/不够格/够格）；
        #           (b) 源码护栏 —— `process_one` 里出现 `eligible_on_3040(`，
        #               且 daemon 侧交棒块**不是**无条件立即交棒。
        try:
            # (a) 行为：假 qb_src 覆盖三种 3040 实况。
            class _FakeSrc9L:
                def __init__(self, info):
                    self._info = info
                def torrents_info(self, **kw):
                    return self._info
            _NOW9L = 1_800_000_000.0
            # ① 3040 上没这条种 ⇒ present=False（还没回加）⇒ 等
            _p1, _e1, _ = _ho.eligible_on_3040(_FakeSrc9L([]), "aa", now=_NOW9L,
                                               seeders_min=3, seed_days=3)
            chk("⑨l 3040 无该种 ⇒ 不交棒（等）", (_p1, _e1), (False, False))
            # ② 做完种但别人做种=0（num_complete=1）、做种 0 天 ⇒ 不够格
            _p2, _e2, _ = _ho.eligible_on_3040(
                _FakeSrc9L([{"hash": "aa", "progress": 1.0, "num_complete": 1,
                             "added_on": _NOW9L}]), "aa", now=_NOW9L,
                seeders_min=3, seed_days=3)
            chk("⑨l 只有自己做种(num_complete=1)·0天 ⇒ 不交棒（等）", _e2, False)
            # ③ 别人做种=3（num_complete=4）⇒ 够格交棒
            _p3, _e3, _ = _ho.eligible_on_3040(
                _FakeSrc9L([{"hash": "aa", "progress": 1.0, "num_complete": 4,
                             "added_on": _NOW9L}]), "aa", now=_NOW9L,
                seeders_min=3, seed_days=3)
            chk("⑨l 别人做种3(num_complete=4) ⇒ 交棒", _e3, True)
            # ④ 还没做完种（progress<1）即便 num_complete 很大 ⇒ 不够格
            _p4, _e4, _ = _ho.eligible_on_3040(
                _FakeSrc9L([{"hash": "aa", "progress": 0.5, "num_complete": 9,
                             "added_on": _NOW9L}]), "aa", now=_NOW9L,
                seeders_min=3, seed_days=3)
            chk("⑨l progress<1 即便出种多 ⇒ 不交棒（等）", _e4, False)
            # (b) 源码护栏：`process_one` 必须真调 `eligible_on_3040`（**调用点**，
            #     ★ 别只搜 `eligible_on_3040(` —— 那是 `def` 行也含的子串，会恒为真）。
            # ★★★ #45：调用现在带 `name=` 兜底（3040 挂站点种 B、hash 与 A 不同）
            #     ⇒ 判据放宽为"出现 `eligible_on_3040(qb_src, info_hash`"（含前缀即可）。
            if "eligible_on_3040(qb_src, info_hash" not in _ho_src:
                ok[0] = False
                detail.append("✗ ⑨l handoff.py 里**没有调用** `eligible_on_3040(qb_src, "
                              "info_hash…)` ⇒ 「够格才交棒」仍是死代码（#136 病根）")
            if "def eligible_on_3040(" not in _ho_src:
                ok[0] = False
                detail.append("✗ ⑨l handoff.py 里没有 `def eligible_on_3040(`")
            # ★★★ #45 回归：`_find_on_3040` 必须存在，且 `eligible_on_3040` 用它
            #     （否则"hash 用 A、3040 挂 B"永远查不到 ⇒ 够格种永不交棒）。
            if "def _find_on_3040(" not in _ho_src:
                ok[0] = False
                detail.append("✗ ⑨l#45 缺 `_find_on_3040`（hash/名字双查）")
            if "_find_on_3040(qb_src, info_hash" not in _ho_src:
                ok[0] = False
                detail.append("✗ ⑨l#45 `eligible_on_3040` 未用 `_find_on_3040` ⇒ "
                              "3040 挂站点种(B) 时仍查不到（够格种永不交棒）")
            # 行为：#45 用 name 兜底命中（hash 不匹配但名字匹配 ⇒ 视为 present）。
            class _FakeSrc45:
                def __init__(self, info):
                    self._info = info
                def torrents_info(self, **kw):
                    if kw.get("torrent_hashes"):
                        return []                       # 按 hash（A）查不到
                    return self._info                    # 全表（含 B，按名字）
            _p5, _e5, _w5 = _ho.eligible_on_3040(
                _FakeSrc45([{"hash": "bb", "name": "专辑X", "progress": 1.0,
                             "num_complete": 4, "added_on": _NOW9L}]),
                "aa", name="专辑X", now=_NOW9L, seeders_min=3, seed_days=3)
            chk("⑨l#45 hash 不匹配·按 name 兜底命中 ⇒ 够格", (_p5, _e5), (True, True))
            if "按名字命中" not in str(_w5):
                ok[0] = False
                detail.append(f"✗ ⑨l#45 按名字命中时应标注 why，实为 {_w5!r}")
        except Exception as _e9l:                                # noqa: BLE001
            ok[0] = False
            detail.append(f"✗ ⑨l 执行失败: {type(_e9l).__name__}: {_e9l}")

        # ── ⑨n ★★★ #136 / ERR-PUBLISH-11：**多版本同名输出碰撞**防护 ──
        #   ★★ 为什么：输出名原先只由 `formatted_title` 算 ⇒ 不同版本（初回/期間/完全/
        #     通常/分轨/整轨）只要算出的标题相同就**算到同一个文件名** ⇒ 后写者覆盖前者的
        #     `.torrent`/`.json` ⇒ 站点挂错版本（实测 `20667`/`23313`/`216217`/`217592`/`216915`
        #     五例）。判据 = 同名 sidecar 里记的 `info_hash` 若**不是自己** ⇒ 消歧改名。
        #   ★ 全部用**注入的 exists/read_json**（`output_sidecar_paths` 是纯函数）——
        #     不碰真文件系统（`ERR-META-14`：自检不许有真副作用）。
        try:
            _out = os.path.join(_tf.mkdtemp(prefix="namecollide-136-selftest-"), "out")
            os.makedirs(_out, exist_ok=True)
            # 假文件系统：`_FS[name] = None`（存在但解析失败）或 = dict（解析成功）。
            _FS = {}
            _exists = lambda p: os.path.basename(p) in _FS      # noqa: E731
            def _readj(p, _FS=_FS):                              # noqa: ANN001
                v = _FS.get(os.path.basename(p))
                if v is None:
                    raise ValueError("坏 json")
                return v

            _TITLE = "女王蜂 - 星 2024 - FLAC 整轨"
            _stem = _safe_output_stem(_TITLE)

            # ① 无同名 sidecar（首见）⇒ **不改名**、无碰撞标志。
            _t1, _j1, _c1 = output_sidecar_paths(
                _TITLE, "aaaaaaaaaaaa", _out, exists=_exists, read_json=_readj)
            chk("⑨n 首见（无同名 sidecar）·用默认名", os.path.basename(_t1), f"{_stem}.torrent")
            chk("⑨n 首见·json 与 torrent 同 stem", os.path.basename(_j1), f"{_stem}.json")
            chk("⑨n 首见·无碰撞标志", _c1, False)

            # ② 同名 sidecar 是**自己**（重扫）⇒ **不改名**（名字稳定，不漂移）。
            _FS[f"{_stem}.json"] = {"info_hash": "AAAAAAAAAAAA"}   # 大小写/同一哈希
            _t2, _j2, _c2 = output_sidecar_paths(
                _TITLE, "aaaaaaaaaaaa", _out, exists=_exists, read_json=_readj)
            chk("⑨n 同名 sidecar 是自己·不改名（重扫稳定）", os.path.basename(_t2), f"{_stem}.torrent")
            chk("⑨n 同名 sidecar 是自己·无碰撞标志", _c2, False)

            # ③ ★★ 核心（阴性对照的镜像）：同名 sidecar 是**别的 infohash** ⇒ 消歧改名。
            _FS[f"{_stem}.json"] = {"info_hash": "bbbbbbbbbbbb"}
            _t3, _j3, _c3 = output_sidecar_paths(
                _TITLE, "abcdef012345", _out, exists=_exists, read_json=_readj)
            chk("⑨n 同名 sidecar 是别的版本·消歧改名", os.path.basename(_t3), f"{_stem}_abcdef01.torrent")
            chk("⑨n 消歧·json 与 torrent **同**基名", os.path.basename(_j3), f"{_stem}_abcdef01.json")
            chk("⑨n 消歧·置碰撞标志（上层须报出）", _c3, True)

            # ④ 消歧名**确定性**：同样输入再算一次必须**逐字节相同**（否则每轮 scan 漂移）。
            _t4, _j4, _c4 = output_sidecar_paths(
                _TITLE, "abcdef012345", _out, exists=_exists, read_json=_readj)
            chk("⑨n 消歧名确定性（同输入同结果）", (_t4, _j4), (_t3, _j3))

            # ⑤ sidecar 坏/解析不出 ⇒ **不改名**（保守，不因坏 json 制造新名）+ 不抛。
            _FS[f"{_stem}.json"] = None
            _t5, _j5, _c5 = output_sidecar_paths(
                _TITLE, "cccccccccccc", _out, exists=_exists, read_json=_readj)
            chk("⑨n sidecar 坏·不改名（保守·不抛）", os.path.basename(_t5), f"{_stem}.torrent")
            chk("⑨n sidecar 坏·无碰撞标志", _c5, False)

            # ⑥ 无 info_hash 也不抛（消歧后缀退化为 nohash）—— 边界不许崩。
            _FS[f"{_stem}.json"] = {"info_hash": "dddddddddddd"}
            _t6, _j6, _c6 = output_sidecar_paths(
                _TITLE, "", _out, exists=_exists, read_json=_readj)
            chk("⑨n 无 info_hash·消歧不抛且带 nohash", os.path.basename(_t6), f"{_stem}_nohash.torrent")
            _sh.rmtree(os.path.dirname(_out), ignore_errors=True)
        except Exception as _e9n:                                # noqa: BLE001
            ok[0] = False
            detail.append(f"✗ ⑨n 执行失败: {type(_e9n).__name__}: {_e9n}")

        # ⑨n 源码护栏：write 路径**不许**再出现旧的"各算一次 stem"（回归护栏）。
        _run_py = _pl9i.Path(__file__).resolve().parent / "run.py"
        _run_src = _run_py.read_text(encoding="utf-8") if _run_py.exists() else ""
        # ★ 先**去掉注释与字符串里的假匹配**：本守卫自己的注释里就写了
        #   `safe_name = re.sub(`（当反面教材），不去掉 ⇒ **自己把自己判红**
        #   （实测踩到：守卫恒红、真回归反而看不见 —— 正是 ERR-META-01 的形状）。
        #   ⇒ 只保留"行内 `#` 之前"的代码部分再搜。
        _run_code_lines = []
        for _ln in _run_src.splitlines():
            _code = _ln.split("#", 1)[0]
            _run_code_lines.append(_code)
        _run_code = "\n".join(_run_code_lines)
        if re.search(r"safe_name\s*=\s*re\.sub\(", _run_code):
            ok[0] = False
            detail.append("✗ ⑨n run.py 仍按 `safe_name` 只从标题算输出名 ⇒ 同名版本又互踏"
                          "（ERR-PUBLISH-11 回归）")
        if "output_sidecar_paths(" not in _run_code:
            ok[0] = False
            detail.append("✗ ⑨n run.py 里没有 `output_sidecar_paths(` ⇒ 消歧护栏没接线")

        # ⑨o 源码护栏（2026-10-07 / #33 / ERR-PUBLISH-12）：`edit_torrent` **绝不许**
        #    假装能换挂的 `.torrent`。判据 = musopia.py 里必须同时出现：
        #      (a) 常量 `EDIT_CANNOT_CHANGE_TORRENT = True`（把这条事实写成真值；
        #          ★ 必须断言 **= True**，不是"名字出现过"——否则把它改成 `= False`
        #            这种"事实被反转"的回归会**静默通过**，本站守卫就成了摆设）；
        #      (b) `edit_torrent` 里对 `EDIT_FILE_LIKE_KEYS` 的**拒绝**（不是静默忽略）。
        #    ★ 先剥注释再搜（同 ⑨n 的教训）：本守卫自己的说明里就写了这些标识符。
        _mus_py = _pl9i.Path(__file__).resolve().parent / "musopia.py"
        _mus_src = _mus_py.read_text(encoding="utf-8") if _mus_py.exists() else ""
        _mus_code = "\n".join(_l.split("#", 1)[0] for _l in _mus_src.splitlines())
        if not re.search(r"EDIT_CANNOT_CHANGE_TORRENT\s*=\s*True\b", _mus_code):
            ok[0] = False
            detail.append("✗ ⑨o musopia.py 没有 `EDIT_CANNOT_CHANGE_TORRENT = True` ⇒ "
                          "『edit 改不了挂的种子』这条事实没被钉住（ERR-PUBLISH-12）")
        if "EDIT_FILE_LIKE_KEYS" not in _mus_code:
            ok[0] = False
            detail.append("✗ ⑨o `edit_torrent` 未拒绝 file 类 overrides ⇒ "
                          "改种子会**静默假成功**（ERR-PUBLISH-12）")

        # ── ⑨p ★ scan 必须连皇后源实例，不许无条件 `qb_client.build(config)` ──
        #   （2026-10-10 #16；根因见 ERRATA 的 ERR-ORCH-02）
        #   ★ 病根：`config.QB_HOST` 是**发种实例**（生产 3010，0 个 opencd），而皇后源是
        #     `QB_HOST_OPENCD`（生产 3260，1376 个 opencd）。scan 原先直读 QB_HOST ⇒ 每轮
        #     「找到 0 个分类为 [opencd] 的种子」⇒ 队列表饿死。
        #   ★★ 判据走 **AST**（不许 `"qb_client.build(config)" in 源码文本` —— 那种写法会被
        #     **字符串字面量 / docstring** 里的同名字样骗成**假红**：本判据第一版正是栽在这
        #     上面，⑧ 的告警串里就有一句 `qb_client.build(config)`；见 ⑦ 那段同样的坑）。
        #   ★ 判据三条：
        #     (a) `main()` 里**不许**有 `qb_client.build(<裸 config>)` 调用；
        #     (b) `main()` 里**必须**出现 `_scan_source_host` 这个 Name（取地址的唯一入口）；
        #     (c) `_scan_source_host()` 的 `or` 链里 `QB_HOST_OPENCD` 必须排在裸 `QB_HOST` 前
        #         （显式 override `QB_HOST_SCAN` 最前不算错）。
        import ast as _ast9p
        _run_tree = _ast9p.parse(_pl9i.Path(__file__).resolve().read_text(encoding="utf-8"))

        def _find_func(tree, name):
            for node in _ast9p.walk(tree):
                if isinstance(node, _ast9p.FunctionDef) and node.name == name:
                    return node
            return None

        # (a) scan 主流程里不许 `qb_client.build(config)`
        #   ★★ 2026-10-10（#17f）：scan 主体已从 `main()` 抽到 `_scan_main(args)`
        #     （`main()` 现在只负责**取单实例锁**再调 `_scan_main`）。⇒ 本判据要覆盖
        #     **两个**函数体：抽函数是重构，不该让护栏"看空"（`ERR-META-01`：判据失效
        #     比不判更危险 —— 改完名字它仍恒绿，但真回归也看不见了）。
        _scan_fns = [f for f in (_find_func(_run_tree, "main"),
                                 _find_func(_run_tree, "_scan_main")) if f is not None]
        _bad_build = []
        for _fn in _scan_fns:
            for node in _ast9p.walk(_fn):
                if (isinstance(node, _ast9p.Call)
                        and isinstance(node.func, _ast9p.Attribute)
                        and node.func.attr == "build"
                        and isinstance(node.func.value, _ast9p.Name)
                        and node.func.value.id == "qb_client"
                        and len(node.args) == 1
                        and isinstance(node.args[0], _ast9p.Name)
                        and node.args[0].id == "config"):
                    _bad_build.append(node.lineno)
        if _bad_build:
            ok[0] = False
            detail.append(f"✗ ⑨p scan 主流程里仍有 `qb_client.build(config)`（行 {_bad_build}）⇒ "
                          "scan 会无条件读 config.QB_HOST（发种实例，0 个 opencd）⇒ "
                          "队列饿死（ERR-ORCH-02）")

        # (b) scan 主流程里必须出现 `_scan_source_host` 这个名字
        _uses_scan_host = False
        for _fn in _scan_fns:
            for node in _ast9p.walk(_fn):
                if isinstance(node, _ast9p.Name) and node.id == "_scan_source_host":
                    _uses_scan_host = True
        if not _uses_scan_host:
            ok[0] = False
            detail.append("✗ ⑨p scan 主流程未经 `_scan_source_host()` 取地址 ⇒ "
                          "scan 源不再被单独钉住（ERR-ORCH-02）")

        # (c) `_scan_source_host()` 的 or 链里 `QB_HOST_OPENCD` 必须排在裸 `QB_HOST` 前
        _ssh_fn = _find_func(_run_tree, "_scan_source_host")
        if _ssh_fn is None:
            ok[0] = False
            detail.append("✗ ⑨p 找不到 `_scan_source_host()`（ERR-ORCH-02）")
        else:
            # ★★★ 只看 **return 表达式**里的字符串字面量（★ 不看 docstring —— 本函数的
            #   docstring 正文里就写着 `QB_HOST_OPENCD`，会把它当成"链里出现过"，从而把
            #   顺序判据骗过。本判据第一版正是栽在这里；见 ⑦ 的同一个坑）。
            _ret = None
            for node in _ast9p.walk(_ssh_fn):
                if isinstance(node, _ast9p.Return):
                    _ret = node.value
                    break
            _names = []
            if _ret is not None:
                for node in _ast9p.walk(_ret):
                    if isinstance(node, _ast9p.Constant) and isinstance(node.value, str):
                        _names.append((node.lineno, node.col_offset, node.value))
            _lit = [v for _, _, v in sorted(_names)]
            _has_opencd = any("QB_HOST_OPENCD" in v for v in _lit)
            if not _has_opencd:
                ok[0] = False
                detail.append("✗ ⑨p `_scan_source_host()` 的 return 未引用 `QB_HOST_OPENCD` ⇒ "
                              "scan 会用发种实例当源（ERR-ORCH-02）")
            # 裸 QB_HOST（逐字，排除 QB_HOST_SCAN / QB_HOST_OPENCD）若出现，必须在 OPENCD 之后
            _idx_opencd = next((i for i, v in enumerate(_lit) if "QB_HOST_OPENCD" in v), None)
            _idx_bare = next((i for i, v in enumerate(_lit) if v == "QB_HOST"), None)
            if _idx_opencd is not None and _idx_bare is not None and _idx_bare < _idx_opencd:
                ok[0] = False
                detail.append("✗ ⑨p `_scan_source_host()` 的 return 里裸 `QB_HOST` 排在 "
                              "`QB_HOST_OPENCD` 之前 ⇒ 源优先级反了（ERR-ORCH-02）")
    except Exception as e:                                        # noqa: BLE001
        ok[0] = False
        detail.append(f"✗ ⑨ 交棒自检执行失败: {type(e).__name__}: {e}")

    # ── ⑨q ★ scan 单实例锁（2026-10-10 #17f；根因 ERR-ORCH-03）──────────────
    #   ★ 判据（三条，含**阴性对照**——没有它就是"测了个寂寞"）：
    #     (a) 冷启动能拿到锁（第一个 acquire 必须成功）；
    #     (b) 已持锁时**再 acquire 同一个锁文件必须失败**（返回 None）—— 这是核心：
    #         若把 `LOCK_NB` 去掉、或改成"只检查文件存在"，这条会变绿 ⇒ 护栏失效；
    #     (c) 释放后**又能拿到**（证明锁真随释放/进程退出而解，不是"永久占死"）。
    #   ★ 只在本机有 fcntl 时做 (b)(c)（Windows 上 `acquire_scan_lock` 走无锁分支，
    #     恒返回句柄 ⇒ (b) 会假红）。判据 = 能不能 import fcntl。
    try:
        import tempfile as _tf9q
        try:
            import fcntl as _has_fcntl  # noqa: F401
            _fcntl_ok = True
        except ImportError:
            _fcntl_ok = False
        with _tf9q.TemporaryDirectory() as _d9q:
            _lp = os.path.join(_d9q, "scan.lock")
            _h1, _p1 = acquire_scan_lock(_lp)
            chk("⑨q(a) 冷启动拿到锁", _h1 is not None, True)
            chk("⑨q(a) 锁路径回传", _p1, _lp)
            if _fcntl_ok:
                _h2, _ = acquire_scan_lock(_lp)
                chk("⑨q(b) 已持锁⇒第二个拿不到（阴性对照）", _h2, None)
                _release_scan_lock(_h1)
                _h3, _ = acquire_scan_lock(_lp)
                chk("⑨q(c) 释放后又能拿到", _h3 is not None, True)
                _release_scan_lock(_h3)
            else:
                # 无 fcntl（Windows）：至少证明"拿到东西、且不抛"。
                _release_scan_lock(_h1)
                detail.append("ℹ ⑨q(b)(c)(d) 跳过：本机无 fcntl（生产 Linux 容器有）")
            if _fcntl_ok:
                # 打不开锁文件 ⇒ 不抛、返回 None（护栏失效但不连累整轮）。
                _h_bad, _ = acquire_scan_lock(os.path.join(_d9q, "no_such_dir", "x.lock"))
                chk("⑨q(d) 坏路径不抛、返回 None", _h_bad, None)
    except Exception as e:                                        # noqa: BLE001
        ok[0] = False
        detail.append(f"✗ ⑨q 锁自检执行失败: {type(e).__name__}: {e}")

    # ── ⑨r ★ 皇后种识别 = tracker 精确匹配（2026-10-11 #10/#172；根因 ERR-ORCH-04）──
    #   ★ 判据（含**阴性对照**——没有它就是"测了个寂寞"）：
    #     (a) 真皇后 tracker ⇒ 判是；
    #     (b) 别站 tracker（musopia/crabpt）⇒ 判否（核心：口径没放宽成"任意有 tracker 的种"）；
    #     (c) **子串形近**（`evil-tracker.open.cd` / `tracker.open.cd.evil.com`）⇒ 判否
    #         （核心：证明是**精确主机名**比较，不是 `in` 子串）；
    #     (d) 空 tracker / 拿不到 ⇒ 判否（"不可知"不当成"是"）；
    #     (e) 大小写不敏感；
    #     (f) `_opencd_tracker_host()` 的默认值 == 实测主机名（口径没被改飘）。
    class _T:  # 极简 torrent 替身（只要 `.tracker`）
        def __init__(self, url):
            self.tracker = url
    try:
        _th = _opencd_tracker_host()
        chk("⑨r(f) 默认 tracker 主机 = tracker.open.cd", _th, "tracker.open.cd")
        chk("⑨r(a) 真皇后 tracker ⇒ 是",
            _torrent_is_opencd(_T("https://tracker.open.cd/announce.php?passkey=x"), _th), True)
        chk("⑨r(a) http 也认",
            _torrent_is_opencd(_T("http://tracker.open.cd/announce"), _th), True)
        for _bad in ("https://www.musopia.vip/announce.php?passkey=x",
                     "https://crabpt.vip/announce.php?passkey=x"):
            chk(f"⑨r(b) 别站⇒否({_bad[8:20]})",
                _torrent_is_opencd(_T(_bad), _th), False)
        for _sub in ("https://evil-tracker.open.cd/announce",
                     "https://tracker.open.cd.evil.com/announce",
                     "https://tracker.open.cdx/announce"):
            chk(f"⑨r(c) 形近子串⇒否({_sub[8:24]})",
                _torrent_is_opencd(_T(_sub), _th), False)
        chk("⑨r(d) 空 tracker⇒否", _torrent_is_opencd(_T(""), _th), False)
        chk("⑨r(d) 拿不到 tracker⇒否", _torrent_is_opencd(object(), _th), False)
        chk("⑨r(e) 大小写不敏感",
            _torrent_is_opencd(_T("https://TRACKER.OPEN.CD/announce"), _th), True)
        # ★ 端到端：给一串混合 torrent，过滤后**只剩**皇后那一个（证明"过滤真发生"）。
        _mixed = [_T("https://tracker.open.cd/a"), _T("https://crabpt.vip/b"),
                  _T(""), _T("https://www.musopia.vip/c"), _T("https://tracker.open.cd/d")]
        _kept = [t for t in _mixed if _torrent_is_opencd(t, _th)]
        chk("⑨r 混合样本过滤后只剩 2 个皇后种", len(_kept), 2)
    except Exception as e:                                        # noqa: BLE001
        ok[0] = False
        detail.append(f"✗ ⑨r 皇后种识别自检执行失败: {type(e).__name__}: {e}")

    # ── ⑨s ★ #31 查重"精准优先 + 命中即停"的**结构**护栏（2026-10-11）──────
    #   ★★ 独立失效模式：有人把 `search_and_compare` 从"逐条查询→立即验证→命中即停"
    #     简化回"先跑完所有查询取并集、再验证"（= 13/20→11/20 的病），或把两条 `raise`
    #     挪出循环（"中途失败被吞"）。行为断言在**夹具宽松时**可能漏掉（`ERR-META-14`：
    #     失效的检测比不检测更危险）⇒ 这里钉**结构**。
    #   ★ 判据（源码文本；先剥注释，免得本守卫自己的说明里就写了这些标识符）：
    #     (a) 函数体内**同时**有 `raise SiteError` 与 `raise LoginExpired`（绝不降级）；
    #     (b) 引用 `DEDUP_MAX_CANDIDATES`（预算是全局的、读 config）；
    #     (c) 命中即停：函数体里有 `return tid`（命中即返回、不继续跑宽查询）。
    try:
        import ast as _ast9s
        _mus_py9 = _pl9i.Path(__file__).resolve().parent / "musopia.py"
        _mus_txt9 = _mus_py9.read_text(encoding="utf-8") if _mus_py9.exists() else ""
        if not _mus_txt9:
            ok[0] = False
            detail.append("✗ ⑨s 找不到 musopia.py ⇒ 判据没测到东西（ERR-META-14）")
        else:
            _sac_src = ""
            for _n in _ast9s.walk(_ast9s.parse(_mus_txt9)):
                if isinstance(_n, _ast9s.FunctionDef) and _n.name == "search_and_compare":
                    _seg = _ast9s.get_source_segment(_mus_txt9, _n) or ""
                    _sac_src = "\n".join(l.split("#", 1)[0] for l in _seg.splitlines())
                    break
            if not _sac_src:
                ok[0] = False
                detail.append("✗ ⑨s 找不到 search_and_compare 函数体（ERR-META-14）")
            else:
                for _mark in ("raise SiteError", "raise LoginExpired"):
                    if _mark not in _sac_src:
                        ok[0] = False
                        detail.append(f"✗ ⑨s search_and_compare 缺 `{_mark}` ⇒ "
                                      "中途失败可能被静默降级成'没重复'（危险）")
                if "DEDUP_MAX_CANDIDATES" not in _sac_src:
                    ok[0] = False
                    detail.append("✗ ⑨s search_and_compare 未引用 `DEDUP_MAX_CANDIDATES` ⇒ "
                                  "全局候选预算可能已失效")
                if "return tid" not in _sac_src:
                    ok[0] = False
                    detail.append("✗ ⑨s search_and_compare 缺 `return tid` ⇒ 命中即停可能已失效")
    except Exception as e:                                        # noqa: BLE001
        ok[0] = False
        detail.append(f"✗ ⑨s 查重结构护栏执行失败: {type(e).__name__}: {e}")

    # ★★★ 2026-10-18（C1，ERR-META-33）：**兜底默认值三处必须一致，且"用了兜底"要可观测**。
    #   ★ 为什么必须是判据而不是注释：`QB_HOST` 的兜底曾与生产真值不一致（3040 vs 3010），
    #     **漂了 11 天没人发现** —— 因为 `.env` 正常时那三处兜底根本不出现。
    #     `ERR-META-33` 的原话："凡'改生产值就够'的改动都是可疑的"。
    #   ★ 判据三条（都走**源码/AST**，不靠人记）：
    #     (a) `config.QB_HOST_DEFAULT` 的三处来源逐字一致：
    #         compose 的两个 `${QB_HOST:-…}` 与 `config.QB_HOST_DEFAULT`；
    #     (b) `QB_HOST` 必须由 `QB_HOST_DEFAULT` 派生（不许写第二个字面量）；
    #     (c) 空串/纯空格必须**回落兜底**（`os.getenv("K", d)` 遇到空串会返回空串 —— 真坑）；
    #     (d) `QB_HOST` 必须进 `daemon.ENV_SNAPSHOT_KEYS`（否则巡检看不见真实值）。
    #   ★ 阴性对照：把 compose 任一处改回 3040 ⇒ (a) 必红；把 config 那行的 `_QB_HOST_RAW or`
    #     删掉 ⇒ (c) 必红（实测见 `ERR-META-33` 落地记录）。
    try:
        import re as _re1
        _root1 = _pl9i.Path(__file__).resolve().parent.parent
        # ★★ 路径要同时认两种落地形态：本机仓库（`<repo>/src/run.py`）与
        #   容器镜像（`/app/run.py`，**所有 .py 平铺在 /app**、无 `src/` 子目录）。
        _src_dir1 = _pl9i.Path(__file__).resolve().parent
        _cfg_txt = (_src_dir1 / "config.py").read_text(encoding="utf-8")
        # (a) 从 config 里抽默认值，再逐个比对 compose 的两处
        _m_def = _re1.search(r'^QB_HOST_DEFAULT\s*=\s*"([^"]+)"', _cfg_txt, _re1.M)
        if not _m_def:
            ok[0] = False
            detail.append("✗ C1(a) config.py 里找不到 `QB_HOST_DEFAULT = \"…\"`"
                          " ⇒ 本判据没测到东西（ERR-META-14）")
        else:
            _qbd = _m_def.group(1)
            # ★★★ 2026-10-18：**compose 不进镜像**（只装 .py 到 /app）⇒ 容器里这条
            #   既不该判红、也不该"静默跳过"。按 `ERR-META-01` 的口径：
            #   **没测到就说没测到**，并且说明"为什么合理"，不是当成通过。
            _compose = _root1 / "docker-compose.yml"
            _sub = _root1 / "src" / "docker-compose.yml"      # 防备别处布局
            if not _compose.exists():
                _compose = _sub if _sub.exists() else None
            if _compose is None:
                # ★ 只有"本机仓库缺失"才是异常；容器内缺 compose 是**设计如此**。
                if (_root1 / "src").is_dir():
                    ok[0] = False
                    detail.append("✗ C1(a) 本机仓库里找不到 docker-compose.yml"
                                  " ⇒ 判据没测到东西（ERR-META-14）")
                else:
                    detail.append("ℹ C1(a) **容器内无 docker-compose.yml**（设计如此："
                                  "镜像只装 .py）⇒ 本段跳过，改由发版前的本机自检覆盖")
            else:
                _ctxt = _compose.read_text(encoding="utf-8")
                _falls = _re1.findall(
                    r'QB_HOST=\$\{QB_HOST:-([^}]*)\}', _ctxt)
                if len(_falls) != 2:
                    ok[0] = False
                    detail.append(f"✗ C1(a) compose 里 `${QB_HOST:-…}` 兜底应恰好 2 处"
                                  f"（common + once），实测 {len(_falls)} 处"
                                  f" ⇒ 判据可能已失准")
                for _i, _f in enumerate(_falls, 1):
                    chk(f"C1(a) compose 第 {_i} 处兜底必须 == config.QB_HOST_DEFAULT",
                        _f, _qbd)
        # (b) `QB_HOST` 不许写第二个字面量（只许由 `_QB_HOST_RAW or QB_HOST_DEFAULT` 派生）
        _m_use = _re1.search(r'^QB_HOST\s*=\s*(.+)$', _cfg_txt, _re1.M)
        if not _m_use:
            ok[0] = False
            detail.append("✗ C1(b) config.py 里找不到 `QB_HOST = …` 赋值")
        else:
            chk("C1(b) QB_HOST 必须由 QB_HOST_DEFAULT 派生（不许第二处字面量）",
                "QB_HOST_DEFAULT" in _m_use.group(1), True)
        # (c) 空串必须回落兜底 —— 这是"os.getenv 默认值对空串无效"这个真坑的判据
        #   ★★★ 2026-10-18：子进程的 `sys.path` 要**按实际落地形态给**：
        #     本机仓库是 `<repo>/src/config.py`，容器镜像是 `/app/config.py`。
        #     写死 `"src"` ⇒ 容器里 `import config` 会落到别处/失败 ⇒ 假红（实测踩到）。
        #     判据 = **沿 config.py 所在目录**（`_src_dir1`），与本进程同一份文件。
        import subprocess as _sp1
        import sys as _sys1
        _env1 = {k: "x" for k in ("QB_PASS", "MUSOPIA_COOKIE", "MUSOPIA_PASSKEY",
                                  "PICGO_API_KEY", "OPENCD_COOKIE")}
        _env1["QB_HOST"] = ""                      # ★ 关键：显式空串
        _env1["DATA_DIR"] = str(_root1 / ".scratch" / "selftest-data")
        _py1 = _sp1.run(
            [_sys1.executable, "-c",
             f"import sys;sys.path.insert(0,{str(_src_dir1)!r});import config;"
             f"print(config.QB_HOST_FROM_DEFAULT, config.QB_HOST)"],
            cwd=str(_root1), env=_env1, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60)
        _out1 = (_py1.stdout or "").strip().splitlines()
        _last = _out1[-1] if _out1 else ""
        chk("C1(c) QB_HOST 为空串时必须回落兜底并标记 FROM_DEFAULT",
            _last.startswith("True "), True)
        chk("C1(c) 空串时 QB_HOST 不许是空串（否则 Client(host='') 连到 localhost 默认端口）",
            _last.split(" ", 1)[1].strip() if " " in _last else "",
            _re1.search(r'^QB_HOST_DEFAULT\s*=\s*"([^"]+)"', _cfg_txt,
                        _re1.M).group(1)
            if _re1.search(r'^QB_HOST_DEFAULT\s*=\s*"([^"]+)"', _cfg_txt, _re1.M) else "?")
        # (d) 必须在 env 快照白名单里
        _dae_txt = (_pl9i.Path(__file__).resolve().parent / "daemon.py").read_text(
            encoding="utf-8")
        _m_whl = _re1.search(r'ENV_SNAPSHOT_KEYS\s*=\s*\((.*?)\n\)', _dae_txt, _re1.S)
        chk("C1(d) QB_HOST 必须进 daemon.ENV_SNAPSHOT_KEYS（否则巡检看不见真实值）",
            (_m_whl is not None) and ('"QB_HOST"' in _m_whl.group(1)), True)
    except Exception as e:                                        # noqa: BLE001
        ok[0] = False
        detail.append(f"✗ C1 兜底一致性检查执行失败：{type(e).__name__}: {e}")

    # ── ⑨i ★★★ ERR-PUBLISH-22：目录里有 qB 下载中临时文件 ⇒ **必须拒发** ──
    #   ★★ 本节的**全部价值 = 两个方向的对照**（`ERR-PUBLISH-21` 的教训：
    #     只测"该拒的拒了"= 只验了必要条件；条件写宽一格就会把正常种卡死）。
    #     ① 有 `.!qB` ⇒ 必拒（阳性）
    #     ② 干净目录（含长得像但不是临时后缀的名字）⇒ 必放行（阴性对照）
    #     ③ 判据必须真的**接了线**（AST：`create_musopia_torrent` 里调用了闸门）
    _tmpE8 = _tf.mkdtemp(prefix="errpub22-selftest-")
    try:
        _dir_bad = os.path.join(_tmpE8, "AlbumBad")
        os.makedirs(os.path.join(_dir_bad, "CD1"))
        with open(os.path.join(_dir_bad, "CD1", "01.flac"), "wb") as _f:
            _f.write(b"A" * 100)
        with open(os.path.join(_dir_bad, "CD1", "02.flac.!qB"), "wb") as _f:
            _f.write(b"B" * 100)
        with open(os.path.join(_dir_bad, "back.jpg.!qB"), "wb") as _f:
            _f.write(b"C" * 100)
        _hits = find_incomplete_download_markers(_dir_bad)
        chk("⑨i 有 .!qB ⇒ 必须检出（含子目录）",
            sorted(_hits), sorted([os.path.join("CD1", "02.flac.!qB"), "back.jpg.!qB"]))
        try:
            assert_no_incomplete_download(_dir_bad)
            ok[0] = False
            detail.append("✗ ⑨i 阳性对照失败：目录里有 .!qB 竟放行 ⇒ 闸门形同虚设")
        except IncompleteDownloadError:
            detail.append("ℹ ⑨i 阳性对照：有 .!qB ⇒ 拒发 ✅")

        # ── ② 阴性对照：干净目录必须放行 ──
        #   ★ 关键：里面要放**长得像**但不是临时后缀的名字（`Godzilla!.flac`
        #     就是 ERR-PUBLISH-22 的实测样本名），以及 `.part` 之外的 `.` 开头文件。
        _dir_ok = os.path.join(_tmpE8, "AlbumOk")
        os.makedirs(os.path.join(_dir_ok, "CD1"))
        for _n in ("01 - Godzilla!.flac", "02.flac", "back.jpg", "03.partial.flac"):
            with open(os.path.join(_dir_ok, "CD1", _n), "wb") as _f:
                _f.write(b"X" * 50)
        _hits_ok = find_incomplete_download_markers(_dir_ok)
        chk("⑨i 阴性对照：干净目录（含 'Godzilla!.flac'）不许误判",
            _hits_ok, [])
        try:
            assert_no_incomplete_download(_dir_ok)
            detail.append("ℹ ⑨i 阴性对照：干净目录 ⇒ 放行 ✅")
        except IncompleteDownloadError as _e:
            ok[0] = False
            detail.append(f"✗ ⑨i 阴性对照失败：干净目录被拒 ⇒ 闸门会卡死队列（{_e}）")

        # ── ③ 结构性判据：闸门必须**真的接在制种点上**（ERR-ENV-03 形状：写了不接）──
        import ast as _astE8
        import pathlib as _plE8
        _run_py = _plE8.Path(__file__).resolve().parent / "run.py"
        _treeE8 = _astE8.parse(_run_py.read_text(encoding="utf-8"))

        def _calls_in(func_name, callee):
            for _fn in _astE8.walk(_treeE8):
                if isinstance(_fn, _astE8.FunctionDef) and _fn.name == func_name:
                    for _c in _astE8.walk(_fn):
                        if (isinstance(_c, _astE8.Call)
                                and isinstance(_c.func, _astE8.Name)
                                and _c.func.id == callee):
                            return True
            return False

        if not _calls_in("create_musopia_torrent", "assert_no_incomplete_download"):
            ok[0] = False
            detail.append("✗ ⑨i create_musopia_torrent **没有**调用 assert_no_incomplete_download"
                          " ⇒ 闸门没接线，坏种照旧能制出来")
        else:
            detail.append("ℹ ⑨i 结构：create_musopia_torrent 已接闸门 ✅")
        # 闸门调用必须在 Torrent(...) 构造**之前**（之后就已经算完哈希了）
        _gate_line = _torf_line = None
        for _fn in _astE8.walk(_treeE8):
            if isinstance(_fn, _astE8.FunctionDef) and _fn.name == "create_musopia_torrent":
                for _c in _astE8.walk(_fn):
                    if isinstance(_c, _astE8.Call):
                        if (isinstance(_c.func, _astE8.Name)
                                and _c.func.id == "assert_no_incomplete_download"
                                and _gate_line is None):
                            _gate_line = _c.lineno
                        if isinstance(_c.func, _astE8.Name) and _c.func.id == "Torrent" \
                                and _torf_line is None:
                            _torf_line = _c.lineno
        if _gate_line is None or _torf_line is None:
            ok[0] = False
            detail.append(f"✗ ⑨i 取不到闸门/构造行号（gate={_gate_line}, Torrent={_torf_line}）"
                          " ⇒ 判据没测到东西")
        elif _gate_line > _torf_line:
            ok[0] = False
            detail.append(f"✗ ⑨i 闸门（行 {_gate_line}）在 Torrent 构造（行 {_torf_line}）"
                          " **之后** ⇒ 太晚")
        else:
            detail.append(f"ℹ ⑨i 结构：闸门（行 {_gate_line}）先于制种（行 {_torf_line}）✅")
    finally:
        _sh.rmtree(_tmpE8, ignore_errors=True)

    return ok[0], detail


def _safe_output_stem(formatted_title):
    """把标题变成可用作文件名的 stem（去非法字符）。

    ★ 单独抽出来是为了**让"stem 怎么算"只有一处**：护栏（`output_sidecar_paths`）
      与落盘都要用它，否则两处各算一次会再次错位（`ERR-META-03` 的形状）。
    """
    return re.sub(r'[\\/*?:"<>|]', "_", formatted_title or "")


def _hash_suffix(info_hash):
    """取 info_hash 的短后缀（前 8 位），用于同名碰撞时消歧。

    ★ 为什么用 info_hash 而**不是**随机数/时间戳：同一版本重扫时 stem 必须**稳定**
      —— 否则每轮 scan 都改一次文件名 ⇒ 库里 `torrent_file` 每轮被刷新、
      已发布页面的本地路径漂移（`flip_status` 那类"路径对不上"的病就是它）。
      info_hash 是这条种子的身份，重扫不变 ⇒ 消歧后的名字也不变。
    """
    h = (info_hash or "").strip().lower()
    return h[:8] if h else "nohash"


def output_sidecar_paths(formatted_title, info_hash, out_dir=None,
                         *, exists=None, read_json=None):
    """算这条种子的输出 `.torrent` / `.json` 路径，**并对同名碰撞消歧**。

    ★★★ 为什么需要它（#136 / `ERR-PUBLISH-11`，2026-10-07 实测）：
      原代码只按 `formatted_title` 算 stem（`run.py` 旧 `safe_name`）——
      **不同版本**（初回/期間/完全/通常/分轨/整轨…）只要算出的标题**碰巧相同**，
      就会**算到同一个文件名**：
        · 后处理的那条把前一条的 `.torrent` 覆盖 ⇒ 站点上挂的是"后一个版本"
          （`20667`、`23313`、`216217`、`217592`、`216915` 实测五例）；
        · 两条的 `.json` sidecar 也互相覆盖 ⇒ 连带错位。
      ⇒ **名字里必须带上"是哪一条种子"的信息**，否则同名必然互踏。

    ★ 判据（唯一，可脱库单测）：看**同 stem 的 `.json` sidecar 里已记的 `info_hash`**。
        · 没有同名 sidecar 或解析不出 ⇒ **不改名**（首个用默认名）；
        · 同名 sidecar 的 `info_hash` == 我们的 ⇒ **不改名**（是同一版本重扫）；
        · 同名 sidecar 的 `info_hash` != 我们的 ⇒ **碰撞** ⇒ 追加 `_<hash8>` 消歧。

    ★ 返回 `(torrent_path, json_path, collided)`：
        · `collided=True` 时调用方**必须**打印（`ERR-META-14`：兜底不许静默）；
        · 消歧名是**确定性的**（只看 info_hash）⇒ 重扫稳定、不漂移。

    ★ `exists` / `read_json` 两个参数**只**为单测注入（默认走真文件系统）——
      让本函数是**纯函数**：给同样的目录状态就算出同样的结果。
    """
    out_dir = out_dir if out_dir is not None else config.OUTPUT_TORRENT_DIR
    _exists = exists if exists is not None else os.path.exists
    stem = _safe_output_stem(formatted_title)
    sidecar = os.path.join(out_dir, f"{stem}.json")
    collided = False
    if _exists(sidecar):
        prev_hash = None
        try:
            if read_json is not None:
                data = read_json(sidecar)
            else:
                with open(sidecar, "r", encoding="utf-8") as f:
                    data = json.load(f)
            if isinstance(data, dict):
                prev_hash = (data.get("info_hash") or "").strip().lower() or None
        except (OSError, ValueError, TypeError):
            # 解析失败 ⇒ **不改名**（保守：宁可沿用默认名，也不因坏 sidecar 制造新名）。
            # ★ 但这是"拿不到旧身份"⇒ 不静默：调用方按 collided=False 会照常命名，
            #   由上层日志覆盖（这里不抛，避免一颗坏 json 让整轮 scan 崩 —— 见 ERR-META-14）。
            prev_hash = None
        if prev_hash is not None and prev_hash != (info_hash or "").strip().lower():
            stem = f"{stem}_{_hash_suffix(info_hash)}"
            collided = True
    return (os.path.join(out_dir, f"{stem}.torrent"),
            os.path.join(out_dir, f"{stem}.json"),
            collided)


# ★★★ `ERR-PUBLISH-22`：qB 的**下载中临时后缀**。目录里出现它们 ⇒ qB 还在下，
#   此刻制种会把临时名（`xx.flac.!qB`）固化进 `info.files`，而 qB 完成下载后会
#   **改名**成正式名 ⇒ 站点种里一个文件都对不上 ⇒ 那条种永久 `progress=0`，
#   且**全程不报错**（制种成功、发种成功、回加 HTTP 200），等发现时种已上站（不可逆）。
# ★ 判据是"**存在即拒发**"，而不是"扫目录时过滤掉" —— 只过滤的话，目录里少一个文件
#   照样能制出一份**看起来正常**的种，缺的那个要到 qB 校验时才暴露。
_TEMP_DOWNLOAD_SUFFIXES = (".!qb", ".part", ".!ut", ".!bt", ".aria2")


class IncompleteDownloadError(RuntimeError):
    """★ 目录里还有 qB 下载中临时文件 ⇒ 拒发（`ERR-PUBLISH-22`）。

    ★ 单独一类（而不是裸 `RuntimeError`）是为了让调用点能**分别措辞**：
      这一类是"还没到时候（下一轮自动好）"，别的情形是"真失败了"。
      ⇒ 两者混成一个 except 时，日志会把可自愈的情况渲染成故障，反之亦然。
    """


def find_incomplete_download_markers(content_path):
    """★ 扫 `content_path`，返回所有**下载中临时文件**的相对路径（排好序）。

    ★★ 为什么单列成函数（`ERR-PUBLISH-22` 的落地点）：
      制种（`create_musopia_torrent`）与发种是两个阶段，中间可能隔很久；只要制种那一刻
      目录里还有 `.!qB`，坏种就已经诞生且**不可逆**（本项目无删站点种能力）。
      ⇒ 闸门必须贴在**制种之前**，而且要能单独单测（判据不能只活在调用点的 `if` 里）。

    ★ 判据细节：
      · 只比**小写后缀**（qB 在不同平台写过 `.!qB`/`.!qb`）。
      · **不递归跳过隐藏目录**：`.!qB` 可能在子目录（多文件夹种）。
      · 目录不存在/不是目录 ⇒ 返回空列表（那是**别的**判据的事，别在这里越权判死）。
      · ★ 不拿"大小写"当唯一依据：`.part` 也可能是合法的专辑内文件名，但那种情况
        **极罕见**且代价不对称 —— 误拒只是少发一条（可人工确认后处理），
        漏拒则是**永久坏种**。⇒ 宁可误拒（`ERR-META-01` 一族的"代价不对称"取舍）。
    """
    hits = []
    if not content_path or not os.path.isdir(content_path):
        return hits
    for root, _dirs, files in os.walk(content_path):
        for fn in files:
            low = fn.lower()
            for suf in _TEMP_DOWNLOAD_SUFFIXES:
                if low.endswith(suf):
                    rel = os.path.relpath(os.path.join(root, fn), content_path)
                    hits.append(rel)
                    break
    hits.sort()
    return hits


def assert_no_incomplete_download(content_path):
    """★ 制种前的闸门：目录里有下载中临时文件 ⇒ **抛错拒发**（`ERR-PUBLISH-22`）。

    ★★ 为什么 raise 而不是返回 `(bool, why)`：调用点（`create_musopia_torrent`）已经
      在一个 `try/except` 里，`raise` 会让那条任务落到 `status="FAILED"` + `reason`
      —— 也就是**这条任务被明确记为失败**，而不是"悄悄跳过、下一轮再来"。
      ★ 这点是关键：`.!qB` 存在说明 qB 还在下，下一轮重扫时它多半就没了 ⇒
      如果只跳过，会**每轮静默跳过**直到碰巧干净，而用户永远看不到原因；
      记成 FAILED 才能在库里查到"这条是被什么挡下的"。

    ★ 判据（自检里必须有**两条**对照，见 `selftest` ⑨i）：
      · 目录里有 `.!qB` ⇒ 必拒（否则本条形同虚设）；
      · 干净目录（含长得像但**不是**临时后缀的名字，如 `Godzilla!.flac`）⇒ 必放行
        （否则闸门会把正常种卡死 —— `ERR-PUBLISH-21` 的教训：条件写宽一格就误判）。
    """
    hits = find_incomplete_download_markers(content_path)
    if hits:
        _show = "、".join(hits[:5])
        _more = f"…（共 {len(hits)} 个）" if len(hits) > 5 else ""
        raise IncompleteDownloadError(
            f"目录里有 qB **下载中临时文件**，此刻制种会产出永久坏种："
            f"{_show}{_more}。"
            f"⇒ 说明 qB 尚未完成下载；等下载完成后重扫即可（ERR-PUBLISH-22）")


def create_musopia_torrent(content_path, save_path):
    """★ 把 `content_path` 做成乌托邦用的 `.torrent`。

    ★★★ 制种**之前**必须先过 `assert_no_incomplete_download`（`ERR-PUBLISH-22`）：
      本函数是**全项目唯一的制种点**，一旦在这里写进 `.!qB` 名字，坏种就固化进
      站点种、不可逆。⇒ 闸门放在这里，而不是放在某个上游调用点（放上游会漏掉
      未来新增的调用者）。
    """
    assert_no_incomplete_download(content_path)
    announce = f"{config.MUSOPIA_URL}/announce.php?passkey={config.MUSOPIA_PASSKEY}"
    t = Torrent(path=content_path, trackers=[announce], private=True)
    t.generate()
    if os.path.exists(save_path):
        os.remove(save_path)
    t.write(save_path)


def _scan_source_host():
    """★ scan 要连的 qB 实例 = **皇后源**（`opencd` 分类原种/数据所在），一处口径。

    ★ 为什么单列（别在构造点再写 `config.QB_HOST`）—— 本 bug 的根因（`ERR-ORCH-02`）：
      `config.QB_HOST` 是**发种实例**（生产 3010 qbit-publish，只有 4 个种、**0 个 opencd**），
      而皇后的 `opencd` 分类种子在**另一个实例**（生产 3260 qbit-music3，1376 个 opencd）。
      原先 scan 直接 `qb_client.build(config)` ⇒ 读 `config.QB_HOST` ⇒ 每轮
      「找到 0 个分类为 [opencd] 的种子」⇒ **队列只出不进、见底**。
    ★ 口径与 `publish._opencd_content_host()` / `daemon.HANDOFF_HOST_3020` **同源**：
      优先 `config.QB_HOST_OPENCD`（唯一真值来源），退 `env`，皆无 ⇒ 兜底 **3260**（正确默认）；
      显式 override 再加一个 `QB_HOST_SCAN` 在最前（向后兼容旧双容器时代的键名）。
    ★ 它**不是凭据**（只是地址），可打印。
    """
    return (os.getenv("QB_HOST_SCAN")
            or getattr(config, "QB_HOST_OPENCD", "")
            or os.getenv("QB_HOST_OPENCD", "")
            or "http://127.0.0.1:3260")


def _opencd_tracker_host():
    """★ 识别皇后种要匹配的 **tracker 主机名**（唯一口径）。

    ★ 为什么改用 tracker 而不是 category（用户 2026-10-11 拍板，`#10 / #172`）：
      生产 3260（皇后源）上 **1433 个种里只有 6 个带 `opencd` 分类**（`category` 口径
      早已失效，`ERR-ORCH-04`）⇒ scan 每轮「找到 6 个」⇒ 队列虽未被饿死但**口径是错的**，
      任何新增的皇后种只要不带分类就会**永久漏扫**。
      ★ 实测三种信号覆盖（3260，2026-10-11）：
        · `tracker=tracker.open.cd` = **1292**（最全且语义最准 = "这个种来自皇后"）
        · qB tag `opencd`          = **0**（该实例的 tag 实际叫 `tracker.open.cd`，不是 `opencd`）
        · `category=opencd`        = **6**（旧口径）
      ⇒ 选 tracker。
    ★★ **qB 服务端的 `tracker=` 参数在本版本是**无效**的**（实测：传任何值都返回全部 1433）
      —— 所以**必须**在 Python 侧按 `torrent.tracker` 过滤，别指望 `torrents_info(tracker=…)`。
      这条是本次实打实踩出来的（`ERR-QB-09`）：**参数收下了、过滤没发生**，
      症状是"看起来过滤了，其实一条没滤"，与 `ERR-META-03` 同族（口径与实现漂移且不报错）。
    ★ 口径单列在这里（别在调用点再写字符串），并给 `OPENCD_TRACKER` 一个 env override
      以便日后皇后换 tracker 域名时不动代码。默认值 = 实测的 tracker 主机名。
    """
    return os.getenv("OPENCD_TRACKER") or getattr(config, "OPENCD_TRACKER", "") \
        or "tracker.open.cd"


def _torrent_is_opencd(torrent, tracker_host):
    """★ 判一个 qB torrent 是不是皇后种 = 它的 tracker URL 主机名等于 `tracker_host`。

    ★ 取**主机名**而不是子串：`"tracker.open.cd" in url` 会被
      `evil-tracker.open.cd` 之类的子串误命中（`ERR-META-06`：宽松匹配把"像"当成"是"）。
      ⇒ 用正则从 `announce.php` 的 URL 里抠出 `host`，再**精确相等**比较。
    ★ 拿不到 tracker（空/解析不出）⇒ **判否**（不是皇后种）：不可知的**不是**"是"。
      实测这类空 tracker 的行在 3260 上都**已在库**（115/115），判否**今天一条也漏不掉**;
      而一旦哪天真的新增了"tracker 丢了"的皇后种，它会显式不出现在列表里（可见），
      比"当成皇后种发出去"安全（同 `PENDING_PUBLISH_WHERE` 里"不知道 ⇒ 拦"的纪律）。
    """
    url = getattr(torrent, "tracker", "") or ""
    m = re.match(r"\s*https?://([^/:]+)", url)
    return bool(m) and m.group(1).lower() == (tracker_host or "").lower()


class _NoopLockHandle:
    """无 `fcntl` 平台（本机 Windows）的**空操作锁句柄**。

    ★ 只为让 `_release_scan_lock()` 有一个可 `.close()` 的对象（实测：裸 `object()`
      会让本机 selftest 在 `finally` 里 `AttributeError`）。它**不提供任何互斥** ——
      本机从不真扫，所以无所谓；生产走真 `fcntl.flock`。
    """

    def close(self):
        pass


def _scan_lock_path(data_dir=None):
    """算 scan 单实例锁文件路径。**纯函数**（可脱库单测），一处口径。

    ★ 放 `DATA_DIR` 下（= 容器内 `/data`，compose 里 bind mount 的那个目录）——
      与 `data.db` / `daemon_state.json` 同级：**同一份状态目录里的东西，才随容器一起
      持久/迁移**，也才不会落在镜像里（`ERR-DOCKER-05`）。
    """
    d = data_dir if data_dir is not None else getattr(config, "DATA_DIR", ".")
    return os.path.join(d, "scan.lock")


def acquire_scan_lock(lock_path=None, *, _fcntl=None, _open=None):
    """尝试拿 scan 的**单实例锁**。返回 `(fd, path)`；拿不到返回 `(None, path)`。

    ★★★ 为什么需要它（`ERR-ORCH-03`，2026-10-10 实测踩到）：
      `run.py`（scan）**没有单实例约束** —— daemon 每 30 分钟按周期跑一次，
      **人手动 `docker exec … python run.py` 又能再起一个**。两者**并发**时：
        · 各扫一遍 1377 个种（重复劳动，`create_musopia_torrent` 逐个全量哈希）；
        · 同时读同一台 NAS 盘上的**同一个 16GB 蓝光原盘** ⇒ 单条从数秒拖到 >7 分钟；
        · 同时写库 / 同时重写 `output_torrents/` 里**同名**的 `.torrent`/`.json`。
      实测：一次误操作起了 **3 个** 并发 scan，整轮迟迟跑不完。

    ★ 用 `fcntl.flock(LOCK_EX | LOCK_NB)`（**内核对文件句柄**加锁），而不是"写个 PID 文件
      再检查存在"——后者有 **TOCTOU 竞态**（两个进程同时"看到没有、然后都去写"），
      而且**进程被 kill 后 PID 文件会残留**、把下一轮误判成"已在跑"（`ERR-META-01`
      同族：一个会自己变脏的判据）。`flock` 随进程退出**由内核自动释放**，无残留。

    ★ 拿不到**不抛**、返回 `None` —— 由调用方决定怎么报（这里是"礼貌跳过"，不是错误；
      见 `main()`）。判据与动作分开：调用方要"跳过并打印"，本函数只答"拿到了没"。

    ★ `_fcntl` / `_open` 两个参数**只**为单测注入（默认走真 `fcntl`/真文件），
      让本函数在**没有 fcntl 的平台**（如本机 Windows 跑 selftest）也能被判定。
    """
    path = lock_path if lock_path is not None else _scan_lock_path()
    _f = _fcntl
    if _f is None:
        try:
            import fcntl as _f  # noqa: PLC0415  （函数内 import：本机 Windows 无 fcntl）
        except ImportError:
            # 没有 fcntl（本机 Windows 跑 selftest）⇒ **不装锁**、恒"拿到"。
            #   ★ 这不是"静默降级"：生产在 Linux 容器里**一定**有 fcntl，
            #     而本机跑 selftest 从不真扫（不连 qB）⇒ 无并发可言。
            #   ★ 返回**专用的无操作句柄**（有 `close()`），不要裸 `object()` ——
            #     否则 `_release_scan_lock()` 在 `finally` 里 `.close()` 会 AttributeError
            #     （实测踩到：本机 selftest 直接红）。
            return (_NoopLockHandle(), path)
    _op = _open or open
    try:
        fd = _op(path, "a+")
    except OSError:
        # 打不开锁文件（目录不存在/只读）⇒ **不因为锁而让整轮 scan 失败**
        #   （锁是护栏、不是前置条件；`ERR-META-14`：护栏失效不许静默，故由调用方打印）。
        return (None, path)
    try:
        _f.flock(fd, _f.LOCK_EX | _f.LOCK_NB)
    except OSError:
        try:
            fd.close()
        except OSError:
            pass
        return (None, path)
    return (fd, path)


def _release_scan_lock(handle):
    """释放 `acquire_scan_lock()` 拿到的锁（进程退出时内核也会自动放，这里是显式收尾）。"""
    if handle is None:
        return
    try:
        handle.close()
    except OSError:
        pass


def main():
    args = parse_args()

    config.print_runtime_info()
    db.init_db()

    # ★★★ 单实例锁（`ERR-ORCH-03`）：daemon 的周期 scan 与人手动起的 scan
    #   **绝不许并发** —— 否则重复扫 1377 个种、同读一个 16GB 原盘、同写同名产物。
    #   ★ 先锁再干活：拿不到就**礼貌退出（exit 0）**，不是错误（另一个 scan 正在干同一件事）。
    #   ★ 拿不到时**必须显式打印**（`ERR-META-14`：护栏触发不许静默）。
    #   ★ 成功路径把 handle 留在进程里（`flock` 随进程退出由内核释放，无需显式 unlock）。
    _lock_handle, _lock_path = acquire_scan_lock()
    if _lock_handle is None:
        print(f"⏭️  已有 scan 在跑（锁 {_lock_path}）⇒ 本轮跳过，不并发。"
              f"（`ERR-ORCH-03`）")
        return 0

    try:
        return _scan_main(args)
    finally:
        _release_scan_lock(_lock_handle)


def _scan_main(args):
    # ★★ scan **必须**连皇后源实例（不是发种实例！）—— `config.QB_HOST`（生产 3010）
    #   是发种实例，**没有** opencd 分类的种子；皇后源是 `QB_HOST_OPENCD`（生产 3260）。
    #   见 `_scan_source_host()` 与 ERRATA 的 ERR-ORCH-02（本 bug 的根因）。
    #   ★ 构造仍走**唯一构造点** `qb_client.build`（`ERR-QB-08`）：优先 API Key，回退账号密码。
    #   ★★ opencd 实例要用**它自己的** API Key（`QB_API_KEY_OPENCD`，与 `QB_API_KEY`=发种实例的
    #     不是同一把 —— 拿发种实例的 key 打皇后源会 403，同 `_opencd_content_host` 的教训）。
    #   ★ 用 `types.SimpleNamespace` 造一个**局部** config，**不动**全局 config.QB_HOST
    #     （那会被发种回加等其它路径读到 ⇒ 会踩坏别处）。
    import types
    import qb_client
    scan_host = _scan_source_host()
    scan_cfg = types.SimpleNamespace(
        QB_HOST=scan_host,
        QB_USER=getattr(config, "QB_USER", "admin"),
        QB_PASS=getattr(config, "QB_PASS", ""),
        QB_API_KEY=getattr(config, "OPENCD_QB_API_KEY", ""),
    )
    print(f"🔗 scan 连皇后源实例：{scan_host}")
    qb = qb_client.build(scan_cfg)
    print("✅ 已连通 qBittorrent")

    # ★★ 识别口径 = **tracker 主机名**，不是 category（用户 2026-10-11 拍板，#10/#172）。
    #   `category` 口径在 3260 上只剩 6/1433（早已失效）；tracker 才 1292（最全、语义最准）。
    #   ★ **必须在 Python 侧过滤**：qB 服务端的 `tracker=` 参数在本版本**无效**
    #     （实测传任何值都返回全部）—— 见 `_opencd_tracker_host()` 的 ERR-QB-09。
    _tracker_host = _opencd_tracker_host()
    _all = qb.torrents_info()
    torrents = [t for t in _all if _torrent_is_opencd(t, _tracker_host)]
    print(f"找到 {len(torrents)} 个 tracker=[{_tracker_host}] 的皇后种"
          f"（本实例共 {len(_all)} 个）\n")
    # ★ 阴性哨兵：若过滤后为 0 而实例非空 ⇒ **口径又漂了**（category 那次就是这样静默的）。
    #   `ERR-META-14`：0 是个必须能被看见的数字，不能只印在滚动日志里。
    if _all and not torrents:
        print(f"⚠️  ⚠️  皇后种为 0 但实例有 {len(_all)} 个种 —— 识别口径可能已失效"
              f"（tracker=[{_tracker_host}] 一个都没匹配上）\n")

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
    # ★ C'（2026-09-28）：皇后路**没写** `formatted_title` 的条数（= 库内既不是皇后原文、
    #   快照又给不出原文）。**必须是个显式数字**，不能只印在滚动日志里 ——
    #   `ERR-META-14`：兜底/跳过不许静默。
    _missing_title = 0

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
            # ★ 2026-10-14：版本标签即使在"没日志"这条早退支里也**照写** ——
            #   它是"这一行是什么版本"的**事实**，与有没有日志无关；早退支
            #   不写它，就会出现"同一条记录有的字段认版本、有的不认"的分歧。
            db.save_task(info_hash=info_hash, torrent_name=name, status="NO_LOG_FILE",
                         reason="目录里没有 .log 抓轨日志", content_path=content_path,
                         track_type=assets.cue_track_type(content_path))
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

        # ★★ 把"来源"印出来（`ERR-META-14`：来源为空不许印成"映射成功"）——
        #   否则「全 N 曲」既是"本来就没有皇后副标题"、又是"没取到皇后信息"，
        #   两种相反的结论排出来一模一样。
        if oid:
            _src = "皇后路, 取 mapped_json" if mj else "皇后路, ★ 无 snapshot ⇒ 退回曲目数"
        else:
            _src = "抓轨路(曲目数)"

        # ══════════════════════════════════════════════════════════════════
        # ★★★ C'（2026-09-28）：**皇后路不写 `formatted_title`**
        # ══════════════════════════════════════════════════════════════════
        # 病：`parse_title()` 对皇后原名做**二次拆分**（按 `-` 切 + 吞括号/方括号），
        #     产出畸形标题（实测四例见 `parse_title` 的 docstring）。删尾缀治不了它。
        #
        # 药：**这一列的唯一权威是 `opencd_ingest.build_row()`**（= `mapped.title`，
        #     皇后原文）。scan 是**重扫**，不该推翻上游写的这一列。
        #
        # ★ 为什么"不传"是安全的（**实测，不是读注释**）：`db.save_task()` 的
        #   UPSERT 只 SET **显式传入的列**（`db.py:945`）。我照抄它的 SQL 在假库上
        #   跑过，配三个阴性对照：
        #     · 不传 ⇒ 库里原值**一字不动**（且 `updated_at` 也不刷）
        #     · 传个真不同的值 ⇒ **真的改掉**（证明那条 SQL 真会写 ⇒ 上一条的
        #       "没变"是真的"没覆盖"，不是"语句没生效"）
        #     · 新行不传 ⇒ 该列 = NULL
        #   ⇒ ★★ 所以"不传"的**前提**是 `build_row` 已经写过这一列。下面对
        #     "库里也没有"的情况**不静默**（报出来），这是 `ERR-META-14` 的纪律：
        #     兜底值不许冒充真结果。
        #
        # ★ 为什么不在 `parse_title()` 里删掉拼接：**抓轨路（无 `oid`）仍然要它**。
        #   ⇒ 分流点是"谁写库"，不是"算术要不要做"。
        #
        # ★★★ 2026-09-28 **返工**：这里原先用一个 `mt_override = None` 表示"照旧写"，
        #   再用 `if mt_override:` 决定打印 —— **一个变量担了两个意思**
        #   （"Nothing" 与 "要不要写"）。实测（假库三例）立刻暴露：
        #     · 抓轨路 `oid` 为空 ⇒ 不进 `if oid` ⇒ 保持 `None` ⇒ `mt_col = {}`
        #       ⇒ **抓轨路的标题从此不再被写**（本该照写的），而且**一声不响**。
        #   ⇒ 三态（写什么 / 不写）已抽到 `_title_write_plan()`（纯函数，可脱库单测），
        #     打印读 `mt_src`（来源），**不再拿 `mt_col` 的真假当判据**。
        mt_col, mt_src, mt_why = _title_write_plan(oid, mj, formatted_title)
        if mt_why:
            print(f"   ⚠️  {mt_why}")
            _missing_title += 1

        # ★★★ 打印的**取值**只能从 `mt_col`（那才是写进库的东西）来，
        #   `mt_src` **只当标签**。两者**绝不许各读一个变量** ——
        #   实测踩过（变异 X2）：`mt_src` 说"抓轨路"、`mt_col` 却是空的（不写这一列），
        #   于是 `mt_col['formatted_title']` 抛 **KeyError** ⇒ **整个 `scan` 阶段崩掉**。
        #   ★ 那不是"测试红"，在生产里是"一轮 scan 什么都不写"。
        #   ⇒ 用 `.get(…, 默认)`：**"不写这一列"必须只能印成"(保留库内原值)"，不许崩**。
        print(f"✔ 主标题: {mt_col.get('formatted_title', '(保留库内原值)')}   ← 来源: {mt_src}")
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

        # ★★★ #136 / `ERR-PUBLISH-11`（2026-10-07）：输出名**必须对同名碰撞消歧**。
        #   旧代码只按 `formatted_title` 算名 ⇒ 不同版本同名 ⇒ 后写者覆盖前者的
        #   `.torrent`/`.json` ⇒ 站点挂错版本（实测五例）。判据与消歧见
        #   `output_sidecar_paths()` 的 docstring（纯函数，可脱库单测）。
        #   ★ `torrent_file`/`json_path` **必须用同一个消歧后的 stem** —— 两者分开算
        #     就又回到"错位"（那是本条的病根本身）。
        torrent_file, json_path, _name_collided = output_sidecar_paths(
            formatted_title, info_hash)
        if _name_collided:
            # ERR-META-14：消歧是兜底动作，**不许静默** —— 必须显式报出。
            print(f"⚠️  输出名碰撞：{formatted_title!r} 已有别的版本占用 "
                  f"⇒ 本条改用 {os.path.basename(torrent_file)} 消歧")

        print("🔨 生成种子中...")
        try:
            create_musopia_torrent(content_path, torrent_file)
            print(f"✅ 种子已生成: {torrent_file}")
        except IncompleteDownloadError as e:
            # ★ ERR-PUBLISH-22：这不是"故障"，是"还不到时候" —— 但**必须显式可见**，
            #   否则下一轮又静默跳过，用户永远不知道这条为什么一直没发出去。
            print(f"⏳ 暂缓制种：{e}")
            db.save_task(info_hash=info_hash, torrent_name=name, status="FAILED",
                         reason=str(e), content_path=content_path,
                         track_type=track_type)
            stats["failed"] += 1
            continue
        except Exception as e:
            print(f"❌ 种子生成失败: {e}")
            db.save_task(info_hash=info_hash, torrent_name=name, status="FAILED",
                         reason=str(e), content_path=content_path,
                         track_type=track_type)
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
        #
        # ★★★ 同理（C'，2026-09-28）：`formatted_title` 对皇后路也**不是 scan 该写的列**
        #   —— 它的权威是 `opencd_ingest`。⇒ 用 `**mt_col` 把"写不写标题、写什么"
        #   收成**一个显式三态**注入点（见上面那段），而不是在 SQL 层做条件。
        if not args.recheck:
            prev = db.get_status(info_hash)
            if prev in db.PROTECTED_STATUSES:
                print(f"⏭️  已有结论 status={prev!r}（非 scan 的管辖）⇒ 只刷新元数据，不动 status")
                db.save_task(
                    info_hash=info_hash,
                    torrent_name=name,
                    cover_url=cover_url,
                    torrent_file=torrent_file,
                    small_descr=small_descr,
                    tracklist=tracklist,
                    log_content=log_content,
                    log_filename=log_filename,
                    logs_json=json.dumps(logs, ensure_ascii=False),
                    content_path=content_path,
                    track_type=track_type,
                    **mt_col,
                )
                stats["ok"] += 1
                continue

        db.save_task(
            info_hash=info_hash,
            torrent_name=name,
            status="SUCCESS",
            reason="",
            cover_url=cover_url,
            torrent_file=torrent_file,
            small_descr=small_descr,
            tracklist=tracklist,
            log_content=log_content,
            log_filename=log_filename,
            logs_json=json.dumps(logs, ensure_ascii=False),
            content_path=content_path,
            track_type=track_type,
            **mt_col,
        )
        stats["ok"] += 1

        # 配套 JSON，供油猴脚本或手动发布时一键取用
        meta = {
            "info_hash": info_hash,
            "torrent_name": name,
            "formatted_title": formatted_title,
            "track_type": track_type,
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
            # ★ 用**与 .torrent 同源**的 `json_path`（`output_sidecar_paths` 的产物）
            #   —— 绝不再各算一次 stem（那样二者会错位，正是 #136 的本病）。
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️  JSON 写入失败: {e}")

    print("\n" + "=" * 60)
    print(f"🎯 扫描完成：新处理 {stats['ok']} 个 | 缓存跳过 {stats['skipped']} 个 | "
          f"无日志 {stats['no_log']} 个 | 已知无日志(短路) {stats['skipped_no_log']} 个 | "
          f"失败 {stats['failed']} 个")
    # ★ C'：把"没写标题"显式报出来。★ 它**不是**失败（发布不会被它拦下，
    #   因为 `publish.py` 渲染时也是从 `mapped_json` 取皇后原文），
    #   但它是"快照缺失/字段改名"的**唯一可见信号** ⇒ 报 0 也要印。
    if _missing_title:
        print(f"⚠️  其中 **{_missing_title}** 条是皇后路、但**没写** `formatted_title`"
              f"（快照缺失或 `mapped_json.title` 为空）。")
        print("   ⇒ 这两条原因**处置不同**：无快照 ⇒ 跑 `crawl`；有快照但 title 空 ⇒ 查皇后字段名。")
        print("   ⇒ ★ 它们**不会**被 scan 用一个畸形标题顶替 —— 宁可留空（`ERR-META-14`）。")
    else:
        print("✔ 皇后路标题零条缺失（本轮没有「该写却写不出」的行）")
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
    import sys as _sys
    if "--selftest" in _sys.argv[1:]:
        # ★ 自检走**独立出口**（照 `cover_fetch.py` / `descr.py` 的成例，含 `--json`）：
        #   它**不 import config**（那是要 QB_PASS 的）⇒ 本机/容器里都能跑。
        _sys.stdout.reconfigure(encoding="utf-8")
        _ok, _detail = selftest()
        if "--json" in _sys.argv[1:]:
            import json as _json
            print(_json.dumps({"script": "run.py --selftest", "ok": _ok,
                               "total": 0, "exit": 0 if _ok else 1},
                              ensure_ascii=False))
            _sys.exit(0 if _ok else 1)
        print("=" * 74)
        print("run.py 离线自检 —— C' 标题三态（纯本地，**不连 qB / 不连库 / 零请求**）")
        print("=" * 74)
        print("  ① 抓轨路           → 写 parse_title() 的产物")
        print("  ② 皇后路 + 有快照   → 写 mapped_json.title（皇后原文）")
        print("  ③ 皇后路 + 无快照   → **不写**，且回报原因")
        print("  ④ 皇后路 + title 空 → 与③同判据、不同原因")
        print("  ⑤ 损坏快照不抛异常  ⑥ title=0 不冒充原文  ⑦ 两个注入点都在")
        print("  ⑦e 三个 WHERE 常量不许漂移（QUEEN / TRANSFERABLE 都必须以 "
              "CROSS 逐字开头，")
        print("      且 TRANSFERABLE 的尾巴**逐字**等于闸门④ 那条 ALLOW）")
        print("  ⑦e-3 这些常量必须能被 ast.literal_eval 静态解出"
              "（否则靠 AST 抽常量的那 4 个工具会在运行时才炸）")
        print("  ⑧ qB 构造点唯一：qb_client.py 之外零 `Client(` / 零 `auth_log_in`"
              "（ERR-QB-08：别让'第 14 处'长回来）")
        print("  ⑧b 六个 opencd_* 旧模块名零 import（已并入 opencd.py，防复制粘贴回来）")
        print("  C1 QB_HOST 兜底三处一致 + 空串回落 + 进 env 快照"
              "（ERR-META-33：改生产值就够的改动都是可疑的）")
        print()
        if _detail:
            print("\n".join(_detail))
            print()
        print("=" * 74)
        if _ok:
            print("✅ 自检通过")
        else:
            print(f"❌ 自检失败：{len(_detail)} 项不符")
        _sys.exit(0 if _ok else 1)
    main()
