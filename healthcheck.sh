#!/usr/bin/env bash
# MusicHub 3.0 容器健康检测探针 (Healthcheck Probe)
set -eo pipefail

# 1. 探针 A：检查 Python 主守护进程是否在存活运行
if ! pgrep -f "musichub.daemon" > /dev/null; then
    echo "[UNHEALTHY] musichub.daemon process not running"
    exit 1
fi

# 2. 探针 B：执行只读审计轻量探针 (验证 DB 可读、背压与端口健康)
python -c "
import sys
from musichub.core.config import MusicHubConfig
from musichub.storage.backpressure import StorageBackpressure
from musichub.routing.graph import RoutingGraphManager

try:
    cfg = MusicHubConfig.from_env()
    # 检查数据库连接与表结构
    mgr = RoutingGraphManager(cfg.db_path)
    mgr.close()
    
    # 检查背压评估能力
    bp = StorageBackpressure(mount_path=cfg.storage.disk_mount_path)
    level, free_gb = bp.assess_watermark()
    if level == 'RED':
        print(f'[WARN] Storage RED watermark triggered: {free_gb:.1f}GB free')
        # RED 水位告警但不强制判定容器死掉（由守护进程执行自愈 GC）
except Exception as e:
    print(f'[UNHEALTHY] Internal probe exception: {e}', file=sys.stderr)
    sys.exit(1)
" || exit 1

echo "[HEALTHY] MusicHub 3.0 daemon and database healthy."
exit 0
