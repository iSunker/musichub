"""tools/test_publish_readd_gate.py —— #49 的 `publish.connect_qb()` readiness 闸门**离线**单测。

═══════════════════════════════════════════════════════════════════════════
它测什么（对应任务 #49 / [7-4]）
═══════════════════════════════════════════════════════════════════════════
`SEED_DELAY_MIN=0`（默认）时，`publish.py` 发布成功后**立即**回加 qB，走的是
`publish.connect_qb()` → `qb_client.build()`。这条**原先没过 #7 闸门**：
qB 没起来（开机后几十秒）时 `build()` 撞 `SSLError port=3020` —— 连接尝试本身
就成了故障期的噪音（`ERR-QB-08` 形状）。

#49 的修法：`connect_qb()` 先 `qb_client.wait_qb_ready(...)`，等不到就绪 ⇒ 返回 None
（**不试 `build()`**）；`readd_to_qb` 的 `qb=None` 分支照旧打印"本次没有可用的 qB 连接"。
闸门**只加不减**：就绪后仍走 `build()`，`build()` 的抛错契约**一字未改**。

★ 判据（每条配**阴性对照**，`ERR-META-32`）：
  1. **就绪** ⇒ `build()` **被调用**、返回那个连接、**不刷新** None；
  2. **未就绪（超时）** ⇒ `build()` **零调用**、返回 None、打"跳过"（**零副作用**）；
  3. ★ **阴性对照**：把手写的"**无闸门**"版 `connect_qb` 拿来跑"未就绪"场景 ⇒
     `build()` **会被调**（⇒ 证明闸门是 load-bearing，不是测了个寂寞）；
  4. 旋钮取自 `config.QB_READY_TIMEOUT/INTERVAL`（可 env 配），缺属性时回退默认；
  5. `build()` 抛错（就绪后仍连不上）⇒ 仍返回 None、打印"连不上"（既有兜底不丢）。

★★ 纪律：本测**零网络、零等待** —— 注入假 `qb_client` 模块 + 假 `wait_qb_ready`，
   `publish.py` 顶层不 import 站点/网络依赖，故可**直接 import**（同 `test_qb_ready.py`）。
   Windows 本机要带 UTF-8：`PYTHONIOENCODING=utf-8 python tools/test_publish_readd_gate.py`
"""
import _bootstrap  # noqa: F401  —— 把 <repo> 与 <repo>/src 放进 sys.path
import os
import sys
import types

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                           # noqa: BLE001
    pass

# config 在 import 期要 env（`require_env`）⇒ 先喂 dummy（同项目既有自检做法）。
for _k in ("QB_PASS", "MUSOPIA_COOKIE", "MUSOPIA_PASSKEY", "PICGO_API_KEY",
           "OPENCD_COOKIE", "LSKY_TOKEN"):
    os.environ.setdefault(_k, "x")
os.environ.setdefault("DATA_DIR", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".scratch", "selftest-data"))

import config  # noqa: E402
import publish  # noqa: E402

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


class _FakeQB:
    """假连接：只作哨兵，证明"返回的是 build() 建的那个"。"""
    def __init__(self, tag="QB"):
        self.tag = tag

    def __repr__(self):
        return f"<FakeQB {self.tag}>"


def _install_fake_qb_client(*, ready=True, build_raises=False):
    """往 `sys.modules` 塞一个假 `qb_client`（顶层 import 走它；`publish` 也函数内 import 它）。

    返回一个 dict，记录调用次数与最近一次 wait 的 kwargs（用于断言旋钮传对）。
    """
    rec = {"wait_calls": 0, "build_calls": 0, "last_wait_kwargs": None,
           "last_wait_host": None, "logs": []}

    def wait_qb_ready(host, *, timeout, interval, clock=None, sleep=None,
                      probe=None, log=None):
        rec["wait_calls"] += 1
        rec["last_wait_host"] = host
        rec["last_wait_kwargs"] = {"timeout": timeout, "interval": interval}
        if log:
            log("ℹ️  qB 未就绪（等了 %ss…）⇒ 本轮顺延" % timeout)
        return ready

    def build(config_mod=None, api_key=None):
        rec["build_calls"] += 1
        if build_raises:
            raise RuntimeError("SSLError: port=3020")
        return _FakeQB()

    mod = types.ModuleType("qb_client")
    mod.wait_qb_ready = wait_qb_ready
    mod.build = build
    mod.qb_ready_once = lambda host, timeout=3: (ready, "v5.2.4" if ready else "conn refused")
    sys.modules["qb_client"] = mod
    return rec


def _with_fake(fn):
    """装上假 qb_client 跑 fn(rec)，跑完**复原** `sys.modules`（别污染后续用例）。"""
    saved = sys.modules.get("qb_client")
    rec = _install_fake_qb_client(**fn._fake_kw)
    try:
        return fn(rec)
    finally:
        if saved is None:
            sys.modules.pop("qb_client", None)
        else:
            sys.modules["qb_client"] = saved


def _fake(**kw):
    def deco(fn):
        fn._fake_kw = kw
        return fn
    return deco


def main():
    print("=" * 66)
    print("#49 publish 回加 readiness 闸门 离线单测（零网络、零等待）")
    print("=" * 66)

    # ------------------------------------------------------------------ #
    print("\n① 就绪 ⇒ build() 被调用、返回那个连接")
    @_fake(ready=True)
    def t1(rec):
        qb = publish.connect_qb()
        chk("返回的是 build() 建的连接", isinstance(qb, _FakeQB) and qb.tag == "QB", f"qb={qb!r}")
        chk("调了 wait 闸门 1 次", rec["wait_calls"] == 1, f"wait={rec['wait_calls']}")
        chk("就绪后调了 build() 1 次", rec["build_calls"] == 1, f"build={rec['build_calls']}")
        chk("wait 的 host = config.QB_HOST", rec["last_wait_host"] == config.QB_HOST,
            f"host={rec['last_wait_host']!r}")
    _with_fake(t1)

    # ------------------------------------------------------------------ #
    print("\n② 未就绪（超时）⇒ build() **零调用**、返回 None、说清原因（零副作用）")
    @_fake(ready=False)
    def t2(rec):
        qb = publish.connect_qb()
        chk("返回 None（跳过本次回加）", qb is None, f"qb={qb!r}")
        chk("★ build() **一次都没调**（不试注定失败的连接）", rec["build_calls"] == 0,
            f"build={rec['build_calls']}")
        chk("wait 闸门被调了 1 次", rec["wait_calls"] == 1, f"wait={rec['wait_calls']}")
    _with_fake(t2)

    # ------------------------------------------------------------------ #
    print("\n③ ★ 阴性对照：**无闸门**版 connect_qb 在未就绪时会去 build() ⇒ 证明闸门 load-bearing")
    @_fake(ready=False)
    def t3(rec):
        # 老实现 = 直接 build()（没有 wait）。用它跑"未就绪"场景。
        def _no_gate_connect_qb():
            import qb_client
            return qb_client.build(config)
        _ = _no_gate_connect_qb()
        chk("★ 无闸门版**确实**调了 build()（反例成立）", rec["build_calls"] == 1,
            f"build={rec['build_calls']}；若为 0 则对比例子没意义")
    _with_fake(t3)
    @_fake(ready=False)
    def t3b(rec):
        publish.connect_qb()
        chk("真实现未就绪 ⇒ build() 0 次", rec["build_calls"] == 0, f"build={rec['build_calls']}")
    _with_fake(t3b)

    # ------------------------------------------------------------------ #
    print("\n④ 旋钮取自 config（QB_READY_TIMEOUT/INTERVAL），缺属性时回退默认")
    @_fake(ready=True)
    def t4(rec):
        publish.connect_qb()
        kw = rec["last_wait_kwargs"] or {}
        chk("timeout 透传 config.QB_READY_TIMEOUT",
            kw.get("timeout") == getattr(config, "QB_READY_TIMEOUT", 120),
            f"timeout={kw.get('timeout')!r} / cfg={getattr(config, 'QB_READY_TIMEOUT', None)!r}")
        chk("interval 透传 config.QB_READY_INTERVAL",
            kw.get("interval") == getattr(config, "QB_READY_INTERVAL", 5),
            f"interval={kw.get('interval')!r} / cfg={getattr(config, 'QB_READY_INTERVAL', None)!r}")
    _with_fake(t4)

    # 缺属性 ⇒ getattr 回退默认（不 AttributeError）
    saved_to = getattr(config, "QB_READY_TIMEOUT", None)
    saved_iv = getattr(config, "QB_READY_INTERVAL", None)
    had_to, had_iv = hasattr(config, "QB_READY_TIMEOUT"), hasattr(config, "QB_READY_INTERVAL")
    try:
        if had_to:
            delattr(config, "QB_READY_TIMEOUT")
        if had_iv:
            delattr(config, "QB_READY_INTERVAL")

        @_fake(ready=True)
        def t4b(rec):
            publish.connect_qb()
            kw = rec["last_wait_kwargs"] or {}
            chk("缺 QB_READY_TIMEOUT ⇒ 回退 120（不抛）", kw.get("timeout") == 120,
                f"timeout={kw.get('timeout')!r}")
            chk("缺 QB_READY_INTERVAL ⇒ 回退 5（不抛）", kw.get("interval") == 5,
                f"interval={kw.get('interval')!r}")
        _with_fake(t4b)
    finally:
        if had_to:
            config.QB_READY_TIMEOUT = saved_to
        if had_iv:
            config.QB_READY_INTERVAL = saved_iv

    # ------------------------------------------------------------------ #
    print("\n⑤ 就绪后 build() 仍抛错 ⇒ 仍返回 None、打印'连不上'（既有兜底不丢）")
    @_fake(ready=True, build_raises=True)
    def t5(rec):
        qb = publish.connect_qb()
        chk("返回 None", qb is None, f"qb={qb!r}")
        chk("build() 被调了（试过了）", rec["build_calls"] == 1, f"build={rec['build_calls']}")
    _with_fake(t5)

    # ------------------------------------------------------------------ #
    print("\n" + "=" * 66)
    if _FAIL:
        print(f"❌ 失败：{_OK}/{_OK + _FAIL} 项通过；不符：{_FAILS}")
        return 1
    print(f"✅ 全部通过：{_OK}/{_OK + _FAIL} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
