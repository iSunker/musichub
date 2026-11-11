"""种子发布与编辑页面内容渲染层。

负责副标题、BBCode 简介渲染（包含封面图、详情正文、曲目列表、抓轨日志折叠等），
以及 NexusPHP 分类下拉选项（媒介、音频编码、製作組）与标签集合的格式化输出。
"""
import json
import re

# ==================== 站点字段字典（来自 edit.php 表单实测） ====================
# select name="medium_sel[4]"
MEDIUM = {"CD": "8", "SACD": "9", "DVD": "10", "VCD": "11", "OTHER": "12"}

# select name="audiocodec_sel[4]"
# ★★ WAV 的编号**未实测**，刻意不放进这张表 —— 见 detect_audiocodec() 的注释。
AUDIOCODEC = {"FLAC": "1", "MP3": "4", "AAC": "6", "APE": "2",
              "DTS": "3", "OGG": "5", "OTHER": "7"}

# select name="team_sel[4]"
TEAM = {"MUSOPIA": "6", "OTHER": "5", "HDS": "1", "CHD": "2",
        "MYSILU": "3", "WIKI": "4"}

# input type=checkbox name="tags[4][]"
#
#    真实证据：用户提供的 `opencdhtml/音乐乌托邦 __ 发布 - Powered by NexusPHP.html`
#    （乌托邦 `takeupload.php` 的真实表单），逐行数出 `name="tags[4][]"` 的
#    checkbox **正好 44 个**，值从 1 到 47。
#    ⇒ 「乌托邦没有 Soul/Blues/NewAge…这些标签」是**旧判断，错的**：
#      它在真实表单里**一个一个都在**（18 天籁(Soul)、22 蓝调(Blues)、19 新世纪(NewAge)…）。
#
# ★ 原有的 23 项**值全部正确**（逐条对过 44 项表，无一错位）⇒ 这次只做**加法**。
# ★ 表里放**短名**（`流行` 而不是 `流行(Pop)`）：站点表单的 checkbox 正文是
#   `流行(Pop)`，但我们只用 `value`（`10`）提交，label 文字不参与提交。
#   短名同时让 `opencd_map` 的「按名字映射」两边对齐（皇后写 `流行(Pop)`，
#   剥后缀后正是 `流行`）。
TAGS = {
    # —— 原有的 23 项（值已逐条对着 44 项表核过，全对）——
    "禁转": "1", "首发": "2", "港台": "6", "大陆": "4", "欧美": "5",
    "日韩": "7", "其他地区": "8", "流行": "10", "OpenCD": "31", "Redacted": "30",
    "完成加速": "47", "种子加速": "44", "摇滚": "9", "爵士": "11", "电子": "12",
    "金属": "13", "古典": "15", "原声": "16", "发烧": "17", "民间": "23",
    "乡村": "24", "器乐": "34", "儿童": "35",
    #   风格/地区类：
    "天籁": "18", "新世纪": "19", "说唱": "20", "雷鬼": "21", "蓝调": "22",
    "朋克": "25", "民族": "26", "音乐剧": "27", "汽车": "28", "宗教": "29",
    "嘻哈": "32", "latin": "33", "贝斯": "14",
    #   站点/来源类（`Orpheus`/`Jpopsuki` 是两个转发友站，`OpenCD`/`Redacted` 已在上面）：
    "Orpheus": "36", "Jpopsuki": "37",
    #   风格细分类（小写英文，与站点 label 一致）：
    "dance": "38", "japanese": "39", "indie": "40", "korean": "41",
    "piano": "42", "bollywood": "43",
}

# ★★ `tags[4][]` 的真实**全表**（44 项，含短名 → 站点 label 原文）。
#    用途有二，都是为了让「表不全」这件事**看得见**：
#      ① `--selftest` 里判 `len(TAGS) == len(TAGS_LABELS) == 44`，
#         哪天站点加/删标签，这条会**当场响**，而不是静默少勾几个；
#      ② 巡检/对照工具能打印"站点有、我们没有"的差集。
#    ★ 值取自真实表单的 `value`；label 取自 checkbox 的正文（原样，含英文后缀）。
TAGS_LABELS = {
    "禁转": "禁转", "首发": "首发", "港台": "港台", "大陆": "大陆",
    "欧美": "欧美", "日韩": "日韩", "其他地区": "其他地区",
    "流行": "流行(Pop)", "OpenCD": "OpenCD", "Redacted": "Redacted",
    "完成加速": "完成加速", "种子加速": "种子加速",
    "摇滚": "摇滚(rock)", "爵士": "爵士(Jazz)", "电子": "电子(Electronic)",
    "金属": "金属(Metal)", "古典": "古典(Classical)", "原声": "原声(OST)",
    "发烧": "发烧(HiFi)", "民间": "民间(Folk)", "乡村": "乡村(Country)",
    "器乐": "器乐(Instrumental)", "儿童": "儿童(Children's)",
    "天籁": "天籁(Soul)", "新世纪": "新世纪(NewAge)", "说唱": "说唱(Rap)",
    "雷鬼": "雷鬼(Reggae)", "蓝调": "蓝调(Blues)", "朋克": "朋克(Punk)",
    "民族": "民族(National)", "音乐剧": "音乐剧(musical)", "汽车": "汽车(garage)",
    "宗教": "宗教(Religion)", "嘻哈": "嘻哈(hip.hop)", "latin": "latin",
    "贝斯": "贝斯(Drum&Bass)",
    "Orpheus": "Orpheus", "Jpopsuki": "Jpopsuki",
    "dance": "dance", "japanese": "japanese", "indie": "indie",
    "korean": "korean", "piano": "piano", "bollywood": "bollywood",
}

#
#    背景：本项目是**皇后 → 乌托邦**的单向适配。两个站**各自**都有一批标签，
#    但**「皇后有」不等于「该在乌托邦勾上」**。下面按**来源**分三类：
#
#    ① 皇后标签 → 乌托邦同名标签：**该勾**（页面已写明的事实，走 `map_tags`）
#    ② 乌托邦**自己的**标签（皇后侧没有对应物）：**一律不勾**
#       —— 典型就是「转发友站」和「站内标记」这类，见下面 `TAG_NEVER_FORWARD`
#    ③ 语义相反/危险的：**绝不勾**（如乌托邦的 `禁转`）
#
#    ★★ 用户点名**不勾**的（逐条）：
#       · `禁转`(1) / `首发`(2) —— **语义相反**：我们是**转载**过去的，
#         勾「禁转」= 禁止别人再转载（且这也正是 `opencd_map` 那句
#         「最危险的错误：把皇后的 1 当成乌托邦的 1」要防的事）；
#         勾「首发」= 谎称本站首发。**两者都是假数据。**
#       · `Redacted`(30) / `Orpheus`(36) / `Jpopsuki`(37) —— **转发友站的标签**，
#         是乌托邦标记"这条来自哪个友站"用的。我们来自 **OpenCD**，
#         勾这些等于**冒充别的站的来源**。
#       · `种子加速`(44) / `完成加速`(47) —— **站内运营标记**，不是内容属性，
#         由站点自己按运营规则打，**转载方不该自己勾**。
#       · `latin`(33) —— 皇后三套标签体系里**都没有**这个东西（实测），
#         无来源 ⇒ 不勾。
#
#    ★ 用户点名**该勾**的：`OpenCD`(31) —— 它标记"这条来自 OpenCD"，
#      正是我们这批种子的**事实来源**。★ 也正因如此，
#      `detect_tags()` 的第一条判据就是 `"OPENCD" in upper`。
#
#    ★ 为什么用**黑名单**而不是白名单：
#      ① 白名单会让"皇后新增了标签、映射表还没跟"变成**静默丢标签**
#         （正是本次要修的病的形状）；
#      ② 黑名单的失效方向是**多勾**，而多勾会在回读校验里**看得见**
#         （`publish.py` 有 tags 回读比对）⇒ 失效可见 > 失效静默。
#      ★ 黑名单不是"什么都不管"：它**只**排除上面这些**有明确理由**的，
#        其余仍走 `map_tags` 的**名字映射**（映不上照样进 `unmapped`，不猜）。
TAG_NEVER_FORWARD = {
    "禁转": "语义相反：我们是转载方，勾「禁转」是假数据",
    "首发": "语义相反：我们是转载方，勾「首发」是谎称本站首发",
    "Redacted": "转发友站标签：我们来自 OpenCD，不是 Redacted",
    "Orpheus": "转发友站标签：我们来自 OpenCD，不是 Orpheus",
    "Jpopsuki": "转发友站标签：我们来自 OpenCD，不是 Jpopsuki",
    "种子加速": "站内运营标记，由站点按运营规则打，转载方不自勾",
    "完成加速": "站内运营标记，由站点按运营规则打，转载方不自勾",
    "latin": "皇后三套标签体系里都没有此项（实测），无来源",
}


def filter_forward_tags(values, labels=None):
    """
    从要发出去的 tag value 列表里**剔除** `TAG_NEVER_FORWARD` 的项。

    `values` 是 **Musopia 的 tag value 字符串**列表（如 `['6','10']`）。
    返回 `(kept, dropped)`，`dropped` 是 `[(value, 名字, 理由), ...]`
    —— ★ **显式返回被剔除的**，不静默丢（同 `map_tags` 的 unmapped 契约）。

    ★ 判据：`filter_forward_tags(['6','10','1','2','30','31','44','47'])`
      → kept 含 `31`（OpenCD）+ `6`/`10`（港台/流行），
        dropped 含 `1`(禁转)/`2`(首发)/`30`(Redacted)/`44`/`47`。
    """
    labels = labels if labels is not None else TAGS_LABELS
    kept, dropped = [], []
    for v in values or []:
        v = str(v)
        name = _name_of_value(v, labels)
        if name in TAG_NEVER_FORWARD:
            dropped.append((v, name, TAG_NEVER_FORWARD[name]))
            continue
        if v not in kept:
            kept.append(v)
    return kept, dropped


def _name_of_value(value, labels=None):
    """tag value → 我们的短名（如 `'31'` → `'OpenCD'`）。查不到回 `''`。"""
    labels = labels if labels is not None else TAGS_LABELS
    for short, v in TAGS.items():
        if v == str(value):
            return short
    return ""


# 简介里日志过长会把页面撑爆，超过这个长度就折叠/截断
LOG_MAX_CHARS = 12000

# 曲目列表同理
TRACKLIST_MAX_CHARS = 8000


def _truncate(text, limit, note="内容过长已截断"):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... [{note}，原始长度 {len(text)} 字符]"


# ==================== 推断站点下拉项 ====================

def detect_audiocodec(*sources):
    """
    从标题/文件名里判断音频编码，返回 Musopia `audiocodec_sel` 的编号。

    ★★ `WAV` 是**已知缺口，不是疏忽**（2026-09-21 实测 + 查证）：
      · 原先关键词表只有 `FLAC/APE/DTS/AAC/OGG/MP3`，**没有 `WAV`**
        ⇒ `… 2003 - WAV 整轨 - OpenCD` 一律得到 `"0"`（= 不指定）。
        而 2026-09-19 发布的那 5 条里 **4 条是 WAV** ⇒ 编码这格**系统性为空**。
      · 参考实现 `repos/auto_feed_js/auto_feed.user.js:2561` **显式认 WAV**
        （`result.match(/(WAV)/i) ⇒ 'WAV'`）⇒ WAV 确实该认。
      · ★ **但「认出来」不等于「知道 Musopia 的编号」** —— 它是影音站的
        字符串标签（`'WAV'`），本项目要的是 `audiocodec_sel[4]` 的**数字编号**，
        这张表（`descr.py:15` 实测自 `edit.php`）里**没有 WAV**，
        参考项目也**只给到字符串、没给 Musopia 的号**。
      ⇒ **拿不到编号就不编一个**（同 `opencd_map.map_audiocodec` 的纪律：
        「取不到就返回 0，**不猜一个相近的**」）。
        猜错的后果比留空更糟：站点上会写一个**错误的编码**，
        而回读校验看不出来（它只比对自己发出去的值）。

    ★ 修复动作**刻意分两步**（2026-09-21）：
      第一步（本次）：把 WAV **识别出来**但**仍返回 `"0"`**，并在返回值旁
        给出显式标记（见 `AUDIOCODEC_UNKNOWN_OK`）—— 这样「知道它是 WAV
        但不知道编号」这件事**在代码里看得见**，不再沉默地退化成"没这回事"。
      第二步（待 `audiocodec_sel` 的真实 `<option>` 值确认后）：填进 `AUDIOCODEC`。
        ★ 验收判据届时补：`detect_audiocodec("…WAV…")` == 那个实编号。
    """
    blob = " ".join(str(s or "") for s in sources).upper()
    for key in ("FLAC", "APE", "DTS", "AAC", "OGG"):
        if key in blob:
            return AUDIOCODEC[key]
    if re.search(r"\bMP3\b|320K|V0", blob):
        return AUDIOCODEC["MP3"]
    # ★ WAV：**已识别，但编号未知** ⇒ 仍返回 "0"（不指定）。
    #   这条分支存在的唯一目的是**让缺口可见**（否则它和"没认出来"长得一模一样）。
    if re.search(r"\bWAV\b", blob):
        return "0"
    return "0"


# ★ 供判据/巡检用：这些格式**认得出但表里没有编号**（当前只有 WAV）。
#   用途：让「系统性为空」这件事能被脚本查出来，而不是靠人记得。
#   判据示例：detect_audiocodec("x - WAV") == "0" 且 "WAV" in AUDIOCODEC_UNKNOWN_OK
AUDIOCODEC_UNKNOWN_OK = ("WAV",)


def detect_medium(*sources):
    """
    **从名字猜**媒介（qB 本地抓轨路的兜底）。

    ★ 2026-09-21：本函数只该在**没有"页面已写明的媒介"**时被调用
      —— 见 `build_form_fields()` 里的优先级注释。
      ★ 末尾仍按 CD 兜底是有意的：这条路（qB 抓轨）**本来就是 CD 抓轨**，
        与皇后转载路不同（那条走 `opencd_map.map_medium()`，`Web` → `Other`）。
        ⇒ 两处的"默认"**刻意不一样**，因为它们的**来源不同**：
          · 这里：名字里没有线索，但**流程上就是 CD 抓轨** ⇒ 猜 CD 合理；
          · 那里：页面**明写** `Web` ⇒ 落 `Other`，**绝不猜 CD**。
    """
    blob = " ".join(str(s or "") for s in sources).upper()
    if "SACD" in blob:
        return MEDIUM["SACD"]
    if "DVD" in blob:
        return MEDIUM["DVD"]
    if "VCD" in blob:
        return MEDIUM["VCD"]
    # 抓轨来源默认按 CD 处理，这批任务全部来自 OpenCD 的 CD 抓轨
    return MEDIUM["CD"]


def detect_tags(*sources):
    """返回要勾选的 tag value 列表。保守起见只打有把握的标签。"""
    blob = " ".join(str(s or "") for s in sources)
    upper = blob.upper()
    picked = []

    if "OPENCD" in upper:
        picked.append(TAGS["OpenCD"])

    # 地区：靠字符集粗判，宁缺毋滥
    if re.search(r"[ぁ-んァ-ヶー]", blob):
        picked.append(TAGS["日韩"])
    elif re.search(r"[가-힣]", blob):
        picked.append(TAGS["日韩"])

    for kw, tag in (("古典", "古典"), ("CLASSICAL", "古典"), ("JAZZ", "爵士"),
                    ("爵士", "爵士"), ("OST", "原声"), ("SOUNDTRACK", "原声")):
        if kw in upper and TAGS[tag] not in picked:
            picked.append(TAGS[tag])

    return picked


# ==================== 副标题 ====================
#
#
#   ★★★ 我在这里犯过一个错，记下来防复发：
#     我曾断言「皇后详情页**没有**副标题字段」—— **那是错的**。
#     我当时只翻了**信息表格**的 12 行（`<td class="rowtitle">`），
#     而皇后的副标题**不在那张表里**，它在页面标题区：
#
#         <div class="title">上官萍 - 怀念经典-1 2017 - FLAC 分轨 - Yuekui</div>
#         <div class="smalltitle">WEB QQ [16Bit-44.1kHz] FLAC</div>
#                                 ^^^^^^^^^^ 这**就是**副标题
#
#     ⇒ `opencd_parse.parse_detail()` **早就把它读出来了**
#       （`info["small_descr"]` + `small_descr_present`，实测全部为 True）。
#     ⇒ 我却在 `descr.build_form_fields()` 里用**主标题**当副标题，
#       把解析层已经拿到的真值**扔掉了** —— 于是预览里主/副标题一模一样。
#       证据不足时只能说"我在这些地方没找到"，**不能断言"没有"**；
#       而本次更糟 —— 数据**本来就在手上**（解析层有），是我没去用它。
#
#   实测各页的 smalltitle（逐页读出，非推断）：
#     · `WEB QQ [16Bit-44.1kHz] FLAC`          （好好系列 / 上官萍）
#     · `WEB 网易 [24Bit-48kHz] FLAC`           （说唱巅峰对决）
#     · `... (Hi-Res 24bit 96kHz FLAC)`         （Beethoven）
#     · `YAMAHA CRW-F1E | 低速原抓 | Log100% | 高清扫描完整图册 |WAV|`（魂断乌江）
#     · 齐秦那条**本来就是空的**（`small_descr_present=False`）⇒ 留空，不硬造
#
#   ★ 两条路**分开**，不再共用同一个函数的行为：
#     · qB 本地抓轨路（**没有** `opencd_tid`）：`derive_small_descr()` 造「全 N 曲」——
#       那条路的主标题是我们自己拼的，行为不变。
#     · 皇后转载路（**有** `opencd_tid`）：`opencd_small_descr()` = **皇后副标题原文**。
#
#      `build_small_descr()`"）—— 那句**把 run.py 当成了只走抓轨路**，是错的：
#      `run.py` 的 scan 扫的是 qB 的 `opencd` 分类，里面**两种种子都有**
#      （既有本地抓轨成果，也有已经转载自皇后、正在做种的）⇒ 它**必须分流**。
#      ★ 把它当成"只走抓轨路"，正是 `scan` 每轮把皇后原文覆盖成「全 N 曲」的由来
#         否则这条注释会把人引回那个 bug）。⇒ 现在走 `scan_small_descr()`。

def opencd_small_descr(parsed_subtitle=""):
    """
    皇后转载路的副标题 = **皇后的 `<div class="smalltitle">` 原文**。

    ★ 取不到（皇后页面本来就没写副标题）就返回 `""`，**不硬造** ——
      实测「齐秦 - 我拿什么爱你」那条的 smalltitle 就是空的。
    ★ 判据：
      · `opencd_small_descr("WEB QQ [16Bit-44.1kHz] FLAC")` == 原样那串
      · `opencd_small_descr("")` == `""`
      · ★ 阴性对照：它**不等于**主标题（这条判据防的正是我犯过的错）
    """
    return (parsed_subtitle or "").strip()


def derive_small_descr(tracklist="", disc_count=None, fallback=""):
    """
    副标题只放主标题里没有的客观信息：曲目数（多碟再带碟数）。
    ★ 本函数供 **qB 本地抓轨那条路**（`run.py`）用，行为**未变**。
      皇后转载路的副标题走 `opencd_small_descr()`（= 皇后的 smalltitle 原文）。

    - 有效曲目 = 非空、且不以「【」开头的行（「【CD1】」这类是分段表头，不计数）。
    - disc_count 缺省时按 tracklist 里的「【」表头数推断，仍无则按 1 碟。
    - 数不出曲目（n==0，多见于历史分轨记录 tracklist 为空）就回退 fallback，不硬造。
    """
    lines = [l.strip() for l in (tracklist or "").splitlines() if l.strip()]
    header_count = sum(1 for l in lines if l.startswith("【"))
    tracks = [l for l in lines if not l.startswith("【")]
    n = len(tracks)
    if n == 0:
        return fallback or ""
    d = disc_count if disc_count else (header_count or 1)
    if d > 1:
        return f"{d}CD · 全 {n} 曲"
    return f"全 {n} 曲"


def build_small_descr(tracklist="", disc_count=1, fallback=""):
    """副标题对外入口（渲染时 build_form_fields 会再重算一次）。

    ★★ 2026-09-26：`run.py`（scan 阶段）**不再**调本函数 —— 见下面 `scan_small_descr`。
       本函数保留给"没有任何皇后信息、只知道曲目数"的调用方。
    """
    return derive_small_descr(tracklist, disc_count=disc_count, fallback=fallback)


def scan_small_descr(tracklist="", disc_count=None, fallback="", mapped_json=None,
                     is_opencd=False):
    """★ `run.py`（scan）算副标题的入口 —— 与 `build_small_descr_for_task` **同口径**。

    ═══════════════════════════════════════════════════════════════════════
    为什么需要第三个入口（2026-09-26 实测踩到， 的真根因）
    ═══════════════════════════════════════════════════════════════════════
    `scan` 每 30 分钟重扫 qB，**已发布的种子仍在 qB 里** ⇒ 它会把
    `small_descr` **重算并写回列**（`run.py` 的 `save_task(small_descr=…)`，
    两条分支都写：`status` 受保护时那一支、和一般那一支）。

    原先那行是 `descr.build_small_descr(tracklist, …)` = `derive_small_descr()`
    = **数曲目算「全 N 曲」**，它**不看 `opencd_tid`、不看 `mapped_json`**
    ⇒ **每轮都把皇后原文覆盖成「全 N 曲」**。

    ⇒ 症状：任何"一次性回写"的工具（`tools/recall_opencd_descr.py`）**跑完就被
      scan 盖回去**，而且**不是一次全退** —— scan 每轮只扫一批，被扫到的退回
      「全 N 曲」、没扫到的保留原文 ⇒ 反复出现「待改 N」而 N 每次都不同。
      ★ 这正是"看起来已经修好了"的静默复发。

    ═══════════════════════════════════════════════════════════════════════
    ★★ 为什么**不能**直接调 `build_small_descr_for_task(task)`（看着最省事）
    ═══════════════════════════════════════════════════════════════════════
    那个函数的皇后路是 `opencd_small_descr(task["small_descr"])` —— **读列**。
    它面向的调用方（`crabpt_site` / `update_metadata`）手里有一条**完整 task**
    （`merge_mapped` 已把 `mapped_json` 铺到顶层）⇒ 读列是对的，列**就是**已生效值。

    **但 `scan` 的处境相反**：它手里**没有**"已生效的列"，它**正要覆盖那一列**。
    在它这里读列 = 读到自己上一轮写的值（甚至读到已经被盖坏的「全 15 曲」），
    再写回去 ⇒ **固化错误**，而且是**假修**（改了代码、行为没变、不报错）。

    ⇒ 判据：**同一个"副标题"，在三个调用点的取值来源不同**（：
      引用一个数字之前先确认它**是**什么）：
        · `crabpt_site` / `update_metadata`：有完整 task ⇒ 读**列**（已生效值）
        · **`scan`：只有证据快照 ⇒ 读 `mapped_json.small_descr`**
    所以 `scan` 需要**它自己**的入口，而不是借用一个"读列"的函数。
    """
    # 皇后转载路：取**证据快照**里的原文（不是列的值）。
    if is_opencd and mapped_json:
        try:
            got = json.loads(mapped_json)
        except (ValueError, TypeError):
            got = None
        if isinstance(got, dict) and "small_descr" in got:
            # ★ 取不到（皇后本来就没写副标题）⇒ 空串，**留空不硬造**
            #   （与 `opencd_small_descr("")` 同一条纪律）。
            return opencd_small_descr(got.get("small_descr"))
        # ★ `mapped_json` 有值但**解析不出** `small_descr` ⇒ 落到「全 N 曲」。
        #   "来源为空"不能印成"映射成功"）。调用方会把这一情形打出来。
    return derive_small_descr(tracklist, disc_count=disc_count, fallback=fallback)


def parse_logs_json(raw_logs):
    """`logs_json` → `[{label, content, ...}]`；解析失败/空 → `None`。

    ★ 抽出来的理由（2026-09-24）：这段逻辑原先**只写在 `build_form_fields()` 里**，
      而新增的 `crabpt_site.build_fields()` 也需要同一份
      ⇒ 抄一遍就是两个口径，且**漂了不报错**。
    """
    if not raw_logs:
        return None
    try:
        parsed = json.loads(raw_logs)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, list) and parsed:
        return parsed
    return None


def build_small_descr_for_task(task):
    """按**一条 task** 算副标题 —— 两个发布站共用这一份口径。

    ★★ 这是一条**必须只写一次**的规则（2026-09-24 从 `build_form_fields()` 抽出）：

        · 皇后转载路（有 `opencd_tid`）→ `opencd_small_descr(task["small_descr"])`
          = 解析层从皇后页面 `<div class="smalltitle">` 读到的**原文**。
          ★ `task["small_descr"]` 就是它，**别再拿主标题顶替**（我犯过这个错）。
          ★ 皇后本来就没写副标题时（实测「齐秦」那条）→ 空串，**留空不硬造**。
        · qB 本地抓轨路 → `derive_small_descr()`「全 N 曲」。

    ★ 为什么抽成函数而不是让新站自己算：**副标题是两站都提交的同一个字段**，
      若各算一份，两站页面上的副标题可能**不一样** —— 那是没人要求过的差异，
      而且不会报错（两条链路各自"看着正常"）。
      ⇒ 判据：**凡是"两站取同一个值"的字段，只能在**一处**算。
    """
    is_opencd = bool(task.get("opencd_tid"))
    if is_opencd:
        return opencd_small_descr(task.get("small_descr"))
    logs = parse_logs_json(task.get("logs_json"))
    return derive_small_descr(
        task.get("tracklist") or "",
        disc_count=len(logs) if logs else None,
        fallback=task.get("small_descr") or "",
    )


# ==================== 主/副标题：猫站（PTerClub）口径 ====================
#
#   ★★ 这一节是**唯一**的标题/副标题口径实现。猫站、乌托邦、蟹黄堡三个发布
#      适配层都调这里（`pter_site.build_fields` / `build_form_fields` /
#      `crabpt_site.build_fields`）。三条链路各写一份 = 三个口径，漂了不报错。
#   ★ 为什么放在本模块而不是 `pter_site`：`descr` **只依赖 stdlib**，
#     三个适配层都能直接 import，不会把站点 client 依赖带进来，也不会成环。
RECOGNIZED_GROUPS = {
    "PTerMUSIC", "PTerWEB", "PTerMV", "PTerTV", "PTer",
    "CHDBits", "HDSky", "OurBits", "HDHome", "WiKi", "MTeam",
    "CHD", "CMCT", "FRDS", "beAst", "ADE", "PTH", "RedBits",
}

# 版本信息判据：只保留"这一版是什么"的描述（首版/德版/Deluxe…），
# 不把格式、日志、抓轨者这些**已经在主/副标题规格段里**的脏数据再拼一遍。
_VERSION_HINT = re.compile(
    r"(?:首版|版|Deluxe|Edition|Remaster(?:ed)?|Limited|Special|Anniversary|"
    r"Collector|Expanded|Complete|Box|Digipak|Digipack|\d+\s*(?:CD|Disc|碟))",
    re.I,
)

# 计算版本信息前先剥掉的"格式噪声"：这些不是版本，重复拼进去只会让副标题变脏。
_VERSION_NOISE = re.compile(
    r"(?:\[?\s*\d{1,2}\s*[Bb]it\s*[-/ ]?\s*\d+(?:\.\d+)?\s*k?[Hh]z\s*\]?"
    r"|\[?\s*\d+(?:\.\d+)?\s*k?[Hh]z\s*\]?"
    r"|\b(?:WEB(?:-DL)?|FLAC|WAV|APE|ALAC|DSD|DSF|DFF|MP3|AAC|Lossless|"
    r"Hi-?Res|Log\s*\d*%?|Self-?rip|CD|Cue)\b)",
    re.I,
)


def _version_note(raw, artist="", album=""):
    """从皇后 `<div class="smalltitle">` 原文里抽出版本信息（抽不到就返回 `""`）。

    ★ 保留的是**版本**（「台灣紙盒2CD首版」「德国版」「Deluxe Edition」），
      不是格式串（「WEB QQ [16Bit-44.1kHz] FLAC」「FLAC / Lossless / CD」）——
      后者已经在副标题的介质/规格段里，再拼一遍就是重复。
    ★ 抽不到就**留空，不硬造**（同全项目"取不到不编"的纪律）。
    """
    s = re.sub(r"[|｜]+", " / ", str(raw or ""))
    s = _VERSION_NOISE.sub(" ", s)
    s = re.sub(r"\s*/\s*(?:/|$)", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" /-·,，、;；.。")
    if not s or len(s) > 120:
        return ""
    if not _VERSION_HINT.search(s):
        return ""
    if artist and s == artist.strip():
        return ""
    if album and s == album.strip():
        return ""
    return s


def _normalize_artist(artist):
    """合辑统一写法（猫站规则：合辑用 Various Artists 或群星）。"""
    a = str(artist or "").strip()
    if a.upper() in ("VA", "V.A.", "VARIOUS ARTIST", "VARIOUS ARTISTS") or a == "群星":
        return "Various Artists"
    return a


def _split_title(raw_title):
    """剥掉制作组后缀、分轨/整轨、盘数标记，返回 `(cleaned_title, group_suffix)`。

    ★ 主标题与副标题共用这一份清洗逻辑 —— 否则会出现
      「主标题把 分轨 剥了、副标题却拿它拼专辑名」这种同页两口径。
    """
    group_suffix = ""
    cleaned_title = raw_title
    m_grp = re.search(r"-\s*([A-Za-z0-9_@]+)\s*$", raw_title)
    if m_grp:
        cand = m_grp.group(1)
        if cand in RECOGNIZED_GROUPS or cand.startswith("PTer"):
            group_suffix = f" - {cand}"
        cleaned_title = raw_title[:m_grp.start()].strip()

    cleaned_title = re.sub(
        r"[\s\-\_\(\)\[\]]*(?:分[轨軌]|整[轨軌])[\s\-\_\(\)\[\]]*",
        " ",
        cleaned_title,
        flags=re.I,
    ).strip()
    cleaned_title = re.sub(r"[\(\[\s]+\d+\s*(?:CD|DISC|DISCS)[\)\]\s]+",
                           " ", cleaned_title, flags=re.I).strip()
    return cleaned_title, group_suffix


def _parse_artist_album(task, cleaned_title):
    """取艺术家/专辑：优先库字段，缺失时从清洗后的标题解析。

    ★ 专辑名只剥「末尾独立年份」而不是「第一个年份」——
      否则 `… BEST OF 2000-2010~ 2010` 会在 2000 处被截断。
    """
    artist = _normalize_artist(task.get("artist"))
    album = str(task.get("album") or "").strip()

    if not (artist and album):
        parts = [p.strip() for p in cleaned_title.split(" - ") if p.strip()]
        if len(parts) >= 2:
            if not artist:
                artist = _normalize_artist(parts[0])
            album = parts[1]
            # 皇后 Raw 标题可能把发行信息塞在标题里（如 milet - Made of Glass
            # {2026.08.19} {Sony Music…}）：专辑名到第一个 `{` 为止。
            album = re.split(r"\s*\{", album)[0].strip()
            album = re.sub(
                r"\s*-\s*(?:FLAC|WAV|APE|DSD|DSF|DFF|ALAC|MP3|AAC)\b.*$",
                "", album, flags=re.I).strip()
            album = re.sub(r"\s+\b(?:19|20)\d{2}\b\s*$", "", album).strip()
        elif parts:
            artist = _normalize_artist(parts[0])
            album = parts[0]

    return artist, album


def normalize_music_title(task):
    """按猫站 PTerClub 官方规范标准化主标题。

    猫站规范（Wiki《上传规则（含标题命名及内容编辑指南）》音乐类）：
      艺术家英文名 - 专辑英文名 发行年份 - 文件格式 采样位深 采样频率 [- 制作组]
      示例：
        JJ Lin - Lost N Found 2011 - FLAC 16bit 44.1kHz - PTerMUSIC
        齐秦 - 我拿什么爱你 1998 - FLAC 16bit 44.1kHz
        Various Artists - 说唱巅峰对决2026 第九期 2026 - FLAC 16bit 44.1kHz

    硬性要求：
      1. 绝不允许带有 分轨/整轨/分軌/整軌；
      2. 绝不允许带有非官方个人压制者后缀（如 - Yuekui、- alin13788）；
      3. 必须包含格式与采样规格（如 FLAC 16bit 44.1kHz）；
      4. 合辑统一使用 Various Artists（或群星）。

    ★ 三站共用：乌托邦/蟹黄堡发布的种子与猫站页面使用**同一串**标题。
    """
    raw_title = (task.get("formatted_title") or task.get("torrent_name") or "").strip()

    # 1-2. 剥制作组 / 分轨 / 盘数标记（与副标题共用同一份清洗逻辑）
    cleaned_title, group_suffix = _split_title(raw_title)

    # 3. 提取音频格式（严格按单词边界匹配，避免 Leaper 误匹配 APE）
    upper = (str(task.get("format") or "") + " " + raw_title).upper()
    m_fmt = re.search(r"\b(FLAC|WAV|APE|DSD|DSF|DFF|ALAC|MP3|AAC)\b", upper)
    fmt = m_fmt.group(1) if m_fmt else "FLAC"

    # 4. 提取或推断采样位深与采样频率
    #   ★ 把 `small_descr`（皇后 smalltitle，如「WEB 网易 [24Bit-48kHz] FLAC」）
    #     也纳入语料：24bit/48kHz 这类事实常常**只**写在副标题里，不纳入就会
    #     把 Hi-Res 误标成 16bit 44.1kHz（三站页面还会因此不一致）。
    text_corpus = " ".join(
        str(task.get(k) or "")
        for k in ("formatted_title", "torrent_name", "small_descr", "descr", "log_content")
    )
    m_bit = re.search(r"(\d{1,2})\s*bit", text_corpus, re.I)
    m_hz = re.search(r"(\d+(?:\.\d+)?)\s*k?Hz", text_corpus, re.I)
    m_log_hz = re.search(r"Sample rate\s*:\s*(\d+(?:\.\d+)?)", task.get("log_content") or "", re.I)
    m_log_bit = re.search(r"Bits per sample\s*:\s*(\d+)", task.get("log_content") or "", re.I)

    if m_log_hz:
        val = float(m_log_hz.group(1))
        if val in (44100, 44.1):
            sample_rate = "44.1kHz"
        elif val in (48000, 48):
            sample_rate = "48kHz"
        elif val in (88200, 88.2):
            sample_rate = "88.2kHz"
        elif val in (96000, 96):
            sample_rate = "96kHz"
        elif val in (192000, 192):
            sample_rate = "192kHz"
        else:
            sample_rate = f"{val}kHz"
    elif m_hz:
        hz_val = m_hz.group(1)
        try:
            f_val = float(hz_val)
            if f_val in (44100.0, 44.1):
                sample_rate = "44.1kHz"
            elif f_val in (48000.0, 48.0):
                sample_rate = "48kHz"
            elif f_val in (88200.0, 88.2):
                sample_rate = "88.2kHz"
            elif f_val in (96000.0, 96.0):
                sample_rate = "96kHz"
            elif f_val in (192000.0, 192.0):
                sample_rate = "192kHz"
            else:
                sample_rate = f"{hz_val}kHz" if not hz_val.lower().endswith("khz") else hz_val
        except ValueError:
            sample_rate = f"{hz_val}kHz" if not hz_val.lower().endswith("khz") else hz_val
    else:
        sample_rate = "44.1kHz"

    if m_log_bit:
        bit_depth = f"{m_log_bit.group(1)}bit"
    elif m_bit:
        bit_depth = f"{m_bit.group(1)}bit"
    elif re.search(r"24bit|24-bit|Hi-Res|HI-RES|SACD", text_corpus, re.I):
        bit_depth = "24bit"
        if sample_rate == "44.1kHz":
            sample_rate = "96kHz"
    else:
        bit_depth = "16bit"

    specs = f"{fmt} {bit_depth} {sample_rate}"

    # 5. 年份提取（标题里可能有 2000-2010 这类区间，取最后一个年份）
    year = ""
    raw_year = str(task.get("year") or "")
    m_y = re.search(r"\b((?:19|20)\d{2})\b", raw_year)
    if not m_y:
        all_years = re.findall(r"\b((?:19|20)\d{2})\b", cleaned_title)
        if all_years:
            year = all_years[-1]
    if m_y:
        year = m_y.group(1)

    # 6. 核心艺术家与专辑（与副标题共用同一份解析逻辑）
    artist, album = _parse_artist_album(task, cleaned_title)

    if artist and album:
        core = f"{artist} - {album}"
        if year and not re.search(r"\b" + year + r"\b", core):
            core = f"{core} {year}"
    else:
        core = re.sub(r"-\s*(?:FLAC|WAV|APE|DSD|ALAC|MP3)\b.*$", "", cleaned_title, flags=re.I).strip()
        if year and not re.search(r"\b" + year + r"\b", core):
            core = f"{core} {year}"

    final_title = f"{core} - {specs}{group_suffix}"
    final_title = re.sub(r"\s+", " ", final_title).strip()
    final_title = re.sub(r"\s+-\s+", " - ", final_title)
    return final_title


def build_music_small_descr(task):
    """按猫站规范组装合规副标题（必填字段）。

    猫站规范：
      艺术家本国名 - 专辑本国名 | 发行类别 | [厂牌 目录号] 介质 / 规格 [/ Log] [/ Cue] [/ 版本]
      示例：
        林俊杰 - 学不会 | 专辑 | Warner Music CD / Lossless / Log (100%) / Cue
        Taylor Swift - Lover | 专辑 | Republic Records / 602577928222 / Deluxe Edition

    ★ 三段式结构严格保留；版本信息（如「台灣紙盒2CD首版」）按用户要求
      追加在第三段末尾，且只保留版本语义、不拼格式噪声。
    ★ 三站共用：乌托邦/蟹黄堡与猫站使用同一串副标题。
    """
    raw = (task.get("formatted_title") or task.get("torrent_name") or "").strip()
    cleaned_title, _ = _split_title(raw)
    artist, album = _parse_artist_album(task, cleaned_title)
    if not artist:
        artist = "华语音乐"
    if not album:
        album = "经典专辑"

    # 发行类别只看**标题/标签**这类短字段；简介与皇后 smalltitle 里的
    # “Live / 现场”常是版本描述（如「Blu-ray + LIVE CD [2Discs]」），
    # 把它们算进来会把录音室专辑误判成现场专辑。
    cat_corpus = " ".join(
        str(task.get(k) or "")
        for k in ("tags", "source", "formatted_title", "track_type")
    )
    corpus = " ".join(
        str(task.get(k) or "")
        for k in ("tags", "source", "formatted_title", "small_descr", "descr")
    )

    if re.search(r"原声|OST|Soundtrack", cat_corpus, re.I):
        cat = "原声"
    elif re.search(r"单曲|Single", cat_corpus, re.I):
        cat = "单曲"
    elif re.search(r"\bEP\b", cat_corpus, re.I):
        cat = "EP"
    elif re.search(r"精选|Best|Greatest Hits|Collection", cat_corpus, re.I):
        cat = "精选"
    elif re.search(r"演唱会|Live|现场", cat_corpus, re.I):
        cat = "现场"
    elif re.search(r"戏剧|戏曲|相声", cat_corpus, re.I):
        cat = "戏曲"
    else:
        cat = "专辑"

    med = "CD"
    if re.search(r"SACD", corpus, re.I):
        med = "SACD"
    elif re.search(r"WEB|Web-DL|Digital", corpus, re.I):
        med = "WEB"
    elif re.search(r"Vinyl|黑胶", corpus, re.I):
        med = "Vinyl"

    qual = "Hi-Res" if re.search(r"24bit|Hi-Res|HI-RES", corpus, re.I) else "Lossless"

    extras = [f"{med} / {qual}"]

    log_content = task.get("log_content") or ""
    if log_content or task.get("logs_json"):
        if "100%" in str(task.get("small_descr") or "") or "100%" in log_content:
            extras.append("Log (100%)")
        else:
            extras.append("Log")

    track_type = str(task.get("track_type") or "")
    if ("cue" in corpus.lower() or re.search(r"分[轨軌]|整[轨軌]", corpus)
            or re.search(r"分[轨軌]|整[轨軌]", track_type)):
        extras.append("Cue")

    # 尝试从简介中提取唱片公司与编号（如 PolyGram 846 789-2）
    m_lbl = re.search(r'(?:唱片公司|厂牌|廠牌)[：:\s]+([A-Za-z0-9_\u4e00-\u9fa5]+)', corpus)
    m_catno = re.search(r'(?:唱片编号|目录号|目錄號|Cat(?:alog)?(?:[.\s]No)?)[：:\s]+([A-Za-z0-9\s\-]+)',
                        corpus, re.I)
    label_prefix = ""
    if m_lbl or m_catno:
        lbl_parts = []
        if m_lbl:
            lbl_parts.append(m_lbl.group(1).strip())
        if m_catno:
            lbl_parts.append(m_catno.group(1).strip())
        label_prefix = " ".join(lbl_parts) + " "

    extras[0] = f"{label_prefix}{extras[0]}"

    note = _version_note(task.get("small_descr"), artist=artist, album=album)
    if note:
        extras.append(note)

    return f"{artist} - {album} | {cat} | {' / '.join(extras)}"


# ==================== BBCode 简介 ====================

def extract_cover_url(descr_text):
    """
    从 BBCode 简介里提取第一张图片的 URL。

    用途：编辑任务时如果本地 cover_url 被抹空（run.py 历史缺陷：重做任务时
    重复上传封面，图床报 Duplicated upload 400，空值写回把 URL 覆盖掉了），
    可以先读站点 edit.php 的现值，从 descr 里把封面 URL 抠回来，避免
    takeedit 整表覆盖时把站点页面上已有的封面一起抹掉。
    """
    if not descr_text:
        return ""
    m = re.search(r"\[img\]\s*(https?://\S+?)\s*\[/img\]", str(descr_text), re.I)
    return m.group(1) if m else ""


def markdown_to_bbcode(text: str) -> str:
    """将 Markdown 语法转换为 NexusPHP 兼容的 BBCode。

    针对猫站（PTerClub）等纯 NexusPHP 站点：前端无法渲染 Markdown，未转义符号
    会直接暴露出裸字符并导致审核退回。
    支持语法：
      - 代码块与行内代码：```code``` -> [code]code[/code]，`code` -> [code]code[/code]
      - 图片与超链接：![alt](url) -> [img]url[/img]，[text](url) -> [url=url]text[/url]
      - 标题：# ~ ###### -> [b]...[/b]
      - 强调：**text** / __text__ -> [b]text[/b]，*text* -> [i]text[/i]
      - 删除线：~~text~~ -> [s]text[/s]
      - 引用块：> quote -> [quote]quote[/quote]
    """
    if not text:
        return ""
    s = str(text).replace("\r\n", "\n")

    # 1. 代码块与行内代码
    s = re.sub(r"\x60{3}(?:[a-zA-Z0-9_-]+)?\n([\s\S]*?)\x60{3}", r"[code]\1[/code]", s)
    s = re.sub(r"\x60([^\x60\n]+)\x60", r"[code]\1[/code]", s)

    # 2. 图片与超链接
    s = re.sub(r"!\[(.*?)\]\((https?://\S+?)\)", r"[img]\2[/img]", s)
    s = re.sub(r"\[(.*?)\]\((https?://\S+?)\)", r"[url=\2]\1[/url]", s)

    # 3. 标题（行首 1-6 个 #）
    s = re.sub(r"(?m)^#{1,6}\s+(.+?)\s*$", r"[b]\1[/b]", s)
    # 3b. 裸标题记号（只有 #、后面没有文字的行）——它不是合法 Markdown 标题，
    #     上面的转换接不住，会原样漏到页面上变成一个孤立的「#」
    #     （实测 908393 妹妹哼情歌：皇后正文里有一条孤立的 `#` 行，
    #     站点审核口径是「Markdown 改为 bbcode」⇒ 裸 # 同样算残留，整行删除）。
    s = re.sub(r"(?m)^#{1,6}\s*$\n?", "", s)

    # 4. 加粗与斜体
    s = re.sub(r"\*\*(.+?)\*\*", r"[b]\1[/b]", s)
    s = re.sub(r"__(.+?)__", r"[b]\1[/b]", s)
    s = re.sub(r"(?<!\*)\*(?!\*)([^\*\n]+?)(?<!\*)\*(?!\*)", r"[i]\1[/i]", s)

    # 5. 删除线
    s = re.sub(r"~~(.+?)~~", r"[s]\1[/s]", s)

    # 6. 引用块（按连续行聚合成单个 quote）
    def _repl_quote(m):
        lines = m.group(0).strip().split("\n")
        cleaned = [re.sub(r"^>\s?", "", line) for line in lines if line.strip() != ">"]
        content = "\n".join(cleaned).strip()
        if not content:
            return ""
        return "[quote]" + content + "[/quote]"
    s = re.sub(r"(?m)(?:^>[^\n]*$\n?)+", _repl_quote, s)
    s = re.sub(r"\[quote\]\s*\[/quote\]\n*", "", s)

    return s


def build_source_link(task):
    """转载来源声明（**四站共用**同一份文案，作为简介第一块 `[quote]`）。

    ★★ 为什么抽成公共函数（2026-11-10）：文案原先在 `pter_site` / `xingwan_site`
      各写一份，而 `descr.build_form_fields()`（乌托邦）与
      `crabpt_site.build_fields()`（蟹黄堡）**根本没传** `source_link`
      ⇒ 这两站页面上**没有**转载引用块，且不会报任何错。
      用户指出「需要转载引用块的呀」⇒ 补齐，并把文案收敛到一处，
      避免下次再加站点又漏一份。
    ★ 优先带 OpenCD 种子 id（可点回原页）；没有 id 时退回种子名，
      两者都没有也至少给出「转自OpenCD」，**绝不返回空**（空 = 又静默丢引用）。
    """
    tid = (task or {}).get("opencd_tid")
    if tid:
        return f"转自OpenCD: https://open.cd/details.php?id={tid}"
    name = (task or {}).get("torrent_name") or (task or {}).get("formatted_title") or ""
    return f"转自OpenCD: {name}" if name else "转自OpenCD"


def build_descr(cover_url="", tracklist="", log_content="", log_filename="",
                logs=None, extra_note=None, opencd_descr="", site=None,
                source_link=""):
    """
    生成种子页面 BBCode 简介。

    ★★ 结构（2026-09-26 改，用户拍板「B 并存」；2026-11-10 改「来源声明置顶」）：
        ①a 资源来源引用   —— ★★ **置顶**（`source_link`，[quote]转自OpenCD...[/quote]）
        ① 封面图          —— **我们的**（`cover_url`，图链）
        ② 皇后简介正文    —— 皇后 `divdescr` 原文（`opencd_descr`，**已剥图**）
        ③ 曲目列表        —— ★ **皇后原版**优先，退回我们的
        ④ 抓轨日志（折叠）—— **保留**，多碟每碟一块（★ 蟹黄堡不要，猫站用 [hide=Log]）
    末尾不追加任何程序署名兜底（旧版会写"由自动化发布程序生成。"）。

    ★★ `source_link` 的位置（2026-11-10 用户拍板：「简介里面的引用应改置顶」）：
      四站**一律**排在最前（在封面图之上）。原先只有猫站置顶、其余站点在封面下方。

    ── `site` 参数（2026-09-27 加，用户拍板）─────────────────────────────
    ★★ **蟹黄堡不要 ④ 那个折叠日志块**。用户原话：「折叠代码也不需要」。
    ★★ **猫站要求 ④ 使用 [hide=Log] 折叠**。规则：「Log 与频谱图两者至少包含一项，优先推荐 Log；使用 hide 代码完成引用」。

    ★ 为什么用参数而不是"直接删掉 ④"：乌托邦**一直有**这一块，删掉会改变
      已在站上的 254 条的页面形态 —— 那是**没人要求过的改动**。用户要的是
      "**蟹黄堡**不需要"，不是"两站都不要"。⇒ 分叉点必须显式落在参数上。

    ★ 为什么默认 `None`（= 保留日志）而不是 `"musopia"`：
      `None` 是"**调用方没声明哪个站**" ⇒ 按**既有行为**（保留）走。
      这与  那条"缺键时的默认值必须中性"同一个理由 ——
      老调用点（`descr.py` 自己的 `build_form_fields`、自检）一个字不改，
      行为就不该变。★ 反过来若默认成"不保留"，所有老调用点会**静默**换行为。

    ★★ 判据（可执行，见本文件自检 ⑨）：同一个 `logs=` 输入下
        `site="crabpt"` 的产出里**不许**出现 `[spoiler=`/`[code]`，
        而 `site=None` 与 `site="musopia"` 必须**逐字节相同**且**含**折叠块。
      ⇒ 三者要**分别**断言。

    专辑名称/副标题/原始名称不写进简介：站点页面顶部的表格已经展示了主标题、
    副标题和种子文件名，简介里再重复一遍纯属冗余。

    ── ② 皇后简介正文（2026-09-26 加）────────────────────────────────────
    ★ 为什么加：用户要"站上的简介和皇后一致"。原先站上**只有**我们现渲染的
      封面+曲目+日志，皇后的正文（乐评/档案/厂牌信息）**从未上过站**。
    ★ 为什么是"并存"而不是"替换"：皇后的 `divdescr` 里**没有**我们的封面图和
      日志折叠块，直接替换会把这两样丢掉（页面反而更难用）⇒ 保留①④、插入②。
    ★ 空值语义：
      `opencd_descr` 为空 ⇒ **整块不出现**，而不是渲染一个空段落。
      两 种 空 要分清：① 皇后那条本来就没写正文；② 我们没抓到页面
      （389 条无 `mapped_json` 的行）—— 渲染层**不需要**分清，两者都该"不显示"；
      要分清是**入库/巡检**的事（那时看 `mapped_json` 有没有）。

    ── ③ 曲目列表（2026-09-26 改：优先皇后原版）────────────────────────
    ★★ 起因：我们的 `tracklist` **列**是"残废版"。实测 tid=199947：
        列    : "Can't Hold Me"                      ← 时长、feat. 全丢
        快照  : "1. Can't Hold Me (02:37)"           ← 完整
        皇后原: "1. Can't Hold Me (02:37) …"         ← 完整（divtracklist）
      ⇒ 曲目列表**该用皇后原版**。而调用方传进来的 `tracklist` 已经是
        `merge_mapped()` 提升过的值（有快照就是皇后原版、没有就是列）
        ⇒ 本函数**不用再做一次分流**，用谁由 `merge_mapped` 决定
        。
      ★ 所以**没有**新开关：`tracklist` 参数的含义"曲目列表文本"不变，
        变的是**上游给它的值**（快照优先）。

    日志用 NexusPHP 折叠代码 [spoiler=标签][code]…[/code][/spoiler]，页面不会被
    整段抓轨日志撑爆。多 CD 时每碟一块：
        logs = [{"label":"CD1 LOG","content":..}, {"label":"CD2 LOG","content":..}]
    单 CD 传 log_content/log_filename 即可（内部包成一条 label="LOG"）。
    """
    # ★ 蟹黄堡：**整块 ④ 都不要**（连 `[spoiler]` 壳带正文一起）。
    #   判据见上面 `site` 参数的 docstring（三条分别断言）。
    _skip_logs = (site == "crabpt")

    blocks = []

    # ①a 资源来源声明 —— ★★ **一律置顶**（2026-11-10 用户拍板）
    #   起因：用户看过星湾页面后要求「简介里面的引用应改置顶」。
    #   原先只有猫站置顶（其规则要求声明排在最前），其余站点（星湾等）
    #   贴在封面图**下方**。⇒ 现四站统一：只要 `source_link` 非空就排第一块，
    #   在封面图之上。⇒ 不再有 ①b 那个"封面下方的引用"分支。
    #   ★ 为什么改成无条件而不是"再加一个站点白名单"：来源声明是**转载合规**信息，
    #     位置"越显眼越好"这个诉求对所有站点都成立，没有站点要求它靠后
    #     ⇒ 站点白名单只会变成下一个"漏加一站就静默错位"的地方。
    if source_link and str(source_link).strip():
        blocks.append(f"[quote]{source_link.strip()}[/quote]")

    # ① 封面图 —— **我们的**图链（皇后正文里的图已在解析层剥掉）
    if cover_url and str(cover_url).strip():
        blocks.append(f"[center][img]{cover_url.strip()}[/img][/center]")

    # ② 皇后简介正文 —— 原文贴入（猫站经 markdown_to_bbcode 清洗保真）
    if opencd_descr and str(opencd_descr).strip():
        body = (str(opencd_descr).strip()
                .replace("[/quote]", "［/quote］"))
        if site == "pter":
            body = markdown_to_bbcode(body)
        blocks.append(body)

    if tracklist and str(tracklist).strip():
        body = _truncate(tracklist, TRACKLIST_MAX_CHARS, "曲目列表过长已截断")
        if site == "pter":
            body = markdown_to_bbcode(body)
        blocks.append(f"[quote][b]曲目列表：[/b]\n{body}[/quote]")

    # ④ 抓轨日志（乌托邦保留；蟹黄堡整块去掉；猫站使用 [hide=Log] 折叠）
    items = []
    if not _skip_logs:
        items = list(logs) if logs else []
        if not items and log_content and str(log_content).strip():
            items = [{"label": "LOG", "content": log_content, "filename": log_filename}]

    for item in items:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not (content and str(content).strip()):
            continue
        label = item.get("label") or "LOG"
        body = _truncate(content, LOG_MAX_CHARS, "日志过长已截断")
        # 兜底：日志里若真出现 [/code]/[/spoiler]/[/hide] 字面量会提前截断折叠块，替换成全角
        body = (body.replace("[/code]", "［/code］")
                    .replace("[/spoiler]", "［/spoiler］")
                    .replace("[/hide]", "［/hide］"))
        if site == "pter":
            hide_label = label if label and label.strip() != "LOG" else "Log"
            blocks.append(f"[hide={hide_label}]\n{body}\n[/hide]")
        else:
            blocks.append(f"[spoiler={label}][code]{body}[/code][/spoiler]")

    # 不再追加任何程序署名/元数据复读（旧版会写"由自动化发布程序生成。"
    # 和"专辑名称/副标题/原始名称"），页面顶部表格已展示这些信息。
    if extra_note is not None:
        blocks.append(extra_note)

    return "\n\n".join(blocks)


def build_form_fields(task, *, anonymous=True, autodetect=True):
    """
    把一条 task（db 里的 dict）渲染成 takeedit.php / takeupload.php 的表单字段。
    返回普通 dict，调用方决定是拼 data= 还是 files= 提交。

    ★ 2026-10-31 纪律订正：全生态强制匿名发布（NexusPHP 标准表单字段 `uplver="yes"`）。
    `anonymous` 默认为 True。若显式传 False（如 update_metadata 仅维护字段且沿用站点现状），
    则不输出 `uplver` 字段。
    """

    # 主标题：三站统一按猫站口径（`normalize_music_title`）。
    #   原先是把 `formatted_title`（皇后原文，带「分轨」和个人压制者后缀）
    #   原样提交 ⇒ 乌托邦页面标题又长又脏，与猫站页面不一致。
    title = normalize_music_title(task)
    raw = task.get("torrent_name") or ""

    # 多碟日志存在 logs_json 里（run.py 写入）。解析失败就退回单条 log_content。
    logs = None
    raw_logs = task.get("logs_json")
    if raw_logs:
        try:
            parsed = json.loads(raw_logs)
            if isinstance(parsed, list) and parsed:
                logs = parsed
        except (ValueError, TypeError):
            logs = None

    tracklist = task.get("tracklist") or ""

    # 副标题：三站统一按猫站口径（`build_music_small_descr`）——三段式
    #   「艺术家 - 专辑 | 发行类别 | 介质 / 规格 / Log / Cue / 版本」。
    #   原先是皇后 smalltitle 原文（「WEB QQ [16Bit-44.1kHz] FLAC」之类）
    #   或「全 N 曲」，两种都不是猫站的三段式规范。
    small = build_music_small_descr(task)

    descr = build_descr(
        cover_url=task.get("cover_url") or "",
        tracklist=tracklist,
        log_content=task.get("log_content") or "",
        log_filename=task.get("log_filename") or "",
        logs=logs,
        #    `mapped_json.descr` 提升成顶层 `task["descr"]`（见 `db.MAPPED_TOP_KEYS`）。
        #    ★ 这里读的是**提升后**的值而不是列 `opencd_descr` —— 两者理论上同值，
        #      但**快照是权威**（`db.py` 的注释：快照 = "抓页面时做的那个决定"，
        #      列只是副本）。与 `small_descr` 走 `merge_mapped` 完全同一个理由。
        #    ★ 兜底：万一调用方没跑过 `merge_mapped`（如蟹黄堡适配层只传了列），
        #      退回列值 —— 否则皇后正文会**静默消失**（那是最难发现的一类 bug）。
        opencd_descr=task.get("descr") or task.get("opencd_descr") or "",
        # ★ 本函数是**乌托邦**那条路（它要拼 `custom_fields[4][N]`，
        #   而蟹黄堡**没有** custom_fields ⇒ 走 `crabpt_site.build_fields()`）。
        #   ⇒ 显式声明 `site`，不让行为依赖 `build_descr` 的默认值
        #     （默认是 None = 保留日志，恰好等于乌托邦的行为；但**写出来**
        #      比"靠默认值凑对"强 —— 默认值将来一变，这里会**静默**跟着变）。
        #   ★ 用**字面量**而不是 `db.SITE_MUSOPIA`：本模块**刻意不 import db**
        #     （它只要 stdlib，所以能脱离 .env 单独跑自检 —— 与 `opencd_rate.py`
        #      那条"零项目内依赖"是同一个纪律）。`db` 那个常量本身就等于
        #     两处各写一份要么共用、要么被判据钉住）。
        site="musopia",
        #   ★★ 2026-11-10 用户指出「乌托邦/蟹黄堡没有引用块」的**根因就在这行缺失**：
        #     `source_link` 默认空串 ⇒ `build_descr` 静默跳过引用块（不报错）。
        #     四站必须都传；文案统一由 `build_source_link()` 生成。
        source_link=build_source_link(task),
    )

    #    真实表单 —— 用户提供的 `opencdhtml/音乐乌托邦 __ 发布 - Powered by NexusPHP.html`，
    #    逐行读出的 `<td class="rowhead">标签</td>` + `<input/textarea name=...>`）：
    #
    #      [4][1] = 艺术家          input
    #      [4][2] = 发行年份 ★必填   input
    #      [4][3] = 抓轨日志 ★必填   **textarea**   ← 不是 input！
    #      [4][4] = 唱片厂牌         input
    #      [4][5] = 抓轨日志得分      input
    #
    #    ★★ **序号不是 1/2/3/4 的顺序** —— 这是本表存在的最大意义。
    #    原代码硬写 `1/2/3/4`，后果有两条，**都是静默的**：
    #      ① `[4][3]` 我们当成普通 input ⇒ 实际是**必填的抓轨日志 textarea**
    #      ② `[4][4]` 我们以为是「第四个自定义字段」⇒ 实际是**唱片厂牌**
    #      ③ `[4][5]`（抓轨日志得分）我们**根本没发这个 key**
    #    ⇒ ★★ 而且 `[4][2]` / `[4][3]` 都是**必填**，我们恰恰把 `[4][3]` 发空
    #      ⇒ 很可能正是「HTTP 200 + 空 body」的成因（站点拒绝但回空 body，
    #
    #    ★ 值来源：`artist` / `year` 由 `opencd_parse` 解析「藝術家名」/「發行年份」得来，
    #      实测样本给出 `'齐秦'` / `'1998年'`。
    #      ⚠️ `year` 带「年」字 —— 站点要不要纯数字**尚未确认**，先原样发，
    #        并在回读校验里逐字比对（不一致会当场报出来，不会静默）。
    fields = {
        "name": title,
        "small_descr": small,
        # ★★ `nfoaction` —— **必须留**，值只能是 `release` 或 `keep`
        #    （NexusPHP 的 `takeupload.php`：`nfoaction = $_POST['nfoaction']=='release' ?
        #     'release' : 'keep'`；我们不发就是 `keep`，等价）。
        #    ★ 为什么要有这一行：**对称性**。`musopia.edit_torrent()` 里有一份
        #      同样的 `fields.setdefault("nfoaction", "keep")`（那里的注释解释了原因：
        #      `takeedit.php` 是**整表覆盖**语义，不发就可能被当"未选择"处理掉已有 nfo）。
        #      发布路径原本**没有**这一行 ⇒ 同一个语义"发布时隐式提供、编辑时显式提供"，
        #      将来谁改编辑那份，会以为发布那份也管着 nfo —— 这是会静默漂移的形状。
        #    ★ 它**不在**乌托邦的表单里（`takeupload.php` 的 95 个 `name=` 里没有它），
        #      所以 `gen_field_table.py` 的对账会报"表单里没有这个 name" —— 这是已知的、
        #      有据可查的豁免，不是漏登记（见那张表的 `nfoaction` 行）。
        "nfoaction": "keep",
        # ★★ `color` / `font` / `size` —— **不在任何表单里，是残留**。
        #    一个都不叫这三个名字；`takeedit.php` 读回来的表单里也没有。
        #    `grep -rn` 全仓库，除了这里这一处赋值**没有任何地方读它们**；
        #    git 历史可追到最初的 `419b6db`，当时是一整块从别处抄来的字段表，
        #    没有任何注释说明它们干什么。
        #    ⇒ 它们**不会出错**（NexusPHP 忽略未知 POST 键），但这正是本项目的纪律
        #      要防的东西：**不留看起来能跑的默认值** —— "发出去没事"会被读成"站点要它"。
        #    ⇒ 已删除。若将来实测它们真的有必要，那时候再加回来，并**当场写明理由**；
        #      在那之前，`gen_field_table.py` 第一节不再登记它们（登记了就是"声明了
        #      一个表单里没有的 key"，会变成常驻假警报，把真警报淹掉）。
        "descr": descr,
        "type": "408",
        "medium_sel[4]": "0",
        "audiocodec_sel[4]": "0",
        "team_sel[4]": "0",
        # ★ 键名里的数字是**站点的字段序号**，不是"第几个" —— 见上面那张表。
        "custom_fields[4][1]": "",   # 艺术家
        "custom_fields[4][2]": "",   # 发行年份（必填）
        "custom_fields[4][3]": "",   # 抓轨日志（必填，textarea）
        "custom_fields[4][4]": "",   # 唱片厂牌
        "custom_fields[4][5]": "",   # 抓轨日志得分
    }

    # ★ 艺术家 / 发行年份：有值就填。**取不到就留空，不编**（同全项目的纪律）。
    #   ★ 这两个字段**只从"页面已写明"的来源取**（`task` 里由解析层写入），
    #     不从标题里猜 —— 猜错年份/艺术家比留空更糟，而且回读校验看不出来
    #     （它只比对自己发出去的值）。
    artist = (task.get("artist") or "").strip()
    if artist:
        fields["custom_fields[4][1]"] = artist
    year = (task.get("year") or "").strip()
    if year:
        fields["custom_fields[4][2]"] = year

    # ★ 抓轨日志（`[4][3]`，textarea）与日志得分（`[4][5]`）。
    #   ⚠️ 这两格**与 `descr` 里的日志折叠块是两回事**：
    #     · `descr` 里的 `[spoiler=LOG][code]…` 是**简介正文**里给人看的；
    #     · `custom_fields[4][3]` 是站点自己的**日志字段**（必填），
    #       站点会拿它算「日志得分」并做 logcheck。
    #   ⇒ 必须两个都发。原先只发 `descr`，站点的日志字段是空的 —— 这与
    #     用户看到的「页面缺东西」是同一族的症状。
    #   ★ `rip_score`（日志得分）站点表单里是独立字段（`input name="rip_score"`），
    #     不是 `custom_fields[4][5]`；`[4][5]` 实测标签就是「抓轨日志得分」，
    log_for_field = (task.get("log_content") or "").strip()
    if log_for_field:
        fields["custom_fields[4][3]"] = log_for_field

    if autodetect:
        #    原先是 `fields["medium_sel[4]"] = detect_medium(title, raw)`，
        #    **无条件覆盖**。于是 `opencd_map.map_medium()` 算得再对也没用：
        #    皇后页面写 `媒介：Web` ⇒ `map_medium` 正解是 `Other(12)`，
        #    但紧跟着被 `detect_medium()` 从**标题**里猜成 `CD(8)` 覆盖掉。
        #    ★ 实测症状：三个 `Web` 页面预览里媒介显示 `CD` —— **暴露的就是这个**。
        #
        #    ★ 现在的规则（"页面已写明" > "从名字猜"，同标签那条纪律）：
        #      · `task["medium_sel"]`（`opencd_map.map_all()` 的产出，
        #        来自**皇后页面已写明的「媒介」**）**优先**；
        #      · 没有才 `detect_medium()` 从标题/文件名猜（qB 本地抓轨路）。
        #    ★ 注意 `"0"` 是**合法值**（= 请选择一项），所以判据是
        #      `is not None` 而不是真值判断 —— 否则 `"0"` 会被当成"没给"而退回猜。
        #
        #
        #      上面那条规则只解决了"快照有值 vs 猜"的优先级，**没解决"快照根本没这一格"**。
        #      而 `detect_medium()` 的末句是**无条件** `return MEDIUM["CD"]`
        #      —— 它从名字里找 SACD/DVD/VCD，**全找不到就回 CD**。对 qB 本地抓轨路
        #      那是**对的**（那条流程本来就只做 CD 抓轨，见 `detect_medium` 的注释）；
        #      但对**皇后路**是**错的**：皇后页面明写 `媒介：Web` 的种子会被判成 CD。
        #
        #      ★ 为什么"皇后路没有 `medium_sel` 键"= 不许猜：
        #        `medium_sel` 这个键**只由** `opencd_map.map_all()` 产出
        #        （`map_medium(info["medium"], info["format"])`，它才是"页面写明了什么"的正道）。
        #        皇后行的 `task["medium_sel"]` 为空 ⇒ 只有两种可能：
        #          ① 解析层这次没抓到「媒介」那一格，或
        #          ② 那份快照是**早期**入库的（那时的 `map_all` 还没有这个键）。
        #        两种都是**"不知道"** ⇒ 拿"不知道"去猜一个具体的 CD 就是
        #        ★ 而且这条**正好命中"网页下载"**：`Web`/`HI-RES` 恰恰是
        #          `detect_medium()` **认不出**（→ CD）而 `map_medium()` **认得**（→ Other）的那类。
        #
        #      ★ 判据（阴性对照）：造一条 `opencd_tid` 有值、`medium_sel` 为 None、
        #        标题里带 `Web` 的 task ⇒ 改前 `medium_sel[4] == "8"`（CD，错），
        #        改后应为 `"0"`（请选择，= 如实说"不知道"）。
        #      ★ 对 qB 本地抓轨路（`opencd_tid` 空）行为**完全不变** —— 那条支仍猜 CD。
        declared_medium = task.get("medium_sel")
        if declared_medium is not None:
            fields["medium_sel[4]"] = declared_medium
        elif task.get("opencd_tid"):
            # 皇后路 + 快照里没有这一格 ⇒ **留"请选择"，不猜**（见上）。
            fields["medium_sel[4]"] = "0"
            print("  ⚠️  皇后路快照里没有 `medium_sel` ⇒ 媒介留「请选择」"
                  "（**不拿 CD 顶替**；那会把页面写明的 Web/HI-RES 说成 CD）")
        else:
            # qB 本地抓轨路：本来就是 CD 抓轨 ⇒ 猜 CD 是对的（行为未变）。
            fields["medium_sel[4]"] = detect_medium(title, raw)

        # ★ 同理：`audiocodec_sel` 也**优先用页面已写明的**，猜是兜底。
        declared_codec = task.get("audiocodec_sel")
        if declared_codec is not None:
            fields["audiocodec_sel[4]"] = declared_codec
        else:
            fields["audiocodec_sel[4]"] = detect_audiocodec(title, raw)

        #    缺口本身：原先这一格**恒发 "0"**（请选择一项），即**从来没填过**。
        #    这不是"取不到"，是"没做" —— 皇后页面明确写着 `其他(Other)`，
        #    而乌托邦有 `Other = 5`，两边**对得上**。
        #    ★ 危险点：皇后 `其他(Other)` 恰好也是 **5**，但那是**巧合**
        #      ⇒ 必须按名字查（`opencd_map.map_team`），不能靠"值一样"。
        #    ★ 优先用 `task["team_sel"]`（`opencd_map.map_all()` 的产出，来自
        #      皇后页面的「製作組」字段）；取不到就**不动**（`_team_sel` 已在
        #      fields 里初始化为 "0"）。**不从标题猜**。
        declared_team = task.get("team_sel")
        if declared_team:
            fields["team_sel[4]"] = declared_team

        #    缺口本身：`detect_tags()` 早已写好、`TAGS` 全表也在，
        #    但 `build_form_fields()` **从不调用它**，form 里连 `tags` 开头的 key
        #    都没有 ⇒ 它是**死代码**（实测：form keys 里零个 `tags*`）。
        #
        #      · `task["tags"]` —— 由 `opencd_map.map_all()` 从**皇后页面的
        #        「標籤列表」字段**映射而来。那是**站点自己写的事实**（实测样本给出
        #        `['6','10']`，即 港台 + 流行）。**这是 OpenCD 路径的正道。**
        #      · `detect_tags()` —— 从**标题里猜**（只看 OpenCD 字样 / 日韩假名 /
        #        古典·JAZZ·OST 关键词）。本仓库原有路径（qB 本地抓轨）用它。
        #      ⇒ **绝不能只留后者**：实测把 OpenCD 标题喂给它，返回 **`[]`** ——
        #        因为 `map_all()` 产出的 title 是 `齐秦 - 我拿什么爱你 1998 - FLAC 分轨`
        #        （解析出来的、**不含 `OpenCD` 字样**），而 `detect_tags` 的第一条
        #        判据正是 `"OPENCD" in upper`。`[]` 与"没有标签"长得一模一样 ⇒
        #        **又是一次把"取不到"当"没有"**，而这正是本次要修的病的形状。
        #
        #    ★ 站点表单是 `input type=checkbox name="tags[4][]"` —— 注意 **`[]` 后缀**，
        #      它是**多值 key**。这里刻意给 **list** 而不是 join 成字符串：
        #        · `requests` 的 `data=` 走 `urlencode(..., doseq=True)`
        #          ⇒ list 展开成**多个同名键**（实测 `tags[4][]=6&tags[4][]=10`）；
        #        · 若 join 成 `"6,10"`，会发成**一个** `tags[4][]=6,10`
        #          ⇒ 站点把 `"6,10"` 当一个 value 解析 ⇒ **一个标签都选不中**。
        #    空 list 会被 `urlencode` 整个丢掉（不带该键）—— 那是想要的语义。
        #    ★ 判据见 `tools/render_compare.py`：最终 form 里必须**有** `tags[4][]`。
        #    ★ 阴性对照：把这行删掉，该判据必须当场报 MISS。
        #
        #       「皇后有」≠「该在乌托邦勾上」：禁转/首发（语义相反）、
        #       Redacted/Orpheus/Jpopsuki（转发友站）、种子加速/完成加速（站内运营）
        #       都要**剔除**，理由逐条写在 `TAG_NEVER_FORWARD` 上。
        #       ★ 剔除是**显式**的：返回的 dropped 会被打印出来，不静默丢。
        declared = task.get("tags")
        if declared:
            picked = list(declared)
        else:
            picked = detect_tags(title, raw)
        kept, dropped = filter_forward_tags(picked)
        if dropped:
            print("  ⏹️  转发白名单剔除 %d 个标签（不勾）:" % len(dropped))
            for v, nm, why in dropped:
                print("      %-10s(value=%s)  %s" % (nm, v, why))
        if kept:
            fields["tags[4][]"] = kept

        # ★★ `OpenCD` 标签：**来源标记**，只要这条种子确实来自 OpenCD 就勾上。
        #    ★ 判据是**"来源是不是 OpenCD"**，不是"标题里有没有 OpenCD 字样"
        #      —— `opencd_map.map_all()` 产出的标题**不含** `OpenCD`
        #      （如 `齐秦 - 我拿什么爱你 1998 - FLAC 分轨`），
        #      所以**不能靠 `detect_tags()` 的字符串判据**（那正是它返 [] 的原因）。
        #      这里认 `opencd_tid`：它由抓取阶段从皇后页面写入，是**可核的事实**。
        if task.get("opencd_tid"):
            ocd = TAGS["OpenCD"]
            cur = fields.get("tags[4][]") or []
            if ocd not in cur:
                fields["tags[4][]"] = list(cur) + [ocd]

    if anonymous:
        fields["uplver"] = "yes"

    return fields


# ==================== 自检（★ 防的是**我犯过的那类错**）====================
#
# ★ 为什么 descr.py 也需要自检：本轮最大的两个错**都出在这个文件**——
#   ① 用主标题当副标题（把解析层已经拿到的真值扔掉）；
#   ② 媒介子串误判（HQCD→CD 那条在 opencd_map，但同类）。
#   它们都**不会让程序崩**，只会让发出去的内容**悄悄是错的**。
#   ⇒ 必须用判据钉住，不能靠"看着像对"。

def _check():
    ok = fails = 0

    def chk(label, good, extra=""):
        nonlocal ok, fails
        ok, fails = ok + bool(good), fails + (not bool(good))
        print(f"  {'✅' if good else '❌'} {label}{('  ' + extra) if extra else ''}")

    print("① 副标题（皇后转载路）—— ★ 必须是**皇后的 smalltitle**，不是主标题")
    # ★ 用**真实页面实测到的**值当期望（不是从代码反填）
    REAL = [
        ("WEB QQ [16Bit-44.1kHz] FLAC", "老实测值"),
        ("WEB 网易 [24Bit-48kHz] FLAC", "说唱巅峰对决实测值"),
    ]
    for val, note in REAL:
        chk(f"原样保留 {val!r}（{note}）", opencd_small_descr(val) == val)
    chk("空 → 空（齐秦那条本来就没副标题，**不硬造**）",
        opencd_small_descr("") == "")
    chk("None → 空（不炸）", opencd_small_descr(None) == "")
    # ★★ 阴性对照 —— 这条**专门防我犯过的错**：
    #   若哪天有人（或我）又把 title 传进来当副标题，这条**必须**报错。
    TITLE = "上官萍 - 怀念经典-1 2017 - FLAC 分轨 - Yuekui"
    SUB = "WEB QQ [16Bit-44.1kHz] FLAC"
    chk("★ 阴性对照：副标题 **≠** 主标题",
        opencd_small_descr(SUB) != TITLE, "(防「拿主标题顶替」复发)")

    print("\n② qB 本地抓轨路（`derive_small_descr`）—— 行为**未变**")
    chk("12 行曲目 → 「全 12 曲」",
        derive_small_descr("\n".join(f"{i:02d}. x" for i in range(1, 13))) == "全 12 曲")
    chk("多碟带碟数 → 「2CD · 全 4 曲」",
        derive_small_descr("【CD1】\na\nb\n【CD2】\nc\nd") == "2CD · 全 4 曲")
    chk("数不出曲目 → 回退 fallback",
        derive_small_descr("", fallback="旧值") == "旧值")

    #    防的是这个**真的发生过**的事故：scan 每 30 分钟把皇后原文覆盖成「全 N 曲」。
    #    实测症状：`tools/recall_opencd_descr.py` 回写 56 条后，下一轮"待改 8"，
    #    且**每次数字都不同**（被扫到的退回、没扫到的保留原文）。
    print("\n④ scan 入口（`scan_small_descr`）—— ★ 防「每轮 scan 覆盖皇后原文」复发")
    _mj = json.dumps({"small_descr": "專輯 | VP Records / VPCD 1793", "title": "x"})
    chk("皇后路 → 取 mapped_json 的原文",
        scan_small_descr("01. a\n02. b", disc_count=1, mapped_json=_mj,
                         is_opencd=True) == "專輯 | VP Records / VPCD 1793")
    #    scan 绝不能退回归曲目数 —— 那正是被覆盖时的形态。
    chk("★ 阴性对照：**不等于**「全 2 曲」",
        scan_small_descr("01. a\n02. b", disc_count=1, mapped_json=_mj,
                         is_opencd=True) != "全 2 曲",
        "(防「scan 覆盖皇后原文」复发)")
    chk("皇后本来就没副标题 → 空，不硬造",
        scan_small_descr("01. a", mapped_json=json.dumps({"small_descr": ""}),
                         is_opencd=True) == "")
    chk("抓轨路（无 opencd_tid）→ 行为未变「全 2 曲」",
        scan_small_descr("01. a\n02. b", disc_count=1) == "全 2 曲")
    chk("皇后路但 snapshot 缺失 → 退回曲目数（调用方会印出来）",
        scan_small_descr("01. a\n02. b", disc_count=1, mapped_json=None,
                         is_opencd=True) == "全 2 曲")
    for _bad in (None, "", "not json", "[]", "123"):
        try:
            scan_small_descr("01. a", mapped_json=_bad, is_opencd=True)
            _r = True
        except Exception:
            _r = False
        chk(f"坏输入 mapped_json={_bad!r} 不抛异常", _r)
    # ★★ 这条是本节**最重要**的一条：它证明 `scan` **不能**借用
    #    `build_small_descr_for_task()` —— 那个函数读**列**，而 scan 正要覆盖该列
    #    ⇒ 会把「全 15 曲」这种已被盖坏的值**再写回去**（假修：改了代码行为没变）。
    _task = {"opencd_tid": "12345", "small_descr": "全 15 曲",
             "tracklist": "01. a\n02. b", "logs_json": None}
    chk("★ 阴性对照：scan 入口 ≠ build_small_descr_for_task（后者读坏列）",
        build_small_descr_for_task(_task) != scan_small_descr(
            "01. a\n02. b", disc_count=1, mapped_json=_mj, is_opencd=True),
        f"(for_task={build_small_descr_for_task(_task)!r} 就是那个假修)")

    print("\n③ 转发白名单 —— ★「皇后有」≠「该在乌托邦勾上」")
    kept, dropped = filter_forward_tags(["1", "2", "30", "31", "44", "6"])
    chk("禁转(1)/首发(2)/Redacted(30)/种子加速(44) 被剔除",
        set(v for v, _, _ in dropped) == {"1", "2", "30", "44"})
    chk("港台(6) 与 OpenCD(31) 保留", set(kept) == {"6", "31"})
    chk("阴性对照：全禁的一批 → 一个不剩",
        filter_forward_tags(["1", "2", "30"])[0] == [])

    print("\n④ 简介简介块（`build_descr` 的②）—— ★ 皇后正文 + 我们四件套并存")
    #    这个改动的风险全在"悄悄多出/少了什么"，不是"报不报错"。
    _COVER = "https://img.example/cover.jpg"
    _TL = "1. a (01:00)\n2. b (02:00)"
    _LOG = "EAC log\nline 2"
    _QDESCR = "專輯資料\nLabel: VP Records"

    _out = build_descr(cover_url=_COVER, tracklist=_TL, log_content=_LOG,
                       opencd_descr=_QDESCR)
    chk("皇后正文进了简介", _QDESCR in _out)
    chk("封面图仍在（★ 不是被替换掉）", f"[img]{_COVER}[/img]" in _out)
    chk("曲目块仍在", "曲目列表：" in _out and "1. a (01:00)" in _out)
    chk("日志折叠块仍在", "[spoiler=LOG][code]" in _out)
    chk("★ 顺序：封面 → 皇后正文 → 曲目 → 日志",
        _out.index("[img]") < _out.index(_QDESCR) < _out.index("曲目列表：")
        < _out.index("[spoiler=LOG]"))
    # ★ 阴性对照：正文为空时**不许**渲染出一个空段落/空行（389 条那种局面）
    _out0 = build_descr(cover_url=_COVER, tracklist=_TL, log_content=_LOG,
                        opencd_descr="")
    chk("★ 阴性对照：皇后正文为空 ⇒ 结构退回原样（不多空块）",
        _out0 == build_descr(cover_url=_COVER, tracklist=_TL, log_content=_LOG))
    chk("★ 阴性对照：空白串也算空（不渲染纯空白块）",
        "\n\n\n" not in build_descr(cover_url="", tracklist="",
                                   opencd_descr="   \n  "))
    # ★ `[/quote]` 中和：若不中和，它会提前闭合**下面**曲目块的引用框
    _inj = build_descr(cover_url="", tracklist=_TL,
                       opencd_descr="正文[/quote]后面还有话")
    chk("★ 皇后正文里的 [/quote] 被中和（不破坏结构）",
        "[/quote]" not in _inj.split("曲目列表")[0])

    # ══════════════════════════════════════════════════════════════════
    #
    #      失败"的断言不算判据）：
    #        · 只断言"crabpt 没有 [spoiler]" ⇒ 一个"整个函数返回空串"的
    #          坏实现也能通过（它当然没有 folded block）⇒ 必须**同时**断言
    #          封面/曲目**还在**。
    #        · 只断言"musopia 有 [spoiler]" ⇒ 测不到"crabpt 也保留了"这个回退。
    #        · 还要断言 `None` 与 `"musopia"` **逐字节相同** —— 这是"默认值
    #          必须中性"那条（老调用点一个字不改、行为就不该变）。
    #   ★ `_LOG`/`_TL`/`_COVER` 复用上面 ⑤ 的夹具（同一个输入比三种 site）。
    # ══════════════════════════════════════════════════════════════════
    _crab = build_descr(cover_url=_COVER, tracklist=_TL, log_content=_LOG,
                        site="crabpt")
    chk("⑨a 蟹黄堡：日志折叠块**不存在**（[spoiler= / [code] 都没有）",
        "[spoiler=" not in _crab and "[code]" not in _crab,
        f"实得 spoiler={'[spoiler=' in _crab} code={'[code]' in _crab}")
    # ★ 阴性对照①：**日志正文**不许换个形式漏进来（比如平铺成普通文本）
    chk("⑨b 蟹黄堡：日志**正文**也没漏进来（不是只去掉了壳）",
        "EAC log" not in _crab,
        f"实得含 'EAC log' = {'EAC log' in _crab}")
    chk("⑨c 蟹黄堡：封面 + 曲目**仍在**（不是『整段返回空』混过了 ⑨a）",
        f"[img]{_COVER}[/img]" in _crab and "曲目列表：" in _crab)

    _mus = build_descr(cover_url=_COVER, tracklist=_TL, log_content=_LOG,
                       site="musopia")
    chk("⑨d 乌托邦：日志折叠块**仍在**（★ 改动没有误伤老站）",
        "[spoiler=LOG][code]" in _mus)
    _none = build_descr(cover_url=_COVER, tracklist=_TL, log_content=_LOG)
    chk("⑨e `site=None`（没声明）与 `site='musopia'` **逐字节相同**"
        " ⇒ 默认值是中性的、老调用点行为不变",
        _none == _mus,
        "★ 若不同，说明默认值改变了既有行为（那会静默改掉 254 条已上站的页面）")
    # ★ 阴性对照②：**未知站点名**不许被当成"蟹黄堡"
    _unk = build_descr(cover_url=_COVER, tracklist=_TL, log_content=_LOG,
                       site="musopiaa")
    chk("⑨f 未知站点名 ⇒ 按保留走（**不**把拼错的站名当蟹黄堡）",
        "[spoiler=LOG][code]" in _unk,
        "★ 若这条失败，说明判据写成了 `site != 'musopia'`（错在'非乌托邦即蟹黄堡'）")
    # ★ `logs=[...]` 那条路（多碟）也要跟着关掉 —— 否则只在单条路上生效
    _multi = build_descr(logs=[{"label": "CD1 LOG", "content": "EAC log\nx"}],
                         site="crabpt")
    chk("⑨g 蟹黄堡：多碟 `logs=[...]` 那条路也关掉了",
        "[spoiler=" not in _multi and "EAC log" not in _multi,
        f"实得 {_multi!r}")

    # ══════════════════════════════════════════════════════════════════
    # ⑨h / ⑨i 猫站（PTerClub）：必须用 [hide=Log] 且支持 source_link
    # ══════════════════════════════════════════════════════════════════
    _pter = build_descr(cover_url=_COVER, tracklist=_TL, log_content=_LOG,
                        site="pter", source_link="转自OpenCD: https://open.cd/details.php?id=12345")
    chk("⑨h 猫站：日志使用 [hide=Log] 折叠（不用 [spoiler/code]）",
        "[hide=Log]" in _pter and "[/hide]" in _pter and "[spoiler=" not in _pter and "[code]" not in _pter)
    chk("⑨i 猫站：来源声明包含在 [quote] 中",
        "[quote]转自OpenCD: https://open.cd/details.php?id=12345[/quote]" in _pter)
    chk("⑨j 猫站：来源声明必须置顶（首行优先于封面图）",
        _pter.startswith("[quote]转自OpenCD: https://open.cd/details.php?id=12345[/quote]"))
    # ★★ 2026-11-10 用户拍板：「简介里面的引用应改置顶」⇒ **非猫站也要置顶**。
    #   阴性对照：无 `source_link` 时首块仍是封面图（不能凭空造引用块）。
    _xw = build_descr(cover_url=_COVER, tracklist=_TL, log_content=_LOG,
                      site="xingwan", source_link="转自OpenCD: https://open.cd/details.php?id=12345")
    chk("⑨l 星湾：来源声明同样置顶（首行优先于封面图）",
        _xw.startswith("[quote]转自OpenCD: https://open.cd/details.php?id=12345[/quote]")
        and _xw.index("[img]") > _xw.index("[quote]"))
    _nosrc = build_descr(cover_url=_COVER, tracklist=_TL, log_content=_LOG, site="xingwan")
    chk("⑨m 星湾：无 source_link 时首块仍是封面图（不凭空造引用）",
        _nosrc.startswith("[center][img]") and "[quote]转自OpenCD" not in _nosrc)
    _md_test = build_descr(opencd_descr="### 简介标题\n**歌手**：测试\n> 引用说明", site="pter")
    chk("⑨k 猫站：Markdown 标题与加粗转为 BBCode",
        "[b]简介标题[/b]" in _md_test and "[b]歌手[/b]" in _md_test and "###" not in _md_test)

    # ══════════════════════════════════════════════════════════════════
    # ⑩ 全生态强制匿名发布（NexusPHP 标准表单字段 uplver="yes"）
    # ══════════════════════════════════════════════════════════════════
    _sample_task = {"formatted_title": "Test Title", "torrent_name": "Test Torrent"}
    _fields_anon = build_form_fields(_sample_task)
    chk("⑩a build_form_fields 默认强制匿名（uplver='yes'）",
        _fields_anon.get("uplver") == "yes",
        f"实得 uplver={_fields_anon.get('uplver')!r}")
    chk("⑩b 废弃旧的 'anonymous' 键（绝不发 anonymous=1）",
        "anonymous" not in _fields_anon)
    _fields_named = build_form_fields(_sample_task, anonymous=False)
    chk("⑩c 显式传 anonymous=False 时不含 uplver 键（供 update_metadata 保留现状）",
        "uplver" not in _fields_named)

    # ══════════════════════════════════════════════════════════════════
    # ⑪ 转载引用块：四站都必须有，且置顶（2026-11-10 用户指出的回归）
    #   根因：`build_form_fields`（乌托邦）与 `crabpt_site.build_fields`（蟹黄堡）
    #   原先**没传** `source_link` ⇒ `build_descr` 静默跳过引用块。
    #   这里直接断言**真实字段产出**，而不是只测 `build_descr`。
    # ══════════════════════════════════════════════════════════════════
    _quote_task = {"formatted_title": "Test Title", "torrent_name": "Test Torrent",
                   "opencd_tid": 12345}
    _mus_fields = build_form_fields(_quote_task)
    chk("⑪a 乌托邦：真实提交的 descr 首块是转载引用",
        _mus_fields["descr"].startswith(
            "[quote]转自OpenCD: https://open.cd/details.php?id=12345[/quote]"),
        f"实得首 60 字：{_mus_fields['descr'][:60]!r}")
    chk("⑪b 引用块在封面图之上",
        _mus_fields["descr"].find("[img]") == -1
        or _mus_fields["descr"].index("[quote]") < _mus_fields["descr"].index("[img]"))
    _mus_noid = build_form_fields({"formatted_title": "Test Title"})
    chk("⑪c 乌托邦：无 opencd_tid 时用种子名兜底，仍不丢引用",
        _mus_noid["descr"].startswith("[quote]转自OpenCD: Test Title[/quote]"),
        f"实得首 60 字：{_mus_noid['descr'][:60]!r}")
    _crab = build_descr(cover_url=_COVER, tracklist=_TL, log_content=_LOG,
                        site="crabpt", source_link="转自OpenCD: https://open.cd/details.php?id=12345")
    chk("⑪d 蟹黄堡：引用块置顶且不含 LOG（原有站点差异保留）",
        _crab.startswith("[quote]") and "[spoiler=LOG]" not in _crab)

    return ok, fails


def main():
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="decr 渲染层离线自检")
    ap.add_argument("--selftest", action="store_true")
    # ★ --json：给 tools/gen_counts.py 读的机读出口。
    #   为什么不让它去正则解析上面的 ✅/❌ 人读行：各脚本的分母口径并不统一
    #   （这里是 ok+fails，opencd_parse 是 total_checked，opencd_rate 是 ok+fails），
    ap.add_argument("--json", action="store_true",
                    help="只把结果以 JSON 打到末行（给 gen_counts.py 读）")
    args = ap.parse_args()
    if not args.selftest:
        ap.error("加 --selftest 跑自检")
    if args.json:
        ok, fails = _check()
        print(json.dumps({"script": "descr.py --selftest", "ok": ok,
                          "total": ok + fails, "exit": 1 if fails else 0},
                         ensure_ascii=False))
        return 1 if fails else 0
    print("=" * 74)
    print("descr 离线自检（纯离线，零请求）")
    print("=" * 74)
    ok, fails = _check()
    print("\n" + "=" * 74)
    if fails:
        print(f"❌ 自检失败：{ok}/{ok + fails} 通过，{fails} 项不符")
        return 1
    print(f"✅ 自检通过：{ok}/{ok} 项全对")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

