"""`tools/qbtune.py` 纯函数区的单测 —— **不需要 qB**，本机就能跑。

★ 为什么单列一个测试文件：`qbtune.py` 的核心（活性判定/迟滞/打分/分配）是**纯函数**，
  它们错了**不会**在 qB 上立刻暴露（只会表现为"调了半天没效果"）。
  而"调了半天没效果"正是最难归因的那种故障（分不清是"算法错"还是"种子上游没源"）。
  ⇒ 把纯逻辑钉死，剩下的怀疑面就只剩 qB 机制本身。

★ 判据必须**能失败**（`ERR-META-32`）：下面每条 assert 都配了一个"反例"注释，
  说明什么样的实现会让它红。不是只打印。

跑：python tools/test_qbtune.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ★ qbtune.py 顶部 `import config`，而 config 需要 QB_PASS（本机可能没有）⇒ 用假 config 顶替，
#   本测试**不碰** config 的任何真实值，只测纯函数。
_fake = types.ModuleType("config")
_fake.QB_HOST = "http://127.0.0.1:3020"
_fake.QB_USER = "admin"
_fake.QB_PASS = "x"
_fake.DATA_DIR = "/tmp"
sys.modules.setdefault("config", _fake)

import qbtune as Q  # noqa: E402


def T(**kw):
    """造一个假 torrent。"""
    d = {
        "hash": kw.get("hash", "h" * 40),
        "name": kw.get("name", "t"),
        "state": kw.get("state", "downloading"),
        "dlspeed": kw.get("dlspeed", 0),
        "upspeed": 0,
        "progress": kw.get("progress", 0.5),
        "num_seeds": kw.get("num_seeds", 0),
        "num_leechs": kw.get("num_leechs", 0),
        "num_complete": kw.get("num_complete", 0),
        "num_incomplete": kw.get("num_incomplete", 0),
        "amount_left": kw.get("amount_left", 100),
        "size": kw.get("size", 1000),
        "eta": 8640000,
        "priority": 1,
        "force_start": False,
        "upload_limit": 512000,
        "download_limit": 0,
        "category": "opencd",
    }
    return types.SimpleNamespace(**d)


class Cfg(Q.Cfg):
    def __init__(self):
        # 不读环境，给确定性默认
        self.interval = 60
        self.cap = 10
        self.stall_enter = 5
        self.live_exit = 2
        self.trickle_bps = 8192
        self.trickle_enter = 10
        self.trickle_penalty = 0.5
        self.retry_ticks = 30
        self.retry_boost = 4.0
        self.credit_rate = 1.0
        self.credit_cap = 1800.0
        self.credit_decay = 0.5
        self.min_dwell_ticks = 2
        self.displace_margin = 0.5
        self.write_cooldown_ticks = 1
        self.max_promote = 3
        self.max_writes = 8
        self.bw_mode = "cap_greedy"
        self.greedy_k = 2
        self.greedy_headroom = 1.5
        self.min_per_torrent_bps = 16384
        self.budget_bps = 0
        self.set_limits = True
        self.w_speed = 2.0
        self.w_seeds = 1.5
        self.w_swarm = 1.0
        self.w_meta = 0.3
        self.w_wait = 1.2
        self.w_left = 0.5
        self.w_boost = 3.0
        self.no_swarm_penalty = 20.0
        self.max_consec_conn_fail = 10
        self.restore_on_exit = True
        self.enable = False


FAILS = []


def check(name, cond, why):
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} —— 反例：{why}")
        FAILS.append(name)


def test_swarm_tier():
    print("[swarm_tier] 有源优先是结构性的")
    check("在传=tier0", Q.swarm_tier(T(dlspeed=100, num_seeds=0)) == 0, "在传的不是 tier0")
    check("有种子=tier1", Q.swarm_tier(T(dlspeed=0, num_seeds=2)) == 1, "有种子的不是 tier1")
    check("有swarm记录=tier2", Q.swarm_tier(T(dlspeed=0, num_seeds=0, num_complete=3)) == 2,
          "有 swarm 记录的不是 tier2")
    check("啥都没有=tier3", Q.swarm_tier(T(dlspeed=0, num_seeds=0, num_complete=0)) == 3,
          "全空的不是 tier3")
    check("metaDL 豁免=-1", Q.swarm_tier(T(state="metaDL", dlspeed=0, num_seeds=0)) == -1,
          "metaDL 没被豁免")

    # ★ 关键：等得久的零源种不该顶掉在传的种子
    cfg = Cfg()
    cfg.cap = 1
    state = {"_active": [], "torrents": {}}
    scored = [
        # 零源但等待积分巨大（会拉高 raw score）——tier3
        (T(hash="dead" + "0" * 36, dlspeed=0, num_seeds=0, num_complete=0), 99.0, False, False),
        # 在传但分数低——tier0
        (T(hash="live" + "0" * 36, dlspeed=500, num_seeds=1), 1.0, False, False),
    ]
    active, *_ = Q.select_active(scored, cfg, tick=5, state=state, qb_active=set())
    check("在传的低分 > 零源的高分", ("live" + "0" * 36) in active,
          "★零源种子靠 fairness 高分顶掉了在传的种子 ⇒ 有源优先失效")


def test_is_stalled():
    print("[is_stalled]")
    check("0速0源=stalled", Q.is_stalled(T(dlspeed=0, num_seeds=0, state="stalledDL")),
          "把 0速0源 判成不 stalled")
    check("有速=不stalled", not Q.is_stalled(T(dlspeed=1000, num_seeds=0)),
          "把有速度的判成 stalled")
    check("有源=不stalled", not Q.is_stalled(T(dlspeed=0, num_seeds=3)),
          "把有源的判成 stalled")
    check("queuedDL 不算 stalled", not Q.is_stalled(T(dlspeed=0, num_seeds=0, state="queuedDL")),
          "★把 queuedDL(被cap挡) 判成 stalled ⇒ 自我实现死循环")
    check("metaDL 不算 stalled", not Q.is_stalled(T(dlspeed=0, num_seeds=0, state="metaDL")),
          "★把取元数据的判成 stalled ⇒ 它永远拿不到元数据")


def test_hysteresis():
    print("[update_hysteresis] 进死难、复活易")
    cfg = Cfg()
    rec = {}
    # 连续 4 帧 stalled：还没到 stall_enter=5 ⇒ 不 dead
    for _ in range(4):
        dead, _ = Q.update_hysteresis(rec, stalled_now=True, trickle_now=False, cfg=cfg)
    check("4 帧 stalled 还不 dead", not dead, "stall_enter=5 却第4帧就 dead")
    # 第 5 帧 ⇒ dead
    dead, changed = Q.update_hysteresis(rec, stalled_now=True, trickle_now=False, cfg=cfg)
    check("第5帧 stalled ⇒ dead", dead, "第5帧没 dead")
    check("dead 翻转上报 changed", changed, "翻转了却没置 changed")
    # 1 帧 live：live_exit=2 ⇒ 还不复活
    dead, _ = Q.update_hysteresis(rec, stalled_now=False, trickle_now=False, cfg=cfg)
    check("1 帧 live 还不复活", dead, "★ live_exit=2 却第1帧就复活 ⇒ 无迟滞")
    # 第 2 帧 live ⇒ 复活
    dead, _ = Q.update_hysteresis(rec, stalled_now=False, trickle_now=False, cfg=cfg)
    check("2 帧 live ⇒ 复活", not dead, "第2帧 live 没复活")
    # 两阈值必须不同
    check("阈值确实不同", cfg.stall_enter != cfg.live_exit,
          "★两阈值相等 = 退化成无迟滞")


def test_promise_monotonic():
    print("[promise_score] log 加权：快种子分更高但非压倒")
    cfg = Cfg()
    slow = Q.promise_score(T(dlspeed=20 * 1024, num_seeds=1), cfg, False)      # 20KB/s 1源
    fast = Q.promise_score(T(dlspeed=2048 * 1024, num_seeds=3), cfg, False)    # 2MB/s 3源
    check("快 > 慢", fast > slow, "快的分不比慢的高")
    # ★ 关键：100x 速度比经 log 后不该变成 100x 分数比（加权而非赢者通吃）
    ratio = fast / slow if slow > 0 else 999
    check("速度比100:1 ⇒ 分数比 < 4:1", ratio < 4.0,
          f"★log 没起作用，分数比={ratio:.1f}（应远小于速度比 100:1）")
    # trickle 打折
    t = T(dlspeed=1000, num_seeds=1)
    full = Q.promise_score(t, cfg, False)
    disc = Q.promise_score(t, cfg, True)
    check("trickle 打折后更低", disc < full, "trickle 没打折")
    # ★ 有源优先：有 swarm 信号的 > 无任何信号的
    noswarm = Q.promise_score(T(dlspeed=0, num_seeds=0, num_complete=0, num_leechs=0), cfg, False)
    hasswarm = Q.promise_score(T(dlspeed=0, num_seeds=0, num_complete=5, num_leechs=0), cfg, False)
    check("有 num_complete 的 > 全无信号的", hasswarm > noswarm,
          "★无 swarm 信号没被罚 ⇒ 死种会跟有源的抢槽")


def test_fairness_grows():
    print("[fairness_score] 等得越久分越高")
    cfg = Cfg()
    t = T(amount_left=500, size=1000)
    f0 = Q.fairness_score({"wait_credit": 0.0}, t, cfg)
    f60 = Q.fairness_score({"wait_credit": 60.0}, t, cfg)
    f600 = Q.fairness_score({"wait_credit": 600.0}, t, cfg)
    check("credit 越大 fairness 越高", f0 < f60 < f600, "fairness 没随 credit 增长")


def test_split_limits():
    print("[split_limits] 无限写 -1，绝不写 0")
    cfg = Cfg()
    acts = [T(hash="a" * 40, dlspeed=5 * 1024 * 1024),   # 最贪
            T(hash="b" * 40, dlspeed=3 * 1024 * 1024),   # 次贪
            T(hash="c" * 40, dlspeed=10 * 1024)]         # 慢
    out = Q.split_limits(acts, cfg)
    check("无 0 值（0=真限0）", all(v != 0 for v in out.values()),
          "★写出了 download_limit=0（语义含糊/危险）")
    check("慢的无限(-1)", out["c" * 40] == -1, "慢种子没给无限")
    check("最贪的被套帽", out["a" * 40] > 0 and out["a" * 40] != -1,
          "最贪的没被套帽")
    # budget 模式
    cfg.bw_mode = "budget"
    cfg.budget_bps = 100000
    out2 = Q.split_limits(acts, cfg)
    check("budget 模式无 0", all(v != 0 for v in out2.values()), "budget 写出了 0")
    # ★ 地板可能让慢种触地板；但**总和不得超过预算**（超了就说明分配逻辑漏了回收）
    check("budget 总和 <= 预算", sum(out2.values()) <= 100000,
          f"budget 分配总和={sum(out2.values())} > 预算 100000")
    check("budget 总和接近预算(>80%)", sum(out2.values()) > 80000,
          f"budget 分配总和={sum(out2.values())} 远低于预算")


def test_select_active():
    print("[select_active] 死种不占位、活种优先")
    cfg = Cfg()
    cfg.cap = 3
    state = {"_active": [], "torrents": {}}
    scored = [
        (T(hash="live1" + "0" * 35, dlspeed=1000, num_seeds=2), 10.0, False, False),
        (T(hash="live2" + "0" * 35, dlspeed=500, num_seeds=1), 8.0, False, False),
        (T(hash="live3" + "0" * 35, dlspeed=100, num_seeds=1), 6.0, False, False),
        (T(hash="dead1" + "0" * 35, dlspeed=0, num_seeds=0), 9.0, False, True),   # 死，分高也不选
        (T(hash="dead2" + "0" * 35, dlspeed=0, num_seeds=0), 7.0, False, True),
    ]
    active, promote, demote, note = Q.select_active(scored, cfg, tick=5, state=state, qb_active=set())
    check("选 3 个", len(active) == 3, f"选了 {len(active)} 个，应 3")
    check("死种(dead1/dead2)未进", not any(h.startswith("dead") for h in active),
          "★死种占了活动槽")
    check("活种全进", all(any(h.startswith(x) for x in ("live1", "live2", "live3")) for h in active),
          "活种没全进")

    # 活种不足时用死种填满
    scored2 = [
        (T(hash="l1" + "0" * 38, dlspeed=1000, num_seeds=1), 5.0, False, False),
        (T(hash="d1" + "0" * 38, dlspeed=0, num_seeds=0), 1.0, False, True),
        (T(hash="d2" + "0" * 38, dlspeed=0, num_seeds=0), 2.0, False, True),
    ]
    state2 = {"_active": [], "torrents": {}}
    active2, *_ = Q.select_active(scored2, cfg, tick=5, state=state2, qb_active=set())
    check("活不足K时用死填满", len(active2) == 3, f"应填满到 3，得 {len(active2)}")


def test_min_dwell():
    print("[select_active] min-dwell 保护")
    cfg = Cfg()
    cfg.cap = 1
    cfg.min_dwell_ticks = 3
    # 上轮 active = old，admitted_tick 很近 ⇒ 不该被顶掉
    state = {"_active": ["old" + "0" * 37],
             "torrents": {"old" + "0" * 37: {"admitted_tick": 4}}}
    scored = [
        (T(hash="old" + "0" * 37, dlspeed=100, num_seeds=1), 1.0, False, False),  # 分低
        (T(hash="new" + "0" * 37, dlspeed=900, num_seeds=9), 9.0, False, False),  # 分高
    ]
    active, promote, demote, _ = Q.select_active(scored, cfg, tick=5, state=state, qb_active={"old" + "0" * 37})
    check("min-dwell 内不被顶掉", ("old" + "0" * 37) in active,
          "★驻留未满就被顶 ⇒ 抖动")


def main():
    test_swarm_tier()
    test_is_stalled()
    test_hysteresis()
    test_promise_monotonic()
    test_fairness_grows()
    test_split_limits()
    test_select_active()
    test_min_dwell()
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败：{FAILS}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
