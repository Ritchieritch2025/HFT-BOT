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
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import publication_generation as pg  # noqa: E402
import warehouse_common as wc  # noqa: E402


CATALOG_MEMBERS = {
    "catalog/series/part-00000.parquet",
    "catalog/events/part-00000.parquet",
    "catalog/markets/part-00000.parquet",
    "catalog/settlements/part-00000.parquet",
    "catalog/series_classified/part-00000.parquet",
}
DIM_NAMES = ("series", "events", "markets")


def has_col(con, rel, col):
    try:
        cols = [d[0] for d in con.execute("SELECT * FROM %s LIMIT 0" % rel).description]
        return col in cols
    except Exception:
        return False


def _catalog_paths(warehouse):
    root = os.path.join(warehouse, "catalog")
    if os.path.lexists(root) and os.path.islink(root):
        raise pg.GenerationError("catalog root is a symlink")
    paths = set()
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in dirs:
            if os.path.islink(os.path.join(base, name)):
                raise pg.GenerationError("catalog directory is a symlink")
        for name in sorted(files):
            full = os.path.join(base, name)
            if os.path.islink(full):
                raise pg.GenerationError("catalog member is a symlink")
            rel = "catalog/" + os.path.relpath(
                full, root).replace(os.sep, "/")
            if rel not in CATALOG_MEMBERS:
                raise pg.GenerationError("unexpected catalog member %s" % rel)
            paths.add(rel)
    return paths


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
    stage_parent = os.path.join(warehouse, ".publication-stage")
    if os.path.lexists(stage_parent) and os.path.islink(stage_parent):
        raise pg.GenerationError("publication stage is a symlink")
    os.makedirs(stage_parent, mode=0o700, exist_ok=True)
    stage_root = tempfile.mkdtemp(prefix="dim-%s-" % date, dir=stage_parent)
    snap_rel_root = "dim/snapshots/date=%s" % date
    latest_rel_root = "dim/latest"
    try:
        # Catalog remains shared-locked from source validation through staged
        # query completion.  The dim lock is exclusive only for final replace.
        with pg.generation_locks(
                warehouse, {"catalog": "shared"}, timeout=30.0):
            catalog_paths = _catalog_paths(warehouse)
            required_catalog = {
                "catalog/%s/part-00000.parquet" % name
                for name in DIM_NAMES}
            missing_catalog = required_catalog - catalog_paths
            if missing_catalog:
                raise pg.GenerationError(
                    "dim source catalog is incomplete: %s" %
                    sorted(missing_catalog))
            catalog_generation, _catalog_state = (
                pg.verified_or_synthesized_manifest(
                    warehouse, "catalog", catalog_paths))

            con = duckdb.connect()
            written = 0
            try:
                for name in DIM_NAMES:
                    pq = os.path.join(
                        cat, name, "part-00000.parquet")
                    rel = "read_parquet('%s')" % pq.replace("'", "''")

                    if name == "markets":
                        event_path = os.path.join(
                            cat, "events", "part-00000.parquet")
                        me = (
                            "coalesce(e.mutually_exclusive, true)"
                            if has_col(
                                con, "read_parquet('%s')" %
                                event_path.replace("'", "''"),
                                "mutually_exclusive") else "true")
                        ev_join = ""
                        if "e." in me:
                            ev_join = (
                                "LEFT JOIN read_parquet('%s') e "
                                "ON m.event_ticker = e.event_ticker" %
                                event_path.replace("'", "''"))
                        strike_cols = [
                            column for column in
                            ("floor_strike", "cap_strike")
                            if has_col(con, rel, column)]
                        strike = (
                            "coalesce(%s)" % ", ".join(
                                "try_cast(m.%s AS DOUBLE)" % column
                                for column in strike_cols)
                            if strike_cols else "NULL::DOUBLE")
                        sel = """
                        WITH base AS (
                          SELECT m.*, %s AS _strike, %s AS _me,
                            count(*) OVER (PARTITION BY m.event_ticker) AS _n,
                            count(%s) OVER (PARTITION BY m.event_ticker)
                              AS _n_numeric
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
                            row_number() OVER (
                              PARTITION BY event_ticker ORDER BY _strike)
                          END AS bracket_rank
                        FROM base""" % (
                            strike, me, "_strike", rel, ev_join)
                    else:
                        sel = "SELECT * FROM %s" % rel

                    n = con.execute(
                        "SELECT count(*) FROM (%s)" % sel).fetchone()[0]
                    dated_rel = "%s/%s.csv" % (snap_rel_root, name)
                    latest_rel = "%s/%s.csv" % (latest_rel_root, name)
                    dated = os.path.join(
                        stage_root, *dated_rel.split("/"))
                    latest = os.path.join(
                        stage_root, *latest_rel.split("/"))
                    os.makedirs(os.path.dirname(dated), exist_ok=True)
                    os.makedirs(os.path.dirname(latest), exist_ok=True)
                    con.execute(
                        "COPY (%s) TO '%s' (FORMAT CSV, HEADER)" %
                        (sel, dated.replace("'", "''")))
                    os.link(dated, latest)
                    print("  %-8s %6d rows -> staged date=%s + latest" %
                          (name, n, date))
                    written += 1
            finally:
                con.close()

            if written != len(DIM_NAMES):
                raise pg.GenerationError("dated dim generation is incomplete")
            dated_paths = [
                "%s/%s.csv" % (snap_rel_root, name)
                for name in DIM_NAMES]
            all_paths = dated_paths + [
                "%s/%s.csv" % (latest_rel_root, name)
                for name in DIM_NAMES]
            manifest = pg.build_manifest(
                "dim", pg.attest_files(stage_root, dated_paths), date=date,
                source_catalog_generation_id=catalog_generation[
                    "generation_id"])
            with pg.generation_locks(
                    warehouse, {"dim": "exclusive"}, timeout=30.0):
                pg.publish_transaction(
                    warehouse,
                    {rel: os.path.join(stage_root, *rel.split("/"))
                     for rel in all_paths},
                    manifest,
                    aliases={
                        "%s/%s.csv" % (latest_rel_root, name):
                        "%s/%s.csv" % (snap_rel_root, name)
                        for name in DIM_NAMES
                    })

        print("DIM SNAPSHOT PASS: %d table(s) [generation %s, catalog %s]" %
              (written, manifest["generation_id"][:16],
               catalog_generation["generation_id"][:16]))
        return 0
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
