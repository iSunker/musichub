"""qBittorrent 客户端的**唯一**构造点 —— 全项目共用这一份（`ERR-QB-08`）。

═══════════════════════════════════════════════════════════════════════════
它解决什么问题
═══════════════════════════════════════════════════════════════════════════
qB 5.x 对**登录接口**（`POST /api/v2/auth/login`）做**失败计数**：用户名/密码
连续错几次就**封 IP**（「身份认证失败次数过多，您的 IP 地址已被封禁」，HTTP 403）。

而本项目原来有 **13 处**各自 `Client(...) + auth_log_in()` —— 每次进程启动、
每次 daemon 扫到待回加、每次跑工具，都在**再试一次登录**。
⇒ 只要密码过期/配错，**重试本身就是把 IP 推向封禁的力量**：
一次**配置错误**（密码不对）被升级成一次**中断**（IP 被封、整个 qB 通路不可用）。
★ 这就是 `ERR-QB-08` 的教训形状：**「重试本身可以是故障的成因」** ——
  对一个**会计数**的端点做重试风暴，只会把小事放大。

★ qB 5.2.0+ 支持 **API Key**（`WebUI\\APIKey`，请求头 `Authorization: Bearer`）——
  它**不走登录接口**、**没有失败计数** ⇒ 配了 key 就**不再会被自己的重试封掉**。

⇒ 本模块把"怎么连 qB"收敛成**一处**：`build(config)`。
  配了 `QB_API_KEY` 就用 key（首选）；没配则**回退**用户名/密码（老行为，一字不改）。

═══════════════════════════════════════════════════════════════════════════
★★ 为什么单独一个模块（而不是塞进 config.py）
═══════════════════════════════════════════════════════════════════════════
`config.py` 被 33 个模块 import，且**在 import 期 `sys.exit`**（`require_env`）。
若把 qB 连接逻辑塞进去，会给 `readd_queue.py` / `db.py` / 各 `--selftest` 入口
引入 `qbittorrentapi` 的 ImportError 风险 —— 而那些入口明确标着
「★ 它**不 import config**（那是要 QB_PASS 的）⇒ 本机/容器里都能跑」，此性质**承重**。
    另：`tools/test_readd_queue.py` 用**只有 4 个属性**的假 config 顶替真 config
    ⇒ 读新键一律用 `getattr(config_mod, "QB_API_KEY", "")`，直读会 `AttributeError`。
本模块只 import `config` + stdlib + **函数内** import `qbittorrentapi`，无环。

═══════════════════════════════════════════════════════════════════════════
★★ 纪律（改了会让别的自检静默失效）
═══════════════════════════════════════════════════════════════════════════
1. **`from qbittorrentapi import Client` 必须在 `build()` 函数体内**，不许提到模块顶。
   理由：`tools/verify_cprime_run.py` 在 `import run` **之前**往 `sys.modules` 塞了
   一个**假** `qbittorrentapi`（`_qb.Client = lambda **kw: _FakeQB()`）⇒ 只有**调用时**
   才 import，才拿得到那个替身。提到模块顶 ⇒ 假替身失效、该回归自检当场崩。
   ★ 而它的症状（"真 Client 被用了" / `ModuleNotFoundError`）**指向的地方与真因相距很远**。
2. **认证那条路不调用任何额外方法**（尤其**不调** `auth_log_in()`）：
   · 调了 = 又去打一次登录接口 = 又进失败计数（本模块存在的全部理由）；
   · 且 `_FakeQB` 只实现 `auth_log_in` / `torrents_info`，多调一个方法它会 `AttributeError`。
3. **API Key 是凭据**：绝不打印（哪怕前 6 位）、绝不进 env 快照
   （`daemon.ENV_SNAPSHOT_KEYS` 是**白名单**制 ⇒ 本键默认不外溢，同 `LSKY_TOKEN` 的纪律）。

═══════════════════════════════════════════════════════════════════════════
自检：`python qb_client.py --selftest`（**不需要 `.env`**）
═══════════════════════════════════════════════════════════════════════════
用假的 `qbittorrentapi`（注入 `sys.modules`）+ 假 config —— **绝不碰真 config**
（真 config 在 import 期就要 `QB_PASS`，本机没 `.env` 会 `sys.exit`）。
"""
import io
import sys


# ★ 哨兵：区分"**没有** QB_API_KEY 属性" 与 "属性存在但为空串"。
#   判据④要的正是前者（`getattr` 兜底不能抛）。
_ABSENT = object()


def build(config_mod=None, api_key=None):
    """建一个可用的 qBittorrent 连接。**连不上就抛**，由调用方按自己的契约处理。

    :param config_mod: 传入 config 模块用于测试注入；默认 `import config`。
      ★ 用参数注入而不是模块顶 `import config`：本模块的 `--selftest` 要能在
        **没有 `.env`** 的机器上跑（真 `import config` 会 `require_env("QB_PASS")` 直接 `sys.exit`）。
    :param api_key: 显式覆盖 API Key（仅本机一次性排查用；写进代码 = 泄露）。
    :return: 已就绪的 `qbittorrentapi.Client`
    :raises ImportError: 没装 `qbittorrentapi`（★ 单独一类 —— `qb_probe.py` 靠它区分
      "没装库" 与 "连不上"，两者在屏幕上应当长得不一样）。
    :raises Exception: 连接 / 鉴权失败（**原样透传**，不吞、不降级）。

    ★★ 为什么默认**抛**而不是返回 `None`：
      全项目 13 个调用点里，**要 `None` 的那批本来就有 `try/except`**（它们的 `except`
      分支更具体：有的打印"本次不做回加"、有的打印"只能依赖库里已有的 content_path"）——
      原样保留即可。返回 `None` 会把"连不上"和"连上了但客户端是空"混成一个值
      （`ERR-SQL-05` 同族：**读不到 != 没有**）。⇒ 不加 `raise_on_fail=False` 之类旋钮，
      那只会造出**第二套失败方言**（`ERR-META-03` 家族）。
    """
    if config_mod is None:
        import config as config_mod

    # ★★★ 函数内导入 —— **不许**提到模块顶（纪律 1；提到顶会毁掉
    #     `tools/verify_cprime_run.py` 的假注入回归，且症状与真因相距很远）。
    from qbittorrentapi import Client

    # ★ `getattr` 而非直读：`tools/test_readd_queue.py` 的假 config 只有 4 个属性。
    key = api_key or getattr(config_mod, "QB_API_KEY", "") or ""

    if key:
        # ---- 首选：API Key（不走登录接口 ⇒ 无失败计数 ⇒ 不会把自己封掉）----
        # ★★ 这条路**不多调任何方法**（纪律 2）：不调 `auth_log_in()`、
        #    不探 `app_version()`。只构造、返回。
        # ★ 不打印 key（纪律 3）。
        return Client(host=config_mod.QB_HOST, api_key=key)

    # ---- 回退：用户名/密码（老行为，一字不改）----
    # ★ 这条**必须留着**：NAS 上还没配 `QB_API_KEY` 的机器、以及 key 轮换前的
    #   过渡期，靠它继续能跑。★ 也是"老路径"唯一的回归面。
    #   ⚠️ 它**会**走登录接口 ⇒ 密码错时**又**会进失败计数（这正是要迁到 key 的理由）。
    qb = Client(host=config_mod.QB_HOST,
                username=getattr(config_mod, "QB_USER", "admin"),
                password=config_mod.QB_PASS)
    qb.auth_log_in()
    return qb


# ==================== 自检（全离线，不需要 .env）====================
#
# ★ 本自检**不 import 真 config**（那要 `QB_PASS`，本机没 `.env` 会 sys.exit）：
#   它用假 config 对象 + 往 `sys.modules` 注入假 `qbittorrentapi`。
# ★ 每条判据都配**阴性对照**（把"能通过的条件"换成"该失败的条件"，证明该判据不是空真）。

class _FakeQB:
    """假 qB 客户端。★ 只实现真项目会用到的那几个方法（镜像 verify_cprime_run 的 _FakeQB）。"""

    def __init__(self, **kw):
        self.kw = kw
        self.calls = []

    def auth_log_in(self):
        self.calls.append("auth_log_in")
        return True


class _RaisingQB(_FakeQB):
    """`auth_log_in` 会抛 —— 用来验证"工厂透传异常、不吞成 None"。"""
    def auth_log_in(self):
        raise RuntimeError("boom-from-fake-login")


class _FakeCfg:
    """假 config。默认**不带** `QB_API_KEY` 属性（判据④要的就是这个）。"""
    QB_HOST = "http://127.0.0.1:3020"
    QB_USER = "admin"
    QB_PASS = "secret-pass"

    def __init__(self, api_key=_ABSENT):
        if api_key is not _ABSENT:
            self.QB_API_KEY = api_key


def _install_fake_lib(record, raising=False):
    """往 `sys.modules` 注入假 `qbittorrentapi`，把每次 `Client(**kw)` 记进 `record`。

    ★ `Client` 刻意写成 `lambda **kw:`（**只吃关键字**）—— 这样"工厂用了位置参数"
      会 `TypeError`，从而把该错暴露出来（同 `verify_cprime_run.py:230`）。
    """
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
    except Exception:                                       # noqa: BLE001
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

    print("\n② key 路径**不调** auth_log_in；回退路径**调**它")
    rec = []
    _install_fake_lib(rec)
    qb_key = build(_FakeCfg(api_key="K"))
    chk("★ key 路径没调 auth_log_in（封禁的根因就在这里）",
        "auth_log_in" not in qb_key.calls, f"calls={qb_key.calls}")
    rec = []
    _install_fake_lib(rec)
    qb_pp = build(_FakeCfg(api_key=""))
    chk("②阴性对照：回退路径**调了** auth_log_in ⇒ 证明 ① 不是空真",
        "auth_log_in" in qb_pp.calls, f"calls={qb_pp.calls}")

    print("\n③ 只传**关键字**参数（假 Client 用 `lambda **kw:`，位置参数会 TypeError）")
    rec = []
    _install_fake_lib(rec)
    try:
        build(_FakeCfg(api_key="K"))
        chk("仅关键字调用（没有 TypeError）", True)
    except TypeError as e:
        chk("仅关键字调用（没有 TypeError）", False, f"{e}")

    print("\n④ 假 config **没有** QB_API_KEY 属性 ⇒ 不 AttributeError（走回退）")
    rec = []
    _install_fake_lib(rec)
    try:
        build(_FakeCfg())                       # 没传 api_key ⇒ 无该属性
        chk("无 QB_API_KEY 属性时不报错、且走回退（kw 含 username）",
            bool(rec) and "username" in rec[0], f"kw={rec}")
    except AttributeError as e:
        chk("无 QB_API_KEY 属性时不报错", False, f"AttributeError: {e}")

    print("\n⑤ 连不上**抛**异常，不返回 None（透传）")
    rec = []
    _install_fake_lib(rec, raising=True)        # 这个假库的 auth_log_in 会抛
    raised, ret = False, "unset"
    try:
        ret = build(_FakeCfg(api_key=""))       # 回退 ⇒ 调 auth_log_in ⇒ 抛
    except RuntimeError:
        raised = True
    chk("auth_log_in 抛 ⇒ build 透传异常（不吞、不返回 None）",
        raised and ret == "unset", f"raised={raised} ret={ret!r}")

    print("\n⑥ 凭据纪律：key 串**不出现**在任何输出里")
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

    # 还原 sys.modules
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
    ap.add_argument("--selftest", action="store_true", help="跑全离线自检（不需要 .env）")
    args = ap.parse_args()
    if args.selftest:
        ok, _ = cmd_selftest()
        return 0 if ok else 1
    print("本模块是库；唯一出口是 `qb_client.build(config)`。加 --selftest 自检。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
