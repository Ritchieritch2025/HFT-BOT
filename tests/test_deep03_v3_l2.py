#!/usr/bin/env python3
"""Adversarial contracts for bounded Deep03 L2/SNBD research."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "research"))

import deep03_v3_l2 as l2  # noqa: E402


def row(
    clock_ns: int,
    market: str,
    msg_type: str,
    *,
    side: str | None = None,
    price: int | None = None,
    delta: int | None = None,
    yes: object | None = None,
    no: object | None = None,
    sid: int = 7,
    seq: int = 1,
    date: str = "2026-07-12",
) -> tuple[object, ...]:
    return (
        date,
        clock_ns // 1000,
        clock_ns,
        clock_ns + 17,
        market,
        "EVENT-A",
        "Baseball",
        "SERIES-A",
        msg_type,
        side,
        price,
        delta,
        json.dumps(yes) if yes is not None else None,
        json.dumps(no) if no is not None else None,
        sid,
        seq,
    )


def clean_receipt(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "date": "2026-07-12",
        "lines": 100,
        "parse_errors": 0,
        "seq_gap_events": 0,
        "seq_missed_total": 0,
        "seq_regressions": 0,
        "markers_lost_frames": 0,
        "no_l2_files": False,
        "recorder_markers": {},
    }
    value.update(overrides)
    return value


def test_full_stream_gap_receipt_is_fail_closed():
    assert l2.assess_l2_quality_receipt(clean_receipt())["state"] == "PASS"
    blocked = l2.assess_l2_quality_receipt(clean_receipt(seq_gap_events=1))
    assert blocked["state"] == "REFUSED"
    assert blocked["blockers"] == ["seq_gap_events=1"]
    marker = l2.assess_l2_quality_receipt(
        clean_receipt(recorder_markers={"epoch_change": 2})
    )
    assert marker["state"] == "REFUSED"
    assert marker["per_market_forward_ws_seq_gap_inference_used"] is False


def test_forward_per_market_sequence_jump_is_not_packet_loss():
    base = 1_000_000_000_000
    result = l2.replay_rows([
        row(base, "M1", "snapshot", yes=[[4000, 20_000]],
            no=[[5000, 20_000]], seq=1),
        # seq 2..98 may belong to sibling markets on the same sid.
        row(base + 1_000_000, "M1", "delta", side="yes", price=4000,
            delta=1_000, seq=99),
    ])
    assert [item["classification"] for item in result["replay_rows"]] == [
        "SNAPSHOT_APPLIED", "DELTA_APPLIED"
    ]
    assert result["qc"]["source_rows"] == result["qc"]["replay_rows"] == 2


def test_epoch_change_requires_snapshot_and_later_snapshot_recovers():
    base = 2_000_000_000_000
    result = l2.replay_rows([
        row(base, "M1", "snapshot", yes=[[4000, 20_000]],
            no=[[5000, 20_000]], sid=7, seq=1),
        row(base + 1_000_000, "M1", "delta", side="yes", price=4000,
            delta=1_000, sid=8, seq=1),
        row(base + 2_000_000, "M1", "delta", side="yes", price=4000,
            delta=1_000, sid=8, seq=2),
        row(base + 3_000_000, "M1", "snapshot", yes=[[4100, 30_000]],
            no=[[5000, 10_000]], sid=8, seq=3),
        row(base + 4_000_000, "M1", "delta", side="yes", price=4100,
            delta=1_000, sid=8, seq=4),
    ])
    assert [item["classification"] for item in result["replay_rows"]] == [
        "SNAPSHOT_APPLIED",
        "REJECTED_EPOCH_CHANGE_WITHOUT_SNAPSHOT",
        "REJECTED_DELTA_BEFORE_SNAPSHOT",
        "SNAPSHOT_APPLIED",
        "DELTA_APPLIED",
    ]


def test_sequence_regression_invalidates_until_snapshot_reset():
    base = 3_000_000_000_000
    result = l2.replay_rows([
        row(base, "M1", "snapshot", yes=[[4000, 20_000]],
            no=[[5000, 20_000]], seq=10),
        row(base + 1_000_000, "M1", "delta", side="yes", price=4000,
            delta=1_000, seq=9),
        row(base + 2_000_000, "M1", "snapshot", yes=[[4000, 21_000]],
            no=[[5000, 20_000]], seq=11),
    ])
    assert result["replay_rows"][1]["classification"] == \
        "REJECTED_SEQUENCE_REGRESSION"
    assert result["replay_rows"][1]["book_valid"] is False
    assert result["replay_rows"][2]["classification"] == "SNAPSHOT_APPLIED"


def test_negative_result_invalidates_and_censors_open_episode():
    base = 4_000_000_000_000
    result = l2.replay_rows([
        row(base, "M1", "snapshot", yes=[[4000, 20_000]],
            no=[[5000, 20_000]], seq=1),
        row(base + 1_000_000, "M1", "delta", side="yes", price=4000,
            delta=-10_000, seq=2),
        row(base + 2_000_000, "M1", "delta", side="yes", price=4000,
            delta=-20_000, seq=3),
    ])
    assert result["replay_rows"][2]["classification"] == \
        "REJECTED_NEGATIVE_RESULT"
    assert len(result["episodes"]) == 1
    assert result["episodes"][0]["endpoint_reason"] == \
        "right_censored_invalid_epoch"
    assert result["episodes"][0]["event_observed"] is False


def test_snapshot_reset_censors_and_never_counts_as_refill():
    base = 5_000_000_000_000
    result = l2.replay_rows([
        row(base, "M1", "snapshot", yes=[[4000, 20_000]],
            no=[[5000, 20_000]], seq=1),
        row(base + 1_000_000, "M1", "delta", side="yes", price=4000,
            delta=-10_000, seq=2),
        row(base + 2_000_000, "M1", "snapshot", yes=[[4000, 20_000]],
            no=[[5000, 20_000]], seq=3),
    ])
    assert len(result["episodes"]) == 1
    episode = result["episodes"][0]
    assert episode["endpoint_reason"] == "right_censored_snapshot_boundary"
    assert episode["event_observed"] is False
    assert episode["refill_ns"] is None


def test_refill_is_causal_positive_depth_recovery_and_features_are_exact():
    base = 6_000_000_000_000
    result = l2.replay_rows([
        row(base, "M1", "snapshot", yes=[[3900, 10_000], [4000, 20_000]],
            no=[[4900, 10_000], [5000, 30_000]], seq=1),
        row(base + 1_000_000, "M1", "delta", side="yes", price=4000,
            delta=-10_000, seq=2),
        row(base + 51_000_000, "M1", "delta", side="yes", price=4000,
            delta=8_000, seq=3),
    ])
    first = result["replay_rows"][0]
    assert first["topology"] == "TWO_SIDED"
    assert first["bid_e4"] == 4000 and first["ask_e4"] == 5000
    assert first["bid_depth3_e4"] == 30_000
    assert first["ask_depth3_e4"] == 40_000
    assert first["microprice_e4"] == pytest.approx(4400.0)
    assert first["imbalance_depth3"] == pytest.approx(-1 / 7)
    assert len(result["episodes"]) == 1
    episode = result["episodes"][0]
    assert episode["endpoint_reason"] == "refill_observed"
    assert episode["refill_fraction"] == pytest.approx(0.8)
    assert episode["duration_us"] == 50_000

