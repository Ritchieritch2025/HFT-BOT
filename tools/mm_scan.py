#!/usr/bin/env python3
"""Daily market-making candidate scanner (MM_ROADMAP Phase 1).

Ranks every market by maker edge potential for one UTC day:

  score = time_weighted_spread_dollars * trade_count   (edge x opportunity)

subject to liquidity floors (min displayed depth on both sides, min trades).
Reads the warehouse via load() (staging for today, archive for past days) —
read-only, no network, no orders.

Output: ranked table + work/mm/candidates_<date>.csv

Usage:
  python3 tools/mm_scan.py [--date 2026-07-06] [--top 30]
      [--min-trades 20] [--min-depth 30] [--category Sports]
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402
from warehouse import load  # noqa: E402


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=None, help="UTC day (default today)")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--min-trades", type=int, default=20,
                    help="min trades in the day (real taker flow exists)")
    ap.add_argument("--min-depth", type=float, default=30.0,
                    help="min avg displayed contracts on BOTH sides")
    ap.add_argument("--category", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--archive-only", action="store_true",
                    help="never attach live staging (automatic for past days)")
    args = ap.parse_args(argv[1:])
    date = args.date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    try:
        end_date = datetime.date.fromisoformat(date)
    except ValueError:
        ap.error("--date must be YYYY-MM-DD")
    archive_only = (args.archive_only or
                    end_date < datetime.datetime.now(datetime.timezone.utc).date())
    load_kwargs = {"category": args.category, "start": date, "end": date,
                   "archive_only": archive_only}

    l1 = load("orderbooks_l1", **load_kwargs)
    tr = load("trades", **load_kwargs)
    con = l1.connection if hasattr(l1, "connection") else None
    import duckdb
    c = duckdb.connect()
    c.register("l1", l1.df())
    c.register("tr", tr.df())

    sql = """
    WITH book AS (
      SELECT market_ticker, any_value(category) AS category,
             any_value(subcategory) AS subcategory, any_value("group") AS grp,
             count(*) AS l1_updates,
             avg((yes_ask_e4 - yes_bid_e4) / 10000.0)
               FILTER (WHERE yes_bid_e4 IS NOT NULL AND yes_ask_e4 IS NOT NULL
                       AND yes_ask_e4 > yes_bid_e4 AND yes_bid_e4 > 0) AS avg_spread,
             avg(yes_bid_qty_e4 / 10000.0) AS avg_bid_depth,
             avg(yes_ask_qty_e4 / 10000.0) AS avg_ask_depth,
             avg((yes_bid_e4 + yes_ask_e4) / 20000.0) AS avg_mid
      FROM l1 GROUP BY market_ticker),
    flow AS (
      SELECT market_ticker, count(*) AS trades,
             sum(count_e4 / 10000.0) AS contracts,
             sum(CASE WHEN taker_side = 'yes' THEN 1 ELSE -1 END) AS taker_imbalance
      FROM tr GROUP BY market_ticker)
    SELECT b.market_ticker, b.category, b.subcategory, b.grp AS "group",
           round(b.avg_spread, 4) AS avg_spread,
           f.trades, round(f.contracts, 0) AS contracts,
           round(b.avg_bid_depth, 0) AS bid_depth, round(b.avg_ask_depth, 0) AS ask_depth,
           round(b.avg_mid, 3) AS avg_mid, f.taker_imbalance,
           round(b.avg_spread * f.trades, 2) AS score
    FROM book b JOIN flow f USING (market_ticker)
    WHERE f.trades >= %d AND b.avg_bid_depth >= %f AND b.avg_ask_depth >= %f
      AND b.avg_spread IS NOT NULL AND b.avg_spread >= 0.01
      AND b.avg_mid BETWEEN 0.05 AND 0.95
    ORDER BY score DESC LIMIT %d
    """ % (args.min_trades, args.min_depth, args.min_depth, args.top)
    rows = c.execute(sql).fetchall()
    cols = [d[0] for d in c.description]

    out = args.out or os.path.join(wc.ROOT, "work", "mm", "candidates_%s.csv" % date)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    import csv as _csv
    with open(out, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)

    print("NON-GATE: source=%s; capture quality UNASSESSED_PENDING_PIPE_W03; "
          "ranking is descriptive, not profitability evidence."
          % ("ARCHIVE-SEALED" if archive_only else "LIVE/MIXED"))
    print("MM candidates for %s (spread x flow, depth-filtered):" % date)
    print("%-44s %-10s %7s %7s %9s %6s" %
          ("market", "category", "spread", "trades", "contracts", "score"))
    for r in rows:
        d = dict(zip(cols, r))
        print("%-44s %-10s %7.3f %7d %9.0f %6.2f" %
              (d["market_ticker"][:44], (d["category"] or "?")[:10], d["avg_spread"],
               d["trades"], d["contracts"], d["score"]))
    print("\n%d candidate(s) -> %s" % (len(rows), out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
