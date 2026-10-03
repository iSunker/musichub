#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档只读投影 —— 四个审计脚本的合并体（headings / refs / terms / probes）。

═══════════════════════════════════════════════════════════════════════════
★ 本文件是 `01_headings.py` / `02_refs.py` / `03_terms.py` / `04_probes.py`
  的合并体（读法：先看子命令，再看对应的段）。原先它们是四个独立脚本，
  各自的模块头写满了来之不易的教训，合并后**逐字保留**在各自的段里。

    python tools/doc-audit/audit.py headings [文档目录]   # 标题树
    python tools/doc-audit/audit.py refs     [文档目录]   # 引用对账
    python tools/doc-audit/audit.py terms    [文档目录]   # terms.txt 扫描
    python tools/doc-audit/audit.py probes                # 只读探针

★ 合并时**只做了三件事**（其余一字未动）：
    ① 四个 `main()` → `_main_headings/_main_refs/_main_terms/_main_probes`；
    ② 四个文件各自在模块级算的 `TOOLS/REPO/OUT`（**逐字相同**）收敛成一份；
       `DOCS`（依赖 `sys.argv[1]`）改由分发器在调用前**按该子命令的剩余参数**重算
       —— 语义与原来"`sys.argv[1]` 即文档目录"**逐字一致**，只是 argv 前多了一层子命令。
    ③ `mdwalk` 是四个脚本共用的姊妹模块，**原样保留**（`from mdwalk import …`）。

★ `_ro` / `_stale_where` 等名字**一字不改**：`tools/gen_counts.py` 会 importlib
  加载本文件的 `_ro()` 与 `_stale_where()`（口径唯一真源，`ERR-META-03`）。
"""
import json
import re
import shutil
import sqlite3
import sys
import threading
import time
import types
import urllib.request
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# ★ 四个脚本原先各自算这三项（**逐字相同**）⇒ 收敛成一份。
TOOLS = Path(__file__).resolve().parent           # out/ 永远跟着脚本走，不往文档树里拉屎
REPO = TOOLS.parent.parent                        # <repo>/tools/doc-audit → <repo>
OUT = TOOLS / "out"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mdwalk import iter_lines as walk, scan_defects  # noqa: E402

# ★ `DOCS` 由分发器在调用子命令前重算（见 `_docs_from_argv`）；模块级先给个兜底。
DOCS = REPO


def _docs_from_argv(rest):
    """按子命令的**剩余参数**算文档目录 —— 复刻原先 `sys.argv[1]` 的语义。

    原先每个脚本是 `DOCS = Path(sys.argv[1] if len(sys.argv) > 1 else REPO).resolve()`。
    合并后 argv[1] 是子命令名，剩下才是真参数 ⇒ 这里只看剩余参数里的**第一个位置参数**
    （跳过 `-` 开头的选项，兼容将来加 flag 而不破坏位置参数语义）。
    """
    pos = [a for a in rest if not a.startswith("-")]
    return Path(pos[0] if pos else REPO).resolve()


# =====================================================================
# ============ 段 1：audit.py headings ================================
# ============ 标题树（原 01_headings.py）=============================
# =====================================================================
"""提取 md 标题树。

★ 围栏规则按 CommonMark，不是"见到 ``` 就翻"：
  开栏 = 行首 0~3 个空格 + >=3 个反引号/波浪号 + 可选 info string
  闭栏 = 同种字符、**不短于**开栏、且**后面什么都没有**
  —— 用"见到就翻"的写法，会在「行尾三反引号」那一行失步，
     之后整条尾巴被吞掉（姊妹项目实测少 115 个标题）。

用法：python audit.py headings <文档目录>      （默认仓库根）
输出：<脚本目录>/out/headings.tsv
"""

HEAD = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def extract(path):
    rows, stack = [], []
    for lineno, line in walk(path):
        h = HEAD.match(line)
        if not h:
            continue
        lvl, title = len(h.group(1)), h.group(2).strip()
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        parent = stack[-1][2] if stack else ""
        slug = "%s/%s" % (parent, title) if parent else title
        rows.append((path.name, lvl, lineno, slug, title, parent))
        stack.append((lvl, title, slug))
    return rows


def _main_headings():
    OUT.mkdir(parents=True, exist_ok=True)
    all_rows = []
    files = sorted(DOCS.glob("*.md"))
    if not files:
        print("[!!] %s 下没有 .md —— 文档目录传对了吗？" % DOCS)
        return
    n_def = scan_defects(files)
    if n_def:
        print("    ↑ 围栏缺陷会同时骗解析器和渲染器，**先修再计数**")
    print()

    for f in files:
        rows = extract(f)
        all_rows.extend(rows)
        prev = 0
        for _, lvl, ln, _, title, _ in rows:
            if lvl > prev + 1:
                print("[!] 层级跳跃 %s:%d h%d→h%d %r" % (f.name, ln, prev, lvl, title))
            prev = lvl
        lv = {}
        for _, lvl, _, _, _, _ in rows:
            lv[lvl] = lv.get(lvl, 0) + 1
        print("  %-16s %4d 标题   %s" % (
            f.name, len(rows),
            "  ".join("h%d=%d" % (k, lv[k]) for k in sorted(lv))))
    with (OUT / "headings.tsv").open("w", encoding="utf-8") as fp:
        fp.write("file\tlevel\tline\tslug\ttitle\tparent\n")
        for r in all_rows:
            fp.write("\t".join(map(str, r)) + "\n")
    print("[ok] %d 个标题 → %s" % (len(all_rows), OUT / "headings.tsv"))


# =====================================================================
# ============ 段 2：audit.py refs ====================================
# ============ 引用对账（原 02_refs.py）===============================
# =====================================================================
"""引用对账：扫所有跨文档锚点，查目标是否真的存在。

管的锚点家族（**两族**，见下为什么只剩两族）：
  1) ERR-XX-NN                          → ERRATA.md 的 H3 条目
  2) README「…」                        → README 的标题（子串命中，必须唯一）

★★ 2026-09-22 重构：原先还有三族，**随 SUMMARY.md / ENVIRONMENT.md 一起退役**：
  · `§N / §N.M`      → 目标是 SUMMARY 的编号标题（该文件已删）
  · `A.N / B.N`      → 目标是 ENVIRONMENT.md 的 H2（该文件已删）
  · `ENVIRONMENT「…」` → 目标是 ENVIRONMENT.md 的标题（该文件已删）
  退役时**不是**把正则悄悄删掉，而是：先把代码/配置里 30 处 `§N` 等引用真正清干净
  （见 `ERRATA.md` 的 `ERR-DEPLOY-01` 一族那次记载），再让这三族失去目标集合。
  ★ 现在若有人重新写出 `§N` 或 `` `A.5` ``，**不会**报 DANGLING（没有目标集合可比），
    但会被下面 code-scope 那趟的 **LEGACY 状态**抓出来 —— 见 `_scan_code`。

★★ 为什么必须有 code-scope 那趟（本次重构踩出来的最大盲点）：
  原先只扫 `*.md`（`root.glob("*.md")`），**从不扫 `.py` / `.sh` / `.env.example`**。
  而全项目有 30 处 `§N` / `ERR-*` 引用散在 15 个**代码**文件里 ——
  于是"删文档"时，这个**专门用来抓悬空引用**的工具**一声都不会响**。
  ★ 教训：**投影的扫描面窄于引用面，就等于没有投影**（ERR-META-04：投影必须覆盖它声称覆盖的范围）。
  ⇒ 现在多跑一趟代码扫描，`refs.tsv` 加一列 `scope`（`md` / `code`）。

用法：python audit.py refs <文档目录>
输出：<脚本目录>/out/refs.tsv
"""

# ★ 代码扫描面：这些后缀的文件也参与对账（原先一条都不扫）
CODE_GLOBS = ("*.py", "*.sh", "*.example")

# ★ 已退役家族：出现在代码里就是"引用了一个不再存在的目标" ⇒ 报 LEGACY
REF_SEC = re.compile(r"§\s?(\d+(?:\.\d+){0,3})")
REF_AB = re.compile(r"`([AB]\.\d+)")
REF_ENV = re.compile(r"ENVIRONMENT(?:\.md)?「([^」]+)」")

REF_ERR = re.compile(r"\bERR-([A-Z]+)-(\d{2})\b")
REF_README = re.compile(r"README「([^」]+)」")

HEAD_SEC = re.compile(r"^#{2,5}\s+§?\s*(\d+(?:\.\d+)*)\b")
HEAD_ERR = re.compile(r"^#{2,5}\s+(ERR-[A-Z]+-\d{2})\b")
HEAD_AB = re.compile(r"^##\s+([AB]\.\d+)\b")
HEAD_ANY = re.compile(r"^#{1,6}\s+(.*?)\s*$")


def collect(root):
    secs, errs, abs_, heads = set(), set(), set(), {}
    for f in sorted(root.glob("*.md")):
        title_set = set()
        for _, line in walk(f):
            for rx, bag in ((HEAD_SEC, secs), (HEAD_ERR, errs), (HEAD_AB, abs_)):
                m = rx.match(line)
                if m:
                    bag.add(m.group(1))
            h = HEAD_ANY.match(line)
            if h:
                title_set.add(h.group(1).strip())
        heads[f.name] = title_set
    return secs, errs, abs_, heads


def _iter_code(root):
    """遍历代码/配置文件。★ 跳过 .git 与部署备份（前者是海量噪声，
    后者是历史快照 —— 审历史快照的引用没有意义）。"""
    for glob in CODE_GLOBS:
        for f in sorted(root.rglob(glob)):
            parts = set(f.parts)
            if ".git" in parts or ".deploy-backup" in parts:
                continue
            if f.name in ("audit.py", "02_refs.py"):
                continue          # 本文件的重正则定义**不是引用**（自指噪声）
            yield f


def _scan_code(root, secs, errs, abs_, heads):
    """★ 代码声明式扫描：对账代码里的引用，并把**已退役家族**报成 LEGACY。

    LEGACY 的含义：引用了一个**目标集合已不存在**的东西（`§N` / `A.N` /
    `ENVIRONMENT「…」`）。它天然是"悬空"，只是没有目标可比 —— 所以单独一个状态，
    免得混进 DANGLING 的计数里假装成"查过了没问题"。
    """
    rows = []
    for f in _iter_code(root):
        rel = f.relative_to(root).as_posix()
        for lineno, line in enumerate(
                f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            hint = line.strip()[:80]
            for m in REF_ERR.finditer(line):
                k = "ERR-%s-%s" % (m.group(1), m.group(2))
                rows.append((rel, lineno, k, "OK" if k in errs else "DANGLING",
                             "ERRATA", hint, "code"))
            for m in REF_README.finditer(line):
                st, extra = anchor(m.group(1), heads, "README.md")
                rows.append((rel, lineno, "README「%s」" % m.group(1), st, extra,
                             hint, "code"))
            for m, why in ((REF_SEC, "SUMMARY 已删"),
                           (REF_AB, "ENVIRONMENT.md 已删"),
                           (REF_ENV, "ENVIRONMENT.md 已删")):
                for mm in m.finditer(line):
                    rows.append((rel, lineno, mm.group(0), "LEGACY", why,
                                 hint, "code"))
    return rows


def anchor(title, heads, doc):
    hits = [h for h in heads.get(doc, ()) if title in h]
    if len(hits) == 1:
        return "OK", ""
    if len(hits) == 0:
        return "ANCHOR-MISS", ""
    return "ANCHOR-AMBIG", "%d 条:%s" % (len(hits), " | ".join(sorted(hits)[:2])[:70])


def _main_refs():
    OUT.mkdir(parents=True, exist_ok=True)
    secs, errs, abs_, heads = collect(DOCS)
    if not heads:
        print("[!!] %s 下没有 .md —— 文档目录传对了吗？" % DOCS)
        return
    scan_defects(sorted(DOCS.glob("*.md")))
    print("目标集合：ERR 条目 %d · README 标题 %d · （已退役：编号标题 %d · A/B 条 %d）\n"
          % (len(errs), len(heads.get("README.md", ())), len(secs), len(abs_)))

    rows = []
    for f in sorted(DOCS.glob("*.md")):
        for lineno, line in walk(f):
            hint = line.strip()[:80]
            for m in REF_ERR.finditer(line):
                k = "ERR-%s-%s" % (m.group(1), m.group(2))
                rows.append((f.name, lineno, k,
                             "OK" if k in errs else "DANGLING", "ERRATA", hint, "md"))
            for m in REF_README.finditer(line):
                st, extra = anchor(m.group(1), heads, "README.md")
                rows.append((f.name, lineno, "README「%s」" % m.group(1), st, extra,
                             hint, "md"))

    # ★ code-scope 那趟（见文件头：原先的扫描盲点）
    code_rows = _scan_code(DOCS, secs, errs, abs_, heads)
    rows.extend(code_rows)

    with (OUT / "refs.tsv").open("w", encoding="utf-8") as fp:
        fp.write("src\tline\tref\tstatus\ttarget\thint\tscope\n")
        for r in rows:
            fp.write("\t".join(map(str, r)) + "\n")

    def tally(st):
        return [r for r in rows if r[3] == st]

    n_md = sum(1 for r in rows if r[6] == "md")
    print("[ok] 共 %d 条引用（md %d · code %d） → %s\n"
          % (len(rows), n_md, len(rows) - n_md, OUT / "refs.tsv"))
    for st, label in (("DANGLING", "★ 悬空（目标根本不存在）"),
                      ("WEAK", "△ 半悬空（父节点在、这一层不在）"),
                      ("LEGACY", "★ LEGACY（引用了已删文档的家族：§N / A.N / ENVIRONMENT「…」）"),
                      ("ANCHOR-MISS", "★ 锚点无命中"),
                      ("ANCHOR-AMBIG", "★ 锚点命中多于一")):
        hit = tally(st)
        print("%s：%d 条" % (label, len(hit)))
        for r in hit[:15]:
            print("   [%s] %s:%d  %s" % (r[6], r[0], r[1], r[2]))
            print("        %s" % r[5])
        if len(hit) > 15:
            print("   ...（共 %d，其余见 refs.tsv）" % len(hit))
        print()


# =====================================================================
# ============ 段 3：audit.py terms ===================================
# ============ terms.txt 扫描（原 03_terms.py）========================
# =====================================================================
"""terms.txt 里每个词扫全部 md，输出命中位置 + 跨文件分布。

用法：python audit.py terms <文档目录>
输入：<脚本目录>/terms.txt
输出：<脚本目录>/out/terms.tsv
"""


def _main_terms():
    tf = TOOLS / "terms.txt"
    if not tf.exists():
        print("[!!] 找不到 %s" % tf)
        return
    terms = [t.strip() for t in tf.read_text(encoding="utf-8").splitlines()
             if t.strip() and not t.startswith("#")]
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    files = sorted(DOCS.glob("*.md"))
    for f in files:
        for lineno, line in walk(f):
            for t in terms:
                if t in line:
                    rows.append((t, f.name, lineno, line.strip()[:120]))
    rows.sort()
    with (OUT / "terms.tsv").open("w", encoding="utf-8") as fp:
        fp.write("term\tfile\tline\tcontext\n")
        for r in rows:
            fp.write("\t".join(map(str, r)) + "\n")

    per = Counter(r[0] for r in rows)
    spread = {t: len({r[1] for r in rows if r[0] == t}) for t in per}
    print("[ok] %d 条命中，%d/%d 个关键词有命中，跨 %d 个文件 → %s\n"
          % (len(rows), len(per), len(terms), len(files), OUT / "terms.tsv"))

    print("★ 只出现在 1 个文件里的（= 只维护一处，健康）：")
    for t in sorted(per):
        if spread[t] == 1:
            print("   %4d 次 / 1 文件   %s" % (per[t], t))
    print()
    print("★★ 跨 >=2 个文件的（= 「同一事实多处出现」的嫌疑，逐条过）：")
    multi = sorted(((spread[t], per[t], t) for t in per if spread[t] >= 2), reverse=True)
    for sp, n, t in multi:
        print("   %4d 次 / %d 文件   %s" % (n, sp, t))
    print()
    zero = [t for t in terms if t not in per]
    if zero:
        print("⚠ 一个都没命中的关键词（清单写错了，或事实已消失）：%s" % ", ".join(zero))


# =====================================================================
# ============ 段 4：audit.py probes ==================================
# ============ 只读探针（原 04_probes.py）=============================
# =====================================================================
"""只读探针：把文档声称的事实，从生产上真读一遍。

★★ 安全纪律（**三条同时成立**才算合格，缺一条就是漏洞）：
   ① **只打聚合。** 判「库里有多少条、回填了多少、有几条待办」**只需要计数** ——
      行内容（`torrent_name` / `cover_url` / `tracklist`）对判读零价值，**一律不取**。
      这是第一道，也是最有效的一道。
   ② **不碰 `.env`。** 本文件全程不读任何配置文件里的凭据值；
      探针要用的地址是**写死的公开端点**（qB 的 `/app/version` 不需要认证）。
   ③ **出口统一过 `redact()`。** 万一异常信息里带出什么，在**打印和落盘之前**抹掉 ——
      按**值的形状**兜底，不只按键名（键名匹配会漏掉嵌在异常文本里的凭据）。

   ★ 另外：**探针一律只读**。库用 `mode=ro` + `PRAGMA query_only=1`，
     **直读 UNC、不 `cp` 副本** —— `cp` 会丢掉 WAL 里未 checkpoint 的数据（`ERR-SQL-01`）。

用法：python audit.py probes
输出：<脚本目录>/out/probes.tsv
"""

NAS = "//iSunker-DS423"
# ★ 2026-10-05：NAS 生产目录已改名 `musopia_script2` → `opencd2others`，
#   且状态收进 `data/` 子目录（compose `${STATE_DIR:-./data}:/data`）。
#   ⇒ 这几个 UNC 跟着改（否则探针会读到一个不存在的路径、报"读不到"）。
COMPOSE_UNC = "%s/docker_ssd/opencd2others" % NAS
DB_UNC = COMPOSE_UNC + "/data/data.db"
HB_UNC = COMPOSE_UNC + "/data/daemon_heartbeat"
STATE_UNC = COMPOSE_UNC + "/data/daemon_state.json"

# ★ 本机访问 NAS 上 qB 的地址。**容器内**看到的是 127.0.0.1:3020（host 网络），
#   本机不是容器，得走 NAS 的局域网地址。这个 IP 是**从姊妹项目记的**，
#   本项目没复核过 —— 探针连不上会如实报错，不会假装成功。
QB_URL = "http://192.168.0.7:3020"

# ---- 脱敏（按值的形状兜底）----
REDACT_MORE = [
    (re.compile(r"(?i)(passkey|password|passwd|token|cookie|apikey)(\s*[=:]\s*)([^\s,;&\"']+)"),
     r"\1\2<redacted>"),
    (re.compile(r"\b[0-9a-fA-F]{32,64}\b"), "<hex32+>"),
    (re.compile(r"(?i)Bearer\s+\S+"), "Bearer <redacted>"),
    (re.compile(r"c_secure_pass=[^;\s]+"), "c_secure_pass=<redacted>"),
]


def redact(text):
    s = str(text)
    for rx, rep in REDACT_MORE:
        s = rx.sub(rep, s)
    return s


def _ro(db_unc, timeout=5):
    """只读打开生产库。

    ★ 不能写 `sqlite3.connect("file:%s?mode=ro" % unc, uri=True)` ——
      UNC 的 `//iSunker-DS423` 会被当成 URI 的 authority，
      报 `invalid uri authority: iSunker-DS423`。要么 `file://` + 路径（多两个斜杠），
      要么退回「普通连接 + `PRAGMA query_only=1`」。两条路都真只读；
      URI 那条更硬（连接层就拒绝写 / 不走 WAL 恢复），所以**优先它、失败再退**。
    """
    p = str(db_unc).replace("\\", "/")
    try:
        con = sqlite3.connect("file://" + p + "?mode=ro", uri=True, timeout=timeout)
    except sqlite3.OperationalError:
        con = sqlite3.connect(p, timeout=timeout)
    con.execute("PRAGMA query_only=1")
    return con


def _stale_where():
    """「待办」的 WHERE —— ★ 从**事实源**取，绝不在这里重抄一遍。

    ★★ 为什么必须这样（2026-09-14 实测踩过）：
      本函数原先不存在，db_overview() 里手抄了一句
      `WHERE site_synced_at IS NULL OR updated_at > site_synced_at`。
      它**漏掉了 `site_status='EXISTS'` 和 `site_torrent_id` 那两个条件**，
      于是把 189 条**从没发布过的** `UNKNOWN` 记录也算成了待办 ——
      报出「待办 191 条」，而真值是 **2 条**（189 + 2 = 191，数字正好对上，
      所以**看起来毫无破绽**）。

      `db.py:103-108` 的注释**专门警告过这件事**：「别在两处各写一份……
      一旦漂移，就是『探针说有活 → 60s 后重跑 → 一条都选不出来』的高频空转，
      而且**不会报任何错**」。本文件读了那条注释、还在 `db.py` 里引了它，
      **然后自己犯了同一个错** ⇒ 已登记为 `ERR-META-03`。

    ★ 口径漂移的代价不是报错，是**一个看起来很正常的错数字**。
      所以这里宁可直接依赖 db.py 的私有函数：它**改签名会当场 TypeError**（响亮地坏），
      而重抄一遍只会**安静地给出错的数**。

    ★ 纪律②不但没被破坏，反而更硬了：先把 `config` 塞进 `sys.modules` 占位，
      `db.py` 的 `import config` 拿到的是这个桩 —— **config.py 根本不会执行
      ⇒ 全程不读 `.env`**。db.py 只在 `connect()` 里用 `config.DB_PATH`，
      而本文件**永远不调 connect()**（连的是 NAS 上的 UNC）。
    """
    if "config" not in sys.modules:
        stub = types.ModuleType("config")
        stub.DATA_DIR = ""
        stub.DB_PATH = ""
        sys.modules["config"] = stub
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import db                                  # 必须在塞桩之后
    where, params = db._published_where(only_stale=True)
    return " AND ".join(where), params


# ---- 探针 ----
def qb_version():
    with urllib.request.urlopen(QB_URL + "/api/v2/app/version", timeout=5) as r:
        return r.read().decode().strip()


def db_overview():
    """★ 只出计数，不出任何行内容。"""
    con = _ro(DB_UNC)
    try:
        total = con.execute("SELECT count(*) FROM tasks").fetchone()[0]
        with_id = con.execute(
            "SELECT count(*) FROM tasks WHERE site_torrent_id IS NOT NULL "
            "AND site_torrent_id <> ''").fetchone()[0]
        stale_where, stale_params = _stale_where()
        stale = con.execute(
            "SELECT count(*) FROM tasks WHERE " + stale_where,
            stale_params).fetchone()[0]
        st = con.execute(
            "SELECT site_status, count(*) FROM tasks GROUP BY site_status "
            "ORDER BY count(*) DESC").fetchall()
    finally:
        con.close()
    return ("总 %d 条 · 已回填 site_torrent_id %d 条 · ★ 待办(stale) %d 条\n"
            "                   site_status: %s"
            % (total, with_id, stale,
               " · ".join("%s=%d" % (k if k else "(NULL)", v) for k, v in st)))


def heartbeat_age():
    """daemon_heartbeat 是纯 unix 时间戳，daemon 每轮刷新一次。"""
    raw = Path(HB_UNC.replace("/", "\\")).read_text(encoding="utf-8").strip() \
        if Path(HB_UNC.replace("/", "\\")).exists() else ""
    if not raw:
        return "（读不到 daemon_heartbeat —— 容器现在多半没在跑）"
    ts = int(re.sub(r"[^0-9]", "", raw) or 0)
    age = int(time.time()) - ts
    return "%d 秒前（%.1f 小时前）" % (age, age / 3600.0)


def daemon_stages():
    """各阶段上次跑是什么时候 —— 判「重建会不会触发全阶段」用的。"""
    p = Path(STATE_UNC.replace("/", "\\"))
    if not p.exists():
        return "（读不到 daemon_state.json）"
    data = json.loads(p.read_text(encoding="utf-8"))
    now = time.time()
    out = []
    for k in sorted(data):
        v = data[k]
        lr = v.get("last_run") if isinstance(v, dict) else None
        if isinstance(lr, (int, float)) and lr > 0:
            age = now - lr
            out.append("%s=%.1fh前" % (k, age / 3600.0))
        else:
            out.append("%s=%s" % (k, "从未"))
    return "\n                   ".join(out) if out else "（空）"


def volume_usage():
    """卷水位 —— 只碰已知共享名；不存在的共享会抛错，由上层记下。"""
    out = {}
    for label, unc in (("/volume2 (docker_ssd)", NAS + "/docker_ssd"),
                       ("/volume1 (video)", NAS + "/video")):
        try:
            u = shutil.disk_usage(unc)
            out[label] = "总 %.2f TiB / 已用 %.2f TiB / 可用 %.2f GiB" % (
                u.total / 2**40, u.used / 2**40, u.free / 2**30)
        except Exception as e:                     # noqa: BLE001
            out[label] = "%s: %s" % (type(e).__name__, redact(e))
    return "\n                   ".join("%s: %s" % kv for kv in out.items())


def _timed(fn, secs=20):
    box = {}

    def run():
        try:
            box["v"] = fn()
        except Exception as e:                     # noqa: BLE001
            box["e"] = "%s: %s" % (type(e).__name__, e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(secs)
    if t.is_alive():
        return None, "TIMEOUT after %ds（SMB 可能卡住，别重试太多次）" % secs
    if "e" in box:
        return None, box["e"]
    return box.get("v"), None


PROBES = [
    ("qB :3020 版本", qb_version),
    ("库概览（只计数）", db_overview),
    ("daemon 心跳年龄", heartbeat_age),
    ("daemon 各阶段 last_run", daemon_stages),
    ("卷水位", volume_usage),
]


def _main_probes():
    OUT.mkdir(parents=True, exist_ok=True)
    lines = ["probe\tactual\terror"]
    for name, fn in PROBES:
        v, err = _timed(fn)
        if err:
            print("[!!] %s: %s" % (name, redact(err)))
            lines.append("%s\t\t%s" % (name, redact(err)))
        else:
            print("[ok] %s: %s" % (name, redact(v)))
            lines.append("%s\t%s\t" % (name, redact(v).replace("\n", " ")))
    (OUT / "probes.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n→ %s" % (OUT / "probes.tsv"))


# =====================================================================
# 分发器
# =====================================================================

_PARTS = {
    "headings": _main_headings,
    "refs": _main_refs,
    "terms": _main_terms,
    "probes": _main_probes,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("用法: python tools/doc-audit/audit.py <子命令> [文档目录]\n")
        print("子命令：")
        print("  headings  标题树 → out/headings.tsv（原 01_headings.py）")
        print("  refs      引用对账 → out/refs.tsv（原 02_refs.py）")
        print("  terms     terms.txt 扫描 → out/terms.tsv（原 03_terms.py）")
        print("  probes    只读探针 → out/probes.tsv（原 04_probes.py）")
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    part = argv[0]
    fn = _PARTS.get(part)
    if fn is None:
        print(f"❌ 未知子命令：{part!r}", file=sys.stderr)
        print(f"   可用: {', '.join(_PARTS)}", file=sys.stderr)
        return 2
    # ★ 重算 DOCS（复刻原先 `sys.argv[1]` 的语义，见 `_docs_from_argv`）
    global DOCS
    DOCS = _docs_from_argv(argv[1:])
    return fn()


if __name__ == "__main__":
    sys.exit(main())
