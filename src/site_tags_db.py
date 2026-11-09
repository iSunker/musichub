#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PT 各站点音乐标签与分类数据库 (site_tags_db)。

以 OpenCD（皇后）为唯一样本真源，收录各大 PT 站点音乐种子表单字段、
分类 ID、媒介/编码枚举、地区参数与标签映射规则。
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==============================================================================
# 一、 皇后 (OpenCD) 基准元数据字典 (真源唯一)
# ==============================================================================

# 1. 资源主类型 (type)
OPENCD_TYPES: Dict[str, str] = {
    "408": "音乐(Music)",
    "402": "演唱会(Vocal Concert)",
    "405": "戏剧(Drama)",
    "409": "其他(Other)",
}

# 2. 来源/流派 (source) - 17 种
OPENCD_SOURCES: Dict[str, str] = {
    "2": "流行(Pop)",
    "3": "古典(Classical)",
    "11": "器乐(Instrumental)",
    "4": "原声(OST)",
    "5": "摇滚(Rock)",
    "8": "爵士(Jazz)",
    "12": "新世纪(NewAge)",
    "13": "舞曲(Dance)",
    "14": "电子(Electronic)",
    "15": "民谣(Folk)",
    "16": "独立(Indie)",
    "17": "嘻哈(Hip Hop)",
    "18": "音乐剧(Musical)",
    "19": "乡村(Country)",
    "20": "另类(Alternative)",
    "21": "世界音乐(World)",
    "9": "其他(Others)",
}

# 3. 27 个分类标签 (分類標籤)
OPENCD_TAGS: Dict[str, Dict[str, str]] = {
    "1": {"name": "大陆", "category": "region", "en": "Mainland,CHN"},
    "2": {"name": "欧美", "category": "region", "en": "Western"},
    "3": {"name": "港台", "category": "region", "en": "HK/TW"},
    "4": {"name": "日韩", "category": "region", "en": "JP/KR"},
    "5": {"name": "其它地区", "category": "region", "en": "Other"},
    "6": {"name": "流行(Pop)", "category": "genre", "en": "Pop"},
    "7": {"name": "发烧(HiFi)", "category": "attribute", "en": "Hi-Fi"},
    "8": {"name": "汽车(garage)", "category": "scene", "en": "Car Audio"},
    "9": {"name": "古典(Classical)", "category": "genre", "en": "Classical"},
    "10": {"name": "民族(National)", "category": "genre", "en": "National/Folk"},
    "11": {"name": "摇滚(rock)", "category": "genre", "en": "Rock"},
    "12": {"name": "原声(OST)", "category": "genre", "en": "OST"},
    "13": {"name": "民间(Folk)", "category": "genre", "en": "Folk"},
    "14": {"name": "乡村(Country)", "category": "genre", "en": "Country"},
    "15": {"name": "天籁(Soul)", "category": "genre", "en": "Soul"},
    "16": {"name": "新世纪(NewAge)", "category": "genre", "en": "NewAge"},
    "17": {"name": "蓝调(Blues)", "category": "genre", "en": "Blues"},
    "18": {"name": "爵士(Jazz)", "category": "genre", "en": "Jazz"},
    "19": {"name": "金属(Metal)", "category": "genre", "en": "Metal"},
    "20": {"name": "朋克(Punk)", "category": "genre", "en": "Punk"},
    "21": {"name": "电子(Electronic)", "category": "genre", "en": "Electronic"},
    "22": {"name": "儿童(Children's)", "category": "audience", "en": "Children"},
    "23": {"name": "宗教(Religion)", "category": "genre", "en": "Religion"},
    "24": {"name": "雷鬼(Reggae)", "category": "genre", "en": "Reggae"},
    "25": {"name": "贝斯(Drum Bass)", "category": "genre", "en": "Drum & Bass"},
    "26": {"name": "说唱(Rap)", "category": "genre", "en": "Rap/Hip-Hop"},
    "27": {"name": "音乐剧(musical)", "category": "genre", "en": "Musical"},
}

# 4. 媒介介质 (medium) - 22 种
OPENCD_MEDIUMS: Dict[str, str] = {
    "1": "CD",
    "2": "24KCD",
    "3": "DSD",
    "4": "LPCD",
    "5": "HDCD",
    "6": "SACD",
    "7": "SRCD",
    "8": "K2CD",
    "9": "DTS",
    "10": "DAT",
    "11": "Blu-ray",
    "12": "HD DVD",
    "13": "HDTV",
    "14": "DVD",
    "16": "HQCD",
    "17": "XRCD",
    "18": "SHM-CD",
    "19": "Blu-spec",
    "20": "Vinyl",
    "21": "Web",
    "22": "HI-RES",
    "15": "Other",
}

# 5. 音频编码与标准 (standard) - 8 种
OPENCD_STANDARDS: Dict[str, str] = {
    "1": "镜像(Mirror)",
    "2": "WAV",
    "4": "FLAC",
    "15": "DTS",
    "17": "DFF",
    "18": "DSF",
    "19": "DST",
    "10": "其它(Other)",
}


# ==============================================================================
# 二、 各目标 PT 站点数据库 (SITES_DB) 与全局规则
# ==============================================================================

# 全生态通用安全与发布规则
COMMON_RULES: Dict[str, Any] = {
    "mandatory_anonymous": True,       # 所有种子跨站发布一律强制匿名
    "anonymous_field": "uplver",        # NexusPHP 标准表单字段名
    "anonymous_value": "yes",           # 提交值
    "safety_blacklist": [
        "jinzhuan", "pr", "bim",
        "tag.禁转", "tag.限转", "tag.独占",
        "tag.原创", "tag.自购",
    ],
}

SITES_DB: Dict[str, Dict[str, Any]] = {
    "pter": {
        "name": "PTerClub",
        "cname": "猫站",
        "category_music": "406",
        "category_mv": "413",
        "form_fields": {
            "type": "type",
            "medium": "source_sel",
            "region": "team_sel",
            "checkbox_anonymous": "uplver",
            "checkbox_guoyu": "guoyu",
            "checkbox_yueyu": "yueyu",
            "checkbox_zhongzi": "zhongzi",
            "checkbox_diy": "diy",
        },
        "medium_map": {
            "FLAC": "8",
            "WAV": "9",
            "Web": "5",       # WEB-DL
            "WEB-DL": "5",
            "镜像(Mirror)": "10",  # ISO
            "ISO": "10",
            "Blu-ray": "2",
            "DVD": "7",
            "Other": "15",
            "default": "15",
        },
        "region_map": {
            "大陆": "1",
            "香港": "2",
            "台湾": "3",
            "港台": "2",       # 默认香港，语义推断台湾时覆盖
            "欧美": "4",
            "韩国": "5",
            "日本": "6",
            "日韩": "6",       # 默认日本，语义推断韩国时覆盖
            "印度": "7",
            "其它地区": "8",
            "Other": "8",
            "default": "8",
        },
        "safety_blacklist": ["jinzhuan", "pr", "bim"],
    },

    "crabpt": {
        "name": "CrabPT",
        "cname": "蟹黄堡",
        "category_music": "408",
        "category_mv": "406",
        "form_fields": {
            "type": "type",
            "medium": "source_sel[4]",
            "audio_codec": "audiocodec_sel[4]",
            "region": "processing_sel[4]",
            "checkbox_anonymous": "uplver",
            "tags": "tags[4][]",
        },
        "medium_map": {
            "Web": "6",       # WEB-DL
            "WEB-DL": "6",
            "CD": "1",        # 实测 CD 抓轨在蟹黄堡多填 Other(1)
            "Blu-ray": "2",
            "HDTV": "7",
            "Other": "1",
            "default": "1",
        },
        "audio_codec_map": {
            "FLAC": "10",
            "WAV": "11",
            "APE": "12",
            "ALAC": "16",
            "MPEG": "9",      # MP3
            "MP3": "9",
            "AAC": "2",
            "DTS": "5",
            "DTS-HD": "15",
            "DTS-HD MA": "17",
            "LPCM": "7",
            "TrueHD": "6",
            "DSD": "23",
            "DFF": "23",
            "DSF": "23",
            "Atmos": "26",
            "Other": "1",
            "default": "1",
        },
        "region_map": {
            "大陆": "2",
            "港台": "3",
            "欧美": "4",
            "日本": "5",
            "韩国": "6",
            "印度": "7",
            "其它地区": "1",
            "Other": "1",
            "default": "1",
        },
        "tags_map": {
            "国语": "5",
            "粤语": "6",
            "中字": "7",
            "原盘DIY": "4",
            "完结": "8",
            "合集": "19",
            "ASMR": "50",
            "动画": "37",
        },
        "safety_blacklist": ["jinzhuan", "pr", "bim"],
    },

    "lemonhd": {
        "name": "LemonHD",
        "cname": "柠檬",
        "category_music": "411",
        "category_mv": "406",
        "form_fields": {
            "type": "type",
            "medium": "medium_sel",
            "audio_codec": "audiocodec_sel",
            "region": "team_sel",
            "checkbox_anonymous": "uplver",
            "tags": "tags[]",
        },
        "medium_map": {
            "Blu-ray": "1",
            "DVD": "2",
            "WEB-DL": "10",
            "Web": "10",
            "Other": "5",
            "CD": "5",
            "default": "5",
        },
        "audio_codec_map": {
            "FLAC": "10",
            "WAV": "11",
            "APE": "12",
            "AAC": "8",
            "LPCM": "9",
            "AC3": "6",
            "Other": "100",
            "default": "100",
        },
        "region_map": {
            "欧美": "1",
            "港台": "2",
            "大陆": "3",
            "日韩": "4",
            "印度": "5",
            "其它地区": "10",
            "Other": "10",
            "default": "10",
        },
        "safety_blacklist": ["tag.禁转", "jinzhuan"],
    },

    "ourbits": {
        "name": "OurBits",
        "cname": "我爱我比特",
        "category_music": "Music",
        "category_mv": "Concert",
        "form_fields": {
            "type": "type",
            "medium": "medium_sel",
            "audio_codec": "audiocodec_sel",
            "region": "processing_sel",
            "team": "team_sel",
            "checkbox_anonymous": "uplver",
            "tags": "tags[]",
        },
        "medium_map": {
            "CD": "CD",
            "WEB-DL": "WEB-DL",
            "Web": "WEB-DL",
            "Blu-ray": "FHD Blu-ray",
            "DVD": "DVD",
            "default": "CD",
        },
        "audio_codec_map": {
            "FLAC": "FLAC",
            "WAV": "WAV",
            "APE": "APE",
            "LPCM": "LPCM",
            "AAC": "AAC",
            "AC3": "AC3",
            "MP3": "MPEG",
            "MPEG": "MPEG",
            "OPUS": "OPUS",
            "DTS-HD MA": "DTS-HDMA",
            "TrueHD": "TrueHD",
            "default": "FLAC",
        },
        "region_map": {
            "大陆": "CN(中国大陆)",
            "港台": "HK/TW(港台)",
            "欧美": "US/EU(欧美)",
            "日韩": "JP(日)",
            "日本": "JP(日)",
            "韩国": "KR(韩)",
            "其它地区": "OT(其他)",
            "default": "OT(其他)",
        },
        "safety_blacklist": ["tag.禁转"],
    },

    "hddolby": {
        "name": "HDDolby",
        "cname": "杜比",
        "category_music": "408",
        "category_mv": "Music Videos",
        "form_fields": {
            "type": "type",
            "medium": "medium_sel",
            "audio_codec": "audiocodec_sel",
            "checkbox_anonymous": "uplver",
            "tags": "tags[]",
        },
        "medium_map": {
            "CD": "9",
            "WEB-DL": "6",
            "Web": "6",
            "Blu-ray": "2",
            "Other": "7",
            "default": "7",
        },
        "audio_codec_map": {
            "FLAC": "7",
            "WAV": "9",
            "APE": "8",
            "MP3": "10",
            "AAC": "6",
            "M4A": "11",
            "LPCM": "3",
            "DTS-HD MA": "1",
            "TrueHD": "2",
            "Other": "12",
            "default": "12",
        },
        "tags_map": {
            "国语": "gy",
            "粤语": "yy",
            "中字": "zz",
            "特效": "tx",
            "DIY": "diy",
        },
        "safety_blacklist": ["tag.禁转"],
    },

    "btschool": {
        "name": "BTSchool",
        "cname": "学校",
        "category_music": "409",
        "form_fields": {
            "type": "type",
            "medium": "medium_sel",
            "audio_codec": "audiocodec_sel",
            "checkbox_anonymous": "uplver",
            "tags": "span[]",
        },
        "medium_map": {
            "CD": "8",
            "WEB-DL": "10",
            "Web": "10",
            "Blu-ray": "1",
            "Other": "11",
            "default": "11",
        },
        "audio_codec_map": {
            "FLAC": "1",
            "APE": "2",
            "MP3": "4",
            "LPCM": "5",
            "AAC": "6",
            "WAV": "7",  # BTSchool 表单通常无独立 WAV，落 Other(7)
            "Other": "7",
            "default": "7",
        },
        "safety_blacklist": ["tag.禁转"],
    },

    "carpt": {
        "name": "CarPT",
        "cname": "车PT",
        "category_music": "406",
        "form_fields": {
            "type": "type",
            "medium": "medium_sel[4]",
            "audio_codec": "audiocodec_sel[4]",
            "checkbox_anonymous": "uplver",
        },
        "medium_map": {
            "CD": "5",
            "WEB-DL": "2",
            "Web": "2",
            "Blu-ray": "7",
            "Other": "6",
            "default": "6",
        },
        "audio_codec_map": {
            "FLAC": "5",
            "WAV": "10",
            "APE": "8",
            "MP3": "6",
            "AAC": "7",
            "LPCM": "4",
            "Other": "9",
            "default": "9",
        },
        "safety_blacklist": ["tag.禁转"],
    },

    "ssd": {
        "name": "SSD",
        "cname": "春天",
        "category_music": "Music(音乐)",
        "category_mv": "MV(音乐视频)",
        "category_audiobook": "Audio(有声)",
        "form_fields": {
            "type": "type",
            "medium": "medium_sel",
            "audio_codec": "audiocodec_sel",
            "region": "source_sel",
            "checkbox_anonymous": "uplver",
            "tags": "tags[]",
        },
        "medium_map": {
            "CD": "CD",
            "WEB-DL": "WEB-DL",
            "Web": "WEB-DL",
            "Blu-ray": "Blu-ray",
            "Other": "Other",
            "default": "Other",
        },
        "audio_codec_map": {
            "FLAC": "FLAC",
            "WAV": "WAV",
            "APE": "APE",
            "MP3": "MP3",
            "AAC": "AAC",
            "LPCM": "LPCM",
            "OPUS": "OPUS",
            "Other": "Other",
            "default": "Other",
        },
        "region_map": {
            "大陆": "Mainland(大陆)",
            "香港": "Hongkong(香港)",
            "台湾": "Taiwan(台湾)",
            "港台": "Hongkong(香港)",
            "欧美": "West(欧美)",
            "日本": "Japan(日本)",
            "韩国": "Korea(韩国)",
            "日韩": "Japan(日本)",
            "其它地区": "Other(其他地区)",
            "Other": "Other(其他地区)",
            "default": "Other(其他地区)",
        },
        "tags_map": {
            "国语": "国配",
            "中字": "中字",
            "原生": "原生",
        },
        "safety_blacklist": ["tag.禁转"],
    },

    "hdsky": {
        "name": "HDSky",
        "cname": "天空",
        "category_music": "HQ Audio/无损音乐",
        "category_mv": "Music Videos/音乐MV",
        "form_fields": {
            "type": "type",
            "medium": "medium_sel",
            "audio_codec": "audiocodec_sel",
            "checkbox_anonymous": "uplver",
            "tags": "tags[]",
        },
        "medium_map": {
            "CD": "CD",
            "SACD": "SACD",
            "WEB-DL": "WEB-DL",
            "Web": "WEB-DL",
            "Track": "Track",
            "Blu-ray": "Blu-ray",
            "Other": "Track",
            "default": "Track",
        },
        "audio_codec_map": {
            "FLAC": "FLAC",
            "WAV": "WAV",
            "APE": "APE",
            "DSD": "DSD",
            "DFF": "DSD",
            "DSF": "DSD",
            "LPCM": "LPCM",
            "PCM": "PCM",
            "MP3": "MP3",
            "AAC": "AAC",
            "ALAC": "ALAC",
            "Other": "Other",
            "default": "Other",
        },
        "safety_blacklist": ["tag.禁转", "tag.限转"],
    },

    "hdhome": {
        "name": "HDHome",
        "cname": "家园",
        "category_music": "Musics",
        "category_mv": "TVMusic",
        "form_fields": {
            "type": "type",
            "medium": "medium_sel",
            "audio_codec": "audiocodec_sel",
            "checkbox_anonymous": "uplver",
            "tags": "tags[]",
        },
        "medium_map": {
            "CD": "CD",
            "WEB-DL": "WEB-DL",
            "Web": "WEB-DL",
            "Blu-ray": "Blu-ray",
            "default": "CD",
        },
        "audio_codec_map": {
            "FLAC": "FLAC",
            "WAV": "WAV",
            "APE": "APE",
            "AAC": "AAC",
            "DTS": "DTS",
            "LPCM": "LPCM",
            "Other": "Other",
            "default": "FLAC",
        },
        "safety_blacklist": ["tag.禁转", "tag.限转"],
    },

    "ubits": {
        "name": "UBits",
        "cname": "UBits",
        "category_music": "406",
        "form_fields": {
            "type": "type",
            "medium": "medium_sel[4]",
            "audio_codec": "audiocodec_sel[4]",
            "region": "source_sel[4]",
            "checkbox_anonymous": "uplver",
            "tags": "tags[4][]",
        },
        "medium_map": {
            "CD": "Lossless Music",
            "分轨": "Track",
            "WEB-DL": "流媒体(WEB-DL)",
            "Web": "流媒体(WEB-DL)",
            "Blu-ray": "蓝光原盘(Blu-ray)",
            "default": "Lossless Music",
        },
        "audio_codec_map": {
            "FLAC": "1",
            "APE": "2",
            "MP3": "MP3",
            "AAC": "AAC",
            "LPCM": "LPCM",
            "Other": "Other",
            "default": "1",
        },
        "region_map": {
            "大陆": "中国大陆(China Mainland)",
            "香港": "中国香港(China HK)",
            "台湾": "中国台湾(China Taiwan)",
            "港台": "中国香港(China HK)",
            "欧美": "欧美(Euro/American)",
            "日本": "日本(Japanese)",
            "韩国": "韩国(Korea)",
            "其它地区": "其它(Other)",
            "default": "其它(Other)",
        },
        "safety_blacklist": ["tag.禁转"],
    },
}


# ==============================================================================
# 三、 查询与映射辅助接口
# ==============================================================================

def get_site_config(site_key: str) -> Optional[Dict[str, Any]]:
    """获取指定站点的配置字典。"""
    return SITES_DB.get(site_key.lower().strip())


def map_opencd_to_site_medium(site_key: str, opencd_medium: str, format_name: str = "") -> str:
    """根据 OpenCD 媒介和格式名称映射目标站媒介值。"""
    site = get_site_config(site_key)
    if not site:
        return ""
    m_map = site.get("medium_map", {})

    # 1. 优先特判 Web / WEB-DL
    if "web" in opencd_medium.lower() or "web" in format_name.lower():
        if "Web" in m_map:
            return m_map["Web"]
        if "WEB-DL" in m_map:
            return m_map["WEB-DL"]

    # 2. 特判 ISO / 镜像
    if "iso" in format_name.lower() or "镜像" in format_name or "mirror" in format_name.lower():
        if "ISO" in m_map:
            return m_map["ISO"]
        if "镜像(Mirror)" in m_map:
            return m_map["镜像(Mirror)"]

    # 3. 按音频格式特判（某些站如 PTer 媒介字段实际选择编码）
    for fmt_key in ["FLAC", "WAV", "APE"]:
        if fmt_key.lower() in format_name.lower() and fmt_key in m_map:
            return m_map[fmt_key]

    # 4. 常规媒介名称匹配
    if opencd_medium in m_map:
        return m_map[opencd_medium]

    return m_map.get("default", "")


def map_opencd_to_site_codec(site_key: str, standard_or_format: str) -> str:
    """根据 OpenCD 音频格式映射目标站音频编码值。"""
    site = get_site_config(site_key)
    if not site:
        return ""
    c_map = site.get("audio_codec_map", {})
    val_upper = standard_or_format.upper().strip()

    for k, v in c_map.items():
        if k.upper() in val_upper:
            return str(v)

    return str(c_map.get("default", ""))


def map_opencd_to_site_region(site_key: str, opencd_region_name: str) -> str:
    """根据 OpenCD 地区标签映射目标站地区值。"""
    site = get_site_config(site_key)
    if not site:
        return ""
    r_map = site.get("region_map", {})

    for k, v in r_map.items():
        if k in opencd_region_name:
            return str(v)

    return str(r_map.get("default", ""))


def export_db_to_json(filepath: str) -> None:
    """导出完整数据库到 JSON 文件。"""
    data = {
        "meta": {
            "source_anchor": "OpenCD",
            "version": "1.0",
            "description": "Cross-site PT music tag and category database",
            "mandatory_anonymous": True,
        },
        "common_rules": COMMON_RULES,
        "opencd": {
            "types": OPENCD_TYPES,
            "sources": OPENCD_SOURCES,
            "tags": OPENCD_TAGS,
            "mediums": OPENCD_MEDIUMS,
            "standards": OPENCD_STANDARDS,
        },
        "sites": SITES_DB,
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ==============================================================================
# 四、 离线自检套件 (--selftest)
# ==============================================================================

def selftest() -> bool:
    """完整自检断言，确保数据完备性与逻辑一致性。"""
    print("=== 开始 site_tags_db 离线自检 ===")
    ok = 0
    fails = 0

    def chk(desc: str, cond: bool) -> None:
        nonlocal ok, fails
        if cond:
            print(f"  ✅ {desc}")
            ok += 1
        else:
            print(f"  ❌ {desc}")
            fails += 1

    # 1. 皇后真源完备性校验
    chk("皇后主分类必须包含 4 项 (408/402/405/409)", len(OPENCD_TYPES) == 4 and "408" in OPENCD_TYPES)
    chk("皇后流派包含 17 项", len(OPENCD_SOURCES) == 17 and OPENCD_SOURCES["2"] == "流行(Pop)")
    chk("皇后标签包含 27 项", len(OPENCD_TAGS) == 27 and "1" in OPENCD_TAGS and "27" in OPENCD_TAGS)
    chk("皇后媒介包含 22 项", len(OPENCD_MEDIUMS) == 22 and OPENCD_MEDIUMS["1"] == "CD")
    chk("皇后编码标准包含 8 项", len(OPENCD_STANDARDS) == 8 and OPENCD_STANDARDS["4"] == "FLAC")

    # 2. 目标站数据库校验
    chk("SITES_DB 包含至少 10 个站点", len(SITES_DB) >= 10)
    for s_key in ["pter", "crabpt", "lemonhd", "ourbits", "hddolby", "btschool", "carpt", "ssd", "hdsky", "hdhome", "ubits"]:
        chk(f"站点 {s_key} 配置完整存在", s_key in SITES_DB and "category_music" in SITES_DB[s_key])

    # 3. 映射逻辑与推断校验
    chk("PTer: FLAC 媒介映射为 8", map_opencd_to_site_medium("pter", "CD", "Album - FLAC 分轨") == "8")
    chk("PTer: WAV 媒介映射为 9", map_opencd_to_site_medium("pter", "CD", "Album - WAV 整轨") == "9")
    chk("PTer: Web 媒介映射为 5", map_opencd_to_site_medium("pter", "Web", "Adele 30") == "5")
    chk("PTer: 大陆地区映射为 1", map_opencd_to_site_region("pter", "大陆") == "1")

    chk("CrabPT: FLAC 编码映射为 10", map_opencd_to_site_codec("crabpt", "FLAC") == "10")
    chk("CrabPT: WAV 编码映射为 11", map_opencd_to_site_codec("crabpt", "WAV") == "11")
    chk("CrabPT: APE 编码映射为 12", map_opencd_to_site_codec("crabpt", "APE") == "12")
    chk("CrabPT: DSD/DFF 编码映射为 23", map_opencd_to_site_codec("crabpt", "DFF") == "23")
    chk("CrabPT: 大陆地区映射为 2", map_opencd_to_site_region("crabpt", "大陆") == "2")
    chk("CrabPT: 欧美地区映射为 4", map_opencd_to_site_region("crabpt", "欧美") == "4")

    chk("LemonHD: 音乐主分类为 411", SITES_DB["lemonhd"]["category_music"] == "411")
    chk("LemonHD: FLAC 编码映射为 10", map_opencd_to_site_codec("lemonhd", "FLAC") == "10")
    chk("LemonHD: 大陆地区映射为 3", map_opencd_to_site_region("lemonhd", "大陆") == "3")

    chk("HDDolby: 音乐分类为 408", SITES_DB["hddolby"]["category_music"] == "408")
    chk("HDDolby: FLAC 编码为 7", map_opencd_to_site_codec("hddolby", "FLAC") == "7")
    chk("HDDolby: 国语标签映射为 gy", SITES_DB["hddolby"]["tags_map"]["国语"] == "gy")

    chk("CarPT: FLAC 编码映射为 5", map_opencd_to_site_codec("carpt", "FLAC") == "5")
    chk("CarPT: WAV 编码映射为 10", map_opencd_to_site_codec("carpt", "WAV") == "10")

    # 4. 安全红线校验
    for s_key, s_cfg in SITES_DB.items():
        bl = s_cfg.get("safety_blacklist", [])
        chk(f"{s_key} 安全黑名单非空", len(bl) > 0)

    # 5. 全生态强制匿名发布校验
    chk("全站强制匿名规则生效", COMMON_RULES["mandatory_anonymous"] is True and COMMON_RULES["anonymous_field"] == "uplver")
    chk("各站点表单包含 uplver 匿名选项", all(s.get("form_fields", {}).get("checkbox_anonymous") == "uplver" for s in SITES_DB.values()))

    print(f"\n自检结果: {ok} 通过, {fails} 失败")
    return fails == 0


if __name__ == "__main__":
    if "--json" in sys.argv or "--export" in sys.argv:
        out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "site_tags_db.json")
        export_db_to_json(out_path)
        print(f"已导出数据库至: {out_path}")
        sys.exit(0)

    success = selftest()
    sys.exit(0 if success else 1)
