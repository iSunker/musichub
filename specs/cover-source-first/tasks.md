# 任务清单：封面源站优先策略

> 对应 spec：[spec.md](./spec.md)
> 执行顺序即依赖顺序；每完成一项即更新状态。

## T1. 新增 `cover_cache` 表与读写方法

- [x] 文件：`musichub/routing/graph.py`
- 改动：
  - `RoutingGraphManager._init_db()` 追加 `cover_cache` 建表 + 索引（幂等）。
  - 新增方法：`get_cover_cache` / `upsert_cover_cache` / `get_site_cover_url` / `set_site_cover_url`。
- 验收：`python -m py_compile musichub/routing/graph.py`；用 `:memory:` 建表后写入并读回一致。

## T2. 重写 `CoverImageService` 主体

- [x] 文件：`musichub/metadata/cover_service.py`
- 改动：
  - `__init__(cache_dir=None, db_path=None)`：延迟 sqlite 连接，失败软降级。
  - `is_valid_image_url(url)`：HTTP 200 + `image/*` + 魔数 + 15MB 上限，结果 TTL 7 天缓存。
  - `find_local_cover`：移植 `COVER_PATTERNS` 分级匹配 + 最大图兜底 + 子目录封面。
  - `_chevereto_upload(image_path, api_url, api_key)`：抽出通用实现；`upload_picgo` / 新增 `upload_pter_img` 复用。
  - `upload_lsky`：token 链、401 重试、超 5MB 压图、`LAST_LSKY_ERROR` 分级。
  - `get_site_cover(...)`：源站优先 → 站点路由 → 失效才本地补图 → 落库；绝不写空 URL。
- 验收：`python -m py_compile`；离线冒烟脚本可跑通。

## T3. 接入发种链路

- [x] 文件：`musichub/adapters/nexusphp_base.py`
- 改动：`build_body` 调 `get_site_cover` 时补传 `source_site` / `source_tid` / `source_name`，并记录封面来源日志。
- 验收：`python -m py_compile`；构造 `CanonicalMusicItem` 调 `build_body`（mock 图床）不抛异常。

## T4. 接入编辑工具

- [x] 文件：`musichub/onsite/editor.py`、`musichub/tools/edit_onsite.py`
- 改动：
  - `resolve_cover_url` 统一走新 `get_site_cover`，新增 `source_name` 参数；显式 URL hint 需校验有效才复用。
  - `edit_onsite.py` 补封面时传入专辑标题。
- 验收：`--check` 与 `--fix-cover` 的 dry-run 分支不回归。

## T5. 单测

- [x] 文件：`musichub/tests/test_core.py`（新增 `TestCoverPolicy`）
- 用例：
  1. 源链有效 ⇒ 返回源链，`upload_*` 未被调用。
  2. 源链失效 + 本地有图 ⇒ 调对应上传并落库。
  3. 无源无本地 ⇒ `("", reason)`，DB 不写空 URL。
  4. 路由：crabpt→lsky、pterclub+key→pter、其余→picgo。
  5. 同 album_key + target_site 第二次命中 DB，不再上传。
  6. Lsky 单条级失败 ⇒ 返回空并写 reason，不抛异常。
- 验收：`$env:PYTHONPATH="."; python musichub/tests/test_core.py` 全绿。

## T6. 推送 NAS 与生产只读验证

- [x] 操作：
  - 将改动文件推送到 NAS `/volume2/docker_ssd/musichub/src/musichub/`（绑定挂载即时生效）。
  - 两端 `sha256sum` 比对。
  - 容器内只读体检近 14 天已发种封面状态（`--scan --days 14 --check`，不写站点）。
- 验收：sha256 一致；只读体检输出可解释（有效源链 / 需补图 / 真无图）。

## T7.（需用户确认）图床环境变量注入

- [x] 前置：东八区 10:00 后（黄金窗结束）。**2026-11-21 08:19 用户明确要求「现在重建」，已越过黄金窗执行**。
- 已就位：NAS `data/cover.env`（600，含 `PICGO_API_KEY` / `LSKY_API_URL` / `LSKY_EMAIL` / `LSKY_PASSWORD` / `LSKY_TOKEN`）+ `docker-compose.override.yml`（`env_file: ./data/cover.env`），`docker compose config -q` 通过；无 `PTER_IMG_*`（猫站按有效源链/回退处理）。
- 操作：把上述键注入 `musichub` 容器环境，`docker compose up -d --force-recreate musichub`。
- 验收：容器内 `env | grep` 仅确认键存在（**不打印值**）；发布样本自动带封面。
