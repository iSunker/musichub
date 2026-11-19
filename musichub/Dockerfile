# syntax=docker/dockerfile:1
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PYTHONPATH=/app

# 安装基础依赖与构建所需工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    tzdata \
    procps \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖定义并安装
COPY musichub/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    beautifulsoup4==4.12.3 \
    qbittorrent-api==2026.10.0

# 拷贝 musichub 核心代码与运行脚本
COPY musichub /app/musichub
COPY musichub/entrypoint.sh /app/entrypoint.sh
COPY musichub/healthcheck.sh /app/healthcheck.sh

RUN chmod +x /app/entrypoint.sh /app/healthcheck.sh

# 默认健康探针 (每 30 秒执行一次探针自检)
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD /app/healthcheck.sh

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "musichub.daemon", "--loop"]
