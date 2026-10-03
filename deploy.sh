#!/usr/bin/env bash
# =====================================================================
# ⚠️ 已弃用（2026-10-06）—— 本脚本的职责已由 `build-push.sh` 取代。
#
#   旧职责：把本机的 27 个 .py + tools/ 经 SMB 白名单同步到 NAS 的生产目录
#           （因为那时"NAS 上构建"）。
#   为什么弃用：
#     · 构建已**移到本机**（`bash build-push.sh` → 推 GHCR → NAS 只 `pull && up -d`）；
#     · NAS 顶层不再需要 .py / tools/ / Dockerfile ⇒ 没有"要同步的文件"了；
#     · 旧模型实测踩过 `ERR-DOCKER-09`（COPY 吃进 SMB 缓存里的旧文件）——
#       根因**只存在于 NAS 构建**，本机构建后消失；
#     · 前提"NAS 的 SSH 没开"**已被实测推翻**（`ssh -p 36688 …@192.168.0.7` 可用，
#       NAS 用户在 docker 组）⇒ 不再需要"只写 SMB、重建靠手工"这套。
#
#   新流程（本机 + NAS 两条命令，详见 README「部署与构建」）：
#       bash build-push.sh                      # 本机构建 + 推 GHCR（含前置自检）
#       ssh -p 36688 -i "C:/Users/艾森克/.ssh/id_ed25519" iSunker@192.168.0.7 \
#         "cd /volume2/docker_ssd/opencd2others && \
#          /usr/local/bin/docker compose pull && \
#          /usr/local/bin/docker compose up -d --remove-orphans musopia"
#
#   ★ 构建前的「Dockerfile COPY 源齐全 / 行尾」自检已搬进 `tools/build-preflight.sh`
#     （由 build-push.sh 调用）—— 那条护栏**没有消失，只是前移**：不再等"同步到
#     NAS 才发现缺文件"，构建前就在本机报。
#
#   ★ 本文件**保留**以下内容，因为它们是**权威定义**、被其它地方引用：
#       · `NEVER` 名单 —— "生产独有、绝不进镜像/绝不覆盖"的状态清单。
#         被 `config.py`（.env 凭据）、`db.py`（data.db）、`daemon.py`（.env）的注释引用。
#       · 一整套"为什么"的教训注释（白名单纪律、`_same()` 静默失效事件、
#         原子替换、CRLF 与 `grep $'\r'` 静默失配、heredoc 引号 …）——
#         这些坑与"在哪台机器构建"无关，仍然有效。
#   ★ 若将来 nv 真的需要"本机 → NAS 传文件"，别复活本脚本 —— 用 scp/rsync，
#     并按下面 NEVER 的语义保证不覆盖生产状态。
# =====================================================================

# 本脚本不再执行任何同步动作。保留一个 `--list-never` 便于随时打印权威名单。
set -euo pipefail

# 生产独有，绝不进镜像、绝不被本机覆盖。写在这里是为了把"动不到"变成一条
# **可检查的断言**，而不是靠每次读代码时自己想起来。
# ★ 2026-10-05：全部状态收进 `data/` 子目录（compose 里 `${STATE_DIR:-./data}:/data`）
#   ⇒ 断言用 `data/` 前缀。**别只改一半** —— 前缀对不上就等于断言失效。
NEVER=(
  .env
  data/data.db data/data.db-journal data/data.db-wal data/data.db-shm
  data/daemon_state.json data/daemon_heartbeat data/daemon_env.json
  data/readd_queue.json data/lsky_token.json
  data/output_torrents data/covers data/raw_html
)

case "${1:-}" in
  --list-never)
    printf '%s\n' "${NEVER[@]}"
    exit 0
    ;;
  ""|--help|-h)
    sed -n '2,45p' "$0"       # 打印本文件头部的弃用说明
    exit 0
    ;;
  *)
    echo "⚠️ deploy.sh 已弃用（见文件头）。构建请用: bash build-push.sh" >&2
    echo "   （唯一保留的用法: bash deploy.sh --list-never）" >&2
    exit 2
    ;;
esac
