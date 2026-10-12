"""
本地资源提取：封面图查找与图床上传、抓轨日志读取、CUE 曲目解析。

run.py 里这些逻辑原本是内联的，backfill_covers.py 也需要同一套（补历史缺失的封面），
所以抽出来共用，避免两处实现慢慢跑偏。
"""
import json
import os
import re
from datetime import datetime

import requests

import config

# ★ 2026-10-09：最近一次 `upload_to_lsky` 失败的**原始原因串**（成功时清空）。
#   ★ 用途：让调用方（`publish._crabpt_cover`）区分「本条封面的问题」（如超大小上限）
#     与「设施级问题」（token/配额/网络）—— 前者跳过本条、后者停整轮。
#     原先只有 `print`，调用方拿不到原因 ⇒ 只能一律当"设施坏"⇒ 一条坏封面卡死整队。
LAST_LSKY_ERROR = ""

# 单条级（item）的失败特征：换个更小的图/补一张封面就能过，**重试无效**。
#   ★ 命中这些 ⇒ 属「本条封面不可用」⇒ 跳过该条，**不停整轮**。
_ITEM_LEVEL_LSKY_HINTS = (
    "图片大小超出限制", "超出限制", "size", "too large", "文件过大", "格式",
    "不支持", "mime", "invalid image", "不是有效",
)


def lsky_error_is_item_level(err=None):
    """`upload_to_lsky` 的失败原因是否属**单条级**（该条种子/这张图自身的问题）。

    True  ⇒ 只这一条过不去（重试无效）⇒ 调用方**跳过本条、继续下一条**。
    False ⇒ 设施级（token/配额/网络/403/429/超时…）⇒ 调用方**停整轮**（退避后再来）。
    """
    s = (LAST_LSKY_ERROR if err is None else err) or ""
    low = s.lower()
    return any(h.lower() in low for h in _ITEM_LEVEL_LSKY_HINTS)


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

# ★★★ 2026-10-10：Lsky（蟹黄堡专用图床）的**单图上限目标**（仅用于"传前压图"）。
#
#   ★ 为什么另开一个常量、**不动** `MAX_COVER_BYTES`：
#     两者管**不同阶段**、语义不同（同一个值会漂移成两个意思，`ERR-META-13`）：
#       · `MAX_COVER_BYTES`（15 MiB）= **pick/预检**阶段的地板：太离谱的原始扫图
#         干脆不选，别拿去送死。
#       · `LSKY_MAX_BYTES`（本项）= **上传**阶段的目标：**超了就压到它以下再传**
#         （而不是跳过）。压图是"尽力救回"，不是"再设一道墙"。
#
#   ★ 实测（2026-10-10，用当前 NAS 的 Lsky token 打 `/api/v1/upload`）：
#     · 15.6 MB 的 `1e39f05940b6.jpg` ⇒ `{"status":false,"message":"图片大小超出限制"}`
#     · 0.7 MB 的图 ⇒ `{"status":true,"message":"上传成功"}`
#     ⇒ 上限落在 (0.7MB, 15.6MB) 之间。用户 2026-10-10 拍板取 **5 MB** 作目标值
#       （Lsky Pro 常见默认 5MB；压到 5MB 以下足以覆盖绝大多数封面）。
#   ★ 可被 `.env` 的 `LSKY_MAX_BYTES` 覆盖（运维按本站真实上限调）。
LSKY_MAX_BYTES = int(os.getenv("LSKY_MAX_BYTES", "") or (5 * 1024 * 1024))
# 压图尺寸上限（最长边，像素）：多半用不到那么细，但给个上限防止"降质量也降不下来"。
LSKY_SHRINK_MAX_DIM = 2400
# 压缩后文件的**后缀**（与 `cover_fetch._cached_file` 的 `<sha1>.<ext>` 命名区分开，
# 免得"原图缓存"与"压过的图"互相覆盖）。
LSKY_SHRINK_SUFFIX = ".lsky.jpg"

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


def _lsky_base():
    """Lsky 接口 base（`https://img.crabpt.vip/api/v1`），去尾斜杠。"""
    return (config.LSKY_API_URL or "").rstrip("/")


def lsky_token_path():
    """token 落盘路径。★ 默认值在这里算 —— `config.LSKY_TOKEN_FILE` 的默认值
    留空是**刻意**的（`config.py` 里 `DATA_DIR` 定义在它后面，那里拼会
    `NameError`，见那条注释）。"""
    p = (config.LSKY_TOKEN_FILE or "").strip()
    if p:
        return p
    return os.path.join(config.DATA_DIR, "lsky_token.json")


def lsky_load_token():
    """读**缓存**的 token（上次换来的）。★ 读不到一律返 `""`，绝不抛。

    ★★ 为什么只认 `token` 字段、不认别的：
      这是**我们的**文件（自己写的），格式由 `lsky_save_token` 定死。
      多认一种形状 = 以后换个写法也"能读"⇒ 坏了看不出来（`ERR-META-14`）。
    """
    p = lsky_token_path()
    if not os.path.exists(p):
        return ""
    try:
        with open(p, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"    [Lsky token] 缓存读不出（{type(e).__name__}: {e}）⇒ 当作没有")
        return ""
    t = d.get("token") if isinstance(d, dict) else None
    return t.strip() if isinstance(t, str) else ""


def lsky_save_token(token):
    """落盘 token（原子替换）。★ **返回值表示"存住了没有"**，不是"token 对不对"。"""
    if not (token or "").strip():
        return False
    p = lsky_token_path()
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            # ★ 只存 token，**不存**邮箱/密码 —— 那两个在 `.env` 里，
            #   多存一份 = 多一个泄漏面，而它换来的好处是零。
            json.dump({"token": token.strip(),
                       "obtained_at": datetime.now().isoformat(timespec="seconds")},
                      fh, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        try:
            os.chmod(p, 0o600)          # ★ 凭据文件：只给主人读写
        except OSError:
            pass
        return True
    except OSError as e:
        print(f"    [Lsky token] 写不进 {p}：{type(e).__name__}: {e}")
        return False


def lsky_login(email=None, password=None, api_url=None, timeout=30):
    """用**账号密码换 token**（`POST <base>/tokens`）。返回 `(token, 说明)`。

    ★★ 逐字对齐站点文档（请求体字段 `email` / `password`，响应 `data.token`）：
      不另起字段名 —— 下次有人对文档核时时能一眼对上。

    ★★★ 为什么必须有这个函数（本次实测的根因，别删掉这段）：
      `.env` 里那个 714 字符、以 `XSRF-TOKEN=` 开头的值**不是** token，
      而是**浏览器 Cookie 串**。它"看起来配好了"（键在、非空、很长），
      打上传接口却回 **401**。★ 而 `upload_to_lsky` 只在**真发布那一刻**
      才用它 ⇒ 这个错一直**潜伏**着，直到"要发三条"时才炸。
      ⇒ 人手从网页复制凭据这条路上**根本没有判据**；把换 token 做成
        一步**可执行**的动作之后，凭据对了就是对了、错了当场报 401。

    ★ 成功判据**只有一条**：`HTTP 200` **且** `status` 为真 **且** `data.token` 非空。
      Lsky 在业务失败时也回 200（`status:false` + `message`）⇒ 只看状态码会把
      「登录被拒」读成「成功」（`ERR-META-01`：检测方法失效比不检测更危险）。
    ★★ token **绝不打印**（同 `guard_auto_key` 的纪律：值不进日志，
      而 `docker logs` 是本项目最容易被抄走的那份）。`说明` 里只出现长度与来源。
    """
    email = config.LSKY_EMAIL if email is None else email
    password = config.LSKY_PASSWORD if password is None else password
    if not (email or "").strip() or not (password or "").strip():
        return "", ("没配 LSKY_EMAIL / LSKY_PASSWORD ⇒ **无法换 token**。"
                    "★ 见 ERR-ENV-02：新增的 .env 键要在 **NAS 上手工加**。")

    url = f"{(api_url or _lsky_base())}/tokens"
    try:
        res = requests.post(
            url,
            json={"email": email.strip(), "password": password},
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
    except Exception as e:                                  # noqa: BLE001
        return "", f"换 token 请求异常：{type(e).__name__}: {e}"

    if res.status_code == 401:
        return "", ("HTTP 401 —— 邮箱或密码不对（★ 注意：这是**登录**被拒，"
                    "与'上传时 token 不对'是**两件事**，别混）")
    if res.status_code == 403:
        return "", "HTTP 403 —— 管理员关闭了接口功能"
    if res.status_code == 429:
        return "", "HTTP 429 —— 超出接口请求配额（★ 别立刻重试，会继续扣）"
    if res.status_code != 200:
        return "", f"HTTP {res.status_code} —— {res.text[:150]}"
    try:
        d = res.json()
    except ValueError:
        return "", f"HTTP 200 但响应不是 JSON：{res.text[:150]}"
    if not d.get("status"):
        return "", f"status=false：{str(d.get('message'))[:150]}"
    tok = ((d.get("data") or {}) if isinstance(d.get("data"), dict) else {}).get("token")
    if not (tok or "").strip():
        return "", f"响应里没有 data.token：{res.text[:150]}"
    return tok.strip(), f"已换到新 token（长度 {len(tok.strip())}）"


def lsky_get_token(force=False, allow_login=True, api_url=None):
    """拿一个**能用的** token，按「缓存 → 换 → .env 里的静态值」顺序。返回 `(token, 说明)`。

    ★★★ 优先级**为什么是这个顺序**（每一条都是踩出来的，别改）：
      ① **缓存文件**：换 token 会扣**接口配额**（站点文档 429 = 超出请求配额）。
         每次上传都换一次 = 白扣。⇒ 先看上次换的还在不在。
      ② **换新的**（`lsky_login`）：只有配了邮箱/密码才走得到。
      ③ **`.env` 里的 `LSKY_TOKEN`**：★ 放**最后**。它是"人手抄的值"，
         而本次实测证明人手抄的可以是**完全不相干的东西**（Cookie 串）。
         ⇒ 它是**兜底**，不是首选 —— 但**保留**它，因为有人可能确实手工配好了
           （那时不该因为我们多了个换 token 的路就不认它）。
      ④ 都没有 ⇒ 返 `("", 原因)`，调用方据此**不发布**。

    ★★ `force=True` 的含义是"**跳过缓存**"，**不是**"跳过登录" ——
      换 token 的第一因是"缓存 401 了"，那时要的正是重新换。
    ★ 但 `allow_login=False` 时**只读缓存/静态值**，绝不发网络请求
      （给"只想看看配没配"的诊断用，且它是**零配额**的判据）。
    """
    if not force:
        cached = lsky_load_token()
        if cached:
            return cached, f"用缓存 token（{lsky_token_path()}，长度 {len(cached)}）"
    if allow_login:
        tok, why = lsky_login(api_url=api_url)
        if tok:
            if lsky_save_token(tok):
                return tok, why + f"；已落盘 {lsky_token_path()}"
            return tok, why + "（★ 落盘失败，本次仍可用，但下次还要再换）"
        # ★ 换失败**不立刻放弃** —— 下面还有静态值那条兜底。
        #   但要**说清**它失败了，否则"兜底顶上了"会被读成"换成功了"。
        print(f"    [Lsky token] 换 token 没成功：{why}")
    static = (config.LSKY_TOKEN or "").strip()
    if static:
        return static, f"用 .env 里的 LSKY_TOKEN（长度 {len(static)}）—— ★ 兜底路径"
    return "", "既没有缓存 token、也换不到、`.env` 里的 LSKY_TOKEN 也是空"


def _shrink_for_lsky(image_path, max_bytes=None, max_dim=None):
    """把一张**过大**的封面压到 `max_bytes` 以下，返回**可上传的本地路径**或 `""`。

    ★★ 为什么要它（2026-10-10）：蟹黄堡专用图床 Lsky 有单图大小上限，而本地封面
      缓存里可能是 **15MB 级的原始扫图**（实测 `1e39f05940b6.jpg` = 15,593,260 字节）
      ⇒ 直接传被 `图片大小超出限制` 拒收 ⇒ `CoverItemError` ⇒ 该条发不出去。
      ⇒ 与其"跳过这条"，不如**当场压小再传**（用户 2026-10-10 拍板：压图，别送死）。

    ★★ 返回值语义（调用方据此分流，**别混**）：
      · 返回**非空路径**  ⇒ 可上传（可能是原图，也可能是压过的那份）。
      · 返回 `""`        ⇒ **压也压不下去**（连缩放+最低质量都超 `max_bytes`）
                            ⇒ 调用方当"本条封面不可用"处理（item 级，跳过本条）。

    ★★★ 三条铁律：
      ① **绝不覆盖原图**：压过的图写到一个**独立后缀**文件（`<原路径>.lsky.jpg`）。
         原图是**乌托邦那份**的来源（`ERR-META-13`：一个值在两个位置含义不同），
         覆盖它 = 把乌托邦页面上的封面也换掉了。
      ② **不联网**：只用本地已有文件重编码，不重新下载（下载由 `cover_fetch` 负责）。
      ③ **能不动就不动**：`size <= max_bytes` ⇒ **原样返回**，不重编码
         （避免无谓的二次有损压缩，也省 CPU）。

    ★ 依赖 Pillow。★ 若 Pillow **不可用**（旧镜像还没重建）⇒ 本函数**如实降级**：
      不假装压成功，而是把原图原样返回（让上传去撞 Lsky 的墙、得到真实报错），
      并打印一条**明确**的"Pillow 缺失、压图未生效"提示（`ERR-META-14`：兜底不许冒充结果）。
    """
    if not image_path or not os.path.exists(image_path):
        return ""
    max_bytes = LSKY_MAX_BYTES if max_bytes is None else max_bytes
    max_dim = LSKY_SHRINK_MAX_DIM if max_dim is None else max_dim

    try:
        size = os.path.getsize(image_path)
    except OSError:
        return image_path            # 拿不到大小 ⇒ 交给上传去报真实错

    # ③ 没超 ⇒ 原样用（零开销，不重编码）。
    if size <= max_bytes:
        return image_path

    try:
        from PIL import Image            # noqa: PLC0415  (延迟导入：旧镜像可能没有)
    except Exception:                    # noqa: BLE001
        print(f"    [压图] ★ Pillow 不可用（镜像未重建？）⇒ **压图未生效**，"
              f"原图 {size} 字节 > 上限 {max_bytes} 字节，仍按原样上传（会撞 Lsky 的墙）")
        return image_path

    # 压过的图写到独立文件（① 不覆盖原图）。
    out_path = image_path + LSKY_SHRINK_SUFFIX
    # 已压过且仍然 ≤ 上限 ⇒ 直接复用（幂等，省一轮重编码）。
    try:
        if os.path.exists(out_path) and os.path.getsize(out_path) <= max_bytes:
            return out_path
    except OSError:
        pass

    try:
        with Image.open(image_path) as im:
            # EXIF 方向要先应用，否则压完可能横竖颠倒。
            try:
                from PIL import ImageOps
                im = ImageOps.exif_transpose(im)
            except Exception:            # noqa: BLE001
                pass
            # 缩放到最长边 ≤ max_dim（只缩不放）。
            w, h = im.size
            longest = max(w, h)
            if longest > max_dim:
                scale = max_dim / float(longest)
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                               Image.LANCZOS)
            # ★ 统一转 RGB 再存 JPEG（PNG/带 alpha 的图直接存 JPEG 会报错）。
            rgb = im.convert("RGB")
            # 逐级降质量，直到 ≤ 上限或到底。
            for q in (92, 88, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40):
                rgb.save(out_path, format="JPEG", quality=q, optimize=True)
                if os.path.getsize(out_path) <= max_bytes:
                    print(f"    [压图] {os.path.basename(image_path)} "
                          f"{size} → {os.path.getsize(out_path)} 字节 (q={q}) ⇒ {out_path}")
                    return out_path
    except Exception as e:               # noqa: BLE001
        print(f"    [压图] 重编码失败 {type(e).__name__}: {e}；原图 {image_path}")
        return ""

    # 连最低质量都压不下去 ⇒ 如实返空（调用方当"本条不可用"）。
    try:
        _final = os.path.getsize(out_path)
    except OSError:
        _final = -1
    print(f"    [压图] ★ 压到最低仍超上限（{_final} > {max_bytes}）⇒ 放弃本条")
    return ""


def upload_to_lsky(image_path, retries=2, api_url=None, token=None,
                   allow_retoken=True):
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

    ★★★ 2026-10-10：上传**之前**先 `_shrink_for_lsky()` 把过大的封面压到
      `LSKY_MAX_BYTES` 以下（用户拍板：压图，别让 15MB 级扫图送死）。
      · 压过 ⇒ 传的是压过的那份（原图不动）。
      · 压不下去（返 `""`）⇒ 设 `LAST_LSKY_ERROR` 为 **item 级**文案后返 `""`
        ⇒ 调用方（`publish._crabpt_cover`）据此抛 `CoverItemError` ⇒ 跳过本条、
        继续下一条（不是设施级、不停整轮）。
    """
    # ★ `global` 必须出现在函数体**任何对该名的引用之前**（Python 语法）⇒ 提到最前，
    #   后面（压图早返分支 + 底部失败分支）都复用这一处声明。
    global LAST_LSKY_ERROR

    if not image_path or not os.path.exists(image_path):
        return ""

    # ★★ 传前压图（2026-10-10）。★ 放在**取 token 之前**：压图是纯本地操作，
    #   不该为了一张注定传不上的图去换 token / 扣配额。
    shrunk = _shrink_for_lsky(image_path)
    if not shrunk:
        # 压到最低仍超上限 ⇒ item 级失败（这张图本身太大）。
        # ★ 必须**显式**设 LAST_LSKY_ERROR：调用方 `assets.lsky_error_is_item_level()`
        #   靠它分流（"跳过本条" vs "停整轮"）。
        LAST_LSKY_ERROR = (f"图片大小超出限制：压到最低仍 > {LSKY_MAX_BYTES} 字节"
                           f"（本地图 {image_path}）")
        print(f"    [Lsky 图床] {os.path.basename(image_path)} -> {LAST_LSKY_ERROR}")
        return ""
    # 压过了就传压过的那份（原图不动）；没压过时 `shrunk == image_path`（零开销）。
    image_path = shrunk

    # ★ `token_arg` 记下"调用方**有没有**显式传" —— 这个区分**必须**留着：
    #   显式传时我们不能替他换 token（会让传进来的值静默失效），
    #   而 `token` 变量后面会被 `lsky_get_token()` 的返回值覆盖 ⇒ 分不清了。
    token_arg = token
    # ★★ token 的来源：**传进来的** > 缓存 > **换** > `.env` 静态值。
    #   ★ 2026-09-27 改：原先直接读 `config.LSKY_TOKEN`，而那个值实测是个
    #     **Cookie 串**（见 `lsky_login` 的 docstring）⇒ 401 且一路沉默到发布那天。
    #     ⇒ 现在默认走 `lsky_get_token()`（它会**换**一个），
    #       而"人手抄的值"退到**兜底**位置 —— 保留它，但不指望它。
    #   ★ 显式传 `token=` 时**不碰网络**（调用方已经自己解决了，别白扣配额）。
    if token is None:
        token, tok_why = lsky_get_token(api_url=api_url)
        print(f"    [Lsky 图床] {tok_why}")
    else:
        token = (token or "").strip()
    if not (token or "").strip():
        print("    [Lsky 图床] 拿不到可用 token ⇒ **不上传**。"
              "★ 两条修法：① 往 NAS 的 `.env` 加 `LSKY_EMAIL`/`LSKY_PASSWORD`"
              "（推荐，程序自己换）；② 或把接口页的 token 填进 `LSKY_TOKEN`。"
              "见 ERR-ENV-02。")
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
                # ★★ 缓存 token 过期 ⇒ **当场重换一次**（2026-09-27 加）。
                #   ★ 为什么值得单独一条路：缓存文件的存在就是为了省配额，
                #     而它**必然**会过期。没有这条，"缓存过期"的表现是
                #     "蟹黄堡永远发不出去、而日志里只有一句 401"
                #     —— 每次都要人去 NAS 手工删文件才恢复。
                #   ★ 只在**没有显式传 token** 时才换：显式传 = 调用方自己管，
                #     我们替他换会让他传进来的值静默失效（`ERR-META-13` 形状）。
                if (token_arg is None and attempt == 0 and allow_retoken
                        and lsky_load_token()):
                    new_tok, why = lsky_login(api_url=api_url)
                    if new_tok:
                        lsky_save_token(new_tok)
                        token = new_tok
                        headers["Authorization"] = f"Bearer {new_tok}"
                        last_err = f"401 ⇒ 已重换 token（{why}）并重试"
                        continue
                    last_err = ("HTTP 401 授权失败，且**重换也失败** —— "
                                f"{why}。★ 若没配 `LSKY_EMAIL`/`LSKY_PASSWORD`，"
                                f"就只能去接口页生成 token 填 `LSKY_TOKEN`"
                                f"（NAS 的 .env）")
                else:
                    last_err = ("HTTP 401 授权失败 —— token 不对或已失效。"
                                "★ 优先修法：往 NAS 的 `.env` 加 "
                                "`LSKY_EMAIL`/`LSKY_PASSWORD`（程序自己换）；"
                                "或去蟹黄堡「接口」页生成 token 填 `LSKY_TOKEN`"
                                "（deploy.sh 不带 .env，见 ERR-ENV-02）")
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
    LAST_LSKY_ERROR = last_err
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


# ==================== Lsky token 链路的自检 ====================
#
# ★★ 为什么给这一段写自检（`ERR-META-31` 的形状）：
#   本次实测的根因是「`.env` 里的凭据**看起来配好了**，实际是个 Cookie 串」。
#   那种错**没有任何判据会报** —— 键在、值非空、长度可观，只在**真发布那一刻**
#   才以 401 现形。⇒ 光把代码改对不够，要留一条判据让"链路断了"以后也能看出来。
#
# ★ 判据分两层：
#   ① **离线**（不发网络请求）：类名/字段名/成功判据是否与站点文档对齐，
#      以及 `lsky_get_token` 的**优先级顺序**（缓存 > 换 > 静态值）对不对。
#      —— 这一层能跑在任何地方，包括本地和容器里。
#   ② **联网**（只有配了凭据才跑）：真去换一次 token，验 HTTP 200 + status:true。
#      —— 这一层**会扣接口配额**，所以默认**不跑**，要 `--online` 显式要。
def selftest(online=False):
    """Lsky token 链路自检。返回 `(是否通过, 明细列表)`。"""
    import inspect

    detail = []
    ok = True

    def check(name, cond, bad_msg):
        nonlocal ok
        if not cond:
            ok = False
            detail.append(bad_msg)
        return cond

    # ---- ① 文档对齐：字段名/端点/成功判据 ----
    src_login = inspect.getsource(lsky_login)
    check("login.json.email", '"email"' in src_login,
          "★ lsky_login 的请求体里没有 `email` 字段 ⇒ 与站点文档对不上")
    check("login.json.password", '"password"' in src_login,
          "★ lsky_login 的请求体里没有 `password` 字段 ⇒ 与站点文档对不上")
    check("login.endpoint", "/tokens" in src_login,
          "★ lsky_login 没打 `POST <base>/tokens` ⇒ 端点与文档不符")
    # ★ 成功判据必须**同时**认 200、status、data.token —— 少一个就会把
    #   "登录被拒"读成"成功"（Lsky 业务失败也回 200）。
    check("login.judge.200", "res.status_code != 200" in src_login,
          "★ lsky_login 没有校验 HTTP 200 ⇒ 会把错误响应当成功")
    check("login.judge.status", 'd.get("status")' in src_login,
          "★ lsky_login 没有校验 `status` ⇒ Lsky 业务失败也回 200，会误判成功")
    check("login.judge.token", '"token"' in src_login,
          "★ lsky_login 没有取 `data.token` ⇒ 换到了也拿不出来")

    src_upload = inspect.getsource(upload_to_lsky)
    # ★★★ 这几条**不能**用 `in src_upload`（整段源码含**注释与文档串**）——
    #   实测踩到：把真正的 `f"Bearer {token}"` 改成不带前缀之后，判据**照样通过**，
    #   因为文档串里那句「`Authorization: Bearer <token>`」和重换 token 那条路
    #   里也有 `Bearer ` ⇒ 子串搜索命中了**别的地方**。
    #   ★ 这是本条判据**第一版就是假的**的证据（阴性对照 RC=0）。
    #   ⇒ 拿 **AST 里的真实调用/字面量**来判，不看源码文本。
    # ★ 做法：找出 `upload_to_lsky` 里构造 `headers` 的那个 dict 字面量，
    #   直接看它的键值（这才是**真正发出去**的东西）。
    _hdr = None
    for node in __import__("ast").walk(__import__("ast").parse(src_upload)):
        if isinstance(node, __import__("ast").Assign):
            for t in node.targets:
                if getattr(t, "id", "") == "headers" and isinstance(node.value, __import__("ast").Dict):
                    _hdr = node.value
    check("upload.headers_found", _hdr is not None,
          "★ 在 `upload_to_lsky` 里找不到构造 `headers` 的字面量 ⇒ "
          "本条判据**没法执行**（不是通过）")
    auth_val, accept_val = "", ""
    if _hdr is not None:
        for k, v in zip(_hdr.keys, _hdr.values):
            ks = getattr(k, "value", None)
            if ks == "Authorization":
                # f-string 里的常量片段用 `JoinedStr.values` 取
                auth_val = __import__("ast").unparse(v)
            elif ks == "Accept":
                accept_val = getattr(v, "value", "") or ""
    check("upload.bearer",
          "Bearer " in auth_val,
          f"★ upload_to_lsky 真正发出的 `Authorization` 值（{auth_val!r}）里"
          f"**没有** `Bearer ` 前缀 ⇒ 与接口文档不符")
    check("upload.accept",
          accept_val == "application/json",
          f"★ upload_to_lsky 的 `Accept` 头是 {accept_val!r}，"
          f"而文档标注 `application/json` 为**必传**")
    check("upload.field", '"file"' in src_upload or "'file'" in src_upload,
          "★ upload_to_lsky 的文件字段名不是 `file` ⇒ 与文档不符")

    # ---- ② 优先级顺序：**这是本次真正的修复点** ----
    # ★ 判据取自源码的**调用顺序**（AST 拿语句序号），不是手抄一个期望值。
    #   ★★ 为什么这条最要紧：如果"静态值"排在"换 token"**前面**，
    #     那个 Cookie 串会被优先用上 ⇒ 401 ⇒ **修复等于没做**，
    #     而且从日志上看和"没改过"一模一样。
    try:
        tree = __import__("ast").parse(inspect.getsource(lsky_get_token))
        order = []
        for node in __import__("ast").walk(tree):
            if isinstance(node, __import__("ast").Call):
                fn = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
                if fn in ("lsky_load_token", "lsky_login"):
                    order.append((node.lineno, fn))
        order.sort()
        seq = [f for _l, f in order]
        # ★★★ 下面这两行我第一版写错了，而错法**正是本条判据要防的东西**
        #   （`ERR-META-01`：判据算错了，却仍然"通过"）：
        #     `seq.index(...)` 返回的是**下标**，而我拿它去和**行号**比大小。
        #     下标 1 vs 行号 23/35 ⇒ `early` 恒为空 ⇒ 判据**永远通过**。
        #     阴性对照实测：注入根因形状后 RC 仍是 0。
        #   ⇒ 必须取**行号**，不是下标。
        login_lines = [ln for ln, f in order if f == "lsky_login"]
        cache_lines = [ln for ln, f in order if f == "lsky_load_token"]
        first_login = login_lines[0] if login_lines else -1
        first_login_idx = seq.index("lsky_login") if "lsky_login" in seq else -1
        check("order.login_called", bool(login_lines),
              "★★ `lsky_get_token` 里**没有**调 `lsky_login` ⇒ 换 token 这条路断了"
              "（`.env` 里的错值又会成为唯一来源）")
        check("order.cache_before_login",
              bool(cache_lines) and bool(login_lines) and cache_lines[0] < login_lines[0],
              "★★ `lsky_get_token` 里**缓存没有排在换 token 前面** ⇒ "
              "每次上传都换一次，会白扣接口配额（文档里 429 就是配额）")
        # ★ 静态值必须**最后**：它出现的位置要在 lsky_login 的**后面**。
        #   ★★★ 判据**不能**只看"某个 `LSKY_TOKEN` 出现的行号 > lsky_login"
        #     —— 我第一版就是这么写的，而它**测不出真东西**：
        #     把静态值提到 lsky_login **前面**并 `return`，源码里**另一处**
        #     （末尾那句兜底）行号仍然大于 lsky_login ⇒ 判据照样通过
        #     （阴性对照实测：注入根因形状后 RC=0）。**判据失效比没有判据更危险**。
        #   ⇒ 真正要测的是「**在 `lsky_login` 之前，有没有一条路径会提前 return 静态值**」。
        #     取 `lsky_get_token` 里**所有** `LSKY_TOKEN` 的读取点，
        #     要求**每一个**都排在 `lsky_login` **之后**。
        static_lines = [n.lineno for n in __import__("ast").walk(tree)
                        if isinstance(n, __import__("ast").Attribute)
                        and getattr(n, "attr", "") == "LSKY_TOKEN"]
        check("order.static_read_exists", bool(static_lines),
              "★★ `lsky_get_token` 里**没有**读 `.env` 的 `LSKY_TOKEN` ⇒ "
              "兜底路径丢了（有人手工配好 token 时会被无视）")
        early = [ln for ln in static_lines if first_login != -1 and ln < first_login]
        check("order.no_early_static",
              not early,
              f"★★ `.env` 的静态 `LSKY_TOKEN` 在**第 {early} 行**被读，"
              f"而换 token 在**第 {first_login} 行** "
              f"⇒ 静态值**优先**于换 token ⇒ 人手抄的错值会被先用上"
              f"（**本次 401 的根因**，修复等于没做）")
    except SyntaxError as e:
        ok = False
        detail.append(f"★ 解析 `lsky_get_token` 源码失败：{e} ⇒ 本条判据**没能执行**（不是通过）")

    # ---- ③ 缓存往返（不联网）----
    try:
        p = lsky_token_path()
        old = None
        if os.path.exists(p):
            old = open(p, "rb").read()
        sentinel = "SELFTEST-NOT-A-REAL-TOKEN"
        if lsky_save_token(sentinel):
            got = lsky_load_token()
            check("cache.roundtrip", got == sentinel,
                  f"★ token 缓存**存了读不回**（存 {sentinel!r} 读到 {got!r}）⇒ "
                  f"缓存形同虚设")
            check("cache.file.hides_login", os.path.exists(p),
                  "★ 缓存文件没落盘")
            # ★ 缓存文件里**不许**出现密码（凭据最小化）
            try:
                body = open(p, encoding="utf-8").read()
                check("cache.no_password",
                      "password" not in body.lower(),
                      "★★ token 缓存文件里出现了 password 字段 ⇒ "
                      "多存了一份凭据（`lsky_save_token` 只该存 token）")
            except OSError:
                pass
        else:
            ok = False
            detail.append(f"★ `lsky_save_token` 写不进 {p} ⇒ 缓存链路断了")
        # 还原
        if old is None:
            try:
                os.remove(p)
            except OSError:
                pass
        else:
            with open(p, "wb") as fh:
                fh.write(old)
    except Exception as e:                                  # noqa: BLE001
        ok = False
        detail.append(f"★ 缓存往返测试抛了异常：{type(e).__name__}: {e}")

    # ---- ④ 联网（默认跳过；会扣配额）----
    if online:
        if not (config.LSKY_EMAIL or "").strip():
            print("   ⏭️  跳过联网检查：没配 LSKY_EMAIL")
        else:
            tok, why = lsky_login()
            check("online.login", bool(tok), f"★ 联网换 token 失败：{why}")
            if tok:
                print(f"   ✅ 联网换 token 成功：{why}")
    else:
        print("   ⏭️  联网检查默认跳过（会扣接口配额）。要跑加 `--online`。")

    if not detail:
        detail.append(f"全部通过（{'含' if online else '不含'}联网检查）")
    return ok, detail


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    _online = "--online" in sys.argv
    _ok, _detail = selftest(online=_online)
    print(f"Lsky token 链路自检：{'✅ 通过' if _ok else '❌ 失败'}")
    for d in _detail:
        print(f"  {d}")
    sys.exit(0 if _ok else 1)
