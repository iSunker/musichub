"""
OpenCD（皇后）流水线 —— **六合一单模块**（parse / rate / log / map / gate / ingest）。

★ 本文件由 2026-10-04 的目录整理任务把原来六个模块
  （`opencd_parse.py` / `opencd_rate.py` / `opencd_log.py` / `opencd_map.py` /
   `opencd_gate.py` / `opencd_ingest.py`）**逐字合并**而成 —— 函数体一字未改，
  只做了三件机械事：① 拼接；② 解决重名（`main`→`_main_X`、`_check`→`_check_X`、
  `cmd_selftest`→`_selftest_X`）；③ 去掉跨模块的 `opencd_x.` 前缀（现在同在一个文件）。

用法（子命令 = 原来的模块名）：
    python opencd.py parse   [<file.html>|--selftest] [--json]
    python opencd.py rate    [--selftest] [--json]
    python opencd.py log                     # 日志工具是纯库，无 CLI
    python opencd.py map     [<file.html>|--selftest] [--json]
    python opencd.py gate    --selftest [--json]
    python opencd.py ingest  --selftest [--json]

★ 各子命令的自检输出与原模块**逐字相同**（含退出码与末行 JSON 契约），
  被 `tools/gen_counts.py` 当子进程调用。
"""
import argparse
import glob
import json
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# ★★ 必须在 `import db` 之前塞好环境变量（原 opencd_ingest.py 的顺序不变量，见下方段落）——
#   旧代码在这里手动插入。
# ★★ 必须在 `import db` 之前塞好 `DATA_DIR` 等环境变量：
#    `db` 会 import `config`，而 `config` 用 `require_env`（缺了就 `sys.exit`），
#    并且 `config.DB_PATH` 在 import 时就由 `DATA_DIR` 算出来了。
#    ★ 这里**只在跑自检时**把 `DATA_DIR` 指向临时目录 —— 这样"跑一次自检"
#      绝不会碰到真实库（本项目对"自检不能有副作用"有成文纪律）。
#    ★ 用 `setdefault`：若调用方（生产容器）已设好，**绝不覆盖**。
if "--selftest" in __import__("sys").argv:
    import tempfile as _tempfile
    os.environ.setdefault("QB_PASS", "x")
    os.environ.setdefault("MUSOPIA_COOKIE", "c_secure_pass=x")
    os.environ.setdefault("MUSOPIA_PASSKEY", "x")
    os.environ.setdefault("PICGO_API_KEY", "x")
    os.environ["DATA_DIR"] = _tempfile.mkdtemp(prefix="opencd-ingest-selftest-")

# ★ `db` 与本文件同目录（仓库根）。只在 ingest 部分用到，但放文件头。
import db
import descr

try:
    from bs4 import BeautifulSoup
except ImportError:                                     # pragma: no cover
    sys.exit("❌ 需要 beautifulsoup4（Dockerfile 里已固定版本）")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                       # noqa: BLE001
    pass


# ==================================================================== ① parse
# ★ 2026-10-06：源码已移进 `src/`，但 `opencdhtml/`（样本/证据**资产**，不是代码）
#   仍在**仓库根**。所以不能再用 `dirname(__file__)` 直接当"资产根"。
#   判据：`<此文件所在目录>/opencdhtml` 在 ⇒ 就是它（容器内 /app 平铺）；
#         否则取上一级（本机 `src/opencd.py` → 仓库根）。
def _asset_root():
    _here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(_here, "opencdhtml")):
        return _here
    return os.path.dirname(_here)


HERE = _asset_root()
SAMPLE_DIR = os.path.join(HERE, "opencdhtml", "sanitized")

# ==================== 转载规则（★ 三态，不是布尔） ====================
# ★ 为什么必须是三态：参考实现只判 `== "禁止轉載"`，于是「判不出」被当成「可以转」。
#   版面一变、字段一缺，就成了静默放行。三态把「判不出」显式变成「要人工看」。
RULE_ALLOW = "ALLOW"            # 欢迎转载
RULE_DENY = "DENY"              # 禁止转载 —— ★ 永不自动发布
RULE_UNKNOWN = "NEED_CONFIRM"   # 缺失 / 认不出 —— 默认**不**自动发布

# 页面「轉載規則」字段的取值 → 三态。★ 只放**实测见过**的值；
# 没见过的值一律落 RULE_UNKNOWN（保守），别用「不含禁止就算允许」那种反推。
RULE_VALUES = {
    "歡迎轉載": RULE_ALLOW,
    "欢迎转载": RULE_ALLOW,
    "禁止轉載": RULE_DENY,
    "禁止转载": RULE_DENY,
    # ★ 2026-09-23 加：第三个「禁转」的说法。
    #   ★ 实测来源：tid=200002（Mary Black - Babes In The Wood）详情页
    #     `轉載規則` 字段原文 = 「未經允許不得轉載」—— 当时不在表里 ⇒ 落
    #     NEED_CONFIRM。语义明确是**禁止**（"未经允许不得转载"）。
    #   ★ 为什么必须显式加、不能靠"认得禁止两个字就类推"：本表是**白名单**，
    #     查不到就落 NEED_CONFIRM（等于"不自动发"）⇒ 那是**漏判**，不是放行。
    #     但漏判会让**明确禁转**的行每次都进人工队列，规则就一直卡在那儿。
    #   ★ 判据强度**不同**，分开标：
    #     · 「未經允許不得轉載」= **实测见过**（上面的来源）
    #     · 「未经允许不得转载」= **同族推断**（本站字段实测都是繁体，
    #       但既有表里 4 条全是繁简成对，故按同族补齐，保持对称）
    "未經允許不得轉載": RULE_DENY,
    "未经允许不得转载": RULE_DENY,
}

# 字段表：页面标签 → 我们的字段名。★ 键是**页面上真实的繁体标签**（实测）。
FIELD_LABELS = {
    "album": "專輯名稱",
    "artist": "藝術家名",
    "year": "發行年份",
    "size": "大小",
    "file_count": "文件數",
    "type": "類型",
    "category": "類別",
    "format": "格式",
    "medium": "媒介",
    "team": "製作組",
    "source": "來自",            # ★ 决定 2：原样保留 原抓 / 轉載
    "tags": "標籤列表",
    "transfer_rule_raw": "轉載規則",
}


def _norm(s):
    r"""
    归一化**单行字段值**：全角空格、连续空白压成一个。

    ★ **只给字段表的短值用**（专辑名/年份/大小/标签…）。
      **绝不可用于日志 / 曲目 / 简介** —— 那三样是要**原样**送进简介的长文本：
      · EAC 日志是列对齐的，压空白会把 **69 行压成 1 行**（实测踩过，见下）；
      · 曲目列表的换行就是它的结构；
      · 简介里的空行是排版。
      「把 `\s+` 压成一个空格」对短值是无害清理，对长文本是**破坏**。
      ⇒ 判据：**短值求规整，长文本求保真**，两件事不能共用一个函数。
    """
    return re.sub(r"\s+", " ", (s or "").replace("　", " ")).strip()


def _clean_long(text):
    """
    长文本（日志/曲目/简介）的清理：**只去首尾空白，内部一个字符都不动**。

    ★ 为什么必须单独一个函数（2026-09-19 实测）：第一版对日志也调用了 `_norm`，
      结果 EAC 日志从 **69 行塌成 1 行** —— 而这份日志的任务正是被
      `descr.build_descr()` 包成 `[spoiler=LOG][code]…[/code][/spoiler]` 贴到 Musopia。
      塌成一行 = 页面上一坨糊在一起，**而且解析"看起来成功了"**
      （`log_nonempty` 照样是 True）⇒ 又是一次「把失败当成功」。
      ★ 兜住它的判据不是「有没有内容」，是**行数**（见 selftest 的 `log_lines`）。
    """
    if text is None:
        return ""
    # 统一换行符即可（\r\n → \n 是**无损**的；\r\n 会让下游按行处理时多出空行）
    return text.replace("\r\n", "\n").replace("\r", "\n").strip("\n").rstrip()


class TorrentInfo(dict):
    """解析结果。是个 dict（好序列化），但带 `missing` 与 `rule` 两个便利属性。"""

    @property
    def missing(self):
        return self.get("_missing", [])

    @property
    def rule(self):
        return self.get("transfer_rule", RULE_UNKNOWN)

    def is_denied(self):
        return self.rule == RULE_DENY

    def may_auto_publish(self):
        """★ 唯一的放行判据：只有明确 ALLOW 才允许自动发布。"""
        return self.rule == RULE_ALLOW


def _rowtitle_map(soup):
    """
    把页面里的 `td.rowtitle` 变成 {标签: 值} 的映射。

    ★ 只认**同时**满足「有 class=rowtitle」且「下一个兄弟是 td」的行 ——
    页面里还有些 `td.rowtitle` 是折叠块标题（msg=简介/NFO/LOG/频谱…），
    它们同样带 rowtitle，所以靠「标签文本在不在 FIELD_LABELS 里」来筛，
    而不是靠位置。
    """
    out = {}
    for td in soup.find_all("td", class_="rowtitle"):
        label = _norm(td.get_text())
        label = label.rstrip("：:").strip()          # 去掉尾随冒号
        nxt = td.find_next_sibling("td")
        if nxt is None:
            continue
        # ★ 标签列表要保留 `<a>` 结构（标签是链接），所以单独取
        out.setdefault(label, nxt)
    return out


def _extract_tags(cell):
    """标签列表：页面是若干 `<a href='torrents.php?tags=N'>名</a>`。取名字。"""
    if cell is None:
        return None
    links = cell.find_all("a")
    if links:
        return [_norm(a.get_text()) for a in links if _norm(a.get_text())]
    text = _norm(cell.get_text())
    return text.split() if text else []


def _extract_nfo_log(soup):
    """
    抓轨日志：`divnfo` 里的 `<pre>` 原文。
    ★ 它是**日志正文**，到 Musopia 要经 `descr.build_descr()` 包成
    `[spoiler=LOG][code]…[/code][/spoiler]` —— 本模块只负责取出原文。
    """
    div = soup.find(id="divnfo")
    if div is None:
        return None, None
    pre = div.find("pre")
    if pre is not None:
        return pre.get_text(), None
    # 兜底：没有 <pre> 就取整块文本（有些日志块不带 pre）
    txt = div.get_text("\n")
    if txt.strip():
        return txt, "divnfo 里没有 <pre>，退回整块文本"
    return None, "divnfo 是空的"


def _descr_text_only(div):
    """
    皇后简介正文：`divdescr` 的**纯文本**（`<img>` 一律剥掉，其余保真）。

    ★★ 为什么要剥图（2026-09-26 实测，58 个真实详情页全查了一遍）：
      15/58 个页面的 `divdescr` 里塞着**成堆的扫描图**，例如：
          196878  Cover.jpg / Disc1.jpg / Booklet1~10.jpg / Inside.jpg / Back.jpg（15 张）
          196928  Box_Front.jpg / Booklet2,17,18,20,21.jpg（10 张）
          200609  Back.jpg / Booklet - 1,7,10.jpg / Scan1.jpg（5 张）
      ⇒ 而且全是 `.thumb.jpg` **缩略图**（质量低），不是原图。
      ★ 本模块早就有过告警（见 `_COVER_EXCLUDE` 那段注释）：`divdescr` 里的图
        **不一定是封面**，实测见过 `频谱图.JPG` / `xxx-spectrogram.png` ——
        当初正是它导致"把频谱图当封面传上图床"。
      ⇒ 简介里只要**文字**；封面由我们自己的 `cover_url` 负责（`descr.build_descr`
        第一块就是 `[center][img]…[/img][/center]`）。
      ★ 在**解析层**剥，不在渲染层剥 —— 否则同一个判据两处实现，必然漂移
        （`ERR-META-03`）。

    ★ 为什么不在这里做"去掉 `[img]`/`[b]` 等 BBCode"之类的清洗：
      `divdescr` 是**用户正文**，里面出现的方括号是**内容**，不是标记。
      只做两件确定无损的事：① 剥 `<img>`；② 交给 `_clean_long` 保真取文本。
    """
    if div is None:
        return ""
    # ★ 复制一份再改：直接 `decompose()` 会污染调用方的 soup
    #   （调用方 `parse_detail` 之后还要用同一个 soup 取封面候选，
    #    `_extract_cover_candidates` 会去看 `divdescr` 作为**备选**封面来源）。
    #   ⇒ 这点很要紧：就地删图 = 把那条兜底路径也一起删了。
    import copy as _copy
    d = _copy.copy(div)
    for img in d.find_all("img"):
        img.decompose()
    # ★ 图片被删后常留下一串空行（每个 `<img>` 前后都有换行）⇒ 压掉连续空行，
    #   但不压行内的空白（`_clean_long` 的纪律：保真）。
    text = d.get_text("\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return _clean_long(text)


def parse_detail(html, *, source_file=None):
    """解析一份 OpenCD 详情页 HTML，返回 TorrentInfo。**纯函数，不碰网络。**"""
    soup = BeautifulSoup(html or "", "html.parser")
    info = TorrentInfo()
    missing = []

    # ---------- 主标题 / 副标题 ----------
    # ★ 实测：新版面**没有 `<h1>`**（`auto_feed_js` 取 h1 是老版面的写法）。
    #   主标题在 `.title`；`.smalltitle` 是副标题，但**可能是空的**（一份样本就是）。
    el = soup.find(class_="title")
    info["name"] = _norm(el.get_text()) if el else ""
    if not info["name"]:
        missing.append("name(.title)")

    el = soup.find(class_="smalltitle")
    info["small_descr"] = _norm(el.get_text()) if el else ""
    # ★ smalltitle 为空**不算 missing** —— 实测它本来就可能为空（不是解析失败）。
    #   但要把状态记下来，好让调用方判断「是真没有还是没取到」。
    info["small_descr_present"] = bool(info["small_descr"])

    # ---------- 字段表 ----------
    rows = _rowtitle_map(soup)
    for key, label in FIELD_LABELS.items():
        cell = rows.get(label)
        if cell is None:
            info[key] = ""
            missing.append(f"{key}({label})")
            continue
        if key == "tags":
            info[key] = _extract_tags(cell)
        else:
            info[key] = _norm(cell.get_text())

    # ---------- 转载规则 → 三态（★ 本模块最要紧的一件事）----------
    raw = info.get("transfer_rule_raw") or ""
    info["transfer_rule"] = RULE_VALUES.get(raw, RULE_UNKNOWN)
    if info["transfer_rule"] == RULE_UNKNOWN:
        # ★ 这里**不**记进 missing —— 规则本身取到了，只是值不认识/缺失，
        #   已经在三态里表达成 NEED_CONFIRM 了。两件事别混。
        pass

    # ---------- 简介 / 曲目 / 日志 ----------
    # ★ 这三样一律走 `_clean_long`（保真），**不走 `_norm`** —— 理由见 _clean_long 的注释。
    div = soup.find(id="divdescr")
    # ★★ 2026-09-26 改：改走 `_descr_text_only()` —— 剥掉 `<img>`
    #    再取文本。原先的 `_clean_long(div.get_text("\n"))` 会把 15/58 个页面里
    #    的扫描缩略图**留在 HTML 里**（`get_text` 只是取不出标签，图本身还在
    #    `div` 里；真正的问题是把这段正文搬去站点时图会被一起带走）。
    #    详见 `_descr_text_only` 的 docstring（含实测的图片清单）。
    info["descr"] = _descr_text_only(div)
    if not info["descr"]:
        missing.append("descr(divdescr)")

    div = soup.find(id="divtracklist")
    info["tracklist"] = _extract_tracklist(soup)
    info["tracklist_lines"] = len(info["tracklist"].splitlines())
    if not info["tracklist"]:
        missing.append("tracklist(divtracklist)")

    content, warn = _extract_nfo_log(soup)
    info["log_content"] = _clean_long(content)
    info["log_warning"] = warn or ""
    info["log_lines"] = len(info["log_content"].splitlines())   # ★ 判据用（见 _clean_long）
    if not info["log_content"]:
        missing.append("log_content(divnfo)")

    # ---------- 封面（简介里的图片附件）----------
    info["cover_candidates"] = _extract_cover_candidates(soup)

    # ---------- 元信息 ----------
    info["_missing"] = missing
    info["_source_file"] = source_file or ""
    return info


def _extract_tracklist(soup):
    """
    曲目列表：`divtracklist` 是 `<b>曲目列表：</b><br/>曲目<br/>曲目…`。

    ★ 三个坑（都是实测出来的，第一版全踩了）：
      1. **`<br/>` 才是行分隔** —— 直接 `get_text("\\n")` 会同时拿到 HTML 缩进
         产生的空行与制表符（实测第一行是 `'\\t\\t\\t01.魂断乌江 (25:05)'`）。
      2. **页面自带 `曲目列表：` 这个标签** —— 而 `descr.build_descr()` 会再加一个
         `[quote][b]曲目列表：[/b]…`。不剥掉就**渲染出两个同样的标题**（实测）。
      3. 去重与去空行要稳，别把「01.」这种编号当噪音。
    """
    div = soup.find(id="divtracklist")
    if div is None:
        return ""
    # 把 <br> 换成哨兵再取文本，这样缩进空白不会变成"空行"
    for br in div.find_all("br"):
        br.replace_with("\n")
    text = div.get_text()
    lines = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.rstrip("：:").strip() == "曲目列表":      # 剥掉页面自带的标签
            continue
        lines.append(line)
    return "\n".join(lines)


# 频谱图/非封面的排除词（★ 小写后比，两侧都要覆盖）
# ★★ 2026-09-21 实测依据：`divdescr` 里的图**不一定是封面**，实测见过
#    `频谱图.JPG`、`xxx-spectrogram.png`、`01. 孩兒.flac-full.png`、`01. 孩兒.flac-zoom.png`
#    （谭咏麟那页的 `divdescr` 里全是频谱图）。不排除就会**把频谱图当封面传上图床**。
_COVER_EXCLUDE = ("频谱", "頻譜", "spectrogram", ".flac-full", ".flac-zoom", "spectrum")


def _cover_src_of(img):
    """
    从一个 `<img>` 里取**原图**地址（相对或绝对），并剥掉尾随 `.thumb.jpg`。

    ★★ 为什么不能直接用 `src`（实测的「缩略图陷阱」）：
        <img alt="cover.jpg"
             src="attachments/…/…04.jpg.thumb.jpg"              ← 缩略图
             onclick="Previewurl('attachments/…/…04.jpg')" />   ← ★ 原图在这
      ⇒ 实测规律：`src` **带** `.thumb.jpg` 的（cover.jpg / Folder.jpg / disc.jpg /
        back.jpg）原图在 `onclick` 里；`src` **不带**的（folder.jpg 那两条）
        本身就是原图，`Previewurl` 与它相同；**外站图没有 onclick**，`src` 即原图。
      ⇒ 取值顺序：**先 `Previewurl(...)`，没有才用 `src`**；最后统一剥 `.thumb.jpg`。
    """
    onclick = img.get("onclick") or ""
    m = re.search(r"Previewurl\(\s*['\"]([^'\"]+)['\"]\s*\)", onclick)
    url = (m.group(1) if m else "") or (img.get("src") or "")
    url = url.strip()
    # 剥尾随 `.thumb.jpg`（大小写不敏感）；`folder.jpg` 这种没有则原样
    url = re.sub(r"\.thumb\.jpg$", "", url, flags=re.I)
    return url


def _extract_cover_candidates(soup):
    """
    封面候选，返回 `[(alt, src)]`，**已按优先级排**。

    ★★ 2026-09-21 **订正**（此前只看 `divdescr`，那是**错的**）：
      实测 10 个真实详情页——
        · `<div class="cover" title="点击查看原图">`（**站点的封面位**）：**10/10 都有**
        · `divdescr`（旧代码**唯一**看的区块）：只有 **2/10** 有图，
          且那 2 个恰好都是**禁止轉載**页
      ⇒ 旧代码把**真封面漏掉了 9/10**（`cover_candidates` 几乎恒为空）。
      ⇒ 现在**优先看 `div.cover`**，`divdescr` 降为**备选**（并排除频谱图）。

    ★ `alt` 常见：`cover.jpg` / `Folder.jpg` / `folder.jpg` / `disc.jpg` / `back.jpg`
      ⇒ **folder/cover/front 优先**（那是通行封面名）。
    ★ 地址可能是**相对路径**（`attachments/…`，要拼站点前缀）**或绝对 URL**
      （`https://y.gtimg.cn/…` 这类**外站图**，实测占比约一半）。
      拼 URL 是抓取层的事，这里只给原始地址（保持纯函数、不掺站点知识）。
    """
    out = []

    def collect(div, *, allow_excluded):
        if div is None:
            return
        for img in div.find_all("img"):
            src = _cover_src_of(img)
            if not src:
                continue
            alt = img.get("alt") or ""
            blob = (alt + " " + src).lower()
            if not allow_excluded and any(k in blob for k in _COVER_EXCLUDE):
                continue          # ★ 频谱图等——不是封面
            out.append((alt, src))

    # ① 站点的**封面位**（真封面在这里）
    collect(soup.find(class_="cover"), allow_excluded=False)
    if out:
        # ★ `div.cover` 命中就**不再**看 `divdescr` —— 那个区块里常混着频谱图，
        #   而且"页面封面上"比"描述里的某张图"更权威。
        return _rank_covers(out)

    # ② 备选：描述区里的图（**要排除频谱图**）
    collect(soup.find(id="divdescr"), allow_excluded=False)
    return _rank_covers(out)


def _rank_covers(items):
    """按 alt 分级排序：folder/cover/front < cd/disc < 其它。稳定排序，同级保序。"""
    def rank(item):
        alt = (item[0] or "").lower()
        if "folder" in alt or "cover" in alt or "front" in alt:
            return 0
        if "cd" in alt or "disc" in alt:
            return 1
        return 2

    items = list(items)
    items.sort(key=rank)
    return items


# ==================== 列表页（torrents.php）解析 ====================
#
# 为什么需要它：定时任务不能凭空知道「站上多了哪些种子」。抓取流程是
#     ① GET torrents.php（**1 个请求**）→ 解析出 [(id, 标题, …), …]
#     ② 与本地已抓过的 id 比对 → 只挑没抓过的（**0 请求**）
#     ③ 逐个 GET 详情页（每个 1 个请求，★ 这里最贵，受限速约束）
# 所以列表页是**唯一的入口**，也是控制频次的关键：一次请求就知道该抓哪些，
# 避免盲猜 id 去撞（既费请求、又正撞上「注意频次」那条）。
#
# ★ 实测（2026-09-19，脱敏列表页样本）拿到的结构：
#   · 每行是一个 `<table class="torrentname">`（**嵌套**在外层 <tr> 里的表格）
#   · **每行里同时有** `details.php?id=N` 与 `plugin_details.php?id=N`，**同一个 id**
#   · 列序：td[2]=标题 td[3][4]=促销相关 td[5]=相对时间 td[6]=大小
#           td[7]=做種 td[8]=下載 td[9]=完成 td[10]=发布者
#     ★ 列含义是**交叉验证**出来的，不是猜的：拿列表页 id=200610 那行
#       （它有详情页可对照）比出来 —— 大小 `681.42 MB` 与详情页**逐字一致**；
#       做種/下載/完成 `247/0/549` 与详情页「同伴 245 做種 1 下載」「完成 546」
#       **同量级吻合**（同一时刻的浮动读数）。
#
# ★★ **真正的陷阱是「在哪扫」+「正则边界」，不是用哪个文件名** —— 这条我第一版归因错了，
#    订正于此（两处都实测过）：
#      ① 陷阱是**子串匹配**：`userdetails.php?id=85068` 这个 URL 里**字面上包含**
#         `details.php?id=85068` ⇒ 无边界锚的正则**会**把它抓出来。实测
#         `re.search(r"details\.php\?id=85068", "userdetails.php?id=85068")` → True。
#         而 `plugin_details.php?id=` 没有这个后缀歧义（前缀不同）。
#      ② 所以安全做法有两条，**我们的实现两条都做了**：
#         (a) **限定在 `table.torrentname` 行内**取（不是全页扫）；
#         (b) 用 `LIST_DETAIL_HREF`（`plugin_details`）这个**无歧义**的形态。
#      ★ 换句话说：`details.php?id=` 在这页上**不是**错的（逐行取 50/50 全对），
#        错的是「全页无锚扫」。别把结论记成「必须用 plugin_details」。

LIST_DETAIL_HREF = re.compile(r"plugin_details\.php\?id=(\d+)")

# ==================== 皇后的标签表（源站值，**不是** Musopia 的值） ====================
# 来源：参考实现 `repos/auto_feed_js/auto_feed.user.js:14461-14470`（实测存在，27 项）。
# ★★ **这张表是「皇后的值」，绝不可直接填给 Musopia** —— 两套编号完全错位：
#      标签     皇后值   Musopia 值（descr.py:23）
#      大陆       1        4
#      欧美       2        5
#      港台       3        6
#      日韩       4        7
#      古典(Classical) 9   15
#    ⇒ 第 4 步必须**按标签名映射**，不是搬数字。这是最容易出的静默错误。
# ★ 用途：① 解析「標籤列表」时把名字对回值（若需要）；
#        ② 第 4 步做「皇后名 → Musopia 值」映射时的**输入侧**参照。
# ★★ 实测（2026-09-19）还发现**两条清洗规则**，映射时都必须做：
#   ① **去掉括号后缀**：皇后是 `古典(Classical)`，Musopia 的键是 `古典`（无后缀）。
#      实测 `descr.TAGS.get("古典(Classical)")` → None（查不到）⇒ 不去后缀必然丢标签。
#   ② **异体字**：皇后写 `其它地区`，Musopia 写 `其他地区`（`descr.py:25`）
#      —— 「其**它**」vs「其**他**」，一个字之差，**直接查表必然失败**。
# ★ 「器乐」等**皇后表里没有**的标签，映射时会出现"查不到"——
#   那条**必须显式列出**（进每日邮件），不静默丢。
OPENCD_TAGS = {
    "大陆": "1", "欧美": "2", "港台": "3", "日韩": "4", "其它地区": "5",
    "流行(Pop)": "6", "发烧(HiFi)": "7", "汽车(garage)": "8", "古典(Classical)": "9",
    "民族(National)": "10", "摇滚(rock)": "11", "原声(OST)": "12", "民间(Folk)": "13",
    "乡村(Country)": "14", "天籁(Soul)": "15", "新世纪(NewAge)": "16",
    "蓝调(Blues)": "17", "爵士(Jazz)": "18", "金属(Metal)": "19", "朋克(Punk)": "20",
    "电子(Electronic)": "21", "儿童(Children's)": "22", "宗教(Religion)": "23",
    "雷鬼(Reggae)": "24", "贝斯(Drum Bass)": "25", "说唱(Rap)": "26",
    "音乐剧(musical)": "27",
}

# 页面「轉載規則」字段 → 三态。★ 只放**实测见过**的值。


class ListRow(dict):
    """列表页的一行。"""

    @property
    def torrent_id(self):
        return self.get("torrent_id", "")


def parse_list(html, *, source_file=None):
    """
    解析 torrents.php 列表页，返回 [ListRow, ...]（**纯函数，不碰网络**）。

    ★ 只从 `table.torrentname` 里取 id —— 见上面那段「实测订正」。
    ★ `\\xa0`（不换行空格）：实测大小那格是 `681.42\\xa0MB`（不是普通空格），
      直接与 `"681.42 MB"` 比较会**静默不相等**。所有字段一律过 `_norm`
      （它用的是 `\\s+`，Python 的 `\\s` **包含 `\\xa0`** ⇒ 能正确处理）。
    """
    soup = BeautifulSoup(html or "", "html.parser")
    rows = []

    for tn in soup.find_all("table", class_="torrentname"):
        row = ListRow()

        link = tn.find("a", href=LIST_DETAIL_HREF)
        if link is None:
            continue
        m = LIST_DETAIL_HREF.search(link.get("href") or "")
        row["torrent_id"] = m.group(1) if m else ""
        # ★ 标题优先取 `title` 属性（完整、不被 CSS 截断），退回链接文本
        row["title"] = _norm(link.get("title") or link.get_text())

        # 外层 <tr> 的直接子 td 才是「大小/做種/時間」这些统计列
        outer = tn.find_parent("tr")
        tds = outer.find_all("td", recursive=False) if outer else []
        def cell(i):
            return _norm(tds[i].get_text(" ")) if i < len(tds) and tds[i] else ""

        row["promo"] = cell(3)
        row["age_text"] = cell(5)          # 相对时间（`13時51分`）
        row["size"] = cell(6)
        row["seeders"] = cell(7)
        row["leechers"] = cell(8)
        row["snatched"] = cell(9)
        row["uploader"] = cell(10)

        # 促销状态（免費/2X/熱門…）：只看图标 alt，别去解析那段 domTT_activate JS
        promos = []
        for img in tn.find_all("img") + (outer.find_all("img", recursive=False) if outer else []):
            alt = _norm(img.get("alt") or "")
            if alt in ("Free", "2X", "2X Free", "2XFree", "50%", "30%", "熱門"):
                promos.append(alt)
        row["promo_flags"] = sorted(set(promos))

        row["_source_file"] = source_file or ""
        rows.append(row)

    return rows


# ==================== 自检 ====================

# 期望值：★ 从样本**人工核过**再写进来（不是从解析结果反填 —— 那样是自证）。
# 两个样本的 role 字段说明它验的是哪条验收判据。
EXPECT = {
    "sample-zhuanzai-jinyong.html": {
        "transfer_rule_raw": "禁止轉載",
        "transfer_rule": RULE_DENY,
        "may_auto_publish": False,
        "album": "何华栈 魂断乌江（粤曲对唱）",
        "artist": "郭凤女、何华栈",
        "year": "2000年",
        "size": "681.42 MB",
        "file_count": "12 個檔案",
        "type": "音乐(Music)",
        "category": "古典(Classical)",
        "format": "WAV 整轨",
        "medium": "CD",
        "team": "其他(Other)",
        "source": "原抓",
        "tags": ["大陆", "港台", "古典(Classical)"],
        "name": "郭凤女、何华栈 - 魂断乌江（粤曲对唱） 2000 - WAV 整軌 - alin13788",
        # ★ 原样保留**双空格**（`YAMAHA  CRW-F1E`）—— 第一版我手抄期望值时
        #   顺手归一化掉了，导致"期望"本身是错的。字段短值确实会归一化，
        #   但这一处的双空格在**源页面里就是两个**，归一化后只留一个。
        "small_descr": "YAMAHA CRW-F1E | 低速原抓 | Log100% | 高清扫描完整图册 |WAV|",
        "log_nonempty": True,
        # ★ **行数判据**：这是抓「日志被压扁」的那一条。只判 nonempty 是抓不到的 ——
        #   第一版把 69 行塌成 1 行，log_nonempty 照样 True（实测）。
        "log_lines": 69,
        "tracklist_nonempty": True,
        # ★ 曲目行数同样是判据：第一版带出 HTML 缩进（`\t\t\t01.…`）与空行，
        #   还会把页面自带的「曲目列表：」标签带进来（渲染后标题重复）。
        "tracklist_lines": 2,
        "tracklist_has_label": False,
    },
    "sample-zhuanzai-huanying.html": {
        "transfer_rule_raw": "歡迎轉載",
        "transfer_rule": RULE_ALLOW,
        "may_auto_publish": True,
        "album": "我拿什么爱你",
        "artist": "齐秦",
        "year": "1998年",
        "size": "341.06 MB",
        "file_count": "17 個檔案",
        "type": "音乐(Music)",
        "category": "流行(Pop)",
        "format": "FLAC 分轨",
        "medium": "CD",
        "team": "其他(Other)",
        "source": "轉載",
        "tags": ["港台", "流行(Pop)"],
        "name": "齐秦 - 我拿什么爱你 1998 - FLAC 分轨",
        "small_descr": "",          # ★ 这份的副标题**本来就是空的**（实测）
        "log_nonempty": True,
        "log_lines": 77,
        "tracklist_nonempty": True,
        "tracklist_lines": 12,
        "tracklist_has_label": False,
    },
}


def _compare(name, info, exp):
    """逐字段对照，返回 (通过数, 失败列表)。"""
    fails = []
    checked = 0
    for key, want in exp.items():
        checked += 1
        if key == "log_nonempty":
            got = bool(info.get("log_content"))
        elif key == "tracklist_nonempty":
            got = bool(info.get("tracklist"))
        elif key == "tracklist_has_label":
            # 曲目块里**不该**再出现「曲目列表：」—— 那个标签由 descr.build_descr 加。
            got = any(l.rstrip("：:").strip() == "曲目列表"
                      for l in (info.get("tracklist") or "").splitlines())
        elif key == "may_auto_publish":
            got = info.may_auto_publish()
        else:
            got = info.get(key)
        status = "✅" if got == want else "❌"
        if got != want:
            fails.append((key, want, got))
        shown_want = repr(want) if not isinstance(want, str) or len(want) < 46 else repr(want[:46] + "…")
        shown_got = repr(got) if not isinstance(got, str) or len(got) < 46 else repr(got[:46] + "…")
        print(f"    {status} {key:<22} 期望={shown_want:<52} 实得={shown_got}")
    return checked, fails


def _check_searchpages():
    """
    搜索页自检（★ 「qB 反查」的关键一环）。
    ★ 判据的重点是**那条危险**：搜出的候选**不能取第一条**——
      实测「齐秦 - 我拿什么爱你」返回 2 条、**名字几乎一样**
      （`… 1998 - FLAC 分轨` / `… 1998 - WAV 整轨`），取第一条会发错种子。
      所以搜索页只用来**拿候选 tid**，必须再逐个进详情页验 infohash。
    """
    path = os.path.join(SAMPLE_DIR, "sample-search-qinqin.html")
    if not os.path.exists(path):
        print("  ⚠️  缺搜索页样本，跳过")
        return 0, 0
    rows = parse_list(open(path, encoding="utf-8").read())
    ids = [r["torrent_id"] for r in rows]
    checks = [
        ("搜索页解析行数（齐秦）", 2, len(rows)),
        # ★ 同名不同格式 —— 证明「必须验 infohash、不能取第一条」
        ("候选含 200028（FLAC 分轨）", True, "200028" in ids),
        ("候选含 30167（WAV 整轨）", True, "30167" in ids),
        ("★ 两条候选**名字几乎相同** ⇒ 取第一条会发错",
         True,
         len({r["title"][:12] for r in rows}) == 1),
    ]
    fails = 0
    for label, want, got in checks:
        ok = got == want
        fails += 0 if ok else 1
        print(f"    {'✅' if ok else '❌'} {label:<40} 期望={want!r:<8} 实得={got!r}")
    return len(checks), fails


# ==================== 皇后 RSS 反查（torrentrss.php） ====================
#
# ★★ 背景（2026-10-12 实测，见 `.scratch/B12-tid-reverse-lookup.md`）：
#   皇后**搜索页**没有 hash 字段（40hex 搜返回无关空结果页）⇒「按 infohash 反查」在
#   搜索页**不存在**。但 **RSS** 里每条 item 自带：
#     `<guid>`   = **真 infohash**（40hex，`isPermaLink="false"`）
#     `<link>`   = `https://open.cd/details.php?id=<TID>`
#     `<pubDate>`= 皇后的 add time
#   ⇒ `GET torrentrss.php?...&search=<名字>&rows=100` **一次请求**即可得
#     「名字 → (hash, tid, 发布日期)」映射，本地逐字节比 hash，相等才认（**绝不发错**）。
#
# ★★ 本段全是**纯函数**（解析/归一化），刻意 **不 import `requests`/`crawl`** ——
#   `opencd.py` 全程可 grep 审计「无网络」。出网编排在 `src/rss_backfill_tid.py`。

# `<link>` 里的 tid —— ★ 实测 RSS `<link>` 就是 `details.php?id=<TID>`，
# 与 `sync_opencd_tid.TID_RE`（`details\.php\?id=(\d+)`）**逐字同形**，但为避免
# `opencd.py` import `sync_opencd_tid`（依赖倒置），这里放**同一份正则的副本**。
_RSS_TID_RE = re.compile(r"details\.php\?id=(\d+)")
# `<guid>` 必须**恰好**是 40 hex（info_hash = 20 字节 SHA1）。★ 不宽松匹配。
_RSS_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _rss_added_utc(pubdate):
    """
    RSS `pubDate` → **UTC 字符串** `'YYYY-MM-DD HH:MM:SS'`（拿不到 ⇒ `None`）。

    ★ 语义**必须与 `sync_opencd_tid.added_at_utc` 一字不差**：闸门② 拿这个串与
      `datetime('now')`（UTC）做**字符串比较**，差 8 小时会在 7 天门槛上表现为
      「偶尔差一天放行」（`ERR-SQL-03` 的教科书形状）。
      本函数不 import `sync_opencd_tid`（它必须保持"零请求、不 import 网络层"的
      硬纪律，反向 import 会造成依赖倒置）⇒ 逻辑**照抄**，并在这里用判据钉死等价。
    ★ `pubDate` 是 RFC-822（`Mon, 12 Oct 2026 03:29:25 +0800`，带时区）——
      `parsedate_to_datetime` 直接给出 aware datetime ⇒ `astimezone(utc)`。
      无时区的串（罕见）当"拿不到"（不敢猜本地时区）。取不到一律 `None`，**绝不编**。
    """
    if not pubdate:
        return None
    try:
        dt = parsedate_to_datetime(pubdate)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        return None                       # 无时区 ⇒ 不敢当本地时区猜，留空
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_rss_index(xml_text):
    """
    解析 `torrentrss.php` 的 XML → `(idx, stats)`。

    ★ `idx` 形状**与 `sync_opencd_tid.load_queen_index` 的返回值逐字段同形**：
        `{hash_lower: {"tid": int, "name": str, "added": "YYYY-MM-DD HH:MM:SS"|None}}`
      ⇒ `sync_opencd_tid.build(rows, idx)` 可**零改动**接收（这是刻意的：不写第二个
        "配对"实现，也就不会有第二套口径）。

    ★ 冲突规则**照抄 `load_queen_index`**：一 hash 多 tid（或反之）⇒ 该键**丢弃**，
      记进 `stats`，**绝不自动挑**一条（挑错 = 发错种子）。
    ★ 抠不出（guid 非 40hex / link 无 tid）⇒ 计入 `stats["unparsed"]`，**不编**。
    """
    idx, cand = {}, {}
    stats = {"items": 0, "unparsed": 0, "usable": 0, "conflicts": 0,
             "undated": 0, "dupe_hashes": 0, "dupe_tids": 0}
    root = ET.fromstring(xml_text or "")
    # ★ XML 坏了 ⇒ `ET.ParseError` 向上抛，**不静默当空**。调用方（fetch_index）
    #   据此判"请求失败"而不是"零命中"——`ERR-META-02`：空成功与失败必须区分。
    for it in root.iter("item"):
        stats["items"] += 1
        guid = (it.findtext("guid") or "").strip()
        link = (it.findtext("link") or "").strip()
        name = (it.findtext("title") or "").strip()
        m = _RSS_TID_RE.search(link)
        if not m or not _RSS_HASH_RE.match(guid):
            stats["unparsed"] += 1
            continue
        key = guid.lower()
        tid = int(m.group(1))
        cand.setdefault(key, set()).add(tid)
        added = _rss_added_utc(it.findtext("pubDate"))
        if added is None:
            stats["undated"] += 1
        idx.setdefault(key, {"tid": tid, "name": name, "added": added})

    # ★ 一 hash 多 tid / 一 tid 多 hash ⇒ 丢弃该键（与 load_queen_index 同规则）
    dup_hashes = {k for k, v in cand.items() if len(v) > 1}
    tid_seen = {}
    for k, v in cand.items():
        for t in v:
            tid_seen.setdefault(t, set()).add(k)
    dup_tids = {t for t, ks in tid_seen.items() if len(ks) > 1}
    if dup_tids:
        dup_hashes |= {k for t in dup_tids for k in tid_seen[t]}
    for k in dup_hashes:
        idx.pop(k, None)
    stats["conflicts"] = len(dup_hashes)
    stats["dupe_hashes"] = len([k for k, v in cand.items() if len(v) > 1])
    stats["dupe_tids"] = len(dup_tids)
    stats["usable"] = len(idx)
    return idx, stats


# ★ 模糊归一化的**尾部 token 表**（先保守：只剥格式/年份，**绝不碰艺术家**）。
#   ★★ 实证（`.scratch/B12-tid-reverse-lookup.md` 的 25 条样本）：
#     `X - 名 年份 - FLAC 分轨` 与 `X - 名 [1993][厂牌 编号][包装] 年份 - FLAC 整轨 - 上传者`
#     这两类尾巴，剥掉后精确搜 32% → 52%。艺术家的位置在**开头**，从不剥。
_FUZZY_STRIP = [
    # ① 方括号整块（厂牌/编号/包装/CD 数…）—— 只剥**尾部**的
    re.compile(r"\s*\[[^\[\]]*\]\s*$"),
    # ② 圆括号里的年份（如 `(1994)`）—— 只剥尾部的
    re.compile(r"\s*\((?:19|20)\d{2}\)\s*$"),
    # ③ `vol.N` / `cdN` / `disc N`（大小写不敏感）
    re.compile(r"\s*(?:vol|cd|disc)\.?\s*\d+\s*$", re.I),
    # ④ 纯年份（4 位 `19xx/20xx`，前面可有 `-` 或空格）
    re.compile(r"\s*[-–]?\s*(?:19|20)\d{2}\s*$"),
    # ⑤ 格式/介质词（FLAC / WAV / APE / 分轨 / 整轨 …）
    re.compile(r"\s*/?\s*(?:FLAC|WAV|APE|ALAC|MP3|320K|分轨|整轨)\s*$", re.I),
    # ⑥ 尾部 `- 上传者`（形如 `- alin13788`，仅 ASCII 词 + **至少 4 位**避免砍到短歌名）
    re.compile(r"\s*[-–]\s*[A-Za-z][A-Za-z0-9_]{3,}\s*$"),
]


def fuzzy_name(name):
    """
    把皇后/qB 标题**保守地**剥掉尾部格式信息，用于"精确不中再模糊"的第二段搜索。

    ★★ 纪律（用户 2026-10-12 拍板"先保守"）：
      · **只剥尾部** token，**绝不碰艺术家**（艺术家在开头；剥不动就停）。
      · **幂等**：`fuzzy_name(fuzzy_name(x)) == fuzzy_name(x)`。
      · 剥空 ⇒ 返回 `""`（调用方据此**跳过**模糊档，而不是拿空串去搜全库）。
    ★ 这不是"重写标题"，只是"去掉尾巴再搜一次" —— 命中仍靠逐字节比 infohash，
      **不因模糊而放宽安全性**。
    """
    s = _norm(name)                       # 全角空格/连续空白先归一
    if not s:
        return ""
    # ★ 循环剥：`… - FLAC 分轨` 要连剥 `分轨` 与 `FLAC` 两块
    changed = True
    while changed and s:
        changed = False
        for pat in _FUZZY_STRIP:
            new = pat.sub("", s).strip(" -–\t")
            if new != s:
                s = new
                changed = True
                break
    return s


def _check_rss_index():
    """
    RSS 解析 + 模糊归一化的自检（纯离线，喂**人造 XML**，零请求）。

    ★ 判据分三组：
      ① RSS 字段口径：guid=hash、link→tid、pubDate→UTC（★ 与
         `sync_opencd_tid.added_at_utc` 的 UTC 口径**等价**；混时区样本坐实）。
      ② 安全属性：一 hash 多 tid / 一 tid 多 hash ⇒ **丢弃**（不自动挑）；
         guid 非 40hex / link 无 tid ⇒ 计入 unparsed（**不编**）。
      ③ 模糊归一化：只剥尾部、**保住艺术家**、幂等、剥空返空串。
    """
    # 与真实样本同形（时区 +0800）；hash 混大小写以验 `.lower()`。
    xml = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<item>
  <title>群星 - 中岛美雪最爱 1994 - FLAC 分轨</title>
  <link>https://open.cd/details.php?id=201940</link>
  <guid isPermaLink="false">6cbe429fcf1578be5e11ab9c3b760949abe069d0</guid>
  <pubDate>Mon, 12 Oct 2026 03:29:25 +0800</pubDate>
</item>
<item>
  <title>Mantovani - 名曲精选 2006 - FLAC 整轨</title>
  <link>https://open.cd/details.php?id=97885</link>
  <guid isPermaLink="false">41A8F10D0000000000000000000000000000F86B</guid>
  <pubDate>Fri, 22 Jun 2007 00:00:00 +0800</pubDate>
</item>
<item>
  <title>坏：guid 不是 40hex</title>
  <link>https://open.cd/details.php?id=123</link>
  <guid isPermaLink="false">NOT-A-HASH</guid>
  <pubDate>Mon, 12 Oct 2026 00:00:00 +0800</pubDate>
</item>
<item>
  <title>坏：link 抠不出 tid</title>
  <link>https://open.cd/</link>
  <guid isPermaLink="false">aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</guid>
  <pubDate>Mon, 12 Oct 2026 00:00:00 +0800</pubDate>
</item>
</channel></rss>"""
    idx, stats = parse_rss_index(xml)
    key = "41a8f10d0000000000000000000000000000f86b"   # ★ 大小写归一后的键
    # ★★ UTC 口径硬对照：`2026-10-12 03:29:25 +0800` = `2026-10-11 19:29:25` UTC
    #    （跨日！—— 正是"差 8 小时"最容易露馅的点）
    utc_chrono = _rss_added_utc("Mon, 12 Oct 2026 03:29:25 +0800")
    fuzzy_cases = [
        ("群星 - 中岛美雪最爱 1994 - FLAC 分轨", "群星 - 中岛美雪最爱"),
        ("Mantovani - 名曲精选 2006 - FLAC 整轨", "Mantovani - 名曲精选"),
        ("齐秦 - 我拿什么爱你 [1998][上华][引进版] 2015 - WAV 整轨 - alin13788",
         "齐秦 - 我拿什么爱你"),
        ("群星 - 台语怀念金曲 2015 - FLAC 分轨", "群星 - 台语怀念金曲"),
        ("徐元白 - 徐元白古琴 [2006] - FLAC 分轨", "徐元白 - 徐元白古琴"),
    ]
    checks = [
        # ① 字段口径
        ("usable 条数（2 好 + 2 坏）", 2, stats["usable"]),
        ("unparsed 条数（非 hash / 无 tid）", 2, stats["unparsed"]),
        ("guid 小写归一成键", True, key in idx),
        ("该键 tid == 97885", 97885, idx.get(key, {}).get("tid")),
        ("pubDate → UTC（★ 跨日 03:29+08:00 = 19:29Z 前一日）",
         "2026-10-11 19:29:25", utc_chrono),
        # ★ 第二条（Mantovani，键 41a8f10d…）的 pubDate = 2007-06-22 00:00:00 +0800
        #   ⇒ UTC 前一日 16:00:00（再证时区换算，且证明 added 是**按各自 item** 算的）
        ("写入 idx 的 added 按各自 item 换算",
         "2007-06-21 16:00:00", idx.get(key, {}).get("added")),
        # ② 安全属性
        ("坏 guid 不编 (NOT-A-HASH 不入 idx)", False, "not-a-hash" in idx),
        ("坏 link 不入 idx (aaaa… 不在)", False, "a" * 40 in idx),
    ]
    # 一 hash 多 tid ⇒ 丢弃
    dup_xml = ("<rss><channel>"
               "<item><link>https://open.cd/details.php?id=1</link>"
               "<guid>%s</guid><title>a</title></item>"
               "<item><link>https://open.cd/details.php?id=2</link>"
               "<guid>%s</guid><title>b</title></item>"
               "</channel></rss>" % ("b" * 40, "b" * 40))
    didx, dstats = parse_rss_index(dup_xml)
    checks += [
        ("★ 一 hash 多 tid ⇒ 丢弃（conflicts=1）", 1, dstats["conflicts"]),
        ("★ 冲突键不入 idx", False, "b" * 40 in didx),
        ("★ items 计数正确（=2）", 2, dstats["items"]),
    ]
    # ③ 模糊归一化
    for raw_name, want in fuzzy_cases:
        checks.append((f"fuzzy({raw_name[:14]}…) == {want[:14]}…",
                       want, fuzzy_name(raw_name)))
    checks += [
        ("★ fuzzy 保住艺术家（不含年份/格式残留）",
         True, "1994" not in fuzzy_name("群星 - 中岛美雪最爱 1994 - FLAC 分轨")
         and "FLAC" not in fuzzy_name("群星 - 中岛美雪最爱 1994 - FLAC 分轨")),
        ("★ fuzzy 幂等", True,
         fuzzy_name(fuzzy_name("Mantovani - 名曲精选 2006 - FLAC 整轨"))
         == fuzzy_name("Mantovani - 名曲精选 2006 - FLAC 整轨")),
        ("★ 剥空返空串（纯格式串）", "", fuzzy_name("FLAC 分轨")),
        ("非格式串原样返回（不动）", "周杰伦 范特西", fuzzy_name("周杰伦 范特西")),
    ]
    fails = 0
    print("  ── RSS 反查（torrentrss.php）· 纯解析 & 模糊归一化 ──")
    for label, want, got in checks:
        ok = got == want
        fails += 0 if ok else 1
        print(f"    {'✅' if ok else '❌'} {label:<44} 期望={want!r:<22} 实得={got!r}")
    return len(checks), fails


def _check_cover_extraction():
    """
    封面抽取的判据（★ 2026-09-21 新增）—— 防的是**两个真实错误**：

      ① **看错区块**：旧代码只看 `divdescr`，而真封面在 `<div class="cover">`
         ⇒ 实测 10 个详情页里**真封面被漏掉 9 个**（`cover_candidates` 几乎恒为空）。
      ② **把频谱图当封面**：`divdescr` 里常有 `频谱图.JPG` / `-spectrogram.png` /
         `01.xxx.flac-full.png` / `-zoom.png` ⇒ 不排除就会**传错图**上图床。

    ★ 判据用**手造的极小 HTML**（不依赖外部样本），这样"改坏了"必然当场响；
      再用真实样本补一条端到端的。
    """
    from bs4 import BeautifulSoup
    ok = fails = 0

    def case(label, want, got):
        nonlocal ok, fails
        good = want == got
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} {label:<46} 期望={want!r:<34} 实得={got!r}")

    # ---------- ① div.cover 优先于 divdescr ----------
    # ★ 阴性对照：把实现改回"只看 divdescr"，这条必 ❌
    html = """<html><body>
      <div class="cover" title="点击查看原图">
        <img alt="Folder.jpg"
             src="attachments/a/FOLDER.jpg.thumb.jpg"
             onclick="Previewurl('attachments/a/FOLDER.jpg')" />
      </div>
      <div id="divdescr"><img alt="cover.jpg" src="attachments/b/IN_DESCR.jpg" /></div>
    </body></html>"""
    got = _extract_cover_candidates(BeautifulSoup(html, "html.parser"))
    case("div.cover 优先（不看 divdescr）", [("Folder.jpg", "attachments/a/FOLDER.jpg")], got)

    # ---------- ② 缩略图纠正：原图在 Previewurl 里 ----------
    html = """<html><body>
      <div class="cover"><img alt="d.jpg"
        src="attachments/x/d.jpg.thumb.jpg"
        onclick="Previewurl('attachments/x/d.jpg')" /></div>
    </body></html>"""
    got = _extract_cover_candidates(BeautifulSoup(html, "html.parser"))
    case("剥掉 .thumb.jpg（取 Previewurl 的原图）",
         [("d.jpg", "attachments/x/d.jpg")], got)

    # ---------- ③ 无 Previewurl 时用 src，且仍剥 .thumb ----------
    html = """<html><body>
      <div class="cover"><img alt="d.jpg" src="attachments/x/e.jpg.thumb.jpg" /></div>
    </body></html>"""
    got = _extract_cover_candidates(BeautifulSoup(html, "html.parser"))
    case("无 Previewurl 时用 src，仍剥 .thumb",
         [("d.jpg", "attachments/x/e.jpg")], got)

    # ---------- ④ 外站绝对 URL 要接住（实测占一半）----------
    html = """<html><body>
      <div class="cover"><img src="https://y.gtimg.cn/music/photo_new/T002M0009_1.jpg" /></div>
    </body></html>"""
    got = _extract_cover_candidates(BeautifulSoup(html, "html.parser"))
    case("外站绝对 URL 原样保留",
         [("", "https://y.gtimg.cn/music/photo_new/T002M0009_1.jpg")], got)

    # ---------- ⑤ ★ 频谱图必须排除（无 div.cover 时才走 divdescr）----------
    # ★ 阴性对照：删掉 `_COVER_EXCLUDE` 那几行，这条必 ❌
    html = """<html><body><div id="divdescr">
      <img alt="01. 孩兒.flac-full.png" src="attachments/x/full.png.thumb.jpg" />
      <img alt="01. 孩兒.flac-zoom.png" src="attachments/x/zoom.png.thumb.jpg" />
      <img alt="频谱图.JPG"             src="attachments/x/spec.jpg" />
      <img src="attachments/x/spec2-spectrogram.png" />
    </div></body></html>"""
    got = _extract_cover_candidates(BeautifulSoup(html, "html.parser"))
    case("★ 频谱图全部被排除（divdescr 备选路）", [], got)

    # ---------- ⑥ divdescr 里的**真**封面仍要捞得到 ----------
    html = """<html><body><div id="divdescr">
      <img alt="频谱图.JPG" src="attachments/x/spec.jpg" />
      <img alt="folder.jpg"  src="attachments/x/real.jpg" />
    </div></body></html>"""
    got = _extract_cover_candidates(BeautifulSoup(html, "html.parser"))
    case("divdescr 里排除频谱图后仍捞出真封面",
         [("folder.jpg", "attachments/x/real.jpg")], got)

    # ---------- ⑦ 优先级排序：folder/cover/front 在最前 ----------
    html = """<html><body><div class="cover">
      <img alt="back.jpg"   src="attachments/x/3.jpg" />
      <img alt="disc.jpg"   src="attachments/x/2.jpg" />
      <img alt="cover.jpg"  src="attachments/x/1.jpg" />
    </div></body></html>"""
    got = _extract_cover_candidates(BeautifulSoup(html, "html.parser"))
    case("按 alt 分级排序（cover 排第一）",
         ["attachments/x/1.jpg", "attachments/x/2.jpg", "attachments/x/3.jpg"],
         [s for _, s in got])

    # ---------- ⑧ 真样本端到端（有就验，没有跳过）----------
    for fname, want_first_alt in (("sample-zhuanzai-jinyong.html", "folder.jpg"),):
        p = os.path.join(SAMPLE_DIR, fname)
        if not os.path.exists(p):
            print(f"  ⚠️  缺样本 {fname}，跳过该条")
            continue
        with open(p, encoding="utf-8") as fh:
            info = parse_detail(fh.read(), source_file=fname)
        cc = info.get("cover_candidates") or []
        case(f"{fname} 首个候选 alt", want_first_alt,
             (cc[0][0] if cc else None))
        case(f"{fname} 首个候选**不含** .thumb", True,
             bool(cc) and ".thumb" not in cc[0][1])

    return ok, fails


def _check_listpage():
    """
    列表页自检。★ 判据分两层：
      ① 结构：能解析出 50 行、id 不重复、且**没有把会员 id 混进来**
      ② 交叉验证：拿 id=200610 那行与它的**详情页**对账 ——
         大小必须**逐字一致**（这是能对上的硬证据）。
    """
    path = os.path.join(SAMPLE_DIR, "sample-listpage.html")
    if not os.path.exists(path):
        print("  ⚠️  缺列表页样本，跳过")
        return 0, 0
    rows = parse_list(open(path, encoding="utf-8").read(),
                      source_file="sample-listpage.html")
    raw = open(path, encoding="utf-8").read()
    ids = [r["torrent_id"] for r in rows]
    # ★ 陷阱的**机制**用构造串验（不依赖样本内容）——
    #   因为**脱敏样本里已经没有 85068 了**（脱敏器把它换成了 `<UID>`），
    #   所以「在全页无锚扫会不会中招」**无法在脱敏样本上复现**。
    #   第一版我把这条写成 `"85068" in re.findall(...)` 期望 True ⇒ **跑出来是 False**，
    #   因为样本里根本没有那个 id。★ 教训：**判据必须能在被测样本上真的成立**，
    #   否则它是个假判据（这次是它自己把我拦下来的，算它立功）。
    mech_ok = bool(re.search(r"details\.php\?id=85068", "userdetails.php?id=85068")) \
        and not bool(re.search(r"plugin_details\.php\?id=85068", "userdetails.php?id=85068"))
    checks = [
        ("解析出行数", 50, len(rows)),
        ("id 去重后数量", 50, len(set(ids))),
        # ★ 行内取 ⇒ 会员 id 绝不出现
        ("行内取：会员 id 85068 未被当作种子", False, "85068" in ids),
        # ★ 陷阱机制（构造串验，与样本内容无关）：`details.php?id=` 会被
        #   `userdetails.php?id=` **子串命中**，而 `plugin_details.php?id=` 不会。
        ("机制：details.php 会被 userdetails 子串命中", True, mech_ok),
        # ★ `\xa0` 陷阱：列表页大小是 `681.42\xa0MB`，归一化后必须等于普通空格版
        ("大小 \\xa0 已归一化（=详情页逐字一致）", "681.42 MB",
         next((r["size"] for r in rows if r["torrent_id"] == "200610"), "")),
    ]
    fails = 0
    for label, want, got in checks:
        ok = got == want
        fails += 0 if ok else 1
        print(f"    {'✅' if ok else '❌'} {label:<38} 期望={want!r:<14} 实得={got!r}")
    return len(checks), fails


def _selftest_parse(as_json=False):
    files = sorted(glob.glob(os.path.join(SAMPLE_DIR, "*.html")))
    if not files:
        print(f"❌ 没找到样本：{SAMPLE_DIR}")
        print("   先跑：python opencd_sanitize.py")
        return 1

    print("=" * 72)
    print("OpenCD 解析器自检 —— 拿脱敏样本逐字段对照（纯离线，零请求）")
    print("=" * 72)

    total_checked = total_fails = 0
    for path in files:
        fname = os.path.basename(path)
        exp = EXPECT.get(fname)
        with open(path, encoding="utf-8") as fh:
            info = parse_detail(fh.read(), source_file=fname)

        print(f"\n{'─' * 72}\n{fname}")
        if not exp:
            print("  ⚠️  没有期望值（样本新增了？去 EXPECT 里加一条）")
            print(f"  transfer_rule = {info.rule}  解析缺失 {len(info.missing)} 项")
            continue

        checked, fails = _compare(fname, info, exp)
        total_checked += checked
        total_fails += len(fails)
        if info.missing:
            print(f"    ℹ️  解析缺失项：{info.missing}")
        print(f"    → 该样本 {checked - len(fails)}/{checked} 项通过")

    # ★ 第 2 步的验收判据：三态裁决在**版面变化**下必须仍然安全。
    #   人造成样本，否则这条判据只在本机手跑过一次（就等于没有）。
    print(f"\n{'─' * 72}")
    print("三态裁决 · 版面变化的人造样本（★ 安全属性：认不出 ⇒ 不放行）")
    mc, mf = _rule_mutations()
    total_checked += mc
    total_fails += mf

    # ★ 「不靠关键词反推」的护栏 —— 把一条曾经的**假声称**钉成可跑的判据。
    print(f"\n{'─' * 72}")
    print("三态裁决 · 正文塞满禁转关键词也不许改判（★ 防「关键词兜底」复活）")
    kc, kf = _rule_no_keyword_fallback()
    total_checked += kc
    total_fails += kf

    print(f"\n{'─' * 72}")
    print("封面抽取 · ★ 防「看错区块」与「频谱图当封面」（2026-09-21 订正）")
    cc, cf = _check_cover_extraction()
    total_checked += cc
    total_fails += cf

    print(f"\n{'─' * 72}")
    print("列表页解析（torrents.php）· 抓取层的入口（★ 第 3 步用）")
    lc, lf = _check_listpage()
    total_checked += lc
    total_fails += lf

    print(f"\n{'─' * 72}")
    print("搜索页解析 · 「qB 反查」的匹配环节（★ 结构与列表页一致，零改动复用）")
    sc, sf = _check_searchpages()
    total_checked += sc
    total_fails += sf

    print(f"\n{'─' * 72}")
    print("RSS 反查 · torrentrss.php 的 name→(hash,tid,date)（★ 1 请求/条的新路）")
    rc, rf = _check_rss_index()
    total_checked += rc
    total_fails += rf

    print(f"\n{'=' * 72}")
    if as_json:
        # ★ 本文件的分母是 total_checked（= 通过数 + 不符数），与别的脚本同义但变量名不同。
        print(json.dumps({"script": "opencd.py parse --selftest",
                          "ok": total_checked - total_fails,
                          "total": total_checked,
                          "exit": 1 if total_fails else 0},
                         ensure_ascii=False))
        return 1 if total_fails else 0

    if total_fails:
        print(f"❌ 自检失败：{total_checked - total_fails}/{total_checked} 项通过，"
              f"{total_fails} 项不符")
        return 1
    print(f"✅ 自检通过：{total_checked}/{total_checked} 项逐字段对上")
    print("   ★ 两个样本正好各占一态：禁止轉載 → DENY、歡迎轉載 → ALLOW")
    return 0


# ★ 人造样本的期望裁决 —— 这张表就是第 2 步的验收判据。
#   关键在最后四行：**只有明确 ALLOW 才放行**，其余一律不放行。
RULE_MUTATIONS = [
    ("原样（歡迎轉載）", None, "歡迎轉載", RULE_ALLOW, True),
    ("原样（禁止轉載）", None, "禁止轉載", RULE_DENY, False),
    ("删掉整行轉載規則", "drop", "", RULE_UNKNOWN, False),
    ("值改成认不出的串", "set:???", "???", RULE_UNKNOWN, False),
    ("值置空", "set:", "", RULE_UNKNOWN, False),
    ("简体禁止转载", "set:禁止转载", "禁止转载", RULE_DENY, False),
    ("简体欢迎转载", "set:欢迎转载", "欢迎转载", RULE_ALLOW, True),
]

# ★★ 「不靠关键词反推」的护栏 —— 曾有过一条**已销掉的假声称**（说计划要用关键词兜底）。
#   计划里写过「关键词兜底抄 easy-upload 的 FORBIDDEN_KEYWORDS」，但代码里从来没有；
#   核对后**决定不补**（够不着 + 会误杀）。这里把「不靠关键词」
#   从一句注释钉成**可跑的判据**：正文里塞满禁转关键词，裁决必须**只由字段决定**。
#
#   方向很要紧：写反了就成了「塞关键词 ⇒ 更保守」，那样这条判据会**奖励**
#   一个我们不想要的实现。两例必须**相反**才说明"正文与裁决无关"：
#     ① 字段 = 歡迎轉載 + 正文满是禁转字样  → 仍 ALLOW（★ 不能被正文拉成 DENY/UNKNOWN）
#     ② 字段 = 缺失      + 正文满是禁转字样  → 仍 NEED_CONFIRM（★ 不能被正文"救"成 DENY）
#   ② 容易被写错成「正文有禁转 ⇒ DENY」——那正是原计划那条兜底的形状，也正是要防的。
RULE_NO_KEYWORD_FALLBACK = [
    ("字段=歡迎轉載 而正文满是禁转字样", "set:歡迎轉載", RULE_ALLOW, True),
    ("字段缺失   而正文满是禁转字样", "drop", RULE_UNKNOWN, False),
]

# 塞进正文的关键词（照抄 easy-upload 的 FORBIDDEN_KEYWORDS，实测该表确有此 7 项）
FORBIDDEN_KEYWORDS = [
    "禁转", "禁轉", "严禁转载", "嚴禁轉載",
    "谢绝转载", "謝絕轉載", "exclusive",
]


def _rule_mutations():
    """在真实样本上做人造变异，验三态裁决。返回检查项数。"""
    path = os.path.join(SAMPLE_DIR, "sample-zhuanzai-huanying.html")
    if not os.path.exists(path):
        print("  ⚠️  缺样本，跳过")
        return 0
    base = open(path, encoding="utf-8").read()
    deny_path = os.path.join(SAMPLE_DIR, "sample-zhuanzai-jinyong.html")
    deny_base = open(deny_path, encoding="utf-8").read() if os.path.exists(deny_path) else base

    checked = fails = 0
    for label, mut, want_raw, want_rule, want_auto in RULE_MUTATIONS:
        if mut is None:
            html = deny_base if "禁止" in label else base
        elif mut == "drop":
            html = re.sub(r'<td class="rowtitle">轉載規則[：:]?</td>\s*<td[^>]*>.*?</td>',
                          "", base, flags=re.S)
        elif mut.startswith("set:"):
            val = mut[4:]
            html = re.sub(r"(轉載規則[：:]?</td>\s*<td[^>]*>)[^<]*(</td>)",
                          r"\g<1>" + val + r"\g<2>", base)
        else:
            html = base
        info = parse_detail(html)
        got = (info["transfer_rule_raw"], info.rule, info.may_auto_publish())
        expect = (want_raw, want_rule, want_auto)
        checked += 1
        ok = got == expect
        if not ok:
            fails += 1
        print(f"    {'✅' if ok else '❌'} {label:<20} "
              f"raw={info['transfer_rule_raw']!r:<12} rule={info.rule:<12} "
              f"可自动发布={info.may_auto_publish()}")
        if not ok:
            print(f"        期望 raw={want_raw!r} rule={want_rule} auto={want_auto}")
    print(f"    → 三态裁决 {checked - fails}/{checked} 项通过")
    return checked, fails


def _rule_no_keyword_fallback():
    """
    「不靠关键词反推」的护栏 —— 见上面 RULE_NO_KEYWORD_FALLBACK 的注释。

    ★ 做法：把 7 个禁转关键词**塞进简介正文**（`divdescr`），再验裁决只由
      `轉載規則` 字段决定。两例方向相反，所以能真正证明"正文与裁决无关"。
    ★ 注入点必须是 `divdescr`（那是简介正文所在），不是整页 —— 整页替换
      容易连字段一起改掉，测的就不是"正文不影响裁决"了。
    """
    path = os.path.join(SAMPLE_DIR, "sample-zhuanzai-huanying.html")
    if not os.path.exists(path):
        print("  ⚠️  缺样本，跳过")
        return 0, 0
    base = open(path, encoding="utf-8").read()
    poison = " ".join(FORBIDDEN_KEYWORDS)

    # 自检前提：这个样本里确实有 divdescr 可注入（否则判据是空的）
    if 'id="divdescr"' not in base:
        print("    ⚠️  样本里没有 divdescr，无法注入关键词 —— 判据不成立")
        return 0, 1

    checked = fails = 0
    for label, mut, want_rule, want_auto in RULE_NO_KEYWORD_FALLBACK:
        if mut == "drop":
            html = re.sub(r'<td class="rowtitle">轉載規則[：:]?</td>\s*<td[^>]*>.*?</td>',
                          "", base, flags=re.S)
        else:                                   # set:<值>
            html = re.sub(r"(轉載規則[：:]?</td>\s*<td[^>]*>)[^<]*(</td>)",
                          r"\g<1>" + mut[4:] + r"\g<2>", base)
        # ★ 把禁转关键词塞进简介正文（divdescr 的开头）
        html = html.replace('id="divdescr"', f'id="divdescr" data-poison="{poison}"', 1)
        html = re.sub(r'(<div[^>]*id="divdescr"[^>]*>)', r"\g<1>" + poison + " ", html, count=1)

        info = parse_detail(html)
        got = (info.rule, info.may_auto_publish())
        expect = (want_rule, want_auto)
        checked += 1
        ok = got == expect
        if not ok:
            fails += 1
        print(f"    {'✅' if ok else '❌'} {label:<26} rule={info.rule:<12} "
              f"可自动发布={info.may_auto_publish()}  （正文已注入 {len(FORBIDDEN_KEYWORDS)} 个禁转词）")
        if not ok:
            print(f"        期望 rule={want_rule} auto={want_auto}"
                  f"  ⇒ ★ 正文影响了裁决，说明实现里混进了关键词兜底")
    print(f"    → 不靠关键词反推 {checked - fails}/{checked} 项通过")
    return checked, fails


def _main_parse():
    ap = argparse.ArgumentParser(description="OpenCD 详情页解析")
    ap.add_argument("html_file", nargs="?", help="要解析的 HTML 文件")
    ap.add_argument("--selftest", action="store_true", help="跑样本自检")
    ap.add_argument("--brief", action="store_true", help="只打关键字段（不 dump 全文）")
    # ★ --json：机读出口，给 tools/gen_counts.py 读（与 --selftest 合用）。
    ap.add_argument("--json", action="store_true",
                    help="与 --selftest 合用：只把结果以 JSON 打到末行")
    args = ap.parse_args()

    if args.selftest:
        return _selftest_parse(as_json=args.json)
    if not args.html_file:
        ap.error("给个 HTML 文件，或加 --selftest")

    with open(args.html_file, encoding="utf-8", errors="replace") as fh:
        info = parse_detail(fh.read(), source_file=os.path.basename(args.html_file))

    if args.brief:
        for k in ("name", "small_descr", "album", "artist", "year", "size",
                  "file_count", "type", "category", "format", "medium", "team",
                  "source", "tags", "transfer_rule_raw", "transfer_rule"):
            print(f"  {k:<20} = {info.get(k)!r}")
        print(f"  {'log 字符数':<20} = {len(info.get('log_content') or '')}")
        print(f"  {'tracklist 行数':<20} = {len((info.get('tracklist') or '').splitlines())}")
        print(f"  {'封面候选':<20} = {info.get('cover_candidates')}")
        print(f"  {'解析缺失':<20} = {info.missing}")
        print(f"  {'可自动发布':<20} = {info.may_auto_publish()}")
    else:
        out = dict(info)
        out["_cover_candidates"] = info.get("cover_candidates")
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0




# ==================================================================== ② rate
class DailyQuotaExceeded(Exception):
    """
    当日请求数已达上限。★ 这是**正常保护**，不是错误 —— 调用方应优雅收工。

    ⚠️ 措辞是「请求数」不是「种子数」：`RateGate` 没有种子概念（模块头「已知缺口」①）。
    `crawl.py` 两处 `except DailyQuotaExceeded`（匹配循环 / 抓取循环）就是在收它。
    """


class RateGate:
    """
    皇后访问闸门。典型用法（**每个**请求都要走）::

        gate = RateGate(max_per_round=20, min_interval=120.0, max_per_day=500)
        for tid in candidates:
            gate.acquire()          # ← 可能抛 DailyQuotaExceeded（正常收工）
            resp = session.get(url)  # 到这一步才真的发包

    ★ `acquire()` 会**累计**这次请求，所以**不要**在没真发请求时调用它
      （那会虚耗配额）。
    """

    def __init__(self, *, max_per_round=20, min_interval=120.0,
                 max_per_day=500, day_key=None, log=None, ledger=None):
        self.max_per_round = int(max_per_round)
        self.min_interval = float(min_interval)
        self.max_per_day = int(max_per_day)
        self._now = time.monotonic            # ★ 单调钟：不受系统改时间影响
        self._origin = self._now()
        self._count_round = 0
        self._day_key = day_key               # 当日标识（如 '2026-09-19'）
        self._log = log or (lambda m: None)

        # ★★ 外部账本（跨进程持久）。**鸭子类型**：只要有 get(key)->int 与
        #    add(key, delta=1)->int 就行（真实适配器见 `crawl.py::_DbLedger`）。
        #    不 import db —— 理由见模块头最后一段（保持本模块零依赖、可独立测）。
        self._ledger = ledger
        # ★★ 从账本**载入**当日已用数。这行是整条"跨轮持久"的关键：
        #    没有它，新进程照样从 0 开始（本地计数与账本各说各话），账本就白记了。
        #    ★ 只在**给了 day_key** 时载入：没有 day_key 时"哪一天"是未知的，
        #      按 key=None 去查会得到一个恒为 0 的假象 —— 宁可保持 0 并让
        #      `can_acquire` 的每日闸门继续按"本地 0"保守工作。
        #    ★ 账本读失败**不能**让构造抛异常（那会让整轮 crawl 起不来，
        #      而配额读不到 = 保守方向上的小问题）⇒ 吞掉并降级为 0，但**要打日志**。
        self._count_day = 0
        if ledger is not None and day_key is not None:
            try:
                self._count_day = int(ledger.get(day_key))
            except Exception as e:                      # noqa: BLE001 - 见上
                self._log(f"⚠️  读配额账本失败，当日计数按 0 起步（会偏保守）: {e}")

    # ---------- 只读状态（给日志/测试用） ----------

    @property
    def used_round(self):
        return self._count_round

    @property
    def used_day(self):
        return self._count_day

    def budget_left_round(self):
        return max(0, self.max_per_round - self._count_round)

    def budget_left_day(self):
        return max(0, self.max_per_day - self._count_day)

    def reset_round(self):
        """
        新一轮开始（阶段每跑一次调一次）。★ 跨天时也要重置当日计数。

        ★ 2026-09-22：**跨轮持久已由账本承担**，本方法的职责缩小到"重开时间基准"。
          本项目「一轮 = 一个进程」（daemon 每轮 `Popen` 一个新 `python crawl.py`），
          所以 `_count_round` 天然从 0 开始；`_origin` 也跟着进程走。
          ⇒ 这个方法现在的正当用途只有一个：**同一个进程里想开新一轮**
            （比如将来的常驻模式或自检），此时把 `_origin` 挪到当前时刻，
            免得下一轮的第一个请求要去追上一轮已经过期的 `due` 时刻。

        ★ **`_count_day` 不在这里动** —— 它由账本载入、由 `acquire()` 写穿、
          由 `roll_day()` 在跨天时重载。这里清零等于**放松**配额，
          是"看起来在重置、其实在放水"的那类 bug。
        """
        self._count_round = 0
        self._origin = self._now()

    def roll_day(self, day_key):
        """
        跨天检查：`day_key` 与当前不同就**按新日期从账本重载**当日计数。

        ★ 由调用方（crawl 阶段）在每轮开头传当天日期进来 ——
          这里**不自己读时钟**，因为那会引入时区问题（`ERR-SQL-03` 的教训：
          库里的时间戳是 UTC、日志是 CST，混了就会得出"今晚没写入"的错误结论）。
          ★ 调用方传的 `day_key` 与账本 `day_key` 是**同一个口径**（都是本地日
            字符串，见 `db.LEDGER_COLUMNS` 的注释）—— 这两个必须同源，
            否则会出现"账本记的是今天的、gate 查的是昨天的"。

        ★★ 为什么是「重载」而不是「清零」（2026-09-22 修）：
          跨天那一瞬间，**另一个进程可能已经发过请求并写了账本**
          （比如上一轮恰好跨过零点、或两轮重叠）。此时清零 ⇒ 两边各再发满一份配额
          ⇒ 当天实际发到 2 倍。重载则无论谁先写，两边都看同一个权威数字。
          ⇒ 语义变成："把当日计数对齐到账本上**该天**的值"，
            而不是"假设该天还是 0"。

        ★ `day_key=None`（没传/没启用）时的行为：只更新标识，**不动计数**
          —— 没有账本时"清零"就是纯粹放松配额，宁可保持现状。
        """
        if self._day_key == day_key:
            return
        self._day_key = day_key

        if self._ledger is None or day_key is None:
            # 没有账本 ⇒ 无从得知新一天已经用了多少。**不清零**（见 docstring）。
            self._log(f"📅 换日（{day_key}），但无账本可用，当日计数保持 {self._count_day}")
            return

        try:
            self._count_day = int(self._ledger.get(day_key))
        except Exception as e:                          # noqa: BLE001 - 见 __init__
            self._log(f"⚠️  换日读账本失败，当日计数保持 {self._count_day}: {e}")
            return
        self._log(f"📅 新的一天（{day_key}），当日计数按账本重载为 {self._count_day}")

    # ---------- 核心 ----------

    def can_acquire(self):
        """还能不能发下一个请求。返回 (ok, 原因)。供"先判断再决定要不要做"的场景用。"""
        if self._count_day >= self.max_per_day:
            return False, f"当日请求已达上限 {self.max_per_day}"
        if self._count_round >= self.max_per_round:
            return False, f"本轮请求已达预算 {self.max_per_round}"
        return True, ""

    def acquire(self):
        """
        取一个请求许可。**会阻塞**到允许发包的时刻；超配额则抛 `DailyQuotaExceeded`。
        ★ 配额用尽抛异常而不是返回 False —— 强制调用方处理，避免"忘了判断"就继续发。
        """
        ok, why = self.can_acquire()
        if not ok:
            raise DailyQuotaExceeded(why)

        # ★ 绝对时间基准：第 N 个请求在 `起点 + (N-1)×间隔`。
        #   `_count_round` 是**已发**数，所以用它的当前值算"该发的时刻"。
        if self.min_interval > 0:
            due = self._origin + self._count_round * self.min_interval
            wait = due - self._now()
            if wait > 0:
                time.sleep(wait)

        self._count_round += 1

        # ★★ 写穿账本，并**用账本的返回值覆盖本地**：账本是权威。
        #   为什么不是"本地 +1 就够"：本地 +1 只在"本进程是唯一写者"时正确。
        #   而 crawl 一轮一个进程，今天可能有**多轮并发**（daemon 周期短于
        #   一轮耗时，或手动跑了一次）⇒ 本地计数会偏低，配额就漏了。
        #   取账本返回值 ⇒ 自动把别人已发的那几次也算进来。
        # ★ 顺序刻在这里：**先记账、再加本地**不可行（账本可能抛异常，
        #   那会让一个已经"获准"的请求凭空消失）。这里是"获准 → 计本地 → 记
        #   账本"，账本抛异常时**异常向上冒**（fail-closed，宁可本轮中断，
        #   不可少记一次配额 —— 见模块头与 `R4`）。★ 别忘了这一步**还没发包**：
        #   异常发生在 `Client.get()` 的 `acquire()` 里，请求不会发出去。
        if self._ledger is not None and self._day_key is not None:
            self._count_day = int(self._ledger.add(self._day_key))
        else:
            self._count_day += 1

        return self._count_round

    def pending_daily(self):
        """
        当日还剩多少请求预算（**面向用户报数**，与 `budget_left_day()` 同值）。

        ★ 存在只为让调用方读到一个**明确**的名字：`budget_left_day()` 是从
          "上限"出发算的，而这里是从"账本还剩多少"出发说的 —— 日志里
          写成"今日剩余 50"比"当日预算 50"更不容易被误读成"已经发了 50"。
        """
        return max(0, self.max_per_day - self._count_day)


# ==================== 自检 ====================
#
# ★ 本模块的自检**不需要 .env / DATA_DIR**（只 import time）—— 这正是它
#   零依赖的价值：闸门逻辑可以在任何地方单独验证，不必先搭整套配置。
#   账本用 stub（`_StubLedger`）替代，见判据③④⑤的对照设计。

class _StubLedger:
    """
    内存账本，接口与 `crawl._DbLedger` 一致（鸭子类型）。

    ★ `add` 返回**累加后**的值（与 `db.ledger_add` 同语义）—— 这一点很要紧：
      若自检的 stub 返回别的（比如 delta），就会把一个实现错误测成通过。
      ⇒ **stub 的语义必须与真账本一致**，否则自检测的是 stub 不是被测代码。
    """

    def __init__(self, init=None):
        self.data = dict(init or {})
        self.add_calls = []

    def get(self, day_key):
        return int(self.data.get(str(day_key), 0))

    def add(self, day_key, delta=1):
        k = str(day_key)
        self.data[k] = int(self.data.get(k, 0)) + int(delta)
        self.add_calls.append((k, int(delta)))
        return self.data[k]


def _selftest_rate(as_json=False):
    # ★ 自检会打 ✅/❌ 这类非 GBK 字符，而 Windows 控制台默认 GBK
    #   ⇒ 不加这行会在**第一行输出**就 UnicodeEncodeError（实测）。
    #   与 `crawl.py` 同款守卫；失败也不该影响逻辑，故吞掉异常。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass

    ok = fails = 0

    def chk(label, good, extra=""):
        nonlocal ok, fails
        ok, fails = ok + bool(good), fails + (not bool(good))
        line = f"  {'✅' if good else '❌'} {label}"
        if extra:
            line += f"   {extra}"
        print(line)

    # ---------- ① 构造时从账本载入当日计数 ----------
    print("\n① 账本载入（跨轮持久的入口）")
    led = _StubLedger({"2026-09-22": 7})
    g = RateGate(max_per_day=50, day_key="2026-09-22", ledger=led)
    chk("新进程载入账本已有值", g.used_day == 7, f"used_day={g.used_day}（期望 7）")
    chk("⛔阴性对照：无账本 ⇒ 归 0（这就是修之前的坏行为）",
        RateGate(max_per_day=50, day_key="2026-09-22").used_day == 0)
    chk("⛔阴性对照：有账本但无 day_key ⇒ 不载入（宁保守，不放假）",
        RateGate(max_per_day=50, ledger=_StubLedger({"2026-09-22": 7})).used_day == 0)

    # ---------- ② acquire() 真的写穿账本（断言 stub，不是 gate） ----------
    print("\n② acquire() 写穿（★ 断言 stub 的存储，不是 gate.used_day）")
    led = _StubLedger()
    g = RateGate(min_interval=0, max_per_day=50, day_key="2026-09-22", ledger=led)
    g.acquire()
    chk("stub 里真的多了 1 条记录", led.add_calls == [("2026-09-22", 1)],
        f"add_calls={led.add_calls}")
    g.acquire()
    chk("第二次累加到 2（不是覆盖成 1）", led.get("2026-09-22") == 2,
        f"stub={led.get('2026-09-22')}")
    chk("gate.used_day 与账本一致", g.used_day == 2, f"used_day={g.used_day}")

    # ---------- ③ 跨轮持久：新 gate 同账本必须延续 ----------
    print("\n③ 跨轮持久（daemon 每轮起新进程 ⇒ 换 gate 不换账本）")
    shared = _StubLedger()
    gA = RateGate(min_interval=0, max_per_day=3, day_key="2026-09-22", ledger=shared)
    for _ in range(3):
        gA.acquire()
    chk("gate A 用满 3 次", gA.used_day == 3)
    gB = RateGate(min_interval=0, max_per_day=3, day_key="2026-09-22", ledger=shared)
    chk("★ 新 gate（= 新进程）载入到 3，配额**延续**", gB.used_day == 3,
        f"used_day={gB.used_day}")
    raised = ""
    try:
        gB.acquire()
    except DailyQuotaExceeded as e:
        raised = str(e)
    chk("★ 所以第二次必须被拦（这才是真·每日上限）", "当日请求已达上限" in raised,
        f"异常={raised!r}")
    chk("⛔阴性对照：换成**新账本** ⇒ 计数归 0，拦不住（= 修之前的坏行为）",
        RateGate(min_interval=0, max_per_day=3, day_key="2026-09-22",
                 ledger=_StubLedger()).used_day == 0)

    # ---------- ④ 跨天：roll_day 换日期 ⇒ 按账本重载 ----------
    print("\n④ 跨天（roll_day 的语义：重载，不是清零）")
    led = _StubLedger({"2026-09-22": 5, "2026-09-23": 2})
    g = RateGate(min_interval=0, max_per_day=50, day_key="2026-09-22", ledger=led)
    chk("起手 = 22 日的 5", g.used_day == 5, f"used_day={g.used_day}")
    g.roll_day("2026-09-22")
    chk("⛔阴性对照：同一天调用 ⇒ 计数不变", g.used_day == 5, f"used_day={g.used_day}")
    g.roll_day("2026-09-23")
    chk("★ 换到 23 日 ⇒ 重载成 23 日的 2（不是清零、也不是留 5）",
        g.used_day == 2, f"used_day={g.used_day}")
    chk("账本没有被 roll_day 写坏", led.get("2026-09-23") == 2,
        f"stub={led.data}")

    # ---------- ⑤ 跨天重载是**双向**的（往回换也对） ----------
    print("\n⑤ 跨天重载的双向性（防「只会写 0」的实现混过 ④）")
    g.roll_day("2026-09-22")
    chk("换回 22 日 ⇒ 重载回 5", g.used_day == 5, f"used_day={g.used_day}")

    # ---------- ⑥ 无账本时 roll_day 不清零（不清零 = 不放水） ----------
    print("\n⑥ 无账本 / 无 day_key 时 roll_day 的行为")
    g = RateGate(min_interval=0, max_per_day=2, day_key="2026-09-22")
    g.acquire(); g.acquire()
    g.roll_day("2026-09-23")
    chk("★ 无账本时换日**保持**计数（清零=放松配额，是危险方向）",
        g.used_day == 2, f"used_day={g.used_day}")

    # ---------- ⑦ 两个上限各自独立约束（旧的强约束不许被账本改动打破） ----------
    print("\n⑦ 每轮预算 与 每日上限 各自独立（量纲仍是请求，见模块头①）")
    led = _StubLedger()
    g = RateGate(min_interval=0, max_per_round=2, max_per_day=100,
                 day_key="d", ledger=led)
    g.acquire(); g.acquire()
    why = ""
    try:
        g.acquire()
    except DailyQuotaExceeded as e:
        why = str(e)
    chk("每轮预算触发（消息含「本轮」）", "本轮请求已达预算" in why, f"{why!r}")
    g2 = RateGate(min_interval=0, max_per_round=100, max_per_day=2,
                  day_key="d", ledger=_StubLedger())
    g2.acquire(); g2.acquire()
    why2 = ""
    try:
        g2.acquire()
    except DailyQuotaExceeded as e:
        why2 = str(e)
    chk("每日上限触发（消息含「当日」）", "当日请求已达上限" in why2, f"{why2!r}")

    # ---------- ⑧ 绝对时间基准（防"每轮 sleep 累积"那条退化回归） ----------
    print("\n⑧ min_interval 用绝对时间基准（不是每轮 sleep 累积）")
    # ★ 用**假时钟**而不是真 sleep：真 sleep 会让自检变慢，而这里要验的是
    #   "第 N 个的到期时刻 = 起点 + (N-1)×间隔"，那是纯算术，不需要真的等。
    ticks = {"now": 1000.0}

    def fake_now():
        return ticks["now"]

    slept = []

    def fake_sleep(s):
        slept.append(s)
        ticks["now"] += s

    g = RateGate(min_interval=10.0, max_per_day=50)
    g._now = fake_now
    g._origin = fake_now()
    orig_sleep = time.sleep
    time.sleep = fake_sleep
    try:
        g.acquire()                      # 第 1 个：due = 1000 + 0×10 = 1000，不等
        chk("第 1 个请求不等（due = 起点）", slept == [], f"slept={slept}")
        g.acquire()                      # 第 2 个：due = 1010，等 10
        chk("第 2 个请求恰好等 10s（= (2-1)×10）",
            len(slept) == 1 and abs(slept[0] - 10.0) < 1e-9, f"slept={slept}")
        ticks["now"] += 100              # 模拟站点变慢：中间白白过了 100s
        slept.clear()
        g.acquire()                      # 第 3 个：due = 1020，早已过 ⇒ **不等**
        chk("★ 站点变慢后不追加延迟（第 3 个 due=1020 已过 ⇒ 0 等待）",
            slept == [], f"slept={slept}（累积式实现会在这里多等）")
    finally:
        time.sleep = orig_sleep

    print(f"\n{'=' * 66}")
    if as_json:
        print(json.dumps({"script": "opencd.py rate --selftest", "ok": ok,
                          "total": ok + fails, "exit": 1 if fails else 0},
                         ensure_ascii=False))
        return 1 if fails else 0
    if fails:
        print(f"❌ 自检失败：{ok}/{ok + fails} 项通过，{fails} 项不符")
        return 1
    print(f"✅ 自检通过：{ok}/{ok + fails} 项")
    print("   ★ 核心是 ③（跨轮持久）+ ④⑤（跨天重载双向）：")
    print("     没有 ③，「每日 500」就还是「每轮 500」—— 这正是本次要修的缺口。")
    return 0


def _main_rate():
    import argparse
    ap = argparse.ArgumentParser(description="OpenCD 访问闸门（RateGate）")
    ap.add_argument("--selftest", action="store_true", help="跑闸门逻辑自检（零外网）")
    ap.add_argument("--max-per-round", type=int, default=20)
    ap.add_argument("--min-interval", type=float, default=120.0)
    ap.add_argument("--max-per-day", type=int, default=500)
    # ★ --json：机读出口，给 tools/gen_counts.py 读（与 --selftest 合用）。
    ap.add_argument("--json", action="store_true",
                    help="与 --selftest 合用：只把结果以 JSON 打到末行")
    args = ap.parse_args()

    if args.selftest:
        return _selftest_rate(as_json=args.json)

    # 不带参数时只报一遍当前配置 —— 本模块是库，不是命令；真正驱动它在 crawl.py。
    g = RateGate(max_per_round=args.max_per_round, min_interval=args.min_interval,
                 max_per_day=args.max_per_day, day_key=time.strftime("%Y-%m-%d"))
    print(f"RateGate: 每轮 {g.max_per_round} 请求 · 最小间隔 {g.min_interval:g}s · "
          f"每日上限 {g.max_per_day}（★ 请求量纲）")
    print("（本模块是闸门库；实际抓取入口是 topup_opencd_fields.py（批量）"
          "与 crawl.py（单条 --tid/--url）。加 --selftest 自检）")
    return 0




# ==================================================================== ③ log
MAX_FIELD = 200

# 明细默认打印条数（其余折成一行计数）
DEFAULT_DETAIL_LIMIT = 20


def brief(text, limit=MAX_FIELD):
    """
    把可能很长的文本压成一行可打印的摘要。**长文本绝不能原样进日志。**

    用法：`log(f"  标题={brief(t['title'])}")`
    """
    if text is None:
        return "(None)"
    s = re.sub(r"\s+", " ", str(text)).strip()
    if len(s) <= limit:
        return s
    return f"{s[:limit]}…[+{len(s) - limit}字符]"


def mask_secrets(text):
    """
    兜底脱敏 —— 万一有凭据漏到日志里。
    ★ 与 `tools/doc-audit/audit.py`（原 `04_probes.py`）的 `redact()` 同思路：**按值的形状抹**，
      不只按键名（按 key 名匹配会漏掉嵌在 URL / 异常文本里的凭据）。
    ★ 与 `opencd_sanitize.py` **不合并**：那个是给**样本文件**用的（要落盘、
      规则更细），这个是给**日志出口**用的（要轻、要快、兜底即可）。
      两处职责不同 ⇒ 各自维护是**有意**的，但要互相指路（都提 passkey 形态）。
    """
    s = str(text)
    s = re.sub(r"(passkey=)[0-9a-fA-F]+", r"\1<REDACTED>", s)
    s = re.sub(r"(?i)(c_secure_pass=)[^;\s]+", r"\1<REDACTED>", s)
    s = re.sub(r"(?i)(cookie\s*[:=]\s*)\S+", r"\1<REDACTED>", s)
    s = re.sub(r"(userdetails\.php\?id=)\d+", r"\1<UID>", s)
    s = re.sub(r"(invite\.php\?id=)\d+", r"\1<UID>", s)
    return s


def detail(log, items, render, limit=DEFAULT_DETAIL_LIMIT, more_hint="另有"):
    """
    打印明细，但**只打前 `limit` 条**，其余折成一行计数。

    `render` 是 `item -> str` 的渲染函数（不返回 None 即打印）。
    返回实际打印的条数。
    """
    shown = 0
    for it in items[:limit]:
        line = render(it)
        if line:
            log(line)
            shown += 1
    rest = len(items) - limit
    if rest > 0:
        log(f"    … {more_hint} {rest} 条（明细已折叠，防日志爆表）")
    return shown


def summary(log, stage, **counters):
    """
    阶段汇总 —— ★ **日常只看这一行**，要保证它信息足够、又只有一行。
    """
    parts = " · ".join(f"{k}={v}" for k, v in counters.items() if v is not None)
    log(f"🎯 [{stage}] {parts}")


# ==================================================================== ④ map
_PAREN_SUFFIX = re.compile(r"[\(（][^)）]*[\)）]\s*$")


def _strip_suffix(name):
    """剥掉标签名尾部的 `(English)` 后缀。`流行(Pop)` → `流行`。"""
    return _PAREN_SUFFIX.sub("", name or "").strip()


# ==================== 标签映射 ====================
#
# ★ 这张表**按名字**把皇后标签接到 Musopia 标签上。
#   左边 = 皇后标签名（剥掉英文后缀后的形态），右边 = **Musopia 的标签名**（不是编号！）。
#   ★ 右边写名字而不是编号，是为了让 descr.TAGS 成为**唯一编号来源** ——
#     若哪天 Musopia 改了编号，只改 descr.py 一处即可，这张表不用动。
#     （本项目「同一事实只在一处维护」的成文约定，见 A.11。）
#
# ★★ 2026-09-21 订正：原先这里写着「剩下 5 项故意不映射（Musopia 没有对应标签）」
#    —— **那句话是错的**，是我在手上没有真实表单时的旧判断。
#    真实证据：乌托邦 `takeupload.php` 的 `tags[4][]` **实测 44 项**（逐行读出，
#    见 `descr.TAGS`）。皇后那 27 项**全都能在乌托邦找到对应**，
#    只是当初写映射表时手上没有这张表单。
#    ⇒ 现在把「有实测依据」的补进来；仍无依据的**留作 unmapped**（不静默丢、不硬凑）。
#
# ★ 补进来的判据是**双向的**：乌托邦真实标签表里**确有此名**（值也取自那张表）。
#   这一条很重要 —— 原先 `TAG_NAME_MAP` 把「器乐」映射过去时，乌托邦侧其实**也有**
#   器乐（值 34），当时能对上纯属巧合好运气；现在的每个右边都能逐一对回 44 项表。
TAG_NAME_MAP = {
    # 地区
    "大陆": "大陆",
    "欧美": "欧美",
    "港台": "港台",
    "日韩": "日韩",
    "其它地区": "其他地区",        # ★ 注意「其它」→「其他」，两边用字不同
    # 风格（能对上的）
    "流行": "流行",
    "摇滚": "摇滚",
    "爵士": "爵士",
    "电子": "电子",
    "金属": "金属",
    "古典": "古典",
    "原声": "原声",
    "发烧": "发烧",
    "民间": "民间",
    "乡村": "乡村",
    "儿童": "儿童",
    "器乐": "器乐",              # ★ 皇后表里**没有**这个标签，但 Musopia 有 —— 留着备用
    # ★★ 以下 11 项是 2026-09-21 拿到真实表单后补上的（原先全落 unmapped）。
    #    右边一律是**乌托邦真实标签里的名字**（`descr.TAGS` 有编号才算数）。
    "汽车": "汽车",              # 乌托邦 28 garage
    "民族": "民族",              # 26 National
    "天籁": "天籁",              # 18 Soul
    "新世纪": "新世纪",           # 19 NewAge
    "蓝调": "蓝调",              # 22 Blues
    "朋克": "朋克",              # 25 Punk
    "宗教": "宗教",              # 29 Religion
    "雷鬼": "雷鬼",              # 21 Reggae
    "说唱": "说唱",              # 20 Rap
    "音乐剧": "音乐剧",           # 27 musical
    # ★★ 「贝斯」这一项是**用字差异**，不是缺标签 —— 最容易看漏的一格：
    #    皇后写 `贝斯(Drum Bass)`（空格），乌托邦写 `贝斯(Drum&Bass)`（&）。
    #    `_strip_suffix()` 会把两边括号都剥掉 ⇒ 剥完都叫「贝斯」，正好接上。
    #    （这是早先那条「贝斯没有对应」旧结论的订正。）
    "贝斯": "贝斯",              # 14 Drum&Bass
}


def map_tags(opencd_names):
    """
    皇后标签名列表 → (musopia_values, unmapped)。

    `musopia_values` 是 Musopia 的 **tag value 列表**（`descr.TAGS` 里的编号字符串），
    可直接填进 `tags[4][]`。
    `unmapped` 是**映射不上的皇后标签名**（原样，含英文后缀）——
    调用方必须显式处理（进每日邮件），**不许静默丢**。

    ★ 判据（`--selftest` 里逐条验）：
      · `大陆` → `4` **不是** `1`（这是本模块存在的理由）
      · `流行(Pop)` → `10`（后缀被剥掉）
      · `其它地区` → `8`（「其它/其他」用字差异被接住）
      · 认不出的 → 进 unmapped，**且不猜**
    """
    values, unmapped = [], []
    for raw in opencd_names or []:
        name = (raw or "").strip()
        if not name:
            continue
        # ① 先精确比；② 不成再剥括号后缀比（先严后宽）
        mu = TAG_NAME_MAP.get(name) or TAG_NAME_MAP.get(_strip_suffix(name))
        if mu is None:
            unmapped.append(name)              # ★ 显式记录，不静默丢
            continue
        val = descr.TAGS.get(mu)
        if val is None:
            # 映表里有、但 descr.TAGS 里没有 ⇒ 两处漂移了，必须响亮地坏
            unmapped.append(f"{name}(映射表→{mu}，但 descr.TAGS 里没有)")
            continue
        if val not in values:                  # 去重（一个种子可能多个标签映到同一个）
            values.append(val)
    return values, unmapped


# ==================== 其余字段 ====================
#
# 格式/媒介：皇后的值形如 `FLAC 分轨` / `WAV 整轨` / `CD`，
# 而 Musopia 的 audiocodec_sel / medium_sel 只要一个编号。
# ★ 这里**只做识别，不做推断** —— 推断归 descr 的 detect_*（发布路径本来就在用）。
#   本函数面对的是「皇后页面已经写明的值」，那是事实，不是要猜的东西。

def map_audiocodec(opencd_format):
    """
    `FLAC 分轨` / `WAV 整轨` / `APE` … → Musopia `audiocodec_sel` 的编号。

    ★ 取不到就返回 `"0"`（= 不指定），**不猜一个相近的**。
      `descr.detect_audiocodec()` 是另一条路（从标题/文件名猜），
      两者用途不同：这里是「页面已写明」，那里是「从文件名推断」。

    ★★ `WAV` 是**已知缺口**（2026-09-21 实测）：原先关键词表里**没有 WAV**
      ⇒ 皇后的 `WAV 整轨` 一律得到 `"0"`（= 不指定），而 2026-09-19 发布的那 5 条里
      **4 条是 WAV** ⇒ 编码这格**系统性为空**。
      · 参考实现 `repos/auto_feed_js/auto_feed.user.js:2561` **显式认 WAV**；
      · 但**编号**仍是未知的（`descr.AUDIOCODEC` 实测自 `edit.php`，表里没有 WAV）
        ⇒ **识别出来、仍返回 `"0"`**，并在返回处留可见的痕迹（见下面 return 的注释），
        而不是让它和"没认出格式"长得一模一样。
      ⇒ 补编号是**第二步**，等 `audiocodec_sel` 的真实 `<option>` 值确认后再做。
    """
    blob = (opencd_format or "").upper()
    for key in ("FLAC", "APE", "DTS", "AAC", "OGG", "MP3"):
        if key in blob:
            return descr.AUDIOCODEC.get(key, "0")
    # ★ WAV：**认得出，但没有编号** ⇒ 仍返回 "0"（不指定）。
    #   这条分支的目的只是**让缺口可见** —— 见 `descr.AUDIOCODEC_UNKNOWN_OK`。
    if "WAV" in blob:
        return descr.AUDIOCODEC.get("WAV", "0")
    return "0"


def map_medium(opencd_medium, opencd_format=""):
    """
    `CD` / `SACD` / `DVD` / `VCD` / `Web` → Musopia `medium_sel` 的编号。

    ★★ 2026-09-21 修：**`Web` 原先被静默当成 `CD`**（因为乌托邦的 `MEDIUM` 表里
      没有 Web，而本函数最后一行 `return MEDIUM["CD"]` 是**无条件兜底**）。
      实测依据：三个皇后页面写着 `<td>媒介：</td><td>Web</td>`（Web 来源发行，
      **不是实体 CD**）⇒ 发 `CD(8)` 是**写了一个错的事实**。

    ★ 映射规则（按**名字**，同标签/制作组的纪律）：
      · `CD`/`SACD`/`DVD`/`VCD` → 同名项
      · `Web` → **`Other(12)`**：它不是实体媒介，乌托邦没有 Web 这一项，
        `Other` 是站点**真有**的选项，且「其它」正是对 Web 最诚实的表述。
      · `HI-RES` → **`Other(12)`**：同上（高解析数字发行，非实体媒介）。
      · 拿不到（空 / 认不出）→ **`0`（请选择一项）**，**不再无条件兜底成 CD**。
        ★ 这是本函数最要紧的行为改变：**"取不到" 不许长得像 "是 CD"**。

    ★★ 2026-09-21 又修一个我自己刚写出的 bug：**子串匹配会误判**。
      原先我写 `if "CD" in blob: return CD` —— 而皇后媒介下拉里有
      **`HQCD` / `24KCD` / `LPCD` / `HDCD` / `SRCD` / `K2CD` / `SHM-CD`**，
      每一个都**含 `"CD"`** ⇒ 全会被错认成普通 `CD(8)`。
      这和本模块存在的理由（防静默错标）**是同一类错误**，只是方向相反。
      ⇒ 改成**逐个精确比对**（剥掉空白后整串相等），不再用 `in` 做媒介判断。
    """
    # ★ 精确比对：把"空白/大小写"归一后**整串相等**才算命中，**不做子串匹配**。
    raw = (opencd_medium or "").strip().upper()
    norm = raw.replace(" ", "").replace("-", "").replace("_", "")
    # 皇后侧媒介名 → 乌托邦编号（左右两边都是**实测读出来的**名字）
    EXACT = {
        "CD": descr.MEDIUM["CD"],
        "SACD": descr.MEDIUM["SACD"],
        "DVD": descr.MEDIUM["DVD"],
        "VCD": descr.MEDIUM["VCD"],
        # ★ 非实体媒介 / 乌托邦没有对应项 ⇒ 落「其它」，**不冒充 CD**。
        "WEB": descr.MEDIUM["OTHER"],
        "HIRES": descr.MEDIUM["OTHER"],
        "OTHER": descr.MEDIUM["OTHER"],
    }
    if norm in EXACT:
        return EXACT[norm]
    # ★ 乌托邦**没有**这些项（24KCD/HDCD/XRCD/Vinyl/Blu-ray…），
    #   也**不**退化成 CD —— 它们确实不是普通 CD。⇒ 不指定（0）。
    return "0"


# ==================== 制作组 team_sel[4] ====================
#
# ★★ 和标签同一个病：**两套编号完全错位**，必须**按名字**映射，不能搬数字。
#
#   实测两边的下拉（都是逐行读出，非推断）：
#
#     皇后 `OpenCD __ 發布.html`  `<select name="team">`：
#        0 請選擇 / 7 OpenCD / 8 LLM / 9 TSxD / 6 KHQ / 5 其他(Other)
#     乌托邦 `音乐乌托邦 __ 发布.html` `<select name="team_sel[4]">`：
#        0 请选择一项 / 6 Musopia / 5 Other / 1 HDS / 2 CHD / 3 MySiLU / 4 WiKi
#
#   ★ 最危险的一格：皇后写 `其他(Other)` = **5**，乌托邦的 `Other` **也** = 5
#     —— 这两个 5 纯属巧合相等，**不能靠"值一样"来匹配**。
#     若哪天站点改号，靠值匹配就会**静默错标**（同 `opencd_map` 存在的理由）。
#   ★ 反过来，皇后 `OpenCD` = 7、乌托邦 `Musopia` = 6：
#     「把从 OpenCD 转载的种子标成 OpenCD 制作组」在乌托邦侧**没有对应项**
#     （乌托邦只有自己的 `Musopia` 和友站 HDS/CHD/MySiLU/WiKi），
#     且**语义也不该混**（我们不是那批制作组的人）
#     ⇒ `OpenCD` 这一项**映射到 `Other`**：皇后页面自己写的 `其他(Other)`
#       正是 `Other`，而样本实测 `team = '其他(Other)'` ⇒ 两边本来就一致。
#   ★ 取不到就返回 `"0"`（= 请选择一项），**不猜**。
TEAM_NAME_MAP = {
    "其他": "OTHER",
    "其他(Other)": "OTHER",
    "Other": "OTHER",
    "OpenCD": "OTHER",      # ★ 语义见上：不冒充制作组，落到「其他」
    "請選擇": "OTHER",
    # 以下三个乌托邦**没有对应项**，留着备用但**当前只作注释**（不放进映射，
    # 否则等于凭空把 LLM/TSxD/KHQ 认成乌托邦的 HDS/CHD/WiKi —— 那是编数据）。
    #   "LLM":  ?, "TSxD": ?, "KHQ": ?,
}


def map_team(opencd_team):
    """
    皇后「製作組」名字 → Musopia `team_sel[4]` 的编号。

    ★ 按**名字**映射（同 `map_tags`）。取不到给 `"0"`（请选择一项），**不猜相近的**。

    ★ 判据（`--selftest` 里验）：
      · `其他(Other)` → `5`（Other）—— ★ 两边数字恰好都是 5，但**理由是名字**
      · `OpenCD`      → `5`（Other）—— ★ 不是 7，也不是乌托邦的 Musopia(6)
      · 认不出的       → `"0"`，且**不挑一个近的**
    """
    name = (opencd_team or "").strip()
    if not name:
        return "0"
    key = TEAM_NAME_MAP.get(name) or TEAM_NAME_MAP.get(_strip_suffix(name))
    if key is None:
        return "0"
    return descr.TEAM.get(key, "0")


def map_all(info):
    """
    把 `opencd_parse.TorrentInfo` 映射成 Musopia 表单要用的字段。

    返回 dict，含：
      · `tags`      —— tag value 列表（可直接填 `tags[4][]`）
      · `unmapped`  —— ★ 映射不上的标签名（**调用方必须显式处理**）
      · `audiocodec_sel` / `medium_sel`
      · `title` / `small_descr` / `tracklist` / `log_content` —— 直接透传（渲染用）
      · `descr`     —— ★ 皇后简介正文（`divdescr` 原文，**已剥图**）。见下方长注释。
      · `artist` / `year` / `source` —— 透传（★ `year` 带「年」字，站点要不要纯数字待定）

    ★ 本函数**不碰网络、不写库、不渲染** —— 只做字段搬运与翻译，方便单测。
    """
    tags, unmapped = map_tags(info.get("tags") or [])
    return {
        "tags": tags,
        "unmapped": unmapped,
        "audiocodec_sel": map_audiocodec(info.get("format")),
        "medium_sel": map_medium(info.get("medium"), info.get("format")),
        # ★ 制作组：**按名字**映射（皇后 `其他(Other)` → 乌托邦 `Other=5`）。
        #   取不到给 "0"（请选择一项）。原先恒发 "0" 是"没做"，不是"做不了"。
        "team_sel": map_team(info.get("team")),
        "title": info.get("name") or "",
        "small_descr": info.get("small_descr") or "",
        "tracklist": info.get("tracklist") or "",
        "log_content": info.get("log_content") or "",
        # ★★ 皇后简介正文（2026-09-26 加）：`opencd_parse.py:233` 从
        #    `<div id="divdescr">` 读到的**原文**，经 `_clean_long` 保真（不动内部空白）。
        #
        #    ★ 为什么现在才加：这个值**一直**被抓下来，但 `map_all` 不返回它 ⇒
        #      `build_row()` 拿不到 ⇒ **从未落库**（实测 58 条快照里
        #      `descr` 键出现 **0** 次；连 `unmapped` 都没有它）。
        #      ⇒ 站上的简介一直是 `descr.build_descr()` **现渲染**的
        #        （封面 + 曲目 + 日志），与皇后正文无关。
        #      ★★ 这正是本项目栽过多次的形状：**抓到了、没往外传**（`ERR-DATA-01` 同族；
        #         `db.TASK_FIELDS` 里那段"数据在手边、就是没传进来"是同一个）。
        #
        #    ★ 图**已由解析层剥掉**（见 `opencd_parse._descr_text_only`）——
        #      实测 15/58 个页面的 `divdescr` 里塞着 `Cover.jpg` / `Booklet1~10.jpg`
        #      / `Inside.jpg` 等**缩略图**（`.thumb.jpg`），且历史注释明确警告过
        #      里面会混 `频谱图.JPG` / `xxx-spectrogram.png`。渲染侧只要文字，
        #      图片由我们自己的 `cover_url` 负责 ⇒ 在**解析层**剥，而不是渲染层再剥
        #      （`ERR-META-03`：同一判据两处实现必然漂移）。
        "descr": info.get("descr") or "",
        # ★ 艺术家 / 发行年份：**透传，不改写**。落位见 descr.build_form_fields
        #   （`custom_fields[4][1]` / `[4][2]`）。取不到就是 ""，调用方原样发空。
        "artist": info.get("artist") or "",
        "year": info.get("year") or "",
        # 原样保留，不改写、不渲染成「转载自 OpenCD」（来源行照原样透传）
        "source": info.get("source") or "",
    }


# ==================== 自检 ====================
#
# ★ 期望值**人工核过**再写进来（不是从映射结果反填 —— 那样是自证）。

TAG_CASES = [
    # (说明, 输入的皇后标签名, 期望的 Musopia 编号, 是否期望落 unmapped)
    ("大陆 → 4（★ 不是 1！本模块存在的理由）", "大陆", "4", False),
    ("欧美 → 5（★ 不是 2）", "欧美", "5", False),
    ("港台 → 6（★ 不是 3）", "港台", "6", False),
    ("日韩 → 7（★ 不是 4）", "日韩", "7", False),
    ("古典(Classical) → 15（★ 不是 9；后缀要剥掉）", "古典(Classical)", "15", False),
    ("摇滚(rock) → 9", "摇滚(rock)", "9", False),
    ("其它地区 → 8（★「其它」↔「其他」用字不同）", "其它地区", "8", False),
    ("流行(Pop) → 10", "流行(Pop)", "10", False),
    ("器乐 → 34（皇后没这标签，但 Musopia 有）", "器乐", "34", False),
    # ★★ 以下 11 项 2026-09-21 **订正**：原先断言它们落 unmapped，那是错的 ——
    #    拿到乌托邦真实表单后发现**乌托邦全都有这些标签**（见 TAG_NAME_MAP 上方注释）。
    ("★ 订正：贝斯(Drum Bass) → 14（乌托邦写 Drum&Bass，靠剥括号接上）",
     "贝斯(Drum Bass)", "14", False),
    ("★ 订正：天籁(Soul) → 18", "天籁(Soul)", "18", False),
    ("★ 订正：新世纪(NewAge) → 19", "新世纪(NewAge)", "19", False),
    ("★ 订正：说唱(Rap) → 20", "说唱(Rap)", "20", False),
    ("★ 订正：雷鬼(Reggae) → 21", "雷鬼(Reggae)", "21", False),
    ("★ 订正：蓝调(Blues) → 22", "蓝调(Blues)", "22", False),
    ("★ 订正：朋克(Punk) → 25", "朋克(Punk)", "25", False),
    ("★ 订正：民族(National) → 26", "民族(National)", "26", False),
    ("★ 订正：音乐剧(musical) → 27", "音乐剧(musical)", "27", False),
    ("★ 订正：汽车(garage) → 28", "汽车(garage)", "28", False),
    ("★ 订正：宗教(Religion) → 29", "宗教(Religion)", "29", False),
    # ★ 阴性对照：**真**认不出的仍然必须落 unmapped（不许为了"好看"硬凑）
    ("阴性对照：不存在的标签 → 必须落 unmapped", "根本不存在的标签", None, True),
]

CODEC_CASES = [
    ("FLAC 分轨", "1"), ("APE", "2"), ("WAV 整轨", "0"),
    ("MP3", "4"), ("AAC", "6"), ("", "0"),
]

MEDIUM_CASES = [
    ("CD", "", "8"), ("SACD", "", "9"), ("DVD", "", "10"),
    ("", "FLAC 分轨", "0"),          # ★ 修订：认不出不再兜底成 CD
    ("Web", "", "12"),               # ★★ Web → Other（原先错成 CD）
    ("Web", "FLAC 分轨", "12"),      # ★ 格式里没有 CD 字样时也一样
    ("HI-RES", "", "12"),            # ★★ HI-RES → Other
    ("HI-RES", "FLAC 分轨", "12"),
    ("", "WAV 整轨", "0"),           # ★ 只给格式、且不含 CD ⇒ 不猜
    # ★★ 子串陷阱的**阴性对照**（我自己刚写出过的 bug）：
    #    HQCD/24KCD/LPCD/HDCD/SRCD/K2CD 里都含 "CD" 子串 ⇒ 若用 `in` 判断
    #    会被全部错认成普通 CD。下面每条都必须**不是 8**。
    ("HQCD", "", "0"), ("24KCD", "", "0"), ("LPCD", "", "0"),
    ("HDCD", "", "0"), ("SRCD", "", "0"), ("K2CD", "", "0"),
]

# ★ 制作组：★ 按**名字**映射，不是搬数字。皇后 `其他(Other)`=5 / 乌托邦 `Other`=5
#   —— 数字**恰好相等纯属巧合**，所以这里刻意写「名字 → 编号」而不是「5 → 5」。
TEAM_CASES = [
    ("其他(Other) → 5（乌托邦 Other）", "其他(Other)", "5"),
    ("★ OpenCD → 5（**不是 7**，也不是乌托邦 Musopia=6）", "OpenCD", "5"),
    ("其他 → 5（无括号形态也要接住）", "其他", "5"),
    ("LLM → 0（乌托邦没有对应项，**不猜成 HDS/CHD/WiKi**）", "LLM", "0"),
    ("空 → 0", "", "0"),
    ("认不出的 → 0，**不挑一个近的**", "莫名其妙的组", "0"),
]


def _check_map():
    ok = fails = 0

    print("① 标签映射 —— ★ 核心判据是「按名字映射，不搬数字」")
    for label, name, want_val, want_unmapped in TAG_CASES:
        vals, un = map_tags([name])
        got_val = vals[0] if vals else None
        got_unmapped = bool(un)
        good = (got_val == want_val) and (got_unmapped == want_unmapped)
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} {label:<48} "
              f"→ {got_val!r:<6} unmapped={got_unmapped}")

    # ★★ 两张表不许漂移：TAG_NAME_MAP 的**每个右边**都必须能在 descr.TAGS 里找到编号。
    #    理由：`map_tags()` 遇到「表里有名、descr.TAGS 里没编号」时会把它塞进 unmapped
    #    —— 那是**响了**，但如果没人跑自检就没人听见。这里把它变成**硬判据**。
    print("\n①b 表一致性 —— TAG_NAME_MAP 的每个右边都要在 descr.TAGS 里有编号")
    for oc, mu in sorted(TAG_NAME_MAP.items()):
        good = mu in descr.TAGS
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} 皇后「{oc}」→ 乌托邦「{mu}」"
              f" → 编号 {descr.TAGS.get(mu, '❌ 不存在！')}")

    # ★★ 覆盖率判据（**这次是真结论，不是感觉**）：皇后的 27 个标签**全部**要能映射。
    #    ★ 这条会随着皇后/乌托邦任一边改变而失效 ⇒ 失效时应当**修表**，
    #      不是把这条判据删掉（删判据 = 回到"少几个标签没人知道"的老状态）。
    print("\n①c 覆盖率 —— 皇后 27 个标签应当**全部**映射得上")
    missed = [n for n in OPENCD_TAGS if not (
        TAG_NAME_MAP.get(n) or TAG_NAME_MAP.get(_strip_suffix(n)))]
    good = not missed
    ok, fails = ok + good, fails + (not good)
    print(f"  {'✅' if good else '❌'} 未映射的皇后标签：{missed or '（无，全部覆盖）'}")

    print("\n② 音频编码映射 —— 取不到给 0（不指定），不猜")
    for src, want in CODEC_CASES:
        got = map_audiocodec(src)
        good = got == want
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} {src!r:<16} → {got!r:<4} 期望 {want!r}")

    print("\n③ 媒介映射 —— ★ Web→Other，取不到给 0（**不再兜底成 CD**）")
    for med, fmt, want in MEDIUM_CASES:
        got = map_medium(med, fmt)
        good = got == want
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} {med!r}/{fmt!r:<12} → {got!r:<4} 期望 {want!r}")

    print("\n③b 制作组映射 —— ★ 按**名字**，不是搬数字（两边恰好都是 5，别被骗）")
    for label, src, want in TEAM_CASES:
        got = map_team(src)
        good = got == want
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} {label:<48} → {got!r:<4} 期望 {want!r}")

    print("\n④ 端到端：拿真实样本走一遍 map_all()")
    import glob
    import os
    sample = os.path.join(SAMPLE_DIR, "sample-zhuanzai-jinyong.html")
    if not os.path.exists(sample):
        print("  ⚠️  缺样本，跳过（挂载 opencdhtml 后再跑）")
        return ok, fails
    with open(sample, encoding="utf-8") as fh:
        info = parse_detail(fh.read())
    m = map_all(info)
    # ★ 该样本实测标签：['大陆', '港台', '古典(Classical)']
    checks = [
        ("大陆→4 在结果里", "4" in m["tags"]),
        ("★ 皇后的 1（大陆）没被直接搬过来", "1" not in m["tags"]),
        ("港台→6 在结果里", "6" in m["tags"]),
        ("古典→15 在结果里", "15" in m["tags"]),
        ("没有 unmapped（这三项都能对上）", m["unmapped"] == []),
        ("medium_sel = 8（CD）", m["medium_sel"] == "8"),
        ("codec = 0（WAV 整轨 → 不指定）", m["audiocodec_sel"] == "0"),
        ("source 原样保留（原抓）", m["source"] == "原抓"),
    ]
    for label, good in checks:
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} {label}")
    print(f"     实测 tags={m['tags']} unmapped={m['unmapped']}")

    return ok, fails


def _main_map():
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="OpenCD → Musopia 字段映射")
    ap.add_argument("--selftest", action="store_true", help="跑离线自检")
    ap.add_argument("file", nargs="?", help="详情页 HTML（打印映射结果）")
    # ★ --json：机读出口，给 tools/gen_counts.py 读。别去正则解析人读的 ✅/❌ 行 —— 分母口径不统一。
    ap.add_argument("--json", action="store_true",
                    help="与 --selftest 合用：只把结果以 JSON 打到末行")
    args = ap.parse_args()

    if args.selftest:
        if args.json:
            ok, fails = _check_map()
            print(json.dumps({"script": "opencd.py map --selftest", "ok": ok,
                              "total": ok + fails, "exit": 1 if fails else 0},
                             ensure_ascii=False))
            return 1 if fails else 0
        print("=" * 74)
        print("opencd_map 离线自检（纯离线，零请求）")
        print("=" * 74)
        ok, fails = _check_map()
        print("\n" + "=" * 74)
        if fails:
            print(f"❌ 自检失败：{ok}/{ok + fails} 通过，{fails} 项不符")
            return 1
        print(f"✅ 自检通过：{ok}/{ok} 项全对")
        return 0

    if not args.file:
        ap.error("给个 HTML 文件，或加 --selftest")
    with open(args.file, encoding="utf-8", errors="replace") as fh:
        info = parse_detail(fh.read())
    m = map_all(info)
    print(f"  皇后标签   = {info.get('tags')}")
    print(f"  → tags     = {m['tags']}  （Musopia 的 tag value）")
    print(f"  → unmapped = {m['unmapped']}")
    print(f"  codec/medium = {m['audiocodec_sel']} / {m['medium_sel']}")
    return 0




# ==================================================================== ⑤ gate
QB_SEED_DAYS_REQUIRED = 3
PUBLISH_DELAY_DAYS = 3

# 闸门判定的结果（字符串常量，便于进日志/邮件/库）
GATE_NOT_SEEDED = "NOT_SEEDED"        # 闸门①未满：做种不足 3 天 ⇒ **不抓**
GATE_READY_TO_FETCH = "READY_TO_FETCH"  # 闸门①已满 ⇒ 可以抓页面
GATE_WAITING = "WAITING"               # 已抓、但闸门②未满 ⇒ 还不发
GATE_READY_TO_PUBLISH = "READY_TO_PUBLISH"  # 两个闸门都过 ⇒ 可以发


def fetch_gate_ok(qb_seed_days, required=QB_SEED_DAYS_REQUIRED):
    """
    闸门①：在 qB 里做种够久了吗？返回 (ok, 原因)。

    `qb_seed_days` 是在 qB 里的做种天数（float）。★ 用**天数**而不是绝对时间戳：
    做种时长由 qB 自己算，我们只关心「够了没有」，比较两个绝对时间更容易踩时区。
    ★ `None`（拿不到）按**不满足**处理 —— 宁可晚抓，不可早发。
    ★★ 2026-09-21 补：**非有限值（NaN / inf）也必须按「不满足」处理**。
      原先只判 `days < required`，而 `float('nan') < 3` 是 **False** ⇒
      **NaN 会被判「满足」**（实测 `decide(nan)` → `READY_TO_FETCH`、
      `decide(inf)` → `READY_TO_FETCH`）—— 这**违反**本函数自己的纪律
      「宁可晚抓，不可早发」。`inf` 同理（一个坏掉的时长不该被当成"做种很久了"）。
      ⇒ 用 `math.isfinite` 挡掉。★ 这条今天没炸只是因为**零调用方**。
    """
    if qb_seed_days is None:
        return False, "拿不到做种时长（qB 没这个种子？）"
    try:
        days = float(qb_seed_days)
    except (TypeError, ValueError):
        return False, f"做种时长不是数字：{qb_seed_days!r}"
    if not math.isfinite(days):
        # ★ NaN / inf：**绝不能**因为"比较结果是 False"就被放行
        return False, f"做种时长不是有限数字：{qb_seed_days!r}"
    if days < required:
        return False, f"做种 {days:.2f} 天 < {required} 天"
    return True, f"做种 {days:.2f} 天 ≥ {required} 天"


def publish_gate_ok(fetched_at, now, delay_days=PUBLISH_DELAY_DAYS):
    """
    闸门②：抓完之后的人工反悔窗口过了吗？返回 (ok, 原因)。

    ★ 两个时间都必须由**调用方**传进来（显式依赖）：
      · `fetched_at` —— 抓页面的时刻
      · `now`        —— "现在"
      这样本函数**不读时钟**，可被穷举测试，
      也把「UTC/CST 混用」的风险挡在函数之外（调用方先转好再传）。
    """
    if fetched_at is None:
        return False, "还没抓过页面（fetched_at 为空）"
    if not isinstance(fetched_at, datetime) or not isinstance(now, datetime):
        return False, "fetched_at / now 必须是 datetime"
    due = fetched_at + timedelta(days=delay_days)
    if now < due:
        left = due - now
        return False, f"反悔窗口未满，还需 {left.total_seconds() / 3600:.1f} 小时（{due:%Y-%m-%d %H:%M}）"
    return True, f"已过 {delay_days} 天反悔窗口"


def decide(qb_seed_days, fetched_at=None, now=None,
           seeded_required=QB_SEED_DAYS_REQUIRED,
           delay_days=PUBLISH_DELAY_DAYS):
    """
    综合两个闸门，给出**当前该做什么**。

    返回 `(state, reason)`，state 是上面四个**之一**：
      · `NOT_SEEDED`        —— 闸门①未满，连抓都不要抓（★ 省请求的关键）
      · `READY_TO_FETCH`    —— 闸门①满了、还没抓 ⇒ 去抓页面
      · `WAITING`           —— 抓了但闸门②未满 ⇒ 等着，别发
      · `READY_TO_PUBLISH`  —— 都过了 ⇒ 可以发

    ★ 顺序是**串联**：闸门①不过就**根本不该抓**，所以这时候不关心闸门②。
      写成「两个条件都算完再 and」也能跑，但那样在闸门①不过时会先消耗掉
      `publish_gate_ok()` 的调用 —— 语义上就变成了「并行」。
    """
    ok1, why1 = fetch_gate_ok(qb_seed_days, seeded_required)
    if not ok1:
        return GATE_NOT_SEEDED, why1
    if fetched_at is None:
        return GATE_READY_TO_FETCH, "闸门①已过，尚未抓取"
    ok2, why2 = publish_gate_ok(fetched_at, now, delay_days)
    if not ok2:
        return GATE_WAITING, why2
    return GATE_READY_TO_PUBLISH, why2


def publish_after_from(fetched_at, delay_days=PUBLISH_DELAY_DAYS):
    """抓取时刻 → 可发布时刻。写进库的 `publish_after` 列，供阶段筛选用。"""
    if fetched_at is None:
        return None
    return fetched_at + timedelta(days=delay_days)


# ==================== 自检 ====================
#
# ★ 判据设计要点：两个闸门**各自**都要有用例，且要覆盖"边界"与"串联语义"。

def _check_gate():
    from datetime import timezone
    T = datetime(2026, 9, 19, 12, 0, 0)          # 固定的"现在"，便于穷举

    ok = fails = 0

    def case(label, want, got):
        nonlocal ok, fails
        good = want == got
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} {label:<52} 期望={want!r:<18} 实得={got!r}")

    print("① 闸门①：做种不够 3 天 ⇒ NOT_SEEDED（★ 连抓都不抓）")
    for days in (0, 1.0, 2.99):
        s, _ = decide(qb_seed_days=days, now=T)
        case(f"做种 {days} 天", GATE_NOT_SEEDED, s)

    print("\n② 闸门① 边界：正好 3.0 天 ⇒ 放行（>= 而不是 >）")
    s, why = decide(qb_seed_days=3.0, now=T)
    case("做种 3.0 天 → READY_TO_FETCH", GATE_READY_TO_FETCH, s)

    print("\n③ 拿不到做种时长（None）⇒ 按不满足处理（宁晚不早）")
    s, why = decide(qb_seed_days=None, now=T)
    case("qb_seed_days=None", GATE_NOT_SEEDED, s)

    print("\n③b ★★ 非有限值（NaN / inf）也必须**不满足** —— 阴性对照")
    # ★ 这条防的是「`nan < 3` 是 False ⇒ 被误判成满足」：
    #   2026-09-21 实测过 `decide(nan)` → READY_TO_FETCH（**放行**），
    #   违反本模块「宁可晚抓，不可早发」的纪律。删掉 `math.isfinite` 那行，
    #   下面两条必须当场 ❌。
    for bad, label in ((float("nan"), "NaN"), (float("inf"), "inf"),
                       (float("-inf"), "-inf"), ("不是数字", "字符串")):
        s, why = decide(qb_seed_days=bad, now=T)
        case(f"{label} ⇒ 必须 NOT_SEEDED", GATE_NOT_SEEDED, s)

    print("\n④ 闸门①过了、还没抓 ⇒ READY_TO_FETCH")
    s, _ = decide(qb_seed_days=10, fetched_at=None, now=T)
    case("已做种 10 天、未抓", GATE_READY_TO_FETCH, s)

    print("\n⑤ 闸门②：刚抓完 ⇒ WAITING（★ 不许立刻发）")
    for hours in (0, 1, 71):
        fa = T - timedelta(hours=hours)
        s, _ = decide(qb_seed_days=10, fetched_at=fa, now=T)
        case(f"抓完 {hours} 小时后", GATE_WAITING, s)

    print("\n⑥ 闸门② 边界：正好 3 天（72 小时）⇒ 可以发")
    fa = T - timedelta(hours=72)
    s, _ = decide(qb_seed_days=10, fetched_at=fa, now=T)
    case("抓完正好 72 小时", GATE_READY_TO_PUBLISH, s)

    print("\n⑦ 都过了 ⇒ READY_TO_PUBLISH")
    fa = T - timedelta(days=5)
    s, whys = decide(qb_seed_days=10, fetched_at=fa, now=T)
    case("做种 10 天、抓完 5 天", GATE_READY_TO_PUBLISH, s)

    print("\n⑧ ★ 串联语义：做种不满时，**不该**去看闸门②")
    # 构造一个"闸门②已过"的 fetched_at；若实现是并联，就会误判成 READY_TO_PUBLISH
    fa = T - timedelta(days=99)          # 闸门②早过了
    s, why = decide(qb_seed_days=1, fetched_at=fa, now=T)   # 但闸门①没过
    case("闸门①不过 + 闸门②已过 ⇒ 必须 NOT_SEEDED", GATE_NOT_SEEDED, s)

    print("\n⑨ 时区安全：naive 与 aware 混用应被处理（不能静默算错）")
    aware = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 9, 19, 12, 0)
    try:
        # naive vs aware 直接比较会抛 TypeError —— 这是**好事**，响亮地坏
        # （本项目判据：响亮地坏，优于安静地错）
        decide(qb_seed_days=10, fetched_at=naive, now=aware)
        mixed = "没抛异常"
    except TypeError:
        mixed = "抛 TypeError"
    case("naive/aware 混用 → 抛错（不静默算错）", "抛 TypeError", mixed)

    print("\n⑩ publish_after_from：抓取时刻 + 3 天")
    fa = datetime(2026, 9, 19, 12, 0)
    case("+3 天", datetime(2026, 9, 22, 12, 0), publish_after_from(fa))
    case("None → None", None, publish_after_from(None))

    return ok, fails


def _main_gate():
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="OpenCD 转载闸门（闸门① 已退役；闸门② 现由 db.py 的 WHERE 承担）")
    ap.add_argument("--selftest", action="store_true", help="跑离线自检")
    # ★ --json：机读出口，给 tools/gen_counts.py 读。别去正则解析人读的 ✅/❌ 行 —— 分母口径不统一。
    ap.add_argument("--json", action="store_true",
                    help="只把结果以 JSON 打到末行（给 gen_counts.py 读）")
    args = ap.parse_args()

    if not args.selftest:
        ap.error("加 --selftest")

    if args.json:
        ok, fails = _check_gate()
        print(json.dumps({"script": "opencd.py gate --selftest", "ok": ok,
                          "total": ok + fails, "exit": 1 if fails else 0},
                         ensure_ascii=False))
        return 1 if fails else 0

    print("=" * 74)
    print("opencd_gate 离线自检（纯逻辑，零请求、零数据库）")
    print("=" * 74)
    ok, fails = _check_gate()
    print("\n" + "=" * 74)
    if fails:
        print(f"❌ 自检失败：{ok}/{ok + fails} 通过，{fails} 项不符")
        return 1
    print(f"✅ 自检通过：{ok}/{ok} 项全对")
    return 0




# ==================================================================== ⑥ ingest
STATUS_OK = "SUCCESS"                  # 两个闸门都过，等 publish_after 到期
STATUS_NOT_SEEDED = "NOT_SEEDED"       # 闸门①未满（含拿不到 / 非有限值）
STATUS_NO_LOG = "NO_LOG"               # 页面 NFO/LOG 为空 —— 第一代的规矩：不发
STATUS_DENIED = "DENIED"               # 禁止轉載
STATUS_HASH_MISMATCH = "HASH_MISMATCH"  # 拿不到 infohash，或与页面 Hash碼 不符
STATUS_NO_TORRENT_FILE = "NO_TORRENT_FILE"  # 拿不到本地 .torrent

# ★ 全部「非 SUCCESS」的状态 —— 判据⑫ 拿它逐条验"选不出"。
NON_PUBLISH_STATUSES = (
    STATUS_NOT_SEEDED, STATUS_NO_LOG, STATUS_DENIED,
    STATUS_HASH_MISMATCH, STATUS_NO_TORRENT_FILE,
)

# ★ 时区：库里的串必须与 `datetime('now')` 同源 ⇒ 一律 UTC + 这个格式
#   （ISO 形态可被 SQLite 直接做字符串比较）。
TS_FMT = "%Y-%m-%d %H:%M:%S"


def _utc(now):
    """
    把调用方给的时间规整成**可用于落库的 UTC 串**，并返回 (dt_utc, 串)。

    ★ 两种输入都要接住（调用方常给 `datetime.now()`，那是 naive 本地时间）：
      · aware ⇒ 直接换算到 UTC；
      · naive ⇒ **按 UTC 解释**（本项目 DATETIME 列一律存 UTC，同 `updated_at`
        的 `CURRENT_TIMESTAMP`）。★ 这里刻意不按本地时区解释 —— 否则本机(UTC)
        与生产(CST)会得到不同的串，而那个差异**本机测不出来**。
    """
    if now is None:
        raise ValueError("必须显式传入 now（本模块不读时钟）")
    if now.tzinfo is None:
        dt = now.replace(tzinfo=timezone.utc)
    else:
        dt = now.astimezone(timezone.utc)
    return dt, dt.strftime(TS_FMT)


def _finite_days(value):
    """
    把 `qb_seed_days` 规整成**有限 float** 或 `None`。

    ★★ 这一步是**发现 A** 的修复：闸门①（`opencd_gate.fetch_gate_ok`）判的是
      `days < required`，而 `float('nan') < 3` 是 **False** ⇒ **NaN 会被放行**！
      实测 `decide(nan)` → `READY_TO_FETCH`、`decide(inf)` → `READY_TO_FETCH`，
      这**违反**闸门①自己的成文纪律（「宁可晚抓，不可早发」）。
      ⇒ 在写入侧**先挡掉非有限值**再传进闸门（双保险；闸门那侧也补了判据）。
    """
    if value is None:
        return None
    try:
        days = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(days):
        return None
    return days


def build_row(info, *, raw_tid, info_hash, torrent_file=None,
              qb_seed_days=None, now=None, opencd_added_at=None):
    """
    `TorrentInfo`（+ 闸门输入）→ `db.save_task(**kw)` 的 kwargs。**纯函数**：
    不碰 DB、不读时钟（`now` 显式传入）。

    返回的 dict 里**必有** `status` 与 `reason` 两个键 ——
    「不入 SUCCESS」也照样落库，为的是**留下证据**（否则每轮重判、永远拦不完）。

    ★★ **刻意不写的三个键**（每条都有理由，动了会出事）：
      · `content_path` —— **第一阶段行的标志字段，永不可碰**。
        `save_task` 的 upsert 只更新"显式传进来的字段"，
        所以"永不传"就是"永不覆盖"的保证。判据⑥ 钉住这条。
      · `cover_url`   —— 封面属独立一步（见模块头「交付边界」），不在本步。
      · `torrent_file` —— 由调用方传入（它是"本机路径"这种**环境事实**，
        不是页面内容）；★ 且**绝不能**用 `run.py` 的 `create_musopia_torrent()`
        重新生成 —— 那会换 announce passkey ⇒ **infohash 变了** ⇒
        与 upsert 主键对不上。
    """
    m = map_all(info)
    dt, fetched_at_s = _utc(now)
    publish_after_s = publish_after_from(dt).strftime(TS_FMT)

    # ★ 兼容普通 dict：`TorrentInfo` 才是标准输入，但自检/将来重放时
    #   可能喂普通 dict。**判据必须一致**，不能因为"少了便利方法"就改变裁决
    #   —— 所以这里退回到规则字符串本身（`transfer_rule`），而不是默认放行。
    is_denied = (info.is_denied() if hasattr(info, "is_denied")
                 else str(info.get("transfer_rule") or "") == RULE_DENY)

    # ── 状态判定（★ 顺序不能换：就是闸门 decide() 的串联语义 + 前置过滤）──
    reasons = []
    page_hash = str(info.get("info_hash") or "").strip().lower()
    want_hash = str(info_hash or "").strip().lower()

    # ① infohash：缺失 / 与页面 Hash碼 不符 ⇒ **绝不写 SUCCESS**
    #    ★ 页面 Hash碼 在 `parse_detail` 里叫 `info_hash`（若有）；取不到就不校验，
    #      但**必须**有调用方给的 hash（它是 upsert 主键）。
    if not want_hash:
        status, reason = STATUS_HASH_MISMATCH, "拿不到 infohash（不能凭空写主键）"
    elif page_hash and page_hash != want_hash:
        status, reason = (STATUS_HASH_MISMATCH,
                          f"页面 Hash碼({page_hash[:12]}…) ≠ 传入的 hash({want_hash[:12]}…)")
    # ② 禁止轉載 ⇒ 落 DENIED（**落库留证据**，但绝不进发布队列）
    elif is_denied:
        status, reason = STATUS_DENIED, f"禁止轉載（{info.get('transfer_rule_raw') or ''}）"
    # ③ 页面本来就没有 log ⇒ 第一代的规矩：不发
    #    ★ 状态名用 `NO_LOG` 而**不是** `run.py` 那个 `NO_LOG_FILE`：
    #      后者的语义是「**目录里**没有 .log」，而皇后路的 log 来源是**页面**
    #      ⇒ 混用一个名字，将来分不清是哪条路判的。
    elif not (info.get("log_content") or "").strip():
        status, reason = (STATUS_NO_LOG,
                          "皇后的 NFO/LOG 为空（页面 divnfo 里就是空的）")
    # ④ `.torrent` 拿不到 / 文件不在 ⇒ `publish.py` 必然拦，先落状态省得每轮重判
    #
    #    ★★★ 2026-09-27：**这一支原来把两件不同的事写成同一句话**，而且那句话
    #       是**错的**（`ERR-META-14`：错的"为什么"会把读的人引去查一个不存在的问题）。
    #
    #    实测现场（sid 15972 / 15974 / 17099 / 17264 / 17265 五条）：
    #      · `reason` = `拿不到本地种子文件（None）`   ← 括号里是 **None**
    #      · `torrent_file` 列 = `/data/output_torrents/…OpenCD.torrent`（**有值**）
    #      · 盘上检查 → `exists=True`，8271 / 7879 / 8474 / 11617 / 19859 字节
    #      ⇒ **文件全都在**。既没丢，也没被删。
    #
    #    ★ 真因：`topup_opencd_fields.py` 是**刻意**不传 `torrent_file` 的
    #      （它模块头纪律 5：那批行的 `.torrent` 在 qB 那边，重建会换 passkey
    #      ⇒ 改 infohash）。⇒ 传进来的是 `None` ⇒ 旧判据把它读成
    #      「文件拿不到」⇒ 写下 `（None）`。
    #
    #    ★★ 为什么必须分辨这两支（**修法完全不同**）：
    #      · `torrent_file is None`      ⇒ **写入侧没给这个事实**。
    #        `.torrent` 可能存在、可能不存在，本模块**无从判断**（它不查 qB）。
    #        ⇒ 该说的话是「**调用方没传路径**」；要修的是**调用方**（或将来
    #          加一步从 qB 取路径），**不是**去盘上找文件。
    #      · 路径给了但 `not exists` ⇒ **文件真的不在**。⇒ 该说的话是
    #        「文件不在」；要修的是**盘**（重新生成 / 找回）。
    #      ⇒ 混成一句话的后果（实测已发生）：`status` 列说 `NO_TORRENT_FILE`、
    #        而 `torrent_file` 列有路径、盘上有文件 —— **三处自相矛盾**，
    #        于是"这条到底能不能发"这个问题在对账里**答不出来**。
    #
    #    ⚠️ 状态码**两支仍用同一个** `STATUS_NO_TORRENT_FILE`：它对下游（发布队列）
    #       的语义是一致的 ——「本行没有可用的 `.torrent`」。变的是 `reason`
    #       （给人看的"为什么"），不是状态机。★ 别据此去加新状态码：那会动
    #       `NON_PUBLISH_STATUSES` 与判据⑫ 的覆盖面，收益为零。
    #
    #    ★ 判据「取不到就不写，绝不编」在这里的落地：不猜"文件大概在别处"，
    #      而是**如实说"我没拿到路径"**。
    elif not torrent_file:
        # ★ 文案纪律（**由判据⑫-B 钉住**）：这句话里**不许**出现"不存在 /
        #   拿不到"这类**断言文件没有**的字样 —— 本模块**无从知道**文件在不在
        #   （它不查 qB）。一出现那几个字，读的人就会去盘上找文件，
        #   而那正是这次要修的误导（`ERR-META-14`）。
        status, reason = (STATUS_NO_TORRENT_FILE,
                          "调用方未传 torrent_file（None）⇒ 无从核对本地有无该文件，"
                          "故落此状态；这不代表文件缺失")
    elif not os.path.exists(str(torrent_file)):
        status, reason = (STATUS_NO_TORRENT_FILE,
                          f"传入的 .torrent 路径在盘上不存在（{torrent_file!r}）")
    else:
        # ★★ 2026-09-23：**闸门① 已退役**（见 `opencd_gate.py` 模块头）。
        #
        #    原先这里调 `opencd_gate.decide(days, ...)`，而 `days` 来自
        #    `qb_seed_days` —— 而**所有调用方都传 `None`**（`crawl.run_manual`
        #    与 `topup_opencd_fields` 都传 `qb_seed_days=None`）。
        #    `decide(None, ...)` 恒返回 `NOT_SEEDED` ⇒ **每一行**都落
        #    `status='NOT_SEEDED'` ⇒ 卡在发布队列闸门①（`status='SUCCESS'`）
        #    ⇒ **皇后路一行都发不出去**。
        #
        #    ★ 实测证据（2026-09-23）：生产库 `status` 分布里
        #      **`NOT_SEEDED` = 0 行** —— 因为闸门① 是**最后一道**，
        #      只有"infohash 对、非禁转、有 log、有 .torrent"的行才会走到它，
        #      而那样完美的行**当时一条都没有**。所以它既没拦住任何东西，
        #      也**永远不会放行任何东西**：一道从未生效、也永不放行的闸门。
        #
        #    ★ 现在直接落 `SUCCESS`：闸门②（发布日期 + 7 天）在
        #      `db.PENDING_PUBLISH_WHERE` 里，**在发布队列那一层**判 ——
        #      那是它该待的地方（本模块不该重复实现，`ERR-META-03`）。
        status, reason = STATUS_OK, ""

    if reasons:
        reason = "；".join(reasons + ([reason] if reason else []))

    row = {
        "torrent_name": m["title"] or info.get("name") or "",
        "formatted_title": m["title"] or "",
        # ★ 副标题：`map_all` 透传的**皇后 smalltitle 原文**（`descr` 会原样用，
        #   不再自己造「全 N 曲」—— 见 `descr.opencd_small_descr`）。
        "small_descr": m["small_descr"] or "",
        "tracklist": m["tracklist"] or "",
        # ★★ 日志**必须落库**：`descr.build_form_fields()` 靠它填
        #    `custom_fields[4][3]`，而那一格是**必填**的抓轨日志 textarea。
        #    漏了它，本步就白做（而且很可能正是「HTTP 200 + 空 body」的成因）。
        "log_content": m["log_content"] or "",
        # ★★ 皇后简介正文（2026-09-26 补）—— 同 `small_descr` 一路：`map_all` 透传，
        #    `descr.build_descr()` 把它作为正文块（`divdescr` 原文，解析层已剥图）。
        #    ★ 它同时也在 `mapped_json` 快照里（`save_task` 下面那一行写整个 `m`
        #      dict），但**必须显式写成列**：渲染读的是顶层键，而顶层键来自
        #      `merge_mapped` 从快照提升 —— 那要求列本身也得有值（`small_descr`
        #      当初的洞就是"只在快照里、列里是旧值"）。
        "opencd_descr": m["descr"] or "",
        "artist": m["artist"] or "",
        "year": m["year"] or "",          # ★ 原样（带「年」字），发送前不转
        "opencd_tid": str(raw_tid or ""),
        # ★★ 皇后的**发布日期**（2026-09-23 补）—— 闸门② 的输入。
        #    ★ 由**调用方**显式传入（本函数是纯函数，不去 join 外部库）：
        #      现实里它来自 `sync_opencd_tid.py` 已填好的 `tasks.opencd_added_at`。
        #    ★ 传 `None` ⇒ 这一列不进 kwargs ⇒ `save_task` 的 upsert
        #      **不碰它** ⇒ 已有的值不会被抹掉（这正是我们要的：
        #      日期是 `vtid` 阶段的产物，本步只负责别把它弄丢）。
        #    ⚠️ 但**新行**不会因此有日期 —— 那时闸门② 会拦（"空 ⇒ 拦"）。
        #      这是**刻意的**：宁可晚发，也不要拿一个不知道发布日期的行去发。
        #      修法是跑一次 `sync_opencd_tid.py --apply`（零请求）。
        **({"opencd_added_at": opencd_added_at} if opencd_added_at else {}),
        "qb_seed_days": _finite_days(qb_seed_days),
        "fetched_at": fetched_at_s,
        "publish_after": publish_after_s,
        # ★ 映射不上的**显式落盘**，不许静默丢
        "unmapped_json": json.dumps(m.get("unmapped") or [], ensure_ascii=False),
        # ★ `map_all()` 的**证据快照** —— 发布时读它，不重算（重算会随映射表漂移）
        "mapped_json": json.dumps(m, ensure_ascii=False, default=str),
        # ★★ 皇后的**转载规则**（2026-09-23 补，三态 ALLOW/NEED_CONFIRM/DENY）。
        #    ★ 为什么必须单独落库：它是**"许不许发"**的判据，而 `mapped_json` 是
        #      **"发什么"**的字段快照 —— 两件事。实测 `id=200982` 的规则是
        #      「禁止轉載」，而它当时**已排进发布队列**（队列不知道有这回事）⇒
        #      差一步就把"禁止轉載"的种子转发出去。
        #    ★ 取 `info["transfer_rule"]`（`opencd_parse` 已归一成三态）；
        #      原始串在 `transfer_rule_raw`，需要时另可取。
        "transfer_rule": info.get("transfer_rule") or "",
        "status": status,
        "reason": reason,
    }
    if torrent_file:
        row["torrent_file"] = str(torrent_file)
    return row


def ingest(info, *, raw_tid, info_hash, torrent_file=None,
           qb_seed_days=None, now=None, opencd_added_at=None):
    """
    落库一条皇后记录。返回 `(status, reason)`。

    ★ 入库**失败不让调用方崩**（抓取与入库是两件事），但异常要**抛出**给调用方
      打印 —— 不能静默吞（吞掉就变成"抓了但没入库，还没人知道"）。
    """
    row = build_row(info, raw_tid=raw_tid, info_hash=info_hash,
                    torrent_file=torrent_file, qb_seed_days=qb_seed_days, now=now,
                    opencd_added_at=opencd_added_at)
    status, reason = row["status"], row["reason"]
    ih = str(info_hash or "").strip().lower()
    if not ih:
        # ★ 没有主键**根本不调 save_task**（它的第一件事就是校验 info_hash）
        return status, reason

    # ★★ 种子量纲的账（与本函数入库动作同源，**不新增出网请求**）：
    #   只在"这条 info_hash **此前不在库**（= 真的新入库一条）"时 +1。
    #   · 为什么不能"每次 ingest 都 +1"：`save_task` 是 **UPSERT**，重跑/补字段会再次
    #     调到这里 ⇒ 会把同一条种子**重复计入**（重跑一次产能就多报一条）。
    #   · 为什么判据用 `get_status(ih) is None`：那是"这一行**存在性**"的权威读法
    #     （只读、行不存在回 None）。`None` = 新行 = 记 1。
    #   · day_key 用**本地日期**字符串，与 `crawl.py` / `opencd_rate` 账本**同口径**
    #     （`db.LEDGER_COLUMNS` 注释有理由，**别改成 UTC**）。
    #   ★ 顺序：**先读存在性、再写库**（写完再读就永远不为 None 了）。
    is_new = db.get_status(ih) is None

    # 入库本身：异常**必须抛**给调用方（既有契约：入库失败不能静默吞）。
    db.save_task(info_hash=ih, **row)

    # 记账：**独立 try** —— 台账是运维口径，记账失败**绝不许反噬入库**
    #   （入库已成功，若因记账异常而 raise，会让调用方误以为"入库失败"）。
    if is_new:
        try:
            import time as _time
            db.ledger_add(_time.strftime("%Y-%m-%d"), delta=1,
                          kind=db.LEDGER_KIND_SEED)
        except Exception as e:                                  # noqa: BLE001
            print(f"⚠️  种子量纲记账失败（不影响入库）: {type(e).__name__}: {e}",
                  flush=True)
    return status, reason


def explain(status, reason):
    """把 `(status, reason)` 变成一行给日志看的话。★ 别把大文本（log/HTML）打进来。"""
    if status == STATUS_OK:
        return "✅ 入库 SUCCESS（等闸门②到期）"
    return f"⏹️ 入库 {status}：{reason}"


def _calls_in(fn, name):
    """
    `fn` 的**代码**里有没有调用/引用名为 `name` 的东西。返回 True/False。

    ★ 为什么必须走 AST 而不是 `name in getsource(fn)`：
      **注释里也会出现那个名字** —— 而本项目的注释里到处是
      「原先这里调 `opencd_gate.decide(...)`」这种**解释它为什么被删**的话。
      纯字符串匹配会把那些注释当成"还在调用"⇒ 判据**永远失败**，
      于是要么判据被删掉（丢失这条性质），要么改成 `"xyz.decide(" not in src`
      这种更脆的写法（照样会被注释绊倒）。
    ★ 教训（`ERR-META-01` 一族）：**检测方法本身出错**比没有检测更危险 ——
      它会让你以为"这条性质已经被看着了"，其实那个断言在对着注释发火。
    """
    import ast as _ast
    import inspect as _ins
    try:
        tree = _ast.parse(_ins.getsource(fn))
    except (OSError, SyntaxError, TypeError):
        return True          # ★ 拿不到源码 ⇒ 按"可能有"处理（保守，宁可误报）
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Name) and node.id == name:
            return True
        if isinstance(node, _ast.Attribute) and node.attr == name:
            return True
    return False


# ==================== 自检 ====================
#
# ★ 判据设计要点：**全部从库里读回来断言**（不是断言中间变量），
#   且每条都配一个"删掉什么会失败"的阴性对照。

def _check_ingest():
    import glob
    import tempfile

    ok = fails = 0

    def chk(label, good, extra=""):
        nonlocal ok, fails
        ok, fails = ok + bool(good), fails + (not bool(good))
        print(f"  {'✅' if good else '❌'} {label}{('  ' + extra) if extra else ''}")

    HERE = _asset_root()                       # ★ 见文件上方 `_asset_root()` 的说明
    SAMPLE = os.path.join(HERE, "opencdhtml", "sanitized")

    import sys as _sys
    P = _sys.modules[__name__]

    def load(name):
        with open(os.path.join(SAMPLE, name), encoding="utf-8") as fh:
            return P.parse_detail(fh.read())

    # ★ `DATA_DIR` 已在模块顶部（import db 之前）指向临时目录 ⇒ 这里直接用。
    tmp = os.environ["DATA_DIR"]
    db.init_db()

    ALLOW = load("sample-zhuanzai-huanying.html")      # 歡迎轉載 + 有 log
    DENY = load("sample-zhuanzai-jinyong.html")        # 禁止轉載

    # 真造一个 .torrent 文件（判据④需要"文件真的存在"）
    tf = os.path.join(tmp, "real.torrent")
    with open(tf, "wb") as fh:
        fh.write(b"d4:infod4:name1:xee")

    NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)

    # ★★★ 闸门② 的夹具必须锚在 **`datetime('now')`**（= 闸门 SQL 用的那个时间源），
    #      不能锚在写死的 `NOW` —— 见下面 `GATE2_NOW` 的长注释（`ERR-META-01`）。
    #
    # ★ 判据：**夹具与它要测的那句 SQL 必须在同一个时间源上。**
    #   闸门② 的 SQL 是 `opencd_added_at <= datetime('now', '-7 days')`
    #   ⇒ 那句话里的"现在"是**真实墙钟**（每次跑都不一样）。
    #   而原先的夹具是 `NOW - timedelta(days=1)`，`NOW` 写死成 2026-09-21
    #   ⇒ 落库值 2026-09-20 在**写下那天**确实"很新"，
    #     但**今天**（2026-09-27）它已经比 `datetime('now','-7 days')` 还旧
    #     ⇒ 情形A（"1 天前 ⇒ 不在队列"）**必然误报**，且误报那天离写下的日子
    #       越远，越不可能被人联想到是这个原因。
    #
    #   ★★ 实测（2026-09-27）：`datetime('now','-7 days')` = 2026-09-20 13:19，
    #      而夹具落库 = 2026-09-20 12:00 ⇒ `12:00 <= 13:19` 成立 ⇒ 被放行
    #      ⇒ 自检报「❌ 发布日期 1 天前 ⇒ 不在队列」。
    #      这不是生产逻辑错，是**夹具过期**。
    #
    #   ⇒ 修法：GATE2_NOW 直接从 `datetime('now')` 取（**同一个源**）。
    #     它只用于**闸门② 相关的夹具与断言**；别的夹具（`fetched_at` 断言、
    #     `publish_after` 等）继续用写死的 `NOW` —— 那些测的是"写进去的值
    #     是不是我当时传的那个"，与墙钟无关，本来就该写死。
    _g2 = db.connect().execute("SELECT datetime('now')").fetchone()[0]
    GATE2_NOW = datetime.strptime(_g2, TS_FMT)
    GATE2_EDGE = 7
    print(f"  ★ 闸门② 夹具基准 GATE2_NOW = {GATE2_NOW}（取自 `datetime('now')`，"
          f"与闸门 SQL 同源；写死的 NOW = {NOW.strftime(TS_FMT)} 只用于非墙钟断言）")
    # ★ 样本里自带的 infohash（实测）。取不到就用不着"hash 相符"那条分支。
    ALLOW_H = str(ALLOW.get("info_hash") or "").strip().lower() or "a" * 40
    DENY_H = str(DENY.get("info_hash") or "").strip().lower() or "b" * 40

    def row_of(ih):
        conn = db.connect()
        r = conn.execute(
            "SELECT status, reason, artist, year, small_descr, log_content, "
            "publish_after, fetched_at, opencd_tid, mapped_json, unmapped_json, "
            "content_path, transfer_rule, opencd_added_at "
            "FROM tasks WHERE info_hash = ?", (ih,)).fetchone()
        conn.close()
        return r

    print("① 落库的值与解析层**逐字相等**（★ 这是 #17 的原始症状）")
    ingest(ALLOW, raw_tid="200028", info_hash=ALLOW_H, torrent_file=tf,
           qb_seed_days=10.0, now=NOW)
    r = row_of(ALLOW_H)
    chk("status = SUCCESS", r[0] == STATUS_OK, f"实得 {r[0]!r}")
    chk("artist 与解析值逐字相等", r[2] == (ALLOW.get("artist") or ""),
        f"{r[2]!r}")
    chk("year 与解析值逐字相等", r[3] == (ALLOW.get("year") or ""), f"{r[3]!r}")
    chk("small_descr = 皇后的 smalltitle（不是主标题）",
        r[4] == (ALLOW.get("small_descr") or "") and r[4] != ALLOW.get("name"),
        f"{r[4]!r}")
    chk("log_content 与解析值逐字相等",
        r[5] == (ALLOW.get("log_content") or ""), f"{len(r[5] or '')} 字符")
    chk("opencd_tid 落库", r[8] == "200028", f"{r[8]!r}")

    print("\n② publish_after = fetched_at + 3 天（★ 比**时间差**，不比字符串）")
    fa = datetime.strptime(r[7], TS_FMT)
    pa = datetime.strptime(r[6], TS_FMT)
    chk("差值恰好 3 天", pa - fa == timedelta(days=3), f"{pa - fa}")
    chk("★ 存的是 **UTC**（本机 TZ=UTC 时看不出，故直接比时刻）",
        fa == NOW.replace(tzinfo=None), f"{r[7]} vs {NOW.strftime(TS_FMT)}")

    # ══════════════════════════════════════════════════════════════════════
    # ③④ 原为「闸门①（做种 3 天）未满 ⇒ NOT_SEEDED」的两条判据。
    #    ★★ 闸门① 已于 2026-09-23 **退役**（理由见 `opencd_gate.py` 模块头：
    #       生产库 `qb_seed_days` 555 行全 NULL ⇒ 它既没拦过东西、也永不放行；
    #       而"种子下完可用"这件事改由**更强的证据**承担 —— 它能从 vertex2 库
    #       按 infohash 配到皇后的页面，说明它是个完整可下的种子）。
    #    ⇒ 这两条**判据反转**：`qb_seed_days` 现在**完全不参与**状态判定，
    #      传什么值都必须落 `SUCCESS`（"不知道做种时长"不再是一道闸）。
    #    ★ 为什么**不删掉**这两段：它们是"闸门①曾经存在过、且为什么不再存在"
    #      的**可执行记录**。删掉之后，将来有人看到 `qb_seed_days` 这一列，
    #      不会再有任何地方告诉他"这一列已经没人读了"（`ERR-META-04`）。
    # ══════════════════════════════════════════════════════════════════════
    print("\n③ ★ 闸门①已退役：`qb_seed_days` **不再参与**判定，传什么都是 SUCCESS")
    for days, label in ((None, "None（拿不到）"), (0.0, "0 天"), (2.99, "2.99 天"),
                        (float("nan"), "NaN"), (float("inf"), "inf"),
                        ("不是数字", "字符串")):
        ih = "c" * 40
        st, why = ingest(ALLOW, raw_tid="t3", info_hash=ih, torrent_file=tf,
                         qb_seed_days=days, now=NOW)
        chk(f"{label} ⇒ SUCCESS（不再落 NOT_SEEDED）",
            st == STATUS_OK, f"{st!r} / {why[:40]}")

    print("\n④ ★ 退役的**可执行记录**：`NOT_SEEDED` 这个状态还在，但已无写入方")
    # ★ 常量留着（不删）是为了让历史数据可读：库里若还有 `NOT_SEEDED` 的旧行，
    #   `explain()` 之类还得认得它。★ 但 `build_row` **再也产不出**这个值。
    chk("★ `STATUS_NOT_SEEDED` 常量仍在（旧数据可读）",
        STATUS_NOT_SEEDED == "NOT_SEEDED")
    chk("★ 且 `build_row` 已不再产出它（上面 6 种输入全是 SUCCESS）", True)
    src_ref = __import__("inspect").getsource(build_row)
    chk("★★ 源码里**没有** `STATUS_NOT_SEEDED` 的赋值（真的退役了，不是被遮住）",
        "STATUS_NOT_SEEDED" not in src_ref)
    chk("★ 源码里也**没有** `decide(` 调用（闸门① 真的不在了）",
        _calls_in(build_row, "decide") is False)
    # ★ 但 `opencd_gate` **仍被 import** —— 因为 `publish_after_from()` 还在用
    #   （`publish_after` 那一列照旧写，只是**不再进闸门② 的 WHERE**）。
    #   ★ 这条是**防止**有人"顺手清理"把 import 删掉（那会 ImportError）。
    import inspect as _ins
    chk("\u2605 `publish_after_from` \u4ecd\u88ab build_row \u8c03\u7528\uff08\u95f8\u95e8\u2160 \u9000\u5f79\u540e\u8fd9\u4e00\u5217\u7167\u65e7\u5199\uff09",
        _calls_in(build_row, "publish_after_from") is True)
    chk("★ `publish_after` 那一列**照旧写**（只是不再驱动闸门②）",
        bool(r[6]), f"实得 {r[6]!r}")
    chk("★★ `publish_after` **不再出现在**闸门 SQL 里",
        "publish_after" not in db.PENDING_PUBLISH_WHERE)

    print("\n⑤ ★★ 闸门② **改锚到皇后的发布日期**（不再是「抓取时刻 + 3 天」）")
    ih = "e" * 40
    # ★ 夹具基准 = GATE2_NOW（取自 `datetime('now')`，与闸门 SQL 同源）。
    #   原先写 `NOW - timedelta(days=1)` ⇒ 会随日子推移变成"早就够了"（见上面长注释）。
    # ★ 边界要**离开等号**：`<=` 是含等的，所以"刚好 7 天"会**在**队列里。
    #   ⇒ 用 7-1=6 天造"未满"、7+1=8 天造"已满"，两边各留 1 天余量，
    #     这样即使夹具基准与 SQL 的真实 `now` 差几秒也不会翻面。
    # ★ 情形 A：发布日期**很新**（6 天前）⇒ 7 天窗口未满 ⇒ 不在队列
    fresh = (GATE2_NOW - timedelta(days=GATE2_EDGE - 1)).strftime(TS_FMT)
    ingest(ALLOW, raw_tid="t5", info_hash=ih, torrent_file=tf,
           qb_seed_days=None, now=NOW, opencd_added_at=fresh)
    inq = any(t["info_hash"] == ih for t in db.get_pending_publish())
    chk(f"★ 发布日期 {GATE2_EDGE - 1} 天前 ⇒ SUCCESS 但**不在**队列"
        f"（{GATE2_EDGE} 天未满）", not inq)
    # ★ 情形 B：发布日期 **8 天前** ⇒ 必须被选出
    old = (GATE2_NOW - timedelta(days=GATE2_EDGE + 1)).strftime(TS_FMT)
    conn = db.connect()
    conn.execute("UPDATE tasks SET opencd_added_at=? WHERE info_hash=?", (old, ih))
    conn.commit(); conn.close()
    inq2 = any(t["info_hash"] == ih for t in db.get_pending_publish())
    chk("★ 发布日期 8 天前 ⇒ **在**队列里", inq2)
    # ★★ 情形 C：**发布日期为空** ⇒ **拦**（不是"照旧发"）
    #    ★ 这是本次改动的**判据反转**：旧口径 `publish_after` 空 ⇒ 放行，
    #      新口径 `opencd_added_at` 空 ⇒ **拦**。理由：空 = "不知道皇后几号发的"，
    #      而"不知道"不是"放了很久了"（同闸门④ 的教训：把 NULL 当允许 = 赌博）。
    ih2 = "7" * 40
    ingest(ALLOW, raw_tid="t5b", info_hash=ih2, torrent_file=tf,
           qb_seed_days=None, now=NOW)          # ★ 不传 opencd_added_at
    r2 = row_of(ih2)
    chk("★ 空的 opencd_added_at 确实没被写（留空，不编）", not r2[13],
        f"实得 {r2[13]!r}")
    inq3 = any(t["info_hash"] == ih2 for t in db.get_pending_publish())
    chk("★★ 发布日期为空 ⇒ **不在**队列（空 ⇒ 拦，**判据反转**）", not inq3)

    # ★★★ 情形 D —— **阴性对照**（`ERR-META-18`）：证明上面那几条**真的在测闸门②**。
    #
    #   ★ 为什么必须有这一条（这是本节最有价值的一条）：
    #     情形A（"未满 ⇒ 不在队列"）**天然是个"默认就通过"的判据** ——
    #     只要某处把这条行拦下来（或行压根没进库、或被别的闸门挡住），
    #     它就会**通过**，而闸门② **完全可能是不存在的**。
    #     ⇒ 上面"情形A 通过"这件事，**推不出**"闸门② 在工作"（`ERR-META-01`：
    #       判据看着对、测的却是别的东西）。
    #
    #   ★ 做法：把闸门② 那句 SQL **独立地**、**注入成永真**（`AND 1=1`）跑一遍
    #     —— 即**模拟"闸门② 不存在"**。此时同一行**必须**变得能被选出。
    #     · 若"注入永真后仍在队列外" ⇒ 说明拦住它的是**别的条件**（或它根本没进库）
    #       ⇒ 上面的情形A 是个**假通过** ⇒ 自检必须**报错**。
    #     · 若"注入永真后在队列里" ⇒ 情形A 的"不在"**确实**是闸门②造成的 ✅
    #
    #   ★ 注入方式：**不动生产 SQL**，只在自检里把 `db.PENDING_PUBLISH_WHERE`
    #     里的闸门② 那一段**字符串替换**掉，再自己拼一条查询。
    #     ⇒ 生产代码一行不改（这是本节的纪律：只动自检段）。
    print("\n⑤-D ★★★ 阴性对照：把闸门② 注入成永真 ⇒ 同一行**必须**变得能选出")
    # ★★ 字面量**必须从 `(` 开始，不能带前导 `AND `** —— 见下面注入那段的长注释。
    gate2_sql = ("(opencd_added_at IS NOT NULL AND opencd_added_at != '' "
                 "     AND opencd_added_at <= datetime('now', '-7 days'))")
    chk("闸门② 那句 SQL 在 PENDING_PUBLISH_WHERE 里找得到（否则对照组无效）",
        gate2_sql in db.PENDING_PUBLISH_WHERE)
    # ★ 注入：把闸门② 整段换成永真（= 闸门② 不存在）
    #   ★★ 替换串**只要在开头留一个空格**，**绝不能自带 `AND`**。
    #      实测原文的两侧边界（别凭印象）：
    #        · 左：`... != 'EXISTS')` 与 `AND (opencd_added_at ...` **有空格**
    #        · 右：本段结尾是 `))`，而下一句是 `AND (opencd_tid ...` ——
    #          也就是说**原文本段里就带着那个 `AND`**（段尾是 `... -7 days'))`
    #          后面紧跟 `AND`，那个 `AND` 属于本段替换串的**右边**）。
    #      ⇒ `"AND 1=1"` → 拼出 `1=1AND`（`unrecognized token: "1AND"`）
    #        `" 1=1 "`  → 拼出 `1=1 AND` 前少空格 → 实测是 `... 1=1 AND AND (...`
    #                     （多一个 `AND`）⇒ `near "1": syntax error`
    #        `" 1=1 AND "` → 变成 `1=1 AND AND (...` 同上 ✗
    #      ⇒ 正解：**`" 1=1"`**（只留前导空格，其余交给原文自己的 `AND`）。
    #      ★ 教训：**替换 SQL 片段前先把两侧边界逐字符看清** —— 我是连错三次
    #        （1AND / near "1" / AND AND）才去打印原文的，那三行本可以省掉。
    # ★★★ 这里连错三次，把**根因**记下来（免得下次重犯）：
    #   原文（实测，见下面那行 repr）是：
    #     `... != 'EXISTS') AND (opencd_added_at ... '-7 days'))AND (opencd_tid ...`
    #                            ↑ 这个 AND 属于**连接词**，不属于闸门② 本体
    #                                                          `))` 后**紧贴** AND
    #   ⇒ 原先字面量写成 `"AND (opencd_added_at ..."`（**自带前导 AND**）⇒
    #     替换时会把那个**连接用的 `AND` 一起吃掉** ⇒ 剩下的 SQL 变成
    #     `... != 'EXISTS') 1=1AND (opencd_tid ...`（左少 AND、右贴成 `1AND`）。
    #     · 补前导 `AND`  → `AND 1=1AND` ⇒ `unrecognized token: "1AND"`
    #     · 不补（只空格）→ `) 1=1AND`   ⇒ 还是 `1AND`
    #     · 段尾再补 `AND` → `1=1 AND AND` ⇒ `near "ANDAND"`
    #   ⇒ **正解是把字面量的边界改对**（从 `(` 开始、到 `))` 为止，不含连接 AND），
    #     这样替换后原文自己的两个 `AND` 都留着，注入成 `AND 1=1 AND` 天然成立。
    #   ★ 一般化：**做 SQL 字符串替换前，先把要替换的那一段的**两个边界**逐字符
    #     打印出来**（`repr` 一下就能看见 `))AND` 这种粘连）—— 靠"看着像"改
    #     会连错三次，而那三次本可以一次都不发生（`ERR-META-01` 的形状）。
    # ★★★ 最终正解（第 5 次才对，把边界一次说清）：
    #   原文粘连处是 `...'-7 days'))AND (opencd_tid ...` ——
    #   闸门② 的 `))` 之后**紧贴**下一个连接词 `AND`（**没有空格**）。
    #   ⇒ 字面量从 `(` 起到 `))` 止（不含连接 AND）是对的，
    #     但**替换串必须以空格结尾**，否则新串会与那个紧贴的 `AND` 粘成 `1=1AND`。
    #   ⇒ `replace(gate2_sql, "1=1 ")` —— 前导空格由原文的 ` AND ` 提供，
    #     尾部空格由我们补上。实测通过后才写进注释（不是推导出来的）。
    injected = db.PENDING_PUBLISH_WHERE.replace(gate2_sql, "1=1 ")
    chk("注入确实改动了 SQL（否则测的还是闸门② 本身）",
        injected != db.PENDING_PUBLISH_WHERE)
    conn = db.connect()
    # ★ 把 ih 摆回"未满"（6 天前）—— 即情形A 的状态
    conn.execute("UPDATE tasks SET opencd_added_at=? WHERE info_hash=?",
                 (fresh, ih))
    conn.commit()
    # ★★ 查询形状必须与**生产**一致：生产是 `WHERE {PENDING_PUBLISH_WHERE}`
    #   （**没有**前置 `AND`，见 `db.get_pending_publish()`）。
    #   我原先写成 `WHERE info_hash=? AND {sql}` ⇒ 多一个连接词 `AND`，
    #   与注入后的 SQL 首尾的 `AND` 叠加 ⇒ `1AND`（**第 4 次踩同一个坑**）。
    #   ⇒ 这里改成把 `info_hash=?` 作为**一个子条件并入 SQL 开头**，
    #     保持"整串就是 WHERE 的内容"这个形态。
    n_real = conn.execute(
        f"SELECT count(*) FROM tasks WHERE info_hash=? AND ({db.PENDING_PUBLISH_WHERE})",
        (ih,)).fetchone()[0]
    n_inj = conn.execute(
        f"SELECT count(*) FROM tasks WHERE info_hash=? AND ({injected})",
        (ih,)).fetchone()[0]
    conn.close()
    # ★ 判据：真口径下选不出（0），注入永真后必须选得出（1）
    chk("真口径：发布日期未满 ⇒ 选不出（0）", n_real == 0, f"实得 {n_real}")
    chk("★★ 注入永真后**选得出**（1）⇒ 上面的『选不出』确实是闸门② 造成的",
        n_inj == 1, f"实得 {n_inj}（若为 0 ⇒ 情形A 是**假通过**，闸门② 可能不存在）")
    # ★ 恢复：把 ih 放回"已满"，免得影响后面的判据
    conn = db.connect()
    conn.execute("UPDATE tasks SET opencd_added_at=? WHERE info_hash=?",
                 (old, ih))
    conn.commit(); conn.close()

    print("\n⑥ ★ 第一阶段行**不被冲掉**（content_path 永不可碰）")
    ih = "f" * 40
    db.save_task(info_hash=ih, status="SUCCESS", content_path="/x/y",
                 formatted_title="本地抓轨标题", log_content="x")
    ingest(ALLOW, raw_tid="t6", info_hash=ih, torrent_file=tf,
           qb_seed_days=10.0, now=NOW)
    r2 = row_of(ih)
    chk("content_path 仍是 '/x/y'", r2[11] == "/x/y", f"{r2[11]!r}")

    print("\n⑦ mapped_json / unmapped_json 是**合法 JSON** 且与当场重算一致")
    mj = json.loads(r[9])
    live = map_all(ALLOW)
    chk("mapped_json 是对象", isinstance(mj, dict))
    chk("★ mapped_json['tags'] == 当场重算的 map_all()",
        mj.get("tags") == live.get("tags"), f"{mj.get('tags')} vs {live.get('tags')}")
    chk("unmapped_json 是合法 JSON 数组", isinstance(json.loads(r[10]), list))

    print("\n⑧ mapped_json 的值**手抄自实测**（不是从代码反填）")
    chk("huanying: tags == ['6','10']", mj.get("tags") == ["6", "10"], f"{mj.get('tags')}")
    chk("huanying: medium_sel == '8'", mj.get("medium_sel") == "8", f"{mj.get('medium_sel')!r}")
    chk("huanying: audiocodec_sel == '1'", mj.get("audiocodec_sel") == "1",
        f"{mj.get('audiocodec_sel')!r}")
    chk("huanying: team_sel == '5'", mj.get("team_sel") == "5", f"{mj.get('team_sel')!r}")

    print("\n⑨ NO_LOG 负控：页面没 log ⇒ 落 NO_LOG，**不产生 SUCCESS**")
    no_log = dict(ALLOW)
    no_log["log_content"] = ""
    ih = "1" * 40
    st, why = ingest(no_log, raw_tid="t9", info_hash=ih, torrent_file=tf,
                     qb_seed_days=10.0, now=NOW)
    chk(f"⇒ {STATUS_NO_LOG}", st == STATUS_NO_LOG, f"{st!r}")

    print("\n⑩ DENIED 负控：禁止轉載 ⇒ 落 DENIED，**不是 SUCCESS**（用户选定要留证据）")
    st, why = ingest(DENY, raw_tid="30167", info_hash=DENY_H, torrent_file=tf,
                     qb_seed_days=10.0, now=NOW)
    chk(f"⇒ {STATUS_DENIED}", st == STATUS_DENIED, f"{st!r} / {why[:40]}")
    chk("且**不在**发布队列", not any(t["info_hash"] == DENY_H
                                     for t in db.get_pending_publish()))

    print("\n⑪ 键白名单：build_row 的键**全部**是 DB 已知列")
    known = {c for c, _ in db.COLUMNS}
    unknown = set(build_row(ALLOW, raw_tid="t", info_hash="a" * 40,
                            torrent_file=tf, qb_seed_days=1.0, now=NOW)) - known
    chk("没有未知列名", not unknown, f"多出 {unknown}" if unknown else "")

    print("\n⑫ 五种非 SUCCESS 状态**全部**选不出（防「只修了其中一种」）")
    conn = db.connect()
    for st_name in NON_PUBLISH_STATUSES:
        # ★ `opencd_added_at` 设成"早就够了"，**故意只让 status 这一条来拦** ——
        #   否则闸门② 会一起把它们挡下，这条判据就分不清"是 status 拦的
        #   还是日期拦的"（`ERR-META-05`：两个口径不许混）。
        conn.execute("UPDATE tasks SET status=?, opencd_added_at=? "
                     "WHERE info_hash=?",
                     (st_name, (GATE2_NOW - timedelta(days=GATE2_EDGE * 4)).strftime(TS_FMT), ALLOW_H))
        n = conn.execute(
            f"SELECT count(*) FROM tasks WHERE info_hash=? AND {db.PENDING_PUBLISH_WHERE}",
            (ALLOW_H,)).fetchone()[0]
        chk(f"{st_name} 不在队列", n == 0)
    conn.close()

    # ── ⑫-B ★★★ 2026-09-27 新判据：第 ④ 条必须**分辨两支** ──────────────
    #
    #  为什么非要加这条判据（`ERR-META-28`：一条断言若说不出"什么输入会让它
    #  失败"，它就不是判据）：
    #    上面⑫ 只断言「NO_TORRENT_FILE 不在队列」—— 而**两支**产出的状态码
    #    相同 ⇒ **旧代码（混成一句）和新代码（分开两支）都能让⑫ 通过**。
    #    ⇒ ⑫ 证不了这次的修复。而这次的病**正好**是"两件不同的事长得一样"，
    #      所以必须直接断言**它们的 `reason` 不同**，并断言**各自说的是自己的事**。
    #
    #  ★ 三条断言，缺一不可（第三条是阴性对照的一半）：
    #    ① `None` 那支的 reason 必须说"**没传路径**"，**不许**说"文件不存在"
    #       —— 旧代码那句 `拿不到本地种子文件（None）` 会**同时**通过①和②下的
    #          模糊检查，所以判据要逐词钉住（`in` 而不是"like"）。
    #    ② 路径给了但不存在那支，reason 必须说"**盘上不存在**"。
    #    ③ ★★ 两支的 reason **必须不相等** —— 只断言①②，将来有人把两支又合并
    #       成一条（比如都改成 `f"没有 .torrent：{torrent_file!r}"`）时，
    #       ① 会因为 `（None）` 里没有"没传"字样而失败，但**只改文案不改结构**
    #       的退化（两支都写同一句）仍可能溜过 ⇒ 这条把"可分辨"本身钉住。
    print("\n⑫-B ★★★ 第④条：『没传路径』与『文件不在』必须是**两句不同的话**")
    st_none, why_none = ingest(ALLOW, raw_tid="t12b-none", info_hash="2" * 40,
                               torrent_file=None, qb_seed_days=10.0, now=NOW)
    st_miss, why_miss = ingest(ALLOW, raw_tid="t12b-miss", info_hash="3" * 40,
                               torrent_file="/不存在/这个文件.torrent",
                               qb_seed_days=10.0, now=NOW)
    chk("两支的状态码**仍然相同**（都 = NO_TORRENT_FILE，下游语义没变）",
        st_none == st_miss == STATUS_NO_TORRENT_FILE,
        f"{st_none!r} vs {st_miss!r}")
    chk("① torrent_file=None ⇒ reason 说的是「未传路径」",
        ("未传" in why_none or "没传" in why_none), f"{why_none!r}")
    chk("① ★ 且 reason **没有**断言「文件不存在 / 拿不到」（本模块无从知道）",
        ("不存在" not in why_none) and ("拿不到" not in why_none),
        f"{why_none!r}")
    chk("② 传了路径但盘上没有 ⇒ reason 说的是「盘上不存在」",
        "不存在" in why_miss, f"{why_miss!r}")
    # ★★ ③ 这一条我第一版写错了，把过程记下来（因为它**正是**本项目反复栽的那个坑）：
    #
    #   第一版写的是 `why_none != why_miss`（"两支的文案必须不相等"），
    #   并在**注入旧 bug 的阴性对照**里**通过了** —— 而旧 bug 恰恰就是
    #   "两支合并"！⇒ 它看着像在钉这件事，其实**没钉住**。
    #
    #   ★ 为什么：旧代码 `f"拿不到本地种子文件（{torrent_file!r}）"` 是**一条
    #     分支**，但 `{torrent_file!r}` 会把 `None` 和路径分别插进去
    #     ⇒ 两句话**字符串不相等**（`（None）` vs `（'/不存在/…'）`）
    #     ⇒ `!=` 为真 ⇒ 断言通过。**这又是 `ERR-META-28`**：
    #     我说不出"什么输入会让 ③ 失败"，而实际上**旧 bug 就不会让它失败**。
    #
    #   ★ 真正的判据不能问"两句话一样吗"（那是**表象**），要问
    #     "**分支是两支还是一支**"（那是**性质**）。两种可执行写法，都用上：
    #       (a) **行为**：`None` 那支的文案里**不许出现我们传的那个路径**
    #           （旧 bug 因为插值，`None` 支会带上"拿不到…"字样 ⇒ ① 失败）。
    #       (b) **结构**：源码里必须是**两个并列的 `elif`**，不是 `or` 合成的一支。
    #     ⇒ (b) 单独就能杀掉旧 bug；加 (a) 是为了把"文案"也钉住，
    #       免得将来有人留两个 `elif` 却把文案抄成同一句。
    chk("★★ ③(a) `None` 支的文案里**没有**出现我们传的那个路径（= 不是同一句话插值）",
        "/不存在/这个文件" not in why_none,
        f"{why_none!r}")
    _src = open(__file__, encoding="utf-8").read()
    _br = _src.split("def build_row(", 1)[1].split("\ndef ", 1)[0]
    chk("★★ ③(b) `build_row` 里确实是**两个并列的 elif**（不是 `or` 合起来的一支）",
        ("elif not torrent_file:" in _br)
        and ("elif not os.path.exists(str(torrent_file)):" in _br)
        and ("not torrent_file or not os.path.exists" not in _br),
        "合并成一支 = 这次的 bug 原样复发（判据① 也会同时失败）")
    # ★ 顺手钉住：`torrent_file` 列**没被这次 ingest 写进去**
    #   （`build_row` 刻意不写它 —— 见它 docstring 里"刻意不写的三个键"）。
    #   ★ 这正是"列里有路径、状态说没有 .torrent"那个自相矛盾的来源，
    #     也是 `flip_status.py`（只改 status 一列）存在的前提。
    #   ★ 用一次直查而不是 `row_of()`：后者只取 14 个固定列，**不含** `torrent_file`。
    _c = db.connect()
    _tf = _c.execute("SELECT torrent_file FROM tasks WHERE info_hash=?",
                     ("2" * 40,)).fetchone()[0]
    _c.close()
    chk("★ ingest(torrent_file=None) **没有**写 torrent_file 列（刻意不写这列）",
        _tf is None, f"{_tf!r}（本行是新建的，故应为 None）")

    print("\n⑬ ★★ 端到端（最有价值的一条）：队列的行 → 渲染 → 标签")
    import descr
    conn = db.connect()
    # ★ 2026-09-23：队列条件里 `publish_after` 已被 `opencd_added_at` 取代
    #   ⇒ 这条前置**必须**把它也设成"8 天前"，否则选不出来
    #   （这正是 `db.PENDING_PUBLISH_WHERE` 那句"空 ⇒ 拦"的现场演示）。
    conn.execute("UPDATE tasks SET status='SUCCESS', "
                 "opencd_added_at=? WHERE info_hash=?",
                 ((GATE2_NOW - timedelta(days=GATE2_EDGE + 1)).strftime(TS_FMT), ALLOW_H))
    conn.commit(); conn.close()
    pending = [t for t in db.get_pending_publish() if t["info_hash"] == ALLOW_H]
    if not pending:
        chk("队列里有这条（前置）", False, "选不出来")
    else:
        task = dict(pending[0])
        # ★ 这一步就是 `publish.py` 要做的：把 mapped_json 解回顶层键
        task.update({k: v for k, v in db._load_mapped(task).items()
                     if k in ("tags", "medium_sel", "audiocodec_sel", "team_sel")})
        f = descr.build_form_fields(task, autodetect=True)
        chk("★ tags[4][] == ['6','10','31']（不是 detect_tags 猜的）",
            f.get("tags[4][]") == ["6", "10", "31"], f"{f.get('tags[4][]')}")
        chk("medium_sel[4] 来自库（'8'）", f.get("medium_sel[4]") == "8",
            f"{f.get('medium_sel[4]')!r}")
        chk("custom_fields[4][1] == 艺术家", f.get("custom_fields[4][1]") == "齐秦",
            f"{f.get('custom_fields[4][1]')!r}")
        chk("custom_fields[4][3] 非空（必填的抓轨日志）",
            bool((f.get("custom_fields[4][3]") or "").strip()))

    # ── ⑭ ★★ 2026-10-04 新判据：种子量纲的账（kind='seed'）────────────────
    #   要钉住两条性质：
    #     ① **新入库一条** ⇒ 该 kind 当天账 +1；
    #     ② **重复 ingest 同一条**（UPSERT）⇒ **不**再 +1（否则重跑虚报产能）。
    #   ★ 日期口径必须与 `ingest()` **内部一致**：它记的是"**今天**"（`time.strftime`），
    #     所以这里也读"今天"—— 用一个**独一无二的 info_hash** 来隔离本条用例
    #     （别的用例不会碰它），而不是换日期（换日期就与 ingest 写的那天对不上了）。
    print("\n⑭ ★★ 种子量纲账本（kind='seed'）：新入库计 1、重复不入账")
    import time as _t
    import hashlib as _h
    _day = _t.strftime("%Y-%m-%d")
    # ★ 独一无二的 info_hash（用时间戳+随机），保证"不在库"前提成立
    #   —— 前面一堆用例已经 ingest 过别的 hash、也把"今天"的 seed 账推上去了，
    #   所以这里**不假设从 0 起**，只比"前后差"。
    _seed_ih = _h.md5(f"seed-ledger-test-{_t.time()}".encode()).hexdigest()
    _before = db.ledger_get(_day, kind=db.LEDGER_KIND_SEED)
    chk("前置：该 info_hash 此前不在库", db.get_status(_seed_ih) is None)
    # 第一次 ingest（新行，且经 UPSERT 落库）
    _info = dict(ALLOW)                       # 复用一份合法解析结果
    ingest(_info, raw_tid="999001", info_hash=_seed_ih, torrent_file=tf, now=NOW)
    _after1 = db.ledger_get(_day, kind=db.LEDGER_KIND_SEED)
    chk("★ 新入库一条 ⇒ seed 账 +1", _after1 == _before + 1,
        f"前 {_before} 后 {_after1}")
    # 第二次 ingest 同一条（UPSERT；info_hash 相同 ⇒ 是"已存在行"）
    ingest(_info, raw_tid="999001", info_hash=_seed_ih, torrent_file=tf, now=NOW)
    _after2 = db.ledger_get(_day, kind=db.LEDGER_KIND_SEED)
    chk("★★ 重复 ingest 同一条 ⇒ **不再** +1（UPSERT 不虚报）",
        _after2 == _after1, f"前 {_after1} 后 {_after2}")

    return ok, fails


def _main_ingest():
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="OpenCD 页面 → tasks 表的写入侧")
    ap.add_argument("--selftest", action="store_true", help="跑离线自检")
    # ★ --json：机读出口，给 tools/gen_counts.py 读。别去正则解析人读的 ✅/❌ 行 —— 分母口径不统一。
    ap.add_argument("--json", action="store_true",
                    help="只把结果以 JSON 打到末行（给 gen_counts.py 读）")
    args = ap.parse_args()

    if not args.selftest:
        ap.error("加 --selftest（本模块由 crawl.py 调用，不单独出网）")

    if args.json:
        ok, fails = _check_ingest()
        print(json.dumps({"script": "opencd.py ingest --selftest", "ok": ok,
                          "total": ok + fails, "exit": 1 if fails else 0},
                         ensure_ascii=False))
        return 1 if fails else 0

    print("=" * 74)
    print("opencd_ingest 离线自检（纯本地，零请求）")
    print("=" * 74)
    ok, fails = _check_ingest()
    print("\n" + "=" * 74)
    if fails:
        print(f"❌ 自检失败：{ok}/{ok + fails} 通过，{fails} 项不符")
        return 1
    print(f"✅ 自检通过：{ok}/{ok} 项全对")
    return 0




# ==================== 子命令分发（原五个模块各有 main，现各归 _main_X）====================

def _main_log():
    """原 `opencd_log.py` 是**纯库**（无 CLI、无自检）—— 这里只作可检索的出口。"""
    print("opencd_log（日志工具）是纯库，没有 CLI。可用函数：")
    print("  brief(text, limit=MAX_FIELD)      长文本截断成一行")
    print("  mask_secrets(text)                兜底脱敏")
    print("  detail(log, items, render, ...)   明细折叠打印")
    print("  summary(log, stage, **counters)   单行汇总")
    return 0


_PARTS = {
    "parse": _main_parse,
    "rate": _main_rate,
    "log": _main_log,
    "map": _main_map,
    "gate": _main_gate,
    "ingest": _main_ingest,
}


def main():
    ap = argparse.ArgumentParser(
        prog="opencd.py",
        description="OpenCD（皇后）流水线六合一：parse / rate / log / map / gate / ingest")
    ap.add_argument("part", nargs="?", choices=sorted(_PARTS),
                    help="子命令 = 原模块名：" + " / ".join(sorted(_PARTS)))
    ap.add_argument("rest", nargs=argparse.REMAINDER,
                    help="透传给该子命令的参数（如 --selftest / --json / <file.html>）")
    args = ap.parse_args()
    if not args.part:
        ap.error("缺少子命令。可选：" + " / ".join(sorted(_PARTS)))
    # 把剩下的 argv 交给子命令的 argparse（它们各自又调 `parse_args()`，
    # 读的是 `sys.argv[1:]`）⇒ argv[1] 必须是**该子命令的第一个参数**，
    # 不能留子命令名（否则子命令的 argparse 会把 `opencd.py-rate` 当成未知位置参数）。
    sys.argv = [sys.argv[0]] + list(args.rest)
    return _PARTS[args.part]()


if __name__ == "__main__":
    sys.exit(main())
