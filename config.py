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
