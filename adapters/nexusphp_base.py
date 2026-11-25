# -*- coding: utf-8 -*-
"""MusicHub 3.0 通用 NexusPHP 站点基础适配器 (NexusPHPBaseAdapter)

严格遵循规范：
1. 统一网络层（强制 IPv4 解析、session.trust_env = False 直连光猫、反反爬 UA）
2. CookieCloud 登录态挂载与存活检测
3. 高精准查重矩阵（严格对齐老版精准分词与 info.name 比对，拒绝模糊标题假阳/假阴）
4. BBCode 标准简介组装（build_body）与图链规避/转存
5. Multipart 表单提交（publish/takeupload）
6. 严格守卫：.!qB 拒发护栏、3010 屏蔽、出种保活、同名冲突与失败快照落盘
7. 【重点 3：细粒度分诊自愈体系】区分 SITE_DOWN 站点侧故障 vs REJECT 内容被拒，并提供精准人话诊断建议
"""

import os
import re
import unicodedata
import logging
from typing import Dict, List, Optional, Tuple, Any

import requests
from bs4 import BeautifulSoup
from musichub.adapters.base import BaseSiteAdapter
from musichub.core.models import CanonicalMusicItem
from musichub.metadata.cookiecloud import CookieCloudClient
from musichub.metadata.cover_service import CoverImageService

logger = logging.getLogger("NexusPHPAdapter")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 细粒度拒绝分诊知识库（对齐老版 publish.py _REJECT_HINTS）
REJECT_HINTS = (
    # ★ 严禁把「上传失败」当同名特征：NexusPHP 的失败页标题恒为「上传失败！」，
    # 真正原因写在正文（如「请填写必填项目: url_vimages」）。旧实现用它判同名，
    # 把所有必填缺失/表单错误全部误记成 EXISTING 并永久阻断重发（2026-11-25 实测）。
    ("同名冲突", ("同名", "已存在", "已经存在", "duplicate", "existed"),
     "站点上有 info.name 逐字相同的条目。验证：在站点搜索该 info.name 或改名后重试。"),
    ("必填项缺失", ("请选择", "未选择", "必须选择", "不能为空", "请填写"),
     "表单某格为空（常见：medium_sel/audiocodec_sel 等分类下标错误）。"),
    ("权限配额限制", ("权限", "不允许", "配额", "超过", "无权", "没有权限", "limit"),
     "账号等级或今日发种配额限制。请检查站点个人信息或发种数。"),
    ("种子结构被拒", ("无效", "不是合法的", "种子文件", "torrent", "无法识别", "corrupt"),
     "上传的 .torrent 文件本身损坏或解析失败。请重新打种。"),
)

# 「站点已有」语义前缀：NexusPHP 的两种重复（同 infohash 302 existed=1 / 同 info.name 200「上传失败」）
# 都属于「站点上已经有了」，不是我方内容错误。返回该前缀让调用方按 EXISTING 记账并跳过，
# 避免同一专辑被无限重试（老项目 publish.py 对同版本命中即关联已有页、不再提交）。
DUPLICATE_NAME_PREFIX = "DUPLICATE_NAME:"


def is_duplicate_name_result(msg: str) -> bool:
    """判断适配器 publish() 的返回是否为「站点已有同名/同种条目」"""
    return str(msg or "").startswith(DUPLICATE_NAME_PREFIX)


def extract_existing_tid(msg: str) -> str:
    """从错误信息或诊断文本中提取数字 TID（支持 [EXISTING_TID:xxx]、details.php?id=xxx 与尾随 :xxx）"""
    m = re.search(r"\[EXISTING_TID:(\d+)\]", str(msg or ""))
    if m:
        return m.group(1)
    m = re.search(r"details\.php\?id=(\d+)", str(msg or ""))
    if m:
        return m.group(1)
    m = re.search(r":(\d+)$", str(msg or "").strip())
    if m:
        return m.group(1)
    return ""


class NexusPHPBaseAdapter(BaseSiteAdapter):
    # 元数据严格度：True 表示源种元数据未补齐（NEED_META）时不发。
    # 默认宽松；由各站适配器按站点要求覆写（如 OpenCD 目标站要求 artist/album/year 齐全）。
    metadata_strict = False

    # 站点是否有**独立的封面表单字段**（发布页 `cover` 上传位）。
    # 有字段的站点（OpenCD）封面不走简介 `[img]`，因此简介里不该写"无封面"说明
    # —— 封面上传是否成功由那条链路自己保证。其余站点（乌托邦等）封面只能靠
    # 简介首图，缺图时必须在简介里显式说明（2026-11-21 用户要求）。
    has_cover_field = False

    def __init__(
        self,
        site_name: str,
        domain: str,
        base_url: str,
        cookie_client: Optional[CookieCloudClient] = None,
        cover_service: Optional[CoverImageService] = None,
        force_ipv4: bool = True
    ):
        super().__init__(site_name=site_name, config={"domain": domain, "base_url": base_url})
        self.domain = domain
        self.base_url = base_url.rstrip("/")
        self.cookie_client = cookie_client or CookieCloudClient()
        self.cover_service = cover_service or CoverImageService()
        self.force_ipv4 = force_ipv4
        self._init_session()

    def _init_session(self):
        """初始化统一 requests.Session
        
        遵循《家庭网络》项目铁律二、六、九与 PT-Project 规约：
        1. 强制锁 IPv4，杜绝客户端 IPv6 解析跑飞
        2. trust_env = False：显式脱钩宿主/系统环境变量代理（HTTP_PROXY/HTTPS_PROXY），
           防止内网直连站点被外部代理劫持或产生 502/SSL 握手超时。
        """
        if self.force_ipv4:
            try:
                import urllib3.util.connection as u3c
                import socket
                u3c.allowed_gai_family = lambda: socket.AF_INET
            except Exception:
                pass

        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    def get_auth_cookies(self) -> Dict[str, str]:
        """从 CookieCloud 获取本站的最新认证 Cookie"""
        return self.cookie_client.get_domain_cookies(self.domain)

    def _prepare_headers(self) -> Dict[str, str]:
        """从 CookieCloud 装配最新 Cookie 请求头"""
        cookie_header = self.cookie_client.get_cookie_header(self.domain)
        headers = {}
        if cookie_header:
            headers["Cookie"] = cookie_header
        return headers

    @staticmethod
    def normalize_name(name: str) -> str:
        """规范化名称（NFC + 折叠连续空白），与老版 site_verify.py 保持完全一致"""
        if not isinstance(name, str):
            return ""
        s = unicodedata.normalize("NFC", name)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def build_search_queries(self, item: CanonicalMusicItem) -> List[str]:
        """生成精准优先的站内查重搜索串列表 (T0 窄搜索优先，避免宽搜索噪声挤占预算)"""
        queries = []
        artist = (item.artist or "").strip()
        album = (item.album or "").strip()
        
        if artist and album and artist != "Various Artists":
            queries.append(f"{artist} {album}".strip())
        if album:
            queries.append(album)
            
        if item.title:
            clean_title = re.sub(r"\[.*?\]|\(.*?\)", " ", item.title).strip()
            clean_title = re.sub(r"\s+", " ", clean_title)
            if clean_title and clean_title not in queries:
                queries.append(clean_title)
                
        return queries[:3]

    def check_exists_detail(self, item: CanonicalMusicItem) -> Tuple[bool, str, Optional[str]]:
        """通用 NexusPHP 站内搜索查重契约：返回 (是否存在, 匹配说明, 匹配到的数字TID)"""
        search_url = f"{self.base_url}/torrents.php"
        headers = self._prepare_headers()

        if "Cookie" not in headers:
            logger.warning(f"No cookies found for {self.site_name} ({self.domain}), bypass check_exists")
            return False, "No cookies", None

        queries = self.build_search_queries(item)
        norm_album = self.normalize_name(item.album).lower()
        norm_artist = self.normalize_name(item.artist).lower()

        for q in queries:
            try:
                resp = self.session.get(
                    search_url,
                    params={"search": q},
                    headers=headers,
                    timeout=15,
                    verify=False
                )
                if resp.status_code != 200:
                    continue
                
                html = resp.text

                # 自动快照落盘并嗅探该站点全站 Free 横幅
                try:
                    from musichub.metadata.snapshot_manager import RawHtmlSnapshotManager
                    from musichub.collector.free_window import SiteFreeWindowDetector
                    snap_mgr = RawHtmlSnapshotManager()
                    snap_mgr.save_snapshot(self.site_name, f"search_{int(time.time())}", html)
                    banner_promo = SiteFreeWindowDetector.detect_global_free_from_html(html)
                    if banner_promo.is_free:
                        logger.info(f"[{self.site_name}] Detected active GLOBAL FREE window! (expire_at={banner_promo.expire_at})")
                except Exception:
                    pass

                if "没有种子" in html or "Nothing found" in html or "No torrents found" in html:
                    continue

                soup = BeautifulSoup(html, "html.parser")
                rows = soup.select("table.torrents tr")
                for r in rows:
                    title_elem = r.select_one("a[href*='details.php?id=']")
                    if not title_elem:
                        continue
                    candidate_title = title_elem.get_text(" ", strip=True)
                    norm_candidate = self.normalize_name(candidate_title).lower()

                    if norm_album and norm_album in norm_candidate:
                        if norm_artist in ("various artists", "群星") or norm_artist in norm_candidate:
                            href = title_elem.get("href", "")
                            m_tid = re.search(r"details\.php\?id=(\d+)", href)
                            matched_tid = m_tid.group(1) if m_tid else None
                            return True, f"Matched existing torrent on {self.site_name}: '{candidate_title}'", matched_tid
            except Exception as e:
                logger.error(f"Error checking exists on {self.site_name} with query '{q}': {e}")
                continue

        return False, "No matching torrent found on target site", None

    def check_exists(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        """通用 NexusPHP 站内搜索查重契约（兼容老接口返回 2 元组）"""
        exists, why, _ = self.check_exists_detail(item)
        return exists, why

    def format_title(self, item: CanonicalMusicItem) -> Tuple[str, str]:
        """优先使用源种原标题（底线合规过滤：仅剥离分轨/整轨与私人压制者后缀），副标题标准化输出"""
        import musichub.adapters.descr_core as descr_core
        # 组装适配 descr_core 的 task 结构
        task = {
            "formatted_title": item.origin_title or item.title,
            "torrent_name": item.origin_title or item.title,
            "artist": item.artist,
            "album": item.album,
            "year": item.year,
            "format": item.format,
            "medium": item.medium,
            "standard": item.standard,
            "small_descr": item.edition or "",
            "tracklist": "\n".join(f"{t.track_number:02d}. {t.title}" for t in item.tracks) if item.tracks else "",
            "origin_site": item.origin_site or "",
            "origin_tid": item.origin_torrent_id or "",
            "opencd_tid": item.origin_torrent_id if item.origin_site == "opencd" else "",
        }
        raw_cand = (item.origin_title or item.title or "").strip()
        main_title = descr_core.clean_origin_music_title(raw_cand, fallback_task=task)
        small_descr = descr_core.build_music_small_descr(task)
        return main_title, small_descr

    def build_body(self, item: CanonicalMusicItem) -> str:
        """使用父项目统一排版内核生成标准化 BBCode 简介"""
        import musichub.adapters.descr_core as descr_core
        # 封面策略：源站图链有效即复用；仅无图/失效才扫本地补图并上传（按站规路由）
        cover_url, cover_why = self.cover_service.get_site_cover(
            target_site=self.site_name,
            source_cover_url=item.cover_url,
            local_dir=item.local_path,
            source_site=item.origin_site,
            source_tid=item.origin_torrent_id,
            source_name=item.title,
        )
        logger.info(f"[{self.site_name}] 封面 ← {cover_why}")
        # Phase 4: 外源真源头穿透解析（避免中继站套娃）
        c_site, c_tid, c_custom_link = descr_core.resolve_canonical_source(
            descr=item.description,
            default_site=item.origin_site or "",
            default_tid=item.origin_torrent_id or "",
            title=item.origin_title or item.title or "",
        )

        task = {
            "formatted_title": item.title,
            "torrent_name": item.title,
            "artist": item.artist,
            "album": item.album,
            "year": item.year,
            "format": item.format,
            "medium": item.medium,
            "cover_url": cover_url,
            "origin_site": c_site,
            "origin_tid": c_tid,
            "opencd_tid": c_tid if c_site == "opencd" else "",
        }
        source_link = c_custom_link or descr_core.build_source_link(task)
        tracklist_text = "\n".join(f"{t.track_number:02d}. {t.title}" for t in item.tracks) if item.tracks else ""

        site_code = self.site_name.lower()
        if "pter" in site_code:
            site_key = "pter"
        elif "crab" in site_code:
            site_key = "crabpt"
        elif "musopia" in site_code:
            site_key = "musopia"
        elif "xingwan" in site_code:
            site_key = "xingwan"
        else:
            site_key = None

        # 无独立封面字段的站点：真缺图时在简介里写明「源站和本地均无封面」
        # ★ 护栏：若本地物理目录其实存在图片，严禁谎称“本地无封面”
        has_physical_local_cover = False
        if not cover_url and item.local_path and os.path.isdir(item.local_path):
            try:
                for fname in os.listdir(item.local_path):
                    fl = fname.lower()
                    if fl.endswith((".jpg", ".jpeg", ".png", ".webp")) and any(
                        k in fl for k in ("cover", "folder", "front", "artwork", "poster", "cd", "album")
                    ):
                        has_physical_local_cover = True
                        break
            except Exception as e:
                logger.debug(f"检查本地物理封面异常: {e}")

        if has_physical_local_cover:
            logger.warning(f"[{self.site_name}] 专辑本地存在物理封面但未获得图床 URL，熔断静默降级，不输出虚假无封面声明")
            no_cover_note = ""
        else:
            no_cover_note = "" if self.has_cover_field else descr_core.NO_COVER_NOTE

        raw_log = getattr(item, "log_text", "") or ""
        log_blocks = descr_core.split_log_blocks(
            log_text=raw_log,
            local_path=getattr(item, "local_path", "") or "",
        )
        return descr_core.build_descr(
            cover_url=cover_url,
            tracklist=tracklist_text,
            log_content=raw_log if not log_blocks else "",
            logs=log_blocks or None,
            opencd_descr=item.description,
            site=site_key,
            source_link=source_link,
            no_cover_note=no_cover_note,
        )

    def _extract_site_error(self, html: str) -> str:
        """从 NexusPHP 响应中精准抠出错误信息，兼顾页面正文与 <title>

        ★ 站点失败页标题恒为「上传失败！」/「错误」，真正原因在正文（如
        「你没有自由上传的权限！」「请填写必填项目: url_vimages」）。若只取标题，
        下游分诊会把权限/必填错误统统一归为未分类甚至同名冲突，因此正文优先。
        """
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        generic = ("上传失败", "错误", "Error", "Upload failed")
        keyword = ("错误", "Error", "失败", "请选择", "必须", "请填写", "权限", "不能",
                   "不得", "无法", "不允许", "超过", "重复", "已经存在", "无权")
        best = ""
        for el in soup.find_all(["td", "div", "p", "h1", "h2", "h3", "span", "li"]):
            txt = el.get_text(" ", strip=True)
            if not txt or len(txt) > 200:
                continue
            if not any(k in txt for k in keyword):
                continue
            if txt in generic:
                continue
            if len(txt) > len(best):
                best = txt
        if best:
            return best
        for sel in ("#outer table td.text", "td.embedded", "h2", "h1"):
            el = soup.select_one(sel)
            if el:
                txt = el.get_text(" ", strip=True)
                if txt and len(txt) < 300 and any(k in txt for k in keyword):
                    return txt
        title_tag = soup.find("title")
        if title_tag:
            t_txt = title_tag.get_text(strip=True)
            if any(k in t_txt for k in generic):
                return t_txt
        return "Unknown error (empty error container)"

    def diagnose_reject_reason(self, raw_msg: str, local_name: str = "") -> str:
        """【重点 3 核心：拒绝原因诊断器】把站点返回的报错文本翻译成人话处置建议"""
        msg = raw_msg or ""
        for kind, needles, advice in REJECT_HINTS:
            if any(n in msg for n in needles):
                if kind == "同名冲突":
                    return f"[{kind}] {advice} (待发本地名称={local_name!r})"
                return f"[{kind}] {advice}"
        return f"[未分类] 站点文案无已知特征 ({raw_msg})，请核对详情页快照"

    def _dump_failure_html(self, html: str, torrent_path: str):
        try:
            dump_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "raw_html")
            os.makedirs(dump_dir, exist_ok=True)
            base = os.path.basename(torrent_path).replace(".torrent", "")
            dump_file = os.path.join(dump_dir, f"{self.site_name}_{base}_fail.html")
            with open(dump_file, "w", encoding="utf-8") as f:
                f.write(html)
            logger.info(f"Dumped failure HTML response to {dump_file}")
        except Exception as e:
            logger.warning(f"Failed to dump failure html: {e}")

    def download_torrent(self, torrent_id: str) -> Optional[bytes]:
        """按需从站点 download.php 下载 B 种二进制流"""
        if not torrent_id or torrent_id == "EXISTING":
            return None
        headers = self._prepare_headers()
        if "Cookie" not in headers:
            logger.warning(f"Missing Cookie credentials for {self.domain}")
            return None
        url = f"{self.base_url}/download.php?id={torrent_id}"
        try:
            resp = self.session.get(url, headers=headers, timeout=30, verify=False)
            if resp.status_code == 200 and len(resp.content) > 100:
                return resp.content
            logger.warning(f"Download torrent failed on {self.site_name} #{torrent_id}: HTTP {resp.status_code}")
            return None
        except Exception as e:
            logger.error(f"Network error downloading torrent from {self.site_name} #{torrent_id}: {e}")
            return None

    def publish(self, item: CanonicalMusicItem, torrent_path: str) -> Tuple[bool, str, Optional[bytes]]:
        """提交发布通用实现，严格分流 SITE_DOWN vs REJECT"""
        chk_ok, chk_msg = self.pre_publish_check(item)
        if not chk_ok:
            logger.warning(f"Blocked by {self.site_name} pre-publish check: {chk_msg}")
            return False, f"REJECT: Pre-publish check failed: {chk_msg}", None

        if not os.path.exists(torrent_path) or os.path.getsize(torrent_path) == 0:
            return False, f"REJECT: Invalid torrent file on path: {torrent_path}", None

        if ".!qB" in torrent_path or any(".!qB" in t.file_path for t in item.tracks):
            return False, "REJECT: Blocked by .!qB guardrail (download not fully completed)", None

        headers = self._prepare_headers()
        if "Cookie" not in headers:
            return False, f"SITE_DOWN: Missing CookieCloud credentials for {self.domain} (LoginExpired)", None

        fields = self.build_form_fields(item)
        upload_url = f"{self.base_url}/takeupload.php"

        try:
            with open(torrent_path, "rb") as f:
                files = {"file": (os.path.basename(torrent_path), f, "application/x-bittorrent")}
                resp = self.session.post(
                    upload_url,
                    data=fields,
                    files=files,
                    headers=headers,
                    timeout=45,
                    verify=False,
                    allow_redirects=True
                )

            # 优先从重定向历史或最终 URL 提取种子 ID（防止详情页偶发权限拦截跳登录页）
            tid = None
            for h in resp.history:
                loc = h.headers.get("Location", "")
                m = re.search(r"details\.php\?id=(\d+)", loc)
                if m:
                    tid = m.group(1)
                    if "existed=1" in loc or "existed" in loc:
                        return False, f"{DUPLICATE_NAME_PREFIX} [EXISTING_TID:{tid}] Torrent already exists on {self.site_name} (details.php?id={tid})", None
                    break

            final_url = resp.url
            if not tid:
                m = re.search(r"details\.php\?id=(\d+)", final_url)
                if m:
                    tid = m.group(1)
                    if "existed=1" in final_url or "existed" in final_url:
                        return False, f"{DUPLICATE_NAME_PREFIX} [EXISTING_TID:{tid}] Torrent already exists on {self.site_name} (details.php?id={tid})", None

            # 检查站点是否直接退回登录页或 5xx 故障
            if resp.status_code in (500, 502, 503, 504):
                return False, f"SITE_DOWN: HTTP status {resp.status_code} on takeupload", None
            if "login.php" in resp.url and not tid:
                return False, "SITE_DOWN: Redirected to login.php (Cookie expired)", None

            resp_text = resp.text

            if not tid:
                self._dump_failure_html(resp_text, torrent_path)
                raw_err = self._extract_site_error(resp_text)
                # 仅从站点错误提示正文提取 EXISTING_TID，严禁全文盲搜被页头公告链接误导
                m_body = re.search(r"details\.php\?id=(\d+)", raw_err or "")
                body_tid = m_body.group(1) if m_body else ""
                diagnosed = self.diagnose_reject_reason(raw_err, local_name=item.title)
                if diagnosed.startswith("[同名冲突]") or "已经存在" in resp_text or "already exists" in resp_text.lower():
                    # 站点上已有同 info.name 条目 ⇒ 按「已在站」处理，提取数字 TID 避免丢失
                    tid_tag = f" [EXISTING_TID:{body_tid}]" if body_tid else ""
                    return False, f"{DUPLICATE_NAME_PREFIX}{tid_tag} {diagnosed}", None
                return False, f"REJECT: Publish rejected by {self.site_name}: {diagnosed}", None

            torrent_id = tid
            logger.info(f"Published to {self.site_name} successfully, torrent_id={torrent_id}")

            download_url = f"{self.base_url}/download.php?id={torrent_id}"
            dl_resp = self.session.get(download_url, headers=headers, timeout=30, verify=False)
            pub_torrent_bytes = dl_resp.content if dl_resp.status_code == 200 else None

            # 自动将刚发布获取到的 B 种补充详情页 Comment 注释并落盘归档，确保客户端显示种子链接且防断种
            if pub_torrent_bytes and len(pub_torrent_bytes) > 100:
                try:
                    from musichub.core.torrent_tools import inject_torrent_comment
                    details_url = f"{self.base_url}/details.php?id={torrent_id}"
                    pub_torrent_bytes = inject_torrent_comment(pub_torrent_bytes, details_url)
                except Exception as c_err:
                    logger.debug(f"Failed to inject torrent comment: {c_err}")

                try:
                    torrents_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "torrents")
                    os.makedirs(torrents_dir, exist_ok=True)
                    target_b_path = os.path.join(torrents_dir, f"{self.site_name.lower()}_{torrent_id}.torrent")
                    with open(target_b_path, "wb") as bf:
                        bf.write(pub_torrent_bytes)
                    logger.info(f"Auto-archived site torrent B with details comment to {target_b_path}")
                except Exception as archive_err:
                    logger.warning(f"Failed to auto-archive site torrent B: {archive_err}")

            return True, torrent_id, pub_torrent_bytes

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error during takeupload on {self.site_name}: {e}")
            return False, f"SITE_DOWN: Network exception {type(e).__name__}: {str(e)}", None
        except Exception as e:
            logger.error(f"Publish failed on {self.site_name}: {str(e)}")
            return False, f"REJECT: Publish request error: {str(e)}", None
