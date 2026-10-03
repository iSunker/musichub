"""把「诊断时真发出去、但没落库」的一条发布**认账**写回库（#22 善后）。

═══════════════════════════════════════════════════════════════════════════
背景（2026-10-04）
═══════════════════════════════════════════════════════════════════════════
诊断乌托邦「9-30 后发不出」时，用户批准「真发一条」做对照实验。结果：
    · `Ysaÿe Sonatas`        ❌ 站点逐条拒（`上传失败！`）
    · `Datcyde - Positive Climb` ✅ **302 → details.php?id=19525**（真发成功）
    · `Forever Now 2026`     ❌ 站点逐条拒

那条成功的是用**裸 `takeupload.php` POST** 发出的 —— **绕过了 publish.py 的落库**。
⇒ 站点上已有 id **19525**，但本地库**两处都没记**：
    · `tasks.site_status = 'UNKNOWN'`、`site_torrent_id = ''`
    · `site_publish(musopia)` 仍是**旧的、已被删的** `('14657','EXISTS')`
⇒ 后果：这条**仍满足 `db.PENDING_PUBLISH_WHERE`** ⇒ 下一轮 daemon 会**再发一次**
   （真·重复发布；发布不可逆）。

═══════════════════════════════════════════════════════════════════════════
本脚本做什么（只写这一条，认账）
═══════════════════════════════════════════════════════════════════════════
**逐字照抄** `publish.py` 发布成功后的落库写法（不发明新写法）：
    db.set_site_id(ih, "19525", site_status="EXISTS", status="PUBLISHED",
                   reason="reconciled:published_id:19525")
    db.site_publish_set(ih, db.SITE_MUSOPIA, "19525")

★ 为什么 `status` 也改成 `PUBLISHED`：与 publish.py 成功路径**同口径**
  （那 56 条真的已发布的也是 `PUBLISHED`）。若留 `SUCCESS`，它就仍满足
  `PENDING_PUBLISH_WHERE`（那句只看 `site_status != 'EXISTS'`，所以本可以不改），
  但**同口径**更不容易在将来被某处按 `status` 筛时漏掉。

★ 为什么**不用**原始 SQL 直写：`db.connect()` 用的是 `config.DB_PATH` + WAL +
  30s busy timeout —— 这正是 daemon 在用的写者协议，**并发安全**。
  另写一条 SQL 路径 = 又一个漂移点（`ERR-META-03` 的形状）。

═══════════════════════════════════════════════════════════════════════════
纪律
═══════════════════════════════════════════════════════════════════════════
1. **默认只读**（打印将要做的改动）；真正写要 `--apply`。
2. **先验证站上 19525 真的在**（GET `details.php?id=19525`，标题里有种子名）；
   站上没有就**拒绝写**（那是"站点又删了"，得另行处理）。
3. **只碰这一条**（info_hash 写死，防手滑）。
4. 写前把**改动前**的两处状态打出来（留痕）。

用法（容器内）:
    python tools/reconcile_19525.py              # 只读预览
    python tools/reconcile_19525.py --apply      # 真写
"""
import argparse
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

# 写死：本次要认账的那一条（防手滑改错行）
INFO_HASH = "37850b204c2aa7be0f26a6c29cae4afb54025180"
SITE_ID = "19525"
EXPECT_NAME = "Datcyde"          # 站上标题应含此串（做存在性判据）


def _fetch_site_title(tid):
    """GET 站上详情页，返回 (http, title, h1)。站没在 / 被删 ⇒ h1 为空。"""
    import musopia
    c = musopia.MusopiaClient(request_interval=0.5)
    r = c.get(f"/details.php?id={tid}")
    html = r.text or ""
    t = re.search(r"<title>(.*?)</title>", html, re.S)
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    title = (t.group(1).strip() if t else "")
    head = (re.sub(r"\s+", " ", h1.group(1)).strip() if h1 else "")
    return r.status_code, title, head


def main():
    ap = argparse.ArgumentParser(description="把真发成功但没落库的 19525 认账写回库")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只读预览）")
    args = ap.parse_args()

    import db

    print("=" * 70)
    print(f"#22 认账：info_hash={INFO_HASH}")
    print(f"       site_id={SITE_ID}")
    print("=" * 70)

    # ---- ① 改动前状态（留痕）----
    conn = db.connect()
    row = conn.execute(
        "SELECT torrent_name, status, site_status, site_torrent_id, "
        "       opencd_tid, updated_at FROM tasks WHERE info_hash=?",
        (INFO_HASH,)).fetchone()
    conn.close()
    if not row:
        print("❌ 库里查无此 info_hash —— 拒绝继续（info_hash 写错了？）")
        return 2
    name, status, site_status, site_id_old, opencd_tid, updated_at = row
    print("【改动前 · tasks】")
    print(f"  torrent_name    = {name!r}")
    print(f"  status          = {status!r}")
    print(f"  site_status     = {site_status!r}")
    print(f"  site_torrent_id = {site_id_old!r}")
    print(f"  opencd_tid      = {opencd_tid!r}")
    print(f"  updated_at      = {updated_at!r}")

    pub_old = db.site_publish_get(INFO_HASH, db.SITE_MUSOPIA)
    print("【改动前 · site_publish(musopia)】")
    print(f"  (torrent_id, status, cover) = {pub_old!r}")

    # ---- ② 站点存在性验证（★ 站上没有就拒写）----
    print("【站点验证】GET details.php?id=19525 …")
    http, title, head = _fetch_site_title(SITE_ID)
    print(f"  HTTP={http}")
    print(f"  title = {title!r}")
    print(f"  h1    = {head!r}")
    if http != 200 or EXPECT_NAME.lower() not in (title + head).lower():
        print(f"❌ 站上 19525 不像是我们的那条（应含 {EXPECT_NAME!r}）⇒ 拒绝写库。")
        print("   可能站点又删了它，或 id 被人占了 —— 这不是本脚本能处理的，请人工核。")
        return 3
    print("  ✅ 站上确实在（标题含种子名）⇒ 可以认账")

    # ---- ③ 计划改动 ----
    print("【计划·tasks】")
    print(f"  site_status     : {site_status!r} -> 'EXISTS'")
    print(f"  site_torrent_id : {site_id_old!r} -> {SITE_ID!r}")
    print(f"  status          : {status!r} -> 'PUBLISHED'")
    print("【计划·site_publish(musopia)】")
    print(f"  {pub_old!r} -> ('{SITE_ID}', 'EXISTS', <保留 cover>)")

    if not args.apply:
        print("\n(只读预览；加 --apply 才真写)")
        return 0

    # ---- ④ 真写（逐字照抄 publish.py 成功路径）----
    db.set_site_id(INFO_HASH, SITE_ID, site_status="EXISTS",
                   status="PUBLISHED",
                   reason=f"reconciled:published_id:{SITE_ID}")
    db.site_publish_set(INFO_HASH, db.SITE_MUSOPIA, SITE_ID)
    db.mark_synced(INFO_HASH)
    print("\n✅ 已写回。")

    # ---- ⑤ 回读确认 ----
    conn = db.connect()
    row = conn.execute(
        "SELECT status, site_status, site_torrent_id FROM tasks WHERE info_hash=?",
        (INFO_HASH,)).fetchone()
    conn.close()
    print(f"【改动后 · tasks】 {row!r}")
    print(f"【改动后 · site_publish(musopia)】 "
          f"{db.site_publish_get(INFO_HASH, db.SITE_MUSOPIA)!r}")

    # ---- ⑥ 断言：不再满足待发队列条件 ----
    still = db.connect().execute(
        f"SELECT COUNT(*) FROM tasks WHERE info_hash=? AND ({db.PENDING_PUBLISH_WHERE})",
        (INFO_HASH,)).fetchone()[0]
    if still == 0:
        print("✅ 该行已满足 `site_status='EXISTS'` ⇒ 不再进待发队列（不会再被重发）。")
    else:
        print(f"⚠️  该行**仍**满足待发队列条件（still={still}）—— 请检查！")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
