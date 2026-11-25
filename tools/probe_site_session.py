# -*- coding: utf-8 -*-
"""站点登录态（CookieCloud 会话）只读探针 (probe_site_session.py)

为什么需要它
------------
NexusPHP 站点的 CookieCloud 载荷里 cookie 可能"未过期"但服务端会话已经失效，
此时站内搜索一律 0 命中，极易被误判成「站点没这个种子」。
2026-11-25 就因此把 `2008 - 五月蠅` / `AK79` 两条读成"搜不到"，实际是未登录（专题 17）。

本工具只读：取适配器的 CookieCloud cookie → GET 一个需要登录的页面 →
按登录页特征判定会话是否有效。**只打印 cookie 名，绝不打印 cookie 值**。

用法
----
    python musichub/tools/probe_site_session.py --site opencd
    python musichub/tools/probe_site_session.py --all
"""

import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from musichub.adapters.sites import (
    MusopiaAdapter, CrabPTAdapter, PTerClubAdapter, XingwanAdapter, HDKylinAdapter,
    TangPTAdapter, MuXueGeAdapter, OpenCDAdapter, UBitsAdapter, SoulVoiceAdapter,
    NanyangPTAdapter, SpringSundayAdapter, LemonHDAdapter, HDTimeAdapter, UHDDolbyAdapter,
)

# 需要登录才能看到种子列表的探针页；登录失效时 NexusPHP 会 302/直出登录页
PROBE_PATH = "/torrents.php"
LOGIN_MARKERS = ("login.php", "takelogin.php", "登錄", "登录", "Sign in", "Login")


def check(adapter) -> None:
    base = getattr(adapter, "base_url", "") or f"https://{getattr(adapter, 'domain', '')}"
    site = getattr(adapter, "site_name", type(adapter).__name__)
    cookies = adapter.get_auth_cookies() or {}
    print(f"\n=== {site} ({base}) ===")
    print(f"  cookie 数={len(cookies)} 名={sorted(cookies.keys())}")
    if not cookies:
        print("  判定: NO_COOKIE（CookieCloud 没有该域名的载荷）")
        return
    try:
        r = adapter.session.get(base + PROBE_PATH, timeout=30, allow_redirects=True)
    except Exception as e:
        print(f"  判定: REQUEST_FAILED {type(e).__name__}: {e}")
        return
    head = (r.text or "")[:4000]
    logged_out = any(m in head for m in LOGIN_MARKERS)
    text_norm = head.replace(" ", "").lower()
    logged_out = logged_out or ("login.php" in r.url.lower())
    print(f"  http={r.status_code} final_url={r.url} len={len(r.text or '')} "
          f"details_links={text_norm.count('details.php?id=')}")
    print(f"  判定: {'SESSION_INVALID（返回登录页，检索必然 0 命中）' if logged_out else 'SESSION_OK'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="站点 CookieCloud 会话有效性只读探针")
    ap.add_argument("--site", default="", help="站点名（适配器 site_name 小写，如 opencd）")
    ap.add_argument("--all", action="store_true", help="巡检全部已接站")
    args = ap.parse_args()

    adapters = [MusopiaAdapter(), CrabPTAdapter(), PTerClubAdapter(), XingwanAdapter(),
                HDKylinAdapter(), TangPTAdapter(), MuXueGeAdapter(), OpenCDAdapter(),
                UBitsAdapter(), SoulVoiceAdapter(), NanyangPTAdapter(), SpringSundayAdapter(),
                LemonHDAdapter(), HDTimeAdapter(), UHDDolbyAdapter()]
    if not args.all:
        if not args.site:
            raise SystemExit("需要 --site <name> 或 --all")
        target = args.site.strip().lower()
        adapters = [a for a in adapters if a.site_name.lower() == target]
        if not adapters:
            raise SystemExit(f"未找到站点适配器: {args.site}")
    for a in adapters:
        check(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
