#!/usr/bin/env python3
"""Pricing-model calibration from our own warehouse (MM_ROADMAP Phase 1.5-C).

Measures, per category/subcategory bucket, the three quantities the dynamic
pricing model needs — all from L1 + trades we already collect:

  toxicity   : mean signed mid drift 30s / 120s AFTER a trade, from the taker's
               direction (positive = takers are informed = quoting there is
               expensive). THE deciding number for where naive quoting dies.
  realized vol: stdev of 1-minute mid changes (dollars) — sets quote half-width.
  spread     : time-average touch spread — the gross edge on offer.

Output: table + work/mm/calibration_<date>.csv. Read-only, no network.

Usage:
  python3 tools/mm_calibrate.py [--date D] [--end D] [--min-trades 50]
"""
import argparse
import csv
import datetime
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402
from warehouse import load  # noqa: E402

HORIZONS_S = (30, 120)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--min-trades", type=int, default=50)
    args = ap.parse_args(argv[1:])
    date = args.date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    end = args.end or date

    l1 = load("orderbooks_l1", start=date, end=end,
              columns=["ts_utc", "market_ticker", "category", "subcategory",
                       "yes_bid_e4", "yes_ask_e4"]).df()
    l1 = l1.dropna(subset=["yes_bid_e4", "yes_ask_e4"])
    l1 = l1[(l1.yes_bid_e4 > 0) & (l1.yes_ask_e4 < 10000) &
            (l1.yes_ask_e4 > l1.yes_bid_e4)]
    l1["mid"] = (l1.yes_bid_e4 + l1.yes_ask_e4) / 20000.0
    l1["spread"] = (l1.yes_ask_e4 - l1.yes_bid_e4) / 10000.0
    # log-odds transform: probability contracts must be skewed/measured in
    # logit space (50c->49c is a tiny move in expected return; 2c->1c doubles
    # the odds) — practitioner-confirmed on Kalshi specifically.
    p = np.clip(l1["mid"].values, 0.01, 0.99)
    l1["mid_lo"] = np.log(p / (1 - p))
    tr = load("trades", start=date, end=end,
              columns=["ts_utc", "market_ticker", "category", "subcategory",
                       "taker_side"]).df().dropna(subset=["taker_side"])

    buckets = defaultdict(lambda: {"tox": {h: [] for h in HORIZONS_S},
                                   "tox_lo": {h: [] for h in HORIZONS_S},
                                   "vol": [], "vol_lo": [], "spread": [], "trades": 0})
    for mt, g in l1.groupby("market_ticker", sort=False):
        g = g.sort_values("ts_utc")
        key = (g.category.iloc[0] or "?", g.subcategory.iloc[0] or "?")
        b = buckets[key]
        b["spread"].append(float(g.spread.mean()))
        # 1-minute realized vol of mid, in price AND log-odds space
        ts = g.ts_utc.values // 60_000_000
        mids = g.mid.values
        mids_lo = g.mid_lo.values
        _, idx = np.unique(ts, return_index=True)
        if len(idx) >= 5:
            b["vol"].append(float(np.std(np.diff(mids[idx]))))
            b["vol_lo"].append(float(np.std(np.diff(mids_lo[idx]))))
        # toxicity: signed post-trade drift
        trm = tr[tr.market_ticker == mt]
        if trm.empty:
            continue
        b["trades"] += len(trm)
        t_arr, m_arr, lo_arr = g.ts_utc.values, mids, mids_lo
        for t_ts, side in zip(trm.ts_utc.values, trm.taker_side.values):
            i0 = np.searchsorted(t_arr, t_ts, side="right") - 1
            if i0 < 0:
                continue
            sign = 1.0 if side == "yes" else -1.0
            for h in HORIZONS_S:
                i1 = np.searchsorted(t_arr, t_ts + h * 1_000_000, side="right") - 1
                if i1 > i0:
                    b["tox"][h].append(sign * (m_arr[i1] - m_arr[i0]))
                    b["tox_lo"][h].append(sign * (lo_arr[i1] - lo_arr[i0]))

    date_tag = date if date == end else "%s_%s" % (date, end)
    out = os.path.join(wc.ROOT, "work", "mm", "calibration_%s.csv" % date_tag)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    rows = []
    for (cat, sub), b in buckets.items():
        if b["trades"] < args.min_trades:
            continue
        rows.append({
            "category": cat, "subcategory": sub, "trades": b["trades"],
            "avg_spread": round(float(np.mean(b["spread"])), 4),
            "vol_1min": round(float(np.mean(b["vol"])), 4) if b["vol"] else None,
            "vol_1min_lo": round(float(np.mean(b["vol_lo"])), 4) if b["vol_lo"] else None,
            "tox_30s": round(float(np.mean(b["tox"][30])), 4) if b["tox"][30] else None,
            "tox_120s": round(float(np.mean(b["tox"][120])), 4) if b["tox"][120] else None,
            "tox_120s_lo": round(float(np.mean(b["tox_lo"][120])), 4)
            if b["tox_lo"][120] else None,
            "edge_after_tox_120s": round(
                float(np.mean(b["spread"])) / 2 -
                (float(np.mean(b["tox"][120])) if b["tox"][120] else 0), 4),
        })
    rows.sort(key=lambda r: -(r["edge_after_tox_120s"] or -9))
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["empty"])
        w.writeheader()
        w.writerows(rows)

    print("calibration %s..%s (buckets with >=%d trades)" % (date, end, args.min_trades))
    print("%-22s %-18s %7s %8s %8s %8s %8s %10s" %
          ("category", "subcategory", "trades", "spread", "vol1m",
           "tox30s", "tox120s", "edge-tox"))
    for r in rows:
        print("%-22s %-18s %7d %8.4f %8s %8s %8s %10.4f" %
              (r["category"][:22], r["subcategory"][:18], r["trades"],
               r["avg_spread"], r["vol_1min"], r["tox_30s"], r["tox_120s"],
               r["edge_after_tox_120s"]))
    print("\n%d bucket(s) -> %s" % (len(rows), out))
    print("read: edge_after_tox = half-spread minus 120s toxicity;"
          " positive = quoting there can pay before smarts are added")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
