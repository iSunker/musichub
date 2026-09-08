"""
docker healthcheck：判断编排器是不是还活着。

只看心跳文件的新鲜度，不去访问站点——healthcheck 每 30 秒跑一次，
要是每次都请求一遍 musopia.vip，光健康检查就能把账号刷成异常流量。
站点连通性由各阶段自己在运行时报错，不该由 healthcheck 兼任。

心跳由 daemon 在每轮调度和每个阶段结束后写入。允许的最大静默时间取
SCHED_TICK 的若干倍，且不小于 STAGE_TIMEOUT——某个阶段跑满超时上限时
主循环确实会长时间不写心跳，那属于正常工作而非卡死。
"""
import os
import sys
import time

DATA_DIR = os.getenv("DATA_DIR", "/data")
HEARTBEAT_FILE = os.path.join(DATA_DIR, "daemon_heartbeat")


def env_int(key, default):
    try:
        return int(float(os.getenv(key) or default))
    except ValueError:
        return default


def main():
    tick = env_int("SCHED_TICK", 30)
    stage_timeout = env_int("STAGE_TIMEOUT", 3600)

    # 阶段运行期间主循环不写心跳，所以容忍度必须覆盖一次完整的阶段超时，
    # 再加一点余量给进程收尾，否则健康检查会把正在干活的容器判死并重启它。
    limit = max(tick * 10, stage_timeout + 300)

    try:
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            last = float(f.read().strip())
    except FileNotFoundError:
        print(f"UNHEALTHY: 心跳文件不存在 ({HEARTBEAT_FILE})，编排器可能还没启动完成")
        return 1
    except (ValueError, OSError) as e:
        print(f"UNHEALTHY: 读心跳失败: {e}")
        return 1

    age = time.time() - last
    if age > limit:
        print(f"UNHEALTHY: 心跳已停止 {age:.0f}s（上限 {limit}s）")
        return 1

    print(f"OK: 心跳 {age:.0f}s 前")
    return 0


if __name__ == "__main__":
    sys.exit(main())
