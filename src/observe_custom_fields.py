"""★ 只读观测：抓一份 edit.php?id=17099，看 custom_fields 到底是什么元素、值在哪。

★★ 本脚本**只观测、不下结论、不改代码**。
   ⇒ 它**只**做三件事：
     ① 出网 1 次（过 Requests-Session，但 edit.php 是 **Musopia** 不是皇后 ⇒
        不过皇后 RateGate，也不进皇后配额台账 —— 两个站点两本账）；
     ② 把整页 HTML 落到 Musopia 的落盘目录（不进 deploy 白名单）；
     ③ 在 HTML **原文**上做若干**计数与取样**，把数字打出来。

★ 为什么落盘而不是直接分析：改一次解析要重放几次 ⇒ 不能每次都再敲站点
  （`crawl.save_raw` 的 docstring 就是这个理由，本脚本沿用同一规矩）。
★ 为什么用 `crawl.save_raw`：它已经在落盘目录与原子替换上都写好了；
  复用同一个函数 ⇒ 不会出现"两份落盘目录口径"。
"""
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import crawl          # ★ 只为 save_raw / RAW_DIR —— 不重造落盘
import musopia

TID = "17099"
KEY = "custom_fields"

print("=" * 74)
print("★ 只读观测：edit.php 上的 custom_fields 元素形态")
print("=" * 74)

c = musopia.MusopiaClient()
r = c.get("/edit.php", params={"id": TID})
html = r.text or ""
print(f"  HTTP      : {r.status_code}")
print(f"  body 字节 : {len(html)}")
print(f"  ★ 注意     : 200 + 0 字节 =  形状（'回了个空壳'），"
      f"不是'站点上没有'")

if not html.strip():
    sys.exit("❌ 空 body ⇒ 观测不成立，别据此改代码")

p = crawl.save_raw(f"edit_{TID}", html)
print(f"  落盘      : {p}")

# ── ① 出现次数 ──────────────────────────────────────────────────────────
n_total = html.count(KEY)
print(f"\n① '{KEY}' 在整页出现 {n_total} 次")

# ── ② 带 name= 的（即表单控件）──────────────────────────────────────────
#   ★ 用宽松正则：属性顺序不保证，name 与 value 之间可能有别的属性
pat = re.compile(r"<[^>]*name\s*=\s*[\"']?(" + KEY + r"\[[^\"'>]*)[\"']?[^>]*>", re.I)
hits = pat.findall(html)
uniq = sorted(set(hits))
print(f"② 带 name= 的表单元素: {len(hits)} 个（去重 {len(uniq)} 个）")
for u in uniq:
    print(f"     {u}")

# ── ③ 逐元素把整段标签打出来（★ 关键：看元素类型 + 值在哪个属性）─────────
print(f"\n③ 逐元素原样（最多 20 个）")
tags = re.findall(r"<[^>]*name\s*=\s*[\"']?" + KEY + r"\[[^>]*>", html, re.I)
for i, t in enumerate(tags[:20], 1):
    print(f"  [{i:>2}] {t[:300]}")

# ── ④ 元素类型分布（不猜，直接数）────────────────────────────────────────
print(f"\n④ 元素类型分布")
for e in ("input", "select", "textarea", "div", "span", "td"):
    n = len(re.findall(r"<" + e + r"\b[^>]*name\s*=\s*[\"']?" + KEY, html, re.I))
    if n:
        print(f"     <{e}>  {n}")

# ── ⑤ ★ 反向观测：站点**有没有**回填 'サカナクション' 这个值 ─────────────
#   ★ 这是**判据的核心**：HTML 里根本没有这个值 ⇒ 站点不回填 ⇒
#     我们改解析也读不到（那是**站点行为**，不是我们的 bug）⇒ 结论会完全不同。
print(f"\n⑤ ★ 反向观测：站点 HTML 里有没有回填已知值")
for v in ("サカナクション", "怪獣", "いらない"):
    idx = html.find(v)
    print(f"     {v!r:<20} 出现位置 {idx}"
          + (f"  ← 上下文: {html[max(0,idx-160):idx+60]!r}" if idx >= 0 else ""))

# ── ⑥ takeedit 表单里解析器**看得见**的键（复现 fetch_edit_form 的口径）──
#   ★ 用来回答"'看不见'是因为元素类型被跳过，还是站点真的没这个值"
print(f"\n⑥ fetch_edit_form 实际解析出的键（复现同一口径）")
try:
    fields, _ = c.fetch_edit_form(TID)
    cf = {k: v for k, v in fields.items() if KEY in k}
    print(f"     解析出 {len(fields)} 个键；其中含 '{KEY}' 的 {len(cf)} 个")
    for k in sorted(cf):
        print(f"       {k} = {cf[k]!r}")
    if not cf:
        print("     ❌ 一个都没有 —— 与「读回 None」的观测一致")
except Exception as e:
    print(f"     ⚠️ {type(e).__name__}: {e}")

print("\n" + "=" * 74)
print("★ 观测到此。**下一步由数据决定**，不要现在改代码。")
print("=" * 74)
