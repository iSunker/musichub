# MusicHub 3.0 —— PT 音乐矩阵自动化中枢 (AGENTS.md)

> **文档定位**：本文件是 `D:\Projects\PT-Project\musichub` 子项目的智能体与开发者总规约，遵循 `AGENTS.md` 规范。
> **生产部署形态**：NAS 容器 `musichub`（工作目录 `/volume2/docker_ssd/musichub`，网络接入 `host` 模式）。

---

## 0. 项目定位与架构职责

MusicHub 3.0 是 PT 音乐抓轨资源的全自动分发、保种、背压调节与生命周期管理中枢：
1. **多源采集**：从 OpenCD 皇后抓轨等原种源抓取音频资源与抓轨报告。
2. **元数据提取与检查**：利用 Mutagen 与音频探针（`audio_inspector`）完成分轨音频校验、CUE 语法分析及封面抓取。
3. **多站分发**：自研 NexusPHP 适配器矩阵，支持发布到乌托邦（Musopia）、蟹黄堡（CrabPT）、不可躺（TangPT）、星湾（XingWan）、牧雪阁（MuXueGe）、红豆麒麟（HDKylin）、PterClub 等多个站点。
4. **两阶段防偷跑回加**：支持延迟注入队列（`delayed_queue` / `delayed_readd`），解决站点发种时自动生成的 infohash 尚未入库引发的校验失败或异哈希偷跑流量问题。
5. **存储背压与生命周期轮转（Backpressure & Handoff）**：监控机械盘卷（`/volume1/video`）与 SSD 缓冲卷（`/volume2/qb_temp`）水位，自动执行安全 GC、多池交棒（`music-pool1~5` / `music1~3`）与断种巡检。
6. **3016 下载调速治理与生命周期闭环（2026-11-24 架构升级）**：3016 实例已正式纳入 `MultiPoolDownloadGovernor` 五档阶梯调速器与 300s 超时轮转暂存治理；原 `DownloadQueueJanitor` 自建线程正式退役，死种最终销毁平滑交由 Vertex 原生删种引擎（`3016-未成活死种48h自动清除`，`onlyDeleteTorrent: true`）统一管理，实现「调速限速 + 5分钟让位 + 轮转再激活 + Vertex 48小时终态回收」的完整闭环。

---

## 1. 基础设施与端口映射

- **发种端 QB**：`qbit-publish`（端口 `3010`，host 网络）
- **下载缓冲端 QB**：`qbit-musicdownload`（端口 `3016`）
- **目标音乐池 QB（2026-11-23 规范化命名与挂载，容器名 = `qbit-music-<端口序号>`，按端口升序编号 1~8 全平权）**：
  - `qbit-music-1` (3210, `music_pool_1`)、`qbit-music-2` (3220, `music_pool_2`)、`qbit-music-3` (3230, `music_pool_3`)、`qbit-music-4` (3240, `music_pool_4`)
  - `qbit-music-5` (3250, `music_pool_5`)、`qbit-music-6` (3260, `music_pool_6`)、`qbit-music-7` (3270, `music_pool_7`)、`qbit-music-8` (3280, `music_pool_8`)
  - ★ **8 池完全平权**：解除 3260 皇后专属硬锁，动态均衡分发，同专辑外站发种全部收拢归集至同一实例，零跨池硬链接（`EXCLUDED_LOAD_POOLS=frozenset()`）。
  - 挂载结构：每个容器统一挂载 `/downloads` $\to$ `/volume1/video/download/music/music_pool_X`，配置 `/config` $\to$ `.../music_pools/config_poolX`，临时 `/downloads/incomplete` $\to$ `.../temp_music_poolX`。
- **存储路径**：
  - 机械盘媒体池：`/volume1/video/download/music`
  - SSD 缓冲目录：`/volume2/qb_temp/completed_musicdownload`
  - 发种缓冲目录：`/volume2/docker_ssd/qbit-all/data/publish/Download`

---

## 2. 核心运行纪律与铁律

1. **发种黄金窗口期容器操作禁令**：
   **严禁在东八区（UTC+8）每天 00:00 - 10:00 期间重启或重建（`--force-recreate`）容器！**
   - 调度器在 `00:00 - 10:00` 期间自动避让重度扫描任务（`scan` 与全库 `patrol`），专注执行各站点发种与延迟回加。
2. **凭据安全**：
   - 依赖 CookieCloud 动态获取站点 Cookie，不得将任何 Cookie、Passkey、Token 写入代码或提交到仓库。
3. **同 info-hash 单实例铁律**：
   - 任何跨站或辅种注入动作前必须查全局，同一 info-hash 只能存在于一个 qB 实例。
4. **与 Vertex 3016 防环联动（方案 C 账本对齐）**：
   - **背景**：随着发布矩阵升级为多对多，外站（7 站）既作为发种目标站，又作为 Vertex 3016 的新种输入站。为防止自家发布的匿名种子被 3016 RSS 重新下回造成闭环：
   - **账本实时同步**：在 `musichub/routing/graph.py` 的 `record_publish` 中集成 `export_vertex_blocklist()`，每次发布成功后自动将已发 TID 账本导出至 `/vertex/data/published_music_ids.json`。
   - **Vertex 源头拦截**：Vertex 侧 `rule/rss/30160002.json` 实时比对该账本进行源头拒收（`rss/30160003.json` 通过 `acceptRules` 引用该规则）；严禁用正文共有特征（如 `转自OpenCD` 或 `- FLAC 16bit`）猜测拦截，杜绝误杀外源正常做种。
   - **启动自愈**：daemon 启动时先导出一次账本，账本文件丢失/被覆盖可立即恢复，不必等下一次发种。
   - **禁止 UNC 兜底**：`export_vertex_blocklist()` 只写容器内生产路径或显式传入目录，`:memory:` 库一律不导出。2026-11-21 曾因本机跑单测经 `\\iSunker-DS423\...` 覆盖生产账本，1834 个已发 ID 全部丢失。
   - **重启孤儿风险**：发种在途时重启容器会造成「站点已收种、本地无账本无归档」，3016 随即抓回成回环。重启尽量避开发种轮次，事后须核对站点 ID 序列缺口。
5. **硬链接与目录保护**：
   - 任何清理操作必须避开系统保留目录（`incomplete`, `@eaDir`, `.RecycleBin`）与未纳管池（如 `music5` 禁删红线）。

6. **3010 达标交棒调度（2026-11-21 重构）**：
   - 独立线程 `HandoffGovernorThread`：120s/轮、`max_batch=60`，走批量流水线（一次停源 / 按池分组唤醒 / 一次下架），单轮通常 3~5s 收口；严禁再挂回主循环的 `900s` 门限（旧实现的队头阻塞根源）。
   - **预过滤优先**：只有 `match_library()` 判定为 `hash` 的任务才占用 batch 名额；`name+size` 只作诊断，未就绪任务不参与排队，避免「前 10 个未就绪挤掉后面全部可交棒项」。
   - **判据口径**：只接受同 infohash；同名同大小也不能证明目标站 B 种已接管。单个与批量执行入口必须再次 fail-closed 校验 `match_kind=hash` 且池 hash 与 3010 B hash 完全一致。
   - **真实做种时长**：够格判定只读 qB `seeding_time`，不得以 `now-added_on` 冒充；字段缺失按 0，保守不交棒。`HANDOFF` 流水必须记当前 B hash，并标注 `match_kind=hash`。
   - 交棒动作恒为 `deleteFiles=false`，SSD 发种区只解除软链接，不删实体数据。
   - **存量必先回填**：交棒只对「池内已有接管任务」生效；历史停在 3010 的 B 种必须先跑 `tools/backfill_pool_pairing.py --apply --only-missing` 注入池，否则 `ready_count` 恒为 0（2026-11-21 实测 30 个够格种在 8 池「完全未收录」，根因不是交棒慢而是无对象）。

7. **3010 上传带宽治理（2026-11-21）**：
   - 队列并发：`max_active_uploads` 必须 ≥100（现 200），`max_connec_per_torrent`/`max_uploads_per_torrent` ≥8；旧值 5/3/4 会把 200+ 种子压在 `queuedUP`。
   - 单种上限按阶梯给足、由 qB 全局 `up_limit=5MB` 兜底：达标 50KB/s 保活、新种 256KB/s、爬坡 2MB/s、全速 4MB/s、收尾 1MB/s；**严禁再用「预留额度扣减总预算」的切分法**（42×50KB+206×100KB=22MB 预约会把剩余带宽钳到最小值）。
   - 实测提醒：单种放开限速后仍受 leecher 数量约束（3 个 peer 峰值约 750KB/s），吃不满 5MB 通常是无下载需求，不是限速配置。
   - 调速器每轮按 `target_limit` 分组批量下发，禁止逐种串行 POST。

8. **3016 下载队列不可被无源种锁死（2026-11-21 教训）**：
   - `max_active_torrents`（总活跃 60）与 `max_active_downloads`（下载并发，现 25）是两道独立闸门；`stalledDL` 属 active download，**占着槽位不会自动让位**。
   - 关闭 `dont_count_slow_torrents` 后慢速种子计入限额，11 个 0 做种任务即可锁死全部下载槽，表现为「站点明明有几十个做种者的种子全部 `queuedDL`」。
   - 处置口径：只清「零进度 + tracker 报告 0 做种 + 加入 >2h」的 `stalledDL`，一律 `deleteFiles=false`；并发值保持 25，不得再调回 15。
   - **禁止用 `docker exec` 起的临时进程充当常驻消费者**：容器重启即失联且无告警。常驻能力一律落在 `daemon.py` 的内置线程（现为 `DelayedReaddThread` 与 `QueueJanitorThread`）。

9. **发布期音乐池配对（2026-11-21 交棒失效根治）**：
   - **问题**：3010 上的 B 种带各站 tracker/passkey，info-hash 与池内 OpenCD A 种天然不同；旧链路只把 B 种回加 3010，从不写入音乐池，导致交棒器唯一合法的 `hash` 判据无法命中，达标种子永远交不出去。旧 `name+size` 兜底会唤醒 A 种却删除 B 种，已禁用。
   - **机制**：`publisher/engine.py` 在 3010 保活探针确认 100% 后，调用 `storage/pool_pairing.py` 把**同一份 B 种字节**以 `paused=True + skip_checking=True` 注入到「物理承载该专辑的池」（`music1~3` / `music_pool1~5`），`save_path=/downloads`。
   - **安全前置**：注入前必须按种子文件清单逐项校验存在性与 size；目录缺失时仅在目标不存在的前提下建立**同卷硬链接**补齐，绝不复写已有数据；`music5` 硬拦截。
   - **单实例铁律的边界**：配对任务必须保持 `paused`，不向 tracker 公告，避免同 hash 与 3010 同时做种；交棒时由 `HandoffGovernor` 先 `stop` 3010，再 `start/resume` 池任务，成功后才从 3010 下架（`deleteFiles=false`）；唤醒失败则保留 3010，防止无人做种空窗。
   - **交棒器扫描范围**：必须覆盖全部 8 个受管池（不再只扫 `music1~3`），否则配对落在 `music_pool_*` 的任务不可见。
   - **GC 引用集合**：真删前必须把「全部池的 qB 任务名」并入活跃集合，交棒后 3010 已无该任务、池内仍在做种的目录不得判为孤儿。
   - **存量回填**：`tools/backfill_pool_pairing.py` 按 infohash 从 `data/torrents/*.torrent` 归档反查种子字节，默认 dry-run，`--apply` 才写入。
   - **重复注入两个缺口（2026-11-21 修复，详见专题 05：`docs/nas_cluster/postmortems/05_qB集群底层运维_多处做种分诊与存储IO调优专题.md`）**：
     - `select_target_pool()` 必须**先全池查重（含 music3）再认 `prefer_pool`**。历史实现把 `prefer_pool` 放在查重之前直接 return，使「人工指定池补种」（`pair_published_torrent(..., target_pool_name=...)`）成为绕开单实例铁律的后门；冲突时按既有归属池执行并告警。
     - 注入后 `verify_timeout`（默认 20s）内未观察到 100% 时，任务其实**已经落地**：必须 `_record_landed()` 把 qB 实况（真实 progress/state）写进选池与查重快照缓存、并记一条 `PAIR` 流水（reason 标注「按已落地记账，禁止换池重注」），**不得只 `return False`**——否则 60s 缓存窗口内下一轮换池再注就是跨实例双活。进度**不得**伪写成 100%，否则会被当成已完成而跳过复核。

10. **池入栈/出栈账本与查重快路（2026-11-21）**：
   - **三层定位**（不可混用）：`pool_event_log` 是只增不改的审计流水；`cluster_infohash_registry` 是当前在册快照（复合主键 `hash+实例`）；`pool_index_meta` 记录各池快照水位。
   - **事件口径**：`PAIR`（配对注入）/ `HANDOFF`（池任务转正、3010 下架）/ `IN` `OUT`（全量刷新发现的池内增删）/ `CONFLICT`（同 hash 跨池，违反单实例铁律）。
   - **快路边界**：`HandoffGovernor.get_library_index()` 仅在「8 池水位全部新鲜（默认 TTL 1800s）」时读库，任一池过期/缺失/读库异常立即回退全量拉取。**严禁把 DB 当唯一真相**——qB 侧人工删任务与幽灵做种都会让库漂移。
   - **接口成本实测**：单轮 8 池全量约 10.2 MB / 1.4 s（3260 独占 4.9 MB）；改为快路后 720 轮/天 → 约 48 轮/天。
   - **机械盘无关**：`/api/v2/torrents/info` 返回内存元数据，实测 100 次全量拉取期间 qB 进程 `read_bytes` 单位时间增量低于空转（126 MB/s vs 156 MB/s），不触发磁盘寻道。真正吃机械盘的是 torf 打种、`verify_payload` 与并发上传读取。
   - **对账**：`reconcile_pool_index()` 每日随交棒线程跑一次，用实况覆盖快照并记录漂移，防止长期失真。

11. **发种真伪判定解耦、纯数字 TID 保护与单调递增性（2026-11-24 重构）**：
   - `site_publish_graph` 必须区分 `publish_status`（`PUBLISHED` 本账号真发 vs `EXISTING` 站点收录/辅种）；
   - `target_torrent_id` 仅存纯数字 TID，**严禁写入 `'EXISTING'` 等任何字符串占位符**；查重命中由适配器 `check_exists_detail()` 提取详情页数字 TID（若无提取到则置空 `''`），杜绝下游 `int(tid)` 崩溃；
   - **单调晋升保护门禁**：`PUBLISHED > EXISTING`。已标记为 `PUBLISHED` 的记录，后续无论遇到何种查重命中均严禁降级为 `EXISTING`；
   - **Vertex 3016 防环账本汇聚导出**：`export_vertex_blocklist()` 兼顾直接目标站 `target_torrent_id` 与跨站矩阵传播的各站点专有列 `SITE_TID_COLUMNS`，只要在目标站存在纯数字 TID（无论是 `PUBLISHED` 还是 `EXISTING`），全量导出至 `/vertex/data/published_music_ids.json`，确保 3016 下载队列 100% 免疫回环；
   - 存量真伪对账与数据清洗统一通过 `python musichub/tools/reconcile_publish_truth.py [--apply]` 运行。详见 [专题 11：发种真伪解耦、数字TID保护与存量流转治理专题](../docs/nas_cluster/postmortems/11_发种真伪解耦_数字TID保护与存量流转治理专题.md)。

12. **发种来龙去脉台账（`publish_lineage`，2026-11-21 新增）**：
   - `site_publish_graph` 只存指纹 + 站点 tid，**无法回答「某个站点 tid 本地是哪个 B 种、该去哪个池补种」**；2026-11-21 lemonhd#48704 就是这样丢失的（只有 EXISTING 记录，无 tid、无 B 种，站点在挂种而本地无任务）。
   - 三个发种入口必须在 `record_publish` 里带 `b_hash`（用 `torrent_infohash(站点回传 B 种字节)` 算）、`target_url`、`qb_instance`/`qb_port`/`save_path`，台账同时落 `publish_lineage`。
   - 查询入口：`python musichub/tools/find_lineage.py --tid 48704 --site lemonhd`（也支持 `--source-hash` / `--hash` / `--album`；台账缺 b_hash 的历史行加 `--fetch` 从站点下载 B 种反查），会现场探针 3010 + 8 池，直接判「在不在做种、在哪个池」。
   - 补种正式入口：`python musichub/tools/repair_seeding.py --target <site>:<tid> [--target ...] [--apply] [--format json]`，默认 dry-run。必须先从本地归档或站点取回并解析 B 种，再全集群查同 hash、按 B 文件清单逐项核实体；唯一 paused 且 100% 才可原地 resume，完全缺失才可 paused+skip_checking 配对，回读 100% 后才 start。
   - 写入前必须再次全集群查重；最终验收是唯一 active、`progress=1`、seeding state、tracker working/status 2，并保留 `seeding_time/uploaded`。禁止自动下载、禁止硬编码池、禁止触碰 `music5`、禁止删任务或文件；权限页、无 torrent、无数据、多个 active 与身份歧义均 fail-closed。
   - **详情页数据库（2026-11-21 新增）**：归档范围仅为 `site_publish_graph` / `publish_lineage` / `publish_attempt_log` 合并后的 MusicHub 已发布纯数字 tid，不是全站爬虫；三表必须先合并、去重、稳定排序，再分页或限制数量。`site_detail_pages` 保存每个 `(site,tid)` 最新净化 gzip HTML、HTML SHA256、解析字段、封面 URL、抓取状态与 parser 版本；`site_detail_observations` 只增保存 seeders/leechers/completed/page_status 观测历史。不得保存 Cookie、响应头或明文凭据，URL 中 passkey/token/auth 等参数必须净化。
   - **归档与统一审计入口**：`python musichub/tools/audit_onsite.py --sync [--apply] [--force] [--site X --tid N] [--days N] [--limit 0]`；`--sync` 默认只列计划，`--apply` 仅写详情页库，默认只补未抓取或超过 24h 的页面，`--force` 才重取所选目标。`--audit` 只读汇总归档页面、严格封面身份/首图字节、B hash、qB state/progress/seeding_time/uploaded、tracker 与页面下载需求；`--limit=0` 不截断，输出必须核对 eligible/selected/truncated。
   - **证据边界**：页面可访问、缓存 URL 命中、qB `progress=1` 均不是封面身份、实体数据或 tracker 健康的单独证明；`NO_DOWNLOADERS` 只是需求观测。报告必须区分当次事实、历史快照与机制推断，写后重新归档并逐项目回读。长期操作规约与工具地图见 `skills/musichub/SKILL.md`。

13. **站点详情页编辑与封面补齐（2026-11-21 新增）**：
   - 工具：`musichub/tools/edit_onsite.py`；规则表：`musichub/onsite/site_rules.py`；编辑器：`musichub/onsite/editor.py`。
   - 常用：`--print-mapping` 看逐站字段/封面/日志/标题约束；`--site X --tid N --check` 体检；`--fix-cover [--apply]` 补封面；`--scan --days 14 [--fix-covers --apply --yes]` 批量；`--set name=...` 改标题/副标题/简介。
   - ★ `takeedit.php` 是**整表覆盖**：必须先 GET `edit.php?id=` 读基线、只覆盖目标字段再提交，禁止只 POST 改动字段（会清空其它字段）。
   - ★ 封面根因：`build_body` 只在 `cover_url` 非空时输出 `[img]`；RSS 条目 `source_tasks` 为空 ⇒ `cover_url` 恒空。补封面优先「本地专辑 front/cover/folder 图 → 图床」，合辑取一层子目录封面。
   - ★ 图床：`PICGO_UPLOAD_URL` 必须带 `www`（`picgo.net` 会 301 把 POST 转 GET，图床回 400 Empty upload source）；同图重复上传会被判 duplicated，`cover_service` 用 `data/_uploaded_urls.json` 按 sha1 缓存 URL。**缓存只作传输复用，不是内容身份证明**。
   - ★ 严格封面身份：必须串起「目标 tid → 发布血缘 → 实际 B torrent infohash → B 文件清单 → 精确实体目录 → 本地封面 SHA256 → 详情页首图远端 SHA256」。只认首图；URL 相同、HTTP 200、尺寸相同、缓存命中、`name+size` 或专辑名相似均不算证据。身份不唯一、无本地封面、B 种不明、`music5` 一律禁止自动修。
   - ★ 提交前字节闸门：图床返回 URL 后必须重新 GET，远端 SHA256 与本地预期完全一致才允许写缓存或提交站点。PicGo/Chevereto 严格路径 `allow_reencode=False`，禁止 duplicate 无 URL 时重编码原图；缓存 URL 字节不符必须绕过缓存重传原图，只有匹配原图的 URL 才能重新写缓存。严格 Lsky 路径同时 `allow_shrink=False / allow_reencode=False`，超限或判重后拿不到原字节 URL 就 blocked，禁止用派生图冒充原图。写后回读详情页首图、复核 SHA256 并重新归档。
   - ★ 逐站差异：`uhddolby` 字段名**不带** `[4]`；`crabpt` 封面必须 Lsky（`img.crabpt.vip`）且 `processing_sel`=地区、`source_sel`=媒介；`hdkylin` 相反且标题禁非 Latin-1；`tangpt`/`lemonhd` 必须走代理。
   - 批量写站点必须显式 `--apply --yes`，默认 dry-run；批量封面写入前所有目标都必须通过严格身份、候选 URL 字节校验和 dry-run，任一失败不得悄悄降级。`music5` 一律不扫描。

14. **「多处做种」告警必须先分诊再动手（2026-11-21 固化）**：
   - 站点回包 `You cannot seed the same torrent in the same location from more than 1 client.` 有两种完全不同的成因，**严禁一律当"集群内重复"处理**：
     - `live_duplicate`：同一 info-hash 在 ≥2 个实例都 active ⇒ 我们自己的编排问题，必须收敛；
     - `stale_seat`：集群内只有一份，站点仍留我们**上一轮会话（peer_id）的旧 peer 行** ⇒ qB 每次重启都换 peer_id，站点在旧行到期前一直拒收新会话。实测 2026-11-21 3220 的 457 个 musopia 任务中 456 个属此类，全集群双活为 0。
   - 入口：`python musichub/tools/cluster_dedupe.py`（只读扫描）/ `--seats N`（站点告警分诊）/ `--apply`（仅收敛双活，deleteFiles 恒 false）；核模块 `musichub/storage/cluster_guard.py`。
   - **陈旧占位本地清不掉**：`stop→start→reannounce` 不会刷新站点旧行（已实测），只能等站点清理 TTL 或请管理组清理 ⇒ 不得删任务、不得重加种子、不得动文件，只记录跟踪。
   - **重启纪律属 qB 侧铁律**：qB 每次启动都换 peer_id，音乐池容器每重启一次，活跃实例的 musopia/hdkylin 种子就会被判"多处做种"直到旧行到期。规范见 `skills/qb-cluster/SKILL.md`「重启纪律」与顶层 `AGENTS.md` 第 2 节第 13 条；本项目的执行要求：换镜像/改名/改 compose 合批，避开发种黄金窗口，重启前确认无在途发种，重启后必跑一次 `--seats` 留证。
   - daemon 已内置每 6h 一次只读巡检（`ClusterDeduper.scan()`，只报警不自动删）；破坏性收敛一律显式 `--apply`。
   - 配对器 `select_target_pool()` 的查重已覆盖全池（含 `music3`）：`music3` 已有同 hash 必须复用该池，禁止换池再注一份（旧实现只查 7 个承接池，属真实盲区）。

15. **简介内容两条硬规则（2026-11-21 固化）**：
   - **无封面必须写明**：`descr` 里一张 `[img]` 都没有时，必须带一行 `封面：源站和本地均无封面`（`descr_core.NO_COVER_NOTE`，由 `build_descr` 自动插入，位置在转载引用之后、正文之前）。乌托邦规则要求音乐种带专辑封面且「封面字段留空则取简介首图」，空着不写等于让审核看不出"已尽力"。有独立封面字段的站点（OpenCD，`has_cover_field=True`）不写这行。
   - **转载来源不许留半截话**：`build_source_link()` 只在 `origin_tid` 是**纯数字**时才拼 `details.php?id=`；infohash（40 位十六进制）不是详情页 id，拼出来是死链，且旧实现会留下 `[quote]转自OpenCD: [/quote]` 这种空引用框（实测乌托邦 28125/28126/28132 等 8 条）。
   - 存量修整入口：`python musichub/tools/edit_onsite.py --scan --site <站> --days N --limit M --fix-descr [--apply --yes]`（默认 dry-run；只删空转载引用块 + 补无封面说明，其余内容原样）。`--scan` 已覆盖 `publish_lineage` / `publish_attempt_log` / **`site_publish_graph`** 三张表，并按时间统一排序（否则 graph 里的新行会被挤掉）。
   - **来源死链修复（2026-11-21 新增）**：`--fix-source-link` 把 `[quote]转自X: <40 位 hash>[/quote]`（含 `details.php?id=<hash>` 假链接）改成真链接；tid 解析顺序 `graph.<源站>_tid → 同行 origin 数字 tid → **Vertex 只读库 hash→OpenCD tid** → source_tasks → publish_lineage`，全查不到时保留清洗后的文案（去 hash、去「[ 免费 ] 剩余时间」尾巴），实在没有可用文案才删整条。单条诊断用 `--site X --tid N --show-source`。DB 缺 `opencd_tid` 先用 `--backfill-opencd-tid [--apply]` 补（Vertex 库映射是主力来源）。实测 2026-11-21 修复 6 站 260 页（crabpt/pterclub/xingwan/hdkylin/muxuege/tangpt），站内每页读写间隔 0.6~1.2s。

16. **站点 tid 分列记账（`site_publish_graph`，2026-11-21 新增）**：
   - `origin_torrent_id` 历史上混存「数字 tid / 40 位 infohash / 专辑名」三种形态（实测 2668 行里 2587 数字、2 hash、79 专辑名）。**该字段定义保持不变、不做清洗**（外部账本与迁移脚本依赖它），改为每个站点各开一列：`opencd_tid` / `musopia_tid` / `crabpt_tid` / `pterclub_tid` / `xingwan_tid` / `hdkylin_tid` / `tangpt_tid` / `muxuege_tid` / `lemonhd_tid` / `uhddolby_tid` / `hdtime_tid`（清单唯一事实源：`musichub/routing/graph.py` 的 `SITE_TID_COLUMNS`，新增站点必须在此登记列名）。
   - 语义：一行仍是「某内容 → 某目标站」，行内 `<origin_site>_tid` 记源站 tid、`<target_site>_tid` 记目标站 tid；`record_publish()` 写入后按 `content_fingerprint` 向同内容其他行同步，任一行都能回答「这个内容在各站的 tid」。读入口 `get_site_tids(fingerprint)`，补写入口 `set_site_tid(fingerprint, site, tid)`。
   - **只认纯数字**：infohash / 专辑名 / `EXISTING` 一律写空串，禁止把 hash 塞进 tid 列（否则又制造一个形态混合字段）。
   - 存量回填：`python musichub/tools/backfill_site_tids.py`（默认只读体检）/ `--apply`（建列 + 回填，只补空值、幂等）。回填四路来源：本行 target tid → 本行 origin tid → 同指纹兄弟行 → `publish_lineage` 台账。老库首次被 `RoutingGraphManager` 打开时自动完成同一套迁移。

17. **源种（A 种）摄取链：搬过去必须真做种（2026-11-21 修复）**：
   - **旧链路缺口**：`daemon.py` 调 `handoff_task()` 从不传 `torrent_bytes` ⇒ 注入步骤整段跳过，数据复制到机械盘后**没有任何任务接管**，随后又用 `delete_files=True` 删掉 3016 任务与 SSD 文件，只剩「有文件、无任务」的孤儿（实测 38 条源种 **0/38** 在集群中做种）。
   - **现口径**：`handoff_task()` 自己解析种子字节（入参 → 3016 `/torrents/export` → `data/torrents/*.torrent`）。归档检索必须**解析种子算 infohash 再比对**（infohash 是 info 字典 SHA1，不会以原始 20 字节出现在 .torrent 里，旧「字节包含」匹配永远命不中）；**拿不到字节直接中止，禁止先搬文件**。
   - **搬运是 mv 不是 copy**（`move_payload_to_hdd()`；`copy_stream_to_hdd()` 保留为兼容别名）：目标同名目录已存在且 size 一致视为幂等重入，size 不一致拒绝覆盖。
   - **必须真做种**：注入后目标池任务 `paused=False + skip_checking=True`，并在 `verify_timeout`（默认 30s）内回读 `progress≈1`；未达标则撤回池任务 + 文件搬回 SSD + `resume` 3016 任务，下一轮重试，绝不留在池里当幽灵。
   - ★ **OpenCD 禁忌**：`opencd` / `open.cd` ⇒ 恒落 `music3(3260)`；除 `route_target()` 锁定外，`handoff_task()` 还有 fail-closed 护栏（目标端口 ≠ 3260 直接拒绝搬移，一个字节都不动）；`music3` 同时不承接 B 种负载。
   - **摄取台账 `ingest_log`**（只增不改，`status` = `SEEDING` / `VERIFYING` / `FAILED` / `ROLLED_BACK`）：记 `target_instance` / `target_port` / `target_save_path` / `host_path` / `size_bytes` / `progress`。查询 `graph_mgr.get_latest_ingest(hash)` / `list_ingest_logs(status)`，**「源种在哪做种」以最新一条 `SEEDING` 为准，不再靠 ls 反查**。
   - **qB 5.x 兼容**：`/torrents/add` 的暂停参数在 5.x 改名 `stopped`，`QBClient.add_torrent()` 同时下发 `paused` 与 `stopped`，否则 5.2.4 会按全局 `AddTorrentStopped=true` 静默落成暂停。
   - **单轮节流**：`check_download_buffer_readiness(max_batch=5)`，按 `completion_on` 升序先完成先处理，避免长等待拖垮主循环。
   - **存量回填工具**：`python musichub/tools/rebuild_source_seeds.py`（默认**只读**，`--apply` 才注入）。逐条核「全集群是否已有该 hash → 种子字节（归档 / 实例 export / **源站 download.php 兜底并校验 infohash**）→ 按种子清单逐文件校验实体数据落点 → OpenCD 只在 music3」，`--apply` 时为 `paused=False + skip_checking=True` 并回读 100% 后写 `ingest_log`。2026-11-21 实测存量 38 条 → 37 条直接回填（music3/3260，tracker working），第 38 条（musopia #28150）因 `Lost&Found.cue` 期望 1176B/实际 1178B 判 `NO_DATA`，用 qB 选择性下载只补该单文件后覆盖 5 池同 inode 文件、回填 `music_pool_2` 并 recheck 100%，**38/38 全部在册**。

18. **机械盘 14+10 天安全退役与空间回收机制（2026-11-22 固化）**：
   - **痛点根因**：56TB 机械盘卷 `/volume1` 使用率已达 99%（可用约 864GB）；多站发种存在发布时差，若单按源站时间粗暴删种会导致新发站点被「抽水断种」，剩余排队站点失去物理数据。
   - **三维五重门禁**：
     1. **门禁 1（矩阵扩散充分度）**：专辑在所有适格目标站均已发布完毕，没有排队未发的站点，充分满足「尽可能多发」；
     2. **门禁 2（各站点做种满 10 天）**：$\max(\text{published\_at}) \le \text{now} - 10 \text{ 天}$，最晚发布的站点也必须做种满 10 天，绝不新站断种；
     3. **门禁 3（源种全局满 14 天）**：$\min(\text{published\_at}) \le \text{now} - 14 \text{ 天}$ 且 100% 达成源站（如 OpenCD 3260）36h/72h H&R；
     4. **门禁 4（背压按需驱动）**：绿色水位（可用 $\ge 1.2\text{TB}$）纯只读保种赚积分；可用 $< 1.0\text{TB}$ 时激活 FIFO 淘汰队列；
     5. **门禁 5（两阶段安全 GC）**：Phase 1 跨 8 池 + 3010 移除任务（`delete_files=False` 铁律）；Phase 2 `SafeGarbageCollector` 跨池协同解除 hardlink，严守 `music5` 禁删红线与系统黑名单。
   - **历史时间对齐**：工具 `musichub/tools/backfill_history_dates.py` 从 `data.db` 成功回填 2585 条历史时间，纠正 `2026-11-19` 截断失真。
   - **全景运维 CLI**：`musichub/tools/retire_matrix.py`（`--status`, `--candidates`, `--dry-run`, `--apply`）。详见 [专题 04：元数据保真、母种溯源与机械盘退役治理](../docs/nas_cluster/postmortems/04_元数据保真_母种溯源与机械盘退役专题.md)。

19. **延迟回加队列阻塞与并发 DB 锁排查（2026-11-22 复盘）**：
    - **事故表象**：外部独立发种进程（如 stock_batch_runner.py）脱离守护进程直接并发高频写 SQLite，主进程交棒线程大事务回写快照，触发底层锁竞争抛出 database disk image is malformed；主守护进程持久连接受损后无法感知新任务，DelayedReaddThread 空转休眠，导致 11 站共 80 个新种被滞留在 delayed_readd_tasks 队列超过 1 小时未做种。
    - **恢复结果**：容器重启后连接自愈，积压队列在 13:27~13:45 期间以极快速度逐一校验并回加做种完毕（含 UBits 378627 于 13:35:26 恢复）。
    - **加固建议**：批处理工具严禁作为独立进程并发写库；持久连接需增加 _execute_with_retry 重连自愈机制；队列空转应增加告警可观测性。详见 [专题 03：发布流水线、交棒与延迟回加治理](../docs/nas_cluster/postmortems/03_发布流水线_交棒与延迟回加治理专题.md)。
20. **站点主标题回归原种风格与多站命名分化（2026-11-22 固化）**：
    - **痛点**：旧逻辑对所有站点强推猫站公式 `normalize_music_title()`，存量本地发种因目录名缺年份常被正文版权年号误导（如将 2024 年误算为 2020 年），且丢失各站多样性。
    - **架构设计**：
      1. 源头穿透：`resolve_stock_origin()` 返回母种原始标题 `origin_name`，`CanonicalMusicItem` 增加 `origin_title` 透传；
      2. 底线清洗：实现 `clean_origin_music_title()`，仅剥离 NexusPHP 禁止的“分轨/整轨”与私人压制者后缀（如 `- Yuekui`），100% 忠实保留原种格式、真实年份、版本（`Deluxe`, `OST`, `SACD`）与官方白名单发布组；
      3. 多站分化：通用 NexusPHP 站点（不可躺、乌托邦、蟹黄堡、杏坛、UBits 等）回归原种风格；猫站（PTerClub）重写 `format_title()` 保持严格 Wiki 标准；麒麟（HDKylin）保持纯拉丁化标准。详见 `skills/musichub/SKILL.md` 与 [专题 04：元数据保真、母种溯源与机械盘退役治理](../docs/nas_cluster/postmortems/04_元数据保真_母种溯源与机械盘退役专题.md)。
21. **3260 大量 stoppedDL 种子有序并发下载与五档阶梯限速调度（2026-11-23 固化）**：
    - **痛点**：批量向 3260 (music3) 灌入大量默认暂停种子（如 532 个 stoppedDL，214GB）时，若全量 resume 会压爆机械盘 IO、引发高寻道超时与网络风暴。
    - **工具**：`musichub/tools/ordered_downloader.py`（对标 3010 SpeedGovernor，双端兼容，零外部依赖）。
    - **铁律与策略**：
      1. **20 并发稳态吞吐**：放开至 20 个活跃并发槽位有序推进，单种连接数放开至 25，确保吃满下行带宽；
      2. **五档阶梯限速（契合用户参考模型）**：
         - 档位 1（0% ~ 10%）：初始预热低速 `1.0 MiB/s`（平稳建连，防握手突发）；
         - 档位 2（10% ~ 20%）：爬坡加速低速 `2.5 MiB/s`（前 20% 低速过渡）；
         - 档位 3（20% ~ 70%）：主力冲刺极速 `5.0 MiB/s`（单种最高 5MB/s，拉满带宽）；
         - 档位 4（70% ~ 90%）：高位稳态高速 `4.0 MiB/s`（20%~90% 高速区间后半程）；
         - 档位 5（90% ~ 100%）：尾盘减速防抖 `1.5 MiB/s`（最后 10% 减速收敛，防磁盘寻道与写缓冲区阻塞）；
      3. **小包优先（`size_asc` 默认）**：按专辑大小从小到大排队，几十秒下一个，极速消减待办数；
      4. **僵死防锁死**：卡在 stalledDL 且超过 5 分钟 0 速度的任务自动 stop 并暂缓，防止死种占死槽位；
      5. **严格保护做种**：对 progress >= 1.0 的 2600+ 个在保母种绝对不动。
    - **常用命令**：
      ```powershell
      python musichub/tools/ordered_downloader.py --status      # 状态体检
      python musichub/tools/ordered_downloader.py --dry-run     # 计划预览
      python musichub/tools/ordered_downloader.py --concurrency 20 # 20并发五档调速运行
      python musichub/tools/ordered_downloader.py --stop-all    # 紧急制动
      ```

22. **存量矩阵平铺补种调度（2026-11-24 升级）**：
    - **目标矩阵**：确立 11 个标准外站目标（`SoulVoice`、`UBits`、`LemonHD`、`HDTime`、`MuXueGe`、`TangPT`、`PTerClub`、`HDKylin`、`Xingwan`、`Musopia`、`CrabPT`），剔除非纯音乐站 `UHDDolby`。
    - **单轮节拍**：解除旧实现 `max_per_round=3` 的全局早退限制，调整为「每轮 11 站，各发 1 个（每半小时最多 11 个）」。单轮内遍历全部 11 个站点，每个站点各自寻找 1 个最佳候选专辑发布，站点间保持 15s 避峰平滑。
    - **启动自愈**：内置 `_init_last_stock_pump_time()`，启动时自动从 `publish_attempt_log` 流水账本恢复最近发种时间戳，防止容器重启立刻早熟连发，严格维持每 30 分钟一轮的平稳发种节拍。

23. **南洋PT矩阵接入与SSD站点规则退役（2026-11-25 调整）**：
    - **南洋PT (NanyangPT)**：`type="407"`，正式纳入发布矩阵。强依赖 `music_time/music_type/music_author/music_name/music_form/music_qual` 动态细分字段与前端 `[YYYY][类型][歌手][专辑][FLAC][无损]` 格式校验；后端老旧 `bdecode` 强校验上传种子顶层 `announce` 键，发种时由适配器向临时种子打桩注入 `announce.php`。
    - **SSD (SpringSunday) 移除发布矩阵**：因 SSD 站点规则严格限制「非官种音乐组不允许发布音乐种子」，为杜绝发种违规与候选风控死锁，SSD 适配器仅作为历史代码留存，**已从 `StockMatrixPump`、`StockBatchRunner`、`LifecycleRetirement` 及后台发布守护进程 `MusicHubDaemon` 中彻底移除**。
    - **单专发种与候选提交统一入口**：固化 `python musichub/tools/publish_item.py`。

---

## 3. 本地自检与验证

动代码前后执行语法检查与单元测试：
```powershell
# 1. 语法检查
python -m py_compile musichub/daemon.py

# 2. 核心单元测试
$env:PYTHONPATH="."
python musichub/tests/test_core.py
python musichub/tests/test_cluster_guard.py

# 3. 单实例/站点告警巡检（只读）
python musichub/tools/cluster_dedupe.py --seats 250

# 4. 8大音乐池与全集群做种状态探针（只读）
python musichub/tools/query_pools_status.py
python musichub/tools/cluster_download_probe.py

# 5. 跨池做种缺失自动硬链接补齐与自愈（需存储权限）
python musichub/tools/auto_heal_pools.py --dry-run

```

---

## 4. 专项复盘文档索引

- [专题 01：八大音乐池平权重构与阶梯调速治理](../docs/nas_cluster/postmortems/01_八大音乐池平权重构与阶梯调速治理专题.md)：8 个核心音乐 QB 实例规范重命名、动态平权、多站专辑做种归一（彻底消除跨池 Hardlink 空间陷阱）、五档精细阶梯调速（1M/2.5M/5M/4M/1.5M）、20 并发槽位管理与实战踩坑全复盘。
- [专题 02：Vertex 抓取过滤、防环联动与 3016 缓冲队列治理](../docs/nas_cluster/postmortems/02_Vertex与3016下载防环及队列治理专题.md)：方案 C 共享账本对齐与防环联动、Vertex 源码级单曲过滤（音频数=1且无cue）、3016 缓冲队列死锁疏通与 `DownloadQueueJanitor` 守护机制、UBits/LemonHD/UHDDolby 等新站接入与防环适配。
- [专题 03：发布流水线、交棒器效率与延迟回加治理](../docs/nas_cluster/postmortems/03_发布流水线_交棒与延迟回加治理专题.md)：3010 发布期 450s 延迟回加机制演进与守护线程常驻化（解决 daemon 失效、静默 FAILED 复活与 SQLite WAL 并发锁冲突）、交棒器（Handoff）效率重构（batch 30、双判据防队头阻塞）。
- [专题 04：元数据保真、母种溯源与机械盘退役治理](../docs/nas_cluster/postmortems/04_元数据保真_母种溯源与机械盘退役专题.md)：已发布详情页数据库（`published_details.db`）与 B 种断种自愈、严格 SHA256 封面身份链（防图床重编码）、真源头穿透（`resolve_stock_origin` 杜绝套娃引用）、存量单曲拒发多层护栏、机械盘 14+10 天安全退役三维五重门禁模型（`retire_matrix.py`）。
- [专题 05：qB 集群底层运维、多处做种分诊与存储 I/O 调优](../docs/nas_cluster/postmortems/05_qB集群底层运维_多处做种分诊与存储IO调优专题.md)：幽灵做种（Ghost Seeding）机制与 save_path 层级铁律、qB 重启 peer_id 换新导致站点“多处做种”陈旧占位（`stale_seat`）分诊与四项重启纪律、硬链接 GC 清理真相与 `music5` 禁删红线、机械盘扫盘高 I/O 调优与 NAS 孤儿进程审计。
- [专题 06：辅种进库校验防偷跑、分流即时唤醒与 reseed 常驻守护](../docs/nas_cluster/postmortems/06_辅种进库校验防偷跑_分流即时唤醒与reseed常驻守护专题.md)：保种池物理限速熔断与平滑单并发排队校验机制。
- [专题 07：跨实例重复做种全集群治理与存储孤儿分诊清理](../docs/nas_cluster/postmortems/07_跨实例重复做种全集群治理与存储孤儿分诊清理专题.md)：全集群同 info-hash 单实例铁律与跨实例查重收敛。
- [专题 08：全集群单种限速升档 4MB 与 qbit_manage 规则治理](../docs/nas_cluster/postmortems/08_全集群单种限速升档4MB与qbit_manage规则治理专题.md)：全集群单种上传限速统一 4MB 基线与 qbit_manage 错峰轮询。
- [专题 09：3016限速治理、reseed7清理、影子对偷跑隔离与分流路径归位](../docs/nas_cluster/postmortems/09_3016限速治理_reseed7清理_影子对偷跑隔离与分流路径归位专题.md)：影子配对任务绝对静默待命与分流路径动态绑定。
- [专题 10：母种穿透、来源致谢规范、种子注释注入与详情页修缮](../docs/nas_cluster/postmortems/10_母种穿透_来源致谢规范_种子注释注入与详情页修缮专题.md)：母种真实溯源穿透、外链致谢与 Bencode 详情页直链安全注入。
- [专题 11：发种真伪解耦、数字TID保护与存量流转治理](../docs/nas_cluster/postmortems/11_发种真伪解耦_数字TID保护与存量流转治理专题.md)：发种真伪解耦、纯数字 TID 强校验与单调晋升保护。
- [专题 12：全集群无Tracker哑种排查、时序竞争根因与批量修复](../docs/nas_cluster/postmortems/12_全集群无Tracker哑种排查_时序竞争根因与批量修复专题.md)：跨实例注入 Tracker 时序竞争根因、强制回读校验与全集群哑种审计。
- [专题 13：全矩阵13站点标签解析、常态化抓取与数据库落盘治理](../docs/nas_cluster/postmortems/13_全矩阵13站点标签解析_常态化抓取与数据库落盘治理专题.md)：全矩阵13站点标签解析、表单智能推断、全天常态化抓取与双库标签原子持久化。
- [专题 17：源种 TID 全量回填、做种健康核查与 CookieCloud 会话失效诊断](../docs/nas_cluster/postmortems/17_源种TID全量回填与做种健康核查专题.md)：87 行无 TID 残行成因与 85 行 hash 定论回填、`status=2 working` 判据证伪"删种"假设、CookieCloud cookie 未过期但服务端会话失效的假阴性诊断，配套 `probe_source_tasks.py` / `probe_torrent_meta.py` / `probe_site_session.py` 三个只读探针。
- [专题 18：南洋PT与SSD接入、候选机制与动态白名单治理专题](../docs/nas_cluster/postmortems/18_南洋PT与SSD接入_候选机制与动态白名单治理专题.md)：南洋PT与SSD发种/候选流水线接入、SSD新手10候选自动探测自适应、纯英文 MediaInfo 校验、站内附件图床转存闭环与单专发种工具标准化归档。
- [历史原始单篇复盘归档](../docs/nas_cluster/postmortems/raw_history/)：保留所有历史单篇记录供备查溯源。
