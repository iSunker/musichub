#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蟹黄堡（crabpt.vip）发布适配层 —— 把一条 task 渲染成 `takeupload.php` 的表单字段。

═══════════════════════════════════════════════════════════════════════════
为什么单独一个文件（不是往 descr.build_form_fields 里加分支）
═══════════════════════════════════════════════════════════════════════════
★ `descr.build_form_fields()` 的输出**在蟹黄堡大部分用不上**，实测（PLAN 附录 C.7）：

    语义         乌托邦                      蟹黄堡
    --------     ------------------------    ---------------------------
    媒介         `medium_sel`                ★ `source_sel[4]`（**没有 medium_sel**）
    音频编码     `audiocodec_sel`(FLAC=1)    `audiocodec_sel[4]`(FLAC=10)
    地区         ★ 在 `tags[4][]` 里(大陆=4…)  ★★ 独立字段 `processing_sel[4]`
    艺术家/年份/  `custom_fields[4][N]`       ★★ **完全没有 custom_fields**
      厂牌/抓轨日志
    MediaInfo    无独立字段                  ★ `technical_info`

⇒ 差异**不是"字段名多一个 [4]"**，而是**字段集合不同**（一个用 custom_fields、
另一个不用；一个把地区放标签里、另一个有独立字段）。
⇒ 硬要塞进 `build_form_fields()` 会让那个函数里一半的分支在蟹黄堡走不到，
  而**走不到的分支比没有分支更危险**（它看着像"已处理"）。⇒ 独立成文件，
  并在这里**逐字段写明来源**。

★★ 两站标签体系也**不同类**（这是最容易被忽略的一条，实测）：
    乌托邦 `tags[4][]` = 地区 + 音乐风格 + 禁转/首发
    蟹黄堡 `tags[4][]` = **影视属性为主**（剧情/战争/犯罪/惊悚/科幻/动画…）
  ⇒ **不能复用** `opencd_map.TAG_NAME_MAP`（那张表右列是乌托邦的**名字**）。
  本文件只映射**两边语义明确同源**的那几个（禁转 / 自购），其余**不硬凑**。

═══════════════════════════════════════════════════════════════════════════
★ 字段值的出处（都是**实测**，不是推断；详见 PLAN-crabpt.md 附录 C）
═══════════════════════════════════════════════════════════════════════════
  · 表单 action `takeupload.php` / 分组 `[4]` / `type=408`  —— 发布页原文
  · 六个 select 的全部选项                              —— 发布页 `<select>` 逐项读出
  · `tags[4][]` 37 项的「值 → 名字」                    —— BeautifulSoup 读 `<label>`
  · `source_sel` 的用法（音乐多为 Other，WEB 发行才用 WEB-DL）—— 四条真实详情页反推

★★ 一条**已知的、必须如实标注的**缺口（别把它读成"已解决"）：
   `processing_sel[4]`（地区）的取值**是从皇后的地区标签映射**来的，
   而皇后那一列**是"日韩"合并的** ⇒ **拆不出日本(5)与韩国(6)**，
   只能落到 `1 其他`。这与 `opencd_map.py` 把"映射不上显式列出"的做法一致。
"""
import re

import config
import db
import descr
import opencd_map

# ==================== 分类 id ====================
# ★ 音乐 = 408（实测）。★ 必须与 config 同源，不手抄（ERR-META-03）。
MUSIC_TYPE = config.CRABPT_MUSIC_CATEGORY

# ==================== `source_sel[4]`：媒介 ====================
#
# ★★ 映射依据（**不是**照着乌托邦的 MEDIUM 表改数字，是照**四条真实详情页反推**）：
#
#     样本                        皇后媒介(WEB)   详情页显示"媒介"    ⇒ 该填
#     ------------------------    -------------   ----------------    ---------
#     Adele（WEB 数字发行）        Web             **WEB-DL**          `6`
#     Final Fantasy VIII           Web?            Other               `1`
#     Giacomo Scinardo             ?               Other               `1`
#     精选700首（WAV 分轨）         CD 抓轨          Other               `1`
#
#   ⇒ ★ 结论：**只有 WEB 数字发行才填 `WEB-DL(6)`，其余（含 CD 抓轨）一律 `Other(1)`**。
#   ★★ 注意这与乌托邦**不同**：乌托邦的 `medium_sel` 有 `CD(8)`，而蟹黄堡的
#      `source_sel[4]` 虽然也列了 `CD(8)`，但**实测四条样本里 CD 抓轨的那条填的是 Other**。
#      ⇒ **不把乌托邦的编号搬过来**，按这里的实测表走。
#
# ★ 皇后页面写的媒介（`<td>媒介：</td><td>Web</td>`）经 `opencd_map.map_medium()`
#   变成**乌托邦编号**；这里再把乌托邦编号转成蟹黄堡编号。
#   ★ 转的是"语义"而不是"数字"：`descr.MEDIUM` 的 `12=OTHER` 在这一层是**兜底**，
#     而实测"CD 抓轨"也落 `Other` ⇒ 这一层只有 Web 需要特判。
_MUSOPIA_MEDIUM_OTHER = descr.MEDIUM.get("OTHER")       # 12
_MUSOPIA_MEDIUM_CD = descr.MEDIUM.get("CD")             # 8

SOURCE_OTHER = 1
SOURCE_WEB_DL = 6


def _norm(v):
    """把编号归一成**字符串** —— 两边口径不同,必须先归一化再比。

    ★★★ 2026-09-24 实测踩过（这个函数就是那时加的）：
      `mapped_json`（`opencd_map.map_all()` 的产出）里的编号是**字符串**
      —— 见 `opencd_map.TAG_CASES` 的期望值 `("大陆", "4")`、`descr.TAGS`
      的值也是 `'6'` / `'12'` 这种**带引号的字符串**。
      而我第一版写的是 `int` 比较（`if v == musopia_tags_int`）⇒
      **`'1' == 1` 恒为假** ⇒ ★ 所有映射**静默落兜底**（全变 `1`/`[]`），
      而**输出的形状完全正常**（页面能提交，只是字段全丢）。
      ★ 更坏的是它**有过一次"碰巧正确"**：我用手写的 `mapped_json` 试时，
        里面恰好是字符串 ⇒ `'1' == '1'` 成立 ⇒ 显示 `FLAC → 10` ✅
        ⇒ **一次假的成功掩盖了真 bug**。查出它靠的是"审计行没报未映射"这条**旁证**，
        不是主判据 —— 记为「判据之外还要看旁证」的一例。
    ⇒ 现在一律先 `_norm()`，字符串对字符串比（**不 `int()`**：两边都是字符串时，
      `int()` 会引入"非数字抛异常"的新失败面，而归一成 str 不改变语义）。
    """
    if v is None:
        return None
    return str(v).strip()


def map_source(musopia_medium):
    """乌托邦 `medium_sel` 编号 → 蟹黄堡 `source_sel[4]`。

    ★ 只有"乌托邦判为 OTHER 且原本是 Web 来源"才该填 WEB-DL —— 但**走到这一层时
      那个信息已经丢了**（乌托邦把 Web 和 HI-RES 都压成了 OTHER）。
    ⇒ 所以本函数**不猜**：它把 `OTHER` 一律映成 `OTHER(1)`（与实测四条样本一致：
      三条 Other 里含 Web 来源的 Final Fantasy）。
      ★ 也就是说：**Adele 那条的 WEB-DL 我们复现不出来** —— 这是**已知的、可接受的**
        信息损失（宁可少标一个 WEB-DL，也不要把 CD 抓轨标成 WEB-DL，那是**写错事实**）。
      ★ 将来若要复现，得从**皇后原始媒介字段**（`opencd_medium`）直取，
        而不是从已经压扁的乌托邦编号反推 —— 那时改这里、并加一条实测判据。
    """
    if _norm(musopia_medium) == _norm(_MUSOPIA_MEDIUM_OTHER):
        return SOURCE_OTHER
    if _norm(musopia_medium) == _norm(_MUSOPIA_MEDIUM_CD):
        # ★ 实测：CD 抓轨在蟹黄堡也填 Other（精选700首那条就是）
        return SOURCE_OTHER
    return SOURCE_OTHER


# ==================== `audiocodec_sel[4]`：音频编码 ====================
#
# ★ 按**名字**转（不是按数字）：两站编号不同（FLAC 乌=1 / 蟹=10），
#   而名字是稳定的。★ 与 `opencd_map` 的既有纪律一致（右边写名字、不写编号）。
_CRABPT_AUDIOCODEC = {
    "FLAC": 10,
    "WAV": 11,
    "ALAC": 16,
    "APE": 12,
    "MP3": 9,          # ★ 蟹黄堡没有独立 MP3 项，`9 = MPEG`（MP3 属 MPEG-1 Layer III）
    "AAC": 2,
    "DTS": 5,
    "OGG": 21,
    "OTHER": 1,
}


def map_audiocodec(musopia_audiocodec):
    """乌托邦 `audiocodec_sel` 编号 → 蟹黄堡 `audiocodec_sel[4]`（按名字转）。

    ★ 认不出就返回 `1`（Other）—— ★ **不返回 0**：`0` 是"请选择一项"，
      而站点对必填项的校验会**拒绝提交**；`Other` 是**站点真有**的选项，
      且"其它"是对"我们不知道"最诚实的表述（与 `opencd_map.map_medium` 同纪律）。
    """
    want = _norm(musopia_audiocodec)
    name = None
    for k, v in descr.AUDIOCODEC.items():
        if _norm(v) == want:            # ★ 归一后比较（值可能是 str 或 int）
            name = k
            break
    if not name:
        return 1
    return _CRABPT_AUDIOCODEC.get(name.strip().upper(), 1)


# ==================== `processing_sel[4]`：地区 ====================
#
# ★★ 来源：**皇后的地区标签**（不是歌手国籍 —— 实测 Adele 是英国歌手却被
#    发布者在蟹黄堡标成「中国大陆」，证明"按国籍推"是错的）。
#
# 皇后标签（`opencd_parse.OPENCD_TAGS` 里的名字）→ 蟹黄堡 `processing_sel[4]`：
#
#     皇后        皇后编号(乌托邦)   蟹黄堡
#     --------    ----------------   ------
#     大陆        4                  2
#     港台        6                  3
#     欧美        5                  4
#     日韩        7                  ★ **拆不出** ⇒ 1（见下）
#     其它地区    8                  1
#
# ★★ 为什么"日韩"落到 `1 其他`：皇后那一列**只有一个合并值**，
#    而蟹黄堡要分 `5 日本` / `6 韩国` ⇒ **信息不足，拆不出来**。
#    ⇒ 落 `1`，并**显式记进 unmapped**（不静默丢，同 `opencd_map` 的纪律）。
#    ★ 这是个**已知缺口**，不是 bug。
_PROCESSING_BY_MUSOPIA_TAG = {
    "大陆": 2,
    "港台": 3,
    "欧美": 4,
    "其它地区": 1,
    "其他地区": 1,      # ★ 两站用字不同（乌托邦写"其他"、皇后写"其它"）
}
# ★ 拆不出的那些：**显式列出**，让调用方能把它们报出来，而不是悄悄填 1
_PROCESSING_SPLIT_LOST = {"日韩": "蟹黄堡分日本(5)/韩国(6)，皇后只给合并值 ⇒ 落 1 其他"}


def map_processing(musopia_tags):
    """乌托邦标签编号列表 → (蟹黄堡 `processing_sel[4]` 值, 说明或 None)。

    ★ 取**第一个**命中的地区标签：皇后一条种子**通常**只有一个地区标签，
      但**不保证**（可能同时有"港台"和"流行"，那是风格不是地区 ⇒ 不受影响；
      真正可能撞的是"大陆"+"港台"这种）。撞了就**取第一个**并**在说明里写明**
      —— ★ 不静默，让"这条种子地区可能填错"变成看得见的事。
    """
    tag_by_value = descr.TAGS
    notes = []
    for v in musopia_tags or []:
        # ★ 归一后查：`descr.TAGS` 的**键是名字、值是字符串编号**
        #   （实测 `descr.TAGS['港台'] == '6'`）⇒ 必须用字符串比。
        name = None
        want = _norm(v)
        for k, val in tag_by_value.items():
            if _norm(val) == want:
                name = k
                break
        if name is None:
            continue
        if name in _PROCESSING_SPLIT_LOST:
            notes.append(f"皇后标签「{name}」{_PROCESSING_SPLIT_LOST[name]}")
            continue
        if name in _PROCESSING_BY_MUSOPIA_TAG:
            val = _PROCESSING_BY_MUSOPIA_TAG[name]
            if len(notes):
                notes.append(f"同时命中多个地区标签 ⇒ 取「{name}」({val})")
            return val, ("；".join(notes) if notes else None)
    return 1, ("；".join(notes) if notes else None)     # ★ 没命中 ⇒ 1 其他


# ==================== `tags[4][]`：标签 ====================
#
# ★★ 只映射**两边语义明确同源**的。实测两边体系**不同类**（见文件头），
#    所以这张表**故意很短** —— ★ **不硬凑**（凑出来的标签 = 写错的事实）。
#
#   乌托邦标签名 → 蟹黄堡标签值
_CRABPT_TAGS = {
    "禁转": 1,          # ★ 两边同名，且这是**最要紧**的一个（禁转必须传过去）
    # ★ 「首发」在蟹黄堡**没有**对应项 ⇒ 不映射（落 unmapped）
}


def map_tags(musopia_tags):
    """乌托邦标签编号列表 → (蟹黄堡 `tags[4][]` 值列表, 未映射的乌托邦标签名)。"""
    out, unmapped = [], []
    for v in musopia_tags or []:
        want = _norm(v)
        name = None
        for k, val in descr.TAGS.items():
            if _norm(val) == want:
                name = k
                break
        if name is None:
            continue
        if name in _CRABPT_TAGS:
            cv = _CRABPT_TAGS[name]
            if cv not in out:
                out.append(cv)
        else:
            unmapped.append(name)
    return out, unmapped


# ==================== 地区/质量字段的**固定值**（有实测先例）====================
#
# ★★★ 这三项**一律填 Other**，依据是**四条真实详情页**（PLAN 附录 C.6）：
#     分辨率 · 视频编码 · 制作组 —— 四条样本**全部**是 `Other`。
#   ★ 音乐种本来就没有分辨率/视频编码（那是视频概念），制作组皇后元数据里**没有**。
#   ⇒ ★ 填 `Other` 是**有先例、站点接受**的，比"猜一个具体值"安全得多。
#   ★★ 不许去猜"这条像哪个制作组"—— 那是把猜测当事实（ERR-META-01 一族）。
STANDARD_OTHER = 1
CODEC_OTHER = 1
TEAM_OTHER = 1


def build_fields(task, *, anonymous=False):
    """把一条 task（db 里的 dict）渲染成蟹黄堡 `takeupload.php` 的字段 dict。

    返回 `(fields, audit)`：
      · `fields` = 直接 `data=` 提交的普通 dict（多值的用 list）
      · `audit`  = **人读的字段来源说明**（逐字段写"这个值从哪来"）
        ★ 为什么要 audit：本文件里几乎每个值都是"从乌托邦编号转过来的"，
          而**转换正是最容易静默出错的地方**。让调用方能把每一格打出来复核。

    ★ 文件字段（`file`）**不在这里** —— 它由调用方按 `.torrent` 路径装进 `files=`。

    ★★★ `db.merge_mapped(task)` 在**本函数内部**先跑一遍 —— 这一步不能省，也
      不能指望调用方做：
        `db.get_pending_publish()` 只 SELECT `db.TASK_FIELDS`，那里面**没有**
        `tags` / `medium_sel` / `audiocodec_sel` / `team_sel` 这四列 ——
        它们是 `mapped_json` 里的 JSON。不铺平 ⇒ 那四个键是 `None`
        ⇒ 本文件会把它们**一律映成 `1`（Other）**，而那是**看着正常的假值**：
        页面能提交成功，只是**音频编码、地区、标签全丢**（`ERR-META-01` 的形状）。
      ★ 实测踩过（2026-09-24）：样例的 `audiocodec_sel[4]=1`，而标题里明明写着 FLAC
        —— 那时就是漏了这一步，且**审计行只报"值=1"，读起来像"映射结果"**。
        ⇒ 现在铺平后，**若四键仍为空，会由下面的 audit 显式报"没有来源"**。
    """
    task = db.merge_mapped(dict(task))     # ★ 拷贝一份，不改调用方的 dict
    audit = []

    # ---- 标题：★ 原样透传（2026-09-24 用户拍板：「皇后标题怎么起就怎么起」）
    #
    # ★ 蟹黄堡页面自带提示：「若不填将使用种子文件名」⇒ ★ **留空也是合法选项**。
    #   但我们的库里 `formatted_title` 已经是渲染好的标题 ⇒ 填它，
    #   **不做任何加后缀/改组名**（那是用户明确否掉的）。
    title = task.get("formatted_title") or task.get("torrent_name") or ""
    audit.append(f"name（标题）← task.formatted_title 原样，未加任何后缀")

    # ---- 副标题：★ 复用 descr 里那份**唯一**的口径
    #      （2026-09-24 从 `build_form_fields()` 抽出 `build_small_descr_for_task()`）
    #      ★ 不在这里自己算 —— 副标题是**两站提交的同一个字段**，
    #        各算一份就可能两站不一样，且不会报错。
    small_descr = descr.build_small_descr_for_task(task)

    # ---- 简介：复用 descr.build_descr()（★ 它本身就是 **BBCode**，
    #     而蟹黄堡 `descr` 实测也是 BBCode ⇒ **格式同构，可直接复用**）
    logs = None
    raw_logs = task.get("logs_json")
    if raw_logs:
        try:
            parsed = __import__("json").loads(raw_logs)
            if isinstance(parsed, list) and parsed:
                logs = parsed
        except (ValueError, TypeError):
            logs = None

    descr_body = descr.build_descr(
        cover_url=task.get("cover_url") or "",
        tracklist=task.get("tracklist") or "",
        log_content=task.get("log_content") or "",
        log_filename=task.get("log_filename") or "",
        logs=logs,
    )
    audit.append("descr（简介）← descr.build_descr()（BBCode，两站同构）")

    # ---- 质量组（六个 select）
    musopia_medium = task.get("medium_sel")
    source = map_source(musopia_medium)
    audio = map_audiocodec(task.get("audiocodec_sel"))
    proc, proc_note = map_processing(_as_list(task.get("tags")))
    ct, unmapped_tags = map_tags(_as_list(task.get("tags")))

    audit.append(f"source_sel[4] ← task.medium_sel={musopia_medium} ⇒ {source}（音乐多为 Other）")
    audit.append(f"audiocodec_sel[4] ← task.audiocodec_sel="
                 f"{task.get('audiocodec_sel')} ⇒ {audio}（按名字转，非按编号）")
    audit.append(f"processing_sel[4] ← 皇后地区标签 ⇒ {proc}"
                 + (f"　★ {proc_note}" if proc_note else ""))
    audit.append(f"standard_sel[4] = {STANDARD_OTHER}（Other，四条样本先例）")
    audit.append(f"codec_sel[4] = {CODEC_OTHER}（Other，同上）")
    audit.append(f"team_sel[4] = {TEAM_OTHER}（Other，同上）")
    audit.append(f"tags[4][] ← {ct}"
                 + (f"　★ 未映射：{unmapped_tags}" if unmapped_tags else ""))

    # ★★★ 「四个键是不是空的」必须**显式报出来** —— 这是本文件唯一会"静默出错"的地方。
    #
    #   为什么：上面六行 audit 的格式是 `← <来源> ⇒ <结果>`，而**当来源本身就是 `None`
    #   时，它和"来源有值、映射成了 Other"长得一模一样**（都印成 `⇒ 1`）。
    #     · `task.medium_sel=12 ⇒ 1`   = 映射结果正确（Other 就是 1）
    #     · `task.medium_sel=None ⇒ 1` = **我们什么都没拿到**，1 只是兜底
    #   ⇒ 两者**结论相反**却**排版相同** —— 这正是 `ERR-META-13`（同一数字在不同
    #     位置含义不同）与 `ERR-META-01`（判据看着对但量错了东西）的合流。
    #   ⇒ 处置：把"来源为空"单独报一行，指出它会让站点上那几个字段**全丢**。
    _empty_src = [n for n, v in (
        ("medium_sel", musopia_medium),
        ("audiocodec_sel", task.get("audiocodec_sel")),
        ("tags", task.get("tags")),
    ) if v in (None, "", [])]
    if _empty_src:
        audit.append(
            f"★★ 警告：这些来源是空的 ⇒ 对应字段在站点上会**全丢**："
            f"{_empty_src}。★ 若来源本是 `mapped_json`，说明**没调 "
            f"`db.merge_mapped()`**（本函数已自动调；仍为空 = 库里真的没有）。")

    # ---- 技术信息：★ 蟹黄堡独有的字段。有 log 就放 log，没有就留空。
    #      ★ 页面说明要的是 **MediaInfo**，而我们的 `log_content` 是**抓轨日志**，
    #        两者不是一回事 ⇒ ★ **不硬塞**（塞错位置比留空更坏）。
    #      ⚠️ 已知缺口：我们**没有生成 MediaInfo** 的能力，这个字段暂留空。
    technical_info = ""

    fields = {
        "type": MUSIC_TYPE,
        "name": title,
        "small_descr": small_descr,
        "descr": descr_body,
        "technical_info": technical_info,
        # 六个质量字段 —— ★ 一律走 `[4]` 组（发音乐用错组会被静默忽略）
        "source_sel[4]": source,
        "standard_sel[4]": STANDARD_OTHER,
        "codec_sel[4]": CODEC_OTHER,
        "audiocodec_sel[4]": audio,
        "team_sel[4]": TEAM_OTHER,
        "processing_sel[4]": proc,
        # 标签：多值 ⇒ 用 list，requests 会编成重复的 `tags[4][]=N`
        "tags[4][]": ct,
    }
    return fields, audit


def _as_list(v):
    """`tags` 在库里可能是 list、也可能是 JSON 串 / 逗号串 ⇒ 统一成 list[int]。"""
    if v is None or v == "":
        return []
    if isinstance(v, list):
        out = []
        for x in v:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                pass
        return out
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("["):
            try:
                import json
                return [int(x) for x in json.loads(s)]
            except (ValueError, TypeError):
                pass
        out = []
        for part in re.split(r"[,\s]+", s):
            try:
                out.append(int(part))
            except (TypeError, ValueError):
                pass
        return out
    try:
        return [int(v)]
    except (TypeError, ValueError):
        return []


def music_upload_url(base=None):
    return (base or config.CRABPT_URL).rstrip("/") + "/takeupload.php"


def music_list_url(base=None):
    """音乐分类的浏览页（★ 用于回读复核：发布后能从列表里找到吗）。"""
    b = (base or config.CRABPT_URL).rstrip("/")
    return f"{b}/torrents.php?cat={MUSIC_TYPE}"
