#!/usr/bin/env python3
"""Maker-edge pilot pipeline (PLAN_RESEARCH_CYCLE_1 S1).

Parameterized, reproducible research pipeline — ONE command:
  materialize slice -> join dim_segments -> markout/decomposition metrics ->
  DQ + results tables + interactive HTML, all under work/research/.

Change --category/--subcategory/--start/--end/--split to get S4/S5 with ZERO
code fork. Read-only over the warehouse; writes only work/research/. Enforces
the PLAN's 12 statistics/credibility disciplines (see PRE_REGISTRATION.md,
written before this runs). columnar (DuckDB), no needless pandas row-loads.

Stages (each checkpointed to a parquet so a long run can resume by --stage):
  slice    : L1 + trades window -> work/research/maker_edge_pilot/{book,trades}.parquet
  markout  : ASOF-join trades to the mid at fill and at t+{1,10,30,120}s;
             bounce/drift/markout_total (discipline #11) + fill classification
             -> markout.parquet
  aggregate: primary metric + exploratory buckets per segment, per-match block
             bootstrap CI, identity self-check -> results tables + HTML
"""
import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import warehouse_common as wc  # noqa: E402
from warehouse import load  # noqa: E402

OUTDIR = os.path.join(wc.ROOT, "work", "research", "maker_edge_pilot")
SEGMENTS = os.path.join(wc.ROOT, "work", "research", "dim_segments.parquet")
HORIZONS_S = (1, 10, 30, 120)
PRIMARY_HORIZON_S = 30
STALENESS_CAP_US = 60_000_000       # discipline #5: mid ≤ 60s stale
NO_MARKET_SPREAD_E4 = 200_000       # discipline #5: spread ≥ 20¢ = no market
MIN_PESSIMISTIC_N = 200             # discipline #10
BOOTSTRAP = 1000                    # discipline #4: per-match block bootstrap
REGIME = "slam-week"


# ─────────────────────────────────────────────────────────── fingerprint

def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(args, materialized):
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wc.ROOT,
                         capture_output=True, text=True).stdout.strip()
    manifest = {os.path.relpath(p, wc.ROOT): (_md5(p) if os.path.exists(p) else None)
                for p in materialized}
    return {"code_sha": sha, "manifest_md5": manifest,
            "command": "python3 " + " ".join(sys.argv), "argv": sys.argv,
            "generated_at": _now(), "regime": REGIME,
            "non_gate": "fees verified=false — research preview only, no facts gate touched (OQ-1)"}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─────────────────────────────────────────────────────── stage: slice

def stage_slice(con, args):
    """Materialize the L1 book + trades slice to parquet (discipline #9:
    partition-filtered, columnar, materialize once)."""
    import warehouse as wh
    l1 = load("orderbooks_l1", category=args.category, subcategory=args.subcategory,
              start=args.start, end=args.end,
              columns=["ts_utc", "market_ticker", "series_ticker",
                       "yes_bid_e4", "yes_ask_e4", "is_snapshot"], ffill=True)
    l1.create_view("l1_raw", replace=True)
    wc_con = wh._conn()
    book_pq = os.path.join(OUTDIR, "book.parquet")
    # valid two-sided book only; mid + spread in E4. LOCF already applied by
    # ffill=True, so every row carries a reconstructed touch.
    wc_con.execute("""
      COPY (
        SELECT ts_utc, market_ticker,
               (yes_bid_e4 + yes_ask_e4)/2.0 AS mid_e4,
               yes_bid_e4, yes_ask_e4, (yes_ask_e4 - yes_bid_e4) AS spread_e4
        FROM l1_raw
        WHERE yes_bid_e4 IS NOT NULL AND yes_ask_e4 IS NOT NULL
          AND yes_bid_e4 > 0 AND yes_ask_e4 < 10000
          AND yes_ask_e4 > yes_bid_e4
        ORDER BY market_ticker, ts_utc
      ) TO '%s' (FORMAT PARQUET)""" % book_pq.replace("'", "''"))

    tr = load("trades", category=args.category, subcategory=args.subcategory,
              start=args.start, end=args.end,
              columns=["ts_utc", "market_ticker", "series_ticker",
                       "yes_price_e4", "count_e4", "taker_side"])
    tr.create_view("tr_raw", replace=True)
    trades_pq = os.path.join(OUTDIR, "trades.parquet")
    wc_con.execute("""
      COPY (
        SELECT ts_utc, market_ticker,
               yes_price_e4, count_e4,
               CASE WHEN taker_side='yes' THEN 1 WHEN taker_side='no' THEN -1
                    ELSE 0 END AS sign
        FROM tr_raw
        WHERE yes_price_e4 IS NOT NULL AND count_e4 IS NOT NULL AND count_e4 > 0
          AND taker_side IN ('yes','no')
      ) TO '%s' (FORMAT PARQUET)""" % trades_pq.replace("'", "''"))
    nb = con.execute("SELECT count(*) FROM read_parquet('%s')"
                     % book_pq.replace("'", "''")).fetchone()[0]
    nt = con.execute("SELECT count(*) FROM read_parquet('%s')"
                     % trades_pq.replace("'", "''")).fetchone()[0]
    print("  slice: book rows=%d, trade rows=%d" % (nb, nt))
    return book_pq, trades_pq


# ───────────────────────────────────────────────────── stage: markout

def stage_markout(con, book_pq, trades_pq):
    """ASOF-join each trade to the mid at fill and at t+h; compute bounce/
    drift/markout (discipline #11) + pessimistic/optimistic fill class.
    Returns the markout parquet path."""
    b = book_pq.replace("'", "''")
    t = trades_pq.replace("'", "''")
    # fill-time book (last book <= trade ts) + staleness. mid in CENTS.
    horizon_joins = []
    horizon_cols = []
    for h in HORIZONS_S:
        alias = "b%d" % h
        horizon_joins.append(
            "ASOF LEFT JOIN read_parquet('%s') %s "
            "ON f.market_ticker = %s.market_ticker "
            "AND (f.ts_utc + %d) >= %s.ts_utc" % (b, alias, alias, h * 1_000_000, alias))
        horizon_cols.append("%s.mid_e4/100.0 AS mid_h%d" % (alias, h))
    sql = """
      WITH fill AS (
        SELECT tr.ts_utc, tr.market_ticker, tr.sign, tr.count_e4,
               tr.yes_price_e4/100.0 AS p_fill,
               bk.mid_e4/100.0 AS mid_t,
               bk.yes_bid_e4/100.0 AS bid_c, bk.yes_ask_e4/100.0 AS ask_c,
               bk.spread_e4, bk.ts_utc AS book_ts
        FROM read_parquet('%s') tr
        ASOF LEFT JOIN read_parquet('%s') bk
          ON tr.market_ticker = bk.market_ticker AND tr.ts_utc >= bk.ts_utc
      )
      SELECT f.*, %s
      FROM fill f
      %s
    """ % (t, b, ", ".join(horizon_cols), "\n      ".join(horizon_joins))
    con.execute("CREATE TEMP TABLE mk_raw AS " + sql)

    # DQ drop classification (discipline #5) — every drop counted, none silent.
    drop_case = (
        "CASE "
        "WHEN mid_t IS NULL THEN 'no_book_before' "
        "WHEN (ts_utc - book_ts) > %d THEN 'stale_book_gt60s' "
        "WHEN spread_e4 >= %d THEN 'no_market_wide_spread' "
        "ELSE 'ok' END" % (STALENESS_CAP_US, NO_MARKET_SPREAD_E4))
    # bounce (horizon-independent) + per-horizon drift/markout/self-check, cents.
    metric_cols = ["sign*(p_fill - mid_t) AS bounce"]
    for h in HORIZONS_S:
        metric_cols.append("sign*(mid_h%d - mid_t) AS drift_%d" % (h, h))
        metric_cols.append("sign*(p_fill - mid_t) + sign*(mid_h%d - mid_t) "
                           "AS markout_%d" % (h, h))
        # independent recompute for the identity self-check (discipline #11)
        metric_cols.append("sign*(p_fill - 2*mid_t + mid_h%d) AS markout_chk_%d"
                           % (h, h))
    # pessimistic fill: trade price strictly THROUGH the touch (back of queue).
    # optimistic: at the touch. (join-the-touch maker)
    fill_class = (
        "CASE "
        "WHEN sign=1 AND p_fill > ask_c THEN 'pessimistic' "     # lifted through ask
        "WHEN sign=1 AND p_fill = ask_c THEN 'optimistic' "
        "WHEN sign=-1 AND p_fill < bid_c THEN 'pessimistic' "    # hit through bid
        "WHEN sign=-1 AND p_fill = bid_c THEN 'optimistic' "
        "ELSE 'inside_or_other' END")
    half_spread = "(spread_e4/2.0)/100.0"   # cents

    mkq = """
      SELECT ts_utc, market_ticker, sign, count_e4, p_fill, mid_t, bid_c, ask_c,
             spread_e4, book_ts,
             %s AS dq_class,
             %s AS half_spread_c,
             %s AS fill_class,
             %s,
             regexp_replace(market_ticker, '-[^-]*$', '') AS event_id
      FROM mk_raw
    """ % (drop_case, half_spread, fill_class, ", ".join(metric_cols))
    out = os.path.join(OUTDIR, "markout.parquet")
    con.execute("COPY (%s) TO '%s' (FORMAT PARQUET)" % (mkq, out.replace("'", "''")))
    n = con.execute("SELECT count(*) FROM read_parquet('%s')"
                    % out.replace("'", "''")).fetchone()[0]
    print("  markout: %d trades scored" % n)
    return out


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--category", default="Sports")
    ap.add_argument("--subcategory", default="Tennis")
    ap.add_argument("--start", default="2026-07-06")
    ap.add_argument("--end", default="2026-07-08")
    ap.add_argument("--train-end", default="2026-07-07",
                    help="last train date; dates after are val (measure-only)")
    ap.add_argument("--stage", default="all",
                    choices=("all", "slice", "markout", "aggregate"))
    args = ap.parse_args(argv[1:])
    import duckdb
    os.makedirs(OUTDIR, exist_ok=True)
    con = duckdb.connect()

    book_pq = os.path.join(OUTDIR, "book.parquet")
    trades_pq = os.path.join(OUTDIR, "trades.parquet")
    if args.stage in ("all", "slice"):
        book_pq, trades_pq = stage_slice(con, args)
        if args.stage == "slice":
            return 0
    if args.stage in ("all", "markout"):
        stage_markout(con, book_pq, trades_pq)
        if args.stage == "markout":
            return 0
    if args.stage in ("all", "aggregate"):
        import aggregate_maker_edge as agg  # split for length; same package
        return agg.run(con, args, fingerprint(args, [SEGMENTS, book_pq, trades_pq,
                       os.path.join(OUTDIR, "markout.parquet")]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
