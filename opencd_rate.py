"""
OpenCD（皇后）访问闸门 —— **所有**对皇后的请求都必须过这里。

为什么单独一个模块（而不是散在 crawl.py 里）：
    「控制频次」是本条流水线最要紧的纪律（用户反复强调），而它有两个特征：
      ① 容易在新增调用点时**忘记加**；
      ② 出问题时**没有症状**（只是"站点被敲得比预期勤"，不报错）。
    ⇒ 做成一个**必须显式调用**的闸门对象，并在文档/注释里写明
      「不经过它的请求 = 违规」。集中一处也才好审计（`grep rate_gate.acquire`）。

三道闸门各管一件事（★ 别混，混了就会出现"调了参数但没效果"）：
    ① **每日上限**  —— 硬刹车，防某天暴涨或代码出 bug 时狂发
    ② **每轮预算**  —— 防单轮爆量
    ③ **最小间隔**  —— 只防瞬时连击（★ 它不是节奏控制）
    ★ **节奏控制是"多久跑一轮"**（阶段周期），那是 `daemon.py` 的事，不在这里。
    这正是本项目那条教训：**一个种子发 2~3 个请求，
    所以"按请求计的间隔"算不准"每分钟处理几个"** —— 两件事必须分开表达。

★★★ 2026-09-19 起：**这三道闸门全是「请求」量纲，而用户要的是「种子」量纲** ——
    见下面「⚠️ 已知缺口」。当前值（用户 2026-09-19 定）：
      · `min_interval` = **120s**（每请求 2 分钟）
      · `max_per_day`  = **50**   ← ★ 已是用户要的种子数，但**量纲仍是请求数**，
                                    在「一种子 ≈ 2~3 请求」下偏保守（实际约 17~25 种子/天）
      · `max_per_round` = 20

✅ 2026-09-22 已修：**跨轮持久计数（那半步）已经做好了** ——
    `RateGate` 现在可以接一个**外部账本**（`ledger=` 参数），把 `_count_day` 存到
    `data.db` 的 `opencd_quota` 表里，于是 "每日 50" 从"每轮 50"变回**真的每日 50**。
      · 接线位置：`crawl.py` 主流程构造 gate 时传 `ledger=_DbLedger()`
        （适配器在 `crawl.py` 里，**故意不在这里 import db** —— 见下）
      · 账本由 `db.ledger_add()` **单条原子 UPSERT** 维护（理由见 `db.py` 的 LEDGER_* 注释）
      · `roll_day()` 跨天时**从账本按新日期重载**（不是简单清零 —— 理由见该方法注释）
    ★ 这条缺口**并未全部关闭**，剩下的仍是量纲：见下面 ①。

⚠️ 已知缺口（★ 别读成已解决）：
      ① `RateGate` 三个旋钮**全是「请求」量纲**，没有「种子」概念；
         ⇒ 拿 `max_per_day` 当种子配额用，量纲就是错的（宁可保守，不可越界）。
         ✅ **2026-10-04 部分闭合**：新增**种子量纲的独立账本**（`db.LEDGER_KIND_SEED`），
            由 `opencd_ingest.ingest()` 在"**真的新入库一条**"时 +1（重跑/补字段不重复计），
            并已在 `crawl.py` / `topup_opencd_fields.py` 的摘要里**与请求账分开报**。
            ⇒ 运维现在能看到"今日入库 N 条种子"，**但闸门（硬刹车）仍按请求**——
            这两件事**故意分开**：请求量纲是**站点保护**（不可越界），
            种子量纲是**产能口径**（给人看）。**没有**把闸门改成按种子卡
            （那会与站点保护机制错位 —— `ERR-META-05`：两种口径不许混）。
         ★ 想真正"按种子卡闸门"，仍要在**闸门层**引入种子概念 —— **还没做**，且有风险，暂不做。
      ② ~~`_count_day` 每轮归 0~~ ⇒ ✅ **已修**（见上，2026-09-22）。留此行只为标出
         "这里曾经错在哪"，免得后人把账本接线当成可选优化删掉。

★★ 用**绝对时间基准**，不是每轮 `sleep(固定值)`：
    第 N 个请求在 `起点 + (N-1)×间隔` 才发。
    后者在站点变慢时会把延迟**累积**上去，实际速率越跑越慢 —— 实测踩过
    （"限速用每轮 sleep，站点一慢速率就衰减"）。
    ★ 这条在 120s 这种大间隔下更明显：每轮多等一截，速率就衰减得更快。
    ★★ **`_origin` 每进程重置，但 `_count_day` 不重置** —— 这两个必须分开看：
       `min_interval` 管的是"本进程内两个请求之间别贴太近"，跨进程没有意义
       （本轮最后一个请求与下轮第一个请求之间隔着**阶段周期**，那由 daemon 管）；
       而 `_count_day` 管的是"这一天总共发了多少"，**必须跨进程**才有意义。

★★ 本模块**不 import 任何项目内模块**（只用 stdlib `time`）：
    这是刻意的，为的是让它能被**脱离 config/db 单独测**（例如
    `python opencd_rate.py --selftest` 不需要 `DATA_DIR` / `.env` 就能跑）。
    ⇒ 账本用**鸭子类型**（只要对象有 `get(key)` 与 `add(key)` 即可），
      真正的适配器放 `crawl.py` 里。**别在这里 import db** —— 那会把
      `config.require_env(...)` 的副作用（缺 .env 直接 sys.exit）带进来。
"""
import json
import sys
import time


class DailyQuotaExceeded(Exception):
    """
    当日请求数已达上限。★ 这是**正常保护**，不是错误 —— 调用方应优雅收工。

    ⚠️ 措辞是「请求数」不是「种子数」：`RateGate` 没有种子概念（模块头「已知缺口」①）。
    `crawl.py` 两处 `except DailyQuotaExceeded`（匹配循环 / 抓取循环）就是在收它。
    """


class RateGate:
    """
    皇后访问闸门。典型用法（**每个**请求都要走）::

        gate = RateGate(max_per_round=20, min_interval=120.0, max_per_day=50)
        for tid in candidates:
            gate.acquire()          # ← 可能抛 DailyQuotaExceeded（正常收工）
            resp = session.get(url)  # 到这一步才真的发包

    ★ `acquire()` 会**累计**这次请求，所以**不要**在没真发请求时调用它
      （那会虚耗配额）。
    """

    def __init__(self, *, max_per_round=20, min_interval=120.0,
                 max_per_day=50, day_key=None, log=None, ledger=None):
        self.max_per_round = int(max_per_round)
        self.min_interval = float(min_interval)
        self.max_per_day = int(max_per_day)
        self._now = time.monotonic            # ★ 单调钟：不受系统改时间影响
        self._origin = self._now()
        self._count_round = 0
        self._day_key = day_key               # 当日标识（如 '2026-09-19'）
        self._log = log or (lambda m: None)

        # ★★ 外部账本（跨进程持久）。**鸭子类型**：只要有 get(key)->int 与
        #    add(key, delta=1)->int 就行（真实适配器见 `crawl.py::_DbLedger`）。
        #    不 import db —— 理由见模块头最后一段（保持本模块零依赖、可独立测）。
        self._ledger = ledger
        # ★★ 从账本**载入**当日已用数。这行是整条"跨轮持久"的关键：
        #    没有它，新进程照样从 0 开始（本地计数与账本各说各话），账本就白记了。
        #    ★ 只在**给了 day_key** 时载入：没有 day_key 时"哪一天"是未知的，
        #      按 key=None 去查会得到一个恒为 0 的假象 —— 宁可保持 0 并让
        #      `can_acquire` 的每日闸门继续按"本地 0"保守工作。
        #    ★ 账本读失败**不能**让构造抛异常（那会让整轮 crawl 起不来，
        #      而配额读不到 = 保守方向上的小问题）⇒ 吞掉并降级为 0，但**要打日志**。
        self._count_day = 0
        if ledger is not None and day_key is not None:
            try:
                self._count_day = int(ledger.get(day_key))
            except Exception as e:                      # noqa: BLE001 - 见上
                self._log(f"⚠️  读配额账本失败，当日计数按 0 起步（会偏保守）: {e}")

    # ---------- 只读状态（给日志/测试用） ----------

    @property
    def used_round(self):
        return self._count_round

    @property
    def used_day(self):
        return self._count_day

    def budget_left_round(self):
        return max(0, self.max_per_round - self._count_round)

    def budget_left_day(self):
        return max(0, self.max_per_day - self._count_day)

    def reset_round(self):
        """
        新一轮开始（阶段每跑一次调一次）。★ 跨天时也要重置当日计数。

        ★ 2026-09-22：**跨轮持久已由账本承担**，本方法的职责缩小到"重开时间基准"。
          本项目「一轮 = 一个进程」（daemon 每轮 `Popen` 一个新 `python crawl.py`），
          所以 `_count_round` 天然从 0 开始；`_origin` 也跟着进程走。
          ⇒ 这个方法现在的正当用途只有一个：**同一个进程里想开新一轮**
            （比如将来的常驻模式或自检），此时把 `_origin` 挪到当前时刻，
            免得下一轮的第一个请求要去追上一轮已经过期的 `due` 时刻。

        ★ **`_count_day` 不在这里动** —— 它由账本载入、由 `acquire()` 写穿、
          由 `roll_day()` 在跨天时重载。这里清零等于**放松**配额，
          是"看起来在重置、其实在放水"的那类 bug。
        """
        self._count_round = 0
        self._origin = self._now()

    def roll_day(self, day_key):
        """
        跨天检查：`day_key` 与当前不同就**按新日期从账本重载**当日计数。

        ★ 由调用方（crawl 阶段）在每轮开头传当天日期进来 ——
          这里**不自己读时钟**，因为那会引入时区问题（`ERR-SQL-03` 的教训：
          库里的时间戳是 UTC、日志是 CST，混了就会得出"今晚没写入"的错误结论）。
          ★ 调用方传的 `day_key` 与账本 `day_key` 是**同一个口径**（都是本地日
            字符串，见 `db.LEDGER_COLUMNS` 的注释）—— 这两个必须同源，
            否则会出现"账本记的是今天的、gate 查的是昨天的"。

        ★★ 为什么是「重载」而不是「清零」（2026-09-22 修）：
          跨天那一瞬间，**另一个进程可能已经发过请求并写了账本**
          （比如上一轮恰好跨过零点、或两轮重叠）。此时清零 ⇒ 两边各再发满一份配额
          ⇒ 当天实际发到 2 倍。重载则无论谁先写，两边都看同一个权威数字。
          ⇒ 语义变成："把当日计数对齐到账本上**该天**的值"，
            而不是"假设该天还是 0"。

        ★ `day_key=None`（没传/没启用）时的行为：只更新标识，**不动计数**
          —— 没有账本时"清零"就是纯粹放松配额，宁可保持现状。
        """
        if self._day_key == day_key:
            return
        self._day_key = day_key

        if self._ledger is None or day_key is None:
            # 没有账本 ⇒ 无从得知新一天已经用了多少。**不清零**（见 docstring）。
            self._log(f"📅 换日（{day_key}），但无账本可用，当日计数保持 {self._count_day}")
            return

        try:
            self._count_day = int(self._ledger.get(day_key))
        except Exception as e:                          # noqa: BLE001 - 见 __init__
            self._log(f"⚠️  换日读账本失败，当日计数保持 {self._count_day}: {e}")
            return
        self._log(f"📅 新的一天（{day_key}），当日计数按账本重载为 {self._count_day}")

    # ---------- 核心 ----------

    def can_acquire(self):
        """还能不能发下一个请求。返回 (ok, 原因)。供"先判断再决定要不要做"的场景用。"""
        if self._count_day >= self.max_per_day:
            return False, f"当日请求已达上限 {self.max_per_day}"
        if self._count_round >= self.max_per_round:
            return False, f"本轮请求已达预算 {self.max_per_round}"
        return True, ""

    def acquire(self):
        """
        取一个请求许可。**会阻塞**到允许发包的时刻；超配额则抛 `DailyQuotaExceeded`。
        ★ 配额用尽抛异常而不是返回 False —— 强制调用方处理，避免"忘了判断"就继续发。
        """
        ok, why = self.can_acquire()
        if not ok:
            raise DailyQuotaExceeded(why)

        # ★ 绝对时间基准：第 N 个请求在 `起点 + (N-1)×间隔`。
        #   `_count_round` 是**已发**数，所以用它的当前值算"该发的时刻"。
        if self.min_interval > 0:
            due = self._origin + self._count_round * self.min_interval
            wait = due - self._now()
            if wait > 0:
                time.sleep(wait)

        self._count_round += 1

        # ★★ 写穿账本，并**用账本的返回值覆盖本地**：账本是权威。
        #   为什么不是"本地 +1 就够"：本地 +1 只在"本进程是唯一写者"时正确。
        #   而 crawl 一轮一个进程，今天可能有**多轮并发**（daemon 周期短于
        #   一轮耗时，或手动跑了一次）⇒ 本地计数会偏低，配额就漏了。
        #   取账本返回值 ⇒ 自动把别人已发的那几次也算进来。
        # ★ 顺序刻在这里：**先记账、再加本地**不可行（账本可能抛异常，
        #   那会让一个已经"获准"的请求凭空消失）。这里是"获准 → 计本地 → 记
        #   账本"，账本抛异常时**异常向上冒**（fail-closed，宁可本轮中断，
        #   不可少记一次配额 —— 见模块头与 `R4`）。★ 别忘了这一步**还没发包**：
        #   异常发生在 `Client.get()` 的 `acquire()` 里，请求不会发出去。
        if self._ledger is not None and self._day_key is not None:
            self._count_day = int(self._ledger.add(self._day_key))
        else:
            self._count_day += 1

        return self._count_round

    def pending_daily(self):
        """
        当日还剩多少请求预算（**面向用户报数**，与 `budget_left_day()` 同值）。

        ★ 存在只为让调用方读到一个**明确**的名字：`budget_left_day()` 是从
          "上限"出发算的，而这里是从"账本还剩多少"出发说的 —— 日志里
          写成"今日剩余 50"比"当日预算 50"更不容易被误读成"已经发了 50"。
        """
        return max(0, self.max_per_day - self._count_day)


# ==================== 自检 ====================
#
# ★ 本模块的自检**不需要 .env / DATA_DIR**（只 import time）—— 这正是它
#   零依赖的价值：闸门逻辑可以在任何地方单独验证，不必先搭整套配置。
#   账本用 stub（`_StubLedger`）替代，见判据③④⑤的对照设计。

class _StubLedger:
    """
    内存账本，接口与 `crawl._DbLedger` 一致（鸭子类型）。

    ★ `add` 返回**累加后**的值（与 `db.ledger_add` 同语义）—— 这一点很要紧：
      若自检的 stub 返回别的（比如 delta），就会把一个实现错误测成通过。
      ⇒ **stub 的语义必须与真账本一致**，否则自检测的是 stub 不是被测代码。
    """

    def __init__(self, init=None):
        self.data = dict(init or {})
        self.add_calls = []

    def get(self, day_key):
        return int(self.data.get(str(day_key), 0))

    def add(self, day_key, delta=1):
        k = str(day_key)
        self.data[k] = int(self.data.get(k, 0)) + int(delta)
        self.add_calls.append((k, int(delta)))
        return self.data[k]


def cmd_selftest(as_json=False):
    # ★ 自检会打 ✅/❌ 这类非 GBK 字符，而 Windows 控制台默认 GBK
    #   ⇒ 不加这行会在**第一行输出**就 UnicodeEncodeError（实测）。
    #   与 `crawl.py` 同款守卫；失败也不该影响逻辑，故吞掉异常。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass

    ok = fails = 0

    def chk(label, good, extra=""):
        nonlocal ok, fails
        ok, fails = ok + bool(good), fails + (not bool(good))
        line = f"  {'✅' if good else '❌'} {label}"
        if extra:
            line += f"   {extra}"
        print(line)

    # ---------- ① 构造时从账本载入当日计数 ----------
    print("\n① 账本载入（跨轮持久的入口）")
    led = _StubLedger({"2026-09-22": 7})
    g = RateGate(max_per_day=50, day_key="2026-09-22", ledger=led)
    chk("新进程载入账本已有值", g.used_day == 7, f"used_day={g.used_day}（期望 7）")
    chk("⛔阴性对照：无账本 ⇒ 归 0（这就是修之前的坏行为）",
        RateGate(max_per_day=50, day_key="2026-09-22").used_day == 0)
    chk("⛔阴性对照：有账本但无 day_key ⇒ 不载入（宁保守，不放假）",
        RateGate(max_per_day=50, ledger=_StubLedger({"2026-09-22": 7})).used_day == 0)

    # ---------- ② acquire() 真的写穿账本（断言 stub，不是 gate） ----------
    print("\n② acquire() 写穿（★ 断言 stub 的存储，不是 gate.used_day）")
    led = _StubLedger()
    g = RateGate(min_interval=0, max_per_day=50, day_key="2026-09-22", ledger=led)
    g.acquire()
    chk("stub 里真的多了 1 条记录", led.add_calls == [("2026-09-22", 1)],
        f"add_calls={led.add_calls}")
    g.acquire()
    chk("第二次累加到 2（不是覆盖成 1）", led.get("2026-09-22") == 2,
        f"stub={led.get('2026-09-22')}")
    chk("gate.used_day 与账本一致", g.used_day == 2, f"used_day={g.used_day}")

    # ---------- ③ 跨轮持久：新 gate 同账本必须延续 ----------
    print("\n③ 跨轮持久（daemon 每轮起新进程 ⇒ 换 gate 不换账本）")
    shared = _StubLedger()
    gA = RateGate(min_interval=0, max_per_day=3, day_key="2026-09-22", ledger=shared)
    for _ in range(3):
        gA.acquire()
    chk("gate A 用满 3 次", gA.used_day == 3)
    gB = RateGate(min_interval=0, max_per_day=3, day_key="2026-09-22", ledger=shared)
    chk("★ 新 gate（= 新进程）载入到 3，配额**延续**", gB.used_day == 3,
        f"used_day={gB.used_day}")
    raised = ""
    try:
        gB.acquire()
    except DailyQuotaExceeded as e:
        raised = str(e)
    chk("★ 所以第二次必须被拦（这才是真·每日上限）", "当日请求已达上限" in raised,
        f"异常={raised!r}")
    chk("⛔阴性对照：换成**新账本** ⇒ 计数归 0，拦不住（= 修之前的坏行为）",
        RateGate(min_interval=0, max_per_day=3, day_key="2026-09-22",
                 ledger=_StubLedger()).used_day == 0)

    # ---------- ④ 跨天：roll_day 换日期 ⇒ 按账本重载 ----------
    print("\n④ 跨天（roll_day 的语义：重载，不是清零）")
    led = _StubLedger({"2026-09-22": 5, "2026-09-23": 2})
    g = RateGate(min_interval=0, max_per_day=50, day_key="2026-09-22", ledger=led)
    chk("起手 = 22 日的 5", g.used_day == 5, f"used_day={g.used_day}")
    g.roll_day("2026-09-22")
    chk("⛔阴性对照：同一天调用 ⇒ 计数不变", g.used_day == 5, f"used_day={g.used_day}")
    g.roll_day("2026-09-23")
    chk("★ 换到 23 日 ⇒ 重载成 23 日的 2（不是清零、也不是留 5）",
        g.used_day == 2, f"used_day={g.used_day}")
    chk("账本没有被 roll_day 写坏", led.get("2026-09-23") == 2,
        f"stub={led.data}")

    # ---------- ⑤ 跨天重载是**双向**的（往回换也对） ----------
    print("\n⑤ 跨天重载的双向性（防「只会写 0」的实现混过 ④）")
    g.roll_day("2026-09-22")
    chk("换回 22 日 ⇒ 重载回 5", g.used_day == 5, f"used_day={g.used_day}")

    # ---------- ⑥ 无账本时 roll_day 不清零（不清零 = 不放水） ----------
    print("\n⑥ 无账本 / 无 day_key 时 roll_day 的行为")
    g = RateGate(min_interval=0, max_per_day=2, day_key="2026-09-22")
    g.acquire(); g.acquire()
    g.roll_day("2026-09-23")
    chk("★ 无账本时换日**保持**计数（清零=放松配额，是危险方向）",
        g.used_day == 2, f"used_day={g.used_day}")

    # ---------- ⑦ 两个上限各自独立约束（旧的强约束不许被账本改动打破） ----------
    print("\n⑦ 每轮预算 与 每日上限 各自独立（量纲仍是请求，见模块头①）")
    led = _StubLedger()
    g = RateGate(min_interval=0, max_per_round=2, max_per_day=100,
                 day_key="d", ledger=led)
    g.acquire(); g.acquire()
    why = ""
    try:
        g.acquire()
    except DailyQuotaExceeded as e:
        why = str(e)
    chk("每轮预算触发（消息含「本轮」）", "本轮请求已达预算" in why, f"{why!r}")
    g2 = RateGate(min_interval=0, max_per_round=100, max_per_day=2,
                  day_key="d", ledger=_StubLedger())
    g2.acquire(); g2.acquire()
    why2 = ""
    try:
        g2.acquire()
    except DailyQuotaExceeded as e:
        why2 = str(e)
    chk("每日上限触发（消息含「当日」）", "当日请求已达上限" in why2, f"{why2!r}")

    # ---------- ⑧ 绝对时间基准（防"每轮 sleep 累积"那条退化回归） ----------
    print("\n⑧ min_interval 用绝对时间基准（不是每轮 sleep 累积）")
    # ★ 用**假时钟**而不是真 sleep：真 sleep 会让自检变慢，而这里要验的是
    #   "第 N 个的到期时刻 = 起点 + (N-1)×间隔"，那是纯算术，不需要真的等。
    ticks = {"now": 1000.0}

    def fake_now():
        return ticks["now"]

    slept = []

    def fake_sleep(s):
        slept.append(s)
        ticks["now"] += s

    g = RateGate(min_interval=10.0, max_per_day=50)
    g._now = fake_now
    g._origin = fake_now()
    orig_sleep = time.sleep
    time.sleep = fake_sleep
    try:
        g.acquire()                      # 第 1 个：due = 1000 + 0×10 = 1000，不等
        chk("第 1 个请求不等（due = 起点）", slept == [], f"slept={slept}")
        g.acquire()                      # 第 2 个：due = 1010，等 10
        chk("第 2 个请求恰好等 10s（= (2-1)×10）",
            len(slept) == 1 and abs(slept[0] - 10.0) < 1e-9, f"slept={slept}")
        ticks["now"] += 100              # 模拟站点变慢：中间白白过了 100s
        slept.clear()
        g.acquire()                      # 第 3 个：due = 1020，早已过 ⇒ **不等**
        chk("★ 站点变慢后不追加延迟（第 3 个 due=1020 已过 ⇒ 0 等待）",
            slept == [], f"slept={slept}（累积式实现会在这里多等）")
    finally:
        time.sleep = orig_sleep

    print(f"\n{'=' * 66}")
    if as_json:
        print(json.dumps({"script": "opencd_rate.py --selftest", "ok": ok,
                          "total": ok + fails, "exit": 1 if fails else 0},
                         ensure_ascii=False))
        return 1 if fails else 0
    if fails:
        print(f"❌ 自检失败：{ok}/{ok + fails} 项通过，{fails} 项不符")
        return 1
    print(f"✅ 自检通过：{ok}/{ok + fails} 项")
    print("   ★ 核心是 ③（跨轮持久）+ ④⑤（跨天重载双向）：")
    print("     没有 ③，「每日 50」就还是「每轮 50」—— 这正是本次要修的缺口。")
    return 0


def main():
    import argparse
    ap = argparse.ArgumentParser(description="OpenCD 访问闸门（RateGate）")
    ap.add_argument("--selftest", action="store_true", help="跑闸门逻辑自检（零外网）")
    ap.add_argument("--max-per-round", type=int, default=20)
    ap.add_argument("--min-interval", type=float, default=120.0)
    ap.add_argument("--max-per-day", type=int, default=50)
    # ★ --json：机读出口，给 tools/gen_counts.py 读（与 --selftest 合用）。
    ap.add_argument("--json", action="store_true",
                    help="与 --selftest 合用：只把结果以 JSON 打到末行")
    args = ap.parse_args()

    if args.selftest:
        return cmd_selftest(as_json=args.json)

    # 不带参数时只报一遍当前配置 —— 本模块是库，不是命令；真正驱动它在 crawl.py。
    g = RateGate(max_per_round=args.max_per_round, min_interval=args.min_interval,
                 max_per_day=args.max_per_day, day_key=time.strftime("%Y-%m-%d"))
    print(f"RateGate: 每轮 {g.max_per_round} 请求 · 最小间隔 {g.min_interval:g}s · "
          f"每日上限 {g.max_per_day}（★ 请求量纲）")
    print("（本模块是闸门库；实际抓取入口是 topup_opencd_fields.py（批量）"
          "与 crawl.py（单条 --tid/--url）。加 --selftest 自检）")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
