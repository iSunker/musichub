"""`daemon.py` 里 **counts.json 每日快照** 相关纯函数单测（#16）。
不碰生产 `/data`、不起线程、不连 NAS、不 import 真 config。

★ 为什么单独测这几个函数（2026-10-14）：
  它们的失效**全是静默的**（`ERR-META-32`）：
    · `counts_snapshot_due` 判反 ⇒ 要么**每天一条都不生成**（源A 阶段台账永久空 —— 正是
      本任务要修的 bug 形状），要么**每 tick 重写**（刷盘 + 口径看起来在动其实没动）；
    · `_mark_counts_snapshot_sent` 写宽 ⇒ "以为生成过了，其实没生成"（读不到就当今天已发）；
    · `_write_counts_snapshot` 抛异常 ⇒ **拖垮整个 daemon**（通知是附属功能，绝不该拖垮跑批）。
  ⇒ 把它们钉在纯函数层：判据**必须能失败**，每条都配"反例"注释。

★★ 在 Windows 本机跑要带 UTF-8（emoji）：
       PYTHONIOENCODING=utf-8 python tools/test_counts_snapshot.py
"""
import os
import sys
import tempfile
import types

import _bootstrap  # noqa: E402,F401  —— 把 <repo> 与 <repo>/src 放进 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ★ 同 test_readd_retry.py：用假 config 顶替（daemon.py 顶部 import config 要 QB_PASS）。
_TMP = tempfile.mkdtemp(prefix="daemon_counts_test_")
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


def _clear_marker():
    try:
        os.remove(daemon._counts_snapshot_day_path())
    except OSError:
        pass


def test_due_guard():
    """`counts_snapshot_due`：缺失⇒due；标记今天⇒不 due；标记昨天⇒due（跨天重生成）。"""
    print("[闸] counts_snapshot_due：缺失/当天/跨天")
    import time as _t
    now = _t.time()
    today = _t.strftime("%Y-%m-%d", _t.localtime(now))
    yesterday = _t.strftime("%Y-%m-%d", _t.localtime(now - 86400))

    _clear_marker()
    # 反例：标记文件不存在却判"不必生成" ⇒ 永远不生成（本任务要修的正是这个症状的反面）
    check("缺失 ⇒ due（会生成）", daemon.counts_snapshot_due(now) is True,
          "★读不到标记却判不 due ⇒ 每天一条都不生成，源A 阶段台账永久空")

    daemon._mark_counts_snapshot_sent(now)
    # 反例：同一天又判 due ⇒ 每 tick 重写（刷盘）
    check("标记今天 ⇒ 不 due", daemon.counts_snapshot_due(now) is False,
          "★同一天仍判 due ⇒ 每 tick 重写 counts.json")

    # 跨天 ⇒ 必须重新生成（否则 counts.json 永远停在昨天）
    with open(daemon._counts_snapshot_day_path(), "w", encoding="utf-8") as f:
        f.write(yesterday)
    check("标记昨天 ⇒ due（跨天重生成）", daemon.counts_snapshot_due(now) is True,
          "★跨天不重生成 ⇒ counts.json 永远停在昨天")

    # 反例：坏内容（不该被当成"今天"）
    with open(daemon._counts_snapshot_day_path(), "w", encoding="utf-8") as f:
        f.write("坏内容\n")
    check("标记损坏 ⇒ due（宁多不少）", daemon.counts_snapshot_due(now) is True,
          "★损坏标记被当成有效 ⇒ 可能误判'今天已生成'")
    _clear_marker()


def test_mark_atomic():
    """`_mark_counts_snapshot_sent` 落盘的是**今天**，且不抛。"""
    print("[标记] _mark_counts_snapshot_sent：写今天 + 原子（不留 .tmp）")
    import time as _t
    now = _t.time()
    today = _t.strftime("%Y-%m-%d", _t.localtime(now))
    _clear_marker()
    daemon._mark_counts_snapshot_sent(now)
    with open(daemon._counts_snapshot_day_path(), "r", encoding="utf-8") as f:
        got = f.read().strip()
    check("标记内容 == 今天", got == today, f"★写入 {got!r} 而非 {today!r}")
    # 反例：留了 .tmp ⇒ 原子写没做干净
    check("无残留 .tmp", not os.path.exists(daemon._counts_snapshot_day_path() + ".tmp"),
          "★留下 .tmp ⇒ os.replace 没走到")
    _clear_marker()


def test_write_never_raises():
    """`_write_counts_snapshot` 在生成器缺失/失败时**绝不抛**，只返回 False。"""
    print("[容错] _write_counts_snapshot：生成器缺失 ⇒ 返回 False，不抛")
    orig_exists = os.path.exists
    try:
        # 反例：让 os.path.exists 对生成器脚本一律 False（模拟镜像里缺文件）
        def fake_exists(p):
            if p.endswith("gen_counts_online.py"):
                return False
            return orig_exists(p)
        os.path.exists = fake_exists
        try:
            r = daemon._write_counts_snapshot()
            check("生成器缺失 ⇒ 返回 False（不抛）", r is False,
                  "★生成器缺失却返回 True ⇒ daemon 以为生成了，实际没有")
        except Exception as e:                                     # noqa: BLE001
            check("生成器缺失 ⇒ 不抛异常", False, f"★抛了 {type(e).__name__}: {e}")
    finally:
        os.path.exists = orig_exists

    # 阳性对照：`_write_counts_snapshot` 对「生成的脚本 rc!=0」也返回 False（不抛）。
    #   ★ 造一个临时仓库布局：<d>/src/daemon 的兄弟 <d>/tools/gen_counts_online.py，必失败。
    print("[容错] _write_counts_snapshot：生成器 rc!=0 ⇒ 返回 False，不抛")
    d = tempfile.mkdtemp(prefix="counts_badgen_")
    tools_dir = os.path.join(d, "tools")
    os.makedirs(tools_dir)
    badscript = os.path.join(tools_dir, "gen_counts_online.py")
    with open(badscript, "w", encoding="utf-8") as f:
        f.write("import sys\nsys.exit(3)\n")     # 必然 rc=3

    import daemon as _d
    import unittest.mock as mock
    # 把 __file__ 假装成 <d>/src/daemon.py ⇒ 脚本推导会找到 <d>/tools/gen_counts_online.py
    with mock.patch.object(_d, "__file__", os.path.join(d, "src", "daemon.py")):
        with mock.patch.object(_d, "counts_snapshot_path",
                               return_value=os.path.join(d, "counts.json")):
            try:
                r = _d._write_counts_snapshot()
                check("rc!=0 ⇒ 返回 False（不抛）", r is False,
                      "★rc!=0 却返回 True ⇒ 假成功")
            except Exception as e:                                 # noqa: BLE001
                check("rc!=0 ⇒ 不抛异常", False, f"★抛了 {type(e).__name__}: {e}")

    # 阳性对照：空生成器（rc=0 但不写文件）⇒ 也应 False（文件不存在）
    with open(badscript, "w", encoding="utf-8") as f:
        f.write("pass\n")
    with mock.patch.object(_d, "__file__", os.path.join(d, "src", "daemon.py")):
        with mock.patch.object(_d, "counts_snapshot_path",
                               return_value=os.path.join(d, "counts.json")):
            r = _d._write_counts_snapshot()
            check("rc=0 但没写文件 ⇒ 返回 False", r is False,
                  "★只看 rc 不看产物 ⇒ '假成功'")


def test_paths():
    """落点/标记都在 DATA_DIR 下（与 heartbeat 同目录）。"""
    print("[路径] counts_snapshot_path / _counts_snapshot_day_path 在 DATA_DIR")
    check("counts.json 在 DATA_DIR", daemon.counts_snapshot_path() == os.path.join(_TMP, "counts.json"),
          "★落点不在 DATA_DIR ⇒ 源A 在 opencd2 data/ 里找不到它")
    check("标记在 DATA_DIR", daemon._counts_snapshot_day_path().startswith(_TMP),
          "★标记不在 DATA_DIR ⇒ 容器重建后闸失效")


def main():
    test_due_guard()
    test_mark_atomic()
    test_write_never_raises()
    test_paths()
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败：{FAILS}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
