"""W-E3: warehouse.load(event=...) three-axis selector.

Resolves an event's window + market set from the W-E1 index and returns exactly
that episode's rows across day partitions — no calendar dates passed by the
caller (PLAN §3.5). Also checks day-mode is unchanged (regression) and unknown
events raise.
"""
import calendar
import datetime as _dt
import os
import sys

import duckdb
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import warehouse as wh  # noqa: E402
import warehouse_common as wc  # noqa: E402

_TDDL = ('ts_utc BIGINT, market_ticker VARCHAR, series_ticker VARCHAR, '
         'event_ticker VARCHAR, category VARCHAR, subcategory VARCHAR, '
         '"group" VARCHAR, trade_id VARCHAR, yes_price_e4 INTEGER, '
         'no_price_e4 INTEGER, count_e4 BIGINT, taker_side VARCHAR')


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


def _ts(s):
    return int(calendar.timegm(_dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timetuple()) * 1_000_000)


def _row(ts, mk, ev):
    return (_ts(ts), mk, "KXNBA", ev, "Sports", "_none", "g", mk + ts, 5000, 5000, 10000, "yes")


def _build(root):
    con = duckdb.connect()

    def archive(date, rows):
        d = os.path.join(root, "facts", "trades", "category=Sports",
                         "subcategory=%s" % wc.sanitize("_none"), "date=%s" % date)
        os.makedirs(d, exist_ok=True)
        con.execute("CREATE OR REPLACE TABLE x (%s)" % _TDDL)
        con.executemany("INSERT INTO x VALUES (%s)" % ",".join("?" * 12), rows)
        con.execute("COPY x TO '%s' (FORMAT csv, HEADER, COMPRESSION gzip)"
                    % os.path.join(d, "part.csv.gz"))

    archive("2026-07-06", [_row("2026-07-06 23:00:00", "KX-SPORT-A", "KX-SPORT-EV1")])
    archive("2026-07-07", [_row("2026-07-07 00:30:00", "KX-SPORT-B", "KX-SPORT-EV1"),
                           _row("2026-07-07 01:00:00", "KX-OTHER-1", "KX-OTHER")])
    idx = os.path.join(root, "index.parquet")
    con.execute("CREATE OR REPLACE TABLE idx (unit_key VARCHAR, category VARCHAR, "
                "markets VARCHAR[], win_start_us BIGINT, win_end_us BIGINT)")
    con.executemany("INSERT INTO idx VALUES (?,?,?,?,?)", [
        ("KX-SPORT-EV1", "Sports", ["KX-SPORT-A", "KX-SPORT-B"],
         _ts("2026-07-06 20:00:00"), _ts("2026-07-07 03:00:00"))])
    con.execute("COPY idx TO '%s' (FORMAT parquet)" % idx)
    return idx


def test_event_selector_reassembles_only_that_event(tmp_path):
    root = str(tmp_path / "wh")
    idx = _build(root)
    rel = wh.load("trades", event="KX-SPORT-EV1", warehouse=root, index_path=idx)
    rows = rel.fetchall()
    cols = [c for c in rel.columns]
    mkts = {r[cols.index("market_ticker")] for r in rows}
    assert mkts == {"KX-SPORT-A", "KX-SPORT-B"}   # foreign KX-OTHER-1 excluded
    assert len(rows) == 2                          # both sides of midnight, no leakage


def test_day_mode_unchanged(tmp_path):
    # regression: without event=, the category/day query still returns everything.
    root = str(tmp_path / "wh")
    _build(root)
    n = wh.load("trades", category="Sports", start="2026-07-06", end="2026-07-07",
                warehouse=root).aggregate("count(*)").fetchone()[0]
    assert n == 3   # includes the foreign KX-OTHER-1 row


def test_unknown_event_raises(tmp_path):
    root = str(tmp_path / "wh")
    idx = _build(root)
    with pytest.raises(KeyError):
        wh.load("trades", event="NOPE", warehouse=root, index_path=idx)
