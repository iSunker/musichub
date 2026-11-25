# -*- coding: utf-8 -*-
"""MusicHub 3.0 全矩阵 PT 站点发种标签智能匹配引擎 (TagMatcher)

设计与业务契约：
1. 【母种来源穿透】：
   - 提取母种原站（如 OpenCD、Musopia 等）标签 (tags) 与流派 (genre)，
     并结合专辑、艺术家、描述、规格等元数据进行多维语义推断。
2. 【严格排除黑名单 (FORBIDDEN_FORWARD_TAGS)】：
   - 杜绝冒领/虚假原创与站内专属属性：
     * “观众” / “Audiences” / “观众群” / “观众组”：绝对禁止勾选（母站观众不等于目标站观众）；
     * “首发”、“自购”、“自制”、“原创”、“禁转”：严格拦截，杜绝转种冒充原创或滥加禁转；
     * “官方”、“官种”、“驻站”、“珍宝楼”：站点运营专属，严禁自动勾选；
     * 视频专属标签（连载、完结、分集、特效、HDR、杜比视界等）：音乐种子严禁错挂。
3. 【目标站点矩阵智能映射 (12 目标站点)】：
   - Musopia (乌托邦): tags[4][]，覆盖 OpenCD(31) 来源、地区、20+ 细分音乐风格
   - UHDDolby (杜比): tags[]，国语(gy)、粤语(yy)、日语(ja)、韩语(ko)、中字(zz)
   - Xingwan (星湾): tags[4][]，无损音乐(17 必选)、国语(5)、粤语(31)、英语(35)、中字(6)
   - MuXueGe (牧雪阁): tags[4][]，乐府(30 必选)、国风音乐(10)、高码率(41)、国语(38)
   - TangPT (唐门): tags[4][]，演唱会(1)、玄学(35 HiFi/发烧)、国语(5)、粤语(10)、中字(6)
   - CrabPT (大闸蟹): tags[4][]，国语(5)、粤语(6)、中字(7)、合集大包(19)、ASMR(50)（严禁 Audiences 23）
   - HDKylin (麒麟): tags[4][]，LIVE现场(10)、国语(5)、中字(6)、英字(18)
   - LemonHD (柠檬): tags[4][]，国语(5)、中字(6)
   - HDTime (时间): tags[4][]，国语(5)、中字(6)
   - UBits (比特): tags[4][]，国语(5)、粤语(11)、中字(6)、合集(20)
   - SoulVoice (聆音): tags[4][] / tags[8][]，国语(5)、中字(6)
   - PTerClub (猫站): 无 tags 复选框控件，安全静默放行
   - NanyangPT (南洋): 无 tags 复选框控件，安全静默放行
   - SpringSunday (SSD/春天): 无 tags[] 数组控件，标签是**独立布尔复选框**
     （`pack` / `mandarin` / `subtitlezh` / `dovi` / `selfcraft` / `selfpurchase` …），
     只允许勾选语义明确且不冒领原创的 `pack`(合集)；`mandarin` 在 SSD 是「国配」
     （外语片国语配音），音乐种子严禁勾选，旧实现按「国语/普通话」关键词误勾已修正。
"""

import re
from typing import Dict, Any, List, Set, Tuple, Optional
import logging

from musichub.core.models import CanonicalMusicItem

logger = logging.getLogger("TagMatcher")

# 严禁在转种发种时勾选的黑名单关键词（小写匹配）
FORBIDDEN_TAG_KEYWORDS: Set[str] = {
    "audiences", "audience", "观众", "觀眾", "观众群", "观众组",
    "首发", "首發", "自购", "自購", "自制", "自製", "原创", "原創",
    "禁转", "禁轉", "官方", "官种", "官種", "驻站", "駐站",
    "珍宝楼", "珍寶樓", "已刮削", "纯净版", "生肉", "熟肉",
    "未完结", "完结", "连载", "分集", "条漫", "短剧", "cosplay", "秀人网"
}


class TagMatcher:
    """全矩阵 PT 站点发种标签智能匹配器"""

    # 各站点发种表单中的标签控件名映射
    SITE_TAG_FIELDS: Dict[str, str] = {
        "musopia": "tags[4][]",
        "uhddolby": "tags[]",
        "xingwan": "tags[4][]",
        "muxuege": "tags[4][]",
        "tangpt": "tags[4][]",
        "crabpt": "tags[4][]",
        "hdkylin": "tags[4][]",
        "lemonhd": "tags[4][]",
        "hdtime": "tags[4][]",
        "ubits": "tags[4][]",
        "soulvoice": "tags[4][]",
        "pterclub": "",  # 无 tags 控件
        "nanyangpt": "",  # 无 tags 控件
        "springsunday": "",  # 无 tags[] 数组控件，走 BOOL_TAG_SITES
    }

    # 标签为**独立布尔复选字段**（name=<标签名>&value=1）的站点：
    # match_tags_for_site() 返回的是字段名而不是下标值，inject 时逐个置 "1"。
    BOOL_TAG_SITES: Set[str] = {"springsunday"}

    @classmethod
    def is_forbidden_tag(cls, tag_name: str) -> bool:
        """检查标签名称是否命中黑名单"""
        if not tag_name:
            return False
        clean = tag_name.strip().lower()
        for kw in FORBIDDEN_TAG_KEYWORDS:
            if kw in clean:
                return True
        return False

    @classmethod
    def extract_semantic_features(cls, item: CanonicalMusicItem) -> Dict[str, Any]:
        """从标准化元数据及源站标签中提取语义特征集合"""
        raw_text = f"{item.artist} {item.album} {item.title} {item.edition or ''} {item.description or ''}"
        text = raw_text.lower()

        # 归一化输入标签池（包含 tags 与 genre）
        tag_pool: Set[str] = set()
        for t in (getattr(item, "tags", None) or []):
            if isinstance(t, str):
                t_str = t.strip().lower()
                tag_pool.add(t_str)
                # 剥离后缀，如 "流行(Pop)" -> "流行", "pop"
                m = re.match(r"^([^(（]+)[(（]([^)）]+)[)）]$", t_str)
                if m:
                    tag_pool.add(m.group(1).strip())
                    tag_pool.add(m.group(2).strip())
            elif isinstance(t, dict):
                name = str(t.get("name") or "").strip().lower()
                if name:
                    tag_pool.add(name)
                    m = re.match(r"^([^(（]+)[(（]([^)）]+)[)）]$", name)
                    if m:
                        tag_pool.add(m.group(1).strip())
                        tag_pool.add(m.group(2).strip())

        for g in (item.genre or []):
            if isinstance(g, str):
                g_str = g.strip().lower()
                tag_pool.add(g_str)
                m = re.match(r"^([^(（]+)[(（]([^)）]+)[)）]$", g_str)
                if m:
                    tag_pool.add(m.group(1).strip())
                    tag_pool.add(m.group(2).strip())

        origin_site = (item.origin_site or "").strip().lower()

        # 1. 地区与语言判断
        is_cantonese = (
            any(k in tag_pool for k in ("港台", "粤语", "cantonese")) or
            any(k in text for k in ("粤语", "cantonese", "香港", "港乐", "tvb", "hk-pop", "hkpop")) or
            ("港台" in text and any(k in text for k in ("粤", "香港", "hk")))
        )

        is_japanese = (
            any(k in tag_pool for k in ("日韩", "日本", "日语", "japanese")) or
            bool(re.search(r"[\u3040-\u30ff]", raw_text)) or
            any(k in text for k in ("日语", "japanese", "jpop", "j-pop", "acg", "anison"))
        )

        is_korean = (
            any(k in tag_pool for k in ("日韩", "韩国", "韩语", "korean")) or
            bool(re.search(r"[\uac00-\ud7af]", raw_text)) or
            any(k in text for k in ("韩语", "korean", "kpop", "k-pop"))
        )

        is_mandarin = (
            any(k in tag_pool for k in ("大陆", "国语", "华语", "内地")) or
            any(k in text for k in ("国语", "普通话", "mandarin", "大陆", "内地", "台湾", "华语")) or
            bool(re.search(r"[\u4e00-\u9fa5]", raw_text))  # 含汉字且未被其他独占
        )

        is_western = (
            any(k in tag_pool for k in ("欧美", "英语", "western")) or
            any(k in text for k in ("欧美", "western", "english", "american", "uk", "billboard", "grammy")) or
            (not is_mandarin and not is_cantonese and not is_japanese and not is_korean and any(c.isascii() for c in (item.artist or "")))
        )

        # 2. 歌词与字幕属性
        has_chinese_sub = (
            is_mandarin or is_cantonese or
            any(k in text for k in ("中字", "歌词", "lrc", "lyrics", "中文")) or
            any(k in tag_pool for k in ("中字", "中文歌词"))
        )
        has_english_sub = any(k in text for k in ("英字", "english lyrics", "english subtitle"))

        # 3. 音乐流派风格判断
        is_pop = any(k in tag_pool for k in ("流行", "pop")) or any(k in text for k in ("pop", "流行"))
        is_rock = any(k in tag_pool for k in ("摇滚", "rock")) or any(k in text for k in ("rock", "摇滚"))
        is_jazz = any(k in tag_pool for k in ("爵士", "jazz")) or any(k in text for k in ("jazz", "爵士"))
        is_classical = (
            any(k in tag_pool for k in ("古典", "classical")) or
            any(k in text for k in ("classical", "古典", "交响", "协奏曲", "philharmonic", "orchestra", "symphony", "concerto"))
        )
        is_ost = (
            any(k in tag_pool for k in ("原声", "ost", "soundtrack")) or
            any(k in text for k in ("ost", "soundtrack", "原声", "配乐", "电影原声", "score"))
        )
        is_electronic = (
            any(k in tag_pool for k in ("电子", "electronic")) or
            any(k in text for k in ("electronic", "电子", "techno", "trance", "house", "edm", "synth"))
        )
        is_metal = any(k in tag_pool for k in ("金属", "metal")) or any(k in text for k in ("metal", "金属", "heavy metal"))
        is_hifi = (
            any(k in tag_pool for k in ("发烧", "hifi")) or
            any(k in text for k in ("hifi", "hi-fi", "发烧", "头版", "母带", "dsd", "sacd")) or
            item.standard == "Hi-Res"
        )
        is_folk = any(k in tag_pool for k in ("民间", "folk", "民谣")) or any(k in text for k in ("folk", "民间", "民谣"))
        is_national = (
            any(k in tag_pool for k in ("民族", "national", "国风", "民乐")) or
            any(k in text for k in ("national", "民族", "国风", "民乐", "古筝", "二胡", "琵琶", "民歌"))
        )
        is_country = any(k in tag_pool for k in ("乡村", "country")) or any(k in text for k in ("country", "乡村"))
        is_soul = any(k in tag_pool for k in ("天籁", "soul")) or any(k in text for k in ("soul", "天籁", "r&b"))
        is_newage = any(k in tag_pool for k in ("新世纪", "newage")) or any(k in text for k in ("newage", "new age", "新世纪"))
        is_blues = any(k in tag_pool for k in ("蓝调", "blues")) or any(k in text for k in ("blues", "蓝调"))
        is_punk = any(k in tag_pool for k in ("朋克", "punk")) or any(k in text for k in ("punk", "朋克"))
        is_rap = (
            any(k in tag_pool for k in ("说唱", "rap", "嘻哈", "hip.hop")) or
            any(k in text for k in ("rap", "说唱", "嘻哈", "hip hop", "hip-hop"))
        )
        is_reggae = any(k in tag_pool for k in ("雷鬼", "reggae")) or any(k in text for k in ("reggae", "雷鬼"))
        is_bass = any(k in tag_pool for k in ("贝斯", "bass", "drum&bass")) or any(k in text for k in ("drum&bass", "贝斯", "dnb"))
        is_musical = any(k in tag_pool for k in ("音乐剧", "musical")) or any(k in text for k in ("musical", "音乐剧"))
        is_garage = any(k in tag_pool for k in ("汽车", "garage")) or any(k in text for k in ("garage", "汽车音乐", "车载"))
        is_religion = any(k in tag_pool for k in ("宗教", "religion")) or any(k in text for k in ("religion", "宗教", "赞美诗", "佛乐"))
        is_children = any(k in tag_pool for k in ("儿童", "children")) or any(k in text for k in ("children", "儿童", "儿歌"))
        is_instrumental = any(k in tag_pool for k in ("器乐", "instrumental")) or any(k in text for k in ("instrumental", "器乐", "纯音乐"))
        is_piano = any(k in tag_pool for k in ("piano", "钢琴")) or any(k in text for k in ("piano", "钢琴"))
        is_dance = any(k in tag_pool for k in ("dance", "舞曲")) or any(k in text for k in ("dance", "舞曲", "disco"))
        is_indie = any(k in tag_pool for k in ("indie", "独立")) or any(k in text for k in ("indie", "独立音乐"))

        # 4. 音质与实体特征
        is_live = any(k in text for k in ("live", "现场", "演唱会", "音乐会", "tour"))
        is_boxset = any(k in text for k in ("合集", "大包", "boxset", "complete", "discography", "全集"))
        is_asmr = "asmr" in text
        fmt_upper = (item.format or "").upper()
        is_lossless = fmt_upper in ("FLAC", "APE", "WAV", "DSD", "DSF", "DFF") or "FLAC" in fmt_upper
        is_hires = item.standard == "Hi-Res" or any(k in text for k in ("24bit", "96khz", "192khz", "dsd", "sacd", "hi-res"))

        return {
            "origin_site": origin_site,
            "is_cantonese": is_cantonese,
            "is_mandarin": is_mandarin,
            "is_japanese": is_japanese,
            "is_korean": is_korean,
            "is_western": is_western,
            "has_chinese_sub": has_chinese_sub,
            "has_english_sub": has_english_sub,
            "is_pop": is_pop,
            "is_rock": is_rock,
            "is_jazz": is_jazz,
            "is_classical": is_classical,
            "is_ost": is_ost,
            "is_electronic": is_electronic,
            "is_metal": is_metal,
            "is_hifi": is_hifi,
            "is_folk": is_folk,
            "is_national": is_national,
            "is_country": is_country,
            "is_soul": is_soul,
            "is_newage": is_newage,
            "is_blues": is_blues,
            "is_punk": is_punk,
            "is_rap": is_rap,
            "is_reggae": is_reggae,
            "is_bass": is_bass,
            "is_musical": is_musical,
            "is_garage": is_garage,
            "is_religion": is_religion,
            "is_children": is_children,
            "is_instrumental": is_instrumental,
            "is_piano": is_piano,
            "is_dance": is_dance,
            "is_indie": is_indie,
            "is_live": is_live,
            "is_boxset": is_boxset,
            "is_asmr": is_asmr,
            "is_lossless": is_lossless,
            "is_hires": is_hires,
            "tag_pool": tag_pool,
        }

    @classmethod
    def match_tags_for_site(cls, site_name: str, item: CanonicalMusicItem) -> List[str]:
        """为特定目标站点计算应勾选的标签值列表（自动经过黑名单审查）"""
        site = site_name.strip().lower()
        feats = cls.extract_semantic_features(item)
        picked: List[str] = []

        if site == "musopia":
            # 乌托邦 tags[4][]
            # 来源站标记
            if feats["origin_site"] == "opencd" or "opencd" in feats["tag_pool"]:
                picked.append("31")  # OpenCD

            # 地区/语言识别
            if feats["is_cantonese"]:
                picked.append("6")  # 港台
            elif feats["is_mandarin"]:
                picked.append("4")  # 大陆

            if feats["is_japanese"]:
                picked.append("7")   # 日韩
                picked.append("39")  # japanese
            elif feats["is_korean"]:
                picked.append("7")   # 日韩
                picked.append("41")  # korean

            if feats["is_western"]:
                picked.append("5")   # 欧美

            if any(k in feats["tag_pool"] for k in ("其它地区", "其他地区")):
                picked.append("8")   # 其他地区

            # 音乐流派
            if feats["is_pop"]: picked.append("10")
            if feats["is_rock"]: picked.append("9")
            if feats["is_jazz"]: picked.append("11")
            if feats["is_electronic"]: picked.append("12")
            if feats["is_metal"]: picked.append("13")
            if feats["is_bass"]: picked.append("14")
            if feats["is_classical"]: picked.append("15")
            if feats["is_ost"]: picked.append("16")
            if feats["is_hifi"]: picked.append("17")
            if feats["is_soul"]: picked.append("18")
            if feats["is_newage"]: picked.append("19")
            if feats["is_rap"]:
                picked.append("20")
                picked.append("32")  # 嘻哈
            if feats["is_reggae"]: picked.append("21")
            if feats["is_blues"]: picked.append("22")
            if feats["is_folk"]: picked.append("23")
            if feats["is_country"]: picked.append("24")
            if feats["is_punk"]: picked.append("25")
            if feats["is_national"]: picked.append("26")
            if feats["is_musical"]: picked.append("27")
            if feats["is_garage"]: picked.append("28")
            if feats["is_religion"]: picked.append("29")
            if feats["is_instrumental"]: picked.append("34")
            if feats["is_children"]: picked.append("35")
            if feats["is_dance"]: picked.append("38")
            if feats["is_indie"]: picked.append("40")
            if feats["is_piano"]: picked.append("42")

        elif site == "uhddolby":
            # 杜比 tags[]
            if feats["is_cantonese"]:
                picked.append("yy")  # 粤语
            elif feats["is_japanese"]:
                picked.append("ja")  # 日语
            elif feats["is_korean"]:
                picked.append("ko")  # 韩语
            elif feats["is_mandarin"]:
                picked.append("gy")  # 国语

            if feats["has_chinese_sub"]:
                picked.append("zz")  # 中字

        elif site == "xingwan":
            # 星湾 tags[4][]
            # 核心必选项：无损音乐 (17)
            picked.append("17")

            if feats["is_cantonese"]:
                picked.append("31")  # 粤语
            elif feats["is_mandarin"]:
                picked.append("5")   # 国语

            if feats["is_western"]:
                picked.append("35")  # 英语

            if feats["has_chinese_sub"]:
                picked.append("6")   # 中字

        elif site == "muxuege":
            # 牧雪阁 tags[4][]
            # 核心必选项：乐府 (30 - 音乐专区)
            picked.append("30")

            if feats["is_national"] or any(k in feats["tag_pool"] for k in ("国风", "古风", "民乐")):
                picked.append("10")  # 国风音乐

            if feats["is_mandarin"]:
                picked.append("38")  # 国语

            if feats["is_hires"] or feats["is_lossless"]:
                picked.append("41")  # 高码率

        elif site == "tangpt":
            # 唐门 tags[4][]
            if feats["is_live"]:
                picked.append("1")   # 演唱会
            if feats["is_hifi"]:
                picked.append("35")  # 玄学

            if feats["is_cantonese"]:
                picked.append("10")  # 粤语
            elif feats["is_mandarin"]:
                picked.append("5")   # 国语

            if feats["has_chinese_sub"]:
                picked.append("6")   # 中字

        elif site == "crabpt":
            # 蟹黄堡 tags[4][] - 绝对禁止勾选 23 (Audiences / 观众)
            if feats["is_cantonese"]:
                picked.append("6")   # 粤语
            elif feats["is_mandarin"]:
                picked.append("5")   # 国语

            if feats["has_chinese_sub"]:
                picked.append("7")   # 中字

            if feats["is_boxset"]:
                picked.append("19")  # 合集大包

            if feats["is_asmr"]:
                picked.append("50")  # ASMR

        elif site == "hdkylin":
            # 麒麟 tags[4][]
            if feats["is_live"]:
                picked.append("10")  # LIVE现场

            if feats["is_mandarin"]:
                picked.append("5")   # 国语

            if feats["has_chinese_sub"]:
                picked.append("6")   # 中字

            if feats["has_english_sub"]:
                picked.append("18")  # 英字

        elif site == "lemonhd":
            # 柠檬 tags[4][]
            if feats["is_mandarin"]:
                picked.append("5")   # 国语
            if feats["has_chinese_sub"]:
                picked.append("6")   # 中字

        elif site == "hdtime":
            # 高清时间 tags[4][]
            if feats["is_mandarin"]:
                picked.append("5")   # 国语
            if feats["has_chinese_sub"]:
                picked.append("6")   # 中字

        elif site == "ubits":
            # 比特 tags[4][]
            if feats["is_cantonese"]:
                picked.append("11")  # 粤语
            elif feats["is_mandarin"]:
                picked.append("5")   # 国语

            if feats["has_chinese_sub"]:
                picked.append("6")   # 中字

            if feats["is_boxset"]:
                picked.append("20")  # 合集

        elif site == "soulvoice":
            # 聆音 tags[4][]
            if feats["is_mandarin"]:
                picked.append("5")   # 国语
            if feats["has_chinese_sub"]:
                picked.append("6")   # 中字

        elif site == "springsunday":
            # SSD 布尔复选框：仅「合集」语义明确可用。
            # 严禁：selfcraft(自制)/selfpurchase(自购)/exclusive(禁转)/selfcompile(自译)
            # 冒领原创；mandarin(国配)/subtitlezh(中字)/subtitlesp(特效) 是影视属性；
            # dovi/hdr10/hdr10plus/hdrvivid/hlg/3d 是视频 HDR，音乐种子一律不勾。
            if feats["is_boxset"]:
                picked.append("pack")

        elif site == "pterclub":
            # 猫站无 tags 控件，保持空
            pass

        # 去重并返回
        unique_picked: List[str] = []
        for p in picked:
            if p not in unique_picked:
                unique_picked.append(p)

        return unique_picked

    @classmethod
    def inject_form_tags(cls, site_name: str, item: CanonicalMusicItem, fields: Dict[str, Any]) -> None:
        """统一将目标站点计算出的标签集合注入发种表单字段字典中"""
        site = site_name.strip().lower()

        if site in cls.BOOL_TAG_SITES:
            # 布尔复选：字段名即标签名，命中黑名单的一律不勾
            tags = [t for t in cls.match_tags_for_site(site, item) if not cls.is_forbidden_tag(t)]
            for name in tags:
                fields[name] = "1"
            if tags:
                logger.debug(f"[{site_name}] 注入布尔标签字段: {tags}")
            return

        field_name = cls.SITE_TAG_FIELDS.get(site)
        if not field_name:
            return

        tags = cls.match_tags_for_site(site, item)
        if tags:
            fields[field_name] = tags
            logger.debug(f"[{site_name}] 注入发种标签 ({field_name}): {tags}")
