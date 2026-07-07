"""W-E0: pure split-detection core of tools/event_measure_split.py.

Deterministic, no warehouse — feeds the fixture ticks through the same
per-(event, UTC-day) aggregation the SQL path uses, then asserts cross-midnight
detection, calendar-span counting, and exact day-boundary math.
"""
import csv
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import event_measure_split as em  # noqa: E402

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


def test_day_index_boundary_exact():
    P = em.US_PER_DAY
    # last microsecond of a day and first of the next map to adjacent indices.
    assert em.day_index(20640 * P + P - 1) == 20640
    assert em.day_index(20641 * P) == 20641
