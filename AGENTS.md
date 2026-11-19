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

---

## 1. 基础设施与端口映射

- **发种端 QB**：`qbit-publish`（端口 `3010`，host 网络）
- **下载缓冲端 QB**：`qbit-musicdownload`（端口 `3016`）
- **目标音乐池 QB**：
  - 传统音乐池：`music1` (3220), `music2` (3240), `music3` (3260)
  - 扩展保种池：`music-pool1` (3210), `music-pool2` (3230), `music-pool3` (3250), `music-pool4` (3270), `music-pool5` (3280)
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
4. **硬链接与目录保护**：
   - 任何清理操作必须避开系统保留目录（`incomplete`, `@eaDir`, `.RecycleBin`）与未纳管池（如 `music5` 禁删红线）。

---

## 3. 本地自检与验证

动代码前后执行语法检查与单元测试：
```powershell
# 1. 语法检查
python -m py_compile musichub/daemon.py

# 2. 核心单元测试
$env:PYTHONPATH="."
python musichub/tests/test_core.py
```
