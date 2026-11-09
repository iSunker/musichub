"""统一网络出口：强制 IPv4 解析补丁、HTTP 重试与代理路由管理。

1. 强制 IPv4：通过 patch urllib3.util.connection.allowed_gai_family 避免双栈环境下 IPv6 异常节点握手超时。
2. 幂等 HTTP 重试：对 GET/HEAD 请求提供重试与指数退避，POST 等写操作绝不重试。
3. 可选封面下载代理：支持按需代理外站封面下载，内网与本地站点白名单直连。
"""
import os
import socket

# 强制使用 IPv4 解析（默认开启，设为 0/false/no/off 时关闭）
FORCE_IPV4 = (os.getenv("NET_FORCE_IPV4", "true").strip().lower()
              not in ("0", "false", "no", "off"))

# GET/HEAD 重试配置
RETRY_ENABLED = (os.getenv("NET_RETRY", "true").strip().lower()
                 not in ("0", "false", "no", "off"))
RETRY_TOTAL = int(os.getenv("NET_RETRY_TOTAL", "3"))        # 额外重试次数
RETRY_CONNECT = int(os.getenv("NET_RETRY_CONNECT", "3"))
RETRY_READ = int(os.getenv("NET_RETRY_READ", "3"))          # 读超时重试次数
RETRY_STATUS = int(os.getenv("NET_RETRY_STATUS", "2"))
RETRY_BACKOFF = float(os.getenv("NET_RETRY_BACKOFF", "0.5"))
# 仅针对限流与服务端临时错误重试（429 与 5xx）
RETRY_STATUS_LIST = (429, 500, 502, 503, 504)
# 允许重试的方法白名单，仅限幂等读操作，POST 绝对不参与重试
RETRY_METHODS = frozenset(["GET", "HEAD"])

# 可选外站封面下载代理地址（例如 http://127.0.0.1:7890，空字符串表示直连）
COVER_PROXY = os.getenv("COVER_PROXY", "").strip()
# 不经过代理的主机名或 IP 白名单（逗号分隔）
COVER_NO_PROXY = os.getenv("COVER_NO_PROXY", "").strip()


def proxies_for(url, proxy=None, no_proxy=None):
    """根据目标 URL 解析代理映射字典。若未配置代理或匹配白名单则返回 None（直连）。

    :param url: 目标 URL。
    :param proxy: 显式指定的代理地址，默认读取 COVER_PROXY。
    :param no_proxy: 绕过代理的域名列表（逗号分隔），默认读取 COVER_NO_PROXY。
    :return: requests 兼容的 proxies 字典或 None。
    """
    p = (proxy if proxy is not None else COVER_PROXY) or ""
    p = p.strip()
    if not p:
        return None
    if "://" not in p:
        p = "http://" + p
    np = (no_proxy if no_proxy is not None else COVER_NO_PROXY) or ""
    np = np.strip()
    if np:
        for token in (t.strip() for t in np.split(",")):
            if token and token in (url or ""):
                return None
    return {"http": p, "https": p}


_patched = False


def force_ipv4():
    """给 urllib3 打上仅使用 IPv4 地址解析的补丁。幂等操作，返回当前生效状态。"""
    global _patched
    if not FORCE_IPV4:
        return False
    try:
        import urllib3.util.connection as u3c
    except ImportError:
        return False
    u3c.allowed_gai_family = lambda: socket.AF_INET
    _patched = True
    return True


def new_session():
    """创建统一配置的 requests.Session 实例。

    根据环境配置挂载针对 GET/HEAD 的自动重试适配器，POST 等不可逆写操作不重试。
    """
    import requests
    s = requests.Session()
    if RETRY_ENABLED:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(
            total=RETRY_TOTAL,
            connect=RETRY_CONNECT,
            read=RETRY_READ,
            status=RETRY_STATUS,
            status_forcelist=RETRY_STATUS_LIST,
            backoff_factor=RETRY_BACKOFF,
            allowed_methods=RETRY_METHODS,
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        ad = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        s.mount("https://", ad)
        s.mount("http://", ad)
    return s


# 模块导入时默认激活 IPv4 补丁
force_ipv4()


def selftest():
    """离线自检，验证 force_ipv4、Retry 策略及代理路由逻辑。返回 (ok, [detail])。"""
    detail = []
    try:
        import urllib3.util.connection as u3c
    except ImportError as e:
        return False, [f"★ urllib3 不可用：{e} ⇒ 无法自检"]

    saved = u3c.allowed_gai_family
    try:
        # 对照组：还原默认
        u3c.allowed_gai_family = lambda: socket.AF_UNSPEC
        pre = u3c.allowed_gai_family()
        detail.append(f"补丁前（对照）allowed_gai_family() = {pre}")
        if pre != socket.AF_UNSPEC:
            return False, detail + ["★ 对照失败：还原后不是 AF_UNSPEC ⇒ 自检环境异常"]

        # 实验组：打补丁
        ok = force_ipv4()
        post = u3c.allowed_gai_family()
        detail.append(f"force_ipv4() 返回 {ok}；补丁后 allowed_gai_family() = {post}")
        if not FORCE_IPV4:
            detail.append("★ NET_FORCE_IPV4=false ⇒ 补丁被显式关闭")
            return True, detail
        if post != socket.AF_INET:
            return False, detail + [f"★ 补丁后不是 AF_INET（得到 {post}）⇒ 修复没生效"]

        ok_retry, d_retry = _selftest_retry()
        detail.extend(d_retry)
        if not ok_retry:
            return False, detail

        ok_prox, d_prox = _selftest_proxies()
        detail.extend(d_prox)
        if not ok_prox:
            return False, detail
        return True, detail
    finally:
        if FORCE_IPV4:
            force_ipv4()
        else:
            u3c.allowed_gai_family = saved


def _selftest_retry():
    """验证重试白名单：GET/HEAD 重试，POST 绝不重试。"""
    detail = []
    try:
        from urllib3.util.retry import Retry
    except ImportError as e:
        return False, [f"★ urllib3.util.retry 不可用：{e}"]

    if "POST" in RETRY_METHODS:
        return False, detail + ["★ RETRY_METHODS 含 POST ⇒ 会重试不可逆上传！"]
    detail.append(f"① RETRY_METHODS = {sorted(RETRY_METHODS)}（不含 POST ✅）")

    def _mk(methods):
        return Retry(total=RETRY_TOTAL, connect=RETRY_CONNECT, read=RETRY_READ,
                     status=RETRY_STATUS, status_forcelist=RETRY_STATUS_LIST,
                     backoff_factor=RETRY_BACKOFF, allowed_methods=methods,
                     respect_retry_after_header=True, raise_on_status=False)

    real = _mk(RETRY_METHODS)
    if not real.is_retry("GET", 503):
        return False, detail + ["★ GET+503 竟然不重试 ⇒ Retry 没生效"]
    if real.is_retry("POST", 503):
        return False, detail + ["★ POST+503 被判定可重试 ⇒ 危险！"]
    if real.is_retry("POST", None):
        return False, detail + ["★ POST+连接/读错 被判定可重试 ⇒ 危险！"]
    detail.append("② 真白名单：GET/503=重试、POST/503=不重试、POST/连接错=不重试 ✅")

    bad = _mk(frozenset(["GET", "HEAD", "POST"]))
    if not bad.is_retry("POST", 503):
        return False, detail + ["★ 阴性对照失败：含 POST 的白名单竟也判 POST 不重试"]
    detail.append("③★ 阴性对照：白名单含 POST 时 POST 变可重试 ✅")

    if not RETRY_ENABLED:
        detail.append("④ NET_RETRY=false（当前配置）⇒ new_session 不挂 Retry")
    else:
        import requests as _rq
        s = new_session()
        ad = s.get_adapter("https://crabpt.vip/")
        mt = getattr(getattr(ad, "max_retries", None), "allowed_methods", None)
        if mt is not None and "POST" in mt:
            return False, detail + [f"★ new_session 的 adapter 白名单含 POST：{mt}"]
        raw = _rq.Session().get_adapter("https://crabpt.vip/")
        if getattr(getattr(raw, "max_retries", None), "allowed_methods", None) == RETRY_METHODS:
            detail.append("④ 注意：裸 Session 也带同款白名单（环境异常，非失败）")
        else:
            detail.append("④ new_session 挂了 Retry 适配器（GET/HEAD），裸 Session 没有 ✅")
    return True, detail


def _selftest_proxies():
    """验证代理路由契约：空代理返回 None，补齐 scheme，白名单绕过。"""
    detail = []
    if proxies_for("https://img2.pixhost.to/x.jpg", proxy="") is not None:
        return False, detail + ["★ 空代理竟返回非 None ⇒ 默认不再是直连"]
    detail.append("① 空代理 ⇒ None（直连）✅")

    got = proxies_for("https://img2.pixhost.to/x.jpg", proxy="192.168.0.7:7894")
    want = {"http": "http://192.168.0.7:7894", "https": "http://192.168.0.7:7894"}
    if got != want:
        return False, detail + [f"★ 补 scheme 后不对：{got!r} != {want!r}"]
    detail.append(f"② `192.168.0.7:7894` ⇒ {got} ✅")

    got2 = proxies_for("https://x/y.jpg", proxy="http://192.168.0.7:7894")
    if got2 != want:
        return False, detail + [f"★ 带 scheme 时被改坏：{got2!r}"]
    detail.append("③ 已带 scheme ⇒ 原样 ✅")

    got3 = proxies_for("https://open.cd/attachments/a.jpg",
                       proxy="192.168.0.7:7894", no_proxy="open.cd")
    if got3 is not None:
        return False, detail + [f"★ 例外主机竟走代理：{got3!r}"]
    detail.append("④ `no_proxy` 命中 ⇒ 绕过（None）✅")

    got4 = proxies_for("https://open.cd/attachments/a.jpg", proxy="192.168.0.7:7894")
    if got4 is None:
        return False, detail + ["★ 阴性对照失败：不给 no_proxy 也绕过 ⇒ ④ 是空真"]
    detail.append("⑤ ★ 对照：不给 no_proxy ⇒ 同 URL 走代理 ✅")

    if proxies_for("https://x/y.jpg", proxy=COVER_PROXY) != proxies_for("https://x/y.jpg"):
        return False, detail + ["★ 默认参数没走 COVER_PROXY"]
    detail.append(f"⑥ 默认参数 == COVER_PROXY（当前={COVER_PROXY!r}）✅")
    return True, detail


def _selftest_cli():
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    ok, detail = selftest()
    print(f"net.py 强制 IPv4 自检：{'✅ 通过' if ok else '❌ 失败'}")
    for d in detail:
        print(f"  {d}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _selftest_cli()
