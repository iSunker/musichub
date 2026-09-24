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
| 想看踩过哪些坑、根因是什么 | **ERRATA.md**（`ERR-*` 表：一句话教训 + 根因） |
| **想知道各种数现在是多少** | **`counts.json`**（自动生成，别手改；`python tools/gen_counts.py` 重新生成）|

> **两份文档 + 一份生成物怎么分工**：
> **README = 操作手册**（唯一事实源：命令、步骤、症状→解法、为什么这么做）；
> **ERRATA.md = 教训索引**（每条坑一个 `ERR-*` 可检索 ID + 根因，**只有表、不含日期进度数字**）；
> **`counts.json` = 会动的量的真值**（自检项数、库条数、各阶段周期、白名单…全部自动采集）。
> ★ **会动的数字一律不进散文** —— 它就住在 `counts.json` 里，由 `python tools/gen_counts.py` 生成。
> 这条纪律的由来见 `ERRATA.md` 的 `ERR-META-03`（"投影重抄了事实源的口径"）。

---

## 目录结构

```
musopia_script2/                    # NAS 部署目录（compose 就放这里，./ 即指它）
├─ docker-compose.yml               # opencd2musopia 常驻 + tools/once 一次性任务
├─ Dockerfile                       # 代码 COPY 进镜像（不是 bind mount）
├─ .env                             # 站点 Cookie / qB 密码 / 图床 Key（唯一密钥来源，绝不进仓库）
├─ .env.example                     # 占位符模板（全是 changeme）
├─ config.py                        # 统一配置；密钥缺失直接终止，不留"看起来能跑"的默认值
├─ db.py                            # ★唯一 schema 来源 + 自动迁移 + 所有查询
├─ descr.py                         # ★副标题 / BBCode 简介 / 站点下拉项推断（发布与编辑共用）
├─ assets.py                        # 封面查找、图床上传、日志与 CUE 解析
├─ musopia.py                       # 站点客户端（登录检查、上传列表、读改写式编辑）
├─ run.py                           # 扫描 qB → 提元数据 → 生成种子 → 入库
├─ publish.py                       # 发布新种子 + 回加 qB 做种
├─ update_metadata.py               # ★批量编辑已发布页面
├─ sync_site.py                     # 回填 site_torrent_id（编辑的前置条件）
├─ backfill_covers.py               # 补历史缺失的封面
├─ daemon.py                        # ★单容器串行调度六个阶段
├─ check_db.py                      # 状态速览，排查问题先跑这个
├─ healthcheck.py                   # 容器健康检查（读写心跳文件）
├─ deploy.sh                        # ★本地→NAS 白名单同步（开发工具，不上 NAS）
├─ tools/                           # 开发工具：nas_health.py 巡检 / gen_counts.py 生成真值 /
│                                   #   doc-audit/ 文档只读投影（headings / refs / terms / probes）
├─ README.md                        # ← 本文件：操作手册（唯一事实源，**会上生产**）
├─ ERRATA.md                        # 教训索引（`ERR-*` 表；不上生产）
└─ counts.json                      # ★自动生成：会动的量的真值（不上生产，**永不手改**）
```

> ★ **每份文件多少行、有多少条记录、自检过多少项** —— 这些是**每天都动的量**，
> 一律**不写在这份手册里**（写死等于制造一个必然过时的"事实"）。真值看 `counts.json`：
> `python tools/gen_counts.py` 重新生成；只想看某个数用 `python -c "import json;..."` 取。

**运行时状态**（`.gitignore` 覆盖，不随代码走）：

```
data.db                     # SQLite：种子记录 + 发布状态 + 站点 ID
output_torrents/            # 生成的 .torrent
daemon_state.json           # 各阶段 last_run / last_ok（调度进度）
daemon_heartbeat            # 纯 unix 时间戳，daemon 每轮刷新
daemon_env.json             # ★ 容器内**生效的**白名单开关快照（daemon 每轮写）
```

> 这些**放在脚本目录**而不是命名卷，是因为 compose 里是 `${STATE_DIR:-.}:/data`：
> SMB 上直接看得到、能备份、出问题能直接查库。那份**站点 ID 已回填**的
> `data.db` 就在这里，**不需要任何迁移**。
> ★ 具体条数**不写死在文档里** —— 它是每天都在动的量，写死等于制造一个必然过时的"事实"。
> 要看现在的数：`python tools/gen_counts.py`（生成 `counts.json`，含库条数/自检项数/各阶段周期）。
> 只想深挖某一个探针：`python tools/doc-audit/04_probes.py`。

> ★★ **`daemon_env.json` 是什么、为什么要有它**：真正决定"哪些阶段关着"的是
> **容器内的环境变量**。本机的两个替代来源都拿不到：NAS 那份 `.env` 在 `deploy.sh`
> 的 NEVER 名单里（`ERR-ENV-02`），容器也进不去（SSH 未开）；而 `.env.example` 只是
> **模板**，实测它与生产**不一致**。
> ⇒ 让 **daemon 自己**把生效值落成这个文件（见 `daemon.save_env_snapshot()`），
> 本机巡检脚本读它。**只有一个来源，且是"生效值"而不是"配置文件里写的值"**。
> · 它是**白名单**（`ENV_SNAPSHOT_KEYS`），**不存在**凭据键 ——
>   整个倒 `os.environ` 会把 Cookie / qB 密码写到共享盘上。
> · 新鲜度由它自己的 `generated_at` 与 mtime 说清；**超过 1 小时没刷新就不再当判据**
>   （停在上个月的快照是"传说"，见 `ERR-META-04`）。
> · 按 `.gitignore` 忽略，**不进版本库**（它是运行时产物，不是事实源）。

★★★ **`WARN[0000] buildx: git was not found`**（2026-09-22 **订正：接受它，不修**）：
那行 WARN **不是我们的代码在调 git** —— 它是 **buildx 自己**打的字。
★ 本条**曾经写错**：原先记的是"buildx 算构建上下文时看到 NAS 构建目录里有一份 `.git`，
就去读提交信息，而 NAS 上没装 git ⇒ 加 `.dockerignore` 把 `.git` 排除掉，WARN 即消失"。
**这个归因已被实测推翻**：实测加了 `.dockerignore` 之后构建上下文从 `174.35kB` 掉到 `724B`、
`.git` **确实**不在上下文里了 —— **而那行 WARN 一字不变**。
⇒ `.git` 在不在上下文里与它**无关**。真因是：**本机有 git、NAS 没有**，buildx 在没有 git 的
机器上拿不到 commit 信息，就 WARN 一句。
⇒ **处置：接受它。** 它不是错误、不影响构建结果（构建成功、镜像生成、容器运行都正常），
后果仅止于"镜像里少几条 commit 标签"这类元数据。**别再为它改代码或加开关。**
（教训见 `ERRATA.md` 的 `ERR-DOCKER-07`。★ 这条比那行 WARN 更值钱的是**过程**：
"读起来很像原因"的东西被当成了原因，写进了三处注释和文档 —— `ERR-META-01` 那一族，
**判据要拿已知样本校验一次**。）

> ★ `.dockerignore` **仍然要留**，但理由是**下面这一条**（与那行 WARN 无关）：
> **没有它时，构建上下文 = 整个目录**，而生产目录里有 `data.db`（生产库）、
> `daemon_state.json`、`output_torrents/`、`__pycache__/`，
> 以及**一堆 `.env*` 历史备份**（`.env` / `.env.old` / `.env.pre-fix` /
> `.env.pre-dollar-fix-*` …）—— 它们**全都会被传给 Docker daemon**。
> `.env` 有真凭据（Cookie / qB 密码 / 图床 key）⇒ 那是**凭据外溢**。
> 实测加了它之后上下文 `174.35kB → 724B`，7 份 `.env*` 与 `data.db` 都不再上传。
> ⇒ `.dockerignore` 里凭据按**形状**排除（`.env` + `.env.*`），
> **不枚举具体文件名** —— 枚举就会漏掉下一个备份（实测 NAS 上有 7 个 `.env*`，逐个核过全被盖住）。



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

还有第三种情况：**CUE 在，但它是坏的**。有些 CUE 由**西欧单字节码页（CP1252/ANSI）**
写出，表示不了中日文，于是**每个非 ASCII 字符被替换成一个 `.`** —— 渲染出来就是
一份没人能读的曲目列表，站点会以「請更正曲目列表，使其易於閱讀」打回。
`read_tracklist()` 现在按**每份 CUE** 判退化：退化项占比 ≥ 50%（`DEGENERATE_RATIO`）
的那一碟，改用**该碟目录下**的音频文件名。
★ 判据用**比例**而不是「全是点」—— 像 `Lydia` 这种纯 ASCII 曲名会幸存，
一条幸存就能让「全是点」失效。

还有第四种情况：**CUE 的码页不在 `LOG_ENCODINGS` 里**。这时链路里
「永不失败」的兜底（`gb18030` / `latin-1`）会把它吞下去，调用方收不到任何失败信号。
**这个根因有两个症状，要两条判据才盖得住**：

- **① 解成带私用区的乱码**（U+E000–U+F8FF）。判据 `_has_pua()`：
  见到私用区就认定解码用错了码页。实证 `MONSTER HUNTER WILDS`（真实码页 Big5 系）。
- **② 解成「假汉字」—— 合法汉字，只是没人看得懂**。Shift-JIS 写的日本专辑就是这种：
  原字节 `82a0 82c8 82bd…`（`あなたに夢中`）被 `gb18030` 解成 `偁側偨偵柌拞`，
  **既没有私用区、也没有点号，前两条判据双双失明**。判据 `_mojibake_ratio()`：
  把标题按 `gb18030` **编回原始字节**（只有它是双射），再用 `cp932`/`euc_jp`/`big5`/`cp949`
  严格重解，重解得**成串假名**（≥2 连）就说明真实码页是它。
  分母取**非 ASCII 标题数**（ASCII 标题在任何码页下都一样，用它当分母会稀释比例），
  阈值 `MOJIBAKE_RATIO = 0.25`。实证 4 张日本专辑（`14619` / `14640` / `14740` / `14824`）。

三条判据都是**按每份 CUE** 判定的，坏哪一碟就回退哪一碟，整份回退到音频文件名。
（兜底细节见「曲目列表兜底」一节。）

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

### 匿名开关：编辑不动，发布默认匿名

两条路径**结论相反**，因为风险面不同 —— 别把它们当成一件事：

| | 行为 | 为什么 |
|---|---|---|
| **编辑**（`update_metadata.py`） | **不提交该字段**（沿用站点现状），要匿名显式加 `--anonymous` | `takeedit.php` 是整表覆盖，提交了就是**对存量 254 条页面的覆盖写**。dry-run 时发现默认填 `anonymous=1` 会把 254 个非匿名种子悄悄改成匿名，所以改为不提交 |
| **发布**（`publish.py`） | **默认匿名**（2026-09-17 改），要具名显式加 `--no-anonymous` | 发布的是**新种子，没有存量可伤** —— 上面那条「会作用到存量」的前提在这里不成立 |

```bash
# 具名发布（覆盖默认的匿名）
python publish.py --no-anonymous
```

★ `--anonymous` / `--no-anonymous` 共用 `dest`，argparse 以**最后一次**出现的为准，
所以 `--no-anonymous` 能盖掉 `default=True`；显式写 `--anonymous` 只是冗余无害（向后兼容）。

> ⚠️ 改这次时顺带修掉一处**静默失效**：`publish.py` 里 `descr.build_form_fields(task)`
> **漏传** `anonymous`，于是参数解析出来了却根本没生效。★ 渲染逻辑共用一处（见下节）
> 只保证**函数**一致，**参数仍由每个调用方各传各的** —— 共用不会自动带来一致。

详见上文「匿名开关」。

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

一个常驻容器跑 `daemon.py`，按各自周期**串行**调度六个阶段：

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
  ↓
crawl（扫 qB 反查皇后、抓详情页入库）★ 默认关（ENABLE_CRAWL=false）
```

★ `crawl` 与上面五个是**两条独立流水线**（只共用 `tasks` 表、不互为前置），
放最后只是因为它是新增的、最可能被关掉的那个 —— **与依赖无关**。
它**只入库、不会产生新发布**（闸门①未打通，理由见下面「OpenCD 转载流水线」）。

### 部署

```bash
cd /volume2/docker_ssd/musopia_script2

# 1. 先空跑一轮：只预览，绝不改站点
DRY_RUN=true docker compose --profile tools run --rm once

# 2. 看日志没问题后，改 .env 里 DRY_RUN=false，然后常驻
docker compose up -d --build

# 3. 观察
docker compose logs -f opencd2musopia    # 或 docker logs -f opencd2musopia
docker compose ps                 # 看 healthy 状态
```

### ⚠️ 改了代码之后怎么生效

代码是 `COPY` 进镜像的（不是 bind mount），所以：

```bash
docker compose restart            # ❌ 只重启容器，跑的还是旧镜像里的旧代码
docker compose up -d --build      # ✅ 重新构建镜像并替换容器，新代码才生效
```

`restart` 后看不到新行为、日志里没有新输出，**九成是这个原因**。

> **★ 顺序也有讲究**：`up -d --build` 必须跑在**任何** `--profile tools run` **之前**。
> 三个 service（`musopia` / `tools` / `once`）共用同一个镜像 `musopia-script:latest`
> （compose 里 `x-common` 锚点定的），而 `docker compose run` **只在镜像不存在时才构建** ——
> 镜像没重建就直接 `run --rm tools python run.py --recheck`，跑的是**旧代码**，
> 会白跑一遍还看不出问题（`--recheck` 静默完成，只是结果没变）。

### 看日志

```bash
docker logs -f opencd2musopia      # 容器名是 opencd2musopia（compose 里 container_name 定死的）
docker logs --tail 100 opencd2musopia    # 只看最近 100 行
docker compose logs -f opencd2musopia    # 等价写法
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

### 为什么是单容器 + 自研调度，而不是几个 cron

各阶段共用一个 SQLite。多容器各自定时必然出现并发写（`database is locked`），
还会「发布还没跑完就开始编辑」，读到半成品数据。串行调度天然规避这两个问题，
顺带省掉 cron 在容器里的 PID 1 / 环境变量继承 / 日志重定向三个坑。

### 关键调度参数（都在 `.env`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DRY_RUN` | `false` | `true` = 全阶段只预览，自动摘掉 `--apply` |
| `ENABLE_<阶段>` | `true` | 单独关某个阶段，如 `ENABLE_PUBLISH=false` |
| `PUBLISH_I_AM_HERE` | *(必须显式填)* | **自动发布的第二把钥匙**，见下节。不填 ⇒ daemon 每轮拒绝发布并退避重试（**不会**静默） |
| `<阶段>_INTERVAL` | 见 `.env` | 周期秒数。`sync` 一天一次够了 |
| `EDIT_MAX_PER_CYCLE` | `100` | 每轮最多改几个页面。★ 代码里的兜底值也是 `100`，两处必须一致 —— 否则 `.env` 一旦缺这行键，会**静默**按兜底值慢跑还不报错 |
| `PUBLISH_MAX_PER_CYCLE` | `1` | **每轮最多发几条** ⇒ 决定**日上限** = `(86400/PUBLISH_INTERVAL) × 本值`。★ 兜底是 `0` = **不限**（方向与 `EDIT_MAX_PER_CYCLE` **相反**，缺键会**瞬间发完整个队列**）⇒ 别删这行 |
| `PUBLISH_MIN_GAP` | `0` | **两条发布之间的最小间隔**（秒），只管**突发形态**、**不降日上限**。`0` = 不限。见「发布限速」节 |
| `EDIT_COOLDOWN` | `60` | **有变更待推时**的重跑间隔（秒）；待办清零才回到 `EDIT_INTERVAL`。见下 |
| `EDIT_RATE_PER_MIN` | `10` | **每分钟处理几个种子**，见下 |
| `EDIT_REQUEST_INTERVAL` | `1` | 两次 HTTP 请求最小间隔 |
| `RUN_ON_START` | `false` | 启动就跑一轮，不等第一个周期。现设为 `false`：重启/重建容器不会马上敲站点，按 state 文件里记录的各阶段周期到期再动 |
| `MAX_BACKOFF` | `21600` | 连续失败的最大退避 |
| `ALLOW_EMPTY_DB` | `false` | **安全阀，别乱开**，见下 |

### ★★ 自动发布的两把钥匙（2026-09-24 起）

**发布是本项目唯一不可逆的动作** —— 站点上没有「批量撤回」。所以「能不能自动发」
这件事**由两个独立的开关同时决定**，而不是一个：

| | 钥匙 | 放哪 | 管什么 |
| --- | --- | --- | --- |
| ① | `ENABLE_PUBLISH` | 容器 `.env`（长期） | 这个阶段**排不排**进调度 |
| ② | `PUBLISH_I_AM_HERE` | 同一个 `.env` | 无人值守时**每次执行**都要成立 |

两把钥匙都成立，`publish` 阶段才会真提交。**任一为假 ⇒ 不发。**

改动前的形状（为什么要加第 ② 把）：`daemon` 原先传的是 `publish.py --yes` ——
而 `--yes` 的语义是「跳过交互确认」，也就是**没有人确认**。于是「自动发」和
「人发的」共用同一条路径，唯一区别只是「有没有 TTY」。那不是保险，那是没人。

现在的分工是**按"谁按的确认"**分的：

| 谁 | 命令 | 第二把钥匙 |
| --- | --- | --- |
| daemon（无人值守） | `publish.py --auto` | **必须**通过 |
| 你在终端前手敲 | `publish.py`（可加 `--yes`） | 不校验（人在回路里） |

```
# 钥匙的值就是下面这个字面量；连字符不用转义（compose 只展开 .env 的**值**）
PUBLISH_I_AM_HERE=i-am-here
```

★ **不填 / 填错的后果是"不发"，不是"发了"** —— 这是刻意的失败方向。
表现为：日志里明写「🛑 拒绝自动发布：第二把钥匙没通过」+ 退出码 `3`
⇒ daemon 记连续失败并**指数退避**（不会退化成每 30 分钟空敲一次站点）。
★ 「还没配」与「配错了」**分开报**（`ERR-ENV-02`：缺键最容易被静默当成某种默认值）。
★ 巡检（`tools/nas_health.py`）有一个 `【自动发布】` 段落逐字报这两把钥匙的状态，
**不要去读日志猜** —— 它们都在 `daemon_env.json` 快照里。

⚠️ **已知缺口（别读成已解决）**：它挡的是「**忘了配 / 以为配了**」，**不是**「要作恶」。
拿到了容器命令行的人可以显式去掉 `--auto` 绕过它 —— 同
`--i-have-stopped-the-container` 的定位：**显式声明**，挡的是疏忽不是敌意。

### ★★ 发布限速：两道**互补**的旋钮（2026-09-24 起）

★ 先把最容易读错的一处说清：**这两道管的不是同一件事，别合并成一个"限速"数。**

| 旋钮 | 在哪读 | 管什么 | 算式 |
| --- | --- | --- | --- |
| `PUBLISH_MAX_PER_CYCLE` | `daemon.py` → 传 `--limit N` | **日上限**（量） | `(86400 / PUBLISH_INTERVAL) × 本值` |
| `PUBLISH_MIN_GAP` | `publish.py` 直接读 env | **突发形态**（节奏） | 一批 N 条摊开成 `(N-1) × 本值` 秒 |

**当前生产口径：`PUBLISH_INTERVAL=1800` ⇒ 48 轮/天；`PUBLISH_MAX_PER_CYCLE=1`
⇒ 每天最多 48 条**（改动前是 `5` ⇒ **240 条/天**，对无人值守偏大）。

★★ **`PUBLISH_MIN_GAP` 压不住日总量** —— 这是本节存在的理由：
`5 条 × 120s = 10 分钟 < 30 分钟一轮` ⇒ 它**永远轮不到触发**。
所以它**不降低**上面的 48 条/天，它只是在「一批」内部把节奏摊开。
想降总量请改 `PUBLISH_MAX_PER_CYCLE`，**不要**指望 `PUBLISH_MIN_GAP`。

★ 那它什么时候有用：① 人工 `publish.py --limit N`（`--only` 点名一批）——
那条路**没有** daemon 的 30 分钟轮间隔兜着，改动前 N 条会在几秒内全糊出去；
② 将来若把 `PUBLISH_MAX_PER_CYCLE` 调大，那时才有「一批」可摊。
★ 人工路径**同样受它约束**（不带 `--auto` 也生效）—— 人在终端前，
并不能让站点少挨几下。要临时加速：跑之前 `PUBLISH_MIN_GAP=0 python publish.py ...`。

⚠️ 它与 `musopia.py` 的 `request_interval=1.5`（**单个请求**之间的地板）**不是一回事**：
一条发布 ≈ **3 个站点请求**，`request_interval` 对「一条发完了没有」**完全无感**。
⇒ 两个都留着，**别把其中一个调大了当另一个用**（那正是 `ERR-META-03` 的形状）。

⚠️ **大 `--limit` 的坑**：它是**串行等待**。`STAGE_TIMEOUT=3600` 下，
`PUBLISH_MIN_GAP=120` 时约 **29 条**是上限；再多，daemon 会**中途杀掉**该子进程 ——
已发出去的种子**不受影响**，但汇总与退出码会丢 ⇒ 被记成阶段失败 ⇒ 触发指数退避，
反而拖慢自动发布。`publish.py` 运行前会打印预计耗时，超限时告警。

★ 巡检（`tools/nas_health.py`）的 `【发布速率】` 段把 **48 条/天** 这个结论
**直接算出来印上**，并把依据的两个原始值一起列出 ⇒ 不必再手算
（手算正是 `ERR-META-03` 的产地：算错不报错，只给出一个看着合理的数）。

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

默认配置下 `EDIT_MAX_PER_CYCLE=100` + `EDIT_RATE_PER_MIN=10`：一轮最多 100 个，
受限速约束实际跑 10 分钟左右。

### 有变更就连续跑，没变更才按 6 小时

`EDIT_INTERVAL=21600` 现在只是**没活时**的兜底周期。daemon 每跑完一轮 edit 都会问一次
`db.count_stale()`：还有待办就隔 `EDIT_COOLDOWN`（默认 60s）接着跑，**待办清零才回到长周期**。

于是 250 条积压不再是「3 轮 × 6h ≈ 18 小时」，而是连着跑 ≈ **半小时**清空。
你只要让 `run.py --recheck` 把记录标脏，daemon 自己会在一分钟内跟上 ——
**不必再手动开一次性容器**（那正是上一轮数据风险的入口）。

> ★ **判据不是「还有没有待办」，是「待办有没有减少」。** 14749 那种永远失败
> （`找不到 takeedit 表单`）的记录会一直挂在待办里；只看「还有活」就变成每 60s 敲一次站点，
> **比改动前更糟**。所以每轮比较跑前/跑后的待办数，没减少就退回长周期，
> 日志留下 `⏸️ [edit] 本轮未推进（待办 N → N）`。判据与实现见上文「有变更就连续跑」。

### 四个必须知道的保护机制

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

**4. 编辑的调度看「待办有没有减少」，不看「还有没有待办」。** 见「有变更就连续跑」——
没有这条防呆，**一条永远失败的记录就能把 daemon 变成每 60 秒敲一次站点**，
比定时轮询还糟。

---

## 改完代码怎么测（Docker）

代码是 `COPY` 进镜像的，**改完必须先重建镜像**，`docker compose restart` 跑的还是旧代码：

```bash
docker compose build
```

下面用 `tools` profile 的一次性容器，跑完即删（`--rm`），不影响常驻的 `opencd2musopia`：

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

- **白名单 = `deploy.sh` 里的 `FILES=(...)` 数组**。它含全部 `.py`，**另外还含**
  `Dockerfile` / `docker-compose.yml` / `README.md` / `.env.example`
  —— 所以它**不等于** `Dockerfile` 的 `COPY` 行（旧描述就是这么写错的，2026-09-22 订正）。
  脚本还会**反向自检**：从 `Dockerfile` 解出实际要 `COPY` 的 `.py` 逐个核对，漏一个就拒绝部署。
  ★ 真值（白名单全集 / Dockerfile COPY 全集 / 双向差集）见 `counts.json` 的 `offline.deploy`。
- ★★ **白名单 ≠ 容器内可见 —— 这是两条独立的路，别混（2026-09-19 踩过）**：

  | | 决定 |
  |---|---|
  | `deploy.sh` 的 `FILES` 白名单 | 文件**能不能被部署过去**（`deploy.sh` 读它）|
  | `Dockerfile` 的 `COPY` 行 / compose 的 `volumes` | 跑起来**容器里看不看得见** |

  白名单里有它，只说明「部署会成功」；**不说明容器跑起来找得到它**。
  实测翻车形状：脚本放 `tools/` → 在 NAS 上跑 → 容器报
  `can't open file '/app/tools/publish_probe.py'`（`tools/` 两条都不占）。
  要进容器只有两条路：写进镜像的 `COPY` 行（得重建镜像），或放进
  **compose 根目录**（`${STATE_DIR:-.}` bind mount 成 `/data`，容器里看得见）。
  ★ 连带：`python /data/x.py` 时 Python 只把**脚本所在目录**加进 `sys.path`、**不加 cwd**
  （容器 `WORKDIR` 是 `/app`）⇒ `import config` 直接 `ModuleNotFoundError`，
  脚本得自己 `sys.path.insert(0, "/app")`。
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

# 发布：默认匿名；具名发布要显式关掉（见「匿名开关」）
python publish.py --no-anonymous
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
| 容器起来就退出，日志写「拒绝启动」 | 空库保护触发了，先确认 `/data` 挂对；详见「四个必须知道的保护机制」 |
| 某条记录「被跳过」 | 缺 `site_torrent_id`，跑 `sync_site.py --apply` |
| 编辑后页面没有图 | 本地 `cover_url` 为空时 edit 阶段会自动从站点简介回收封面；站点也没有的，跑 `backfill_covers.py --apply` 补齐后 `set_cover` 会刷新 `updated_at`，记录重新变 stale，下轮自动把封面写进简介 |
| 日志出现「从站点简介回收」 | 历史数据修复：本地 `cover_url` 曾被旧版 bug 抹空，现已从站点页面找回并回写库 |
| 校验提示「本地无封面」 | 本地和站点都没有封面，不是编辑失败。跑 `backfill_covers.py --apply` 补齐 |
| `找不到 takeedit 表单` | 种子已被删除，或当前账号无编辑权限 |
| 提示「无需修改」 | 站点内容已和本地一致，属正常（脚本可安全重复运行） |
| 改了本地数据但 edit 阶段说没任务 | `--stale-only` 看 `updated_at`。注意 `save_task()` 只在**值真的变了**时才刷新它；手改库请自己带上 `updated_at=CURRENT_TIMESTAMP` |
| **改了记录但页面迟迟不更新** | 有变更时 edit 应在 60s 内跟上。若长时间不动，看日志有没有 `⏸️ [edit] 本轮未推进`（卡在某条永远失败的记录上，如缺 takeedit 表单）；否则查 `docker compose logs opencd2musopia` 里 edit 阶段是否在报错（Cookie 失效会走指数退避） |
| 容器 unhealthy | 心跳停了。`docker compose logs opencd2musopia` 看卡在哪个阶段 |
| 想立刻重跑某阶段 | 删掉 `daemon_state.json` 里对应的 `last_run`，或整个文件删掉重启 |
| 改了代码但行为没变 | `restart` 不重新构建镜像。用 `docker compose up -d --build` |
| **站点打回「請更正曲目列表，使其易於閱讀」** | 源 CUE 被**西欧单页码页**写坏（非 ASCII 字符整体变成 `.`），或码页不在 `LOG_ENCODINGS` 里、被兜底解成**私用区乱码**。先看本地 `tracklist` 是不是整片点号/乱码。`read_tracklist()` 已按**每份 CUE** 判退化并回退到音频文件名（见「曲目列表兜底」）|
| **多碟页面出现两个同名 `CDx` 折叠块** | 有张碟的目录名不符合 `CD/DISC/DISK+数字`，补号与已有碟号撞了。已修（补号避开已占用编号），见上文「多碟日志的存储」 |
| `database is locked` | 有别的进程在并发写库。检查是否有孤儿容器（`docker ps`），见「清理孤儿容器」 |
| `docker logs` 报 requires exactly 1 argument | 后面要跟容器名：`docker logs -f opencd2musopia` |

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

**状态（2026-09-14 核 · 2026-09-19 复核）**：生产在 NAS `/volume2/docker_ssd/musopia_script2` 常驻运行，
本机在 `D:\Projects\musopia\zhuanzhong` 有一份完整开发副本。
★ **全部代码文件本机 ↔ NAS 逐字节一致**（2026-09-19 复核：`deploy.sh` 报「生产与本地一致」）。

> ✅ **2026-09-17/18 那笔积压已清（2026-09-19 10:21 部署）**：`publish.py` 匿名默认值、
> `config.py` 两处订正、OpenCD 第 3/4 步全部文件，共 **9 个文件**已随
> `deploy.sh --apply` 写入 NAS，并已 `up -d --build` 重建镜像、
> **实测确认新代码生效**（库列数 17 → 22）。
> ★ 两点仍不成立，别读成"什么都跟上了"：① ~~`ENABLE_PUBLISH=false` **未恢复**~~
> **→ 2026-09-24 已恢复（见本节末那笔）**；
> ② `OPENCD_COOKIE` 的状态**以 NAS 上那份 `.env` 为准**（crawl 已接调度，
>    2026-09-22 起 `ENABLE_CRAWL=false` 默认关 ⇒ 不填也不会白跑一轮）。

**✅ 已了结**：

- **commit 已推 `origin/main`；`deploy.sh` 已真实跑通**（字节级核验一致；`data.db` / `.env` 未动，
  部署前自动备份在 NAS 的 `.deploy-backup/<时间戳>/`）。
- **全树文本规范：被跟踪文件全部 LF，且全都有结尾换行。**
  ★ **修法有坑**：`git checkout-index -a -f` **无效** —— 它跳过「索引与工作区一致」的文件，
  而 `git status` 干净恰恰就是这种情况，所以它一个文件都不重写（实测跑完 CR 数纹丝不动）。
  必须**先删再从索引恢复**：
  ```bash
  git ls-files -z | xargs -0 rm -f && git checkout -- .
  ```
  索引里本来就是 LF，所以这一步**不产生 commit**。未跟踪的 `.env` / `data.db` 不受影响。
- **2026-09-14 文档机制建成**：`README`（操作手册）+ `ERRATA.md`（`ERR-*` 教训表）。
  ★ **2026-09-22 重构**：原 `SUMMARY.md`（DEBUG 层）/ `ENVIRONMENT.md`（检索层）**已删除** ——
  它们的维护成本全在「同一事实抄好几份且互不联动」（见 `ERRATA.md` 的 `ERR-META-03`）。
  现在：**会动的数字 → `counts.json`**（`python tools/gen_counts.py` 生成，**永不手改**）；
  **教训 → `ERRATA.md`**；**操作事实 → 本文件**。同一事实仍然只在一处维护。
- **2026-09-14 `tools/doc-audit/` 只读投影建成**（headings / refs / terms / probes + 基线快照）。
  ★ **投影只读事实源，事实源不读投影** —— 方向由结构强制（事实源是 `.md`，不是可导入模块）。
- **2026-09-22 巡检的"关着"判据改成读容器快照**（`daemon_env.json`，见上「运行时状态」）。
  ★ 起因是一次**真实误报**：`nas_health.py` 曾硬编码 `DISABLED_STAGES={"publish"}`，
  后来改成"默认空集 + `--disabled` 手传"，但`ENABLE_PUBLISH` 的真值**本机根本读不到**，
  于是巡检把「publish 停在 67h」当成了**真异常**报出来 —— 而它其实是
  **`ENABLE_PUBLISH=false`，不动是预期内的**。
  ⇒ 现在由 daemon 落盘、巡检读盘，**判据与真值同源**。判据来源会打在报告里
  （`「关着」的判据来源：容器快照 …（NNNs 前；ENABLE_PUBLISH=False）`），
  快照过期则退回空集并**显式说明**，不静默猜。
- **2026-09-22 订正过的一处错误口径（记下来防复发）**：
  曾有巡检提示词写着「`ENABLE_PUBLISH=**true**`，publish 是开着的、每 30 分钟一跑，
  早期说"publish 已禁用"是**错的**」。**那句本身是错的** —— 实测
  `docker exec opencd2musopia env` 给出 `ENABLE_PUBLISH=false`。
  ★ 教训：**把开关值写死进提示词**，隔一天就会变成反的，
  且它会像真的一样被下游引用。要判开关，**去问容器**，别抄结论。
  ★★ **上面两条里的 `ENABLE_PUBLISH=false` 是当时（2026-09-22）的实测值**，
  **不是现状** —— 2026-09-24 已恢复为 `true`（见本节末那笔）。
  ⚠️ 留在这里是当**教训**用（"判据怎么被写反的"），**读的时候只看它的推理，
  别取它的数字** —— 这正是本条要防的那个坑，写在它自己身上。
- **2026-09-14 调度改动已上生产**（见「有变更就连续跑」）。
- **2026-09-14 `publish` 已关闭**（NAS `.env` 的 `ENABLE_PUBLISH=false`，`18:55` 生效），
  止住「权限抖动时每 30 分钟一轮无效 POST」。三个时刻与复核判据见
  **`ERRATA.md` 的 `ERR-SITE-01`**。
- **✅ 2026-09-24 `publish` 已恢复（自动发布首次真正跑起来）** —— 上面那条「已关闭」
  **是历史**，不是现状。当天做了三件事，顺序**不可颠倒**（次序本身是一条纪律）：
  1. **上代码**（`deploy.sh --apply`）：`publish.py` 加第二把钥匙 + `daemon.py` 改传 `--auto`；
  2. **交钥匙**：NAS `.env` 加 `PUBLISH_I_AM_HERE=i-am-here` + `up -d --build`；
  3. **开总闸**：NAS `.env` 的 `ENABLE_PUBLISH` 改回 `true` + 再 `up -d --build`。
  ★ **③ 必须在 ① 之后**：旧代码跑的是 `publish.py --yes`（**没有**钥匙校验）⇒
  若先开总闸而新代码还没上，旧代码会**立刻无钥匙自动发布**。这是本轮唯一真正危险的一步。
  ★ 实测确认：先手工 `--limit 3` 发了 3 条（站点 ID `17278/17279/17280`，简介回读落地、
  已回加 qB）**人工验看合格后**，才开的自动。**自动发布闸门见上面
  「自动发布的两把钥匙」与「发布限速」两节。**
  ★ 同一轮把**发布限速**改成 `48 条/天` + 条间隔 `120s` —— 起因是实测发现
  一轮会在 7.5 秒内连发 5 条，而日上限是 240 条（详见「发布限速」节）。

**🔴 下一步**（2026-09-14 定稿 · 2026-09-18 销掉第 1 条 · 2026-09-24 销掉第 3 条）：

1. **✅ 已了结（2026-09-18）：`publish` 的退出码说真话了。**
   `publish.py` 现在**只要有失败就以非 0 退出**，于是 daemon 的退避分支真的进得去：
   实测 `n_fail=1..4` 分别等 `60 / 120 / 240 / 360` 分钟（上限 `MAX_BACKOFF`），
   原先卡死的那一行是 `30` 分钟 —— 「一天 240 次无效 POST」降到约 **8 次**。
   ★ **已部署（2026-09-19）**：`publish.py` 在白名单里，已随那次 `deploy.sh --apply`
   上线并重建镜像。
   ★ ~~但**功能尚未启用**：`ENABLE_PUBLISH=false` 还没恢复 ⇒ 这个退避语义
   **要等 publish 重新启用才真正起作用**~~ ⇒ **2026-09-24 已启用，本措辞作废**
   （见上面那笔）。完整归因见 **`ERRATA.md` 的 `ERR-SITE-01`**（已同步订正）。
2. **★ 把提交前的凭据扫描落成 `tools/` 里的脚本。**
   同一个扫描**已经手写过两次**，2026-09-14 那次还被 `ERR-OS-01`（GBK 控制台编不出某些字符）
   当场打断。本项目对这类东西有现成原则：**不做会各自漂的拷贝**
   （`mdwalk.py` 就是为这个而生的）。
3. **✅ 已了结（2026-09-24）：恢复 `publish`。** 实际执行顺序与当时的预案略有出入，
   **以本节末那笔的序号 ①②③ 为准**（关键差别：第二把钥匙要**单独一次**上线，
   即「先代码、后钥匙、最后总闸」三趟，不能合并成两趟）。
   当时写的两条注意事项**仍然有效**，抄录如下：
   - NAS 上改 `.env` 后必须 `sudo docker compose up -d --build`
     （★ **不是 `restart`** —— 它重启的是同一个容器、沿用同一份环境变量，
     **新的 `.env` 根本不会被读到**）。
   - **先手工跑一条**再交回定时：
     ```bash
     docker compose --profile tools run --rm tools python publish.py --limit 1
     ```
   ★ 那句「先确认站方权限确实通了，否则会白等一轮退避」**已由本轮实测兑付**：
   3 条实发全成功（见本节末那笔），站方权限当时是通的。
   恢复条件见 **`ERRATA.md` 的 `ERR-SITE-01`**。
4. **✅ 已了结（2026-09-19）：NAS 上的 `README.md` 已对齐。**
   原条目写的是「`deploy.sh` **不带**它（白名单 = `Dockerfile` 的 `COPY` 行）」——
   ★ **这句是错的**：实测 `deploy.sh` 的 `FILES` 白名单里**有** `README.md` 与
   `.env.example`（自 `ebbffcc` 起就有），本次 `deploy.sh --apply` 已把 README
   一起同步过去（`cmp` 逐字节核过）。该条目的两条断言**都不成立**，故销掉。
   ★ **已修（2026-09-22）**：那三处「白名单 = `Dockerfile` 的 `COPY` 行」的描述**不准确**，
   已在本文档内就地订正 —— `deploy.sh` 的 `FILES` 白名单**除了** `.py` 还含
   `Dockerfile` / `docker-compose.yml` / `README.md` / `.env.example`，
   而 `Dockerfile` 的 `COPY` 行**只**决定「镜像里有什么」。**两者是两条独立的路**
   （见 `ERRATA.md` 的 `ERR-DOCKER-05`：在白名单里 ≠ 容器里看得见）。
   现在真值由 `counts.json` 采集（`offline.deploy.files` 与
   `offline.deploy.dockerfile_copy_py`，含**双向**差集）。
5. **🟢 OpenCD 转载流水线（第 1~3 步已了结 · 抓取层**已接调度 + 已入库**）。**
   定时从 OpenCD（皇后）爬种子详情页 → 解析 → **合规裁决** → 映射字段 →
   渲染简介（★ 抓轨日志要转成折叠代码）→ 发布到 Musopia。
   ★ **剩余工作与验收判据（就地写清，别再指向别处）**：
   - **第 4 步：字段映射 → 渲染 → 发布**（`opencd_map.py` → 渲染 → 站点）。
     验收：① 映射层有 `--selftest`（本地样本，不碰外网）；
     ② 全链路默认 **dry-run**，要显式开才会真的写站点；
     ③ dry-run 下**详情页/发布请求数必须为 0**（这条已有回归判据，见
     `tools/opencd-crawl-selftest.py` 判据⑧ —— 它抓到过一次真 bug）。
   - ★★ **当前它**不会**产生任何新发布**（刻意的，不是没做完）：`crawl.py` 传
     `qb_seed_days=None` ⇒ 每行落 `NOT_SEEDED` ⇒ 而发布队列要求 `status='SUCCESS'`
     ⇒ 一条都进不去。闸门①纪律：**宁可晚抓，不可早发**。
   - **打通闸门①**（读 qB 的做种时长）：验收 = 在 NAS 上实测 `qbittorrent-api`
     返回的字段名，并写成一条断言（**不许靠"看起来对"**）。
   - **已完成**：`opencd_parse.py` 解析 + 三态裁决，`--selftest` 通过（纯离线）。
     就地验：`python opencd_parse.py --selftest`  ★ 项数见 `counts.json`，**不在此处手维护**。
   - **第 3 步（抓取层）已接进调度（2026-09-22）**：`crawl.py` / `opencd_rate.py` /
     `opencd_log.py`，离线自检 `python tools/opencd-crawl-selftest.py`
     （本地 mock 皇后，不碰外网；★ 项数见 `counts.json`）。★ **已入库**（`opencd_ingest`），
     且 daemon 里多了 `crawl` 阶段（`ENABLE_CRAWL`，★ **默认 false**）。
   - **限速（2026-09-19 调）**：每请求最小间隔 **120s**、每日上限 **50**（见 `.env.example`）。
     ⚠️ **两个旋钮都是「请求」量纲，不是「种子」** —— 用户要的「每天 50 个种子」
     目前只能**近似**成「每天 50 个请求」（一种子 ≈ 2~3 请求 ⇒ 实际约 17~25 种子/天，偏保守）。
     ✅ **2026-09-22 修**：`RateGate` 原先每轮新建 ⇒ 「每日」上限**退化成每轮上限**；
     现在计数落在 `data.db` 的 `opencd_quota` 表（跨轮持久）⇒ 是**真的每日 50**。
     ⚠️ 但量纲仍是**请求** —— 按种子卡要在入库那步记 `kind='seed'`（表结构已留位置，**还没做**）。
   - **cookie 失效不会静默**：探活失败 ⇒ crawl 退出码 1 ⇒ daemon 指数退避；
     `daemon_state.json` 里 `crawl.last_ok=false`，每小时巡检会报出来。
     ★「发邮件告警」并入每日邮件待办，**本期不实现 SMTP**。
   - **下一步**：**第 4 步**（字段映射 → 渲染 → 发布，**全部默认 dry-run**）+
     打通闸门①（读 qB 的做种时长，需要先在 NAS 上实测 `qbittorrent-api` 的字段名）。
   - ★ 动手前必读：**转载规则是三态**（只有明确 ALLOW 才自动发布；
     ★ 且裁决**只认字段、不靠关键词兜底**）、
     样本脱敏（`opencdhtml/` 默认忽略，只放行 `sanitized/`）。

详见本文档上文各节；**教训与检索入口见 `ERRATA.md`**（每条一个 `ERR-*` ID）。
