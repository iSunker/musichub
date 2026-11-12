# 方案：接入麒麟（HDKylin）—— 每日自动转音乐种

> 状态：**已上线 · 试发验证通过 · 定时已开** · 2026-11-12
> 依据：`opencdhtml/HDKylin - 麒麟 __ 发布 - Powered by NexusPHP.html` + `opencdhtml/上传规则 _ HDKylin.html`（**离线 HTML，实测**，非推测）
> 本方案所有字段名 / 取值，出处都在第 2 节，可直接复核。

> ✅ 部署与试发记录（2026-11-12）：
> · 镜像 `c235762-dirty` / `hdkylin-20261112` 推送成功，digest 两端一致
>   （`sha256:fcdc27e07670…`），容器内 `hdkylin_site.py` / `descr.py` MD5 与本地一致。
> · 期间修复 `tools/image-smoke-test.sh` 开关清单漂移（漏 `ENABLE_PUBLISH_HDKYLIN` +
>   两个星湾遗留开关 —— 正是此前构建没能推上去的原因）。
> · dry-run（容器内）：待发布 1326 条 / 预检拦 203 / 有效 1123；表单逐字段核对无误。
> · **单条试发成功**：`9e107bcd9ba5` Shaggy - Intoxication → 麒麟 tid=473337；
>   详情页回读逐字段一致：H1=`Shaggy - Intoxication 2007 FLAC`、副标题一致、
>   类型=HQ Audio/音乐、年代=Earlier/更早、媒介=CD、音频编码=FLAC、
>   地区=Other/其他、视频编码/分辨率/制作组=Other×3。
> · `ENABLE_PUBLISH_HDKYLIN` 已开（试发+回读验证通过后，按批准的验证节奏）。

---

## 0. 一句话

麒麟（`hdkyl.in`）是 **NexusPHP** 站，音乐分类 = **`408`（HQ Audio/音乐）**，发布表单 `takeupload.php`，
与现有乌托邦/蟹黄堡/猫站/星湾**同源同构**，可照「站点适配器」模式接入第五站。

**但它的字段语义与现有四站都不完全一致**（尤其 `processing_sel[4]`=**年代**、`source_sel[4]`=**地区**），
必须**新写一个 `hdkylin_site.py`**，不能改数字复用。

---

## 1. 站点基本信息（实测）

| 项 | 值 | 出处 |
|---|---|---|
| 站点域名 | `https://www.hdkyl.in` | 页内绝对链接 |
| 表单引擎 | NexusPHP | 页脚 `Powered by NexusPHP` |
| 发布页 | `GET /upload.php` | 导航 |
| 发布 action | `takeupload.php`（`method=post`, `enctype=multipart/form-data`, `name=upload`, `id=compose`） | `<form id="compose">` |
| Tracker | `https://www.hdkyl.in/announce.php` | 表单顶部 `Tracker服务器的URL是` |
| 音乐分类 | **`type=408`（HQ Audio/音乐）** | `browsecat` 下拉 |
| 下载种子 | `download.php?id=<id>` | NexusPHP 惯例 |
| 详情页 | `details.php?id=<id>` | NexusPHP 惯例 |

---

## 2. ★ 发布页完整字段表（BeautifulSoup 逐元素实测）

### 2.1 表单本体

| 项 | 值 |
|---|---|
| 分类 | `type`（`id=browsecat`, `data-mode='4'`），**音乐 = 408** |
| 种子文件 | `file`（`type=file`，**必填**） |
| 标题 | `name`（text，页面提示「若不填将使用种子文件名。**要求规范填写**」） |
| 副标题 | `small_descr`（text） |
| IMDb链接 | `url`（text，音乐留空） |
| PT-Gen | `pt_gen`（text，音乐留空）★ 现有站**没有**这个字段 |
| NFO文件 | `nfo`（file，留空） |
| 简介 | `descr`（textarea，**必填**，BBCode） |
| MediaInfo | `technical_info`（textarea） |

### 2.2 `mode_4` 组字段（音乐走这一组，字段名全带 `[4]`）

| 字段 | 页面标签 | 取值（完整） |
|---|---|---|
| `processing_sel[4]` | **年代** ★ | `0`请选择 `12`2026 `11`2025 `10`2024 `1`2023 `2`2022 `3`2021 `4`2020 `5`2019 `6`2018 `7`2017 `8`2016 `9`Earlier/更早 |
| `medium_sel[4]` | **媒介** | `0` `24`UHD Blu-ray `25`Blu-ray(原盘) `27`DVD(原盘) `28`HDTV `29`Encode `30`REMUX `31`WEB-DL `32`Track `33`CD `34`Other |
| `codec_sel[4]` | 视频编码 | `0` `6`H.265/HEVC `1`H.264/AVC `2`VC-1 `3`Xvid `4`MPEG-2/MPEG-4 `7`AV1 `5`Other |
| `audiocodec_sel[4]` | 音频编码 | `0` `8`DTS-HD MA `16`DTS:X `19`DTS-HD HR `3`DTS `9`TrueHD `15`TrueHD Atmos `11`DD/AC3 `17`DDP/E-AC3 `6`AAC `10`LPCM `12`APE `13`WAV `14`M4A `18`MPEG `1`FLAC `4`MP3 `20`Opus `7`Other |
| `standard_sel[4]` | 分辨率 | `0` `7`8K `6`4K `10`2K `1`1080p `3`720p `8`480p `9`Other |
| `source_sel[4]` | **地区** ★ | `0` `15`CN/中国 `16`HK/香港 `17`TW/台湾 `18`US/美国 `28`EU/欧洲 `19`JPN/日本 `20`Kr/韩国 `21`GB/英国 `22`FR/法国 `23`DE/德国 `25`IN/印度 `30`RU/俄罗斯 `31`CA/加拿大 `32`BR/巴西 `33`SE/瑞典 `34`DK/丹麦 `35`TH/泰国 `14`Other/其他 |
| `team_sel[4]` | 制作组 | `0` `6`HDK `7`HDKWeb `8`HDKGame `12`Kylin `14`RedLeaves `10`CatEDU `9`GodDramas `13`StarfallWeb `5`Other |

### 2.3 标签 `tags[4][]`（完整）

```
3 官种   15 驻站   2 首发   1 禁转   21 特效字幕   4 DIY   5 国语   18 英字
6 中字   7 HDR    8 Dolby Vision   9 Blu-ray   10 LIVE现场   11 4K   16 完结   19 分集
```

★ 音乐**真正可用**的只有：`5 国语` `6 中字` `9 Blu-ray` `10 LIVE现场` `16 完结` `19 分集`。
★ **没有**「粤语」「英语」「日语」等语言标签（有 `18 英字` = 英文字幕，是影视概念）。
★ 禁转(1)/首发(2)/DIY(4)/官种(3)/驻站(15) —— 我们是**转载方**，**一律不勾**（同 `descr.TAG_NEVER_FORWARD` 纪律）。

### 2.4 匿名

`uplver`（checkbox, `value=yes`）「不要在发布者项目中显示我的用户名」。

---

## 3. ★★★ 与现有四站的关键差异（最大的坑）

| 语义 | 麒麟 | 蟹黄堡 | 星湾 | 乌托邦 |
|---|---|---|---|---|
| **年代** | **`processing_sel[4]`** | ✘ 无 | ✘ 无 | `custom_fields` |
| **地区** | **`source_sel[4]`** | `processing_sel[4]` | tags 里 | tags 里 |
| **媒介** | **`medium_sel[4]`** | `source_sel[4]` | `medium_sel[4]` | `medium_sel` |
| 媒介·CD 值 | **`33`** | `8`(但实测填 Other) | `19` | `8` |
| 媒介·Other 值 | **`34`** | `1` | `21` | `12` |
| 音频·FLAC 值 | **`1`** | `10` | （无，仅无损标记） | `1` |
| 音频·WAV 值 | **`13`** | `11` | — | `2` |

★ **结论**：麒麟的 `processing_sel[4]` / `source_sel[4]` 与蟹黄堡**同名但语义互换**——
蟹黄堡 `processing`=地区、`source`=媒介；麒麟 `processing`=**年代**、`source`=**地区**。
★ 照抄蟹黄堡的 `map_processing` / `map_source` 会**静默填错字段**（页面能提交、字段全错位），
这是本接入**最需要单独写一份映射表**的地方。

---

## 3.5 ★ 站规实测（`上传规则 _ HDKylin.html`，2026-11-12 补）

> 出处：`opencdhtml/上传规则 _ HDKylin.html`（wiki.hdkyl.in 站点规则，管理员 2025-08-08 更新）

### ★ 图床：规则**不强制**使用官方图床

全文**没有**「图床」「必须使用官方图床」等任何字样。唯一的图片要求是：

> 音乐：**必须包含专辑封面和曲目列表（如果存在的话）**；NFO图请写入 NFO 文件，而不是粘贴到简介里。

⇒ **结论：封面放哪不限**，只要简介里有封面 + 曲目即可。用户给的官方图床
`https://img.hdkylin.com/`（Chevereto 风格，URL 形如 `/images/2025/02/14/<hash>.png`，
与现有 PicGo `picgo.net` 同族，可复用 `assets.py` 的 Chevereto 上传逻辑、只换 URL + API Key）
是**可选**方案，不是强制。★ 页面里那行 picgo SDK 被注释掉、指向 `img.hdkyl.in/upload`（旧域名），
与用户给的 `img.hdkylin.com` 不是一回事，接入时以 `img.hdkylin.com` 为准。

### ★★ 标题格式（音轨）—— 与蟹黄堡**同款**，已定案

```
音轨：[中文艺术家名 - 中文专辑名] 艺术家名 - 专辑名 [年份] [版本] [发布说明] 音频编码[-发布组名称]
例：恩雅 - 冬季降临 Enya - And Winter Came 2008 FLAC
```

⇒ 与蟹黄堡站规一致：**中文名在前、原名在后**，`年份-音频编码` 无空格。
★ `descr.normalize_music_title` 是猫站口径，**需核对是否产出这个形状**（§6-3 从"待确认"降级为"需对齐"）。

### ★★ 转载铁律（对方案是**硬约束**，比图床重要）

> 转载其他 PT 站点种子**禁止删除原站后缀、修改文件名、修改文件夹结构**等操作。

⇒ 我们转载的是 OpenCD 种子 ⇒ **标题后缀保留 `- OpenCD`**（诚实标注来源，与乌托邦先例一致，
且 `opencd_map.py` 有"不冒充制作组"的纪律）；文件名/文件夹结构**原样**（现有 pipeline 已如此）。

### 体积闸门（`PENDING_PUBLISH` 需补一条，同蟹黄堡）

> 总体积小于 **100MB** 的资源不允许；例外：**单曲专辑**、2.0 声道以上国语/粤语音轨。

⇒ 发布前需查 `size`：<100MB 且非单曲专辑 ⇒ 拦。

### 无损音轨 dupe 规则

> 每个无损音轨资源原则上只保留一个版本，其余不同格式的版本将被视为重复。**分轨 FLAC 优先级最高**。

⇒ 发前查重（`DEDUP_BEFORE_PUBLISH` 已有）**必须开**；且「同专辑不同格式」会被判重复，需注意。

### 其它对方案的约束

| 规则 | 影响 |
|---|---|
| 无正确 cue 表单的**多轨**音频不允许 | 分轨（无 cue）的普通专辑不受影响；但**多轨整轨必须有 cue** |
| 有损 MP3/WMA 不允许（未达 5.1 声道） | 只转无损（FLAC/APE/WAV） |
| 音乐简介必须含封面 + 曲目 | `descr.build_descr` 已含 `[img]` + `[quote]曲目` |
| 打包：5 张以上方可打包；两年内发售专辑可单发 | 我们**单发**，正好合规 |
| ★ 不得改变管理员对种子信息的修改 | 与 `update_metadata` 编辑阶段冲突 ⇒ **默认不编辑**（同蟹黄堡 B.3） |

---

## 4. 代码接入点清单（照「星湾」这条最近的先例抄）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `config.py` | 新增 `HDKYLIN_URL` / `HDKYLIN_COOKIE` / `HDKYLIN_MUSIC_CATEGORY=408` / `HDKYLIN_PUBLISH_CATEGORY`（回加 qB 分类或标签）/ `HDKYLIN_MAX_PER_CYCLE` 等 |
| 2 | `db.py` | 加 `SITE_HDKYLIN = "hdkylin"`；直发队列 `HDKYLIN_DIRECT_WHERE`（复用星湾口径 `transfer_rule='ALLOW' AND status != 'FAILED'`）+ `pending_hdkylin_direct()` |
| 3 | **新建 `src/hdkylin_site.py`** | 适配层：`build_fields()` + `HdkylinClient`（check_login / fetch_upload_form / upload_torrent / download_torrent / find_existing_by_name / fetch_edit_form / edit_torrent / fetch_details）+ `selftest()` |
| 4 | `publish.py` | `--site` choices 加 `hdkylin`；`main()` 加 `if args.site == "hdkylin": return run_hdkylin(args)`；新增 `run_hdkylin(args)`（照 `run_xingwan`）+ `_hdkylin_cover()` + `readd_to_qb_hdkylin()` |
| 5 | `daemon.py` | 加 `publish_hdkylin` 阶段（`ENABLE_PUBLISH_HDKYLIN`，默认 false）；`ENV_SNAPSHOT_KEYS` 加麒麟键；`build_patrol_clients` 的 sites/cats/tags 加麒麟（若做巡检） |
| 6 | `.env.example` | 星湾块后加麒麟块（URL/COOKIE/分类/开关/周期） |
| 7 | （可选）`update_metadata.py` | 加 `--site hdkylin`；★ 建议默认**不做编辑**（站规「不得改变管理员修改」，同蟹黄堡 B.3） |

★ **回加机制无需麒麟 passkey**：回加 qB 用的是 `client.download_torrent(tid)`（麒麟服务端重写
announce 后的 .torrent），不是本地 `tasks.torrent_file`（那份带乌托邦 announce）。
⇒ 用户给的 `f7a8bc2ee876a021242762c125f20300` 在**发布/回加主链路用不上**，仅可能在
查重下载/后续 RSS 等场景用到。

---

## 5. 字段映射方案（麒麟特有，待定部分见 §6）

| 麒麟字段 | 来源 | 备注 |
|---|---|---|
| `type` | 常量 `408` | |
| `name` | `descr.build_kylin_title(task)` | ★ 麒麟定制口径（2026-11-12 定案）：`艺术家 - 专辑 年份 编码[-发布组][- OpenCD]`，**不带**位深/采样率；解析复用猫站口径同一份清洗（见 §6 #3） |
| `small_descr` | `descr.build_music_small_descr(task)` | |
| `descr` | `descr.build_descr(..., site=db.SITE_HDKYLIN)` | BBCode；麒麟默认形态（保留日志折叠 + 转载声明） |
| `technical_info` | `""` | 无 MediaInfo 能力，留空（不硬塞抓轨日志） |
| `url` / `pt_gen` / `nfo` | `""` | 音乐留空 |
| `processing_sel[4]`（年代） | `task.year` | `2026→12 2025→11 2024→10 2023→1 2022→2 2021→3 2020→4 2019→5 2018→6 2017→7 2016→8 ≤2015→9`（★ 非线性） |
| `medium_sel[4]`（媒介） | `task.medium_sel` 语义 | CD→`33`、OST/Track→`32`、WEB/数字→`31`，兜底 CD→`33` |
| `audiocodec_sel[4]` | `task.audiocodec_sel` 按名字 | FLAC→`1` WAV→`13` APE→`12` M4A→`14` MP3→`4` MPEG→`18` AAC→`6` Opus→`20` LPCM→`10` 兜底 Other→`7` |
| `codec_sel[4]` | 固定 Other `5` | 音乐无视频编码 |
| `standard_sel[4]` | 固定 Other `9` | 音乐无分辨率 |
| `source_sel[4]`（地区） | 皇后地区标签 | ★ **方案 B（已定案）**：大陆→CN(15) / 港台→香港(16) / 欧美→欧洲(28) / 日韩→日本(19) / 其它→Other(14) |
| `team_sel[4]` | 固定 Other `5` | 官方组（HDK 等）不能冒充 |
| `tags[4][]` | 白名单 | 国语`5`/中字`6`/LIVE`10`/Blu-ray`9` 等，禁转首发不勾 |
| `uplver` | `yes` | 匿名 |

---

## 6. ★ 待用户提供 / 待决定（缺一不可，逐条）

| # | 项 | 为什么必须 | 当前状态 |
|---|---|---|---|
| 1 | **`HDKYLIN_COOKIE`** | NexusPHP 登录态 = Cookie；没有它无法发布。★ 用户只给了 passkey，**没给 Cookie** | ❌ 缺 |
| 2 | **图床** | ✅ **已明确：不强制**（站规无图床条款）。官方 `img.hdkylin.com`（Chevereto 风格）可选；若用，需给其 **API Key** 并确认上传端点（`/api/1/upload`？） | ✅ 结论定，仅 API 细节待定 |
| 3 | **麒麟标题格式规范** | ✅ **已定案（2026-11-12，定制实现）**：新增 `descr.build_kylin_title()`，产出 `艺术家 - 专辑 年份 编码[-发布组][- OpenCD]`（**不带**位深/采样率，与猫站口径共用同一份清洗/解析）。★ 实测生产库 40 条：OpenCD 源数据**没有独立"中文名"字段**（中文专辑标题本身就是中文，如 `群星 - 追忆林振强`），站规例句那种「中文名 原名」双语形态不可得 ⇒ 不造中文名，宁缺勿错；源标题带 `- OpenCD` 时保留来源后缀 | ✅ 定案，已实现 |
| 4 | **地区映射的粗→细损失** | ✅ **已定案（方案 B，2026-11-12）**：大陆→CN(15)、港台→香港(16)、欧美→欧洲(28)、日韩→日本(19)、其它→Other(14)。接受「约一半标错」（台湾→香港、美/英→欧洲、韩→日本），audit 显式标注。外部权威源（MusicBrainz）实测命中仅 3/5、且一专辑多发行国，已评估**不采用** | ✅ 定案 |
| 5 | **回加 qB 实例 + 分类/标签** | 麒麟种回加到哪个 qB 实例（现有 3010 发种实例？）、打什么分类/标签 | ❌ 待定 |
| 6 | **是否要做编辑阶段** | 站规「不得改变管理员修改」⇒ 建议默认不编辑（同蟹黄堡） | 建议不编辑 |

★ 最高优先级是 **#1 Cookie** 和 **#3 标题格式**（后者决定站上会不会被管理员打回）。
★ #4 可参考蟹黄堡先例：先落「保守值 + 显式标注」，不硬拆。

---

## 7. 建议实施步骤（每步可单独验收，照 PLAN-crabpt 分步法）

| 步 | 内容 | 验收判据 | 风险 |
|---|---|---|---|
| 1 | 写 `hdkylin_site.py` + `selftest` | 离线自检全过；字段映射与 §2 表逐项对照 | 零（纯离线） |
| 2 | `config.py` / `db.py` / `publish.py` / `daemon.py` 接线 | `--selftest` 过；`--site hdkylin --dry-run` 打印完整表单 | 零 |
| 3 | 配 `.env`（Cookie 等）→ `--dry-run --limit 1` | 提交前看到**完整表单**，逐字段核对（尤其 processing/source 语义） | 零 |
| 4 | **单条试发 + 详情页回读** | 年代/媒介/音频编码/地区与提交值逐字段一致 | ★ 不可逆，`--only <hash>` 点名 |
| 5 | 开 `ENABLE_PUBLISH_HDKYLIN=true` 定时 | `site_publish` 里 `hdkylin` 行有值、EXISTS 正常 | 中 |

★ 步 1~3 零风险，**可先做**；步 4 起真发，需用户点头。

---

## 8. 已知风险

1. ★ **`processing_sel[4]` / `source_sel[4]` 语义与蟹黄堡互换** —— 照抄会静默填错（页面能提交、字段错位）。
2. ★ **年代映射非单调**（2026→12、2023→1、2016→8、更早→9）—— 手写表必须逐项核对，别用公式推。
3. ★ **音频编码编号独立**（FLAC=1 与蟹黄堡 10 不同）—— 按名字转，不按数字转（同 crabpt 纪律）。
4. ★ **音乐标签极少**（无粤语/英语）—— 语言标签基本只能勾「国语」，其余宁缺勿错。
5. ★ **地区粗→细不可逆** —— 已按**方案 B** 取代表值（港台→香港、欧美→欧洲、日韩→日本），接受约一半标错，audit 显式标注。
6. ★ 账号是否满足麒麟发种门槛未知 —— 页面顶部有「指标1/2/3 未通过」提示（下载增量、做种积分），
   ★ **需确认当前账号等级是否可直接发音乐种**（发种教程里可能有门槛说明）。
