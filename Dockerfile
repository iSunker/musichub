FROM python:3.10-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

# 版本固定，避免某天上游一个 breaking change 让整套脚本在容器重建后突然跑不起来
RUN pip install --no-cache-dir \
    qbittorrent-api==2024.9.67 \
    torf==4.2.7 \
    requests==2.32.3 \
    beautifulsoup4==4.12.3

# 代码直接打进镜像，不再依赖 bind mount 到 /app —— 状态（数据库/产物）
# 走独立的 /data，两者彻底解耦，见 docker-compose.yml 和 config.py。
COPY config.py db.py descr.py assets.py musopia.py \
     run.py publish.py update_metadata.py sync_site.py \
     backfill_covers.py check_db.py backfill_covers_opencd.py \
     daemon.py healthcheck.py \
     crawl.py opencd_parse.py opencd_rate.py opencd_log.py \
     opencd_map.py opencd_gate.py opencd_ingest.py cover_fetch.py ./

# 容器内不需要 root：脚本只写 /data，音乐目录是只读挂载。
# 但 /data 是 bind mount 过来的宿主机目录，属主由宿主机决定，这里不改 USER，
# 交给 compose 的 user: 参数按需覆盖，避免在 NAS 上撞上 UID 不匹配写不进去。

HEALTHCHECK --interval=60s --timeout=15s --start-period=60s --retries=3 \
    CMD python healthcheck.py

# 默认就是常驻编排器。一次性任务由 compose 的 tools profile 覆盖 command。
CMD ["python", "daemon.py"]
