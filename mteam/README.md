# mteam —— M-Team（馒头）发种 / 候选子项目

> **状态**：考察与骨架阶段（2026-11-13）。**未接入生产 daemon，不参与镜像构建。**
> **定位**：把 M-Team 的发种/候选能力从 `opencd2others` 主流水线里**物理隔离**出来，单独沉淀规则、标题口径与 API 客户端，等接口实测通过后再决定是否并入 `src/publish.py`。
> **规则原文整理**：[`RULES.md`](RULES.md)（发布规则 / 候选规则 / 标题命名 / 总则，逐条按站点原文重组）
> **前置考察记录**：[`../PLAN-mteam.md`](../PLAN-mteam.md)（站点形态、API 端点实测、账号权限结论）

---

## 0. 一句话结论

M-Team 不是 NexusPHP，**不能套用本项目现有的六个站接入方式**。本账号（mTorrent Master）**没有直接发种权**，只能提交候选，候选经审核通过后 **24 小时内必须开始做种**，否则被移除。

---

## 1. 为什么单独开一个子项目

`opencd2others` 现有六个站的接入全部建立在「NexusPHP 表单 + `takeupload.php` + Cookie 鉴权」之上（详见 `../HOWTO-新站接入.md`）。M-Team 在这三件事上全部不同：

| 维度 | NexusPHP 站（现有六站） | M-Team |
| :--- | :--- | :--- |
| 发布页 | `upload.php` 服务端渲染表单 | `/upload` 返回 10,277B 的 SPA 空壳，**表单在 JS 里** |
| 提交 | `POST takeupload.php`（multipart 表单） | `POST /api/torrent/createOredit`（JSON / multipart **未知**） |
| 鉴权 | Cookie 五件套 | **`x-api-key`**（站规：第三方工具用 cookie 调 API 有停号风险） |
| 发布形态 | 直接发布 | **候选 → 审核 → 转正** |
| Tracker | 站点注入 | 不必填，系统覆写 |

因此不复用 `publish.py` 的 `--site` 分支，改为独立目录，先解决「接口能不能调通」与「标题/规则口径」，再谈接线。

> **不影响现有生产**：根目录 `Dockerfile` 只 `COPY src/*.py`，本目录不在镜像内；本目录也不被 `daemon.py` 引用。改动这里不会触发任何发种行为。

---

## 2. 账号与权限（实测，详见 `../PLAN-mteam.md`）

| 项 | 值 |
| :--- | :--- |
| uid / 用户名 | `279680` / `iSunker` |
| 等级 | `role=9` mTorrent Master（最高级） |
| 权限点 | `1105, 1107, 1002, 1003, 1101`（与 7/8 级相同，**不含发布权**） |
| `authorities` | 含 `USER_OFFER_PUBLISH`（候选），不含发种权 |
| 已发布种子数 | `0`（`UPLOADED` 接口 total=0） |
| 结论 | **只能走候选**；想直接发种需另获发布组权限 |

`RULES.md §3` 补充：候选通过满 10 种的用户可以免候选，但该账号发布数为 0，**不满足免候选条件**；且「资源 > 100GB」「游戏/软件分类」**强制候选**，免候选也绕不过。

---

## 3. 目录结构

```
opencd2others/mteam/
├── README.md          本文（项目说明、流程、未决项）
├── RULES.md           站点规则整理（发布 / 候选 / 标题命名 / 总则）
├── mteam_title.py     标题构建 + 合规校验 + 离线自检
└── mteam_api.py       API 客户端 + 枚举 + 候选判定 + 离线自检
```

两个 `.py` **零第三方依赖**（仅标准库），可脱离容器直接跑。

---

## 4. 程序说明

### 4.1 `mteam_title.py` —— 标题构建与校验

按 `RULES.md §4.5` 的格式产出主标题，并按 §4.1 的禁令做形状校验。

```python
from mteam_title import build_movie_title, build_music_title, normalize_audio, validate_title

build_movie_title("Moneyball", 2011, "bluray", "2160p", "x265",
                  normalize_audio("DTS-HD MA", "5.1"), "MTeam")
# => 'Moneyball 2011 BluRay 2160p x265 DTS-HD MA5.1-MTeam'

build_music_title("Kidney", "Better Late Than Never", 2014, "flac", "分軌")
# => 'Kidney - Better Late Than Never 2014 FLAC 分軌'

validate_title("蜘蛛侠 2026 BluRay 2160p H.265 DDP5.1")
# => ['主标题含中日韩字符（仅 HACG 类在管理特许下可用）']
```

关键设计：

| 点 | 处理 |
| :--- | :--- |
| 编码写法分场景 | 压制用 `H.264`/`x265`，原盘/REMUX 用 `AVC`/`HEVC`；`normalize_video(codec, scene="encode"|"disc")` |
| 音频别名 | `EAC3`→`DDP`、`AC3`→`DD`（站规明令禁止写 EAC3/AC3） |
| 点号不误杀 | `H.264`、`5.1` 是合法形态，先剔除再做「禁止点号」判定 |
| 汉字白名单 | 规则要求音乐标题写 `整軌`/`分軌`，该词不触发「禁中文」 |
| 未知枚举 | 抛 `ValueError`，**不静默放行** |

自检含**阴性对照**（中文、扩展名、括号、禁止符号、缺年份、`4K/HQ` 六类各一条必红样本）：

```bash
python mteam/mteam_title.py        # => mteam_title selftest OK
```

### 4.2 `mteam_api.py` —— API 客户端

```bash
export MTEAM_API_KEY=...           # 只从环境变量读，不落盘、不打印

python mteam/mteam_api.py --selftest                    # 离线自检，不联网
python mteam/mteam_api.py --profile                     # 账号资料 / role / authorities
python mteam/mteam_api.py --categories videoCodec       # 拉枚举（category/videoCodec/...）
python mteam/mteam_api.py --search "Spider-Man"         # 搜索已有种子（查重用）
python mteam/mteam_api.py --check-offer 120 Movie       # 是否强制候选
```

| 方法 | 端点 | 实测状态 |
| :--- | :--- | :--- |
| `profile()` | `/member/profile` | ✅ 已验证（`code=0`） |
| `enum_list(name)` | `/torrent/{name}List` | ✅ 已验证（9 个枚举全部 200） |
| `search()` | `/torrent/search` | ⚠️ 路径来自前端静态提取，**未实测** |
| `detail()` | `/torrent/detail` | ⚠️ 未实测（限额 100/h，读数前注意） |
| `gen_dl_token()` | `/torrent/genDlToken` | ⚠️ 未实测 |
| `post_create_or_edit()` | `/torrent/createOredit` | ⚠️ 仅验证「鉴权已过」（空载荷返回 `code=1 參數錯誤`），**载荷未实测** |

`post_create_or_edit(payload, dry_run=True)` 默认 dry-run，只回显 `path` / `payload_keys` / `payload`，不发请求。

`CREATE_OR_EDIT_FIELDS_PENDING` 常量登记了**待确认的字段名清单**——它们是待办，不是已验证的接口契约，**不要**直接拿去拼载荷。

---

## 5. 发种 / 候选流程（含闸门）

```
① 源数据与合规           ② 标题              ③ 提交              ④ 审核后 24h 内做种
   ├ 分类是否强制候选      ├ build_*_title     ├ createOredit        ├ tracker 转正即回加 qB
   ├ >100GB 需发布码       ├ validate_title    ├ 载荷待实测          └ 逾期候选被移除
   ├ 图床/截图/mediainfo   └ dry-run 核形状    └ 只信详情页回读
   └ 查重（search）
```

| 阶段 | 动作 | 判据 |
| :--- | :--- | :--- |
| ① 前置 | 判断是否强制候选（`--check-offer`）、是否需发布码（>100GB 音乐 / 国产 / Bangumi 缺失）、查重 | 三条结论都有出处，不靠推测 |
| ② 标题 | `build_*_title` + `validate_title` 双闸门 | 返回空问题列表；dry-run 打印完整主副标题 |
| ③ 提交 | `createOredit`（dry-run 先行） | 回读 `/torrent/detail` 与实际站点页面，**不信提交成功的打印** |
| ④ 做种 | 候选通过后 24h 内完成 qB 回加 | 站点显示做种中；逾期候选被移除 |

**24 小时窗口是这个项目最硬的约束**：`RULES.md §3` 明确「候選通過 24 小時後仍未開始做種，該候選將被移除」。回加链路必须在此之前跑完。

---

## 6. 规则要点速查

完整内容见 [`RULES.md`](RULES.md)，自动化最常踩的六条：

1. **主标题仅英文/拼音**，禁中文（HACG 类除外）；`! ; . # $ % :` 禁符；禁扩展名；禁括号。
2. **禁他站名称/推广** ⇒ 源标题的 `- OpenCD` 后缀必须剥离。
3. **音乐标题不带位深/采样率**，必须写 `整軌`/`分軌`。
4. **必须附图**；影片类必须 mediainfo/bdinfo + ≥3 张截图（或 1x3）。
5. **>100GB 音乐必须先取发布码**（`ticket.m-team.io`），发布码 24h 内未用即失效。
6. **候选通过 24h 内必须开始做种**；候选被冻结后 48h 未处理直接删除，解冻要 PM 管理员并附链接。

---

## 7. 未决项（按优先级）

| # | 事项 | 阻塞点 | 需要什么 |
| :--- | :--- | :--- | :--- |
| P0 | `createOredit` 真实载荷 | 字段名/编码形式/候选标志位全未知 | 浏览器登录后手工发一条候选并录 HAR（取完即删，不落凭据） |
| P0 | API Key 未部署 | 本机 `.env`、`.env.example` 与 NAS `/volume2/docker_ssd/*/.env` 均无 `MTEAM_API_KEY` | 用户在「控制台 → 实验室 → 存取令牌」生成，只写入 NAS `data/.env` |
| P1 | 视频/游戏/软件分类 id | 仅实测了音乐相关（`Music=110`、`Music(Lossless)=434`、`MV=406`） | `--categories category` 现场拉取后补进 `mteam_api.py` 常量 |
| P1 | `/torrent/search` 与 `/torrent/detail` | 未实测，参数形态未知 | 有 key 后各跑一次，记录返回结构 |
| P2 | 候选状态查询 | 不确定用哪个端点查「待审/冻结/通过」 | 同 P0 的 HAR 里一并观察 |
| P2 | 与主流水线接线 | 需先完成 P0 | 参照 `HOWTO-新站接入.md` 六处接线，但鉴权与提交层要重写 |

---

## 8. 安全红线

- **Key 只进 NAS `data/.env`**，不进代码、文档、日志、Git、`daemon_env.json`。
- `gen_dl_token()` 返回的 URL 含凭据，**不得打印或落盘**。
- 站规禁止把站点/tracker 地址与沟通内容外传（`RULES.md §1`），文档与日志同理。
- 提交是**不可逆**动作：dry-run → 单条试发 → 详情页回读，三步一步都不能跳。

---

## 9. 自检与验收

```bash
# 语法
python -m py_compile mteam/mteam_title.py mteam/mteam_api.py

# 离线自检（含阴性对照，不需要网络与凭据）
python mteam/mteam_title.py
python mteam/mteam_api.py --selftest

# 仅在有 key 时
python mteam/mteam_api.py --profile
python mteam/mteam_api.py --categories category
```

当前状态：**两个模块离线自检通过；联网路径除 `profile` / `enum_list` 外均未验证。**
