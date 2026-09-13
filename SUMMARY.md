# Musopia 自动化发布 / 种子页面编辑 —— 方案与进度总结

> **本文档的定位**：README 是 INFO 层（操作手册），本文是 **DEBUG 层**（完整过程、决策理由、踩过的坑）。
> 操作步骤不在本文重复，只写「为什么这么做」和「为什么曾经不这么做」。
>
> **⚠️ 关于本文的证据边界**：本文写于 2026-09-13，材料来源是**代码本身 + 仓库原有的 README
> + 当日的开发会话**。这个项目在 git 里只有 3 个提交（见 §5），**没有逐日的开发过程日志**。
> 因此本文按**主题**组织，而不是按时间流水；凡是推断而非直接可考的结论，都标了「（推断）」。
> 不要把它当成一份逐次会话的记录 —— 那是 reseed-toolkit 的 SUMMARY 的形态，这里不具备。

---

## 导航（先读这里 30 秒）

| 我想… | 看哪节 |
|---|---|
| 快速搞清这项目干嘛的 | **§1 目标** + **§2 拓扑** |
| 搞清每条设计为什么是这个样 | **§3 关键决策与踩过的坑** ← 最重要的一节 |
| 查某个文件负责什么 | **§4 各文件最终状态** |
| 看现在做到哪了 | **§5 进度** |
| 看曾经错过什么（避免重走） | **§6 已了结的坑** + **走过的弯路.md** |
| 看还有什么没解决 | **§7 已知的非阻塞问题** |
| 搞清本机怎么开发、怎么推上去 | **§10 本机开发与部署工作流** |
| 密钥/脱敏的约定 | **§0** + **§8** |

---

## 0. 占位符约定

本文档里的主机名 / 内网地址 / 站点凭据**一律不写真值**，用下述占位符：

| 占位符 | 指代 |
|---|---|
| `<NAS>` | 群晖 NAS 主机名 |
| `<NAS-SHARE>` | NAS 上的 SMB 共享路径，形如 `//<NAS>/docker_ssd/` |
| `<PROD-DIR>` | NAS 上的部署目录，本文举例用 `/volume2/docker_ssd/musopia_script2` |
| `<DEV-DIR>` | 本机开发副本目录，本文举例用 `D:\Projects\musopia\zhuanzhong` |
| `<COOKIE>` / `<QB-PASS>` / `<PICGO-KEY>` | 站点 Cookie / qB 密码 / 图床 API Key，**只在 NAS 的 `.env` 里** |

`.env` 含上述全部真凭据，**不在任何白名单里、不进仓库、不随代码同步**。

---

## 1. 目标（范围）

**做两件事，第二件才是重头：**

1. **发布流水线**：从 qBittorrent 抓 OpenCD 分类下已完成的抓轨资源 → 抽元数据（封面、CUE
   曲目、EAC/XLD 抓轨日志）→ 生成 `.torrent` → 发布到 Musopia → 回加 qB 做种。
   这一段是**一次性**的，每个资源只走一遍。

2. **批量编辑已发布页面**：站点上已有 **250+ 个**种子页面。站点的简介结构会变、封面会丢、
   副标题想统一改 —— 这些都要能**反复重跑**，且**不能有副作用**。

> 第 2 条决定了很多设计：为什么是 dry-run 优先、为什么编辑要先读表单、
> 为什么"改一处"的渲染要发布和编辑共用（见 §3）。

**非目标**：不做站点侧的数据抓取/统计，不做多站点分发，不做种子重命名。

---

## 2. 拓扑

### 2.1 数据流

```
qBittorrent(:3020, 分类 opencd)      ← 音乐文件在这里，只读挂载进容器
     │
     ▼ run.py ── 抽元数据 → 生成 .torrent → 写 data.db
     │
     ▼ publish.py ── takeupload 发到 Musopia ── 回加 qB 做种
     │
     ▼ sync_site.py ── 回填 site_torrent_id（★编辑的前置条件）
     │
     ▼ update_metadata.py ── takeedit 读改写式编辑（daemon 里带 --stale-only）
     │
     ▼ Musopia 站点上的种子页面
```

### 2.2 容器拓扑

**单容器**（这就是个设计决策，见 §3）：

```
musopia (container_name 定死)  ← docker compose up -d 唯一带起来的
  command: python daemon.py
  network_mode: host
  volumes:
    ${STATE_DIR:-.}:/data                        # data.db / output_torrents / daemon_state.json
    ${MUSIC_DIR:-/volume1/video/music}:/downloads:ro
    ${INCOMPLETE_DIR:-...}:/downloads/incomplete:ro
  healthcheck: python healthcheck.py   (60s 一次)

musopia_tools  (profiles: tools)  ← 手动一次性任务，--rm 跑完即删
musopia_once   (profiles: tools)  ← 跑一轮全部阶段就退出，配 DRY_RUN=true 做演练
```

- 代码是 **`COPY` 进镜像**的（不是 bind mount），所以改代码必须 `up -d --build`，`restart` 无效。
- `network_mode: host` 下**没有** `host.docker.internal` 这条 DNS 记录，所以 qB 用
  宿主机自己的地址 `QB_HOST=http://127.0.0.1:3020`。
- 状态（`data.db` / `output_torrents/` / `daemon_state.json`）用 **bind mount 放在脚本目录**，
  不用命名卷 —— 图的是 SMB 上直接看得到、能备份、出问题能直接查库。

### 2.3 状态文件

| 文件 | 内容 | 谁写 |
|---|---|---|
| `data.db` | 254 条种子记录 + 发布状态 + `site_torrent_id` | 各阶段 |
| `output_torrents/` | 生成的 `.torrent` | `run.py` |
| `daemon_state.json` | 各阶段 `last_run` / `last_ok` / `last_finish` | `daemon.py` |
| `daemon_heartbeat` | 纯 unix 时间戳，每轮刷新 | `daemon.py` |

全部在 `.gitignore` 里，全部**不在 deploy.sh 白名单里**。

---

## 3. 关键决策与踩过的坑

> 本节是全文重点。每条先写「现在怎么做」，再写「为什么 —— 之前错在哪」。

### 3.1 编辑用「读表单 → 覆盖指定字段 → 整表提交」

`takeedit.php` 是**整表覆盖**语义：没提交的字段会被站点当空值处理。

**旧版错在哪**：硬编码了一份字段表，把 `medium_sel[4]` / `audiocodec_sel[4]` / `team_sel[4]`
填 0、`tags` 完全不提交。结果是**每编辑一次就把这些站点属性抹掉一次** ——
而且是静默的，站点那边不会报错。

**现在**：先 `GET edit.php` 把现值读出来当基线，只改需要改的，其余原样回填。副作用是零。

> 这条的普适教训：**对「整表覆盖」的接口做部分更新，必须先读后写。**
> 看起来像 PATCH 的接口可能实际是 PUT。

### 3.2 默认 dry-run，批量要二次确认

改站点上 250+ 个页面**不可撤销**。所以 `--apply` 是显式动作，批量时还要输入 `yes`。

**这条是被 §3.3 逼出来的** —— 如果不是先在 dry-run 里看到了匿名开关的副作用，
那次上线会静默改掉 254 个种子的匿名状态。

### 3.3 不自动改匿名开关

**错在哪**：dry-run 时发现默认填 `anonymous=1`，而站点上有 **254 个非匿名种子** ——
照提交就会把它们**悄悄改成匿名**。

这不是需求的一部分。所以改为**不提交该字段**（即沿用站点现状），需要匿名请显式加 `--anonymous`。

> 教训：**危险默认值的副作用只有 dry-run 能暴露。** 先干跑不是流程洁癖，是唯一的检查手段。

### 3.4 schema 只有一处定义

**错在哪**：原先 `run.py` 和 `publish.py` **各写一份建表语句**且字段不一致
（`publish.py` 少了 `log_content` / `log_filename`）。谁先跑谁建表 ——
于是 `update_metadata.py` 查 `log_content` 直接 `OperationalError`。
这类 bug 的恶劣之处在于**取决于执行顺序**，本地复现不出来。

**现在**：统一由 `db.init_db()` 建表并自动补列，任何脚本启动时调用，**幂等**。
`COLUMNS` 是唯一来源，`TASK_FIELDS` 与它对齐。

`logs_json` 列就是靠这套机制给旧库补上的（`init_db()` 自动 `ALTER TABLE`）。

### 3.5 简介渲染：发布和编辑**共用一处**

`descr.build_form_fields()` 一处渲染，`takeupload` 和 `takeedit` 都走它。
改一处对**新种子和已发布的 250+ 个页面同时生效**。

**为什么重要**：如果发布和编辑各写一份渲染，两边一定会漂移 —— 站点上就会同时存在
两种风格的页面，而且**没法批量修**（改哪一份都只影响一半）。

### 3.6 抓轨日志用折叠代码，不塞 `[quote]`

**错在哪**：旧版把整段 EAC/XLD 日志放进 `[quote]`，页面被撑得很长，
主体内容（曲目列表）反而要滚很久才看得到。

**现在**：NexusPHP 折叠块 `[spoiler=LOG][code]…[/code][/spoiler]`；
多 CD 每碟一块（`[spoiler=CD1 LOG]`…），碟号从 CD 子目录名 / 日志文件名正则识别，
识别不出就按路径顺序编号。

配套存储：新增 `logs_json` 列存多碟日志序列化结果；`log_content`/`log_filename`
仍保留**首个**日志供兼容显示/预览。读取侧 `assets.read_logs()` 返回全部，
`assets.read_log()` 是取首个的兼容封装。

### 3.7 副标题改为「渲染时重算」

**错在哪**：副标题原先和主标题重复（主标题已经是 `歌手 - 专辑 年份 - 格式 整轨 - OpenCD`），
而且是在**扫描时**算好存库的 —— 于是已发布的旧页面重新编辑也**不会更新**副标题。

**现在**：副标题改为客观的曲目/碟数信息（单碟 `全 N 曲` / 多碟 `DCD · 全 N 曲`），
并在**渲染时重算**。所以已发布页面重新编辑也会更新。

数不出曲目时**回落到库里旧值/种子名，不硬造残缺文本**。

> 教训：**派生数据不要存库，除非有明确的性能理由。** 存了就会和源数据漂移，
> 且历史记录永远停在旧算法上。

### 3.8 曲目列表兜底

有 CUE 就聚合所有 CUE 的 `TITLE`（多 CUE 加 `【CD1】/【CD2】` 分段）；
**分轨专辑无 CUE 时用音频文件名（去扩展名）兜底**，避免「曲目为空」。

### 3.9 封面绝不重复上传，丢了自动找回（三层防护）

这一条踩得最深，所以有三层：

**① `run.py` 对已发布任务直接跳过。**
原先会因为本地 `.torrent` 文件被清理而**整个重做**；重做场景下库内已有 `cover_url` 就直接复用。

**② 图床报 `Duplicated upload` 400 时，从错误响应里回收已有图片的 URL。**
**错在哪**：把「上传失败」当成「没有封面」写回空值 —— 于是封面被自己抹掉了。
图床说「重复」其实是在告诉你**图已经在了**，只要把 URL 拿回来。

**③ `update_metadata.py` 发现本地 `cover_url` 为空时，先读站点表单从现有简介里把封面
URL 抠回来**（回写本地库修复历史数据），再渲染新简介提交。
避免用空封面渲染 descr 把站点上**仅剩的**封面也抹掉。

> 教训：**「没找到」和「取失败」必须区分开。** 把失败当空值写回，是数据丢失的经典路径。
> 第 ③ 条其实是前两条漏洞的历史补救：本地 `cover_url` 曾被旧版 bug 抹空，
> 现在能从站点页面找回并回写库。

### 3.10 单容器 + 自研调度，而不是五个 cron

**错在哪**：早期按服务拆分成 `musopia_publish` / `musopia_scan` 等多个容器，各自定时。
五个阶段**共用一个 SQLite**，多容器各自定时必然出现并发写（`database is locked`），
还会「发布还没跑完就开始编辑」，读到半成品数据。

**现在**：`daemon.py` 单容器**串行**调度 `scan → publish → sync → covers → edit`。
天然规避上面两个问题，顺带省掉 cron 在容器里的 PID 1 / 环境变量继承 / 日志重定向三个坑。

**遗留**：早期那些孤儿容器如果还在跑，`docker compose` 会报
`Found orphan containers ([musopia_publish musopia_scan])` —— 它们正是 `database is locked`
的来源。清理方式见 README。

### 3.11 编辑限速用 `--rate`（按种子），不用 `--interval`（按请求）

**错在哪**：一个种子要发 **2~3 个 HTTP 请求**（读 `edit.php` → 提交 `takeedit.php` →
可选的详情页校验），所以只调 `--interval` **算不准每分钟处理量**。

**现在**：`--rate` 按**种子**计（默认 10/分），`--interval` 按**请求**计，两者分开。

**还有一个坑**：限速用**绝对时间基准**（第 N 条在 `起点 + (N-1)×6秒` 才开始），
不是每轮 `sleep(6)` —— 后者在站点响应慢的时候会把延迟累加上去，**实际速率越跑越低**。

### 3.12 `save_task()` 只在值真变了才写 `updated_at`

和 §3.13 的增量编辑是**一套**的。

**错在哪**：`scan` 每 30 分钟重扫一遍 qB，原先**无条件刷新** `updated_at`。
于是即使内容一个字没变，254 条记录也会**全部重新变成 stale**，
`edit` 阶段每轮把整站页面重新拉一遍 —— 按 10 个/分钟算，
**每 30 分钟里有 25 分钟在做无用请求**。

**现在**：upsert 带 `WHERE 任一字段 IS NOT excluded.该字段`，无变化就整行不动。

**★ 用 `IS NOT` 而不是 `!=`**：SQLite 里 `NULL != 'x'` 结果是 `NULL`（被当假），
会**漏掉「字段从空补成有值」**这类最常见的变化 —— 比如 `log_content` 首次被填上。

### 3.13 编辑是增量的（`--stale-only`）

只处理 `site_synced_at IS NULL OR updated_at > site_synced_at` 的记录。
没有它，每轮都要把 250+ 个页面全部拉一遍 `edit.php` 做对比，纯属拿站点带宽换空转。

### 3.14 空库拒绝启动

**防的是什么**：如果 `/data` 挂错或被重建，库就是空的 → `run.py` 会把 qB 里**所有**种子
当成新任务 → `publish.py` 紧接着把已经发过的资源**重复发一遍** → 站点上出现几百个重复种子
且**无法批量撤回**。

**现在**：daemon 启动时发现「库是空的 + 启用了 publish」直接拒绝启动。
确认是全新部署时才设 `ALLOW_EMPTY_DB=true`。

### 3.15 密钥不留默认值

`config.py` 对四个真密钥用 `require_env()`：**缺失直接 `sys.exit`**。
源码里**不给**「看起来能跑」的真实默认值 —— 免得悄悄拿一个可能已过期/泄露的旧值继续跑。

> 这是个反直觉的决定：多数项目会给个 `os.getenv("X", "default")` 图省事。
> 这里刻意不给，因为「配置错了但看起来在跑」比「启动失败」危险得多。

---

## 4. 各文件最终状态（要点）

| 文件 | 行数 | 角色 | 要点 |
|---|---:|---|---|
| `config.py` | 60 | 统一配置 | 密钥缺失即终止；`DATA_DIR` 与代码目录解耦 |
| `db.py` | 254 | ★唯一 schema 来源 | `COLUMNS` + `TASK_FIELDS`；`init_db()` 幂等建表/补列 |
| `descr.py` | 255 | ★简介渲染 | `build_form_fields()` 发布/编辑共用；`derive_small_descr()` 渲染时重算 |
| `assets.py` | 340 | 素材处理 | `read_logs()` 多碟 / `read_log()` 兼容；封面查找与图床上传 |
| `musopia.py` | 370 | 站点客户端 | 登录检查、上传列表、**读改写式编辑** |
| `run.py` | 233 | 扫描入库 | 改元数据 → 生成种子；已发布任务跳过 |
| `publish.py` | 176 | 发布 | `takeupload` + 回加 qB 做种 |
| `update_metadata.py` | 263 | ★批量编辑 | 默认 dry-run；`--stale-only` / `--rate` / `--verify` |
| `sync_site.py` | 119 | 回填站点 ID | 编辑功能的前置条件 |
| `backfill_covers.py` | 144 | 补历史封面 | `set_cover` 会刷新 `updated_at`，记录重新变 stale |
| `check_db.py` | 53 | 状态速览 | **排查问题先跑这个** |
| `daemon.py` | 371 | ★串行调度 | `build_stages()` / `preflight()` / `beat()` |
| `healthcheck.py` | 55 | 健康检查 | 读写心跳文件 |
| `Dockerfile` | 32 | 镜像 | 依赖版本**全部固定**，避免上游 breaking change |
| `docker-compose.yml` | 86 | 编排 | `x-common` 锚点复用；`network_mode: host` |
| `deploy.sh` | — | 部署工具 | 白名单同步；**不上 NAS** |

合计约 **2811 行**（不含文档）。

> `Dockerfile` 里 `pip install` 的版本是**写死**的（`qbittorrent-api==2024.9.67` 等），
> 理由写在注释里：「避免某天上游一个 breaking change 让整套脚本在容器重建后突然跑不起来」。

---

## 5. 进度

### 5.1 git 历史（全部）

```
ebbffcc  加 deploy.sh：本地 → NAS 的白名单同步              (2026-09-13)
1a8b1fe  生产已验证：折叠日志、副标题重算、封面防丢、schema 单一来源   (2026-09-13)
419b6db  Musopia 自动化发布与种子页面编辑流水线              (2026-09-08)
```

**★ 值得注意**：`1a8b1fe` 的内容（346 行）在生产环境**已经跑了一段时间**，
但一直只存在于 NAS 工作区、**没有落库**。2026-09-13 才连同本机开发副本一起补上。
在此之前，NAS 上那份和 git 里的 `419b6db` 是**不一致**的 ——
如果当时有人 `git checkout .`，这批已验证的改动就没了。

### 5.2 当前运行状态

- NAS `<PROD-DIR>` 上常驻 `musopia` 容器，254 条记录，站点 ID 已回填。
- `daemon_state.json` 记录五个阶段的 `last_run` / `last_ok`（全部 `true`）。
  最近一次落盘是 2026-09-09。
- 本机 `<DEV-DIR>` 是**整目录复制**来的完整开发副本（含 `.git`、`data.db`、
  `output_torrents/`、真实的 `.env`）。

### 5.3 本轮（2026-09-13）新做的

1. 从 NAS 整目录复制出本机开发副本，md5 逐文件比对确认 16 个源文件一致。
2. 把生产已验证的 346 行改动落成 commit `1a8b1fe`。
3. 新增 `deploy.sh`（commit `ebbffcc`）—— 见 §10。
4. 补齐三份文档（README / SUMMARY / 走过的弯路）。

---

## 6. 已了结的坑（保留记录，避免重走）

| # | 坑 | 状态 | 详见 |
|---|---|---|---|
| 1 | 整表覆盖抹掉 `medium/codec/team/tags` | ✅ 已修（读表单当基线） | §3.1 |
| 2 | `anonymous=1` 会把 254 个非匿名种子改匿名 | ✅ 已修（不提交该字段） | §3.3 |
| 3 | schema 两处定义不一致 → `OperationalError` | ✅ 已修（`db.init_db()` 单一来源） | §3.4 |
| 4 | 抓轨日志塞 `[quote]` 撑爆页面 | ✅ 已修（折叠代码） | §3.6 |
| 5 | 副标题与主标题重复、且存库不更新 | ✅ 已修（渲染时重算） | §3.7 |
| 6 | 分轨专辑无 CUE 时曲目为空 | ✅ 已修（音频文件名兜底） | §3.8 |
| 7 | 多碟日志只存首个 | ✅ 已修（`logs_json` 列） | §3.6 |
| 8 | 图床 `Duplicated upload` 被当「没封面」写回空值 | ✅ 已修（回收 URL） | §3.9 |
| 9 | 已发布任务因本地 `.torrent` 被清理而重做 | ✅ 已修（跳过 + 复用） | §3.9 |
| 10 | 本地 `cover_url` 被旧版 bug 抹空 | ✅ 已修（从站点简介抠回） | §3.9 |
| 11 | 孤儿容器并发写 SQLite → `database is locked` | ✅ 已修（单容器串行） | §3.10 |
| 12 | `run.py` 无条件刷 `updated_at` → 254 条全 stale | ✅ 已修（条件 upsert） | §3.12 |
| 13 | `NULL != 'x'` 在 SQLite 里为 `NULL`，漏掉「空→有值」 | ✅ 已修（改 `IS NOT`） | §3.12 |
| 14 | `--interval` 算不准每分钟处理量 | ✅ 已修（`--rate` 按种子） | §3.11 |
| 15 | 每轮 `sleep(6)` 累加导致速率衰减 | ✅ 已修（绝对时间基准） | §3.11 |
| 16 | 空库会把已发布资源重复发一遍 | ✅ 已修（空库拒绝启动） | §3.14 |
| 17 | 「配置错了但看起来在跑」 | ✅ 已修（密钥无默认值） | §3.15 |

> 归因分析见 **走过的弯路.md**（同一批事实，那边只写「错在哪 + 根因」）。

---

## 7. 其它已知的非阻塞问题

1. **工作区行尾不一致，且与 `.gitattributes` 的声明不符。**
   实测**被跟踪的 20 个文件里，12 个是 CRLF、8 个已是 LF**：

   | 行尾 | 文件 |
   |---|---|
   | **CRLF** (12) | `.env.example` `.gitattributes` `.gitignore` `assets.py` `backfill_covers.py` `check_db.py` `daemon.py` `db.py` `descr.py` `healthcheck.py` `musopia.py` `sync_site.py` |
   | **LF** (8) | `Dockerfile` `README.md` `config.py` `deploy.sh` `docker-compose.yml` `publish.py` `run.py` `update_metadata.py` |

   `.gitattributes` 写着 `* text=auto eol=lf`，注释里明确警告
   「Dockerfile 带 CRLF 的 RUN 指令会报 `/bin/sh^M 找不到`」。
   属仓库声明与实际不符（SMB 往返 + Windows 编辑器所致）。
   **索引里已经是 LF**（所以 `git status` 干净），要修的是**工作区**：

   ```bash
   git checkout-index -a -f     # 重写全部被跟踪文件；★ 执行前先提交所有改动
   ```

   `deploy.sh` 会告警但**不擅自改**。

   > ⚠️ **勘误**：本条最初写成「17 个文件（13 个 `.py` + …）**全是 CRLF**」，
   > 是**错的** —— 那个结论来自一个假阳性的检测方法，见 §10.5 b)。

2. **`deploy.sh` 尚未实际部署过一次。**
   写好了、沙箱测过了，但本机与 NAS 目前内容一致（本机就是从 NAS 拷来的），
   `bash deploy.sh` 会直接报「无需同步」。要等下次真正改完代码才走一遍。

3. **分轨旧记录曲目为空、多 CD 旧记录 `logs_json` 为空。**
   这些历史记录会被当成 1 碟，副标题显示不正确。需要先全量重扫回填、再批量编辑：
   ```bash
   python run.py --recheck --no-upload-cover
   python update_metadata.py --stale-only --apply --yes --rate 10
   ```

4. **本机跑不通完整流程。** compose 是 `network_mode: host` +
   `QB_HOST=http://127.0.0.1:3020`，那个 `127.0.0.1` 指的是 **NAS 自己**。
   所以本机只能做 README「改完代码怎么测」的第 ① 步（渲染单测，不连站点不连 qB）。
   ②~⑤ 在本机连不上 qB、也连不上站点内网地址 —— **要验证集成行为必须推回 NAS 重建**。

5. **`data.db` 是一份快照。** 本机这份是 2026-09-09 从 NAS 复制的，
   之后 NAS 上的变化不会同步过来。别拿本机这份当生产现状看。

---

## 8. 安全约定（务必遵守）

1. **四个真密钥只在 `.env`**：`QB_PASS` / `MUSOPIA_COOKIE` / `MUSOPIA_PASSKEY` / `PICGO_API_KEY`。
2. `.env` 与 `.env.*` 在 `.gitignore` 里（`!.env.example` 除外）。
3. **`.env.example` 只放 `changeme` 占位符**，可安全提交。
4. **`deploy.sh` 的白名单永不包含 `.env`**，且有显式断言兜底。
5. **本机的 `.env` 是真凭据副本**（2026-09-13 整目录复制时带过来的）。
   推送前**必须实际核查**它没被跟踪 —— 别只信 `.gitignore` 规则。
   ```bash
   git status --porcelain
   git ls-files | grep -i '\.env'      # 期望只看到 .env.example
   ```
6. 站点侧的操作（发布、编辑）**默认 dry-run**，`--apply` 是显式动作。

---

## 9. 环境备忘

| 项 | 值 |
|---|---|
| 生产目录 | NAS `<PROD-DIR>`（例 `/volume2/docker_ssd/musopia_script2`） |
| 本机开发副本 | `<DEV-DIR>`（例 `D:\Projects\musopia\zhuanzhong`） |
| 远端仓库 | `github.com/iSunker/opencd-musopia-script` |
| 访问方式 | NAS **SSH 未开放** —— 本机不能在 NAS 上执行命令；读走 SMB，写走 `deploy.sh` |
| qB | 宿主机 `:3020`，分类 `opencd`；发布后回加分类 `Musopia` |
| 音乐目录 | 容器内 `/downloads`（只读挂载），由 `.env` 的 `MUSIC_DIR` 指定 |
| 时区 | `TZ=Asia/Shanghai`（Dockerfile 里写死） |
| Python | 3.10-slim；依赖版本在 Dockerfile 里**全部固定** |

---

## 10. 本机开发与部署工作流

### 10.1 为什么需要 `deploy.sh`

**NAS 的 SSH 没开** —— 本机不能在 NAS 上执行任何命令。所以：
- 读：直接走 SMB 挂载；
- 写：走 `deploy.sh` 做**白名单同步**。

**它只负责把文件放过去，不重建容器。** 重建那一步必须在 NAS 上手动做
（脚本结尾会把命令打出来）。

### 10.2 白名单怎么定的

**白名单 = `Dockerfile` 的 `COPY` 行** + `Dockerfile`/`docker-compose.yml` 本身：

```
COPY config.py db.py descr.py assets.py musopia.py \
     run.py publish.py update_metadata.py sync_site.py \
     backfill_covers.py check_db.py \
     daemon.py healthcheck.py ./
```

这 13 个 `.py` 正好就是镜像构建上下文需要的全部 —— 多一个不用同步，**少一个容器里就缺模块**。

脚本另有一道**反向自检**：用 awk 拼掉续行、从 `Dockerfile` 解出实际要 `COPY` 的文件，
逐个核对白名单，**漏一个就拒绝部署**（退出码 1）。

> **为什么要有这道自检**：这是照搬 reseed-toolkit 的教训。那边 `Dockerfile` 是
> `COPY orchestrator/ /app/orchestrator/`（拷**整个目录**），而白名单是按文件列的，
> 漏了 `state.py`。平时看不出来（主程序不导入它），一加新子命令就是**模块级 ImportError**，
> 连默认的 `preflight` 都一起打挂。
> ★ 教训：**「这个文件需不需要同步」要按「谁拷它」去数**，不能凭印象。

### 10.3 其它设计要点

- **默认 dry-run**：`bash deploy.sh` 只打 diff，`--apply` 才写。
- **备份在本地**：写到 `<DEV-DIR>/.deploy-backup/<时间戳>/`，不污染 NAS 上的
  docker build 上下文，也不会因为写 NAS 中途失败而把备份一起丢掉。支持 `--rollback`。
- **原子替换**：写临时文件再 `mv -f`，不原地 `cp` 覆盖。
- **生产状态永不覆盖**：`.env` / `data.db` / `daemon_state.json` / `daemon_heartbeat` /
  `output_torrents` **不在白名单里**，且脚本开头有显式断言（混进去直接退出）。
- **外部 `DST` 优先于 `.deploy.nasrc`**：这条是**照搬 reseed-toolkit 踩过的坑**——
  那边 `DST=/tmp/sandbox bash deploy.sh --apply` 被 `.nasrc` 里的 `DST` 覆盖掉，
  于是「在沙箱里演练」实际**打到了生产 NAS**。这里先存下外部给的值，`source` 完再恢复。
  （`.deploy.nasrc` 的定位是**默认值**，不是**强制值**。）
- **心跳提示**：读 `<PROD-DIR>/daemon_heartbeat` 的新鲜度，用来判断「现在重建容器合不合适」。
  ⚠️ 与 reseed-toolkit 不同 —— 那边 `drive-loop` 是 `sh` 直接从 NAS 文件系统读脚本，
  覆盖运行中的脚本会让它读到错位字节；这里代码是**构建时 `COPY` 进镜像**的，
  同步 `.py` 对运行中的容器**零影响**，真正会打断它的是 `up -d --build`。

### 10.4 一次完整的改动流程

```bash
# 本机
cd <DEV-DIR>
# ...改代码...
bash deploy.sh                     # ① 先看 diff
bash deploy.sh --apply             # ② 写入 NAS（自动备份）

# NAS 上手动（SSH 没开，脚本替不了你）
cd <PROD-DIR>
sudo docker compose config --quiet && echo 'YAML OK'
sudo docker compose up -d --build  # ★ 必须 up --build，不是 restart
sudo docker logs --tail=60 musopia
```

回滚：`bash deploy.sh --rollback`，然后同样在 NAS 上 `up -d --build`。

### 10.5 本轮（2026-09-13）踩的坑

**同族错误犯了两次，方向相反 —— 一次假阴性、一次假阳性。**

#### a) `grep $'\r'` 在 Git Bash 下静默失效（假阴性）

`deploy.sh` 的 CRLF 检测原本用 `grep -q $'\r' "$file"`，
在这台机器上**永远返回「没找到」**：实测文件里有 255 个 CR，`grep -c` 却返回 0。
原因是 Git Bash 把含裸 CR 的**参数**传给 Windows 版 `grep.exe` 时，
那个 CR 被当行终止符吃掉了。

改成 `tr -cd '\r' < "$file" | wc -c` **数字节**，不经过参数传递。

#### b) `od -c | grep '\r'` 假阳性 —— 并据此写下了错文档

判断「哪些文件是 CRLF」时，最初用的是：

```bash
head -c 4096 "$f" | od -c | grep -q '\\r'
```

这个模式在 GNU grep 下**退化成匹配字母 `r`**（`\\r` 并不是「反斜杠 + r」的字面量）。
实测：

```bash
printf 'abc r def\n' | grep -c '\\r'   # → 1   （纯字母 r 就匹配）
printf 'abc\ndef\n'   | grep -c '\\r'   # → 0
```

而 `od -c` 的输出行里到处是字母 `r`，于是**纯 LF 的文件也被判成 CRLF**。

**后果**：得出「17 个文件全是 CRLF」的**错误结论**，并写进了
README / SUMMARY / 走过的弯路 **三份文档**。真实情况是 **12 个 CRLF、8 个 LF**（见 §7 第 1 条）。
是靠写入后复核（拿 `config.py` 的原始字节 `od -c` 出来看，发现是 `\n` 而非 `\r\n`）才揪出来的。

> ★ **教训**：a) 假阴性、b) 假阳性，**同一类错误在一天内犯了两次，方向还相反** ——
> 说明根因不在具体命令，而在**检测方法从未用已知样本校验过**。
> 正确做法：先拿一个**已知答案**的文件试检测方法
> （「我确知这个是 LF —— 它报 CRLF，那方法就是错的」），再拿去扫全量。
> 「检测方法失效」比「不检测」危险，因为它让你得出**确定性的错误结论**，
> 而你会照着它去改别的东西。

---

## 11. 未做 / 下一步

### 🔴 断言级别（必须做）

1. **推送本地 2 个 commit 到 `origin/main`** —— `1a8b1fe`、`ebbffcc` 目前只在本地。
   **推之前先核实 `.env` 没被跟踪**（本机这份是真凭据，见 §8 第 5 条）。
2. **统一工作区行尾为 LF** —— 见 §7 第 1 条。

### 🟡 待验证

3. **`deploy.sh` 走一遍真实流程** —— 目前只在沙箱里测过，没实际部署过。
4. **分轨 / 多 CD 旧记录的曲目与 `logs_json` 回填** —— 见 §7 第 3 条。

### 🟢 可考虑

5. **本机能不能跑通 ②~⑤ 的集成测试** —— 目前被 `network_mode: host` +
   `QB_HOST=127.0.0.1` 挡住了。如果要，得让 qB 和站点在本机可达
   （改 `QB_HOST` 指向 NAS 的真实内网地址，并确认站点允许外部访问）。
6. **`data.db` 的同步机制** —— 本机这份是快照，会越来越旧。要不要一个只读的拉取？
   （注意：**不能双向同步**，并发写 SQLite 正是 §3.10 那个坑。）
