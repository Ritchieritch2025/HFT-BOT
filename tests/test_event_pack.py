"""W-E2: event pack materializer (PLAN_EVENT_PACKAGING §3.3/§3.6).

Builds a synthetic two-day archive (trades csv.gz + L1 parquet spanning UTC
midnight) and asserts: cross-day reassembly into one pack, market filtering,
row counts match a direct SQL filter, MONEY-INTEGRITY (E4 integer columns
byte-exact, no float / no sub-penny loss — AF-1/AF-2), idempotent rebuild, the
AF-5 stale-window refuse/refresh behaviour, and sealed-only packing.
"""
import calendar
import csv
import datetime as _dt
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import event_pack as ep  # noqa: E402


def _ts(s):
    return int(calendar.timegm(_dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timetuple()) * 1_000_000)


def _make_warehouse(root):
    """Synthetic archive: facts/<table>/category=Sports/subcategory=NBA/date=D/*."""
    import duckdb
    con = duckdb.connect()

    def _copy(table, ddl, rows, date, ext, fmt):
        d = os.path.join(root, "facts", table, "category=Sports", "subcategory=NBA",
                         "date=%s" % date)
        os.makedirs(d, exist_ok=True)
        con.execute("CREATE OR REPLACE TABLE x (%s)" % ddl)
        con.executemany("INSERT INTO x VALUES (%s)" % ",".join("?" * (ddl.count(",") + 1)), rows)
        con.execute("COPY x TO '%s' (%s)" % (os.path.join(d, "part." + ext), fmt))

    T_DDL = ('ts_utc BIGINT, market_ticker VARCHAR, series_ticker VARCHAR, '
             'event_ticker VARCHAR, category VARCHAR, subcategory VARCHAR, '
             '"group" VARCHAR, trade_id VARCHAR, yes_price_e4 INTEGER, '
             'no_price_e4 INTEGER, count_e4 BIGINT, taker_side VARCHAR')
    def tr(ts, mk, tid, yp, np_, cnt, side):
        return (_ts(ts), mk, "KXNBA", "KX-SPORT-EV1" if mk.startswith("KX-SPORT") else "KX-OTHER",
                "Sports", "NBA", "game", tid, yp, np_, cnt, side)
    # sub-penny price (0.0090) + fractional count (1.2345) exercise E4 exactness.
    _copy("trades", T_DDL, [
        tr("2026-07-06 22:30:00", "KX-SPORT-A", "t1", 90, 9910, 12345, "yes"),
        tr("2026-07-06 23:15:00", "KX-SPORT-B", "t2", 5500, 4500, 100000, "no"),
    ], "2026-07-06", "csv.gz", "FORMAT csv, HEADER, COMPRESSION gzip")
    _copy("trades", T_DDL, [
        tr("2026-07-07 00:20:00", "KX-SPORT-A", "t3", 6000, 4000, 30000, "yes"),
        tr("2026-07-07 01:00:00", "KX-SPORT-B", "t4", 4200, 5800, 5000, "no"),
        tr("2026-07-07 00:30:00", "KX-OTHER-1", "t5", 5000, 5000, 10000, "yes"),  # other event
    ], "2026-07-07", "csv.gz", "FORMAT csv, HEADER, COMPRESSION gzip")

    L_DDL = ('ts_utc BIGINT, market_ticker VARCHAR, series_ticker VARCHAR, '
             'event_ticker VARCHAR, category VARCHAR, subcategory VARCHAR, '
             '"group" VARCHAR, record_class VARCHAR, yes_bid_e4 INTEGER, '
             'yes_bid_qty_e4 BIGINT, yes_ask_e4 INTEGER, yes_ask_qty_e4 BIGINT, '
             'price_e4 INTEGER, volume_e4 BIGINT, open_interest_e4 BIGINT, is_snapshot BOOLEAN')
    def l1(ts, mk, snap, bid, bidq, ask, askq):
        return (_ts(ts), mk, "KXNBA", "KX-SPORT-EV1" if mk.startswith("KX-SPORT") else "KX-OTHER",
                "Sports", "NBA", "game", "l1", bid, bidq, ask, askq, 0, 0, 0, snap)
    _copy("orderbooks_l1", L_DDL, [
        l1("2026-07-06 22:00:00", "KX-SPORT-A", True, 4000, 5000000, 4200, 1000000),
        l1("2026-07-06 22:45:00", "KX-SPORT-B", True, 3000, 2000000, 3300, 800000),
    ], "2026-07-06", "parquet", "FORMAT parquet")
    _copy("orderbooks_l1", L_DDL, [
        l1("2026-07-07 00:10:00", "KX-SPORT-A", False, 4100, 4000000, 4300, 900000),
        l1("2026-07-07 00:10:00", "KX-SPORT-A", False, 4150, 3000000, 4350, 700000),  # SAME µs (tiebreak)
        l1("2026-07-07 00:25:00", "KX-OTHER-1", True, 5000, 100, 5100, 100),  # other event
    ], "2026-07-07", "parquet", "FORMAT parquet")


def _index_row(**over):
    row = {
        "unit": "event", "unit_key": "KX-SPORT-EV1", "category": "Sports",
        "subcategory": "NBA", "group": "game", "event_ticker": "KX-SPORT-EV1",
        "series_ticker": "KXNBA", "markets": ["KX-SPORT-A", "KX-SPORT-B"],
        "win_start_us": _ts("2026-07-06 20:00:00"),
        "win_end_us": _ts("2026-07-07 03:00:00"),
        "window_source": "scheduled_close", "crossed_day_boundary": True,
        "status": "sealed",
    }
    row.update(over)
    return row


NOW = _ts("2026-07-09 00:00:00")


def _read_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def test_pack_reassembles_cross_midnight(tmp_path):
    wh_root = str(tmp_path / "wh")
    _make_warehouse(wh_root)
    m = ep.build_pack(_index_row(), wh_root, str(tmp_path / "out"), NOW)
    assert m["status"] == "packed"
    # both days present, other-event market excluded -> 4 trades, 4 L1 (incl. a
    # same-µs pair on KX-SPORT-A).
    assert m["row_counts"] == {"trades": 4, "orderbooks_l1": 4}
    tr = _read_csv(str(tmp_path / "out" / "data" / "unit=KX-SPORT-EV1" / "trades.csv"))
    assert {r["market_ticker"] for r in tr} == {"KX-SPORT-A", "KX-SPORT-B"}  # no KX-OTHER-1
    days = {r["ts_utc"][:0] or _dt.datetime.utcfromtimestamp(int(r["ts_utc"]) / 1e6).strftime("%Y-%m-%d") for r in tr}
    assert days == {"2026-07-06", "2026-07-07"}  # reassembled across the boundary


def test_money_integrity_e4_byte_exact(tmp_path):
    wh_root = str(tmp_path / "wh")
    _make_warehouse(wh_root)
    ep.build_pack(_index_row(), wh_root, str(tmp_path / "out"), NOW)
    tr = _read_csv(str(tmp_path / "out" / "data" / "unit=KX-SPORT-EV1" / "trades.csv"))
    t1 = next(r for r in tr if r["trade_id"] == "t1")
    # sub-penny price and fractional count survive EXACTLY as E4 integers.
    assert t1["yes_price_e4"] == "90"      # $0.0090 — NOT "0.01", NOT "0.009"
    assert t1["no_price_e4"] == "9910"
    assert t1["count_e4"] == "12345"       # 1.2345 contracts — integer, not float
    # no column is a floated dollar value: every money cell is a bare integer.
    for r in tr:
        for col in ("yes_price_e4", "no_price_e4", "count_e4"):
            assert "." not in r[col] and r[col].lstrip("-").isdigit()


def test_rebuild_is_idempotent(tmp_path):
    wh_root = str(tmp_path / "wh")
    _make_warehouse(wh_root)
    out = str(tmp_path / "out")
    m1 = ep.build_pack(_index_row(), wh_root, out, NOW)
    m2 = ep.build_pack(_index_row(), wh_root, out, NOW)
    assert m1["files"] == m2["files"]  # md5 of every data file is stable


def test_af5_refuses_when_window_would_clip(tmp_path):
    wh_root = str(tmp_path / "wh")
    _make_warehouse(wh_root)
    # win_end BEFORE the 01:00 trade -> stored window would clip it.
    row = _index_row(win_end_us=_ts("2026-07-07 00:45:00"))
    refused = ep.build_pack(row, wh_root, str(tmp_path / "o1"), NOW)
    assert refused["status"] == "refused" and "stale index" in refused["reason"]
    # --refresh re-infers the window and includes the late trade.
    refreshed = ep.build_pack(row, wh_root, str(tmp_path / "o2"), NOW, refresh=True)
    assert refreshed["status"] == "packed" and refreshed["reinferred_window"] is True
    assert refreshed["row_counts"]["trades"] == 4


def test_l1_same_us_deterministic_order(tmp_path):
    # audit Defect-2: same-µs L1 rows must sort by the full-column tiebreak so the
    # manifest md5 (idempotency proof) is reproducible.
    wh_root = str(tmp_path / "wh")
    _make_warehouse(wh_root)
    ep.build_pack(_index_row(), wh_root, str(tmp_path / "out"), NOW)
    l1 = _read_csv(str(tmp_path / "out" / "data" / "unit=KX-SPORT-EV1" / "orderbooks_l1.csv"))
    same = [r for r in l1 if r["market_ticker"] == "KX-SPORT-A"
            and r["ts_utc"] == str(_ts("2026-07-07 00:10:00"))]
    assert len(same) == 2
    assert [r["yes_bid_e4"] for r in same] == ["4100", "4150"]  # ascending, deterministic


def test_af5_refuses_at_exact_win_end(tmp_path):
    # audit Defect-1: extract is exclusive (ts_utc < win_end); a tick AT win_end
    # would be clipped, so the guard must refuse it (not silently pack).
    wh_root = str(tmp_path / "wh")
    _make_warehouse(wh_root)
    row = _index_row(win_end_us=_ts("2026-07-07 01:00:00"))  # == last trade t4's µs
    m = ep.build_pack(row, wh_root, str(tmp_path / "out"), NOW)
    assert m["status"] == "refused"


def test_non_sealed_unit_skipped(tmp_path):
    wh_root = str(tmp_path / "wh")
    _make_warehouse(wh_root)
    m = ep.build_pack(_index_row(status="active"), wh_root, str(tmp_path / "out"), NOW)
    assert m["status"] == "skipped"


def test_update_index_win_end_preserves_other_columns(tmp_path):
    # Defect-3: --refresh must write the re-inferred win_end back to the index so
    # load(event=) and the pack agree; the rewrite must preserve markets etc.
    import duckdb
    idx = str(tmp_path / "index.parquet")
    con = duckdb.connect()
    con.execute("CREATE TABLE idx (unit_key VARCHAR, markets VARCHAR[], "
                "win_end_us BIGINT, category VARCHAR)")
    con.executemany("INSERT INTO idx VALUES (?,?,?,?)",
                    [("EV1", ["A", "B"], 1000, "Sports"), ("EV2", ["C"], 2000, "Crypto")])
    con.execute("COPY idx TO '%s' (FORMAT parquet)" % idx)
    ep.update_index_win_end(idx, {"EV1": 5000})
    got = {r[0]: (list(r[1]), r[2]) for r in duckdb.sql(
        "SELECT unit_key, markets, win_end_us FROM read_parquet('%s')" % idx).fetchall()}
    assert got["EV1"] == (["A", "B"], 5000)   # updated, markets preserved
    assert got["EV2"] == (["C"], 2000)        # untouched


def test_row_counts_match_direct_sql(tmp_path):
    import duckdb
    wh_root = str(tmp_path / "wh")
    _make_warehouse(wh_root)
    m = ep.build_pack(_index_row(), wh_root, str(tmp_path / "out"), NOW)
    # independent count straight off the archive files, same filter.
    g = os.path.join(wh_root, "facts", "trades", "*", "*", "date=*", "*.csv.gz")
    direct = duckdb.sql(
        "SELECT count(*) FROM read_csv('%s', header=true, union_by_name=true, "
        "types={'market_ticker':'VARCHAR','taker_side':'VARCHAR'}) "
        "WHERE market_ticker IN ('KX-SPORT-A','KX-SPORT-B') "
        "AND ts_utc >= %d AND ts_utc < %d" % (g, _ts("2026-07-06 20:00:00"), _ts("2026-07-07 03:00:00"))
    ).fetchone()[0]
    assert m["row_counts"]["trades"] == direct == 4
