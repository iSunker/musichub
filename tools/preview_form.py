#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生成**本地预览网页**：把 `descr.build_form_fields()` 真正要发出去的 form，
渲染成一个能在浏览器里打开的 HTML（**模仿乌托邦发布页的样子**）。

★ 为什么要这个（而不是只看 `render_compare` 的文本）：
  `render_compare` 打印的是**键值对**，但"页面长什么样"是另一回事 ——
  简介(BBCode)、曲目、日志折叠块、标签勾选状态，**只有渲染出来才看得见**。
  本项目栽过的坑里就有"字段发了但页面看着不对"，文本对照抓不到。

★ 它是**只读**的：不碰网络、不写库、不发任何请求。产物写到 `_preview/`。

用法：
    python tools/preview_form.py                    # 用默认样本
    python tools/preview_form.py <详情页.html>       # 用指定页面
"""
import html
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import descr
import opencd_map as M
import opencd_parse as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "_preview")
DEFAULT_SAMPLE = os.path.join(
    ROOT, "opencdhtml", "sanitized", "sample-zhuanzai-huanying.html")


def bbcode_to_html(text):
    """把 BBCode **粗浅**转成 HTML —— 只为预览，不求完整。

    ★ 刻意保持"浅"：预览的作用是让人**看出结构**（封面/曲目/折叠日志），
      不是做一个完整 BBCode 引擎。转不了的原文照显，不做花哨兜底。
    """
    if not text:
        return ""
    s = html.escape(text)
    s = re.sub(r"\[/?(center|quote|b|i|u|code|spoiler)[^\]]*\]", "", s)
    s = re.sub(r"\[img\]\s*(\S+?)\s*\[/img\]",
               r'<img src="\1" style="max-width:320px">', s)
    return "<br>".join(s.splitlines())


def sel(name, options, chosen, extra=""):
    """
    渲染成**真实的 `<select>`**，选中态照站点原样 —— 不是打印数字。

    ★★ 2026-09-21 用户指出：「媒介、编码、制作组什么的怎么都是数字啊？
      乌托邦页面哪有媒介编码制作组啊？」—— 没错。
      **数字是提交值（value），人在页面上看到的是文字**（`<option value="8">CD`）。
      原先预览直接显示 `8`，那是**预览做得不对**：它把"提交包"当成了"页面"。
      ⇒ 现在渲染成下拉框，选中项显示**文字**，并保留 value 供核对。
    """
    opts = []
    for val, label in options:
        mark = " selected" if str(val) == str(chosen) else ""
        opts.append('<option value="%s"%s>%s</option>'
                    % (html.escape(str(val)), mark, html.escape(str(label))))
    return ('<select%s style="font-size:12px">%s</select>' % (extra, "".join(opts)))


def build_html(info, m, fields):
    title = fields.get("name") or ""
    tags_sent = [str(x) for x in (fields.get("tags[4][]") or [])]

    # ★ 下拉项按**乌托邦真实顺序**列出（逐行读自 takeupload.php）
    MEDIUM_OPTS = [("0", "请选择一项")] + [(v, k) for k, v in descr.MEDIUM.items()]
    CODEC_OPTS = [("0", "请选择一项")] + [(v, k) for k, v in descr.AUDIOCODEC.items()]
    TEAM_OPTS = ([("0", "请选择一项"), ("6", "Musopia"), ("5", "Other"),
                  ("1", "HDS"), ("2", "CHD"), ("3", "MySiLU"), ("4", "WiKi")])

    def row(label, value, req=False, kind="input"):
        star = ' <span style="color:red">*</span>' if req else ""
        if kind == "textarea":
            val = html.escape(str(value or ""))
            ctrl = ('<textarea rows="4" style="width:99%%">%s</textarea>' % val)
        else:
            val = html.escape(str(value or ""))
            empty = ' style="color:#bbb"' if not val else ''
            ctrl = ('<input type="text" value="%s" style="width:99%%"%s>'
                    % (val, empty))
        return ('<tr><td class="rowhead">%s%s</td><td class="rowfollow">%s</td></tr>'
                % (html.escape(label), star, ctrl))

    # 标签：把 44 项全列出来，**勾上的**高亮 —— 这样"勾了什么"一眼可见
    tag_cells = []
    for short, val in sorted(descr.TAGS.items(), key=lambda x: int(x[1])):
        checked = val in tags_sent
        label = descr.TAGS_LABELS.get(short, short)
        tag_cells.append(
            '<label class="%s"><input type="checkbox"%s disabled>%s'
            '<span class="v">(%s)</span></label>'
            % ("on" if checked else "off", " checked" if checked else "",
               html.escape(label), val))

    dropped = [t for t in (m.get("unmapped") or [])]

    no_log = not (fields.get("custom_fields[4][3]") or "").strip()

    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>预览：%s</title>
<style>
 body{font-family:"Microsoft YaHei",sans-serif;margin:0;background:#f2f2f2;color:#222}
 .wrap{max-width:1000px;margin:0 auto;background:#fff;padding:18px 26px 40px}
 h1{font-size:19px;border-bottom:3px solid #2a7;padding-bottom:8px}
 h2{font-size:15px;margin-top:26px;color:#2a7;border-left:4px solid #2a7;padding-left:8px}
 table.form{border-collapse:collapse;width:100%%}
 td{border:1px solid #ddd;padding:6px 9px;vertical-align:top}
 td.rowhead{background:#f6f8f6;width:150px;text-align:right;font-size:13px;color:#555}
 td.rowfollow{font-size:13px}
 .off{display:inline-block;margin:2px 6px 2px 0;color:#aaa;font-size:12px}
 .on{display:inline-block;margin:2px 6px 2px 0;color:#0a0;font-weight:bold;font-size:12px;
     background:#eaffe9;padding:1px 5px;border-radius:3px}
 .v{color:#bbb;font-weight:normal;font-size:10px}
 .desc{border:1px solid #ddd;padding:10px;background:#fafafa;font-size:13px;
       max-height:420px;overflow:auto}
 .note{background:#fffbe6;border-left:3px solid #e8b;padding:8px 12px;
       font-size:12px;color:#664;margin:10px 0}
 .blocked{background:#ffecec;border-left:3px solid #c00;padding:8px 12px;
          font-size:13px;color:#900;margin:10px 0}
 .bad{color:#c00}
 .tagbox{border:1px solid #ddd;padding:10px;background:#fafafa}
 .q b{color:#555;font-weight:normal}
</style></head><body><div class="wrap">

<h1>乌托邦发布表单预览 —— %s</h1>

<div class="note">
  <b>这是本地渲染，不是真发。</b>内容来自
  <code>descr.build_form_fields()</code> 的**实际产出**。
  <b>布局照乌托邦 <code>takeupload.php</code> 原样</b>：下拉框显示**文字**，
  括号里的小字是对应的提交值。<br>
  <b>来源样本</b>：%s
</div>

%s

<h2>基本字段</h2>
<table class="form">
  <tr><td class="rowhead">种子文件<span style="color:red">*</span></td>
      <td class="rowfollow" style="color:#bbb">（本地预览无文件）</td></tr>
  <tr><td class="rowhead">标题</td><td class="rowfollow">%s</td></tr>
  <tr><td class="rowhead">副标题</td><td class="rowfollow">%s</td></tr>
  <tr><td class="rowhead">类型<span style="color:red">*</span></td>
      <td class="rowfollow">发布至<b>种子</b>区
        <select style="font-size:12px">
          <option>请选择</option>
          <option selected>HQ Audio</option>
        </select>
        &nbsp;<span class="v">(408)</span></td></tr>
</table>

<h2>质量（★ 乌托邦原样：**一行三格**）</h2>
<table class="form"><tr>
  <td class="rowhead">质量</td>
  <td class="rowfollow q">
    <b>媒介: </b>%s&nbsp;&nbsp;
    <b>音频编码: </b>%s&nbsp;&nbsp;
    <b>制作组: </b>%s
  </td></tr></table>

<h2>自定义字段 custom_fields[4][N] ★ 序号不是 1/2/3/4 的顺序</h2>
<table class="form">
%s
</table>

<h2>标签 tags[4][] ★ 绿=勾上，灰=不勾（共 44 项）</h2>
<div class="tagbox">%s</div>
<div class="note">
  实际发出的 <code>tags[4][]</code> = <b>%s</b>（共 %d 个）<br>
  %s
</div>

<h2>简介 descr（BBCode 渲染）</h2>
<div class="desc">%s</div>

<h2>曲目列表（原始值）</h2>
<table class="form">
  <tr><td class="rowhead">曲目列表</td>
      <td class="rowfollow"><pre style="margin:0;font-size:12px">%s</pre></td></tr>
</table>

</div></body></html>
""" % (
        html.escape(title),
        html.escape(title),
        os.path.relpath(info.get("_source_file") or "", ROOT).replace(os.sep, "/"),
        (('<div class="blocked"><b>❌ 这条会被拦下，不发布。</b><br>'
          '原因：<b>无 log</b> —— 皇后页面本身就没有 NFO/LOG'
          '（<code>&lt;div id="divnfo"&gt;&lt;pre&gt;&lt;/pre&gt;&lt;/div&gt;</code>）。<br>'
          '乌托邦的「抓轨日志」是<b>必填</b>，发空的出去站点大概率退回，'
          '症状是「HTTP 200 + 空 body」，<b>失败还看不出原因</b>。<br>'
          '规矩：<b>没有 log 就不发布</b>。</div>') if no_log else ""),
        html.escape(title),
        html.escape(str(fields.get("small_descr") or "")),
        sel("medium_sel[4]", MEDIUM_OPTS, fields.get("medium_sel[4]") or "0"),
        sel("audiocodec_sel[4]", CODEC_OPTS, fields.get("audiocodec_sel[4]") or "0"),
        sel("team_sel[4]", TEAM_OPTS, fields.get("team_sel[4]") or "0"),
        "".join([
            row("艺术家", fields.get("custom_fields[4][1]")),
            row("发行年份", fields.get("custom_fields[4][2]"), req=True),
            row("抓轨日志", fields.get("custom_fields[4][3]"), req=True, kind="textarea"),
            row("唱片厂牌", fields.get("custom_fields[4][4]")),
            row("抓轨日志得分", fields.get("custom_fields[4][5]")),
        ]),
        "".join(tag_cells),
        html.escape(", ".join(tags_sent)),
        len(tags_sent),
        (("<span class='bad'>未映射（显式列出，未静默丢）：%s</span>"
          % html.escape(", ".join(dropped))) if dropped else
         "★ 未映射的标签会显式列出，不静默丢。"),
        bbcode_to_html(fields.get("descr") or ""),
        html.escape(str(info.get("tracklist") or "")),
    )


def _first_cover(info):
    """
    取第一个封面 URL。

    ★ `cover_candidates` 实测是**元组列表**（不是字符串列表）——
      我第一版直接 `[0]` 当字符串用，第二个样本上当场 `AttributeError:
      'tuple' object has no attribute 'strip'`。
      ⇒ 这里显式拆开两种形态，并**只取 URL 那一段**，不让类型猜。
    """
    cands = info.get("cover_candidates") or []
    for c in cands:
        if isinstance(c, str) and c.strip():
            return c.strip()
        if isinstance(c, (tuple, list)) and c:
            for part in c:
                if isinstance(part, str) and part.strip().startswith("http"):
                    return part.strip()
    return ""


def main(argv):
    path = argv[1] if len(argv) > 1 else DEFAULT_SAMPLE
    if not os.path.exists(path):
        sys.exit("找不到文件: " + path)

    with open(path, encoding="utf-8", errors="replace") as fh:
        info = P.parse_detail(fh.read())
    info["_source_file"] = path
    m = M.map_all(info)

    task = {
        "formatted_title": m["title"],
        "torrent_name": m["title"],
        "tracklist": m["tracklist"],
        "log_content": m["log_content"],
        "cover_url": _first_cover(info),
        "tags": m["tags"],
        "artist": m["artist"],
        "year": m["year"],
        "team_sel": m["team_sel"],
        # ★★ 媒介/编码：**必须传进 task**，否则 build_form_fields 会退回
        #    `detect_medium()` 从标题猜 —— 那会把皇后的 `Web` 猜成 `CD`。
        "medium_sel": m["medium_sel"],
        "audiocodec_sel": m["audiocodec_sel"],
        "opencd_tid": info.get("torrent_id") or "sample",
    }
    fields = descr.build_form_fields(task, autodetect=True)

    os.makedirs(OUT_DIR, exist_ok=True)
    base = re.sub(r"[^\w\-]+", "_", os.path.splitext(os.path.basename(path))[0])[:60]
    out = os.path.join(OUT_DIR, base + ".preview.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(build_html(info, m, fields))

    print("已生成预览: " + os.path.relpath(out, ROOT).replace(os.sep, "/"))
    print("用浏览器打开即可（file:// 直接开，无需服务器）")
    print("  发出的 tags[4][] = %s" % (fields.get("tags[4][]")))
    print("  team_sel[4]      = %s" % fields.get("team_sel[4]"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
