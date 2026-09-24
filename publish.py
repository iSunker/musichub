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
                    help="只发布这些 info_hash（前缀即可，逗号分隔）。"
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


def run_crabpt(args):
    """蟹黄堡（crabpt.vip）发布路径。★ 与 `main()` 的乌托邦路径**并列**，不互相调用。

    ★★★ 为什么**不**共用 `main()` 那个循环：见 `parse_args()` 里 `--site` 的注释
      —— 共用的是**不变量**（都在本函数里逐条照做），而不是**字段与提交**。
      本函数里属于"不变量"的每一处都**明写了它对应 `main()` 的哪一段**，
      将来改一边就能立刻看出另一边要不要跟。

    ★★ 本函数**没有**接进 daemon（`daemon.py` 的 publish 阶段仍只调
      `publish.py --yes --auto`，即默认的 musopia）。这是**刻意的**：
      在"发一条、回读一条"验证通过之前，不让它进入每 30 分钟跑一次的无人值守路径。
      接线是下一步，且那时要同时决定 `PUBLISH_CATEGORY` 与 qB 做种路径。
    """
    import crabpt_site

    print("=" * 60)
    print("🌐 站点 = 蟹黄堡（crabpt.vip）★ 与乌托邦完全独立的一条路径")
    print("=" * 60)

    # ---- ★★ 两个安全护栏：先立起来，再干别的 ----
    #
    # ① `--only` **必须**有（与乌托邦不同：那边是可选的）。
    #
    #   ★ 理由：乌托邦已经发过 254 条、常规批量发布是**它的日常**；
    #     蟹黄堡**一条都没发过** —— 第一条的意义是"验证字段层到底对不对"，
    #     而"发什么"必须由人**点名**，不能由 SQL 的排序决定。
    #   ★ 这正是 `--only` 当初被加进来的理由（见它的 help）：**发布不可逆**，
    #     所以"发十条"只能是"点名的十条"，不能是"队列前十条"。
    #     ⇒ 对这条**从未验证过**的路径，把"可选"收紧成"必须"。
    if not args.only:
        sys.exit("❌ --site crabpt 必须带 --only <info_hash 前缀>：\n"
                 "   ★ 蟹黄堡这条路**一次都没跑过**，第一条必须由人点名，\n"
                 "     不能是「队列里排序最靠前的那条」（发布不可逆，无批量撤销）。\n"
                 "   ⇒ 例：python publish.py --site crabpt --only 3f2a91c8… "
                 "--limit 1 --yes")

    # ② ★ **硬杀只读闸门** —— `--dry-run` 是本函数唯一会自己加上的 flag。
    #
    #   ★ 为什么要"硬杀"而不是"提示用户别忘"：
    #     它**只影响这一条路径**（乌托邦那边一个字都不改），且方向**单向**
    #     ——（关掉 dry-run = 真提交，**不可逆**）。
    #     一个"不可逆动作的开关"如果默认是关的（= 会真发），那就等于没有开关。
    #   ★ 反过来"不小心发了一次"是**不可逆**的 ⇒ 宁可多一步显式确认。
    if not args.dry_run:
        print("\n★★ 安全默认：`--site crabpt` **强制先走 dry-run**。")
        print("   本次**不会**向蟹黄堡提交任何数据（只是把表单字段与来源打出来）。")
        print("   ★ 看过字段没问题之后，去掉 `--dry-run` 再跑一次才会真发布。")
        args.dry_run = True
    else:
        print("\n★ 显式 dry-run：不会向站点提交任何数据。")

    # ---- 闸门与队列：★ 与 main() 同一份判据，不手抄 SQL（ERR-META-03）----
    tasks = db.get_pending_publish()
    if not tasks:
        print("💡 当前没有需要发布的任务（`PENDING_PUBLISH_WHERE`）。")
        return

    want = tuple(s.strip().lower() for s in args.only.split(",") if s.strip())
    hit = [t for t in tasks
           if any((t.get("info_hash") or "").lower().startswith(w) for w in want)]
    in_queue = {(t.get("info_hash") or "").lower() for t in tasks}
    absent = [w for w in want if not any(h.startswith(w) for h in in_queue)]

    print("\n" + "=" * 60)
    print(f"★ --only：点名 {len(want)} 个 ⇒ 命中队列 {len(hit)} 条")
    print("=" * 60)
    for t in hit:
        print(f"   ✅ {(t.get('info_hash') or '')[:12]}  {t.get('formatted_title')}")
    for w in absent:
        print(f"   ❌ {w} 不在待发布队列里（被闸门拦下 或 hash 打错）")
    if absent:
        # ★ 同一份判据（`explain_not_queued`），不另写一套。
        #   ★ 蟹黄堡最可能撞上的是闸门④：皇后的 `transfer_rule`
        #     —— 我们在蟹黄堡发之前**必须先确认皇后允许转载**。
        print("\n   ★ 逐条查为什么没进队列（同一份判据，不做手抄）：")
        for w in absent:
            print(f"      {w}: {explain_not_queued(w)}")
    if not hit:
        sys.exit("❌ --only 与队列**零交集** ⇒ 拒绝执行（不会去发队列里的别的）")

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
    # ★★ 蟹黄堡这条路**再加一道**：一次最多 1 条。
    #
    #   ★ 这不是"保守"，是**不可逆操作的正确默认**：这条路**从未验证过**
    #     （字段层是照实测表单写的，但"提交之后站点上长什么样"没人看过）。
    #     第一条发出去、回读确认之后，才谈得上第二条。
    #   ★ 想多发就得显式改这个数 —— 而改它的人必然看到这段注释
    #     （与"一个能靠多打一个词关掉的保险不是保险"同一个思路，
    #       这里反过来：**能靠多打一个词放开的上限，至少得让人问一句为什么**）。
    if len(valid) > 1:
        print(f"\n★★ 蟹黄堡路径一次只发 1 条（本次点名命中 {len(valid)} 条 "
              f"⇒ 只发第一条）。★ 理由：这条路从未验证过，发布不可逆。")
        print("   ⇒ 若确实要发多条，请逐条点名、逐条确认。")
        valid = valid[:1]

    # ---- 提交前：读发布页（★ 唯一能在提交前发现"没有发种权限"的判据）----
    client = crabpt_site.CrabptClient()
    if not client.have_cookie():
        sys.exit("❌ CRABPT_COOKIE 是空的 ⇒ 拒绝提交。\n"
                 "   ★ 必须在 **NAS 的 .env** 里加（`deploy.sh` 的白名单不含 `.env`，"
                 "见 ERR-ENV-02），然后 `sudo docker compose up -d --build`。")

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
    # ★ 落库（与 main() 同一对调用）。★ 只说这次真的做了什么、没做什么：
    #   下面这三句**不写进 reason**（reason 是库里的字段，落进去就成了"事实"，
    #   而这三件都是**已知缺口**）—— 缺口记在 PLAN-crabpt.md 附录 D.5。
    db.set_site_id(t["info_hash"], tid, site_status="EXISTS",
                   status="PUBLISHED", reason=f"crabpt_published_id:{tid}")
    db.mark_synced(t["info_hash"])
    print("    ℹ️  未做（已知缺口，见 PLAN-crabpt.md 附录 D.5）："
          "technical_info 留空 · 日韩拆不出 · 不回加 qB（蟹黄堡尚未接线）")

    _crabpt_readback(client, tid, fields)
    print("\n★ 下一步（人工）：打开详情页逐字段复核，"
          "并**顺便把 `takeedit.php?id=%s` 采下来** ——" % tid)
    print("  那条编辑页是先前采集计划里**唯一**拿不到、"
          "而现在已经可达的东西（先得自己发过一条）。")
    print("★ 它同时是回填 `PLAN-crabpt.md` 附录 C.6 的依据。")
    return


def main():
    args = parse_args()

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
