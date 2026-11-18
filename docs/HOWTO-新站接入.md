# HOWTO：给 opencd2others 接入一个新 PT 站（音乐转种）

> 状态：**2026-11-12 沉淀，麒麟（HDKylin）接入实战全流程验证通过**
> 定位：`AGENTS.md` §7 是**一页速查**，本文是**完整操作手册**（每阶段的命令、验收判据、踩过的坑与实证数据）。
> 适用前提：目标站是 NexusPHP（发布页 `upload.php` / 提交 `takeupload.php`），有音乐分类；来源是 OpenCD（皇后）音频抓轨资源。
> 已落地样本：Musopia 乌托邦 / CrabPT 蟹黄堡 / PTerClub 猫站 / XingWan 星湾 / **HDKylin 麒麟**。
> 铁律：新增站点**不改其他站的既有代码路径**，用独立 `--site <站>` 分支 + 独立 daemon 阶段；发布不可逆，闸门一步都不能跳。

---

## 0. 全景流程与门禁

| 阶段 | 动作 | 验收判据（"做完"的证明） | 风险 |
|---|---|---|---|
| ① 考察 | 探针抓发布页/规则页、逐字段核语义、写 `PLAN-<站>.md` | `opencdhtml/` 有 HTML；每个 select 的语义有人工确认记录 | 零风险（只读 GET） |
| ② 凭据 | CookieCloud 取 Cookie 写 NAS `data/.env`；**开关先 false** | `grep -c '^XXX_COOKIE='` = 1（不打印值）；开关 = false | 零风险 |
| ③ 代码 | 适配层 + 六处接线 + 标题口径定案 | 各模块离线自检全过、`gen_counts` 重生成 | 零风险 |
| ④ 构建部署 | 预检 → 构建 → **本机冒烟** → push → NAS 点名 sha 拉取重建 | 两端 digest 一致 + 容器内 MD5 一致 + 容器 env 回读正确 + healthy | 需用户授权推镜像 |
| ⑤ 发布验证 | dry-run 逐字段 → **单条试发** → 详情页回读 | `site_publish` 落 EXISTS；详情页字段与提交表单逐项一致 | **不可逆** |
| ⑥ 开定时 | 开 `ENABLE_PUBLISH_XXX=true` + force-recreate | daemon 横幅出现该阶段；观察 1~2 轮失败率 | 自动发布开始 |

---

## 1. 阶段① 站点考察：不靠肉眼抄字段

```bash
# 仓库根目录；该站 Cookie 需已在 .env
python tools/probe_site.py --site hdkylin
# 任意站：--base https://站点 --cookie-env XXX_COOKIE
# 规则页自动发现失败时：--rules-url https://站点/wiki/上传规则
```

`src/site_probe.py`（通用函数）+ `tools/probe_site.py`（CLI）自动完成：按 `<title>` 把发布页/规则页存进 `opencdhtml/`、解析 `action`/`enctype`/每个 `select` 的全部 `option`/全部 `input`、从 `/usercp.php`「密钥」单元格抓 passkey（默认脱敏）。

**逐字段核对语义（本项目最大教训）**——同名字段在不同站可能完全相反：

| 字段 | 麒麟 | 蟹黄堡 | 纪律 |
|---|---|---|---|
| `processing_sel[4]` | 年代 | 地区 | 读 option 文字确认，不按字段名猜 |
| `source_sel[4]` | 地区 | 媒介 | 同上 |
| `medium_sel[4]` | 媒介（CD=33） | 无此字段 | 编号各站独立 |
| `audiocodec_sel[4]` | FLAC=1 | FLAC=10 | **按名字转，不按数字搬** |

**站规五问**：图床是否强制？标题格式？转载规则（能否删原站后缀/改文件名）？体积与 dupe 限制？是否允许编辑？
**站点坐标**：域名、发布页、tracker、`download.php?id=`、`details.php?id=`、匿名参数（多为 `uplver`）。

**源数据可得性探针（容易漏）**：站规要求的字段，源库里是否真有？麒麟站规例句是「中文名 + 原名」双语标题，实测生产库 40 条 `formatted_title` / `mapped_json` 后发现 **OpenCD 没有独立中文字段**（中文专辑标题本身就是中文，英文专辑就是原名）⇒ 定案「不造中文名，宁缺勿错」。**结论必须来自现场取数，不许从站规例句倒推源数据。**

把表单映射、地区粗→细方案、未决项写进 `PLAN-<站>.md`（如 `PLAN-hdkylin.md`），决策留痕。

---

## 2. 阶段② 凭据与防误发

- Cookie 从 NAS CookieCloud（`192.168.0.7:3050`）取，登录态五件套：`c_secure_uid` / `c_secure_pass` / `c_secure_ssl` / `c_secure_tracker_ssl` / `c_secure_login`；解密方法见 `README.md`。
- 凭据**只进** NAS `/volume2/docker_ssd/opencd2others/data/.env`，严禁进代码/文档/日志/`daemon_env.json`；写完后只 `grep -c` 验证存在，不打印值。
- passkey 由 `site_probe.fetch_passkey()` 自动取，**发布/回加主链路用不上**（回加用站点重写后的 `.torrent`），不必进 `.env`。
- ★ **防误发**：新 `.env` 里 `ENABLE_PUBLISH_XXX` 必须是 `false`。本次实测曾发现它提前是 `true` 而容器尚无新代码 —— 一旦部署就会绕过 dry-run 自动发种。部署前用 `grep -n '^ENABLE_PUBLISH_XXX' data/.env` 复核。

---

## 3. 阶段③ 代码：新增适配层 + 六处接线（第六处最易漏）

| 文件 | 改动 |
|---|---|
| `src/config.py` | `XXX_URL` / `XXX_COOKIE` / `XXX_MUSIC_CATEGORY` / `XXX_MAX_PER_CYCLE` / 开关等 |
| `src/db.py` | `SITE_XXX` 常量 + `XXX_DIRECT_WHERE`（同口径复用，别另写 SQL）+ `pending_xxx_direct()` |
| `src/<站>_site.py` | **新增主体（不算接线）**：`map_year` / `map_medium` / `map_audiocodec` / `map_region` / `map_tags` / `build_fields` / `XxxClient` / `selftest()` |
| `src/publish.py` | `import` + `--site` choices + `run_xxx()` + `_xxx_cover()` + `readd_to_qb_xxx()` |
| `src/daemon.py` | `publish_xxx` 阶段（`ENABLE_PUBLISH_XXX`）+ `ENV_SNAPSHOT_KEYS` + 发种窗口名单 |
| `.env.example` | 站点块（URL/COOKIE/分类/开关/周期/间隔） |
| **`tools/image-smoke-test.sh`** | ★ **第六处、最易漏**：`D_ENV` 显式关掉新阶段 `ENABLE_PUBLISH_XXX=false` |

最后一行不是可选：`daemon.py` 里出现 `env_bool("ENABLE_...")` 而冒烟脚本没跟上，其 ⑤「开关清单核对」会判红并**拒绝 push**。本次麒麟接入就是被它拦下（还带出两个星湾遗留开关），修复后 14 个开关全绿才放行。冒烟脚本用「源码做真源」核对，别手工绕过。

### 标题口径怎么定

- **默认**：复用 `descr.normalize_music_title()` / `descr.build_music_small_descr()` / `descr.build_descr(site=...)` —— 全站一份，严禁复制第二份实现。
- **站规形状不同时**：新增 `descr.build_<站>_title()`（麒麟实例），但**复用同一份清洗与解析**（`_split_title` / `_parse_artist_album` / 年份与编码提取），只改尾巴。麒麟定案 = `艺术家 - 专辑 年份 编码[-发布组][- OpenCD]`，不带位深/采样率；源标题带 `- OpenCD` 时保留来源后缀。
- 新增断言要带**阴性对照**（麒麟自检：标题不含 `16bit`/`kHz`；`- OpenCD` 后缀阳性用例）。
- ★ **核对目标站的自动审核判据**：站规允许的形状不等于机器接受的形状。麒麟实测要求主标题全 Latin-1（详见 `ERR-SITE-11`），因此在 `publish.py::run_xxx` 的预检与提交前各设一道闸门；`dry-run` 通过 ≠ 审核通过，试发后还要看站点系统消息。

### 其它适配纪律

- 匿名 `uplver=yes` 强制；`audit` 逐字段写「这个值从哪来」。
- 固定 Other 字段（视频编码/分辨率/制作组）写明「不冒充官方组」。
- 标签白名单：禁转/首发/DIY/官种/驻站一律不勾；语言标签宁缺勿错。
- 地区粗→细不可逆时，代表值 + 会标错的说明都要进 `audit`（麒麟方案 B：港台→香港、欧美→欧洲、日韩→日本）。
- 年代映射非线性就手写表，别用公式（麒麟 2026→12、2023→1、2016→8、更早→9）。

---

## 4. 阶段④ 本地闸门 → 构建 → 部署

```bash
# 本机：dummy env 不联网
$env:QB_PASS='x'; $env:MUSOPIA_COOKIE='x'; $env:MUSOPIA_PASSKEY='x';
$env:PICGO_API_KEY='x'; $env:OPENCD_COOKIE='x'; $env:XXX_COOKIE='x';
$env:DATA_DIR='d:/Projects/PT-Project/opencd2others/.scratch/selftest-data'
python -m py_compile src/<站>_site.py src/publish.py src/db.py src/config.py src/daemon.py src/descr.py
python src/<站>_site.py            # 适配层自检
python tools/gen_counts.py          # 自检项数变了必须重生成，严禁手改 counts.json
python src/run.py --selftest        # 总闸
```

**构建推送**（大版本须用户明确授权；Windows 上 `bash` 可能指向坏掉的 WSL，用 Git Bash 绝对路径）：

```bash
"C:\Program Files\Git\bin\bash.exe" build-push.sh <附加tag>
# 内部顺序：build-preflight.sh → docker build → image-smoke-test.sh（push 前闸门）→ docker push
```

推送后**核对 Hub 上真的有这个 tag**（`curl -x http://192.168.0.7:7894 https://hub.docker.com/v2/repositories/isunker/opencd2others/tags`）——本次后台脚本失败时，本地 tag 与 RepoDigest 都在，但 Hub 的 `latest` 最后推送时间仍是两天前；若脚本被转后台，务必回读结果，别用「本地有镜像」推「已推送」。

**NAS 部署**：compose 里点名**不可变 sha tag**（不用 `latest`，见 `ERR-DOCKER-13`），然后：

```bash
ssh -p 36688 -i C:/Users/艾森克/.ssh/id_ed25519 iSunker@192.168.0.7 \
  'cd /volume2/docker_ssd/opencd2others && \
   sed -i "s#image: isunker/opencd2others:.*#image: isunker/opencd2others:<sha>#" docker-compose.yml && \
   /usr/local/bin/docker compose pull && \
   /usr/local/bin/docker compose up -d --force-recreate --remove-orphans musopia'
```

**四项验收，缺一不可**：
1. `docker images --digests | grep <sha>` 的 Digest 与本机 `docker image inspect <tag> --format '{{json .RepoDigests}}'` 一致（比 digest，不比 image ID）；
2. `docker exec opencd2musopia md5sum /app/<改动文件>` 与本机 `Get-FileHash -Algorithm MD5` 一致；
3. `docker exec opencd2musopia printenv <新开关>` 回读容器内的值（★ `.env` 里是 true **不蕴含**容器里是 true）；
4. `docker ps` healthy + `docker logs` 横幅确认新阶段进调度。

---

## 5. 阶段⑤ 发布验证（不可逆三步）

```bash
# ① dry-run：零提交，打印完整表单
/usr/local/bin/docker exec opencd2musopia python /app/publish.py --site <站> --dry-run --limit 2
# ② 单条试发：点名 hash
/usr/local/bin/docker exec opencd2musopia python /app/publish.py --site <站> --only <hash> --limit 1
# ③ 手动跑一轮（等价 daemon）
/usr/local/bin/docker exec opencd2musopia python /app/publish.py --site <站> --yes --auto --limit 3
```

- dry-run 核对：主/副标题形状、年代、媒介、音频编码、地区（audit 里的标错说明）、标签、匿名、简介来源声明；每条的版本闸门（文件集一致 + 分块物理校验）、封面来源、查重证据（查询数/候选数/验证数）。
- 试发后用只读探针查 `site_publish` 落库（`site='<站>' AND status='EXISTS'`），再 GET 详情页 `details.php?id=<tid>` 抽取 `<h1>` 与「基本信息」行逐项比对。**只信回读，不信提交成功的打印。**
- 麒麟实证：`9e107bcd9ba5` Shaggy → tid=473337，H1=`Shaggy - Intoxication 2007 FLAC`，年代 Earlier / 媒介 CD / 编码 FLAC / 地区 Other / 视频编码·分辨率·制作组 Other×3，与表单一致。

---

## 6. 阶段⑥ 开定时与上线观察

```bash
sed -i 's/^ENABLE_PUBLISH_XXX=false/ENABLE_PUBLISH_XXX=true/' data/.env
/usr/local/bin/docker compose up -d --force-recreate musopia    # ★ restart 不重载 .env
/usr/local/bin/docker exec opencd2musopia printenv ENABLE_PUBLISH_XXX
```

- daemon 横幅应出现 `publish_xxx 每 <间隔>s python publish.py --site <站> --yes --auto --limit <N>`；容器 healthy、心跳正常。
- 观察 1~2 轮：`site_publish` 的 FAILED 数与站点拒绝原因；若有失败会退避停轮，别直接重跑覆盖。
- 麒麟实证：`publish_hdkylin 每 1800s --limit 3`；手动一轮 3 条（473341/473342/473343）全成功，OST 类型正确映射 `媒介=32`，handoff 复制逐字节核对并排入 15 分钟延迟回加队列。

---

## 7. 陷阱清单（本次实证）

| 坑 | 症状 | 修法 |
|---|---|---|
| **残留 `Created` 改名容器**（见 `ERR-DOCKER-15`） | 反复 `--force-recreate` 报 `Conflict ... already in use` / `removal ... already in progress`；`.env` 改了容器 env 仍旧值；并发重建一度把主容器移除，daemon 停 | 重建**一次一条**、不排队不并发；先 `docker ps -a` 找改名残留 → `docker rm -f <残留名>` → `docker compose up -d musopia` 恢复；用 `printenv` 回读验收 |
| 开关提前为 true | 新镜像一部署就自动发种、绕过 dry-run | 接入期 `.env` 开关恒 false，试发+回读后才开 |
| 冒烟脚本开关清单漂移 | 构建成功但脚本判红，**镜像根本没 push** | `D_ENV` 补齐所有 `ENABLE_*`；修完前台重跑冒烟，再单独 `docker push` 三个 tag |
| 后台构建/推送结果不可见 | Hub 上 `latest` 推送时间还是旧的，NAS `pull` 报 manifest unknown | 回读 Hub tags API + 本地 RepoDigest；推送完成前不部署 |
| WSL `bash` 不可用 | `execvpe(/bin/bash) failed` | 用 `"C:\Program Files\Git\bin\bash.exe"` |
| Docker daemon 未启动 | `failed to connect ... dockerDesktopLinuxEngine` | 启动用户级 `Docker Desktop.exe`，`docker info` 确认 29.x 就绪 |
| 站规字段源数据不可得 | 想产出「中文名+原名」双语标题，库里没中文字段 | 只读探针取真实样本；不造数据，宁缺勿错 |
| **站规 ≠ 自动审核**（`ERR-SITE-11`） | 麒麟站规要求中文名前缀，自动审核却判「主标题包含中文」拒种（含 CJK 2/2 被拒、纯拉丁 2/2 通过） | 查种审脚本源码拿真实判据 `/[^\x00-\xff]/`；发布前按**最终主标题**加预检+兜底两道闸门；影响面按 `build_<站>_title()` 产出统计（按原始 `formatted_title` 会因「分轨」虚高到 97%，真值 42.9%）；规则冲突时请管理组人工复核，别自行删中文 |
| 多份标题实现漂移 | 各站标题形状不一致 | 新增站函数复用同一份 `_split_title` / `_parse_artist_album`，只改尾巴 |
| **站点删种后库里仍是假 EXISTS** | 管理组删种，`site_publish` 仍标 `EXISTS` + 旧 tid，库与站点口径不一致；查重与统计都失真 | 按 `tools/retract_missing_pages.py` 的**撤回**口径对齐：`UPDATE site_publish SET torrent_id='', status='UNKNOWN' WHERE site=? AND info_hash=? AND status='EXISTS'`（单事务、不删行、保留 `cover_url`/`synced_at`），写前只读预览、写后回读；`pending_*_direct()` 用 `NOT IN (SELECT … WHERE site=?)` ⇒ 行在就不重发 |
| **「审核不通过」误当成「已删种」** | 口述「删种了」就直接撤回，结果详情页 HTTP 200、H1 标「禁止」、下载链接还在；错误撤回后 tid 丢失，站点编辑/回改工具都找不到这两条（麒麟 2026-11-12 实证，已回滚） | ★ **口述状态一律现场 `GET details.php` 核验**：200 + 禁止 + 可下载 ⇒ 种子仍在，只是审核未通过 ⇒ 走**编辑**（`takeedit.php` 只覆盖 `name`，其余沿用表单现值）或回复管理组复核，**不撤回**；编辑不会自动复审，原因字段仍显示旧结论，需人工复核 |

---

## 8. 一页 Checklist

- [ ] `python tools/probe_site.py --site <站>` 跑完，发布页/规则页落到 `opencdhtml/`
- [ ] 表单全字段语义逐个核对（尤其 `processing_sel[4]` / `source_sel[4]` 互换）
- [ ] 站规五问有答案；源数据可得性用只读探针验证过
- [ ] `PLAN-<站>.md` 记录映射、地区方案、标题口径定案
- [ ] Cookie 已写 NAS `.env`（不打印值）；`ENABLE_PUBLISH_XXX=false`
- [ ] 适配层 + config/db/publish/daemon/.env.example + **冒烟脚本开关清单** 全部接线
- [ ] `py_compile` / 适配层自检 / `gen_counts.py` / `run.py --selftest` 全过（新增断言有阴性对照）
- [ ] 用户授权后 `build-push.sh`；冒烟通过；Hub tags API 回读确认推送成功
- [ ] NAS 点名 sha tag 拉取重建；digest / MD5 / 容器 env / healthy 四项验收
- [ ] dry-run 表单逐字段核对
- [ ] 单条试发成功 + `site_publish` 落 EXISTS + 详情页回读一致
- [ ] 开 `ENABLE_PUBLISH_XXX=true` + force-recreate；daemon 横幅确认阶段；观察失败率
- [ ] 站点侧删种/审核不通过的条目，把 `site_publish` 假 EXISTS 撤回为 `torrent_id=''` / `status='UNKNOWN'`（写前预览、写后回读）
- [ ] 代码与文档一并 commit
