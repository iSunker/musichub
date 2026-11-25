# -*- coding: utf-8 -*-
"""3010 达标交棒只读诊断工具。

按加入日期统计 3010 任务分布，并与 8 大音乐池（3210~3280）实时任务比对，
定位未交棒原因（未达标、缺同 hash 配对、池内未完成/reseed-mismatch 等）。
"""

import argparse
import datetime
import json
import time
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

from musichub.routing.graph import RoutingGraphManager
from musichub.storage.handoff_governor import HandoffGovernor
from musichub.storage.pool_pairing import POOL_SPECS


def _fetch_live_pools(gov: HandoffGovernor) -> Tuple[Dict[str, tuple], Dict[str, list]]:
    by_hash: Dict[str, tuple] = {}
    by_name: Dict[str, list] = {}
    for name, port, _dir in POOL_SPECS:
        url = f"http://192.168.0.7:{port}/api/v2/torrents/info"
        with gov.opener.open(url, timeout=10) as resp:
            pts = json.loads(resp.read().decode("utf-8"))
        for pt in pts:
            h = (pt.get("hash") or "").lower()
            if h:
                by_hash[h] = (name, port, pt)
            n = pt.get("name") or ""
            if n:
                by_name.setdefault(n, []).append((name, port, pt))
    return by_hash, by_name


def diagnose(db_path: str, dates: List[str], samples_limit: int = 3) -> None:
    gm = RoutingGraphManager(db_path)
    gov = HandoffGovernor(graph_manager=gm)
    torrents = gov.get_publish_torrents()
    print(f"Total 3010 torrents: {len(torrents)}")

    by_date: Dict[str, list] = defaultdict(list)
    for t in torrents:
        dt = datetime.datetime.fromtimestamp(t.get("added_on", 0)).strftime("%Y-%m-%d")
        by_date[dt].append(t)

    for dt in sorted(by_date.keys()):
        print(f"  {dt}: {len(by_date[dt])} tasks")

    live_by_hash, live_by_name = _fetch_live_pools(gov)
    now = time.time()
    target_dates = dates or sorted(by_date.keys())

    for dt in target_dates:
        items = by_date.get(dt, [])
        if not items:
            continue
        print(f"\n=== {dt} ({len(items)} tasks) ===")
        reasons: Counter = Counter()
        samples: Dict[str, list] = defaultdict(list)
        for t in items:
            h = (t.get("hash") or "").lower()
            name = t.get("name", "")
            ok, elig_reason = gov.evaluate_eligible(t, now)
            if not ok:
                key = f"NOT_ELIGIBLE: {elig_reason.split('(')[0].strip()} | state={t.get('state')}"
                reasons[key] += 1
                if len(samples[key]) < samples_limit:
                    samples[key].append(
                        f"{name[:40]} (prog={t.get('progress'):.2f}, "
                        f"seed_h={t.get('seeding_time', 0)/3600:.2f}h, "
                        f"ratio={t.get('ratio'):.2f}, state={t.get('state')})"
                    )
                continue

            if h in live_by_hash:
                pname, pport, pt = live_by_hash[h]
                prog = float(pt.get("progress", 0))
                aleft = pt.get("amount_left")
                size = int(pt.get("size") or 0)
                is_comp = prog >= 1.0 or (aleft is not None and int(aleft) == 0 and size > 0)
                key = f"IN_POOL_HASH_MATCH: complete={is_comp}, pool_state={pt.get('state')}, prog={prog:.2f}"
                reasons[key] += 1
                if len(samples[key]) < samples_limit:
                    samples[key].append(
                        f"{name[:40]} -> {pname}({pport}) state={pt.get('state')} prog={prog:.2f} tags={pt.get('tags')}"
                    )
            elif name in live_by_name:
                key = "NO_HASH_MATCH_BUT_NAME_EXISTS_IN_POOL"
                reasons[key] += 1
                if len(samples[key]) < samples_limit:
                    matches = live_by_name[name]
                    desc = ", ".join(f"{pn}({pp}):st={pt.get('state')}" for pn, pp, pt in matches[:2])
                    samples[key].append(f"{name[:40]} (h={h[:8]}) vs {desc}")
            else:
                key = "MISSING_FROM_ALL_8_POOLS"
                reasons[key] += 1
                if len(samples[key]) < samples_limit:
                    samples[key].append(f"{name[:40]} (h={h[:8]})")

        for k, v in reasons.most_common():
            print(f"  [{v}] {k}")
            for s in samples[k]:
                print(f"      e.g. {s}")


def main() -> None:
    parser = argparse.ArgumentParser(description="3010 达标交棒只读诊断")
    parser.add_argument("--db", default="/app/musichub/data/musichub.db")
    parser.add_argument("--date", action="append", default=[], help="指定 YYYY-MM-DD（可重复）")
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args()
    diagnose(args.db, args.date, args.samples)


if __name__ == "__main__":
    main()
