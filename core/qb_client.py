# -*- coding: utf-8 -*-
"""MusicHub 3.0 轻量级 qBittorrent 客户端适配器 (QBClient)

专用于：
1. 监控缓冲下载器 musicdownload (3016)
2. 操作发种端 publish (3010)

遵循 AGENTS.md 铁律：
- 严格开启 AddTorrentStopped=true（默认暂停）
- 支持 skip_checking=True 秒做种
- 提供探针方法轮询做种健康状态
"""

import urllib.request
import urllib.parse
import json
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("QBClient")


class QBClient:
    def __init__(self, host: str = "192.168.0.7", port: int = 3016, username: str = "", password: str = ""):
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}/api/v2"
        self.username = username
        self.password = password
        # 内网 LAN 请求显式绕过代理 (ProxyHandler({}))，避免走 mihomo 产生 502 Bad Gateway
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor()
        )

    def login(self) -> bool:
        """登录 qB WebUI"""
        login_url = f"{self.base_url}/auth/login"
        data = urllib.parse.urlencode({
            "username": self.username,
            "password": self.password,
        }).encode("utf-8")

        req = urllib.request.Request(login_url, data=data)
        try:
            with self.opener.open(req, timeout=10) as resp:
                text = resp.read().decode("utf-8").strip()
                return text == "Ok."
        except Exception as e:
            logger.error(f"qB login failed on {self.base_url}: {str(e)}")
            return False

    def get_torrents(
        self,
        filter_status: Optional[str] = None,
        tag: Optional[str] = None,
        hashes: Optional[str] = None,
        strict: bool = False,
    ) -> List[Dict]:
        """获取种子列表

        strict=True 时网络/解析异常直接抛出，供 GC 等「读不到就不能删」的
        fail-closed 场景使用；默认仍保持宽松（失败返回空列表）。
        """
        url = f"{self.base_url}/torrents/info"
        params = {}
        if filter_status:
            params["filter"] = filter_status
        if tag:
            params["tag"] = tag
        if hashes:
            params["hashes"] = hashes
        if params:
            url += "?" + urllib.parse.urlencode(params)

        req = urllib.request.Request(url)
        try:
            with self.opener.open(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.error(f"Failed to fetch torrents from {self.base_url}: {str(e)}")
            if strict:
                raise
            return []

    def export_torrent(self, info_hash: str) -> Optional[bytes]:
        """从 qB 导出种子元数据（用于存量种子的池配对回填）

        qB 的 `/torrents/export` 并非所有版本都提供，失败返回 None，
        调用方应回退到本地归档 `data/torrents/*.torrent`。
        """
        clean = (info_hash or "").strip()
        if not clean:
            return None
        url = f"{self.base_url}/torrents/export?" + urllib.parse.urlencode({"hash": clean})
        req = urllib.request.Request(url)
        try:
            with self.opener.open(req, timeout=20) as resp:
                data = resp.read()
                if resp.getcode() == 200 and len(data) > 50 and data[:1] == b"d":
                    return data
        except Exception as e:
            logger.debug(f"Export torrent {clean} from {self.base_url} failed: {e}")
        return None

    def add_torrent(
        self,
        torrent_bytes: bytes,
        save_path: str = "/downloads",
        is_paused: bool = True,
        is_skip_checking: bool = False,
        tags: Optional[str] = None,
        **kwargs,
    ) -> bool:
        """向 qB 添加种子任务

        遵循铁律：默认 is_paused=True，由上层校验磁盘并就绪后再唤醒。
        同时支持兼容参数：paused, skip_checking。
        """
        # 兼容处理入参命名
        paused = kwargs.get("paused", is_paused)
        skip_checking = kwargs.get("skip_checking", is_skip_checking)
        auto_tmm = kwargs.get("auto_tmm")
        if auto_tmm is not None:
            auto_tmm = str(auto_tmm).strip().lower() in ("1", "true", "yes", "on")

        url = f"{self.base_url}/torrents/add"
        boundary = "----WebKitFormBoundaryMusicHub7MA4YWxkTrZu0gW"
        
        body = []
        def add_field(name: str, val: str):
            body.append(f"--{boundary}\r\n".encode("utf-8"))
            body.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            body.append(f"{val}\r\n".encode("utf-8"))

        add_field("savepath", save_path)
        add_field("paused", "true" if paused else "false")
        # qB 5.x 起 `/torrents/add` 的暂停参数改名为 stopped，4.x 只认 paused。
        # 两个都发：否则 5.2.4 会忽略 paused，按全局 AddTorrentStopped=true 落成暂停，
        # 源种搬进池后永远不跑（2026-11-21 源种不做种的隐性成因之一）。
        add_field("stopped", "true" if paused else "false")
        add_field("skip_checking", "true" if skip_checking else "false")
        # 显式关闭 AutoTMM 可防止类目/默认路径覆盖我们指定的 savepath（池配对必须精确落盘）
        if auto_tmm is not None:
            add_field("autoTMM", "true" if auto_tmm else "false")
        if tags:
            add_field("tags", tags)

        # 添加二进制 torrent 文件
        body.append(f"--{boundary}\r\n".encode("utf-8"))
        body.append(b'Content-Disposition: form-data; name="torrents"; filename="upload.torrent"\r\n')
        body.append(b"Content-Type: application/x-bittorrent\r\n\r\n")
        body.append(torrent_bytes)
        body.append(b"\r\n")
        body.append(f"--{boundary}--\r\n".encode("utf-8"))

        data = b"".join(body)
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(data)),
            },
        )
        try:
            with self.opener.open(req, timeout=15) as resp:
                return resp.getcode() == 200
        except Exception as e:
            logger.error(f"Failed to add torrent on {self.base_url}: {str(e)}")
            return False

    def add_torrent_file(
        self,
        torrent_bytes: bytes,
        save_path: str = "/downloads",
        skip_checking: bool = False,
        paused: bool = True,
        tags: Optional[str] = None,
        **kwargs,
    ) -> bool:
        """对齐各调用方习惯的别名入口"""
        return self.add_torrent(
            torrent_bytes=torrent_bytes,
            save_path=save_path,
            is_paused=paused,
            is_skip_checking=skip_checking,
            tags=tags,
            **kwargs,
        )

    def resume_torrent(self, hashes: str) -> bool:
        """唤醒做种/下载任务（兼容 qB v4.x 的 resume 与 v5.x 的 start）"""
        # qB v5.x 推荐 /torrents/start，v4.x 使用 /torrents/resume
        for endpoint in ["start", "resume"]:
            url = f"{self.base_url}/torrents/{endpoint}"
            data = urllib.parse.urlencode({"hashes": hashes}).encode("utf-8")
            req = urllib.request.Request(url, data=data)
            try:
                with self.opener.open(req, timeout=10) as resp:
                    if resp.getcode() == 200:
                        return True
            except Exception:
                continue
        logger.error(f"Failed to resume/start torrent {hashes} via API")
        return False

    def stop_torrent(self, hashes: str) -> bool:
        """停源（qB v5 用 /stop，v4 用 /pause）；停源会向 tracker 汇报 stopped"""
        for endpoint in ["stop", "pause"]:
            url = f"{self.base_url}/torrents/{endpoint}"
            data = urllib.parse.urlencode({"hashes": hashes}).encode("utf-8")
            req = urllib.request.Request(url, data=data)
            try:
                with self.opener.open(req, timeout=10) as resp:
                    if resp.getcode() == 200:
                        return True
            except Exception:
                continue
        logger.error(f"Failed to stop torrent {hashes} via API")
        return False

    def get_trackers(self, info_hash: str) -> List[Dict]:
        """查询单个任务的 tracker 明细（含 status / msg，用于站点告警分诊）"""
        url = f"{self.base_url}/torrents/trackers?" + urllib.parse.urlencode({"hash": info_hash})
        req = urllib.request.Request(url)
        try:
            with self.opener.open(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8")) or []
        except Exception as e:
            logger.error(f"Failed to fetch trackers of {info_hash} from {self.base_url}: {e}")
            return []

    def recheck_torrent(self, hashes: str) -> bool:
        """重新校验任务数据"""
        url = f"{self.base_url}/torrents/recheck"
        data = urllib.parse.urlencode({"hashes": hashes}).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        try:
            with self.opener.open(req, timeout=10) as resp:
                return resp.getcode() == 200
        except Exception as e:
            logger.error(f"Failed to recheck torrent {hashes}: {str(e)}")
            return False

    def delete_torrent(self, hashes: str, delete_files: bool = False) -> bool:
        """删除任务

        严格遵守铁律：delete_files 默认强制为 False，绝不级联物理删除！
        """
        url = f"{self.base_url}/torrents/delete"
        data = urllib.parse.urlencode({
            "hashes": hashes,
            "deleteFiles": "true" if delete_files else "false"
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        try:
            with self.opener.open(req, timeout=10) as resp:
                return resp.getcode() == 200
        except Exception as e:
            logger.error(f"Failed to delete torrent {hashes}: {str(e)}")
            return False
