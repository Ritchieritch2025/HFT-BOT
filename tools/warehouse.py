#!/usr/bin/env python3
"""Single analysis entry point for the Kalshi warehouse.

    load(table, category=None, subcategory=None, group=None,
         start=None, end=None, columns=None, ffill=False,
         archive_only=None) -> duckdb relation

Routes transparently: past (archived) dates read the final partition files
under <ARCHIVE_ROOT>/<table>/category=<C>/subcategory=<S>/date=<D>/, the
current (and any not-yet-exported) day reads the live staging DuckDB — the
caller never needs to know which. Archived days are excluded from the staging
scan so the overlap window (staging retains today + 1 prior day) can never
double-count.

Tables: orderbooks_l1, orderbooks_full (Parquet archives), trades (csv.gz
archives). All timestamps are UTC epoch-micros; `start`/`end` accept
'YYYY-MM-DD' or epoch-us. Category/subcategory filters prune by partition PATH
(sanitized identically to the exporter), so a slice like

    load("trades", category="Sports", subcategory="MLB",
         start="2026-07-06", end="2026-07-06")
    load("orderbooks_l1", category="Crypto", group="BTC", ffill=True)

only opens the matching files. `group` (derived league/region/asset) is a
row-level filter from the pinned classification dim.

`ffill=True` applies last-observation-carried-forward on orderbooks_l1 (the
change-only reconstruction rule: book state at time T = most recent row <= T;
max lookback 1h thanks to hourly heartbeats).

stdlib + duckdb only.
"""
import argparse
import datetime
import glob as _glob
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402

TABLES = ("orderbooks_l1", "orderbooks_full", "trades")
_EXT = {"orderbooks_l1": "parquet", "orderbooks_full": "parquet", "trades": "csv.gz"}


def _to_us(v, end=False):
    if v is None:
        return None
    if isinstance(v, int):
        return v
    s = str(v)
    if len(s) == 10 and s[4] == "-":            # YYYY-MM-DD
        import datetime
        d = datetime.date.fromisoformat(s)
        if end:
            d = d + datetime.timedelta(days=1)
        return int(datetime.datetime(d.year, d.month, d.day,
                   tzinfo=datetime.timezone.utc).timestamp() * 1_000_000)
    return int(s)


_CON = None
_ATTACHED = set()
_VALIDATED_ARCHIVES = {}
_VALIDATED_RAW = {}


def _conn():
    global _CON
    if _CON is None:
        import duckdb
        _CON = duckdb.connect()
    return _CON


def _archive_files(archive_root, table, category, subcategory, s_us, e_us):
    """Path-targeted partition scan. Returns (files, part_max) where part_max
    maps (sanitized_category, sanitized_subcategory) -> max archived date str.

    part_max is PER PARTITION, not a global max (AF-3): staging dedup must drop a
    staging row only when that row's OWN partition archived through its day. A
    global max would impose the busiest partition's archival frontier on a
    laggard, silently dropping the laggard's not-yet-archived staging rows on the
    boundary day (undercount on every aggregate/all-category backtest tape)."""
    pat = os.path.join(
        archive_root, table,
        "category=%s" % (wc.sanitize(category) if category else "*"),
        "subcategory=%s" % (wc.sanitize(subcategory) if subcategory else "*"),
        "date=*", "*.%s" % _EXT[table])
    rex = re.compile(r"category=([^/]+)/subcategory=([^/]+)/date=(\d{4}-\d{2}-\d{2})")
    files, part_max = [], {}
    for f in sorted(_glob.glob(pat)):
        m = rex.search(f)
        if not m:
            continue
        scat, ssub, dt = m.group(1), m.group(2), m.group(3)
        d_lo = wc.day_start_us(dt)
        # `files` is date-window-filtered for READING; part_max tracks the full
        # archived frontier per partition (even outside the window) so the
        # boundary-day dedup is correct.
        if not (s_us is not None and d_lo + 86_400_000_000 <= s_us) and \
           not (e_us is not None and d_lo >= e_us):
            files.append(f)
        cur = part_max.get((scat, ssub))
        if cur is None or dt > cur:
            part_max[(scat, ssub)] = dt
    return files, part_max


def _sani_sql(col):
    """SQL replica of warehouse_common.sanitize(): runs of chars outside
    [A-Za-z0-9._-] -> '_', strip leading/trailing '_', empty/NULL ->
    '_unclassified'. Used to match a staging row's (category, subcategory) to
    the archive's sanitized partition-dir names (sanitize is not invertible)."""
    return ("coalesce(nullif(trim(regexp_replace(coalesce(%s, ''), "
            "'[^A-Za-z0-9._-]+', '_', 'g'), '_'), ''), '_unclassified')" % col)


def _staging_dedup_sql(stg, part_max):
    """Wrap the staging SELECT so a row is kept iff its OWN partition has no
    archive (LEFT JOIN miss) OR its ts is on/after that partition's post-archive
    cutoff (day after its max archived date). Per-partition — AF-3 residual."""
    vals = ", ".join(
        "('%s','%s',%d)" % (c.replace("'", "''"), s.replace("'", "''"),
                            wc.day_start_us(d) + 86_400_000_000)
        for (c, s), d in sorted(part_max.items()))
    return ("SELECT _st.* FROM (%s) _st LEFT JOIN (VALUES %s) AS _a(_c, _s, _cut) "
            "ON %s = _a._c AND %s = _a._s "
            "WHERE _a._cut IS NULL OR _st.ts_utc >= _a._cut"
            % (stg, vals, _sani_sql("_st.category"), _sani_sql("_st.subcategory")))


def _resolve_event(event, index_path):
    """Resolve an event/market unit_key from the W-E1 index → (category, markets,
    win_start_us, win_end_us). Read-only; raises if the index or unit is absent."""
    import duckdb
    if not os.path.exists(index_path):
        raise FileNotFoundError("event index not found: %s (run event_index first)" % index_path)
    rows = duckdb.sql("SELECT category, markets, win_start_us, win_end_us "
                      "FROM read_parquet('%s') WHERE unit_key = '%s'"
                      % (index_path.replace("'", "''"), event.replace("'", "''"))).fetchall()
    if not rows:
        raise KeyError("event/unit not in index: %s" % event)
    cat, markets, ws, we = rows[0]
    return cat, list(markets), ws, we


def _md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _require_sealed_dates(warehouse_root, archive_root, raw_root, table, start, end):
    """Fail closed unless every UTC day in the requested window is sealed."""
    s_us, e_us = _to_us(start), _to_us(end, end=True)
    if s_us is None or e_us is None or e_us <= s_us:
        raise ValueError("archive_only requires a non-empty bounded start/end window")
    first = datetime.datetime.fromtimestamp(
        s_us / 1_000_000, tz=datetime.timezone.utc).date()
    last = datetime.datetime.fromtimestamp(
        (e_us - 1) / 1_000_000, tz=datetime.timezone.utc).date()
    manifest_path = os.path.join(warehouse_root, "manifest.csv")
    day = first
    while day <= last:
        date = day.isoformat()
        path = wc.seal_path(warehouse_root, date)
        if not os.path.isfile(path):
            raise FileNotFoundError("archive day is not sealed: %s" % date)
        try:
            with open(path) as f:
                seal = json.load(f)
        except (OSError, ValueError) as e:
            raise RuntimeError("invalid archive seal %s: %s" % (path, e))
        digest, manifest_rows = wc.manifest_date_sha256(manifest_path, date)
        if (seal.get("version") != 1 or seal.get("status") != "SEALED" or
                seal.get("date") != date or
                seal.get("manifest_date_sha256") != digest):
            raise RuntimeError("stale or invalid archive seal: %s" % path)
        raw_rows = seal.get("raw_files")
        if not isinstance(raw_rows, list) or not raw_rows:
            raise RuntimeError("sealed raw inventory missing for %s" % date)
        expected_raw = {}
        for row in raw_rows:
            fpath = os.path.abspath(os.path.join(raw_root, row.get("file", "")))
            expected_raw[fpath] = row
        disk_raw = {os.path.abspath(p) for p in wc.seal_raw_files(
            raw_root, date, int(seal.get("receipt_cross_day_hours", 2)),
            warehouse_root=warehouse_root)}
        if disk_raw != set(expected_raw):
            raise RuntimeError("sealed raw file-set mismatch for %s: missing=%s extra=%s"
                               % (date, sorted(set(expected_raw) - disk_raw),
                                  sorted(disk_raw - set(expected_raw))))
        for fpath, row in sorted(expected_raw.items()):
            st = os.stat(fpath)
            if (int(row.get("size", -1)) != st.st_size or
                    int(row.get("checkpoint", -1)) != st.st_size):
                raise RuntimeError("sealed raw file changed after seal: %s" % fpath)
            stat_key = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            recorded_stat = (int(row.get("inode", -1)), int(row.get("size", -1)),
                             int(row.get("mtime_ns", -1)),
                             int(row.get("ctime_ns", -1)))
            cache_key = (fpath, row.get("sha256"))
            if stat_key != recorded_stat and _VALIDATED_RAW.get(cache_key) != stat_key:
                h = hashlib.sha256()
                with open(fpath, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != row.get("sha256"):
                    raise RuntimeError("sealed raw sha256 mismatch: %s" % fpath)
                _VALIDATED_RAW[cache_key] = stat_key
        table_rows = [r for r in manifest_rows if r.get("table") == table]
        sealed_archive_stats = {
            os.path.abspath(os.path.join(archive_root, r.get("file", ""))): r
            for r in seal.get("archive_file_stats", []) if r.get("table") == table
        }
        expected = {}
        for row in table_rows:
            # Reconstruct from logical partition identity, not manifest's
            # original-host file_path.  A sealed warehouse copied/restored under
            # a new root must remain verifiable byte-for-byte.
            cat, sub = row.get("category"), row.get("subcategory")
            # Manifest's sentinel represents a SQL NULL; convert it back before
            # path sanitization or `_unclassified` would lose its underscores.
            cat = None if cat == "_unclassified" else cat
            sub = None if sub == "_unclassified" else sub
            fpath = os.path.abspath(os.path.join(
                wc.partition_dir(archive_root, table, cat, sub, date),
                wc.partition_file(table, cat, sub, date, _EXT[table])))
            if fpath in expected:
                raise RuntimeError("duplicate sealed archive path: %s" % fpath)
            expected[fpath] = row.get("file_md5")
        disk = {os.path.abspath(p) for p in _glob.glob(os.path.join(
            archive_root, table, "category=*", "subcategory=*", "date=%s" % date,
            "*.%s" % _EXT[table]))}
        if disk != set(expected):
            raise RuntimeError("sealed archive file-set mismatch for %s/%s: "
                               "missing=%s extra=%s"
                               % (date, table, sorted(set(expected) - disk),
                                  sorted(disk - set(expected))))
        cache_key = (os.path.abspath(warehouse_root), date, table, digest)
        prior = _VALIDATED_ARCHIVES.get(cache_key, {})
        current = {}
        for fpath, recorded_md5 in sorted(expected.items()):
            st = os.stat(fpath)
            stat_key = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            sealed_stat = sealed_archive_stats.get(fpath, {})
            recorded_stat = (int(sealed_stat.get("inode", -1)),
                             int(sealed_stat.get("size", -1)),
                             int(sealed_stat.get("mtime_ns", -1)),
                             int(sealed_stat.get("ctime_ns", -1)))
            if stat_key != recorded_stat and prior.get(fpath) != stat_key:
                actual_md5 = _md5_file(fpath)
                if actual_md5 != recorded_md5:
                    raise RuntimeError("sealed archive md5 mismatch: %s" % fpath)
            current[fpath] = stat_key
        _VALIDATED_ARCHIVES[cache_key] = current
        day += datetime.timedelta(days=1)


def load(table, category=None, subcategory=None, group=None, start=None, end=None,
         columns=None, ffill=False, warehouse=None, event=None, index_path=None,
         archive_only=None):
    """Return a relation over archive files and, by default, live staging.

    A fully bounded range ending before the current UTC day automatically uses
    sealed archive-only mode. ``archive_only=True`` forces that mode;
    ``archive_only=False`` is the explicit diagnostic/migration escape hatch.
    Archive-only never
    opens or ATTACHes ``staging.duckdb``.  This is a correctness and liveness
    boundary, not just a query optimization.  A long-running read-only ATTACH
    prevents DuckDB's ingest writer from opening the database.  Missing archive
    coverage therefore fails loudly instead of falling back to live staging.
    """
    if table not in TABLES:
        raise ValueError("table must be one of %s" % (TABLES,))
    global _CON
    cfg = wc.load_config()
    warehouse = warehouse or cfg["warehouse_root"]
    # Three-axis selector: event= resolves its window + market set from the index
    # so callers never pass calendar dates for an episode (PLAN §3.5). Explicit
    # start/end still override the index window if given.
    event_markets = None
    if event is not None:
        idx = index_path or os.path.join(
            warehouse if warehouse != cfg["warehouse_root"] else ".",
            "event_packs" if warehouse != cfg["warehouse_root"] else "work/event_packs",
            "index.parquet")
        ecat, event_markets, ews, ewe = _resolve_event(event, idx)
        category = category or ecat
        start = ews if start is None else start
        end = ewe if end is None else end
    staging = cfg["staging_db"] if warehouse == cfg["warehouse_root"] else \
        os.path.join(warehouse, "staging.duckdb")
    archive_root = cfg["archive_root"] if warehouse == cfg["warehouse_root"] else \
        os.path.join(warehouse, "facts")
    if archive_only is None:
        s_probe, e_probe = _to_us(start), _to_us(end, end=True)
        today_start = wc.day_start_us(
            datetime.datetime.now(datetime.timezone.utc).date().isoformat())
        archive_only = (s_probe is not None and e_probe is not None and
                        e_probe > s_probe and e_probe <= today_start)
    if archive_only:
        # A process that previously performed a live load already holds the
        # staging reader lock.  Crossing into sealed mode is an explicit source
        # boundary: close the cached connection (and invalidate its old relation
        # handles) before constructing the archive-only relation.
        if _ATTACHED:
            if _CON is not None:
                _CON.close()
            _CON = None
            _ATTACHED.clear()
        raw_root = cfg["raw_root"] if warehouse == cfg["warehouse_root"] else \
            os.path.join(warehouse, "raw")
        _require_sealed_dates(warehouse, archive_root, raw_root, table, start, end)
    con = _conn()  # persistent so the returned relation stays valid

    s_us, e_us = _to_us(start), _to_us(end, end=True)
    files, part_max = _archive_files(archive_root, table, category, subcategory,
                                     s_us, e_us)
    parts = []
    if files:
        lst = ", ".join("'%s'" % f.replace("'", "''") for f in files)
        if _EXT[table] == "parquet":
            parts.append("SELECT * FROM read_parquet([%s], union_by_name=true, "
                         "hive_partitioning=false)" % lst)
        else:
            # Explicit types for string columns: DuckDB's sniffer narrows
            # 'yes'/'no' taker_side to BOOLEAN (caught 2026-07-07 when the
            # archived day fed the gold build and every trade fail-closed).
            # Never let type inference touch identity/enum columns.
            str_cols = ("market_ticker", "series_ticker", "event_ticker",
                        "category", "subcategory", "group", "trade_id",
                        "taker_side")
            types = ", ".join("'%s': 'VARCHAR'" % c for c in str_cols)
            parts.append(
                "SELECT * FROM read_csv([%s], header=true, union_by_name=true, "
                "hive_partitioning=false, types={%s})" % (lst, types))
    if not archive_only and os.path.exists(staging):
        if staging not in _ATTACHED:
            # The ingest daemon holds the write lock briefly each cycle; retry
            # through that window instead of failing the analysis call.
            import time as _time
            for attempt in range(8):
                try:
                    con.execute("ATTACH '%s' AS stg (READ_ONLY)"
                                % staging.replace("'", "''"))
                    _ATTACHED.add(staging)
                    break
                except Exception:
                    if attempt == 7:
                        raise
                    _time.sleep(1.5)
        stg = "SELECT * FROM stg.%s" % table
        if part_max:  # archived days are final per partition — never re-read from staging
            stg = _staging_dedup_sql(stg, part_max)
        parts.append(stg)
    if not parts:
        source = "archive" if archive_only else "staging or archive"
        raise FileNotFoundError("no %s data for %s" % (source, table))

    base = " UNION ALL BY NAME ".join("(%s)" % p for p in parts)
    where = []
    if category:    where.append("category = '%s'" % category.replace("'", "''"))
    if subcategory: where.append("subcategory = '%s'" % subcategory.replace("'", "''"))
    if group:       where.append('"group" = \'%s\'' % group.replace("'", "''"))
    if event_markets is not None:  # event= restricts to the unit's market set
        where.append("market_ticker IN (%s)"
                     % ", ".join("'%s'" % m.replace("'", "''") for m in event_markets))
    if s_us is not None: where.append("ts_utc >= %d" % s_us)
    if e_us is not None: where.append("ts_utc < %d" % e_us)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    cols = ", ".join(columns) if columns else "*"

    sql = "SELECT %s FROM (%s)%s ORDER BY market_ticker, ts_utc" % (cols, base, w)

    if ffill and table == "orderbooks_l1":
        # LOCF within each market: carry the last non-null book value forward.
        sql = """
        SELECT ts_utc, market_ticker, category, subcategory, "group",
          last_value(yes_bid_e4 IGNORE NULLS) OVER w AS yes_bid_e4,
          last_value(yes_bid_qty_e4 IGNORE NULLS) OVER w AS yes_bid_qty_e4,
          last_value(yes_ask_e4 IGNORE NULLS) OVER w AS yes_ask_e4,
          last_value(yes_ask_qty_e4 IGNORE NULLS) OVER w AS yes_ask_qty_e4,
          is_snapshot
        FROM (%s)
        WINDOW w AS (PARTITION BY market_ticker ORDER BY ts_utc
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
        ORDER BY market_ticker, ts_utc""" % ("SELECT * FROM (%s)%s" % (base, w))

    return con.sql(sql)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("table", choices=TABLES)
    ap.add_argument("--category"); ap.add_argument("--subcategory"); ap.add_argument("--group")
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--ffill", action="store_true")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args(argv[1:])
    rel = load(args.table, category=args.category, subcategory=args.subcategory,
               group=args.group, start=args.start, end=args.end, ffill=args.ffill)
    n = rel.count("*").fetchone()[0]
    print("rows: %d" % n)
    rel.limit(args.limit).show()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
