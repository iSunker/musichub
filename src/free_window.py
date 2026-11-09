"""收费种子免费窗口状态检测与恢复/暂停管理。

监测指定标签（TAG_PAID）的种子优惠状态：
  - 进入免费窗口：恢复做种（torrents_resume）；
  - 窗口结束或失效：重新暂停（torrents_pause），避免产生非预期下载流量。

判定规则：
  - 列表页行解析：基于站点列表 HTML 优惠图标提取优惠类型与截止时间；
  - 首页全局公告解析：多区间取最新有效截止时间，无具体区间的弱信号不执行状态切换；
  - 无法确认优惠状态时保持现状，默认 dry-run 模式只读不写。
"""
from __future__ import annotations

import argparse
import html as html_module
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

# ★ 函数内 import 纪律：qb_client / crawl 都在**用到处**再 import（避免顶层拉重依赖）。

TAG_PAID = "收费种子（暂停下载）"          # ★ 实测的原始标签串（注意全角括号）
STATE_PATH = os.path.join(os.getenv("DATA_DIR", "/data"), "free_window_state.json")

# ---- 站点端判据：优惠 img 的 class → promo 语义 ----
#   来源：实测 `torrents.php` 原文（见 §2）。★ class 是稳定机器判据，别用中文文案。
PROMO_BY_CLASS = {
    "pro_free2up":        "free2up",     # 免费 + 2x 上传
    "pro_free":           "free",        # 免费
    "pro_50pctdown2up":   "50pct2up",    # 50% 下载 + 2x 上传（★ 不免费）
    "pro_50pctdown":      "50pct",       # 仅 50% 下载（★ 不免费）
    "pro_30pctdown":      "30pct",       # 仅 30% 下载（★ 不免费；实测真页有 27 处）
    "pro_2up":            "2up",         # 仅 2x 上传（★ 不免费）
}
PROMO_FREE = {"free", "free2up"}         # 只有这两个算「免费」

# 到期时刻在 title 属性里（实测；比"剩餘時間"的相对值稳）。
# ★★ 实测坑（本机单测抓到）：原始 HTML 里是 **HTML 转义**的 `title=&quot;2026-10-07 17:55:03&quot;`
#    —— 若只匹配 `title="..."`（字面引号）会**永远匹配不到**（静默 expire_at=''）。
#    ⇒ 先 `unescape` 再匹配；正则同时容忍 `"` 与 `&quot;`（双保险）。
# ★★ 实测坑（真样本单测抓到，2026-10-06）：onmouseover 里还嵌着 `<font class="twoupfree">` 之类，
#    unescape 之后该内层 class 就是**真 class 之后的第二个 class**。
#    若用 `<img[^>]*class="([^"]*)"`（`[^>]*` 贪婪），引擎会走到**本标签内最后一个 class=** 再回退 ——
#    ⇒ 只要内层 class 排在真 class 之前（或落在同一"首个 >"之前），就会**认错 class**（认成内层）。
#    ⇒ 修法：**只取 `<img` 之后、本标签自身属性区段（到首个 `>` 为止）里的第一个 `class=`**；
#            永不越过 `>`，也就永不误取 onmouseover 内层 class。
_IMG_TAG_RE = re.compile(r'<img\b[\s\S]*?/>', re.I)       # 整个 img 标签（到 `/>` 为止；★ 可含 onmouseover 内的 `>`）
_CLASS_ATTR_RE = re.compile(r'<img\b[^>]*?\bclass\s*=\s*"([^"]*)"', re.I)   # 只在本标签**属性区**（首个 `>` 前）取首个 class
_TITLE_ATTR_RE = re.compile(r'title=(?:"|&quot;)(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:"|&quot;)')
# 全站横幅（三种形态，均实测）：
#   A: 北京时间<起> 至 <止> 开启全站Free。          （日志式，带区间）
#   B: 全站 [Free] 生效中！时间：<起> ~ <止>        （横幅式，带区间）
#   C: <b>全站Free活动</b> / 全站Free              （★ 裸标题，**无区间** —— live 实测就是这种）
#   ⇒ 有区间用区间；裸标题 ⇒ 视为"当前生效"，但 expire_at 留空（保守：不会自动到期 pause）。
_GLOBAL_A_RE = re.compile(r"北京时间\s*(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s*至\s*(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
_GLOBAL_B_RE = re.compile(r"全站\s*\[?Free\]?\s*生效中.{0,60}?时间：\s*(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s*~\s*(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", re.S)
_GLOBAL_C_RE = re.compile(r"全站\s*(?:\[?Free\]?|免費|免费)\s*(?:活动|活動)?", re.I)


@dataclass
class FreeWindow:
    """一颗种子的免费判定结果。"""
    promo: str = "unknown"               # free2up / free / 50pct2up / 2up / 50pct / none / unknown
    expire_at: str = ""                  # 'YYYY-MM-DD HH:MM:SS' 或 ''
    source: str = "unknown"              # site-row / site-global / local-table / unknown

    @property
    def is_free(self) -> bool:
        return self.promo in PROMO_FREE

    def expired(self, now: datetime | None = None) -> bool:
        """免费是否已到期（无到期时刻 ⇒ 视为未到期，保守）。"""
        if not self.expire_at:
            return False
        now = now or datetime.now()
        try:
            return now >= datetime.strptime(self.expire_at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False                          # 解析不出版本 ⇒ 保守：当作未到期


# =========================================================================== #
# 站点端解析（★ 纯函数，不碰网络 —— 便于单测/重放落盘 HTML）
# =========================================================================== #
def parse_row_promo(row_html: str) -> FreeWindow:
    """从一个种子行（`table.torrentname` 对应 HTML 片段）解出优惠。"""
    # ★ 先 unescape（实测 title 在原文里是 &quot; 转义）；再逐 img 判 class。
    raw = html_module.unescape(row_html or "")
    seen_promo_class = ""                        # 见到过 pro_* 但不在白名单 ⇒ 认不出（★ 不能当收费）
    for tag in _IMG_TAG_RE.finditer(raw):
        # ★ 只在本 img 标签**属性区**里取 class（不越过 `>`）—— 见 _CLASS_ATTR_RE 注释：
        #   否则会误取 onmouseover 内层 `<font class="twoupfree">` ⇒ 认错 class。
        cm = _CLASS_ATTR_RE.search(tag.group(0))
        if not cm:
            continue
        classes = (cm.group(1) or "").lower()
        for cls, promo in PROMO_BY_CLASS.items():
            if cls in classes:
                # 到期时刻：该 img 标签内（unescape 后已是正常引号）
                tm = _TITLE_ATTR_RE.search(tag.group(0))
                return FreeWindow(promo=promo,
                                  expire_at=tm.group(1) if tm else "",
                                  source="site-row")
        # 是 pro_* 但白名单里没有（站点新增/改名的优惠类型）⇒ 记下来，别当"无优惠"
        if re.search(r'\bpro_[a-z0-9]+', classes):
            seen_promo_class = classes
    if seen_promo_class:
        # ★ 认不出就报 unknown（既不 resume 也不 pause）—— 绝不猜成"收费"而误 pause
        return FreeWindow(promo="unknown", source="site-row")
    return FreeWindow(promo="none", source="site-row")   # 行在但无优惠 ⇒ 收费


def parse_global_free(index_html: str) -> FreeWindow:
    """从首页解「全站 Free 窗口」。无 ⇒ promo='none'。

    # 解析规则：
    # 1. 提取所有带时间区间的公告，按截止时间取最新者判断生效状态；
    # 2. 无明确时间区间的文本仅作为弱信号（free_suspect），不触发恢复或暂停。
    """
    html = index_html or ""

    # ① 收集所有带区间的窗口（A/B），取 expire 最新者
    ranges: list[tuple[str, str]] = []            # [(起, 止), ...]
    for m in _GLOBAL_A_RE.finditer(html):
        ranges.append((m.group(1), m.group(2)))
    for m in _GLOBAL_B_RE.finditer(html):
        ranges.append((m.group(1), m.group(2)))
    if ranges:
        # 按"止"排序取最新（字符串排序对 'YYYY-MM-DD HH:MM:SS' 即时间序）
        _start, end = max(ranges, key=lambda r: r[1])
        return FreeWindow(promo="free", expire_at=end, source="site-global")

    # ② 无区间，但有裸"全站Free活动" ⇒ **弱信号**（不判 free；严格保守）
    if _GLOBAL_C_RE.search(html):
        return FreeWindow(promo="free_suspect", expire_at="", source="site-global")

    return FreeWindow(promo="none", source="site-global")


# =========================================================================== #
# 检测：对一批 hash 求 free window（合并 全站 ∪ 单种）
# =========================================================================== #
def detect_free_windows(hashes, *, site_client, now=None) -> dict:
    """返回 {hash: FreeWindow}。

    ★ 接口契约（写给未来的调用方，见 `免费窗口检测机制` §7）：
      · 抓不到 ⇒ promo='unknown'（**绝不**默认成 free 或 charging）。
      · 全站 Free 生效 ⇒ 所有 hash 直接 free（并集），不再逐条抓（省配额）。
    """
    now = now or datetime.now()
    result = {h: FreeWindow() for h in hashes}

    # ① 先抓首页（**1 个请求**）判全站窗口
    idx = site_client.get("/index.php")
    if getattr(idx, "status_code", 0) != 200 or "takelogin.php" in (idx.text or ""):
        return result                              # cookie 失效 ⇒ 全 unknown（不误判）
    g = parse_global_free(idx.text or "")
    if g.is_free and not g.expired(now):
        for h in hashes:
            result[h] = FreeWindow(promo="free", expire_at=g.expire_at, source="site-global")
        return result

    # ② 全站非免费 ⇒ 逐条抓（★ 这里最贵；调用方用 --limit 控制小样）
    #    ★ 只用**列表页按 hash 定位**，不抓详情页（省配额）。
    for h in hashes:
        r = site_client.get("/torrents.php", search=h)
        if getattr(r, "status_code", 0) != 200:
            continue                               # 留 unknown
        sub = _isolate_row_for_hash(r.text or "", h)
        if sub is None:
            result[h] = FreeWindow(promo="unknown", source="site-row")
            continue
        result[h] = parse_row_promo(sub)
    return result


def _isolate_row_for_hash(html: str, infohash: str) -> str | None:
    """在列表页里找到含该 infohash 的 `table.torrentname` 行 HTML。★ 纯函数。"""
    # ★ 实测列表页行里 infohash 出现在下载链接；用行级切分而非全页正则。
    for seg in re.split(r'(?i)<table[^>]*class="torrentname"', html or ""):
        if infohash.lower() in seg.lower():
            return seg
    return None


# =========================================================================== #
# 动作：resume / pause（幂等）
# =========================================================================== #
@dataclass
class Plan:
    to_resume: list = field(default_factory=list)    # 现在暂停、且已进免费窗口
    to_pause: list = field(default_factory=list)     # 现在在跑、但已不在免费窗口
    skipped: list = field(default_factory=list)      # (hash, why)


def build_plan(infos, windows: dict, *, now=None) -> Plan:
    """★ 纯函数：据 qB 现状 + 窗口判定，算出"该 resume 谁 / 该 pause 谁"。

    infos: qB `torrents/info` 的 list（含 hash, state, tags）。
    幂等靠这里：已 uploading 的不再 resume，已 stoppedDL 的不再 pause。
    """
    now = now or datetime.now()
    p = Plan()
    for t in infos:
        h = t.get("hash", "")
        st = t.get("state", "")
        w = windows.get(h, FreeWindow())
        is_free = w.is_free and not w.expired(now)
        paused = st in ("stoppedDL", "stoppedUP", "pausedDL", "pausedUP")

        if w.promo == "unknown":
            p.skipped.append((h, "unknown-promo"))        # ★ 抓不到 ⇒ 不动
        elif w.promo == "free_suspect":
            # ★ F-b：裸"全站Free活动"（无区间）= **弱信号**，语义歧义 ⇒ 绝不据此动作
            #    （既不当 free 去 resume，也不当收费去 pause）—— 与 unknown 同样保守。
            p.skipped.append((h, "free-suspect-weak-signal"))
        elif is_free and paused:
            p.to_resume.append(h)                          # 免费 + 暂停 ⇒ 起
        elif (not is_free) and (not paused):
            p.to_pause.append(h)                           # 不免费 + 在跑 ⇒ 停
        else:
            p.skipped.append((h, "already-correct"))
    return p


def apply_plan(qb, plan: Plan, *, dry_run=True, log=print) -> dict:
    """执行计划。★ dry_run=True 只打印。返回计数。"""
    n_r = n_p = 0
    if plan.to_resume:
        log(f"  → resume {len(plan.to_resume)} 条：" + ", ".join(x[:12] for x in plan.to_resume[:5]) + (" ..." if len(plan.to_resume) > 5 else ""))
        if not dry_run:
            qb.torrents_resume(torrent_hashes=plan.to_resume)
        n_r = len(plan.to_resume)
    if plan.to_pause:
        log(f"  → pause  {len(plan.to_pause)} 条：" + ", ".join(x[:12] for x in plan.to_pause[:5]) + (" ..." if len(plan.to_pause) > 5 else ""))
        if not dry_run:
            qb.torrents_pause(torrent_hashes=plan.to_pause)
        n_p = len(plan.to_pause)
    log(f"  · 跳过 {len(plan.skipped)} 条（unknown/free-suspect/already-correct）")
    return {"resumed": n_r, "paused": n_p, "skipped": len(plan.skipped)}


# =========================================================================== #
# 入口
# =========================================================================== #
def main(argv=None):
    ap = argparse.ArgumentParser(description="免费窗口 resume/pause（收费种子）")
    ap.add_argument("--apply", action="store_true", help="真做（默认只看）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（小样）")
    ap.add_argument("--only-hash", nargs="*", default=None, help="只动指定 hash（最小实验）")
    ap.add_argument("--tag", default=TAG_PAID, help=f"目标标签（默认 {TAG_PAID}）")
    args = ap.parse_args(argv)
    dry_run = not args.apply

    import config
    import qb_client
    import crawl
    import opencd as opencd_rate              # ★ RateGate 在 opencd 里（不是 crawl）
    import time as _time

    qb = qb_client.build()                       # 唯一构造点（不打印 key）
    infos = qb.torrents_info(tag=args.tag)       # ★ 只取该类标签的种
    if args.only_hash:
        infos = [t for t in infos if t.get("hash") in set(args.only_hash)]
    if args.limit:
        infos = infos[: args.limit]
    hashes = [t.get("hash", "") for t in infos]
    print(f"[free_window] 目标 {len(hashes)} 条（标签={args.tag!r}） dry_run={dry_run}")

    # ★ 显式构造 RateGate（同 backfill_covers_opencd.py 的用法；配额/间隔都在这）
    gate = opencd_rate.RateGate(
        max_per_round=int(os.getenv("OPENCD_MAX_PER_ROUND", "20")),
        min_interval=float(os.getenv("OPENCD_MIN_INTERVAL", "120")),
        max_per_day=int(os.getenv("OPENCD_MAX_PER_DAY", "50")),
        day_key=_time.strftime("%Y-%m-%d"),
        log=print,
    )
    site = crawl.Client(gate=gate)
    ok, why = site.check_login()
    if not ok:
        print(f"[free_window] 站点登录态无效：{why} ⇒ 本轮不改任何东西", file=sys.stderr)
        return 2

    windows = detect_free_windows(hashes, site_client=site)
    plan = build_plan(infos, windows)
    counts = apply_plan(qb, plan, dry_run=dry_run)

    # 原子落盘"本次结论"（供溯源；dry_run 也写，便于留痕）
    _save_state({"checked_at": datetime.now().isoformat(timespec="seconds"),
                 "dry_run": dry_run, "counts": counts,
                 "windows": {h: vars(w) for h, w in windows.items()}})
    print(f"[free_window] 完成 {counts}")
    return 0


def _save_state(obj):
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass                                     # 状态落盘失败不阻塞主流程


if __name__ == "__main__":
    raise SystemExit(main())
