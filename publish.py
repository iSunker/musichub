"""
把 run.py 生成好的种子发布到 Musopia，并把新种子回加到 qBittorrent 做种。

改动要点：建表/查询走 db.py，简介渲染走 descr.py，HTTP 走 musopia.py。
发布成功后立刻用 edit.php 回读一次，确认站点上的内容跟本地一致——发布接口
拿到 302 只代表种子进库了，简介有没有正确落地是另一回事。

★★ 安全模型：每个保险里都放**一把**钥匙（2026-09-24 定，ERR-META-12）。

   发布是**本项目唯一不可逆**的动作（站点上没有"批量撤回"，详见 readd_to_qb
   上方与 ERRATA.md 的 ERR-META-12：**"没有被禁止" ≠ "被允许"**）。原设计有
   三道独立保险，但**它们的钥匙都放在同一处** —— 只要 `ENABLE_PUBLISH=true`
   且 `DRY_RUN=false`，daemon 就会**无人值守地把当前队列自动发出去**，
   人根本不在回路里：

     · 保险 ① `ENABLE_PUBLISH`  —— 容器的长期开关
     · 保险 ② `DRY_RUN=false`   —— 同容器，同一个 `.env`
     · 保险 ③ `preflight()`     —— 只拦"**空库** + publish 开着"这一种组合
       （为什么不够：`db.py` 的 `preflight` 明写它要防的是
        "/data 卷挂错或被重建" ⇒ 只对 total==0 报警；而这里最危险的
        **恰恰不是空库** —— 是库好好的、闸门①~④ 都判"可以发"、于是
        无人值守地发出去。闸门回答的是"**这行该不该发**"，它不回答
        "**现在该不该发**"。两件事，混在一起就会以为"有闸门 = 安全"。）

    ⇒ 从 2026-09-24 起加**第二把、独立的**钥匙：**`PUBLISH_I_AM_HERE`**。
      它由 `guard_auto_key()` 在**无人值守路径**（`--auto`，daemon 恒带）上
      校验，通过才发。
      ① 它不是"把 `ENABLE_PUBLISH` 挪个地方" —— 那把钥匙仍在容器里（安全）；
      ② 答错的**唯一后果是这一轮不发**，不污染数据；
      ③ ★ 而且"**根本没配**"（= 变量不存在）与"**配错了**"**分开报**：
         `ERR-ENV-02` 的教训是"缺键"最容易被静默当成某一种默认值
         （`iter_my_uploads` 的 uid 就是这么错的）。"没配"要说成"你还没配"，
         不能含糊成"校验失败"。

    ★★ 判据是**显式开关 `--auto`**，不是 `sys.stdin.isatty()`。
      为什么换掉 isatty：**它会因环境而变** —— 同一条命令 `docker compose run`
      在 CI、管道、重定向下 isatty 为假，`docker exec -t` 下为真。
      ⇒ 把"有没有人"绑在 TTY 上，等于让保险的成败取决于**调用者怎么挂终端**，
      而不是取决于**我们是不是在无人值守地发**。`--auto` 是我们在 daemon 里
      **写死的**，判据只有一个来源（`ERR-META-03` 的味道）。
    ★ 人工路径（人在终端前敲 `publish.py`）不受影响：不带 `--auto` ⇒ 不查钥匙。
    ★★ 改这一道**不动闸门①~④**：那四道管"该不该发"（`db.PENDING_PUBLISH_WHERE`），
      这一道只管"**有没有人在场**"。两次发布之间**没有任何闸门被放松**。
    ⚠️ 已知缺口：它挡的是"**忘了 / 以为**"，**不是**"要作恶" —— 拿到容器命令行
      的人可以显式去掉 `--auto`。同 `--i-have-stopped-the-container` 的定位。

★★ 节流：两道**互补**的旋钮（2026-09-24 加，别把它们读成同一件事）

   ① `PUBLISH_MAX_PER_CYCLE`（daemon 侧，**每轮条数**）—— 决定**日上限**
      `日上限 = 86400 / PUBLISH_INTERVAL × PUBLISH_MAX_PER_CYCLE`
      实测口径：`1800s × 1` ⇒ **48 条/天**。
   ② `PUBLISH_MIN_GAP`（本文件，**条间隔秒数**）—— 决定**突发形态**

   ★★ 为什么两个都要、且**不能合并成一句**：`MIN_GAP` **压不住日总量**。
      5 条 × 120s = 10min，而一轮的周期是 30min ⇒ **闸门永远轮不到触发**。
      它真正的用场是**手工 `--limit N`**（或将来把①调大）时，
      把"N 条在 7.5 秒内连打站点"摊平开。
      ⇒ 把两者说成"限速"一个词，就是 `ERR-META-03`（判据漂移）的形状：
        读的人会以为调了②就把①也管住了。**各管各的，分开写。**

   ★ 为什么**不是**把 `musopia.py` 的 `request_interval` 调大：
      那是**单次 HTTP 请求**之间的地板（1.5s），而**一条发布 = 多个请求**
      （`upload_torrent` + `fetch_edit_form` 回读 + `download_torrent` + qB 回加）
      ⇒ 2 分钟要包住的是**整条记录**，只能在**发布循环层**表达。
      ★ 写在 `musopia.py` 里做不到这件事 —— 那里看不见"一条记录"的边界。
"""
import argparse
import ast as _ast
import io
import json
import os
import re
import sys
import time

import config
import crabpt_site
import db
import descr
import readd_queue
import torf
from musopia import LoginExpired, MusopiaClient, SiteError


def parse_args():
    ap = argparse.ArgumentParser(description="发布待上传的种子到 Musopia")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认（用于定时任务）")
    ap.add_argument("--limit", type=int, default=0, help="最多发布多少条")
    ap.add_argument("--no-readd", action="store_true", help="发布后不回加到 qBittorrent")
    ap.add_argument("--dry-run", action="store_true", help="只列出将要发布的内容，不提交")
    # ★★ 2026-09-23 新增 `--only`：**只发显式点名的 hash**，其余一律不发。
    #
    #   ★ 为什么需要它（不是"方便"，是"安全默认"）：
    #     `db.get_pending_publish()` 是个**查询**，它返回的集合随闸门判据变化而变化。
    #     用户要的是"**手动发十来个看看网页效果**" —— 那是一个**点名的集合**，
    #     不是"当前队列"。没有这个开关，"发十条"就只能靠 `--limit 10`，
    #     而那等于**取队列前 10 条**（顺序由 SQL 决定、不由人决定）。
    #   ★☆ 判据：`--only` 给的是**交集**，不是"另开一条路"：
    #         点名 ∧ 仍满足 `PENDING_PUBLISH_WHERE` ⇒ 才发。
    #         ⇒ 若某条因闸门（如 `transfer_rule != 'ALLOW'`）进不了队列，
    #           点名它也**发不出去**，并会**逐条报出原因**（不静默跳过）。
    #   ★ 与 `--limit` 可叠加：先按 `--only` 收窄，再取前 `--limit` 条。
    ap.add_argument("--only", default="",
                    help="只发布这些 **info_hash 前缀**（逗号分隔）。"
                         "★ 只认 info_hash 前缀 —— **站点 id（site_torrent_id）不认**"
                         "（传站点 id 会提示你该用哪个 info_hash，不会当成'库里没有'）。"
                         "★ 是与待发布队列取**交集**：点名但被闸门拦下的会被报出，"
                         "不会因此绕过闸门。默认空 = 不限制。")
    # ★ 2026-09-17：匿名改为**默认开启** —— 想具名发布要显式 --no-anonymous。
    #   两个参数共用 dest="anonymous"，后加的那个覆盖前者（argparse 以最后一次为准）。
    ap.add_argument("--anonymous", action="store_true", default=True,
                    help="匿名发布（**默认**；保留此参数只为向后兼容，脚本里若显式传它也无害）")
    ap.add_argument("--no-anonymous", dest="anonymous", action="store_false",
                    help="★ 具名发布（覆盖默认的匿名）")
    # ★★ 2026-09-24：`--auto` 是**无人值守**路径的显式标记，由 **`daemon.py` 在
    #   拼命令行时**加上（`apply_flag("--yes", "--auto")`）。
    #
    #   ★★ 注意：它**不**由本文件内部的某个"自动入口"注入 —— 本文件**只有一个
    #     入口 `main()`**（曾经那个 `run_auto()` 是死代码，已删除，
    #     理由见下面 `readd_to_qb` 上方那段）。
    #   ★★ 所以无人值守时**两个 flag 都要有，少一个都是 bug**：
    #        `--yes`  跳过 `main()` 里的 `input()` ⇒ **没有它 = EOFError 崩掉**
    #        `--auto` 声明无人值守 ⇒ 校验第二把钥匙 ⇒ **没有它 = 保险被关掉**
    #      （2026-09-24 实测：只传 `--auto` ⇒ `EOFError`，阶段退出码 1。见 daemon.py 那段。）
    #
    #   ★ 为什么要这个开关（不是"多余的自我声明"）：它让**保险 ②** 的判据
    #     「**自动路径 ⇒ 必须过钥匙**」在**代码里有名字**，而不是只能靠
    #     "调用方保证加了 --yes"。★ 顺序想清楚了才定：必须是
    #     `--auto` 为**真**才查钥匙（即"钥匙 = 自动路径的**附加**要求"）。
    #     反过来说：**只要带 `--auto`，钥匙就没得商量** ——
    #     所以 `--yes --auto` 这个组合（daemon 用的那个）恒被拦到钥匙通过为止。
    #   ★ 反过来**绝不能**写成"没带 --auto 才查钥匙"：那样任何人只要手敲一条
    #     带 `--auto` 的命令，就把钥匙**绕过去了** —— 一个能靠"多打一个词"
    #     关掉的保险，不是保险。`--yes` 同理（下面 main() 里那段）。
    ap.add_argument("--auto", action="store_true",
                    help="★ 无人值守路径（由 daemon.py 拼命令行时加上）。带它 ⇒ "
                         "**必须**通过 PUBLISH_I_AM_HERE 校验，否则拒绝发布。"
                         "人类在终端前手敲时不要用它。"
                         "★ 无人值守还必须同时带 --yes（否则 input() 会拿到 EOF 崩掉）。")

    # ★★★ 站点分流（2026-09-25 新增）—— 走 `--site` 而不是"两个脚本"。
    #
    #   ★ 为什么**不**新开一个 `publish_crabpt.py`：
    #     两站共享的**不是**字段（字段全不同，见 crabpt_site.py 文件头），
    #     而是**安全不变量**：`--only` 交集语义、`--limit` 有界、
    #     闸门（`PENDING_PUBLISH_WHERE`）、dry-run 默认列出、两把钥匙、
    #     逐条落库（`db.set_site_id`）、说真话的退出码。
    #     ⇒ 抄一份脚本 = 把六个**已经调对**的判据复制成两份，而它们必然漂移
    #       （`ERR-META-03`）。★ 更坏的是漂移方向：安全不变量漂移 = 某一条
    #       悄悄放宽，且**不报错**。
    #   ★ 为什么不用 `--crabpt` 这种布尔开关：布尔只能表达"是不是蟹黄堡"，
    #     加第三个站时就得再加一个，**两个布尔同时为真**时无定义。
    #     枚举值把这个状态排除在设计里（`ERR-ENV-03` 那个"多开关=一把钥匙"的教训）。
    #
    #   ★ 默认值的选择是刻意的：**默认仍是 musopia**。
    #     它是生产上跑了几个月的站点（254 条在做种），改默认 = 改变既有行为；
    #     而"新增一个站"不该顺手改动老站的行为（本项目的 `ERR-ENV-02` 同理：
    #     缺键时的默认值必须**中性**）。
    ap.add_argument("--site", choices=("musopia", "crabpt"), default="musopia",
                    help="发布到哪个站（默认 musopia = 保持既有行为）。★ "
                         "`crabpt` = 蟹黄堡，走 crabpt_site.py 的字段层与客户端；"
                         "★ 两站共用 --only/--limit/闸门/dry-run/两把钥匙，"
                         "只有**字段与提交地址**不同。")
    # ★ 负向自检（不碰站点、不碰库）。见 `selftest_crabpt_isolation`。
    ap.add_argument("--selftest", action="store_true",
                    help="只跑本文件的负向自检：断言蟹黄堡那条路**不碰**乌托邦的 "
                         "`site_status`/`site_torrent_id`、且确实写 `site_publish`。"
                         "★ 不发任何请求、不读库。")

    return ap.parse_args()


# ---- ★★ 第二把钥匙：自动发布前，「人必须在回路里」 ----
#
# ★ 为什么把它抽成模块级函数、而不是写在 main() 里：
#   "多处各写一份同样的判据"就是 `ERR-META-03`（判据漂移）的标准产地 ——
#   一处放宽、另一处没跟，而两边都"看着对"。
#   ★★ 2026-09-24：本文件现在**只有一个入口 `main()`**（交互与无人值守都走它，
#     差别只在传了哪些 flag）⇒ 这个函数**只有 `main()` 一处调用**。
#     原注释说"两处会真正提交（main 与 run_auto）"—— 那是错的，`run_auto()`
#     从来没人调用（它正是 17:02 那次崩溃的成因）。详见下面那段。
#
# ★ 返回值是**三元组**，因为"配错了"与"没配"必须分开说：
#   (True,  "",                    "")            → 校验通过
#   (False, "值不对（已脱敏）",      "值不对")      → 用户配了，但配错
#   (False, "还没配置这个变量",      "根本没配")    → 压根没这回事
AUTO_KEY_ENV = "PUBLISH_I_AM_HERE"
AUTO_KEY_VALUE = "i-am-here"


# ---- ★★ 条间隔（突发保护）：两条发布之间至少隔这么久 ----
#
# ★ 与 `PUBLISH_MAX_PER_CYCLE` 的分工见模块 docstring「节流：两道互补的旋钮」。
#   一句话：**那个管日上限，这个管突发形态**；这个**压不住**日总量。
#
# ★ 为什么默认是 **0（关）**：本参数是**加法**，不是修正。
#   不填就该等于"行为与加它之前完全一致"（`ERR-ENV-02` 的味道：缺键最容易被
#   静默当成某个默认值，所以那个默认值必须是**中性的**）。生产由 `.env` 显式给 120。
#
# ★ 为什么**不加** `--min-gap` 这种 CLI 开关：那会让同一个判据有**两个真值来源**
#   （env 与 argv），正是 `ERR-META-03` 的标准产地。要临时关就 `PUBLISH_MIN_GAP=0`
#   （`.env` 里改，或 `docker compose run -e PUBLISH_MIN_GAP=0 ...`）。
#
# ★ 生效范围 = **手工与自动都生效**（不带 `--auto` 条件）：
#   它防的是"站点被连打"，而手工 `--limit 10` 恰恰是突发最可能的来源。
#   ⇒ 凭"人在不在场"来放行突发保护是**反的**（人在场不代表站点更挨得住）。
#   ★ 而且 `--auto` 是**归责**判据（"有没有人在场"），不是**限速**判据；
#     让一个开关同时管两件事，就是 `ERR-ENV-03` 那个形状。
PUBLISH_MIN_GAP_ENV = "PUBLISH_MIN_GAP"
PUBLISH_MIN_GAP_DEFAULT = 0
# ★ 站点请求的兜底地板（与 `musopia.py` 的 `request_interval` 同值）。
#   这里只用于**打印**"距上一条多久了"，不参与判据 —— 不抄第二份真值。
MUSOPIA_REQUEST_INTERVAL = 1.5


def publish_min_gap():
    """读 `PUBLISH_MIN_GAP`（秒）。0 / 负 / 非数字 ⇒ 0（= 关）。

    ★ 非数字**不** sys.exit —— 一个可选的节流参数不该让整个发布阶段起不来。
      那会把 `ERR-META-01`「检测手段失效比不检测更危险」**反过来**踩：
      为了一个"更安全"的参数，把唯一能发种的路径整个打挂。
      而且退出非 0 ⇒ `fails["publish"] += 1` ⇒ **指数退避**
      （`MAX_BACKOFF=21600`，最长 6 小时一轮）⇒ 一个配置**笔误**会让
      **真正的发布**停摆半天。⇒ 只**报警 + 退回中性默认**。
    ★ 负数按 0 处理（不是取绝对值、不是报错）：`time.sleep(-1)` 会直接
      抛 ValueError；而"等一个负数秒"唯一合理的读法是"不等"。
    """
    raw = os.getenv(PUBLISH_MIN_GAP_ENV)
    if raw is None or str(raw).strip() == "":
        return PUBLISH_MIN_GAP_DEFAULT
    try:
        val = int(float(raw))
    except ValueError:
        print(f"⚠️  {PUBLISH_MIN_GAP_ENV}={raw!r} 不是数字，"
              f"本条间隔按 {PUBLISH_MIN_GAP_DEFAULT}s（= 关）处理", flush=True)
        return PUBLISH_MIN_GAP_DEFAULT
    if val < 0:
        print(f"⚠️  {PUBLISH_MIN_GAP_ENV}={val} 是负数，按 0（= 关）处理", flush=True)
        return 0
    return val


def wait_publish_gap(gap, i, total, last_upload_ts):
    """等到"距上一条**上传**满 `gap` 秒"再返回。返回本次的上传时间戳基准不变。

    ★★ 为什么按"距上次上传"算、而不是直接 `sleep(gap)`：
      一条发布**本身**要花几秒（3 个站点请求 × 1.5s 地板 + qB 回加 + SQLite）。
      直接 sleep ⇒ 实际间距变成 `gap + 耗时`，旋钮的名字就成了假话，
      且它是**不可预测**的（随网络/站点快慢漂）。
      ⇒ 睡**余额**，与 `musopia.py:_throttle()` 同一个写法，只是"上一次请求"
        换成"上一次上传"（高一层）。副作用是**好的**：若上一条本来就够慢，
        这一条会**完全不睡**（不产生无意义的等待）。

    ★ `gap` 已保证 > 0（`publish_min_gap()` 归一过），`i > 1` 才调用
      ⇒ N 条恰好产生 **N-1 段**间隔，末尾**不会**多睡一次。
    """
    elapsed = time.time() - last_upload_ts
    if elapsed >= gap:
        print(f"    ⏳ [{i}/{total}] 距上一条已 {elapsed:.1f}s（≥ {gap}s），不等待",
              flush=True)
        return
    need = gap - elapsed
    # ★ flush=True 是**承重**的：容器里 stdout 是**块缓冲**（非 tty），
    #   不 flush 的话这一行要等缓冲区满才出现 —— 而它恰恰是在"卡住 120 秒"
    #   的那段时间里唯一能安慰人的输出。daemon.log() 同样用 flush=True。
    print(f"    ⏳ [{i}/{total}] 距上一条已 {elapsed:.1f}s，"
          f"再等 {need:.1f}s（{PUBLISH_MIN_GAP_ENV}={gap}）", flush=True)
    time.sleep(need)


def guard_auto_key():
    """无人值守发布前的第二把钥匙。返回 (放行?, 打印原因, 机器可读的原因)。"""
    got = os.getenv(AUTO_KEY_ENV) or ""
    if got == AUTO_KEY_VALUE:
        return True, "", ""
    if not got.strip():
        return False, (f"还没配置环境变量 {AUTO_KEY_ENV}"
                       f"（当前值：{'空' if got == '' else '只有空白'}）"), "根本没配"
    # ★ 绝不回显值本身。哪怕它只是个"短语"，回显就等于把它印进 docker logs，
    #   而 logs 是本项目里**最容易被抄走的那份**（见 daemon.save_env_snapshot
    #   为何按白名单落盘）。这里只报"对不对"，不报"是什么"。
    return False, f"{AUTO_KEY_ENV} 的值不对（★ 不在此回显，避免写进日志）", "值不对"


def explain_key_reason(why):
    """把上面第三种返回值翻成一句人话 —— 两种"不发"的修法**完全不同**。"""
    if why == "根本没配":
        return ("   ⇒ 这是「还没配」：请在**容器**的 `.env` 里加一行\n"
                f"        {AUTO_KEY_ENV}={AUTO_KEY_VALUE}\n"
                "      然后 `docker compose up -d --build`"
                "（★ 不是 restart，见 deploy.sh 里那段）。\n"
                "      ★ 字面量里的连字符**不需要转义**；这是**值**"
                "（compose 只展开 `.env` 的**值**，不展开键名）。")
    return ("   ⇒ 这是「配错了」：变量存在但值不是约定值（常见：多了引号/空格、"
            f"或抄了模板里的占位符）。改成 `{AUTO_KEY_ENV}={AUTO_KEY_VALUE}` 即可。")


# ⚠️⚠️ 这里**曾经**有一个 `run_auto()` —— 已删除（2026-09-24）。别把它加回来。
#
#   它当时的写法是"自己查一遍钥匙，再拼 `--yes --auto` 调 `main()`"，
#   而 docstring 写着「无人值守入口（daemon 的 publish 阶段）」。
#
#   ★★ 问题：**daemon 根本不调它**。`daemon.py` 的 publish 阶段是起**子进程**：
#      `subprocess.Popen(["python", "publish.py", "--yes", "--auto", ...])`
#      ⇒ 走的是模块级 `if __name__ == "__main__": main()`。
#      ⇒ `run_auto()` 是个**没人调用的死函数**，而它让上一轮的推理"看着很完整"：
#        "`run_auto()` 会补上 `--yes`" ⇒ 于是 `daemon.py` 那边把 `--yes` 删了
#        ⇒ **真实路径上没人补** ⇒ `main()` 走进 `input()` ⇒ 容器无 stdin
#        ⇒ `EOFError` ⇒ 阶段崩（2026-09-24 17:02 实测，退出码 1，一条都没发）。
#
#   ★★★ 这是 `ERR-META-01` 的一个**新形状**，记在这里：
#      **判据"看着对"不等于"它覆盖的是那条路"**。上一轮验的是 `run_auto()`
#      （读它、信它），却**从未**验"daemon 到底往命令行传了什么"。
#      ⇒ 纪律：**改"谁传什么参数"时，去查真实调用链**（`grep` 调用点，
#        或直接看 daemon 拼出来的 `cmd`），**不要信函数名或它自己的文档**。
#
#   ★ 为什么是"删掉"而不是"把 daemon 改成调它"：
#     那会让"谁按的确认"多出一条**并行**入口，两个入口各自拼参数
#     = 判据漂移的标准产地（`ERR-META-03`）。
#     ⇒ 现在**只有一个入口 `main()`**：daemon 与人都走它，
#       差别只在**传了哪些 flag**（`--yes` 跳过确认 / `--auto` 查钥匙）。
#   ★★ `--yes` 与 `--auto` **两件不同的事，无人值守时要**都传**
#      （`daemon.py` 的 publish 阶段已改回 `apply_flag("--yes", "--auto")`）：
#        · `--yes`  跳过 `main()` 里的 `input()`（**没有它 ⇒ EOFError ⇒ 崩**）
#        · `--auto` 声明无人值守 ⇒ 校验第二把钥匙（**没有它 ⇒ 保险被关**）


def readd_to_qb(qb, client, info_hash, torrent_id):
    """把站点上的新种子下载回来加进 qB，做种路径沿用原种子的。

    ★★★ **不要给 `torrents_add()` 传 layout 参数**（2026-09-24 修，别再"顺手加回"）

    这里曾写 `is_root_folder=False`，造成**种子已上站、但从不做种**：qB 显示
    `state=missingFiles`、`content_path` 退化成光秃秃的 `/downloads`。
    实测影响 `Musopia` 260 条中的 5 条（全部 2026-09-24 新发）。

    **为什么 `False` 会错**（依据是 pin 住的库源码，非推测）——
    `qbittorrent-api==2024.9.67` 的 `torrents_add()` 在 Web API ≥ 2.7 上会把该参数
    **改写**掉：

        if content_layout is None and is_root_folder is not None and api_version >= v("2.7"):
            content_layout = "Original" if is_root_folder else "NoSubfolder"
            is_root_folder = None          # ⇒ 真正发出去的是 contentLayout

    ⇒ `is_root_folder=False` **≡ 发 `contentLayout=NoSubfolder`**
    （实测表单：`savepath=%2Fdownloads&contentLayout=NoSubfolder&category=Musopia`）。
    而 `run.py` 用 `torf` 生成的种子是**多文件**结构 —— `info['name']` 是目录名、
    但 `info['files'][].path` **不含**该前缀（`torf._torrent._set_files`）。
    「要不要把 `name` 补回文件路径前」**正是 `contentLayout` 在管的事**：
    `NoSubfolder` ⇒ 不补 ⇒ qB 去 `/downloads/01.Hurricane.flac` 找，
    而文件实际在 `/downloads/<专辑名>/01.Hurricane.flac` ⇒ **错位** ⇒ 从不做种。

    **为什么"不传"是对的**：不传 ⇒ `content_layout` 与 `is_root_folder` 都是 `None`
    ⇒ 这两个字段**根本不进 HTTP 表单**（实测：lib 用 `(None, value)` 二元组装配，
    这是 `requests` 的 multipart 约定，值为 `None` 时字段被丢弃）
    ⇒ **完全交给 qB 按种子自己的 metainfo 决定**。

    ★ 这么做的**关键好处**：qB 是唯一掌握"每个种子内部结构"的一方，判据交给它，
    我们**不必去猜**、也不必自己读 `.torrent` 反推。两种结构各按各的还原：
      · `milet - Made of Glass`（单层，`01.flac` 直接在根）⇒ `name` 就是真实目录名
      · `[260909] 岩崎太整…`（2CD，`Disc 1/`+`Disc 2/`）⇒ `name` 是不落地的容器名
    没有**任何**固定取值能同时伺候这两种 —— 所以"不传"不是偷懒，是唯一正解。

    ⚠️ **一处仍未验证的未知**（如实写下，别让本函数的输出显得比实际可信）：
    qB 对**多根目录**种子（上面 2CD 那类）具体如何摆放，**没有实测过**
    （只测过 2 个单根目录样本）。⇒ 本函数**不为此加代码**（那等于重新去猜结构），
    而是让下面的复核**如实报出**结果；若真出问题，**那时才有据可依地处理**。

    ★★ 同时**不要传 `is_skip_checking=True`**（原先也传了）：
    它让 qB **跳过哈希校验**，而校验**正是唯一**能发现"文件对不上"的机制
    —— 正因为它被关掉，那 5 条才**静默**变成 `missingFiles` 而无人察觉。
    去掉后文件对则秒进做种、不对则**立刻**报出来（**响亮地坏**）。
    代价是大种首次校验要读一遍文件（`BIGBANG` 那种 100GB+ 会慢），
    但这是"慢"换"静默错误"，符合本项目 `ERR-META-01` 的成文纪律
    「**没报错 ≠ 成立**」。★ 若将来实测撞上 `STAGE_TIMEOUT=3600` 再单独取舍。
    """
    if qb is None:
        # ★ 原先这里**静默 return** ⇒ "没做"与"做了"在日志里都可能看着正常。
        #   现在把**原因**说出来（同 `nas_health.py` 的三态纪律：缺位要说清是哪种）。
        print("    ⚠️  回加跳过：本次没有可用的 qB 连接（启动时未连上，见上方警告）")
        return
    try:
        orig = qb.torrents_info(torrent_hashes=info_hash)
        if not orig:
            print("    ⚠️  qB 里找不到原始种子，无法确定做种路径，跳过回加")
            return
        save_path = orig[0].save_path

        content = client.download_torrent(torrent_id)
        if not content:
            print("    ❌ 从站点下载种子文件失败，跳过回加")
            return

        # ★★ 参数**只有这三个**：save_path / is_paused / category。
        #   ★ 别加 layout（见 docstring）、别加 is_skip_checking（见 docstring）。
        #
        # ★★★ 延迟回加（2026-10-03）：`SEED_DELAY_MIN>0` 时**不立即** add，
        #   而是把待回加记进 `/data/readd_queue.json`，由 `daemon.py` 主循环
        #   15 分钟后代发（qB 收到后自己校验、自己做种）。理由与形态见
        #   `readd_queue.py` 模块 docstring 与 `config.SEED_DELAY_MIN`。
        #   ★ 默认 0 = 关闭 = 维持老行为（立即 add）。
        if config.SEED_DELAY_MIN > 0:
            readd_queue.enqueue(
                content, save_path, config.PUBLISH_CATEGORY,
                config.SEED_DELAY_MIN, note=f"乌托邦 {info_hash[:12]}…",
            )
            # ★ 延迟回加时**不做**下面的回加复核（种子还没进 qB，查必然为空）
            return
        qb.torrents_add(
            torrent_files=content,
            save_path=save_path,
            is_paused=False,
            category=config.PUBLISH_CATEGORY,
        )
        print(f"    ✅ 已提交回加至 qB（做种路径: {save_path}）")

        # ★★★ 发布后复核 —— 把"没报错"变成"有判据"。
        #
        # ★ 为什么要按**另一个** hash 查：站点那个种子是 `run.py` 用新 announce
        #   **重新生成**的 ⇒ 它的 infohash **不等于**库里的 `info_hash`
        #   （`info_hash` 是皇后的**原种**）。⇒ 从刚下载的字节里读出真正的 infohash。
        #   （`torf` 已在镜像里，`Dockerfile` 锁 `torf==4.2.7`）
        #
        # ★ 判据是 `state` **配合**它自己报的路径，**不是**单看"没抛异常"
        #   —— 原先那行 `✅ 已回加至 qB` 只证明 `torrents_add()` 没抛，不证明做种成立。
        #
        # ★ 失败**不算阶段失败**（不 `sys.exit` ⇒ 不触发 daemon 退避）：
        #   种子**已经上站了**（不可逆），回加失败是**本地问题**，退避重试没意义
        #   （下一轮不会再发同一条）。★ 与 `main()` 里 `fetch_edit_form` 回读校验
        #   **同一模式**：只醒目打印，不动 `fail` 计数、不影响退出码。
        #
        # ★★★ 关于"查不到"这句措辞（2026-09-24 **实测踩过，别改回去**）：
        #   `torrents_add()` 是**异步**的 —— qB 收下后**在后台**校验入库，
        #   **不会**在 HTTP 返回前完成。实测那一次：`publish` 阶段全程 **6.2 秒**，
        #   这里查 ⇒ **必然查不到**（日志原话当时是"可能还没入库"）。
        #   ⇒ ★ 那句话**把两种截然不同的情况混成了一个词**：
        #       (a) qB 还没入库（**正常**）      (b) 回加**根本没发生**（**故障**）
        #     故障被"可能还没入库"盖住 ⇒ 这正是「没报错 ≠ 成立」的同族毛病。
        #   ⇒ 现在**分开说**：明确"此刻查不到 ⇒ 无法区分 (a) 与 (b)"，
        #     并指向**延迟复核**（不要在阶段里 sleep —— 拖长阶段没有意义，
        #     且 `STAGE_TIMEOUT=3600` 是硬上限）。
        try:
            new_hash = torf.Torrent.read_stream(io.BytesIO(content)).infohash
            got = qb.torrents_info(torrent_hashes=new_hash)
            if not got:
                print(f"    ⚠️  回加复核：此刻 qB 里查不到 {new_hash[:12]}…")
                print("        ★ 注意：本复核是**同步**跑的，而 qB 入库是**异步**的"
                      "（阶段常只跑数秒）")
                print("        ⇒ 查不到**无法区分**「还没入库」（正常）"
                      "与「回加根本没发生」（故障）。")
                print("        ⇒ 要定论请**延迟后**再查一次（如隔几分钟，"
                      f"按 infohash {new_hash[:12]}… 查 state）")
            else:
                t = got[0]
                ok = t.state != "missingFiles"
                mark = "✅" if ok else "★★"
                print(f"    {mark} 回加复核：state={t.state}  "
                      f"content_path={t.content_path!r}")
                if not ok:
                    print("        ★★ 文件没对上 ⇒ **这条种子不会做种**（已上站，不可逆）。"
                          "见 readd_to_qb 的 docstring。")
        except Exception as e:                              # noqa: BLE001
            print(f"    ⚠️  回加复核失败（不影响发布本身）: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"    ❌ 回加 qB 出错: {e}")


def connect_qb():
    """建一个已登录的 qBittorrent 连接；连不上返回 `None`（并**说清原因**）。

    ★ 从 `main()` 里那段**原样**抽出来（乌托邦与蟹黄堡两条路共用同一份）。
      ★ 不静默吞异常：连不上要打印，否则"没做回加"与"回加成功"在日志里
        长得一样（`readd_to_qb` 的纪律）。
    """
    try:
        from qbittorrentapi import Client
        qb = Client(host=config.QB_HOST, username=config.QB_USER, password=config.QB_PASS)
        qb.auth_log_in()
        return qb
    except Exception as e:                                  # noqa: BLE001
        print(f"⚠️  连不上 qBittorrent ({e})，本次不做回加")
        return None


def readd_to_qb_crabpt(qb, client, info_hash, torrent_id):
    """把蟹黄堡上的新种子**下载回来**加进 qB，做种路径**沿用乌托邦那条**的。

    ★★★ 与 `readd_to_qb`（乌托邦）的**唯一实质区别**：**从蟹黄堡下种子**、
      用**蟹黄堡那条**的 infohash 复核。其余纪律**逐条照搬**，理由见下 ——
      别因为"看起来像"就把这段删掉复用 `readd_to_qb`：

      ① **做种路径必须取乌托邦那条的 `save_path`**（不是按蟹黄堡的 id 找）。
         ★ 为什么：蟹黄堡是**转种**（同一批文件先上乌托邦、再上蟹黄堡）。
           qB 里**只有乌托邦那条**在跑（`tasks.info_hash` = 皇后原种，
           与乌托邦种子对得上）；蟹黄堡种子是**新 infohash**（站点重写了
           announce/piece length，2026-10-02 实测），**加它之前 qB 里没有它**
           ⇒ 只能拿乌托邦那条的 `save_path` 当"文件在哪"的权威。
         ★ 两条的文件**逐字节相同**（实测：同 name、同 80 文件、同总大小）
           ⇒ 指同一个 `save_path` 是对的。
      ② **种子字节来自蟹黄堡**（`client.download_torrent(torrent_id)`），
         不是本地 `tasks.torrent_file`。
         ★ 本地那份带的是**乌托邦** announce ⇒ 拿它回加 = 做种报到乌托邦、
           蟹黄堡仍记 hit-and-run（**假成功**，比不做种更坏）。
      ③ **不传 layout**（`is_root_folder`/`content_layout`）、
         **不传 `is_skip_checking`** —— 逐字沿用 `readd_to_qb` 的教训
         （传 layout ⇒ `NoSubfolder` ⇒ 文件错位 ⇒ 静默 `missingFiles`；
          传 skip-checking ⇒ 关掉唯一能发现文件对不上的校验）。
      ④ 复核按**蟹黄堡那份**的 infohash 查，且**分开说**"还没入库（正常）"
         与"回加没发生（故障）"——`torrents_add` 是**异步**的。
      ⑤ 失败**不算阶段失败**（不 `sys.exit`）：种子已上站（不可逆），
         回加失败是本地问题，退避重试没意义（下一轮不会再发同一条）。
      ⑥ **分类用 `CRABPT_PUBLISH_CATEGORY`**（不是乌托邦的 `PUBLISH_CATEGORY`）
         —— 见下面 `torrents_add` 前的长注释，2026-10-03 接线。

    ★ 依赖 `torf`（已在镜像里，`Dockerfile` 锁 `torf==4.2.7`）读 infohash。
    """
    if qb is None:
        print("    ⚠️  回加跳过：本次没有可用的 qB 连接（启动时未连上，见上方警告）")
        return
    try:
        # ① 做种路径：**乌托邦那条**（= 皇后原种 infohash）的 save_path
        orig = qb.torrents_info(torrent_hashes=info_hash)
        if not orig:
            print("    ⚠️  qB 里找不到**乌托邦**那条原始种子，无法确定做种路径，跳过回加")
            return
        save_path = orig[0].save_path

        # ② 种子字节：**蟹黄堡服务端**那份（带蟹黄堡 announce）
        content = client.download_torrent(torrent_id)
        if not content:
            print("    ❌ 从蟹黄堡下载种子文件失败，跳过回加")
            return

        # ③ 参数只有这三个（见 docstring ①③）
        #
        # ★★★ `category` 用**蟹黄堡自己的**键（`CRABPT_PUBLISH_CATEGORY`），
        #   不是乌托邦的 `PUBLISH_CATEGORY`（2026-10-03 修）。
        #   原先这里和 `readd_to_qb`（乌托邦）**共用** `PUBLISH_CATEGORY` ⇒
        #   **两站的种子混进同一个 qB 分类**，"哪条种子属于哪一站"在 qB 里
        #   分辨不出、也没法按站统计。
        #   ★ `config.CRABPT_PUBLISH_CATEGORY` 早在 2026-09-24 就已**定义**
        #     （`config.py` 里注释白纸黑字写着"与乌托邦分开"），
        #     但**没有任何代码引用它** —— 键定义了、意图写了、**接线漏了**
        #     （`ERR-ENV-03` 形状：看着配好了，其实那条路根本没用它）。
        #     这正是本行改动的唯一目的：把那条**已存在的意图**接上。
        #   ★★ 只影响**此后新加**的种子；qB 里**已有**的蟹黄堡种子仍挂在
        #     `PUBLISH_CATEGORY` 名下，**不会自动迁移**（要不要回迁是另一个决定）。
        #   ★ 另外：qB **不会自动创建**该分类 ⇒ 需在 qB 里先建好
        #     `CRABPT_PUBLISH_CATEGORY`（默认 `CrabPT`），或在 `.env` 里把它
        #     指到一个**已存在**的分类；否则 qB 会把它当作不存在（种子仍入库，
        #     但分类为空）—— 这一条**未在 5.0.4 实测**，启用前需验。
        #
        # ★★★ 延迟回加（2026-10-03）：同 `readd_to_qb` —— `SEED_DELAY_MIN>0`
        #   时不立即 add，记进队列由 daemon 主循环代发。见 `readd_queue.py`。
        if config.SEED_DELAY_MIN > 0:
            readd_queue.enqueue(
                content, save_path, config.CRABPT_PUBLISH_CATEGORY,
                config.SEED_DELAY_MIN, note=f"蟹黄堡 {t.get('info_hash','')[:12]}…",
            )
            return
        qb.torrents_add(
            torrent_files=content,
            save_path=save_path,
            is_paused=False,
            category=config.CRABPT_PUBLISH_CATEGORY,
        )
        print(f"    ✅ 已提交回加至 qB（做种路径: {save_path}）")
        # ④ 复核（判据 = qB 真实 state，不是"没抛异常"）
        try:
            new_hash = torf.Torrent.read_stream(io.BytesIO(content)).infohash
            got = qb.torrents_info(torrent_hashes=new_hash)
            if not got:
                print(f"    ⚠️  回加复核：此刻 qB 里查不到 {new_hash[:12]}…")
                print("        ★ 本复核**同步**跑，而 qB 入库**异步**"
                      "（阶段常只跑数秒）")
                print("        ⇒ 查不到**无法区分**「还没入库」（正常）"
                      "与「回加根本没发生」（故障）。")
                print("        ⇒ 要定论请**延迟后**再查一次"
                      f"（按 infohash {new_hash[:12]}… 查 state）")
            else:
                t = got[0]
                ok = t.state != "missingFiles"
                mark = "✅" if ok else "★★"
                print(f"    {mark} 回加复核：state={t.state}  content_path={t.content_path!r}")
                if not ok:
                    print("        ★★ 文件没对上 ⇒ **这条蟹黄堡种子不会做种**"
                          "（已上站，不可逆）。见 readd_to_qb_crabpt 的 docstring。")
        except Exception as e:                              # noqa: BLE001
            print(f"    ⚠️  回加复核失败（不影响发布本身）: {type(e).__name__}: {e}")
    except Exception as e:                                  # noqa: BLE001
        print(f"    ❌ 回加 qB 出错: {e}")


def merge_mapped(task):
    """
    把 `task["mapped_json"]` 解回**顶层键**。

    ★★ 2026-09-24：**实现已下移到 `db.merge_mapped()`**，这里只是转调。
      下移的理由：新增第二个发布站（蟹黄堡），它的适配层也需要这一步，
      而 `crabpt_site.py` 不能 import publish（会成环）⇒ 各写一份 = 两个口径，
      而这一步**恰恰是"漏了不报错"的那一步**（原 docstring 原话），
      最不该有两份。⇒ 事实源放在 `db.py`（它已经有 `_load_mapped`）。

    ★ 保留这个名字与签名：`publish.py` 内有多处调用（含 `--dry-run` 预览那条
      `fields = descr.build_form_fields(merge_mapped(valid[0]))`），
      改名会波及那些点，而**它们的行为一个字都不该变**。
    """
    return db.merge_mapped(task)



# ★ 闸门判据的**逐条**解释器：只读、不发请求。
#
# ★ 为什么不手抄 `PENDING_PUBLISH_WHERE`：手抄必然漂（本项目的成文纪律，
#   `ERR-META-03`）。这里用**单条件累加**的方式问库 —— 每一问都只回答
#   "这一条是卡在哪个条件上"，而最后一个条件就是真闸门本身。
#   ★ 仍然只读（`PRAGMA query_only=1`），`--only` 的预检不会写任何东西。
def explain_not_queued(prefix):
    """给一个 hash 前缀，用**同一个库连接**逐条问：它卡在哪个闸门上。"""
    import sqlite3
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    try:
        con.execute("PRAGMA query_only=1")
        rows = con.execute(
            "SELECT info_hash, status, site_status, publish_after, "
            "       opencd_tid, mapped_json, transfer_rule "
            "  FROM tasks WHERE info_hash LIKE ?", (prefix + "%",)).fetchall()
        if not rows:
            return "库里没有这个 hash（打错了？）"
        if len(rows) > 1:
            return f"前缀歧义，命中 {len(rows)} 行（给长一点的前缀）"
        (h, st, ss, pa, tid, mj, rule) = rows[0]
        reasons = []
        if st != "SUCCESS":
            reasons.append(f"status={st!r} ≠ 'SUCCESS'")
        if ss == "EXISTS":
            reasons.append("site_status='EXISTS'（已在站上）")
        import datetime
        if pa and pa > datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"):
            reasons.append(f"publish_after={pa}（还在 3 天反悔窗口内）")
        if tid and not (mj or "").strip():
            reasons.append(f"opencd_tid={tid} 非空但 mapped_json 空（闸门③：想读皇后却没读）")
        if (rule or "") != "ALLOW":
            reasons.append(f"transfer_rule={rule!r}（闸门④：只有 'ALLOW' 放行）")
        return "；".join(reasons) if reasons else "（各闸门都过 —— 应该已在队列里，请复核）"
    finally:
        con.close()


def explain_not_transferable(prefix):
    """★ 蟹黄堡路专用：给一个 hash 前缀，说清它**为什么不在转种队列里**。

    ★★ 为什么不复用 `explain_not_queued()`（那是乌托邦路的）：
      两条路问的是**不同的问题** ——
        · `explain_not_queued`：为什么不 **发布到乌托邦**？
        · 本函数          ：为什么不 **从乌托邦转去蟹黄堡**？
      前者会把"`site_status='EXISTS'`（已在站上）"报成**拦下**，
      而后者恰恰**要求**它成立。⇒ 复用 = 给人看**反的**原因
      （这是 `ERR-META-03` 的形状：同一段代码服务两个消费者，
       而两个消费者要的**答案方向相反**）。

    ★ 返回**多行列表**而不是一个字符串：原因可能同时有多条
      （比如"源站没发过" **且** "蟹黄堡发过"），拼成一行会看不清哪条先触发。

    ★★ 标识类型（2026-09-28 修，`ERR-SITE-06` 一族）：本函数只认 **info_hash
      前缀**。而用户眼里一条种子**最显眼的标识是站点 id**（如 `15972`）——
      于是很容易传错。★ 修前那句 `库里没有这个 hash（打错了？）` 是**误导**：
      它把"**你传错了标识类型**"说成"这个 hash 不存在"，而**两者完全不同**
      （一个是"你的输入形式不对"，一个是"库里没有这条"）。
      ⇒ 现在**先探一下**前缀是否命中 `site_torrent_id`，命中就明说该用哪个。
      ★ 更要紧的一层（实测踩过）：这会让「`--only` 零交集」这条判据
        **因传错参数而"碰巧成立"** —— 我当时据此误判"它证明了闸门④ 挡住了
        `15972`"，其实它只证明了"`15972` 不是一个 info_hash"。
        ⇒ **判据通过 ≠ 判据测了它想测的东西**。
    """
    import sqlite3
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    try:
        con.execute("PRAGMA query_only=1")
        rows = con.execute(
            "SELECT info_hash FROM tasks WHERE info_hash LIKE ?",
            (prefix + "%",)).fetchall()
        if not rows:
            # ★★ 关键一支：先看它是不是**站点 id**（而不是 info_hash 前缀）。
            #   查的是 `tasks.site_torrent_id`（乌托邦那一侧的旧镜像列）——
            #   本函数**只做提示**，不改变任何判据（判据仍只认 info_hash）。
            sid_rows = con.execute(
                "SELECT info_hash, site_torrent_id FROM tasks "
                " WHERE site_torrent_id LIKE ?", (prefix + "%",)).fetchall()
            if sid_rows:
                out = [f"★ `{prefix}` 看着像**站点 id**（`site_torrent_id`），"
                       f"不是 info_hash ⇒ 本选项只认 **info_hash 前缀**。"]
                for _h, _sid in sid_rows[:5]:
                    out.append(f"     · site_torrent_id={_sid} ⇒ 该用 "
                               f"`--only {_h[:12]}`（info_hash 前缀）")
                if len(sid_rows) > 5:
                    out.append(f"     …另有 {len(sid_rows) - 5} 条")
                out.append('   ★ 改用它重跑；★ 这不是"库里没有这条"。')
                return out
            return ["库里没有这个 hash（打错了？）"]
        if len(rows) > 1:
            return [f"前缀歧义，命中 {len(rows)} 行（给长一点的前缀）"]
        h = rows[0][0]
        ok, why = db.is_transferable_to(h, db.SITE_CRABPT)
        if ok:
            return [f"✅ 它**在**转种队列里（{why}）—— 请复核上面的命中列表"]
        out = [f"❌ {why}"]
        # ★ 附加信息：**别的**站点上它的分布（"这条到底发过哪些站"）。
        #   ★ 只说事实，不替用户下结论 —— 判据已在 `why` 里给全了。
        try:
            pubs = db.site_publishes(h)
        except Exception as e:  # noqa: BLE001
            out.append(f"   （各站记录读不到：{e}）")
            return out
        if pubs:
            out.append("   各站记录：")
            for site, rec in sorted(pubs.items()):
                out.append(f"     · {site}: id={rec.get('torrent_id')!r} "
                           f"status={rec.get('status')!r}")
        else:
            out.append("   各站记录：**一条都没有**（`site_publish` 里查无此行）")
        return out
    finally:
        con.close()


def _crabpt_cover(task, *, dry_run=False):
    """确保这条种子在**蟹黄堡**上有一份**属于蟹黄堡的**封面 URL。返回 `(url, 说明)`。

    ★★ 为什么必须单独一步（用户 2026-09-25 明确要求「蟹黄堡要用专用的图链」）：
      两站的封面**各自一份** —— 乌托邦是 picgo（`tasks.cover_url`），
      蟹黄堡是 Lsky Pro（`db.site_publish.cover_url`，site='crabpt'）。
      ⇒ 拿 picgo 的链去发蟹黄堡 = **跨站引用**，用户已否掉。
      ⇒ 也不能反过来"就地改 `tasks.cover_url`" —— 那会把乌托邦页面上的封面顶掉
        （`ERR-META-13` 的那个形状：一个值在两个位置含义不同）。

    ★★ 三步，**每一步失败都返回空**（调用方据此**放弃发布**，见下面）：
      ① `site_publish` 里已有 ⇒ **直接用，不重传**
         （★ Lsky 没有 picgo 那种"重复上传 400 + 可回收 URL"的宽恕路径，
           重传就是真占两份容量；而 Lsky 只有 500MB）
      ② 去本地封面缓存找那张图（`cover_fetch._cached_file`，按 URL 的 sha1 前缀）
         ★ **复用既有函数**，不新写下载逻辑；★ 也**绝不**去 picgo 重传
         （那会得到一条**不该出现在蟹黄堡**的链）
      ③ `assets.upload_to_lsky()` 传上去

    ★ 为什么"失败就不发"而不是"退回用 picgo"：退回 = 往蟹黄堡写一条跨站图链，
      是**用户明确不要**的东西；而"发一条没封面的"会让页面成空壳。
      ⇒ 两个都不行 ⇒ **停下来**（发布不可逆，宁可少发一条）。
    ★ 返回 `(url, 说明)`：说明是给人看的**这一格从哪来**（同 `build_fields` 的 audit 纪律）。
    """
    h = task["info_hash"]
    site = db.SITE_CRABPT

    # ① 蟹黄堡那侧已有
    have = db.site_cover_url_of(h, site)
    if have:
        return have, f"已有蟹黄堡专用封面（site_publish）⇒ 复用，不重传：{have}"

    # ② 本地缓存里找原图。★ 本地图是用**乌托邦那个 URL** 做 key 缓存的
    #   （封面当初就是为乌托邦抓的）⇒ 拿它当 key 去查，**只查本地、不联网**。
    src = (task.get("cover_url") or "").strip()
    if not src:
        return "", "★ 库里连 cover_url 都没有 ⇒ 无源可传"
    try:
        import cover_fetch
        local = cover_fetch._cached_file(src)
    except Exception as e:                                  # noqa: BLE001
        return "", f"★ 查本地封面缓存出错: {type(e).__name__}: {e}"
    if not local:
        return "", (f"★ 本地封面缓存里没有这张图（key={src[:60]}…）⇒ "
                    f"没有可上传的源")
    if dry_run:
        # ★ dry-run **不真上传**（那是外部副作用而且是不可撤销的写）。
        #   但也**不能说"封面就绪"** —— 那是个假判据。如实报"会传这一张"。
        return src, (f"dry-run：**不会**真上传。验证通过后会传这一张上去：{local}")

    # ③ 真传
    import assets
    url = assets.upload_to_lsky(local)
    if not url:
        return "", f"★ Lsky 上传失败（本地图 {local}）—— 见上面图床那行输出"
    return url, f"新传到 Lsky：{local} ⇒ {url}"


def _crabpt_fields(task, anonymous):
    """第 3 个字段层（详见 `crabpt_site.py` 文件头与 `PLAN-crabpt.md` 附录 C）。

    ★ 为什么不写成 `lambda t: crabpt_site.build_fields(t, anonymous=anonymous)[0]`：
      那样会**丢掉 audit**，而 audit 是这个站点唯一能在提交前发现"值全是兜底"
      的东西（本文件在 2026-09-24 就吃过一次：漏 `merge_mapped` ⇒ 六个字段全变 1，
      而**审计行的排版与"映射成功"完全相同**）。⇒ 有 audit 就**打出来**。
    """
    fields, audit = crabpt_site.build_fields(task, anonymous=anonymous)
    for line in audit:
        print(f"      · {line}")
    return fields


def _crabpt_readback(client, torrent_id, fields):
    """提交后回读（判据层，不是日志层）。

    ★★ 与乌托邦那份 `fetch_edit_form` 回读的**角色相同**，但**判据不同**：
      乌托邦比的是「发出 vs 回读」（它有编辑页可读）；
      蟹黄堡这份**只能证明"页面存在"** —— 因为详情页（详情表格）与编辑页是
      两套视图，字段值不一定都露在详情页上。
    ⇒ ★ 所以这里**如实分层**：
        「详情页 200 ⇒ 这条种子确实建出来了」 ← **真判据**
        「字段值逐项比对」                  ← **没有**，不假装有
      ★ 若将来要逐字段比对，得先**采一次 `takeedit.php?id=<自己发的种>`
        的基线**（先前的采集计划正是死在这里：一条种子都没发过 ⇒ 没有编辑页；
        现在发过一条之后，这条路才**第一次**可达）。
    ★ 失败**不算阶段失败**（不 `sys.exit`）—— 与 `readd_to_qb` 的复核同一模式：
      种子**已经上站了**（不可逆），回读失败是本地问题，让 daemon 退避没有意义。
    """
    try:
        r = client.get(f"/details.php?id={torrent_id}")
        if r.status_code == 401:
            print("    ⚠️  回读详情页被拒（401）—— 但**提交已经发出**，"
                  "请手动打开站点核对")
            return
        if r.status_code != 200:
            print(f"    ⚠️  回读详情页失败：HTTP {r.status_code}")
            return
        body = r.text or ""
        # ★ 判据要"正向证据"，不能只看"没报错"：详情页上必须有那个 id 的下载链接。
        has_dl = re.search(rf"download\.php\?id={re.escape(str(torrent_id))}", body)
        print(f"    🔎 详情页 {client._url(f'/details.php?id={torrent_id}')} "
              f"HTTP 200，{len(body)} 字节，"
              f"下载链接{'在' if has_dl else '★ 不在'}")
        if not has_dl:
            print("        ★ 页面 200 但找不到该 id 的下载链接 ⇒ 这条可能不是"
                  "刚发布的那条（或页面版式变了）。请手动核对。")
            return
        # ★ 有封面就查一次（这是**我们能查的唯一一个字段**：它在简介 HTML 里，
        #   而简介在详情页上是渲染出来的）。
        cover = (fields.get("descr") or "")
        m = re.search(r'\[img\]([^\[\]]+)\[/img\]', cover)
        if m:
            url = m.group(1).strip()
            print(f"        · 简介里的封面 {'已出现' if url in body else '★ 未出现'}"
                  f"：{url[:80]}")
            if url not in body:
                print("          ★ 可能只是详情页没渲染 BBCode 的 [img]"
                      "（不等于没落地）—— 别据此下结论。")
    except Exception as e:                                  # noqa: BLE001
        print(f"    ⚠️  回读详情页出错（不影响发布本身）: {type(e).__name__}: {e}")


# ---- ★★ 负向自检：蟹黄堡这条路**不许**碰乌托邦那两列 ----
#
# ★★★ 为什么需要它（`ERR-META-01` 的形状，"检测方法失效比不检测更危险"）：
#   2026-09-25 之前，`run_crabpt` 的落库调的是 `db.set_site_id(..., site_status=...)`,
#   而那是**乌托邦**的列 ⇒ 发一条蟹黄堡就污染四个真实判据（发布队列 / 已发布集合 /
#   `run.py` 的 scan 跳过 / 巡检计数）。★ 这个错**不报错、不抛异常**，
#   页面还说"发布成功"，只有别的判据会悄悄错掉。
#   ⇒ 修法不能只是"这次改对了"，要**留一条判据让它以后也错不了**。
#
# ★ 判据取自**本文件自己的源码**（不是手抄一份清单 —— 那必然漂移，`ERR-META-03`）。
#   范式照 `flip_status.py` 的 `selftest()`：它就是这么拿 AST 抽 `publish.py`
#   的预检体去断言的（那边有条同族判据：「`site_status` 不在发布预检里」）。
_CRABPT_FORBIDDEN = ("site_status", "site_torrent_id")


def _code_only(node):
    """取出 `node` 的**代码**（丢掉注释与字符串字面量）。用于负向自检。

    ★★★ 这个函数是**必须**的，而我第一版漏了它 —— 漏掉的后果是自检当场报了个
      **假阳性**：`run_crabpt` 里那三段**注释**（"绝不碰 `site_status`"、
      "改之前这里调的是 `db.set_site_id(..., site_status=...)`"、
      "不用 `set_site_id`"）本身**就是在解释为什么不能用它们的产品**，
      而按"源码文本"去搜就把这些注释也算成了违规。
      ⇒ ★ 而假阳性比假阴性更坏：它会让人**把一条有效的判据删掉**
        （"这个自检老是误报"）—— 这条判据就永久失效了。
      ⇒ 判据必须只看**代码**：字符串与注释都是"说这件事"的地方，
        不是"做这件事"的地方（这句区分正是本函数的全部意义）。

    ★ 做法：把 `Constant`（字符串/数字字面量）与 docstring 整段替换成空，
      然后 `ast.unparse()` 回源码文本。⇒ 注释**天然**不在 AST 里，
      字符串被替换掉 ⇒ 剩下的就是真正的代码。
    """
    class _Strip(_ast.NodeTransformer):
        def visit_Constant(self, n):
            # ★ 保留数字（如 `408`），只清掉字符串 —— 我们要查的是**名字**，
            #   而 `"site_status"` 这种字符串**正是**要抓的（它可能是 getattr 的参数）。
            if isinstance(n.value, str):
                return _ast.copy_location(_ast.Constant(value=""), n)
            return n

        def visit_Expr(self, n):
            # docstring：`Expr` 里只有一个字符串常量 ⇒ 整句删掉
            if (isinstance(n.value, _ast.Constant)
                    and isinstance(n.value.value, str)):
                return None
            return self.generic_visit(n)

    return _ast.unparse(_Strip().visit(_ast.parse(_ast.unparse(node))))


def _column_offenders(node, names):
    """在 `node` 的 AST 里找出**会被求值的位置**上出现的那些列名。

    ★★★ 为什么是"按位置找"而不是"按源码文本搜" —— 这是本自检**两次**修正后的结论：
      ① 第一版按**源码文本**搜 ⇒ 把三段**注释**（"绝不碰 `site_status`"、
         "改之前调的是 `db.set_site_id(..., site_status=...)`"）算成了违规
         ⇒ **假阳性**。而假阳性比假阴性更坏：它让人**把一条有效判据删掉**。
      ② 第二版把字符串全抹掉再搜 ⇒ 修好了假阳性，但把
         `t.get("site_status")` 这种**真读法**也一起放了 ⇒ **假阴性**。
         （两个方向都试过、都被实测打脸 —— 记在 `ERR-META-01` 那一族里。）
      ⇒ ③ 现在的判据：**注释根本不在 AST 里**（天然放行），
        而字符串只有在**会被求值的位置**上才算数：
          · 下标 `t["site_status"]`
          · 关键字实参 `site_status="EXISTS"`
          · 属性 `db.site_status`
          · 位置实参 `t.get("site_status")`
        ★ 这四条覆盖了"读一列"的全部常规写法，而**纯注释/文档里的提及**不落在其中。
    ★ ★ 已知局限（如实写下，别让判据显得比实际管用）：
      `getattr(db, "site_" + "torrent_id")` 这类**拼出来的名字**抓不到。
      ★ 本自检挡的是"顺手多写一列"/"忘了写新表"这类**直白**的错，不是蓄意绕过。
    """
    hits = []
    for n in _ast.walk(node):
        if (isinstance(n, _ast.Subscript) and isinstance(n.slice, _ast.Constant)
                and n.slice.value in names):
            hits.append(f"下标 [{n.slice.value}]")
        elif isinstance(n, _ast.keyword) and n.arg in names:
            hits.append(f"关键字 {n.arg}=")
        elif isinstance(n, _ast.Attribute) and n.attr in names:
            hits.append(f"属性 .{n.attr}")
        elif isinstance(n, _ast.Call):
            for a in n.args:
                if isinstance(a, _ast.Constant) and a.value in names:
                    hits.append(f"实参 {a.value!r}")
    return hits


def selftest_crabpt_isolation(verbose=True):
    """断言 `run_crabpt` 的**代码**里不出现乌托邦那两列。返回 `(是否通过, 明细)`。

    ★ 判据取自**本文件自己的源码**（不是手抄一份清单 —— 那必然漂移，`ERR-META-03`）。
      范式照 `flip_status.py` 的 `selftest()`：它就是这么拿 AST 抽 `publish.py`
      的预检体去断言的（那边有条同族判据：「`site_status` 不在发布预检里」）。
    ★★ 具体判据与两次修正史见 `_column_offenders` 的 docstring；
      局限（拼名字抓不到）也在那里如实写明了。
    """
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = _ast.parse(src)
    fn = next((n for n in _ast.walk(tree)
               if isinstance(n, _ast.FunctionDef) and n.name == "run_crabpt"), None)
    if fn is None:
        return False, ["★ 找不到 run_crabpt —— 函数被改名了？本条自检失效"]
    code = _code_only(fn)
    # ★★ 判据分**三层**，因为"污染"有三种写法，一层拦不住另一种：
    #   ① 列名出现在会被求值的位置（`t["site_status"]` / `t.get("site_status")` / …）
    #   ② 调了 `set_site_id`（它内部写那两列）
    #   ③ 完全没写新表（"改了但没落库"）
    bad = _column_offenders(fn, set(_CRABPT_FORBIDDEN))
    detail = []
    if bad:
        detail.append(f"★★ run_crabpt 里读了/写了乌托邦的列：{bad} ⇒ "
                      f"改用 `db.site_publish_set(...)` / `db.site_*_of(...)`"
                      f"（见 `_crabpt_cover` 与落库那两段）")
    if "set_site_id" in code:
        detail.append("★★ run_crabpt 里调了 `db.set_site_id` —— 它会写乌托邦那两列")
    # ★ 正向上还要求它**必须**写新表（否则"改了但没落库"也过得了上面两条）
    if "site_publish_set" not in code:
        detail.append("★★ run_crabpt 里**没有** `db.site_publish_set` ⇒ "
                      "发布结果没落到 site_publish（新表会缺蟹黄堡的行）")
    if verbose:
        mark = "✅" if not detail else "❌"
        print(f"{mark} 蟹黄堡隔离自检：run_crabpt 不碰 site_status/site_torrent_id、"
              f"且确实写 site_publish")
        for d in detail:
            print(f"    {d}")
    return (not detail), detail


# ---- ★★ 负向自检：转种队列的判据**确实是** `db.transferable_to` ----
#
# ★★★ 为什么需要它（`ERR-META-31` 的形状：状态文件的两种历史序列化后一样、
#   携带不了那个区分）：
#   用户 2026-09-27 拍板「需求是【转种】，目标站缺、源站有的才转过去」。
#   而**忘记**这个筛选**不报错、也不抛异常** —— 表现只是"蟹黄堡照发"，
#   而下面 `site_publish` 的判重**拦不住**（它问的是"蟹黄堡发过没有"，
#   对"这条在**乌托邦**发过"一无所知）⇒ **静默多发**，多发不可撤销。
#
# ★ 判据取自**本文件自己的源码**（AST），不是手抄清单 —— 那必然漂移。
#   具体断两件事：
#     ① `run_crabpt` **必须**调 `db.transferable_to`（而不是 `get_pending_publish`）
#     ② `run_crabpt` **不许**调 `db.get_pending_publish()` 当**选行**用
#        ★ 注意判据要**精确到语法位置**，不能只 `if "get_pending_publish" in code`：
#          那会误伤我特意写进 `run_crabpt` 的那段**子集断言**
#          （它**故意**调一次 `db.get_pending_publish()` 来求差集）。
#          ⇒ 只检查"它有没有出现在**赋值给 `tasks`/`hit`** 的位置"
#          —— 那才是"拿它当选行的判据"。
def selftest_crabpt_transfer_queue(verbose=True):
    """断言 `run_crabpt` 的选行判据是**转种**（`db.transferable_to`）。"""
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = _ast.parse(src)
    fn = next((n for n in _ast.walk(tree)
               if isinstance(n, _ast.FunctionDef) and n.name == "run_crabpt"), None)
    if fn is None:
        return False, ["★ 找不到 run_crabpt —— 函数被改名了？本条自检失效"]
    code = _code_only(fn)
    detail = []

    # ① 必须调 transferable_to（"改了但没换队列"要能被抓到）
    if "transferable_to" not in code:
        detail.append("★★ run_crabpt 里**没有** `db.transferable_to` ⇒ "
                      "用的是发布队列（`PENDING_PUBLISH_WHERE`），"
                      "而它含 `site_status != 'EXISTS'` ⇒ 转种队列**恒为空**"
                      "（实测 0 条）")

    # ② 不许把 `get_pending_publish()` 的**返回值**当选行判据。
    #    ★ 判据是"有没有 `x = db.get_pending_publish()` 这种赋值"，
    #      而**不是**子串出现 —— 见上面那段注释（子集断言故意调它）。
    for node in _ast.walk(fn):
        if isinstance(node, (_ast.Assign, _ast.AnnAssign)):
            val = node.value
            if val is None:
                continue
            # 剥掉 `list(...)` / `[t for t in ...]` 之外的原样调用
            if (isinstance(val, _ast.Call)
                    and getattr(val.func, "attr", "") == "get_pending_publish"):
                tgt = node.targets[0] if isinstance(node, _ast.Assign) else node.target
                nm = getattr(tgt, "id", "?")
                detail.append(f"★★ run_crabpt 里 `{nm} = db.get_pending_publish()` "
                              f"⇒ 又拿**发布队列**当选行判据了（转种队列必须用 "
                              f"`db.transferable_to`）")

    # ③ 队列来源必须显式带站点（`db.SITE_CRABPT`）—— 否则默认参数会静默
    #    变成 `SITE_MUSOPIA`，而那会让筛选"查自己站发过没有" ⇒ 恒为空。
    ok_kw = False
    for node in _ast.walk(fn):
        if isinstance(node, _ast.Call) and getattr(node.func, "attr", "") == "transferable_to":
            if node.args:
                a = node.args[0]
                if getattr(a, "attr", "") == "SITE_CRABPT":
                    ok_kw = True
            for kw in node.keywords:
                if getattr(kw.value, "attr", "") == "SITE_CRABPT":
                    ok_kw = True
    if "transferable_to" in code and not ok_kw:
        detail.append("★★ 调 `transferable_to` 时**没有**显式传 `db.SITE_CRABPT` "
                      "⇒ 落到默认参数 `SITE_MUSOPIA`，筛选变成'乌托邦查乌托邦'，"
                      "队列恒为空")

    if verbose:
        mark = "✅" if not detail else "❌"
        print(f"{mark} 转种队列自检：run_crabpt 用 db.transferable_to(SITE_CRABPT) 选行")
        for d in detail:
            print(f"    {d}")
    return (not detail), detail


def selftest_crabpt_explain_same_source(verbose=True):
    """★★★ 断言「**队列口径**（`db.transferable_to`）与**解释口径**
    （`db.is_transferable_to`）」**同源** —— 两条路对同一行必须给同一个答案。

    ★★★ 为什么必须有（2026-09-28 实测事故，`ERR-META-32` 一族）：
      同一个命令里两句话互相矛盾：

        --site crabpt --dry-run --only 144f017a
          ❌ 144f017a 不在转种队列里          ← 队列**实际筛选**（对）
          ✅ 它**在**转种队列里（源站有…）    ← **解释文案**（错）

      ★ 根因：`transferable_to()` 当时已改用 `TRANSFERABLE_WHERE`（含闸门④
        `ALLOW`），而 `is_transferable_to()` **只判「源有 ∧ 目标没有」**
        —— 少了闸门④ 那一层 ⇒ **两个判据对同一行给出相反答案**。
      ★ 危害形状（`ERR-META-14`）：不是"多发一条"（数据风险），而是
        **工具告诉人"它在队列里"、实际不会发** ⇒ **误导人做决定**。
      ★ 一般化：**给人看的判据必须与实际判据同源**；否则读的人只能猜
        哪句是真的。⇒ 所以这里**不靠注释承诺**，靠一条**可执行判据**。

    ★★ 判据为什么必须走**行为**（拿内存 fixture 跑两个函数）而不是 AST：
      AST 只能查"有没有闸门④ 这个词"—— 那正是 `ERR-META-30` 的坑
      （**提到了对的词、却问了个错的问题**：`!= 'DENY'` 也含这个词，
       却与闸门④ 语义相反）。⇒ 只有**真跑一遍**才测得到"答案相不相同"。

    ★ fixture 是**内存** sqlite（`connect()` 走 `config.DB_PATH`，
      本函数临时改写它并复原）—— **不碰生产库**、不联网、无副作用。
    """
    import tempfile
    detail = []
    # fixture：四条，覆盖闸门④ 的三档 + 一条正常的 ALLOW
    #   hash 前 12 位都是 `aaaa…` / `dddd…` 便于人读
    #
    # ★★ `site_torrent_id` 必须**非空**（这里给站点 id 的字符串形态）——
    #   因为 `CROSS_SITE_DEDUP_WHERE`（转种口径的前缀）里含
    #   `site_torrent_id IS NOT NULL AND site_torrent_id != ''`：
    #   它是"**乌托邦上有这条**"那个事实判据的一部分。
    #   ★ 实测踩到过：第一版给空串 ⇒ **四条全部落不进队列**，
    #     于是判据报"口径分叉（EXISTS ∧ ALLOW）" —— 而那是**我 fixture 的错**，
    #     不是产的错。⇒ 判据设计上要能区分"分叉"与"根本没测到"：
    #     下面除了比对两个口径，还**单独断言** ALLOW 那条**必须在队列里**
    #     （`should_be=True`），否则 fixture 自己坏掉时它不会冒充"通过"。
    H_ALLOW = "aaaa00000000" + "0" * 28     # EXISTS ∧ ALLOW ∧ crabpt 无 ⇒ **该转**
    H_DENY = "dddd00000000" + "0" * 28      # EXISTS ∧ DENY  ⇒ **闸门④ 拦**
    H_EMPTY = "eeee00000000" + "0" * 28     # EXISTS ∧ 规则空 ⇒ **闸门④ 拦**
    H_CONF = "cccc00000000" + "0" * 28      # EXISTS ∧ NEED_CONFIRM ⇒ **闸门④ 拦**
    rows = [
        (H_ALLOW, "EXISTS", "10001", "ALLOW"),
        (H_DENY, "EXISTS", "10002", "DENY"),
        (H_EMPTY, "EXISTS", "10003", None),
        (H_CONF, "EXISTS", "10004", "NEED_CONFIRM"),
    ]
    import sqlite3
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    orig_db_path = config.DB_PATH
    try:
        con = sqlite3.connect(path)
        # ★★ fixture 的表必须与**生产表同形**（列**全**建出来，值留空）——
        #   理由：`db.transferable_to()` 走 `SELECT {db.TASK_FIELDS}`，
        #   若 fixture 少一列，它会 `OperationalError` ⇒ 本条自检会以
        #   "**没能跑起来**"收尾（**不是通过** —— 见下面 except 支）。
        #   ★ 实测踩到两次：第一版只建了 3 列（`no such column: torrent_name`）；
        #     改用 `db.TASK_FIELDS` 后又少 `transfer_rule`
        #     （它**故意不在** `TASK_FIELDS` 里 —— 那是渲染字段表，
        #      `transfer_rule` 是"许不许发"的判据，见 `COLUMNS` 里的注释）。
        #   ⇒ 正确取法：**从 schema 常量建表**（`db.COLUMNS` /
        #     `db.SITE_PUBLISH_COLUMNS`），而不是从 `TASK_FIELDS` 或手抄清单
        #     —— 手抄/取错源都会让本条自检在**别人加列的那天**静默失效
        #     （与它要防的形状同族，`ERR-META-03`）。
        def _mk(cols):
            return ", ".join(f"{n} {t}" for n, t in cols)

        con.execute(f"CREATE TABLE tasks ({_mk(db.COLUMNS)})")
        con.execute(f"CREATE TABLE site_publish ({_mk(db.SITE_PUBLISH_COLUMNS)})")
        for h, st, sid, rule in rows:
            con.execute(
                f"INSERT INTO tasks (info_hash, site_status, site_torrent_id,"
                f" transfer_rule) VALUES (?,?,?,?)", (h, st, sid, rule))
            # 源站（乌托邦）已上站 —— 让判据① 成立，把差别**只留**给闸门④
            # ★ 列名**显式列出**（不靠位置）—— fixture 的表列比这里写的多
            #   （`synced_at` 等），靠位置会在别人加列时静默错位。
            con.execute("INSERT INTO site_publish"
                        " (info_hash, site, torrent_id, status, cover_url)"
                        " VALUES (?,?,?,?,?)",
                        (h, db.SITE_MUSOPIA, "10000", "EXISTS", None))
        con.commit()
        con.close()

        config.DB_PATH = path
        # 队列口径（实际选行）
        queued = {(t.get("info_hash") or "") for t in db.transferable_to(db.SITE_CRABPT)}
        # 解释口径（给人看的 why）
        for h, label, should_be in [
            (H_ALLOW, "EXISTS ∧ ALLOW", True),
            (H_DENY, "EXISTS ∧ DENY", False),
            (H_EMPTY, "EXISTS ∧ 规则空", False),
            (H_CONF, "EXISTS ∧ NEED_CONFIRM", False),
        ]:
            in_q = h in queued
            ok, why = db.is_transferable_to(h, db.SITE_CRABPT)
            if in_q != ok:
                detail.append(
                    f"✗ ★★ 口径分叉（{label}）：队列认为 in_queue={in_q}，"
                    f"但 `is_transferable_to()` 说 ok={ok}（why={why!r}）\n"
                    f"     ⇒ 这正是 `--only` 报文与实际筛选**互相矛盾**的形状"
                    f"（ERR-META-32）。两个判据必须同源。")
            if ok != should_be:
                detail.append(
                    f"✗ ★★ `is_transferable_to()` 对（{label}）答 ok={ok}，"
                    f"应当为 {should_be}（why={why!r}）\n"
                    f"     ⇒ 闸门④ 的语义是'**只有 `ALLOW` 放行**'："
                    f"`DENY` / 空 / `NEED_CONFIRM` **一律拦**。")
            # ★ 报文必须**说清是哪一档**（ERR-SITE-06：多种原因不许压成一句）
            if not ok and ("闸门④" not in why and "源站" not in why
                           and "目标站" not in why):
                detail.append(f"✗ 报文没点明卡在哪一条判据：{why!r}")
    except Exception as e:                                        # noqa: BLE001
        detail.append(f"✗ ★ 本条自检**没能跑起来**（不是通过）：{type(e).__name__}: {e}")
    finally:
        config.DB_PATH = orig_db_path
        try:
            os.remove(path)
        except OSError:
            pass

    if verbose:
        mark = "✅" if not detail else "❌"
        print(f"{mark} 转种口径自检：队列（transferable_to）与解释"
              f"（is_transferable_to）**对同一行说同一句话**")
        for d in detail:
            print(f"    {d}")
    return (not detail), detail


def run_crabpt(args):
    """蟹黄堡（crabpt.vip）发布路径。★ 与 `main()` 的乌托邦路径**并列**，不互相调用。

    ★★★ 为什么**不**共用 `main()` 那个循环：见 `parse_args()` 里 `--site` 的注释
      —— 共用的是**不变量**（都在本函数里逐条照做），而不是**字段与提交**。
      本函数里属于"不变量"的每一处都**明写了它对应 `main()` 的哪一段**，
      将来改一边就能立刻看出另一边要不要跟。

    ★★ **已接进 daemon**（2026-09-27，`daemon.py` 的 `publish_crabpt` 阶段，
      开关 `ENABLE_PUBLISH_CRABPT`）。原注释写的是"刻意不接"，那条**已作废** ——
      ★ 但当时那段话里另有一条**仍然成立**、且是本次真正被推翻的东西：
        「在'发一条、回读一条'验证通过之前，不让它进入无人值守路径」。
      ⇒ 用户 2026-09-27 拍板**先接**（"现在就接，并放宽护栏"），
        验证改为**事后**做（先发三条看效果）。所以现在的状态是：
        **开关关着**（`.env` 里 `ENABLE_PUBLISH_CRABPT=false`），
        接线的**代码**已就位 —— 打开开关即生效，不必再改代码。

    ★★ **队列是"转种队列"而不是"发布队列"**（2026-09-27 用户拍板，见下面
      那段长注释）：本函数选行用 `db.transferable_to()`，
      **不是** `db.get_pending_publish()`。这是与乌托邦路**最大的**语义差异。
    """
    import crabpt_site

    print("=" * 60)
    print("🌐 站点 = 蟹黄堡（crabpt.vip）★ 与乌托邦完全独立的一条路径")
    print("=" * 60)

    # ---- ★★ 两个安全护栏：先立起来，再干别的 ----
    #
    # ① `--only` **可选**（2026-09-27 用户拍板放宽，原先必须）。
    #
    #   ★★ 改动史（别把这段当"随便加的"）：
    #      2026-09-25 加时写的是「**必须**带 --only」，理由逐字如下：
    #        · 乌托邦已经发过 254 条、常规批量发布是**它的日常**；
    #          蟹黄堡**一条都没发过** ⇒ 第一条的意义是"验证字段层对不对"，
    #          而"发什么"必须由人点名，不能由 SQL 排序决定（发布不可逆）。
    #      ★ 那条理由**当时是对的**，但它锚在"从未验证过"这个**临时状态**上。
    #        2026-09-27 用户决定**接进 daemon**（`ENABLE_PUBLISH_CRABPT`），
    #        即要它**无人值守地按队列发** —— 而不带 `--only` 正是那件事的形态。
    #        ⇒ 护栏锚点消失，`--only` 从"必须"退回"可选"。
    #
    #   ★★★ 放宽后仍然成立的**安全性质**（这些一条都没丢）：
    #      · `--only` **给了**就仍然只发点名的那些（下面那套 `hit`/`absent`
    #        逻辑一个字没改，且"零交集 ⇒ 拒绝执行"仍在）
    #      · 两把钥匙（`ENABLE_*` + `PUBLISH_I_AM_HERE`）仍在
    #      · `PENDING_PUBLISH_WHERE` 四道闸门仍在（含闸门④ transfer_rule）
    #      · 预检（缺封面/缺种子文件）仍在
    #      · `site_publish` 判重仍在 ⇒ **不会重复发**
    #   ⇒ 放宽的是"**谁决定发哪条**"（人点名 → 队列排序），
    #     不是"**能不能发**"。

    # ② `--dry-run` 不再是强制（2026-09-27 同一次拍板放宽）。
    #
    #   ★ 原话：「安全默认：`--site crabpt` **强制先走 dry-run**」——
    #     同样是锚在"从未跑过"上的临时护栏。要接 daemon 就必须能真发。
    #
    #   ★★★ 但**放宽不等于取消**：真发这条路上仍要求第二把钥匙。
    #     这是**唯一**能防"人以为在预览、其实真发了"的机制。
    #     `--auto` 的语义（见 `parse_args`）就是"声明这是无人值守路径"
    #     ⇒ 校验 `PUBLISH_I_AM_HERE`。**daemon 会传 `--auto`**。
    if args.dry_run:
        print("\n★ 显式 dry-run：不会向站点提交任何数据。")
    elif not args.auto:
        # ★ 没声明无人值守、又没要 dry-run ⇒ 这是"人手工真发"。
        #   与乌托邦 `main()` 的交互确认**不同**：那边有 `input()`。
        #   这里**不**加 `input()`（容器里没有 TTY，`input()` 会 EOFError 崩掉
        #   —— 2026-09-24 实测过那个形状），而是在下面出口显式打印"即将真发"。
        print("\n★★ 真发模式（没有 --dry-run，也没声明 --auto）：")
        print("   请确认你确实要往**蟹黄堡**提交 —— 发布**不可逆**、无批量撤销。")

    # ---- ★★★ 队列：**不是** `get_pending_publish()`，是**转种队列** ----------
    #
    # 用户 2026-09-27 原话：「需求是【转种】，一条种子不是每个站都要有，
    #   是**目标站缺、而源站有**的，才转过去」。
    #
    # ★★ 为什么这里**不能**用 `db.get_pending_publish()`（改之前就是它，
    #   而它让队列**恒为空** —— 见下面那段实测）：
    #   `PENDING_PUBLISH_WHERE` 里那句 `site_status != 'EXISTS'` 问的是
    #   「**乌托邦**发过没有」，而转种要问的是「**蟹黄堡**发过没有」。
    #   ⇒ 转种恰恰需要 `site_status = 'EXISTS'`（**有源才可转**），
    #     与那句 WHERE **方向相反** ⇒ 拿它当队列 = 一条都选不出来（实测 0 条）。
    #
    # ★★★ 为什么**不**去改 `PENDING_PUBLISH_WHERE` 那句（那是最自然的"修法"，
    #   而它是错的）：那条 WHERE 被**两个消费者**共用 —— `main()`（乌托邦）
    #   和本函数（蟹黄堡）。乌托邦靠那句拿"还没发过的新活"。
    #   ⇒ 为蟹黄堡把 `!=` 改成 `=`，乌托邦队列立刻**全空**。
    #   **改一条共用判据 = 静默改掉另一个消费者的行为**（它不会报错，
    #     只会表现为"乌托邦不再发新种"，而日志里一切正常）。
    #   ⇒ 正确做法：判据本身**有名字**（`db.transferable_to()`），
    #     而不是把 SQL **手抄**进这里（手抄 = `ERR-META-03` 的漂移形状：
    #     将来闸门改了、这里没跟，两边静默分叉）。
    #
    # ★★ 忘记"排除已发过蟹黄堡的"会怎样（**这是本条最危险的失败模式**）：
    #   下面 `site_publish` 的判重**拦不住它** —— 那个判重问的是
    #   "**蟹黄堡**发过没有"，对"这条在**乌托邦**发过"一无所知。
    #   ⇒ 结果是把乌托邦已发的那批**反复搬**到蟹黄堡 = **静默多发**。
    #     多发**不可撤销**（蟹黄堡没有批量删种），而少发只是补一次。
    #   ⇒ 所以这条判据必须**是函数**、必须**有阴性对照**（见
    #     `selftest_crabpt_transfer_queue()`：把筛选去掉，自检**必须**报红）。
    tasks = db.transferable_to(db.SITE_CRABPT)
    if not tasks:
        print("💡 当前没有可转的种（源站有、蟹黄堡没有、**且过了闸门④ ALLOW**）——"
              f"判据 = `db.TRANSFERABLE_WHERE` = `{db.CROSS_SITE_DEDUP_WHERE}` "
              f"+ `transfer_rule = 'ALLOW'`，再加蟹黄堡无记录。")
        return

    # ---- ★ 可执行断言：转种队列**必须**是"已过闸门④"的子集 -----------------
    #
    # ★ 为什么需要它（这是**推理**，不是上面那条 SQL 自己保证的东西）：
    #   本节开头论证过「能上乌托邦 ⇒ 已过闸门④ `transfer_rule='ALLOW'`」。
    #   ★★★ 2026-09-28 **实测：那条推理不成立** —— `EXISTS` 的 28 行里
    #   有 1 行是 `DENY`（`site_torrent_id=15972`）⇒ 若转种路只按"已上站"筛，
    #   这一行会被搬到蟹黄堡（**不可逆**）。
    #   ⇒ 修法**不在这里**，而在 `db.transferable_to()` 的口径：
    #     它现在用 `db.TRANSFERABLE_WHERE`（= CROSS + `transfer_rule='ALLOW'`）。
    #   ★★ 而本条断言**仍然用 `CROSS_SITE_DEDUP_WHERE`**（**不**跟着换成
    #     `TRANSFERABLE_WHERE`）—— 这是**故意的**，不是漏改：
    #       · 它问的问题是「**事实**（已上站）里有哪些**没过**闸门④」；
    #       · 换成 `TRANSFERABLE_WHERE` 后这个差集**恒为空**（集合里已经没有
    #         非 ALLOW 的行可找）⇒ 它就成了一段"永远显示通过"的废码
    #         （`ERR-META-14` 里最贵的一种：**检测器静默失效**）。
    #       · 留着它 = 留一条**回归探针**：将来若有人再让某行绕过闸门④ 上站
    #         （手工 `set_site_id`、或新增一条不查闸门的路径），差集会**重新
    #         变非空**，而它指着的正是那句话。**那天它是唯一会响的东西。**
    #
    # ★★ 为什么是断言（打印 + 非零退出）而不是 `sys.exit(0)` 静默通过：
    #   本项目的纪律是"判据要接到能让人看见的地方"。这里**不拦**执行
    #   （拦住会让一条`NEED_CONFIRM`把整批卡死），而是**大声报出来** ——
    #   它是一条**应当恒为空**的差集。非空 = 有东西违反了推理，要人看一眼。
    _gaps = []
    try:
        _pend = {(t.get("info_hash") or "") for t in db.get_pending_publish()}
        _on_muso = {(t.get("info_hash") or "") for t in db.get_published(
            torrent_id=None, only_with_cover=False)}
        if _on_muso:
            _con = db.connect()
            _rows = _con.execute(
                "SELECT info_hash, transfer_rule FROM tasks "
                f" WHERE {db.CROSS_SITE_DEDUP_WHERE} "
                "   AND (transfer_rule IS NULL OR transfer_rule != 'ALLOW')"
            ).fetchall()
            _con.close()
            _gaps = [(r[0], r[1]) for r in _rows]
    except Exception as e:  # noqa: BLE001
        # ★ 断言自身出错**不许**装成"通过"（`ERR-META-14`：兜底值不许冒充真结果）。
        print(f"⚠️  子集断言**没能跑起来**（不是通过）：{e}")
    if _gaps:
        print("\n" + "!" * 60)
        print(f"★★ **已上站、却没标 ALLOW** 的有 {len(_gaps)} 条"
              f"（应当恒为 0）——")
        print("   它们能上乌托邦，说明闸门④当年被绕过了（或有人手工标了 site_status）；")
        print("   ★ 它们**不会**进本次转种队列（队列口径 = ALLOW），但请人工过一遍：")
        for h, rule in _gaps[:10]:
            print(f"      {h[:12]}  transfer_rule={rule!r}")
        if len(_gaps) > 10:
            print(f"      …另有 {len(_gaps) - 10} 条")
        print("!" * 60)

    # ★★ `--only` 给了 ⇒ 只发点名的；**没给** ⇒ 按队列（= 无人值守那条路）。
    #    ★ 两条路的**其余部分完全一样**（下面的预检、提交、落库都不分叉），
    #      这里只决定 `hit` 怎么来 —— 单一分叉点，好审计。
    if args.only:
        want = tuple(s.strip().lower() for s in args.only.split(",") if s.strip())
        hit = [t for t in tasks
               if any((t.get("info_hash") or "").lower().startswith(w) for w in want)]
        in_queue = {(t.get("info_hash") or "").lower() for t in tasks}
        absent = [w for w in want if not any(h.startswith(w) for h in in_queue)]

        print("\n" + "=" * 60)
        print(f"★ --only：点名 {len(want)} 个 ⇒ 命中转种队列 {len(hit)} 条")
        print("=" * 60)
        for t in hit:
            print(f"   ✅ {(t.get('info_hash') or '')[:12]}  {t.get('formatted_title')}")
        for w in absent:
            print(f"   ❌ {w} 不在转种队列里（被闸门拦下 / 已发过 / hash 打错）")
        if absent:
            # ★ 同一份判据（`explain_not_transferable`），不另写一套。
            print("\n   ★ 逐条查为什么没进转种队列：")
            for w in absent:
                for line in explain_not_transferable(w):
                    print(f"      {line}")
        if not hit:
            sys.exit("❌ --only 与转种队列**零交集** ⇒ 拒绝执行（不会去发队列里的别的）")
    else:
        # ★ 无人值守那条路：按队列取。★ `--limit` 与乌托邦同义（截前 N 条）。
        hit = list(tasks)
        print("\n" + "=" * 60)
        print(f"★ 无 --only ⇒ 按**转种**队列取（无人值守路），队列 {len(hit)} 条")
        print("=" * 60)
        for t in hit[:12]:
            print(f"   · {(t.get('info_hash') or '')[:12]}  "
                  f"{(t.get('formatted_title') or '')[:64]}")
        if len(hit) > 12:
            print(f"   …另有 {len(hit) - 12} 条")

    # ---- 预检：★ 与 main() 同一套（缺封面 / 种子文件 / 无 log）----
    valid, blocked = [], []
    for i, t in enumerate(hit, 1):
        reasons = []
        if not (t.get("cover_url") or "").strip():
            reasons.append("缺封面")
        tf = t.get("torrent_file")
        if not tf or not os.path.exists(tf):
            reasons.append("种子文件缺失")
        # ★★ 蟹黄堡**允许无 LOG**（实测：站点规则不要求），这一点**与乌托邦不同**。
        #   ⇒ 这里**不拦**无 log。★ 但要说清"没有"与"没拦"是两回事：
        #     不拦不等于有数据 —— 无 log 时 `build_descr()` 会少一段，
        #     这是**已知且可接受**的差异，不是缺陷被忽略。
        has_log = bool((t.get("log_content") or "").strip())
        if not has_log:
            try:
                lj = json.loads(t.get("logs_json") or "null")
                has_log = bool(lj) and any(
                    (x.get("content") or "").strip()
                    for x in lj if isinstance(x, dict))
            except (ValueError, TypeError):
                pass
        # ★ 蟹黄堡的**必填**项里有一项乌托邦没有：简介（`descr`）。
        #   空简介发上去 = 一条不可撤销的、页面是空壳的种子 ⇒ 拦在这里。
        if not (t.get("cover_url") or "").strip() and not (t.get("tracklist") or "").strip():
            reasons.append("简介与封面都为空 ⇒ 蟹黄堡的 descr 会是空壳")

        flag = "✅ 可发布" if not reasons else "❌ " + "、".join(reasons)
        print(f"\n[{i}] {t.get('formatted_title')}")
        print(f"    副标题: {t.get('small_descr') or '(空)'}")
        print(f"    log: {'有' if has_log else '无（★ 蟹黄堡不要求，不拦）'}")
        print(f"    {flag}")
        (valid if not reasons else blocked).append(t)

    if blocked:
        print(f"\n⚠️  {len(blocked)} 条被拦下（见上）。")
    if not valid:
        print("没有符合发布条件的任务，退出。")
        return
    if args.limit:
        valid = valid[:args.limit]
    # ★★ 未显式给 `--limit` 时，蟹黄堡这条路**一次最多 1 条**（2026-09-27 放宽后）。
    #
    #   ★ 改动史：原来这里是**无条件** `valid = valid[:1]`（无论给没给 --limit），
    #     锚在"这条路从未验证过"上。2026-09-27 用户决定接 daemon
    #     ⇒ 无人值守要能按 `CRABPT_MAX_PER_CYCLE` 发 N 条，
    #       所以上限改由 `--limit` **显式**表达（daemon 会传它）。
    #
    #   ★★★ 但护栏本身**没有取消**，只是从"写死 1"变成"**默认 1**"：
    #        给了 `--limit N` ⇒ 发 N 条（调用方**显式声明**了要多发）；
    #        没给          ⇒ 仍然只发 1 条。
    #      ⇒ 这个方向是对的：**不可逆操作的默认值必须保守**，
    #        放开要有人显式写一个数（与 `ERR-ENV-02` 那条"缺键时默认中性"同族，
    #        只是这里中性 = 保守）。
    #   ★ 若真发了多条，**回读确认**仍是后面的步骤（见下面的比对段）。
    elif len(valid) > 1:
        print(f"\n★★ 蟹黄堡路径未给 --limit ⇒ 一次只发 1 条（本次命中 {len(valid)} 条）。")
        print(f"   ★ 理由：发布不可逆。要一次多发请在命令行**显式**给 "
              f"`--limit N`（daemon 用的是 `CRABPT_MAX_PER_CYCLE`）。")
        valid = valid[:1]

    # ---- 提交前：读发布页（★ 唯一能在提交前发现"没有发种权限"的判据）----
    client = crabpt_site.CrabptClient()
    if not client.have_cookie():
        sys.exit("❌ CRABPT_COOKIE 是空的 ⇒ 拒绝提交。\n"
                 "   ★ 必须在 **NAS 的 .env** 里加（`deploy.sh` 的白名单不含 `.env`，"
                 "见 ERR-ENV-02），然后 `sudo docker compose up -d --build`。")

    # ★★ qB 连接（回加做种用）—— 2026-10-02 加。
    #   ★ 与 `main()` 同形：`--no-readd` 时才不连；连不上**只说清原因、不中断**
    #     （回加失败是本地问题，不该让"已经发上站"的这条看起来像失败）。
    qb = None
    if not args.no_readd:
        qb = connect_qb()

    form_info = None
    if not args.dry_run:
        try:
            uid = client.check_login()
            print(f"✅ 蟹黄堡 Cookie 有效，user_id = {uid or '(页面上没读到，不影响发布)'}")
            form_info = client.fetch_upload_form()
            print(f"   · 发布页 OK：action={form_info['action']!r}、"
                  f"enctype={form_info['enctype']!r}、"
                  f"{len(form_info['actions'])} 个字段、"
                  f"csrf={list(form_info['csrf']) or '（站点没给）'}")
            if "multipart" not in form_info["enctype"]:
                print(f"   ★ 注意：发布页的 enctype={form_info['enctype']!r} 不是 "
                      f"multipart ⇒ 上传文件可能不被识别，**先停下来人工确认**。")
        except SiteError as e:
            sys.exit(f"❌ {e}")

    # ---- dry-run：把字段与**来源**逐项打出来 ----
    if args.dry_run:
        t = valid[0]
        print("\n" + "=" * 60)
        print(f"🔍 dry-run：{t.get('formatted_title')}")
        print("=" * 60)
        # ★★ 封面也要**走一遍**（`dry_run=True` ⇒ 只查不传）。
        #   ★ 为什么不能跳过这一步：不跑它就看不出一件最要紧的事 ——
        #     **本地到底有没有那张图**。而"没有图"正是真发布时会**拒绝发**的原因。
        #     ⇒ 跳过 = 预览说"一切就绪"、真跑时却在最后一步退出（假绿色）。
        #   ★ 但 `dry_run=True` 分支**不真上传**（那是不可撤销的外部写）。
        cover, cover_why = _crabpt_cover(t, dry_run=True)
        print(f"封面 ← {cover_why}")
        if not cover:
            print("   ★★ 注意：按现在的状态**真发布会在这里停下**（不发）—— 见上。")
        else:
            t = dict(t)
            t["cover_url"] = cover
        try:
            fields, audit = crabpt_site.build_fields(t, anonymous=args.anonymous)
        except ValueError as e:
            sys.exit(f"❌ 渲染字段失败: {e}")
        print("字段来源（每一格从哪来）：")
        for line in audit:
            print(f"  · {line}")
        print("\n将要提交的表单：")
        for k, v in fields.items():
            shown = v if not isinstance(v, str) else v.replace("\n", "\\n")
            if isinstance(shown, str) and len(shown) > 160:
                shown = shown[:160] + f"…（共 {len(v)} 字符）"
            print(f"  {k} = {shown!r}")
        print(f"\n  file = {t.get('torrent_file')}")
        print("\n★ 以上仅为预览，**没有向蟹黄堡提交任何数据**。")
        print("★ 看过之后去掉 --dry-run 再跑同一条命令才会真发布。")
        return

    # ---- 确认（★ 与 main() 同一处语义：`--yes` 跳过，`--auto` 另查钥匙）----
    t = valid[0]
    if not args.yes:
        answer = input(f"\n确认向**蟹黄堡**发布这 1 条吗？(y/N): ").strip().lower()
        if answer != "y":
            print("🚫 已取消，未向站点提交任何数据。")
            return

    # ★★★ 第二把钥匙 —— **在建立任何客户端 / 发任何请求之前**。
    #   位置与判据**逐字**沿用 `main()`（同一份 `guard_auto_key()`）：
    #   钥匙没通过时一个请求都不该发出去。★ 蟹黄堡这条路虽然现在只有人手工跑，
    #   但将来接进 daemon 时它**必然**带 `--auto` ⇒ 这段现在就得在。
    if args.auto:
        key_ok, key_detail, key_why = guard_auto_key()
        if not key_ok:
            print("=" * 60)
            print(f"🛑 拒绝自动发布（蟹黄堡）：第二把钥匙没通过 —— {key_detail}")
            print(explain_key_reason(key_why))
            print("=" * 60)
            sys.exit(3)

    # ---- 提交 ----
    #
    # ★★ 封面必须在**渲染字段之前**解决 —— `descr` 是提交内容的一部分，
    #   而封面就写在 `descr` 里（`[img]…[/img]`）。顺序反了就等于没换。
    #   ★ 而且这里**失败就退出**（不发）：见 `_crabpt_cover` 的 docstring
    #     —— 两个退路（退回 picgo 的链 / 发一条没封面的）都被用户否掉了。
    crabpt_cover, cover_why = _crabpt_cover(t)
    print(f"   · 封面 ← {cover_why}")
    if not crabpt_cover:
        sys.exit("❌ 蟹黄堡封面没准备好 ⇒ **拒绝发布**（发布不可逆，宁可少发一条）。\n"
                 "   ★ 修法：先让这条种子在本地有封面（`python backfill_covers_opencd.py`），\n"
                 "     或检查 NAS `.env` 里的 `LSKY_TOKEN`（见 ERR-ENV-02）。")
    # ★ 只覆盖**这一份 task 拷贝**的 `cover_url`，`build_fields` 会用它渲染 `descr`。
    #   ★ 不改 `build_fields` 的签名 —— "用哪个封面"是**调用方的决定**，
    #     而它只管渲染（两站共用同一个渲染函数喂不同的 URL，见 PLAN 7b）。
    #   ★ 也**不**回写 `tasks.cover_url`（那是乌托邦那一份，别顶掉它）。
    t = dict(t)
    t["cover_url"] = crabpt_cover

    fields = _crabpt_fields(t, args.anonymous)
    print(f"\n🚀 向蟹黄堡提交：《{t.get('formatted_title')}》")
    try:
        tid, msg = client.upload_torrent(t.get("torrent_file"), fields, form_info)
    except (SiteError, LoginExpired) as e:
        sys.exit(f"❌ {e}")

    if not tid:
        # ★ 说真话的退出码（同 `main()` 末尾那段纪律）：有失败 ⇒ 非 0。
        #   而且这条路径必须非 0：**站点可能已经建了种子而我们没读到 id**，
        #   非 0 会让调用方**停下来**而不是"照常重来"。
        sys.exit(f"❌ 蟹黄堡发布未取得站点 id：{msg}")

    print(f"    🎉 已提交，站点 ID = {tid}")
    # ★★★ 落库：**只写蟹黄堡那一行**，绝不碰 `site_status`/`site_torrent_id`
    #   （2026-09-25 修 —— 这两列是**乌托邦**的）。
    #
    #   ★ 改之前这里调的是 `db.set_site_id(..., site_status="EXISTS")`，
    #     它写的是 `tasks` 上那**唯一一列** ⇒ 发一条蟹黄堡就等于对乌托邦宣称
    #     "这条已发过"，于是**四个真实判据**同时被污染（发布队列 / 已发布集合 /
    #     `run.py` 的 scan 跳过 / 巡检计数）。详见 `db.SITE_PUBLISH_TABLE` 那段。
    #   ⇒ 现在"发到哪个站"存在**它自己的表**里 `(info_hash, site)`。
    #
    #   ★ `cover_url` 也一并落在这里（**每站一份**）：`_crabpt_cover()` 传进来的
    #     是 Lsky 的链，与乌托邦那份 picgo 的链**互不影响**。
    db.site_publish_set(t["info_hash"], db.SITE_CRABPT, tid,
                        cover_url=crabpt_cover or None)
    # ★ `status` / `reason` 仍走 `save_task`：它们是**本地处理状态**（与站点无关），
    #   而 `flip_status.py` 的分类判据依赖 `status`。
    #   ★ 不用 `set_site_id` —— 那会顺手写乌托邦那两列（就是上面刚修掉的 bug）。
    db.save_task(info_hash=t["info_hash"], status="PUBLISHED",
                 reason=f"crabpt_published_id:{tid}")
    db.mark_synced(t["info_hash"])
    print("    ℹ️  未做（已知缺口，见 PLAN-crabpt.md 附录 D.5）："
          "technical_info 留空 · 日韩拆不出")

    # ★★ 回加做种（2026-10-02 接线）—— 用**蟹黄堡服务端**那份 .torrent，
    #   做种路径沿用**乌托邦那条**。逐条理由见 `readd_to_qb_crabpt` 的 docstring。
    #   ★ 放在 `_crabpt_readback` **之前**：先让做种这件事**有着落**，
    #     再做字段回读（回读失败不影响发布，但做种失败要尽早可见）。
    #   ★ 失败**不算阶段失败**（不 `sys.exit`）—— 见该函数 docstring ⑤。
    if not args.no_readd:
        readd_to_qb_crabpt(qb, client, t["info_hash"], tid)

    _crabpt_readback(client, tid, fields)
    print("\n★ 下一步（人工）：打开详情页逐字段复核，"
          "并**顺便把 `takeedit.php?id=%s` 采下来** ——" % tid)
    print("  那条编辑页是先前采集计划里**唯一**拿不到、"
          "而现在已经可达的东西（先得自己发过一条）。")
    print("★ 它同时是回填 `PLAN-crabpt.md` 附录 C.6 的依据。")
    return


def main():
    args = parse_args()

    # ★★ `--selftest`：跑**本文件的负向自检**（不碰站点、不碰库）。
    #   ★ 为什么给它一条 CLI：自检如果只能靠"记得去 import 它"，
    #     那它跟没有一样（本项目的老毛病：判据存在但没人跑）。
    #   ★ 判据 = **阴性对照也要跑**（见 `selftest_crabpt_isolation` 的 docstring）：
    #     把原 bug 的形状塞回去，自检**必须**报错。没做过阴性对照的自检不算验证过。
    if args.selftest:
        ok1, _ = selftest_crabpt_isolation()
        ok2, _ = selftest_crabpt_transfer_queue()
        ok3, _ = selftest_crabpt_explain_same_source()
        # ★ 三条判据**都要**跑（`and` 而不是 `or`）—— 它们是**独立**的失效模式：
        #   ① 碰了乌托邦的列（"发一条污染四个判据"）
        #   ② 队列取错（"静默多发、不可撤销"）
        #   ③ ★ 队列口径与**解释口径**分叉（"工具告诉你它在队列里、实际不发"
        #      —— 2026-09-28 实测，`--only` 报文与实际筛选互相矛盾）
        #   ★ 用 `or` 的话，一条报红时另一条**根本没跑**，而"没跑"与"通过"
        #     在输出上应当长得不一样（`ERR-META-14`：兜底不许冒充真结果）。
        ok = ok1 and ok2 and ok3
        # ★★ 必须 `sys.exit`，**不能 `return`** —— `main()` 是被
        #   `if __name__ == "__main__": main()` 调的，那个写法的返回值**会被丢掉**
        #   ⇒ 退出码恒为 0 ⇒ **自检报红也没人看得见**（阴性对照实测踩到这一条）。
        #   ★ 这是 `ERR-META-01` 的形状：判据算对了，但**没接到能让人看见的地方**。
        sys.exit(0 if ok else 1)

    # ★ 站点分流（2026-09-25）。★ 只有这一处 if —— 分流点越少，
    #   "某条路漏了某个不变量"的机会越少。两条路各自**完整**：
    #   都自己查闸门、自己预检、自己落库、自己算退出码。
    if args.site == "crabpt":
        return run_crabpt(args)

    config.print_runtime_info()
    db.init_db()

    tasks = db.get_pending_publish()
    if not tasks:
        print("💡 当前没有需要发布的任务。")
        return

    # ★★ `--only`：与队列取**交集**，并**逐条报出**被拦下的（不静默跳过）。
    #
    #   ★ 为什么"报出"是承重的：用户点名 10 条时，若其中 1 条因闸门进不了队列，
    #     `--limit 10` 会**默默多发一条队列里的别的**（用户以为发的是自己点的那 10 条）
    #     —— 那是"发出去的东西和以为发出去的不一样"，本项目最忌讳的形状。
    #   ★ 所以先算**差集**并打出来，再继续。
    if args.only:
        want = tuple(s.strip().lower() for s in args.only.split(",") if s.strip())
        if not want:
            sys.exit("❌ --only 给了但解析出 0 个 hash")
        hit, miss = [], []
        for t in tasks:
            h = (t.get("info_hash") or "").lower()
            (hit if any(h.startswith(w) for w in want) else miss).append(t)
        # 队列里没有 = 被闸门拦下（或 hash 打错）——两件事都要报
        in_queue = {(t.get("info_hash") or "").lower() for t in tasks}
        absent = []
        for w in want:
            if not any(h.startswith(w) for h in in_queue):
                absent.append(w)
        print()
        print("=" * 60)
        print(f"★ --only：点名 {len(want)} 个 ⇒ 命中队列 {len(hit)} 条")
        print("=" * 60)
        for t in hit:
            print(f"   ✅ {(t.get('info_hash') or '')[:12]}  {t.get('formatted_title')}")
        for w in absent:
            # ★ 把"为什么没进来"查出来 —— 这条比"没命中"有用得多
            print(f"   ❌ {w} 不在待发布队列里（被闸门拦下 或 hash 打错）")
        if absent:
            print("\n   ★ 逐条查为什么没进队列（同一份判据，不做手抄）：")
            for w in absent:
                print(f"      {w}: {explain_not_queued(w)}")
        if not hit:
            sys.exit("❌ --only 与队列**零交集** ⇒ 拒绝执行（不会去发队列里的别的）")
        tasks = hit

    print("\n" + "=" * 60)
    print(f"📋 发现 {len(tasks)} 条待发布任务，预检清单：")
    print("=" * 60)

    valid, blocked = [], []
    for i, t in enumerate(tasks, 1):
        reasons = []
        if not (t.get("cover_url") or "").strip():
            reasons.append("缺封面")
        tf = t.get("torrent_file")
        if not tf or not os.path.exists(tf):
            reasons.append("种子文件缺失")

        # ★★ 没有 log 就不发布 —— **最初的规矩**（2026-09-21 用户重申并确认）。
        #   ★ 为什么必须在这里也拦一道：`run.py` 的 `NO_LOG_FILE` 短路只覆盖
        #     **qB 本地抓轨那条路**（它读目录找 `.log`）。皇后转载那条路的日志
        #     来自**页面里的 NFO/LOG 字段**，那是**另一条来源** ——
        #     页面本来就没有（`<div id="divnfo"><pre></pre></div>`，实测三个
        #     Web 来源页面都是空的）⇒ 它**不会**被 `run.py` 判断到。
        #   ★ 而且乌托邦的 `custom_fields[4][3]`（抓轨日志）是**必填**：
        #     发一条空的出去，站点大概率退回，症状正是 `ERR-SITE-01` 那个
        #     「HTTP 200 + 空 body」—— **失败还看不出原因**。
        #   ⇒ 拦在发布前，进邮件提醒，让人自己判断。
        has_log = bool((t.get("log_content") or "").strip())
        if not has_log:
            # 多碟日志走 logs_json，也要认（否则会误拦）
            try:
                lj = json.loads(t.get("logs_json") or "null")
                has_log = bool(lj) and any(
                    (x.get("content") or "").strip()
                    for x in lj if isinstance(x, dict))
            except (ValueError, TypeError):
                pass
        if not has_log:
            reasons.append("无 log（最初的规矩：没有 log 不发布）")

        flag = "✅ 可发布" if not reasons else "❌ " + "、".join(reasons)
        print(f"[{i}] {t.get('formatted_title')}")
        print(f"    副标题: {t.get('small_descr') or '(空)'}")
        print(f"    {flag}")

        (valid if not reasons else blocked).append(t)

    print("=" * 60)
    if blocked:
        print(f"⚠️  {len(blocked)} 条因缺封面或种子文件被拦下。"
              f"缺封面的可以先跑: python backfill_covers.py --apply --all")
    if not valid:
        print("没有符合发布条件的任务，退出。")
        return

    if args.limit:
        valid = valid[:args.limit]

    # ★★ 条间隔（`PUBLISH_MIN_GAP`）：在这里**先算出并报出来**，再走后面任一条路径。
    #   ★ 为什么在 dry-run 之前：dry-run 也要能看见"真实发布会隔多久"，
    #     否则"看效果"看到的节奏是假的（`ERR-META-01`：判据只覆盖一部分）。
    #   ★ N=1 时 `gap` 会算出 0 段间隔 —— **照实报**（"本条不等待"是事实，
    #     不是"没配"）。别把它说成"已启用"。
    gap = publish_min_gap()
    n_gap = max(len(valid) - 1, 0)
    if gap > 0 and n_gap > 0:
        est = n_gap * gap
        print(f"\n⏳ 发布间隔：每条之间至少 {gap}s（{PUBLISH_MIN_GAP_ENV}）"
              f" ⇒ 本次 {len(valid)} 条 = {n_gap} 段 ≈ {est / 60:.1f} 分钟"
              f"（★ 另有每条约几秒的请求耗时）")
        # ★ STAGE_TIMEOUT 是**daemon 侧的**开关（`daemon.py` 的 `env_int`），
        #   `config.py` 里没有它 ⇒ 这里只能从 env 读，且**手工跑时它通常不存在**
        #   （那时根本没有超时，所以下面的告警要说清"仅当在 daemon 里跑"）。
        #   ★ 不把 3600 抄成第二个真值来源：读 env，兜底值只用于**提示**。
        try:
            stage_timeout = int(float(os.getenv("STAGE_TIMEOUT", "3600")))
        except ValueError:
            stage_timeout = 3600
        if est > stage_timeout:
            print(f"⚠️  预计纯等待就 {est / 60:.1f} 分钟，超过 STAGE_TIMEOUT="
                  f"{stage_timeout}s（{stage_timeout / 3600:.1f} 小时）。\n"
                  f"    在 daemon 里跑的话，它会在**提交中途**把本进程杀掉：\n"
                  f"    · ★ **已发出去的种子不受影响**（每条自己就落了库）；\n"
                  f"    · 但**汇总与退出码会丢** ⇒ 会被记成阶段失败 ⇒ 触发**指数退避**"
                  f"（最长 6 小时一轮），进而**拖慢真正的自动发布**。\n"
                  f"    ⇒ 请减小 `--limit`，或调小 `{PUBLISH_MIN_GAP_ENV}`。")
    elif gap > 0:
        print(f"\n⏳ 发布间隔：{gap}s（{PUBLISH_MIN_GAP_ENV}）"
              f"，但本次只有 {len(valid)} 条 ⇒ 无条间等待")
    else:
        print(f"\n⏳ 发布间隔：不限（{PUBLISH_MIN_GAP_ENV} 未配或为 0）")

    if args.dry_run:
        print(f"\n🔍 dry-run：以下 {len(valid)} 条会被发布，简介预览（第一条）：")
        # ★ 与真实发布走**同一个 merge_mapped**，否则预览与实际不一致
        fields = descr.build_form_fields(merge_mapped(valid[0]),
                                         anonymous=args.anonymous)

        print("-" * 60)
        print(fields["descr"][:1200])
        print("-" * 60)
        return

    if not args.yes:
        answer = input(f"\n确认向站点发布这 {len(valid)} 条吗？(y/N): ").strip().lower()
        if answer != "y":
            print("🚫 已取消，未向站点提交任何数据。")
            return

    # ★★★ 第二把钥匙（保险 ③）—— 就在这里，**在建立任何客户端之前**。
    #
    #   ★ 位置是刻意的：必须在 `MusopiaClient()` 之前。钥匙没通过时
    #     **一个请求都不该发出去**（连 `check_login()` 也不发）——
    #     "拒绝"要真的什么都没做，而不是"打了个招呼再拒绝"。
    #
    #   ★ 触发条件 = `--auto` **为真**。⇒
    #     · daemon 的路径：`daemon.py` 拼的命令带 `--auto`（**且必须同时带
    #       `--yes`**，否则会在下面 BEFORE 这里就先撞上 `input()` 的 `EOFError`）
    #       ⇒ **恒被拦到钥匙通过**；
    #     · 人手工敲 `publish.py --yes`：不带 `--auto` ⇒ 不查钥匙。
    #       ★ 这是**刻意**的、也**不算洞**：人在终端前手敲就是"人在回路里"
    #         —— 无头/定时路径才是本保险要覆盖的那一条。
    #     · 把 `--auto` 去掉就能绕过？⇒ 能，但那需要**有权限进容器敲命令**，
    #       而那种情况本来就不是"无人值守"了（同 `--i-have-stopped-the-container`
    #       的定位：它是**纪律的显式声明**，挡的是"忘了/以为"，不是"要作恶"）。
    if args.auto:
        key_ok, key_detail, key_why = guard_auto_key()
        if not key_ok:
            print("=" * 60)
            print(f"🛑 拒绝自动发布：第二把钥匙没通过 —— {key_detail}")
            print(explain_key_reason(key_why))
            print("=" * 60)
            sys.exit(3)

    client = MusopiaClient()
    try:
        uid = client.check_login()
        print(f"✅ Cookie 有效，user_id = {uid}")
    except SiteError as e:
        sys.exit(f"❌ {e}")

    qb = None
    if not args.no_readd:
        try:
            from qbittorrentapi import Client
            qb = Client(host=config.QB_HOST, username=config.QB_USER, password=config.QB_PASS)
            qb.auth_log_in()
        except Exception as e:
            print(f"⚠️  连不上 qBittorrent ({e})，本次不做回加")

    ok = fail = 0
    # ★★ 上一次**上传**的时刻 —— `PUBLISH_MIN_GAP` 按它算"距上一条多久"。
    #   ★ 用 `None` 而不是 0：`time.time()` 是个大数，用 0 会让第一条算出
    #     "距上一条已有 50 年 ⇒ 不用等"——**结论对，但推理是假的**
    #     （`ERR-META-01`：判据要能说清它为什么对）。
    last_upload_ts = None
    for i, task in enumerate(valid, 1):
        # ★★★ 条间隔（突发保护）—— 放在**循环开头**、且 `i > 1` 才等。
        #
        #   ★★ 为什么**不能**放在循环末尾（"发完再等"）：本循环体里有**两处
        #     `continue`**（上传抛异常 / 上传返回空）—— 放在末尾的话，那两处
        #     会**跳过等待**，于是"连续两条失败"就变成**背靠背打站点**，
        #     只受 `musopia.py` 的 1.5s 地板约束。放在开头则**必然**生效：
        #     无论上一条成功还是失败，下一条都先等。
        #   ★ 放在开头还让"睡多少"可测：间距 = 距上次**上传**的时间，
        #     而不是"sleep(120) + 上一条耗时"（那会让旋钮名不副实）。
        if i > 1 and gap > 0 and last_upload_ts is not None:
            wait_publish_gap(gap, i, len(valid), last_upload_ts)

        title = task.get("formatted_title")
        print(f"\n[{i}/{len(valid)}] 🚀 发布: {title}")

        fields = descr.build_form_fields(merge_mapped(task),
                                         anonymous=args.anonymous)
        last_upload_ts = time.time()
        try:
            torrent_id, msg = client.upload_torrent(task["torrent_file"], fields)
        except (SiteError, LoginExpired) as e:
            print(f"    ❌ {e}")
            fail += 1
            continue

        if not torrent_id:
            print(f"    ❌ 发布失败: {msg}")
            fail += 1
            continue

        ok += 1
        print(f"    🎉 发布成功，站点 ID = {torrent_id}")
        db.set_site_id(task["info_hash"], torrent_id, site_status="EXISTS",
                       status="PUBLISHED", reason=f"published_id:{torrent_id}")
        # ★★ 2026-09-25：**同步写新表**（`site_publish`）—— 这是**双写**，不是替换。
        #
        #   ★ 为什么是双写：上面那句 `set_site_id` 是**乌托邦**的既有写法，
        #     而 278 条已发记录 + 15 个读点都依赖它 ⇒ **不动它**（回归面 ≈ 0）。
        #   ★ 为什么必须也写新表：否则"一条种子在哪些站上"这个问题**只在新表里答得全**，
        #     而乌托邦的行不进新表就答不全 —— 那正是本次要修的病
        #     （`ERR-SQL-07`：汇总口径与实际会写的集合必须同口径）。
        #   ★ `cover_url` 传 `task` 那一份（就是 picgo 的链，本就是乌托邦在用的）。
        db.site_publish_set(task["info_hash"], db.SITE_MUSOPIA, torrent_id,
                            cover_url=(task.get("cover_url") or None))
        db.mark_synced(task["info_hash"])

        # 回读确认简介真的落地了。takeupload.php 返回 302 只说明种子进库，
        # 简介字段有没有被正确接收要另外验证。
        #
        # ★★ 2026-09-21 扩到**表单字段**（原先只查简介里的封面 URL）：
        #    为什么必须扩：2026-09-19 那 5 条发布，程序全报成功、
        #    但站点上「艺术家 / 发行年份」整行都不存在 —— 因为那些值在
        #    `custom_fields[4][*]` 里是空的，而**回读校验从不看这些字段**
        #    （`ERR-META-01`：检测方法只覆盖了一部分，于是"全绿"是假的）。
        #    ★ 判据是「**回读到的值 == 我们发出去的值**」，逐字段比，不是看有没有。
        try:
            got, _ = client.fetch_edit_form(torrent_id)
            site_descr = got.get("descr") or ""
            expect_cover = (task.get("cover_url") or "").strip()
            if expect_cover and expect_cover not in site_descr:
                print("    ⚠️  站点简介里没找到封面 URL，建议随后跑 "
                      f"update_metadata.py {torrent_id} --apply 修补")
            else:
                print(f"    🔎 简介已确认落地（{len(site_descr)} 字符）")

            # ---- 表单字段逐项回读（★ 比的是「发出去的 vs 回读到的」）----
            # tags[4][] 是**多值**：两边都排好序再比，否则顺序不同会假报差异。
            sent_tags = sorted(str(x) for x in (fields.get("tags[4][]") or []))
            got_tags = sorted(str(x) for x in (got.get("tags[4][]") or []))
            if sent_tags != got_tags:
                print(f"    ⚠️  标签回读不一致：发出 {sent_tags} / 站点 {got_tags}")

            # 编码/媒介：发出 "0" = 我们**没指定**，那不比对（比对会假报"不一致"）。
            for key, label in (("audiocodec_sel[4]", "音频编码"),
                               ("medium_sel[4]", "媒介")):
                sent = str(fields.get(key) or "")
                if sent and sent != "0":
                    back = str(got.get(key) or "")
                    if sent != back:
                        print(f"    ⚠️  {label}回读不一致：发出 {sent} / 站点 {back}")

            # ★ 已知缺口：custom_fields[4][*] 的 `[4][4]`（唱片厂牌）**皇后页面没有**
            #   ⇒ 留空是**无来源**，不是没做（见 `unmatched` 与 `ERR-META-01`）。
            empties = [k for k in ("custom_fields[4][1]", "custom_fields[4][2]",
                                  "custom_fields[4][3]", "custom_fields[4][4]")
                       if not (fields.get(k) or "").strip()]
            if len(empties) == 4:
                print("    ℹ️  自定义字段（艺术家/发行年份等）仍为空 —— "
                      "见 `python publish.py` 的字段来源说明")
        except SiteError as e:
            print(f"    ⚠️  回读校验失败: {e}")

        readd_to_qb(qb, client, task["info_hash"], torrent_id)

    print("\n" + "=" * 60)
    print(f"🎯 发布完成：成功 {ok} 条，失败 {fail} 条")

    # ★ 说真话的退出码（2026-09-18）。
    #
    # daemon 判一个阶段成败**只看退出码**（`daemon.py:280` 的 `if code == 0`），
    # 而这里原先**一条路都不 sys.exit** —— 于是「成功 0 条，失败 5 条」也以 0 退出，
    # daemon 每轮把 `fails["publish"]` 清零 ⇒ **指数退避分支根本进不去**
    # ⇒ 按 `PUBLISH_INTERVAL` 原样重来一次。
    #
    # 后果不是"保险没生效"，是**反向**的：`daemon.py:14-15` 明写「Cookie 过期这类
    # 问题会让每个阶段都失败，不退避就是每 30 分钟无意义地敲站点一次」，
    # 而实测发种权限是**会抖**的（`ERRATA.md` 的 `ERR-SITE-01`：16:19 全败、
    # 18:50 全成）⇒ 这个「一天 240 次无效 POST」的窗口会**反复**出现。
    #
    # ★ 判据分层，别混成一个数：
    #   - 有失败（重试有意义，多半是权限/站点抖动）→ 非 0，让退避生效；
    #   - 有成功、也有失败 → 同样非 0。**不能因为"成功了几条"就当成功** ——
    #     那正是原先那条静默路径的形状。
    #   - 失败一条都没有 → 0，正常。
    #   - 一条都没试（没待办 / 全被拦 / dry-run / 用户取消）→ 0。这些**不是**失败，
    #     退避它们只会让"没活干"被误记成"连续失败"。
    #
    # ★ 只退出码，**不 sys.exit(msg)** —— 上面那句汇总已经在 stdout 里了，
    #   再往 stderr 打一遍是重复信息（且容器里两条流都会进 docker logs）。
    if fail:
        # ★ 不在这里写死「每 30 分钟」—— 周期由 `PUBLISH_INTERVAL` 定，写死就等于
        #   制造一个必然过时的"事实"（README 首页那条纪律）。只报事实：失败了几条。
        print(f"↩️  以非 0 退出：有 {fail} 条失败，让 daemon 的退避生效"
              f"（不再按 PUBLISH_INTERVAL 原样重来）")
        sys.exit(1)


if __name__ == "__main__":
    main()
