# -*- coding: utf-8 -*-
"""通用站点探针：下载发布页 / 规则页到本地 HTML，解析表单，抓 passkey。

═══════════════════════════════════════════════════════════════════════════
职责（**与具体站点无关**，任何 NexusPHP 站都能用）
═══════════════════════════════════════════════════════════════════════════
· `download_page()`    GET 一个页面 → 按 `<title>` 生成浏览器保存风格的文件名 → 存本地；
· `parse_upload_form()` 解析 `<form>`：action / enctype / 每个 select 的 option / input 名；
· `fetch_passkey()`    从用户控制台（`usercp.php` / `my.php`）的「密钥」单元格抓 passkey。

★ 为什么抽成公共模块：接入新站时"存 HTML + 肉眼抄字段 + 抄 passkey"是重复劳动，
  而且手抄必然漏字段。把它收敛成一套函数，供 `tools/probe_site.py`（接入期跑一次）
  与各站 `<站>_site.py`（运行时可选调用）共用（`ERR-META-03`：同一件事别有多份实现）。

★ 本模块只做 GET、只读；passkey 是凭据，**只返回**，不打印、不落盘、不进日志。
"""
import os
import re
import urllib.parse

import bs4


# ==================== 文件名与页面 ====================

def extract_title(html):
    """取 `<title>` 文本（单行、去多余空白）。"""
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def title_to_filename(title):
    """浏览器保存风格的文件名：`A :: B` → `A __ B`；`A | B` → `A _ B`。

    ★ 与"另存为"得到的名字一致，接入期靠它对齐既有 `opencdhtml/` 里的存档。
    """
    s = (title or "").strip()
    s = s.replace("::", "__").replace("|", "_")
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or "page"


def inject_base(html, base):
    """注入 `<base>`，让相对路径的 CSS/图片仍从站点取（本地双击打开不裸奔）。"""
    base_tag = f'<base href="{base.rstrip("/")}/">'
    if re.search(r"<base\b", html, re.I):
        return html
    if re.search(r"<head[^>]*>", html, re.I):
        return re.sub(r"(<head[^>]*>)", r"\1\n" + base_tag, html, count=1, flags=re.I)
    return base_tag + "\n" + html


def save_html(html, out_dir, filename):
    """把 HTML 写到 out_dir/filename.html，返回绝对路径。"""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename + ".html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path


def download_page(sess, base, path, out_dir, *, inject=True):
    """GET 一个页面并存成 HTML。

    返回 `(本地路径, 原始 HTML, 页面 title)`。`path` 可以是相对路径或完整 URL。
    """
    url = path if path.startswith("http") else f"{base.rstrip('/')}/{path.lstrip('/')}"
    r = sess.get(url, timeout=30)
    raw = r.text or ""
    title = extract_title(raw)
    fname = title_to_filename(title)
    html = inject_base(raw, base) if inject else raw
    out = save_html(html, out_dir, fname)
    return out, raw, title


# ==================== 表单解析 ====================

def parse_upload_form(html):
    """解析发布表单，返回：

    ```
    {
      "action": "takeupload.php", "method": "post", "enctype": "multipart/form-data",
      "selects": { "type": [{"value": "408", "text": "HQ Audio/音乐"}, ...], ... },
      "inputs":  ["file", "name", "small_descr", ...],
    }
    ```

    找不到表单返回 None。
    """
    soup = bs4.BeautifulSoup(html or "", "html.parser")

    form = None
    for f in soup.find_all("form"):
        action = (f.get("action") or "").lower()
        if "takeupload" in action or f.get("id") == "compose":
            form = f
            break
    if form is None:
        form = soup.find("form")
    if form is None:
        return None

    selects = {}
    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        selects[name] = [
            {"value": (opt.get("value") or ""), "text": opt.get_text(strip=True)}
            for opt in sel.find_all("option")
        ]

    inputs = []
    for el in form.find_all(["input", "textarea"]):
        name = el.get("name")
        if name and name not in inputs:
            inputs.append(name)

    return {
        "action": form.get("action"),
        "method": (form.get("method") or "").lower(),
        "enctype": (form.get("enctype") or "").lower(),
        "selects": selects,
        "inputs": inputs,
    }


# ==================== passkey ====================

# NexusPHP 用户控制台里「密钥」/「个人密钥」/「Passkey」后面跟着的 32~64 位 hex。
_PASSKEY_PATTERNS = (
    re.compile(r"(?:密钥|密鑰|個人密鑰|个人密钥)\s*</td>\s*<td[^>]*>\s*([0-9a-fA-F]{32,64})\s*<"),
    re.compile(r"(?:passkey|secret\s*key|pass\s*key)[^0-9a-fA-F]{0,40}([0-9a-fA-F]{32,64})", re.I),
)

# 控制台页面路径候选（不同 NexusPHP 站命名不同）。
_PASSKEY_PATHS = ("/usercp.php", "/my.php", "/userdetails.php")


def fetch_passkey(sess, base):
    """从用户控制台抓 passkey，返回 `(passkey 或 None, 来源路径或 None)`。

    ★ 凭据：只返回给调用方，**不打印、不写文件**。抓不到返回 `(None, None)`。
    """
    for path in _PASSKEY_PATHS:
        try:
            r = sess.get(base.rstrip("/") + path, timeout=25)
        except Exception:                                   # noqa: BLE001
            continue
        if r.status_code != 200:
            continue
        body = r.text or ""
        for pat in _PASSKEY_PATTERNS:
            m = pat.search(body)
            if m:
                return m.group(1), path
    return None, None


# ==================== 规则页发现 ====================

# 「规则 / 教程」类链接关键词，按优先级从高到低（数字越小越优先）。
_RULES_KEYWORDS = (
    ("上传规则", 0), ("upload rule", 0), ("发布规范", 0), ("发种规范", 0),
    ("发种教程", 1), ("upload guide", 1), ("规则", 2), ("rules", 2),
)


def find_rules_url(html, base):
    """从发布页里找「上传规则 / 发种教程 / rules」链接，返回绝对 URL 或 None。

    ★ 优先「上传规则」，其次「发种教程」，最后泛化的「规则/rules」。
      找不到就返回 None（调用方可用 `--rules-url` 显式指定）。
    """
    soup = bs4.BeautifulSoup(html or "", "html.parser")
    cands = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        blob = (href + " " + a.get_text(" ", strip=True)).lower()
        for kw, rank in _RULES_KEYWORDS:
            if kw in blob:
                cands.append((rank, href))
                break
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    href = cands[0][1]
    return href if href.startswith("http") else urllib.parse.urljoin(
        base.rstrip("/") + "/", href)


# ==================== 综合探针 ====================

def probe(sess, base, out_dir, *, rules_url=None):
    """综合探针：下载发布页、**规则页（必须项）**、解析表单、抓 passkey。

    返回报告 dict：
      `files` / `title` / `form` / `passkey` / `passkey_source`
      `rules` = `{"url", "saved", "ok", "reason"}` —— 规则页是**必须抓取项**，
      抓不到时 `ok=False` 且 `reason` 写明具体原因（不静默）。
    """
    report = {
        "base": base,
        "title": None,
        "files": [],
        "form": None,
        "passkey": None,
        "passkey_source": None,
        "rules": {"url": None, "saved": None, "ok": False, "reason": ""},
    }

    # ① 发布页
    out, raw, title = download_page(sess, base, "/upload.php", out_dir)
    report["files"].append(out)
    report["title"] = title
    report["form"] = parse_upload_form(raw)

    # ② 规则页（★ 必须项：抓不到必须给出可执行的原因）
    rules = rules_url or find_rules_url(raw, base)
    if not rules:
        report["rules"]["reason"] = (
            "发布页中未发现「上传规则 / 发种教程」链接 ⇒ 无法自动定位；"
            "请用 --rules-url 指定规则页 URL（一般在站内 wiki 或论坛置顶帖）")
    else:
        report["rules"]["url"] = rules
        try:
            url2 = rules if rules.startswith("http") else f"{base.rstrip('/')}/{rules.lstrip('/')}"
            r2 = sess.get(url2, timeout=30)
            body2 = r2.text or ""
            title2 = extract_title(body2)
            if r2.status_code != 200:
                hint = "（Cookie 失效或无权限）" if r2.status_code in (401, 403) else ""
                report["rules"]["reason"] = f"规则页请求失败 HTTP {r2.status_code}{hint}：{rules}"
            elif title2 and "login" in title2.lower():
                report["rules"]["reason"] = (
                    f"规则页落在登录页 ⇒ 该页需要登录或 Cookie 无权限：{rules}")
            elif len(body2) < 1500:
                report["rules"]["reason"] = (
                    f"规则页内容过短（{len(body2)}B），疑似 404/空页：{rules}")
            else:
                fname = title_to_filename(title2 or "rules")
                saved = save_html(inject_base(body2, base), out_dir, fname)
                report["files"].append(saved)
                report["rules"]["saved"] = saved
                report["rules"]["ok"] = True
        except Exception as e:                              # noqa: BLE001
            report["rules"]["reason"] = f"规则页请求异常：{type(e).__name__}: {e}"

    # ③ passkey
    report["passkey"], report["passkey_source"] = fetch_passkey(sess, base)
    return report
