"""
OpenCD（皇后）页面 → `tasks` 表的**写入侧**。

═══════════════════════════════════════════════════════════════════════════
这个模块补上的是「最后一环」—— 此前抓取层把页面解析完就**丢掉**了
═══════════════════════════════════════════════════════════════════════════

实测（2026-09-21）：
  · `crawl.py` 的 `run_from_qb()` 把结果攒进一个**局部 list**，函数返回即消失；
  · 该文件 `import db` 命中数 = **0** ⇒ **结构上不可能入库**；
  · 生产 `tasks` 表里 `opencd_tid` 非空的记录 = **0 条**。

★ 2026-09-23：`run_from_qb()` 那条路（扫 qB → 按种子名称搜皇后）**已整条删除**，
  但本模块要解决的那个缺口**一模一样地还在** —— 抓取层仍然只解析、不入库，
  入库这件事仍然由本模块单独承担（现在的调用方是 `topup_opencd_fields.py`
  与 `crawl.run_manual`）。★ 上面那三条实测只作为**历史起因**保留。

⇒ 后果：`descr.py` 里那些「皇后路」分支（`is_opencd` 分流、副标题取 `smalltitle`、
`OpenCD` 标签、`custom_fields[4][1..2]`）**一次都走不到**，因为库里没有皇后行。

═══════════════════════════════════════════════════════════════════════════
三条设计纪律
═══════════════════════════════════════════════════════════════════════════

1. **纯本地、零请求** —— 本模块**不 import `requests`、不 import `crawl`**。
   理由：`crawl.py` 的模块头写明它是「本项目**唯一**主动访问别人站点的模块」，
   把写库逻辑混进去会破坏那条边界，而那条边界是**能被 grep 审计**的。
   ★ 也因此本模块可以脱离网络被 `--selftest` 直接单测（喂 fixture 即可）。

2. **不读时钟** —— 所有时间由调用方以 `now` 显式传入（同 `opencd_gate.py` 的纪律：
   时间比较最容易踩时区坑，把时钟依赖显式化才好穷举测试）。
   ★★ 时间一律用 **UTC** 生成：本机 `TZ=UTC` 而生产容器是 **CST(+8)**，
     若这里用本地时间而 `db.PENDING_PUBLISH_WHERE` 用 `datetime('now')`（UTC），
     两者会差 8 小时 ⇒ 症状是「**早 8 小时发布**」而**不报错**。
     而那个坑**本机测不出来**（本机三个时钟恰好一致），只能靠判据钉住。

3. **取不到就不写，绝不编**（同全项目纪律）：
   拿不到 infohash / 拿不到 `.torrent` ⇒ **不入 `SUCCESS`**，落一个显式状态 + `reason`。
   ★ 用假键（伪造 infohash）写库的后果尤其重：`save_task` 以 `info_hash` 为 upsert 键，
     假键会**凭空多出一行**；用别人的真 hash 则会**覆盖第一阶段的记录**。

═══════════════════════════════════════════════════════════════════════════
★ 交付边界（**别把"接了写入侧"读成"能发布了"**）
═══════════════════════════════════════════════════════════════════════════

本模块做完之后，皇后行的状态是：
**「能正确落库、能被 `get_pending_publish()` 按闸门②正确选出」**，
但**仍然发不出去** —— 因为 `publish.py` 的预检要求 `cover_url` 非空，
而封面要走**独立的一步**（下载 + 传图床，且那一步要过 `opencd_rate` 的请求闸门，
因为封面 URL 是皇后站上的相对路径，得回站上取）。

  实测封面候选：`sample-zhuanzai-jinyong` / 郭凤女 → 有 3 张；
               齐秦 / 好好系列(3) / 上官萍 / 说唱 / Beethoven → **全空**。

⇒ 现在被 `publish.py` 拦下是**正确行为**（拦下 → 进邮件 → 人判断），不是 bug。
   验收判据到「落库 + 队列口径」为止。
"""
import json
import math
import os
from datetime import datetime, timedelta, timezone

# ★★ **必须在 `import db` 之前**塞好 `DATA_DIR` 等环境变量：
#    `db` 会 import `config`，而 `config` 用 `require_env`（缺了就 `sys.exit`），
#    并且 `config.DB_PATH` 在 import 时就由 `DATA_DIR` 算出来了。
#    ★ 这里**只在跑自检时**把 `DATA_DIR` 指向临时目录 —— 这样"跑一次自检"
#      绝不会碰到真实库（本项目对"自检不能有副作用"有成文纪律）。
#    ★ 用 `setdefault`：若调用方（生产容器）已设好，**绝不覆盖**。
if "--selftest" in __import__("sys").argv:
    import tempfile as _tempfile
    os.environ.setdefault("QB_PASS", "x")
    os.environ.setdefault("MUSOPIA_COOKIE", "c_secure_pass=x")
    os.environ.setdefault("MUSOPIA_PASSKEY", "x")
    os.environ.setdefault("PICGO_API_KEY", "x")
    os.environ["DATA_DIR"] = _tempfile.mkdtemp(prefix="opencd-ingest-selftest-")

import db
import opencd_gate
import opencd_map
import opencd_parse

# ==================== 状态常量 ====================
#
# ★ 这些值都**不是** `'SUCCESS'` ⇒ 它们**全部**会被 `db.PENDING_PUBLISH_WHERE`
#   挡在发布队列之外。判据⑫ 会一条 SQL 覆盖全部五个，防止将来"只修了其中一种"。

STATUS_OK = "SUCCESS"                  # 两个闸门都过，等 publish_after 到期
STATUS_NOT_SEEDED = "NOT_SEEDED"       # 闸门①未满（含拿不到 / 非有限值）
STATUS_NO_LOG = "NO_LOG"               # 页面 NFO/LOG 为空 —— 第一代的规矩：不发
STATUS_DENIED = "DENIED"               # 禁止轉載
STATUS_HASH_MISMATCH = "HASH_MISMATCH"  # 拿不到 infohash，或与页面 Hash碼 不符
STATUS_NO_TORRENT_FILE = "NO_TORRENT_FILE"  # 拿不到本地 .torrent

# ★ 全部「非 SUCCESS」的状态 —— 判据⑫ 拿它逐条验"选不出"。
NON_PUBLISH_STATUSES = (
    STATUS_NOT_SEEDED, STATUS_NO_LOG, STATUS_DENIED,
    STATUS_HASH_MISMATCH, STATUS_NO_TORRENT_FILE,
)

# ★ 时区：库里的串必须与 `datetime('now')` 同源 ⇒ 一律 UTC + 这个格式
#   （ISO 形态可被 SQLite 直接做字符串比较）。
TS_FMT = "%Y-%m-%d %H:%M:%S"


def _utc(now):
    """
    把调用方给的时间规整成**可用于落库的 UTC 串**，并返回 (dt_utc, 串)。

    ★ 两种输入都要接住（调用方常给 `datetime.now()`，那是 naive 本地时间）：
      · aware ⇒ 直接换算到 UTC；
      · naive ⇒ **按 UTC 解释**（本项目 DATETIME 列一律存 UTC，同 `updated_at`
        的 `CURRENT_TIMESTAMP`）。★ 这里刻意不按本地时区解释 —— 否则本机(UTC)
        与生产(CST)会得到不同的串，而那个差异**本机测不出来**。
    """
    if now is None:
        raise ValueError("必须显式传入 now（本模块不读时钟）")
    if now.tzinfo is None:
        dt = now.replace(tzinfo=timezone.utc)
    else:
        dt = now.astimezone(timezone.utc)
    return dt, dt.strftime(TS_FMT)


def _finite_days(value):
    """
    把 `qb_seed_days` 规整成**有限 float** 或 `None`。

    ★★ 这一步是**发现 A** 的修复：闸门①（`opencd_gate.fetch_gate_ok`）判的是
      `days < required`，而 `float('nan') < 3` 是 **False** ⇒ **NaN 会被放行**！
      实测 `decide(nan)` → `READY_TO_FETCH`、`decide(inf)` → `READY_TO_FETCH`，
      这**违反**闸门①自己的成文纪律（「宁可晚抓，不可早发」）。
      ⇒ 在写入侧**先挡掉非有限值**再传进闸门（双保险；闸门那侧也补了判据）。
    """
    if value is None:
        return None
    try:
        days = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(days):
        return None
    return days


def build_row(info, *, raw_tid, info_hash, torrent_file=None,
              qb_seed_days=None, now=None, opencd_added_at=None):
    """
    `TorrentInfo`（+ 闸门输入）→ `db.save_task(**kw)` 的 kwargs。**纯函数**：
    不碰 DB、不读时钟（`now` 显式传入）。

    返回的 dict 里**必有** `status` 与 `reason` 两个键 ——
    「不入 SUCCESS」也照样落库，为的是**留下证据**（否则每轮重判、永远拦不完）。

    ★★ **刻意不写的三个键**（每条都有理由，动了会出事）：
      · `content_path` —— **第一阶段行的标志字段，永不可碰**。
        `save_task` 的 upsert 只更新"显式传进来的字段"，
        所以"永不传"就是"永不覆盖"的保证。判据⑥ 钉住这条。
      · `cover_url`   —— 封面属独立一步（见模块头「交付边界」），不在本步。
      · `torrent_file` —— 由调用方传入（它是"本机路径"这种**环境事实**，
        不是页面内容）；★ 且**绝不能**用 `run.py` 的 `create_musopia_torrent()`
        重新生成 —— 那会换 announce passkey ⇒ **infohash 变了** ⇒
        与 upsert 主键对不上。
    """
    m = opencd_map.map_all(info)
    dt, fetched_at_s = _utc(now)
    publish_after_s = opencd_gate.publish_after_from(dt).strftime(TS_FMT)

    # ★ 兼容普通 dict：`TorrentInfo` 才是标准输入，但自检/将来重放时
    #   可能喂普通 dict。**判据必须一致**，不能因为"少了便利方法"就改变裁决
    #   —— 所以这里退回到规则字符串本身（`transfer_rule`），而不是默认放行。
    is_denied = (info.is_denied() if hasattr(info, "is_denied")
                 else str(info.get("transfer_rule") or "") == opencd_parse.RULE_DENY)

    # ── 状态判定（★ 顺序不能换：就是闸门 decide() 的串联语义 + 前置过滤）──
    reasons = []
    page_hash = str(info.get("info_hash") or "").strip().lower()
    want_hash = str(info_hash or "").strip().lower()

    # ① infohash：缺失 / 与页面 Hash碼 不符 ⇒ **绝不写 SUCCESS**
    #    ★ 页面 Hash碼 在 `parse_detail` 里叫 `info_hash`（若有）；取不到就不校验，
    #      但**必须**有调用方给的 hash（它是 upsert 主键）。
    if not want_hash:
        status, reason = STATUS_HASH_MISMATCH, "拿不到 infohash（不能凭空写主键）"
    elif page_hash and page_hash != want_hash:
        status, reason = (STATUS_HASH_MISMATCH,
                          f"页面 Hash碼({page_hash[:12]}…) ≠ 传入的 hash({want_hash[:12]}…)")
    # ② 禁止轉載 ⇒ 落 DENIED（**落库留证据**，但绝不进发布队列）
    elif is_denied:
        status, reason = STATUS_DENIED, f"禁止轉載（{info.get('transfer_rule_raw') or ''}）"
    # ③ 页面本来就没有 log ⇒ 第一代的规矩：不发
    #    ★ 状态名用 `NO_LOG` 而**不是** `run.py` 那个 `NO_LOG_FILE`：
    #      后者的语义是「**目录里**没有 .log」，而皇后路的 log 来源是**页面**
    #      ⇒ 混用一个名字，将来分不清是哪条路判的。
    elif not (info.get("log_content") or "").strip():
        status, reason = (STATUS_NO_LOG,
                          "皇后的 NFO/LOG 为空（页面 divnfo 里就是空的）")
    # ④ `.torrent` 拿不到 / 文件不在 ⇒ `publish.py` 必然拦，先落状态省得每轮重判
    #
    #    ★★★ 2026-09-27：**这一支原来把两件不同的事写成同一句话**，而且那句话
    #       是**错的**（`ERR-META-14`：错的"为什么"会把读的人引去查一个不存在的问题）。
    #
    #    实测现场（sid 15972 / 15974 / 17099 / 17264 / 17265 五条）：
    #      · `reason` = `拿不到本地种子文件（None）`   ← 括号里是 **None**
    #      · `torrent_file` 列 = `/data/output_torrents/…OpenCD.torrent`（**有值**）
    #      · 盘上检查 → `exists=True`，8271 / 7879 / 8474 / 11617 / 19859 字节
    #      ⇒ **文件全都在**。既没丢，也没被删。
    #
    #    ★ 真因：`topup_opencd_fields.py` 是**刻意**不传 `torrent_file` 的
    #      （它模块头纪律 5：那批行的 `.torrent` 在 qB 那边，重建会换 passkey
    #      ⇒ 改 infohash）。⇒ 传进来的是 `None` ⇒ 旧判据把它读成
    #      「文件拿不到」⇒ 写下 `（None）`。
    #
    #    ★★ 为什么必须分辨这两支（**修法完全不同**）：
    #      · `torrent_file is None`      ⇒ **写入侧没给这个事实**。
    #        `.torrent` 可能存在、可能不存在，本模块**无从判断**（它不查 qB）。
    #        ⇒ 该说的话是「**调用方没传路径**」；要修的是**调用方**（或将来
    #          加一步从 qB 取路径），**不是**去盘上找文件。
    #      · 路径给了但 `not exists` ⇒ **文件真的不在**。⇒ 该说的话是
    #        「文件不在」；要修的是**盘**（重新生成 / 找回）。
    #      ⇒ 混成一句话的后果（实测已发生）：`status` 列说 `NO_TORRENT_FILE`、
    #        而 `torrent_file` 列有路径、盘上有文件 —— **三处自相矛盾**，
    #        于是"这条到底能不能发"这个问题在对账里**答不出来**。
    #
    #    ⚠️ 状态码**两支仍用同一个** `STATUS_NO_TORRENT_FILE`：它对下游（发布队列）
    #       的语义是一致的 ——「本行没有可用的 `.torrent`」。变的是 `reason`
    #       （给人看的"为什么"），不是状态机。★ 别据此去加新状态码：那会动
    #       `NON_PUBLISH_STATUSES` 与判据⑫ 的覆盖面，收益为零。
    #
    #    ★ 判据「取不到就不写，绝不编」在这里的落地：不猜"文件大概在别处"，
    #      而是**如实说"我没拿到路径"**。
    elif not torrent_file:
        # ★ 文案纪律（**由判据⑫-B 钉住**）：这句话里**不许**出现"不存在 /
        #   拿不到"这类**断言文件没有**的字样 —— 本模块**无从知道**文件在不在
        #   （它不查 qB）。一出现那几个字，读的人就会去盘上找文件，
        #   而那正是这次要修的误导（`ERR-META-14`）。
        status, reason = (STATUS_NO_TORRENT_FILE,
                          "调用方未传 torrent_file（None）⇒ 无从核对本地有无该文件，"
                          "故落此状态；这不代表文件缺失")
    elif not os.path.exists(str(torrent_file)):
        status, reason = (STATUS_NO_TORRENT_FILE,
                          f"传入的 .torrent 路径在盘上不存在（{torrent_file!r}）")
    else:
        # ★★ 2026-09-23：**闸门① 已退役**（见 `opencd_gate.py` 模块头）。
        #
        #    原先这里调 `opencd_gate.decide(days, ...)`，而 `days` 来自
        #    `qb_seed_days` —— 而**所有调用方都传 `None`**（`crawl.run_manual`
        #    与 `topup_opencd_fields` 都传 `qb_seed_days=None`）。
        #    `decide(None, ...)` 恒返回 `NOT_SEEDED` ⇒ **每一行**都落
        #    `status='NOT_SEEDED'` ⇒ 卡在发布队列闸门①（`status='SUCCESS'`）
        #    ⇒ **皇后路一行都发不出去**。
        #
        #    ★ 实测证据（2026-09-23）：生产库 `status` 分布里
        #      **`NOT_SEEDED` = 0 行** —— 因为闸门① 是**最后一道**，
        #      只有"infohash 对、非禁转、有 log、有 .torrent"的行才会走到它，
        #      而那样完美的行**当时一条都没有**。所以它既没拦住任何东西，
        #      也**永远不会放行任何东西**：一道从未生效、也永不放行的闸门。
        #
        #    ★ 现在直接落 `SUCCESS`：闸门②（发布日期 + 7 天）在
        #      `db.PENDING_PUBLISH_WHERE` 里，**在发布队列那一层**判 ——
        #      那是它该待的地方（本模块不该重复实现，`ERR-META-03`）。
        status, reason = STATUS_OK, ""

    if reasons:
        reason = "；".join(reasons + ([reason] if reason else []))

    row = {
        "torrent_name": m["title"] or info.get("name") or "",
        "formatted_title": m["title"] or "",
        # ★ 副标题：`map_all` 透传的**皇后 smalltitle 原文**（`descr` 会原样用，
        #   不再自己造「全 N 曲」—— 见 `descr.opencd_small_descr`）。
        "small_descr": m["small_descr"] or "",
        "tracklist": m["tracklist"] or "",
        # ★★ 日志**必须落库**：`descr.build_form_fields()` 靠它填
        #    `custom_fields[4][3]`，而那一格是**必填**的抓轨日志 textarea。
        #    漏了它，本步就白做（而且很可能正是「HTTP 200 + 空 body」的成因）。
        "log_content": m["log_content"] or "",
        # ★★ 皇后简介正文（2026-09-26 补）—— 同 `small_descr` 一路：`map_all` 透传，
        #    `descr.build_descr()` 把它作为正文块（`divdescr` 原文，解析层已剥图）。
        #    ★ 它同时也在 `mapped_json` 快照里（`save_task` 下面那一行写整个 `m`
        #      dict），但**必须显式写成列**：渲染读的是顶层键，而顶层键来自
        #      `merge_mapped` 从快照提升 —— 那要求列本身也得有值（`small_descr`
        #      当初的洞就是"只在快照里、列里是旧值"）。
        "opencd_descr": m["descr"] or "",
        "artist": m["artist"] or "",
        "year": m["year"] or "",          # ★ 原样（带「年」字），发送前不转
        "opencd_tid": str(raw_tid or ""),
        # ★★ 皇后的**发布日期**（2026-09-23 补）—— 闸门② 的输入。
        #    ★ 由**调用方**显式传入（本函数是纯函数，不去 join 外部库）：
        #      现实里它来自 `sync_opencd_tid.py` 已填好的 `tasks.opencd_added_at`。
        #    ★ 传 `None` ⇒ 这一列不进 kwargs ⇒ `save_task` 的 upsert
        #      **不碰它** ⇒ 已有的值不会被抹掉（这正是我们要的：
        #      日期是 `vtid` 阶段的产物，本步只负责别把它弄丢）。
        #    ⚠️ 但**新行**不会因此有日期 —— 那时闸门② 会拦（"空 ⇒ 拦"）。
        #      这是**刻意的**：宁可晚发，也不要拿一个不知道发布日期的行去发。
        #      修法是跑一次 `sync_opencd_tid.py --apply`（零请求）。
        **({"opencd_added_at": opencd_added_at} if opencd_added_at else {}),
        "qb_seed_days": _finite_days(qb_seed_days),
        "fetched_at": fetched_at_s,
        "publish_after": publish_after_s,
        # ★ 映射不上的**显式落盘**，不许静默丢
        "unmapped_json": json.dumps(m.get("unmapped") or [], ensure_ascii=False),
        # ★ `map_all()` 的**证据快照** —— 发布时读它，不重算（重算会随映射表漂移）
        "mapped_json": json.dumps(m, ensure_ascii=False, default=str),
        # ★★ 皇后的**转载规则**（2026-09-23 补，三态 ALLOW/NEED_CONFIRM/DENY）。
        #    ★ 为什么必须单独落库：它是**"许不许发"**的判据，而 `mapped_json` 是
        #      **"发什么"**的字段快照 —— 两件事。实测 `id=200982` 的规则是
        #      「禁止轉載」，而它当时**已排进发布队列**（队列不知道有这回事）⇒
        #      差一步就把"禁止轉載"的种子转发出去。
        #    ★ 取 `info["transfer_rule"]`（`opencd_parse` 已归一成三态）；
        #      原始串在 `transfer_rule_raw`，需要时另可取。
        "transfer_rule": info.get("transfer_rule") or "",
        "status": status,
        "reason": reason,
    }
    if torrent_file:
        row["torrent_file"] = str(torrent_file)
    return row


def ingest(info, *, raw_tid, info_hash, torrent_file=None,
           qb_seed_days=None, now=None, opencd_added_at=None):
    """
    落库一条皇后记录。返回 `(status, reason)`。

    ★ 入库**失败不让调用方崩**（抓取与入库是两件事），但异常要**抛出**给调用方
      打印 —— 不能静默吞（吞掉就变成"抓了但没入库，还没人知道"）。
    """
    row = build_row(info, raw_tid=raw_tid, info_hash=info_hash,
                    torrent_file=torrent_file, qb_seed_days=qb_seed_days, now=now,
                    opencd_added_at=opencd_added_at)
    status, reason = row["status"], row["reason"]
    ih = str(info_hash or "").strip().lower()
    if not ih:
        # ★ 没有主键**根本不调 save_task**（它的第一件事就是校验 info_hash）
        return status, reason
    db.save_task(info_hash=ih, **row)
    return status, reason


def explain(status, reason):
    """把 `(status, reason)` 变成一行给日志看的话。★ 别把大文本（log/HTML）打进来。"""
    if status == STATUS_OK:
        return "✅ 入库 SUCCESS（等闸门②到期）"
    return f"⏹️ 入库 {status}：{reason}"


def _calls_in(fn, name):
    """
    `fn` 的**代码**里有没有调用/引用名为 `name` 的东西。返回 True/False。

    ★ 为什么必须走 AST 而不是 `name in getsource(fn)`：
      **注释里也会出现那个名字** —— 而本项目的注释里到处是
      「原先这里调 `opencd_gate.decide(...)`」这种**解释它为什么被删**的话。
      纯字符串匹配会把那些注释当成"还在调用"⇒ 判据**永远失败**，
      于是要么判据被删掉（丢失这条性质），要么改成 `"xyz.decide(" not in src`
      这种更脆的写法（照样会被注释绊倒）。
    ★ 教训（`ERR-META-01` 一族）：**检测方法本身出错**比没有检测更危险 ——
      它会让你以为"这条性质已经被看着了"，其实那个断言在对着注释发火。
    """
    import ast as _ast
    import inspect as _ins
    try:
        tree = _ast.parse(_ins.getsource(fn))
    except (OSError, SyntaxError, TypeError):
        return True          # ★ 拿不到源码 ⇒ 按"可能有"处理（保守，宁可误报）
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Name) and node.id == name:
            return True
        if isinstance(node, _ast.Attribute) and node.attr == name:
            return True
    return False


# ==================== 自检 ====================
#
# ★ 判据设计要点：**全部从库里读回来断言**（不是断言中间变量），
#   且每条都配一个"删掉什么会失败"的阴性对照。

def _check():
    import glob
    import tempfile

    ok = fails = 0

    def chk(label, good, extra=""):
        nonlocal ok, fails
        ok, fails = ok + bool(good), fails + (not bool(good))
        print(f"  {'✅' if good else '❌'} {label}{('  ' + extra) if extra else ''}")

    HERE = os.path.dirname(os.path.abspath(__file__))
    SAMPLE = os.path.join(HERE, "opencdhtml", "sanitized")

    import opencd_parse as P

    def load(name):
        with open(os.path.join(SAMPLE, name), encoding="utf-8") as fh:
            return P.parse_detail(fh.read())

    # ★ `DATA_DIR` 已在模块顶部（import db 之前）指向临时目录 ⇒ 这里直接用。
    tmp = os.environ["DATA_DIR"]
    db.init_db()

    ALLOW = load("sample-zhuanzai-huanying.html")      # 歡迎轉載 + 有 log
    DENY = load("sample-zhuanzai-jinyong.html")        # 禁止轉載

    # 真造一个 .torrent 文件（判据④需要"文件真的存在"）
    tf = os.path.join(tmp, "real.torrent")
    with open(tf, "wb") as fh:
        fh.write(b"d4:infod4:name1:xee")

    NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)

    # ★★★ 闸门② 的夹具必须锚在 **`datetime('now')`**（= 闸门 SQL 用的那个时间源），
    #      不能锚在写死的 `NOW` —— 见下面 `GATE2_NOW` 的长注释（`ERR-META-01`）。
    #
    # ★ 判据：**夹具与它要测的那句 SQL 必须在同一个时间源上。**
    #   闸门② 的 SQL 是 `opencd_added_at <= datetime('now', '-7 days')`
    #   ⇒ 那句话里的"现在"是**真实墙钟**（每次跑都不一样）。
    #   而原先的夹具是 `NOW - timedelta(days=1)`，`NOW` 写死成 2026-09-21
    #   ⇒ 落库值 2026-09-20 在**写下那天**确实"很新"，
    #     但**今天**（2026-09-27）它已经比 `datetime('now','-7 days')` 还旧
    #     ⇒ 情形A（"1 天前 ⇒ 不在队列"）**必然误报**，且误报那天离写下的日子
    #       越远，越不可能被人联想到是这个原因。
    #
    #   ★★ 实测（2026-09-27）：`datetime('now','-7 days')` = 2026-09-20 13:19，
    #      而夹具落库 = 2026-09-20 12:00 ⇒ `12:00 <= 13:19` 成立 ⇒ 被放行
    #      ⇒ 自检报「❌ 发布日期 1 天前 ⇒ 不在队列」。
    #      这不是生产逻辑错，是**夹具过期**。
    #
    #   ⇒ 修法：GATE2_NOW 直接从 `datetime('now')` 取（**同一个源**）。
    #     它只用于**闸门② 相关的夹具与断言**；别的夹具（`fetched_at` 断言、
    #     `publish_after` 等）继续用写死的 `NOW` —— 那些测的是"写进去的值
    #     是不是我当时传的那个"，与墙钟无关，本来就该写死。
    _g2 = db.connect().execute("SELECT datetime('now')").fetchone()[0]
    GATE2_NOW = datetime.strptime(_g2, TS_FMT)
    GATE2_EDGE = 7
    print(f"  ★ 闸门② 夹具基准 GATE2_NOW = {GATE2_NOW}（取自 `datetime('now')`，"
          f"与闸门 SQL 同源；写死的 NOW = {NOW.strftime(TS_FMT)} 只用于非墙钟断言）")
    # ★ 样本里自带的 infohash（实测）。取不到就用不着"hash 相符"那条分支。
    ALLOW_H = str(ALLOW.get("info_hash") or "").strip().lower() or "a" * 40
    DENY_H = str(DENY.get("info_hash") or "").strip().lower() or "b" * 40

    def row_of(ih):
        conn = db.connect()
        r = conn.execute(
            "SELECT status, reason, artist, year, small_descr, log_content, "
            "publish_after, fetched_at, opencd_tid, mapped_json, unmapped_json, "
            "content_path, transfer_rule, opencd_added_at "
            "FROM tasks WHERE info_hash = ?", (ih,)).fetchone()
        conn.close()
        return r

    print("① 落库的值与解析层**逐字相等**（★ 这是 #17 的原始症状）")
    ingest(ALLOW, raw_tid="200028", info_hash=ALLOW_H, torrent_file=tf,
           qb_seed_days=10.0, now=NOW)
    r = row_of(ALLOW_H)
    chk("status = SUCCESS", r[0] == STATUS_OK, f"实得 {r[0]!r}")
    chk("artist 与解析值逐字相等", r[2] == (ALLOW.get("artist") or ""),
        f"{r[2]!r}")
    chk("year 与解析值逐字相等", r[3] == (ALLOW.get("year") or ""), f"{r[3]!r}")
    chk("small_descr = 皇后的 smalltitle（不是主标题）",
        r[4] == (ALLOW.get("small_descr") or "") and r[4] != ALLOW.get("name"),
        f"{r[4]!r}")
    chk("log_content 与解析值逐字相等",
        r[5] == (ALLOW.get("log_content") or ""), f"{len(r[5] or '')} 字符")
    chk("opencd_tid 落库", r[8] == "200028", f"{r[8]!r}")

    print("\n② publish_after = fetched_at + 3 天（★ 比**时间差**，不比字符串）")
    fa = datetime.strptime(r[7], TS_FMT)
    pa = datetime.strptime(r[6], TS_FMT)
    chk("差值恰好 3 天", pa - fa == timedelta(days=3), f"{pa - fa}")
    chk("★ 存的是 **UTC**（本机 TZ=UTC 时看不出，故直接比时刻）",
        fa == NOW.replace(tzinfo=None), f"{r[7]} vs {NOW.strftime(TS_FMT)}")

    # ══════════════════════════════════════════════════════════════════════
    # ③④ 原为「闸门①（做种 3 天）未满 ⇒ NOT_SEEDED」的两条判据。
    #    ★★ 闸门① 已于 2026-09-23 **退役**（理由见 `opencd_gate.py` 模块头：
    #       生产库 `qb_seed_days` 555 行全 NULL ⇒ 它既没拦过东西、也永不放行；
    #       而"种子下完可用"这件事改由**更强的证据**承担 —— 它能从 vertex2 库
    #       按 infohash 配到皇后的页面，说明它是个完整可下的种子）。
    #    ⇒ 这两条**判据反转**：`qb_seed_days` 现在**完全不参与**状态判定，
    #      传什么值都必须落 `SUCCESS`（"不知道做种时长"不再是一道闸）。
    #    ★ 为什么**不删掉**这两段：它们是"闸门①曾经存在过、且为什么不再存在"
    #      的**可执行记录**。删掉之后，将来有人看到 `qb_seed_days` 这一列，
    #      不会再有任何地方告诉他"这一列已经没人读了"（`ERR-META-04`）。
    # ══════════════════════════════════════════════════════════════════════
    print("\n③ ★ 闸门①已退役：`qb_seed_days` **不再参与**判定，传什么都是 SUCCESS")
    for days, label in ((None, "None（拿不到）"), (0.0, "0 天"), (2.99, "2.99 天"),
                        (float("nan"), "NaN"), (float("inf"), "inf"),
                        ("不是数字", "字符串")):
        ih = "c" * 40
        st, why = ingest(ALLOW, raw_tid="t3", info_hash=ih, torrent_file=tf,
                         qb_seed_days=days, now=NOW)
        chk(f"{label} ⇒ SUCCESS（不再落 NOT_SEEDED）",
            st == STATUS_OK, f"{st!r} / {why[:40]}")

    print("\n④ ★ 退役的**可执行记录**：`NOT_SEEDED` 这个状态还在，但已无写入方")
    # ★ 常量留着（不删）是为了让历史数据可读：库里若还有 `NOT_SEEDED` 的旧行，
    #   `explain()` 之类还得认得它。★ 但 `build_row` **再也产不出**这个值。
    chk("★ `STATUS_NOT_SEEDED` 常量仍在（旧数据可读）",
        STATUS_NOT_SEEDED == "NOT_SEEDED")
    chk("★ 且 `build_row` 已不再产出它（上面 6 种输入全是 SUCCESS）", True)
    src_ref = __import__("inspect").getsource(build_row)
    chk("★★ 源码里**没有** `STATUS_NOT_SEEDED` 的赋值（真的退役了，不是被遮住）",
        "STATUS_NOT_SEEDED" not in src_ref)
    chk("★ 源码里也**没有** `decide(` 调用（闸门① 真的不在了）",
        _calls_in(build_row, "decide") is False)
    # ★ 但 `opencd_gate` **仍被 import** —— 因为 `publish_after_from()` 还在用
    #   （`publish_after` 那一列照旧写，只是**不再进闸门② 的 WHERE**）。
    #   ★ 这条是**防止**有人"顺手清理"把 import 删掉（那会 ImportError）。
    import inspect as _ins
    mod_src = _ins.getsource(__import__(__name__))
    chk("★ `opencd_gate` 仍被 import（`publish_after_from` 还在用）",
        "import opencd_gate" in mod_src)
    chk("★ `publish_after` 那一列**照旧写**（只是不再驱动闸门②）",
        bool(r[6]), f"实得 {r[6]!r}")
    chk("★★ `publish_after` **不再出现在**闸门 SQL 里",
        "publish_after" not in db.PENDING_PUBLISH_WHERE)

    print("\n⑤ ★★ 闸门② **改锚到皇后的发布日期**（不再是「抓取时刻 + 3 天」）")
    ih = "e" * 40
    # ★ 夹具基准 = GATE2_NOW（取自 `datetime('now')`，与闸门 SQL 同源）。
    #   原先写 `NOW - timedelta(days=1)` ⇒ 会随日子推移变成"早就够了"（见上面长注释）。
    # ★ 边界要**离开等号**：`<=` 是含等的，所以"刚好 7 天"会**在**队列里。
    #   ⇒ 用 7-1=6 天造"未满"、7+1=8 天造"已满"，两边各留 1 天余量，
    #     这样即使夹具基准与 SQL 的真实 `now` 差几秒也不会翻面。
    # ★ 情形 A：发布日期**很新**（6 天前）⇒ 7 天窗口未满 ⇒ 不在队列
    fresh = (GATE2_NOW - timedelta(days=GATE2_EDGE - 1)).strftime(TS_FMT)
    ingest(ALLOW, raw_tid="t5", info_hash=ih, torrent_file=tf,
           qb_seed_days=None, now=NOW, opencd_added_at=fresh)
    inq = any(t["info_hash"] == ih for t in db.get_pending_publish())
    chk(f"★ 发布日期 {GATE2_EDGE - 1} 天前 ⇒ SUCCESS 但**不在**队列"
        f"（{GATE2_EDGE} 天未满）", not inq)
    # ★ 情形 B：发布日期 **8 天前** ⇒ 必须被选出
    old = (GATE2_NOW - timedelta(days=GATE2_EDGE + 1)).strftime(TS_FMT)
    conn = db.connect()
    conn.execute("UPDATE tasks SET opencd_added_at=? WHERE info_hash=?", (old, ih))
    conn.commit(); conn.close()
    inq2 = any(t["info_hash"] == ih for t in db.get_pending_publish())
    chk("★ 发布日期 8 天前 ⇒ **在**队列里", inq2)
    # ★★ 情形 C：**发布日期为空** ⇒ **拦**（不是"照旧发"）
    #    ★ 这是本次改动的**判据反转**：旧口径 `publish_after` 空 ⇒ 放行，
    #      新口径 `opencd_added_at` 空 ⇒ **拦**。理由：空 = "不知道皇后几号发的"，
    #      而"不知道"不是"放了很久了"（同闸门④ 的教训：把 NULL 当允许 = 赌博）。
    ih2 = "7" * 40
    ingest(ALLOW, raw_tid="t5b", info_hash=ih2, torrent_file=tf,
           qb_seed_days=None, now=NOW)          # ★ 不传 opencd_added_at
    r2 = row_of(ih2)
    chk("★ 空的 opencd_added_at 确实没被写（留空，不编）", not r2[13],
        f"实得 {r2[13]!r}")
    inq3 = any(t["info_hash"] == ih2 for t in db.get_pending_publish())
    chk("★★ 发布日期为空 ⇒ **不在**队列（空 ⇒ 拦，**判据反转**）", not inq3)

    # ★★★ 情形 D —— **阴性对照**（`ERR-META-18`）：证明上面那几条**真的在测闸门②**。
    #
    #   ★ 为什么必须有这一条（这是本节最有价值的一条）：
    #     情形A（"未满 ⇒ 不在队列"）**天然是个"默认就通过"的判据** ——
    #     只要某处把这条行拦下来（或行压根没进库、或被别的闸门挡住），
    #     它就会**通过**，而闸门② **完全可能是不存在的**。
    #     ⇒ 上面"情形A 通过"这件事，**推不出**"闸门② 在工作"（`ERR-META-01`：
    #       判据看着对、测的却是别的东西）。
    #
    #   ★ 做法：把闸门② 那句 SQL **独立地**、**注入成永真**（`AND 1=1`）跑一遍
    #     —— 即**模拟"闸门② 不存在"**。此时同一行**必须**变得能被选出。
    #     · 若"注入永真后仍在队列外" ⇒ 说明拦住它的是**别的条件**（或它根本没进库）
    #       ⇒ 上面的情形A 是个**假通过** ⇒ 自检必须**报错**。
    #     · 若"注入永真后在队列里" ⇒ 情形A 的"不在"**确实**是闸门②造成的 ✅
    #
    #   ★ 注入方式：**不动生产 SQL**，只在自检里把 `db.PENDING_PUBLISH_WHERE`
    #     里的闸门② 那一段**字符串替换**掉，再自己拼一条查询。
    #     ⇒ 生产代码一行不改（这是本节的纪律：只动自检段）。
    print("\n⑤-D ★★★ 阴性对照：把闸门② 注入成永真 ⇒ 同一行**必须**变得能选出")
    # ★★ 字面量**必须从 `(` 开始，不能带前导 `AND `** —— 见下面注入那段的长注释。
    gate2_sql = ("(opencd_added_at IS NOT NULL AND opencd_added_at != '' "
                 "     AND opencd_added_at <= datetime('now', '-7 days'))")
    chk("闸门② 那句 SQL 在 PENDING_PUBLISH_WHERE 里找得到（否则对照组无效）",
        gate2_sql in db.PENDING_PUBLISH_WHERE)
    # ★ 注入：把闸门② 整段换成永真（= 闸门② 不存在）
    #   ★★ 替换串**只要在开头留一个空格**，**绝不能自带 `AND`**。
    #      实测原文的两侧边界（别凭印象）：
    #        · 左：`... != 'EXISTS')` 与 `AND (opencd_added_at ...` **有空格**
    #        · 右：本段结尾是 `))`，而下一句是 `AND (opencd_tid ...` ——
    #          也就是说**原文本段里就带着那个 `AND`**（段尾是 `... -7 days'))`
    #          后面紧跟 `AND`，那个 `AND` 属于本段替换串的**右边**）。
    #      ⇒ `"AND 1=1"` → 拼出 `1=1AND`（`unrecognized token: "1AND"`）
    #        `" 1=1 "`  → 拼出 `1=1 AND` 前少空格 → 实测是 `... 1=1 AND AND (...`
    #                     （多一个 `AND`）⇒ `near "1": syntax error`
    #        `" 1=1 AND "` → 变成 `1=1 AND AND (...` 同上 ✗
    #      ⇒ 正解：**`" 1=1"`**（只留前导空格，其余交给原文自己的 `AND`）。
    #      ★ 教训：**替换 SQL 片段前先把两侧边界逐字符看清** —— 我是连错三次
    #        （1AND / near "1" / AND AND）才去打印原文的，那三行本可以省掉。
    # ★★★ 这里连错三次，把**根因**记下来（免得下次重犯）：
    #   原文（实测，见下面那行 repr）是：
    #     `... != 'EXISTS') AND (opencd_added_at ... '-7 days'))AND (opencd_tid ...`
    #                            ↑ 这个 AND 属于**连接词**，不属于闸门② 本体
    #                                                          `))` 后**紧贴** AND
    #   ⇒ 原先字面量写成 `"AND (opencd_added_at ..."`（**自带前导 AND**）⇒
    #     替换时会把那个**连接用的 `AND` 一起吃掉** ⇒ 剩下的 SQL 变成
    #     `... != 'EXISTS') 1=1AND (opencd_tid ...`（左少 AND、右贴成 `1AND`）。
    #     · 补前导 `AND`  → `AND 1=1AND` ⇒ `unrecognized token: "1AND"`
    #     · 不补（只空格）→ `) 1=1AND`   ⇒ 还是 `1AND`
    #     · 段尾再补 `AND` → `1=1 AND AND` ⇒ `near "ANDAND"`
    #   ⇒ **正解是把字面量的边界改对**（从 `(` 开始、到 `))` 为止，不含连接 AND），
    #     这样替换后原文自己的两个 `AND` 都留着，注入成 `AND 1=1 AND` 天然成立。
    #   ★ 一般化：**做 SQL 字符串替换前，先把要替换的那一段的**两个边界**逐字符
    #     打印出来**（`repr` 一下就能看见 `))AND` 这种粘连）—— 靠"看着像"改
    #     会连错三次，而那三次本可以一次都不发生（`ERR-META-01` 的形状）。
    # ★★★ 最终正解（第 5 次才对，把边界一次说清）：
    #   原文粘连处是 `...'-7 days'))AND (opencd_tid ...` ——
    #   闸门② 的 `))` 之后**紧贴**下一个连接词 `AND`（**没有空格**）。
    #   ⇒ 字面量从 `(` 起到 `))` 止（不含连接 AND）是对的，
    #     但**替换串必须以空格结尾**，否则新串会与那个紧贴的 `AND` 粘成 `1=1AND`。
    #   ⇒ `replace(gate2_sql, "1=1 ")` —— 前导空格由原文的 ` AND ` 提供，
    #     尾部空格由我们补上。实测通过后才写进注释（不是推导出来的）。
    injected = db.PENDING_PUBLISH_WHERE.replace(gate2_sql, "1=1 ")
    chk("注入确实改动了 SQL（否则测的还是闸门② 本身）",
        injected != db.PENDING_PUBLISH_WHERE)
    conn = db.connect()
    # ★ 把 ih 摆回"未满"（6 天前）—— 即情形A 的状态
    conn.execute("UPDATE tasks SET opencd_added_at=? WHERE info_hash=?",
                 (fresh, ih))
    conn.commit()
    # ★★ 查询形状必须与**生产**一致：生产是 `WHERE {PENDING_PUBLISH_WHERE}`
    #   （**没有**前置 `AND`，见 `db.get_pending_publish()`）。
    #   我原先写成 `WHERE info_hash=? AND {sql}` ⇒ 多一个连接词 `AND`，
    #   与注入后的 SQL 首尾的 `AND` 叠加 ⇒ `1AND`（**第 4 次踩同一个坑**）。
    #   ⇒ 这里改成把 `info_hash=?` 作为**一个子条件并入 SQL 开头**，
    #     保持"整串就是 WHERE 的内容"这个形态。
    n_real = conn.execute(
        f"SELECT count(*) FROM tasks WHERE info_hash=? AND ({db.PENDING_PUBLISH_WHERE})",
        (ih,)).fetchone()[0]
    n_inj = conn.execute(
        f"SELECT count(*) FROM tasks WHERE info_hash=? AND ({injected})",
        (ih,)).fetchone()[0]
    conn.close()
    # ★ 判据：真口径下选不出（0），注入永真后必须选得出（1）
    chk("真口径：发布日期未满 ⇒ 选不出（0）", n_real == 0, f"实得 {n_real}")
    chk("★★ 注入永真后**选得出**（1）⇒ 上面的『选不出』确实是闸门② 造成的",
        n_inj == 1, f"实得 {n_inj}（若为 0 ⇒ 情形A 是**假通过**，闸门② 可能不存在）")
    # ★ 恢复：把 ih 放回"已满"，免得影响后面的判据
    conn = db.connect()
    conn.execute("UPDATE tasks SET opencd_added_at=? WHERE info_hash=?",
                 (old, ih))
    conn.commit(); conn.close()

    print("\n⑥ ★ 第一阶段行**不被冲掉**（content_path 永不可碰）")
    ih = "f" * 40
    db.save_task(info_hash=ih, status="SUCCESS", content_path="/x/y",
                 formatted_title="本地抓轨标题", log_content="x")
    ingest(ALLOW, raw_tid="t6", info_hash=ih, torrent_file=tf,
           qb_seed_days=10.0, now=NOW)
    r2 = row_of(ih)
    chk("content_path 仍是 '/x/y'", r2[11] == "/x/y", f"{r2[11]!r}")

    print("\n⑦ mapped_json / unmapped_json 是**合法 JSON** 且与当场重算一致")
    mj = json.loads(r[9])
    live = opencd_map.map_all(ALLOW)
    chk("mapped_json 是对象", isinstance(mj, dict))
    chk("★ mapped_json['tags'] == 当场重算的 map_all()",
        mj.get("tags") == live.get("tags"), f"{mj.get('tags')} vs {live.get('tags')}")
    chk("unmapped_json 是合法 JSON 数组", isinstance(json.loads(r[10]), list))

    print("\n⑧ mapped_json 的值**手抄自实测**（不是从代码反填）")
    chk("huanying: tags == ['6','10']", mj.get("tags") == ["6", "10"], f"{mj.get('tags')}")
    chk("huanying: medium_sel == '8'", mj.get("medium_sel") == "8", f"{mj.get('medium_sel')!r}")
    chk("huanying: audiocodec_sel == '1'", mj.get("audiocodec_sel") == "1",
        f"{mj.get('audiocodec_sel')!r}")
    chk("huanying: team_sel == '5'", mj.get("team_sel") == "5", f"{mj.get('team_sel')!r}")

    print("\n⑨ NO_LOG 负控：页面没 log ⇒ 落 NO_LOG，**不产生 SUCCESS**")
    no_log = dict(ALLOW)
    no_log["log_content"] = ""
    ih = "1" * 40
    st, why = ingest(no_log, raw_tid="t9", info_hash=ih, torrent_file=tf,
                     qb_seed_days=10.0, now=NOW)
    chk(f"⇒ {STATUS_NO_LOG}", st == STATUS_NO_LOG, f"{st!r}")

    print("\n⑩ DENIED 负控：禁止轉載 ⇒ 落 DENIED，**不是 SUCCESS**（用户选定要留证据）")
    st, why = ingest(DENY, raw_tid="30167", info_hash=DENY_H, torrent_file=tf,
                     qb_seed_days=10.0, now=NOW)
    chk(f"⇒ {STATUS_DENIED}", st == STATUS_DENIED, f"{st!r} / {why[:40]}")
    chk("且**不在**发布队列", not any(t["info_hash"] == DENY_H
                                     for t in db.get_pending_publish()))

    print("\n⑪ 键白名单：build_row 的键**全部**是 DB 已知列")
    known = {c for c, _ in db.COLUMNS}
    unknown = set(build_row(ALLOW, raw_tid="t", info_hash="a" * 40,
                            torrent_file=tf, qb_seed_days=1.0, now=NOW)) - known
    chk("没有未知列名", not unknown, f"多出 {unknown}" if unknown else "")

    print("\n⑫ 五种非 SUCCESS 状态**全部**选不出（防「只修了其中一种」）")
    conn = db.connect()
    for st_name in NON_PUBLISH_STATUSES:
        # ★ `opencd_added_at` 设成"早就够了"，**故意只让 status 这一条来拦** ——
        #   否则闸门② 会一起把它们挡下，这条判据就分不清"是 status 拦的
        #   还是日期拦的"（`ERR-META-05`：两个口径不许混）。
        conn.execute("UPDATE tasks SET status=?, opencd_added_at=? "
                     "WHERE info_hash=?",
                     (st_name, (GATE2_NOW - timedelta(days=GATE2_EDGE * 4)).strftime(TS_FMT), ALLOW_H))
        n = conn.execute(
            f"SELECT count(*) FROM tasks WHERE info_hash=? AND {db.PENDING_PUBLISH_WHERE}",
            (ALLOW_H,)).fetchone()[0]
        chk(f"{st_name} 不在队列", n == 0)
    conn.close()

    # ── ⑫-B ★★★ 2026-09-27 新判据：第 ④ 条必须**分辨两支** ──────────────
    #
    #  为什么非要加这条判据（`ERR-META-28`：一条断言若说不出"什么输入会让它
    #  失败"，它就不是判据）：
    #    上面⑫ 只断言「NO_TORRENT_FILE 不在队列」—— 而**两支**产出的状态码
    #    相同 ⇒ **旧代码（混成一句）和新代码（分开两支）都能让⑫ 通过**。
    #    ⇒ ⑫ 证不了这次的修复。而这次的病**正好**是"两件不同的事长得一样"，
    #      所以必须直接断言**它们的 `reason` 不同**，并断言**各自说的是自己的事**。
    #
    #  ★ 三条断言，缺一不可（第三条是阴性对照的一半）：
    #    ① `None` 那支的 reason 必须说"**没传路径**"，**不许**说"文件不存在"
    #       —— 旧代码那句 `拿不到本地种子文件（None）` 会**同时**通过①和②下的
    #          模糊检查，所以判据要逐词钉住（`in` 而不是"like"）。
    #    ② 路径给了但不存在那支，reason 必须说"**盘上不存在**"。
    #    ③ ★★ 两支的 reason **必须不相等** —— 只断言①②，将来有人把两支又合并
    #       成一条（比如都改成 `f"没有 .torrent：{torrent_file!r}"`）时，
    #       ① 会因为 `（None）` 里没有"没传"字样而失败，但**只改文案不改结构**
    #       的退化（两支都写同一句）仍可能溜过 ⇒ 这条把"可分辨"本身钉住。
    print("\n⑫-B ★★★ 第④条：『没传路径』与『文件不在』必须是**两句不同的话**")
    st_none, why_none = ingest(ALLOW, raw_tid="t12b-none", info_hash="2" * 40,
                               torrent_file=None, qb_seed_days=10.0, now=NOW)
    st_miss, why_miss = ingest(ALLOW, raw_tid="t12b-miss", info_hash="3" * 40,
                               torrent_file="/不存在/这个文件.torrent",
                               qb_seed_days=10.0, now=NOW)
    chk("两支的状态码**仍然相同**（都 = NO_TORRENT_FILE，下游语义没变）",
        st_none == st_miss == STATUS_NO_TORRENT_FILE,
        f"{st_none!r} vs {st_miss!r}")
    chk("① torrent_file=None ⇒ reason 说的是「未传路径」",
        ("未传" in why_none or "没传" in why_none), f"{why_none!r}")
    chk("① ★ 且 reason **没有**断言「文件不存在 / 拿不到」（本模块无从知道）",
        ("不存在" not in why_none) and ("拿不到" not in why_none),
        f"{why_none!r}")
    chk("② 传了路径但盘上没有 ⇒ reason 说的是「盘上不存在」",
        "不存在" in why_miss, f"{why_miss!r}")
    # ★★ ③ 这一条我第一版写错了，把过程记下来（因为它**正是**本项目反复栽的那个坑）：
    #
    #   第一版写的是 `why_none != why_miss`（"两支的文案必须不相等"），
    #   并在**注入旧 bug 的阴性对照**里**通过了** —— 而旧 bug 恰恰就是
    #   "两支合并"！⇒ 它看着像在钉这件事，其实**没钉住**。
    #
    #   ★ 为什么：旧代码 `f"拿不到本地种子文件（{torrent_file!r}）"` 是**一条
    #     分支**，但 `{torrent_file!r}` 会把 `None` 和路径分别插进去
    #     ⇒ 两句话**字符串不相等**（`（None）` vs `（'/不存在/…'）`）
    #     ⇒ `!=` 为真 ⇒ 断言通过。**这又是 `ERR-META-28`**：
    #     我说不出"什么输入会让 ③ 失败"，而实际上**旧 bug 就不会让它失败**。
    #
    #   ★ 真正的判据不能问"两句话一样吗"（那是**表象**），要问
    #     "**分支是两支还是一支**"（那是**性质**）。两种可执行写法，都用上：
    #       (a) **行为**：`None` 那支的文案里**不许出现我们传的那个路径**
    #           （旧 bug 因为插值，`None` 支会带上"拿不到…"字样 ⇒ ① 失败）。
    #       (b) **结构**：源码里必须是**两个并列的 `elif`**，不是 `or` 合成的一支。
    #     ⇒ (b) 单独就能杀掉旧 bug；加 (a) 是为了把"文案"也钉住，
    #       免得将来有人留两个 `elif` 却把文案抄成同一句。
    chk("★★ ③(a) `None` 支的文案里**没有**出现我们传的那个路径（= 不是同一句话插值）",
        "/不存在/这个文件" not in why_none,
        f"{why_none!r}")
    _src = open(__file__, encoding="utf-8").read()
    _br = _src.split("def build_row(", 1)[1].split("\ndef ", 1)[0]
    chk("★★ ③(b) `build_row` 里确实是**两个并列的 elif**（不是 `or` 合起来的一支）",
        ("elif not torrent_file:" in _br)
        and ("elif not os.path.exists(str(torrent_file)):" in _br)
        and ("not torrent_file or not os.path.exists" not in _br),
        "合并成一支 = 这次的 bug 原样复发（判据① 也会同时失败）")
    # ★ 顺手钉住：`torrent_file` 列**没被这次 ingest 写进去**
    #   （`build_row` 刻意不写它 —— 见它 docstring 里"刻意不写的三个键"）。
    #   ★ 这正是"列里有路径、状态说没有 .torrent"那个自相矛盾的来源，
    #     也是 `flip_status.py`（只改 status 一列）存在的前提。
    #   ★ 用一次直查而不是 `row_of()`：后者只取 14 个固定列，**不含** `torrent_file`。
    _c = db.connect()
    _tf = _c.execute("SELECT torrent_file FROM tasks WHERE info_hash=?",
                     ("2" * 40,)).fetchone()[0]
    _c.close()
    chk("★ ingest(torrent_file=None) **没有**写 torrent_file 列（刻意不写这列）",
        _tf is None, f"{_tf!r}（本行是新建的，故应为 None）")

    print("\n⑬ ★★ 端到端（最有价值的一条）：队列的行 → 渲染 → 标签")
    import descr
    conn = db.connect()
    # ★ 2026-09-23：队列条件里 `publish_after` 已被 `opencd_added_at` 取代
    #   ⇒ 这条前置**必须**把它也设成"8 天前"，否则选不出来
    #   （这正是 `db.PENDING_PUBLISH_WHERE` 那句"空 ⇒ 拦"的现场演示）。
    conn.execute("UPDATE tasks SET status='SUCCESS', "
                 "opencd_added_at=? WHERE info_hash=?",
                 ((GATE2_NOW - timedelta(days=GATE2_EDGE + 1)).strftime(TS_FMT), ALLOW_H))
    conn.commit(); conn.close()
    pending = [t for t in db.get_pending_publish() if t["info_hash"] == ALLOW_H]
    if not pending:
        chk("队列里有这条（前置）", False, "选不出来")
    else:
        task = dict(pending[0])
        # ★ 这一步就是 `publish.py` 要做的：把 mapped_json 解回顶层键
        task.update({k: v for k, v in db._load_mapped(task).items()
                     if k in ("tags", "medium_sel", "audiocodec_sel", "team_sel")})
        f = descr.build_form_fields(task, autodetect=True)
        chk("★ tags[4][] == ['6','10','31']（不是 detect_tags 猜的）",
            f.get("tags[4][]") == ["6", "10", "31"], f"{f.get('tags[4][]')}")
        chk("medium_sel[4] 来自库（'8'）", f.get("medium_sel[4]") == "8",
            f"{f.get('medium_sel[4]')!r}")
        chk("custom_fields[4][1] == 艺术家", f.get("custom_fields[4][1]") == "齐秦",
            f"{f.get('custom_fields[4][1]')!r}")
        chk("custom_fields[4][3] 非空（必填的抓轨日志）",
            bool((f.get("custom_fields[4][3]") or "").strip()))

    return ok, fails


def main():
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="OpenCD 页面 → tasks 表的写入侧")
    ap.add_argument("--selftest", action="store_true", help="跑离线自检")
    # ★ --json：机读出口，给 tools/gen_counts.py 读。别去正则解析人读的 ✅/❌ 行 —— 分母口径不统一。
    ap.add_argument("--json", action="store_true",
                    help="只把结果以 JSON 打到末行（给 gen_counts.py 读）")
    args = ap.parse_args()

    if not args.selftest:
        ap.error("加 --selftest（本模块由 crawl.py 调用，不单独出网）")

    if args.json:
        ok, fails = _check()
        print(json.dumps({"script": "opencd_ingest.py --selftest", "ok": ok,
                          "total": ok + fails, "exit": 1 if fails else 0},
                         ensure_ascii=False))
        return 1 if fails else 0

    print("=" * 74)
    print("opencd_ingest 离线自检（纯本地，零请求）")
    print("=" * 74)
    ok, fails = _check()
    print("\n" + "=" * 74)
    if fails:
        print(f"❌ 自检失败：{ok}/{ok + fails} 通过，{fails} 项不符")
        return 1
    print(f"✅ 自检通过：{ok}/{ok} 项全对")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
