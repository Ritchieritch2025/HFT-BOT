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
import json
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


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _code_commit():
    """Seal provenance (operator seal ruling step 3). Fail closed: a seal
    without the producing code commit is not a seal."""
    import subprocess
    r = subprocess.run(["git", "-C", os.path.dirname(os.path.abspath(__file__)),
                        "rev-parse", "HEAD"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError("cannot determine code commit for seal")
    return r.stdout.strip()


def _seal_verifies(warehouse_root, archive_root, raw_root, date):
    """True iff the existing seal for date passes the reader-side gate."""
    import warehouse
    try:
        for table, _ in TABLES:
            warehouse._require_sealed_dates(
                warehouse_root, archive_root, raw_root, table, date, date)
    except Exception:
        return False
    return True


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


def _archived_row_count(con, path, ext):
    src = path.replace("'", "''")
    if ext == "parquet":
        return con.execute("SELECT count(*) FROM read_parquet('%s')" % src).fetchone()[0]
    return con.execute(
        "SELECT count(*) FROM read_csv('%s', header=true, all_varchar=true)" % src
    ).fetchone()[0]


def _archived_select(table, path, ext):
    src = path.replace("'", "''")
    if ext == "parquet":
        return "SELECT * FROM read_parquet('%s', hive_partitioning=false)" % src
    # Keep identity/enumeration fields as strings.  DuckDB otherwise narrows a
    # yes/no-only taker_side file to BOOLEAN, making exact comparison impossible.
    str_cols = ("market_ticker", "series_ticker", "event_ticker", "category",
                "subcategory", "group", "trade_id", "taker_side")
    # W-TL1: an all-NULL ladder column sniffs as VARCHAR in csv and would fail
    # the exact schema comparison against staging's BIGINT — pin them, same as
    # warehouse.load()'s ladder pin.
    ladder = ("exchange_ts_us", "recv_wall_ns", "recv_mono_ns",
              "local_recv_ts_us")
    types = ", ".join("'%s': 'VARCHAR'" % c for c in str_cols)
    types += ", " + ", ".join("'%s': 'BIGINT'" % c for c in ladder)
    return ("SELECT * FROM read_csv('%s', header=true, hive_partitioning=false, "
            "types={%s})"
            % (src, types))


def verify_raw_caught_up(con, raw_root, date, warehouse_root=None):
    """Prove every byte in every closed raw file has an exact checkpoint."""
    day_dir = wc.raw_day_dir(raw_root, date)
    files = wc.seal_raw_files(raw_root, date, warehouse_root=warehouse_root)
    if not files:
        raise RuntimeError("no raw files for completed day: %s" % day_dir)
    checkpoints = dict(con.execute(
        "SELECT file, byte_offset FROM stg.checkpoint").fetchall())
    proof = []
    for path in files:
        path = os.path.abspath(path)
        before = os.stat(path)
        size = before.st_size
        if size <= 0:
            raise RuntimeError("empty closed raw file: %s" % path)
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                raise RuntimeError("closed raw file has a partial trailing record: %s"
                                   % path)
        offset = checkpoints.get(path)
        if offset != size:
            raise RuntimeError("ingest checkpoint behind raw: %s checkpoint=%s size=%d"
                               % (path, offset, size))
        after = os.stat(path)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("raw file changed during catch-up proof: %s" % path)
        digest = sha256_file(path)
        final = os.stat(path)
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != \
           (final.st_size, final.st_mtime_ns, final.st_ctime_ns):
            raise RuntimeError("raw file changed during sha256 proof: %s" % path)
        proof.append({"file": os.path.relpath(path, raw_root), "size": size,
                      "inode": final.st_ino, "mtime_ns": final.st_mtime_ns,
                      "ctime_ns": final.st_ctime_ns, "checkpoint": int(offset),
                      "sha256": digest})
    return proof


def verify_raw_proof_still_current(raw_root, date, proof, warehouse_root=None):
    """Close the inventory TOCTOU window immediately before seal publish."""
    expected = {os.path.abspath(os.path.join(raw_root, r["file"])): r for r in proof}
    current = {os.path.abspath(p) for p in wc.seal_raw_files(
        raw_root, date, warehouse_root=warehouse_root)}
    if current != set(expected):
        raise RuntimeError("raw inventory changed during seal: missing=%s extra=%s"
                           % (sorted(set(expected) - current),
                              sorted(current - set(expected))))
    for path, row in expected.items():
        st = os.stat(path)
        observed = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        recorded = (int(row["inode"]), int(row["size"]),
                    int(row["mtime_ns"]), int(row["ctime_ns"]))
        if observed != recorded:
            raise RuntimeError("raw file changed during seal: %s" % path)


def verify_archive_proof_still_current(archive_root, date, proof):
    """Close the archive verify-to-seal TOCTOU window before publish."""
    expected = {os.path.abspath(os.path.join(archive_root, r["file"])): r
                for r in proof}
    current = set()
    for table, ext in TABLES:
        current.update(os.path.abspath(p) for p in glob.glob(os.path.join(
            archive_root, table, "category=*", "subcategory=*", "date=%s" % date,
            "*.%s" % ext)))
    if current != set(expected):
        raise RuntimeError("archive inventory changed during seal: missing=%s extra=%s"
                           % (sorted(set(expected) - current),
                              sorted(current - set(expected))))
    for path, row in expected.items():
        st = os.stat(path)
        observed = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        recorded = (int(row["inode"]), int(row["size"]),
                    int(row["mtime_ns"]), int(row["ctime_ns"]))
        if observed != recorded:
            raise RuntimeError("archive changed during seal: %s" % path)


def _schema_signature(con, sql, integer_family=False):
    rows = con.execute("DESCRIBE SELECT * FROM (%s)" % sql).fetchall()
    out = []
    for name, typ, *_ in rows:
        norm = typ.upper()
        if integer_family and "INT" in norm:
            norm = "INTEGER_FAMILY"
        out.append((name, norm))
    return out


def verify_archived_date(con, date, day_lo, day_hi, archive_root, warehouse_root,
                         return_proof=False):
    """Prove that one final day exactly matches staging, manifest and files.

    This verifier is deliberately read-only.  It is the only safe way for the
    supervisor to treat a write-once ``already archived`` result as complete:
    seeing the first existing file says nothing about the remaining partitions.
    """
    manifest = [r for r in read_manifest(os.path.join(warehouse_root, "manifest.csv"))
                if r.get("date") == date]
    actual_manifest = {}
    for row in manifest:
        key = (row.get("table"), row.get("category"), row.get("subcategory"))
        if key in actual_manifest:
            raise RuntimeError("duplicate manifest row for %s" % (key,))
        actual_manifest[key] = row

    expected = {}
    expected_files = set()
    for table, ext in TABLES:
        rows = con.execute(
            'SELECT category, subcategory, count(*) FROM stg.%s '
            'WHERE ts_utc >= ? AND ts_utc < ? GROUP BY 1, 2 ORDER BY 1, 2' % table,
            [day_lo, day_hi]).fetchall()
        for cat, sub, count in rows:
            mcat = cat if cat is not None else "_unclassified"
            msub = sub if sub is not None else "_unclassified"
            key = (table, mcat, msub)
            fpath = os.path.abspath(os.path.join(
                wc.partition_dir(archive_root, table, cat, sub, date),
                wc.partition_file(table, cat, sub, date, ext)))
            cat_w = ("category IS NULL" if cat is None else
                     "category = '%s'" % cat.replace("'", "''"))
            sub_w = ("subcategory IS NULL" if sub is None else
                     "subcategory = '%s'" % sub.replace("'", "''"))
            source_sql = ("SELECT * FROM stg.%s WHERE ts_utc >= %d AND ts_utc < %d "
                          "AND %s AND %s" %
                          (table, day_lo, day_hi, cat_w, sub_w))
            expected[key] = (int(count), fpath, ext, source_sql)
            expected_files.add(fpath)

    if set(actual_manifest) != set(expected):
        missing = sorted(set(expected) - set(actual_manifest))
        extra = sorted(set(actual_manifest) - set(expected))
        raise RuntimeError("manifest partition mismatch: missing=%s extra=%s"
                           % (missing, extra))

    disk_files = set()
    for table, ext in TABLES:
        disk_files.update(os.path.abspath(p) for p in glob.glob(os.path.join(
            archive_root, table, "category=*", "subcategory=*", "date=%s" % date,
            "*.%s" % ext)))
    if disk_files != expected_files:
        raise RuntimeError("archive file-set mismatch: missing=%s extra=%s"
                           % (sorted(expected_files - disk_files),
                              sorted(disk_files - expected_files)))

    proof = []
    for key, (staged_count, expected_path, ext, source_sql) in sorted(expected.items()):
        row = actual_manifest[key]
        recorded_path = row.get("file_path", "")
        if not os.path.isabs(recorded_path):
            recorded_path = os.path.join(wc.ROOT, recorded_path)
        recorded_path = os.path.abspath(recorded_path)
        if recorded_path != expected_path:
            raise RuntimeError("manifest path mismatch for %s: %s != %s"
                               % (key, recorded_path, expected_path))
        if not os.path.isfile(expected_path):
            raise RuntimeError("archive file missing: %s" % expected_path)
        stat_before = os.stat(expected_path)
        archived_count = _archived_row_count(con, expected_path, ext)
        manifest_count = int(row.get("row_count") or -1)
        if archived_count != staged_count or manifest_count != staged_count:
            raise RuntimeError(
                "row-count mismatch for %s: staging=%d file=%d manifest=%d"
                % (key, staged_count, archived_count, manifest_count))
        archived_sql = _archived_select(key[0], expected_path, ext)
        source_schema = _schema_signature(con, source_sql,
                                          integer_family=(ext != "parquet"))
        archive_schema = _schema_signature(con, archived_sql,
                                           integer_family=(ext != "parquet"))
        if source_schema != archive_schema:
            raise RuntimeError("schema mismatch for %s: staging=%s archive=%s"
                               % (key, source_schema, archive_schema))
        staged_only = con.execute(
            "SELECT count(*) FROM ((%s) EXCEPT ALL (%s))"
            % (source_sql, archived_sql)).fetchone()[0]
        archived_only = con.execute(
            "SELECT count(*) FROM ((%s) EXCEPT ALL (%s))"
            % (archived_sql, source_sql)).fetchone()[0]
        if staged_only or archived_only:
            raise RuntimeError("content mismatch for %s: staging_only=%d archive_only=%d"
                               % (key, staged_only, archived_only))
        digest = md5_file(expected_path)
        if digest != row.get("file_md5"):
            raise RuntimeError("md5 mismatch for %s: %s != %s"
                               % (key, digest, row.get("file_md5")))
        stat_after = os.stat(expected_path)
        before_key = (stat_before.st_ino, stat_before.st_size,
                      stat_before.st_mtime_ns, stat_before.st_ctime_ns)
        after_key = (stat_after.st_ino, stat_after.st_size,
                     stat_after.st_mtime_ns, stat_after.st_ctime_ns)
        if before_key != after_key:
            raise RuntimeError("archive changed during exact proof: %s" % expected_path)
        proof.append({
            "file": os.path.relpath(expected_path, archive_root),
            "table": key[0], "size": stat_after.st_size,
            "inode": stat_after.st_ino, "mtime_ns": stat_after.st_mtime_ns,
            "ctime_ns": stat_after.st_ctime_ns, "md5": digest,
            "sha256": sha256_file(expected_path),
        })
    return (len(expected), proof) if return_proof else len(expected)


def write_day_seal(con, date, day_lo, day_hi, cfg):
    """Atomically attest raw->staging->archive completeness for one day."""
    raw_proof = verify_raw_caught_up(
        con, cfg["raw_root"], date, cfg["warehouse_root"])
    manifest_path = os.path.join(cfg["warehouse_root"], "manifest.csv")
    manifest_before = os.stat(manifest_path)
    nfiles, archive_stats = verify_archived_date(
        con, date, day_lo, day_hi, cfg["archive_root"], cfg["warehouse_root"],
        return_proof=True)
    # The sealed day (yesterday) remains inside the configured today+prior-day
    # window; prune only older staging after every proof passes and before the
    # seal becomes visible.
    prune_staging(con, cfg["staging_retain_days"])
    verify_raw_proof_still_current(
        cfg["raw_root"], date, raw_proof, cfg["warehouse_root"])
    verify_archive_proof_still_current(cfg["archive_root"], date, archive_stats)
    manifest_after = os.stat(manifest_path)
    if (manifest_before.st_ino, manifest_before.st_size, manifest_before.st_mtime_ns,
            manifest_before.st_ctime_ns) != \
       (manifest_after.st_ino, manifest_after.st_size, manifest_after.st_mtime_ns,
            manifest_after.st_ctime_ns):
        raise RuntimeError("manifest changed during seal: %s" % manifest_path)
    digest, rows = wc.manifest_date_sha256(manifest_path, date)
    seal = {
        "version": 2,
        "method": "full_v2",
        "go_no_go_eligible": True,
        "unverified": [],
        "code_commit": _code_commit(),
        "status": "SEALED",
        "date": date,
        "sealed_at": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifest_date_sha256": digest,
        "archive_files": nfiles,
        "archive_rows": sum(int(r["row_count"]) for r in rows),
        "archive_file_stats": archive_stats,
        "capture_quality_status": "UNASSESSED_PENDING_PIPE_W03",
        "raw_retention_requirement": "LOCAL_OR_VAULT_VERIFIED_RECEIPT",
        "receipt_cross_day_hours": 2,
        "raw_files": raw_proof,
    }
    path = wc.seal_path(cfg["warehouse_root"], date)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(seal, f, sort_keys=True, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path, seal


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
    ap.add_argument("--verify-only", action="store_true",
                    help="read-only proof that staging, archive files and manifest "
                         "match exactly for the completed day")
    ap.add_argument("--check-caught-up", action="store_true",
                    help="read-only proof that every closed raw byte is checkpointed")
    ap.add_argument("--seal", action="store_true",
                    help="after catch-up + exact archive proof, atomically seal the day")
    ap.add_argument("--verify-seal", action="store_true",
                    help="validate an existing seal/files without opening staging")
    ap.add_argument("--legacy-seal", action="store_true",
                    help="seal a PRE-SEAL-SYSTEM historical day from archive "
                         "self-consistency alone (method=legacy_v0, operator "
                         "ruling 2026-07-11 option A): manifest/md5/row-count/"
                         "sha256 verified, raw/staging identity UNVERIFIED, "
                         "go_no_go_eligible=false forever")
    ap.add_argument("--operator-invalidate-seal", metavar="REASON", default=None,
                    help="OPERATOR ONLY: park (never delete) the active seal "
                         "with a stated reason; the parked seal + ledger entry "
                         "remain as evidence")
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
    proof_modes = sum(bool(x) for x in
                      (args.verify_only, args.check_caught_up, args.seal,
                       args.verify_seal, args.legacy_seal,
                       args.operator_invalidate_seal is not None))
    if proof_modes > 1 or (proof_modes and
                           (args.snapshot or args.force or args.csv or args.flat)):
        print("proof/seal modes are exclusive and cannot be combined with "
              "snapshot/force/format options",
              file=sys.stderr)
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
    if args.verify_seal:
        try:
            import warehouse
            for table, _ in TABLES:
                warehouse._require_sealed_dates(
                    warehouse_root, archive_root, cfg["raw_root"], table, date, date)
        except Exception as e:
            print("DAY SEAL VERIFY FAIL %s: %s" % (date, e), file=sys.stderr)
            return 1
        print("DAY SEAL VERIFY PASS %s" % date)
        return 0
    if args.operator_invalidate_seal is not None:
        reason = args.operator_invalidate_seal.strip()
        if not reason:
            print("--operator-invalidate-seal requires a non-empty reason",
                  file=sys.stderr)
            return 2
        active = wc.seal_path(warehouse_root, date)
        if not os.path.exists(active):
            print("no active seal for %s; nothing to invalidate" % date,
                  file=sys.stderr)
            return 1
        seal_dir = os.path.dirname(active)
        stamp = "%d.%d" % (__import__("time").time_ns(), os.getpid())
        parked = os.path.join(seal_dir,
                              "date=%s.invalidated-operator.%s.json" % (date, stamp))
        os.replace(active, parked)
        dfd = os.open(seal_dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        ledger = os.path.join(warehouse_root, "seal_invalidations.ndjson")
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "event": "SEAL_INVALIDATED_BY_OPERATOR",
                "exchange_date": date,
                "observed_at_utc": datetime.datetime.now(datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "reason": reason,
                "parked_seal": os.path.relpath(parked, warehouse_root),
            }, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        print("SEAL INVALIDATED (operator) %s -> %s reason=%s"
              % (date, parked, reason))
        return 0

    if args.legacy_seal:
        active = wc.seal_path(warehouse_root, date)
        if os.path.exists(active):
            print("REFUSING --legacy-seal: %s already has a seal (write-once)"
                  % date, file=sys.stderr)
            return 4
        if os.path.isdir(wc.raw_day_dir(cfg["raw_root"], date)):
            print("REFUSING --legacy-seal: raw for %s is still present — use the "
                  "full --seal path instead of downgrading the evidence grade"
                  % date, file=sys.stderr)
            return 2
        manifest_path = os.path.join(warehouse_root, "manifest.csv")
        rows = [r for r in read_manifest(manifest_path) if r.get("date") == date]
        if not rows:
            print("LEGACY SEAL FAIL %s: no manifest rows for the day" % date,
                  file=sys.stderr)
            return 1
        lcon = duckdb.connect()
        stats = []
        try:
            for row in rows:
                rel = row.get("file_path", "")
                fpath = rel if os.path.isabs(rel) else os.path.join(wc.ROOT, rel)
                fpath = os.path.abspath(fpath)
                if not os.path.isfile(fpath):
                    raise RuntimeError("archived file missing: %s" % fpath)
                if md5_file(fpath) != row.get("file_md5"):
                    raise RuntimeError("md5 mismatch vs manifest: %s" % fpath)
                ext = "parquet" if fpath.endswith(".parquet") else "csv.gz"
                n = _archived_row_count(lcon, fpath, ext)
                if n != int(row.get("row_count") or -1):
                    raise RuntimeError("row-count mismatch vs manifest: %s" % fpath)
                st = os.stat(fpath)
                stats.append({
                    "file": os.path.relpath(fpath, archive_root),
                    "table": row.get("table"), "size": st.st_size,
                    "inode": st.st_ino, "mtime_ns": st.st_mtime_ns,
                    "ctime_ns": st.st_ctime_ns, "md5": row.get("file_md5"),
                    "sha256": sha256_file(fpath),
                })
        except Exception as e:
            lcon.close()
            print("LEGACY SEAL FAIL %s: %s" % (date, e), file=sys.stderr)
            return 1
        lcon.close()
        digest, _mrows = wc.manifest_date_sha256(manifest_path, date)
        seal = {
            "version": 2,
            "method": "legacy_v0",
            "go_no_go_eligible": False,
            "unverified": ["raw_byte_checkpoint",
                           "staging_archive_content_identity"],
            "note": "pre-seal-system historical day sealed from archive "
                    "self-consistency alone (operator ruling 2026-07-11 "
                    "option A); PERMANENTLY ineligible for go/no-go verdicts",
            "code_commit": _code_commit(),
            "status": "SEALED",
            "date": date,
            "sealed_at": datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "manifest_date_sha256": digest,
            "archive_files": len(stats),
            "archive_rows": sum(int(r["size"] >= 0 and 0) or 0 for r in []) or
                            sum(int(row.get("row_count") or 0) for row in rows),
            "archive_file_stats": stats,
            "capture_quality_status": "UNASSESSED_LEGACY",
            "raw_files": [],
        }
        path = wc.seal_path(warehouse_root, date)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp.%d" % os.getpid()
        with open(tmp, "w") as f:
            json.dump(seal, f, sort_keys=True, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        print("LEGACY SEAL PASS %s: %d file(s), method=legacy_v0, "
              "go_no_go_eligible=false" % (date, len(stats)))
        return 0

    if not os.path.exists(staging):
        print("no staging db at %s; nothing to export" % staging, file=sys.stderr)
        return 1
    if not proof_modes and not archive_root_ok(archive_root):
        return 1

    day_lo = wc.day_start_us(date)
    day_hi = day_lo + 86_400_000_000
    con = duckdb.connect()
    attach_mode = " (READ_ONLY)" if proof_modes and not args.seal else ""
    con.execute("ATTACH '%s' AS stg%s" % (staging.replace("'", "''"), attach_mode))

    if args.check_caught_up:
        try:
            proof = verify_raw_caught_up(
                con, cfg["raw_root"], date, cfg["warehouse_root"])
        except Exception as e:
            print("INGEST CATCH-UP FAIL %s: %s" % (date, e), file=sys.stderr)
            con.close()
            return 1
        con.close()
        print("INGEST CATCH-UP PASS %s: %d closed raw file(s), all bytes checkpointed"
              % (date, len(proof)))
        return 0

    if args.verify_only:
        try:
            nfiles = verify_archived_date(con, date, day_lo, day_hi,
                                          archive_root, warehouse_root)
        except Exception as e:
            print("ARCHIVE VERIFY FAIL %s: %s" % (date, e), file=sys.stderr)
            con.close()
            return 1
        con.close()
        print("ARCHIVE VERIFY PASS %s: %d file(s), exact staging/manifest/file match"
              % (date, nfiles))
        return 0

    if args.seal:
        # WRITE-ONCE (operator seal ruling 2026-07-11): an existing seal is
        # never replaced. Valid -> idempotent success; invalid -> operator
        # remediation, never a silent rewrite.
        active = wc.seal_path(warehouse_root, date)
        if os.path.exists(active):
            con.close()
            if _seal_verifies(warehouse_root, archive_root, cfg["raw_root"], date):
                print("DAY SEAL PASS %s: existing seal verifies (write-once no-op)"
                      % date)
                return 0
            print("DAY SEAL FAIL %s: existing seal FAILS verification. Seals are "
                  "write-once; park it explicitly with "
                  "--operator-invalidate-seal <reason> (operator only), rebuild, "
                  "then reseal." % date, file=sys.stderr)
            return 4
        effective_cfg = dict(cfg)
        effective_cfg.update(staging_db=staging, archive_root=archive_root,
                             warehouse_root=warehouse_root)
        try:
            path, seal = write_day_seal(con, date, day_lo, day_hi, effective_cfg)
        except Exception as e:
            print("DAY SEAL FAIL %s: %s" % (date, e), file=sys.stderr)
            con.close()
            return 1
        con.close()
        print("DAY SEAL PASS %s: raw_files=%d archive_files=%d seal=%s"
              % (date, len(seal["raw_files"]), seal["archive_files"], path))
        return 0

    # Shrink guard (2026-07-07 audit): a --force re-export replaces write-once
    # archive files from whatever staging still holds. If staging has FEWER
    # rows for the day than the manifest already certifies (e.g. an operator
    # lowered staging_retain_days and the day was already pruned), proceeding
    # would silently shrink the archive. Refuse instead.
    if args.force and not args.snapshot:
        if os.path.exists(wc.seal_path(warehouse_root, date)):
            print("REFUSING --force: %s is SEALED and write-once (final). Late "
                  "data lands in the corrections partition; rebuilding a sealed "
                  "day requires --operator-invalidate-seal first." % date,
                  file=sys.stderr)
            con.close()
            return 3
        manifest_rows = read_manifest(os.path.join(warehouse_root, "manifest.csv"))
        for table, _ in TABLES:
            certified = sum(int(r["row_count"]) for r in manifest_rows
                            if r["date"] == date and r["table"] == table)
            staged = con.execute(
                "SELECT count(*) FROM stg.%s WHERE ts_utc >= %d AND ts_utc < %d"
                % (table, day_lo, day_hi)).fetchone()[0]
            if staged < certified:
                print("REFUSING --force: staging holds %d %s rows for %s but the "
                      "archive already certifies %d — re-export would SHRINK the "
                      "write-once archive (staging pruned?)"
                      % (staged, table, date, certified), file=sys.stderr)
                con.close()
                return 3
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
