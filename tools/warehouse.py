#!/usr/bin/env python3
"""Single analysis entry point for the Kalshi warehouse.

    load(table, category=None, subcategory=None, group=None,
         start=None, end=None, columns=None, ffill=False) -> duckdb relation

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
import glob as _glob
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


def load(table, category=None, subcategory=None, group=None, start=None, end=None,
         columns=None, ffill=False, warehouse=None, event=None, index_path=None):
    if table not in TABLES:
        raise ValueError("table must be one of %s" % (TABLES,))
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
    con = _conn()  # persistent so the returned relation stays valid

    s_us, e_us = _to_us(start), _to_us(end, end=True)
    files, part_max = _archive_files(archive_root, table, category, subcategory,
                                     s_us, e_us)
    parts = []
    if files:
        lst = ", ".join("'%s'" % f.replace("'", "''") for f in files)
        if _EXT[table] == "parquet":
            parts.append("SELECT * FROM read_parquet([%s], union_by_name=true)" % lst)
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
                "types={%s})" % (lst, types))
    if os.path.exists(staging):
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
        raise FileNotFoundError("no staging or archive data for %s" % table)

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
