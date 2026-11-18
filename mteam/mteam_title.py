# -*- coding: utf-8 -*-
"""主/副标题的构建与合规校验。

规则来源见同目录 RULES.md §4；本模块只做字符串层面的形状约束，
不判断资源内容是否允许发布。
"""

import re

# 主标题禁止的特殊符号（点号另行处理，因为 H.264 / 5.1 合法）
_BANNED_PUNCT = re.compile(r"[!;#$%:]")
# 合法的带点形态，先从标题里剔除再做点号判定
_LEGAL_DOT = re.compile(r"(H\.26[45]|\d+\.\d+)")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
# 音乐标题规则要求写 整軌/分軌（繁体）或 整轨/分轨，属规则内允许的汉字
_MUSIC_SPLIT = re.compile(r"整[軌轨]|分[軌轨]")
_EXT = re.compile(r"\.(mkv|mp4|avi|ts|m2ts|flac|ape|wav|iso|rar|zip|7z)\b", re.IGNORECASE)
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_BRACKET = re.compile(r"[()\[\]（）【】]")
_NONSTD = re.compile(r"\b(HQ|4K)\b", re.IGNORECASE)

# 视频编码：压制场景用 x264/H.264，原盘与 REMUX 场景用 AVC/HEVC
_VIDEO_ENCODE = {"h264": "H.264", "h.265": "H.265", "h265": "H.265", "avc": "H.264",
                 "x264": "x264", "x265": "x265", "av1": "AV1", "xvid": "Xvid"}
_VIDEO_DISC = {"h264": "AVC", "avc": "AVC", "h265": "HEVC", "hevc": "HEVC",
               "mpeg2": "MPEG2", "vc-1": "VC-1", "vc1": "VC-1",
               "x264": "x264", "x265": "x265"}

_SOURCE = {"bluray": "BluRay", "blu-ray": "BluRay", "blu_ray": "BluRay",
           "uhd bluray": "UHD BluRay", "uhd blu-ray": "UHD BluRay",
           "remux": "BluRay REMUX", "bluray remux": "BluRay REMUX",
           "uhd bluray remux": "UHD BluRay REMUX", "4k remux": "UHD BluRay REMUX",
           "bdrip": "BDRip", "webdl": "WEB-DL", "web-dl": "WEB-DL",
           "webrip": "WEB-DL", "hdtv": "HDTV", "dvd": "DVD", "dvdiso": "DVD"}

_AUDIO = {"eac3": "DDP", "ddp": "DDP", "dd+": "DDP", "ac3": "DD", "dd": "DD",
          "truehd": "TrueHD", "dts": "DTS", "dts-hd ma": "DTS-HD MA",
          "dts-hd-ma": "DTS-HD MA", "dts-hd": "DTS-HD MA", "dtsx": "DTS-X",
          "dts-x": "DTS-X", "aac": "AAC", "flac": "FLAC", "lpcm": "LPCM",
          "pcm": "LPCM", "opus": "Opus", "mp3": "MP3"}

_ATMOS = {"truehd": "TrueHD Atmos", "ddp": "DDP Atmos", "eac3": "DDP Atmos"}


def _norm(value, table, field):
    key = (value or "").strip().lower()
    if key not in table:
        raise ValueError("未知的%s: %r" % (field, value))
    return table[key]


def normalize_video(codec, scene="encode"):
    """把视频编码规范成站点要求的写法。

    scene: "encode"（压制）或 "disc"（原盘/REMUX）。
    """
    table = _VIDEO_DISC if scene == "disc" else _VIDEO_ENCODE
    return _norm(codec, table, "视频编码")


def normalize_audio(codec, channels=None, atmos=False):
    """规范音频写法并拼上声道，如 DDP5.1 / DTS-HD MA 5.1。"""
    key = (codec or "").strip().lower()
    name = _ATMOS.get(key, "") if atmos else ""
    if not name:
        name = _norm(codec, _AUDIO, "音频编码")
    if channels:
        name += str(channels).strip()
    return name


def normalize_source(source):
    return _norm(source, _SOURCE, "来源")


def build_movie_title(name, year, source, resolution, video, audio, group=None,
                      region=None, scene="encode"):
    """电影主标题：英文名 年份 [地区/版本] 来源 解析度 视频编码 音频编码-小组。"""
    parts = [(name or "").strip(), str(year).strip()]
    if region:
        parts.append(region.strip())
    parts += [normalize_source(source), str(resolution).strip(),
              normalize_video(video, scene), audio.strip()]
    title = " ".join([p for p in parts if p])
    if group:
        title += "-" + group.strip()
    return title


def build_music_title(artist, album, year, codec, split, group=None):
    """无损音乐主标题：藝人名稱 - 專輯名稱 年份 音頻編碼 整軌/分軌-小組名稱。"""
    if split not in ("整軌", "分軌"):
        raise ValueError("split 只能是 整軌 或 分軌，收到 %r" % (split,))
    title = "%s - %s %s %s %s" % ((artist or "").strip(), (album or "").strip(),
                                  str(year).strip(),
                                  _norm(codec, _AUDIO, "音频编码"), split)
    if group:
        title += "-" + group.strip()
    return title


def validate_title(title, allow_cjk=False):
    """返回违规原因列表，空列表表示通过形状校验。"""
    problems = []
    text = (title or "").strip()
    if not text:
        return ["标题为空"]
    if _CJK.search(_MUSIC_SPLIT.sub("", text)) and not allow_cjk:
        problems.append("主标题含中日韩字符（仅 HACG 类在管理特许下可用）")
    if _EXT.search(text):
        problems.append("主标题含文件扩展名")
    if _BRACKET.search(text):
        problems.append("主标题含括号（HGame/HComic/HAnime 或片名本身含括号除外）")
    stripped = _LEGAL_DOT.sub("", text)
    if _BANNED_PUNCT.search(stripped):
        problems.append("主标题含禁止符号（! ; # $ %）")
    if "." in stripped:
        problems.append("主标题含非编码用途的点号")
    if not _YEAR.search(text):
        problems.append("主标题缺少年份")
    if _NONSTD.search(text):
        problems.append("主标题含 HQ/4K 等非标准字段")
    return problems


def validate_subtitle(subtitle):
    """副标题只做最低限度检查：禁止全角标点。"""
    problems = []
    for ch in subtitle or "":
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E or code in (0x3002, 0x300C, 0x300D, 0x3010, 0x3011):
            problems.append("副标题含全角标点: %r" % ch)
            break
    return problems


def selftest():
    """离线自检：阳性样本必须通过，阴性样本必须报出对应原因。"""
    ok_movie = build_movie_title("Moneyball", 2011, "bluray", "2160p", "x265",
                                 normalize_audio("DTS-HD MA", "5.1"), "MTeam",
                                 scene="encode")
    assert ok_movie == "Moneyball 2011 BluRay 2160p x265 DTS-HD MA5.1-MTeam", ok_movie
    assert validate_title(ok_movie) == [], validate_title(ok_movie)

    ok_disc = build_movie_title("Jason X", 2001, "uhd bluray", "2160p", "hevc",
                               normalize_audio("DTS-HD MA", "5.1"), "MTeam", scene="disc")
    assert ok_disc == "Jason X 2001 UHD BluRay 2160p HEVC DTS-HD MA5.1-MTeam", ok_disc
    assert validate_title(ok_disc) == []

    ok_music = build_music_title("Kidney", "Better Late Than Never", 2014, "flac", "分軌")
    assert ok_music == "Kidney - Better Late Than Never 2014 FLAC 分軌", ok_music
    assert validate_title(ok_music) == [], validate_title(ok_music)

    # 阴性对照：每个样本都必须命中预期原因
    cases = [
        ("蜘蛛侠 2026 BluRay 2160p H.265 DDP5.1", "中日韩"),
        ("Moneyball 2011 BluRay 2160p H.265 DDP5.1.mkv", "扩展名"),
        ("Moneyball (2011) BluRay 2160p H.265 DDP5.1", "括号"),
        ("Moneyball 2011 BluRay 2160p H.265 DDP5.1!", "禁止符号"),
        ("Moneyball BluRay 2160p H.265 DDP5.1", "年份"),
        ("Moneyball 2011 4K BluRay 2160p H.265 DDP5.1", "非标准字段"),
    ]
    for text, expect in cases:
        got = " ".join(validate_title(text))
        assert expect in got, "%r 期望命中 %r，实际 %r" % (text, expect, got)

    # 副标题全角标点必须被拦下
    assert validate_subtitle("再见，怦然心动") != []
    assert validate_subtitle("2014 Reissue") == []

    # 未知编码必须显式报错，不能静默放行
    for call in (lambda: normalize_video("vp9"), lambda: normalize_source("hddvd"),
                 lambda: normalize_audio("vorbis")):
        try:
            call()
        except ValueError:
            continue
        raise AssertionError("未知枚举未抛错")

    return "mteam_title selftest OK"


if __name__ == "__main__":
    print(selftest())
