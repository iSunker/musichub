"""只读诊断：qB 里 `Musopia` 分类的种子**到底处于什么 state**。

═══════════════════════════════════════════════════════════════════════════
它补的是 `qb_probe.py` 的缺口（**别把两者混为一谈**）
═══════════════════════════════════════════════════════════════════════════
`qb_probe.py` 取的是 `content_path` / `name` / `save_path` —— 它能回答
"种子在不在 qB 里、路径对不对"，但**不取 `state`**。而 2026-09-26 要回答的
问题恰恰是 state：

    「两个种子都发布了，但是**没有做种**」—— 为什么？

⇒ 没有 `state` 就**定不了论**，只能猜。本脚本专门去取它。

═══════════════════════════════════════════════════════════════════════════
为什么 `state` 是这条问题的**唯一**判据
═══════════════════════════════════════════════════════════════════════════
`publish.py::readd_to_qb()` 的 docstring 记着一次真实事故（2026-09-24 修）：
曾经给 `torrents_add()` 传了 `is_root_folder=False`，库会把它改写成
`contentLayout=NoSubfolder` ⇒ qB 去 `/downloads/01.flac` 找，而文件实际在
`/downloads/<专辑名>/01.flac` ⇒ **错位** ⇒ `state=missingFiles`、
`content_path` 退化成光秃秃的 `/downloads`。

★ 所以 `state` 的几种取值指向**完全不同的修法**，一个都不能猜：

    uploading / stalledUP / forcedUP  ⇒ ★ **在做种**（只是没下载者）⇒ 正常
    missingFiles                      ⇒ ★★ 文件没对上（上面那个坑，或文件真没了）
    pausedUP / stoppedUP              ⇒ 加了但没跑 ⇒ 恢复即可
    checkingUP / checkpointing        ⇒ qB 正在校验 ⇒ 等一会儿再查
    error                             ⇒ qB 报错 ⇒ 看 message
    （查不到）                          ⇒ ★★ 根本没加进 qB（回加没跑到）

═══════════════════════════════════════════════════════════════════════════
只读保证
═══════════════════════════════════════════════════════════════════════════
只调 `qb.torrents_info(...)`（**查询**）—— 不 add / 不 delete / 不 recheck /
不 pause。**不碰本地库**（不 import db、不连 data.db）。

用法（在 NAS 上，一行）:
    sudo docker compose run --rm tools python tools/qb_state_probe.py

只看发布队列里那几条（按库里的 info_hash 过滤）:
    sudo docker compose run --rm tools python tools/qb_state_probe.py --hashes-only

看全部（不限分类）:
    sudo docker compose run --rm tools python tools/qb_state_probe.py --all
"""
import argparse
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.environ.get("MUSOPIA_APP_DIR", "/app"))

import config  # noqa: E402

# ★ 与 `publish.py::readd_to_qb()` 的复核用的是**同一组判据**（那边是
#   `ok = t.state != "missingFiles"`）。这里把每个取值展开成人话，
#   因为"到底是什么"决定下一步做什么 —— 只印一个裸 state 等于没印。
#   ★ 本脚本**刻意不 import publish**：publish 一进来就会连站点、还会在
#     import 期做其他副作用（本脚本要能独立在只读场景跑）。
STATE_MEANING = {
    "uploading": ("✅", "在做种（有下载者）"),
    "stalledUP": ("✅", "在做种（暂无下载者 —— **正常**，不是故障）"),
    "forcedUP": ("✅", "在做种（已强制开始）"),
    "queuedUP": ("⏳", "排队等做种（qB 队列满）"),
    "checkingUP": ("⏳", "正在校验（qB 异步进行中，稍后再查）"),
    "checkingDL": ("⏳", "正在校验"),
    "checkingResumeData": ("⏳", "正在校验恢复数据"),
    "metaDL": ("⏳", "正在取元数据"),
    "allocating": ("⏳", "正在分配空间"),
    "pausedUP": ("⚠️", "★ 已完成但**被暂停** ⇒ 没在做种，恢复即可"),
    "pausedDL": ("⚠️", "被暂停（未完成）"),
    "stoppedUP": ("⚠️", "★ 已停止 ⇒ 没在做种"),
    "stoppedDL": ("⚠️", "已停止（未完成）"),
    "missingFiles": ("★★", "★★ **文件没对上** ⇒ 这条种子不会做种（见模块 docstring）"),
    "error": ("★★", "★★ qB 报错 ⇒ 看下方 message"),
    "unknown": ("❔", "未知状态"),
}


def main(argv=None):
    ap = argparse.ArgumentParser(description="qB 种子的 state 探针（只读）")
    ap.add_argument("--all", action="store_true",
                    help="不限分类，列出 qB 里的全部种子")
    ap.add_argument("--hashes-only", action="store_true",
                    help="只列库里有记录的种子（要读本地库；默认读全分类）")
    ap.add_argument("--category", default=None,
                    help=f"分类名（默认 config.PUBLISH_CATEGORY = "
                         f"{getattr(config, 'PUBLISH_CATEGORY', 'Musopia')!r}）")
    ap.add_argument("--limit", type=int, default=0,
                    help="最多打印几条（0 = 不限；默认 0，因为'没做种'可能是少数）")
    args = ap.parse_args(argv)

    try:
        import qb_client
    except ImportError as e:
        # ★ 缺库 ⇒ `qb_client.build()` 首行抛的也是 ImportError（同"没装 qbittorrentapi"）。
        print(f"✗ 没装 qbittorrentapi（{e}）", file=sys.stderr)
        return 1

    cat = args.category or getattr(config, "PUBLISH_CATEGORY", "Musopia")

    print("=" * 74)
    print("★ 只读诊断：qB 种子 state（不 add / 不 delete / 不 recheck / 不碰本地库）")
    print("=" * 74)
    print(f"  QB_HOST  = {config.QB_HOST}")
    print(f"  分类     = {'(全部)' if args.all else cat}")
    print("  ★ 判据：state 决定修法 —— 见下方逐条解释")
    print()

    try:
        # ★★ 唯一构造点 `qb_client.build`（`ERR-QB-08`）：优先 API Key，回退账号密码。
        qb = qb_client.build(config)
    except Exception as e:                                   # noqa: BLE001
        print(f"✗ 连不上 qB（{config.QB_HOST}）：{type(e).__name__}: {e}",
              file=sys.stderr)
        print("  ★ 注意 QB_HOST 是 127.0.0.1 ⇒ 只有**在容器内 / NAS 本机**才连得上。",
              file=sys.stderr)
        return 2

    kwargs = {} if args.all else {"category": cat}
    try:
        torrents = list(qb.torrents_info(**kwargs))
    except Exception as e:                                   # noqa: BLE001
        print(f"✗ 查询失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    print(f"  ✅ 读到 {len(torrents)} 个种子"
          f"（{'全部' if args.all else f'分类 {cat}'}）")

    # ★ 按 state 计数 —— **先给分布**，因为"有几条没做种"要的是这个，
    #   而不是"某一条的 state 是什么"。两个都要，但分布先给。
    dist = {}
    for t in torrents:
        dist[t.state or "(空)"] = dist.get(t.state or "(空)", 0) + 1
    print()
    print("【状态分布】★ 这是回答『有没有没做种』的总账")
    for st, n in sorted(dist.items(), key=lambda kv: -kv[1]):
        mark, why = STATE_MEANING.get(st, ("❔", "（未收录的 state，请照原样记下）"))
        print(f"  {mark} {st:14} × {n:5}   {why}")

    # ★ 只把**有问题**的那些列出来明细 —— 正常的（在做种）看一眼分布就够，
    #   全列出来会把真正的异常淹掉（而"没做种"恰恰只需要看异常那几条）。
    bad = [t for t in torrents
           if (t.state or "") not in ("uploading", "stalledUP", "forcedUP")]
    print()
    print(f"【需要看的明细】非「在做种」的 {len(bad)} 条"
          f"（在做种的没列 —— 它们无异常）")
    if not bad:
        print("  （无）⇒ ★ 全部都在做种：那『没做种』的症状**不在这里**，"
              "请重新确认观察口径")
    shown = bad[:args.limit] if args.limit else bad
    for t in shown:
        mark, why = STATE_MEANING.get(t.state or "", ("❔", "?"))
        print(f"\n  {mark} state={t.state}  progress={t.progress * 100:.1f}%")
        print(f"     name         = {(t.name or '')[:66]}")
        print(f"     hash         = {(t.hash or '')[:16]}…")
        print(f"     save_path    = {t.save_path!r}")
        print(f"     content_path = {t.content_path!r}")
        if getattr(t, "error", 0):
            print(f"     ⚠️ error      = {t.error}")
        if getattr(t, "message", ""):
            print(f"     message      = {t.message!r}")
        print(f"     ⇒ {why}")
    if args.limit and len(bad) > args.limit:
        print(f"\n  … 另有 {len(bad) - args.limit} 条（--limit 限制）")

    print()
    print("=" * 74)
    print("★ 本脚本没有修改任何东西（qB 只查询；本地库完全没碰）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
