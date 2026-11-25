# -*- coding: utf-8 -*-
"""MusicHub 3.0 统一封面与图床服务 (CoverImageService)

封面策略（2026-11-21 用户拍板，移植父项目 `opencd2others` 的封面链路）：

1. **源站有图且图链有效 ⇒ 直接复用源站图链**，不下载、不上传、不消耗图床配额。
2. **仅当源站无图或图链失效** 时：扫描本地种子目录找封面 → 按目标站规定上传图床。
3. 上传结果写入 `cover_cache` 表（专辑 × 站点），同一专辑给别站发种时直接查库复用。
4. **站点专用图床优先**：
   - `crabpt` 必须 Lsky Pro（`img.crabpt.vip`），绝不透传 PicGo 链；
   - `pterclub` 配了 `PTER_IMG_API_KEY` 走猫站图床，否则有效公网链 / PicGo；
   - 其余站点走 PicGo（Chevereto API v1）。
5. **失败绝不写空 URL**（避免抹掉站点已有封面），失败原因落 `reason`。

复用自父项目的核心实现：
- `assets.COVER_PATTERNS` / `assets.find_cover`（分级匹配 + 最大图兜底）
- `cover_fetch.image_ext`（按魔数识别图片，不信 URL 后缀）
- `assets.upload_to_chevereto` / `upload_to_picgo` / `upload_to_pter_img`
- `assets.upload_to_lsky` / `_shrink_for_lsky` / `LAST_LSKY_ERROR` / `lsky_error_is_item_level`
"""

import os
import re
import json
import time
import hashlib
import logging
from typing import Optional, Tuple, Dict, Any, List
from urllib.parse import urlparse

import requests

logger = logging.getLogger("CoverImageService")

# ==================== 常量（对齐父项目 assets.py / cover_fetch.py） ====================

# 选图/下载阶段允许的原始扫图上限（50MB）；上传阶段超图床上限会自动压缩
MAX_COVER_BYTES = 50 * 1024 * 1024

# PicGo / Chevereto 上传上限（默认 10MB，超限自动压缩到 cache_dir 后再传）
PICGO_MAX_BYTES = int(os.getenv("PICGO_MAX_BYTES", "") or (10 * 1024 * 1024))

# Lsky Pro 常见默认 5MB；超过则先压图再传（不是跳过）
LSKY_MAX_BYTES = int(os.getenv("LSKY_MAX_BYTES", "") or (5 * 1024 * 1024))
LSKY_SHRINK_MAX_DIM = 2400
LSKY_SHRINK_SUFFIX = ".lsky.jpg"

# 源站图链有效性探测结果的缓存有效期（7 天）
PROBE_TTL = 7 * 24 * 3600

# 图片魔数（只看字节，不看 URL 后缀）
MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),
)

COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# 分级匹配：cover/folder/front 是通行命名，放前面优先命中
COVER_PATTERNS = [
    re.compile(r"^(cover|folder|front|frontal)\.(jpg|jpeg|png|webp)$", re.I),
    re.compile(r"(cover|folder|front)", re.I),
    re.compile(r"(artwork|poster|scan|booklet|case|inlay|box)", re.I),
    re.compile(r"(a|album|cd|disc)\d*\s*$", re.I),
]

_COVER_STEMS = ("front", "cover", "folder", "album", "poster")

# Lsky 失败分级：单条级（换图即可）vs 设施级（token/配额/网络）
LAST_LSKY_ERROR = ""
_ITEM_LEVEL_LSKY_HINTS = (
    "图片大小超出限制", "超出限制", "size", "too large", "文件过大", "格式",
    "不支持", "mime", "invalid image", "不是有效",
)


def image_ext(content: bytes) -> str:
    """从字节判断图片类型，返回扩展名或 `""`。"""
    if not content:
        return ""
    for magic, ext in MAGIC:
        if content.startswith(magic):
            if ext == ".webp" and content[8:12] != b"WEBP":
                continue
            return ext
    return ""


def sanitize_image_error(exc: Exception) -> str:
    """保留可诊断错误，同时净化 URL 中可能泄露账号的查询参数。"""
    text = re.sub(r"\s+", " ", f"{type(exc).__name__}: {exc}").strip()
    return re.sub(
        r"(?i)([?&](?:passkey|token|auth|authkey|secret|key|apikey|api_key|sid)=)[^&\s]+",
        r"\1[redacted]", text,
    )[:300]


def image_bytes_evidence(content: bytes) -> Dict[str, Any]:
    """图片内容证据：魔数 + Pillow 解码 + SHA256，不信扩展名或 URL。"""
    if not content or len(content) > MAX_COVER_BYTES or not image_ext(content[:64]):
        return {"ok": False, "error": "空内容、超过大小上限或图片魔数不符", "size": len(content or b"")}
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(content)) as im:
            image_format = str(im.format or "")
            width, height = im.size
            im.verify()
        return {
            "ok": True, "sha256": hashlib.sha256(content).hexdigest(),
            "format": image_format, "width": width, "height": height, "size": len(content),
        }
    except Exception as exc:                                      # noqa: BLE001
        return {"ok": False, "error": "Pillow 解码失败: " + sanitize_image_error(exc), "size": len(content)}


def image_file_evidence(path: str) -> Dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            content = fh.read(MAX_COVER_BYTES + 1)
    except OSError as exc:
        return {"ok": False, "error": sanitize_image_error(exc)}
    evidence = image_bytes_evidence(content)
    evidence["path"] = path
    return evidence


def lsky_error_is_item_level(err: Optional[str] = None) -> bool:
    """Lsky 失败原因是否属单条级（该条封面自身的问题，重试无效）。"""
    s = (LAST_LSKY_ERROR if err is None else err) or ""
    low = s.lower()
    return any(h.lower() in low for h in _ITEM_LEVEL_LSKY_HINTS)


class CoverImageService:
    def __init__(self, cache_dir: Optional[str] = None, db_path: Optional[str] = None):
        self.cache_dir = cache_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "data", "covers"
        )
        os.makedirs(self.cache_dir, exist_ok=True)
        self._db_path = db_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "data", "musichub.db"
        )
        self._graph = None
        self._db_disabled = False
        self._lsky_token = None
        self._probe_cache: Optional[Dict[str, Any]] = None
        self.last_lsky_error = ""

    # ==================== DB（lazy，失败软降级） ====================

    def _db(self):
        """延迟创建 RoutingGraphManager；失败只警告，封面逻辑仍可用（不落库）。"""
        if self._graph is not None or self._db_disabled:
            return self._graph
        try:
            from musichub.routing.graph import RoutingGraphManager
            self._graph = RoutingGraphManager(self._db_path)
        except Exception as e:                                  # noqa: BLE001
            logger.warning(f"封面缓存库不可用（不落库）：{type(e).__name__}: {e}")
            self._db_disabled = True
            self._graph = None
        return self._graph

    def _db_get_site_cover(
        self,
        album_key: str,
        target_site: str,
        local_dir: str = "",
        source_name: str = "",
    ) -> str:
        graph = self._db()
        if not graph or not album_key:
            return ""
        try:
            row = graph.get_cover_cache(album_key) if hasattr(graph, "get_cover_cache") else None
            if row:
                cached_dir = str(row.get("local_dir") or "").strip()
                req_dir = str(local_dir or "").strip()
                cached_name = str(row.get("source_name") or "").strip()
                req_name = str(source_name or "").strip()
                if cached_dir and req_dir and os.path.basename(cached_dir.rstrip("/\\")) != os.path.basename(req_dir.rstrip("/\\")):
                    if not (cached_name and req_name and cached_name == req_name):
                        logger.warning(
                            f"封面缓存目录不一致，拒绝复用 [{album_key} → {target_site}]: "
                            f"cached={cached_dir} vs req={req_dir}"
                        )
                        return ""
            direct = graph.get_site_cover_url(album_key, target_site)
            if direct:
                return direct

            # 若目标站属于通用图床模式（非 Lsky/猫站专线），优先复用同专辑在其他站点已上传的公共图床 URL
            mode = self._site_cover_mode(target_site)
            if row and (mode == "generic" or (mode == "pter" and not os.getenv("PTER_IMG_API_KEY", "").strip())):
                try:
                    mapping = json.loads(row.get("site_covers_json") or "{}")
                except (ValueError, TypeError):
                    mapping = {}
                if isinstance(mapping, dict):
                    # 优先选 PicGo 等公共图床链接，绝不复用私有站点图床
                    for _other_site, other_url in mapping.items():
                        u = str(other_url or "").strip()
                        if not u.startswith(("http://", "https://")):
                            continue
                        blocked, _h = self._is_blocked_private_image_url(u, target_site)
                        if not blocked and "picgo.net" in u.lower():
                            return u
                    for _other_site, other_url in mapping.items():
                        u = str(other_url or "").strip()
                        if not u.startswith(("http://", "https://")):
                            continue
                        blocked, _h = self._is_blocked_private_image_url(u, target_site)
                        if not blocked:
                            return u
            return ""
        except Exception as e:                                  # noqa: BLE001
            logger.warning(f"读封面缓存失败：{e}")
            return ""

    def _db_upsert(self, album_key: str, **fields) -> None:
        graph = self._db()
        if not graph or not album_key:
            return
        try:
            graph.upsert_cover_cache(album_key, **fields)
        except Exception as e:                                  # noqa: BLE001
            logger.warning(f"写封面缓存失败：{e}")

    def _db_set_cover(self, album_key: str, target_site: str, url: str, **fields) -> None:
        if not url:
            return
        self._db_upsert(album_key, **fields)
        graph = self._db()
        if not graph or not album_key:
            return
        try:
            graph.set_site_cover_url(album_key, target_site, url)
        except Exception as e:                                  # noqa: BLE001
            logger.warning(f"登记站点封面失败：{e}")

    def _db_reason(self, album_key: str, reason: str) -> None:
        if album_key:
            self._db_upsert(album_key, status="FAILED", reason=(reason or "")[:500])

    def close(self) -> None:
        """释放封面缓存库连接（测试与长驻进程退出时用）。"""
        if self._graph is not None:
            try:
                self._graph.close()
            except Exception:                                   # noqa: BLE001
                pass
            self._graph = None

    # ==================== 缓存键 ====================

    @staticmethod
    def _album_key(source_site: str = "", source_tid: str = "", source_name: str = "") -> str:
        site = (source_site or "").strip().lower() or "unknown"
        tid = (source_tid or "").strip()
        if tid:
            return f"{site}:{tid}"
        name = (source_name or "").strip()
        if name:
            digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]
            return f"{site}:album:{digest}"
        return ""

    # ==================== 站点图床路由 ====================

    @staticmethod
    def _site_cover_mode(target_site: str) -> str:
        """返回 lsky / pter / generic。未登记站点按 generic 处理。"""
        site = (target_site or "").strip().lower()
        if "crab" in site:
            return "lsky"
        if "pter" in site:
            return "pter"
        try:
            from musichub.onsite.site_rules import get_rule
            rule = get_rule(site)
            if "img.crabpt.vip" in str(rule.get("cover_host_required") or ""):
                return "lsky"
        except Exception:                                       # noqa: BLE001
            logger.debug(f"站点 {target_site} 未登记封面规则，按通用图床处理")
        return "generic"

    @staticmethod
    def _lsky_host() -> str:
        base = os.getenv("LSKY_API_URL", "https://img.crabpt.vip/api/v1")
        return base.split("//")[-1].split("/")[0]

    # 私有 PT 站内图床域名（外站审核员/用户端常无法访问或存在防盗链，跨站发布禁止直接复用）
    PRIVATE_SITE_IMAGE_HOSTS = (
        "musopia.vip",
        "pterclub.net",
        "crabpt.vip",
        "open.cd",
        "ubits.club",
        "lemonhd.net",
        "hdtime.org",
        "hdkylin.top",
        "hdkyl.in",
        "xingwan.cc",
        "tangpt.top",
        "muxuege.org",
        "soulvoice.club",
        "nanyangpt.com",
        "springsunday.net",
        "uhddolby.org",
    )

    def _is_blocked_private_image_url(self, url: str, target_site: str) -> Tuple[bool, str]:
        """判断源图 URL 是否属于禁止跨站直链复用的私有站点图床。"""
        raw = (url or "").strip()
        if not raw.startswith(("http://", "https://")):
            return False, ""
        host = (urlparse(raw).hostname or "").strip().lower()
        if not host:
            return False, ""

        target = (target_site or "").strip().lower()
        mode = self._site_cover_mode(target)
        # 目标站自家的专用图床允许直链复用
        lsky_host = self._lsky_host().lower()
        if mode == "lsky" and lsky_host and host == lsky_host:
            return False, ""
        if mode == "pter" and (host == "img.pterclub.net" or host.endswith(".pterclub.net")):
            return False, ""

        for priv in self.PRIVATE_SITE_IMAGE_HOSTS:
            if host == priv or host.endswith("." + priv):
                return True, host
        return False, ""

    # ==================== 主入口 ====================

    def get_site_cover(
        self,
        target_site: str,
        source_cover_url: str = "",
        local_dir: Optional[str] = None,
        source_site: str = "",
        source_tid: str = "",
        source_name: str = "",
        cached_site_covers: Optional[Dict[str, str]] = None,
        expected_image_path: str = "",
    ) -> Tuple[str, str]:
        """获取适用于目标站点的封面 URL，返回 `(url, 说明)`。

        传入 `expected_image_path` 时进入严格模式：调用方缓存、数据库缓存、
        源站 URL 都必须与精确实体目录图片的 SHA256 相同；否则忽略旧链接，
        上传预期图片。URL 复用不再是图片身份正确的证明。
        """
        target = (target_site or "").strip().lower()
        expected: Dict[str, Any] = {}
        if expected_image_path:
            expected = image_file_evidence(expected_image_path)
            if not expected.get("ok"):
                return "", "预期封面无效: " + str(expected.get("error") or "未知错误")

        cached = cached_site_covers or {}
        candidates: List[Tuple[str, str]] = []
        if cached.get(target):
            candidates.append((cached[target], f"复用调用方缓存封面（{target_site}）"))

        album_key = self._album_key(source_site, source_tid, source_name)
        if album_key:
            hit = self._db_get_site_cover(
                album_key, target, local_dir=str(local_dir or ""), source_name=source_name
            )
            if hit:
                candidates.append((hit, f"复用已落库封面 [{album_key} → {target}]"))

        # 严格模式下，缓存只有远端字节与预期 SHA256 完全一致才可返回；
        # 且私有站点图床 URL 即使曾误入缓存也禁止跨站复用。
        for hit, why in candidates:
            blocked_hit, blocked_host = self._is_blocked_private_image_url(hit, target)
            if blocked_hit:
                logger.info(f"忽略私有站点图床缓存 [{album_key} → {target}] ({blocked_host}): {hit}")
                continue
            if not expected:
                return hit, why
            actual = self.fetch_image_evidence(hit)
            if actual.get("ok") and actual.get("sha256") == expected.get("sha256"):
                return hit, why + "；SHA256 已核验"
            logger.warning(f"忽略未通过封面字节校验的缓存 [{album_key} → {target}]: "
                           f"{actual.get('error') or actual.get('sha256')}")

        src = (source_cover_url or "").strip()
        src_valid = False
        src_reason = "源站无封面"
        blocked_priv, blocked_host = self._is_blocked_private_image_url(src, target)
        if blocked_priv:
            src_valid = False
            src_reason = f"私有站点图床禁止跨站复用（{blocked_host}），强制转存公网图床"
            logger.info(f"[CoverImageService] {src_reason}: {src}")
        elif src.startswith(("http://", "https://")):
            src_valid, src_reason = self.is_valid_image_url(src)
        elif src and os.path.exists(src):
            src_valid, src_reason = True, "本地文件路径"
        if src_valid and expected and src.startswith(("http://", "https://")):
            actual = self.fetch_image_evidence(src)
            if not actual.get("ok") or actual.get("sha256") != expected.get("sha256"):
                src_valid = False
                src_reason = "源站图与精确实体目录封面 SHA256 不同"

        if album_key:
            self._db_upsert(
                album_key,
                source_site=(source_site or "").strip().lower(),
                source_tid=str(source_tid or ""),
                source_name=(source_name or "")[:200],
                source_cover_url=src,
                source_cover_status=("OK" if src_valid else ("BROKEN" if src else "MISSING")),
                source_cover_checked_at=time.time(),
            )

        mode = self._site_cover_mode(target)
        if expected:
            return self._resolve_expected_cover(
                target, mode, expected_image_path, src if src_valid else "", album_key,
            )
        if mode == "lsky":
            return self._resolve_crabpt_cover(target, src, src_valid, src_reason,
                                              local_dir, album_key)
        if mode == "pter":
            return self._resolve_pter_cover(target, src, src_valid, src_reason,
                                            local_dir, album_key)
        return self._resolve_generic_cover(target, src, src_valid, src_reason,
                                           local_dir, album_key)

    # ==================== 各站解析 ====================

    def _resolve_expected_cover(self, target: str, mode: str, image_path: str,
                                verified_src: str, album_key: str) -> Tuple[str, str]:
        """严格身份模式只上传预期图片；已核验源链可按站规直接复用。"""
        if verified_src:
            if mode == "generic" or (mode == "pter" and not os.getenv("PTER_IMG_API_KEY", "").strip()):
                return verified_src, f"源站封面 SHA256 与实体目录一致，直接复用（{target}）"
            if mode == "lsky" and self._lsky_host() in verified_src:
                return verified_src, "源站 Lsky 封面 SHA256 与实体目录一致，直接复用"

        expected = image_file_evidence(image_path)
        expected_sha256 = str(expected.get("sha256") or "")
        upload_evidence = expected
        if mode == "lsky":
            actual_path = image_path
            if int(expected.get("size") or 0) > LSKY_MAX_BYTES:
                shrunk = self._shrink_image_to_cache(image_path, LSKY_MAX_BYTES)
                if shrunk:
                    actual_path = shrunk
                    upload_evidence = image_file_evidence(shrunk)
            url = self.upload_lsky(
                actual_path, allow_shrink=False, allow_reencode=False,
                expected_sha256=str(upload_evidence.get("sha256") or ""),
            )
            host = "Lsky"
        elif mode == "pter" and os.getenv("PTER_IMG_API_KEY", "").strip():
            url = self.upload_pter_img(image_path, allow_reencode=False)
            host = "猫站图床"
        else:
            actual_path = image_path
            if int(expected.get("size") or 0) > PICGO_MAX_BYTES:
                shrunk = self._shrink_image_to_cache(image_path, PICGO_MAX_BYTES)
                if shrunk:
                    actual_path = shrunk
                    upload_evidence = image_file_evidence(shrunk)
            url = self.upload_picgo(
                actual_path, allow_reencode=False,
                expected_sha256=str(upload_evidence.get("sha256") or ""),
            )
            host = "PicGo"
        if url:
            # 图床 URL 也必须先过字节校验，才能进入 cover_cache 或站点编辑请求。
            # 否则 Chevereto 判重后的重编码 URL 会先污染站点，post-check 才发现。
            actual = self.fetch_image_evidence(url)
            if not upload_evidence.get("ok") or not actual.get("ok") \
                    or actual.get("sha256") != upload_evidence.get("sha256"):
                why = (f"{host} 返回 URL 未通过提交前 SHA256 校验"
                       f"（预期 {upload_evidence.get('sha256') or '无效'}，"
                       f"实际 {actual.get('sha256') or actual.get('error') or '无效'}）；拒绝写站点")
                self._db_reason(album_key, why)
                return "", why
            self._db_set_cover(
                album_key, target, url, local_dir=os.path.dirname(image_path),
                local_image=image_path, status="OK", reason="严格身份与 SHA256 校验通过",
            )
            return url, f"严格身份校验通过，已上传并预检 {host} 封面字节"

        if verified_src:
            return verified_src, "专用图床上传失败，回退已按 SHA256 核验的源站封面"
        why = f"{host} 上传失败；严格模式拒绝改用未经身份校验的图片"
        self._db_reason(album_key, why)
        return "", why

    def _resolve_generic_cover(self, target: str, src: str, src_valid: bool,
                               src_reason: str, local_dir: Optional[str],
                               album_key: str) -> Tuple[str, str]:
        # 源站图链有效 ⇒ 直接复用（不下载、不上传）
        if src_valid and src.startswith(("http://", "https://")):
            return src, f"源站封面有效，直接复用（{target}）：{src}"

        local_img = self._pick_local_image(src, local_dir, album_key)
        if not local_img:
            why = f"源站无图或失效（{src_reason}），本地也未找到封面"
            self._db_reason(album_key, why)
            return "", why

        url = self.upload_picgo(local_img)
        if url:
            self._db_set_cover(album_key, target, url,
                               local_dir=local_dir or "", local_image=local_img,
                               status="OK", reason="")
            return url, f"源站无有效封面，已上传本地图到 PicGo：{os.path.basename(local_img)}"

        why = "PicGo 上传失败（检查 PICGO_API_KEY / PICGO_UPLOAD_URL）"
        self._db_reason(album_key, f"{why}；本地图 {local_img}")
        return "", why

    def _resolve_pter_cover(self, target: str, src: str, src_valid: bool,
                            src_reason: str, local_dir: Optional[str],
                            album_key: str) -> Tuple[str, str]:
        pter_key = os.getenv("PTER_IMG_API_KEY", "").strip()
        src_is_http = src.startswith(("http://", "https://"))

        # 未配猫站图床：有效源链直接复用；否则退回通用 PicGo
        if not pter_key:
            if src_valid and src_is_http:
                return src, f"猫站未配置专用图床，复用有效公网封面：{src}"
            return self._resolve_generic_cover(target, src, src_valid, src_reason,
                                               local_dir, album_key)

        local_img = self._pick_local_image(src, local_dir, album_key)
        if not local_img:
            if src_valid and src_is_http:
                return src, f"猫站图床无本地图可传，回退有效源链：{src}"
            why = f"源站无图或失效（{src_reason}），本地也未找到封面"
            self._db_reason(album_key, why)
            return "", why

        url = self.upload_pter_img(local_img)
        if url:
            self._db_set_cover(album_key, target, url,
                               local_dir=local_dir or "", local_image=local_img,
                               status="OK", reason="")
            return url, f"已上传本地图到猫站图床：{os.path.basename(local_img)}"

        if src_valid and src_is_http:
            return src, f"猫站图床上传失败，回退有效源链：{src}"
        why = "猫站图床上传失败（检查 PTER_IMG_API_KEY / PTER_IMG_UPLOAD_URL）"
        self._db_reason(album_key, f"{why}；本地图 {local_img}")
        return "", why

    def _resolve_crabpt_cover(self, target: str, src: str, src_valid: bool,
                              src_reason: str, local_dir: Optional[str],
                              album_key: str) -> Tuple[str, str]:
        lsky_host = self._lsky_host()
        # 源链本身就是自家 Lsky 且有效 ⇒ 复用
        if src_valid and lsky_host and lsky_host in src:
            return src, f"复用已有 Lsky 封面（蟹黄堡）：{src}"

        local_img = self._pick_local_image(src, local_dir, album_key)
        if not local_img:
            why = (f"蟹黄堡必须用 Lsky；源站无有效图（{src_reason}），"
                   f"本地也未找到封面")
            self._db_reason(album_key, why)
            return "", why

        url = self.upload_lsky(local_img)
        if url:
            self._db_set_cover(album_key, target, url,
                               local_dir=local_dir or "", local_image=local_img,
                               status="OK", reason="")
            return url, f"已上传本地图到 Lsky Pro（蟹黄堡）：{os.path.basename(local_img)}"

        err = self.last_lsky_error or LAST_LSKY_ERROR or "未知错误"
        why = f"Lsky 上传失败：{err}（本地图 {local_img}）"
        self._db_reason(album_key, why)
        return "", why

    # ==================== 本地图片查找 ====================

    def _pick_local_image(self, src: str, local_dir: Optional[str],
                          album_key: str) -> Optional[str]:
        """本地目录优先；本地没有且源链是 http 时，下载到缓存再用。"""
        if local_dir:
            local_img = self.find_local_cover(local_dir)
            if local_img:
                if album_key:
                    self._db_upsert(album_key, local_dir=local_dir, local_image=local_img)
                return local_img
        if src.startswith(("http://", "https://")):
            cached = self._get_cached_file(src)
            if cached:
                return cached
            downloaded = self._download_and_cache(src)
            if downloaded:
                if album_key:
                    self._db_upsert(album_key, local_image=downloaded)
                return downloaded
        return None

    def find_local_cover(self, folder_path: Optional[str]) -> Optional[str]:
        """在本地专辑目录里找最合适的封面（移植父项目 assets.find_cover）。

        分级匹配，同级取最大文件；全部不匹配时取最大图；顶层无图再看一层子目录。
        """
        if not folder_path or not os.path.exists(folder_path):
            return None

        if os.path.isfile(folder_path):
            folder_path = os.path.dirname(folder_path)

        images: List[Tuple[str, str, int]] = []
        for root, _, files in os.walk(folder_path):
            for f in files:
                if not f.lower().endswith(COVER_EXTS):
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
            return self._find_nested_cover(folder_path)

        for pattern in COVER_PATTERNS:
            hits = [(f, p, s) for f, p, s in images
                    if pattern.search(os.path.splitext(f)[0]) or pattern.search(f)]
            if hits:
                hits.sort(key=lambda x: -x[2])
                return hits[0][1]

        images.sort(key=lambda x: -x[2])
        return images[0][1]

    @staticmethod
    def _find_nested_cover(dir_path: str) -> Optional[str]:
        """合辑类种子顶层没有图时，取一层子目录里的 front/cover/folder。"""
        try:
            subs = [e for e in os.scandir(dir_path) if e.is_dir()]
        except OSError:
            return None
        for sub in sorted(subs, key=lambda x: x.name):
            try:
                files = list(os.scandir(sub.path))
            except OSError:
                continue
            for f in files:
                if not f.is_file():
                    continue
                stem, ext = os.path.splitext(f.name)
                if ext.lower() not in COVER_EXTS:
                    continue
                if stem.strip().lower() in _COVER_STEMS:
                    return f.path
        return None

    # ==================== 源链有效性探测 ====================

    def _probe_cache_path(self) -> str:
        return os.path.join(self.cache_dir, "_url_probe.json")

    def _load_probe_cache(self) -> Dict[str, Any]:
        if self._probe_cache is not None:
            return self._probe_cache
        try:
            with open(self._probe_cache_path(), "r", encoding="utf-8") as f:
                self._probe_cache = json.load(f)
        except Exception:                                       # noqa: BLE001
            self._probe_cache = {}
        return self._probe_cache

    def _save_probe_cache(self) -> None:
        try:
            tmp = self._probe_cache_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._probe_cache or {}, f, ensure_ascii=False)
            os.replace(tmp, self._probe_cache_path())
        except Exception as e:                                  # noqa: BLE001
            logger.debug(f"封面探测缓存写入失败：{e}")

    def _remember_probe(self, url: str, ok: bool, reason: str) -> None:
        cache = self._load_probe_cache()
        cache[url] = {"ok": bool(ok), "reason": reason, "ts": int(time.time())}
        if len(cache) > 5000:
            newest = sorted(cache.items(), key=lambda kv: kv[1].get("ts", 0))[-3000:]
            cache = dict(newest)
            self._probe_cache = cache
        self._save_probe_cache()

    @staticmethod
    def fetch_image_evidence(url: str, timeout: int = 25) -> Dict[str, Any]:
        """实时取回图片并计算内容证据；严格审计不使用 7 天 URL 探测缓存。"""
        u = (url or "").strip()
        if not u.startswith(("http://", "https://")):
            return {"ok": False, "error": "非 http(s) URL"}
        try:
            with requests.get(u, timeout=(10, timeout), stream=True, allow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status_code != 200:
                    return {"ok": False, "error": f"HTTP {resp.status_code}"}
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if ctype and not ctype.startswith(("image/", "application/octet-stream")):
                    return {"ok": False, "error": f"非图片 Content-Type: {ctype[:60]}"}
                chunks: List[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_COVER_BYTES:
                        return {"ok": False, "error": "图片超过大小上限", "size": total}
                    chunks.append(chunk)
                return image_bytes_evidence(b"".join(chunks))
        except requests.RequestException as exc:
            return {"ok": False, "error": sanitize_image_error(exc)}

    def is_valid_image_url(self, url: str, timeout: int = 20) -> Tuple[bool, str]:
        """探测源站图链是否为**真实可用图片**（HTTP 200 + 图片类型 + 魔数 + 大小上限）。

        结果按 URL 缓存 7 天，避免每轮发种重复探测同一图链。
        """
        u = (url or "").strip()
        if not u:
            return False, "空 URL"
        if not u.startswith(("http://", "https://")):
            return False, "非 http(s) URL"

        cached = self._load_probe_cache().get(u)
        if cached and (time.time() - int(cached.get("ts", 0))) < PROBE_TTL:
            return bool(cached.get("ok")), str(cached.get("reason") or "")

        try:
            with requests.get(u, timeout=(10, timeout), stream=True,
                              allow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status_code != 200:
                    reason = f"HTTP {resp.status_code}"
                    self._remember_probe(u, False, reason)
                    return False, reason

                ctype = (resp.headers.get("Content-Type") or "").lower()
                first = b""
                total = 0
                for chunk in resp.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    if not first:
                        first = chunk[:64]
                        if ctype and not ctype.startswith("image/"):
                            reason = f"非图片（Content-Type={ctype[:40]}）"
                            self._remember_probe(u, False, reason)
                            return False, reason
                        if not image_ext(first):
                            reason = "非图片（魔数不匹配）"
                            self._remember_probe(u, False, reason)
                            return False, reason
                    total += len(chunk)
                    if total > MAX_COVER_BYTES:
                        reason = f"超过 {MAX_COVER_BYTES // (1024 * 1024)}MB"
                        self._remember_probe(u, False, reason)
                        return False, reason

                if total == 0:
                    self._remember_probe(u, False, "0 字节")
                    return False, "0 字节"
        except requests.RequestException as e:
            reason = f"请求失败：{type(e).__name__}"
            self._remember_probe(u, False, reason)
            return False, reason

        self._remember_probe(u, True, "OK")
        return True, "OK"

    # ==================== 下载与本地缓存 ====================

    def _cache_key(self, url: str) -> str:
        return hashlib.sha1((url or "").strip().encode("utf-8")).hexdigest()[:12]

    def _get_cached_file(self, url: str) -> Optional[str]:
        key = self._cache_key(url)
        for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            p = os.path.join(self.cache_dir, f"{key}{ext}")
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return p
        return None

    def _download_and_cache(self, url: str) -> Optional[str]:
        """下载源图到本地缓存；校验魔数与大小，失败返回 None。"""
        u = (url or "").strip()
        if not u:
            return None
        try:
            resp = requests.get(u, timeout=40, stream=True,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                return None
            content = b""
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                content += chunk
                if len(content) > MAX_COVER_BYTES:
                    return None
            ext = image_ext(content[:64])
            if not ext or not content:
                return None
            target = os.path.join(self.cache_dir, self._cache_key(u) + ext)
            tmp = target + ".tmp"
            with open(tmp, "wb") as f:
                f.write(content)
            os.replace(tmp, target)
            return target
        except Exception as e:                                  # noqa: BLE001
            logger.warning(f"下载封面失败 {u[:80]}: {e}")
            return None

    # ==================== PicGo / Chevereto ====================

    def _url_cache_path(self) -> str:
        return os.path.join(self.cache_dir, "_uploaded_urls.json")

    def _load_url_cache(self) -> Dict[str, str]:
        try:
            with open(self._url_cache_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:                                       # noqa: BLE001
            return {}

    def _save_url_cache(self, data: Dict[str, str]) -> None:
        try:
            tmp = self._url_cache_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._url_cache_path())
        except Exception as e:                                  # noqa: BLE001
            logger.warning(f"封面 URL 缓存写入失败: {e}")

    def _cache_url_on_host(self, digest: str, accepted_hosts: List[str]) -> str:
        """命中缓存且 URL 主机属于 accepted_hosts（任一为子串）才返回，否则空串。

        缓存按图片 sha1 建键、跨图床共享；A 图床的链接不能被 B 图床（有主机白名单）
        误用——否则 crabpt 会拿到 PicGo 链被 cover_host_required 拒掉。
        """
        if not digest:
            return ""
        url = (self._load_url_cache().get(digest) or "").strip()
        if not url:
            return ""
        try:
            netloc = (urlparse(url).netloc or "").lower()
        except Exception:                                        # noqa: BLE001
            return ""
        for h in accepted_hosts:
            if h and h.lower() in netloc:
                return url
        return ""

    def remember_upload(self, file_path: str, url: str) -> None:
        """登记「本地文件 → 已上传 URL」，避免同图第二次上传被判重复后拿不到链。"""
        if not file_path or not url:
            return
        try:
            with open(file_path, "rb") as f:
                digest = hashlib.sha1(f.read()).hexdigest()
            cache = self._load_url_cache()
            cache[digest] = url
            self._save_url_cache(cache)
        except Exception as e:                                  # noqa: BLE001
            logger.warning(f"封面 URL 缓存登记失败 {file_path}: {e}")

    @staticmethod
    def _picgo_extract_url(data: Any) -> str:
        """从 Chevereto 响应里抠图片 URL，兼容 image.url / result.url / url。"""
        if not isinstance(data, dict):
            return ""
        img = data.get("image") or {}
        if isinstance(img, dict):
            url = img.get("url") or img.get("display_url") or ""
            if url:
                return url
        result = data.get("result") or {}
        if isinstance(result, dict):
            url = result.get("url") or ""
            if url:
                return url
        return str(data.get("url") or "")

    def _chevereto_upload(self, image_path: str, api_url: str, api_key: str,
                          retries: int = 3, allow_reencode: bool = True) -> Optional[str]:
        """通用 Chevereto API v1 上传；具备指数退避重试，严格封面模式禁止判重后重编码。"""
        if not image_path or not os.path.exists(image_path) or not api_key:
            return None

        last_err = ""
        duplicate_no_url = False
        for attempt in range(retries + 1):
            try:
                with open(image_path, "rb") as f:
                    res = requests.post(
                        api_url,
                        headers={"X-API-Key": api_key},
                        files={"source": (os.path.basename(image_path), f)},
                        timeout=45,
                    )
                if res.status_code == 200:
                    url = self._picgo_extract_url(res.json())
                    if url:
                        return url
                    last_err = f"响应里没有 url 字段: {res.text[:150]}"
                elif res.status_code == 400 and "duplicat" in res.text.lower():
                    try:
                        url = self._picgo_extract_url(res.json())
                    except ValueError:
                        url = ""
                    if url:
                        logger.info(f"[图床] {os.path.basename(image_path)} 已存在，复用 URL: {url}")
                        return url
                    duplicate_no_url = True
                    last_err = f"重复上传但响应无可回收 URL（HTTP 400: {res.text[:150]}）"
                else:
                    last_err = f"HTTP {res.status_code}: {res.text[:150]}"
            except Exception as e:                              # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"

            if attempt < retries:
                backoff = 2 * (attempt + 1)
                logger.warning(f"[图床] 上传 {os.path.basename(image_path)} 失败 ({attempt+1}/{retries})，{backoff}s 后重试: {last_err}")
                time.sleep(backoff)

        if duplicate_no_url and allow_reencode:
            # 图床按内容去重：该图已存在但本地缓存未命中、400 响应又不带 URL。
            # 改字节重传（新内容 → 不再判重）拿回唯一 URL（与 Lsky 同理）。
            import tempfile
            try:
                with open(image_path, "rb") as f:
                    blob = f.read()
            except OSError:
                blob = b""
            new_blob = self._reencode_bytes(
                blob, os.path.splitext(image_path)[1] or ".jpg")
            if new_blob:
                ext = os.path.splitext(image_path)[1] or ".jpg"
                nd = hashlib.sha1(new_blob).hexdigest()
                tmp = os.path.join(tempfile.gettempdir(), f"cv_{nd}{ext}")
                try:
                    with open(tmp, "wb") as f:
                        f.write(new_blob)
                    logger.info(f"[图床] {os.path.basename(image_path)} 重复且无URL，"
                                f"改字节重传 → {nd}{ext}")
                    with open(tmp, "rb") as f:
                        res2 = requests.post(
                            api_url,
                            headers={"X-API-Key": api_key},
                            files={"source": (f"{nd}{ext}", f)},
                            timeout=40,
                        )
                    if res2.status_code == 200:
                        url2 = self._picgo_extract_url(res2.json())
                        if url2:
                            return url2
                    last_err = f"改字节重传仍失败（HTTP {res2.status_code}）"
                except Exception as e:                          # noqa: BLE001
                    last_err = f"{type(e).__name__}: {e}"
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        logger.warning(f"[图床失败] {os.path.basename(image_path)} → {last_err}")
        return None

    def _shrink_image_to_cache(self, image_path: str, max_bytes: int,
                               max_dim: int = LSKY_SHRINK_MAX_DIM) -> Optional[str]:
        """将超限大图压缩为 JPEG 存入 self.cache_dir，绝不修改或污染保种原目录。"""
        if not image_path or not os.path.exists(image_path):
            return None
        try:
            orig_size = os.path.getsize(image_path)
        except OSError:
            return None
        if orig_size <= max_bytes:
            return image_path
        try:
            from PIL import Image
        except Exception:                                       # noqa: BLE001
            logger.warning("Pillow 未安装，无法压缩超限封面")
            return None

        try:
            with open(image_path, "rb") as fh:
                key_hash = hashlib.sha1(fh.read(1024 * 1024)).hexdigest()[:16]
        except OSError:
            return None

        out_path = os.path.join(self.cache_dir, f"shrunk_{key_hash}_{orig_size}_{max_bytes}.jpg")
        try:
            if os.path.exists(out_path) and 0 < os.path.getsize(out_path) <= max_bytes:
                return out_path
        except OSError:
            pass

        try:
            with Image.open(image_path) as im:
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                for dim in (max_dim, 1800, 1400):
                    work = im.copy()
                    work.thumbnail((dim, dim))
                    for quality in (90, 82, 72, 62):
                        tmp = out_path + ".tmp"
                        work.save(tmp, "JPEG", quality=quality, optimize=True)
                        if os.path.getsize(tmp) <= max_bytes:
                            os.replace(tmp, out_path)
                            logger.info(
                                f"[大图压缩] {os.path.basename(image_path)} "
                                f"({orig_size / (1024 * 1024):.2f}MB → {os.path.getsize(out_path) / (1024 * 1024):.2f}MB)"
                            )
                            return out_path
                        os.remove(tmp)
        except Exception as e:                                  # noqa: BLE001
            logger.warning(f"[大图压缩失败] {os.path.basename(image_path)}: {e}")
        return None

    def upload_picgo(self, file_path: str, allow_reencode: bool = True,
                     expected_sha256: str = "", allow_shrink: bool = True) -> Optional[str]:
        """上传 PicGo；超限大图自动压缩至 cache_dir 后上传，严格模式会绕过错误缓存。"""
        api_key = (os.getenv("PICGO_API_KEY") or "").strip()
        # ★ 默认必须带 www：picgo.net 会 301 到 www，POST 被转成 GET 后丢文件体，图床回 400
        api_url = (os.getenv("PICGO_UPLOAD_URL") or os.getenv("PICGO_API_URL")
                   or "https://www.picgo.net/api/1/upload").strip()
        if not api_key:
            logger.warning("未配置 PICGO_API_KEY，无法上传封面")
            return None

        upload_path = file_path
        try:
            if allow_shrink and os.path.exists(file_path) and os.path.getsize(file_path) > PICGO_MAX_BYTES:
                shrunk = self._shrink_image_to_cache(file_path, PICGO_MAX_BYTES)
                if not shrunk:
                    logger.warning(f"封面超过 PicGo 上限且压缩失败: {file_path}")
                    return None
                upload_path = shrunk
            with open(upload_path, "rb") as f:
                blob = f.read()
        except OSError as e:
            logger.warning(f"封面读取失败 {upload_path}: {e}")
            return None

        digest = hashlib.sha1(blob).hexdigest()
        cache = self._load_url_cache()
        cached_url = self._cache_url_on_host(
            digest, ["picgo.net", urlparse(api_url).netloc or ""])
        if cached_url:
            if not expected_sha256:
                logger.info(f"复用已上传封面 {os.path.basename(upload_path)} → {cached_url}")
                return cached_url
            actual = self.fetch_image_evidence(cached_url)
            if actual.get("ok") and actual.get("sha256") == expected_sha256:
                logger.info(f"复用已核验 PicGo 封面 {os.path.basename(upload_path)} → {cached_url}")
                return cached_url
            logger.warning(f"PicGo 缓存字节不符，忽略并重新上传原图: {cached_url}")

        url = self._chevereto_upload(
            upload_path, api_url, api_key,
            allow_reencode=allow_reencode and not expected_sha256,
        )
        if url:
            if expected_sha256:
                actual = self.fetch_image_evidence(url)
                if actual.get("ok") and actual.get("sha256") == expected_sha256:
                    cache[digest] = url
                    self._save_url_cache(cache)
                else:
                    logger.warning(f"PicGo 新 URL 字节不符，不写原图缓存: {url}")
            else:
                cache[digest] = url
                self._save_url_cache(cache)
        return url

    def upload_pter_img(self, file_path: str, allow_reencode: bool = True) -> Optional[str]:
        """上传至猫站专用图床（Chevereto API v1）；未配 key 返回 None。"""
        api_key = (os.getenv("PTER_IMG_API_KEY") or "").strip()
        api_url = (os.getenv("PTER_IMG_UPLOAD_URL")
                   or "https://img.pterclub.net/api/1/upload").strip()
        if not api_key:
            return None
        return self._chevereto_upload(
            file_path, api_url, api_key, allow_reencode=allow_reencode,
        )

    # ==================== Lsky Pro（蟹黄堡专线） ====================

    def _get_lsky_token(self, api_url: str, force: bool = False) -> Optional[str]:
        if not force:
            env_token = (os.getenv("LSKY_TOKEN") or "").strip()
            if env_token:
                return env_token
            if self._lsky_token:
                return self._lsky_token

        email = (os.getenv("LSKY_EMAIL") or "").strip()
        pwd = (os.getenv("LSKY_PASSWORD") or "").strip()
        if not email or not pwd:
            return self._lsky_token or (os.getenv("LSKY_TOKEN") or "").strip() or None

        try:
            resp = requests.post(
                f"{api_url}/tokens",
                json={"email": email, "password": pwd},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json() or {}
                token = ((data.get("data") or {}).get("token")
                         or data.get("token") or "")
                if token:
                    self._lsky_token = token
                    return token
            logger.warning(f"Lsky 换取 token 失败：HTTP {resp.status_code}")
        except Exception as e:                                  # noqa: BLE001
            logger.error(f"Lsky 换取 token 异常：{type(e).__name__}: {e}")
        return None

    def _shrink_for_lsky(self, image_path: str) -> Optional[str]:
        """超过 Lsky 大小上限时压到 5MB 以下；Pillow 缺失则返回 None（单条级失败）。"""
        global LAST_LSKY_ERROR
        try:
            from PIL import Image
        except Exception:                                       # noqa: BLE001
            LAST_LSKY_ERROR = "Pillow 未安装，无法压缩超过 Lsky 上限的图"
            self.last_lsky_error = LAST_LSKY_ERROR
            return None

        target = os.path.splitext(image_path)[0] + LSKY_SHRINK_SUFFIX
        try:
            with Image.open(image_path) as im:
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                im.thumbnail((LSKY_SHRINK_MAX_DIM, LSKY_SHRINK_MAX_DIM))
                for quality in (90, 80, 70, 60):
                    tmp = target + ".tmp"
                    im.save(tmp, "JPEG", quality=quality)
                    if os.path.getsize(tmp) <= LSKY_MAX_BYTES:
                        os.replace(tmp, target)
                        return target
                    os.remove(tmp)
        except Exception as e:                                  # noqa: BLE001
            LAST_LSKY_ERROR = f"压图失败：{type(e).__name__}: {e}"
            self.last_lsky_error = LAST_LSKY_ERROR
            return None

        LAST_LSKY_ERROR = f"压图后仍超过 Lsky 上限 {LSKY_MAX_BYTES}"
        self.last_lsky_error = LAST_LSKY_ERROR
        return None

    @staticmethod
    def _lsky_url_named_by(url: str, digest: str) -> bool:
        """Lsky 返回的 URL 文件名是否按指定 digest 命名（唯一路径判据）。"""
        name = (url or "").rstrip("/").split("/")[-1].split("?")[0]
        return bool(digest) and name.rsplit(".", 1)[0].startswith(digest)

    def _reencode_bytes(self, blob: bytes, ext: str) -> Optional[bytes]:
        """用 Pillow 重新编码产生新字节，打破 Lsky 按内容去重命中旧碰撞路径。

        旧版把不同专辑都以 cover.jpg 传到同日同一路径，Lsky 已存下这些内容；
        同内容重传会被去重返回旧的 cover.jpg 路径——必须改字节才能拿到新唯一路径。
        """
        try:
            from PIL import Image
            import io
        except Exception:                                    # noqa: BLE001
            return None
        try:
            im = Image.open(io.BytesIO(blob))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            out = io.BytesIO()
            e = (ext or ".jpg").lower()
            if e in (".jpg", ".jpeg"):
                im.save(out, "JPEG", quality=95, optimize=True)
            elif e == ".png":
                im.save(out, "PNG", optimize=True)
            else:
                im.save(out, "JPEG", quality=95, optimize=True)
            data = out.getvalue()
            return data if data and data != blob else None
        except Exception as e:                                # noqa: BLE001
            logger.warning(f"Lsky 封面重编码失败: {e}")
            return None

    def _lsky_post_upload(self, api_url: str, token: str, name: str,
                          blob: bytes) -> Optional[str]:
        """单次 Lsky 上传（含一次 401 刷 token 重试），返回 URL 或 None。"""
        global LAST_LSKY_ERROR
        tok = token
        for attempt in (0, 1):
            try:
                resp = requests.post(
                    f"{api_url}/upload",
                    headers={"Authorization": f"Bearer {tok}",
                             "Accept": "application/json"},
                    files={"file": (name, blob)},
                    timeout=40,
                )
            except Exception as e:                              # noqa: BLE001
                LAST_LSKY_ERROR = f"网络异常：{type(e).__name__}: {e}"
                self.last_lsky_error = LAST_LSKY_ERROR
                return None
            if resp.status_code == 200:
                try:
                    data = resp.json() or {}
                except ValueError:
                    data = {}
                if data.get("status"):
                    url = ((data.get("data") or {}).get("links") or {}).get("url") or ""
                    if url:
                        return url
                    LAST_LSKY_ERROR = "Lsky 返回成功但没有 links.url"
                else:
                    LAST_LSKY_ERROR = str(data.get("message") or resp.text[:200])
            elif resp.status_code in (401, 403) and attempt == 0:
                tok = self._get_lsky_token(api_url, force=True)
                if tok:
                    continue
                LAST_LSKY_ERROR = f"Lsky 鉴权失败（HTTP {resp.status_code}）"
            else:
                LAST_LSKY_ERROR = f"HTTP {resp.status_code}: {resp.text[:200]}"
            self.last_lsky_error = LAST_LSKY_ERROR
            logger.warning(f"Lsky 上传失败：{LAST_LSKY_ERROR}")
            return None
        return None

    def upload_lsky(self, file_path: str, allow_shrink: bool = True,
                    allow_reencode: bool = True, expected_sha256: str = "") -> Optional[str]:
        """上传 Lsky；严格模式禁止缩图/重编码，且只缓存原字节匹配的 URL。"""
        global LAST_LSKY_ERROR
        if expected_sha256:
            allow_shrink = False
            allow_reencode = False
        api_url = (os.getenv("LSKY_API_URL") or "https://img.crabpt.vip/api/v1").rstrip("/")
        token = self._get_lsky_token(api_url)
        if not token:
            LAST_LSKY_ERROR = "没有可用的 Lsky token（检查 LSKY_TOKEN 或 LSKY_EMAIL/PASSWORD）"
            self.last_lsky_error = LAST_LSKY_ERROR
            logger.warning(LAST_LSKY_ERROR)
            return None

        upload_path = file_path
        try:
            if os.path.getsize(file_path) > LSKY_MAX_BYTES:
                if not allow_shrink:
                    LAST_LSKY_ERROR = "严格封面模式禁止缩图，原图超过 Lsky 大小上限"
                    self.last_lsky_error = LAST_LSKY_ERROR
                    return None
                upload_path = self._shrink_for_lsky(file_path)
                if not upload_path:
                    self.last_lsky_error = LAST_LSKY_ERROR
                    return None
        except OSError as e:
            LAST_LSKY_ERROR = f"读取文件失败：{e}"
            self.last_lsky_error = LAST_LSKY_ERROR
            return None

        # ★ 用内容 sha1 做唯一文件名 + 客户端缓存（2026-11-21 修复）：
        # Lsky 按「日期/原名」落盘不做唯一化，同日多次上传 "cover.jpg" 会互相覆盖，
        # 导致不同专辑指向同一路径、显示同一张错图（实测 8 张专辑全落到 cover.jpg）。
        # 改为 sha1 命名后路径唯一；同一张图复用缓存 URL，也避免重复上传。
        try:
            with open(upload_path, "rb") as f:
                blob = f.read()
        except OSError as e:
            LAST_LSKY_ERROR = f"读取文件失败：{e}"
            self.last_lsky_error = LAST_LSKY_ERROR
            return None
        digest = hashlib.sha1(blob).hexdigest()
        cache = self._load_url_cache()
        cached_url = self._cache_url_on_host(digest, [self._lsky_host()])
        if cached_url:
            # 只信任「文件名按本内容 sha1 命名」的缓存：旧版用原名上传导致同日同名覆盖，
            # 残留的 cover.jpg / 原名缓存必须作废，否则不同专辑继续撞同一路径。
            cached_name = cached_url.rstrip("/").split("/")[-1].split("?")[0]
            if cached_name.rsplit(".", 1)[0].startswith(digest):
                if not expected_sha256:
                    logger.info(f"复用已上传(Lsky)封面 {os.path.basename(upload_path)} → {cached_url}")
                    return cached_url
                actual = self.fetch_image_evidence(cached_url)
                if actual.get("ok") and actual.get("sha256") == expected_sha256:
                    logger.info(f"复用已核验 Lsky 封面 {os.path.basename(upload_path)} → {cached_url}")
                    return cached_url
                logger.warning(f"Lsky 缓存字节不符，忽略并重新上传原图: {cached_url}")
            else:
                logger.info(f"忽略陈旧 Lsky 缓存（非 sha1 命名，将重传）: {cached_url}")
        ext = os.path.splitext(upload_path)[1] or ".jpg"
        unique_name = f"{digest}{ext}"

        url = self._lsky_post_upload(api_url, token, unique_name, blob)
        if url and not self._lsky_url_named_by(url, digest):
            if not allow_reencode:
                LAST_LSKY_ERROR = "严格封面模式禁止在 Lsky 内容判重后重编码原图"
                self.last_lsky_error = LAST_LSKY_ERROR
                return None
            # Lsky 按内容去重：该内容曾以原名(cover.jpg)上传过，会返回旧的碰撞路径，
            # 而非我们传的 digest 文件名。重新编码改字节 → 新 digest → 唯一新路径。
            logger.info(f"Lsky 返回陈旧路径（内容去重命中 {url}），改字节重传")
            new_blob = self._reencode_bytes(blob, ext)
            if new_blob:
                nd = hashlib.sha1(new_blob).hexdigest()
                url2 = self._lsky_post_upload(api_url, token, f"{nd}{ext}", new_blob)
                if url2 and self._lsky_url_named_by(url2, nd):
                    cache[nd] = url2
                    self._save_url_cache(cache)
                    return url2
                logger.warning(f"Lsky 改字节重传仍未拿到唯一路径: {url2}")
            # 拿不到唯一正确路径：宁可不补封面（由 descr 兜底），也不返回错图
            LAST_LSKY_ERROR = ("Lsky 内容去重命中旧碰撞路径，"
                               "且无法改字节获得唯一封面路径")
            self.last_lsky_error = LAST_LSKY_ERROR
            return None
        if url:
            if expected_sha256:
                actual = self.fetch_image_evidence(url)
                if actual.get("ok") and actual.get("sha256") == expected_sha256:
                    cache[digest] = url
                    self._save_url_cache(cache)
                else:
                    logger.warning(f"Lsky 新 URL 字节不符，不写原图缓存: {url}")
            else:
                cache[digest] = url
                self._save_url_cache(cache)
            return url
        self.last_lsky_error = LAST_LSKY_ERROR
        return None
