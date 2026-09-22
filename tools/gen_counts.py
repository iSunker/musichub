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
     - stale 判据 → `db._published_where(only_stale=True)`（借 04_probes 的桩机制）
     - 自检项数 → 各脚本自己的 `--selftest --json`（不解析人读的 ✅/❌ 行）
       ★ 为什么不正则解析：各脚本分母口径**并不统一**
         （descr/gate/ingest/map/rate/cover 是 ok+fails，opencd_parse 是 total_checked），
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

# 与 tools/doc-audit/04_probes.py 同款：NAS 根 → 部署目录
NAS = "//iSunker-DS423"
DEFAULT_DST = f"{NAS}/docker_ssd/musopia_script2"

# ★ 要跑自检的脚本（键 = 传给 gen_counts 的路径，也是 counts.json 里的标识）
SELFTEST_SCRIPTS = [
    "descr.py",
    "opencd_gate.py",
    "opencd_ingest.py",
    "opencd_map.py",
    "opencd_parse.py",
    "opencd_rate.py",
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
    """NAS 部署目录。优先级与 deploy.sh 一致（见 ERR-DEPLOY-01）：
    DST 环境变量 → .deploy.nasrc → 硬编码兜底。★ .nasrc 是默认值，不是强制值。"""
    v = os.environ.get("DST")
    if v:
        return pathlib.Path(v)
    nrc = REPO / ".deploy.nasrc"
    if nrc.exists():
        for ln in nrc.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith("DST="):
                return pathlib.Path(ln[4:].strip())
    return pathlib.Path(DEFAULT_DST)


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
    """跑每个脚本的 --selftest --json，读**末行** JSON。"""
    out = {}
    for rel in SELFTEST_SCRIPTS:
        cmd = [sys.executable, str(REPO / rel), "--selftest", "--json"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               cwd=str(REPO), timeout=300)
            last = [l for l in (r.stdout or "").strip().splitlines() if l.strip()]
            data = json.loads(last[-1]) if last else {}
            out[data.get("script", rel)] = {
                "ok": data.get("ok"), "total": data.get("total"),
                "exit": r.returncode,
            }
        except Exception as e:                                # noqa: BLE001
            out[rel] = {"ok": None, "total": None, "exit": None,
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
    files, never = _sh_array("FILES"), _sh_array("NEVER")
    copy_py = _dockerfile_copy_files()
    return {
        "generated_at": _now_iso(),
        "generator": "tools/gen_counts.py",
        "selftests": _selftests(),
        "env": {"source": ".env.example", "values": dict(sorted(env.items())),
                # ★ 说清口径边界：这是**模板**的值，不是生产 `.env` 的值。
                #   `.env` 在 deploy.sh 的 NEVER 名单里、本机拿不到（ERR-ENV-02），
                #   所以「生产是不是真的关着某阶段」本文件**无从得知**。
                "note": ".env.example（模板）的值；生产 .env 不在白名单、本机读不到，"
                        "两者可能不一致 —— stages[].disabled 只表示「按模板该不该动」"},
        "deploy": {
            "files": files, "never": never, "readme_deployed": "README.md" in files,
            "dockerfile_copy_py": copy_py,
            # ★ 两向差集都出，因为 deploy.sh 的自检只查了一个方向
            "dockerfile_copy_missing_from_whitelist":
                sorted(set(copy_py) - set(files)),
            "whitelist_missing_from_dockerfile":
                sorted(set(f for f in files if f.endswith(".py")) - set(copy_py)),
        },
        "line_counts": _line_counts(),
        "errata": _errata(),
    }


# ---------------------------------------------------------------- online

def _db_read(dst):
    """只读打开 NAS 上的生产库。★ 不 cp（ERR-SQL-01）；UNC 不能塞进 sqlite URI。"""
    import importlib.util
    # ★ 直接复用 04_probes 的 _ro / _stale_where —— 那是全项目最硬的一份只读读法，
    #   而且 _stale_where 会去 import db._published_where ⇒ 口径永不重抄（ERR-META-03）。
    spec = importlib.util.spec_from_file_location(
        "_probes04", str(REPO / "tools" / "doc-audit" / "04_probes.py"))
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
          f"ERR {off['errata']['count']} 条 · 白名单 {len(off['deploy']['files'])} 项")
    if on.get("reachable"):
        d = on["db"]
        print(f"   online   库 total={d['total']} exists={d['exists']} "
              f"stale={d['stale']} · 心跳{'活' if on['heartbeat']['alive'] else '★停'}")
    else:
        print(f"   online   ★ 没读成：{on.get('skipped_reason')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
