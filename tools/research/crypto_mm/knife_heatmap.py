#!/usr/bin/env python3
"""Falling-knife heatmap: pre-fill fair drift vs post-fill markout drift.

For every FILL, compute:
  dir        = +1 if side=='bid' (long YES), -1 if side=='ask_no' (long NO)
  pre_drift  = dir * (fair(t0) - fair(t0 - PRE_W))      # <0 = market was moving AGAINST
                                                        #      the position we just acquired
  post_drift = dir * (fair(t0 + h) - fair(t0))          # <0 = kept falling after fill (knife)

Heatmap cells: rows = pre_drift bucket, cols = horizon, value = mean post_drift (cents).
Reads mm_*.ndjson engine logs (QUOTE_EVAL + FILL). Usage:
  python3 knife_heatmap.py <glob-or-files...>
"""
import json, glob, sys, bisect, collections

PRE_W = 10.0                     # seconds of pre-fill window
HORIZONS = [5, 15, 30, 60]       # post-fill markout horizons (s)
PRE_BUCKETS = [(-99, -2.0, "<=-2"), (-2.0, -1.0, "-2..-1"), (-1.0, -0.3, "-1..-0.3"),
               (-0.3, 0.3, "-0.3..0.3"), (0.3, 1.0, "0.3..1"), (1.0, 99, ">1")]
TOL_PRE, TOL_POST = 6.0, 12.0    # max staleness (s) of fair snapshots used


def load(files):
    fair = collections.defaultdict(lambda: ([], []))   # mt -> (ts list, fair list)
    fills, seen = [], set()
    for fp in files:
        for line in open(fp):
            h = hash(line)
            if h in seen:
                continue
            seen.add(h)
            try:
                o = json.loads(line)
            except Exception:
                continue
            ev = o.get("ev")
            if ev == "QUOTE_EVAL" and o.get("fair_c") is not None:
                ts = o["wall_ns"] / 1e9
                t, v = fair[o["mt"]]
                t.append(ts); v.append(float(o["fair_c"]))
            elif ev == "FILL":
                fills.append(o)
    return fair, fills


def fair_at(series, ts, direction, tol):
    """direction=-1: last value at/before ts; +1: first at/after ts."""
    t, v = series
    if not t:
        return None
    if direction < 0:
        i = bisect.bisect_right(t, ts) - 1
        if i >= 0 and ts - t[i] <= tol:
            return v[i]
    else:
        i = bisect.bisect_left(t, ts)
        if i < len(t) and t[i] - ts <= tol:
            return v[i]
    return None


def main(patterns):
    files = sorted({f for p in patterns for f in glob.glob(p, recursive=True)})
    fair, fills = load(files)
    print(f"{len(files)} files, {len(fills)} fills, {sum(len(t) for t,_ in fair.values())} fair points")
    rows = []
    for f in fills:
        mt = f.get("ticker") or f.get("mt")
        d = +1 if f["side"] == "bid" else -1
        t0 = (f.get("wall_ns") or int(f["ts_s"] * 1e9)) / 1e9
        s = fair[mt]
        f0 = fair_at(s, t0, -1, TOL_PRE)
        fpre = fair_at(s, t0 - PRE_W, -1, TOL_PRE)
        if f0 is None or fpre is None:
            continue
        pre = d * (f0 - fpre)
        post = {}
        for h in HORIZONS:
            fh = fair_at(s, t0 + h, +1, TOL_POST)
            if fh is not None:
                post[h] = d * (fh - f0)
        if post:
            rows.append((pre, post, f["side"]))
    print(f"{len(rows)} fills with usable pre+post fair series")

    grid = {}
    for lo, hi, name in PRE_BUCKETS:
        sel = [r for r in rows if lo <= r[0] < hi]
        for h in HORIZONS:
            xs = [r[1][h] for r in sel if h in r[1]]
            grid[(name, h)] = (len(xs), sum(xs) / len(xs) if xs else None)

    hdr = "pre-drift ¢ (10s) | " + " | ".join(f"post {h:>3}s" for h in HORIZONS) + " |    n"
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for lo, hi, name in PRE_BUCKETS:
        cells = []
        n0 = grid[(name, HORIZONS[0])][0]
        for h in HORIZONS:
            n, m = grid[(name, h)]
            cells.append(f"{m:+8.2f}" if m is not None else "       -")
        print(f"{name:>17} | " + " | ".join(cells) + f" | {n0:4d}")
    return rows, grid


if __name__ == "__main__":
    main(sys.argv[1:] or ["tmp/*/**/mm_2026*.ndjson"])
