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


def require_env(key: str) -> str:
    """读取必须存在的环境变量；缺失就直接终止程序。"""
    val = os.getenv(key)
    if not val:
        sys.exit(f"❌ 缺少必需的环境变量 {key}，请检查 .env 文件（可参考 .env.example 创建）")
    return val


# ==================== qBittorrent ====================
# ★ 兜底默认值与 compose 保持一致：`docker-compose.yml` 用的是
#   `QB_HOST=${QB_HOST:-http://127.0.0.1:3020}`，且该 service 是 `network_mode: "host"`。
#   ★★ 不要改回 `host.docker.internal` —— **host 网络下没有那条特殊 DNS 记录**
#      （compose 里紧挨着的注释也是这么写的），它只在 bridge 网络里存在。
#   这里这个值平时用不到（compose 总会把 QB_HOST 传进来），但一旦有人不经 compose
#   直接 `python run.py`，旧的 `host.docker.internal` 会给出一个**看起来像配置问题、
#   其实是"这条域名根本不存在"**的报错。改成与 compose 同形，错了也错得一致。
QB_HOST = os.getenv("QB_HOST", "http://127.0.0.1:3020")
QB_USER = os.getenv("QB_USER", "admin")
QB_PASS = require_env("QB_PASS")

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
    print(f"🌐 MUSOPIA_URL         = {MUSOPIA_URL}")
    print("=" * 50)
