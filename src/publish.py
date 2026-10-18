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


def _opencd_content_host():
    """★ O-6 漏洞 a（2026-10-10）：opencd 原种 A 所在实例的地址 —— **一处口径**。

    ★ 为什么单列一个函数（别在调用点再写 `os.getenv`）：重构后 `3020 ≠ opencd`（= movies1 电影），
      旧代码 `os.getenv("QB_HOST_OPENCD", "http://127.0.0.1:3020")` 的**兜底默认 3020 是错的**：
      一旦 env 没传（或有人不读 env），会去 **movies1** 找音乐原种 ⇒ 必然找不到 ⇒ 静默返 ""。
    ★ 取 config 优先（`config.QB_HOST_OPENCD`，与 daemon 的 `HANDOFF_HOST_3020` 同源），
      再退 env，二者皆无 ⇒ 用**正确**的默认 **3260=music3**（opencd 原种/data 所在；见台账 §11.1）。
    ★ 生产 `.env` 里 `QB_HOST_OPENCD=http://127.0.0.1:3260` ⇒ 本函数返回 3260。
    """
    return (getattr(config, "QB_HOST_OPENCD", "")
            or os.getenv("QB_HOST_OPENCD", "")
            or "http://127.0.0.1:3260")


def _require_handoff_for_readd():
    """★ O-6 漏洞 c（2026-10-10，方案 B）：**回加是否强制要求 handoff 已开**。

    ★ 方案 B 不变量 = 「数据先复制到发种区（handoff），再回加」⇒ handoff 未开时**不应回加**
      （否则落到没有数据的 save_path ⇒ stalledDL/prog=0 ⇒ 站点 0 做种，正是 O-6 病根）。
    ★ 默认 **true**（方案 B 生效）。置 `false` ⇒ 回退老行为（应急开关，别在正常部署用）。
    ★ 复用 `config` 读 env（与其它旋钮同口径，不新造方言）。
    """
    raw = os.getenv("REQUIRE_HANDOFF_FOR_READD",
                    getattr(config, "REQUIRE_HANDOFF_FOR_READD", "true"))
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


def _resolve_opencd_content_path(info_hash, *, return_reason=False):
    """查 **opencd 原种所在实例** 上这条种（按 DB 原种 infohash）的 `content_path`。

    ★ 用途（#92-f1/#133）：handoff 要把成品从数据盘复制到发种区，得先知道
      "专辑目录在哪"。**权威来源 = opencd 原种 A 所在实例上那条仍在做种的种**
      —— 它的 `content_path` 就是 `<该实例>/downloads/<专辑>`。
    ★ 实例地址见 `_opencd_content_host()`（★ O-6 漏洞 a：不再硬编码 3020）。
    ★ 只在 `ENABLE_HANDOFF` 开启时才被调用。
    ★ **绝不 raise**（回加主流程不能被它带崩）。

    ★★ O-6 漏洞 a（2026-10-10）：**取不到不再"静默返空"** ——
      旧调用点（`src and album` 为假）只会 print 一行、然后 `record_missing_data`。
      但**原因**（连不上 / 原种不在 / content_path 为空）**没带进账本**，事后 grep 只知道
      "漏了"，不知道"为什么漏"。⇒ 新增 `return_reason=True` 时返回 `(path, reason)`：
        · 成功 ⇒ `(path, "")`；
        · 失败 ⇒ `("", 具体原因)` —— **调用方据此把原因入账**（本函数自己**不**写账本，
          保持"纯查询"职责；入账在 `readd_to_*` 的既有 3 个跳过点做，别新增第 4 处——
          run.py 自检 `⑨j` 对账本调用点有精确计数断言）。
    """
    def _out(path, reason):
        return (path, reason) if return_reason else path
    host = _opencd_content_host()
    try:
        import types
        import qb_client
        cfg = types.SimpleNamespace(
            QB_HOST=host,
            QB_USER=getattr(config, "QB_USER", "admin"),
            QB_PASS=getattr(config, "QB_PASS", ""),
            # ★★★ 2026-10-07（#129）：opencd 实例用**它自己的** key（`OPENCD_QB_API_KEY`），
            #   绝不用 `config.QB_API_KEY`（那是发种实例的）—— 否则回 403（实测）。
            QB_API_KEY=getattr(config, "OPENCD_QB_API_KEY", ""),
        )
        qb_src = qb_client.build(cfg)
        got = qb_src.torrents_info(torrent_hashes=info_hash)
        if not got:
            msg = f"opencd 实例({host}) 上找不到原种 {str(info_hash)[:12]}…"
            print(f"    ⚠️  handoff：{msg} ⇒ 无法定位成品目录")
            return _out("", msg)
        cp = str(getattr(got[0], "content_path", "") or "")
        if not cp:
            msg = f"opencd 实例({host}) 上原种 content_path 为空"
            print(f"    ⚠️  handoff：{msg}")
            return _out("", msg)
        return _out(cp, "")
    except Exception as e:                                        # noqa: BLE001
        msg = f"连 opencd 实例({host}) 查 content_path 失败: {type(e).__name__}: {e}"
        print(f"    ⚠️  handoff：{msg}")
        return _out("", msg)


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
        # ★★★ #92-f1/#133（2026-10-07）：**回加 3040 的 save_path 语义订正**。
        #   老写法：`orig = qb.torrents_info(info_hash)` + `save_path = orig[0].save_path`
        #     —— 那是拿 **3040 上已有该 infohash** 的种取路径；但 3040 上挂的会是
        #     **站点 .torrent**（infohash ≠ DB 原种），⇒ `orig` 查空 ⇒ 跳过回加。
        #   新写法：数据由 handoff 复制到 **3040 的 `/downloads/<专辑>`**（见下方 hook），
        #     所以 `save_path` **固定给父目录** `config.PUBLISH_SAVE_ROOT`（= `/downloads`）。
        #   ★ 父目录语义实测（NAS-92 §2）：给专辑目录 ⇒ qB 去找 `<专辑>/<专辑>/…` ⇒ 进度 0。
        #   ★★ 仅在 `ENABLE_HANDOFF` 开启时用新语义（复制由本项目代码做）；
        #     关闭时**回退**老行为（保 `orig[0].save_path`）⇒ 不改上线前的既有路径。
        content = client.download_torrent(torrent_id)
        if not content:
            print("    ❌ 从站点下载种子文件失败，跳过回加")
            return

        _handoff_on = str(getattr(config, "ENABLE_HANDOFF", "false")).strip().lower() in (
            "1", "true", "yes", "y", "on")
        # ★ #136：把成品源路径带到延迟回加条目里，供 daemon 回加前核对发种区就位。
        _h_src, _h_album = "", ""
        if _handoff_on:
            # ★ 复制成品 →3010 发种区 + 入队（用户要求：复制/删除由本项目代码自动化）。
            #   源 = opencd 原种所在实例上这条种的 content_path（★ O-6 漏洞 a：读 env，不再硬编码 3020）。
            save_path = config.PUBLISH_SAVE_ROOT
            try:
                import handoff
                # ★ O-6 漏洞 a：`return_reason=True` ⇒ 取不到时把**具体原因**带出来入账。
                src, _src_why = _resolve_opencd_content_path(info_hash, return_reason=True)
                album = os.path.basename(str(src).rstrip("/\\")) if src else ""
                _h_src, _h_album = src, album        # ★ #136：带给 delay 队列核对用
                if src and album:
                    _ok, _why = handoff.begin_handoff_for_task(
                        src, album, info_hash, task=None,
                        source_category=config.PUBLISH_CATEGORY,
                        target_category=config.PUBLISH_CATEGORY,
                        dry_run=(config.HANDOFF_DRY_RUN == "true"))
                    print(f"    🔀 handoff 复制: {_why}")
                    # ★★★ #134 定论：**数据没就位 ⇒ 绝不回加 3010**。
                    #   原先这里**丢弃** `_ok` ⇒ 复制失败/半截也照推 ⇒ qB 去
                    #   `/downloads/<专辑>` 找不到文件 ⇒ progress=0 变下载（实测踩到）。
                    #   现在据返回值否决：种子已上站（不可逆、改不了），但**能否做种**
                    #   取决于数据在位；宁可不回加（下一轮可补救），也不推一条假下载。
                    if not _ok:
                        print(f"    ❌ handoff 未就位 ⇒ **跳过回加 3010**（避免变成下载）："
                              f"{_why}")
                        # ★★★ B2：**入账**（否则这条"已上站却不会做种"**静默消失**，见下）。
                        handoff.record_missing_data(info_hash, album, "musopia", _why)
                        return
                else:
                    print("    ⚠️  handoff：拿不到 opencd 原种 content_path，"
                          "跳过复制（本条大概率不会做种）")
                    # ★ 拿不到源路径 ⇒ 无法复制 ⇒ 数据必然不在发种区 ⇒ 同样拒绝回加。
                    print("    ❌ 无法定位源成品 ⇒ **跳过回加 3010**（避免变成下载）")
                    # ★ O-6 漏洞 a：把**具体原因**（连不上/原种不在/cp 为空）入账，别只写死话术。
                    handoff.record_missing_data(info_hash, album, "musopia",
                                                _src_why or "拿不到 opencd content_path")
                    return
            except Exception as _e:                              # noqa: BLE001
                # ★ 复制/核对**抛异常** ⇒ 数据状态未知 ⇒ 保守拒绝回加（宁可漏做种，不可推假下载）。
                print(f"    ❌ handoff 复制/核对异常 ⇒ **跳过回加 3040**（避免变成下载）: "
                      f"{type(_e).__name__}: {_e}")
                handoff.record_missing_data(info_hash, album, "musopia",
                                            f"{type(_e).__name__}: {_e}")
                return
        else:
            # ★★★ O-6 漏洞 c（2026-10-10，方案 B）：**handoff 关 ⇒ 拒绝回加**。
            #   方案 B 的不变量 = 「数据先复制到发种区，再回加」。handoff 关时数据**不会**
            #   被复制 ⇒ 回加必然落到一个没有数据的 save_path（重构后 3010 的 /downloads
            #   是 SSD 发种区，只在 handoff 复制过才有该专辑）⇒ 变 stalledDL/prog=0。
            #   ⇒ 宁可**不回加**（漏做种，入账，可事后补），也**绝不**推一条假下载。
            #   ★ 仅在方案 B 生效（`REQUIRE_HANDOFF_FOR_READD=true`，默认 true）时拒绝；
            #     置 false ⇒ 回退老行为（取 orig 的 save_path）——留一个应急开关。
            if _require_handoff_for_readd():
                print("    ❌ [O-6/方案B] ENABLE_HANDOFF 未开 ⇒ 无法保证发种区有数据 ⇒ "
                      "**跳过回加**（避免变成下载；如确需关闭 handoff 回加，设 "
                      "REQUIRE_HANDOFF_FOR_READD=false）")
                try:
                    import handoff as _ho_c
                    _ho_c.record_missing_data(info_hash, "", "musopia",
                                              "ENABLE_HANDOFF 未开（方案B 拒绝回加）")
                except Exception:                                 # noqa: BLE001
                    pass
                return
            orig = qb.torrents_info(torrent_hashes=info_hash)
            if not orig:
                print("    ⚠️  qB 里找不到原始种子，无法确定做种路径，跳过回加")
                return
            save_path = orig[0].save_path

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
                src_path=_h_src, album=_h_album,     # ★ #136：回加前核对发种区用
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
                # ★★★ #134 定论：判据从 `state != "missingFiles"` **收紧**。
                #   原先只挡 `missingFiles`，而"变下载"那一族（`stalledDL`/`downloading`/
                #   `metaDL`/`stoppedDL`/`pausedDL`/`allocating`/`checkingDL`）**都不是**
                #   `missingFiles` ⇒ 复核**把一条正在下载的假种当成功**（实测：`☆ac8.13f`
                #   `progress=0 state=stalledDL`，复核却打 ✅）。
                #   ⇒ 现在明确：**progress<1 或 state 属下载族 ⇒ 报红**（"不会做种"）。
                _dl_states = {"stalledDL", "downloading", "metaDL", "stoppedDL",
                              "pausedDL", "allocating", "checkingDL", "forcedDL",
                              "queuedDL", "missingFiles", "error", "unknown"}
                prog = float(getattr(t, "progress", 0) or 0)
                ok = (t.state not in _dl_states) and prog >= 1.0
                mark = "✅" if ok else "★★"
                print(f"    {mark} 回加复核：state={t.state} progress={prog:.3f} "
                      f"content_path={t.content_path!r}")
                if not ok:
                    print("        ★★ 未在做种（下载族/进度<1）⇒ **这条种子不会做种**"
                          "（已上站，不可逆）。见 readd_to_qb 的 docstring。")
        except Exception as e:                              # noqa: BLE001
            print(f"    ⚠️  回加复核失败（不影响发布本身）: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"    ❌ 回加 qB 出错: {e}")


def connect_qb():
    """建一个已登录的 qBittorrent 连接；连不上返回 `None`（并**说清原因**）。

    ★ 从 `main()` 里那段**原样**抽出来（乌托邦与蟹黄堡两条路共用同一份）。
      ★ 不静默吞异常：连不上要打印，否则"没做回加"与"回加成功"在日志里
        长得一样（`readd_to_qb` 的纪律）。

    ★★★ 2026-10-06（#49）：连之前先过 **qB readiness 闸门**（`qb_client.wait_qb_ready`，
      与 `daemon` 那条延迟回加**同一个**实现）。
      · **为什么**：`SEED_DELAY_MIN=0`（默认）时发布成功后**立即**回加，而开机后 qB
        要几十秒才起来 ⇒ `build()` 撞 `SSLError port=3020`（`ERR-QB-08` 形状：**连接尝试
        本身**在故障期就是噪音/推手）。#7 只给**延迟回加**那条路加了闸门，这条**没加** ⇒ 本补。
      · **为什么闸门放这、不放 `readd_to_qb()`**：`qb` 是**传进来**的，`readd_to_qb` 只拿到一个
        可能为 `None` 的连接 —— 它**无法**"重连"。而 `qb_client.build()` 是**唯一构造点**
        （`ERR-QB-08`），本函数正是它的**唯一入口** ⇒ 在这里等就绪，`readd_to_qb` 的 docstring
        与 `qb=None` 分支**一字不动**（那里"本次没有可用的 qB 连接"仍成立，只是**更少**发生了）。
      · **失败语义不变**：等不到就绪 ⇒ 超时返回 False ⇒ **跳过本次回加**、连 `build()` 都
        不试（少一次注定失败的连接）⇒ 返回 `None` ⇒ `readd_to_qb` 打印"本次没有可用的 qB 连接"。
        **绝不 raise**（回加失败是本地问题，不能让"已发上站"的这条看着像失败 —— 见 `readd_to_qb`）。
      · **旋钮**：复用 `QB_READY_TIMEOUT`/`QB_READY_INTERVAL`（`config` 里由 `daemon.py`
        一套 `env_int` 导入；本模块多用两个旋钮、**不新造方言**）。
      · **`--no-readd` 不受影响**：那样根本不会调本函数。
    """
    try:
        # ★★ 唯一构造点 `qb_client.build`（`ERR-QB-08`）：优先 API Key，回退账号密码。
        #   ★ 失败契约不变：`build()` 连不上会抛 ⇒ 仍由本 try 收、打印、返回 None。
        import qb_client
        if not qb_client.wait_qb_ready(
                config.QB_HOST,
                timeout=getattr(config, "QB_READY_TIMEOUT", 120),
                interval=getattr(config, "QB_READY_INTERVAL", 5),
                log=lambda m: print(f"    {m}")):
            print("⚠️  回加跳过：qB 在就绪等待窗口内没起来（不改任何状态；下轮/下次再试）")
            return None
        qb = qb_client.build(config)
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
        # ② 种子字节：**蟹黄堡服务端**那份（带蟹黄堡 announce）
        content = client.download_torrent(torrent_id)
        if not content:
            print("    ❌ 从蟹黄堡下载种子文件失败，跳过回加")
            return

        # ① 做种路径（★★ #92-f1/#133 订正，同 `readd_to_qb`）：
        #   开启 handoff 时，数据已复制到 **3040 的 `/downloads/<专辑>`**，
        #   `save_path` 固定给**父目录** `config.PUBLISH_SAVE_ROOT`（= `/downloads`）。
        #   ★ 蟹黄堡与乌托邦**共用同一份专辑数据**（同一 infohash 的原种），
        #     所以复制这一步若乌托邦路已做过 ⇒ 这里幂等跳过（copy_to_publish_area 幂等）。
        #   ★ 关闭 handoff ⇒ 回退老行为（取 3040 上 orig 的 save_path）。
        _handoff_on = str(getattr(config, "ENABLE_HANDOFF", "false")).strip().lower() in (
            "1", "true", "yes", "y", "on")
        # ★ #136：同乌托邦路 —— 把成品源路径带进延迟回加条目，供前核对发种区。
        _h_src, _h_album = "", ""
        if _handoff_on:
            save_path = config.PUBLISH_SAVE_ROOT
            try:
                import handoff
                # ★ O-6 漏洞 a：`return_reason=True` ⇒ 取不到时把**具体原因**带出来入账。
                src, _src_why = _resolve_opencd_content_path(info_hash, return_reason=True)
                album = os.path.basename(str(src).rstrip("/\\")) if src else ""
                _h_src, _h_album = src, album        # ★ #136
                if src and album:
                    _ok, _why = handoff.begin_handoff_for_task(
                        src, album, info_hash, task=None,
                        source_category=config.CRABPT_PUBLISH_CATEGORY,
                        target_category=config.CRABPT_PUBLISH_CATEGORY,
                        dry_run=(config.HANDOFF_DRY_RUN == "true"))
                    print(f"    🔀 handoff 复制: {_why}")
                    # ★★★ #134 定论（同乌托邦路）：数据没就位 ⇒ **绝不回加 3010**。
                    #   实测病根：蟹黄堡这条 `☆ac8.13f` 在 SSD 发种区**根本没目录**，
                    #   却被推上 3010 变 `stalledDL`（10 leechers）—— 就是因为这里丢弃了 `_ok`。
                    if not _ok:
                        print(f"    ❌ handoff 未就位 ⇒ **跳过回加 3010**（避免变成下载）："
                              f"{_why}")
                        # ★★★ B2：入账（同乌托邦路，见 `readd_to_qb` 内注释）。
                        handoff.record_missing_data(info_hash, album, "crabpt", _why)
                        return
                else:
                    print("    ⚠️  handoff：拿不到 opencd 原种 content_path，跳过复制")
                    print("    ❌ 无法定位源成品 ⇒ **跳过回加 3010**（避免变成下载）")
                    # ★ O-6 漏洞 a：把**具体原因**入账，别只写死话术。
                    handoff.record_missing_data(info_hash, album, "crabpt",
                                                _src_why or "拿不到 opencd content_path")
                    return
            except Exception as _e:                              # noqa: BLE001
                print(f"    ❌ handoff 复制/核对异常 ⇒ **跳过回加 3040**（避免变成下载）: "
                      f"{type(_e).__name__}: {_e}")
                handoff.record_missing_data(info_hash, album, "crabpt",
                                            f"{type(_e).__name__}: {_e}")
                return
        else:
            # ★★★ O-6 漏洞 c（2026-10-10，方案 B）：同乌托邦路 —— handoff 关 ⇒ 拒绝回加。
            if _require_handoff_for_readd():
                print("    ❌ [O-6/方案B] ENABLE_HANDOFF 未开 ⇒ 无法保证发种区有数据 ⇒ "
                      "**跳过回加**（避免变成下载）")
                try:
                    import handoff as _ho_c
                    _ho_c.record_missing_data(info_hash, "", "crabpt",
                                              "ENABLE_HANDOFF 未开（方案B 拒绝回加）")
                except Exception:                                 # noqa: BLE001
                    pass
                return
            # ① 做种路径：**乌托邦那条**（= 皇后原种 infohash）的 save_path
            orig = qb.torrents_info(torrent_hashes=info_hash)
            if not orig:
                print("    ⚠️  qB 里找不到**乌托邦**那条原始种子，无法确定做种路径，跳过回加")
                return
            save_path = orig[0].save_path

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
                config.SEED_DELAY_MIN, note=f"蟹黄堡 {info_hash[:12]}…",
                src_path=_h_src, album=_h_album,     # ★ #136：回加前核对发种区用
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
                # ★★★ #134 定论（同乌托邦路）：判据从 `state != "missingFiles"` **收紧**，
                #   下载族/进度<1 一律报红（原先把 `stalledDL progress=0` 当成功）。
                _dl_states = {"stalledDL", "downloading", "metaDL", "stoppedDL",
                              "pausedDL", "allocating", "checkingDL", "forcedDL",
                              "queuedDL", "missingFiles", "error", "unknown"}
                prog = float(getattr(t, "progress", 0) or 0)
                ok = (t.state not in _dl_states) and prog >= 1.0
                mark = "✅" if ok else "★★"
                print(f"    {mark} 回加复核：state={t.state} progress={prog:.3f} "
                      f"content_path={t.content_path!r}")
                if not ok:
                    print("        ★★ 未在做种（下载族/进度<1）⇒ **这条蟹黄堡种子不会做种**"
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


def _local_info_name(task):
    """取本地 `.torrent` 的**权威** `info.name`（= 查重判据的左侧）。

    ★★ 为什么用 `.torrent` 的 `info.name`、**不**用 `formatted_title`：
       2026-10-11 实测（3/3 已知重复）—— 本地 `.torrent` 的 `info.name`
       == 站侧 `.torrent` 的 `info.name`（精确相等）；而**标题不同**
       （本地 `Forever Now 2026 - FLAC 整轨 - OpenCD` vs 站侧
       `Switchfoot - Forever Now 2026 - FLAC 分軌 - Orpheus`）
       ⇒ 比标题要么假阳（搜错）要么假阴（漏查）。`info.name` 由**种子内容**决定，
         与发布者无关 ⇒ 才是"这条是不是同一专辑"的权威身份。
    ★ 复用 `site_verify.extract_info_name`（它只认 `metainfo["info"]["name"]`，
      **绝不**退回 `torf.Torrent.name`——单文件种子上那是文件名，会给出假结论）。
    ★ 读不出（文件缺失 / 解析失败）⇒ 返回 `""`。**不抛**：查重侧对空名字
      一律按"比不了 ⇒ 照旧提交"处理（不更坏，见 `find_existing_by_name`）。
    """
    import site_verify
    path = (task or {}).get("torrent_file") or ""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:  # noqa: BLE001
        print(f"    ℹ️  查重：本地种子文件读不出（{e}）⇒ 跳过查重、照旧提交")
        return ""
    name, why = site_verify.extract_info_name(data)
    if not name:
        print(f"    ℹ️  查重：本地种子 info.name 取不出（{why}）⇒ 跳过查重、照旧提交")
        return ""
    return name


def _version_consistency_gate(task):
    """★ 发种前**版本一致性闸门**（2026-10-14，`ERR-PUBLISH-20` 修复③）。

    返回 `(ok: bool, reason: str)`。`ok=False` ⇒ 调用方**拒发**（标 FAILED 出队）。

    ═══════════════════════════════════════════════════════════════════════════
    为什么必须有（`#136` 族第 3 次复发的当场，21662）
    ═══════════════════════════════════════════════════════════════════════════
    21662 那次：本地目录**实际是 12 个分轨 flac**，却因为里面躺着一个**指向不存在
    WAV 的 .cue**，旧判据把它当"整轨"⇒ 站点标题写成整轨版本 ⇒ 站点那条**永远
    校验不到 100%**（B 版 `prog=0.4893` 卡死，只能人工去站点删种）。

    ⇒ 闸门查**两件事**（都必须一致，任一不符即拒发）：
      ① **版本标签**：DB 记的 `track_type` vs **现在**从本地目录重算的
         `assets.cue_track_type()`。二者不一致 = 库里的版本是**旧判据/别的时候**
         算出来的，与**盘上现在的实况**对不上 ⇒ 拒发（这正是本条要拦的形状）。
         ★ DB 侧为空（老行/皇后路未写）⇒ **不拦**（无从比，显式报出，不猜）。
      ② **文件集**：本地 `.torrent` 的文件集 vs **本地内容目录**的文件集。
         不一致 = 要发的种子**装的东西 ≠ 盘上的东西**（错版本/漏文件）⇒ 拒发。
         ★ 任一侧读不出 ⇒ **不拦**（比不了 ⇒ 显式报出，不猜 —— 同查重纪律）。
    ★★ 为什么不比 `info.name`：分轨/整轨**共用同一个目录名** ⇒ 只比名字比不出
      挂错版本（21662 就是这么漏过去的）。要比的是**文件集**。
    ★ 纯本地（读盘 + 读本地 `.torrent`），**零网站请求** —— 故放在 `upload_torrent`
      之前、查重之后（查重会发请求，本闸门不会）。
    """
    import site_verify as _sv
    import assets as _assets

    content_path = (task or {}).get("content_path") or ""
    torrent_file = (task or {}).get("torrent_file") or ""

    # ① 版本标签：DB vs 盘上实况
    db_tt = (task.get("track_type") or "").strip()
    if content_path and os.path.isdir(content_path):
        live_tt = _assets.cue_track_type(content_path)
        if db_tt and live_tt and db_tt != live_tt:
            return False, (f"版本标签不一致：库={db_tt!r} ≠ 盘上实况={live_tt!r}"
                           f"（目录 {content_path!r}）")
        if not db_tt:
            print(f"    ℹ️  版本闸门：库内 track_type 为空（老行？）⇒ 只按实况 {live_tt!r} 记录、不拦")
    else:
        print(f"    ℹ️  版本闸门：内容目录不存在（{content_path!r}）⇒ 跳过版本比对、不拦")

    # ② 文件集：本地 .torrent vs 本地内容目录
    if not torrent_file or not os.path.isfile(torrent_file):
        print(f"    ℹ️  版本闸门：本地种子文件不存在（{torrent_file!r}）⇒ 跳过文件集比对、不拦")
        return True, ""
    try:
        with open(torrent_file, "rb") as fh:
            t_bytes = fh.read()
    except OSError as e:                            # noqa: BLE001
        print(f"    ℹ️  版本闸门：读本地种子失败（{e}）⇒ 跳过文件集比对、不拦")
        return True, ""
    t_files, why = _sv.extract_files(t_bytes)
    if t_files is None:
        print(f"    ℹ️  版本闸门：本地种子文件集读不出（{why}）⇒ 跳过比对、不拦")
        return True, ""
    l_files = _sv.files_of_dir(content_path)
    verdict, note = _sv.compare_file_sets(l_files, t_files)
    if verdict == _sv.MISMATCH:
        return False, f"文件集不一致 ⇒ 拒发（{note}）"
    print(f"    ✔ 版本闸门：{note}")
    return True, ""


def _dedup_lookup(client, *, search_name, local_name, site_label):
    """发布**前**查一次目标站点；返回命中的 tid 或 `None`。**出错向上抛**。

    ★★ 这是 #13/#11 的**唯一**查重入口（两站共用，`ERR-META-03`：一处生效）——
      把"搜不了 ⇒ 停轮非 0"与"搜到了不重复 ⇒ 照旧发"两条**相反**的处置，
      在一个地方写死，免得两个调用点各写一份 `if` 链后慢慢漂。

    ★ 判据链全在 `MusopiaClient.find_existing_by_name` 里（`info.name` 精确比对）；
      本函数只负责：① 开关判断；② 取本地权威名；③ 命中/未命中/出错三态分流；
        ④ 把结果打成一行人话日志。
    ★ 返回 `None` = **要么开关关、要么搜到了不重复**（两者对调用方都是"照旧提交"）；
      副作用上，命中的 tid 会**非 None** 返回，调用方据此改走"关联已有页"分支。
    ★★ 异常**不吞**：`find_existing_by_name` 抛的 `LoginExpired`/`SiteError`
      （含"查重搜索 HTTP≠200"）**原样向上冒** ⇒ 调用方按 `site_down` 停轮 + 非 0。
      这正是用户拍板的"查重搜索失败 ⇒ 停轮非 0"（绝不静默降级成"没重复"）。
    """
    if not config.DEDUP_BEFORE_PUBLISH:
        return None
    if not search_name:
        print("    ℹ️  查重：无搜索串（标题为空）⇒ 跳过查重、照旧提交")
        return None
    # ★（#23）打印**子查询计划**：多召回是"隐形的"，不打出来生产日志里看不出来
    #   （`ERR-PUBLISH-17` 警告的"静默特性"）。计划纯本地、零网络。
    try:
        import musopia as _mus
        _plan = _mus.SEARCH_QUERY_PLAN(
            local_name, search_name=search_name,
            max_queries=config.DEDUP_MAX_SEARCH_QUERIES)
        print(f"    🔎 查重计划：{len(_plan)} 条子查询（本地名={local_name!r}）"
              f" ⇒ {_plan}")
    except Exception as _e:                         # noqa: BLE001 —— 只影响日志，不影响查重
        print(f"    ℹ️  查重计划打印失败（不影响查重）：{_e}")
    # ★★（`S4` / `ERR-PUBLISH-25`）诊断累加器 —— 让"查重返回 None"带上**证据**。
    #   ★ 为什么这是必须的：`21811` 那次站点**真的**挂着同名页，`find_existing_by_name`
    #     却返回 `None`，调用方只印一句"未发现同版本" ⇒ 无法分辨"站点上没有"与
    #     "站点上有、但我没搜到/没验到/预算被吃光" ⇒ 我改去猜字段，猜了三轮全错。
    #   ★ 无证据时的合理行为是**去猜**，而猜在不可逆操作前是最贵的错。
    _diag = {}
    try:
        hit = client.find_existing_by_name(search_name, local_name=local_name, diag=_diag)
    except TypeError:
        # ★ 兼容：站侧客户端若还是旧签名（没有 diag 参数）⇒ 退回旧调用，
        #   绝不让"多一个诊断参数"把整个发布路径打挂（`ERR-META-01` 的反面）。
        _diag = {}
        hit = client.find_existing_by_name(search_name, local_name=local_name)
    if hit:
        print(f"    🔍 查重命中：{site_label} 已有同版本 ⇒ id={hit}"
              f"（本地名={local_name!r}）⇒ 关联已有页、**不再重复提交**")
    else:
        print(f"    🔍 查重：{site_label} 未发现同版本 ⇒ 照旧提交")
        if _diag:
            print(f"       ↳ 证据：查询 {len(_diag.get('queries') or [])} 条 / "
                  f"候选 {_diag.get('candidates', 0)} 个 / "
                  f"验证 {_diag.get('verified', 0)} 个")
            if _diag.get("near_miss"):
                _nm = "、".join(f"id={t}({n!r})" for t, n in _diag["near_miss"][:3])
                print(f"       ⚠️  ★有名字极像但不是同版本的候选：{_nm}"
                      f" ⇒ 提交时若被拒，优先怀疑**同名冲突**（见下一条闸门）")
            if _diag.get("why"):
                print(f"       ↳ {_diag['why']}")
    return hit


# ==================== 同名冲突：提交**前**的确定性查询（`S4`）====================
#
# ★★ 为什么必须有这一道（`ERR-PUBLISH-25`，`21811` 实测）：
#   NexusPHP 有**两种**「重复」，响应**完全不同**，而本方只认第一种：
#     · 同一 infohash ⇒ HTTP 302 `details.php?id=…&existed=1`（**明确**）
#     · 同 `info.name`、不同种 ⇒ **HTTP 200「上传失败！」**，正文零线索
#   ⇒ 第二种在日志里长得和"必填项没填"**一模一样**。我当时据此连续怀疑了
#     `medium_sel` / `custom_fields` / 种子损坏三件事，**全错**（`ERR-PUBLISH-25` 记了全过程）。
#   ⇒ 真正该做的是**提交前就问**：「站点上有没有**名字撞车**的条目？」
#     这是**只读**的一问，代价是一次搜索；而问错的代价是**不可逆**的发布。
#
# ★★ 判据是「同名」而**不是**「近亲」（`site_verify.is_near_miss`）：
#   近亲只进日志、**绝不拒发**。拒发判据必须与站点的拒发判据**同构**
#   （站点拒的正是 `info.name` 逐字相同），否则我们会拒掉站点本来会收的条目 ——
#   少发是**静默**的，比多发更危险（多发至少站点会拒）。
#
# ★ 这一道是 #13 查重的**补充**，不是替代：查重回答"站点上有没有**同一版本**"，
#   本闸门回答"有没有**同名的别的东西**"。两者都对 ⇒ 提交大概率不会被那句
#   「上传失败」闷死；两者之一命中 ⇒ 各有明确的处置（关联 / 拒发交人）。


def _same_name_precheck(client, *, search_name, local_name, site_label):
    """提交**前**问一句：目标站点上有没有 `info.name` **逐字相同**的条目？

    返回 `[(tid, hosted_name), …]`（**可能为空**）；**网络/解析异常向上抛**
    （与查重同一条纪律：搜不了 ⇒ 停轮非 0，绝不把"搜不了"当"没有同名"）。

    ★ 为什么复用 `client.find_existing_by_name(..., diag=…)` 而不是自己再搜一遍：
      ① 召回（`SEARCH_QUERY_PLAN` 的多子查询、精准优先、命中即停）已经在那一处，
         另写一份就是 `ERR-META-03`（判据漂移）的标准产地；
      ② 它**本来就**逐条下载站侧 `.torrent` 取了 `info.name` —— 同名条目它一定见过，
         只是**撞上就等于"同版本"**，于是走 MATCH 分支返回了。
      ⇒ 我们只需要从 `diag` 里把"见过的名字"再取一次：`diag["same_name"]`
         （由 `musopia.search_and_compare` 逐条填；**相等就 return 了，所以它恒为空**）。
      ★★ 换句话说：本闸门**在正常路径上不会命中** —— 它命中的唯一情形是
         "站点上有同名条目，但那条的名字没被读到 / 没进候选"。
         这恰恰是 `21811` 当时的形态（本条查重返回 `None`，站点上却真有同名页）。
      ⇒ 因此本闸门**必须有单元级证据**（自检里用假 client 造"同名但没进 MATCH 分支"的场面），
        不能靠"生产上没见它报过"来证明它没用（那是空真）。
    ★★ 空 `local_name` ⇒ 返回 `[]`（无从比，**不拒发**：拒发是更强的主张，不能凭空给）。
    """
    diag = {}
    try:
        hit = client.find_existing_by_name(search_name, local_name=local_name, diag=diag)
    except TypeError:
        # ★ 兼容：站侧客户端若还是旧签名（没有 `diag` 参数）⇒ 退回旧调用。
        #   绝不让"多一个诊断参数"把整条发布路径打挂（`ERR-META-01` 的反面：
        #   为了更好的检测手段，反而把被执行的主路径弄没了）。
        #   ★ 但此时**拿不到同名证据** ⇒ 本闸门退化为"放行"，且必须说出来：
        #     静默放行会让人以为"闸门查过了、没问题"。
        print("    ℹ️  同名预检：站侧客户端不支持 diag ⇒ 本次**查不了**（按放行处理）")
        diag = {}
        hit = client.find_existing_by_name(search_name, local_name=local_name)
    if hit:
        # 连"同版本"都命中 ⇒ 调用方本该走"关联已有页"分支（这里只是兜底）。
        return [(str(hit), diag.get("local_name") or local_name)]
    out = list(diag.get("same_name") or [])
    if out:
        print(f"    ⚠️  同名预检：{site_label} 上已有 info.name 逐字相同的条目 "
              f"{'、'.join(f'id={t}({n!r})' for t, n in out[:3])}")
    return out


# ==================== 被拒后的**分诊**（`S4` / `ERR-PUBLISH-25`）====================
#
# ★★ 为什么这不是"日志美化"：`ERR-PUBLISH-25` 的全部代价都花在**分诊错误**上。
#   站点对"同名冲突"和"字段没填"回的是**同一句**「上传失败！」，
#   我当时把它读成"字段没填"，于是改 `medium_sel` → 拆 `custom_fields` → 重新制种，
#   三轮全错，每次都靠"再来一次真发种"验证 —— 而重跑 = 一次**不可逆**的写。
#   ⇒ 分诊的作用是：**把"下一次该查什么"从猜测变成结论**，并且写进 `reason` 落库，
#     让它在日志滚动之后仍然可查。
#
# ★ 判据必须**能被证伪**：这里只做"按站点文案归类 + 指出最可能的成因"，
#   **不**声称"就是它" —— 真正的裁决仍是 `_same_name_precheck` 的**事先**查询
#   （它若命中，本条根本走不到这里）。所以下面每条都同时说"下一步怎么验证"。

# ★ 站点文案 → 分诊类别。**精确匹配优先**（同一句话在不同站有不同含义时，
#   按"先到先得"会随字典顺序漂 —— 本项目不容这种不确定，故用有序元组）。
_REJECT_HINTS = (
    ("同名冲突", ("上传失败",),       # NexusPHP 对"同 info.name 不同种"的通用文案
     "站点上有 info.name 逐字相同的条目。验证：用另一个**已知能发**的种子"
     "打同一套字段 ⇒ 若 302 则字段无罪（ERR-PUBLISH-25 的决定性对照）；"
     "或在站点搜该 info.name。"),
    ("必填项缺失", ("请选择", "未选择", "必须选择", "不能为空", "请填写"),
     "表单某格为空（常见：medium_sel/audiocodec_sel/tags 的**分区下标**错了 —— "
     "`[4]` 音乐区 vs `[7]` 视频区）。验证：`python tools/preview_form.py` 看渲染出的值。"),
    ("权限/配额", ("权限", "不允许", "配额", "超过", "无权", "没有权限"),
     "账号侧限制。验证：看 `check_login` 的 user_id 与站点个人页的今日发种计数。"),
    ("种子被拒", ("无效", "不是合法的", "种子文件", "torrent", "无法识别"),
     "上传的 .torrent 本身被拒。验证：本地 `torch`/`torf` 读一遍 + 逐片自校验。"),
)


def _diag_kind(msg):
    """把站点消息归类成一个**短标签**（落库用）。未知 ⇒ `"未分类"`。

    ★ 只认**机器可读前缀先于文案**：`SITE_DOWN:`/`UNKNOWN:` 已在上层分流，
      走到这里的一定是"这条被拒"，所以这里只分**为什么**被拒。
    """
    s = msg or ""
    for kind, needles, _advice in _REJECT_HINTS:
        if any(n in s for n in needles):
            return kind
    return "未分类"


def _diagnose_upload_reject(msg, *, local_name=""):
    """给"这条被拒"写一句**下一步该查什么**。返回人话字符串（落进 `reason`）。

    ★ 入参 `local_name` 只用于把"我们要发的 info.name"写进结论 ——
      同名冲突的排查**必须有这个值**，否则到了站点上还得再翻一次库。

    ★★ 未知文案**不猜**：返回"文案无已知特征"并**列出**可做的三步对照，
      而不是硬套一个类别。硬套就是 `ERR-META-01` 那个形状
      （"检测手段只覆盖一部分，于是'结论'看起来总是有的"）。
    """
    s = (msg or "").strip()
    kind = _diag_kind(s)
    for k, _needles, advice in _REJECT_HINTS:
        if k == kind:
            if k == "同名冲突":
                return f"{advice} 本方要发的 info.name = {local_name!r}"
            return advice
    return ("站点文案无已知特征（不是本表覆盖的任何一类）⇒ **不要**顺着文案猜字段。"
            "按 ERR-PUBLISH-25 的顺序做三步对照："
            "① 换一个**已知发过**的种子打同一套字段（302 ⇒ 字段无罪）；"
            "② 只改 `info.name` 再发一次（302 ⇒ 是**同名冲突**）；"
            "③ 用盘上数据重新制种（infohash 不变 ⇒ 不是种子坏了）。"
            f" 本方要发的 info.name = {local_name!r}")




class CoverItemError(Exception):
    """**单条**种子的封面不可用（如本地无源图 / 图超图床大小上限 / 站点按 item 拒收）。

    ★★ 与"设施级失败"（`LSKY_TOKEN` 没配 / 图床不可达 / 401）**必须分开**（ERR-META-03：
    两件不同的事别用同一个判据）：
      · **设施级** ⇒ 每条都过不去 ⇒ **停 + 非 0**（退避），修好设施再继续（保持原行为）。
      · **本条级**（本异常）⇒ **只这一条**过不去，且**重跑多少次都一样**（如一张永久
        超过图床大小的图）⇒ 必须**跳过这条、继续下一条**，否则**一条坏封面会把整条
        蟹黄堡转种队列永久卡死**（2026-10-09 实测：`1e39f05940b6.jpg` 15.9 MB
        被 Lsky 拒「图片大小超出限制」，蟹黄堡三天 0 条发布成功）。
    ★ 抛出本异常 = 调用方**跳过本条**（不标 FAILED：种子本身没坏，只是这一站缺图；
      可先 `backfill_covers_opencd.py` 换一张再重试）。
    """


def _crabpt_cover(task, *, dry_run=False):
    """确保这条种子在**蟹黄堡**上有一份**属于蟹黄堡的**封面 URL。返回 `(url, 说明)`。

    ★★ 失败分级（2026-10-09 修）：**本条级**失败抛 `CoverItemError`（调用方跳过本条、
    继续下一条）；**设施级**失败仍返回 `("", 说明)`（调用方停整轮 + 非 0）。见 `CoverItemError`。

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
        # ★ 本条级：这条种子**根本没有封面源** ⇒ 跳过本条（不是设施坏）。
        raise CoverItemError("库里连 cover_url 都没有 ⇒ 无源可传")
    try:
        import cover_fetch
        local = cover_fetch._cached_file(src)
    except Exception as e:                                  # noqa: BLE001
        return "", f"★ 查本地封面缓存出错: {type(e).__name__}: {e}"
    if not local:
        # ★ 本条级：本地没有这张图（且不联网重下）⇒ 跳过本条。
        raise CoverItemError(
            f"本地封面缓存里没有这张图（key={src[:60]}…）⇒ 没有可上传的源")
    if dry_run:
        # ★ dry-run **不真上传**（那是外部副作用而且是不可撤销的写）。
        #   但也**不能说"封面就绪"** —— 那是个假判据。如实报"会传这一张"。
        return src, (f"dry-run：**不会**真上传。验证通过后会传这一张上去：{local}")

    # ③ 真传
    import assets
    url = assets.upload_to_lsky(local)
    if not url:
        # ★★ 失败分级（2026-10-09）：单条级（这张图本身传不上去，如超大小上限）⇒
        #   抛 `CoverItemError`，调用方**跳过本条**；设施级（token/配额/网络）⇒ 返空，
        #   调用方**停整轮**。判据集中在 `assets.lsky_error_is_item_level`（一处口径）。
        if assets.lsky_error_is_item_level():
            raise CoverItemError(
                f"Lsky 拒收这张图（单条级，重试无效）：{assets.LAST_LSKY_ERROR}"
                f"；本地图 {local}")
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


def selftest_readd_names_defined(verbose=True):
    """★★★ 断言两条**回加**路（`readd_to_qb` / `readd_to_qb_crabpt`）里，
    **函数局部**变量不会在"首次赋值之前"被求值（= `UnboundLocalError` 的形状）。

    ★★★ 为什么必须有（2026-10-04 坐实的 `ERR-PUBLISH-02`）：
      `readd_to_qb_crabpt` 的延迟回加分支写了
        `note=f"蟹黄堡 {t.get('info_hash','')[:12]}…"`
      而 **`t` 在该函数里"赋值"发生在更靠后**（`t = got[0]`，在 `enqueue` 之后的
      复核块里）⇒ `SEED_DELAY_MIN>0` 时**每条蟹黄堡发布都 `UnboundLocalError`**，
      `enqueue` 从未执行 ⇒ 19 条发布成功却 **0 条做种**（站点记 hit-and-run）。
      ⇒ 这类 bug **抄自"看起来像"的另一条路**（乌托邦版写的是参数 `info_hash`），
        只差一个名字 ⇒ **靠人眼对读抓不住**，正需要一条机器判据。

    ★★ 判据怎么定（这是本函数两次返工的结论，别退回第一版）：
      · 第一版按"逐 stmt 维护已绑定集合"扫 ⇒ **假阳性一片**：`orig`/`got`/`t`
        都是**合法**的（它们的赋值在 `if`/`try`/`with` 块里，而使用在**更靠后**的
        顶层语句里）。逐 stmt 的顺序模型**不懂嵌套块**，把合法的报成违规。
        ⇒ 而**假阳性比假阴性更坏**（`_code_only` 的 docstring 已写明：
          它让人把一条有效判据删掉）。
      · 终版改为**用 Python 自己的作用域分析**（`symtable`）定"谁是这个函数的局部变量"
        （`is_local() ∧ is_assigned() ∧ ¬is_parameter()`）—— 这一点由编译器保证，不靠我推。
        然后只查一件事：**该名字有没有在"行号小于它首次赋值行"的地方被读**
        （AST 里 `Name(Load)` 的 lineno < 首次 `Name(Store)` 的 lineno）。
        ★ 这是**行号顺序**这个简单判据，但它对"局部变量"这个**已由 symtable 锁定**的
          小集合是够用的：这些名字要么是参数（已绑）、要么是本函数内赋值（顺序可比）。
        ★ 已知局限（如实写下）：若某名字"只在**循环体内**晚赋值、循环体外早读"，
          行号顺序仍会对（早读的 lineno 更小）—— 与本条要防的形状一致，不误伤。
          真正难判的是"同一行、条件分支"的边角（如 `a = a or x`），
          那属于自引用而非"未绑定"，且本项目无此写法 ⇒ 不在本条范围。

    ★ 阴性对照（必须做）：把原 bug 的形状（把正确片段里的 `info_hash[:12]`
      换成 `t.get('info_hash','')[:12]`）塞回**源码字符串**，判据**必须**报红；
      否则这条自检等于没验过（`selftest_crabpt_isolation` 的 docstring 立的规矩）。

    ★ 只读：只读**本文件源码**，不碰站点、不碰库。
    """
    # ★ 控制台编码守卫：本函数会打 ✅/❌（非 GBK 字符），Windows 控制台默认 GBK
    #   ⇒ 不加这行会在**第一行输出**就 UnicodeEncodeError（同 `opencd_rate` 自检）。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass

    import symtable

    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = _ast.parse(src)
    targets = ("readd_to_qb", "readd_to_qb_crabpt")

    def _find_fn(t, name):
        """在 symtable 树里按名找函数作用域（含嵌套）。"""
        for c in t.get_children():
            if c.get_name() == name:
                return c
            r = _find_fn(c, name)
            if r is not None:
                return r
        return None

    def _scan(parsed_src, parsed_tree, label):
        """返回该份源码里"局部变量被在首赋值前读"的明细（空 = 通过）。"""
        try:
            st = symtable.symtable(parsed_src, "<publish.py>", "exec")
        except SyntaxError as e:
            return [f"✗ {label}: 源码语法错误，无法分析：{e}"]
        out = []
        for nm in targets:
            f = _find_fn(st, nm)
            fnode = next((n for n in _ast.walk(parsed_tree)
                          if isinstance(n, _ast.FunctionDef) and n.name == nm), None)
            if f is None or fnode is None:
                out.append(f"✗ ★ 找不到 `{nm}`（改名了？）本条自检失效")
                continue
            # ★ 只取"函数自己的局部"：symtable 保证这一点，不靠我推。
            locals_assigned = {
                s.get_name() for s in f.get_symbols()
                if s.is_local() and s.is_assigned() and not s.is_parameter()
            }
            # 首 STORE 行 + 所有 LOAD 行
            first_store, loads = {}, {}
            for n in _ast.walk(fnode):
                if isinstance(n, _ast.Name):
                    if isinstance(n.ctx, _ast.Store):
                        first_store[n.id] = min(first_store.get(n.id, n.lineno), n.lineno)
                    elif isinstance(n.ctx, _ast.Load):
                        loads.setdefault(n.id, []).append(n.lineno)
            for name in sorted(locals_assigned):
                fs = first_store.get(name)
                if fs is None:
                    continue
                early = sorted({l for l in loads.get(name, []) if l < fs})
                if early:
                    out.append(
                        f"✗ ★★ {label}: `{name}` 在**首次赋值之前**被读"
                        f"（读于行 {early}，首次赋值在行 {fs}）"
                        f" ⇒ 运行时 `UnboundLocalError`"
                        f"（正是 `ERR-PUBLISH-02` 的形状）")
        return out

    detail = _scan(src, tree, "publish.py")

    # ---------- 阴性对照：把原 bug 形状塞回源码，判据必须报红 ----------
    # ★ 正确片段 = `{info_hash[:12]}`；坏片段 = `{t.get('info_hash','')[:12]}`
    #   ★ 用**拼接**构造 bad 片段，免得嵌套引号把源码字面量搞脏。
    _good_frag = "{" + "info_hash" + "[:12]}"
    _bad_frag = "{" + "t.get(" + repr("info_hash") + ", " + repr("") + ")[:12]}"
    bad_src = src.replace(_good_frag, _bad_frag)
    if bad_src == src:
        detail.append("✗ 阴性对照的**锚点**没找到（那行已被改动？）⇒ "
                      "本条自检失效（锚点漂了 = 没测）")
    else:
        try:
            neg = _scan(bad_src, _ast.parse(bad_src), "NEG")
        except SyntaxError as e:
            neg = [f"✗ 阴性对照源码语法错误：{e}"]
        if not any("`t`" in d for d in neg):
            detail.append("✗ ★ 阴性对照失败：把原 bug（`t.get(...)`）塞回源码后，"
                          "判据**没**报到 `t` ⇒ 本条自检是空的")

    if verbose:
        mark = "✅" if not detail else "❌"
        print(f"{mark} 回加路名字绑定自检：`readd_to_qb` / `readd_to_qb_crabpt` "
              f"里没有「首赋值前读」的局部变量（含阴性对照）")
        for d in detail:
            print(f"    {d}")
    return (not detail), detail


# ── ★★ 负向自检：发布循环"失败分级处置"确实存在（#21）──────────────────────
#
# ★★★ 为什么需要它：`#21` 的整个价值 = 「坏种子 (REJECT) 跳过并标 FAILED、站点崩
#   (SITE_DOWN) 立即停」这一条**分支结构**。而这个结构的**失效方式恰恰是静默的**：
#   谁哪天图省事把它"简化"回"任何失败都 `continue`"，代码照样跑、自检照样绿
#   （除了本条），而**坏种子又开始堵死整条队列** —— 用户点名要修的那个病**原样复发**。
#   ⇒ 判据必须**钉住这个结构本身**，而不是钉住某次运行的结果。
#
# ★ 判据取自**本文件自己的源码**（AST，非手抄清单 —— 那必然漂移，`ERR-META-03`）。
#   `main()` 里那段循环要同时满足：
#     ① 出现 `SITE_DOWN:` 的判定（否则站点崩会被当成"这条坏"继续连打）
#     ② `SITE_DOWN` 支用 `break`（停），不是 `continue`
#     ③ `REJECT:` 支带 `db.save_task(..., status="FAILED", ...)`（否则坏种子不出队，队列不动）
#     ④ 退出码区分 `site_down`（否则站点崩不退避）
#   ★ 阴性对照：把"REJECT 支那段的 `db.save_task(status='FAILED')`"抹掉 ⇒ 必须报红。
def selftest_publish_failure_classification(verbose=True):
    """断言发布循环按 `SITE_DOWN`/`REJECT` **分级处置**（#21）。返回 `(是否通过, 明细)`。

    ★ 判据基于**源码文本**（不是 `_code_only`）：因为这里的契约**正是字符串前缀**
      `"SITE_DOWN:"` / `"REJECT:"` —— 它们是 `musopia.upload_torrent` 与 `publish.main`
      之间的**机器可读接口**。`_code_only` 会把这些前缀清掉（它专为"查名字"设计），
      用在**这里**反而是错的（会把要查的东西删掉）—— 所以本自检**不**走 `_code_only`。
      ★ 但 `save_task` 这类**动作**用源码文本查同样成立（它是名字，不受影响）。
    """
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = _ast.parse(src)
    fn = next((n for n in _ast.walk(tree)
               if isinstance(n, _ast.FunctionDef) and n.name == "main"), None)
    if fn is None:
        return False, ["★ 找不到 main —— 函数被改名了？本条自检失效"]
    # ★ 取 main 的**源码切片**（按行号），保留字符串 —— 见 docstring 的理由。
    lines = src.splitlines()
    start = fn.lineno - 1
    end = max((getattr(n, "lineno", start) for n in _ast.walk(fn)), default=start)
    body = "\n".join(lines[start:end])

    def _scan(text):
        out = []
        if "SITE_DOWN:" not in text:
            out.append("★★ main() 里没有 `SITE_DOWN:` 判定 ⇒ 站点崩会被当成"
                       "「这条坏」继续连打整条队列（#21 复发）")
        if "REJECT:" not in text:
            out.append("★★ main() 里没有 `REJECT:` 分级 ⇒ 无法区分「这条坏」与「站点崩」")
        # ★ 退出码分层：`site_down` 这个名字必须出现（它驱动"非 0 + 退避"）
        if "site_down" not in text:
            out.append("★★ main() 里没有 `site_down` ⇒ 站点崩不再触发退避"
                       "（Cookie 过期时会每周期白敲站点）")
        # ★ 坏种子必须被标 FAILED 出队 ⇒ `db.save_task` 这个**动作**必须存在。
        if "save_task" not in text:
            out.append("★★ main() 里没有 `db.save_task` ⇒ 被拒的种子**不出队**，"
                       "下一轮还在，队列推不动（FAILED 出队是本改动的关键机制）")
        return out

    detail = _scan(body)

    # ---------- 阴性对照：抹掉坏种子出队那段，判据必须报红 ----------
    # ★ 锚点 = main 源码里 `save_task` 那些行；**全部**删掉后，**只重扫 main 的切片**
    #   （★ 不能重扫整份文件 —— 那样会把**本自检函数自己**源码里的 `save_task` 也扫进来，
    #    于是阴性对照永远报不出红、自检形同虚设。这是本自检第一版的真 bug。）
    if "save_task" not in body:
        detail.append("✗ 阴性对照的**锚点**没找到（main 里 `save_task` 那行已变？）⇒ "
                      "本条自检失效（锚点漂了 = 没测）")
    else:
        bad_body = "\n".join(ln for ln in lines[start:end] if "save_task" not in ln)
        neg = _scan(bad_body)
        if not any("save_task" in d for d in neg):
            detail.append("✗ ★ 阴性对照失败：删掉 `save_task` 那几行后判据**没**报红 "
                          "⇒ 本条自检是空的")

    if verbose:
        mark = "✅" if not detail else "❌"
        print(f"{mark} 发布失败分级自检：main() 按 SITE_DOWN/REJECT 分级、坏种子标 FAILED 出队"
              f"（含阴性对照）")
        for d in detail:
            print(f"    {d}")
    return (not detail), detail


def selftest_crabpt_failure_classification(verbose=True):
    """断言 `run_crabpt` 按 `SITE_DOWN`/`REJECT`/`UNKNOWN` **分级处置**（#23）。
    返回 `(是否通过, 明细)`。

    ★ 与 `selftest_publish_failure_classification`（乌托邦那条，#21）**同款镜像**，
      但对象是 `run_crabpt` 而非 `main`，且**多一个 `UNKNOWN:` 前缀**：
      蟹黄堡的"HTTP 200 + 无 id + 无报错"**可能是真发成功了**（发布不可逆）
      ⇒ 那类**不**能当 REJECT 出队（会漏发/发重），必须单列 `UNKNOWN:` 停 + 非 0。

    ★ 判据基于**源码文本**（不是 `_code_only`）—— 理由同乌托邦那条：这里的契约
      **正是字符串前缀**，`_code_only` 会把它清掉（它专为"查名字"设计）。
    """
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = _ast.parse(src)
    fn = next((n for n in _ast.walk(tree)
               if isinstance(n, _ast.FunctionDef) and n.name == "run_crabpt"), None)
    if fn is None:
        return False, ["★ 找不到 run_crabpt —— 函数被改名了？本条自检失效"]
    lines = src.splitlines()
    start = fn.lineno - 1
    end = max((getattr(n, "lineno", start) for n in _ast.walk(fn)), default=start)
    body = "\n".join(lines[start:end])

    def _scan(text):
        out = []
        for pref in ("SITE_DOWN:", "REJECT:", "UNKNOWN:"):
            if pref not in text:
                out.append(f"★★ run_crabpt() 里没有 `{pref}` 判定 ⇒ "
                           f"蟹黄堡失败不再按性质分级（#23 复发）")
        # ★ 退出码分层：`site_down` 必须出现（它驱动"非 0 + 退避"）。
        if "site_down" not in text:
            out.append("★★ run_crabpt() 里没有 `site_down` ⇒ 站点侧/无法判定不再触发退避"
                       "（Cookie 过期或'可能已发成功'时会继续连打整条队列）")
        # ★ 坏种子必须被标 FAILED 出队 ⇒ `db.save_task` 这个**动作**必须存在。
        if "save_task" not in text:
            out.append("★★ run_crabpt() 里没有 `db.save_task` ⇒ 被明确拒的种子**不出队**，"
                       "下一轮还在（`ORDER BY updated_at` 让它恒在队首）⇒ 转种队列推不动")
        return out

    detail = _scan(body)

    # ---------- 阴性对照：抹掉坏种子出队那段，判据必须报红 ----------
    # ★ 锚点 = run_crabpt 源码里 `save_task` 那些行；全部删掉后**只重扫 run_crabpt 的切片**
    #   （★ 不能重扫整份文件 —— 会把本自检函数自己源码里的 `save_task` 也扫进来，
    #    于是阴性对照永远报不出红、自检形同虚设）。
    if "save_task" not in body:
        detail.append("✗ 阴性对照的**锚点**没找到（run_crabpt 里 `save_task` 那行已变？）⇒ "
                      "本条自检失效（锚点漂了 = 没测）")
    else:
        bad_body = "\n".join(ln for ln in lines[start:end] if "save_task" not in ln)
        neg = _scan(bad_body)
        if not any("save_task" in d for d in neg):
            detail.append("✗ ★ 阴性对照失败：删掉 `save_task` 那几行后判据**没**报红 "
                          "⇒ 本条自检是空的")

    if verbose:
        mark = "✅" if not detail else "❌"
        print(f"{mark} 蟹黄堡失败分级自检：run_crabpt() 按 SITE_DOWN/REJECT/UNKNOWN 分级、"
              f"坏种子标 FAILED 出队（含阴性对照）")
        for d in detail:
            print(f"    {d}")
    return (not detail), detail


def selftest_crabpt_limit_enumerates(verbose=True):
    """断言 `run_crabpt` 的一轮**不会因为队首封面坏就白跑**（2026-10-10）。

    ★★ 这条防的是什么（蟹黄堡 2026-10-07~10-10 实测三天零出种的**根因**）：
      旧代码 `valid = valid[:limit]`（先截候选）**然后**才进循环逐条试封面。
      `CRABPT_MAX_PER_CYCLE=1` ⇒ `valid` 只剩 1 条 ⇒ 队首那条封面坏（15.6MB 被
      Lsky 拒）⇒ `CoverItemError` 的 `continue` **无处可去** ⇒ 一轮白跑；该条又**没标
      FAILED** ⇒ 仍是队首（`ORDER BY updated_at`）⇒ **永久卡死**。
    ⇒ 契约：`run_crabpt` 里**不得**在提交循环之前对 `valid` 做 `[:limit]` / `[:1]` 预截；
      改为循环内按"**成功计数**"（`published_this_round >= max_publish`）截断。

    ★ 判据 = **源码文本**（同 `selftest_crabpt_failure_classification` 的理由：
      这里查的是"某段赋值语句在不在"，是**结构契约**；`_code_only` 会把它清掉）。
    ★ 阴性对照：把 `valid = valid[:1]`（旧 bug 形状）塞回 `run_crabpt` ⇒ 判据**必须**报红。
      没做过阴性对照的自检不算验证过（`ERR-META-01`：失效的检测比不检测更危险）。
    """
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = _ast.parse(src)
    fn = next((n for n in _ast.walk(tree)
               if isinstance(n, _ast.FunctionDef) and n.name == "run_crabpt"), None)
    if fn is None:
        return False, ["★ 找不到 run_crabpt —— 函数被改名了？本条自检失效"]
    lines = src.splitlines()
    start = fn.lineno - 1
    end = max((getattr(n, "lineno", start) for n in _ast.walk(fn)), default=start)
    body = "\n".join(lines[start:end])

    def _scan(text):
        out = []
        # ★ 契约 ①：不得出现"把 valid 按切片预截"的写法。
        #   只看**对 `valid` 赋值且右侧是切片**的行（`valid = valid[:...]`），
        #   不看只读的 `valid[:12]`（打印队列）或 `valid[0]`（dry-run 预览）。
        bad_slice = []
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("valid = valid[") and ":" in s:
                bad_slice.append(s)
        if bad_slice:
            out.append(f"★★ run_crabpt() 里仍有 `valid = valid[...]` 预截候选 "
                       f"⇒ 队首封面坏时一轮白跑、队列永久卡死（#134 复发）：{bad_slice}")
        # ★ 契约 ②：必须有"成功计数"驱动的高限。
        if "published_this_round" not in text:
            out.append("★★ run_crabpt() 里没有 `published_this_round` ⇒ `--limit` 不是按"
                       "**成功条数**截断 ⇒ 坏封面会占掉名额、堵住后面的种子")
        # ★ 契约 ③：坏封面必须**跳过并继续**（不是 break、不是标 FAILED）。
        if "CoverItemError" not in text or "continue" not in text:
            out.append("★★ run_crabpt() 里没有 `CoverItemError` + `continue` ⇒ "
                       "坏封面不再'跳过并继续下一条'")
        return out

    detail = _scan(body)

    # ---------- 阴性对照：把旧 bug 形状 `valid = valid[:1]` 塞回去，必须报红 ----------
    #   ★ 注：塞到一个安全位置即可（这里拼在 body 末尾），只要 `_scan` 能扫到它。
    neg = _scan(body + "\n        valid = valid[:1]\n")
    if not any("预截候选" in d for d in neg):
        detail.append("✗ ★ 阴性对照失败：把 `valid = valid[:1]` 塞回去后判据**没**报红 "
                      "⇒ 本条自检是空的")

    if verbose:
        mark = "✅" if not detail else "❌"
        print(f"{mark} 蟹黄堡 --limit 自检：run_crabpt() 按**成功计数**截断、"
              f"坏封面跳过即继续（无 valid 预截）（含阴性对照）")
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

    # ★★★ 2026-10-10：`--limit` 的语义从"**预截候选**"改成"**成功计数截断**"。
    #
    #   病根（2026-10-07~10-10 实测，蟹黄堡三天零出种）：旧代码是
    #     `valid = valid[:limit]`（**先**截到 limit 条）**然后**才进提交循环逐条试封面。
    #   `CRABPT_MAX_PER_CYCLE=1` ⇒ `valid` 只有 1 条 ⇒ 队首那条封面坏（15.6MB 被
    #   Lsky 拒）抛 `CoverItemError` ⇒ `continue` **无处可去**（valid 就 1 条）
    #   ⇒ 一轮白跑；而该条**没标 FAILED** ⇒ 仍是队首 ⇒ **永久卡死**，后面 34 条不动。
    #
    #   ⇒ 正确行为（用户 2026-10-10 拍板「先发别的种子」）：`--limit N` 表达的是
    #     "**这一轮至多发成功 N 条**"，而**不是**"只试前 N 条候选"。循环必须能
    #     **跳过坏封面、继续试下一条**，直到发够 N 条或候选耗尽。
    #   ⇒ 所以这里**不再** `valid = valid[:limit]`；改为把"至多发 N 条"交给下面的
    #     提交循环用 `published_this_round` 计数实现（见那段的注释）。
    #
    #   ★ 上界语义**没变**：一轮仍是**最多**发 N 条（不可逆操作的上限还得有）。
    #     变的只是"这 N 条名额**被谁占用**" —— 旧代码让一个坏封面**占掉**唯一名额，
    #     新代码把它**让给下一个能发的**。
    #
    #   ★ 默认仍保守（给了 --limit 才发 N 条，没给仍只发 1 条）：
    #     不可逆操作的默认值必须保守，放开要有人显式写一个数（同 ERR-ENV-02 家族）。
    #     只是这个"1"现在**必须真的发出去一条**，而不是"试一条、失败就收工"。
    if args.limit:
        max_publish = args.limit
    else:
        max_publish = 1
        if len(valid) > 1:
            print(f"\n★★ 蟹黄堡路径未给 --limit ⇒ 一次只发 1 条（本次命中 {len(valid)} 条）。")
            print(f"   ★ 理由：发布不可逆。要一次多发请在命令行**显式**给 "
                  f"`--limit N`（daemon 用的是 `CRABPT_MAX_PER_CYCLE`）。")

    # ★★ 枚举软上限：坏封面会被**跳过并继续下一条**，若队列里坏封面极多，
    #   一轮可能横扫整队、把 Lsky 打爆（每试一条 = 一次上传请求）。
    #   ⇒ 给"本轮最多**试**多少条候选"设一个远大于 limit 的护栏；撞上就如实
    #     打印"因候选耗尽停在此处"（ERR-META-14：兜底不许冒充真结果）。
    max_scan = max(max_publish * 20, 200)

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
    #
    #   ★★ 2026-10-10：dry-run 只预览**队首一条**（`valid[0]`）。但真发布现在会
    #     **跳过坏封面、继续下一条** ⇒ 若队首恰是坏封面，预览会停在它上面而**看不到**
    #     真正会被发的那条。⇒ 下面把这一点**说清**（ERR-META-14：别让预览冒充真结果）。
    if args.dry_run:
        t = valid[0]
        print("\n" + "=" * 60)
        print(f"🔍 dry-run：{t.get('formatted_title')}")
        print("=" * 60)
        print(f"   ★ 注意：真发布会**跳过坏封面、继续下一条**（至多发成功 {max_publish} 条）；"
              f"本预览只演示**队首这一条**。")
        # ★★ 封面也要**走一遍**（`dry_run=True` ⇒ 只查不传）。
        #   ★ 为什么不能跳过这一步：不跑它就看不出一件最要紧的事 ——
        #     **本地到底有没有那张图**。而"没有图"正是真发布时会**拒绝发**的原因。
        #     ⇒ 跳过 = 预览说"一切就绪"、真跑时却在最后一步退出（假绿色）。
        #   ★ 但 `dry_run=True` 分支**不真上传**（那是不可撤销的外部写）。
        cover, cover_why = _crabpt_cover(t, dry_run=True)
        print(f"封面 ← {cover_why}")
        if not cover:
            print("   ★★ 注意：队首这条**真发布会在这里跳过**（不改发别的）—— 见上。")
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
    # ★ 2026-10-10：确认文案随 `--limit` 新语义更新 —— 说的是"至多发成功几条"，
    #   而不是"valid 有几条"（valid 现在**不预截**，`len(valid)` 会比实际名额大）。
    if not args.yes:
        answer = input(f"\n确认向**蟹黄堡**发布吗？（至多发成功 {max_publish} 条；"
                       f"本轮候选 {len(valid)} 条，坏封面会跳过）(y/N): ").strip().lower()
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

    # ---- 提交（#23：**逐条循环** + 失败分级）--------------------------------
    #
    # ★★ 为什么是循环（#23 的**核心**，之前这里是 `t = valid[0]` 只发 1 条）：
    #   被站点**明确拒**的种子标 `FAILED`（`CRABPT_QUEUE_WHERE` 不含它 ⇒ 出队）+
    #   **继续发下一条**；站点侧问题 / 无法判定 ⇒ 立即停 + 非 0（退避）。
    #
    # ★★★ 2026-10-10：`--limit` 语义订正为"**成功计数**"（见上面 `max_publish` 那段）。
    #   旧代码 `valid = valid[:limit]`（**先截候选**）在这里**恰好**与 `continue` 打架：
    #     `CRABPT_MAX_PER_CYCLE=1` ⇒ `valid` 只 1 条 ⇒ 队首封面坏 ⇒ `continue` 无处可去
    #     ⇒ 一轮白跑 + 队首永不前进 ⇒ **永久卡死**（蟹黄堡 2026-10-07~10-10 三天零出种）。
    #   ⇒ 现在 `valid` **不预截**；循环逐条往下试，**坏封面跳过、继续下一条**，直到
    #     **成功发出 `max_publish` 条** 或候选耗尽。这正是用户要的「先发别的种子」。
    #
    #   ★ 遍历的是**已过预检**的 `valid`（都有 cover_url / 种子文件 / descr 非空壳），
    #     所以"预检拦下的（如缺 cover_url 的队首）"**不会**占用名额 —— 它压根没进 valid。
    ok = rejected = skipped = 0
    scanned = 0                 # 本轮**试过**的候选数（含被跳过的坏封面）
    published_this_round = 0    # 本轮**成功发出**的条数（= 真正消耗 `--limit` 名额的）
    site_down = False
    dup_existing = 0            # ★ #13：查重命中 ⇒ 关联而非重复提交的条数
    for t in valid:
        # ★★ 枚举软上限：坏封面会被跳过并继续；若坏封面极多，一轮可能横扫整队、
        #   把 Lsky 打爆。撞上护栏就**如实**说清"因候选耗尽停在此处"（不许冒充"发完了"）。
        if scanned >= max_scan:
            print(f"\n⚠️  本轮已试 {scanned} 条、成功 {published_this_round} 条，"
                  f"达枚举软上限（{max_scan}）⇒ 停在此处（队列里坏封面可能很多，"
                  f"建议先 `python backfill_covers_opencd.py` 批量换封面）。")
            break
        scanned += 1
        # ★★ 封面必须在**渲染字段之前**解决 —— `descr` 是提交内容的一部分，
        #   而封面就写在 `descr` 里（`[img]…[/img]`）。顺序反了就等于没换。
        #   ★★ 失败分级（2026-10-09 修）——
        #     · `CoverItemError`（**本条**封面不可用，如超图床大小上限 / 本地无源图）
        #       ⇒ **跳过本条、继续下一条**（重试无效；否则一条坏封面永久卡死整队）。
        #     · 其余返空 = **设施级**（token/配额/网络）⇒ 停整轮 + 非 0（保持原行为）。
        try:
            crabpt_cover, cover_why = _crabpt_cover(t)
        except CoverItemError as _ce:
            print(f"   · 封面 ← ❌ 本条封面不可用（跳过本条，继续下一条）：{_ce}")
            print(f"     ★ 修法：`python backfill_covers_opencd.py` 给这条换一张封面后重试。")
            skipped += 1
            continue
        print(f"   · 封面 ← {cover_why}")
        if not crabpt_cover:
            print("🛑 蟹黄堡封面没准备好（设施级）⇒ 停止本轮（不标 FAILED —— 这是本地设施问题）。\n"
                  "   ★ 修法：检查 NAS `.env` 里的 `LSKY_TOKEN`（见 ERR-ENV-02）。")
            site_down = True
            break
        # ★ 只覆盖**这一份 task 拷贝**的 `cover_url`，`build_fields` 会用它渲染 `descr`。
        #   ★ 不改 `build_fields` 的签名 —— "用哪个封面"是**调用方的决定**，
        #     而它只管渲染（两站共用同一个渲染函数喂不同的 URL，见 PLAN 7b）。
        #   ★ 也**不**回写 `tasks.cover_url`（那是乌托邦那一份，别顶掉它）。
        t = dict(t)
        t["cover_url"] = crabpt_cover

        # ★★★ 发布前跨站查重（#13 / #11 核心，2026-10-11 加）—— 蟹黄堡这一路。
        #   ★ 为什么放在**封面之后**：查重命中 ⇒ 不提交 ⇒ 这一步的封面白算了，
        #     但 `_crabpt_cover()` 还有"写 site_publish.cover_url"的副作用是**想要**的
        #     （关联已有页时也把封面链记下）。★ 且蟹黄堡的 `UNKNOWN:` 停轮机制比乌托邦
        #     更脆（重复提交回 HTTP200 无 id）⇒ 这里查重的价值更高。
        #   ★★ 三态处置与乌托邦**逐字相同**（用户要求两站都做）：命中 ⇒ 只写
        #     `site_publish_set(...SITE_CRABPT...)` + `save_task(PUBLISHED)`，
        #     **绝不碰** `site_status`/`site_torrent_id`（那是乌托邦的两列，见下）；
        #     未命中 ⇒ 照旧提交；出错 ⇒ 停轮非 0（`site_down`）。
        if config.DEDUP_BEFORE_PUBLISH:
            _local_nm = _local_info_name(t)
            try:
                _hit = _dedup_lookup(client,
                                     search_name=t.get("formatted_title"),
                                     local_name=_local_nm,
                                     site_label="蟹黄堡")
            except LoginExpired as e:
                print(f"    🛑 查重时登录失效，停止本轮（不标 FAILED）: {e}")
                site_down = True
                break
            except SiteError as e:
                print(f"    🛑 查重失败，停止本轮（不标 FAILED —— 站点侧问题）: {e}")
                site_down = True
                break
            if _hit:
                # ★★★ 只写**蟹黄堡那一行**，绝不碰 `site_status`/`site_torrent_id`
                #   （与下面成功路径同一纪律 —— 那两列是乌托邦的，写它们会污染
                #    发布队列 / 已发布集合 / scan 跳过 / 巡检计数四个判据）。
                db.site_publish_set(t["info_hash"], db.SITE_CRABPT, _hit,
                                    status="EXISTS",
                                    cover_url=crabpt_cover or None)
                db.save_task(info_hash=t["info_hash"], status="PUBLISHED",
                             reason=f"crabpt_dup_existing:{_hit}")
                db.mark_synced(t["info_hash"])
                dup_existing += 1
                print("    ✅ 已关联到蟹黄堡已有页（未重复提交）。")
                continue

            # ★★ 同名闸门（`S4` / `ERR-PUBLISH-25`）—— 与乌托邦**同一处判据、同一套处置**
            #   （`ERR-META-03`：两站共用，绝不各写一份）。
            #   ★ 蟹黄堡不命中时的表现是 `UNKNOWN:`（"可能真发成功了"，不许当 REJECT），
            #     比乌托邦的「上传失败」更不可判 ⇒ 这一问在这里的价值更高。
            _sn = _same_name_precheck(client, search_name=t.get("formatted_title"),
                                      local_name=_local_nm, site_label="蟹黄堡")
            if _sn:
                _why = (f"同名冲突：蟹黄堡上已有 info.name 逐字相同的条目 "
                        f"{'、'.join(tid for tid, _ in _sn[:3])} ⇒ 提交会被静默吞掉。"
                        f"处置：先在站点删掉那条旧页，再重发（`tools/republish_one.py`）")
                print(f"    🛑 同名闸门不通过 ⇒ 拒发：{_why}")
                fail += 1
                rejected += 1
                db.save_task(info_hash=t["info_hash"], status="FAILED",
                             reason=f"crabpt_same_name_conflict: {_why}")
                continue

        # ★★★ 发种前**版本一致性闸门**（2026-10-14，`ERR-PUBLISH-20` 修复③）——
        #   蟹黄堡这一路与乌托邦**同款**（同一 helper、同一开关，`ERR-META-03`：
        #   同一事实只维护一份口径）。不通过 ⇒ 拒发 + 标 FAILED 出队。
        if config.VERSION_GATE_BEFORE_PUBLISH:
            _vg_ok, _vg_why = _version_consistency_gate(t)
            if not _vg_ok:
                print(f"    🛑 版本一致性闸门不通过 ⇒ 拒发：{_vg_why}")
                fail += 1
                rejected += 1
                db.save_task(info_hash=t["info_hash"], status="FAILED",
                             reason=f"version_gate: {_vg_why}")
                continue

        fields = _crabpt_fields(t, args.anonymous)
        print(f"\n🚀 向蟹黄堡提交：《{t.get('formatted_title')}》")
        try:
            tid, msg = client.upload_torrent(t.get("torrent_file"), fields, form_info)
        except LoginExpired as e:
            # ★ Cookie 失效 = 站点/凭据侧 ⇒ 停 + 非 0（退避），**不**推进队列、**不**标 FAILED。
            print(f"    🛑 登录失效，停止本轮（不标 FAILED，稍后重试）: {e}")
            site_down = True
            break
        except SiteError as e:
            # ★ 网络/协议层异常（`requests` 抛的那个包装）⇒ 归"这条坏"存疑？
            #   ★ 判据：`SiteError` 是**本函数/`upload_torrent` 主动 raise** 的
            #     （种子文件读不出、缺文件…）——是**这条**的问题，与站点无关
            #     ⇒ 标 FAILED 出队 + 继续下一条（否则一条坏种子文件永远卡队列）。
            print(f"    ❌ {e}")
            rejected += 1
            db.save_task(info_hash=t["info_hash"], status="FAILED",
                         reason=f"crabpt_publish_failed: {e}")
            continue

        if not tid:
            # ★★ 按**机器可读前缀**分流（#23）。三个前缀的处置**相反**，见
            #   `crabpt_site.upload_torrent` 里那段长注释 —— 尤其 `UNKNOWN:` 与
            #   乌托邦的空壳页**同名形态、相反处置**（乌托邦那份确实没发上去；
            #   蟹黄堡这份**可能是真发成功了**）。
            if msg.startswith("SITE_DOWN:"):
                print(f"    🛑 蟹黄堡站点侧问题，停止本轮（不标 FAILED）: {msg}")
                site_down = True
                break
            if msg.startswith("UNKNOWN:"):
                # ★ 绝不标 FAILED、绝不继续：这条**可能已经在站点建好了**
                #   （发布不可逆）⇒ 停 + 非 0，**要人上站点核对**。
                #   ★ 复用 `site_down` 通道 ⇒ 退出码非 0（触发退避 + 暴露）。
                print(f"    ⚠️  {msg}")
                site_down = True
                break
            # 明确 REJECT（`REJECT:` 前缀）：内容被站点拒 ⇒ 标 FAILED 出队
            #   （`CRABPT_QUEUE_WHERE` 不含 FAILED ⇒ 立刻出队）+ 继续发下一条。
            if msg.startswith("REJECT:"):
                # ★★（`S4`）与乌托邦同款分诊 —— 两站共用同一份判据与措辞
                #   （`ERR-META-03`：一处生效，绝不各写一份）。
                _why_c = _diagnose_upload_reject(msg, local_name=_local_nm)
                print(f"    ❌ 发布被拒（本条标 FAILED 出队，继续下一条）: {msg}")
                print(f"       ↳ 分诊：{_why_c}")
                rejected += 1
                db.save_task(info_hash=t["info_hash"], status="FAILED",
                             reason=(f"crabpt_publish_rejected[{_diag_kind(msg)}]: "
                                     f"{msg} | 分诊:{_why_c}"))
                continue

            # ★ 兜底：**没见过**的前缀 ⇒ 归到"停 + 非 0 + 要人核对"（安全侧）。
            #   ★ 为什么不 fall-through 当成"这条坏"出队：把一个**未知**失败当坏种子
            #     标 FAILED 是不可逆的误杀（发布不可逆语境下，未知 ⇒ 要人看）。
            #     这与 `UNKNOWN:` 同通道（site_down ⇒ 非 0 + 退避）。
            print(f"    ⚠️  未识别的失败前缀（按'要人核对'处理，不推进队列）: {msg}")
            site_down = True
            break

        ok += 1
        published_this_round += 1
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

        # ★★★ 成功计数到顶 ⇒ 停（`--limit` 的**新语义**，见上面 `max_publish` 那段）。
        #   ★ 放在**整条处理完之后**才 break：落库 / 回加做种 / 回读都做完了，
        #     不会漏掉那条已发的种子的收尾。
        if published_this_round >= max_publish:
            print(f"\n★ 本轮已成功发出 {published_this_round} 条"
                  f"（= --limit / CRABPT_MAX_PER_CYCLE 上限）⇒ 停。"
                  f"（共试 {scanned} 条，跳过坏封面 {skipped} 条）")
            break

    # ---- 退出码分层（★ 与乌托邦 `main()` 末尾那段**同形**，见 ERR-PUBLISH-03）----
    if site_down:
        print("↩️  以非 0 退出：蟹黄堡站点侧/无法判定（已中止本轮），让 daemon 的退避生效")
        sys.exit(1)
    if rejected and ok == 0:
        # ★ 系统性护栏（同 #21）：整轮一条都没成、且全是坏种子 ⇒ **不像**正常损耗，
        #   更像站点改版/判据误伤（ERR-META-01）⇒ 非 0 让它暴露 + 退避。
        print(f"↩️  以非 0 退出：{rejected} 条被拒、**无一成功** —— 疑似系统性故障"
              f"（站点改版/判据误伤），不是「恰好都是坏种子」")
        sys.exit(1)
    if rejected:
        print(f"ℹ️  退出码 0：{rejected} 条坏种子已标 FAILED 出队（队列已推进），"
              f"其余 {ok} 条成功 ⇒ **不**触发退避")
    if skipped:
        # ★ 2026-10-09：封面属"本条不可用"而**跳过**的条数（不停整轮）。
        # ★ 2026-10-10：现在"跳过"会**继续枚举下一条**（见 `max_publish` / 循环那段）
        #   —— 所以跳过本身不再是"本轮白跑"，而是"名额让给了后面的种子"。
        print(f"ℹ️  另有 {skipped} 条因**本条封面不可用**跳过（未标 FAILED，未停整轮）"
              f"⇒ 想补：给这些条换封面后重跑（`python backfill_covers_opencd.py`）")
    if ok == 0 and skipped > 0 and not site_down:
        # ★ 鲜明区分两种"零出种"：
        #   （a）**候选里有能发的、但都被前面的坏封面挡住了** —— 本改动后不会发生
        #        （坏封面只跳过、不挡后面的）；若仍出现 ⇒ 说明**整队**候选封面都坏。
        #   （b）本轮**所有**候选封面都不可用 ⇒ 一条没发。
        #   两者都**不是**站点故障 ⇒ 退出码 0（不退避，避免每 30min 空敲），但要**说清**。
        print(f"⚠️  本轮成功 0 条：试了 {scanned} 条候选，封面**全部**不可用"
              f"（{skipped} 条跳过）—— 不是站点故障。"
              f"⇒ 建议 `python backfill_covers_opencd.py --apply` 批量换封面。")
    if dup_existing:
        # ★ #13：查重命中 ⇒ 关联到蟹黄堡已有页（未重复提交）的条数。
        print(f"ℹ️  另有 {dup_existing} 条**查重命中**（蟹黄堡已有同版本）⇒ "
              f"已关联到已有页、未重复提交（退出码 0）。")
    return


# ---- ★★ 自检（#13）：发布前查重的**结构性判据** + 判据的**运行时**验证 ----
#
# ★★★ 为什么需要它（#13 / #11 的核心，2026-10-11）：
#   病根是 pipeline **只信本地 `site_status`**、从不问目标站点"有没有" ⇒ 同一专辑
#   以不同 tid 再入库时就发重复种 ⇒ 站点用**不透明的**「上传失败！」拒绝。
#   ⇒ 修法 = 发布**前**查一次站、命中就**不再提交**。
#   ★ 这个修法**会静默失效**：把查重那一整段删掉，`upload_torrent` 照发、
#     站点照拒、日志**看起来一样**（都是"上传失败"）—— 这正是它必须被自检钉住的理由。
#   ★ 判据分两半：
#     ① **结构性**（源码 AST）：`main` 与 `run_crabpt` 都必须**在 `upload_torrent`
#        之前**调用 `_dedup_lookup`；命中分支必须 `continue`（不许落到提交）。
#     ② **运行时**：`_dedup_lookup`/`_local_info_name`/`search_and_compare` 在**假 client**
#        上真的能"命中 ⇒ 返回 tid"、"不重复 ⇒ 返回 None"、"搜索失败 ⇒ 抛异常"。
#        ★ 含**阴性对照**：把 `local_name` 改一个字 ⇒ 必须**不命中**（证明命中不是空真）。
def selftest_dedup_before_publish(verbose=True):
    """断言两站发布路都**先查重、命中即不提交**；并运行时验判据可失败。返回 `(ok, 明细)`。

    ★★ 阴性对照（能失败）：见 `_neg` —— 把本地名字改一个字，`search_and_compare`
      必须**不命中**；若它仍命中，说明判据是空真（`ERR-META-32`）。
    """
    import io as _io
    detail = []
    fails = []

    # ---------- ① 结构性：两站都必须在 upload_torrent 之前调 _dedup_lookup ----------
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = _ast.parse(src)

    def _fn_node(name):
        return next((n for n in _ast.walk(tree)
                     if isinstance(n, _ast.FunctionDef) and n.name == name), None)

    def _call_lineno(node, func_name):
        """`node` 里第一次调 `func_name(...)` 的行号（按调用名匹配）。"""
        for n in _ast.walk(node):
            if isinstance(n, _ast.Call):
                f = n.func
                nm = f.attr if isinstance(f, _ast.Attribute) else getattr(f, "id", "")
                if nm == func_name:
                    return n.lineno
        return None

    for fname in ("main", "run_crabpt"):
        fn = _fn_node(fname)
        if fn is None:
            fails.append(f"★ 找不到 {fname} —— 函数被改名了？本条自检失效")
            continue
        dedup_line = _call_lineno(fn, "_dedup_lookup")
        upload_line = _call_lineno(fn, "upload_torrent")
        if dedup_line is None:
            fails.append(f"★★ {fname} 里**没有**调 `_dedup_lookup` ⇒ 发布前不查重"
                         f"（重复种又被发出去了）")
        elif upload_line is not None and dedup_line > upload_line:
            fails.append(f"★★ {fname} 里 `_dedup_lookup`（行 {dedup_line}）在 "
                         f"`upload_torrent`（行 {upload_line}）**之后** ⇒ 查重太晚")
        else:
            detail.append(f"① {fname}：先 `_dedup_lookup`（行 {dedup_line}）"
                          f"后 `upload_torrent`（行 {upload_line}）✅")

    # ---------- ①b（S4）结构性：同名闸门必须在 `upload_torrent` 之前 ----------
    #   ★ 与 ① 同一条纪律：这个闸门**删掉也不会报错**（站点会自己拒，只是用那句
    #     零线索的「上传失败」） ⇒ 不钉住就会静默失效。
    for fname in ("main", "run_crabpt"):
        fn = _fn_node(fname)
        if fn is None:
            continue                            # 上面已经记过 fails 了
        sn_line = _call_lineno(fn, "_same_name_precheck")
        upload_line = _call_lineno(fn, "upload_torrent")
        if sn_line is None:
            fails.append(f"★★ {fname} 里**没有**调 `_same_name_precheck` ⇒ 同名冲突"
                         f"又会以『上传失败』的形式闷在通用文案里（ERR-PUBLISH-25）")
        elif upload_line is not None and sn_line > upload_line:
            fails.append(f"★★ {fname} 里同名闸门（行 {sn_line}）在 `upload_torrent`"
                         f"（行 {upload_line}）**之后** ⇒ 闸门太晚（种已发出去）")
        else:
            detail.append(f"①b {fname}：同名闸门（行 {sn_line}）在 upload（行 {upload_line}）之前 ✅")

    # ---------- ② 结构性：命中分支必须 `continue`（不许落到提交）----------
    #   ★ 判据：源码里命中分支的那段**必须**含 `continue`，且**不**含 `upload_torrent`。
    #   ★ 用**源码文本**（不是 AST unparse）—— 这里要的是"那段长什么样"，
    #     而且注释里正好写着"不再重复提交"，读起来对得上。
    for label, anchor in (("乌托邦(main) ", "已关联到站点已有页"),
                          ("蟹黄堡(run_crabpt) ", "已关联到蟹黄堡已有页")):
        idx = src.find(anchor)
        if idx < 0:
            fails.append(f"★ 找不到 {label}命中分支的标记串 {anchor!r} —— 分支被改了？")
            continue
        seg = src[idx:idx + 700]        # 命中分支那几十行
        if "continue" not in seg:
            fails.append(f"★★ {label}命中分支里没有 `continue` ⇒ 会**落到提交**（重复发）")
        else:
            detail.append(f"② {label}命中分支 `continue` ✅")

    # ---------- ③ 运行时：search_and_compare 判据（含阴性对照）----------
    import musopia
    import site_verify

    def _benc(obj):
        if isinstance(obj, int):
            return b"i%de" % obj
        if isinstance(obj, bytes):
            return b"%d:%s" % (len(obj), obj)
        if isinstance(obj, str):
            return _benc(obj.encode())
        if isinstance(obj, dict):
            out = b"d"
            for k in sorted(obj, key=lambda x: x.encode() if isinstance(x, str) else x):
                out += _benc(k) + _benc(obj[k])
            return out + b"e"
        raise TypeError(type(obj))

    def _tb(name):
        return _benc({"announce": "http://x/announce",
                      "info": {"name": name, "piece length": 16384,
                               "pieces": b"\x00" * 20, "length": 1000}})

    LOCAL = "Stravinsky; Chamber Works (2024) [FLAC]"

    class _Resp:
        def __init__(self, text="", status=200):
            self.text, self.status_code = text, status

    class _Fake:
        """假客户端：`/torrents.php` 按**站点真实语义**作答（#23）。

        ★★ 为什么不能像旧夹具那样"恒返回同一条链接"：那样**结构上不可能**发现 #23 的
           召回 bug（单串/多串都返回同一结果 ⇒ 静默绿）。本假站点复刻实测到的站点语义：
           **AND（每个空白 token 都必须是假站点标题的子串）** + 命中才吐链接。
           站点假标题 = `SITE_TITLE`（与 LOCAL 的 `info.name` **不同** —— 正是现实）。
        """
        # 站点**显示标题**（本地 info.name 是另一串 —— 这正是判据必须用 info.name 的原因）
        SITE_TITLE = "Various Artists - Stravinsky: Chamber Works 2024 - FLAC 分軌 - Orpheus"
        SEARCH_PARAMS = staticmethod(lambda nm: musopia.SEARCH_PARAMS(nm))

        def __init__(self, hit_map, *, site_title=None, target_tid="5998",
                     decoys=()):
            self.hit_map = hit_map
            self.site_title = site_title or self.SITE_TITLE
            self.target_tid = target_tid
            self.decoys = list(decoys)      # 排在目标**之前**的诱饵 id（打"靠运气排序"）
            self.queries = []               # ★ 记录**实际发过**的列表页查询（#31：验"命中即停"）

        def _match(self, query):
            toks = [t for t in str(query).split() if t]
            return bool(toks) and all(t in self.site_title for t in toks)

        def get(self, path, **kw):
            if path.startswith("/torrents.php"):
                q = (kw.get("params") or {}).get("search", "")
                self.queries.append(q)      # ★ 记下每一条真正发出的查询
                if not self._match(q):
                    return _Resp("")           # AND 不成立 ⇒ **0 候选**（复刻实测）
                links = "".join(f'<a href="details.php?id={d}">x</a>' for d in self.decoys)
                links += f'<a href="details.php?id={self.target_tid}">x</a>'
                return _Resp(links)
            if path.startswith("/details.php"):
                return _Resp("<html><h1>Album</h1></html>")
            raise AssertionError(path)

        def download_torrent(self, tid):
            return self.hit_map.get(tid)

        def find_existing_by_name(self, name, *, local_name, base_path="/torrents.php",
                                  max_candidates=8, diag=None):
            # ★ 夹具显式给 8（镜像生产旧默认）；生产走 config（`find_existing_by_name` 默认 None）。
            return musopia.search_and_compare(self, name, local_name=local_name,
                                              base_path=base_path,
                                              max_candidates=max_candidates,
                                              diag=diag)

    # ---- #23 核心：AND-语义假站点上，「单串」必须**漏**、「多子查询」必须**中** ----
    # 本地名与站点标题共享 token：`Stravinsky`、`Chamber`、`Works`、`2024`（站侧标题里也有）。
    SITE_TITLE = _Fake.SITE_TITLE
    LOCAL_FULL = ("Stravinsky; Royal Academy of Music and Juilliard School Ensemble, "
                  "Barbara Hannigan - Chamber Works (2024) [FLAC]")
    # 目标页挂的站点副本：其 info.name 与本地**相同**（= 真重复）
    hit_map = {"5998": _tb(LOCAL_FULL)}

    # ---- (#31 精准优先) 顺序 + 命中即停判据 ----
    #   ★ 旧版这里是"#23 前修复对照：单串必须**漏**" —— **已废弃**：#31 把**高精度单 token**
    #     （艺术家名）排最前，所以"单串"如今**本该命中**（那正是本修的目的）。
    #     真问题是相反方向：顺序写反（宽查询在前）⇒ 噪声吃光预算 ⇒ 漏召回（13/20→11/20）。

    # (c) ★ 顺序断言（纯函数、零网络）：`SEARCH_QUERY_PLAN` 必须**窄在前、宽在后**。
    #   ★ 实测铁律：**单 token（哪怕 CJK）是宽的**（`Etre`/`群星` 单独查 109 条）
    #     ⇒ 单 token 必须**排在多 token 窗口之后**；多 token 窗口（头3/头2）才是窄的。
    _plan_full = musopia.SEARCH_QUERY_PLAN(LOCAL_FULL, max_queries=5)
    if not _plan_full or _plan_full[0] != "Stravinsky Royal Academy":
        fails.append(f"★★ #31 顺序：首条应是**多 token 窄窗** `Stravinsky Royal Academy`，"
                     f"得到 {_plan_full[:1]!r}")
    else:
        detail.append("③ #31 顺序：多 token 窄窗（头3）排首条 ✅")
    #   ★ **单 token 不许**出现在前两条（它们宽 ⇒ 会吃光预算）。
    _plan_etre = musopia.SEARCH_QUERY_PLAN(
        "Etre (2001.03.10) (album) One Day [FLAC] {PQCD-10004}", max_queries=5)
    if not _plan_etre or _plan_etre[0] != "Etre 2001 03":
        fails.append(f"★★ #31 顺序：`Etre` 行首条应是 `Etre 2001 03`（多 token 窄窗），"
                     f"得到 {_plan_etre[:1]!r}")
    elif "Etre" in _plan_etre[:2]:
        fails.append(f"★★ #31 顺序：单 token `Etre`（宽，实测 109 条）不该排前两条：{_plan_etre[:2]}")
    else:
        detail.append("③ #31 顺序：`Etre` 行首条 = 多 token 窄窗（单 token 后置）✅")
    _plan_cjk = musopia.SEARCH_QUERY_PLAN("[250813] 徐元白 - 古琴 [FLAC]", max_queries=5)
    if not _plan_cjk or _plan_cjk[0] != "徐元白 古琴 FLAC":
        fails.append(f"★★ #31 顺序：CJK 行首条应是去日期头窗 `徐元白 古琴 FLAC`，得到 {_plan_cjk[:1]!r}")
    elif _plan_cjk[0] == "徐元白":
        fails.append("★★ #31 顺序：CJK 单 token 不该排首条（宽）")
    else:
        detail.append("③ #31 顺序：CJK 行首条 = 去日期多 token 窗（非单 token）✅")
    if musopia.SEARCH_QUERY_PLAN("x", search_name="S", max_queries=0) != ["S"]:
        fails.append("★★ #31 杀开关失效：max_queries<=0 应只回 [search_name]")
    else:
        detail.append("③ #31 杀开关：`max_queries<=0 ⇒ [search_name]` ✅")
    if musopia.SEARCH_QUERY_PLAN(LOCAL_FULL) != musopia.SEARCH_QUERY_PLAN(LOCAL_FULL):
        fails.append("★★ #31 plan 不确定：两次调用结果不同 ⇒ 非纯函数")
    else:
        detail.append("③ #31 plan 确定性：两次调用一致 ✅")

    # (a) ★ 命中即停：首条**窄**查询命中 ⇒ 后续宽查询**不得**发出。
    ETRE_LOCAL = "Etre (2001.03.10) (album) One Day [FLAC] {PQCD-10004}"
    ETRE_SITE = "Etre 2001 03 - One Day - FLAC - JPopsuki"
    _etre_fake = _Fake({"5998": _tb(ETRE_LOCAL)}, site_title=ETRE_SITE)
    _etre_hit = musopia.search_and_compare(_etre_fake, ETRE_LOCAL, local_name=ETRE_LOCAL)
    _etre_forbidden = musopia.SEARCH_QUERY_PLAN(ETRE_LOCAL, max_queries=5)[-1]
    if _etre_hit != "5998":
        fails.append(f"★★ #31 命中即停：窄查询应命中 5998，得到 {_etre_hit!r} "
                     f"（实发 {_etre_fake.queries}）")
    elif _etre_forbidden in _etre_fake.queries:
        fails.append(f"★★ #31 命中即停：命中后仍发了宽查询 {_etre_forbidden!r} "
                     f"（实发 {_etre_fake.queries}）⇒ 没早停")
    else:
        detail.append("③ #31 命中即停：首条窄查询命中 ⇒ 宽查询未发 ✅")

    # (a-阴性对照) 让**只有宽查询**（最后一条 `One Day`）能中 ⇒ 必须继续走（不早停）且**确实命中**。
    #   ★ 证明上一条不是空真：记录器是真的、早停是"顺序"造成的（精准查询先命中才早停），
    #     而不是"宽查询结构上不可能". 假站点题只含 `One Day` ⇒ 所有高精度/头尾查询都落空，
    #     只有宽 tier 的 `One Day` 命中。
    _neg_fake = _Fake({"5998": _tb(ETRE_LOCAL)}, site_title="One Day")
    _neg_hit = musopia.search_and_compare(_neg_fake, ETRE_LOCAL, local_name=ETRE_LOCAL)
    if _neg_hit != "5998":
        fails.append(f"★★ #31 阴性对照：宽查询能中时应召回 5998，得到 {_neg_hit!r}"
                     f"（实发 {_neg_fake.queries}）")
    elif len(_neg_fake.queries) < 2:
        fails.append(f"★★ #31 阴性对照：精准查询未中时应继续发后续查询，实发 {_neg_fake.queries}")
    elif "One Day" not in _neg_fake.queries:
        fails.append(f"★★ #31 阴性对照：应发出宽查询 `One Day`，实发 {_neg_fake.queries}")
    else:
        detail.append("③ #31 阴性对照：精准查询落空 ⇒ 继续发后续查询至 `One Day` 命中 ✅")

    # (b) ★ 噪声不截断：目标 = **精准查询**首候选；20 诱饵挂**宽查询**，`max_candidates=8`。
    _decoys20 = [f"8{i:04d}" for i in range(20)]

    class _FakeSplit(_Fake):
        """精准查询只吐目标；**宽查询**吐一堆诱饵（复刻"宽查询噪声淹预算"）。"""
        def get(self, path, **kw):
            if path.startswith("/torrents.php"):
                q = (kw.get("params") or {}).get("search", "")
                self.queries.append(q)
                if q == "Etre 2001 03":                  # 首条窄查询 ⇒ 只有真目标
                    return _Resp(f'<a href="details.php?id={self.target_tid}">x</a>')
                links = "".join(f'<a href="details.php?id={d}">x</a>' for d in self.decoys)
                links += f'<a href="details.php?id={self.target_tid}">x</a>'
                return _Resp(links)
            return super().get(path, **kw)

    _split = _FakeSplit({**{d: _tb("Some Other Album") for d in _decoys20},
                         "5998": _tb(ETRE_LOCAL)},
                        site_title="Etre 2001 03 One Day FLAC PQCD 10004 JPopsuki",
                        decoys=_decoys20)
    _split_hit = musopia.search_and_compare(_split, ETRE_LOCAL, local_name=ETRE_LOCAL,
                                            max_candidates=8)
    if _split_hit != "5998":
        fails.append(f"★★ #31 噪声不截断：精准查询的目标应先于宽查询噪声被验证，得到 {_split_hit!r}")
    else:
        detail.append("③ #31 噪声不截断：精准查询的目标先于宽查询噪声被验证 ⇒ 命中 ✅")

    # 后修复（绿）：多子查询（精准优先）⇒ 必须命中 5998。
    post = musopia.search_and_compare(_Fake(hit_map), LOCAL_FULL, local_name=LOCAL_FULL)
    if post != "5998":
        fails.append(f"★★ 多子查询应召回 5998，得到 {post!r} ⇒ 召回修复失效")
    else:
        detail.append("③ 多子查询（精准优先）⇒ 召回 tid=5998 ✅")

    # 噪声位次：15 个诱饵排在目标**之前** ⇒ 仍必须命中（打掉"靠运气排序"）。
    decoys = [f"9{i:04d}" for i in range(15)]
    noisy_map = {**{d: _tb("Some Other Album") for d in decoys}, "5998": _tb(LOCAL_FULL)}
    noisy = musopia.search_and_compare(
        _Fake(noisy_map, decoys=decoys), LOCAL_FULL, local_name=LOCAL_FULL,
        max_candidates=20)
    if noisy != "5998":
        fails.append(f"★★ 噪声位次：目标在 15 诱饵之后应仍命中，得到 {noisy!r}")
    else:
        detail.append("③ 噪声位次：15 诱饵在前仍命中 5998 ✅")

    # 原有基础用例（保持）：同一 info.name ⇒ 命中；这里用一条**单 token** 名，
    #   plan 只有 1 条，走的是与旧逻辑等价的路径。
    _one = "Forever Now (2026)"
    hit = musopia.search_and_compare(_Fake({"5998": _tb(_one)}, site_title=_one + " x"),
                                     "t", local_name=_one)
    if hit != "5998":
        fails.append(f"★★ 运行时①: 同版本应命中 5998，得到 {hit!r}")
    else:
        detail.append("③ 运行时：info.name 相同 ⇒ 命中 tid=5998 ✅")

    # ★ 阴性对照：本地名字改一个字 ⇒ 必须**不**命中（证明上一条不是空真）。
    neg = musopia.search_and_compare(_Fake({"5998": _tb(_one)}, site_title=_one + " x"),
                                     "t", local_name=_one + "Z")
    if neg is not None:
        fails.append(f"★★ 阴性对照失败：名字加 Z 仍命中 {neg!r} ⇒ 判据是空真")
    else:
        detail.append("③ ★ 阴性对照：名字改一字 ⇒ 不命中 ✅")

    # 搜索失败（HTTP≠200）⇒ 必须**抛**（不是返回 None）—— 用户拍板"停轮非 0"。
    class _Down(_Fake):
        def get(self, path, **kw):
            if path.startswith("/torrents.php"):
                return _Resp(status=503)
            return super().get(path, **kw)

    try:
        musopia.search_and_compare(_Down({}), LOCAL_FULL, local_name=LOCAL_FULL)
        fails.append("★★ 搜索 HTTP 503 竟没抛 ⇒ '搜不了'会被当成'没重复'照发（危险）")
    except musopia.SiteError:
        detail.append("③ 搜索失败(HTTP503) ⇒ 抛 SiteError（调用方停轮非 0）✅")

    # ★★ 阴性对照（纪律）：多子查询里**第 2 条**才 503 ⇒ 仍必须**抛**
    #   （护住"绝不把**中途**的传输失败降级成'没匹配'"—— 这是本修复最危险的回归面）。
    #   ★★ 命中即停带来的**空真陷阱**（#31 实测）：若第 1 条查询就命中，只会发 1 次请求，
    #      "第 2 条 503"永远不发生 ⇒ 本判据会**空真通过**（绿得没意义）。故用
    #      `site_title` 让**第 1 条精准查询也落空**（0 候选），**强制**走到第 2 条查询。
    class _DownSecond(_Fake):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._n = 0

        def get(self, path, **kw):
            if path.startswith("/torrents.php"):
                self._n += 1
                if self._n >= 2:
                    return _Resp(status=503)
            return super().get(path, **kw)

    try:
        # ★ site_title = 与 LOCAL_FULL 无交集 ⇒ 第 1 条精准查询 0 命中 ⇒ 才会走到第 2 条。
        musopia.search_and_compare(_DownSecond(hit_map, site_title="ZZZ nomatch"),
                                   LOCAL_FULL, local_name=LOCAL_FULL)
        fails.append("★★ 中途(第2条查询)503 竟没抛 ⇒ 多查询会**吞**中途失败（危险）")
    except musopia.SiteError:
        detail.append("③ 中途第2条查询 503 ⇒ 仍抛 SiteError ✅（首条落空、非空真）")

    # `_local_info_name` 从真实 .torrent 字节取权威名（复用 site_verify）。
    import tempfile
    _d = tempfile.mkdtemp(prefix="dedupless-")
    _p = os.path.join(_d, "x.torrent")
    with open(_p, "wb") as _fh:
        _fh.write(_tb(LOCAL))
    got_name = _local_info_name({"torrent_file": _p})
    if site_verify.normalize_name(got_name) != site_verify.normalize_name(LOCAL):
        fails.append(f"★★ _local_info_name 取出的名字不对：{got_name!r} != {LOCAL!r}")
    else:
        detail.append("③ `_local_info_name` 从 .torrent 取到权威 info.name ✅")
    # 文件缺失 ⇒ 返回 ""（不抛；调用方据此"跳过查重、照旧提交"）。
    if _local_info_name({"torrent_file": os.path.join(_d, "nope.torrent")}) != "":
        fails.append("★★ 缺文件时 _local_info_name 竟非空 ⇒ 应返回 ''（跳过查重）")
    else:
        detail.append("③ `_local_info_name` 缺文件 ⇒ 返回 ''（不抛）✅")

    # `_dedup_lookup`：选中开关时命中会返回 tid；关闭开关时短路返回 None。
    #   ★ 用 `site_title=_one + " x"` 的假站点：查询串 `_one` 的 token 都成立 ⇒ 命中 5998。
    _saved = config.DEDUP_BEFORE_PUBLISH
    try:
        config.DEDUP_BEFORE_PUBLISH = True
        r = _dedup_lookup(_Fake({"5998": _tb(_one)}, site_title=_one + " x"),
                          search_name=_one, local_name=_one, site_label="测试站")
        if r != "5998":
            fails.append(f"★★ _dedup_lookup 开关开、命中时应返回 5998，得到 {r!r}")
        else:
            detail.append("③ `_dedup_lookup` 命中返回 tid ✅")
        config.DEDUP_BEFORE_PUBLISH = False
        r2 = _dedup_lookup(_Fake({"5998": _tb(_one)}, site_title=_one + " x"),
                           search_name=_one, local_name=_one, site_label="测试站")
        if r2 is not None:
            fails.append(f"★★ 开关关时 _dedup_lookup 竟返回 {r2!r} ⇒ 短路失效")
        else:
            detail.append("③ `_dedup_lookup` 开关关 ⇒ 短路 None（旧行为）✅")
    finally:
        config.DEDUP_BEFORE_PUBLISH = _saved

    # ---------- ④（S4 / ERR-PUBLISH-25）同名闸门 + 被拒分诊 ----------
    #
    # ★★ 为什么这些判据必须存在（而不是"生产上没见过它报"就算数）：
    #   `_same_name_precheck` 在**正常路径上永远返回空**（同名 ⇒ 查重已 MATCH ⇒ 走了
    #   "关联已有页"分支）。它命中的唯一场面是"站点上有同名条目，但那条**没进候选**"——
    #   这正是 `21811` 的真实形态。⇒ 生产日志里看不到它命中，**恰恰是它没被验证过**，
    #   不能当成"它没问题"（那是空真，`ERR-META-32`）。
    #   ⇒ 必须用假 client **造**这个场面，并证明它**真的能被拒**（有阳性对照）。
    # ★ `_diag_kind`/`_diagnose_upload_reject` 同理：文案归类错了不会报错、不会崩，
    #   只会**安静地把人引向错的排查方向**（`ERR-PUBLISH-25` 的全部代价）。⇒ 逐类钉住。

    # 阳性：假 client 报"站点上有同名条目"（走新签名的 diag 通道）
    class _SameNameFake(_Fake):
        def find_existing_by_name(self, name, *, local_name, base_path="/torrents.php",
                                  max_candidates=None, diag=None):
            # ★ 只模拟"站上真有同名条目"的**结论**：查重没命中（None），
            #   但 diag 里带着同名的 tid —— 正是 21811 那个局面的最小复刻。
            if diag is not None:
                diag["same_name"] = [("21811", local_name)]
                diag["local_name"] = local_name
            return None

    _sn = _same_name_precheck(_SameNameFake({}), search_name=_one, local_name=_one,
                              site_label="测试站")
    if not (_sn and _sn[0][0] == "21811"):
        fails.append(f"★★ 同名闸门：假客户端的同名条目没被识别出来（得到 {_sn!r}）"
                     f" ⇒ 21811 那种『上传失败』仍会闷死在通用文案里")
    else:
        detail.append("④ 同名闸门：站点上有同名条目 ⇒ 识别为冲突 ✅")

    # ★ 阴性对照：站点上**没有**同名条目 ⇒ 必须返回空（否则闸门会拒掉本来能发的条目）
    _sn2 = _same_name_precheck(_Fake({"5998": _tb("Some Other Album")},
                                     site_title="Some Other Album x"),
                               search_name="Some Other Album",
                               local_name=_one, site_label="测试站")
    if _sn2:
        fails.append(f"★★ 阴性对照：站点上没有同名条目，闸门却报冲突 {_sn2!r}"
                     f" ⇒ 会拒发站点本来会收的条目（少发是静默的，比多发危险）")
    else:
        detail.append("④ ★ 阴性对照：无同名 ⇒ 闸门放行 ✅")

    # ★★ 近亲 ≠ 同名（`site_verify.is_near_miss` 的边界）：近亲**只进日志**、
    #   **绝不**进 `same_name` ⇒ 绝不拒发。这条是上面阴性对照的加强版：
    #   名字"极像"（只多一个词）仍必须放行，否则闸门会大面积误杀。
    if site_verify.is_near_miss(_one, _one + " TESTNAME"):
        detail.append("④ 近亲判据能认出『同名加尾缀』✅")
    else:
        fails.append("★ 近亲判据认不出『同名加尾缀』 ⇒ near_miss 诊断失效")

    class _NearOnlyFake(_Fake):
        """只报「近亲」，**不**报「同名」—— 闸门必须**放行**。"""
        def find_existing_by_name(self, name, *, local_name, base_path="/torrents.php",
                                  max_candidates=None, diag=None):
            if diag is not None:
                diag["local_name"] = local_name
                diag["near_miss"] = [("777", local_name + " TESTNAME")]
            return None

    if _same_name_precheck(_NearOnlyFake({}), search_name=_one, local_name=_one,
                           site_label="测试站"):
        fails.append("★★ 近亲（非同名）竟被判成同名冲突 ⇒ 会拒发站点本来会收的条目"
                     "（少发是**静默**的，比多发危险 —— ERR-PUBLISH-25 的边界）")
    else:
        detail.append("④ ★ 近亲（非同名）⇒ **不**判冲突 ✅")

    # ★★★ 上面 `_NearOnlyFake` 是**桩**：它直接伪造 `diag["near_miss"]`，
    #   因此**结构上查不出** `musopia.search_and_compare` 把近亲塞进 `same_name` 的错误
    #   （实测：把 musopia 里那句改成 `diag["same_name"].append(...)`，上面两条**全绿**）。
    #   ⇒ 必须再走一遍**真实**的 `search_and_compare`（用 `_Fake` 假站点），
    #     让 musopia **自己**决定往哪个桶里放。这才是这条边界的真正判据。
    # ★ 造场面：本地名 = `_one`，站点上挂的是**多一个词**的 `_one + " TESTNAME"`。
    #   两者不相等 ⇒ 查重返回 None ⇒ 走"记 diag"这条路（正是我们要验的）。
    #   ★★ `target_tid` 必须**显式**给，并与 `hit_map` 的键一致 —— `_Fake.get` 吐的
    #     链接永远指向 `self.target_tid`（默认 `"5998"`）。第一次写这条判据时我漏了它，
    #     于是 `download_torrent("5998")` 取不到 ⇒ 静默跳过 ⇒ `near_miss` 恒空 ⇒
    #     **判据红在一个与它无关的理由上**（这类"夹具自己配错"最容易把测试写成假绿/假红）。
    _real_diag = {}
    _rr = musopia.search_and_compare(_Fake({"777": _tb(_one + " TESTNAME")},
                                           site_title=(_one + " TESTNAME") + " x",
                                           target_tid="777"),
                                     _one, local_name=_one, diag=_real_diag)
    if _real_diag.get("same_name"):
        fails.append(f"★★ 真实 `search_and_compare` 把**近亲**（不是同名）放进了 "
                     f"`same_name` {_real_diag['same_name']!r} ⇒ 同名闸门会大面积误杀"
                     f"（这是桩测试查不出的：桩直接伪造了桶）")
    elif not _real_diag.get("near_miss"):
        fails.append("★ 真实 `search_and_compare` 没把近亲记进 `near_miss` ⇒ "
                     "诊断证据为零（21811 那种『查重说没有、站点上偏有』又无从解释）")
    else:
        detail.append("④ ★ 真实路径：近亲只进 `near_miss`、**不**进 `same_name` ✅")

    # 分诊：四类文案各归各类，且未知文案**不**硬套类别
    for _msg, _want in (("REJECT: 音乐乌托邦 :: 上传失败！", "同名冲突"),
                        ("REJECT: 请选择类别", "必填项缺失"),
                        ("REJECT: 你没有权限发布", "权限/配额"),
                        ("REJECT: 无效的种子文件", "种子被拒"),
                        ("REJECT: 站点说了句没有特征的话", "未分类")):
        _got = _diag_kind(_msg)
        if _got != _want:
            fails.append(f"★ 分诊错：{_msg!r} 归成 {_got!r}，应为 {_want!r}")
    if _diag_kind("REJECT: 音乐乌托邦 :: 上传失败！") == "同名冲突":
        detail.append("④ 「上传失败」⇒ 分诊为**同名冲突**（不是字段）✅")
    # ★ 未知文案的结论里必须**带上三步对照法**、且**不许**自称已定位
    _unk = _diagnose_upload_reject("REJECT: 站点说了句没有特征的话", local_name=_one)
    if "三步对照" not in _unk or "info.name" not in _unk:
        fails.append("★★ 未知文案的分诊没给出下一步动作 ⇒ 又会去猜字段（ERR-PUBLISH-25 重演）")
    else:
        detail.append("④ 未知文案 ⇒ 给出三步对照法 + 要发的 info.name ✅")

    ok = not fails
    if verbose:
        print(f"{'✅' if ok else '❌'} 发布前查重自检（结构性 + 运行时，含阴性对照）")
        for d in detail:
            print(f"    {d}")
        for f in fails:
            print(f"    {f}")
    return ok, (fails or detail)


def selftest_version_gate(verbose=True):
    """断言两站发布路都在 `upload_torrent` 之前跑**版本一致性闸门**；并运行时验判据。

    返回 `(ok, 明细)`。

    ★★ 阴性对照（能失败）：
      · 构造"库说整轨 / 盘上是分轨"的假件 ⇒ 闸门**必须** `ok=False`（拒发）；
      · 构造"文件集不一致"（本地种子比目录少一个文件）⇒ **必须**拒发；
      · 一致性件 ⇒ **必须**放行（否则闸门变成"一律拒发"，等价于把整条队列停掉）。
    """
    import tempfile
    detail, fails = [], []

    # ---------- ① 结构性：两站都要在 upload_torrent 之前调闸门 ----------
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = _ast.parse(src)

    def _fn_node(name):
        return next((n for n in _ast.walk(tree)
                     if isinstance(n, _ast.FunctionDef) and n.name == name), None)

    def _call_lineno(node, func_name):
        for n in _ast.walk(node):
            if isinstance(n, _ast.Call):
                f = n.func
                nm = f.attr if isinstance(f, _ast.Attribute) else getattr(f, "id", "")
                if nm == func_name:
                    return n.lineno
        return None

    for fname in ("main", "run_crabpt"):
        fn = _fn_node(fname)
        if fn is None:
            fails.append(f"★ 找不到 {fname} —— 函数被改名了？本条自检失效")
            continue
        gate_line = _call_lineno(fn, "_version_consistency_gate")
        upload_line = _call_lineno(fn, "upload_torrent")
        if gate_line is None:
            fails.append(f"★★ {fname} 里**没有**调 `_version_consistency_gate`"
                         f" ⇒ 版本一致性闸门缺失（21662 那种错版本又会被发出去）")
        elif upload_line is not None and gate_line > upload_line:
            fails.append(f"★★ {fname} 里闸门（行 {gate_line}）在 `upload_torrent`"
                         f"（行 {upload_line}）**之后** ⇒ 闸门太晚")
        else:
            detail.append(f"① {fname}：先闸门（行 {gate_line}）后 upload（行 {upload_line}）✅")

    # ---------- ② 运行时：真造件，验证判据能失败、也能放行 ----------
    import assets as _assets
    import site_verify as _sv
    try:
        import torf as _torf
    except ImportError as e:                        # noqa: BLE001
        fails.append(f"★ 缺 torf（{e}）⇒ 运行时判据没跑（对照缺失，不算通过）")
        _torf = None

    _saved_flag = config.VERSION_GATE_BEFORE_PUBLISH
    config.VERSION_GATE_BEFORE_PUBLISH = True
    try:
        with tempfile.TemporaryDirectory() as td:
            # ★ 内容目录与输出种子的**父目录分开** —— 否则 `out.torrent` 会被算进
            #   `files_of_dir(td)`（实测踩到：本地 4 个 vs 种子 3 个 ⇒ 假拒发，
            #   那是我这个夹具自己的 bug，不是判据的）。
            content = os.path.join(td, "content")
            os.makedirs(content)
            t1 = os.path.join(content, "01.flac"); open(t1, "wb").write(b"a")
            t2 = os.path.join(content, "02.flac"); open(t2, "wb").write(b"b")
            with open(os.path.join(content, "x.cue"), "w", encoding="utf-8") as fh:
                fh.write('FILE "missing.wav" WAVE\n')
            # 强判据应当判**分轨**（cue 的 FILE 目标缺失）
            live = _assets.cue_track_type(content)
            if live != "分轨":
                fails.append(f"★★ 强判据：有 cue 但 FILE 缺失 ⇒ 应判分轨，实得 {live!r}"
                             f"（修复①未生效）")
            else:
                detail.append("② 强判据：cue 的 FILE 目标缺失 ⇒ 判分轨 ✅")

            # ②b ★★ 阴性对照：**逐轨 cue**（多 FILE 各指一轨、目标**都存在**）⇒ 仍须判分轨。
            #   ★ 这正是 B16 一修**失手**的形状（Kinks）：只看"目标在不在"会判整轨。
            #   造法：一份 cue 里 2 条 FILE，分别指向目录里真实存在的两个 flac。
            with open(os.path.join(content, "x.cue"), "w", encoding="utf-8") as fh:
                fh.write('FILE "01.flac" WAVE\n  TRACK 01 AUDIO\n    TITLE "a"\n'
                         'FILE "02.flac" WAVE\n  TRACK 02 AUDIO\n    TITLE "b"\n')
            live_per = _assets.cue_track_type(content)
            if live_per != "分轨":
                fails.append(f"★★ 阴性对照：逐轨 cue（多 FILE 各指一轨、目标都在）"
                             f"⇒ 应判分轨，实得 {live_per!r}（修复①只治了'目标缺失'，"
                             f"没治'逐轨 cue'）")
            else:
                detail.append("②b 阴性对照：逐轨 cue（多 FILE 各指一轨）⇒ 判分轨 ✅")
            # 恢复成'目标缺失'夹具，供后面的闸门用例用（库=整轨/盘=分轨）。
            with open(os.path.join(content, "x.cue"), "w", encoding="utf-8") as fh:
                fh.write('FILE "missing.wav" WAVE\n')

            # 假件 A：库说整轨（旧判据的错值），盘上实况分轨 ⇒ 必须拒发
            if _torf is not None:
                tp = os.path.join(td, "out.torrent")
                tb = _torf.Torrent(path=content, trackers=["http://x/announce"], private=True)
                tb.generate(); tb.write(tp)
                fake_task = {"content_path": content, "torrent_file": tp,
                             "track_type": "整轨"}          # ★ 库内错值
                ok, why = _version_consistency_gate(fake_task)
                if ok:
                    fails.append("★★ 阴性对照：库=整轨/盘=分轨 竟放行 ⇒ 闸门是空真")
                else:
                    detail.append(f"③ 阴性对照：库=整轨/盘=分轨 ⇒ 拒发 ✅（{why[:40]}…）")
                # 一致性件（库=分轨）⇒ 必须放行
                fake_ok = {"content_path": content, "torrent_file": tp,
                           "track_type": "分轨"}
                ok2, why2 = _version_consistency_gate(fake_ok)
                if not ok2:
                    fails.append(f"★ 一致性件（库=分轨）竟被拒 ⇒ 闸门会卡死队列：{why2}")
                else:
                    detail.append("④ 一致性件（库=分轨/盘=分轨/文件集同）⇒ 放行 ✅")
                # 文件集不一致 ⇒ 必须拒发（造一个"多一个文件"的目录 vs 旧种子）
                extra = os.path.join(content, "03.flac"); open(extra, "wb").write(b"c")
                ok3, why3 = _version_consistency_gate(fake_ok)
                os.remove(extra)
                if ok3:
                    fails.append("★★ 阴性对照：文件集不一致（目录多一个文件）竟放行")
                else:
                    detail.append(f"⑤ 阴性对照：文件集不一致 ⇒ 拒发 ✅（{why3[:40]}…）")
    finally:
        config.VERSION_GATE_BEFORE_PUBLISH = _saved_flag

    ok = not fails
    if verbose:
        print(f"{'✅' if ok else '❌'} 版本一致性闸门自检（结构性 + 运行时，含阴性对照）")
        for d in detail:
            print(f"    {d}")
        for f in fails:
            print(f"    {f}")
    return ok, (fails or detail)


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
        # ★★ 第 4 条（2026-10-04 加）：两条回加路里"绑定前求值"的名字。
        #   ★ 独立失效模式：前三条查的是"站点/口径"，这条查的是"**Python 作用域**"
        #     —— `ERR-PUBLISH-02` 就是它（`t` 未定义 ⇒ 19 条发布成功却 0 条做种）。
        ok4, _ = selftest_readd_names_defined()
        # ★★ 第 5 条（2026-10-04 加，#21）：发布循环的**失败分级处置**结构。
        #   ★ 独立失效模式：前四条查"站点/口径/作用域"，这条查"**控制流分支**"
        #     —— 坏种子跳过(REJECT→标FAILED→continue) / 站点崩停止(SITE_DOWN→break)。
        #     它**静默失效**：简化回"统一 continue"照样绿，而坏种子又开始堵队列。
        ok5, _ = selftest_publish_failure_classification()
        # ★★ 第 6 条（2026-10-04 加，#23）：**蟹黄堡** `run_crabpt` 的失败分级结构。
        #   ★ 独立失效模式：与第 5 条**同族但不同函数** —— 蟹黄堡是**另一条**发布路
        #     （转种队列、`ORDER BY updated_at` 确定性队首、`UNKNOWN:` 单列）。
        #     第 5 条**不会**覆盖它（那条只扫 `main`）。简化回"任何失败 sys.exit"
        #     照样绿，而一条坏种子又把**转种队列**永久卡住（比乌托邦更糟）。
        ok6, _ = selftest_crabpt_failure_classification()
        # ★★ 第 7 条（2026-10-10 加，蟹黄堡三天零出种）：`--limit` 必须按**成功计数**
        #   截断、坏封面跳过即继续 —— 而不是"先 `valid[:limit]` 预截、再只试那一条"。
        #   ★ 独立失效模式：前六条都不查"**候选截断的时机**"。这条查的正是它
        #     —— 旧形状 `valid = valid[:1]` 在 `CRABPT_MAX_PER_CYCLE=1` 下
        #     会让队首坏封面**永久占掉唯一名额**（2026-10-07~10-10 实测）。
        #     简化回预截照样绿前六条，而队列又卡死。见 `selftest_crabpt_limit_enumerates`。
        ok7, _ = selftest_crabpt_limit_enumerates()
        # ★★ 第 8 条（2026-10-11 加，#13）：**发布前查重**的结构 + 运行时判据。
        #   ★ 独立失效模式：前七条都不查"**发布前有没有问站点'有没有'**"。
        #     删掉查重那一整段，`upload_torrent` 照发、站点照拒、日志看起来一样
        #     （都是"上传失败"）—— **完全静默**（这正是它必须被钉住的理由）。
        ok8, _ = selftest_dedup_before_publish()
        # ★★ 第 9 条（2026-10-14 加，`ERR-PUBLISH-20` 修复③）：**发种前版本一致性闸门**。
        #   ★ 独立失效模式：前八条都不查"**要发的这份，版本/文件集与盘上实况对不对得上**"。
        #     删掉闸门，`upload_torrent` 照发、站点照收，日志一片绿 —— 而那条会像 21662
        #     一样**永久校验不到 100%**（只能人工去站点删种），**完全静默**。
        ok9, _ = selftest_version_gate()
        # ★ 七条判据**都要**跑（`and` 而不是 `or`）—— 它们是**独立**的失效模式：
        #   ① 碰了乌托邦的列（"发一条污染四个判据"）
        #   ② 队列取错（"静默多发、不可撤销"）
        #   ③ ★ 队列口径与**解释口径**分叉（"工具告诉你它在队列里、实际不发"
        #      —— 2026-09-28 实测，`--only` 报文与实际筛选互相矛盾）
        #   ④ ★ 回加路里名字用在了绑定之前（"发布成功却永不做种"，`ERR-PUBLISH-02`）
        #   ⑤ ★ 失败不再分级 ⇒ 坏种子堵死队列（#21 复发，静默）
        #   ⑥ ★ 蟹黄堡那条路同样不再分级 ⇒ 坏种子堵死**转种**队列（#23 复发，静默）
        #   ⑦ ★ `--limit` 又变回"预截候选" ⇒ 队首坏封面永久占名额、队列不动
        #      （蟹黄堡 2026-10-07~10-10 三天零出种，静默）
        #   ⑧ ★ 删掉/改坏**发布前查重** ⇒ 重复种又照发（站点用"上传失败！"静默拒绝）
        #   ★ 用 `or` 的话，一条报红时另一条**根本没跑**，而"没跑"与"通过"
        #     在输出上应当长得不一样（`ERR-META-14`：兜底不许冒充真结果）。
        #   ★ 2026-10-11：修掉原先的 `ok7 and ok7` 笔误（应为 …and ok7），并接上 ok8。
        ok = ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and ok7 and ok8 and ok9
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
            # ★★ 唯一构造点 `qb_client.build`（`ERR-QB-08`）：优先 API Key，回退账号密码。
            import qb_client
            qb = qb_client.build(config)
        except Exception as e:
            print(f"⚠️  连不上 qBittorrent ({e})，本次不做回加")

    ok = fail = 0
    # ★★ 2026-10-04（#21）：区分"这条坏"与"站点崩" —— 前者跳过继续，后者立即停。
    #   · `rejected`：内容被站点拒（`REJECT:` 前缀 / 抛 SiteError）⇒ 标 FAILED 出队，发下一条；
    #   · `site_down`：站点侧问题（`SITE_DOWN:` 前缀 / 登录失效）⇒ **停**，非 0 退出让退避生效。
    #   ★ 为什么必须在循环外声明：`break` 之后要用它决定退出码（见函数末尾）。
    rejected = 0
    site_down = False
    # ★★ #13（2026-10-11）：查重**命中**（站点已有同版本）⇒ 关联而非重复提交的条数。
    dup_existing = 0
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
        #   ★★ 2026-10-04（#21）：`continue` 的**语义收窄了** —— 现在只有"这条坏
        #     （REJECT）"才 `continue`；"站点崩（SITE_DOWN）"改为 `break`（不再往下发）。
        #     ⇒ 这段间隔逻辑仍然放在循环开头，对所有继续走的人**必然**生效。
        if i > 1 and gap > 0 and last_upload_ts is not None:
            wait_publish_gap(gap, i, len(valid), last_upload_ts)

        title = task.get("formatted_title")
        print(f"\n[{i}/{len(valid)}] 🚀 发布: {title}")

        # ★★★ 发布前跨站查重（#13 / #11 核心，2026-10-11 加）——
        #   在**提交之前**问一次目标站点"有没有同版本"（判据 = 站侧 .torrent 的
        #   info.name 与本地 .torrent 的 info.name 精确相等）。
        #   ★ 放在这里（`client` 已建、循环体内、`upload_torrent` 之前）：
        #     查重要发搜索请求 ⇒ 不能放到 `:2573` 那段"零请求"的第二把钥匙区之前。
        #   ★★ 三态处置（与用户拍板一致）：
        #     · 命中 ⇒ **不提交**、关联已有页（`set_site_id EXISTS` + `site_publish_set`），
        #       计入 `dup_existing`，`continue` 下一条；
        #     · 未命中 ⇒ 落到下面**原样**提交（行为与从前逐字相同）；
        #     · 出错（`LoginExpired`/`SiteError`，含"搜索 HTTP≠200"）⇒ **停轮 + 非 0**
        #       （`site_down=True`），**绝不**把"搜不了"当"没重复"照发。
        if config.DEDUP_BEFORE_PUBLISH:
            _local_nm = _local_info_name(task)
            try:
                _hit = _dedup_lookup(client,
                                     search_name=title,
                                     local_name=_local_nm,
                                     site_label="乌托邦")
            except LoginExpired as e:
                print(f"    🛑 查重时登录失效，停止本轮（不标 FAILED）: {e}")
                site_down = True
                break
            except SiteError as e:
                # ★ 查重**本身**失败（搜索打不开 / 超时）⇒ 停轮非 0，让退避生效。
                #   这是用户明确拍的："查重搜索失败 ⇒ 停轮非 0"（安全侧）。
                print(f"    🛑 查重失败，停止本轮（不标 FAILED —— 这是站点侧问题）: {e}")
                site_down = True
                break
            if _hit:
                # 站点已有同版本 ⇒ 把本地行**关联**到已有页，**不再重复提交**。
                # ★ 复用成功路径的写库形状（`set_site_id` + `site_publish_set`），
                #   但 reason 标 `dup_existing` 以便事后能分辨"是发的还是查出来的"。
                db.set_site_id(task["info_hash"], _hit, site_status="EXISTS",
                               status="PUBLISHED",
                               reason=f"dup_existing:{_hit}")
                db.site_publish_set(task["info_hash"], db.SITE_MUSOPIA, _hit,
                                    status="EXISTS",
                                    cover_url=(task.get("cover_url") or None))
                db.mark_synced(task["info_hash"])
                dup_existing += 1
                print("    ✅ 已关联到站点已有页（未重复提交）。")
                continue

            # ★★★ 提交**前**的同名闸门（`S4` / `ERR-PUBLISH-25`）——
            #   查重回答"有没有同一版本"；本闸门回答"有没有**同名的别的东西**"。
            #   ★ 站点对后者的处置是 HTTP 200「上传失败！」（零线索），
            #     而我当时据此猜了三轮字段、全错 ⇒ 现在改成**事先问清楚**。
            #   ★ 命中 ⇒ **拒发 + 标 FAILED 出队**（同"被拒"处置）：不可逆动作前的
            #     一声"停"，代价只是少发一条且**当轮可见**；猜错的代价是不可逆。
            _sn = _same_name_precheck(client, search_name=title,
                                      local_name=_local_nm, site_label="乌托邦")
            if _sn:
                _why = (f"同名冲突：站点上已有 info.name 逐字相同的条目 "
                        f"{'、'.join(t for t, _ in _sn[:3])} ⇒ 提交必被「上传失败」拒。"
                        f"处置：先在站点删掉那条旧页，再重发（`tools/republish_one.py`）")
                print(f"    🛑 同名闸门不通过 ⇒ 拒发：{_why}")
                fail += 1
                rejected += 1
                db.save_task(info_hash=task["info_hash"], status="FAILED",
                             reason=f"same_name_conflict: {_why}")
                continue

        # ★★★ 发种前**版本一致性闸门**（2026-10-14，`ERR-PUBLISH-20` 修复③）——
        #   放在查重之后、`upload_torrent` 之前（查重会发请求，本闸门纯本地零请求）。
        #   ★ 不通过 ⇒ **拒发**：标 FAILED 出队（同"被拒"的处置，队列自然推进），
        #     绝不把"版本对不上"的种发出去（那正是 21662 只能人工删种的根因）。
        if config.VERSION_GATE_BEFORE_PUBLISH:
            _vg_ok, _vg_why = _version_consistency_gate(task)
            if not _vg_ok:
                print(f"    🛑 版本一致性闸门不通过 ⇒ 拒发：{_vg_why}")
                fail += 1
                rejected += 1
                db.save_task(info_hash=task["info_hash"], status="FAILED",
                             reason=f"version_gate: {_vg_why}")
                continue

        fields = descr.build_form_fields(merge_mapped(task),
                                         anonymous=args.anonymous)
        last_upload_ts = time.time()
        try:
            torrent_id, msg = client.upload_torrent(task["torrent_file"], fields)
        except LoginExpired as e:
            # ★★ 登录失效 = 站点侧问题（Cookie 过期）⇒ **立即停**，不再往下发。
            #   不标 FAILED —— 这条种子本身没问题，是站点的问题；标了会**误杀**。
            print(f"    🛑 登录失效，停止本轮（不标记本条，稍后重试）: {e}")
            site_down = True
            break
        except SiteError as e:
            # ★ 其它 SiteError（种子文件不存在 / 读不出等）= 这条的问题 ⇒ 跳过、发下一条。
            print(f"    ❌ {e}")
            fail += 1
            rejected += 1
            db.save_task(info_hash=task["info_hash"], status="FAILED",
                         reason=f"publish_failed: {e}")
            continue

        if not torrent_id:
            # ★★ 按 `upload_torrent` 给出的**机器可读前缀**分流（#21 的核心判据）：
            #   · SITE_DOWN: ⇒ 站点侧 ⇒ 停 + 非 0（让退避生效，绝不连打整条队列）；
            #   · REJECT:    ⇒ 这条被拒 ⇒ 标 FAILED 出队、发下一条。
            if isinstance(msg, str) and msg.startswith("SITE_DOWN:"):
                print(f"    🛑 站点侧问题，停止本轮（不标记本条）: {msg}")
                site_down = True
                break

            # 到这里 = 这条被拒（REJECT 或未知）。
            # ★★ 标 FAILED 是**关键机制**（不是顺手写库）：
            #   `PENDING_PUBLISH_WHERE` 首条件是 `status='SUCCESS'`（db.py:347）
            #   ⇒ 标 FAILED **立刻出队** ⇒ 下一轮 `get_pending_publish()` 不再返回它
            #   ⇒ 队列自然推进，不会每条都卡在同一条上。
            #   `FAILED ∈ PROTECTED_STATUSES` ⇒ scan 重扫不会把它冲回 SUCCESS。
            #   （这与 2026-10-04 应急时手动做的那步是**同一动作**，此处自动化。）
            # ★★★（`S4` / `ERR-PUBLISH-25`）**先诊断再落库**：
            #   站点那句「上传失败！」对**字段错**与**同名冲突**用的是**同一句话**，
            #   一律记成 `publish_failed: …` 等于把两种相反处置的分诊线索丢在日志里。
            #   ⇒ 落库 reason 必须写明**我们判断是哪一类**，否则下一个看日志的人
            #     （很可能是我）又会顺着文案去猜字段（`ERR-PUBLISH-25` 记了三轮全错的经过）。
            _diag_why = _diagnose_upload_reject(msg, local_name=_local_nm)
            print(f"    ❌ 发布被拒（本条标 FAILED 出队，继续下一条）: {msg}")
            print(f"       ↳ 分诊：{_diag_why}")
            fail += 1
            rejected += 1
            db.save_task(info_hash=task["info_hash"], status="FAILED",
                         reason=f"publish_failed[{_diag_kind(msg)}]: {msg} | 分诊:{_diag_why}")
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
    print(f"🎯 发布完成：成功 {ok} 条，失败 {fail} 条"
          f"（其中被拒标 FAILED {rejected} 条）"
          + (f"，查重命中关联 {dup_existing} 条（未重复提交）" if dup_existing else "")
          + ("；⚠️ 站点侧问题已中止本轮" if site_down else ""))

    # ★ 说真话的退出码（2026-09-18；2026-10-04 按 #21 **重划判据**）。
    #
    # daemon 判一个阶段成败**只看退出码**（`daemon.py:620` 的 `if code == 0`），
    # 非 0 ⇒ `fails[name]++` ⇒ **指数退避**（`daemon.py:992`，最长 6h）。
    #
    # ★★ #21 的核心：原判据是"只要有失败就非 0"，于是 **一条坏种子 → 整条队列陪绑退避 2~6h**。
    #   现在按**失败的性质**分层（两类处置相反）：
    #
    #   ① `site_down`（站点崩 / Cookie 失效）⇒ **非 0**：这是**该退避**的情形
    #      —— `daemon.py:14-15` 明写「Cookie 过期这类问题会让每个阶段都失败，不退避就是
    #      每 30 分钟无意义地敲站点一次」。且我们已 `break`，**没有**连打整条队列。
    #
    #   ② 有 `rejected` 但**也有成功** ⇒ **0**：坏种子已逐条标 FAILED 出队，
    #      队列在推进、站点也证明是好的 ⇒ 退避只会**拖慢真正的发布**，不该惩罚一次成功的"清障"。
    #
    #   ③ ★ **保守护栏**：`ok == 0 且 fail > 0`（一条都没成、全是 REJECT）⇒ **非 0**。
    #      理由：整批全被拒**不像**"N 条恰好都是坏种子"，更像**判据误伤**（站点改版把
    #      所有条都拒了 / 落盘前缀判错）⇒ 让它退避 + 日志把它**暴露**出来。
    #      （`ERR-META-01`：别把系统性故障静默成"N 条坏种子"。）
    #
    #   ④ 失败一条都没有 ⇒ 0，正常。
    #   ⑤ 一条都没试（没待办 / 全被拦 / dry-run / 用户取消）⇒ 0，这些**不是**失败。
    #
    # ★ 只退出码，**不 sys.exit(msg)** —— 上面那句汇总已经在 stdout 里了，
    #   再往 stderr 打一遍是重复信息（且容器里两条流都会进 docker logs）。
    if site_down:
        print("↩️  以非 0 退出：站点侧问题（已中止本轮），让 daemon 的退避生效")
        sys.exit(1)
    if fail and ok == 0:
        # ③ 系统性失败护栏 —— 退避 + 日志暴露，别静默成"N 条坏种子"。
        print(f"↩️  以非 0 退出：{fail} 条失败、**无一成功** —— 疑似系统性故障"
              f"（判据误伤 / 站点改版？）而非「恰好都是坏种子」，让退避暴露它")
        sys.exit(1)
    if rejected:
        # ② 清障成功 ⇒ 0。**不退避**（否则会拖慢真正的发布）。
        print(f"ℹ️  退出码 0：{rejected} 条坏种子已标 FAILED 出队"
              f"（队列已推进），其余 {ok} 条成功 ⇒ **不**触发退避")


if __name__ == "__main__":
    main()
