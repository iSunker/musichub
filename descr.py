"""
种子页面内容生成：副标题、BBCode 简介、以及分类下拉/标签的自动推断。

发布(takeupload.php)和编辑(takeedit.php)必须用同一份渲染逻辑，否则编辑一次
页面内容就跟发布时不一样，很难排查。所以这里是唯一来源。
"""
import re

# ==================== 站点字段字典（来自 edit.php 表单实测） ====================
# select name="medium_sel[4]"
MEDIUM = {"CD": "8", "SACD": "9", "DVD": "10", "VCD": "11", "OTHER": "12"}

# select name="audiocodec_sel[4]"
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
    """从标题/文件名里判断音频编码。"""
    blob = " ".join(str(s or "") for s in sources).upper()
    for key in ("FLAC", "APE", "DTS", "AAC", "OGG"):
        if key in blob:
            return AUDIOCODEC[key]
    if re.search(r"\bMP3\b|320K|V0", blob):
        return AUDIOCODEC["MP3"]
    return "0"


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

def build_small_descr(artist=None, album=None, year=None, fmt=None,
                      track_type=None, fallback=""):
    """
    副标题：`歌手 - 专辑 (年份) | 格式 整轨/分轨`
    任何一段缺失就自然省略，不留下 " -  () | " 这种残缺文本。
    """
    left = " - ".join(p for p in (artist, album) if p and str(p).strip())
    if year and str(year).strip():
        left = f"{left} ({year})" if left else f"({year})"

    right = " ".join(p for p in (fmt, track_type) if p and str(p).strip())

    parts = [p for p in (left, right) if p]
    result = " | ".join(parts)
    result = re.sub(r"\s+", " ", result).strip(" |-")
    return result or (fallback or "")


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


def build_descr(formatted_title="", torrent_name="", cover_url="", tracklist="",
                log_content="", log_filename="", small_descr="", extra_note=None):
    """
    生成种子页面 BBCode 简介。
    结构：封面图 -> 基本信息 -> 曲目列表 -> 抓轨日志 -> 署名
    """
    blocks = []

    if cover_url and str(cover_url).strip():
        blocks.append(f"[center][img]{cover_url.strip()}[/img][/center]")

    info_lines = []
    if formatted_title:
        info_lines.append(f"[b]专辑名称：[/b]{formatted_title}")
    if small_descr:
        info_lines.append(f"[b]副标题：[/b]{small_descr}")
    if torrent_name:
        info_lines.append(f"[b]原始名称：[/b]{torrent_name}")
    if info_lines:
        blocks.append("\n".join(info_lines))

    if tracklist and str(tracklist).strip():
        body = _truncate(tracklist, TRACKLIST_MAX_CHARS, "曲目列表过长已截断")
        blocks.append(f"[quote][b]曲目列表：[/b]\n{body}[/quote]")

    if log_content and str(log_content).strip():
        title = f"抓轨日志（{log_filename}）" if log_filename else "抓轨日志"
        body = _truncate(log_content, LOG_MAX_CHARS, "日志过长已截断")
        # 长日志放 quote 里，页面不至于被无限拉长
        blocks.append(f"[quote][b]{title}：[/b]\n{body}[/quote]")

    blocks.append(extra_note if extra_note is not None else "由自动化发布程序生成。")

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
    small = task.get("small_descr") or ""

    descr = build_descr(
        formatted_title=title,
        torrent_name=raw,
        cover_url=task.get("cover_url") or "",
        tracklist=task.get("tracklist") or "",
        log_content=task.get("log_content") or "",
        log_filename=task.get("log_filename") or "",
        small_descr=small,
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

    if anonymous:
        fields["anonymous"] = "1"

    return fields
