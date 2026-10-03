"""★ 只读：**问 qB 和本地盘**，回答"这些行到底有没有 `content_path`、有没有 `.torrent`"。

═══════════════════════════════════════════════════════════════════════════
它要解开的那个矛盾（SESSION-NOTES 里 `tid=198601 status=NO_TORRENT_FILE` 却"四道预检全过"）
═══════════════════════════════════════════════════════════════════════════
★ 不引「编号节」这种指法：那套编号标题已随文档体制迁移退役（编号只 README/ERRATA
  还在用），引它会被 `tools/doc-audit/audit.py`（原 `02_refs.py`）报成 LEGACY —— 而它的"目标不存在"
  会让**真正悬空的引用埋进这种噪声里**。⇒ 用**标题文本**指路，
  标题改名时 grep 得到、也不受编号体制影响。
  ★ 同理：★**别在这个文件里把那个退役编号原样写出来**（连"反例"也不行）——
    对账工具扫的是文本，读不出"这是反例"，会把它当成一次真引用（我第一版就这么错了）。
`readiness_probe.py` 报出 `tid=198601 status=NO_TORRENT_FILE site=EXISTS ✅ 预检全过`
—— 一条号称"没有种子文件"的行，四道预检**全过**。两种可能：

  ① `status` 是**历史残留**（曾经缺过、后来有了）⇒ 那 `readiness_probe` 的结论成立；
  ② 又一次**列错位假数**（`readiness_probe.py` 自己就踩过一次）
     ⇒ 那读数**全部要重算**。

★ 本脚本用**两条独立的路**去碰同一个事实，不一致就是错位：
  · 库里那一列说它是什么（SQL，`SELECT` 与 `zip` 共用同一份列元组）
  · **盘上/ qB 里实际有什么**（`os.path.exists` / qB API 按 infohash 查）
★ 用户 2026-09-23 的原话：**"你去读一下 qb3020 不就知道了。"**

═══════════════════════════════════════════════════════════════════════════
"`content_path` 是什么" —— 一句话定义
═══════════════════════════════════════════════════════════════════════════
`content_path` 是 **qBittorrent 的"种子内容落点"**：一条种子的**根路径**，
整轨是一条文件、分轨（多文件种子）是那个目录。本项目里它只在一处产生 ——
`run.py` 的 scan 阶段从 `torrent.content_path` 抄下来（`run.py:112`），
目的是**别再问 qB**（`backfill_covers.py` 要按它回原目录找封面图）。

★★ 由此推出一条**成文的错判**（`SESSION-NOTES.md:150-153`）：
   `content_path` 非空 **不能**用来分"本地抓轨行 / 皇后路行" ——
   实测那 428 条第一阶段行的 `content_path` **全部非空**。
   本脚本就是把那个"非空"进一步量到"**那个路径今天还在不在**"。

★★ 而"皇后路"（`topup` 补出来）的行 **结构性没有** `content_path`：
   它刻意的（`opencd_ingest.build_row` 不碰 `content_path` / `torrent_file`），
   理由见 `topup_opencd_fields.py` 纪律 5：重建 `.torrent` 会换 passkey ⇒
   **改 infohash**（严禁）。⇒ 那些行的 `.torrent` **只能在 qB 里找**，
   或者**根本不生成**（本脚本就是来量"到底有没有"的）。

═══════════════════════════════════════════════════════════════════════════
口径与纪律
═══════════════════════════════════════════════════════════════════════════
  1. **只读**：库走 `query_only=1`；qB 只调 `torrents_info`（查询），
     **不 add / 不 delete / 不 recheck**；本脚本**不写任何文件**。
  2. **零出网**：qB 是局域网/本机（`http://127.0.0.1:3020`），不碰皇后。
  3. **qB 连不上不是错误**是**信息** —— 本机跑不到 NAS 的 3020 是**常态**
     （那是 `network_mode: host` 的容器视角）⇒ 打 `⏭ 跳过` 并**继续**，
     ★ 但**必须显式报告"这一段没测"**，绝不静默当成"没有"
     （`ERR-META-01`：失效的检测比没有检测更危险）。
  4. **两个口径分开数，不许相减**（`ERR-META-05`）。

═══════════════════════════════════════════════════════════════════════════
用法（★ NAS 上跑才有意义 —— `torrent_file`/`content_path` 是**容器内路径**）
═══════════════════════════════════════════════════════════════════════════
    sudo docker compose run --rm tools python qb_probe.py
    sudo docker compose run --rm tools python qb_probe.py --limit 15
    sudo docker compose run --rm tools python qb_probe.py --tids 198601 196878

★ 本机（开发机）跑：`torrent_file` 是 `/data/output_torrents/…`（NAS 上的路径），
  本机**必然全部不存在** ⇒ 只会得到"路径不在本机"这个**已知的假结论**。
  所以本机跑的价值只有一个：**验代码**（`--selftest`）。
"""
import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
import db


# ★ 库里的列清单：**一个元组，SELECT 与 zip 共用** ——
#   `readiness_probe.py` 2026-09-23 踩过"漏一列 ⇒ 后面每列错位一格"，
#   于是报出"缺封面 4 条"这种**看着像真的**假数。本脚本是它的兄弟，
#   同一个坑不许踩第二遍（列名只写一次，从形状上消灭位置对齐）。
COLS = ("info_hash", "opencd_tid", "torrent_name", "status", "site_status",
        "content_path", "torrent_file", "log_content", "mapped_json",
        "updated_at")


def load_rows(tids=()):
    """候选 = 有 `opencd_tid` 的行（与 `readiness_probe.py` 同池，便于对照）。"""
    conn = db.connect()
    try:
        conn.execute("PRAGMA query_only=1")          # ★ 双保险：库里只许读
    except Exception:
        pass
    sel = ", ".join(COLS)
    sql = f"SELECT {sel} FROM tasks WHERE opencd_tid IS NOT NULL AND opencd_tid != ''"
    params = ()
    if tids:
        sql += f" AND opencd_tid IN ({','.join('?' * len(tids))})"
        params = tuple(str(t) for t in tids)
    rows = [dict(zip(COLS, r)) for r in conn.execute(sql, params)]
    conn.close()
    return rows


def qb_index():
    """问 qB 要全部种子，建 `infohash -> {content_path, name, save_path}`。

    ★ 只调 `torrents_info()`（**查询**）—— 不 add / 不 delete / 不 recheck。
    ★ 连不上 ⇒ 返回 `(None, 原因)` 而**不是**空 dict —— 空 dict 会被读成
      "qB 里一条都没有"，那是一个**看着像真的**错结论（`ERR-META-01`）。
    """
    try:
        import qb_client
    except ImportError as e:
        # ★ 等价于"没装 qbittorrentapi"：`qb_client.build()` 的首行就是
        #   `from qbittorrentapi import Client` ⇒ 缺库时它抛的正是 ImportError。
        #   ⇒ 两段式判据（"没装库" vs "连不上"）原样保留，字样逐字不变。
        return None, f"没装 qbittorrentapi（{e}）"
    try:
        # ★★ 唯一构造点 `qb_client.build`（`ERR-QB-08`）：优先 API Key，回退账号密码。
        qb = qb_client.build(config)
        out = {}
        for t in qb.torrents_info():
            out[(t.hash or "").lower()] = {
                "content_path": t.content_path or "",
                "name": t.name or "",
                "save_path": t.save_path or "",
            }
        return out, f"{len(out)} 个种子"
    except Exception as e:
        # ★ 常见且**正常**：本机跑 / 容器没起 / 3020 不通
        return None, f"{type(e).__name__}: {str(e)[:100]}"


def main():
    ap = argparse.ArgumentParser(description="qB / 本地盘 实证（只读）")
    ap.add_argument("--tids", nargs="*", default=[],
                    help="只看这几个 tid（默认：全部有 tid 的行）")
    ap.add_argument("--limit", type=int, default=12, help="最多打印几条（默认 12）")
    args = ap.parse_args()

    db.init_db()
    print("=" * 74)
    print("★ qB3020 / 本地盘 实证（只读 · 零出网 · 不发任何请求）")
    print("=" * 74)
    print(f"   QB_HOST      = {config.QB_HOST}")
    print(f"   DATA_DIR     = {config.DATA_DIR}")
    print(f"   TORRENT_DIR  = {config.OUTPUT_TORRENT_DIR}")

    rows = load_rows(args.tids)
    print(f"\n候选（有 tid 的行）: {len(rows)} 条")

    # ── 一、库里两列的真实分布（**这是问题 1 的直接答案**）───────────────
    n_cp = sum(1 for r in rows if (r["content_path"] or "").strip())
    n_tf = sum(1 for r in rows if (r["torrent_file"] or "").strip())
    cp_exists = sum(1 for r in rows
                    if (r["content_path"] or "").strip()
                    and os.path.exists(r["content_path"]))
    tf_exists = sum(1 for r in rows
                    if (r["torrent_file"] or "").strip()
                    and os.path.exists(r["torrent_file"]))
    n_log = sum(1 for r in rows if (r["log_content"] or "").strip())
    n_map = sum(1 for r in rows if (r["mapped_json"] or "").strip())
    print("\n[① 库里两列：有没有值 ／ 值指向的东西还在不在]")
    print(f"   content_path  非空          : {n_cp:>4} / {len(rows)}")
    print(f"   ├ 且路径**存在**（盘上真在）: {cp_exists:>4}"
          f"   ← ★ 与上行相减 = '路径已失效'")
    print(f"   torrent_file  非空          : {n_tf:>4} / {len(rows)}")
    print(f"   ├ 且文件**存在**（盘上真在）: {tf_exists:>4}")
    print(f"   log_content   非空          : {n_log:>4}")
    print(f"   mapped_json   非空          : {n_map:>4}")

    # ── 二、两份口径**交叉**：content_path 与 status 到底有没有关系 ────────
    #   ★ 这一节就是要**证伪或证实** SESSION-NOTES:150 那条成文教训
    #     （"不能用 content_path 是否为空来分本地路/皇后路"）。
    print("\n[② 交叉：status × 有没有 content_path（★ 不同口径，不许相减）]")
    cross = {}
    for r in rows:
        k = (r["status"] or "-", "有" if (r["content_path"] or "").strip() else "无")
        cross[k] = cross.get(k, 0) + 1
    for (stt, cp), n in sorted(cross.items(), key=lambda kv: -kv[1]):
        print(f"   {stt:<18} content_path={cp}   {n:>4}")

    # ── 三、qB：这些 infohash 在 qB 里到底有没有 ──────────────────────────
    idx, why = qb_index()
    print(f"\n[③ qB 查询（{config.QB_HOST}）]")
    if idx is None:
        print(f"   ⏭ **本段没测**（{why}）")
        print("   ★ 本机跑不到 NAS 的 3020 是**常态**（host 网络是容器视角）。")
        print("   ★ 要真答案请在 **NAS 上**跑本脚本；这一段**不算数**，"
              "别把它读成「qB 里没有」。")
    else:
        print(f"   ✅ 读到 {why}")
        hit = miss = 0
        tf_in_qb = 0
        for r in rows:
            rec = idx.get((r["info_hash"] or "").lower())
            if rec:
                hit += 1
                # qB 里的 content_path 与库里那列**是不是同一个值**
                if (rec["content_path"] or "").strip() == \
                        (r["content_path"] or "").strip():
                    tf_in_qb += 1
            else:
                miss += 1
        print(f"   ├ infohash 在 qB 里**命中**    : {hit:>4} / {len(rows)}")
        print(f"   ├ infohash 在 qB 里**没有**    : {miss:>4}")
        print(f"   └ 其中 qB 的 content_path 与库里**一致**: {tf_in_qb:>4}")

    # ── 四、逐条明细（★ 这一节用来当面查上面那个"预检全过"的矛盾）───────────
    print(f"\n[④ 明细（前 {args.limit} 条）]")
    for i, r in enumerate(rows[:args.limit], 1):
        cp = (r["content_path"] or "").strip()
        tf = (r["torrent_file"] or "").strip()
        cp_s = "路径在" if (cp and os.path.exists(cp)) else ("路径不在" if cp else "**空**")
        tf_s = "文件在" if (tf and os.path.exists(tf)) else ("文件不在" if tf else "**空**")
        qb_s = "-"
        if idx is not None:
            rec = idx.get((r["info_hash"] or "").lower())
            qb_s = (f"qB有 content_path={rec['content_path'][:34]!r}"
                    if rec else "qB无此 infohash")
        print(f"  [{i:>3}] tid={r['opencd_tid']:<9} status={r['status']:<16} "
              f"site={r['site_status'] or '-':<8}")
        print(f"        {(r['torrent_name'] or '')[:62]}")
        print(f"        content_path: {cp_s:<8} {cp[:52] or '—'}")
        print(f"        torrent_file: {tf_s:<8} {tf[:52] or '—'}")
        print(f"        log_content={'有' if (r['log_content'] or '').strip() else '空'}"
              f"  mapped_json={'有' if (r['mapped_json'] or '').strip() else '空'}"
              f"  {qb_s}")
        print(f"        updated_at={r['updated_at']}")

    print("\n" + "=" * 74)
    print("★ 本脚本**只报事实**。★ 两个口径（库里的值 ／ 盘上的存在性）若不一致，")
    print("  那是**要查的线索**，不是可以二选一的答案。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
