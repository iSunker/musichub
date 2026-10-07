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
# ★ 兜底默认值与 compose 保持一致：`docker-compose.yml` 用的是
#   `QB_HOST=${QB_HOST:-http://127.0.0.1:3040}`，且该 service 是 `network_mode: "host"`。
#   ★★ 2026-10-07（#92）：默认从 3020(opencd) 改为 **3040(publish，发种专用实例)**。
#     发种（readd_to_qb / readd_to_qb_crabpt）改走 SSD 上的发种实例；两条路共用本值
#     （唯一构造点 qb_client.build）。★ 回滚：改回 `http://127.0.0.1:3020`。
#   ★★ 不要改回 `host.docker.internal` —— **host 网络下没有那条特殊 DNS 记录**
#      （compose 里紧挨着的注释也是这么写的），它只在 bridge 网络里存在。
#   这里这个值平时用不到（compose 总会把 QB_HOST 传进来），但一旦有人不经 compose
#   直接 `python run.py`，旧的 `host.docker.internal` 会给出一个**看起来像配置问题、
#   其实是"这条域名根本不存在"**的报错。改成与 compose 同形，错了也错得一致。
QB_HOST = os.getenv("QB_HOST", "http://127.0.0.1:3040")
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

OPENCD_CATEGORY = os.getenv("OPENCD_CATEGORY", "opencd")
PUBLISH_CATEGORY = os.getenv("PUBLISH_CATEGORY", "Musopia")

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
#     默认 `/volume2/docker_ssd/qbittorrent-publish/Download`）挂到这里。
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
        f"{os.getenv('CRABPT_PUBLISH_CATEGORY', 'CrabPT')}",
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
