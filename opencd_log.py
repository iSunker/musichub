"""
OpenCD 流水线的日志 —— 要「写全」，但不能「爆表」。

★ 用户要求原文：「相应的日志要写全了，也要防止日志爆表问题。」
  这两条是**互相拉扯**的，所以要显式设计，不能靠"随手 print"。

爆表的真实来源（本项目实测过的形状）：
  1. **每轮重复的摘要**：阶段每 30 分钟跑一次，若每轮都把 50 条明细全打一遍，
     一天 48 轮 × 50 行 = 2400 行**全是重复**。
  2. **大文本**：抓轨日志单条就近 2000 字符（实测 `log_lines` 69~77 行）；
     把 HTML / 日志正文打进日志，一次就是几十 KB。容器 `json-file` 驱动
     虽配了 `max-size: 20m`（`docker-compose.yml`），但那是**兜底**，
     不该靠它来扛：日志一超就**滚动丢弃**，出事时你恰恰看不到前面那段。
  3. **异常回溯刷屏**：一个请求失败若把 `requests` 的完整异常栈打出来，
     配合 48 轮/天就是灾难。

对策（三条，都在这个模块里强制）：
  · `brief()` —— 长文本一律**截断 + 标注原长**，不让正文进日志
  · `Detail` —— 明细**默认只打前 N 条**，其余折成一行「另有 M 条」
  · `summary()` —— 每轮**只打一行**汇总（这才是日常要看的）

★ 写法上刻意**不引入 logging 模块**：本项目其余脚本都用 `print` 到 stdout，
  由 docker 收走。换一套日志框架会让本项目出现两种日志风格（`mdwalk.py` 那条
  「不做会各自漂的拷贝」的同类问题）。这里只加**约束**（截断/折叠），不换机制。
"""
import re

# 单条日志里允许出现的最大字符数（超出截断并标注原长）
# ★ 取 200 是实测拍的：容器日志一行 200 字符在 `docker logs` 里可读、可 grep；
#   而抓轨日志正文是 2000 字符量级 —— 截断后省掉 90% 的日志体积。
MAX_FIELD = 200

# 明细默认打印条数（其余折成一行计数）
DEFAULT_DETAIL_LIMIT = 20


def brief(text, limit=MAX_FIELD):
    """
    把可能很长的文本压成一行可打印的摘要。**长文本绝不能原样进日志。**

    用法：`log(f"  标题={brief(t['title'])}")`
    """
    if text is None:
        return "(None)"
    s = re.sub(r"\s+", " ", str(text)).strip()
    if len(s) <= limit:
        return s
    return f"{s[:limit]}…[+{len(s) - limit}字符]"


def mask_secrets(text):
    """
    兜底脱敏 —— 万一有凭据漏到日志里。
    ★ 与 `tools/doc-audit/04_probes.py` 的 `redact()` 同思路：**按值的形状抹**，
      不只按键名（按 key 名匹配会漏掉嵌在 URL / 异常文本里的凭据）。
    ★ 与 `opencd_sanitize.py` **不合并**：那个是给**样本文件**用的（要落盘、
      规则更细），这个是给**日志出口**用的（要轻、要快、兜底即可）。
      两处职责不同 ⇒ 各自维护是**有意**的，但要互相指路（都提 passkey 形态）。
    """
    s = str(text)
    s = re.sub(r"(passkey=)[0-9a-fA-F]+", r"\1<REDACTED>", s)
    s = re.sub(r"(?i)(c_secure_pass=)[^;\s]+", r"\1<REDACTED>", s)
    s = re.sub(r"(?i)(cookie\s*[:=]\s*)\S+", r"\1<REDACTED>", s)
    s = re.sub(r"(userdetails\.php\?id=)\d+", r"\1<UID>", s)
    s = re.sub(r"(invite\.php\?id=)\d+", r"\1<UID>", s)
    return s


def detail(log, items, render, limit=DEFAULT_DETAIL_LIMIT, more_hint="另有"):
    """
    打印明细，但**只打前 `limit` 条**，其余折成一行计数。

    `render` 是 `item -> str` 的渲染函数（不返回 None 即打印）。
    返回实际打印的条数。
    """
    shown = 0
    for it in items[:limit]:
        line = render(it)
        if line:
            log(line)
            shown += 1
    rest = len(items) - limit
    if rest > 0:
        log(f"    … {more_hint} {rest} 条（明细已折叠，防日志爆表）")
    return shown


def summary(log, stage, **counters):
    """
    阶段汇总 —— ★ **日常只看这一行**，要保证它信息足够、又只有一行。
    """
    parts = " · ".join(f"{k}={v}" for k, v in counters.items() if v is not None)
    log(f"🎯 [{stage}] {parts}")
