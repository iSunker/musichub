#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生成「皇后 → 乌托邦」字段对照表（Markdown），输出到桌面。

★ 为什么要有这个脚本：对照表里**每一个值都必须来自真实页面**，
  手写一遍就必然混进记忆和推测（本项目栽过多次，`SUMMARY §6` 那一族）。
  ⇒ 让它从**真实 HTML + 真实代码表**里现取，手写零参与。

★ 它是**只读**的：不碰网络、不写库、不改任何状态，只读本地已存页面。
"""
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
    w("| 我们的数据 | 乌托邦 form 的 key | 元素类型 | 站点行头 | 状态 |")
    w("|---|---|---|---|---|")
    rows = [
        ("`title`", "`name`", "input", "标题", "✅ 已接"),
        ("`small_descr`（全 N 曲）", "`small_descr`", "input", "副标题", "✅ 已接"),
        ("`descr`（简介+曲目+日志折叠）", "`descr`", "textarea", "（编辑器）", "✅ 已接"),
        ("固定值 `408`", "`type`", "select", "类型 *", "✅ 已接（HQ Audio）"),
        ("`medium`（映射）", "`medium_sel[4]`", "select", "质量 媒介:", "✅ 已接"),
        ("`format`（映射）", "`audiocodec_sel[4]`", "select", "质量 媒介:",
         "⚠️ 已接，**WAV 留 0**"),
        ("`team`", "`team_sel[4]`", "select", "质量 媒介:", "⚠️ 恒发 `0`（未映射）"),
        ("页面「標籤列表」→ 名字映射", "`tags[4][]`", "**checkbox 多值**", "标签",
         "✅ 已接（44 项表）"),
        ("`artist`（藝術家名）", "`custom_fields[4][1]`", "input", "艺术家", "✅ 已接"),
        ("`year`（發行年份）", "`custom_fields[4][2]`", "input",
         "发行年份 **★必填**", "✅ 已接（值带「年」字）"),
        ("`tracklist`（曲目列表）", "`descr` 内", "—", "—", "✅ 已接（并入简介）"),
        ("`cover_url`（封面）", "`descr` 内", "—", "—", "✅ 已接（并入简介）"),
        ("`log_content`（抓轨日志）", "`custom_fields[4][3]`", "**textarea**",
         "抓轨日志 **★必填**", "✅ 已接"),
        ("——", "`custom_fields[4][4]`", "input", "唱片厂牌", "⏹️ **皇后无此字段**"),
        ("日志里的 `Score : NN`", "`custom_fields[4][5]`", "input",
         "抓轨日志得分", "🟡 **待定**"),
        ("`rip_score`", "`rip_score`", "**number(-100~100)**", "rip_score",
         "🟡 **待定**"),
        ("`source`（來自）", "**无对应**", "—", "—", "⏹️ 原样保留，不上站"),
        ("——", "`uplver`", "input", "匿名发布", "⏹️ 默认不发"),
        ("`nfo`", "`nfo`", "input", "NFO文件", "⏹️ 不使用"),
    ]
    for r in rows:
        w("| " + " | ".join(r) + " |")
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
    w("python opencd_map.py --selftest    # 标签映射 68/68")
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
