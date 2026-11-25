# -*- coding: utf-8 -*-
"""已发布种子的封面身份链与字节审计。

唯一可信路径：目标 tid 的发布台账 -> B torrent -> 精确文件清单 ->
实体专辑目录 -> 预期封面 SHA256 -> 站点首图 SHA256。
名称相似、name+size、URL 相同或 cover_cache 命中均不能替代这条证据链。
"""

import os
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from musichub.metadata.cover_service import (
    CoverImageService, image_file_evidence, sanitize_image_error,
)
from musichub.onsite.detail_archive import sanitize_url
from musichub.storage.pool_pairing import POOL_SPECS, PoolPairingManager

HEX40 = frozenset("0123456789abcdef")
FORBIDDEN_ROOTS = {"music5"}


def _valid_hash(value: Any) -> str:
    h = str(value or "").strip().lower()
    return h if len(h) == 40 and set(h) <= HEX40 else ""


def _failure(code: str, message: str, **fields: Any) -> Dict[str, Any]:
    return {"ok": False, "status": code, "error": message, **fields}


def _safe_torrent_files(name: str, files: Sequence[Tuple[str, int]]) -> bool:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return False
    for rel, size in files:
        parts = [p for p in str(rel).replace("\\", "/").split("/") if p]
        if not parts or ".." in parts or parts[0] != name or int(size) < 0:
            return False
    return True


def _read_b_torrent(target: Dict[str, Any], db_path: str, archive: Any) -> Tuple[bytes, str, str]:
    site, tid = target["target_site"], str(target["target_tid"])
    expected = _valid_hash(target.get("b_hash"))
    path = os.path.join(os.path.dirname(os.path.abspath(db_path)), "torrents", f"{site}_{tid}.torrent")
    local_error = ""
    if os.path.isfile(path):
        try:
            with open(path, "rb") as fh:
                payload = fh.read()
            _name, _files, _size, found = PoolPairingManager.parse_torrent(payload)
            if expected and found.lower() != expected:
                return b"", "local_archive", "本地 B 种 hash 与发布血缘不一致"
            return payload, "local_archive", ""
        except Exception as exc:  # 继续尝试站点取回，但不在错误里泄露响应正文
            local_error = sanitize_image_error(exc)
    if archive is None:
        return b"", "local_archive", local_error or "本地 B 种归档不可用"
    try:
        payload = archive.adapter(site).download_torrent(tid) or b""
        if not payload:
            return b"", "site_download", "站点未返回 B 种字节"
        _name, _files, _size, found = PoolPairingManager.parse_torrent(payload)
        if expected and found.lower() != expected:
            return b"", "site_download", "站点 B 种 hash 与发布血缘不一致"
        return payload, "site_download", ""
    except Exception as exc:
        note = "站点取回内容不是可解析 torrent: " + sanitize_image_error(exc)
        if local_error:
            note += "；本地归档也不可用: " + local_error
        return b"", "site_download", note


def resolve_cover_identity(
    target: Dict[str, Any],
    db_path: str,
    archive: Any = None,
    pool_roots: Optional[Sequence[Tuple[str, str]]] = None,
    cover_service: Optional[CoverImageService] = None,
) -> Dict[str, Any]:
    """解析可自动修复的精确封面身份；任何疑义均 fail-closed。

    `pool_roots` 为 `(池名, 宿主数据根)`，测试可传临时目录。生产默认只含
    3010 配对使用的 8 池，绝不扫描 music5。
    """
    payload, torrent_source, error = _read_b_torrent(target, db_path, archive)
    if not payload:
        return _failure("B_TORRENT_UNRESOLVED", error, torrent_source=torrent_source)
    try:
        name, files, total_size, b_hash = PoolPairingManager.parse_torrent(payload)
    except Exception as exc:
        return _failure("B_TORRENT_UNRESOLVED", sanitize_image_error(exc), torrent_source=torrent_source)
    if not b_hash or not _safe_torrent_files(name, files):
        return _failure("B_TORRENT_UNRESOLVED", "B 种根目录或文件清单不安全", torrent_source=torrent_source)
    if not target.get("source_site") and not target.get("source_tid") \
            and not target.get("b_hash") and not target.get("local_path"):
        return _failure(
            "LINEAGE_UNRESOLVED", "发布台账缺少来源/B hash/本地路径，禁止按标题猜专辑",
            b_hash=b_hash, torrent_source=torrent_source,
        )

    roots = list(pool_roots or [(n, root) for n, _port, root in POOL_SPECS])
    manager = PoolPairingManager()
    requested = os.path.realpath(str(target.get("local_path") or "")) if target.get("local_path") else ""
    candidates: List[Dict[str, Any]] = []
    rejected: Dict[str, str] = {}
    for pool_name, root in roots:
        if pool_name in FORBIDDEN_ROOTS or "music5" in os.path.normpath(root).split(os.sep):
            return _failure("FORBIDDEN_ROOT", "music5 禁区不得进入封面身份链", b_hash=b_hash)
        exact_dir = os.path.realpath(os.path.join(root, name))
        if requested and requested != exact_dir:
            continue
        if requested and not requested.startswith(os.path.realpath(root) + os.sep):
            return _failure("LOCAL_PATH_MISMATCH", "lineage.local_path 不在受管音乐池内", b_hash=b_hash)
        if not os.path.isdir(exact_dir):
            rejected[pool_name] = "精确根目录不存在"
            continue
        valid, detail = manager.verify_payload(root, files)
        if valid:
            candidates.append({"pool": pool_name, "root": root, "local_path": exact_dir})
        else:
            rejected[pool_name] = (
                f"文件清单不符 missing={detail.get('missing_count')} mismatch={detail.get('mismatch_count')}"
            )
    if requested and not candidates:
        return _failure(
            "LOCAL_PATH_MISMATCH", "lineage.local_path 未通过 B 种精确文件清单校验",
            b_hash=b_hash, torrent_source=torrent_source, rejected=rejected,
        )
    if not candidates:
        return _failure(
            "PAYLOAD_UNRESOLVED", "没有实体目录通过 B 种精确文件清单校验",
            b_hash=b_hash, torrent_source=torrent_source, rejected=rejected,
        )

    svc = cover_service or CoverImageService()
    expected: List[Dict[str, Any]] = []
    for candidate in candidates:
        image_path = svc.find_local_cover(candidate["local_path"])
        if not image_path:
            continue
        real_image = os.path.realpath(image_path)
        if not real_image.startswith(os.path.realpath(candidate["local_path"]) + os.sep):
            return _failure("COVER_PATH_UNSAFE", "封面路径越出 B 种精确实体目录", b_hash=b_hash)
        evidence = image_file_evidence(real_image)
        if not evidence.get("ok"):
            return _failure("COVER_BROKEN", evidence.get("error") or "本地封面不是有效图片", b_hash=b_hash)
        expected.append({**candidate, "image_path": real_image, **evidence})

    base = {
        "b_hash": b_hash, "torrent_name": name, "torrent_size": total_size,
        "torrent_source": torrent_source, "candidate_pools": [c["pool"] for c in candidates],
    }
    if not expected:
        return _failure("NO_LOCAL_COVER", "实体目录通过身份校验，但没有有效本地封面", **base)
    digests = {str(item["sha256"]) for item in expected}
    if len(digests) != 1:
        return _failure(
            "POOL_COVER_CONFLICT", "多个精确池目录的封面字节不一致，需人工选择", **base,
            candidates=[{"pool": x["pool"], "sha256": x["sha256"]} for x in expected],
        )
    chosen = expected[0]
    return {
        "ok": True, "status": "OK", **base,
        "local_path": chosen["local_path"], "expected_image_path": chosen["image_path"],
        "expected_sha256": chosen["sha256"], "expected_format": chosen["format"],
        "expected_size": chosen["size"], "mirrors": [x["local_path"] for x in expected],
    }


def compare_site_cover(
    identity: Dict[str, Any],
    cover_urls: Sequence[str],
    fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """只把详情页首图当封面；其余插图不参与专辑封面判等。"""
    if not cover_urls:
        return {"status": "COVER_MISSING", "ok": False}
    url = str(cover_urls[0] or "")
    result: Dict[str, Any] = {"url": sanitize_url(url), "ok": False}
    if not identity.get("ok"):
        result.update(status="IDENTITY_UNRESOLVED", error=identity.get("error") or identity.get("status"))
        return result
    fetch = fetcher or CoverImageService.fetch_image_evidence
    actual = fetch(url)
    result["actual"] = actual
    result["expected_sha256"] = identity.get("expected_sha256")
    if not actual.get("ok"):
        result.update(status="COVER_BROKEN", error=actual.get("error") or "站点首图不可用")
    elif actual.get("sha256") != identity.get("expected_sha256"):
        result.update(status="COVER_MISMATCH", error="站点首图与精确实体目录封面 SHA256 不同")
    else:
        result.update(status="MATCH", ok=True, error="")
    return result
