#!/usr/bin/env python3
"""Day-rollover exporter: staging DuckDB -> final archive partitions (LAYER 3).

At UTC midnight (invoked by the pipeline supervisor, or by hand) the completed
day is exported write-once to:

  <ARCHIVE_ROOT>/<table>/category=<C>/subcategory=<S>/date=<D>/
      <table>__<C>__<S>__<D>.parquet    orderbooks_l1, orderbooks_full (zstd-15,
                                        sorted by market_ticker, ts_utc)
      <table>__<C>__<S>__<D>.csv.gz     trades

Then row counts are verified by re-reading every written file, manifest.csv at
the warehouse root gains one row per file (date, table, category, subcategory,
row_count, file_path, file_md5, created_ts), the per-day per-category
compression report (raw ticks seen vs rows written) is appended to
compression_report.csv, and staging is pruned to today + 1 prior day.

Safety: the exporter first checks ARCHIVE_ROOT is mounted/writable; if not it
retains staging, alerts on stderr, and exits non-zero so the supervisor retries
next cycle. Files are final: re-exporting an already-archived (table, day)
fails unless --force (which replaces the files and their manifest rows).

stdlib + duckdb only.
"""
import argparse
import csv
import datetime
import glob
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402

TABLES = (
    ("orderbooks_l1", "parquet"),
    ("orderbooks_full", "parquet"),
    ("trades", "csv.gz"),
)
MANIFEST_FIELDS = ["date", "table", "category", "subcategory", "row_count",
                   "file_path", "file_md5", "created_ts"]


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def archive_root_ok(root):
    """Mounted + writable probe. Never export into a dead path."""
    try:
        os.makedirs(root, exist_ok=True)
        probe = os.path.join(root, ".write_probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.unlink(probe)
        return True
    except OSError as e:
        print("ALERT: archive root %s is not writable (%s); retaining staging, "
              "will retry next cycle" % (root, e), file=sys.stderr)
        return False


def read_manifest(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_manifest(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in MANIFEST_FIELDS})
    os.replace(tmp, path)


def export_table(con, table, ext, date, day_lo, day_hi, archive_root, force):
    """Export one table's day -> list of manifest rows. Raises on verify failure."""
    parts = con.execute(
        'SELECT DISTINCT category, subcategory FROM stg.%s '
        "WHERE ts_utc >= ? AND ts_utc < ? ORDER BY 1, 2" % table,
        [day_lo, day_hi]).fetchall()
    out = []
    for cat, sub in parts:
        pdir = wc.partition_dir(archive_root, table, cat, sub, date)
        fname = wc.partition_file(table, cat, sub, date, ext)
        fpath = os.path.join(pdir, fname)
        if os.path.exists(fpath) and not force:
            raise RuntimeError("already archived (write-once): %s — use --force to replace"
                               % fpath)
        os.makedirs(pdir, exist_ok=True)
        cat_w = "category IS NULL" if cat is None else "category = '%s'" % cat.replace("'", "''")
        sub_w = "subcategory IS NULL" if sub is None else \
            "subcategory = '%s'" % sub.replace("'", "''")
        sel = ("SELECT * FROM stg.%s WHERE ts_utc >= %d AND ts_utc < %d AND %s AND %s "
               "ORDER BY market_ticker, ts_utc" % (table, day_lo, day_hi, cat_w, sub_w))
        n_src = con.execute("SELECT count(*) FROM (%s)" % sel).fetchone()[0]
        dst = fpath.replace("'", "''")
        if ext == "parquet":
            con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION zstd, "
                        "COMPRESSION_LEVEL 15)" % (sel, dst))
            n_out = con.execute("SELECT count(*) FROM read_parquet('%s')" % dst).fetchone()[0]
        else:
            comp = ", COMPRESSION gzip" if ext.endswith(".gz") else ""
            con.execute("COPY (%s) TO '%s' (FORMAT CSV, HEADER%s)" % (sel, dst, comp))
            n_out = con.execute("SELECT count(*) FROM read_csv('%s', header=true, all_varchar=true)"
                                % dst).fetchone()[0]
        if n_out != n_src:
            os.unlink(fpath)
            raise RuntimeError("row-count verify FAILED for %s: staging=%d file=%d"
                               % (fpath, n_src, n_out))
        out.append({"date": date, "table": table,
                    "category": cat if cat is not None else "_unclassified",
                    "subcategory": sub if sub is not None else "_unclassified",
                    "row_count": n_src, "file_path": os.path.relpath(fpath, wc.ROOT),
                    "file_md5": md5_file(fpath),
                    "created_ts": datetime.datetime.now(datetime.timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ")})
        print("  %-16s %-28s %-20s %7d rows -> %s"
              % (table, cat or "_unclassified", sub or "_unclassified", n_src, fname))
    return out


# Human/strategy-friendly projections for --flat --csv exports: ISO time and
# dollar-unit columns first, raw E4 columns kept behind them.
STRATEGY_COLS = {
    "orderbooks_l1": """
      strftime(to_timestamp(ts_utc/1000000), '%Y-%m-%d %H:%M:%S') AS time_utc,
      market_ticker, category, subcategory, "group",
      yes_bid_e4/10000.0  AS yes_bid,  yes_ask_e4/10000.0 AS yes_ask,
      (yes_bid_e4+yes_ask_e4)/20000.0 AS mid,
      (yes_ask_e4-yes_bid_e4)/10000.0 AS spread,
      yes_bid_qty_e4/10000.0 AS bid_qty, yes_ask_qty_e4/10000.0 AS ask_qty,
      price_e4/10000.0 AS last_price, volume_e4/10000.0 AS volume,
      open_interest_e4/10000.0 AS open_interest, is_snapshot,
      series_ticker, event_ticker, ts_utc""",
    "trades": """
      strftime(to_timestamp(ts_utc/1000000), '%Y-%m-%d %H:%M:%S') AS time_utc,
      market_ticker, category, subcategory, "group",
      yes_price_e4/10000.0 AS yes_price, no_price_e4/10000.0 AS no_price,
      count_e4/10000.0 AS contracts, taker_side,
      trade_id, series_ticker, event_ticker, ts_utc""",
}


def export_flat(con, table, ext, date, day_lo, day_hi, out_root):
    """Snapshot convenience: one merged file per table (category cols in rows)."""
    os.makedirs(out_root, exist_ok=True)
    fpath = os.path.join(out_root, "%s__%s.%s" % (table, date, ext))
    cols = STRATEGY_COLS.get(table, "*") if ext == "csv" else "*"
    sel = ("SELECT %s FROM stg.%s WHERE ts_utc >= %d AND ts_utc < %d "
           "ORDER BY market_ticker, ts_utc" % (cols, table, day_lo, day_hi))
    n_src = con.execute("SELECT count(*) FROM (%s)" % sel).fetchone()[0]
    if n_src == 0:
        return []
    dst = fpath.replace("'", "''")
    if ext == "parquet":
        con.execute("COPY (%s) TO '%s' (FORMAT PARQUET, COMPRESSION zstd, "
                    "COMPRESSION_LEVEL 15)" % (sel, dst))
        n_out = con.execute("SELECT count(*) FROM read_parquet('%s')" % dst).fetchone()[0]
    else:
        comp = ", COMPRESSION gzip" if ext.endswith(".gz") else ""
        con.execute("COPY (%s) TO '%s' (FORMAT CSV, HEADER%s)" % (sel, dst, comp))
        n_out = con.execute("SELECT count(*) FROM read_csv('%s', header=true, all_varchar=true)"
                            % dst).fetchone()[0]
    if n_out != n_src:
        os.unlink(fpath)
        raise RuntimeError("row-count verify FAILED for %s: staging=%d file=%d"
                           % (fpath, n_src, n_out))
    print("  %-16s %-49s %8d rows -> %s"
          % (table, "(all categories merged)", n_src, os.path.basename(fpath)))
    return [{"date": date, "table": table, "category": "_all", "subcategory": "_all",
             "row_count": n_src, "file_path": os.path.relpath(fpath, wc.ROOT),
             "file_md5": md5_file(fpath),
             "created_ts": datetime.datetime.now(datetime.timezone.utc)
             .strftime("%Y-%m-%dT%H:%M:%SZ")}]


def compression_report(con, warehouse_root, date):
    rows = con.execute(
        "SELECT category, ticks_seen, l1_written, trades_written FROM stg.ingest_stats "
        "WHERE day = ? ORDER BY category", [date]).fetchall()
    if not rows:
        return
    path = os.path.join(warehouse_root, "compression_report.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "category", "raw_ticks_seen", "rows_written_after_dedup"])
        print("  compression (ticks seen -> rows written):")
        for cat, ticks, l1, tr in rows:
            written = (l1 or 0) + (tr or 0)
            w.writerow([date, cat, ticks, written])
            print("    %-28s %9d -> %7d (%.1f%%)"
                  % (cat, ticks, written, 100.0 * written / ticks if ticks else 0.0))


def prune_staging(con, retain_days):
    """Keep today + (retain_days - 1) prior days in staging; drop the rest."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    cutoff = wc.day_start_us((today - datetime.timedelta(days=retain_days - 1)).isoformat())
    total = 0
    for t in ("orderbooks_l1", "orderbooks_full", "trades"):
        n = con.execute("SELECT count(*) FROM stg.%s WHERE ts_utc < %d" % (t, cutoff)).fetchone()[0]
        con.execute("DELETE FROM stg.%s WHERE ts_utc < %d" % (t, cutoff))
        total += n
    # drop checkpoints for raw files that no longer exist on disk
    for (f,) in con.execute("SELECT file FROM stg.checkpoint").fetchall():
        if not os.path.exists(f):
            con.execute("DELETE FROM stg.checkpoint WHERE file = ?", [f])
    print("  pruned %d staged rows older than %s" % (total, cutoff))


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=None, help="UTC day to export (default: yesterday)")
    ap.add_argument("--staging", default=None)
    ap.add_argument("--archive-root", default=None)
    ap.add_argument("--force", action="store_true", help="replace already-archived files")
    ap.add_argument("--no-prune", action="store_true")
    ap.add_argument("--snapshot", metavar="OUTDIR", default=None,
                    help="NON-FINAL snapshot export of a (possibly current) day into "
                         "OUTDIR: same partition layout + snapshot manifest, no prune, "
                         "overwrites freely. For backtesting on today's data before the "
                         "midnight archive. The real archive stays untouched.")
    ap.add_argument("--csv", action="store_true",
                    help="snapshot mode only: write plain .csv for every table "
                         "(double-clickable in Numbers/Excel) instead of parquet/csv.gz")
    ap.add_argument("--flat", action="store_true",
                    help="snapshot mode only: ONE merged file per table "
                         "(<table>__<date>.<ext> in OUTDIR) instead of "
                         "category/subcategory partitions — category columns stay "
                         "in every row")
    args = ap.parse_args(argv[1:])
    if (args.csv or args.flat) and not args.snapshot:
        print("--csv/--flat are snapshot-only; the final archive format is locked "
              "(partitioned parquet + csv.gz)", file=sys.stderr)
        return 2
    import duckdb

    cfg = wc.load_config()
    staging = args.staging or cfg["staging_db"]
    archive_root = args.archive_root or cfg["archive_root"]
    warehouse_root = cfg["warehouse_root"]
    if args.snapshot:
        archive_root = os.path.abspath(os.path.expanduser(args.snapshot))
        args.force, args.no_prune = True, True
        date = args.date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    else:
        date = args.date or (datetime.datetime.now(datetime.timezone.utc).date()
                             - datetime.timedelta(days=1)).isoformat()
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        if date >= today:
            print("refusing to export %s: only completed (past UTC) days are final "
                  "(use --snapshot OUTDIR for a non-final export of today)" % date,
                  file=sys.stderr)
            return 2
    if not os.path.exists(staging):
        print("no staging db at %s; nothing to export" % staging, file=sys.stderr)
        return 1
    if not archive_root_ok(archive_root):
        return 1

    day_lo = wc.day_start_us(date)
    day_hi = day_lo + 86_400_000_000
    con = duckdb.connect()
    con.execute("ATTACH '%s' AS stg" % staging.replace("'", "''"))

    print("exporting %s -> %s" % (date, archive_root))
    tables = [(t, "csv") for t, _ in TABLES] if args.csv else TABLES
    new_rows = []
    try:
        for table, ext in tables:
            if args.flat:
                new_rows.extend(export_flat(con, table, ext, date, day_lo, day_hi,
                                            archive_root))
            else:
                new_rows.extend(export_table(con, table, ext, date, day_lo, day_hi,
                                             archive_root, args.force))
    except RuntimeError as e:
        print("EXPORT FAIL: %s" % e, file=sys.stderr)
        return 1
    if not new_rows:
        print("  (no rows for %s in staging)" % date)

    if args.snapshot:
        write_manifest(os.path.join(archive_root, "manifest_snapshot.csv"), new_rows)
    else:
        manifest_path = os.path.join(warehouse_root, "manifest.csv")
        manifest = [r for r in read_manifest(manifest_path)
                    if not (r["date"] == date and args.force)]
        manifest.extend(new_rows)
        write_manifest(manifest_path, manifest)
        compression_report(con, warehouse_root, date)

    if not args.no_prune:
        prune_staging(con, cfg["staging_retain_days"])
    con.close()
    mpath = os.path.join(archive_root, "manifest_snapshot.csv") if args.snapshot \
        else os.path.join(warehouse_root, "manifest.csv")
    print("EXPORT %sPASS %s: %d file(s), manifest=%s"
          % ("SNAPSHOT " if args.snapshot else "", date, len(new_rows), mpath))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
