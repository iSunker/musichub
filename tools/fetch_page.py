"""抓取站点页面并存成本地 HTML —— 为了在浏览器里看**真实渲染效果**。

★ 与 descr.py 的关系：`descr.build_descr()` 产出的是 **BBCode**，浏览器看不了。
  这个工具走另一条路：直接把**站点已经渲染好的 HTML** 存下来，
  双击打开 = 站上长什么样就是什么样（不是模拟渲染，没有近似）。

★ 为什么注入 `<base>`：NexusPHP 的 CSS/图片是相对路径，
  存到本地后相对路径会指向本地磁盘 ⇒ 页面裸奔。加 `<base href="站点根">`
  让它们仍然按绝对地址去站点取 ⇒ 正常渲染。

用法（在本机，仓库根目录下跑）:
    python tools/fetch_page.py 17096                # details.php?id=17096
    python tools/fetch_page.py 17096 --open         # 存完顺手用浏览器打开
    python tools/fetch_page.py --path /torrents.php
    python tools/fetch_page.py --query "叶惠美"     # 站点搜索（看有没有重复）

★ 只发 GET，不改站点任何东西。
"""
import argparse
import os
import pathlib
import re
import sys
import urllib.parse
import webbrowser

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "_preview"


def _load_dotenv():
    """把仓库根的 .env 灌进 os.environ。

    本机直接跑时没人喂环境变量（那是 compose 的 env_file 干的），
    所以这里自己来。★ 行尾注释按 compose 的行为剥掉（`\\s+#` 之后视为注释）。
    """
    p = ROOT / ".env"
    if not p.exists():
        sys.exit(f"★ 找不到 {p}")
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        os.environ.setdefault(k.strip(), re.sub(r"\s+#.*$", "", v).strip())


def main():
    ap = argparse.ArgumentParser(description="抓站点页面存成本地 HTML（只读）")
    ap.add_argument("torrent_id", nargs="?", help="种子 id（等价于 details.php?id=N）")
    ap.add_argument("--path", help="任意路径，如 /torrents.php")
    ap.add_argument("--query", help="站点搜索关键词（走 torrents.php?search=）")
    ap.add_argument("--open", action="store_true", help="存完用默认浏览器打开")
    ap.add_argument("--name", help="输出文件名（不含扩展名）")
    args = ap.parse_args()

    if not any((args.torrent_id, args.path, args.query)):
        ap.error("至少要给 torrent_id / --path / --query 之一")

    _load_dotenv()
    # ★ 别让 config.py 在 Windows 上真去建 /data：把它指到本地
    os.environ["DATA_DIR"] = str(OUT_DIR / "_scratch")
    os.environ["DB_PATH"] = str(OUT_DIR / "_scratch" / "data.db")

    import config
    from musopia import MusopiaClient, LoginExpired, SiteError

    if args.torrent_id:
        path = f"/details.php?id={urllib.parse.quote(str(args.torrent_id))}"
        slug, params = f"details_{args.torrent_id}", None
    elif args.query:
        path = "/torrents.php"
        slug = "search_" + re.sub(r"[^\w一-鿿]+", "_", args.query)[:40]
        params = {"search": args.query,
                  "incldead": 0, "spstate": 0, "inclbookmarked": 0,
                  "search_area": 0, "search_mode": 0}
    else:
        path = args.path
        slug = re.sub(r"[^\w]+", "_", path).strip("_")[:40]
        params = None

    if args.name:
        slug = args.name

    client = MusopiaClient()
    print(f"站点  {config.MUSOPIA_URL}")
    print(f"请求  {path}  params={params}")
    try:
        r = client.get(path, params=params) if params else client.get(path)
    except (SiteError, LoginExpired) as e:
        sys.exit(f"★ 请求失败: {e}")

    body = r.text or ""
    print(f"HTTP  {r.status_code}   响应长度 {len(body)}")

    if "takelogin.php" in body or "login.php?returnto" in body:
        sys.exit("★ 返回的是登录页 ⇒ Cookie 失效，请更新 .env 里的 MUSOPIA_COOKIE")

    # ★ 注入 <base>，让相对路径的 CSS/图片仍指向站点
    base_tag = f'<base href="{config.MUSOPIA_URL}/">'
    if re.search(r"<base\b", body, re.I):
        pass
    elif re.search(r"<head[^>]*>", body, re.I):
        body = re.sub(r"(<head[^>]*>)", r"\1\n" + base_tag, body, count=1, flags=re.I)
    else:
        body = base_tag + "\n" + body

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{slug}.html"
    out.write_text(body, encoding="utf-8")
    print(f"已存  {out}")
    print(f"      （相对路径的资源仍会从 {config.MUSOPIA_URL} 取，所以看图/样式需要联网）")

    # 顺手报一下「这页里出现了几个 details.php?id=」，搜索页看条数很有用
    ids = sorted({int(m) for m in re.findall(r"details\.php\?id=(\d+)", body)})
    if ids:
        print(f"页内 details.php?id= 共 {len(ids)} 个，范围 {ids[0]} ~ {ids[-1]}")

    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
