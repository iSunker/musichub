"""tools/test_qb_ready.py —— #7 的 qB readiness 闸门**离线**单测（零网络、零等待）。

═══════════════════════════════════════════════════════════════════════════
它测什么（对应 `开机时序改法草案-2026-10-06.md` §5）
═══════════════════════════════════════════════════════════════════════════
`daemon._wait_qb_ready()` 是"回加前等 qB 就绪"的闸门（封住开机竞态 SSLError port=3020）：
  · probe 立刻 True ⇒ 立即返回 True、**不 sleep**；
  · probe 前 N 次 False、之后就绪 ⇒ True，sleep 了 N 次；
  · probe 恒 False ⇒ **超时返回 False**（**绝不 raise**），sleep 次数 = timeout/interval 量级；
  · 用**注入的假 clock/sleep** ⇒ 单测**零真实等待**。

★ 纪律：每条判据配**阴性对照**（`ERR-META-32`）：把超时判据去掉必然挂死/报红。
"""
import _bootstrap  # noqa: F401  —— 把 <repo> 与 <repo>/src 放进 sys.path
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                           # noqa: BLE001
    pass

# daemon.py 顶层会 `import config`（要 env）；先喂 dummy env（同项目既有自检做法）。
for _k in ("QB_PASS", "MUSOPIA_COOKIE", "MUSOPIA_PASSKEY", "PICGO_API_KEY",
           "OPENCD_COOKIE", "LSKY_TOKEN"):
    os.environ.setdefault(_k, "x")
os.environ.setdefault("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".scratch", "selftest-data"))

import daemon  # noqa: E402

_OK = 0
_FAIL = 0
_FAILS = []


def chk(label, good, extra=""):
    global _OK, _FAIL
    if good:
        _OK += 1
        print(f"  ✅ {label}" + (f"   {extra}" if extra else ""))
    else:
        _FAIL += 1
        _FAILS.append(label)
        print(f"  ❌ {label}" + (f"   {extra}" if extra else ""))


class _Clock:
    """假单调钟：`sleep(n)` 把 now 推进 n，**不真睡**。"""
    def __init__(self, t0=1000.0):
        self.t = t0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, n):
        self.sleeps.append(n)
        self.t += n


def _probe_seq(seq):
    """按序列依次返回 (ok, detail)；用完后重复最后一个。"""
    state = {"i": 0}

    def _p(host):
        i = state["i"]
        state["i"] += 1
        val = seq[i] if i < len(seq) else seq[-1]
        return val
    return _p


def main():
    print("=" * 66)
    print("#7 qB readiness 闸门 离线单测（零网络、零等待）")
    print("=" * 66)

    # ------------------------------------------------------------------ #
    print("\n① 首次就绪 ⇒ 立即 True、**不 sleep**")
    c = _Clock()
    logs = []
    r = daemon._wait_qb_ready("http://x", timeout=120, interval=5,
                              clock=c.now, sleep=c.sleep,
                              probe=_probe_seq([(True, "v5.2.4")]),
                              log=logs.append)
    chk("返回 True", r is True)
    chk("零 sleep（首次即就绪）", c.sleeps == [], f"sleeps={c.sleeps}")
    chk("未打'未就绪'日志", logs == [], f"logs={logs}")

    # ------------------------------------------------------------------ #
    print("\n② 前 2 次 False、第 3 次 True ⇒ True，sleep 2 次")
    c = _Clock()
    r = daemon._wait_qb_ready("http://x", timeout=120, interval=5,
                              clock=c.now, sleep=c.sleep,
                              probe=_probe_seq([(False, "e1"), (False, "e2"), (True, "v5.2.4")]),
                              log=lambda *a: None)
    chk("返回 True", r is True)
    chk("sleep 恰好 2 次", c.sleeps == [5, 5], f"sleeps={c.sleeps}")

    # ------------------------------------------------------------------ #
    print("\n③ 恒 False ⇒ **超时返回 False**（不 raise），sleep 次数 ≈ timeout/interval")
    c = _Clock()
    logs = []
    r = daemon._wait_qb_ready("http://x", timeout=20, interval=5,
                              clock=c.now, sleep=c.sleep,
                              probe=_probe_seq([(False, "conn refused")]),
                              log=logs.append)
    chk("返回 False（不是异常）", r is False)
    # 时间线：t0 探1(now=1000) → sleep5 → 1005 探2 → sleep5 →1010 探3 →sleep5→1015 探4
    #   →sleep5→1020：此时 clock()>=deadline(1020) ⇒ 在探5前判超时返回
    chk("sleep 次数 = 4（20/5）", len(c.sleeps) == 4, f"sleeps={len(c.sleeps)}")
    chk("打了'未就绪…顺延'日志", any("顺延" in m for m in logs), f"logs={logs}")

    # ------------------------------------------------------------------ #
    print("\n④ 零真实等待（用假 clock ⇒ 总推进 = timeout，而墙钟为 0）")
    import time as _t
    c = _Clock()
    t_wall0 = _t.time()
    daemon._wait_qb_ready("http://x", timeout=120, interval=5,
                          clock=c.now, sleep=c.sleep,
                          probe=_probe_seq([(False, "no")]), log=lambda *a: None)
    wall = _t.time() - t_wall0
    chk("墙钟耗时 < 0.5s（证明没真睡）", wall < 0.5, f"wall={wall:.3f}s")
    chk("假钟推进了 120s", c.t - 1000.0 == 120.0, f"advanced={c.t-1000.0}")

    # ------------------------------------------------------------------ #
    print("\n⑤ ★ 阴性对照：去掉超时判据 ⇒ 必然挂死（用'限量 sleep'保护测出）")
    # 真代码若漏了 `clock()>=deadline` 判定，会无限 sleep。这里用一个"最多允许 3 次
    # sleep 就抛异常"的假 sleep 来**证明**：正常代码在超时处停下，而"无超时判据"的
    # 等价实现会突破 3 次 —— 从而把'超时判据是 load-bearing'这件事测出来。
    class _GuardClock(_Clock):
        def sleep(self, n):
            super().sleep(n)
            if len(self.sleeps) > 5:          # 真实代码最多 4 次；>5 才算"停不下来"
                raise AssertionError("sleep 超过 5 次 ⇒ 超时判据不生效（阴性对照成立）")

    c = _GuardClock()
    try:
        daemon._wait_qb_ready("http://x", timeout=20, interval=5,
                              clock=c.now, sleep=c.sleep,
                              probe=_probe_seq([(False, "no")]), log=lambda *a: None)
        ok_real = (len(c.sleeps) == 4)       # 到点前停
    except AssertionError:
        ok_real = False
    chk("真代码在超时处停住（恰好 4 次 sleep）", ok_real, f"sleeps={len(c.sleeps)}")

    # 反面对照：手写一个"漏了超时判据"的等价实现 ⇒ 必须触发守卫异常
    def _no_timeout_impl(sleep, probe):
        while True:                          # ← 故意漏 `clock()>=deadline`
            ok, _ = probe("http://x")
            if ok:
                return True
            sleep(5)
    c2 = _GuardClock()
    guard_hit = False
    try:
        _no_timeout_impl(c2.sleep, _probe_seq([(False, "no")]))
    except AssertionError:
        guard_hit = True
    chk("★ 漏超时判据的实现必触发守卫 ⇒ 证明判据 non-trivial", guard_hit)

    # ------------------------------------------------------------------ #
    print("\n⑥ _qb_ready_once：200/非200/抛错 三支（注入假 urlopen）")
    # 200 且 body=v5.x
    class _R200:
        status = 200
        def read(self): return b"v5.2.4"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    real_uo = daemon.__dict__.get("__builtins__")
    import urllib.request as _ur
    saved = _ur.urlopen
    try:
        _ur.urlopen = lambda req, timeout=3: _R200()
        ok, det = daemon._qb_ready_once("http://x")
        chk("200 + v5.2.4 ⇒ (True, v5.2.4)", ok and det == "v5.2.4", f"{ok},{det}")

        class _R500:
            status = 500
            def read(self): return b"oops"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        _ur.urlopen = lambda req, timeout=3: _R500()
        ok, det = daemon._qb_ready_once("http://x")
        chk("500 ⇒ (False, ...)", (not ok) and "500" in det, f"{ok},{det}")

        def _boom(req, timeout=3): raise ConnectionRefusedError("refused")
        _ur.urlopen = _boom
        ok, det = daemon._qb_ready_once("http://x")
        chk("抛连不上 ⇒ (False, ConnectionRefusedError...)（不 raise）",
            (not ok) and "ConnectionRefusedError" in det, f"{ok},{det}")
    finally:
        _ur.urlopen = saved

    # ------------------------------------------------------------------ #
    print("\n" + "=" * 66)
    if _FAIL:
        print(f"❌ 失败：{_OK}/{_OK + _FAIL} 项通过；不符：{_FAILS}")
        return 1
    print(f"✅ 全部通过：{_OK}/{_OK + _FAIL} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
