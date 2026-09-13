# Musopia 自动化发布 / 种子页面编辑

从 qBittorrent 抓取 OpenCD 抓轨资源，自动生成种子、提取元数据、发布到 Musopia，
并支持批量编辑已发布种子页面的**主标题 / 副标题 / 简介（封面图床 + 曲目 + 抓轨日志）**。

```
qBittorrent(:3020, 分类 opencd)
     │  扫描已完成的抓轨目录
     ▼
  run.py ── 抽元数据（封面/CUE/抓轨日志）→ 生成 .torrent → 写 data.db
     │
     ▼
 publish.py ── 发到 Musopia(takeupload) ── 回加 qB 做种
     │
     ▼
 sync_site.py ── 回填 site_torrent_id（编辑功能的前置条件）
     │
     ▼
 update_metadata.py ── 批量改已发布页面的标题/副标题/简介(takeedit)
     │
     ▼
     站点上 250+ 个页面
```

抓取 → 发布这一段是「一次性」的（每个资源只走一遍），真正的重头是**后续反复批量编辑**
已发布的 250+ 个页面：站点的简介结构变了、封面丢了、副标题想统一改，都要能重跑。

---

## 我要做什么 → 看哪节

| 我想… | 看哪节 |
|---|---|
| 搞清这东西干嘛的 | 上面这段 + **目录结构** |
| 从零在本机跑起来 | **部署步骤** + **验证清单** |
| **改页面渲染**（标题 / 副标题 / 简介 / 日志 / 曲目） | **页面内容渲染** ← 发布和编辑共用一处，改这里两边同时生效 |
| **批量改已发布的 250+ 个页面** | **编辑已发布页面** + **更新路线** |
| 改完代码怎么测 | **改完代码怎么测（Docker）** |
| **把代码推到 NAS 生产** | **部署与同步（deploy.sh）** |
| 我卡住了（页面没图 / 记录被跳过 / 容器 unhealthy） | **排障** |
| 搞清调度为什么是单容器 | **为什么是单容器 + 自研调度** |
| **下一步该做什么** | **当前状态与下一步** |
| 想看完整决策理由和踩过的坑 | **SUMMARY.md** |
| 想看走过的弯路清单 | **走过的弯路.md** |

> **三份文档怎么分工**：
> **README = INFO 层**（操作手册：命令、步骤、症状→解法）；
> **SUMMARY = DEBUG 层**（完整过程、踩坑、决策理由）；
> **走过的弯路.md** = 从两者里抽出来的**归因清单**（只记「错在哪、根因是什么」，不复述操作）。
> 同一事实**只在一处维护**，跨文档用「详见 SUMMARY §N」/「详见走过的弯路 #N」单向指路 —— 防止两边漂移。

---

## 目录结构

```
musopia_script2/                    # NAS 部署目录（compose 就放这里，./ 即指它）
├─ docker-compose.yml        86 行  # musopia 常驻 + tools/once 一次性任务
├─ Dockerfile                32 行  # 代码 COPY 进镜像（不是 bind mount）
├─ .env                             # 站点 Cookie / qB 密码 / 图床 Key（唯一密钥来源，绝不进仓库）
├─ .env.example                     # 占位符模板（全是 changeme）
├─ config.py                 60 行  # 统一配置；密钥缺失直接终止，不留"看起来能跑"的默认值
├─ db.py                    254 行  # ★唯一 schema 来源 + 自动迁移 + 所有查询
├─ descr.py                 255 行  # ★副标题 / BBCode 简介 / 站点下拉项推断（发布与编辑共用）
├─ assets.py                340 行  # 封面查找、图床上传、日志与 CUE 解析
├─ musopia.py               370 行  # 站点客户端（登录检查、上传列表、读改写式编辑）
├─ run.py                   233 行  # 扫描 qB → 提元数据 → 生成种子 → 入库
├─ publish.py               176 行  # 发布新种子 + 回加 qB 做种
├─ update_metadata.py       263 行  # ★批量编辑已发布页面
├─ sync_site.py             119 行  # 回填 site_torrent_id（编辑的前置条件）
├─ backfill_covers.py       144 行  # 补历史缺失的封面
├─ daemon.py                371 行  # ★单容器串行调度五个阶段
├─ check_db.py               53 行  # 状态速览，排查问题先跑这个
├─ healthcheck.py            55 行  # 容器健康检查（读写心跳文件）
├─ deploy.sh                        # ★本地→NAS 白名单同步（开发工具，不上 NAS）
└─ README.md / SUMMARY.md / 走过的弯路.md
```

**运行时状态**（`.gitignore` 覆盖，不随代码走）：

```
data.db                     # SQLite，254 条种子记录 + 发布状态 + 站点 ID
output_torrents/            # 生成的 .torrent
daemon_state.json           # 各阶段 last_run / last_ok（调度进度）
daemon_heartbeat            # 纯 unix 时间戳，daemon 每轮刷新
```

> 这些**放在脚本目录**而不是命名卷，是因为 compose 里是 `${STATE_DIR:-.}:/data`：
> SMB 上直接看得到、能备份、出问题能直接查库。那份已经回填好 254 个站点 ID 的
> `data.db` 就在这里，**不需要任何迁移**。

---

## 部署步骤（分阶段，每步可独立验证）

> 前置：把本目录内容整个放到 NAS 部署目录（本文档举例用 `/volume2/docker_ssd/musopia_script2`）。
> `cp .env.example .env`，然后填 `.env`：至少 `QB_PASS`、`MUSOPIA_COOKIE`、`MUSOPIA_PASSKEY`、`PICGO_API_KEY`。
> **这四个缺失时 `config.py` 会直接终止程序**（不留默认值，见「安全」）。

### Phase 0 — 空跑一轮，绝不改站点

```bash
cd /volume2/docker_ssd/musopia_script2
DRY_RUN=true docker compose --profile tools run --rm once
```

`once` 跑完一轮全部阶段就退出，配 `DRY_RUN=true` 做**无副作用演练**。确认日志没报错再往下。

### Phase 1 — 看清现状

```bash
docker compose --profile tools run --rm tools python check_db.py
```

### Phase 2 — 回填站点种子 ID（没有它就无法编辑，必须先做）

```bash
docker compose --profile tools run --rm tools python sync_site.py            # 预览匹配结果
docker compose --profile tools run --rm tools python sync_site.py --apply    # 写库
```

### Phase 3 — 补封面（可选，但没封面的页面编辑后也不会有图）

```bash
docker compose --profile tools run --rm tools python backfill_covers.py           # 预览
docker compose --profile tools run --rm tools python backfill_covers.py --apply   # 上传图床并写库
```

### Phase 4 — 编辑页面（默认 dry-run，永远先预览）

```bash
docker compose --profile tools run --rm tools python update_metadata.py              # 预览全部
docker compose --profile tools run --rm tools python update_metadata.py 14611 --apply --verify   # 单条试水
docker compose --profile tools run --rm tools python update_metadata.py --apply --verify         # 全量提交
```

### Phase 5 — 常驻

```bash
# .env 里 DRY_RUN=false
docker compose up -d --build
```

日常新种子流程：`run.py` → `publish.py`（Phase 5 的 daemon 会自动做）。

---

## 验证清单

| 验证什么 | 怎么验 | 期望 |
|---|---|---|
| 配置齐了 | `docker compose --profile tools run --rm tools python check_db.py` | 能连库、打印状态，不报缺环境变量 |
| 渲染对不对 | 见「改完代码怎么测」的第 ① 步 | `全 3 曲` / `2CD · 全 3 曲` / 含 `[spoiler=LOG][code]` |
| 站点 ID 回填了 | `check_db.py` | 不再有大量 `site_torrent_id` 为空的记录 |
| 编辑不误伤 | `update_metadata.py 14611 --apply --verify` | 回读校验通过；站点上 `medium/codec/team/tags` **没变** |
| 容器活着 | `docker compose ps` | `healthy` |
| 本地与生产一致 | `bash deploy.sh`（本地跑） | `✓ 生产与本地一致，无需同步` |

---

## 页面内容渲染（descr.py）

站点简介结构固定，**发布(takeupload)与编辑(takeedit)共用** `build_form_fields()` 一处渲染，
改一处对新种子和已发布的 250+ 个页面**同时生效**：

```
封面图（[center][img]…）
  ↓
曲目列表（[quote][b]曲目列表：[/b] …）
  ↓
抓轨日志（折叠代码，见下）
```

### 抓轨日志用折叠代码，不再塞进 `[quote]`

旧版把整段 EAC/XLD 日志放进 `[quote]`，页面被撑得很长。现在渲染成 NexusPHP 折叠块：

- 单 CD：`[spoiler=LOG][code] …日志原文… [/code][/spoiler]`
- 多 CD：每碟一块 —— `[spoiler=CD1 LOG][code]…[/code][/spoiler]` `[spoiler=CD2 LOG][code]…[/code][/spoiler]`
  （碟号从 CD 子目录名 / 日志文件名正则识别，识别不出就按路径顺序编号）

### 副标题不再和主标题重复

主标题已是 `歌手 - 专辑 年份 - 格式 整轨 - OpenCD`，副标题改为客观的曲目/碟数信息，
并在**渲染时重算**（不是只在扫描时算好存库），因此已发布页面重新编辑也会更新：

- 单碟：`全 N 曲`
- 多碟：`DCD · 全 N 曲`（如 `2CD · 全 24 曲`）
- 数不出曲目时回落到库里旧值/种子名，**不硬造残缺文本**

### 曲目列表兜底

有 CUE 就聚合所有 CUE 的 `TITLE`（多 CUE 加 `【CD1】/【CD2】` 分段）；
分轨专辑无 CUE 时用音频文件名（去扩展名）兜底，避免「曲目为空」。

> 简介不再写「专辑名称/副标题/原始名称」这类与页面顶部表格重复的信息，
> 也不再追加「由自动化发布程序生成。」署名。

### 多碟日志的存储

数据库列 `logs_json`（`db.py` schema，`init_db()` 自动给旧库补列）：多碟日志序列化存此；
`log_content`/`log_filename` 仍保留**首个**日志供兼容显示/预览。
`assets.read_logs()` 返回全部日志，`assets.read_log()` 是取首个的兼容封装。

---

## 编辑已发布页面（update_metadata.py）

### 编辑采用「读表单 → 覆盖指定字段 → 整表提交」

`takeedit.php` 是**整表覆盖**语义：没提交的字段会被站点当空值处理。旧版硬编码了一份
字段表，把 `medium_sel[4]` / `audiocodec_sel[4]` / `team_sel[4]` 填 0、`tags` 完全不提交，
于是**每编辑一次就把这些站点属性抹掉一次**。

现在先 `GET edit.php` 把现值读出来当基线，只改需要改的，其余原样回填。副作用是零。

### 默认 dry-run

改站点上 250+ 个页面**不可撤销**，`--apply` 是显式动作，批量时还要输入 `yes` 二次确认。

### 不自动改匿名开关

dry-run 时发现默认填 `anonymous=1` 会把 254 个非匿名种子悄悄改成匿名。
这不是需求的一部分，所以改为**不提交该字段**（即沿用站点现状），需要匿名请显式加 `--anonymous`。

### 增量编辑

daemon 的 edit 阶段带 `--stale-only`，只处理
`site_synced_at IS NULL OR updated_at > site_synced_at` 的记录。
没有它，每轮都要把 250+ 个页面全部拉一遍 `edit.php` 做对比，纯属拿站点带宽换空转。

---

## 封面绝不重复上传，丢了会自动找回（三层防护）

1. `run.py` 对已发布（`site_status='EXISTS'`）的任务直接跳过，不再因本地 `.torrent`
   文件被清理而整个重做；重做场景下库内已有 `cover_url` 就直接复用。
2. 图床报 `Duplicated upload` 400 时，`assets.py` 会尝试从错误响应里**回收已有图片的 URL**，
   而不是把「上传失败」当成「没有封面」写回空值。
3. `update_metadata.py` 发现本地 `cover_url` 为空时，先读站点表单从现有简介里
   把封面 URL **抠回来**（回写本地库修复历史数据），再渲染新简介提交 ——
   避免用空封面渲染 descr 把站点上仅剩的封面也抹掉。

---

## Docker 全自动运行

一个常驻容器跑 `daemon.py`，按各自周期**串行**调度五个阶段：

```
scan（扫 qB、抽元数据、做种）
  ↓
publish（发新种子）
  ↓
sync（回填 site_torrent_id）
  ↓
covers（补历史缺失封面）
  ↓
edit（改标题/副标题/简介，--stale-only 增量）
```

### 部署

```bash
cd /volume2/docker_ssd/musopia_script2

# 1. 先空跑一轮：只预览，绝不改站点
DRY_RUN=true docker compose --profile tools run --rm once

# 2. 看日志没问题后，改 .env 里 DRY_RUN=false，然后常驻
docker compose up -d --build

# 3. 观察
docker compose logs -f musopia    # 或 docker logs -f musopia
docker compose ps                 # 看 healthy 状态
```

### ⚠️ 改了代码之后怎么生效

代码是 `COPY` 进镜像的（不是 bind mount），所以：

```bash
docker compose restart            # ❌ 只重启容器，跑的还是旧镜像里的旧代码
docker compose up -d --build      # ✅ 重新构建镜像并替换容器，新代码才生效
```

`restart` 后看不到新行为、日志里没有新输出，**九成是这个原因**。

### 看日志

```bash
docker logs -f musopia            # 容器名是 musopia（compose 里 container_name 定死的）
docker logs --tail 100 musopia    # 只看最近 100 行
docker compose logs -f musopia    # 等价写法
```

`docker logs` 后面**必须跟容器名**，不带参数会报 `requires exactly 1 argument`。

### 清理孤儿容器

如果日志里出现 `Found orphan containers ([musopia_publish musopia_scan])`，
说明早期按服务拆分部署的旧容器还在跑 —— 它们和 daemon **并发写同一个 SQLite**，
正是 `database is locked` 的来源。清掉：

```bash
docker rm -f musopia_publish musopia_scan
# 或者下次 up 时顺带清理
docker compose up -d --build --remove-orphans
```

### 为什么是单容器 + 自研调度，而不是五个 cron

五个阶段共用一个 SQLite。多容器各自定时必然出现并发写（`database is locked`），
还会「发布还没跑完就开始编辑」，读到半成品数据。串行调度天然规避这两个问题，
顺带省掉 cron 在容器里的 PID 1 / 环境变量继承 / 日志重定向三个坑。

### 关键调度参数（都在 `.env`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DRY_RUN` | `false` | `true` = 全阶段只预览，自动摘掉 `--apply` |
| `ENABLE_<阶段>` | `true` | 单独关某个阶段，如 `ENABLE_PUBLISH=false` |
| `<阶段>_INTERVAL` | 见 `.env` | 周期秒数。`sync` 一天一次够了 |
| `EDIT_MAX_PER_CYCLE` | `100` | 每轮最多改几个页面 |
| `EDIT_RATE_PER_MIN` | `10` | **每分钟处理几个种子**，见下 |
| `EDIT_REQUEST_INTERVAL` | `1` | 两次 HTTP 请求最小间隔 |
| `RUN_ON_START` | `false` | 启动就跑一轮，不等第一个周期。现设为 `false`：重启/重建容器不会马上敲站点，按 state 文件里记录的各阶段周期到期再动 |
| `MAX_BACKOFF` | `21600` | 连续失败的最大退避 |
| `ALLOW_EMPTY_DB` | `false` | **安全阀，别乱开**，见下 |

### 编辑限速：每分钟 10 个

```bash
# 手动跑：每分钟 10 个，一直跑到 250 条全部处理完
docker compose --profile tools run --rm tools \
  python update_metadata.py --stale-only --apply --yes --rate 10
```

`--rate` 按**种子**计，`--interval` 按**HTTP 请求**计，两者不是一回事：一个种子要发
2~3 个请求（读 `edit.php` → 提交 `takeedit.php` → 可选的详情页校验），所以只调
`--interval` 是算不准每分钟处理量的。想控制「每分钟改几个页面」就用 `--rate`。

限速用的是**绝对时间基准**（第 N 条在 `起点 + (N-1)×6秒` 才开始），不是每轮 `sleep(6)`。
后者在站点响应慢的时候会把延迟累加上去，实际速率越跑越低。

默认配置下 `EDIT_INTERVAL=21600` + `EDIT_MAX_PER_CYCLE=100` + `EDIT_RATE_PER_MIN=10`：
每 6 小时一轮，一轮最多 100 个但受限速约束实际跑 10 分钟左右。
250 条积压大约 3 轮清完，之后每轮只处理真正有变化的少数几条。

### 三个必须知道的保护机制

**1. 空库拒绝启动。** 如果 `/data` 挂错或被重建，库就是空的，`run.py` 会把 qB 里
所有种子当成新任务，`publish.py` 紧接着把已经发过的资源**重复发一遍**，站点上出现
几百个重复种子且无法批量撤回。所以 daemon 启动时发现「库是空的 + 启用了 publish」
会直接拒绝启动。确认是全新部署时才设 `ALLOW_EMPTY_DB=true`。

**2. 编辑是增量的。** 见「编辑已发布页面 → 增量编辑」。

**3. `save_task()` 只在值真变了才写 `updated_at`。** 这条和上一条是一套的：
`scan` 每 30 分钟重扫一遍 qB，原先无条件刷新 `updated_at`，于是即使内容一个字没变，
254 条记录也会**全部重新变成 stale**，`edit` 阶段每轮把整站页面重新拉一遍 —— 按
10 个/分钟算，每 30 分钟里有 25 分钟在做无用请求。现在 upsert 带
`WHERE 任一字段 IS NOT excluded.该字段`，无变化就整行不动。

用 `IS NOT` 而不是 `!=`：SQLite 里 `NULL != 'x'` 结果是 `NULL`（被当假），
会漏掉「字段从空补成有值」这类最常见的变化 —— 比如 `log_content` 首次被填上。

---

## 改完代码怎么测（Docker）

代码是 `COPY` 进镜像的，**改完必须先重建镜像**，`docker compose restart` 跑的还是旧代码：

```bash
docker compose build
```

下面用 `tools` profile 的一次性容器，跑完即删（`--rm`），不影响常驻的 `musopia`：

```bash
# ① 渲染/副标题单测（不连站点、不连 qB，最快）
docker compose --profile tools run --rm -T tools python - <<'PY'
import descr
tl = """a
b
c"""
print(descr.derive_small_descr(tl, 1))            # 期望: 全 3 曲
tl2 = """【CD1】
a
b
【CD2】
c"""
print(descr.derive_small_descr(tl2, 2))           # 期望: 2CD · 全 3 曲
print(descr.build_descr(log_content="EAC LOG ..."))   # 含 [spoiler=LOG][code]…[/code][/spoiler]
print(descr.build_descr(logs=[{"label":"CD1 LOG","content":"A"},
                              {"label":"CD2 LOG","content":"B"}]))
PY

# ② 重扫回填 logs_json + 曲目（连 qB、写本地库、不碰站点）
docker compose --profile tools run --rm tools python run.py --recheck --no-upload-cover --limit 3

# ③ 发布预览（dry-run，看日志是折叠块）
docker compose --profile tools run --rm tools python publish.py --dry-run --limit 3

# ④ 编辑预览（默认 dry-run，看副标题/简介 diff）
docker compose --profile tools run --rm tools python update_metadata.py --limit 3

# ⑤ 单条试水并回读校验（换成真实的 site_torrent_id）
docker compose --profile tools run --rm tools python update_metadata.py 14611 --apply --verify
```

> 已发布的**分轨**旧记录曲目为空、**多 CD** 旧记录 `logs_json` 为空（会被当 1 碟）。
> 要让它们副标题正确显示，需先全量重扫回填、再批量编辑：
>
> ```bash
> docker compose --profile tools run --rm tools python run.py --recheck --no-upload-cover
> docker compose --profile tools run --rm tools python update_metadata.py --stale-only --apply --yes --rate 10
> ```

确认无误后让常驻容器生效：`docker compose up -d --build`（**不是** `restart`）。

---

## 部署与同步（deploy.sh）

本机改完代码怎么送上去 —— 见 **deploy.sh**，它做的是**白名单同步**：

```bash
bash deploy.sh                     # 预览差异（默认 dry-run）
bash deploy.sh --apply             # 写入（自动先备份到 .deploy-backup/<时间戳>/）
bash deploy.sh --rollback          # 回滚到最近一次备份
```

设计要点：

- **白名单 = `Dockerfile` 的 `COPY` 行** + `Dockerfile`/`docker-compose.yml` 本身。
  脚本还会**反向自检**：从 `Dockerfile` 解出实际要 `COPY` 的文件逐个核对，漏一个就拒绝部署。
- **`.env` / `data.db` / `daemon_state.json` / `daemon_heartbeat` / `output_torrents` 永不在白名单里**，
  脚本开头有显式断言（混进去直接退出）。
- 写入走「临时文件 + `mv`」**原子替换**。
- 目标路径读 `.deploy.nasrc`（已 gitignore），也可用 `DST=` 临时覆盖 —— **外部 `DST` 优先**。

> ⚠️ **deploy.sh 只负责把文件放过去，不重建容器。** NAS 的 SSH 没开，本机不能在
> NAS 上执行命令，所以脚本结尾会把 NAS 上要手动跑的命令打出来：
> `sudo docker compose up -d --build`（**不是** `restart`）。

---

## 常用参数

```bash
# 只改副标题，不动标题和简介
python update_metadata.py --apply --fields small_descr

# 只处理本地已有封面的记录
python update_metadata.py --apply --only-cover

# 保留站点现有的载体/编码，不自动推断
python update_metadata.py --apply --no-autodetect

# 放慢速度
python update_metadata.py --apply --interval 5
```

### 手动执行单次任务

```bash
docker compose --profile tools run --rm tools python check_db.py
docker compose --profile tools run --rm tools python sync_site.py --apply
docker compose --profile tools run --rm tools python update_metadata.py 14611 --apply --verify
```

`network_mode: host` 下容器直接共享宿主机网络栈，所以 qB 用宿主机自己的地址
（`QB_HOST=http://127.0.0.1:3020`）—— host 网络里没有 `host.docker.internal` 这条 DNS 记录。

---

## 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| `Cookie 已失效` | 更新 `.env` 里的 `MUSOPIA_COOKIE`。各阶段会指数退避，不会疯狂重试 |
| 启动即报「缺少必需的环境变量 X」 | `config.py` 的设计：密钥不留默认值。补 `.env` |
| 容器起来就退出，日志写「拒绝启动」 | 空库保护触发了，先确认 `/data` 挂对；详见「三个必须知道的保护机制」 |
| 某条记录「被跳过」 | 缺 `site_torrent_id`，跑 `sync_site.py --apply` |
| 编辑后页面没有图 | 本地 `cover_url` 为空时 edit 阶段会自动从站点简介回收封面；站点也没有的，跑 `backfill_covers.py --apply` 补齐后 `set_cover` 会刷新 `updated_at`，记录重新变 stale，下轮自动把封面写进简介 |
| 日志出现「从站点简介回收」 | 历史数据修复：本地 `cover_url` 曾被旧版 bug 抹空，现已从站点页面找回并回写库 |
| 校验提示「本地无封面」 | 本地和站点都没有封面，不是编辑失败。跑 `backfill_covers.py --apply` 补齐 |
| `找不到 takeedit 表单` | 种子已被删除，或当前账号无编辑权限 |
| 提示「无需修改」 | 站点内容已和本地一致，属正常（脚本可安全重复运行） |
| 改了本地数据但 edit 阶段说没任务 | `--stale-only` 看 `updated_at`。注意 `save_task()` 只在**值真的变了**时才刷新它；手改库请自己带上 `updated_at=CURRENT_TIMESTAMP` |
| 容器 unhealthy | 心跳停了。`docker compose logs musopia` 看卡在哪个阶段 |
| 想立刻重跑某阶段 | 删掉 `daemon_state.json` 里对应的 `last_run`，或整个文件删掉重启 |
| 改了代码但行为没变 | `restart` 不重新构建镜像。用 `docker compose up -d --build` |
| `database is locked` | 有别的进程在并发写库。检查是否有孤儿容器（`docker ps`），见「清理孤儿容器」 |
| `docker logs` 报 requires exactly 1 argument | 后面要跟容器名：`docker logs -f musopia` |

---

## 安全

- **密钥只来自 `.env`**，且 `.env` / `.env.*` 在 `.gitignore` 里（`!.env.example` 除外）。
- `config.py` 对四个真密钥（`QB_PASS` / `MUSOPIA_COOKIE` / `MUSOPIA_PASSKEY` / `PICGO_API_KEY`）
  用 `require_env()`：**缺失直接 `sys.exit`**，源码里不给「看起来能跑」的真实默认值 ——
  免得悄悄拿一个可能已过期/泄露的旧值继续跑。
- `.env.example` 里全是 `changeme` 占位符，可安全提交。
- `deploy.sh` 的白名单**永不包含** `.env`，且有断言兜底。

---

## 当前状态与下一步

**状态（2026-09-13）**：生产在 NAS `/volume2/docker_ssd/musopia_script2` 常驻运行，
254 条记录、站点 ID 已回填。本机在 `D:\Projects\musopia\zhuanzhong` 有一份完整开发副本。
本文档与 `SUMMARY.md` / `走过的弯路.md` 由本轮补齐。

**✅ 本轮已了结**：

- **工作区行尾已统一为 LF** —— 被跟踪的 22 个文件现在全部是 LF。
  ★ **修法有坑**：`git checkout-index -a -f` **无效** —— 它跳过「索引与工作区一致」的文件，
  而 `git status` 干净恰恰就是这种情况，所以它一个文件都不重写（实测跑完 CR 数纹丝不动）。
  必须**先删再从索引恢复**：
  ```bash
  git ls-files -z | xargs -0 rm -f && git checkout -- .
  ```
  索引里本来就是 LF，所以这一步**不产生 commit**。未跟踪的 `.env` / `data.db` 不受影响。
- **副作用：本地与 NAS 的字节不再相同** —— 本地是 LF、NAS 仍是 CRLF，
  于是 `bash deploy.sh` 现在会报 **11 个文件有差异**（10 个纯行尾差异 + `README.md` 内容重写）。
  这不是错误，是一次**待同步**；同步后 NAS 也会统一成 LF。

**🔴 下一步（按优先级）**：

1. **推送本地 commit 到 `origin/main`** —— `1a8b1fe` / `ebbffcc` / `fec1182` 目前只在本地。
   推之前先核实 `.env` 没被跟踪（本机这份是**从 NAS 拷来的真凭据**）。
2. **让 deploy.sh 走一遍真实流程** —— 目前只在沙箱里测过，没真正部署过。
   顺带把上面那 11 个文件同步上去（含行尾统一），之后记得在 NAS 上
   `docker compose up -d --build`。不执行也不影响运行 —— NAS 现状本来就是能跑的。

详见 **SUMMARY.md**。
