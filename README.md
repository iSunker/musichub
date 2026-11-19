# MusicHub 3.0

PT 音乐矩阵自动化中枢系统（原从 `opencd2others` 独立分离而来）。

## 核心功能
- **全自动多站点发种**：乌托邦 (Musopia)、蟹黄堡 (CrabPT)、不可躺 (TangPT)、星湾 (XingWan)、牧雪阁 (MuXueGe)、红豆麒麟 (HDKylin)、PterClub。
- **两阶段延迟回加 (delayed_readd)**：完美规避站点差异导致的哈希不匹配与防偷跑。
- **存储背压与智能交棒**：自动监控磁盘红黄绿水位，实现多池轮转（`music-pool1~5` / `music1~3`）与安全 GC。
- **断种自动巡检 (Seed Patrol)**：定期巡检保种状态与 tracker 健康度。

## 部署与运行
详见 [AGENTS.md](AGENTS.md)。
