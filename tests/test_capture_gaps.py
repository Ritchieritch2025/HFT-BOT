"""W-C2: capture-gap record builder + live detector.

Proves the detector emits the EXACT [start_us,end_us] for a seeded market-wide
silence and nothing for a dense stream; that it catches holes at the day EDGES
and across MIDNIGHT (the boundary blind spot); that it FAIL-CLOSES when raw is
unreadable and PRESERVES the record when raw was pruned (never wipes a real
recorded gap); and — the load-bearing case — that a record BUILT FROM RAW then
drives event_validate to `degraded` for an overlapping event (AF-1 / D2).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.dirname(__file__))  # sibling test module reuse
import capture_gaps as cg  # noqa: E402
import event_validate as ev  # noqa: E402
from test_event_pack import _make_warehouse, _index_row, _ts, NOW  # noqa: E402
import event_pack as ep  # noqa: E402

US = 1_000_000
DS = _ts("2026-07-07 00:00:00")  # a UTC day start
DAY_END = _ts("2026-07-08 00:00:00")


def _write_raw(path, times_us, ticker="MKT-A"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for t in times_us:
            f.write(json.dumps({
                "recv_mono_ns": 1,
                "recv_wall_ns": int(t) * 1000,
                "source": "Kalshi", "channel": "trade", "source_ticker": ticker,
            }) + "\n")


# ---- pure detector -----------------------------------------------------------

def test_find_gaps_dense_none():
    assert cg.find_gaps([i * US for i in range(60)], 60 * US) == []


def test_find_gaps_seeded_hole_exact():
    times = [i * US for i in range(11)] + [610 * US, 611 * US]
    assert cg.find_gaps(times, 60 * US) == [(10 * US, 610 * US)]


def test_extract_rejects_implausible_and_corrupt(line=None):
    good = '{"recv_mono_ns":1,"recv_wall_ns":1783470000000000000,"channel":"trade"}'
    assert cg._extract_recv_us(good) == 1783470000000000  # ns -> us
    # a corrupt digit-run that would overflow int64 us -> dropped, not crash.
    assert cg._extract_recv_us('{"recv_wall_ns":178347000000000000000000000,"x":1}') is None
    assert cg._extract_recv_us('{"recv_wall_ns":0}') is None          # implausibly old
    assert cg._extract_recv_us('{"no_field":1}') is None


# ---- scan raw: interior gaps (fixtures span day_start..now, so no edge gaps) --

def test_scan_date_interior_gap_exact(tmp_path):
    raw = str(tmp_path / "raw")
    # dense from day_start for 6s, 900s hole, resume 2 recs; now just after last.
    times = [DS + i * US for i in range(6)] + [DS + 906 * US, DS + 907 * US]
    _write_raw(os.path.join(raw, "date=2026-07-07", "firehose_00.ndjson"), times)
    gaps, stats = cg.scan_date("2026-07-07", raw, 60 * US, now_us=DS + 908 * US)
    assert gaps == [(DS + 5 * US, DS + 906 * US)]
    assert stats["records"] == 8 and stats["unparsed"] == 0


def test_scan_date_dense_no_gap(tmp_path):
    raw = str(tmp_path / "raw")
    times = [DS + i * US for i in range(300)]
    _write_raw(os.path.join(raw, "date=2026-07-07", "firehose_00.ndjson"), times)
    gaps, _ = cg.scan_date("2026-07-07", raw, 60 * US, now_us=DS + 299 * US)
    assert gaps == []


def test_scan_spans_file_boundary(tmp_path):
    raw = str(tmp_path / "raw")
    d = os.path.join(raw, "date=2026-07-07")
    _write_raw(os.path.join(d, "firehose_00.ndjson"), [DS, DS + US])
    _write_raw(os.path.join(d, "firehose_00.ndjson.1"), [DS + 800 * US, DS + 801 * US])
    gaps, _ = cg.scan_date("2026-07-07", raw, 60 * US, now_us=DS + 802 * US)
    assert gaps == [(DS + US, DS + 800 * US)]


def test_scan_handles_time_overlapping_files(tmp_path):
    # Regression: within-hour respawn/rotation can leave two segments whose time
    # ranges OVERLAP; global sort must NOT invent a false backward gap.
    raw = str(tmp_path / "raw")
    d = os.path.join(raw, "date=2026-07-07")
    # file A alone looks holed (00:00:00-00:02:00 then 00:08:00-00:10:00); file B
    # fills it (00:01:30-00:08:30). TOGETHER continuous.
    a = [DS + i * US for i in range(121)] + [DS + (480 + i) * US for i in range(121)]
    b = [DS + (90 + i) * US for i in range(421)]
    _write_raw(os.path.join(d, "firehose_00.ndjson"), a)
    _write_raw(os.path.join(d, "firehose_00.ndjson.1"), b)
    gaps, _ = cg.scan_date("2026-07-07", raw, 60 * US, now_us=DS + 600 * US)
    assert gaps == []


# ---- B1: day-edge + midnight-boundary holes (were silently missed) -----------

def test_leading_edge_hole(tmp_path):
    # feed down from midnight until 08:00 -> a real leading hole.
    raw = str(tmp_path / "raw")
    start = DS + 8 * 3600 * US
    _write_raw(os.path.join(raw, "date=2026-07-07", "firehose_08.ndjson"),
               [start + i * US for i in range(10)])
    gaps, _ = cg.scan_date("2026-07-07", raw, 60 * US, now_us=start + 9 * US)
    assert (DS, start) in gaps


def test_trailing_edge_hole_on_elapsed_day(tmp_path):
    # feed dies early and never returns that day; the day is fully elapsed.
    raw = str(tmp_path / "raw")
    last = DS + 5 * US
    _write_raw(os.path.join(raw, "date=2026-07-07", "firehose_00.ndjson"),
               [DS + i * US for i in range(6)])
    gaps, _ = cg.scan_date("2026-07-07", raw, 60 * US, now_us=DAY_END + 3600 * US)
    assert gaps == [(last, DAY_END)]


def test_in_progress_day_not_flagged_trailing(tmp_path):
    # today, feed healthy up to `now` -> no spurious trailing gap.
    raw = str(tmp_path / "raw")
    times = [DS + i * US for i in range(600)]
    _write_raw(os.path.join(raw, "date=2026-07-07", "firehose_00.ndjson"), times)
    gaps, _ = cg.scan_date("2026-07-07", raw, 60 * US, now_us=DS + 600 * US)
    assert gaps == []


def test_midnight_crossing_hole_recorded_both_sides(tmp_path):
    # feed dies 2026-07-07 23:58, resumes 2026-07-08 00:03 (5-min hole across
    # midnight). Recorded as a trailing gap of 07-07 + a leading gap of 07-08.
    raw = str(tmp_path / "raw")
    last7 = _ts("2026-07-07 23:58:00")
    first8 = _ts("2026-07-08 00:03:00")
    _write_raw(os.path.join(raw, "date=2026-07-07", "firehose_23.ndjson"),
               [DS, last7])
    _write_raw(os.path.join(raw, "date=2026-07-08", "firehose_00.ndjson"),
               [first8, first8 + US])
    g7, _ = cg.scan_date("2026-07-07", raw, 60 * US, now_us=DAY_END + US)
    g8, _ = cg.scan_date("2026-07-08", raw, 60 * US, now_us=first8 + 2 * US)
    assert (last7, DAY_END) in g7           # trailing half
    assert (DAY_END, first8) in g8          # leading half — no midnight blind spot


# ---- B2/B3: fail-closed on missing/unreadable data; never wipe the record ----

def test_unreadable_raw_is_failclosed_fullday_gap(tmp_path):
    # raw present but the timestamp field was renamed -> 0 parseable records.
    raw = str(tmp_path / "raw")
    p = os.path.join(raw, "date=2026-07-07", "firehose_00.ndjson")
    os.makedirs(os.path.dirname(p))
    with open(p, "w") as f:
        for i in range(100):
            f.write(json.dumps({"recv_wall_ts_ns": (DS + i) * 1000, "channel": "trade"}) + "\n")
    gaps, stats = cg.scan_date("2026-07-07", raw, 60 * US, now_us=DAY_END + US)
    assert stats["unreadable"] and stats["records"] == 0 and stats["unparsed"] == 100
    assert gaps == [(DS, DAY_END)]          # whole day is a hole, not silent-clean


def test_pruned_day_has_no_files(tmp_path):
    raw = str(tmp_path / "raw")  # no date dir at all
    gaps, stats = cg.scan_date("2026-07-07", raw, 60 * US, now_us=DAY_END + US)
    assert gaps == [] and stats["has_files"] is False and stats["records"] == 0


def test_rescan_after_prune_preserves_recorded_gap(tmp_path):
    # B3: a real gap recorded while raw existed must survive a later re-run once
    # the raw has aged out of the 3-day retention.
    rec = str(tmp_path / "capture_gaps.csv")
    real = (_ts("2026-07-07 05:24:00"), _ts("2026-07-07 05:54:00"))
    cg.write_record(rec, "2026-07-07", [real], replace_day=True)
    assert set(ev.load_gaps(rec)[0]) == {real}
    # raw is gone -> scan yields no files -> caller must NOT overwrite the day.
    cg.write_record(rec, "2026-07-07", [], replace_day=False)
    assert set(ev.load_gaps(rec)[0]) == {real}  # still there — not wiped


def test_write_record_replace_vs_preserve_and_merge(tmp_path):
    rec = str(tmp_path / "capture_gaps.csv")
    g07 = [(_ts("2026-07-07 01:00:00"), _ts("2026-07-07 01:10:00"))]
    cg.write_record(rec, "2026-07-07", g07, replace_day=True)
    cg.write_record(rec, "2026-07-07", g07, replace_day=True)  # idempotent
    assert list(ev.load_gaps(rec)[0]) == [tuple(g07[0])]
    g08 = [(_ts("2026-07-08 05:00:00"), _ts("2026-07-08 05:20:00"))]
    cg.write_record(rec, "2026-07-08", g08, replace_day=True)
    # a genuine clean re-scan of 07-07 (data present) drops its row; 07-08 kept.
    cg.write_record(rec, "2026-07-07", [], replace_day=True)
    assert set(ev.load_gaps(rec)[0]) == {tuple(g08[0])}


# ---- live detector / alert (fail-closed) -------------------------------------

def test_live_ok_when_fresh(tmp_path):
    raw = str(tmp_path / "raw")
    _write_raw(os.path.join(raw, "date=2026-07-07", "firehose_12.ndjson"),
               [_ts("2026-07-07 12:00:00"), _ts("2026-07-07 12:00:20")])
    a = cg.live_check(raw, str(tmp_path / "alert.json"), 45, now_us=_ts("2026-07-07 12:00:30"))
    assert a["status"] == "ok" and a["silent_secs"] == 10.0


def test_live_gap_when_silent(tmp_path):
    raw = str(tmp_path / "raw")
    _write_raw(os.path.join(raw, "date=2026-07-07", "firehose_12.ndjson"),
               [_ts("2026-07-07 12:00:00")])
    a = cg.live_check(raw, str(tmp_path / "alert.json"), 45, now_us=_ts("2026-07-07 12:10:00"))
    assert a["status"] == "gap" and a["silent_secs"] == 600.0


def test_live_no_raw_is_failclosed_gap(tmp_path):
    a = cg.live_check(str(tmp_path / "empty"), str(tmp_path / "alert.json"), 45,
                      now_us=_ts("2026-07-07 12:00:00"))
    assert a["status"] == "gap" and a["last_record_us"] is None


# ---- integration: a RAW-BUILT record drives event_validate -> degraded -------

def test_record_from_raw_degrades_overlapping_event(tmp_path):
    raw = str(tmp_path / "raw")
    s, e = _ts("2026-07-07 00:00:00"), _ts("2026-07-07 00:15:00")
    # dense day with an interior hole s->e (edges clean so only the hole is a gap)
    times = [s + i * US for i in range(1)] + [e + i * US for i in range(3600)]
    _write_raw(os.path.join(raw, "date=2026-07-07", "firehose_00.ndjson"), times)
    rec = str(tmp_path / "capture_gaps.csv")
    gaps, _ = cg.scan_date("2026-07-07", raw, 60 * US, now_us=e + 3600 * US)
    cg.write_record(rec, "2026-07-07", gaps, replace_day=True)
    loaded, available = ev.load_gaps(rec)
    assert available and (s, e) in loaded

    wh_root = str(tmp_path / "wh")
    _make_warehouse(wh_root)
    out = str(tmp_path / "out")
    m = ep.build_pack(_index_row(), wh_root, out, NOW)
    dd = os.path.join(out, "data", "unit=KX-SPORT-EV1")
    res = ev.validate_pack(m, dd, warehouse=wh_root, gaps=loaded)
    assert res["verdict"] == "degraded"
    assert res["checks"]["V-EP15"]["status"] == "degraded"
