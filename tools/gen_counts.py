"""生成 counts.json —— 项目里**会变的数字**的机器可读真值。

★ 为什么有这个文件：
  原先这些数字散在 SUMMARY.md / ENVIRONMENT.md / README.md / 生成器脚本里，
  同一个量最多抄了 5 份且互不联动（同一个自检数在文档里出现 3 次、
  还被硬编码进 tools/gen_field_table.py 的生成器）。文档里那句
  「会动的量，别写进散文」只在一部分地方被遵守。
  ⇒ 这个脚本把"会动的量"统一成一个**生成的** JSON，文档只负责指路。

★ 设计要点：
  1. **两半分开**：offline（纯本地、随时可重生）/ online（要连 NAS 才读得到）。
     两半各带 generated_at —— 它们的刷新节奏不同。
  2. **数字只从事实源取**，绝不在这里重抄口径：
     - stale 判据 → `db._published_where(only_stale=True)`（借 doc-audit/audit.py（原 04_probes）的桩机制）
     - 自检项数 → 各脚本自己的 `--selftest --json`（不解析人读的 ✅/❌ 行）
       ★ 为什么不正则解析：各脚本分母口径**并不统一**
         （descr/gate/ingest/map/rate/cover 是 ok+fails，opencd parse 是 total_checked；
          2026-10-04 六个 opencd_* 合并成 opencd.py 后口径不变），
         拿一个正则去套多种分母 = 制造一个"看起来很正常的"错数字（ERR-META-01 一族）。
     - 阶段间隔 → `daemon.INTERVALS` 的**同源**（本文件从 config 读，见 _intervals）
  3. **NAS 不可达时保留上一次的 online 块**，只写 reachable=false + skipped_reason。
     否则一次网络抖动会抹掉最后一份好读数。

用法（站在仓库根）:
    python tools/gen_counts.py                 # 生成 counts.json
    python tools/gen_counts.py --offline-only  # 跳过 NAS（CI / 无网时）
    python tools/gen_counts.py --out /tmp/x.json
    DST=//其它/路径 python tools/gen_counts.py  # 覆盖 NAS 目录（见 ERR-DEPLOY-01）

★ 纪律：本文件**只读**生产库（不走 sqlite3 的 URI 写法，见 ERR-SQL-01；
  绝不 cp 生产库）。它不写 data.db、不碰 .env。

★★ **不许依赖 git**（2026-09-22 定）：生产 NAS 上**没装 git**。
  本文件跑在两个地方（开发机 + NAS），任何 `git ...` 调用在 NAS 上都会
  `FileNotFoundError` ⇒ 会把整个生成器带崩，而不是"少一块数据"。
  ⇒ 需要文件清单就**遍历文件系统**（见 `_line_counts`），别问版本控制。
  （同理 `deploy.sh` 里也去掉了 git 提示 —— 那条命令在 NAS 上根本跑不了。）
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import types

sys.stdout.reconfigure(encoding="utf-8")

REPO = pathlib.Path(__file__).resolve().parent.parent
OUT = REPO / "counts.json"

# 与 tools/doc-audit/audit.py 同款：NAS 根 → 部署目录
NAS = "//iSunker-DS423"
# ★ 2026-10-05：生产端改名 `musopia_script2` → `opencd2others`，且状态移进 `data/`
#   子目录（compose `${STATE_DIR:-./data}:/data`）。本工具的 `dst` 口径 = **data 目录**
#   （下面 `dst / "data.db"` 直接拼）。
DEFAULT_DST = f"{NAS}/docker_ssd/opencd2others/data"

# ★ 要跑自检的脚本（键 = 传给 gen_counts 的路径，也是 counts.json 里的标识）
#   ★★ 2026-10-04：`opencd_*` 六个模块已合并成 `opencd.py`（子命令 parse/rate/
#     log/map/gate/ingest）⇒ 这里改成**参数列表**：字符串 = 单脚本，列表 =
#     `python <第一项> <其余参数>`（例：`python opencd.py parse --selftest --json`）。
SELFTEST_SCRIPTS = [
    "descr.py",
    ["opencd.py", "gate"],
    ["opencd.py", "ingest"],
    ["opencd.py", "map"],
    ["opencd.py", "parse"],
    ["opencd.py", "rate"],
    "cover_fetch.py",
    "tools/opencd-crawl-selftest.py",
]

# 阶段间隔：**从 .env.example 的默认值读**，不在这里另抄一份。
#   键名 → 阶段名；找不到就回退到这里的同源默认（与 daemon.py 的 build_stages 一致）。
STAGE_INTERVAL_KEYS = {
    "scan": "SCAN_INTERVAL",
    "publish": "PUBLISH_INTERVAL",
    "sync": "SYNC_INTERVAL",
    "covers": "COVERS_INTERVAL",
    "edit": "EDIT_INTERVAL",
    "crawl": "CRAWL_INTERVAL",
}
STAGE_INTERVAL_FALLBACK = {"scan": 1800, "publish": 1800, "sync": 86400,
                           "covers": 21600, "edit": 21600, "crawl": 1800}
# 已禁用的阶段：last_run 长期不动是预期内的（不是故障）
DISABLED_STAGES: set = set()        # ★ 由 .env.example 的 ENABLE_* 实际值推出，见 _disabled


def resolve_dst():
    """NAS 上**状态目录（data/）**的路径。优先级与 deploy.sh 一致（见 ERR-DEPLOY-01）：
    DST 环境变量 → .deploy.nasrc → 硬编码兜底。★ .nasrc 是默认值，不是强制值。
    ★★ 注意口径差异：`.deploy.nasrc` 里的 `DST=` 是 **compose 目录**（deploy.sh 的
      同步目标），而本工具的 `dst` 要的是其下的 **`data/`**（库/心跳都在那里）
      ⇒ nasrc 分支必须补 `data/`，否则 `dst / "data.db"` 会指到 compose 目录下的
      `data.db`（= 不存在）—— 这正是 `ERR-META-01` 的"路径对不上但静默返回空"形状。
    """
    v = os.environ.get("DST")
    if v:
        return _as_data_dir(pathlib.Path(v))
    nrc = REPO / ".deploy.nasrc"
    if nrc.exists():
        for ln in nrc.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith("DST="):
                return _as_data_dir(pathlib.Path(ln[4:].strip()))
    return pathlib.Path(DEFAULT_DST)


def _as_data_dir(p):
    """把 compose 目录路径归一成其下的 `data/`；若已经指向 `data/` 则原样返回。"""
    return p if p.name == "data" else p / "data"


# ---------------------------------------------------------------- offline

def _parse_env_example():
    """解析 .env.example 的 KEY=VALUE（★ 不读 .env —— 那里面有密钥，不进库）。"""
    p = REPO / ".env.example"
    vals, raw = {}, {}
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k, v = k.strip(), v.strip()
        # 去掉行尾注释（值里没出现过 '#'，安全）
        if " #" in v:
            v = v.split(" #", 1)[0].strip()
        raw[k] = v
    for k, v in raw.items():
        if v.lower() in ("true", "false"):
            vals[k] = v.lower() == "true"
        elif re.fullmatch(r"-?\d+", v):
            vals[k] = int(v)
        elif re.fullmatch(r"-?\d+\.\d+", v):
            vals[k] = float(v)
        else:
            vals[k] = v
    return vals


def _intervals(env):
    return {st: env.get(key, STAGE_INTERVAL_FALLBACK[st])
            for st, key in STAGE_INTERVAL_KEYS.items()}


def _disabled(env):
    """哪些阶段被 `ENABLE_*` 关掉了 ⇒ 它们的 last_run 不动是**预期内**的。

    ★★ 口径边界（必须说清，否则就是个"看起来很正常的"错数字）：
      这里读的是 **`.env.example`（模板）**，不是 NAS 上那份真正的 `.env`
      —— 后者在 `deploy.sh` 的 NEVER 名单里、本机拿不到（ERR-ENV-02）。
      两者**可以不一致**：实测 `.env.example` 的 `ENABLE_PUBLISH=true`，
      而生产 `.env` 里是 `false`。
      ⇒ 所以本字段的含义是「**按模板**该不该动」，不是「生产真的关了吗」。
      要判"生产是不是真的关着"，只能看 `stages[x].last_run` 是否长期不动
      （配合 NAS 上 `docker exec ... env` 或日志，本机读不到）。
    本函数只是**把模板里的事实摊开**，不假装知道生产状态。"""
    out = set()
    for st in STAGE_INTERVAL_KEYS:
        v = env.get(f"ENABLE_{st.upper()}")
        if v is False:
            out.add(st)
    return out


def _selftests():
    """跑每个脚本的 --selftest --json，读**末行** JSON。

    ★ 条目可以是字符串（单脚本）或列表（`[脚本, *前置参数]`）——
      后者给合并后的 `opencd.py <子命令>` 用。键取 JSON 里的 `script` 字段
      （含子命令，如 `opencd.py parse --selftest`），拿不到才退回路径标识。
    """
    out = {}
    for rel in SELFTEST_SCRIPTS:
        parts = [rel] if isinstance(rel, str) else list(rel)
        label = parts[0] + ((" " + " ".join(parts[1:])) if len(parts) > 1 else "")
        cmd = [sys.executable, str(REPO / parts[0]), *parts[1:], "--selftest", "--json"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               cwd=str(REPO), timeout=300)
            last = [l for l in (r.stdout or "").strip().splitlines() if l.strip()]
            data = json.loads(last[-1]) if last else {}
            out[data.get("script", label)] = {
                "ok": data.get("ok"), "total": data.get("total"),
                "exit": r.returncode,
            }
        except Exception as e:                                # noqa: BLE001
            out[label] = {"ok": None, "total": None, "exit": None,
                          "error": f"{type(e).__name__}: {e}"}
    return out


def _sh_array(name):
    """从 deploy.sh 里读 `NAME=( ... )` 数组的值列表。"""
    text = (REPO / "deploy.sh").read_text(encoding="utf-8")
    m = re.search(rf"^{name}=\(\s*(.*?)\s*\)", text, re.S | re.M)
    if not m:
        return []
    body = re.sub(r"#[^\n]*", "", m.group(1))
    return body.split()


def _dockerfile_copy_files():
    """Dockerfile 里 `COPY` 的 .py 文件（★ 跨 `\\` 续行还原）。"""
    text = (REPO / "Dockerfile").read_text(encoding="utf-8")
    text = re.sub(r"\\\s*\n\s*", " ", text)      # 先并掉续行
    files = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln.upper().startswith("COPY"):
            continue
        for tok in ln.split()[1:]:
            if tok.startswith("--"):
                continue
            if tok.endswith(".py"):
                files.append(pathlib.Path(tok).name)
    return sorted(set(files))


def _line_counts():
    """各 `.py` / `.md` 的行数。

    ★ 原先用 `git ls-files` 取文件清单 —— **已改成纯文件系统遍历**：
      (a) 生产 NAS **没装 git**，`subprocess.run(["git", ...])` 会直接
          `FileNotFoundError` ⇒ 把整个生成器带崩（不是"少一块数据"，是整份没生成）；
      (b) 而且"哪些文件算数"本不该依赖版本控制：未跟踪的新文件也是真实存在的代码。
          （本次重构就踩过 -- 用 git 清单时会漏掉新文件。）
    ★ 跳过 `.git` / `.deploy-backup` / `repos`：前两者是噪声与历史快照，
      `repos/` 是**第三方 vendored 代码**，它的行数不反映本项目的规模。
    """
    SKIP_DIRS = {".git", ".deploy-backup", "repos", "opencdhtml",
                 "output_torrents", "__pycache__", "raw_html"}
    files = {}
    for f in sorted(REPO.rglob("*")):
        if not f.is_file() or f.suffix not in (".py", ".md"):
            continue
        rel = f.relative_to(REPO)
        if set(rel.parts) & SKIP_DIRS:
            continue
        try:
            files[rel.as_posix()] = len(f.read_text(
                encoding="utf-8", errors="replace").splitlines())
        except Exception:                                     # noqa: BLE001
            pass
    return dict(sorted(files.items()))


def _errata():
    p = REPO / "ERRATA.md"
    if not p.exists():
        return {"count": 0, "ids": []}
    HEAD_ERR = re.compile(r"^#{2,5}\s+(ERR-[A-Z]+-\d{2})\b")
    ids = [m.group(1) for ln in p.read_text(encoding="utf-8").splitlines()
           if (m := HEAD_ERR.match(ln))]
    return {"count": len(ids), "ids": sorted(ids)}


def collect_offline():
    env = _parse_env_example()
    # ★ 2026-10-06：构建移到本机后，`deploy.sh` 的 `FILES` 白名单**已不存在**
    #   （deploy.sh 退化为弃用说明，只保留 `NEVER`）。所以这里只读 `NEVER`。
    never = _sh_array("NEVER")
    copy_py = _dockerfile_copy_files()
    # ★「Dockerfile 说要 COPY 的 .py」必须真的在磁盘上（本机）。
    #   这是 tools/build-preflight.sh 的同一条判据，在这里出个计数便于巡检。
    copy_py_present = sorted(f for f in copy_py if (REPO / f).exists() or (REPO / "src" / f).exists())
    return {
        "generated_at": _now_iso(),
        "generator": "tools/gen_counts.py",
        "selftests": _selftests(),
        "env": {"source": ".env.example", "values": dict(sorted(env.items())),
                # ★ 说清口径边界：这是**模板**的值，不是生产 `.env` 的值。
                #   `.env` 在 NEVER 名单里、本机拿不到（ERR-ENV-02），
                #   所以「生产是不是真的关着某阶段」本文件**无从得知**。
                "note": ".env.example（模板）的值；生产 .env 本机读不到，"
                        "两者可能不一致 —— stages[].disabled 只表示「按模板该不该动」"},
        "build": {
            # ★ 2026-10-06：本机构建 + 推 GHCR ⇒ 这里出的是**构建**相关的真值
            #   （旧的 `deploy.files` 白名单口径已随 deploy.sh 退场而消失）。
            "never": never,
            "dockerfile_copy_py": copy_py,
            "dockerfile_copy_py_present": copy_py_present,
            "dockerfile_copy_py_missing": sorted(set(copy_py) - set(copy_py_present)),
        },
        "line_counts": _line_counts(),
        "errata": _errata(),
    }


# ---------------------------------------------------------------- online

def _db_read(dst):
    """只读打开 NAS 上的生产库。★ 不 cp（ERR-SQL-01）；UNC 不能塞进 sqlite URI。"""
    import importlib.util
    # ★ 直接复用 doc-audit/audit.py 的 _ro / _stale_where —— 那是全项目最硬的一份只读读法，
    #   而且 _stale_where 会去 import db._published_where ⇒ 口径永不重抄（ERR-META-03）。
    #   ★ 2026-10-05：原先加载的是 `04_probes.py`，四个审计脚本合并成 `audit.py` 后
    #     改指向它（`_ro` / `_stale_where` 名字一字未改，仍在模块级）。
    spec = importlib.util.spec_from_file_location(
        "_probes04", str(REPO / "tools" / "doc-audit" / "audit.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    con = mod._ro(str(pathlib.Path(dst) / "data.db"))
    where, params = mod._stale_where()
    try:
        q = lambda sql, p=(): con.execute(sql, p).fetchone()[0]   # noqa: E731
        db = {
            "total": q("SELECT count(*) FROM tasks"),
            "exists": q("SELECT count(*) FROM tasks WHERE site_status='EXISTS'"),
            "stale": q("SELECT count(*) FROM tasks WHERE " + where, params),
            "no_log_file": q("SELECT count(*) FROM tasks WHERE status='NO_LOG_FILE'"),
            "success": q("SELECT count(*) FROM tasks WHERE status='SUCCESS'"),
            "max_updated_at": con.execute(
                "SELECT max(updated_at) FROM tasks").fetchone()[0],
        }
    finally:
        con.close()
    return db


def collect_online(dst, intervals, prev_online=None):
    now = time.time()
    disabled = _disabled(_parse_env_example())
    try:
        state = json.loads((pathlib.Path(dst) / "daemon_state.json")
                           .read_text(encoding="utf-8"))
        hb_path = pathlib.Path(dst) / "daemon_heartbeat"
        raw = hb_path.read_text(encoding="utf-8").strip()
        hb_content = int(raw) if raw.isdigit() else None
        hb_age = now - max(hb_path.stat().st_mtime, hb_content or 0)
        stages = {}
        for st, iv in intervals.items():
            s = state.get(st) or {}
            lr = s.get("last_run")
            stages[st] = {
                "last_run": lr,
                "age_s": (now - lr) if lr else None,
                "interval_s": iv,
                "overdue": (None if lr is None or st in disabled
                            else (now - lr) > iv),
                "disabled": st in disabled,
                "last_ok": s.get("last_ok"),
                "no_progress": s.get("no_progress"),
            }
        return {
            "generated_at": _now_iso(),
            "reachable": True,
            "source_host": str(dst),
            "heartbeat": {"alive": hb_age < 120, "content_epoch": hb_content,
                          "age_s": hb_age},
            "stages": stages,
            "db": _db_read(dst),
            "skipped_reason": None,
        }
    except Exception as e:                                    # noqa: BLE001
        # ★ 保留上一次的好读数，只标注这次没读成 —— 见文件头设计要点 3
        base = dict(prev_online or {})
        base.update({
            "reachable": False,
            "source_host": str(dst),
            "skipped_reason": f"{type(e).__name__}: {e}",
        })
        return base


def _now_iso():
    """★ 一律写成带 `Z` 的 UTC，**不写本地时间** ——
    本机 python 自认时区是 UTC（`time.timezone == 0`，实测），
    所以 `localtime` 在这里是 no-op；写个不带时区的串会让人以为是 CST。
    这是 ERR-SQL-03 那一族：时间戳必须自带口径，别让人猜。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="生成 counts.json")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--offline-only", action="store_true",
                    help="跳过 NAS（无网 / CI）")
    ap.add_argument("--dst", default=None, help="覆盖 NAS 部署目录")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    prev = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}

    env = _parse_env_example()
    intervals = _intervals(env)

    doc = {"schema": 1, "offline": collect_offline()}

    if args.offline_only:
        doc["online"] = prev.get("online") or {"generated_at": None,
                                               "reachable": None, "skipped_reason":
                                               "未采样（--offline-only）"}
    else:
        doc["online"] = collect_online(pathlib.Path(args.dst) if args.dst
                                       else resolve_dst(), intervals,
                                       prev.get("online"))

    # ★ `newline="\n"` 是**必须的**，不是风格问题：`Path.write_text` 在 Windows 上
    #   会把 `\n` 翻成 `os.linesep`（= CRLF）⇒ 生成物里出现 **317 个 CR**。
    #   后果与 `deploy.sh` 那条 CRLF 门同源（`ERR-OS-01` 一族）：
    #   本机 CRLF、NAS LF ⇒ `cmp` 永远报"有差异"，而差异是**行尾**不是内容，
    #   `deploy.sh --apply` 会反复推一个"看起来改了其实没改"的文件。
    #   ★ 这是**生成器**的责任，别去手改 `counts.json`（它会被重新生成，改了回去）。
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")

    off, on = doc["offline"], doc["online"]
    n_ok = sum(1 for v in off["selftests"].values() if v.get("exit") == 0)
    print(f"✅ 写出 {out}")
    print(f"   offline  自检 {n_ok}/{len(off['selftests'])} 通过 · "
          f"ERR {off['errata']['count']} 条 · COPY {len(off['build']['dockerfile_copy_py'])} 项"
          + (f" · ★缺 {len(off['build']['dockerfile_copy_py_missing'])}"
             if off['build']['dockerfile_copy_py_missing'] else ""))
    if on.get("reachable"):
        d = on["db"]
        print(f"   online   库 total={d['total']} exists={d['exists']} "
              f"stale={d['stale']} · 心跳{'活' if on['heartbeat']['alive'] else '★停'}")
    else:
        print(f"   online   ★ 没读成：{on.get('skipped_reason')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
