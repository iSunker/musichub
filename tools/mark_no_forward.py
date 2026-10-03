"""把「人工已知禁止转发」的行标成 `transfer_rule='DENY'`，让闸门④真的拦住它们。

═══════════════════════════════════════════════════════════════════════════
背景（2026-09-23 实测）
═══════════════════════════════════════════════════════════════════════════
用户手工往库/队列里加过 3 条 `V.A. - 中國名家名曲【器樂篇】·廣东音乐(1996) [FLAC]{GRCD‑409}`
（`205965ff…` / `00475fec…` / `6966e959…`），并明确说：

    「这三个是禁止转发呀！我手动添加的，不发」

只读实测：这 3 行的 `transfer_rule` 是 **NULL** ⇒
`db.PENDING_PUBLISH_WHERE` 的**闸门④**判据是「空 ⇒ 放行」⇒ **没有任何东西拦它们**。
（闸门④ 只拦 `transfer_rule='DENY'`。这三条走的是"空 ⇒ 放行"那一支。）

═══════════════════════════════════════════════════════════════════════════
★★ 为什么"空 ⇒ 放行"在这里是**错的**，而本脚本正是补那个洞
═══════════════════════════════════════════════════════════════════════════
闸门④ 里「`transfer_rule` 空 ⇒ 放行」当初的理由是：

    "空 = 没抓过皇后页面（本地抓轨行）⇒ 不拦；一刀切会挡下整个第一阶段"

★★ 这个理由在**本场景**下不成立：这 3 条**正是**本地抓轨行（没有皇后页面），
   而它们**也确实是**禁止转发的。
   ⇒ 「没有皇后页面」**不等于**「随便转」。

闸门④ 的 `DENY` 本来**只能**来自皇后页面的解析结果
（`opencd_parse` → `opencd_gate` → `opencd_ingest.STATUS_DENIED`），
而本地抓轨行**从来不过 ingest** ⇒ "人工知道的禁止"在库里**没地方记**。

本脚本把它记进**已有的那一列** `transfer_rule`（值 `'DENY'`）：
    · 不需要新列、不需要改 schema、不需要改闸门判据
    · 闸门④ 的判据是「值 = DENY ⇒ 拦」，**与有没有皇后页面无关** ⇒ 立刻生效
    · 而"空 ⇒ 放行"对**绝大多数**本地抓轨行**仍然正确**
      （那是你自己抓的、你有权发）⇒ 这就是「**默认允许 + 显式禁止**」

═══════════════════════════════════════════════════════════════════════════
三条纪律
═══════════════════════════════════════════════════════════════════════════
1. **默认只读**；真正写要 `--apply` **且** `--i-have-stopped-the-container`
   （两个写者写同一个 SQLite 是 `ERR-DOCKER-03`；实测 daemon 每 30s 前进一次心跳）。
2. **只 UPDATE `transfer_rule` 一列** —— 不碰 `status` / `site_status` /
   `content_path` / `log_content` / `opencd_tid`，**不调 `save_task()`**
   （那是 upsert，会顺带刷 `updated_at` ⇒ 那些行会全变 stale、`edit` 每轮重拉整站，
   见 `db.STALE_CONDITION` 那个坑）。★ 与 `sync_opencd_tid.py` 同一条纪律。
3. **只改 `--hashes` 明确列出的行**；不给 hash 就不写。
   ★ **绝不按标题模糊匹配**（本项目的 `ERR-SITE-03`：同一个作品常有多个版本/多条记录，
     按标题去凑会改错行）。

用法（本机；**容器停着**）：
    python tools/mark_no_forward.py                    # 只读预览（默认那 3 条）
    python tools/mark_no_forward.py --hashes 205965ff…
    python tools/mark_no_forward.py --apply --i-have-stopped-the-container
"""
import argparse
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

LOCAL_DB = r"//iSunker-DS423/docker_ssd/opencd2others/data/data.db"

# ★★ 用户 2026-09-23 **两次**点名的禁转行，合起来正是"当时的整个发布队列"：
#    第一次：3 条中國名家名曲  「这三个是禁止转发呀！我手动添加的，不发」
#    第二次：3 条周杰伦        「那三条周杰伦是禁止转发！！！不能发」
#
#    ★ 这 6 条的意义**超出它们本身**：它们证明闸门④ 原来的「空 ⇒ 放行」
#      把**整个队列**（6/6）都放了过去 ⇒ 那是**默认值错了**，不是"漏标 6 行"。
#      `db.PENDING_PUBLISH_WHERE` 的闸门④ 已据此**反转为**「必须 = ALLOW 才放行」。
#
#    ★ 那为什么**还要**标 DENY？两条理由（第二条承重）：
#      ① 表达意图：`空`=「不知道」，`DENY`=「明确不许」。用户已经明确了。
#      ② ★★ 将来若有人为了"让本地行能发"去**批量刷 `ALLOW`**（很可能发生），
#         这 6 条有 `DENY` 会被**挡住**；若还是 `空`，就可能被一起刷成 `ALLOW`
#         ⇒ **一次误操作就让禁转的种子发出去**。
#         ⇒ **`DENY` 是"不可被批量授权绕过"的那道锁。**
DEFAULT_HASHES = (
    # 中國名家名曲（3 条，同一作品的多份记录）
    "205965ff4591", "00475fec155c", "6966e95913d7",
    # 周杰伦（3 条）
    "e76e695b6332",   # 太阳之子 环球豪华日版 (2026)
    "2201d9c0e204",   # 八度空间 (环球日版) (2002)
    "7c96605ab39b",   # Jay (环球日版) (2000)
)

# ★ 只改这一列。其余一概不碰（尤其 updated_at / status / site_status）
SET_COL = "transfer_rule"
DENY = "DENY"


def select_targets(conn, hashes):
    """
    ★ 按 `info_hash` **前缀**匹配（用户/日志里给的都是前 12 位）。
      前缀必须能唯一定位一行 —— 命中 0 行或多行都算失败，**不猜**。
    """
    out = []
    for h in hashes:
        rows = list(conn.execute(
            f"SELECT info_hash, torrent_name, status, site_status, {SET_COL} "
            f"  FROM tasks WHERE info_hash LIKE ?", (h + "%",)))
        out.append((h, rows))
    return out


def main():
    ap = argparse.ArgumentParser(
        description="把人工已知「禁止转发」的行标成 transfer_rule='DENY'")
    ap.add_argument("--apply", action="store_true", help="真正写库")
    ap.add_argument("--i-have-stopped-the-container", action="store_true",
                    help="★ 按纪律：必须显式声明容器已停（ERR-DOCKER-03）")
    ap.add_argument("--hashes", default=",".join(DEFAULT_HASHES),
                    help="要标的 info_hash（前缀即可，逗号分隔）")
    ap.add_argument("--db", default=LOCAL_DB)
    args = ap.parse_args()

    hashes = tuple(s.strip() for s in args.hashes.split(",") if s.strip())
    if not hashes:
        sys.exit("❌ 必须显式给 --hashes（绝不按标题匹配，纪律 3）")

    # ★★ 守卫一律放**最前面**（顺序错了就成了"先写库、再报拒绝"。
    #    上次写 backfill 工具时正是在这里栽过一次，见 SESSION-NOTES）
    if args.apply and not args.i_have_stopped_the_container:
        sys.exit("❌ 拒绝写库：必须同时给 --i-have-stopped-the-container（纪律 1）")

    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA query_only=0" if args.apply else "PRAGMA query_only=1")
    conn.row_factory = sqlite3.Row

    print("=" * 74)
    print("★ 把「人工已知禁止转发」的行标成 transfer_rule='DENY'（让闸门④拦住）")
    print("=" * 74)
    print(f"  库        : {args.db}")
    print(f"  模式      : {'★ 写入' if args.apply else '只读预览（不会改一个字）'}")
    print(f"  判据      : info_hash 前缀匹配，命中必须**恰好 1 行**（否则报错退出）")

    targets = select_targets(conn, hashes)

    print("\n" + "-" * 74)
    print("① 逐条核对（命中数 / 现值 / 会变成什么）")
    print("-" * 74)
    problems = []
    todo = []
    for h, rows in targets:
        print()
        print(f"  --hashes {h}  ⇒ 命中 {len(rows)} 行")
        if len(rows) == 0:
            problems.append(f"{h}: 命中 0 行")
            print("     ★★ 命中 0 行 ⇒ 报错，不猜（纪律 3）")
            continue
        if len(rows) > 1:
            problems.append(f"{h}: 命中 {len(rows)} 行（前缀歧义）")
            for r in rows:
                print(f"     ⚠ 歧义: {r['info_hash'][:16]}  {r['torrent_name']}")
            print("     ★★ 前缀歧义 ⇒ 报错，请给更长的前缀（不猜哪一条）")
            continue
        r = rows[0]
        cur = r[SET_COL]
        print(f"     info_hash    : {r['info_hash']}")
        print(f"     torrent_name : {r['torrent_name']}")
        print(f"     status       : {r['status']}  site_status={r['site_status']}")
        print(f"     ★ {SET_COL:<12} : {cur!r}  ⇒  {DENY!r}")
        if cur == DENY:
            print("     （已经是 DENY ⇒ 幂等，无需再写）")
        elif cur not in (None, ""):
            print(f"     ⚠ 非空且不是 DENY（现值 {cur!r}）—— 覆盖它要你确认")
            problems.append(f"{h}: 现值 {cur!r} 非空，拒绝擅自覆盖")
        else:
            todo.append(r["info_hash"])

    print("\n" + "-" * 74)
    print("② 汇总")
    print("-" * 74)
    print(f"  要标 DENY 的行 : {len(todo)}")
    print(f"  已是 DENY      : {sum(1 for _, rows in targets for r in rows if r[SET_COL] == DENY)}")
    print(f"  问题           : {len(problems)}  " + ("".join("\n     " + p for p in problems) if problems else "（无）"))

    if problems:
        conn.close()
        sys.exit("❌ 有问题未解决 ⇒ 拒绝执行（不猜、不擅自覆盖）")

    if not todo:
        print("\n✅ 无需改动（幂等）。")
        conn.close()
        return

    print("\n" + "-" * 74)
    print("③ ★ 连带效果（承重，必须看清）")
    print("-" * 74)
    print(f"""  这 {len(todo)} 行将从发布队列里**被闸门④拦掉**：
    `db.PENDING_PUBLISH_WHERE` 里 `transfer_rule='DENY'` ⇒ 不满足"空或非 DENY"
  ⇒ 它们**不再出现**在 `db.get_pending_publish()` 里 ⇒ publish 不会发它们。

  ★ 其余**不受影响**：本脚本只动 `transfer_rule` 一列；
    `updated_at` **不刷**（所以不会变 stale、不会触发 edit 重拉）。""")

    if not args.apply:
        print("\n" + "=" * 74)
        print("★ 只读预览到此。要真正写库：")
        print("    python tools/mark_no_forward.py --apply --i-have-stopped-the-container")
        print("=" * 74)
        conn.close()
        return

    # ── 写入 ────────────────────────────────────────────────────────────────
    print("\n" + "-" * 74)
    print("④ 写入（只 UPDATE 一列）")
    print("-" * 74)
    # 记录写入前的 `updated_at`，写完回读**逐行比对** ⇒ 证明真的没碰它
    pre = {h: conn.execute("SELECT updated_at FROM tasks WHERE info_hash=?",
                           (h,)).fetchone()[0] for h in todo}

    conn.executemany(
        f"UPDATE tasks SET {SET_COL}=? WHERE info_hash=?",
        [(DENY, h) for h in todo])
    conn.commit()

    post = {h: conn.execute("SELECT updated_at FROM tasks WHERE info_hash=?",
                            (h,)).fetchone()[0] for h in todo}
    print(f"  已写 {len(todo)} 行")
    print("\n  ★ 回读校验（写入后立刻读回）：")
    ok = 0
    for h in todo:
        cur = conn.execute(f"SELECT {SET_COL} FROM tasks WHERE info_hash=?",
                           (h,)).fetchone()[0]
        good = cur == DENY
        ok += good
        print(f"     {h[:12]}  {SET_COL}={cur!r}  {'✅' if good else '❌'}")
    print(f"\n  ★ `updated_at` 逐行比对（必须**逐行相同** ⇒ 证明没碰它）：")
    same = all(pre[h] == post[h] for h in todo)
    for h in todo:
        print(f"     {h[:12]}  {pre[h]} → {post[h]}  "
              f"{'✅ 未变' if pre[h] == post[h] else '❌ 被刷了！'}")
    print(f"\n  {'✅' if same else '❌❌'} updated_at 全未变: {same}")

    # ★ 闸门生效的**正面**证据：写后这些行必须不在队列里
    print("\n" + "-" * 74)
    print("⑤ ★ 闸门④ 是否真的拦住（写后查队列，才是真判据）")
    print("-" * 74)
    ph = ",".join("?" * len(todo))
    still = conn.execute(
        f"SELECT count(*) FROM tasks "
        f" WHERE info_hash IN ({ph}) AND ({_pending_where_or_empty()})",
        tuple(todo)).fetchone()[0]
    print(f"  这些行里**仍落在**待发布队列的: {still}  "
          f"{'✅ 全被拦住了' if still == 0 else '❌ 还有漏网的'}")
    print(f"  （判据与 db.PENDING_PUBLISH_WHERE 同源 —— 直接读 db 那个常量，不手抄）")

    conn.close()
    print(f"\n{'=' * 74}")
    print(f"✅ 完成：{ok}/{len(todo)} 行已标 {DENY}")
    print("=" * 74)


def _pending_where_or_empty():
    """
    ★ 直接拿 `db.PENDING_PUBLISH_WHERE`（**唯一真值**），不在这里再手抄一份。
      —— "同一事实散在多处必然漂移"是本项目的成文纪律。

    ★★ 踩过的坑：`db.py` 里那个常量是**多行相邻字符串字面量**隐式拼接
       （`"a" "b"`），而 CPython 在**编译期就把它们折叠成一个 `ast.Constant`**
       ⇒ 第一版按 `node.value.elts`（`BinOp`/`JoinedStr` 的形状）取，直接
       抛 `AttributeError: 'Constant' object has no attribute 'elts'`。
       ⇒ 两种形状都要认：折叠过的 `Constant` 与没折叠的 `BinOp(+)/JoinedStr`。
    """
    import ast
    db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db.py")
    tree = ast.parse(open(db_path, encoding="utf-8").read())

    node = None
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", None) == "PENDING_PUBLISH_WHERE" for t in n.targets):
            node = n.value
            break
    if node is None:
        raise RuntimeError("没找到 db.PENDING_PUBLISH_WHERE")

    def flatten(v):
        """把字符串字面量（可能是相邻拼接）拼起来。"""
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            return v.value
        if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add):
            return flatten(v.left) + flatten(v.right)
        raise RuntimeError(f"不认识的字面量形状: {type(v).__name__}")

    return flatten(node)


if __name__ == "__main__":
    main()
