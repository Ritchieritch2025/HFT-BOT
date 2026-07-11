#!/usr/bin/env python3
"""W-E7: operator-facing per-event CSV export (PLAN_EVENT_PACKAGING §3.6).

Three-axis selector — pick ANY single market, single event, or whole series and
get clean, separated CSVs, one folder per event, with a completeness manifest:

  python3 tools/event_export.py --event  KXATPMATCH-25JUL07ABC
  python3 tools/event_export.py --market KXATPMATCH-25JUL07ABC-PLAYER1
  python3 tools/event_export.py --series KXATPMATCH --status sealed

Composes W-E2 (money-safe E4-integer packer) + W-E3 (validator) — it never
re-derives money, so E4 fixed-point is preserved byte-exact (D5, AF-1/2). Writes
under work/event_exports/<series>/<event>/. `_manifest.json.completeness` is
`pass` ONLY when event_validate passes (V-EP15 interior-gap included); a holed
window is `degraded`, never pass. NO mixing events in one file (default).
"""
import argparse
import calendar
import csv
import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import event_pack as ep  # noqa: E402
import event_validate as ev  # noqa: E402


def _now_us(s=None):
    d = _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else _dt.datetime.utcnow()
    return int(calendar.timegm(d.timetuple()) * 1_000_000)


def _read(path):
    if not os.path.exists(path):
        return [], []
    with open(path) as f:
        r = csv.reader(f)
        header = next(r, [])
        return header, list(r)


def _write(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def resolve_targets(axis, key, index_path):
    """Resolve the selector to index rows + an optional single-market filter."""
    import duckdb
    k = key.replace("'", "''")
    if axis == "event":
        where, mfilter = "unit_key = '%s'" % k, None
    elif axis == "market":
        where = "unit_key = '%s' OR list_contains(markets, '%s')" % (k, k)
        mfilter = {key}
    elif axis == "series":
        where, mfilter = "series_ticker = '%s'" % k, None
    else:
        raise ValueError("axis must be event|market|series")
    rows = duckdb.sql(
        "SELECT unit_key, event_ticker, series_ticker, markets, category, "
        "win_start_us, win_end_us, window_source, crossed_day_boundary, status "
        "FROM read_parquet('%s') WHERE %s ORDER BY unit_key"
        % (index_path.replace("'", "''"), where)).fetchall()
    return rows, mfilter


def export_one(idx_row, mfilter, warehouse, pack_root, out_root, now_us, gaps,
               gaps_available, archive_only=None):
    """Pack (W-E2) + validate (W-E3) one unit, then write its operator export
    folder. Returns a status dict."""
    (unit_key, event_ticker, series_ticker, markets, category,
     ws, we, wsrc, crossed, status) = idx_row
    row = {"unit": "market" if unit_key in set(markets) and len(markets) == 1 else "event",
           "unit_key": unit_key, "event_ticker": event_ticker,
           "series_ticker": series_ticker, "markets": list(markets),
           "category": category, "win_start_us": ws, "win_end_us": we,
           "window_source": wsrc, "crossed_day_boundary": crossed, "status": status}
    m = ep.build_pack(row, warehouse, pack_root, now_us,
                      archive_only=archive_only)
    if m["status"] != "packed":
        return {"unit_key": unit_key, "status": m["status"], "reason": m.get("reason", "")}

    data_dir = os.path.join(pack_root, "data", "unit=%s" % unit_key.replace("/", "_"))
    res = ev.validate_pack(m, data_dir, warehouse=warehouse, gaps=gaps,
                           gaps_available=gaps_available, archive_only=archive_only)

    # sanitize path components (defense-in-depth: never let an index ticker with
    # a '/' or '..' escape out_root — matches build_pack's unit_key handling).
    def _safe(s):
        return (s or "").replace("/", "_").replace("..", "_") or "_none"
    dest = os.path.join(out_root, _safe(series_ticker or "_noseries"),
                        _safe(event_ticker or unit_key))
    os.makedirs(dest, exist_ok=True)
    counts = {}
    for table in ("trades", "orderbooks_l1"):
        header, rows = _read(os.path.join(data_dir, table + ".csv"))
        if mfilter is not None and header:
            mi = header.index("market_ticker")
            rows = [r for r in rows if r[mi] in mfilter]
        _write(os.path.join(dest, table + ".csv"), header, rows)
        counts[table] = len(rows)

    exp_markets = sorted(mfilter) if mfilter is not None else list(markets)
    _write(os.path.join(dest, "_event_summary.csv"),
           ["event_ticker", "series_ticker", "category", "markets",
            "window_start_utc", "window_end_utc", "crossed_day_boundary",
            "n_trades", "n_orderbooks_l1", "completeness"],
           [[event_ticker, series_ticker, category, "|".join(exp_markets),
             _iso(ws), _iso(we), bool(crossed),
             counts["trades"], counts["orderbooks_l1"], res["verdict"]]])
    manifest = {
        "selector": {"axis": _axis_of(mfilter), "key": unit_key},
        "series_ticker": series_ticker, "event_ticker": event_ticker,
        "markets": exp_markets,
        "window_start_utc": _iso(ws), "window_end_utc": _iso(we),
        "win_start_us": ws, "win_end_us": we,
        "window_source": wsrc, "crossed_day_boundary": bool(crossed),
        "row_counts": counts,
        "completeness": res["verdict"],          # pass | degraded | fail
        "gaps": res.get("gaps", []),
        "validation": {cid: c["status"] for cid, c in res["checks"].items()},
    }
    with open(os.path.join(dest, "_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return {"unit_key": unit_key, "status": "exported", "dest": dest,
            "completeness": res["verdict"], "row_counts": counts}


def _iso(ts_us):
    return None if ts_us is None else \
        _dt.datetime.utcfromtimestamp(int(ts_us) / 1e6).strftime("%Y-%m-%dT%H:%M:%SZ")


def _axis_of(mfilter):
    return "market" if mfilter is not None else "event"


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--event")
    g.add_argument("--market")
    g.add_argument("--series")
    ap.add_argument("--index", default="work/event_packs/index.parquet")
    ap.add_argument("--warehouse")
    ap.add_argument("--out-root", default="work/event_exports")
    ap.add_argument("--pack-root", default="work/event_packs")
    ap.add_argument("--status", help="for --series: only this seal status (e.g. sealed)")
    ap.add_argument("--gaps", help="structured start_us,end_us CSV (else quality_log)")
    ap.add_argument("--quality-log", default="work/quality_log.ndjson")
    ap.add_argument("--now")
    args = ap.parse_args(argv[1:])

    axis, key = (("event", args.event) if args.event else
                 ("market", args.market) if args.market else ("series", args.series))
    gaps, avail = (ev.load_gaps(args.gaps) if args.gaps
                   else ev.gaps_from_quality_log(args.quality_log))
    rows, mfilter = resolve_targets(axis, key, args.index)
    if args.status and axis == "series":
        rows = [r for r in rows if r[9] == args.status]
    if not rows:
        print("no matching units for %s=%s" % (axis, key))
        return 1
    now_us = _now_us(args.now)
    exported = 0
    for r in rows:
        out = export_one(r, mfilter, args.warehouse, args.pack_root, args.out_root,
                         now_us, gaps, avail)
        exported += out["status"] == "exported"
        print("  %-9s %-30s %s" % (out["status"], out["unit_key"][:30],
              out.get("completeness") or out.get("reason", "")))
    print("exported %d / %d unit(s) -> %s" % (exported, len(rows), args.out_root))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
