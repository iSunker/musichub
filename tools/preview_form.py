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
import glob
import html
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import descr
import opencd as _opencd
M = P = _opencd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "_preview")
DEFAULT_SAMPLE = os.path.join(
    ROOT, "opencdhtml", "sanitized", "sample-zhuanzai-huanying.html")


def bbcode_to_html(text):
    """把 BBCode **粗浅**转成 HTML —— 只为预览，不求完整。

    ★ 刻意保持"浅"：预览的作用是让人**看出结构**（封面/曲目/折叠日志），
      不是做一个完整 BBCode 引擎。转不了的原文照显，不做花哨兜底。

    ★★ 2026-09-21 修一个**顺序 bug**（这是封面在预览里恒为空的原因）：
      原先把 `[img]` 转换放在**去标签之后**，而那条去标签正则写的是
      `\\[/?(center|quote|b|…)[^\\]]*\\]` —— `[^\\]]*` 会匹配 `[` 之后的**任意非 `]` 字符**，
      于是 **`[img]` 与 `[/img]` 自己也被它吃掉了**（实测：`[img]` 拼出的
      `img` 正好落进 `[^\\]]*`）⇒ 后面的 `[img]` 正则再也匹配不到 ⇒ 封面永远不显示。
      ⇒ 修法：**先换 `[img]`（此时标签还在），再去多余标签**。
    """
    if not text:
        return ""
    s = html.escape(text)
    # ① **先**把 [img] 换成 <img>（必须在去标签之前，否则 [img] 会被一起去掉）
    s = re.sub(r"\[img\]\s*(\S+?)\s*\[/img\]",
               r'<img src="\1" style="max-width:320px">', s)
    # ② 再去掉展示性的 BBCode 外壳（不含 img —— 上一步已经处理掉了）
    s = re.sub(r"\[/?(center|quote|b|i|u|code|spoiler)[^\]]*\]", "", s)
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
    取第一个封面地址。

    ★ `cover_candidates` 实测是**元组列表**（不是字符串列表）——
      我第一版直接 `[0]` 当字符串用，第二个样本上当场 `AttributeError:
      'tuple' object has no attribute 'strip'`。
      ⇒ 这里显式拆开两种形态，并**只取 URL 那一段**，不让类型猜。

    ★★ 2026-09-21 订正（第二个真 bug）：此前要求候选 `startswith("http")`
      ⇒ **所有站内 `attachments/…` 候选都被丢掉** ⇒ 预览里封面**恒为空**。
      而实测**一半以上**的封面正是站内相对路径。
      ⇒ 现在**相对地址也接受**：相对的要拼站点前缀才能当图片 URL，
        预览时拼 `OPENCD_URL`（默认 `https://open.cd`）—— 只为看得见，
        真正下载由 `cover_fetch` 负责。
    """
    cands = info.get("cover_candidates") or []
    url = ""
    for c in cands:
        if isinstance(c, str) and c.strip():
            url = c.strip()
            break
        if isinstance(c, (tuple, list)) and len(c) >= 2:
            # (alt, src) —— ★ 取**第二个**（src），不是第一个（alt）
            cand = str(c[1] or "").strip()
            if cand:
                url = cand
                break
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if not url.startswith("http"):
        base = os.getenv("OPENCD_URL", "https://open.cd").rstrip("/")
        return f"{base}/{url.lstrip('/')}"
    return url


def _write_with_cache_bust(base, html_text, keep_stale=False):
    """
    写预览文件，并**保证浏览器不会拿旧缓存糊弄人**。

    ★★ 为什么需要这个（2026-09-21 踩过一次，用户"看了预览还是没改啊"）：
      实测**文件早就改对了**，但 Windows 用 `explorer.exe` 打开 `file://` 时会
      **复用同一个已开的标签**，文件名没变就**直接吃缓存、不重新读盘**
      ⇒ 用户看到的是几分钟前那份 ⇒ 以为"没改"。
      ★ 这是**同一个病**的另一张脸（同本项目 ERR-META 家族）：
        **"我看到的没变" ≠ "文件没变"** —— 中间隔着一个**没被验证的环节**。
      （更早那次是把"提交包"当成"页面"，也是没验证中间环节。）

    ★ 做法：文件名带**时间戳**（`xxx.preview.HHMMSS.html`）。
      文件名变了 ⇒ 浏览器**必须重新读**，缓存骗不了。
    ★ 同时把**上一批**旧文件删掉：否则 `_preview/` 会越积越多，
      而且**旧文件是主动的误导源**（人可能点开的是旧那份）。
      `keep_stale=True` 时保留（调试用）。
    """
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    out = os.path.join(OUT_DIR, f"{base}.preview.{stamp}.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html_text)

    if not keep_stale:
        # 删掉同一个 base 的**其它所有**版本 —— 包括：
        #   · 旧的无时间戳版 `xxx.preview.html`
        #   · 带时间戳的旧批次 `xxx.preview.HHMMSS.html`
        #   · ★ **手造的临时副本**（如 `xxx.v101639.html`）——
        #     实测漏删过一次：我手动生成的那批 `.v101639.html` 没被这个 glob
        #     覆盖到，留在了目录里。**旧文件是主动的误导源**（人可能点开旧那份
        #     还以为是新的）⇒ 所以按 `base + "."` 前缀**全清**，只留刚写的这个。
        stale = [p for p in glob.glob(os.path.join(OUT_DIR, base + ".*"))
                 if os.path.abspath(p) != os.path.abspath(out)]
        for old in stale:
            try:
                os.remove(old)
            except OSError:
                pass
    return out


def _open_in_browser(path):
    """尽力用系统默认方式打开；失败**不算错**（用户可自己双击）。"""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)                       # noqa: S606
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}" >/dev/null 2>&1 &')
        return True
    except Exception:
        return False


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    open_flag = "--open" in argv
    path = args[0] if args else DEFAULT_SAMPLE
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
        # ★★ 副标题（皇后的 `<div class="smalltitle">`）：
        #    **必须传进 task** —— 否则 `build_form_fields` 拿到的是空串，
        #    预览里副标题就永远是空的。
        #    ★ 这跟 artist/year/medium 是**同一个坑**：数据在 `info`/`m` 里有，
        #      但**没传进 task** ⇒ 渲染层看不见 ⇒ 静默变空。
        "small_descr": info.get("small_descr") or "",
        "opencd_tid": info.get("torrent_id") or "sample",
    }
    fields = descr.build_form_fields(task, autodetect=True)

    # ★★ 传参完整性自检 —— 防的是**同一个坑反复踩**（2026-09-21 连踩三次）：
    #   `render_compare` 漏传 artist/year、`preview_form` 漏传 small_descr，
    #   两次都是"数据在手边、就是没传进 task"⇒ 渲染层看不见 ⇒ **静默变空**。
    #   这里把「皇后页面有值、但 task 没传」的情形**当场喊出来**。
    gap = []
    for src_key, task_key, label in (
            ("small_descr", "small_descr", "副标题"),
            ("artist", "artist", "艺术家"),
            ("year", "year", "发行年份")):
        if (info.get(src_key) or "").strip() and not (task.get(task_key) or "").strip():
            gap.append(label)
    if gap:
        print("  ⚠️  传参缺口（页面有值但没传进 task）：%s" % "、".join(gap))

    base = re.sub(r"[^\w\-]+", "_", os.path.splitext(os.path.basename(path))[0])[:60]
    out = _write_with_cache_bust(base, build_html(info, m, fields))

    rel = os.path.relpath(out, ROOT).replace(os.sep, "/")
    print("已生成预览: " + rel)
    print("  ★ 文件名带时间戳 ⇒ 浏览器**必须重新读**，不会拿旧缓存糊弄（旧版已清掉）")
    print("  发出的 tags[4][] = %s" % (fields.get("tags[4][]")))
    print("  team_sel[4]      = %s" % fields.get("team_sel[4]"))
    print("  副标题           = %r" % (fields.get("small_descr") or ""))
    if open_flag:
        print("  已请求浏览器打开" if _open_in_browser(out) else
              "  ⚠️ 自动打开失败，请手动双击上面那个文件")
    else:
        print("  （加 --open 可直接用浏览器打开）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
