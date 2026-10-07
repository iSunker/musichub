"""`free_window` 单测 —— **纯离线**，零请求、零写库、不碰 qB、不碰 /data。

★ 为什么值得单测（2026-10-06，`ERR-META-32`：判据必须**能失败**）：
  `free_window.py` 的三条判据（行内优惠 / 到期时刻 / 全站横幅）**全部是"静默失效"型** ——
  解析失败不抛异常，只是**安静地返回空结果**：

    · `title` 是 **HTML 转义**（`&quot;`）⇒ 正则匹配不到 ⇒ `expire_at` 恒空
      ⇒ 脚本**永远不知道该何时 pause 回去**（窗口过了还在跑 ⇒ 白扣下载量）。
    · `_IMG_RE` 用 `[^>]*` 会被 `onmouseover="..."` 里的 `...` 等处**截断**
      ⇒ img 片段取不全 ⇒ class 认不出 ⇒ 免费种被当"收费"**永不 resume**。
    · 全站横幅有**第三种形态**（裸 `全站Free活动`，无时间区间）
      ⇒ ★ 经 2026-10-06 复盘（F-b）该形态**语义歧义**，已**降级为弱信号** `free_suspect`
        （既不当 free 去 resume，也不当收费去 pause）；另修 F-a：多区间**取最新**。

  ⇒ 这些**都在 2026-10-06 的真机上真实踩中并修过**（见 `.scratch/免费窗口脚本-草案与实测`）。
    本测把**当时的真样本**钉死，防止改动时回归。

★★ 每条都配**反例注释**：把对应修法撤掉，该条必须报红。

★★ 在 Windows 本机跑要带 UTF-8（中文）：`PYTHONIOENCODING=utf-8 python tools/test_free_window.py`

跑：python tools/test_free_window.py
"""
import os
import sys
from datetime import datetime

import _bootstrap  # noqa: E402,F401  —— 把 <repo> 与 <repo>/src 放进 sys.path

# ★ 2026-10-06（#34）：`free_window.py` 已从 `.scratch/` 正式收进 `src/`
#   ⇒ 本测**只** import 入库版（`_bootstrap` 已把 `src/` 放进 sys.path）。
#   顶层只 import 标准库（qb_client/crawl 都在函数体里 import），故可直接 import。
import free_window as fw  # noqa: E402

FAILS = []


def check(name, cond, why=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [XX] {name}  <-- 反例：{why}")
        FAILS.append(name)


# =========================================================================== #
# 样本 1（★ B1 + B2）：真机 `torrents.php` 行 —— title 是 HTML 转义 + onmouseover 里有 &gt;
#   反例成因：若不 unescape，则 `title=&quot;...&quot;` 匹配不到 ⇒ expire_at 恒空（B1）；
#             若 `_IMG_RE` 用 `[^>]*`，会在 `&gt;` / `...` 处截断 ⇒ class 认不出（B2）。
# =========================================================================== #
ROW_FREE2UP = (
    '<table class="torrentname"><tr><td class="embedded">'
    '<a href="download.php?id=12345">&nbsp;</a>'
    '<img class="pro_free2up" src="pic/trans.gif" alt="2X Free" '
    'onmouseover="domTT_activate(this, event, \'content\', '
    '\'&lt;font class=&quot;twoupfree&quot;&gt;2X&#20813;&#36027;&lt;/font&gt; '
    '&lt;b&gt;&#21129;&#39192;&#26178;&#38291;&#65306;&lt;/b&gt;'
    '&lt;span title=&quot;2026-10-07 17:55:03&quot;&gt;1&#22825;0&#26178;&lt;/span&gt;\', '
    'this, \'width\', 300)" />'
    '</td></tr></table>'
)

# 样本 2（★ 免费但不带 2x）：只 class="pro_free"，到期时刻同法
ROW_FREE = (
    '<table class="torrentname"><tr><td>'
    '<img class="pro_free" src="pic/trans.gif" alt="Free" '
    'onmouseover="domTT_activate(this, event, \'content\', '
    '\'&lt;span title=&quot;2026-10-09 16:49:06&quot;&gt;2&#22825;&lt;/span&gt;\')" />'
    '</td></tr></table>'
)

# 样本 3（★ 不免费的两种：50%下载+2x / 仅 2x）
ROW_50PCT2UP = (
    '<table class="torrentname"><tr><td>'
    '<img class="pro_50pctdown2up" src="pic/trans.gif" alt="2X 50%" '
    'onmouseover="domTT_activate(this, event, \'content\', '
    '\'&lt;span title=&quot;2026-10-09 16:49:06&quot;&gt;2&#22825;&lt;/span&gt;\')" />'
    '</td></tr></table>'
)
ROW_2UP = (
    '<table class="torrentname"><tr><td>'
    '<img class="pro_2up" src="pic/trans.gif" alt="2X" />'
    '</td></tr></table>'
)

# 样本 4：行在、无任何优惠 img ⇒ 收费
ROW_CHARGING = (
    '<table class="torrentname"><tr><td>'
    '<a href="details.php?id=999">某收费种</a></td></tr></table>'
)


def test_row_free2up():
    print("[行判据] pro_free2up ⇒ 免费 + 到期时刻（★ B1/B2 真样本）")
    w = fw.parse_row_promo(ROW_FREE2UP)
    check("promo=free2up", w.promo == "free2up", f"promo={w.promo!r}；若 class 认不出多为 _IMG_RE 截断(B2)")
    check("is_free=True", w.is_free is True, "free2up 必须在 PROMO_FREE 里")
    # ★ B1：这一段**必须**要在 unescape 之后才拿得到
    check("expire_at 取到(HTML转义修复)",
          w.expire_at == "2026-10-07 17:55:03",
          f"expire_at={w.expire_at!r}；空 ⇒ 没 unescape(B1)")
    check("source=site-row", w.source == "site-row", "")


def test_row_free():
    print("[行判据] pro_free ⇒ 免费")
    w = fw.parse_row_promo(ROW_FREE)
    check("promo=free", w.promo == "free", f"promo={w.promo!r}")
    check("is_free=True", w.is_free is True, "free 必须在 PROMO_FREE 里")
    check("expire_at=2026-10-09 16:49:06", w.expire_at == "2026-10-09 16:49:06",
          f"expire_at={w.expire_at!r}")


def test_row_not_free():
    print("[行判据] 50%下载 / 仅2x ⇒ **不免费**（但 promo 名要认对）")
    w50 = fw.parse_row_promo(ROW_50PCT2UP)
    check("50pct2up 认得出", w50.promo == "50pct2up", f"promo={w50.promo!r}")
    check("50pct2up 不免费", w50.is_free is False, "★ 50%下载≠免费，绝不能算 free")
    w2 = fw.parse_row_promo(ROW_2UP)
    check("2up 认得出", w2.promo == "2up", f"promo={w2.promo!r}")
    check("2up 不免费", w2.is_free is False, "★ 仅 2x 上传≠免费")


def test_row_charging():
    print("[行判据] 无优惠 img ⇒ 收费(none)")
    w = fw.parse_row_promo(ROW_CHARGING)
    check("promo=none", w.promo == "none", f"promo={w.promo!r}")
    check("is_free=False", w.is_free is False, "收费种绝不能算 free")


def test_row_unknown_class():
    print("[行判据] ★ 有 pro_* 但白名单认不出 ⇒ **unknown**（不猜成收费/免费）")
    row = '<table class="torrentname"><tr><td><img class="pro_99pctdown" src="x" /></td></tr></table>'
    w = fw.parse_row_promo(row)
    check("promo=unknown", w.promo == "unknown",
          f"promo={w.promo!r}；认不出必须 unknown，绝不能当 none(收费)去误 pause")


# =========================================================================== #
# 样本 5（★ B3）：全站横幅三形态
#   A 日志式（带区间） / B 横幅式（带区间） / C 裸标题（无区间，live 实测形态）
#   反例成因：若不加 `_GLOBAL_C_RE`，形态 C 漏认 ⇒ 全站免费时整批不 resume（B3）。
# =========================================================================== #
IDX_A = "<div>北京时间2026-10-06 00:00:00 至 2026-10-10 23:59:59 开启全站Free。</div>"
IDX_B = '<font color="white">全站 [Free] 生效中！<br/>时间：2026-10-10 23:27:30 ~ 2030-05-10 23:27:33</font>'
IDX_C = "<b>全站Free活动</b> 进行中"          # ★ 无区间：live 首页就是这种
IDX_NONE = "<div>今日签到得 5 魔力。</div>"


def test_global_a():
    print("[全站判据] 形态A 日志式（带区间）⇒ free + 到期=区间止")
    g = fw.parse_global_free(IDX_A)
    check("promo=free", g.promo == "free", f"promo={g.promo!r}")
    check("expire_at=区间止", g.expire_at == "2026-10-10 23:59:59", f"expire_at={g.expire_at!r}")
    check("source=site-global", g.source == "site-global", "")


def test_global_b():
    print("[全站判据] 形态B 横幅式（带区间）⇒ free + 到期=区间止")
    g = fw.parse_global_free(IDX_B)
    check("promo=free", g.promo == "free", f"promo={g.promo!r}")
    check("expire_at=2030-05-10 23:27:33", g.expire_at == "2030-05-10 23:27:33",
          f"expire_at={g.expire_at!r}")


def test_global_c():
    print("[全站判据] 形态C 裸标题（无区间）★ F-b：**弱信号 free_suspect** ⇒ 不判 free")
    g = fw.parse_global_free(IDX_C)
    check("promo=free_suspect（弱信号）", g.promo == "free_suspect",
          f"promo={g.promo!r}；★ 裸标题无区间绝不能直接判 free（F-b）")
    check("is_free=False（不 resume）", g.is_free is False,
          "★ F-b：无区间裸标题 ⇒ 不判免费，否则可能在全站无窗时误 resume")
    check("expire_at='' (无区间)", g.expire_at == "", f"expire_at={g.expire_at!r}")
    check("expired()=False(无到期不判过)", g.expired() is False,
          "★ 弱信号无到期 ⇒ 不能判成已到期")


def test_global_none():
    print("[全站判据] 无横幅 ⇒ none")
    g = fw.parse_global_free(IDX_NONE)
    check("promo=none", g.promo == "none", f"promo={g.promo!r}")


def test_global_expired_announcement():
    print("[全站判据] ★ F-a：只剩**过期**旧公告 ⇒ **none**（不回落成 free、不误判当前免费）")
    # 真机首页实测：`北京时间2026-05-06 至 2026-05-10 开启全站Free。`（过去）
    g = fw.parse_global_free("<div>北京时间2026-05-06 00:00:00 至 2026-05-10 23:59:59 开启全站Free。</div>")
    # ★ F-a 修法：A/B 区间命中即返回"最新一条"，是否生效由调用方按 now 判 expired；
    #   此处区间止=2026-05-10 ⇒ expired(now=10-06)=True ⇒ 调用方不会当免费。
    check("promo=free（解析到区间）", g.promo == "free", f"promo={g.promo!r}")
    check("expired(now=2026-10-06)=True",
          g.expired(datetime(2026, 10, 6, 12, 0, 0)) is True,
          "★ 过期公告必须判为已过期，否则会把过去的公告当成'当前全站免费'")


def test_global_fa_pick_latest():
    print("[全站判据] ★ F-a：首页同挂**过期旧公告 + 生效新公告** ⇒ 必须取**最新**那条")
    # 旧(5月，已过期) 在前，新(11月，生效) 在后 —— 老代码 `A_RE.search` 会拿到旧的那条(错)。
    html = ("<div>北京时间2026-05-06 00:00:00 至 2026-05-10 23:59:59 开启全站Free。</div>"
            "<div>北京时间2026-11-01 00:00:00 至 2026-11-05 23:59:59 开启全站Free。</div>")
    g = fw.parse_global_free(html)
    check("取最新区间止=2026-11-05", g.expire_at == "2026-11-05 23:59:59",
          f"expire_at={g.expire_at!r}；★ 若=5月 ⇒ 拿了陈旧那条(F-a 未修)")
    check("新公告未过期(now=10-06)", g.expired(datetime(2026, 10, 6)) is False,
          "★ 应判为当前生效")


def test_global_fa_expired_no_fallthrough_to_c():
    print("[全站判据] ★ F-a 负向：只剩过期区间 + 另有裸标语 ⇒ **不回落成 free**")
    # 过期区间(A) + 裸标语(C) 并存：老逻辑会 A 命中(过期) 被 expired 挡，但 C 会兜成 free(错)。
    html = ("<div>北京时间2026-05-06 00:00:00 至 2026-05-10 23:59:59 开启全站Free。</div>"
            "<b>全站Free活动</b>")
    g = fw.parse_global_free(html)
    # 有区间 ⇒ 返回区间那条(free)，由调用方 expired 判过期；**不会**掉到 C。
    check("有区间时优先返回区间条", g.promo == "free" and g.expire_at == "2026-05-10 23:59:59",
          f"promo={g.promo!r} expire={g.expire_at!r}")
    check("★ 该条 expired(10-06)=True ⇒ 调用方不当免费", g.expired(datetime(2026, 10, 6)) is True,
          "★ 过期区间必须能判过期，绝不因同页有裸标语 C 而误判当前免费(F-a)")


def test_global_fb_suspect_never_acts():
    print("[计划] ★ F-b 负向：free_suspect ⇒ **既不 resume 也不 pause**（弱信号不动手）")
    infos = [_t("s1", "stoppedDL"), _t("s2", "uploading")]
    wins = {"s1": fw.FreeWindow(promo="free_suspect", source="site-global"),
            "s2": fw.FreeWindow(promo="free_suspect", source="site-global")}
    p = fw.build_plan(infos, wins)
    check("弱信号不 resume", "s1" not in p.to_resume,
          "★ free_suspect 被当免费 resume = 全站无窗时白扣下载量(F-b)")
    check("弱信号不 pause", "s2" not in p.to_pause,
          "★ free_suspect 被当收费 pause = 误停正在跑的种")
    check("都进 skipped", len(p.skipped) == 2 and
          {w for _, w in p.skipped} == {"free-suspect-weak-signal"}, f"skipped={p.skipped}")


# =========================================================================== #
# 到期判定 + 计划（★ 纯函数，不碰网络 —— 幂等/边界的核心）
# =========================================================================== #
def test_expired_boundary():
    print("[到期] 边界：now<到期 未过 / now>=到期 已过 / 无到期 视为未过")
    now = datetime(2026, 10, 7, 17, 55, 3)
    w = fw.FreeWindow(promo="free", expire_at="2026-10-07 17:55:03", source="site-row")
    check("now==到期 ⇒ expired(>=)", w.expired(now) is True, "")
    check("now 早一秒 ⇒ 未过期", w.expired(datetime(2026, 10, 7, 17, 55, 2)) is False, "")
    check("now 晚一秒 ⇒ 已过期", w.expired(datetime(2026, 10, 7, 17, 55, 4)) is True, "")
    w0 = fw.FreeWindow(promo="free", expire_at="", source="site-global")
    check("无到期 ⇒ 不判过期（保守）", w0.expired(now) is False, "")


def _t(h, state):
    return {"hash": h, "state": state, "tags": "收费种子（暂停下载）"}


def test_plan_unknown_never_touched():
    print("[计划] ★ unknown ⇒ 一律 skip（既不 resume 也不 pause）—— 绝不猜")
    infos = [_t("u1", "stoppedDL"), _t("u2", "uploading")]
    wins = {"u1": fw.FreeWindow(promo="unknown"), "u2": fw.FreeWindow(promo="unknown")}
    p = fw.build_plan(infos, wins)
    check("unknown 不 resume", "u1" not in p.to_resume, "★ unknown 被当免费 resume = 白扣下载量")
    check("unknown 不 pause", "u2" not in p.to_pause, "★ unknown 被当收费 pause = 误停")
    check("都进 skipped", len(p.skipped) == 2, f"skipped={p.skipped}")


def test_plan_free_resume_paused():
    print("[计划] 免费 + 已暂停 ⇒ 该 resume")
    infos = [_t("f1", "stoppedDL")]
    wins = {"f1": fw.FreeWindow(promo="free2up", expire_at="2026-10-07 17:55:03")}
    p = fw.build_plan(infos, wins, now=datetime(2026, 10, 6, 12, 0, 0))
    check("进 to_resume", p.to_resume == ["f1"], f"to_resume={p.to_resume}")
    check("不进 to_pause", p.to_pause == [], f"to_pause={p.to_pause}")


def test_plan_charging_pause_running():
    print("[计划] 收费 + 在跑 ⇒ 该 pause")
    infos = [_t("c1", "uploading")]
    wins = {"c1": fw.FreeWindow(promo="none", source="site-row")}
    p = fw.build_plan(infos, wins, now=datetime(2026, 10, 6, 12, 0, 0))
    check("进 to_pause", p.to_pause == ["c1"], f"to_pause={p.to_pause}")
    check("不进 to_resume", p.to_resume == [], f"to_resume={p.to_resume}")


def test_plan_idempotent():
    print("[计划] ★ 幂等：免费+在跑 不重复 resume；收费+已暂停 不重复 pause")
    infos = [_t("a", "uploading"), _t("b", "stoppedDL")]
    wins = {"a": fw.FreeWindow(promo="free", expire_at="2026-10-07 17:55:03"),
            "b": fw.FreeWindow(promo="none", source="site-row")}
    p = fw.build_plan(infos, wins, now=datetime(2026, 10, 6, 12, 0, 0))
    check("在跑的免费种不 resume", p.to_resume == [], f"to_resume={p.to_resume}")
    check("已停的收费种不 pause", p.to_pause == [], f"to_pause={p.to_pause}")
    check("都进 skipped(already-correct)", {w for _, w in p.skipped} == {"already-correct"},
          f"skipped={p.skipped}")


def test_plan_free_but_expired():
    print("[计划] 免费标记在、但**已到期** + 在跑 ⇒ 该 pause 回去")
    infos = [_t("e1", "uploading")]
    wins = {"e1": fw.FreeWindow(promo="free", expire_at="2026-10-05 00:00:00")}
    p = fw.build_plan(infos, wins, now=datetime(2026, 10, 6, 12, 0, 0))
    check("已到期 ⇒ 进 to_pause", p.to_pause == ["e1"], f"to_pause={p.to_pause}")


def test_apply_plan_dryrun_no_call():
    print("[执行] ★ dry_run=True ⇒ **绝不**调用 qb（用假 qb 断言零副作用）")
    class FakeQB:
        def __init__(self):
            self.calls = []
        def torrents_resume(self, **kw):
            self.calls.append(("resume", kw))
        def torrents_pause(self, **kw):
            self.calls.append(("pause", kw))
    plan = fw.Plan(to_resume=["h1"], to_pause=["h2"])
    qb = FakeQB()
    out = fw.apply_plan(qb, plan, dry_run=True, log=lambda *a, **k: None)
    check("dry_run 无任何 qb 调用", qb.calls == [], f"calls={qb.calls}")
    check("计数仍正确", out == {"resumed": 1, "paused": 1, "skipped": 0}, f"out={out}")
    # 反向对照：--apply 才真调
    qb2 = FakeQB()
    fw.apply_plan(qb2, plan, dry_run=False, log=lambda *a, **k: None)
    check("apply 时才调 qb", [c[0] for c in qb2.calls] == ["resume", "pause"],
          f"calls={qb2.calls}")


def test_isolate_row_for_hash():
    print("[定位] 列表页按 hash 切行（大小写不敏感）")
    html = ('<table class="torrentname"><tr><td>abc123DEF</td></tr></table>'
            '<table class="torrentname"><tr><td>other</td></tr></table>')
    seg = fw._isolate_row_for_hash(html, "abc123def")
    check("命中该行", seg is not None and "abc123DEF" in seg, f"seg={seg!r}")
    check("未命中 ⇒ None", fw._isolate_row_for_hash(html, "nope") is None, "")


# =========================================================================== #
# ★ 真页面夹具（最强样本）：`opencdhtml/sanitized/sample-listpage.html`
#   这是真机上保存下来的 `torrents.php` 列表页。用它做**端到端**回归：
#   逐行 parse_row_promo，断言 promo 分布。
#   实测真值：free2up=7, free=4（⇒ 免费 11 条）、50pct=4、2up=3，其余 none。
#   反例成因：若 `parse_row_promo` 的 class 取法改回贪婪 `[^>]*class="..."`，
#             内层 `<font class="twoupfree">` 会被误取 ⇒ free2up 掉到 0（红）。
# =========================================================================== #
_LISTPAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "opencdhtml", "sanitized", "sample-listpage.html")


def test_real_listpage():
    print("[真样本] 端到端跑真机列表页 sample-listpage.html")
    if not os.path.exists(_LISTPAGE):
        print(f"  [--] 跳过（缺夹具 {_LISTPAGE}）")
        return
    import re as _re
    with open(_LISTPAGE, encoding="utf-8", errors="replace") as fh:
        page = fh.read()
    rows = _re.split(r'(?i)<table[^>]*class="torrentname"', page)
    from collections import Counter
    c = Counter()
    for seg in rows:
        if "pro_" in seg:
            c[fw.parse_row_promo(seg).promo] += 1
    check("free2up=7（★ 贪婪 class 会掉到 0）", c["free2up"] == 7, f"c={dict(c)}")
    check("free=4", c["free"] == 4, f"c={dict(c)}")
    check("免费合计=11", c["free2up"] + c["free"] == 11, f"c={dict(c)}")
    check("2up=3", c["2up"] == 3, f"c={dict(c)}")
    check("50pct=4", c["50pct"] == 4, f"c={dict(c)}")


def test_class_regex_order_fragility():
    print("[阴性对照] ★ 证明 class 取法是 load-bearing：贪婪法必错")
    import re as _re
    raw = ("<img class=\"pro_free2up\" src=\"x\" alt=\"2X Free\" "
           "onmouseover=\"domTT(this,event,'content','<font class=\"twoupfree\">X</font> "
           "<span title=\"2026-10-07 17:55:03\">1</span>', this, 'width', 300)\" />")
    greedy = _re.search(r'<img[^>]*class="([^"]*)"[\s\S]*?/>', raw, _re.I)
    fixed = _re.search(fw._CLASS_ATTR_RE.pattern, raw, _re.I)
    check("贪婪法误取内层 class（反例成立）", greedy and greedy.group(1) == "twoupfree",
          f"greedy={greedy.group(1) if greedy else None}")
    check("修法取到真 class", fixed and fixed.group(1) == "pro_free2up",
          f"fixed={fixed.group(1) if fixed else None}")


def test_global_pick_latest_is_loadbearing():
    print("[阴性对照] ★ 证明 F-a '取最新' 是 load-bearing：老'首个匹配'法必错")
    html = ("<div>北京时间2026-05-06 00:00:00 至 2026-05-10 23:59:59 开启全站Free。</div>"
            "<div>北京时间2026-11-01 00:00:00 至 2026-11-05 23:59:59 开启全站Free。</div>")
    # 老实现 = `_GLOBAL_A_RE.search(...)` 取"第一个"（这里是 5 月那条）
    import re as _re
    old = _re.search(fw._GLOBAL_A_RE, html)
    fixed = fw.parse_global_free(html)
    check("老'首个匹配'法取到 5 月(过期)那条（反例成立）",
          old and old.group(2) == "2026-05-10 23:59:59",
          f"old={old.group(2) if old else None}")
    check("修法取到 11 月(最新)那条", fixed.expire_at == "2026-11-05 23:59:59",
          f"fixed={fixed.expire_at!r}")


def main():
    for fn in (test_row_free2up, test_row_free, test_row_not_free, test_row_charging,
               test_row_unknown_class,
               test_global_a, test_global_b, test_global_c, test_global_none,
               test_global_expired_announcement,
               test_global_fa_pick_latest, test_global_fa_expired_no_fallthrough_to_c,
               test_global_fb_suspect_never_acts,
               test_expired_boundary,
               test_plan_unknown_never_touched, test_plan_free_resume_paused,
               test_plan_charging_pause_running, test_plan_idempotent,
               test_plan_free_but_expired, test_apply_plan_dryrun_no_call,
               test_isolate_row_for_hash,
               test_real_listpage, test_class_regex_order_fragility,
               test_global_pick_latest_is_loadbearing):
        fn()
        print()
    if FAILS:
        print(f"[FAIL] {len(FAILS)} 项失败：{FAILS}")
        return 1
    print("[PASS] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
