"""qBittorrent 客户端统一构造与探活模块。

提供 qBittorrent 客户端的收敛构造入口（`build`）以及就绪状态检测（`wait_qb_ready`）。
支持优先通过 `QB_API_KEY` 鉴权，未配置时自动回退为用户名/密码认证。
"""
import io
import sys

_ABSENT = object()


def build(config_mod=None, api_key=None):
    """构建并返回可用的 qBittorrent 客户端实例。

    :param config_mod: 配置模块对象（默认延迟导入全局 `config`）。
    :param api_key: 显式覆盖 API Key。
    :return: 已初始化的 `qbittorrentapi.Client` 实例。
    :raises ImportError: 未安装 `qbittorrentapi` 依赖。
    :raises Exception: 连接或鉴权失败（原样透传，不吞异常）。
    """
    if config_mod is None:
        import config as config_mod

    # 函数内部延迟导入，以便测试桩注入与无依赖环境下的导入解耦
    from qbittorrentapi import Client

    key = api_key or getattr(config_mod, "QB_API_KEY", "") or ""

    if key:
        # 首选：API Key 鉴权（无需显式登录）
        return Client(host=config_mod.QB_HOST, api_key=key)

    # 回退：用户名/密码鉴权
    qb = Client(
        host=config_mod.QB_HOST,
        username=getattr(config_mod, "QB_USER", "admin"),
        password=config_mod.QB_PASS,
    )
    qb.auth_log_in()
    return qb


def qb_ready_once(host, timeout=3):
    """轻量探活 qB 是否就绪。返回 `(ok, detail)`，不抛出异常。"""
    import urllib.request
    try:
        req = urllib.request.Request(host.rstrip("/") + "/api/v2/app/version")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ver = r.read().decode("utf-8", "replace").strip()
        if getattr(r, "status", 0) == 200 and ver.startswith("v"):
            return True, ver
        return False, f"HTTP {getattr(r, 'status', '?')} ver={ver[:12]!r}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def wait_qb_ready(host, *, timeout, interval, clock=None, sleep=None,
                  probe=None, log=None):
    """等待 qB 就绪，超时返回 False，不抛出异常。"""
    import time as _time
    clock = clock or _time.monotonic
    sleep = sleep or _time.sleep
    probe = probe or qb_ready_once
    if log is None:
        def log(msg):
            print(msg, flush=True)
    deadline = clock() + timeout
    last = ""
    while True:
        ok, detail = probe(host)
        if ok:
            return True
        last = detail
        if clock() >= deadline:
            log(f"ℹ️  qB 未就绪（等了 {timeout}s，最后一次：{last}）⇒ 本轮顺延")
            return False
        sleep(interval)


class _FakeQB:
    """测试替身客户端。"""

    def __init__(self, **kw):
        self.kw = kw
        self.calls = []

    def auth_log_in(self):
        self.calls.append("auth_log_in")
        return True


class _RaisingQB(_FakeQB):
    """抛出异常的测试替身。"""
    def auth_log_in(self):
        raise RuntimeError("boom-from-fake-login")


class _FakeCfg:
    """测试配置桩，默认不包含 QB_API_KEY。"""
    QB_HOST = "http://127.0.0.1:3020"
    QB_USER = "admin"
    QB_PASS = "secret-pass"

    def __init__(self, api_key=_ABSENT):
        if api_key is not _ABSENT:
            self.QB_API_KEY = api_key


def _install_fake_lib(record, raising=False):
    import types

    def _make(**kw):
        record.append(kw)
        return _RaisingQB(**kw) if raising else _FakeQB(**kw)

    mod = types.ModuleType("qbittorrentapi")
    mod.Client = _make
    sys.modules["qbittorrentapi"] = mod
    return mod


def cmd_selftest(verbose=True):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ok = fails = 0

    def chk(label, good, extra=""):
        nonlocal ok, fails
        ok, fails = ok + bool(good), fails + (not bool(good))
        line = f"  {'✅' if good else '❌'} {label}"
        if extra:
            line += f"   {extra}"
        print(line)

    saved_mod = sys.modules.get("qbittorrentapi", None)

    print("① API Key 路径被选中（且不传 username）")
    rec = []
    _install_fake_lib(rec)
    qb = build(_FakeCfg(api_key="KEY-ABC"))
    chk("Client(**kw) 含 api_key='KEY-ABC'",
        bool(rec) and rec[0].get("api_key") == "KEY-ABC", f"kw={rec}")
    chk("Client(**kw) **不含** username（key 路径不该带账号密码）",
        bool(rec) and "username" not in rec[0], f"kw={rec}")

    rec2 = []
    _install_fake_lib(rec2)
    build(_FakeCfg(api_key=""))
    chk("①阴性对照：QB_API_KEY='' ⇒ 走回退、kw 有 username 无 api_key",
        bool(rec2) and ("api_key" not in rec2[0]) and ("username" in rec2[0]),
        f"kw={rec2}")

    print("\n② key 路径不调 auth_log_in；回退路径调它")
    rec = []
    _install_fake_lib(rec)
    qb_key = build(_FakeCfg(api_key="K"))
    chk("key 路径没调 auth_log_in",
        "auth_log_in" not in qb_key.calls, f"calls={qb_key.calls}")
    rec = []
    _install_fake_lib(rec)
    qb_pp = build(_FakeCfg(api_key=""))
    chk("②阴性对照：回退路径调了 auth_log_in",
        "auth_log_in" in qb_pp.calls, f"calls={qb_pp.calls}")

    print("\n③ 只传关键字参数")
    rec = []
    _install_fake_lib(rec)
    try:
        build(_FakeCfg(api_key="K"))
        chk("仅关键字调用（没有 TypeError）", True)
    except TypeError as e:
        chk("仅关键字调用（没有 TypeError）", False, f"{e}")

    print("\n④ 假 config 没有 QB_API_KEY 属性 ⇒ 不抛 AttributeError（走回退）")
    rec = []
    _install_fake_lib(rec)
    try:
        build(_FakeCfg())
        chk("无 QB_API_KEY 属性时不报错、且走回退（kw 含 username）",
            bool(rec) and "username" in rec[0], f"kw={rec}")
    except AttributeError as e:
        chk("无 QB_API_KEY 属性时不报错", False, f"AttributeError: {e}")

    print("\n⑤ 连不上抛异常，不返回 None（透传）")
    rec = []
    _install_fake_lib(rec, raising=True)
    raised, ret = False, "unset"
    try:
        ret = build(_FakeCfg(api_key=""))
    except RuntimeError:
        raised = True
    chk("auth_log_in 抛 ⇒ build 透传异常（不吞、不返回 None）",
        raised and ret == "unset", f"raised={raised} ret={ret!r}")

    print("\n⑥ 凭据纪律：key 串不出现在任何输出里")
    rec = []
    _install_fake_lib(rec)
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        build(_FakeCfg(api_key="SUPER-SECRET-KEY-123"))
    finally:
        sys.stdout = old
    out = buf.getvalue()
    chk("key 串没有被打到 stdout",
        "SUPER-SECRET-KEY-123" not in out, f"stdout={out!r}")

    if saved_mod is not None:
        sys.modules["qbittorrentapi"] = saved_mod
    else:
        sys.modules.pop("qbittorrentapi", None)

    print(f"\n{'=' * 62}")
    if fails:
        print(f"❌ 自检失败：{ok}/{ok + fails} 项通过，{fails} 项不符")
    else:
        print(f"✅ 自检通过：{ok}/{ok + fails} 项")
    return (not fails), fails


def main():
    import argparse
    ap = argparse.ArgumentParser(description="qBittorrent 客户端工厂（qb_client.build）")
    ap.add_argument("--selftest", action="store_true", help="跑全离线自检")
    args = ap.parse_args()
    if args.selftest:
        ok, _ = cmd_selftest()
        return 0 if ok else 1
    print("本模块是库；唯一出口是 `qb_client.build(config)`。加 --selftest 自检。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
