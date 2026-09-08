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


def read_log(folder_path):
    """
    找并读取抓轨日志（.log）。EAC / dBpoweramp / XLD 导出的编码不统一，
    按常见编码依次尝试，全失败则忽略无法解码的字节兜底。
    返回 (content, filename)。
    """
    if not folder_path or not os.path.exists(folder_path):
        return "", ""

    if os.path.isfile(folder_path):
        folder_path = os.path.dirname(folder_path)

    for root, _, files in os.walk(folder_path):
        for f in files:
            if not f.lower().endswith(".log"):
                continue
            path = os.path.join(root, f)
            for enc in LOG_ENCODINGS:
                try:
                    with open(path, "r", encoding=enc) as fh:
                        return fh.read(), f
                except (UnicodeDecodeError, UnicodeError):
                    continue
                except Exception as e:
                    print(f"    ⚠️  读取日志 {path} 出错: {e}")
                    return "", f
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    return fh.read(), f
            except Exception as e:
                print(f"    ⚠️  读取日志 {path} 彻底失败: {e}")
                return "", f
    return "", ""


def read_tracklist(folder_path):
    """从 CUE 里抽曲目名。返回换行分隔的字符串。"""
    if not folder_path or not os.path.exists(folder_path):
        return ""

    if os.path.isfile(folder_path):
        folder_path = os.path.dirname(folder_path)

    for root, _, files in os.walk(folder_path):
        for f in files:
            if not f.lower().endswith(".cue"):
                continue
            path = os.path.join(root, f)
            for enc in LOG_ENCODINGS:
                try:
                    with open(path, "r", encoding=enc) as fh:
                        lines = fh.readlines()
                    break
                except (UnicodeDecodeError, UnicodeError):
                    continue
                except Exception:
                    return ""
            else:
                continue

            tracks = []
            for line in lines:
                s = line.strip()
                if s.upper().startswith("TITLE") and "FILE" not in s.upper():
                    tracks.append(s)
            if tracks:
                return "\n".join(tracks)
    return ""


def has_cue(folder_path):
    if not folder_path or not os.path.exists(folder_path):
        return False
    if os.path.isfile(folder_path):
        folder_path = os.path.dirname(folder_path)
    for _, _, files in os.walk(folder_path):
        if any(f.lower().endswith(".cue") for f in files):
            return True
    return False
