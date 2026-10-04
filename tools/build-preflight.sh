#!/usr/bin/env bash
# =====================================================================
# 本机构建前置自检（build-push.sh 调它，也可单独跑）
#
# 为什么需要它：
#   构建**移到本机**后（`bash build-push.sh` → 推 GHCR → NAS `pull`），
#   原先 deploy.sh 里「白名单 = Dockerfile 的 COPY 行，漏一个就拒绝部署」那条
#   反查护栏**不能直接删** —— 它防的形状没变：
#
#     ★ 加了新模块、忘了加进 Dockerfile 的 COPY ⇒ 构建**成功**（COPY 不会因为
#       漏列一个文件而报错），但容器里没有那个模块 ⇒ 跑到**用到它的那个阶段**
#       才 ModuleNotFoundError。本仓库已真实踩过两次：
#         · qb_client.py（ERR-QB-08 的修法载体）
#         · readd_queue.py
#       两次的症状都是"跑到某阶段才炸"，而构建日志一片绿。
#
#   本机构建把这条护栏**前移**了：不再等"部署到 NAS 才发现"，构建前就在本机查。
#   ★ 判据方向：Dockerfile 说要 COPY 的**每一个源**，在本机必须存在。
#     反向（本机有某 .py 但 Dockerfile 没 COPY）**不报警也没法自动报** ——
#     本机有一堆 tools/ / 探测脚本本来就不该进镜像，那是**设计**不是漏。
#     ⇒ 所以这里只做"COPY 的源存在吗"这一半，另一半（"新模块有没有进 COPY"）
#       靠 CLAUDE.md / RULES.md 的自查清单 + ⑧b 那类判据兜。
#
# 只读，不写任何文件。退出码非 0 = 构建应当中止。
# =====================================================================
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SRC:-$(cd "$SELF_DIR/.." && pwd)}"     # 仓库根
DOCKERFILE="$SRC/Dockerfile"

[[ -f "$DOCKERFILE" ]] || { echo "✗ 找不到 Dockerfile: $DOCKERFILE" >&2; exit 1; }

fail=0

# ---------- ① Dockerfile 的 COPY 源必须在本机存在 ----------
# 把 \ 续行拼成一行，取 COPY 指令的内容，去掉标志位（--chown=… 之类），
# 最后一个词是**目标**（如 ./ 或 ./tools/），其余是**源**。
# ★ 源可能带通配（`src/*.py`）——用 compgen -G 展开；无匹配 ⇒ 报错。
copy_sources() {
  awk '{ while (sub(/\\[[:space:]]*$/, "")) { getline nxt; $0 = $0 " " nxt } ; print }' "$DOCKERFILE" \
    | awk '/^COPY /{ $1=""; print }' \
    | while IFS= read -r line; do
        # shellcheck disable=SC2086
        set -- $line
        # 丢掉 --flag=… 形式的选项（COPY 的选项总在源之前）
        while [[ "${1:-}" == --* ]]; do shift; done
        n=$#
        [[ "$n" -ge 2 ]] || continue
        i=1
        for src in "$@"; do
          [[ "$i" -eq "$n" ]] && break      # 最后一个 = 目标，不是源
          echo "$src"
          i=$((i + 1))
        done
      done
}

while IFS= read -r s; do
  [[ -z "$s" ]] && continue
  if [[ "$s" == *'*'* || "$s" == *'?'* ]]; then
    # 通配源：必须有匹配，否则 COPY 会静默拷进 0 个文件
    # shellcheck disable=SC2086
    if ! compgen -G "$SRC/$s" >/dev/null; then
      echo "✗ Dockerfile 里 COPY 的通配源**无匹配**：$s" >&2
      echo "  ⇒ COPY 会静默拷进 0 个文件，容器里缺模块（构建却成功）" >&2
      fail=1
    fi
  else
    if [[ ! -e "$SRC/$s" ]]; then
      echo "✗ Dockerfile 里 COPY 的源**不存在**：$s" >&2
      fail=1
    fi
  fi
done < <(copy_sources)

# ---------- ② CRLF 行尾检测（对象 = 会进镜像的文本文件） ----------
# ★★★ 为什么单独有这条：**Dockerfile 带 CRLF 的 RUN 指令会报 `/bin/sh^M: not found`**
#   而且那个报错很难看出是行尾引起的。构建在本机跑，行尾问题会**更早**暴露，
#   但报错信息依旧晦涩，所以在构建前先判出来。
#
# ★ 底下刻意**不用** `grep $'\r'`：Git Bash 把含裸 CR 的参数交给 Windows 版
#   grep.exe 时，那个 CR 会被当行终止符吃掉 ⇒ **静默失配**（实测文件里有 255 个 CR、
#   `grep -c $'\r'` 却返回 0，警告永远不触发）。"检测本身静默失效"比"不检测"更危险。
#   ⇒ 改成数字节，不经过参数传递。
#
# 对象：Dockerfile + .dockerignore + 会被 COPY 进镜像的 .py（根 + src/ + tools/）。
crlf=()
scan_crlf() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  # ★★ 显式 return —— 绝不能把 `[[ ... ]]` 当函数最后一条命令：
  #   它在"计数为 0"时返回 1，于是函数返回 1，`set -e` 当场把脚本带崩
  #   （实测：本脚本第一版本就这样，什么都不打印、静默 exit 1）。
  #   ★ 这与 deploy.sh 里 `_same()` 那段血泪史是**同一个坑**（"判据的返回值靠隐式
  #     规则、被中间命令一带就失效"）—— 修法也一样：别依赖隐式返回值。
  if [[ "$(tr -cd '\r' < "$f" | wc -c)" -gt 0 ]]; then
    crlf+=("${f#"$SRC"/}")
  fi
  return 0
}
scan_crlf "$DOCKERFILE"
scan_crlf "$SRC/.dockerignore"
# ★ 逐个目录判断存在性再 find —— `find <不存在的目录>` 会非 0 退出，在 `set -e` +
#   进程替换下会把整个脚本带崩（Part 2 把源码移进 src/ 之前，src/ 就是不存在）。
for _d in "$SRC/src" "$SRC/tools"; do
  [[ -d "$_d" ]] || continue
  while IFS= read -r f; do
    scan_crlf "$f"
  done < <(find "$_d" -maxdepth 2 -name '*.py' 2>/dev/null | sort)
done

if [[ ${#crlf[@]} -gt 0 ]]; then
  echo "⚠ 下列文件是 CRLF 行尾（应为 LF）：" >&2
  printf '    %s\n' "${crlf[@]}" >&2
  echo "  ★ Dockerfile 带 CRLF 的 RUN 会报 /bin/sh^M 找不到；.py 一般无害但同样按 LF 收。" >&2
  echo "  ★ 这是工作区实际状态（SMB 往返 + Windows 编辑器造成），不一定是本次改动引入的。" >&2
  echo "  脚本不擅自改你的文件 —— 请自行落成 LF 后重来。" >&2
  # ★ 行尾**不**作为硬失败（历史上它也一直是警告）——但构建方可以按需改成 fail=1。
fi

# ---------- ③ 本机跑 selftest（可选，设 PREFLIGHT_SELFTEST=1 开） ----------
# ⑧/⑧b 那套判据要 dummy env 才能 import config，这里不重复它的活，
# 只把"构建前先绿一遍"作为可选步骤（默认关，因为 build-push 会单独提示）。
if [[ "${PREFLIGHT_SELFTEST:-0}" == "1" ]]; then
  # ★ 入口在 `src/`（2026-10-04 起源码已移进 src/）。★ 不要 `cd "$SRC"` 再跑
  #   `python run.py` —— 那样会在**仓库根**找 run.py，报 `can't open file`。
  #   直接跑 `python "$SRC/src/run.py"`：脚本目录（= src/）会自动进 sys.path，
  #   满足 `run.py` 顶部的裸 `import config`（见 ERR-META-* 的 src/ 迁移记录）。
  echo "→ 跑 src/run.py --selftest（PREFLIGHT_SELFTEST=1）"
  QB_PASS=x MUSOPIA_COOKIE=x MUSOPIA_PASSKEY=x PICGO_API_KEY=x OPENCD_COOKIE=x \
    DATA_DIR="$SRC/.scratch/selftest-data" \
    python "$SRC/src/run.py" --selftest
fi

if [[ "$fail" -ne 0 ]]; then
  echo "" >&2
  echo "✗ 构建前置自检未通过 —— 别急着 build。修完再来。" >&2
  exit 1
fi

echo "✓ 构建前置自检通过（COPY 源齐全；行尾见上）。"
