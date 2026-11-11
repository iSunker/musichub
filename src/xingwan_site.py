# -*- coding: utf-8 -*-
"""星湾（xingwan.cc）发布适配层 —— 把一条 task 渲染成 `takeupload.php` 的表单字段。

═══════════════════════════════════════════════════════════════════════════
为什么单独一个文件（与乌托邦 musopia.py、蟹黄堡 crabpt_site.py、猫站 pter_site.py 并列）
═══════════════════════════════════════════════════════════════════════════
★ 星湾是 NexusPHP 站点，音乐分类 = 408（HQ Audio），但表单与另外三站都不同：

    字段                    星湾取值
    --------------------    --------------------------------------------
    分类                     `type=408`
    媒介                     `medium_sel[4]`（CD=19 / Track=20 / Other=21）
    视频编码                 `codec_sel[4]`（音乐分类不填）
    分辨率                   `standard_sel[4]`（音乐分类不填）
    制作组                   `team_sel[4]`（音乐分类不填）
    标签                     `tags[4][]`：无损音乐=17、国语=5、英语=35、粤语=31
    匿名                     `uplver=yes`
    简介                     BBCode（与 OpenCD 同源，无需 markdown 转换）

★ 标题/副标题沿用 `descr.normalize_music_title` / `descr.build_music_small_descr`
  —— 与乌托邦/蟹黄堡/猫站同一份口径，避免四站标题漂移。
★ 简介沿用 `descr.build_descr(site=db.SITE_XINGWAN)`：通用 NexusPHP 形态
  （封面 + 来源引用 + 曲目 + 折叠日志）。

★★ 安全铁律：
    作为转载方（从 OpenCD 转载），**严禁勾选**禁转/原创/自购类标签；
    只勾「无损音乐」和由元数据能证明的语言标签。
"""
import json
import os
import re
import time

import config
import net
import db
import descr

# 复用乌托邦的 UA / 错误提取 / 失败页落盘 —— 四个站同属 NexusPHP
from musopia import SiteError, UA, _extract_error, _dump_takeupload

# ==================== 分类与标签 id ====================
MUSIC_TYPE = str(config.XINGWAN_MUSIC_CATEGORY)

TAG_LOSSLESS = "17"          # 无损音乐
TAG_GUOYU = "5"              # 国语
TAG_YUEYU = "31"             # 粤语
TAG_ENGLISH = "35"           # 英语

MEDIUM_CD = "19"             # CD（音乐 CD）
MEDIUM_TRACK = "20"          # Track / Soundtrack（原声音乐 / OST）
MEDIUM_OTHER = "21"          # Other（其他或未知媒介）


def _as_list(value):
    """把 tags 字段统一成 list（可能是 list / JSON 串 / 逗号串 / None）。"""
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(v) for v in data]
        except (ValueError, TypeError):
            pass
    return [x.strip() for x in re.split(r"[,，;；|]", text) if x.strip()]


def map_medium(task):
    """把 task 的介质映射到星湾 `medium_sel[4]`。

    星湾音乐分类只有三个可用值：CD / Track-Soundtrack / Other。
    判据优先看映射快照（`medium_sel` / `source`），再看标题与副标题。
    ★ 兜底取 **CD**（与 `descr.build_music_small_descr` 的默认一致）：
      本项目的目标就是无损**唱片**资源，元数据没写媒介时按 CD 处理；
      只有明确是数字发行（WEB/Digital）时才落 Other。
    """
    corpus = " ".join(
        str((task or {}).get(k) or "")
        for k in ("medium_sel", "source", "medium", "format", "formatted_title",
                  "small_descr", "torrent_name", "tags")
    )
    if re.search(r"原声|OST|Soundtrack", corpus, re.I):
        return MEDIUM_TRACK
    if re.search(r"\bWEB(?:-DL)?\b|Digital|数字发行|流媒体", corpus, re.I):
        return MEDIUM_OTHER
    return MEDIUM_CD


def map_tags(task):
    """生成 `tags[4][]`：必带「无损音乐」，语言标签只在元数据能证明时才加。"""
    tags = [TAG_LOSSLESS]
    corpus = " ".join(
        str((task or {}).get(k) or "")
        for k in ("tags", "language", "formatted_title", "small_descr")
    )
    if re.search(r"国语|华语|中文|Mandarin", corpus, re.I):
        tags.append(TAG_GUOYU)
    if re.search(r"粤语|粵語|Cantonese", corpus, re.I):
        tags.append(TAG_YUEYU)
    if re.search(r"英语|英文|English", corpus, re.I):
        tags.append(TAG_ENGLISH)
    return sorted(set(tags), key=int)


def _source_link(task):
    """转载来源声明（四站同一份文案，收敛在 `descr.build_source_link`）。"""
    return descr.build_source_link(task)


def _logs(task):
    raw = (task or {}).get("logs_json")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, list) and parsed else None


def _descr_body(task, site):
    return descr.build_descr(
        cover_url=task.get("cover_url") or "",
        tracklist=task.get("tracklist") or "",
        log_content=task.get("log_content") or "",
        log_filename=task.get("log_filename") or "",
        logs=_logs(task),
        opencd_descr=task.get("descr") or task.get("opencd_descr") or "",
        site=site,
        source_link=_source_link(task),
    )


def build_fields(task, *, anonymous=True):
    """把一条 task 渲染成星湾 `takeupload.php` 的表单字段。"""
    task = db.merge_mapped(dict(task))
    audit = []

    title = descr.normalize_music_title(task)
    audit.append(f"name（主标题）← {title}")

    small_descr = descr.build_music_small_descr(task)
    audit.append(f"small_descr（副标题）← {small_descr}")

    fields = {
        "type": MUSIC_TYPE,
        "name": title,
        "small_descr": small_descr,
        "descr": _descr_body(task, db.SITE_XINGWAN),
        "medium_sel[4]": map_medium(task),
        "tags[4][]": map_tags(task),
    }
    audit.append(f"medium_sel[4]（媒介）← {fields['medium_sel[4]']}")
    audit.append(f"tags[4][]（标签）← {','.join(fields['tags[4][]'])}")
    audit.append("descr（简介）← descr.build_descr(site='xingwan')")

    if anonymous:
        fields["uplver"] = "yes"
        audit.append("uplver=yes (匿名发布)")

    return fields, audit


def build_edit_fields(task, *, anonymous=False):
    """编辑页只改文本元数据：标题 / 副标题 / 简介，不提交分类与质量下拉。"""
    fields, audit = build_fields(task, anonymous=anonymous)
    for key in ("type", "medium_sel[4]", "tags[4][]", "uplver"):
        fields.pop(key, None)
    return fields, audit


# ============================================================================
# 星湾客户端
# ============================================================================

class XingwanClient:
    """星湾（xingwan.cc）客户端。只读 + POST takeupload/takeedit，不碰 qB、不碰库。"""

    def __init__(self, base_url=None, cookie=None, request_interval=1.5):
        self.base = (base_url or config.XINGWAN_URL).rstrip("/")
        self.cookie = cookie if cookie is not None else config.XINGWAN_COOKIE
        self.interval = request_interval
        self._last_request = 0.0

        self.sess = net.new_session()
        self.sess.headers.update({
            "User-Agent": UA,
            "Cookie": self.cookie,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    def _throttle(self):
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

    def download_torrent(self, torrent_id):
        """下载星湾服务端重写 tracker 后的 .torrent 文件，返回 bytes 或 None。"""
        r = self.get("/download.php", params={"id": torrent_id})
        if r.status_code == 200 and r.content.startswith(b"d"):
            return r.content
        return None

    @staticmethod
    def SEARCH_PARAMS(name):
        """星湾列表页搜索参数（NexusPHP 标准）。"""
        return {
            "search": name,
            "incldead": 0,
            "spstate": 0,
            "inclbookmarked": 0,
            "search_area": 0,
            "search_mode": 0,
        }

    def find_existing_by_name(self, name, *, local_name, base_path="/torrents.php",
                              max_candidates=None, diag=None):
        """在星湾全站搜索，比对 info.name，防止发重。"""
        import musopia
        return musopia.search_and_compare(self, name, local_name=local_name,
                                          base_path=base_path,
                                          max_candidates=max_candidates,
                                          diag=diag)

    def have_cookie(self):
        return bool((self.cookie or "").strip())

    def check_login(self):
        """验证 Cookie 有效性，返回 user_id 或 None。"""
        r = self.get("/torrents.php")
        if r.status_code == 401 or "WWW-Authenticate" in (r.headers or {}):
            raise SiteError(f"星湾拒绝访问 (HTTP {r.status_code})，XINGWAN_COOKIE 已失效或未配置。")
        if r.status_code != 200:
            raise SiteError(f"访问星湾 torrents.php 失败，HTTP {r.status_code}")
        body = r.text or ""
        if "takelogin.php" in body or "login.php?returnto" in body:
            raise SiteError("星湾 Cookie 已失效（页面退回登录页），请更新 XINGWAN_COOKIE。")
        m = re.search(r"userdetails\.php\?id=(\d+)", body)
        return m.group(1) if m else None

    def fetch_upload_form(self):
        """GET /upload.php 读取空白表单，获取 action 与必要字段校验。"""
        r = self.get("/upload.php")
        if r.status_code != 200:
            raise SiteError(f"获取星湾发布页失败，HTTP {r.status_code}")
        body = r.text or ""
        if "takelogin.php" in body:
            raise SiteError("星湾 Cookie 已失效（发布页退回登录页）")

        bs = __import__("bs4").BeautifulSoup(body, "html.parser")
        form = bs.find("form", id="compose") or bs.find("form", attrs={"name": "upload"})
        if form is None:
            for f in bs.find_all("form"):
                if "takeupload" in (f.get("action") or "").lower():
                    form = f
                    break
        if form is None:
            raise SiteError("星湾发布页中未找到发布表单（可能是账号无发种权限或页面结构变更）。")

        actions = {i.get("name"): i for i in form.find_all(["input", "select", "textarea"])
                   if i.get("name")}
        csrf = {k: v for k, v in actions.items()
                if k.lower() in ("csrf_token", "csrf", "_token")}
        return {
            "form": form,
            "actions": actions,
            "action": form.get("action") or "/takeupload.php",
            "csrf": csrf,
            "enctype": (form.get("enctype") or "").lower(),
        }

    def upload_torrent(self, torrent_path, fields, form_info=None):
        """向星湾提交发布新种子，返回 (torrent_id 或 None, 消息文本)。"""
        import torf

        if not os.path.exists(torrent_path):
            raise SiteError(f"种子文件不存在: {torrent_path}")
        try:
            with open(torrent_path, "rb") as fh:
                raw = fh.read()
            torf.Torrent.read_stream(__import__("io").BytesIO(raw))
        except Exception as e:
            raise SiteError(f"种子文件损坏，非有效 .torrent: {torrent_path}: {e}") from e

        field_name = "file"
        if form_info:
            cand = [k for k in (form_info.get("actions") or {}) if k in ("file", "torrent")]
            if cand:
                field_name = cand[0]

        files = {
            field_name: (
                os.path.basename(torrent_path),
                open(torrent_path, "rb"),
                "application/x-bittorrent",
            )
        }

        data = dict(fields)
        if form_info and form_info.get("csrf"):
            for k, el in form_info["csrf"].items():
                if k not in data and el.get("value"):
                    data[k] = el["value"]

        action = (form_info.get("action") if form_info else None) or "/takeupload.php"
        try:
            r = self.post(action, data=data, files=files, allow_redirects=False)
        finally:
            try:
                files[field_name][1].close()
            except Exception:
                pass

        _dump_takeupload(r.text or "", torrent_path, kind="xingwan-takeupload")

        if r.status_code in (401, 403):
            raise SiteError(f"星湾发布被拒 (HTTP {r.status_code})")
        if r.status_code >= 500:
            raise SiteError(f"星湾服务端故障 (HTTP {r.status_code})")

        if r.status_code in (301, 302, 303):
            loc = r.headers.get("Location") or ""
            if "login.php" in loc.lower():
                raise SiteError("星湾发布后被重定向至登录页，Cookie 可能已失效")
            m = re.search(r"[?&]id=(\d+)", loc)
            if m:
                tid = m.group(1)
                return tid, f"SUCCESS: 种子发布成功 (id={tid})"
            return None, f"UNKNOWN: 重定向 Location 未解析出种子 ID: {loc}"

        if "takelogin.php" in (r.text or "") or "login.php" in (r.url or ""):
            raise SiteError("星湾发布返回登录页，Cookie 可能已失效")

        err = _extract_error(r.text or "")
        if err:
            lower_err = err.lower()
            if "already exists" in lower_err or "已存在" in err or "重复" in err:
                return None, f"REJECT: 种子已存在: {err}"
            return None, f"REJECT: 站点拒绝: {err}"

        tid = None
        m = re.search(r"[?&]id=(\d+)", r.url or "")
        if m:
            tid = m.group(1)
        if not tid:
            m = re.search(r"details\.php\?id=(\d+)&uploaded=1", r.text or "")
            if m:
                tid = m.group(1)
            else:
                m = re.search(r'href=["\']details\.php\?id=(\d+)["\']', r.text or "")
                if m:
                    tid = m.group(1)

        if tid:
            return tid, f"SUCCESS: 种子发布成功 (id={tid})"
        return None, "UNKNOWN: 提交未报错但未解析出种子 ID，请核查页面"

    def fetch_edit_form(self, torrent_id):
        """GET /edit.php?id=N，把 takeedit.php 表单解析成 (fields, current_snapshot)。"""
        r = self.get("/edit.php", params={"id": torrent_id})
        if r.status_code != 200:
            raise SiteError(f"打开 edit.php?id={torrent_id} 失败，HTTP {r.status_code}")
        body = r.text or ""
        if "takelogin.php" in body:
            raise SiteError("星湾 Cookie 已失效（打开编辑页退回登录页）")

        bs = __import__("bs4").BeautifulSoup(body, "html.parser")
        form = None
        for fm in bs.find_all("form"):
            if "takeedit" in (fm.get("action") or "").lower():
                form = fm
                break
        if form is None:
            raise SiteError(f"星湾 id={torrent_id} 编辑页未找到 takeedit 表单")

        fields = {}
        for el in form.find_all(["input", "select", "textarea"]):
            name = el.get("name")
            if not name:
                continue
            if el.name == "input":
                typ = (el.get("type") or "text").lower()
                if typ in ("submit", "button", "reset", "file", "image"):
                    continue
                val = el.get("value") or ""
                if typ in ("checkbox", "radio"):
                    if not el.has_attr("checked"):
                        continue
                    if name.endswith("[]"):
                        fields.setdefault(name, []).append(val)
                    else:
                        fields[name] = val
                else:
                    fields[name] = val
            elif el.name == "textarea":
                fields[name] = __import__("html").unescape(el.get_text() or "")
            elif el.name == "select":
                selected = [o.get("value") or "" for o in el.find_all("option")
                            if o.has_attr("selected")]
                if not selected:
                    opts = el.find_all("option")
                    selected = [opts[0].get("value") or ""] if opts else [""]
                fields[name] = selected[0]

        fields["id"] = str(torrent_id)
        return fields, dict(fields)

    def edit_torrent(self, torrent_id, overrides, *, dry_run=False, verbose=True,
                     prefetched=None):
        """提交编辑种子页面。overrides 覆盖目标字段，其余沿用站点现值。"""
        if prefetched:
            fields, before = prefetched[0], prefetched[1]
            fields = dict(fields)
        else:
            fields, before = self.fetch_edit_form(torrent_id)

        changes = []
        for k, v in overrides.items():
            old = before.get(k)
            if isinstance(v, (list, tuple)):
                v = [str(x) for x in v]
            else:
                v = "" if v is None else str(v)

            if isinstance(old, list) and isinstance(v, list):
                same = sorted(old) == sorted(v)
            else:
                same = (old == v)
            if not same:
                changes.append((k, old, v))
            fields[k] = v

        if verbose or dry_run:
            if not changes:
                print(f"    ℹ️  id={torrent_id} 无字段变化")
            for k, old, new in changes:
                if k == "descr":
                    short_old = (old[:60] + "...") if len(old) > 60 else old
                    short_new = (new[:60] + "...") if len(new) > 60 else new
                    print(f"    · descr:\n        [原值]: {short_old!r}\n        [新值]: {short_new!r}")
                else:
                    print(f"    · {k}: {old!r} -> {new!r}")

        if dry_run:
            return True, f"dry-run: {len(changes)} 个字段将被修改"

        if not changes:
            return True, "无需修改"

        parts = []
        for k, v in fields.items():
            if isinstance(v, list):
                for item in v:
                    parts.append((k, (None, item)))
            else:
                parts.append((k, (None, str(v))))

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
                return True, f"SUCCESS: 编辑成功 (HTTP {r.status_code})"
            if "login.php" in loc:
                raise SiteError("Cookie 失效（被重定向到登录页）")

        if r.status_code == 200:
            if "takelogin.php" in (r.text or ""):
                raise SiteError("Cookie 失效")
            err = _extract_error(r.text or "")
            if err:
                return False, f"REJECT: 站点拒绝: {err}"
            refresh = r.headers.get("refresh", "") or r.headers.get("Refresh", "")
            if "details.php" in refresh or f"id={torrent_id}" in refresh:
                return True, "SUCCESS: 编辑成功"
            if "details.php" in (r.url or "") or f"id={torrent_id}" in (r.text or ""):
                return True, "SUCCESS: 编辑成功"

        return False, f"UNKNOWN: 编辑返回 HTTP {r.status_code}"

    def fetch_details(self, torrent_id):
        """读取详情页，用于确认编辑结果真的落地了。"""
        r = self.get("/details.php", params={"id": torrent_id, "hit": 1})
        if r.status_code != 200:
            raise SiteError(f"打开 details.php?id={torrent_id} 失败，HTTP {r.status_code}")

        bs = __import__("bs4").BeautifulSoup(r.text or "", "html.parser")
        h1 = bs.find("h1")
        title = h1.get_text(strip=True) if h1 else ""
        imgs = [im.get("src") for im in bs.find_all("img")
                if (im.get("src") or "").startswith("http")]
        # 封面 = 非站内域名的外链图（本项目的封面都来自 PicGo）
        has_cover = any(
            "picgo" in (s or "").lower()
            or re.search(r"\.(?:jpg|jpeg|png|webp|gif)(?:\?|$)", s or "", re.I)
            for s in imgs
            if "xingwan.cc" not in (s or "")
        )
        return {
            "title": title,
            "has_cover": has_cover,
            "image_count": len(imgs),
            "raw_len": len(r.text or ""),
        }


# ============================================================================
# 离线自检
# ============================================================================

def selftest(verbose=True):
    """星湾适配层纯本地离线自检（零请求、零写库）。"""
    print("=" * 60)
    print("xingwan_site 离线自检（纯离线，零请求）")
    print("=" * 60)

    ok = [True]

    def chk(name, cond, msg=""):
        if cond:
            print(f"  ✅ {name}")
        else:
            print(f"  ❌ {name} -- {msg}")
            ok[0] = False

    chk("分类 = 408", MUSIC_TYPE == "408")
    chk("CD 介质映射为 19", map_medium({"medium_sel": "CD"}) == MEDIUM_CD)
    chk("OST 映射为 20", map_medium({"formatted_title": "Album OST"}) == MEDIUM_TRACK)
    chk("数字发行映射为 21", map_medium({"medium_sel": "WEB"}) == MEDIUM_OTHER)

    tags = map_tags({"tags": "国语,粤语,英语"})
    chk("无损音乐标签必带 17", TAG_LOSSLESS in tags)
    chk("国语/粤语/英语标签齐全", set(tags) == {TAG_LOSSLESS, TAG_GUOYU, TAG_YUEYU, TAG_ENGLISH})
    chk("禁转/原创标签不被添加", not ({"1", "2", "4"} & set(tags)))

    task = {
        "formatted_title": "周杰伦 - 范特西 2001 - FLAC 16bit 44.1kHz",
        "torrent_name": "周杰伦 - 范特西 2001 - FLAC 整轨 - OpenCD",
        "opencd_tid": "12345",
        "cover_url": "https://img.example.com/a.jpg",
        "medium_sel": "CD",
        "tags": "国语",
        "year": "2001",
        "artist": "周杰伦",
    }
    fields, audit = build_fields(task)
    chk("字段 type 存在", fields.get("type") == MUSIC_TYPE)
    chk("字段 name 存在", bool(fields.get("name")))
    chk("主标题不含分轨/整轨", not any(w in fields["name"] for w in ("分轨", "整轨", "分軌", "整軌")))
    chk("字段 small_descr 存在", bool(fields.get("small_descr")))
    chk("字段 descr 含封面与来源", "[img]" in fields["descr"] and "转自OpenCD" in fields["descr"])
    chk("medium_sel[4] = 19", fields.get("medium_sel[4]") == MEDIUM_CD)
    chk("tags[4][] 含 17", TAG_LOSSLESS in fields.get("tags[4][]", []))
    chk("uplver=yes", fields.get("uplver") == "yes")
    chk("audit 非空", bool(audit))

    edit_fields, _ = build_edit_fields(task)
    chk("编辑字段不含 type", "type" not in edit_fields)
    chk("编辑字段保留 name", "name" in edit_fields)

    print("=" * 60)
    print("✅ 全部通过" if ok[0] else "❌ 有失败项")
    return ok[0]


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
