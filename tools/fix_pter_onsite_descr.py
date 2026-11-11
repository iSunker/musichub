"""核验并修正猫站（PTerClub）线上已发布种子的简介合规性。

═══════════════════════════════════════════════════════════════════════════
它修什么（2026-11-09，用户指令「把站点上指定不符合的修改过来」）
═══════════════════════════════════════════════════════════════════════════
审核退回三条（见 `猫站发种失败反馈.txt`）：① 标题不符规范；② 转载声明置顶；
③ Markdown 改 BBCode。②③ 都落在**简介**里。本脚本对每条猫站线上种子：

  1. 读本地库 task → `pter_site.build_fields()` 产出**合规** `descr`
     （`descr.build_descr(site='pter')`：转载声明 `[quote]` 置顶于封面、
       正文/曲目过 `markdown_to_bbcode`、日志走 `[hide=…]`）；
  2. `PterClient.fetch_edit_form(tid)` 读**站点现值**；
  3. 对站点现值做合规三判：
       a. 转载声明置顶（descr 以 `[quote]转自OpenCD` 开头）；
       b. 无 Markdown 残留（`###` / `![` / `**` / `~~` / `](`）；
       c. 有日志时必须用 `[hide=`（不许 `[spoiler=`/`[code]`）;
  4. 现值 ≠ 合规值 ⇒ 列为待修；`--apply` 时经
     `PterClient.edit_torrent(tid, {"descr": …})` 推送，成功后**回读复核**。

═══════════════════════════════════════════════════════════════════════════
纪律
═══════════════════════════════════════════════════════════════════════════
1. **默认只读**（不带 `--apply` 只预览，绝不发 takeedit）。
2. **只改 `descr` 一格**。`name` / `small_descr` 的合规状态**只打印不推**
   （标题/副标题由发布时的 `normalize_pter_title` / `build_pter_small_descr`
   负责，本轮用户指令聚焦简介）；要一起推须显式 `--with-name-small`。
3. 逐字节相同的不发（`ERR-SITE-01`：HTTP 200 + 空 body 的已知触发条件）。
4. 每条编辑之间走 `PterClient` 的节流（1.5s）。

用法：
    python tools/fix_pter_onsite_descr.py --db <data.db 路径>          # 只读预览
    python tools/fix_pter_onsite_descr.py --db <data.db 路径> --apply  # 真推
    python tools/fix_pter_onsite_descr.py --db <data.db> --only 12345  # 点名一条
（`--env` 默认读 NAS 生产 `data/.env`，取 PTER_COOKIE 等凭据；只取键值，
  不在输出里回显任何密钥。）
"""
import argparse
import os
import re
import sys

import _bootstrap  # noqa: F401  （把 <repo>/src 放进 sys.path，须在 import config 之前）

sys.stdout.reconfigure(encoding="utf-8")

# ★ 生产 .env（含 PTER_COOKIE 等）；脚本不在输出里回显任何值。
PROD_ENV = r"//iSunker-DS423/docker_ssd/opencd2others/data/.env"

# Markdown 残留特征（判据 b）：审核口径是「Markdown改为bbcode」
_MARKDOWN_PAT = re.compile(
    r"(?m)^#{1,6}\s"      # 标题
    r"|!\[|\]\(https?://"  # 图片/链接
    r"|\*\*[^*]+\*\*"      # 加粗
    r"|~~[^~]+~~"          # 删除线
    r"|^\x60{3}"           # 代码围栏
)


def load_env_file(path):
    """把 .env 读进 os.environ（已存在的键不覆盖）。不打印任何值。"""
    n = 0
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k and k not in os.environ:
                os.environ[k] = v
                n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="猫站线上简介合规核验/修正")
    ap.add_argument("--db", required=True, help="本地（或共享）data.db 路径")
    ap.add_argument("--env", default=PROD_ENV)
    ap.add_argument("--apply", action="store_true", help="真推 takeedit（默认只读预览）")
    ap.add_argument("--only", default="", help="只处理指定 site_torrent_id（逗号分隔）")
    ap.add_argument("--with-name-small", action="store_true",
                    help="连同 name/small_descr 一起推（默认只推 descr）")
    args = ap.parse_args()

    n = load_env_file(args.env)
    # DB 路径：环境变量 → config.DB_PATH（必须在 import config 之前设）
    os.environ["DB_PATH"] = os.path.abspath(args.db)
    os.environ.setdefault("DATA_DIR", os.path.dirname(os.path.abspath(args.db)))
    print(f"✅ 载入 {args.env}：{n} 个新键；DB = {os.path.abspath(args.db)}")

    import db
    import pter_site
    from pter_site import PterClient

    client = PterClient()
    uid = client.check_login()
    if not uid:
        print("❌ 猫站 Cookie 校验失败（拿不到 user_id）⇒ 终止，不发任何编辑")
        return 1
    print(f"✅ 猫站登录有效（user_id={uid}）")
    mode = "APPLY（真推）" if args.apply else "DRY-RUN（只读预览）"
    print(f"模式：{mode}\n" + "=" * 74)

    only = {s.strip() for s in args.only.split(",") if s.strip()}

    conn = db.connect()
    # ★ 别名用 sp_torrent_id：tasks.* 里自带的 site_torrent_id（乌托邦那两列）
    #   会把同名键覆盖掉（dict(zip(...)) 后写者胜），猫站 id 在 site_publish.torrent_id。
    cur = conn.execute(
        "SELECT sp.torrent_id AS sp_torrent_id, sp.status AS sp_status, t.* "
        "FROM site_publish sp JOIN tasks t ON t.info_hash = sp.info_hash "
        "WHERE sp.site = 'pter' AND sp.torrent_id IS NOT NULL AND sp.torrent_id != '' "
        "ORDER BY CAST(sp.torrent_id AS INTEGER)"
    )
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    conn.close()
    tasks = [dict(zip(cols, r)) for r in rows]
    print(f"库内猫站已发布条目：{len(tasks)} 条\n")

    stats = {"ok": 0, "fix": 0, "fail": 0}
    for task in tasks:
        tid = str(task["sp_torrent_id"])
        title_short = (task.get("formatted_title") or task.get("torrent_name") or "")[:44]
        if only and tid not in only:
            continue

        # 1) 本地合规口径
        try:
            fields, audit = pter_site.build_fields(dict(task))
        except Exception as e:  # noqa: BLE001
            print(f"[id={tid}] {title_short}")
            print(f"    ❌ 本地 build_fields 异常：{type(e).__name__}: {e}\n")
            stats["fail"] += 1
            continue
        new_descr = fields.get("descr") or ""

        # 2) 站点现值
        try:
            site_fields, _snap = client.fetch_edit_form(tid)
        except Exception as e:  # noqa: BLE001
            print(f"[id={tid}] {title_short}")
            print(f"    ❌ 取编辑页失败：{type(e).__name__}: {e}\n")
            stats["fail"] += 1
            continue
        cur_descr = site_fields.get("descr") or ""

        # 3) 合规三判（对**站点现值**）
        top_ok = cur_descr.lstrip().startswith("[quote]转自OpenCD")
        md_hit = bool(_MARKDOWN_PAT.search(cur_descr))
        has_log_src = bool((task.get("log_content") or "").strip() or (task.get("logs_json") or "").strip())
        hide_needed = has_log_src and "[hide=" not in cur_descr
        bad_spoiler = "[spoiler=" in cur_descr or "[/code]" in cur_descr

        compliant = top_ok and not md_hit and not hide_needed and not bad_spoiler
        changed = cur_descr != new_descr

        problems = []
        if not top_ok:
            problems.append("转载声明未置顶/缺失")
        if md_hit:
            problems.append("含 Markdown 残留")
        if hide_needed:
            problems.append("有日志但未用 [hide=]")
        if bad_spoiler:
            problems.append("使用了 [spoiler]/[code]")

        print(f"[id={tid}] {title_short}")
        if compliant and not changed:
            print("    ✅ 合规且与本地口径一致 ⇒ 不动")
            stats["ok"] += 1
            continue
        if not problems and changed:
            # 合规但与本地新口径有差异（如封面/曲目更新）——也按需对齐
            problems.append("内容与本地最新口径不一致")

        print(f"    ⚠️  {'；'.join(problems)}")
        print(f"    · 站点现值 {len(cur_descr)} 字符；本地合规值 {len(new_descr)} 字符")
        if not args.apply:
            head = new_descr[:120].replace("\n", "⏎")
            print(f"    → dry-run：将推送 descr（开头预览）: {head}…")
            stats["fix"] += 1
            continue

        # 4) 真推
        overrides = {"descr": new_descr}
        if args.with_name_small:
            overrides["name"] = fields.get("name") or ""
            overrides["small_descr"] = fields.get("small_descr") or ""
        try:
            ok, msg = client.edit_torrent(tid, overrides, dry_run=False, verbose=True)
        except Exception as e:  # noqa: BLE001
            print(f"    ❌ takeedit 异常：{type(e).__name__}: {e}\n")
            stats["fail"] += 1
            continue
        if not ok:
            print(f"    ❌ 推送失败：{msg}\n")
            stats["fail"] += 1
            continue
        # 5) 回读复核：站点现值必须等于推的值
        try:
            site_fields2, _ = client.fetch_edit_form(tid)
            got = site_fields2.get("descr") or ""
            print(f"    {'✅' if got == new_descr else '⚠️'} 回读复核："
                  f"{'一致' if got == new_descr else '仍不一致（站点可能清洗了内容）'}")
        except Exception as e:  # noqa: BLE001
            print(f"    ⚠️ 推送成功但回读失败：{type(e).__name__}: {e}")
        stats["fix"] += 1
        print()

    print("=" * 74)
    print(f"汇总：无需修改 {stats['ok']} / 待修或已推 {stats['fix']} / 失败 {stats['fail']}")
    if not args.apply and stats["fix"]:
        print("★ 预览完成。确认无误后加 --apply 真推（只推 descr）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
