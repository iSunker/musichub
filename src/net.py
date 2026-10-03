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
"""
import os
import socket

# 回退开关。★ 默认 true；只有当某个目标**确实只有 IPv6**时才需要关掉。
FORCE_IPV4 = (os.getenv("NET_FORCE_IPV4", "true").strip().lower()
              not in ("0", "false", "no", "off"))

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

    ★ 现在它与 `requests.Session()` 等价（补丁是**全局**的、对所有 Session 生效）；
      存在的意义是**留一个落点**：将来若要加统一 header / `HTTPAdapter` / `Retry`，
      改这里一处即可，不必去追三份复制粘贴的 Session。
    ★ 纪律：**新写网络代码一律用 `net.new_session()`**，不要再裸写
      `requests.Session()` —— 否则将来有人改了统一层，第四条 Session 会静默漏掉。
    """
    import requests
    return requests.Session()


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
        return True, detail
    finally:
        # 还原成"本模块应有的状态"：若开着开关就保持补丁，否则还原默认。
        if FORCE_IPV4:
            force_ipv4()
        else:
            u3c.allowed_gai_family = saved


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
