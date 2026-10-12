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
