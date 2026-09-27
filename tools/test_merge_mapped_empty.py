"""★ 对照测试 `db.merge_mapped()` 的「空快照权威」分支（ERR-DATA-05）。

用法（本机，不需要 .env 真值）：
    QB_HOST=x QB_USER=x QB_PASS=x MUSOPIA_COOKIE=x MUSOPIA_PASSKEY=x \n      PICGO_API_KEY=x python tools/test_merge_mapped_empty.py

★ 7 组 14 项，含 2 处阴性对照（空↔非空必须不同、缺席↔为空必须可分辨）。
"""

import io, json, os, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, "/app")

def mk(mapped, opencd=True, col_small="全 15 曲", col_descr="老简介"):
    t = {"info_hash": "x"*40, "small_descr": col_small, "opencd_descr": col_descr,
         "tags": None, "medium_sel": None, "audiocodec_sel": None, "team_sel": None,
         "tracklist": "col-tracklist"}
    if opencd: t["opencd_tid"] = "199947"
    if mapped is None: t["mapped_json"] = None
    else: t["mapped_json"] = json.dumps(mapped, ensure_ascii=False)
    return t

import db
MM = db.merge_mapped
KEYS = ("small_descr", "descr")

cases = []
def chk(name, got, want):
    ok = got == want
    cases.append((name, ok, got, want))
    print(("  ✅ " if ok else "  ❌ ") + name)
    if not ok: print("       got =%r\n       want=%r" % (got, want))

print("① 皇后行 + 快照**有**副标题 ⇒ 盖过列（原有行为，必须不变）")
r = MM(mk({"small_descr": "專輯 | VP Records / VPCD 1793", "descr": "皇后正文"}))
chk("small_descr 取快照", r["small_descr"], "專輯 | VP Records / VPCD 1793")
chk("descr 取快照", r["descr"], "皇后正文")

print()
print("② ★ 皇后行 + 快照**为空** ⇒ 现在必须盖成空（本次修的行为）")
r = MM(mk({"small_descr": "", "descr": ""}))
chk("small_descr 盖成 ''（不是保留「全 15 曲」）", r["small_descr"], "")
chk("descr 盖成 ''（不是保留快照外的老简介）", r["descr"], "")

print()
print("③ 皇后行 + 快照里**没有**这个键 ⇒ 不动（键缺席 ≠ 空，不许当成空）")
r = MM(mk({"title": "x"}))
chk("small_descr 保持列值", r["small_descr"], "全 15 曲")
chk("descr 键缺席 ⇒ 不带进来（列名是 opencd_descr）", "descr" in r, False)
chk("       列 opencd_descr 不动", r["opencd_descr"], "老简介")

print()
print("④ 非皇后行（本地抓轨路，无 opencd_tid）⇒ 一律不动（行为与改前一致）")
r = MM(mk({"small_descr": "", "descr": ""}, opencd=False))
chk("small_descr 保持列值", r["small_descr"], "全 15 曲")
chk("descr 键缺席 ⇒ 不带进来（列名是 opencd_descr）", "descr" in r, False)
chk("       列 opencd_descr 不动", r["opencd_descr"], "老简介")
r = MM(mk(None, opencd=False))
chk("无 mapped_json ⇒ 不动", r["small_descr"], "全 15 曲")

print()
print("⑤ ★★ 不适用空权威的键：快照为空时仍**保留列值**（保守行为，没被误伤）")
r = MM(mk({"small_descr": "", "descr": "", "tags": [], "tracklist": ""}))
chk("tracklist 快照空 ⇒ 保留列值", r["tracklist"], "col-tracklist")

print()
print("⑥ ★★ 阴性对照：把「空」换成「非空」⇒ 结果必须不同")
r_empty = MM(mk({"small_descr": "", "descr": ""}))
r_nonempty = MM(mk({"small_descr": "X", "descr": "Y"}))
chk("空 vs 非空 结果不同", r_empty["small_descr"] != r_nonempty["small_descr"], True)
if r_empty["small_descr"] == r_nonempty["small_descr"]:
    print("  ❌❌ 阴性对照失败：改动没有区分空与非空 ⇒ 判据无效")

print()
print("⑦ 键缺席 vs 键为空 必须**可分辨**（这是本次改动的关键区分）")
r_absent = MM(mk({"title": "x"}))
r_emptyv = MM(mk({"small_descr": "", "descr": ""}))
chk("缺席⇒列 / 为空⇒'' 不同", r_absent["small_descr"] != r_emptyv["small_descr"], True)

bad = [c for c in cases if not c[1]]
print()
print("=" * 60)
print("结果: %d/%d 通过" % (len(cases) - len(bad), len(cases)))
sys.exit(1 if bad else 0)
