"""
统一配置模块。

设计原则：
1. 所有密钥/连接信息只在这里定义一次，run.py / publish.py / update_metadata.py
   一律 `import config` 使用，不再各自重复写一份默认值。
2. 真正的密钥（密码、Cookie、API Key）绝不在源码里给"看起来能跑"的真实默认值——
   缺失就直接终止程序，逼你在 .env 里显式配置，而不是悄悄拿一个可能已经过期/
   泄露的旧值继续跑。
3. 状态目录（数据库、生成的种子/JSON）用统一的 DATA_DIR，与代码目录、
   与"到底是哪个 docker service 在跑"完全解耦，避免出现两个容器各自
   挂载不同宿主机路径、导致状态对不上、任务被重复处理的问题。
"""
import os
import sys

# ★★ 全局网络出口：import 即给 urllib3 打上"只解析 IPv4"的补丁
#   （绕开"个别 Cloudflare IPv6 边缘 IP 不可达"的黑洞，见 net.py 文件头）。
#   ★ 放这里而不是每个网络模块各加一行：全项目 33 处 import config ⇒ 一处生效全覆盖。
#   ★ 必须在 `requests` 发第一个请求**之前**生效 —— import 时执行正是为此。
#   ★ net 不 import config、不读 env ⇒ 不影响下面 require_env / 各脚本的 --selftest。
import net  # noqa: F401  (导入即产生副作用：给 urllib3 打 IPv4 补丁)


def require_env(key: str) -> str:
    """读取必须存在的环境变量；缺失就直接终止程序。"""
    val = os.getenv(key)
    if not val:
        sys.exit(f"❌ 缺少必需的环境变量 {key}，请检查 .env 文件（可参考 .env.example 创建）")
    return val


# ==================== qBittorrent ====================
# ★ 兜底默认值**必须**与 compose 三处逐字一致：`docker-compose.yml` 的两个 service
#   （`common` 与 `once`）都写 `${QB_HOST:-<同一串>}`，且它们都是 `network_mode: "host"`。
#   ★★ 2026-10-07（#92）：默认从 3020(opencd) 改为 3040 —— 当时发种落在 SSD 的 publish 实例。
#   ★★★ 2026-10-18（C1，`ERR-META-33`）：生产 `.env` 后来把发种实例挪到 **3010(qbit-publish)**，
#     `.env` 改了，但**这三处兜底没跟着改**（还写着 3040）⇒ 一旦 `.env` 漏了这一行
#     （新环境 / 重写 / 重建），代码**不报错**，直接连 3040 那个**别的实例**——
#     症状是"发错实例 / 查不到源"，而不是"配置错误"。现已三处统一为 **3010**。
#   ★★ 回滚语义（不再是"改回 3020"）：本键语义是"**发种实例**"，
#     回滚 = 把它改回**当时那个发种实例**的端口，并**同时**改 compose 两处 + 下面 QB_HOST_DEFAULT。
#   ★★ 不要改回 `host.docker.internal` —— **host 网络下没有那条特殊 DNS 记录**
#      （compose 里紧挨着的注释也是这么写的），它只在 bridge 网络里存在。
#   这里这个值平时用不到（compose 总会把 QB_HOST 传进来），但一旦有人不经 compose
#   直接 `python run.py`，旧的 `host.docker.internal` 会给出一个**看起来像配置问题、
#   其实是"这条域名根本不存在"**的报错。改成与 compose 同形，错了也错得一致。
QB_HOST_DEFAULT = "http://127.0.0.1:3010"
# ★★★ 2026-10-18（C1）：`QB_HOST=""`（compose 里写成 `QB_HOST=` 或 .env 留空）**必须**也算用兜底。
#   ★ 为什么不能直接 `os.getenv("QB_HOST", QB_HOST_DEFAULT)`：`os.getenv` 的默认值**只在键
#     完全不存在时**生效；键存在但值为空串 ⇒ 返回 `""` ⇒ `QB_HOST` 变成空串 ⇒
#     `qb_client.build` 里 `Client(host="")` 会去连 **localhost 默认端口**，
#     而日志却显示"读到了 .env" ⇒ 又一个静默错连（本条目就是治这个病的）。
#   ★ 本项目其它键用的是 `env_int`/`env_bool` 的"空串=未设"口径（见 `daemon.py`），此处对齐。
_QB_HOST_RAW = (os.getenv("QB_HOST") or "").strip()
QB_HOST_FROM_DEFAULT = not _QB_HOST_RAW
QB_HOST = _QB_HOST_RAW or QB_HOST_DEFAULT
# ★★ 2026-10-18（C1）：**"用了兜底"必须可观测** —— 这是 `ERR-META-33` 的判据原话：
#   "把 `.env` 里该键删掉后启动，日志或巡检里应能**看出**它在用兜底，而不是静默照跑"。
#   ★ 为什么不是"改对默认值就完事"：默认值改了以后，将来再挪实例**又会**漂一次
#     （本次就是同一个错漂了 11 天）。只有"用兜底会在日志里喊出来"才是**结构性**的护栏。
#   ★ 消费方：`daemon.preflight()` 启动时打印、`daemon.ENV_SNAPSHOT_KEYS` 收 `QB_HOST` 值。
# ★★★ 2026-10-10（#16）：**皇后源实例**（opencd 分类的原种/数据所在）的地址 —— 第二个真值来源。
#   ★ 为什么**必须**是独立一个键（本 bug 的根因，见 ERRATA 的 ERR-ORCH-02）：
#     `QB_HOST` 被**两个语义不同的消费者**共用 ——
#       · **scan 阶段**（`run.py`）：读皇后的 `opencd` 分类 ⇒ 必须连**皇后源**（生产 = 3260 qbit-music3）；
#       · **发种回加**（`publish.py` 的 `readd_to_*`）：把发出去的种加回**发种实例**
#                （生产 = 3010 qbit-publish；见本文件上方 `QB_HOST` 那段）。
#     单容器重构时把旧的 `QB_HOST_SCAN`/`QB_HOST_PUBLISH` 双钥匙删掉了，注释还写"现已无任何
#     代码读取"——但 scan 的源**没有**消失。于是 .env 把 `QB_HOST` 从 3040 改成 3010（给发种用）
#     时，**scan 跟着一起被带偏**：它去 3010（发种实例，0 个 opencd）找皇后种子 ⇒ 每轮
#     「找到 0 个分类为 [opencd] 的种子」⇒ **队列只出不进、见底** ⇒ 站点"不发种"。
#   ★★ 生产 `.env` 里 `QB_HOST_OPENCD=http://127.0.0.1:3260` ⇒ 本键 = 3260。兜底也给 3260
#      （与 `publish._opencd_content_host()` 同源同名，语义 = "皇后源实例"）。
#   ★★★ 它**同时**是交棒链路的口径来源：`publish._opencd_content_host()` 与
#      `daemon.HANDOFF_HOST_3020` 都优先读本键（旧名 `QB_HOST_OPENCD` 在 .env 里一字不改）。
#   ★ 它**不是凭据**，可打印、可进 daemon env 快照（白名单 `ENV_SNAPSHOT_KEYS` 已含本键名）。
QB_HOST_OPENCD = os.getenv("QB_HOST_OPENCD", "http://127.0.0.1:3260")
QB_USER = os.getenv("QB_USER", "admin")
QB_PASS = require_env("QB_PASS")

# ★★ qB API Key（2026-10-04 加，见 `qb_client.py` 与 `ERR-QB-08`）。
#   ★ 为什么：qB 5.x 对**登录接口**做失败计数 ⇒ 用户名/密码错几次就**封 IP**
#     （「身份认证失败次数过多」，HTTP 403）。而本项目原先有 13 处各自
#     `auth_log_in()` ⇒ **重试本身就是把 IP 推向封禁的力量**。
#     API Key 走 `Authorization: Bearer`，**不碰登录接口** ⇒ 不再会被自己的重试封掉。
#   ★★ 用 `os.getenv`（**可选**）而**不是** `require_env`：
#     与 `CRABPT_COOKIE` 同款 —— 可选键缺失**不许**让整个 `import config` 挂掉
#     （那会把"缺一个可选键"升级成"全站不可用"）。留空 ⇒ `qb_client.build` 回退到
#     用户名/密码（老行为，一字不改）。
#   ★★★ 这是**凭据**：绝不打印、绝不进 daemon 的 env 快照
#     （`daemon.ENV_SNAPSHOT_KEYS` 是**白名单**制 ⇒ 本键默认不外溢，同 `LSKY_TOKEN` 的纪律）。
QB_API_KEY = os.getenv("QB_API_KEY", "")

# ★★★ 2026-10-07（#129/#92-g）：**3020(opencd)** 自己的 API Key —— 交棒链路专用。
#   ★ 为什么必须**再开一个键**（实测踩出来的，不是洁癖）：
#     `QB_API_KEY` 现在指 **3040**（发种实例，见 `QB_HOST`）。而交棒要连 **3020** 查原种 /
#     加种 —— 若复用 `QB_API_KEY`，就会**拿 3040 的 key 去打 3020** ⇒ 3020 回 **403**，
#     整条交棒链在"定位 HDD 目录"那一步就静默失败（_resolve_opencd_content_path 返回 ""）。
#   ★★ qB 5.2.4 实测铁律（见 NAS-92g）：API Key 必须是 `qbt_`+28 位 = **32 位总长**；
#     36 位的 key 能存进 conf，但 `Authorization: Bearer` 会 **403**。
#   ★ 留空 ⇒ 3020 客户端**回退用户名/密码**（`config.QB_USER`/`QB_PASS`——两台机器同账号即成立）。
#     ⇒ 老部署不填这个键也**不会更坏**（回退路径本就在）。
OPENCD_QB_API_KEY = os.getenv("QB_API_KEY_OPENCD", "")

OPENCD_CATEGORY = os.getenv("OPENCD_CATEGORY", "opencd")
# ★★ 皇后种识别口径 —— 2026-10-11 从 category 改为 **tracker 主机名**（#10/#172）。
#   ★ 为什么弃 category：生产 3260（皇后源）1433 个种里**只有 6 个**带 `opencd` 分类
#     ⇒ 用 category 会漏掉所有不带分类的新皇后种（`ERR-ORCH-03`）。
#   ★ 为什么是 tracker：`tracker=tracker.open.cd` 覆盖 **1292** 个（最全、语义最准
#     = "这个种来自皇后"）；该实例的 qB tag 实际叫 `tracker.open.cd` 而**不是** `opencd`。
#   ★ 值是 **tracker 主机名**（不含 `https://`、不含路径），匹配用精确相等（见
#     `run._torrent_is_opencd`）。换域名时改这个键即可，不动代码。
OPENCD_TRACKER = os.getenv("OPENCD_TRACKER", "tracker.open.cd")
PUBLISH_CATEGORY = os.getenv("PUBLISH_CATEGORY", "Musopia")

# ---- ★★ 发布前跨站查重（#13 / #11 核心，2026-10-11 加）----
# ★ 干什么：发布**之前**先在目标站点搜一遍，比对站侧 `.torrent` 的 `info.name`
#   与本地 `.torrent` 的 `info.name`；**相等 ⇒ 站点已有同版本** ⇒ 不重复提交，
#   改为把本地行**关联**到已有站点页（musopia 写 site_publish tid + EXISTS；
#   crabpt 只写 site_publish）。
# ★★ 为什么必须有它（根因）：pipeline 原先**只信本地 `site_status`**、从不问
#   目标站点"有没有" ⇒ 同一专辑以不同 tid/路径再入库时就发重复种 ⇒ 站点用
#   **不透明的**「上传失败！」（正文零消息）拒绝 ⇒ 近 15 条 FAILED 里 ≈11 条是重复。
# ★★★ 默认 **开**（用户 2026-10-11 拍板）。为什么默认开是安全的：
#   · 它**只多读、不多写** —— 查重不命中就**照旧提交**（行为与从前逐字相同）；
#   · 命中才改动作（关联而非重复提交）—— 那正是我们要的；
#   · 查重**搜索本身失败** ⇒ 停轮 + 非 0（`site_down` 通道，触发 daemon 退避），
#     **绝不**把"搜不了"静默降级成"没重复"（否则又会发出一条重复种）。
# ★ 要回旧行为：`.env` 里显式设 `DEDUP_BEFORE_PUBLISH=false`（如站点搜索接口
#   临时不可用、你想临时绕开时）。
DEDUP_BEFORE_PUBLISH = (os.getenv("DEDUP_BEFORE_PUBLISH", "true").strip().lower()
                        not in ("0", "false", "no", "off"))

# ---- ★★ 发种前**版本一致性闸门**（2026-10-14，`ERR-PUBLISH-20` 修复③）----
# ★ 干什么：`upload_torrent` **之前**，纯本地查两件事，任一不符即**拒发**（标 FAILED）：
#     ① DB 记的 `track_type` vs **现在**从本地目录重算的版本（库 vs 盘上实况）；
#     ② 本地 `.torrent` 的文件集 vs 本地内容目录的文件集（要发的 ≠ 盘上的）。
# ★★ 为什么必须有（根因，21662）：本地目录**实际是分轨**，却因一个**指向不存在
#   WAV 的 .cue**被旧判据当成整轨 ⇒ 站点标题挂错版本 ⇒ 站点那条永久校验不到 100%、
#   只能人工去删种（`#136` 族第 3 次）。这道闸门让**发之前**就拦住这种"版本对不上"。
# ★★★ 默认 **开**（用户 2026-10-14 拍板，安全侧）。为什么默认开是安全的：
#   · 它**零网站请求**（只读盘 + 读本地种子）；
#   · 一致性 ⇒ **照旧提交**（行为与从前逐字相同）；
#   · 一致性**比不了**（目录不在 / 种子读不出 / 一侧为空）⇒ **不拦**，显式打印"跳过"
#     —— 绝不让"不可知"变成"拒发"（同查重纪律，`ERR-META-14`）。
# ★ 要回旧行为：`.env` 里显式设 `VERSION_GATE_BEFORE_PUBLISH=false`。
VERSION_GATE_BEFORE_PUBLISH = (
    os.getenv("VERSION_GATE_BEFORE_PUBLISH", "true").strip().lower()
    not in ("0", "false", "no", "off"))

# ---- ★★（#23，2026-10-11）查重「召回」子查询上限 ----
# ★ 为什么需要它：#13 初版**只发一条**搜索串（`formatted_title`），而站点搜索是
#   **AND + 对标点敏感**（对「显示标题」匹配）⇒ 长名/带标点的标题**整串返回 0 候选**
#   ⇒ **假阴性** ⇒ 站点上明明有、却当"没有" ⇒ 照旧提交 ⇒ 又一条重复种
#   （实测：3 条已知重复里只有 1 条单串能召回；CJK 长名任何单串都 0 候选）。
# ⇒ #23 改为按 `musopia.SEARCH_QUERY_PLAN` 发**多条子查询**；#31 起**精准优先 + 命中即停**
#   （高精度短串在前、命中就返回，宽查询不发）。
# ★ 值 = **每条专辑**最多发几次搜索请求。**默认 5**。
# ★★ 召回**宽**只多花请求，**不会**制造假阳性 —— "是不是同一版本"由
#   `site_verify.compare_hosted_vs_db` 的**精确** `info.name` 相等判定。
#   ★ 但"宽"会经**预算截断**制造**假阴性**（`ERR-PUBLISH-17`）⇒ 所以**顺序承重**
#     （`SEARCH_QUERY_PLAN` 必须精准优先）；本值调大是 R1 回归（head/tail 窗口被截掉）的首选缓解。
# ★ `<=0` ⇒ 只发 1 条（= #13 初版行为，**不回退** 到"不查重"）—— 站点搜索接口
#   吃紧时可用它临时收窄；真要整条关掉用上面的 `DEDUP_BEFORE_PUBLISH=false`。
# ★ 候选验证另有**全局**上限 `DEDUP_MAX_CANDIDATES`（下面那条）。
try:
    DEDUP_MAX_SEARCH_QUERIES = int(float(os.getenv("DEDUP_MAX_SEARCH_QUERIES", "5")))
except ValueError:
    DEDUP_MAX_SEARCH_QUERIES = 5

# ---- ★★（#31，2026-10-11）候选验证**全局**上限 ----
# ★ 为什么需要它：#31 把 `search_area` 0→1（最宽、唯一能召回**中文艺术家**），
#   但 area=1 让候选数**暴涨**（实测 `Etre` 21→109、`徐元白` 0→2）——
#   若候选预算**太小**，真目标页会在**验证前**被截断 ⇒ 反而**漏召回**（实测
#   area=1 + 预算 8 = 11/20，比旧 area=0 的 13/20 **更差**）。
# ★★ 但"调大预算"**治不了本**：噪声随宽召回**线性**增长（要加到几百条才够，不可接受）
#   ⇒ 正解是**精准优先排序 + 命中即停**（`search_compare`），预算只是**安全阀**。
#   故默认**保持 16**（够用），**不**靠调大它来救召回。
# ★ 语义：对每条专辑，**跨全部子查询累计**最多**验证**几个候选（每个 = 1 次 details + 1 次下载）。
# ★ 两站**同源**：乌托邦与蟹黄堡的 `find_existing_by_name` 都默认 `max_candidates=None` 读本键
#   （蟹黄堡旧版硬编码 8 = 与 area=1 错配的 11/20 配置，已于 #31 修正）。
# ★ 护栏性质：**不是**运维旋钮——调大只多花请求、**不会**假阳性；但仍需**有界**（挡超常见 token 扇出）。
try:
    DEDUP_MAX_CANDIDATES = int(float(os.getenv("DEDUP_MAX_CANDIDATES", "16")))
except ValueError:
    DEDUP_MAX_CANDIDATES = 16

# ==================== Musopia 站点 ====================
MUSOPIA_URL = os.getenv("MUSOPIA_URL", "https://www.musopia.vip")
MUSOPIA_COOKIE = require_env("MUSOPIA_COOKIE")
MUSOPIA_PASSKEY = require_env("MUSOPIA_PASSKEY")

# ==================== 蟹黄堡站点（第二发布目标，2026-09-24 加）====================
#
# ★★ 为什么用 `os.getenv` 而**不是** `require_env`：
#   `require_env` 在缺失时 `sys.exit`（见文件头）⇒ 若这里也 require，
#   **只要配了蟹黄堡这一个新站，乌托邦那条老链路就会在本机直接起不来**
#   （本机 `.env` 里没有 CRABPT_COOKIE，见 ERR-ENV-02：新增键要在 NAS 手工加）。
#   ⇒ 老站**必须继续 require**（缺了就是真故障），新站**允许暂时缺**（还没启用）。
#   ★ 真要用它时（`publish.py --site crabpt`）由 `crabpt_client()` 检查并给出**明确报错**，
#     而不是让整个 `import config` 挂掉 —— 那是"缺一个可选键"升级成"全站不可用"。
CRABPT_URL = os.getenv("CRABPT_URL", "https://crabpt.vip")
CRABPT_COOKIE = os.getenv("CRABPT_COOKIE", "")
# ★ 音乐分类 id（实测 `type=408`，见 PLAN-crabpt.md 附录 C.1）
CRABPT_MUSIC_CATEGORY = os.getenv("CRABPT_MUSIC_CATEGORY", "408")
# ★ 蟹黄堡的 qB 分类：★ 与乌托邦**分开**，这样"哪一站的种子"在 qB 里一眼可辨，
#   也便于按站统计（本项目已有 PUBLISH_CATEGORY=Musopia）。
CRABPT_PUBLISH_CATEGORY = os.getenv("CRABPT_PUBLISH_CATEGORY", "CrabPT")

# ==================== 猫站（PTerClub，第三发布目标）====================
# ★★ 为什么用 `os.getenv` 而不是 `require_env`：
#   老链路不需要这个键，不能让乌托邦/蟹黄堡因缺键而起不来。
#   真要用它时（`publish.py --site pter`）由 `PterClient` 检查并给出明确报错。
PTER_URL = os.getenv("PTER_URL", "https://pterclub.net")
PTER_COOKIE = os.getenv("PTER_COOKIE", "")
# ★ 音乐分类 id（实测 type=406，见 PTNexus pterclub.yaml）
PTER_MUSIC_CATEGORY = os.getenv("PTER_MUSIC_CATEGORY", "406")
# ★ 猫站的 qB 分类
PTER_PUBLISH_CATEGORY = os.getenv("PTER_PUBLISH_CATEGORY", "PTer")
# ★ 猫站图床配置（标准 Chevereto API，留空则自动退回通用公网图床/已缓存封面）
PTER_IMG_URL = os.getenv("PTER_IMG_URL", "https://img.pterclub.net/api/1/upload")
PTER_IMG_API_KEY = os.getenv("PTER_IMG_API_KEY", "")

# ---- ★ 延迟回加 qB（2026-10-03 加）---------------------------------------
#
# ★ 干什么：种子**发布成功后**，**不立即**加进 qB 做种，而是等 N 分钟再加。
#   目的（用户 2026-10-03）：别人 RSS 到这条种子有时差，延迟一会儿让**尽量多
#   的人同时开始下载**（=扩散更广）。★ 15 是用户暂定的值，**没有实测**哪种最合适。
#
# ★ 怎么实现：`publish.py` 发布成功后把「待回加」写进 `/data/readd_queue.json`
#   （含 `.torrent` 字节），`daemon.py` 主循环每 `SCHED_TICK`(30s) 扫一次，
#   到点才 `torrents_add` —— qB 收到后**自己**校验、自己做种（不需再调一次）。
#   详见 `readd_queue.py` 的模块 docstring。
#
# ★★ 默认 **0 = 关闭**（= 维持 2026-10-03 之前的老行为：发布后立即回加）。
#   为什么默认关：新行为改变"发布→做种"的时间关系，属于**行为变更**，
#   要开必须**显式**在 `.env` 加一行（`ERR-ENV-02`：缺键时默认值必须中性）。
#
# ★★★ 已知干扰（用户 2026-10-03 明确「先忽略，记录一下」）：
#   延迟窗口内，NAS 上别的容器（reseed/iyuu 辅种那套）若看到站点有新种、
#   自动辅种，可能**抢先**把它加进 qB ⇒ 延迟失效。本项目**不拦不检测**。
SEED_DELAY_MIN = int(os.getenv("SEED_DELAY_MIN", "0") or "0")

# ==================== PicGo 图床 ====================
PICGO_API_KEY = require_env("PICGO_API_KEY")
PICGO_UPLOAD_URL = os.getenv("PICGO_UPLOAD_URL", "https://www.picgo.net/api/1/upload")

# ==================== Lsky Pro 图床（★ 蟹黄堡专用，2026-09-25 加）====================
#
# ★★ 为什么蟹黄堡要**另一个**图床（用户 2026-09-25 明确要求「要用专用的图链」）：
#   两站各有自己的图链，**不能混用** —— 拿 picgo 的链去发蟹黄堡就是跨站引用，
#   用户已明确否掉。⇒ 封面按站各存一份（见 `db.site_publish` 的 `cover_url`）。
#
# ★ 接口规格**实测**自蟹黄堡站内「接口」页
#   （`opencdhtml/蟹黄宝图链接口-Lsky Pro.html`）：
#     base = `https://img.crabpt.vip/api/v1`，上传 `POST <base>/upload`
#     鉴权 `Authorization: Bearer <token>`，字段 `file`
# ★★ 默认值**写死 base 而不是完整 upload URL**：接口页把 `/api/v1` 称为「接口URL」，
#   而 `/upload` 是它下面的一个端点 —— 将来若还要用本接口别的端点（如
#   `/profile` 查容量），有 base 就够了，写死完整路径反而要再拆一次。
LSKY_API_URL = os.getenv("LSKY_API_URL", "https://img.crabpt.vip/api/v1")
# ★★ 用 `os.getenv` **不是** `require_env`：理由逐字同上面的 `CRABPT_COOKIE` ——
#   它是**新站**（蟹黄堡）才需要的键，require 会让配了蟹黄堡的机器上
#   **乌托邦那条老链路也起不来**。
#   ★ 真要用它时由 `assets.upload_to_lsky()` 检查并给出**明确报错**（空 token ⇒ 不上传），
#     而不是让整个 `import config` 挂掉。
# ★★★ 它是**凭据**：绝不打印、绝不进 SMB 分享。`deploy.sh` 的 NEVER 已含 `.env`；
#   `daemon.save_env_snapshot` 是**白名单**制 ⇒ 本键**默认不外溢**
#   （代价：巡检看不见它，见 PLAN 的未决项）。
LSKY_TOKEN = os.getenv("LSKY_TOKEN", "")
# ★ 可选：储存策略 id。留空 = 由站点用默认策略（接口页把它列为**可选**）。
#   ★ 我们**不猜**这个 id —— 猜错会传到别的策略/桶里，而那是"写错事实"的一种。
LSKY_STRATEGY_ID = os.getenv("LSKY_STRATEGY_ID", "")

# ---- ★★★ 用基本验证**换** token（2026-09-27 加）---------------------------
#
# ★ 为什么需要这组键（用户 2026-09-27 贴来的接口文档，逐字）：
#   「当前版本接口采用 『HTTP 基本验证』的方式验证授权，**获取到 token 后**，
#     通过设置请求 header 标头来验证请求(Bearer Token)」
#   ⇒ 那句话里有**两个**动作：① 用账号密码**换** token；② 用 token 发请求。
#     本项目在 2026-09-27 之前**只实现了 ②** —— `assets.upload_to_lsky()` 有，
#     换 token 的那半**没有**。⇒ 这是"文档写着、代码缺一半"的缺口，不是 bug。
#
# ★★★ 为什么**不能**靠"让人去网页上点一下复制 token"（这是本次实测的教训）：
#   `.env` 里那个 714 字符、以 `XSRF-TOKEN=` 开头的值，**不是** token，
#   而是一个**浏览器 Cookie 串**。它看起来"配了凭据"（键在、非空、长度可观），
#   打过去却是 **HTTP 401**。★ 更糟的是：`upload_to_lsky` 只在**真发布那一刻**
#   才用到它 ⇒ 这个错**潜伏**着，等到"要发三条"时才发现（`ERR-ENV-03` 形状）。
#   ⇒ 修法不是"再去网页上小心点复制"，而是**把换 token 变成可执行的一步**：
#     账号密码进 `.env`，token **由程序换**。人手不参与 ⇒ 抄错的可能**归零**。
#
# ★ 键名与站点文档的请求体字段**逐字对齐**（`email` / `password`）——
#   不另起名字，免得下次有人对着文档核对时对不上。
# ★ `os.getenv` 不用 `require_env`：理由同上（乌托邦老链路不该被蟹黄堡的配置拖死）。
# ★★★ 是**凭据**：绝不打印、绝不进快照、绝不进仓库（同 LSKY_TOKEN 的纪律）。
LSKY_EMAIL = os.getenv("LSKY_EMAIL", "")
LSKY_PASSWORD = os.getenv("LSKY_PASSWORD", "")
# ★ token 换取结果的落盘路径。★ 为什么落盘而不是每次换：
#   站点对**接口**有配额（429 = 超出请求配额）⇒ 每次上传都换一次 token
#   是**白扣配额**，而配额是本项目反复踩的坑（见 opencd_rate.py 那段）。
#   ★★ 默认值**不能**在这里就 `os.path.join(DATA_DIR, ...)` —— `DATA_DIR`
#   定义在本文件**后面**（第 130 行附近），此处引用会 `NameError`
#   ⇒ 用 `os.getenv` 的默认值**延迟到用时**算（见 `assets.lsky_base_url` 一族）。
#   ★ 留空 = 由 `assets` 在用到时按 `DATA_DIR` 拼默认路径。
LSKY_TOKEN_FILE = os.getenv("LSKY_TOKEN_FILE", "")

# ==================== ★★ 发种交棒（#92 新流程，2026-10-07 加）====================
#
# ★ 流程（用户 2026-10-07 定稿）：成品从 HDD 复制到 SSD 发种区 → 3040(:3040) 发种做种
#   → 满足「出种≥N 或 做种≥D 天」→ 把种子任务交棒到 3020（零拷贝）→ 清 SSD 副本。
#   ★ HDD 原数据**全程不动**；SSD 那份只是"发种用的一次性副本"。
#
# ★ 这些键**都只控制本项目动作**（复制/心跳判据/交棒/清盘），不改变对外发布行为，
#   所以缺省值必须中性：**默认全关**（环境变量没配 ⇒ 新流程不接管，老行为一字不改）。
#   ★ 理由同 SEED_DELAY_MIN：新行为属于**行为变更**，要开必须显式在 `.env` 加行
#     （ERR-ENV-02：缺键时默认值必须中性）。本机也读不到 NAS 的 .env。

# ---- 总开关 + 演练开关 ----
# ENABLE_HANDOFF=true 才会让 daemon 起交棒线程（默认 false = 完全不动）。
ENABLE_HANDOFF = os.getenv("ENABLE_HANDOFF", "false")
# HANDOFF_DRY_RUN=true ⇒ 只**判定 + 记日志**，不真复制/不真交棒/不真删（默认 true）。
# ★ 默认 true（不写）与 DRY_RUN 相反方向：发种区这一串动作有**不可逆**的一步（删 SSD），
#   默认必须落在"只看不做"那一侧。
HANDOFF_DRY_RUN = os.getenv("HANDOFF_DRY_RUN", "true")

# ---- 路径（容器内）----
# ★★ 发种成品区（SSD）—— 2026-10-07（#133/#92-f1）**落点订正**：
#   ★ 这里（容器内路径）是 `/publish`，但**真正要紧的是它的宿主侧**：compose 现在把
#     **宿主 3040 的 Download 目录**（`${PUBLISH_DIR}`，
#     默认 `/volume2/docker_ssd/qbit-publish/Download`）挂到这里。
#   ★ 原先宿主侧是 `opencd2others/publish_data`（本项目**自己**的子目录）⇒ 复制过去的东西
#     **3040 根本看不到**（3040 的 `/downloads` 是**另一个**宿主目录），这就是三条种补救时
#     不得不**手工 cp** 的原因（见 NAS-92f 定稿文档 §1）。
#   ★ 订正后：复制到这里 = 直接写进 3040 的 `/downloads`；3040 加种时 `save_path=/downloads`
#     （**父目录**语义，数据在 `/downloads/<专辑>/`）。
#   ★★ 铁律：本目录下**只**放"发种用的一次性副本"，落点是 `<本目录>/<专辑>/` 一层子目录。
#     "清 SSD"只删 `PUBLISH_CONTAINER_DIR/<专辑>` 这一个子目录，**绝不**删本目录本身
#     —— 它的宿主侧现在**就是 3040 的下载根**，删它等于清空 3040（见 `_safe_rmtree` 多重护栏）。
PUBLISH_CONTAINER_DIR = os.getenv("PUBLISH_CONTAINER_DIR", "/publish")
# ★★ 3040(publish) 的做种落地根 —— 回加 3040 时 `save_path` 给**这个父目录**。
#   ★ 父目录语义（NAS-92 §2 实测）：qB 找文件 = `<save_path>/<info.name>/**<相对路径>**`。
#     数据复制到 `<PUBLISH_CONTAINER_DIR>/<专辑>`（= 此根的宿主侧 = 3040 的 /downloads/<专辑>），
#     所以 save_path 必须给**父目录**（= 3040 的 `/downloads`），给专辑目录 ⇒ 找不到 ⇒ 进度 0。
#   ★ 3040 的 `/downloads` 容器内路径 = `/downloads`（与 3020 同名，但宿主不同）。
PUBLISH_SAVE_ROOT = os.getenv("PUBLISH_SAVE_ROOT", "/downloads")
# 3020(opencd) 的做种落地根（= 容器外 /volume1/video/music，3020 的 /downloads）。
# ★ 交棒时给 3020 的 `save_path` 用；本项目容器**不需要**挂到它（零拷贝：3020 自己看得见）。
HANDOFF_TARGET_SAVE_ROOT = os.getenv("HANDOFF_TARGET_SAVE_ROOT", "/downloads")

# ---- ★★★ 隔离白名单：只有"本项目的音乐种"才走本流程（用户 2026-10-07 指令）----
# ★ 用户原话：「**仅限本项目的音乐种子**才移动到 3040 交接做种啊，这个 3020qb 我可能还要
#   发其他种子的」⇒ 本流程**绝不许误碰** 3020 上用户手发的（非本项目）种。
# ★★ 判据（用户已拍板）= **3040 上的分类命中本白名单**：
#   3040 是**本项目专用的发种实例**，其分类即"来源"。凡 3040 上 `category` ∈ 本集合
#   ⇒ 认定为本项目种 ⇒ 才允许 复制/交棒/清盘。
# ★ 落地三道闸门（见 handoff.py）：入队前 / 交棒前 / 清盘前各过一遍。
# ★ 值 = 两个发布站各自的分类（与下方 PUBLISH_CATEGORY / CRABPT_PUBLISH_CATEGORY 同源）。
HANDOFF_OWN_CATEGORIES = [
    c.strip() for c in os.getenv(
        "HANDOFF_OWN_CATEGORIES",
        f"{os.getenv('PUBLISH_CATEGORY', 'Musopia')},"
        f"{os.getenv('CRABPT_PUBLISH_CATEGORY', 'CrabPT')},"
        f"{os.getenv('PTER_PUBLISH_CATEGORY', 'PTer')}",
    ).split(",") if c.strip()
]

# ---- 交棒判据（二选一满足即交棒）----
# ★ 判据一：**出种 ≥ N 个**。★ 口径 = qB `num_complete`（swarm 里做种者数）**减去自己**，
#   即"别人也开始做种了"这件事发生了几次 ⇒ 判 `num_complete - 1 >= N`。
#   ★ 为什么要减自己：`num_complete` 把**本机这个做种者**也算进去 ⇒ 直接拿它比 3 会把
#     "只有自己在做种(=1)"读成"出种 3"。★ 见 handoff.py::judge 的注释（含阴性对照）。
HANDOFF_SEEDERS_MIN = int(os.getenv("HANDOFF_SEEDERS_MIN", "3") or "3")
# ★ 判据二：**做种 ≥ D 天**。从 qB 的 `added_on` 起算（那是种加进 3040 的时刻，≈发种成功）。
HANDOFF_SEED_DAYS = float(os.getenv("HANDOFF_SEED_DAYS", "3") or "3")

# ---- 线程节拍 ----
# 交棒线程自跑间隔（秒）。默认 300（5min）—— 交棒是"慢判断"，不需要高频。
HANDOFF_THREAD_TICK = int(os.getenv("HANDOFF_THREAD_TICK", "300") or "300")
# 每轮最多处理几条（别让一次排空卡住线程）。
HANDOFF_MAX_PER_ROUND = int(os.getenv("HANDOFF_MAX_PER_ROUND", "5") or "5")

# ==================== 状态与产物存储 ====================
# 统一挂载点：`docker-compose.yml` 里所有 service 都把**同一个 bind mount**
# 挂到这里 —— 形如 `${STATE_DIR:-.}:/data`（默认就是 compose 文件所在目录本身）。
# ★ 注意：**不是命名卷**。这段注释原先写的是「都挂同一个命名卷」，
#   而 compose 早已改成 bind mount ⇒ 照着旧注释去 `docker volume ls` 找状态
#   会**一个都找不到**（数据其实就在宿主机的 `${STATE_DIR:-.}` 下）。
# 这么做是为了不再依赖"当前目录是否存在 /app"这种隐式猜测。
DATA_DIR = os.getenv("DATA_DIR", "/data")
DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "data.db"))
OUTPUT_TORRENT_DIR = os.getenv("OUTPUT_TORRENT_DIR", os.path.join(DATA_DIR, "output_torrents"))

os.makedirs(OUTPUT_TORRENT_DIR, exist_ok=True)


def print_runtime_info():
    """启动时打印当前实际生效的关键路径/地址，方便排查"状态对不上"一类的问题。"""
    print("=" * 50)
    print(f"🗂️  DB_PATH            = {os.path.abspath(DB_PATH)}")
    print(f"🗂️  OUTPUT_TORRENT_DIR = {os.path.abspath(OUTPUT_TORRENT_DIR)}")
    print(f"🌐 QB_HOST             = {QB_HOST}")
    # ★ 只报"配没配"，**绝不打印 key 本身**（凭据纪律）。★ 这不是装饰：
    #   "现在到底走 key 还是走账号密码"直接决定"会不会被登录失败计数封 IP"，
    #   是启动自检最该看清的一格（`ERR-QB-08`）。
    print(f"🔑 QB_API_KEY          = {'已配置（走 API Key）' if QB_API_KEY else '未配置（回退账号密码）'}")
    print(f"🌐 MUSOPIA_URL         = {MUSOPIA_URL}")
    # ★★ 发种交棒的关键路径/开关 —— 排"复制到哪 / 3040 看不看得见"一类问题时最该看清。
    print(f"🔀 ENABLE_HANDOFF      = {ENABLE_HANDOFF}（dry_run={HANDOFF_DRY_RUN}）")
    print(f"🔀 PUBLISH 区(3040可见) = {PUBLISH_CONTAINER_DIR}")
    print(f"🔀 交棒目标 save_path   = {HANDOFF_TARGET_SAVE_ROOT}")
    print(f"🔀 本项目种白名单(分类) = {HANDOFF_OWN_CATEGORIES}")
    print("=" * 50)
