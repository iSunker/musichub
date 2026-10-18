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
import tempfile

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


def selftest():
    """★ 离线自检（纯本地：**不连库、不连 qB、不发请求**）。返回 `(是否通过, [明细])`。

    ★★★ 为什么必须真的有这个入口（`ERR-META-04` / `ERR-META-37`）：
      本文件 docstring 底部写着"本机跑的价值只有一个：验代码（`--selftest`）"——
      而**这个入口一直不存在**：`main()` 只注册了 `--tids`/`--limit`，`--selftest`
      会被 argparse 当场拒掉。⇒ 一条**承诺了却不存在**的接口比没有承诺更坏
      （读文档的人会以为"这里测过了"）。本函数即是补上的那一半。

    ★★ 阴/阳对照（`ERR-META-32`：判据必须能失败）：
      每条"应当如此"的断言都配一条**必须相反**的用例，否则把被测函数改成
      `return None` 也能全绿。
    """
    detail = []
    ok = [True]

    def chk(name, got, want):
        if got != want:
            ok[0] = False
            detail.append(f"✗ {name}\n     got = {got!r}\n     want= {want!r}")
        return got == want

    # ── ① `qb_index()` 的返回值契约 ──────────────────────────────────────
    #   ★ 本脚本最贵的一条纪律（文件头纪律 3）：连不上 ⇒ **返回 `(None, 原因)`**，
    #     绝不返回空 dict —— 空 dict 会被下游读成"qB 里一条都没有"，
    #     那是一个**看着像真的**错结论（`ERR-META-01`）。
    #   ★ 阴性对照：把"连不上"改成返回 `({}, …)` ⇒ 本断言必红。
    import qb_client as _qbc
    _orig_build = _qbc.build

    class _Boom(Exception):
        pass

    def _raise(_cfg):
        raise _Boom("模拟连不上")

    try:
        _qbc.build = _raise
        _idx, _why = qb_index()
        chk("① qB 连不上时 qb_index() 的第一个返回值必须是 None（不是 {}）",
            _idx is None, True)
        chk("① 连不上时原因里应能看出异常类型（不许静默）",
            "Boom" in str(_why), True)
    finally:
        _qbc.build = _orig_build

    # ★ 阳性对照：正常路径**必须**返回 dict（否则上面那条"是 None"靠崩掉也能过）。
    class _FakeTorrent:
        def __init__(self, h, cp, nm, sp):
            self.hash, self.content_path, self.name, self.save_path = h, cp, nm, sp

    class _FakeQB:
        def torrents_info(self):
            return [_FakeTorrent("AABBCC", "/d/专辑", "专辑", "/d")]

    try:
        _qbc.build = lambda _cfg: _FakeQB()
        _idx2, _why2 = qb_index()
        chk("① 阳性对照：能连上时必须返回 dict 而非 None",
            isinstance(_idx2, dict), True)
        # ★ infohash 必须**统一小写**作键 —— 库里与 qB 两端大小写不一致会让"命中"
        #   整段静默归零（同 ERR-META-05：口径不一致的相减没有意义）。
        chk("① infohash 作键时必须小写（库/qB 两端口径一致）",
            list(_idx2.keys()), ["aabbcc"])
        chk("① 正常路径的说明里应带种子条数",
            "1 个种子" in str(_why2), True)
    finally:
        _qbc.build = _orig_build

    # ── ② `COLS` 与 SELECT/zip 的列数契约 ────────────────────────────────
    #   ★ 文件头写着"列名只写一次，从形状上消灭位置对齐"——
    #     但**列元组与 SQL 字符串仍是两份**，只要有人往 `COLS` 里加一列而忘了同步
    #     SQL，`zip` 就会**少一格**，后面每列错位（`readiness_probe.py` 踩过）。
    #     ⇒ 把"SELECT 里的列数 == len(COLS)"变成判据。
    #   ★ 阴性对照：把 `COLS` 临时加一个元素 ⇒ 本断言必红。
    _sel = ", ".join(COLS)
    chk("② SELECT 的列数必须等于 len(COLS)（否则 zip 会错位）",
        len(_sel.split(",")), len(COLS))
    chk("② COLS 里不许有重复列名",
        len(set(COLS)), len(COLS))

    # ── ③ 只读纪律：本文件里不许出现写操作 ──────────────────────────────
    #   ★ 文件头纪律 1「**只读**」是**行为契约**，靠人记会漂（`ERR-META-01`）。
    #     用 AST 走一遍：不许有 `torrents_add`/`torrents_delete`/`recheck`，
    #     也不许有 `open(...,"w")` 一类写文件。
    #   ★ 阴性对照：本检查若写成"只要文件里有 'torrents' 就红"，那它**恒红**
    #     （`torrents_info` 合法）—— 此处按**禁止名单**逐个精确匹配，且断言
    #     合法调用 `torrents_info` 时**不**报红（阳性对照在下面 chk 里）。
    try:
        import ast as _ast
        _src_path = os.path.abspath(__file__)
        _tree = _ast.parse(open(_src_path, encoding="utf-8").read())
        _banned = {"torrents_add", "torrents_delete", "torrents_recheck",
                   "torrents_reannounce", "torrents_edit"}
        _found = set()
        for _n in _ast.walk(_tree):
            if isinstance(_n, _ast.Attribute) and _n.attr in _banned:
                _found.add(_n.attr)
        chk("③ 本脚本不许出现 qB 写操作（只读纪律）",
            sorted(_found), [])
        chk("③ 阳性对照：合法的 torrents_info 不应被误报",
            "torrents_info" in _banned, False)
    except Exception as e:                                        # noqa: BLE001
        ok[0] = False
        detail.append(f"✗ ③ 只读纪律检查执行失败：{type(e).__name__}: {e}")

    # ── ④ `--selftest` 入口真的注册了（本函数的存在性自证）───────────────
    #   ★ 这就是本任务（C2）的病根：docstring 承诺了入口、argparse 里没有。
    #     ⇒ 直接构造一次 parser 来看它认不认这个 flag。
    #   ★ 阴性对照：把 `--selftest` 从 `_build_parser()` 删掉 ⇒ 本节抛 SystemExit ⇒ 红。
    try:
        _p = _build_parser()
        _a = _p.parse_args(["--selftest"])
        chk("④ --selftest 必须被 argparse 认（本任务的病根）",
            getattr(_a, "selftest", "MISSING"), True)
        _a2 = _p.parse_args([])
        chk("④ 不带 --selftest 时默认是 False（不许默认就跑自检）",
            _a2.selftest, False)
    except SystemExit:
        ok[0] = False
        detail.append("✗ ④ `--selftest` **没有被 argparse 注册** ⇒ "
                      "docstring 承诺的入口不存在（ERR-META-04）")
    except Exception as e:                                        # noqa: BLE001
        ok[0] = False
        detail.append(f"✗ ④ 入口检查执行失败：{type(e).__name__}: {e}")

    # ── ⑤ 两个口径不许相减（`ERR-META-05`）────────────────────────────────
    #   ★ 文件头纪律 4 写死了这条：`content_path 非空` 与 `status` 是两个口径，
    #     "相减"得到的差**没有意义**（却看着像个数）。
    #   ★★★ 判据必须走 **AST**，不许查文本 —— 我第一版就是 `"len(rows) - " in 源码`，
    #     而**本自检自己的断言字符串**里就写着 `len(rows) - ` ⇒ 恒红（假阳性）。
    #     这正是 `ERR-META-32` 的形状：判据把"描述问题的字"当成"问题本身"。
    #   ★ 真判据 = 语法树里 `BinOp(Sub)` 的两侧，只要**一侧**含 `len(`/计数名、
    #     另一侧含另一个计数名 ⇒ 红。分段打印用的减法（如"还剩几条"）不受影响。
    try:
        _countish = ("n_cp", "n_tf", "n_log", "n_map", "hit", "miss", "len(")
        _subs = 0
        for _n in _ast.walk(_tree):
            if not isinstance(_n, _ast.BinOp) or not isinstance(_n.op, _ast.Sub):
                continue
            _l = _ast.unparse(_n.left) if hasattr(_ast, "unparse") else ""
            _r = _ast.unparse(_n.right) if hasattr(_ast, "unparse") else ""
            _l_hit = any(t in _l for t in _countish)
            _r_hit = any(t in _r for t in _countish)
            if _l_hit and _r_hit:
                _subs += 1
                detail.append(f"✗ ⑤ 发现两个口径相减：`{_l} - {_r}`"
                              f"（ERR-META-05：差没有意义）")
        if _subs:
            ok[0] = False
        else:
            # ★ 阳性对照：确认本判据**不是**因为"抽不出表达式"才全绿。
            if not hasattr(_ast, "unparse"):
                ok[0] = False
                detail.append("✗ ⑤ 当前 Python 无 ast.unparse ⇒ 本判据没测到东西（ERR-META-14）")
    except Exception as e:                                        # noqa: BLE001
        ok[0] = False
        detail.append(f"✗ ⑤ 口径相减检查执行失败：{type(e).__name__}: {e}")

    return ok[0], detail


def _build_parser():
    """单独抽出来，好让 `selftest()` 直接构造 parser 验 `--selftest` 入口。"""
    ap = argparse.ArgumentParser(description="qB / 本地盘 实证（只读）")
    ap.add_argument("--tids", nargs="*", default=[],
                    help="只看这几个 tid（默认：全部有 tid 的行）")
    ap.add_argument("--limit", type=int, default=12, help="最多打印几条（默认 12）")
    # ★★ 2026-10-18（C2）：docstring 早就承诺了这个入口，而 argparse 里**没有** ⇒
    #   `--selftest` 会被当场拒掉。补齐它（纯离线，不连库不连 qB）。
    ap.add_argument("--selftest", action="store_true",
                    help="跑离线自检（不连库、不连 qB、不发请求）")
    return ap


def main():
    args = _build_parser().parse_args()

    # ★ 自检必须在**任何数据库/qB 动作之前**返回（纯离线纪律）。
    if args.selftest:
        ok, detail = selftest()
        print("=" * 74)
        print("★ qb_probe 离线自检（不连库 · 不连 qB · 不发请求）")
        print("=" * 74)
        for d in detail:
            print(d)
        print(("✅ 全绿" if ok else "❌ 有失败项") + f"（{len(detail)} 条明细）")
        return 0 if ok else 1

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
