#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在**容器内**生成 `counts.json`（只写 online 半块 + 源A 需要的 offline.env）。

## 为什么需要这个文件（而不是直接跑 `tools/gen_counts.py`）

`tools/gen_counts.py` 是**本机**生成器：它的
  * `collect_offline()` 要读 `.env.example` / `Dockerfile` / `deploy.sh` / `ERRATA.md`
    并逐个跑 11 个脚本的 `--selftest`；
  * `_db_read()` 要 `importlib` 加载 `tools/doc-audit/audit.py`。

而本项目的镜像**故意不含**这些（`.dockerignore` 排掉 `tools/doc-audit/`，
Dockerfile 只 `COPY src/*.py` + `COPY tools/`，根级 `.env.example`/`Dockerfile`/`ERRATA.md`
**都不在** `/app`）——实测在容器里跑 `gen_counts.py` 会 `FileNotFoundError`。

⇒ 本文件是**容器内的最小替代品**：只产出 `pt-notice` 源 A 真正消费的字段，
且**不重抄任何口径**：

  * `online` 半块 —— **直接复用** `gen_counts.collect_online()`（schema 单一来源）；
    只把它内部的 `_db_read` 换成**容器内可用的等价实现**（见下）。
  * `offline.env.values` —— 取**容器内生效的环境变量**（比 `.env.example` 模板更权威：
    它就是生产真值；源A 的 `env_values()` 只做 `_get`，键名对齐即可）。
  * 待办口径 —— 复用 `db._published_where(only_stale=True)`（**同一事实源**，`ERR-META-03`）。

## 用法（容器内）

    python tools/gen_counts_online.py --out /data/counts.json
    python tools/gen_counts_online.py --out /data/counts.json --dst /data

★ **只读**：`data.db` 走 `PRAGMA query_only=1`；只 SELECT。
★ **原子写**：先写 `.tmp` 再 `os.replace`。
★ **永不抛到调用方**：读不到 ⇒ 落 `online.reachable=false`（保留上一次好读数），
  与 `gen_counts.collect_online` 的既有契约一致。
★ 本文件**不含凭据**：只挑**普通整数/布尔**的开关/间隔键（凭据键一律不取）。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys

# --- 把 `db` 与 `gen_counts` 的所在目录放进 sys.path -------------------------------
#   ★ 两个 import 来源**不同目录**，必须都加：
#     · `gen_counts` 是本文件的**兄弟**（同目录 `tools/`）⇒ 加 `_HERE.parent`；
#     · `db` 在镜像内 = `/app/db.py`（Dockerfile: COPY src/*.py ./ 平铺），
#       本机仓库里在 `src/` ⇒ 两种布局都试。
#   ★ 本机/容器两种布局都兼容（别只在容器里能跑）。
_HERE = pathlib.Path(__file__).resolve()
_CANDIDATES = [
    _HERE.parent,                 # tools/ 自身（为 import gen_counts）：/app/tools 或 <repo>/tools
    _HERE.parent,                 # （同上；gen_counts 与本文件永远同目录）
    _HERE.parent.parent,          # 镜像内：/app/tools → /app（为 import db）
    _HERE.parent.parent / "src",  # 本机：<repo>/src（为 import db）
]
for _p in _CANDIDATES:
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

#: 源 A（pt-notice `sources/_body/a.py.inc::env_values`）要用的键 —— **只这些**。
#: ★ 全是普通整数/布尔，**无一凭据**（凭据纪律：绝不把密钥写进 counts.json）。
ENV_KEYS = (
    "SCHED_TICK", "STAGE_TIMEOUT",
    "SCAN_INTERVAL", "PUBLISH_INTERVAL", "SYNC_INTERVAL", "COVERS_INTERVAL",
    "EDIT_INTERVAL", "VTID_INTERVAL", "RSS_TID_INTERVAL", "CRAWL_INTERVAL",
    "FREE_WINDOW_INTERVAL",
    "ENABLE_SCAN", "ENABLE_PUBLISH", "ENABLE_PUBLISH_CRABPT", "ENABLE_SYNC",
    "ENABLE_COVERS", "ENABLE_EDIT", "ENABLE_VTID", "ENABLE_RSS_TID",
    "ENABLE_CRAWL", "ENABLE_FREE_WINDOW", "ENABLE_HANDOFF", "ENABLE_SEED_PATROL",
)


def _coerce(v: str):
    """字符串 → int / bool / 原串（与 `gen_counts._parse_env_example` 同口径）。"""
    s = (v or "").strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def effective_env_values() -> dict:
    """容器内**生效**的开关/间隔（只取 `ENV_KEYS`；缺的键不进 ⇒ 源A 走 fallback）。

    ★ 与 `gen_counts` 的差别（**有意为之**，不是抄漏）：
      `gen_counts` 读 `.env.example`（模板），因为它在本机跑、拿不到生产 `.env`；
      本文件**在容器里**跑 ⇒ `os.environ` **就是**生产生效值（compose 插值后的结果）。
      ⇒ 这里给的是「生产真值」，源A 的 `env_values()` 只做 `_get`，键名对齐即可。
      ★ 这条差别让源A 的 `disabled_stages()` 含义从「按模板该不该动」升级为
        「生产**真**关没关」——是增强，不是漂移。
    """
    out = {}
    for k in ENV_KEYS:
        raw = os.environ.get(k)
        if raw is None or raw.strip() == "":
            continue
        out[k] = _coerce(raw)
    return out


def _ro_conn(db_path: pathlib.Path, timeout: float = 5.0) -> sqlite3.Connection:
    """只读打开 sqlite（容器内本地路径版）。

    ★ 与 `doc-audit/audit.py::_ro` 同款纪律：**优先 URI 只读**，退「普通连接 + query_only」。
      与它的**唯一**差别：不做 UNC 的 `\\`→`/` 归一（容器内是本地路径，没有 UNC）。
    """
    p = str(db_path)
    try:
        con = sqlite3.connect("file://" + p + "?mode=ro", uri=True, timeout=timeout)
    except sqlite3.OperationalError:
        con = sqlite3.connect(p, timeout=timeout)
    con.execute("PRAGMA query_only=1")
    return con


def _db_read_local(dst) -> dict:
    """容器内可用的 `_db_read`（**替代** gen_counts 里那份要 doc-audit 的）。

    ★ 待办口径**不重抄**：`where/params` 取自 `db._published_where(only_stale=True)`
      （与 `audit.py::_stale_where` **同一事实源**）；字段名与数量与
      `gen_counts._db_read` **逐一对齐**（源A / 日报按这些键读）。
    """
    import db  # 只读；本地 db.py（镜像内 /app/db.py）
    con = _ro_conn(pathlib.Path(dst) / "data.db")
    where, params = db._published_where(only_stale=True)
    try:
        q = lambda sql, p=(): con.execute(sql, p).fetchone()[0]   # noqa: E731
        return {
            "total": q("SELECT count(*) FROM tasks"),
            "exists": q("SELECT count(*) FROM tasks WHERE site_status='EXISTS'"),
            "stale": q("SELECT count(*) FROM tasks WHERE " + " AND ".join(where), params),
            "no_log_file": q("SELECT count(*) FROM tasks WHERE status='NO_LOG_FILE'"),
            "success": q("SELECT count(*) FROM tasks WHERE status='SUCCESS'"),
            "max_updated_at": con.execute(
                "SELECT max(updated_at) FROM tasks").fetchone()[0],
        }
    finally:
        con.close()


def build_doc(dst: pathlib.Path, prev: dict) -> dict:
    """装配出一份**源A 够用**的 counts.json（online 复用 gen_counts，offline 来自 env）。"""
    import gen_counts  # 镜像内 /app/tools/gen_counts.py；import 无副作用（不跑 main）

    # ★★ 把 gen_counts 里**两处只在本机成立的依赖**替换成容器内可用版，
    #    这样 `collect_online` 的**其余**逻辑（stages/heartbeat 组装 + 异常兜底）**原样复用**：
    #    · `_db_read`        → 要 `tools/doc-audit/audit.py`（镜像里**故意没有**）⇒ 换本地只读版；
    #    · `_parse_env_example` → 要根级 `.env.example`（镜像里没有）⇒ 换**容器生效 env**。
    #      ★ 附带好处：`collect_online` 里 `stages[st]["disabled"]` 因此变成
    #        「生产**真**关没关」而不是「按模板该不该动」（更强，见文件头说明）。
    env_vals = effective_env_values()
    prev_patches = {name: getattr(gen_counts, name, None)
                    for name in ("_db_read", "_parse_env_example")}
    gen_counts._db_read = _db_read_local
    gen_counts._parse_env_example = lambda: env_vals
    try:
        intervals = dict(gen_counts.STAGE_INTERVAL_FALLBACK)
        # 若 env 给了 *_INTERVAL 就覆盖（与源A 的 intervals() 优先级一致：env > fallback）
        for st, key in gen_counts.STAGE_INTERVAL_KEYS.items():
            v = os.environ.get(key)
            if v and v.strip().isdigit():
                intervals[st] = int(v.strip())
        online = gen_counts.collect_online(dst, intervals, prev.get("online"))
    finally:
        for name, orig in prev_patches.items():
            if orig is not None:
                setattr(gen_counts, name, orig)   # 还原（别污染同进程里的其它调用）

    return {
        "schema": 1,
        "offline": {"env": {"source": "container-env", "values": effective_env_values()}},
        "online": online,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="在容器内生成 counts.json（online + 源A 的 offline.env）")
    ap.add_argument("--out", required=True, help="输出路径，如 /data/counts.json")
    ap.add_argument("--dst", default=None,
                    help="状态目录（data/）。默认 = config.DATA_DIR 或 /data")
    args = ap.parse_args(argv)

    out = pathlib.Path(args.out)
    if args.dst:
        dst = pathlib.Path(args.dst)
    else:
        try:
            import config
            dst = pathlib.Path(getattr(config, "DATA_DIR", "/data") or "/data")
        except Exception:                                # noqa: BLE001
            dst = pathlib.Path("/data")

    prev = {}
    try:
        if out.exists():
            prev = json.loads(out.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001 —— 坏了当没有
        prev = {}

    try:
        doc = build_doc(dst, prev)
    except Exception as e:                               # noqa: BLE001 —— 绝不抛到调用方
        # 保底：别整份不写；把 online 标成不可达（保留上一次好读数），offline 仍给。
        base_online = dict(prev.get("online") or {})
        base_online.update({"reachable": False, "source_host": str(dst),
                            "skipped_reason": f"{type(e).__name__}: {e}"})
        doc = {"schema": 1,
               "offline": {"env": {"source": "container-env", "values": effective_env_values()}},
               "online": base_online}

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    # ★ newline="\n"：必须，别让 Windows 把它写成 CRLF（与 gen_counts 同款纪律）
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, out)

    on = doc["online"]
    print(f"✅ 写出 {out}（offline 键 {len(doc['offline']['env']['values'])} 个）")
    if on.get("reachable"):
        st = on.get("stages", {})
        d = on.get("db", {})
        print(f"   online  stages={len(st)} · 库 total={d.get('total')} "
              f"exists={d.get('exists')} stale={d.get('stale')} · "
              f"心跳{'活' if (on.get('heartbeat') or {}).get('alive') else '★停'}")
    else:
        print(f"   online  ★没读成：{on.get('skipped_reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
