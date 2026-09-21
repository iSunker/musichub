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
# ★★ 2026-09-21 订正：原先这里写着「剩下 5 项故意不映射（Musopia 没有对应标签）」
#    —— **那句话是错的**，是我在手上没有真实表单时的旧判断。
#    真实证据：乌托邦 `takeupload.php` 的 `tags[4][]` **实测 44 项**（逐行读出，
#    见 `descr.TAGS` 与 `SUMMARY #16`）。皇后那 27 项**全都能在乌托邦找到对应**，
#    只是当初写映射表时手上没有这张表单。
#    ⇒ 现在把「有实测依据」的补进来；仍无依据的**留作 unmapped**（不静默丢、不硬凑）。
#
# ★ 补进来的判据是**双向的**：乌托邦真实标签表里**确有此名**（值也取自那张表）。
#   这一条很重要 —— 原先 `TAG_NAME_MAP` 把「器乐」映射过去时，乌托邦侧其实**也有**
#   器乐（值 34），当时能对上纯属巧合好运气；现在的每个右边都能逐一对回 44 项表。
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
    # ★★ 以下 11 项是 2026-09-21 拿到真实表单后补上的（原先全落 unmapped）。
    #    右边一律是**乌托邦真实标签里的名字**（`descr.TAGS` 有编号才算数）。
    "汽车": "汽车",              # 乌托邦 28 garage
    "民族": "民族",              # 26 National
    "天籁": "天籁",              # 18 Soul
    "新世纪": "新世纪",           # 19 NewAge
    "蓝调": "蓝调",              # 22 Blues
    "朋克": "朋克",              # 25 Punk
    "宗教": "宗教",              # 29 Religion
    "雷鬼": "雷鬼",              # 21 Reggae
    "说唱": "说唱",              # 20 Rap
    "音乐剧": "音乐剧",           # 27 musical
    # ★★ 「贝斯」这一项是**用字差异**，不是缺标签 —— 最容易看漏的一格：
    #    皇后写 `贝斯(Drum Bass)`（空格），乌托邦写 `贝斯(Drum&Bass)`（&）。
    #    `_strip_suffix()` 会把两边括号都剥掉 ⇒ 剥完都叫「贝斯」，正好接上。
    #    （这是 `SUMMARY §12` 里那个「贝斯没有对应」旧结论的订正。）
    "贝斯": "贝斯",              # 14 Drum&Bass
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
    """
    `CD` / `SACD` / `DVD` / `VCD` / `Web` → Musopia `medium_sel` 的编号。

    ★★ 2026-09-21 修：**`Web` 原先被静默当成 `CD`**（因为乌托邦的 `MEDIUM` 表里
      没有 Web，而本函数最后一行 `return MEDIUM["CD"]` 是**无条件兜底**）。
      实测依据：三个皇后页面写着 `<td>媒介：</td><td>Web</td>`（Web 来源发行，
      **不是实体 CD**）⇒ 发 `CD(8)` 是**写了一个错的事实**。

    ★ 映射规则（按**名字**，同标签/制作组的纪律）：
      · `CD`/`SACD`/`DVD`/`VCD` → 同名项
      · `Web` → **`Other(12)`**：它不是实体媒介，乌托邦没有 Web 这一项，
        `Other` 是站点**真有**的选项，且「其它」正是对 Web 最诚实的表述。
      · 拿不到（空 / 认不出）→ **`0`（请选择一项）**，**不再无条件兜底成 CD**。
        ★ 这是本函数最要紧的行为改变：**"取不到" 不许长得像 "是 CD"**。
    """
    blob = f"{opencd_medium or ''} {opencd_format or ''}".upper()
    for key in ("SACD", "DVD", "VCD"):
        if key in blob:
            return descr.MEDIUM.get(key, "0")
    if "CD" in blob:
        return descr.MEDIUM["CD"]
    # ★ Web 来源：非实体媒介 ⇒ 落「其它」，不是 CD。
    if "WEB" in blob:
        return descr.MEDIUM["OTHER"]
    # ★ 认不出/为空 ⇒ 不指定（0），**不猜成 CD**。
    return "0"


# ==================== 制作组 team_sel[4] ====================
#
# ★★ 和标签同一个病：**两套编号完全错位**，必须**按名字**映射，不能搬数字。
#
#   实测两边的下拉（都是逐行读出，非推断）：
#
#     皇后 `OpenCD __ 發布.html`  `<select name="team">`：
#        0 請選擇 / 7 OpenCD / 8 LLM / 9 TSxD / 6 KHQ / 5 其他(Other)
#     乌托邦 `音乐乌托邦 __ 发布.html` `<select name="team_sel[4]">`：
#        0 请选择一项 / 6 Musopia / 5 Other / 1 HDS / 2 CHD / 3 MySiLU / 4 WiKi
#
#   ★ 最危险的一格：皇后写 `其他(Other)` = **5**，乌托邦的 `Other` **也** = 5
#     —— 这两个 5 纯属巧合相等，**不能靠"值一样"来匹配**。
#     若哪天站点改号，靠值匹配就会**静默错标**（同 `opencd_map` 存在的理由）。
#   ★ 反过来，皇后 `OpenCD` = 7、乌托邦 `Musopia` = 6：
#     「把从 OpenCD 转载的种子标成 OpenCD 制作组」在乌托邦侧**没有对应项**
#     （乌托邦只有自己的 `Musopia` 和友站 HDS/CHD/MySiLU/WiKi），
#     且**语义也不该混**（我们不是那批制作组的人）
#     ⇒ `OpenCD` 这一项**映射到 `Other`**：皇后页面自己写的 `其他(Other)`
#       正是 `Other`，而样本实测 `team = '其他(Other)'` ⇒ 两边本来就一致。
#   ★ 取不到就返回 `"0"`（= 请选择一项），**不猜**。
TEAM_NAME_MAP = {
    "其他": "OTHER",
    "其他(Other)": "OTHER",
    "Other": "OTHER",
    "OpenCD": "OTHER",      # ★ 语义见上：不冒充制作组，落到「其他」
    "請選擇": "OTHER",
    # 以下三个乌托邦**没有对应项**，留着备用但**当前只作注释**（不放进映射，
    # 否则等于凭空把 LLM/TSxD/KHQ 认成乌托邦的 HDS/CHD/WiKi —— 那是编数据）。
    #   "LLM":  ?, "TSxD": ?, "KHQ": ?,
}


def map_team(opencd_team):
    """
    皇后「製作組」名字 → Musopia `team_sel[4]` 的编号。

    ★ 按**名字**映射（同 `map_tags`）。取不到给 `"0"`（请选择一项），**不猜相近的**。

    ★ 判据（`--selftest` 里验）：
      · `其他(Other)` → `5`（Other）—— ★ 两边数字恰好都是 5，但**理由是名字**
      · `OpenCD`      → `5`（Other）—— ★ 不是 7，也不是乌托邦的 Musopia(6)
      · 认不出的       → `"0"`，且**不挑一个近的**
    """
    name = (opencd_team or "").strip()
    if not name:
        return "0"
    key = TEAM_NAME_MAP.get(name) or TEAM_NAME_MAP.get(_strip_suffix(name))
    if key is None:
        return "0"
    return descr.TEAM.get(key, "0")


def map_all(info):
    """
    把 `opencd_parse.TorrentInfo` 映射成 Musopia 表单要用的字段。

    返回 dict，含：
      · `tags`      —— tag value 列表（可直接填 `tags[4][]`）
      · `unmapped`  —— ★ 映射不上的标签名（**调用方必须显式处理**）
      · `audiocodec_sel` / `medium_sel`
      · `title` / `small_descr` / `tracklist` / `log_content` —— 直接透传（渲染用）
      · `artist` / `year` / `source` —— 透传（★ `year` 带「年」字，站点要不要纯数字待定）

    ★ 本函数**不碰网络、不写库、不渲染** —— 只做字段搬运与翻译，方便单测。
    """
    tags, unmapped = map_tags(info.get("tags") or [])
    return {
        "tags": tags,
        "unmapped": unmapped,
        "audiocodec_sel": map_audiocodec(info.get("format")),
        "medium_sel": map_medium(info.get("medium"), info.get("format")),
        # ★ 制作组：**按名字**映射（皇后 `其他(Other)` → 乌托邦 `Other=5`）。
        #   取不到给 "0"（请选择一项）。原先恒发 "0" 是"没做"，不是"做不了"。
        "team_sel": map_team(info.get("team")),
        "title": info.get("name") or "",
        "small_descr": info.get("small_descr") or "",
        "tracklist": info.get("tracklist") or "",
        "log_content": info.get("log_content") or "",
        # ★ 艺术家 / 发行年份：**透传，不改写**。落位见 descr.build_form_fields
        #   （`custom_fields[4][1]` / `[4][2]`）。取不到就是 ""，调用方原样发空。
        "artist": info.get("artist") or "",
        "year": info.get("year") or "",
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
    # ★★ 以下 11 项 2026-09-21 **订正**：原先断言它们落 unmapped，那是错的 ——
    #    拿到乌托邦真实表单后发现**乌托邦全都有这些标签**（见 TAG_NAME_MAP 上方注释）。
    ("★ 订正：贝斯(Drum Bass) → 14（乌托邦写 Drum&Bass，靠剥括号接上）",
     "贝斯(Drum Bass)", "14", False),
    ("★ 订正：天籁(Soul) → 18", "天籁(Soul)", "18", False),
    ("★ 订正：新世纪(NewAge) → 19", "新世纪(NewAge)", "19", False),
    ("★ 订正：说唱(Rap) → 20", "说唱(Rap)", "20", False),
    ("★ 订正：雷鬼(Reggae) → 21", "雷鬼(Reggae)", "21", False),
    ("★ 订正：蓝调(Blues) → 22", "蓝调(Blues)", "22", False),
    ("★ 订正：朋克(Punk) → 25", "朋克(Punk)", "25", False),
    ("★ 订正：民族(National) → 26", "民族(National)", "26", False),
    ("★ 订正：音乐剧(musical) → 27", "音乐剧(musical)", "27", False),
    ("★ 订正：汽车(garage) → 28", "汽车(garage)", "28", False),
    ("★ 订正：宗教(Religion) → 29", "宗教(Religion)", "29", False),
    # ★ 阴性对照：**真**认不出的仍然必须落 unmapped（不许为了"好看"硬凑）
    ("阴性对照：不存在的标签 → 必须落 unmapped", "根本不存在的标签", None, True),
]

CODEC_CASES = [
    ("FLAC 分轨", "1"), ("APE", "2"), ("WAV 整轨", "0"),
    ("MP3", "4"), ("AAC", "6"), ("", "0"),
]

MEDIUM_CASES = [
    ("CD", "", "8"), ("SACD", "", "9"), ("DVD", "", "10"),
    ("", "FLAC 分轨", "0"),          # ★ 修订：认不出不再兜底成 CD
    ("Web", "", "12"),               # ★★ Web → Other（原先错成 CD）
    ("Web", "FLAC 分轨", "12"),      # ★ 格式里没有 CD 字样时也一样
    ("", "WAV 整轨", "0"),           # ★ 只给格式、且不含 CD ⇒ 不猜
]

# ★ 制作组：★ 按**名字**映射，不是搬数字。皇后 `其他(Other)`=5 / 乌托邦 `Other`=5
#   —— 数字**恰好相等纯属巧合**，所以这里刻意写「名字 → 编号」而不是「5 → 5」。
TEAM_CASES = [
    ("其他(Other) → 5（乌托邦 Other）", "其他(Other)", "5"),
    ("★ OpenCD → 5（**不是 7**，也不是乌托邦 Musopia=6）", "OpenCD", "5"),
    ("其他 → 5（无括号形态也要接住）", "其他", "5"),
    ("LLM → 0（乌托邦没有对应项，**不猜成 HDS/CHD/WiKi**）", "LLM", "0"),
    ("空 → 0", "", "0"),
    ("认不出的 → 0，**不挑一个近的**", "莫名其妙的组", "0"),
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

    # ★★ 两张表不许漂移：TAG_NAME_MAP 的**每个右边**都必须能在 descr.TAGS 里找到编号。
    #    理由：`map_tags()` 遇到「表里有名、descr.TAGS 里没编号」时会把它塞进 unmapped
    #    —— 那是**响了**，但如果没人跑自检就没人听见。这里把它变成**硬判据**。
    print("\n①b 表一致性 —— TAG_NAME_MAP 的每个右边都要在 descr.TAGS 里有编号")
    for oc, mu in sorted(TAG_NAME_MAP.items()):
        good = mu in descr.TAGS
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} 皇后「{oc}」→ 乌托邦「{mu}」"
              f" → 编号 {descr.TAGS.get(mu, '❌ 不存在！')}")

    # ★★ 覆盖率判据（**这次是真结论，不是感觉**）：皇后的 27 个标签**全部**要能映射。
    #    ★ 这条会随着皇后/乌托邦任一边改变而失效 ⇒ 失效时应当**修表**，
    #      不是把这条判据删掉（删判据 = 回到"少几个标签没人知道"的老状态）。
    print("\n①c 覆盖率 —— 皇后 27 个标签应当**全部**映射得上")
    missed = [n for n in opencd_parse.OPENCD_TAGS if not (
        TAG_NAME_MAP.get(n) or TAG_NAME_MAP.get(_strip_suffix(n)))]
    good = not missed
    ok, fails = ok + good, fails + (not good)
    print(f"  {'✅' if good else '❌'} 未映射的皇后标签：{missed or '（无，全部覆盖）'}")

    print("\n② 音频编码映射 —— 取不到给 0（不指定），不猜")
    for src, want in CODEC_CASES:
        got = map_audiocodec(src)
        good = got == want
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} {src!r:<16} → {got!r:<4} 期望 {want!r}")

    print("\n③ 媒介映射 —— ★ Web→Other，取不到给 0（**不再兜底成 CD**）")
    for med, fmt, want in MEDIUM_CASES:
        got = map_medium(med, fmt)
        good = got == want
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} {med!r}/{fmt!r:<12} → {got!r:<4} 期望 {want!r}")

    print("\n③b 制作组映射 —— ★ 按**名字**，不是搬数字（两边恰好都是 5，别被骗）")
    for label, src, want in TEAM_CASES:
        got = map_team(src)
        good = got == want
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} {label:<48} → {got!r:<4} 期望 {want!r}")

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
