#!/usr/bin/env python3
"""Build the market segmentation dimension table (PLAN_RESEARCH_CYCLE_1).

`work/research/dim_segments.parquet` — the ONE table every research analysis
joins to for its grouping keys (no ad-hoc per-script slicing). Derived +
rebuildable. Parameterized by category/subcategory/date window so S1 (Tennis)
and S4 (all categories) use the same builder — zero code fork.

Dimension columns (each market_ticker maps to EXACTLY one combination; a
market that cannot be classified lands in an `_unsegmented` bucket, and if
that bucket exceeds 2% of markets the build FAILS, D2):
  category, subcategory   from catalog/series_classified (pinned)
  tour_level              parsed from the series prefix (ATP / WTA / ITF /
                          Challenger; Challenger checked first so
                          KXATPCHALLENGER -> Challenger, not ATP)
  market_kind             win_loss (MATCH) / totals / handicap / other, from
                          the series ticker shape
  tick_stratum            "1c" vs "subcent", from OBSERVED price granularity
                          in L1 (every price on the 1c grid => 1c)
  maker_fee_class         zero (fee_type=quadratic) / charged
                          (quadratic_with_maker_fees) / unknown (series not in
                          the catalog) — fail-closed: unknown is kept as its
                          own class and the analysis drops it (Q3)
  event_id                {series}-{event_id} from the ticker (phase boundary
                          is a per-time detector, computed in the pipeline)

stdlib + duckdb. Read-only over the warehouse; writes only work/research/.
"""
import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import warehouse_common as wc  # noqa: E402
from warehouse import load  # noqa: E402

OUT = os.path.join(wc.ROOT, "work", "research", "dim_segments.parquet")


def _catalog_paths(cfg):
    cat = os.path.join(cfg["warehouse_root"], "catalog")
    sc = os.path.join(cat, "series_classified", "part-00000.parquet")
    ser = glob.glob(os.path.join(cat, "series", "*.parquet"))
    return sc, (ser[0] if ser else None)


def _tour_level_sql(col):
    """SQL CASE deriving tour_level from a series ticker. Challenger first."""
    return ("CASE "
            "WHEN {c} LIKE '%CHALLENGER%' THEN 'Challenger' "
            "WHEN {c} LIKE 'KXATP%' THEN 'ATP' "
            "WHEN {c} LIKE 'KXWTA%' THEN 'WTA' "
            "WHEN {c} LIKE 'KXITF%' THEN 'ITF' "
            "ELSE '_unsegmented' END").format(c=col)


def _market_kind_sql(col):
    """win_loss / totals / handicap / other from the series ticker shape.
    MATCH (and not EXACTMATCH) = the win/loss market."""
    return ("CASE "
            "WHEN {c} LIKE '%EXACTMATCH%' THEN 'other' "
            "WHEN {c} LIKE '%MATCH%' THEN 'win_loss' "
            "WHEN {c} LIKE '%TOTAL%' THEN 'totals' "
            "WHEN {c} LIKE '%SPREAD%' THEN 'handicap' "
            "ELSE 'other' END").format(c=col)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--category", default="Sports")
    ap.add_argument("--subcategory", default="Tennis")
    ap.add_argument("--start", default="2026-07-06")
    ap.add_argument("--end", default="2026-07-08")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv[1:])
    import duckdb

    cfg = wc.load_config()
    sc_pq, ser_pq = _catalog_paths(cfg)
    if not ser_pq:
        print("FAIL: no catalog/series parquet (need fee_type)", file=sys.stderr)
        return 1

    # Distinct markets in the window + their observed 1c-ness, from L1.
    # Run on the warehouse's own connection (the load() relation lives there);
    # a cross-connection register is not allowed.
    import warehouse as wh  # noqa: E402
    l1 = load("orderbooks_l1", category=args.category, subcategory=args.subcategory,
              start=args.start, end=args.end,
              columns=["market_ticker", "series_ticker", "yes_bid_e4", "yes_ask_e4"])
    l1.create_view("l1v", replace=True)
    con = wh._conn()
    # A market is "1c" iff EVERY observed bid/ask sits on the 1c grid (e4 % 100 == 0).
    con.execute("""
      CREATE TEMP TABLE mk AS
      SELECT market_ticker, any_value(series_ticker) AS series_ticker,
             CASE WHEN sum(CASE WHEN (yes_bid_e4 % 100)<>0 OR (yes_ask_e4 % 100)<>0
                                THEN 1 ELSE 0 END) = 0 THEN '1c' ELSE 'subcent' END
             AS tick_stratum,
             count(*) AS l1_rows
      FROM l1v GROUP BY market_ticker""")

    sc_e = sc_pq.replace("'", "''")
    ser_e = ser_pq.replace("'", "''")
    tour = _tour_level_sql("mk.series_ticker")
    kind = _market_kind_sql("mk.series_ticker")
    # event_id = {series}-{event}: strip the final -{outcome} segment.
    con.execute("""
      CREATE TEMP TABLE seg AS
      SELECT mk.market_ticker, mk.series_ticker, mk.tick_stratum, mk.l1_rows,
             coalesce(cl.category, '%s') AS category,
             coalesce(cl.subcategory, '%s') AS subcategory,
             %s AS tour_level,
             %s AS market_kind,
             CASE WHEN se.fee_type = 'quadratic' THEN 'zero'
                  WHEN se.fee_type = 'quadratic_with_maker_fees' THEN 'charged'
                  ELSE 'unknown' END AS maker_fee_class,
             regexp_replace(mk.market_ticker, '-[^-]*$', '') AS event_id
      FROM mk
      LEFT JOIN read_parquet('%s') cl ON cl.series_ticker = mk.series_ticker
      LEFT JOIN read_parquet('%s') se ON se.ticker = mk.series_ticker
    """ % (args.category.replace("'", "''"), args.subcategory.replace("'", "''"),
           tour, kind, sc_e, ser_e))

    n = con.execute("SELECT count(*) FROM seg").fetchone()[0]
    n_unseg = con.execute(
        "SELECT count(*) FROM seg WHERE tour_level='_unsegmented'").fetchone()[0]
    n_unknown_fee = con.execute(
        "SELECT count(*) FROM seg WHERE maker_fee_class='unknown'").fetchone()[0]
    # exactly-one-group invariant: market_ticker is the PK of seg by construction
    dup = con.execute(
        "SELECT count(*) FROM (SELECT market_ticker FROM seg GROUP BY 1 "
        "HAVING count(*)>1)").fetchone()[0]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    con.execute("COPY (SELECT * FROM seg) TO '%s' (FORMAT PARQUET)"
                % args.out.replace("'", "''"))

    print("dim_segments: %d markets -> %s" % (n, args.out))
    print("  tour_level:", con.execute(
        "SELECT tour_level, count(*) FROM seg GROUP BY 1 ORDER BY 2 DESC").fetchall())
    print("  market_kind:", con.execute(
        "SELECT market_kind, count(*) FROM seg GROUP BY 1 ORDER BY 2 DESC").fetchall())
    print("  tick_stratum:", con.execute(
        "SELECT tick_stratum, count(*) FROM seg GROUP BY 1 ORDER BY 2 DESC").fetchall())
    print("  maker_fee_class:", con.execute(
        "SELECT maker_fee_class, count(*) FROM seg GROUP BY 1 ORDER BY 2 DESC").fetchall())
    unseg_pct = 100.0 * n_unseg / n if n else 0.0
    print("  _unsegmented: %d (%.2f%%); unknown-fee: %d; dup-market: %d"
          % (n_unseg, unseg_pct, n_unknown_fee, dup))
    if dup:
        print("FAIL: %d market(s) map to >1 segment" % dup, file=sys.stderr)
        return 1
    if unseg_pct > 2.0:
        print("FAIL: _unsegmented %.2f%% > 2%% — fix the segmentation rules"
              % unseg_pct, file=sys.stderr)
        return 1
    print("SEGMENTS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
