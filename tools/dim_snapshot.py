#!/usr/bin/env python3
"""Daily dimension snapshots: catalog parquets -> dated CSVs + latest, with
derived market fields.

Dual-writes (per docs/warehouse_schema.md):
  <warehouse>/dim/snapshots/date=<YYYY-MM-DD>/{series,events,markets}.csv
  <warehouse>/dim/latest/{series,events,markets}.csv

`markets` gains two derived fields (dim tables remain the raw source of truth;
these are conveniences inferred at snapshot time):
  event_structure : bracket | binary | multi_outcome | head_to_head | cumulative
      inferred from per-event market count + whether strikes are numeric +
      event.mutually_exclusive:
        1 market                     -> binary
        >1, numeric strikes, ME      -> bracket
        >1, numeric strikes, not ME  -> cumulative
        2, no strikes                -> head_to_head
        >2, no strikes               -> multi_outcome
  bracket_rank    : strike-ordered position within bracket events (null otherwise)

Runs entirely from the local catalog parquets written by tools/catalog_sync.py —
no network. stdlib + duckdb only.
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402


def has_col(con, rel, col):
    try:
        cols = [d[0] for d in con.execute("SELECT * FROM %s LIMIT 0" % rel).description]
        return col in cols
    except Exception:
        return False


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--warehouse", default=None)
    ap.add_argument("--date", default=None, help="snapshot date (default: today UTC)")
    args = ap.parse_args(argv[1:])
    import duckdb

    cfg = wc.load_config()
    warehouse = args.warehouse or cfg["warehouse_root"]
    date = args.date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    cat = os.path.join(warehouse, "catalog")
    snap_dir = os.path.join(warehouse, "dim", "snapshots", "date=%s" % date)
    latest_dir = os.path.join(warehouse, "dim", "latest")
    os.makedirs(snap_dir, exist_ok=True)
    os.makedirs(latest_dir, exist_ok=True)

    con = duckdb.connect()
    written = 0
    for name in ("series", "events", "markets"):
        pq = os.path.join(cat, name, "part-00000.parquet")
        if not os.path.exists(pq):
            print("  %-8s skipped (no catalog parquet at %s)" % (name, pq))
            continue
        rel = "read_parquet('%s')" % pq.replace("'", "''")

        if name == "markets":
            me = ("coalesce(e.mutually_exclusive, true)"
                  if os.path.exists(os.path.join(cat, "events", "part-00000.parquet"))
                  and has_col(con, "read_parquet('%s')" % os.path.join(
                      cat, "events", "part-00000.parquet").replace("'", "''"),
                      "mutually_exclusive")
                  else "true")
            ev_join = ""
            if "e." in me:
                ev_join = ("LEFT JOIN read_parquet('%s') e ON m.event_ticker = e.event_ticker"
                           % os.path.join(cat, "events", "part-00000.parquet").replace("'", "''"))
            # W-A4 incident (2026-07-09): a fresh catalog crawl produced a
            # markets parquet with floor_strike but NO cap_strike column (no
            # open market carried the field; union_by_name creates only the
            # columns present in the data) — referencing an absent column
            # unconditionally is a Binder Error. Build the expression from
            # the columns that actually exist (D3: validate at the boundary).
            strike_cols = [c for c in ("floor_strike", "cap_strike")
                           if has_col(con, rel, c)]
            strike = ("coalesce(%s)" % ", ".join(
                          "try_cast(m.%s AS DOUBLE)" % c for c in strike_cols)
                      if strike_cols else "NULL::DOUBLE")
            sel = """
            WITH base AS (
              SELECT m.*, %s AS _strike, %s AS _me,
                     count(*) OVER (PARTITION BY m.event_ticker) AS _n,
                     count(%s) OVER (PARTITION BY m.event_ticker) AS _n_numeric
              FROM %s m %s
            )
            SELECT * EXCLUDE (_strike, _me, _n, _n_numeric),
              CASE
                WHEN _n = 1 THEN 'binary'
                WHEN _n_numeric = _n AND _me THEN 'bracket'
                WHEN _n_numeric = _n AND NOT _me THEN 'cumulative'
                WHEN _n = 2 THEN 'head_to_head'
                ELSE 'multi_outcome'
              END AS event_structure,
              CASE WHEN _n_numeric = _n AND _n > 1 AND _me THEN
                row_number() OVER (PARTITION BY event_ticker ORDER BY _strike)
              END AS bracket_rank
            FROM base""" % (strike, me, "_strike", rel, ev_join)
        else:
            sel = "SELECT * FROM %s" % rel

        n = con.execute("SELECT count(*) FROM (%s)" % sel).fetchone()[0]
        for out_dir in (snap_dir, latest_dir):
            dst = os.path.join(out_dir, "%s.csv" % name).replace("'", "''")
            con.execute("COPY (%s) TO '%s' (FORMAT CSV, HEADER)" % (sel, dst))
        print("  %-8s %6d rows -> snapshots/date=%s + latest" % (name, n, date))
        written += 1

    print("DIM SNAPSHOT %s: %d table(s)" % ("PASS" if written else "EMPTY", written))
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
