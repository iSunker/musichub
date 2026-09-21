#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把「皇后 → 乌托邦」的全部匹配摊开来看（纯离线，零请求）。

三层：
  ① 标签：皇后名 → 乌托邦编号   （opencd_map.map_tags）
  ② 其它字段：格式/媒介 → 编号  （opencd_map.map_audiocodec / map_medium）
  ③ 字段落位：我们的 key → 站点 form 的 key（descr.build_form_fields）

★ 为什么要摊开：这三层的**编号体系完全不同**（大陆在皇后是 1、在乌托邦是 4），
  直接搬值是最容易出的静默错误。摊出来才看得出哪一格是猜的、哪一格是实测的。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import descr
import opencd_map as M
import opencd_parse as P

LINE = "─" * 78


def main():
    print("=" * 78)
    print("① 标签：皇后名 → 乌托邦编号")
    print("=" * 78)
    print()
    print("★ 映射**分两步**，中间那张表用的是**名字**，不是编号：")
    print("   皇后标签名 ──TAG_NAME_MAP──> 乌托邦标签名 ──descr.TAGS──> 乌托邦编号")
    print("   ★ 第二步写名字而不是编号，是为了让 descr.TAGS 成为**唯一编号来源**。")
    print()
    print("  皇后值  皇后标签名            → 乌托邦标签名   → 乌托邦编号  状态")
    print("  " + "-" * 68)
    for name, ocval in P.OPENCD_TAGS.items():
        bare = M._strip_suffix(name)
        mname = M.TAG_NAME_MAP.get(name) or M.TAG_NAME_MAP.get(bare)
        mval = descr.TAGS.get(mname) if mname else None
        if mval:
            st = "✅"
        elif mname:
            st = "⚠️ 表里有名、descr.TAGS 里没编号（两处漂移）"
        else:
            st = "❌ 未映射（★ 若有此行即表示表不全）"
        print("  %-7s %-20s → %-14s → %-11s %s"
              % (ocval, name, mname or "—", mval or "—", st))
    print()
    print("  ★★ 2026-09-21 订正：**全部 27 项都已映射**，上面不该再出现 ❌ 行。")
    print("     旧版这里有一批 ⏹️「故意不映射（乌托邦没有对应标签）」—— 那句注释是**错的**：")
    print("     乌托邦真实表单的 `tags[4][]` 有 44 项，Soul/Blues/NewAge/Reggae… 一个都不少。")
    print("     唯一的真差异是**用字**：皇后写 `贝斯(Drum Bass)`、乌托邦写 `贝斯(Drum&Bass)`，")
    print("     剥掉括号后同名 ⇒ 能接上。")
    print()
    print("  ★ 乌托邦 `tags[4][]` 全表 %d 项（`descr.TAGS`），我们的表已与站点对齐。"
          % len(descr.TAGS))
    print("     判据在 `tools/render_compare.py` ⑥：**对着站点真实表单数 checkbox**，")
    print("     不是对着自己的表数（对着自己数 = 自证，少了也看不出来）。")
    print("  ★ 对照：同一个标签，两边编号**完全不同**")
    print()
    print("  标签名        皇后    乌托邦")
    print("  " + "-" * 30)
    for n in ("大陆", "欧美", "港台", "日韩", "古典", "流行", "摇滚"):
        oc = P.OPENCD_TAGS.get(n, "—")
        mu = descr.TAGS.get(n, "—")
        print("  %-14s %-7s %s" % (n, oc, mu))
    print()
    print("  ⇒ 两套编号**完全错位**（大陆：皇后 1 / 乌托邦 4）。")
    print("    把皇后的数字直接填给乌托邦 = 静默错标，这就是要按**名字**映射的理由。")
    print()
    print("  ★ 端到端验一遍（用真实样本的标签）:")
    for sample in ("sample-zhuanzai-huanying.html", "sample-zhuanzai-jinyong.html"):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "opencdhtml", "sanitized", sample)
        info = P.parse_detail(open(path, encoding="utf-8").read())
        vals, un = M.map_tags(info["tags"])
        print("    %-32s %s → %s%s"
              % (sample, info["tags"], vals, f"  未映射={un}" if un else ""))
    print("    ★ 未映射的**显式列出**（不静默丢）—— 这是 map_tags 的返回值契约。")

    print()
    print("=" * 78)
    print("② 编码 / 媒介：字符串 → 编号")
    print("=" * 78)
    print()
    print("  格式（皇后页面的值）      音频编码编号    媒介编号")
    print("  " + "-" * 56)
    for f in ("FLAC 分轨", "FLAC 整轨", "APE 整轨", "WAV 整轨", "WAV", "MP3", ""):
        a = M.map_audiocodec(f)
        med = M.map_medium("CD", f)
        note = ""
        if "WAV" in f.upper() and a == "0":
            note = "   ← ★ 已知缺口：认得出，但编号未实测"
        print("  %-24s %-15s %s%s" % (f or "(空)", a, med, note))
    print()
    print("  乌托邦 AUDIOCODEC 表（descr.py，实测自 edit.php）:")
    print("   ", descr.AUDIOCODEC)
    print("  乌托邦 MEDIUM 表:")
    print("   ", descr.MEDIUM)
    print()
    print("  ⇒ WAV **不在** AUDIOCODEC 表里 ⇒ 刻意返回 '0'（不指定），不猜。")

    print()
    print("=" * 78)
    print("③ 字段落位：解析出的字段 → 站点 form 的 key")
    print("=" * 78)
    print()
    rows = [
        ("專輯名稱 / 解析出的 name", "name", "✅ 已接"),
        ("（渲染：曲目数）", "small_descr", "✅ 已接"),
        ("简介 + 曲目 + 日志折叠", "descr", "✅ 已接"),
        ("類別（固定 HQ Audio）", "type=408", "✅ 已接"),
        ("媒介", "medium_sel[4]", "✅ 已接"),
        ("格式（编码）", "audiocodec_sel[4]", "✅ 已接（WAV 留 0）"),
        ("標籤列表", "tags[4][]", "✅ 已接（2026-09-21 修）"),
        ("製作組", "team_sel[4]", "⚠️ 恒发 '0'（未映射）"),
        ("藝術家名", "custom_fields[4][1]", "✅ 已接（2026-09-21）"),
        ("發行年份", "custom_fields[4][2]", "✅ 已接（★ 带「年」字，原样发）"),
        ("抓轨日志", "custom_fields[4][3]", "✅ 已接（textarea，★ 必填）"),
        ("抓轨日志得分", "custom_fields[4][5]", "⚠️ 恒发 ''（与 rip_score 关系待确认）"),
        ("來自（原抓/轉載）", "（无对应）", "⏹️ 决定 2 原样保留，不上站"),
    ]
    print("  解析出的字段                      站点 form 的 key        状态")
    print("  " + "-" * 70)
    for src, key, st in rows:
        print("  %-34s %-22s %s" % (src, key, st))
    print()
    print("  ⇒ ★ 2026-09-21 ②③ 已落地：序号是 `[4][1]` 艺术家 / `[4][2]` 发行年份 /")
    print("     `[4][3]` 抓轨日志(textarea，必填) / `[4][4]` 唱片厂牌 / `[4][5]` 抓轨日志得分。")
    print("     ★★ **序号不是 1/2/3/4 的顺序** —— 这是原先认错字段的根因。")
    print("     判据：`tools/render_compare.py` ④ 逐 key 比对，MISS 数必须为 0。")
    print()
    print("  ⇒ 「來自」那行标 ⏹️ 是**设计上不发**（乌托邦表单里搜 來源/原抓/轉載 = 0 处），")
    print("     不是缺口。见 SUMMARY §12 决定 2。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
