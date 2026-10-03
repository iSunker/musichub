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
    ★★ 2026-09-28 订正：**`takeupload.php` 是 POST 端点（提交），不是可读的页面**；
       **空白表单页是 `GET /upload.php`**（站点导航「发布」指向它）。
       ⇒ 读基线要用 `/upload.php`，提交才用 `takeupload.php`。见
         `CrabptClient.fetch_upload_form()` 的 docstring（那里记着两个 bug）。
  · 六个 select 的全部选项                              —— 发布页 `<select>` 逐项读出
  · `tags[4][]` 37 项的「值 → 名字」                    —— BeautifulSoup 读 `<label>`
  · `source_sel` 的用法（音乐多为 Other，WEB 发行才用 WEB-DL）—— 四条真实详情页反推

★★ 一条**已知的、必须如实标注的**缺口（别把它读成"已解决"）：
   `processing_sel[4]`（地区）的取值**是从皇后的地区标签映射**来的，
   而皇后那一列**是"日韩"合并的** ⇒ **拆不出日本(5)与韩国(6)**，
   只能落到 `1 其他`。这与 `opencd_map.py` 把"映射不上显式列出"的做法一致。
"""
import os
import re
import time

import requests

import config
import net   # ★ 统一网络出口（强制 IPv4）——新 Session 一律走 net.new_session()
import db
import descr
import opencd_map

# ★ 复用乌托邦那份 UA / 错误提取 —— 两站都是 NexusPHP，出错的页面形状同族。
#   ★ 不各写一份：这两样不是"站点差异"，是"NexusPHP 共性"，各写一份就是两个口径
#     （`ERR-META-03`），而它们**恰恰是排错时唯一能看的东西**。
#   ★ 方向是单向的（crabpt_site → musopia），不会成环：
#     `musopia.py` 不 import 本文件，也不 import publish。
from musopia import SiteError, UA, _extract_error

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
#     日韩        7                  ★ **拆不出** ⇒ 5（见下）
#     其它地区    8                  1
#
# ★★ 为什么"日韩"落到 `5 日本`（2026-09-27 用户拍板，**原先落 `1 其他`**）：
#    皇后那一列**只有一个合并值 `日韩`**，而蟹黄堡要分 `5 日本` / `6 韩国`
#    ⇒ **信息不足，数学上拆不出来**（这不是 bug，是上游数据粒度不够）。
#    用户的决定：**落 `5 日本`**。
#
#    ★★★ 必须如实标注的后果（别把它读成"已解决"）：
#      **韩国歌手会被标成日本。** 这是一个**已知的、有意的**不精确。
#      理由（用户的判断）：蟹黄堡对元数据的要求**没有乌托邦那么高**，
#      而落 `1 其他` 会让"日韩"这一整类（在皇后占比不小）在蟹黄堡侧
#      全都是"其他地区"，反而更没用。
#
#    ★ 所以这一支**仍然进 `notes`**（不静默）：读日志的人要看得出
#      "这条的地区是**猜**的，可能是韩国被动标成日本"。
#      ⇒ 判据：**"我不确定"必须能被表达出来**（`ERR-SITE-06` 同族）——
#        落 5 是**决定**，但"为什么是 5"必须留在说明里。
_PROCESSING_BY_MUSOPIA_TAG = {
    "大陆": 2,
    "港台": 3,
    "欧美": 4,
    "日韩": 5,          # ★ 用户 2026-09-27 拍板：拆不出 ⇒ 落日本(5)，见上
    "其它地区": 1,
    "其他地区": 1,      # ★ 两站用字不同（乌托邦写"其他"、皇后写"其它"）
}
# ★ 拆不出的那些：**显式列出**，让调用方能把它们报出来（不静默）
_PROCESSING_SPLIT_LOST = {
    "日韩": "皇后只给合并值 ⇒ 蟹黄堡分日本(5)/韩国(6)，**拆不出** ⇒ "
            "按约定落 5 日本；★ 韩国条目会被标成日本（已知不精确）",
}


def map_processing(musopia_tags):
    """乌托邦标签编号列表 → (蟹黄堡 `processing_sel[4]` 值, 说明或 None)。

    ★ 取**第一个**命中的地区标签：皇后一条种子**通常**只有一个地区标签，
      但**不保证**（可能同时有"港台"和"流行"，那是风格不是地区 ⇒ 不受影响；
      真正可能撞的是"大陆"+"港台"这种）。撞了就**取第一个**并**在说明里写明**
      —— ★ 不静默，让"这条种子地区可能填错"变成看得见的事。

    ★★ 2026-09-27 改：「日韩」从"跳过（落 1）"改成"**落 5 但带说明**"。
      改之前这一支在 `_PROCESSING_SPLIT_LOST` 里 **`continue` 掉** ——
      那是对的（当时 `_PROCESSING_BY_MUSOPIA_TAG` 里没有它，跳过 ⇒ 兜底 1）。
      现在它**同时在两张表里**（映射表给了 5、说明表给了理由）⇒
      这里必须**先记说明、再照常往下走映射**，否则 `continue` 会让它
      **继续被跳过**、又落回 1 —— 那就是"改了配置但行为没变"的假修
      （判据⑧b 钉住了这个形状：日韩的返回值必须是 5，不是 1）。
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
        # ★ 先记"这一支有已知的不精确"，**不 continue** —— 若它在映射表里有值，
        #   就照常返回那个值（见上面 docstring 里 2026-09-27 那段）。
        if name in _PROCESSING_SPLIT_LOST:
            notes.append(f"皇后标签「{name}」：{_PROCESSING_SPLIT_LOST[name]}")
        if name in _PROCESSING_BY_MUSOPIA_TAG:
            val = _PROCESSING_BY_MUSOPIA_TAG[name]
            if len(notes) and not notes[-1].startswith(f"皇后标签「{name}」"):
                notes.append(f"同时命中多个地区标签 ⇒ 取「{name}」({val})")
            return val, ("；".join(notes) if notes else None)
    return 1, ("；".join(notes) if notes else None)     # ★ 没命中 ⇒ 1 其他


# ==================== `tags[4][]`：标签 ====================
#
# ★★★ 2026-09-25 用户纠正 —— 本段原先写的是「禁转 两边同名、必须传过去」，
#    **那是错的，而且错在根上**。用户原话：
#        「从皇后往蟹黄堡转发，为什么要勾选蟹黄堡的禁转？」
#
#    ★ 为什么错（这是**方向**问题，不是映射问题）：
#      我们是**转载方**。皇后那条种子的发布者说"欢迎转载"，我们才转出来；
#      转出来之后，在**蟹黄堡**这一侧勾「禁转」的意思是
#      「**禁止别人再转载这条蟹黄堡种子**」—— 那是**我们自己**对下游加的限制，
#      而我们的种子是**转载来的**，根本没资格、也没理由加这个限制。
#      ⇒ ★ 勾上它是**假数据**（写了一个我们无权主张的事实）。
#
#    ★★ 而这个错误**本项目早就解决过**，就在 `descr.TAG_NEVER_FORWARD`
#       （2026-09-21 用户拍板，乌托邦那一侧）：
#          「禁转(1)/首发(2) —— **语义相反**：我们是**转载**过去的」
#       ⇒ 我在写蟹黄堡这个**新调用方**时，**没有把那条既有判据带过来**
#         —— 这正是 `ERR-META-16`（"同一个语义来自哪一列/哪一份判据，
#         换了调用方不能凭印象"）的又一次实例，而且这次的代价是**往站点写假数据**。
#       ⇒ 修法不是"这里再写一份黑名单"，而是**复用 `descr.filter_forward_tags()`**
#         —— 两站共用一份"转发方向白名单"，将来改一处两站都跟。
#
#    ★★ 顺带纠正一个我先前说错的判断：
#      我说过「标了 = 违背用户『怎么起就怎么起』的拍板」。**那句话是错的**
#      —— 用户那个拍板说的是**标题**（`formatted_title` 原样透传），
#      与**标签**无关。而且用户当时的语境**本身就是「不要勾禁转」**。
#      ⇒ 所以这条**根本不是"取舍"**，是**单方向的对错**：不该勾，就是不勾。
#
#    ★ 蟹黄堡**页面里确实有**「禁转」标签（也正因为它存在，才有"勾不勾"的问题）。
#      结论是：**它存在，但我们不用它** —— 这一格我们本来就不该填。
#
# 乌托邦标签名 → 蟹黄堡标签值
#   ★ 蟹黄堡的 `tags[4][]` 以**影视属性**为主（剧情/战争/犯罪/惊悚/科幻/动画…），
#     与音乐几乎无交集 ⇒ 实测**没有一个**乌托邦标签名能在蟹黄堡找到语义同源的项。
#     ⇒ 这张表**故意是空的**，而**空表比硬凑一张表安全**：
#       硬凑出来的每个标签都是写错的事实，而空表只会让页面少几个标签。
#   ★ 唯一还想勾的是 `OpenCD`（来源标记，蟹黄堡有没有这一项**未实测**）——
#     所以它**不在这里猜**（见 `build_fields` 里那条显式 TODO）。
_CRABPT_TAGS = {}


def map_tags(musopia_tags):
    """乌托邦标签编号列表 → (蟹黄堡 `tags[4][]` 值列表, 未映射的乌托邦标签名)。

    ★★ 本函数**先过一遍 `descr.filter_forward_tags()`**（转发方向白名单），
       再查蟹黄堡自己的映射表。两道是**串联**的，各管一件事：
         · `filter_forward_tags` 管**方向**：「皇后有」≠「该勾上」
           （禁转/首发语义相反、友站标记、站内运营标记）
         · `_CRABPT_TAGS`        管**落点**：这个标签蟹黄堡有没有对应项
       ★ 顺序不能反：先方向、后落点。"蟹黄堡有没有禁转这一项"是个无关的问题
         —— 就算有，我们也不勾（见上）。
    """
    # ★★ 方向过滤：★ 复用乌托邦那份既有判据，**不在这里再写一份黑名单**
    #    （`ERR-META-03`：两处各写一份必然漂移，而这次漂移的代价是**写假数据**）。
    #    ★ `dropped` 要**返回给调用方**并打印 —— 不静默丢（同 `map_tags` 的契约）。
    values = [str(x) for x in (musopia_tags or [])]
    kept, dropped = descr.filter_forward_tags(values)

    out, unmapped = [], []
    for v in kept:
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
    # ★★ 被方向过滤剔掉的，**并进 unmapped 一起报**（不静默丢）。
    #   ★ 但**带上理由**：它们没落上**不是**因为"蟹黄堡没有这一项"，
    #     而是因为**我们本来就不该勾**。两种"没落上"混成一个列表会让人误判
    #     （`ERR-META-14`：兜底与映射结果必须分开说）。
    for v, nm, why in dropped:
        unmapped.append(f"{nm}（★ 方向过滤：{why}）")
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
        # ★★ 皇后简介正文（2026-09-26 加）—— **两站必须传同一个值**。
        #    ★ 为什么这里也要传：`build_descr()` 是**两站共用**的（本文件上方
        #      那条注释已写明"格式同构、可直接复用"）。若这边不传，乌托邦页面
        #      有皇后正文、蟹黄堡页面没有 —— 那是**没人要求过的差异**，
        #      而且不会报错（两条链路各自"看着正常"）。
        #      与 `build_small_descr_for_task` 抽出来的理由是**同一个**
        #      （见 `descr.build_small_descr_for_task` 的 docstring）。
        #    ★ `db.merge_mapped(task)` 在本函数**内部**第 349 行已跑过
        #      ⇒ `task["descr"]` 是**快照提升后**的值（权威值）。
        #      兜底取列 `opencd_descr`：万一将来有人把 merge 那步挪走，
        #      这里不会**静默**变成空。
        opencd_descr=task.get("descr") or task.get("opencd_descr") or "",
        # ★★★ 2026-09-27：**蟹黄堡不要日志折叠块**（用户拍板「折叠代码也不需要」）。
        #   ★ 这一行是**承重的**：不传 `site=` 的话 `_skip_logs` 就是 False
        #     ⇒ 蟹黄堡的简介里会长出 `[spoiler=LOG][code]…[/code][/spoiler]`，
        #     而**不会报任何错**（`build_descr` 照常返回一段合法 BBCode）。
        #   ★ 用法是 `db.SITE_CRABPT` 常量而不是字面量 `"crabpt"` ——
        #     站名只有 `db` 那两个常量是唯一真值源（`ERR-META-03`），
        #     本文件也**只**用常量（见文件头"站点名"那条）。
        site=db.SITE_CRABPT,
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
                 + (f"　★ 未映射/未勾：{unmapped_tags}" if unmapped_tags else ""))

    # ★★ 一条**显式的 TODO**（不是"忘了"）：
    #   乌托邦那一侧会给每条种子勾上 `OpenCD`（来源标记，2026-09-21 用户点名
    #   「opencd 勾上」）。蟹黄堡**有没有这一项，未实测** ⇒ ★ 这里**不猜**。
    #   ★ 想要补上它，判据是**一次真实观测**：在蟹黄堡发布页的 `tags[4][]` 选项里
    #     找有没有一个叫 `OpenCD` / 含义为"来自 OpenCD"的项。
    #     —— 而这件事**下一次 dry-run 就能顺手做完**（发布页已经在读了）。
    audit.append("tags[4][] 的 OpenCD 来源标记：★ 未实测蟹黄堡有无此项 ⇒ 本次不勾"
                 "（补它的判据 = 读一次发布页的 tags 选项）")

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


# ============================================================================
# 客户端 —— 只做三件事：验登录 / 取发布页基线 / 提交发布
# ============================================================================
#
# ★★ 为什么**不**继承 `MusopiaClient`、也不去改它加个 `site=` 参数：
#    乌托邦那份是**主发布线**（254 条已在做种、`update_metadata.py` 的编辑路径
#    也走它）。把蟹黄堡塞进同一个 class ⇒ 每次动本文件都可能改到那条线的行为，
#    而那条线的回归代价是"已经在做种的种子被改坏"。
#    ⇒ ★ 共用**数据**（`build_fields` / `_norm` 等），不共用**连接与提交**。
#
# ★★ 「基线」这件事两站**完全不同**，这是本 class 比乌托邦那份简单的原因：
#    · 乌托邦 `update_metadata.py` 要「读表单 → 覆盖指定字段 → 整表提交」，
#      因为 `takeedit.php` 是**整表覆盖**语义（没提交的字段当空）。
#    · 蟹黄堡走的是**发布**（`takeupload.php`），我们是**从零构造**整张表
#      （`build_fields()` 把每一个字段都填了）⇒ **不需要读表单当基线**。
#      ⚠️ 将来若要**编辑**已有的蟹黄堡种子，那条路径**必须**先读一次
#      `takeedit.php?id=` 取基线 —— 否则会重现乌托邦踩过的"每编辑一次就抹掉
#      一批属性"。★ 现在没有这条路径，所以这里**没有** `fetch_edit_form`，
#      而不是"忘了写"。
class CrabptClient:
    """蟹黄堡（crabpt.vip）客户端。★ 只读 + 一次 POST，不碰 qB、不碰库。"""

    def __init__(self, base_url=None, cookie=None, request_interval=1.5):
        self.base = (base_url or config.CRABPT_URL).rstrip("/")
        self.cookie = cookie if cookie is not None else config.CRABPT_COOKIE
        self.interval = request_interval
        self._last_request = 0.0

        self.sess = net.new_session()
        self.sess.headers.update({
            "User-Agent": UA,
            "Cookie": self.cookie,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    # ---------- 底层 ----------

    def _throttle(self):
        """站点是别人的，别把人家打疼。每次请求之间留间隔。"""
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
        """把**蟹黄堡服务端**的那个 `.torrent` 拉回来（回加做种用）。返回 bytes 或 None。

        ★★ 为什么必须从**站点**下、不能拿本地那份：本地 `tasks.torrent_file` 是
          `run.py` 用**乌托邦**的 announce 生成的；而蟹黄堡在收下后**会重写**它
          （换成 `https://crabpt.vip/announce.php?passkey=<蟹黄堡passkey>`，
           连 `name`/`piece length` 都可能变 ⇒ **infohash 也变**）。
          2026-10-02 实测：同一条（皇后 `69c5ee1b…`）乌托邦种子 infohash
          `edda64b7…`、蟹黄堡服务端种子 `03c17802…`，**两者不同**，
          但 80 个文件与总大小**逐字节相同**。
          ⇒ 要"做种给蟹黄堡"，回加的**必须**是这份带蟹黄堡 announce 的种子。
          ★ 拿本地那份去回加 = 做种报到乌托邦 ⇒ 蟹黄堡仍记 hit-and-run（假成功）。
        ★ 判据同 `musopia.MusopiaClient.download_torrent`：HTTP 200 且首字节 `d`
          （bencode dict）—— 站点在没登录/无权限时回的是 HTML（首字节 `<`）。
        """
        r = self.get("/download.php", params={"id": torrent_id})
        if r.status_code == 200 and r.content.startswith(b"d"):
            return r.content
        return None

    def have_cookie(self):
        return bool((self.cookie or "").strip())

    # ---------- 登录态 ----------

    def check_login(self):
        """验 Cookie 有效性，返回 user_id 或 None。

        ★★ 这里**故意不抄**乌托邦那份的判据，因为**两站的登录页形状不同**：
            乌托邦失效 ⇒ 回**登录页**（body 里出现 `takelogin.php`）
            蟹黄堡失效 ⇒ 回 **401 + Basic 认证弹窗**（NexusPHP 的 `.htaccess`
                          保护整站；`WWW-Authenticate: Basic realm="crabpt.vip"`）
            ★ 所以"有没有登录页"这条判据在蟹黄堡**恒为假** ⇒ 照抄过来会把
              "Cookie 全废"报成"登录成功"（`ERR-META-01` 的经典形状）。
          ⇒ 判据用**响应形状**：401 / 带 `WWW-Authenticate` ⇒ 判定失效并抛。

        ★ 另记一条**不能当判据**的东西（实测 2026-09-25）：出错时站点回的那张
          401 页**带一行 `setCookie('nexusphp_test', ...)`**。它**不是**登录 cookie，
          是 NexusPHP 的探测脚本在检查"浏览器收不收 cookie"。
          ⇒ 谁要是拿它判断"站点给我发 cookie 了 ⇒ 我登录了"，就会得到一个
            永远为真的假判据。（已实测：正常页面 `nexusphp_test` 出现 **0** 次。）
        """
        r = self.get("/torrents.php")
        if r.status_code == 401 or "WWW-Authenticate" in (r.headers or {}):
            raise SiteError(
                f"蟹黄堡拒绝访问（HTTP {r.status_code}，带 WWW-Authenticate）"
                f" ⇒ CRABPT_COOKIE 已失效或没配上。请更新 NAS `.env` 的 "
                f"CRABPT_COOKIE（见 ERR-ENV-02：新增键要在 NAS 手工加）")
        if r.status_code != 200:
            raise SiteError(f"访问蟹黄堡 torrents.php 失败，HTTP {r.status_code}")
        body = r.text or ""
        if "takelogin.php" in body or "login.php?returnto" in body:
            raise SiteError("蟹黄堡 Cookie 已失效（页面退回登录页），"
                            "请更新 NAS `.env` 里的 CRABPT_COOKIE")
        # ★ 优先从 userdetails 链接抠 uid；抠不到**不抛** ——
        #   提交发布**不需要** uid（不是靠 uid 发种的），抛在这里会把
        #   "取不到 uid"（页面版式微调）误判成"登录失败"。
        m = re.search(r"userdetails\.php\?id=(\d+)", body)
        return m.group(1) if m else None

    # ---------- 发布页（★ 基线 + 可用性预报）----------

    def fetch_upload_form(self):
        """GET `upload.php`（**空白发布表单页**），返回 `(form, actions, action, csrf)`。

        ★★★ 2026-09-28 修的两个 bug（**两个既有，且互为掩护** —— 详见下面）：
          ① **`GET takeupload.php` 就是"提交"** —— `takeupload.php` 是 **POST 端点**，
             站点导航里的「发布」指向的是 **`/upload.php`**（空白表单页）。
             实测：GET `takeupload.php` → 站点回 **「上传失败！请填写必填项目」**，
             **HTTP 200 + 正常导航**，页面上只剩一个搜索 <form>（`action='search.php'`）。
             ⇒ 于是"四个核心字段全缺"这条报错**看起来像"页面版式变了 / 没权限"**，
               而真相是**我们拿提交端点当读了页面** —— 这一点整条流水线从没验过，
               直到 2026-09-28 第一次真发才暴露（`publish.py --site crabpt` 真发）。
          ② **`bs.find("form")` 取到的是搜索框，不是发布表单。**
             实测真实发布页有**两个** <form>：
                 <form action="search.php" method="get" target="_blank">   ← 第 1 个
                 <form id="compose" enctype="multipart/form-data"
                       action="takeupload.php" method="post" name="upload"> ← 第 2 个 ★
             ⇒ 必须 `bs.find("form", id="compose")`。
             ★★ 这条 bug **即使 ① 修好了也照样存在**（拿到正常发布页，`find("form")`
               仍然只看到搜索框的 2 个字段）⇒ **两处必须一起改**。

        ★★ 为什么发布前**必须**先读一次这张页面（两个理由，第二个才是关键）：
          ① 它是**基线**：`build_fields()` 产出的是 12 个字段，而站点那张表单
             实际有 **101** 个字段（编辑器按钮、别的分类组等）。先读一次，
             才能把"我们没填的"与"站点没有的"分开。
          ② ★★★ **它是唯一能在提交前发现"账号不能发种"的判据。**
             `ERR-SITE-01`（乌托邦实测）：Cookie 有效 ≠ 有发种权限 ——
             权限被关后站点回的是 **HTTP 200 的普通页**，不是 302、不是报错。
             ⇒ 若不做这一步，我们会带着一个"看着成功"的 200 走完整个提交，
               把失败**留给不可逆的那一步**才发现。

        ★★★ 判据本身也改了（这是本次真正的教训）：
          旧判据是"有 form 但缺 4 个字段 ⇒ 版式变了"。实测**它误导了人** ——
          缺字段的真实原因是**选错了 form**（见 ②），而不是站点改了名字。
          新判据 = **找不到 `#compose` 表单** ⇒ 这不是发布页。三种情况**分开报**：
            · 页面退回登录页            ⇒ Cookie 失效
            · 没有 `#compose` 表单      ⇒ 不是发布页（权限被关 / 版式变了 / 路径变了）
            · 有 `#compose` 但缺 4 个字段 ⇒ 站点改名了（**保留这条**，但排最后）
          ★ 纪律（`ERR-SITE-06` 同族）：一个报错若并列多种原因，就必须同时给出
            **分辨它们的判据**；否则读的人只能猜，而猜错的代价已经实测过。

        ★ 返回的 `form.action` 是**站点自己写的** action（相对或绝对），
          提交要用它，不要用我们猜的路径。
        """
        # ★★ 读的是**空白表单页** `/upload.php`，**不是** `takeupload.php`（那是提交端点）。
        #    ★ 别"顺手改回 takeupload.php" —— GET 它 = 提交一个空表单（见上面 ①）。
        #    ★ `/upload.php?type=408` 实测与无参形态**同构**（63,860 vs 63,877 字节，
        #      同样是 2 个 form、`#compose` 101 字段）⇒ 用无参的更少假设。
        r = self.get("/upload.php")
        if r.status_code != 200:
            raise SiteError(f"取蟹黄堡发布页失败，HTTP {r.status_code}")
        body = r.text or ""
        if "takelogin.php" in body:
            raise SiteError("蟹黄堡 Cookie 已失效（发布页退回登录页）")

        bs = __import__("bs4").BeautifulSoup(body, "html.parser")
        # ★★ 按 **id** 选，不按"第几个"选：页面上第 1 个 form 是**搜索框**。
        form = bs.find("form", id="compose")
        if form is None:
            # ★ 把"页面里究竟有什么"报出来 —— 这是 ERR-SITE-01 那个
            #   「HTTP 200 + 看不出原因」的**解药**：至少要能看出这不是发布页。
            #   ★★ 顺带把"页面上一共有几个 form、各自是什么"打出来：
            #     本次就是因为只报"缺字段"，才把"选错 form"读成了"版式变了"。
            looks_logged_in = "userdetails.php" in body
            seen = []
            for f in bs.find_all("form")[:4]:
                seen.append(f"id={f.get('id')!r} action={f.get('action')!r}")
            # ★ 若是提交端点被 GET 了，页面标题会是「上传失败！」—— 单独指出来，
            #   因为这跟"权限/版式"是**完全不同**的修法（见本函数 docstring ①）。
            is_submit_err = ("上传失败" in body) or ("请填写必填项目" in body)
            raise SiteError(
                "蟹黄堡发布页里**没有 `#compose` 表单** ⇒ 这不是发布页。"
                f"（HTTP 200、{len(body)} 字节、"
                f"{'页面上有 userdetails 链接（登录态在）' if looks_logged_in else '页面上没有 userdetails 链接'}、"
                f"页面上的 form: {seen or '（一个都没有）'}）"
                + ("。★★ 页面正文是「上传失败！请填写必填项目」⇒ **这是提交端点的响应**："
                   "路径被改回了 `takeupload.php`（GET 它 = 提交空表单）。"
                   "正确路径是 `/upload.php` —— 见本函数 docstring ①。"
                   if is_submit_err else
                   "。★ 见 ERR-SITE-01：Cookie 有效 ≠ 有发种权限 —— "
                   "这类失败在 NexusPHP 上就是回一张 HTTP 200 的普通页。"
                   "⇒ 请确认该账号的**发种权限**（或页面版式/路径是否已变）。"))

        actions = {i.get("name"): i for i in form.find_all(["input", "select", "textarea"])
                   if i.get("name")}
        # ★ 我们的关键文件字段叫什么 —— 不写死 `file`：NexusPHP 各站可能不同名，
        #   而写死一个错名字的后果是**上传的是空文件**（站点多半照收）。
        #   实测蟹黄堡是 `file`，但判据走"在不在 form 里"。
        # ★★ 这条现在是**最后一道**（`#compose` 已经找到了才走到这里）——
        #   它剩下的唯一含义是"站点把某个字段改名了"，而**不再是**"选错了页面"。
        required = [k for k in ("type", "name", "descr", "small_descr") if k not in actions]
        if required:
            raise SiteError(
                f"蟹黄堡 `#compose` 表单（已找到，{len(actions)} 个字段）缺了这些字段："
                f"{required} ⇒ 站点把这些字段**改名了**，本适配层的字段名过期"
                f"（见 PLAN-crabpt.md 附录 C），**先复核再发布**。")
        csrf = {k: v for k, v in actions.items()
                if k.lower() in ("csrf_token", "csrf", "_token")}
        return {
            "form": form,
            "actions": actions,
            "action": form.get("action") or "/takeupload.php",
            "csrf": csrf,
            # ★ 站点自己声明的 enctype —— 用来印证"确实要 multipart"。
            #   若哪天不是 multipart，提交时**文件会变成一个普通文本字段**。
            "enctype": (form.get("enctype") or "").lower(),
        }

    # ---------- 提交 ----------

    def upload_torrent(self, torrent_path, fields, form_info=None):
        """发布新种子，返回 `(torrent_id 或 None, 人读消息)`。

        ★★ 三点与乌托邦那份**故意不同**（不是漏写）：
          ① **不传 `allow_redirects=False`** —— 见下面"ID 从哪来"那段。
          ② **先验证再提交**：`torrent_path` 必须是**真 .torrent**（`torf` 能读）。
             ★ 传个空文件/坏文件上去，站点**多半照收**（拿到一个不可撤销的空种子）。
          ③ `action` 用**站点给的**（`form_info`），不写死路径。

        ★ "ID 从哪来"—— 这是本函数唯一必须解释的技术选型：
          乌托邦那份读的是**重定向**的 `Location: ...id=N`。这要求
          `allow_redirects=False`，否则 `requests` 会跟到详情页、`Location` 就没了。
          ★ 但"发布成功"与"拿到 id"在**不可逆提交**上不该绑成一件：
            重定向没跟上 ⇒ 我们会报"失败"⇒ 而站点上**种子已经建好了**
            ⇒ 触发重试就是**发重**。
          ⇒ 蟹黄堡这份**让 requests 跟重定向**（默认行为），然后从**最终 URL**
            和 **body 里的 `details.php?id=`** 两处取 id：
              · 拿到 ⇒ 一切正常
              · ★ 拿不到 ⇒ **仍然算提交成功**（POST 没抛异常、页面也没报错），
                只把"没读到 id"单独报出来，让人去站点核对。
            ★ 这**不是**把失败当成功 —— 判据分层：
              HTTP 层（POST 有没有抛 / 有没有 5xx）与 id 层分开报，
              而"到底成没成"的最终判据是**回读**（见 `publish.py --site crabpt`
              的 `readback`），不是这里返回的那个数字。
        """
        import torf

        if not os.path.exists(torrent_path):
            raise SiteError(f"种子文件不存在: {torrent_path}")
        try:
            with open(torrent_path, "rb") as fh:
                raw = fh.read()
            torf.Torrent.read_stream(__import__("io").BytesIO(raw))
        except Exception as e:                              # noqa: BLE001
            raise SiteError(f"种子文件读不出（不是合法 .torrent）: "
                            f"{torrent_path}: {type(e).__name__}: {e}") from e

        # ★ 文件字段名：以发布页上实际存在的为准；页面读不到时退回 `file`
        #   （实测蟹黄堡就是 `file`）。★ 不传一个"我们猜的名字"。
        field_name = "file"
        if form_info:
            cand = [k for k in (form_info.get("actions") or {}) if k in ("file", "torrent")]
            if cand:
                field_name = cand[0]

        action = (form_info or {}).get("action") or "/takeupload.php"
        files = {field_name: (os.path.basename(torrent_path), raw,
                              "application/x-bittorrent")}

        r = self.post(
            action,
            data=fields,
            files=files,
            headers={"Referer": self._url("/upload.php"), "Origin": self.base},
            # ★ 显式写出来（= requests 的默认值）：**要跟重定向**，
            #   这样最终 URL 就是详情页。别照抄乌托邦那边的 False。
            allow_redirects=True,
        )

        # ---- id：两处取，最终 URL 优先（它就是详情页）----
        tid = None
        for src in (r.url or "", r.text or ""):
            m = re.search(r"details\.php\?id=(\d+)", src)
            if m:
                tid = m.group(1)
                break

        if tid:
            return tid, "ok"

        # ---- 没拿到 id：先判断这是"坏"还是"可能成了但没读到" ----
        if r.status_code >= 400:
            err = _extract_error(r.text or "")
            return None, (err or f"HTTP {r.status_code}（★ 5xx 也可能发生在"
                                 f"提交**之后**，请务必去站点核对是否已建）")

        err = _extract_error(r.text or "")
        if err:
            return None, err

        # ★ 到这里：HTTP 200、页面上没有报错、也没有 details 链接。
        #   这正是 `ERR-SITE-01` 那个"看着正常"的形状 ⇒ **不许说成功**，
        #   但也**不许说失败**（说了失败就会有人重试 ⇒ 发重）。
        return None, (f"★ 无法判定：HTTP {r.status_code}，既没拿到 details 链接 "
                      f"也没读出报错（最终 URL {r.url!r}，{len(r.text or '')} 字节）。"
                      f"⇒ 请**立刻去站点确认这条种子建了没有**，"
                      f"**不要重跑**（发布不可逆）。")


# ==================== 自检（纯离线：零请求、零写库） ====================
#
# ★★ 为什么值得给这个文件加自检（它原先一个都没有）：
#    2026-09-27 改「日韩 → 5」时踩到一个**静默失效**风险 ——
#    `map_processing` 里原来对 `_PROCESSING_SPLIT_LOST` 是 **`continue`**，
#    而当时「日韩」**只**在那张表里（映射表里没有）⇒ 跳过 ⇒ 兜底 1。
#    改法必须**同时**把「日韩」加进映射表、并**去掉那个 continue**。
#    只做一半（只加表、留着 continue）⇒ 结果**还是 1**，
#    而代码"看起来"改了、不报错、跑得通 —— 这正是 `ERR-META-01` 的形状。
#    ⇒ 判据必须钉住**返回值**（5），不是"表里有没有这个键"。
#
# ★ 判据分四层（每层都能单独失败）：
#     ① 日韩 → **5**（不是 1）—— 本次改动的核心
#     ② 日韩的**说明**必须带出来（落了 5 也要如实标注"可能是韩国"）
#     ③ 其余四个地区**没被顺手改坏**（阴性对照：改动只影响日韩）
#     ④ `descr.build_descr` 的调用**确实传了** `site=D.SITE_CRABPT`
#        —— 只测 `build_descr` 本身（descr 自检⑨）测不到"本文件没接线"
def selftest(verbose=True):
    """返回 `(ok, fails)`。★ 纯离线：只调纯函数 + 读自己的源码。"""
    import ast as _ast
    import os as _os
    ok = fails = 0

    def chk(label, cond, detail=""):
        nonlocal ok, fails
        if cond:
            ok += 1
            if verbose:
                print(f"  ✅ {label}" + (f"   {detail}" if detail else ""))
        else:
            fails += 1
            print(f"  ❌ {label}" + (f"   ← {detail}" if detail else ""))

    print("=" * 72)
    print("crabpt_site 离线自检（零请求、零写库）")
    print("=" * 72)

    # ── ① 本次改动的核心：日韩 → 5 ──────────────────────────────────
    print("\n① 地区映射：皇后「日韩」⇒ 蟹黄堡 **5 日本**（2026-09-27 用户拍板）")
    val, note = map_processing([descr.TAGS["日韩"]])
    chk("日韩 → 5（★ 不是 1 —— 落 1 是**改之前**的行为）",
        val == 5, f"实得 {val}")
    # ★ 阴性对照：这条断言**能**失败 —— 把 `_PROCESSING_BY_MUSOPIA_TAG` 的
    #   "日韩" 删掉（或恢复那个 continue）就能让它挂（已实测，见提交信息）。
    chk("日韩 ⇒ **不等于** 1（把「回落 1」这个旧行为明写出来对照）",
        val != 1, f"实得 {val}")

    # ── ② 落了 5 也要如实标注"可能是韩国" ───────────────────────────
    print("\n② 说明文案：落了 5，但**必须**让人看出这是猜的")
    chk("日韩有说明（不静默）", bool(note), f"实得 {note!r}")
    chk("说明里点出「日本」与「韩国」两个选项都存在（信息不足）",
        note and "日本" in note and "韩国" in note, f"实得 {note!r}")
    chk("★ 说明里**明确写出**了已知后果（韩国会被标成日本）",
        note and "韩国条目会被标成日本" in note, f"实得 {note!r}")

    # ── ③ 阴性对照：其余地区**没被顺手改坏** ───────────────────────
    print("\n③ 阴性对照：改动只影响日韩，其余四个地区原样")
    for name, want in (("大陆", 2), ("港台", 3), ("欧美", 4), ("其他地区", 1)):
        v, _ = map_processing([descr.TAGS[name]])
        chk(f"{name} → {want}", v == want, f"实得 {v}")
    chk("没有地区标签 ⇒ 兜底 1（不编一个）",
        map_processing([])[0] == 1)

    # ── ④ build_descr 的接线：本文件**确实**传了 site=crabpt ─────────
    #
    # ★★ 为什么必须在这一侧测：`descr.py` 自检⑨ 钉的是 **build_descr 本身**
    #    的三种行为；而"**本文件调用时有没有传 site**"是**另一个**判据 ——
    #    不传的话 `site` 默认 None ⇒ 日志折叠块照样长出来，而 descr 那边
    #    全绿（它测的场景自带 site 参数）。这正是 `ERR-META-01`：
    #    两条判据各自都"看着在跑"，合起来覆盖不到那条缝。
    print("\n④ 接线：build_fields 调 build_descr 时**必须**传 site=蟹黄堡")
    _src = open(_os.path.abspath(__file__), encoding="utf-8").read()
    _tree = _ast.parse(_src)
    _calls = [n for n in _ast.walk(_tree)
              if isinstance(n, _ast.Call)
              and isinstance(n.func, _ast.Attribute)
              and n.func.attr == "build_descr"]
    if not _calls:
        chk("找到 build_descr 调用点", False, "★ 没找到 ⇒ 本条判据失效（调用被改名了？）")
    else:
        _kws = {k.arg for c in _calls for k in c.keywords}
        chk(f"build_descr 调用传了 `site=`（共 {len(_calls)} 个调用点）",
            "site" in _kws, f"实得关键字参数 {sorted(_kws)}")
        # ★ 值必须是 `db.SITE_CRABPT` 而不是字面量 —— 站名只有 db 那两个常量
        #   是唯一真值源（ERR-META-03）。
        _vals = [getattr(k.value, "attr", None) or getattr(k.value, "value", None)
                 for c in _calls for k in c.keywords if k.arg == "site"]
        chk("site 的值是 `db.SITE_CRABPT`（不是手抄字面量）",
            all(v == "SITE_CRABPT" for v in _vals) if _vals else False,
            f"实得 {_vals}")

    print()
    print("=" * 72)
    print(f"结果: {ok}/{ok + fails} 通过")
    print("=" * 72)
    return (fails == 0), fails


if __name__ == "__main__":
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8")
    _ok, _n = selftest()
    print("★ 退出码：0 = 全过；1 = 有断言不符" if _ok else "★ 退出码 1（有断言不符）")
    _sys.exit(0 if _ok else 1)
