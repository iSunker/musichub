"""
Musopia (NexusPHP) 站点客户端。

核心设计：编辑走「读表单 -> 覆盖指定字段 -> 整表提交」，不再手写一份猜的字段列表。
原因是 takeedit.php 是整表覆盖语义：你没提交的字段，站点当空值处理。之前的
update_metadata.py 硬编码了一份字段表，medium/audiocodec/team/tags 全填 0 或干脆
不提交，每编辑一次就把这些属性抹掉一次。先 GET edit.php 把当前值读出来当基线，
只改要改的，这个问题就不存在了。
"""
import os
import re
import time
import html as htmllib

import requests
from bs4 import BeautifulSoup

import config

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 这些是编辑器的排版按钮和文件上传控件，不属于要回填的表单数据
SKIP_NAMES = {"b", "i", "u", "url", "IMG", "list", "quote", "tagcount", "nfo"}


class SiteError(Exception):
    pass


class LoginExpired(SiteError):
    pass


class MusopiaClient:
    def __init__(self, base_url=None, cookie=None, request_interval=1.5):
        self.base = (base_url or config.MUSOPIA_URL).rstrip("/")
        self.cookie = cookie or config.MUSOPIA_COOKIE
        self.interval = request_interval
        self._last_request = 0.0

        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent": UA,
            "Cookie": self.cookie,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    # ---------- 底层 ----------

    def _throttle(self):
        """站点是别人的，别把人家打疼。每次请求之间留间隔。"""
        elapsed = time.time() - self._last_request
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last_request = time.time()

    def _url(self, path):
        return path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"

    def get(self, path, **kw):
        self._throttle()
        kw.setdefault("timeout", 30)
        return self.sess.get(self._url(path), **kw)

    def post(self, path, **kw):
        self._throttle()
        kw.setdefault("timeout", 60)
        return self.sess.post(self._url(path), **kw)

    # ---------- 登录态 ----------

    def check_login(self):
        r = self.get("/torrents.php")
        if r.status_code != 200:
            raise SiteError(f"访问 torrents.php 失败，HTTP {r.status_code}")
        body = r.text or ""
        if "takelogin.php" in body or "login.php?returnto" in body:
            raise LoginExpired("Cookie 已失效，请更新 .env 里的 MUSOPIA_COOKIE")
        m = re.search(r"userdetails\.php\?id=(\d+)", body)
        return m.group(1) if m else None

    @property
    def user_id(self):
        if not hasattr(self, "_uid"):
            self._uid = self.check_login()
        return self._uid

    # ---------- 我的上传列表 ----------

    def iter_my_uploads(self, user_id=None, max_pages=30):
        """
        逐页产出 (torrent_id, 标题)。
        接口：getusertorrentlistajax.php?userid=&type=uploaded&page=N（0 基，100 条/页）
        """
        uid = user_id or self.user_id
        if not uid:
            raise SiteError("无法确定 user_id，请检查 Cookie 或显式传入 user_id")

        seen = set()
        for page in range(max_pages):
            r = self.get(
                "/getusertorrentlistajax.php",
                params={"userid": uid, "type": "uploaded", "page": page},
                headers={"X-Requested-With": "XMLHttpRequest",
                         "Referer": self._url(f"/userdetails.php?id={uid}")},
            )
            if r.status_code != 200:
                raise SiteError(f"拉取上传列表失败 page={page}, HTTP {r.status_code}")

            found = 0
            for tid, title in _parse_torrent_links(r.text):
                if tid in seen:
                    continue
                seen.add(tid)
                found += 1
                yield tid, title

            if found == 0:
                break

    def fetch_my_uploads(self, user_id=None):
        return list(self.iter_my_uploads(user_id=user_id))

    # ---------- 编辑 ----------

    def fetch_edit_form(self, torrent_id):
        """
        GET edit.php?id=N，把 takeedit.php 表单的当前值解析成 dict。
        返回 (fields, current_snapshot)，fields 可直接提交。
        """
        r = self.get("/edit.php", params={"id": torrent_id})
        if r.status_code != 200:
            raise SiteError(f"打开 edit.php?id={torrent_id} 失败，HTTP {r.status_code}")
        if "takelogin.php" in (r.text or ""):
            raise LoginExpired("Cookie 已失效")

        soup = BeautifulSoup(r.text, "html.parser")
        form = None
        for fm in soup.find_all("form"):
            if "takeedit" in (fm.get("action") or ""):
                form = fm
                break
        if form is None:
            raise SiteError(
                f"id={torrent_id} 的页面里找不到 takeedit 表单"
                "（种子可能已被删除，或当前账号没有编辑权限）"
            )

        fields = {}
        for el in form.find_all(["input", "select", "textarea"]):
            name = el.get("name")
            if not name or name in SKIP_NAMES:
                continue

            if el.name == "input":
                typ = (el.get("type") or "text").lower()
                if typ in ("button", "submit", "reset", "file", "image"):
                    continue
                val = el.get("value") or ""
                if typ in ("checkbox", "radio"):
                    # 只有勾选/选中的才算提交值
                    if not el.has_attr("checked"):
                        continue
                    if name.endswith("[]"):
                        fields.setdefault(name, []).append(val)
                    else:
                        fields[name] = val
                else:
                    fields[name] = val

            elif el.name == "textarea":
                fields[name] = htmllib.unescape(el.get_text() or "")

            else:  # select
                selected = [o.get("value") or "" for o in el.find_all("option")
                            if o.has_attr("selected")]
                if not selected:
                    opts = el.find_all("option")
                    selected = [opts[0].get("value") or ""] if opts else [""]
                fields[name] = selected[0]

        fields["id"] = str(torrent_id)
        return fields, dict(fields)

    def edit_torrent(self, torrent_id, overrides, *, dry_run=False,
                     keep_tags=True, verbose=False, prefetched=None):
        """
        编辑种子页面。overrides 里给什么就改什么，其余字段沿用页面上的现值。

        返回 (ok: bool, message: str)。
        dry_run=True 时只读表单并打印将要变更的字段，不提交。
        prefetched: 可选 (fields, before) 元组，由 fetch_edit_form() 预取。
        传了就不再重复 GET edit.php——update_metadata.py 的"本地无封面时
        沿用站点现值"流程需要先读表单提取封面，再渲染 overrides 再提交，
        若这里再拉一次表单就是同一页面每轮多敲一次站点。
        """
        if prefetched is not None:
            fields, before = prefetched
            if "id" not in fields:
                fields["id"] = str(torrent_id)
                before = dict(fields)
        else:
            fields, before = self.fetch_edit_form(torrent_id)

        if not keep_tags:
            fields.pop("tags[4][]", None)

        # nfoaction 必须显式保留，否则站点可能按"未选择"处理掉已有 nfo
        fields.setdefault("nfoaction", "keep")

        changes = []
        for k, v in overrides.items():
            old = before.get(k)
            if isinstance(v, (list, tuple)):
                v = [str(x) for x in v]
            else:
                v = "" if v is None else str(v)
            if old != v:
                changes.append((k, old, v))
            fields[k] = v

        if verbose or dry_run:
            if not changes:
                print(f"    ℹ️  id={torrent_id} 无字段变化")
            for k, old, new in changes:
                if k == "descr":
                    print(f"    · descr: {_short(old)}  ->  {_short(new)}")
                else:
                    print(f"    · {k}: {old!r} -> {new!r}")

        if dry_run:
            return True, f"dry-run：{len(changes)} 个字段将被修改"

        if not changes:
            return True, "无需修改"

        # takeedit.php 用 multipart/form-data；requests 的 files= 能正确编码
        # 中文与换行，且和抓包结构一致。列表字段要展开成多个同名分片。
        parts = []
        for k, v in fields.items():
            if isinstance(v, list):
                for item in v:
                    parts.append((k, (None, item)))
            else:
                parts.append((k, (None, v)))

        r = self.post(
            "/takeedit.php",
            files=parts,
            headers={"Referer": self._url(f"/edit.php?id={torrent_id}"),
                     "Origin": self.base},
            allow_redirects=False,
        )

        if r.status_code in (301, 302, 303):
            loc = r.headers.get("Location", "")
            if "details.php" in loc or f"id={torrent_id}" in loc:
                return True, f"已提交（302 -> {loc}）"
            return False, f"重定向到异常位置: {loc}"

        if r.status_code == 200:
            body = r.text or ""
            err = _extract_error(body)
            if err:
                return False, f"站点返回错误: {err}"
            # NexusPHP 编辑成功有时直接渲染详情页
            if "details.php" in body or "编辑" in body[:5000]:
                return True, "已提交（HTTP 200）"
            return True, "已提交（HTTP 200，未识别到明确回执）"

        return False, f"HTTP {r.status_code}"

    # ---------- 详情页校验 ----------

    def fetch_details(self, torrent_id):
        """读取详情页，用于确认编辑结果真的落地了。"""
        r = self.get("/details.php", params={"id": torrent_id, "hit": 1})
        if r.status_code != 200:
            raise SiteError(f"打开 details.php?id={torrent_id} 失败，HTTP {r.status_code}")

        soup = BeautifulSoup(r.text, "html.parser")
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""
        imgs = [im.get("src") for im in soup.find_all("img")
                if (im.get("src") or "").startswith("http")]
        return {
            "title": title,
            "has_cover": any("picgo" in (s or "") for s in imgs),
            "image_count": len(imgs),
            "raw_len": len(r.text),
        }

    # ---------- 上传 ----------

    def upload_torrent(self, torrent_path, fields):
        """发布新种子，返回 torrent_id 或 None。"""
        if not os.path.exists(torrent_path):
            raise SiteError(f"种子文件不存在: {torrent_path}")

        with open(torrent_path, "rb") as f:
            files = {"file": (os.path.basename(torrent_path), f,
                              "application/x-bittorrent")}
            r = self.post(
                "/takeupload.php",
                data=fields,
                files=files,
                headers={"Referer": self._url("/upload.php"), "Origin": self.base},
                allow_redirects=False,
            )

        loc = r.headers.get("Location", "")
        m = re.search(r"id=(\d+)", loc)
        if m:
            return m.group(1), "ok"

        err = _extract_error(r.text or "")
        return None, err or f"未拿到重定向 ID，HTTP {r.status_code}"

    def download_torrent(self, torrent_id):
        r = self.get("/download.php", params={"id": torrent_id})
        if r.status_code == 200 and r.content.startswith(b"d"):
            return r.content
        return None


# ==================== 辅助 ====================

def _parse_torrent_links(html_text):
    """从列表页 HTML 抽 (id, 标题)。标题优先取 a[title]，那里是完整不截断的。"""
    soup = BeautifulSoup(html_text, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        m = re.match(r"details\.php\?id=(\d+)", a["href"])
        if not m:
            continue
        title = a.get("title") or a.get_text(strip=True)
        if not title:
            continue
        out.append((m.group(1), htmllib.unescape(title).strip()))
    return out


def _extract_error(body):
    """NexusPHP 出错时通常渲染一个 '错误' 页面，把提示抠出来方便排查。"""
    if not body:
        return ""
    soup = BeautifulSoup(body, "html.parser")
    for sel in ("#outer table td.text", "td.embedded", "h2", "h1"):
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(" ", strip=True)
            if txt and len(txt) < 300 and ("错误" in txt or "Error" in txt or "失败" in txt):
                return txt
    m = re.search(r"(?:错误|Error)[：:]\s*([^<\n]{4,200})", body)
    return m.group(1).strip() if m else ""


def _short(text, n=60):
    text = re.sub(r"\s+", " ", str(text or ""))
    return f"[{len(text)}字符] {text[:n]}..." if len(text) > n else f"[{len(text)}字符] {text}"


def normalize_title(s):
    """标题归一化，用于本地记录与站点条目的匹配。"""
    if not s:
        return ""
    s = htmllib.unescape(str(s))
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()
