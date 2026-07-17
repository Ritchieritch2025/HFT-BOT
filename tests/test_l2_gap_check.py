#!/usr/bin/env python3
"""Regression contract for the L2 continuity receipt."""
import json
import os
import sys

import pytest


TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import l2_gap_check as lgc  # noqa: E402


DATE = "2026-07-11"
WALL = 1_752_300_000_000_000_000


def _line(channel="orderbook_delta", ticker="KXMLB-26JUL12-BOS", sid=None,
          seq=None, epoch=None, marker=None, raw_sid="same", raw_seq="same",
          raw=None):
    outer = {"recv_mono_ns": 1, "recv_wall_ns": WALL, "source": "Kalshi",
             "channel": channel, "source_ticker": ticker}
    if seq is not None:
        outer["source_sequence"] = seq
    if sid is not None:
        outer["sid"] = sid
    if epoch is not None:
        outer["stream_epoch"] = epoch
    if marker is not None:
        outer["marker"] = marker
    if raw is None:
        raw_sid = sid if raw_sid == "same" else raw_sid
        raw_seq = seq if raw_seq == "same" else raw_seq
        raw = json.dumps({"type": channel, "sid": raw_sid, "seq": raw_seq,
                          "msg": {"market_ticker": ticker}},
                         separators=(",", ":"))
    outer["raw"] = raw
    return json.dumps(outer, separators=(",", ":"))


def _marker(kind, lost=None):
    outer = {"recv_mono_ns": 1, "recv_wall_ns": WALL, "source": "Kalshi",
             "channel": "", "source_ticker": "", "marker": kind}
    if lost is not None:
        outer["source_sequence"] = lost
    return json.dumps(outer, separators=(",", ":"))


def _write(day_dir, name, lines):
    os.makedirs(day_dir, exist_ok=True)
    with open(os.path.join(day_dir, name), "w") as handle:
        handle.write("\n".join(lines) + "\n")


@pytest.fixture
def raw_root(tmp_path):
    return str(tmp_path / "raw")


def test_seq_gaps_markers_restarts_and_reanchors(raw_root):
    day_dir = os.path.join(raw_root, "date=%s" % DATE)
    ticker = "KXMLB-26JUL12-BOS"
    _write(day_dir, "l2_13.ndjson", [
        _line("orderbook_snapshot", ticker, sid=7, seq=1),
        _line("orderbook_delta", ticker, sid=7, seq=2),
        _line("orderbook_delta", ticker, sid=7, seq=6),
        _marker("gap"),
        _marker("loss", lost=5),
        _line("orderbook_snapshot", ticker, sid=7, seq=7),
    ])
    _write(day_dir, "l2_13.ndjson.1", [
        _line("orderbook_delta", ticker, sid=7, seq=8),
        _line("orderbook_snapshot", ticker, sid=7, seq=1),
    ])
    _write(day_dir, "l2_14.ndjson", [
        _line("orderbook_snapshot", ticker, sid=7, seq=1),
    ])

    record = lgc.scan_date(DATE, raw_root)
    assert record["schema_version"] == "l2-gap-receipt-v2"
    assert record["files"] == ["l2_13.ndjson", "l2_13.ndjson.1",
                               "l2_14.ndjson"]
    assert record["lines"] == 9 and record["corrupt_rows"] == 0
    assert record["seq_gap_events"] == 1
    assert record["seq_missed_total"] == 3
    assert record["seq_regressions"] == 0
    assert record["stream_restarts"] == 1
    assert record["sids_total"] == 2
    assert record["sids_with_seq_gaps"] == 1
    assert record["recorder_markers"] == {"gap": 1, "loss": 1}
    assert record["markers_lost_frames"] == 5
    assert record["snapshot_re_anchors_total"] == 2
    assert record["seq_continuity_complete"] is True
    assert record["seq_counts_are_lower_bound"] is False


def test_decimal_splice_mismatch_cannot_pollute_continuity(raw_root):
    day_dir = os.path.join(raw_root, "date=%s" % DATE)
    _write(day_dir, "l2_00.ndjson", [
        _line(sid=1, seq=174675),
        _line(sid=1, seq=174676415217, raw_sid=1, raw_seq=415217),
        _line(sid=1, seq=174676),
    ])

    record = lgc.scan_date(DATE, raw_root)
    assert record["corrupt_rows"] == 1
    assert record["corrupt_sequence_rows"] == 1
    assert record["corrupt_row_reasons"] == {
        "sequence_identity_mismatch": 1,
    }
    assert record["seq_gap_events"] == 0
    assert record["seq_missed_total"] == 0
    assert record["seq_regressions"] == 0
    assert record["continuity_resets_due_to_corruption"] == 1
    assert record["seq_continuity_complete"] is False
    assert record["seq_counts_are_lower_bound"] is True


def test_duplicate_envelope_key_is_rejected_before_last_key_wins(raw_root):
    day_dir = os.path.join(raw_root, "date=%s" % DATE)
    good = _line(sid=1, seq=7540)
    duplicate = good.replace('"source_sequence":7540',
                             '"source_sequence":270913987261794270075,'
                             '"source_sequence":7540', 1)
    _write(day_dir, "l2_00.ndjson", [
        _line(sid=1, seq=7539), duplicate, _line(sid=1, seq=7541),
    ])

    record = lgc.scan_date(DATE, raw_root)
    assert record["corrupt_rows"] == 1
    assert record["corrupt_sequence_rows"] == 1
    assert record["corrupt_row_reasons"] == {"duplicate_envelope_key": 1}
    assert record["seq_gap_events"] == 0
    assert record["seq_missed_total"] == 0
    assert record["seq_regressions"] == 0


def test_malformed_row_breaks_continuity_instead_of_inventing_gap(raw_root):
    day_dir = os.path.join(raw_root, "date=%s" % DATE)
    _write(day_dir, "l2_10.ndjson", [
        _line(sid=3, seq=1), "not json", _line(sid=3, seq=9),
    ])
    record = lgc.scan_date(DATE, raw_root)
    assert record["corrupt_row_reasons"] == {"outer_json": 1}
    assert record["seq_gap_events"] == 0
    assert record["seq_missed_total"] == 0
    assert record["seq_counts_are_lower_bound"] is True


def test_corrupt_file_base_is_quarantined_from_reported_gap_totals(raw_root):
    day_dir = os.path.join(raw_root, "date=%s" % DATE)
    _write(day_dir, "l2_10.ndjson", [
        _line(sid=3, seq=1), _line(sid=3, seq=9), "not json",
    ])
    _write(day_dir, "l2_11.ndjson", [
        _line(sid=3, seq=1), _line(sid=3, seq=4),
    ])
    record = lgc.scan_date(DATE, raw_root)
    assert record["untrusted_file_bases"] == ["l2_10"]
    assert record["trusted_file_bases"] == ["l2_11"]
    assert record["seq_gap_events"] == 1
    assert record["seq_missed_total"] == 2
    assert record["seq_gap_events_discarded_untrusted"] == 1
    assert record["seq_missed_total_discarded_untrusted"] == 7


def test_legitimate_gap_and_regression_still_count(raw_root):
    day_dir = os.path.join(raw_root, "date=%s" % DATE)
    _write(day_dir, "l2_10.ndjson", [
        _line(sid=3, seq=1), _line(sid=3, seq=9), _line(sid=3, seq=4),
    ])
    record = lgc.scan_date(DATE, raw_root)
    assert record["seq_gap_events"] == 1
    assert record["seq_missed_total"] == 7
    assert record["seq_regressions"] == 1
    assert record["corrupt_rows"] == 0


def test_rotation_shards_use_numeric_order(raw_root):
    day_dir = os.path.join(raw_root, "date=%s" % DATE)
    _write(day_dir, "l2_13.ndjson", [_line(sid=1, seq=n) for n in (1, 2, 3)])
    _write(day_dir, "l2_13.ndjson.2", [_line(sid=1, seq=n) for n in (4, 5)])
    _write(day_dir, "l2_13.ndjson.10", [_line(sid=1, seq=n) for n in (6, 7)])
    record = lgc.scan_date(DATE, raw_root)
    assert record["files"] == ["l2_13.ndjson", "l2_13.ndjson.2",
                               "l2_13.ndjson.10"]
    assert record["seq_gap_events"] == 0
    assert record["seq_regressions"] == 0


def test_payload_text_cannot_fake_envelope_fields(raw_root):
    day_dir = os.path.join(raw_root, "date=%s" % DATE)
    raw = json.dumps({"type": "trade", "msg": {
        "note": '"sid":40,"source_sequence":40,"marker":"gap"'}})
    _write(day_dir, "l2_09.ndjson", [_line("trade", sid=None, seq=None,
                                             raw=raw)])
    record = lgc.scan_date(DATE, raw_root)
    assert record["sids_total"] == 0
    assert record["recorder_markers"] == {}
    assert record["seq_gap_events"] == 0
    assert record["corrupt_rows"] == 0


def test_no_l2_files_writes_honest_record_and_exits_zero(raw_root, tmp_path):
    out_dir = str(tmp_path / "packs")
    quality_log = str(tmp_path / "quality_log.ndjson")
    rc = lgc.main(["--date", DATE, "--raw-root", raw_root,
                   "--out-dir", out_dir, "--quality-log", quality_log])
    assert rc == 0
    record = json.load(open(os.path.join(out_dir, "l2_gaps_%s.json" % DATE)))
    assert record["no_l2_files"] is True and record["lines"] == 0
    entry = json.loads(open(quality_log).read().splitlines()[-1])
    assert entry["wp"] == "l2-gap-check"
    assert "no l2 raw files" in entry["finding"]


def test_main_is_atomic_and_surfaces_lower_bound(raw_root, tmp_path):
    day_dir = os.path.join(raw_root, "date=%s" % DATE)
    _write(day_dir, "l2_13.ndjson", [
        _line(sid=7, seq=1), _line(sid=7, seq=9),
    ])
    out_dir = str(tmp_path / "packs")
    quality_log = str(tmp_path / "quality_log.ndjson")
    rc = lgc.main(["--date", DATE, "--raw-root", raw_root,
                   "--out-dir", out_dir, "--quality-log", quality_log])
    assert rc == 0
    assert not [path for path in os.listdir(out_dir) if ".tmp" in path]
    record = json.load(open(os.path.join(out_dir, "l2_gaps_%s.json" % DATE)))
    assert record["seq_gap_events"] == 1
    assert record["seq_missed_total"] == 7
    entry = json.loads(open(quality_log).read().splitlines()[-1])
    assert "seq_gaps=1 (missed=7; lower_bound=false)" in entry["finding"]


def test_bad_date_is_usage_error(raw_root):
    with pytest.raises(SystemExit):
        lgc.main(["--date", "not-a-date", "--raw-root", raw_root])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
