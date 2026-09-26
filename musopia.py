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
import sys
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

    def iter_my_uploads(self, user_id=None, max_pages=30, debug=False):
        """
        逐页产出 (torrent_id, 标题)。
        接口：getusertorrentlistajax.php?userid=&type=uploaded&page=N（0 基，100 条/页）

        ★★ 关于「只捞出 2 条」这件事（2026-09-22 实测定性）：
          库里 `site_torrent_id` 非空的有 **264** 条，而本函数只产出 **2** 条。
          **本函数本身没有翻页 bug** —— 本地用假响应喂过三种模型验证：
            · 正常翻页（每一页内容不同）→ pages=[0,1,2,3]，产满 264 条 ✅
            · 站点忽略 `page`（每页都返同一批）→ 因 `seen` 去重后 `found==0`，
              在第 2 页**正确地**停下（继续翻也只会重复）
            · 站点用 1 基分页（我们发 0 基）→ 第 2 页与第 1 页完全重叠，同样正确停下
          ⇒ 所以「2 条」的真因在**站点那一侧**，候选有二，且**已排除其一**：
            ① ★ **`getusertorrentlistajax.php` 只列「上传者」而我们的号是「发布者」**
               —— 本函数的 URL 里写的是 `type=uploaded`（NexusPHP 的 `uploaded` 语义
               就是 uploader）。**2026-09-22 实测：换成 `type=seeded` 返回 0 条**，
               故「类型写错了」这条**不成立**（`seeded` 有同族的"拉不到"风险，
               不能据此下结论，但至少没有比 `uploaded` 更好）。
            ② 我们对这个 AJAX 端点的**调用姿势**不对：缺/多了参数、或者
               它要求的 `Referer`/`X-Requested-With` 与站点校验不符，
               于是站点**回一个格式正常的空壳页面**（HTTP 200 + 0 字节或空列表）
               —— 这和 `ERR-SITE-01` 那个「HTTP 200 + 空 body」是同一个形状。
          ★★ 定性这件事需要**一次真站点观测**，本机做不到（没有 Cookie、也不该拿
             生产凭据去打站点）：把端点原样回什么、`fetch_my_uploads()` 之外的
             另一个已知能用的列表页（`userdetails.php?id=N` 的 HTML 表格）
             回什么，两边并排看一次就能定。在那之前**不许**用它的结果下任何结论
             （`tools/uploads_probe.py` 是干这个的诊断件，它自己也依赖本函数
             ⇒ 它现在报的「共 2 条」**同样不能当结论**）。

        ★ 返回值语义没变；`debug=True` 时把每一页的「HTTP 码 / body 字节数 /
          解析出几条 / 其中新几条」打到 stderr —— 正是上面②需要的观测点，
          而且**不改变任何行为**。
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
            parsed = _parse_torrent_links(r.text)
            for tid, title in parsed:
                if tid in seen:
                    continue
                seen.add(tid)
                found += 1
                yield tid, title

            if debug:
                print(f"[iter_my_uploads] page={page} HTTP={r.status_code} "
                      f"body={len(r.text or '')} 字节 解析={len(parsed)} 条 新增={found} 条 "
                      f"累计={len(seen)}", file=sys.stderr)

            # ★ `found == 0` 是**正确的**停止条件，不是 bug：它同时覆盖
            #   「这页真的空了」与「这页全是前面见过的」（站点忽略 page / 分页错位）
            #   —— 两种情况下继续翻都只会重复劳动。
            if found == 0:
                break

    def fetch_my_uploads(self, user_id=None):
        return list(self.iter_my_uploads(user_id=user_id))

    # ---------- 编辑 ----------

    def _explain_no_edit_form(self, torrent_id):
        """`edit.php` 打不开 takeedit 表单时，**分辨是哪种原因**再报错。

        ═══════════════════════════════════════════════════════════════════════
        要解决的具体问题（2026-09-26）
        ═══════════════════════════════════════════════════════════════════════
        原先那句报错是：

            「种子可能已被删除，或当前账号没有编辑权限」

        ★ 它把**两种修法完全不同**的原因写成了一个词，而且**读的人无法分辨**。
          实测代价：2026-09-26 我按"没有编辑权限"那一支追了两轮
          （让用户确认 uid、建议换管理员账号、去查站点权限模型），
          **全是白工** —— 真因是**种子已被删除**（277/280 条如此）。
          而那个结论 `tools/reset_deleted_site_status.py` 里**早就写着**
          （2026-09-23 实测定性，用户已确认种子都被删了）。
          ⇒ 这正是 `ERR-META-01`：**判据本身失效，而失效的输出长得像结论。**

        ═══════════════════════════════════════════════════════════════════════
        怎么分辨（判据是**详情页还在不在**）
        ═══════════════════════════════════════════════════════════════════════
        两者在 `edit.php` 上的表现都一样（都没有 takeedit 表单），
        但在 `details.php` 上**不同**：

            种子已删除   ⇒ details.php 打不开 / 无 h1 标题 / 回错误页
            种子还在     ⇒ details.php 正常，能读到标题

        所以：**编辑页失败 + 详情页正常 = 无权限**；**详情页也失败 = 已删除**。

        ═══════════════════════════════════════════════════════════════════════
        ★ 为什么只在**出错时**才探（零成本设计）
        ═══════════════════════════════════════════════════════════════════════
        本函数**只在 `fetch_edit_form` 真失败时才被调用** —— 正常路径
        （表单读到了）**一次额外请求都不发**。而"到底为什么失败"这件事
        **只有失败时才有用** ⇒ 探详情页的成本只落在**已经失败**的那些行上。
        （若放在正常路径上预探，那才是白花配额。）

        ★ 探测本身失败（网络抖动等）⇒ **如实说"分辨不了"**，**不猜**是哪种。
          猜错方向正是本函数存在的原因（`ERR-META-14`：缺失要如实表达，
          不许替换成一个"看着正常"的默认值）。
        """
        # ★ 复用 `fetch_details()`（走 /details.php + _throttle 限速），
        #   不另写一套请求 —— 两处实现必然漂（`ERR-META-03`）。
        try:
            d = self.fetch_details(torrent_id)
            title = (d.get("title") or "").strip()
        except Exception as e:                              # noqa: BLE001
            # ★ 探测**本身**失败 ⇒ 分辨不了 ⇒ 如实说，别猜。
            return (f"id={torrent_id} 的页面里找不到 takeedit 表单；"
                    f"★ 也无法分辨原因（探 details.php 时出错："
                    f"{type(e).__name__}: {e}）"
                    f"⇒ 请手工打开 details.php?id={torrent_id} 确认种子是否还在")
        if title:
            # 详情页正常 ⇒ 种子在 ⇒ 那问题只能是权限
            return (f"id={torrent_id} 的页面里找不到 takeedit 表单，"
                    f"但 **details.php 正常（标题={title[:40]!r}）⇒ 种子还在** "
                    f"⇒ ★ 这是**没有编辑权限**（不是被删除）")
        # 详情页打得开但读不到标题 ⇒ 多半是错误页/已被删
        return (f"id={torrent_id} 的页面里找不到 takeedit 表单，"
                f"且 **details.php 读不到标题（种子多半已被删除）** "
                f"⇒ ★ 这是**种子已不在站点上**（不是权限问题）")

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
            raise SiteError(self._explain_no_edit_form(torrent_id))

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
            # ★★ 2026-09-23 修：多值键（`xxx[]`）必须做**顺序无关**比较。
            #
            #   ★ 症状（实测）：`tags[4][]` 每次都被判成"有变化"，于是**每轮都
            #     真提交一次**、白花 2~3 个请求，而站点上的内容其实一字未改。
            #     实测输出：`['5','31','24','23'] -> ['5','24','23','31']` —— 集合相同。
            #
            #   ★ 根因（`fetch_edit_form` line 203-204）：站点读回来的多值键是
            #     `fields.setdefault(name, []).append(val)`，顺序 = **页面上 checkbox
            #     的 DOM 顺序**；而我们算出来的顺序 = `mapped_json` 里的顺序（再 append
            #     `OpenCD`）。两边**天然不同** ⇒ `old != v` 恒真。
            #
            #   ★ 为什么是"比较时排序"而不是"构造时排序"：
            #     `tags[4][]` 是 **checkbox 多值**，站点按**集合**语义处理 ⇒ 顺序**无意义**。
            #     但 `fields[k] = v` 仍要写**原顺序**（保持与 `mapped_json` 可对照，
            #     且 `descr` 的注释专门讲过不要 join 成一个值）。⇒ 只改**判等**，不改**载荷**。
            #
            #   ★ 判据（阴性对照）：改前，对一条**站点内容已与本地一致**的条目
            #     跑 `--apply`，会打印 `· tags[4][]: [...] -> [...]` 并 `edited=1`；
            #     改后同一操作应打印 `ℹ️ 无字段变化` 且返回「无需修改」。
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
