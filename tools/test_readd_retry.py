"""`daemon.py` 里 readd 相关的**纯函数**单测 —— 不碰生产 `/data`、不碰 qB、不起线程。

★ 为什么单独测这几个函数（2026-10-04，#24）：
  它们的失效**全是静默的**：
    · `_readd_backoff` 退避算错 ⇒ 要么仍每 30s 重试（刷屏回归）、要么退避成天
      才试一次（回加太慢）；
    · `_readd_should_log` 写宽 ⇒ 一条永久失败的条目又把日志刷满（正是这次的起因）；
      写严 ⇒ 首次失败也静默，人不知道有东西卡了；
    · `_alive_line` 漏阶段 ⇒ 存活行说谎（"看着没在跑"，回到用户误判"容器死了"的老路）。
  ⇒ 把它们钉在纯函数层：判据**必须能失败**（`ERR-META-32`），每条都配"反例"注释。

★ 本文件**不**驱动 `_drain_pending_readds`（那要假 qB + 假 readd_queue，留给集成层）；
  这里只测**判据本身**（退避 / 打印节流 / 存活行），它们是上面那些失效的根源。

★★ 在 Windows 本机跑要带 UTF-8（emoji）：
       PYTHONIOENCODING=utf-8 python tools/test_readd_retry.py
"""
import os
import sys
import tempfile
import types

import _bootstrap  # noqa: E402,F401  —— 把 <repo> 与 <repo>/src 放进 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ★ `daemon.py` 顶部 `import config`（要 QB_PASS 等必填项，本机多半没有）
#   ⇒ 用假 config 顶替，DATA_DIR 指到临时目录。本测试只调纯函数，不碰真实值。
_TMP = tempfile.mkdtemp(prefix="daemon_readd_test_")
_fake = types.ModuleType("config")
_fake.DATA_DIR = _TMP
_fake.QB_HOST = "http://127.0.0.1:3020"
_fake.QB_USER = "admin"
_fake.QB_PASS = "x"
sys.modules["config"] = _fake

import daemon  # noqa: E402

FAILS = []


def check(name, cond, why):
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} —— 反例：{why}")
        FAILS.append(name)


def test_backoff():
    print("[退避] _readd_backoff：5min 起、指数增长、封顶 6h")
    b = daemon._readd_backoff
    check("首次(1) = BASE", b(1) == daemon.READD_RETRY_BASE,
          "★第一次失败后不是最短间隔 ⇒ 与设计不符")
    check("(2) = 2×BASE", b(2) == daemon.READD_RETRY_BASE * 2, "没翻倍")
    check("(3) = 4×BASE", b(3) == daemon.READD_RETRY_BASE * 4, "没再翻倍")
    check("单调不减", all(b(i) <= b(i + 1) for i in range(1, 20)),
          "★退避不是单调增 ⇒ 时快时慢")
    check("封顶 CAP", b(999) == daemon.READD_RETRY_CAP,
          "★不封顶 ⇒ 退避会涨到无穷（实际等于永不重试）")
    # 边界：attempts<=0 不许炸（不该发生，但纯函数要稳）
    try:
        b(0)
        b(-5)
        check("attempts<=0 不炸", True, "")
    except Exception as e:                                      # noqa: BLE001
        check("attempts<=0 不炸", False, f"★抛了 {type(e).__name__} ⇒ 真路径上会中断 drain")


def test_should_log():
    print("[去刷屏] _readd_should_log：首次/里程碑/触顶打，其余不打")
    s = daemon._readd_should_log
    check("首次(1) 打", s(1) is True, "★首次失败静默 ⇒ 人不知道有东西卡了")
    check("(2) 打", s(2) is True, "里程碑缺失")
    # ★ 反例：中间那次**必须静默**，否则就是刷屏回归
    check("(4) 静默", s(4) is False,
          "★连续中间也打 ⇒ 刷屏回归（这正是本轮要修的 bug）")
    check("(6) 静默", s(6) is False, "连续中间也打 ⇒ 刷屏回归")
    check("(8) 打", s(8) is True, "里程碑缺失")
    # 触顶那一次要打（状态变化：间隔不再变）
    cap_first = None
    for a in range(1, 60):
        if daemon._readd_backoff(a) >= daemon.READD_RETRY_CAP:
            cap_first = a
            break
    check(f"触顶({cap_first}) 打", s(cap_first) is True,
          "★退避触顶没打 ⇒ 人不知道它已到最大间隔")
    check(f"触顶后一档({cap_first + 1}) 静默", s(cap_first + 1) is False,
          "触顶后还在每档都打 ⇒ 又刷屏")


def test_alive_line():
    print("[存活行] _alive_line：含每个阶段名 + 各自状态")
    stages = [{"name": "scan", "interval": 1800},
              {"name": "publish", "interval": 3600},
              {"name": "crawl", "interval": 1800}]
    state = {
        "scan": {"last_run": 100, "skip_reason": "未到期：距上次 10s < 周期 1800s",
                 "last_skip_at": "x"},
        "publish": {"last_run": 200, "last_ok": True, "last_took": 3.4},
        "crawl": {},                       # 从没跑过
    }
    line = daemon._alive_line(state, stages, now=1000)
    check("是一行（无换行）", "\n" not in line, "★存活行里有换行 ⇒ tail 时占多行")
    for nm in ("scan", "publish", "crawl"):
        check(f"含阶段名 {nm}", nm in line,
              f"★存活行漏了阶段 {nm} ⇒ 它说谎（看着像没这个阶段）")
    check("跳过阶段带原因", "跳过" in line, "★跳过没体现 ⇒ 看不出'没到期'是正常的")
    check("跑过阶段带 OK+耗时", "OK" in line and "3.4" in line,
          "★跑过的阶段没显示结果/耗时")
    check("未跑过阶段有标记", "未跑过" in line, "★从没跑过的阶段没区分出来")


def main():
    test_backoff()
    test_should_log()
    test_alive_line()
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败：{FAILS}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
