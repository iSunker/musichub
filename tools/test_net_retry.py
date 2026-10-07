"""tools/test_net_retry.py —— #38 的重试改法**离线**单测（零网络、零副作用）。

═══════════════════════════════════════════════════════════════════════════
它测什么（对应 `crabpt超时重试改法-diff-2026-10-06.md` §5 的 1~7）
═══════════════════════════════════════════════════════════════════════════
`net.new_session()` 现在给统一 Session 挂了一个 `HTTPAdapter(max_retries=Retry(...))`：
  · **GET/HEAD** 遇 连接错 / 读超时 / 5xx ⇒ **自动重试**；
  · **POST** —— **永不重试**（`takeupload.php` 不可逆；重试 = 发两条）。

★ 本文件**不真发请求**：用 `urllib3` 的假连接池（count 每次"投递"）来断言
  "到底发了几次" —— 这才是"重试有没有发生"的**可观测**判据。

★ 纪律（本项目钉过）：**每条判据都要配阴性对照**，证明它**能失败**（`ERR-META-32`）。
  最关键的阴性对照是 #7：把 `allowed_methods` 改成**含 POST** ⇒ #3/#4 **必须报红**。

跑法：`PYTHONIOENCODING=utf-8 python tools/test_net_retry.py`（Windows 下必须加前者）。
"""
import _bootstrap  # noqa: F401  —— 把 <repo> 与 <repo>/src 放进 sys.path
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                           # noqa: BLE001
    pass

import urllib3.util.retry as r
from urllib3.util.retry import Retry
import urllib3.exceptions as u3e

import net

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


def _mk(methods, total=3, read=3, status=2):
    """复刻 net.new_session() 里那份 Retry（用来做"发几次"的仿真）。"""
    return Retry(total=total, connect=total, read=read, status=status,
                 status_forcelist=net.RETRY_STATUS_LIST, backoff_factor=0,
                 allowed_methods=methods, respect_retry_after_header=True,
                 raise_on_status=False)


class _FakeResponse:
    """假响应：用**真的** `urllib3.HTTPResponse`（带 status/headers），语义与线上逐字一致。"""
    def __init__(self, status=200):
        import io
        import urllib3
        self.status = status
        self.headers = urllib3.response.HTTPHeaderDict()
        self._r = urllib3.HTTPResponse(body=io.BytesIO(b"x"), status=status,
                                       headers=self.headers, preload_content=False)

    def get_redirect_location(self):
        return False

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


def _simulate(retry, method, outcomes):
    """用假 scripted outcomes 跑一遍 `retry.increment`，返回 (发的次数, 最终status)。

    outcomes：每个元素是 'ok' / 503 / 'read'（读超时）。
    ★ 这是 urllib3 Retry 的**标准用法**：每投递一次调
      `hist = retry.increment(method, url, response=..., error=...)`；
      非幂等方法不消耗任何重试 ⇒ 第二次投递即耗尽 ⇒ 上层不会重发。
    """
    hist = retry.new()                                      # ★ urllib3 2.x: new() 不吃参数
    sent = 0
    for oc in outcomes:
        sent += 1
        err = None
        resp = None
        if oc == "ok":
            return sent, 200                                # 发出去就成功 ⇒ 不再重试
        if oc == "read":
            import urllib3.exceptions as _e
            err = _e.ReadTimeoutError(None, "https://crabpt.vip/x", "read timed out")
        else:
            resp = _FakeResponse(status=oc)
        try:
            hist = retry.increment(method, "https://crabpt.vip/x",
                                   response=resp, error=err)
        except r.MaxRetryError:
            return sent, (resp.status if resp is not None else 0)
        except Exception:                                   # ★ 非幂等方法遇读错：increment 直接 re-raise ⇒ 不重试
            return sent, (resp.status if resp is not None else 0)
        # 这一步没成功：是否还有"下一发"？—— 复刻 urllib3 连接池 `urlopen` 的真实判定：
        #   (a) **方法**必须在白名单内（POST 永不满足 ⇒ 直接从源头断掉不可逆重发）；
        #   (b) 错误可重试：连接错 ⇒ hist.connect>0；读错 ⇒ hist.read>0；状态码 ⇒ is_retry(...)。
        if not hist._is_method_retryable(method):
            return sent, (resp.status if resp is not None else 0)
        retryable = False
        if err is not None:
            if hist._is_connection_error(err):
                retryable = hist.connect > 0
            elif hist._is_read_error(err):
                retryable = hist.read > 0
        else:
            retryable = hist.is_retry(method, resp.status, has_retry_after=False)
        if not retryable or hist.is_exhausted():
            return sent, (resp.status if resp is not None else 0)
    return sent, 200


def main():
    print("=" * 66)
    print("#38 net Retry 离线单测（零网络）")
    print("=" * 66)

    # ------------------------------------------------------------------ #
    print("\n① 常量白名单：只 GET/HEAD，不含 POST")
    chk("net.RETRY_METHODS == {GET, HEAD}", set(net.RETRY_METHODS) == {"GET", "HEAD"},
        f"{sorted(net.RETRY_METHODS)}")
    chk("net.RETRY_METHODS 不含 POST", "POST" not in net.RETRY_METHODS)

    # ------------------------------------------------------------------ #
    print("\n② 真白名单下：GET 遇读超时 ⇒ 重试（#37 病灶）")
    retry = _mk(net.RETRY_METHODS)
    sent, final = _simulate(retry, "GET", ["read", "ok"])
    chk("GET 读超时 1 次后重试、第 2 次成功 ⇒ 共发 2 次", sent == 2 and final == 200,
        f"sent={sent} final={final}")

    print("\n②' GET 遇 503 连两次 ⇒ 重试到第 3 次 200 ⇒ 共发 3 次")
    sent, final = _simulate(_mk(net.RETRY_METHODS), "GET", [503, 503, "ok"])
    chk("GET 503×2 后成功 ⇒ 发 3 次", sent == 3 and final == 200, f"sent={sent} final={final}")

    # ------------------------------------------------------------------ #
    print("\n③ ★★ POST 遇 503 ⇒ **只发 1 次、不重试**（最关键安全判据）")
    sent, final = _simulate(_mk(net.RETRY_METHODS), "POST", [503, "ok", "ok", "ok"])
    chk("POST 503 ⇒ 只发 1 次（绝不重试不可逆上传）", sent == 1, f"sent={sent}")

    print("\n④ ★★ POST 遇读超时 ⇒ **只发 1 次、不重试**")
    sent, final = _simulate(_mk(net.RETRY_METHODS), "POST", ["read", "ok", "ok"])
    chk("POST 读超时 ⇒ 只发 1 次", sent == 1, f"sent={sent}")

    # ------------------------------------------------------------------ #
    print("\n⑤ NET_RETRY=false ⇒ new_session **不挂** Retry（零重试）")
    import importlib, os
    old_env = os.environ.get("NET_RETRY")
    os.environ["NET_RETRY"] = "false"
    try:
        mod2 = importlib.reload(net)
        s = mod2.new_session()
        ad = s.get_adapter("https://crabpt.vip/")
        mt = getattr(getattr(ad, "max_retries", None), "allowed_methods", None)
        # 关掉后应是 urllib3 默认（Retry(0) ⇒ allowed_methods 为 None 或空），不是我们的白名单
        chk("NET_RETRY=false ⇒ 适配器不是我们的重试白名单",
            mt != net.RETRY_METHODS, f"allowed_methods={mt}")
    finally:
        if old_env is None:
            os.environ.pop("NET_RETRY", None)
        else:
            os.environ["NET_RETRY"] = old_env
        importlib.reload(net)                              # 还原成默认开关的模块

    # ------------------------------------------------------------------ #
    print("\n⑥ new_session 真挂了带 Retry 的适配器（且白名单对）")
    s = net.new_session()
    ad = s.get_adapter("https://crabpt.vip/")
    got = getattr(getattr(ad, "max_retries", None), "allowed_methods", None)
    chk("new_session 的 https 适配器白名单 == {GET,HEAD}",
        got is not None and set(got) == {"GET", "HEAD"}, f"allowed_methods={got}")

    # ------------------------------------------------------------------ #
    print("\n⑦ ★★ 阴性对照：白名单含 POST ⇒ ③/④ 必须翻面（证明判据不空真）")
    bad = _mk(frozenset(["GET", "HEAD", "POST"]))
    s1, _ = _simulate(bad, "POST", [503, "ok", "ok", "ok"])
    s2, _ = _simulate(bad, "POST", ["read", "ok", "ok", "ok"])
    chk("含 POST 的白名单下，POST+503 会重试（sent>1）⇒ 反面成立", s1 > 1, f"sent={s1}")
    chk("含 POST 的白名单下，POST+读超时会重试（sent>1）⇒ 反面成立", s2 > 1, f"sent={s2}")

    # ------------------------------------------------------------------ #
    print("\n" + "=" * 66)
    if _FAIL:
        print(f"❌ 失败：{_OK}/{_OK + _FAIL} 项通过；不符：{_FAILS}")
        return 1
    print(f"✅ 全部通过：{_OK}/{_OK + _FAIL} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
