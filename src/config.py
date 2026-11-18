import os
import sys

# 全局网络出口：给 urllib3 打上 IPv4 解析补丁，防止部分 IPv6 边缘节点不可达
import net  # noqa: F401  (导入即产生副作用：为 urllib3 设置 IPv4 优先)


def require_env(key: str) -> str:
    """读取必需的环境变量；缺失时终止程序。"""
    val = os.getenv(key)
    if not val:
        sys.exit(f"❌ 缺少必需的环境变量 {key}，请检查 .env 配置文件")
    return val


# ==================== qBittorrent ====================
# 发种实例默认地址（与 compose 默认端口保持一致）
QB_HOST_DEFAULT = "http://127.0.0.1:3010"
_QB_HOST_RAW = (os.getenv("QB_HOST") or "").strip()
QB_HOST_FROM_DEFAULT = not _QB_HOST_RAW
QB_HOST = _QB_HOST_RAW or QB_HOST_DEFAULT

# 皇后源实例地址（提供原种与媒体数据源）
QB_HOST_OPENCD = os.getenv("QB_HOST_OPENCD", "http://127.0.0.1:3260")
QB_USER = os.getenv("QB_USER", "admin")
QB_PASS = require_env("QB_PASS")

# qBittorrent API Key（可选凭据，留空时回退为账号密码认证）
QB_API_KEY = os.getenv("QB_API_KEY", "")

# 皇后源实例专用 API Key（可选，留空时回退为账号密码认证）
OPENCD_QB_API_KEY = os.getenv("QB_API_KEY_OPENCD", "")

OPENCD_CATEGORY = os.getenv("OPENCD_CATEGORY", "opencd")
# 皇后种识别 Tracker 域名匹配规则
OPENCD_TRACKER = os.getenv("OPENCD_TRACKER", "tracker.open.cd")
PUBLISH_CATEGORY = os.getenv("PUBLISH_CATEGORY", "Musopia")

# ---- 发布前跨站查重配置 ----
# 发布前检索目标站点是否已存在相同 info.name，避免重复发种
DEDUP_BEFORE_PUBLISH = (os.getenv("DEDUP_BEFORE_PUBLISH", "true").strip().lower()
                        not in ("0", "false", "no", "off"))

# ---- 发种前版本一致性检查 ----
# 校验数据库与本地磁盘分轨/整轨状态及文件清单一致性
# ---- 音乐种子体积门禁配置 ----
# 超过此大小（GB）的音乐种子视为异常大种，触发拦截保护（0 表示不限制）
try:
    MAX_TORRENT_SIZE_GB = float(os.getenv("MAX_TORRENT_SIZE_GB", "5.0"))
except ValueError:
    MAX_TORRENT_SIZE_GB = 5.0

VERSION_GATE_BEFORE_PUBLISH = (
    os.getenv("VERSION_GATE_BEFORE_PUBLISH", "true").strip().lower()
    not in ("0", "false", "no", "off"))

# 查重子查询上限（单张专辑最多发起的搜索词变体次数）
try:
    DEDUP_MAX_SEARCH_QUERIES = int(float(os.getenv("DEDUP_MAX_SEARCH_QUERIES", "5")))
except ValueError:
    DEDUP_MAX_SEARCH_QUERIES = 5

# 候选结果验证上限（跨全部子查询最多下载比对的详情页数）
try:
    DEDUP_MAX_CANDIDATES = int(float(os.getenv("DEDUP_MAX_CANDIDATES", "16")))
except ValueError:
    DEDUP_MAX_CANDIDATES = 16

# ==================== Musopia 站点 ====================
MUSOPIA_URL = os.getenv("MUSOPIA_URL", "https://www.musopia.vip")
MUSOPIA_COOKIE = require_env("MUSOPIA_COOKIE")
MUSOPIA_PASSKEY = require_env("MUSOPIA_PASSKEY")
MUSOPIA_PUBLISH_WINDOW = os.getenv("MUSOPIA_PUBLISH_WINDOW", "00:30-09:30")

# ==================== 蟹黄堡站点 ====================
CRABPT_URL = os.getenv("CRABPT_URL", "https://crabpt.vip")
CRABPT_COOKIE = os.getenv("CRABPT_COOKIE", "")
CRABPT_MUSIC_CATEGORY = os.getenv("CRABPT_MUSIC_CATEGORY", "408")
CRABPT_PUBLISH_CATEGORY = os.getenv("CRABPT_PUBLISH_CATEGORY", "CrabPT")
CRABPT_PUBLISH_WINDOW = os.getenv("CRABPT_PUBLISH_WINDOW", "00:00-10:00")

# ==================== 猫站（PTerClub）====================
PTER_URL = os.getenv("PTER_URL", "https://pterclub.net")
PTER_COOKIE = os.getenv("PTER_COOKIE", "")
PTER_MUSIC_CATEGORY = os.getenv("PTER_MUSIC_CATEGORY", "406")
PTER_PUBLISH_CATEGORY = os.getenv("PTER_PUBLISH_CATEGORY", "PTer")
PTER_IMG_URL = os.getenv("PTER_IMG_URL", "https://img.pterclub.net/api/1/upload")
PTER_IMG_API_KEY = os.getenv("PTER_IMG_API_KEY", "")
PTER_PUBLISH_WINDOW = os.getenv("PTER_PUBLISH_WINDOW", "00:15-09:45")

# ==================== 星湾（xingwan.cc）====================
XINGWAN_URL = os.getenv("XINGWAN_URL", "https://xingwan.cc")
XINGWAN_COOKIE = os.getenv("XINGWAN_COOKIE", "")
XINGWAN_MUSIC_CATEGORY = os.getenv("XINGWAN_MUSIC_CATEGORY", "408")
# 回加 qB 时打的**标签**（不是分类：星湾不建分类，用标签识别本项目种）
XINGWAN_PUBLISH_TAG = os.getenv("XINGWAN_PUBLISH_TAG", "Xingwan")
# 每轮最多成功发布几条
XINGWAN_MAX_PER_CYCLE = int(os.getenv("XINGWAN_MAX_PER_CYCLE", "1") or "1")
# ★ 仅星湾生效的发种窗口（每天 00:00-10:00）；其它站点仍按各自周期全天发布
XINGWAN_PUBLISH_WINDOW = os.getenv("XINGWAN_PUBLISH_WINDOW", "00:00-10:00")

# ==================== 麒麟（HDKylin）====================
HDKYLIN_URL = os.getenv("HDKYLIN_URL", "https://www.hdkyl.in")
HDKYLIN_COOKIE = os.getenv("HDKYLIN_COOKIE", "")
HDKYLIN_MUSIC_CATEGORY = os.getenv("HDKYLIN_MUSIC_CATEGORY", "408")
# 回加 qB 时打的**分类**（与乌托邦/蟹黄堡/猫站分开，便于按站识别本项目种）
HDKYLIN_PUBLISH_CATEGORY = os.getenv("HDKYLIN_PUBLISH_CATEGORY", "HDKylin")
# 每轮最多成功发布几条
HDKYLIN_MAX_PER_CYCLE = int(os.getenv("HDKYLIN_MAX_PER_CYCLE", "1") or "1")
HDKYLIN_PUBLISH_WINDOW = os.getenv("HDKYLIN_PUBLISH_WINDOW", "00:45-10:00")
# ★ 封面走公共图床 PicGo（复用 PICGO_API_KEY / PICGO_UPLOAD_URL），不新增图床依赖

# ==================== 不可躺（TangPT）====================
TANGPT_URL = os.getenv("TANGPT_URL", "https://www.tangpt.top")
TANGPT_COOKIE = os.getenv("TANGPT_COOKIE", "")
TANGPT_MUSIC_CATEGORY = os.getenv("TANGPT_MUSIC_CATEGORY", "409")
TANGPT_PUBLISH_CATEGORY = os.getenv("TANGPT_PUBLISH_CATEGORY", "TangPT")
TANGPT_MAX_PER_CYCLE = int(os.getenv("TANGPT_MAX_PER_CYCLE", "1") or "1")
TANGPT_PUBLISH_WINDOW = os.getenv("TANGPT_PUBLISH_WINDOW", "01:15-09:15")

# ==================== 慕雪阁（MuXueGe）====================
MUXUEGE_URL = os.getenv("MUXUEGE_URL", "https://pt.muxuege.org")
MUXUEGE_COOKIE = os.getenv("MUXUEGE_COOKIE", "")
MUXUEGE_MUSIC_CATEGORY = os.getenv("MUXUEGE_MUSIC_CATEGORY", "408")
MUXUEGE_PUBLISH_CATEGORY = os.getenv("MUXUEGE_PUBLISH_CATEGORY", "MuXueGe")
MUXUEGE_MAX_PER_CYCLE = int(os.getenv("MUXUEGE_MAX_PER_CYCLE", "1") or "1")
MUXUEGE_PUBLISH_WINDOW = os.getenv("MUXUEGE_PUBLISH_WINDOW", "01:30-09:30")

# 延迟回加 qB 设定（分钟，0 表示发布后立即回加）
SEED_DELAY_MIN = int(os.getenv("SEED_DELAY_MIN", "0") or "0")

# ==================== PicGo 图床 ====================
PICGO_API_KEY = require_env("PICGO_API_KEY")
PICGO_UPLOAD_URL = os.getenv("PICGO_UPLOAD_URL", "https://www.picgo.net/api/1/upload")

# ==================== Lsky Pro 图床 ====================
LSKY_API_URL = os.getenv("LSKY_API_URL", "https://img.crabpt.vip/api/v1")
LSKY_TOKEN = os.getenv("LSKY_TOKEN", "")
LSKY_STRATEGY_ID = os.getenv("LSKY_STRATEGY_ID", "")

# 基本身份认证换取 Token 凭据（可选）
LSKY_EMAIL = os.getenv("LSKY_EMAIL", "")
LSKY_PASSWORD = os.getenv("LSKY_PASSWORD", "")
LSKY_TOKEN_FILE = os.getenv("LSKY_TOKEN_FILE", "")

# ==================== 发种交棒流程 ====================
# 总开关与试运行开关
ENABLE_HANDOFF = os.getenv("ENABLE_HANDOFF", "false")
HANDOFF_DRY_RUN = os.getenv("HANDOFF_DRY_RUN", "true")

# 容器内路径与存储根映射
PUBLISH_CONTAINER_DIR = os.getenv("PUBLISH_CONTAINER_DIR", "/publish")
PUBLISH_SAVE_ROOT = os.getenv("PUBLISH_SAVE_ROOT", "/downloads")
HANDOFF_TARGET_SAVE_ROOT = os.getenv("HANDOFF_TARGET_SAVE_ROOT", "/downloads")

# 常规做种目标实例与物理目录映射（支持负载均衡选择）
HANDOFF_MUSIC_HOSTS = [
    h.strip() for h in os.getenv(
        "HANDOFF_MUSIC_HOSTS",
        "http://127.0.0.1:3220,http://127.0.0.1:3240"
    ).split(",") if h.strip()
]
# 各实例对应的宿主/容器物理根目录映射
HANDOFF_DIR_MAP = {
    "http://127.0.0.1:3220": os.getenv("HANDOFF_DIR_3220", "/volume1/video/download/music/music1"),
    "http://127.0.0.1:3240": os.getenv("HANDOFF_DIR_3240", "/volume1/video/download/music/music2"),
    "http://127.0.0.1:3260": os.getenv("HANDOFF_DIR_3260", "/volume1/video/download/music/music3"),
}

# 受管分类白名单（按分类识别：Musopia / CrabPT / PTer / HDKylin / TangPT / MuXueGe）
HANDOFF_OWN_CATEGORIES = [
    c.strip() for c in os.getenv(
        "HANDOFF_OWN_CATEGORIES",
        f"{os.getenv('PUBLISH_CATEGORY', 'Musopia')},"
        f"{os.getenv('CRABPT_PUBLISH_CATEGORY', 'CrabPT')},"
        f"{os.getenv('PTER_PUBLISH_CATEGORY', 'PTer')},"
        f"{os.getenv('HDKYLIN_PUBLISH_CATEGORY', 'HDKylin')},"
        f"{os.getenv('TANGPT_PUBLISH_CATEGORY', 'TangPT')},"
        f"{os.getenv('MUXUEGE_PUBLISH_CATEGORY', 'MuXueGe')}",
    ).split(",") if c.strip()
]

# 受管标签白名单（星湾等"不建分类、只打标签"的站点 —— 按标签识别）
HANDOFF_OWN_TAGS = [
    t.strip() for t in os.getenv(
        "HANDOFF_OWN_TAGS",
        f"{os.getenv('XINGWAN_PUBLISH_TAG', 'Xingwan')}",
    ).split(",") if t.strip()
]

# 交棒触发阈值：做种人数 (除去发布者自身) 或 做种达标天数
HANDOFF_SEEDERS_MIN = int(os.getenv("HANDOFF_SEEDERS_MIN", "3") or "3")
HANDOFF_SEED_DAYS = float(os.getenv("HANDOFF_SEED_DAYS", "2") or "2")

# 调度线程周期与批次配额
HANDOFF_THREAD_TICK = int(os.getenv("HANDOFF_THREAD_TICK", "300") or "300")
HANDOFF_MAX_PER_ROUND = int(os.getenv("HANDOFF_MAX_PER_ROUND", "5") or "5")
HANDOFF_MAX_ATTEMPTS = int(os.getenv("HANDOFF_MAX_ATTEMPTS", "20") or "20")
HANDOFF_RETRY_BASE = int(os.getenv("HANDOFF_RETRY_BASE", "300") or "300")
HANDOFF_RETRY_CAP = int(os.getenv("HANDOFF_RETRY_CAP", "21600") or "21600")
HANDOFF_MATCH_MAX_FAILS = int(os.getenv("HANDOFF_MATCH_MAX_FAILS", "12") or "12")

# ==================== 状态与产物存储 ====================
DATA_DIR = os.getenv("DATA_DIR", "/data")
DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "data.db"))
OUTPUT_TORRENT_DIR = os.getenv("OUTPUT_TORRENT_DIR", os.path.join(DATA_DIR, "output_torrents"))

os.makedirs(OUTPUT_TORRENT_DIR, exist_ok=True)


def print_runtime_info():
    """打印运行时关键生效参数与路径。"""
    print("=" * 50)
    print(f"🗂️  DB_PATH            = {os.path.abspath(DB_PATH)}")
    print(f"🗂️  OUTPUT_TORRENT_DIR = {os.path.abspath(OUTPUT_TORRENT_DIR)}")
    print(f"🌐 QB_HOST             = {QB_HOST}")
    print(f"🔑 QB_API_KEY          = {'已配置（走 API Key）' if QB_API_KEY else '未配置（回退账号密码）'}")
    print(f"🌐 MUSOPIA_URL         = {MUSOPIA_URL}")
    print(f"🔀 ENABLE_HANDOFF      = {ENABLE_HANDOFF}（dry_run={HANDOFF_DRY_RUN}）")
    print(f"🔀 PUBLISH 区          = {PUBLISH_CONTAINER_DIR}")
    print(f"🔀 交棒目标 save_path   = {HANDOFF_TARGET_SAVE_ROOT}")
    print(f"🔀 交棒常规做种实例     = {HANDOFF_MUSIC_HOSTS}")
    print(f"🔀 本项目种白名单(分类) = {HANDOFF_OWN_CATEGORIES}")
    print(f"🔀 本项目种白名单(标签) = {HANDOFF_OWN_TAGS}")
    print("=" * 50)
