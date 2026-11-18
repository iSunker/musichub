#!/usr/bin/env bash
# =====================================================================
# 本机镜像冒烟测试（build-push.sh 在 **push 之前** 调它）
#
# 为什么需要它（2026-10-20 用户要求）：
#   「构建镜像后在本机 docker desktop 启动运行测试一下为 bug 然后再 push，然后再部署」
#
#   在此之前，唯一的构建期判据是 `tools/build-preflight.sh`（查 COPY 源齐不齐）
#   和可选的 `run.py --selftest`（静态/AST 判据）。**两者都不让镜像真的跑起来。**
#   而本项目栽过好几类"构建绿、进镜像、一跑就炸"的 bug：
#     · ERR-DOCKER-09  COPY 吃到 SMB 旧文件 ⇒ 镜像里是旧代码
#     · ERR-QB-08      qb_client.py 没进 COPY ⇒ 跑到那阶段才 ModuleNotFoundError
#     · ERR-PUBLISH-27 "selftest 绿 + 代码进镜像" ≠ "闸门在生产生效"
#   本脚本补的就是这一格：**在推之前**让镜像真的启动、真的 import 全部模块、
#   真的跑一遍 `--selftest`。
#
# ★★★ 安全纪律（这是本脚本最重要的一节）：
#   1. **绝不碰生产数据**：挂载的是 `mktemp -d` 出来的空目录，
#      并用 `DATA_DIR` / `DB_PATH` / `OUTPUT_TORRENT_DIR` 显式指向它。
#      ⇒ 本机跑冒烟测试**不可能**写到 NAS 的 `/data`，也不可能读到生产库。
#   2. **绝不联网发种**：只跑 import + `--selftest`（纯离线）。
#      不跑 daemon 的发布阶段 —— 那会真发种（不可逆）。
#   3. **凭据用假值**：`QB_PASS=x` 之类占位，从本机 `.env` 只取**键名**、不取值。
#      ⇒ 冒烟测试失败时不会把凭据打到终端。
#   4. 容器**总是**被删（`trap … EXIT`），失败也删 —— 不留僵尸，不占本机资源。
#
# 用法:
#   bash tools/image-smoke-test.sh                    # 测最:latest
#   bash tools/image-smoke-test.sh isunker/opencd2others:abc1234
#
# 退出码非 0 = 镜像有问题，**push 应当中止**。
# =====================================================================
set -euo pipefail

# ★★★ 必须关掉 MSYS/Git-Bash 的路径自动转换。
#   实测（2026-10-20 第一次跑）：Git Bash 会把 `-e DATA_DIR=/data` 里的**值**
#   改写成 `C:/Program Files/Git/data` ⇒ 容器里 DB_PATH 变成
#   `/app/C:/Program Files/Git/data/data.db` ⇒ 写不进隔离目录、**心跳也写不出**。
#   而脚本当时**报的错是"daemon 没能启动"** —— 看起来像镜像的 bug，
#   实际是**测试工具的 bug**。这正是「检测器自己坏了，却指责被测物」的形状。
#   ⇒ `MSYS_NO_PATHCONV=1` + 下面所有容器内路径都用 `//` 开头写法，双保险。
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SRC:-$(cd "$SELF_DIR/.." && pwd)}"
IMAGE="${1:-isunker/opencd2others:latest}"
NAME="opencd2smoke-$$"

command -v docker >/dev/null || { echo "✗ 找不到 docker" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "✗ 本机没有镜像 $IMAGE（先构建）" >&2; exit 1; }

# ---------- 隔离运行环境 ----------
# ★ `mktemp -d` 在失败时不能留下垃圾 ⇒ 记下来，trap 里删。
TMPD="$(mktemp -d 2>/dev/null || mktemp -d -t smoke)"
cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  rm -rf "$TMPD" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ★★ 凭据只取**键名**、值一律用占位 `smoke`。
#   `config.require_env()` 在 import 期就会检查这些键在不在，
#   但我们**不需要**真值（本脚本不发种、不连站点）。
#   ⇒ 从这里把真值读进来 = 把凭据送进一个可能失败并打日志的进程，没必要。
#
# ★★★ 但**不是所有键都能给占位**（2026-10-20 第二次跑实测踩到）：
#   `.env` 里有 `DATA_DIR=/data`、`SCHED_TICK=30` 这类**路径/数值**键，
#   把它们也换成 `smoke` ⇒ 容器里 `DATA_DIR='smoke'` ⇒ 相对路径
#   `smoke/daemon_env.json.tmp` 写不进去、心跳也落不到隔离目录，
#   而脚本**报的是"隔离目录是空的 ⇒ 可能写到别处去了"** —— 又是
#   "测试工具自己坏了、却指责被测物"的形状。
#   ⇒ 分两类：
#     · **结构性键**（路径/开关/数值）：只**原样透传**（值来自 .env），绝不改写；
#     · **凭据键**（名字里带 PASS/KEY/COOKIE/TOKEN/SECRET/PASSKEY）：改写成 `smoke`。
#   ★ 判据用**名字模式**而不是手抄白名单 —— 手抄必然漏（ERR-META-03）。
_is_secret() {
  case "${1,,}" in
    *pass*|*key*|*cookie*|*token*|*secret*|*auth*) return 0 ;;
    *) return 1 ;;
  esac
}
# 这些键**永远**由本脚本自己给值（隔离用），.env 里的原值不尊重。
#
# ★★★ 为什么 `ENABLE_*` / `ALLOW_EMPTY_DB` 也在这里（2026-10-20 第三次跑实测踩到）：
#   冒烟测试的 daemon 那步把 `ENABLE_PUBLISH=false` 放在 `D_ENV`，而 `.env` 里的
#   `ENABLE_PUBLISH=true`、`ALLOW_EMPTY_DB=false` 通过 `ENV_ARGS` 又传了一遍。
#   **docker 的多个 `-e` 同名时后者覆盖前者**（`ENV_ARGS` 在 `D_ENV` 之后展开）
#   ⇒ 我关掉的又被 `.env` 打开了 ⇒ daemon 判定「空库 + publish 开着」⇒ 拒绝启动。
#   而脚本报的是"隔离目录是空的"，看起来像镜像 bug —— 又是**同类形状**。
#   ⇒ 凡是"这次测试要强制关闭/开启"的键，一律列进这里由脚本独占。
_is_forced() {
  case "$1" in
    DATA_DIR|DB_PATH|OUTPUT_TORRENT_DIR) return 0 ;;
    ALLOW_EMPTY_DB) return 0 ;;               # 冒烟测试：由 D_ENV 显式给 true
    ENABLE_*) return 0 ;;                     # 全部阶段开关：由 D_ENV 显式关掉
    *) return 1 ;;
  esac
}

ENV_ARGS=()
if [[ -f "$SRC/.env" ]]; then
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"          # 去前导空白
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    k="${line%%=*}"
    k="${k%"${k##*[![:space:]]}"}"                    # 去尾部空白
    v="${line#*=}"
    v="${v%\"}"; v="${v#\"}"                          # 去引号
    v="${v%$'\r'}"                                    # ★ 去 CRLF 的 `\r`
    [[ -z "$k" ]] && continue
    _is_forced "$k" && continue                       # 隔离键由下面显式给
    if _is_secret "$k"; then
      ENV_ARGS+=(-e "${k}=smoke")                     # 凭据：占位
    else
      ENV_ARGS+=(-e "${k}=${v}")                      # 结构性键：原样透传
    fi
  done < "$SRC/.env"
else
  # 没 .env 时给最小的几个（够 config import 通过即可）。
  for k in QB_HOST QB_USER QB_PASS OPENCD_API_KEY MUSOPIA_COOKIE CRABPT_COOKIE; do
    ENV_ARGS+=(-e "${k}=smoke")
  done
fi

echo "→ 镜像冒烟测试：$IMAGE"
echo "  隔离目录: $TMPD（不碰生产 /data）"

FAIL=0
run() {   # run <人类可读的名字> <容器内命令...>
  local label="$1"; shift
  if docker run --rm --name "$NAME" \
       --network none \
       -e DATA_DIR=//data \
       -e DB_PATH=//data/data.db \
       -e OUTPUT_TORRENT_DIR=//data/output_torrents \
       "${ENV_ARGS[@]}" \
       -v "$TMPD:/data" \
       --entrypoint "${1:-}" "$IMAGE" "${@:2}" >"$TMPD/.out" 2>&1; then
    echo "  ✅ $label"
  else
    echo "  ❌ $label"
    sed -e 's/^/       /' "$TMPD/.out" | tail -25
    FAIL=1
  fi
}

# ---------- ① 容器能起来、依赖齐 ----------
#   `--network none`：这一步**故意**断网 —— 冒烟测试不该有任何外发流量。
run "解释器与依赖可用（python -V + import 全部第三方包）" \
    python -c "import sys, qbittorrentapi, torf, requests, bs4, PIL; print(sys.version)"

# ---------- ② 镜像内代码**全部**能编译 + 能 import ----------
#   ★ 这一步专抓 ERR-QB-08 那类"漏进 COPY ⇒ 跑到那阶段才炸"：
#     本机 `.py` 有、镜像里没有 ⇒ 这里当场 ModuleNotFoundError。
#   ★ 用 `src/*.py` 的文件名清单（构建时 COPY 的就是它）——而不是手抄模块名，
#     手抄清单必然漂移（ERR-META-03）。
#
# ★★★ 2026-10-20（D11f）实测踩到，**分两段**是因为第一版把两件事混在一段里：
#   第一版是 `for m in mods: importlib.import_module(m)` 在**同一个进程**里全 import，
#   结果炸在 `observe_custom_fields`：它在 **import 期**就发 HTTP 请求，而本机 `.env`
#   的值带 `\r`（本机文件是 CRLF）⇒ `InvalidURL: Host 'www.musopia.vip\r'`。
#   ⇒ 那是**测试工具没隔离**（不是镜像的 bug）—— 又一次"检测器自己坏了，却指责被测物"。
#   ★ 分两段：
#     ① **编译**（`compile()`）：0 副作用、0 网络、0 env 依赖。它抓"语法/编码/文件缺失"
#        —— 也就是"代码真的进镜像了吗"这件最核心的事。
#     ② **import**：每个模块**独立子进程**跑（`multiprocessing` 起不来就用子进程逐个），
#        超时/失败只影响那一个模块，不污染其它模块、也不让脚本整体超时。
#        只对"有 import 期副作用（联网）"的模块做**已知例外白名单**：见 NOIMPORT。
#
#   ★ 为什么保留 import 这一段（而不是只编译）：`ERR-QB-08` 那种"缺依赖包"的错
#     在编译期是**看不出来**的（`import qbittorrentapi` 语法完全合法）。
MODS="$(cd "$SRC/src" && ls *.py 2>/dev/null | sed 's/\.py$//' | grep -v '^__' | tr '\n' ' ')"
run "镜像内代码全部可编译（$(echo "$MODS" | wc -w) 个模块 / AST 级，无副作用）" \
    python -c "import os,sys
n=0; bad=[]
for f in sorted(os.listdir('.')):
    if not f.endswith('.py') or f.startswith('__'): continue
    n+=1
    try: compile(open(f,encoding='utf-8').read(), f, 'exec')
    except Exception as e: bad.append((f,type(e).__name__,str(e)[:120]))
for f,t,e in bad: print('  ✗',f,t,e)
print(f'  编译通过 {n-len(bad)}/{n}')
sys.exit(1 if bad else 0)"

# ★ 已知的「import 期有副作用」模块 —— 只列**实测**有网络/文件副作用的，别的都不许进来。
#   `observe_custom_fields`：import 时就发 HTTP 探站点自定义字段（实测 D11f）。
NOIMPORT="observe_custom_fields"
IMPORTABLE="$(for m in $MODS; do case " $NOIMPORT " in *" $m "*) ;; *) printf '%s ' "$m";; esac; done)"
run "镜像内模块逐个 import（$(echo "$IMPORTABLE" | wc -w) 个，各自独立子进程；跳过已知副作用: $NOIMPORT）" \
    python -c "import importlib,subprocess,sys
mods='$IMPORTABLE'.split()
bad=[]
for m in mods:
    # ★ 每个模块一个**独立 python 进程**（不是 importlib 同进程连跑）⇒
    #   某个模块 import 期崩了/挂住了，不会带跑其它模块的结论。
    #   `-c` 走 import 再 exit(0)；异常时 python 自己打 traceback 到 stderr。
    p=subprocess.run([sys.executable,'-c',f'import {m}'],
                     capture_output=True,text=True,timeout=120)
    if p.returncode!=0:
        last=[l for l in (p.stderr or '').strip().splitlines() if l.strip()]
        bad.append((m, (last[-1] if last else '')[:140]))
for m,e in bad: print('  ✗',m,'→',e)
print(f'  import 通过 {len(mods)-len(bad)}/{len(mods)}')
sys.exit(1 if bad else 0)"

# ---------- ③ 两个 --selftest 真跑一遍 ----------
#   ★ `run.py --selftest` 里的 ⑦e / ⑦e-2 / ⑦e-3 是本仓库**钉 WHERE 常量**的判据，
#     它们是"代码进镜像后还对不对"的现场证据（不是本机源码对不对）。
#   ★ `publish.py --selftest` 的 10 条判据全是离线内存 fixture，不碰站点、不碰库。
#
# ★★★ `publish.py --selftest`：要求**必须绿**（它是纯内存 fixture，镜像里没理由红）。
run "publish.py --selftest" python publish.py --selftest

# ★★★ `run.py --selftest`：**不要求绿，要求"不比基线更红"**。
#   为什么？（2026-10-20 实测，这是本脚本最有价值的一条判据）
#   它在本机也**不是全绿**的：⑨i 的几条依赖 `/.` 隐藏文件、以及若干只在生产库/生产
#   目录下才有意义的判据，在镜像的空 `/data` 下本来就红。
#   ⇒ 第一版直接 `run "run.py --selftest"` ⇒ **永远红** ⇒ 脚本永远报失败 ⇒
#     要么被人 `|| true` 掉（判据全废），要么没人敢 push。两种结局都比没有它更糟。
#   ★ 但也**不能**改成 `|| true`：**本次就是它抓到了真的回归** ——
#     D6 新增第三条 `save_task` 写支后，⑦（`**mt_col` 注入点恰好 2 处）当场红，
#     也就是"无 log 那条写支**没注入标题**"（= 那批种子会丢 `formatted_title`）。
#     这个 bug 本机 selftest 同样会红，只是"没人拿它当 push 的闸门"就漏了。
#   ⇒ 正确判据 = **基线对比**：把"已知会红的条目"钉成基线文件，只报**新增**的红。
#     基线改了必须**显式**改文件（`SMOKE_SELFTEST_BASELINE=…` 或直接编辑），
#     不许静默漂移（`ERR-META-05`：两个口径）。
BASELINE_FILE="$SRC/tools/.smoke-selftest-baseline.txt"
_run_selftest_baseline() {
  local out="$TMPD/.run_selftest"
  docker run --rm --name "$NAME" --network none \
    -e DATA_DIR=//data -e DB_PATH=//data/data.db \
    -e OUTPUT_TORRENT_DIR=//data/output_torrents \
    "${ENV_ARGS[@]}" -v "$TMPD:/data" \
    --entrypoint python "$IMAGE" run.py --selftest >"$out" 2>&1 || true
  # ★ 只抽"✗ …"开头的不符行，且**去掉具体数字**（行号/计数会随每次改代码而变，
  #   留着就必然基线永远漂移）⇒ 只留**判据标识**（第一个词，如 `⑦`、`⑦b`）。
  #   `sed` 抽得到的是形如 `✗ ⑦`**mt_col** 注入点应为…` 的行。
  grep -E '^[[:space:]]*✗ ' "$out" 2>/dev/null \
    | sed -E 's/^[[:space:]]*✗[[:space:]]*//; s/[（(].*$//' \
    | awk '{print $1}' | sed -E 's/[，,。：:].*$//' | sort -u
  # ★ 另外抓"自检失败：N 项不符"这一行的 **N**，并跑失败条数
  echo "---"
  grep -E '自检失败|自检通过' "$out" 2>/dev/null | tail -1 || echo "(无结论行)"
}
echo "→ run.py --selftest（基线对比：$BASELINE_FILE）"
_cur="$(_run_selftest_baseline)"
_cur_ids="$(echo "$_cur" | sed '/^---$/,$d')"
_cur_tail="$(echo "$_cur" | sed -n '/^---$/,$p' | tail -1)"
if [[ -z "$_cur_ids" ]]; then
  echo "  ✅ run.py --selftest 全绿（$_cur_tail）"
else
  _base_ids=""
  [[ -f "$BASELINE_FILE" ]] && _base_ids="$(grep -vE '^\s*(#|$)' "$BASELINE_FILE" | sort -u)"
  _new="$(comm -23 <(echo "$_cur_ids") <(echo "$_base_ids") 2>/dev/null || true)"
  if [[ -z "$_new" ]]; then
    echo "  ✅ run.py --selftest 的不符条目**全部在已知基线内**（$_cur_tail）"
    echo "     已知基线: $(echo "$_base_ids" | tr '\n' ' ')"
  else
    echo "  ❌ run.py --selftest 出现**基线之外的新不符**（$_cur_tail）"
    echo "     新增判据: $(echo "$_new" | tr '\n' ' ')"
    echo "     ⇒ 这是本脚本要拦的真回归。修代码，**不要**把它加进基线。"
    echo "     若确认「镜像环境本来就会红、与本次改动无关」，才改 $BASELINE_FILE"
    FAIL=1
  fi
fi

# ---------- ④ daemon 能起来（用 5 秒超时，绝不真跑发布）----------
#   ★ 这一步验的是 CMD/ENTRYPOINT 与调度骨架：daemon.py 能 import、
#     能写心跳、能进主循环。**不发种**（把所有 ENABLE_* 关掉 + 5s 后 kill）。
#   ★ 不用 `--entrypoint daemon.py`：走镜像**默认 CMD**，才验得到 CMD 本身有没有坏。
#    ★ 键名从 `grep -oE 'env_bool\("ENABLE_[A-Z_]+"' src/daemon.py` 得来**再手抄一遍**
#      —— 这里没能自动取，是因为它是 shell 脚本、不该在运行时 import Python 源码。
#      ⇒ 用一条**自检**兜住漂移：daemon.py 里若新增 `ENABLE_*` 而这里没跟上，
#        下一段的判据会报"漏了这个开关"（见下方 ⑤ 之后的清单核对）。
D_ENV=(
  -e ENABLE_SCAN=false -e ENABLE_PUBLISH=false -e ENABLE_PUBLISH_CRABPT=false
  -e ENABLE_PUBLISH_PTER=false
  -e ENABLE_PUBLISH_HDKYLIN=false -e ENABLE_PUBLISH_XINGWAN=false
  -e ENABLE_PUBLISH_TANGPT=false -e ENABLE_PUBLISH_MUXUEGE=false
  -e ENABLE_EDIT_XINGWAN=false
  -e ENABLE_SYNC=false -e ENABLE_COVERS=false -e ENABLE_EDIT=false
  -e ENABLE_VTID=false -e ENABLE_RSS_TID=false -e ENABLE_CRAWL=false
  -e ENABLE_FREE_WINDOW=false
  -e ENABLE_PRUNE=false -e ENABLE_GC=false -e ENABLE_ORPHAN_GC=false
  # ★★ 这两个是**注释里提到过**、但 `env_bool("ENABLE_...")` 里**没有**的键
  #   ⇒ 下面的「开关清单核对」不会要求它们；留着无害，但也不必留。
  #   `ENABLE_HANDOFF` 实际是 `config.py` 里的 `HANDOFF_*` 一组，见 ⑤ 的输出。
  -e ENABLE_HANDOFF=false -e ENABLE_READD=false
  -e SCHED_TICK=1
  # ★★★ 必须显式放行空库（2026-10-20 第三次跑实测踩到）。
  #   daemon 有一道保护闸门：「数据库是空的 && publish 阶段已启用 ⇒ 拒绝启动」。
  #   那道闸门**是对的**——它在防"卷挂错导致 qB 里所有种被当成新任务、
  #   publish 再把已发过的重复发一遍，站点上多出几百个无法批量撤回的重复种"。
  #   而冒烟测试**故意**用空库 ⇒ 必然撞上它。
  #   ★ 这里是冒烟测试的**正当豁免**：我们同时把所有 ENABLE_* 都关了，
  #     而且 `--network none` ⇒ 结构上不可能发种。豁免的是"空库检查"，
  #     不是"发布闸门"。
  -e ALLOW_EMPTY_DB=true
)
# ★★★ 2026-10-20（第四次跑实测）**删掉了"看退出码判成败"这条**。
#   实测：全阶段禁用后 `daemon.py:1756` 走的是
#     `log("❌ 所有阶段都被禁用了…") / sys.exit(1)`  ⇒ **退出码恒为 1**，
#   于是第一版 `if docker run … ; then ✅ else ❌` **每次都判失败**，
#   而同一份 `.out` 里「启动横幅 + 建调度」两条阳性证据明明都在。
#   ⇒ 这就是「用错了判据」：`ERR-PUBLISH-27` 的同一形状（拿"没报错"当成功，
#     反过来拿"报错"当失败，却不问**它退在哪一行**）。
#   ★ 现在的判据只有阳性证据（见下面两段），退出码**只记录、不判**。
set +e
timeout 12 docker run --rm --name "$NAME" \
     --network none \
     -e DATA_DIR=//data -e DB_PATH=//data/data.db \
     -e OUTPUT_TORRENT_DIR=//data/output_torrents \
     "${D_ENV[@]}" "${ENV_ARGS[@]}" \
     -v "$TMPD:/data" "$IMAGE" >"$TMPD/.out" 2>&1
D_RC=$?
set -e
echo "  （daemon 退出码 $D_RC —— 全阶段禁用时**预期**为 1，见上面注释）"
# ★★ 阳性证据判据（`ERR-PUBLISH-27`：不许只看"没报错"）：
#   必须同时看到 ① 启动横幅 ② 走到了建调度那一步。
#   ★ 为什么用这两条而不是退出码：全阶段禁用时 daemon 是 `sys.exit(1)` 主动退的，
#     退出码 1 在这里**是正确行为**；而真崩溃时**看不到横幅**（更早炸）。
if grep -q "Musopia 编排器启动" "$TMPD/.out" 2>/dev/null \
   && grep -qE "所有阶段都被禁用了" "$TMPD/.out" 2>/dev/null; then
  echo "  ✅ 启动横幅 + 建调度两步都到了（daemon.py 真执行到底）"
elif grep -q "Musopia 编排器启动" "$TMPD/.out" 2>/dev/null; then
  echo "  ✅ 启动横幅已打印（daemon.py 真执行到建调度）"
else
  echo "  ❌ 日志里没有「Musopia 编排器启动」横幅 ⇒ daemon.py 可能没跑到那一步"
  sed -e 's/^/       /' "$TMPD/.out" | tail -15
  FAIL=1
fi
if [[ -f "$TMPD/daemon_heartbeat" ]]; then
  echo "  ✅ 心跳文件已写出（/data/daemon_heartbeat）"
fi
# ★★ 隔离目录**不一定**会被写（2026-10-20 第三次跑实测更正）：
#   全阶段禁用时 daemon 只做预检就退出了，**不写任何文件**。所以这一条不能
#   要求"目录非空"，否则会把正常行为判成故障。
#   ⇒ 改成判"**没有文件写到隔离目录之外**"：如果 DATA_DIR 没生效，
#     文件会落到 `/app`（镜像内可写层）——那里在容器删掉后就没了，看不出来。
#     所以这里改判**路径本身**：只要 daemon 打印的 `DB_PATH` 确实指向隔离目录即可。
#     ★ 这比"目录非空"更直接：它验的是变量有没有生效，而不是附带副作用。
if grep -qE "DB_PATH\s*=\s*//data/data\.db|DB_PATH\s*=\s*/data/data\.db" "$TMPD/.out" 2>/dev/null; then
  echo "  ✅ DB_PATH 确实指向隔离目录（DATA_DIR 生效）"
else
  echo "  ❌ 日志里的 DB_PATH 没指向隔离目录 ⇒ DATA_DIR/DB_PATH 没生效"
  grep -E "DB_PATH" "$TMPD/.out" | sed -e 's/^/       /' | head -3 || true
  FAIL=1
fi

# ---------- ⑤ 开关清单不许漂移（本脚本自己的判据）----------
#   ★ `D_ENV` 里的 `ENABLE_*` 是**手抄**自 `daemon.py` 的。手抄必然漂移
#     （`ERR-META-03`）：哪天有人加了第 11 个 `ENABLE_*` 阶段，这里没跟上 ⇒
#     那个阶段会在冒烟测试里**静默开着**跑（可能真去连站点 / 写东西）。
#   ⇒ 用源码做**真源**核对：`daemon.py` 出现的每个 `ENABLE_*` 都必须出现在本脚本里。
echo "→ 开关清单核对（daemon.py ↔ 本脚本）"
_daemon_keys="$(grep -oE 'env_bool\("ENABLE_[A-Z_]+"' "$SRC/src/daemon.py" \
                | sed 's/env_bool("//;s/"//' | sort -u)"
_missing=""
for k in $_daemon_keys; do
  grep -q -- "$k=false" "$SRC/tools/image-smoke-test.sh" || _missing="$_missing $k"
done
if [[ -n "$_missing" ]]; then
  echo "  ❌ daemon.py 里有这些 ENABLE_* 开关，但本脚本**没有显式关掉**：$_missing"
  echo "     ⇒ 它们在冒烟测试里会按 .env / 默认值运行（可能真连站点）"
  FAIL=1
else
  echo "  ✅ daemon.py 的 $(echo "$_daemon_keys" | wc -w) 个 ENABLE_* 开关都已在本脚本里显式关掉"
fi

echo ""
if [[ "$FAIL" -eq 0 ]]; then
  echo "✅ 冒烟测试通过：$IMAGE —— 可以 push"
else
  echo "❌ 冒烟测试**失败**：$IMAGE —— 不要 push，先修"
fi
exit "$FAIL"
