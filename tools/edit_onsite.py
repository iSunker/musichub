# -*- coding: utf-8 -*-
"""逐站种子详情页编辑 / 封面体检工具。

用法（默认 dry-run，只有显式 --apply 才写站点）：

    # 0) 打印逐站映射表（发布/编辑表单、封面/日志/标题约束、证据等级）
    python musichub/tools/edit_onsite.py --print-mapping

    # 1) 看某条种子现在有没有封面
    python musichub/tools/edit_onsite.py --site lemonhd --tid 48728 --check

    # 2) 补封面（自动从源种详情页/本地目录找图；dry-run）
    python musichub/tools/edit_onsite.py --site lemonhd --tid 48728 --fix-cover

    # 3) 真正写入并回读校验
    python musichub/tools/edit_onsite.py --site lemonhd --tid 48728 --fix-cover --apply

    # 4) 改标题/副标题/简介（只覆盖指定字段，其余按站点现值回填）
    python musichub/tools/edit_onsite.py --site hdkylin --tid 12345 \
        --set "name=Artist - Album (2020) [FLAC]" --set "small_descr=..." --apply

    # 5) 扫描近 N 天已发布种子，列出缺封面清单（不写站点）
    python musichub/tools/edit_onsite.py --scan --days 14 --limit 80
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from musichub.core.config import MusicHubConfig
from musichub.onsite.editor import (
    OnsiteEditor, OnsiteEditError, normalize_descr,
    has_bad_source_quote, rebuild_source_quote,
)
from musichub.onsite.detail_archive import DetailArchive
from musichub.onsite.cover_identity import compare_site_cover, resolve_cover_identity
from musichub.onsite.site_rules import SITE_RULES, rules_rows

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("edit_onsite")


def build_adapter(site: str):
    from musichub.adapters.sites import (
        MusopiaAdapter, CrabPTAdapter, PTerClubAdapter, XingwanAdapter, HDKylinAdapter,
        TangPTAdapter, MuXueGeAdapter, LemonHDAdapter, UHDDolbyAdapter, HDTimeAdapter,
        OpenCDAdapter, UBitsAdapter, SoulVoiceAdapter, NanyangPTAdapter, SpringSundayAdapter,
    )
    mapping = {
        "lemonhd": LemonHDAdapter, "musopia": MusopiaAdapter, "crabpt": CrabPTAdapter,
        "pterclub": PTerClubAdapter, "xingwan": XingwanAdapter, "hdkylin": HDKylinAdapter,
        "tangpt": TangPTAdapter, "muxuege": MuXueGeAdapter, "uhddolby": UHDDolbyAdapter,
        "hdtime": HDTimeAdapter, "opencd": OpenCDAdapter, "ubits": UBitsAdapter,
        "soulvoice": SoulVoiceAdapter, "nanyangpt": NanyangPTAdapter, "springsunday": SpringSundayAdapter,
    }
    cls = mapping.get((site or "").strip().lower())
    if not cls:
        raise SystemExit(f"未登记的站点: {site}（可选: {', '.join(sorted(mapping))}）")
    return cls()


def load_published(db_path: str, site: str, days: int, limit: int) -> List[Dict[str, Any]]:
    """从台账取近 N 天 PUBLISHED 记录；台账缺源信息时用发种流水兜底。"""
    if not db_path:
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    since = f"-{int(days)} days"
    rows: List[Dict[str, Any]] = []
    try:
        q1 = """
            SELECT target_site, target_tid, source_site, source_tid, source_name,
                   b_hash, local_path, created_at
            FROM publish_lineage
            WHERE status = 'PUBLISHED' AND target_tid GLOB '[0-9]*'
              AND created_at >= datetime('now', ?)
        """
        args: List[Any] = [since]
        if site and site != "all":
            q1 += " AND LOWER(target_site) = LOWER(?)"
            args.append(site)
        q1 += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        for r in conn.execute(q1, args):
            rows.append(dict(r))

        q2 = """
            SELECT target_site, target_torrent_id AS target_tid, '' AS source_site,
                   '' AS source_tid, album_name AS source_name, '' AS local_path, created_at
            FROM publish_attempt_log
            WHERE result = 'PUBLISHED'
              AND target_torrent_id IS NOT NULL AND target_torrent_id != ''
              AND target_torrent_id GLOB '[0-9]*'
              AND created_at >= datetime('now', ?)
        """
        args2: List[Any] = [since]
        if site and site != "all":
            q2 += " AND LOWER(target_site) = LOWER(?)"
            args2.append(site)
        q2 += " ORDER BY id DESC LIMIT ?"
        args2.append(limit)
        for r in conn.execute(q2, args2):
            rows.append({
                "target_site": str(r["target_site"] or "").lower(),
                "target_tid": str(r["target_tid"]),
                "source_site": "",
                "source_tid": "",
                "source_name": r["source_name"],
                "local_path": "",
                "created_at": r["created_at"],
            })

        # ③ site_publish_graph：**权威发布映射**（含没有 publish_lineage 记录的历史/EXISTING 转正行）
        #    ★ 2026-11-21 加：`publish_lineage` 只覆盖新链路，乌托邦 28125/28126/28132 这批
        #      只在 graph 里，导致 `--scan --site musopia` 扫不到（"近 3 天没有匹配记录"）。
        q3 = """
            SELECT target_site, target_torrent_id AS target_tid,
                   origin_site AS source_site, origin_torrent_id AS source_tid,
                   '' AS source_name, '' AS local_path, published_at AS created_at
            FROM site_publish_graph
            WHERE target_torrent_id IS NOT NULL AND target_torrent_id != ''
              AND target_torrent_id GLOB '[0-9]*'
              AND published_at >= datetime('now', ?)
        """
        args3: List[Any] = [since]
        if site and site != "all":
            q3 += " AND LOWER(target_site) = LOWER(?)"
            args3.append(site)
        q3 += " ORDER BY id DESC LIMIT ?"
        args3.append(limit)
        for r in conn.execute(q3, args3):
            rows.append({
                "target_site": str(r["target_site"] or "").lower(),
                "target_tid": str(r["target_tid"]),
                "source_site": str(r["source_site"] or ""),
                "source_tid": str(r["source_tid"] or ""),
                "source_name": r["source_name"] or "",
                "local_path": r["local_path"] or "",
                "created_at": r["created_at"],
            })
    finally:
        conn.close()

    seen = set()
    dedup: List[Dict[str, Any]] = []
    for r in rows:
        key = (r.get("target_site"), str(r.get("target_tid")))
        if key in seen:
            continue
        seen.add(key)
        r["target_tid"] = str(r.get("target_tid") or "")
        dedup.append(r)
    # ★ 三张表各自的 `ORDER BY id DESC LIMIT N` 合起来会超过 limit：
    #   必须按时间**统一重排**再截断，否则先并入的表（lineage）会把更新的
    #   graph 行（如 musopia 28125~28132 这批）挤出去，扫描结果看着"没这批"。
    dedup.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return dedup[:limit]


def resolve_fix_identity(db_path: str, site: str, tid: str,
                         row: Dict[str, Any], editor: OnsiteEditor) -> Dict[str, Any]:
    """编辑与审计共用严格身份链；模糊标题匹配不得进入 --apply。"""
    target = dict(row)
    target.update({"target_site": site, "target_tid": str(tid)})
    archive = DetailArchive(None, adapters={site: editor.adapter})
    return resolve_cover_identity(target, db_path, archive=archive)


def lookup_lineage(db_path: str, site: str, tid: str) -> Dict[str, Any]:
    """按 (target_site, target_tid) 取最近一条台账，用于自动补 source/local 信息。"""
    if not db_path:
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT source_site, source_tid, source_name, b_hash, local_path, created_at
               FROM publish_lineage
               WHERE target_site = ? AND target_tid = ?
               ORDER BY id DESC LIMIT 1""",
            (site, str(tid)),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


VERTEX_DB_CANDIDATES = (
    "/app/vertex/sql.db",
    "/volume2/docker_ssd/vertex2/data/vertex/db/sql.db",
)
_VERTEX_LINK_ID_RE = re.compile(r"details\.php\?id=(\d+)")


def lookup_vertex_opencd_tid(origin_hash: str) -> str:
    """在 Vertex 只读库里按 infohash 反查 OpenCD tid（源种多为 OpenCD A 种）"""
    import re as _re
    if not origin_hash or len(origin_hash) != 40:
        return ""
    for path in VERTEX_DB_CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
            try:
                rows = conn.execute(
                    "SELECT tracker, link FROM torrents WHERE hash = ?",
                    (origin_hash.lower(),),
                ).fetchall()
            finally:
                conn.close()
        except Exception:                                      # noqa: BLE001
            continue
        for tracker, link in rows:
            if "open.cd" in str(tracker or "").lower() or "opencd" in str(tracker or "").lower():
                m = _re.search(r"details\.php\?id=(\d+)", str(link or ""))
                if m:
                    return m.group(1)
    return ""


def lookup_source_tid(db_path: str, site: str, tid: str,
                      prefer: str = "opencd") -> Tuple[str, str]:
    """解析某条已发布种子对应的源站 tid（默认 OpenCD），返回 `(tid, 来源说明)`。

    顺序：site_publish_graph 的 `<源站>_tid` 列 → 同行 origin 数字 tid →
    source_tasks（按 origin hash 反查）→ publish_lineage。
    都查不到返回 `("", 原因)`，调用方据此决定「删死链」还是「跳过」。
    """
    if not db_path:
        return "", "无数据库"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        col = f"{prefer}_tid"
        origin_hash = ""
        row = conn.execute(
            """SELECT * FROM site_publish_graph
               WHERE LOWER(target_site) = ? AND target_torrent_id = ?
               ORDER BY id DESC LIMIT 1""",
            (site, str(tid)),
        ).fetchone()
        if row:
            keys = row.keys()
            if col in keys and str(row[col] or "").strip().isdigit():
                return str(row[col]).strip(), f"graph.{col}"
            origin_site = str(row["origin_site"] or "").lower()
            origin_id = str(row["origin_torrent_id"] or "").strip()
            if origin_site == prefer and origin_id.isdigit():
                return origin_id, "graph.origin_torrent_id"
            if len(origin_id) == 40:
                origin_hash = origin_id.lower()

        if origin_hash:
            vx_tid = lookup_vertex_opencd_tid(origin_hash) if prefer == "opencd" else ""
            if vx_tid:
                return vx_tid, "vertex.torrents"
            r2 = conn.execute(
                "SELECT source_tid FROM source_tasks WHERE LOWER(info_hash) = ?",
                (origin_hash,),
            ).fetchone()
            if r2 and str(r2["source_tid"] or "").strip().isdigit():
                return str(r2["source_tid"]).strip(), "source_tasks"

        r3 = conn.execute(
            """SELECT source_site, source_tid, source_name, local_path, note FROM publish_lineage
               WHERE target_site = ? AND target_tid = ? ORDER BY id DESC LIMIT 1""",
            (site, str(tid)),
        ).fetchone()
        if (r3 and str(r3["source_site"] or "").lower() == prefer
                and str(r3["source_tid"] or "").strip().isdigit()):
            return str(r3["source_tid"]).strip(), "publish_lineage"

        # 4. 若上述静态列均无数字 tid，调用统一母种反查器查 Vertex 原始抓取
        if r3:
            s_name = str(r3["source_name"] or "")
            l_path = str(r3["local_path"] or "")
            note = str(r3["note"] or "")
            try:
                from musichub.publisher.origin_resolver import resolve_stock_origin
                o_site, o_tid, o_hash, _, _, _ = resolve_stock_origin(
                    folder_name=s_name or note,
                    album=note or s_name,
                    full_path=l_path,
                    conn=conn,
                    db_path=db_path
                )
                if o_site == prefer and o_tid.isdigit():
                    return o_tid, "vertex.origin_resolver"
            except Exception as ex:
                logger.debug(f"lookup_source_tid 委托 resolve_stock_origin 异常: {ex}")
    except Exception as e:                                      # noqa: BLE001
        return "", f"查库失败: {e}"
    finally:
        conn.close()
    return "", "数据库各路均无数字 tid"


def backfill_opencd_tid(db_path: str, apply: bool = False) -> Tuple[int, int]:
    """把 graph 里「源头是 40 位 hash、opencd_tid 为空」的行按 source_tasks / 台账补足。

    返回 `(可补条数, 已写入条数)`。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT content_fingerprint, origin_torrent_id
               FROM site_publish_graph
               WHERE (opencd_tid IS NULL OR opencd_tid = '')
                 AND LENGTH(origin_torrent_id) = 40
               GROUP BY content_fingerprint"""
        ).fetchall()
        task_map = {
            str(r["info_hash"]).lower(): str(r["source_tid"]).strip()
            for r in conn.execute("SELECT info_hash, source_tid FROM source_tasks")
            if str(r["source_tid"] or "").strip().isdigit()
        }
        plan: List[Tuple[str, str]] = []
        for r in rows:
            fp = str(r["content_fingerprint"] or "")
            src_hash = str(r["origin_torrent_id"] or "").lower()
            tid = (lookup_vertex_opencd_tid(src_hash)
                   or task_map.get(src_hash, ""))
            if not tid:
                hit = conn.execute(
                    """SELECT l.source_tid FROM publish_lineage l
                       JOIN site_publish_graph g
                         ON g.target_site = l.target_site
                        AND g.target_torrent_id = l.target_tid
                       WHERE g.content_fingerprint = ? AND l.source_site = 'opencd'
                         AND l.source_tid GLOB '[0-9]*' LIMIT 1""",
                    (fp,),
                ).fetchone()
                tid = str(hit["source_tid"]).strip() if hit else ""
            if tid:
                plan.append((fp, tid))
        if apply and plan:
            with conn:
                for fp, tid in plan:
                    conn.execute(
                        """UPDATE site_publish_graph SET opencd_tid = ?
                           WHERE content_fingerprint = ?
                             AND (opencd_tid IS NULL OR opencd_tid = '')""",
                        (tid, fp),
                    )
        return len(plan), (len(plan) if apply else 0)
    finally:
        conn.close()


def parse_sets(items: Optional[List[str]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for raw in items or []:
        if "=" not in raw:
            raise SystemExit(f"--set 需要 key=value 形式，收到: {raw!r}")
        k, v = raw.split("=", 1)
        k = k.strip()
        if not k:
            raise SystemExit(f"--set 字段名为空: {raw!r}")
        if k.endswith("[]"):
            out[k] = [x for x in (v.split(",") if v else [])]
        else:
            out[k] = v
    return out


def print_mapping() -> None:
    cols = ["site", "display", "category", "cover_mode", "cover_host_required",
            "log_mode", "anonymous_field", "title_latin1_only", "verified"]
    print("\t".join(cols))
    for row in rules_rows():
        print("\t".join(str(row.get(c) or "-") for c in cols))
        print(f"   └ 字段: {', '.join(row.get('fields') or []) or '-'}")
        print(f"   └ 证据: {row.get('source') or '-'}｜备注: {row.get('note') or '-'}")


def cmd_check(editor: OnsiteEditor, tid: str) -> int:
    details = editor.fetch_details(tid)
    if not details.get("title"):
        print(f"[FAIL] {editor.site}#{tid} details.php 读不到标题（种子已删或无权限）")
        return 2
    has = bool(details.get("cover_url"))
    print(f"[{'OK' if has else 'MISS'}] {editor.site}#{tid} {details['title'][:70]}")
    print(f"  封面: {details.get('cover_url') or '(详情页无封面图)'}")
    return 0 if has else 1


def main() -> int:
    p = argparse.ArgumentParser(description="MusicHub 逐站种子详情页编辑 / 封面体检")
    p.add_argument("--print-mapping", action="store_true", help="打印逐站规则映射表")
    p.add_argument("--site", default="", help="站点短名（lemonhd/hdkylin/...）或 all")
    p.add_argument("--tid", default="", help="目标站种子 ID")
    p.add_argument("--check", action="store_true", help="只检查详情页是否有封面")
    p.add_argument("--fix-cover", "--fix-covers", dest="fix_cover", action="store_true",
                   help="缺少封面时补写")
    p.add_argument("--fix-descr", action="store_true",
                   help="修整简介：删空的转载引用块（[quote]转自X: [/quote]）、无图时补「源站和本地均无封面」说明")
    p.add_argument("--fix-source-link", action="store_true",
                   help="把「转自X: <infohash>」死链改成源站详情页链接（查不到 tid 则删死链）")
    p.add_argument("--show-source", action="store_true",
                   help="打印简介里含「转自」的行（诊断来源引用格式用，只读）")
    p.add_argument("--backfill-opencd-tid", action="store_true",
                   help="按 source_tasks / 台账回填 graph 中缺失的 opencd_tid（单独动作）")
    p.add_argument("--cover-url", default="", help="显式指定封面 URL")
    p.add_argument("--source-site", default="", help="源站短名（用于取源种封面）")
    p.add_argument("--source-tid", default="", help="源站种子 ID")
    p.add_argument("--local-path", default="", help="本地专辑目录（用于本地封面上传）")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="直接改字段，可重复；tags[4][] 用逗号分隔")
    p.add_argument("--apply", action="store_true", help="真正提交（默认 dry-run）")
    p.add_argument("--no-verify", action="store_true", help="提交后不回读校验")
    p.add_argument("--scan", action="store_true", help="扫描台账近 N 天已发布种子")
    p.add_argument("--days", type=int, default=14, help="扫描窗口（天）")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--yes", action="store_true", help="批量写站点时确认")
    args = p.parse_args()

    if args.print_mapping:
        print_mapping()
        return 0

    verify = not args.no_verify

    # ---------- 台账补足（单独动作） ----------
    if args.backfill_opencd_tid:
        cfg = MusicHubConfig.from_env()
        total, written = backfill_opencd_tid(cfg.db_path, apply=args.apply)
        print(f"graph 缺 opencd_tid 且源头为 hash 的内容: {total} 条可补，"
              f"{written} 条已写入" + ("（dry-run，加 --apply 落地）" if not args.apply else ""))
        if not total:
            return 0
        if not args.apply:
            return 0

    # ---------- 扫描模式 ----------
    if args.scan:
        if not args.site:
            raise SystemExit("--scan 需要 --site <短名|all>")
        cfg = MusicHubConfig.from_env()
        rows = load_published(cfg.db_path, args.site, args.days, args.limit)
        if not rows:
            print(f"近 {args.days} 天没有匹配的 PUBLISHED 记录")
            return 0
        print(f"扫描 {len(rows)} 条（窗口 {args.days} 天，站点 {args.site}）")
        missing = []
        identity_blocked = []
        bad_descr = []
        bad_link = []
        by_site: Dict[str, int] = {}
        editors: Dict[str, OnsiteEditor] = {}
        for r in rows:
            site, tid = r["target_site"], r["target_tid"]
            by_site[site] = by_site.get(site, 0) + 1
            try:
                if site not in editors:
                    editors[site] = OnsiteEditor(build_adapter(site))
                editor = editors[site]
                details = editor.fetch_details(tid)
                fields, _ = editor.fetch_edit(tid)
            except OnsiteEditError as e:
                print(f"[ERR ] {site}#{tid} {e}")
                continue
            except Exception as e:                              # noqa: BLE001
                print(f"[ERR ] {site}#{tid} {type(e).__name__}: {e}")
                continue
            if not details.get("title"):
                print(f"[GONE] {site}#{tid} 详情页无标题")
                continue

            _, changes = normalize_descr(fields.get("descr") or "")
            if changes:
                r["_descr_changes"] = changes
                bad_descr.append(r)
            descr = fields.get("descr") or ""
            src_site = (r.get("source_site") or "opencd").lower()
            src_tid = str(r.get("source_tid") or "").strip()
            src_from = "扫描行"
            if not src_tid.isdigit() and src_site in ("opencd", "musopia"):
                src_tid, src_from = lookup_source_tid(cfg.db_path, site, tid, prefer=src_site)
            if has_bad_source_quote(descr, has_numeric_source_tid=bool(src_tid.isdigit())):
                r["_src_site"] = src_site
                r["_src_tid"] = src_tid
                r["_src_from"] = src_from
                bad_link.append(r)
            r["_detail_title"] = details.get("title") or ""
            if not details.get("cover_url"):
                missing.append(r)
            else:
                # 有图不等于封面正确：缓存 URL 与标题相似都不能替代 B 种身份链。
                candidate = dict(r)
                lin = lookup_lineage(cfg.db_path, site, tid)
                for key in ("source_site", "source_tid", "source_name", "b_hash", "local_path"):
                    if not candidate.get(key):
                        candidate[key] = lin.get(key) or ""
                identity = resolve_fix_identity(cfg.db_path, site, tid, candidate, editor)
                check = compare_site_cover(identity, [details["cover_url"]])
                r["_cover_identity"] = identity
                r["_cover_check"] = check
                if check.get("status") in ("COVER_MISMATCH", "COVER_BROKEN"):
                    missing.append(r)
                elif check.get("status") == "IDENTITY_UNRESOLVED":
                    identity_blocked.append(r)

            flags = []
            if changes:
                flags.append("简介:" + "/".join(changes))
            if has_bad_source_quote(descr):
                flags.append(f"来源死链(tid={r.get('_src_tid') or '缺'}/{r.get('_src_from')})")
            if not details.get("cover_url"):
                flags.append("缺封面")
            cover_status = (r.get("_cover_check") or {}).get("status")
            if cover_status == "COVER_MISMATCH":
                flags.append("封面错配")
            elif cover_status == "COVER_BROKEN":
                flags.append("封面失效")
            elif cover_status == "IDENTITY_UNRESOLVED":
                flags.append(f"封面身份未决({(r.get('_cover_identity') or {}).get('status')})")
            if flags:
                print(f"[BAD ] {site}#{tid} {'; '.join(flags)} | {details['title'][:50]}")
            else:
                print(f"[ OK ] {site}#{tid} {details['title'][:50]}")
            time.sleep(0.6)

        print(f"\n汇总: 共 {len(rows)} 条，封面待修 {len(missing)} 条，"
              f"封面身份未决 {len(identity_blocked)} 条，简介待修 {len(bad_descr)} 条，"
              f"来源死链 {len(bad_link)} 条；站点分布 {by_site}")
        if (args.apply and not args.yes
                and ((missing and args.fix_cover) or (bad_descr and args.fix_descr)
                     or (bad_link and args.fix_source_link))):
            print("批量写站点需要 --yes 确认，已停止")
            return 3

        rc = 0

        # ① 简介修整（删空转载引用块 / 补无封面说明）—— 先做，避免补完封面又被覆盖
        if bad_descr and args.fix_descr:
            fixed = failed = 0
            for r in bad_descr:
                site, tid = r["target_site"], r["target_tid"]
                editor = editors.get(site) or OnsiteEditor(build_adapter(site))
                try:
                    res = editor.fix_descr(tid, dry_run=not args.apply, verify=verify)
                except Exception as e:                      # noqa: BLE001
                    failed += 1
                    print(f"[ERR ] {site}#{tid} 简介修整异常: {type(e).__name__}: {e}")
                    time.sleep(1.0)
                    continue
                tag = "FIXED" if res.get("ok") else "FAIL "
                print(f"[{tag}] {site}#{tid} {'/'.join(res.get('changes') or [])} "
                      f"{res.get('message') or res.get('error') or ''}")
                if res.get("ok"):
                    fixed += 1
                else:
                    failed += 1
                time.sleep(1.0)
            print(f"简介修整: 成功 {fixed}，失败 {failed}" + ("（dry-run，加 --apply 落地）" if not args.apply else ""))
            if failed:
                rc = 1

        # ② 来源死链修正（转自X: <infohash> → 源站详情页链接）
        if bad_link and args.fix_source_link:
            fixed = failed = 0
            for r in bad_link:
                site, tid = r["target_site"], r["target_tid"]
                editor = editors.get(site) or OnsiteEditor(build_adapter(site))
                try:
                    res = editor.fix_source_link(
                        tid,
                        source_site=r.get("_src_site") or "opencd",
                        source_tid=r.get("_src_tid") or "",
                        source_name=r.get("source_name") or r.get("_detail_title") or "",
                        dry_run=not args.apply, verify=verify,
                    )
                except Exception as e:                      # noqa: BLE001
                    failed += 1
                    print(f"[ERR ] {site}#{tid} 来源死链修正异常: {type(e).__name__}: {e}")
                    time.sleep(1.2)
                    continue
                tag = "FIXED" if res.get("ok") else "FAIL "
                print(f"[{tag}] {site}#{tid} {'/'.join(res.get('changes') or [])} "
                      f"{res.get('message') or res.get('error') or ''}")
                if res.get("ok"):
                    fixed += 1
                else:
                    failed += 1
                time.sleep(1.2)
            print(f"来源死链修正: 成功 {fixed}，失败 {failed}"
                  + ("（dry-run，加 --apply 落地）" if not args.apply else ""))
            if failed:
                rc = 1

        # ③ 补封面
        if missing and args.fix_cover:
            fixed = failed = 0
            for r in missing:
                site, tid = r["target_site"], r["target_tid"]
                editor = editors.get(site) or OnsiteEditor(build_adapter(site))
                # 只接受 B 种文件清单证明的精确目录；名称匹配不再进入自动修复。
                try:
                    lin = lookup_lineage(cfg.db_path, site, tid)
                    candidate = dict(r)
                    for key in ("source_site", "source_tid", "source_name", "b_hash", "local_path"):
                        if not candidate.get(key):
                            candidate[key] = lin.get(key) or ""
                    candidate["local_path"] = candidate.get("local_path") or args.local_path
                    candidate["source_site"] = candidate.get("source_site") or args.source_site
                    candidate["source_tid"] = candidate.get("source_tid") or args.source_tid
                    identity = r.get("_cover_identity") or resolve_fix_identity(
                        cfg.db_path, site, tid, candidate, editor)
                    if not identity.get("ok"):
                        res = {"ok": False, "error": f"{identity.get('status')}: {identity.get('error')}",
                               "identity": identity}
                    else:
                        res = editor.fix_cover(
                            tid, cover_url=args.cover_url,
                            source_site=candidate.get("source_site") or "",
                            source_tid=candidate.get("source_tid") or "",
                            source_name=candidate.get("source_name") or r.get("_detail_title") or "",
                            local_path=identity["local_path"],
                            expected_image_path=identity["expected_image_path"],
                            dry_run=not args.apply, verify=verify,
                        )
                        res["identity"] = identity
                except Exception as e:                      # noqa: BLE001
                    failed += 1
                    print(f"[ERR ] {site}#{tid} 补封面异常: {type(e).__name__}: {e}")
                    time.sleep(1.0)
                    continue
                if res.get("ok"):
                    fixed += 1
                    print(f"[FIXED] {site}#{tid} ← {res.get('cover_url')}（{res.get('cover_source')}）")
                else:
                    failed += 1
                    print(f"[FAIL ] {site}#{tid} {res.get('error')}")
                time.sleep(1.0)
            print(f"补封面完成: 成功 {fixed}，失败 {failed}")
            if failed:
                rc = 1

        if not (args.fix_cover or args.fix_descr) and (missing or bad_descr):
            print("提示：加 --fix-descr / --fix-cover 与 --apply --yes 才会真正写站点")
        return rc

    # ---------- 单条模式 ----------
    if not args.site or not args.tid:
        raise SystemExit("单条模式需要 --site 与 --tid（或用 --scan / --print-mapping）")
    if args.site not in SITE_RULES:
        raise SystemExit(f"未登记站点 {args.site}；可先 --print-mapping 查看")

    editor = OnsiteEditor(build_adapter(args.site))

    lineage: Dict[str, Any] = {}
    db_path = MusicHubConfig.from_env().db_path
    if args.check or args.fix_cover or args.fix_source_link:
        try:
            lineage = lookup_lineage(db_path, args.site, args.tid)
        except Exception as e:                                  # noqa: BLE001
            logger.warning(f"读台账失败（不影响编辑）: {e}")

    if args.check:
        rc = cmd_check(editor, args.tid)
        if lineage:
            print(f"  台账: 源={lineage.get('source_site') or '-'}#{lineage.get('source_tid') or '-'}"
                  f" 本地={lineage.get('local_path') or '-'}")
        return rc

    overrides = parse_sets(args.set)
    rc = 0

    if overrides:
        res = editor.edit(args.tid, overrides, dry_run=not args.apply, verify=verify)
        print(f"[{'OK' if res.get('ok') else 'FAIL'}] {args.site}#{args.tid} {res.get('message') or res.get('error')}")
        for ch in res.get("changes") or []:
            print(f"  - {ch['field']}: {str(ch['old'])[:60]!r} → {str(ch['new'])[:60]!r}")
        if res.get("verify") and not res["verify"].get("ok"):
            print(f"  ⚠ 回读未落地: {res['verify'].get('mismatch') or res['verify'].get('error')}")
        rc = 0 if res.get("ok") else 1

    if args.fix_cover:
        candidate = dict(lineage)
        candidate.update({
            "source_site": args.source_site or lineage.get("source_site") or "",
            "source_tid": args.source_tid or lineage.get("source_tid") or "",
            "local_path": args.local_path or lineage.get("local_path") or "",
        })
        identity = resolve_fix_identity(db_path, args.site, args.tid, candidate, editor)
        if not identity.get("ok"):
            res = {"ok": False, "error": f"{identity.get('status')}: {identity.get('error')}",
                   "identity": identity}
        else:
            res = editor.fix_cover(
                args.tid, cover_url=args.cover_url,
                source_site=candidate.get("source_site") or "",
                source_tid=candidate.get("source_tid") or "",
                source_name=lineage.get("source_name") or "",
                local_path=identity["local_path"],
                expected_image_path=identity["expected_image_path"],
                dry_run=not args.apply, verify=verify,
            )
            res["identity"] = identity
            print(f"  身份链: B={identity['b_hash'][:10]} 池={','.join(identity['candidate_pools'])} "
                  f"SHA256={identity['expected_sha256']}")
        status = "OK" if res.get("ok") else "FAIL"
        print(f"[{status}] {args.site}#{args.tid} 封面: {res.get('message') or res.get('error')}")
        if res.get("cover_url"):
            print(f"  URL: {res['cover_url']}（{res.get('cover_source')}）")
        if res.get("verify"):
            print(f"  回读: {res['verify']}")
        if not res.get("ok"):
            rc = 1

    if args.fix_descr:
        res = editor.fix_descr(args.tid, dry_run=not args.apply, verify=verify)
        status = "OK" if res.get("ok") else "FAIL"
        detail = res.get("message") or res.get("error") or ""
        print(f"[{status}] {args.site}#{args.tid} 简介: {'/'.join(res.get('changes') or []) or detail}")
        if res.get("changed"):
            print("  --- 修改前 ---")
            print("\n".join(str(res.get("descr_before") or "").splitlines()[:8]))
            print("  --- 修改后 ---")
            print("\n".join(str(res.get("descr_after") or "").splitlines()[:8]))
        if res.get("verify") and not res["verify"].get("ok"):
            print(f"  ⚠ 回读未落地: {res['verify']}")
        if not res.get("ok"):
            rc = 1

    if args.show_source:
        fields, _ = editor.fetch_edit(args.tid)
        descr = fields.get("descr") or ""
        hits = [ln for ln in descr.splitlines() if "转自" in ln]
        print(f"[{args.site}#{args.tid}] 含「转自」的简介行 {len(hits)} 条：")
        for ln in hits[:5]:
            print("  " + ln[:200])
        print(f"  死链判定: {'BAD' if has_bad_source_quote(descr) else 'OK'}")
        return 0

    if args.fix_source_link:
        src_tid = args.source_tid or str(lineage.get("source_tid") or "")
        src_site = args.source_site or str(lineage.get("source_site") or "") or "opencd"
        if not src_tid.isdigit():
            src_tid, src_from = lookup_source_tid(
                MusicHubConfig.from_env().db_path, args.site, args.tid, prefer=src_site)
            logger.info(f"源站 tid 解析: {src_tid or '缺'}（{src_from}）")
        res = editor.fix_source_link(
            args.tid, source_site=src_site, source_tid=src_tid,
            source_name=str(lineage.get("source_name") or ""),
            dry_run=not args.apply, verify=verify,
        )
        status = "OK" if res.get("ok") else "FAIL"
        detail = res.get("message") or res.get("error") or ""
        print(f"[{status}] {args.site}#{args.tid} 来源引用: "
              f"{'/'.join(res.get('changes') or []) or detail}")
        if res.get("changed"):
            print("  --- 修改前 ---")
            print("\n".join(str(res.get("descr_before") or "").splitlines()[:6]))
            print("  --- 修改后 ---")
            print("\n".join(str(res.get("descr_after") or "").splitlines()[:6]))
        if res.get("verify") and not res["verify"].get("ok"):
            print(f"  ⚠ 回读未落地: {res['verify']}")
        if not res.get("ok"):
            rc = 1

    if not overrides and not args.fix_cover and not args.fix_descr and not args.fix_source_link \
            and not args.show_source:
        raise SystemExit("没有指定动作：用 --check / --fix-descr / --fix-source-link / --fix-cover / --set / --scan")
    return rc


if __name__ == "__main__":
    sys.exit(main())
