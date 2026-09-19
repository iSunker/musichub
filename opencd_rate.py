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
    这正是本项目 `SUMMARY §3.11` 那条教训：**一个种子发 2~3 个请求，
    所以"按请求计的间隔"算不准"每分钟处理几个"** —— 两件事必须分开表达。

★★★ 2026-09-19 起：**这三道闸门全是「请求」量纲，而用户要的是「种子」量纲** ——
    见下面「⚠️ 已知缺口」。当前值（用户 2026-09-19 定）：
      · `min_interval` = **120s**（每请求 2 分钟）
      · `max_per_day`  = **50**   ← ★ 已是用户要的种子数，但**量纲仍是请求数**，
                                    在「一种子 ≈ 2~3 请求」下偏保守（实际约 17~25 种子/天）
      · `max_per_round` = 20

⚠️ 已知缺口（★ 别读成已解决）：**「每天最多 N 个种子」当前做不到** ——
    不是配置问题，是**缺机制**，两处叠加：
      ① `RateGate` 三个旋钮**全是「请求」量纲**，没有「种子」概念；
         ⇒ 拿 `max_per_day` 当种子配额用，量纲就是错的（宁可保守，不可越界）。
      ② ★★ **`RateGate` 每个进程只 new 一次**（`crawl.py` 的 `main()` 里），
         而 daemon 每轮用 `subprocess.Popen` 起**新进程**（`daemon.py:252`）
         ⇒ `_count_day` **每轮归 0** ⇒ 现有「每日上限」实际退化成**每轮上限**。
         `roll_day()` / `reset_round()` 至今**零调用者**（死代码）就是这条的症状。
    ⇒ 真正的跨轮持久配额要等「接调度 + 入库」那一步**一起做**，因为那时才需要一个
      统一的状态层（账本落 `/data`，与 `daemon_state.json` 同源）。
      ★ 判据也要一起补：跨轮持久（新进程、同一账本 ⇒ 第二个必须被拦）与跨天归零。

★★ 用**绝对时间基准**，不是每轮 `sleep(固定值)`：
    第 N 个请求在 `起点 + (N-1)×间隔` 才发。
    后者在站点变慢时会把延迟**累积**上去，实际速率越跑越慢 ——
    `SUMMARY §3.11` 实测踩过（"限速用每轮 sleep，站点一慢速率就衰减"）。
    ★ 这条在 120s 这种大间隔下更明显：每轮多等一截，速率就衰减得更快。
"""
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
                 max_per_day=50, day_key=None, log=None):
        self.max_per_round = int(max_per_round)
        self.min_interval = float(min_interval)
        self.max_per_day = int(max_per_day)
        self._now = time.monotonic            # ★ 单调钟：不受系统改时间影响
        self._origin = self._now()
        self._count_round = 0
        self._count_day = 0
        self._day_key = day_key               # 当日标识（如 '2026-09-19'）
        self._log = log or (lambda m: None)

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

        ⚠️ **至今零调用者（死代码）** —— 见模块头「已知缺口」②。
        本项目「一轮 = 一个进程」（daemon 每轮 `Popen` 一个新 `python crawl.py`），
        所以 `_count_round` 天然从 0 开始，不需要重置；而 `_count_day` 想跨轮累计
        **必须靠外部账本**，靠这个方法的重置只会让它更不像"每日"上限。
        ★ 保留它（而非删掉）是因为「接调度 + 入库」那步要引入账本，
          届时它会被接线到账本的跨天逻辑上 —— 现在删掉等于把坑挪走、不留标记。
        """
        self._count_round = 0
        self._origin = self._now()

    def roll_day(self, day_key):
        """
        跨天检查：`day_key` 与当前不同就清零当日计数。
        ★ 由调用方（crawl 阶段）在每轮开头传当天日期进来 ——
          这里**不自己读时钟**，因为那会引入时区问题（`ERR-SQL-03` 的教训：
          库里的时间戳是 UTC、日志是 CST，混了就会得出"今晚没写入"的错误结论）。

        ⚠️ **至今零调用者（死代码）** —— 见模块头「已知缺口」②。
        没有账本时它最多把计数清成 0（**放松**配额），接线反而更危险 ⇒
        留到「接调度 + 入库」那步与账本一起接。
        """
        if self._day_key != day_key:
            self._day_key = day_key
            self._count_day = 0
            self._log(f"📅 新的一天（{day_key}），当日配额计数清零")

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
        self._count_day += 1
        return self._count_round
