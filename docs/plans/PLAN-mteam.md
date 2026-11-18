# PLAN-mteam：M-Team（馒头）音乐转种接入 —— 考察与资格结论

> 状态：**2026-11-13 阶段①② 完成；结论：只能发「候選」，不能直接发种；且必须走 API Key（禁止 cookie）**
> 目标站：`https://kp.m-team.cc/upload`（繁体中文界面）
> 一句话：M-Team 是 SPA + JSON API 站，接入必须用 `x-api-key`（用网页 cookie 调 API 有停号风险）；本账号等级最高但**没有发种权**，只能提交候选。

---

## 0. 考察产物（已归档 `opencdhtml/`）

| 文件 | 内容 |
|---|---|
| `M-Team 發佈規則.html` | 发布规则（`wiki.m-team.cc/zh-tw/upload-rules`） |
| `M-Team 上傳標題命名.html` | 命名规范，含「音樂：無損音樂」口径 |
| `M-Team 候選規則.html` | 候选规则（免候选门槛、强制候选条件） |
| `M-Team 總則.html` | 站点总则 |
| `M-Team 上傳頁 SPA 外殼.html` | `/upload` 实际返回（10,277B 空壳，前端路由） |

---

## 1. 站点性质：非 NexusPHP（实测）

| 观测 | 结果 |
|---|---|
| GET `/` `/login` `/upload` `/rules` `/faq.php` | 全部 200 且响应体**完全相同**（10,277B，`<title>M-Team</title>`）⇒ SPA 外壳 |
| 前端 | React + webpack，静态资源 `static.m-team.cc/static/js/*.chunk.js`（28 个懒加载 chunk） |
| API 根 | `api.m-team.cc/api`、`api2.m-team.cc/api`、`api.m-team.io/api` |
| 无 UA 请求 | `302 → https://www.google.com/`；**必须带浏览器 UA** |
| 直连 vs 代理 | `kp`/`api` 两者皆通；`static.m-team.cc` **直连通、走 7894 代理 TLS 被拒（SSLEOF）** |

## 1.1 ★ 站规硬约束：第三方必须用 API Key

`wiki.m-team.cc/zh-tw/api` 原文：

- 第三方工具请改用 **API Access Token（`x-api-key`）**，于「控制台 → 实验室 → 存取令牌」自助取得。
- **「若发现第三方工具仍以 cookie 存取接口，不排除停用帐号」** ⇒ 本项目的 Cookie/JWT 路线**作废**，只能走 API Key。
- 禁止调用前缀：`/admin/**`、`/login`、`/apikey/**`；`/member/**` 仅白名单（`profile`/`base`/`bases`/`sysRoleList`/`getUserTorrentList`/`getCrimeRecords`/`queryUserLoginHistory`）；`/msg/statistic`。
- 速率建议：种子下载 1000/天、100/小时；`/torrent/detail` 100/小时；`/torrent/search` 1000/近 24h。
- Swagger 文档在 **test2 站**（`https://test2.m-team.cc/api/swagger-ui.html`），需工单申请测试站账号；官方自述「请求格式 json/form 可能与实际不符、必填参数可能不准」。

## 1.2 接口全景（前端静态提取 + 实测）

| 用途 | 路径 | 实测 |
|---|---|---|
| 账号资料 | `/member/profile` | ✅ `code:"0"` |
| 等级表 | `/member/sysRoleList` | ✅ 22 个等级 + `permissions` |
| 批量用户 | `/member/bases` | ✅ |
| 我的种子 | `/member/getUserTorrentList` | ✅ `type` ∈ `UPLOADED`/`SEEDING`/`LEECHING`/`COMPLETED`/`INCOMPLETE`/`SEEK`/`SEEK_SUBMIT` |
| 枚举 | `/torrent/categoryList` `mediumList` `audioCodecList` `standardList` `sourceList` `processingList` `teamList` `videoCodecList` | ✅ 全部 200 |
| **发布/编辑** | **`/torrent/createOredit`** | 空载荷 → `code:1「參數錯誤」`（**非** 401/权限不足）⇒ 鉴权已过，载荷仍未知 |
| 详情/文件/同伴 | `/torrent/detail` `/torrent/files` `/torrent/peers` | 未跑（限额 100/h） |
| 下载 | `/torrent/genDlToken` | 未跑 |
| 搜索 | `/torrent/search` | 未跑 |
| 候选配置 | `/offer/config` | ✅ `OFFER_MIN_VOTES=21`、`OFFER_UP_TIMEOUT=86400`、`OFFER_VOTE_TIMEOUT=604800` |

---

## 2. ★ 账号资格结论（2026-11-13 实测）

| 项 | 值 | 来源 |
|---|---|---|
| uid / 用户名 | `279680` / `iSunker` | `/member/profile` |
| 等级 | `role="9"` ⇒ **mTorrent Master（大臣，最高级）** | `sysRoleList` id=9 |
| 该等级权限点 | `1105, 1107, 1002, 1003, 1101` | 与 7/8 级完全相同 |
| 权限点中含发布权？ | **否**（发布组/管理组才有 `546/614/615/616/1001` 等） | 对比 id=11/20/21/13/14 |
| `authorities` | `USER_TORRENT`、`USER_ACCOUNT_NEVER_DELETE`、`USER_FUN_POST`、`USER`、`USER_STORE`、`USER_INVITE_REG`、**`USER_OFFER_PUBLISH`** | `/member/profile` |
| 已发布种子数 | **0**（`type=UPLOADED` → `total=0`） | `/member/getUserTorrentList` |
| 交叉校验 | `COMPLETED=1480`、`SEEDING=766` 正常返回 ⇒ 口径可信 | 同上 |

**判定**：

1. 站规「候選通過達 10 種以上的用戶免候選」+「>100GB 強制候選」⇒ 本账号 0 个发布 ⇒ **不满足免候选**。
2. 等级权限表（wiki）+ `permissions` 实测 ⇒ **等级本身不授予发种权**（mTorrent Master 只多了「永久保号」）。
3. ⇒ **只能走「新增候選」**（`USER_OFFER_PUBLISH`），候选经审核通过后才在站内正式发布。

> 标注：结论基于站规 + 账号数据双重证据；**未做**「页面上真点一次发布」的实测（`createOredit` 真实载荷未知，且发布不可逆）。

---

## 3. 枚举落地（音乐相关）

| 枚举 | 结果 |
|---|---|
| **分类** | `Music`=**110**（父），子分类 **`Music(Lossless)`=434**、`MV`=406 |
| `audioCodecList` | FLAC=**1**、APE=2、DTS=3、MP2/3=4、OGG=5、AAC=6、Other=7、AC3(DD)=8、TrueHD=9、TrueHD Atmos=10、DTS-HD MA=11、E-AC3(DDP)=12、DDP Atmos=13、LPCM/PCM=14、WAV=**15** |
| `sourceList` | Web-DL=8、Bluray=1、Remux=4、HDTV/TV=5、DVD=3、**CD=10**、Other=6 |
| `processingList` | **是地区不是年代**：CN=1、US/EU=2、HK/TW=3、JP=4、KR=5、OT=6（★ 与 NexusPHP 站同名不同义，勿照抄） |
| `standardList` | 1080p=1、1080i=2、720p=3、SD=5、4K=6、8K=7（视频分辨率，音乐不用） |
| `mediumList` | Blu-ray=1、HD DVD=2、Remux=3、Encode=7、MiniBD=4、HDTV=5…（视频载体，音乐不用） |
| `teamList` | 25 项：MTeam=9、MWeb=44、TPTV=43、TnP=23、BMDru=6、CNHK=19、Pack=8…（**不冒充官方组**） |

★ **整轨/分轨没有独立枚举字段** —— 它是标题文本的一部分（站规示例 `... 2014 FLAC 分軌`）。

---

## 4. 发布规则要点

### 4.1 标题（已定案：华语专辑走「官方中文名例外」）

- 主标题**仅限英文/拼音，禁中文**；唯一例外：**中港澳台以中文发行的专辑可用官方中文名**（← 本项目采用此条）。
- 格式：`藝人名稱 - 專輯名稱 年份 音頻編碼 整軌/分軌-小組名稱`
  - 实例：`Kidney - Better Late Than Never 2014 FLAC 分軌`，副标题 `腰乐队 相见恨晚 专辑`
  - **不带位深/采样率**；必须写明 整軌/分軌；命名须与官方发行一致（禁别名/译名）。
- 禁扩展名、禁 `! ; . # $ % :`、禁括号（特定分类除外）；副标题禁全角标点。
- 种子内容**不得含他站名称/推广** ⇒ 原 `- OpenCD` 后缀**必须处理**（保留真实制作组、去掉站点名；具体取舍见 §7 待定项）。
- **>100GB 音乐种必须先到 `ticket.m-team.io` 取「發佈碼」**。

### 4.2 发布 / 候选 / 撤回

- 種子內容**必须附图片**；Tracker 不必填（系统自动覆写）。
- 候选：审核制；**`OFFER_UP_TIMEOUT=86400` ⇒ 候选通过后 24 小时内必须开始做种，否则移除**。
- 撤种：≥3 完成者，或 72h 无人下载，或做种 >7 天且完成数 <3 且 24h 无人下载。
- 出现死种/DUPE 可申请删种重发（留旧不留新）。

---

## 5. 凭据与配置

| 项 | 结论 |
|---|---|
| 网页 Cookie | CookieCloud 里只有 `.m-team.io` 的 `cf_clearance`，无登录态 ⇒ 且**按站规不应使用** cookie 调 API |
| API Key | 用户已提供（存 NAS `.env` 的 `MTEAM_API_KEY`，**不入库、不硬编码**；站规要求定期轮换） |
| passkey | 主链路不需要（回加用站点重写的 `.torrent`） |

---

## 6. 实现路线（与本项目现有三站的差异）

| 项 | NexusPHP 站 | M-Team |
|---|---|---|
| 提交 | `POST takeupload.php`（multipart 表单） | `POST /api/torrent/createOredit`（载荷待定） |
| 鉴权 | Cookie 五件套 | `x-api-key`（站规强制） |
| 详情/下载 | `details.php?id=` / `download.php?id=` | `/torrent/detail` / `/torrent/genDlToken` |
| 匿名 | `uplver=yes` | 字段待确认（profile 有 `anonymous` 字段） |
| 发布形态 | 直接发布 | **候选**（审核后转正） |
| 标题 | 复用 `normalize_music_title()` | 新写 `build_mteam_title()`：去位深/采样率、补 整軌/分軌、华语专辑用官方中文名 |

---

## 7. 未决项 / 下一步

1. **`createOredit` 真实载荷**（最高优先级）：字段名、`json` vs `multipart`、`.torrent` 文件字段、候选标志位、`anonymous` 名称。
   - 建议取法：浏览器登录后**手工发一条候选**并录 HAR / 或用 Playwright 打开 `/upload` 抓表单 DOM + 提交请求体（取完即删，不落地凭据）。
2. **候选 → 自动回加 qB 的 24h 窗口**：`OFFER_UP_TIMEOUT=86400`，`readd_to_qb_mteam()` 必须快于审核通过后 24h（现有 `readd` 链路按站点重写的 `.torrent` 回加，可直接复用）。
3. **`- OpenCD` 后缀取舍**：站规禁他站名称，倾向「保留真实制作组 + 去掉站点名」，需确认。
4. **图片**：站内编辑器用私有 imgApi；站规未强制官方图床 ⇒ 可沿用 PicGo/Lsky，但需实测审核是否接受。
5. **>100GB 大包**：人工申请發佈碼，不进自动流程（按规则强制候选且需码）。
