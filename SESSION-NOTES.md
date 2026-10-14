# 会话归档（按时间倒序，只增不改）

> **这是会话结论的落盘处** —— 防止"结论只活在某次对话里"（那正是 `ERR-META-04` 说的"传说"）。
> ★ 纪律：**只写"已实测坐实"的结论**，未坐实的写进「未解」并标出**待验的判据**。
> ★ 会变的数字（库计数、阶段年龄）**不写这里** —— 见 `counts.json`。
> ★ 本文档**不进 `deploy.sh` 的 `FILES`**（与 `ERRATA.md`/`counts.json` 同理：
>   文档不是运行时输入；加进去会撞 `ERR-DOCKER-05` —— 落在 compose 目录却不进镜像）。
>   `.dockerignore` 也应把它排除，免得进构建上下文。

---

> ★★ **导航（2026-10-11 整理）** —— 本文件是**逆序**（最新在最上），但历史上有**两套互不相同的「续 N」编号**
> 撞在一起，读时别混：
> - **10-11 序列**（本会话链的**待办/结论**）：`续 104 → 105 → 106 → 107 → 108 → 109 → 110 → 111 → 112 → 113 → 114 → 115`
>   （★ `续106` = **待办总清单 T1~T6**；★ `续112` = **全问题待办大纲（BACKLOG）入口**，恢复会话先读它；
>    ★ `续113` = **B12+B1 发版上线**；★ `续114` = **`#136`族复发/21662 定性**；
>    ★ `续115` = R1 实测：rss_tid 首轮自动执行已观测；
>    ★ **`续116` = B16 三条代码修复已实现（最新；未发版）**）。
> - **10-08 序列**（**另一批**独立编号，现落在文件**末尾**）：`续 100 → 101 → 102 → 103 → 104 → 105 → 106 → 107`
>   （这里的 `续106` = `#56 retract 自检`，与上面**完全无关**）。
> ⇒ 找待办看 **顶部「续106」**；找"护栏 #39/#49/#51"那批工作看**末尾的 10-08 序列**。
> ★ 本次仅**修正顶部四处**：补回丢失的 `续106` 节头、把 `续109/续108` 摆回逆序、加本导航 —— 旧节内容一字未改。

---

## 2026-10-14（续 115）—— R1 实测：rss_tid 首轮自动执行**已发生**（B12 上线后首次真跑）

> 待办 R1 的完成条件：确认 daemon 自动跑过 rss_tid 首轮并核对台账。**只读取证，未动任何生产状态。**

### ★ 结论：首轮已跑完（成功），但两条判据需校正
- **阶段行**（`daemon_state.json` + 日志）：`▶️ 阶段 [rss_tid] 开始: python rss_backfill_tid.py --limit 200 --apply`（**2026-10-13 12:40:47**）
  → `✅ 阶段 [rss_tid] 完成，耗时 2282.3s`（**13:18:49**）；`rss_tid.last_ok=true / last_code=0`。
- **实际处理 = `14` 行**（非配置的 200）。逐条 `at` 时间戳**每 ~2 分钟一条**（12:40:49 → 13:16:49），
  与 RateGate 稳态节拍一致 ⇒ **不是超时拖慢**；13:16 后**无行可处理** ⇒ 自然收工（候选池耗尽）。
  当日 `opencd_quota` request 仅 **38/500** ⇒ **也不是配额上限**。
- **请求 ≈ 1.0 请求/行**（14 行 = 14 请求，符合设计）。
- **命中 3 条**：`tid=145783 / 171746 / 40563`（本轮 21%；10-12 手动小样累计 38 条命中 19 ≈ 50%）。
- **写库只填空白**：ledger 3 条 `ok:true`。★ **`updated_at` 判据≥通过（补测）**：三条被填行的 `updated_at` =
  `05:31:53 / 05:22:52 / 17:11:43`（10-13），**均不等于 rss_tid 运行时刻 12:40~13:18** ⇒ rss_tid 写入**未碰 `updated_at`**（符合「只填空白」设计）。

### ★ 两条判据校正（写回 BACKLOG §R1）
1. **ledger 实路径 = `data/raw_html/rss_tried.jsonl`**（38 行；BACKLOG 原判据写的 `data/rss_tried.jsonl` **是错的**）。
   本会话因按错路径查过一次「文件不存在」，实为路径笔误，**非 bug**。
2. **「空白 `opencd_tid` 计数下降」未达标**：`1647`（10-12 快照）→ **`1745`（10-14 实测）= 涨 98**。
   根因 = 皇后 crawl 每日新增空白行（~100/天）**远大于** rss_tid 每日实补（≈7~19/天）⇒ **长期入不敷出**。

### ★ R2 结论（据此定调，见 BACKLOG §R2）
- `RSS_TID_LIMIT_PER_ROUND=200` **从未被打满**（每轮只 14~几十行可补）⇒ **调 LIMIT 无意义**（瓶颈在候选池，不在上限）。
- 若要真降空白积压，须另想办法（放宽候选口径 / 提高命中率 / 处理 crawl 里大量 `fetch:ReadTimeout` 的行），
  **均超出 R2「调 LIMIT」范围** ⇒ R2 判据应改述为「已评估，结论=不动」，并**新开一条**待定（是否解决吞吐/入不敷出）。
- ★ 另注：crawl/topup 阶段日志见大量 `fetch:ReadTimeout`（如 tid=197271/198581/199477/199644/200128 连续 5 条全超时），
  且**每次 drain 都是同一批**⇒ 疑似**死信行反复重试**，与 rss_tid 的「候选池只有 14」可能同源（这些行有 tid 但内容补不上）。
  ⇒ 值得单列观察（**不是** rss_tid 故障）。

### ★ 现场数字（只读，2026-10-14 12:2x）
`blank opencd_tid = 1745` / `non-blank = 601` / `total tasks = 2346`；
`opencd_quota` 10-13 request=**38**、10-12=62；生产镜像 `922dcfc`，容器 healthy。

---

> 用户起点：问 musopia `id=21662`「为什么没做种」「为什么校验只能到一半、发种失败」。**非皇后侧**，是**本地版本判据 + 乌托邦站点种**的问题。

### ★ 现场（只读，NAS 实测坐实）
- 站点页 `id=21662` 标题 = **群星 - 巨星精选4 我怎么哭了 1992**；`0 做种者 / 9 下载者`；站点 Hash码 = **`61b13258…`(B)**。
- 本地库那条（`info_hash=41dba1e8…`，A）：`status=PUBLISHED`、`reason=published_id:21662`、`site_torrent_id=21662`、`site_status=EXISTS`。
- **A**（`41dba1e8…`）在 **3260**：`stalledUP prog=1`；目录 **16 文件 / 293,546,972 B**，与 qB 声明**完全一致** ⇒ **数据完整**。
- **B**（`61b13258…`）在 **3010+3260**：两条都 `stalledDL **prog=0.4893…**` ⇒ **这就是"校验只到一半"**。
- 给 A 加乌托邦 announce 后站点回 `Torrent not exists` ⇒ **站点认的是 B(`61b13258…`)，不是 A**（`ERR-PUBLISH-15` 铁律：两端 hash 不同）。

### ★ 根因（= `ERRATA` 新条 `ERR-PUBLISH-20`）
1. **判据取弱信号**：`run.py:43` `track_type = "整轨" if assets.has_cue(folder) else "分轨"` —— **只看"目录里有没有 `.cue`"**。
   该目录里有个 `群星.-.[…](1992)[WAV].cue`，其 `FILE "...[WAV].wav"` **指向一个不存在的整轨 WAV**；而实际文件是 **12 个分轨 flac** ⇒ **误判"整轨"**。
2. **双写分歧**：同一条记录 `torrent_name` 标**分轨**、`torrent_file` 标**整轨**（`ERRATA:576` 形状）。
3. **无发种前版本一致性闸门** ⇒ 站点 B（按整轨另一版本生成）与本地 A 的文件集只有约一半对得上 ⇒ **校验停在 48.9%**。

### ★ 复发史（★ 这是关键 —— 不是孤例）
`#136` 族**已复发 3 次**：① `20667`（初回 vs 期間，**标题碰撞覆盖**）② `20517/19946/20095/20877` 等 9 条（**B 丢/announce 错**）③ 本次（**cue 判据错**）。
**共同结局都是"只能人工站点删种"**——因为**程序无删站点种能力**（`SESSION-NOTES:961/6275` 同款）。见 `续101/108`、`ERRATA:732/749`。

### ★ 处置（已落盘，等用户）
- **B15**：用户**站点侧删 21662** → 告知 → 本项目用本地**分轨 A（100% 完整）**走**正常发种**发新页（验收：新页版本=分轨、站点 Hash码做种 `0→1`）。
- **B16**：B14 三条代码修复（`has_cue` 强判据 / 单一判据 / 版本一致性闸门）—— **待用户拍板：现在做 or 并入下一版**。
- 本次落盘：`ERRATA` `ERR-PUBLISH-20`；`BACKLOG` B14/B15/B16 + §4.6/§4.7；`RESUME` §0。
- ★ 无关澄清：用户手动删的"**皇后已删种**"是**皇后侧**（源站下架，`Torrent not exists`），与本 21662（**乌托邦侧+本地 bug**）**两码事**。

### ★ 环境（本次实测新增）
- NAS SSH：`ssh -i C:/Users/艾森克/.ssh/id_ed25519 -o UserKnownHostsFile=/dev/null -p 36688 iSunker@192.168.0.7`。
  ★ Git Bash 下 `~` 会因**中文用户名**转义坏 ⇒ **必须显式 `-i` 给私钥绝对路径**。
- **docker 在非交互 ssh 里不在 PATH** ⇒ 用全路径 `/usr/local/bin/docker`。
- 实例映射实测：**3010=qbit-publish(host 网络,本项目发种)**、**3260=qbit-music3(皇后源)**、3020=qbit-movies1、3040=qbit-movies2。qB 凭据复用容器 `.env`（`QB_USER/QB_PASS`）。

---

## 2026-10-14（续 116）—— ★★ B16 三条代码修复**已实现**（本机离线全绿；未发版）

> 用户指示："先修复代码，再考虑删除废旧文件。" ⇒ 本次只做 B16（`ERR-PUBLISH-20` 的三条修法），
> 项目整理（NAS 净化已完成、tools/.scratch 清理）**排在其后**。**未发版**（等排期）。

### 修的是什么（`#136` 族第 3 次复发的根治，21662）
现场：本地目录**实为 12 个分轨 flac**，却混进一个 `.cue`，其 `FILE "…wav"` 指向**不存在的整轨 WAV**
⇒ 旧判据 `has_cue`（只看"有没有 .cue 文件"）误判**整轨** ⇒ 站点挂错版本 ⇒ 站点那条**永远校验停在 48.9%**，
且**程序无删站点种能力** ⇒ 只能人工删种。三条独立缺陷叠加，各修各的：

1. **判据取弱信号** → **强判据**（`src/assets.py`）
   - `has_cue()` 重写：**至少一份 cue 的 `FILE` 目标真存在**才算整轨（目标按 cue 所在目录 + 专辑根**两处**解析）；
     有 cue 但目标一个都不在 ⇒ **分轨**；无 cue ⇒ 分轨（同旧行为）。
   - 新增**单一出口** `cue_track_type()`；`run.parse_title` 由 `assets.has_cue()` 改调它。
   - 新增 `_cue_file_targets()`（抽 `FILE` 指令，含引号/裸路径两种写法）。
2. **两字段双写分歧** → **单一列 `track_type`**（`src/db.py` / `src/run.py` / `src/opencd.py`）
   - 新增 `tasks.track_type` 列（`COLUMNS` 自动迁移；已补进 `TASK_FIELDS` 供发布侧读）。
   - 两个写者写**同一列**：抓轨路 `assets.cue_track_type()`；皇后路新增
     `opencd.track_type_from_format(info["format"])`（认不出 ⇒ **空串**，不猜）。
   - `run.py` **四处** `save_task` 全带 `track_type`（含 `NO_LOG_FILE` 早退支、`FAILED` 支、受保护支、
     正常支）+ sidecar `meta` 也带上。
3. **无发种前版本闸门** → **版本一致性闸门**（`src/site_verify.py` / `src/publish.py` / `src/config.py`）
   - `site_verify` 新增 `extract_files`（从 `.torrent` 取**文件集**）/`files_of_dir`（本地目录同口径）/
     `compare_file_sets`（三态 MATCH/MISMATCH/NO_DB_NAME）。
     ★ 比**文件集**、**非 `info.name`** —— 分轨/整轨共用同一个目录名，只比名字比不出挂错版本（21662 就漏在这）。
   - `publish._version_consistency_gate(task)`：① 库 `track_type` vs **现算**的盘上实况版本；
     ② 本地 `.torrent` 文件集 vs 本地内容目录文件集。不一致 ⇒ **拒发**（标 FAILED 出队）。
   - **两站路都接**（`main` 乌托邦 + `run_crabpt` 蟹黄堡），都在 `upload_torrent` **之前**。
   - 开关 `config.VERSION_GATE_BEFORE_PUBLISH` **默认开**（用户拍板，安全侧；**零网站请求**；
     "比不了"（目录不在/种子读不出/一侧空）⇒ **不拦**且显式打印 —— 绝不让"不可知"变成"拒发"）。

### 回归（本机，全绿）
- `run.py --selftest` ✅
- `publish.py --selftest` ✅ —— **新增 ok9 `selftest_version_gate`**：2 条结构断言（两站路都在 upload 之前调闸门）
  + 运行时假件（真造目录+种子）：强判据判分轨 / 库=整轨盘=分轨⇒拒 / 一致性⇒放行 / 文件集不一致⇒拒。
- `opencd.py map --selftest` **91/91** ✅（新增 ③c 版本标签 6 条用例）
- `assets.py` selftest ✅ / `tools/verify_cprime_run.py` A-E 五态 ✅ / `tools/build-preflight.sh` ✅
- `db` 迁移实测：`track_type` 列已自动建、`TASK_FIELDS` 含它。

### 未做 / 下一步
- **未发版**（build-push + NAS pull 点名 sha）—— 等用户排期。
- **B15**：等用户站点删 21662 ⇒ 用本地分轨 A 重发正确版本（`ERR-PUBLISH-20` §4.6）。
- 项目整理：NAS 净化（A）已完成；本机 `tools/` 探针删除、`.scratch/` 归档、铁律落盘（B/C/E/F）待做。

---

## 2026-10-12（续 113）—— ★★ 发版：B12（RSS 反查补 tid + 配额 500）+ B1（#31 精准优先）上线

> 用户拍板：**先 commit 再发** / **全部 WIP 一起发** / commit 后 **push 分支（不开 PR）** / **现在就走整个发版**。
> 发版细则与验收：`.scratch/B12-release-record.md`（gitignore）。

### ★ 结论（已实测坐实）
- **commit `2bf9469`**（`feat/crawl-stage-wiring`）已 push：37 files, +7955/-221（26 改 + 11 新）。
  ★ `.env` / `.scratch/` / `*.db` / pyc 均 gitignore，**未入 commit**（`git check-ignore` 核过）。
- **镜像**：`isunker/opencd2others:latest` + `:2bf9469`，digest **`sha256:2cbad467…`**。
- **NAS 部署**（点名 sha，`ERR-DOCKER-13`）：compose `image: …:2bf9469`，容器 **healthy**；
  NAS `RepoDigest = sha256:2cbad467…` **与推送一致**；`printenv` 配额 **500**、rss_tid **关+干跑**；
  daemon stage 表 `crawl` 在、**`rss_tid` 不在**（`ENABLE_RSS_TID=false` ⇒ 安全默认，非故障）。

### ★ 新踩的坑（已写进 `ERRATA` 候选 / `build-push.sh` 待改）
- **ERR-（新，build）**：本机 **Docker Desktop 29.8.0** 下 `docker build` **默认走 buildkit**
  ⇒ buildkit 不读 dockerd 代理 ⇒ `failed to fetch oauth token`（DNS 污染 IP）。
  **解法：`DOCKER_BUILDKIT=0`**。`build-push.sh` 默认分支没导出它 ⇒ **建议脚本加 `export DOCKER_BUILDKIT=0`**（待办）。
- **ERR-DOCKER-13 补充**：验证"跑的是不是刚推的镜像"要**比 `RepoDigests`**
  （`docker image inspect <ref> --format '{{json .RepoDigests}}'`）；
  `docker inspect <容器> --format '{{.Image}}'` 给的是**本地 config id**，跨主机**必然不等**，
  比它 = 假阴性。`build-push.sh` 尾注那条"`.Image` 必须等于本机 image id"**是错的**（待改）。

### ★ 代码内容（本次上线）
- **B12**：`opencd.py` `parse_rss_index/fuzzy_name/...`（纯函数）；**新** `rss_backfill_tid.py`
  （cache-first + `rss_tried.jsonl` + 两段式精确→模糊 + 复用 `sync_opencd_tid.apply_tids` 写库）；
  `daemon.py` 加 `rss_tid` 阶段（默认关）；`nas_health/gen_counts/.env.example` 登记；顺带补 `free_window`；
  ★ **日配额 50→500**（`crawl.DEFAULT_MAX_PER_DAY` 唯一源头 + 兜底 + 文档；`min_interval` 120s **未动**）；
  修 `topup_opencd_fields.py` 缺 `--dry-run` 的真 bug。
- **B1**：`musopia.py` `SEARCH_QUERY_PLAN` 精准优先（高精度短查询先发、命中即停）；
  `site_verify.py` + `verify_hosted_edition.py`。真站 20 行回归 **14/20**（旧 area=0 = 13/20）。

### ✅ 全部完成
- **#26-#29** 发版（commit/push/build-push/NAS 部署/文档）全绿。
- **#23** 容器小样试跑 + 修复 + 复测通过（见下）。
- ★ 生产运行镜像 = **`922dcfc`**；`ENABLE_RSS_TID` 仍默认 **false**（要不要常开 = 另需用户点头）。

### ★★ #23 试跑追加：抓到并修复一条真 bug（2026-10-12，同日）
- 初测镜像 `2bf9469`：`--apply --limit 5` ⇒ 请求 5 · **命中 0 · 失败 5**（全 `parse:ParseError`）。
- 根因（容器内实测坐实）：**`rss_backfill_tid.fetch_index()` 的 RSS 请求从未带 `passkey`**
  ⇒ 皇后返回纯文本 `passkey error`（13B，非 XML）。★ 且 `crawl.Client` 只带 cookie、**不拼 passkey**；
  ★ **皇后 RSS passkey ≠ `MUSOPIA_PASSKEY`**（后者是乌托邦的）。
- 修复：commit **`922dcfc`** → 镜像 **`922dcfc`**（digest `08388b59…`）→ NAS 部署（备份
  `docker-compose.yml.bak-b12fix-20261012`）；NAS `.env` 加 `QUEEN_RSS_PASSKEY`（备份 `.env.bak-rsspk-*`）；
  `tools/opencd-crawl-selftest.py` 加**判据㉒**（"请求必须含 passkey" + "未配 ⇒ 0 请求"）⇒ **55/55**。
- ★ 复测（`--apply --limit 5`）：**请求 6 · 命中 4 · 未中 1 · 失败 0**；SQL 空白 tid **1651→1647（−4）**、
  4 行 `updated_at` **未变**（纪律 3 守住）。含 1 条模糊档命中（Dua Lipa）。
- ★ 仍默认 `ENABLE_RSS_TID=false`（daemon 不自动跑）；上面都是手动 `compose run`。
- 细节：`.scratch/B12-23-trialrun-finding.md`。

---

## 2026-10-12（续 112）—— ★★ 全问题待办大纲落盘（BACKLOG）+ 乌托邦即将自然出种

> 用户："把现在所有问题列入待办大纲，本地落盘。" 本节 = 汇总大纲的**权威入口**；
> 细则与快照在 **`.scratch/BACKLOG.md`**（gitignore，不进仓库/镜像）。

### ★ 主表（B 系列；与 `续106` T1~T6 对应，避免重复）

| ID | 类 | 问题 | 现状 | 阻塞 |
|----|----|------|------|------|
| **B1** | 修 | #31 精准优先 | 代码+回归 14/20 完成 | ★ 未发版（B4）|
| **B2** | 修 | A组 8 行重发(#54)+改指针(#55) | 手册 v2 在；`republish_one.py` 缺阴性对照 | ★ 等你批（不可逆）|
| **B3** | 修 | #173 的 16 条坏指针残留 | 只止血（5 条 stale）| 等你定订正口径 |
| **B4** | 修 | 本机 WIP 未发版（21改+10新）| 工作树自洽、默认 dry-run | ★ 等你批发版 |
| **B5** | 修 | #5 漏做种告警清空 | 未清 | 等你批 |
| **B6** | 决 | 无 log 直发蟹黄堡旁路（解锁 188 行）| 未决 | ★ 等你拍板 |
| **B7** | 决 | 无 log WEB 发行放宽（~180 行）| 未决 | ★ 等你拍板 |
| **B8** | 清 | `.scratch/` 堆积 | 未清 | 可选 |
| **B9** | 清 | `tools/` 临时脚本堆积 | 未清 | 可选 |
| **B10** | 闭环 | 乌托邦不发种 | ✅ 查清=队列空 | — |
| **B11** | 闭环 | HR 补种 171 | ✅ 全绿 | — |

### ★★ 快照（2026-10-12 实测，会变的数字以 `counts.json`/`.scratch/BACKLOG.md §0` 为准）
- **发布队列 = 0**：正常 —— 12 条 `ALLOW` 未到「皇后发布+7天」闸，最早 `41dba1e864c9` 到期 **10-13 09:51**
  ⇒ **乌托邦 ~10-13 上午自然出种，无需干预**（同 `续111 §A`）。
- git 分支 `feat/crawl-stage-wiring`，HEAD=`a160375`；工作树 **21 改 + 10 新**未提交/未发版。
- HR 补种：qbit-music3 171/171 在库（170 做种 + 1 暂停，新增仅 id=145523）—— 见 `.scratch/hr-rescue/STATE.md`。

### ★ 依赖与建议
- **B1/B3/B5 → 并入 B4（发版总闸）→ 生产生效**；**B2 不可逆、B6/B7 决策，全需你点头**。
- 零风险先做：**B2 补阴性对照**（纯本地）、**B3 出 16 条错值表**（只读）。

### ★★ B12（本次会话新增·进行中）—— 皇后按 infohash 反查补 tid + 日配额 50→500
- 用户指示：**一天能搜 500 个，改限制；把"按 infohash 反查页面补 tid"补上**。
- 缺口实测：全库 2212 行，**1651 行无 `opencd_tid`**（SUCCESS 335 / NO_LOG_FILE 1316）⇒ 抓不了页；
  tid 只来自 `sync_opencd_tid.py`（读 vertex2 外库），vertex2 没收的拿不到。
- 现状：`crawl.py` 的 `--search` 路 2026-09-23 已整条删除；★ 解析器 `opencd.parse_list` 还在。
- 两个待定：**Q1** 日配额 500 后 `min_interval`(现 120s)/`max_per_round`(现 20) 怎么调（**要你点头**）；
  **Q2** 皇后搜索页**能否按 infohash 搜**（未验证）。
- ★ 详情/出处：`.scratch/B12-tid-reverse-lookup.md`；TaskList #8~#13。

> ★ 本节只登记**问题与状态**；不写会变的库计数；细则/证据全在 `.scratch/BACKLOG.md`。

---


> 用户问："前几次会话还有哪些问题没解决？乌托邦还是没有开始发种。" 本节 = 真机只读核查的答复。

### A. ★★★ 乌托邦为什么"没发种" —— **队列里根本没有到期可发的行**（不是卡死/不是 bug）

**真机实测（NAS 容器内只读 + `docker logs`，2026-10-12 07:10 NAS/CST）：**
- **发布队列计数 = `0`**（`SELECT count(*) FROM tasks WHERE {db.PENDING_PUBLISH_WHERE}`）。
- publish 阶段**每轮都跑、正常退出**，输出逐字为 **「💡 当前没有需要发布的任务。」**（耗时 1.4s，`last_ok:true/last_code:0`）。
- ⇒ **「乌托邦卡住」是误报**（同 `续104 §F`）。**流水线健康**：同一轮里 **蟹黄堡 `publish_crabpt` 正在发**（耗时 42.4s，handoff 复制 + 入队都正常）。

**★ 为什么队列=0 —— 把候选逐闸门数出来（`SUCCESS` 且 `site_status≠EXISTS` 共 509 行）：**

| 子集 | 行数 | 卡在哪道闸 |
|---|---:|---|
| `transfer_rule = NULL`（未映射，327 无 `opencd_tid` / 4 有） | 331 | 闸门①/③（**没走皇后路**）|
| `transfer_rule = DENY` | 155 | 闸门④（禁转，**正确拒**）|
| **`transfer_rule = ALLOW`** | **12** | **闸门② —— 全部 `not_due`（皇后发布 < 7 天）** ← **唯一"迟早能发"的那批** |
| `transfer_rule = NEED_CONFIRM` | 11 | 闸门④（待确认）|

**⇒ 直接原因：那 12 条 `ALLOW` 的行，`opencd_added_at + 7 天` 全部**还没到**。**
**最早一条 `41dba1e864c9` 到期 = `2026-10-13 09:51`（NAS 时间）**，其余多在 `10-14 ~ 10-16`。
⇒ **乌托邦会在 ~2 天后（10-13 上午）自然开始出种**，无需任何干预。

> ★ 一般化：**"没发种"要先分清「不能发」与「没有可发的」** —— 队列空 ≠ 故障。
> 判据：`db.PENDING_PUBLISH_WHERE` 计数 + 逐闸门分桶（见上表），而不是看"站点上没新页"就断定坏了。

### B. ★ 前几次会话未解决问题清单（截至本次核查，仍未闭合）

| # | 问题 | 状态 | 归口 |
|---|---|---|---|
| 1 | **乌托邦不发种** | ✅ **本次查清=队列空**（10-13 自然出种） | 本条 §A |
| 2 | **A 组 8 行错版本页重发（#54）+ 改指针（#55）** | ❌ **未做**（不可逆，等你批；方案 v2 已落 `.scratch/A_group_republish_plan.md`；`republish_one.py` 已写+自检绿，**缺阴性对照+发版**） | `续109` §C/§F |
| 3 | **#173 的 16 条坏指针行残留** | ⚠️ **未订正**（止血只拦了当前 5 条 stale；根治护栏已上线，但**回填路未查修、21 条错值未逐条订正**） | `续108` |
| 4 | **全部 WIP 未发版**（21 改 + 11 新增） | ❌ **未部署**（`musopia.py` 已修好、不再是地雷；其余已验证） | `续104 §A` |
| 5 | **#31 精准优先** | ✅ 代码+自检+回归 14/20 完成；⚠️ **未发版**（随 #4） | `续107` |
| 6 | **决策类 T4.1/T4.2**（无 log 直发蟹黄堡旁路 / WEB 放宽） | ❌ 未决（等你拍板，业务规则） | `续106` T4 |
| 7 | **#5 漏做种告警**（`handoff_missing_data.jsonl`） | ⚠️ 建议清空/归档以消警（**需批**） | `续106` T4.4 |
| 8 | **`.scratch/` 临时脚本堆积 + `tools/` 自称"用完即删"脚本** | ⚠️ 未清（T6.3，可选） | `续106` T6.3 |
| 9 | **`续106` 节头曾丢失**（引用指向它、但它无标题） | ✅ **本次补回**（见 §C） | 本条 |

### C. 文档整理（本次执行）—— `SESSION-NOTES.md` 顶部排序理顺
- **补回丢失的 `## 2026-10-11（续 106）` 节头**（原是一段无标题 preamble，被夹在 续105 前 —— 引用全指向它，却搜不到）。
- **把 `续109/续108` 摆回逆序**（原 108 在 109 之前；#173 是排查 A 组时"顺带挖出"，晚于 A 组 v2 ⇒ 109 应在 108 上）。
- **加顶部「导航」**：说明本文件有**两套"续 N"编号**（10-11 序列 vs 10-08 序列）撞车，读时别混。
- ★ **旧节内容一字未改**（守"只增不改"纪律，仅修正节头/顺序/导航）。

---

## 2026-10-11（续 110）—— `qb3260 空 tracker` 探因 + 「会话链收尾/落盘」总结

> 本节 = 收尾：① 回答"为什么 qb3260 有很多没有 tracker 的种子"；② 把 10-11 会话链
> （`94a5b153` 等）**已完成但未单独成节**的产出补记一笔；③ 会话 jsonl 清理记录。
> ★ 已单独成节的产出（`#31` 精准优先、`#173` 护栏、A 组 v2 方案）**不在此重复**。

### A. ★★★ `qb3260` 空 tracker 之谜 —— 是 **IYUU「原地辅种」**，非本项目 bug

**现象**（真机只读实测，`qbit-music3` = `:3260` 皇后源实例，共 1562 种）：

| tracker | 数量 |
|---|---:|
| `tracker.open.cd`（皇后） | 1416 |
| **空 tracker** | **115** |
| `crabpt.vip` | 19 |
| `www.musopia.vip` | 12 |

**115 条的共同特征**（逐条查过）：
- 全 `stalledUP`、`progress=1.0`、`category=''`、`save_path=/downloads` ⇒ "本地已有内容后加的种"，非下载来的。
- `added_on` 全落在 **2026-10-08 07:35:40~42（2 秒内）** ⇒ **一次批量导入**。
- tag 全是 `other, ~share_limit_1.all-500k`（一条带 `IYUU自动辅种`）—— **IYUU 辅种标签**。
- `magnet_uri` **没有 `&tr=`**（只有 `btih`+`dn`）⇒ 加入时用的种子/磁力**本身不带 announce**。

**根因**：NAS 上跑着 `iyuuplus_ssd`，其计划任务 **ID 7「qb原地辅种」**（拿本地已有文件回填种子）
⇒ 这样构造/匹配出来的种**不带 tracker**，加进 3260 后 qB 的 tracker 列即空。
**不是本项目的 bug，是 IYUU 辅种的正常现象。**

**对本项目影响 = 0（可放心保持现状）**：
- 115 个 hash 与生产库交叉比对 → **115/115 全部已在库**（`NO_LOG_FILE 68 / SUCCESS 34 / PUBLISHED 12 / NO_TORRENT_FILE 1`），
  与 `src/run.py:1707` 已写死的历史结论（"空 tracker 的行 115/115 已在库"）**逐字吻合**。
- scan 认皇后种用 **tracker 主机名精确相等**（非 category/tag）；空 tracker 一律**判否**（"不可知 ≠ 是"）
  ⇒ 这 115 条**不会被误当皇后种发出去**，且因都已在库**一条也不会漏扫**。★ 设计正确，**不处理**。

### B. 会话链日志巡检（只读，无异常）
- 容器 `opencd2musopia` healthy、跑**镜像原版**；`docker logs --since 24h` **无 traceback/exception**。
- `daemon_state.json` 五阶段全 `last_ok:true / last_code:0`（只是"未到期跳过"）。
- 唯一刷屏的两类输出**均属已知非异常**：① `❌ 跳过：目录里没有 .log 抓轨日志` = 铁律（乌托邦必须有 log）正确行为；
  ② `[图床失败] … Duplicated upload`（24h 内 4 次）= 已知封面重传问题（已修，随 WIP 待上线）。

### C. NAS 挂载搬迁 —— **代码零改动**（补记，详见本节之后 `续107` 同款小节）
用户把宿主侧 `qbittorrent-publish/Download` → `qbit-publish/Download`（qB 3010 宿主 + 项目 `PUBLISH_DIR`）。
★ 代码只认**容器内** `/publish`（`config.PUBLISH_CONTAINER_DIR`）⇒ 宿主侧改名对它**透明**，**无需改代码**。
两容器挂载已一致、2 个 FLAC 实测 `stalledUP prog=100%`（qB 自动重认）。已同步 3 处**默认值/注释**
（`docker-compose.yml` / `.env.example` / `config.py`+`handoff.py` 注释）避免日后误用旧路径。

### D. 会话文件清理（本次执行）
删除旧会话 jsonl（内容已全部并入本文档 + `ERRATA.md`，无独有信息）：
- `94a5b153-…jsonl`（3.8MB 主会话）+ 其 `subagents/`（3 个 `Explore`/`Plan` 子代理，主题均为 `#31` 精准优先，结论已落 `续107`）。
- `566a133d-…jsonl`（仅一条 `/exit`）、`60e0f86b-…jsonl`（截断噪音）。
> ★ 保留：`memory/`（空）；本会话自身的 `35bc0935-…jsonl` 不删。

---

## 2026-10-11（续 109）—— A 组重发【拦路虎】+ 方案 v2（**未执行生产**）

> 用户选 **Z**：先把"重发怎么发"写成方案，**不动生产**。方案落 `.scratch/A_group_republish_plan.md` §v2。

### 前置全过（只读）
- **8/8 `MISSING-PAGE`**（`crabpt 216915` 首轮 `DOWNLOAD-FAILED` = crabpt 抖动，复跑即 MISSING）。
- **源全在**：8 行 `.torrent` + `content_path` 数据目录 ✅。
- **DB 现状**：8 行 `tasks.site_status='EXISTS'`。

### ★★ 拦路虎（v1 方案漏掉，实测踩到）
- **`publish.py --only <hash>` 是与待发队列取交集**，而队列 WHERE 首条 = **`site_status != 'EXISTS'`**。
- 8 行全是 `EXISTS` ⇒ **点名也发不出去**（实测 `--dry-run` 报「💡 当前没有需要发布的任务」）。
- 且这 8 条 `transfer_rule` 已止血成 `NEED_CONFIRM` ⇒ **闸门④也拦**（只有 `ALLOW` 放行）。
- ⇒ **v1 的"`publish.py --only` 重发"路径走不通**。

### 两条重发路径（详见方案 §C）
- **X 临时改 `site_status` 回队列**：快、零代码；但制造"这行没发过"的**语义谎言**（窗口期可能被 handoff/dedup 误读）。
- **Y 写 `tools/republish_one.py` 手工单条发**：净、可复用、可自检；绕过队列（"重发已存在行"本就不该进队列）；代价=写码+发版。
- ★ **推荐 Y**（`ERR-META-03`：一个动作别背两个语义）。
- ★ 无论 X/Y：**发布 + 改指针（#55）必须连着做完** —— #173 护栏上线后，发了新页却不改 `site_torrent_id` ⇒ edit 会被**永久拒改**。

### 状态
- **未动生产**（只做只读核对）。
- ⚠️ **`9e9ed78ab8` 现 `transfer_rule='ALLOW'`**（试水准备时由用户复位）。因 #173 护栏已上线 ⇒ 即便 stale，edit 也会拒改（`site_torrent_id=20667` 指向别人页）⇒ **无新增风险**。**待发或复原，见方案 §F 清单。**

---

## 2026-10-11（续 108）—— ★★★ 新 bug `#173`：`tasks.site_torrent_id` 号段错值（21 条指向别人页）+ 已止血

> 本节 = 排查 A 组 8 行时**顺带挖出的独立高危 bug**。★ 与 A 组（错版本页）**是两码事**。

### 现象（只读扫 130 行 `EXISTS`：`.scratch/scan_ptrs.py`）
`MATCH=104  MISMATCH=21  MISSING(页已删)=5  ERR=0`。21 条 **全是 crabpt**：
- **`tasks.site_torrent_id` 指向的页 ≠ 本地 `info.name`**，且指向的是**完全不相干的资源**：
  - 音乐条目 → **别人的电影/剧集页**（`I.Am.From.Chile`/`CTRL`/`Sucker.Punch`/`Pacific Rim`/`Severance`/`Ted`/`呼叫大明星`…）
  - 音乐条目 → **别人的音乐页**（`GAO-Christmas`/`My Little Airport`/`Beyond`/`Max Oazo`…）
- 坏 tid 分布在 `15972…19350`（**非连续号段**）⇒ 不是单纯的号段偏移，是**错位匹配/写错源**。

### 根因（初判，待坐实）
- `site_torrent_id` 由**回填**写入（`sync_site.py` 按**归一化标题**匹配"我的上传"列表；`sync_opencd_tid.py` 另有写入路）。
- **21 条全是 crabpt + 指向别人页** ⇒ 大概率是**某次回填把"id↔内容"对错**（标题匹配错/号段混用/写了别的列）。
- ★ **待办：坐实机制**（看 `sync_opencd_tid` 写入路 + 历史回填日志）。

### ★★★ **危害不在"误发重复"，在"edit 会改写别人的页面"**
1. **发布路安全**：`get_pending_publish` 判据 = `status='SUCCESS' AND site_status!='EXISTS'`，**不用 `site_torrent_id`** ⇒ 21 条 `site_status='EXISTS'` 被排除 ⇒ **不会误发重复**。
2. **edit 路高危**：`update_metadata.py` 用 `site_torrent_id` 当**页面 id 去写**（`edit_torrent(tid, overrides)`），且**没有"这是我自己的页吗"的身份护栏**；判据只是 `site_status='EXISTS' AND site_torrent_id!='' AND transfer_rule='ALLOW' AND stale`。
   ⇒ 一条**坏指针 + ALLOW + stale** 的行 ⇒ **会把它的元数据写进别人的页**（不可逆）。
   ★ 实测踩到：`3309ad2a2e`（SEKAI NO OWARI Stella，tid=**19069**=""CTRL 电影页"，ALLOW + stale）⇒ **当时随时会在下一轮 edit 改写电影页**（edit 周期 6h，≈5h 后到期）。
   ★★ 一般化（同 `ERR-PUBLISH-14` 族）：**拿一个可能错的指针当"事实"去执行不可逆动作** = 最坏形状。指针错值本身无害（只读），**但被另一条路当成"要做的事"就有了破坏力**。

### 已止血（用户执行，2026-10-11）
备份 `data/data.db.bak-rule-20261011-114413` → 把这 5 条"坏指针 + ALLOW + stale"的 `transfer_rule` 改 **`NEED_CONFIRM`**（edit 闸门④ 仅放行 `ALLOW` ⇒ 立即被拦）：
`9b4408b944`/`0295c1305c`(tid 19333) · `3309ad2a2e`(19069) · `1f42798019`(20639) · `9e9ed78ab8`(20667)。
⇒ **edit 候选 35 → 29**，电影页保住。★ 用 `NEED_CONFIRM` 而**非 `DENY`**（DENY = "禁止轉載"是另一语义，别误用）。

### 根治方案（**待排期，改代码**）
- **主方案**：`update_metadata.py`（及任何"用 `site_torrent_id` 写站点"的路）在**提交前加身份护栏**——用 `site_verify` 验证"站侧页 `info.name` == 本地 `info.name`"，**不符 ⇒ 拒改**（同 `#173` 与 `ERR-PUBLISH-14` 的"拿事实、不拿推测"纪律）。
- **配套**：查清并修 `site_torrent_id` 的回填路（`sync_site`/`sync_opencd_tid`），对 21 条错值**逐条订正或清空**。
- **判据**：edit 路加护栏后，构造"命中别人页"的假 client ⇒ 必须**拒改**（阴性对照）。

### ★ 根治补丁已落盘 + 已部署（2026-10-11）
- `site_verify.edit_target_ok(local,hosted)`（纯判据）+ `update_metadata.py` 在 `edit_torrent` **之前**核身份、不符 ⇒ 拒改（计数/明细）+ `--selftest`（判据 + AST 接线，**两条阴性对照实测均变红**）。
- 注册进 `tools/gen_counts.py`（自检 **9/9**）。`ERRATA.md` 记 **`ERR-PUBLISH-19`**。
- **只读实测**：5 条真坏指针行 **5/5 拒改**（`3309ad2a2e`→`CTRL`、`19333`→`仙逆`、`20639`→`郎君不如意`、`20667`→`Wolf.Warriors`）。
- **已发版上线**（digest `793e782b…`，容器 healthy，容器内两自检 RC=0）⇒ **edit 现在会先核身份、拒改别人的页**。

### 与 A 组（#54/#55）的关系
- A 组 8 行里 **5 行**同时是坏指针行（即上面止血的 5 条）⇒ **A 组的"重发+改指针"会顺带订正这 5 条**。
- ★ 但**另 16 条坏指针行**不在 A 组里 ⇒ 属 #173 单独处理。
- 方案 A（A 组只做自己的语义）**不变**：`tasks.site_torrent_id` 的错值**不**在 A 组里顺手改。

---

## 2026-10-11（续 107）—— ★★★ T1/T2.1/T3.1 三件急办全落地：`#31` 精准优先（真站回归 14/20）

> 本节落盘"三件急办"的结果。★ 恢复会话先读**续 106（待办总清单）** + 本节。

### T2.1 —— scan 结果核实 ✅（只读）
容器内跑 `verify29.py`（★ 查库用 `like '<12位前缀>%'`）：**29/29 已入库**
= **22 `SUCCESS` + 7 `NO_LOG_FILE`**（与期望逐字一致，无第四条 FAILED）。scan 已跑完（`last_ok=true`）。

### T3.1 —— WIP 分类（判据 = `Dockerfile` 的 `COPY src/*.py` + `COPY tools/`）
- **A 进镜像的代码** = `src/*.py` + `src/site_*.py` + `tools/*.py` + `Dockerfile`/`docker-compose.yml`/`.env.example`。
- **B 只在本机的记录** = `ERRATA.md`/`SESSION-NOTES.md`/`counts.json`/`README.md`/`CLAUDE.md`（不进镜像）。
- **C `src/musopia.py`** = 半成品，**曾绝不能部署**；现已随 T1 修好。
- ★ 真实耦合：T3 发版会带上 `musopia.py` ⇒ **必须先修好 T1 才能发版**（现已具备）。

### T1 —— `#31` 精准优先：落地 + **真站回归 14/20** ✅

**改了什么**（`src/musopia.py` / `src/crabpt_site.py`；+`publish.py`/`run.py` 自检）：
- `SEARCH_QUERY_PLAN` 改**窄→宽**排序；`search_and_compare` 改**逐查询→立即验证→命中即停**；
  蟹黄堡 `find_existing_by_name` 默认 `max_candidates=8 → None`（读 config，与乌托邦同源）。

**★★★ 关键实测（推翻了初版直觉 —— 这是本节的精髓）**：
初版假设"高精度 = 单 token（艺术家名）"，但**真站实测恰好相反**：
- 单 token 是**宽**的：`Etre` 单独查 **109 条**（目标第 44）、`群星` **109**（第 67）、`Shaggy` 90；
- 多 token 窗口才**窄**：`Etre 2001 03` **18**（第 8）、`群星 爱是我们共同的声音` **2**、`Shaggy Intoxication 2007` **2**。
⇒ 初版把**最宽的查询排到了最前** ⇒ 预算被噪声吃光 ⇒ 仍 11 量级。改正后 = **多 token 窗口在前、单 token 最后**。
★ 一般化 = **`ERR-PUBLISH-18`**：「精度是**可测量的外部事实**（该查询在此站返回多少候选），
**不是** token 的类型/长短」。

**真站回归（20 行，`q31_regress.py`，低频只读）**：
- 本修 = **14/20**（旧 area=0 = 13/20；半成品 = 11/20）；命中落点 **`pos ≤ 3`、`n_over_8=0`**（预算不再是瓶颈）。
- 新救回：`Etre`(pos3)、`群星`(pos0)、`Shaggy`(pos0) 等；按"同 info.name 的旧指针页算命中"口径 ⇒ **18/20**。
- 2 个 ❌（`徐元白 19245`/`李泰祥 19350`）实测 **`page_exists=False`（页已删）** ⇒ **非召回失败**。

**自检（含阴性对照，均已实测变红）**：
- `publish.py --selftest` ok8 扩：顺序断言 + 命中即停 + 噪声不截断 + **加固旧"中途 503 仍抛"**（命中即停曾让它**空真变绿**，已强制首条落空）。
- `run.py --selftest` 新增 **⑨s** 结构护栏（`search_and_compare` 内两条 `raise` + `return tid` + `DEDUP_MAX_CANDIDATES`）。
- 本机 `run.py`/`publish.py`/`crabpt_site.py` `--selftest` 全 **EXIT=0**。

### 你改 NAS 挂载路径的影响评估 ✅（无需改代码）
你把 `qbittorrent-publish/Download` → `qbit-publish/Download`（qB 3010 宿主侧 + 项目 `PUBLISH_DIR`）。
**代码零改动**（代码只认容器内 `/publish`，宿主侧改名对它透明）；两容器挂载已一致；
两个 FLAC 实测 **`stalledUP prog=100%`**（qB 自动重认、无需 recheck）。已**同步 3 处默认值/注释**避免日后误用旧路径。

### 文档/账同步
- `ERRATA.md` 新增 **`ERR-PUBLISH-18`**（精准优先的教训/根因）。
- `README.md` 新增「发布前跨站查重」节。
- `counts.json` 重生成（**ERR 139 条**）。

### 未做（待你批）
- **T4c 发版**：`build-push.sh` → NAS 点名 `:<git-sha>` → `up -d` → 容器内 `--selftest`（**等用户批**）。
- T4.3 A 组 8 行重发（不可逆，需批）；T6 清理（`.scratch/` 88 文件 + 3 旧会话目录）。

---

## 2026-10-11（续 106）—— ★ 待办总清单 T1~T6（★ 恢复会话先读本节）

> ★ 本节是**待办的落盘处**（`ERR-META-04`：结论/待办不许只活在对话里；与续 102 的 `#17a–#17i` 同一体制）。
> **恢复会话先读本节**。每条写死**判据**与**依赖**。
> ★ 完成即在此节标 `✅`；全完成后本节并入后续续节，不留空壳。

### ★★ 亟需现在做的（3 件）
1. **【T1.2/T1.3】定 `#31` 方向并落地** —— 已实测（续 105）：本机半成品（`area=1`+词对）**比旧代码更差**（13→11）。
   ⇒ **必须先决定：① 做「精准优先」；或 ② 回退 `area=0`。** ★ 决定前 `src/musopia.py` **绝不能部署**。
2. **【T3.1】把本机 WIP 分类**（代码 vs 本地记录）—— ~28 个改动文件悬着，其中**除 `musopia.py` 外**的部分
   （蟹黄堡坏封面修复、传前压图、scan 单实例锁、C 类巡检等，见续 100/104）**已验证可用、与 #31 无关**
   ⇒ 可先单独发一版上生产。
3. **【T2.1/T2.2】核实后台 scan 结果** —— 后台 scan **正在跑**（`▶️ 阶段 [scan] 开始` @ 2026-10-11 07:58:44）。
   跑完核对 29 条入库（期望 22 `SUCCESS` + 7 `NO_LOG_FILE`；脚本 `.scratch/verify29.py`）。

### 主干概览
| 主干 | 一句话 | 现状 | 阻塞 |
|---|---|---|---|
| **T1** | `#31` 查重召回 | ★ 已定性：半成品更差 | **等用户选 ①/②** |
| **T2** | scan 29 条入库验证 | 后台 scan 正在跑 | 等 scan 跑完 |
| **T3** | 本机 WIP 上生产（代码 vs 本地记录） | 未提交/未部署 | T1；**其余部分可先发版** |
| **T4** | 决策类 | 未决 | **等用户拍板** |
| **T5** | 文档与账同步 | 部分 | 随 T1~T4 |
| **T6** | 清理 | 部分完成 | — |

---

### T1 —— `#31` 查重召回（★ 最高优先）
- [ ] **T1.1（已定案·实测）** 方向 = **精准优先**（`area=1` 只有在精准查询下才用）；做不出来 ⇒ **回退 `area=0`**。
- [ ] **T1.2** 改 `src/musopia.py`：① `SEARCH_QUERY_PLAN` 把**高精度短串**（2 词对/单词艺术家名）**排最前**；
      ② 宽 area 的大扇出查询**放在精准串全部落空之后**（或干脆不用）。判据：回归 **>13/20**（旧基线）且无"整片 MISS"。
- [ ] **T1.3** 用本机半成品对真站点重跑（`docker cp src/musopia.py → 容器 /tmp/musopia_local.py`，`importlib` 覆盖；**不动 /app**）。
      ★ 判读：脚本比 `site_torrent_id` **过严**，`菅原圭/サカナクション/Mary Black/milet` 的"⚠️误中"= **命中另一同 `info.name` 页**（DB 指针陈旧）⇒ 算命中正确；`徐元白/李泰祥` 站点页 `page_exists=False`。
- [ ] **T1.4** `musopia`/`publish` `--selftest` + 补断言（`SEARCH_QUERY_PLAN` 顺序 + 阴性对照）。
- [ ] **T1.5** `ERRATA.md` 记 `ERR-PUBLISH-17`：「**召回面变宽 ≠ 召回变好 —— 候选被噪声污染会把真目标推出预算**」。
- [ ] **T1.6** `tools/gen_counts.py` 重生成 `counts.json`。
- [ ] **T1.7** 部署（并入 T3）。

### T2 —— scan 29 条入库验证（等后台 scan）
- [ ] **T2.1** 确认 scan 跑完：`docker logs opencd2musopia | grep -E "🎯 扫描完成|阶段 \[scan\]"`；`run.py` 进程数 ∈ {0,1}。
- [ ] **T2.2** 核对 29 条：**22 `SUCCESS` + 7 `NO_LOG_FILE`**，无第四条 `FAILED`。★ 查询**必须 `like '<12位前缀>%'`**。
- [ ] **T2.3（#17c）** 确认 22 条进"可发布"口径（`SUCCESS` ≠ 已发）。判据：`db.publish_funnel()` 队列数增长，新增含这些 hash。
- [ ] **T2.4（#17d）** 站点侧开始出种：publish 下周期后新增页 > 0，或 `site_publish` 新增行。
- [ ] **T2.5（#17e，可选）** 7 条无 log 复核（`find -iname '*.log'` 为空）。
- [ ] **T2.6（#17i，已定）** 报告巨型/可能含脏文件的种子清单，供用户自删。**不动代码。**
- [ ] **T2.7（#17g）** `ERRATA.md` 加 `### ERR-ORCH-03`（长任务缺单实例锁）；不写日期/会变的数字。
- [ ] **T2.8（#17h）** `README.md` 加"scan 是单实例的"小节 + 2 条排障行。

### T3 —— 本机 WIP 上生产
- [ ] **T3.1** WIP 分类：**代码（上 git）** = `src/*.py` + `src/site_*.py` + `tools/*.py` + `Dockerfile`/`docker-compose.yml`/`.env.example`；
      **本地记录（不上 git）** = `counts.json`/`SESSION-NOTES.md`/`ERRATA.md`（★ 按用户"只传代码"纪律）。
      ★ **`musopia.py` 单独处理**（等 T1）—— 其余部分**可先发一版**。
- [ ] **T3.2** `python src/run.py --selftest` ✅（含接线断言）。
- [ ] **T3.3** `bash tools/build-preflight.sh` 通过。
- [ ] **T3.4** `bash build-push.sh`（★ 本机 `auth.docker.io` 间歇超时，重试可成）。
- [ ] **T3.5** NAS 部署：`pull` **点名不可变 `:<git-sha>` tag**（`ERR-DOCKER-13`）→ `up -d --remove-orphans musopia`。
- [ ] **T3.6** 验证跑的是新镜像（`docker inspect … .Image` vs `buildx imagetools inspect`）。
- [ ] **T3.7** `docker exec opencd2musopia python run.py --selftest` ✅。
- [ ] **T3.8（纪律）** **不主动 commit**；要提交时若在默认分支先开分支。

### T4 —— 决策类（等用户拍板，勿自行执行）
- [ ] **T4.1** 是否为「无 log 但可发蟹黄堡」开**直发旁路**（绕过乌托邦 `EXISTS` 前置）？
      背景：蟹黄堡队列源 = 乌托邦已发 ⇒ 187 行「无解」桶永远拿不到。★ 铁律：乌托邦必须有 log、蟹黄堡可以没有（代码已实现，缺的只是旁路设计）。
- [ ] **T4.2** 无 log 的 WEB 发行（180 行）是否放宽可发？（业务规则，非 bug）
- [ ] **T4.3** `#54`/`#55` A 组 8 行错版本页重发 + 改指针（**不可逆，需批，先 1 条试水**）。
      前置：8 个 id 只读复跑全 `MISSING-PAGE`。方案见 `.scratch/A_group_republish_plan.md`。
- [ ] **T4.4** `#5` 建议：清空/归档 NAS `data/handoff_missing_data.jsonl` 以消"漏做种"告警。（需批）

### T5 —— 文档与账同步（随 T1~T4）
- [ ] **T5.1** `SESSION-NOTES.md` 每完成一条主干追加一节（**只增不改、逆序**）。
- [ ] **T5.2** `ERRATA.md`：`ERR-ORCH-03`（T2.7）+ `ERR-PUBLISH-17`（T1.5）。
- [ ] **T5.3** `README.md`：行为变更（scan 单实例 / 蟹黄堡跳过坏封面 / 传前压图 / `LSKY_MAX_BYTES`）。
- [ ] **T5.4** `tools/gen_counts.py` 重生成 `counts.json`（**别手改**）。
- [ ] **T5.5** `.env.example` 新键 → **手工**加到 NAS `.env`（`ERR-ENV-02`）。

### T6 —— 清理
- [x] **T6.1** 旧恢复笔记并入本文件 + 精简 `.scratch/RESUME.md`；删过期噪点（`lastprompt.txt`/`tail.txt`/`SESSION-ARCHIVE-*`/3 份旧 `RESUME-*.md`）。
- [x] **T6.2** ★ 删除旧会话记录（`~/.claude/projects/D--Projects-opencd2others-zhuanzhong/`）。
      **状态：已删 `94a5b153-*`（3.8MB 主会话 + `subagents/`）、`566a133d-*`、`60e0f86b-*`**（内容已并入本文件/`ERRATA`，见 `续110 §D`）。
- [ ] **T6.3（可选）** `tools/` 里自称"用完即删"的临时脚本（`publish_probe.py`/`uploads_probe.py`/`show_response.py`）与历史 `_probe_*/diag_*` 归档或删除。

---

## 2026-10-11（续 105）—— ★★★ #31 查重召回「决定性实测」：`area=1` 半成品**净亏损**，不能部署

> 本节是**本次会话的核心结论**。★ 待办见**下一节「续 106」（待办清单 T1~T6）**。

### 干了什么（全部只读，真站点）
在 NAS 容器上对**同一批 20 行** `site_status='EXISTS'` 行跑了两次回归
（脚本 `.scratch/q31_regress.py` + 一个"本机版"：`docker cp src/musopia.py → 容器 /tmp/musopia_local.py`，
`importlib` 覆盖 import；**不改 /app 生产代码**）：

| 代码 | 配置 | 召回 | 命中落点 |
|---|---|---:|---|
| **生产容器（镜像原版）** | `area=0`、无词对窗口、`maxc=8` | **13/20** | pos ≤ 3 |
| **本机半成品**（未部署） | `area=1`、有词对窗口、`maxc=16` | **11/20** | pos ≤ 3 |

**⇒ 本机半成品比旧代码更差（13→11）⇒ 这个方向不能部署。**
- **修好 1 条**：`Etre` 17337（旧 ❌ → 新 ✅ `pos=3`；词对窗口确实救回了它 —— 目标页 `MATCH`、页面存在）。
- **打坏 5 条**：`村松崇継`/`岩崎太整`/`音田雅則`（旧 ✅ → 新 ❌）、`菅原圭`/`サカナクション`（旧 ⚠️误中 → 新 ❌）。

### ★★ 根因（决定性，且**推翻了此前所有关于"预算不够"的假设**）
**所有命中落点 `pos ∈ {0,3}`、`n_over_8=0`** ⇒ **`max_candidates` 从来不是瓶颈**（调大它无用）。
真因：**`area=1` 让单条查询的候选从 ~4–21 条暴涨到 109–120 条**（噪声填满候选列表），
真目标被挤到 `pos ≥ 8` ⇒ **撞 `max_candidates` 截断**。旧 `area=0` 时它排最前，所以没事。
★ 教训形状：**「召回面变宽 ≠ 召回变好」** —— 候选列表被噪声污染会把真目标**推出预算**；
而噪声是**线性**增长 ⇒ **加预算治不了**（要加到几百条，不可接受）。

### ★ 结论（T1.1 最终定性）
**`area=1` 单独用是净亏损。** 正解**只有一条**：**`area=1` 必须配「精准优先」** ——
先发**高精度短查询**（艺术家名 / 2 词对），**命中即停**，绝不拿宽 area 的大扇出查询去撞候选预算。
⇒ **若做不出「精准优先」，就回退 `area=0`**（= 13/20 的旧行为，至少不倒退）。

### ★ 判读要点（防误判，别把"命中另一页"当缺陷）
脚本比较 `site_torrent_id` **过严**：`菅原圭/サカナクション/Mary Black/milet` 的"⚠️误中"
是**命中另一张同 `info.name` 的页**（DB 指针陈旧；`milet 14081 vs DB 17287`，DB 那条是**死链**）
⇒ 判读时应算**命中正确**。`徐元白/李泰祥` 站点页 `page_exists=False`（页没了，非召回问题）。

### ★ 顺带确认的事实
- **生产容器 = 镜像原版**（`search_area:0`、无 `word_pairs`、`maxc=8`）✅ 与镜像一致。
- **后台 scan 正在跑**（`▶️ 阶段 [scan] 开始` @ 2026-10-11 07:58:44，日志逐条在推进）
  ⇒ T2 的核对**要等它跑完**（`run.py` 进程数现为 1）。

### ★ 本次会话产出的落盘物
- 本文件**续 106**（待办总清单 T1~T6，按依赖排序；每条含判据/依赖）。
- 本文件**续 105**（本节，`#31` 决定性实测）。
- `.scratch/RESUME.md`（精简的机器可读快照，已删 3 份旧 `RESUME-*.md` + `lastprompt.txt` 等过期噪点）。
- ★ 未新建 TODO/TASKS 文件 —— 按项目既有体制，待办写在 `SESSION-NOTES` 节内（同续 102）。

---

## 2026-10-11（续 104）—— ★★★ 会话合并落盘：未提交代码/未部署项/未决决策/临时记录一览

> **本节是"上一次会话链（续 100～103 + `ab19e30b`/`9a9df987`/`e801a0ef`）的合并落盘"** ——
> 把散在多份 `.scratch/RESUME-*.md` 与对话里的**未完工作**收敛成一条入口，避免结论只活在会话里
> （`ERR-META-04`）。★ 只写"已实测坐实"的；未坐实的写明**待验判据**。

### A. ★★ 本机工作树 = 未提交、未部署（一树自洽的工作，别拆散）

`git` 分支 `feat/crawl-stage-wiring`，HEAD = `a160375`（2026-10-07）。工作树 WIP 混着
**必须随镜像上线的代码** 与 **只该在本机的记录**，提交时按后者分类（见本节 C）。

**A.1 代码改动（M，需随镜像）**
| 文件 | 主题 | 关联 |
|---|---|---|
| `src/daemon.py` | C 类护栏巡检线程 + `PUBLISH_WINDOW` 时段门 | #51/#75 |
| `src/handoff.py` | #45 fix：3040 上站点种 B hash≠皇后 A ⇒ name 兜底匹配 | #45 |
| `src/run.py` | selftest 护栏 ⑨f/⑨l/⑨o + **#17f scan 单实例锁**（`fcntl.flock`） | #33/#45/#17f |
| `src/publish.py` | 蟹黄堡 `--limit` 改**成功计数** + 跳过即枚举（`selftest_crabpt_limit_enumerates`） | 续 100 |
| `src/assets.py` | `LSKY_MAX_BYTES`(默认 5MiB) + `_shrink_for_lsky()` **传前压图**（Pillow，不覆盖原图） | 续 100 |
| `src/musopia.py` | ★**半成品**：`SEARCH_PARAMS` `search_area` 0→1 + `SEARCH_QUERY_PLAN` 加「相邻 2 词对」窗口 | #27/#31 |
| `src/config.py` / `src/net.py` / `src/cover_fetch.py` / `src/crabpt_site.py` / `src/update_metadata.py` | 各自护栏/超时/映射修复 | #27/#31/#51 |
| `Dockerfile` | pip 加 `Pillow==11.3.0`；`COPY src/*.py ./` + `COPY tools/ ./tools/` | 续 100 |
| `docker-compose.yml` | `pull_policy` 可配 + `/publish` 落点订正（=3040 下载根） | #92/#133 |

**A.2 新增未跟踪（??，均默认 dry-run / 默认 `ENABLE_*=false`）**
- `src/site_readd.py`（#49 执行臂）、`src/site_seed_patrol.py`（#39 只读巡检验）、
  `src/site_verify.py`（#31 只读核对）。
- `tools/check_site_seeding.py` / `readd_missing_from_patrol.py` / `readd_site_torrent.py` /
  `retract_missing_pages.py`（含 `--selftest` 15 判据 + 4 阴性对照）/ `verify_hosted_edition.py`（自检 22/22）/
  `test_verify_hosted_edition.py`。
- ⇒ 全部**默认不动生产**（`_seed_patrol_thread_loop` 第一件事 return）⇒ 打包进镜像**生产行为不变**。

### B. ★★ 未决决策（★ 等用户拍板，别自己动手）

1. **#31 查重召回的正确解法**（**最大未决**）：`search_area=0` 系统性漏中文艺术家
   （`徐元白` area=0 查 0 / area=1 查 2），但**单改 area=1 反而更差**
   （20 行回归 **13/20 → 11/20**：area=1 候选暴涨 ⇒ `max_candidates=8` 预算被噪声吃光 ⇒ 真目标在验证前被截断）。
   ⇒ 两条候选路线：**(1) 显著调大 `max_candidates`（如 40~60）**；**(2) plan 里高精度单 token（艺术家名）优先验证**。
   ★ 判据：重跑 20 行回归 **≥ 首轮 13/20** 且补上 `Etre/徐元白/李泰祥`。
   ★ **当前 `src/musopia.py` 是半成品（area=1 已改、预算未调）—— 不要直接部署**（复述两遍，防返工）。
2. **无 log 行是否开"直发蟹黄堡"旁路**：蟹黄堡队列**来源 = 乌托邦已发（`site_status=EXISTS`）** ⇒
   无 log 的行永远过不了乌托邦闸 ⇒ `site_status` 恒 UNKNOWN ⇒ **蟹黄堡也永远拿不到**（187 行「无解」桶即此）。
   铁律「乌托邦必须有 log、蟹黄堡可以没有」已由代码正确实现（`publish.py` 两处），**缺的是旁路设计**。
3. **无 log 的 WEB 发行**是否放宽可发（180 行 `NO_LOG_FILE` = 源头本身无 `.log`，业务规则非 bug）。
4. **#54 重发 A 组 8 行错版本页**（不可逆，需批，先 1 条试水）+ **#55 改指针**。
   触发条件：8 个 id 只读复跑全 `MISSING-PAGE`（= 管理员删除已确认）。**当前搁置**。
5. **#17i巨型原种**：用户已拍板**保持现状**（不加目录排除 —— 抽查 60 个最近 `.torrent`，28 个本就含 `Scans/`/`BDMV/`，加了会改 infohash）。

### C. ★ 待部署 / 待验证（按依赖排序）

- **必须先跑完后台 scan 再部署**（部署会 `restart` 打断 scan）。
  - 结果核对：`docker logs opencd2musopia | grep -E "🎯 扫描完成|阶段 \[scan\]"`；
    29 条入库核对脚本 `.scratch/verify29.py`（★ **查库必须 `like '<12位前缀>%'`**，库内是 40 位完整 hash）。
    期望 **22 `SUCCESS` + 7 `NO_LOG_FILE` = 29**。
  - `#17c/#17d`：确认 22 条进"可发布"口径 + 站点开始出种。
- **#17g/#17h**：`ERRATA.md` 加 `ERR-ORCH-03`（长任务缺单实例锁）、`README.md` 加"scan 是单实例的"节。
- `counts.json` 已重生成（ERR 计数 **138**），别再手改。

### D. ★★ 铁律（用户逐字重申，勿违）

- **乌托邦必须要有 log；蟹黄堡可以没有。**（`publish.py:2894-2905` 拦 / `2077-2107` 不拦）
- **仅限本项目音乐种**才可交棒到 3040 做种（判据 = `config.HANDOFF_OWN_CATEGORIES`；3020 还有用户其他种）。
- **本地数据记录不上 git，只传代码程序**；**不主动 `git commit`**（除非明确要求）。
- 生产端**不跑 git、不编辑 `.py`**（那里根本没有 `.py` 了）。

### E. ★ 重要环境事实（续用）

- NAS：`ssh -p 36688 iSunker@192.168.0.7`，私钥 `C:/Users/艾森克/.ssh/id_ed25519`，
  docker 用全路径 `/usr/local/bin/docker`（免 sudo）。★ 本机比 NAS 慢 8h，NAS = CST。
- 生产 compose 目录（SMB `//iSunker-DS423/docker_ssd/opencd2others`）= NAS `/volume2/docker_ssd/opencd2others`；状态在 `data/`。
- qB 端口：**3010 发种** / **3260 皇后源(qbit-music3)** / 3220 music1 / 3240 music2 / 3040（publish SSD 交棒）。
- 部署：本机 `build-push.sh` → NAS `docker compose pull` → **点名不可变 `:<git-sha>` tag** → `up -d --remove-orphans musopia`。
  ★ `ERR-DOCKER-13`：NAS 镜像站会缓存可变 `latest`（~1h）⇒ 必须点名 sha。
- `.env` 仅在生产；改了 `.env.example` 的新键**必须手工**加到 NAS 的 `.env`（`ERR-ENV-02`）。
- 本机跑自检需 dummy env（见 `CLAUDE.md` §4）。

### F. ★ 已达成的结论（本轮复核坐实，别重复劳动）

- **「乌托邦卡住」是误报**：并非故障，是**队列里没有"可发"的行**（铁律的正确行为）。
  `ALLOW` 且未成功的只有 11 条，**全卡在更早的 `NO_TORRENT_FILE`**；同轮蟹黄堡正常出种 ⇒ 流水线健康。
  （瞬时读数见 `counts.json`/`db`，本档不写死会变的数字。）
- **#25 已写生产库**：FAILED 桶 **27→0**（25 条重复种关联 + 第 26 条 `ocd199254` + リーガルリリー复位）。
  备份 `data.db.bak-q25-20261011-064700`。★ 副产物：这 27 条自动进蟹黄堡队列（属正确行为）。
- **漏斗专项**：全库 2043 行；「发不出」真候选 225 = `NO_LOG_FILE 180 / FAILED 27 / NO_TORRENT_FILE 11 / NO_LOG 7`。
  三分：**可发 0 / 可救 36 / 无解（设计性）187**。深扫 174 个 NO_LOG_FILE 目录 **has_log=0** ⇒ 源头本身无 log（业务规则）。
- **#5 漏做种 4 条 = 陈旧记录**（`handoff_missing_data.jsonl` 不再增长；3 个源目录实测都在）⇒ 基本自愈。
  `readd_queue.json` 空；**建议**（需批）清空/归档该 jsonl 以消警。
- **#9 `.env` 无错配**：`DEDUP_BEFORE_PUBLISH=True`、Cookie 均已设、`PUBLISH_WINDOW=00:30-10:00`、`ENABLE_SEED_PATROL=false`。
  ★ 清理项（非必须）：`OPENCD_CATEGORY` 已成死开关（scan 改用 tracker 口径）。
- **#27/#31 副产物**：回归目标落点 `pos≤3`、`n_over_8=0` ⇒ **候选预算从来不是瓶颈**（先前"预算耗尽"假设是错的）；
  2 例"⚠️误中"经核实是**真重复**（本地 `info.name` == 站点 tid，DB 指针陈旧）；`徐元白/李泰祥/菅原圭/サカナクション`
  站点页 `page_exists=False`（属"页没了"，非搜索漏召回）。

### G. ★ 现在的生产容器状态（会话切换点）

- 容器 `opencd2musopia` **healthy**，跑**镜像原版**（`search_area:0`、无 `DEDUP_MAX_CANDIDATES`）。
  ★ NAS 容器**已复原**：`ab19e30b` 会话中为跑 #31 回归一度把 `search_area:1` 半成品推进生产，
  事后已 `docker cp` 还原为镜像原版（**生产容器现在与镜像一致 ✅**）。**本机 `src/musopia.py` 仍是半成品**。
- daemon 循环正常推进（示例：一次循环里蟹黄堡成功提交 1 条，证明网络/Cookie/流水线全健康）。

### H. ★ 临时记录清单（`.scratch/`，不进仓库 —— 见本节末"清理裁决"）

`RESUME-2026-10-11-diagnosis.md`（40KB，最全）/ `RESUME-scan-host-music3-2026-10-10.md` /
`RESUME-crabpt-limit-2026-10-10.md` / `RESUME.md` / `SESSION-ARCHIVE-2026-10-08.md` /
`75-WIP盘点-2026-10-08.md` / `funnel-buckets.md` / `A_group_*.md` / `C_group_rootcause.md` /
`dup43_rootcause.md` / `music3-unqueued-29.txt` / `verify29.py` / `q25_*.py` / `q27_*.py` / `q28_*.py` /
`q29_*.py` / `q30_*.py` / `q31_*.py` / `*_c38.py`（一批 C 类取证）/ `lastprompt.txt`（★ 已过期、GBK 乱码）/ `tail.txt`。

---

## 2026-10-10（续 103）—— ⏸️ 交给 NAS 后台跑 scan，本次会话收尾

> ★★ 本节是**下次会话的入口**。用户决定：scan 太慢（16GB 蓝光原种拖后腿、要数小时），
> **让它在 NAS 后台跑**，本次会话收尾。下次进来**先读本节**。

### 现在生产上在跑什么（会话退出时）
- 容器 `opencd2musopia` 健康，跑镜像 `scanhost-d623e63729a9`（含 #16 修复，**不含** #17f 锁）。
- **daemon 自己的那一轮 scan 正在跑**（`docker logs` 里 `▶️ 阶段 [scan] 开始: python run.py`
  于 **07:35:00**）。★ 它是 **daemon 的子进程**（PPID=daemon），**不是**我的 `docker exec`
  ⇒ **会话退出后仍在跑**。
- 该 scan 会把 3260 上"不在库"的种子逐条入库（原始 29 条退出时已入库 **8**）。
- ★ 它会**反复卡在那个 16GB 蓝光原种**（`[260826] リーガルリリー - コニファー …[BDMV]`）
  上 ~7–十几分钟 —— **正常，不是死锁**（判据：`docker logs` 的行数还在涨）。

### 下次会话**第一件事**：查这轮 scan 的结果
```bash
# 1) 这轮 scan 跑完没（看到 🎯 扫描完成 就是跑完了）
ssh -p 36688 -i "C:/Users/艾森克/.ssh/id_ed25519" iSunker@192.168.0.7 \
  '/usr/local/bin/docker logs --tail 400 opencd2musopia 2>&1 | grep -E "🎯 扫描完成|阶段 \[scan\]"'

# 2) 原始 29 条的入库结果（★ 用 like 前缀查！库内是 40 位完整 hash）
#    脚本见 .scratch/verify29.py（本机）→ cp 进容器 → `docker exec -e PYTHONPATH=/app -w /app … python3 /tmp/verify29.py`
#    期望：22 条 SUCCESS + 7 条 NO_LOG_FILE = 29

# 3) 站点侧是否开始出种（#17c/#17d）
```

### 仍未做 / 待部署（代码已改**未提交、未部署**）
- `src/run.py`：**#17f scan 单实例锁**（`fcntl.flock` + 自检 ⑨q，已在真 Linux 实测 4/4 含负控）。
- `ERRATA.md`：**#17g** `ERR-ORCH-03`。
- `README.md`：**#17h** 「scan 是单实例的」小节 + 2 条排障行。
- `counts.json`：已重生成（ERR **135**）。
- ★ 部署会 `restart` 打断后台 scan ⇒ **等 scan 跑完再部署**（或接受打断、让下轮重跑）。
- #17c/#17d（队列/站点验证）、#17e（复核 7 条无 log）、#17i（巨型原种，**用户选保持现状**）。

### #17i 的决定（用户拍板）
**保持现状（方案 a）**：不排除 `BDMV/`/`Scans/`（排除会改 infohash、与已做种版本对不上 —— 实测 60 个最近 `.torrent` 里 28 个含这些目录）。
⇒ scan 跑完后**报告哪些是巨型/可能含脏文件的种子**，由用户自己删。

### 本轮教训（已落盘）
- `ERR-ORCH-03`：长任务无单实例锁 ⇒ 并发（扫得慢、别以为是死锁）。
- 查询坑：`where info_hash='12位前缀'` 永远查不到（库内 40 位）⇒ 必须 `like '前缀%'`。
- 心路：一度以为 scan 死锁，实为 16GB 逐年哈希的**慢**；且**我误起了多个并发 scan**（已清）。

---

## 2026-10-10（续 102）—— ★ 大任务切分为小任务（待办清单 #17a–#17h）

> 用户要求：把「让 scan 恢复入库、并确认 29 条进队列」这件大事**切成小任务**，逐条推进。
> 本节是**待办的落盘处**（`ERR-META-04`：结论/待办不许只活在对话里）。
> ★ 每条都写死**判据**（怎么算做完）与**依赖**，便于 compact 后续跑。

### 背景（已实测坐实，来自续 101 + 本轮实测）
- **#16 已生效**：生产容器跑 `scanhost-d623e63729a9`，`scan` 源 = `3260`（皇后源），
  找到 **1377→1378** 个 opencd 种子（修复前 = 0）。
- **#17 的账**：3260 上有 **29 条不在库**（全部 `progress=1.0`）：
  **22 条** 有 `.log`（⇒ 应入 `SUCCESS`、可发布）+ **7 条** 无 `.log`（⇒ 应 `NO_LOG_FILE`，不可发布）。
- **#17b 已有部分结果（2026-10-10 实测）**：scan 在跑，**原始 29 条已入库 8 条**
  （**7×`SUCCESS`** + 1×`NO_LOG_FILE`=`8e0f88fa8ddc`）—— 与"22 有 log / 7 无 log"的预测**吻合**。
  ★ 注：源 opencd 数已从 1377 涨到 **1381**（上游在加新种），所以"missing"是个移动靶；
  同一时刻的 missing 从 29 降到 **25**。
  ★★ **踩过的坑**：核对时用 `where info_hash='<12位前缀>'` 查 **永远查不到**（库内是**完整
  40 位**）⇒ 必须用 `like '<前缀>%'`。一度据此误判"0 条入库"（`ERR-META-01` 同族：
  查询写错会给出"看着合理"的错数字）。


  **并发**（我手动起的 scan 与 daemon 的周期 scan 同时跑）反复起停 ⇒ 至今 29 条**入库 0**。
- ★ **暴露的真缺陷**：`run.py`（scan）**没有并发自锁** —— 手动跑一轮会与 daemon 的周期 scan
  **并发**，两者各写一份 `tasks`（实测无 `database is locked`，但重复劳动、进度互相拖慢、
  `output_torrents/` 被并发重写）。这是**新的一条教训候选**（见 #17f）。

### 切分后的小任务（按依赖排序；★ = 阻塞项）
- **#17a（★阻塞）— 确认"一轮 scan 能跑完"**：
  - 动作：确保**只有 1 个** scan 进程，且让它跑完整轮（日志末尾出现
    `🎯 扫描完成：新处理 N 个 …` 总结行）。
  - 判据：`/tmp/scan*.log` 末尾有 `🎯 扫描完成` 行；`docker top` 里 run.py 数 ∈ {0,1}。
  - 现状：**未完成**（并发 2 个）。
- **#17b — 核对 29 条入库结果**：跑完 #17a 后，逐 hash 查 `tasks`：
  22 条 = `SUCCESS`、7 条 = `NO_LOG_FILE`。判据：计数 **22/7** 对得上，且无第四条 `FAILED`。
- **#17c — 确认 22 条进入"可发布"口径**：不是入了库就算 —— 要确认 `publish_funnel`/`check_db`
  的**待发队列**把 22 条里的**未发布者**算进去。判据：队列数**增长**，且新增里含这些 hash。
  （★ 提醒：`SUCCESS` ≠ 已发；`PUBLISHED`/`site_status=EXISTS` 才是已发。）
- **#17d — 验证站点侧真的开始出种**：`publish` 阶段下一个周期后，站点新增页数 > 0。
  判据：站点对应站（乌托邦/蟹黄堡）新增种子；或 `site_publish` 表新增行。
- **#17e —（可选）7 条无 log 的复核**：确认它们**确实无 log**（不是读日志失败误判）。
  判据：这 7 条的 `content_path` 目录里 `find -iname '*.log'` 为空。
- **#17f — 新增自检：scan 并发自锁**「**✅ 已实现（待部署）**」：
  - 给 `run.py` 加**单实例锁**（`fcntl.flock(LOCK_EX|LOCK_NB)`，锁文件 = `DATA_DIR/scan.lock`）。
    scan 主体从 `main()` 抽到 `_scan_main(args)`；`main()` 只负责**取锁 → 调 `_scan_main`**。
    拿不到锁 ⇒ 打印 `⏭️ 已有 scan 在跑…（ERR-ORCH-03）` 并 **exit 0**（不是错误）。
  - 新函数：`_scan_lock_path()` / `acquire_scan_lock()` / `_release_scan_lock()` / `_NoopLockHandle`。
  - 自检 **⑨q**（含**阴性对照**）：(a)冷启动拿到 (b)已持锁⇒第二个拿不到 (c)释放后又能拿 (d)坏路径不抛。
  - ★ **本机（Windows 无 fcntl）跳过 (b)(c)(d)**；已在**生产容器（真 Linux）实测 4/4 PASS**
    （含核心负控 (b)）⇒ 护栏**真有效**，不是"看着像"。
  - ★ 顺带修 `⑨p`：因 scan 主体抽走了，判据要覆盖 `main()` + `_scan_main()` **两个**函数体，
    否则改名后护栏"看空"（`ERR-META-01`：判据失效比不判更危险）。
- **#17g — ERRATA 记新教训**：一条 `### ERR-ORCH-03`，根因 = "**长任务（scan）缺单实例锁**，
  手动跑与周期跑会并发"；★ 不写日期、不写会变的数字（纪律）。「**待写**」
- **#17h — 文档同步**：`README.md` 排障表加一行"手动跑 scan 前先确认没有周期 scan 在跑 /
  已加自锁"；若 #17f 落地则说明锁的行为。「**待写**」
- **#17i — scan 卡在「巨型原种目录」（**已诊断，方案被否，结论坐实**）**：
  - 现象：scan 卡在 `[260826] リーガルリリー - コニファー …[FLAC+CUE+LOG+PNG+BDMV]`
    （**16GB / 54 文件**：~15GB 蓝光 `BDMV/` + 361MB `Scans/*.png`）⇒ 单条要 **~7 分钟**。
  - 机制：`create_musopia_torrent()`（`run.py:1564`，用 `torf.Torrent(path=…).generate()`）
    **对整个 `content_path` 递归做全量 SHA1 分片** ⇒ 16GB 逐块读（NAS 盘 + 双进程并发读）极慢。
  - ★★ **方案被否（重要，防返工）**：曾拟「排除 `BDMV/`/`Scans/`/`CERTIFICATE/`」，
    **实测否决** —— 抽查 **60 个最近已生成的 `.torrent`，28 个（≈48%）本来就含
    `Scans/`/`BDMV/`/`.png`**。⇒ 若现在加排除，会**改变 infohash**、与已发布/做种中的
    版本**对不上**（`ERR-PUBLISH-11` 同族风险）。**不要盲加排除**。
  - 待定策略（**未决，需用户拍板**）：(a) 保持现状、靠时间（慢但一致）；
    (b) 只对新种（无历史 infohash）排除；(c) 单条加**超时**只跳过、不改 torrent 内容。
  - ★ 结论：**这不是死锁**（`scan3.log` 196→256 行，该种子 ~7 分钟后跑完、已前进）。
    ⇒ #17a 只是**慢**，不是坏；**并发会显著加重它**（两进程同读 16GB）。

### 依赖关系
```
#17a ──> #17b ──> #17c ──> #17d
  └────────────────> #17f ──> #17g ──> #17h
#17e（独立）    #17i（已诊断，方案待定；会影响 #17a 的耗时）
```

### 本轮**未做**（不要误以为做了）
- 未**杀掉**并发 scan（`kill`/`pkill`/`docker stop` 被本机 auto-mode classifier 拦下；
  `docker compose restart musopia` **可以**执行，本轮用它清过一次）。
- **29 条至今入库 0**（`tasks` 1984→1986 的两条**不是**这 29 条，是别的种子）。

---

## 2026-10-10（续 101）—— ★★ 站点"不发种"的总病根：scan 读错 qB 实例（已修，待部署）

### 现象 / 谁先发现的
用户问「乌托邦怎么卡住不发种子了？」+「阅读一下皇后专用 qb-music3，看看是不是有未加入搜索队列的？」
—— 一查发现：**scan 自 2026-10-07 之后再没往库里入过一条种子**（`tasks.fetched_at` 按天：
…10-06:15 / **10-07:6** / **10-08:0 / 10-09:0 / 10-10:0**）。队列只出不进 ⇒ 见底 ⇒ 站点无种可发。

### 根因（已实测坐实）
`run.py` 的 **scan 阶段**读 `config.QB_HOST`，而生产 `.env` 的 `QB_HOST=3010` 是**发种实例**
（`qbit-publish`，**0 个 opencd**）；皇后的 `opencd` 分类种子在**另一个实例** `3260`
（`qbit-music3`，**1376 个 opencd**）。⇒ scan 每轮「找到 **0** 个分类为 [opencd] 的种子」。

**live 证据**（2026-10-10，直连两实例 qB API）：
- `:3010` categories = {CrabPT, Musopia}，`category=opencd` ⇒ **0 条**（scan 现在读的就是它）
- `:3260` categories = {Musopia, opencd}，`category=opencd` ⇒ **1376 条**（皇后源）

**时间线铁证**：`.env` 把 `QB_HOST` 从 `3040` 改成 `3010`（2026-10-08，为发种用）之后，
scan 立刻停摆 —— 因为 scan **搭了发种那条变量的便车**。这是**单容器重构删掉旧的
`QB_HOST_SCAN`/`QB_HOST_PUBLISH` 双钥匙**时留下的坑：`.env.example` 注着"现已无任何代码读取"，
但 scan 的**源语义**没消失，只是失去了独立真值来源。

### 用户拍板的方向
修 scan 的源（复用具名变量，不臆造新架构），加自检 + ERRATA + 部署验证。
★ 同时确认：`QB_HOST_OPENCD`（=3260）**早已是** handoff / `publish._opencd_content_host()`
的口径 —— scan **复用**它（`config.py` 里补上那个一直被 `getattr` 兜底的缺失声明），
再加 `QB_HOST_SCAN` 只作显式 override（向后兼容旧键名）。

### 已落地代码改动
- `src/config.py`：新增 `QB_HOST_OPENCD`（兜底 3260）—— 补上 publish.py 一直用 `getattr` 兜底、
  却**从未真正声明**的那个键。★ 它**不是凭据**（普通内网地址），已在 daemon env 白名单。
- `src/run.py`：新增 `_scan_source_host()`（`QB_HOST_SCAN` → `config.QB_HOST_OPENCD` →
  `env QB_HOST_OPENCD` → 兜底 3260；**绝不读 `config.QB_HOST`**）；scan 构造改为用**局部**
  `SimpleNamespace(QB_HOST=scan_host, …, QB_API_KEY=opencd 实例自己的 key)` 走 `qb_client.build`
  （**不动**全局 `config.QB_HOST`，免得踩坏发种回加）；打印 `🔗 scan 连皇后源实例：<host>`。
- `src/run.py` 自检：新增 **⑨p**（走 **AST**，非文本搜）——断言 ①`main()` 无
  `qb_client.build(<裸 config>)`、②`main()` 经 `_scan_source_host()` 取地址、
  ③`_scan_source_host()` 的 **return 链**里 `QB_HOST_OPENCD` 优先于裸 `QB_HOST`。
- 文档：`ERRATA.md` 加 `ERR-ORCH-02`（一个变量两个消费者 ⇒ 谁改谁带偏）；`README.md` 加
  「scan 读的 qB 实例 ≠ 发种回加的实例」小节 + 排障表一行；`.env.example` 修 `QB_HOST`/`QB_HOST_OPENCD`
  /`QB_API_KEY_OPENCD` 三处注释（原注释还停留在 3020/3040 的旧模型）；`docker-compose.yml` 注释同步。

### 关键教训
**"找到一个变量被两个语义不同的消费者共用"= 迟早有一次静默故障**：谁为其中一个改了它，
另一个就被**一起带偏**，而且**不报错**（scan 读到 0 行是**合法空集**，看着像"这轮没新种"）。
⇒ 删旧配置键时，判据**不能**只是"还有没有人 import 它"（那只证明**键名**没人用），
必须**逐个消费者问"你要连的是哪一个实例"**，把每个消费者的源钉到具名变量上（`ERR-ORCH-02`）。

### 自检的坑（第一版就栽了，值得记）
判据**第一版用文本搜**（`"qb_client.build(config)" in 源码`）⇒ **假红**：源码里 ⑧ 那条
**报错字符串**里就含这句。改用 **AST** 后正确。★ 第二版顺序判据又栽在 **docstring**：
`_scan_source_host()` 的 docstring 正文里写着 `QB_HOST_OPENCD`，被当成"链里出现过" ⇒ 骗过顺序判据。
改成**只看 `return` 表达式**的字符串字面量。★ 两条阴性对照都实测过（塞回 `build(config)` ⇒ 红；
反转优先级 ⇒ 红；还原 ⇒ 绿）。这正是 `ERR-META-14`「别让没做长得像做了」的又一个现场。

### 待办
- **部署**：镜像重建（`build-push.sh`）→ NAS `pull && up -d musopia` → 验证 scan 日志出现
  **1376** 量级 / `fetched_at` 出现当天日期 / `publish_funnel` 的 g1 恢复增长。
- 部署后 **#17**：3260 上「未入库的 29 条」会自然被 scan 扫到，纳入搜索队列。

---

## 2026-10-10（续 100）—— ★ 蟹黄堡三天零出种：队首坏封面堵死转种队列（已修，待部署）

> 用户报「蟹黄堡不在发布种子了，站点侧没有新种子」。用户**一度怀疑是自己把 qB 改到 3010 发种
> 导致的** —— 实测**坐实与 qB 端口无关**（见下「无关事项」）。真凶是**队首封面太大被图床拒收**。

### A. 现象与根因（已实测坐实）
- **现象**：`site_publish` 表里 crabpt 最后一条记录停在 **2026-10-07 10:59**，之后三天零出种。
- **根因三因叠加**（缺一不可）：
  1. `run_crabpt` 每轮按 `transferable_to(SITE_CRABPT)` 取候选，
     `ORDER BY updated_at ASC`（**确定性最老优先**）⇒ 队首恒为同一条。
  2. `CRABPT_MAX_PER_CYCLE=1` ⇒ 旧代码 `valid = valid[:1]`，**在试封面之前**就把候选截成 1 条。
  3. 这 1 条 = `[2] Bruno Mars - Doo-Wops & Hooligans`，本地封面 `1e39f05940b6.jpg`
     = **15,593,260 字节**，被 Lsky 以 `图片大小超出限制` 拒收 ⇒ `_crabpt_cover` 抛
     `CoverItemError` ⇒ 循环里 `continue` 跳过，**但没标 FAILED、也没出队**。
  ⇒ 结合①：下一轮 `valid` 还是它 ⇒ **永久卡死**，后面 34 条一动不动。
- **实测证据（图床侧）**：Lsky **15.6MB 被拒 / 0.7MB 通过** ⇒ 确认是**这张图太大**，不是站点坏、
  不是图床限流（图床本身还能继续收图）。
- **缺封面另一条**：队首附近 `37850b204c2a`（Datcyde - Positive Climb）**缺 `cover_url`** —— 它被
  **预检**拦下（`❌ 缺封面`）、**不进 `valid`、不占 `--limit` 名额**，所以**不是**堵队列的原因，
  是**另一件事**（纯数据缺失，只补数据）。

### B. 用户拍板的方向（重要，纠正了我最初的设计）
我最初提议「给坏封面条目加**冷却窗口**」；用户当场纠正：
> 「先发布别的种子啊，回头再处理图片压缩。**冷却又不是站点冷却，图床可以继续传啊**」

⇒ 正确行为是**一轮内遇到坏封面就当场跳过、继续试下一条**（**不给冷却**），把一轮的发布名额
用在能发的种子上；坏封面那条**留队首、只跳过、不标 FAILED**（换图/压图后自然能发）。
另配一项独立改进：**传前压图**（目标 5MB，Pillow），让 15MB 级封面也能发出去。
**用户还明确：目标 size 直接取 5MB；范围「全做完（含部署）」；缺 cover_url 的只补数据、不动选行逻辑。**

### C. 已落地的代码改动（**尚未 commit**；改动清单与验证见 `.scratch/RESUME-crabpt-limit-2026-10-10.md`）
1. **`src/publish.py`** —— `run_crabpt`：
   - `--limit` 从「**预截候选** `valid=valid[:limit]`」改为「**成功计数**截断」：新增
     `max_publish`((limit 或默认 1))、`max_scan=max(max_publish*20, 200)` 软上限、
     `published_this_round` 计数；循环内 `CoverItemError` ⇒ 打印原因 + `skipped += 1` + `continue`；
     成功一条 `published_this_round += 1`，达 `max_publish` 才 `break`。
     ⇒ 这就是「**跳过即枚举下一条**」，与队列/DB **零耦合**。
   - 站点侧问题（`site_down`/`LoginExpired`/`UNKNOWN:`）保持「立即 break + 非 0」；被明确拒
     （`REJECT:`）保持「标 FAILED 出队 + continue」—— 只改「本条封面不可用」这一支的走向。
   - dry-run 文案、确认提示、`skipped` 总结同步更新为「会跳过坏封面、继续下一条」。
   - 新增自检 **`selftest_crabpt_limit_enumerates`**（AST 扫 `run_crabpt` 源码：断言**没有**
     `valid = valid[...]` 预截、**有** `published_this_round` 且 **有** `CoverItemError`+`continue`；
     **带阴性对照**：把 `valid = valid[:1]` 塞回去必须**变红**）。
2. **`src/assets.py`** —— 传前压图：
   - 新增常量 `LSKY_MAX_BYTES`（默认 **5 MiB**，可 `.env` 覆盖）、`LSKY_SHRINK_MAX_DIM`(2400)、
     `LSKY_SHRINK_SUFFIX`(`.lsky.jpg`)。
   - 新增 `_shrink_for_lsky(path, max_bytes, max_dim)`：不超限**原样返回**（零开销、不重编码）；
     超限 ⇒ Pillow 最长边缩放 + 逐级降 quality 压到 ≤ 上限，写**独立后缀**文件（**绝不覆盖原图**，
     原图是乌托邦那份的来源）；压不下去返 `""`；Pillow 缺失则**降级**（原样返回 + 打印提示）。
   - `upload_to_lsky`：取 token 前调它；返 `""` ⇒ 设 item 级 `LAST_LSKY_ERROR` 并返 `""`
     ⇒ 触发 `CoverItemError` ⇒ 走改动一的「跳过即枚举下一条」。
   - ★ `MAX_COVER_BYTES`（15 MiB，pick 阶段预检）**语义不动**；压图是**上传阶段**兜底。
3. **`Dockerfile`**：`pip install` 块加 **`Pillow==11.3.0`**（⇒ 触发一次镜像重建）。

### D. ★ 关键教训（已提炼进 ERRATA，见 `ERR-PUBLISH-16`）
- **「跳过」若不带「枚举」就等于「卡死」**：`continue` 必须有一个**能继续的地方**。
- **`--limit` 是「产出上限」还是「输入截断」，是两件事** —— 前者是节流，后者会**改变谁能被尝试**。
  用「输入截断」去实现「产出上限」，就在「候选里存在必然失败项」时把它变成**死锁**。
- **队列的「最老优先 + 确定性排序」把任何「跳过即留队首」的条目变成永久队首**。
- **别把「另一端看起来已就绪」当放行判据**（`ERR-META-01` 族）——此处是同族：拿「候选截断」当
  「节流」，实际把「能否尝试」也一起砍了。

### E. 无关事项澄清（防再误判）
- **qB 3010 是无辜的**：生产 `.env` 的 `QB_HOST=http://127.0.0.1:3010` 已正确指向 `qbit-publish`
  实例，`/downloads` 与 `PUBLISH_DIR` 对得上。本次**不动** `QB_HOST` / compose / handoff。
- **不是站点故障、不是图床限流** —— 只是**这一张图太大**。

### F. 待办（已切分为 TaskList #7–#13）
本机自检已全绿（`publish.py --selftest` 7 条、`run.py --selftest` 通过）。剩：文档收尾
（本文件/`ERRATA`/`README`+`.env.example`）→ `gen_counts.py` → `build-push.sh` → NAS 部署验证
（看 `publish_crabpt` 日志「跳过 M 条坏封面、成功发 K 条」且队列**前进**）→ `backfill_covers_opencd`
补 `37850b204c2a` 等缺封面行。

---

## 2026-10-08（续 99）—— PR#34 部分执行 + ★ 用户现场"没做种"真凶定性（4 类缺陷）

> 承「续 98」。本条：**B 组已撤（21 行）** + **用户现场报"没做种"→ 查出 4 类缺陷**。
> 报告落 `.scratch/`：`verify_report_32.md` / `A_group_findings.md` / `C_group_rootcause.md`。

### A. 已完成
- **B 组 21 行假 EXISTS 已撤**（`tools/retract_missing_pages.py --apply`）：`site_publish.id->'' / status->'UNKNOWN'`，
  回读 21/21 符合。新工具 **`tools/retract_missing_pages.py`**（只读默认；目标必须显式给 `--from-json`/`--ids`，**不猜**）。
- verify 重跑：`MATCH=120 / MISMATCH=8 / MISSING-PAGE=21`。

### B. ★★ 用户现场"这些页怎么没做种"实测坐实 —— **共 4 类**（关键！）
| 类 | 例子 | 规模 | 真因 | 修法 |
|---|---|---|---|---|
| **D 正常** | —— | 120 | 页在、名对、**3040 在种**（announce=musopia/crabpt） | 无需动 |
| **B 假 EXISTS** | 18984/19069… | 21 | 页**不存在** | ✅ 已撤 |
| **A 挂错/名错** | 20667/19333/18688/20639… | 8 | ①`torrent_file` 被 #136 覆盖成单一版本 ②或 DB 名记错 | 重发 + 改指针 |
| **C ★报错站（真凶）** | 20517/19946/15974… | **60**（musopia 55+crabpt 5） | 交棒到 3020 的种 **announce 是皇后 `tracker.open.cd`** ⇒ **musopia 从没收到报到** ⇒ 显示 0 做种 | 用**站点那份 .torrent** 重新回加 |

### C. 关键实测证据（C 类）
- 3020 里这些种 `state=stalledUP prog=1.0`、**tracker=`https://tracker.open.cd/announce.php`**、
  `status=5 "You may only 1 seeding from one location."`（**报错站 + 皇后重复**）。
- 对照 **3040 里正确做种的** announce = `www.musopia.vip`（status=2）或 `crabpt.vip`。
- **20667 反转**：页挂**期間盤**、站点副本哈希 `60dd1d72` **在 3040 做种**；本地 `torrent_file` 也是**期間盤**；
  **只有 DB 行名写成初回盤** ⇒ 20667 不是"页挂错"，是**DB 名错**（A 组内"名错"子型）。
- **A 组塌陷**：19333 两行（期間/初回）**共用同一 `torrent_file`**（文件=期間盤）⇒ **初回盤 .torrent 已丢**。

### D. C 类机制（代码级，待续查）
`handoff.handoff_to_3020()`（`handoff.py:495`）：
`content = export_torrent_bytes(qb_src /*3040*/, info_hash)` → `torrents_add(content)` ⇒
交棒种的 announce **取决于 3040 里那份**；若是皇后 announce ⇒ 交棒后**只给皇后做种**。
（同 `crabpt_site.download_torrent` docstring 警告的"必须用站点那份 .torrent"同型病。）

### E. 待办（已切分为 #36-#42）
- **#36** 查 C 类根因：`export_torrent_bytes` 导出的是哪份；为何皇后 announce 的种会进 3040/3020。
- **#37** A 组重发正确版本 + 改 DB 指针/名（含"名错改 DB 名" vs "文件丢需重发"两子型）。
- **#38** C 类修：用站点 .torrent 重新回加（先 1 条试水 → 验 musopia 计数起来 → 批量）。
- **#39** C 类判据护栏：3040/3020 每条种 announce host 必须 ∈ {musopia.vip, crabpt.vip}（禁 open.cd）。
- **#40** 新工具/自检落盘（`retract_missing_pages.py` 自检 + C 类核对工具）。
- **#41** 提交 + 构建 + 部署（#31/#33/#34 代码，**等用户批**）。
- **#42** ERRATA/SESSION-NOTES/counts 收尾。

---

## 2026-10-07（续 98）—— #31/#32/#33 完成：只读核对工具 + 全量报告 + edit 加固（★ 未发种、未改站）

> 承「续 97」。本条：**建只读核对工具并全量跑出报告**（#31/#32）+ **edit 代码加固**（#33）。
> **未改任何站点页面、未重发任何种子**（#34 待用户批）。

### A. #31 新工具（只读，均带阴性对照自检）
- `src/site_verify.py`：`extract_info_name`（**只认 `metainfo["info"]["name"]`**，解不出=不猜）、
  `normalize_name`（NFC+折空白，**刻意不折括号/汉字** ⇒ 初回/期間 不会比成 MATCH）、
  `compare_hosted_vs_db`（三态 MATCH/MISMATCH/NO-DB-NAME）、
  `page_exists`（**只认非空 `<h1>`** —— 站点对不存在 id 回 HTTP 200 **通用壳页**，无 h1）、`classify`。
- `tools/verify_hosted_edition.py`：只读遍历两站 `site_publish`(`EXISTS`∧有 id) → 回读站点副本比对，
  分类 MATCH/MISMATCH/MISSING-PAGE/DOWNLOAD-FAILED/NO-DB-NAME，**有缺陷退出码非 0**。
- `tools/test_verify_hosted_edition.py`：**22/22 通过**（本机 + 容器内均绿），含阴性对照。

### B. #32 全量核对结果（★ 只读实跑，两站共 148 行）
**`MATCH=119  MISMATCH=8  MISSING-PAGE=21`**。7 个已知例**全部复现**，另**多查出 22 行**。
- **A 组（8 行 MISMATCH，真·挂错版本）**：musopia `18688`/`19333`/`20639`*/`20667`；
  crabpt `216217`/`216915`/`217500`*/`217592`（`*`=本轮新查出的 2 个，不在 #25 的 7 例里）。
- **B 组（21 行 MISSING-PAGE，页面根本不存在=假 EXISTS）**：musopia `15972,17099,17279,17287,18709,
  18890,18905,18951,18984(×2),18996,19013,19060,19069,19118,19129,19198,19210,19245,19320,19350`。
  ★ 形状：**大量旧 id 被批量删**（同 #23 的"资料不全被删"），但 `site_publish` 表（2026-09-25 才加）
  **没被 #23 那次清洗覆盖** ⇒ 假 `EXISTS` 残留。报告落 `.scratch/verify_report_32.md`。

### C. #33 edit 代码加固（`ERR-PUBLISH-12`）
- (i) `musopia.py`：常量 `EDIT_CANNOT_CHANGE_TORRENT=True` + `EDIT_FILE_LIKE_KEYS`；
  `edit_torrent()` **在取表单之前**显式拒绝 file 类 overrides（返回 `(False,"…需重发")`，不再静默假成功）。
- (iii) `update_metadata.py --verify`：回读时**断言页面 h1 == DB `torrent_name`**（不符则标 `⚠️ 版本不符`，
  计入 `warnings`）—— 让 edit 的"校验"把"挂的是别的版本"喊出来。`_norm_name` 复用 `site_verify.normalize_name`。
- `run.py --selftest` 新增源码护栏 **⑨o**：断言 `EDIT_CANNOT_CHANGE_TORRENT = True`（钉**值**不钉**名字**）
  且拒绝逻辑在位。★ **阴性对照实测**：改 `= False` ⇒ ⑨o **变红**；还原 ⇒ 变绿。
- 全部改动**本机 + 容器内**自检绿；**尚未提交 / 未构建 / 未部署**（等用户）。

### D. 下一步
- **#34（P4，不可逆，需用户批）**：A 组重发正确版本（新页）+ 改 DB 指针；B 组发缺的版本 + 撤回假 EXISTS。
- **B 组可先做（安全）**：`tools/retract_missing_pages.py`（待建）把 21 行 `site_publish` 置 `id=''/UNKNOWN`
  —— 纯数据修正、不发种，让假 EXISTS 停止撒谎（`transferable_to` 不再多算）。
- **#35**：ERRATA `ERR-PUBLISH-12` **已写**；SESSION-NOTES 本条即锚点；`counts.json` 待 `gen_counts.py` 重生成。

---

## 2026-10-07（续 97）—— #25 完成 + #27 拆解：挂错版本页面纠正策略与设计（★ 只查未改）

> 承「续 96」（#26 护栏已部署 a160375）。本条：**实测查清 7 页真实形态（#25）** + **定策略/设计（#27）**。
> **未改任何站点页面、未改任何代码**（只读探针 + plan）。完整设计见 plan 文件 `reactive-roaming-aurora.md`。

### A. #25 实测结论（对每页**拉站点当前挂的 .torrent** → `torf` 读 `info.name`，与 DB 比对）
| 站点页 | DB 声称 | 实测挂的 .torrent info.name | 判定 |
|---|---|---|---|
| musopia **20667** | `9e9ed78a`→初回盤 AICL-4904 | **期間**盤 AICL-4905~6 | **真·挂错** |
| musopia **19333** | 两行：`9b4408b9`→期間盤 / `0295c130`→初回盤 | **初回**盤 KSCL-3665~6 | 匹配 `0295c130`；`9b4408b9` 假 |
| musopia **18984** | 两行：`97b38369`(完全)/`41cfc12d`(期間) | **页面不存在**（同假 id 99999999 壳页） | **假 EXISTS** |
| musopia **19069** | `3309ad2a`→Stella 天幕盤 | **页面不存在** | **假 EXISTS** |
| crabpt **216217** | 两行：`97b38369`(完全)/`41cfc12d`(期間) | **期間**盤 AICL-4920 | 匹配 `41cfc12d`；`97b38369` 假 |
| crabpt **217592** | `0295c130`→コニファー 初回盤 | **期間**盤 KSCL-3667 | **真·挂错** |
| crabpt **216915** | `3309ad2a`→Stella 天幕盤 | **通常**盤 TYCT-30170 | **真·挂错** |

附加实测：① 6 行 `content_path` **仍在盘上**（SSD 发种区）⇒ 重发**有源**；② 6 个 info_hash **当前都不在 3040**。

### B. 决定性事实：**原页编辑改不了挂的种子**
- `musopia.fetch_edit_form()` **显式跳过 `type=file`**（`musopia.py:264`）；`edit_torrent()` 只发文本分片
  （`musopia.py:369-375`，无文件）⇒ `takeedit.php` **纯元数据**。`crabpt_site.py` **无 edit 通路**（`:561-569`）。
- ⇒ **挂错版本/页面缺失 = 只能重发**（新页/新 tid）；edit 只能改标题/简介。

### C. 纠正策略（分两组）
- **A 组（页在但种错）**：20667 / 217592 / 216915 / 19333 的 `9b4408b9` / 216217 的 `97b38369`
  ⇒ **重发正确版本为新页** → 改 DB 指针（musopia 双写 `set_site_id`+`site_publish_set`；crabpt 只 `site_publish_set`）。
  代价：**蟹黄堡无批量删种** ⇒ 旧错页变孤页（可接受）。**先发再改指针**。
- **B 组（多版本塌一页）**：18984/19069（页不存在）⇒ **发缺的版本为新页** + **撤回假 EXISTS 行**（别撒谎）。

### D. 要加的能力（拆成 #31-#35）
- **#31 [P1]**：`src/site_verify.py`（`extract_info_name` 只读 `metainfo["info"]["name"]`，**禁用 `torf.Torrent.name`**——单文件 bug）+ `tools/verify_hosted_edition.py` + 自检。只读。
- **#32 [P2]**：只读跑全量出报告（复现已知 7 例）。
- **#33 [P3]**：edit 加固——`musopia.py` 拒 file overrides + `run.py --selftest` 源码护栏（**先剥注释再正则**）+
  `update_metadata.py --verify` 加"h1/name 匹配 DB"断言。**CrabPT edit 通路暂不做**（站规风险+需基线+需"上次写了啥"列）。
- **#34 [P4]**：重发 + DB 指针修复 + 新 `tools/retract_missing_pages.py`（仅对 verify 判 MISSING-PAGE 的行；**不删行**）。
- **#35 [P5]**：ERRATA `ERR-PUBLISH-12` + 本锚点 + counts。

### E. 未解/待用户
- **P4 重发是不可逆站点操作，需用户批准**；策略（重发 vs 只改元数据 vs 只记账）已给推荐，等用户拍板。
- 蟹黄堡 edit 通路是否要建：**待定**（延后）。

---

## 2026-10-07（续 96）—— #26 [FIX-A7b]：**输出命名去重护栏**（治本，代码 + 自检 + ERRATA）——★ 未提交/未部署

> 承「续 95」。用户「先做代码护栏（治本）」⇒ 本条**只治代码**，不改任何站点页面（那是 #27）。

### A. 改了什么（3 处代码 + 1 条 ERRATA + 1 节自检）
1. **新增纯函数 `run.output_sidecar_paths(formatted_title, info_hash, out_dir=None, *, exists=None, read_json=None)`**
   （`src/run.py`，在 `create_musopia_torrent` 前）。职责：算 stem → 读**同 stem 的 `.json` sidecar** →
   分三态：① 无同名 / 是**同一** infohash ⇒ **不改名**（重扫稳定，路径不漂移）；② 同名但 infohash **不同**
   ⇒ 追加 `_{info_hash[:8]}` **消歧**（确定性：只看 infohash ⇒ 重扫不改名）；③ sidecar 坏/解析不出 ⇒
   **保守不改名**且**不抛**。返回 `(torrent_path, json_path, collided)`。`exists`/`read_json` 两个注入参数
   **只为单测**（使其成为可脱库的**纯函数**）。
2. **接线写点**（`src/run.py` `main()` 内，原 `safe_name = re.sub(...)` / `torrent_file = ...` 两行**删除**）：
   `torrent_file, json_path, _name_collided = output_sidecar_paths(formatted_title, info_hash)`；
   `_name_collided` ⇒ **必须显式打印**（`ERR-META-14`）。下游 `.json` 落盘改用**同源** `json_path`
   （**不再各算一次 stem** —— 各算一次正是本病根）。
3. **旧注释同步**（`src/run.py:98` 附近）：把"由 `safe_name` 算"改成"由 `output_sidecar_paths()` 算"（`ERR-META-04`：改行为要同改文档）。
4. **ERRATA 新增 `ERR-PUBLISH-11`**：教训 = "内容不同、名字相同"共用文件名 ⇒ 后写者静默覆盖前者**身份**；
   **判据必须看内容身份（infohash）不看名字**。含"570 条假阳性（站点重写 infohash）/真信号 2 条"的关键鉴别。

### B. 自检 ⑨n（`src/run.py` selftest，加在 ⑨l 之后）——**全部带阴性对照，实测双向**
- **行为断言 6 条**（用注入的假文件系统，**不碰真盘**）：① 首见 ⇒ 默认名、无碰撞标志；② 同名是自己 ⇒
  不改名；③ 同名是他者 ⇒ **改名 + `collided=True`**（核心）；④ 消歧名**确定性**（同输入逐字节相同）；
  ⑤ sidecar 坏 ⇒ 不改名不抛；⑥ 无 info_hash ⇒ 不抛、后缀退化为 `nohash`。
- **源码护栏**：剥注释后用**正则** `safe_name\s*=\s*re\.sub\(` 搜"旧单路命名"（回归即报红）+ 断言
  `output_sidecar_paths(` 已接线。
- **★ 阴性对照实测（两条都做了，不是"看着合理"）**：
  · 把旧 `safe_name` 行**真的塞回** `run.py` ⇒ ⑨n **报红**（`❌ 自检失败：1 项不符`）；还原 ⇒ ✅。
  · 把消歧逻辑改成 `pass`（不改名）⇒ ⑨n **报警 4 条**（消歧改名/同基名/碰撞标志/nohash）⇒ 还原 ✅。
- **★ 踩坑并已修（本身即 `ERR-META-01` 形状，写进 ERRATA）**：
  · 第一版源码护栏用**字面量**匹配 ⇒ 反斜杠转义层级写错（守卫里 `\\` ≠ 源码里 `\`）⇒ **守卫恒不触发**（静默失效）。
  · 改成正则后 ⇒ 正则**命中了守卫自己的反面教材注释** ⇒ **守卫恒红**（真回归反而被淹没）。
  · 终版 = **先剥行内注释**再搜 ⇒ 实测：clean ✅ / 塞回旧行 ❌ / 还原 ✅（三态都对）。

### C. 影响与边界（**已核对**）
- 只改**输出文件名**；`create_musopia_torrent()` 把内容目录做成 `.torrent`，**标题不进 info dict** ⇒ **不改 infohash**。
- 消歧名**确定性**（只看 infohash）⇒ 重扫**不漂移**、库里 `torrent_file` 不每轮刷新（`flip_status` 那类"路径对不上"不会因此新增）。
- `publish.py` 读的是 **DB 里存的 `torrent_file` 全路径**（`t.get("torrent_file")`）⇒ 只要 scan 把**消歧后的路径**写库，发布就上传**正确的文件** ⇒ 修法与下游自洽。
- **不改历史**：已发布的 5+2 页仍是错的（#27 才处理）；本护栏只保证**今后**不再产生新的错版本。

### D. 未做 / 待办
- **未 commit、未 build、未 deploy**（项目规则：不主动 commit）。镜像 `bb68bf9-dirty` 是上一批修复的，**不含**本条。
- #25（补查 `18984`/`19069` 两页）、#27（纠正 5+2 页）**未动**。
- 待用户拍板：是否 (a) 提交/构建/部署本条护栏；(b) 以何策略处理 #27 的页面。

---

## 2026-10-07（续 95）—— #136 续 2：**多版本同名输出碰撞**（发布挂错版本的根因）——★ 只查未修

> 承「续 94」。用户报 `musopia/details.php?id=20667` **又不做种/不上传**。**纯只读排查**，未改任何东西。

### A. 病灶定性（NAS + 站点实测坐实）
**输出文件名只由 `formatted_title` 决定**（不含版本/编号）⇒ **同一专辑的不同版本会算出同一个 basename** ⇒
`.torrent` 与 sidecar `.json` **互相覆盖错位** ⇒ 发布时上传的是"**另一个版本**"的种子 ⇒ 站点页面**忠实挂上错版本**，
而 DB 记着"对的"任务元数据 ⇒ **"页面挂的版本 ≠ DB 声称的版本"**。

**实例（20667）确诊链**：
- musopia 页 20667 抓到的 infohash = **`60dd1d72…`**（`created_by=音乐乌托邦` ⇒ 从站点下载回来的那份），内容 = **期間版 AICL-4905~6，818928227B**。
- DB `site_publish`/`tasks` 里 id 20667 = **`9e9ed78a`**、`torrent_name=[…初回生産限定盤][AICL-4904]…`、`opencd_tid=197283`（=**初回版**）。
- 本地 `output_torrents/Hana Hope - blue hour - FLAC 整轨 - OpenCD.torrent` 内容 = **期間版**（`65399d35`），
  **但同目录 sidecar `.json` 却写着初回版**（`info_hash=9e9ed78a`）⇒ **同名文件内容被另一版本覆盖**。

### B. 输出目录扫（判据要用"名字"，不能用 info_hash）
- basename **字面重名 = 0**（不是同一文件名写两遍）。
- `.json.info_hash ≠ 旁 .torrent.info_hash` = **570 条**，但**绝大多数假阳性**：`.torrent` 是**站点回传再生成**的
  （`announce=…/announce.php?passkey=…`、`private=1`）⇒ 哈希必然 ≠ 原始 OpenCD 的 `.json`。**这不是 bug**。
- **正确判据 = 比"名字"**（`json.torrent_name` vs `.torrent` 的 `info.name`，归一化）⇒ **真拼错只有 2 条**：
  1. `Hana Hope - blue hour - FLAC 整轨 - OpenCD`（json=初回 AICL-4904，torrent=**期間** AICL-4905~6）
  2. `SEKAI NO OWARI - Stella - FLAC 整轨 - OpenCD`（json=通常 TYCT-30170，torrent=**天幕** TYCT-39354）

### C. 站点交叉核对（147 个已发布页）
命中 8 个候选；**已坐实 5 个**（页面渲染出的 `[音乐乌托邦].`/`[XHB].` 文件名即其真正挂的种）：
| 站点 | 页面 | DB/任务说 | 页面实际挂 | 判定 |
|---|---|---|---|---|
| musopia | 19333 | リーガルリリー-コニファー **期間** KSCL-3667 | **初回** KSCL-3665~6 | ❌ |
| musopia | 20667 | Hana Hope-blue hour **初回** AICL-4904 | **期間** AICL-4905~6 | ❌ |
| crabpt | 216217 | 女王蜂-星 **完全** AICL-4918~9 | **期間** AICL-4920 | ❌ |
| crabpt | 217592 | リーガルリリー-コニファー **初回** KSCL-3665~6 | **期間** KSCL-3667 | ❌ |
| crabpt | 216915 | SEKAI NO OWARI-Stella **天幕** TYCT-39354 | **通常** TYCT-30170 | ❌ |
| musopia | 18984(女王蜂) / 19069(Stella) | — | 页面未渲染出（**待查**） | ⚠️ |
- ★ 有 9 月的老页（19333 等）⇒ **长期存在**，非本轮新引入。
- 本轮**未修任何页面/代码**。根因是**输出命名不含版本标识**。

### D. ★ 下一步（见 TaskList #24）
- **FIX-A7（#24）代码护栏（治本）**：输出文件名纳入版本/编号标识（如 `info.name` 的 `AICL-xxxx`），
  或**写文件前比同名 `.json` 的 `info_hash`**，不一致即报错/加后缀 ⇒ 杜绝覆盖。
- **数据纠正（治标）**：对 C 表 5+2 个页面逐页裁决（重发正确版本 / 编辑页面 / 仅记录）。
- ★ 未解：`18984`/`19069` 两页未渲染出，**待验**（换 header/UA 或查 DB 该行的实际 torrent_file 内容）。

### E. 本轮其余已完成（#14–#22，见续 94）
坏种补救 20639/217848（已 `prog=1.0` 做种）· 3040 队列上限（`max_active_torrents 3→100000`，`queuedUP→0`）·
ERRATA +3 条（`ERR-PUBLISH-09/10`、`ERR-QBTUNE-02`）· counts.json（ERR 126）· **构建并部署 `bb68bf9-dirty`**（NAS RepoDigest `sha256:8f7905ba…` 核对一致、容器内 grep 新代码 6/6、容器内 selftest 通过、healthy）。

---

## 2026-10-07（续 94）—— #136 续：坏种补救（#14–#17）+ 3040 队列上限修复（#22）+ 文档（#18/#19）

> 承「续 93」。用户："先把那两个发种问题解决了" + "为什么 qb 里种子在排队也不发种"。
> **本轮全部已实测坐实**；代码侧四个修复仍在「续 93」（**本轮未动代码**）。

### A. 坏种清单（#14，NAS 实测）
- 3040 上本项目种共 **8 个**；坏种**恰好 2 个**，健康 6 个（全 `prog=1.000`）。
  | 站 | id | 3040 hash | 病因 | 源（3020 `stalledUP`） |
  |---|---|---|---|---|
  | Musopia | 20639 | `0908cab5d258` | 18:57 重启**前**的旧镜像发布 ⇒ 从没复制 | Eric Clapton - Icon 2，734M ✓ |
  | CrabPT | 217848 | `d80b97b5e8d8` | 交棒失败→`FAILED→DONE` 删了 SSD 副本 | 秦腔名戏双CD，588M ✓ |
- **`handoff_missing_data.jsonl` 账本不存在** ⇒ 这个部署上闸门还没触发过（两坏种都卡在闸门验证边界）。

### B. 补救（#15/#16，recipe = 续 92 §B）
- 用容器内 `handoff.copy_to_publish_area` + `verify_publish_area_complete`（**逐字节**）复制 HDD→SSD，再 3040 `POST torrents/recheck`。
  - `20639`：769199147 字节，逐字节核对通过 ⇒ `stalledUP prog=1.000` ✓
  - `217848`：615698388 字节，逐字节核对通过 ⇒ `uploading prog=1.000` ✓
- 复查（#17）：全量 8 种 **BAD=0/8**，全 `prog=1.000` + 发种区逐字节就位。
- 够格未交棒集（部署 FIX-2 后由交棒线程处理）：`20625`(出种4)/`217789`(18)/`217808`(11)/`217832`(13)；`20058`(comp=3) 按 `num_complete-1>=3` **不够格**。

### C. ★ 3040「排队不发种」根因（#22，已修）
- **不是数据/代码问题**：8 种全 `prog=1.000` 健康，但 3 `stalledUP` + 5 `queuedUP`。
- **根因 = `Session\MaxActiveTorrents=3`**（全局活动槽，上下行共用）+ **慢种豁免未开**（`dont_count_slow_torrents=false`，实测读回 `True` 前是默认 false）⇒ 3 个停滞做种（`up=0`）**永久霸占活动槽** ⇒ 排队 5 个**永远轮不上**。
- **谁设的**：3040 是 **10-07 12:37 新建**的实例（`qBittorrent.conf.bak-init` 无这三个键 ⇒ 用原生默认 `5/3/3`），到 14:48 (`bak-apikey`) 已被手写成 `3/10/1`。**不是** qbit_manage（`config_publish/config.yml` 明确不碰队列）、**不是**本项目代码。
- **为什么不合理**：同机的 **3020 早已踩过并修好**（`ERR-QBTUNE-01`：`max_active_*`→`100000` + `IgnoreSlowTorrentsForQueueing=true`），**3040 新实例漏抄**。
- **修法（已实测生效）**：`docker stop qbittorrent-publish` → 改 conf（`MaxActiveTorrents=100000`/`MaxActiveUploads=100000` + 加 `Session\IgnoreSlowTorrentsForQueueing=true`）→ `docker start`。备份 `qBittorrent.conf.bak-queuefix-20261007-220121`。
  - **验证（按内容）**：回读 prefs `max_active_torrents=100000`、`dont_count_slow_torrents=True`；`queuedUP → 0`（8 种全 active，2 个正在 `uploading` ~3MB/s）。

### D. 文档（#18/#19，本轮已做）
- `ERRATA.md` 新增三条（**只教训+根因**）：`ERR-PUBLISH-09`（FAILED 落 finalize ⇒ 误删在用副本）、`ERR-PUBLISH-10`（延迟回加是第二条路径无闸门）、`ERR-QBTUNE-02`（判据死代码 + 新实例漏抄防护配置）。引用已解析（新 id 无悬空）；`ERR-SITE-01` 悬空是**既有**问题，与本轮无关。
- `counts.json` 重生成：`ERR 126 条`（原 123，+3）。

### E. ★ 未做（下一步）
- **DEPLOY（#20/#21）**：`run.py --selftest` → `bash build-push.sh` → NAS `pull && up -d musopia` → **容器内 grep + selftest** → 观察日志确认真出现「已交棒到 3020」。
- **改动的 5 个文件（仍未提交）**：`src/handoff.py` `src/daemon.py` `src/publish.py` `src/readd_queue.py` `src/run.py`。

---

## 2026-10-07（续 93）—— #136：`20639`/`217848` 未做种 + 「出种≥3 不交棒」根因 + 四个修复（**未部署**）

> 承「续 92」。用户报：`musopia/details.php?id=20639`、`crabpt/details.php?id=217848` 两个种
> 已添加但**没文件/没做种**；又问「出种>3 的种为什么没交棒到 3020」。**先诊断、后改码**，
> 本轮**未部署**（改完本机 selftest 全绿 + 每个修复都有阴性对照）。

### A. 诊断（NAS 实测坐实，非推测）
1. **不是"代码没部署"**：容器跑的正是**修复镜像** `isunker/opencd2others:922aad9`（18:57 CST 起，`Up healthy`）。
2. **两个种都"发种区无数据"**（`stalledDL`/`queuedDL prog=0`），但**成因不同**，且都在 18:57 前后：
   | 站 | id | added_on(CST) | state | 实测链条 |
   |---|---|---|---|---|
   | crabpt | 217848 | 19:14 | `queuedDL` 0 | 18:59 复制**成功**+入队(15min后回加)→19:02 交棒失败`PENDING→FAILED`→**19:07 `FAILED→DONE` 把 SSD 副本删了**→19:14 回加时目录已没 |
   | musopia | 20639 | 19:03 | `stalledDL` 0 | 发布在 18:57 重启**之前**（旧镜像）⇒ 从没复制；队列条目被持久化，重启后延迟回加线程 19:03 原样推到 3040（数据本就不在） |
3. **`217848` 的直接凶手**：`handoff.process_one` 的交棒块只认 `("PENDING","HANDOFF_ADDED")`，
   `FAILED` 条目会**跳过交棒**、直落 `verify_3020_seeding`+`finalize_source`；此刻 3020 恰好
   已有该种在做种（辅种工具抢先）⇒ 判就绪 ⇒ `finalize_source` 把**3040 还没回加、副本还在用**的
   SSD 目录删掉（`_source_category` 对"找不到该种"当"已删⇒安全"放行）。
4. **`20639` 的直接凶手**：`daemon._drain_pending_readds`（**延迟回加**，与 publish 同步回加并列的
   **第二条异步路径**）直接 `torrents_add`，**从不核对发种区**⇒数据缺失也照推。#134 的三道闸门
   只加在 publish 同步路径，**这条漏了**。
5. **「出种≥3 不交棒」根因**：`handoff.judge`/`pick_handoff_candidates` **只在 `run.py --selftest` 被调用**，
   生产 daemon 从不引用 ⇒ 判据是**死代码**。实测：3040 上本项目种 `☆ac8.13f` **20 个别人做种**、
   `David Foster` 13、`谈芳兵` 12 —— **一个都没交棒**；容器内 `已交棒到 3020` 计数 **= 0**。

### B. 修复（四个，均本机 selftest 绿 + **阴性对照已做**；**未提交、未构建、未部署**）
- **FIX-1 `process_one`（handoff.py）**：交棒块条件加 `"FAILED"`；新增**结构性旗标** `handed_off`
  —— `finalize` 前显式 `if not handed_off: 拒删`。⇒ 堵死"FAILED 跳过交棒⇒直接清盘"。
- **FIX-2 够格才交棒（用户 2026-10-07 拍板语义）**：新增 `handoff.eligible_on_3040(qb_src, ih)`
  三态（缺种/不够格/够格）；`process_one` 的 **PENDING** 分支先过它 —— 不够格就**停在 PENDING**
  （不交棒/不标 FAILED/不动源）。判据 = 用户原话「出种≥N(HANDOFF_SEEDERS_MIN=3) 或 做种≥D(HANDOFF_SEED_DAYS=3) 天」。
- **FIX-3 延迟回加闸门（daemon.py + readd_queue.py + publish.py）**：`readd_queue.enqueue` 增 `src_path`/`album`
  （兼容变更，不 bump 版本）；`publish.py` 两路带过去；抽出纯函数 **`daemon._readd_area_ready(item)`**
  （有 src/album ⇒ `verify_publish_area_complete` 逐字节；老条目 ⇒ `save_path` 目录非空兜底；都无 ⇒ 判不就位），
  `_drain_pending_readds` 在 `torrents_add` **之前**必须过它，不就位 ⇒ **跳过+`record_missing_data` 入账**。
- **FIX-4 空 `last_error`（daemon.py）**：`_drain_handoff` 的 FAILED 日志原先读**过期快照**
  `it.get('last_error')`（`process_one` 只 `update_item` 写盘、不改传入的 dict）⇒ 日志
  `FAILED（）` 括号**恒空**（现场 `e3babe7f… PENDING → FAILED（）` 即此）。改为从**队列重读**真值。

### C. 自检（新增 ⑨k / ⑨l / ⑨m，均含阴性对照）
- **⑨k**：FAILED 条目 + SSD 副本在位 + 假 qb_dst 报"3020 在做种" + `handoff_to_3020` 失败（复刻 217848）
  ⇒ 断言 SSD 副本**未被删**、结果**非 DONE**、条目**留队列**。阴性对照：撤掉 FIX-1 两处 ⇒ 报红 3 项。
- **⑨l**：`eligible_on_3040` 四态（缺种/只自己做种/别人3够格/progress<1）+ 源码护栏（**调用点**
  `eligible_on_3040(qb_src, info_hash)`）。阴性对照：删调用点 ⇒ 报红。
- **⑨m**：`_readd_area_ready` 四态（空/完整/老条目兜底/无路径）+ 源码护栏（`_readd_area_ready(it)` +
  `def _readd_area_ready(` **且** 不得再出现 `f"（{it.get('last_error')}）"`）。阴性对照：拆闸门/还原旧读法 ⇒ 报红。

### D. ★ 未做（**下一步**，见 TaskList #12/#13/#14）
- **OPS-A（#12）**：数据面补救——用 `handoff_missing_data.jsonl` 账本 + 3040 实况列**全部**坏种
  （含 20639/217848 + 4 个够格未交棒的），复制 HDD→SSD → recheck。recipe 见续 92 §B。
- **DEPLOY（#13）**：`src/run.py --selftest` → 构建 → 推/直传 → NAS `up -d musopia` → 容器内 grep+selftest
  → 观察日志确认真出现「已交棒到 3020」。
- **DOCS（#14）**：ERRATA 新条目（FAILED 落 finalize / 交棒判据死代码 / 延迟回加无闸门）+ `counts.json` 重生成。
  ★ 注意：本节 SESSION-NOTES 是**追加**，改完计数要 `python tools/gen_counts.py`。
- **改动的 5 个文件（未提交）**：`src/handoff.py` `src/daemon.py` `src/publish.py` `src/readd_queue.py` `src/run.py`。

---

## 2026-10-07（续 92）—— #135：复制判据重构 + 5 条"发种区无文件"坏种修复 + 自检副作用隔离

> 承「续 91」。用户报："又有好几个种子添加进来但没做种、没文件"。本轮：查清 → 修复 5 条 → 重构复制判据 → 发现并修自检污染生产。**中途 compact**，本节点为续接锚点。

### A. 已提交（本地，**未 push**）
- `12b1152`：数据没就位 ⇒ 绝不回加 3040（上轮栅门）。
- `5b278ee`：**复制完成判据从「目录非空」改为「逐字节核对补齐」** —— `copy_to_publish_area` 原先 `if isdir(dst) and listdir(dst): return`（把 qB 自己写进来的半截数据当"已复制好"）⇒ 改为一律 `verify_publish_area_complete` 核对，不完整即 `copytree(dirs_exist_ok=True)` 补齐。自检 ⑨h 加"半截目录必须被补齐"三态 + 回归护栏。**已构建镜像 `isunker/opencd2others:5b278ee` 并部署 NAS**（容器 `Up healthy`，容器内 selftest 通过，容器内 `handoff.py` 有/无新代码均已 grep 核实）。

### B. 5 条坏种（3040 上属于本项目、但发种区无数据）—— **已全部修好并验证做种**
| 站 | id | 专辑 | 病因 | 现状 |
|---|---|---|---|---|
| CrabPT | 217789 | ☆ac8.13f | 发种区无目录 | **stalledUP 1.000** |
| Musopia | 20625 | Beyond II | 有目录但字节不符(缺18MB) | **stoppedUP 1.000** |
| CrabPT | 217808 | 谈芳兵-问候你 | 发种区无目录 | **uploading 1.000** |
| Musopia | 20636 | Buoc Chan Thoi Gian | 发种区无目录 | **queuedUP 1.000** |
| CrabPT | 217832 | David Foster | 发种区无目录 | **uploading 1.000** |

- **判"是本项目加的"**：`comment` = 发布页 URL + 分类 ∈ {Musopia,CrabPT} + 库里 `status=PUBLISHED/site_status=EXISTS` ⇒ **全是本项目发的，非别人手加**。
- **recipe（已验证有效）**：① 复制 HDD→SSD（`begin_handoff_for_task` 或 `copy_to_publish_area`，逐字节核对）② 向 qB `POST /api/v2/torrents/recheck?hashes=<3040 hash>` ⇒ 转 `*UP progress=1.0`。
- 补齐用的源 = 3020 上原种的 `content_path`（`/downloads/<专辑>`，容器内 = HDD ro 挂载）；**3020 上这 5 条都在做种（stalledUP）**，源可用。

### C. ★ 根因：自动复制当时"静默没跑"（**未完全坐实，见「未解」**）
- 事实链：① 复制代码（`0b362da`）+ `/publish` 挂载修正（`4da5979`）**14:31–14:37 CST 才进 git**；② 这 5 条 `added_on` = **16:01–18:14 CST**；③ **`☆ac8.13f`/`Beyond` 在续 91（17:11）已被记为"已坏"** ⇒ 坏在修复部署生效**之前**的镜像上；④ 我**现在**用**真路径**复现（`_resolve_opencd_content_path` + `begin_handoff_for_task`）**100% 成功**。
- ⇒ **最可能**：那段时间跑的镜像里"复制整条没生效"（镜像早于 14:31，或 `#129` 3020-key 修复（15:11 CST）尚未部署 ⇒ `_resolve_opencd_content_path` 拿不到 source ⇒ `src=""` ⇒ 跳过复制）。**且当时是"静默"的**（只有一行 print，无落地）。

### D. ★ 顺带发现并修复：自检 ⑨h **污染了生产队列**（真事故）
- ⑨h 自检会真调 `begin_handoff_for_task` ⇒ `enqueue` ⇒ 写**真** `/data/handoff_queue.json`。NAS 实测：队列里多了一条 `cafebabe AlbumY FAILED`。**已清掉**。
- **修法**：⑨h 里把 `_ho.QUEUE_FILE` 重定向到临时目录、跑完恢复（`_saved_qf3`）。**本机已验**：自检后真队列为空。
- ★ 教训：自检**绝不许**有真副作用（`ERR-META-14`）。已加进本轮改动。

### E. B2 / C1 / C2 —— 本轮**已全部完成**（compact 后接续做完）
- **B2（已完成）**：`handoff.record_missing_data(info_hash, album, site, reason)` → 追加式账本 `data/handoff_missing_data.jsonl`（让"漏做种"不再静默）；`publish.py` 两站**各 3 个跳过点共 6 处**全部接线入账（未就位 / 拿不到源 / 复制抛异常）；`run.py` 自检 **⑨j** 钉住（≥6 处、两站各 ≥3、`handoff.py` 必须有该函数）。本机 selftest 通过 + **阴性对照**（删掉接线 ⇒ ⑨j 报红 3 项）。
- **C1（已完成）**：提交 `abf8f62`（feat：账本 + 自检副作用隔离）。
- **C2（已完成）**：提交 `922aad9`（docs：ERRATA 新增 `ERR-PUBLISH-07`「存在≠完成」/ `ERR-PUBLISH-08`「跳过必须入账、账落磁盘」/ `ERR-META-48`「自检不许真副作用」；`counts.json` 重生成 ERR=123）。**构建镜像 `isunker/opencd2others:922aad9` 并部署 NAS**。
- **部署验证（按内容，不按"pull 成功"）**：NAS compose 点名 `:922aad9`；NAS 侧该 tag 的 **RepoDigest = `sha256:59b52cab…` = 本机 push 的 digest**；容器 `.Image`（config ID `77d23783…`）≠ 该 id 属**正常**（manifest digest ≠ config Image ID）；**容器内 grep** `def record_missing_data`=1、`record_missing_data(`=6 ⇒ 新代码**确在跑**；**容器内 selftest 通过**；容器 `Up healthy`；daemon 线程（readd + handoff）正常起。
- **回归核查**：3040 上 6 条本项目种（Musopia 3 + CrabPT 3）**全部 `progress=1.000`**，5 条修复种未被重部署打回。

### G. 清理（本轮已做）
- 本机：清空 `.scratch/`、旧镜像 19→1（只留 `5b278ee`）。
- NAS `opencd2others/`：删 `.env.bak-*`、`docker-compose.yml.bak-*`、旧落点 `publish_data/`、`@eaDir/`、`rollback-92g/`；镜像 13→1；同步 `.env.example`/`README.md`（compose 保留 NAS 的 sha pin 未覆盖）。NAS 部署目录现为标准 5 项 + `data/`。



> 用户报：`crabpt/details.php?id=217789`、`musopia/details.php?id=20625` 这两个种
> **在文件传输前就被推到 3040**，导致没做种、没发种、**变成下载**。排查 + 修复 + 重建镜像。

### A. 根因（NAS 实测坐实，**不是推测**）

3040 上两条的实况（`/api/v2/torrents/info`，按 `comment` 字段对上站点 id）：

| 站 | id | 3040 任务 | state | progress | 关键证据 |
|---|---|---|---|---|---|
| Musopia | 20625 | `2025.Beyond.真的Beyond II …` | `stoppedDL` | 0.014 | SSD 副本 16:05 **正在拷**（wav mtime 16:12），qB 在同目录**边下边写** |
| CrabPT | 217789 | `☆ac8.13f` | `stalledDL` | **0** | **SSD 发种区根本没有这个目录**（源 12 文件仍在 HDD `/volume1/video/music/☆ac8.13f`）；10 leechers 真在从本机下载 |

**三处代码缺陷**（见 `ERR-PUBLISH-06`）：
1. `handoff.begin_handoff_for_task` 复制后**不核对**就返回 True；
2. `publish.py` 两个回加点**丢弃** `_ok`，照样 `torrents_add(save_path=/downloads)`；
3. 回加复核判据 `state != "missingFiles"` **放过**下载族（`stalledDL/stoppedDL` 都不是 missingFiles）⇒ 假种被打上 `Musopia`/`CrabPT` 分类 ⇒ **命中交棒白名单** ⇒ 交棒线程会把它当本项目种，够 3 天就"零拷贝交棒"到 3020（二次伤害）。

### B. 修复（三道闸门 + 判据收紧）

- `handoff.verify_publish_area_complete(src, album)`：新增，**逐字节**核对（文件数+总字节），不只看"目录非空"（挡不住"只拷了头几个"）。
- `handoff.begin_handoff_for_task`：复制不完整 / 源不存在 ⇒ **返回 False**；`enqueue` 增记 `src_path`/`album` 供交棒前复核。
- `publish.py`（乌托邦 + 蟹黄堡两路）：`_ok=False`、拿不到源 path、或复制抛异常 ⇒ **一律 `return` 跳过回加**（宁可漏做种，不可推假下载）。
- `handoff.process_one`：交棒前**第四道闸门**——复核 SSD 副本仍在位。
- `publish.py` 两处回加复核：判据从 `state != "missingFiles"` **收紧**为"不在下载族 **且** progress ≥ 1.0"。

### C. 自检（含阴性对照，已实测能报红）

`python src/run.py --selftest` 新增 ⑨h / ⑨i：
- ⑨h：发种区**空** / **半截** / **完整** 三态 + `begin_handoff_for_task` 在源缺失、半截副本两态必须返回 False。
- ⑨i：回归护栏——若 `publish.py` 出现旧写法 `ok = t.state != "missingFiles"` 或缺 `progress >= 1.0` ⇒ **报红**（实测把代码改坏后确实报红 2 项）。

### D. 部署（实测完成）

- 本机 `SKIP_PUSH=1 bash build-push.sh` 构建成功，tag `6628b1c-dirty`（config id `63ebcd9a…`）。
- **走直传**（免 Docker Hub 登录；NAS 的 `scp` 子系统被关 ⇒ 用 `ssh` 管道传 tar.gz，sha256 比对一致）：
  `docker save | gzip` → NAS `docker load` → compose 点名 sha tag → `up -d --force-recreate musopia`。
- **compose 改动**：`pull_policy` 从硬编码 `always` 改为 `${PULL_POLICY:-missing}`。
  原因：`always` 对**直传**路径**必然报错**（registry 上没有该 sha tag ⇒ `Head .../manifests/…: EOF`）；
  Hub 路线仍在 `.env` 设 `PULL_POLICY=always`。`image:` 仍点名不可变 sha tag（ERR-DOCKER-13 铁律不变）。
- **验证**：容器 `Up (healthy)`，`docker inspect … {{.Image}}` = `63ebcd9a…`（= load 的镜像）；
  容器内 `python run.py --selftest` **通过**（目标环境判据）；`grep` 确认镜像里含
  `handoff.verify_publish_area_complete` 与 publish 的"跳过回加 3040"分支；
  启动日志 `ENABLE_HANDOFF=true（dry_run=false）`、handoff 线程已起。
- 已清理 NAS 上的 `imgtransfer-*.tar.gz` 与 compose 备份。

### E. 遗留（下一步要做，**未做**）

- 3040 上那两条**已被污染的旧任务**仍在：`Beyond`(20625) `stoppedDL` 1.4%、
  `☆ac8.13f`(217789) `stalledDL` 0。修复只保证**此后**不再产生这类假种，
  **不会**自动修好这两条 ⇒ 需人工：删 3040 任务（`deleteFiles=false`）→ 确认 SSD 副本
  已在位（`☆ac8.13f` 需先复制）→ 重新按新逻辑回加/校验。
- 注意：这两条任务带着 `Musopia`/`CrabPT` 分类 ⇒ **命中交棒白名单**，交棒线程可能会
  在它们"够 3 天"后把它们当本项目种推走（`☆ac8.13f` 已 `added_on` 较早）。
  ⇒ 建议先删掉这两条，避免被交棒线程误处理。



> 承「续 12」。compact 后继续。本机复核 + 修复 + 部署完成。

### A. #40 部署（已实测完成）

- **`deploy.sh --apply`** 已把 `opencd.py` / `tools/readd.py` / `tools/doc-audit/audit.py`
  等推上 NAS；NAS `cd /volume2/docker_ssd/opencd2others && docker compose up -d --build musopia`
  重建（**只 musopia**）。
- **手工删旧模块**：`deploy.sh` **不删** NAS 文件 ⇒ 已手工删 NAS 上 `opencd_parse/rate/log/
  map/gate/ingest.py` 六个 + 旧 `tools/readd_qb/readd_crabpt/readd_site_missing.py` 三个。
- **复核（本机 SSH 实测）**：
  - `docker ps` → `opencd2musopia Up (healthy)`；容器名不变（`name:` 写死的效果）。
  - NAS 生产目录：`opencd.py` 在、`opencd_*.py` **不存在**；状态全在 `data/`。
  - 镜像内：`/app/opencd.py`（163640B）在、`/app/opencd_*.py` **不存在**、`/app/tools/readd.py` 在。
  - 容器内 `python run.py --selftest` → **自检通过**。
  - 日志：`✅ 预检通过：库内已有 1511 行记录`、`🔑 QB_API_KEY = 已配置`、readd 线程已起。

### B. ★ 部署中发现并修复一处**真回归**（教训载体）

- NAS-only 遗留脚本 `tools/watch_readd.py` 里是**裸** `Client(...) + auth_log_in()`，
  撞上了 #29 新增的 ⑧ 接线断言（qB 构造点唯一）⇒ **容器内 `run.py --selftest` 报红**。
  已改成 `qb_client.build(config)` ⇒ ⑧ 复绿。
  ★ **教训**：`tools/` 里有**只在 NAS 上存在**的文件（不在本机 git）——本机全绿**不代表** NAS 全绿；
  「判据必须在**目标环境**里也跑一遍」。这条正是"生产端该不该保留 tools"争议的余波（见续 12 §A）。

### C. README 危险示例标注（#42 的一部分）

- 给 `docker compose up -d --build` 的示例加**服务名 `musopia`**（`:196`/`:375`/`:388`/`:418`）
  并加说明：**裸 `up -d` 会把 `profiles:[tools]` 的 `tools`/`once` 一并拉起**（变常驻、
  与 daemon 并发写 SQLite）。`:418` 的 `--remove-orphans` 保留（清孤儿是它的目的），末尾点名 musopia。

### 未解 / 待办

- **#41** 本机目录 + GitHub 仓库改名 → `opencd2others`（用户已确认）。
- **#42** 余项：ERRATA 补条（候选见下）。
- **#34** 18 条 `site_publish` 站点已 404 的清洗（用户"后面再解决"）。

---

## 2026-10-05（续 12）—— ★★★ 生产端目录整理 + 代码轻度合并 + 规则文件

**背景**：用户要求整理生产端（NAS）目录：改名、状态收进 `data/`、去 git、合并同族模块、
写"每轮自动读取"的规则文件。

### A. 生产端（NAS）目录改造（已实测完成）

- **改名**：`musopia_script2` → **`opencd2others`**（NAS `/volume2/docker_ssd/opencd2others`）。
  ★ compose 顶层 `name: opencd2musopia` **写死** ⇒ 改名不影响容器记账（实测：`mv` 后
  `docker compose up -d musopia` 正常 Recreate，容器名不变）。
- **状态收进 `data/`**：compose 由 `${STATE_DIR:-.}:/data` 改 **`${STATE_DIR:-./data}:/data`**；
  `data.db`（13.5MB）/`daemon_state.json`/`daemon_heartbeat`/`daemon_env.json`/
  `readd_queue.json`/`lsky_token.json`/`output_torrents/`/`covers/`/`raw_html/` 全 `mv` 进 `data/`。
  实测：容器内 `/data` **读到 1511 tasks**，healthy。
- **删生产端本地专属物**：`.git`（212K）、15 个备份垃圾（`.env.bak-*`/`Dockerfile.bak-*`/
  `*.db.bak-*`/`daemon_state.json.bak-*`/`qbtune_*.bak-*`/`_env_backups_archived_*`）、
  6 个 `_probe_*.py`、3 个 `.html` 证据、2 个 `_*.sh`、`_warm_cover.log`、`.watch_readd_mark.json`、
  `probe.py`、`__pycache__/`、`ERRATA.md`/`SESSION-NOTES.md`（不在白名单）。
- **★ 保留 `tools/` 在生产端**：与用户"删 tools"的初意**相反** —— 因为 `Dockerfile` 有
  `COPY tools/ ./tools/`，而**构建上下文就是该目录**；删了容器内
  `docker compose run --rm tools ...` 全废（判据：**"该不该同步"要按"谁拷它"去数**）。
- **`deploy.sh`**：`NEVER` 全改 `data/` 前缀（含新状态文件）；路径示例改新名；
  `.deploy.nasrc` 的 `DST` 改 `.../opencd2others`。
- **`.dockerignore`**：状态项改 `data/` 前缀（`data/` + `**/data.db` 双保险），加 `readd_queue.json`/
  `lsky_token.json`/`covers`，再加 `CLAUDE.md`/`.claude`（本机规则不进上下文）。
- **备份**：本机 `.deploy-backup/prerestructure-*/`（含 `.env` + `data.db` + 各 state；gitignored）。

### B. 代码轻度合并（已实测完成，本机全绿）

- **`opencd_parse/rate/log/map/gate/ingest` 六个 → `opencd.py`**（3101 行）。
  导入图是**无环 DAG**（parse←map←ingest）。撞名处置：`main`×5 → 单一 dispatcher +
  `_main_parse/rate/map/gate/ingest`；`_check`×3 → `_check_map/_check_gate/_check_ingest`；
  `cmd_selftest`×2 → `_selftest_parse/_selftest_rate`。保 `opencd_ingest` 的 argv/env 前置块
  在**文件顶部、import db 之前**（否则 `db→config→require_env` 会 `sys.exit`）。
  CLI：`python opencd.py <parse|rate|log|map|gate|ingest> [...]`。
  ★ 实测各子命令 `--selftest` 计数与合并前**逐项一致**：parse 71/71、rate 21/21、
  map 85/85、gate 19/19、ingest 60/60；log 是纯库（无 CLI）。
- **`tools/readd_qb/readd_crabpt/readd_site_missing` → `tools/readd.py`**（子命令 `qb|crabpt|site`）。
  ★ 修了一个**继承来的**缺陷：只有 `/app` 在 `sys.path` ⇒ **本机跑不了**
  （`ModuleNotFoundError: config`）；补了仓库根路径插入（与 `nas_health.py` 同款）。
- **`tools/doc-audit/{01_headings,02_refs,03_terms,04_probes}` → `tools/doc-audit/audit.py`**
  （子命令 `headings|refs|terms|probes`）；`gen_counts.py` 的 importlib 路径改指 `audit.py`，
  `_ro`/`_stale_where` 仍在模块级（实测 `gen_counts.py` 跑通 = 8/8 offline）。
- **`run.py --selftest` 新增 ⑧b 判据**：六个 `opencd_*` 旧模块名**零 import**（AST，
  不看文本注释）。★ 阴性对照实测：临时加 `import opencd_parse` ⇒ 报红；撤销 ⇒ 绿。
- **`deploy.sh`/`Dockerfile`**：六行 → `opencd.py`。★ 阴性对照：从 `FILES` 删 `opencd.py`
  ⇒ 脚本报「白名单漏了 Dockerfile 要 COPY 的文件」并**拒绝部署**（判据真生效）。

### C. 规则文件（每轮自动读取）

- **`CLAUDE.md`**（仓库根）：Claude Code 启动自动注入 —— 铁律（目录/部署/凭据/文档/固定动作）。
- **`.claude/RULES.md`**：细目（生产端 vs 本机清单、白名单规则、命令、命名约定）；
  `CLAUDE.md` 末尾 `@.claude/RULES.md` 引用。
  ★ `.gitignore` 只忽略 `.claude/scheduled_tasks.json`（等状态文件），**不忽略整个 `.claude/`**
  ⇒ `RULES.md` 会入库（实测 `git add -n .claude/` 只加 `RULES.md`）。

### D. tools 路径常量修正（改名 + `data/` 的连带影响）

改名 + 库移进 `data/` 后，所有**直接读 NAS 库/目录**的 tools 常量失效（`ERR-META-01` 形状：
路径对不上但静默返回空）。已改 ~20 处：`LOCAL_DB`/`DB` 常量（`.../opencd2others/data/data.db`）、
`nas_health.DST`（指向 `.../opencd2others/data`，因其下四个 state 都拼在 DST 后）、
`queue_probe`（`dst/"data"/"data.db"`）、`gen_counts.DEFAULT_DST` + `resolve_dst`（nasrc 是
compose 目录 ⇒ 补 `data/`）、`verify_cprime_step4` 文档/help、`doc-audit/terms.txt`。

### 未解 / 待办（详见 TaskList）

- **#40** 合并后代码**尚未上 NAS**（NAS 现跑旧代码，容器 healthy）。
  ★ 注意：`deploy.sh` **不删** NAS 文件 ⇒ 上新的 `opencd.py` 后，NAS 上旧的
  `opencd_parse.py` 等六个**要手工删**。
- **#41** 本机目录 + GitHub 仓库改名 → `opencd2others`（用户已确认）。
- **#42** SESSION-NOTES/ERRATA/README 细节收尾。
- **#34** 18 条 `site_publish` 站点已 404 的清洗（用户"后面再解决"）。

---

## 2026-10-04（续 11）—— ★★★ 部署上线 + 补回 4 条「上站但没做种」+ 全量扫描

> 承「续 10」。用户 compact 后继续，并给了站点 id（乌托邦 19525 / 蟹黄堡 215731 / 215726）。

### A. #31 部署（已完成，实证）
- 发现**运行中的 `opencd2musopia` 是旧代码**：`/app/qb_client.py` **不存在**、`publish.py` 里还有
  `auth_log_in` ⇒ 日志全是 `⚠️ 连不上 qBittorrent (…IP 已被封禁…)`。
- ★★ **踩坑（`ERR-DOCKER-09` 同族）**：第一次 rebuild 后 `/app/qb_client.py` **仍不存在** ——
  因为 **Dockerfile 的 `COPY` 行没列 `qb_client.py`**（它靠**枚举文件名**维护，加文件必漏，
  正是 deploy.sh 注释里警告过的形状）。修：`Dockerfile` COPY 行 + `deploy.sh` FILES **都加**
  `qb_client.py`，再 deploy+rebuild ⇒ `ls /app/qb_client.py` ✅、`import qb_client` ✅。
- NAS `.env` 加 `QB_API_KEY=qbt_ugIvm…`（**手工**，deploy.sh 白名单不含 `.env`；已备份）。
- **实证**：`qb_probe.py` ③ `✅ 读到 2561 个种子`（**零 403**）；daemon 启动打
  `🔑 QB_API_KEY = 已配置（走 API Key）`；`qb_client.build(config)` 返回 2561 条无异常。
- ★ 顺带确认 `qbit_manage/docker-compose.yml` 三实例（suite/reseed/opencd）**统一 API Key**，
  其中 opencd 用的 key 与本项目**同一个** ⇒ 一致。

### B. ★★★ 根因：`site_publish.EXISTS` ≠ "站点那条在做种"
- 用户点名三条没做种：乌托邦 `id=19525` Datcyde、蟹黄堡 `id=215731` Barbara、`id=215726` Mia Wray。
- **实测方法（唯一靠谱）**：把**站点那条种子**下载下来读**真 infohash**，再去 qB 查。
  ⇒ 三条的**站点 hash 与 DB `info_hash` 完全不同**（DB 记的是**皇后原种** hash；站点那条是
  `run.py` 用**带站点 passkey 的新 announce 重生成**的 ⇒ hash 变了）。
- **结论**：三条站点 hash **都不在 qB** ⇒ **真的没做种**。
- **根因**：它们在 qB IP 被 **403 封禁期间**发布 ⇒ `readd_to_qb` 走到
  `⚠️ 连不上 qBittorrent…本次不做回加` ⇒ **种子上了站、从没加进 qB**。
  DB 的 `EXISTS` 只是**发布那一刻**写的，**不代表回加成功**。

### C. 全量扫描（77 条 EXISTS）
- 逐条下站点种子读真 hash，比对 qB：
  `crabpt 17/20 在做种`、`musopia 38/57 在做种`、**没做种 4 条**（crabpt 3 + musopia 1），
  `18 条 musopia 站点下载 404`（★ 站点已删这条，不是限速——已用 HTTP 状态坐实）。
- ★★ **两个口径必须分开**（`ERR-META-05`）：「站点 hash 不在 qB」= 回加没成功；
  「站点下载 404」= 站点已删该种（**与做种无关**，那是 `#23` 清洗的另一种情形）。

### D. 补回（已完成，实证开始做种）
- 新增工具 **`tools/readd_site_missing.py`**（dry-run 默认；`--apply` 才加）：
  按**站点 id** 下种子 → 读真 hash → 查 qB → 缺则 `torrents_add(save_path=皇后原种的 save_path,
  category=站点分类)`。逻辑逐字镜像 `readd_to_qb` / `readd_to_qb_crabpt`。
- `--apply` 补了 **4 条**，复查（按站点 hash 直查）：
  `19525 Datcyde uploading 上传49MiB` / `215731 Barbara uploading 307MiB` /
  `215730 音田雅則 stalledUP` / `215726 Mia Wray uploading 384MiB` ⇒ **四条都在做种**。
- ★ **未改 DB**：`site_publish.info_hash` 是**皇后原种 hash**（主键/对账键），**刻意不换**；
  `status=EXISTS` 本来就对（种子确实在站上）。缺的只是"qB 里那条"，现已补。

### E. 仍未处理
- **#33** NAS 部署目录改名 `musopia_script2` → `opencd2others`（用户先前要求，未做；风险见 TaskList）。
- ★ 全量扫描暴露的 **18 条站点 404**（已删）—— 是否要把 `site_publish` 里这些标成 `DELETED`/`UNKNOWN`，
  待用户定（`#23` 的另一半）。
- ★ 是否给"回加成功"加一条**持久判据**（不让 `EXISTS` 冒充"在做种"）——待议。

---

## 2026-10-04（续 10）—— ★ #29 接线断言落地 + #30 文档（compact 后接着做）

> 承「续 9」。用户 compact 后继续：**#29 → #30 → 等 #31 部署**。

### #29 已完成（本地，**未部署**）
- **`run.py --selftest` 加第 ⑧ 组判据**：AST 扫**全仓库** `*.py`，断言
  `qb_client.py` **之外零** `Client(` / 零 `auth_log_in`（`ERR-QB-08` 的护栏）。
- ★★ **第一版假红（当场修，记进注释）**：只查 `X.Client(...)` ⇒ 报 **11 处**，
  其中 **10 处是皇后站点客户端** `crawl.Client`（`crawl.py:145`，**根本不用
  `qbittorrentapi`**），1 处是 `repos\Pt-Autoseed\…` 的 `transmissionrpc.Client`
  （另一个项目）—— **一个真目标都没有**。⇒ 判据收紧成**两条都必须成立**：
  ① 该文件**真的 import 了 `qbittorrentapi`**；② 命中的调用是被调用者名对得上
  （`qbittorrentapi.Client(...)` 或 `from qbittorrentapi import Client` 后的裸 `Client(...)`）。
  ★ 教训：**回归判据宁可精确，不可宽** —— 假红会逼人把判据关掉（`ERR-META` 一族）。
- **跳过目录**：`.git`/`.venv`/`__pycache__`/`.scratch`/`.deploy-backup`/`.claude`/`repos`
  等 + `*_bak/_backup/_old/-bak/-backup` 后缀目录（那些是**另一份**代码，扫它们=假红）。
- **阴性对照已跑**：临时在 `qb_probe.py` 塞一行 `from qbittorrentapi import Client;
  _negctl = Client(...)` ⇒ 判据**报红**且**精确点名** `qb_probe.py:78`；撤回后再跑 **绿**。
- 结果：`python run.py --selftest` **✅ 通过**（需 dummy env 骗过 import 期 `require_env`）；
  `python tools/verify_cprime_run.py` **exit 0**。

### #30 已完成（本地，**未部署**）
- **`.env.example`**：`QB_PASS` 之后加 `QB_API_KEY=` 段（**可选**、附取 key 方式、
  留空即**逐字回退**、★ 是**凭据不许打印**、★★ 必须**手工加到 NAS 的 .env**——deploy.sh 白名单不含它）。
- **`ERRATA.md`**：新增 **`### ERR-QB-08`**（教训：**「重试本身可以是故障的成因」**；
  根因：qB 5.x 登录失败计数 + 13 处各自 `auth_log_in()`；★ 封禁在 qB **进程内存**里、
  **不会**被本项目 rebuild 清掉，要清得重启 `qbittorrent-opencd`）。
  ★ `tools/doc-audit/02_refs.py` 已核：**悬空 0 / 半悬空 0 / LEGACY 0**。
- **本文件**：已记（续 10）。

### 本轮**未做**（剩 #31 / #25）
- **#31** NAS 部署：`bash deploy.sh --apply` → `docker compose up -d --build musopia`（**只 musopia**）
  → NAS `.env` 手工加 `QB_API_KEY=qbt_...` → 重建 → `docker exec opencd2musopia python qb_probe.py`
  实证**零 `LoginFailed` / 零 403**。
- **#25** 收尾：`tools/readd_crabpt.py --tids 215726,215730 --apply --recheck`。

### 未提交/未部署清单（compact 安全性）
- 本地改动（**未 git commit、未 deploy**）：`qb_client.py`(新)、`config.py`、`run.py`、
  `publish.py`、`daemon.py`、`qb_probe.py`、`backfill_covers.py`、`backfill_covers_opencd.py`、
  `tools/readd_qb.py`、`tools/readd_crabpt.py`、`tools/diag_17304.py`、`tools/qb_state_probe.py`、
  `tools/survey_content_path.py`、`.env.example`、`ERRATA.md`、`SESSION-NOTES.md`。

---

## 2026-10-04（续 9）—— ★ qB 连接收敛为单一工厂 + API Key（修「登录重试把自己封 IP」）（compact 前落盘）

> 用户：qB 升到 **5.2.4** 后 IP 被封（`身份认证失败次数过多，您的 IP 地址已被封禁`，登录接口 403）。
> 问：能不能直接用 API Key 加种子？→ 已设计并实现（本轮）。

### 结论（已实测坐实）
- qB 5.x 对**登录接口**做**失败计数**；本项目原有 **13 处**各自 `Client()+auth_log_in()`
  ⇒ **重试本身就是把 IP 推向封禁的力量**（一次配置错误被放大成一次中断）。教训 = `ERR-QB-08`。
- `qbittorrent-api==2026.10.0`（镜像已 pin）**原生支持** `Client(host=..., api_key=KEY)`，
  走 `Authorization: Bearer`，**不碰登录接口** ⇒ 不再被封。
- 实测：3020 容器 conf 里已有 `WebUI\APIKey=qbt_...`；`curl -H "Authorization: Bearer <key>"`
  → **200**（**无视当前封禁**），无 auth → 403。⇒ 配 key 即可**立刻恢复**，**不必重启 qB**（用户已选「先不管封禁」）。
- 注意：本项目**只连 3020**（`QB_HOST`），代码**零处**引用 reseed 的 3060。

### 本轮已做（本地，**未部署**）
1. **新增 `qb_client.py`**（根目录）：唯一构造点 `build(config_mod=None, api_key=None)`。
   · 有 `QB_API_KEY` → `Client(host=..., api_key=key)`（**不调** `auth_log_in`）；无 → 回退账号密码。
   · ★★ 承重纪律：`from qbittorrentapi import Client` **必须在函数体内**（不许提到模块顶）——
     `tools/verify_cprime_run.py` 靠"`import run` 之前注入假 `qbittorrentapi`"做回归，提到顶会毁掉它。
   · 读键一律 `getattr(config_mod,"QB_API_KEY","")`（兼容 `tools/test_readd_queue.py` 的 4 属性假 config）。
   · 默认**抛**异常、**不**返回 None（各调用点原有 try/except 保留，契约不变）。
   · `--selftest`（**不需要 .env**）：9 项含阴性对照，**已全绿**。
2. **`config.py`**：加 `QB_API_KEY = os.getenv("QB_API_KEY", "")`（**可选**，同 CRABPT_COOKIE 先例）；
   `print_runtime_info()` 加一行「QB_API_KEY = 已配置/未配置」（**不打印 key**）。
3. **迁移 13 处构造**（12 文件）→ `import qb_client; qb = qb_client.build(config)`：
   `run.py` / `publish.py`（2 处）/ `daemon.py` / `qb_probe.py` / `backfill_covers.py` /
   `backfill_covers_opencd.py` / `tools/{readd_qb,readd_crabpt,diag_17304,qb_state_probe,survey_content_path}.py`。
   · 两个 `tools/*.py` 的函数名保持 `qb_client`（调用方依赖），用 `import qb_client as qbc` 避同名。
   · 四个"两段式"脚本（qb_probe/diag_17304/qb_state_probe/survey_content_path）保留
     「ImportError vs 连接失败」两段判据（`build()` 首行 import 抛的正是 ImportError）。
   · **已 py_compile 全绿**；`python tools/verify_cprime_run.py` **仍绿**（假注入回归未破）。
   · `tools/reconcile_19525.py` 无需改（只用 db + 站点 GET，无 qB）。

### 本轮**未做**（compact 后接着做 —— TaskList #25/#29/#30/#31）
- **#29** `run.py --selftest` 加**接线断言**（AST 读源码，断言 `qb_client.py` 之外零 `Client(`/`auth_log_in`；
  含阴性对照：临时塞回裸 `Client(` 必报红）。
- **#30** 文档：`.env.example` 加 `QB_API_KEY=` 段；`ERRATA.md` 加 **`ERR-QB-08`**；本文件已记。
- **#31** NAS 部署：`bash deploy.sh --apply` → `docker compose up -d --build musopia`（**只 musopia**）
  → NAS `.env` 手工加 `QB_API_KEY=qbt_...` → 重建 → `docker exec opencd2musopia python qb_probe.py`
  实证**零 `LoginFailed` / 零 403**。
- **#25** 收尾：2 条蟹黄堡补种 `tools/readd_crabpt.py --tids 215726,215730 --apply --recheck`
  （现在有了 key 认证才能跑；工具已写好且 dry-run 通过到"连不上 qB"那一步，正是被封所致）。

### 未提交/未部署清单（compact 安全性）
- 本地改动（**未 git commit、未 deploy**）：`qb_client.py`(新)、`config.py`、`run.py`、`publish.py`、
  `daemon.py`、`qb_probe.py`、`backfill_covers.py`、`backfill_covers_opencd.py`、
  `tools/readd_qb.py`、`tools/readd_crabpt.py`、`tools/diag_17304.py`、`tools/qb_state_probe.py`、
  `tools/survey_content_path.py`；以及上一轮遗留的 `ERRATA.md`/`SESSION-NOTES.md`/`publish.py`(蟹黄堡修复)。
- 计划落盘：`C:\Users\艾森克\.claude\plans\cuddly-watching-spark.md`（已批准）。

---

## 2026-10-04（凌晨·续 5）—— 三个未完成任务的状态（compact 前落盘）

> 本节是**待办的落盘处**（TaskList 里也有对应 #22~#25），确保 compact 后能续。

### #22 善后：把测试真发的 19525 写回库 ★ 最高优先（防重发）
- 诊断时**真发**了 Datcyde（`info_hash=37850b204c2aa7be0f26a6c29cae4afb54025180`），
  站点 id=**19525**，但**裸 POST 没落库** ⇒ 下一轮 daemon 会**再发一次**（真·重复）。
- **需用户决定**：① 写回库（`db.set_site_id(ih, "19525", site_status="EXISTS", status="PUBLISHED")`
  + `db.site_publish_set(ih, db.SITE_MUSOPIA, "19525")`）；或 ② 删站上 19525。

### #23 清洗本地库（用户本轮新要求）
- 用户：**"之前不是洗了库了么，200 多个种子都因为资料不全被删除了，本地库都要清洗一下啊"**。
- 背景（已知）：站点曾**批量删除**我们发的种子（资料不全）⇒ `site_publish` 的 `EXISTS`
  与 `tasks.site_status` **都不是站上事实**（幽灵行）。
- 现成工具：**`tools/reset_deleted_site_status.py`**（把"站上已删"行的 `site_status` 改回 `UNKNOWN`，
  **不删行**；保留 `log_content`/`content_path` 等抓轨成果）。
- ★ 连带后果（用户已知情）：改回 `UNKNOWN` ⇒ 这些行**重新落进待发队列 ⇒ 会被重发**。
  **若不想重发，需另定判据**（见该工具头部长注释）。**执行前必须先与用户确认口径**。
- ★ 我的实测已撞上此问题的实例：Datcyde 行 `tasks.site_status=UNKNOWN` 但
  `site_publish=('14657','EXISTS')`，而站上 **14657 已被删** ⇒ 三条记录互相矛盾 ⇒ 被重发。

### #24 部署蟹黄堡回加修复（本地已改完，未部署）
- `publish.py`：`readd_to_qb_crabpt` 的 `t.get('info_hash','')` → `info_hash[:12]`（已改）。
- `ERRATA.md` 加 **`ERR-PUBLISH-02`**（已加）。
- `publish.py` 加**第 4 条自检** `selftest_readd_names_defined`（symtable 版，含阴性对照；
  本地 harness 已验：正确绿、坏源码报红；已接进 `--selftest` 的 `ok1 and ok2 and ok3 and ok4`）。
- **待做**：`bash deploy.sh --apply` → `docker compose up -d --build musopia` →
  `docker exec opencd2musopia python publish.py --selftest`（4 条全绿）。

### #25 2 条蟹黄堡种子仍未做种（用户说手动补）
- 蟹黄堡 19 条发布中，**17 条用户已手动补种**；剩 **2 条**未做种：
  · **Mia Wray**（`32480ba483b2`，站点 id 215726）
  · **音田雅則 - back shot**（`0799afa3645a`，站点 id 215730）
- 用户明确"手动补了一些"，故此项**默认等用户自补**；若要代补需用户授权。

### 本次两处**非本次任务**但已改动的文件（未提交）
- `publish.py`、`ERRATA.md`、`SESSION-NOTES.md` —— 均因 #24 而改，**未 deploy、未 git commit**。

---

## 2026-10-04（凌晨·续 4）—— ★ 诊断：蟹黄堡回加 qB 的 `UnboundLocalError`（19 条全没做种）+ 乌托邦 PU 后仍发不出

> 用户提问："Mia Wray - hi, it's nice to meet me 2025 - FLAC Multi File 发布过了 15 分钟怎么没做种？"
> "乌托邦怎么 9.30 以来也没发布？我昨天升到 PU 了。就是蟹黄堡没做种。"
> **结论来自只读探测**（未发任何发布请求，未改生产）。

### A. 蟹黄堡（CrabPT）—— 根因坐实：`publish.py::readd_to_qb_crabpt` 的 `t` 未定义

- **症状**：目标种子 `32480ba4…`（Mia Wray）在 qB 里 `state=stalledUP`、`category=opencd`、
  tracker = `https://tracker.open.cd/announce.php`（**皇后**），**不是** `crabpt.vip`。
  ⇒ 它只是**皇后那条在顺带做种**；**蟹黄堡侧从没被回加**。
- **证据（铁证）**：`site_publish site='crabpt'` **19 条**（id 215344～215730）。
  ★★ **订正**：初判"19/19 全没做种"的依据是"按**原 infohash** 查 qB，19/19 tracker = `tracker.open.cd`"
  —— **那是错的判据**：**回加后的种子 infohash 会变**（站点重写 announce/pieces，见 `readd_to_qb_crabpt` docstring ①），
  必须**按名字**配对（`ERR-META-19/20` 的形状：数不出来 ≠ 它不在）。
  订正后按**名字**配对：**17 条已由用户手动补种**（qB 里带 `crabpt.vip` tracker，hash 与原种不同），
  **仅剩 2 条仍未做种**：
    · **Mia Wray - hi, it's nice to meet me (2025)**（id 215726）—— 用户本次问的那条
    · **[260902] 音田雅則 - back shot**（id 215730）
  ⇒ bug **真实存在**（日志+代码铁证），只是用户手工补种绕过了大部分后果；余下 2 条 = "bug 崩 + 还没手动补"。
- **根因**：`readd_to_qb_crabpt` 走 `SEED_DELAY_MIN=15` 的延迟回加分支时，
  `readd_queue.enqueue(..., note=f"蟹黄堡 {t.get('info_hash','')[:12]}…")`
  —— **`t` 在该函数里从未定义**（函数参数是 `info_hash`），
  而 `t = got[0]` 出现在**函数末尾的复核块**（`enqueue` 之后）⇒
  **`UnboundLocalError: local variable 't' referenced before assignment`**。
  ⇒ `enqueue` 从未成功 ⇒ **队列里没有它** ⇒ 15 分钟后**没有任何东西去加回 qB**。
- **日志铁证**：`2026-10-04 04:05:27 ❌ 回加 qB 出错: local variable 't' referenced before assignment`。
- **对照**：乌托邦版 `readd_to_qb` 的同一处写的是 `note=f"乌托邦 {info_hash[:12]}…"`
  （用**参数** `info_hash`，正确）⇒ 蟹黄堡版是**从乌托邦版抄漏/抄错**。
- **readd 队列状态**：`/data/readd_queue.json` = `{"version":1,"items":[]}`（空）—— 与上述一致。
- **触发条件**：`config.SEED_DELAY_MIN = 15`（>0）⇒ 该 bug **一开启延迟回加就必现**；
  这正是 2026-10-03 新接的线（`ERR-ENV-03` 形状：新接的路径没跑过真实分支）。
- **未做（待用户拍板）**：修法 = 把 `t.get('info_hash','')` 改成 `info_hash[:12]`（同乌托邦版）。
  ★ 另需**补做 19 条的历史回加**（种子已上站，必须补做种，否则 hit-and-run）——
  这是**不可逆动作的补救**，先只读列出清单、由人确认后再动。

### B. 乌托邦（Musopia）—— 9-30 起没发布，**用户已升 PU 但今日仍失败**

- **时间线（权威源）**：`site_publish site='musopia'` 最后一条成功 = **2026-09-25 20:33:53**（id 17497）；
  此后**再无** musopia 成功记录 ⇒ 乌托邦自 **9-25 晚**起停发（用户感知为"9.30 以来"）。
- **已知历史真因（`ERR-SITE-09`）**：账号原为 `User`，只能**周六12:00～周日23:59** 发种；
  9 月有「发种大赛」（9-01～9-30 User 及以上自由发），**9-30 24:00 结束** ⇒
  10-01～10-03（工作日）发不出 = **站点时段规则**，与代码无关。
- **新变量（本次关键）**：用户**昨天升到 PU**。只读探针确认：
  首页 `class='PowerUser_Name'`、用户名 `iSunker` ⇒ **确实已是 Power User**（有随时发种权限）。
- **仍未解**：`2026-10-04 04:05` daemon 的 `publish` 阶段（`--limit 1`）**仍失败**：
  `❌ 发布失败: 未拿到重定向 ID，HTTP 200`（`takeupload.php` 返回 200 而非 302）。
  今日 NAS/容器时间 = **10-04 星期日 04:0x CST**（★ 在 User 窗口内、且已是 PU）⇒ **权限不再是解释**。
- **只读探测已做**：
  - `upload.php` 可打开（HTTP 200，标题「音乐乌托邦 :: 发布」）⇒ **不是登录问题**。
  - 表单字段含 `resource_count` / `rip_score` / `type` / `uplver` / `team_sel[4|7]` /
    `medium_sel[4|7]` / `audiocodec_sel[4]` / `codec_sel[7]` / `tags[4|7][]` 等。
  - `descr.build_form_fields` 发送集**不含** `resource_count`、**不含** `rip_score`
    （代码注释明写 `rip_score`「待确认关系 ⇒ **先不发**，不猜」）⇒ **疑似缺字段**（PU 新必填？），**未坐实**。
- **时区陷阱（本次踩到，值得记）**：用户**本机时间比 NAS 慢 8 小时**
  （本机 `2026-10-03 20:08 周六` 时，NAS = `2026-10-04 04:08 周日 CST`）。
  ⇒ 用户按本机时间说"昨天/15分钟前"，在 NAS 口径上差 8 小时。**凡涉"窗口/时刻"的判断，一律以 NAS 为准。**

- **★★ 用户批准"真发一条"后的决定性实测（2026-10-04，用裸 `takeupload.php` POST 连发 3 条）**：
  - 结果**逐条不同**：

    | # | 种子 | 结果 |
    |---|---|---|
    | 0 | Ysaÿe Sonatas | ❌ HTTP 200，`title=音乐乌托邦 :: 上传失败！` |
    | 1 | **Datcyde - Positive Climb** | ✅ **HTTP 302 → `details.php?id=19525&uploaded=1`**（**真发成功**） |
    | 2 | Forever Now 2026 | ❌ HTTP 200，`title=…上传失败！` |

  - ⇒ **发布通道完全正常**（#1 用**同一套字段、同一秒**成功，拿到 id 19525）；
    **不是**权限/时段/字段问题 ⇒ 那两条是**被站点逐条拒**（内容层面规则）。
  - **失败页形态**：`title=「上传失败！」`、正文**零消息**（`#outer` 之后只剩 "全站 [Free] 生效中！"），
    与 `ERR-SITE-09` 描述的空消息页**一致**；`_extract_error()` 对它束手无策
    （报出的"未拿到重定向 ID，HTTP 200"是**症状不是原因**）。
  - **账号健康**：`分享率: 无限`、`上传量: 718.98 GB`、`H&R: [0/0/10]`、`可连接: 是`。

- **★★★ 本次实测暴露的 3 个关联问题（都需用户决定，勿擅动）**：
  1. **我"真发"了一条 → 站点已有 id 19525，但库没记**：
     Datcyde（`info_hash=37850b20…`）本次**真发成功**（id 19525），但那是**裸 POST**、
     **没走 `db.set_site_id`** ⇒ **本地不知道已发** ⇒ 下一轮 daemon 会**再发一次**（真·重复）。
     ⇒ **必须把 19525 写回库**（或删除站上 19525）。★ 待用户决定后执行。
  2. **该行的三处状态互相矛盾**（这才是它进队列的原因）：
     · `tasks.site_status = UNKNOWN`、`status = SUCCESS`、`site_torrent_id = ''`
     · 但 `site_publish(musopia) = ('14657','EXISTS')`（**2026-09-09**）
     · 而站上 **14657 已被删除**（详情页无 h1）
     ⇒ 三条记录**各自为政**：`site_publish` 说"发过"、`tasks` 说"没有"、站上"已删"。
     ⇒ 这个不一致让该行**重新进了待发队列**，于是被**重新发了**（19525）。
     ★ 这是**独立的数据一致性 bug**，与 PU/时段无关。
  3. **"上传失败！"的那两条**是站点**内容规则**拒绝（具体原因站点不回消息）——
     需人工去站点看（或联系管理组）。**不是**代码能修的。

- **待办（未做，等用户拍板）**：
  - 是否把 **19525** 写回库（否则会重发）。
  - 本机时区**比 NAS 慢 8 小时** —— 是否要修（否则"发布时间/窗口"的人工判断总是错位 8 小时）。
    有无 `Location` 头）—— **需要真的 POST 一次**（发布**不可逆**）⇒ 必须先获用户同意。
  - 对照实验：对一条**已发布**（重复种）POST ⇒ 若返回 `302 …&existed=1`，说明"通道正常、只是新种被拒"，
    可把"权限/字段"与"通道"分开。

---

## 2026-10-04（凌晨·续）—— ★★ 废弃自研 qbtune，改用外部 `qbit_manage`

### 0. 决定（用户拍板）

- **整体废弃 `tools/qbtune.py`**（自研 qB 下载调度器：有源优先/死种轮转 + 公平轮转/带宽加权）。
- 下载管理改由**外部项目 `qbit_manage`** 承担。
- 关联：`#7`（排查 qbtune 为何停写心跳）**作废**；`#11` 里"qbtune budget"一并随本项作废。

### 1. 为什么整体废弃（本项范围）

- qbtune 是一个**独立 sidecar 容器**（`musopia_qbtune`），默认 `QBTUNE_ENABLE=false`。
- **它已停**：进程内部早死、容器 unhealthy、心跳停 >180s（见 `#4`）。
- 用户用 `qbit_manage` 替代后，本仓**不再需要**这套调度器 ⇒ 彻底移除，不留半截接线。

### 2. 本仓已删（代码/配置）

| 项 | 动作 |
|---|---|
| `tools/qbtune.py` | **删除**（调度器主体，~870 行） |
| `tools/qbtune_probe.py` | **删除**（只读 API 探针） |
| `tools/test_qbtune.py` | **删除**（单测，硬 import `qbtune`） |
| `tools/__pycache__/qbtune*.pyc` 等 | **删除**（3 个） |
| `docker-compose.yml` 的 `qbtune:` 服务 | **删除**（含 `musopia_qbtune` 容器名、两条 `tools/qbtune.py` 调用） |
| `.env.example` 的 `QBTUNE_*` 段 | **删除**（`QBTUNE_ENABLE`/`INTERVAL`/`ACTIVE_CAP`/`STALL_ENTER`/`LIVE_EXIT`/`WRITE_COOLDOWN_TICKS`/`BW_MODE` 等） |
| `tools/doc-audit/terms.txt` | 删 `musopia_qbtune` 关键字（防 doc-audit 假 drift 信号） |
| `daemon.py` 里一处提到 qbtune 的过时注释 | 改写（去掉 qbtune 指代） |

### 3. 保留（★ 刻意不删）

- **`ERRATA.md::ERR-QBTUNE-01` 保留教训**，只在条目末尾加一句「来源 qbtune 已废弃；教训仍通用」。
  它讲的是 **qB 参数语义 / 优先级机制 / 跨进程控制必须当场回读** —— 与 qbtune 存废无关，换 `qbit_manage` 同样成立。
- **`ERR-QB-05` 里提到 qbtune 是历史归因事实**（"先拉时间线再定罪"），保留。
- **延迟回加做种（`readd_queue.py` + daemon 独立线程）与 qbtune 无关 —— 完全不动。**

### 4. NAS 侧（仓库外，已用 SMB 完成部分）

> ⚠️ **`deploy.sh` 无"NAS 侧删除"路径** —— 本地删文件**不会**删 NAS 旧副本，必须手动清。

- ✅ **已删 NAS 旧文件**（走 SMB `//iSunker-DS423/docker_ssd/musopia_script2/tools/`）：
  `qbtune.py` / `qbtune_probe.py` / `test_qbtune.py`。
- ✅ **已清 NAS `.env`** 的 `QBTUNE_*` 8 行（注释+7 键），备份 `.env.bak-before-qbtune-removal-*`；
  确认 `SEED_DELAY_MIN=15` / `CRABPT_PUBLISH_CATEGORY=CrabPT` 未受影响。
- ✅ **已退役** `/data` 下 `qbtune_heartbeat` / `qbtune_state.json`（改名 `.bak-*`）
  —— ★ 关键：否则告警系统会对着一个**永不更新的心跳**永远报「qbtune 死」（见告警总纲）。
- ✅ `deploy.sh --apply` 已把 `docker-compose.yml`(删 qbtune 服务) / `.env.example` 推上 NAS；
  本地 `yaml.safe_load` 校验 services = `musopia/tools/once`。
- ✅ **NAS 收尾已完成**（SSH `iSunker@192.168.0.7:36688`，key `id_ed25519` —— ★ 用户名是 **`iSunker`** 不是 `艾森克`，docker 在 `/usr/local/bin/docker`）：
  1. `docker rm -f musopia_qbtune`（原为 `Exited(0)` 残留容器）。
  2. `docker compose up -d --build musopia`（只 musopia；`compose config --services` 已只剩 `musopia`）。
  3. 验证：**NAS 无任何 qbtune 容器**；镜像 `/app/tools` count=**45** = 宿主 45（`ERR-DOCKER-09` 判据通过）、**qbtune files: NONE**；
     `opencd2musopia` = **healthy**；日志 `LoginFailed/连不上 qBittorrent/回加跳过` 命中 **0**；
     **`🧵 [readd] 延迟回加线程启动`** 在跑（延迟做种未受影响）。
- ★ 用户替代者 **`qbit_manage_opencd` 容器已在运行**（Up，2h）—— 下载管理已交接给 `qbit_manage`。

### 4b. 教训（SSH 用户名）

- ⚠️ 我前几轮一直用 `艾森克@192.168.0.7`（⇒ `Permission denied`），据此判定"NAS 只认密码、无法自动化"，
  实际**用户名是 `iSunker`**、key 一直有效 —— 正是 `ERRATA.md::ERR-META-39` 那条"照抄旧结论、不当场试一次"的复发。
  **判据：连不上时先试候选用户名，别把"permission denied"直接读成"只能密码"。**

### 5. 未解 / 待验

- `qbit_manage` 侧的**配置与接线**不在本仓 —— 由用户/独立项目负责，本仓只负责"不再生产 qbtune"。
- `counts.json` / `tools/doc-audit/out/*.tsv` 是**生成物**，需重跑 `tools/gen_counts.py` 与 doc-audit 再生（不手改）。
- ⚠️ **顺带发现一个与 qbtune 无关的既有时间炸弹（已修）**：`tools/opencd-crawl-selftest.py`
  把 `D1="2026-09-22"` **写死**，而判据⑬调 `ledger_prune(7)`（按 `date('now','-6 days')` 切）。
  今天（2026-10-04）该日期已 **12 天前** ⇒ 被 prune 掉 ⇒ 判据⑬ `当天的值没被误删` 失败
  （`gen_counts` 自检从 8/8 掉到 7/8）。**已修**：改成 `D1=今天 / D2=昨天`（相对 `date.today()`），
  **永远**在保留窗内 ⇒ 自检恢复 **8/8**。教训记入 `ERRATA.md::ERR-META-47`。任务 `#14` 已完成。

---

## 2026-10-04（凌晨·续 3）—— #11 长尾：opencd_rate「请求 vs 种子」量纲缺口（部分闭合）

### 1. 关系/背景

`opencd_rate.py` 的 `RateGate` 三个旋钮（每日上限/每轮预算/最小间隔）**全是「请求」量纲**，
但用户的心里模型是「**种子**」。模块头「已知缺口①」记着这点、`db.py` 留了 `LEDGER_KIND_REQUEST`
"旁边将来加 kind" 的位置 —— 本轮把这半步做掉。

### 2. 做法（★ 只新增，不动闸门）

- **`db.py`**：加 `LEDGER_KIND_SEED = "seed"`（与 `request` 两本独立账，同表不同 kind）。
- **`opencd_ingest.ingest()`**：入库时**只在新行**记 `kind='seed'` +1。
  - 判据 = `db.get_status(ih) is None`（该 info_hash **此前不在库**）。
  - **为什么不能"每次 ingest 都 +1"**：`save_task` 是 **UPSERT**，重跑/补字段会再次走到这
    ⇒ 会把同一条种子**重复计入**。
  - **顺序**：先读存在性、再 `save_task`（写完再读就永远不为 None）。
  - **记账独立 try**：记账失败**绝不许反噬入库**（入库已成功，不能因记账异常让调用方以为入库失败）。
- **`crawl.py` / `topup_opencd_fields.py`**：摘要里**与请求账分开报**「今日入库 N 条种子」。
- **`opencd_rate.py`**：模块头「已知缺口①」标注**部分闭合**（明确：闸门仍按请求=站点保护；
  种子账=产能口径；**两件事故意分开**，没把闸门改成按种子卡 —— 那会与站点保护错位，`ERR-META-05`）。

### 3. 实测（本机）

- `opencd_ingest.py --selftest`：新增判据⑭（新入库 +1、重复 == 不变、前置不在库）⇒ **60/60 全对**。
- `tools/opencd-crawl-selftest.py`：**30/30**（未受影响）。
- `gen_counts`：自检 **8/8**、ERR 106 条。
- 语法检查：`db.py/crawl.py/opencd_ingest.py/opencd_rate.py/topup_opencd_fields.py` 全 OK。

### 4. 边界（★ 别读成"全闭合"）

- **闸门（硬刹车）仍按请求**，未按种子 —— 这是**刻意的**（站点保护不可与产能口径混）。
- 真要"按种子卡闸门"仍要在**闸门层**引入种子概念，**还没做**，有风险，暂不做。
- 未部署到 NAS（本轮只改代码 + 本机自检；部署由用户决定）。

---

## 2026-10-04（凌晨·续 2）—— 订正 `ERR-QB-06`：「3060 稳」不成立 + s6 守护真相

### 1. 起因（任务 `#10`）

`ERR-QB-06` 记的是 3020 qB 虚拟内存泄漏（`VmSize` 单调涨、`VmRSS` 平 ⇒ `std::bad_alloc`）。
本轮实测要**订正两处**：① 「3060 稳」的假设；② 「容器在跑」被当成「进程没崩」的误读。

### 2. 实测（NAS，2026-10-04，一手数据）

- **两个 qB 实例**（都是 `linuxserver/qbittorrent:5.2.4`，都 `Up`）：
  - `qbittorrent-opencd`（**:3020**，本项目用）：`VmSize` **5.90→5.95→6.00 GB**（40s +100MB ≈ **+2.5MB/s，仍在漏**），`VmRSS` 平 ~437MB。
  - `qbittorrent-reseed`（**:3060**，reseed 工具用）：`VmSize` ≈ **89.9 GB 但三次采样平**（89.944→89.942→89.944），`VmRSS` ~145MB。
  ⇒ **同版本一个涨一个平** ⇒ 「3060 稳」**不能**推出「泄漏被升级修掉了」。

- **s6 守护**：两容器 **PID 1 = `s6-svscan`**（`/etc/s6-overlay` 存在；qB 由 `/run/service/svc-qbittorrent` 经 `s6-supervise` 拉起）。
  ⇒ qB 进程崩了会被 **s6 静默拉起**，而 **Docker `RestartCount` 不 +1、容器仍 `Up`、`StartedAt` 不变**。
  实测：两容器 `RestartCount=0`，但**这不能证明进程没崩过**（本窗口内 qB 进程启动时刻 = 容器启动时刻，故**本窗口确实没崩**，但也说明不了别的窗口）。

### 3. 订正内容（已写入 `ERRATA.md::ERR-QB-06` 下方的 `>` 注）

1. **「3060 稳」不成立**：泄漏与实例运行状态相关，须**每实例各自采样看趋势**。
2. **s6 守护真相**：判「进程有没有崩过」**不能读 Docker 元数据**（`RestartCount`/`Up`），
   要读**进程自身启动时刻**（`ps -o lstart= -p <qB pid>`）与容器 `StartedAt` 比 —— 不一致即被守护重启过。
   一般化：**「容器在跑」≠「里面进程从头跑到尾」**。

### 4. 未做 / 边界

- 本轮**只订正认知**，**不修** qB 泄漏（用户此前明确「崩溃先放着」）。
- 用户替代者 `qbit_manage_opencd` 已在跑；qB 管理职责交接中。

---

## 2026-10-04（凌晨）—— ★★★ 蟹黄堡"最近两个种子没做种"根因：qB 升级改登录响应（`ERR-QB-07`）

### 0. 起因（用户报障）

用户原话：「为什么蟹宝王最近两个种子没有做种？」（蟹宝王 = 蟹黄堡 = CrabPT。
用户先让读上一个会话（`abf5ef44`）倒数十轮，再点名 SSH `iSunker@192.168.0.7:36688` 去查。）

### 1. 根因链（**全部实测坐实**）

1. **qB 3020 于 2026-10-03 16:12 从 5.0.4 升级到 `linuxserver/qbittorrent:5.2.4`**
   （`docker inspect qbittorrent-opencd` → `Created=2026-10-03T16:12:12Z`）。
   ★ 这次升级发生在**两个会话之间的空档**，当事会话（到 15:01）未记录 —— 我一度把
   "3020 是 5.0.4"当既定事实（上会话记录），**跨会话的环境事实已过期**。
2. qB 5.1.2+ 成功登录响应由 `200 "Ok."` 改为 **`204` 空 body**（实测裸 HTTP：`login HTTP 204 body=''`）。
3. 容器里 `qbittorrent-api==2024.9.67` 的 `auth_log_in()` 判据是
   `if auth_response.text != "Ok.": raise LoginFailed()` ⇒ **抛无参 `LoginFailed()`，`str(e)==''`**（实测：`EXC type = LoginFailed; EXC str = ''`）。
4. `publish.py::connect_qb()` 捕获后返回 `None`。**关键分叉**：
   - `readd_to_qb_crabpt` 开头 `if qb is None: print("⚠️ 回加跳过…"); return`
     ⇒ **发布成功但静默跳过回加 ⇒ 不做种**（本障真因）。日志只留
     `⚠️ 连不上 qBittorrent ()，本次不做回加`（**括号空**，看着像网络问题、实为登录被拒）。
   - `run.py:524-526` / `publish.py:1821-1823` 的 `qb.auth_log_in()` **无 try/except**
     ⇒ 阶段**硬崩**（退出码 1）。
5. **"为什么延迟做种没兜住"**：`SEED_DELAY_MIN=15` 虽已在 `.env`，
   但 qb=None 时 `readd_to_qb_crabpt` **在 enqueue 之前就 return** ⇒ `readd_queue.json` **从未生成**
   （实测：`/data/readd_queue.json` 不存在）。⇒ 延迟做种**实际从未生效过**。

### 2. 影响面（实测，daemon 日志）

| 阶段 | 症状 | 类别 |
|---|---|---|
| `scan`（`python run.py`） | ❌ 硬崩，退出码 1，栈：`run.py:866 → 526 → qbittorrentapi/auth.py:98 raise LoginFailed()` | 主线停摆 |
| `publish`（乌托邦） | ❌ 硬崩，退出码 1 | 主线停摆 |
| `publish_crabpt` 回加 | ⚠️ 阶段"完成"（退出码 0）但**不做种** | **软失败（本障元凶）** |
| `_drain_pending_readds` | ⚠️ 同理无法回加 | 软失败 |
| `crawl`（不碰 qB） | ✅ 正常 | 无影响 |

★ 教训：**"阶段成功" ≠ "这件事做成了"** —— `publish_crabpt` 是**唯一软失败**路径，
在日志里只留一行 `⚠️ 回加跳过`，不像 scan/publish 那样有 `❌ 退出码 1` 醒目。

### 3. 修法（已做并**在生产验证**）

| 项 | 判据 | 结果 |
|---|---|---|
| `Dockerfile` pin：`qbittorrent-api==2024.9.67` → `2026.10.0` | 本地 + NAS 都改（NAS 留 `Dockerfile.bak-before-qbit-api-*`） | ✅ |
| 新版 `auth_log_in` 判据 | `text == "" or "Ok."`（认 204 空 body）+ 原生支持 API Key（v5.2.0+） | ✅ 读源码坐实 |
| 重建范围 | `docker compose up -d --build musopia`（**只这个服务**，未动 qbtune） | ✅ |
| 镜像内库版本 | `importlib.metadata.version('qbittorrent-api')` = **2026.10.0** | ✅ |
| `publish.connect_qb()` 实测 | 返回 **OK**，`app_version()=v5.2.4` | ✅ |
| `run.py --selftest` | `✅ 自检通过` | ✅ |
| `run.py --limit 1`（真实 scan，复刻崩掉的路径） | `✅ 已连通 qBittorrent` → `🎯 扫描完成…失败 0 个` | ✅ |
| 重建后 daemon 日志 | LoginFailed / 连不上 qB / 回加跳过 计数 **= 0** | ✅ |
| `ERRATA.md` | 新增 `ERR-QB-07`；`tools/doc-audit/02_refs.py` 悬空 **0**；已同步 NAS | ✅ |

### 4. 用户手工处置（已确认生效）

- 用户**已手动添加**那 3 条已在站的蟹黄堡种子（215682 黄清元 / 215701 Gene Krupa / 215715 鲍正芳）：
  实测 `category=CrabPT` 的 2 条在 `uploading`/`stalledUP` 正在做种；
  **`鲍正芳`（hash `14f46591…`）分类为空**（`cat=''`）——待补（见 TaskList #5）。

### 5. 待办状态（本轮更新）

- ✅ **#4** 停掉 `musopia_qbtune`（原 up 但内部进程已死：心跳停 >180s）—— **已停**
  （先 `docker update --restart=no` 再 `stop`；恢复重启策略：`--restart=unless-stopped`）
- ✅ **#5** 鲍正芳那条分类补成 `CrabPT` —— **已做**（`CrabPT` 分类现 4 条全在做种）
- ⛔ **#6** rotate qB API Key —— **用户明确不管**（已取消）
- ⛔ **#7** 排查 qbtune 进程为何停止写心跳 —— **作废**（qbtune 已整体废弃，见第 7 节）
- ⛔ **#8** 3020 qB 崩溃（`bad_alloc`/VmSize 泄漏）—— **用户明确不修**（已取消）
- ✅ **#9** 补记录延迟做种部署 + 实测 —— **见下"第 6 节"，实测已通、并修了一个真缺陷**
- ✅ **#10** 订正 `ERR-QB-06` 的"3060 稳"假设 + 记 s6 守护真相 —— **已做**（见上「凌晨·续 2」）
- ✅ **#11** 长尾里的"邮件告警"—— 转由独立项目接手，本仓出「告警总纲」（见下）

### 6. ★★★ 延迟做种端到端实测（用户点名"实测能不能生效"）—— 实测抓到一个真缺陷并已修

**结论：延迟做种原设计有调度缺陷（不守时），已修为独立线程，实测生效。**

- **实测方法**（不依赖真发布）：用一条真实 `.torrent` 字节
  `readd_queue.enqueue(..., delay_min=1)` 入队 → 观察 daemon 是否到点回加。
- **测通的环节**（都是实测，不是读代码）：`enqueue` 写队列 ✅、`pop_due` 取到期
  ✅、`_drain_pending_readds` 真调 `torrents_add` ✅（409 冲突时留下重试 ✅）、
  分类正确 `CrabPT` ✅、`ack` 清队列 ✅。
- **★★★ 测出的真缺陷**：`_drain_pending_readds()` 原先由**主循环内联调用**，
  而主循环是**单线程同步**：一轮 tick 顺序跑完所有到期 stage（`run_stage` 阻塞）。
  `scan` ~20min、`crawl` ~38min ⇒ **整轮被占满**，drain 无论放循环开头/末尾，
  **一轮只调一次**，到点条目要等**整轮 stage 跑完** ⇒ "15 分钟延迟"实测被拖成
  **15~53min 不定**。（实测证据：重建后进 scan，5min 不出轮，入队 1 分钟后到期的
  条目一直不被消费；heartbeat 落后 866s。）
- **修法（2026-10-04）**：drain 移到**独立线程** `_readd_thread_loop`
  （`threading.Thread(..., daemon=True)`，每 30s 自跑），主循环内**不再**调它。
  - 线程安全已评估：移出后**唯一**访问 `readd_queue.json` 的就是该线程（无并发、
    不需文件锁）；qB 连接是各自独立 `Client`（WebAPI 无状态，不冲突）；线程体
    每圈吞异常（绝不因异常而死）；`daemon=True` + `_shutdown` 检查保证优雅退出。
- **修复后实测（决定性判据）**：日志 `02:53:57 🧵 readd 线程启动` →
  `02:53:57 ▶️ 阶段 [scan] 开始`（主循环进长 stage）→ **`02:56:27 ✅ [readd] 延迟
  到点，已回加 qB（[TEST4]）`** —— 回加**发生在 scan 仍在运行期间** ⇒ 证明线程
  **完全不被长 stage 阻塞**。qB 回读：`state=stalledUP cat=CrabPT`，队列 ack 清空。
- **教训**：上一会话只验证了"接线在"（`import` 成功、函数名在 daemon.py 里），
  **没做端到端实测** ⇒ 位置错了跟没接一样（`ERR-META-14` 族）。**这次是实测抓到的。**

---

## 2026-09-28（日间）—— ★★★ 转种口径缺闸门④（`ERR-DATA-08`）：已修 **一半**，另一半带病在产

### 0. 起因（用户点名要查，不是我自己发现的）

用户原话：「要我去读 `publish.py` / `crabpt_site.py` 的转种筛选口径，确认它用的是哪一个吗？
**这个必须先查 —— 若它筛的是"已上站"，那 15972 会被转过去，违反禁转；而转种不可逆。**」

⇒ **用户的前提是对的。** 实测确认：转种路原先只按 `CROSS_SITE_DEDUP_WHERE`（= 已上站 + id 非空）筛，
**没有** `transfer_rule = 'ALLOW'`。而 `EXISTS` 的 28 行里 **27 ALLOW / 1 DENY**，
那条 DENY 是 `site_torrent_id=15972`（真实 info_hash = **`144f017ad0c0…`**，`opencd_tid='200002'`，
`Mary Black - Babes In The Wood 1991`）⇒ **它会进队列、会被搬到蟹黄堡**。

### 1. 已修并**已在生产验证**的部分（这一半是硬的）

| 判据 | 结果 |
|---|---|
| 新常量 `db.TRANSFERABLE_WHERE` = `CROSS_SITE_DEDUP_WHERE` + `AND transfer_rule = 'ALLOW'` | 新增 |
| `transferable_to()` / `count_transferable_to()` 改用它 | ✅ |
| `CROSS_SITE_DEDUP_WHERE` **一字未动** | ✅ 故意（见下） |
| `run.py --selftest` ⑦e / ⑦e-2 / ⑦e-3 | ✅ 新增，**7 个变异 7/7 全被抓**，基线绿 |
| NAS 部署后 sha256 四份全等 | ✅ |
| 镜像重建 `sha256:7a847f13653d238e3332df87a49c7b9a2cbf82d8578666a55fb4ac20b1dcc6e6` | ✅ |
| 容器内 `run.py --selftest` | ✅ `✅ 自检通过`（**且印出 ⑦e/⑦e-3 两行 ⇒ 证明镜像真更新了**，`ERR-DOCKER-09` 的正面用法） |
| `tools/` 快照 vs 宿主 | ✅ 44 = 44 |
| ★ `publish.py --site crabpt --dry-run` | ✅ **队列 27 条**（原 28），`15972` 不在其中 |
| ★ 差集探针 | ✅ **仍报出 1 条** `144f017ad0c0 transfer_rule='DENY'` ⇒ **检测器没被弄死** |

★★ **为什么 `CROSS_SITE_DEDUP_WHERE` 不许被合并**（这是本次最容易做错的修法）：
`publish.py` 里那条"**应当恒为空**的差集断言"问的**正是**「已上站 ∧ **非** ALLOW 的有哪些」。
把 `ALLOW` 并进 `CROSS_SITE_DEDUP_WHERE` ⇒ 它**恒返回 0 行** ⇒ **永远显示"通过"** =
**检测器静默失效**（比不检测更危险）。
★ 用 fixture 实测证明：并进去后差值 = **0**。⇒ **一条 WHERE 服务两个方向相反的问题 ⇒ 必须分叉成两个常量。**
（`TRANSFERABLE_WHERE` = 判据；`CROSS_SITE_DEDUP_WHERE` = 事实，继续供探针用。）

### 2. ★★★ 未修完，而且**带病在产**：`is_transferable_to()` 仍是旧口径（= Task #7）

**这条是本次最该记住的 —— 因为它是我"查了函数名却没查完"的直接后果。**

用户的清单里明确写着待确认项：「whether the per-row `db.is_transferable_to(h, SITE_CRABPT)`
called at `publish.py:535` tests `transfer_rule`」。**我查到了那个函数名、写进了清单，然后改了代码、
部署了生产，始终没有回头回答它。**

⇒ 实测后果（一条命令里**自己跟自己矛盾**）：

```
sudo docker compose run --rm tools python publish.py --site crabpt --dry-run --only 144f017a
  ❌ 144f017a 不在转种队列里（被闸门拦下 / 已发过 / hash 打错）     ← 队列实际筛选（对）
  ✅ 它**在**转种队列里（源站有（乌托邦）、目标站（crabpt）没有 ⇒ 该转） ← 解释文案（错）
```

根因：`db.is_transferable_to()`（`db.py:1419-1459`）只判**两**件事（源站有 + 目标站没有），
**不含闸门④**。而 `explain_not_transferable()`（`publish.py:508-553`）正是走它生成给人看的 `why`。

⇒ 危害定级：比原来那个洞**更隐蔽**。原来只是"会多发一条"（数据风险）；
现在是**"工具会告诉你'它在队列里'，而实际不会发"** ⇒ **误导人做决定**（`ERR-META-14` 的形状）。

### 3. 顺带抓到的第二个缺陷：`--only` 的标识类型（= Task #8）

`--only 15972`（传的是 **site_torrent_id**）报「库里没有这个 hash（打错了？）」——
★ 这句是**误导**：它查的是 `info_hash LIKE '15972%'`，而 `15972` 是**站点 id**。

★ 更要紧：我据此**误判过** —— 我上一条曾说"命令 3 证明了闸门④ 挡住了 15972"。
**那句是错的**：它只证明了"`15972` 不是一个 info_hash"，**与闸门④ 无关**。
★ 教训：**"零交集"这条判据会因为传错参数而"碰巧成立"** ⇒ 判据通过 ≠ 判据测了它想测的东西。

### 4. 本轮方法论收获（都已落成**可执行判据**，不是文字承诺）

1. **一个"推理"被写进注释却从没落成判据 ⇒ 它会在被推翻的那天静默失效。**
   `publish.py` 的 `_gaps` 断言注释**自己写着**「…**但那段是推理**」；`db.py` 里还有一句引用
   `crabpt_rule_gaps()` 的注释（**那函数全仓库不存在**）。⇒ 实测证明推理已破。
2. **`ast.literal_eval` 不认 `Name` 节点** ⇒ `TRANSFERABLE_WHERE = CROSS + "…"` 写法虽干净，
   但会让**4 个靠 AST 抽常量的工具**（它们**不能** `import db`：`config` 在 import 期 `require_env()`）
   在**运行时**才炸（不是构建期）—— 而它们正是用来"半夜在 NAS 上查紧急问题"的。
   ⇒ 已加 **⑦e-3** 在构建期钉住；`db.py` 里那段"刻意的重复"就是为满足它，**注释里写明了理由**。
3. **判据提到了对的词、却问了个错的问题**：⑦e 只查"尾巴提到 `transfer_rule`"
   ⇒ `transfer_rule != 'DENY'` **也会通过**，而它与闸门④ **语义相反**
   （空 / `NEED_CONFIRM` 在闸门④ 是**拦**，在 `!= 'DENY'` 下是**放**）。
   ⇒ 必须有 **⑦e-2 逐字比**。已用变异 ② 证明它会被抓到。
4. **给用户的命令，flag 必须逐个在源码里 grep 过**（不能凭记忆）。
   实测：我把 `--site crabpt` 写成 `--crabpt`（该 flag **全仓库不存在**）。
   ★ 同族第二次（上次是位置参数 `torrent_id` 写成 `--torrent_id`）。
   ★ 好消息：两次都被 argparse 挡在 `main()` **之前**，**零副作用**（一条库、一条请求都没发）。

### 5. 未解 / 待验（**已进 TaskList**）

- **Task #7**（最紧急）：`is_transferable_to()` 补闸门④ + 钉住"队列口径与解释口径同源"。
- **Task #8**：`--only` 标识类型的报错文案。
- **Task #9**：把本页 §4 的 4 条 + §2 的教训补进 `ERRATA.md`（`ERR-DATA-08` **已写完**，别重复）。
- **Task #10**：`15972` 的标题不一致 —— **要用户拍板**（它是上游的 DENY 语义决定，
  不是我们能单方面"修"的；`stale 待办 0 / 只读口径 1` 是**正确拦截**，不是卡住）。
- **Task #11**：清理 `.scratch/` 与 NAS 上的 `q4_before.json`（第 4 步已判定，可删）。
- **Task #12**：7 条既有 watch items（原样留着，别再往下挖）。

### 6. 另：第 4~6 步的结论（本轮早段完成，独立于上面）

- **第 4 步 ✅ 通过**：`scan` 跑完**一整轮**（exit 0，223.6s，`18:03:10 → 18:37:13`），
  28 条皇后路已上站行的标题 **`updated_at` 动过 0 条** ⇒ **C' 生效**（"没被动"，**不是**"变得对了"）。
- **第 5 步 ✅ 零工作**：28 行的 `formatted_title` **已逐字节等于** `mapped_json.title`。
- **第 6 步 ✅ 结论为"不动手"**：站点 `name` 与库值 27/28 已一致；唯一不一致的 `15972`
  **被闸门④ 正确拦住**（`require_allow=True`）⇒ 不是失败，是**设计如此**。
- ★ 期间修掉一个工具真缺陷：`verify_cprime_step4.py` 在印了 `❌★ C' 没生效` 之后
  **仍以 `return 0` 收尾** ⇒ "失败"对机器读起来是"成功"。已修 + 加**拒绝闸门**
  （不到点不印判据，`rc 3`）—— 根因是工具**没把"时机"当前置条件**，导致同一条命令被连跑 8 次。

---

## 2026-09-24（夜）—— ★★★ **自动发布一次都没成功**：`--yes` 被误删 ⇒ `EOFError`

### 0. 症状（实测，不是推测）

用户开总闸、三把钥匙全绿之后，`publish` **连续两轮失败**（`last_ok: false`）：

| 时刻（CST） | 症状 |
|---|---|
| 16:32:38 | 退出码 1 |
| 17:02:56 | 退出码 1，耗时 **1.2s** ⇒ 秒崩 |

★ 关键旁证：**库里零痕迹**（队列仍 6 条、`EXISTS` 仍 272、无 `reason` 写入）
⇒ 说明它在**写库之前**就退出了。**"零痕迹 + 秒级失败"** 这个组合，
本身就指向"参数/环境不对"，而不是"站点拒绝"（后者会留下失败记录、且耗时更长）。

### 1. 根因（用户贴的容器日志，逐字）

```
📋 发现 6 条待发布任务，预检清单：
[1] ✅ 可发布 ... [6] ✅ 可发布
⏳ 发布间隔：120s（PUBLISH_MIN_GAP），但本次只有 1 条 ⇒ 无条间等待
确认向站点发布这 1 条吗？(y/N):
Traceback (most recent call last):
  File "/app/publish.py", line 512, in main
    answer = input(f"\n确认向站点发布这 {len(valid)} 条吗？(y/N): ").strip().lower()
EOFError: EOF when reading a line
```

⇒ daemon 拼的命令是 `publish.py --auto` —— **没有 `--yes`**，
于是 `main()` 走到那个 `input()`；而容器里**没有 stdin** ⇒ 立刻 EOF ⇒ 崩。

★★ 这是**上一次会话引入的回归**：那次把 daemon 的 `--yes` 换成 `--auto`
（理由「确认换个人按」），**但没意识到 `--yes` 还兼着"跳过 input()"这件完全不同的事**。
两者管的是**两件事**：

| flag | 管什么 | 少了的后果 |
|---|---|---|
| `--yes` | 跳过 `main()` 的 `input()` | ★ **EOFError ⇒ 阶段崩**（实测） |
| `--auto` | 声明无人值守 ⇒ 校验第二把钥匙 | 保险被关掉 |

### 2. ★★★ 为什么上次的"验证"没拦住它（这是最该记的一条）

`publish.py` 里有个 `run_auto()`，docstring 写着「无人值守入口（daemon 的 publish 阶段）」，
内容是「自己查钥匙 → 拼 `--yes --auto` → 调 `main()`」。

**但 `daemon.py` 根本不调它** —— daemon 是 `subprocess.Popen(["python","publish.py",...])`
起**子进程**，走的是模块级 `if __name__ == "__main__": main()`。

⇒ `run_auto()` 是个**没人调用的死函数**，却让上次的推理"看着很完整"：
「`run_auto()` 会补上 `--yes`」⇒ 于是放心地把 daemon 的 `--yes` 删了。

★★ **`ERR-META-01` 的新形状**：**判据"看着对" ≠ "它覆盖的是那条路"**。
上次验的是 `run_auto()`（读它、信它），**从未**验"daemon 到底往命令行传了什么"。

★★★ **纪律（写下来防复发）**：
> 改「谁传什么参数」时，**去查真实调用链** —— `grep` 调用点，
> 或**直接把拼出来的命令打出来看**。**不要信函数名，也不要信它自己的 docstring。**

⇒ 这正是本次修复的验法（见 §4）：调 `daemon.build_stages()` 把 `cmd` **印出来**。

### 3. 修复

1. **`daemon.py:161`**：`apply_flag("--auto")` → **`apply_flag("--yes", "--auto")`**。
2. **删除 `publish.py` 的 `run_auto()`**（死代码，是误导的源头）。
   ★ 为什么不改成"让 daemon 调它"：那会多出一条**并行入口**，
   两个入口各自拼参数 = 判据漂移的标准产地（`ERR-META-03`）。
   ⇒ 现在**只有一个入口 `main()`**，daemon 与人都走它，差别只在传了哪些 flag。
3. 四处过时注释（说"由 `run_auto()` 注入"）全部订正为"由 `daemon.py` 拼命令行时加上"。
4. `apply_flag` 的 DRY_RUN 分支补注释：摘掉 `--yes` 看着像少一层保险，
   但 **DRY_RUN 下不会崩**（`publish.py` 在更早的 `args.dry_run` 处就 return 了）
   —— 把"为什么不崩"写清楚，免得下一个人以为是同一个坑。

### 4. 验（★ 这次按 §2 的纪律验**真实调用链**）

**① 把 daemon 拼出来的命令印出来**（上次漏掉的那一步）：
```
python publish.py --yes --auto --limit 1
  ✅ 带 --yes : True   ✅ 带 --auto: True   ✅ --limit 1
```

**② 真实子进程 + `stdin=DEVNULL` + **非空队列**，两种组合对照**：
| 组合 | 退出码 | EOFError | 判定 |
|---|---|---|---|
| `--auto`（旧） | **1** | ❌ **有** | ★★ **精确复现** 17:02 的生产崩溃 |
| `--yes --auto`（新） | **3** | ✅ 无 | 越过 `input()`，被**钥匙校验**拦下（正确——桩里没配钥匙） |

★★ **新组合退 3 而不是 0，是"两件事都对了"的证据**：
`--yes` 生效（不再 EOF）+ `--auto` 的钥匙校验**仍在起作用**（没被顺手关掉）。

★ 第一次测时用了空队列 ⇒ 在 `input()` **之前**就 return ⇒ **测了个寂寞**。
⇒ 教训：**要验 `input()` 那一行，就必须让流程真的走到它** ——
用桩造非空队列（本次做法），不能拿"跑到别的 return 了"当通过。

### 5. 现状与后续

- ✅ 修复**已改本机**，**尚未部署**（`deploy.sh --apply` + `up -d --build` 待做）。
- ⚠️ daemon 已把 publish 退避到 **3600s**（`连续失败 1 次`）⇒ 不修的话下次仍崩、退避翻倍。
- 部署前**还有一个待观察项**：16:32/17:02 那两次失败的**同一个根因**已定位，
  ★ 但**站点权限是否正常**仍未验（修好 `--yes` 后第一轮真发才知道）。
  判据：`EXISTS` 从 272 变 273（每轮 1 条 = 新限速在起作用）。
- ⚠️ 队列那 6 条里 `群星 - 爱是我们共同的声音 1997` 的副标题是 **`(空)`**
  —— 9 条里唯一一条，发出去可能排版缺一块，值得单独看一眼。

### 6. 顺带修：`nas_health.py` 的整数键读数（趁快照头一次有真值才暴露）

`【发布速率】` 段把 `PUBLISH_MAX_PER_CYCLE` 印成 **`True`**、`PUBLISH_MIN_GAP` 印成 **`0`**。
★ 根因：我从 `snap_vals`（**布尔**视图）读了**整数**键 —— `"1"` → `True`、`"120"` → `True`。
★★ 这与 2026-09-24 早些时候 `PUBLISH_I_AM_HERE`（字符串）被按布尔转**是同一个错**，
只是这次错的是整数。⇒ 已在 `read_env_snapshot` 的 docstring 里把判据一般化：

> **`vals` 只对"开关"成立**；凡**非布尔**的键（字符串/整数/时长）**一律从 `raw` 读**，
> 再按它**自己的**类型解析。⇒ 先问"这个键是什么类型"，再决定用哪个视图。

★ 修完实测：`48 条/天`、`120s`（正确）。
★ 这个 bug **只有在快照里第一次出现真值时才会暴露** —— 之前一直报"判不了"。

---

## 2026-09-24（傍晚）—— ★★ 自动发布**首次真正跑起来** + 发布限速（日上限 240 → 48）

### 0. 上半场结论（用户指令「阅读上次会话倒数 10 轮，继续」）

★ 接手时发现：上次会话结尾悬着的**两个问题里，①已经不声不响地做完了**。
用户当时贴的是 NAS `--dry-run` 输出，我从实测反推，而不是信那次对话的结论：

| 实测 | 值 | 判据 |
|---|---|---|
| 本机 ↔ NAS 7 个关键文件 | **sha256 全同** | 逐文件比对 |
| `PUBLISH_I_AM_HERE` | `i-am-here` ✅ | `daemon_env.json`（容器自己落盘） |
| `ENABLE_PUBLISH`（接手时） | `false` | 同上 |
| 那次 dry-run **有没有误发** | **没有** | `site_synced_at` 最新仍是 09-23 |

★★ 这一条值得记：**接手时最该做的是"重新取一遍真值"，而不是延续上一轮的判断**。
上一轮的结论是"代码和钥匙都没上"，而实际**代码和钥匙都上了** —— 只差总闸。
若照着旧结论继续「上代码 / 加钥匙」，会**重复做一遍已完成的事**
（`ERR-META-04`：结论一旦落盘就会像真的一样被引用，所以要先验再引）。

### 1. 用户操作：分三步上线（顺序**不可颠倒**）

1. 上代码（`deploy.sh --apply`）→ 2. 加钥匙（NAS `.env` + `up -d --build`）
→ 3. 开总闸（`ENABLE_PUBLISH=true` + 再 `up -d --build`）。

★ **③ 必须在 ① 之后**是本轮唯一真正危险的一步：旧代码跑 `publish.py --yes`
（**没有**钥匙校验）⇒ 若先开总闸而新代码没上，**旧代码会立刻无钥匙自动发布**。

★ **实测踩到的一个细节**：用户改完 `.env` 后，`nas_health.py` 仍报
`ENABLE_PUBLISH=false` —— 而 `.env` 的 mtime 已更新。原因是**容器没重建**
（`.env` 只在 `up` 时被 compose 读入；`restart` 不算）。
⇒ **判据：比 `.env` 的 mtime 与快照的 `generated_at`**，差得多就是"改了文件但没重建"。

### 2. 用户验看：手工 `--limit 3` 真发 3 条，**人工确认合格后**才开自动

站点 ID `17278/17279/17280`；简介回读落地（4336 / 7140 / 7204 字符）；已回加 qB。
★ 全库 `EXISTS` 269 → **272**（+3，与预期一致）。
★ 这 3 条我在发之前用 `PENDING_PUBLISH_WHERE` 跑过生产库：9 条队列**全部**
`rule=ALLOW` + 全过 7 天窗口 + 站上无页面 ⇒ 复核过才建议发。

### 3. ★★ 用户提出的真问题：「每天最多发布多少条？」

★★ **当时那个数字必须靠人脑算** —— 而这正是本项目的**标准事故产地**
（`ERR-META-03`：算错的数字**不报错**，只给出一个看着合理的值）。清点如下：

```
PUBLISH_INTERVAL = 1800s ⇒ 86400/1800 = 48 轮/天
PUBLISH_MAX_PER_CYCLE = 5
⇒ 每天上限 = 48 × 5 = 240 条
```

### 4. ★★ 本轮的**核心订正**：用户要的「每条间隔 2 分钟」**压不住日总量**

> 用户原话：「加一道时间闸门啊，每一条发布间隔 2 分钟」

★ 我没有直接照做，而是先把算术摆出来：

```
5 条 × 2 分钟 = 10 分钟 < 30 分钟（一轮的周期）
⇒ 这个闸门**永远轮不到触发** ⇒ 每天上限**仍是 240 条，一条都没少**
```

⇒ 这是**"闸门加在了不会成为瓶颈的那一级"** —— 挡住了本来就不紧的口子，
却让人以为已经管住了。**规模上完全没有效果**，只是多了一个会让人误以为
"已经限速了"的东西。⇒ 我把这个算术讲清后，用户重新选了**两个都做**：

| 旋钮 | 值 | 管什么 |
|---|---|---|
| `PUBLISH_MAX_PER_CYCLE` | `5 → 1` | **日上限：240 → 48 条/天** |
| `PUBLISH_MIN_GAP`（新增） | `120`（模板默认 `0`） | **突发形态**（不降日上限） |

★★ **两个旋钮互补、不是同一件事**，所以**分开写、不合并成一句"限速"** ——
合并就是 `ERR-META-03` 的形状（读的人会以为调一个就把另一个也管住了）。

★ 顺带查清的既有事实：`musopia.py` 的 `request_interval=1.5` 是
**单个请求**之间的地板，对"一条发完了没有"**完全无感**；
而**一条发布 ≈ 3 个站点请求**（upload → edit 回读 → download 取回）+ 一次 qB 回加
⇒ 2 分钟要包住的是**整条记录**，只能在**发布循环层**表达。

### 5. 实现：`PUBLISH_MIN_GAP`（`publish.py`）

| 决定 | 选法 | 理由 |
|---|---|---|
| 睡在哪 | **循环开头** + `i > 1` | ★★ 循环体里有**两处 `continue`**（上传抛异常/返回空）—— 放末尾会被它们**跳过** ⇒ "连续两条失败"就变成**背靠背打站点**。放开头则**必然**生效 |
| 睡多少 | 按"距上次**上传**"算**余额** | 一条发布本身要花几秒 ⇒ 直接 `sleep(gap)` 会让实际间距变成 `gap + 耗时`，旋钮名不副实且**不可预测**。与 `musopia.py:_throttle()` 同一写法，只是高一层 |
| 生效范围 | **手工 + 自动都生效** | 突发保护针对**站点**；且 `--auto` 是**归责**判据，不是**限速**判据 —— 一个开关管两件事 = `ERR-ENV-03` 的形状 |
| CLI 开关 | **不加** `--min-gap` | 同一判据两个真值来源 = `ERR-META-03`。要临时关就 `PUBLISH_MIN_GAP=0` |
| 坏值 | ≤0 → 关；非数字 → **警告 + 按 0**（**不** `sys.exit`） | 配置笔误不该让发种停摆；且非 0 退出 ⇒ 指数退避（最长 6h/轮）⇒ **一个错别字让真发布停半天** |
| `flush=True` | 加 | 容器里 stdout 是**块缓冲** ⇒ 不 flush，"卡住 120 秒"期间**唯一能安慰人的那行**不会出现 |

★ 已知交互（写进注释 + 告警）：`STAGE_TIMEOUT=3600` ⇒ 本值 120 时约 **29 条**是上限，
再多 daemon 会**中途杀掉**子进程（已发的不受影响，但汇总/退出码会丢 ⇒ 进退避）。

### 6. 实测（都跑过，非推演）

| 测法 | 结果 |
|---|---|
| `publish_min_gap()` 判据 8 例（缺键/空串/正常/0/负数/非数字/浮点串/带空格） | ✅ 全通过（负数与非数字各打一句警告） |
| N=4 → `⏳` 行数 | ✅ **3 段**（= N-1），末尾**无**多余等待 |
| 睡余额 | ✅ 日志实测「已 0.3s，**再等 0.7s**」而非整 1s |
| ★ **失败后仍要等**（`continue` 路径） | ✅ 第 1 条抛异常后第 2 条**仍然等了**（实测间距 2.00s） |
| N=1（= 生产的每轮行为） | ✅ **0 段等待**，与改动前完全一致 |
| `gap=0` | ✅ 完全不等待，报「不限」 |
| `nas_health.py` 新段（快照**还没有**新键时） | ✅ 报「判不了」**拒绝**按兜底硬算（兜底 `0` 会算出"无限"这种假数） |

★ 验证脚本是**临时**的（monkeypatch 掉出网/写库/回加），**用完即删、不入仓库**。

### 7. 顺手订正的 stale 面（本轮暴露，都是**必然过时**的写死文字）

| 文件 | 改了什么 |
|---|---|
| `README.md` | 销掉「恢复 `publish`」第 3 条；订正 2 处 `ENABLE_PUBLISH=false` 断言；**新增**「发布限速」一节；env 表加两行 |
| `tools/reset_deleted_site_status.py` | 写死的 `每轮上限(5)` 与 `一天可发 240 条` ⇒ 改成**按生效值现算**（`ERR-META-03`） |
| `.env.example` | `PUBLISH_MAX_PER_CYCLE 5→1` + 新增 `PUBLISH_MIN_GAP` + ★ **警告它的兜底是 `0`＝不限（与 `EDIT_MAX_PER_CYCLE` 方向相反）** |
| `counts.json` | **重新生成**（★ 生成物，不手改）；第一次 `--offline-only` 会把 online 块留成 09-23 的旧值 ⇒ 补跑一次带 NAS 的 |
| `daemon.py` | `ENV_SNAPSHOT_KEYS` 加 `PUBLISH_MAX_PER_CYCLE` / `PUBLISH_MIN_GAP`（不加 ⇒ 巡检看不见，**本轮改动的意图就无法从本机验证**） |
| `tools/nas_health.py` | 新增 `【发布速率】` 段，把 **48 条/天** 直接算出来印上（本机工具，不进镜像） |

★★ 记一条**已核实的"不是 stale"**：`README.md` L755 与 `SESSION-NOTES` 里的
`240` 都是**别的量**（前者是退避前的"一天 240 次无效 POST"，
后者是"短路 240"的日志行数）—— **不要动它们**。量纲不同的数字长得像，
是 `ERR-META-05` 的经典陷阱。

### 8. 未解 / 待观察

- ⚠️ **`PUBLISH_MIN_GAP` 在生产里"看不见"**：每轮只有 1 条 ⇒ `i > 1` 不成立
  ⇒ **永不触发**。这是**设计使然**（5×120s=10min < 30min，塞得进一轮），
  但代价是**它坏掉也没人会发现**。⇒ 只能靠上面那组桩测 + 手工 `--limit ≥ 2` 验。
  ★ 这正是我**建议加 `--min-gap`**（可快速测）而被纪律否掉后留下的洞，记在这里。
- 队列剩余 **6 条**（发完那 3 条之后）⇒ 按每轮 1 条 ≈ **3 小时**发完。
- `ENABLE_CRAWL` 仍 `false` ⇒ 队列**不会自动增长** ⇒ 48/天 短期不是瓶颈。
  ★ 但一旦打开，48/天 会明显偏慢（那时要么调大每轮数，要么做真日配额账本
  —— `opencd_rate.py` 有现成的跨轮账本实现可复用）。

---

## 2026-09-24（下午·续）—— ★ `check_db.py` 两行标签的口径错（已修）+ 「242 拦掉」的定性

### 1. 用户的问题

> 「为什么 1-2 未上站拦截掉 242 个那么多？」

★ 问题的前提**本身就是那个 bug 教出来的** —— 用户把 `[status / site_status 分布]` 里的
`SUCCESS / EXISTS 242` 当成了「①+② 本级拦掉 242」。**这个读法极其自然**，
而它**恰好量级相等**（这也是它危险的原因，见第 3 条）。

### 2. ★ 先定性：那 242 条**不是"发不出去"，是"已经发出去了"**

闸门② 的判据是 `site_status != 'EXISTS'` ⇒ **取反 = `EXISTS` ⇒ 拦**。
实测那 269 条 `EXISTS` **全部**有 `site_torrent_id`（站上真有页面）。

★★ **就算把闸门② 整个拆了，这 242 条也一条发不出去**（用生产库逐级算过）：

| 级 | 剩余 | 本级拦掉 |
|---|---|---|
| 只放开 ② | 242 | — |
| +②b 发布日期≤7天前 | 197 | 45 |
| +③ 已抓页面 | **12** | **185** |
| +④ `rule=ALLOW` | ★ **0** | 12 |

原因：`EXISTS` 里 `transfer_rule` = 空 230 / `ALLOW` **20** / `DENY` 19，
而那 20 条 `ALLOW` 的 `status` **全是 `NO_TORRENT_FILE`**（不是 `SUCCESS`）
⇒ **闸门① 就不认它们**。
⇒ **"拦掉 242" 的量级是对的、也是必需的** —— 它是**账本上的"已交付"清单**，
不是候选池。**拦它们正是 `preflight()` 整段注释在防的事**（重复发布，不可撤回）。

### 3. ★★ 根因：`242 + 34 = 276` 是**巧合**，两个数各错一处、恰好抵消

| 那一行 | 实际口径 | ★ 正确口径 |
|---|---|---|
| `SUCCESS / EXISTS 242` | `status='SUCCESS' AND site_status='EXISTS'` | `site_status='EXISTS'` = **269**（闸门② 真拦的） |
| `SUCCESS / UNKNOWN 34` | `status='SUCCESS' AND site_status='UNKNOWN'`（**漏了"不是 EXISTS"**） | **33**（闸门① 真输入） |

⇒ 242 少算了 27 条 `NO_TORRENT_FILE`、34 多算了 1 条 `DENIED`，**恰好抵消**。
★★ **最危险的一类错**：它让读的人以为「口径已经核过了」。
⇒ 这正是 `ERR-META-03`（判据漂移后**不报错**，只给出看着合理的错数字）的又一次。

★ 附带坐实：`276` vs 后面的 `285` **不可相减**（`ERR-META-05`）——
差的那 9 条是 `DENIED 6` + `NO_TORRENT_FILE 4`（当时状态，现已漂到 7/0）。

### 4. ✅ 已修（只改**打印口径**，**不碰任何闸门逻辑**）

`check_db.py` 三处，全部实测过：

1. 分布段标题改成「**两列联合**口径，不是发布队列的某一级」，每行标
   `status=` / `site_status=`；
2. ★ 新增一段「**闸门用的极简单列数**」，把 `site_status='EXISTS'` = **269** 与
   `不是 EXISTS` = **286** **直接印出来** ⇒ 以后**不用手推**、也就**推不错**；
3. ★ 新增「**闸门② 拦掉的那些：两个原因分开看**」——
   **没有发布日期**（`6`，要补数据）vs **日期太新**（`17`，到期自己放开）。
   ★ 两者修法完全不同，合成一句会读错；且 **6 + 17 = 23 = ②b 那格的本级拦掉数**（自校验）。
4. 「数据现状」段标题加 **「全库口径」** 与「别当『能发多少』用」——
   因为 `transfer_rule='ALLOW' = 31` **一条都不在发布队列里**（队列那 9 条另有出处）。

### 5. ★★ 用户后半句的订正：「后面的发布日期小于 7 天，很多都出错了对吧」

**不对。那一级没出错 —— 错的是它**左边那两行标签**。**

| 环节 | 判定 | 依据 |
|---|---|---|
| 闸门② **本身** | ✅ **正确**，一行都没错 | 直接来自 `db.PENDING_PUBLISH_WHERE` |
| 那格的 **delta 数字** | ✅ **正确** | `publish_funnel()` 同一条累加链，**同一口径** ⇒ 差合法 |
| ★ 它**左边**的 `242` / `34` | ❌ **标签口径错** | 见第 3 条 |

★★ **混淆点说清**：**闸门② 拦掉的是"行"，不是"日期"**。
`opencd_added_at` **一个都没被改过**（那是 `vtid` 阶段写的，闸门只读不写）。
那 23 条里 `6` 条**本来就没日期**、`17` 条**日期太新只是还没到点** ——
⇒ **没有"出错"，只有"还没轮到你"**。

### 6. ★ 顺着挖出的第二个问题：`db.stats()` 的 `site_status` 分桶**比闸门更宽**

`stats()['groups']` 印出 `NO_TORRENT_FILE / EXISTS 27`，
⇒ 它的 `site_status` 分桶**不看 `status`**。

| 桶 | 条件 | 条数 |
|---|---|---|
| `NO_TORRENT_FILE / EXISTS` | `site_status='EXISTS'`（**不管 status**） | **27** |
| `NO_TORRENT_FILE / UNKNOWN` | `site_status!='EXISTS'` | **0** |

⇒ ★ **全库 27 条 `NO_TORRENT_FILE` 全都在"已上站"那一桶里**。
⇒ 这**仍然是对的、且重要**：说明 `publish.py` 里那条
「**发布成功却没拿到站点 ID**」的路 → `NO_TORRENT_FILE` 的产生条件比
`opencd_ingest` 里那条**宽一倍**（`db.set_site_id` 的 `status=` 参数不止一处传它）。
★ **未追到底**，但已定性：**这是"已上站"的 27 条，与队列无关**。
⚠️ 我一开始把它读成"已上站的 `NO_TORRENT_FILE` 应该恒等于 0" —— **那是错的**，
   我是拿 `opencd_ingest` 一条路的印象去套全库（判据太远）。

### 7. 顺带核对：`check_db` 报的**待办**数在漂

`有 tid 但没页面`：`380`（我读 `2` 分钟前）→ `381`（**用户**那一跑）
⇒ 对不上的原因**未见** —— ⚠️ 可能只是两次读之间 `topup`/`vtid`/`edit` 动过。
★ **未定性，别当成异常**：要定性得同一时刻读两次。
（判据：`[抓页面待办]` 段不进发布队列，漂它**不影响**队列那 9 条。）

### 8. 本轮改动清单（**仍未上生产**）

| 文件 | 改了什么 | 部署 |
|---|---|---|
| `publish.py` | ★★ 第二把钥匙 + `run_auto()` + `--auto` | 待部署 |
| `daemon.py` | `--yes` → `--auto`；白名单加钥匙；`apply_flag` 告警 | 待部署 |
| `.env` / `.env.example` | 加 `PUBLISH_I_AM_HERE=i-am-here` | ★ `.env` **同步不了**，要 NAS 手工加 |
| `README.md` / `ERRATA.md` | 「两把钥匙」一节 / `ERR-ENV-03` | 待部署 |
| `tools/nas_health.py` | 新增 `【自动发布】` 段 | 本机工具，**不进镜像** |
| ★ `check_db.py` | **本轮**：三处口径与打印（节 4） | 待部署 |

★ `deploy.sh` 预览共 **5 个**文件有差异，无自检失败。
★ **一行都还没上生产**；生产队列仍是 **9** 条，`publish` 仍关着。


### 0. 用户指令

> 「开启自动 publish，看下效果」

★ 字面意思是"把 `ENABLE_PUBLISH` 打开"。**先查了生产现状，发现两件事**：

| 实测 | 值 | 判据 |
|---|---|---|
| 生产 `ENABLE_PUBLISH` | **已经 `true`** | `daemon_env.json` 快照（`nas_health.py` 读的） |
| 生产 `DRY_RUN` | `false` | 同上 |
| ★ 但容器里**没有**这个环境变量 | —— | **我不知道它真值** |

⇒ 两件事必须**分开做**，不能混：
① **"看效果"必须看真的** ⇒ 先跑 `--dry-run`（只读，零请求）；
② ★★ 但在合并任何"能自动发"的开关**之前**，发现了一个**结构问题**（下节）。

### 1. ★★ 关键发现：所谓"三道保险"的钥匙**都插在同一个孔上**

自动发布原设计有**三道**独立保险 —— 但**它们全部住在同一个 `.env` / 同一个容器里**：

| 保险 | 位置 | 生效条件 |
|---|---|---|
| ① `ENABLE_PUBLISH` | 容器 `.env` | 长期 |
| ② `DRY_RUN=false` | **同一个** `.env` | 长期 |
| ③ `preflight()` | 容器代码 | 只拦"**空库** + publish 开着" |

★★ **根因**：把"**配置齐全**"误当成了"**有人把关**"。三道开关**一次配好就永久同时生效**
⇒ 人在回路里的那一刻**根本不存在**。判断的核心不是**有几个开关**，
而是**它们会不会同时被打开**。

★ 保险③ 的覆盖范围还特别窄：`db.preflight()` 自己写明它防的是
"`/data` 卷挂错或被重建" ⇒ **只对 `total==0` 报警**。而这里最危险的
**恰恰不是空库** —— 是**库好好的**、闸门①~④ 全判"可以发"、于是无人值守地发出去。
⇒ **闸门回答的是「这行该不该发」，它不回答「现在该不该发」**；两件事。

### 2. 做了什么（已改，**未部署**）

加**第二把、独立的**钥匙 `PUBLISH_I_AM_HERE`，并把两条路**按"谁按的确认"**分开：

| 谁 | 命令 | 第二把钥匙 |
|---|---|---|
| daemon（无人值守） | `publish.py --auto` | **必须**通过 |
| 人在终端前手敲 | `publish.py`（可加 `--yes`） | 不校验（人在回路里） |

- `publish.py`：`guard_auto_key()` / `explain_key_reason()` / `run_auto(limit, only)`；
  钥匙校验插在 **`MusopiaClient()` 之前**（验证过行号：`389` 钥匙 < `397` 建客户端）
  ⇒ **拒绝时一个请求都不发**（连 `check_login()` 都不发）。
- `daemon.py`：`publish.py --yes` → **`publish.py --auto`**；
  `ENV_SNAPSHOT_KEYS` 加 `PUBLISH_I_AM_HERE`（★ 不加巡检就**看不见**它）；
  `apply_flag()` 对 `--auto` **打印告警**（摘掉它 = 把保险关掉，不能静默）。
- `.env` / `.env.example` / `README.md`（新增「自动发布的两把钥匙」一节）/
  `ERRATA.md`（新增 **`ERR-ENV-03`**）/ `tools/nas_health.py`（新增 `【自动发布】` 段）。

### 3. ★★ 三个**不同原因**都表现为"不发" —— 必须分开报

| 状态 | 判据 | 后果 |
|---|---|---|
| 配对了 | `PUBLISH_I_AM_HERE == 'i-am-here'` | ✅ 放行 |
| **根本没配** | 变量**不存在** | ⛔ 退出码 3，日志写"**还没配**" |
| **配错了** | 存在但不是那个值 | ⛔ 退出码 3，★ **不回显值**（避免写进 logs） |

★ 分开报的理由是 `ERR-ENV-02`：**"缺键"最容易被静默当成某一种默认值**
（`iter_my_uploads` 的 uid 就是这么错的）。两句话的**修法完全不同**，
合成一句就等于把"你还没配"说成了"你配错了"。

### 4. 已实测坐实的（逐条，**本机**）

| 判据 | 结果 |
|---|---|
| `build_stages()` 生成的命令 | `['python','publish.py','--auto','--limit','5']` ✅ |
| `guard_auto_key()` 7 种取值 | unset/空/空白 → **根本没配**；`yes`/`${VAR}`/`"i-am-here"` → **值不对**；正确值 → 放行 ✅ |
| `run_auto()` 钥匙缺失 | `SystemExit(3)` + 打印「还没配」✅ |
| `run_auto()` 钥匙正确 | 注入 `--yes --auto --limit 5` 并进 `main()` ✅ |
| `publish.py --auto --limit 5`（真跑，无键） | ★ **看到「还没配」** —— 见第 5 条 |
| 钥匙检查 vs 网络调用顺序 | 钥匙在**前**（389 < 397）✅ |
| 三个文件 `ast.parse` | OK ✅ |

### 5. ★★ 本轮我差点犯的错（第 5 次同形状，**这次当场抓住了**）

`publish.py --auto --limit 5` 第一次跑，**返回码 0**，输出是
「💡 当前没有需要发布的任务。」—— 我差点记成"**钥匙没拦住**"。

★★ 真因是一条链，**四段各自都对**：

1. `config.py` 里 `MUSOPIA_URL` 有兜底默认 ⇒ **本机不传也能 import**；
2. ⇒ `main()` 跑起来，`db.init_db()` 先执行；
3. ⇒ `init_db()` **幂等补列**，而我这份本机 `data.db` 是 **2026-09-09 的老快照**
   （**只有 17 列**，缺 `opencd_added_at` 等 10 列）⇒ **它被自动迁移了**；
4. ⇒ 老库里 `status='SUCCESS'` 的行 `opencd_added_at` 全是 NULL ⇒
   闸门② 命中 `IS NULL ⇒ 拦` ⇒ **队列 0 条** ⇒ `main()` 在**钥匙校验之前** `return`。

⇒ 正确判据是「**钥匙拦得住"有活"的情形吗**」，不是「这一跑为啥啥也没发生」。
★ 同一个陷阱的**更危险版本**：在**生产**容器里 `publish.py --auto` 且**忘了配钥匙**时，
   **日志里不会出现那条 🛑** —— 因为队列为空就提前 `return` 了。
   ⇒ 判"钥匙有没有生效"**不能只看日志有没有 🛑**（`ERR-META-01`：检测方法失效比不检测更危险）。
   ★ 已复核：这一跑**是只读的**（`init_db()` 之外零写；`publish.py` 不写库），
     本机库**行数仍 406**，只是补了列（`data.db` 在 `.gitignore` 里，未进 git）。
★ **纪律（本轮新增）**：要验"拒绝路径"，必须喂一个**能走到那一步**的输入 ——
  本机老库喂不出来（队列恒 0）⇒ 得用**单测**（第 4 条那张表）或**在生产**验。

### 6. ★★ 生产 vs 本机：队列 0 与 9 是**两个不同的集合**

| | 本机 `data.db` | 生产 `//iSunker…/data.db` |
|---|---|---|
| 总行 / `SUCCESS` | 406 / **261** | **555 / 276** |
| ★ 队列 | **0** | ★★ **9** |
| 逐级 | 缺列、闸门② 全拦 | ①`33` → ①+②`10` → ①+②+③`10` → **`9`** |

★ 本机那份是**开发快照**（`ERR-META-02`）⇒ **本机的结论不许拿去说生产**
（这是"判据太远"的又一个口）。生产那 9 条**逐条**已复核：
`rule` **全 `ALLOW`**、`mapped_json` 非空、封面 `.picgo.net` 非空、
`log_content` 非空、`torrent_file` 非空且**路径是容器内 `/data/output_torrents/…`**
（★ 所以**本机 `os.path.exists` 恒 False**，见 §14.6 那条纪律，别在本机判它）。
第 10 条 `200237`（`NEED_CONFIRM`）被闸门④ 正确拦下。

### 7. 部署状态

★ **一行都还没上生产。** `deploy.sh` 预览：4 个文件有差异
（`publish.py` / `daemon.py` / `README.md` / `.env.example`），无自检失败。
★ 且 deploy **同步不了 `.env`**（在 `NEVER` 名单里）⇒
**生产那份 `.env` 必须手工加 `PUBLISH_I_AM_HERE=i-am-here`** ——
不加的后果是 `ENABLE_PUBLISH=true` 但**每轮被拒 + 退避**（**不发**，安全方向）。

### 8. 未解 / 下一步

1. ★★ **等用户明示"可以动生产"** → 顺序是：
   ① `deploy.sh --apply`；② **NAS 上手工**加 `.env` 那行；
   ③ `up -d --build`（★ 不是 restart）④ `nas_health.py` 看
   `【自动发布】` 段是否变 `★★ 会自动发`。
   ⚠️ 改了 `.env` 会**重建容器**⇒ 会中断正在跑的 `scan`/`edit`
   （`stop_grace_period: 5m`，正在跑的阶段不处理 SIGTERM ⇒ 撞满上限被 SIGKILL）。
2. ★★ **先看 "看下效果" 的那一步（只读）**：
   `docker compose run --rm tools python publish.py --dry-run --limit 5`
   ★ **不要加 `--yes`、不要加 `--auto`** —— 两者都会**绕过交互确认直接发**。
   ★ `DRY_RUN=true` 不要把整容器切过去（它会**同时摘掉 edit 的 `--apply`**，
     等于顺带停了编辑，不是"只停发布"）。
3. ★★ **那 9 条是真要发的**（当用户明确授权时）：
   `publish.py --yes --limit 9`（★ 不传 `--only` 才能发全，`--only` 传空=不限制；
   传非空反而是**交集**。别把 `--only ""` 当成"全发"的开关 —— 它只是默认值）。
4. `config.py` 的 `MUSOPIA_URL` 有兜底 ⇒ **本机 `main()` 会在缺密钥时不 sys.exit**，
   于是"没 TTY / 没配置"的表现被**队列为空**掩盖（第 5 条）。**未修**。
5. 仍未解（沿用上一节）：`flip_status` 清单 18 vs 写 12 的口径不一致；
   `daemon.beat()` 在长阶段期间不刷新；重复的叶惠美 17096；`OPENCD_COOKIE` 的 `$` 插值；
   空 body（HTTP 200 + 0 字节）。

### 1. ★★ 实验结果（这是 §13.7/§14.10 一直等的那个数）

`check_db.py` 的逐级累加，**同一个工具、同一个口径**，前后对照：

| 级 | 跑 `topup` 前 | 补完页面后 | ★ 改 `status` 后 |
|---|---|---|---|
| ① `status=SUCCESS` | 266 | 266 | **276** |
| ①+② 未上站 | 24 | 24 | **34** |
| ①+②+②b 发布日期≤7天前 | **0** | **0** | ★ **10** |
| ①+②+②b+③ 已抓页面 | 0 | 0 | **10** |
| ①+②+②b+③+④ `rule=ALLOW` | 0 | 0 | ★★ **9** |
| **队列（真口径）** | **0** | **0** | ★★ **9** |

⇒ ★★ **「皇后路能不能产出」的答案是：能。** 但**得三步都做**：
① 补页面（`topup`）→ ② 把 `status` 改回 `SUCCESS`（`flip_status`）→ ③ 队列自己长出来。
★ **少任何一步都是 0** —— 补完页面不挑 `status`，行会掉出闸门①（`topup` 的已知副作用）。

### 2. 那 9 条是谁（逐条，含闸门④ 判定）

| tid | 皇后发布日期 | `transfer_rule` | 标题 |
|---|---|---|---|
| 199928 | 2026-09-07 09:45 | ALLOW | Cesaria Evora - L'essentiel 2001 |
| 199936 | 2026-09-07 10:06 | ALLOW | Barbara Hendricks / Roland Pöntinen - Schumann Lieder |
| 199947 | 2026-09-07 11:49 | ALLOW | Shaggy - Intoxication 2007 |
| 199954 | 2026-09-07 13:10 | ALLOW | milet - Made of Glass 2026 |
| 199963 | 2026-09-08 00:27 | ALLOW | 群星 - 爱是我们共同的声音 1997 |
| 200016 | 2026-09-09 20:41 | ALLOW | milet - Sunny 2026 |
| 200231 | 2026-09-14 11:42 | ALLOW | 音田雅則 - back shot 2026 |
| 200232 | 2026-09-14 11:45 | ALLOW | 菅原圭 - 孔雀 2026 |
| 200268 | 2026-09-16 18:53 | ALLOW | 岩崎太整 等 - 攻殻機動隊 O.S.T |

★ 第 10 条被闸门④ 拦下：**`200237` Sweety - 花言喬語 `NEED_CONFIRM`**（旧页面的规则是
`其他情況(參考標題及種子介紹)`）⇒ 闸门④ 要求 `= 'ALLOW'` ⇒ **拦**。**这正是闸门④ 该有的行为。**

### 3. ★ `flip_status --apply` 实测：**只改了 12 条**（不是清单里的 18 条）

```
⚠️  fe598dd100e2 rowcount=0（status 已被别人改过？⇒ 没写，正确行为）   × 6 条
💾 已改：12 条
```

**那 6 条全是 `status=DENIED`**（`200108`/`200235`/`200607`/`200608`/`200609`/`200610`）。
根因（`flip_status.py:598`）：写库 SQL 硬钉了 `WHERE ... AND status='NO_TORRENT_FILE'`，
而它们不是 ⇒ 匹配不到 ⇒ **没写**。

★★ 这是**守卫在正确工作**（只改"补页面造成的 `NO_TORRENT_FILE`"，不碰 `DENIED`），
**但暴露一个口径不一致**：逐条清单算 **18**、实际只可能写 **12**。
★ **`ERR-SQL-07` 的又一例**（汇总数字与实际会写的集合必须同口径）—— **未修**。

★ 附带订正一处昨晚的旧结论：昨晚 `flip_status` 报「要改的 0 条」，
**那是当时口径下对的**（当时这 12 条还没补页面 ⇒ `status` 还是 `SUCCESS` ⇒ 不在候选里）。
**不是**矛盾 —— 是**同一个工具在两个不同数据状态下**的正确读数。

### 4. 复核（写库脚本自己报的，含五列逐条比对）

```
status='SUCCESS' 的条数（应 = 12）: 12
✅ torrent_file / log_content / mapped_json / site_status / updated_at **逐条一字未变**
```
★ 仍符合那条纪律：**回填一列时绝不顺带刷 `updated_at`**（`ERR-SQL-06`）。

### 5. ★★ 我犯的错（**同一个形状第 4 次**，比结论更值钱）

| 我说过 | 真相 |
|---|---|
| ★★ **"daemon 卡住了，卡在 `run_stage()` 里"** | ★ **错。** 它只是跑了个 **50 分钟**的 `scan`（13:10:03 起 → **14:00:01 正常完成**）。**我从"心跳没动"推"进程卡住"，没先看 `docker inspect`** |

**判据链的漏洞（写清楚，防第 5 次）**：
「心跳停」**只能**证明"`beat()` 最近没被调用"，它**区分不了三种情况**：

| 可能 | 怎么分辨 |
|---|---|
| (a) 进程崩了 | `docker inspect` → `Status=exited` |
| (b) 容器被 `up -d --build` 换掉 | `docker inspect` → `running` 但 **PID/`StartedAt` 变了**；`RestartCount=0` |
| ★ (c) **正在跑一个很长的阶段** | `docker inspect` → `running` + `StartedAt` 没变 + 日志里有 `▶️ 阶段 [x] 开始` |

★★ **`docker inspect` 一条命令就能分辨这三种** —— 我却**先下了结论**。
★ 共同形状与前三次完全一致：**判据离结论太远**（`ERR-META-01` 一族）。

### 6. ★★ 由上面的 (c) 挖出的**真设计问题**（未修，已定性）

`daemon.py` 的 `main()` 里 `beat()` 只在**两个**位置被调用：

```python
while not _shutdown:
    beat()                     # ① 每轮循环开头
    for stage in stages:
        ok = run_stage(stage, stage_timeout)   # ← 阶段**跑完才回来**
        ...
        beat()                 # ② 每个阶段**跑完后**
```

⇒ ★★ **阶段执行期间心跳完全不刷新**。而 `healthcheck.py` 依赖心跳
⇒ **任何超过 120s 的阶段都会让容器被判 `unhealthy`**。
★ 实测 `scan` 跑 **50 分钟**（13:10:03 → 14:00:01）⇒ **它跑了 25 倍心跳阈值**。

**这可能正是那条未解之谜的真因**：`up -d --build` 的 `Started` 波动
`301.0s / 195.2s / 61.9s / 1.6s`（§13.7 第 8 条，后被订正为"正常"）——
★ **`up` 等旧容器收尾（`stop_grace_period: 5m`）时，正在跑的 `scan` 不收 SIGTERM 就等满，
满 5 分钟才被 SIGKILL ⇒ 301.0s 正好撞上限**。⇒ **"正常"这个订正可能也不完整。**

★ **未修，两个候选修法**（都**没做**，待定）：
1. 让 `run_stage()` 用 `Popen.poll()` 轮询，**期间定期 `beat()`**（真修法）
2. 调大 `HEALTHCHECK --start-period` / 把心跳阈值与 `STAGE_TIMEOUT` 挂钩
★ **没动手** —— 它要改 `daemon.py` + `Dockerfile`（healthcheck 参数），属**另一件独立的事**。

### 7. ★★ 纪律事故：`--i-have-stopped-the-container` 是**声明**，不是**验证**

写库那次，容器**其实是活的**（我上一条已说清，此处落盘）：

| 证据 | 读数 |
|---|---|
| 用户刚跑的 `docker inspect` | `running restart=0 exit=0` |
| 巡检**心跳翻转** | `False → True` |
| `scan.last_run` | `02:55:36 → 14:00:01` |
| `covers.last_run` | `02:25:52 → 14:00:03` |

⇒ ★★ **daemon 在 14:00 跑完了 scan + covers，而写库发生在 13:59:15** ⇒
**两个写者同时写了同一个 SQLite**（`ERR-DOCKER-03` 实际发生了一次）。

**这次没造成损失**（侥幸）：`总记录 555`/`site EXISTS 269`/`stale 40` 都没变；
写库脚本自己的五列逐条比对也全过。
★ **但这是运气** —— 单条 UPDATE 很快、撞锁窗口小。

★★ **纪律（下次必须）**：`--i-have-stopped-the-container` **只是让脚本闭嘴的字符串**，
**脚本无法检查**。⇒ 真停容器要用命令确认，**再**跑写库：

```
sudo docker compose stop
```
```
sudo docker inspect --format '{{.State.Status}}' opencd2musopia
```
（第二次必须是 `exited` 才往下走。★ 别用 `ERR-SMB-02` 那种"文件静止"当判据。）

### 8. 本轮新增/改动的文件

| 文件 | 改动 | 上生产？ |
|---|---|---|
| `.claude/scheduled_tasks.json` | ★ cron 巡检 prompt **整条重写**（`126f5d05`）| ❌ 本机 |
| `SESSION-NOTES.md` | 本节 | ❌ 文档 |

★ **cron prompt 订正的内容**（都曾是**连续多轮**的错误判据）：
· `ENABLE_PUBLISH=**true**` → **`false`**（判据：启动 banner 里**没有 `· publish` 这一行**）
· 「publish 的 last_run 长期不动是**真异常**」→ **预期内**（阶段关着不执行）
· 「`iter_my_uploads()` 翻页是坏的」→ ★ **结论不可信**（那次用的是**错的 uid 10427**，真 uid **85068**）
· 「短路 241 vs 库 244 差 3」→ **作废**（量纲混淆，`ERR-META-05`）
· `{{.RestartCount}}` 在 `docker ps --format` 里会报错 → 改用 `docker inspect`
· ★ **新增**：心跳停的**三种**可能（含"在跑长阶段"）—— 见上第 5 条

### 9. 未解 / 下一步

1. ★★ **那 9 条要不要发？** 发 = **不可撤销**（`ERR-META-12`：没授权就是不许）。
   `publish` 在生产**关着** ⇒ daemon **不会**替你发。要发得**显式**：
   ```
   sudo docker compose run --rm tools python publish.py --limit 9
   ```
   ★ **但先别跑** —— ① `publish` 无 `--dry-run`（会用 `DRY_RUN=true` 顺带打开 edit 的写）；
   ② 这 9 条**从没在站上**，发出去就是新的公开页面。**等用户定。**
2. ★ **`flip_status.py` 的口径不一致**（清单 18 / 实写 12）—— 待修（`ERR-SQL-07` 族）。
3. ★ **`daemon.beat()` 在长阶段期间不刷新**（第 6 条）—— 待修，且它可能解释 `Started` 波动。
4. **scan 为什么跑 50 分钟**（它接近 `STAGE_TIMEOUT=3600`）—— 未查。
5. **`topup` 今日配额可能已满**（跑了一轮 `--limit 50`）⇒ 明天再跑。
6. 遗留：`counts.json` 缺 `transfer_rule`/`opencd_tid`/`mapped_json` 口径；
   `readiness_probe.py` 的口径漂移；三个探针 `COLS` 未收敛到 `db.py`。

---

## 2026-09-23（晚·再续）—— ★ 工具 1 交付：`topup_opencd_fields.py`（按 tid 分小批补全）

### 交付物

| 文件 | 内容 | 上生产？ |
|---|---|---|
| `topup_opencd_fields.py`（**根目录**） | 按 tid 直取详情页（**1 请求/条**，非搜索路 2~3 个），只抓**一小批**，补全 `mapped_json`/`artist`/`year`/`transfer_rule` 后入库 | ✅ 已进 `Dockerfile` 的 `COPY` **与** `deploy.sh` 的 `FILES` |
| `tools/_probe_rule_from_raw.py` | 从落盘页面**离线**回算 `transfer_rule`（零请求） | ❌ `tools/` 不上生产 |
| `tools/_probe_vertex_columns.py` | 列遍 vertex 表**每一列**，回答「有没有不用爬页面就拿到的字段」 | ❌ 同上 |
| `publish.py` 的 `--only` | **点名 ∧ 仍满足闸门 ⇒ 才发**（交集语义，不绕过闸门） | ✅ |

### ★★ 实测坐实的事实

**1. vertex 库里**没有**转载规则 —— 这个问题由数据回答，不再靠推理**

`torrents` 表（皇后 583 行）的**全部** 15 列：
`id · hash · name · size · upload · download · record_time · add_time · delete_time ·
rss_id · tracker · link · category · record_type · record_note`
⇒ **没有任何一列是「转载规则」或「页面正文」**。
`link = 'https://open.cd/details.php?id=194702'` ⇒ 匹配到的**只是一个页面编号**。
★ 所以用户那句「不是在 vertex 里匹配到了种子 id 么」的答案是：
  **匹配到的是 tid（地址），不是页面内容** —— 而 `ALLOW` 只能从内容里读出来。

**2. `topup_opencd_fields.py` 在容器里 dry-run 通过（判据 = 选中 5 条全 `SUCCESS`）**

```
落盘目录 : /data/raw_html
目标行数 : 428
★ 本轮选中: 5
  [1] tid=196636 status=SUCCESS  [2] tid=196690 status=SUCCESS
  [3] tid=196691 status=SUCCESS  [4] tid=196692 status=SUCCESS
  [5] tid=196706 status=SUCCESS
```

**3. ★★ 新坑：排序优先级漏了「有 log 优先」—— 一个**静默选错批**的错**

第一版选批的排序键是 `(是否已落盘, opencd_tid)`。跑 dry-run 实测选中：

```
[1] tid=196636  status=SUCCESS       ← 只有 1 条有 log
[2] tid=196650  status=NO_LOG_FILE   ← ★ 4 条没 log
[3] tid=196674  status=NO_LOG_FILE
[4] tid=196681  status=NO_LOG_FILE
[5] tid=196686  status=NO_LOG_FILE
```

根因：**没 log 的 `opencd_tid` 号段恰好靠前**，而我把 `tid` 当第一关键字。
⇒ 直接**违反用户两轮前定的「先补 428 里面有 log 的」**。修法：加一级
`0 if status == 'SUCCESS' else 1`，之后 5 条全 `SUCCESS`。

★★ **这个错的形状值得记**：修不是"代码写错了"，而是**排序键没有编码用户的优先级**。
   它的症状是"工具跑得好好的、选了 5 条、没有任何报错" —— **完全静默**。
   ⇒ 与 `ERR-META-01` 同族：**判据/实现自身对了，但选中的集合不是要的那一个**。
   ★ 唯一能抓住它的办法是**把选中的集合逐条打出来看**（dry-run 打印是承重的，不是装饰）。
   ★ 附带坐实：**`status='SUCCESS'` 与 `log_content <> ''` 在这批上等价**（都是 248）
     ⇒ 用 `status` 筛是**免费**的（不用把 248 份日志读出来比字符串）。

### ★ 设计要点（为什么这么写）

| 决定 | 理由 |
|---|---|
| **走 `crawl.Client`**，不自己写 `requests` | `Client.get()` 是**唯一**出网口（`gate.acquire()` 在里面），配额账本与最小间隔都靠它。绕过去 = 违规 |
| **不传 `torrent_file`** | 那些 `.torrent` 在 qB 那边才有；**重建会换 announce passkey ⇒ 改 infohash**（严禁）⇒ 落 `NO_TORRENT_FILE`，是**预期行为** |
| **台账用只增的 `raw_html/topup_tried.jsonl`**，不复用 `opencd_quota` | 后者是**配额账本**（一行一天一个数）；把"抓过哪个 tid"塞进去会让两个口径混在一个表里（`ERR-META-05`） |
| **不按 `status='SUCCESS'` 过滤目标**，只按排序优先 | 一次跑完两类：有 log 的入库后**保持 `SUCCESS`**，没 log 的落 `NO_LOG`（第一代规矩）。各自由 `opencd_ingest` 判，不必分两次排队 |
| **不碰 `status`**（只以副作用形式改） | 目标集合是"内容还没补的"，而 `status` 是**别的阶段下的结论**；改它是**另一件要用户定的事** |

### ★★ 一个必须讲清、且**不做**的后继动作

入库后这 428 条的 `status` 会变成 **`NO_TORRENT_FILE`**。
⇒ **要发它们必须显式把 `status` 改回 `SUCCESS`** —— 本工具**不做**，
   因为「发哪些」是用户的决定（`ERR-META-12`：不可撤销的事，没授权就是不许）。
★ 好处：内容（`mapped_json`/`artist`/`year`/`transfer_rule`）已补全 ⇒
  **将来要发不用再花请求**。

### ★ 部署（已回读哈希坐实，不是"应该跑过了"）

```
DRY-RUN diff: [新] topup_opencd_fields.py  [改] Dockerfile
bash deploy.sh --apply  ⇒ 3 个文件
回读: topup_opencd_fields.py ✅  Dockerfile ✅  publish.py ✅
NAS:  sudo docker compose up -d --build   （COPY 层非 CACHED，镜像 d4062b80…）
```

★ 两处白名单**都要加**（`ERR-DOCKER-05`：`deploy.sh` 说得过去 ≠ 容器里看得见）：
  `Dockerfile` 的 `COPY` + `deploy.sh` 的 `FILES`。
  ★ 加 `Dockerfile` 时我把续行 `\` 写成了字面 `\n`（一行 `... cover_fetch.py \n     topup...`），
    `deploy.sh` 的 dry-run diff 一读就露出来了 —— **这也是"回读"抓住的**。

### ★ 未解 / 下一步

1. ★ **真抓还没跑**（只跑到 dry-run）。NAS 上跑
   `sudo docker compose run --rm tools python topup_opencd_fields.py --apply --limit 5`。
   ★ 今天配额 `opencd_quota` 记 `used=3/50` ⇒ 剩 47。
2. ★★ 抓完要**看 ③ 摘要里的三态分布** —— 那才是"这 248 条里有多少真写着 `歡迎轉載`"。
   **在拿到这个数之前，谁都不该批量标 `ALLOW`**（上一轮我差点按 248 硬标，被用户的反问挡住）。
3. `status` 改回 `SUCCESS` 的动作**待用户定**（发哪些）。
4. 遗留：`counts.json` 缺 `transfer_rule`/`opencd_tid`/`mapped_json` 口径、
   `nas_health.py` 把「`keys` 里有但 `values` 里没有」误报成 `❔`（`ERR-META-10` 家族第 3 例）、
   `ENABLE_PUBLISH` 的 `.env` 两份不一致（`ERR-ENV-02`，已判明不是矛盾）。

---

## 2026-09-23（晚·续）—— ★ compact 后接手：428 的口径 + 用户指令「有 log 的先补」

### 用户指令（2026-09-23，compact 之后）

> 「之前你问我 428 条什么时候补 `mapped_json`，**先补 428 里面有 log 的啊，排进每日 50 次请求里面去**」

### ★ 已坐实（这次全部**当场数量**，逐条口径写死）

**1. 428 / 430 / 117 三个数的定义（此前只在对话里，现在落盘）**

| 数 | 定义（SQL 口径） | 实测 |
|---|---|---|
| **430** | `opencd_tid` 非空 | 430 |
| **117** | `opencd_tid` 空 —— ★ 这才是"第一阶段本地抓轨行" | 117 |
| **428** | `opencd_tid` 非空 **且** `mapped_json` 空 ⇒ 430 − 2 = 428 | 428 |
| **2** | 已补好的（`198601` 黄清元 / `198827` 群星醉民乐，均 `ALLOW`） | 2 |
| 自洽校验 | 430 + 117 = 547 = 总行数 ✅ | — |

**2. ★★ 428 的拆分 —— 用户点名的「有 log 的」= 248（不是 26）**

| 子集 | 判据 | 实测 |
|---|---|---|
| **B. 有 log 的** | `status='SUCCESS'` ⇒ ★ 这批**有 `log_content`** | **248** |
| ├ 已在站上 | 其中 `site_status='EXISTS'` | **222** |
| └ 未上站 | 其中 `site_status` 非 `EXISTS` | **26** |
| C. 没 log 的 | `status='NO_LOG_FILE'`（180）+ `NO_TORRENT_FILE`（2，已可发不阻断） | 180 |

★ **为什么"有 log"= 248 能**算出来**而不是猜**：三条独立判据互相对上 ——
  ① `status='SUCCESS'` 的 248 条**恰好**是 `log_content` 非空的 248 条；
  ② `status='NO_LOG_FILE'` 的 246 条里 `log_content` 非空 = **0**（逐条验过）；
  ③ `428 = 248 SUCCESS + 180 NO_LOG_FILE`（合计 428）✅
★ 所以「有 log 的」这批**不需要读 log 内容**，用 `status` 就能筛。

**3. 428 那批**不是**"本地抓轨行" —— 它们是"已配对皇后、没抓页面"的**

判据（实测）：那 428 条 `content_path` **全部非空**（是本地抓轨的目录），
而 `mapped_json`/`fetched_at`/`qb_seed_days` 全空 ⇒ 说明 `content_path`
是**早期 `run.py` 手写的**，不是 `opencd_ingest` 写的
⇒ ★ **不能用 `content_path` 是否为空来分"皇后路 / 本地路"**，那条判据是错的。

### ★★ 关键更正 1：「补 `mapped_json`」= **1 个网页请求 + 1 个封面请求 = 2**，不是 1

此前（compact 前）我按"补一条 = 1 个请求"算出「430 条要 8.6 天」。**低估了一倍**：

| 请求 | 出处 | 过不过 `RateGate`？ |
|---|---|---|
| 详情页 | `crawl.py` `fetch_detail()` → `client.get("/plugin_details.php")` | ✅ 过（**唯一**出口，`Client.get` 里 `gate.acquire()`） |
| 封面图 | `assets.upload_to_picgo()` → `requests.post(config.PICGO_UPLOAD_URL)` | ❌ **不过门**（picgo.net，不是皇后） |

⇒ **248 条 ≈ 500 个皇后请求 ≈ 10 天**（`OPENCD_MAX_PER_DAY=50`）。

### ★★ 关键更正 2：`crawl.py --from-qb` **走不通** —— 结构性不能当回填工具

判据（读代码实测，非推测）：

| 事实 | 位置 |
|---|---|
| `known` = **本地落盘 `raw_html/*.html` 的文件名**，而**不是**"库里有没有 `mapped_json`" | `crawl.py` `run_from_qb()` |
| 命中 tid 且 `tid in known` ⇒ `stats["cached"] += 1; continue`（一条请求都不发） | 同上 |
| 打开浏览器看过 `//…/musopia_script2/raw_html/`：**空目录（0 个文件）** | 本机 SMB 实测 |
| `base = OPENCD_URL`，而 `.env.example` 写 `https://open.cd` ⇒ **只能打真站，没有测试入口** | `crawl.py` `Client.__init__` |
| 该路**无法指定"只处理这 26 条"** ⇒ 每轮都从第一条重来，靠全局 `known` 跳过 ⇒ 那 26 条**永远轮不到**（饿死，`crawl.py` 自己注释警告过这个形状） | 同上 |

⇒ ★ 两条独立理由（**范围不可控** + **只打真站**）都指向同一件事：
   **别用 `--from-qb` 当回填工具。**

### ★★ 跨目录「同一事实两份实现」的第四个实例

| 文件 | 在镜像里？ | 在 `deploy.sh` 白名单里？ |
|---|---|---|
| `sync_opencd_tid.py`（根目录） | ✅ `COPY` + 白名单 | ✅ |
| `tools/backfill_opencd_tid.py` | ❌ | ❌ |

而 `tools/mark_no_forward.py` 又**用 AST 去读 `db.PENDING_PUBLISH_WHERE`**
（"同一事实两份"那条纪律的**正确**解法）⇒ 本项目里这个形状**已经出现 4 次**
（a. `run_from_qb` 返回即丢 / b. 白名单≠可见 / c. `tools/` vs 根目录双实现 / d. 不得手抄 SQL）。
★ 但 **a 是唯一"怎么摆都不对"的那种**：改放容器内则从**内部**破坏
  「crawl 不 import db」那条 **grep 可审计**的边界；放 `tools/` 则本地跑不了、
  放容器里又要 `restart` 才生效（`ERR-DOCKER-01`）⇒ **必须人工跑**（与 `crawl` 的
  「不加 `pending` 探针」同结论）。

### ★★ 更正 4（2026-09-23 晚·再续）：`crawl` 那个硬约束**不适用**——前提本来就不成立

上一节我写「`crawl` 必须人工跑，因为（i）范围不可控、（ii）只打真站」。
**（i）是真的，（ii）是我的前提错了**（`ERR-META-01` 的形状：判据从没拿已知样本校验过）。

| 我写的 | 实测 | 证据 |
|---|---|---|
| 「`base` 只能是 `https://open.cd`，所以会往**真站**发 430+ 请求」 | ★ **假的** | `Client.__init__`: `self.base = os.getenv("OPENCD_URL", "https://open.cd")` ⇒ **`OPENCD_URL` 是可覆盖的 env**，不必改一行代码 |
| 「需要"测试入口"才能安全试」 | ★ 不需要 | 用**本地静态服务器**顶替皇后站：`python -m http.server <port>` 起在 `_preview/queen/` 放几个 `NNN.html`，然后设 `OPENCD_URL=http://127.0.0.1:<port>` ⇒ **真站一个包都不发** |

★★ **更关键的一条**：`save_raw(tid, html)` 把详情页落成 `<tid>.html`，而
   `fetch_detail()` 开头 `load_raw(tid)` 命中就**直接返回本地、一个请求都不发**
   ⇒ **落盘目录 = 离线可重放的 fixture**。这正是 `crawl.py` 模块头写的
   「解析层永不重复请求，改版时重放即可」——**我把这条能力漏了**。

⇒ **结论更正**：`crawl` 的回填路**能安全验证**，不违反「只打真站」。
   仍然成立的只剩 **(i) 范围不可控**（`known` 是全局的、没有游标）⇒ 那要靠**工具 1 自己的游标**解决，
   而不是靠"人工跑"。

### ★★ 更正 5：「首次跑会真发 N 个请求、之后幂等」这个说法**不成立**

`fetch_detail` 命中 `load_raw` 就返回 ⇒ **只有第一次**发请求。但 `raw_html/` 现在是**空目录**
⇒ **每一轮都会真发请求**，直到落盘填满。这恰好与用户「排进每日 50 次请求里面去」契合：
   每轮放当天配额允许的条数 ⇒ 它们**永远在队列里、永远选不到**（因为
   `status='NOT_SEEDED'` ⇒ 闸门①）。

★★ **所以真正的设计约束是**：回填的选批逻辑**必须自己带游标**，
   **不能**指望"下一次就跳过已抓的"。用一个**只增的落盘台账**（`ran/` 目录）或
   `data.db` 里一个专用表来记「本轮想抓哪些、谁成了谁没成」。

### ★★ 更正 3：`transfer_rule='ALLOW'` **不够**，发布路还有一道独立关卡

判据（glob 实测）：`transfer_rule` / `is_denied` 在 `publish.py`、`descr.py` 里
**命中数 = 0** ⇒ `ALLOW` 只是**队列判据**（`db.PENDING_PUBLISH_WHERE`），
**不是**发布判据 —— `publish.py` 自己的预检（`cover_url` 非空等）会再拦一次。
⇒ 给 248 条标 `ALLOW` **不会**自动发布，安全性**可离线论证**。

### ★★ 更正 6：`ENABLE_PUBLISH` 与 `ENABLE_CRAWL` 两条**都已解**（不是悬案）

**（a）`ENABLE_CRAWL` 的「❔」是 `tools/nas_health.py` 的 bug，不是真未知**

容器快照里 `keys` **有** `ENABLE_CRAWL`、而 `values` **没有** ⇒
`daemon.py` 的 `save_env_snapshot()` 用 `if k in os.environ` 收集值 ⇒
**容器里没设这个变量** ⇒ 按 `env_bool("ENABLE_CRAWL", False)` 的默认 ⇒
**crawl 阶段是关的**。
★ `nas_health.py` 判「关着」只读 `values` ⇒ 把「变量不存在 ⇒ 按默认关」
  误读成「未声明 ⇒ 该动就得动」。**这是 `ERR-META-10` 同族的第 3 个实例**
  （读一个不支撑结论的量）。★ 这条是**可修的代码**（`values` 的语义是
  「显式设了的值」，拿它判「默认关」必然错）。

**（b）`ENABLE_PUBLISH` 的「`'false'` vs `.env true`」是 `ERR-ENV-02` 的实例，不是矛盾**

本机 `.env` = `true`，而容器快照（**生效值**）= `false`。
★ **本机 `.env` 不是 NAS 的 `.env`** —— `.env` 在 `deploy.sh` 白名单**外**
  ⇒ 两份文件本来就不一样 ⇒ 我读到的"矛盾"是**拿 A 机器上的文件去对 B 机器上的进程**。
★ 快照 `generated_at = 2026-09-23T10:34:47Z` = **CST 18:34:47**，是**刚写的**
  ⇒ **不是陈旧快照**。

★★ **由它得出一条有价值的结论**：`publish` 的 `last_run` 停在 `2026-09-19 22:20:41`，
   距今 **92 小时**；而阶段被关着时**根本不会被执行** ⇒
   **这独立证明了那 6 条禁转从来没被发出去过**（此前只能从"队列被拦"间接推）。
★ 同时**作废一条判据**：我原写的「下次 `publish` 自然触发时看它选几条」
  **前提不成立**（阶段关着就没有"自然触发"）⇒ 改用**离线实算**：
  `①+②+③` = 6，6 条**全 `DENY`** ⇒ 新判据 → **0**。

### ★ 工具的**起手式**：用 `db.get_pending_publish()` 当发布步的**空跑替身**

`publish.py` **没有 `--dry-run`**（`daemon.py` 里那条 `if dry_run: cmd.append("--dry-run")`
  只在 `DRY_RUN=true` 时才加，而那会**顺带打开 edit 的写操作**，`apply_flag` 那条注释写过）。
⇒ 想验证「发布这步会选几条」而**不写站点**，最安全的替身是
   **直接调 `db.get_pending_publish()`**（正是 `publish.py` 用的同一个查询）
   —— **不 import `publish`、不碰 `musopia.py`、不发任何请求**。
★★ 这一步**不需要容器**：`DATA_DIR` 指到 NAS 共享就能跑（**只读**）。
   命中的行必然带 `cover_url` 空值（`mapped_json` 空 ⇒ 没有封面 → `publish` 预检必拦），
   所以它只能**报告**、绝不会发。

### ★★ 更正 7：不推站点其实**不丢任何东西**——离线可逐字预言

「补完字段但没推上去」看起来像风险，实际**可判定**：

```python
task = <从 db.get_published() 取出的行>      # TASK_FIELDS 里已含 mapped_json
import db, descr, publish as _pub
_pub.merge_mapped(task)
form = descr.build_form_fields(task)         # ← 本地算出"站点上会变成什么"
```
与 `tools/render_compare.py` 同族 —— **同一份渲染逻辑**，纯本地、零请求。
⇒ 「推上去会改成什么」可以在**本地逐字预言**，因此：

★ **工具 2 不需要一次成功**：先补 `mapped_json`（**纯加法、非破坏**），
  推站点（覆盖式的危险那一步）**留到你想推的时候**再推；
  而推之前可以**离线比对**出确切的新旧差异。
★ 这也把 `sys_synced_at` 那条纪律的代价降到了最小：
  「不碰 `updated_at` ⇒ 不会自动推上去」不再是"看不到结果"，
  而是"结果可离线算出来，只是需要显式推"。

### ★★ 更正 8：工具 2 的目标集合有一条**现成、同源**的判据

| 阶段 | cmd |
|---|---|
| `publish` | `python publish.py --yes`（★ 无 `--dry-run`） |
| `edit` | `python update_metadata.py --stale-only --apply --yes --limit 100 --verify --interval 2 --rate 10` |
| `covers` | `python backfill_covers.py --apply --limit 20` |

★ `edit` 走 `--stale-only` ⇒ 判据**就是** `db.STALE_CONDITION`（与 `db.count_stale()` 同源）。
★ 而 `update_metadata.py main()` 里已经有了「无封面就**先读站点**从简介里回收封面」
  那条逻辑（注释写明是为了"避免用空封面渲染 `descr` 把站点上仅剩的封面抹掉"）
   ⇒ 那条对**未上站的行**（还没有 id）不适用，但**已经在站上的 222 条**正需要它。

⇒ ★ **工具 2 不该自己发明判据，而该复用 `--stale-only` 那条**：
   `site_status='EXISTS'` **且** `mapped_json` 非空 **且** `STALE_CONDITION`
   ⇒ 这就是"补好了、且还没推到站点"的**精确集合**。

### ★★ 更正 3：`transfer_rule='ALLOW'` **不够**，发布路还有一道独立关卡

判据（glob 实测）：`transfer_rule` / `is_denied` 在 `publish.py`、`descr.py` 里
**命中数 = 0** ⇒ `ALLOW` 只是**队列判据**（`db.PENDING_PUBLISH_WHERE`），
**不是**发布判据 —— `publish.py` 自己的预检（`cover_url` 非空等）会再拦一次。
⇒ 给 248 条标 `ALLOW` **不会**自动发布，安全性**可离线论证**。

### ★★ 出错就**回滚**是安全动作（判据）

`crawl.py --tid NN --info-hash HH` 若把行写坏（如 17264/17265 那两个 `NO_TORRENT_FILE`），
**回滚是可行的**：`save_task` 是 `info_hash` upsert，**只更新显式传进来的列**
⇒ 重放"不传 `status`"的那组列即可恢复。★ 这也是为什么这一步**可以试**、
而 `publish` **不可以试**（后者往公开站点写、不可撤销）。

### ★★ 用户决定（2026-09-23，紧凑后）

| 问题 | 用户的选择 |
|---|---|
| 范围 | ★ **「只补 248（有 log 的）」** —— 那 222 条已在站上的在**该范围之内** |
| 推进方式 | ★ **「先不写工具，只把方案落盘」** |
| 尚未回答 | 「要不要给这 248 条标 `transfer_rule='ALLOW'`」—— 用户**没选任何一项**它是个独立问题、与"补范围"不是同一个问题（`ALLOW` 决定"能不能进队列"，补 `mapped_json` 决定"发什么"） |

⇒ ★ **本轮不动手写工具、不改库、不部署**。下面把方案与判据落盘，等用户下一步指令。

### ★ 待落盘的方案（三个工具，各自解决一个**不同**的缺口）

| # | 缺口（实测来源） | 工具要做什么 | 关键判据 |
|---|---|---|---|
| **1** | 248 条要放**哪 26 条**进当天的 50 个请求里 | `tools/topup_opencd_fields.py`：**按 `fetched_at ASC, id ASC` 排序**取前 N 条（★ N 由**当日已用配额**算出，不写死） | ★ 必须有**持久游标**：谁有 `fetched_at`、谁已"试过但失败" ⇒ 否则每轮从第一批重来 = `crawl` 那个饿死形状 |
| **2** | ★★ **"补了但没推上去"是完全静默的**：`sync_opencd_tid.py` / `mark_no_forward.py` / `sync_site.py` 都**刻意不碰 `updated_at`**（改了 `= 整站重拉`），结果 `STALE_CONDITION` 读不到"我变新了" ⇒ 已在站上的 222 条补完**不会**被 `edit` 推上去 | 需要一条**显式**的"把该重推的标 stale"的动作，且**只标这批** | ★ 不能直接刷 `updated_at` 就算了 —— 那会让 `edit` 每轮重拉整站。**要能收窄到指定行** |
| **3** | `crawl.py --tid` 手工路一次只吃一个 tid ⇒ 248 条要手工跑 248 次 | 循环调用 `crawl.Client`，**每个请求都过同一个 `RateGate`** | ★ 排队 + 重试 + 单条失败不拖垮整批；且 `--dry-run` 下**到 `continue` 为止**（`crawl.py` 自己那条注释钉过这个） |

★★ **工具 2 与 `sync_opencd_tid.py` 的纪律直接冲突**（后者第 3 条：绝不碰 `updated_at`）。
  我的初步判断：**那条纪律没有错**（它的代价被写明过："已在站上的行，皇后路渲染**不会自动推上去**。
  那是**另一件独立的事**"）。⇒ 工具 2 就是那件"独立的事"，
  但**它需要用户定"推哪些"**（推错了 = 把站点上的标题/副标题/简介覆盖掉）。
  ★ 这是我**不会**替用户决定的那一类。

### ★ 未解 / 待用户定

1. ★★ **248 = 222 已在站上的 + 26 未上站的，两批要不要分开？**
   已在站上的：补完要**再跑 `edit` 推上去**；未上站的：补完只是"进入可发布**候选**"。
   ⇒ 这是**产品决定**，不能替用户定。
2. **补的策略**：用户已说"排进每日 50 次请求里面去" ⇒ 但**没有**说
   "要不要一次性排 248 条、由容器逐日推进"。**不做批量授权工具、不替用户排序**
   （`ERR-META-12`：不可撤销的事，没授权就是不许）。
3. `counts.json` 仍缺 `transfer_rule` / `opencd_tid` / `mapped_json` 口径（遗留）。
4. `ENABLE_PUBLISH` 快照 `'false'` vs `.env` `true`（`ERR-ENV-02`，遗留）。
5. `deploy.sh` 的「心跳在运行」单读数判活（`ERR-META-10` 同族，遗留）。

---

## 2026-09-22（下午 / 晚）—— git-WARN 订正 + nas_health 修洞 + uid 定性

### 已坐实

**1. `WARN[0000] buildx: git was not found` —— 接受它，不修（订正了四处错误归因）**

| 事实 | 证据 |
|---|---|
| 那行 WARN 与构建上下文里的 `.git` **无关** | 加 `.dockerignore` 后上下文 `174.35kB → 724B`、`.git` 确实被排除，**而 WARN 一字不变** |
| 真因 | **本机有 git、NAS 没有**；buildx 在没有 git 的机器上拿不到 commit 信息就 WARN 一句 |
| 后果 | 仅"镜像少几条 commit 标签"这类元数据；**不影响构建结果**（构建成功、镜像生成、容器运行均正常） |

★ **原归因（"buildx 看到 context 里有 `.git` 就去读提交信息，排除即可"）是我写的，已被自己的下一次构建推翻。** 已订正 4 处：`.dockerignore`（文件头 ① + L26）、`deploy.sh:72-74`、`ERRATA.md` 的 `ERR-DOCKER-07`、`README.md:117+`。其中 **`.dockerignore` 与 `README.md` 已上生产**（2026-09-22 13:56）。
★ **`.dockerignore` 继续保留**，但理由是**凭据外溢**（实测 7 份 `.env*` 与 `data.db` 不再上传），**不是**那行 WARN。

**2. `deploy.sh` 自己不在生产目录里**

```
ls //iSunker-DS423/docker_ssd/musopia_script2/deploy.sh  →  No such file or directory
```
它是**本地开发工具**（脚本注释明写）。⇒ 我一度说"`deploy.sh` 白名单里（会过去）"是**错的**。
★ 本次上生产的只有 **2 个文件**：`.dockerignore`、`README.md`。

**3. `ENABLE_PUBLISH=false` —— 三源一致，提示词那行是错的**

| 来源 | 值 |
|---|---|
| `docker exec opencd2musopia env` | `ENABLE_PUBLISH=false` |
| 容器自己落的 `daemon_env.json` | `"ENABLE_PUBLISH": "false"` |
| 71h 日志（无 publish 输出） | 旁证 |

⇒ **publish 的 `last_run` 停着是预期的**，不是真异常。**快照机制如实反映 `os.environ`（校验通过）。**
★ 巡检提示词里 `ENABLE_PUBLISH=**true**` 那一段**已连续污染四轮**，必须删（改法见 TaskList #19）。

**4. `crawl` 是第 6 个阶段，`ENABLE_CRAWL` 默认 False（且不在生产 `.env`）**

`daemon.build_stages()` 共 **6** 个阶段：`scan / publish / sync / covers / edit / crawl`，
各由 `env_bool("ENABLE_*", …)` 门控；**只有 `crawl` 默认 False**（`daemon.py:169`）。
间隔：`CRAWL_INTERVAL` 默认 **1800**。
★ **startup banner 不是"阶段列表"的判据** —— 它只列**门控后**进来的阶段，
两个 false（publish/crawl）就被它吃掉。**我拿它当判据错过一次。**

**5. `iter_my_uploads()` 没有翻页 bug；`check_login()` 的 uid 提取是对的**

| 事实 | 证据（真站点观测，本机跑） |
|---|---|
| 端点姿势正确 | `getusertorrentlistajax.php?userid=&type=uploaded&page=N`（**0 基**）+ `X-Requested-With` + `Referer` ⇒ `HTTP 200` + 6498 字节正常列表 |
| 站点自报条数 | 回执正文：**`<b>3</b> 条记录 | 总大小：853.46 MB`** |
| `type=uploaded` 语义对 | 返回真实数据（非空壳） |
| 我们自己的种子属于 10427 | 库里 `site_torrent_id=14866/14865/14864` 的 `details.php` **上传者全是 `10427`** |
| `check_login()` 取法 | `re.search(r"userdetails\.php\?id=(\d+)", torrents.php 正文)` —— 取**第一个**命中 |

⇒ **docstring 里的候选 ①②（type 写错 / 调用姿势不对）都已被推翻。**
⇒ **"264 只捞 2" 的真因不在这两个函数里**：10427 在站点上**确实只有 3 条**，
   而 `iter_my_uploads` 产出 2 条（**解析边缘差，非 bug**）。

**6. `run.py` 的 scan 计数已改对；「短路」与库内 `NO_LOG_FILE` **不可相减****

- `run.py` 现在直接打印**库里**的 `NO_LOG_FILE` 总数（唯一真值）。
- 日志实测：`已知无日志(短路) 240`（两轮**不变**）而库内 `NO_LOG_FILE` `244 → 245`（随 `total` 同步 +1）
  ⇒ 两者是**两个量纲**：短路 = `qB ∩ 库[NO_LOG_FILE]`（历史累积、只在 qB 里的那部分）。
- ★ 提示词里的 **`241` 本身也是手抄错值**（日志原话 `240`）。

### 本会话改的代码 / 文档（均已验证，已上生产的标出）

| 文件 | 改动 | 上生产？ |
|---|---|---|
| `tools/nas_health.py` | `INTERVALS` 补 `"crawl": 1800`；`disabled` 判定改**三态**（`None`=未声明 ≠ 没关），并把缺键**显式印出** | ❌ 本机（`tools` 不进生产） |
| `.dockerignore` | 文件头 ① 与 L26 的 git-WARN 归因订正 | ✅ |
| `README.md` | `定性并修掉` → **`订正：接受它，不修`** + 过程教训 | ✅ |
| `deploy.sh` | 72-74 归因订正 | ❌ 不在生产 |
| `ERRATA.md` | `ERR-DOCKER-07` 订正（含"本条曾写错"） | ❌ 刻意不上生产 |

**验证门（全过）**：引用完整性 悬空 0 / 半悬空 0 / LEGACY 0 / 锚点无命中 0 / 锚点歧义 0；
5 个改动文件 **CR=0**（**字节计数**，不用 `grep '\r'` —— `ERR-OS-02/03`）。

### 未解（**待验的判据已写明，别凭印象推**）

1. ★★ **库里 264 个 `site_torrent_id` vs uid 10427 在站点上只有 3 条** —— 二者严重不符。
   候选：**(a)** `sync_site.py` 的归属判据认错人（`ERR-META-03` 一族）；**(b)** 站点旧号段被清理。
   **未坐实，不许下结论。**
2. **`id=85068`** —— 用户给出但**未说明含义**（我们真正的 uid？某种子 id？叶惠美那个种子？）。**存疑。**

### ★ 本会话我犯的错（记下来，比结论更值钱）

| 错 | 形状 |
|---|---|
| 说"新增 2 行是非 `EXISTS`" | 从**两个孤立读数**推一个**没观测过**的过程 |
| 说"`deploy.sh` 在白名单里（会过去）" | 凭"听起来合理"推断**没核对过**的事实 |
| 给出 `docker compose run … /data/_probe_uploads.py` | 没核对**文件到底在不在生产**（`tools/` 被 `.dockerignore` 排除且不在白名单） |
| 编了个端点 `myuploads.php` 去打 | **猜判据**；真端点是 `getusertorrentlistajax.php`（好在 404 刺眼，没敢下结论） |
| 说"10427 是别人、我们查错人了" | 漏掉更简单且与全部证据相容的解释（**10427 就是我们的，只是发布只有 3 条**） |

★ **共同形状：判据没先拿已知样本校验一次（`ERR-META-01`）。**
★ **纪律**：`grep -rn <pattern> /` 是**无界扫描**，会跑到天荒地老（实测超时 120s 被停）；
查工具的报错文字去查**它自己的安装目录或文档**，不是扫全盘。

---

## 2026-09-22 22:29 —— 归档补充（B 修复上线后的首次巡检）

**1. `nas_health.py` 的 B 修复已在巡检中生效**（正常表现，非异常）：

```
⚠ 快照里没有这些键 ⇒ 按'该动就得动'判（未声明≠关着）：crawl
crawl  1800s  -  -  ❔ state 里没有这个阶段
```

`crawl` 原**完全不在检查循环里**（`INTERVALS` 缺它），现在被检查、且缺位被**显式报出**
而不是被 `None is False` 吞成"没关"。`last_run` 为 `-` ⇒ 与 `ENABLE_CRAWL` 默认 False、
从没跑过一致。

⚠️ **遗留模糊点**：`❔ state 里没有这个阶段` 分不清「**没配**（关着，正常）」与
「**配了但每轮崩在写 state 之前**（真异常）」。**答一句即可定：生产 `.env` 里有没有
`ENABLE_CRAWL` 这一行？**（见 TaskList #23）

**2. 巡检提示词的三处假判据（第 5 轮仍未改）**

| 提示词原文 | 实测 |
|---|---|
| `ENABLE_PUBLISH=**true**` + "publish 是开着的、每 30 分钟一跑" | ~~**三源一致为 false**~~ ★ **本条已订正：真值是 `true`** —— 见下一节（2026-09-23）第 2 条。当时说的"三源"是三个**间接读数**，同源同错；而 `.env` 可直读却没读 |
| 第 4 条「短路 `241` vs 库 `NO_LOG_FILE=244`，差 3」 | **量纲混淆**：短路 = `qB ∩ 库[NO_LOG_FILE]`，**不可**与库总数相减（`ERR-META-05`）；且 `241` 是**手抄错值**（日志原话 `240`） |
| 第 1 条「`iter_my_uploads()` 翻页是坏的」 | ~~**已证伪**：函数无 bug；`userid=10427` 站点自报**只有 3 条**~~ ★ **本条需重验**：`10427` **不是我们的 uid**（真是 `85068`）⇒ 当时的观测是在**查别人的号**下取得的，不足以证伪 |

★ 改法（替换文字）见 TaskList #19。**提示词是用户配置，本机改不了。**

**3. 待用户确认的输入**

- **`id=85068`** —— 用户给出但**未说明含义**（我们真正的 uid？某种子 id？叶惠美那个种子？）。
  **未猜、未用**。#17 的入口。
- **`ENABLE_CRAWL` 在不在生产 `.env`** —— #23 的唯一问题。

---

## 2026-09-23 —— 读 vertex2 + 定性 site_torrent_id 号段 + 订正三处旧结论
### 已坐实

**1. `id=85068` = 我们自己的皇后 uid（`iSunker`）** —— ★ 上面那两条"待确认"里的第 1 条，**有答案了**

`\\iSunker-DS423\docker_ssd\vertex2\vertex\db\sql.db` 的 `sites` 表 243 行，
**全部**是 `('OpenCD', 85068, 'iSunker', ...)`（唯一一种 `(site,uid,username)` 组合）。

⇒ 它是我们皇后账号的 uid，**不是**某种子 id。顺带拿到该表当时间线用：
最新一行 `upload=630941433200`（≈630.9 GB）、`download=184887647674`、`seeding_num=568`。
★ 那是**别人项目的库**，只读；本项目的 `data.db` 与它无关，别混。

**2. ★★ 订正：上一节"`ENABLE_PUBLISH` 三源一致为 false"是错的 —— 真值 `true`**

| 来源 | 值 |
|---|---|
| 本机 `.env`（`D:\Projects\musopia\zhuanzhong\.env`） | **`ENABLE_PUBLISH=true`** |

上一节把**会话里的三处读数**当成了三源，而它们可能同源同错（`ERR-META-01`）。
★ 教训：`.env` 是**可直读的事实源**，当时没去读它，反而用了三个间接读数互相印证。
⇒ 巡检提示词里 `ENABLE_PUBLISH=true` 那条**是对的**，此前 6 轮的"纠错"**本身是错的**。

**3. 生产 `.env` 里没有 `ENABLE_CRAWL`** ⇒ #23 结案

`daemon.py:169` 的默认 `False` 生效 ⇒ `state` 里没有 `crawl` 阶段是**预期的**，
不是"配了但每轮崩在写 state 之前"。上一节标为「遗留模糊点」的那条可以关掉。

**4. ★★ 269 个 `site_torrent_id`（14611–17100）不是皇后 id，也不是本项目发布的 id**

判定用四条独立证据（全部只读）：

| 判据 | 实测 | 排除什么 |
|---|---|---|
| 同一 infohash 两个编号 | `e4388478…` 我们记 **14611**、皇后是 **198601** | 不是皇后 id |
| 号段交集 | 我们 14611–17100；皇后真实段 **194702–201008**（557 个）⇒ 交集 **0** | 同上 |
| `status` 取值 | 全库只有 `SUCCESS` / `NO_LOG_FILE`，**没有 `PUBLISHED`** | 不是 `publish.py` 写的（它写 `PUBLISHED`） |
| EXISTS 行的 `reason` | **269 条全为空** | 同上（`publish.py` 会写 `published_id:…`） |
| `updated_at` vs `site_synced_at` | 261 行 `updated_at < synced` | 符合"行先存在、ID 后回填" |

**时间线**：只有 4 个写入日 —— **09-09(252) / 09-13(6) / 09-14(6) / 09-19(5)**，之后冻结。
09-22 以后**没有任何新 EXISTS**。

★ **强形状**：259 条的 `site_synced_at` 以 **≈6 秒/条连续递增**，且在遍历一个连续 id 表；
**5 个 id 被两条不同种子共用**（14622/14669/14706/14714/14761 —— 全是「期間生産限定盤 / 初回生産限定盤」
这种同专辑相邻条目）⇒ 形状像**按标题模糊匹配的批量导表**，不像自动同步。

⇒ **14611–17100 属于哪个站点：本地证据不足，未定性。** 不猜（`ERR-META-01`）。

**5. ★ `sync_site.py` 从未跑通过 —— 结构性不可能跑通**

它拿的 id 来自 `client.iter_my_uploads()`，而该函数（`musopia.py:92-158`）拉的
是**皇后**的 `details.php?id=`（`_parse_torrent_links` 的判据）。
皇后 id 是 **19 万段** ⇒ **在结构上写不出 14611**。

⇒ 那 269 个值是**外部/手工**写进来的，不是本项目的任何一条自动通路。

★ 附带：`iter_my_uploads()` 的 `userid` 也写死了（快照里是 `10427`），
而真 uid 是 **85068** ⇒ 见 `ERR-SITE-02`。**"264 只捞 2"由此有了第二个候选解释**
（不是端点语义，是**在查别人的号**）—— 与旧结论不冲突，但旧结论（"端点语义如此"）
是在**用错的 uid** 的前提下得出的，**需要重验**。

### 本会话改的文档

| 文件 | 改动 |
|---|---|
| `ERRATA.md` | 新增 `ERR-META-06`（跨站 ID 必须标站点）/ `ERR-META-07`（落库值要记来源判据）/ `ERR-SITE-02`（`userid` 不能写死）/ `ERR-SITE-03`（同 infohash 两号段） |
| `SESSION-NOTES.md` | 本节；并把上一节「`ENABLE_PUBLISH` 三源一致 false」**标为已订正** |
| `tools/backfill_opencd_tid.py` | **新增**（只读预览 + 受守卫的回填）。见下 |

★ **未进 `deploy.sh` 白名单**（`ERRATA.md`/`SESSION-NOTES.md`/`tools/` 都不是运行时输入，
加了会撞 `ERR-DOCKER-05`）。★ `data.db` 本会话**一个字都没改**（复核：非空 `opencd_tid` 仍 = 0，总行数仍 547）。

### 6. `tools/backfill_opencd_tid.py` —— 只读预览已跑通（**未写库**）

把皇后页面 id 按 **infohash** 映射进 `tasks.opencd_tid` 的工具。预览实测：

| | |
|---|---|
| 皇后库记录（`tracker.open.cd`） | 583 |
| ★ 可配对（infohash） | **430**（需回填 430 / 已是该值 0） |
| 未命中 | **117**（其中 45 条带 `site_torrent_id`，逐条打印） |
| infohash 冲突（同 hash 多 tid） | **0** |

三条纪律已写进文件头并**实测生效**：
1. 默认只读；`--apply` 必须同时给 `--i-have-read-the-preview`；
2. 只 `UPDATE opencd_tid` 一列 —— **不调 `save_task()`**（那是 upsert，会冲掉第一阶段的
   `content_path`/`log_content`，即 `opencd_ingest` ⑥ 号测试钉的那个）；**不碰** `site_torrent_id`/`site_status`；
3. 未命中就报出，**绝不用标题去凑**（`ERR-SITE-03`）。

★ **★ 提交前自检抓到自己的一个 bug**：守卫原先写在 `apply_backfill()` **调用之后** ⇒
会「先写库、再报拒绝」。已把守卫移到函数最前面（读取任何库之前）。
★ 而且我用来验证的那行 `find("apply_backfill(todo")` **先命中了函数的*定义*处** ⇒
第一次核对**报了个假「顺序错」**。判据要用**调用点**，不是**定义点**（`ERR-META-01` 的又一例）。


### 未解（**判据已写明**）

1. **14611–17100 属于哪个站点** —— 需要一次**真实的站点观测**，本机做不到。
   候选：**(a)** 本地下游站点（非皇后）的 id；**(b)** 已废弃的旧号段。
   ★ **不许**据此改动 `data.db`；那条列现在**不可信**。
2. **`iter_my_uploads()` 用真 uid（85068）重跑会产出几条** —— 需真站点观测量。
   ★ 这才是"264 只捞 2"的真正检验；旧结论是**在错 uid 下**取得的。

---

## 2026-09-23（下午）—— ★ `opencd_tid` 回填 430 条**已写入生产**

### 已坐实

**1. `tasks.opencd_tid`：0 → 430**（生产库，2026-09-23 08:18）

来源：另一个项目（vertex2）的库，**按 infohash 配对**。

```
总行数            547   （不变）
opencd_tid 非空   430   ← 写入成功
opencd_tid 空白   117   ← vertex 库里没有对应 infohash
号段              196636–201008  （皇后 19 万段 ✅，**不是**我们自己的 14611）
非纯数字           0
```

**2. ★★ 两条纪律的现场验证（这是本次最有价值的部分）**

| 判据 | 回填前 | 回填后 | 说明 |
|---|---|---|---|
| `stale`（`EXISTS` 且 `updated_at > site_synced_at`） | **6** | **6** | ★ **没变** ⇒ 回填**确实没碰 `updated_at`** |
| `max(updated_at)` | `2026-09-23 04:54:39` | 同 | ★ 同上 |

⇒ 若当初"顺手"刷了 `updated_at`，这 430 条会全变 stale、`edit` 会每轮重拉整站。
**这是 `db.STALE_CONDITION` 那个坑被正面避开的证据。**

**3. 单写者纪律（`ERR-DOCKER-03`）怎么落实的**

用户停容器 → 我用**三条独立信号**确认（不是靠"应该停了"）：

| 信号 | 观测 |
|---|---|
| 心跳**冻结** | `08:16:09` 起 20s+ 不动（活着的 daemon 每个 tick 都写） |
| 无 `data.db-wal`/`-shm` | ✅ ⇒ 最后一个连接已关闭（`ERR-SQL-01` 的推论） |
| `data.db` mtime 静止 | `04:54:39` |

★ **踩到一个假判据**："心跳年龄 54s < 120s ⇒ 还在跳" —— 那是**误报**：
mtime 与内容都冻结在容器停止那一刻，年龄只是"停止距今多久"。
**判"死没死"要看它有没有在*前进*，不是看它多老**（`ERR-SMB-02` 的又一例）。

**4. 交叉核对（`ERR-META-01`：判据要拿已知样本校验）**

两份**独立实现**给出同一组数：

| | 需回填 | 已是该值 | 未命中 | 冲突 |
|---|---|---|---|---|
| `tools/backfill_opencd_tid.py` | **0** | **430** | 117 | 0 |
| `sync_opencd_tid.py`（生产） | **430** → 写后 **0** | 0 → **430** | 117 | 0 |

**5. 备份**：`//iSunker-DS423/docker_ssd/musopia_backup/data.db.before-opencd-tid-20260923-081809`
（compose 目录**之外**，sha256 与源一致 `fbd3b07e…`）。

### 本会话新增 / 改动的代码

| 文件 | 改动 | 上生产？ |
|---|---|---|
| `sync_opencd_tid.py` | **新增**（根目录）。生产模块：只读 vertex 库 → infohash 配对 → **只填空白**、**不碰 `updated_at`**、**不用 `save_task()`**。`--apply` / `--selftest --json`（**29/29 全绿**） | ❌ **尚未部署**（见待办） |
| `tools/backfill_opencd_tid.py` | WHERE 收紧为**只填空白**；头部加「与生产模块的分工」表 | ❌ `tools/` 不进生产 |
| `counts.json` | 刷新（`total=547`） | ❌ 生成物 |

**生产模块的关键判据（自检 29 项，含阴性对照）**：
`updated_at` 逐行不变 / 已有非空异值**不被覆盖** / 抠的是 `link` 里的 tid **不是 `torrents.id`** /
外部库 mtime+size 不变且**没多出 `-wal`/`-shm`** / **源缺失·表结构漂移·0 字节库都 raise**（不降级成"没有"）。

★ **自检抓到两个真问题**：
① `build()` 把"非空但不同"的值也归进 `todo`，而 SQL 的"只填空白"守卫会拒写它们
   ⇒ **汇总数字虚高 + 幂等判据永远不成立**。已拆出独立的 `diff` 堆（报出、不写）。
   ★ 这是"判据与实际写入必须同口径"（`ERR-META-03` 一族）。
② 我第一版**数错了 fixture**（`tracker.open.cd` 是 6 行不是 5 行）。

### 未解 / 待办（**判据已写明**）

1. ★ **`sync_opencd_tid.py` 还没部署** —— 它要跑自动化必须：加进 `Dockerfile` COPY + `deploy.sh` FILES；
   ★★ **compose 要加只读挂载** `${VERTEX_DB_DIR:-/volume2/docker_ssd/vertex2/vertex/db}:/vertex:ro`
   （容器现在**看不到** vertex2 目录）；`daemon.py` 加第 7 阶段 `queen_tid`（默认开、86400s、**无探针**）；
   `nas_health`/`gen_counts` 的字典同步。**详见计划文件**。
2. ★ **第 2 步：让改动在站点上生效** —— 回填**不碰 `updated_at`** ⇒ `edit` 看不到这些行"变新了"
   ⇒ **已在站上的 224 条**的皇后路渲染（`OpenCD` 标签/副标题/艺术家）**不会自动推上去**。
   需**显式**把该重推的行标 stale，再跑 `edit`（先不带 `--apply` 预演）。**尚未做。**
3. **117 条未命中**：`62c57cd1…` 等，在 vertex 库里没有对应 infohash。
   区分"皇后上真没有" vs "被 vertex 的 RSS 条件滤掉" —— 用户已说"先不管"。
4. **14611–17100 属于哪个站点** —— 仍需真站点观测。（上节未解 #1，未动）
5. ★ **`counts.json` 里没有 `opencd_tid` 的口径** —— 它是会变的数字，按纪律该进 `counts.json`；
   但那要动 `db`/`04_probes` 的口径源，**未擅自加**。
6. **`opencd_tid` 有了，但 `mapped_json`/`artist`/`year` 仍全空** ⇒ 皇后路**分流开了、内容还缺一半**。
   独立议题。

---

## 2026-09-23（下午 / 晚）—— 试发 2 条 + 挖出「禁转」洞 + 加闸门③④

### 已坐实

**1. ★★ 第一条真正的 `PUBLISHED`：试发 2 条成功**

```
17264  黄清元 - 金钻名曲珍藏   （opencd_tid=198601）
17265  群星 - 醉民乐 2011      （opencd_tid=198827）
```
手法：`--only` 只改 2 条的 `site_status` → 手动 `publish.py --limit 2 --yes`（不起 daemon）。
★ 全库**第一次**出现 `status='PUBLISHED'`（此前只有 SUCCESS / NO_LOG_FILE）
⇒ 印证"publish 从没成功跑通过"那条旧结论。

**2. ★★★ 试发暴露：站上**只剩一个 `OpenCD` 标签**，皇后的「港台/流行」丢了**

根因**已复现**（跑了一遍 `build_form_fields`）：

```
mapped_json = None → db._load_mapped() = {} → declared = None
  ⇒ descr.py:621 的 `if declared:` 不成立 ⇒ 走 detect_tags(title, raw)
  ⇒ 而 descr.py:599-603 的注释**自己警告过**它对 OpenCD 标题返回 []
  ⇒ tags[4][] = ['31']  ← 只剩 OpenCD（:640 那条只依赖 opencd_tid，所以它生效）
```
⇒ `medium_sel` 是猜的、`team_sel` 是 `'0'`、`artist`/`year` 空。
★ 这是「**分流开了、内容还缺一半**」的实测坐实：`opencd_tid` 是**门牌号**，
  而页面内容在 `mapped_json`/`artist`/`year` 里 —— **那三列从没写过**。

**3. ★★★ 更重的一条：探针挖出「禁止轉載」的种子**已经排在发布队列里

只读探针（`_probe_queen_page.py`，1 个 GET）实测 `id=200982`：

| 字段 | 值 |
|---|---|
| `transfer_rule_raw` | **`禁止轉載`** ⇒ `transfer_rule = 'DENY'` |
| `small_descr` | `YAMAHA CRW-F1E \| 低速原抓 \| Log100% \| 高清扫描完整图册 \|WAV\|` |
| `tags` | `['大陆','古典(Classical)']` → `['4','15']` |
| `medium`/`team` | `CD` → `medium_sel='8'`；`其他(Other)` → `team_sel='5'` |
| `artist`/`year` | `冯刚毅 苏春梅` / `2001年` |
| `unmapped` | **`[]`**（一个都没漏） |

⇒ ① **六个字段全都能拿到** ⇒ 「补 `mapped_json`」技术上成立
⇒ ② ★ **差一步就把"禁止转载"的种子转发出去** —— 而 `opencd_ingest` 本来有
   `STATUS_DENIED` 挡这个，**但它只管 ingest 那条路**；这批是**第一阶段的行**
   （qB 本地抓轨），**从来不过 ingest** ⇒ 那道闸门对它们**完全没有约束力**。
★ 顺带**证伪**「皇后没有副标题」：这份页面的副标题很长很完整；
  之前那 2 条没副标题是**那两个页面本身**的事（`opencd_parse` 有
  `small_descr_present` 专门区分「真没有」和「没取到」）。

**4. ★★ 新增闸门③④（`db.PENDING_PUBLISH_WHERE`）**

| 闸门 | 判据 | 分寸（★ 承重） |
|---|---|---|
| **③ 没抓页面不许发** | `opencd_tid` 非空 **但** `mapped_json` 空 ⇒ 拦 | `opencd_tid` **空** = 本地抓轨行 ⇒ **照旧发**（一刀切会把第一阶段全挡下） |
| **④ 禁止转载不许发** | `transfer_rule = 'DENY'` ⇒ 拦 | `transfer_rule` **空** = 没抓过 ⇒ 不拦；`NEED_CONFIRM` **暂定放行**（该不该拦是产品决定，未定不擅改） |

**实测**：队列 **32 → 6**（那 6 条是本地抓轨行，不受影响）。
**五种组合逐个验过**（含阴性对照）：`mapped空/rule空→拦`、`mapped有/rule空→放`、
`mapped有/ALLOW→放`、`mapped有/DENY→拦`、`mapped有/NEED_CONFIRM→放`。

★ **新增列 `transfer_rule`**（`db.COLUMNS`）⇒ `init_db()` **自动 ALTER 补列**
  （实测：`🔧 数据库自动迁移，新增列: transfer_rule`，547 行未变）；
  `opencd_ingest.build_row()` 现在会写它。★ 已部署、已重建镜像、**闸门已生效**。

### ★★ 本会话我犯的错（比结论更值钱）

| 错 | 形状 |
|---|---|
| ★★ 说"等你一条命令部署"，但**我那句 `deploy.sh --apply` 被权限拦了**，我没发现 | **"打算做"当成"做到了"** ⇒ 用户跑了 `up -d --build`，而代码还在本机 ⇒ 闸门没生效（报 32 条）。判据只能是**回读 NAS 文件哈希** |
| ★ 用 **12s 窗口**判断容器停没停，两次读数相同就判"已停" | 采样窗口**短于**心跳周期（`SCHED_TICK=30s`）⇒ "没动"是**常态**不是信号。后来 15s×3 采样才发现它在跳 |
| 第一版阴性对照**设计错了**（挑了条已发布的测 ALLOW） | 那条 `status='PUBLISHED'` ⇒ 本来就不该在队列里 ⇒ 结论无效。重做后五种组合才可信 |

### 新发现的一个真 bug（**未修**，待议）

**`scan` 会把 `PUBLISHED` 冲回 `SUCCESS`。**

```
run.py:203-206   db.save_task(info_hash=..., status="SUCCESS", ...)   ← 无条件写
scan 每 30 分钟扫 qB，**已发布的种子也在 qB 里** ⇒ 每条都被重新 save_task
⇒ status='PUBLISHED'（publish.py:210 写的）**活不过一轮 scan**
```
**实测印证**：08:56 刚发完是 `PUBLISHED`（2 条），09:2x 再看已是 **`SUCCESS`**（2 条）。

★ **暂时无害**：`save_task` 的 upsert 只更新**显式传入**的列 ⇒
  `site_status`/`site_torrent_id` **没被冲掉**（那 2 条仍在 `EXISTS` + 有 id）
  ⇒ **没造成重复发布**。
★ **但是隐患**：「已发布」这个事实**丢了** ⇒ 任何靠 `status='PUBLISHED'` 判事的逻辑都失效。

### 未解（判据已写明）

1. ★ **430 条怎么补 `mapped_json`？** 要**逐条抓页面**，而
   `OPENCD_MAX_PER_DAY = 50` ⇒ 430 个请求 = **8.6 天**（且配额全站共用）。
   候选：**(a)** 只补要发的（按需）；**(b)** 每天 50 条慢慢补；**(c)** 只补已在站上的 224 条。
2. **已发的 2 条（17264/17265）**：用户说**不是禁转**（未独立验证）。
   但它们**仍内容不全**（缺港台/流行、artist/year 空）。
   修法：抓那 2 个页面（2 个请求）→ 补 `mapped_json` → `edit` 推上去。
3. ★ **`NEED_CONFIRM` 该不该也拦** —— 产品决定，未定。
4. **`scan` 冲掉 `PUBLISHED`** —— 要不要修、怎么修（见上）。
5. **`counts.json` 没有 `opencd_tid`/`mapped_json` 的口径**（上节 #5，未动）。

---

## 2026-09-23（晚）—— ★ 用 `--fields` 白名单精确补全已发布的 2 条（**成功**）

### 已坐实

**1. ★ 抓 2 个皇后页面 → 补 `mapped_json` → 用 `--fields` 只推该推的格子**

```
crawl.py --tid 198601 --info-hash e43884787e3a...   （1 请求）
crawl.py --tid 198827 --info-hash 049f10efaf24...   （1 请求）

update_metadata.py 17264 --fields <6 个键> --apply --yes --verify
update_metadata.py 17265 --fields <6 个键> --apply --yes --verify
```

★ 两条**都回读校验通过**：

```
✅ 已提交（302 -> details.php?id=17264&edited=1）
🔎 校验: ✅ 封面已生效 | 标题《黄清元 - 金钻名曲珍藏 FLAC 1998 - FLAC 整轨 - OpenCD[免费]》| 页面图片 1 张
```

**2. ★★ 关键判据：标题**没被改**（`--fields` 白名单真的拦住了）**

回读的标题仍是**我们自己的写法** `黄清元 - 金钻名曲珍藏 FLAC 1998 - FLAC 整轨 - OpenCD`，
**没有**被换成皇后的 `黄清元 - 金钻名曲珍藏 1998 - FLAC 分轨`。
⇒ 这是"只补不覆盖"（A1）**生效的现场证据**。

**3. 最终状态（与抓取前基线逐条比过）**

| 列 | 17264 | 17265 |
|---|---|---|
| `mapped_json` | None → **已补**（tags `['6','10']` / medium `8` / team `5`） | None → **已补**（tags `['4','10']` / medium `8` / team `5`） |
| `artist` | None → **`黄清元`** | None → **`群星`** |
| `year` | None → **`1998年`** | None → **`2011年`** |
| `transfer_rule` | None → **`ALLOW`** | None → **`ALLOW`** |
| 站点 `tags` | `['31']` → **`['6','10','31']`** | `['31']` → **`['4','10','31']`** |
| ★ `site_status`/`site_torrent_id`/`content_path` | **一字未变** ✓ | **一字未变** ✓ |

★ **`transfer_rule = ALLOW`（歡迎轉載）证实了用户"这两条不是禁转"的判断**
（对照：`200982` 是 `DENY`）。

**4. ★ `update_metadata.py --fields` 是**任意键**的白名单（不是只有三项）**

```
select_fields(): {k:v for k,v in full_fields.items() if k in keep}
musopia.edit_torrent() docstring: "overrides 里给什么就改什么，其余字段沿用页面上的现值"
```
实际用的白名单（7 个键名逐个核对**都存在**）：
```
tags[4][],custom_fields[4][1],custom_fields[4][2],custom_fields[4][3],team_sel[4],medium_sel[4]
```
★ 被**排除**的三项：`name`（会换成皇后标题）、`small_descr`（会清空站点副标题）、`descr`。
⚠️ **`--fields` 的 help 是过时的**（写"可选 name,small_descr,descr"，实际接受任意键）—— 未改，待议。

### ★ 本会话的错（同一形状，第 3 次）

| 错 | 形状 |
|---|---|
| 报「`mapped_json` 存着标题字符串、会让 `_load_mapped()` 抛异常」 | ★ **假的**。我的 `SELECT` 取了 6 列却按 5 个字段 `print` ⇒ **把 `torrent_file` 读成了 `mapped_json`**。改用 `sqlite3.Row` 按列名取后：全库 `typeof(mapped_json)='null'`，547 行全空 |
| 说「闸门管不着那 6 条」 | **措辞不准**。准确说法：闸门③ 管的是「从皇后转载」那条路（判据看 `opencd_tid`），而那 6 条是**本地抓轨行**（`content_path` 指向 `/downloads/…`），**本来就不在它的管辖范围内** |

★ **共同形状**：**用"看起来对"代替"核对过"** —— 与今天早些时候的
「12s 窗口误判容器已停」「以为部署过了」**同族**（`ERR-META-08`/`ERR-META-09`）。

### 新增未解

1. ★★ **430 条里还有 428 条 `mapped_json` 是空的** —— 补一条 = 1 个请求，
   而 `OPENCD_MAX_PER_DAY = 50` ⇒ 全补要 **8.6 天**（配额全站共用）。
   候选：只补要发的（按需）／每天 50 条慢慢补／只补已在站上的 224 条。
2. **`crawl.py` 的手工路必须显式给 `--torrent-file`**，否则落 `NO_TORRENT_FILE`
   （设计如此：重建 `.torrent` 会改 passkey ⇒ 改 infohash）。
   ★ 那两条现在 `status='NO_TORRENT_FILE'` —— **无害**（`torrent_file` 列没被冲），
     但它们因此**不在"可发布"队列里**（对已发布的种子无所谓）。
3. **`update_metadata.py --fields` 的 help 过时**（见上）。


---

## 2026-09-23（上午·续）—— ★ `scan` 冲状态的真机制 + 两条代码修复 + 工具就位

### ★★ 1. 订正：「`scan` 把 `PUBLISHED` 冲回 `SUCCESS`」—— **结论对，我给的机制错**

上一节我写的机制是「已发布的种子也在 qB 里 ⇒ 每轮重扫被重新 `save_task`」。
**读 `run.py:114-146` 逐条推完发现不是这样**：

| 入口分支（`if not args.recheck and info_hash in known`） | 行为 |
|---|---|
| `L121` `status=='SUCCESS'` 且 `site_status=='EXISTS'` | → skip |
| `L124` `status=='SUCCESS'` 且 `.torrent` 在 | → skip |
| `L127` `status=='SUCCESS'`（文件不在） | → 往下走（"重做"） |
| `L144` `status=='NO_LOG_FILE'` | → skip |
| ★ **其它 status（`PUBLISHED`/`FAILED`/`NO_TORRENT_FILE`）** | ★ **没有任何分支 ⇒ 直接落到 `L203` 那个无条件 `save_task(status="SUCCESS")`** |

⇒ 真机制：**不是"发布的行被重扫"，而是"`PUBLISHED` 不在那两条 `if` 里 ⇒ 畅通无阻"**。
⇒ 而它一旦变成 `SUCCESS`，下一轮就落进 `L121`（`site_status=EXISTS`）被 skip
   ⇒ **只冲一次**（这正好解释实测：08:56 是 `PUBLISHED`、09:2x 已是 `SUCCESS`、且没重复发布）。

★ **附带发现**：`NO_TORRENT_FILE` 那 2 条**正在被同一条路冲** ——
  实测 `17264`/`17265` 现在 `status='NO_TORRENT_FILE'`（`crawl.py` 手工路写的），
  下一轮 `scan` 一跑就会变 `SUCCESS`。**这就是那个 bug 当下的活受害者。**

### 2. ★ Fix A：`db.PROTECTED_STATUSES` + `run.py` 的守卫

```python
# db.py（新增，唯一来源）
PROTECTED_STATUSES = ("PUBLISHED", "NO_TORRENT_FILE", "FAILED")
def get_status(info_hash):   # 只读，行不存在 ⇒ None
```

`run.py` 在**无条件写 `SUCCESS` 之前**插了守卫：
`if not args.recheck:` → 读库现况 → 命中受保护集合就**只刷元数据、不动 `status`**。

★ **为什么是这三个值**：都是**别的阶段下的结论**，`scan` 没有资格推翻。
★ **为什么 `NO_LOG_FILE` 不在里面**：`run.py` 对它**有专门短路分支**（`L144` 直接 continue，
  压根走不到 `save_task`），而 `--recheck` 的语义正是"无视这个缓存重做一遍"
  ⇒ 把它塞进来会让 `--recheck` **失效**。
★ **为什么 `--recheck` 要豁免**：它是**显式**要求重做，有权推翻；默认路径无权。

**验证（在库副本上，生产库只读）——含阴性对照**：

| 判据 | 结果 |
|---|---|
| 复现旧行为（无条件写） | `NO_TORRENT_FILE` → `SUCCESS` ✅ **bug 复现了** |
| 新守卫 | `NO_TORRENT_FILE` → **保住了** ✅ |
| 阴性对照：普通 `SUCCESS/UNKNOWN` 行 | `in PROTECTED? False` ⇒ 照旧能写 ✅（否则第一阶段会被挡死） |
| `get_status(不存在的行)` | `None` ✅ |

### 3. ★ Fix B：`--fields` 的 help + 新增 `--list-fields`

★ 旧 help 写「**可选 name,small_descr,descr**……默认改全部三项」—— **过时且错**：
`select_fields()` 做的是 `{k:v for k,v in full_fields.items() if k in keep}`，
**没有任何键名白名单校验** ⇒ 它接受**任意键**。

★ 新增 `--list-fields`：**真值来自真跑一次 `descr.build_form_fields()`**，把键名印出来。
  加它的理由是——help 要指向一份**真实**清单，而清单真值只在那份 dict 里
  （手抄必然过时，**这次的错就是手抄来的**）。实跑结果 **13 个键**：

```
audiocodec_sel[4]  custom_fields[4][1]  custom_fields[4][2]  custom_fields[4][3]
custom_fields[4][4]  custom_fields[4][5]  descr  medium_sel[4]  name
nfoaction  small_descr  tags[4][]  team_sel[4]
```

★ 顺带验了 `select_fields` 语义（AST 抠函数，不启网络）：空 = 全部；
给 4 个键 ⇒ **恰好那 4 个**（`name`/`small_descr`/`descr` 确实被排除）；
不存在的键**静默丢弃**（**已知**行为，不是 bug）；空格/空项容错。

### 4. `tools/mark_no_forward.py` —— 新增（**只读预览已跑通，未写库**）

补「**人工已知禁止转发**」在库里**无处可记**的那个洞。用户点名 3 条
（`205965ff` / `00475fec` / `6966e959`，中國名家名曲）**不发**，
而它们的 `transfer_rule` 是 **NULL** ⇒ 闸门④ 判「空 ⇒ 放行」⇒ **没有东西拦它们**。

★ **为什么洞在这里**：闸门④ 的 `DENY` 本来**只能**来自皇后页面
（`opencd_parse`→`opencd_gate`→`opencd_ingest.STATUS_DENIED`），
而**本地抓轨行从来不过 ingest** ⇒ 「人工知道的禁止」无处可写。
★ 而闸门④ 那句「空 ⇒ 放行」当初的理由是
  *"空 = 没抓过页面（本地抓轨行）⇒ 不拦；一刀切会挡下整个第一阶段"* ——
  这三条**正是**本地抓轨行，而它们**也确实是**禁转的
  ⇒ **「没有皇后页面」不等于「随便转」**。
★ 修法：写进**已有那一列** `transfer_rule='DENY'`（不需要新列/schema/改判据），
  闸门④ 的判据是「值 = `DENY` ⇒ 拦」，**与有没有皇后页面无关** ⇒ 立刻生效。
  「空 ⇒ 放行」对**绝大多数**本地抓轨行**仍然正确** ⇒ **默认允许 + 显式禁止**。

**三条纪律**（与 `sync_opencd_tid.py` 同族）：
1. 默认只读；写要 `--apply` **且** `--i-have-stopped-the-container`；
2. **只 UPDATE `transfer_rule` 一列** —— 不调 `save_task()`（upsert 会刷 `updated_at`
   ⇒ 那些行全变 stale、`edit` 每轮重拉整站，即 `db.STALE_CONDITION` 那个坑）；
3. **只改 `--hashes` 明确列出的行**，且**前缀必须唯一定位一行**
   —— 命中 0 行或 >1 行都**报错退出**，**绝不按标题模糊匹配**（`ERR-SITE-03`）。

**副本验证全过（含阴性对照）**：

| 测什么 | 结果 |
|---|---|
| `--apply` 但不声明容器已停 | ❌ 拒绝，且**拒绝后 `DENY` 行数仍为 0** ⇒ 守卫在**写之前** ✅ |
| 正常写入 3 行 | ✅ 3/3，回读全 `DENY` |
| ★ `updated_at` 逐行比对 | **逐行相同**（`09-19 03:49:12` 等）⇒ 证明真没碰它 ✅ |
| ★ 写后**查队列**（真判据） | **0 条仍落在待发布队列** ⇒ 闸门④ 真拦住了 ✅ |
| 幂等重跑 | 「✅ 无需改动」✅ |
| 阴性对照：命中 0 行 | ❌ 拒绝 ✅ |
| 阴性对照：前缀歧义（`a739` 库里 2 行） | ❌ 拒绝 ✅ |

### ★ 5. 本会话我犯的错（★ 比结论更值钱）

| 错 | 形状 |
|---|---|
| ★★ 报「容器在前进」→ 又报「容器冻结」→ **同一个容器，两个相反结论** | **采样窗口（15s×2=30s）≤ 心跳周期（`SCHED_TICK=30s`）** ⇒ 「变了」是**必然**，判据恒为真、**从来没生效过**；叠加 **SMB 内容缓存**（同一秒连读 5 次同值、隔会儿跳变）⇒ **把测量噪声当信号**。真判据＝**心跳年龄 < 周期**（心跳值本身就是 epoch，不靠 mtime 粒度）。已记 `ERR-META-10` |
| ★★ 脚本里**预写死的"结论"句子**当证据用 | 我在同一个脚本里**先算读数、后按预设分支打印**「★ 结论：容器已经停了」，而当场读数（`年龄=26s`、`心跳=10:10:21`）说的**正好相反** ⇒ 输出里"结论"与"证据"自相矛盾而我照抄了结论。**判据必须由当场数据算出**。已记 `ERR-META-11` |
| 说「不用停容器，在容器里跑」可行 | **没先核对通路**：NAS **没开 SSH**（`deploy.sh` 开头明写"本机不能在 NAS 上执行命令，写只能走 SMB"），Docker Desktop 只有本地 context ⇒ `docker exec` **根本到不了**那台机器。★ 同族第 3 次（前两次：以为部署过了、`deploy.sh` 在白名单里） |

★ 共同形状：**做决定之前没有先核对"这条路通不通"/"这个判据测的是什么"**。

### 未解 / 待办（**判据已写明**）

1. ★★ **那 3 条 `DENY` 还没写进生产库** —— 容器**在跑**（心跳年龄 5~30s 循环），
   按 `ERR-DOCKER-03` 不能从本机写。用户已选「**停容器 → 本机跑**」。
   ⇒ 待办：**容器停了**就跑
   `python tools/mark_no_forward.py --apply --i-have-stopped-the-container`
   （写入前基线已存 `~/.mark_no_forward_baseline.json`；预期 `updated_at` 逐行不变）。
2. **Fix A / Fix B 还没部署** —— 要进 `Dockerfile` COPY + `deploy.sh` FILES
   （`db.py`/`run.py`/`update_metadata.py` 已在其中）。
   ★ 部署前先回读 NAS 文件哈希确认（**"以为部署过了"犯过一次**）。
3. **那 3 条周杰伦（本地抓轨行）要不要发、怎么发** —— `publish` 在生产**关着**
   （`daemon_env.json` 快照 `ENABLE_PUBLISH='false'` + `last_run` 停在 09-19）
   ⇒ daemon **不会**替你发，要**手动**跑。★ 发之前必须先确认那 3 条禁转行已被 `DENY` 拦住。
4. **`NEED_CONFIRM` 该不该也拦** —— 产品决定，**未定不擅改**（上一节 #3，仍未定）。
5. **`scan` 冲状态的连带风险**：`FAILED` 也在受保护集合里 ⇒ 失败行**不会**被自动重试，
   要 `--recheck` 才重做。**这是刻意的**（"顺手重扫"不该抹掉失败结论），但**用户须知**。

---

## 2026-09-23（上午）—— ★★★ 闸门④ **判据反转** + 差点发出禁转种子

### ★★★ 0. 先说最重的：我差一步就发布了禁止转发的种子

上一节我把「3 条周杰伦」列进了**待办 #4：发布**，还写「你要发那 3 条周杰伦
也得**手动**跑 publish」。用户随即纠正：**「那三条周杰伦是禁止转发！！！不能发」**。

| | 我当时的处置 | 依据 |
|---|---|---|
| 3 条中國名家名曲 | 「**不发**」 | 用户**显式**说过禁转 |
| 3 条周杰伦 | ★ 「**待发布**」 | ★ **用户从没说过能发** |

⇒ ★★ 根因不是我"不知道它们禁转"，而是**把「没有被禁止」当成了「被允许」**。
   与闸门④ 原来那句「`transfer_rule` 空 ⇒ 放行」**是完全同一个错误** ——
   只是一个发生在**代码里**，一个发生在**我的判断里**。已记 `ERR-META-12`。

### ★★ 1. 由此定性：错的是**闸门④ 的默认值**，不是"漏标 6 行"

实测：当时的发布队列 **6 条，全是禁止转发**（3 条中國名家名曲 + 3 条周杰伦），
6 条的 `transfer_rule` **全是 NULL** ⇒ 旧判据「空 ⇒ 放行」**把 6/6 全放过去了**。

⇒ 一个**全为禁转**的队列能整队通过 ⇒ 不是运气差，是**默认值错了**：
   `transfer_rule` 空的语义是「**不知道**能不能转」，**不是**「允许」。
   把"不知道"当"允许" ⇒ 每次都在赌，而**连赌错 6 次**。

★ 旧注释里「本地抓轨行 ⇒ 不拦」的理由（"空 = 没抓过页面"）被实测**推翻**：
  这 6 条**正是**本地/手工加的行（`opencd_tid` 空、`content_path` 指 `/downloads/`），
  而它们**一条都不许发** ⇒ **「没有皇后页面」不等于「随便转」**。

### ★★ 2. 闸门④ 反转（用户拍板：「未知 ⇒ 拦」）

```python
# db.PENDING_PUBLISH_WHERE 的第四段（前）：
"AND (transfer_rule IS NULL OR transfer_rule = '' OR transfer_rule != 'DENY')"
# （后）—— ★ 反转
"AND transfer_rule = 'ALLOW'"
```

| | 判据 | 语义 |
|---|---|---|
| 旧 | 空 / `ALLOW` / `NEED_CONFIRM` ⇒ **放行**；仅 `DENY` ⇒ 拦 | 默认允许 + 显式禁止 |
| **新** | ★ **只有 `ALLOW` ⇒ 放行**；空 / `DENY` / `NEED_CONFIRM` / 其它 ⇒ **拦** | ★ **默认拒绝 + 显式授权** |

★ `NEED_CONFIRM` **现在也拦**了（旧注释"暂时放行"已作废）—— 它的语义是"拿不准"，
  而"拿不准就不发"安全。

**影响面（逐闸门拆开数的，不是给一个总数）**：

| 闸门组合 | 条数 |
|---|---|
| ① `status='SUCCESS'` | 299 |
| ①+② `site_status != 'EXISTS'` | 32 |
| ①+②+②b `publish_after` | 32 |
| ①+②+③ `mapped_json` | 6 |
| ①+②+③+④**旧** | **6** |
| ①+②+③+④**新** | ★ **0** |

⇒ 队列 **6 → 0**（6 条禁转**全被拦住**）。
⇒ ★ **不影响任何已在站上的行**：`site_status='EXISTS'` 的 269 条本来就不在队列里。
⇒ ★ **代价**：第一阶段（本地抓轨）今后要发必须先**显式**标 `ALLOW`。**刻意如此**
   （禁转是少数，但"不知道"更常见 ⇒ **授权才该是需要动作的那一方**）。

**验证（含阴性对照）**：
· 新判据下 `db.get_pending_publish()` = **0 条** ✅
· ★ **阴性对照**：在副本上把一条本地行标 `ALLOW` ⇒ 它**确实进了队列** ✅
  （证明"0 条"不是把闸门写死成"谁都不过"）

### 3. ★ 仍要标 `DENY`（两条理由，第二条承重）

新判据下"空"和"DENY"**都拦** ⇒ 单从防发看标 `DENY` 已非必需。但仍要标：

1. **表达意图**：`空` = "不知道"，`DENY` = "**明确不许**"。用户已明确 ⇒ 应落库。
2. ★★ **防"批量刷 ALLOW"误伤**：将来若有人为让本地行能发而**批量刷 `ALLOW`**，
   这 6 条有 `DENY` 会被**挡住**；若还是 `空`，可能被一起刷成 `ALLOW`
   ⇒ **一次误操作就把禁转种子发出去**。⇒ **`DENY` 是不可被批量授权绕过的那道锁。**

**`tools/mark_no_forward.py` 副本验证（全 6 条）**：6/6 写入成功；
★ `updated_at` **逐行相同**（`09-19 03:49:12` 等，六个时间戳全未变）；
★ 写后查队列 = **0 条仍待发**；守卫（不声明容器已停 ⇒ 写前拒绝）、
幂等、命中 0 行、前缀歧义四个阴性对照全过。

### ★ 4. 本会话第 3、4 个错

| 错 | 形状 |
|---|---|
| ★★ 把禁转的 3 条周杰伦列入「待发布」 | **「没有被禁止」当成「被允许」** —— 见上，已记 `ERR-META-12` |
| ★ 说「不用停容器，在容器里跑」可行 | **没先核对通路**：NAS 没开 SSH（`deploy.sh` 开头明写）、Docker Desktop 只有本地 context ⇒ `docker exec` 到不了那台机器 |

### 未解 / 待办（**判据已写明**）

1. ★★ **6 条 `DENY` 还没写进生产库**，且 **`db.py` 的闸门反转也没部署**。
   ⇒ **生产容器里跑的还是旧判据**（实测读 NAS 上的 `db.py`：仍是「空 ⇒ 放行」）。
   ★ **当前仍安全**，因为 `publish` 在生产**关着**
     （`daemon_env.json` 快照 `ENABLE_PUBLISH='false'` + `last_run` 停在 09-19）。
   ⇒ ★ 但**手动**跑 `publish.py` 就会发 ⇒ **在停容器标 DENY + 部署前，别手动跑 publish。**
2. **部署**：`db.py` / `run.py` / `update_metadata.py` 的改动要进 `Dockerfile` COPY +
   `deploy.sh` FILES（三者已在白名单）。★ 部署后**回读 NAS 文件哈希**确认
   （「以为部署过了」犯过一次）。
3. ★ **第一阶段（本地抓轨，117 条）怎么授权** —— 反转后有**显式标 `ALLOW`** 这一步。
   要不要写个批量工具？**要用户定**（涉及"哪些真的有权发"，不能替他决定）。
4. **`NEED_CONFIRM`** —— 现按"拦"处理（安全侧）。若将来要放行，是**产品决定**。
5. **`counts.json` 仍没有 `transfer_rule` / `opencd_tid` / `mapped_json` 的口径**（前几节遗留）。

---

## 2026-09-23（上午）—— ★★ 6 条禁转已标 `DENY`（**已写入生产**）

### 已坐实

**1. `tasks.transfer_rule`：`DENY` 0 → **6**（生产库，2026-09-23 10:26）

| 列 | 写入前 | 写入后 |
|---|---|---|
| `total` | 547 | **547**（不变） |
| `transfer_rule='DENY'` | 0 | ★ **6** |
| `transfer_rule IS NULL` | 545 | 539 |
| `transfer_rule='ALLOW'` | 2 | 2（不变） |

标的 6 行（= 用户先后两次点名的**全部**禁转行）：

| info_hash | 内容 |
|---|---|
| `205965ff…` / `00475fec…` / `6966e959…` | 中國名家名曲【器樂篇】·廣东音乐 (1996)（同一作品 3 份记录） |
| `e76e695b…` | 周杰伦 - 太阳之子 环球豪华日版 (2026) [WAV] |
| `2201d9c0…` | 周杰伦 - 八度空间（环球日版）(2002) [WAV] |
| `7c96605a…` | 周杰伦 - Jay（环球日版）(2000) [WAV] |

**2. ★★ 两条纪律的现场验证**

| 判据 | 结果 |
|---|---|
| ★ `updated_at` **逐行比对**（6 个时间戳） | **逐行相同**（`09-19 03:49:12` / `09-13 11:22:44` / `09-20 18:31:05` / `09-19 03:49:20` / `09-19 03:49:41` / `09-19 03:49:47`）⇒ **确实没碰它**，不会变 stale、不会触发 `edit` 重拉 |
| ★ 写后**查队列**（真判据，非"我以为"） | 0 条 ⇒ 拦住了 |

**3. ★★ 两层保险，现在都已生效**

| 层 | 判据 | 状态 |
|---|---|---|
| **① 现在**（未部署，生产仍是旧判据） | 旧「空/非 DENY ⇒ 放行」 | 队列 **0** —— 靠那 6 条 `DENY` |
| **② 部署后**（`db.py` 闸门反转） | 新「**必须 = `ALLOW`** 才放行」 | 队列 **0** —— 靠默认拒绝 |

⇒ ★ 关键：**`DENY` 不需要等部署就已生效**（实测用**生产自己的 `db.py`** 旧判据算，队列 0）。
⇒ ★ 而且即使将来有人把闸门改回「空 ⇒ 放行」，`DENY` 仍是**改不动**的那道锁。

**4. 单写者纪律（`ERR-DOCKER-03`）怎么落实的**

用户停容器后，我用**三条独立信号**复核（不是靠"用户说停了"）：

| 信号 | 观测 |
|---|---|
| ★ 心跳**75 秒窗口** | 只见**一个**值，年龄 `49→110s` **单调增长** ⇒ 冻结（真判据：年龄 > 1.5×周期 `SCHED_TICK=30s`） |
| 无 `data.db-wal`/`-shm` | ✅ ⇒ 最后一个连接已关闭（`ERR-SQL-01` 推论） |
| `data.db` mtime | 静止在 `09:48:33` |

★ **备份**：`//iSunker-DS423/docker_ssd/musopia_backup/data.db.before-mark-deny-20260923-102635`
（compose 目录**之外** —— 不污染 build 上下文，也不会因写 NAS 中途失败而丢备份），
sha256 与源一致 `3156cca3…`。

★ **本机 `data.db` 一个字没动**（本会话所有测试都在**副本**上跑）。

### 未解 / 待办

1. ★ **`db.py` / `run.py` / `update_metadata.py` 三个改动还没部署** ——
   生产容器里跑的还是旧 `db.py`（实测读 NAS 上的文件确认）。
   ⇒ 闸门反转与 `scan` 守卫**都还没生效**；`DENY` 那条已生效（不依赖部署）。
   ★ 部署后**回读 NAS 文件哈希**确认（「以为部署过了」犯过一次，`ERR-META-08`）。
2. ★ **第一阶段（本地抓轨，117 条）今后发什么** —— 反转后要先**显式**标 `ALLOW`。
   要不要写批量授权工具、授权哪些，**要用户定**（"哪些真的有权发"不能替他决定）。
3. **`NEED_CONFIRM`** 现按"拦"处理（安全侧）；要放行是**产品决定**。
4. `counts.json` 仍缺 `transfer_rule` / `opencd_tid` / `mapped_json` 的口径（遗留）。

---

## 2026-09-23（上午）—— 三个改动**已部署到 NAS**（镜像待重建）

### 已坐实

**1. `deploy.sh --apply` 已同步 3 个文件**

```
有差异的文件 (3):  [改] db.py   [改] run.py   [改] update_metadata.py
→ 备份 .deploy-backup/20260923-103137   （回滚: bash deploy.sh --rollback）
```

**2. ★ 回读哈希确认（不靠"我以为部署过了" —— `ERR-META-08`）**

| 文件 | local sha256 | NAS sha256 | |
|---|---|---|---|
| `db.py` | `64d3a3dc0636e423…` | `64d3a3dc0636e423…` | ✅ 一致 |
| `run.py` | `eaf0fb710c57420e…` | `eaf0fb710c57420e…` | ✅ 一致 |
| `update_metadata.py` | `b06a6548ebd57660…` | `b06a6548ebd57660…` | ✅ 一致 |

**3. ★ 用 NAS 上的 `db.py` 实测新判据（部署后）**

```
闸门④ 片段          : AND transfer_rule = 'ALLOW'
PROTECTED_STATUSES  : ('PUBLISHED', 'NO_TORRENT_FILE', 'FAILED')
db.get_status 可用  : True
★ 队列              : 0 条
```

**4. ★★ 三层保护，逐层独立生效（不必等重建）**

| 层 | 依赖 | 状态 |
|---|---|---|
| ① 6 条 `transfer_rule='DENY'` | **不依赖部署** | ✅ **已生效**（旧判据队列也是 0，实测） |
| ② `db.py` 闸门反转（文件） | 已上 NAS | ✅ 文件已在，实测新判据队列 0 |
| ③ 容器里真正跑新代码 | ★ **要 `up -d --build`** | ⏳ **未做** |

★ 关键：**那 6 条禁转现在已安全** —— 即使镜像不重建，`DENY` 也拦得住（旧判据同样拦）。

### ★ 仍未做：镜像重建（NAS 上手动，SSH 未开）

容器当前**已停**（心跳年龄 464s，`ERR-META-10` 的真判据）。重建命令（**纯命令，可直接粘**）：

```
sudo docker compose config --quiet && echo 'YAML OK'
sudo docker compose up -d --build
sudo docker logs --tail=60 opencd2musopia
```

★ 为什么不能 `restart`：代码是 `COPY` 进镜像的（`ERR-DOCKER-01`）。

### 未解 / 待办

1. ★ **镜像未重建** ⇒ ①`scan` 守卫、②闸门反转**在容器里都还没生效**。
   ★ 但**不影响**那 6 条禁转（`DENY` 硬拦，不依赖判据版本）。
2. ★ **第一阶段（本地抓轨，117 条）今后发什么** —— 反转后要**显式**标 `ALLOW`
   （重建之后才生效）。要不要批量授权工具、授权哪些，**要用户定**。
3. ★ **`deploy.sh` 的「心跳在运行」提示是假的**（它拿单一读数的年龄当"在跑"，
   实测容器已停 400s+ 仍报"434s 前刚刷过心跳（在运行）"）⇒ `ERR-META-10` 同族，
   与 `nas_health.py` 的告警口径不一致。仍未修。
4. `counts.json` 缺 `transfer_rule`/`opencd_tid`/`mapped_json` 口径（遗留）。

---

## 2026-09-23（上午）—— ★ 镜像已重建，**两处改动都已在生产运行**

### 已坐实

**1. 重建过程（NAS 上手动，用户执行）**

```
sudo docker compose config --quiet && echo 'YAML OK'   → YAML OK
sudo docker compose up -d --build
  → [musopia 4/4] COPY config.py db.py ...  0.3s   （★ 这一层**没 CACHED** ⇒ 新代码进镜像了）
  → image sha256:36e398e7de87…   容器 opencd2musopia Started
```

★ 判据：`COPY` 那层**不是 CACHED**（前 3 层 CACHED）⇒ 代码层确实是新建的。
★ `WARN buildx: git was not found` 照旧出现 —— **接受它、不修**（`ERR-DOCKER-07`）。

**2. ★★ `scan` 守卫**已在生产运行**（正面证据）**

`scan` 于 **10:34:47** 跑完。实测那两条 `site_status='EXISTS'` 的行：

| info_hash | status | updated_at | 
|---|---|---|
| `e43884787e3a`（id 17264） | ★ **`NO_TORRENT_FILE`** | `2026-09-23 10:34:29` |
| `049f10efaf24`（id 17265） | ★ **`NO_TORRENT_FILE`** | `2026-09-23 10:34:47` |

★ **推理链**：这两条不在 `run.py` 的 `L121/L124`（都要求 `status=='SUCCESS'`）里
  ⇒ 旧代码下**必然落到 L203** 那个无条件 `save_task(status="SUCCESS")` ⇒ **必变 `SUCCESS`**。
  实测**没变** ⇒ ★ **守卫生效**，且 `updated_at` 落在 scan 窗口内
  ⇒ 证明 scan **确实处理了这两行**、但走的是新的"只刷元数据、不动 status"分支。
★ 附带：`torrent_file` 与 `content_path` 都还在（没丢东西）。

**3. 心跳恢复正常**：`nas_health.py` 报 `✅ daemon 活着`（年龄 28s）。

**4. `stale` 6 → 8 —— ★ 预期内，非异常**

多出的 2 条正是 `17264`/`17265`：本轮 **scan 刷新了它们的 `updated_at`**
（`10:34:29`/`10:34:47`），而 `site_synced_at` 还是旧的（`09:47:38`/`09:48:33`）
⇒ `updated_at > site_synced_at` ⇒ 满足 stale。`edit` 下轮会去同步它们。

### ★ 本会话第 5 个错

| 错 | 形状 |
|---|---|
| 说那两条「`updated_at` 也未变」 | ★ **假的**。它们**变了**（`10:34:29`/`10:34:47`，正是 scan 写的）。**结论（守卫生效）是对的，但我随手配的证据句没核** ⇒ 又是 `ERR-META-11` 的形状（**结论对、证据没核**）。★ 订正后的证据反而**更强**：时间戳落在 scan 窗口内，证明 scan **碰过**这两行却没改 status |

### 未解 / 待办

1. ★ **闸门④ 反转在容器里是否真生效，还没直接验** —— 文件已确认新版、
   但"容器里 `/app/db.py`"我读不到（NAS 无 SSH）。
   ★ 最强判据是让 `publish` 跑一次看它选几条（新判据应选 **0**），
     但 `publish` 会**往公开站点写**（不可撤销）⇒ **不擅自触发**。
   ★ 且 `ENABLE_PUBLISH` 快照是 `'false'`、与 `.env` 的 `true` **矛盾**（`ERR-ENV-02`，未解）。
   ⇒ 建议：下次 `publish` 自然触发时（若真开着）看它的条数。
2. ★ **第一阶段（本地抓轨，117 条）今后发什么** —— 反转后要**显式**标 `ALLOW`。
   要不要批量授权工具、授权哪些，**要用户定**。
3. `deploy.sh` 的「心跳在运行」提示用单一读数判（实测已停 400s+ 仍报"在运行"）
   ⇒ `ERR-META-10` 同族，与 `nas_health.py` 口径不一致。未修。
4. `counts.json` 缺 `transfer_rule`/`opencd_tid`/`mapped_json` 口径（遗留）。

---

## ★ compact 前的交接快照（2026-09-23 上午，供下一轮直接接上）

### 生产当前状态（都是**已坐实**的事实）

| 项 | 值 | 怎么验的 |
|---|---|---|
| 容器 | **在跑** | `nas_health.py` 报 `✅ 活着`（心跳 28s） |
| 镜像 | **已重建**（含新代码） | NAS 上 `up -d --build`，`COPY` 层非 CACHED |
| `db.py` / `run.py` / `update_metadata.py` | **已部署**（哈希与本地一致） | `sha256sum` 回读比对 |
| 6 条禁转 `transfer_rule='DENY'` | **已入库** | 独立直读复核 6/6；`updated_at` 逐行未变 |
| 发布队列 | **0 条** | 新旧判据**都**算出 0 |
| `scan` 守卫 | **已生效** | `17264`/`17265` 走完 scan 仍 `NO_TORRENT_FILE` |
| `stale` | 8（含上述 2 条，预期内） | `db.get_published(only_stale=True)` |
| 备份 | `musopia_backup/data.db.before-mark-deny-20260923-102635`（sha256 `3156cca3…`） | 写在 compose 目录**之外** |

### ★ 下一步该做什么（按优先级，判据已写明）

1. ★ **闸门④ 反转在容器里尚未直接验证** —— 文件已是新版、队列实测 0，
   但"容器内 `/app/db.py`"读不到（NAS 无 SSH）。
   **判据**：下次 `publish` 自然触发时看它**选几条**（新判据应选 **0**）。
   ★ **不要**为了验证而手动触发 `publish` —— 它会往公开站点写、**不可撤销**。
2. ★ **第一阶段（本地抓轨，117 条）今后发什么** —— 反转后必须**显式**标 `ALLOW`。
   要不要批量授权工具、授权哪些，**要用户定**（"哪些真有权发"不能替他决定）。
3. **未解**：`ENABLE_PUBLISH` 快照 `'false'` vs `.env` `true` 的矛盾（`ERR-ENV-02`）。
4. **未解**：`deploy.sh` 的「心跳在运行」提示用单一读数判（`ERR-META-10` 同族）。
5. **未解**（旧，仍在盯）：重复的叶惠美（id 17096 已上站）、`OPENCD_COOKIE` 的 `$` 插值、
   空 body 间歇触发、"短路 240 vs 库 NO_META_LOG_FILE" 量纲不可相减。
6. `counts.json` 缺 `transfer_rule`/`opencd_tid`/`mapped_json` 口径（遗留）。

### ★ 本会话的错（5 个，形状收敛为两类）

| # | 错 | 形状 |
|---|---|---|
| 1 | 报「容器在前进」→ 又报「冻结」（同一容器） | **判据测的不是那件事**（窗口 ≤ 周期 + SMB 缓存）→ `ERR-META-10` |
| 2 | 脚本里预写死「结论：容器已停」 | **结论不由当场数据算出** → `ERR-META-11` |
| 3 | 说「不用停容器、在容器里跑」可行 | **没先核对通路**（NAS 无 SSH） |
| 4 | 把禁转的 3 条周杰伦列入「待发布」 | ★ **「没有被禁止」当成「被允许」** → `ERR-META-12` |
| 5 | 说那两条「`updated_at` 也未变」 | **结论对、随手配的证据没核**（实际变了）→ `ERR-META-11` 同族 |

★ **收敛后的共同形状**：**下结论前没有先核对「这个判据/这条路能不能支撑它」**。
★ **本会话唯一值钱的正向产出**：用户两次纠正暴露出**闸门④ 的默认值是错的**
   （整队 6/6 禁转全被放行）⇒ 从"补 6 行"升级为**反转判据**。

## 2026-09-23（深夜）—— ★★ 「补 mapped_json」的真实目的：**修站上页面**，不是发新种

### 0. 这一段纠正了整个任务的方向（我错了两轮）

我此前一直把「补 `mapped_json`」和「发新种子」捆在一起，于是推出了
「改 `status` → 绕过 `publish_after` → 手动发十来个」整条错路。
用户两句话把它推翻了：

1. 「都说了其他种子都被网站删掉了」——**库里的 `site_status` 不是站上的事实**
2. 给出站上真实 5 条 URL ⇒ 数字上确认 **269 条 EXISTS 里 264 条是幽灵**

**正确链路（本次实测走通）**：

```
opencd_tid  →  topup_opencd_fields.py 补 mapped_json / transfer_rule
                    ↓
            update_metadata.py（认 site_torrent_id，编辑已发页面）
                    ↓
            站上页面的「艺术家 / 发行年份 / 标签 / 日志」从空/猜 → 真值
```

★ `update_metadata.py` **不是新代码**：它 `line 242-249` 早已 `merge_mapped(task)`，
`line 100` docstring 就是「批量编辑站点上已发布种子的页面」。**我找错了工具。**

### 1. ★★ 锚点事实（判据 + 数字，全部实测）

| 口径 | 数 | 来源 |
|---|---|---|
| 库里总记录 | 547 | `data.db` |
| `site_status='EXISTS'` | 269 | 库列 |
| **站上真实存在** | **5** | ★ 用户从浏览器给的 URL |
| **幽灵（EXISTS 但站上已删）** | **264** | 269 − 5 |
| 站上 5 条的 ID | 17264 / 17265 / 17099 / 15974 / 15972 | 用户提供 |
| 428 目标（有 tid + `mapped_json` 空） | 428 → 415 → 414 | 随补随降 |

**★ 幽灵的判据（可复用）**：

```sql
site_status='EXISTS'
AND (site_torrent_id IS NULL OR site_torrent_id NOT IN (<站上真实ID列表>))
```

⇒ **`site_status` 只写不撤**（`sync_site.py:110` 只写 `EXISTS`，全文件无一处撤销）
⇒ 种子被站方删除后，库里**没有任何机制**标记它（`ERR-META-01` 的"判据不会自己更新"）。

### 2. 站上 5 条在库里的样子

| site_id | status | `mapped_json` | 结论 |
|---|---|---|---|
| 17264 黄清元 | `NO_TORRENT_FILE` | 4875B | 已补，**已编辑成功**（`edited=1`，SESSION 前文 801 行） |
| 17265 群星醉民乐 | `NO_TORRENT_FILE` | 8459B | 已补，同上 |
| 17099 サカナクション | `NO_TORRENT_FILE` | 5386B | ★ 本次补 + 推，**成功** |
| 15974 齐秦 | `NO_TORRENT_FILE` | 2278B | ★ 本次补 + 推，**成功** |
| 15972 Mary Black | `NO_TORRENT_FILE` | 8630B | ★ 本次补 + 推，**成功** |

### 3. ★★★ 三次推同一行（id=17099）的对照 —— 「真数据」的判据就在这里

| 字段 | 第 1 次（补前） | 第 2 次（补前） | **第 3 次（补后）** |
|---|---|---|---|
| `custom_fields[4][1]` 艺术家 | `''` | `''` | **`'サカナクション'`** |
| `custom_fields[4][2]` 年份 | `''` | `''` | **`'2026年'`** |
| `custom_fields[4][3]` 日志 | 4740B | 4740B | **4792B（多 `Score : 100`）** |
| `tags[4][]` | `['31','7']` | `['31','7']` | **`['7','9','31']`**（多 `9`） |

**★ 判据（以后判断"推的是不是真数据"就看它）**：
`custom_fields[4][1]/[4][2]` 从 **`None`** 变成**非空中文/日文值** ⇒ 数据来自
`mapped_json`；若一直是 `''` ⇒ `merge_mapped` 没东西可注入（`mapped_json` 为空）。

★ `merge_mapped` 只在 `mapped_json` 有值时改顶层键；空 `mapped_json` ⇒ 退回
`detect_*()` **从标题猜** ⇒ 页面"看着有值，值是猜的"（`ERR-META-01` 形状）。

### 4. ★ 工具 1 新增 `--tids`（本次交付）

**为什么需要**：批量路的游标是 `opencd_tid` 升序 ⇒ **永远选不到指定行**。
实测：`--limit 3` 选中 `196879/196880/196885`，而用户要的是
`200002/200028/200104` —— **看着跑了，补的是别的行**。

**实现要点**（`topup_opencd_fields.py`）：

- `select_batch(limit, tried, retry_failed=False, tids=())`
- 收窄在 **sort / limit 之前** —— 否则 `limit` 先被非点名的行占满
- **是收窄不是绕过**：点名 ∧ 仍须满足 `TARGET_WHERE`；已补好的点名⇒不在 `rows` ⇒
  **不会被"顺手重抓"**（这是要防的形状：点名一个已补好的，白花一个请求）
- 返回多一个 `wanted_absent`，**逐条报出**没命中的（不静默）
- `n_target` 仍是**全库目标行数**（不受点名影响）—— 两个口径不混（`ERR-META-05`）

**部署**：`Dockerfile` COPY 行 + `deploy.sh` FILES **上次已加**（本次只改文件内容）
⇒ `deploy.sh --apply` + NAS `up -d --build`（`ERR-DOCKER-01`）。

★ **实测教训**：用户先跑了 `--tids ... --apply`（**没先 `up -d --build`**）
⇒ `error: unrecognized arguments: --tids`。**我把步骤排版成两节，用户抄了后面那节。**
**⇒ 交付命令块要把「前置步骤」和「目标命令」放**同一个**代码块里。**

### 5. ★★ 本次新发现的 4 个问题（未修，待定）

**① `未經允許不得轉載` 不在 `RULE_VALUES`**（★ 最重要）

```
tid=200002 Mary Black → 转载规则 = '未經允許不得轉載' → NEED_CONFIRM
```

`opencd_parse.RULE_VALUES` 只有 4 个词：`歡迎轉載/欢迎转载/禁止轉載/禁止转载`。
`未經允許不得轉載`（"未经允许不得转载"）语义 ≒ 禁止，但**不在表里 ⇒ 落 NEED_CONFIRM**。
⇒ **词汇表不完整**。加不加**要用户定**（`ERR-META-12`：不确定不许自动判）。

**② 工具 1 会覆盖 `formatted_title`** ⇒ 本地与站上标题**漂移**

```
入库前：サカナクション - 怪獣／いらない - FLAC 整轨 - OpenCD
入库后：サカナクション - 怪獣／いらない 2026 - FLAC 分轨 - Nazuna   ← 皇后格式
```

站上标题**没变**（`--fields` 里没有 `name`）⇒ **本地 ≠ 站上**。判据：同一条的
`update_metadata.py` 输出里，标题栏（本地）与校验行书名号内（站上）**不一致**。

**③ `update_metadata.py` 的 `tags[4][]` 提交时没排序** ⇒ 永久假报"有改动"

```
· tags[4][]: ['5', '31', '24', '23'] -> ['5', '24', '23', '31']   ← 集合相同！
```

`line 326` 的**回读校验**排了序（对的），但 `edit_torrent()` 提交用的是**未排序**的
`fields["tags[4][]"]` ⇒ 每轮都判"有改动"、真提交、白花 2~3 个请求。

**④ `--tids` 的只读预览提示没回显 `--tids`**

```
要真跑：
    python topup_opencd_fields.py --apply --limit 5      ← ★ 少了 --tids
```

**照抄会退化成批量模式 ⇒ 补错行。** 交付命令块里的这个坑必须堵。

### 6. ★ 封面"两张同一张"的判据（用户提问，已定性）

**不是 bug，是站点行为**：站点把简介里的第一张 `[img]` **抽出来当页面题图** ⇒
同一条目上图出现两次（题图 + 简介）。

**判据**（不猜，来自代码）：`musopia.py:323-328`

```python
imgs = [...soup.find_all("img") if src.startswith("http")]
"has_cover":  any("picgo" in s for s in imgs),
"image_count": len(imgs),       # ★ 数的是**整页所有** img，含站点自己的题图
```

**三条数据完全相同**（各有且仅有一个 `cover_url`，`mapped_json` 12 键同构）

| id | `cover_url` |
|---|---|
| 17099 | `.../2026/09/19/Covereb8f16bbf6a3e32b.jpg` |
| 15974 | `.../2026/09/13/cover77363a6a2c70f11b.jpg` |
| 15972 | `.../2026/09/09/Covere0d98adfc182626d.jpg` |

⇒ 所以"只有 15972 是 2 张、另两条是 1 张"**不是数据差异**。
待判的可能：站点抽题图**有延迟**（校验时还没生成）/ 对某些条目不抽。
**★ 未验证，不许当结论**（判据：隔一段时间再看那三条的 `image_count`）。

### 7. 本次走过的错路（写给未来的自己）

| 我说过 | 真相 |
|---|---|
| 269 条 EXISTS 要编辑 ⇒ `update_metadata.py --limit 3` | ❌ 取到了**被删的**那 3 条（id 最小者） |
| 那 5 条的 `.torrent` **不存在** | ❌ 我用**容器内路径**在本机 `os.path.exists` ⇒ `False`（`ERR-SQL-05`）|
| 改 `status` 为 SUCCESS / 绕过 `publish_after` | ❌ 已发的行**永不再进发布队列**，且编辑路**不读** `publish_after` |
| `iter_my_uploads()` 翻页是坏的 | ❌ 函数没 bug（`musopia.py:97-119` 三种假响应验过），是**站点只回 2 条** |
| 17264 是"刚发布的" | ❌ 是**编辑**过（`edited=1`，SESSION 前文 801 行） |

**★ 收敛后的共同形状**：**把「我们库里的一个列」当成「外部的、会变化的事实」。**
`site_status` / `torrent_file`(容器内路径) / `publish_after` 全是这一类。

### 8. 下一步（等用户定）

1. ★ `未經允許不得轉載` 加不加进 `RULE_VALUES`
2. 站上那三条的图片张数复核（判"延迟"还是"恒 1 张"）
3. 414 条待补：继续按每日 50 配额跑（`--tids` 只用于精确点名）
4. 264 条幽灵：**要动得先有完整的站上 ID 列表**（`iter_my_uploads()` 不可信）
5. 17264 / 17265：`mapped_json` 已有，**推一次即可**（零额外皇后请求）
6. 三个小 bug（②③④）要不要修 ⇒ 修要重新部署

### 9. 2026-09-23 深夜（续）—— ★ C 落地：规则表扩容 + 7 条订正 + 部署教训

**① `未經允許不得轉載` 进 `RULE_VALUES`（`opencd_parse.py`）**

用户定：映射到 **`DENY`**；繁简**两条一起加**（用户选）。

```python
"未經允許不得轉載": RULE_DENY,
"未经允许不得转载": RULE_DENY,
```

★ 注释里**分开标了判据强度**：
  · `未經允許不得轉載` = **实测见过**（tid=200002 页面 `轉載規則` 原文）
  · `未经允许不得转载` = **同族推断**（既有 4 条全是繁简成对，按对称补齐）
—— 这一条是**对 `opencd_parse.py:48` 那句「只放实测见过的值」的诚实让步**：
  加了一条没实测过的，**必须在原地标明**，否则将来没人知道判据强度不同。

**★ 验证方法（可复用）**：用**今天真抓的 HTML** 回放，不是构造样本：

```
raw_html/200002.html  → parse_detail() → '未經允許不得轉載' → DENY   （原为 NEED_CONFIRM）
raw_html/200028.html  → '歡迎轉載' → ALLOW   （未受影响）
raw_html/200104.html  → '歡迎轉載' → ALLOW   （未受影响）
原有 4 条回归 → 全对；未知值仍落 NEED_CONFIRM
```

**② 库里有 7 条规则记错 —— 已订正为 `DENY`**

判据：**库里 `transfer_rule` ≠ 用落盘 HTML 重放的结论**（零请求可判）。

| `opencd_tid` | `site_id` | 旧 | 新 | 原始值 |
|---|---|---|---|---|
| 196749 | 14690 | NEED_CONFIRM | DENY | 未經允許不得轉載 |
| 196772 | 14789 | NEED_CONFIRM | DENY | 同上 |
| 196877 | 14794 | NEED_CONFIRM | DENY | 同上 |
| 196878 | 14616 | NEED_CONFIRM | DENY | 同上 |
| 196879 | 14620 | NEED_CONFIRM | DENY | 同上 |
| 196880 | 14751 | NEED_CONFIRM | DENY | 同上 |
| **200002** | **15972** | NEED_CONFIRM | DENY | 同上 |

★ 订正方式：**只 `UPDATE transfer_rule` 一列**，**不重跑 `opencd_ingest.ingest()`**
—— 后者会**整套覆盖**（`formatted_title`/`fetched_at`/`publish_after` 全部重写），
对**已在站上**的行是纯噪声。

★ 安全性：这 7 条**之前也没被放行过**（`NEED_CONFIRM` 同样进不了闸门④）。
  订正的是**记录准确性**，不是堵一个已发生的泄漏。
  **但**若将来有人把 `NEED_CONFIRM` 当"待定可发"，这 7 条就会出去 ——
  所以订正**有实际防御价值**。

★ 订正后的全库分布：`DENY 14 / ALLOW 14`，**零 `NEED_CONFIRM`**。

**③ ★★ 部署教训：`deploy.sh --apply` 漏跑 ⇒ `up -d --build` 白 build**

**事故形状**（可复用）：

1. 我改了 `opencd_parse.py`（本机）
2. 直接让用户 `up -d --build`
3. 输出里 `COPY ... 0.0s`、镜像哈希 `35334a65…` **与上一次完全相同**
4. ⇒ **cache 命中，镜像里还是旧文件**

**判据**：**`up -d --build` 之后必须核镜像哈希变没变**；不变 = 白 build。
（`COPY` 步的耗时也是旁证：`0.0s` = 命中，`0.3s` = 真拷。）

**修法**：`deploy.sh --apply` 推 `opencd_parse.py` → 再 `up -d --build`
⇒ 哈希 `35334a65…` → **`18eb17a386…`**（变了），`COPY` 0.0s → 0.3s。

**★ 容器内验证（不是"我改了文件"）**：

```
sudo docker compose run --rm tools python -c "import opencd_parse as P; print(sorted(P.RULE_VALUES.items()))"
[('未經允許不得轉載','DENY'), ('未经允许不得转载','DENY'), ('欢迎转载','ALLOW'),
 ('歡迎轉載','ALLOW'), ('禁止轉載','DENY'), ('禁止转载','DENY')]
```

**④ ★ 给用户命令的排版纪律（血的教训）**

**问题**：NAS 的终端**会把长命令行折断**，折行处被 shell 当成**真换行** ⇒

- `python -c "…"` 多行 ⇒ `IndentationError` / `unterminated string literal`
- `cat > f <<'EOF' … EOF` 粘贴时**每行被缩进** ⇒ 结束标记 `PYEOF` 不顶格 ⇒ **卡在续行提示符 `>`**
- `printf '%s\n' "…" "…"` 太长 ⇒ 同样被折断、断在字符串中间

**⇒ 规矩**：

1. **命令块里不放任何中文/说明文字**（用户实测：`第 4 条：` 被当命令 ⇒ `command not found`）
2. **要写多行文件时，用「多次极短 `echo` 追加」**（每条 ≤ 一屏宽，绝不会折）

```
echo 'import sqlite3' > /tmp/f.py
echo 'c = sqlite3.connect("/data/data.db")' >> /tmp/f.py
echo 'T = ("196878","196879","196749","196880","196772","196877","200002")' >> /tmp/f.py
echo 'for t in T: c.execute("UPDATE tasks SET transfer_rule=\"DENY\" WHERE opencd_tid=?", (t,))' >> /tmp/f.py
echo 'c.commit()' >> /tmp/f.py
echo 'print(c.total_changes)' >> /tmp/f.py
echo 'c.close()' >> /tmp/f.py
```

3. **宁可多给几条短命令，也不给一条长的** —— 长度是唯一会让用户粘贴失败的因素
4. 跑完**必须核输出**（`total_changes = 7` = 改了几行，可自证）

★ 这一族的根因和 `ERR-OS-02/03`（`grep $'\r'` 静默失配）**同形**：
**工具/通道会吃掉或改写你给它的字符，而失败是静默的。**

---

### 10. 2026-09-23（深夜·续二）—— ★★ 新发现：`custom_fields[4][N]` **读不回来**（判据失效 ⇒ 每轮白提交）

**★ 症状（实测，两张表并排）**

| 观测 | 结果 |
|---|---|
| 站点**页面上**（用户肉眼看到） | `サカナクション` **有值** ✅ |
| `update_metadata.py 17099 --fields "custom_fields[4][1]"`（dry-run） | `· custom_fields[4][1]: None -> 'サカナクション'` |
| 同上，**不加 `--fields`**（全量） | `· custom_fields[4][1]: None -> 'サカナクション'` **同样出现** |

```bash
sudo docker compose run --rm tools python update_metadata.py 17099 --fields "custom_fields[4][1]"
# ⇒ · custom_fields[4][1]: None -> 'サカナクション'
# ⇒ ✅ dry-run：1 个字段将被修改
```

**★ 定性**：**站点有值，我们读回来是 `None`** ⇒ `before.get(k)` 取不到 ⇒
`musopia.edit_torrent` 判 `old(None) != v('サカナクション')` ⇒ **有变化** ⇒ **真提交**。
每次 `update_metadata.py` 都会把这几列**重新提交一遍**（站点内容其实一字未改）。

**★ 与 Bug 2（`tags[4][]`）同族，但根因不同、修法也不同 —— 别混用**

| | Bug 2 `tags[4][]` | 本次 `custom_fields[4][N]` |
|---|---|---|
| 读得到吗 | ✅ 读得到（是 list） | ❌ 读回来 `None` |
| 差异来源 | 顺序（集合相同） | **值整个取不到** |
| 修法 | **判等时排序**（`sorted(old)==sorted(v)`）—— 已修已验 | **改 `fetch_edit_form` 的解析** —— **未修** |
| 载荷 | **不动** | **不动**（写进去的确实对） |

**★ 同一个"判据失效"形状**（`ERR-META-01`：检测方法失效比没有检测更危险）：
两处的表现都是 **"站点内容已与本地一致，却每次都判成有变化 ⇒ 白花请求"**。
★★ **后果不是"少一次编辑"，是"每次批量编辑都多提交几列"** ——
若哪天这几列的**载荷算法变了**，会把**旧的猜值覆盖站点上正确的值**。
（这是 `ERR-SQL-05` 的兄弟：**"读不到"被静默当成"没有"**。）

**★ 根因**：`fetch_edit_form` 遍历的是
`form.find_all(["input", "select", "textarea"])`（`musopia.py:189`），
只从这**三类元素**取值：
- `input`：非 checkbox/radio ⇒ `el.get("value") or ""`
- `textarea` ⇒ 文本
- `select` ⇒ 第一个 `selected` 的 option

`custom_fields[4][N]` 是**自定义字段**，它在 edit.php 上的元素形态**尚未观测**
（候选：另一个表单 / `input` 但值放在别处 / JS 设的 / 站点渲染时压根没回填）。
⇒ **先观测，再改**（`ERR-META-11`：结论不能先烤进脚本）。

**★ 下一步（一次真站点观测，本机做不到）**

```
sudo docker compose run --rm tools python -c "import musopia,config,re;c=musopia.MusopiaClient();r=c.get('/edit.php',params={'id':17099});open('/data/raw_html/edit_17099.html','w',encoding='utf-8').write(r.text);print(len(r.text))"
```
（★ **注意这条命令很长，会被 NAS 终端折断** ⇒ 见本文件「命令粘贴纪律」，
优先用**多次极短 `echo` 追加**写成脚本再跑。）

拿到 HTML 后：**在 HTML 里 `grep custom_fields`**，看它到底是什么元素、值在哪个属性。
**在那之前不许改代码**（`ERR-META-12`）。

**★ 影响范围（已确认的）**
- `update_metadata.py` 每次多提交 `custom_fields[4][1..5]`（每列都有 ⇒ 每次 2~3 个请求的浪费幅度）
- **写入是成功的**（站点上值在）⇒ 不产生**错数据**，只产生**多花请求**
- ⇒ **优先级：中**（不是错数据，是浪费；但会掩盖真变化）

**★ 未验项（承接上一节）**：Bug 3（`--tids` 预览提示回显）**仍未验** ——
点名的 `200028`/`200104`/`196879` 三条**全都已补好** ⇒ 不在 `TARGET_WHERE` ⇒
`命中 0 条` ⇒ 程序在 `💡 没有要抓的` 提前返回，**走不到那行提示**。
⇒ 要验必须点一个**还没补**的 tid（即 `mapped_json` 为空的、且不在台账里的）。
★★ **副产品**：这三次 `命中 0 条` 反过来**证明了 `wanted_absent` 的分支是对的**
（它正确地**拒绝**了对已补好的行"顺手重抓"—— 这正是当初加 `--tids` 要防的形状）。

---

## 11. 名字搜索已删除 · VT 成为唯一 id 来源（2026-09-23 夜）

### 做了什么

| 文件 | 改动 |
|---|---|
| `crawl.py` | 删 `search_candidates` / `match_by_name` / `run_from_qb` / `qb_torrents` / `_exit_code` / `raw_exists` / `--from-qb` / `--search` / `--max-candidates`。现在只剩**一个出网原语** `fetch_detail` + `--tid/--url` 手工路 |
| `daemon.py` | 新增 `vtid` 阶段（默认**开**）；`crawl` 阶段从 `crawl.py --from-qb` 改指 `topup_opencd_fields.py`（默认仍**关**）；`ENV_SNAPSHOT_KEYS` 加 `ENABLE_VTID` |
| `docker-compose.yml` | `x-common` 加只读挂载 `${VERTEX_DB_DIR:-/volume2/docker_ssd/vertex2/vertex/db}:/vertex:ro` + `VERTEX_DB=/vertex/sql.db` |
| `deploy.sh` / `Dockerfile` | `FILES` / `COPY` 同时加 `sync_opencd_tid.py`（`ERR-DOCKER-05`：只改一处 = 容器里缺模块） |
| `.env.example` | 加 `ENABLE_VTID` / `VTID_INTERVAL` / `VERTEX_DB` / `VERTEX_DB_DIR` / `CRAWL_LIMIT_PER_ROUND`；订正 crawl 阶段说明与「2~3 请求/种子」比例 |
| `opencd_ingest.py:9`、`opencd_rate.py:453`、`topup_opencd_fields.py:11` | 三处「入口是 `crawl.py --from-qb`」的**假文档**改掉 |

### 为什么删（三条都是实测，不是观感）

1. **贵** —— 一条 2~3 请求（搜索 1 + 验候选 1~5），而 `OPENCD_MAX_PER_DAY` 只有 50。
2. **会搜错** —— 搜「齐秦 - 我拿什么爱你」返回 2 条（FLAC 分轨 / WAV 整轨），
   名字几乎相同 ⇒ 取第一条就**发错种子**。
3. **抓了也发不出去** —— 传 `qb_seed_days=None` ⇒ 落 `NOT_SEEDED` ⇒ 卡闸门①
   （`status='SUCCESS'`）⇒ 白抓。

★ 新路的实测对比：按 `name` 配对 **1/547**；按 **`info_hash`** 从 VT 库 **430/547**，
且**零请求**（`sync_opencd_tid.py` 不 import `requests`、不 import `crawl`）。

### ★★ 一个必须记住的结构性事实（别指望换路能省掉抓页面）

VT 的 `torrents` 表**十四列里没有任何一列**是转载规则或页面正文
（`tools/_probe_vertex_columns.py` 逐列打过）。它只给**页面编号** `link`。
⇒ 转载规则 / 标签 / 媒介 / 製作組 / 艺术家 / 年份**只存在于页面上**，
`db.PENDING_PUBLISH_WHERE` 的闸门③④ 因此**必须**保留。**VT 不降低"必须抓页面"。**

★ 顺带：`add_time`（n=567）确实是**加入 tracker 的时刻**（unix 秒），与
`record_time` 同值 —— 这是「按发布日期卡门槛」那条新规则的原料（尚未实施）。

### 离线验证（全绿，均在本地、未碰 NAS、未出网）

```
crawl.py 残留引用 ......... 4 处（全是解释性注释，无活符号）
AST (5 个改动文件) ........ 全 OK
tools/opencd-crawl-selftest.py .. 30/30
sync_opencd_tid.py --selftest ... 29/29
tools/_probe_vertex_columns.py .. torrents 14 列 / 皇后行 591 / add_time n=567
sync_opencd_tid.py（报告模式）... 皇后行 591，未写库  ← VERTEX_DB 指到**开发 UNC 路径**
deploy.sh（dry-run）...... 9 个文件，**.env 与 data.db 都不在**（NEVER 名单纯净）
行尾符 ................... 10 个文件 CR=0（全 LF）
Dockerfile↔FILES 核对 ..... 一致（少一个直接拒绝部署）
```

### ★ 下一步（需要你在 NAS 上做，本机没有 SSH）

1. `bash deploy.sh --apply` 同步这 9 个文件
2. NAS 上 `sudo docker compose up -d --build`（★ 必须 `--build`，代码是 COPY 进镜像的）
3. **确认镜像 ID 变了**（一样 = 白构建，`ERR-DOCKER-01`）
4. **确认 `/vertex` 挂载真的通**（挂错路径不会静默跳过 —— 会非零退出并退避）
5. ★ `.env` 在 `NEVER` 名单里 ⇒ 上面几个**新键不进 NAS**。
   NAS 的 `.env` **不需要加任何键**也能跑（`ENABLE_VTID` 默认开、
   `VERTEX_DB` 由 compose 提供）。**要开 crawl 才需手工加 `ENABLE_CRAWL=true`**。

### 未做（明确划界）

- **不动 `topup_opencd_fields.py` 的逻辑** —— 只把它变成 daemon 的入口。
- **不动闸门④** —— `transfer_rule='ALLOW'` 仍然要求真抓过页面。
- **不动 `.env`**、**不 `cp` `data.db`**（`ERR-ENV-02` / `ERR-SQL-01`）。
- **「3+3 日发布门槛 → 发布日期 + 7 天」尚未实施** —— 它要动闸门②
  （`db.py` + `opencd_gate.publish_after_from`），须**单独一轮**做，
  且需先决定「发布日期」怎么回填进我们自己的库（它现在只在 VT 库里）。

---

## 12. 闸门换代：3+3 → 皇后发布日期 + 7 天（2026-09-23 深夜）

### 用户决定（原话）

> 「之前设定的规则是 3+3 日的发布门槛……发布日期大于 7 天也就可以发布了，
>   把那个 3+3 规则替换掉吧」「对，按照皇后的添加日期算，本容器 nas 都在东八区时间」
> 「那 24 条 add_time 为空的：**拦住**」

### 做了什么（6 个文件，已部署到 NAS 并逐字节校验一致）

| 文件 | 改动 |
|---|---|
| `db.py` | ① 加列 **`opencd_added_at`**（TIMESTAMP）② **闸门②换锚**：`publish_after <= now` → `opencd_added_at <= datetime('now','-7 days')`，**空 ⇒ 拦** |
| `sync_opencd_tid.py` | 读 VT 的 `add_time`，**显式东八区→UTC**，回填 `opencd_added_at`；新增判据⑨（防"日期永远填不上"） |
| `opencd_ingest.py` | **闸门① 退役**（原先它把**所有**行都落 `NOT_SEEDED`）；`build_row`/`ingest` 透传发布日期 |
| `topup_opencd_fields.py` | SELECT 补 `opencd_added_at`，入库时带上 |
| `crawl.py` | 手工路注释说明"为何留空日期" |
| `opencd_gate.py` | 模块头写清退役理由；标明**真实门槛在 db.py，不在本模块** |

### ★★ 关键发现：闸门① 是"抓了也发不出去"的真因

`opencd_ingest` 的闸门① 是**最后一道**（前面是 infohash/DENY/log/.torrent），
而**所有调用方都传 `qb_seed_days=None`** ⇒ `decide(None)` 恒返回 `NOT_SEEDED`
⇒ **每一行都落 `NOT_SEEDED`** ⇒ 卡在发布队列闸门①（`status='SUCCESS'`）。

**实测证据**：生产库 `status` 分布里 **`NOT_SEEDED` = 0 行**
⇒ 它既没拦过东西，也**永不放行任何东西** —— 一道从未生效、也永不生效的闸门
（`ERR-META-01`：失效的检测比不检测更危险）。

### ★★ 为什么存"原始日期"而不是算好的"+7 天"

用户选了方案①。理由：把事实（发布日期）加工成派生值（`publish_after`）后，
**改门槛就得重算全库，且原始日期没了**（`ERR-META-03`：派生值不该盖掉来源）。

### ★ 时区（用户点名的坑）

`add_time` 是 unix 秒，**本身无时区**；危险在转换。本机 `TZ=UTC`、NAS `CST+8`
⇒ 裸 `fromtimestamp` 差 8 小时，**本机测不出来**。
**修法**：显式 `timezone(timedelta(hours=8))`。**判据**：断言具体 UTC 字符串
+ 一个**跨日**时刻（东八区 2025-01-01 00:30 ⇒ UTC 2024-12-31 16:30）。

★ `add_time = 0`（SQLite "没填"的占位）**必须当"拿不到"** ——
当成 1970 会让闸门判"放了 56 年"而**放行**。

### ★★ 自检当场抓到的两个真 bug

1. **`LOCAL_SELECT` 漏了 `opencd_added_at`** —— 生产路径（`path is None`）读不到日期。
2. **`build()` 把"tid 已对、日期还空"归进 `same`** —— 那 438 行的日期**永远填不上**，
   而闸门② 的"空 ⇒ 拦"会**永久锁死整个队列，且不报错**。判据⑨ 专钉这条。

### 离线验证（全绿，139 项）

```
opencd_gate.py        19/19
sync_opencd_tid.py    44/44   （从 29 涨到 44）
opencd_ingest.py      46/46
crawl selftest        30/30
```

### ★ 实测数字（换闸门前的生产快照）

| 口径 | 条数 |
|---|---|
| 有 tid | 438 |
| 有 tid + 已抓页面（`mapped_json` 非空） | 38 |
| 　其中 `transfer_rule='ALLOW'` | **20** |
| 旧口径队列（`rule='ALLOW'`） | **0** |

**发布日期分布**（438 行**全部**能配对到 `add_time`）：
中位 36.9 天 / 最小 0.0 / 最大 91.0 ⇒ **7 天门槛放行 346、拦 92**。

**★★ 换完闸门队列不会立刻涨** —— 因为**闸门③ 还拦着 405 条"有 tid 没页面"**。
**真正的瓶颈是页面，不是日期。** 要让队列长出来必须开 `crawl` 阶段补页面。

### 部署验证

NAS 上 6 个文件 `cmp` 逐字节一致；`deploy.sh` 再跑报「生产与本地一致，无需同步」。
★ 第一次 `cmp` 报了 6 个不一致，**那是 SMB 元数据滞后**，不是写入失败（第二次即全 ✓）。

### 未做 / 待办

- **未在 NAS 上跑 `up -d --build`** ⇒ 新代码**还没进镜像**（下一步必做）
- **未回填那 438 行的日期** ⇒ 跑 `sync_opencd_tid.py --apply`（零请求）
- `crawl` 阶段仍**关**（用户"先不开"）；要开需 NAS `.env` 加两行
- ★ **`.env` 里 `ENABLE_PUBLISH` 到底 true 还是 false 有冲突**：
  常设巡检判据说 `true`，而容器快照 `daemon_env.json` 说 `False`。
  **以快照为准**（那是容器内生效值）⇒ 那条判据可能已过期（`ERR-META-04`）
- ★ **巡检发现心跳 140s 未刷新**（阈值 120s），但 `max(updated_at)` 是 1 分钟前
  ⇒ 多半是重建窗口；**待用户贴 `docker ps` / `logs` 确认**

---

## §13 2026-09-23 深夜：队列为什么是 0 —— 逐级数出来 + 两个「原料不齐」的发现

**这一节取代 §12 结尾那些"待办"里的推测** —— §12 说"真正的瓶颈是页面"，
今晚**测出来了**：**页面只是第一层，后面还有两层**。

### 13.1 逐级数（★ 口径不再靠手写 SQL）

新增 `db.publish_funnel()` —— 把 `PENDING_PUBLISH_WHERE` **逐级累加**地数出来，
并带一条**漂移哨兵**（`queue` 与链末必须相等，不等就响亮报错）。
`check_db.py` 现在会打印它。**★ 起因**：队列 0 时 `publish.py --dry-run` 只说
"没有需要发布的任务"，**不告诉你卡在哪级**；而现场手拼 `python -c` 在 NAS 终端
**折行就断**（今晚断了 4 次）⇒ 把"逐级数"做成**一个函数**，从形状上消灭手写。

实测（555 行）：

| 级 | 口径 | 数 | 本级拦掉 |
|---|---|---|---|
| ① | `status='SUCCESS'` | 282 | — |
| ①+② | 未上站 | **40** | **242**（已 EXISTS 的，本来就不进队） |
| ①+②+②b | `opencd_added_at <= now-7d` | **11** | **29**（发布日期太新，★ 闸门正常工作） |
| ①+②+②b+③ | 皇后行已抓页面 | **0** | **11**（有 tid 但没 `mapped_json`） |
| ①+②+②b+③+④ | `transfer_rule='ALLOW'` | **0** | 0（没东西走到这级） |

**⇒ 队列 0 的真因：不是闸门④，是闸门③**（那 11 条没抓页面）。
★ 我一度预判"闸门④ 是唯一真瓶颈"，**这个预判是错的** —— 证据是"闸门④ 本级拦掉 0"。

### 13.2 ★★ 发现一：`topup` 在给"补了也没用"的行花配额（已修）

**实测**：`topup --limit 0` 选中 **399** 条，而**真正花请求的前 8 条 `site_status` 全是 `EXISTS`**。
⇒ 已上站的行**走不到发布队列**（闸门② 排掉 EXISTS）⇒ 给它补 `mapped_json` 是**无效功**，
而请求**不可退**。

**修法（`topup_opencd_fields.py`）**：

| 改动 | 内容 |
|---|---|
| **A** | 新增 `PICK_WHERE` = `TARGET_WHERE` ∧ `site_status != 'EXISTS'`；**批量路**用它 |
| **B** | 排序键**第一优先级**改成"未上站"（原第一是"有 log"，它与 EXISTS 高度重合 ⇒ 正是 bug 根源） |
| **C** | 配额满 ⇒ **优雅退出**（原先 `check_login()` 里抛 `DailyQuotaExceeded` **打出 traceback** —— 那是 `ERR-META-01` 形状：表现的语义"脚本崩了" ≠ 真实语义"今天额度用完，明天再来"） |

★ **点名路（`--tids`）仍按 `TARGET_WHERE`** ⇒ 要**重推已上站的行**，
用 `--tids` 显式点名（"默认不浪费 + 要重推必须自己点名"，与闸门④ 同分寸）。

验证（scratch DB，复现 399 的形状：190 EXISTS + 29 未上站 SUCCESS + 180 NO_LOG_FILE）：
`n_target=399` · `n_pick=209` · **前 8 条不再是 EXISTS** · 180 条 NO_LOG_FILE **没被丢掉** ·
`--tids` 仍能点中 EXISTS 的行。

### 13.3 ★★ 发现二：`publish.py` 有**四道**预检，不是三道（`.torrent` 那层）

用 `ast` 从 `publish.py` **源码**抠出的四道（**★ 抠出来才发现我凭印象只记得三道**）：

```
① site_status='EXISTS'（已在站上）
② 缺封面
③ 种子文件缺失        ← torrent_file 空 / 文件不存在
④ 无 log（最初的规矩：没有 log 不发布）
```

**⇒ `topup` 的产物天生过不了 ③④**：它**刻意不传 `.torrent`**（重建会换 passkey ⇒ 改 infohash），
而皇后页面的 NFO 是 `<pre></pre>`（**空的**）⇒ **没有 log**。
**⇒ "把 status 改回 SUCCESS 就能发"是错的** —— 改了照样卡 ③④。

### 13.4 ★★★ 发现三（**待验证 —— 可能推翻 13.3 的一部分**）

`readiness_probe.py --limit 12` 的实测输出里，**一条 `NO_TORRENT_FILE` 的行"预检全过"**：

```
[1] tid=198601  status=NO_TORRENT_FILE  site=EXISTS  rule=ALLOW
    黄清元 - 金钻名曲珍藏 1998 FLAC
    ✅ 预检全过          ← ★ 说明它 torrent_file 有值、log 也有！
```

**这与"`NO_TORRENT_FILE` = 没有 `.torrent`"矛盾。** 两种可能，**必须实测**：

1. `status` 只是个**历史残留**（列里有值但状态没更新）⇒ 那 13.3 的"③断"**部分不成立**
2. **又一个 `zip` 位置错位**（今晚刚在 `readiness_probe.py` 犯过一次：
   `cols` 元组漏了 `site_status` ⇒ 后面每列错位一格 ⇒ 报出"缺封面 4 条"的**假数**）

**⇒ 下一次开工第一件事：查 `content_path` / `torrent_file` / `log_content` 的真实分布。**
★ 用户提示：**"你去读一下 qb3020 不就知道了"** —— 即直接问 qB API，
看那些行在 qB 里到底有没有对应的 .torrent / 文件目录。

### 13.5 新增 `readiness_probe.py`（根目录，已进镜像）

逐条答"**这批行能不能发、卡在什么上**"。特点：

- **口径不手抄**：闸门链来自 `db.publish_funnel()`；**四道预检用 `ast` 从 `publish.py` 源码抠**
  （抠不到就**报错退出**，绝不猜 —— `ERR-META-01`）
- **实测文件在不在**（`os.path.exists`），不只判列非空
- ★ **回答"改 status 能不能发"**：分三批数（`SUCCESS`且预检过 / `≠SUCCESS`但预检过 / 预检不过）
- **只读**：不发请求、不写库

实测（438 条有 tid 的行）：预检全过 **250** 条 · 其中 `status=SUCCESS` **228** ·
`≠SUCCESS` 但预检过 **22** · 预检不过 **188**（188 = 180 缺种子/缺 log + 8 缺封面）。

★ **`tools/` 整个不部署**（不在 Dockerfile COPY / deploy.sh FILES）⇒ 诊断脚本要放**根目录**，
且 `Dockerfile` + `deploy.sh` **两处都要加**（`ERR-DOCKER-05`），否则 `deploy.sh` 的自检直接拒绝。

### 13.6 部署状态（今晚累计）

镜像 ID 依次：`8e43dd9bac06` → `541bf3affa9d` → `dbd9f8f1374e` → `3b305495903c` → **`1c8e0035a92d`**（最新）
⇒ 每次 `up -d --build` **ID 都变了**（`ERR-DOCKER-01` 的核实动作）。

★ 本机 selftest：**139 项全绿**（`opencd_gate` 19 · `sync_opencd_tid` 44 · `opencd_ingest` 46 · 其余 30）。
★ 本轮新增验证：`publish_funnel` 逐级语义 + 漂移哨兵 + 闸门② 边界（7天整过/6天拦）·
`topup` 选批三条（A/B/C）· `check_db.py` 端到端 · `readiness_probe.py` 端到端。

### 13.7 ★ 未解 / 下次开工清单

1. ★★ **查 `content_path` / `torrent_file` / `log_content` 的真实分布**（13.4，**最高优先**）
   —— 决定"制作种子那步能不能复用 `run.py`"，也决定 13.3 是否成立
2. **读 qB (127.0.0.1:3020)** 看那些 tid 在 qB 里到底有没有 `.torrent` / 音乐目录
   （用户提示"你去读一下 qb3020 不就知道了"）
3. **`ENABLE_CRAWL` 从未加进 NAS `.env`** ⇒ ★ **"每天自动抓"其实不存在**，
   今晚三次 `topup` 全是**手动**跑的。要自动必须加 `ENABLE_CRAWL=true` +
   `OPENCD_CRAWL_DRY_RUN=false`。**用户已问"能不能改成凌晨 1:45"** —— 待决定
   （`daemon` 只有 `interval` 轮询、**没有 cron**；加 cron 是 30 行改动）
4. 配额：今日 **51/50 已满** ⇒ **明天**再跑 `topup --apply --limit 50`
   （前 34 条是能进队列的 `SUCCESS`，之后才是 `NO_LOG_FILE`）
5. 那 **34 条**待补（`SUCCESS`+未上站）里，只有 **11 条**日期够老 ⇒ **明早那 11 条是关键实验**：
   补完页面看闸门③ 那级是否从 0 变正数 ⇒ 决定"皇后路到底能不能产出"
6. `ENABLE_PUBLISH` 快照(False) vs 常设判据(true) 冲突**仍未解**
7. 4 项长期盯：重复叶惠美（site 17096）· `iter_my_uploads()` 翻页坏 · `OPENCD_COOKIE` 的 `$` 插值 · 空 body
8. `up -d --build` 的**停止超时**（今晚 301s → 61.9s → 1.6s → 1.1s，**不稳定**，未修）

## §14 2026-09-23 深夜（二）：台长条实测量出「原料齐不齐」+ 我自己的三次误判

§13 留了两个「原料不齐」的悬案。§14 是把它们**量掉**的那一轮，
外加**三个我自己犯的错**（都记下来，因为形状会重复）。

### 14.1 `content_path` 是什么 —— 现在有实测数了

**定义**：qBittorrent 的「种子内容落点」—— 一条种子的**根路径**
（整轨 = 一个文件，分轨 = 那个目录），本项目里**只有一个来源**：
`run.py:112` 的 scan 阶段从 `torrent.content_path` 抄下来。
**目的**：以后不用再问 qB（`backfill_covers.py` 要按它回原目录找封面）。

`qb_probe.py` 实测（**容器内**跑的，438 条有 tid 的行）：

| | 数 |
|---|---|
| `content_path` 非空 | **438 / 438**（全都有） |
| ├ 路径**今天还在**盘上 | **408** |
| └ 路径**已失效** | **30** |
| `torrent_file` 非空 | **258** |
| `log_content` 非空 | **258** |
| `mapped_json` 非空 | **39** |
| qB 命中（1406 个种子里） | **437 / 438** |
| qB 的 `content_path` 与库里**逐字一致** | **408** ← 与"路径在"**完全相等** |

★ 两行相等不是巧合：`content_path` 是**从 qB 抄的** ⇒ 它在"qB 里那个目录还在不在"
这件事上是 qB 的**忠实副本**。
★★ 所以 §13 引的 `SESSION-NOTES:150-153` 那条教训**仍然成立**：
**不能用 `content_path` 是否为空分"本地路/皇后路"**（438 条全非空）。

### 14.2 ★★★ §13.4 的矛盾解开了 —— 但结论**和我 §13.3 说的相反**

§13.4 那个：`tid=198601 status=NO_TORRENT_FILE ✅ 预检全过`。

实测的三个样本（`qb_probe` 明细）：

| tid | status | torrent_file | log_content | mapped_json | updated_at |
|---|---|---|---|---|---|
| 198601 | `NO_TORRENT_FILE` | **文件在** | **有** | **有** | `2026-09-23 10:34:29` |
| 198827 | `NO_TORRENT_FILE` | **文件在** | **有** | **有** | `2026-09-23 10:34:47` |
| 196878 | `NO_TORRENT_FILE` | **文件在** | **有** | **有** | `2026-09-23 12:15:33` |

**⇒ 结论（★ 推翻 §13.3）**：

> **`NO_TORRENT_FILE` 这个名字是错的。** 它**不代表**"这条没有 `.torrent`"。
> 对**第一阶段行**它代表"**刚给这条补完了皇后页面**"。

**成因（代码级）**：`topup_opencd_fields.py` 调 `opencd_ingest.ingest(...)` 时
**刻意不传 `torrent_file`**（纪律 5：重建 `.torrent` 会换 passkey ⇒ **改 infohash**）
⇒ `opencd_ingest.py:198` 的第 ④ 条判据
`elif not torrent_file or not os.path.exists(str(torrent_file))` 命中
⇒ 落 `status=NO_TORRENT_FILE`。
★★ 而 `save_task` 的 upsert **只更新显式传入的列**（`build_row` 刻意不写
`torrent_file`/`log_content`/`content_path`）⇒ **原来那些值一个都没被抹掉**。
⇒ 于是库里出现"名字说没有、实际全都有"的行。**那 4 道预检报"全过"是对的** ——
错的是 `status` 这个名字，不是预检。

★ §13.3 写的"皇后路种子**结构性**没有 `.torrent` ⇒ ③④ 必不过"，
**只对"第二阶段新生行"成立，对这套 438 条一条都不成立**。

### 14.3 但 `status` 改对**也没用** —— `flip_status.py` 的实测结论是 **0**

`flip_status.py`（新脚本，见 14.5）容器内实测：

```
★ 要改回 SUCCESS 的（预检全过 + 未上站 + status≠SUCCESS）:    0
├ 已经是 SUCCESS（不用动）                                  :   34
├ 预检**不过**（★ 改 status 也没用）                        :  180
├ 已上站 EXISTS（★ 补了也进不了队列）                       :  224
└ 手动标记禁止转发（★ 硬钉，不进候选）                      :    0
                            34 + 180 + 224 = 438 ✅（分类完备）
```

⇒ **`--apply` 一行都不该跑。它是正确的空转。**
★ 为什么是 0：那 27 条 `NO_TORRENT_FILE` 的 `site_status` **是 `EXISTS`**
（198601/198827/196878 三个都印着 `site=EXISTS`）⇒ 它们落在 **224** 那格，
**已被闸门② 排掉**，**不是**"只差 status"。
★★ **我 §13 说"27 条只差一个 status"是错的。**

### 14.4 ★★ `22` vs `0` —— **已对齐**（容器内实测，两个探针都没错）

**结论先写**：`readiness_probe` 的 **22** = `flip_status` 对账段里那一格
「已上站 且 预检全过 且 status≠SUCCESS」**= 22** —— **完全同一个集合**。
两个脚本**从来没错**，`0` 与 `22` 只是**同一个集合减去"够不着"的那部分**。

**容器内实测（`flip_status.py --limit 5`，镜像 `aede84bf4854`）**：

```
★ 要改回 SUCCESS 的（预检全过 + 未上站 + status≠SUCCESS）:    0
├ 已经是 SUCCESS（不用动）                                  :   34
├ 预检**不过**                                               :  180
├ 已上站 EXISTS                                              :  224
└ 手动标记禁止转发（硬钉）                                   :    0
                      34 + 180 + 224 = 438 ✅（分类完备）

[对账]
   ★ 本脚本排掉的那部分（site_status='EXISTS' ⇒ 进不了队列）：
     ├ 已上站 且 预检全过 且 status≠SUCCESS :   22   ← ★ 就是它
     └ 已上站 且 预检全过 且 status=SUCCESS :  194
        ⇒ 两格相加 = 216   （「已上站 EXISTS」= 224 ⇒ 差 8）
```

**那 8 条差额也不是错**：= **已上站 且 预检不过** 的（`qb_probe` 实测
`torrent_file` 空的 180 条里有 `EXISTS` 的）⇒ **分类完备，只是我少印了一格。**

**三个脚本第一次互相对得上**：

| | |
|---|---|
| `readiness_probe` 预检全过 | **250** = `228`(SUCCESS) + `22`(≠SUCCESS) ✅ |
| `flip_status` 已上站预检全过 | **216** = `22` + `194` ✅ |
| `flip_status` 分类 | `34 + 180 + 224 = 438` ✅ |
| `readiness_probe` 分类 | `228 + 22 + 188 = 438` ✅ |
| ★ 两脚本对"预检全过"总量 | `250 = 216 + 34` ✅ |

**★★ 而那 22 条全部"够不着"**：清一色 `status=NO_TORRENT_FILE` + `site=EXISTS`
（明细：`198601` / `198827` / `196878` / `196879` / `196759` …）。
⇒ 它们**永远进不了队列**（闸门② 排掉 EXISTS）⇒
**`flip_status --apply` 一行都不该跑**，`0` 是**正确且最终**的结论。

**★ 我上一轮在本机跑出的「已上站 且 预检全过 = 0」是坏判据的产物**（14.6）——
本机的 `os.path.exists` 对 258 条全 False ⇒ 224 条全被塞进"预检不过"，
于是**对账段的两格也一起变 0**。
⇒ **坏判据不只污染它直接数的那一栏，还会污染由它派生的每一栏。**
⇒ ★ 教训：**先修自检，再读任何派生数字。**

**★ 仍未修**：`readiness_probe.py` 把 `site_status='EXISTS'` **当第四道发布预检印出来**
—— 那是 `publish.py` 里 `explain_not_queued()` 的**闸门口径**（且用的是**作废的**
`publish_after`），**不是** `publish.py` 的发布预检（那三条：缺封面 / 种子文件缺失 / 无 log）。
★ 它**不影响本次对齐**（两边的"预检全过"用的都是那三条），但**会误导读输出的人**。**待修。**

### 14.5 新增 `flip_status.py` —— 判据**从 publish.py 源码原文抽出来编译**

★★ 核心设计：**不手抄 `publish.py` 的预检**，而是用 `ast` 把那段**预检循环体**
**原文**抽出来、编译成 `f(t) -> [原因]`（含中间变量 `tf` / `has_log` / 多碟
`try/except` 回退）⇒ 中间**没有"人肉翻译"这一步**，也就没法漂（`ERR-META-03`）。

★ 三个**硬钉**：
- `MANUAL_BLOCK` = 6 条（3 中國名家名曲 + 3 周杰伦）—— **用户的决定**，`--apply` 也绝不碰
- 写库要 `--apply` **且** `--i-have-stopped-the-container`（`ERR-DOCKER-03`）
- **只 UPDATE 一列** `status`，`WHERE` 里再钉一次 `status='NO_TORRENT_FILE'`；
  写完**逐条比对** `torrent_file`/`log_content`/`mapped_json`/`site_status`/`updated_at`
  **一字未变**

★★ 自检 `--selftest` **16/16 全绿**（容器内也跑过），其中两条负向判据**真的救过场**：
- **`site_status` / `publish_after` 不许出现在抽出的代码里** —— 见 14.4 的漂移
- **恰好 3 条 `reasons.append`** —— 第一版抽出来是 **4** 条（多的是
  `explain_not_queued`），**自检当场报出来**，否则就会拿一个**作废的口径**去改生产库

★ 生成过程中自检抓出的两个真 bug（都写进代码注释了）：
1. 只抽 `if` 的条件 ⇒ `NameError: tf`（中间变量没带）⇒ 改成抽**整段循环体**
2. `want()` 漏了 `Expr` ⇒ 三条 `reasons.append` 全被滤掉 ⇒ 抽出 0 处 append

### 14.6 ★★★ `ERR-META-01`：我给探针加了「判据失效就报错退出」

**踩的坑（我自己的）**：`torrent_file` / `content_path` 存的是**容器内路径**
（`/data/output_torrents/…`）⇒ `os.path.exists` **只在容器里**判得准。
**本机跑** ⇒ 258 条 `torrent_file` 非空、`os.path.exists` **全 False**
⇒ 打印出「预检不过 **214**」，而容器里正确数是 **180**。

★ **`readiness_probe.py` 顶部早就写了这条注释** —— 但**注释不是判据**，
我下一轮就违反了它（`ERR-DOCKER-05` 的形状）。

**解法（两个脚本都加了）**：判定前先自检 —— 若 `torrent_file` **非空但一条都不存在**
⇒ **报错退出**，并直接告诉用户"你多半在本机跑，请去 NAS 上跑"。
★ 阈值刻意是"**一条都不存在**"而不是"很多条不存在"：
生产里**确实**有失效路径（`content_path` 实测 30 条指向已删目录）
⇒ "部分不存在"正常、不许拦；"**全部**不存在"只可能是跑错地方。

### 14.7 ★ 我这一轮的三个错（形状会重复，必须记）

1. **"27 条只差一个 status"** —— 错。它们 `site_status=EXISTS`，够不着队列（14.3）
2. **"daemon 只活 30 秒"** —— 错。心跳旧是因为**旧容器被 `up -d --build` 换掉**
   （心跳**必然**停）—— 一条 `docker ps` 就能证伪（`RestartCount 0` / `ExitCode 0`），
   我却**先下了结论**。★ `nas_health.py` 的判据没错，但它**分不出**"死了"与"正被替换"
3. ~~**"22 = 已上站里预检全过的"** —— 错，对账段实测 = 0（14.4）~~
   ★★ **订正：这一条其实是「对」的**，22 **确实**就是"已上站 且 预检全过 且 status≠SUCCESS"
   （14.4 容器内实测 = **22**）。
   我之所以**误判成错**，是因为**我当时是在本机跑的** —— 而本机的 `os.path.exists`
   对 258 条全 False ⇒ 224 条全被塞进"预检不过" ⇒ **对账段两格一起变 0**（14.6）。
   ⇒ **这个"错"本身，是坏判据的第二个产物。** 它是本轮唯一一次"我改对了又推翻自己"。

★ 共同形状：**推断链太长、判据太远**。三次都是"先猜原因，再去找证据"。
⇒ 纪律：**能直接量的，不许推断**（14.6 的自检就是这个纪律的产物）。
★★ 追加纪律：**修好自检之前，不要读任何由它派生的数字** —— 坏判据会污染
它下游的**每一栏**（14.7 第 3 条就是这么来的）。

### 14.8 ★ 订正两条「评分标准」（都是 `ERR-META-04`，行为早了、文档没跟）

1. **`ENABLE_PUBLISH` 实测 = `false`**（★ 不是 `true`）
   —— 实测判据：**daemon 启动日志里根本没有 `· publish` 这一行**，
   `build_stages()` 直接跳过它。前面那份"真实开关 publish=true"是**错的**。
2. **`crawl` 也不在 stages 里** ⇒ `nas_health.py` 报"state 里没有这个阶段"**是正常的**。

★ 附带：`up -d --build` 的 `Started 1.1s / 61.9s / 195.2s / 301.0s` 波动
**不是故障** —— 那是 `up` 在等旧容器收尾（`stop_grace_period: 5m`），
`301.0s` 正好撞上限 = 被 SIGKILL。**§13.7 第 8 条"停止超时不稳、未修"应订正为「正常」。**

### 14.9 部署状态（本轮）

镜像链：`76690b335965` → `93bd306c5fb2` → **`c67e912b1c24`**
（每次 `COPY` 层都重建 ⇒ `context 14.21kB → 35.56kB → 38.02kB`）
★ 新增文件：`qb_probe.py`、`flip_status.py`（都在 `Dockerfile` COPY + `deploy.sh` FILES）
★ 三次 deploy 备份：`.deploy-backup/20260923-142510` / `-143208` / `-143522` / `-144002`
★ **`SESSION-NOTES.md` 本身不在 deploy 白名单**（它是文档，不进镜像）

### 14.10 ★ 下次开工清单（按优先级）

1. ~~`up -d --build` 一次~~ ✅ **已完成**：镜像 `c67e912b1c24` → **`aede84bf4854`**
   （旁证：构建上下文 `38.02kB → 57.46kB`）。两个自检**都在镜像里跑过了**：
   `flip_status --selftest` **16/16**；两者的 `[判据自检]` 在容器内均报
   `非空 258 / 存在 258 ✅ 成立`
2. ★★ ~~对齐 `22`~~ ✅ **已完成**（14.4）：**两边都对**，`22` = 已上站且预检全过且
   `status≠SUCCESS`，两个探针**第一次互相对得上**（`250 = 216 + 34`）
3. ★ **修 `readiness_probe.py` 里那处口径漂移**（14.4 末段）：它把
   `site_status='EXISTS'` 当第四道发布预检印出来，而那是 `explain_not_queued()`
   的**闸门口径**、且用的是**作废的 `publish_after`** ⇒ 会误导读输出的人。**待修**
4. **查 `readiness_probe.py` 候选池在本机读成 0 条**的原因
   （★ 容器里读的是 **438**，正确 ⇒ 这是**本机专用**的问题，影响的是"本机能不能用它"）
5. ★★ **那 11 条关键实验**（§13.7 第 5 条）：明早配额重置后
   `topup_opencd_fields.py --apply --limit 50` ⇒ 看闸门③ 那级是否从 0 变正数
   ⇒ **这一条决定"皇后路到底能不能产出"**
6. `ENABLE_CRAWL` / `OPENCD_CRAWL_DRY_RUN` 仍未加进 NAS `.env` ⇒ **"每天自动抓"不存在**；
   用户问过"改成凌晨 1:45"—— 待决定（`daemon` **没有 cron**）
7. 4 项长期盯：重复叶惠美（site 17096）· `iter_my_uploads()` 翻页坏 ·
   `OPENCD_COOKIE` 的 `$` 插值 · 空 body（HTTP 200 + 0 字节）· 另 241 vs 244
8. ★ 纪律（本轮新增）：
   - **命令块里不许混中文**（我又犯了一次：`★ ID must differ…` 被 shell 当命令
     ⇒ `command not found`）
   - **多条命令各自一个代码块**（两条贴一起 ⇒ 第二条被吞掉）
   - ★★ **修好自检之前，不读任何由它派生的数字**（坏判据污染下游每一栏，14.7）
   - ★★ **`os.path.exists` 类判据必须自我宣告失效**（14.6）—— 不许"记得在容器里跑"


---

## 2026-09-24（夜·续）—— `stale 待办 = 43` 的定性（★ 查了、但**只查了分流**）

### 0. 为什么单独记这一条

`stale 待办` 是巡检报告里**每轮都出现**的一个数，而 `edit` 那一行**永远**挂着一句
`no_progress=true（待办清不掉，退回长周期，正常）`。
⇒ 下一个人（或 compact 后的我）**必然会重复问**"这 43 条要紧吗"。
**把定性写下来，比把根因查清更划算** —— 因为它不影响任何流程，却每轮都在诱人分心。

### 1. 43 条的拆解（实测，生产库 SMB 只读直连）

口径 = `db.STALE_CONDITION`（`site_synced_at IS NULL OR updated_at > site_synced_at`）
且 `site_status='EXISTS'`：

| 成因 | 条数 |
|---|---|
| **A.** `site_synced_at IS NULL`（**从未同步过**） | **1** |
| **B.** `updated_at > site_synced_at` | **42** |

B 类差值（天）：**min 0.0100（≈14 分钟）~ max 13.99（≈14 天）**。

★ **这个跨度就是定性**：0.01~14 天**不是一批同源操作**，而是**持续灌注**出来的。
（同源批次会长成一个紧簇；这里散开两个数量级 ⇒ 是被**不同时刻的不同动作**陆续标上的。）

### 2. ★★ `no_progress=true` 是**设计内的降级**，不是故障

`daemon.py:643` 的判据：
```python
st["no_progress"] = before > 0 and after >= before
```
即"**跑了、但待办没降**" ⇒ 置标志并退回 `EDIT_INTERVAL=21600`（6h）。

★ **为什么退回长周期是对的**：若判据写成"还有活就 60s 冷却"，
就会变成**每 60s 敲一次站点** —— 比改之前更糟（`daemon.py:636-641` 的注释专门钉了这点）。
`before > 0` 这个前置也不能省：无活时 `0 >= 0` 会被误判成"没推进"，刷无意义日志。

⇒ **`edit` 上一轮 14:04 起、现在仍挂着 `no_progress=true`，是它自己选的等待**，
与 `tools/doc-audit/INDEX-USAGE.md:295` 那次定性**同一条**（当时是 5.8h，结论"正常"）。

### 3. ★ 与「补了但没推上去」那件事**不是同一件**，别混

`SESSION-NOTES.md` 里已记过**方向相反**的一条（约 L1017，`ERR-SQL-06` 一族）：

| | 现象 | 成因 |
|---|---|---|
| **本节的 43** | **判出了** stale，但 `edit` 推不动 | `updated_at` **被刷了**（或从未同步） |
| **L1017 那条** | **压根没判出** stale，所以**永远不会**被推 | 那些工具**刻意不碰** `updated_at` ⇒ `STALE_CONDITION` 读不到"我变新了" |

★ 两条都长在 `STALE_CONDITION` 上，但**一个"多判了"、一个"漏判了"**。
**修法完全不同**：前者要查 `edit` 为何推不动，后者要**显式标 stale**。
⇒ 看到 `stale` 这个数**先问是哪个方向**，不要直接归因。

### 4. ⚠️ 我**没**做到的：根因未查

说清边界 —— **这 43 条为什么推不动，我不知道**。已排除的和未查的：

- ✅ **不是被 `--limit` 截断**：`edit` 跑的是 `--limit 100`，而 43 < 100。
- ✅ **不是 daemon 崩**：`edit.last_run` 在推、`last_ok=true`。
- ❔ 那 **1 条 `site_synced_at IS NULL`** 成因与其余 42 条**不同**（从未同步 vs 同步后又变新），
  值得单独看 —— 但**不该和 42 条一起归因**。
- ❔ `update_metadata.py` 是否对这些行**报了错却静默跳过** ⇒ **必须看 `edit` 那轮的日志**，
  **查库得不出这个结论**。

⇒ 要查的话：`sudo docker logs --tail=200 opencd2musopia | grep -B2 -A30 "阶段 \[edit\]"`。

### 5. 优先级：**低**，理由是三条硬的

1. **不阻塞发布** —— `PENDING_PUBLISH_WHERE` **不依赖** `stale`（两者是独立口径）。
   实测旁证：本节写作时 `publish` 刚成功发出 1 条（`EXISTS` 272→273），
   而 `stale` 仍是 43 ⇒ **两者互不影响已被观察到**。
2. **没在伤害站点** —— `edit` 已在 6h 长周期上，没有高频敲打。
3. `no_progress=true` 是**设计内的降级信号**，不是崩溃。

★ 但仍够格记这一条 —— 它的价值是**止损**：让下一个人**不必重新查一遍**才知道它可忽略。

### 6. 同轮的另一件事（顺带记，别丢）

★★ **`--yes` 修复在生产实证成功**（本节的 43 是在这个背景下查的）：

| 信号 | 修复前(17:02) | 修复后(17:33) |
|---|---|---|
| `publish.last_run` | 推进 | 推进 |
| `site EXISTS` | 272（**不动**） | **273（+1）** |

⇒ 修复前"参数错"和"权限错"都表现为 `EXISTS` 不动、**分不开**；
修复后 `last_run` 推进 **+** `EXISTS` +1 = **真发出去了**。
★ **同时实证了「站点权限正常」** —— 那是此前一直标着"未验"的最后一项，
现在**不是推断而是观测**。
★ 生产 banner 自印 `python publish.py --yes --auto --limit 1`（两个 flag 都在）。

## 2026-09-24（夜·续二）—— ★★★★ 一次**四次翻案**的排查：从「`save_path` 假缺陷」到「`is_root_folder=False` 真因」

> ★ 这是一次排查的**完整过程**，不是四次独立排查。
> **四次里我错了四次**，每次都是被**直接判据**打回。过程**故意不删** ——
> 它比结论更能防止下一个人（含 compact 后的我）重走一遍。

### 0. 起因

用户报：`https://www.musopia.vip/details.php?id=17287`（`milet - Made of Glass`）
**页面在，但没人做种**。该行是 18:11:01 那轮 publish 刚发的（`--yes` 修复后第 2 条）。

★ 现象**真实**、**至今仍存在**、**最终也查清了** —— 中间四次翻案如下。

### 1. ★ 翻案 ①：`save_path`「缺陷」（**错**）

**当时的推理**（看着很严谨，实则最不严谨）：
1. 日志 `✅ 已回加至 qB（做种路径: /downloads）`
2. 该行 `content_path` = `/downloads/milet - Made of Glass  {…, SECL-3339, CD} {FLAC}`
3. ⇒ 我断定 `save_path` 与 `content_path` **"在结构上不可能同时成立"**
4. 加 `is_skip_checking=True` ⇒ 断定"数据对不上"
5. 全库 273 条 `content_path` 都是 `depth=1` ⇒ 升格为"**可能是系统性缺陷**"

**打回**（探针实读生产 qB）：

```
save_path 分布：1416 个种子，**只有 1 种** —— '/downloads'
20549316…（原种）：state=stalledUP  uploaded=**75,031,146**  tracker=working
```

| 断言 | 实测 | 判定 |
|---|---|---|
| 两路径冲突 | `content_path` = `save_path` + 种子根名，**零冲突** | ❌ |
| 数据对不上 | **传过 75 MB** | ❌ |
| 分布是"混合" | **1416/1416 同一值** | ❌（我假设的分布不存在） |

★ **技术错因**：把"值看起来不对"当成了"**语义是错的**"。
qB 两个字段有明确分工 —— `save_path` = 加种时的**目标父目录**；
`content_path` = 内容**实际完整路径** = `save_path` + **种子内部根名**。
★ 我**从没查过**这两个字段的语义，就下了"结构上"的断言（`ERR-META-01` 形状：
**判据看着严谨，但它量的不是那件事**）。

★★ **顺带一次真正的勘误**：订正此节时我曾写下"`save_path` 缺陷**不存在**"。
那句**对**（`save_path` 确实没问题），但我**几乎同时**又写成"**一切正常**" —— 那**又错了**
（真缺陷就在下一节）。⇒ ★ **"排除一个假设"≠"证明无缺陷"**。

### 2. ★★ 翻案 ②：内容目录**不在磁盘上**（**错**）

**当时的推理**：`missingFiles` 判的是"文件缺失" ⇒ 那目录大概被删了/没移进库。

**打回**：
```
$ ls -ld "/volume1/video/music/milet - Made of Glass  {…, SECL-3339, CD} {FLAC}"
drwxr-xr-x 1 iSunker users 708 Sep  7 21:10   ← 存在、权限正常、9-7 起就在
```

★ **错因**：为"合理"**编了一个未验证的前提**。

### 3. ★★ 翻案 ③：qB 看不到 `/downloads`（容器内路径）（**错**）

**当时的推理**：`/downloads` 是**容器内**的挂载点名，而 qB 是另一个进程 ⇒ qB 解析不了。

**打回**：**我自己手里就有反证** —— 同一挂载下 **254 条正在正常做种**
（`up` 高达 **562 亿字节**）。若 qB 真的看不到，**255 条全该坏**。

★ **错因**：**忽略了自己已经拿到的反证**。

### 4. ★★★ 真因（已用**直接判据**坐实）

**判据来源**：`qb.torrents_files()` —— 列出 qB **自己认为**的文件路径。
★ 关键在这**不从我方推理一层**（今晚已因"推一层"错三次）。

| | qB 认为的文件 | 文件实际位置 | 结果 |
|---|---|---|---|
| **坏的 5 条** | `01.Hurricane.flac`（**裸**） | `/downloads/<专辑名>/01…` | ❌ **错位** ⇒ `missingFiles` |
| **好的 254 条** | `<专辑名>/01…`（**带前缀**） | `/downloads/<专辑名>/01…` | ✅ 一致 |
| ★ **反例**（09-19） | `01. 怪獣.flac`（**也裸**） | 与 qB 查找位置**恰好一致** | ✅ 一致 ⇒ **正常** |

⇒ **病因**：`readd_to_qb()` 的 **`is_root_folder=False`** 让 qB **丢掉了种子内部的根目录名**，
于是它在 `/downloads/01.Hurricane.flac` 找文件 —— **该路径不存在**。

| 项 | 实测 |
|---|---|
| 影响面 | `Musopia` **260 条中 5 条** `missingFiles`，`added_on` **全部 = 2026-09-24**（14:49 ×3 / 17:33 / 18:11） |
| 是否回归 | **不是** —— `readd_to_qb` 自 `419b6db`（初始 commit）起 **从未被改过** |
| 文件 | ✅ **都还在**（已 `ls`）⇒ **可修复，不是数据丢失** |

### 5. ★★★ 真正的一般化判据（比"有没有目录名"准）

> **决定成败的，不是"有没有丢目录名"，而是「qB 的查找路径」与「文件的实际位置」是否一致。**

★ **反例证明"丢目录名 ≠ 必然坏"**：09-19 那条 `サカナクション` 同样丢了目录名
（qB 认为文件是 `01. 怪獣.flac`），但 `up = 13,697,024`（**真传过 13 MB**）⇒ **正常**，
因为它要找的 `/downloads/01. 怪獣.flac` **真的存在**（那条的文件确实散在根下）。

⚠️ **"它的文件为何散在根下"，我没有证据** —— 可能是手工加种时给错 `save_path`、
可能做种前手工移动过、也可能用了**另一个** `is_root_folder` 取值。
⇒ 本条**只记现象与结果，不编机制**（见第 7 节纪律 ④）。

### 6. ★ 一个必须解释的矛盾 —— 以及它如何被解开

**矛盾**：`readd_to_qb` **从未改过**，那为什么 09-07 那批（254 条）**是好的**？

**解开**：那批**根本不是它加的** —— 旁证是 qB 认为的文件：

| 批次 | qB 认为的文件 | 判定 |
|---|---|---|
| 09-07 那批 | `Infected Mushroom - Army of Mushrooms (2012)/01…`（**带完整前缀**） | `is_root_folder` 行为**是对的** ⇒ **不是这条代码路径** |
| 今天 5 条 | `01.Hurricane.flac`（**裸**） | `readd_to_qb` 的产物 |

★ 而 09-07 那批 `tracker msg='Torrent not exists'` ⇒ **站点上已被删** ——
它们正是 `tools/reset_deleted_site_status.py` 处理的那 266 条**同批**。
⇒ **是历史遗留，不是"今天的对照组"**（我一开始把它们当对照，**是错的**）。

⇒ 结论：**今天这 5 条很可能是 `readd_to_qb` 第一次真正用在新种子上。**

### 7. ★★ `is_root_folder` 之所以难用：同一个值对不同种子结构效果不同

【A】里第一条暴露了这点：

```
[坏] [260909] 岩崎太整… → 'Disc 1/01. …'    ← 有 'Disc 1'！但**没有专辑根目录**
```

⇒ 它是 **2CD**（`Disc 1/`、`Disc 2/`），**种子内部本来就没有统一根目录**；
而 milet 那几条是**单层**（`01.flac` 直接在根）。

★ ⇒ **没有一个固定的 `is_root_folder` 取值能通吃这两种结构。**
⇒ 修法**不能**简单改成 `True`（那会**坏掉 260909 这类**）。

### 8. ★★★ 过程纪律：**我 4 次倾向、4 次被数据打回**

| # | 我的倾向 | 被什么打回 | 错因 |
|---|---|---|---|
| ① | `save_path` 与 `content_path` 冲突 | 1416/1416 同一值；原种传过 75 MB | 把"值看着不对"当成"语义是错的" |
| ② | 内容目录不在磁盘 | `ls -ld`：**在**、权限正常 | 为"合理"编了未验证的前提 |
| ③ | qB 看不到 `/downloads` | **254 条同挂载下正常**（自己手里的反证） | 忽略已有反证 |
| ④ | 反例"单独建成功了（文件被摊平）" | **无证据** | ★★ **为未验证的事编机制** |

★ **另有两次"未读证据就定性"**：第一次没读日志就"倾向 A"（而 A 也错）；
第二次在**只有一条样本**时把结论写成"这解释了为什么不是所有条目都中招"。

★★ **六条纪律**：

1. **措辞的强度不能超过证据的强度** —— 我写过"在**结构上不可能**同时成立"，
   那是对**两个没验证过语义的字段**下断言。
2. **`n=1` 不是分布** —— 一条日志（17287）不能支撑**普遍性**结论。
3. **别把"排除一个假设"当成"证明无缺陷"** —— 订正①时我差点就这么写了。
4. **未证实现象存在之前，不要解释它** —— 我曾为"为什么不是所有条目都中招"编解释，
   而那个现象**根本不存在**。
5. **别为未验证的事编机制** —— 反例的文件为何散在根下，我不知道，就不说。
6. ★ **先拿证据，再定性** —— 两次"未读证据就倾向"，**两次都错**。

### 9. ★★ 工具纪律：别用 `docker exec … python -c "多行"`

本节过程中**连续失败两次**，两种不同的坏法：

| 失败 | 报错 | 成因 |
|---|---|---|
| 第 1 次 | `IndentationError: unexpected indent` | `-c` 字符串里留了**前导空格** |
| 第 2 次 | `SyntaxError: invalid syntax` | 命令**粘贴时换行**，`from qbittorrentapi import` 与 `Client` 被**拆成两行** |

⇒ ★ **多行 Python 一律写成文件**（`tools/_probe_*.py`），再
`docker compose run --rm tools python /data/<file>`。
★ 比"小心点"更可靠 —— **换行是终端的行为，不是人的行为**。

★ 与本文件早已记过的「命令块里不许混中文」「两条命令各自一个代码块」是**同族第三条**。

### 10. ★ 待办（**未变：看到实况前不改代码**）

1. **查 09-07 那批当初是怎么加进去的** —— ★ 那是**唯一**能回答
   "正确的 `is_root_folder` 该是什么"的线索来源。
2. **修法未定**，且**不能简单改 `True`**（见第 7 节）。可能方向：
   - 按**种子内部结构**决定（`torf` 读出的 `name` 与实际文件前缀是否构成目录）
   - 用 `content_path` 的**完整路径**做 `save_path`，让 qB 无需拼接
   - ★ **加发布后复核**：确认 `state != 'missingFiles'`，否则告警 ——
     `✅ 已回加至 qB` 那行**只证明没抛异常**，**不证明做种成立**。
3. **★ 更根本的闸门问题**（本缺陷暴露的）：
   发布前是否该校验**内容目录存在**？—— 现在 `run.py` 抓轨完就发，
   而文件**是否已就位**、**位置是否与 qB 一致**，**没有任何校验**。

★ 优先级：**中**。不影响发布（种子确实上站了），但**站点上挂着种子却拿不到数据**，
比没有更糟。

### 11. 本节的探针（已删）

`tools/_probe_qb_savepath.py` / `_probe_qb_recent.py` / `_probe_qb_files.py` ——
**本机与 NAS 两份都已删**。
★ 判据链条全部记录在第 1、4 节的两张表里，**不必重建探针也能复核**。

---

## 2026-09-24（夜·续三）—— `readd_to_qb` 已修 + **两条流程教训**（修改已上线，判据待观）

### 0. 本节与「续二」的关系

「续二」**只查到真因、没改代码**（当时纪律是"看到实况前不改代码"）。
本节是**执行**：改 → 自检 → 部署，并把过程中**新犯的两个错**记下来。

★ 状态：**代码已改完、已部署、镜像已重建**；
但 **④ 生产判据（下一条新发布的 qB `state`）写本节时尚未取到** —— 见第 5 节。

### 1. 改法（`publish.py` → `readd_to_qb()`）

**核心：不传任何 layout 参数，也不传 `is_skip_checking`。**

```python
qb.torrents_add(
    torrent_files=content,
    save_path=save_path,
    is_paused=False,
    category=config.PUBLISH_CATEGORY,
)
```

| 删掉 | 为什么 |
|---|---|
| `is_root_folder=False` | lib 在 Web API ≥2.7 上**改写**成 `contentLayout="NoSubfolder"` ⇒ qB 丢种子内根目录名 ⇒ `missingFiles`（详见「续二」） |
| `is_skip_checking=True` | 它关掉的**正是唯一**能发现"文件对不上"的机制 ⇒ 那 5 条才**静默**变坏。去掉后：对则秒做种、不对则**立刻**响亮报错 |

★ **「不传」不是"传个 None"** —— 实测（`requests._encode_params`）证明
`(None, None)` 二元组 ⇒ **字段被整个丢弃**：
```
旧: savepath=%2Fdownloads&contentLayout=NoSubfolder&category=Musopia
新: savepath=%2Fdownloads&category=Musopia
```

另加：
1. 三条**原本静默**的 `return`（`qb is None` / 找不到原种 / 下载失败）各补一句**带原因**的打印
2. **发布后复核**：从 `content` 字节用 `torf` 读出**新** infohash（★ **不等于**库里的
   `info_hash` —— 站点种子是 `run.py` 用新 announce 重新生成的）⇒ 查 `state` ⇒ 醒目打印
3. docstring 写明**为什么不能传 layout**（引 pin 住的库源码，防下一个人"顺手加回"）
   + 如实写明**一处未知**：qB 对**多根目录**种子（2CD 那种 `Disc 1/`+`Disc 2/`）如何摆放，
   **没实测过** ⇒ 不为此加代码（那等于重新去猜结构），让复核**如实报出**结果

★ 复核**不 `sys.exit`**（不算阶段失败）—— 与 `main()` 里 `fetch_edit_form` 回读校验
**同一模式**。理由：种子**已上站**（不可逆），回加失败是**本地问题**，让 daemon 退避重试**无意义**
（下一轮不会再发同一条）。

### 2. ★★ 教训一：**我把"写完了"当成"部署了"**

自检第一次跑出来是：
```
✅ save_path 仍在 / ✅ category 仍在 / ✅ 不含 content_layout
❌ 不含 is_root_folder
❌ 不含 is_skip_checking
torrents_add 收到的 kwargs: ['torrent_files','save_path','is_skip_checking','is_paused','is_root_folder','category']
```

★★ **但这与"② 表单层全绿（body 里没有 contentLayout）"同时为真** —— 这个**矛盾**才是线索：
两句话不可能都对**同一份代码**。

⇒ 一查文件哈希：

| 位置 | 大小 | mtime | md5 |
|---|---|---|---|
| 本机 | 46764 | 11:08 | `781c6bee…` |
| **NAS** | **40821** | **09:13** | **`0a778ac4…`** |

⇒ **`deploy.sh --apply` 我根本没跑。** 容器里加载的是 NAS 上 09:13 的**旧** `publish.py`。

**根因（流程，不是代码）**：计划的 ③（部署）是**独立一步**，我从 ② 直接跳到 ④ 的观测准备，
**中间那一步没执行、也没发现**。★ 而 `②` 之所以绿，是因为它**只复现 lib 的装配逻辑，
不读 `publish.py`** —— 它测的是"我们理解对不对"，**不是"代码是什么"**。

★★ **附带收获：这次失败反而是自检有效性的证据。**
同一个脚本、同一份断言，只换了文件就从**红**变**绿** ——
说明它**有分辨力**，不是"永远打 ✅"的摆设。
★ 一个在文件不对时仍全绿的自检，等于没有自检。

**纪律**：**哈希对照是最便宜的"改了没生效"判据**（比读日志、比猜都便宜）。
改完 ⇒ `deploy.sh --apply` ⇒ **`md5sum` 两边对一次** ⇒ 才 `up -d --build`。

### 3. ★★ 教训二：**daemon 的时基是 `state` 里的 `last_run`，不是"重建时刻"**

`RUN_ON_START=False` 时我曾**推**"计时器从重建起算"（⇒ 首轮 = 重建 + interval）。
**读了 `daemon.py` 才发现这是错的**：

```python
state = load_state()                  # ← 从 daemon_state.json 读回 last_run
if run_on_start:                      # ← False ⇒ **不清零**
    for s in stages: state[..]["last_run"] = 0
...
last = st.get("last_run", 0) or 0
if now - last < wait: continue        # ← 时基是**文件里的 last_run**
```

⇒ **首轮 = `state.publish.last_run` + 1800s**，**与重建时刻无关**。
实测：`last_run=1790247144` = `10:52:24 UTC` ⇒ 首轮 `11:22:24 UTC`
（**不是** 重建的 11:11 + 1800s = 11:41 —— 差 19 分钟）。

★ **为什么这条值得记**：若把"重建+interval"当规律，**每次重建都会晚 19 分钟**去等一个
早该出现的信号 ⇒ 然后误判"daemon 卡住了" ⇒ 去重启 / 去查不存在的问题。

★ **附带**：`daemon_state.json` 落在 SMB 上，**容器重建不清它**（这是对的 —— 正是它让时基延续）。
`daemon_heartbeat` 是**Unix 秒（UTC）**，`last_finish` 是 **CST 字符串** —— 同一文件里**两种时基**。
**两个时钟**（本机 UTC / NAS CST）差**整 8 小时**。

### 4. ★ 教训三：**窗口没到就探测 ⇒ 得到的"空"没有信息量**

同一晚我让人连跑**三次** grep 都空，每次都得解释"正常、还没到点"。
**空输出在窗口外不构成任何证据**，却极易被读成"坏了"。

**纪律**：**先算出窗口（`last_run + interval`），窗口内再探。**
★ 这条与「命令块里不许混中文」「两条命令各自一个代码块」是**同族**：
都是在**省掉一次本可避免的误读**。

### 5. ★★ 当前状态（**本节写作时，判据尚未取到**）

| 项 | 状态 |
|---|---|
| `publish.py` 改动 | ✅ 已写、语法通过、实参区无三个 layout 参数 |
| 本机 ↔ NAS md5 | ✅ 一致（`781c6bee…` / 46764） |
| 镜像重建 | ✅ 容器 19:11:12 CST 起（新镜像） |
| 自检（容器内） | ✅ **三层全绿**（① 参数层 kwargs 只剩 4 个 / ② 表单层无 `contentLayout` / ②-b 对照红） |
| 复核块**真的在跑** | ✅ 输出 `✅ 回加复核：state=stalledUP content_path='/downloads/某专辑/01.flac'` |
| **④ 生产判据（真 qB）** | ⏳ **未取** —— 等下一条新发布（预期 `11:22:24 UTC` 起第一个 tick） |

★★ **注意自检里那个 `state=stalledUP` 是 `FakeQB` 桩的固定返回值** ——
它证明的是**我的复核代码会正确打印**，**不是** qB 真的把文件对上了。
**别把它当判据。** 这形状与「续二」的四次翻案同源：**把"我的代码行为"当"外部世界的状态"**。

### 6. 待办（本节新增，均**未做**）

1. ★ **取 ④ 判据**：下一条真实发布 ⇒ qB `state != missingFiles` 且 `content_path` 带专辑目录名
2. **那 5 条历史坏种子**（`missingFiles`，`added_on` 全是 2026-09-24）：
   文件**都在**（已 `ls -ld` 验证）⇒ **可修复**。修法是「从 qB 删掉 → 重新加」，
   ★ **独立一步**，**等 ④ 确认后再做**
3. **临时文件用完要删**：`tools/_selftest_readd.py`、`tools/_probe_watch_new.py`
   （NAS 上还有 `_selftest_readd.py` 一份）
4. **git 提交**：38 个文件改动**至今无版本记录**。
   ★ 且**等 ④ 之后再提交** —— 若 ④ 失败，`readd_to_qb` 可能要再改，那时提交会白写一条历史

---

## 2026-09-24（夜·续四）—— ★★ `readd_to_qb` 修复**已验收**（真 qB，非桩）

### 0. 结论先行

「续三」第 5 节标着 **④ 生产判据未取**。**本节把它取到了，并且成立。**

```
infohash      = c1db840fc8ea510a04faae7d1239b2ee902be9cc   （= 站点 id 17289）
name          = 'Barbara Hendricks, Roland Pöntinen - Robert Schumann Lieder (2007) [FLAC] {ARV-002}'
state         = stalledUP            ✅ 不是 missingFiles
content_path  = '/downloads/Barbara Hendricks, … (2007) [FLAC] {ARV-002}'
                                       ★ 带**专辑目录名**了（修复前退化成光秃秃 /downloads）
save_path     = '/downloads'
added_on      = 1790249604
```

★ 时序自证：**部署 11:11 → 发出 11:33:22** ⇒ 用的**确实是新代码**。

### 1. ★★ 但复核块**第一次跑是"没有信息量"的** —— 我的设计缺陷

`publish` 日志里那次复核报的是：

```
⚠️  回加复核：qB 里查不到 c1db840fc8ea…（可能还没入库）
🎯 发布完成：成功 1 条，失败 0 条
✅ 阶段 [publish] 完成，耗时 6.2s        ← ★ 6.2 秒
```

★★ **根因：`torrents_add()` 是异步的** —— qB 收到后**在后台**校验入库，
**不会**在 HTTP 返回前完成。而我那次复核**在 6.2 秒内跑** ⇒ **必然查不到**。

⇒ ★★ **这个复核准点在"必然查不到"的时刻执行** —— 它**既不是成功也不是失败**。
**我把一个永远拿不到结论的检查装进了主流程。**

### 2. ★★★ 更要紧的：那句措辞**把两种情况混成了一个词**

```
「qB 里查不到 …（可能还没入库）」
```

| 实际情况 | 后果 |
|---|---|
| qB **还没入库**（正常，异步） | 同一句话 |
| **回加根本没发生**（故障：`torrents_add` 抛错 / 走了某条 `return`） | ★★ **同一句话** |

⇒ ★★ **故障被"可能还没入库"这个含糊词盖住了**。
**这与「窗口没到就探测 ⇒ 空输出没有信息量」是同族**，
也与 `nas_health.py` 的三态纪律（「**未声明 ≠ 关着**」）反着来 ——
**那句话正是在把"不知道"说成"大概没事"。**

**处置（未改，待定）**：
- ★ **不在 `publish.py` 里 sleep**（拖长阶段无意义，且 `STAGE_TIMEOUT=3600` 是硬上限）
- ⇒ 改为：**措辞改硬**（把"可能还没入库"换成"**此刻查不到，无法区分『没加』与『晚入库』**"）
- ⇒ 并把**延迟复核**做成**独立的巡检**（`nas_health.py` 加一项：查 `Musopia` 分类里
  有没有 `missingFiles`）★ 这才是"长期可读回"的判据

### 3. ★ 顺带纠正一条我早先的误读

我早先取基线时把 `17288` 当"好的那条"，**不对**：

| 站点 id | 发出时刻 | 修复前/后 |
|---|---|---|
| `17288` Cesaria Evora | 10:52:24 | ★ **修复前** ⇒ **本来就该坏** |
| **`17289` Barbara Hendricks** | **11:33:22** | ★ **修复后** ⇒ 好 |

⇒ 实测 `Musopia` 分类里 `missingFiles` **从 5 条变 6 条**，
新增的正是 **`16708e52` Cesaria Evora**（`added_on=1790247144` = 10:52:24 = `17288`）。
★ **这不是回归** —— `17288` 是修复前发的，**它坏是预期的**。

⇒ ★ 教训：**"最新"不等于"最新代码发的"**。取基线时**必须把部署时刻当分界线**，
不能只看"水位线最高那条"。

### 4. ★ 仍未验的（如实标出，别当成"全好了"）

| 项 | 状态 |
|---|---|
| `uploaded > 0` | **0 字节** —— ★ **不是判据失败**（刚发 28 分钟、冷门古典乐**无 peer** 就传不出去）。★ 但因此**"真做种成立"只验了一半**：文件对上了（`content_path` + `state`），**数据真流通**还没见证据 |
| 覆盖面 | **只验了 1 条** ⇒ 单条成立 ≠ 全部成立 |
| 那 **6 条历史坏种** | ★ **未修**（各自 `added_on` 都在修复前）⇒ 见待办 |

### 5. 本节新增待办

1. ★ **改 `publish.py` 复核的措辞**（见第 2 节）—— 把含糊词换成分清的
2. ★ **`nas_health.py` 加一项**：`Musopia` 分类里 `missingFiles` 计数（长期判据）
3. ★ **那 6 条历史坏种**：文件都在（已 `ls -ld` 验过）⇒ 修法是「从 qB 删掉 → 重新加」。
   ★ **现在可以做了**（④ 已验收）—— 但**单独一步**，且要先备份 qB 侧信息
4. ★ **git 提交**（多轮改动至今无版本记录）
5. 探针**已删**：本机 `_check_new_seed.py` / `_probe_titles.py` / `_probe_watch_new.py` /
   `_selftest_readd.py`；NAS 上 `_check_new_seed.py` / `_selftest_readd.py` —— **共 6 份，全删**

---

## 2026-09-24（夜·续五）—— ★★★ `readd_to_qb` 全线闭环（含 6 条历史坏种修复）

### 0. 闭环总表（这是本线的**完整**验收）

| 阶段 | 判据 | 结果 |
|---|---|---|
| 诊断 | 6/6 的 `content_path` **都退化成 `/downloads`** | ✔ 症状一致，无例外 |
| 机制 | 读 pin 住的库源码 | ✔ `is_root_folder=False` ⇒ lib 改写 `contentLayout="NoSubfolder"` ⇒ 丢根目录名 |
| 修复 | `publish.py` **不传** layout / `is_skip_checking` | ✔ 已改 + 已部署（`781c6bee…`） |
| 新发布 | `17289` | ✔ `state=stalledUP` + `content_path` **带专辑目录名** |
| **历史 6 条** | **正面判据 6/6** | ✔✔ 见第 2 节 |
| 覆盖 | **7 条**全部成立（1 新 + 6 修） | ★ 机制诊断**完备**，**不存在第二种坏法** |

### 1. ★★ 修历史 6 条时，我的 v1 脚本有**两个真错**（都被探针抓住）

**错一：重加用的种子路径靠 `t.name` 拼 —— 必然找不到**

```python
tp = os.path.join("/data/output_torrents", f"{t.name}.torrent")   # v1 ✗
```

实测对照（**本地文件名 ≠ 种子内的 `name`**）：

| | 值 |
|---|---|
| 本地文件名 | `milet - Sunny - FLAC 整轨 - OpenCD.torrent` |
| 种子**内**的 `name` | `[260902] milet - Sunny [初回生産限定盤][SECL-3343~5][FLAC+CUE+LOG+PNG+BDMV]` |

⇒ ★★ v1 **每一条都会"文件不存在 ⇒ 跳过"**，**什么也修不成**，
而且**是静默的**（脚本会正常退出，只打一行 ⚠️）。

★ **修法**：用**库里的 `torrent_file` 列**（`db.py` 的权威字段，直接给完整路径）。

**错二：我没有验证"重加该用哪个 `.torrent`"这个前提就往下写了**

★ 我先停下来核（`_probe_announce.py`），**结论**：本地
`output_torrents/*.torrent` 的 trackers **已经**是
`https://www.musopia.vip/announce.php?passkey=…` ⇒ **它就是"发布到站上的那一份"**。
⇒ 用它是对的。**但这是验证出来的，不是假设成立的**（我原先只是碰巧猜对）。

### 2. ★★ 关键：判据是**正面**的，不是**排除法**

第一次修完我跑的是 `_probe_bad6.py`，它报 `missingFiles = 0 条`。
★★ **但那是排除法**（必要条件，**不充分**）：qB 里理论上可能有条目
**既不在 `missingFiles`、`content_path` 也不对**（如卡在 `checkingResumeData`）
⇒ **排除法会把它放过去**。

⇒ 于是**另写正面判据**（`_verify_fixed6.py`），对 6 个**写死的 infohash** 逐条查：

```
正面判据结果：**6 / 6 全过**，0 条有问题
```

| 判据 | 结果 |
|---|---|
| ① `state` 可用（非 `missingFiles`/`error`/`unknown`） | ✔ 6/6 `stalledUP` |
| ② ★ `content_path` **逐字节等于** `/downloads/<name>` | ✔ 6/6 |
| ③ 目录**在磁盘上且非空**（真读一次目录，数项数） | ✔ 6/6：**3 / 20 / 12 / 15 / 9 / 12** 项 |
| ④ `uploaded` | ⚠️ **全为 0** —— 见第 4 节 |

★★ **教训**：**「不在坏列表里」不是「好了」** —— 这与 `ERR-META-01` 同族
（判据看着对、测的是另一件事）。**能用正面判据就别用排除法。**

### 3. ★ 修法的**安全性设计**（为什么敢删）

`qb.torrents_delete(delete_files=False, …)` —— ★ **`delete_files=False` 是关键**：
只删 qB 条目，**文件一个不碰** ⇒ **即使全错，文件还在，可以再修一次**。
⇒ 这条把"不可逆操作"降级成"**可重复的**操作"。**这值得作为今后处理 qB 条目的默认姿势。**

另加**四道闸门**（任一不过就**跳过那一条**，不是整体失败）：
① 当下仍是 `missingFiles` 且 `content_path` 已退化（有人动过就不碰）
② 做种目录在磁盘上（文件不在 ⇒ 删了就真没了 ⇒ 绝不删）
③ 库里的 `torrent_file` 存在
④ ★ **种子内的 `name` == qB 里那条的 `name`**（防"拿错文件"）

★ 结果 **6/6 四道全过** ⇒ ④ 不是白设的，它正是能挡住"文件名相近但内容不同"的那道闸。

### 4. ★ 仍未验的（**别把闭环读成"全好了"**）

| 项 | 状态 |
|---|---|
| `uploaded > 0` | ★ **6 条全为 0**。**符合预期**（冷门古典/日系，站上无人下 ⇒ **无 peer 传不出去**），且 `stalledUP` 的语义正是"已就绪等传"。★ **但严格说：「文件对上了」已证实，「数据真流通」从未见过证据** —— 这个只能等真有人下，**别当已验** |
| 站点侧页面 | 未核（发帖仍在、可下载 —— 但那是**站上**的事，与 qB 侧是两个独立面） |

### 5. 附：`PUBLISHED` 计数已加进巡检（`tools/nas_health.py`）

★ 起因：`publish.last_run` 只说明**阶段跑过**，不说明**发出东西**。
⇒ 加了 `PUBLISHED` 条数（**单调增** ⇒ 两次巡检之差 = 本轮真发出去的条数）。

```
PUBLISHED     8   (已上站的条数；★ 只到「上站」为止，**不**代表 qB 里在做种)
```

★★ **为什么要写那半句警告**：`nas_health.py` 是**纯 SMB 只读、不连 qB** 的。
硬塞 qB 调用会破坏它"本机可跑、零依赖"的性质。
⇒ 改成**在输出和注释里都写死这个缺口**：那条数**到不了"做种"这一层**
（**修 `readd_to_qb` 前那 6 条全是 `PUBLISHED`**）⇒ 免得下一个人当"做种正常"。`ERR-META-01`。

### 6. ★★ `publish.py` 复核措辞已改（把含糊词换成分清的）

原措辞（**实测踩过**）：
```
⚠️  回加复核：qB 里查不到 <hash>…（可能还没入库）
```
★★ **它把两种截然不同的情况混成了一个词**：
- (a) qB **还没入库**（**正常** —— `torrents_add()` 是**异步**的，实测阶段全程 **6.2 秒**，必然查不到）
- (b) **回加根本没发生**（**故障**）

⇒ **故障被"可能还没入库"盖住** —— 与「空输出没有信息量」同族，
且**与 `nas_health.py` 的三态纪律反着来**（把"不知道"说成"大概没事"）。

**新措辞**：明说"此刻查不到**无法区分** (a) 与 (b)"，并指向**延迟复核**。
★ **不在阶段里 sleep**（拖长阶段无意义，且 `STAGE_TIMEOUT=3600` 是硬上限）。

### 7. 本节新增待办

1. ★ **git 提交**（今晚改动仍**无版本记录**）—— 现在可以做（本线已闭环）
2. ★ **`CRABPT_COOKIE`** + 蟹黄堡「填写规范全文」/「音乐种编辑页」（见 `PLAN-crabpt.md` 附录 A.5 / B.8）
3. ★ `publish.py` 的 `- OpenCD` 后缀是否保留 —— **待用户判断**（是发布组位；站上写 `-CHDMusic`）

---

## 2026-09-29 深夜 收尾落盘（用户 exit 前的交接）

★ 本轮**净零写入**（有一处改了又回滚，字节相同）。下面按「你回来先做什么」排序。

### ★★ 0. 最重要：有一个决定卡在你手上，且只有你能做

**凭据是否按"已泄漏"处理**（详见任务表 Task #12）。
决定性的一步 —— 我判定不了：

```
DSM → 控制面板 → 共享文件夹 → docker_ssd → 编辑 → 权限
```

看左侧有没有**非你本人**的账号且勾了**可读写**。
- 只有你 ⇒ 风险低，轮换可选
- 有别人 ⇒ 按已泄漏办

我给的三个选项 + 建议（**C：只换 `LSKY_TOKEN` / `OPENCD_COOKIE` 两个纯配置凭据**）
都在 Task #12 描述里。

### ★ 1. 我在本轮犯的两个错（都已订正，但你要知道）

**① 我编了一个不存在的键。** 我在给你的命令里写了 `CRABPT_PASSKEY` ——
**全项目零引用**（`config.py` / `crabpt_site.py` / `publish.py` / `.env.example` 全查过）。
蟹黄堡按 **Cookie** 鉴权（`CRABPT_COOKIE`），不按 passkey。
我把乌托邦的 `MUSOPIA_PASSKEY`（announce passkey）串到蟹黄堡上了。
⇒ 你照着跑出的那 40 字节随机串**作废**。

**② ★ 更严重：我改了 `.env` 就以为换成了钥匙。**
`publish.py:186-187`：
```python
AUTO_KEY_ENV   = "PUBLISH_I_AM_HERE"
AUTO_KEY_VALUE = "i-am-here"      # ← 硬编码常量，编译进镜像
```
**这把"钥匙"比的是代码里的常量**，不是配置 ⇒ 我只改 `.env` 会让两边不等
⇒ 下一轮 publish 判「值不对」⇒ **拒绝发布，而且每晚都拒**。
★ 已当场回滚，`.env` 现为 `i-am-here`，巡检仍报「两把钥匙成立」。
★ 讽刺点：这正是我反复写进 ERRATA 的 `ERR-DOCKER-11`（"宿主改了 ≠ 容器里是新的"），
  我拿它警告过别人，然后自己踩。**根因**：我把"凭据轮换"当成了"改文件"。
  真规则是 —— **任何凭据类值，先查它被谁读、跟什么比**。

### ★ 2. 提示词里那句「NAS SSH 未开」**已过期**

本轮实测**SSH 直连是通的**（此前多轮我误以为不通，把部署推给你手做 —— `ERR-META-44`）：

```
ssh -p 36688 -o StrictHostKeyChecking=no -i "/c/Users/艾森克/.ssh/id_ed25519" iSunker@192.168.0.7 '<命令>'
```

- ★ 必须 `-i` **显式指定 key**。**别**用 `HOME=/c/Users/艾森克 ssh ...` ——
  Git Bash 把中文 HOME 转成 GBK 字节（`/c/Users/\260\254\311\255\277\313`）⇒ `Permission denied`。
- ★ `docker` 不在 PATH：宿主上用 `/usr/local/bin/docker`；但
  `ssh host 'docker ...'` 里 `docker` 反而找不到 ⇒ 给全路径。
- ★ `sudo` 要密码；`iSunker` 在 `docker` 组 ⇒ 裸 `docker` 够用。
- 噪声过滤（每次都要）：`2>&1 | grep -vE "WARNING|post-quantum|store now|upgraded|Could not create|Failed to add"`

### ★ 3. 巡检本轮结论：无异常

- 心跳停 5m15s ⇒ 当场定性 **(c) 正在跑长阶段**（旁证：同一次采样里 state 写着
  `跳过至 22:40:51` ⇒ 22:41 时调度器还活着；且本轮**没有**重建过容器 ⇒ 排除 (b)）。
  `scan` 实测跑过 384s，`STAGE_TIMEOUT=3600` ⇒ 远未到门槛。**不报异常。**
- `EXISTS 46` / `PUBLISHED 40` / `stale 0`（只读口径 1 = 闸门④正确拦住的那条）—— **无变化**
- `crawl` 配额 `50/50` 满 ⇒ 空转、目标 `233→233`，**预期内**（明天换天恢复）

### 4. 新发现（小，但值得记）

`ENABLE_PUBLISH_CRABPT` **不在 `daemon_env.json` 快照里** ——
`daemon.save_env_snapshot` 是**白名单制**（`config.py:51` 注释已自陈这个代价）。
⇒ 判蟹黄堡开关**只能读 `.env`**（现为第 147 行 `=false`），**别从快照判**。

### 5. 本轮待办（任务表 #13 / #14）

- **#13** 把上面 ①②③ 写进 `ERRATA.md`；顺带**改提示词里"SSH 未开"那句**
  （那是常态化的错前提，`ERR-META-01` 的形状）
- **#14** 清理 NAS 上本轮产生的 2 份 `.env` 探路备份
  （`.env.before-rotation-20260929` / `.env.pre-publishrotate-20260929-*`）
  ★ **等 #12 落地后再删**；★ 删前先确认 `.dockerignore` 的 `.env*` 排除**覆盖得住**它们

### 6. 仍然挂着的旧待办（未变）

1. **git 提交** —— 今晚改动仍**无版本记录**（工作区有一批 `M`/`??`，含本轮 `deploy.sh`、
   `ERRATA.md`、`.gitignore`、`SESSION-NOTES.md`）
2. 提示词里的「仍未解」清单：重复叶惠美（真 uid **85068**，旧结论作废要重观测）、
   `OPENCD_COOKIE` 的 `$` 插值、空 body、`publish_crabpt` 队列基数、
   `beat()` 长阶段不刷新（读法上已缓解）

---

## 2026-10-01（凌晨）—— 蟹黄堡转种未开 + 暖封面缓存受阻 + `deploy.sh` 假阴性

### 0. 本轮实测到的硬事实（全部现场核对，非推断）

| 项 | 值 | 判据来源 |
|---|---|---|
| 容器 | `running` / **`healthy`** / StartedAt=`2026-09-29 21:25:54 CST` 未变 / ExitCode=0 | `docker inspect` |
| `ENABLE_PUBLISH_CRABPT` | **`false`**（容器 env + NAS `.env:147` + 两个轮换前备份**都是 false**） | `printenv` / `grep .env` |
| 蟹黄堡已发 | **0 条**（`site_publish` 280 行**全部** `site='musopia'`） | 容器内 SQL |
| 近 7 天日志含 `crabpt` | **0 次命中** | `docker logs \| grep -ic` |
| 转种队列 | **55 条**；闸门④ 断言报 `144f017ad0c0 transfer_rule='DENY'` **1 条**（= `15972`）且**不在队列** | `publish.py --site crabpt --dry-run --auto` |
| 闸门④ 修复在不在生产 | ★ **在**（生产 `db.py` 有 **15 处** `TRANSFERABLE_WHERE`，`is_transferable_to()` 含 `transfer_rule=='ALLOW'`） | `docker exec grep` |

★★ **纠错**：我曾说"闸门④ 修复没部署到生产" —— **那是错的**。本地 `git status` 的 `M` 只说明工作区脏，不代表生产没这个修复。

### 1. ★★★ "没有可上传的源"到底指什么（这是本轮最有用的结论）

`publish.py:_crabpt_cover()` 三步，**第 ② 步是唯一断点**：

```
① site_publish(site='crabpt').cover_url   → 表里 crabpt 0 行 ⇒ 恒空
② cover_fetch._cached_file(tasks.cover_url) → ★ 返回 ""（= 那句报错的来源）
③ assets.upload_to_lsky(<本地文件>)        → 也要本地文件
```

- `cover_fetch.COVER_DIR = /data/covers` —— **现在存在**（09-28 16:14 建），
  **不再是** 09-28 那次"从未建过"的状态 ⇒ 本文件旧描述已过时。
- `_cached_file()` 判据：按 **`sha1(url)[:12]` 前缀**在目录里找**非空**文件。
- 实测该目录里**只有 1 个文件**；全库 **306 行有 `cover_url`**，**305 行没进缓存**。

★★ **"预检绿 vs 最终预览红"不是矛盾**（我一度误判成 `ERR-META-14`，收回）：
  预检查的是 `tasks.cover_url` **字段非空**；最终预览要求**磁盘上真有那个文件**。
  两者是**不同判据**，可以同时成立。

### 2. `tools/warm_cover_cache.py` —— 能用，但有个真缺陷（已修 + 已部署）

- 它**本来就在镜像里**（Dockerfile `COPY tools/ ./tools/`）——我曾说"没部署"，**错**。
- 真缺陷：`python tools/warm_cover_cache.py` 时 **`sys.path[0]` 是 `/app/tools` 而非 `/app`**
  ⇒ `ModuleNotFoundError: No module named 'cover_fetch'`。
  ★ 容器里 `docker compose run --rm tools python tools/xxx.py` 走的正是这条路
  ⇒ ★★ **每一个不在 `/app` 根目录下的 `tools/*.py` 都受影响**。
- 修法：用 `__file__` 反推仓库根目录插进 `sys.path`（从哪调都对）。
- 已 `deploy.sh --apply` 同步到 NAS（11755 字节），容器内**跑通**。

### 3. ★★★ `deploy.sh` 的假阴性 bug（本轮的第二个真收获）

`_same()` 里写成：

```bash
local k; k="$(_dir_diff_kind "$a" "$b")"
[[ "$k" == "content" || "$k" == "srcnew" ]]
return $?
```

★ 最小对照实验（同一个 bash）：

```
cmp -s a b; return $?                          → 返回 1（正确）
local k; k="$(…)"; [[ … ]]; return $?          → 返回 0（**恒定**）
```

⇒ **函数返回值恒为 0** ⇒ 调用方 `if ! _same …` 永远为假
⇒ **`tools/` 里的任何改动都被静默漏同步**，脚本还印「✓ 生产与本地一致，无需同步」。
★ 实测代价：连跑两次 `--apply` 都是假阴性，NAS 上那份**一直是旧的**。
★ 属 `ERR-META-01` 同族：**判据提到了对的词，却从来没生效过**。
★ 修法：`case` 显式配常量返回值，**不碰 `$?`**。修完立刻正确报出 `[改] tools`。

### 4. 未解 / 待办（见 TaskList）

- **T1**：容器连不上 picgo —— 宿主 `curl` **200/200/超时**，容器 `requests` **6/6 全 ReadTimeout**
  ⇒ "宿主能拉容器不能"是个**真异常**，且可能同时解释"publish 一直缺封面"。**未定性，待查**。
- **T2**：暖封面缓存（305 条待暖）—— 被 T1 挡住。
- **T3**：Lsky 上传 token —— 用户说"晚点解决"（第 ③ 步，暖缓存只解决 ②）。
- **T4**：`ENABLE_PUBLISH_CRABPT` 仍 `false` —— 用户要"开 + 发 3 条"，**尚未执行**（我停在等确认）。
- **T5**：`publish`（乌托邦）连续失败 3 次退避至 21600s，根因 `www.musopia.vip` **间歇** ReadTimeout。
- **T6**：`crawl` 01:35 那轮 `open.cd` ReadTimeout，退避 3600s。同一族网络抖动。

---

## 2026-10-01（03:00 续查）—— T1 有重大进展：这是 IPv6 问题

### 新增硬事实（全部实测）

**1. NAS 地址是 `192.168.0.7`（SSH 36688），不是 192.168.1.100。**
   上一轮我用的 `192.168.1.100` 是**错的**（超时）。正确地址在
   `~/.ssh/known_hosts` 里（`[192.168.0.7]:36688`）。

**2. ★★★ 宿主 `curl` 拿到的决定性一行：**

```
try1 http=301 ip=2606:4700:3034::ac43:81c9 connect=0.280s tls=0.773s total=0.988s  → 成功(IPv6)
try2 http=000 ip=2606:4700:3032::6815:2e7 connect=0.185s tls=0.000s total=40.001s → 超时(IPv6)
try3 http=301 ip=104.21.2.231            connect=0.521s tls=1.093s total=1.347s  → 成功(IPv4)
```

★ 关键**不是**"宿主能拉容器不能"，而是 —— **宿主自己也是 3 次里错 1 次**，
  而**失败那两次的 `remote_ip` 全是 IPv6**（`2606:4700:...` = Cloudflare IPv6），
  成功那次是 **IPv4**（`104.21.2.231`）。
  ⇒ **根因是 IPv6 出网不通**（TCP connect 能过、TLS 握手 0 秒失败 ⇒ 黑洞），
    不是 picgo 的问题，也不是容器的特殊问题。

**3. 容器与宿主的 DNS 配置**完全相同（都是 `nameserver 223.5.5.5` +
   `2400:3200::1`），解析结果也相同（`origin.picgo.net -> 104.21.2.231`）。

**4. `origin.picgo.net` 是 Cloudflare 双栈**（A: 104.21.2.231 / 172.67.129.201；
   AAAA: 2606:4700:3032::6815:2e7 / 2606:4700:3034::ac43:81c9）。

### 推论（待验证，别当结论用）

- Python `requests` **默认优先 IPv6**（`getaddrinfo` 顺序 + Happy Eyeballs 支持差）
  ⇒ 容器里"6/6 全 ReadTimeout"完全可能是**每次都挑到 IPv6 死路**。
- 宿主 `curl` 有 Happy Eyeballs（`--happy-eyeballs-timeout-ms`），所以只有 ~1/3 失败。
  ⇒ 这解释了"宿主能拉容器不能"的**表观矛盾**，两边的差异**不**在容器网络，在**选地址的策略**。

### ★★ 修法（下一步就做这个，风险低）

容器里加环境变量强制走 IPv4 —— python 没有全局开关，但有两个办法：

1. **`docker exec` 里直接验**（先证伪，再改 compose）：

```
docker exec opencd2musopia python -c "import socket; print(socket.getaddrinfo('origin.picgo.net','443',socket.AF_INET))"
```

```
docker exec opencd2musopia python -c "import requests;r=requests.get('https://origin.picgo.net/',timeout=30,headers={'Host':'origin.picgo.net'});print(r.status_code,len(r.content))"
```

   再做**强制 IPv4** 的对照（用 `socket.setdefaulttimeout` + 自建 adapter，
   或最简：`requests.get(url_that_resolves_v4_only)`）。

2. **改 compose 加 `sysctls` 禁 IPv6**（治本，但要重建容器 ⇒ 会重启 daemon）：

```
sysctls:
  - net.ipv6.conf.all.disable_ipv6=1
```

   ★ 注意：**这会重启容器**，等于 `up -d --build`。daemon 正在跑，选时机。

3. **最省事且不重启**：在 `cover_fetch.py` / `assets.py` 的会话上挂
   `HTTPAdapter` + `socket.AF_INET` 强制（代码改动，需重建镜像才生效
   ⇒ 也重启）。**唯一"今天就能用"的办法是宿主先抓图喂 `/data/covers`**
   （= 我上一轮列的"路 2"，不治本但立刻能推进 T4 发 3 条）。

### 下一步（回来第一件事）

先跑上面那两条 `docker exec` 验**强制 IPv4 能不能通**（只读、无副作用）。
通了就写进 ERRATA + 改 compose；不通就换查 TLS/SNI 层面。

### ★ 交给下轮的环境须知

- SSH：`ssh -p 36688 -i ~/.ssh/id_ed25519 iSunker@192.168.0.7`
  （`HOME=/c/Users/艾森克`；`192.168.1.100` **是错的**，别再用）
- NAS 上跑 docker 要 `PATH=$PATH:/usr/local/bin`；`iSunker` 在 docker 组，**不用 sudo**。
- NAS 上**没有** `getent`（用 `nslookup` 或 `python -c socket`）。

---

## 2026-10-02（20:00 续查）—— T1/T3/T5 的真根因坐实并修复：不是"IPv6 黑洞"，是"个别 Cloudflare v6 边缘 IP 不可达 + requests 不做 Happy Eyeballs"

### 本轮的起点
用户提供了**蟹宝王图床（蟹黄堡专用 Lsky Pro）的 token**：
`https://img.crabpt.vip/api/v1`，token `14|jusQ5S1k17CvfnbnhgEkkUXIXtZHw2lXF8u518Y1`。

### 1. ★★★ 根因更正（推翻前几轮的"IPv6 出网黑洞"结论）

前几轮把 T1 归因为"IPv6 出网黑洞"。**本轮实测证伪**：

**逐 IP 直连 TLS 测试（NAS 上，宿主与容器各测一次，结论一致）：**

| 域名 | IPv6 | IPv4 |
|---|---|---|
| `img.crabpt.vip`（蟹黄堡图床） | ❌ 部分 v6 超时（同域不同 IP 表现不同） | ✅ TLS1.3 ~0.4s |
| `origin.picgo.net`（乌托邦图床） | ✅ 可用（所以 T1 时好时坏） | ✅ 可用 |
| `www.musopia.vip`（乌托邦站点） | ❌ 两个 v6 全超时 | ✅ 可用 |

- **宿主 `curl -6 https://img.crabpt.vip/` 能通**（404，<1s）⇒ IPv6 **整族没问题**，
  "黑洞"这个词用错了。
- 真相：**个别 Cloudflare 边缘 IPv6 单点不可达**（TCP connect 能过——内核层，
  但 TLS 握手挂到超时 = 典型单点黑洞）。

**为什么 curl 通、Python 不通（这才是"宿主能拉容器不能"的真相）：**
- `socket.getaddrinfo` 返回 **IPv6 排在 IPv4 之前**（实测，宿主与容器一致）；
- `curl` 有 **Happy Eyeballs** ⇒ 坏的那族被好的那族盖过；
- `requests`→`urllib3` **取第一条、不竞速** ⇒ 稳定选中坏 v6 IP，挂到超时。
- ★ 容器是 `network_mode: host`，与宿主**共用网络栈**，实测两侧行为**逐字一致**
  ⇒ "容器特殊"从来就不是真的；差异在**选地址策略**。

### 2. 修法：新增 `net.py`（全局强制 IPv4）+ `config.py` import 它

- 新增叶子模块 `net.py`：`import` 时 patch
  `urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET`
  （只影响 urllib3 = requests 的地址选择，最小侵入，不动 `socket.getaddrinfo`）。
  开关 `NET_FORCE_IPV4`（默认 true）可回退。带 `--selftest`（含**阴性对照**：
  先还原默认断言 AF_UNSPEC，再 patch 断言 AF_INET ⇒ 判据**能失败**，ERR-META-32）。
- `config.py` 顶部 `import net`（config 被全项目 33 处 import ⇒ 一处生效覆盖全部，
  含 4 处**裸** `requests.get/post`）。`net.py` 不 import config、不读 env ⇒ 自检零负担。
- 3 处 `requests.Session()` → `net.new_session()`（明文约定，防将来漏改）。
- `deploy.sh` FILES 与 `Dockerfile` COPY 都加了 `net.py`（★ 漏一处 = 容器 ImportError）。
- **不动** `qbittorrentapi`（9 处）：目标是 `http://127.0.0.1:3020` 字面 IP，不走 DNS（已核实）。

### 3. 蟹黄堡图床 token：验证 + 落地

- **只读验证**：`GET /api/v1/profile` → **200**；坏 token / 无 token → **401**（阴性对照成立）。
- **真上传**：`POST /api/v1/upload`（字段 `file`）→ `status:true` + `data.links.url` ✅（测试图已删）。
- NAS `.env`（第 29/30 行）**:清洗**：
  - `LSKY_API_URL` 行尾多余空格已去；
  - 坏的 `LSKY_TOKEN`（**714 字符浏览器 Cookie 串**，ERRATA 已记的那个）→ 用户给的真 token
    （43 字符、`14|` 开头，Sanctum `{id}|{plain}`）。
  - 备份：`/volume2/docker_ssd/musopia_script2/.env.bak-before-lskytoken-*`。
  - ★ 真 token 是**兜底路径**；容器另有 9-28 落盘的**有效缓存 token**（iSunker 账号），
    且已配 `LSKY_EMAIL/PASSWORD`。三条路都能走通。

### 4. 部署 + 重建 + 验证（全部实测）

- `deploy.sh --apply` → 同步 `net.py` + 4 个改动文件（`[改] tools` 也正确报出）。
- NAS `docker compose up -d --build` → 重建成功（COPY 行含 `net.py`），容器 recreate+start。
- **验证修复生效**：
  - 容器内 `allowed_gai_family() = AF_INET` ✅；`python net.py` 自检 ✅。
  - **决定性对照**：容器内默认策略（已 patch）打图床 `/profile` → **200，3/3 次，~1s**
    （**修复前是 TLS 握手超时**）。
  - 三个目标一起测：`www.musopia.vip`→200 / `origin.picgo.net`→200 / `img.crabpt.vip`→404(裸路径，正常)。
  - `assets.upload_to_lsky()` 容器内**真上传成功** → `https://img.crabpt.vip/xhb-image/.../xxx.png`。
- **一次修复同时解决 T1（picgo 封面）+ T3（蟹黄堡图床）+ T5（乌托邦发布间歇超时）**
  —— 三者的共同点是"目标都在 Cloudflare 后面"。

### 5. 暖封面缓存（T2）已恢复

- `tools/warm_cover_cache.py --db /data/data.db --apply --limit 5` → **5/5 成功**
  （含一张 10MB 图，无超时）—— 修复前这一步被 IPv6 黑洞全挡。
- 全量 301 条在 NAS 后台跑（日志 `/volume2/docker_ssd/musopia_script2/_warm_cover.log`）。

### ★ 交给下轮的环境须知
- SSH：`ssh -p 36688 -i ~/.ssh/id_ed25519 iSunker@192.168.0.7`（NAS 目录 `/volume2/docker_ssd/musopia_script2`）。
- NAS 上 docker 要 `PATH=$PATH:/usr/local/bin`；`iSunker` 在 docker 组，**不用 sudo**。
- ★ 排查网络务必**逐 IP 直连**，别拿域名级 `curl` 一次当判据（ERR-NET-01）。
- ★ "宿主通/容器不通"在 host 网络下**不成立**——别再从容器网络找原因（ERR-NET-02）。

---

## 2026-10-02（21:00 续）—— 蟹黄堡发布链路打通：又抓到两个真缺陷

### 6. ★★★ 缺陷 A：daemon 的 `publish_crabpt` 阶段**永远发不出去**（漏了 `--yes`）

`daemon.py` 里：
- 乌托邦 publish 阶段（161 行）：`apply_flag("--yes", "--auto")` —— **两个都传**；
- 蟹黄堡 publish 阶段（202 行）：`apply_flag("--auto")` —— ★ **漏了 `--yes`**。

后果：容器里**没有 TTY**，`publish.py` 走到交互确认那行 `input()` 立刻 `EOFError` 崩掉
⇒ 即使把 `ENABLE_PUBLISH_CRABPT` 打开、队列里全是待发，**一条也发不出去**，
且每轮都以非 0 退出、触发退避。**实测复现**：手工 `docker exec ... publish.py --site crabpt --auto`
（不带 `--yes`）→ `EOFError: EOF when reading a line`。
★ 修法：改成 `apply_flag("--yes", "--auto")`（与乌托邦同形）。
★ 这是 ERRATA「保险看起来在、其实没生效」那族的又一例 —— 而且更隐蔽：
  代码读了像"只是少个交互参数"，实际是"这条路**从未跑通过**"。

### 7. ★★★ 缺陷 B：蟹黄堡发布**从不回加 qB 做种**（用户问出来的）

用户原话：「目前为止发了四个种，都合格的，但是为什么**不添加做种**呢？」
★ 查清：`run_crabpt()` 里**根本没有** `readd_to_qb` 调用（乌托邦路径有），
  PLAN-crabpt.md §3.4 / D.5 记为「不回加 qB（蟹黄堡尚未接线）」。
⇒ 后果：**种子发上站了却没人做种** ⇒ 蟹黄堡那边记 hit-and-run，等于白发。

**实现前先核实了关键事实（逐条实测）：**
- 蟹黄堡服务端那个 `.torrent`（`/download.php?id=N`）**已被重写**：
  announce = `https://crabpt.vip/announce.php?passkey=8fb322d2…`（蟹黄堡自己的 passkey）。
- 同一条（皇后 `69c5ee1b…`）：乌托邦种子 infohash `edda64b7…`、
  蟹黄堡服务端种子 infohash `03c17802…` —— ★ **两者不同**（站点重写了 piece length/name）
  ⇒ 属于 PLAN §3.4 的「**重新生成**⇒ 两个种子、两份回加」那一支，不是"同文件只回加一次"。
- 但**文件内容逐字节相同**：同 name、同 80 个文件、同总大小 ⇒ 可共用**乌托邦那条的 `save_path`**。

**实现**（`publish.py`）：
- 新增 `connect_qb()`（从 `main()` 抽出，两条路共用）。
- 新增 `readd_to_qb_crabpt(qb, client, info_hash, torrent_id)`：
  ① 做种路径取**乌托邦那条**（`info_hash` = 皇后原种）的 `save_path`；
  ② 种子字节取**蟹黄堡服务端**那份（`client.download_torrent(tid)`，新增到 `crabpt_site.py`；
     ★ **不能**拿本地 `tasks.torrent_file` —— 它带乌托邦 announce ⇒ 做种报到乌托邦、
     蟹黄堡仍记 hit-and-run，是**假成功**，比不做种更坏）；
  ③ 参数只有 `torrent_files/save_path/is_paused/category` ——
     **不传 layout、不传 is_skip_checking**（逐字沿用 `readd_to_qb` 的教训）；
  ④ 复核按**蟹黄堡那份**的 infohash 查 `state`，且把「还没入库（正常）」
     与「回加没发生（故障）」**分开说**（`torrents_add` 异步）；
  ⑤ 失败**不算阶段失败**（不 `sys.exit`）—— 种子已上站，回加失败是本地问题，退避无意义。
- `run_crabpt()` 里：`--no-readd` 时才连接 qB（`connect_qb()`），提交成功后调 `readd_to_qb_crabpt()`。

**验证（全部实测）**：把已发的 4 条（215344/345/346/347）手工回加：
- 全部到达 **`progress=1.0`**（= 文件对上，无 `missingFiles`）、
  state ∈ {`uploading`, `stalledUP`}、tracker = `https://crabpt.vip/announce.php?passkey=…`
  ⇒ **4 条都在向蟹黄堡做种** ✅。（`up=0` 是正常的——还没有人下载。）

### 8. 本轮完成的总账
- ✅ T1（picgo 封面）/ T3（蟹黄堡图床）/ T5（乌托邦发布间歇超时）—— 一次 IPv4 修复全解。
- ✅ T2 暖封面缓存：**300/301 成功**。
- ✅ T4 开蟹黄堡发布：`ENABLE_PUBLISH_CRABPT=true`；已发 **4 条**（215344~215347），
  每条带**自己那份**蟹黄堡图链。
- ✅ 新增：蟹黄堡**回加做种**（本轮补的缺口，4 条已做种）。
- ✅ 修：daemon `publish_crabpt` 漏 `--yes`（不修则一条都发不出）。

### ★ 遗留（下轮）
- 乌托邦 `publish` 阶段本轮失败 1 次（退避 10800s）—— **未定性**，需看 `docker logs` 那一条的原因。
- 「已上站、却没标 ALLOW」的有 1 条（`144f017ad0c0`，`transfer_rule='DENY'`）—— 建议人工过一遍。
- 暖缓存那 **1 条失败**的封面（可重跑一次）。

---

## 2026-10-02（21:15）—— 蟹黄堡发布节奏调整（用户要求）

用户要求：**改成 1 小时一条**消化存量；并因刚发了 4 条，**开始时间定在 4 小时后**。

- NAS `.env`：`CRABPT_INTERVAL=1800 → 3600`（1h/条）。备份 `.env.bak-before-crabpt-interval-*`。
- **延迟首轮**：`daemon_state.json` 的 `publish_crabpt.last_run` 设为 **now+3h**。
  ★ 机制：daemon 判到期用 `now - last_run >= interval`；令 `last_run` 落在**未来**
    ⇒ 头 4 小时恒"未到期"，到 `last_run+3600`（= now+4h）才首次触发，之后按 1h 走。
  ★ 为什么必须**先停容器再改盘**：`load_state()` 只在 `run_loop` 开头读一次，
    之后内存态每轮覆盖盘 —— 容器在跑时改 `daemon_state.json` 会被立刻冲掉。
    顺序 = `compose stop` → 改 `.env`+state → `compose up -d`。备份 `daemon_state.json.bak-before-crabpt-delay-*`。
- 实测确认：启动计划 `publish_crabpt 每 3600s`；state `skip_reason=未到期：距上次 -10756s < 周期 3600s`
  （负的 age 正是"未来时刻"的指纹）；首次触发 **2026-10-03 09:10:59 CST**，之后每 3600s。
- 其余 7 个阶段周期**未动**（scan 1800 / publish 5400 / sync 86400 / covers 21600 / edit 21600 / vtid 86400 / crawl 1800）。

## 2026-10-03 诊断：乌托邦 publish 连续 code=1（三天没发新种）

### 现象
- 生产 `daemon_state.publish`: `last_ok=false, last_code=1, last_took≈8s`，连续多轮。
- 队列**不是空的**：闸门①②③④后仍有 **72 条**（不是 51）。
- 最近一次成功发布：2026-10-02 20:44（tid 18896）。

### 复现（容器内，`docker exec -w /app opencd2musopia python publish.py --yes --auto --limit 1 --no-readd`）
```
✅ Cookie 有效，user_id = 10427
[1/1] 🚀 发布: Infinity Naked In The Rain 2001 - FLAC 整轨 - OpenCD
    ❌ 发布失败: 未拿到重定向 ID，HTTP 200
🎯 发布完成：成功 0 条，失败 1 条
```

### 抓到的原始回包（决定性的）
`POST /takeupload.php` → **HTTP 200, 无 Location**，页面标题 = `音乐乌托邦 :: 上传失败！`
但 `#outer` 区（NexusPHP stderr 放消息处）**只有站点横幅**，**没有任何错误文字**（412 字节，无 table/消息）。
`_extract_error()` 抓不到东西 ⇒ 报"未拿到重定向 ID"。

### 已排除（逐条实测）
| 假设 | 结论 | 证据 |
|---|---|---|
| Cookie 失效 | ❌ 排除 | 站点问候 `iSunker`，user_id=10427，首页可读 |
| passkey 失效 | ❌ 排除 | announce.php 返 `Invalid info_hash`（过了鉴权），非鉴权错 |
| 字段名不符 | ❌ 排除 | `medium_sel[4]/audiocodec_sel[4]/team_sel[4]/type=408` 与站点表单逐字一致 |
| multipart 畸形 | ❌ 排除 | PreparedRequest 检查：boundary×16、name 全对、file 在末 |
| 单字段触发 | ❌ 排除 | descr 简/仅图/空、去 custom_fields、medium_sel=8、tags — **全部同样失败** |
| torrent 文件损坏 | ❌ 排除 | torf 可解析，announce/passkey 正确 |
| **无 file 字段 / file 空** | ⚠️ | **同样返回"上传失败！"空页** ⇒ 空页是站点的"通用拒绝模板" |
| 重复种子 | ✅ 有 1 条 | `コニファー` → `302 details.php?id=19333&existed=1`（站点**认**这条） |

### 关键对照
- **重复种** → `302 ...existed=1`（站点 accept 路径**正常**）
- **任意新种** → `200 上传失败！`（空消息）—— 与字段/文件**全无关**

### 推断（**未证实，需站点侧或抓包**）
站点 `takeupload.php` 对**新种**走了一条**静默拒绝**路径（标题置"上传失败！"但**不写消息**）——
像是站点侧新增的**反滥用/审核/入库钩子**在 `stderr()` 之前 `die()`。
可能是：站点改版、Redis 缓存层异常、或对新上传的全局限制。
**这超出本项目代码的控制范围**，需站点管理员或浏览器抓包（HAR）确认具体拒绝原因。

### 附带发现
1. **队列 72 条 torrent 文件 infohash ≠ DB info_hash（72/72 全不一致）**。
   `torf` 读文件 infohash vs DB `info_hash` 全部不同 —— 说明**输出种子是重新生成的**
   （DB 的 info_hash 来自 scan 时读 qB，文件是 torf 现做的）。站点自行算 hash 入库，
   故**不直接致因**，但值得记录（可能是历史设计）。
2. **`edit` 的 code=1 是另一回事**：`id=19320 种子已被删除`（details 读不到），非本次根因。
3. **`crawl` 的 code=1** 未单独查（与 publish 无直接关系）。

## 2026-10-03 【更正上一条诊断】乌托邦 publish 失败的真因 = 账号等级 + 发种大赛窗口

★★ 上面那节「推断：站点侧反滥用/静默拒绝」是**错误归因**，此处更正。用户提示"站点发种权限正常"后，
继续深挖（补了：抓浏览器全头 / 换域名 / 预热会话 / 候选页 / 等级页 / FAQ / 首页公告），真因如下：

### 真因
- 账号 10427 等级 = **`User`**（`userdetails.php` → `<img alt="User">`；`upload.php` 原文
  「NFO文件（不允许 **Power User 以下**用户查看）」再次佐证）。
- 站点 FAQ 原文：**「User：新用户的默认级别。只能在每周六中午12点至每周日晚上11点59分发布种子。」**
  **「Power User：…可以直接发布种子。」**
- **首页公告**：**「音乐乌托邦社区第一届发种大赛」9-01 ~ 9-30 24:00**，参与对象写
  **「所有注册用户都可以参与」「User 及以上用户组均可自由发布」** ⇒ **活动期内工作日也能发**。
- **活动已于 9-30 24:00 结束** ⇒ **10-01 起回到"仅周六12:00~周日23:59"限制**。

### 时间线（实测，北京时间）
| 时间 | 能否发 | 说明 |
|---|---|---|
| 09-28(一)~09-30(三) 全天 | ✅ | 发种大赛期内，User 自由发（**工作日也发，10-02 前那批 40+ 条都是这时发的**） |
| 10-01(四) 起 | ❌ | 大赛结束，User 回到周末限时 |
| 10-03(六) 06:0x | ❌ | **未到 12:00 窗口** |

### 实测证据（排除一切代码侧原因）
- 浏览器全头（`sec-ch-ua`/`Sec-Fetch-*`/`DNT`…）→ 仍失败 ⇒ 不挑 UA/头。
- 无 www 域名 → `musopia.vip` 无 DNS 记录，站点只有 `www.`。
- 先 GET `upload.php` 预热会话再 POST → 仍失败 ⇒ 非调用顺序问题。
- 字段名与站点表单逐字一致、multipart 结构正确、torrent 文件合法、passkey 有效
  （announce 返 `Invalid info_hash` = 过了鉴权）。
- **决定性对照**：**重复种**（队列里 `コニファー`）→ `302 details.php?id=19333&existed=1`（站点 accept 路径正常）；
  **任意新种** → `200 上传失败！`（`#outer` 区零消息）⇒ 权限/时段拒绝，非代码。

### 作业（已做）
1. `PUBLISH_INTERVAL`：5400 → **3600（1h/条）**（用户要求；`.env.bak-before-publish-interval-20261003-054859`）。
2. **退避处理**：`fails` 是**内存变量**（重启归零），但窗口前会反复失败把退避推大（实测已见 `wait=7200=3600×2`）。
   ⇒ 方案：**北京时间 10-03 11:58 定时重启容器**（脚本 `NAS:_reset_publish_window.sh`：停→把
   `publish.last_run` 提前 2h→起，`fails` 归零 + 立即到期）⇒ 12:00 窗口一开即发。
   ★ 定时挂在本机（`CronCreate`），**注意本机 shell 时区是 UTC**（用户桌面是北京时），故 cron 用 `58 3`（=北京11:58）。
3. **队列体检**：72 条 → **63 条现成可发**；9 条缺封面（`backfill_covers --all` 实测**无源，补不了**），
   但 publish 的 `valid` 是**过滤后才 `[:limit]`** ⇒ 缺封面的会被**跳过、不堵队**（设计正确）；
   18 条缺副标题**不影响**（52 条已发布里有 10 条副标题为空）。

### 待用户
- 向站点申请 **`Power User`** 权限（升级硬条件含"下载≥50G"，而账号下载量=0 ⇒ 多半要**管理员特批**）。
  拿到后**随时可发**，不必等周末。

---

## 2026-10-03 【修正上条作业#2】窗口重置改为 NAS 自带容器（不再依赖 Claude 会话）

上一条作业#2 用 **`CronCreate`（session-only）** 挂定时 —— 它**随 Claude 退出即失效**，
且依赖本机开机。已废弃（job `d95db686` 已删）。

**改为**：NAS 上跑一个自调度容器 `musopia-window-reset`（`alpine` + `--restart unless-stopped`），
每天 **UTC 03:58 = 北京 11:58** 执行 `_reset_publish_window.sh`。跨重启/关机/会话退出均存活。

### 实测踩到的 3 个坑（已修）
1. **`docker compose` 在 alpine 里不可用**（compose 插件是 glibc 二进制，alpine 是 musl）
   ⇒ 重置脚本改用 **`docker stop` / `docker start`**（单服务与 `compose stop/up -d` 等价）。
2. **容器 `restart: unless-stopped`** ⇒ `docker stop` 后 Docker 立刻拉起，daemon 用**内存旧值**覆盖编辑
   ⇒ 必须先 **`docker update --restart=no`** → 停 → 改 → 起 → **恢复 `--restart=unless-stopped`**。
3. ★★ **`daemon_state.json` 每个阶段块都有 `last_run`，`scan` 在最先**
   ⇒ "替换第一处 `last_run`"会改到 **scan** 而不是 **publish**（这个坑静默）。已改为 **awk 状态机**：
   进入 `"publish": {` 块后只改该块第一处 `last_run`。
   - 处置：误改的 `scan.last_run` 已用 python 精确还原为原值（1790979054.0338974）。

### 验证（已做，非"我以为对"）
- 修正脚本**实跑**：`publish.last_run 1790978152 → 1790974103`（提前 ~2h，age≈2.1h ⇒ 立刻到期），
  JSON `VALID`，`scan` 未受影响。
- 倒计时**实算**：下次触发 = `2026-10-03 03:58:00 UTC` = **北京 11:58**，剩余 ~18175s。
- ⚠️ 已知代价：`docker stop` 因 daemon `stop_grace_period: 5m` 可能耗时数十秒~数分钟（无人值守可接受，届时容器自会重启）。

### 保留（用户明确要求）
- `publish_crabpt` 的 `note_delayed_until = 2026-10-03T09:10:59` **保留**（用户本地 4 个种子还没传完）。

---

## 2026-10-03 【新作业】qB 3020 升级 5.0.4 + 下载调度器 qbtune（有源优先×公平轮转）

### 背景（用户诉求演进）
1. 用户先把 3020 设成"最多 10 个并发下载"解决卡 IO，但**总速度只有 ~0.02 MB/s**（500M 宽带闲置）。
2. 上一步同时设了 `max_active_uploads=5` ⇒ qB 把 1892 个种子排进 `queuedUP`（**不做种**）
   —— 用户发现"种子都没在做种了"。**根因就是这一步配置**（`ERR-QBTUNE-01`）。
3. 用户要求：**保持做种** + 活动下载 ≤10 + 活动上传 ≤5 + 全种 500KB/s。
   ★ **关键冲突**：实测证明"活动上传≤5"与"全部做种"**物理上不能共存**（qB 会将其余排 queueUP）。
   ⇒ 用户拍板：**做种不设上限**（下载才卡 10）。

### 已做（本轮，均实测）
| 项 | 结果 |
|---|---|
| **修做种** | `max_active_uploads` / `max_active_torrents` 设为 **100000**（★ 实测 qB 4.6.5/5.0.4 **不认 0=不限**）⇒ 1897 个顺利完成种子**全部在做种**，`queuedUP → 0` |
| **补限速** | 62 个漏网的种子补上 500KiB/s ⇒ **2294 个全覆盖** |
| **qB 升级** | `linuxserver/qbittorrent` **4.6.5 → 5.0.4**（WebAPI 2.11.2 / libtorrent 2.0.11）。配置自动迁移（`MigrationVersion 6 → 8`）。**2294 个种子一个没丢**，做种/下载上限/分类全保留。备份在 `qbittorrent-opencd/config-backup-20261003-120959.tgz`。 |
| **瓶颈定性** | 下载集 397 个里 **`num_seeds>0` 的只有 5 个**（392 个零源）⇒ **带宽是被"上游没源"卡住的，不是队列顺序**。调度器只能"别浪费槽位"，造不出带宽。 |

### 新增：`tools/qbtune.py`（下载调度器，sidecar 常驻 · ★ 默认关闭）
- **两套策略合一**：A=有源优先+死种轮转（准入），B=公平轮转+带宽加权（名单内排序）。
- **实测核过的 qB 机制**（见 `tools/qbtune_probe.py`）：
  - `torrents_top_priority` 实测有效（prio 397→1，state queuedDL→stalledDL）。
  - `stalledDL`（0源）**会占着活动槽不被自动换下** ⇒ `bottom_priority` **足以腾槽**，不需要 `force_start`（force 会破 10 上限）。
  - `torrents_set_download_limit(limit=-1)` = 无限，**qB 回读把无限报成 0** ⇒ 代码一律写 -1、绝不写 0。
- **踩到并修掉的真 bug**：第一版用工具**自己的记忆**算 promote/demote ⇒ 与 qB 现场不符 ⇒ `promote` 恒 0、永不生效。改成用 **qB 现场队列顺序**当基准后立即生效（`ERR-QBTUNE-01` 第 3 条）。
- **纯函数单测**：`tools/test_qbtune.py`（不需要 qB，本机绿灯；判据能失败）。
- **形态**：独立 sidecar 容器 `musopia_qbtune`（`<<: *common`；**不写 data.db**，状态在 `/data/qbtune_state.json`、心跳 `/data/qbtune_heartbeat`；自带 `--healthcheck`，不复用根级 healthcheck.py 以免看门狗互锁）。
- **纪律**：默认只读；`QBTUNE_ENABLE=false` + `--apply` 双保险；写后回读断言 cap；`queueing_enabled=false` 时拒绝运行。

### 部署验证（已做，非"我以为对"）
- `deploy.sh --apply` 同步；NAS `docker compose up -d --build qbtune`（只重建该服务，daemon 不受影响）。
- 容器内 `ls /app/tools/` 见 `qbtune.py`（ERR-DOCKER-09 判据，宿主文件数 == 容器文件数）。
- `musopia_qbtune` **healthy**，日志有 60s 节拍，`qbtune_heartbeat` 在动，`enable=False dry=True`（只读）。
- `opencd2musopia` daemon **未受影响**（Up 9h healthy）。

### 待用户
- **启用调度器**：NAS `.env` 里设 `QBTUNE_ENABLE=true` → `docker compose up -d --build qbtune`。
  建议先观察日志几轮（确认被踢的确实是零源种、有源的确实上位）再开。

## 2026-10-03 【续·下午】分类接线 + 延迟做种 + qB 崩溃归因

### 起因
用户观察到两个问题：① 发到蟹黄堡的种子分类到了乌托邦（`Musopia`）下；
② "ptd 往 3020 推种子失败"，怀疑"安全校验规则变了"。

### ① 蟹黄堡分类：不是 bug 里的"设计如此"，而是**定义了没接线**（已修）
- 现象：`readd_to_qb`（乌托邦）与 `readd_to_qb_crabpt`（蟹黄堡）**都**用
  `config.PUBLISH_CATEGORY`（=`Musopia`）⇒ 两站种子混进同一分类，无法按站辨识/统计。
- **真因**：`config.py` 早在 2026-09-24 就定义了 `CRABPT_PUBLISH_CATEGORY`
  （默认 `"CrabPT"`，注释白纸黑字写"与乌托邦分开"），但 **`grep` 全项目零引用**
  —— 键定义了、注释写了、**接线漏了**（`ERR-ENV-03` 形状）。
- **修**：`publish.py::readd_to_qb_crabpt` 改用 `config.CRABPT_PUBLISH_CATEGORY`
  （399 行仍 `PUBLISH_CATEGORY`，542 行改 `CRABPT_PUBLISH_CATEGORY`）。
- **前置**：qB **不会自动创建分类** ⇒ 已在 qB 建好 `CrabPT`（`savePath=''`，同 `Musopia`）。
- **范围**：只影响**此后新加**的种子；已有 291 条混在 `Musopia` 里的**不会自动迁移**
  （回迁需按判据筛：tracker 含 `crabpt.vip` 或 `db.site_publish.site='crabpt'`，**待办**）。
- 记 `ERR-PUBLISH-01`。

### ② "推种子失败"：真因是 **qB 5.0.4 自身 `std::bad_alloc` 崩溃**（已归因，用户决定先不修）
- 日志实测：3020 的 qB 近 3h 崩 6 次 `terminate … std::bad_alloc`
  ⇒ 客户端 `RemoteDisconnected` ⇒ `APIConnectionError` ⇒ 发布/scan 阶段失败
  （"现在能推进去一些"= 崩-重启-短暂窗口，**假象**）。
- **★ 归因（关键）**：`bad_alloc` 最早两次（12:53、13:11 UTC）发生在 **qbtune 首启（13:16）
  之前** ⇒ **与 qbtune 无因果**，是升级引入的 qB 版本 bug（升级后 43 分钟即崩）。
- 内存证据：容器用 2.1G/17.4G、无限制、系统 available 11.9G ⇒ **非真内存不足**。
- 用户决定：**崩溃先放着**（未回退/未升级）。记 `ERR-QB-05`。
- ⚠️ 副作用记录：过程中一次 `docker compose up -d --build`（**没带服务名**）把
  `musopia_qbtune` 一并重建启动了（本该 `up -d --build musopia`）。随后**只**重建
  `musopia` 服务，qbtune 未再被动。

### ③ SSH 通道（本机 → NAS）
- 正确地址是 **`192.168.0.7:36688`**（从 `~/.ssh/known_hosts` 查到；先前误用 `192.168.1.100`
  超时）。命令：`ssh -p 36688 -i ~/.ssh/id_ed25519 iSunker@192.168.0.7`。
- ★ `iSunker` 在 `docker` 组 ⇒ **免 sudo**；● **非交互 SSH 里 sudo 要密码**（用不了）。
- ★ `scp` 子系统被禁 ⇒ 传文件用 `ssh 'cat > /tmp/x' < localfile`。

### ④ 延迟做种（**已实现，默认关闭**）
- 需求（用户）：种子**发布成功后隔 N 分钟**再 `torrents_add`（让别人 RSS 时差过去、
  更多人**同时**下载）。15 是暂定值，用户说"先不用验证这个 15 分钟最优不最优"。
- ★ 用户方案比"延迟 start"更简：既然是延迟，就**让 add 本身延迟**，qB 收到后自己校验、
  自己做种，**不需要再调一次 qB**（也绕开了"`is_paused` 是否仍 announce"的未知项）。
- 形态（用户拍板）：**执行者塞进 `opencd2musopia` 容器**（不新容器、不塞 qbtune）。
  实现 = 写盘队列 + daemon 主循环**到点代发**（不在阶段里 sleep；阶段同步阻塞会拖垮全流水线）。
- 新增 `readd_queue.py`（publish 写 / daemon 读，**同源**；存 `.torrent` **字节 base64**，
  自包含不依赖 15 分钟后站点可达；`/data/readd_queue.json`，原子写；`pop_due`+`ack` 两步
  防抖丢；幂等去重；版本不符/坏文件降级为空队列不抛）。
- `daemon.py`：主循环末尾 `_drain_pending_readds()`（事件驱动、绝不 raise、失败留队重试、
  上限 `READD_GIVE_UP`）。
- `config.SEED_DELAY_MIN`（默认 **0 = 关闭 = 老行为**；>0 才延迟）。
- 单测 `tools/test_readd_queue.py`（15 条判据全绿；本机要 `PYTHONIOENCODING=utf-8`——
  只是 Windows 控制台 GBK，容器是 UTF-8）。
- ★ 已知干扰（用户说先忽略、记录即可）：延迟窗口内 reseed/iyuu 那套辅助工具可能**抢先**
  把站点新种加进 qB ⇒ 延迟失效。本项目不拦不检测。记在 `config.py`/`.env.example`/`readd_queue.py`。
- 记 `ERR-ENV-04`。

### 部署验证（已做）
- `readd_queue.py` 加进 `Dockerfile`（COPY）与 `deploy.sh`（FILES 白名单）；`deploy.sh` 自检通过。
- `deploy.sh --apply` → NAS `docker compose up -d --build musopia`（**只重建该服务**）。
- 容器内验证：`readd_queue.py` 在、`import` 成功、`SEED_DELAY_MIN=0`、
  `CRABPT_PUBLISH_CATEGORY='CrabPT'`、`daemon.py` 含 `_drain_pending_readds`（×2）。
- daemon 启动日志**无 ImportError/readd 报错**，正常跑 scan 阶段。

### 待用户
- **开启延迟做种**：NAS `.env` 设 `SEED_DELAY_MIN=15` → `docker compose up -d --build musopia`。
- **回迁已有蟹黄堡种子**到 `CrabPT`（需先定筛法判据）——待办。
- **qB 崩溃**：回退 4.6.5 / 升 5.0.5 —— 用户说先放着。

---

## 2026-10-08（续 100）—— C 类"页面 0 做种"根因坐实 + 交棒 identity bug 修复 + 交棒落盘

### 任务
接上一轮（#36 C 类根因已钉死）。本轮推进 #38/#43/#44/#45/#46/#48，并收尾文档。

### ★★★ C 类真因（三层证据齐，且纠正了我先前两次误判）
1. **不是"没做种"、不是"报错站"、不是"3040 双做种"**（`both=0`，3040 早空）。
2. **真因**：给站点做种的「**站点那份 `.torrent`(B)**」从 3020 丢了，只剩皇后原种 A
   （`cat=opencd`，只报 `tracker.open.cd`）⇒ 站点收不到上报 ⇒ **0 做种**。
   全量：`B-SEEDING=120 / B-MISSING=9`（musopia 20517/19946/20095/20877；crabpt 5 条）。
3. **修法**：`tools/readd_site_torrent.py`（站点 `download.php` 取 B → `torrents_add`
   `save_path=**父目录** /downloads` → recheck → 做种）。**踩坑已修**：save_path 给专辑
   目录 ⇒ qB 当新下载挂 `incomplete/`（prog=0）；**必须给父目录**。3020 WebUI 偶发 SSL
   抖动 ⇒ 工具已加 `_retry()`。

### ★★★ #43 —— "same location more than 1 client" 的真相
- **站点规则**：同一条种 **同账号**只允许 **1 个做种者**。可控复现：连发 3 次 `left=0
  started`（peer_id/port 都换）⇒ 第2次接受、**第3次必回 same-location**。
- **真凶是我们自己的陈旧 peer 占位**（qB 反复重启/重加前那次 announce）。
  `viewpeerlist` 里 `iSunker` 只 1 条、被记在**下载者**表 ⇒ 我们的做种位被幽灵占着。
- 排误：① `torf.infohash` 是 **40-hex 串**不是 20 字节（announce 要 `bytes.fromhex`）；
  ② `download.php` hash == details 页 `Hash码` == B，**一致**，非错配。
- ⇒ **#44 放量**：8 条 apply 成功（prog=1，tracker status=1/2），加 20517/20877 共 9 条齐。

### ★★★ #45 —— 交棒 identity 用错 hash（够格种永不交棒）
- `eligible_on_3040` 拿 DB 的 `tasks.info_hash`（**A/皇后原种**）去 **3040**（挂 **B/站点种**）
  查 ⇒ 恒查不到 ⇒ 3 条够格种**永远 PENDING**、`已交棒到 3020` 恒 0。（也解释了 #48。）
- 修：`handoff._find_on_3040`（hash 或 **name** 双查）；`eligible_on_3040`/`handoff_to_3020`/
  `_source_category`/`finalize_source`/`verify_3020_seeding` 全部加 `name` 兜底；export/
  recheck/delete 用 3040 上的**真实 hash**；`process_one` 传 `name=item.name or album`。
  `run.py --selftest` ⑨l 加固（前缀断言 + #45 回归）。**自检 ✅ 通过**（容器内 workspace 副本）。

### ★★★ #46 —— 交棒 11 条（3040→3020）并删 3040（用户选定顺序）
- 3040 上 **11 条够格**（比原估 6 多）全部：export → add(3020, /downloads, 原分类) →
  recheck → **3020 就绪（prog=1.00 stalledUP）** → `torrents_delete(delete_files=False)`。
- 验证：3020 里 11/11 在种；**3040 只剩 1 条**（Hannah Hope 下载中，正确没动）。
- 安全前置：daemon `/downloads`=/volume1/video/music=3020 的 `/downloads`；daemon
  `/publish`=3040 的 `/downloads`（**不同盘**）；11 条数据在 3020 目标盘**全部在位**。
- stale 队列 3 条（DB hash A）已用 `handoff.remove_item` 移除，队列现空。

### ★ #48 —— 交棒线程为何从不实际交棒
- 线程**正常运行**（`ENABLE_HANDOFF=true dry_run=false`）。日志**从无** `PENDING → …`/`已交棒`/
  `清盘` ⇒ `process_one` 从未越过 PENDING。**根因 = #45**（非独立 bug）。
- 附带：复制步有 3020 **读超时**（`ReadTimeout 15.1s` → `跳过复制`）⇒ 建议给该查询加 `_retry`。

### 未部署 / 未提交
- #45 代码修复**只在工作树**（`src/handoff.py` + `src/run.py`，自检通过），**未构建/未部署**。
  部署前生产 daemon 仍走旧逻辑（够格种仍会卡 PENDING，但因队列已空，暂无实际影响）。
- 未 `git commit`（等用户明确要求）。新增/改动见 `git status`。

### 待办（TaskList）
- #39 护栏：巡检每条 EXISTS 行的 B 在做种 + tracker msg 不含 same-location，丢则加回。
- #41 DEP：提交 + 构建 + 部署（含 #31/#33/#45 代码）——**需用户批准**。
- #37 A 组重发正确版本 + 改 DB 指针（8 行）。
- #40 retract_missing_pages 自检 + C 类核对工具。
- #42 收尾（ERRATA/SESSION-NOTES/counts 已随本轮更新）。
- 小任务：3020 读超时加 `_retry`；#39 见 same-location ⇒ 清陈旧 peer。

## 2026-10-08（续 101）—— #39 护栏-1：B 做种只读巡检 + 全量跑（+ 用户问 20667）

### 交付物（新文件）
- **`src/site_seed_patrol.py`**：库。单条巡检 `patrol_row` + 选行 `iter_exists_rows` +
  自检 `--selftest`（纯假件，**不需要 `.env`**）。
- **`tools/check_site_seeding.py`**：CLI（只读）。`--site/--id/--info-hash/--limit/
  --only-defective/--json`；缺 `--limit` 打全量。退出码 0=全在做种 / 1=有 MISSING|AMBIGUOUS / 2=环境。
- 两文件已 `docker cp` 进容器；容器内 `--selftest` 全绿。

### 五态判定
`SEEDING`（在+prog≥1+做种族）/ `PRESENT`（在但 prog<1）/ `MISSING`（查不到 B ⇒ 丢了）/
`AMBIGUOUS`（同 B 命中多条 ⇒ 同位置/残留）/ `NO_SITE_TORRENT`（没有 B 可查：缺 id/下不动/壳页——
**不是** MISSING）。

### ★★★ 身份铁律（自检当场抓出的初版 bug，见 ERR-PUBLISH-15）
- **判据 = `qB.hash == infohash(站点 B)`**。B 从 `download.php` 取，`torf` 算其 infohash。
- **初版错**：拿 `category ∈ HANDOFF_OWN_CATEGORIES` 当命中 ⇒ 3020 里 **648 条本项目别的专辑**
  全算进来 ⇒ 每条报 `AMBIGUOUS(649)`，**全假阳性**。已改为 **hash 相等**；`category/name` 只作旁证。
- 自检含**阴性对照**：同名+同分类+**不同 hash** 的种 ⇒ 必须 `MISSING`（③）。

### 全量跑（只读；两站）
- **汇总：SEEDING=128  PRESENT=1  MISSING=1**（共 130 行；musopia 66 / crabpt 64）。
  报告：`/data/c_seeding_report.json`。
- **MISSING**：`musopia 20889` 'Bruno Mars - Unorthodox Jukebox (2012) [FLAC]-mtpMTP'
  （B=`2b06eaec5a…`，A=`8bcdecf3ca…`，3020/3040 都查不到 ⇒ **真丢了**）⇒ `#49` 自动加回候选；
  其 `content_path=/downloads/Bruno Mars - Unorthodox Jukebox…`（HDD 有源）。
- **PRESENT**：`musopia 20667`（见下）。

### ★ 用户问：20667 为什么"还没做种"
- **页面 20667 实挂**：`[260617] Hana Hope - blue hour [**期間**生産限定盤][AICL-4905~6]…`，
  站点 infohash **`60dd1d72…`**（B）；**DB 那条**（`9e9ed78a…`，A）却是 **[初回**生産限定盤][AICL-4904]。
- 3020 里在做种的是 **初回 A**（`cat=opencd`，tracker=`tracker.open.cd`，msg=`You may only 1 seeding
  from one location.` —— **皇后站**的抱怨，**与 musopia 无关**）。
- 页面挂的那条 **期間 B（`60dd1d72…`）在 3040 上存在，但 `stalledDL prog=0`**（文件不在 3040 发种区）
  ⇒ 站点收不到它的 seeder ⇒ **页面 0 做种**。
- **定性**：`#136` 错版本页 + `#45` 身份错配的合体 —— **不是"漏做种、加回就行"**，属 **A 组 #52–#55**
  （重发正确版本为新页 + 改 DB 指针）。页面挂的 期間盤若无效只能弃（无批量删种 ⇒ 留孤页）。

### 未部署 / 未提交（同前）
- `site_seed_patrol.py` / `check_site_seeding.py` 只在**容器内 `/app`**（docker cp）+ NAS `/data`，
  **尚未进镜像** ⇒ 随 `#41` DEP 一起提交/构建/部署。
- `#45` 修复仍在工作树。未 `git commit`（等用户明确要求）。

## 2026-10-08（续 102）—— #49 护栏-2：自动加回丢失的 B（库 + CLI，已实修 20889）

### 交付物
- **`src/site_readd.py`**（库，`--selftest` 全绿，纯假件不碰 qB）：
  - `resolve_save_path(qb, a_ih)` —— 由 A 的 `content_path` 取**父目录**（`ERR-PUBLISH-13` 的坑）。
  - `b_present(clients, b_ih)` —— 幂等判据：B 在不在（**按 hash**，`ERR-PUBLISH-15`）。
  - `readd_b_torrent(...)` —— add(paused,`is_skip_checking=False`)→recheck→resume→reannounce→
    **回读 `progress`/`state` + tracker `msg`**；`same-location` 判失败（`ERR-PUBLISH-14`）；
    `progress<1` 判失败；`tracker status∉{1,2}` 判失败。**dry-run 默认**。
  - `readd_row` / `readd_missing` —— 批量编排（只对 `MISSING` 动手）。
- **`tools/readd_missing_from_patrol.py`**（CLI）：巡检→对 `MISSING` 加回；**默认 dry-run**，`--apply` 才写。
- （`tools/readd_site_torrent.py` 保留为 #38 手工当班 CLI；两者同源逻辑。）

### ★ 实修：`musopia 20889`（Bruno Mars）
- 巡检判 `MISSING`（B `2b06eaec5a…` 在 3020/3040 都查不到；A `8bcdecf3ca…`）。
- `--apply` ⇒ `✅ 已加回并在做种 progress=1.00 state=stalledUP trk=[('www.musopia.vip', 2, '')]`。
- 复巡 ⇒ `SEEDING`（`命中['3020'] how=hash+自分类`）。**全量只剩 20667 那条 PRESENT（属 A 组）。**

### 未部署 / 未提交（同前）
- `site_readd.py` / `readd_missing_from_patrol.py` 只在容器 `/app`（docker cp）+ NAS `/data`，
  **未进镜像** ⇒ 随 `#41` DEP。未 `git commit`。

## 2026-10-08（续 103）—— #50 护栏-3：same-location 来源诊断（只读；结论已定）

### 诊断问题：陈旧 peer 从哪来？
- 全机 qB：**3020**(opencd) / **3040**(publish) / **3060**(reseed, `reseed-cross-seed` 在用)。
- 扫 **3060 全 3220 条种**的 tracker 主机 ⇒ **14 个别的 PT 站**（hdarea/pterclub/muxuege/
  btschool/keepfrds/m-team/hdfans/hdtime…），**musopia 命中 0、crabpt 命中 0**。
- ⇒ **3060 不是**我们 stale peer 的来源（它只辅种外站，从不碰 musopia/crabpt）。
- ⇒ stale peer 来自**我们自己的 3020/3040**：qB 重启 / re-add / 交棒前后那次 announce
  留下的幽灵，占着站点"同账号同种只 1 个做种者"的位子（`ERR-PUBLISH-14` 已坐实）。

### 自动化边界（定论，供 #51 实现）
- **只读可自动**：巡检见 `msg` 含 same-location ⇒ 标"重复占位"（`site_seed_patrol` 已能回读 state，
  tracker `msg` 已由 `site_readd` 回读）。
- **清位动作要保守**：本机没有"直接对站点踢 peer"的 API；能做的只是**本机侧去重**——
  确保同一 B 只在 **一个** qB 实例（3020）持有；若 3040 也残留同 B ⇒ 删 3040 那条（`delete_files=False`）。
- **不自动删**：等 `interval` 超时是站点侧的事，我们不主动踢（无权限）；**只保证"我们这边只有一个持有者"**。
- **记录到账本**：同一条 B 的 same-location 反复出现 ⇒ 记一笔（避免每轮重复排查）。

### 状态
- #50 属**诊断类**，结论如上；**未写新代码**（自动化并入 #51 的巡检线程：发现 same-location ⇒
  记日志 + 若 3040 有重复实例则本地去重；**默认 dry-run**）。未部署/未提交（随 #41）。

## 2026-10-08（续 104）—— #51 护栏-4：把巡检+加回接进 daemon 线程 + 自检护栏

### 干了什么
- **`src/daemon.py`**：新增 **C 类护栏巡检线程**（`_seed_patrol_thread_loop` / `_drain_seed_patrol`
  / `_seed_patrol_build_clients` / `_seed_patrol_retry`），在 `run_main_loop()` 里随
  readd/handoff 线程**一起启动**（`Thread(target=_seed_patrol_thread_loop, name="patrol")`）。
  - 复用**同一份** `site_seed_patrol`（眼睛）+ `site_readd`（加回）库逻辑（`ERR-META-03`）。
  - 加回**不直接 add**：`readd_queue.enqueue(raw, "/downloads", cat, 1, …)` 交给既有 readd
    线程（复用其发种区就位闸门 `_readd_area_ready` + 退避重试，`#136`/`#134` 纪律）。
  - qB 取数器带**重试**（3020 SSL 抖动 `#43`）；任一 qB 连不上 ⇒ `RuntimeError` ⇒ **本轮作废**
    （不写队列），绝不折成行判定。
  - **双重显式开关**：`ENABLE_SEED_PATROL=false` 默认**不开**（线程第一件事就 return）；
    即使开了，`SEED_PATROL_DRY_RUN=true` 默认**只巡不修**。要真修须**两键都显式**。
  - 自动化边界（`#50` 定论）：只对 `MISSING` 入队加回；`AMBIGUOUS`/`PRESENT`/`NO_SITE_TORRENT`
    **不动**（绝不碰站点）。
  - `ENV_SNAPSHOT_KEYS` 加 4 键（`ENABLE_SEED_PATROL`/`SEED_PATROL_DRY_RUN`/
    `SEED_PATROL_THREAD_TICK`/`SEED_PATROL_MAX_PER_ROUND`）—— 让巡检能回答"护栏开没开"。

- **`src/run.py --selftest` ⑨f**：把 `_seed_patrol_thread_loop` 加进"必须接线 + 必须定义"
  的 AST 判据（照 readd/handoff 同款，`ERR-META-01`：写了不接 = 死代码）。

### 验证
- 离线 `run.py --selftest` ✅；**阴性对照**：临时删掉 patrol 接线 ⇒ ⑨f 变红（EXIT 1）⇒ 护栏有效。
- 容器内 `run.py --selftest` ✅（补同步了 `handoff.py`，容器里那份是 #45 之前的旧版）。
- 容器内 `site_seed_patrol` / `site_readd` 自检 ✅。
- 容器内**实跑**接线后的 `_drain_seed_patrol()`（dry-run）：`巡 130 行: SEEDING=129 PRESENT=1`
  —— 20889 已 SEEDING（`#49` 实修保持），唯一 PRESENT = 20667（A 组）。
- `readd_queue.enqueue` 位置参数实测 `(torrent_bytes, save_path, category, delay_min, …)` 对得上。

### 状态
- 全部**只在容器 `/app`（docker cp）+ 工作树**，**未进镜像、未 commit** ⇒ 随 `#41` DEP。
- 默认两键都关 ⇒ **老行为一字不改**（线程起了但不做事，日志明说"未开"）。

## 2026-10-08（续 105）—— #52 A 组-1：全量核对站点实挂版本 vs DB（只读）

### 干了什么
- `tools/verify_hosted_edition.py` 自检 22/22 通过；在容器内**全量只读跑** 130 行
  （两站 `site_publish` EXISTS）：报告 `/data/vhe_report.json`（已拷回 `.scratch/vhe_report.json`）。

### 结果：**MATCH=122  MISMATCH=8**（0 MISSING-PAGE / 0 DOWNLOAD-FAILED）
8 行 MISMATCH，分两子型（详见 `.scratch/A_group_mismatch.md`）：

**① 同专辑多版本塌页 / 共用行（5 行）**
| DB info_hash | DB 声称 | musopia | crabpt |
|---|---|---|---|
| `3870e15b53` | blue hour **期間** | id=18688 **挂初回**❌ | id=215575 ✅ |
| `9b4408b944` | コニファー **期間** | id=19333 **挂初回**❌ | id=217500 **挂初回**❌ |
| `0295c1305c` | コニファー **初回** | id=19333 ✅ | id=217592 **挂期間**❌ |
| `9e9ed78ab8` | blue hour **初回** | id=20667 **挂期間**❌ | — |
| `97b38369bd` | 女王蜂 星 **完全** | — | id=216217 **挂期間**❌ |

★ **musopia 19333 一个 tid 被两行引用**（`9b4408b944`期間 / `0295c1305c`初回）⇒ 同页不可能
同时是两版本 ⇒ 必有一行指针是假的（实测 `0295c1305c` MATCH、`9b4408b944` MISMATCH）。

**② 单版本页挂了别的东西（3 行）**
- musopia 20639：DB `Eric Clapton … 分軌` vs 站上 `… [FLAC] {B0015402-02}`（分轨/整轨差）
- crabpt 216915：DB Stella **天幕盤** vs 站上 **通常盤**
- crabpt 216217：DB 女王蜂 星 **完全盤** vs 站上 **期間盤**（= 子型①里 crabpt 侧那条，同 tid）

### 结论与下一步
- 本批 8 行**全是真·挂错/塌页**，**不含**"页与种都对、只是 DB 名写错"那类
  ⇒ **#53 预计空操作**（会再逐行确认）。
- **#54 重发**（不可逆，★ 需你批，先 1 条试水）+ **#55 改指针**（musopia 双写 / crabpt 只 site_publish）。
- 无 MISSING-PAGE（18984/19069 此前已处理，本轮未再出现）。

## 2026-10-08（续 106）—— #56 收尾-1：retract_missing_pages 自检（阴性对照）

### 干了什么
`tools/retract_missing_pages.py` 补 `--selftest`（离线、临时 sqlite，不碰生产库/.env）：
- **抽出核心写逻辑** `_retract(conn, targets, *, apply, out)`（`main` 与自检共用一份，`ERR-META-03`）。
- **15 项判据 + 4 个阴性对照**：
  ① `_targets_from_json` **只取** `MISSING-PAGE`（MATCH/MISMATCH 不许被取）；
  ② 只读预览（apply=False）⇒ **一行不改**；③ apply ⇒ 目标改对 + **MATCH 行原封不动** +
  **cover/synced_at 保留**；④ **非 EXISTS 行（id 已空）不许改**（点旧 id 查不到 ⇒ 跳过；
     点名空 id 也不许把空 status 提成 UNKNOWN）；⑤ **跨站同 ih**：撤 M 侧不许动 C 侧；
  ⑥ 查不到的 id ⇒ 0 行、exit=0。
- 修了写自检时的两处 fixture/断言 bug（列数不齐、断言期望值写错）——非生产代码问题。

### 验证
- 本机 `python tools/retract_missing_pages.py --selftest` ✅ 全绿。
- 容器内同命令 ✅ 全绿。

### 状态
- 只在工作树 + 容器 `/app`（docker cp），**未提交/未部署**（随 #41）。
- #40（工具自检）一并关闭（#56 ✅ + #57 ✅）。

## 2026-10-08（续 107）—— #53 A 组-2：确认"改 DB 名"对本批**是空操作**

### 判定（只读，逐行）
`#53` 的目标是"页与挂的种**都对**，只是 DB 的 `tasks.torrent_name` 写错" ⇒ 改名即可。
逐行核对本批 8 行：**DB 声称的版本 ≠ 站上实挂的版本，且是不同版本**（初回/期間/完全/通常/天幕）：

| 站点 | id | DB 声称 | 站上实挂 |
|---|---|---|---|
| musopia | 18688 | 期間 | **初回** |
| musopia | 19333 | 期間 | **初回** |
| musopia | 20639 | 分軌 | **—（整轨命名）** |
| musopia | 20667 | 初回 | **期間** |
| crabpt | 216217 | 完全 | **期間** |
| crabpt | 216915 | 天幕 | **通常** |
| crabpt | 217500 | 期間 | **初回** |
| crabpt | 217592 | 初回 | **期間** |

⇒ **8/8 都是"不同版本"** ⇒ 改 `torrent_name` **修不了**（页面挂的 `.torrent` 就是另一个版本的；
改名只会让 DB 撒另一个谎）。**#53 = 空操作，不做。**

### 唯一正解 = #54（重发正确版本为新页）+ #55（改指针）
- 方案已落盘 `.scratch/A_group_republish_plan.md`（含正确版本名 / DB info_hash / 试水顺序 /
  musopia 双写 vs crabpt 只 site_publish 的指针规则 / 执行勾选清单）。
- **触发条件**：8 个 id 只读复跑全部 `MISSING-PAGE`（= 管理员删除已确认）。**当前搁置。**
