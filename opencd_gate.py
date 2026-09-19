"""
两个 3 天闸门（`SUMMARY §12` 决定 5）—— **纯逻辑、零请求、零数据库。**

═══ 闸门长什么样 ═══

    别的下载器下好 → qB 做种满 3 天 → 抓皇后页面 → 再等 3 天 → 发 Musopia
                          ↑                              ↑
                    闸门①：证明种子下完、在做种        闸门②：人工反悔窗口

★ **闸门①为什么比「抓到就等 3 天」强**（用户 2026-09-19 提出）：
  · **没做满 3 天根本不抓** ⇒ 更省请求；
  · 「在做种」**证明这个种子真的下完可用** —— 原方案只管「延迟」，
    不验证可用性（下了一半的种子也会被延迟 3 天后发出去）。

★ **闸门②是串联，不是重叠**（用户原话：「做种满三天了，再去皇后下载网页，
  在三天后去 musopia 发布」）。
  含义是「**我已经看过页面内容、反悔还来得及**」——
  所以是 `fetched_at + 3 天`，而**不是**「做种满 6 天就发」。
  ⚠️ 若实为重叠（做种 3 天即发），改这一个常量即可，判据会跟着变。

═══ 为什么单独一个模块 ═══

闸门判据是**时间比较**，而本项目在时间上栽过不止一次
（`ERR-SQL-03`：库里是 UTC、日志是 CST，混了就得出「今晚没写入」这种**假阴性**）。
所以这里把判据做成**纯函数**，输入全是显式的 datetime，**不自己读时钟**、
**不自己查库** —— 这样它能被单测穷举，且不可能踩时区坑：
调用方从库里读出来的时间戳，必须先自己用 `datetime(..., 'localtime')` 转好再传进来。
"""
from datetime import datetime, timedelta

# 闸门天数。★ 用户 2026-09-19 定：两个都是 3 天。
QB_SEED_DAYS_REQUIRED = 3
PUBLISH_DELAY_DAYS = 3

# 闸门判定的结果（字符串常量，便于进日志/邮件/库）
GATE_NOT_SEEDED = "NOT_SEEDED"        # 闸门①未满：做种不足 3 天 ⇒ **不抓**
GATE_READY_TO_FETCH = "READY_TO_FETCH"  # 闸门①已满 ⇒ 可以抓页面
GATE_WAITING = "WAITING"               # 已抓、但闸门②未满 ⇒ 还不发
GATE_READY_TO_PUBLISH = "READY_TO_PUBLISH"  # 两个闸门都过 ⇒ 可以发


def fetch_gate_ok(qb_seed_days, required=QB_SEED_DAYS_REQUIRED):
    """
    闸门①：在 qB 里做种够久了吗？返回 (ok, 原因)。

    `qb_seed_days` 是在 qB 里的做种天数（float）。★ 用**天数**而不是绝对时间戳：
    做种时长由 qB 自己算，我们只关心「够了没有」，比较两个绝对时间更容易踩时区。
    ★ `None`（拿不到）按**不满足**处理 —— 宁可晚抓，不可早发。
    """
    if qb_seed_days is None:
        return False, "拿不到做种时长（qB 没这个种子？）"
    try:
        days = float(qb_seed_days)
    except (TypeError, ValueError):
        return False, f"做种时长不是数字：{qb_seed_days!r}"
    if days < required:
        return False, f"做种 {days:.2f} 天 < {required} 天"
    return True, f"做种 {days:.2f} 天 ≥ {required} 天"


def publish_gate_ok(fetched_at, now, delay_days=PUBLISH_DELAY_DAYS):
    """
    闸门②：抓完之后的人工反悔窗口过了吗？返回 (ok, 原因)。

    ★ 两个时间都必须由**调用方**传进来（显式依赖）：
      · `fetched_at` —— 抓页面的时刻
      · `now`        —— "现在"
      这样本函数**不读时钟**，可被穷举测试，
      也把「UTC/CST 混用」的风险挡在函数之外（调用方先转好再传）。
    """
    if fetched_at is None:
        return False, "还没抓过页面（fetched_at 为空）"
    if not isinstance(fetched_at, datetime) or not isinstance(now, datetime):
        return False, "fetched_at / now 必须是 datetime"
    due = fetched_at + timedelta(days=delay_days)
    if now < due:
        left = due - now
        return False, f"反悔窗口未满，还需 {left.total_seconds() / 3600:.1f} 小时（{due:%Y-%m-%d %H:%M}）"
    return True, f"已过 {delay_days} 天反悔窗口"


def decide(qb_seed_days, fetched_at=None, now=None,
           seeded_required=QB_SEED_DAYS_REQUIRED,
           delay_days=PUBLISH_DELAY_DAYS):
    """
    综合两个闸门，给出**当前该做什么**。

    返回 `(state, reason)`，state 是上面四个**之一**：
      · `NOT_SEEDED`        —— 闸门①未满，连抓都不要抓（★ 省请求的关键）
      · `READY_TO_FETCH`    —— 闸门①满了、还没抓 ⇒ 去抓页面
      · `WAITING`           —— 抓了但闸门②未满 ⇒ 等着，别发
      · `READY_TO_PUBLISH`  —— 都过了 ⇒ 可以发

    ★ 顺序是**串联**：闸门①不过就**根本不该抓**，所以这时候不关心闸门②。
      写成「两个条件都算完再 and」也能跑，但那样在闸门①不过时会先消耗掉
      `publish_gate_ok()` 的调用 —— 语义上就变成了「并行」。
    """
    ok1, why1 = fetch_gate_ok(qb_seed_days, seeded_required)
    if not ok1:
        return GATE_NOT_SEEDED, why1
    if fetched_at is None:
        return GATE_READY_TO_FETCH, "闸门①已过，尚未抓取"
    ok2, why2 = publish_gate_ok(fetched_at, now, delay_days)
    if not ok2:
        return GATE_WAITING, why2
    return GATE_READY_TO_PUBLISH, why2


def publish_after_from(fetched_at, delay_days=PUBLISH_DELAY_DAYS):
    """抓取时刻 → 可发布时刻。写进库的 `publish_after` 列，供阶段筛选用。"""
    if fetched_at is None:
        return None
    return fetched_at + timedelta(days=delay_days)


# ==================== 自检 ====================
#
# ★ 判据设计要点：两个闸门**各自**都要有用例，且要覆盖"边界"与"串联语义"。

def _check():
    from datetime import timezone
    T = datetime(2026, 9, 19, 12, 0, 0)          # 固定的"现在"，便于穷举

    ok = fails = 0

    def case(label, want, got):
        nonlocal ok, fails
        good = want == got
        ok, fails = ok + good, fails + (not good)
        print(f"  {'✅' if good else '❌'} {label:<52} 期望={want!r:<18} 实得={got!r}")

    print("① 闸门①：做种不够 3 天 ⇒ NOT_SEEDED（★ 连抓都不抓）")
    for days in (0, 1.0, 2.99):
        s, _ = decide(qb_seed_days=days, now=T)
        case(f"做种 {days} 天", GATE_NOT_SEEDED, s)

    print("\n② 闸门① 边界：正好 3.0 天 ⇒ 放行（>= 而不是 >）")
    s, why = decide(qb_seed_days=3.0, now=T)
    case("做种 3.0 天 → READY_TO_FETCH", GATE_READY_TO_FETCH, s)

    print("\n③ 拿不到做种时长（None）⇒ 按不满足处理（宁晚不早）")
    s, why = decide(qb_seed_days=None, now=T)
    case("qb_seed_days=None", GATE_NOT_SEEDED, s)

    print("\n④ 闸门①过了、还没抓 ⇒ READY_TO_FETCH")
    s, _ = decide(qb_seed_days=10, fetched_at=None, now=T)
    case("已做种 10 天、未抓", GATE_READY_TO_FETCH, s)

    print("\n⑤ 闸门②：刚抓完 ⇒ WAITING（★ 不许立刻发）")
    for hours in (0, 1, 71):
        fa = T - timedelta(hours=hours)
        s, _ = decide(qb_seed_days=10, fetched_at=fa, now=T)
        case(f"抓完 {hours} 小时后", GATE_WAITING, s)

    print("\n⑥ 闸门② 边界：正好 3 天（72 小时）⇒ 可以发")
    fa = T - timedelta(hours=72)
    s, _ = decide(qb_seed_days=10, fetched_at=fa, now=T)
    case("抓完正好 72 小时", GATE_READY_TO_PUBLISH, s)

    print("\n⑦ 都过了 ⇒ READY_TO_PUBLISH")
    fa = T - timedelta(days=5)
    s, whys = decide(qb_seed_days=10, fetched_at=fa, now=T)
    case("做种 10 天、抓完 5 天", GATE_READY_TO_PUBLISH, s)

    print("\n⑧ ★ 串联语义：做种不满时，**不该**去看闸门②")
    # 构造一个"闸门②已过"的 fetched_at；若实现是并联，就会误判成 READY_TO_PUBLISH
    fa = T - timedelta(days=99)          # 闸门②早过了
    s, why = decide(qb_seed_days=1, fetched_at=fa, now=T)   # 但闸门①没过
    case("闸门①不过 + 闸门②已过 ⇒ 必须 NOT_SEEDED", GATE_NOT_SEEDED, s)

    print("\n⑨ 时区安全：naive 与 aware 混用应被处理（不能静默算错）")
    aware = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 9, 19, 12, 0)
    try:
        # naive vs aware 直接比较会抛 TypeError —— 这是**好事**，响亮地坏
        # （本项目判据：响亮地坏，优于安静地错）
        decide(qb_seed_days=10, fetched_at=naive, now=aware)
        mixed = "没抛异常"
    except TypeError:
        mixed = "抛 TypeError"
    case("naive/aware 混用 → 抛错（不静默算错）", "抛 TypeError", mixed)

    print("\n⑩ publish_after_from：抓取时刻 + 3 天")
    fa = datetime(2026, 9, 19, 12, 0)
    case("+3 天", datetime(2026, 9, 22, 12, 0), publish_after_from(fa))
    case("None → None", None, publish_after_from(None))

    return ok, fails


def main():
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="OpenCD 转载的两个 3 天闸门")
    ap.add_argument("--selftest", action="store_true", help="跑离线自检")
    args = ap.parse_args()

    if not args.selftest:
        ap.error("加 --selftest")

    print("=" * 74)
    print("opencd_gate 离线自检（纯逻辑，零请求、零数据库）")
    print("=" * 74)
    ok, fails = _check()
    print("\n" + "=" * 74)
    if fails:
        print(f"❌ 自检失败：{ok}/{ok + fails} 通过，{fails} 项不符")
        return 1
    print(f"✅ 自检通过：{ok}/{ok} 项全对")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
