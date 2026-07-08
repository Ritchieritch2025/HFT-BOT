"""W-C2: capture-gap record builder + live detector.

Proves the detector emits the EXACT [start_us,end_us] for a seeded market-wide
silence and nothing for a dense stream, that the durable record is idempotent
and fail-closed, and — the load-bearing case — that a record BUILT FROM RAW then
drives event_validate to `degraded` for an overlapping event (AF-1 / D2: a hole
can never again pass as a silent green `pass`).
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
    times = [i * US for i in range(60)]  # 1s spacing, 60s
    assert cg.find_gaps(times, 60 * US) == []


def test_find_gaps_seeded_hole_exact():
    # dense to t=10s, then a 600s silence, then resume.
    times = [i * US for i in range(11)] + [610 * US, 611 * US]
    gaps = cg.find_gaps(times, 60 * US)
    assert gaps == [(10 * US, 610 * US)]  # last-before, first-after — exact


# ---- scan raw + write record -------------------------------------------------

def test_scan_date_emits_exact_window(tmp_path):
    raw = str(tmp_path / "raw")
    base = _ts("2026-07-07 12:00:00")
    # dense 0..5s, 900s hole, resume — one market-wide gap.
    times = [base + i * US for i in range(6)] + [base + 906 * US, base + 907 * US]
    _write_raw(os.path.join(raw, "date=2026-07-07", "firehose_12.ndjson"), times)
    gaps, stats = cg.scan_date("2026-07-07", raw, 60 * US)
    assert gaps == [(base + 5 * US, base + 906 * US)]
    assert stats["records"] == 8 and stats["unparsed"] == 0


def test_scan_date_dense_no_gap(tmp_path):
    raw = str(tmp_path / "raw")
    base = _ts("2026-07-07 12:00:00")
    times = [base + i * US for i in range(300)]  # 5 min, 1s spacing
    _write_raw(os.path.join(raw, "date=2026-07-07", "firehose_12.ndjson"), times)
    gaps, _ = cg.scan_date("2026-07-07", raw, 60 * US)
    assert gaps == []


def test_scan_spans_file_boundary(tmp_path):
    # a silence straddling two rotation segments is still caught (last-ts carried
    # across files, ordered by first record not filename).
    raw = str(tmp_path / "raw")
    base = _ts("2026-07-07 12:00:00")
    d = os.path.join(raw, "date=2026-07-07")
    _write_raw(os.path.join(d, "firehose_12.ndjson"), [base, base + US])
    _write_raw(os.path.join(d, "firehose_12.ndjson.1"), [base + 800 * US, base + 801 * US])
    gaps, _ = cg.scan_date("2026-07-07", raw, 60 * US)
    assert gaps == [(base + US, base + 800 * US)]


def test_scan_handles_time_overlapping_files(tmp_path):
    # Regression: a within-hour respawn/rotation can leave two segments whose
    # time ranges OVERLAP. Global sort must NOT invent a false backward gap.
    # (Real 2026-07-08 hour-00 case: file0 00:10-00:32, file1 00:17-00:33.)
    raw = str(tmp_path / "raw")
    base = _ts("2026-07-08 00:10:00")
    d = os.path.join(raw, "date=2026-07-08")
    # file A alone LOOKS holed (00:10:00-00:12:00 then 00:18:00-00:20:00, a 6-min
    # hole); file B fills it (00:11:30-00:18:30). TOGETHER they are continuous.
    a = [base + i * US for i in range(121)] + [base + (480 + i) * US for i in range(121)]
    b = [base + (90 + i) * US for i in range(421)]
    _write_raw(os.path.join(d, "firehose_00.ndjson"), a)
    _write_raw(os.path.join(d, "firehose_00.ndjson.1"), b)
    gaps, _ = cg.scan_date("2026-07-08", raw, 60 * US)
    # NO gap: sequential-per-file streaming would have invented a false backward
    # jump; global sort sees a continuous 00:10:00 -> 00:20:00 timeline.
    assert gaps == []


def test_write_record_idempotent_and_merges(tmp_path):
    rec = str(tmp_path / "capture_gaps.csv")
    g07 = [(_ts("2026-07-07 01:00:00"), _ts("2026-07-07 01:10:00"))]
    cg.write_record(rec, "2026-07-07", g07)
    cg.write_record(rec, "2026-07-07", g07)  # again — no duplication
    rows1 = list(ev.load_gaps(rec)[0])
    assert rows1 == [tuple(g07[0])]
    # a different day is preserved when 07-07 is rebuilt.
    g08 = [(_ts("2026-07-08 05:00:00"), _ts("2026-07-08 05:20:00"))]
    cg.write_record(rec, "2026-07-08", g08)
    cg.write_record(rec, "2026-07-07", [])  # 07-07 now clean — drops its old row
    rows2 = set(ev.load_gaps(rec)[0])
    assert rows2 == {tuple(g08[0])}  # 07-07 row gone, 07-08 kept


# ---- live detector / alert (fail-closed) -------------------------------------

def test_live_ok_when_fresh(tmp_path):
    raw = str(tmp_path / "raw")
    now = _ts("2026-07-07 12:00:30")
    _write_raw(os.path.join(raw, "date=2026-07-07", "firehose_12.ndjson"),
               [_ts("2026-07-07 12:00:00"), _ts("2026-07-07 12:00:20")])
    a = cg.live_check(raw, str(tmp_path / "alert.json"), 45, now_us=now)
    assert a["status"] == "ok" and a["silent_secs"] == 10.0


def test_live_gap_when_silent(tmp_path):
    raw = str(tmp_path / "raw")
    now = _ts("2026-07-07 12:10:00")  # 600s after last record
    _write_raw(os.path.join(raw, "date=2026-07-07", "firehose_12.ndjson"),
               [_ts("2026-07-07 12:00:00")])
    a = cg.live_check(raw, str(tmp_path / "alert.json"), 45, now_us=now)
    assert a["status"] == "gap" and a["silent_secs"] == 600.0


def test_live_no_raw_is_failclosed_gap(tmp_path):
    a = cg.live_check(str(tmp_path / "empty"), str(tmp_path / "alert.json"), 45,
                      now_us=_ts("2026-07-07 12:00:00"))
    assert a["status"] == "gap" and a["last_record_us"] is None


# ---- integration: a RAW-BUILT record drives event_validate -> degraded -------

def test_record_from_raw_degrades_overlapping_event(tmp_path):
    # Build a real capture-gap record from raw, then validate a pack whose window
    # spans the hole. Mirrors test_event_validate's AF-1 case, but the gap is now
    # DISCOVERED from raw by capture_gaps, not hand-fed.
    raw = str(tmp_path / "raw")
    s, e = _ts("2026-07-07 00:00:00"), _ts("2026-07-07 00:15:00")
    _write_raw(os.path.join(raw, "date=2026-07-07", "firehose_00.ndjson"), [s, e])
    rec = str(tmp_path / "capture_gaps.csv")
    gaps, _ = cg.scan_date("2026-07-07", raw, 60 * US)
    cg.write_record(rec, "2026-07-07", gaps)
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
