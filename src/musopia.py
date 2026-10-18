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
import net   # ★ 统一网络出口（强制 IPv4）——新 Session 一律走 net.new_session()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 这些是编辑器的排版按钮和文件上传控件，不属于要回填的表单数据
SKIP_NAMES = {"b", "i", "u", "url", "IMG", "list", "quote", "tagcount", "nfo"}

# ★★ `edit_torrent` **改不了**页面挂的 `.torrent` 文件（#33 / `ERR-PUBLISH-12`）。
#   NexusPHP 的 `takeedit.php` 是**纯元数据表单**（`fetch_edit_form` 显式跳过
#   `type=file`，见本文件 `SKIP` 那个 `continue`；提交也只发文本分片）。
#   ⇒ 要换挂的种子**只能重发**（新页/新 tid）。把这条事实做成**常量 + 显式拒绝**，
#     免得有人（或未来的我）以为往 overrides 里塞个 `file` 就能换种。
#   ★ 这些键名一旦出现在 overrides 里就**拒绝整个编辑**（不是静默忽略）：
#     静默忽略会让"改种子"看起来成功了，那正是最坏的一种"假成功"。
EDIT_CANNOT_CHANGE_TORRENT = True
EDIT_FILE_LIKE_KEYS = frozenset({
    "file", "torrent", "torrent_file", "infohash", "info_hash", "filehash",
})

# 失败响应落盘目录。★★ 与 `crawl.RAW_DIR` **同一套口径**（env 可覆盖、懒创建、原子写）——
#   ★ 为什么在 `musopia.py` 里**照抄**一份、而不是 `import crawl`：
#     `crawl` 有重依赖（RateGate / opencd / db），而本站点客户端**不该反向依赖**抓取层
#     （`cover_fetch.py` 头注释立的同一条边界规矩）。三行常量 + 一个写函数，不值得为它
#     引入反向耦合（那会让 `import musopia` 顺带拖进整套抓取栈）。
#   ★ 用 `OPENCD_RAW_DIR` 这个**同名** env ⇒ 与 crawl 落进**同一个目录**（人查一处即可）。
RAW_DIR = os.getenv("OPENCD_RAW_DIR", os.path.join(config.DATA_DIR, "raw_html"))


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

        self.sess = net.new_session()
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
        # ★★ 护栏（#33）：`takeedit.php` **改不了挂在页上的 `.torrent` 文件**——
        #   `fetch_edit_form()` 显式跳过 `type=file`（本文件 :264），本函数也只发
        #   文本分片、**不带任何文件部件**。⇒ 若有人把 `file`/`torrent` 之类键塞进
        #   `overrides`，它**不会**生效，却会静默"提交成功"、让人误以为改了种子
        #   ⇒ 必须**显式拒绝**（`ERR-PUBLISH-12`：改不了的事别让它"看起来改了"）。
        #   ★ 要换挂的种子，唯一正确路径是**重发**（新页/新 tid）——见 #27/#34。
        #   ★★ 放在**取表单之前**：拒绝是本地判据，不该先敲一次站点才发现（快、且省配额）。
        _bad = [k for k in overrides
                if k in EDIT_FILE_LIKE_KEYS or k.lower() in ("file", "torrent")]
        if _bad:
            return False, (f"编辑改不了 .torrent（本站 takeedit.php 是纯元数据表单）："
                           f"被拒的键 {_bad} ⇒ 需**重发**（新页）")

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
        """发布新种子，返回 `(torrent_id 或 None, 消息)`。

        ★ 失败时消息带**机器可读前缀**（`publish.py` 据此分层处置，见 #21）：
          · `"SITE_DOWN: ..."` = 站点侧问题（HTTP≠200 / 退回登录页）⇒ 调用方应停 + 退避；
          · `"REJECT: ..."`    = 这条内容被拒（或确定性空壳失败）⇒ 调用方应跳过、发下一条。
        成功时为 `(id, "ok")`，**不落盘**（成功响应无诊断价值）。
        """
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

        # ★★ 失败支一律**先落盘再判定**（#19）——否则每次排查都要"人肉 monkey-patch 重跑"
        #   才能拿到站点的 HTML 快照，而重跑 = 真发种（**不可逆**）。
        #   对比：`crawl.save_raw()` 早就在做同类事（详情页落 `raw_html/`）——同类机制
        #   不该两套待遇。落盘失败**绝不**把发布搞崩（见 `_dump_takeupload`）。
        _dump_takeupload(r.text, torrent_path)

        # ★★ 判据分层（#21 的**唯一来源**）——把"这条坏"与"站点崩"分开，两者处置相反：
        #   · 站点崩（HTTP≠200 / 退回登录页）⇒ 调用方应**立即停 + 退避**，绝不连打整条队列；
        #   · 这条坏（站点明确拒了内容，或确定性空壳失败）⇒ 调用方应**跳过它、发下一条**。
        #   ★ 用**机器可读前缀**承载判据（不改两元组签名 ⇒ 蟹黄堡路径零回归）：
        #     老消费方看到的仍是"一句话"，新消费方按前缀分流。
        body = r.text or ""
        if r.status_code != 200:
            return None, f"SITE_DOWN: HTTP {r.status_code}"
        if "takelogin.php" in body or "login.php?returnto" in body:
            # 与 `check_login()` / `fetch_edit_form()` 同一判据：页面退回登录页 = Cookie 失效
            return None, "SITE_DOWN: 页面退回登录页（Cookie 已失效，请更新 .env）"

        err = _extract_error(body)
        if err:
            return None, f"REJECT: {err}"
        return None, (f"REJECT: 未拿到重定向 ID，HTTP {r.status_code}"
                      f"（空壳失败页，响应已落盘：{RAW_DIR}/）")

    def download_torrent(self, torrent_id):
        r = self.get("/download.php", params={"id": torrent_id})
        if r.status_code == 200 and r.content.startswith(b"d"):
            return r.content
        return None

    # ---------- 发布前跨站查重（#13 / #11 核心）----------

    def find_existing_by_name(self, name, *, local_name, base_path="/torrents.php",
                              max_candidates=None, diag=None):
        """在**本站全站范围**搜 `name`，返回**已存在同版本**的 tid；没有则返回 `None`。

        ═══════════════════════════════════════════════════════════════════════
        为什么存在（#13 / #11）
        ═══════════════════════════════════════════════════════════════════════
        pipeline **只信本地 `site_status`**、从不问目标站点「有没有」⇒ 同一专辑以
        不同 tid/路径再入库时就去发重复种 ⇒ 站点用**不透明的**「上传失败！」拒绝
        （正文零消息，见 `_extract_error`）⇒ 每次排查都要人肉反推。本函数把
        「站点上到底有没有这条」变成**发布前的一次确定性查询**。

        ═══════════════════════════════════════════════════════════════════════
        ★★ 判据 = 站侧 `.torrent` 的 `info.name` **精确相等**（不是标题！）
        ═══════════════════════════════════════════════════════════════════════
        2026-10-11 实测（3/3 条已知重复都成立）：**本地 `.torrent` 的 `info.name`
        == 站侧 `.torrent` 的 `info.name`**（`site_verify.normalize_name` 后精确相等）；
        而**标题不同**（本地 `formatted_title` = `Forever Now 2026 - FLAC 整轨 - OpenCD`，
        站侧标题 = `Switchfoot - Forever Now 2026 - FLAC 分軌 - Orpheus`）。
        ⇒ **绝不能比标题**：标题带发布者/编曲/分轨口径等一切可变前缀，
          比标题要么假阳（搜错）要么假阴（漏查）。而 `info.name` 由**种子内容**决定、
          与发布者无关 ⇒ 才是"这条是不是同一专辑"的权威身份。

        ★★ 为什么**不能**用 `iter_my_uploads`（本类上方那个）：
          ① 它的 docstring 自陈"只捞出 2 条、不许拿它的结果下结论"；
          ② **重复页的发布者不是我们**（实测 5272/5075/5998 的 uploader 都是别人）
             ⇒ 我们的上传列表里**根本不会有**这些页。
        ⇒ 必须**站点范围搜**（`/torrents.php?search=…`），搜的是全站、不限发布者。

        ═══════════════════════════════════════════════════════════════════════
        ★ 任何网络/解析异常**向上抛**（调用方按"停轮非 0"处理，绝不吞）
        ═══════════════════════════════════════════════════════════════════════
        用户拍板（2026-10-11）：**查重搜索本身失败 ⇒ 停轮非 0**（触发 daemon 退避）。
        ⇒ 本函数**不**吞异常、**不**在出错时返回 `None`（那会把"搜不了"静默降级成
        "没重复"⇒ 照发 ⇒ 又一条重复）。`None` 的语义**只有一个**：**搜到了、但确认不重复**。
        这与 `crabpt_site` 的 `UNKNOWN` 停轮、`publish` 的 `SITE_DOWN` 同族。

        :param name: 提交给站点搜索框的字符串（一般用本地 `formatted_title`）。
        :param local_name: **本地 `.torrent` 的 `info.name`**（调用方用
            `site_verify.extract_info_name(读 task["torrent_file"])` 取出）。
            与之精确相等的站侧条目即判"重复"。★ 为空 ⇒ 无从比，直接返回 `None`
            （`compare_hosted_vs_db` 会给 `NO_DB_NAME`，不是"重复"）。
        :param base_path: 列表页路径（两站同名；蟹黄堡复用本方法时传它自己的）。
        :param max_candidates: 最多验几个候选（每个候选 = 1 次 details + 1 次下载，
            **真实配额**）。防止名字太泛时把站点打疼。★ `None` ⇒ 读
            `config.DEDUP_MAX_CANDIDATES`（默认 16）；area=1（#31）下**必须**够大，
            否则真目标页在验证前被截断 ⇒ 反而漏召回。
        :return: 命中的 tid（`str`）或 `None`。
        """
        return search_and_compare(self, name, local_name=local_name,
                                  base_path=base_path, max_candidates=max_candidates,
                                  diag=diag)

    @staticmethod
    def SEARCH_PARAMS(name):
        """列表页搜索的固定参数（**与 `tools/fetch_page.py` 的乌托邦口径一致**）。

        ★ 把参数集中成一处（`ERR-META-03`）：两站复用同一套，改一处即改全部；
          也方便自检断言"参数没被写歪"。
        """
        return SEARCH_PARAMS(name)


def SEARCH_PARAMS(name):
    """（模块级，供两站客户端复用）列表页搜索的固定参数。

    ★ `tools/fetch_page.py` 的乌托邦搜索口径 —— 但**查重路已订正 `search_area`**（见下）。
    ★ 集中一处（`ERR-META-03`）。
    ★★ `search_area`（#27 实测订正，2026-10-11）：**0 → 1**。
       站点 `search_area` 是"搜哪个字段"的选择器，实测各值召回面：
         · `0`（原值）：`Etre`→21、`Forever Now`→4，但**中文艺术家名 `徐元白`→0**（系统性漏中文）
         · `1`：`Etre`→109、`Forever Now`→104、**`徐元白`→2**（**最宽、唯一能召回中文**）
         · `2`：更窄（`Forever Now`→2）
         · `3`：全 0（另一种模式，不用）
       ⇒ `0` 会**系统性漏掉中文艺术家**的行 ⇒ 假阴性 ⇒ 漏判重复 ⇒ 又发重复种。
         **必须用 `1`**（召回最全、唯一能召回中文）。
       ★★★ 但 area=1 **不是无代价的**（`ERR-PUBLISH-17`，「召回面变宽 ≠ 召回变好」）：
         宽召回让单条查询候选从 ~4–21 涨到 109–120 ⇒ 真目标会被 `max_candidates` **截断**
         ⇒ **反而漏召回**（20 行回归 13/20 → 11/20）。**判据精确**只挡住**假阳性**，
         **挡不住**这种"候选被噪声挤出预算"的**假阴性**。
         ⇒ area=1 **必须**配 `SEARCH_QUERY_PLAN` 的**精准优先排序** + `search_and_compare`
           的**命中即停**（高精度短串在前、命中就返回，宽查询不发）—— 否则净亏损。
    ★ `search_mode=0` 保持（AND 逐 token 语义；多子查询计划正为其设计）。
    """
    return {"search": name, "incldead": 0, "spstate": 0,
            "inclbookmarked": 0, "search_area": 1, "search_mode": 0}


# ==================== 查重「召回」：分词 + 查询计划（#23）====================
#
# ★★ 为什么需要这一层（#23 / 实测，2026-10-11）：
#   站点搜索是 **AND（逐 token）+ 对「显示标题」字段匹配**，且**标点会打断** ——
#   实测：`"Chamber Works 2024"→命中` 但 `"Chamber Works (2024)"→0 候选`；
#   整条带 `;`/`–`/`[FLAC]` 的本地标题 → **0 候选**。⇒ **只发一条串会漏召回**
#   （假阴性）⇒ 站点上明明有、却当"没有"⇒ 照旧提交 ⇒ 又一条重复种。
#   ⇒ 改为**发多条子查询**（覆盖头/尾/内部窗口 + **高精度单 token**），逐条做**精确**
#     `info.name` 比对；顺序**精准优先**、**命中即停**（#31）。
# ★ 召回**宽**只多花请求、**不会**制造假阳性：负判据（"是不是同一版本"）由
#   `site_verify.compare_hosted_vs_db` 的**精确相等**结构性挡住 —— 与召回宽度无关。
# ★★ 但"宽"会经**预算截断**制造**假阴性**（`ERR-PUBLISH-17`）⇒ 所以**顺序承重**：
#   高精度短串必须排前（见 `SEARCH_QUERY_PLAN`）；只去重不排序 = 把 13/20→11/20 的亏重新引入。
# ★ token 源 = **本地 `info.name`**（不是 `formatted_title`，也不是站点显示标题）：
#   它是权威身份、与发布者无关，且**不含** `- FLAC 整轨 - OpenCD` 这种站点标题才有的尾巴。
# ★ 分词器**必含假名**（`3040-30ff`）：否则 `とんがり帽子のアトリエ` 会被切碎（实测）。

# CJK 字符类：平假名+片假名 · CJK 扩展A · CJK 基本 · CJK 兼容（覆盖实测到的日文/中文标题）
_CJK_CLASS = "぀-ヿ㐀-䶿一-鿿豈-﫿"
# ★ 显式字符类，**不用** `\w`（`\w` 语义随构建漂移，本项目不容这种不确定）。
_SEARCH_SPLIT_RE = re.compile(r"[^0-9A-Za-z" + _CJK_CLASS + r"]+")
_CJK_ONE_RE = re.compile("[" + _CJK_CLASS + "]")


# ==================== 查重「精准优先」的纯判据（#31，2026-10-11）====================
#
# ★★ 为什么需要这一层（`ERR-PUBLISH-17`：「召回面变宽 ≠ 召回变好」）：
#   实测 `search_area=1` 是**唯一**能召回中文艺术家名的口径（`徐元白` area=0 → 0），
#   但它让单条查询的候选从 ~4–21 涨到 109–120（噪声填满）⇒ 真目标被 `max_candidates`
#   截断 ⇒ **反而更差**（20 行回归 13/20 → 11/20）。加预算治不了（噪声线性增长）。
#   ⇒ 正解 = **精准优先**：高精度短串排最前、命中即停，宽扇出查询放最后。
#   下面三个纯函数就是"哪个 token 精度高"的判据（全部零网络、可单测、不依赖 config）。

def _is_numeric_tok(tok):
    """token 是不是"纯数字/带分隔的数字"（`2001`/`03`/`250813`/`2024.08`）。

    ★ 从原 `SEARCH_QUERY_PLAN` 内部的闭包 `_numeric` 提到模块级 —— 一处定义、多处共用
      （`ERR-META-03`：口径不许在多处各写一份）。
    """
    return bool(tok) and all(ch.isdigit() or ch in ".-/" for ch in tok)


def _is_datey_tok(tok):
    """token 是不是**疑似日期/流水号**（`2001`/`250813`/`20240310`）—— 只降权，**不过滤**。

    ★ 依据（实测 20 行）：`[250813] 徐元白 …` 的首 token = `250813`（`[` 包起来的日期标签），
      `[260617] 北村友香 …` 同形；这类 token 在站点上**海量命中**、身份信息≈0。
    ★ 判据 = 纯数字且长度 ∈ {4,6,8}（覆盖 `2001`/`250813`/`20240310`）。
    ★★ 只作**排序信号**、绝不作过滤：误判（如把真·识别号 `2001` 当日期）代价为零 ——
      它同时也在兜底 tier 里，仍会被搜到。
    """
    return _is_numeric_tok(tok) and len(tok) in (4, 6, 8)


# ★ 格式/容器/常见词 —— 几乎**每一个** rip 的标题里都有，组成的查询扇出极大、身份信息≈0
#   （实测：`FLAC PQCD 10004`、`CUE LOG PNG` 这类查询在 area=1 下命中上百条）。
# ★★ **降权名单，不是过滤名单**：漏收一个词（如 `MQA`/`SACD`）只会让它排得靠前一点、
#   召回不受影响（判据仍是精确 `info.name` 比对）。大小写不敏感匹配（`SEARCH_TOKENS` 保留原大小写）。
_FORMAT_TOKENS = frozenset({
    "flac", "ape", "wav", "wavpack", "wv", "alac", "aac", "mp3", "m4a", "dsd", "dsf", "dff",
    "hi-res", "hires", "24bit", "16bit", "96khz", "44khz", "lossy", "lossless",
    "cue", "log", "png", "jpg", "jpeg", "scan", "scans",
    "ep", "cd", "cd1", "cd2", "cd3", "cd4", "disc", "disc1", "disc2", "disc3",
    "ost", "mix", "mixed", "mixtape", "int", "retail", "digital", "web", "web-flac",
    "vinyl", "limited", "remaster", "remastered", "deluxe", "album", "single", "ost.",
})


def _is_format_tok(tok):
    """token 是不是"全场通用"的格式/容器词（`FLAC`/`CUE`/`LOG`/`CD`…）—— 只降权，不过滤。"""
    return bool(tok) and tok.lower() in _FORMAT_TOKENS


def _is_high_precision_tok(tok):
    """单 token 是否**高精度**（值得**单独**作为第一条查询）。

    ★ 判据（实测：艺术家名恒是名里最"站点可辨"的串）：
      · CJK 多字 token（含假名/汉字，`len>=2`）⇒ 是 —— 这正是 area=0 的盲区、且实测候选极少
        （`徐元白` 单独查 → 2 条候选，远在预算内）。
      · 非数字、非格式的**纯 ASCII 且 `len>=4`** token（`Etre`/`Stravinsky`/`Hannigan`）⇒ 是
        （`len>=4` 挡掉 `of`/`and`/`the` 这类虚词）。
    ★ 数字/日期（`2001`/`250813`）与格式词（`FLAC`/`cd`）⇒ **不是**（它们是扇出噪声）。
    """
    if not tok:
        return False
    if _is_numeric_tok(tok) or _is_format_tok(tok):
        return False
    if _CJK_ONE_RE.search(tok):
        return len(tok) >= 2
    return len(tok) >= 4


# ★★ T0（高精度单 token）条数上限 —— 防 15-token 长名把扇出打爆（如
#   `Stravinsky; Royal Academy … Hannigan …` 会有 ~11 个候选单 token）。
#   ★ 取前 N 个（保序）即可：艺术家名通常在名前部；多出的单 token 价值低、成本高。
_MAX_SINGLE_TOKEN_QUERIES = 4


def SEARCH_TOKENS(name):
    """把名字切成搜索 token（纯函数、零网络）。保序去重。

    ★ 切分点 = 一切非 `[0-9A-Za-z]`、非 CJK 的字符（含空格、`-`、`–`、`;`、`(`、`[`…）。
    ★ 保留条件：长度 ≥ 2 **或** 含 CJK 单字（CJK 单字有信息量，不能因短而丢）。
    ★ 例：`"Stravinsky; … Hannigan - Chamber Works (2024) [FLAC]"`
          → `['Stravinsky', 'Royal', 'Academy', 'of', 'Music', 'and', 'Juilliard',
              'School', 'Ensemble', 'Barbara', 'Hannigan', 'Chamber', 'Works', '2024', 'FLAC']`。
    """
    if not name:
        return []
    s = htmllib.unescape(str(name))
    import unicodedata
    s = unicodedata.normalize("NFC", s)
    out = []
    for tok in _SEARCH_SPLIT_RE.split(s):
        if not tok:
            continue
        if tok in out:
            continue
        if len(tok) >= 2 or _CJK_ONE_RE.search(tok):
            out.append(tok)
    return out


def SEARCH_QUERY_PLAN(local_name, *, search_name="", max_queries=5):
    """由本地 `info.name` 生成**精准优先**的有序搜索串列表（纯函数、零网络）。

    ═══════════════════════════════════════════════════════════════════════════
    ★★★ 顺序是**承重**的（#31 / `ERR-PUBLISH-17`：「召回面变宽 ≠ 召回变好」）
    ═══════════════════════════════════════════════════════════════════════════
    实测：`search_area=1` 是**唯一**能召回中文艺术家名的口径，但它让单条查询候选从 ~4–21
    涨到 109–120（噪声填满）⇒ 真目标被 `max_candidates` **截断** ⇒ 20 行回归 **13/20 → 11/20**
    （**更差**）。加预算治不了（噪声线性增长）。
    ⇒ 正解 = **精准优先**：**高精度短串排最前**、命中即停（见 `search_and_compare`），
      宽扇出查询放最后。**只去重但不排序**的 plan 会把这个亏重新引入。

    ★ 依据（实测 20 行，`pos ∈ {0,3}`、`n_over_8=0`）：命中时真目标很靠前；病在"宽查询先跑"。

    ═══════════════════════════════════════════════════════════════════════════
    ★ tier（前=**窄**，后=宽）—— ★★ 顺序依据见正文注释（真站实测：**单 token 是宽的**）
    ═══════════════════════════════════════════════════════════════════════════
      · **T0 头 3**（艺术家+专辑连在一起 ⇒ 最窄：实测 `群星 爱是我们共同的声音`→2 条）
      · **T0b 去前导日期后的头 3**（CJK 行 `[250813] 徐元白 …`）
      · **T0c 头 2**（仍"艺术家+专辑"）
      · **T1 非数字非格式的相邻 2 词对**（`One Day`）—— #27 已证有效
      · **T2 尾 3** —— 兜尾部窗口
      · **T3 其余 2 词对**（数字对/格式对）—— **不丢，只后移**
      · **T4 稀疏内部 3-窗口** —— 保底（`Stravinsky` 靠它）
      · **T5 单 token**（CJK 优先，≤ `_MAX_SINGLE_TOKEN_QUERIES`）—— **放最后**：
        它们**最宽**（实测 `Etre`/`群星` 单独查 **109** 条）⇒ 提前会吃光预算、把真目标挤出。
      · **T6 `search_name` 兜底**（老的单串，向后兼容）。
    ★ 前导**日期/流水号**（`250813`/`2001`）与**格式词**（`FLAC`/`CUE`/`LOG`/`CD`）一律降权
      ——它们是"全场通用"的扇出噪声（`_is_datey_tok` / `_is_format_tok`）。
    ★ 短名（`n_tokens<=3`）**走同一套 tier**（不再只发整名）。
    ★ `max_queries<=0` ⇒ 只回 `search_name`（= **旧行为**，无需改代码的杀开关）。
    ★ 末尾追加 `search_name` 作兜底（向后兼容：老调用点仍传 `formatted_title`）。
    :return: `list[str]`（按**验证顺序**排列；空列表 ⇒ 无串可搜 ⇒ 调用方返 None）。
    """
    t = SEARCH_TOKENS(local_name)

    def _nonblank(x):
        return bool(x and str(x).strip())

    if max_queries is None or int(max_queries) <= 0:
        return [search_name] if _nonblank(search_name) else []

    wins = []
    if t:
        # ★★★ 顺序依据（2026-10-11 真站点实测；推翻"单 token 精度最高"的初版假设）：
        #   实测每条查询的候选数 —— **单 token（哪怕 CJK）是宽的、多 token 窗口才是窄的**：
        #     `Etre` 109(pos44) / `Etre 2001 03` **18**(pos8) · `齐秦` 28 / `齐秦 我拿什么爱你` **2**
        #     `群星` 109(pos67) / `群星 爱是我们共同的声音` **2**(pos0) · `Shaggy` 90 / `Shaggy Intoxication 2007` **2**
        #   ⇒ **多 token 窗口排最前**（头/尾窗口最窄）；**单 token 排最后**
        #     （它们宽 ⇒ 会以 ~109 条噪声吃光 `max_candidates` 预算 ⇒ 真目标被截断 ⇒ 漏召回）。

        # ---- T0：头 3（最窄：艺术家+专辑连在一起；★ 先**去前导日期/流水号**再取）----
        #   ★ 为什么先剥前导日期：`[250813] 徐元白 - 古琴` 的 t[0] 是日期标签；带它组头 3
        #     会变成 `250813 徐元白 古琴`（宽）—— 剥掉才是 `徐元白 古琴`（窄，实测 2 条）。
        _lead = 0
        while _lead < len(t) and _is_datey_tok(t[_lead]):
            _lead += 1
        t2 = t[_lead:] if _lead < len(t) else t          # 去掉前导日期标签的 token 序列
        wins.append(t2[:3])                              # 头 3（已去日期）
        if len(t2) >= 4:
            wins.append(t2[:2])                          # 头 2（仍"艺术家+专辑"）

        # ---- T2：尾 3（★ **先于**长词对列表：站点题常把词序打乱/夹入自有 token，
        #      如 `Stravinsky … Chamber Works 2024 …` 那条的命中窗在**尾部** `Works 2024 FLAC`；
        #      若把长词对列表放前面，尾窗会被 `max_queries` 截掉 ⇒ 漏召回。）----
        if len(t) > 3:
            wins.append(t[-3:])

        # ---- T1：相邻 2 词对（双非数字、双非格式）—— 保序、不稀疏采样 ----
        pairs = [t[i:i + 2] for i in range(len(t) - 1)]
        word_pairs = [p for p in pairs
                      if not _is_numeric_tok(p[0]) and not _is_numeric_tok(p[1])
                      and not _is_format_tok(p[0]) and not _is_format_tok(p[1])]
        other_pairs = [p for p in pairs if p not in word_pairs]
        wins += word_pairs

        # ---- T3：其余 2 词对（数字对/格式对）—— 不丢，只后移 ----
        wins += other_pairs

        # ---- T4：稀疏内部 3-窗口（保底）----
        if len(t) >= 6:
            mids = [t[i:i + 3] for i in range(1, len(t) - 3)]
            if mids:
                step = max(1, len(mids) // 2)
                wins += mids[::step][:2]

        # ---- T5：**单 token 最后**（它们最宽 —— 实测 `Etre`/`群星` 单独查 109 条）----
        hp = [tok for tok in t if _is_high_precision_tok(tok)]
        hp_cjk = [tok for tok in hp if _CJK_ONE_RE.search(tok)]
        hp_asc = [tok for tok in hp if not _CJK_ONE_RE.search(tok)]
        for tok in (hp_cjk + hp_asc)[:_MAX_SINGLE_TOKEN_QUERIES]:
            wins.append([tok])

    qs = []
    for w in wins:
        s = " ".join(w)
        if _nonblank(s) and s not in qs:
            qs.append(s)
    if _nonblank(search_name) and search_name not in qs:
        qs.append(search_name)                    # T5 兜底：老的单串
    return qs[:max(0, int(max_queries))]


def search_and_compare(client, name, *, local_name, base_path="/torrents.php",
                       max_candidates=None, max_queries=None, diag=None):
    """**两站共用**的查重核心（`ERR-META-03`：一处生效、两站不漂）。

    `client` 需提供：`.get(path, params=…)`、`.download_torrent(tid)`、
    `.SEARCH_PARAMS(name)`（或直接用模块级 `SEARCH_PARAMS`）。
    `MusopiaClient` 与 `crabpt_site.CrabptClient` **都**满足（后者只需 `get` +
    `download_torrent`，见 `crabpt_site.CrabptClient.find_existing_by_name`）。

    ★★ 判据 = **站侧 `.torrent` 的 `info.name` 与 `local_name` 精确相等**（`site_verify`
      归一化后），**不是**标题。理由见 `MusopiaClient.find_existing_by_name` 的 docstring。
    ★★ **召回**（#23/#31）：按 `SEARCH_QUERY_PLAN` 发**多条子查询**，但**逐条查询→立即验证
      →命中即停**（不再"先跑完所有查询取并集再验证"）—— 见下"为什么"。
    ★★ 任何网络/解析异常**向上抛**（用户拍板：查重失败 ⇒ 停轮非 0，绝不吞成"没重复"）。
      ★ 这条对**每一条子查询**都成立：任一条 HTTP≠200 / 退回登录页 ⇒ 立即抛（绝不降级）。
    :param max_candidates: **全局**候选上限（跨全部子查询累计，非每查询），挡住扇出。
      `None` ⇒ 读 `config.DEDUP_MAX_CANDIDATES`（读不到时兜底 16）。
      ★★（#31）预算花在"**已验证**候选"上（跨查询累计）；**精准查询在前** ⇒ 预算先给精目标，
      绝不被宽扇出查询的噪声吃光（旧形状的 13/20→11/20 亏损即此）。
    :param max_queries: 子查询条数上限；`None` ⇒ 读 `config.DEDUP_MAX_SEARCH_QUERIES`
      （读不到时兜底 5）；`<=0` ⇒ 只发 `name` 一条（旧行为）。
    :param diag: ★（`ERR-PUBLISH-25` / `S4`）可选的**诊断累加器**（`dict`）。传入则本函数
      往里记「为啥返回 None」的可解释证据：`queries`（发过的查询）/ `candidates`（见过的候选数）/
      `verified`（已花预算验证的候选数）/ `near_miss`（**不是**同版本、但**名字像**的候选 `[(tid,name)]`）。
      ★ 为什么必须记 `near_miss`：`21811` 那次站点**真的**有一条同 `info.name` 的页，
        本函数却返回 `None` —— 而调用方只看到一句"未发现同版本"，**无法分辨**
        「查得窄了/预算被噪声吃了/站点标题排序把它挤出去了」。没有这份证据时，
        唯一出路就是像我当时那样去猜字段（猜了三轮全错）。
      ★ 不传 ⇒ 行为**逐字不变**（旧调用方零回归）。
    :return: 命中的 tid（`str`）或 `None`（搜到了但不重复 / `local_name` 为空无从比）。
    """
    import site_verify

    # ★（`S4` / `ERR-PUBLISH-25`）诊断累加器：**不传则零改动**。传了就一路记证据，
    #   让"返回 None"从一句无法反驳的话变成一份可核对的账。
    if diag is not None:
        diag.setdefault("queries", [])
        diag.setdefault("candidates", 0)
        diag.setdefault("verified", 0)
        diag.setdefault("near_miss", [])
        # ★★ `same_name` = 站点上 `info.name` 与 local_name **规范化后逐字相同**、
        #   却**没进** MATCH 分支的条目。正常路径下它恒为空（相等就 `return tid` 了）；
        #   能出现只有一种成因：`compare_hosted_vs_db` 返回了 `NO_DB_NAME`
        #   （`normalize_name` 的一侧为空，理论上与"相等"互斥 ⇒ 防御性记录）。
        #   ★ 保留它，是要让 `publish._same_name_precheck` 的判据**显式**：
        #     它读 `same_name`，**不**去把 `near_miss` 当同名用 ——
        #     混用两者 = 把"极像"当"相同" ⇒ 会拒发站点本来会收的条目
        #     （少发是静默的，`ERR-PUBLISH-25` 的教训）。
        diag.setdefault("same_name", [])
        diag["local_name"] = local_name

    if not local_name:
        # 没有本地权威名 ⇒ 无从比 ⇒ 如实返回 None（不是"没重复"，是"比不了"）。
        if diag is not None:
            diag["why"] = "local_name 为空 ⇒ 无从比（不是'没重复'）"
        return None

    if max_queries is None:
        try:
            max_queries = config.DEDUP_MAX_SEARCH_QUERIES
        except Exception:                       # noqa: BLE001 —— 无 config 的自检也能跑
            max_queries = 5

    if max_candidates is None:
        try:
            max_candidates = config.DEDUP_MAX_CANDIDATES
        except Exception:                       # noqa: BLE001 —— 无 config 的自检也能跑
            max_candidates = 16

    plan = SEARCH_QUERY_PLAN(local_name, search_name=name, max_queries=max_queries)
    if not plan:
        # 无串可搜（本地名切完为空且没兜底串）⇒ 不是"没重复"，是"搜不了" ⇒ None。
        if diag is not None:
            diag["why"] = "查询计划为空 ⇒ 搜不了（不是'没重复'）"
        return None

    sp = getattr(client, "SEARCH_PARAMS", None)

    # ★★★ 逐条查询 → **立即验证** → 命中即停（#31 精准优先）。
    #
    #   ★ 为什么不再是"先跑完所有查询取并集、再验证"：那会让**宽扇出查询在前**时，
    #     它的噪声候选先占满 `max_candidates` 预算 ⇒ 精准查询的真目标在**验证前**被截断
    #     ⇒ 漏召回（实测 20 行 13/20 → 11/20，`ERR-PUBLISH-17`）。
    #   ★ 交错后：`plan` 已按精准优先排序（`SEARCH_QUERY_PLAN`），第一条查询若命中就**返回**,
    #     后续宽查询**根本不发** ⇒ 预算与请求都省在精准查询上。
    #   ★ 不变量（逐条保住，见各 raise/注释）：
    #     · 任一条查询 HTTP≠200 / 退回登录页 ⇒ **立即抛**（绝不降级成"没重复"）；
    #     · `max_candidates` 仍是**全局**预算（改为计"已验证候选"，跨查询累计）；
    #     · 同一 tid 跨查询不重复验证（`seen`）；
    #     · 返回契约不变。
    seen = set()                                # 跨查询去重（同一 tid 不重复验）
    verified = 0                                # 已验证候选计数（全局预算，跨查询累计）
    for q in plan:
        params = sp(q) if callable(sp) else SEARCH_PARAMS(q)
        r = client.get(base_path, params=params)
        if diag is not None:
            diag["queries"].append(q)
        if r.status_code != 200:
            # ★ 纪律：**任一**子查询非 200 ⇒ 抛（"搜不了"绝不静默降级成"没重复"）。
            raise SiteError(f"查重搜索失败：{base_path}?search=… HTTP {r.status_code}")
        body = r.text or ""
        # ★ 与 check_login 同一判据：退回登录页 = Cookie 失效 ⇒ 抛（别当"没结果"）
        if "takelogin.php" in body or "login.php?returnto" in body:
            raise LoginExpired("查重搜索退回登录页（Cookie 已失效，请更新 .env）")

        # 本条查询**新引入**的候选（保序）—— 只验它，验完再看下一条查询。
        fresh = []
        for tid, _title in _parse_torrent_links(body):
            if tid in seen:
                continue
            seen.add(tid)
            fresh.append(tid)
        if diag is not None:
            diag["candidates"] = len(seen)

        room = max_candidates - verified
        for tid in fresh[:max(0, room)]:
            # ① 先验详情页真的存在（★ HTTP 200 ≠ 存在：壳页无 `<h1>`，见 site_verify 头注释）
            d = client.get("/details.php", params={"id": tid})
            verified += 1
            if d.status_code != 200:
                continue                   # 单个候选打不开 ⇒ 跳过（不是致命；搜索本身已成功）
            if not site_verify.page_exists(d.text or ""):
                continue                   # 壳页 ⇒ 这条 id 实际不存在
            # ② 取站侧 `.torrent` 的 info.name（★ 权威身份，见 docstring）
            tb = client.download_torrent(tid)
            if not tb:
                continue                   # 拿不到站点副本 ⇒ 无从比 ⇒ 试下一个
            hosted, _why = site_verify.extract_info_name(tb)
            verdict = site_verify.compare_hosted_vs_db(local_name, hosted)
            if verdict == site_verify.MATCH:
                return tid                 # ★ 命中 ⇒ 即停（后续宽查询不再发）
            # MISMATCH/NO_DB_NAME ⇒ 不是这条 ⇒ 继续试下一个候选
            # ★（`S4` / `ERR-PUBLISH-25`）两条**互斥**的旁证，都**只记不判**（见 diag 的 docstring）：
            #   ① `same_name`：规范化后**逐字相同**却没进 MATCH 分支 ——
            #      只有 `NO_DB_NAME`（读不出名字）能造成这种局面。这是真·同名冲突证据。
            #   ② `near_miss`：**极像但不等** —— 说明站点上有近亲条目，
            #      提交被拒时优先怀疑同名/近名冲突，而不是字段。
            if diag is not None:
                if site_verify.normalize_name(hosted) == site_verify.normalize_name(local_name):
                    diag["same_name"].append((tid, hosted))
                elif site_verify.is_near_miss(local_name, hosted):
                    diag["near_miss"].append((tid, hosted))
    if diag is not None:
        diag["verified"] = verified
        if diag["same_name"]:
            diag["why"] = (f"★站点上有 info.name **逐字相同**的条目 "
                           f"{[t for t, _ in diag['same_name']]}（但未进 MATCH 判定，"
                           f"见同名校验）⇒ 提交**必被**「上传失败」拒")
        elif diag["near_miss"]:
            diag["why"] = (f"有 {len(diag['near_miss'])} 条**名字极像但不是同版本**的候选"
                           f"（同 info.name 判定仍为不匹配）⇒ 很像同名冲突")
        elif verified >= max_candidates:
            diag["why"] = (f"预算被吃光（verified={verified} ≥ max_candidates={max_candidates}）"
                           f"⇒ 真目标可能**没被验到**")
        else:
            diag["why"] = ("搜到了、逐条比过都不重复，且没有名字相近的候选"
                           "⇒ 站点上确实没有同版本/同名条目")
    return None                            # 搜到了、但不重复


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


def _dump_takeupload(html, torrent_path=None, kind="takeupload"):
    """
    把 `takeupload.php` 的**失败响应**落盘，供事后离线分析（★ 关键：**不必重跑**）。

    ★ 为什么必须落盘（#19）：2026-10-04 那条皇后种子每次上传都返回 HTTP 200 +
    "上传失败！" **空壳页**（`_extract_error()` 抠不出任何文字）。当时排查唯一的取证办法是
    **人肉 monkey-patch 复现**——而重跑要**真发种**（不可逆）。落盘后即可"发一次、查无数次"。
    ★ 键用**种子文件名**（`torrent_path` 的 basename）而非 info_hash：
      `takeupload.php` 表单里**没有** info_hash（站点自己从文件算），而文件名嵌了完整标题、
      对每条种子唯一 ⇒ 是这里拿得到的最好的稳定键。
    ★ 原子写（`.tmp` + `os.replace`），与 `crawl.save_raw()` / `daemon.save_state()` 同款；
      同一键反复失败只留**最后一份**（覆盖，不堆文件）。
    ★ 落盘失败**绝不**抛出——取证是"锦上添花"，不能反过来把发布搞崩（那才是本末倒置）。

    ★★ `kind`（2026-10-04 加，#23）：落到哪个站的后缀。**默认 `takeupload`**（乌托邦，保持
      #19 已上线行为不变）；**蟹黄堡**传 `crabpt-takeupload` ⇒ 落
      `<名>-crabpt-takeupload.html`。为什么必须区分两站：两个站可能**同名种子**
      （转种场景极常见：一条从乌托邦转去蟹黄堡）⇒ 用同一后缀会**互相覆盖**，
      把"乌托邦失败页"和"蟹黄堡失败页"混成一份、取证价值归零。

    返回落盘路径；失败返回 None。
    """
    try:
        os.makedirs(RAW_DIR, exist_ok=True)
        base = os.path.basename(torrent_path or "") or "unknown"
        # 去掉 .torrent 后缀 + 把路径不安全字符换成 `_`，避免把标题里的 `/` `:` 写进文件名
        key = re.sub(r"[^0-9A-Za-z一-鿿._-]+", "_", os.path.splitext(base)[0])[:80]
        p = os.path.join(RAW_DIR, f"{key or 'unknown'}-{kind}.html")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(html or "")
        os.replace(tmp, p)                       # 原子替换（同 crawl.save_raw）
        return p
    except OSError as e:                          # noqa: BLE001
        print(f"⚠️  失败响应落盘失败（不影响发布）: {e}")
        return None


def _extract_error(body):
    """NexusPHP 出错时通常渲染一个 '错误' 页面，把提示抠出来方便排查。

    ★★ 2026-10-04（`ERR-SITE-10`）：**加读 `<title>`**。
      实测乌托邦对某些种子回的是 **HTTP 200 + `<title>…上传失败！…</title>`** 的页面，
      **正文**（`#outer`）只有导航/账号栏/全站Free横幅、**零错误文本** ⇒ 老实现的
      四个选择器（`#outer table td.text` / `td.embedded` / `h2` / `h1`）**全部落空**，
      于是被上层兜底判成"未拿到重定向 ID…（**空壳**失败页）"。⇒ 把"**有名字的失败**"
      （标题就写着「上传失败」）降级成"**无名空壳**"，白让排查的人以为"站点什么都不说"。

    ★ 判据的**保守之处**（为什么不做成"标题有字就返回"）：只在标题里含**失败/错误**
      关键词时才取它 —— 否则蟹黄堡那种正常表单页标题（`蟹黄堡 :: 发布`）也会被误当错误。
      ⇒ 返回的是**标题里的失败字样**（仍拿不到"为什么失败"，但至少不再谎报"空壳"）。
    ★ 负面副作用已评估：蟹黄堡路径（`crabpt_site.py`）对"HTTP 200 + 无 details 链接"
      的判据是 **`UNKNOWN`**（**可能真发成功了**，不许当 REJECT）。其**正常**页面标题
      （`蟹黄堡 :: 发布`，实测）**不含**这些关键词 ⇒ 本改动**不会**把蟹黄堡误判成 REJECT。
    """
    if not body:
        return ""
    soup = BeautifulSoup(body, "html.parser")
    for sel in ("#outer table td.text", "td.embedded", "h2", "h1"):
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(" ", strip=True)
            if txt and len(txt) < 300 and ("错误" in txt or "Error" in txt or "失败" in txt):
                return txt
    # ★ 兜底读 `<title>`：只在这里**确定是失败页**时才返回（见 docstring 的保守判据）
    if soup.title:
        t = soup.title.get_text(" ", strip=True)
        if t and len(t) < 300 and ("上传失败" in t or "错误" in t or "Error" in t or "失败" in t):
            return t
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
