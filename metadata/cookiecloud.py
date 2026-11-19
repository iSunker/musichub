# -*- coding: utf-8 -*-
"""MusicHub 3.0 CookieCloud 凭据同步与 Cookie 管理工具

遵循 AGENTS.md 与 cookiecloud-decrypt skill：
- 严格在运行内存中解密，不回显、不落盘任何凭据明文
- 按域名自动组装有效 Cookie 字典与请求头
"""

import os
import glob
import json
import hashlib
import base64
import time
from typing import Dict, List, Optional
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


class CookieCloudClient:
    def __init__(self, data_dir: Optional[str] = None):
        if data_dir:
            self.data_dir = data_dir
        else:
            # 优先检查容器内挂载路径 /data/cookiecloud 或 /volume2/...，最后回退到 Windows SMB 共享
            candidate_dirs = [
                "/data/cookiecloud",
                "/volume2/docker_ssd/cookiecloud/data",
                r"\\iSunker-DS423\docker_ssd\cookiecloud\data",
                os.getenv("COOKIECLOUD_DATA_DIR", "")
            ]
            chosen = ""
            for cd in candidate_dirs:
                if cd and os.path.exists(cd):
                    chosen = cd
                    break
            self.data_dir = chosen or "/data/cookiecloud"

    def get_domain_cookies(self, target_domain: str) -> Dict[str, str]:
        """获取指定域名的全部有效 Cookie 字典"""
        all_cookies = self.load_all_cookies()
        result = {}
        now = time.time()

        for domain, cookie_list in all_cookies.items():
            if target_domain.lower() in domain.lower():
                if isinstance(cookie_list, list):
                    for c in cookie_list:
                        exp = c.get("expirationDate")
                        if exp is not None and exp < now:
                            continue
                        name = c.get("name")
                        val = c.get("value")
                        if name and val:
                            result[name] = val
        return result

    def get_cookie_header(self, target_domain: str) -> str:
        """获取目标域名的 'name=val; name2=val2' 格式 Header 串"""
        cdict = self.get_domain_cookies(target_domain)
        return "; ".join([f"{k}={v}" for k, v in cdict.items()])

    def load_all_cookies(self) -> Dict[str, list]:
        """从 CookieCloud 数据目录读取凭据并解密载荷"""
        info_path = os.path.join(self.data_dir, "浏览器端信息.txt")
        if not os.path.exists(info_path):
            raise FileNotFoundError(f"CookieCloud info file not found: {info_path}")

        uuid, password = None, None
        with open(info_path, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
            for i, l in enumerate(lines):
                if any(k in l for k in ["用户KEY", "UUID", "用户 KEY"]):
                    uuid = lines[i + 1]
                elif any(k in l for k in ["密码", "端对端"]):
                    password = lines[i + 1]

        if not uuid or not password:
            raise ValueError("Could not parse UUID or password from 浏览器端信息.txt")

        json_files = glob.glob(os.path.join(self.data_dir, "*.json"))
        if not json_files:
            raise FileNotFoundError(f"No payload JSON found in: {self.data_dir}")

        with open(json_files[0], "r", encoding="utf-8") as f:
            payload = json.load(f)

        encrypted = payload.get("encrypted")
        if not encrypted:
            raise ValueError("No encrypted data found in CookieCloud JSON")

        key16 = hashlib.md5(f"{uuid}-{password}".encode()).hexdigest()[:16]
        raw = base64.b64decode(encrypted)
        salt = raw[8:16]

        d, prev = b"", b""
        while len(d) < 48:
            prev = hashlib.md5(prev + key16.encode() + salt).digest()
            d += prev

        cipher = AES.new(d[:32], AES.MODE_CBC, d[32:48])
        data = json.loads(unpad(cipher.decrypt(raw[16:]), AES.block_size).decode())
        return data.get("cookie_data", {})
