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


def test_missing_sequence_key_invalidates_prior_state_until_snapshot():
    base = 3_500_000_000_000
    missing = list(row(
        base + 1_000_000, "M1", "delta", side="yes", price=4000,
        delta=1_000, seq=2,
    ))
    missing[-1] = None
    result = l2.replay_rows([
        row(base, "M1", "snapshot", yes=[[4000, 20_000]],
            no=[[5000, 20_000]], seq=1),
        tuple(missing),
        row(base + 2_000_000, "M1", "delta", side="yes", price=4000,
            delta=1_000, seq=3),
    ])
    assert result["replay_rows"][1]["classification"] == \
        "REJECTED_MISSING_REPLAY_KEY"
    assert result["replay_rows"][1]["book_valid"] is False
    assert result["replay_rows"][2]["classification"] == \
        "REJECTED_DELTA_BEFORE_SNAPSHOT"


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


def test_unknown_message_type_invalidates_epoch_and_censors_episode():
    base = 5_500_000_000_000
    result = l2.replay_rows([
        row(base, "M1", "snapshot", yes=[[4000, 20_000]],
            no=[[5000, 20_000]], seq=1),
        row(base + 1_000_000, "M1", "delta", side="yes", price=4000,
            delta=-10_000, seq=2),
        row(base + 2_000_000, "M1", "mystery", seq=3),
        row(base + 3_000_000, "M1", "delta", side="yes", price=4000,
            delta=8_000, seq=4),
    ])
    assert result["replay_rows"][2]["classification"] == \
        "REJECTED_INVALID_MESSAGE_TYPE"
    assert result["replay_rows"][2]["book_valid"] is False
    assert result["replay_rows"][3]["classification"] == \
        "REJECTED_DELTA_BEFORE_SNAPSHOT"
    assert result["episodes"][0]["endpoint_reason"] == \
        "right_censored_invalid_epoch"


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
    assert episode["covariate_timing"] == "PRE_DEPLETION_STATE"
    assert episode["pre_side_depth3_e4"] == 30_000
    assert episode["pre_opposite_depth3_e4"] == 40_000
    assert episode["pre_imbalance_depth3"] == pytest.approx(-1 / 7)


def test_refill_exactly_at_closed_one_second_endpoint_is_observed():
    base = 6_250_000_000_000
    depletion = base + 1_000_000
    result = l2.replay_rows([
        row(base, "M1", "snapshot", yes=[[4000, 20_000]],
            no=[[5000, 20_000]], seq=1),
        row(depletion, "M1", "delta", side="yes", price=4000,
            delta=-10_000, seq=2),
        row(depletion + l2.EPISODE_HORIZON_NS, "M1", "delta",
            side="yes", price=4000, delta=8_000, seq=3),
    ])
    assert len(result["episodes"]) == 1
    assert result["episodes"][0]["endpoint_reason"] == "refill_observed"
    assert result["episodes"][0]["duration_us"] == 1_000_000


def test_refill_after_one_second_endpoint_is_not_observed():
    base = 6_275_000_000_000
    depletion = base + 1_000_000
    result = l2.replay_rows([
        row(base, "M1", "snapshot", yes=[[4000, 20_000]],
            no=[[5000, 20_000]], seq=1),
        row(depletion, "M1", "delta", side="yes", price=4000,
            delta=-10_000, seq=2),
        row(depletion + l2.EPISODE_HORIZON_NS + 1, "M1", "delta",
            side="yes", price=4000, delta=8_000, seq=3),
    ])
    assert len(result["episodes"]) == 1
    assert result["episodes"][0]["endpoint_reason"] == \
        "right_censored_1s_horizon"
    assert result["episodes"][0]["event_observed"] is False


def test_top3_retreat_uses_pre_delta_top3_depth_baseline():
    base = 6_300_000_000_000
    result = l2.replay_rows([
        row(base, "M1", "snapshot",
            yes=[[3800, 5_000], [3900, 5_000], [4000, 20_000]],
            no=[[5000, 20_000]], seq=1),
        row(base + 1_000_000, "M1", "delta", side="yes", price=4000,
            delta=-20_000, seq=2),
    ])
    depletion = result["replay_rows"][1]
    assert depletion["pre_side_depth3_e4"] == 30_000
    assert depletion["post_side_depth3_e4"] == 10_000
    assert depletion["top3_removed_e4"] == 20_000
    assert depletion["top3_retreat_fraction"] == pytest.approx(2 / 3)
    assert depletion["top3_retreat"] is True
    assert result["episodes"][0]["top3_retreat"] is True


def test_control_anchor_requires_prior_quiet_top3_state(monkeypatch):
    monkeypatch.setattr(l2, "_stable_control_sample", lambda *_args: True)
    base = 6_400_000_000_000
    result = l2.replay_rows([
        row(base, "M1", "snapshot",
            yes=[[3700, 1_000], [3800, 2_000], [3900, 3_000], [4000, 20_000]],
            no=[[5000, 20_000]], seq=1),
        # Fourth-level depth does not alter the top-three baseline.  It is too
        # early to serve as a quiet anchor.
        row(base + l2.QUIET_ANCHOR_LOOKBACK_NS - 1, "M1", "delta",
            side="yes", price=3700, delta=1_000, seq=2),
        row(base + l2.QUIET_ANCHOR_LOOKBACK_NS, "M1", "delta",
            side="yes", price=3700, delta=1_000, seq=3),
    ])
    assert result["replay_rows"][1]["control_candidate"] is False
    assert result["replay_rows"][2]["control_candidate"] is True
    assert result["replay_rows"][2]["control_quiet_lookback_ns"] == \
        l2.QUIET_ANCHOR_LOOKBACK_NS


def test_clean_full_stream_end_observes_no_refill_through_fixed_horizon():
    base = 6_500_000_000_000
    engine = l2.L2ReplayEngine()
    engine.process(row(
        base, "M1", "snapshot", yes=[[4000, 20_000]],
        no=[[5000, 20_000]], seq=1,
    ))
    engine.process(row(
        base + 1_000_000, "M1", "delta", side="yes", price=4000,
        delta=-10_000, seq=2,
    ))
    episodes = engine.finish(
        observation_end_ns=base + 2 * l2.EPISODE_HORIZON_NS
    )
    assert len(episodes) == 1
    assert episodes[0]["endpoint_reason"] == "right_censored_1s_horizon"
    assert episodes[0]["duration_us"] == 1_000_000
    assert episodes[0]["event_observed"] is False


def _atlas_replay_row(
    clock_ns: int,
    market: str,
    *,
    epoch: int = 1,
    valid: bool = True,
    msg_type: str = "delta",
) -> dict[str, object]:
    value = {column: None for column in l2.REPLAY_COLUMNS}
    value.update({
        "date": "2026-07-12",
        "t_us": clock_ns // 1000,
        "recv_wall_ns": clock_ns,
        "recv_mono_ns": clock_ns + 1,
        "market_ticker": market,
        "event_proxy": f"E-{market}",
        "sport": "Baseball",
        "family": "SERIES-A",
        "ws_sid": 7,
        "ws_seq": clock_ns,
        "msg_type": msg_type,
        "classification": (
            "DELTA_APPLIED" if valid else "REJECTED_NEGATIVE_RESULT"
        ),
        "snapshot_epoch": epoch,
        "book_valid": valid,
        "topology": "TWO_SIDED" if valid else "INVALID_EPOCH",
        "bid_e4": 4000 if valid else None,
        "bid_qty_e4": 20_000 if valid else None,
        "ask_e4": 5000 if valid else None,
        "ask_qty_e4": 20_000 if valid else None,
        "bid_depth3_e4": 20_000 if valid else None,
        "ask_depth3_e4": 20_000 if valid else None,
        "spread_e4": 1000 if valid else None,
        "imbalance_depth3": 0.0 if valid else None,
        "top_changed": valid,
        "touch_depletion": False,
        "top3_retreat": False,
        "control_candidate": False,
    })
    return value


def _atlas_episode(
    episode_id: str,
    duration_us: int,
    observed: bool,
) -> dict[str, object]:
    value = {column: None for column in l2.EPISODE_COLUMNS}
    value.update({
        "episode_id": episode_id,
        "date": "2026-07-12",
        "market_ticker": f"M-{episode_id}",
        "event_proxy": f"E-{episode_id}",
        "sport": "Baseball",
        "family": "SERIES-A",
        "side": "yes",
        "depletion_ns": 1_000_000_000,
        "observation_end_ns": 1_000_000_000 + duration_us * 1000,
        "duration_us": duration_us,
        "endpoint_reason": "refill_observed" if observed else "right_censored_date_end",
        "event_observed": observed,
        "refill_ns": 1_000_000_000 + duration_us * 1000 if observed else None,
        "refill_fraction": 0.8 if observed else None,
        "original_touch_price_e4": 4000,
        "pre_touch_qty_e4": 20_000,
        "removed_e4": 10_000,
        "depletion_fraction": 0.5,
        "post_depletion_depth_e4": 10_000,
        "snapshot_epoch": 1,
        "covariate_timing": "PRE_DEPLETION_STATE",
        "covariate_clock_ns": 1_000_000_000,
        "pre_topology": "TWO_SIDED",
        "pre_spread_e4": 1000,
        "pre_imbalance_depth3": 0.0,
        "pre_side_depth3_e4": 20_000,
        "pre_opposite_depth3_e4": 20_000,
    })
    return value


def test_atlas_extends_final_valid_states_and_labels_reset_invalid_censoring():
    con = duckdb.connect()
    l2._create_build_table(con, "replay_fixture", l2.REPLAY_TYPES)
    replay = [
        _atlas_replay_row(100, "M-RESET", epoch=1, msg_type="snapshot"),
        _atlas_replay_row(200, "M-RESET", epoch=2, msg_type="snapshot"),
        _atlas_replay_row(110, "M-INVALID", epoch=1, msg_type="snapshot"),
        _atlas_replay_row(250, "M-INVALID", epoch=1, valid=False),
        _atlas_replay_row(300, "M-END", epoch=1, msg_type="snapshot"),
    ]
    l2._insert_dict_rows(con, "replay_fixture", l2.REPLAY_COLUMNS, replay)
    l2._create_build_table(con, "episode_fixture", l2.EPISODE_TYPES)
    con.execute(
        "CREATE TEMP TABLE atlas AS "
        + l2._atlas_sql("replay_fixture", "episode_fixture")
    )
    endpoint_rows = dict(con.execute("""
      SELECT endpoint_reason,sum(n_rows)::BIGINT
      FROM atlas WHERE record_kind='STATE' GROUP BY endpoint_reason
    """).fetchall())
    assert endpoint_rows["RIGHT_CENSORED_SNAPSHOT_RESET"] == 1
    assert endpoint_rows["RIGHT_CENSORED_INVALID_OR_REJECTED"] == 1
    assert endpoint_rows["RIGHT_CENSORED_CAPTURE_END"] == 2
    capture_dwell = con.execute("""
      SELECT sum(total_dwell_us) FROM atlas
      WHERE record_kind='STATE'
        AND endpoint_reason='RIGHT_CENSORED_CAPTURE_END'
    """).fetchone()[0]
    # M-RESET's final epoch extends from 200 to the proven stream end at 300;
    # M-END itself contributes a zero-length terminal state.
    assert capture_dwell == pytest.approx(0.1)
    con.close()


def test_refill_hazard_is_interval_risk_set_not_raw_episode_rate():
    con = duckdb.connect()
    l2._create_build_table(con, "replay_fixture", l2.REPLAY_TYPES)
    l2._insert_dict_rows(
        con, "replay_fixture", l2.REPLAY_COLUMNS,
        [_atlas_replay_row(2_000_000_000, "M-BOUND")],
    )
    l2._create_build_table(con, "episode_fixture", l2.EPISODE_TYPES)
    l2._insert_dict_rows(
        con, "episode_fixture", l2.EPISODE_COLUMNS,
        [
            _atlas_episode("FAST", 50_000, True),
            _atlas_episode("EDGE", 100_000, True),
            _atlas_episode("CENSORED", 150_000, False),
        ],
    )
    con.execute(
        "CREATE TEMP TABLE atlas AS "
        + l2._atlas_sql("replay_fixture", "episode_fixture")
    )
    first_two = con.execute("""
      SELECT horizon_start_us,horizon_end_us,at_risk_n,events_n,
             interval_hazard,survival_to_end
      FROM atlas WHERE record_kind='REFILL_HAZARD'
      ORDER BY horizon_start_us LIMIT 2
    """).fetchall()
    assert first_two[0][:4] == (0, 100_000, 3, 2)
    assert first_two[0][4] == pytest.approx(2 / 3)
    assert first_two[0][5] == pytest.approx(1 / 3)
    assert first_two[1][:4] == (100_000, 200_000, 1, 0)
    assert first_two[1][5] == pytest.approx(1 / 3)
    columns = {
        row[0] for row in con.execute("DESCRIBE atlas").fetchall()
    }
    assert "refill_rate" not in columns
    con.close()


def test_matching_refuses_future_and_reset_controls_and_caps_candidates():
    con = duckdb.connect()
    base = 1_900_000_000_000_000_000
    l2._create_build_table(con, "replay_fixture", l2.REPLAY_TYPES)
    controls = []
    for index in range(100):
        candidate = _atlas_replay_row(
            base - index - 1, f"M-PAST-{index}"
        )
        candidate.update({
            "event_proxy": f"E-CONTROL-{index}",
            "side": "yes",
            "ws_seq": index + 1,
            "top_changed": False,
            "control_candidate": True,
            "control_quiet_lookback_ns": l2.QUIET_ANCHOR_LOOKBACK_NS,
        })
        controls.append(candidate)
    future = _atlas_replay_row(base + 1, "M-FUTURE")
    future.update({
        "event_proxy": "E-FUTURE",
        "side": "yes",
        "control_candidate": True,
        "control_quiet_lookback_ns": l2.QUIET_ANCHOR_LOOKBACK_NS,
    })
    reset_adjacent = _atlas_replay_row(base - 1, "M-RESET-ADJACENT")
    reset_adjacent.update({
        "event_proxy": "E-RESET-ADJACENT",
        "side": "yes",
        "control_candidate": True,
        "control_quiet_lookback_ns": l2.QUIET_ANCHOR_LOOKBACK_NS - 1,
    })
    controls.extend((future, reset_adjacent))
    l2._insert_dict_rows(con, "replay_fixture", l2.REPLAY_COLUMNS, controls)
    l2._create_build_table(con, "episode_fixture", l2.EPISODE_TYPES)
    episode = _atlas_episode("TREAT", 1_000_000, False)
    episode.update({
        "market_ticker": "M-TREAT",
        "event_proxy": "E-TREAT",
        "depletion_ns": base,
        "covariate_clock_ns": base,
    })
    l2._insert_dict_rows(con, "episode_fixture", l2.EPISODE_COLUMNS, [episode])
    con.execute(
        "CREATE TEMP TABLE matches AS "
        + l2._matches_sql("replay_fixture", "episode_fixture")
    )
    selected = con.execute("""
      SELECT control_ns,anchor_ns,control_market,control_quiet_lookback_ns,
             candidate_pool_n,stratum_bucket_rank
      FROM matches
    """).fetchone()
    assert selected is not None
    assert selected[0] < selected[1]
    assert selected[2] not in {"M-FUTURE", "M-RESET-ADJACENT"}
    assert selected[3] >= l2.QUIET_ANCHOR_LOOKBACK_NS
    assert selected[4] <= l2.MAX_CONTROLS_PER_STRATUM_BUCKET
    assert selected[5] <= l2.MAX_CONTROLS_PER_STRATUM_BUCKET
    con.close()


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
        receipt_overrides: dict[str, object] = {"date": date}
        if date == "2026-07-13":
            receipt_overrides["seq_gap_events"] = 1
        elif date == "2026-07-14":
            receipt_overrides.update({
                "parse_errors": 37,
                "markers_lost_frames": 1000,
                "recorder_markers": {"loss": 4},
            })
        elif date == "2026-07-16":
            receipt_overrides.update({
                "seq_gap_events": 2,
                "recorder_markers": {"epoch_change": 3},
            })
        quality.write_text(json.dumps(clean_receipt(**receipt_overrides)) + "\n")
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
    assert first["state"] == "COMPLETE_WITH_DATA_QUALITY_EXCLUSIONS"
    assert first["claim_tier"] == "DESCRIPTIVE_CLEAN_DATES_ONLY_NO_PNL"
    assert first["row_accounting"]["all_captured_source_rows"] == 18
    assert first["row_accounting"]["physical_coverage_rows"] == 18
    assert first["row_accounting"]["included_clean_source_rows"] == 9
    assert first["row_accounting"]["excluded_data_quality_rows"] == 9
    assert first["row_accounting"]["replay_rows"] == 9
    assert first["row_conservation"]["included_plus_excluded_coverage"][
        "state"
    ] == "PASS"
    availability = con.execute(
        f"SELECT date,state FROM read_parquet('{checkpoint_root / 'l2_availability/data/scope.parquet'}') ORDER BY date"
    ).fetchall()
    assert availability[:2] == [
        ("2026-07-10", "ABSENT_NOT_CAPTURED"),
        ("2026-07-11", "ABSENT_NOT_CAPTURED"),
    ]
    assert availability[2:] == [
        ("2026-07-12", "CAPTURED_CLEAN_INCLUDED"),
        ("2026-07-13", "EXCLUDED_DATA_QUALITY"),
        ("2026-07-14", "EXCLUDED_DATA_QUALITY"),
        ("2026-07-15", "CAPTURED_CLEAN_INCLUDED"),
        ("2026-07-16", "EXCLUDED_DATA_QUALITY"),
        ("2026-07-17", "CAPTURED_CLEAN_INCLUDED"),
    ]
    assert first["availability"]["included_clean_dates"] == [
        "2026-07-12", "2026-07-15", "2026-07-17"
    ]
    assert first["availability"]["excluded_data_quality_dates"] == [
        "2026-07-13", "2026-07-14", "2026-07-16"
    ]
    assert first["universe_scope"]["full_market_coverage_claim"] is False
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


def test_bounded_execution_excludes_bad_date_without_killing_clean_dates_and_refuses_absent_fact(
    tmp_path: Path,
):
    manifest = _write_exact_fixture(tmp_path)
    quality_object = next(
        obj for obj in manifest["objects"]
        if obj["kind"] == "l2_quality_receipt" and obj["date"] == "2026-07-12"
    )
    quality_path = Path(quality_object["local_path"])
    quality_path.write_text(json.dumps(clean_receipt(
        date="2026-07-12", seq_gap_events=1
    )) + "\n")
    quality_object["sha256"] = __import__("hashlib").sha256(
        quality_path.read_bytes()
    ).hexdigest()
    con = duckdb.connect()
    store = l2.BoundedCheckpointStore(
        tmp_path / "gap-checkpoints", l2.bounded_source_binding(manifest)
    )
    result = l2.execute_l2_snbd_bounded(
        con, manifest, store, market_buckets=1
    )
    assert result["state"] == "COMPLETE_WITH_DATA_QUALITY_EXCLUSIONS"
    assert result["availability"]["included_clean_dates"] == [
        "2026-07-15", "2026-07-17"
    ]
    assert result["row_accounting"]["all_captured_source_rows"] == 18
    assert result["row_accounting"]["physical_coverage_rows"] == 18
    assert result["row_accounting"]["included_clean_source_rows"] == 6
    assert result["row_accounting"]["excluded_data_quality_rows"] == 12
    assert result["quality"]["2026-07-12"]["analysis_disposition"] == \
        "EXCLUDED_DATA_QUALITY"
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


def test_missing_bad_day_quality_receipt_is_local_exclusion(tmp_path: Path):
    manifest = _write_exact_fixture(tmp_path)
    manifest["objects"] = [
        obj for obj in manifest["objects"]
        if not (
            obj["kind"] == "l2_quality_receipt"
            and obj["date"] == "2026-07-13"
        )
    ]
    con = duckdb.connect()
    store = l2.BoundedCheckpointStore(
        tmp_path / "missing-quality-checkpoints",
        l2.bounded_source_binding(manifest),
    )
    result = l2.execute_l2_snbd_bounded(
        con, manifest, store, market_buckets=1
    )
    assert result["availability"]["included_clean_dates"] == [
        "2026-07-12", "2026-07-15", "2026-07-17"
    ]
    assert result["quality"]["2026-07-13"]["analysis_disposition"] == \
        "EXCLUDED_DATA_QUALITY"
    assert result["quality"]["2026-07-13"]["blockers"][0].startswith(
        "quality_receipt_error:"
    )
    assert result["row_accounting"]["physical_coverage_rows"] == 18
    store.close()
    con.close()


def test_exact_reducers_cross_market_buckets_and_match_without_replacement(
    tmp_path: Path,
):
    con = duckdb.connect()
    base = 1_900_000_000_000_000_000
    replay_paths = []
    episode_paths = []
    for bucket in range(2):
        l2._create_build_table(con, "replay_fixture", l2.REPLAY_TYPES)
        replay_row = {column: None for column in l2.REPLAY_COLUMNS}
        replay_clock = base if bucket == 0 else base - 1_000_000
        replay_row.update({
            "date": "2026-07-12",
            "t_us": replay_clock // 1000,
            "recv_wall_ns": replay_clock,
            "recv_mono_ns": 100 + bucket,
            "market_ticker": "M-TREAT" if bucket == 0 else "M-CONTROL",
            "event_proxy": "EVENT-TREAT" if bucket == 0 else "EVENT-CONTROL",
            "sport": "Baseball",
            "family": "SERIES-SHARED",
            "ws_sid": 7 + bucket,
            "ws_seq": 1,
            "msg_type": "snapshot" if bucket == 0 else "delta",
            "side": None if bucket == 0 else "yes",
            "classification": "SNAPSHOT_APPLIED" if bucket == 0 else "DELTA_APPLIED",
            "snapshot_epoch": 1,
            "book_valid": True,
            "topology": "TWO_SIDED",
            "bid_e4": 4000,
            "bid_qty_e4": 20_000,
            "ask_e4": 5000,
            "ask_qty_e4": 20_000,
            "bid_depth3_e4": 20_000,
            "ask_depth3_e4": 20_000,
            "bid_levels": 1,
            "ask_levels": 1,
            "mid_e4": 4500.0,
            "microprice_e4": 4500.0,
            "spread_e4": 1000,
            "mid_logodds": 0.0,
            "spread_logodds": 0.4,
            "imbalance_depth3": 0.0,
            "top_changed": bucket == 0,
            "touch_depletion": bucket == 0,
            "top3_retreat": bucket == 0,
            "pre_topology": "TWO_SIDED",
            "pre_spread_e4": 1000,
            "pre_imbalance_depth3": 0.0,
            "pre_side_depth3_e4": 20_000,
            "pre_opposite_depth3_e4": 20_000,
            "post_side_depth3_e4": 10_000 if bucket == 0 else 20_000,
            "top3_removed_e4": 10_000 if bucket == 0 else 0,
            "top3_retreat_fraction": 0.5 if bucket == 0 else 0.0,
            "control_candidate": bucket == 1,
            "control_quiet_lookback_ns": (
                l2.QUIET_ANCHOR_LOOKBACK_NS if bucket == 1 else None
            ),
        })
        l2._insert_dict_rows(
            con, "replay_fixture", l2.REPLAY_COLUMNS, [replay_row]
        )
        replay_path = tmp_path / f"replay-{bucket}.parquet"
        con.execute(
            f"COPY replay_fixture TO '{replay_path}' (FORMAT PARQUET)"
        )
        replay_paths.append(replay_path)

        l2._create_build_table(con, "episode_fixture", l2.EPISODE_TYPES)
        if bucket == 0:
            episode = {column: None for column in l2.EPISODE_COLUMNS}
            episode.update({
                "episode_id": "DEP-CROSS-BUCKET",
                "date": "2026-07-12",
                "market_ticker": "M-TREAT",
                "event_proxy": "EVENT-TREAT",
                "sport": "Baseball",
                "family": "SERIES-SHARED",
                "side": "yes",
                "depletion_ns": base,
                "observation_end_ns": base + 1_000_000,
                "duration_us": 1000,
                "endpoint_reason": "refill_observed",
                "event_observed": True,
                "refill_ns": base + 1_000_000,
                "refill_fraction": 0.8,
                "original_touch_price_e4": 4000,
                "pre_touch_qty_e4": 20_000,
                "removed_e4": 10_000,
                "depletion_fraction": 0.5,
                "post_depletion_depth_e4": 10_000,
                "snapshot_epoch": 1,
                "covariate_timing": "PRE_DEPLETION_STATE",
                "covariate_clock_ns": base,
                "pre_topology": "TWO_SIDED",
                "pre_spread_e4": 1000,
                "pre_imbalance_depth3": 0.0,
                "pre_side_depth3_e4": 20_000,
                "pre_opposite_depth3_e4": 20_000,
                "top3_removed_e4": 10_000,
                "top3_retreat_fraction": 0.5,
                "top3_retreat": True,
            })
            l2._insert_dict_rows(
                con, "episode_fixture", l2.EPISODE_COLUMNS, [episode]
            )
        else:
            # Missing event identity must not be replaced with market ticker
            # and then presented as a balanced different-event match.
            no_root = {column: None for column in l2.EPISODE_COLUMNS}
            no_root.update({
                "episode_id": "DEP-MISSING-EVENT",
                "date": "2026-07-12",
                "market_ticker": "M-NO-ROOT",
                "event_proxy": "",
                "sport": "Baseball",
                "family": "SERIES-SHARED",
                "side": "yes",
                "depletion_ns": base,
                "observation_end_ns": base + 1_000_000,
                "duration_us": 1000,
                "endpoint_reason": "right_censored_date_end",
                "event_observed": False,
                "original_touch_price_e4": 4000,
                "pre_touch_qty_e4": 20_000,
                "removed_e4": 10_000,
                "depletion_fraction": 0.5,
                "post_depletion_depth_e4": 10_000,
                "snapshot_epoch": 1,
                "covariate_timing": "PRE_DEPLETION_STATE",
                "covariate_clock_ns": base,
                "pre_topology": "TWO_SIDED",
                "pre_spread_e4": 1000,
                "pre_imbalance_depth3": 0.0,
                "pre_side_depth3_e4": 20_000,
                "pre_opposite_depth3_e4": 20_000,
            })
            l2._insert_dict_rows(
                con, "episode_fixture", l2.EPISODE_COLUMNS, [no_root]
            )
        episode_path = tmp_path / f"episode-{bucket}.parquet"
        con.execute(
            f"COPY episode_fixture TO '{episode_path}' (FORMAT PARQUET)"
        )
        episode_paths.append(episode_path)

    store = l2.BoundedCheckpointStore(tmp_path / "reducers", "a" * 64)
    atlas, matches, reuse = l2._write_date_reducers(
        con,
        store,
        abi=l2._l2_abi(2),
        date="2026-07-12",
        replay_paths=replay_paths,
        episode_paths=episode_paths,
    )
    assert reuse == {"atlas_reused": False, "match_reused": False}
    atlas_path = store.root / atlas["data"]["path"]
    assert con.execute(
        f"SELECT sum(n_rows) FROM read_parquet('{atlas_path}') WHERE record_kind='STATE'"
    ).fetchone()[0] == 2
    kinds = {
        row[0] for row in con.execute(
            f"SELECT DISTINCT record_kind FROM read_parquet('{atlas_path}')"
        ).fetchall()
    }
    assert {"STATE", "RETREAT_TOP3_BASELINE", "EPISODE", "REFILL_HAZARD"} <= kinds
    match_path = store.root / matches["data"]["path"]
    assert con.execute(
        f"SELECT count(*) FROM read_parquet('{match_path}')"
    ).fetchone()[0] == 1
    match = con.execute(
        f"SELECT episode_id,treatment_market,control_market,matching_method "
        f"FROM read_parquet('{match_path}')"
    ).fetchone()
    assert match == (
        "DEP-CROSS-BUCKET", "M-TREAT", "M-CONTROL",
        "PAST_ONLY_QUIET_NEAREST_GLOBAL_ARBITRATION",
    )
    temporal = con.execute(
        f"SELECT control_ns<depletion_ns,past_only,quiet_anchor,"
        f"control_quiet_lookback_ns FROM read_parquet('{match_path}')"
    ).fetchone()
    assert temporal == (True, True, True, l2.QUIET_ANCHOR_LOOKBACK_NS)
    match_receipt = json.loads(
        (store.root / "l2_matched_controls/receipts/date=2026-07-12.json").read_text()
    )
    metrics = match_receipt["metrics"]
    assert metrics["invariants"]["future_rows"] == 0
    assert metrics["negative_controls"]["future_leakage"]["state"] == "PASS"
    assert metrics["negative_controls"]["reset_proximity"]["state"] == "PASS"
    assert metrics["negative_controls"]["past_shift_placebo"]["state"] == \
        "DIAGNOSTIC_ONLY_NO_OUTCOME_ESTIMATE"
    assert metrics["negative_controls"]["past_shift_placebo"][
        "future_violations"
    ] == 0
    assert metrics["theoretical_candidates_per_episode_bound"] <= \
        l2.MAX_MATCH_CANDIDATES_PER_EPISODE
    assert "balance" in metrics and "concentration" in metrics
    columns = {
        row[0] for row in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{match_path}')"
        ).fetchall()
    }
    assert not {"pnl", "fees", "fill_probability"} & columns
    store.close()
    con.close()
