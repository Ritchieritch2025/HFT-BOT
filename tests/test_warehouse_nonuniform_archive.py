"""AF-3 red-first: warehouse.load() archive-vs-staging exclusion must be scoped
PER category, not a GLOBAL max-archive-date.

Scenario: Sports archived through day D (2026-07-07), Crypto only through D-1
(2026-07-06); staging still holds Crypto's day-D rows (not yet archived) and a
Sports day-D row (already archived — must be deduped out).

Bug (global max = D from Sports): a Crypto query spanning D excludes ALL staging
>= D, silently DROPPING Crypto's day-D rows (undercount on every backtest tape).
Fix (per-category max): Crypto's max is D-1, so its day-D staging rows are kept;
Sports' day-D staging row is still excluded (dedup preserved).
"""
import calendar
import datetime as _dt
import os

import duckdb
import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import warehouse as wh  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_wh_conn():
    # load() attaches staging under a fixed global alias 'stg' on a module-global
    # connection; reset it so each test's synthetic warehouse attaches cleanly.
    if wh._CON is not None:
        try:
            wh._CON.close()
        except Exception:
            pass
    wh._CON = None
    wh._ATTACHED.clear()
    yield

_DDL = ('ts_utc BIGINT, market_ticker VARCHAR, series_ticker VARCHAR, '
        'event_ticker VARCHAR, category VARCHAR, subcategory VARCHAR, '
        '"group" VARCHAR, trade_id VARCHAR, yes_price_e4 INTEGER, '
        'no_price_e4 INTEGER, count_e4 BIGINT, taker_side VARCHAR')


def _ts(s):
    return int(calendar.timegm(_dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timetuple()) * 1_000_000)


def _row(ts, mk, cat):
    return (_ts(ts), mk, "S", "EV", cat, "_none", "g", mk + ts, 5000, 5000, 10000, "yes")


def _build(root):
    con = duckdb.connect()

    def archive(cat, date, rows):
        d = os.path.join(root, "facts", "trades", "category=%s" % cat,
                         "subcategory=_none", "date=%s" % date)
        os.makedirs(d, exist_ok=True)
        con.execute("CREATE OR REPLACE TABLE x (%s)" % _DDL)
        con.executemany("INSERT INTO x VALUES (%s)" % ",".join("?" * 12), rows)
        con.execute("COPY x TO '%s' (FORMAT csv, HEADER, COMPRESSION gzip)"
                    % os.path.join(d, "part.csv.gz"))

    archive("Sports", "2026-07-07", [_row("2026-07-07 10:00:00", "S1", "Sports"),
                                     _row("2026-07-07 10:05:00", "S2", "Sports")])
    archive("Crypto", "2026-07-06", [_row("2026-07-06 12:00:00", "C1", "Crypto"),
                                     _row("2026-07-06 13:00:00", "C2", "Crypto")])
    # staging: Crypto day-D (NOT archived) + Sports day-D (already archived).
    scon = duckdb.connect(os.path.join(root, "staging.duckdb"))
    scon.execute("CREATE TABLE trades (%s)" % _DDL)
    scon.executemany("INSERT INTO trades VALUES (%s)" % ",".join("?" * 12), [
        _row("2026-07-07 12:00:00", "C1", "Crypto"),
        _row("2026-07-07 13:00:00", "C2", "Crypto"),
        _row("2026-07-07 11:00:00", "S1", "Sports"),  # archived day -> must dedup out
    ])
    scon.close()


def _count(root, category):
    rel = wh.load("trades", category=category, start="2026-07-06", end="2026-07-07",
                  warehouse=root)
    return rel.aggregate("count(*)").fetchone()[0]


def test_lagging_category_staging_not_dropped(tmp_path):
    root = str(tmp_path / "wh")
    _build(root)
    # Crypto archived only through 07-06 -> its 07-07 staging rows MUST survive.
    assert _count(root, "Crypto") == 4  # 2 archive (07-06) + 2 staging (07-07)


def test_archived_category_staging_deduped(tmp_path):
    root = str(tmp_path / "wh2")
    _build(root)
    # Sports archived through 07-07 -> its 07-07 staging row is excluded (no double count).
    assert _count(root, "Sports") == 2  # archive only; staging 07-07 deduped
