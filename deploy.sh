#!/usr/bin/env bash
# =====================================================================
# 本地 → NAS 生产 的白名单同步（opencd2others）
#
#   本地: D:\Projects\opencd2others\zhuanzhong（默认 = 脚本所在目录，SRC 可覆盖）
#   生产: NAS 上的 compose 目录（DST 指定，或写进 .deploy.nasrc）
#
# 为什么需要它：
#   NAS 的 SSH 没开，本机不能在 NAS 上执行命令，写只能走 SMB。
#   所以这个脚本**只负责把文件放过去** —— 重建容器那一步仍然必须在
#   NAS 上手动做（结尾会把命令打出来）。别指望它一条龙。
#
# 原则：
#   1. 白名单 —— 只覆盖 FILES 里列出的文件，其余一律不碰。
#      白名单 = Dockerfile 的 COPY 行 + Dockerfile/docker-compose.yml 本身。
#      多一个不用同步，少一个容器里就缺模块（下面有自检会核对）。
#   2. 绝不覆盖生产独有的运行时状态与密钥：
#      .env（真实 Cookie / qB 密码 / 图床 Key）、以及 `data/` 下的全部状态
#      （data.db + 254 条记录与站点 ID、daemon_state.json 调度进度、
#        daemon_heartbeat、readd_queue.json、lsky_token.json、
#        output_torrents/、covers/、raw_html/）。
#      它们不在白名单里，动不到；脚本还会断言一遍。
#   3. 覆盖前备份到本地 .deploy-backup/<时间戳>/，可一键回滚。
#      备份放**本地**：不污染 NAS 上的 docker build 上下文，也不会因为
#      写 NAS 中途失败而把备份一起丢掉。
#   4. 默认 dry-run 只打 diff，--apply 才写入。
#
# 用法:
#   bash deploy.sh                    预览差异（默认）
#   bash deploy.sh --apply            写入（自动先备份）
#   bash deploy.sh --rollback         回滚到最近一次备份
#   临时指定目标（优先级高于 .deploy.nasrc）:
#   DST=//iSunker-DS423/docker_ssd/opencd2others bash deploy.sh
# =====================================================================
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ★ 命令行/环境变量必须**优先于** .deploy.nasrc。
#   reseed-toolkit 2026-09-12 实测踩过：`DST=/tmp/sandbox bash deploy.sh --apply`
#   会被 .nasrc 里的 DST 覆盖掉，于是"在沙箱里演练"实际打到了**生产 NAS**。
#   想拿临时目录做演练却误伤生产，这个口子必须堵上：先存下外部给的值，
#   source 完再恢复。（.deploy.nasrc 的定位是**默认值**，不是**强制值**。）
_DST_FROM_ENV="${DST:-}"
# shellcheck source=/dev/null
[[ -f "$SELF_DIR/.deploy.nasrc" ]] && source "$SELF_DIR/.deploy.nasrc"

SRC="${SRC:-$SELF_DIR}"
DST="${_DST_FROM_ENV:-${DST:-}}"
DST="${DST:?请设置 DST（NAS 上的 compose 目录），或写进 $SELF_DIR/.deploy.nasrc 一行：DST=//iSunker-DS423/docker_ssd/opencd2others}"
BACKUP_ROOT="$SRC/.deploy-backup"

# 白名单。★ 这份清单要和 Dockerfile 的 COPY 行保持一致 ——
# 少了哪个，容器里就没有那个模块（ImportError 要到实际用到才发现）。
# 文件末尾的自检会拿 Dockerfile 反过来核对，漏了直接拒绝部署。
# deploy.sh 自己不在里面：它是本地开发工具，不需要上 NAS。
FILES=(
  config.py db.py descr.py assets.py musopia.py
  # ★ 统一网络出口（2026-10-02 加）—— config.py `import net`，缺失即容器 ImportError。
  #   强制 IPv4 解析，绕开"个别 Cloudflare IPv6 边缘 IP 不可达"的黑洞（见 net.py 文件头）。
  net.py
  run.py publish.py update_metadata.py sync_site.py
  backfill_covers.py check_db.py backfill_covers_opencd.py
  daemon.py healthcheck.py
  # ★★ 2026-10-04：`opencd_parse/rate/log/map/gate/ingest` 六个模块已合并成
  #   单文件 `opencd.py`（CLI 子命令——用法见 README / .claude/RULES.md）。
  #   同步与 Dockerfile 的 COPY 行保持一致（本脚本末尾会反查，漏了就拒绝部署）。
  crawl.py opencd.py cover_fetch.py
  topup_opencd_fields.py observe_custom_fields.py
  sync_opencd_tid.py readiness_probe.py qb_probe.py flip_status.py
  # ★ 蟹黄堡站适配层（2026-09-24 加）—— publish.py 会 import 它，
  #   不进白名单就是"跑到发蟹黄堡那一步才 ImportError"（本注释上方那条纪律的场景）。
  crabpt_site.py
  # ★ 延迟回加队列（2026-10-03 加）—— publish.py 与 daemon.py **都** import 它。
  #   不进白名单 ⇒ 容器里没有它 ⇒ import 即 ImportError（上面 crabpt_site 同款场景）。
  readd_queue.py
  # ★★ qB 客户端工厂（2026-10-04 加）—— run/publish/daemon/qb_probe 与 6 个 tools
  #   **都** import 它（`import qb_client`）。不进白名单 ⇒ 容器里没有它 ⇒ **import 即
  #   ImportError**（`crabpt_site` / `readd_queue` 同款场景，`ERR-QB-08` 的修法载体）。
  #   ★ 它是"key 优先 / 回退账号密码"的唯一构造点，缺它整个回加路全挂。
  qb_client.py
  # ★★ `tools/` 目录（2026-09-26 加）—— Dockerfile 里是 `COPY tools/ ./tools/`。
  #   为什么整个目录同步而不是逐个列文件：`tools/` 下的脚本是**按需在容器里跑**的
  #   （`docker compose run --rm tools python tools/xxx.py`），逐个列必然漏
  #   ——「白名单靠枚举维护、加文件时必漏」正是本脚本自检想防的那个形状，
  #   只不过这次漏的是**整个目录**（连自检都看不见，见下面 `dockerfile_copy_dirs`）。
  #   ★ 只同步目录本身；`tools/doc-audit/` 等被 `.dockerignore` 排除的**不进上下文**，
  #     但也**不该上 NAS**（纯本机审计工具）。
  tools
  Dockerfile docker-compose.yml
  .dockerignore
  README.md
  .env.example
)

# ★ 为什么 `.dockerignore` **必须**在白名单里（2026-09-22 加）：
#   它只对**构建上下文**生效，而上下文是在 **NAS 的 compose 目录**里算的
#   ⇒ 它在生产目录里不存在的话，等于没有。本机有、NAS 没有是最容易漏的一格
#   （和 Dockerfile 被漏是同一个形状）。
#   ★ 它排除掉**全部 `.env*` 历史备份**（那些里面有真凭据，没有它就会被传进
#     构建上下文）与 `data.db` 等运行时状态 —— 实测上下文 174.35kB → 724B。
#     **凭据外溢**才是它存在的理由。
#   ★ 它**不**负责消那行 `buildx: git was not found` WARN —— 那条归因
#     （"`context` 里有 `.git`"）**已被实测推翻**：`.git` 排掉之后上下文从
#     174kB 掉到 724B，WARN 一字不变 ⇒ 它是 buildx 在没装 git 的机器上的固有
#     行为，只影响"镜像少几条 commit 标签"这类元数据。**接受它**，别再去改代码
#     或加开关。（详见 `.dockerignore` 文件头 ① 与 ERRATA.md 的 ERR-DOCKER-07。）
#   ★ 注意它**不是**白名单文件（不进镜像）—— 它是给构建引擎读的元数据，
#     与 Dockerfile / docker-compose.yml 同类。

# 生产独有，绝不同步。写在这里是为了把"动不到"变成一条**可检查的断言**，
# 而不是靠每次读代码时自己想起来。
# ★ 2026-10-05：全部状态收进 `data/` 子目录（compose 里 `${STATE_DIR:-./data}:/data`）
#   ⇒ 断言改用 `data/` 前缀。**别只改一半** —— 前缀对不上就等于断言失效
#   （`NEVER` 是"白名单里不许出现它"的判据，写成旧路径会让它永远为真却什么都没防住）。
NEVER=(
  .env
  data/data.db data/data.db-journal data/data.db-wal data/data.db-shm
  data/daemon_state.json data/daemon_heartbeat data/daemon_env.json
  data/readd_queue.json data/lsky_token.json
  data/output_torrents data/covers data/raw_html
)

MODE="${1:---dry-run}"
case "$MODE" in
  --apply|--rollback|--dry-run) ;;
  *) echo "未知参数: $MODE  (可用: --apply | --rollback | --dry-run)" >&2; exit 2 ;;
esac

[[ -d "$SRC" ]] || { echo "✗ 本地目录不存在: $SRC" >&2; exit 1; }
[[ -d "$DST" ]] || { echo "✗ 生产目录不可达（检查 SMB 连接 / NAS 是否在线）: $DST" >&2; exit 1; }

# ---------- 断言：白名单里没有生产独有文件 ----------
for n in "${NEVER[@]}"; do
  for p in "${FILES[@]}"; do
    if [[ "$p" == "$n" ]]; then
      echo "✗ 白名单里混进了生产独有文件: $p（会覆盖生产状态/密钥）" >&2
      exit 1
    fi
  done
done

# ---------- 自检：白名单是否覆盖了 Dockerfile 的 COPY 行 ----------
# 冲着 reseed-toolkit 踩过的坑来的：那边 Dockerfile 是
# `COPY orchestrator/ /app/orchestrator/`（拷整个目录），白名单按文件列，
# 漏了 state.py —— 平时看不出来（main.py 以前不导入它），一加新子命令就是
# **模块级 ImportError**，连默认的 preflight 都一起打挂。
# 所以这里反过来做：从 Dockerfile 解析出实际要 COPY 的文件，逐个核对白名单。
# ★ 教训："这个文件需不需要同步"要按**谁拷它**去数，不能凭印象。
dockerfile_copy_files() {
  # 先把 \ 续行拼成一行，再取 COPY 指令的内容，最后只留 .py
  awk '{ while (sub(/\\[[:space:]]*$/, "")) { getline nxt; $0 = $0 " " nxt } ; print }' "$SRC/Dockerfile" \
    | awk '/^COPY /{ $1=""; print }' \
    | tr ' ' '\n' | grep -E '\.py$' || true
}

# ★★ 新增（2026-09-26）：`COPY <dir>/ <dir>/` 这种**整目录**拷贝。
#   上面那个函数只 grep `\.py$` ⇒ **整目录 COPY 它完全看不见**
#   —— 而这正是本自检自己注释里警告过的形状（reseed-toolkit 那次漏 `state.py`）。
#   ⇒ 把"目录"也解析出来核对：源的目录名必须出现在 FILES 里。
#   判据：**`COPY` 的目标可以是一个目录，此时"哪些文件需要同步"没有静态答案**
#     ⇒ 只能要求白名单里有那个**目录**，否则就会重演"容器里少文件"。
dockerfile_copy_dirs() {
  awk '{ while (sub(/\\[[:space:]]*$/, "")) { getline nxt; $0 = $0 " " nxt } ; print }' "$SRC/Dockerfile" \
    | awk '/^COPY /{ $1=""; print }' \
    | tr ' ' '\n' \
    | grep -vE '^$' \
    | grep -vE '^\.?/' \
    | grep -vE '^\.\.$|^\.$' \
    | grep -E '/$' | sed 's#/$##' | sort -u || true
}

missing=()
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  ok=0
  for p in "${FILES[@]}"; do
    [[ "$p" == "$f" ]] && { ok=1; break; }
  done
  [[ "$ok" -eq 1 ]] || missing+=("$f")
done < <(dockerfile_copy_files)

# ★★ 目录型 COPY 的核对（2026-09-26 加）—— 见 `dockerfile_copy_dirs` 上方那段。
while IFS= read -r d; do
  [[ -z "$d" ]] && continue
  ok=0
  for p in "${FILES[@]}"; do
    [[ "$p" == "$d" ]] && { ok=1; break; }
  done
  [[ "$ok" -eq 1 ]] || missing+=("$d/  （整目录 COPY）")
done < <(dockerfile_copy_dirs)

if [[ ${#missing[@]} -gt 0 ]]; then
  echo "✗ 白名单漏了 Dockerfile 要 COPY 的文件：" >&2
  for f in "${missing[@]}"; do echo "    $f" >&2; done
  echo "  容器里会缺这些模块。补进 deploy.sh 的 FILES 再部署。" >&2
  exit 1
fi

# ---------- 回滚 ----------
if [[ "$MODE" == "--rollback" ]]; then
  LATEST="$(ls -1 "$BACKUP_ROOT" 2>/dev/null | sort | tail -1 || true)"
  [[ -n "${LATEST:-}" ]] || { echo "✗ 没有任何备份: $BACKUP_ROOT" >&2; exit 1; }
  echo "→ 从备份回滚: $LATEST"
  ( cd "$BACKUP_ROOT/$LATEST"
    find . -type f | while read -r f; do
      f="${f#./}"
      echo "  恢复 $f"
      mkdir -p "$DST/$(dirname "$f")"
      # 同样走原子替换 —— 回滚也可能撞上批次运行中
      cp -p "$f" "$DST/$f.new.$$"
      mv -f "$DST/$f.new.$$" "$DST/$f"
    done )
  echo "✓ 回滚完成。NAS 上记得: cd <compose目录> && sudo docker compose up -d --build"
  exit 0
fi

# ---------- 算差异 ----------
# ★★ 白名单里可以有**目录**（如 `tools`）—— 见 FILES 里那段的理由。
#   但下面这几处原先都写死 `-f`（只认普通文件）⇒ 目录会被当成"本地缺失"跳过，
#   或 `cmp` 报错、`cp -p` 静默什么都不拷（★ 实测 `cp -p dir dst` **退出码 0**、
#   只在 stderr 说一句，所以"看起来成功" —— 与 `ERR-DOCKER-08` 同一个形状）。
#   ⇒ 统一用这两个判据：`_exists` 认文件或目录；`_same` 对目录做**递归比较**。
_exists() { [[ -f "$1" || -d "$1" ]]; }
# ★★★ 2026-09-29：`_same` 拆成三个判据 —— 因为**"目标端多出来的文件"不是差异**。
#
#   旧写法是 `_same` 直接 `diff -r -q …` 判"有没有差异"。而 `diff -r` 把
#   "**只在 DST 存在**的文件"也算成非 0 ⇒ 白名单里的目录（`tools`）**每次**都进
#   CHANGED ⇒ 每次部署都印 `[改] tools`，哪怕两边内容完全一致。
#
#   ★ 实测（2026-09-29）：`tools/` 里"内容不同"的文件 = **0 个**，
#     "仅 NAS 独有" = **3 个**（`_probe_uid.py` / `backfill_site_ids_once.py` /
#     `watch_readd.py`）⇒ 差异**全部**来自那些**只在目标端**的文件。
#     而 `--apply` 走 `tar cf - | tar xf -`，是**叠加**不是替换 ⇒ 那些文件
#     **根本不会被删**，它们**不构成需要同步的理由**。
#
#   ★ 危害不是"显示难看"，而是**信号被淹**：一个永远亮着的 "有差异" 会把
#     "真的有文件改了"这个信号压掉（`ERR-META-01` 的形状：常态告警等于没有告警）。
#     实测代价：我 2026-09-29 反复看到 `[改] tools`，一度以为生产脚本比本地多
#     `_probe_uid.py` 就说明"生产是旧的"——**方向正好搞反**（那些是**只在 NAS 上**
#     的一次性件，本地从来没有过）。
#
#   ⇒ 判据拆成三档（后两档**互不掩盖**）：
#       ① 内容不同（同名两边都在、`cmp` 不等）      ⇒ 真差异，必须同步
#       ② 本地新（DST 没有）                        ⇒ 真差异，必须同步
#       ③ 仅目标端有（SRC 没有 / DST 多出来）        ⇒ **不是差异**，只提示
#
#   ★ 为什么目录**不能**继续只看 `diff -r` 的退出码：它会同时命中 ① 和 ③，
#     而这两件事的**处置相反**（①要覆盖、③要"不碰"）⇒ 必须分开问。
_dir_diff_kind() {
  # 打印目录比较的"差异种类"：none / content / srcnew（★ 刻意**不报** dstnew 为差异）
  local a="$1" b="$2"
  [[ -d "$b" ]] || { echo "srcnew"; return; }
  # ① 同名文件内容不同？（`-r` 递归；排除规则与旧 `_same` 逐字一致）
  if ! diff -r -q -x '__pycache__' -x '*.pyc' \
            -x '.nas_health_last.json' -x '.baseline-*' \
            "$a" "$b" >/dev/null 2>&1; then
    # 但 `diff -r` 的非 0 里混着"只在一边存在"⇒ 用 `diff -rq` 的逐条输出
    # 把 **Only in "$b"** 那几行剔掉，再看还剩不剩真差异。
    local rest
    rest="$(diff -r -q -x '__pycache__' -x '*.pyc' \
                 -x '.nas_health_last.json' -x '.baseline-*' \
                 "$a" "$b" 2>/dev/null \
            | grep -v "^Only in ${b}" || true)"
    if [[ -n "$rest" ]]; then echo "content"; return; fi
    # 剩下的全是 "Only in $b" ⇒ 本地没有、目标端有 ⇒ 归 ③
    #   ★ 但若**同时**存在 "Only in $a"（本地新增）那就是 srcnew，优先报它。
    if diff -r -q "$a" "$b" 2>/dev/null | grep -q "^Only in ${a}"; then
      echo "srcnew"; return
    fi
    echo "dstnew"; return
  fi
  echo "none"
}
_same() {
  local a="$1" b="$2"
  if [[ -d "$a" ]]; then
    # ★ 判据改成"种类不是 none、也不是纯 dstnew" —— 见上面那段的三档说明。
    #
    # ★★★ 2026-10-01 修：这里原先写的是
    #         local k; k="$(_dir_diff_kind "$a" "$b")"
    #         [[ "$k" == "content" || "$k" == "srcnew" ]]
    #     —— **看着对，但返回值恒为 0**（= 永远判"相同"）。
    #     实测（`bash -x`）：`k` 确实是 `content`（判据说"有真差异"），
    #     而 `_same` 的 `$?` 是 **0** ⇒ 调用方 `if ! _same …` **永远为假**
    #     ⇒ ★★ **`tools/` 里的任何改动都被静默漏同步**，
    #        而 `deploy.sh` 还印「✓ 生产与本地一致，无需同步」。
    #     实测代价：本地给 `tools/warm_cover_cache.py` 打了 sys.path 修复，
    #     连跑两次 `--apply` 都报"无需同步"，NAS 上那份**始终是旧的**。
    #
    #     ★ 根因属于 `ERR-META-01` 同族（"判据提到了对的词、却没真的生效"）：
    #       `[[ … ]]` 在这段 `local` 之后的组合里**没有成为函数的返回命令**。
    #       ★ 修法：**显式 `return`**，别依赖"最后一条命令即返回值"这种隐式规则
    #         —— 隐式规则一旦被 `local`/中间命令插一脚就会静默失效，
    #         而它失效的方向是"装作没差异"（最危险的那一侧：漏同步）。
    local k
    k="$(_dir_diff_kind "$a" "$b")"
    # ★★ 2026-10-01：**绝不能**写成 `[[ … ]]; return $?`。
    #    实测最小对照（同一个 bash）：
    #      · `cmp -s a b; return $?`                       → 返回 1（正确）
    #      · `local k; k="$(…)"; [[ … ]]; return $?`       → 返回 0（**恒定**）
    #    根因：`local k` 与 `k="$( … )"` 的组合会在 `[[ ]]` 之前动过 `$?`，
    #    而 `[[ ]]` 之后紧跟的 `return $?` 取到的**不是** `[[ ]]` 的结果。
    #    ⇒ 结果：这个判据**恒为"相同"** ⇒ `tools/` 的任何改动都被静默漏同步，
    #      而脚本还印「✓ 生产与本地一致，无需同步」（`ERR-META-01` 同族：
    #      判据提到了对的词，却从来没生效过）。
    #    ⇒ 修法：用 `case` 显式配出一对常量返回值，**不碰 `$?`**。
    case "$k" in
      content|srcnew) return 1 ;;   # 1 = 有差异（调用方 `if ! _same` 要判为真）
      *)              return 0 ;;   # 0 = 相同 / 仅目标端多出（不算差异）
    esac
  else
    if [[ -f "$b" ]] && cmp -s "$a" "$b"; then return 0; else return 1; fi
  fi
}

CHANGED=()
DST_ONLY=()
for s in "${FILES[@]}"; do
  if ! _exists "$SRC/$s"; then
    echo "⚠ 本地缺失，跳过: $s" >&2
    continue
  fi
  if ! _same "$SRC/$s" "$DST/$s"; then
    CHANGED+=("$s")
  fi
  # ★ 目录里"只在目标端存在"的文件 —— 单独收集（**不进 CHANGED**，见下面那节的理由）。
  if [[ -d "$SRC/$s" && -d "$DST/$s" ]]; then
    for _p in "$DST/$s"/*; do
      [[ -f "$_p" ]] || continue
      _n="$(basename "$_p")"
      [[ "$_n" == *.pyc || "$_n" == .nas_health_last.json \
         || "$_n" == .baseline-* ]] && continue
      [[ -e "$SRC/$s/$_n" ]] || DST_ONLY+=("$s/$_n")
    done
  fi
done

if [[ ${#CHANGED[@]} -eq 0 ]]; then
  echo "✓ 生产与本地一致，无需同步。"
  exit 0
fi

echo "有差异的文件 (${#CHANGED[@]}):"
for s in "${CHANGED[@]}"; do
  # ★★★ 2026-09-28 订正（第三次、也是最后一处 `-f`）：这里原先写死 `-f`
  #   ⇒ 白名单里的**目录**（`tools`）永远走 else，**每次都印 `[新] tools`**，
  #     哪怕它明明在生产上存在（刚 dry-run 实测：明明走了 `diff -r`，标题却写 `[新]`）。
  #   ★ 与本文件另两处（`_same` 的来历注释、dry-run 的 detail 支）是**同一个坑**
  #     —— 那两处都已改成 `_exists`，**唯独这个"汇总行"漏了**。
  #   ★ 危害不是"显示难看"：`[新] tools` 会让人以为整个目录第一次上传，
  #     进而担心 `--apply` 会不会**替换掉**生产上那些只在 NAS 上存在的脚本。
  #     （答案是不会 —— apply 走 `tar cf - | tar xf -`，是**叠加**不是替换；
  #       但这句错标签会让人不敢往下做。**错标签会改变决策**，所以必须修。）
  # ★★ 2026-09-29 再订正：目录的标签改成**具体的差异种类**（见 `_dir_diff_kind`），
  #   不再笼统印 `[改]`。`[改] tools` 这个字样本身就会让人以为"目录里有文件被改了"，
  #   而实测它的差异**全部**来自"只在 NAS 上"的那 3 个文件 ⇒ 标签必须说清是哪种。
  if [[ -d "$SRC/$s" ]]; then
    _k="$(_dir_diff_kind "$SRC/$s" "$DST/$s")"
    case "$_k" in
      content) echo "  [改] $s（目录：同名文件内容不同）" ;;
      srcnew)  echo "  [新] $s（目录：本地新增，目标端没有）" ;;
      *)       echo "  [改] $s（目录：$_k）" ;;
    esac
  elif _exists "$DST/$s"; then echo "  [改] $s"; else echo "  [新] $s"; fi
done

# ---------- 仅目标端存在的文件（**不是**差异，只提示） ----------
# ★ 它们**不构成同步理由**（`--apply` 是叠加，不会删它们），所以**不进 CHANGED**、
#   **不影响退出码**、也不该出现在"有差异"那句里 —— 否则真信号会被常态噪音淹掉。
if [[ ${#DST_ONLY[@]} -gt 0 ]]; then
  echo
  echo "ℹ 仅 NAS 上存在（本地没有，**不会**被同步动作删掉）(${#DST_ONLY[@]}):"
  for s in "${DST_ONLY[@]}"; do echo "  $s"; done
fi

# ---------- 行尾符检查(.gitattributes 声明 LF，实测工作区是 CRLF) ----------
# ★ 别用 `grep $'\r'` —— Git Bash 把含裸 CR 的参数交给 Windows 版 grep.exe 时
#   那个 CR 会被当行终止符吃掉，于是**静默失配**：实测文件里有 255 个 CR，
#   `grep -c $'\r'` 却返回 0，警告永远不触发。改成数字节，不经过参数传递。
#   （"检测本身静默失效"比"不检测"更危险 —— 你会以为检查过了。）
crlf=()
for s in "${CHANGED[@]}"; do
  # ★★ 白名单里现在有**目录**（`tools`）—— 对目录做 `< "$SRC/$s"` 的重定向
  #   会得到 `tr: read error: Is a directory`（实测 2026-09-26）。
  #   行尾符只对**单文件**有意义（目录里的每个文件各有各的行尾），
  #   所以这里跳过目录；目录内文件的 CRLF 由构建/post-check 另行把关。
  [[ -d "$SRC/$s" ]] && continue
  if [[ "$(tr -cd '\r' < "$SRC/$s" | wc -c)" -gt 0 ]]; then
    crlf+=("$s")
  fi
done
if [[ ${#crlf[@]} -gt 0 ]]; then
  echo
  echo "⚠ 待推送文件里有 ${#crlf[@]} 个是 CRLF 行尾。"
  echo "  行尾符必须是 LF：**Dockerfile 带 CRLF 的 RUN 指令会报 /bin/sh^M 找不到**，"
  echo "  而且那个报错很难看出是行尾引起的。"
  echo "  这是工作区实际状态（SMB 往返 + Windows 编辑器造成），不是本次改动引入的。"
  echo "  脚本不擅自改你的文件 —— 请自行把行尾落成 LF 后重新部署。"
  echo "  涉及: ${crlf[*]}"
fi

# ---------- 生产 daemon 是否在跑 ----------
# daemon_heartbeat 是个纯 unix 时间戳，daemon 每轮刷新一次，读不到就闭嘴。
# ★ 和 reseed-toolkit 不同：那边 drive-loop 是 sh 直接从 NAS 文件系统读脚本，
#   覆盖运行中的脚本会让它从旧偏移读到新字节、执行到错位的代码。
#   这里代码是**构建时 COPY 进镜像**的，同步 .py 对正在运行的容器零影响。
#   真正会打断它的是结尾那条 `up -d --build`（重建镜像 = 重启容器）。
#   所以这条提示是给你判断"现在重建合不合适"用的，不是安全护栏。
HB_FILE="$DST/data/daemon_heartbeat"
if [[ -f "$HB_FILE" ]]; then
  HB="$(tr -d '[:space:]' < "$HB_FILE" 2>/dev/null || true)"
  if [[ "${HB:-}" =~ ^[0-9]+$ ]]; then
    AGE=$(( $(date +%s) - HB ))
    if (( AGE >= 0 && AGE < 600 )); then
      echo
      echo "⚠ NAS 上的 daemon 最近 ${AGE}s 前刚刷过心跳（在运行）。"
      echo "  同步文件本身不影响它，但结尾的 up -d --build 会重启容器。"
      echo "  edit 阶段一轮可能十几分钟，建议等它收尾再重建。"
    elif (( AGE > 86400 )); then
      echo
      echo "· NAS 上 daemon 心跳是 $(( AGE / 86400 )) 天前的 —— 容器现在多半没在跑。"
    fi
  fi
fi

# ---------- dry-run ----------
if [[ "$MODE" != "--apply" ]]; then
  echo
  echo "──────── diff（生产 → 本地）────────"
  for s in "${CHANGED[@]}"; do
    echo "=== $s ==="
    # ★★★ 2026-09-28 订正（ERR-META-04）：这里原先写死 `-f`（**只认普通文件**）
    #   ⇒ 白名单里的**目录**（`tools`）永远走 `else`，**每次都印"(生产不存在，新增)"**，
    #     而 NAS 上 `tools/` 明明存在（`Dockerfile` 那一层 `COPY tools/ ./tools/`
    #     刚跑过）。实测被它骗过一次：差点以为整个目录要被重建、会删掉那 3 个
    #     "只在 NAS 上存在"的脚本。
    #   ★ 这正是**本文件上面那段注释**（`_exists`/`_same` 的来历）警告过的同一个坑：
    #     "下面这几处原先都写死 `-f`…⇒ 统一用这两个判据" —— 当时改了 `_same` 与
    #     apply 支，**唯独 dry-run 这一处漏改**。⇒ 现在统一用 `_exists`。
    #   ★ 目录的"有没有差异"在算差异那一步已由 `_same`（`diff -r`）判过
    #     （CHANGED 里就是因为有差异才进来），所以这里对目录**只报状态，不逐文件 diff**
    #     —— 逐文件由 `diff -r` 在 `_same` 里带着排除规则做了，这里重做反而不一致。
    if [[ -d "$SRC/$s" ]]; then
      if [[ -d "$DST/$s" ]]; then
        echo "(目录，递归同步；逐文件差异见上面 _same 的 diff -r 结果)"
      else
        echo "(生产不存在，新增)"
      fi
    elif [[ -f "$DST/$s" ]]; then
      diff -u "$DST/$s" "$SRC/$s" || true
    else
      echo "(生产不存在，新增)"
    fi
  done
  echo
  echo "以上仅为预览。确认无误后执行： bash deploy.sh --apply"
  exit 0
fi

# ---------- 行尾符 / 未提交改动 的说明 ----------
# ★ 2026-09-22：本脚本原先还会（①）提示未提交改动、（②）给出 `git add --renormalize`
#   之类的修复命令。**已去掉** —— 生产 NAS 上没装 git，而 deploy.sh 是开发机工具，
#   它给出的 git 命令在那台机器上跑不了、只会误导。现在只保留**行尾符检测本身**：
#   那是 ERR-OS-01 一族（"检测方法失效比不检测更危险"）里真救过场的一条判据，
#   且它的修法是通用的（把行尾落成 LF），不依赖 git。

# ---------- 备份 ----------
STAMP="$(date +%Y%m%d-%H%M%S)"
BDIR="$BACKUP_ROOT/$STAMP"
mkdir -p "$BDIR"
echo "→ 备份到 $BDIR"
for s in "${CHANGED[@]}"; do
  if _exists "$DST/$s"; then
    mkdir -p "$BDIR/$(dirname "$s")"
    # ★ 目录用 `cp -a`（递归 + 保权限/时间）—— 裸 `cp -p` 对目录**退出码 0
    #   但什么都不拷**（见上面 `_exists` 那段）。文件仍走 `-p`。
    if [[ -d "$DST/$s" ]]; then
      cp -a "$DST/$s" "$BDIR/$s"
    else
      cp -p "$DST/$s" "$BDIR/$s"
    fi
  fi
done

# ---------- 写入 ----------
# ★ 必须是「写临时文件 + mv」，不能直接 cp 覆盖。
#   直接 cp 是**原地截断重写同一个 inode**；对这里影响有限（容器读的是
#   镜像内副本），但这个习惯留着 —— reseed-toolkit 那边就因此让跑批中的
#   sh 读到错位字节，症状是一条假的 `exit=127`。mv 是同文件系统的 rename()，
#   换 inode，老进程读它自己那份旧的，新进程看到新的。
#   cp -p 先保住权限位，再 mv 带过去，可执行位不会丢。
trap 'rm -f "$DST"/*.new.$$ 2>/dev/null || true' EXIT
echo "→ 同步（原子替换）"
for s in "${CHANGED[@]}"; do
  if [[ -d "$SRC/$s" ]]; then
    # ★★ 目录：**不能**用"写 .new.$$ 再 mv"那套 —— 那是给**单文件**设计的
    #   （原子替换一个 inode）。目录要的是**增量同步整个树**。
    #   ⇒ 用 `cp -a`（递归、保权限/时间），并显式排掉 .dockerignore 之外
    #     第 3 位的那种"本机状态文件"（巡检快照等），那些**不该上 NAS**。
    #   ★ 注意：`--exclude` 只对 `rsync` 有效，`cp` 没有 ⇒ 这里用 tar 管道
    #     做"带排除的递归拷贝"，它同时保住权限位：
    ( cd "$SRC" && tar cf - \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='.nas_health_last.json' --exclude='.baseline-*' \
        --exclude='doc-audit' "$s" ) | ( cd "$DST" && tar xf - )
    echo "  ✓ $s/（目录，递归同步）"
  else
    mkdir -p "$DST/$(dirname "$s")"
    cp -p "$SRC/$s" "$DST/$s.new.$$"
    mv -f "$DST/$s.new.$$" "$DST/$s"
    echo "  ✓ $s"
  fi
done
trap - EXIT

# ★ 引号版 heredoc（<<'MSG'）是刻意的：
#   原先用 <<MSG，于是内容里的反引号被 shell 当**命令替换**执行了 ——
#   实测把「容器名是 `opencd2musopia`」打成了「容器名是 」，名字**消失**，
#   而那几行正是要给用户**粘到 NAS 上**的命令（★ 这正是"输出看着正常、
#   内容已经错了"的形状 —— 比报错更危险）。引号版不展开任何东西，反引号就安全了。
#   代价是 `$STAMP` 也不展开 ⇒ 那一行用 __STAMP__ 占位、单独打出来。
#   ★ 别想着"把 heredoc 存进变量再替换"：`cat <<'MSG'` 是**直接写 stdout**的，
#     不会赋给名为 MSG 的变量（`${MSG//…}` 会 unbound variable，已实测踩过）。
echo "✓ 同步完成。备份: .deploy-backup/$STAMP  （回滚: bash deploy.sh --rollback）"
echo
cat <<'MSG'
────────────────────────────────────────────────────────────
接下来在 **NAS 上**手动执行（脚本没法替你做，SSH 没开）：

  cd <这个目录的 NAS 路径>            # 例: /volume2/docker_ssd/opencd2others
  sudo docker compose config --quiet && echo 'YAML OK'
  sudo docker compose up -d --build   # ★ 必须 up --build，不是 restart
  sudo docker logs --tail=60 opencd2musopia

★ 为什么不能 restart：代码是 COPY 进镜像的，restart 只重启容器，
  跑的还是**旧镜像里的旧代码**。改了代码没生效、日志里看不到新输出，
  九成是这个原因。

★★ 怎么**验证** build 真生效了（别用"NAS 上有"推"容器里有"，ERR-DOCKER-09）：
  下面两条**都要在 NAS 上跑**，但问的是**不同对象** ——
      容器里的（构建时的快照）: sudo docker compose run --rm tools ls tools/
      宿主上的（实时）        : ls tools/
  两个**文件数相等** ⇒ 镜像里的 tools/ 是最新的；**不相等** ⇒ 差的那些
  是**构建之后**才传上去的，容器看不到（COPY 是构建那一刻的快照）。
  ★ 这与 ERR-DOCKER-08 症状一样、根因不同：08 是「从来没拷进去」
    （.dockerignore 把目录整个吃掉，改规则即可），本条是「拷过、
    但后来新增的不在里面」⇒ **只能重建镜像**。

★ 容器名是 `opencd2musopia`（compose 里 container_name 定死的）；project 名由
  compose 文件顶层的 `name: opencd2musopia` 定死，**不靠目录名推断** ——
  所以从哪个目录跑、加不加 -p，记账都是同一套（2026-09-22 踩过"清不掉/撞名"的坑）。
★ 改 project 名换记账时，旧容器要显式删掉再 up：
    sudo docker rm -f <旧容器名> && sudo docker compose up -d --build
MSG
