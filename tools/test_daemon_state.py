"""★ 回归测试：`daemon_state.json` 必须能分辨「跑了」和「被跳过」。

═══════════════════════════════════════════════════════════════════════════
它测什么（一句话）
═══════════════════════════════════════════════════════════════════════════
**一轮里没被跑到的阶段，不许在盘上留下"我跑过"的痕迹。**

真实起因（2026-09-27 实测，不是假想）：
  容器 22:27:54 重建，`RUN_ON_START=false` ⇒ 首轮只有 `crawl` 到期
  （`now - last_run = 1824s > 1800`），其余 6 个阶段全部 `continue`。
  但盘上 `daemon_state.json` 里 **`edit.last_finish = 22:27:06`** ——
  而我据此把它读成了「22:27:06 跑过 edit」。**读错了。**

  根因：`last_run` 与 `last_finish` 在**同一行、同一时刻**写入，
  而跳过的阶段走的是**纯 `continue`**（既不写、也不留痕迹）。
  ⇒ 于是这两种完全不同的情形在盘上**长得一模一样**：
        (A) 这一轮真的跑了 edit      → last_finish = T
        (B) 这一轮 edit 被跳过       → last_finish = 上一轮的 T（没动）
  要分辨只能靠"我记不记得容器启动时刻" —— 那是**脑内状态**，不是判据。

  ★ 这是 `ERR-META-01` 家族：**一个看起来对、但测的不是它声称的东西的判据**。
    观测手段分辨不出的两件事，在盘上就变成了同一件事。

═══════════════════════════════════════════════════════════════════════════
改动后的判据（本文件钉住的就是它）
═══════════════════════════════════════════════════════════════════════════
① **`last_finish` 只在真跑了的分支里写** ⇒ 没跑过 ⇒ **不会出现**
② **跳过时写 `last_skip_at` + `skip_reason`**（不动 `last_run`/`last_finish`）
③ **跳过支必须真的落盘** —— ★ 这是本测试存在的**第二个**理由：
   我第一版改动就死在这里 —— `save_state()` 原先只在"真跑了阶段之后"
   调用，跳过支 `continue` 了就**永远走不到** ⇒ 我写进内存的 `last_skip_at`
   **从不落盘**，加了等于没加。所以断言必须落在**文件内容**上，
   **不能**落在"我写了这行代码"上。
④ **退出码/耗时落盘**（`last_code`/`last_took`）——
   `topup_opencd_fields.py` 的"今日额度用完"是**退 0 的正常收工**，
   与"真抓成功若干条"在盘上原本同形（都 `last_ok=true`、日志都干净）。

═══════════════════════════════════════════════════════════════════════════
怎么测（不碰生产库、不碰站点、不出网）
═══════════════════════════════════════════════════════════════════════════
把 `daemon` 的 `STATE_FILE` 指向临时文件，**直接把它的主循环跑起来**，
用两个假阶段（一个"该跑"、一个"没到期"）驱动，然后**读回文件**。
★ 不 mock `save_state` —— 要让"落盘"这件事**真的发生**，
  否则又变成"测的是我以为的代码路径"。

    python tools/test_daemon_state.py
"""
import json
import os
import sys
import tempfile
import time

# ★ 本机 Windows 控制台是 GBK ⇒ 不 reconfigure 的话，daemon 打印的 emoji
#   （`👋 编排器已退出。`）会抛 UnicodeEncodeError，把测试带崩。
#   这与生产无关（容器里是 UTF-8），纯本机跑测试的必需品。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - 老 Python 没有 reconfigure，忽略即可
    pass

import _bootstrap  # noqa: E402,F401  —— 把 <repo> 与 <repo>/src 都放进 sys.path（源码已移进 src/）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_fail = 0
_total = 0


def chk(label, cond, detail=""):
    global _fail, _total
    _total += 1
    if cond:
        print(f"  ✅ {label}")
    else:
        _fail += 1
        print(f"  ❌ {label}" + (f"   ← {detail}" if detail else ""))


def _run_one_round(daemon, stages, state):
    """把 daemon 主循环**跑一轮**（不睡、不真起子进程）。

    ★ 用 `run_stage` 的**替身**只替换"起进程"这一步，其余（判到期、
      写 state、落盘）全走**真代码** —— 那才是要测的东西。
    """
    calls = []

    def fake_run_stage(stage, timeout):
        calls.append(stage["name"])
        return {"ok": True, "code": 0, "took": 1.5,
                "started_at": "2026-09-27T22:00:00"}

    orig_run, orig_sleep, orig_beat = daemon.run_stage, daemon.sleep_interruptible, daemon.beat
    orig_snap = daemon.save_env_snapshot
    # ★ 让主循环只跑一轮：sleep 时置 _shutdown
    def stop_after_one(_seconds):
        daemon._shutdown = True

    daemon.run_stage = fake_run_stage
    daemon.sleep_interruptible = stop_after_one
    daemon.beat = lambda: None
    daemon.save_env_snapshot = lambda: None
    daemon._shutdown = False
    try:
        daemon.run_main_loop(stages, state, max_backoff=21600)
    finally:
        daemon.run_stage, daemon.sleep_interruptible, daemon.beat = orig_run, orig_sleep, orig_beat
        daemon.save_env_snapshot = orig_snap
        daemon._shutdown = False
    return calls


def main():
    import daemon

    print("=" * 72)
    print("★ daemon_state.json：「跑了」与「被跳过」必须分得开")
    print("=" * 72)

    tmpdir = tempfile.mkdtemp(prefix="daemon_state_test_")
    state_path = os.path.join(tmpdir, "daemon_state.json")
    orig_state_file = daemon.STATE_FILE
    daemon.STATE_FILE = state_path

    try:
        now = time.time()
        # 阶段 A：**已到期**（last_run 很旧）⇒ 这轮该跑
        # 阶段 B：**没到期**（last_run 就是刚才）⇒ 这轮该跳过
        stages = [
            {"name": "due", "cmd": ["echo"], "interval": 1800},
            {"name": "fresh", "cmd": ["echo"], "interval": 1800},
        ]
        state = {
            "due": {"last_run": now - 99999, "last_ok": True,
                    "last_finish": "2020-01-01T00:00:00"},
            "fresh": {"last_run": now - 5, "last_ok": True,
                      "last_finish": "2026-09-27T22:27:06"},
        }
        # 先把夹具写盘，让 load_state 有东西可读（模拟真实的"上一轮留下的"）
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        calls = _run_one_round(daemon, stages, state)

        # ── ① 该跑的跑了、该跳的跳了 ───────────────────────────────
        print()
        print("① 调度本身：到期的跑、没到期的跳")
        chk("到期的阶段被真的调用了", calls == ["due"], f"实得 {calls}")

        # ★★★ 核心：**读回文件**，不看内存
        with open(state_path, "r", encoding="utf-8") as f:
            disk = json.load(f)

        # ── ② 没跑过的阶段不许留下"我跑过"的痕迹 ──────────────────
        print()
        print("② ★★ 没跑过的阶段：`last_finish` 不许被刷新（本测试的**核心**）")
        chk("fresh.last_finish **原样未动** —— 仍是上一轮那个时刻",
            disk["fresh"]["last_finish"] == "2026-09-27T22:27:06",
            f"实得 {disk['fresh']['last_finish']!r}  "
            f"（★ 若变了，说明跳过支在**冒充**自己跑过 —— 这正是本次要修的误读）")
        chk("fresh.last_run 也没被动过",
            abs(disk["fresh"]["last_run"] - (now - 5)) < 1,
            f"实得 {disk['fresh']['last_run']}")

        # ── ③ 跳过必须**落盘**（我第一版改动死在这里） ──────────────
        print()
        print("③ ★★ 跳过要**落盘**（不是只写内存）—— 我第一版就死在这一条")
        chk("fresh 有 last_skip_at ⇒ 跳过这件事**真的写进了文件**",
            "last_skip_at" in disk.get("fresh", {}),
            "★ 缺这一条 = 跳过支 continue 了、走不到 save_state ⇒ 加了等于没加")
        chk("fresh 的 skip_reason 说明了没到期（含『未到期』字样）",
            "未到期" in (disk["fresh"].get("skip_reason") or ""),
            f"实得 {disk['fresh'].get('skip_reason')!r}")
        chk("skip_reason **没有**替调度器猜原因（不许出现『配额』）",
            "配额" not in (disk["fresh"].get("skip_reason") or ""),
            "★ 配额是 crawl 阶段内部的事，调度器无从知道（ERR-META-14）")

        # ── ④ 真跑了的那条：痕迹要齐，且不残留"跳过"标记 ────────────
        print()
        print("④ 真跑了的阶段：痕迹齐全，且不残留「我在跳过」的标记")
        chk("due.last_finish 被刷新（不再是 2020 那个）",
            disk["due"]["last_finish"] != "2020-01-01T00:00:00",
            f"实得 {disk['due']['last_finish']!r}")
        chk("due.last_ok = True", disk["due"]["last_ok"] is True)
        chk("due.last_code = 0（退出码落盘）",
            disk["due"].get("last_code") == 0,
            f"实得 {disk['due'].get('last_code')!r}")
        chk("due.last_took ≈ 1.5（耗时落盘）",
            disk["due"].get("last_took") == 1.5,
            f"实得 {disk['due'].get('last_took')!r}")
        chk("due **没有** last_skip_at（跑过就不该有跳过痕迹）",
            "last_skip_at" not in disk["due"],
            f"实得 {disk['due'].get('last_skip_at')!r}")

        # ── ⑤ `last_finish < 容器启动时刻` ⇒ 从没跑过 ─────────────
        print()
        print("⑤ 由 ③④ 推出的新判据：`last_finish` 早于容器启动 ⇒ **从没跑过**")
        print("     （这正是 2026-09-27 我读错的那个场景，现在可判了）")
        # 造一个"容器刚启动"的场景：两个阶段都没到期
        calls2 = []
        state2 = {
            "a": {"last_run": now - 5, "last_ok": True,
                  "last_finish": "2026-09-27T22:27:06"},
            "b": {"last_run": now - 5, "last_ok": True,
                  "last_finish": "2026-09-27T22:27:06"},
        }
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state2, f, indent=2)
        _run_one_round(
            daemon, [{"name": "a", "cmd": ["echo"], "interval": 1800},
                     {"name": "b", "cmd": ["echo"], "interval": 1800}], state2)
        with open(state_path, "r", encoding="utf-8") as f:
            disk2 = json.load(f)
        chk("一个阶段都没跑 ⇒ 两条 last_finish **都还是老时刻**（没有被刷新成'现在'）",
            disk2["a"]["last_finish"] == "2026-09-27T22:27:06"
            and disk2["b"]["last_finish"] == "2026-09-27T22:27:06",
            f"实得 a={disk2['a']['last_finish']!r} b={disk2['b']['last_finish']!r}")
        chk("但两条都有 last_skip_at ⇒ 能看出'它们在跳'，不是'它们没在跳'",
            "last_skip_at" in disk2["a"] and "last_skip_at" in disk2["b"])

    finally:
        daemon.STATE_FILE = orig_state_file
        try:
            os.unlink(state_path)
            os.rmdir(tmpdir)
        except OSError:
            pass

    print()
    print("=" * 72)
    passed = _total - _fail
    print(f"结果: {passed}/{_total} 通过")
    print("=" * 72)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
