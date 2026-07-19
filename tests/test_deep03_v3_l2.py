#!/usr/bin/env python3
"""Adversarial contracts for bounded Deep03 L2/SNBD research."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import duckdb


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


def _write_exact_fixture(tmp_path: Path) -> dict[str, object]:
    objects: list[dict[str, object]] = []
    releases = []
    for index, date in enumerate(l2.L2_SCOPE_DATES):
        release_id = f"{date}__v3ref__fixture"
        releases.append({"date": date, "release_id": release_id})
        if date in l2.L2_ABSENT_DATES:
            continue
        fact = tmp_path / f"facts/orderbooks_full/category=Sports/date={date}/part.parquet"
        fact.parent.mkdir(parents=True, exist_ok=True)
        base = 1_800_000_000_000_000_000 + index * 10_000_000
        con = duckdb.connect()
        con.execute("""
          CREATE TABLE source(
            date DATE,local_recv_ts_us BIGINT,recv_wall_ns BIGINT,
            recv_mono_ns BIGINT,market_ticker VARCHAR,event_ticker VARCHAR,
            subcategory VARCHAR,series_ticker VARCHAR,msg_type VARCHAR,
            side VARCHAR,price_e4 BIGINT,delta_e4 BIGINT,yes_levels VARCHAR,
            no_levels VARCHAR,ws_sid BIGINT,ws_seq BIGINT
          )
        """)
        con.executemany("INSERT INTO source VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (
                date, base // 1000, base, base + 1, f"M-{index}", f"E-{index}",
                "Baseball", f"S-{index}", "snapshot", None, None, None,
                "[[4000,20000]]", "[[5000,20000]]", 7, 1,
            ),
            (
                date, (base + 1_000_000) // 1000, base + 1_000_000, base + 2,
                f"M-{index}", f"E-{index}", "Baseball", f"S-{index}",
                "delta", "yes", 4000, -10_000, None, None, 7, 2,
            ),
            (
                date, (base + 51_000_000) // 1000, base + 51_000_000, base + 3,
                f"M-{index}", f"E-{index}", "Baseball", f"S-{index}",
                "delta", "yes", 4000, 8_000, None, None, 7, 3,
            ),
        ])
        con.execute(f"COPY source TO '{fact}' (FORMAT PARQUET)")
        con.close()
        fact_payload = fact.read_bytes()
        objects.append({
            "release_id": release_id,
            "date": date,
            "kind": "facts",
            "channel": "orderbooks_full",
            "logical_key": (
                f"warehouse/facts/orderbooks_full/category=Sports/date={date}/part.parquet"
            ),
            "local_path": str(fact),
            "source_version_id": f"fact-version-{date}",
            "sha256": __import__("hashlib").sha256(fact_payload).hexdigest(),
            "size": len(fact_payload),
            "row_count": 3,
        })
        quality = tmp_path / f"quality/date={date}/l2_gaps.json"
        quality.parent.mkdir(parents=True, exist_ok=True)
        quality.write_text(json.dumps(clean_receipt(date=date)) + "\n")
        quality_payload = quality.read_bytes()
        objects.append({
            "release_id": release_id,
            "date": date,
            "kind": "l2_quality_receipt",
            "channel": None,
            "logical_key": f"control/quality/v1/date={date}/l2_gaps.json",
            "local_path": str(quality),
            "source_version_id": f"quality-version-{date}",
            "sha256": __import__("hashlib").sha256(quality_payload).hexdigest(),
            "size": len(quality_payload),
            "row_count": None,
        })
    return {
        "release_ids": [row["release_id"] for row in releases],
        "release_dates": list(l2.L2_SCOPE_DATES),
        "releases": releases,
        "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
        "objects": objects,
    }


def test_bounded_exact_input_absent_days_conservation_and_resume(tmp_path: Path):
    manifest = _write_exact_fixture(tmp_path)
    binding = l2.bounded_source_binding(manifest)
    checkpoint_root = tmp_path / "checkpoints"
    con = duckdb.connect()
    store = l2.BoundedCheckpointStore(checkpoint_root, binding)
    first = l2.execute_l2_snbd_bounded(
        con, manifest, store, market_buckets=2
    )
    assert first["state"] == "COMPLETE"
    assert first["claim_tier"] == "DESCRIPTIVE_ONLY_NO_PNL"
    assert first["row_conservation"]["observed_rows"] == 18
    assert first["row_conservation"]["expected_rows"] == 18
    availability = con.execute(
        f"SELECT date,state FROM read_parquet('{checkpoint_root / 'l2_availability/data/scope.parquet'}') ORDER BY date"
    ).fetchall()
    assert availability[:2] == [
        ("2026-07-10", "ABSENT_NOT_CAPTURED"),
        ("2026-07-11", "ABSENT_NOT_CAPTURED"),
    ]
    assert all(row[1] == "CAPTURED_SEQUENCE_RECEIPT_PASS" for row in availability[2:])
    store.close()

    resumed_store = l2.BoundedCheckpointStore(checkpoint_root, binding)
    resumed = l2.execute_l2_snbd_bounded(
        con, manifest, resumed_store, market_buckets=2
    )
    assert resumed["activity"]["l2_physical"]["written"] == 0
    assert resumed["activity"]["l2_replay"]["written"] == 0
    assert resumed["availability"]["reused"] is True
    resumed_store.close()
    con.close()


def test_bounded_resume_refuses_corrupt_completed_payload(tmp_path: Path):
    manifest = _write_exact_fixture(tmp_path)
    binding = l2.bounded_source_binding(manifest)
    checkpoint_root = tmp_path / "checkpoints"
    con = duckdb.connect()
    store = l2.BoundedCheckpointStore(checkpoint_root, binding)
    l2.execute_l2_snbd_bounded(con, manifest, store, market_buckets=1)
    store.close()
    payload = next((checkpoint_root / "l2_replay/data").glob("*.parquet"))
    with payload.open("ab") as handle:
        handle.write(b"corruption")
    resumed = l2.BoundedCheckpointStore(checkpoint_root, binding)
    with pytest.raises(RuntimeError, match="payload hash mismatch"):
        l2.execute_l2_snbd_bounded(con, manifest, resumed, market_buckets=1)
    resumed.close()
    con.close()


def test_bounded_execution_refuses_gap_receipt_and_absent_day_fact(tmp_path: Path):
    manifest = _write_exact_fixture(tmp_path)
    quality_object = next(
        obj for obj in manifest["objects"]
        if obj["kind"] == "l2_quality_receipt" and obj["date"] == "2026-07-14"
    )
    quality_path = Path(quality_object["local_path"])
    quality_path.write_text(json.dumps(clean_receipt(
        date="2026-07-14", seq_gap_events=1
    )) + "\n")
    quality_object["sha256"] = __import__("hashlib").sha256(
        quality_path.read_bytes()
    ).hexdigest()
    con = duckdb.connect()
    store = l2.BoundedCheckpointStore(
        tmp_path / "gap-checkpoints", l2.bounded_source_binding(manifest)
    )
    with pytest.raises(l2.L2ResearchError, match="sequence receipt refused"):
        l2.execute_l2_snbd_bounded(con, manifest, store, market_buckets=1)
    store.close()

    absent_fact = dict(next(
        obj for obj in manifest["objects"] if obj["kind"] == "facts"
    ))
    absent_fact["date"] = "2026-07-10"
    absent_fact["release_id"] = manifest["release_ids"][0]
    absent_fact["logical_key"] = absent_fact["logical_key"].replace(
        "date=2026-07-12", "date=2026-07-10"
    )
    manifest["objects"].append(absent_fact)
    with pytest.raises(l2.L2ResearchError, match="declared ABSENT"):
        l2._validate_scope(manifest)
    con.close()
