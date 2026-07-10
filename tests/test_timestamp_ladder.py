"""W-TL1 §3 acceptance: old archive files are NEVER rewritten; they read back
NULL in the four timestamp-ladder columns through warehouse.load()'s
union_by_name, while new rows carry real values with stable BIGINT dtypes.

Covers the two archive formats:
  - orderbooks_l1: pre-TL1 16-col parquet UNION new 20-col staging
  - trades: pre-TL1 12-col csv.gz UNION new staging, INCLUDING the dtype trap
    where a new csv.gz whose ladder column is all-NULL sniffs as VARCHAR and
    (unpinned) would drag the unioned dtype away from BIGINT
plus the export leg: export_day's SELECT * carries the four columns into new
partition files (old files untouched by construction — write-once, D1).
"""
import gzip
import json
import os
import sys

import duckdb
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ingest  # noqa: E402
import warehouse as wh  # noqa: E402
import warehouse_common as wc  # noqa: E402

LADDER = ["exchange_ts_us", "recv_wall_ns", "recv_mono_ns", "local_recv_ts_us"]

DAY_OLD = "2026-07-01"          # pre-TL1 archive day
DAY_NEW = "2026-07-02"          # ladder-era staging day
T_OLD = wc.day_start_us(DAY_OLD) + 3_600_000_000
T_NEW = wc.day_start_us(DAY_NEW) + 3_600_000_000
MT = "KXBTC-25DEC31-B50"


@pytest.fixture(autouse=True)
def _reset_wh_conn():
    if wh._CON is not None:
        try:
            wh._CON.close()
        except Exception:
            pass
    wh._CON = None
    wh._ATTACHED.clear()
    yield


def _classification(root):
    out = os.path.join(root, "catalog", "series_classified")
    os.makedirs(out)
    con = duckdb.connect()
    con.execute("CREATE TABLE c (series_ticker TEXT, category TEXT, "
                "subcategory TEXT, \"group\" TEXT, record_class TEXT)")
    con.execute("INSERT INTO c VALUES ('KXBTC','Crypto','BTC','BTC','A')")
    con.execute("COPY c TO '%s' (FORMAT PARQUET)"
                % os.path.join(out, "part-00000.parquet").replace("'", "''"))


def _old_archive_l1(root):
    """Pre-TL1 16-column parquet, exactly as a 2026-07-0x export wrote it."""
    d = wc.partition_dir(os.path.join(root, "facts"), "orderbooks_l1",
                         "Crypto", "BTC", DAY_OLD)
    os.makedirs(d)
    f = os.path.join(d, wc.partition_file("orderbooks_l1", "Crypto", "BTC",
                                          DAY_OLD, "parquet"))
    con = duckdb.connect()
    con.execute("""CREATE TABLE t (
      ts_utc BIGINT, market_ticker VARCHAR, series_ticker VARCHAR,
      event_ticker VARCHAR, category VARCHAR, subcategory VARCHAR,
      "group" VARCHAR, record_class VARCHAR, yes_bid_e4 INTEGER,
      yes_bid_qty_e4 BIGINT, yes_ask_e4 INTEGER, yes_ask_qty_e4 BIGINT,
      price_e4 INTEGER, volume_e4 BIGINT, open_interest_e4 BIGINT,
      is_snapshot BOOLEAN)""")
    con.execute("INSERT INTO t VALUES (%d, '%s', 'KXBTC', 'KXBTC-25DEC31', "
                "'Crypto', 'BTC', 'BTC', 'A', 4000, 1000000, 4200, 1000000, "
                "4100, 100000, 50000, true)" % (T_OLD, MT))
    con.execute("COPY t TO '%s' (FORMAT PARQUET)" % f.replace("'", "''"))


def _old_archive_trades(root):
    """Pre-TL1 12-column csv.gz."""
    d = wc.partition_dir(os.path.join(root, "facts"), "trades",
                         "Crypto", "BTC", DAY_OLD)
    os.makedirs(d)
    f = os.path.join(d, wc.partition_file("trades", "Crypto", "BTC",
                                          DAY_OLD, "csv.gz"))
    hdr = ("ts_utc,market_ticker,series_ticker,event_ticker,category,"
           "subcategory,group,trade_id,yes_price_e4,no_price_e4,count_e4,taker_side")
    row = ("%d,%s,KXBTC,KXBTC-25DEC31,Crypto,BTC,BTC,old-t1,5000,5000,10000,yes"
           % (T_OLD, MT))
    with gzip.open(f, "wt") as g:
        g.write(hdr + "\n" + row + "\n")


def _new_staging(root):
    """Ladder-era staging built by the production Ingester from a raw envelope."""
    con = duckdb.connect(os.path.join(root, "staging.duckdb"))
    ing = ingest.Ingester(con, root)
    cap = os.path.join(root, "cap.ndjson")
    tick = {"recv_mono_ns": 424242, "recv_wall_ns": (T_NEW + 300_000) * 1000,
            "raw": json.dumps({"type": "ticker", "msg": {
                "market_ticker": MT, "ts_ms": T_NEW // 1000,
                "yes_bid_dollars": "0.4100", "yes_ask_dollars": "0.4300",
                "yes_bid_size_fp": "100.00", "yes_ask_size_fp": "100.00"}})}
    tr = {"recv_mono_ns": 424243, "recv_wall_ns": (T_NEW + 400_000) * 1000,
          "raw": json.dumps({"type": "trade", "msg": {
              "market_ticker": MT, "ts_ms": (T_NEW + 100_000) // 1000,
              "trade_id": "new-t1", "yes_price_dollars": "0.4200",
              "no_price_dollars": "0.5800", "count_fp": "1.00",
              "taker_side": "yes"}})}
    with open(cap, "w") as f:
        f.write(json.dumps(tick) + "\n" + json.dumps(tr) + "\n")
    ing.process_file(cap)
    con.close()


@pytest.fixture()
def root(tmp_path):
    r = str(tmp_path / "warehouse")
    os.makedirs(r)
    _classification(r)
    _old_archive_l1(r)
    _old_archive_trades(r)
    _new_staging(r)
    return r


def test_l1_union_old_parquet_reads_null_ladder(root):
    rel = wh.load("orderbooks_l1", warehouse=root, start=DAY_OLD, end=DAY_NEW)
    df = rel.df()
    assert set(LADDER) <= set(df.columns)
    old = df[df.ts_utc == T_OLD].iloc[0]
    new = df[df.ts_utc == T_NEW].iloc[0]
    import pandas as pd
    assert all(pd.isna(old[c]) for c in LADDER)
    assert int(new["exchange_ts_us"]) == (T_NEW // 1000) * 1000
    assert int(new["recv_wall_ns"]) == (T_NEW + 300_000) * 1000
    assert int(new["recv_mono_ns"]) == 424242
    assert int(new["local_recv_ts_us"]) == T_NEW + 300_000


def test_trades_union_old_csv_reads_null_ladder(root):
    rel = wh.load("trades", warehouse=root, start=DAY_OLD, end=DAY_NEW)
    df = rel.df()
    assert set(LADDER) <= set(df.columns)
    old = df[df.trade_id == "old-t1"].iloc[0]
    new = df[df.trade_id == "new-t1"].iloc[0]
    import pandas as pd
    assert all(pd.isna(old[c]) for c in LADDER)
    assert int(new["exchange_ts_us"]) == ((T_NEW + 100_000) // 1000) * 1000
    assert int(new["local_recv_ts_us"]) == T_NEW + 400_000


def test_trades_all_null_csv_ladder_column_stays_bigint(root):
    """A new-era csv.gz whose exchange_ts_us is ALL NULL sniffs as VARCHAR;
    load() must pin it back to BIGINT so the union dtype never degrades."""
    d = wc.partition_dir(os.path.join(root, "facts"), "trades",
                         "Crypto", "BTC", DAY_NEW)
    os.makedirs(d)
    f = os.path.join(d, wc.partition_file("trades", "Crypto", "BTC",
                                          DAY_NEW, "csv.gz"))
    hdr = ("ts_utc,market_ticker,series_ticker,event_ticker,category,"
           "subcategory,group,trade_id,yes_price_e4,no_price_e4,count_e4,"
           "taker_side,exchange_ts_us,recv_wall_ns,recv_mono_ns,local_recv_ts_us")
    row = ("%d,%s,KXBTC,KXBTC-25DEC31,Crypto,BTC,BTC,allnull-t1,5000,5000,"
           "10000,yes,,,," % (T_NEW + 1, MT))
    with gzip.open(f, "wt") as g:
        g.write(hdr + "\n" + row + "\n")
    rel = wh.load("trades", warehouse=root, start=DAY_OLD, end=DAY_NEW)
    types = dict(zip(rel.columns, [str(t) for t in rel.types]))
    for c in LADDER:
        assert types[c] == "BIGINT", (c, types[c])
    df = rel.df()
    assert "allnull-t1" in set(df.trade_id)


def test_export_carries_ladder_into_new_partition_files(root, tmp_path):
    """export_day's SELECT * writes the four columns into NEW files only;
    the pre-TL1 partition file on disk is untouched (write-once)."""
    import export_day
    old_file = os.path.join(
        wc.partition_dir(os.path.join(root, "facts"), "orderbooks_l1",
                         "Crypto", "BTC", DAY_OLD),
        wc.partition_file("orderbooks_l1", "Crypto", "BTC", DAY_OLD, "parquet"))
    before = (os.path.getmtime(old_file), os.path.getsize(old_file))

    con = duckdb.connect()
    con.execute("ATTACH '%s' AS stg (READ_ONLY)"
                % os.path.join(root, "staging.duckdb").replace("'", "''"))
    day_lo = wc.day_start_us(DAY_NEW)
    rows = export_day.export_table(con, "orderbooks_l1", "parquet", DAY_NEW,
                                   day_lo, day_lo + 86_400_000_000,
                                   str(tmp_path / "out"), force=False)
    assert rows and rows[0]["row_count"] == 1
    import glob as g
    written = g.glob(str(tmp_path / "out" / "**" / "*.parquet"), recursive=True)[0]
    cols = {r[0] for r in con.execute(
        "SELECT name FROM parquet_schema('%s')" % written.replace("'", "''")).fetchall()}
    assert set(LADDER) <= cols
    got = con.execute("SELECT exchange_ts_us, local_recv_ts_us FROM "
                      "read_parquet('%s')" % written.replace("'", "''")).fetchone()
    assert got == ((T_NEW // 1000) * 1000, T_NEW + 300_000)
    assert (os.path.getmtime(old_file), os.path.getsize(old_file)) == before
