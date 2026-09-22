#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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

用法：python 02_refs.py <文档目录>
输出：<脚本目录>/out/refs.tsv
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent.parent
DOCS = Path(sys.argv[1] if len(sys.argv) > 1 else REPO).resolve()
OUT = TOOLS / "out"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mdwalk import iter_lines as walk, scan_defects  # noqa: E402

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
            if f.name == "02_refs.py":
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


def main():
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


if __name__ == "__main__":
    main()
