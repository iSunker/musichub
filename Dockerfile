FROM python:3.10-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

# 版本固定，避免某天上游一个 breaking change 让整套脚本在容器重建后突然跑不起来
# ★★ 2026-10-04：qbittorrent-api 2024.9.67 → 2026.10.0。
#    旧版 `auth_log_in()` 判据是 `auth_response.text != "Ok."` ⇒ LoginFailed；
#    而 qB 5.1.2+（本 NAS 的 3020 已升到 5.2.4）登录成功返回 **204 空 body**
#    ⇒ 旧版**静默** `LoginFailed`（异常消息为空）⇒ 所有 qB 连接全挂，
#    表现是「种子发布成功但从没加进 qB ⇒ 不做种」（crabpt 软失败）
#    以及 scan/publish 阶段硬崩（退出码 1）。详见 ERRATA。
#    新版识别空 body（`text == "" or "Ok."`），并原生支持 API Key（v5.2.0+）。
RUN pip install --no-cache-dir \
    qbittorrent-api==2026.10.0 \
    torf==4.2.7 \
    requests==2.32.3 \
    beautifulsoup4==4.12.3 \
    Pillow==11.3.0

# 代码直接打进镜像，不再依赖 bind mount 到 /app —— 状态（数据库/产物）
# 走独立的 /data，两者彻底解耦，见 docker-compose.yml 和 config.py。
# ★★ 2026-10-06：源码已收进 `src/`（本机仓库顶层更清爽）。这里 `COPY src/*.py ./`
#   —— 目标是 **`/app` 根（保持平铺）**，所以**镜像内布局逐字不变**：
#   `/app/{config.py,daemon.py,…}` 照旧。这一点是承重的：
#     · compose 的 `command: ["python","daemon.py"]`、`CMD`、`WORKDIR /app` 都不用改；
#     · `daemon.py` 里 8 条裸相对 `cmd`（`["python","run.py"]` …）依赖 `cwd=/app`；
#     · tools 里 12 处 `os.environ.get("MUSOPIA_APP_DIR", "/app")` 也默认 `/app`。
#   ⇒ 只改了"本机仓库的形状"，没改"镜像内的形状"。
#   ★ 逐文件枚举的旧写法（27 行）已换成通配 —— 但**护栏没消失**：
#     `tools/build-preflight.sh` 会检查通配源是否有匹配（无匹配时 COPY 会静默拷进
#     0 个文件、构建却成功），见 .claude/RULES.md §B。
COPY src/*.py ./
# ★ 2026-10-04：`opencd_parse/rate/log/map/gate/ingest` 六个模块已合并成
#   单文件 `opencd.py`（CLI 子命令 parse/rate/log/map/gate/ingest）。
#   ★ 合并后 `run.py --selftest` 的 ⑧b 判据会扫全仓库断言**没有**任何文件再 import
#   那六个旧名（防"合了但没改完"），见 ERRATA。

# ★★ `tools/` 整个目录也打进去（2026-09-26）—— 原先**只 COPY 根目录的 .py**，
#    于是容器里根本没有 `/app/tools/` ⇒ 任何 `tools/*.py` 都只能在本机跑。

# ★★ `tools/` 整个目录也打进去（2026-09-26）—— 原先**只 COPY 根目录的 .py**，
#    于是容器里根本没有 `/app/tools/` ⇒ 任何 `tools/*.py` 都只能在本机跑。
#    实测踩过：`docker compose run --rm tools python tools/recall_opencd_descr.py`
#    ⇒ `can't open file '/app/tools/...': No such file or directory`。
#    ★ 但有两类**刻意排除**：
#      · `tools/doc-audit/`  —— 纯本机文档审计，跑它要读仓库里全部 .md，容器里没有意义
#      · `tools/.baseline-*.json` / `.nas_health_last.json` —— 本机巡检的状态文件，
#        打进去只会让"容器里的巡检"和"本机的巡检"各记一份，两个口径（ERR-META-05）
#    ★ 判据：**容器内要跑的脚本必须真的在容器里** —— 别假设 bind mount 里有
#      （根目录那份 COPY 就是反例：它靠"枚举文件名"维护，加文件时必漏）。
COPY tools/ ./tools/

# 容器内不需要 root：脚本只写 /data，音乐目录是只读挂载。
# 但 /data 是 bind mount 过来的宿主机目录，属主由宿主机决定，这里不改 USER，
# 交给 compose 的 user: 参数按需覆盖，避免在 NAS 上撞上 UID 不匹配写不进去。

HEALTHCHECK --interval=60s --timeout=15s --start-period=60s --retries=3 \
    CMD python healthcheck.py

# 默认就是常驻编排器。一次性任务由 compose 的 tools profile 覆盖 command。
CMD ["python", "daemon.py"]
