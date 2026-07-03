#!/usr/bin/env python3

import csv
import math
import sys


def percentile(values, pct):
    if not values:
        return math.nan
    values = sorted(values)
    rank = (len(values) - 1) * pct
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - rank) + values[hi] * (rank - lo)


def main():
    if len(sys.argv) != 2:
        print("usage: analyze_full_chain_latency.py /tmp/full_chain_latency.csv")
        return 2

    columns = {}
    with open(sys.argv[1], newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            for key, value in row.items():
                if key == "trace_id":
                    continue
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if number >= 0:
                    columns.setdefault(key, []).append(number)

    for key in sorted(columns):
        values = columns[key]
        if not values:
            continue
        print(
            f"{key:38s} "
            f"n={len(values):5d} "
            f"p50={percentile(values, 0.50):10.3f}us "
            f"p90={percentile(values, 0.90):10.3f}us "
            f"p99={percentile(values, 0.99):10.3f}us "
            f"max={max(values):10.3f}us"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
