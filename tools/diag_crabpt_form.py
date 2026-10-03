"""诊断蟹黄堡**发布页**（`takeupload.php`）的真实表单结构 —— **只 GET，不提交**。

═══════════════════════════════════════════════════════════════════════════
为什么需要它（2026-09-28 实测）
═══════════════════════════════════════════════════════════════════════════
`publish.py --site crabpt` 真发时停在：

    ❌ 蟹黄堡发布页缺了这些字段：['type', 'name', 'descr', 'small_descr']
       ⇒ 页面版式已变，本适配层的字段名可能已过期（见 PLAN-crabpt.md 附录 C）

★ 那四个是**最核心**的字段（标题 / 简介 / 副标题 / 类型）—— 全缺 ⇒ 不是"少一个"，
  而是**读法整个不匹配**。`crabpt_site.fetch_upload_form()` 里的读法是：

    form = bs.find("form")                       ← ★ 只找**第一个** <form>
    actions = {i.get("name"): i for i in form.find_all(["input","select","textarea"])}

★ 两个可疑点（本工具就是来分辨它们的）：
  ① **`bs.find("form")` 拿到的可能不是发布表单** —— 页面上若有搜索框、登录框、
     "跳转到"下拉等**更靠前**的 <form>，第一个 <form> 就不是它
     ⇒ 于是 `actions` 是**别的表单**的字段集 ⇒ 核心字段全"缺"。
  ② 站点真的改名了（不太可能四个一起改）。

★★ 判据：**把页面上所有 <form> 逐个列出来**（action / method / enctype / 字段名），
  一眼就能看出 ① 是不是真的 —— 不用猜。

═══════════════════════════════════════════════════════════════════════════
三条纪律
═══════════════════════════════════════════════════════════════════════════
1. **只发 GET**，**绝不 POST** ⇒ 对站点零写入。（本文件里没有 `.post(`。）
2. **Cookie 绝不打印**（同 `guard_auto_key` 的纪律：值不进日志）。
3. 落盘的 HTML 里**可能带**页面上的凭据痕迹 ⇒ 写到 `/data/diag_*.html` 这类
   **临时文件**，命令里提醒用完删。★ 不要把整页 HTML 打进聊天/日志。

用法（**容器内**，因为 Cookie 在容器里）:
    python /data/tools/diag_crabpt_form.py --out /data/diag_crabpt_takeupload.html
"""
import argparse
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="诊断蟹黄堡发布页的表单结构（只 GET）")
    ap.add_argument("--out", default="/data/diag_crabpt_takeupload.html",
                    help="把原始 HTML 落盘到哪（★ 用完请删）")
    ap.add_argument("--all-forms", type=int, default=0,
                    help="只列前 N 个表单（0 = 全列）")
    args = ap.parse_args()

    import crabpt_site
    from bs4 import BeautifulSoup

    client = crabpt_site.CrabptClient()
    if not client.have_cookie():
        sys.exit("❌ CRABPT_COOKIE 是空的 ⇒ 没法读发布页（这不是页面问题）")

    print("=" * 78)
    print("★ 诊断蟹黄堡发布页结构（**只 GET，绝不 POST**）")
    print("=" * 78)

    r = client.get("/takeupload.php")
    body = r.text or ""
    print(f"  HTTP        : {r.status_code}")
    print(f"  响应长度    : {len(body)} 字节")
    print(f"  Content-Type: {r.headers.get('Content-Type')!r}")
    print(f"  是登录页吗  : {'takelogin.php' in body}   （应为 False）")
    print(f"  有 userdetails 链接 : {'userdetails.php' in body}   （登录态在）")

    bs = BeautifulSoup(body, "html.parser")

    # ── ★ 关键判据：页面上**所有** <form>，逐个列 ──
    forms = bs.find_all("form")
    print("\n" + "─" * 78)
    print(f"★ 页面上共有 {len(forms)} 个 <form>")
    print("─" * 78)
    for i, f in enumerate(forms, 1):
        if args.all_forms and i > args.all_forms:
            print(f"  …（按 --all-forms={args.all_forms} 截断）")
            break
        items = [it for it in f.find_all(["input", "select", "textarea"])
                 if it.get("name")]
        names = [it.get("name") for it in items]
        kinds = {}
        for it in items:
            kinds[it.name] = kinds.get(it.name, 0) + 1
        print(f"\n  <form #{i}>  action={f.get('action')!r}  method={f.get('method')!r}"
              f"  enctype={f.get('enctype')!r}  id={f.get('id')!r}")
        print(f"            字段数={len(names)}  类型分布={kinds}")
        print(f"            字段名: {names[:24]}")
        if len(names) > 24:
            print(f"                    …另有 {len(names) - 24} 个")

    # ── 我们期望的四个字段，出现在**哪个** form 里 ──
    WANT = ("type", "name", "descr", "small_descr")
    print("\n" + "─" * 78)
    print(f"★ 期望字段 {list(WANT)} 分别出现在第几个 <form> 里")
    print("─" * 78)
    for w in WANT:
        where = []
        for i, f in enumerate(forms, 1):
            if f.find(attrs={"name": w}) is not None:
                where.append(i)
        print(f"  {w:<12} → {where if where else '❌ 全页都找不到'}")
    # ★ 也按"字段名精确等于"再查一遍（find(attrs=) 会做更宽的属性匹配）
    print("\n  ★ 再按**精确** name 属性核一遍：")
    for w in WANT:
        where = [i for i, f in enumerate(forms, 1)
                 if any(it.get("name") == w for it in f.find_all(["input", "select", "textarea"]))]
        print(f"  {w:<12} → {where if where else '❌ 全页都找不到'}")

    # ── 全页面的 input 字段全集（不按 form 分）——用来对照"到底有没有" ──
    all_names = [it.get("name") for it in bs.find_all(["input", "select", "textarea"])
                 if it.get("name")]
    print("\n" + "─" * 78)
    print(f"★ 全页字段名全集（不按 form 分；共 {len(all_names)} 个）")
    print("─" * 78)
    print("  " + ", ".join(all_names[:60]))
    if len(all_names) > 60:
        print(f"  …另有 {len(all_names) - 60} 个")

    # ── 页面里有没有"权限不足"之类的话术 ──
    for pat in ("权限不足", "没有权限", "无权", "Permission denied",
                "You are not", "禁止", "只能上传"):
        if re.search(pat, body, re.I):
            print(f"\n  ⚠️  页面里出现疑似权限话术: {pat!r}")

    open(args.out, "w", encoding="utf-8").write(body)
    print(f"\n★ 原始 HTML 已落盘: {args.out}")
    print("  ★ 用完请删：docker exec opencd2musopia rm -f " + args.out)
    print("  ★★ 里面可能有页面上的凭据痕迹 ⇒ **别把整页贴进聊天/日志**。")
    print("\n★ 本工具**没有提交任何东西**（全文件只有一次 GET）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
