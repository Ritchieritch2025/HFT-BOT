"""W-A4 regression: dim_snapshot must survive catalog parquets whose OPTIONAL
columns are absent — a fresh crawl writes only the columns present in the
data (union_by_name), so e.g. cap_strike vanishes whenever no open market
carries it (live incident, 2026-07-09: Binder Error at cutover step 4).
Offline: builds fixture parquets in a temp warehouse and runs dim_snapshot's
main() against them."""
import os
import sys
import tempfile

import duckdb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import dim_snapshot  # noqa: E402


def _write_parquet(path, rows_sql):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    duckdb.connect().execute(
        "COPY (%s) TO '%s' (FORMAT PARQUET)" % (rows_sql, path))


def _make_warehouse(tmp, with_cap_strike):
    cat = os.path.join(tmp, "catalog")
    strike_col = ", 2.5 AS cap_strike" if with_cap_strike else ""
    _write_parquet(
        os.path.join(cat, "markets", "part-00000.parquet"),
        "SELECT 'T-A' AS ticker, 'EV-1' AS event_ticker, "
        "1.5 AS floor_strike%s UNION ALL "
        "SELECT 'T-B', 'EV-1', 3.5%s" % (strike_col, strike_col))
    _write_parquet(
        os.path.join(cat, "events", "part-00000.parquet"),
        "SELECT 'EV-1' AS event_ticker, true AS mutually_exclusive")
    _write_parquet(
        os.path.join(cat, "series", "part-00000.parquet"),
        "SELECT 'SER' AS ticker, 'Sports' AS category")
    return tmp


def _run(warehouse):
    rc = dim_snapshot.main(["dim_snapshot", "--warehouse", warehouse,
                            "--date", "2026-01-01"])
    assert rc in (0, None), "dim_snapshot failed rc=%r" % rc
    out = os.path.join(warehouse, "dim", "latest", "markets.csv")
    assert os.path.exists(out), "markets snapshot csv missing"
    return open(out).read()


def test_survives_missing_cap_strike_column():
    with tempfile.TemporaryDirectory() as tmp:
        csv = _run(_make_warehouse(tmp, with_cap_strike=False))
        # bracket structure still derived from floor_strike alone
        assert "bracket" in csv


def test_full_schema_still_works():
    with tempfile.TemporaryDirectory() as tmp:
        csv = _run(_make_warehouse(tmp, with_cap_strike=True))
        assert "bracket" in csv
