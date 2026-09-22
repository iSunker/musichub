"""
皇后封面的**取回**层：本地优先 → 页面封面位 → 描述图 → 下载 → 落盘缓存。

═══════════════════════════════════════════════════════════════════════════
它在整条流水线的哪一步
═══════════════════════════════════════════════════════════════════════════

    crawl.py            抓详情页 → 落盘 raw_html/<tid>.html
        ↓
    opencd_ingest.py    解析结果落库（tasks 行，含 `cover_candidates`）
        ↓
    ★ cover_fetch.py    取封面：本地找 → 页面找 → 下载 → **落盘**     ← 本模块
        ↓
    assets.upload_to_picgo()   上传到图床
        ↓
    db.set_cover()      写回 cover_url（★ **只有成功才写**）        ← 见下面「纪律」

═══════════════════════════════════════════════════════════════════════════
★ 用户定的优先级（2026-09-21）
═══════════════════════════════════════════════════════════════════════════

    「封面文件能在已下载好的种子文件里面找出来啊？我记得前面的代码就是这么写的，
      如果本地有，优先用本地，本地没有再去皇后请求。」

    ① **本地**（qB 的内容目录）：`assets.find_cover(content_path)`
       ⇒ **0 次皇后请求**，★ 最优先。而且**通常真有** —— 皇后种子在 qB 里做种
       （闸门①「做种满 3 天」就是证据），内容目录里就有 `folder.jpg`。
    ② **页面封面位** `<div class="cover">`：站内附件 / 外站图
    ③ **描述区** `divdescr`：已排除频谱图的图（兜底）

═══════════════════════════════════════════════════════════════════════════
三条纪律
═══════════════════════════════════════════════════════════════════════════

1. **绝不写空 `cover_url`**。`assets.py` 的 `upload_to_picgo` docstring 记着历史事故：
   「绝不能把"上传失败"当成"没有封面"——历史上 `run.py` 重做任务时正是这样
   把库里已有的 `cover_url` 覆盖成空，连锁导致站点页面封面被抹掉。」
   而 `db.set_cover()` 是**无条件 UPDATE**（没有"仅当为空才写"的护栏）
   ⇒ **这条纪律只能由调用方守住**：本模块**任一步失败都返回 `("", …)`**，
   由调用方据"是否非空"决定要不要写库。

2. **外站图不算皇后的请求配额**。实测封面**约一半是外站图**
   （`y.gtimg.cn` / `p4.music.126.net` / `imgaes.dpdns.org`）。
   那些**不走** `crawl.Client.get`（它一进去就 `RateGate.acquire()` 扣配额），
   而是直接用 `requests` 取 —— 否则会**白耗皇后那 120 秒/请求的额度**。

3. **不读时钟、不碰 DB、不 import `crawl`**。
   站内下载要复用 `crawl.Client`（拿它的 `RateGate` 与站点基址），
   但由**调用方传进来**，本模块不 import 它（避免循环导入，也让边界能用 grep 审计）。

═══════════════════════════════════════════════════════════════════════════
落盘缓存
═══════════════════════════════════════════════════════════════════════════

照抄 `crawl.RAW_DIR` 那套（`raw_html/`）：env 可覆盖、懒创建、**原子写**（`.tmp`+`os.replace`）。
键用**远程地址的 sha1 前 12 位**（不只用 tid）—— 因为**同一张图会被多个种子引用**
（实测 `folder.jpg` 那张图出现在多个页面里），按 tid 存会重复下载、白烧配额。
"""
import hashlib
import json
import os
import re
import sys
from urllib.parse import urlparse

import requests

# ★★ 必须在 `import config`/`assets` **之前**塞好 dummy 环境变量（跑自检时）：
#    `config` 用 `require_env`（缺了就 `sys.exit`）。用 `setdefault` ⇒
#    生产容器里已设好的值**绝不覆盖**。
if "--selftest" in sys.argv:
    os.environ.setdefault("QB_PASS", "x")
    os.environ.setdefault("MUSOPIA_COOKIE", "c_secure_pass=x")
    os.environ.setdefault("MUSOPIA_PASSKEY", "x")
    os.environ.setdefault("PICGO_API_KEY", "x")
    os.environ.setdefault("DATA_DIR", "/tmp/_cover-selftest-data")

import assets
import config

# 下载超时（秒）。★ 沿用 `crawl.Client.get` 的 30s 量级；图比 HTML 大，给宽一点。
DOWNLOAD_TIMEOUT = 40

# 魔数 → 扩展名（**不看 URL 后缀**：站内附件常是 `xxx.jpg.thumb.jpg`，
# 而且后缀可能骗人；只认字节）
MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),          # RIFF....WEBP；下面再看一眼
)

COVER_DIR = os.getenv("OPENCD_COVER_DIR", os.path.join(config.DATA_DIR, "covers"))


def is_external(url):
    """
    是不是**外站**图（不是皇后站内的附件）。

    ★ 判据是「**有没有 `attachments/`**」而不是「是不是绝对 URL」——
      实测站内附件地址是相对路径（`attachments/…`），外站图是绝对 URL。
      但也可能有站点把它写成绝对 URL（`https://open.cd/attachments/…`），
      所以**看路径里有没有 `attachments/`** 更稳。
    """
    u = (url or "").strip()
    if not u:
        return False
    return "attachments/" not in u


def image_ext(content):
    """
    从**字节**判断图片类型，返回扩展名或 `""`（不是图片）。

    ★ 看魔数不看后缀：站内附件常带 `.thumb.jpg` 后缀而已，但我们要确认
      **下载回来的确实是张图**。实测过 mock 服务器把 HTML 当图片返回 ⇒
      不校验就会把一个 HTML 页面当封面传上图床。
    """
    if not content:
        return ""
    for magic, ext in MAGIC:
        if content.startswith(magic):
            if ext == ".webp" and content[8:12] != b"WEBP":
                continue
            return ext
    return ""


def _cache_path(url, dest_dir=None):
    """缓存路径：`<dest_dir>/<sha1(url)[:12]><ext>`。ext 由**字节**决定，故先取内容。"""
    dest_dir = dest_dir or COVER_DIR
    return dest_dir, hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:12]


def _cached_file(url, dest_dir=None):
    """缓存里已有这张图吗？返回路径或 `""`。★ 靠**文件名前缀**找，不重下。"""
    dest_dir, key = _cache_path(url, dest_dir)
    if not os.path.isdir(dest_dir):
        return ""
    for fn in os.listdir(dest_dir):
        if fn.startswith(key + "."):
            p = os.path.join(dest_dir, fn)
            if os.path.getsize(p) > 0:
                return p
    return ""


def download(url, *, client=None, dest_dir=None, logger=None):
    """
    下载一张图到缓存目录，返回**本地路径**或 `""`（失败）。

    ★ 外站图 ⇒ 普通 `requests`（**不扣皇后配额**）
    ★ 站内附件 ⇒ `client.get(url)`（**过 RateGate**，扣 1 次请求）
      ⇒ 所以调用方要**先把候选筛好**再调这里（`acquire()` 是先扣再发，
        见 `opencd_rate.py` 的 docstring）。
    """
    def say(msg):
        if logger:
            logger(msg)

    u = (url or "").strip()
    if not u:
        return ""

    cached = _cached_file(u, dest_dir)
    if cached:
        say(f"      (缓存命中) {os.path.basename(cached)}")
        return cached

    external = is_external(u)
    try:
        if external:
            r = requests.get(u, timeout=DOWNLOAD_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        else:
            if client is None:
                say("      ⚠️ 站内图需要 client（拿 RateGate 用），但没有 ⇒ 跳过")
                return ""
            r = client.get(u)
    except Exception as e:
        say(f"      ❌ 下载异常：{type(e).__name__}: {e}")
        return ""

    if r is None or r.status_code != 200:
        say(f"      ❌ 下载失败 HTTP {getattr(r, 'status_code', '?')}")
        return ""

    content = r.content or b""
    if not content:
        say("      ❌ 下载到 0 字节")
        return ""
    # ★ 大小上限：**复用** `assets.MAX_COVER_BYTES`（它本来就是图床的大小上限）
    if len(content) > assets.MAX_COVER_BYTES:
        say(f"      ❌ 太大 {len(content)} > {assets.MAX_COVER_BYTES}（图床上限）")
        return ""

    ext = image_ext(content)
    if not ext:
        ctype = (r.headers.get("Content-Type") or "")[:40]
        say(f"      ❌ 不是图片（魔数不匹配；Content-Type={ctype!r}，{len(content)} 字节）")
        return ""

    dest_dir, key = _cache_path(u, dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    p = os.path.join(dest_dir, key + ext)
    tmp = p + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(content)
    os.replace(tmp, p)                    # ★ 原子替换（同 crawl.save_raw）
    say(f"      💾 已缓存 {os.path.basename(p)}（{len(content) // 1024} KB）")
    return p


def pick_candidates(info, *, content_path=None):
    """
    把「本地目录」+「页面封面候选」合成一个**有序候选列表**。

    返回 `[(source, url_or_path)]`，`source ∈ {"local", "page"}`。
    ★ 纯函数（不下载、不读时钟），方便穷举测试。

    ★ 顺序 = 用户定的优先级：**本地最优先**，然后按解析层给好的页面顺序
      （解析层已按 folder/cover/front → cd/disc → 其它 排过）。
    """
    out = []

    # ① 本地内容目录（0 次请求）—— ★ 复用 assets.find_cover，不重写
    if content_path:
        local = ""
        try:
            local = assets.find_cover(content_path)
        except Exception:
            local = ""
        if local:
            out.append(("local", local))

    # ②③ 页面候选（解析层已排好序；`div.cover` 优先、已排除频谱图）
    for item in (info.get("cover_candidates") or []):
        if isinstance(item, str):
            url = item.strip()
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            url = str(item[1] or "").strip()
        else:
            url = ""
        if url:
            out.append(("page", url))

    return out


def fetch_cover(info, *, client=None, content_path=None, dest_dir=None, logger=None):
    """
    按优先级取一张封面，返回 `(local_path, source, reason)`。

    ★★ **任何一步失败都返回 `("", …, 原因)`，绝不抛异常给调用方崩**
      —— 且**绝不返回一个"空"当成功**（见模块头纪律 1）。

    ★ **只试到第一张成功为止**：站内附件每试一次就扣 1 次皇后配额
      （120 秒/次），所以失败要**早停**，不做"三张都试一遍"。
    """
    def say(msg):
        if logger:
            logger(msg)

    cands = pick_candidates(info, content_path=content_path)
    if not cands:
        return "", "", "页面与本地都没有封面候选"

    for source, target in cands:
        if source == "local":
            say(f"      📁 本地找到封面：{os.path.basename(target)}")
            return target, "local", ""
        # ── 页面候选：下载 ──
        kind = "外站图（不占皇后配额）" if is_external(target) else "站内附件（过限速）"
        say(f"      ⬇️  下载 {kind}：{target[:70]}")
        p = download(target, client=client, dest_dir=dest_dir, logger=logger)
        if p:
            return p, "page", ""
        # ★ 站内附件失败 ⇒ **不再试下一个**：每次都是 120 秒的真实配额
        if not is_external(target):
            return "", "", f"站内附件下载失败（不再重试以省配额）：{target[:60]}"

    return "", "", "所有候选都取不到"


# ==================== 自检 ====================

class _FakeResp:
    def __init__(self, content=b"", status=200, ctype="image/jpeg"):
        self.content = content
        self.status_code = status
        self.headers = {"Content-Type": ctype}


class _FakeClient:
    """假 client：记下每次被调的 URL，返回预置字节。★ 用来验"有没有白扣配额"。"""

    def __init__(self, payloads=None, status=200, ctype="image/jpeg"):
        self.calls = []
        self.payloads = payloads or {}
        self.status = status
        self.ctype = ctype

    def get(self, path, **params):
        self.calls.append(path)
        body = self.payloads.get(path, JPEG_BYTES)
        return _FakeResp(body, self.status, self.ctype)


# 一张极小的**真 JPEG**（1×1 px）—— 魔数校验要过，所以不能是随便的字节
JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00" + b"\x08" * 64 +
    b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    b"\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\x37\xff\xd9"
)


def _check():
    import shutil
    import tempfile

    ok = fails = 0

    def chk(label, good, extra=""):
        nonlocal ok, fails
        ok, fails = ok + bool(good), fails + (not bool(good))
        print(f"  {'✅' if good else '❌'} {label}{('  ' + extra) if extra else ''}")

    tmp = tempfile.mkdtemp(prefix="cover-fetch-selftest-")
    cache = tmp

    print("① is_external：站内 vs 外站")
    chk("attachments/… ⇒ 站内", is_external("attachments/a/b.jpg") is False)
    chk("https://y.gtimg.cn/… ⇒ 外站", is_external("https://y.gtimg.cn/x.jpg") is True)
    chk("★ 绝对 URL 但含 attachments/ ⇒ 仍算站内（不该白扣配额）",
        is_external("https://open.cd/attachments/a/b.jpg") is False)

    print("\n② image_ext：只看**字节**，不看后缀")
    chk("JPEG 魔数 ⇒ .jpg", image_ext(JPEG_BYTES) == ".jpg")
    chk("PNG 魔数 ⇒ .png", image_ext(b"\x89PNG\r\n\x1a\n" + b"x" * 20) == ".png")
    chk("★ HTML（不是图）⇒ 空", image_ext(b"<!DOCTYPE html><html>") == "")
    chk("空 ⇒ 空", image_ext(b"") == "")

    print("\n③ download：内容校验（★ 阴性对照：只看 HTTP 200 会中招）")
    c = _FakeClient()
    chk("正常图片 ⇒ 落盘",
        bool(download("attachments/a/ok.jpg", client=c, dest_dir=cache)))
    # ★ 这条**必须换内容**而不只是换 Content-Type —— 我们的实现刻意
    #   只看**魔数**、不看 Content-Type（后缀与头部都会骗人）。
    #   ★ 第一版我写成"只改 Content-Type、内容仍是 JPEG" ⇒ 实现**正确地**接收了
    #     它 ⇒ 判据假失败。**判据写错了要改判据，不是改实现**。
    html_url = "attachments/a/x.jpg"
    p_html = download(html_url, dest_dir=cache, logger=lambda m: None,
                      client=_FakeClient(payloads={html_url: b"<!DOCTYPE html><html>hi</html>"},
                                         ctype="text/html"))
    chk("★ 内容是 HTML（魔数不对）⇒ 拒绝", p_html == "")
    # 阴性对照的另一半：确认"拒绝"是**因为魔数**，不是因为别的（比如大小）
    chk("（对照）同一 URL 给真 JPEG ⇒ 接纳",
        bool(download(html_url, dest_dir=cache, logger=lambda m: None,
                      client=_FakeClient(payloads={html_url: JPEG_BYTES}))))
    big = JPEG_BYTES + b"\x00" * (assets.MAX_COVER_BYTES + 10)
    chk("★ 超过 MAX_COVER_BYTES ⇒ 拒绝",
        download("attachments/a/big.jpg", client=_FakeClient(payloads={"attachments/a/big.jpg": big}),
                 dest_dir=cache) == "")
    chk("HTTP 404 ⇒ 拒绝",
        download("attachments/a/404.jpg", client=_FakeClient(status=404), dest_dir=cache) == "")

    print("\n④ ★★ 外站图**不走 client**（否则白扣皇后配额）")
    c = _FakeClient()
    # 用真 requests 会出网，所以这里只验「外站分支不碰 client」：
    #   传一个会记录调用的 client，外站 URL 不应出现在它的 calls 里。
    #   ★ 让下载失败没关系（本机不出网）—— 关键是 client 没被调用。
    download("https://127.0.0.1:1/nope.jpg", client=c, dest_dir=cache, logger=lambda m: None)
    chk("外站图：client.calls == []", c.calls == [], f"{c.calls}")
    c2 = _FakeClient()
    download("attachments/a/station.jpg", client=c2, dest_dir=cache)
    chk("站内图：client 恰好被调 1 次", len(c2.calls) == 1, f"{c2.calls}")

    print("\n⑤ 缓存：同一 URL 第二次不再请求")
    c = _FakeClient()
    u = "attachments/cache/once.jpg"
    first = download(u, client=c, dest_dir=cache)
    n1 = len(c.calls)
    second = download(u, client=c, dest_dir=cache)
    chk("第一次下载成功", bool(first))
    chk("★ 第二次**不增加** client 调用", len(c.calls) == n1, f"{n1} → {len(c.calls)}")
    chk("两次拿到同一个文件", first == second)

    print("\n⑥ pick_candidates：本地最优先")
    info = {"cover_candidates": [("cover.jpg", "attachments/a/p.jpg")]}
    local_dir = os.path.join(tmp, "album")
    os.makedirs(local_dir, exist_ok=True)
    with open(os.path.join(local_dir, "folder.jpg"), "wb") as fh:
        fh.write(JPEG_BYTES)
    got = pick_candidates(info, content_path=local_dir)
    chk("本地排第一、页面排第二",
        [s for s, _ in got] == ["local", "page"], f"{[s for s, _ in got]}")
    got2 = pick_candidates(info, content_path=None)
    chk("无本地 ⇒ 只剩页面", [s for s, _ in got2] == ["page"])

    print("\n⑦ ★ 本地优先 ⇒ **一次网络都不发**")
    c = _FakeClient()
    path, source, why = fetch_cover(info, client=c, content_path=local_dir,
                                    dest_dir=cache, logger=lambda m: None)
    chk("source == 'local'", source == "local", f"{source!r}")
    chk("★ client 一次都没被调（0 次皇后请求）", c.calls == [], f"{c.calls}")
    chk("拿到的是本地那个文件", os.path.basename(path) == "folder.jpg")

    print("\n⑧ 页面候选：站内附件**恰好 1 次**请求（不试第 2 个）")
    info2 = {"cover_candidates": [("folder.jpg", "attachments/a/1.jpg"),
                                  ("disc.jpg", "attachments/a/2.jpg"),
                                  ("back.jpg", "attachments/a/3.jpg")]}
    c = _FakeClient()
    path, source, why = fetch_cover(info2, client=c, content_path=None,
                                    dest_dir=cache, logger=lambda m: None)
    chk("source == 'page'", source == "page")
    chk("★ 恰好 1 次请求（成功即停）", len(c.calls) == 1, f"{c.calls}")
    chk("请求的是第 1 个候选", c.calls[:1] == ["attachments/a/1.jpg"])

    print("\n⑨ 站内附件失败 ⇒ **早停**，不试后面（省配额）")
    info3 = {"cover_candidates": [("a.jpg", "attachments/f/1.jpg"),
                                 ("b.jpg", "attachments/f/2.jpg")]}
    c = _FakeClient(status=500)
    path, source, why = fetch_cover(info3, client=c, content_path=None,
                                    dest_dir=cache, logger=lambda m: None)
    chk("返回空路径", path == "")
    chk("★ 只试了 1 次就停", len(c.calls) == 1, f"{c.calls}")
    chk("reason 非空（供调用方记日志）", bool(why))

    print("\n⑩ 无候选 ⇒ 空且不炸")
    path, source, why = fetch_cover({"cover_candidates": []}, content_path=None,
                                    dest_dir=cache, logger=lambda m: None)
    chk("返回 ('', '', 原因)", path == "" and source == "" and bool(why))

    shutil.rmtree(tmp, ignore_errors=True)
    return ok, fails


def main():
    import argparse
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="皇后封面取回（本地优先 → 页面 → 下载）")
    ap.add_argument("--selftest", action="store_true", help="跑离线自检")
    # ★ --json：机读出口，给 tools/gen_counts.py 读。别去正则解析人读的 ✅/❌ 行 —— 分母口径不统一。
    ap.add_argument("--json", action="store_true",
                    help="只把结果以 JSON 打到末行（给 gen_counts.py 读）")
    args = ap.parse_args()
    if not args.selftest:
        ap.error("加 --selftest（实际取封面由 backfill_covers_opencd.py 驱动）")

    if args.json:
        ok, fails = _check()
        print(json.dumps({"script": "cover_fetch.py --selftest", "ok": ok,
                          "total": ok + fails, "exit": 1 if fails else 0},
                         ensure_ascii=False))
        return 1 if fails else 0

    print("=" * 74)
    print("cover_fetch 离线自检（纯本地，零请求）")
    print("=" * 74)
    ok, fails = _check()
    print("\n" + "=" * 74)
    if fails:
        print(f"❌ 自检失败：{ok}/{ok + fails} 通过，{fails} 项不符")
        return 1
    print(f"✅ 自检通过：{ok}/{ok} 项全对")
    return 0


if __name__ == "__main__":
    sys.exit(main())
