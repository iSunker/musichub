"""
两个 3 天闸门 —— **纯逻辑、零请求、零数据库。**

═══════════════════════════════════════════════════════════════════════════
★★ 2026-09-23：**闸门①（做种 3 天）已退役，闸门② 改锚到皇后的发布日期。**
═══════════════════════════════════════════════════════════════════════════

  本模块刚写出来时的设计：

      别的下载器下好 → qB 做种满 3 天 → 抓皇后页面 → 再等 3 天 → 发 Musopia
                            ↑                              ↑
                      闸门①：证明种子下完、在做种        闸门②：人工反悔窗口

  ★★ 为什么闸门① 退役（两条都是实测，不是"觉得没必要"）：

    1. **它早就是死的** —— 生产库里 `qb_seed_days` **555 行全部为 NULL**
       （实测 2026-09-23）⇒ `fetch_gate_ok(None)` 恒返回 **不满足**。
       也就是说：**没有任何一行**曾经通过过闸门①。
       而现在这条流水线（`vtid` → `topup`）拿 tid 时**根本不经过它** ——
       它的输入 `qb_seed_days` 现在**没有任何写入方**。
       ⇒ 留着一个"看起来在管、其实从不生效"的闸门，比没有更危险
         （`ERR-META-01`：失效的检测比不检测更危险 —— 你会以为有人看着）。

    2. **它的原意已经被更好的东西取代** —— 闸门① 的目的是"证明这个种子
       真的下完可用"。而**新路**（`sync_opencd_tid.py` 从 vertex2 库按
       `info_hash` 配到皇后的 `details.php?id=`）**本身就是更强的证据**：
       那条种子在皇后的页面上存在，说明它是个完整、可下载的种子。
       ⇒ 不需要再用"做种时长"这个我们自己造的、间接的代理指标。

  ★★ 为什么闸门② 换锚（从 `fetched_at` → `opencd_added_at`）：

    `fetched_at` 是**我们**抓页面的时刻 ⇒ 反悔窗口从**我们自己动手**那一刻起算。
    这有个内在毛病：**晚抓 = 白等**。那些早就上站几个月、直到今天才补页面的行，
    反而要再干等三天，与"这条新不新"**完全脱钩**。

    `opencd_added_at`（= 皇后的 `add_time`，该种子被加进 tracker 的时刻）是
    **外部事实**：皇后什么时候发的，不会因为我们晚抓而改变。
    ⇒ 窗口锚在真正该锚的地方。用户 2026-09-23 定：「按皇后的添加日期算」，
      门槛 **7 天**（原 3 天）。

  ★★ 本模块现在还剩什么：
    · `publish_gate_ok()` 的判据逻辑仍被 `opencd_gate` 的自检用着，且它是
      **纯函数**（两个 datetime 进来、一个判断出去），**没有时区坑** ——
      但**发布队列的实际闸门已经不在本模块里**了：
      它是 `db.PENDING_PUBLISH_WHERE` 里的一句 SQL（`opencd_added_at <=
      datetime('now','-7 days')`）。
      ⚠️ **改门槛天数要改那里，不是改本模块的 `PUBLISH_DELAY_DAYS`** ——
         这两个常量现在**只服务于自检**，不再驱动生产。
    · `fetch_gate_ok()` **留着不删**（它是纯函数、自检还在跑），但
      **它在生产里没有任何调用方** —— 上面那段就是"为什么退役"的答案。
      ★ 若将来要恢复"做种时长"门槛，先想清楚**谁来写 `qb_seed_days`**。

═══ 为什么单独一个模块 ═══

闸门判据是**时间比较**，而本项目在时间上栽过不止一次
（`ERR-SQL-03`：库里是 UTC、日志是 CST，混了就得出「今晚没写入」这种**假阴性**）。
所以这里把判据做成**纯函数**，输入全是显式的 datetime，**不自己读时钟**、
**不自己查库** —— 这样它能被单测穷举，且不可能踩时区坑：
调用方从库里读出来的时间戳，必须先自己用 `datetime(..., 'localtime')` 转好再传进来。
"""
import json
from datetime import datetime, timedelta
import math

# 闸门天数。★ 2026-09-23 起这两个常量**不再驱动生产** ——
#   真实门槛在 `db.PENDING_PUBLISH_WHERE`（`opencd_added_at <= datetime('now','-7 days')`）。
#   ★ 留着是因为本模块的自检还在用它们当输入（纯函数测试需要一套天数）：
#     `QB_SEED_DAYS_REQUIRED` 只喂 `fetch_gate_ok` 的自检（该函数已无生产调用方）；
#     `PUBLISH_DELAY_DAYS` 只喂 `publish_gate_ok` 的自检。
#   ⚠️ **改生产门槛请改 db.py，不要改这里** —— 改了这里队列不会有任何变化，
#      那正是"调了没效果"的旋钮（比没有这个旋钮更糟）。
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
    ★★ 2026-09-21 补：**非有限值（NaN / inf）也必须按「不满足」处理**。
      原先只判 `days < required`，而 `float('nan') < 3` 是 **False** ⇒
      **NaN 会被判「满足」**（实测 `decide(nan)` → `READY_TO_FETCH`、
      `decide(inf)` → `READY_TO_FETCH`）—— 这**违反**本函数自己的纪律
      「宁可晚抓，不可早发」。`inf` 同理（一个坏掉的时长不该被当成"做种很久了"）。
      ⇒ 用 `math.isfinite` 挡掉。★ 这条今天没炸只是因为**零调用方**。
    """
    if qb_seed_days is None:
        return False, "拿不到做种时长（qB 没这个种子？）"
    try:
        days = float(qb_seed_days)
    except (TypeError, ValueError):
        return False, f"做种时长不是数字：{qb_seed_days!r}"
    if not math.isfinite(days):
        # ★ NaN / inf：**绝不能**因为"比较结果是 False"就被放行
        return False, f"做种时长不是有限数字：{qb_seed_days!r}"
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

    print("\n③b ★★ 非有限值（NaN / inf）也必须**不满足** —— 阴性对照")
    # ★ 这条防的是「`nan < 3` 是 False ⇒ 被误判成满足」：
    #   2026-09-21 实测过 `decide(nan)` → READY_TO_FETCH（**放行**），
    #   违反本模块「宁可晚抓，不可早发」的纪律。删掉 `math.isfinite` 那行，
    #   下面两条必须当场 ❌。
    for bad, label in ((float("nan"), "NaN"), (float("inf"), "inf"),
                       (float("-inf"), "-inf"), ("不是数字", "字符串")):
        s, why = decide(qb_seed_days=bad, now=T)
        case(f"{label} ⇒ 必须 NOT_SEEDED", GATE_NOT_SEEDED, s)

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

    ap = argparse.ArgumentParser(description="OpenCD 转载闸门（闸门① 已退役；闸门② 现由 db.py 的 WHERE 承担）")
    ap.add_argument("--selftest", action="store_true", help="跑离线自检")
    # ★ --json：机读出口，给 tools/gen_counts.py 读。别去正则解析人读的 ✅/❌ 行 —— 分母口径不统一。
    ap.add_argument("--json", action="store_true",
                    help="只把结果以 JSON 打到末行（给 gen_counts.py 读）")
    args = ap.parse_args()

    if not args.selftest:
        ap.error("加 --selftest")

    if args.json:
        ok, fails = _check()
        print(json.dumps({"script": "opencd_gate.py --selftest", "ok": ok,
                          "total": ok + fails, "exit": 1 if fails else 0},
                         ensure_ascii=False))
        return 1 if fails else 0

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
