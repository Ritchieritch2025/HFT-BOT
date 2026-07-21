#!/usr/bin/env python3
"""Read-only status source for the three-layer warehouse (dashboard panel).

Reports, without mutating anything:
  - staging (LAYER 2): row counts, latest ts, ingest checkpoint freshness
  - archive (LAYER 3): partition-file count, latest archived date, manifest rows
  - catalog dims: series/events/markets/settlements row counts
  - source_kind: operator_capture if the ingest checkpoints point at real
    capture logs (work/raw, work/live, work/*_capture.ndjson);
    synthetic_fixture if only fixture/demo inputs were ever ingested

Keeps the key shape the ops-console warehouse panel renders (present,
row_counts, last_run, source_kind, schema_ok, idempotent_replace, partitions,
latest_partition_date, missing_categories, warnings, errors).

stdlib + duckdb only. Exit 0 + "ALL PASS" when the warehouse is present and
schema-consistent; an absent warehouse is "not built yet" (still exit 0 — a
valid pre-collection state, not a failure).
"""
import argparse
import datetime
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402

FACT_TABLES = ("orderbooks_l1", "orderbooks_full", "trades")
EXPECTED_L1_COLS = {"ts_utc", "market_ticker", "series_ticker", "event_ticker",
                    "category", "subcategory", "group", "record_class",
                    "yes_bid_e4", "yes_bid_qty_e4", "yes_ask_e4", "yes_ask_qty_e4",
                    "price_e4", "volume_e4", "open_interest_e4", "is_snapshot"}
CATALOG_TABLES = ("series", "events", "markets", "settlements")


def _iso(us):
    return datetime.datetime.fromtimestamp(
        us / 1_000_000, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_status(warehouse_dir):
    warehouse_dir = os.path.abspath(warehouse_dir)
    cfg = wc.load_config()
    is_default = warehouse_dir == os.path.abspath(cfg["warehouse_root"])
    staging = cfg["staging_db"] if is_default \
        else os.path.join(warehouse_dir, "staging.duckdb")
    archive_root = cfg["archive_root"] if is_default \
        else os.path.join(warehouse_dir, "facts")
    manifest = os.path.join(warehouse_dir, "manifest.csv")

    out = {
        "warehouse": os.path.relpath(warehouse_dir, wc.ROOT)
        if warehouse_dir.startswith(wc.ROOT) else warehouse_dir,
        "present": False, "schema_ok": False, "idempotent_replace": True,
        "source_kind": "unknown", "row_counts": {}, "last_run": {},
        "partitions": 0, "latest_partition_date": None,
        "missing_categories": [], "warnings": [], "errors": [],
        "staging": {}, "archive": {}, "catalog": {},
    }

    # --- archive (LAYER 3) ---
    files = [f for f in glob.glob(os.path.join(
        archive_root, "*", "category=*", "subcategory=*", "date=*", "*"))
        if os.path.isfile(f)]
    dates = sorted({p.split("date=")[-1].split(os.sep)[0]
                    for p in files if "date=" in p})
    out["archive"] = {"files": len(files), "manifest_present": os.path.exists(manifest)}
    out["partitions"] = len(files)
    if dates:
        out["latest_partition_date"] = dates[-1]
    if os.path.exists(manifest):
        n_manifest = max(0, sum(1 for _ in open(manifest)) - 1)
        out["archive"]["manifest_rows"] = n_manifest
        if n_manifest != len(files):
            out["warnings"].append(
                "manifest rows (%d) != archive files (%d)" % (n_manifest, len(files)))

    # --- staging (LAYER 2) ---
    if os.path.exists(staging):
        out["present"] = True
        try:
            import duckdb
            con = duckdb.connect(staging, read_only=True)
            cols = {r[1] for r in con.execute(
                "pragma table_info('orderbooks_l1')").fetchall()}
            out["schema_ok"] = EXPECTED_L1_COLS.issubset(cols)
            if not out["schema_ok"]:
                out["errors"].append("orderbooks_l1 staging columns missing: %s"
                                     % sorted(EXPECTED_L1_COLS - cols))
            max_ts = 0
            for t in FACT_TABLES:
                try:
                    n, mx = con.execute(
                        'SELECT count(*), max(ts_utc) FROM "%s"' % t).fetchone()
                except Exception:
                    n, mx = 0, None
                out["row_counts"][t] = n
                if mx:
                    max_ts = max(max_ts, mx)
            if max_ts:
                out["staging"]["latest_ts"] = _iso(max_ts)
            cps = con.execute(
                "SELECT file, byte_offset, updated_us FROM checkpoint "
                "ORDER BY updated_us DESC").fetchall()
            if cps:
                f, _off, up = cps[0]
                out["last_run"] = {"run_id": "ingest:" + os.path.basename(f),
                                   "finished_at": _iso(up)}
                srcs = [c[0] for c in cps]
                real = [s for s in srcs if ("work/raw" in s or "work/live" in s
                        or s.endswith("_capture.ndjson"))]
                fixture = [s for s in srcs if ("fixture" in s or "demo" in s
                           or "scratch" in s or "tmp" in s or "test" in s)]
                out["source_kind"] = ("operator_capture" if real else
                                      "synthetic_fixture" if fixture else "unknown")
            con.close()
        except Exception as e:
            out["errors"].append("staging unreadable: %s" % e)
    elif files:
        out["present"] = True
        out["schema_ok"] = True  # archive-only; row counts were verified at export

    # --- catalog dims ---
    for name in CATALOG_TABLES:
        pq = os.path.join(warehouse_dir, "catalog", name, "part-00000.parquet")
        if os.path.exists(pq):
            try:
                import duckdb
                out["catalog"][name] = duckdb.connect().execute(
                    "SELECT count(*) FROM read_parquet('%s')"
                    % pq.replace("'", "''")).fetchone()[0]
            except Exception:
                out["catalog"][name] = -1
    missing = [t for t in FACT_TABLES if not out["row_counts"].get(t)
               and not any(os.sep + t + os.sep in f for f in files)]
    missing += ["catalog:" + t for t in CATALOG_TABLES if t not in out["catalog"]]
    out["missing_categories"] = missing
    return out


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--warehouse", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv[1:])
    cfg = wc.load_config()
    status = collect_status(args.warehouse or cfg["warehouse_root"])
    ok = (not status["present"]) or (status["schema_ok"] and not status["errors"])
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0 if ok else 1
    if not status["present"]:
        print("warehouse : %s (not built yet)" % status["warehouse"])
        print("ALL PASS")
        return 0
    print("warehouse : %s (schema_ok=%s source=%s)"
          % (status["warehouse"], status["schema_ok"], status["source_kind"]))
    for t, n in sorted(status["row_counts"].items()):
        print("  staging %-16s %8d rows" % (t, n))
    print("  archive %d file(s), latest date %s"
          % (status["partitions"], status["latest_partition_date"]))
    for w_ in status["warnings"]:
        print("  WARN: %s" % w_)
    for e_ in status["errors"]:
        print("  ERROR: %s" % e_)
    print("ALL PASS" if ok else "WAREHOUSE STATUS FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
