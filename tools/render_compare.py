#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
渲染对照：皇后详情页 → 解析 → 映射 → **实际发出的 form**。

★ 存在的理由：`opencd_map.map_all()` 算得再对，只要 `descr.build_form_fields()`
  不把这些 key 放进 form，站点上就是空的 —— 而**两层各自单独看都"正常"**。
  这正是 ERR-META-01 的形状（输出看起来正常，结论却是错的）。
  ⇒ 判据必须是「**最终 form 里有没有那个 key**」，不能只看中间层。

只读本地已脱敏样本，零请求。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import descr
import opencd_map as M
import opencd_parse as P

SAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "opencdhtml", "sanitized", "sample-zhuanzai-huanying.html",
)


def main():
    info = P.parse_detail(open(SAMPLE, encoding="utf-8").read())
    m = M.map_all(info)

    line = "=" * 78
    print(line)
    print("渲染对照：皇后详情页 → 解析 → 映射 → 实际发出的 form")
    print("样本：" + os.path.relpath(SAMPLE).replace(os.sep, "/"))
    print(line)

    print("\n【① 解析层 opencd_parse 拿到的页面字段】")
    for k in ("name", "album", "artist", "year", "format", "medium", "source", "tags"):
        print("    %-10s = %r" % (k, info.get(k)))

    print("\n【② 映射层 opencd_map.map_all() 产出】")
    for k in ("title", "tags", "audiocodec_sel", "medium_sel", "source",
              "artist", "year"):
        print("    %-16s = %r" % (k, m.get(k)))

    task = {
        "formatted_title": m["title"],
        "torrent_name": m["title"],
        "tracklist": m["tracklist"],
        "log_content": m["log_content"],
        "cover_url": "http://example/cover.jpg",
        # ★ 标签：走 map_all 的产出（页面已写明的）—— 不是让 build_form_fields
        #   从标题里猜。这条**就是被测的行为**，见 descr.build_form_fields 的注释。
        "tags": m["tags"],
        # ★★ 艺术家 / 发行年份：**必须传进 task**，否则 build_form_fields 拿不到
        #    ⇒ `custom_fields[4][1]` / `[4][2]` 会是空串。
        #    ★ 这个 bug 2026-09-21 在这里**当场被 ④ 的判据抓出来**（两条 MISS）——
        #      说明判据是有效的：它不是"打印一遍看着像对"，而是真会失败。
        "artist": m["artist"],
        "year": m["year"],
    }
    f = descr.build_form_fields(task, autodetect=True)

    print("\n【③ 实际发出的 form】★ 这才是站点收到的")
    for k, v in f.items():
        print("    %-24s = %s" % (k, repr(v)[:58]))

    print("\n【④ 缺口对照】判据 = 「最终 form 里有没有这个 key / 值对不对」")
    # ★ tags[4][] 是**多值 key**：取值要按 key 精确匹配，不能 startswith("tags")
    #   —— 否则 `tagsfoo` 之类的乱键也会被算成"有"（判据自己就不可靠了）。
    got_tags = f.get("tags[4][]")

    checks = [
        ("tags[4][]", m["tags"], got_tags),
        ("audiocodec_sel[4]", m["audiocodec_sel"], f.get("audiocodec_sel[4]")),
        ("medium_sel[4]", m["medium_sel"], f.get("medium_sel[4]")),
        ("custom_fields[4][1]", info.get("artist"), f.get("custom_fields[4][1]")),
        ("custom_fields[4][2]", info.get("year"), f.get("custom_fields[4][2]")),
        ("custom_fields[4][3]", info.get("log_content"), f.get("custom_fields[4][3]")),
    ]

    # ★ 多值 key 的类型判据：必须是 list，且**编码后真的展开成多个同名键**。
    #   若哪天有人图省事 join 成 "6,10"，站点会把整个串当一个值 ⇒ 一个标签都不选中。
    #   这里把「编码后的样子」也打出来，免得"form 里有 key"就以为对了。
    print("\n【⑤ 多值 key 的编码校验】tags[4][] 必须是 list 且展开成多个同名键")
    if isinstance(got_tags, list):
        from urllib.parse import urlencode
        enc = urlencode({"tags[4][]": got_tags}, doseq=True)
        n = enc.count("tags%5B4%5D%5B%5D=")
        print("    类型      = list ✅")
        print("    编码结果  = %s" % enc)
        print("    同名键个数 = %d（期望 = %d）%s"
              % (n, len(got_tags), "✅" if n == len(got_tags) else "❌"))
    elif got_tags is None:
        print("    类型      = 缺失 ❌（build_form_fields 没写这个 key）")
    else:
        print("    类型      = %s ❌（必须是 list；字符串会被当成单个值）"
              % type(got_tags).__name__)

    bad = 0
    for key, want, got in checks:
        ok = bool(got)
        if not ok:
            bad += 1
        print("    %s %-22s 期望=%r  实发=%r" % ("OK  " if ok else "MISS", key, want, got))

    print("\n【⑥ 标签表完整性】判据 = 对着**站点真实表单**数 checkbox，不是对着自己的表数")
    # ★★ 这条判据存在的理由（2026-09-21 实测）：
    #    `descr.TAGS` 原先只有 23 项，而站点真实表单有 **44** 项 —— 缺的 21 项
    #    **没有任何地方会报错**，只是"少勾几个标签"。这就是本项目最怕的静默错误的形状。
    #    ⇒ 拿页面上真实的 `name="tags[4][]"` checkbox 逐个数，与 TAGS 求差集。
    REAL_FORM = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "opencdhtml", "音乐乌托邦 __ 发布 - Powered by NexusPHP.html",
    )
    if not os.path.exists(REAL_FORM):
        print("    ⚠️  缺真实表单 HTML，跳过（这条判据需要它，见文件名）")
    else:
        import re
        html = open(REAL_FORM, encoding="utf-8", errors="replace").read()
        real = {}
        for m in re.finditer(r'<input[^>]*name="tags\[4\]\[\]"[^>]*>', html):
            val = re.search(r'value="([^"]*)"', m.group(0))
            end = html.find("</label>", m.end())
            label = re.sub(r"<[^>]+>", "", html[m.end():end]).strip()
            if val:
                real[val.group(1)] = label
        ours = set(descr.TAGS.values())
        missing = sorted(set(real) - ours, key=int)
        extra = sorted(ours - set(real), key=int)
        bad_len = (len(descr.TAGS) != len(real)) or (len(descr.TAGS_LABELS) != len(real))
        for label, good in (
            ("站点题面 checkbox 数 = 44", len(real) == 44),
            ("descr.TAGS 项数 = 站点数（不缺不多）", not missing and not extra),
            ("descr.TAGS 与 TAGS_LABELS 项数一致", len(descr.TAGS) == len(descr.TAGS_LABELS)),
            ("★ 我们表里每一项的编号，站点表单里真的存在", not extra),
        ):
            bad += 0 if good else 1
            print("    %s %s" % ("OK  " if good else "FAIL", label))
        if bad_len:
            bad += 1
        print("    站点 %d 项 / 我们 %d 项；缺的=%s；多的=%s"
              % (len(real), len(descr.TAGS), missing or "无", extra or "无"))
        if missing:
            print("    ❌ 缺的标签（站点有、我们没编号）:")
            for v in missing:
                print("        %-4s %s" % (v, real[v]))

    print("\n    ⇒ 缺口 %d 处" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
