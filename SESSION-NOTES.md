# 会话归档（按时间倒序，只增不改）

> **这是会话结论的落盘处** —— 防止"结论只活在某次对话里"（那正是 `ERR-META-04` 说的"传说"）。
> ★ 纪律：**只写"已实测坐实"的结论**，未坐实的写进「未解」并标出**待验的判据**。
> ★ 会变的数字（库计数、阶段年龄）**不写这里** —— 见 `counts.json`。
> ★ 本文档**不进 `deploy.sh` 的 `FILES`**（与 `ERRATA.md`/`counts.json` 同理：
>   文档不是运行时输入；加进去会撞 `ERR-DOCKER-05` —— 落在 compose 目录却不进镜像）。
>   `.dockerignore` 也应把它排除，免得进构建上下文。

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
| `ENABLE_PUBLISH=**true**` + "publish 是开着的、每 30 分钟一跑" | **三源一致为 false**（快照 / `docker exec env` / 72h 无 publish 日志） |
| 第 4 条「短路 `241` vs 库 `NO_LOG_FILE=244`，差 3」 | **量纲混淆**：短路 = `qB ∩ 库[NO_LOG_FILE]`，**不可**与库总数相减（`ERR-META-05`）；且 `241` 是**手抄错值**（日志原话 `240`） |
| 第 1 条「`iter_my_uploads()` 翻页是坏的」 | **已证伪**：函数无 bug；`userid=10427` 站点自报**只有 3 条**，函数产出 2 条是解析边缘差 |

★ 改法（替换文字）见 TaskList #19。**提示词是用户配置，本机改不了。**

**3. 待用户确认的输入**

- **`id=85068`** —— 用户给出但**未说明含义**（我们真正的 uid？某种子 id？叶惠美那个种子？）。
  **未猜、未用**。#17 的入口。
- **`ENABLE_CRAWL` 在不在生产 `.env`** —— #23 的唯一问题。

