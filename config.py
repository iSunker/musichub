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
QB_HOST = os.getenv("QB_HOST", "http://host.docker.internal:3020")
QB_USER = os.getenv("QB_USER", "admin")
QB_PASS = require_env("QB_PASS")

OPENCD_CATEGORY = os.getenv("OPENCD_CATEGORY", "opencd")
PUBLISH_CATEGORY = os.getenv("PUBLISH_CATEGORY", "Musopia")

# ==================== Musopia 站点 ====================
MUSOPIA_URL = os.getenv("MUSOPIA_URL", "https://www.musopia.vip")
MUSOPIA_COOKIE = require_env("MUSOPIA_COOKIE")
MUSOPIA_PASSKEY = require_env("MUSOPIA_PASSKEY")

# ==================== PicGo 图床 ====================
PICGO_API_KEY = require_env("PICGO_API_KEY")
PICGO_UPLOAD_URL = os.getenv("PICGO_UPLOAD_URL", "https://www.picgo.net/api/1/upload")

# ==================== 状态与产物存储 ====================
# 统一挂载点：docker-compose.yml 里所有 service 都挂同一个命名卷到这里，
# 不再依赖"当前目录是否存在 /app"这种隐式猜测。
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
