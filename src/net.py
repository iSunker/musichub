"""
统一网络出口：**强制 IPv4 解析**，绕开"个别 Cloudflare IPv6 边缘 IP 不可达"的黑洞。

═══════════════════════════════════════════════════════════════════════════
为什么需要这个模块（2026-10-02 实测定位，别再删掉）
═══════════════════════════════════════════════════════════════════════════

症状（一直存在、且被误归因过）：
  · `cover_fetch.download()` 拉外站封面图超时（T1）；
  · `assets.upload_to_lsky()` 传蟹黄堡图床超时（T3）；
  · `musopia.MusopiaClient` 打乌托邦站点**间歇** ReadTimeout（T5）。
  三者共同点是**目标域名都在 Cloudflare 后面**。

★ 历史上记成了"IPv6 出网黑洞"，**这个归因是错的**（2026-10-02 证伪）：
  NAS 宿主 `curl -6 https://img.crabpt.vip/` **能通**（404，<1s）⇒ IPv6 整族没问题。

真根因（本轮逐 IP 直连 TLS 实测）：

  域名                  IPv6                                       IPv4
  ---------------       --------------------------------------    --------------
  img.crabpt.vip        个别 v6 IP 超时（同域不同 IP 表现不同）   ✅ ~0.4s
  origin.picgo.net      ✅ 可用（所以 T1 时好时坏）               ✅ 可用
  www.musopia.vip       两个 v6 全超时                            ✅ 可用

  即：**个别 Cloudflare 边缘 IPv6 单点不可达**（TCP connect 能过——内核层，
  但 TLS 握手挂到超时 ⇒ 典型"黑洞"），**不是整族不通**。

  ★ 为什么 curl 看起来"能通"、Python 却"全挂"：
    - `socket.getaddrinfo` 返回 **IPv6 排在 IPv4 之前**（实测，宿主与容器一致）；
    - `curl` 有 **Happy Eyeballs**（`--happy-eyeballs-timeout-ms`）⇒ 快速回落 v4；
    - Python `requests` → `urllib3` **取返回列表第一条、不做竞速**
      ⇒ 直接选中那个坏 v6 IP，握手挂到超时。
  ⇒ 差异在**选地址策略**，不在网络栈（容器是本项目 `network_mode: host`，
    与宿主共用网络栈，实测两侧行为**逐字一致**）。

═══════════════════════════════════════════════════════════════════════════
修法：全局 monkey-patch urllib3 的地址族选择
═══════════════════════════════════════════════════════════════════════════

  `urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET`

★ 为什么 patch `allowed_gai_family` 而**不是** `socket.getaddrinfo`：
  前者只影响 urllib3（= `requests` 的地址选择），最小侵入；改 `getaddrinfo`
  会波及**整个进程**里所有库的 IPv6 用法（含 `qbittorrentapi` 等），代价过大。
★ 为什么放在**模块 import 时**做（模块级副作用）：
  `requests` → `urllib3` 在**每次连接**时读 `allowed_gai_family()`（不是 import 时读），
  所以只要补丁在"第一次发请求之前"生效即可。放在 import 时最省事、最不会漏。
★ 为什么由 `config.py` `import net`（而不是每个使用者各加一行）：
  `config` 是纯叶子（只 import os/sys）、**全项目 33 处 import 它**、
  且已有模块级副作用先例（`os.makedirs`）⇒ 一处生效、覆盖全部网络代码。
  ★ 本模块**不 import config、不读任何 env** ⇒ 自检路径（`--selftest` 那套
    `os.environ.setdefault` 的 dummy-env hack）**完全不受影响**。

★ 回退开关：`NET_FORCE_IPV4=false` ⇒ 不打补丁（用于对照实验或某个目标只有 IPv6 时）。
  默认 `true`。

═══════════════════════════════════════════════════════════════════════════
第二件事：**GET/HEAD 的重试**（2026-10-06，`#38`）
═══════════════════════════════════════════════════════════════════════════
上面那件事治的是"**选错了地址**"。但"**选对了地址、那个 Cloudflare 边缘却慢到
>timeout**"是另一回事：`crabpt.vip` 的 GET 在 `timeout=30` 上间歇 `ReadTimeout`，
而全项目**零重试** ⇒ 一次超时就把整阶段判 `exit 1`（见 `#37` 的复现）。
落点就是本模块 —— 文件头早就写着"将来若要加 `Retry`，改这里一处即可"。

  ★ **只对幂等方法（GET/HEAD）重试**，**绝不重试 POST**：
    `takeupload.php` 上传**不可逆**、且"超时 ≠ 失败"（可能已经发出）⇒
    重试 POST = **发两条**。`urllib3.Retry(allowed_methods=frozenset(["GET","HEAD"]))`
    对**不在白名单**的方法**直接放行、不重试** ⇒ 从**机制上**杜绝（不是靠自觉）。

★ 回退开关：`NET_RETRY=false` ⇒ 不挂 Retry（对照实验 / 排查用）。默认 `true`。
"""
import os
import socket

# 回退开关。★ 默认 true；只有当某个目标**确实只有 IPv6**时才需要关掉。
FORCE_IPV4 = (os.getenv("NET_FORCE_IPV4", "true").strip().lower()
              not in ("0", "false", "no", "off"))

# ★★ 2026-10-06（#38）：GET/HEAD 的重试策略（见文件头「第二件事」）。
#   ★ 全部 env 可覆盖，便于"改了不用重构建"（与 FORCE_IPV4 同款）。
RETRY_ENABLED = (os.getenv("NET_RETRY", "true").strip().lower()
                 not in ("0", "false", "no", "off"))
RETRY_TOTAL = int(os.getenv("NET_RETRY_TOTAL", "3"))        # 额外重试次数
RETRY_CONNECT = int(os.getenv("NET_RETRY_CONNECT", "3"))
RETRY_READ = int(os.getenv("NET_RETRY_READ", "3"))          # ★ 读超时重试（#37 病灶）
RETRY_STATUS = int(os.getenv("NET_RETRY_STATUS", "2"))
RETRY_BACKOFF = float(os.getenv("NET_RETRY_BACKOFF", "0.5"))
# ★ 只重试"确定可再试"的状态码：429(限流) + 5xx（服务端临时错）。
#   ★ 不重试 4xx 业务错（403/404/401）—— 那是**确定性**的，重试白敲站点（还可能被限）。
RETRY_STATUS_LIST = (429, 500, 502, 503, 504)
# ★ 允许重试的方法白名单。POST **永不**在列（不可逆上传的保护）。
RETRY_METHODS = frozenset(["GET", "HEAD"])

# ★★ 2026-10-11（#15）：**可选出网代理**。
#   起因：皇后封面**约一半是外站图**（`img2.pixhost.to` 等），对本 NAS 出口 IP
#     **间歇 reset/超时**（`ERR-SITE-*` 记过）；而 NAS 上恰有一个可用代理
#     `192.168.0.7:7894`（实测：经它取 pixhost 封面 673KB~1.7MB 全成功）。
#   ★ 只管**外站封面下载**这一条路（`cover_fetch.download` 的 external 分支）——
#     不碰皇后站内请求、不碰上传（POST）、不碰 qB。**默认空 = 直连**（行为零变化）。
#   ★ 取"代理地址"（可带或不带 `http://`）与"例外列表"（不经代理的网段/域名，逗号分隔，
#     走 `requests` 的 `no_proxy`）。★ 值是**地址不是凭据**，可打印；但仍只在日志里印一次。
COVER_PROXY = os.getenv("COVER_PROXY", "").strip()
# ★ 例外：这些主机**不走代理**（如内网/站点自身）。默认空 ⇒ 全部走代理（若配了代理）。
COVER_NO_PROXY = os.getenv("COVER_NO_PROXY", "").strip()


def proxies_for(url, proxy=None, no_proxy=None):
    """给 `requests` 用的 `proxies` dict —— **没配代理就返回 `None`（直连）**。

    :param url: 要访问的 URL（用来决定是否落进 `no_proxy` 例外）。
    :param proxy: 代理地址；默认读本模块的 `COVER_PROXY`。空 ⇒ 返回 `None`。
    :param no_proxy: 例外列表（逗号分隔）；默认读 `COVER_NO_PROXY`。
    :return: `{"http": p, "https": p}` 或 `None`。

    ★ 为什么返回 `None` 而不是 `{}`：`requests` 对 `proxies=None` 与 `proxies={}` 都当直连，
      但 `{}` 会**覆盖**掉环境里的 `HTTP_PROXY`（如果有）。返回 `None` 才是**诚实的直连**
      （不静默改环境语义）—— 同 `ERR-META-06`（别用"看起来一样"的值顶替语义不同的值）。
    ★ 代理地址**自动补 `http://`**（用户常只给 `host:port`）：`requests` 认 scheme，
      少了会报 `MissingSchema`。补 scheme 只影响"地址长啥样"，不改语义。
    ★ `no_proxy` 用**子串**判据（`requests` 自己的 `should_bypass_proxies` 也是子串/后缀匹配）；
      这里图简单只做"URL 里含该串就绕过"，够用（例外一般写域名/网段）。
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


# 幂等标记：同一个进程里被 import 多次也只打一次补丁（也便于自检断言"补丁已生效"）。
_patched = False


def force_ipv4():
    """给 urllib3 打上"只解析 IPv4"的补丁。**幂等**，返回是否处于已生效状态。

    ★ 用函数包起来而不是裸写模块级语句，是为了让自检能显式调用它做**阴性对照**
      （先还原成默认、断言补丁前是 AF_UNSPEC，再调用本函数、断言变成 AF_INET）——
      遵守本项目纪律：**判据必须能失败**（ERR-META-32），不能只是打印一下。
    """
    global _patched
    if not FORCE_IPV4:
        return False
    try:
        import urllib3.util.connection as u3c
    except ImportError:
        # urllib3 不在（例如某些极简自检环境）⇒ 静默跳过，不算失败。
        return False
    u3c.allowed_gai_family = lambda: socket.AF_INET
    _patched = True
    return True


def new_session():
    """创建一个统一的 `requests.Session()`。

    ★★ 2026-10-06（#38）：挂 **Retry** —— 对 **GET/HEAD** 的
      **连接错 / 读超时 / 5xx** 自动重试（指数退避）；**POST 永不重试**（不可逆）。
    ★ 为什么只重试 GET/HEAD：`crabpt_site`/`musopia` 的 `takeupload.php` 是**不可逆上传**，
      超时**不等于**失败（可能已发出）⇒ 重试 = 发两条。`Retry(allowed_methods=...)`
      对**不在白名单**的方法**直接放行、不重试** ⇒ 从机制上杜绝。
    ★ 为什么放这里：三客户端（musopia/crabpt_site/crawl）**都**走 `net.new_session()`
      ⇒ 一处生效、覆盖 T1/T3/T5 三族（封面/图床/站点）超时（`ERR-META-03`）。
    ★ 回退开关 `NET_RETRY=false` ⇒ 不挂 Retry（对照实验 / 排查用），默认开。
    ★ 纪律：**新写网络代码一律用 `net.new_session()`**，不要再裸写
      `requests.Session()` —— 否则将来有人改了统一层，第四条 Session 会静默漏掉。
    """
    import requests
    s = requests.Session()
    if RETRY_ENABLED:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(
            total=RETRY_TOTAL,                 # 额外重试次数
            connect=RETRY_CONNECT,             # 连接阶段重试
            read=RETRY_READ,                   # ★ 读超时重试（#37 的直接病灶）
            status=RETRY_STATUS,               # 5xx 等状态码重试
            status_forcelist=RETRY_STATUS_LIST,
            backoff_factor=RETRY_BACKOFF,      # 0.5 → 1 → 2 → 4 s 指数退避
            allowed_methods=RETRY_METHODS,     # ★ 只 GET/HEAD —— 绝不含 POST
            respect_retry_after_header=True,
            raise_on_status=False,             # 重试耗尽仍返回响应，交给上层判（不新造异常方言）
        )
        ad = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        s.mount("https://", ad)
        s.mount("http://", ad)
    return s


# ★ 模块 import 时立即生效（见文件头「为什么放在模块 import 时做」）。
force_ipv4()


# ==================== 自检 ====================
def selftest():
    """证明补丁**真的**把地址族改成了 IPv4，且**这条判据能失败**。

    返回 `(ok, [明细...])`。★ 关键：做**阴性对照** —— 先还原 urllib3 的默认，
    断言此时是 `AF_UNSPEC`；再调用 `force_ipv4()`，断言变为 `AF_INET`。
    只有这样，"看到 AF_INET"才是一个**能失败的**判据，而不是恒真的打印。
    """
    detail = []
    try:
        import urllib3.util.connection as u3c
    except ImportError as e:                                # noqa: BLE001
        return False, [f"★ urllib3 不可用：{e} ⇒ 无法自检"]

    saved = u3c.allowed_gai_family
    try:
        # 阴性对照①：还原默认 ⇒ 必须是 AF_UNSPEC（Python 默认行为）
        u3c.allowed_gai_family = lambda: socket.AF_UNSPEC
        pre = u3c.allowed_gai_family()
        detail.append(f"补丁前（对照）allowed_gai_family() = {pre}")
        if pre != socket.AF_UNSPEC:
            return False, detail + ["★ 对照失败：还原后不是 AF_UNSPEC ⇒ 自检环境异常"]

        # 正样本：打补丁 ⇒ 必须是 AF_INET
        ok = force_ipv4()
        post = u3c.allowed_gai_family()
        detail.append(f"force_ipv4() 返回 {ok}；补丁后 allowed_gai_family() = {post}")
        if not FORCE_IPV4:
            detail.append("★ NET_FORCE_IPV4=false ⇒ 补丁被显式关闭（这是配置，不是失败）")
            return True, detail
        if post != socket.AF_INET:
            return False, detail + [f"★ 补丁后不是 AF_INET（得到 {post}）⇒ 修复没生效"]
        # ★ 顺带验 #38 的 Retry：GET 在白名单、POST 不在（安全侧）。
        ok_retry, d_retry = _selftest_retry()
        detail.extend(d_retry)
        if not ok_retry:
            return False, detail
        # ★ 顺带验 #15 的代理：没配=直连(None)、配了=补 scheme、例外=绕过。
        ok_prox, d_prox = _selftest_proxies()
        detail.extend(d_prox)
        if not ok_prox:
            return False, detail
        return True, detail
    finally:
        # 还原成"本模块应有的状态"：若开着开关就保持补丁，否则还原默认。
        if FORCE_IPV4:
            force_ipv4()
        else:
            u3c.allowed_gai_family = saved


def _selftest_retry():
    """#38 的判据：**只重试 GET/HEAD、绝不含 POST**（且这条判据**能失败**）。

    ★ 关键安全判据：把 `allowed_methods` 换成"含 POST"必须**报红** ——
      即证明"不重试 POST"是被**测出来**的，而不是空真（`ERR-META-32`）。
    """
    detail = []
    try:
        from urllib3.util.retry import Retry
    except ImportError as e:                                # noqa: BLE001
        return False, [f"★ urllib3.util.retry 不可用：{e}"]

    # ① 常量白名单本身不含 POST
    if "POST" in RETRY_METHODS:
        return False, detail + ["★ RETRY_METHODS 含 POST ⇒ 会重试不可逆上传！"]
    detail.append(f"① RETRY_METHODS = {sorted(RETRY_METHODS)}（不含 POST ✅）")

    # ② 用与 new_session 相同的参数构造 Retry，断言：GET 会重试、POST 不会。
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
    if real.is_retry("POST", None):        # 无 status（=连接/读错）也不该重试 POST
        return False, detail + ["★ POST+连接/读错 被判定可重试 ⇒ 危险！"]
    detail.append("② 真白名单：GET/503=重试、POST/503=不重试、POST/连接错=不重试 ✅")

    # ③★ 阴性对照：把 POST 加进白名单 ⇒ 上面那条"不该重试"的断言**必须**翻面。
    bad = _mk(frozenset(["GET", "HEAD", "POST"]))
    if not bad.is_retry("POST", 503):
        return False, detail + ["★ 阴性对照失败：含 POST 的白名单竟也判 POST 不重试 ⇒ 判据是空真"]
    detail.append("③★ 阴性对照：白名单含 POST 时 POST 变可重试 ⇒ 证明判据②不是空真 ✅")

    # ④ NET_RETRY=false ⇒ new_session 不应挂 adapter（用假 requests 验，零网络）。
    if not RETRY_ENABLED:
        detail.append("④ NET_RETRY=false（当前配置）⇒ new_session 不挂 Retry；这是配置不是失败")
    else:
        import requests as _rq
        s = new_session()
        ad = s.get_adapter("https://crabpt.vip/")
        mt = getattr(getattr(ad, "max_retries", None), "allowed_methods", None)
        if mt is not None and "POST" in mt:
            return False, detail + [f"★ new_session 的 adapter 白名单含 POST：{mt}"]
        # 阴性对照：裸 requests.Session() 的 adapter 没有我们的 Retry（allowed_methods 非本白名单）
        raw = _rq.Session().get_adapter("https://crabpt.vip/")
        if getattr(getattr(raw, "max_retries", None), "allowed_methods", None) == RETRY_METHODS:
            detail.append("④ 注意：裸 Session 也带同款白名单（环境异常，非失败）")
        else:
            detail.append("④ new_session 挂了 Retry 适配器（GET/HEAD），裸 Session 没有 ✅")
    return True, detail


def _selftest_proxies():
    """#15 的判据：**没配代理 ⇒ 直连(`None`)**；配了 ⇒ 补 scheme；例外 ⇒ 绕过。带阴性对照。

    ★ 关键判据（能失败）：`proxies_for(url, proxy="")` **必须**返回 `None`（不是 `{}`）——
      若哪天有人把它改成 `return {}`，这条会红（`ERR-META-32`：判据必须能失败）。
    """
    detail = []
    # ① 空代理 ⇒ None（直连）。★ 这是**生产默认**，必须钉死。
    if proxies_for("https://img2.pixhost.to/x.jpg", proxy="") is not None:
        return False, detail + ["★ 空代理竟返回非 None ⇒ 默认不再是直连（危险）"]
    detail.append("① 空代理 ⇒ None（直连）✅")

    # ② 配了 host:port（无 scheme）⇒ 自动补 http://
    got = proxies_for("https://img2.pixhost.to/x.jpg", proxy="192.168.0.7:7894")
    want = {"http": "http://192.168.0.7:7894", "https": "http://192.168.0.7:7894"}
    if got != want:
        return False, detail + [f"★ 补 scheme 后不对：{got!r} != {want!r}"]
    detail.append(f"② `192.168.0.7:7894` ⇒ {got} ✅")

    # ③ 已带 scheme ⇒ 原样（不重复补）
    got2 = proxies_for("https://x/y.jpg", proxy="http://192.168.0.7:7894")
    if got2 != want:
        return False, detail + [f"★ 带 scheme 时被改坏：{got2!r}"]
    detail.append("③ 已带 scheme ⇒ 原样 ✅")

    # ④ 例外列表：URL 含例外串 ⇒ None（绕过代理）。
    got3 = proxies_for("https://open.cd/attachments/a.jpg",
                       proxy="192.168.0.7:7894", no_proxy="open.cd")
    if got3 is not None:
        return False, detail + [f"★ 例外主机竟走代理：{got3!r}"]
    detail.append("④ `no_proxy` 命中 ⇒ 绕过（None）✅")

    # ⑤ 阴性对照：**不**给例外 ⇒ 同一 URL 走代理（证明④不是空真）。
    got4 = proxies_for("https://open.cd/attachments/a.jpg", proxy="192.168.0.7:7894")
    if got4 is None:
        return False, detail + ["★ 阴性对照失败：不给 no_proxy 也绕过 ⇒ ④ 是空真"]
    detail.append("⑤ ★ 对照：不给 no_proxy ⇒ 同 URL 走代理 ✅")

    # ⑥ 默认参数走模块级 COVER_PROXY（当前 env）——只断言"与显式传该值一致"，不假设配没配。
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
