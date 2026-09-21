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
    for k in ("title", "tags", "audiocodec_sel", "medium_sel", "source"):
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

    print("\n    ⇒ 缺口 %d 处" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
