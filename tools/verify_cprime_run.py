"""端到端验证 C'（皇后路不写 `formatted_title`）—— **跑真 `run.main()`，断言库里的值**。

═══════════════════════════════════════════════════════════════════════════
它和 `run.py --selftest` 是**两层，各管一件事**（别把其中一个当成了全部）
═══════════════════════════════════════════════════════════════════════════
    · `python run.py --selftest`   —— **纯函数契约**：`_title_write_plan()` 三态
      + "接线对不对"（AST 判据）。**零依赖**，不连库、不连 qB、不发请求。
    · `python tools/verify_cprime_run.py` —— **端到端**：喂一个**假库**、跑**真的
      `run.main()`**，然后**读库**断言"写进去的到底是什么"。

★★ 为什么两层都要有（**这是本文件存在的理由**）：
  `run.py --selftest` 的 ⑦ 判据只能查"`**mt_col` 这个符号出现了""右值是那个调用"——
  它**查不到"`save_task` 最终把哪一列写进了库"**。实测过两个变异从它眼皮下溜过
  （`mt_col` 恒空 / 绕过纯函数），补了 ⑦b 才抓住。而 ⑦b 仍是**静态**判据。
  ⇒ 真值只能由**跑一遍 + 读库**给出。这一层补的就是这个缝。

═══════════════════════════════════════════════════════════════════════════
纪律（三条，缺一条这个工具就不可信）
═══════════════════════════════════════════════════════════════════════════
① **绝不碰生产库**。假库建在 `--db-dir`（默认系统临时目录）下，用 `db.py` 的
   `COLUMNS` 现抠出来的 DDL 建表 ⇒ 表结构与生产**同源**，但文件是全新的。
   ★ 本文件里**没有一处**指向 `/data/data.db` 或 NAS 路径。
② **不连 qB、不发任何网络请求**：`qbittorrentapi` 被替身注入，`assets` 的
   读目录/读日志/传图床全被替身顶掉。
③ **阴性对照**：4 条用例里**至少 1 条"期望真的被写"**（抓轨路 + 皇后路未上站），
   否则"三态都没写"也能全绿 —— 那是"测了个寂寞"（`ERR-META-14` 的形状）。

用法（★ 容器内 / 本机都行，只要 `PYTHONPATH` 能找到模块）:
    python tools/verify_cprime_run.py
    python tools/verify_cprime_run.py --keep      # 留下假库，便于自己再查
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import types

sys.stdout.reconfigure(encoding="utf-8")

# ★★ 这些键必须给：`config.py` 在 import 时就 `require_env()` 它们。
#    ★ 值是**假**的 —— 本工具不连任何服务，它们只用来"骗过 import 期的检查"。
#    ★★ 别把它们换成真值（那才是真危险）。这里全是占位串。
_DUMMY_ENV = {
    "QB_PASS": "dummy", "QB_USER": "dummy", "QB_HOST": "dummy",
    "MUSOPIA_URL": "http://dummy.invalid", "MUSOPIA_PASSKEY": "dummy",
    "MUSOPIA_COOKIE": "dummy", "PICGO_API_KEY": "dummy",
    "OPENCD_CATEGORY": "opencd-verify-fixture",
    # ★ 2026-10-11（#10/#172）：scan 识别口径改用 tracker 主机名 ⇒ 夹具也要给这个键。
    #   值 = 与替身 `_FT.tracker` 里的主机名**一致**（否则过滤后空、本工具全红）。
    "OPENCD_TRACKER": "tracker.open.cd",
}


def _columns_pairs(mod_dir):
    """从 `db.py` 源码里抠出 `COLUMNS` 字面量 ⇒ 假库的表结构与生产**同源**。

    ★ 为什么不直接 `import db` 再读 `db.COLUMNS`：那样也能，但这条更早、
      且不依赖 `db` 能不能 import（假库里我们还要把 `db.DB_PATH` 指过去）。
    ★ 更稳的做法其实是 `import db` 后读 `db.COLUMNS` —— 见 `_build_fake_db`。
    """
    src = open(os.path.join(mod_dir, "db.py"), encoding="utf-8").read()
    i = src.index("COLUMNS = [")
    block = src[i: src.index("\n]", i) + 2]
    return re.findall(r'\(\s*"([a-z_0-9]+)"\s*,\s*"([^"]+)"\s*\)', block)


# ── 5 条用例：★ 必须**含"期望真的被写"**的阳性对照（见纪律③）──
#
# ★★★ 哨兵（sentinel）律：**凡是"期望不变"的用例，初始值必须是 `parse_title()`
#   ` 不可能产出的字符串**。否则"没写"与"写了自家产物"两种情况的**库里值相等**
#   ⇒ 判据**区分不了**它们 ⇒ 假绿。
#   实测踩过（变异 X3）：C 的初值原本就是畸形标题，而"写了自家产物"也长那个样
#   ⇒ `got == want` ⇒ **绿**，可是代码已经错了。⇒ 初值改成 `SENTINEL_C`。
_SENT = "★原值(必须保住·哨兵)"


def _parse_title_like(name):
    """复刻 `run.parse_title()` 对 fixture 名的产物形状（**只用于写"期望值"**）。

    ★ 刻意**不复用**真 `parse_title()`：那样就成了"拿被测代码算期望值"
      ⇒ 它错了期望值也跟着错，判据恒绿。这里是**独立**实现的对照。
      fixture 名形如 `抓轨原始名B` / `皇后原始名C`，`assets.has_cue=True` ⇒ 整轨。
    """
    return f"{name} - FLAC 整轨"


CASES = [
    dict(
        key="A", hash="A1" * 8, name="皇后路·有快照·已上站（短路支）",
        # ★ 初值用**哨兵**：这条走短路跳过支（不进 `save_task`）⇒ 库里必须**仍是哨兵**。
        #   若哪天有人把短路支去掉、让它掉进普通写支 ⇒ 会写成皇后原文 ⇒ **红**。
        #   ★ 这也顺带钉住"短路支不许被误删"。
        row=dict(title=_SENT, oid="12345",
                 mj=json.dumps({"title": "皇后原文A 2026 - FLAC 整轨 - OpenCD"},
                               ensure_ascii=False),
                 site="EXISTS"),
        want=_SENT,
        why="走**短路跳过支** ⇒ 不进 save_task ⇒ 库里必须**仍是哨兵**。"
            "★ 它**不是** C' 起作用的证据（这条路径本来就不会被写），"
            "也**验不出**普通写支的注入点缺失 —— 那是 D/E 的活。",
    ),
    dict(
        key="B", hash="B2" * 8, name="抓轨路（阳性对照）",
        row=dict(title=_SENT, oid=None, mj=None, site=None),
        want=_parse_title_like("抓轨原始名B"),
        why="★ **必须被 scan 写**（返工前这里是'没被写' = 我改出来的 bug）。"
            "初值是哨兵 ⇒ 若「写不进去」，库里会留着哨兵 ⇒ **红**。"
            "没有这条，'三态全不写'也能全绿。",
    ),
    dict(
        key="C", hash="C3" * 8, name="皇后路·**无**快照·已上站",
        row=dict(title=_SENT, oid="999", mj=None, site="EXISTS"),
        want=_SENT,
        why="**不写**、保留库内原值（不拿自家产物顶替）。"
            "★ 初值是**哨兵** ⇒ 一旦被写（哪怕是写成自家产物）⇒ 库里不再是哨兵 ⇒ **红**。"
            "（初值若用畸形标题，就区分不了'没写'与'写了畸形值' —— 变异 X3 假绿的教训。）",
    ),
    dict(
        key="D", hash="D4" * 8, name="皇后路·有快照·未上站",
        row=dict(title=_SENT, oid="777",
                 mj=json.dumps({"title": "皇后原文D 2026 - FLAC 整轨 - OpenCD"},
                               ensure_ascii=False),
                 site=None),
        want="皇后原文D 2026 - FLAC 整轨 - OpenCD",
        why="★ 走**普通写支** ⇒ `mt_col` 生效，把库内原值**换成**皇后原文 —— "
            "**这条才是 C' 真起作用的路径**。",
    ),
    # ★★★ E：**专门用来验"注入点没漏"**的用例（A 验不出这点，见 A 的 why）。
    #   它与 D 同为"皇后路 + 有快照 + 未上站"，但**值不同** ⇒ 两条都红才说明
    #   "普通写支的 `**mt_col` 真的在"。★ 若把 `**mt_col` 删掉，D 与 E **同时红**。
    dict(
        key="E", hash="E5" * 8, name="皇后路·有快照·未上站（注入点对照）",
        row=dict(title=_SENT, oid="555",
                 mj=json.dumps({"title": "皇后原文E 2026 - APE 分轨 - OpenCD"},
                               ensure_ascii=False),
                 site=None),
        want="皇后原文E 2026 - APE 分轨 - OpenCD",
        why="★ 与 D 同路径、**不同值** ⇒ 两条同时红才说明普通写支的注入点在。"
            "单看一条可能被别的巧合掩盖。",
    ),
]


def main():
    ap = argparse.ArgumentParser(description="端到端验证 C'（跑假 run.main()，断言库值）")
    ap.add_argument("--db-dir", default="", help="假库放哪（默认系统临时目录）")
    ap.add_argument("--keep", action="store_true", help="跑完不删假库")
    ap.add_argument("--mod-dir", default="",
                    help="模块目录（默认 = 本文件的上一级，即仓库根）")
    args = ap.parse_args()

    mod_dir = os.path.abspath(args.mod_dir or os.path.join(os.path.dirname(__file__), ".."))
    out_dir = os.path.abspath(args.db_dir) if args.db_dir else tempfile.mkdtemp(prefix="cprime-")
    os.makedirs(out_dir, exist_ok=True)
    db_path = os.path.join(out_dir, "fake_run.db")

    # ── 环境（全假值；见 `_DUMMY_ENV`）──
    for k, v in _DUMMY_ENV.items():
        os.environ[k] = v
    os.environ["DATA_DIR"] = out_dir
    os.environ["OUTPUT_TORRENT_DIR"] = os.path.join(out_dir, "output_torrents")
    os.makedirs(os.environ["OUTPUT_TORRENT_DIR"], exist_ok=True)
    sys.path.insert(0, mod_dir)

    print("=" * 74)
    print("C' 端到端验证 —— 跑**真 `run.main()`**，断言**库里写进去的值**")
    print("=" * 74)
    print(f"  模块目录 : {mod_dir}")
    print(f"  假库     : {db_path}")
    print("  ★ 假库是**新建**的，用 `db.COLUMNS` 同源建表 ⇒ **零生产接触**")
    print("  ★ 不连 qB、零网络请求（替身注入）")

    # ── 建假库（用真 db.COLUMNS ⇒ 表结构与生产同源）──
    import config
    import db
    # ★★★ 必须改 **`config.DB_PATH`**，不是 `db.DB_PATH` ——
    #   `db.connect()` 里是 `sqlite3.connect(config.DB_PATH, …)`（`db.py:231`），
    #   `db` 模块上**根本没有** `DB_PATH` 这个全局。
    #   ★ 第一版写的是 `db.DB_PATH = db_path` ⇒ **静默不生效** ⇒ `known` 查询
    #     落到 `DATA_DIR` 下那个（空的）库 ⇒ **每一行都查不到** ⇒ 全走抓轨路。
    #     症状：A/C 因为"期望值恰好等于没写的值"而**假绿**，只有 B/D 红。
    #   ⇒ 这一条踩坑记在注释里：**改 `config.DB_PATH`，那才是唯一的真源。**
    config.DB_PATH = db_path
    if hasattr(db, "DB_PATH"):
        db.DB_PATH = db_path                       # 双保险（若将来 db 也加了同名全局）
    if os.path.exists(db_path):
        os.remove(db_path)
    c = sqlite3.connect(db_path)
    c.execute("CREATE TABLE tasks (" + ", ".join(f"{n} {t}" for n, t in db.COLUMNS) + ")")
    for cs in CASES:
        r = cs["row"]
        c.execute(
            "INSERT INTO tasks (info_hash, torrent_name, formatted_title, opencd_tid,"
            " mapped_json, status, small_descr, cover_url, site_status)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (cs["hash"], r["title"], r["title"], r["oid"], r["mj"],
             "SUCCESS", "旧副标题", "", r["site"]))
    c.commit()
    c.close()
    # ★ 自检：**确认真的连到假库**（否则整个工具是"测了个寂寞"）
    _c = sqlite3.connect(db_path)
    _n = _c.execute("SELECT count(*) FROM tasks").fetchone()[0]
    _c.close()
    _reached = db.connect()
    _rn = _reached.execute("SELECT count(*) FROM tasks").fetchone()[0]
    _reached.close()
    if _rn != _n:
        print(f"\n❌ 假库没接上：`db.connect()` 读到 {_rn} 行，假库有 {_n} 行 ⇒ "
              "判据全部作废（`config.DB_PATH` 没改对）")
        return 2
    print(f"  ✔ 假库已接上（`db.connect()` 读到 {_rn} 行，与建库数一致）")

    # ── 替身：qB ──
    class _FT:
        def __init__(self, h, n, tracker=""):
            self.hash, self.name = h, n
            self.content_path = os.path.join(out_dir, "no-such-content-path")
            # ★★ 2026-10-11（#10/#172）：scan 识别口径从 `category` 改成 **tracker 主机名**
            #   ⇒ 替身**必须**带上 `.tracker`，否则 `run._torrent_is_opencd` 判否 ⇒ 本夹具
            #   里的皇后行会被整体过滤掉、这个工具全红（这正是它该抓的"口径变了"）。
            self.tracker = tracker

    class _FakeQB:
        def auth_log_in(self):
            return True

        # ★ 2026-10-11：scan 现在调 `torrents_info()`（**不带参数**）再在 Python 侧
        #   按 tracker 过滤（qB 服务端的 `tracker=` 参数在本版本无效，见 run.py）。
        def torrents_info(self, category=None, tracker=None):
            # ★★ 别搞错参数序：`_FT(hash, name)` —— 第一版把两者写反了，
            #   于是 `torrent.hash` 成了**种子名**、`torrent.name` 成了 hash ⇒
            #   `known[info_hash]` **一次都没命中** ⇒ 每行 `oid=None` ⇒ 全走抓轨路。
            #   症状：日志里 C/D 也印 `来源: 抓轨路(parse_title)` ⇒ B/D 期望值不符。
            #   ★ 这正是本工具的价值：它把这个**fixture 错误**变成了两条红色判据。
            # ★ 皇后路（`oid` 非空）的种给**真 tracker**，抓轨路给空 ⇒ 过滤后两者都留下
            #   （取决于用例）：B 是抓轨路却也在 qB 里 ⇒ 它**不在**皇后列表里，但本工具
            #   直接调 `run._scan_main`，其 `_all` 全量、用例覆盖两类 —— 故这里让**所有**
            #   用例都带皇后 tracker，确保它们都进 `torrents`（本工具不测"过滤掉别的"，
            #   那由 `run.py --selftest` 的 ⑨r 专测）。
            return [_FT(cs["hash"],
                        ("皇后原始名" if cs["row"]["oid"] else "抓轨原始名") + cs["key"],
                        "https://tracker.open.cd/announce.php?passkey=x")
                    for cs in CASES]

    _qb = types.ModuleType("qbittorrentapi")
    _qb.Client = lambda **kw: _FakeQB()
    sys.modules["qbittorrentapi"] = _qb

    # ── 替身：assets（读目录 / 读日志 / 传图床 —— 一个都别真做）──
    import assets
    assets.read_logs = lambda p: [{"content": "LOG", "filename": "a.log", "label": "CD1"}]
    assets.read_tracklist = lambda p: "1. t\n2. t"
    assets.has_cue = lambda p: True                # ⇒ track_type = 整轨
    assets.find_cover = lambda p: ""
    assets.upload_to_picgo = lambda p: ""

    import run
    run.create_musopia_torrent = lambda cp, sp: open(sp, "wb").write(b"d8:announce0:e")
    config.print_runtime_info = lambda: None

    sys.argv = ["run.py"]
    print("\n" + "─" * 74)
    # ★★★ 必须兜异常 —— **"没跑完"不许和"跑完了但结果对"混在一起**（`ERR-META-14`）。
    #
    # 实测踩过（变异 X7：`mt_src, mt_col, mt_why = …` 接错位）：
    #   接错位后 `mt_col` 拿到的是**来源字符串** ⇒ `save_task(**mt_col)` 把字符串
    #   展开成"逐字符的 kwarg" ⇒ **TypeError**。
    #   ★ 后果不是"一行红"，而是**第 2 层一条判据都不印**（提前退出）——
    #     看起来像"工具没输出"，而不是像"接线错了"。
    #   ★ 而它在**生产**里的表现更硬：`run.main()` 抛异常 ⇒ **整个 scan 阶段什么都不写**。
    #   ⇒ 兜住它，变成一条**显式判据**（`bad` 会把它算进失败、exit≠0）。
    _boom = None
    try:
        run.main()
    except Exception as e:                                      # noqa: BLE001
        _boom = e
        print(f"💥 run.main() 抛异常：{type(e).__name__}: {e}")
        print("   ★ 这**不是**「没输出」—— 是判断依据作废，下面会算成一条失败。")
    print("─" * 74)

    # ── 判据：读库 ──
    c = sqlite3.connect(db_path)
    got = {h: t for h, t in c.execute("SELECT info_hash, formatted_title FROM tasks")}
    c.close()

    print("\n" + "=" * 74)
    print("★ 判据（读**假库**，不是读日志）")
    print("=" * 74)
    bad = []
    # ★★ 先处置"没跑完"：崩溃 ⇒ **所有 5 条判据都作废**，不能只说"5 条都过了"。
    #   ★ 顺序上放在最前：它比任何一条具体判据都严重（`ERR-META-14`）。
    if _boom is not None:
        bad.append(f"run.main() 崩溃:{type(_boom).__name__}")
        print(f"  ❌★ run.main() **抛异常** ⇒ 下面 5 条判据**全部作废**"
              f"（{type(_boom).__name__}: {_boom}）")
        print("     ★ 别把「没跑完」读成「跑完了没问题」—— 那是 `ERR-META-14` 的形状。")
    for cs in CASES:
        g, w = got[cs["hash"]], cs["want"]
        ok = g == w
        if not ok:
            bad.append(cs["key"])
        print(f"  {'✅' if ok else '❌'} {cs['key']} {cs['name']}")
        print(f"       got = {g!r}")
        if not ok:
            print(f"       want= {w!r}")
        print(f"       {cs['why']}")
    # ★★ 哨兵完整性（"期望不变"的用例**必须**带哨兵初值）——
    #   否则"没写"与"写了自家产物"库里值相等 ⇒ 假绿（变异 X3 的教训）。
    #   ★ 判据：**凡 `want == row['title']`（期望不变）的用例，初值必须含哨兵标记**。
    _nosent = [cs["key"] for cs in CASES
               if cs["want"] == cs["row"]["title"] and _SENT not in cs["row"]["title"]]
    if _nosent:
        bad.append(f"哨兵缺失:{_nosent}")
        print(f"\n  ❌ 用例 {_nosent} 是「期望不变」但初值**没有哨兵** ⇒ "
              "它区分不了'没写'与'写了自家产物'（会假绿）")

    # ★ 阳性对照必须**至少一条真的被改动**（否则"全没写"也能绿 —— 纪律③）
    changed = [cs["key"] for cs in CASES if cs["row"]["title"] != cs["want"]]
    if not changed:
        bad.append("阳性对照缺失")
        print("\n  ❌ **没有任何一条「期望被改动」的用例** ⇒ 这个测试测不到「写」这件事")
    else:
        print(f"\n  ✔ 阳性对照（期望被改动）: {changed}")

    print("\n" + "=" * 74)
    if bad:
        print(f"❌ 失败：{len(bad)} 项不符 → {bad}")
    else:
        print("✅ 全部符合（A/B/C/D/E 五态 + 哨兵 + 阳性对照）")

    if args.keep:
        print(f"\n★ 假库留着: {db_path}（用完自己删）")
    else:
        shutil.rmtree(out_dir, ignore_errors=True)
        print("\n★ 假库已删（**生产库从头到尾没被碰过**）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
