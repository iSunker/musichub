# -*- coding: utf-8 -*-
"""M-Team JSON API 客户端。

鉴权用 x-api-key（站规禁止第三方工具用 cookie 调接口），key 从环境变量
MTEAM_API_KEY 读取，不落盘、不打印。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.m-team.cc/api"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 已实测的枚举值（其余枚举请用 --categories 现场拉取，不要照抄他站编号）
CATEGORY_MUSIC = 110
CATEGORY_MUSIC_LOSSLESS = 434
CATEGORY_MV = 406
AUDIOCODEC_FLAC = 1
AUDIOCODEC_APE = 2
AUDIOCODEC_WAV = 15
SOURCE_WEB_DL = 8
SOURCE_CD = 10

# 强制走候选的判据（RULES.md §3）
FORCE_OFFER_BYTES = 100 * 1024 ** 3
FORCE_OFFER_CATEGORIES = ("Game", "Software")

# createOredit 的字段名尚未实测确认（HAR 未采集），此处只登记待确认项，
# 不作为可直接提交的载荷使用。
CREATE_OR_EDIT_FIELDS_PENDING = (
    "name", "smallDescr", "descr", "category", "source", "medium",
    "videoCodec", "audioCodec", "standard", "processing", "team",
    "imdb", "douban", "bangumi", "anonymous", "offer",
)


class MTeamError(RuntimeError):
    """接口返回 code != 0 或网络层失败。"""


def mask(secret):
    """脱敏展示，保留首 4 尾 2。"""
    if not secret:
        return "<empty>"
    if len(secret) <= 8:
        return "*" * len(secret)
    return secret[:4] + "*" * 6 + secret[-2:]


def requires_offer(size_bytes, category_name):
    """是否强制走候选。"""
    if size_bytes and size_bytes > FORCE_OFFER_BYTES:
        return True
    return (category_name or "") in FORCE_OFFER_CATEGORIES


class MTeamClient:
    def __init__(self, api_key=None, base=None, timeout=20):
        self.api_key = api_key or os.environ.get("MTEAM_API_KEY", "")
        self.base = (base or API_BASE).rstrip("/")
        self.timeout = timeout

    def _request(self, path, method="GET", params=None, payload=None):
        if not self.api_key:
            raise MTeamError("缺少 api key（环境变量 MTEAM_API_KEY）")
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = None
        headers = {"x-api-key": self.api_key, "User-Agent": USER_AGENT,
                   "Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            raise MTeamError("HTTP %s on %s" % (exc.code, path)) from exc
        except urllib.error.URLError as exc:
            raise MTeamError("网络失败 %s: %s" % (path, exc.reason)) from exc
        try:
            data = json.loads(raw or "{}")
        except ValueError as exc:
            raise MTeamError("响应不是 JSON: %s" % raw[:120]) from exc
        code = str(data.get("code", "0"))
        if code not in ("0", "200"):
            raise MTeamError("接口返回 code=%s message=%s"
                             % (code, data.get("message") or data.get("msg")))
        return data

    def profile(self):
        return self._request("/member/profile")

    def enum_list(self, name):
        """name 取 category / medium / videoCodec / audioCodec / standard /
        source / processing / team 之一。"""
        return self._request("/torrent/%sList" % name)

    def search(self, keyword, page=1, page_size=50):
        return self._request("/torrent/search",
                             payload={"keyword": keyword, "pageNumber": page,
                                      "pageSize": page_size})

    def detail(self, info_hash):
        return self._request("/torrent/detail", params={"id": info_hash})

    def gen_dl_token(self, info_hash):
        """取一次性下载链接（返回的 URL 含凭据，调用方不得打印）。"""
        return self._request("/torrent/genDlToken", params={"id": info_hash})

    def post_create_or_edit(self, payload, dry_run=True):
        if dry_run:
            return {"dry_run": True, "path": "/torrent/createOredit",
                    "payload_keys": sorted(payload.keys()),
                    "payload": payload}
        return self._request("/torrent/createOredit", method="POST", payload=payload)


def selftest():
    assert requires_offer(101 * 1024 ** 3, "Movie") is True
    assert requires_offer(50 * 1024 ** 3, "Movie") is False
    assert requires_offer(1 * 1024 ** 2, "Game") is True
    assert requires_offer(1 * 1024 ** 2, "Software") is True

    assert mask("") == "<empty>"
    shown = mask("abcdefghijklmnop")
    assert shown.startswith("abcd") and shown.endswith("op") and "*" * 6 in shown
    assert mask("abc") == "***"

    client = MTeamClient(api_key="", base="https://example.invalid/api")
    try:
        client.profile()
    except MTeamError as exc:
        assert "缺少 api key" in str(exc), str(exc)
    else:
        raise AssertionError("空 key 未抛错")

    result = client.post_create_or_edit({"name": "x"}, dry_run=True)
    assert result["dry_run"] is True and result["payload"] == {"name": "x"}

    return "mteam_api selftest OK"


def main(argv=None):
    parser = argparse.ArgumentParser(description="M-Team API 探针")
    parser.add_argument("--profile", action="store_true", help="查账号资料与权限")
    parser.add_argument("--categories", help="拉取枚举，如 category / videoCodec")
    parser.add_argument("--search", help="按关键字搜索种子")
    parser.add_argument("--check-offer", nargs=2, metavar=("SIZE_GB", "CATEGORY"),
                        help="判断是否强制候选")
    parser.add_argument("--selftest", action="store_true", help="离线自检")
    args = parser.parse_args(argv)

    if args.selftest:
        print(selftest())
        return 0
    if args.check_offer:
        size_gb = float(args.check_offer[0])
        forced = requires_offer(int(size_gb * 1024 ** 3), args.check_offer[1])
        print("强制候选: %s" % ("是" if forced else "否"))
        return 0

    client = MTeamClient()
    try:
        if args.profile:
            data = client.profile()
            item = data.get("data") or {}
            print("username = %s" % item.get("username"))
            print("role     = %s" % item.get("role"))
            print("authorities = %s" % ",".join(item.get("authorities") or []))
        elif args.categories:
            data = client.enum_list(args.categories)
            for row in data.get("data") or []:
                print("%-6s %s" % (row.get("id"), row.get("name")))
        elif args.search:
            data = client.search(args.search)
            rows = (data.get("data") or {}).get("data") or []
            print("命中 %d 条" % len(rows))
            for row in rows[:20]:
                print("%-34s %s" % (row.get("id"), row.get("name")))
        else:
            parser.print_help()
    except MTeamError as exc:
        print("请求失败: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
