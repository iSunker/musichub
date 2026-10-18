"""只读核对：**站点上实际挂的 `.torrent`** 与 **DB 声称的版本** 是否一致（#31）。

═══════════════════════════════════════════════════════════════════════════
它存在的理由（一句话）
═══════════════════════════════════════════════════════════════════════════
`#136` 的后果：输出文件名只由 `formatted_title` 决定 ⇒ **同一专辑的不同版本**
（初回/期間/完全/通常盤）算到同一个 basename ⇒ `.torrent`/`.json` **互相覆盖**
⇒ 站点上挂的是**错版本**。治本护栏（`run.output_sidecar_paths()`，`a160375`）保证
**今后**不再产生；但**存量页面仍然是错的**，需要一个**只读**工具把它们**逐个验出来**。

★ 关键前提（决定了本模块的一切写法）：
  NexusPHP 站点在收下种子后**会重写**它（换 announce/passkey ⇒ **infohash 变**）。
  ⇒ **不能**拿本地的 `tasks.torrent_file`（那是用乌托邦 announce 生成的）去比；
     **身份判据只能来自回读站点副本**（`download_torrent` → `info.name`）。
  （2026-10-02 实测：同一条种子乌托邦 infohash `edda64b7…`、蟹黄堡 `03c17802…`，
   两者不同，但 80 个文件与总大小逐字节相同。见 `crabpt_site.download_torrent` docstring。）

═══════════════════════════════════════════════════════════════════════════
★★ 三条纪律（这个模块**很**容易被写成"看起来能发现、其实在撒谎"）
═══════════════════════════════════════════════════════════════════════════
1. **只读** —— 本模块**不**碰数据库（调用方传 `db_name` 进来）、**不**发种。
2. **不许猜** —— `.torrent` 里唯一的权威名字是 `metainfo["info"]["name"]`。
   **绝不**退回 `torf.Torrent.name`：单文件种子上它会返回**文件名**而非目录名
   （`tools/plan_mv_17304.py:80-88` 记着这个坑）⇒ 拿它比会得到**假 MATCH/MISMATCH**。
   解析不出就是 `(None, 原因)`，**不是**"大约等于"。
3. **`page_exists` 必须能证伪** —— 站点对**不存在的 id** 回的**不是 404**，而是一个
   HTTP 200 的**通用壳页**（`len≈24987`、`<title>` 与真页同款、**没有 `<h1>`**）。
   ⇒ 判据是「**有 `<h1>`（非空）**」；仅看 HTTP 200 / `<title>` 会把壳页当成真页
   （`18984/19069` 就是这样被记成 `EXISTS` 的——见 `#25` 结论）。
"""
import io
import re
import unicodedata

# 站点对"不存在的 id"回的通用壳页的 `<title>`。★ 只是**参考**，判据不靠它
# （真页恰好也可能有此串）——真判据是 `page_exists()` 里的 `<h1>`。
_SHELL_TITLE_HINT = "Powered by NexusPHP"

# 判定结果（字符串常量；报告与退出码都按它们分类）
MATCH = "MATCH"
MISMATCH = "MISMATCH"
MISSING_PAGE = "MISSING-PAGE"
DOWNLOAD_FAILED = "DOWNLOAD-FAILED"
NO_DB_NAME = "NO-DB-NAME"

# 有缺陷的判定（决定 `verify_hosted_edition.py` 的退出码）
DEFECTIVE = (MISMATCH, MISSING_PAGE, DOWNLOAD_FAILED)


def extract_info_name(torrent_bytes, *, torf_mod=None):
    """从 `.torrent` 字节里取**权威** `info.name`。返回 `(name_or_None, why)`。

    ★★ 只认 `metainfo["info"]["name"]` —— **绝不**退回 `torf.Torrent.name`：
       单文件种子上 `t.name` 是**文件名**（`plan_mv_17304.py:80-88`），
       拿它比会给"看起来对、其实错"的假结论（`ERR-META-01` 的形状）。
       解不出 ⇒ `(None, 原因)`，让上层判 `NO-DB-NAME`/`DOWNLOAD-FAILED`，
       **不许**在这里猜一个名字出来。
    """
    if torf_mod is None:
        try:
            import torf as torf_mod                # noqa: PLC0415
        except ImportError as e:                    # noqa: BLE001
            return None, f"缺 torf 库（{e}）"
    try:
        t = torf_mod.Torrent.read_stream(io.BytesIO(torrent_bytes))
    except Exception as e:                          # noqa: BLE001
        return None, f"解析种子失败：{type(e).__name__}: {e}"
    try:
        info = t.metainfo.get("info") or {}
        name = info.get("name")
        if not isinstance(name, str) or not name:
            return None, "metainfo 里没有 `info.name`（★ 不许猜成别的字段）"
        return name, ""
    except Exception as e:                          # noqa: BLE001
        return None, f"取 info.name 失败：{type(e).__name__}: {e}"


def normalize_name(name):
    """把名字折成**可比较**的规范形（NFC + 折叠所有空白 + 去首尾）。

    ★ 为什么要折空白：站点侧可能把连续空格/换行压过，而本地 `torrent_name` 未必
      ⇒ 不折会把"同一版本"报成 `MISMATCH`（假阳性）。
    ★ 为什么**只**折这些、不做得更狠（不去标点、不去大小写、不去括号）：
      因为初回/期間/完全 的差异**恰恰**在括号与汉字上 —— 折狠了会把**真的错版本
      比成 MATCH**（假阴性，那是本工具**最不能**犯的错）。宁可留白，不可抹掉差异。
    """
    if not isinstance(name, str):
        return ""
    s = unicodedata.normalize("NFC", name)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def compare_hosted_vs_db(db_name, hosted_name):
    """比对 DB 声称的名字与站点实挂的名字。返回三态之一。

    三态（**不是** bool）：
      · `MATCH`      —— 两名字规范化后相等（同一版本）。
      · `MISMATCH`   —— 两边都有名字，但不等（**挂错版本**）。
      · `NO_DB_NAME` —— DB 侧（或站点侧）**缺名字**，无从比 ⇒ 显式报"比不了"，
                        **不**退回 False 蒙混（否则"缺名字"会被读成"挂错"）。
    """
    a = normalize_name(db_name)
    b = normalize_name(hosted_name)
    if not a or not b:
        return NO_DB_NAME
    return MATCH if a == b else MISMATCH


# ★★「名字相近」的判据（`S4` / `ERR-PUBLISH-25`）—— **只用于诊断，绝不用于判定重复**。
#
#   为什么存在：`21811` 那次站点**真的**挂着一条 `info.name` 与本方**逐字相同**的页，
#   而 `search_and_compare` 返回了 `None`。调用方只看到"未发现同版本"一句话，
#   无法分辨"站点上没有"与"站点上有、但我没搜到/没验到"。于是我只能去猜字段，
#   连猜三轮全错（`ERR-PUBLISH-25`）。
#   ⇒ 这里给"**近亲条目**"一个判据：它出现 ⇒ 这条大概率是**同名冲突**，
#     而不是"站点上没有这条"。
#
#   ★★★ 边界（这条最要紧）：**近亲 ≠ 重复**。它在 `search_and_compare` 里**只写 diag、
#     绝不改返回值**；`compare_hosted_vs_db` 的精确相等仍是唯一判"重复"的判据。
#     若把近亲当重复 ⇒ 会跳过**本该发**的条目（少发一条，且**看不见** —— 比多发危险得多，
#     因为多发至少站点会拒，少发是静默的）。
#   ★ 为什么不用"编辑距离 ≤ N"：编辑距离对**长名**太松（改一个字就"不近"了）、
#     对**短名**太紧（`AB` vs `AC` 距离 1 就算近）。而实测的同类冲突形态是
#     **一方是另一方的前缀/包含**（`…FLAC` vs `…FLAC TESTNAME`）。⇒ 用包含关系 + 词级 Jaccard，
#     两个都**不看顺序**、都**只在线性时间**内算完，且**不读 config**（纯函数可单测）。
_NEAR_MISS_MIN_JACCARD = 0.8      # 词集重合度下限（0.8 ⇒ 改一个词以内）
_NEAR_MISS_MIN_TOKENS = 3         # 名称至少 3 个词才走 Jaccard（太短的名字随便两个都"像"）
_NEAR_MISS_MIN_LEN = 12           # 包含关系的短边至少要这么长（挡 `CD1` 这种"包含于一切"）


def name_tokens(name):
    """把名字切成**词级**集合（小写、按非字母数字切、去空）。

    ★ 为什么按非字母数字切：`群星 - HIGH PARTY2 2005 FLAC` 与
      `…FLAC TESTNAME` 的差别落在**词**上，不在**字符**上。
    ★ 用 `str.isalnum()` 而不是正则 `\\w`：`\\w` 的语义随 Python 构建/Unicode 版本漂移，
      本项目不容这种不确定（同 `musopia._SEARCH_SPLIT_RE` 的理由）。
    """
    if not isinstance(name, str):
        return set()
    out, cur = set(), []
    for ch in unicodedata.normalize("NFC", name):
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.add("".join(cur).lower())
            cur = []
    if cur:
        out.add("".join(cur).lower())
    return out


def is_near_miss(a, b):
    """两个名字是否**极像但不相等**（**仅供诊断**，见上面那段「边界」）。

    判据（任一成立即算近亲，且**必须先不相等**）：
      ① **包含**：短的一方（规范化后）是长的一方的子串，且短边长度 ≥ 12
         —— 覆盖实测形态 `…FLAC` vs `…FLAC TESTNAME`；
      ② **词集 Jaccard ≥ 0.8** 且两侧词数都 ≥ 3
         —— 覆盖"换了一个词/多加一个词"。

    ★ 相等 ⇒ 返回 False（相等的处置是 `compare_hosted_vs_db` 的 MATCH，不归这里）。
    ★ 两侧任一为空 ⇒ False（无从比，别当近亲 —— 否则"名字读不出"会被报成"同名冲突"）。
    """
    x, y = normalize_name(a), normalize_name(b)
    if not x or not y or x == y:
        return False
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    if len(short) >= _NEAR_MISS_MIN_LEN and short in long_:
        return True
    ta, tb = name_tokens(x), name_tokens(y)
    if len(ta) < _NEAR_MISS_MIN_TOKENS or len(tb) < _NEAR_MISS_MIN_TOKENS:
        return False
    union = ta | tb
    return bool(union) and (len(ta & tb) / len(union)) >= _NEAR_MISS_MIN_JACCARD


def extract_files(torrent_bytes, *, torf_mod=None):
    """从 `.torrent` 字节里取**权威文件集**。返回 `(files_or_None, why)`。

    `files` = 归一化后（正斜杠、去首尾）的文件路径列表，**已排序**；单文件种
    则为一个元素的列表（`info.name`）。

    ★ 2026-10-14（`ERR-PUBLISH-20` 修复③）：版本一致性闸门要的是**文件集**，
      不是 `info.name`。实测 21662 —— 分轨/整轨两种版本**共用同一个目录名**
      （`info.name` 都是那个 basename）⇒ **只比 `info.name` 比不出挂错版本**。
      要比的是"里面到底装了哪些文件"（12 个分轨 flac vs 1 个整轨 wav）。
    ★ 只认 `metainfo["info"]`：`files`（多文件）或 `name`（单文件）。
      解不出 ⇒ `(None, 原因)`，**不猜**（同 `extract_info_name` 的纪律）。
    """
    if torf_mod is None:
        try:
            import torf as torf_mod                # noqa: PLC0415
        except ImportError as e:                    # noqa: BLE001
            return None, f"缺 torf 库（{e}）"
    try:
        t = torf_mod.Torrent.read_stream(io.BytesIO(torrent_bytes))
    except Exception as e:                          # noqa: BLE001
        return None, f"解析种子失败：{type(e).__name__}: {e}"
    try:
        info = t.metainfo.get("info") or {}
        name = info.get("name") or ""
        if not isinstance(name, str) or not name:
            return None, "metainfo 里没有 `info.name`（★ 不许猜成别的字段）"
        entries = info.get("files")
        files = []
        if isinstance(entries, list) and entries:
            for ent in entries:
                parts = ent.get("path") if isinstance(ent, dict) else None
                if isinstance(parts, list) and parts:
                    files.append(_norm_path("/".join(str(p) for p in parts)))
                elif isinstance(parts, str) and parts:
                    files.append(_norm_path(parts))
            if not files:
                return None, "`info.files` 存在但一条路径都读不出"
        else:
            files = [_norm_path(name)]              # 单文件种
        files.sort()
        return files, ""
    except Exception as e:                          # noqa: BLE001
        return None, f"取文件集失败：{type(e).__name__}: {e}"


def _norm_path(p):
    """文件路径归一：反斜杠→正斜杠、折叠空白、去首尾（**去开头的 `./`**）。"""
    s = str(p or "").replace("\\", "/").strip()
    s = re.sub(r"\s+", " ", s)
    while s.startswith("./"):
        s = s[2:]
    return s


def files_of_dir(folder_path):
    """把一个**本地目录**折成与 `extract_files` **同口径**的文件集（相对路径，已排序）。

    ★ 与 `.torrent` 侧同规矩（`_norm_path` + 排序），否则两边永远比不平
      （`ERR-META-03`：同一事实只维护一份口径）。
    ★ 目录不存在 / 读不出 ⇒ `[]`（调用方据此判"比不了 ⇒ 不拦"，不猜）。
    ★ **不含**目录项本身（`.torrent` 里也不会出现目录名那一层）。
    """
    import os
    if not folder_path or not os.path.isdir(folder_path):
        return []
    out = []
    for root, _dirs, files in os.walk(folder_path):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, folder_path)
            out.append(_norm_path(rel))
    out.sort()
    return out


def compare_file_sets(local_files, torrent_files):
    """比两个**已归一排序**的文件集。返回 `(verdict, note)`。

    `verdict ∈ {MATCH, MISMATCH, NO_DB_NAME}`（复用同一组三态常量）：
      · 任一侧为空 ⇒ `NO_DB_NAME`（**比不了** ⇒ 调用方据此不拦，显式报出）。
      · 相等 ⇒ `MATCH`。
      · 不等 ⇒ `MISMATCH`（附一份差异摘要：只在一侧的、各自几条）。
    """
    a = list(local_files or [])
    b = list(torrent_files or [])
    if not a or not b:
        return NO_DB_NAME, "一侧文件集为空，无从比对"
    if a == b:
        return MATCH, f"文件集一致（{len(a)} 个文件）"
    sa, sb = set(a), set(b)
    only_local = sorted(sa - sb)
    only_torrent = sorted(sb - sa)
    note = (f"文件集不一致：本地 {len(a)} 个 / 种子 {len(b)} 个；"
            f"只在本地 {len(only_local)} 个 {only_local[:3]}；"
            f"只在种子 {len(only_torrent)} 个 {only_torrent[:3]}")
    return MISMATCH, note


def edit_target_ok(local_name, hosted_name):
    """★ `#173` 编辑护栏：**站侧页的 `info.name` 是否就是"我的这条"**。

    返回 `(ok: bool, reason: str)`。`ok=True` 才允许把编辑写上去。

    ═══════════════════════════════════════════════════════════════════════════
    为什么必须有（`#173` 实测踩到，不可逆）
    ═══════════════════════════════════════════════════════════════════════════
    `update_metadata.py`（edit 路）用 `tasks.site_torrent_id` 当**页面 id** 去写
    （`edit_torrent(tid, overrides)`），而**没有**核过"这个 id 真是我的页吗"。
    一旦 `site_torrent_id` 是**错值**（`#173`：21 条 crabpt 行指向**别人的**电影/音乐页），
    edit 就会把**本地音乐任务的元数据写进别人的页**（不可逆）。

    ★★ 判据 = **站侧 `.torrent` 的 `info.name` 与本地 `.torrent` 的 `info.name`
      归一化后精确相等**（复用 `compare_hosted_vs_db`）—— 与查重判据同一把尺
      （`ERR-META-03`：同一事实只维护一份口径）。
    ★★ **拿不到站侧名 / 本地名为空 ⇒ 拒改**（`NO_DB_NAME` ⇒ 拒）：
      "不可知"**不是**"是我的"（同 `ERR-PUBLISH-14`：别拿推测当事实去执行不可逆动作）。
    ★ 与 `#33`（edit 改不了挂的 `.torrent`）**是两件事**：那条管"改不了文件"，
      本条管"别改错页" —— 两条都要有。
    """
    verdict = compare_hosted_vs_db(local_name, hosted_name)
    if verdict == MATCH:
        return True, "站侧页 info.name 与本地一致"
    if verdict == NO_DB_NAME:
        return False, (f"无法核对页面身份（本地名={local_name!r} / 站侧名={hosted_name!r}）"
                       f"⇒ 拒改（#173：不可知不等于'是我的'）")
    return False, (f"页面身份不符（本地名={local_name!r} ≠ 站侧名={hosted_name!r}）"
                   f"⇒ 拒改（#173：site_torrent_id 可能是错值，别写坏别人的页）")


def page_exists(details_html):
    """详情页**真的存在**吗（= 站点确实有这条种子）。

    ★★ 判据 = **存在非空的 `<h1>`**。站点对**不存在的 id** 回的**不是 404**，
       而是一个 HTTP 200 的**通用壳页**（`#25` 实测：id=18984/19069 与**假 id
       99999999** 的响应**一模一样** —— `len≈24987`、`<title>` 同为
       `"… - Powered by NexusPHP"`、**没有 `<h1>`**）。
    ⇒ 若只看 HTTP 200 或 `<title>` 判存在，会把壳页当真有 ⇒ **假 EXISTS**
      （这正是 `18984/19069` 被记错 `EXISTS` 的机制）。
    """
    if not details_html:
        return False
    m = re.search(r"<h1\b[^>]*>(.*?)</h1>", details_html, re.S | re.I)
    if not m:
        return False
    inner = re.sub(r"<[^>]+>", " ", m.group(1))     # 去内嵌标签
    inner = inner.replace("&nbsp;", " ").strip()
    return bool(inner)


def classify(*, page_ok, torrent_bytes, db_name):
    """把"页面/下载/名字"三件事收成一个判定 + 一句人话。返回 `(verdict, hosted_name_or_None, note)`。

    ★ 这里集中定义**判定顺序**，避免每个调用方各写一份 `if` 链（`ERR-META-03`）：
        1) 页面不存在           → `MISSING_PAGE`（先判，因为壳页也能"下载"到东西/什么都没有）
        2) 没拿到种子字节        → `DOWNLOAD_FAILED`
        3) 从字节里取不出 info.name → `NO_DB_NAME`（附原因）
        4) 正常比对              → `MATCH` / `MISMATCH` / `NO_DB_NAME`（DB 侧缺名）
    """
    if not page_ok:
        return MISSING_PAGE, None, "详情页不存在（站点通用壳页：无 h1）"
    if not torrent_bytes:
        return DOWNLOAD_FAILED, None, "download.php 未返回 .torrent（非 bencode/无权限）"
    hosted, why = extract_info_name(torrent_bytes)
    if hosted is None:
        return NO_DB_NAME, None, f"站点副本读不出 info.name：{why}"
    verdict = compare_hosted_vs_db(db_name, hosted)
    if verdict == NO_DB_NAME:
        return NO_DB_NAME, hosted, "DB 侧缺 torrent_name，无从比对"
    note = "一致" if verdict == MATCH else f"不一致：DB={db_name!r} 站点={hosted!r}"
    return verdict, hosted, note
