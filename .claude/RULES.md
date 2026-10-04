# RULES.md — 项目细目规则（由 `CLAUDE.md` 用 `@` 引用）

> 本文件是 `CLAUDE.md` 的展开细目。铁律在 `CLAUDE.md`；这里放"清单 / 命令 / 命名"。

---

## A. 生产端 vs 本机：什么放哪

### A.1 与 `data/` 平级的（容器定义类，上生产）
`docker-compose.yml` · `.env` · `.env.example` · `README.md`
> ★ 2026-10-06：**`Dockerfile` / `.dockerignore` / 27 个 `.py` / `tools/` 不再上生产** ——
> 构建已移到本机（`bash build-push.sh` → 推 Docker Hub → NAS `pull`）。生产端只需要
> "一份 compose + 真凭据 `.env` + 状态 `data/`"就能把镜像跑起来。

### A.2 进 `data/` 的（容器状态与产物，生产端**只在**这里）
```
data.db  data.db-wal  data.db-shm  data.db-journal
daemon_state.json  daemon_heartbeat  daemon_env.json
readd_queue.json  lsky_token.json
output_torrents/  covers/  raw_html/
```
> 判据：**"容器写它、人要看它"的都进 `data/`**。它们由 `config.DATA_DIR` /
> compose 的 `${STATE_DIR:-./data}:/data` 统一寻址，容器内一律 `/data/...`。

### A.3 **只在本机**的（源码、工具、文档、元层 —— 一概不上生产）
- **源码**：`src/` 下全部 `.py` —— 本机构建时 `COPY src/*.py ./` 进镜像（**镜像内 `/app` 平铺，
  与容器内的相对导入/相对 `cmd` 兼容**），但**不落 NAS 磁盘**。
  ★ 本机跑 tools 时 `sys.path` 需要**同时**有 `<repo>` 与 `<repo>/src` ——
    统一走 `tools/_bootstrap.py`（一行 `import _bootstrap`），别各写一套。
- `tools/` 整个目录（容器内按需 `docker compose run --rm tools ...` —— 跑的是**镜像里**那份；
  源码只在本机维护）。
- 探测/诊断脚本：`_probe_*.py`、`diag_*.py`、`show_*.py`、`preview_*.py`、`fetch_*.py`。
- 自测与回归：`tools/test_*.py`、`tools/verify_*.py`、`tools/*-selftest.py`、`tools/build-preflight.sh`。
- 证据/产物：`*.html`（站点响应快照）、`*.log`、`counts.json`、`.scratch/`、`_preview/`。
- 构建与部署工具：`Dockerfile`、`.dockerignore`、`build-push.sh`、`deploy.sh`（已弃用）、
  `.deploy-backup/`。
- 文档与元层：`CLAUDE.md`、`.claude/RULES.md`、`ERRATA.md`、`SESSION-NOTES.md`、
  `PLAN-*.md`、`x/`。
- `.git/`（生产端不存；git 只在本机）。

---

## B. 本机构建 → Docker Hub → NAS `pull`（旧 `deploy.sh` 已弃用）

- 构建入口 = **`bash build-push.sh`**：先跑 `tools/build-preflight.sh`，再
  `docker buildx build --platform linux/amd64 --provenance=false --sbom=false`，
  推 `isunker/opencd2others:latest` + `:<git-sha>`。
  - ★ `--provenance=false --sbom=false`：NAS 是 Docker 24.0.2，对 buildx 默认产的
    attestation manifest 有兼容问题（会在 manifest list 里多出 `unknown/unknown`）。
  - ★ `--platform linux/amd64`：NAS 是 x86_64；显式写出防在别的架构机器上构建出跑不了的镜像。
- 前置（一次性）：本机 `docker login -u isunker`（公开库 ⇒ NAS 端**不需要** login）。
  - ★ Docker Hub 公开库：NAS 只需在其 docker 上配好出网代理即可 `pull`。
- `NEVER=(...)`（在旧 `deploy.sh` 里，`bash deploy.sh --list-never` 可打印）是
  "生产独有、绝不进镜像"的断言：`.env` + 全部 `data/` 状态。
- ★ 改了 `Dockerfile` 的 `COPY` 行 / 加了新模块 ⇒ `tools/build-preflight.sh` 会查
  "COPY 的源在本机存在吗"。**这条护栏没有消失，只是从"同步时"前移到了"构建前"**。

---

## C. 常用命令

### C.1 本机（Git Bash）
```bash
cd /d/Projects/opencd2others/zhuanzhong

# dummy env（跑自检必需）
export QB_PASS=x MUSOPIA_COOKIE=x MUSOPIA_PASSKEY=x PICGO_API_KEY=x \
       OPENCD_COOKIE=x DATA_DIR="$PWD/.scratch/selftest-data"
mkdir -p "$DATA_DIR"

python src/run.py --selftest             # 主自检（含 ⑧ 接线断言）
python tools/gen_counts.py               # 重生成 counts.json
python src/opencd.py parse --selftest    # 合并后：模块自检（子命令分布见 README）

bash build-push.sh                       # 本机构建 + 推 Docker Hub（含前置自检）
SKIP_PUSH=1 bash build-push.sh           # 只本机构建、不推（本地验证）
bash tools/build-preflight.sh            # 只跑构建前置自检
```

### C.2 NAS（SSH）
```bash
ssh -p 36688 -i "C:/Users/艾森克/.ssh/id_ed25519" iSunker@192.168.0.7
# 登录后：
cd /volume2/docker_ssd/opencd2others
/usr/local/bin/docker compose config --quiet && echo 'YAML OK'
/usr/local/bin/docker compose pull                       # ★ 先拉新镜像（构建在本机做）
/usr/local/bin/docker compose up -d --remove-orphans musopia   # ★ 只 musopia；没有 --build 了
/usr/local/bin/docker logs --tail=60 opencd2musopia
/usr/local/bin/docker exec opencd2musopia python run.py --selftest
```
> ★ `pull` 要在**任何** `--profile tools run` **之前**（`run` 不会自动 pull，会用旧镜像）。
> ★ 非交互 SSH 下 docker 不在 PATH ⇒ 用全路径 `/usr/local/bin/docker`。
> ★ SSH 输出过噪，过滤：`grep -v "WARNING\|post-quantum\|store now\|may need to be upgraded\|openssh.com"`。

---

## D. 命名与结构性约定

- compose 顶层 `name: opencd2musopia`（**写死**）⇒ 目录改名**不影响**容器记账；
  改 project 名 = 换一套记账，须 `docker rm -f` 旧容器再 up。
- 容器名 `opencd2musopia`（`container_name` 定死）；一次性任务容器
  `musopia_tools` / `musopia_once`（`profiles: [tools]`）。
- **qB 构造点唯一**：全项目只有 `qb_client.py` 能 `Client(...)` / `auth_log_in()`；
  别处一律 `import qb_client` + `qb_client.build(config)`。由 `run.py` ⑧ 判据钉住。
- **合并后的模块**（2026-10 整理）：
  - `opencd.py`（由 `opencd_parse/rate/log/map/gate/ingest` 六合一；
    CLI：`python opencd.py <parse|rate|log|map|gate|ingest> [...]`）。
  - `tools/readd.py`（由 `readd_qb/readd_crabpt/readd_site_missing` 合并；
    CLI：`python tools/readd.py <qb|crabpt|site> [...]`）。
  - `tools/doc-audit/audit.py`（由 `01_headings/02_refs/03_terms/04_probes` 合并；
    CLI：`python tools/doc-audit/audit.py <headings|refs|terms|probes> [...]`）。
  - ★ 旧文件名**不得**再被 import（`run.py` 自检会报红）。
- 只读外部库：`vertex2` 的 `sql.db`（只读挂载 `/vertex:ro`），永远 `:ro` + `query_only=1`。

---

## E. 提交前自查清单

- [ ] `python src/run.py --selftest` ✅
- [ ] 相关模块 `--selftest` ✅（数量与改动前一致，或**有意**变更并已在注释里说明）
- [ ] 改了计数相关 ⇒ `python tools/gen_counts.py` 已重跑
- [ ] 改了 `Dockerfile` 的 `COPY` 行 / 加了新模块 ⇒ `bash tools/build-preflight.sh` 通过
- [ ] 要上线 ⇒ `bash build-push.sh`（本机构建 + 推 Docker Hub），NAS 侧 `pull && up -d`
- [ ] 文档同步：`README.md`（行为）/ `ERRATA.md`（教训）/ `SESSION-NOTES.md`（追加）
- [ ] 没有把凭据 / `data/` 状态 / 备份垃圾带到生产端或 git
