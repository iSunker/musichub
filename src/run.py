"""
扫描 qBittorrent 里指定分类的种子，提取元数据（标题/副标题/曲目/日志/封面），
生成 Musopia 用的种子文件，写入数据库等待 publish.py 发布。
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

    # 版本标签只认 assets.cue_track_type() 强判据（cue 的 FILE 目标必须真存在才算整轨）
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
    # 标题拼接：核心名称、年份、格式与音轨类型
    formatted_title = f"{core} {year} - {fmt} {track_type}"
    formatted_title = re.sub(r"\s+", " ", formatted_title).strip()

    # 副标题由 main() 取得 tracklist/logs 后调用 descr.scan_small_descr() 生成
    return formatted_title, fmt, track_type


def _json_title(mapped_json):
    """从 mapped_json 快照中提取原文标题（mapped.title），取不到返回空字符串。"""
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
    """决定当前任务是否写入 formatted_title 以及写入的值。

    返回 (cols, src, why_missing):
      - cols: 注入 db.save_task(**cols) 的字段字典（空 dict 表示不写此列）
      - src: 来源说明标签
      - why_missing: 缺失原因（正常时为 None）

    分支规则：
      1. 抓轨路（无 oid）：写入 parse_title() 解析标题
      2. 皇后路 + 存在快照标题：写入 mapped_json 中的原文标题
      3. 皇后路 + 无快照或标题为空：不写入此列（保留已有值），并返回原因说明
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
    """离线自检（纯本地：不连 qB、不连库、不发请求）。返回 (是否通过, 明细)。"""
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

    # ── ① 抓轨路：必须写入 parse_title() 解析结果 ──
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

    # ── ④ 皇后路 + 有快照但无有效 title：不写并返回原因 ──
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

    # ── ⑥ 快照内非字符串标题校验 ──
    cols, src, why = _title_write_plan("12345", json.dumps({"title": 0}), PARSED)
    chk("⑥快照 title=0 不许当原文", cols, {})

    # ── ⑦ 结构性判据：每一个 save_task 调用点都必须带 **mt_col ──
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

    def _status_writes(fn_name):
        """所有同时包含 status= 和 torrent_file= 的 save_task 调用行号。"""
        return [n.lineno for n in _ast.walk(_tree)
                if (isinstance(n, _ast.Call)
                    and getattr(n.func, "attr", None) == fn_name
                    and any(k.arg == "status" for k in n.keywords)
                    and any(k.arg == "torrent_file" for k in n.keywords))]

    _sites = _starred_kwargs("save_task")
    _writes = _status_writes("save_task")
    _missing_sites = [ln for ln in _writes if ln not in _sites]
    if len(_sites) < 2 or _missing_sites:
        ok[0] = False
        detail.append(f"✗ ⑦`**mt_col` 注入点应 ≥2 处且覆盖每个写支，"
                      f"实测 {len(_sites)} 处（AST 判据，行号 {_sites}）；"
                      f"带 status= 的 save_task 有 {len(_writes)} 处"
                      f"（行号 {_writes}），**漏注入**的行号 {_missing_sites}")

    # ⑦b 数据流判据：断言右值来自 _title_write_plan
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

    # ⑦c 判每个写支各自都被注入
    _scoped = {n.lineno for n in _ast.walk(_tree)
               if isinstance(n, _ast.Call) and getattr(n.func, "attr", None) == "save_task"}
    _lines = sorted(set(_sites))
    _near = {ln: [sl for sl in _scoped if abs(ln - sl) <= 12] for ln in _lines}
    if len(_lines) < 2 or any(not v for v in _near.values()) \
            or len({tuple(v) for v in _near.values()}) < 2:
        ok[0] = False
        detail.append(f"✗ ⑦c `**mt_col` 注入点疑似集中在**同一处** `save_task` 里"
                      f"（近似归属 {_near}）⇒ 至少一个写支漏了注入")

    # ⑦d 判 mt_col 不许被裸下标取键
    _bad_sub = [n.lineno for n in _ast.walk(_tree)
                if isinstance(n, _ast.Subscript)
                and getattr(n.value, "id", "") == "mt_col"]
    if _bad_sub:
        ok[0] = False
        detail.append(f"✗ ⑦d `mt_col` 被**裸下标**取键（行 {_bad_sub}）⇒ "
                      "`mt_col` 为空（= 不写这一列）时会 KeyError 崩掉整个 scan。"
                      "改用 `mt_col.get('formatted_title', '(保留库内原值)')`。")

    # ⑦e 判 WHERE 常量前缀一致性
    _prefix_checks = [
        # (子常量名, 父常量名, 尾巴必须谈的词, 尾巴**不许**重复的词)
        ("QUEEN_PUBLISHED_WHERE", "CROSS_SITE_DEDUP_WHERE", "opencd_tid",
         ("site_status", "site_torrent_id")),
        ("TRANSFERABLE_WHERE", "CROSS_SITE_DEDUP_WHERE", "transfer_rule",
         ("site_status", "site_torrent_id", "opencd_tid")),
        # CRABPT_QUEUE_WHERE 必须以 TRANSFERABLE_WHERE 逐字开头，追加 status 条件
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
                              f"⇒ 前缀判据**没测到东西**")
                continue
            _tail = _kv[len(_dv):] if _kv.startswith(_dv) else None
            if _tail is None:
                ok[0] = False
                detail.append(f"✗ ⑦e `db.{_kid}` 不以 `db.{_dad}` **逐字开头**：\n"
                              f"          {_kid} = {_kv!r}\n"
                              f"          {_dad} = {_dv!r}\n"
                              f"     ⇒ 两个口径漂移了。")
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

        # ⑦e-3 常量必须能被 ast.literal_eval 直接解出
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
                          f"（这条判据**没测**，不是通过）")
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

    # ⑧ qB 客户端构造点收敛校验：除工厂文件外不得出现裸 Client 构造或 auth_log_in
    import pathlib as _pl2
    # 确定扫描根目录（适配开发机源码树与容器内平铺形态）
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
            f"（未检出）。检查 run.py 的 _ROOT 推导。")
    # ★ 要跳过的目录名（出现在路径的**任意层级**即跳过该文件）。
    #   ★★ 故意写得**宽**（宁可跳过也不误报）：判据的价值在"挡住新写的构造点"，
    #     不在"审第三方的 .venv / 别的项目源码"。
    _SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
                  ".scratch", ".deploy-backup", ".claude", ".mypy_cache",
                  ".pytest_cache", ".idea", ".vscode", "repos"}
    # 根目录下备份与历史目录跳过
    _SKIP_DIR_SUFFIX = ("_bak", "_backup", "_old", "-bak", "-backup")
    # 定位工厂文件路径
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
            f"✗ ⑧ 找不到工厂 {_FACTORY.name} ⇒ 本判据**没测到东西**，"
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
                continue                      # 读不到 = 判据失效，**不并进"没有"**
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
                "     ★ 修法：换成 `import qb_client` + `qb = qb_client.build(config)`"
                "（key 优先 / 回退账号密码），把构造收敛回那个**唯一**工厂。")

    # ⑧b 废弃旧模块 import 检查：旧模块已合并至 opencd.py
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

    # ⑨ 交棒判据与清盘护栏离线自检
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

        # ── ⑨g 隔离闸门：只有白名单内分类允许交棒与清盘 ──
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
        # 结构性判据：handoff_to_3020 / finalize_source 必须调用 is_own_project
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

        # ── ⑨f 结构性判据：daemon 必须挂接全部后台线程 ──
        import ast as _ast9
        import pathlib as _pl9
        _WANT_THREADS = ("_readd_thread_loop", "_handoff_thread_loop",
                         "_seed_patrol_thread_loop", "_counts_thread_loop")
        _daemon_py = _pl9.Path(__file__).resolve().parent / "daemon.py"
        if not _daemon_py.exists():
            ok[0] = False
            detail.append(f"✗ ⑨f 找不到 daemon.py（{_daemon_py}）⇒ 判据没测到东西")
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

        # ── ⑨h 数据完整性校验：数据未到位严禁回加 ──
        _tmp3 = _tf.mkdtemp(prefix="handoff-134-selftest-")
        _pub_root3 = os.path.join(_tmp3, "publish")
        _saved_pub3 = _ho.config.PUBLISH_CONTAINER_DIR
        _saved_handoff3 = _ho.config.ENABLE_HANDOFF
        _saved_dry3 = _ho.config.HANDOFF_DRY_RUN
        # 使用临时队列文件隔离测试副作用
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
                              "")

        # ── ⑨h-2 逐片校验：分块哈希一致性检查 ──
            import torf as _torf9h
            _pp = os.path.join(_tmp3, "pieces")
            _srcP = os.path.join(_pp, "hdd", "AlbumP")
            os.makedirs(_srcP)
        # 构造内容不同的测试文件
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

        # ② 阴性对照：文件集合与字节数相等但内容顺序不同，逐片校验必须报错
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
                              "⇒ 它退化成了'文件数+字节数'")

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

        # ── ⑨i 回加复核判据：对未就绪与下载中状态必须报错 ──
        import pathlib as _pl9i
        _pub_py = _pl9i.Path(__file__).resolve().parent / "publish.py"
        _pub_src = _pub_py.read_text(encoding="utf-8") if _pub_py.exists() else ""
        if not _pub_src:
            ok[0] = False
            detail.append(f"✗ ⑨i 找不到 publish.py（{_pub_py}）⇒ 判据没测到东西")
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

        # ── ⑨k 失败保护：FAILED 条目不得直接清盘 ──
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

        # ── ⑨m 延迟回加前必须核对发种区就位 ──
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

        # ── ⑨l 达标交棒判据校验 ──
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

        # ── ⑨n 多版本同名输出碰撞防护 ──
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
        # 过滤注释后检查源码中是否存在旧命名模式
        _run_code_lines = []
        for _ln in _run_src.splitlines():
            _code = _ln.split("#", 1)[0]
            _run_code_lines.append(_code)
        _run_code = "\n".join(_run_code_lines)
        if re.search(r"safe_name\s*=\s*re\.sub\(", _run_code):
            ok[0] = False
            detail.append("✗ ⑨n run.py 仍按 `safe_name` 只从标题算输出名 ⇒ 同名版本又互踏"
                          "")
        if "output_sidecar_paths(" not in _run_code:
            ok[0] = False
            detail.append("✗ ⑨n run.py 里没有 `output_sidecar_paths(` ⇒ 消歧护栏没接线")

        # ⑨o 源码护栏：edit_torrent 必须拒绝文件类变更
        _mus_py = _pl9i.Path(__file__).resolve().parent / "musopia.py"
        _mus_src = _mus_py.read_text(encoding="utf-8") if _mus_py.exists() else ""
        _mus_code = "\n".join(_l.split("#", 1)[0] for _l in _mus_src.splitlines())
        if not re.search(r"EDIT_CANNOT_CHANGE_TORRENT\s*=\s*True\b", _mus_code):
            ok[0] = False
            detail.append("✗ ⑨o musopia.py 没有 `EDIT_CANNOT_CHANGE_TORRENT = True` ⇒ "
                          "『edit 改不了挂的种子』这条事实没被钉住")
        if "EDIT_FILE_LIKE_KEYS" not in _mus_code:
            ok[0] = False
            detail.append("✗ ⑨o `edit_torrent` 未拒绝 file 类 overrides ⇒ "
                          "改种子会**静默假成功**")

        # ── ⑨p scan 实例连接口径校验 ──
        import ast as _ast9p
        _run_tree = _ast9p.parse(_pl9i.Path(__file__).resolve().read_text(encoding="utf-8"))

        def _find_func(tree, name):
            for node in _ast9p.walk(tree):
                if isinstance(node, _ast9p.FunctionDef) and node.name == name:
                    return node
            return None

        # (a) scan 主流程不得直接调用 qb_client.build(config)
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
                          "队列饿死")

        # (b) scan 主流程里必须出现 `_scan_source_host` 这个名字
        _uses_scan_host = False
        for _fn in _scan_fns:
            for node in _ast9p.walk(_fn):
                if isinstance(node, _ast9p.Name) and node.id == "_scan_source_host":
                    _uses_scan_host = True
        if not _uses_scan_host:
            ok[0] = False
            detail.append("✗ ⑨p scan 主流程未经 `_scan_source_host()` 取地址 ⇒ "
                          "scan 源不再被单独钉住")

        # (c) `_scan_source_host()` 的 or 链里 `QB_HOST_OPENCD` 必须排在裸 `QB_HOST` 前
        _ssh_fn = _find_func(_run_tree, "_scan_source_host")
        if _ssh_fn is None:
            ok[0] = False
            detail.append("✗ ⑨p 找不到 `_scan_source_host()`")
        else:
            # 检查 return 表达式中的主机常量引用顺序
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
                              "scan 会用发种实例当源")
            # 裸 QB_HOST（逐字，排除 QB_HOST_SCAN / QB_HOST_OPENCD）若出现，必须在 OPENCD 之后
            _idx_opencd = next((i for i, v in enumerate(_lit) if "QB_HOST_OPENCD" in v), None)
            _idx_bare = next((i for i, v in enumerate(_lit) if v == "QB_HOST"), None)
            if _idx_opencd is not None and _idx_bare is not None and _idx_bare < _idx_opencd:
                ok[0] = False
                detail.append("✗ ⑨p `_scan_source_host()` 的 return 里裸 `QB_HOST` 排在 "
                              "`QB_HOST_OPENCD` 之前 ⇒ 源优先级反了")
    except Exception as e:                                        # noqa: BLE001
        ok[0] = False
        detail.append(f"✗ ⑨ 交棒自检执行失败: {type(e).__name__}: {e}")

    # ── ⑨q scan 单实例文件锁自检 ──
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

    # ── ⑨r 站点种子识别校验：按 tracker 精确匹配 ──
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

    # ── ⑨s 查重结构护栏校验 ──
    try:
        import ast as _ast9s
        _mus_py9 = _pl9i.Path(__file__).resolve().parent / "musopia.py"
        _mus_txt9 = _mus_py9.read_text(encoding="utf-8") if _mus_py9.exists() else ""
        if not _mus_txt9:
            ok[0] = False
            detail.append("✗ ⑨s 找不到 musopia.py ⇒ 判据没测到东西")
        else:
            _sac_src = ""
            for _n in _ast9s.walk(_ast9s.parse(_mus_txt9)):
                if isinstance(_n, _ast9s.FunctionDef) and _n.name == "search_and_compare":
                    _seg = _ast9s.get_source_segment(_mus_txt9, _n) or ""
                    _sac_src = "\n".join(l.split("#", 1)[0] for l in _seg.splitlines())
                    break
            if not _sac_src:
                ok[0] = False
                detail.append("✗ ⑨s 找不到 search_and_compare 函数体")
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

    # 兜底默认值一致性与配置导出校验
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
                          " ⇒ 本判据没测到东西")
        else:
            _qbd = _m_def.group(1)
            # 容器环境未包含 docker-compose.yml 时跳过文件检查
            _compose = _root1 / "docker-compose.yml"
            _sub = _root1 / "src" / "docker-compose.yml"      # 防备别处布局
            if not _compose.exists():
                _compose = _sub if _sub.exists() else None
            if _compose is None:
                # ★ 只有"本机仓库缺失"才是异常；容器内缺 compose 是**设计如此**。
                if (_root1 / "src").is_dir():
                    ok[0] = False
                    detail.append("✗ C1(a) 本机仓库里找不到 docker-compose.yml"
                                  " ⇒ 判据没测到东西")
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
        # (c) 空串回落默认值校验
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

    # ── ⑨i 临时下载文件拦截闸门校验 ──
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

        # ── ② 阴性对照：干净目录放行 ──
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

        # ── ③ 结构性判据：制种流程前置挂接下载检查闸门 ──
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

    # ── ⑨j 选种硬门槛自检：未下载完成的种子拒绝选入 ──
    from types import SimpleNamespace as _SN
    _t_ok = _SN(progress=1.0, amount_left=0, state="uploading",
                content_path="/volume1/music/Album", save_path="/volume1/music")
    _t_prog = _SN(progress=0.999, amount_left=0, state="uploading",
                  content_path="/volume1/music/Album", save_path="/volume1/music")
    _t_left = _SN(progress=1.0, amount_left=1024, state="uploading",
                  content_path="/volume1/music/Album", save_path="/volume1/music")
    _t_dl = _SN(progress=1.0, amount_left=0, state="downloading",
                content_path="/volume1/music/Album", save_path="/volume1/music")
    _t_inc = _SN(progress=1.0, amount_left=0, state="uploading",
                 content_path="/volume1/incomplete/Album", save_path="/volume1/incomplete")

    _ok_res, _ = _torrent_is_download_completed(_t_ok)
    chk("⑨j 阴性对照：完工种子放行", _ok_res, True)
    _prog_res, _ = _torrent_is_download_completed(_t_prog)
    chk("⑨j 进度未达100%拦截", _prog_res, False)
    _left_res, _ = _torrent_is_download_completed(_t_left)
    chk("⑨j 剩余字节>0拦截", _left_res, False)
    _dl_res, _ = _torrent_is_download_completed(_t_dl)
    chk("⑨j 下载状态拦截", _dl_res, False)
    _inc_res, _ = _torrent_is_download_completed(_t_inc)
    chk("⑨j 临时目录拦截", _inc_res, False)

    # 结构性判据：main() 必须调用 _torrent_is_download_completed
    _calls_gate = [n.lineno for n in _ast.walk(_tree)
                   if isinstance(n, _ast.Call) and getattr(n.func, "id", "") == "_torrent_is_download_completed"]
    if len(_calls_gate) < 1:
        ok[0] = False
        detail.append("✗ ⑨j main() 中没有调用 _torrent_is_download_completed ⇒ 选种门槛未接线")
    else:
        detail.append(f"ℹ ⑨j 结构：选种门槛已接线（调用点行号 {_calls_gate}）✅")

    return ok[0], detail


def _safe_output_stem(formatted_title):
    """把标题转换为安全文件名 stem（过滤非法字符）。"""
    return re.sub(r'[\\/*?:"<>|]', "_", formatted_title or "")


def _hash_suffix(info_hash):
    """取 info_hash 的前 8 位短后缀，用于同名碰撞时消歧。"""
    h = (info_hash or "").strip().lower()
    return h[:8] if h else "nohash"


def output_sidecar_paths(formatted_title, info_hash, out_dir=None,
                         *, exists=None, read_json=None):
    """计算当前种子的输出 .torrent / .json 路径，并对同名碰撞消歧。

    返回 (torrent_path, json_path, collided):
      - 若同名 sidecar 中的 info_hash 与当前种子不一致，则追加短 hash 后缀消歧；
      - collided: 是否发生同名消歧。
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
            # 解析失败时保留默认命名，不生成异常后缀
            prev_hash = None
        if prev_hash is not None and prev_hash != (info_hash or "").strip().lower():
            stem = f"{stem}_{_hash_suffix(info_hash)}"
            collided = True
    return (os.path.join(out_dir, f"{stem}.torrent"),
            os.path.join(out_dir, f"{stem}.json"),
            collided)


# 下载中临时文件后缀：命中则表示仍在下载中，拒绝制种以避免坏种
_TEMP_DOWNLOAD_SUFFIXES = (".!qb", ".part", ".!ut", ".!bt", ".aria2")


class IncompleteDownloadError(RuntimeError):
    """目录内存在下载中临时文件时的异常。"""


def find_incomplete_download_markers(content_path):
    """扫描 content_path，返回所有下载中临时文件（如 .!qB, .part 等）的相对路径列表。"""
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
    """制种前检查：若目录内存在下载中临时文件则抛出 IncompleteDownloadError。"""
    hits = find_incomplete_download_markers(content_path)
    if hits:
        _show = "、".join(hits[:5])
        _more = f"…（共 {len(hits)} 个）" if len(hits) > 5 else ""
        raise IncompleteDownloadError(
            f"目录里有 qB **下载中临时文件**，此刻制种会产出永久坏种："
            f"{_show}{_more}。"
            f"⇒ 说明 qB 尚未完成下载；等下载完成后重扫即可")


def create_musopia_torrent(content_path, save_path):
    """将 content_path 制作成 .torrent 种子文件并保存至 save_path。"""
    assert_no_incomplete_download(content_path)
    announce = f"{config.MUSOPIA_URL}/announce.php?passkey={config.MUSOPIA_PASSKEY}"
    t = Torrent(path=content_path, trackers=[announce], private=True)
    t.generate()
    # 制种后立即执行分块物理反验
    try:
        if not t.verify(content_path):
            raise IncompleteDownloadError(f"制种后分块物理校验未通过（{content_path}）")
    except IncompleteDownloadError:
        raise
    except Exception as _e_v:
        raise IncompleteDownloadError(f"制种后分块物理校验失败（疑似下载中数据变动）: {_e_v}") from _e_v

    if os.path.exists(save_path):
        os.remove(save_path)
    t.write(save_path)


def _scan_source_host():
    """获取 scan 待连接的源 qBittorrent 实例地址。"""
    return (os.getenv("QB_HOST_SCAN")
            or getattr(config, "QB_HOST_OPENCD", "")
            or os.getenv("QB_HOST_OPENCD", "")
            or "http://127.0.0.1:3260")


def _opencd_tracker_host():
    """获取识别源站点种子的 tracker 主机名。"""
    return os.getenv("OPENCD_TRACKER") or getattr(config, "OPENCD_TRACKER", "") \
        or "tracker.open.cd"


def _torrent_is_opencd(torrent, tracker_host):
    """判断种子 tracker 是否匹配指定主机名。"""
    url = getattr(torrent, "tracker", "") or ""
    m = re.match(r"\s*https?://([^/:]+)", url)
    return bool(m) and m.group(1).lower() == (tracker_host or "").lower()


# 做种/已完成状态族（与 handoff / site_readd 一致）
_SEEDING_OR_COMPLETED_STATES = {
    "stalledUP", "uploading", "forcedUP", "queuedUP", "checkingUP", "pausedUP", "stoppedUP"
}


def _torrent_is_download_completed(torrent):
    """判断种子是否已完全下载完成（进度 100%、剩余 0 字节、非下载状态、非临时目录）。"""
    # 1. 进度检查：必须 100%
    prog = getattr(torrent, "progress", None)
    if prog is not None and prog < 1.0:
        return False, f"progress={prog:.1%} < 100%"

    # 2. 剩余字节：必须为 0
    left = getattr(torrent, "amount_left", None)
    if left is not None and left > 0:
        return False, f"amount_left={left} > 0"

    # 3. 状态检查：排除任何下载态与未就绪态
    state = str(getattr(torrent, "state", "") or "")
    if state and state not in _SEEDING_OR_COMPLETED_STATES:
        return False, f"state={state} (非做种/未完成状态)"

    # 4. 路径检查：不能在 incomplete 临时目录中
    cp = str(getattr(torrent, "content_path", "") or "")
    sp = str(getattr(torrent, "save_path", "") or "")
    if "/incomplete/" in cp.replace("\\", "/") or "/incomplete/" in sp.replace("\\", "/"):
        return False, f"content_path 在临时目录: {cp}"

    # 5. 实体检查：若本地路径存在，检查是否残留下载未完成临时文件
    if cp and os.path.exists(cp):
        hits = find_incomplete_download_markers(cp)
        if hits:
            return False, f"目录含临时下载标记文件: {hits[0]}"

    return True, ""


class _NoopLockHandle:
    """非 fcntl 平台下的空操作锁句柄。"""

    def close(self):
        pass


def _scan_lock_path(data_dir=None):
    """计算 scan 进程单实例锁文件路径。"""
    d = data_dir if data_dir is not None else getattr(config, "DATA_DIR", ".")
    return os.path.join(d, "scan.lock")


def acquire_scan_lock(lock_path=None, *, _fcntl=None, _open=None):
    """尝试获取 scan 单实例文件锁。成功返回 (fd, path)，失败返回 (None, path)。"""
    path = lock_path if lock_path is not None else _scan_lock_path()
    _f = _fcntl
    if _f is None:
        try:
            import fcntl as _f  # noqa: PLC0415  （函数内 import：本机 Windows 无 fcntl）
        except ImportError:
            # 无 fcntl 平台返回空操作锁句柄
            return (_NoopLockHandle(), path)
    _op = _open or open
    try:
        fd = _op(path, "a+")
    except OSError:
        # 无法打开锁文件时不阻断流程
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

    # 单实例锁：避免周期性扫描与手动执行并发冲突
    _lock_handle, _lock_path = acquire_scan_lock()
    if _lock_handle is None:
        print(f"⏭️  已有 scan 在跑（锁 {_lock_path}）⇒ 本轮跳过，不并发。"
              f"")
        return 0

    try:
        return _scan_main(args)
    finally:
        _release_scan_lock(_lock_handle)


def _scan_main(args):
    # scan 进程连接源 qBittorrent 实例
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

    # 按 tracker 主机名过滤源站点种子
    _tracker_host = _opencd_tracker_host()
    _all = qb.torrents_info()
    torrents = [t for t in _all if _torrent_is_opencd(t, _tracker_host)]
    print(f"找到 {len(torrents)} 个 tracker=[{_tracker_host}] 的皇后种"
          f"（本实例共 {len(_all)} 个）\n")
    # 过滤结果为空时发出告警提示
    if _all and not torrents:
        print(f"⚠️  ⚠️  皇后种为 0 但实例有 {len(_all)} 个种 —— 识别口径可能已失效"
              f"（tracker=[{_tracker_host}] 一个都没匹配上）\n")

    # 选种硬门槛：排除所有未完全下载完成的种子
    _ready = []
    _incomplete_rejected = 0
    for t in torrents:
        _ok, _reason = _torrent_is_download_completed(t)
        if _ok:
            _ready.append(t)
        else:
            _incomplete_rejected += 1
    if _incomplete_rejected > 0:
        print(f"🛡️ 选种门槛：已过滤 {_incomplete_rejected} 个尚未完全下载完成的种子\n")
    torrents = _ready

    if args.limit:
        torrents = torrents[:args.limit]

    # 批量加载已完成任务的状态与元数据
    conn = db.connect()
    cur = conn.cursor()
    cur.execute("SELECT info_hash, status, torrent_file, site_status, cover_url, "
                "opencd_tid, mapped_json, transfer_rule FROM tasks")
    _rows = cur.fetchall()
    known = {r[0]: (r[1], r[2], r[3], r[4], r[5], r[6]) for r in _rows}
    # 维护 transfer_rule 映射字典
    _known_rule = {r[0]: r[7] for r in _rows}
    conn.close()

    stats = {"skipped": 0, "no_log": 0, "ok": 0, "failed": 0, "skipped_no_log": 0}
    # 统计未写入标题的任务计数
    _missing_title = 0

    for torrent in torrents:
        info_hash = torrent.hash
        name = torrent.name
        content_path = torrent.content_path
        _seed_days = round(getattr(torrent, "seeding_time", 0) / 86400.0, 2)

        # 选种硬门槛：排除未完全下载完成的种子
        _comp_ok, _comp_why = _torrent_is_download_completed(torrent)
        if not _comp_ok:
            print(f"🛑 选种门槛拦截：{name} 未完全下载（{_comp_why}）⇒ 跳过处理")
            stats["skipped"] += 1
            continue

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

            # 已知无日志且已制种的种子跳过重复处理
            if status == "NO_LOG_FILE" and tfile and os.path.exists(tfile):
                stats["skipped_no_log"] += 1
                continue

        print("-" * 60)
        print(f"扫描: {name}")

        logs = assets.read_logs(content_path)
        _has_log = bool(logs)
        if not _has_log:
            print("❌ 跳过：目录里没有 .log 抓轨日志")
            # 记录版本类型
            db.save_task(info_hash=info_hash, torrent_name=name, status="NO_LOG_FILE",
                         reason="目录里没有 .log 抓轨日志", content_path=content_path,
                         track_type=assets.cue_track_type(content_path),
                         qb_seed_days=_seed_days)
            stats["no_log"] += 1
            # transfer_rule 为 ALLOW 时允许无日志制种
            _rule = _known_rule.get(info_hash)
            if (_rule or "").strip() != "ALLOW":
                print(f"   ⏭  无 log 且 transfer_rule={_rule!r} 非 ALLOW"
                      f" ⇒ 不制种（制出来也永远发不出去）")
                continue
            print("   ⏩ 无 log 但 transfer_rule='ALLOW' ⇒ **继续制种**"
                  "（供无需日志的站点直发）")

        # 兼容性日志字段提取
        if _has_log:
            log_content, log_filename = logs[0]["content"], logs[0]["filename"]
        else:
            log_content, log_filename = "", ""

        formatted_title, fmt, track_type = parse_title(name, content_path)
        tracklist = assets.read_tracklist(content_path)

        # 副标题根据来源站点与元数据分流生成
        oid = known[info_hash][4] if info_hash in known else None
        mj = known[info_hash][5] if info_hash in known else None
        small_descr = descr.scan_small_descr(
            tracklist, disc_count=len(logs), fallback=name,
            mapped_json=mj, is_opencd=bool(oid))

        # 记录副标题来源
        if oid:
            _src = "皇后路, 取 mapped_json" if mj else "皇后路, ★ 无 snapshot ⇒ 退回曲目数"
        else:
            _src = "抓轨路(曲目数)"

        # 规划标题写入方案（三态分流）
        mt_col, mt_src, mt_why = _title_write_plan(oid, mj, formatted_title)
        if mt_why:
            print(f"   ⚠️  {mt_why}")
            _missing_title += 1

        # 格式化主标题输出信息
        print(f"✔ 主标题: {mt_col.get('formatted_title', '(保留库内原值)')}   ← 来源: {mt_src}")
        print(f"✔ 副标题: {small_descr or '(空)'}   ← 来源: {_src}")
        if _has_log:
            print(f"📄 日志: {len(logs)} 个 [" + ", ".join(x['label'] for x in logs) + "]")
        else:
            # 无日志制种提示
            print("📄 日志: 0 个（★ 本条无抓轨日志，但 `transfer_rule='ALLOW'` ⇒ 照常制种）")
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

        # 计算输出文件名并进行同名消歧
        torrent_file, json_path, _name_collided = output_sidecar_paths(
            formatted_title, info_hash)
        if _name_collided:
            # 发生输出名碰撞时记录提示
            print(f"⚠️  输出名碰撞：{formatted_title!r} 已有别的版本占用 "
                  f"⇒ 本条改用 {os.path.basename(torrent_file)} 消歧")

        print("🔨 生成种子中...")
        try:
            create_musopia_torrent(content_path, torrent_file)
            print(f"✅ 种子已生成: {torrent_file}")
        except IncompleteDownloadError as e:
            # 临时下载文件拦截，记录暂缓制种
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

        # 无日志种子保持 NO_LOG_FILE 状态保存，供对应站点直发
        if not _has_log:
            print("   ⏸  无 log ⇒ **不写 status=SUCCESS**（否则会污染乌托邦队列）")
            db.save_task(
                info_hash=info_hash,
                torrent_name=name,
                status="NO_LOG_FILE",
                reason="目录里没有 .log 抓轨日志（已制种，供蟹黄堡直发）",
                cover_url=cover_url,
                torrent_file=torrent_file,
                small_descr=small_descr,
                tracklist=tracklist,
                log_content=log_content,
                log_filename=log_filename,
                logs_json=json.dumps(logs, ensure_ascii=False),
                content_path=content_path,
                track_type=track_type,
                qb_seed_days=_seed_days,
                **mt_col,
            )
            stats["ok"] += 1
            continue

        # 受保护状态的任务不覆盖既有状态，仅刷新元数据
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
            qb_seed_days=_seed_days,
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
        print("   ⇒ ★ 它们**不会**被 scan 用一个畸形标题顶替 —— 宁可留空。")
    else:
        print("✔ 皇后路标题零条缺失（本轮没有「该写却写不出」的行）")
    # 直接查询数据库统计各状态任务总数
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
              "")
        print("  ⑧b 六个 opencd_* 旧模块名零 import（已并入 opencd.py，防复制粘贴回来）")
        print("  C1 QB_HOST 兜底三处一致 + 空串回落 + 进 env 快照"
              "")
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
