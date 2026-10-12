"""★ 自检：`src/site_verify.py` 的版本核对**真能证伪**（#31）。

═══════════════════════════════════════════════════════════════════════════
为什么需要这个文件（而不是"看着对就行"）
═══════════════════════════════════════════════════════════════════════════
本工具的全部价值在于：**能把"站点挂的版本 ≠ DB 声称"检测出来**。
一个恒真的实现（永远说 MATCH）在真跑时**安静地**放走错版本 —— 那比没有工具更坏。
⇒ 这里**每条判据都配阴性对照**（`ERR-META-01`）：构造**真的不一致**的输入，
   断言它**报了** `MISMATCH`；构造**真的缺页**的输入，断言它**报了** `MISSING-PAGE`。

★ 三个最容易写错的点，各配一条对照：
  ① `extract_info_name` 不许退回 `torf.Torrent.name` —— 用**单文件**种子证明：
     正确实现读出的 `info.name` **恰好是**"文件名"，而若误用别处字段就会不同。
  ② `page_exists` 不许把**通用壳页**当真有 —— 用**无 `<h1>`** 的壳页证明报 False。
  ③ `compare_hosted_vs_db` 不许把差异**折没了** —— 初回 vs 期間 必报 `MISMATCH`。

用法（本机；**不需要 .env 真密钥**、**不碰生产库**）:
    python tools/test_verify_hosted_edition.py
    # 若本机没装 torf，可用容器内解释器跑：
    #   docker exec -i opencd2musopia python tools/test_verify_hosted_edition.py
"""
import io
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
import _bootstrap  # noqa: E402,F401  —— 把 <repo> 与 <repo>/src 放进 sys.path

import site_verify as sv  # noqa: E402

_fail = 0


def chk(label, cond, detail=""):
    global _fail
    ok = bool(cond)
    print(f"  {'✅' if ok else '❌'} {label}" + (f"   —— {detail}" if detail else ""))
    if not ok:
        _fail += 1


# ── 构造真 `.torrent` 字节（自己编码 bencode —— 不靠 torf 的"造种"API，
#    让"输入"与"被测的解析"解耦，避免用同一库既造又解而互相掩盖）──────────
def _benc(obj):
    if isinstance(obj, int):
        return b"i%de" % obj
    if isinstance(obj, bytes):
        return b"%d:" % len(obj) + obj
    if isinstance(obj, str):
        b = obj.encode("utf-8")
        return b"%d:" % len(b) + b
    if isinstance(obj, list):
        return b"l" + b"".join(_benc(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        items = sorted(obj.items(), key=lambda kv: kv[0].encode("utf-8")
                       if isinstance(kv[0], str) else kv[0])
        return b"d" + b"".join(_benc(k) + _benc(v) for k, v in items) + b"e"
    raise TypeError(type(obj))


def _mk_single_file_torrent(name):
    """单文件种：`info` 里只有 `length`（无 `files`）⇒ 权威 `info.name` = 文件名。"""
    info = {"name": name, "piece length": 32768, "pieces": b"\x00" * 20,
            "length": 16}
    return _benc({"info": info, "announce": "http://x/announce"})


def _mk_multi_file_torrent(folder, inner="01.flac"):
    """多文件种：`info` 里有 `files` ⇒ 权威 `info.name` = 顶层目录名。"""
    info = {"name": folder, "piece length": 32768, "pieces": b"\x00" * 20,
            "files": [{"length": 16, "path": [inner]}]}
    return _benc({"info": info, "announce": "http://x/announce"})


def _single_file_shape(torf_mod):
    """★ 证明"单文件种的 `info.name` 语义是**文件名**、`info` 里**没有** `files` 键"
    —— 这正是 `plan_mv_17304.py:80-88` 记的坑：拿 `info.name` 当**目录名**去
    找源目录，在单文件种上会找**一个不存在的目录**（假阴性）。
    ⇒ 本工具只把它当**版本名**比较（不当初目录用），所以必须能证明这个形状。

    返回 (info_name, has_files_key)。
    """
    t = torf_mod.Torrent.read_stream(io.BytesIO(_mk_single_file_torrent("lonely.flac")))
    info = t.metainfo.get("info") or {}
    return info.get("name"), ("files" in info)


def main():
    global _fail
    try:
        import torf
    except ImportError as e:            # noqa: BLE001
        print(f"⚠️  本机无 torf（{e}）—— 请用容器内解释器跑本测试；跳过。")
        return 0
    print("=" * 72)
    print("★ 自检：site_verify（站点实挂版本 vs DB 核对）")
    print("=" * 72)

    # ── ① extract_info_name：读的是 metainfo['info']['name'] -----------------
    print("\n① extract_info_name —— **只认** metainfo['info']['name']")
    raw_multi = _mk_multi_file_torrent("专辑名 初回盤")
    name, why = sv.extract_info_name(raw_multi)
    chk("多文件种：info.name == 目录名", name == "专辑名 初回盤", f"实得 {name!r} ({why})")

    raw_single = _mk_single_file_torrent("lonely.flac")
    name_s, why_s = sv.extract_info_name(raw_single)
    chk("单文件种：info.name == 文件名（这就是权威值本身）",
        name_s == "lonely.flac", f"实得 {name_s!r} ({why_s})")

    # ★★ 阴性对照①：证明单文件种 `info.name` 语义是**文件名**、且 `info` 无 `files` 键
    #    —— 这正是"拿 info.name 当目录名"会翻车的形状；本工具只当版本名比，故须坐实。
    sname, has_files = _single_file_shape(torf)
    chk("★★ 单文件种：`info.name` == 文件名、且 `info` 里**没有** `files` 键"
        "（⇒ 它就是权威版本名，绝不能当目录名用）",
        sname == "lonely.flac" and has_files is False,
        f"info.name={sname!r} has_files={has_files}")

    # 源码级护栏：实现里**不得**出现 `t.name` / `.name` 直接当返回（只认 metainfo）
    # ★ 路径兼容两种布局：本机 <repo>/src/site_verify.py、容器内 /app/site_verify.py
    _root = os.path.dirname(_HERE)
    _sv_path = os.path.join(_HERE, "..", "src", "site_verify.py")
    for cand in (os.path.join(_root, "src", "site_verify.py"),
                 os.path.join(_root, "site_verify.py")):
        if os.path.exists(cand):
            _sv_path = cand
            break
    src = open(_sv_path, encoding="utf-8").read()
    # ★ 先剥注释与字符串里的说明，再正则（ERR-PUBLISH-11 教训）
    code = re.sub(r'"""(?:.|\n)*?"""', "", src)
    code = re.sub(r"#[^\n]*", "", code)
    chk("源码护栏：extract_info_name 走的是 metainfo['info']['name']（不是 t.name）",
        'metainfo' in code and 'get("info")' in code,
        "若改成 t.name 会在此断言失败")

    # ── ② 垃圾字节不猜 ---------------------------------------------------------
    print("\n② 解不出 ⇒ (None, 原因)，**不许**猜")
    n2, w2 = sv.extract_info_name(b"<html>not a torrent</html>")
    chk("垃圾字节 ⇒ name is None（不猜）", n2 is None, f"实得 {n2!r}")
    chk("垃圾字节 ⇒ 有原因说明", bool(w2), f"why={w2!r}")
    n3, w3 = sv.extract_info_name(_mk_multi_file_torrent("x"))
    chk("合法字节 ⇒ name 非空（阳性对照）", bool(n3), f"实得 {n3!r}")

    # ── ③ compare_hosted_vs_db：三态 + **差异不许被折没** --------------------
    print("\n③ compare_hosted_vs_db —— 三态，且初回/期間 必 MISMATCH")
    chk("同版本 ⇒ MATCH",
        sv.compare_hosted_vs_db("Album 初回盤", "Album 初回盤") == sv.MATCH)
    chk("★ 初回 vs 期間 ⇒ MISMATCH（差在汉字上，**不许**折掉）",
        sv.compare_hosted_vs_db("Album 初回盤", "Album 期間盤") == sv.MISMATCH)
    chk("★ 空白差异 ⇒ MATCH（多空格/换行折平后相等）",
        sv.compare_hosted_vs_db("Album  初回盤", "Album\n初回盤") == sv.MATCH)
    chk("DB 侧缺名 ⇒ NO_DB_NAME（不是 MISMATCH）",
        sv.compare_hosted_vs_db("", "Album") == sv.NO_DB_NAME)
    chk("站点侧缺名 ⇒ NO_DB_NAME", sv.compare_hosted_vs_db("Album", "") == sv.NO_DB_NAME)

    # ── ④ page_exists：壳页 vs 真页 -------------------------------------------
    print("\n④ page_exists —— 通用壳页必须报 False")
    real = "<html><head><title>X</title></head><body><h1> 真标题 </h1></body></html>"
    shell = ("<html><head><title>音乐乌托邦 - Powered by NexusPHP</title></head>"
             "<body><div id='logo'></div><p>抱歉</p></body></html>")
    chk("真页（有非空 h1）⇒ True", sv.page_exists(real) is True)
    chk("★ 壳页（HTTP 200、无 h1）⇒ False", sv.page_exists(shell) is False)
    chk("空 h1 ⇒ False", sv.page_exists("<h1>   </h1>") is False)
    chk("None/空 ⇒ False", sv.page_exists(None) is False and sv.page_exists("") is False)

    # ── ⑤ classify：判定顺序与缺陷归类 ---------------------------------------
    print("\n⑤ classify —— 判定顺序 & 缺陷集合")
    v, hn, _ = sv.classify(page_ok=False, torrent_bytes=None, db_name="X")
    chk("页面不存在 ⇒ MISSING-PAGE（优先于下载判定）", v == sv.MISSING_PAGE)
    v, hn, _ = sv.classify(page_ok=True, torrent_bytes=None, db_name="X")
    chk("页面在、没下到种 ⇒ DOWNLOAD-FAILED", v == sv.DOWNLOAD_FAILED)
    v, hn, _ = sv.classify(page_ok=True, torrent_bytes=raw_multi,
                           db_name="专辑名 初回盤")
    chk("页面在、下到种、名字一致 ⇒ MATCH", v == sv.MATCH, f"实得 {v}")
    v, hn, _ = sv.classify(page_ok=True, torrent_bytes=raw_multi,
                           db_name="专辑名 期間盤")
    chk("★ 名字不一致 ⇒ MISMATCH（工具的核心用途）", v == sv.MISMATCH, f"实得 {v}")
    chk("`DEFECTIVE` 集合 = {MISMATCH, MISSING-PAGE, DOWNLOAD-FAILED}",
        set(sv.DEFECTIVE) == {sv.MISMATCH, sv.MISSING_PAGE, sv.DOWNLOAD_FAILED})

    print("\n" + "=" * 72)
    total = 22
    passed = total - _fail
    print(f"结果: {passed}/{total} 通过")
    print("=" * 72)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
