# 方案计划：新增 3 大新站（LemonHD / UHDDolby / HDTime）适配发种与 Vertex 3016 抓取配套流水线

> **文档定位**：本计划针对 PT 音乐自动化中枢（`musichub`）与抓取前哨（`vertex`）进行能力扩展，覆盖站点：
> 1. **LemonHD (柠檬)**：`https://lemonhd.net`
> 2. **UHDDolby (杜比)**：`https://www.uhddolby.com`
> 3. **HDTime (高清时间)**：`https://hdtime.org`
>
> 遵循 `pt-site-publish`、`nexusphp-adapter`、`qb-cluster` 与 `nas-ops` 铁律，实现“发种适配 + 防环联动 + 3016 抓取 + 延迟回加”完整闭环。

---

## 一、 现场真实契约实测总结 (现场取证核准)

| 维度 / 契约 | LemonHD (柠檬) | UHDDolby (杜比) | HDTime (高清时间) |
|---|---|---|---|
| **网络通路** | 必须走 NAS 本地代理 `http://192.168.0.7:7894` (直连阻断) | 直连光猫 (IPv4，`trust_env=False`) | 直连光猫 (IPv4，`trust_env=False`) |
| **Cookie 鉴权** | CookieCloud 单项 `c_secure_pass` 即有效 | 五件套 (`c_secure_uid/pass/ssl/tracker_ssl`) | 五件套 (`c_secure_uid/pass/ssl/tracker_ssl`) |
| **音乐分类 ID** | `type=402` (Muisc 音乐) | `type=408` (HQ Audio 音乐) | `type=408` (音乐) |
| **二级选择器结构** | `medium_sel[4]`, `audiocodec_sel[4]`, `codec_sel[4]`, `team_sel[4]` | `medium_sel`, `audiocodec_sel`, `codec_sel`, `standard_sel`, `team_sel` (无索引数组) | `medium_sel[4]`, `audiocodec_sel[4]`, `codec_sel[4]`, `team_sel[4]` |
| **匿名发布支持** | **不支持** (页面无 `uplver` 复选框，账号具名发布) | **支持** (`uplver="yes"`) | **支持** (`uplver="yes"`) |
| **Vertex 适配器** | 官方内置通用或需轻量封装 | 官方已收录或轻量封装 | `vertex/build/adapters/HDTime.js` 已存在，可复用 |

---

## 二、 核心架构改造与配套设计

### 1. MusicHub 发种适配器矩阵 (`musichub/adapters/sites/`)
新增三个站点的专属 Adapter 类，继承自 `NexusPHPBaseAdapter`：
1. `musichub/adapters/sites/lemonhd.py` (`LemonHDAdapter`)
   - 注入 `proxies={"http": "http://192.168.0.7:7894", "https": "http://192.168.0.7:7894"}`；
   - 映射 `type=402`，解析 `medium_sel[4]` (CD/WEB-DL) 与 `audiocodec_sel[4]` (FLAC/APE/WAV/MP3/Other)；
   - 具名发布（不附带 `uplver`）。
2. `musichub/adapters/sites/uhddolby.py` (`UHDDolbyAdapter`)
   - 映射 `type=408`，解析常规 `medium_sel` 与 `audiocodec_sel`；
   - 支持 `uplver="yes"` 匿名发布。
3. `musichub/adapters/sites/hdtime.py` (`HDTimeAdapter`)
   - 映射 `type=408`，解析 `*_sel[4]` 系列字段；
   - 支持 `uplver="yes"` 匿名发布。

### 2. 发布引擎与账本防环联动 (`musichub/routing/graph.py` & `publisher/`)
- **多对多发布矩阵扩容**：将 `lemonhd`、`uhddolby`、`hdtime` 纳入 `site_publish_graph`、`StockMatrixPump` 及调度器；
- **方案 C 账本同步**：在 `export_vertex_blocklist()` 中追加三个新站的 key (`lemonhd`, `uhddolby`, `hdtime`)，发种成功即向 `/vertex/data/published_music_ids.json` 写入已发 TID，从源头切断自家发布种重新回流至 3016。

### 3. Vertex 3016 抓取配套与规则对齐 (`vertex/data/rule/rss/30160002.json`)
- 更新 3016 音乐专属过滤规则中的 `siteKeys` 识别列表：加入 `lemonhd`、`uhddolby`、`hdtime`；
- 扩充防线 2 具名作者防环列表：因 LemonHD 亦为具名发布，追加作者拦截兜底。

### 4. 3010 发种端防偷跑与安全注入固化
- 将 3010 实例配置固化 `Session\AddTorrentStopped=true`，并更新 `engine.py` 制种覆写逻辑，彻底规避 `stalledDL 21.6%`。

---

## 三、 执行计划与检验步骤

1. **Phase 1: 编写 3 站适配器与单元测试**
   - 落地 `lemonhd.py`、`uhddolby.py`、`hdtime.py` 并注册至 `musichub/adapters/sites/__init__.py`；
   - 编写 `tests/test_new_adapters.py`，验证字段组装与前置校验逻辑。
2. **Phase 2: 联动更新 Vertex 规则与防环账本**
   - 更新 `graph.py` 的 blocklist 导出站点集合；
   - 更新生产环境 `30160002.json` 规则并热更验证。
3. **Phase 3: NAS 热更同步与 Dry-Run 离线验证**
   - 远端执行 `--dry-run` 测试各站点连接性、CookieCloud 鉴权与表单组装；
   - 在安全窗口期内进行受控发种与 3016 RSS 联调验证。
