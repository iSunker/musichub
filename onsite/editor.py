# -*- coding: utf-8 -*-
"""NexusPHP 站点「种子详情页」编辑器。

解决两类问题：
1. 已发布种子缺封面 / 简介不对 —— 直接改站点页面（`edit.php` → `takeedit.php`）；
2. 改一个字段不能把其它字段冲掉 —— `takeedit.php` 是**整表覆盖**语义，
   所以必须先 GET edit.php 把站点现值读成基线，只覆盖目标字段，其余原样回填。

边界（明确写死，避免“看起来能改”的假象）：
- `takeedit.php` 改不了页面挂的 `.torrent` 文件；换种只能重发。
- 封面只能写进 `descr` 的 `[img]`（OpenCD 除外，它有独立 cover 字段）。
- 找不到 takeedit 表单时，用 details.php 是否可读来区分「种子已删」与「无编辑权限」。
"""

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from musichub.onsite.site_rules import get_rule

logger = logging.getLogger("OnsiteEditor")

# takeedit 表单里不该回填的控件
SKIP_INPUT_TYPES = {"button", "submit", "reset", "file", "image"}

IMG_BBCODE_RE = re.compile(r"\[img[^\]]*\][^\[]+?\[/img\]", re.I)
QUOTE_HEAD_RE = re.compile(r"^\s*\[quote\].*?\[/quote\]", re.S | re.I)

# 空转载引用块：`[quote]转自OpenCD: [/quote]`（末尾空 URL）或 `[quote]转自OpenCD[/quote]`
# 2026-11-21 实测乌托邦 28125/28126/28132 等一批种子：源头 infohash 拼不出链接时，
# 旧实现留下「转自OpenCD: 」这种**只有前缀没有内容**的残句，站点上就是一个空引用框。
EMPTY_SOURCE_QUOTE_RE = re.compile(r"\[quote\]\s*转自[^\[\]\r\n:：]{1,20}[:：]?\s*\[/quote\]\s*", re.I)

# 「转自OpenCD: <40 位 infohash>」——旧实现把源种 infohash 当 tid 拼进来源引用，
# 站点上是一条打不开的死链。修复方向：能查到源站 tid 就换成 details.php 链接，
# 查不到且没有可用种子名时直接删掉这条死引用（绝不留半截话）。
SOURCE_QUOTE_RE = re.compile(r"\[quote\](.*?)\[/quote\]", re.I | re.S)
# 引用块里出现 32~40 位十六进制串 ⇒ 是把 infohash 当来源（可能是裸 hash，
# 也可能是 `details.php?id=<hash>` 这种拼出来的假链接），两种都要修。
HASH_TOKEN_IN_QUOTE_RE = re.compile(r"(?<![0-9a-zA-Z])([0-9a-fA-F]{32,40})(?![0-9a-zA-Z])")
# 详情页/RSS 标题里常带「[ 免费 ] 剩余时间：…」这类促销尾巴，回退用种子名时要剪掉
TITLE_JUNK_RE = re.compile(r"\s*\[\s*免费[^\]]*\].*$|\s*（?\s*免费\s*）?.*$|"
                           r"\s*剩余时间[:：].*$|\s*\[[^\]]*优惠[^\]]*\].*$")
NO_COVER_NOTE = "封面：源站和本地均无封面"


class OnsiteEditError(Exception):
    def __init__(self, message: str, *, deleted: bool = False):
        super().__init__(message)
        self.deleted = deleted


# ==================== 纯解析函数（可离线单测） ====================

def parse_edit_form(html: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """从 edit.php 页面解析 takeedit 表单。

    返回 `(fields, action)`；找不到表单时返回 `(None, "")`。
    `fields` 是可直接回填提交的 dict，多值键（`tags[4][]`）为 list。
    """
    soup = BeautifulSoup(html or "", "html.parser")
    form = None
    for fm in soup.find_all("form"):
        action = (fm.get("action") or "")
        if "takeedit" in action.lower():
            form = fm
            break
    if form is None:
        return None, ""

    fields: Dict[str, Any] = {}
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name or name.lower() in ("action", "submit"):
            continue

        if el.name == "input":
            typ = (el.get("type") or "text").lower()
            if typ in SKIP_INPUT_TYPES:
                continue
            val = el.get("value") or ""
            if typ in ("checkbox", "radio"):
                if not el.has_attr("checked"):
                    continue
                if name.endswith("[]"):
                    fields.setdefault(name, []).append(val)
                else:
                    fields[name] = val
            else:
                fields[name] = val
        elif el.name == "textarea":
            fields[name] = el.get_text() or ""
        else:  # select
            opts = el.find_all("option")
            selected = [o.get("value") or "" for o in opts if o.has_attr("selected")]
            if not selected:
                selected = [(opts[0].get("value") or "")] if opts else [""]
            fields[name] = selected[0]
    return fields, form.get("action") or ""


def parse_detail_page(html: str) -> Dict[str, str]:
    """从 details.php 读标题、简介区封面、简介文本（用于回读校验）。"""
    soup = BeautifulSoup(html or "", "html.parser")
    title_node = soup.select_one("h1#top") or soup.select_one("h1")
    title = title_node.get_text(" ", strip=True) if title_node else ""

    cover_url = ""
    cover_node = soup.select_one("div.cover img") or soup.select_one("#kdescr img") \
        or soup.select_one("#divdescr img") or soup.select_one("img.cover")
    if cover_node is not None:
        cover_url = cover_node.get("src") or cover_node.get("data-src") or ""

    descr_node = soup.select_one("#kdescr") or soup.select_one("#divdescr")
    descr_text = descr_node.get_text("\n", strip=True) if descr_node else ""
    return {"title": title, "cover_url": cover_url, "descr_text": descr_text}


def absolutize(url: str, base_url: str) -> str:
    """相对封面地址（NexusPHP 常见 `attachments/…`）转绝对 URL。"""
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("http://") or u.startswith("https://"):
        return u
    return urljoin((base_url or "").rstrip("/") + "/", u.lstrip("/"))


def has_cover(descr: str) -> bool:
    return bool(IMG_BBCODE_RE.search(descr or ""))


# 音乐池根目录（music5 是禁删红线池，明确不参与任何扫描）
POOL_ROOTS = [f"/volume1/video/download/music/{name}" for name in (
    "music1", "music2", "music3",
    "music_pool_1", "music_pool_2", "music_pool_3", "music_pool_4", "music_pool_5",
)]

_FORMAT_TAIL_RE = re.compile(r"\s*[-–]\s*(FLAC|WAV|APE|MP3|AAC|ALAC|DSD)\b.*$", re.I)


def _norm_album(name: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFC", str(name or ""))
    s = _FORMAT_TAIL_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


# 封面目录匹配的噪声词：纯数字（年份/日期/编号）会在分词阶段丢弃；
# 这里再剔除格式、常见修饰词与英文停用词——它们会让「两张不同专辑」
# 因共享 "2026/flac/web" 被误判为同名目录（2026-11-21 实测错配根因）。
_NOISE_TOKENS = {
    "flac", "wav", "ape", "mp3", "aac", "alac", "dsd", "dsf", "dff",
    "web", "remaster", "remastered", "limited", "edition", "deluxe",
    "bonus", "disc", "cd", "lp", "ep", "single", "album", "hq", "uhq",
    "mqa", "sacd", "vinyl", "japan", "us", "eu", "uk", "jp", "kr",
    "the", "a", "an", "of", "in", "on", "and", "to", "vs", "feat",
}


def _meaningful_tokens(s: str) -> set:
    """只保留有意义的词：纯数字（[a-z]+ 不含数字）一律丢弃，噪声词丢弃。"""
    toks = re.findall(r"[a-z]+|[\u4e00-\u9fff]{2,}", s or "")
    return {t for t in toks if t not in _NOISE_TOKENS and len(t) >= 2}


def _dir_confidence(target: str, name_norm: str) -> float:
    """target（专辑搜索名）与候选目录名的匹配置信度：0~1。"""
    if not target or not name_norm:
        return 0.0
    if name_norm == target:
        return 1.0
    ta = _meaningful_tokens(target)
    tb = _meaningful_tokens(name_norm)
    if not ta or not tb:
        return 0.0
    shared = ta & tb
    if len(shared) < 2:                      # 至少两个有意义词重合，杜绝单关键词误命中
        return 0.0
    return len(shared) / min(len(ta), len(tb))


def find_local_album_dir(*names: str, roots: Optional[List[str]] = None) -> str:
    """按专辑名在各音乐池一级目录里找本地目录（只读、不递归），找不到返回 ""。

    安全策略（封面错配红线，见经验教训）：精确命中直接采信；否则在**所有池**里选
    置信度最高者，且必须 ≥0.8 才返回。宁可返回空（让 descr 补「无封面」说明），
    也不给不同专辑错配图。旧版「按池顺序返回第一个命中 + 含噪声词的 0.6 阈值」
    会把专辑 A 匹配到池顺序更靠前、完全无关的专辑 B 的目录。
    """
    targets = [t for t in (_norm_album(n) for n in names if n) if t]
    if not targets:
        return ""
    best_path, best_conf = "", 0.0
    for root in (roots or POOL_ROOTS):
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for e in entries:
            if not e.is_dir() or e.name.startswith((".", "@")):
                continue
            nn = _norm_album(e.name)
            for t in targets:
                if nn == t:
                    return e.path              # 精确命中，直接采信
                c = _dir_confidence(t, nn)
                if c > best_conf:
                    best_conf, best_path = c, e.path
    if best_path and best_conf >= 0.8:
        return best_path
    return ""


def _token_overlap(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", a or ""))
    tb = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", b or ""))
    if not ta or not tb:
        return 0.0
    shared = ta & tb
    if not shared:
        return 0.0
    return len(shared) / min(len(ta), len(tb))


_COVER_STEMS = ("front", "cover", "folder", "album", "poster")


def find_nested_cover(dir_path: str) -> str:
    """合辑类种子顶层没有图时，取一层子目录里的封面（front/cover/folder…）。"""
    try:
        subs = [e for e in os.scandir(dir_path) if e.is_dir()]
    except OSError:
        return ""
    for sub in sorted(subs, key=lambda x: x.name):
        try:
            files = list(os.scandir(sub.path))
        except OSError:
            continue
        for f in files:
            if not f.is_file():
                continue
            stem, ext = os.path.splitext(f.name)
            if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                continue
            if stem.strip().lower() in _COVER_STEMS:
                return f.path
    return ""


def insert_cover_block(descr: str, cover_url: str) -> str:
    """把封面块插到简介里；已存在任何 `[img]` 时保持原文不动。

    位置与发布时一致：转载声明 `[quote]…[/quote]` 之后、正文之前。
    """
    descr = descr or ""
    url = (cover_url or "").strip()
    if not url or has_cover(descr):
        return descr
    block = f"[center][img]{url}[/img][/center]"
    m = QUOTE_HEAD_RE.match(descr)
    if m:
        return descr[:m.end()] + "\n\n" + block + descr[m.end():]
    return block + ("\n\n" + descr.lstrip() if descr.strip() else "")


def first_cover_url(descr: str) -> str:
    match = IMG_BBCODE_RE.search(descr or "")
    if not match:
        return ""
    return re.sub(r"^\[img[^\]]*\]|\[/img\]$", "", match.group(0), flags=re.I).strip()


def replace_cover_block(descr: str, cover_url: str) -> str:
    """只替换首图，保留其余简介与插图；无首图时走插入流程。"""
    url = (cover_url or "").strip()
    if not url:
        return descr or ""
    match = IMG_BBCODE_RE.search(descr or "")
    if not match:
        return insert_cover_block(descr, url)
    return (descr or "")[:match.start()] + f"[img]{url}[/img]" + (descr or "")[match.end():]


def has_bad_source_quote(descr: str, has_numeric_source_tid: bool = False) -> bool:
    """简介里是否还有坏引用或需要补全/升级来源引用：
    1. 含有 [quote]转自...[/quote]，但带有 hash 或促销尾巴；
    2. 含有 [quote]转自...[/quote]，但没有合法详情页直链（只有曲名文本等），且外部掌握纯数字 TID；
    3. 完全没有 [quote] 来源引用块，且外部掌握纯数字 TID。
    """
    if not descr:
        return bool(has_numeric_source_tid)
    found_quote = False
    for m in SOURCE_QUOTE_RE.finditer(descr):
        body = m.group(1)
        if "转自" not in body:
            continue
        found_quote = True
        if HASH_TOKEN_IN_QUOTE_RE.search(body) or TITLE_JUNK_RE.search(body):
            return True
        if has_numeric_source_tid and not re.search(r"(?:details\.php\?id=\d+|torrents\.php\?id=\d+|感谢原制作者发布)", body):
            return True
    if not found_quote and has_numeric_source_tid:
        return True
    return False


def rebuild_source_quote(descr: str, source_site: str = "opencd",
                         source_tid: str = "", source_name: str = "") -> Tuple[str, List[str]]:
    """把「转自X: <infohash>」或无直链的「转自X: 种子名」改成带源站种子 ID 的可点击链接。

    - 有纯数字源站 tid ⇒ `转自OpenCD: https://open.cd/details.php?id=<tid>`；
    - 没有 tid 但有种子名 ⇒ 退化为 `转自X: <种子名>`（与 descr_core.build_source_link 一致）；
    - 两者都没有 ⇒ 删掉整条死引用，绝不留半截话。
    已经是正常链接/目标文案的引用原样保留（幂等）。
    """
    original = descr or ""
    site = (source_site or "opencd").strip().lower()
    try:
        from musichub.adapters.descr_core import SOURCE_SITES
        display, base = SOURCE_SITES.get(site, (site or "OpenCD", ""))
    except Exception:                                          # noqa: BLE001
        display, base = (site or "OpenCD"), ""

    tid = str(source_tid or "").strip()
    name = TITLE_JUNK_RE.sub("", str(source_name or "").strip()).strip(" -")
    if tid.isdigit() and base:
        target = f"转自{display}: {base}/details.php?id={tid}"
    elif name:
        target = f"转自{display}: {name}"
    else:
        target = ""

    changes: List[str] = []

    def _repl(m: "re.Match[str]") -> str:
        body = m.group(1).strip()
        if "转自" not in body:
            return m.group(0)

        # 已经完全对齐目标直链或文案，保持幂等
        if target and body == target:
            return m.group(0)

        # 1. 掌握纯数字 TID 时：若正文已精准匹配目标源站的合法数字直链，保持幂等原样保留；仅在无直链/死链时主动纠正/升级
        if tid.isdigit() and base:
            if re.search(rf"{re.escape(base)}/details\.php\?id=\d+(?:&hit=\d+)?(?:\s*$|[\s\]])", body):
                return m.group(0)
            changes.append(f"升级来源引用为源站直链 (tid={tid})")
            return f"[quote]{target}[/quote]"

        # 2. 未掌握显式纯数字 TID 时：若正文已经带有合法的详情页数字直链（严格纯数字，无脏文本后缀），原样保留
        if re.search(r"details\.php\?id=\d+(?:&hit=\d+)?(?:\s*$|[\s\]])", body):
            return m.group(0)

        # 2. 未掌握纯数字 TID 时的旧有清洗守卫（清理 hash 与促销尾巴）
        if not HASH_TOKEN_IN_QUOTE_RE.search(body) and not TITLE_JUNK_RE.search(body):
            return m.group(0)

        if not target:
            # 没有外部 tid/名字时，用引用块现成文案兜底：去掉 hash 与促销尾巴，
            # 仍然有可用文案就保留（如「转自OpenCD: Artist - Album」），否则整条删掉。
            salvaged = TITLE_JUNK_RE.sub("", HASH_TOKEN_IN_QUOTE_RE.sub("", body)).strip()
            m2 = re.match(r"^转自\s*([^\[\]\r\n:：]{1,20})\s*[:：]\s*(.+)$", salvaged)
            if m2 and len(m2.group(2).strip()) >= 2:
                changes.append("来源引用去 hash/促销尾巴")
                return f"[quote]转自{m2.group(1)}: {m2.group(2).strip()}[/quote]"
            changes.append("删除 infohash 来源死链引用")
            return ""
        changes.append(f"来源引用修正为: {target[:48]}")
        return f"[quote]{target}[/quote]"

    new = SOURCE_QUOTE_RE.sub(_repl, original)
    if not changes and tid.isdigit() and base and "转自" not in original:
        changes.append(f"补全来源引用为源站直链 (tid={tid})")
        new = f"[quote]{target}[/quote]\n\n" + original.lstrip()
    new = re.sub(r"\n{3,}", "\n\n", new).strip()
    return (new if changes else original.strip()), changes


def normalize_descr(descr: str, note: str = NO_COVER_NOTE) -> Tuple[str, List[str]]:
    """体检并修整已发布种子的 BBCode 简介，返回 `(新简介, 改动说明列表)`。

    只做两件**幂等、可回读**的事，不做重排版：
    1. 删掉**空的转载引用块**（`[quote]转自OpenCD: [/quote]`）——
       这是旧实现"拼不出链接就留半截话"的残渣，站点上会显示成一个空引用框；
    2. 整个简介没有 `[img]` 时，补一行「封面：源站和本地均无封面」，
       位置与原封面块一致（转载引用之后、正文之前）。
       已存在该说明则跳过（幂等，重复跑不会堆叠）。

    ★ 为什么不在这一步"顺手补封面"：补图要真图（`fix_cover` 有独立流程与回读校验），
      这里只做**文本层面的确定修复**；两者可以组合使用（先 fix-descr 再 fix-cover）。
    """
    original = descr or ""
    new = original
    changes: List[str] = []

    if EMPTY_SOURCE_QUOTE_RE.search(new):
        new = EMPTY_SOURCE_QUOTE_RE.sub("", new)
        changes.append("移除空转载引用块")

    # 有封面时剥掉陈旧「无封面」说明：先 fix-cover 失败 → fix-descr 补了说明，
    # 后续 fix-cover 成功后需清掉，避免「封面图 + 无封面说明」自相矛盾。
    if note and has_cover(new) and note in new:
        new = "\n".join(ln for ln in new.split("\n") if ln.strip() != note.strip())
        changes.append("移除无封面说明（已有封面）")

    if note and not has_cover(new) and note not in new:
        block = str(note).strip()
        m = QUOTE_HEAD_RE.match(new.lstrip())
        if m:
            head = new.lstrip()[:m.end()]
            rest = new.lstrip()[m.end():]
            new = head + "\n\n" + block + ("\n\n" + rest.lstrip() if rest.strip() else "")
        else:
            new = block + ("\n\n" + new.lstrip() if new.strip() else "")
        changes.append("补无封面说明")

    new = re.sub(r"\n{3,}", "\n\n", new).strip()
    if new == original.strip():
        return original.strip(), changes
    return new, changes


def resolve_cover_url(
    hint: str = "",
    source_site: str = "",
    source_tid: str = "",
    local_path: str = "",
    source_base_url: str = "",
    target_site: str = "",
    source_name: str = "",
    expected_image_path: str = "",
) -> Tuple[str, str]:
    """按「源站优先、失效才补图」的封面策略取一条**可公网访问**的封面 URL。

    顺序：显式 hint / 源种详情页（先校验有效性）→ 已落库封面 → 本地专辑目录补图上传。
    上传目标按站点规则路由（crabpt → Lsky，pterclub → 猫站图床，其余 → PicGo）。
    任何一步失败继续下一步；全失败返回 `("", 原因)`，绝不返回空当成功。
    """
    h = (hint or "").strip()
    if h and not h.startswith(("http://", "https://")):
        if source_base_url:
            h = absolutize(h, source_base_url)
        else:
            logger.info(f"封面是相对路径 {h!r}，缺 base_url 无法转绝对")
            h = ""

    # 没有显式封面时，回源种详情页取一条候选
    if not h and source_site and source_tid:
        try:
            from musichub.collector.source_detail import SourceDetailFetcher, SITE_INFO
            result = SourceDetailFetcher().fetch(source_site, source_tid)
            if result.get("ok") and result.get("cover_url"):
                base = (SITE_INFO.get(source_site) or {}).get("base", "")
                h = absolutize(result["cover_url"], base)
            else:
                logger.info(f"源种 {source_site}#{source_tid} 无封面: "
                            f"{result.get('error') or '页面无封面'}")
        except Exception as e:                                  # noqa: BLE001
            logger.warning(f"取源种封面失败 {source_site}#{source_tid}: {e}")

    try:
        from musichub.metadata.cover_service import CoverImageService
        svc = CoverImageService()
        url, why = svc.get_site_cover(
            target_site or "", h, local_path or None,
            source_site=source_site, source_tid=source_tid,
            source_name=source_name, expected_image_path=expected_image_path,
        )
        if url:
            return url, why or "封面解析成功"
        return "", why or "没有可用封面"
    except Exception as e:                                      # noqa: BLE001
        logger.warning(f"封面解析失败: {e}")
        return "", f"封面解析失败：{type(e).__name__}: {e}"


# ==================== 编辑器 ====================

class OnsiteEditor:
    """基于现有站点适配器的会话（CookieCloud 登录态）做详情页编辑。"""

    def __init__(self, adapter):
        self.adapter = adapter
        self.site = getattr(adapter, "site_name", "") or ""
        self.base_url = getattr(adapter, "base_url", "") or ""
        self.session = adapter.session

    # ---------- 基础请求 ----------

    def _headers(self, referer: str = "") -> Dict[str, str]:
        headers = self.adapter._prepare_headers()               # noqa: SLF001 - 复用登录态装配
        if referer:
            headers["Referer"] = referer
        return headers

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None, referer: str = ""):
        for attempt in range(3):
            try:
                resp = self.session.get(
                    self.base_url + path, params=params, headers=self._headers(referer),
                    timeout=30, verify=False, allow_redirects=True,
                )
                if "takelogin.php" in (resp.url or "") or "login.php" in (resp.url or ""):
                    raise OnsiteEditError(f"{self.site} Cookie 已失效（被重定向到登录页）")
                return resp
            except Exception as e:
                if attempt == 2 or isinstance(e, OnsiteEditError):
                    raise
                time.sleep(1.5 * (attempt + 1))

    def fetch_edit(self, tid: str) -> Tuple[Dict[str, Any], str]:
        """读 edit.php 现值；失败时区分「种子已删」与「无编辑权限」。"""
        resp = self._get("/edit.php", {"id": str(tid)})
        if resp.status_code != 200:
            raise OnsiteEditError(f"打开 edit.php?id={tid} 失败：HTTP {resp.status_code}")
        fields, action = parse_edit_form(resp.text)
        if fields is None:
            d = self.fetch_details(tid)
            if d.get("title"):
                raise OnsiteEditError(
                    f"edit.php?id={tid} 里没有 takeedit 表单，但 details.php 标题可读"
                    f"（{d['title'][:40]!r}）⇒ 种子还在，但没有编辑权限")
            raise OnsiteEditError(
                f"edit.php?id={tid} 没有 takeedit 表单，details.php 也读不到标题 ⇒ 种子多半已被删除",
                deleted=True)
        return fields, action

    def fetch_details(self, tid: str) -> Dict[str, str]:
        resp = self._get("/details.php", {"id": str(tid), "hit": "1"})
        if resp.status_code != 200:
            return {"title": "", "cover_url": "", "descr_text": ""}
        return parse_detail_page(resp.text)

    # ---------- 编辑 ----------

    @staticmethod
    def _norm(v: Any) -> Any:
        if isinstance(v, list):
            return sorted(str(x) for x in v)
        return "" if v is None else str(v)

    def _diff(self, before: Dict[str, Any], overrides: Dict[str, Any]) -> List[Tuple[str, Any, Any]]:
        changes = []
        for k, v in overrides.items():
            old = before.get(k)
            if self._norm(old) != self._norm(v):
                changes.append((k, old, v))
        return changes

    def edit(
        self,
        tid: str,
        overrides: Dict[str, Any],
        *,
        dry_run: bool = True,
        verify: bool = True,
    ) -> Dict[str, Any]:
        """只改 overrides 里的字段，其余按站点现值回填。"""
        bad = [k for k in overrides if k.lower() in ("file", "torrent") or k.startswith("file")]
        if bad:
            return {"ok": False, "tid": str(tid), "error": f"takeedit 改不了种子文件，被拒字段: {bad}"}

        fields, action = self.fetch_edit(tid)
        before = {k: (list(v) if isinstance(v, list) else v) for k, v in fields.items()}
        changes = self._diff(before, overrides)

        report: Dict[str, Any] = {
            "ok": True,
            "tid": str(tid),
            "site": self.site,
            "action": action,
            "dry_run": dry_run,
            "changes": [{"field": k, "old": o, "new": n} for k, o, n in changes],
        }
        if not changes:
            report["message"] = "无需修改（站点现值与目标一致）"
            return report
        if dry_run:
            report["message"] = f"dry-run：将修改 {len(changes)} 个字段"
            return report

        for k, _, v in changes:
            fields[k] = v

        parts = []
        for k, v in fields.items():
            if isinstance(v, list):
                for item in v:
                    parts.append((k, (None, str(item))))
            else:
                parts.append((k, (None, "" if v is None else str(v))))

        url = f"{self.base_url}/takeedit.php"
        try:
            resp = self.session.post(
                url, files=parts,
                headers=self._headers(referer=f"{self.base_url}/edit.php?id={tid}"),
                timeout=45, verify=False, allow_redirects=False,
            )
        except Exception as e:                                  # noqa: BLE001
            report.update(ok=False, error=f"提交异常: {type(e).__name__}: {e}")
            return report

        if resp.status_code in (301, 302, 303):
            loc = resp.headers.get("Location", "")
            if "details.php" in loc or f"id={tid}" in loc:
                report["message"] = f"已提交（302 → {loc}）"
            else:
                report.update(ok=False, error=f"提交后重定向到异常位置: {loc}")
                return report
        elif resp.status_code == 200:
            err = self._extract_error(resp.text)
            if err:
                report.update(ok=False, error=f"站点返回错误: {err}")
                return report
            report["message"] = "已提交（HTTP 200）"
        else:
            report.update(ok=False, error=f"提交失败：HTTP {resp.status_code}")
            return report

        if verify:
            report["verify"] = self.verify(tid, overrides)
            report["ok"] = bool(report["verify"].get("ok"))
        return report

    def verify(self, tid: str, overrides: Dict[str, Any]) -> Dict[str, Any]:
        """回读 edit.php，逐字段比对是否真的落地。"""
        try:
            fields, _ = self.fetch_edit(tid)
        except OnsiteEditError as e:
            return {"ok": False, "error": f"回读失败: {e}"}
        mismatch = []
        for k, v in overrides.items():
            if self._norm(fields.get(k)) != self._norm(v):
                mismatch.append(k)
        return {"ok": not mismatch, "mismatch": mismatch,
                "checked": sorted(overrides.keys())}

    @staticmethod
    def _extract_error(html: str) -> str:
        soup = BeautifulSoup(html or "", "html.parser")
        for sel in ("#outer table td.text", "td.embedded", "h2", "h1"):
            el = soup.select_one(sel)
            if el:
                txt = el.get_text(" ", strip=True)
                if txt and len(txt) < 300 and any(k in txt for k in ("错误", "Error", "失败", "请选择", "必须")):
                    return txt
        return ""

    # ---------- 封面修复 ----------

    def fix_cover(
        self,
        tid: str,
        *,
        cover_url: str = "",
        source_site: str = "",
        source_tid: str = "",
        source_name: str = "",
        local_path: str = "",
        expected_image_path: str = "",
        dry_run: bool = True,
        verify: bool = True,
    ) -> Dict[str, Any]:
        rule = get_rule(self.site)
        fields, _ = self.fetch_edit(tid)

        def byte_verification(url: str) -> Dict[str, Any]:
            if not expected_image_path:
                return {"ok": bool(url), "detail_cover": url}
            from musichub.metadata.cover_service import CoverImageService, image_file_evidence
            expected = image_file_evidence(expected_image_path)
            actual = CoverImageService.fetch_image_evidence(absolutize(url, self.base_url))
            return {
                "ok": bool(expected.get("ok") and actual.get("ok")
                           and actual.get("sha256") == expected.get("sha256")),
                "detail_cover": url, "expected_sha256": expected.get("sha256"),
                "actual": actual,
            }

        if rule.get("cover_mode") == "separate_field":
            current = (fields.get("cover") or "").strip()
            if current:
                if not expected_image_path:
                    return {"ok": True, "tid": str(tid), "message": f"已有独立封面字段: {current}"}
                current_check = byte_verification(current)
                if current_check.get("ok"):
                    return {"ok": True, "tid": str(tid), "message": "独立封面 SHA256 已匹配",
                            "verify": current_check}
            url, why = resolve_cover_url(cover_url, source_site, source_tid, local_path,
                                         rule.get("base_url", ""), target_site=self.site,
                                         source_name=source_name,
                                         expected_image_path=expected_image_path)
            if not url:
                return {"ok": False, "tid": str(tid), "error": why}
            result = self.edit(tid, {"cover": url}, dry_run=dry_run,
                               verify=verify and not expected_image_path)
            result.update(cover_url=url, cover_source=why)
            if verify and expected_image_path and not dry_run and result.get("ok"):
                result["verify"] = byte_verification(url)
                result["ok"] = result["verify"]["ok"]
            return result

        descr = fields.get("descr") or ""
        current = first_cover_url(descr)
        if current:
            if not expected_image_path:
                return {"ok": True, "tid": str(tid), "message": "简介里已有封面，跳过"}
            current_check = byte_verification(current)
            if current_check.get("ok"):
                return {"ok": True, "tid": str(tid), "message": "简介首图 SHA256 已匹配",
                        "verify": current_check}

        url, why = resolve_cover_url(cover_url, source_site, source_tid, local_path,
                                     rule.get("base_url", ""), target_site=self.site,
                                     source_name=source_name,
                                     expected_image_path=expected_image_path)
        if not url:
            return {"ok": False, "tid": str(tid), "error": why}

        required_host = (rule.get("cover_host_required") or "").strip()
        if required_host and required_host not in (urlparse(url).netloc or ""):
            return {"ok": False, "tid": str(tid),
                    "error": f"{rule.get('display') or self.site} 要求封面托管在 {required_host}，"
                             f"当前图链 {url} 不满足（需先上传专用图床）"}

        new_descr = replace_cover_block(descr, url) if current else insert_cover_block(descr, url)
        result = self.edit(tid, {"descr": new_descr}, dry_run=dry_run, verify=False)
        result["cover_url"] = url
        result["cover_source"] = why
        result["cover_action"] = "replace" if current else "insert"
        if verify and not dry_run and result.get("ok"):
            details = self.fetch_details(tid)
            got = details.get("cover_url") or ""
            if expected_image_path:
                result["verify"] = byte_verification(got)
            else:
                result["verify"] = {"ok": bool(got) and (url in got or got in url),
                                    "detail_cover": got}
            result["ok"] = result["verify"]["ok"]
        return result

    # ---------- 简介修整（空转载引用块 / 无封面说明） ----------

    def fix_source_link(
        self,
        tid: str,
        *,
        source_site: str = "opencd",
        source_tid: str = "",
        source_name: str = "",
        dry_run: bool = True,
        verify: bool = True,
    ) -> Dict[str, Any]:
        """把「转自X: <infohash>」死链改成源站详情页链接（默认 dry-run）。

        只动来源引用那一块 `[quote]…[/quote]`，其余字段按站点现值回填。
        """
        fields, _ = self.fetch_edit(tid)
        descr = fields.get("descr") or ""
        name = str(source_name or "").strip() or str(fields.get("name") or "").strip()
        new_descr, changes = rebuild_source_quote(descr, source_site, source_tid, name)
        if not changes:
            return {"ok": True, "tid": str(tid), "changed": False,
                    "message": "来源引用无 infohash 死链"}
        result = self.edit(tid, {"descr": new_descr}, dry_run=dry_run, verify=False)
        result["changed"] = True
        result["changes"] = changes
        result["descr_before"] = descr
        result["descr_after"] = new_descr
        if verify and not dry_run and result.get("ok"):
            got, _ = self.fetch_edit(tid)
            after = got.get("descr") or ""
            ok = after.strip() == new_descr.strip() and not has_bad_source_quote(after)
            result["verify"] = {"ok": ok, "descr_len": len(after)}
            result["ok"] = ok
        return result

    def fix_descr(self, tid: str, *, note: str = NO_COVER_NOTE,
                  dry_run: bool = True, verify: bool = True) -> Dict[str, Any]:
        """体检 + 修整已发布种子的简介（默认 dry-run）。

        只做 `normalize_descr()` 那两件事（删空转载引用块、补无封面说明），
        其余内容原样保留；写回走 `takeedit.php` 整表覆盖流程（其余字段按现值回填）。
        """
        fields, _ = self.fetch_edit(tid)
        descr = fields.get("descr") or ""
        new_descr, changes = normalize_descr(descr, note=note)
        if not changes:
            return {"ok": True, "tid": str(tid), "changed": False,
                    "message": "简介无异常（空引用块/无封面说明均无需处理）"}
        result = self.edit(tid, {"descr": new_descr}, dry_run=dry_run, verify=False)
        result["changed"] = True
        result["changes"] = changes
        result["descr_before"] = descr
        result["descr_after"] = new_descr
        if verify and not dry_run and result.get("ok"):
            got, _ = self.fetch_edit(tid)
            after = got.get("descr") or ""
            ok = after.strip() == new_descr.strip()
            result["verify"] = {"ok": ok, "descr_len": len(after)}
            result["ok"] = ok
        return result
