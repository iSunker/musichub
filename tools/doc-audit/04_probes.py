#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：把文档声称的事实，从生产上真读一遍。

★★ 安全纪律（**三条同时成立**才算合格，缺一条就是漏洞）：
   ① **只打聚合。** 判「库里有多少条、回填了多少、有几条待办」**只需要计数** ——
      行内容（`torrent_name` / `cover_url` / `tracklist`）对判读零价值，**一律不取**。
      这是第一道，也是最有效的一道。
   ② **不碰 `.env`。** 本文件全程不读任何配置文件里的凭据值；
      探针要用的地址是**写死的公开端点**（qB 的 `/app/version` 不需要认证）。
   ③ **出口统一过 `redact()`。** 万一异常信息里带出什么，在**打印和落盘之前**抹掉 ——
      按**值的形状**兜底，不只按键名（键名匹配会漏掉嵌在异常文本里的凭据）。

   ★ 另外：**探针一律只读**。库用 `mode=ro` + `PRAGMA query_only=1`，
     **直读 UNC、不 `cp` 副本** —— `cp` 会丢掉 WAL 里未 checkpoint 的数据（`ERR-SQL-01`）。

用法：python 04_probes.py
输出：<脚本目录>/out/probes.tsv
"""
import json
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

TOOLS = Path(__file__).resolve().parent
OUT = TOOLS / "out"

NAS = "//iSunker-DS423"
COMPOSE_UNC = "%s/docker_ssd/musopia_script2" % NAS
DB_UNC = COMPOSE_UNC + "/data.db"
HB_UNC = COMPOSE_UNC + "/daemon_heartbeat"
STATE_UNC = COMPOSE_UNC + "/daemon_state.json"

# ★ 本机访问 NAS 上 qB 的地址。**容器内**看到的是 127.0.0.1:3020（host 网络），
#   本机不是容器，得走 NAS 的局域网地址。这个 IP 是**从姊妹项目记的**，
#   本项目没复核过 —— 探针连不上会如实报错，不会假装成功。
QB_URL = "http://192.168.0.7:3020"

# ---- 脱敏（按值的形状兜底）----
REDACT_MORE = [
    (re.compile(r"(?i)(passkey|password|passwd|token|cookie|apikey)(\s*[=:]\s*)([^\s,;&\"']+)"),
     r"\1\2<redacted>"),
    (re.compile(r"\b[0-9a-fA-F]{32,64}\b"), "<hex32+>"),
    (re.compile(r"(?i)Bearer\s+\S+"), "Bearer <redacted>"),
    (re.compile(r"c_secure_pass=[^;\s]+"), "c_secure_pass=<redacted>"),
]


def redact(text):
    s = str(text)
    for rx, rep in REDACT_MORE:
        s = rx.sub(rep, s)
    return s


def _ro(db_unc, timeout=5):
    """只读打开生产库。

    ★ 不能写 `sqlite3.connect("file:%s?mode=ro" % unc, uri=True)` ——
      UNC 的 `//iSunker-DS423` 会被当成 URI 的 authority，
      报 `invalid uri authority: iSunker-DS423`。要么 `file://` + 路径（多两个斜杠），
      要么退回「普通连接 + `PRAGMA query_only=1`」。两条路都真只读；
      URI 那条更硬（连接层就拒绝写 / 不走 WAL 恢复），所以**优先它、失败再退**。
    """
    p = str(db_unc).replace("\\", "/")
    try:
        con = sqlite3.connect("file://" + p + "?mode=ro", uri=True, timeout=timeout)
    except sqlite3.OperationalError:
        con = sqlite3.connect(p, timeout=timeout)
    con.execute("PRAGMA query_only=1")
    return con


# ---- 探针 ----
def qb_version():
    with urllib.request.urlopen(QB_URL + "/api/v2/app/version", timeout=5) as r:
        return r.read().decode().strip()


def db_overview():
    """★ 只出计数，不出任何行内容。"""
    con = _ro(DB_UNC)
    try:
        total = con.execute("SELECT count(*) FROM tasks").fetchone()[0]
        with_id = con.execute(
            "SELECT count(*) FROM tasks WHERE site_torrent_id IS NOT NULL "
            "AND site_torrent_id <> ''").fetchone()[0]
        stale = con.execute(
            "SELECT count(*) FROM tasks WHERE site_synced_at IS NULL "
            "OR updated_at > site_synced_at").fetchone()[0]
        st = con.execute(
            "SELECT site_status, count(*) FROM tasks GROUP BY site_status "
            "ORDER BY count(*) DESC").fetchall()
    finally:
        con.close()
    return ("总 %d 条 · 已回填 site_torrent_id %d 条 · ★ 待办(stale) %d 条\n"
            "                   site_status: %s"
            % (total, with_id, stale,
               " · ".join("%s=%d" % (k if k else "(NULL)", v) for k, v in st)))


def heartbeat_age():
    """daemon_heartbeat 是纯 unix 时间戳，daemon 每轮刷新一次。"""
    raw = Path(HB_UNC.replace("/", "\\")).read_text(encoding="utf-8").strip() \
        if Path(HB_UNC.replace("/", "\\")).exists() else ""
    if not raw:
        return "（读不到 daemon_heartbeat —— 容器现在多半没在跑）"
    ts = int(re.sub(r"[^0-9]", "", raw) or 0)
    age = int(time.time()) - ts
    return "%d 秒前（%.1f 小时前）" % (age, age / 3600.0)


def daemon_stages():
    """各阶段上次跑是什么时候 —— 判「重建会不会触发全阶段」用的。"""
    p = Path(STATE_UNC.replace("/", "\\"))
    if not p.exists():
        return "（读不到 daemon_state.json）"
    data = json.loads(p.read_text(encoding="utf-8"))
    now = time.time()
    out = []
    for k in sorted(data):
        v = data[k]
        lr = v.get("last_run") if isinstance(v, dict) else None
        if isinstance(lr, (int, float)) and lr > 0:
            age = now - lr
            out.append("%s=%.1fh前" % (k, age / 3600.0))
        else:
            out.append("%s=%s" % (k, "从未"))
    return "\n                   ".join(out) if out else "（空）"


def volume_usage():
    """卷水位 —— 只碰已知共享名；不存在的共享会抛错，由上层记下。"""
    out = {}
    for label, unc in (("/volume2 (docker_ssd)", NAS + "/docker_ssd"),
                       ("/volume1 (video)", NAS + "/video")):
        try:
            u = shutil.disk_usage(unc)
            out[label] = "总 %.2f TiB / 已用 %.2f TiB / 可用 %.2f GiB" % (
                u.total / 2**40, u.used / 2**40, u.free / 2**30)
        except Exception as e:                     # noqa: BLE001
            out[label] = "%s: %s" % (type(e).__name__, redact(e))
    return "\n                   ".join("%s: %s" % kv for kv in out.items())


def _timed(fn, secs=20):
    box = {}

    def run():
        try:
            box["v"] = fn()
        except Exception as e:                     # noqa: BLE001
            box["e"] = "%s: %s" % (type(e).__name__, e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(secs)
    if t.is_alive():
        return None, "TIMEOUT after %ds（SMB 可能卡住，别重试太多次）" % secs
    if "e" in box:
        return None, box["e"]
    return box.get("v"), None


PROBES = [
    ("qB :3020 版本", qb_version),
    ("库概览（只计数）", db_overview),
    ("daemon 心跳年龄", heartbeat_age),
    ("daemon 各阶段 last_run", daemon_stages),
    ("卷水位", volume_usage),
]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    lines = ["probe\tactual\terror"]
    for name, fn in PROBES:
        v, err = _timed(fn)
        if err:
            print("[!!] %s: %s" % (name, redact(err)))
            lines.append("%s\t\t%s" % (name, redact(err)))
        else:
            print("[ok] %s: %s" % (name, redact(v)))
            lines.append("%s\t%s\t" % (name, redact(v).replace("\n", " ")))
    (OUT / "probes.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n→ %s" % (OUT / "probes.tsv"))


if __name__ == "__main__":
    main()
