# -*- coding: utf-8 -*-
"""逐站发布/编辑规则登记表（单一事实源）。

用途
----
行情页（发布页 / 编辑页 / 规则页）的结论不能只散落在 10 个适配器的
`build_form_fields()` 里——那样「某站到底要不要封面、日志放哪、标题能不能用中文」
只能靠读代码猜。本表把**编辑侧**需要的结论集中登记，供：

1. `musichub/onsite/editor.py` 决定封面块插到哪、日志块怎么折叠；
2. `musichub/tools/edit_onsite.py --print-mapping` 直接打印映射表；
3. 缺字段排查时一眼看出「这站是没适配，还是适配了没验证」。

证据等级
--------
`verified=True`  = 有发布页表单快照 / 线上 edit.php 探针实测；
`verified=False` = 沿用父项目实测契约或适配器注释，尚未在编辑页复核。
不确定项一律写进 `note`，不假装已验证。
"""

from typing import Any, Dict, List, Optional

# 目标站在 descr 里的排版差异（与 adapters/descr_core.build_descr 的 site 参数一致）
LOG_SPOILER = "spoiler"   # [spoiler=LOG][code][/code][/spoiler]
LOG_HIDE = "hide"         # 猫站 [hide=Log]…[/hide]
LOG_NONE = "none"         # 蟹黄堡不要日志折叠块

SITE_RULES: Dict[str, Dict[str, Any]] = {
    "lemonhd": {
        "display": "LemonHD",
        "domain": "lemonhd.net",
        "base_url": "https://lemonhd.net",
        "adapter": "LemonHDAdapter",
        "category": "402",
        "edit_url": "/edit.php",
        "edit_action": "takeedit",
        "cover_mode": "descr_img",           # 封面只能写进 descr 的 [img]
        "cover_host_required": "",           # 无专用图床要求
        "log_mode": LOG_SPOILER,
        "anonymous_field": "",               # 表单无 uplver，具名发布
        "title_latin1_only": False,
        "proxy": True,
        "fields": ["name", "small_descr", "type", "medium_sel[4]", "audiocodec_sel[4]",
                   "codec_sel[4]", "standard_sel[4]", "team_sel[4]", "descr"],
        "verified": True,
        "source": "发布页快照 data/lemonhd_upload.html（2026-11-20）",
        "note": "必须走 NAS mihomo 代理；编辑页 takeedit 是否允许匿名账号待现场复核",
    },
    "uhddolby": {
        "display": "UHDDolby",
        "domain": "uhddolby.com",
        "base_url": "https://www.uhddolby.com",
        "adapter": "UHDDolbyAdapter",
        "category": "408",
        "edit_url": "/edit.php",
        "edit_action": "takeedit",
        "cover_mode": "descr_img",
        "cover_host_required": "",
        "log_mode": LOG_SPOILER,
        "anonymous_field": "uplver",
        "title_latin1_only": False,
        "proxy": False,
        "fields": ["name", "small_descr", "type", "medium_sel", "audiocodec_sel",
                   "codec_sel", "standard_sel", "team_sel", "descr", "uplver"],
        "verified": True,
        "source": "发布页快照 data/uhddolby_upload.html（2026-11-20）",
        "note": "★ 字段名**不带** [4] 后缀（与其余站不同），编辑时按表单原名回填，禁止套用 [4]",
    },
    "hdtime": {
        "display": "HDTime",
        "domain": "hdtime.org",
        "base_url": "https://hdtime.org",
        "adapter": "HDTimeAdapter",
        "category": "408",
        "edit_url": "/edit.php",
        "edit_action": "takeedit",
        "cover_mode": "descr_img",
        "cover_host_required": "",
        "log_mode": LOG_SPOILER,
        "anonymous_field": "uplver",
        "title_latin1_only": False,
        "proxy": False,
        "fields": ["name", "small_descr", "type", "medium_sel[4]", "audiocodec_sel[4]",
                   "codec_sel[4]", "team_sel[4]", "descr", "uplver"],
        "verified": True,
        "source": "发布页快照 data/hdtime_upload.html（2026-11-20）",
        "note": "发布页无 standard_sel[4]（适配器也没发），编辑时沿用站点现值，不要凭空补",
    },
    "musopia": {
        "display": "乌托邦",
        "domain": "musopia.vip",
        "base_url": "https://www.musopia.vip",
        "adapter": "MusopiaAdapter",
        "category": "408",
        "edit_url": "/edit.php",
        "edit_action": "takeedit",
        "cover_mode": "descr_img",
        "cover_host_required": "",
        "log_mode": LOG_SPOILER,
        "anonymous_field": "uplver",
        "title_latin1_only": False,
        "proxy": False,
        "fields": ["name", "small_descr", "type", "nfoaction", "medium_sel[4]",
                   "audiocodec_sel[4]", "team_sel[4]", "descr", "uplver",
                   "custom_fields[4][1]", "custom_fields[4][2]", "custom_fields[4][3]",
                   "custom_fields[4][4]", "custom_fields[4][5]"],
        "verified": True,
        "source": "父项目 src/musopia.py + tools/show_mapping.py（edit.php 实测）",
        "note": "custom_fields[4][2] 发行年份、[4][3] 抓轨日志为**必填**；[4][3] 是 textarea 不是 input",
    },
    "crabpt": {
        "display": "蟹黄堡",
        "domain": "crabpt.vip",
        "base_url": "https://crabpt.vip",
        "adapter": "CrabPTAdapter",
        "category": "408",
        "edit_url": "/edit.php",
        "edit_action": "takeedit",
        "cover_mode": "descr_img",
        "cover_host_required": "img.crabpt.vip",   # Lsky Pro 专线，不接受跨站图链
        "log_mode": LOG_NONE,
        "anonymous_field": "uplver",
        "title_latin1_only": False,
        "proxy": False,
        "fields": ["name", "small_descr", "type", "source_sel[4]", "standard_sel[4]",
                   "codec_sel[4]", "audiocodec_sel[4]", "team_sel[4]",
                   "processing_sel[4]", "descr", "uplver"],
        "verified": True,
        "source": "父项目 src/crabpt_site.py + 憨憨与蟹黄宝音乐种子字段.md",
        "note": "★ processing_sel[4] 是**地区**、source_sel[4] 是**媒介**（与麒麟相反）；封面必须 Lsky",
    },
    "pterclub": {
        "display": "猫站",
        "domain": "pterclub.net",
        "base_url": "https://pterclub.net",
        "adapter": "PTerClubAdapter",
        "category": "406",
        "edit_url": "/edit.php",
        "edit_action": "takeedit",
        "cover_mode": "descr_img",
        "cover_host_required": "",
        "log_mode": LOG_HIDE,
        "anonymous_field": "uplver",
        "title_latin1_only": False,
        "proxy": False,
        "fields": ["name", "small_descr", "type", "source_sel", "team_sel", "descr", "uplver"],
        "verified": True,
        "source": "父项目 src/pter_site.py（edit.php → takeedit.php 实测）",
        "note": "规则要求 Log 与频谱图至少一项，日志用 [hide=Log]；简介正文需 Markdown→BBCode",
    },
    "xingwan": {
        "display": "星湾",
        "domain": "xingwan.cc",
        "base_url": "https://xingwan.cc",
        "adapter": "XingwanAdapter",
        "category": "408",
        "edit_url": "/edit.php",
        "edit_action": "takeedit",
        "cover_mode": "descr_img",
        "cover_host_required": "",
        "log_mode": LOG_SPOILER,
        "anonymous_field": "uplver",
        "title_latin1_only": False,
        "proxy": False,
        "fields": ["name", "small_descr", "type", "medium_sel[4]", "tags[4][]", "descr", "uplver"],
        "verified": False,
        "source": "适配器实测契约（无发布页快照入库）",
        "note": "tags 由描述关键词猜（17 基础 + 5/31/35），属**推断**；编辑页字段未逐项复核",
    },
    "hdkylin": {
        "display": "红豆麒麟",
        "domain": "hdkylin.in",
        "base_url": "https://www.hdkyl.in",
        "adapter": "HDKylinAdapter",
        "category": "408",
        "edit_url": "/edit.php",
        "edit_action": "takeedit",
        "cover_mode": "descr_img",
        "cover_host_required": "",
        "log_mode": LOG_SPOILER,
        "anonymous_field": "uplver",
        "title_latin1_only": True,
        "proxy": False,
        "fields": ["name", "small_descr", "type", "processing_sel[4]", "medium_sel[4]",
                   "codec_sel[4]", "audiocodec_sel[4]", "standard_sel[4]",
                   "source_sel[4]", "team_sel[4]", "descr", "uplver"],
        "verified": True,
        "source": "docs/references/麒麟站点要求.md + edit.php/takeedit.php 只读探针",
        "note": "★ processing_sel=年代、source_sel=地区（与蟹黄堡相反）；自动审核只要主标题含非 Latin-1 就拒，编辑**不会**自动复审",
    },
    "tangpt": {
        "display": "不可躺/唐门",
        "domain": "www.tangpt.top",
        "base_url": "https://www.tangpt.top",
        "adapter": "TangPTAdapter",
        "category": "409",
        "edit_url": "/edit.php",
        "edit_action": "takeedit",
        "cover_mode": "descr_img",
        "cover_host_required": "",
        "log_mode": LOG_SPOILER,
        "anonymous_field": "uplver",
        "title_latin1_only": False,
        "proxy": True,
        "fields": ["name", "small_descr", "type", "medium_sel[4]", "codec_sel[4]",
                   "standard_sel[4]", "audiocodec_sel[4]", "team_sel[4]", "descr", "uplver"],
        "verified": False,
        "source": "父项目 tangpt_site.py 实测契约",
        "note": "Cloudflare 保护，直连偶发握手超时，必须走代理；编辑页字段未逐项复核",
    },
    "muxuege": {
        "display": "牧雪阁",
        "domain": "pt.muxuege.org",
        "base_url": "https://pt.muxuege.org",
        "adapter": "MuXueGeAdapter",
        "category": "408",
        "edit_url": "/edit.php",
        "edit_action": "takeedit",
        "cover_mode": "descr_img",
        "cover_host_required": "",
        "log_mode": LOG_SPOILER,
        "anonymous_field": "",
        "title_latin1_only": False,
        "proxy": False,
        "fields": ["name", "small_descr", "type", "processing_sel[4]",
                   "audiocodec_sel[4]", "medium_sel[4]", "codec_sel[4]", "team_sel[4]", "descr"],
        "verified": False,
        "source": "适配器实测契约（无发布页快照入库）",
        "note": "medium_sel[4] 恒发 10、team_sel[4] 恒发 52，属**写死值**不是映射结果；编辑时沿用现值更安全",
    },
    "opencd": {
        "display": "OpenCD",
        "domain": "open.cd",
        "base_url": "https://open.cd",
        "adapter": "OpenCDAdapter",
        "category": "408",
        "edit_url": "",
        "edit_action": "",
        "cover_mode": "separate_field",       # 表单有独立 cover 字段
        "cover_host_required": "",
        "log_mode": LOG_SPOILER,
        "anonymous_field": "uplver",
        "title_latin1_only": False,
        "proxy": False,
        "fields": ["type", "source", "standard", "medium", "audio_mode", "team",
                   "boardid", "frname", "share_rule", "artist", "resource_name",
                   "year", "small_descr", "cover", "descr", "track_list", "uplver"],
        "verified": True,
        "source": "父项目 opencd.py plugin_upload.php 实测",
        "note": "★ 发布走 plugin_upload_save.php，非 takeupload.php；标准 edit.php/takeedit 未验证",
    },
    "ubits": {
        "display": "UBits",
        "domain": "ubits.club",
        "base_url": "https://ubits.club",
        "adapter": "UBitsAdapter",
        "category": "406",
        "edit_url": "/edit.php",
        "edit_action": "takeedit",
        "cover_mode": "descr_img",
        "cover_host_required": "",
        "log_mode": LOG_SPOILER,
        "anonymous_field": "uplver",
        "title_latin1_only": False,
        "proxy": False,
        "fields": ["name", "small_descr", "type", "medium_sel[4]", "audiocodec_sel[4]",
                   "codec_sel[4]", "standard_sel[4]", "source_sel[4]", "team_sel[4]", "descr", "uplver"],
        "verified": True,
        "source": "发布页现场探针与快照（2026-11-22）",
        "note": "NexusPHP，直连可达，表单带 _token，uplver=yes 支持匿名",
    },
    "soulvoice": {
        "display": "SoulVoice",
        "domain": "pt.soulvoice.club",
        "base_url": "https://pt.soulvoice.club",
        "adapter": "SoulVoiceAdapter",
        "category": "408",
        "edit_url": "/edit.php",
        "edit_action": "takeedit",
        "cover_mode": "descr_img",
        "cover_host_required": "",
        "log_mode": LOG_SPOILER,
        "anonymous_field": "uplver",
        "title_latin1_only": False,
        "proxy": False,
        "fields": ["name", "small_descr", "type", "medium_sel[4]", "codec_sel[4]",
                   "standard_sel[4]", "team_sel[4]", "hr[4]", "descr", "uplver"],
        "verified": True,
        "source": "发布页与详情页现场实测（2026-11-22）",
        "note": "直连可用；type=408，medium_sel[4]=10(APE/FLAC)/11(DSD)/12(Other)，hr[4]=0，uplver=yes",
    },
    "nanyangpt": {
        "display": "NanyangPT",
        "domain": "nanyangpt.com",
        "base_url": "https://nanyangpt.com",
        "adapter": "NanyangPTAdapter",
        "category": "407",
        "edit_url": "/edit.php",
        "edit_action": "takeedit",
        "cover_mode": "descr_img",
        "cover_host_required": "",
        "log_mode": LOG_SPOILER,
        "anonymous_field": "uplver",
        "title_latin1_only": False,
        "proxy": False,
        "fields": ["type", "name", "small_descr", "music_time", "music_type",
                   "music_author", "music_name", "music_form", "music_qual", "descr", "uplver"],
        "verified": True,
        "source": "发布页与 ajaxdealing 现场实测（2026-11-25）",
        "note": "NYPT Project，直连可用；type=407，动态音乐字段 music_time/music_type/music_author/music_name/music_form/music_qual，uplver=yes",
    },
    "springsunday": {
        "display": "SpringSunday",
        "domain": "springsunday.net",
        "base_url": "https://springsunday.net",
        "adapter": "SpringSundayAdapter",
        "category": "508",
        "edit_url": "/edit.php",
        "edit_action": "takeedit",
        "cover_mode": "poster_and_attachment",
        "cover_host_required": "",
        "log_mode": LOG_SPOILER,
        "anonymous_field": "uplver",
        "title_latin1_only": False,
        "proxy": False,
        "fields": ["type", "source_sel", "medium_sel", "standard_sel", "codec_sel",
                   "audiocodec_sel", "name", "small_descr", "url_poster", "url_vimages",
                   "Media_BDInfo", "descr", "uplver", "pack"],
        "verified": True,
        "source": "upload.php?offer=1 与 attachment-picker.js 现场实测（2026-11-25）",
        "note": ("直连可用；type=508，英文 Media_BDInfo 必填，截图必填（白名单图床 URL 或 "
                 "/attachment_upload.php 附件 screenshot_attachment_ids[]）；标签为独立布尔复选框，"
                 "仅勾 pack(合集)，mandarin=国配严禁勾；无直接发种权限时自动附加 offer=yes 发候选；"
                 "候选积分<10 走候选，待定候选≥3 或存在冻结种子时站点整体禁发（1 小时冷却）"),
    },
}


def get_rule(site: str) -> Dict[str, Any]:
    """取站点规则；未登记时给出可读报错，而不是静默用默认值。"""
    key = (site or "").strip().lower()
    if key not in SITE_RULES:
        raise KeyError(f"未登记的站点规则: {site!r}（已登记: {', '.join(sorted(SITE_RULES))}）")
    return SITE_RULES[key]


def rules_rows() -> List[Dict[str, Any]]:
    """给 `--print-mapping` 用的稳定行序。"""
    return [dict(site=k, **v) for k, v in sorted(SITE_RULES.items())]


def cover_capable_sites() -> List[str]:
    """允许通过 descr 补封面的站点（独立 cover 字段的站点走别的入口）。"""
    return [k for k, v in SITE_RULES.items() if v.get("cover_mode") == "descr_img"]


def site_for_domain(domain: Optional[str]) -> Optional[str]:
    d = (domain or "").strip().lower()
    for k, v in SITE_RULES.items():
        if v.get("domain") == d or d.endswith("." + str(v.get("domain") or "\0")):
            return k
    return None
