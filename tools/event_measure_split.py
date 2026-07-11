#!/usr/bin/env python3
"""Measure calendar-split pain (PLAN_EVENT_PACKAGING W-E0).

READ-ONLY report. Quantifies how many events straddle a UTC-midnight boundary in
the warehouse (the reason day-partitioned archives are the wrong unit for
per-event research). For each event it computes the observed activity span from
trades (min/max ts_utc) and flags events whose first and last tick fall on
different UTC dates, with per-day row counts so the split size is visible.

  python3 tools/event_measure_split.py --category Sports --days 7

Writes work/event_packs/split_report_<UTC-date>.csv (one row per event) and
prints the headline count + top-20 by tick volume. Touches nothing but the
derived work/event_packs/ dir; never reads or writes capture/ingest/export.
"""
import argparse
import csv
import datetime as _dt
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse as wh  # noqa: E402
import warehouse_common as wc  # noqa: E402

US_PER_DAY = 86_400_000_000


def day_index(ts_us):
    """UTC day number since epoch for a microsecond timestamp."""
    return int(ts_us) // US_PER_DAY


def day_str(day_idx):
    return _dt.datetime.utcfromtimestamp(int(day_idx) * 86_400).strftime("%Y-%m-%d")


def us_iso(ts_us):
    return _dt.datetime.utcfromtimestamp(int(ts_us) / 1e6).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_spans(per_event_day):
    """Pure split-detection core (unit-testable, no warehouse).

    Input: iterable of (event, day_idx, min_ts_us, max_ts_us, count[, contracts_e4
           [, notional_e8]]). The two weight fields are optional (default 0) so
           row-count-only callers/tests keep working (AF-2).
    Output: {event: {obs_start_us, obs_end_us, first_day, last_day,
             n_days_present, calendar_span_days, crossed_day_boundary,
             total_ticks (trade ROWS), total_contracts_e4 (Σcount_e4, E4),
             total_notional_e8 (Σ price_e4·count_e4, integer), day_counts}}.
    """
    acc = defaultdict(lambda: {"mn": None, "mx": None, "days": defaultdict(int),
                               "contracts": 0, "notional": 0})
    for rec in per_event_day:
        event, d, mn, mx, cnt = rec[:5]
        contracts = int(rec[5]) if len(rec) > 5 and rec[5] is not None else 0
        notional = int(rec[6]) if len(rec) > 6 and rec[6] is not None else 0
        a = acc[event]
        mn, mx, cnt, d = int(mn), int(mx), int(cnt), int(d)
        a["mn"] = mn if a["mn"] is None else min(a["mn"], mn)
        a["mx"] = mx if a["mx"] is None else max(a["mx"], mx)
        a["days"][d] += cnt
        a["contracts"] += contracts
        a["notional"] += notional
    out = {}
    for event, a in acc.items():
        ds = sorted(a["days"])
        out[event] = {
            "obs_start_us": a["mn"],
            "obs_end_us": a["mx"],
            "first_day": ds[0],
            "last_day": ds[-1],
            "n_days_present": len(ds),
            "calendar_span_days": ds[-1] - ds[0] + 1,
            "crossed_day_boundary": ds[0] != ds[-1],
            "total_ticks": sum(a["days"].values()),
            "total_contracts_e4": a["contracts"],
            "total_notional_e8": a["notional"],
            "day_counts": dict(a["days"]),
        }
    return out


def collect(category, start, end, warehouse=None, archive_only=None):
    """Aggregate trades per (event, UTC-day) via the read-only warehouse loader."""
    rel = wh.load("trades", category=category, start=start, end=end, warehouse=warehouse,
                  archive_only=archive_only,
                  columns=["event_ticker", "series_ticker", '"group"', "category",
                           "ts_utc", "count_e4", "yes_price_e4"])
    # notional Σ(price_e4·count_e4) can exceed int64 over a category — sum as
    # HUGEINT (128-bit); DuckDB returns it to Python as an arbitrary-precision int.
    agg = rel.aggregate(
        "event_ticker, ts_utc // %d AS d, any_value(series_ticker) AS sr, "
        "any_value(\"group\") AS grp, any_value(category) AS cat, "
        "min(ts_utc) AS mn, max(ts_utc) AS mx, count(*) AS n, "
        "sum(count_e4) AS contracts_e4, "
        "sum(CAST(yes_price_e4 AS HUGEINT) * count_e4) AS notional_e8" % US_PER_DAY,
        "event_ticker, ts_utc // %d" % US_PER_DAY)
    rows = agg.fetchall()  # (event, d, sr, grp, cat, mn, mx, n, contracts_e4, notional_e8)
    # event -> (series, group, category) — the REAL per-event category, so a
    # multi-category (--category all) run labels each row correctly.
    meta = {r[0]: (r[2], r[3], r[4]) for r in rows}
    spans = event_spans([(r[0], r[1], r[5], r[6], r[7], r[8], r[9]) for r in rows])
    return spans, meta


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--category", default="Sports",
                    help="category to measure (default Sports); 'all' = no filter")
    ap.add_argument("--days", type=int, default=7,
                    help="look back this many UTC days from now (default 7)")
    ap.add_argument("--start", help="explicit ISO/date start (overrides --days)")
    ap.add_argument("--end", help="explicit ISO/date end")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--out-dir", default="work/event_packs")
    ap.add_argument("--warehouse", help="warehouse root (default: config)")
    args = ap.parse_args(argv[1:])

    category = None if args.category.lower() == "all" else args.category
    if args.start:
        start, end = args.start, args.end
    else:
        now = _dt.datetime.utcnow()
        start = (now - _dt.timedelta(days=args.days)).strftime("%Y-%m-%d")
        end = None

    spans, meta = collect(category, start, end, warehouse=args.warehouse)
    crossed = {e: s for e, s in spans.items() if s["crossed_day_boundary"]}

    def _share(key):
        tot = sum(s[key] for s in spans.values())
        cr = sum(s[key] for s in crossed.values())
        return cr, tot, 100.0 * cr / max(1, tot)

    print("category=%s  window_start=%s  events=%d" %
          (args.category, start, len(spans)))
    print("cross-midnight events: %d / %d (%.1f%%)" % (
        len(crossed), len(spans), 100.0 * len(crossed) / max(1, len(spans))))
    # Three weightings — row count over/under-states $ impact (AF-2). 'ticks' =
    # trade ROWS; contracts = Σcount_e4 (÷1e4 = contracts); notional =
    # Σ price_e4·count_e4 (÷1e8 = $), integer throughout (D5, no float on money).
    for label, key, div in (("trade-rows ", "total_ticks", 1),
                             ("contracts  ", "total_contracts_e4", 10000),
                             ("notional $ ", "total_notional_e8", 100000000)):
        cr, tot, pct = _share(key)
        print("  cross-midnight %s: %s / %s (%.1f%%)"
              % (label, cr // div, tot // div, pct))

    top = sorted(spans.items(), key=lambda kv: kv[1]["total_ticks"], reverse=True)[:args.top]
    print("\ntop %d events by ticks (X = crosses UTC midnight):" % args.top)
    print("  %-1s %-34s %-8s %7s  per-day counts" % ("", "event_ticker", "group", "ticks"))
    for event, s in top:
        sr, grp, cat = meta.get(event, (None, None, None))
        dc = ", ".join("%s:%d" % (day_str(d), s["day_counts"][d]) for d in sorted(s["day_counts"]))
        print("  %s %-34s %-8s %7d  %s" % (
            "X" if s["crossed_day_boundary"] else " ",
            event[:34], (grp or "")[:8], s["total_ticks"], dc))

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = _dt.datetime.utcnow().strftime("%Y-%m-%d")
    out_path = os.path.join(args.out_dir, "split_report_%s.csv" % stamp)
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["event_ticker", "series_ticker", "group", "category",
                    "total_trade_rows", "total_contracts_e4", "total_notional_e8",
                    "obs_start_utc", "obs_end_utc",
                    "n_days_present", "calendar_span_days",
                    "crossed_day_boundary", "day_counts"])
        for event, s in sorted(spans.items(), key=lambda kv: kv[1]["total_ticks"], reverse=True):
            sr, grp, cat = meta.get(event, (None, None, None))
            w.writerow([event, sr, grp, cat, s["total_ticks"],
                        s["total_contracts_e4"], s["total_notional_e8"],
                        us_iso(s["obs_start_us"]), us_iso(s["obs_end_us"]),
                        s["n_days_present"], s["calendar_span_days"],
                        s["crossed_day_boundary"],
                        json.dumps({day_str(d): c for d, c in sorted(s["day_counts"].items())})])
    print("\nwrote %s (%d rows)" % (out_path, len(spans)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
