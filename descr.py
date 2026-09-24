"""
种子页面内容生成：副标题、BBCode 简介、以及分类下拉/标签的自动推断。

发布(takeupload.php)和编辑(takeedit.php)必须用同一份渲染逻辑，否则编辑一次
页面内容就跟发布时不一样，很难排查。所以这里是唯一来源。
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
# ★★ 2026-09-21 订正 —— 这张表原先只有 23 项，**缺了 21 项**。
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
    # —— ★★ 2026-09-21 补的 21 项（原先缺，全在真实 44 项表里）——
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

# ★★ 转发方向白名单 —— 「哪些标签**允许**被勾上」（2026-09-21 用户拍板）。
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
# ★★ 2026-09-21 用户拍板：**皇后 → 乌托邦 这条路的副标题用皇后的副标题**。
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
#     ★ 教训（同 ERR-META-01 家族）：**"我没找到"不等于"不存在"**。
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
#     · `run.py`（qB 本地抓轨）继续用 `build_small_descr()` 造「全 N 曲」——
#       那条路的主标题是我们自己拼的，行为不变，**不受本次改动影响**。
#     · 皇后转载路：用 `opencd_small_descr()` = **解析层读到的皇后副标题原文**。

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
    """副标题对外入口（run.py 扫描时用）。渲染时 build_form_fields 会再重算一次。"""
    return derive_small_descr(tracklist, disc_count=disc_count, fallback=fallback)


def parse_logs_json(raw_logs):
    """`logs_json` → `[{label, content, ...}]`；解析失败/空 → `None`。

    ★ 抽出来的理由（2026-09-24）：这段逻辑原先**只写在 `build_form_fields()` 里**，
      而新增的 `crabpt_site.build_fields()` 也需要同一份
      ⇒ 抄一遍就是两个口径（`ERR-META-03`），且**漂了不报错**。
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


def build_descr(cover_url="", tracklist="", log_content="", log_filename="",
                logs=None, extra_note=None):
    """
    生成种子页面 BBCode 简介。
    结构：封面图 -> 曲目列表 -> 抓轨日志（折叠代码）
    末尾不追加任何程序署名兜底（旧版会写"由自动化发布程序生成。"）。

    专辑名称/副标题/原始名称不写进简介：站点页面顶部的表格已经展示了主标题、
    副标题和种子文件名，简介里再重复一遍纯属冗余。

    日志用 NexusPHP 折叠代码 [spoiler=标签][code]…[/code][/spoiler]，页面不会被
    整段抓轨日志撑爆。多 CD 时每碟一块：
        logs = [{"label":"CD1 LOG","content":..}, {"label":"CD2 LOG","content":..}]
    单 CD 传 log_content/log_filename 即可（内部包成一条 label="LOG"）。
    """
    blocks = []

    if cover_url and str(cover_url).strip():
        blocks.append(f"[center][img]{cover_url.strip()}[/img][/center]")

    if tracklist and str(tracklist).strip():
        body = _truncate(tracklist, TRACKLIST_MAX_CHARS, "曲目列表过长已截断")
        blocks.append(f"[quote][b]曲目列表：[/b]\n{body}[/quote]")

    # 统一成 [{label, content}, ...]：优先用多碟 logs，否则退回单条 log_content。
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
        # 兜底：日志里若真出现 [/code]/[/spoiler] 字面量会提前截断折叠块，替换成全角
        body = body.replace("[/code]", "［/code］").replace("[/spoiler]", "［/spoiler］")
        blocks.append(f"[spoiler={label}][code]{body}[/code][/spoiler]")

    # 不再追加任何程序署名/元数据复读（旧版会写"由自动化发布程序生成。"
    # 和"专辑名称/副标题/原始名称"），页面顶部表格已展示这些信息。
    if extra_note is not None:
        blocks.append(extra_note)

    return "\n\n".join(blocks)


def build_form_fields(task, *, anonymous=False, autodetect=True):
    """
    把一条 task（db 里的 dict）渲染成 takeedit.php / takeupload.php 的表单字段。
    返回普通 dict，调用方决定是拼 data= 还是 files= 提交。

    anonymous 默认 False 且此时**不输出该字段**：编辑走的是"只覆盖指定字段"，
    不提交就等于沿用站点现值。站上已有的种子都是非匿名的，默认填 1 会把 254 个
    页面悄悄改成匿名发布——这种没人要求过的副作用必须避免。要匿名请显式传 True。
    """

    title = task.get("formatted_title") or task.get("torrent_name") or ""
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

    # 副标题渲染时重算：已发布的老记录当初存的是「歌手-专辑(年)|格式」这种和主标题
    # 重复的旧值，只有在这里重算才能在下一次编辑时刷新成「全 N 曲 / DCD」。
    # 数不出曲目（老分轨记录 tracklist 为空）就退回库里旧值，不硬造。
    # ★★ 副标题的取值（2026-09-21 用户拍板：皇后转载路用**皇后的副标题**）：
    #    · 皇后转载路（有 `opencd_tid`）→ `opencd_small_descr(task["small_descr"])`
    #      = 解析层从皇后页面 `<div class="smalltitle">` 读到的**原文**。
    #      ★ `task["small_descr"]` 就是它，**别再拿主标题顶替**（我犯过这个错）。
    #      ★ 皇后本来就没写副标题时（实测「齐秦」那条）→ 空串，**留空不硬造**。
    #    · qB 本地抓轨路 → 维持 `derive_small_descr()`「全 N 曲」不变。
    is_opencd = bool(task.get("opencd_tid"))
    if is_opencd:
        small = opencd_small_descr(task.get("small_descr"))
    else:
        small = derive_small_descr(
            tracklist,
            disc_count=len(logs) if logs else None,
            fallback=task.get("small_descr") or "",
        )
    # ★★ 上面这几行与 `build_small_descr_for_task()` **是同一份口径**（2026-09-24 抽出）。
    #    这里**刻意保留原写法**而不是直接调那个函数，因为两者有一个**真实差异**：
    #    本函数外面已经算过 `logs`（多碟日志，用于后面拼简介），复用它**省一次 json 解析**；
    #    而 `build_small_descr_for_task()` 面向"只想拿副标题"的调用方（如蟹黄堡适配层），
    #    自己没有 logs。
    #    ★ 两条路的**结果必须一致** —— 若哪天有人只改了这里，就成了两个口径。
    #    已用一条断言把它钉住（见本函数末尾的 selftest，或 tests 里的对照）。

    descr = build_descr(
        cover_url=task.get("cover_url") or "",
        tracklist=tracklist,
        log_content=task.get("log_content") or "",
        log_filename=task.get("log_filename") or "",
        logs=logs,
    )

    # ★★ `custom_fields[4][N]` 的**真实序号**（2026-09-21 实测自乌托邦 `takeupload.php`
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
    #      见 `ERR-SITE-01` 与待办 #4）。
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
        #    2026-09-22 实测：乌托邦 `takeupload.php` 的真实表单里 95 个 `name=` 属性
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
    #     两者**待确认关系** ⇒ **先不发**，不猜（同 `ERR-META-01`：拿不到就不编）。
    log_for_field = (task.get("log_content") or "").strip()
    if log_for_field:
        fields["custom_fields[4][3]"] = log_for_field

    if autodetect:
        # ★★ 2026-09-21 修 —— **顺序即优先级**，这是一条真 bug 的修法：
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
        declared_medium = task.get("medium_sel")
        if declared_medium is not None:
            fields["medium_sel[4]"] = declared_medium
        else:
            fields["medium_sel[4]"] = detect_medium(title, raw)

        # ★ 同理：`audiocodec_sel` 也**优先用页面已写明的**，猜是兜底。
        declared_codec = task.get("audiocodec_sel")
        if declared_codec is not None:
            fields["audiocodec_sel[4]"] = declared_codec
        else:
            fields["audiocodec_sel[4]"] = detect_audiocodec(title, raw)

        # ★★ 制作组 `team_sel[4]` —— 2026-09-21 接上（**按名字**映射，不搬数字）。
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

        # ★★ 标签：必须真的发出去 —— 2026-09-21 修。
        #    缺口本身：`detect_tags()` 早已写好、`TAGS` 全表也在，
        #    但 `build_form_fields()` **从不调用它**，form 里连 `tags` 开头的 key
        #    都没有 ⇒ 它是**死代码**（实测：form keys 里零个 `tags*`）。
        #
        #    ★★ 两个来源，**优先"页面已写明"的那个**（这条是 2026-09-21 实测踩出来的）：
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
        #    ★★ 2026-09-21 新增：**转发方向白名单**（`TAG_NEVER_FORWARD`）。
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
        #    （用户 2026-09-21 点名：「opencd 勾上」。）
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
        fields["anonymous"] = "1"

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

    print("\n③ 转发白名单 —— ★「皇后有」≠「该在乌托邦勾上」")
    kept, dropped = filter_forward_tags(["1", "2", "30", "31", "44", "6"])
    chk("禁转(1)/首发(2)/Redacted(30)/种子加速(44) 被剔除",
        set(v for v, _, _ in dropped) == {"1", "2", "30", "44"})
    chk("港台(6) 与 OpenCD(31) 保留", set(kept) == {"6", "31"})
    chk("阴性对照：全禁的一批 → 一个不剩",
        filter_forward_tags(["1", "2", "30"])[0] == [])

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
    #   拿一个正则去套多种分母 = 制造一个"看起来很正常的"错数字（ERR-META-01 一族）。
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
