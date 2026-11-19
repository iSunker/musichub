# -*- coding: utf-8 -*-
"""MusicHub 3.0 统一封面与图床服务 (CoverImageService)

落实对齐老版（assets.py / publish.py）的封面与图床铁律：
1. 蟹黄堡独走专用图床（Lsky Pro），绝不跨站混用 PicGo 链接
2. 乌托邦、猫站、星湾、麒麟、唐门、慕雪阁允许公网图床直通（PicGo 等合法公网图链直接复用）
3. 本地缓存防重复（优先命中本地/内存已落库图片或 sha1 缓存），避免重复消耗额度
4. 严格遵循凭据安全铁律：不落盘任何凭据明文，Lsky Token 缓存由内存或受限路径维护
"""

import os
import re
import hashlib
import logging
from typing import Optional, Tuple, Dict, Any
import requests

logger = logging.getLogger("CoverImageService")


class CoverImageService:
    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = cache_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "covers")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._lsky_token = None

    def get_site_cover(
        self,
        target_site: str,
        source_cover_url: str,
        local_dir: Optional[str] = None,
        cached_site_covers: Optional[Dict[str, str]] = None
    ) -> Tuple[str, str]:
        """获取适用于目标站点的封面 URL

        返回: (cover_url, status_desc)
        """
        cached_site_covers = cached_site_covers or {}
        site_lower = target_site.lower()

        # 1. 检查该站是否已有专用缓存记录
        if site_lower in cached_site_covers and cached_site_covers[site_lower]:
            return cached_site_covers[site_lower], f"Reusing cached cover for {target_site}"

        # 2. 蟹黄堡专属图床通道 (Lsky Pro)
        if "crabpt" in site_lower or "crab" in site_lower:
            return self._resolve_crabpt_cover(source_cover_url, local_dir)

        # 3. 其余站点（乌托邦、猫站、星湾、麒麟、唐门、慕雪阁）：
        # 若已有合法的公网 http/https 链接，直接安全复用，免重复消耗上传配额
        clean_url = (source_cover_url or "").strip()
        if clean_url.startswith("http://") or clean_url.startswith("https://"):
            return clean_url, f"Directly reuse valid public cover url for {target_site}"

        # 4. 若传入的是本地文件或无外链，尝试走通用图床 (PicGo/Chevereto)
        if local_dir or (clean_url and os.path.exists(clean_url)):
            local_img = clean_url if os.path.exists(clean_url) else self.find_local_cover(local_dir)
            if local_img and os.path.exists(local_img):
                uploaded_url = self.upload_picgo(local_img)
                if uploaded_url:
                    return uploaded_url, f"Uploaded local image to PicGo for {target_site}"

        return "", f"No usable cover found for {target_site}"

    def _resolve_crabpt_cover(self, source_cover_url: str, local_dir: Optional[str]) -> Tuple[str, str]:
        """蟹黄堡专线：必须使用 Lsky Pro，严禁跨站透传 PicGo 链"""
        # 如果原始链接本身就是 Lsky Pro 域名，允许复用
        lsky_base = os.getenv("LSKY_API_URL", "https://img.crabpt.vip/api/v1")
        lsky_host = lsky_base.split("//")[-1].split("/")[0]
        if source_cover_url and lsky_host in source_cover_url:
            return source_cover_url, "Reusing existing Lsky Pro cover for CrabPT"

        # 寻找本地待上传文件：本地缓存或 local_dir 下的扫图/cover
        local_img = None
        if source_cover_url:
            local_img = self._get_cached_file(source_cover_url)
            if not local_img or not os.path.exists(local_img):
                local_img = self._download_and_cache(source_cover_url)

        if not local_img and local_dir:
            local_img = self.find_local_cover(local_dir)

        if not local_img or not os.path.exists(local_img):
            return "", "Failed to resolve local cover file for Lsky Pro upload"

        # 执行 Lsky Pro 专线上传
        lsky_url = self.upload_lsky(local_img)
        if lsky_url:
            return lsky_url, "Successfully uploaded cover to Lsky Pro for CrabPT"
        return "", "Lsky Pro upload failed"

    def find_local_cover(self, folder_path: Optional[str]) -> Optional[str]:
        """在本地专辑目录中查找封面图片（cover.jpg, folder.jpg, front.jpg 等）"""
        if not folder_path or not os.path.exists(folder_path):
            return None

        candidates = [
            "cover.jpg", "cover.png", "folder.jpg", "front.jpg", "front.png",
            "Cover.jpg", "Folder.jpg", "Front.jpg"
        ]
        # 直接匹配常见名字
        for c in candidates:
            p = os.path.join(folder_path, c)
            if os.path.isfile(p) and os.path.getsize(p) > 1024:
                return p

        # 遍历第一层与 scans/ 目录
        for root, _, files in os.walk(folder_path):
            for f in files:
                low = f.lower()
                if low.endswith((".jpg", ".jpeg", ".png", ".webp")):
                    if any(k in low for k in ("cover", "folder", "front", "scan")):
                        p = os.path.join(root, f)
                        if os.path.getsize(p) > 1024:
                            return p
            # 仅向下探索一层
            if root != folder_path:
                break
        return None

    def _get_cached_file(self, url: str) -> Optional[str]:
        h = hashlib.sha1(url.strip().encode("utf-8")).hexdigest()
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            p = os.path.join(self.cache_dir, f"{h}{ext}")
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return p
        return None

    def _download_and_cache(self, url: str) -> Optional[str]:
        """将外网 URL 封面下载到本地缓存中"""
        try:
            h = hashlib.sha1(url.strip().encode("utf-8")).hexdigest()
            target_path = os.path.join(self.cache_dir, f"{h}.jpg")
            resp = requests.get(url, timeout=20, stream=True)
            if resp.status_code == 200:
                with open(target_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                if os.path.getsize(target_path) > 1024:
                    return target_path
        except Exception as e:
            logger.warning(f"Failed to download and cache cover from {url}: {e}")
        return None

    def upload_picgo(self, file_path: str) -> Optional[str]:
        """上传至通用 PicGo/Chevereto 图床"""
        api_key = os.getenv("PICGO_API_KEY")
        api_url = os.getenv("PICGO_API_URL", "https://picgo.net/api/1/upload")
        if not api_key:
            return None
        try:
            with open(file_path, "rb") as f:
                resp = requests.post(
                    api_url,
                    headers={"X-API-Key": api_key},
                    files={"source": f},
                    timeout=30
                )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("image", {}).get("url") or data.get("result", {}).get("url")
        except Exception as e:
            logger.warning(f"PicGo upload failed: {e}")
        return None

    def upload_lsky(self, file_path: str) -> Optional[str]:
        """上传至 Lsky Pro 图床（专供 CrabPT）"""
        token = os.getenv("LSKY_TOKEN") or self._lsky_token
        api_url = os.getenv("LSKY_API_URL", "https://img.crabpt.vip/api/v1")
        upload_endpoint = f"{api_url.rstrip('/')}/upload"

        # 若无 token 则尝试从 LSKY_EMAIL 与 LSKY_PASSWORD 自动获取
        if not token:
            email = os.getenv("LSKY_EMAIL")
            pwd = os.getenv("LSKY_PASSWORD")
            if email and pwd:
                try:
                    tok_resp = requests.post(
                        f"{api_url.rstrip('/')}/tokens",
                        json={"email": email, "password": pwd},
                        timeout=15
                    )
                    if tok_resp.status_code == 200:
                        token = tok_resp.json().get("data", {}).get("token")
                        self._lsky_token = token
                except Exception as e:
                    logger.error(f"Failed to acquire Lsky token: {e}")

        if not token:
            logger.warning("No Lsky Pro token available; cannot upload CrabPT cover")
            return None

        try:
            with open(file_path, "rb") as f:
                resp = requests.post(
                    upload_endpoint,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    files={"file": (os.path.basename(file_path), f)},
                    timeout=30
                )
            if resp.status_code == 200:
                res_data = resp.json()
                if res_data.get("status"):
                    return res_data.get("data", {}).get("links", {}).get("url")
            logger.warning(f"Lsky Pro returned reject: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Lsky Pro upload error: {e}")
        return None
