#!/usr/bin/env python3
"""W-E2: event pack materializer (PLAN_EVENT_PACKAGING §3.3/§3.6).

Given an index row (from W-E1's index.parquet), materialize a per-unit pack:
  work/event_packs/data/unit=<unit_key>/{trades.csv, orderbooks_l1.csv}
  work/event_packs/manifests/<unit_key>.json

MONEY-INTEGRITY (D5, audit AF-1/AF-2/AF-3): every price/size/quantity is carried
as its authoritative E4 fixed-point INTEGER column, byte-exact from the
warehouse — NO float division, NO 2dp rounding, NO dollar-string derivation in
v1. `count_e4` is contract quantity (not a row count). (Dollar-readability
strings, if ever added, must be 4dp integer-derived per §3.6 — deliberately
omitted here so there is zero float surface.)

STALE-WINDOW (AF-5): pack ONLY `sealed` units, and re-check actual observed
`max(ts_utc)` at pack time. If real trades exist AFTER the stored `win_end`, the
stored window would clip them: REFUSE by default (fail-closed, S2) — re-run
event_index — or `--refresh` to extend the window to cover them. Never silently
clip.

READ-ONLY on the warehouse; writes only the derived work/event_packs/ tree.
"""
import argparse
import csv
import datetime as _dt
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse as wh  # noqa: E402

# CSV column order (the pack contract). ts_utc + E4 integer columns are
# authoritative; time_utc is a non-money readability helper.
HEADERS = {
    "trades": ["ts_utc", "time_utc", "event_ticker", "series_ticker",
               "market_ticker", "category", "subcategory", "group", "trade_id",
               "yes_price_e4", "no_price_e4", "count_e4", "taker_side"],
    "orderbooks_l1": ["ts_utc", "time_utc", "event_ticker", "series_ticker",
                      "market_ticker", "category", "subcategory", "group",
                      "record_class", "yes_bid_e4", "yes_bid_qty_e4",
                      "yes_ask_e4", "yes_ask_qty_e4", "price_e4", "volume_e4",
                      "open_interest_e4", "is_snapshot"],
}
# authoritative source columns per table (E4 ints selected verbatim, no math)
_SRC = {
    "trades": ["ts_utc", "event_ticker", "series_ticker", "market_ticker",
               "category", "subcategory", "group", "trade_id", "yes_price_e4",
               "no_price_e4", "count_e4", "taker_side"],
    "orderbooks_l1": ["ts_utc", "event_ticker", "series_ticker", "market_ticker",
                      "category", "subcategory", "group", "record_class",
                      "yes_bid_e4", "yes_bid_qty_e4", "yes_ask_e4",
                      "yes_ask_qty_e4", "price_e4", "volume_e4",
                      "open_interest_e4", "is_snapshot"],
}
# Deterministic total order (idempotent rebuild = stable manifest md5). trade_id
# is unique; L1 has no unique key, so tiebreak on every payload column (audit
# Defect-2: record_class is constant within a market, so same-µs L1 rows would
# otherwise reorder under DuckDB's parallel sort).
_ORDER = {"trades": "market_ticker, ts_utc, trade_id",
          "orderbooks_l1": ("market_ticker, ts_utc, record_class, yes_bid_e4, "
                            "yes_bid_qty_e4, yes_ask_e4, yes_ask_qty_e4, price_e4, "
                            "volume_e4, open_interest_e4, is_snapshot")}

# AF-5 finality contract.  A stale-index check that stops on the same UTC day
# as the stored window cannot see next-day late activity.  Archive-only packs
# therefore require one additional *complete sealed UTC day* after the day
# containing win_end.  This deliberately conservative, finite cutoff replaces
# the prior false claim that the archive scan had no upper bound.
AF5_FINALITY_DAYS = 1
DAY_US = 86_400_000_000


def _iso(ts_us):
    if ts_us is None:
        return None
    return _dt.datetime.utcfromtimestamp(int(ts_us) / 1e6).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_utc(ts_us):
    return _dt.datetime.utcfromtimestamp(int(ts_us) / 1e6).strftime("%Y-%m-%d %H:%M:%S")


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _mkt_in(markets):
    return ", ".join("'%s'" % m.replace("'", "''") for m in markets)


def _latest_contiguous_seal_end(warehouse, win_start):
    """Exclusive UTC boundary after the latest contiguous sealed day."""
    import warehouse_common as wc
    cfg = wc.load_config()
    root = warehouse or cfg["warehouse_root"]
    day = _dt.datetime.fromtimestamp(
        win_start / 1e6, tz=_dt.timezone.utc).date()
    today = _dt.datetime.now(_dt.timezone.utc).date()
    last = None
    while day < today and os.path.isfile(wc.seal_path(root, day.isoformat())):
        last = day
        day += _dt.timedelta(days=1)
    if last is None:
        # Let warehouse.load emit the authoritative missing-seal error.
        return ((win_start // DAY_US) + 1) * DAY_US
    return int(_dt.datetime(last.year, last.month, last.day,
                            tzinfo=_dt.timezone.utc).timestamp() * 1e6) + DAY_US


def _required_af5_horizon(win_end):
    """Exclusive cutoff after one full UTC day beyond the window's day."""
    if win_end is None:
        raise ValueError("AF-5 finality requires bounded win_end_us")
    containing_day_end = ((int(win_end) - 1) // DAY_US + 1) * DAY_US
    return containing_day_end + AF5_FINALITY_DAYS * DAY_US


def _load(table, category, start, end, warehouse, archive_only=None):
    try:
        return wh.load(table, category=category, start=start, end=end,
                       warehouse=warehouse, archive_only=archive_only)
    except FileNotFoundError as e:
        # Zero rows for one table is legitimate.  Missing seal/raw/archive
        # evidence is not and must propagate fail-closed.
        if str(e).startswith("no archive data for") or \
           str(e).startswith("no staging or archive data for"):
            return None
        raise


def observed_last_ts(markets, category, win_start, warehouse, archive_only=None,
                     scan_end=None):
    """Actual max(ts_utc) across trades+L1 for the unit's markets, from
    win_start through the explicit exclusive ``scan_end`` evidence horizon.

    Live diagnostic mode may pass ``scan_end=None``.  Archive-only mode is
    bounded by contiguous seals and makes no claim about later activity.
    """
    last = None
    for table in ("trades", "orderbooks_l1"):
        rel = _load(table, category, win_start, scan_end, warehouse, archive_only)
        if rel is None:
            continue
        v = rel.query("f", "SELECT max(ts_utc) FROM f WHERE market_ticker IN (%s)"
                      % _mkt_in(markets)).fetchone()[0]
        if v is not None:
            last = v if last is None else max(last, v)
    return last


def _write_table(table, markets, category, win_start, win_end, out_path, warehouse,
                 archive_only=None):
    header = HEADERS[table]
    rel = _load(table, category, win_start, win_end, warehouse, archive_only)
    rows = []
    if rel is not None:
        q = ("SELECT %s FROM f WHERE market_ticker IN (%s) ORDER BY %s"
             % (", ".join('"%s"' % c if c == "group" else c for c in _SRC[table]),
                _mkt_in(markets), _ORDER[table]))
        rows = rel.query("f", q).fetchall()
    # splice time_utc (non-money) right after ts_utc; everything else verbatim.
    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow([r[0], _time_utc(r[0]), *r[1:]])
    return len(rows)


def build_pack(row, warehouse, out_root, now_us, refresh=False, archive_only=None):
    """Materialize one unit's pack. Returns a manifest dict (status skipped/
    refused/packed)."""
    uk = row["unit_key"]
    if row.get("status") != "sealed":
        return {"unit_key": uk, "status": "skipped",
                "reason": "unit not sealed (status=%s)" % row.get("status")}

    markets = list(row["markets"])
    category = row.get("category")
    win_start, win_end = row["win_start_us"], row["win_end_us"]

    if archive_only is None:
        import warehouse_common as wc
        today_start = wc.day_start_us(_dt.datetime.utcnow().date().isoformat())
        archive_only = bool(win_end is not None and win_end <= today_start)
    scan_end = None
    af5_required_end = None
    if archive_only:
        if win_end is None:
            raise ValueError("archive-only event pack requires bounded win_end_us")
        scan_end = _latest_contiguous_seal_end(warehouse, win_start)
        if scan_end < win_end:
            raise FileNotFoundError("sealed horizon ends before event window")
        af5_required_end = _required_af5_horizon(win_end)
        if scan_end < af5_required_end:
            return {
                "unit_key": uk, "status": "refused",
                "reason": "AF-5 finality unavailable: sealed horizon %d is "
                          "before required post-window horizon %d"
                          % (scan_end, af5_required_end),
            }

    # AF-5: would the stored window clip real trades?
    obs_last = observed_last_ts(markets, category, win_start, warehouse,
                                archive_only, scan_end)
    reinferred = False
    # `>=` not `>`: the extract filters `ts_utc < win_end` (exclusive), so a tick
    # AT win_end would be clipped — refuse it too (audit Defect-1).
    if obs_last is not None and win_end is not None and obs_last >= win_end:
        if not refresh:
            return {"unit_key": uk, "status": "refused",
                    "reason": "stale index: activity at %d >= win_end %d "
                              "(exclusive); re-run event_index or pass --refresh"
                              % (obs_last, win_end)}
        # re-inferred window covers the late activity. +1µs because load()'s end
        # filter is exclusive (ts_utc < end) — must include the last tick itself.
        refreshed_end = obs_last + 1
        if archive_only:
            refreshed_required = _required_af5_horizon(refreshed_end)
            if scan_end < refreshed_required:
                return {
                    "unit_key": uk, "status": "refused",
                    "reason": "AF-5 late activity found, but sealed horizon %d "
                              "does not prove refreshed post-window horizon %d"
                              % (scan_end, refreshed_required),
                }
            af5_required_end = refreshed_required
        win_end = refreshed_end
        reinferred = True

    data_dir = os.path.join(out_root, "data", "unit=%s" % uk.replace("/", "_"))
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(out_root, "manifests"), exist_ok=True)

    counts, files = {}, {}
    for table in ("trades", "orderbooks_l1"):
        p = os.path.join(data_dir, table + ".csv")
        counts[table] = _write_table(table, markets, category, win_start, win_end, p,
                                     warehouse, archive_only)
        files[table + ".csv"] = _md5(p)

    manifest = {
        "unit": row["unit"], "unit_key": uk,
        "event_ticker": row.get("event_ticker"),
        "series_ticker": row.get("series_ticker"),
        "category": category, "markets": markets,
        "win_start_us": win_start, "win_end_us": win_end,
        "window_start_utc": _iso(win_start), "window_end_utc": _iso(win_end),
        "window_source": row.get("window_source"),
        "crossed_day_boundary": bool(row.get("crossed_day_boundary")),
        "status": "packed", "reinferred_window": reinferred,
        "af5_scan_end_us": scan_end,
        "af5_required_horizon_us": af5_required_end,
        "af5_finality_days": AF5_FINALITY_DAYS if archive_only else None,
        "row_counts": counts,                       # ROW cardinality (AF-3/AF-4)
        "files": files,                             # md5 per data file
        "built_at_us": now_us,
        # completeness is asserted by W-E3 event_validate; packs are raw+honest.
    }
    mpath = os.path.join(out_root, "manifests", "%s.json" % uk.replace("/", "_"))
    with open(mpath, "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    manifest["_manifest_path"] = mpath
    return manifest


def _load_index(index_path, unit_key=None):
    import duckdb
    where = "" if unit_key is None else " WHERE unit_key = '%s'" % unit_key.replace("'", "''")
    rel = duckdb.sql("SELECT * FROM read_parquet('%s')%s" % (index_path.replace("'", "''"), where))
    cols = [c for c in rel.columns]
    return [dict(zip(cols, r)) for r in rel.fetchall()]


def update_index_win_end(index_path, updates):
    """Rewrite index.parquet with a new win_end_us for the given unit_keys, so a
    --refresh pack and load(event=) agree on the window (audit Defect-3). Atomic
    (temp file + os.replace); preserves all columns incl. markets VARCHAR[]."""
    import duckdb
    if not updates:
        return
    case = " ".join("WHEN unit_key = '%s' THEN %d" % (k.replace("'", "''"), int(v))
                    for k, v in updates.items())
    tmp = index_path + ".tmp"
    duckdb.sql("COPY (SELECT * REPLACE (CASE %s ELSE win_end_us END AS win_end_us) "
               "FROM read_parquet('%s')) TO '%s' (FORMAT parquet)"
               % (case, index_path.replace("'", "''"), tmp.replace("'", "''")))
    os.replace(tmp, index_path)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", default="work/event_packs/index.parquet")
    ap.add_argument("--unit", help="pack only this unit_key (default: all sealed)")
    ap.add_argument("--warehouse", help="warehouse root (default: config)")
    ap.add_argument("--out-root", default="work/event_packs")
    ap.add_argument("--refresh", action="store_true",
                    help="re-infer window if activity exceeds stored win_end (AF-5)")
    ap.add_argument("--now", help="UTC 'YYYY-MM-DD HH:MM:SS' for deterministic built_at")
    args = ap.parse_args(argv[1:])

    import calendar as _cal
    now_us = (int(_cal.timegm(_dt.datetime.strptime(args.now, "%Y-%m-%d %H:%M:%S")
                              .timetuple()) * 1_000_000) if args.now
              else int(_cal.timegm(_dt.datetime.utcnow().timetuple()) * 1_000_000))

    rows = _load_index(args.index, args.unit)
    packed = skipped = refused = 0
    reinferred = {}
    for row in rows:
        row["markets"] = list(row["markets"]) if row.get("markets") is not None else []
        m = build_pack(row, args.warehouse, args.out_root, now_us, refresh=args.refresh)
        st = m["status"]
        packed += st == "packed"; skipped += st == "skipped"; refused += st == "refused"
        if m.get("reinferred_window"):
            reinferred[m["unit_key"]] = m["win_end_us"]
        if st != "skipped":
            print("  %-8s %-30s %s" % (st, m["unit_key"][:30],
                  m.get("row_counts") or m.get("reason", "")))
    if reinferred:  # keep the index consistent with refreshed packs (Defect-3)
        update_index_win_end(args.index, reinferred)
        print("  updated index win_end for %d reinferred unit(s)" % len(reinferred))
    print("packs: %d packed, %d refused, %d skipped (of %d units)"
          % (packed, refused, skipped, len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
