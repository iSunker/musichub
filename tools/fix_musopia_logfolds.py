# -*- coding: utf-8 -*-
"""清理乌托邦详情页简介里的空日志折叠壳。

背景：乌托邦 2026-11 改版后，[spoiler=CDn LOG][code]…[/code][/spoiler] 里的
`[/code]` 正文被站点搬到「抓轨 Log（共 N 碟）」原生区，简介里只剩
`[spoiler=CDn LOG][/spoiler]` 这样点开为空的折叠壳。本工具只删除**空壳**，
正文/其它字段一概不动（takeedit 是整表覆盖，故复用 OnsiteEditor 的基线回填）。

用法（在 musichub 容器内执行；默认 dry-run）：

    python3 -m musichub.tools.fix_musopia_logfolds 26384 21482
    python3 -m musichub.tools.fix_musopia_logfolds --all-affected --apply
"""

import argparse
import json
import logging
import re
import sys
import time
from typing import Dict, List

from musichub.onsite.editor import OnsiteEditor, OnsiteEditError
from musichub.adapters.sites import MusopiaAdapter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("fix_musopia_logfolds")

# 只匹配「没有正文」的折叠壳：允许标签间有空白/<br>
EMPTY_FOLD_RE = re.compile(
    r"[ \t]*(?:<br\s*/?>\s*)*\[spoiler=[^\]]*\]\s*(?:<br\s*/?>\s*)*\[/spoiler\][ \t]*\n?",
    re.I,
)


def strip_empty_folds(descr: str) -> tuple:
    """返回 (新简介, 删除的空壳数)"""
    descr = descr or ""
    n = len(EMPTY_FOLD_RE.findall(descr))
    if not n:
        return descr, 0
    new = EMPTY_FOLD_RE.sub("", descr)
    new = re.sub(r"\n{3,}", "\n\n", new).strip()
    return new, n


def clean_one(editor: OnsiteEditor, tid: str, dry_run: bool) -> Dict:
    rep = {"tid": str(tid), "ok": False}
    try:
        fields, _ = editor.fetch_edit(tid)
    except OnsiteEditError as e:
        rep["error"] = str(e)
        return rep
    descr = fields.get("descr") or ""
    new_descr, removed = strip_empty_folds(descr)
    rep["empty_folds"] = removed
    if removed == 0:
        rep.update(ok=True, message="无空折叠壳，跳过")
        return rep
    if dry_run:
        rep.update(ok=True, action="would_clean",
                   old_len=len(descr), new_len=len(new_descr))
        return rep
    res = editor.edit(tid, {"descr": new_descr}, dry_run=False, verify=True)
    rep.update(ok=bool(res.get("ok")),
               action="cleaned",
               error=res.get("error") or "",
               verify=res.get("verify") or {})
    return rep


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="清理乌托邦简介里的空日志折叠壳")
    ap.add_argument("tids", nargs="*", help="种子 ID")
    ap.add_argument("--tids-file", help="每行一个 tid 的文件")
    ap.add_argument("--apply", action="store_true", help="真正写站点（默认 dry-run）")
    ap.add_argument("--sleep", type=float, default=0.8, help="每条之间的间隔秒数")
    args = ap.parse_args(argv)

    tids = list(args.tids)
    if args.tids_file:
        with open(args.tids_file, encoding="utf-8") as f:
            tids += [x.strip() for x in f if x.strip() and not x.startswith("#")]
    tids = list(dict.fromkeys(tids))
    if not tids:
        ap.error("没有提供 tid")

    editor = OnsiteEditor(MusopiaAdapter())
    reports = []
    for i, tid in enumerate(tids, 1):
        rep = clean_one(editor, tid, dry_run=not args.apply)
        reports.append(rep)
        logger.info("[%d/%d] %s", i, len(tids), json.dumps(rep, ensure_ascii=False))
        time.sleep(args.sleep)

    cleaned = [r for r in reports if r.get("action") == "cleaned" and r.get("ok")]
    failed = [r for r in reports if not r.get("ok")]
    skipped = [r for r in reports if r.get("empty_folds") == 0]
    print("\n== SUMMARY ==")
    print("total=%d cleaned=%d skipped=%d failed=%d dry_run=%s"
          % (len(reports), len(cleaned), len(skipped), len(failed), not args.apply))
    if failed:
        print("failed:", json.dumps(failed, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
