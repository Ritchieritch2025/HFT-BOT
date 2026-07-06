#!/usr/bin/env python3
"""One-time migration: legacy captures + Greed-layout partitions -> three-layer
warehouse.

Steps (all idempotent; nothing is deleted):
  1. Re-ingest every legacy raw capture NDJSON still on disk into staging via
     tools/ingest.py's checkpointed path (already-ingested byte ranges are
     skipped automatically): work/live_capture.ndjson,
     work/exchange_check_capture.ndjson, work/live/*.ndjson.
  2. Move the legacy Greed-layout partition dirs
     (<warehouse>/{orderbooks_l1,orderbooks_full,trades,...}/date=*) aside to
     <warehouse>/legacy_greed/ — they came from the removed dump/convert path
     and their raw sources (where still on disk) were re-ingested in step 1.
  3. Regenerate the classification + per-series tags report
     (config/series_tags_report.csv) for manual review.
  4. Export any completed (pre-today) UTC days now sitting in staging.

stdlib + duckdb only.
"""
import datetime
import glob
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402

LEGACY_TABLES = ["orderbooks_l1", "orderbooks_full", "trades", "markets",
                 "events", "market_settlements", "rfq_events"]


def main(argv):
    cfg = wc.load_config()
    warehouse = cfg["warehouse_root"]
    work = os.path.join(wc.ROOT, "work")

    print("== 1. re-ingest legacy raw captures into staging")
    captures = [p for p in
                [os.path.join(work, "live_capture.ndjson"),
                 os.path.join(work, "exchange_check_capture.ndjson")] +
                sorted(glob.glob(os.path.join(work, "live", "*.ndjson")))
                if os.path.exists(p)]
    if captures:
        rc = subprocess.call([sys.executable, os.path.join(wc.ROOT, "tools", "ingest.py")]
                             + captures)
        if rc != 0:
            print("MIGRATION FAIL: ingest returned %d" % rc, file=sys.stderr)
            return 1
    else:
        print("  (no legacy captures on disk)")

    print("== 2. park legacy Greed-layout partitions")
    legacy_dir = os.path.join(warehouse, "legacy_greed")
    moved = 0
    for t in LEGACY_TABLES:
        src = os.path.join(warehouse, t)
        if os.path.isdir(src) and glob.glob(os.path.join(src, "date=*")):
            os.makedirs(legacy_dir, exist_ok=True)
            dst = os.path.join(legacy_dir, t)
            if os.path.exists(dst):
                print("  %-18s already parked" % t)
                continue
            shutil.move(src, dst)
            print("  %-18s -> legacy_greed/%s" % (t, t))
            moved += 1
    if not moved:
        print("  (nothing to park)")

    print("== 3. classification + per-series tags report")
    subprocess.call([sys.executable, os.path.join(wc.ROOT, "tools", "build_classification.py")])

    print("== 4. export completed days from staging")
    import duckdb
    con = duckdb.connect(cfg["staging_db"], read_only=True)
    days = [r[0] for r in con.execute(
        "SELECT DISTINCT strftime(to_timestamp(ts_utc/1000000), '%Y-%m-%d') "
        "FROM orderbooks_l1 ORDER BY 1").fetchall()]
    con.close()
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    for d in days:
        if d >= today:
            print("  %s stays in staging (current day)" % d)
            continue
        subprocess.call([sys.executable, os.path.join(wc.ROOT, "tools", "export_day.py"),
                         "--date", d, "--no-prune"])
    print("MIGRATION DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
