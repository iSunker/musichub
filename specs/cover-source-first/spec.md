# 封面策略：源站优先 + 失效才本地补图 + 落库复用 + 站点专用图床路由

> 状态：待批准（Spec Mode）
> 日期：2026-11-21
> 范围：`musichub` 子项目（生产容器 `musichub`，代码经 NAS 绑定目录即时生效）
> 依据：用户原话「只有1种情况需要自己上传图床，就是源站没有图片，或者源站图链失效需要补图，那就扫描本地种子，把图片上传到图床，并把数据写入到数据库，以便给别站发种时填入，另外站点特殊规定图床，就按照站点规定来。你把这部分内容从父项目读取过来。」

---

## 1. 目标

1. **源站有图且有效 ⇒ 直接复用源站图链**，不下载、不上传、不消耗图床配额。
2. **仅当源站无图或图链失效** 时：扫描本地种子目录找图 → 按目标站规定上传图床 → **写入数据库**。
3. 同一专辑给**其他站**发种时，优先查库命中已上传的图链，避免重复上传。
4. **站点专用图床规则优先**：`crabpt` 必须 Lsky Pro（`img.crabpt.vip`）；`pterclub` 配置了 `PTER_IMG_*` 时走猫站图床；其余站走 PicGo。
5. 任何失败**绝不写空 URL**（避免抹掉站点已有封面），失败原因落 `reason`。

## 2. 现状与差距

| 位置 | 现状 | 差距 |
| --- | --- | --- |
| `musichub/metadata/cover_service.py` | `get_site_cover` 对任何 `http(s)` 源链**无条件复用**；无有效性探测；无落库；`find_local_cover` 规则弱（仅名字包含） | 源链已 404 仍会填到站点，且没有跨站复用记录 |
| `musichub/adapters/nexusphp_base.py::build_body` | 调 `get_site_cover(target, item.cover_url, item.local_path)`，未传源站/TID/专辑名 | 无法建立缓存键，也无法回源查详情 |
| `musichub/onsite/editor.py::resolve_cover_url` | 仅 `crabpt` 走专用图床；任何 `http` hint 直接返回 | 补封面时同样不复用「已落库/有效源链」策略 |
| `musichub/routing/graph.py` | 有 `site_publish_graph` / `publish_lineage` / `source_tasks` | **无封面缓存表**，无法按专辑+站点取图链 |
| 父项目 `opencd2others` | `assets.find_cover` 分级匹配、`cover_fetch` 魔数校验/URL sha1 缓存/本地优先/早停、`publish._crabpt_cover`/`_pter_cover` 站点路由与失败分级、`db.site_cover_url_of` 每站一份封面 | 待移植 |

## 3. 设计

### 3.1 新增封面缓存表（`musichub/routing/graph.py`）

在 `RoutingGraphManager._init_db()` 中新增：

```sql
CREATE TABLE IF NOT EXISTS cover_cache (
    album_key TEXT PRIMARY KEY,          -- {source_site}:{source_tid} 或 {source_site}:album:{sha1(name)[:12]}
    source_site TEXT DEFAULT '',
    source_tid TEXT DEFAULT '',
    source_name TEXT DEFAULT '',
    source_cover_url TEXT DEFAULT '',
    source_cover_status TEXT DEFAULT '', -- OK / BROKEN / MISSING
    source_cover_checked_at REAL DEFAULT 0,
    local_dir TEXT DEFAULT '',
    local_image TEXT DEFAULT '',
    site_covers_json TEXT DEFAULT '{}',  -- {"crabpt": "...", "pterclub": "...", "picgo": "..."}
    status TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cover_cache_source ON cover_cache(source_site, source_tid);
```

新增方法（同一类，均加 `self._lock`）：

- `get_cover_cache(album_key) -> Dict[str, Any]`（无记录返回 `{}`）
- `upsert_cover_cache(album_key, **fields)`（只覆盖传入字段，保留 `site_covers_json` 合并语义）
- `get_site_cover_url(album_key, target_site) -> str`
- `set_site_cover_url(album_key, target_site, url)`（仅 URL 非空才写；同时更新 `updated_at`）

> 说明：不改动 `site_publish_graph` / `publish_lineage` 的既有语义。封面缓存是**独立**的「专辑 × 站点」映射。

### 3.2 重写 `musichub/metadata/cover_service.py`

#### 构造

```python
class CoverImageService:
    def __init__(self, cache_dir=None, db_path=None):
        ...
        self._db_path = db_path          # 显式传入优先；否则延迟从 core.config 取
        self._conn = None                # 延迟连接，失败软降级（仅不落库）
```

- 生产默认 `musichub/core/config.py` 的 `db_path`（`musichub/data/musichub.db`）。
- DB 打开失败只记 warning，封面逻辑继续但**不落库**。
- 单元测试传 `db_path=<tmpfile>`，或传 `:memory:` 时自行建表（与图 SQL 同源）。

#### 新增/改造函数

1. `is_valid_image_url(url, *, timeout=20) -> Tuple[bool, str]`
   - 仅 `http/https`；`requests.get(stream=True, allow_redirects=True)`。
   - 校验：HTTP 200、`Content-Type` 以 `image/` 开头（缺失时放行到魔数）、首块魔数属于 JPEG/PNG/GIF/WEBP、总大小 ≤ `MAX_COVER_BYTES`(15MB)。
   - 结果写入 `data/covers/_url_probe.json`，TTL 7 天（避免每轮发种重复探测同一图链）。
   - 返回 `(True, "OK")` 或 `(False, "HTTP 404" / "非图片(text/html)" / "超时" / "超过 15MB")`。

2. `find_local_cover(folder_path) -> Optional[str]`（移植父项目 `assets.find_cover`）
   - 分级正则：`^(cover|folder|front|frontal)\.(jpg|jpeg|png|webp)$` → `(cover|folder|front)` → `(artwork|poster|scan|booklet|case|inlay|box)` → `(a|album|cd|disc)\d*$`。
   - 同级取最大文件；全部不匹配时取目录内最大图。
   - 跳过 0 字节、>15MB、非图片扩展名。
   - 顶层无图时调用 `_find_nested_cover(folder)`（一层子目录里的 front/cover/folder，合辑用）。

3. `get_site_cover(target_site, source_cover_url="", local_dir=None, source_site="", source_tid="", source_name="", cached_site_covers=None) -> Tuple[str, str]`
   执行顺序（核心策略）：

   ```
   ① album_key = f"{source_site}:{source_tid}" 或 f"{source_site}:album:{sha1(source_name)[:12]}"
   ② 兼容旧参数 cached_site_covers[target] 非空 ⇒ 直接返回
   ③ DB get_site_cover_url(album_key, target) 非空 ⇒ 直接返回「复用已落库封面」
   ④ 校验源链（有则 is_valid_image_url）并 upsert source_cover_status
   ⑤ 按站点路由：
      crabpt:
        - 源链 host 是 Lsky 且有效 ⇒ 复用
        - 否则 local_img = find_local_cover(local_dir) 或 download(source_url)
        - upload_lsky(local_img) → 成功 set_site_cover_url(album_key,"crabpt",url)
      pterclub:
        - PTER_IMG_API_KEY 存在且源链非 pter 图床 ⇒ local_img → upload_pter_img → 落库
        - 未配置 key 且源链有效 ⇒ 复用
        - 配置了但上传失败且源链有效 ⇒ 回退源链
      OTHER:
        - 源链有效 ⇒ 直接复用（不下载不上传）
        - 源链无效/缺失 ⇒ local_img → upload_picgo → 落库
   ⑥ 全失败 ⇒ ("", 明确原因)，并 upsert reason；绝不写空 URL
   ```

   `local_img` 顺序：`local_dir` 扫描 → 源链下载到 `data/covers/<sha1(url)[:12]><ext>`（复用 `_download_and_cache`，补魔数校验与 15MB 上限）。

4. `upload_pter_img(file_path) -> Optional[str]`
   - 抽出通用 `_chevereto_upload(image_path, api_url, api_key, retries=2)`：`X-API-Key` 头、字段 `source`、URL 取 `image.url` / `result.url`、400 duplicated 回收 URL。
   - `upload_picgo` = `_chevereto_upload(PICGO_UPLOAD_URL || https://www.picgo.net/api/1/upload, PICGO_API_KEY)`，保留 sha1 → URL 缓存。
   - `upload_pter_img` = `_chevereto_upload(PTER_IMG_UPLOAD_URL || https://img.pterclub.net/api/1/upload, PTER_IMG_API_KEY)`。
   - 未配 key ⇒ 返回 `None`（调用方按站规回退）。

5. `upload_lsky(file_path)`（对齐父项目）
   - token 链：`LSKY_TOKEN` → `POST {LSKY_API_URL}/tokens`（`LSKY_EMAIL`/`LSKY_PASSWORD`）→ 进程内缓存；401/403 时重取 token 重试一次。
   - 上传：`Authorization: Bearer`、字段 `file`、端点 `{LSKY_API_URL}/upload`、URL 取 `data.links.url`。
   - 超 `LSKY_MAX_BYTES`（默认 5MB）：Pillow 压缩到最长边 `LSKY_SHRINK_MAX_DIM`(2400)，落 `.lsky.jpg` 再传；Pillow 缺失则按单条级失败处理。
   - `LAST_LSKY_ERROR` + `lsky_error_is_item_level()`：单条级（超限/格式）返回 `None` 但不停整轮；设施级（401/429/网络）同样返回 `None`，由调用方决定停轮（发种侧当前只需"本条无封面"）。
   - 凭据只读环境变量，不写日志、不落盘。

### 3.3 接入发种（`musichub/adapters/nexusphp_base.py`）

`build_body` 内改为：

```python
cover_url, cover_why = self.cover_service.get_site_cover(
    target_site=self.site_name,
    source_cover_url=item.cover_url,
    local_dir=item.local_path,
    source_site=item.origin_site,
    source_tid=item.origin_torrent_id,
    source_name=item.title,
)
logger.info(f"[{self.site_name}] 封面 ← {cover_why}")
```

不改 `build_body` 其它字段与签名；封面为空时维持现有「不输出 `[img]`」行为。

### 3.4 接入编辑工具（`musichub/onsite/editor.py::resolve_cover_url`）

- 统一调用新 `get_site_cover(...)`，传入 `source_site`/`source_tid`/`source_name`（新增可选参数 `source_name`，由 `edit_onsite.py` 把 `_detail_title` 传入）。
- 删除「任何 `http` hint 无条件返回」分支：hint 为 URL 时先用 `is_valid_image_url` 校验，有效才复用，否则继续走本地补图。
- `crabpt` 特判保留但简化为调用 `get_site_cover("crabpt", ...)`（路由已内置）。

### 3.5 站点图床路由表（单一事实源）

复用 `musichub/onsite/site_rules.py`：

| 站点 | `cover_host_required` | 路由 |
| --- | --- | --- |
| `crabpt` | `img.crabpt.vip` | Lsky Pro |
| `pterclub` | `""` | `PTER_IMG_API_KEY` 存在 → 猫站图床；否则有效公网链 / PicGo |
| 其余 9 站 | `""` | 有效源链直用；失效才 PicGo |

`CoverImageService` 增加 `_site_cover_mode(target_site)`：读 `site_rules.get_rule()`，未登记站点按 `OTHER` 处理（不抛异常，仅 warning）。

## 4. 非目标

- 不改 `site_publish_graph` / `publish_lineage` / `source_tasks` 的既有字段与语义。
- 不重新实现详情页解析（源站无图时用 `collector/source_detail.py` 现有能力，已在编辑链路中）。
- 不新增图床服务商；不改变 `data/_uploaded_urls.json` 的既有格式与用途。
- 不扫描 `music5`（禁删红线池）。

## 5. 风险与边界

1. **图床凭据未进容器**（当前 `PICGO_API_KEY` / `LSKY_*` / `PTER_IMG_*` 均未注入）：crabpt 会返回空（宁可不发也不跨站透传），其他站在源链有效时仍可正常发种。需要 10:00 后加环境变量并 `--force-recreate`（需用户确认）。
2. **源链探测成本**：默认每张图链 1 次 HEAD/GET，7 天缓存；失败即视为 BRONK，避免反复探测死链。
3. **`pterclub` 站规**：若未配 `PTER_IMG_*` 且源链是 PicGo，直接复用（与父项目 `_pter_cover` 一致）。
4. **Lsky 重复上传不可回收**：先查 `cover_cache.site_covers_json`，命中即不再传。
5. **DB 写失败**：降级为「本次可用但不落库」，记 warning，不影响发种。

## 6. 验证步骤

```powershell
# 1. 语法
python -m py_compile musichub/metadata/cover_service.py `
  musichub/routing/graph.py `
  musichub/adapters/nexusphp_base.py `
  musichub/onsite/editor.py `
  musichub/tools/edit_onsite.py

# 2. 单测
$env:PYTHONPATH="."
python musichub/tests/test_core.py

# 3. 离线冒烟（本机临时库 + 样本目录，不联网、不上传）
python musichub/tools/edit_onsite.py --print-mapping
python musichub/tools/edit_onsite.py --site lemonhd --tid 48704 --check
```

生产验证（推 NAS 后）：

```bash
# 容器内只读体检：源链是否有效 / 是否需要补图
docker exec musichub python -m musichub.tools.edit_onsite --scan --days 14 --check
```

## 7. 需要用户批准的事项

1. 本 spec 的封面策略与站点路由是否符合预期。
2. 是否允许在 `musichub.db` 新增 `cover_cache` 表（生产库 DDL，幂等）。
3. 10:00 后是否执行「注入图床环境变量 + `--force-recreate`」（黄金窗内不执行）。
