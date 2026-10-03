# RULES.md — 项目细目规则（由 `CLAUDE.md` 用 `@` 引用）

> 本文件是 `CLAUDE.md` 的展开细目。铁律在 `CLAUDE.md`；这里放"清单 / 命令 / 命名"。

---

## A. 生产端 vs 本机：什么放哪

### A.1 与 `data/` 平级的（容器定义类，上生产）
`docker-compose.yml` · `Dockerfile` · `.dockerignore` · `.env` · `.env.example` · `README.md`

### A.2 进 `data/` 的（容器状态与产物，生产端**只在**这里）
```
data.db  data.db-wal  data.db-shm  data.db-journal
daemon_state.json  daemon_heartbeat  daemon_env.json
readd_queue.json  lsky_token.json
output_torrents/  covers/  raw_html/
```
> 判据：**"容器写它、人要看它"的都进 `data/`**。它们由 `config.DATA_DIR` /
> compose 的 `${STATE_DIR:-./data}:/data` 统一寻址，容器内一律 `/data/...`。

### A.3 **只在本机**、绝不上生产的（`deploy.sh` 白名单之外 + NEVER 名单）
- `tools/` 整个目录（容器内按需用 `docker compose run --rm tools ...`，
  但**源码只在本机维护**；镜像构建时由 Dockerfile `COPY tools/` 打进去）。
- 探测/诊断脚本：`_probe_*.py`、`diag_*.py`、`show_*.py`、`preview_*.py`、`fetch_*.py`。
- 自测与回归：`tools/test_*.py`、`tools/verify_*.py`、`tools/*-selftest.py`。
- 证据/产物：`*.html`（站点响应快照）、`*.log`、`counts.json`、`.scratch/`、`_preview/`。
- 文档与元层：`CLAUDE.md`、`.claude/RULES.md`、`ERRATA.md`、`SESSION-NOTES.md`、
  `PLAN-*.md`、`x/`、`deploy.sh`、`.deploy.nasrc`、`.deploy-backup/`。
- **备份垃圾**（生产端发现即删，不留）：`.env.bak-*` / `.env.before-*` / `.env.pre-*` /
  `Dockerfile.bak-*` / `*.db.bak-*` / `daemon_state.json.bak-*` /
  `_env_backups_archived_*` / `qbtune_*.bak-*`。
- `.git/`（生产端删；git 只在本机）。

---

## B. `deploy.sh` 白名单同步规则

- 白名单 = 脚本里的 `FILES` 数组；**权威来源是 `Dockerfile` 的 `COPY` 行**。
  脚本末尾 `dockerfile_copy_files()` / `dockerfile_copy_dirs()` 会反查，
  漏一个 ⇒ **拒绝部署**。改了 `Dockerfile` 就**必须**改 `FILES`。
- `NEVER=(...)` 是"生产独有、绝不覆盖"的断言：`.env` + 全部 `data/` 状态。
  白名单里混进它们 ⇒ 脚本拒绝部署。
- 默认 dry-run；`--apply` 才写；写前备份到本机 `.deploy-backup/<时间戳>/`，
  `--rollback` 回滚。
- ★ `deploy.sh` 只会**叠加/覆盖**，**不会删除** NAS 上的文件。
  要删生产端的东西只能手工（SMB 或 SSH `rm`）。

---

## C. 常用命令

### C.1 本机（Git Bash）
```bash
cd /d/Projects/opencd2others/zhuanzhong

# dummy env（跑自检必需）
export QB_PASS=x MUSOPIA_COOKIE=x MUSOPIA_PASSKEY=x PICGO_API_KEY=x \
       OPENCD_COOKIE=x DATA_DIR="$PWD/.scratch/selftest-data"
mkdir -p "$DATA_DIR"

python run.py --selftest                 # 主自检（含 ⑧ 接线断言）
python tools/gen_counts.py               # 重生成 counts.json
python opencd.py parse --selftest        # 合并后：模块自检（子命令分布见 README）
bash deploy.sh                           # 预览（dry-run）
bash deploy.sh --apply                   # 同步到 NAS
```

### C.2 NAS（SSH）
```bash
ssh -p 36688 -i "C:/Users/艾森克/.ssh/id_ed25519" iSunker@192.168.0.7
# 登录后：
cd /volume2/docker_ssd/opencd2others
/usr/local/bin/docker compose config --quiet && echo 'YAML OK'
/usr/local/bin/docker compose up -d --build musopia      # ★ 只 musopia
/usr/local/bin/docker logs --tail=60 opencd2musopia
/usr/local/bin/docker exec opencd2musopia python run.py --selftest
```
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

- [ ] `python run.py --selftest` ✅
- [ ] 相关模块 `--selftest` ✅（数量与改动前一致，或**有意**变更并已在注释里说明）
- [ ] 改了计数相关 ⇒ `python tools/gen_counts.py` 已重跑
- [ ] `bash deploy.sh`（dry-run）无"白名单漏文件"报错
- [ ] 文档同步：`README.md`（行为）/ `ERRATA.md`（教训）/ `SESSION-NOTES.md`（追加）
- [ ] 没有把凭据 / `data/` 状态 / 备份垃圾带到生产端或 git
