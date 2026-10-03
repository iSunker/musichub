"""探蟹黄堡**空白发布表单页**在哪、表单长什么样 —— **只 GET，零写入**。

═══════════════════════════════════════════════════════════════════════════
两个已确认的 bug（2026-09-28 实测，本工具就是来钉住它们 + 找正确读法）
═══════════════════════════════════════════════════════════════════════════
① ★★ **`GET /takeupload.php` 就是"提交"** —— 它是 POST 端点。
   实测：GET 它 → 站点回「上传失败！请填写必填项目」，**HTTP 200 + 正常导航**，
   页面上只剩一个搜索 <form>（`action='search.php'`）。
   ⇒ `crabpt_site.fetch_upload_form()` 一直拿它当"读基线"，**方法就是错的**。

② ★★ **`bs.find("form")` 取到的是搜索框，不是发布表单。**
   实测（用户 2026-09-24 存下来的**真实发布页** `opencdhtml/蟹黄堡 __ 发布*.html`）：
       <form action="search.php" method="get" target="_blank">      ← 第 1 个
       <form id="compose" enctype="multipart/form-data"
             action="takeupload.php" method="post" name="upload">    ← 第 2 个 ★ 发布表单
   ⇒ 必须 `bs.find("form", id="compose")`。

★★ 两个 bug **互为掩护**：GET 拿到的是错误页 ⇒ "缺字段"看起来像"版式变了"；
   而即使 GET 拿到正常发布页，`find("form")` 也照样只看到搜索框。

★★★ 站点导航给出了正确答案：错误页里写着
       <li><a href="/upload.php">发 布</a></li>
   ⇒ **空白表单页 = `GET /upload.php`**。

═══════════════════════════════════════════════════════════════════════════
纪律
═══════════════════════════════════════════════════════════════════════════
· **只 GET 安全路径**。★ 本工具的候选清单里**绝不含 `takeupload.php`**
  —— 那是 POST 端点，GET 它 = 提交（见 ①）。
· Cookie 绝不打印。
· 默认**不落盘 HTML**（只有给 `--save` 才写，且提醒用完删）。
"""
import argparse
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

# ★★ 只探"看着像表单页"的安全路径。★ 明确排除 takeupload.php（GET 它 = 提交）。
CANDIDATES = ("/upload.php", "/upload.php?type=408")

WANT = ("type", "name", "descr", "small_descr")


def probe(client, path, save=""):
    from bs4 import BeautifulSoup
    print("=" * 78)
    print(f"★ GET {path}")
    print("=" * 78)
    try:
        r = client.get(path)
    except Exception as e:                                   # noqa: BLE001
        print(f"  ❌ 请求异常: {type(e).__name__}: {e}")
        return None
    body = r.text or ""
    print(f"  HTTP        : {r.status_code}")
    print(f"  响应长度    : {len(body)} 字节")
    t = re.findall(r"<title>(.*?)</title>", body, re.S | re.I)
    print(f"  标题        : {t[:1]!r}")
    print(f"  是错误页吗  : {'上传失败' in body}   （应为 False）")

    bs = BeautifulSoup(body, "html.parser")
    forms = bs.find_all("form")
    print(f"\n  form 数     : {len(forms)}")
    for i, f in enumerate(forms, 1):
        items = [it for it in f.find_all(["input", "select", "textarea"]) if it.get("name")]
        names = [it.get("name") for it in items]
        fid = f.get("id")
        act = f.get("action")
        meth = f.get("method")
        enc = f.get("enctype")
        print(f"    #{i} id={fid!r}")
        print(f"       action={act!r} method={meth!r} enctype={enc!r} 字段数={len(names)}")
        print(f"       字段: {names[:18]}")

    comp = bs.find("form", id="compose")
    print()
    if not comp:
        print("  ★ find(form, id='compose') → ❌ 没有 ⇒ 这个路径**不是**发布表单页")
        return None
    names = [it.get("name") for it in comp.find_all(["input", "select", "textarea"])
             if it.get("name")]
    have = [w for w in WANT if w in names]
    print(f"  ★ find(form, id='compose') → ✅ 找到，字段数 {len(names)}")
    print(f"     核心四字段都在吗: {have}  （应为全部 {list(WANT)}）")
    print(f"     缺的: {[w for w in WANT if w not in names]}")
    print(f"     action={comp.get('action')!r} enctype={comp.get('enctype')!r}")
    if save:
        open(save, "w", encoding="utf-8").write(body)
        print(f"\n  ★ 原始 HTML 已落盘: {save}（★ 用完请删）")
    return comp


def main():
    ap = argparse.ArgumentParser(description="探蟹黄堡发布表单页（只 GET 安全路径）")
    ap.add_argument("--save", default="", help="把最后一个成功的页面落盘（★ 用完请删）")
    args = ap.parse_args()

    import crabpt_site
    client = crabpt_site.CrabptClient()
    if not client.have_cookie():
        sys.exit("❌ CRABPT_COOKIE 是空的 ⇒ 先修凭据（这不是页面问题）")

    print("★ 本工具**只 GET**，且候选清单里**不含 takeupload.php**"
          "（那是 POST 端点，GET 它 = 提交）\n")
    hit = None
    for p in CANDIDATES:
        got = probe(client, p, save=args.save if p == CANDIDATES[0] else "")
        if got is not None and hit is None:
            hit, hit_path = got, p
        print()

    print("=" * 78)
    if hit:
        print(f"🎯 结论：空白发布表单页 = **{hit_path}**")
        print("   ⇒ `crabpt_site.fetch_upload_form()` 要改两处：")
        print("     ① GET 路径: takeupload.php → " + hit_path)
        print("     ② 选择器  : find('form') -> find('form', id='compose')")
        print("        ★ 判据同时换：**找不到 #compose 就是「不是发布页」**，")
        print("          不要再用「缺 4 个字段」当判据（那个判据这次误导了人）。")
    else:
        print("❌ 候选路径里没有一个能给出 #compose 表单 ⇒ **先别改代码**，")
        print("   需要你浏览器看一眼「发布」入口实际打开的是哪个 URL。")
    print("=" * 78)
    print("\n★ 本工具**没有提交任何东西**（每次都是 GET 安全路径）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
