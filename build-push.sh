#!/usr/bin/env bash
# =====================================================================
# 本机构建 + 推送镜像到 Docker Hub（取代旧 deploy.sh 的"同步 .py 到 NAS"职责）
#
# 为什么改成这样（2026-10-06）：
#   旧模型：本机 `deploy.sh` 经 SMB 把 27 个 .py 同步到 NAS → **NAS 上**
#           `docker compose up -d --build`。
#     · NAS 顶层因此摊着 27 个 .py + tools/ + Dockerfile（构建上下文），难看、易丢；
#     · NAS 出网受限（实测 `registry-1.docker.io` 直连不可达）⇒ NAS 上构建拉基础镜像本就脆弱；
#     · 实测踩过 ERR-DOCKER-09（COPY 吃进 SMB 缓存里的旧文件）—— 根因**只在 NAS 构建时**存在。
#   新模型：**本机** build → push Docker Hub → NAS 只 `docker compose pull && up -d`。
#     · NAS 顶层只剩 compose 定义类文件 + data/；
#     · 构建在有完整 Docker 的本机跑，快且稳；
#     · 镜像按 git sha 打 tag ⇒ 部署**可回滚**（旧模型只有一个会被就地覆盖的 latest）。
#
# ★ 为什么是 Docker Hub（`isunker/opencd2others`，公开库）而不是 GHCR：
#   本机 Docker Desktop 已配代理（settings-store.json → 192.168.0.7:7894），
#   实测 `docker pull alpine` 走代理成功 ⇒ 本机对 Docker Hub 可达；
#   NAS 侧 docker 也已配好代理，能直接 `pull`。公开库 ⇒ NAS **无需 login**。
#
# 用法:
#   bash build-push.sh                 # 构建 + 推 :latest 与 :<git-sha>
#   bash build-push.sh v1.2            # 额外打一个自定义 tag
#   PREFLIGHT_SELFTEST=1 bash build-push.sh   # 构建前先跑 run.py --selftest
#   SKIP_PUSH=1 bash build-push.sh     # 只在本机构建（不推），用于本地验证
#
# 前置（一次性）:
#   docker login -u isunker            # 交互式输入密码 / Access Token（本机 Docker Desktop）
#   （公开库：NAS 端**不需要** login 就能 pull）
# =====================================================================
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SELF_DIR"                                  # 本脚本在仓库根

IMAGE_NAME="isunker/opencd2others"
EXTRA_TAG="${1:-}"

# ---------- 前置检查 ----------
command -v docker >/dev/null || { echo "✗ 找不到 docker" >&2; exit 1; }
docker buildx version >/dev/null 2>&1 || { echo "✗ docker buildx 不可用" >&2; exit 1; }

# ---------- ① 构建前置自检（COPY 源齐全 / 行尾）----------
echo "→ 构建前置自检"
PREFLIGHT_SELFTEST="${PREFLIGHT_SELFTEST:-0}" bash "$SRC/tools/build-preflight.sh"

# ---------- ② git sha 打 tag ----------
# ★ 工作区有未提交改动时也要能构建（部署的是"当前磁盘态"而非"某次 commit"），
#   但 tag 要诚实：有脏改动就加 `-dirty`，免得 sha tag 名不副实。
if command -v git >/dev/null && git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
  SHA="$(git -C "$SRC" rev-parse --short HEAD)"
  git -C "$SRC" diff --quiet --ignore-submodules HEAD 2>/dev/null || SHA="${SHA}-dirty"
else
  SHA="nogit-$(date +%Y%m%d-%H%M%S)"
fi

TAGS=(-t "${IMAGE_NAME}:latest" -t "${IMAGE_NAME}:${SHA}")
if [[ -n "$EXTRA_TAG" ]]; then
  TAGS+=(-t "${IMAGE_NAME}:${EXTRA_TAG}")
fi

echo "→ 构建 ${IMAGE_NAME}:latest  (sha tag: ${SHA})"

# ---------- ③ buildx 构建 ----------
# ★★ `--platform linux/amd64`：NAS 是 x86_64（实测 `uname -m`），本机也是 x86_64
#   ⇒ 同架构、无需 QEMU。显式写出来是**防止**在 Apple Silicon / 其它架构机器上
#   构建出 NAS 跑不了的镜像（那些机器上 buildx 默认取宿主架构）。
#
# ★★ `--provenance=false --sbom=false`：本机 buildx（Docker Desktop 29.x）默认会产
#   **attestation manifest（provenance + SBOM）**，它会在 manifest list 里多出一个
#   `unknown/unknown` 平台项。NAS 是 **Docker 24.0.2**（较旧），对这类 OCI attestation
#   索引有兼容问题 ⇒ 拉取可能报错或选错平台。关掉即得干净的**单平台 manifest**。
#   ★ 已验证：关不关 provenance，产出的 **image ID 相同** —— 它只影响 manifest 元数据。
#
# ★ `--push` 与 `--load` 互斥：`--load`（打进本地 image store）会丢掉多 tag，
#   且我们本来就要推。SKIP_PUSH=1 时才改 `--load` 以便本地验证。
#
# ★★ 2026-10-04 重要修正：**默认改用经典 `docker build` + `docker push`**，
#   不再默认走 buildx。原因（实测踩坑）：
#     Docker Desktop 的 `OverrideProxyHTTP/HTTPS`（settings-store.json）只作用于
#     **dockerd**（`docker pull` 会走代理，实测 `docker pull alpine` 成功）；
#     但 **buildx 的 buildkit worker 是 containerd executor、跑在独立网络命名空间**，
#     它**不读**这个代理 ⇒ `buildx --push` 直连 registry-1.docker.io，撞上被污染
#     的 DNS 应答（157.240.6.35，Facebook 段），报 `failed to fetch oauth token`。
#   经典路径的 push 由 **dockerd** 完成 ⇒ 走已配好的代理，与 `docker pull` 同一条网络。
#   ★ 何时仍要用 buildx：`USE_BUILDX=1`（例如需要多平台/缓存导出）。那时须自行
#     给 buildkit 配代理（`docker buildx create --driver-opt env.HTTP_PROXY=...`）。
if [[ "${USE_BUILDX:-0}" == "1" ]]; then
  # --platform linux/amd64：NAS 是 x86_64（实测 `uname -m`），本机也是 x86_64
  #   ⇒ 同架构、无需 QEMU。显式写出来是**防止**在别的架构机器上构建出 NAS 跑不了的镜像。
  # --provenance=false --sbom=false：本机 buildx 默认会产 attestation manifest
  #   （provenance + SBOM），在 manifest list 里多出 `unknown/unknown` 平台项。
  #   NAS 是 Docker 24.0.2（较旧），对这类 OCI attestation 索引有兼容问题。
  echo "  （USE_BUILDX=1：走 buildx）"
  if [[ "${SKIP_PUSH:-0}" == "1" ]]; then
    docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
      -t "${IMAGE_NAME}:latest" --load "$SRC"
  else
    docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
      "${TAGS[@]}" --push "$SRC"
  fi
else
  # 经典路径（默认）：build 由 dockerd 做，push 也由 dockerd 做 ⇒ 走 Docker Desktop 代理。
  #   ★ `--platform linux/amd64` 对经典 builder 也有效（daemon 支持时）。
  echo "  （经典 docker build + push；如需 buildx 加 USE_BUILDX=1）"
  docker build --platform linux/amd64 "${TAGS[@]}" "$SRC"
  if [[ "${SKIP_PUSH:-0}" == "1" ]]; then
    echo "  （SKIP_PUSH=1：已构建，不推）"
  else
    docker push "${IMAGE_NAME}:latest"
    docker push "${IMAGE_NAME}:${SHA}"
    [[ -n "$EXTRA_TAG" ]] && docker push "${IMAGE_NAME}:${EXTRA_TAG}"
  fi
fi

# ---------- ④ 交付信息 ----------
echo ""
echo "✓ 构建完成：${IMAGE_NAME}"
echo "  tags: latest${EXTRA_TAG:+ · $EXTRA_TAG} · ${SHA}"
echo ""
cat <<MSG
────────────────────────────────────────────────────────────
接下来在 **NAS 上**执行（本机 → NAS 走 SSH，见 README「部署与构建」）：

  ssh -p 36688 -i "C:/Users/艾森克/.ssh/id_ed25519" iSunker@192.168.0.7
  cd /volume2/docker_ssd/opencd2others
  # ★★ 部署**点名本次 sha tag**（不是 latest！）—— NAS 有 registry-mirrors，
  #    它缓存 latest 这个**可变 tag**（实测滞后约 1 小时）⇒ pull latest 会静默
  #    拿到旧镜像（命令却回 Status: Image is up to date），而所有命令都报成功。
  #    不可变引用（:<sha> / @sha256:…）**绕过** 镜像站的 tag 缓存（ERR-DOCKER-13）。
  sed -i 's#image: ${IMAGE_NAME}:.*#image: ${IMAGE_NAME}:${SHA}#' docker-compose.yml
  /usr/local/bin/docker compose pull
  /usr/local/bin/docker compose up -d --force-recreate --remove-orphans musopia
  /usr/local/bin/docker compose logs --tail=60 opencd2musopia

  ★ 顺序纪律：\`pull\` 必须在**任何** \`--profile tools run\` **之前** ——
    \`docker compose run\` **不会**自动 pull，会用本地旧镜像（症状同 ERR-DOCKER-01：
    "跑了但没生效"）。
  ★★ 验证跑的是新镜像（**别用"pull 成功 / up to date"推"NAS 上生效了"** —— 镜像站
     会把 latest 报成 up-to-date；见 ERR-DOCKER-13）：
      /usr/local/bin/docker inspect opencd2musopia --format '{{.Image}}'
    必须等于本机刚 push 的 image id（本机：docker image inspect ${IMAGE_NAME}:${SHA} --format '{{.Id}}'）。
  ★ 回滚：把 compose 里的 image 换回上一个 git-sha tag 再 up -d。
    sha tag 是这次改造带来的净收益（部署一律点名它）。
MSG
