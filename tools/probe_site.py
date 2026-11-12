# -*- coding: utf-8 -*-
"""接入期探针：自动下载目标站发布页/规则页到 `opencdhtml/`，并解析字段、抓 passkey。

═══════════════════════════════════════════════════════════════════════════
用途（对应 AGENTS.md §7 SKILL-1 / SKILL-2）
═══════════════════════════════════════════════════════════════════════════
给一个新站做接入考察时，不用再"手工另存网页 + 肉眼抄字段 + 抄 passkey"：

    python tools/probe_site.py --site hdkylin

它会：
  ① GET `/upload.php` → 按 `<title>` 命名存进 `opencdhtml/`（浏览器另存为同款名字）；
  ② 尽力从发布页找「上传规则/wiki」链接并一起存档；
  ③ 解析表单：action / enctype / 每个 select 的全部 option / input 名；
  ④ 从用户控制台抓 passkey（默认脱敏显示）。

★ 只发 GET、只读；passkey 是凭据，默认只显示前 4 后 4，`--show-passkey` 才全部显示。
"""
import argparse
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

OUT_DEFAULT = ROOT / "opencdhtml"

# 站点名 → (URL 环境变量名, COOKIE 环境变量名, 默认 URL)
SITES = {
    "hdkylin": ("HDKYLIN_URL", "HDKYLIN_COOKIE", "https://www.hdkyl.in"),
    "crabpt": ("CRABPT_URL", "CRABPT_COOKIE", "https://crabpt.vip"),
    "pter": ("PTER_URL", "PTER_COOKIE", "https://pterclub.net"),
    "xingwan": ("XINGWAN_URL", "XINGWAN_COOKIE", "https://xingwan.cc"),
    "musopia": ("MUSOPIA_URL", "MUSOPIA_COOKIE", "https://www.musopia.vip"),
}


def _load_dotenv():
    """把仓库根 `.env` 灌进 os.environ（行尾 ` # 注释` 按 compose 行为剥掉）。"""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        os.environ.setdefault(k.strip(), re.sub(r"\s+#.*$", "", v).strip())


def _mask(secret):
    if not secret:
        return "（未取到）"
    if len(secret) <= 8:
        return secret
    return f"{secret[:4]}…{secret[-4:]}（len={len(secret)}）"


def _print_form(form):
    if not form:
        print("⚠️  未解析到发布表单（可能 Cookie 失效，或该账号无发种权限）")
        return
    print(f"   action   = {form['action']}")
    print(f"   method   = {form['method']}")
    print(f"   enctype  = {form['enctype']}")
    print(f"   select   = {len(form['selects'])} 个")
    for name, opts in form["selects"].items():
        preview = "、".join(f"{o['value']}={o['text']}" for o in opts[:8])
        more = " …" if len(opts) > 8 else ""
        print(f"     - {name}（{len(opts)} 项）：{preview}{more}")
    print(f"   input    = {', '.join(form['inputs'])}")


def main():
    ap = argparse.ArgumentParser(description="接入期站点探针：下载发布页/规则页并解析（只读）")
    ap.add_argument("--site", choices=sorted(SITES), help="站点名（自动取 URL/COOKIE 环境变量）")
    ap.add_argument("--base", help="站点根 URL（与 --site 二选一，覆盖 --site）")
    ap.add_argument("--cookie-env", help="Cookie 所在的环境变量名（默认按 --site 推断）")
    ap.add_argument("--rules-url", help="规则页 URL（默认从发布页自动发现）")
    ap.add_argument("--out", help=f"输出目录（默认 {OUT_DEFAULT}）")
    ap.add_argument("--show-passkey", action="store_true", help="明文显示 passkey（默认脱敏）")
    args = ap.parse_args()

    _load_dotenv()
    # ★ 让 config 别在 Windows 上真去建 /data
    os.environ.setdefault("DATA_DIR", str(ROOT / ".scratch"))
    os.environ.setdefault("DB_PATH", str(ROOT / ".scratch" / "data.db"))
    for k in ("QB_PASS", "MUSOPIA_COOKIE", "MUSOPIA_PASSKEY", "PICGO_API_KEY"):
        os.environ.setdefault(k, "x")

    import config                                            # noqa: E402
    import requests                                          # noqa: E402
    import site_probe                                        # noqa: E402

    base = args.base
    cookie_env = args.cookie_env
    if args.site:
        url_key, ck_key, default_url = SITES[args.site]
        base = base or getattr(config, url_key, default_url)
        cookie_env = cookie_env or ck_key
    if not base:
        ap.error("需要 --site 或 --base")
    cookie = (os.getenv(cookie_env or "") or "").strip()
    if not cookie:
        ap.error(f"环境变量 {cookie_env or '(未指定)'} 为空 ⇒ 先在该站 .env 里填 Cookie")

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
        "Cookie": cookie,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })

    out_dir = pathlib.Path(args.out) if args.out else OUT_DEFAULT
    print("=" * 60)
    print(f"站点  {base}")
    print(f"输出  {out_dir}")
    print("=" * 60)

    report = site_probe.probe(sess, base, str(out_dir), rules_url=args.rules_url)

    print("\n【发布页】")
    print(f"   title = {report['title']}")
    for f in report["files"]:
        print(f"   已存   {f}")
    _print_form(report["form"])

    print("\n【规则页】（★ 必须项）")
    rules = report["rules"]
    if rules["ok"]:
        print(f"   ✅ {rules['url']}")
        print(f"   已存   {rules['saved']}")
    else:
        print(f"   ❌ 抓取失败：{rules['reason']}")

    print("\n【passkey】")
    print(f"   来源   {report['passkey_source'] or '（未找到）'}")
    print(f"   值     {report['passkey'] if args.show_passkey else _mask(report['passkey'])}")

    if not rules["ok"]:
        print("\n⚠️ 规则页是必须项，未抓到 ⇒ 探针未完成。请按上面的原因处理后重跑"
              "（或用 --rules-url 指定规则页）。")
        sys.exit(2)

    print("\n✅ 探针完成。字段解析结果可直接用于编写 <站>_site.py 的映射（对照 AGENTS.md §7 SKILL-3）。")


if __name__ == "__main__":
    main()
