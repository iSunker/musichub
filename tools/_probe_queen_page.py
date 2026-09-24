"""只读诊断：抓一个皇后详情页，把 `opencd_parse` 解析出的字段**原样打出来**。

═══════════════════════════════════════════════════════════════════════════
要回答的问题（2026-09-23）
═══════════════════════════════════════════════════════════════════════════
现象：试发的 2 条站上**只有 `OpenCD` 一个标签**，没有皇后的「港台/流行」。
根因已定位为 `mapped_json` 全空（`descr.py` 的标签订阅不了），
而 `mapped_json` 要由 `opencd_map.map_all()` 从**皇后页面解析结果**算出来。

⇒ 本脚本回答：**这个页面到底解析出了什么？**
   特别是用户点名的那几样：**副标题 / 標籤列表 / 媒介 / 製作組**。

用户 2026-09-23 指定用它试：`id=200982`（冯刚毅 苏春梅 - 宝玉与晴雯）。
★ 用户说前两条「皇后好像没有副标题」⇒ 换这条**有副标题**的来验。

═══════════════════════════════════════════════════════════════════════════
★★ 只读保证
═══════════════════════════════════════════════════════════════════════════
- 只发 **1 个 GET**（`/plugin_details.php?id=N&hit=1`），且**过 RateGate**
  （同 `crawl.py` 的唯一出口纪律）。
- **不写库**：不调 db.save_task / ingest / 任何写口。
- 落盘与否：默认**不落盘**（`--save` 才落，且落在 `/data/opencdhtml/raw/`，
  与 `crawl.py` 的 `save_raw` 同目录、同命名，方便后续 crawl 复用它省请求）。

在 NAS 上运行（**一行**）:
    cd /volume2/docker_ssd/musopia_script2
    sudo docker compose --profile tools run --rm tools python /data/_probe_queen_page.py

换 id: 加 -e PROBE_TID=200983
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.environ.get("MUSOPIA_APP_DIR", "/app"))

import crawl
import opencd_rate          # noqa: F401  （crawl 通过它暴露；显式 import 便于无 crawl 时诊断）
import opencd_map
import opencd_parse

TID = os.getenv("PROBE_TID", "200982")
SAVE = os.environ.get("PROBE_SAVE", "") == "1"


def main():
    print("=" * 74)
    print(f"★ 只读诊断：皇后详情页 id={TID} 解析出什么")
    print("=" * 74)
    print(f"  OPENCD_URL = {os.getenv('OPENCD_URL', 'https://open.cd')}")
    print(f"  有 Cookie  = {'是' if os.getenv('OPENCD_COOKIE') else '★ 否（会被踢到登录页）'}")
    print("  ★ 只发 1 个 GET，过 RateGate；不写库")
    print()

    # ★ 照 crawl.py:722-739 的**原样**构造 —— 不自己编参数。
    #   ★★ 关键：必须传 ledger=_DbLedger()，否则「每日上限」跨进程失效
    #      （crawl.py:224-235 专门为此删掉了兜底，并抛 ValueError）。
    gate = crawl._make_gate() if hasattr(crawl, "_make_gate") else None
    if gate is None:
        day_key = time.strftime("%Y-%m-%d")
        gate = opencd_rate.RateGate(
            max_per_round=int(os.getenv("OPENCD_MAX_PER_ROUND", "20")),
            min_interval=float(os.getenv("OPENCD_MIN_INTERVAL", "120")),
            max_per_day=int(os.getenv("OPENCD_MAX_PER_DAY", "50")),
            day_key=day_key,
            log=lambda m: None,
            ledger=crawl._DbLedger(),
        )
        gate.roll_day(day_key)
    client = crawl.Client(gate=gate)

    r = client.get("/plugin_details.php", **{"id": TID, "hit": 1})
    html, code = r.text, r.status_code

    print(f"  HTTP={code}  body={len(html)} 字节")
    if "takelogin" in html or "登錄" in html[:4000]:
        print("  ❌ 被踢到登录页 ⇒ Cookie 不对/失效。本脚本到此为止。")
        return 1
    print()

    if SAVE:
        crawl.save_raw(TID, html)
        print(f"  💾 已落盘（crawl 下次可直接复用，省一个请求）")

    # ---- 解析 ----
    info = opencd_parse.parse_detail(html, source_file=f"{TID}.html")
    print("=" * 74)
    print("★ opencd_parse.parse_detail() 的结果（逐字段）")
    print("=" * 74)
    for k in sorted(info.keys()):
        v = info[k]
        s = str(v)
        if len(s) > 300:
            s = s[:300] + f" …(+{len(str(v)) - 300} 字符)"
        print(f"  {k:>16} : {s}")

    print()
    print("=" * 74)
    print("★ 决定站上字段的几项（用户点名：副标题 / 標籤 / 媒介 / 製作組）")
    print("=" * 74)
    # ★ 键名以 `opencd_parse.FIELD_LABELS` + `parse_detail` 实际产出为准（不是猜的）：
    #   `small_descr`=副标题（另有 `small_descr_present` 区分「真没有 vs 没取到」），
    #   `medium`=媒介，`team`=製作組，`tags`=標籤列表，`source`=來自（原抓/轉載）
    for k in ("name", "small_descr", "small_descr_present",
              "tags", "medium", "team", "artist", "year",
              "type", "category", "format", "source",
              "transfer_rule", "transfer_rule_raw"):
        if k in info:
            print(f"  {k:>22} = {info[k]!r}")
    print()
    print("  ★ `missing` 列表（**取不到**的字段 —— 不静默）:", info.get("missing") or "（无）")
    print()

    # ---- 映射 ----
    print("=" * 74)
    print("★ opencd_map.map_all() —— 这就是 `mapped_json` 的来源")
    print("=" * 74)
    m = opencd_map.map_all(info)
    for k in sorted(m.keys()):
        print(f"  {k:>16} = {m[k]!r}")
    print()
    print("  ★ unmapped（映射不上、**必须显式列出、不许静默丢**）:")
    for u in (m.get("unmapped") or []):
        print(f"      {u!r}")
    print()
    print("  ⇒ 若这里的 `tags` 非空（如 ['6','10']），那站上就该有对应标签。")
    print("  ⇒ 若 `mapped_json` 曾被写进库，`descr.py:621` 的 `if declared:` 就会成立，")
    print("     从而**不再走** `detect_tags()` 那条会返回 [] 的兜底。")
    print()
    print("★ 本脚本没有修改任何东西（站点只读、库只读）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
