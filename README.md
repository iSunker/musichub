# Musopia 自动化发布 / 种子页面编辑

从 qBittorrent 抓取 OpenCD 抓轨资源，自动生成种子、提取元数据、发布到 Musopia，
并支持批量编辑已发布种子页面的**主标题 / 副标题 / 简介（封面图床 + 曲目 + 抓轨日志）**。

## 模块结构

| 文件 | 作用 |
| --- | --- |
| `config.py` | 统一配置，密钥缺失直接终止（不留可用默认值） |
| `db.py` | **唯一** schema 来源 + 自动迁移 + 所有查询 |
| `descr.py` | 副标题、BBCode 简介、站点下拉项推断（发布/编辑共用） |
| `assets.py` | 封面查找、图床上传、日志与 CUE 解析 |
| `musopia.py` | 站点客户端（登录检查、上传列表、读改写式编辑） |
| `run.py` | 扫描 qB → 提元数据 → 生成种子 → 入库 |
| `publish.py` | 发布新种子 + 回加 qB 做种 |
| `update_metadata.py` | **批量编辑已发布页面**（本次核心需求） |
| `sync_site.py` | 回填 `site_torrent_id`（编辑功能的前置条件） |
| `backfill_covers.py` | 补历史缺失的封面 |
| `check_db.py` | 状态速览，排查问题先跑这个 |

## 页面内容渲染（descr.py）

站点简介结构固定，**发布(takeupload)与编辑(takeedit)共用** `build_form_fields()` 一处渲染，
改一处对新种子和已发布的 250+ 个页面同时生效：

```
封面图（[center][img]…）
  ↓
曲目列表（[quote][b]曲目列表：[/b] …）
  ↓
抓轨日志（折叠代码，见下）
```

**抓轨日志用折叠代码，不再塞进 `[quote]`。** 旧版把整段 EAC/XLD 日志放进 `[quote]`，
页面被撑得很长。现在渲染成 NexusPHP 折叠块：

- 单 CD：`[spoiler=LOG][code] …日志原文… [/code][/spoiler]`
- 多 CD：每碟一块 —— `[spoiler=CD1 LOG][code]…[/code][/spoiler]` `[spoiler=CD2 LOG][code]…[/code][/spoiler]`
  （碟号从 CD 子目录名 / 日志文件名正则识别，识别不出就按路径顺序编号）

**副标题不再和主标题重复。** 主标题已是 `歌手 - 专辑 年份 - 格式 整轨 - OpenCD`，
副标题改为客观的曲目/碟数信息，并在**渲染时重算**（不是只在扫描时算好存库），
因此已发布页面重新编辑也会更新：

- 单碟：`全 N 曲`
- 多碟：`DCD · 全 N 曲`（如 `2CD · 全 24 曲`）
- 数不出曲目时回落到库里旧值/种子名，不硬造残缺文本

**曲目列表兜底。** 有 CUE 就聚合所有 CUE 的 `TITLE`（多 CUE 加 `【CD1】/【CD2】` 分段）；
分轨专辑无 CUE 时用音频文件名（去扩展名）兜底，避免「曲目为空」。

> 简介不再写「专辑名称/副标题/原始名称」这类与页面顶部表格重复的信息，
> 也不再追加「由自动化发布程序生成。」署名。

数据库新增列 `logs_json`（`db.py` schema，`init_db()` 自动给旧库补列）：
多碟日志序列化存此；`log_content`/`log_filename` 仍保留首个日志供兼容显示/预览。

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

确认无误后让常驻容器生效：`docker compose up -d --build`（不是 `restart`）。

## 首次使用顺序

```bash
# 0. 看清现状
python check_db.py

# 1. 回填站点种子 ID —— 没有 ID 就无法编辑，必须先做
python sync_site.py            # 预览匹配结果
python sync_site.py --apply    # 写库

# 2. 补封面（可选，但没封面的页面编辑后也不会有图）
python backfill_covers.py           # 预览
python backfill_covers.py --apply   # 上传图床并写库

# 3. 编辑页面 —— 默认 dry-run，永远先预览
python update_metadata.py                        # 预览全部
python update_metadata.py 14611 --apply --verify # 单条试水（推荐）
python update_metadata.py --apply --verify       # 全量提交
```

日常新种子流程：`run.py` → `publish.py`。

## 设计上的几个关键决定

**编辑采用「读表单 → 覆盖指定字段 → 整表提交」。**
`takeedit.php` 是整表覆盖语义：没提交的字段会被站点当空值处理。旧版硬编码了一份
字段表，把 `medium_sel[4]` / `audiocodec_sel[4]` / `team_sel[4]` 填 0、`tags` 完全不提交，
于是每编辑一次就把这些站点属性抹掉一次。现在先 `GET edit.php` 把现值读出来当基线，
只改需要改的，其余原样回填。副作用是零。

**默认 dry-run。** 改站点上 250+ 个页面不可撤销，`--apply` 是显式动作，批量时还要
输入 `yes` 二次确认。

**不自动改匿名开关。** dry-run 时发现默认填 `anonymous=1` 会把 254 个非匿名种子
悄悄改成匿名。这不是需求的一部分，所以改为不提交该字段（即沿用站点现状），
需要匿名请显式加 `--anonymous`。

**schema 只有一处定义。** 原先 `run.py` 和 `publish.py` 各写一份建表语句且字段不一致
（`publish.py` 少了 `log_content` / `log_filename`），谁先跑谁建表，导致
`update_metadata.py` 查 `log_content` 直接 `OperationalError`。现在统一由 `db.init_db()`
建表并自动补列，任何脚本启动时调用，幂等。

**请求限速。** 客户端内置最小请求间隔（默认 2 秒，`--interval` 可调），批量编辑
250 条不会把站点打疼。

**封面绝不重复上传，丢了会自动找回。** 三层防护：
1. `run.py` 对已发布（`site_status='EXISTS'`）的任务直接跳过，不再因本地
   `.torrent` 文件被清理而整个重做；重做场景下库内已有 `cover_url` 就直接复用。
2. 图床报 `Duplicated upload` 400 时，`assets.py` 会尝试从错误响应里回收已有
   图片的 URL，而不是把"上传失败"当成"没有封面"写回空值。
3. `update_metadata.py` 发现本地 `cover_url` 为空时，先读站点表单从现有简介里
   把封面 URL 抠回来（回写本地库修复历史数据），再渲染新简介提交——
   避免用空封面渲染 descr 把站点上仅剩的封面也抹掉。

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
cd /volume?/docker_ssd/musopia_script2

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

`restart` 后看不到新行为、日志里没有新输出，九成是这个原因。

### 看日志

```bash
docker logs -f musopia            # 容器名是 musopia（compose 里 container_name 定死的）
docker logs --tail 100 musopia    # 只看最近 100 行
docker compose logs -f musopia    # 等价写法
```

`docker logs` 后面**必须跟容器名**，不带参数会报 `requires exactly 1 argument`。

### 清理孤儿容器

如果日志里出现 `Found orphan containers ([musopia_publish musopia_scan])`，
说明早期按服务拆分部署的旧容器还在跑——它们和 daemon **并发写同一个 SQLite**，
正是 `database is locked` 的来源。清掉：

```bash
docker rm -f musopia_publish musopia_scan
# 或者下次 up 时顺带清理
docker compose up -d --build --remove-orphans
```

### 为什么是单容器 + 自研调度，而不是五个 cron

五个阶段共用一个 SQLite。多容器各自定时必然出现并发写（`database is locked`），
还会"发布还没跑完就开始编辑"，读到半成品数据。串行调度天然规避这两个问题，
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
`--interval` 是算不准每分钟处理量的。想控制"每分钟改几个页面"就用 `--rate`。

限速用的是绝对时间基准（第 N 条在 `起点 + (N-1)×6秒` 才开始），不是每轮 `sleep(6)`。
后者在站点响应慢的时候会把延迟累加上去，实际速率越跑越低。

默认配置下 `EDIT_INTERVAL=21600` + `EDIT_MAX_PER_CYCLE=100` + `EDIT_RATE_PER_MIN=10`：
每 6 小时一轮，一轮最多 100 个但受限速约束实际跑 10 分钟左右。
250 条积压大约 3 轮清完，之后每轮只处理真正有变化的少数几条。

### 三个必须知道的保护机制

**1. 空库拒绝启动。** 如果 `/data` 挂错或被重建，库就是空的，`run.py` 会把 qB 里
所有种子当成新任务，`publish.py` 紧接着把已经发过的资源**重复发一遍**，站点上出现
几百个重复种子且无法批量撤回。所以 daemon 启动时发现"库是空的 + 启用了 publish"
会直接拒绝启动。确认是全新部署时才设 `ALLOW_EMPTY_DB=true`。

**2. 编辑是增量的。** `edit` 阶段带 `--stale-only`，只处理
`site_synced_at IS NULL OR updated_at > site_synced_at` 的记录。没有它，每轮都要把
250+ 个页面全部拉一遍 `edit.php` 做对比，纯属拿站点带宽换空转。

**3. `save_task()` 只在值真变了才写 `updated_at`。** 这条和上一条是一套的：
`scan` 每 30 分钟重扫一遍 qB，原先无条件刷新 `updated_at`，于是即使内容一个字没变，
254 条记录也会**全部重新变成 stale**，`edit` 阶段每轮把整站页面重新拉一遍——按
10 个/分钟算，每 30 分钟里有 25 分钟在做无用请求。现在 upsert 带
`WHERE 任一字段 IS NOT excluded.该字段`，无变化就整行不动。

用 `IS NOT` 而不是 `!=`：SQLite 里 `NULL != 'x'` 结果是 `NULL`（被当假），
会漏掉"字段从空补成有值"这类最常见的变化——比如 `log_content` 首次被填上。

### 手动执行单次任务

```bash
docker compose --profile tools run --rm tools python check_db.py
docker compose --profile tools run --rm tools python sync_site.py --apply
docker compose --profile tools run --rm tools python update_metadata.py 14611 --apply --verify
```

`network_mode: host` 下容器直接共享宿主机网络栈，所以 qB 用宿主机自己的地址
（`QB_HOST=http://127.0.0.1:3020`）——host 网络里没有 `host.docker.internal` 这条 DNS 记录。

状态（`data.db` / `output_torrents/` / `daemon_state.json`）bind mount 在脚本目录而不是
命名卷：SMB 上直接看得到、能备份、出问题能直接查库。

## 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| `Cookie 已失效` | 更新 `.env` 里的 `MUSOPIA_COOKIE`。各阶段会指数退避，不会疯狂重试 |
| 容器起来就退出，日志写"拒绝启动" | 空库保护触发了，先确认 `/data` 挂对；详见上一节 |
| 某条记录"被跳过" | 缺 `site_torrent_id`，跑 `sync_site.py --apply` |
| 编辑后页面没有图 | 本地 `cover_url` 为空时 edit 阶段会自动从站点简介回收封面；站点也没有的，跑 `backfill_covers.py --apply` 补齐后 `set_cover` 会刷新 `updated_at`，记录重新变 stale，下轮自动把封面写进简介 |
| 日志出现"从站点简介回收" | 历史数据修复：本地 cover_url 曾被旧版 bug 抹空，现已从站点页面找回并回写库 |
| 校验提示"本地无封面" | 本地和站点都没有封面，不是编辑失败。跑 `backfill_covers.py --apply` 补齐 |
| `找不到 takeedit 表单` | 种子已被删除，或当前账号无编辑权限 |
| 提示"无需修改" | 站点内容已和本地一致，属正常（脚本可安全重复运行） |
| 改了本地数据但 edit 阶段说没任务 | `--stale-only` 看 `updated_at`。注意 `save_task()` 只在**值真的变了**时才刷新它；手改库请自己带上 `updated_at=CURRENT_TIMESTAMP` |
| 容器 unhealthy | 心跳停了。`docker compose logs musopia` 看卡在哪个阶段 |
| 想立刻重跑某阶段 | 删掉 `daemon_state.json` 里对应的 `last_run`，或整个文件删掉重启 |
| 改了代码但行为没变 | `restart` 不重新构建镜像。用 `docker compose up -d --build`，见上文 |
| `database is locked` | 有别的进程在并发写库。检查是否有孤儿容器（`docker ps`），见上文清理方法 |
| `docker logs` 报 requires exactly 1 argument | 后面要跟容器名：`docker logs -f musopia` |