# -*- coding: utf-8 -*-
"""SpringSunday (SSD / 春天) 站点适配器

遵循 SSD NexusPHP 实测契约 (https://springsunday.net)：
1. 音乐分类: type=508 (Music(音乐))
2. 扁平表单字段（不带 [4]）:
   - source_sel: 1=大陆, 2=香港, 3=台湾, 4=欧美, 5=日本, 6=韩国, 99=Other
   - medium_sel: 11=CD, 7=WEB-DL, 8=WEBRip, 99=Other
   - standard_sel: 99 (Other)
   - codec_sel: 99 (Other)
   - audiocodec_sel: 7=FLAC, 8=APE, 9=WAV, 10=MP3, 5=AAC, 12=OPUS, 99=Other
3. 媒体信息 Media_BDInfo: 必填且必须为英文格式（禁止「概要/概览」开头）
4. 海报与截图（截图必填，二选一）:
   - url_poster: 海报图床 URL
   - url_vimages: 外部图床 URL（仅限白名单域名，且要求 ≥3 张 PNG）
   - 或走附件模式：/attachment_upload.php 上传本地封面，回填 screenshot_attachment_ids[]
5. 标签: 独立布尔复选字段（pack 合集 等），由 TagMatcher 统一判定；严禁勾 mandarin(国配) 等影视属性
6. 候选期兼容（offers.php 规则原文实测）:
   - 候选积分 < 10 时无直接发种权限，自动携带 offer=yes 提交至 /takeupload.php 候选区
   - 待定候选 ≥ 3 或存在冻结种子时，站点整体禁发（表现为「你没有自由上传的权限！」）
     ⇒ pre_publish_check 先读 offers.php 实况拦截并进入 1 小时冷却，避免逐专辑空转
   - 候选积分 ≥ 10 后自动切回正式发布
7. 匿名发布: uplver=yes
★ 直连光猫，IPv4 优先，脱钩外部环境变量代理 (trust_env=False)
"""

import os
import re
import time
import uuid
import logging
import mimetypes
from typing import Dict, Any, Tuple, Optional, List
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from musichub.adapters.nexusphp_base import NexusPHPBaseAdapter, DUPLICATE_NAME_PREFIX
from musichub.adapters.tag_matcher import TagMatcher
from musichub.core.models import CanonicalMusicItem

logger = logging.getLogger("SpringSundayAdapter")

SSD_SCREENSHOT_HOST_WHITELIST = (
    "cmct.xyz",
    "static.hdcmct.org",
    "gifyu.com",
    "imgbox.com",
    "pixhost.to",
    "pixhost.cc",
    "ptpimg.me",
    "static.ssdforum.org",
)

# 候选区硬规则（offers.php 规则原文，2026-11-25 实测）：
# 「新人及以上且候选积分≥10 可直接发种；有冻结种子或待定候选≥3 的用户不可发布种子或添加候选」
DIRECT_PUBLISH_POINTS = 10
OFFER_PENDING_LIMIT = 3


class SpringSundayAdapter(NexusPHPBaseAdapter):
    has_cover_field = True

    def __init__(self, cookie_client=None, cover_service=None):
        super().__init__(
            site_name="SpringSunday",
            domain="springsunday.net",
            base_url="https://springsunday.net",
            cookie_client=cookie_client,
            cover_service=cover_service,
        )
        self._cached_offer_required: Optional[bool] = None
        self._stats_cache: Optional[Tuple[float, Dict[str, int]]] = None
        self._blocked_until: float = 0.0

    def _fetch_offer_stats(self) -> Dict[str, int]:
        """读取候选区规则实况：{'points': 候选积分, 'pending': 我的待定候选数}

        站点规则（offers.php 现场文本）：新人及以上、候选积分≥10 可直接发种；
        有冻结种子或待定候选≥3 时一律不可发种/加候选。匿名发布不在候选列表署名，
        因此必须按 userid 过滤自己的候选，不能按用户名匹配。
        """
        now = time.time()
        if self._stats_cache and now - self._stats_cache[0] < 600:
            return self._stats_cache[1]
        headers = self._prepare_headers()
        if "Cookie" not in headers:
            return {}
        try:
            r = self.session.get(f"{self.base_url}/offers.php", headers=headers, timeout=20, verify=False)
            txt = r.text
            page_txt = BeautifulSoup(txt, "html.parser").get_text("\n", strip=True)
            m = re.search(r"当前候选积分：\s*(\d+)", page_txt)
            points = int(m.group(1)) if m else -1
            uid_m = re.search(r"欢迎回来[\s\S]{0,600}?userdetails\.php\?id=(\d+)", txt)
            pending = -1
            if uid_m:
                r2 = self.session.get(
                    f"{self.base_url}/offers.php?userid={uid_m.group(1)}",
                    headers=headers, timeout=20, verify=False,
                )
                # 候选列表是嵌套表格（父子两层 <tr>），且待定行标题链到 details.php?id=，
                # 必须按种子 id 去重，不能按用户名（匿名发布）或行数统计
                pend_ids = set()
                for tr in BeautifulSoup(r2.text, "html.parser").find_all("tr"):
                    if not re.search(r"[（(]\s*(?:待定|待审|初审待复核)\s*[)）]", tr.get_text(" ", strip=True)):
                        continue
                    pend_ids.update(re.findall(r"(?:details|offers)\.php\?id=(\d+)", str(tr)))
                pending = len(pend_ids)
            stats = {"points": points, "pending": pending}
            self._stats_cache = (now, stats)
            return stats
        except Exception as e:                                   # noqa: BLE001
            logger.warning(f"[SpringSunday] 读取候选区实况失败: {e}")
            return {}

    def pre_publish_check(self, item: CanonicalMusicItem) -> Tuple[bool, str]:
        if not item.tracks:
            return False, "SpringSunday requires at least one track"
        if time.time() < self._blocked_until:
            return False, "候选/发种权限受限冷却中（站点明确拒绝自由上传），本轮跳过"
        stats = self._fetch_offer_stats()
        if stats and stats.get("points", -1) < DIRECT_PUBLISH_POINTS:
            pending = stats.get("pending", -1)
            if pending >= OFFER_PENDING_LIMIT:
                return False, (
                    f"候选待定已达上限（{pending}/{OFFER_PENDING_LIMIT}），站点禁止继续添加候选；"
                    f"当前候选积分 {stats.get('points')}/{DIRECT_PUBLISH_POINTS}，需等待审核通过后释放名额"
                )
        return True, "SpringSunday check passed"

    def _map_medium(self, med: str) -> str:
        """medium_sel: 11=CD, 7=WEB-DL, 8=WEBRip, 99=Other"""
        m = (med or "").upper()
        if "WEB" in m:
            return "7"
        if "CD" in m or "SACD" in m:
            return "11"
        return "99"

    def _map_audiocodec(self, fmt: str) -> str:
        """audiocodec_sel: 7=FLAC, 8=APE, 9=WAV, 10=MP3, 5=AAC, 12=OPUS, 99=Other"""
        f = (fmt or "").upper()
        if "FLAC" in f:
            return "7"
        if "APE" in f:
            return "8"
        if "WAV" in f:
            return "9"
        if "MP3" in f:
            return "10"
        if "AAC" in f:
            return "5"
        if "OPUS" in f:
            return "12"
        return "99"

    def _map_source(self, item: CanonicalMusicItem) -> str:
        """source_sel: 1=大陆, 2=香港, 3=台湾, 4=欧美, 5=日本, 6=韩国, 99=Other"""
        text = f"{item.artist} {item.album} {item.title} {item.description or ''}".lower()
        if any(k in text for k in ("日本", "japan", "jp", "oricon")) or re.search(r"[\u3040-\u30ff]", text):
            return "5"
        if any(k in text for k in ("韩国", "korea", "kr", "k-pop")) or re.search(r"[\uac00-\ud7af]", text):
            return "6"
        if any(k in text for k in ("香港", "hong kong", "粤语", "cantonese")):
            return "2"
        if any(k in text for k in ("台湾", "taiwan", "tw")):
            return "3"
        if any(k in text for k in ("大陆", "内地", "国语", "华语", "mainland")):
            return "1"
        return "4" if re.match(r"^[a-z0-9\s\-_.,'\"&()\[\]]+$", (item.artist or "").lower()) else "99"

    @staticmethod
    def _is_whitelisted_image_url(url: str) -> bool:
        if not url or not url.startswith(("http://", "https://")):
            return False
        host = (urlparse(url).hostname or "").lower()
        return any(w in host for w in SSD_SCREENSHOT_HOST_WHITELIST)

    def _find_local_cover(self, local_dir: str) -> str:
        if not local_dir or not os.path.isdir(local_dir):
            return ""
        try:
            for fname in sorted(os.listdir(local_dir)):
                fl = fname.lower()
                if fl.endswith((".jpg", ".jpeg", ".png", ".webp")) and any(
                    k in fl for k in ("cover", "folder", "front", "artwork", "poster", "album")
                ):
                    return os.path.join(local_dir, fname)
            for fname in sorted(os.listdir(local_dir)):
                if fname.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    return os.path.join(local_dir, fname)
        except Exception:
            pass
        return ""

    def _upload_attachment_image(self, image_path: str) -> Tuple[Optional[str], Optional[str]]:
        """调用 SSD 站内 /attachment_upload.php 上传截图附件，返回 (attachment_id, url)

        实测契约（2026-11-25）：multipart POST `file` + `upload_id` + `kind=image`，
        成功回 `{"success": true, "attachment": {"id": 10415, "url": "https://file.springsunday.net/image/...", "preview_url": "..."}}`。
        """
        if not image_path or not os.path.isfile(image_path):
            return None, None
        headers = self._prepare_headers()
        if "Cookie" not in headers:
            return None, None
        upload_url = f"{self.base_url}/attachment_upload.php"
        mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        try:
            with open(image_path, "rb") as f:
                resp = self.session.post(
                    upload_url,
                    data={"upload_id": uuid.uuid4().hex[:16], "kind": "image"},
                    files={"file": (os.path.basename(image_path), f, mime)},
                    headers=headers,
                    timeout=60,
                    verify=False,
                )
            if resp.status_code == 200:
                data = resp.json()
                if not data.get("success", True):
                    logger.warning(f"[SpringSunday] attachment_upload rejected: {data.get('error')}")
                    return None, None
                att = data.get("attachment") if isinstance(data.get("attachment"), dict) else data
                att_id = str(att.get("id") or att.get("attachment_id") or "")
                att_url = str(att.get("url") or att.get("preview_url") or "")
                if att_id or att_url:
                    return att_id or None, att_url or None
            logger.warning(f"[SpringSunday] attachment_upload HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"[SpringSunday] attachment_upload failed: {e}")
        return None, None

    def _resolve_local_screenshot(self, item: CanonicalMusicItem, cover_url: str) -> str:
        """取可用于上传附件的本地图片：专辑目录 → 封面缓存 → 按封面 URL 下载缓存"""
        local = self._find_local_cover(item.local_path)
        if local:
            return local
        cs = self.cover_service
        if not cs:
            return ""
        try:
            local = cs.find_local_cover(item.local_path)
            if local:
                return local
            if cover_url:
                local = cs._get_cached_file(cover_url) or cs._download_and_cache(cover_url)
                if local:
                    return local
        except Exception as e:                                   # noqa: BLE001
            logger.warning(f"[SpringSunday] local screenshot resolve failed: {e}")
        return ""

    def _build_english_mediainfo(self, item: CanonicalMusicItem) -> str:
        """生成符合 SSD 校验规则的英文 MediaInfo 文本（禁止以中文「概要/概览」开头）"""
        first_track = item.tracks[0]
        fname = os.path.basename(first_track.file_path or f"{first_track.title}.{(item.format or 'flac').lower()}")
        fmt = (item.format or "FLAC").upper()
        sr = first_track.sample_rate or 44100
        bd = first_track.bit_depth or 16
        dur = int(first_track.duration_seconds or 180)
        mins, secs = divmod(dur, 60)
        lines = [
            "General",
            f"Complete name                            : {fname}",
            f"Format                                   : {fmt}",
            f"Duration                                 : {mins} min {secs} s",
            f"Album                                    : {item.album or item.title}",
            f"Performer                                : {item.artist}",
            "",
            "Audio",
            f"Format                                   : {fmt}",
            "Channel(s)                               : 2 channels",
            f"Sampling rate                            : {sr / 1000:.1f} kHz",
            f"Bit depth                                : {bd} bits",
        ]
        return "\n".join(lines)

    def needs_offer_mode(self) -> bool:
        """动态检测当前账号是否仍处于候选阶段（候选积分 < 10）"""
        if self._cached_offer_required is not None:
            return self._cached_offer_required
        headers = self._prepare_headers()
        if "Cookie" not in headers:
            return True
        # 优先按候选积分判定（最贴近站点规则），积分达标即直接发种
        stats = self._fetch_offer_stats()
        if stats and stats.get("points", -1) >= 0:
            self._cached_offer_required = stats["points"] < DIRECT_PUBLISH_POINTS
            return self._cached_offer_required
        try:
            resp = self.session.get(f"{self.base_url}/upload.php", headers=headers, timeout=15, verify=False)
            if resp.status_code == 200:
                denied = any(k in resp.text for k in ("没有发布种子的权限", "没有自由上传的权限"))
                has_direct = ("takeupload.php" in resp.text) and not denied
                self._cached_offer_required = not has_direct
                return self._cached_offer_required
        except Exception:
            pass
        return True

    def build_form_fields(self, item: CanonicalMusicItem, allow_attachment_upload: bool = False) -> Dict[str, Any]:
        main_title, sub_title = self.format_title(item)
        cover_url, _ = self.cover_service.get_site_cover(
            target_site=self.site_name,
            source_cover_url=item.cover_url,
            local_dir=item.local_path,
            source_site=item.origin_site,
            source_tid=item.origin_torrent_id,
            source_name=item.title,
        )

        fields: Dict[str, Any] = {
            "type": "508",                                   # 508: Music(音乐)
            "source_sel": self._map_source(item),
            "medium_sel": self._map_medium(item.medium),
            "standard_sel": "99",                            # Other
            "codec_sel": "99",                               # Other
            "audiocodec_sel": self._map_audiocodec(item.format),
            "name": main_title,
            "small_descr": sub_title,
            "url_poster": cover_url or "",
            "url_vimages": cover_url if self._is_whitelisted_image_url(cover_url or "") else "",
            "Media_BDInfo": self._build_english_mediainfo(item),
            "descr": self.build_body(item),
            "uplver": "yes",                                 # 匿名发布
        }

        # 截图必填：白名单图床 URL 与站内附件二选一，站点不接受两者皆空
        if allow_attachment_upload and not fields["url_vimages"]:
            local_cover = self._resolve_local_screenshot(item, cover_url or "")
            att_id, att_url = self._upload_attachment_image(local_cover)
            if not att_id:
                logger.warning("[SpringSunday] 截图附件上传失败，且封面不在图床白名单内（站点会拒绝发种）")
            if att_id:
                fields["screenshot_attachment_ids[]"] = [att_id]
            if not fields["url_poster"] and att_url:
                fields["url_poster"] = att_url

        if self.needs_offer_mode():
            fields["offer"] = "yes"

        # 标签勾选：SSD 是独立布尔复选字段（pack 等），统一由 TagMatcher 判定
        TagMatcher.inject_form_tags("springsunday", item, fields)

        return fields

    def publish(self, item: CanonicalMusicItem, torrent_path: str) -> Tuple[bool, str, Optional[bytes]]:
        """支持正式发布与候选发布（offer=yes）的统一提交"""
        chk_ok, chk_msg = self.pre_publish_check(item)
        if not chk_ok:
            return False, f"REJECT: Pre-publish check failed: {chk_msg}", None
        if not os.path.exists(torrent_path) or os.path.getsize(torrent_path) == 0:
            return False, f"REJECT: Invalid torrent file on path: {torrent_path}", None
        if ".!qB" in torrent_path or any(".!qB" in t.file_path for t in item.tracks):
            return False, "REJECT: Blocked by .!qB guardrail (download not fully completed)", None

        headers = self._prepare_headers()
        if "Cookie" not in headers:
            return False, f"SITE_DOWN: Missing CookieCloud credentials for {self.domain} (LoginExpired)", None

        fields = self.build_form_fields(item, allow_attachment_upload=True)
        if not fields.get("url_vimages") and not fields.get("screenshot_attachment_ids[]"):
            # 站点 url_vimages/截图 为必填；两者皆空必然被拒，不要白提交制造失败页
            return False, "REJECT: 截图必填但未生成（本地无封面可上传附件，且封面不在 SSD 图床白名单）", None
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
                    allow_redirects=True,
                )

            tid = None
            for h in list(resp.history) + [resp]:
                loc = h.headers.get("Location", "") if hasattr(h, "headers") else ""
                target_str = loc or getattr(h, "url", "")
                m = re.search(r"(?:details|offers)\.php\?id=(\d+)", target_str)
                if m:
                    tid = m.group(1)
                    if "existed=1" in target_str or "existed" in target_str:
                        return False, f"{DUPLICATE_NAME_PREFIX} [EXISTING_TID:{tid}] Torrent already exists on {self.site_name} (details.php?id={tid})", None
                    break

            if not tid:
                self._dump_failure_html(resp.text, torrent_path)
                raw_err = self._extract_site_error(resp.text)
                diagnosed = self.diagnose_reject_reason(raw_err, local_name=item.title)
                if any(k in resp.text for k in ("没有自由上传的权限", "没有发布种子的权限", "不可发布种子")):
                    # 候选待定≥3 / 有冻结种子 → 站点整体禁止发种；冷却 1 小时避免逐专辑空转
                    self._blocked_until = time.time() + 3600
                    self._stats_cache = None
                    return False, (
                        f"REJECT: 权限配额限制 —— {raw_err}；规则为候选积分≥{DIRECT_PUBLISH_POINTS} 可直发、"
                        f"待定候选≥{OFFER_PENDING_LIMIT} 或存在冻结种子即禁发，需等待站点审核"
                    ), None
                if diagnosed.startswith("[同名冲突]") or "已经存在" in resp.text:
                    return False, f"{DUPLICATE_NAME_PREFIX} {diagnosed}", None
                return False, f"REJECT: Publish rejected by {self.site_name}: {diagnosed}", None

            logger.info(f"Published to {self.site_name} successfully (offer={fields.get('offer', 'no')}), tid={tid}")
            dl_resp = self.session.get(f"{self.base_url}/download.php?id={tid}", headers=headers, timeout=30, verify=False)
            pub_bytes = dl_resp.content if dl_resp.status_code == 200 and b"d8:announce" in dl_resp.content[:64] else None
            return True, tid, pub_bytes
        except Exception as e:
            return False, f"SITE_DOWN: {type(e).__name__}: {e}", None
