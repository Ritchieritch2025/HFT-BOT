import json
from pathlib import Path

import duckdb

import l2_hypothesis_stage as l2


def row(
    date, clock_ns, market, msg_type, *, side=None, price=None, delta=None,
    yes_levels=None, no_levels=None, root="ROOT-A", family="SERIES-A",
):
    return (
        date,
        clock_ns // 1000,
        clock_ns,
        clock_ns + 7,
        market,
        msg_type,
        side,
        price,
        delta,
        json.dumps(yes_levels) if yes_levels is not None else None,
        json.dumps(no_levels) if no_levels is not None else None,
        1,
        1,
        root,
        "Baseball",
        family,
        0,
    )


def test_snapshot_reset_right_censors_and_never_refills():
    base = 1_000_000_000_000
    rows = [
        row("2026-07-13", base, "M1", "snapshot",
            yes_levels=[[4000, 20_000]], no_levels=[[5000, 20_000]]),
        row("2026-07-13", base + 1_000_000, "M1", "delta",
            side="yes", price=4000, delta=-10_000),
        row("2026-07-13", base + 2_000_000, "M1", "snapshot",
            yes_levels=[[4000, 20_000]], no_levels=[[5000, 20_000]]),
    ]
    result = l2.replay_rows(rows)
    assert result["qc"]["qualifying_touch_depletions"] == 1
    assert result["qc"]["snapshot_censored_episodes"] == 1
    assert len(result["depletion_episodes"]) == 1
    episode = result["depletion_episodes"][0]
    assert episode["endpoint_reason"] == "right_censored_snapshot_boundary"
    assert episode["event_observed"] is False
    assert episode["refill_ns"] is None


def test_refill_requires_causal_positive_delta_after_depletion():
    base = 2_000_000_000_000
    rows = [
        row("2026-07-13", base, "M1", "snapshot",
            yes_levels=[[4000, 20_000]], no_levels=[[5000, 20_000]]),
        row("2026-07-13", base + 1_000_000, "M1", "delta",
            side="yes", price=4000, delta=-10_000),
        row("2026-07-13", base + 51_000_000, "M1", "delta",
            side="yes", price=4000, delta=8_000),
    ]
    result = l2.replay_rows(rows)
    episode = result["depletion_episodes"][0]
    assert episode["endpoint_reason"] == "refill_observed"
    assert episode["event_observed"] is True
    assert episode["refill_ns"] == base + 51_000_000
    assert episode["refill_fraction"] == 0.8


def test_authoritative_receipt_not_per_market_sequence_projection():
    clean = {
        "date": "2026-07-13", "lines": 100, "parse_errors": 0,
        "seq_gap_events": 0, "seq_missed_total": 0, "seq_regressions": 0,
        "markers_lost_frames": 0, "stream_restarts": 2,
        "snapshot_re_anchors_total": 3, "no_l2_files": False,
        "recorder_markers": {},
    }
    assessment = l2.receipt_assessment(clean)
    assert assessment["usable_for_continuous_replay"] is True
    assert assessment["per_market_ws_seq_gap_inference_used"] is False
    broken = dict(clean, seq_gap_events=1)
    assert l2.receipt_assessment(broken)["usable_for_continuous_replay"] is False


def test_coordination_threshold_is_trained_and_causal():
    events = []
    for date, offset in ((l2.TRAIN_DATE, 0), (l2.EVAL_DATE, 10_000_000_000)):
        for index, market in enumerate(("M1", "M2")):
            events.append({
                "date": date, "clock_ns": offset + index * 10_000_000,
                "event_id": f"{date}-{market}", "market_ticker": market,
                "root_event_id": "ROOT", "side": "yes",
            })
    annotated, threshold = l2.annotate_retreats(events)
    assert threshold == 2
    assert all(e["causal_component_max_ns"] <= e["clock_ns"] for e in annotated)
    assert max(e["causal_coordination_score"] for e in annotated) == 2


def test_asof_outcomes_use_current_and_past_at_target(tmp_path: Path):
    con = duckdb.connect(":memory:")
    spool = l2.StateSpool(tmp_path / "states.csv")
    state_id = 0
    for market, root, current_mid, future_mid in (
        ("T", "RT", 0.0, 0.2),
        ("C", "RC", 0.0, 0.05),
    ):
        for clock, mid in ((1_000_000_000, current_mid), (1_100_000_000, future_mid)):
            state_id += 1
            spool.add({
                "state_id": state_id, "date": "2026-07-13", "clock_ns": clock,
                "market_ticker": market, "root_event_id": root,
                "sport": "Baseball", "family": "S", "bid_e4": 4000,
                "bid_qty_e4": 10_000, "ask_e4": 5000, "ask_qty_e4": 10_000,
                "spread_logodds": 0.4, "mid_logodds": mid, "snapshot_epoch": 1,
            })
    l2.load_state_table(con, spool)
    rows = [
        ("P1", "HFOLLOW", 0, "treatment", "T1", "2026-07-13",
         1_000_000_000, "T", "RT", "Baseball", "S", 1, None, None),
        ("P1", "HFOLLOW", 0, "control", "C1", "2026-07-13",
         1_000_000_000, "C", "RC", "Baseball", "S", 1, None, None),
    ]
    l2.compute_outcomes(con, rows)
    result = con.execute(
        "SELECT adverse_effect_logodds FROM l2_pair_effects "
        "WHERE analysis='HFOLLOW' AND horizon_us=100000"
    ).fetchone()
    assert result is not None
    assert abs(result[0] - 0.15) < 1e-12
    con.close()


def test_one_day_status_can_only_collect_more():
    status, reason = l2.hypothesis_status(12, 8, 1, False)
    assert status == "COLLECT_MORE"
    assert "Only 1 evaluation day" in reason
