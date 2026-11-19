#!/usr/bin/env bash
# MusicHub 3.0 容器入口启动脚本
set -eo pipefail

echo "=========================================================="
echo " Starting MusicHub 3.0 Container Infrastructure "
echo " Time: $(date '+%Y-%m-%d %H:%M:%S') "
echo " User: $(id -u):$(id -g) "
echo " Workdir: $(pwd) "
echo "=========================================================="

# 确保必要数据目录存在
mkdir -p /app/musichub/data/torrents
mkdir -p /app/musichub/data/raw_html

# 启动环境自检
python -c "
from musichub.core.config import MusicHubConfig
cfg = MusicHubConfig.from_env()
print(f'[Init] DB Path: {cfg.db_path}')
print(f'[Init] Publish QB: {cfg.publish_qb.host}:{cfg.publish_qb.port}')
print(f'[Init] Download QB: {cfg.musicdownload_qb.host}:{cfg.musicdownload_qb.port}')
print(f'[Init] Storage Mount: {cfg.storage.disk_mount_path}')
"

echo "[Init] Preflight checks passed, executing command: $@"
exec "$@"
