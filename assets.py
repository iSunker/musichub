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
            # CUE 被西欧码页写坏时 TRACK TITLE 会整体退化成点号
            # （"唇にBe My Baby" → "..Be My Baby"）。这种曲目列表站点会打回，
            # 改用**这一碟目录下**的音频文件名 —— 它们通常是完好的原文。
            if _degenerate_ratio(titles) >= DEGENERATE_RATIO:
                names = _audio_filenames_in(os.path.dirname(path))
                if names:
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
