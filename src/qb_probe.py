"""只读探查任务表记录与磁盘文件、qBittorrent 做种状态一致性。

核对 tasks 表中 content_path 目录与 torrent_file 文件的物理存在性，
并通过 qBittorrent API（torrents_info）按 info_hash 匹配查询。
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


# 数据库 tasks 查询列元组
COLS = ("info_hash", "opencd_tid", "torrent_name", "status", "site_status",
        "content_path", "torrent_file", "log_content", "mapped_json",
        "updated_at")


def load_rows(tids=()):
    """加载具有 opencd_tid 的任务行。"""
    conn = db.connect()
    try:
        conn.execute("PRAGMA query_only=1")
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
    """从 qBittorrent 批量获取种子信息，构建 infohash -> {content_path, name, save_path} 索引字典。

    只读调用 torrents_info()；如无法连接返回 (None, 原因描述)。
    """
    try:
        import qb_client
    except ImportError as e:
        return None, f"没装 qbittorrentapi（{e}）"
    try:
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
        return None, f"{type(e).__name__}: {str(e)[:100]}"


def selftest():
    """离线自检逻辑，验证返回契约、SQL 列同步、只读纪律等。返回 (ok, [detail])。"""
    detail = []
    ok = [True]

    def chk(name, got, want):
        if got != want:
            ok[0] = False
            detail.append(f"✗ {name}\n     got = {got!r}\n     want= {want!r}")
        return got == want

    # 1. qb_index() 返回值契约
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
        chk("① infohash 作键时必须小写（库/qB 两端口径一致）",
            list(_idx2.keys()), ["aabbcc"])
        chk("① 正常路径的说明里应带种子条数",
            "1 个种子" in str(_why2), True)
    finally:
        _qbc.build = _orig_build

    # 2. COLS 与 SELECT/zip 的列数契约
    _sel = ", ".join(COLS)
    chk("② SELECT 的列数必须等于 len(COLS)（否则 zip 会错位）",
        len(_sel.split(",")), len(COLS))
    chk("② COLS 里不许有重复列名",
        len(set(COLS)), len(COLS))

    # 3. 只读纪律（AST 校验不允许出现写操作）
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
    except Exception as e:
        ok[0] = False
        detail.append(f"✗ ③ 只读纪律检查执行失败：{type(e).__name__}: {e}")

    # 4. --selftest 参数支持
    try:
        _p = _build_parser()
        _a = _p.parse_args(["--selftest"])
        chk("④ --selftest 必须被 argparse 认",
            getattr(_a, "selftest", "MISSING"), True)
        _a2 = _p.parse_args([])
        chk("④ 不带 --selftest 时默认是 False",
            _a2.selftest, False)
    except SystemExit:
        ok[0] = False
        detail.append("✗ ④ `--selftest` 没有被 argparse 注册")
    except Exception as e:
        ok[0] = False
        detail.append(f"✗ ④ 入口检查执行失败：{type(e).__name__}: {e}")

    # 5. 口径不可相减检查
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
                detail.append(f"✗ ⑤ 发现两个口径相减：`{_l} - {_r}`")
        if _subs:
            ok[0] = False
        else:
            if not hasattr(_ast, "unparse"):
                ok[0] = False
                detail.append("✗ ⑤ 当前 Python 无 ast.unparse")
    except Exception as e:
        ok[0] = False
        detail.append(f"✗ ⑤ 口径相减检查执行失败：{type(e).__name__}: {e}")

    return ok[0], detail


def _build_parser():
    ap = argparse.ArgumentParser(description="qB / 本地盘 实证（只读）")
    ap.add_argument("--tids", nargs="*", default=[],
                    help="只看这几个 tid（默认：全部有 tid 的行）")
    ap.add_argument("--limit", type=int, default=12, help="最多打印几条（默认 12）")
    ap.add_argument("--selftest", action="store_true",
                    help="跑离线自检（不连库、不连 qB、不发请求）")
    return ap


def main():
    args = _build_parser().parse_args()

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
    print("★ qB 实例 / 本地盘 状态验证（只读）")
    print("=" * 74)
    print(f"   QB_HOST      = {config.QB_HOST}")
    print(f"   DATA_DIR     = {config.DATA_DIR}")
    print(f"   TORRENT_DIR  = {config.OUTPUT_TORRENT_DIR}")

    rows = load_rows(args.tids)
    print(f"\n候选（有 tid 的行）: {len(rows)} 条")

    # 1. 字段及文件路径分布统计
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
    print("\n[① 数据库字段分布与对应文件磁盘存在性]")
    print(f"   content_path  非空          : {n_cp:>4} / {len(rows)}")
    print(f"   ├ 路径在磁盘真实存在        : {cp_exists:>4}")
    print(f"   torrent_file  非空          : {n_tf:>4} / {len(rows)}")
    print(f"   ├ 文件在磁盘真实存在        : {tf_exists:>4}")
    print(f"   log_content   非空          : {n_log:>4}")
    print(f"   mapped_json   非空          : {n_map:>4}")

    # 2. 交叉状态分布
    print("\n[② 任务状态与 content_path 分布]")
    cross = {}
    for r in rows:
        k = (r["status"] or "-", "有" if (r["content_path"] or "").strip() else "无")
        cross[k] = cross.get(k, 0) + 1
    for (stt, cp), n in sorted(cross.items(), key=lambda kv: -kv[1]):
        print(f"   {stt:<18} content_path={cp}   {n:>4}")

    # 3. qBittorrent 状态匹配
    idx, why = qb_index()
    print(f"\n[③ qB 查询（{config.QB_HOST}）]")
    if idx is None:
        print(f"   ⏭ 本段跳过（{why}）")
    else:
        print(f"   ✅ 读到 {why}")
        hit = miss = 0
        tf_in_qb = 0
        for r in rows:
            rec = idx.get((r["info_hash"] or "").lower())
            if rec:
                hit += 1
                if (rec["content_path"] or "").strip() == \
                        (r["content_path"] or "").strip():
                    tf_in_qb += 1
            else:
                miss += 1
        print(f"   ├ infohash 在 qB 命中       : {hit:>4} / {len(rows)}")
        print(f"   ├ infohash 在 qB 不存在     : {miss:>4}")
        print(f"   └ qB content_path 与库一致  : {tf_in_qb:>4}")

    # 4. 明细展示
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
