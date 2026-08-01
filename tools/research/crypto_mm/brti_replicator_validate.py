#!/usr/bin/env python3
"""brti_replicator_validate.py — synthetic vs official BRTI error report.

Usage:
    python3 brti_replicator_validate.py \
        --synthetic "research_fast_anchor/synthetic_brti_*.ndjson" \
        --official  "path/to/rti_capture_*.ndjson" \
        --official-ts-key source_ms --official-val-key rti \
        [--official-ts-scale 0.001] [--lam-grid 0.5,1,2,5,10.3,20]

Aligns both series on whole seconds and reports the error distribution,
overall and stratified by volatility regime (calm vs burst), plus an
optional lambda grid search re-scoring stored mid-curves is NOT possible
post-hoc — the grid here re-weights only via recorded (v_T, synthetic) pairs,
so lambda search requires rerunning the replicator per candidate; this tool
instead reports errors per BRTI_LAMBDA_MODE run directory if several exist.

Acceptance (sources doc): median |err| < $5 and p95 |err| < $15,
in BOTH calm and burst strata.
"""
import argparse
import glob
import json
import statistics


def load(pattern, ts_key, val_key, ts_scale=1.0):
    out = {}
    for path in sorted(glob.glob(pattern)):
        for line in open(path):
            try:
                r = json.loads(line)
                ts = int(float(r[ts_key]) * ts_scale)
                out[ts] = float(r[val_key])
            except (KeyError, ValueError, json.JSONDecodeError):
                continue
    return out


def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    i = min(len(xs) - 1, int(q * len(xs)))
    return xs[i]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", required=True)
    ap.add_argument("--official", required=True)
    ap.add_argument("--official-ts-key", default="source_ms")
    ap.add_argument("--official-val-key", default="rti")
    ap.add_argument("--official-ts-scale", type=float, default=0.001)
    ap.add_argument("--burst-sigma", type=float, default=3.0,
                    help="|1s official move| > k*sigma defines burst stratum")
    a = ap.parse_args()

    syn = load(a.synthetic, "ts", "synthetic_brti")
    off = load(a.official, a.official_ts_key, a.official_val_key,
               a.official_ts_scale)
    common = sorted(set(syn) & set(off))
    if len(common) < 300:
        raise SystemExit(f"only {len(common)} overlapping seconds — need more")

    # volatility stratum from official 1s moves
    moves = [abs(off[t] - off[t - 1]) for t in common if t - 1 in off]
    sigma = statistics.pstdev(moves) if moves else 0.0
    thr = a.burst_sigma * sigma

    strata = {"all": [], "calm": [], "burst": []}
    for t in common:
        err = syn[t] - off[t]
        strata["all"].append(err)
        mv = abs(off[t] - off[t - 1]) if t - 1 in off else 0.0
        strata["burst" if mv > thr else "calm"].append(err)

    print(f"n={len(common)}s overlap  sigma_1s=${sigma:.2f}  "
          f"burst_thr=${thr:.2f}")
    ok = True
    for name, errs in strata.items():
        if not errs:
            continue
        ab = [abs(e) for e in errs]
        med, p95, mx = pct(ab, 0.5), pct(ab, 0.95), max(ab)
        bias = statistics.fmean(errs)
        line = (f"{name:>6}: n={len(errs):6d} bias=${bias:+7.2f} "
                f"med|e|=${med:6.2f} p95|e|=${p95:6.2f} max|e|=${mx:7.2f}")
        if name in ("calm", "burst"):
            passed = med < 5.0 and p95 < 15.0
            ok &= passed
            line += "  " + ("PASS" if passed else "FAIL")
        print(line)
    print("\nVERDICT:", "ELIGIBLE as B-stage anchor candidate" if ok
          else "NOT eligible — keep statistical beta; replicator stays research")


if __name__ == "__main__":
    main()
