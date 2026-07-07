#!/usr/bin/env python3
"""W2.6 first real build (PLAN_GOLD_DATA_CONTRACT W2.6) — gold_build: THIN
COMPOSITION of the frozen W2.1-W2.5 modules into one CLI. No new business
logic lives here: every gate, FSM rule, merge rule, writer check and
validator check is the composed module's, unchanged.

Pipeline: V12 spec-drift gate -> warehouse load() (read-only; the staging DB
writer holds the DuckDB lock in bursts — load() retries) -> W2.1 typed
loaders + quarantine/loader report -> per-(channel, market) sorted slices ->
W2.2 FSM + W2.3 k-way merge -> W2.4 gold_io.write_day -> W2.5
gold_validate.validate_day (any failure => quarantine move + nonzero exit).

COMPOSITION DECISIONS (wiring choices, documented per E2 — not new logic):

1. HEARTBEAT mapping (closes the W2.2/W2.3 BACKLOG note). Warehouse L1 rows
   with is_snapshot=true AND price_e4/volume_e4/open_interest_e4 all NULL are
   the ingester's hourly *scheduler* heartbeats (docs/warehouse_schema.md
   "Heartbeat scheduler": book fields replayed from remembered state,
   price/volume/oi NULL by construction). They are routed through the FULL
   W2.1 L1 gate set as their own source ("orderbooks_l1_heartbeat"), then
   re-kinded via Event._replace(kind=EVENT_TYPE["HEARTBEAT"]) — payload kept
   for forensics, ignored by FSM/merge/writer (V9 neutrality is theirs).
   is_snapshot=true WITH price data = first-observation / lazy hour-tick L1
   rows carrying real state -> L1_TICKER, unchanged. Measured 2026-07-06:
   428,204 scheduler heartbeats out of 8,108,826 L1 rows.

2. Merge-order disposition (closes the W2.3/W2.5 BACKLOG policy question).
   Merge sources are per-(channel, market) slices — a whole channel list is
   NOT ts-sorted because load() orders by (market_ticker, ts_utc), so a
   global per-channel source would violate merge monotonicity across market
   boundaries. Each slice is STABLE-sorted by ts_us at compose time; after
   that a slice cannot be non-monotonic (it was just sorted), so
   GoldMergeError "Merge Order Violation" is structurally unreachable from
   loader output, and same-ts intra-slice order preserves load() row order
   (Timsort stability) — the per-market file-order contract the FSM replay
   needs. Loader-reported ts regressions therefore stay REPORT-ONLY (in the
   loader report, D2) and the day builds; nothing is reordered silently
   across records with distinct timestamps.

3. Liquidity tier (markets_<D>.csv sidecar METADATA, day-scoped, not
   business logic). From the day's accepted TRADE events: per-market summed
   count_e4 (contracts, E4), traded markets ranked descending (ties broken
   by ticker ascending, deterministic). High = top decile of traded markets
   (rank < ceil(0.10*n)); Mid = next decile (rank < ceil(0.20*n)); Low =
   everything else, including markets with zero trades that day.

Other wiring (also E2):
  - V12 gate reads the SAVED work/kalshi_spec_alignment.json: status must be
    "pass" AND generated_at_ms at most 7 days old. Stale/red/missing =>
    REFUSE (exit 2) and tell the operator to rerun
    ./tools/kalshi_spec_sync.py --allow-network — the build NEVER runs the
    network sync itself (offline safety class).
  - Loaders are fed exactly the columns their gates validate; the heartbeat
    discriminator columns and category are consumed at compose time and not
    stored per row (memory: the real day is ~10M rows).
  - Loader outputs (report JSON, malformed-sample CSV, quarantine NDJSON)
    are written INSIDE the day partition, so a validator quarantine MOVES
    them with the day (forensics travel with the failure).
  - dim sidecar fields: category comes from the day's own warehouse rows;
    close_time from <warehouse_root>/dim/latest/markets.csv (missing file or
    ticker => empty field, surfaced by write_day as markets_missing_dim and
    by the printed close_time coverage line — the catalog dim currently
    holds only ~80k open markets, so settled intraday markets resolve no
    close_time; see the 2026-07-07 BACKLOG note).
  - Perf: composed modules are used AS-IS (frozen). The W2.4/W2.5 BACKLOG
    perf notes (per-record Python loops) stand; vectorizing them belongs to
    a W-BENCH follow-up, never to this composition.
  - Exit codes: 0 day GREEN; 1 day failed validation (quarantined); 2 V12
    refusal; any exception = build failure (no partial manifest is ever
    written — gold_io writes the manifest LAST, so readers refuse partials).
"""
import argparse
import csv
import json
import math
import os
import resource
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gold_dtype import EVENT_TYPE          # noqa: E402  frozen W1 constants
import gold_io                             # noqa: E402  W2.4 writer (composed)
import gold_load                           # noqa: E402  W2.1 loaders (composed)
import gold_validate                       # noqa: E402  W2.5 harness (composed)
from gold_merge import merge               # noqa: E402  W2.3 merge (composed)
import warehouse                           # noqa: E402  read-only load()
import warehouse_common as wc              # noqa: E402  config paths only

SPEC_PATH = "work/kalshi_spec_alignment.json"
SPEC_MAX_AGE_DAYS = 7                      # V12: saved result must be this fresh
FETCH_CHUNK = 200_000

_L1_COLS = ("ts_utc", "market_ticker", "yes_bid_e4", "yes_bid_qty_e4",
            "yes_ask_e4", "yes_ask_qty_e4", "is_snapshot",
            "price_e4", "volume_e4", "open_interest_e4", "category")
_FULL_COLS = ("ts_utc", "market_ticker", "msg_type", "side", "price_e4",
              "delta_e4", "yes_levels", "no_levels", "category")
_TRADE_COLS = ("ts_utc", "market_ticker", "trade_id", "yes_price_e4",
               "no_price_e4", "count_e4", "taker_side", "category")


def spec_gate(path, max_age_days=SPEC_MAX_AGE_DAYS):
    """V12 precondition on the SAVED kalshi_spec_sync result. Returns
    (ok, message). Never runs the sync — that is the operator's network
    tool (./tools/kalshi_spec_sync.py --allow-network)."""
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        return False, "saved spec result unreadable (%s): %s" % (path, e)
    status = d.get("status")
    age_days = (time.time() * 1000.0 - d.get("generated_at_ms", 0)) / 86400000.0
    if status != "pass":
        return False, "saved status=%r (need 'pass')" % (status,)
    if age_days > max_age_days:
        return False, "saved result is %.1f days old (max %d)" % (age_days,
                                                                  max_age_days)
    return True, "status=pass, %.2f days old" % age_days


def _fetch(table, date, cols):
    """Stream (chunked) tuples for one warehouse day, read-only via load()."""
    rel = warehouse.load(table, start=date, end=date, columns=list(cols))
    while True:
        batch = rel.fetchmany(FETCH_CHUNK)
        if not batch:
            return
        for t in batch:
            yield t


def _intern(v):
    return sys.intern(v) if isinstance(v, str) else v


def fetch_l1(date, cat):
    """L1 day rows -> (heartbeat_rows, regular_rows) loader dicts.
    Composition decision 1: the scheduler-heartbeat discriminator is
    is_snapshot=true AND NULL price_e4/volume_e4/open_interest_e4."""
    hb, reg = [], []
    for t in _fetch("orderbooks_l1", date, _L1_COLS):
        mt = _intern(t[1])
        if isinstance(mt, str) and isinstance(t[10], str):
            cat.setdefault(mt, sys.intern(t[10]))
        row = {"ts_utc": t[0], "market_ticker": mt,
               "yes_bid_e4": t[2], "yes_bid_qty_e4": t[3],
               "yes_ask_e4": t[4], "yes_ask_qty_e4": t[5],
               "is_snapshot": t[6]}
        is_hb = (t[6] is True and t[7] is None and t[8] is None
                 and t[9] is None)
        (hb if is_hb else reg).append(row)
    return hb, reg


def fetch_full(date, cat):
    rows = []
    for t in _fetch("orderbooks_full", date, _FULL_COLS):
        mt = _intern(t[1])
        if isinstance(mt, str) and isinstance(t[8], str):
            cat.setdefault(mt, sys.intern(t[8]))
        rows.append({"ts_utc": t[0], "market_ticker": mt, "msg_type": t[2],
                     "side": t[3], "price_e4": t[4], "delta_e4": t[5],
                     "yes_levels": t[6], "no_levels": t[7]})
    return rows


def fetch_trades(date, cat):
    rows = []
    for t in _fetch("trades", date, _TRADE_COLS):
        mt = _intern(t[1])
        if isinstance(mt, str) and isinstance(t[7], str):
            cat.setdefault(mt, sys.intern(t[7]))
        rows.append({"ts_utc": t[0], "market_ticker": mt, "trade_id": t[2],
                     "yes_price_e4": t[3], "no_price_e4": t[4],
                     "count_e4": t[5], "taker_side": t[6]})
    return rows


def market_slices(events):
    """One channel's events -> per-market merge sources (composition
    decision 2: stable sort by ts_us; same-ts order = load() row order)."""
    per = {}
    for e in events:
        per.setdefault(e.market_ticker, []).append(e)
    return [sorted(evs, key=lambda e: e.ts_us) for evs in per.values()]


def liquidity_tiers(trade_events):
    """Composition decision 3: day-scoped volume tiering (sidecar metadata)."""
    vol = {}
    for e in trade_events:
        vol[e.market_ticker] = vol.get(e.market_ticker, 0) + e.payload.count_e4
    ranked = sorted(vol.items(), key=lambda kv: (-kv[1], kv[0]))
    n = len(ranked)
    hi, mid = math.ceil(n * 0.10), math.ceil(n * 0.20)
    return {mt: ("High" if r < hi else "Mid" if r < mid else "Low")
            for r, (mt, _) in enumerate(ranked)}


def build_dim(cat, tiers, markets_csv):
    """ticker -> {category, close_time, liquidity_tier} for the sidecar."""
    close = {}
    if os.path.isfile(markets_csv):
        csv.field_size_limit(16_000_000)   # dim rows carry huge custom_strike
        with open(markets_csv, newline="") as f:
            for row in csv.DictReader(f):
                close[row.get("ticker", "")] = row.get("close_time", "") or ""
    else:
        print("WARN: markets dim missing (%s) — close_time left empty"
              % markets_csv, flush=True)
    return {mt: {"category": c or "",
                 "close_time": close.get(mt, ""),
                 "liquidity_tier": tiers.get(mt, "Low")}
            for mt, c in cat.items()}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="W2.6: build + validate one gold day from the warehouse "
                    "(thin composition of gold_load/gold_fsm/gold_merge/"
                    "gold_io/gold_validate).")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD partition date")
    ap.add_argument("--root", default="work/gold")
    ap.add_argument("--spec", default=SPEC_PATH,
                    help="saved kalshi_spec_sync result (V12 gate)")
    a = ap.parse_args(argv)
    t0 = time.monotonic()
    marks = []

    def mark(stage):
        marks.append((stage, time.monotonic() - t0))
        print("[%7.1fs] %s" % (marks[-1][1], stage), flush=True)

    ok, msg = spec_gate(a.spec)
    if not ok:
        print("V12 spec-drift gate: REFUSED — %s" % msg)
        print("Operator: rerun `./tools/kalshi_spec_sync.py --allow-network` "
              "(network_read; this build never auto-runs it), then rebuild.")
        return 2
    print("V12 spec-drift gate: GREEN — %s" % msg, flush=True)

    cat = {}
    hb_rows, l1_rows = fetch_l1(a.date, cat)
    mark("fetched orderbooks_l1: %d regular + %d scheduler-heartbeat rows"
         % (len(l1_rows), len(hb_rows)))
    full_rows = fetch_full(a.date, cat)
    trade_rows = fetch_trades(a.date, cat)
    mark("fetched orderbooks_full: %d rows, trades: %d rows"
         % (len(full_rows), len(trade_rows)))

    l1_events, l1_rep = gold_load.load_l1(l1_rows, source="orderbooks_l1")
    del l1_rows
    hb_src, hb_rep = gold_load.load_l1(hb_rows, source="orderbooks_l1_heartbeat")
    del hb_rows
    hb_events = [e._replace(kind=EVENT_TYPE["HEARTBEAT"]) for e in hb_src]
    del hb_src
    full_events, full_rep = gold_load.load_full(full_rows, source="orderbooks_full")
    del full_rows
    trade_events, tr_rep = gold_load.load_trades(trade_rows, source="trades")
    del trade_rows
    reports = [tr_rep, l1_rep, hb_rep, full_rep]
    mark("loaders done")

    pdir = gold_io.day_paths(a.root, a.date)["dir"]
    outs = gold_load.write_outputs(reports, pdir, tag=a.date)
    summaries = [r.summary() for r in reports]
    del reports                    # quarantined rows persisted above; free them
    print("loader report: %s" % outs["report"])
    for s in summaries:
        print("  %-24s rows_in=%-9d events_out=%-9d Malformed Records=%-7d "
              "Quarantined Input Rows=%-7d ts_regressions=%d"
              % (s["source"], s["rows_in"], s["events_out"],
                 s["Malformed Records"], s["Quarantined Input Rows"],
                 s["ts_regressions_reported"]), flush=True)
        if s["Rejected Rows"]:
            print("      Rejected Rows: %s" % json.dumps(s["Rejected Rows"]))

    tiers = liquidity_tiers(trade_events)
    sources = []
    for evs in (trade_events, l1_events, hb_events, full_events):
        sources.extend(market_slices(evs))
    mark("composed %d per-(channel,market) merge sources" % len(sources))

    records, mrep = merge(sources)
    del sources, trade_events, l1_events, hb_events, full_events
    mark("merge done: %d records" % mrep["records"])
    print("merge report: %s" % json.dumps(mrep, sort_keys=True), flush=True)

    dim = build_dim(cat, tiers,
                    os.path.join(wc.load_config()["warehouse_root"],
                                 "dim", "latest", "markets.csv"))
    n_ct = sum(1 for d in dim.values() if d["close_time"])
    print("dim: %d tickers; close_time resolved for %d (%d empty — surfaced,"
          " never invented)" % (len(dim), n_ct, len(dim) - n_ct), flush=True)
    manifest = gold_io.write_day(
        records, dim, a.date, a.root,
        source_ids={"build_date": a.date,
                    "warehouse": "tools/warehouse.py load() (read-only)",
                    "spec_alignment": {"path": a.spec, "gate": msg},
                    "loader_summaries": summaries})
    del records, dim
    p = gold_io.day_paths(a.root, a.date)
    mark("write_day done: %s (%d bytes), markets sidecar %d rows, "
         "trade_ids sidecar %d rows, markets_missing_dim %d"
         % (p["bin"], os.path.getsize(p["bin"]), manifest["markets"],
            manifest["trade_count"], manifest["markets_missing_dim"]))

    vreport = gold_validate.validate_day(a.root, a.date)
    mark("validate_day done")
    print(gold_validate.format_report(vreport), flush=True)
    if vreport["verdict"] == "GREEN":
        rpath = os.path.join(pdir, "validation_report_%s.json" % a.date)
        with open(rpath, "w") as f:
            json.dump(vreport, f, indent=1, sort_keys=True)
        print("DAY GREEN: all checks passed for %s" % a.date)
        rc = 0
    else:
        dst = gold_validate.quarantine_day(a.root, a.date, vreport)
        rpath = os.path.join(dst, "validation_report_%s.json" % a.date)
        print("DAY QUARANTINED -> %s" % dst)
        rc = 1
    print("validation report: %s" % rpath)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print("timings: %s" % "; ".join("%s=%.1fs" % (s, t) for s, t in marks))
    print("peak RSS: %.2f GB (ru_maxrss=%d)" % (rss / 2**30, rss))
    return rc


if __name__ == "__main__":
    sys.exit(main())
