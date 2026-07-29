#!/usr/bin/env python3
"""Parameter sweep for quote_core_v2 on a training day.

Runs the grid with a small worker pool, prints a ranked table.
Objective: pnl - 0.5*drawdown (smooth-up preference), report everything.
"""
import itertools
import json
import multiprocessing as mp
import sys

from quote_core_v2 import Params
import run_replay

GRID = {
    "as_base_c": [0.6, 1.0, 1.5],
    "min_net_edge_c": [0.2, 0.4],
    "flow_imb_k": [1.4, 2.0],
}


def one(job):
    dates, overrides, limit = job
    p = Params()
    for k, v in overrides.items():
        setattr(p, k, v)
    try:
        _res, curve, s = run_replay.run(dates, p, limit=limit)
        return overrides, s
    except Exception as e:
        return overrides, {"error": str(e)[:120]}


def main():
    dates = sys.argv[1].split(",") if len(sys.argv) > 1 else ["2026-07-26"]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    keys = sorted(GRID)
    jobs = [(dates, dict(zip(keys, vals)), limit)
            for vals in itertools.product(*(GRID[k] for k in keys))]
    with mp.Pool(3) as pool:
        results = pool.map(one, jobs)
    scored = []
    for ov, s in results:
        if "error" in s:
            print("ERR", ov, s["error"])
            continue
        obj = s["pnl_usd"] - 0.5 * s["max_drawdown_usd"]
        scored.append((obj, ov, s))
    scored.sort(reverse=True, key=lambda x: x[0])
    for obj, ov, s in scored:
        print(f"obj={obj:+.2f} pnl={s['pnl_usd']:+.2f} "
              f"dd={s['max_drawdown_usd']:.2f} locks={s['locks']} "
              f"locked={s['locked_usd']:+.2f} cuts={s['taker_cuts']} "
              f"losers={s['losers']}/{s['markets']} {ov}")
    json.dump([(o, ov, s) for o, ov, s in scored],
              open("sweep_results.json", "w"), default=str)


if __name__ == "__main__":
    main()
