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
    #     · `torrent_file` 的文件名由 `safe_name` 算（看下面）⇒ 库里那个路径会
    #       在下一轮 `scan` 里被重算覆盖，`save_task` 会连 `updated_at` 一起刷新。
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
        import ast as _ast9
        import pathlib as _pl9
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
            for _want in ("_readd_thread_loop", "_handoff_thread_loop"):
                if _want not in _targets:
                    ok[0] = False
                    detail.append(f"✗ ⑨f daemon.py 没有 `Thread(target={_want}...)` 调用点 "
                                  f"⇒ 该线程**定义了但没接线**（实测 target={sorted(_targets)}）")
            # ★ 且这两个函数真的**有定义**（防止 target 指向不存在的名字：只在运行期炸）
            _defs = {_n.name for _n in _ast9.walk(_dtree)
                     if isinstance(_n, _ast.FunctionDef)}
            for _want in ("_readd_thread_loop", "_handoff_thread_loop"):
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
        try:
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
        finally:
            _ho.config.PUBLISH_CONTAINER_DIR = _saved_pub3
            _ho.config.ENABLE_HANDOFF = _saved_handoff3
            _ho.config.HANDOFF_DRY_RUN = _saved_dry3
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
    except Exception as e:                                        # noqa: BLE001
        ok[0] = False
        detail.append(f"✗ ⑨ 交棒自检执行失败: {type(e).__name__}: {e}")

    return ok[0], detail


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

    # ★★ 唯一构造点 `qb_client.build`（`ERR-QB-08`）：优先 API Key，回退账号密码。
    #   ★ 这里**刻意不加 try/except** —— 老代码也是硬崩（本阶段要 qB 才能干活，
    #     连不上就该让整条 main 停），`build()` 连不上会抛 ⇒ 契约不变。
    import qb_client
    qb = qb_client.build(config)
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
            **mt_col,
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
