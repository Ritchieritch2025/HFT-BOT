"""W-E0: pure split-detection core of tools/event_measure_split.py.

Deterministic, no warehouse — feeds the fixture ticks through the same
per-(event, UTC-day) aggregation the SQL path uses, then asserts cross-midnight
detection, calendar-span counting, and exact day-boundary math.
"""
import calendar
import csv
import datetime as _dt
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import event_measure_split as em  # noqa: E402


def _ts(s):
    return int(calendar.timegm(_dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timetuple()) * 1_000_000)


def _make_trades_archive(root, rows_by_date):
    """AF-4: a minimal Sports trades archive (csv.gz) so collect()'s real SQL
    path can be exercised end-to-end."""
    import duckdb
    ddl = ('ts_utc BIGINT, market_ticker VARCHAR, series_ticker VARCHAR, '
           'event_ticker VARCHAR, category VARCHAR, subcategory VARCHAR, '
           '"group" VARCHAR, trade_id VARCHAR, yes_price_e4 INTEGER, '
           'no_price_e4 INTEGER, count_e4 BIGINT, taker_side VARCHAR')
    con = duckdb.connect()
    for date, rows in rows_by_date.items():
        d = os.path.join(root, "facts", "trades", "category=Sports",
                         "subcategory=_none", "date=%s" % date)
        os.makedirs(d, exist_ok=True)
        con.execute("CREATE OR REPLACE TABLE x (%s)" % ddl)
        con.executemany("INSERT INTO x VALUES (%s)" % ",".join("?" * 12), rows)
        con.execute("COPY x TO '%s' (FORMAT csv, HEADER, COMPRESSION gzip)"
                    % os.path.join(d, "part.csv.gz"))

FX = os.path.join(os.path.dirname(__file__), "fixtures", "event_split_cases.csv")


def _spans():
    ticks = []
    with open(FX) as f:
        for r in csv.DictReader(f):
            ticks.append((r["event_ticker"], int(r["ts_utc"])))
    # Mimic the DuckDB "GROUP BY event, ts_utc//US_PER_DAY" aggregation.
    agg = defaultdict(lambda: [None, None, 0])
    for e, ts in ticks:
        d = em.day_index(ts)
        a = agg[(e, d)]
        a[0] = ts if a[0] is None else min(a[0], ts)
        a[1] = ts if a[1] is None else max(a[1], ts)
        a[2] += 1
    return em.event_spans([(e, d, mn, mx, c) for (e, d), (mn, mx, c) in agg.items()])


def test_same_day_not_crossed():
    s = _spans()["KX-SAMEDAY"]
    assert s["crossed_day_boundary"] is False
    assert s["calendar_span_days"] == 1
    assert s["n_days_present"] == 1


def test_cross_midnight_flagged():
    s = _spans()["KX-CROSS"]
    assert s["crossed_day_boundary"] is True
    assert s["calendar_span_days"] == 2
    assert s["n_days_present"] == 2
    assert sorted(s["day_counts"].values()) == [1, 1]  # one tick each side of midnight


def test_three_day_span():
    s = _spans()["KX-THREEDAY"]
    assert s["crossed_day_boundary"] is True
    assert s["calendar_span_days"] == 3
    assert s["n_days_present"] == 3
    assert s["total_ticks"] == 3


def test_gap_day_span_exceeds_days_present():
    # data on day D and D+2 only (nothing on D+1): calendar span counts the gap.
    s = _spans()["KX-GAPDAY"]
    assert s["crossed_day_boundary"] is True
    assert s["n_days_present"] == 2
    assert s["calendar_span_days"] == 3
    assert s["calendar_span_days"] > s["n_days_present"]


def test_af2_weighting_reorders_ranking():
    # AF-2: a row-heavy/volume-light event and a row-light/volume-heavy event
    # must swap order between the row-count view and the contract/notional views,
    # proving the weighting is real (row count does not size $ impact).
    P = em.US_PER_DAY
    base = 20640 * P + 12 * 3_600_000_000
    ped = [
        # event A: 3 trade rows, tiny (count_e4=10000 => 1 contract each)
        ("A", 20640, base, base, 1, 10000, 5000 * 10000),
        ("A", 20640, base + 1, base + 1, 1, 10000, 5000 * 10000),
        ("A", 20640, base + 2, base + 2, 1, 10000, 5000 * 10000),
        # event B: 1 trade row, huge (count_e4=30_000_000 => 3000 contracts)
        ("B", 20640, base, base, 1, 30_000_000, 5000 * 30_000_000),
    ]
    spans = em.event_spans(ped)
    by_rows = sorted(spans, key=lambda e: spans[e]["total_ticks"], reverse=True)
    by_contracts = sorted(spans, key=lambda e: spans[e]["total_contracts_e4"], reverse=True)
    by_notional = sorted(spans, key=lambda e: spans[e]["total_notional_e8"], reverse=True)
    assert by_rows[0] == "A"          # more trade rows
    assert by_contracts[0] == "B"     # more contract volume
    assert by_notional[0] == "B"      # more notional
    assert by_rows != by_contracts    # the weighting REORDERS the ranking
    # exact weighted sums (integer, no float — D5)
    assert spans["A"]["total_contracts_e4"] == 30000
    assert spans["B"]["total_contracts_e4"] == 30_000_000
    assert spans["B"]["total_notional_e8"] == 5000 * 30_000_000


def test_event_spans_row_only_backcompat():
    # 5-tuples (no weights) still work; weight totals default to 0 (AF-2 backcompat).
    s = em.event_spans([("Z", 20640, 100, 200, 5)])["Z"]
    assert s["total_ticks"] == 5
    assert s["total_contracts_e4"] == 0 and s["total_notional_e8"] == 0


def test_collect_sql_path_matches_handcomputed(tmp_path):
    # AF-4: the 42.8%/77.6%-style numbers come from collect()'s DuckDB aggregate
    # (ts_utc // US_PER_DAY, sum(count_e4), HUGEINT notional), which was never
    # tested — only the pure event_spans was. Drive the real SQL end-to-end.
    root = str(tmp_path / "wh")

    def r(ts, mk, ev, cnt, yp):
        return (_ts(ts), mk, "S", ev, "Sports", "_none", "g", mk + ts, yp,
                10000 - yp, cnt, "yes")
    _make_trades_archive(root, {
        "2026-07-06": [r("2026-07-06 23:00:00", "G1-A", "G1", 10000, 5000)],
        "2026-07-07": [r("2026-07-07 00:30:00", "G1-B", "G1", 20000, 6000),
                       r("2026-07-07 12:00:00", "G2-A", "G2", 30000, 4000)],
    })
    spans, meta = em.collect("Sports", "2026-07-06", "2026-07-07", warehouse=root)
    g1, g2 = spans["G1"], spans["G2"]
    # cross-midnight detection from the SQL day-bucketing (// US_PER_DAY)
    assert g1["crossed_day_boundary"] is True and g1["total_ticks"] == 2
    assert g2["crossed_day_boundary"] is False and g2["total_ticks"] == 1
    assert sorted(g1["day_counts"].keys()) == [em.day_index(_ts("2026-07-06 23:00:00")),
                                               em.day_index(_ts("2026-07-07 00:30:00"))]
    # weighted sums straight off the SQL path (integer, exact)
    assert g1["total_contracts_e4"] == 30000                    # 10000 + 20000
    assert g1["total_notional_e8"] == 5000 * 10000 + 6000 * 20000
    assert meta["G1"][2] == "Sports"                            # any_value(category)


def test_day_index_boundary_exact():
    P = em.US_PER_DAY
    # last microsecond of a day and first of the next map to adjacent indices.
    assert em.day_index(20640 * P + P - 1) == 20640
    assert em.day_index(20641 * P) == 20641
