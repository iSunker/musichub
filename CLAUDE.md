# CLAUDE.md — 本项目的工作纪律（每轮对话开头自动读取）

> 本文件是**给 AI 助手读的铁律**，不是给人看的手册。人的手册是 `README.md`。
> 细目在 `.claude/RULES.md`（本文件末尾用 `@` 引用，会被一并注入）。
> ★ 改动本文件 = 改动"每轮都生效的约束"，请当作纪律变更来评审，别顺手改。

---

## 0. 这个项目是什么

从 qBittorrent 抓 OpenCD（皇后）的抓轨资源 → 抽元数据 → 发到 **Musopia（乌托邦）**
与 **CrabPT（蟹黄堡）** → 回加 qB 做种 → 批量编辑已发布页面。
一个常驻容器（`opencd2musopia`）跑 `daemon.py` 串行调度各阶段。

---

## 1. 目录与部署（最容易搞错的）

- **本机仓库**：`D:\Projects\opencd2others\zhuanzhong`（= 本项目地址，**git 只在这里**）。
  源码在 **`src/`**（全部 `.py` 模块）；`Dockerfile` 用 `COPY src/*.py ./` 保持**镜像内 `/app` 平铺**。
- **生产端（NAS）**：`\\iSunker-DS423\docker_ssd\opencd2others`（SMB 路径）
  = NAS 上的 `/volume2/docker_ssd/opencd2others`。
- **★ 生产端只有"容器定义类 + 状态"两类东西**（2026-10-06 起）：
  - 与 `data/` 平级：`docker-compose.yml` / `.env` / `.env.example` / `README.md`；
  - 全部状态与产物在 `data/` 子目录（`data.db` / `daemon_state.json` /
    `daemon_heartbeat` / `daemon_env.json` / `readd_queue.json` / `lsky_token.json` /
    `output_torrents/` / `covers/` / `raw_html/`）。compose 里是 `${STATE_DIR:-./data}:/data`。
  - **不再有** `Dockerfile` / `.dockerignore` / 27 个 `.py` / `tools/` —— 构建已移到本机。
- **★★ 生产端顶层白名单（只有这 5 项允许出现在 NAS 顶层，别再长别的）**：
  `docker-compose.yml` / `.env` / `.env.example` / `README.md` / `data/`。
  ★ **禁止**在生产端留任何 `.bak-*` / 备份 / `.py` / `tools/` / 测试文件 / 工作草稿。
  ★ **禁止**把工作文件（`.py`/`tools/`/测试/文档草稿）直接 scp/SMB 传到 NAS ——
    上生产**只走镜像**（本机 `build-push.sh` → Docker Hub → NAS `pull && up -d musopia`）。
  ★ 可执行核查片段见 `.claude/RULES.md` §A（含阴性对照）。
- **生产端不留 git**：`.git` 只在本机。
- **代码上生产 = 本机构建 + 推 Docker Hub + NAS `pull`**，见 §3。

> ★ 生产端目录**不是** git 仓库，**不要**在生产端跑任何 git 命令。
> ★ 也**不要**在生产端 create/编辑 `.py` —— 那里根本没有 `.py` 了（构建已移到本机）。

---

## 2. 凭据与安全（违反 = 安全事故）

- `.env`（站点 Cookie / qB 密码 / 图床 Key / `QB_API_KEY`）是**生产独有真凭据**：
  - **绝不进 git**（`.gitignore` 已排 `.env` / `.env.*` / `!.env.example`）。
  - **绝不在生产端做 `.env` 备份**（历史因此堆过 20+ 个 `.env.bak-*`；
    2026-10-14 已按白名单清除，**由 §1 的核查片段持续保证**——别再新建）。
  - **绝不打印、绝不进日志、绝不进 `daemon_env.json` 快照**
    （`daemon.ENV_SNAPSHOT_KEYS` 是白名单，凭据键不在其中）。
  - `.env` **不在镜像里**（`.dockerignore` 排 `.env*`，构建上下文不进它）⇒ 改了
    `.env.example` 里的新键，**必须手工**加到 NAS 那份 `.env`（`ERR-ENV-02`）。
- `data.db` 是**只读**（`PRAGMA query_only=1`）；**不要** `cp` 生产库到本机当事实源。
- 单容器：`docker compose up -d` **不带服务名**会带起别的服务 ⇒ 只做
  `docker compose up -d musopia`（**不再有 `--build`**，镜像从 Docker Hub 拉）。

---

## 3. 部署与构建纪律

- **布署 = 本机构建 → 推 Docker Hub → NAS `pull`**（2026-10-06 起）：
  1. 本机：`bash build-push.sh`（会先跑 `tools/build-preflight.sh` 自检 COPY 源/行尾，
     再 buildx 推 `isunker/opencd2others:latest` + `:<git-sha>`；公开库）。
  2. NAS：`cd <compose目录> && /usr/local/bin/docker compose pull && \
     /usr/local/bin/docker compose up -d --remove-orphans musopia`（**只 musopia**）。
  - ★ `restart` 跑的是旧镜像，无效；`up -d` 不带 `--build`（没 `build:` 了，只能 pull）。
- **顺序纪律**：NAS 上 `pull` 必须在**任何** `--profile tools run` **之前** ——
  `docker compose run` **不会**自动 pull，会用本地旧镜像（症状同 `ERR-DOCKER-01`）。
- 验证跑的是**新镜像**（别用"push 过"推"NAS 上生效了"，`ERR-DOCKER-09` 同族）：
  `docker inspect opencd2musopia --format '{{.Image}}'` 与
  `docker buildx imagetools inspect isunker/opencd2others:latest` 的 digest 对比。
- **回滚**：compose 里 `image:` 换成 `:<上一个 git-sha>` tag 再 `pull && up -d`
  （sha tag 是本机构建带来的净收益，旧模型只有一个会被就地覆盖的 `latest`）。
- 验证**镜像里**那份：`docker exec opencd2musopia python <模块> --selftest`。
- NAS SSH：`ssh -p 36688 iSunker@192.168.0.7`，私钥显式给
  `C:/Users/艾森克/.ssh/id_ed25519`（`$HOME` 有 mojibake，写 `~` 会失败）。
  NAS 用户**在 docker 组** ⇒ 用 `/usr/local/bin/docker`，**不用 sudo**。
  ★ 时区：本机比 NAS 慢 8 小时；NAS = CST。
- ★ 旧 `deploy.sh`（SMB 白名单同步）**已弃用**，只保留 `NEVER` 名单与教训注释
  （`bash deploy.sh --list-never` 可打印）；别再指望它同步文件。

---

## 4. 改代码的固定动作

1. **改完必跑**：`python src/run.py --selftest`（含接线断言）。
2. 改了 `Dockerfile` 的 `COPY` 行 / 加了新模块 ⇒ 构建前会有 `tools/build-preflight.sh`
   查"Dockerfile 说要 COPY 的源在本机存在吗"（缺了**构建不会报错**、要到跑阶段才
   `ModuleNotFoundError`——本仓库已踩过 `qb_client.py`、`readd_queue.py` 两次）。
3. 涉及计数/自检项数时：`python tools/gen_counts.py` 重新生成 `counts.json`
   （**别手改 `counts.json`**，它是产物）。
4. 本机跑自检需要 dummy env（`config.require_env` 会 `sys.exit`）：
   ```bash
   export QB_PASS=x MUSOPIA_COOKIE=x MUSOPIA_PASSKEY=x PICGO_API_KEY=x \
          OPENCD_COOKIE=x DATA_DIR="$PWD/.scratch/selftest-data"
   mkdir -p "$DATA_DIR"
   ```
5. **判据必须先拿已知样本校验一次**（阴性对照）：新写的断言要能在一个**已知会红**
   的输入上报红，否则它可能永远为真（`ERR-META-01`：失效的检测比不检测更危险）。

---

## 5. 文档纪律

- `SESSION-NOTES.md`：**只增不改**，逆序（最新在最上面），改就追加一节。
  ★ **待办也写在这里**（某节里切分成小任务，每条写死判据+依赖，见续 102 / 续 106）——
  **不另建 TODO/TASKS 文件**：本项目的历史体制就是"节内待办"。
- `ERRATA.md`：**只写教训 + 根因，不写日期、不写会变的数字**；每条必须是一个
  `### ERR-XX-NN` 标题（`tools/doc-audit/audit.py`，原 `02_refs.py`，靠它解析引用）。
- `README.md`：人的操作手册，**是事实源**；改了行为要同步改它（`ERR-META-04`）。
- 会变的量（库条数、自检项数、阶段周期）**一律不写死在文档里**——看 `counts.json`。

---

## 6. 其他铁律

- **不主动 `git commit`**，除非用户明确要求。要提交时：若在默认分支先开分支。
- 别在生产端跑 `git`、别在生产端编辑 `.py`（那里没有 `.py` 了）。
- 用户已明确**不做**的事：不修 `daemon` 内存泄漏、不管 `QB_API_KEY` 轮换。

@.claude/RULES.md
