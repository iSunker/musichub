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
TAGS = {
    "禁转": "1", "首发": "2", "港台": "6", "大陆": "4", "欧美": "5",
    "日韩": "7", "其他地区": "8", "流行": "10", "OpenCD": "31", "Redacted": "30",
    "完成加速": "47", "种子加速": "44", "摇滚": "9", "爵士": "11", "电子": "12",
    "金属": "13", "古典": "15", "原声": "16", "发烧": "17", "民间": "23",
    "乡村": "24", "器乐": "34", "儿童": "35",
}

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

def derive_small_descr(tracklist="", disc_count=None, fallback=""):
    """
    副标题只放主标题里没有的客观信息：曲目数（多碟再带碟数）。
    主标题已是「歌手 - 专辑 年份 - 格式 整轨 - OpenCD」，副标题再抄一遍纯冗余。

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
    small = derive_small_descr(
        tracklist,
        disc_count=len(logs) if logs else None,
        fallback=task.get("small_descr") or "",
    )

    descr = build_descr(
        cover_url=task.get("cover_url") or "",
        tracklist=tracklist,
        log_content=task.get("log_content") or "",
        log_filename=task.get("log_filename") or "",
        logs=logs,
    )

    fields = {
        "name": title,
        "small_descr": small,
        "nfoaction": "keep",
        "color": "0",
        "font": "0",
        "size": "0",
        "descr": descr,
        "type": "408",
        "medium_sel[4]": "0",
        "audiocodec_sel[4]": "0",
        "team_sel[4]": "0",
        "custom_fields[4][1]": "",
        "custom_fields[4][2]": "",
        "custom_fields[4][3]": "",
        "custom_fields[4][4]": "",
    }

    if autodetect:
        fields["medium_sel[4]"] = detect_medium(title, raw)
        fields["audiocodec_sel[4]"] = detect_audiocodec(title, raw)

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
        declared = task.get("tags")
        if declared:
            fields["tags[4][]"] = list(declared)
        else:
            picked = detect_tags(title, raw)
            if picked:
                fields["tags[4][]"] = picked

    if anonymous:
        fields["anonymous"] = "1"

    return fields
