"""
OpenCD（皇后）字段 → Musopia 表单字段的映射。**纯离线、零请求。**

★ 为什么单独一个模块：这是第 4 步里**最容易出静默错误**的地方，而静默错误
  在本项目已经栽过多次（`SUMMARY §6` 那一族）。集中一处才好审计、才好写判据。

═══ 这个模块存在的唯一理由：两套标签编号完全错位 ═══

实测对照（`SUMMARY §12` 第 4 步）：

    | 标签名   | 皇后值 | Musopia 值 |
    |---------|-------|-----------|
    | 大陆     |   1   |     4     |
    | 欧美     |   2   |     5     |
    | 港台     |   3   |     6     |
    | 日韩     |   4   |     7     |
    | 古典     |   9   |    15     |
    | 器乐     | 没有  |    34     |

  ⇒ **必须按「标签名」映射，绝不能把皇后的数字直接填给 Musopia。**
    把皇后的 `1`（大陆）直接填过去，Musopia 会理解成 `1`（禁转）——
    不但标签错了，还会给每个转载种子打上「禁转」标。**这是本步最危险的错误。**

═══ 三条设计纪律 ═══

1. **按名字映射，不按数字。** 数字是各站自己的内部编号，跨站没有可比性。
2. **`()/（）` 里的英文后缀要剥掉再比**。皇后写 `流行(Pop)`，Musopia 写 `流行`。
   ★ 但**只在精确名查不到时才剥** —— 否则将来若有标签名本身带括号，
     会被误剥（先精确、后宽松，跟解码判据「先严后宽」同一个思路）。
3. **映射不上的必须显式列出，绝不静默丢。** 返回 `unmapped` 列表，
   调用方要把它写进每日邮件（`SUMMARY §12` 决定 9）。
   ★ 静默丢 = 种子发上去少几个标签，**没有任何报错**，只能靠人眼发现。
"""
import re

import descr                       # Musopia 侧的唯一真值来源（TAGS / MEDIUM / AUDIOCODEC）
import opencd_parse                # 皇后侧的标签表（OPENCD_TAGS）

# 剥英文后缀：`流行(Pop)` → `流行`；半角/全角括号都要认
_PAREN_SUFFIX = re.compile(r"[\(（][^)）]*[\)）]\s*$")


def _strip_suffix(name):
    """剥掉标签名尾部的 `(English)` 后缀。`流行(Pop)` → `流行`。"""
    return _PAREN_SUFFIX.sub("", name or "").strip()


# ==================== 标签映射 ====================
#
# ★ 这张表**按名字**把皇后标签接到 Musopia 标签上。
#   左边 = 皇后标签名（剥掉英文后缀后的形态），右边 = **Musopia 的标签名**（不是编号！）。
#   ★ 右边写名字而不是编号，是为了让 descr.TAGS 成为**唯一编号来源** ——
#     若哪天 Musopia 改了编号，只改 descr.py 一处即可，这张表不用动。
#     （本项目「同一事实只在一处维护」的成文约定，见 A.11。）
#
# 皇后 27 项里，能对上的 22 项；剩下 5 项**故意不映射**（不是遗漏）：
#   · 天籁(Soul)/蓝调(Blues)/新世纪(NewAge)/宗教(Religion)/雷鬼(Reggae)
#     贝斯(Drum Bass)/说唱(Rap)/音乐剧(musical) —— 这些**Musopia 没有对应标签**
#   ⇒ 它们会落进 `unmapped`，由调用方显式列出。**不硬凑一个相近的**：
#     把「雷鬼」塞成「电子」是制造假数据，比少一个标签糟得多。
TAG_NAME_MAP = {
    # 地区
    "大陆": "大陆",
    "欧美": "欧美",
    "港台": "港台",
    "日韩": "日韩",
    "其它地区": "其他地区",        # ★ 注意「其它」→「其他」，两边用字不同
    # 风格（能对上的）
    "流行": "流行",
    "摇滚": "摇滚",
    "爵士": "爵士",
    "电子": "电子",
    "金属": "金属",
    "古典": "古典",
    "原声": "原声",
    "发烧": "发烧",
    "民间": "民间",
    "乡村": "乡村",
    "儿童": "儿童",
    "器乐": "器乐",              # ★ 皇后表里**没有**这个标签，但 Musopia 有 —— 留着备用
}


def map_tags(opencd_names):
    """
    皇后标签名列表 → (musopia_values, unmapped)。

    `musopia_values` 是 Musopia 的 **tag value 列表**（`descr.TAGS` 里的编号字符串），
    可直接填进 `tags[4][]`。
    `unmapped` 是**映射不上的皇后标签名**（原样，含英文后缀）——
    调用方必须显式处理（进每日邮件），**不许静默丢**。

    ★ 判据（`--selftest` 里逐条验）：
      · `大陆` → `4` **不是** `1`（这是本模块存在的理由）
      · `流行(Pop)` → `10`（后缀被剥掉）
      · `其它地区` → `8`（「其它/其他」用字差异被接住）
      · 认不出的 → 进 unmapped，**且不猜**
    """
    values, unmapped = [], []
    for raw in opencd_names or []:
        name = (raw or "").strip()
        if not name:
            continue
        # ① 先精确比；② 不成再剥括号后缀比（先严后宽）
        mu = TAG_NAME_MAP.get(name) or TAG_NAME_MAP.get(_strip_suffix(name))
        if mu is None:
            unmapped.append(name)              # ★ 显式记录，不静默丢
            continue
        val = descr.TAGS.get(mu)
        if val is None:
            # 映表里有、但 descr.TAGS 里没有 ⇒ 两处漂移了，必须响亮地坏
            unmapped.append(f"{name}(映射表→{mu}，但 descr.TAGS 里没有)")
            continue
        if val not in values:                  # 去重（一个种子可能多个标签映到同一个）
            values.append(val)
    return values, unmapped


# ==================== 其余字段 ====================
#
# 格式/媒介：皇后的值形如 `FLAC 分轨` / `WAV 整轨` / `CD`，
# 而 Musopia 的 audiocodec_sel / medium_sel 只要一个编号。
# ★ 这里**只做识别，不做推断** —— 推断归 descr 的 detect_*（发布路径本来就在用）。
#   本函数面对的是「皇后页面已经写明的值」，那是事实，不是要猜的东西。

def map_audiocodec(opencd_format):
    """
    `FLAC 分轨` / `WAV 整轨` / `APE` … → Musopia `audiocodec_sel` 的编号。

    ★ 取不到就返回 `"0"`（= 不指定），**不猜一个相近的**。
      `descr.detect_audiocodec()` 是另一条路（从标题/文件名猜），
      两者用途不同：这里是「页面已写明」，那里是「从文件名推断」。

    ★★ `WAV` 是**已知缺口**（2026-09-21 实测）：原先关键词表里**没有 WAV**
      ⇒ 皇后的 `WAV 整轨` 一律得到 `"0"`（= 不指定），而 2026-09-19 发布的那 5 条里
      **4 条是 WAV** ⇒ 编码这格**系统性为空**。
      · 参考实现 `repos/auto_feed_js/auto_feed.user.js:2561` **显式认 WAV**；
      · 但**编号**仍是未知的（`descr.AUDIOCODEC` 实测自 `edit.php`，表里没有 WAV）
        ⇒ **识别出来、仍返回 `"0"`**，并在返回处留可见的痕迹（见下面 return 的注释），
        而不是让它和"没认出格式"长得一模一样。
      ⇒ 补编号是**第二步**，等 `audiocodec_sel` 的真实 `<option>` 值确认后再做。
    """
    blob = (opencd_format or "").upper()
    for key in ("FLAC", "APE", "DTS", "AAC", "OGG", "MP3"):
        if key in blob:
            return descr.AUDIOCODEC.get(key, "0")
    # ★ WAV：**认得出，但没有编号** ⇒ 仍返回 "0"（不指定）。
    #   这条分支的目的只是**让缺口可见** —— 见 `descr.AUDIOCODEC_UNKNOWN_OK`。
    if "WAV" in blob:
        return descr.AUDIOCODEC.get("WAV", "0")
    return "0"


def map_medium(opencd_medium, opencd_format=""):
    """`CD` / `SACD` / `DVD` / `VCD` → Musopia `medium_sel` 的编号。取不到给 CD。"""
    blob = f"{opencd_medium or ''} {opencd_format or ''}".upper()
    for key in ("SACD", "DVD", "VCD"):
        if key in blob:
            return descr.MEDIUM.get(key, descr.MEDIUM["CD"])
    return descr.MEDIUM["CD"]


def map_all(info):
    """
    把 `opencd_parse.TorrentInfo` 映射成 Musopia 表单要用的字段。

    返回 dict，含：
      · `tags`      —— tag value 列表（可直接填 `tags[4][]`）
      · `unmapped`  —— ★ 映射不上的标签名（**调用方必须显式处理**）
      · `audiocodec_sel` / `medium_sel`
      · `title` / `small_descr` / `tracklist` / `log_content` —— 直接透传（渲染用）

    ★ 本函数**不碰网络、不写库、不渲染** —— 只做字段搬运与翻译，方便单测。
    """
    tags, unmapped = map_tags(info.get("tags") or [])
    return {
        "tags": tags,
        "unmapped": unmapped,
        "audiocodec_sel": map_audiocodec(info.get("format")),
        "medium_sel": map_medium(info.get("medium"), info.get("format")),
        "title": info.get("name") or "",
        "small_descr": info.get("small_descr") or "",
        "tracklist": info.get("tracklist") or "",
        "log_content": info.get("log_content") or "",
        # 原样保留，不改写、不渲染成「转载自 OpenCD」（SUMMARY §12 决定 2）
        "source": info.get("source") or "",
    }


# ==================== 自检 ====================
#
# ★ 期望值**人工核过**再写进来（不是从映射结果反填 —— 那样是自证）。

TAG_CASES = [
    # (说明, 输入的皇后标签名, 期望的 Musopia 编号, 是否期望落 unmapped)
    ("大陆 → 4（★ 不是 1！本模块存在的理由）", "大陆", "4", False),
    ("欧美 → 5（★ 不是 2）", "欧美", "5", False),
    ("港台 → 6（★ 不是 3）", "港台", "6", False),
    ("日韩 → 7（★ 不是 4）", "日韩", "7", False),
    ("古典(Classical) → 15（★ 不是 9；后缀要剥掉）", "古典(Classical)", "15", False),
    ("摇滚(rock) → 9", "摇滚(rock)", "9", False),
    ("其它地区 → 8（★「其它」↔「其他」用字不同）", "其它地区", "8", False),
    ("流行(Pop) → 10", "流行(Pop)", "10", False),
    ("器乐 → 34（皇后没这标签，但 Musopia 有）", "器乐", "34", False),
    ("新世纪(NewAge) → 无对应，进 unmapped", "新世纪(NewAge)", None, True),
    ("雷鬼(Reggae) → 无对应，进 unmapped（★ 不硬凑成电子）", "雷鬼(Reggae)", None, True),
    ("说唱(Rap) → 无对应，进 unmapped", "说唱(Rap)", None, True),
]

CODEC_CASES = [
    ("FLAC 分轨", "1"), ("APE", "2"), ("WAV 整轨", "0"),
    ("MP3", "4"), ("AAC", "6"), ("", "0"),
]

MEDIUM_CASES = [
    ("CD", "", "8"), ("SACD", "", "9"), ("DVD", "", "10"),
    ("", "FLAC 分轨", "8"),
]


def _check():
    ok = fails = 0

    print("① 标签映射 —— ★ 核心判据是「按名字映射，不搬数字」")
    for label, name, want_val, want_unmapped in TAG_CASES:
        vals, un = map_tags([name])
        got_val = vals[0] if vals else None
        got_unmapped = bool(un)
        good = (got_val == want_val) and (got_unmapped == want_unmapped)
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} {label:<48} "
              f"→ {got_val!r:<6} unmapped={got_unmapped}")

    print("\n② 音频编码映射 —— 取不到给 0（不指定），不猜")
    for src, want in CODEC_CASES:
        got = map_audiocodec(src)
        good = got == want
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} {src!r:<16} → {got!r:<4} 期望 {want!r}")

    print("\n③ 媒介映射 —— 默认 CD")
    for med, fmt, want in MEDIUM_CASES:
        got = map_medium(med, fmt)
        good = got == want
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} {med!r}/{fmt!r:<12} → {got!r:<4} 期望 {want!r}")

    print("\n④ 端到端：拿真实样本走一遍 map_all()")
    import glob
    import os
    sample = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "opencdhtml", "sanitized", "sample-zhuanzai-jinyong.html")
    if not os.path.exists(sample):
        print("  ⚠️  缺样本，跳过（挂载 opencdhtml 后再跑）")
        return ok, fails
    with open(sample, encoding="utf-8") as fh:
        info = opencd_parse.parse_detail(fh.read())
    m = map_all(info)
    # ★ 该样本实测标签：['大陆', '港台', '古典(Classical)']
    checks = [
        ("大陆→4 在结果里", "4" in m["tags"]),
        ("★ 皇后的 1（大陆）没被直接搬过来", "1" not in m["tags"]),
        ("港台→6 在结果里", "6" in m["tags"]),
        ("古典→15 在结果里", "15" in m["tags"]),
        ("没有 unmapped（这三项都能对上）", m["unmapped"] == []),
        ("medium_sel = 8（CD）", m["medium_sel"] == "8"),
        ("codec = 0（WAV 整轨 → 不指定）", m["audiocodec_sel"] == "0"),
        ("source 原样保留（原抓）", m["source"] == "原抓"),
    ]
    for label, good in checks:
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} {label}")
    print(f"     实测 tags={m['tags']} unmapped={m['unmapped']}")

    return ok, fails


def main():
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="OpenCD → Musopia 字段映射")
    ap.add_argument("--selftest", action="store_true", help="跑离线自检")
    ap.add_argument("file", nargs="?", help="详情页 HTML（打印映射结果）")
    args = ap.parse_args()

    if args.selftest:
        print("=" * 74)
        print("opencd_map 离线自检（纯离线，零请求）")
        print("=" * 74)
        ok, fails = _check()
        print("\n" + "=" * 74)
        if fails:
            print(f"❌ 自检失败：{ok}/{ok + fails} 通过，{fails} 项不符")
            return 1
        print(f"✅ 自检通过：{ok}/{ok} 项全对")
        return 0

    if not args.file:
        ap.error("给个 HTML 文件，或加 --selftest")
    with open(args.file, encoding="utf-8", errors="replace") as fh:
        info = opencd_parse.parse_detail(fh.read())
    m = map_all(info)
    print(f"  皇后标签   = {info.get('tags')}")
    print(f"  → tags     = {m['tags']}  （Musopia 的 tag value）")
    print(f"  → unmapped = {m['unmapped']}")
    print(f"  codec/medium = {m['audiocodec_sel']} / {m['medium_sel']}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
