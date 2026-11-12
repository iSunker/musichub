# -*- coding: utf-8 -*-
"""麒麟（hdkyl.in）发布适配层 —— 把一条 task 渲染成 `takeupload.php` 的表单字段。

═══════════════════════════════════════════════════════════════════════════
为什么单独一个文件（与乌托邦 musopia.py、蟹黄堡 crabpt_site.py、猫站 pter_site.py、
星湾 xingwan_site.py 并列）
═══════════════════════════════════════════════════════════════════════════
★ 麒麟是 NexusPHP 站点，音乐分类 = 408（HQ Audio/音乐），但表单字段语义
  与另外四站都不同（**尤其 `processing_sel[4]`=年代、`source_sel[4]`=地区**，
  与蟹黄堡**同名但语义互换**），必须单独写一份映射表，不能改数字复用。

    字段                    麒麟取值
    --------------------    --------------------------------------------
    分类                     `type=408`
    年代                     `processing_sel[4]`（2026=12 … 2016=8，Earlier=9）★ 非线性
    媒介                     `medium_sel[4]`（CD=33 / Track=32 / WEB-DL=31 / Other=34）
    视频编码                 `codec_sel[4]`（音乐不填 ⇒ Other=5）
    音频编码                 `audiocodec_sel[4]`（FLAC=1 / WAV=13 / APE=12 / MP3=4 …）
    分辨率                   `standard_sel[4]`（音乐不填 ⇒ Other=9）
    地区                     `source_sel[4]`（CN=15 / HK=16 / TW=17 / US=18 …）★ 粗→细损失
    制作组                   `team_sel[4]`（官方组不能冒充 ⇒ Other=5）
    标签                     `tags[4][]`（国语=5 / LIVE现场=10 / Blu-ray=9）
    匿名                     `uplver=yes`
    简介                     BBCode（与 OpenCD 同源，无需 markdown 转换）

★ 标题用 `descr.build_kylin_title`（麒麟站规：`艺术家 - 专辑 年份 编码[-发布组][- OpenCD]`，
  不带采样位深/采样率；解析与清洗与其余四站同一份口径）；副标题沿用
  `descr.build_music_small_descr`。
★ 简介沿用 `descr.build_descr(site=db.SITE_HDKYLIN)`：通用 NexusPHP 形态
  （封面 + 转载来源 + 曲目 + 折叠日志）。

★★ 安全铁律：
    作为转载方（从 OpenCD 转载），**严禁勾选**禁转/首发/DIY/官种/驻站类标签；
    只勾「国语 / LIVE现场 / Blu-ray」等由元数据能证明的内容标签。
"""
import json
import os
import re
import time

import config
import net
import db
import descr

# 复用乌托邦的 UA / 错误提取 / 失败页落盘 —— 五个站同属 NexusPHP
from musopia import SiteError, UA, _extract_error, _dump_takeupload

# ==================== 分类 id ====================
MUSIC_TYPE = str(config.HDKYLIN_MUSIC_CATEGORY)          # 408

# ==================== `processing_sel[4]`：年代（★ 非线性映射，逐项实测）====================
_YEAR_BY_VALUE = {
    2026: "12", 2025: "11", 2024: "10",
    2023: "1", 2022: "2", 2021: "3", 2020: "4",
    2019: "5", 2018: "6", 2017: "7", 2016: "8",
}
_YEAR_EARLIER = "9"          # Earlier/更早（≤2015）
YEAR_UNSET = "0"             # 请选择一项


def map_year(task):
    """把 task 的年份映射到麒麟 `processing_sel[4]`（年代）。

    年份来源优先级：`year` 字段 → 标题/副标题正则。
    ★ 麒麟年代编号**非线性**（2026→12 但 2023→1），必须按实测表逐项查，
      不能拿「2026 - year + 1」这类公式推。
    ★ 拿不到年份 ⇒ 返回 `0`（请选择一项）—— 不猜一个年份（`ERR-META-01` 的纪律）。
    """
    year = None
    raw_year = (task or {}).get("year")
    m = re.search(r"\b(19|20)\d{2}\b", str(raw_year or ""))
    if m:
        year = int(m.group(0))
    if year is None:
        corpus = " ".join(str((task or {}).get(k) or "")
                          for k in ("formatted_title", "torrent_name", "small_descr"))
        m = re.search(r"\b(19|20)\d{2}\b", corpus)
        if m:
            year = int(m.group(0))
    if year is None:
        return YEAR_UNSET
    if year >= 2026:
        return _YEAR_BY_VALUE[2026]
    if year <= 2015:
        return _YEAR_EARLIER
    return _YEAR_BY_VALUE.get(year, YEAR_UNSET)


# ==================== `medium_sel[4]`：媒介 ====================
MEDIUM_CD = "33"
MEDIUM_TRACK = "32"          # Track / Soundtrack（原声 / OST）
MEDIUM_WEB = "31"            # WEB-DL（数字发行）
MEDIUM_OTHER = "34"


def map_medium(task):
    """把 task 的介质映射到麒麟 `medium_sel[4]`。

    判据优先看映射快照（`medium_sel` / `source` / `format`），再看标题与副标题。
    ★ 兜底取 **CD**（`33`）：本项目的目标就是无损**唱片**资源，元数据没写媒介时
      按 CD 处理；只有明确是原声/OST 或数字发行时才落 Track / WEB-DL。
    """
    corpus = " ".join(
        str((task or {}).get(k) or "")
        for k in ("medium_sel", "source", "medium", "format", "formatted_title",
                  "small_descr", "torrent_name", "tags")
    )
    if re.search(r"原声|OST|Soundtrack", corpus, re.I):
        return MEDIUM_TRACK
    if re.search(r"\bWEB(?:-DL)?\b|Digital|数字发行|流媒体", corpus, re.I):
        return MEDIUM_WEB
    return MEDIUM_CD


# ==================== `audiocodec_sel[4]`：音频编码（按名字转，非按编号）====================
#
# ★ 麒麟编号与乌托邦/蟹黄堡**都不同**（FLAC 麒麟=1、蟹黄堡=10），必须按**名字**转。
_AUDIOCODEC_BY_NAME = {
    "FLAC": "1", "WAV": "13", "APE": "12", "M4A": "14", "ALAC": "14",
    "MP3": "4", "AAC": "6", "DTS": "3", "LPCM": "10", "OPUS": "20",
    "OGG": "20", "MPEG": "18",
}
AUDIOCODEC_OTHER = "7"


def map_audiocodec(task):
    """把 task 的音频格式映射到麒麟 `audiocodec_sel[4]`。

    ★ 直接从**格式关键字**（`format` 字段 + 标题/副标题）判断，而不是从乌托邦
      `audiocodec_sel` 编号反查 —— 因为乌托邦那张表里 **WAV 的编号未实测**（`descr.py`
      `AUDIOCODEC_UNKNOWN_OK`），靠编号反查会漏掉 WAV（本项目一大半是 WAV）。
    ★ DSD/DSF/DFF 在麒麟没有对应项 ⇒ 落 `Other`（7），**不猜一个相近的**。
    ★ 认不出 ⇒ `Other`（7）—— `0` 是「请选择一项」，站点对必填项会拒收；`Other`
      是站点真有的选项，且「其它」是对「我们不知道」最诚实的表述。
    """
    corpus = " ".join(str((task or {}).get(k) or "")
                      for k in ("format", "formatted_title", "torrent_name",
                                "small_descr", "audiocodec_sel"))
    upper = corpus.upper()
    for key in ("FLAC", "WAV", "APE", "ALAC", "M4A", "MP3", "AAC", "DTS",
                "LPCM", "OPUS", "OGG", "MPEG"):
        if re.search(rf"\b{key}\b", upper):
            return _AUDIOCODEC_BY_NAME[key]
    return AUDIOCODEC_OTHER


# ==================== `source_sel[4]`：地区（★ 方案 B：粗→细取代表值）====================
#
# ★ 来源：皇后的**地区标签**（不是歌手国籍）。皇后标签经 `opencd_map` 映射成
#   乌托邦编号（`descr.TAGS`：大陆=4 / 港台=6 / 欧美=5 / 日韩=7 / 其他地区=8）。
# ★ 皇后「地区(Area)」实测只有 5 类：大陆 / 欧美 / 港台 / 日韩 / 其它地区；麒麟要分 18 国，
#   粒度细 3 倍且方向不可逆。2026-11-12 用户拍板 **方案 B**：拆不出的取代表值，
#   接受「约一半标错」的代价（详见 PLAN-hdkylin.md §3.5 / §6-4）。
# ★ 「大陆」是精确映射；港台/欧美/日韩落**代表值**，并在 audit 里显式标注会标错的情形。
_REGION_BY_TAG = {
    "大陆": "15",            # CN/中国 —— 精确
    "港台": "16",            # HK/香港 ★ 台湾的会标成香港
    "欧美": "28",            # EU/欧洲 ★ 美/英/法/德的会标成欧洲
    "日韩": "19",            # JPN/日本 ★ 韩国的会标成日本
}
REGION_OTHER = "14"          # Other/其他
# ★ 代表值的信息损失说明（写进 audit，不静默；不拦截提交）
_REGION_REPRESENTATIVE_NOTE = {
    "港台": "方案B 取代表值 香港(16)：皇后不分 HK/TW ⇒ 台湾的会标成香港",
    "欧美": "方案B 取代表值 欧洲(28)：皇后不分 US/EU/GB/FR/DE ⇒ 美/英的会标成欧洲",
    "日韩": "方案B 取代表值 日本(19)：皇后不分 JPN/Kr ⇒ 韩国的会标成日本",
}


def map_region(task):
    """乌托邦标签编号列表 → (麒麟 `source_sel[4]` 值, 说明或 None)。

    ★ 方案 B：能精确的精确（大陆→CN），拆不出的取代表值（港台→HK / 欧美→EU / 日韩→JPN），
      并在返回值第二项里给出「可能会标错」的显式说明。
    """
    notes = []
    tag_by_value = descr.TAGS
    values = [str(x) for x in _as_list((task or {}).get("tags"))]
    for v in values:
        name = None
        for k, val in tag_by_value.items():
            if str(val) == str(v):
                name = k
                break
        if name is None:
            continue
        if name in _REGION_BY_TAG:
            if name in _REGION_REPRESENTATIVE_NOTE:
                notes.append(_REGION_REPRESENTATIVE_NOTE[name])
            return _REGION_BY_TAG[name], ("；".join(notes) if notes else None)
    return REGION_OTHER, ("；".join(notes) if notes else None)


# ==================== 固定值（有「官方组不能冒充」的先例）====================
CODEC_OTHER = "5"            # 视频编码：音乐无视频编码
STANDARD_OTHER = "9"         # 分辨率：音乐无分辨率
TEAM_OTHER = "5"             # 制作组：HDK/HDKWeb/Kylin… 都是麒麟官方组，转载方不冒充


# ==================== `tags[4][]`：标签（白名单，禁转/首发/DIY/官种/驻站一律不勾）====================
TAG_GUOYU = "5"              # 国语
TAG_BLURAY = "9"             # Blu-ray
TAG_LIVE = "10"              # LIVE现场


def map_tags(task):
    """生成 `tags[4][]`：只勾由元数据能证明的内容标签，禁转/首发/DIY/官种/驻站**不勾**。

    ★ 麒麟音乐标签极少（无粤语/英语/日语），语言标签只有「国语」可勾。
    ★ 未映射的一律**不勾**（宁可少选，不可错选）。
    """
    tags = []
    corpus = " ".join(str((task or {}).get(k) or "")
                      for k in ("tags", "language", "formatted_title",
                                "torrent_name", "small_descr"))
    if re.search(r"国语|华语|中文|Mandarin", corpus, re.I):
        tags.append(TAG_GUOYU)
    if re.search(r"\bLIVE\b|现场", corpus, re.I):
        tags.append(TAG_LIVE)
    if re.search(r"Blu-?ray|蓝光|\bBD\b", corpus, re.I):
        tags.append(TAG_BLURAY)
    return sorted(set(tags), key=int)


def _as_list(value):
    """把 `tags` 字段统一成 list[str]（可能是 list / JSON 串 / 逗号串 / None）。"""
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(v) for v in data]
        except (ValueError, TypeError):
            pass
    return [x.strip() for x in re.split(r"[,，;；|]", text) if x.strip()]


def _logs(task):
    raw = (task or {}).get("logs_json")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, list) and parsed else None


def _descr_body(task, site):
    return descr.build_descr(
        cover_url=task.get("cover_url") or "",
        tracklist=task.get("tracklist") or "",
        log_content=task.get("log_content") or "",
        log_filename=task.get("log_filename") or "",
        logs=_logs(task),
        opencd_descr=task.get("descr") or task.get("opencd_descr") or "",
        site=site,
        source_link=descr.build_source_link(task),
    )


def build_fields(task, *, anonymous=True):
    """把一条 task 渲染成麒麟 `takeupload.php` 的表单字段。

    返回 `(fields, audit)`：
      · `fields` = 直接 `data=` 提交的普通 dict（多值的用 list）
      · `audit`  = 人读的字段来源说明（逐字段写「这个值从哪来」）
    """
    task = db.merge_mapped(dict(task))     # ★ 铺平 mapped_json，不改调用方的 dict
    audit = []

    title = descr.build_kylin_title(task)
    audit.append(f"name（主标题）← 麒麟站规（编码尾巴，无位深/采样率）：{title}")

    small_descr = descr.build_music_small_descr(task)
    audit.append(f"small_descr（副标题）← 猫站口径组装：{small_descr}")

    year = map_year(task)
    medium = map_medium(task)
    audio = map_audiocodec(task)
    region, region_note = map_region(task)
    tags = map_tags(task)

    fields = {
        "type": MUSIC_TYPE,
        "name": title,
        "small_descr": small_descr,
        "descr": _descr_body(task, db.SITE_HDKYLIN),
        # 音乐留空：IMDb / PT-Gen / NFO / MediaInfo
        "url": "",
        "pt_gen": "",
        "technical_info": "",
        # 六个质量字段 —— 全部走 `[4]` 组
        "processing_sel[4]": year,          # 年代
        "medium_sel[4]": medium,            # 媒介
        "codec_sel[4]": CODEC_OTHER,        # 视频编码（音乐不填）
        "audiocodec_sel[4]": audio,         # 音频编码
        "standard_sel[4]": STANDARD_OTHER,  # 分辨率（音乐不填）
        "source_sel[4]": region,            # 地区
        "team_sel[4]": TEAM_OTHER,          # 制作组
        "tags[4][]": tags,
    }
    audit.append(f"processing_sel[4]（年代）← {year}" + ("　★ 未取到年份 ⇒ 0" if year == YEAR_UNSET else ""))
    audit.append(f"medium_sel[4]（媒介）← {medium}")
    audit.append(f"audiocodec_sel[4]（音频编码）← {audio}")
    audit.append(f"source_sel[4]（地区）← {region}" + (f"　★ {region_note}" if region_note else ""))
    audit.append(f"codec_sel[4]={CODEC_OTHER} / standard_sel[4]={STANDARD_OTHER} / "
                 f"team_sel[4]={TEAM_OTHER}（音乐固定 Other）")
    audit.append(f"tags[4][]（标签）← {','.join(tags) if tags else '（空：无匹配的内容标签）'}")
    audit.append("descr（简介）← descr.build_descr(site='hdkylin')")

    if anonymous:
        fields["uplver"] = "yes"
        audit.append("uplver=yes (匿名发布)")

    return fields, audit


def build_edit_fields(task, *, anonymous=False):
    """编辑页只改文本元数据：标题 / 副标题 / 简介，不提交分类与质量下拉。

    ★ 麒麟站规「不得改变管理员对种子信息的修改」⇒ 默认**不做编辑**（同蟹黄堡）。
    此函数仅为将来**显式**编辑路径预留，与 `build_fields` 同源。
    """
    fields, audit = build_fields(task, anonymous=anonymous)
    for key in ("type", "processing_sel[4]", "medium_sel[4]", "codec_sel[4]",
                "audiocodec_sel[4]", "standard_sel[4]", "source_sel[4]",
                "team_sel[4]", "tags[4][]", "uplver", "url", "pt_gen",
                "technical_info"):
        fields.pop(key, None)
    return fields, audit


# ============================================================================
# 麒麟客户端
# ============================================================================

class HdkylinClient:
    """麒麟（hdkyl.in）客户端。只读 + POST takeupload/takeedit，不碰 qB、不碰库。"""

    def __init__(self, base_url=None, cookie=None, request_interval=1.5):
        self.base = (base_url or config.HDKYLIN_URL).rstrip("/")
        self.cookie = cookie if cookie is not None else config.HDKYLIN_COOKIE
        self.interval = request_interval
        self._last_request = 0.0

        self.sess = net.new_session()
        self.sess.headers.update({
            "User-Agent": UA,
            "Cookie": self.cookie,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    def _throttle(self):
        elapsed = time.time() - self._last_request
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last_request = time.time()

    def _url(self, path):
        return path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"

    def get(self, path, **kw):
        self._throttle()
        kw.setdefault("timeout", 30)
        return self.sess.get(self._url(path), **kw)

    def post(self, path, **kw):
        self._throttle()
        kw.setdefault("timeout", 60)
        return self.sess.post(self._url(path), **kw)

    def download_torrent(self, torrent_id):
        """下载麒麟服务端重写 tracker 后的 .torrent 文件，返回 bytes 或 None。"""
        r = self.get("/download.php", params={"id": torrent_id})
        if r.status_code == 200 and r.content.startswith(b"d"):
            return r.content
        return None

    @staticmethod
    def SEARCH_PARAMS(name):
        """麒麟列表页搜索参数（NexusPHP 标准）。"""
        return {
            "search": name,
            "incldead": 0,
            "spstate": 0,
            "inclbookmarked": 0,
            "search_area": 0,
            "search_mode": 0,
        }

    def find_existing_by_name(self, name, *, local_name, base_path="/torrents.php",
                              max_candidates=None, diag=None):
        """在麒麟全站搜索，比对 info.name，防止发重。"""
        import musopia
        return musopia.search_and_compare(self, name, local_name=local_name,
                                          base_path=base_path,
                                          max_candidates=max_candidates,
                                          diag=diag)

    def have_cookie(self):
        return bool((self.cookie or "").strip())

    def fetch_passkey(self):
        """从麒麟用户控制台抓 passkey（`usercp.php` 的「密钥」单元格）。

        ★ 凭据：只返回 `(passkey, 来源路径)`，**不打印、不写文件**。
        ★ 本项目的发布/回加主链路用不上它（回加用站点重写后的 `.torrent`）；
          保留此方法是为了接入期"自动核验",避免手工抄 passkey 进 `.env`。
        """
        import site_probe
        return site_probe.fetch_passkey(self.sess, self.base)

    def check_login(self):
        """验证 Cookie 有效性，返回 user_id 或 None。"""
        r = self.get("/torrents.php")
        if r.status_code == 401 or "WWW-Authenticate" in (r.headers or {}):
            raise SiteError(f"麒麟拒绝访问 (HTTP {r.status_code})，HDKYLIN_COOKIE 已失效或未配置。")
        if r.status_code != 200:
            raise SiteError(f"访问麒麟 torrents.php 失败，HTTP {r.status_code}")
        body = r.text or ""
        if "takelogin.php" in body or "login.php?returnto" in body:
            raise SiteError("麒麟 Cookie 已失效（页面退回登录页），请更新 HDKYLIN_COOKIE。")
        m = re.search(r"userdetails\.php\?id=(\d+)", body)
        return m.group(1) if m else None

    def fetch_upload_form(self):
        """GET /upload.php 读取空白表单，获取 action 与必要字段校验。"""
        r = self.get("/upload.php")
        if r.status_code != 200:
            raise SiteError(f"获取麒麟发布页失败，HTTP {r.status_code}")
        body = r.text or ""
        if "takelogin.php" in body:
            raise SiteError("麒麟 Cookie 已失效（发布页退回登录页）")

        bs = __import__("bs4").BeautifulSoup(body, "html.parser")
        form = bs.find("form", id="compose")
        if form is None:
            for f in bs.find_all("form"):
                if "takeupload" in (f.get("action") or "").lower():
                    form = f
                    break
        if form is None:
            raise SiteError("麒麟发布页中未找到发布表单（可能是账号无发种权限或页面结构变更）。")

        actions = {i.get("name"): i for i in form.find_all(["input", "select", "textarea"])
                   if i.get("name")}
        csrf = {k: v for k, v in actions.items()
                if k.lower() in ("csrf_token", "csrf", "_token")}
        return {
            "form": form,
            "actions": actions,
            "action": form.get("action") or "/takeupload.php",
            "csrf": csrf,
            "enctype": (form.get("enctype") or "").lower(),
        }

    def upload_torrent(self, torrent_path, fields, form_info=None):
        """向麒麟提交发布新种子，返回 (torrent_id 或 None, 消息文本)。"""
        import torf

        if not os.path.exists(torrent_path):
            raise SiteError(f"种子文件不存在: {torrent_path}")
        try:
            with open(torrent_path, "rb") as fh:
                raw = fh.read()
            torf.Torrent.read_stream(__import__("io").BytesIO(raw))
        except Exception as e:
            raise SiteError(f"种子文件损坏，非有效 .torrent: {torrent_path}: {e}") from e

        field_name = "file"
        if form_info:
            cand = [k for k in (form_info.get("actions") or {}) if k in ("file", "torrent")]
            if cand:
                field_name = cand[0]

        files = {
            field_name: (
                os.path.basename(torrent_path),
                raw,
                "application/x-bittorrent",
            )
        }

        data = dict(fields)
        if form_info and form_info.get("csrf"):
            for k, el in form_info["csrf"].items():
                if k not in data and el.get("value"):
                    data[k] = el["value"]

        action = (form_info.get("action") if form_info else None) or "/takeupload.php"
        r = self.post(
            action,
            data=data,
            files=files,
            headers={"Referer": self._url("/upload.php"), "Origin": self.base},
            allow_redirects=True,
        )

        # id：最终 URL 优先（详情页），其次 body 里的 details 链接
        tid = None
        for src in (r.url or "", r.text or ""):
            m = re.search(r"details\.php\?id=(\d+)", src)
            if m:
                tid = m.group(1)
                break
        if tid:
            return tid, "ok"

        _dump_takeupload(r.text or "", torrent_path, kind="hdkylin-takeupload")

        body = r.text or ""
        if r.status_code >= 400:
            return None, (f"SITE_DOWN: HTTP {r.status_code}（★ 5xx 也可能发生在"
                          f"提交**之后**，请务必去站点核对是否已建）")
        if "takelogin.php" in body or "login.php?returnto" in body:
            return None, "SITE_DOWN: 页面退回登录页（Cookie 已失效，请更新 .env）"

        err = _extract_error(body)
        if err:
            lower_err = err.lower()
            if "already exists" in lower_err or "已存在" in err or "重复" in err:
                return None, f"REJECT: 种子已存在: {err}"
            return None, f"REJECT: {err}"

        return None, (f"UNKNOWN: HTTP {r.status_code}，既没拿到 details 链接 "
                      f"也没读出报错（最终 URL {r.url!r}，{len(body)} 字节）。"
                      f"⇒ 请**立刻去站点确认这条种子建了没有**，**不要重跑**（发布不可逆）。"
                      f"响应已落盘 `raw_html/*-hdkylin-takeupload.html`。")


# ============================================================================
# 离线自检
# ============================================================================

def selftest(verbose=True):
    """麒麟适配层纯本地离线自检（零请求、零写库）。"""
    print("=" * 60)
    print("hdkylin_site 离线自检（纯离线，零请求）")
    print("=" * 60)

    ok = [True]

    def chk(name, cond, msg=""):
        if cond:
            print(f"  ✅ {name}")
        else:
            print(f"  ❌ {name} -- {msg}")
            ok[0] = False

    chk("分类 = 408", MUSIC_TYPE == "408")

    # 年代（非线性映射，逐项钉住）
    chk("2026 → 12", map_year({"year": "2026"}) == "12")
    chk("2023 → 1（非线性，不是 12-3=9）", map_year({"year": "2023"}) == "1")
    chk("2016 → 8", map_year({"year": "2016"}) == "8")
    chk("2015 → 9（Earlier）", map_year({"year": "2015"}) == "9")
    chk("无年份 → 0", map_year({}) == "0")
    chk("标题年份兜底（2008 → Earlier 9）",
        map_year({"formatted_title": "恩雅 - 冬季降临 2008 FLAC"}) == "9")

    # 媒介
    chk("CD 兜底 → 33", map_medium({}) == MEDIUM_CD)
    chk("OST → 32", map_medium({"formatted_title": "Album OST"}) == MEDIUM_TRACK)
    chk("WEB-DL → 31", map_medium({"medium_sel": "WEB"}) == MEDIUM_WEB)

    # 音频编码（按名字，含 WAV）
    chk("FLAC → 1", map_audiocodec({"format": "FLAC"}) == "1")
    chk("WAV → 13（★ 乌托邦编号缺失，靠名字转）", map_audiocodec({"formatted_title": "x - WAV 整轨"}) == "13")
    chk("APE → 12", map_audiocodec({"format": "APE"}) == "12")
    chk("MP3 → 4", map_audiocodec({"formatted_title": "x - 320K MP3"}) == "4")
    chk("DSD → Other 7（无对应项）", map_audiocodec({"format": "DSD"}) == "7")

    # 地区（方案 B：能精确的精确，拆不出的取代表值，接受约一半标错）
    v, n = map_region({"tags": [descr.TAGS["大陆"]]})
    chk("大陆 → 15 CN（精确）", v == "15", f"实得 {v}")
    v2, n2 = map_region({"tags": [descr.TAGS["港台"]]})
    chk("港台 → 16 香港（方案B 代表值）", v2 == "16", f"实得 {v2}")
    chk("港台有『会标错』说明（不静默）", bool(n2))
    v3, n3 = map_region({"tags": [descr.TAGS["欧美"]]})
    chk("欧美 → 28 欧洲（方案B 代表值）", v3 == "28", f"实得 {v3}")
    chk("欧美有『会标错』说明", bool(n3))
    v4, n4 = map_region({"tags": [descr.TAGS["日韩"]]})
    chk("日韩 → 19 日本（方案B 代表值）", v4 == "19", f"实得 {v4}")
    chk("日韩有『会标错』说明", bool(n4))
    chk("其它地区 → Other 14", map_region({"tags": [descr.TAGS["其他地区"]]})[0] == "14")
    chk("无地区标签 → Other 14", map_region({"tags": []})[0] == "14")

    # 标签白名单
    tags = map_tags({"formatted_title": "华语流行 Blu-ray LIVE 现场"})
    chk("国语标签勾上", TAG_GUOYU in tags)
    chk("LIVE 标签勾上", TAG_LIVE in tags)
    chk("Blu-ray 标签勾上", TAG_BLURAY in tags)
    tags2 = map_tags({"formatted_title": "Enya - And Winter Came"})
    chk("无语言/内容标签时不勾", tags2 == [])

    # build_fields 产出
    task = {
        "formatted_title": "恩雅 - 冬季降临 Enya - And Winter Came 2008 FLAC",
        "torrent_name": "恩雅 - 冬季降临 Enya - And Winter Came 2008 FLAC 分轨 - OpenCD",
        "opencd_tid": "12345",
        "cover_url": "https://img.example.com/a.jpg",
        "medium_sel": "CD",
        "tags": "4",          # 乌托邦「大陆」编号
        "year": "2008",
        "artist": "恩雅",
    }
    fields, audit = build_fields(task)
    chk("字段 type = 408", fields.get("type") == MUSIC_TYPE)
    chk("字段 name 存在", bool(fields.get("name")))
    chk("标题为麒麟形态（编码尾巴，无位深/采样率）",
        fields.get("name") == "恩雅 - 冬季降临 Enya 2008 FLAC",
        f"实得 {fields.get('name')}")
    chk("阴性对照：麒麟标题不含 16bit/44.1kHz",
        "16bit" not in fields.get("name", "") and "kHz" not in fields.get("name", ""))
    t_oc = {"formatted_title": "張惠妹 - 姊妹 1996 - FLAC Multi File - OpenCD",
            "year": "1996", "artist": "張惠妹"}
    chk("源标题带 - OpenCD 时保留来源后缀",
        descr.build_kylin_title(t_oc) == "張惠妹 - 姊妹 1996 FLAC - OpenCD",
        f"实得 {descr.build_kylin_title(t_oc)}")
    chk("字段 processing_sel[4]（年代，2008 → Earlier 9）= 9",
        fields.get("processing_sel[4]") == "9")
    chk("字段 medium_sel[4] = 33", fields.get("medium_sel[4]") == MEDIUM_CD)
    chk("字段 source_sel[4]（地区）= 15", fields.get("source_sel[4]") == "15")
    chk("字段 audiocodec_sel[4] = 1（FLAC）", fields.get("audiocodec_sel[4]") == "1")
    chk("字段 codec/standard/team 固定 Other",
        fields.get("codec_sel[4]") == CODEC_OTHER
        and fields.get("standard_sel[4]") == STANDARD_OTHER
        and fields.get("team_sel[4]") == TEAM_OTHER)
    chk("字段 descr 含封面与来源", "[img]" in fields["descr"] and "转自OpenCD" in fields["descr"])
    chk("uplver=yes", fields.get("uplver") == "yes")
    chk("禁转/首发/DIY 不勾", not ({"1", "2", "4"} & set(fields.get("tags[4][]", []))))
    chk("audit 非空", bool(audit))

    edit_fields, _ = build_edit_fields(task)
    chk("编辑字段不含 type", "type" not in edit_fields)
    chk("编辑字段保留 name", "name" in edit_fields)

    print("=" * 60)
    print("✅ 全部通过" if ok[0] else "❌ 有失败项")
    return ok[0]


if __name__ == "__main__":
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(0 if selftest() else 1)
