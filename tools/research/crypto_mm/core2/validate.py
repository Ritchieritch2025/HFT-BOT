#!/usr/bin/env python3
"""Validate the sweep winner out-of-sample and emit the verdict pack.

Usage: validate.py <train_dates> <val_dates> [limit]

- takes the best row from sweep_results.json (by objective)
- reruns it on the validation dates (never touched by the sweep)
- writes verdict.json with train/val summaries + equity curves
"""
import json
import sys

from quote_core_v2 import Params
import run_replay


def main():
    train = sys.argv[1].split(",")
    val = sys.argv[2].split(",")
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    rows = json.load(open("sweep_results.json"))
    rows = [r for r in rows if isinstance(r[2], dict) and "pnl_usd" in r[2]]
    rows.sort(key=lambda r: r[0], reverse=True)
    best = rows[0]
    overrides = best[1]
    print("winner:", overrides, "train_obj:", best[0])
    p = Params()
    for k, v in overrides.items():
        setattr(p, k, v)
    out = {"params": vars(p), "winner_overrides": overrides}
    for name, dates in (("train", train), ("val", val)):
        res, curve, s = run_replay.run(dates, p, limit=limit)
        out[name] = {"summary": s, "curve": curve,
                     "per_market": [{k: r[k] for k in
                                     ("ticker", "pnl_c", "fills", "locks",
                                      "locked_c", "taker_cuts")}
                                    for r in res]}
        print(name, s)
    json.dump(out, open("verdict.json", "w"), default=str)
    print("wrote verdict.json")


if __name__ == "__main__":
    main()
