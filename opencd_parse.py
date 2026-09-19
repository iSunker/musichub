"""
OpenCD（皇后）种子详情页解析 —— 纯离线、零请求。

设计约束（都来自实测，不是推断）：
1. **不碰网络。** 输入是一段 HTML 字符串，输出一个 `TorrentInfo`。
   所以解析器可以用落盘的样本反复跑、可以离线回归；抓取（第 3 步）与解析解耦。
2. **按行标题文本定位，不用 `nth-child`。**
   参考实现 `auto_feed_js:13306` 用的是
   `#outer > … > tr:nth-child(13)` 取「轉載規則」—— 页面一改版就失效，
   而且它自己承认皇后有两套版面（`opencd_mode` 0/1）却没都覆盖。
   实测（2026-09-19）新版面的结构是稳定的：
       <td class="rowtitle">轉載規則：</td><td>禁止轉載</td>
   ⇒ 按 `td.rowtitle` 的**标签文本**找，再取它的兄弟 `<td>`。
3. **解析失败要能说出来，不能静默给空值。**
   `業餘` 系列坑（`ERR-META-01`「把失败当成功」）在本项目出现过多次，
   所以每个字段都带「有没有取到」的信息，缺失时**显式记录到 `missing`**，
   而不是让调用方拿到一个分不清「空」和「没解析到」的 ""。

用法:
    python opencd_parse.py --selftest              # 跑 opencdhtml/sanitized/ 样本，逐字段对照
    python opencd_parse.py <file.html>             # 解析单个文件，打印 JSON
"""
import argparse
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

try:
    from bs4 import BeautifulSoup
except ImportError:                                     # pragma: no cover
    sys.exit("❌ 需要 beautifulsoup4（Dockerfile 里已固定版本）")

HERE = os.path.dirname(os.path.abspath(__file__))
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
    info["descr"] = _clean_long(div.get_text("\n")) if div else ""
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


def _extract_cover_candidates(soup):
    """
    封面候选：`divdescr` 里 `alt="folder.jpg"` 之类的附件图。
    ★ 实测（2026-09-19）：alt 有 folder.jpg / disc.jpg / back.jpg / CD.jpg 等，
      **folder 优先**（那是通行封面名）。返回 [(alt, src)]，已按优先级排。
    注意 src 是**相对路径**，要拼站点前缀才能下载 —— 拼 URL 是抓取层的事，
    这里只给原始 src（保持纯函数、不掺站点知识）。
    """
    div = soup.find(id="divdescr")
    if div is None:
        return []
    out = []
    for img in div.find_all("img"):
        src = img.get("src") or ""
        if "attachments/" not in src:
            continue
        out.append((img.get("alt") or "", src))

    def rank(item):
        alt = (item[0] or "").lower()
        if "folder" in alt or "cover" in alt or "front" in alt:
            return 0
        if "cd" in alt or "disc" in alt:
            return 1
        return 2

    out.sort(key=rank)
    return out


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


def cmd_selftest():
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

    print(f"\n{'=' * 72}")
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


def main():
    ap = argparse.ArgumentParser(description="OpenCD 详情页解析")
    ap.add_argument("html_file", nargs="?", help="要解析的 HTML 文件")
    ap.add_argument("--selftest", action="store_true", help="跑样本自检")
    ap.add_argument("--brief", action="store_true", help="只打关键字段（不 dump 全文）")
    args = ap.parse_args()

    if args.selftest:
        return cmd_selftest()
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


if __name__ == "__main__":
    sys.exit(main())
