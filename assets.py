"""
本地资源提取：封面图查找与图床上传、抓轨日志读取、CUE 曲目解析。

run.py 里这些逻辑原本是内联的，backfill_covers.py 也需要同一套（补历史缺失的封面），
所以抽出来共用，避免两处实现慢慢跑偏。
"""
import os
import re

import requests

import config

# 按优先级排列：cover/folder/front 是通行命名，放前面优先命中
COVER_PATTERNS = [
    re.compile(r"^(cover|folder|front|frontal)\.(jpg|jpeg|png|webp)$", re.I),
    re.compile(r"(cover|folder|front)", re.I),
    re.compile(r"(artwork|poster|scan|booklet|case|inlay|box)", re.I),
    re.compile(r"(a|album|cd|disc)\d*\s*$", re.I),
]

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# 图床有大小限制，太大的原始扫图先跳过而不是上传失败
MAX_COVER_BYTES = 15 * 1024 * 1024

LOG_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16", "latin-1")

# 分轨专辑没有 CUE 时，用音频文件名兜底做曲目列表
AUDIO_EXTS = (".flac", ".ape", ".wav", ".mp3", ".m4a", ".dts", ".ogg", ".wv", ".tak", ".tta")

# 有些 CUE 是西欧单字节码页（CP1252/ANSI）写出来的，表示不了中日文，于是
# **每个非 ASCII 字符被整体替换成一个 "."**（"唇にBe My Baby" → "..Be My Baby"，
# ASCII 部分原样保留）。这种 CUE 里 TRACK TITLE 会退化成纯点号，
# 按它渲染出来就是一份没人能读的曲目列表（站点会以「請更正曲目列表，使其易於閱讀」打回）。
# 判据按**比例**而不是「全部是点」：ASCII 曲名会幸存（如 "Lydia"），
# 一条幸存项就能让「全是点」的判定失效。
_DEGENERATE_RE = re.compile(r"^[\s.?_*~\-]*$")
# 补充判据：含连续点号（"唇にBe My Baby" → "..Be My Baby"，ASCII 部分幸存，
# 逃过「全是点」的判定）。**不能单独用** —— 实测会误伤正当曲名
# （莫扎特小夜曲 "…Serenade No. 13 in G major for st..."）。只在下面
# _degenerate_ratio 里、**这份 CUE 已被证实存在点号替换之后**才启用。
_DOT_RUN = re.compile(r"\.\.")
# 私用区码位（U+E000–U+F8FF）是**解码用错了码页**的铁证 —— 正常曲名里不会出现。
# 实证：`MONSTER HUNTER WILDS` 的 CUE 真实码页是 Big5 系，不在 LOG_ENCODINGS 里，
# `gbk` 失败后，排在它前面的 `gb18030`「成功」解出带私用区的乱码 —— 调用方收不到
# 任何失败信号（又是「把解不开伪装成解开了」）。
# 见到私用区就认定这份 CUE 不可信，整份回退到音频文件名。


def _has_pua(text):
    """文本里有没有私用区码位（U+E000–U+F8FF）。有 = 解码用错了码页。"""
    return any(0xE000 <= ord(c) <= 0xF8FF for c in text)


DEGENERATE_RATIO = 0.5

# 为什么是 0.5：实测全库退化比例在 11%~91% 之间**连续分布，没有干净的分界**
# （因为比例只是「曲名含中日文的比例」的代理指标）。低比例的专辑（1%/5%/8%）
# 往往只是整张里坏了一条，按比例整体回退会连 90% 的好曲名一起丢掉。
# 取 0.5 是**宁可漏判、不可误判**。

# 第三类解错码页：解出来**不是**私用区，而是**合法但无意义的汉字**。
#
# Shift-JIS 写的 CUE（日本专辑）不在 LOG_ENCODINGS 里；`gbk` 失败后，排在它前面的
# `gb18030` 这个「永不失败」的兜底会把它整份吃下去：
#
#     原字节 82a0 82c8 82bd 82c9 96b2 9286   (Shift-JIS "あなたに夢中")
#       gb18030 解出 → 偁側偨偵柌拞                ← 既没有点号，也没有私用区
#
# 于是 `_has_pua()` 和点号退化**两条判据双双失明**，站点上就挂着这么一份曲目列表。
# 实证：14619 / 14640 / 14740 / 14824 四张日本专辑，全部已推上站。
#
# 判据：把标题按 gb18030 **编回原始字节**（gb18030 是双射，只有它能把解出来的
# 东西精确还原成原字节），再用候选码页严格重解。重解得**成串假名**就说明
# 这份 CUE 的真实码页是它。
#
# 要求「成串」（≥2 连）而不是「有个假名就算」：单蹦一个假名在误命中里太常见 ——
# GB 中文编回字节再用 big5 解，很容易蹦出一个孤立的假名来。
_MOJIBAKE_CANDIDATES = ("cp932", "euc_jp", "big5", "cp949")
_KANA_RE = re.compile(r"[ぁ-ゖァ-ヺ]")        # 真·假名（刻意不含 ・ U+30FB 与 ー U+30FC）
_KANA_RUN_RE = re.compile(r"[ぁ-ゖァ-ヺ]{2,}")

# 实测全库（分母=非 ASCII 标题）：真命中 37.5% / 70.1% / 80.0% / 84.6%，
# 误命中最高 2.9%（"风凄凄意绵绵" 编回字节用 big5 解，恰好出「ぼぼ」）。
# 取 0.25：离最高误命中还有 8 倍余量，同时接得住最低的真命中（37.5%）。
MOJIBAKE_RATIO = 0.25


def _read_text(path):
    """按 LOG_ENCODINGS 依次尝试解码，全失败则忽略坏字节兜底。读不出返回 ""。"""
    for enc in LOG_ENCODINGS:
        try:
            with open(path, "r", encoding=enc) as fh:
                return fh.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
        except OSError as e:
            print(f"    ⚠️  读取 {path} 出错: {e}")
            return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError as e:
        print(f"    ⚠️  读取 {path} 彻底失败: {e}")
        return ""


def _disc_label(path, folder_path):
    """
    从路径里认碟号，返回 "CD1"/"CD2"…，认不出返回 ""。
    先看相对 folder_path 的子目录名（CD1 / Disc 2 / DISK_3），再看文件名本身。
    """
    try:
        rel = os.path.relpath(path, folder_path)
    except ValueError:
        rel = os.path.basename(path)
    for seg in rel.replace("\\", "/").split("/"):
        m = re.search(r"(?:CD|DISC|DISK)\s*_?\s*(\d+)", seg, re.I)
        if m:
            return f"CD{int(m.group(1))}"
    return ""


def _assign_disc_labels(discs):
    """
    给一串碟号补全（元素可能为 "" 表示认不出），返回等长的新列表。

    认得出的原样保留；认不出的补**尚未被占用的最小编号**。

    ★ 不能拿遍历序号补（`CD{idx}`）—— 「认出 CD1、CD3 + 一个认不出」时，
    认不出的那个 idx=3 会补成 `CD3`，**与已有的 CD3 撞号**，页面上就出现
    两个同名的折叠块。补号必须避开已被占用的编号。
    """
    used = {d for d in discs if d}
    out, nxt = [], 1
    for d in discs:
        if d:
            out.append(d)
            continue
        while f"CD{nxt}" in used:
            nxt += 1
        used.add(f"CD{nxt}")
        out.append(f"CD{nxt}")
        nxt += 1
    return out


def find_cover(folder_path):
    """
    在目录里找最合适的封面图。返回路径或 ""。
    先按命名模式分级匹配，同级里挑文件最大的（通常分辨率最高）。
    """
    if not folder_path or not os.path.exists(folder_path):
        return ""

    if os.path.isfile(folder_path):
        folder_path = os.path.dirname(folder_path)

    images = []
    for root, _, files in os.walk(folder_path):
        for f in files:
            if not f.lower().endswith(IMAGE_EXTS):
                continue
            p = os.path.join(root, f)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size == 0 or size > MAX_COVER_BYTES:
                continue
            images.append((f, p, size))

    if not images:
        return ""

    for pattern in COVER_PATTERNS:
        hits = [(f, p, s) for f, p, s in images if pattern.search(os.path.splitext(f)[0])
                or pattern.search(f)]
        if hits:
            hits.sort(key=lambda x: -x[2])
            return hits[0][1]

    # 一张都没匹配上命名规则，就退而取最大的那张图
    images.sort(key=lambda x: -x[2])
    return images[0][1]


def _picgo_extract_url(data):
    """从 PicGo 响应 JSON 里抠出图片 URL，兼容几种常见结构。"""
    if not isinstance(data, dict):
        return ""
    img = data.get("image") or {}
    if isinstance(img, dict):
        url = img.get("url") or img.get("display_url") or ""
        if url:
            return url
    # Chevereto 系图床报 Duplicated upload 时，有些版本会把已存在图片的
    # 信息放在 result 里返回，别浪费，直接回收复用
    result = data.get("result") or {}
    if isinstance(result, dict):
        url = result.get("url") or ""
        if url:
            return url
    return str(data.get("url") or "")


def upload_to_picgo(image_path, retries=2):
    """
    上传图片到 PicGo，返回 URL 或 ""。

    重复上传时图床会报 400 Duplicated upload。此时尽力从响应里回收已有
    图片的 URL（Chevereto 部分版本会返回 result.url），回收不到才返回空。
    绝不能把"上传失败"当成"没有封面"——历史上 run.py 重做任务时正是
    这样把库里已有的 cover_url 覆盖成空，连锁导致站点页面封面被抹掉。
    """
    if not image_path or not os.path.exists(image_path):
        return ""

    headers = {"X-API-Key": config.PICGO_API_KEY}
    last_err = ""

    for attempt in range(retries + 1):
        try:
            with open(image_path, "rb") as f:
                res = requests.post(
                    config.PICGO_UPLOAD_URL,
                    headers=headers,
                    files={"source": f},
                    timeout=40,
                )
            if res.status_code == 200:
                url = _picgo_extract_url(res.json())
                if url:
                    return url
                last_err = f"响应里没有 url 字段: {res.text[:150]}"
            elif res.status_code == 400 and "duplicat" in res.text.lower():
                # 重复上传：图床已存过这张图。先尝试从错误响应里回收 URL。
                try:
                    url = _picgo_extract_url(res.json())
                except ValueError:
                    url = ""
                if url:
                    print(f"    [图床] {os.path.basename(image_path)} 已存在，"
                          f"复用已有 URL: {url}")
                    return url
                last_err = ("重复上传但响应里没有可回收的 URL "
                            f"(HTTP 400: {res.text[:150]})")
            else:
                last_err = f"HTTP {res.status_code}: {res.text[:150]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

        if attempt < retries:
            continue

    print(f"    [图床失败] {os.path.basename(image_path)} -> {last_err}")
    return ""


def _lsky_extract_url(data):
    """从 Lsky Pro 响应 JSON 里抠出图片 URL。

    ★ 路径是**实测**的（蟹黄堡站内「接口」页，`opencdhtml/蟹黄宝图链接口-Lsky Pro.html`）：

        {"status": true, "message": "上传成功",
         "data": {"key": "...", "links": {"url": "...", "bbcode": "...",
                                          "markdown": "...", ...}}}

    ⇒ 正确路径是 `data.links.url`。
    ★ **不要**照抄 picgo 那边的 `image.url` / `result.url` —— 那是 Chevereto 系的结构
      （且 `result` 那一支专为"重复上传回收"而生，Lsky 没有对应物，见下）。
    ★ 返回的是**裸 URL**，不带 BBCode —— 因为调用方（`descr.build_descr`）
      自己会套 `[img]…[/img]`；这里若返回 `bbcode` 就会**嵌套两层**。
    """
    if not isinstance(data, dict):
        return ""
    d = data.get("data")
    if isinstance(d, dict):
        links = d.get("links")
        if isinstance(links, dict):
            url = links.get("url") or ""
            if url:
                return url
        # 少数版本可能把 url 直接挂在 data 下，认了它（但**不**去猜别的结构）
        if d.get("url"):
            return str(d["url"])
    return ""


def upload_to_lsky(image_path, retries=2, api_url=None, token=None):
    """上传图片到**蟹黄堡的专用图床**（Lsky Pro），返回 URL 或 ""。

    ★★ 为什么单独一个函数、不复用 `upload_to_picgo`（2026-09-25 用户要求
      「蟹黄堡要用专用的图链」）。三处**实质性**不同，逐条列清免得下次有人来合并：

        项           picgo（乌托邦）                    Lsky（蟹黄堡）
        ----------   ------------------------------     ---------------------------
        鉴权头       `X-API-Key: <key>`                 ★ `Authorization: Bearer <token>`
        文件字段名   `source`                           ★ `file`
        取 URL       `image.url` / `result.url`         ★ `data.links.url`
        接口地址     `.../api/1/upload`                 ★ `<base>/upload`（base 已含 /api/v1）

    ★★ **Lsky 没有"重复上传"这条回收路径** —— 别把 picgo 那段 400-Duplicated
      的回收逻辑照抄过来：Lsky 上传同一张图会**直接再存一份**（也可能因策略去重，
      但**没有**约定俗成的 400 + 可回收 URL）。
      ⇒ 照抄的代价是：把一个**真错误**（如 401/403）当成"已存在、复用 URL"而静默吞掉
        —— 那正是 `ERR-META-01`「检测方法失效比不检测更危险」的形状。
      ⇒ 所以这里**只认 `HTTP 200 + status:true + data.links.url`** 这一条成功判据，
        其余一律当失败（返 `""` 并打原因）。

    ★★ 返回 `""` 表示**失败**，调用方**必须**当成"没有封面"处理
      —— **绝不能**退回用 picgo 的链去发蟹黄堡（用户明确否掉了跨站图链），
      也**绝不能**因此把库里已存的封面覆盖成空（`upload_to_picgo` docstring
      记着的那次事故）。

    ★ `token` 为 None 时从 `config.LSKY_TOKEN` 取；**空 token 直接返 `""`**
      （不带鉴权头去传 = 站点按「游客上传」处理，行为不明、且可能把图传到
       别人的账下 ⇒ 宁可不传）。★ token **绝不打印**（同 `guard_auto_key` 的纪律：
      值不进日志，而 docker logs 是本项目最容易被抄走的那份）。
    """
    if not image_path or not os.path.exists(image_path):
        return ""

    token = config.LSKY_TOKEN if token is None else token
    if not (token or "").strip():
        print("    [Lsky 图床] 没配 LSKY_TOKEN（空）⇒ **不上传**。"
              "★ 见 ERR-ENV-02：新增的 .env 键要在 **NAS 上手工加**。")
        return ""

    base = (api_url or config.LSKY_API_URL).rstrip("/")
    url = f"{base}/upload"
    # ★ Accept 是接口页**明确要求**的（不是可选）：不设它站点可能回 HTML 而不是 JSON。
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/json",
    }
    last_err = ""

    for attempt in range(retries + 1):
        try:
            with open(image_path, "rb") as f:
                # ★ 字段名是 `file`（实测）。★ 不传 `strategy_id` ⇒ 由站点用默认策略
                #   （接口页把它列为**可选**；我们不掌握策略 id，猜一个反而可能传错桶）。
                res = requests.post(
                    url,
                    headers=headers,
                    files={"file": (os.path.basename(image_path), f)},
                    timeout=60,
                )
            if res.status_code == 200:
                try:
                    data = res.json()
                except ValueError:
                    last_err = f"HTTP 200 但响应不是 JSON: {res.text[:150]}"
                    continue
                # ★★ 两道判据都要：HTTP 200 **且** status 为真。
                #   Lsky 在业务失败时也回 200（`status:false` + message），
                #   只看状态码就会把"上传被拒"读成"成功"。
                if not data.get("status"):
                    last_err = f"status=false: {str(data.get('message'))[:150]}"
                    continue
                u = _lsky_extract_url(data)
                if u:
                    return u
                last_err = f"响应里没有 data.links.url: {res.text[:150]}"
            elif res.status_code == 401:
                # ★ 单独认这一条：这是**最可能**的失败（token 过期/抄错），
                #   而它的修法与别的原因**完全不同**（去 NAS 换 .env）。
                last_err = ("HTTP 401 授权失败 —— LSKY_TOKEN 不对或已失效。"
                            "★ 去蟹黄堡「接口」页重新生成 token，"
                            "再改 **NAS 的 .env**（deploy.sh 不带 .env，见 ERR-ENV-02）")
            elif res.status_code == 403:
                last_err = "HTTP 403 —— 蟹黄堡管理员关闭了接口功能"
            elif res.status_code == 429:
                last_err = (f"HTTP 429 —— 超出请求配额"
                            f"（X-RateLimit-Remaining="
                            f"{res.headers.get('X-RateLimit-Remaining')!r}）")
            else:
                last_err = f"HTTP {res.status_code}: {res.text[:150]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

    print(f"    [Lsky 图床失败] {os.path.basename(image_path)} -> {last_err}")
    return ""


def read_logs(folder_path):
    """
    收集目录下**全部** .log 抓轨日志，返回 [{"label","filename","content"}, ...]。

    - 单个日志：label="LOG"。
    - 多个日志：能从子目录/文件名认出碟号就用「CDx LOG」，认不出的按出现顺序补号。
    - 按碟号/路径自然排序，保证 CD1 在 CD2 前。
    EAC / dBpoweramp / XLD 导出的编码不统一，用 _read_text 多编码兜底。
    """
    if not folder_path or not os.path.exists(folder_path):
        return []

    if os.path.isfile(folder_path):
        folder_path = os.path.dirname(folder_path)

    found = []
    for root, _, files in os.walk(folder_path):
        for f in files:
            if not f.lower().endswith(".log"):
                continue
            path = os.path.join(root, f)
            found.append((path, f, _disc_label(path, folder_path)))

    if not found:
        return []

    def sort_key(item):
        path, _f, disc = item
        num = int(disc[2:]) if disc else 9999
        return (num, path.lower())

    found.sort(key=sort_key)
    labels = _assign_disc_labels([d for _p, _f, d in found])

    single = len(found) == 1
    logs = []
    for idx, (path, fname, _disc) in enumerate(found, start=1):
        content = _read_text(path)
        label = "LOG" if single else f"{labels[idx - 1]} LOG"
        logs.append({"label": label, "filename": fname, "content": content})
    return logs


def read_log(folder_path):
    """兼容旧调用：返回首个日志的 (content, filename)。新代码请用 read_logs。"""
    logs = read_logs(folder_path)
    if not logs:
        return "", ""
    return logs[0]["content"], logs[0]["filename"]


def _cue_titles(text):
    """从 CUE 文本里抽 TRACK 的 TITLE。返回曲名列表（去掉 TITLE 前缀和引号）。"""
    titles = []
    in_track = False
    for line in (text or "").splitlines():
        s = line.strip()
        u = s.upper()
        if u.startswith("TRACK "):
            in_track = True
            continue
        if in_track and u.startswith("TITLE") and "FILE" not in u:
            val = s[5:].strip().strip('"').strip()
            if val:
                titles.append(val)
    # 有些 CUE 不带 TRACK 关键字（少见），兜底取全部 TITLE，并丢掉第一条（通常是专辑名）
    if not titles:
        for line in (text or "").splitlines():
            s = line.strip()
            u = s.upper()
            if u.startswith("TITLE") and "FILE" not in u:
                val = s[5:].strip().strip('"').strip()
                if val:
                    titles.append(val)
        if len(titles) > 1:
            titles = titles[1:]
    return titles


def _audio_filenames_in(directory):
    """列出**单个目录**里的音频文件名（去扩展名，已排序）。读不到返回 []。"""
    try:
        names = [os.path.splitext(f)[0] for f in os.listdir(directory)
                 if f.lower().endswith(AUDIO_EXTS)]
    except OSError:
        return []
    names.sort()
    return names


def _mojibake_codepage(text):
    """
    这条标题是不是「被 gb18030 兜底吃掉的别的码页」。是就返回那个码页名，否则 ""。

    已经含真假名的直接放行 —— 那说明本来就没解错，不必再猜。
    """
    s = (text or "").strip()
    if len(s) < 2 or _KANA_RE.search(s):
        return ""
    try:
        raw = s.encode("gb18030")      # 双射，能精确还原原始字节
    except UnicodeEncodeError:
        return ""
    for enc in _MOJIBAKE_CANDIDATES:
        try:
            alt = raw.decode(enc)      # 严格解码，**不要** errors= 兜底
        except UnicodeDecodeError:
            continue
        if _KANA_RUN_RE.search(alt):
            return enc
    return ""


def _mojibake_ratio(titles):
    """
    titles 里「假汉字」占**非 ASCII 标题**的比例。没有非 ASCII 标题返回 0.0。

    ★ 分母为什么不是 len(titles)：假汉字只可能出现在**含非 ASCII 字符**的标题上，
    ASCII 标题（"Something"）在任何码页下都长一样。拿总条数当分母，非 ASCII 占比
    低的专辑会被稀释掉 —— 实测 14640 用总条数算只有 23.1%、够不着阈值，
    换成非 ASCII 分母是 37.5%；而两份误命中两种算法都只有 ~2.5%。
    """
    non_ascii = [t for t in titles if t and any(ord(c) > 127 for c in t)]
    if not non_ascii:
        return 0.0
    bad = sum(1 for t in non_ascii if _mojibake_codepage(t))
    return bad / len(non_ascii)


def _degenerate_ratio(titles):
    """
    titles 里「退化」项的占比。空列表返回 0.0。

    先数**确凿**的退化项（整条只剩 . ? _ - * ~ 和空白）。一个都没有，说明这份
    CUE 没有发生过点号替换，直接返回 0.0 —— 此时**不启用**「连续点号」这条补充
    判据，免得把 "Serenade No. 13 ... for st..." 这类正当曲名算成坏的。

    有确凿退化项时，再把含连续点号的标题一并计入（ASCII 幸存的半坏标题）。
    """
    if not titles:
        return 0.0
    # 私用区 = 解码用错了码页，这份 CUE 整份不可信
    if any(_has_pua(t) for t in titles):
        return 1.0
    if not any(_DEGENERATE_RE.match(t) for t in titles):
        return 0.0
    return sum(1 for t in titles
               if _DEGENERATE_RE.match(t) or _DOT_RUN.search(t)) / len(titles)


def _audio_filenames_tracklist(folder_path):
    """无 CUE 时兜底：按目录列音频文件名（去扩展名）。多目录(CD1/CD2)加【CDx】表头。"""
    by_dir = {}
    for root, _, files in os.walk(folder_path):
        names = _audio_filenames_in(root)
        if names:
            by_dir[root] = names

    if not by_dir:
        return ""

    if len(by_dir) == 1:
        return "\n".join(next(iter(by_dir.values())))

    def dir_key(d):
        disc = _disc_label(os.path.join(d, "x"), folder_path)
        num = int(disc[2:]) if disc else 9999
        return (num, d.lower())

    dirs = sorted(by_dir, key=dir_key)
    heads = _assign_disc_labels(
        [_disc_label(os.path.join(d, "x"), folder_path) for d in dirs])
    blocks = []
    for idx, d in enumerate(dirs, start=1):
        blocks.append(f"【{heads[idx - 1]}】\n" + "\n".join(by_dir[d]))
    return "\n".join(blocks)


def read_tracklist(folder_path):
    """
    曲目列表。优先聚合**所有** CUE 的 TRACK TITLE；多 CUE 时每段加【CDx】表头。
    没有 CUE（分轨）时退回音频文件名。返回换行分隔的字符串。
    """
    if not folder_path or not os.path.exists(folder_path):
        return ""

    if os.path.isfile(folder_path):
        folder_path = os.path.dirname(folder_path)

    cues = []
    for root, _, files in os.walk(folder_path):
        for f in files:
            if f.lower().endswith(".cue"):
                path = os.path.join(root, f)
                cues.append((path, _disc_label(path, folder_path)))

    if cues:
        def sort_key(item):
            path, disc = item
            num = int(disc[2:]) if disc else 9999
            return (num, path.lower())

        cues.sort(key=sort_key)
        multi = len(cues) > 1
        heads = _assign_disc_labels([d for _p, d in cues])
        blocks = []
        for idx, (path, _disc) in enumerate(cues, start=1):
            titles = _cue_titles(_read_text(path))
            # 这份 CUE 不可信时，改用**这一碟目录下**的音频文件名 ——
            # 它们通常是完好的原文。两种不可信：
            #   ① 被西欧码页写坏，TRACK TITLE 整体退化成点号
            #      （"唇にBe My Baby" → "..Be My Baby"）；
            #   ② 码页不在 LOG_ENCODINGS 里，被 gb18030 兜底解成假汉字
            #      （Shift-JIS "あなたに夢中" → "偁側偨偵柌拞"）。
            reason = ""
            if _degenerate_ratio(titles) >= DEGENERATE_RATIO:
                reason = "点号退化（码页表示不了中日文）"
            elif _mojibake_ratio(titles) >= MOJIBAKE_RATIO:
                reason = "假汉字（码页不在 LOG_ENCODINGS，被 gb18030 兜底解坏）"
            if reason:
                names = _audio_filenames_in(os.path.dirname(path))
                if names:
                    print(f"    ↩️  {os.path.basename(path)} 曲目不可信（{reason}），"
                          f"回退到音频文件名")
                    titles = names
            if not titles:
                continue
            if multi:
                head = heads[idx - 1]
                blocks.append(f"【{head}】\n" + "\n".join(titles))
            else:
                blocks.append("\n".join(titles))
        if blocks:
            return "\n".join(blocks)

    # 没有可用 CUE 曲目 → 音频文件名兜底
    return _audio_filenames_tracklist(folder_path)


def has_cue(folder_path):
    if not folder_path or not os.path.exists(folder_path):
        return False
    if os.path.isfile(folder_path):
        folder_path = os.path.dirname(folder_path)
    for _, _, files in os.walk(folder_path):
        if any(f.lower().endswith(".cue") for f in files):
            return True
    return False
