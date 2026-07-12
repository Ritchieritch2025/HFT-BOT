#!/usr/bin/env python3
"""PIPE-W06 Stage 1: l2_gap_check contract (spec §5, D2 counted-not-hidden).

Synthetic raw fixtures only (WsRecorder envelope format from
src/storage.cpp RawLogWriter::write). The contract under test:
  * per-sid seq continuity keyed by (hour file base, stream_epoch, sid):
    a jump counts a gap event + exact missed count; seq==1 on an
    established stream is a benign stream_restart; anything else backwards
    is a regression — three separate counters, none folded together;
  * rotation shards continue their base stream in NUMERIC order
    (.10 after .2 — the lexical trap must not invent gaps);
  * WsRecorder markers (gap/loss/epoch_change/...) are all counted and a
    loss marker's source_sequence sums into markers_lost_frames;
  * per-market orderbook_snapshot re-anchors on the same stream;
  * payload bytes can never fake an envelope field (the `,"raw"` cut);
  * a day with no l2 files writes an honest no_l2_files record;
  * atomic record write + a quality_log line, and gaps still exit 0
    (evidence, not a gate).
"""
import json
import os
import sys

import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import l2_gap_check as lgc  # noqa: E402

DATE = "2026-07-11"
WALL = 1_752_300_000_000_000_000  # ns, inside the plausible window


def _line(channel="orderbook_delta", ticker="KXMLB-26JUL12-BOS", sid=None,
          seq=None, epoch=None, marker=None, raw=None):
    """One WsRecorder envelope line (key order mirrors RawLogWriter::write:
    every envelope field precedes "raw")."""
    o = {"recv_mono_ns": 1, "recv_wall_ns": WALL, "source": "Kalshi",
         "channel": channel, "source_ticker": ticker}
    if seq is not None:
        o["source_sequence"] = seq
    if sid is not None:
        o["sid"] = sid
    if epoch:
        o["stream_epoch"] = epoch
    if marker is not None:
        o["marker"] = marker
    o["raw"] = raw if raw is not None else json.dumps(
        {"type": channel, "sid": sid, "seq": seq,
         "msg": {"market_ticker": ticker}})
    # compact separators: RawLogWriter emits no whitespace (src/storage.cpp)
    return json.dumps(o, separators=(",", ":"))


def _marker(kind, lost=None):
    o = {"recv_mono_ns": 1, "recv_wall_ns": WALL, "source": "Kalshi",
         "channel": "", "source_ticker": ""}
    if lost is not None:
        o["source_sequence"] = lost
    o["marker"] = kind
    return json.dumps(o, separators=(",", ":"))


def _write(day_dir, name, lines):
    os.makedirs(day_dir, exist_ok=True)
    with open(os.path.join(day_dir, name), "w") as f:
        f.write("\n".join(lines) + "\n")


@pytest.fixture
def raw_root(tmp_path):
    return str(tmp_path / "raw")


def test_seq_gaps_markers_restarts_and_re_anchors(raw_root):
    day_dir = os.path.join(raw_root, "date=%s" % DATE)
    mt = "KXMLB-26JUL12-BOS"
    _write(day_dir, "l2_13.ndjson", [
        _line("orderbook_snapshot", mt, sid=7, seq=1),
        _line("orderbook_delta", mt, sid=7, seq=2),
        _line("orderbook_delta", mt, sid=7, seq=3),
        _line("orderbook_delta", mt, sid=7, seq=6),   # gap: 4,5 missed
        _marker("gap"),
        _marker("loss", lost=5),
        _marker("epoch_change"),
        _line("orderbook_snapshot", mt, sid=7, seq=7),  # re-anchor, same stream
    ])
    _write(day_dir, "l2_13.ndjson.1", [                 # rotation shard: same stream
        _line("orderbook_delta", mt, sid=7, seq=8),     # continues; NOT a gap
        _line("orderbook_snapshot", mt, sid=7, seq=1),  # mid-hour relaunch: restart
    ])
    _write(day_dir, "l2_14.ndjson", [                   # next hour: its own stream
        _line("orderbook_snapshot", mt, sid=7, seq=1),  # first-seen, NOT a restart
    ])
    rec = lgc.scan_date(DATE, raw_root)
    assert rec["no_l2_files"] is False
    assert rec["files"] == ["l2_13.ndjson", "l2_13.ndjson.1", "l2_14.ndjson"]
    assert rec["lines"] == 11 and rec["parse_errors"] == 0
    assert rec["seq_gap_events"] == 1
    assert rec["seq_missed_total"] == 2
    assert rec["seq_regressions"] == 0
    assert rec["stream_restarts"] == 1
    assert rec["sids_total"] == 2            # (l2_13,0,7) + (l2_14,0,7)
    assert rec["sids_with_seq_gaps"] == 1
    assert rec["recorder_markers"] == {"gap": 1, "loss": 1, "epoch_change": 1}
    assert rec["markers_lost_frames"] == 5
    m = rec["per_market"][mt]
    # the shard's post-restart snapshot anchors a "fresh" stream state on the
    # same key, so it counts as a re-anchor too — snapshots beyond the first
    # per (base,epoch,sid,market) are surfaced, never smoothed away (D2)
    assert m["msgs"] == 8 and m["snapshots"] == 4 and m["re_anchors"] == 2
    assert rec["snapshot_re_anchors_total"] == 2


def test_true_seq_regression_is_counted_separately(raw_root):
    day_dir = os.path.join(raw_root, "date=%s" % DATE)
    _write(day_dir, "l2_10.ndjson", [
        _line(sid=3, seq=5),
        _line(sid=3, seq=6),
        _line(sid=3, seq=4),   # backwards, not a fresh stream
    ])
    rec = lgc.scan_date(DATE, raw_root)
    assert rec["seq_regressions"] == 1
    assert rec["seq_gap_events"] == 0 and rec["stream_restarts"] == 0


def test_rotation_shards_scan_in_numeric_order(raw_root):
    day_dir = os.path.join(raw_root, "date=%s" % DATE)
    _write(day_dir, "l2_13.ndjson", [_line(sid=1, seq=n) for n in (1, 2, 3)])
    _write(day_dir, "l2_13.ndjson.2", [_line(sid=1, seq=n) for n in (4, 5)])
    _write(day_dir, "l2_13.ndjson.10", [_line(sid=1, seq=n) for n in (6, 7)])
    rec = lgc.scan_date(DATE, raw_root)
    # lexical order (.10 before .2) would fabricate a regression + a gap
    assert rec["files"] == ["l2_13.ndjson", "l2_13.ndjson.2", "l2_13.ndjson.10"]
    assert rec["seq_gap_events"] == 0
    assert rec["seq_regressions"] == 0
    assert rec["stream_restarts"] == 0


def test_payload_bytes_cannot_fake_envelope_fields(raw_root):
    day_dir = os.path.join(raw_root, "date=%s" % DATE)
    evil = json.dumps({"type": "trade", "msg": {
        "note": '"sid":40,"source_sequence":40,"marker":"gap"'}})
    _write(day_dir, "l2_09.ndjson", [
        _line("trade", "KXMLB-26JUL12-BOS", raw=evil),  # unsequenced channel (I9)
    ])
    rec = lgc.scan_date(DATE, raw_root)
    assert rec["sids_total"] == 0
    assert rec["recorder_markers"] == {}
    assert rec["seq_gap_events"] == 0 and rec["parse_errors"] == 0
    assert rec["per_market"]["KXMLB-26JUL12-BOS"]["msgs"] == 1


def test_firehose_files_are_out_of_scope(raw_root):
    day_dir = os.path.join(raw_root, "date=%s" % DATE)
    _write(day_dir, "firehose_13.ndjson", [_line(sid=1, seq=1)])
    rec = lgc.scan_date(DATE, raw_root)
    assert rec["no_l2_files"] is True and rec["files"] == []


def test_no_l2_files_writes_honest_record_and_exits_zero(raw_root, tmp_path,
                                                         capsys):
    out_dir = str(tmp_path / "packs")
    qlog = str(tmp_path / "quality_log.ndjson")
    rc = lgc.main(["--date", DATE, "--raw-root", raw_root,
                   "--out-dir", out_dir, "--quality-log", qlog])
    assert rc == 0
    rec = json.load(open(os.path.join(out_dir, "l2_gaps_%s.json" % DATE)))
    assert rec["no_l2_files"] is True and rec["lines"] == 0
    entry = json.loads(open(qlog).read().splitlines()[-1])
    assert entry["wp"] == "l2-gap-check" and entry["window"] == DATE
    assert "no l2 raw files" in entry["finding"]
    assert "recorded" == entry["action"]


def test_main_record_is_atomic_and_gaps_still_exit_zero(raw_root, tmp_path):
    day_dir = os.path.join(raw_root, "date=%s" % DATE)
    _write(day_dir, "l2_13.ndjson", [
        _line(sid=7, seq=1), _line(sid=7, seq=9),      # 7 missed
    ])
    out_dir = str(tmp_path / "packs")
    qlog = str(tmp_path / "quality_log.ndjson")
    rc = lgc.main(["--date", DATE, "--raw-root", raw_root,
                   "--out-dir", out_dir, "--quality-log", qlog])
    assert rc == 0  # counted-not-hidden: gaps are evidence, not a gate (D2)
    assert not [p for p in os.listdir(out_dir) if ".tmp" in p]
    rec = json.load(open(os.path.join(out_dir, "l2_gaps_%s.json" % DATE)))
    assert rec["seq_gap_events"] == 1 and rec["seq_missed_total"] == 7
    assert "generated_at_utc" in rec
    entry = json.loads(open(qlog).read().splitlines()[-1])
    assert "seq_gaps=1 (missed=7)" in entry["finding"]
    assert entry["evidence"].endswith("l2_gaps_%s.json" % DATE)


def test_bad_date_is_a_usage_error(raw_root):
    with pytest.raises(SystemExit):
        lgc.main(["--date", "not-a-date", "--raw-root", raw_root])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
