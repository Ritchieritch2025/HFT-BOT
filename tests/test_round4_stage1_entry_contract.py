"""Pure-synthetic tests for the ROUND4 Stage-1 entry roster contract."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from tools.research.crypto_mm.round4_stage1_entry_contract import (
    EXPERIMENT_ID,
    EntryStage1ContractError,
    build_capital_interval,
    build_stage1_risk_intervals,
    validate_and_serialize_stage1_rows,
)


T0 = 1_000_000_000_000
LINEAGE = "a" * 64


def pair_episode(
    *,
    episode_id="entry-1",
    cycle_id="cycle-1",
    active=T0,
    terminal=T0 + 1_500_000_000,
    cause="YES_FIRST",
):
    return {
        "experiment_id": EXPERIMENT_ID,
        "data_role": "DISCOVERY",
        "source_date_utc": "2026-07-20",
        "market_ticker": "SYNTH-MARKET",
        "market_cluster_id": "SYNTH-MARKET",
        "cycle_id": cycle_id,
        "entry_episode_id": episode_id,
        "entry_action_id": "P3_SPEND1_A1_SKIP",
        "action_set_version": "DIAGNOSTIC_SOURCE_POLICY_V0",
        "source_rows_sha256": LINEAGE,
        "decision_recv_wall_ns": active,
        "decision_recv_mono_ns": active // 10,
        "feature_asof_wall_ns": active,
        "entry_active_wall_ns": active,
        "terminal_wall_ns": terminal,
        "book_ws_sid": "7",
        "book_ws_seq": 11,
        "action_kind": "ENTRY_PAIR_POST_ONLY",
        "yes_price_e4": 3_000,
        "no_price_e4": 6_900,
        "clip_fp": Decimal("1"),
        "pair_cost_e4": 9_900,
        "locked_pair_ceiling_e4": 9_900,
        "legal_grid": True,
        "yes_post_only": True,
        "no_post_only": True,
        "ack_state": "BOTH_ACKED",
        "tte_ms": 240_000,
        "yes_same_price_ahead_fp": Decimal("2"),
        "no_same_price_ahead_fp": Decimal("3"),
        "yes_better_depth_fp": Decimal("0"),
        "no_better_depth_fp": Decimal("0"),
        "yes_queue_position_fp": Decimal("3"),
        "no_queue_position_fp": Decimal("4"),
        "yes_flow_1s_fp": Decimal("1"),
        "no_flow_1s_fp": Decimal("2"),
        "yes_flow_5s_fp": Decimal("3"),
        "no_flow_5s_fp": Decimal("4"),
        "yes_flow_10s_fp": Decimal("5"),
        "no_flow_10s_fp": Decimal("6"),
        "yes_flow_60s_fp": Decimal("7"),
        "no_flow_60s_fp": Decimal("8"),
        "touch_imbalance": Decimal("0.1"),
        "spread_e4": 100,
        "mid_move_1s_e4": 0,
        "mid_move_10s_e4": 10,
        "terminal_cause": cause,
        "first_fill_wall_ns": (
            terminal if cause in ("YES_FIRST", "NO_FIRST") else None
        ),
        "censor_reason": (
            "ZONE_INVALID_CANCEL_ACK"
            if cause == "ADMIN_CENSOR_NO_FIRST_FILL"
            else None
        ),
    }


def state(*, asof, yes_ahead, no_ahead):
    return {
        "feature_asof_wall_ns": asof,
        "yes_same_price_ahead_fp": Decimal(yes_ahead),
        "no_same_price_ahead_fp": Decimal(no_ahead),
        "yes_better_depth_fp": Decimal("0"),
        "no_better_depth_fp": Decimal("0"),
        "yes_flow_10s_fp": Decimal("5"),
        "no_flow_10s_fp": Decimal("6"),
        "yes_flow_60s_fp": Decimal("7"),
        "no_flow_60s_fp": Decimal("8"),
        "touch_imbalance": Decimal("0.1"),
        "spread_e4": 100,
        "mid_move_1s_e4": 0,
        "mid_move_10s_e4": 10,
        "tte_ms": 240_000,
    }


def stage1_outcome(
    episode,
    *,
    first_fill_episode_id="SYNTH-MARKET|1000000|1001500",
    strict=True,
):
    cause = episode["terminal_cause"]
    admitted = episode["ack_state"] == "BOTH_ACKED"
    return {
        "entry_episode_id": episode["entry_episode_id"],
        "entry_action_id": episode["entry_action_id"],
        "source_date_utc": episode["source_date_utc"],
        "market_ticker": episode["market_ticker"],
        "admitted": admitted,
        "terminal_type": cause,
        "first_fill_episode_id": (
            first_fill_episode_id
            if cause in ("YES_FIRST", "NO_FIRST")
            else None
        ),
        "strict_trade_through_verified": (
            strict if cause in ("YES_FIRST", "NO_FIRST") else None
        ),
        "ack_observation_kind": "SHADOW_IMMEDIATE_BOTH_ACKED",
        "entry_quote_seconds": (
            Decimal(
                episode["terminal_wall_ns"]
                - episode["entry_active_wall_ns"]
            )
            / Decimal("1000000000")
            if admitted
            else Decimal("0")
        ),
        "capital_dollar_seconds": Decimal("0"),
        "peak_episode_capital_usd": Decimal("0"),
        "reconciliation_ok": True,
    }


def test_full_roster_includes_first_fill_no_fill_skip_and_ack_failed():
    first = pair_episode()
    no_fill = pair_episode(
        episode_id="entry-2",
        cycle_id="cycle-2",
        active=T0 + 2_000_000_000,
        terminal=T0 + 7_000_000_000,
        cause="ADMIN_CENSOR_NO_FIRST_FILL",
    )
    skip = pair_episode(
        episode_id="entry-skip",
        cycle_id="cycle-skip",
        active=T0 + 8_000_000_000,
        terminal=T0 + 8_000_000_000,
    )
    skip.update(
        {
            "action_kind": "ENTRY_SKIP",
            "entry_action_id": "ENTRY_SKIP_A1_INVALID",
            "entry_active_wall_ns": None,
            "ack_state": "NOT_SENT",
            "terminal_cause": "ENTRY_SKIP",
            "first_fill_wall_ns": None,
            "yes_price_e4": None,
            "no_price_e4": None,
            "clip_fp": None,
            "pair_cost_e4": None,
            "locked_pair_ceiling_e4": None,
            "legal_grid": False,
            "yes_post_only": False,
            "no_post_only": False,
            "censor_reason": "A1_POST_ALLOCATION_ETA_INVALID",
        }
    )
    ack = pair_episode(
        episode_id="entry-ack",
        cycle_id="cycle-ack",
        active=T0 + 9_000_000_000,
        terminal=T0 + 9_060_000_000,
    )
    ack.update(
        {
            "entry_active_wall_ns": None,
            "ack_state": "ONE_FAILED",
            "terminal_cause": "ACK_FAILED",
            "first_fill_wall_ns": None,
            "censor_reason": "YES_ACK_NO_REJECT_CANCEL_ACK",
        }
    )

    first_risk = build_stage1_risk_intervals(
        first,
        {
            Decimal("0"): state(asof=T0, yes_ahead="2", no_ahead="3"),
            Decimal("1000"): state(
                asof=T0 + 900_000_000,
                yes_ahead="1",
                no_ahead="2",
            ),
        },
    )
    no_fill_risk = build_stage1_risk_intervals(
        no_fill,
        {
            Decimal("0"): state(
                asof=no_fill["entry_active_wall_ns"],
                yes_ahead="2",
                no_ahead="3",
            ),
            Decimal("1000"): state(
                asof=no_fill["entry_active_wall_ns"] + 900_000_000,
                yes_ahead="1",
                no_ahead="2",
            ),
            Decimal("2000"): state(
                asof=no_fill["entry_active_wall_ns"] + 1_900_000_000,
                yes_ahead="1",
                no_ahead="1",
            ),
        },
    )

    capital = [
        build_capital_interval(first, 0, Decimal("0.99")),
        build_capital_interval(no_fill, 0, Decimal("0.99")),
        {
            "entry_episode_id": ack["entry_episode_id"],
            "entry_action_id": ack["entry_action_id"],
            "segment_index": 0,
            "segment_start_wall_ns": ack["decision_recv_wall_ns"],
            "segment_stop_wall_ns": ack["terminal_wall_ns"],
            "locked_capital_usd": Decimal("0.30"),
            "capital_dollar_seconds": Decimal("0.018"),
            "component_reason": "ONE_ACKED_ROLLBACK_RESERVATION",
        },
    ]
    outcomes = [
        stage1_outcome(first),
        stage1_outcome(no_fill),
        stage1_outcome(skip),
        stage1_outcome(ack),
    ]
    outcomes[0]["capital_dollar_seconds"] = Decimal("1.485")
    outcomes[0]["peak_episode_capital_usd"] = Decimal("0.99")
    outcomes[1]["capital_dollar_seconds"] = Decimal("4.95")
    outcomes[1]["peak_episode_capital_usd"] = Decimal("0.99")
    outcomes[3].update(
        {
            "ack_observation_kind": "SYNTHETIC_ACK_FAILURE_TEST",
            "capital_dollar_seconds": Decimal("0.018"),
            "peak_episode_capital_usd": Decimal("0.30"),
            "entry_quote_seconds": Decimal("0"),
        }
    )

    batch = validate_and_serialize_stage1_rows(
        [first, no_fill, skip, ack],
        [*first_risk, *no_fill_risk],
        capital,
        outcomes,
        expected_first_fill_ids={"SYNTH-MARKET|1000000|1001500"},
        source_audit={
            "eligible_entry_decisions": 4,
            "pair_post_only_decisions": 3,
            "entry_skip_decisions": 1,
            "both_acked": 2,
            "ack_failed": 1,
        },
    )
    assert batch.receipt["roster_partition_exact"] is True
    assert batch.receipt["admitted_entries"] == 2
    assert batch.receipt["first_fill_entries"] == 1
    assert batch.receipt["no_first_fill_entries"] == 1
    assert batch.receipt["ack_failed_entries"] == 1
    assert batch.receipt["entry_skip_entries"] == 1
    assert batch.receipt["capital_dollar_seconds"] == "6.45300000"


def test_dynamic_interval_state_is_asof_each_bin_not_frozen_at_entry():
    episode = pair_episode()
    rows = build_stage1_risk_intervals(
        episode,
        {
            Decimal("0"): state(asof=T0, yes_ahead="2", no_ahead="3"),
            Decimal("1000"): state(
                asof=T0 + 999_000_000,
                yes_ahead="0.5",
                no_ahead="1.5",
            ),
        },
    )
    assert [row["elapsed_start_ms"] for row in rows] == [
        Decimal("0"),
        Decimal("1000"),
    ]
    assert rows[0]["yes_same_price_ahead_fp"] == Decimal("2")
    assert rows[1]["yes_same_price_ahead_fp"] == Decimal("0.5")
    assert rows[-1]["event_yes_first"] == 1


def test_active_grid_empty_public_touch_is_explicit_nullable_bundle():
    episode = pair_episode()
    missing_touch = state(
        asof=T0 + 900_000_000,
        yes_ahead="1",
        no_ahead="2",
    )
    missing_touch.update(
        {
            "touch_imbalance": None,
            "spread_e4": None,
            "mid_move_1s_e4": None,
            "mid_move_10s_e4": None,
        }
    )
    rows = build_stage1_risk_intervals(
        episode,
        {
            Decimal("0"): state(
                asof=T0,
                yes_ahead="2",
                no_ahead="3",
            ),
            Decimal("1000"): missing_touch,
        },
    )
    assert rows[1]["spread_e4"] is None
    assert rows[1]["touch_imbalance"] is None
    assert rows[1]["mid_move_1s_e4"] is None
    assert rows[1]["mid_move_10s_e4"] is None
    assert rows[1]["yes_same_price_ahead_fp"] == Decimal("1")
    assert rows[1]["yes_flow_10s_fp"] == Decimal("5")


def test_missing_spread_cannot_keep_stale_touch_dependent_features():
    episode = pair_episode()
    inconsistent = state(
        asof=T0 + 900_000_000,
        yes_ahead="1",
        no_ahead="2",
    )
    inconsistent["spread_e4"] = None
    with pytest.raises(
        EntryStage1ContractError,
        match="spread NULL requires",
    ):
        build_stage1_risk_intervals(
            episode,
            {
                Decimal("0"): state(
                    asof=T0,
                    yes_ahead="2",
                    no_ahead="3",
                ),
                Decimal("1000"): inconsistent,
            },
        )


def test_first_fill_requires_strict_trade_through_and_exact_identity():
    episode = pair_episode()
    rows = build_stage1_risk_intervals(
        episode,
        {
            Decimal("0"): state(asof=T0, yes_ahead="2", no_ahead="3"),
            Decimal("1000"): state(
                asof=T0 + 900_000_000,
                yes_ahead="1",
                no_ahead="2",
            ),
        },
    )
    capital = [build_capital_interval(episode, 0, Decimal("0.99"))]
    outcome = stage1_outcome(episode, strict=False)
    outcome["capital_dollar_seconds"] = Decimal("1.485")
    outcome["peak_episode_capital_usd"] = Decimal("0.99")
    with pytest.raises(EntryStage1ContractError, match="strict trade-through"):
        validate_and_serialize_stage1_rows(
            [episode],
            rows,
            capital,
            [outcome],
            expected_first_fill_ids={"SYNTH-MARKET|1000000|1001500"},
            source_audit={
                "eligible_entry_decisions": 1,
                "pair_post_only_decisions": 1,
                "entry_skip_decisions": 0,
                "both_acked": 1,
                "ack_failed": 0,
            },
        )


def test_no_fill_is_in_denominator_and_must_have_terminal_risk_flag():
    episode = pair_episode(cause="ADMIN_CENSOR_NO_FIRST_FILL")
    episode["censor_reason"] = "ZONE_INVALID_CANCEL_ACK"
    rows = list(
        build_stage1_risk_intervals(
            episode,
            {
                Decimal("0"): state(
                    asof=T0,
                    yes_ahead="2",
                    no_ahead="3",
                ),
                Decimal("1000"): state(
                    asof=T0 + 900_000_000,
                    yes_ahead="1",
                    no_ahead="2",
                ),
            },
        )
    )
    rows[-1] = deepcopy(rows[-1])
    rows[-1]["admin_censor"] = 0
    capital = [build_capital_interval(episode, 0, Decimal("0.99"))]
    outcome = stage1_outcome(episode)
    outcome["capital_dollar_seconds"] = Decimal("1.485")
    outcome["peak_episode_capital_usd"] = Decimal("0.99")
    with pytest.raises(EntryStage1ContractError, match="terminal-cause"):
        validate_and_serialize_stage1_rows(
            [episode],
            rows,
            capital,
            [outcome],
            source_audit={
                "eligible_entry_decisions": 1,
                "pair_post_only_decisions": 1,
                "entry_skip_decisions": 0,
                "both_acked": 1,
                "ack_failed": 0,
            },
        )


def test_float_money_and_zero_duration_first_fill_fail_closed():
    episode = pair_episode()
    with pytest.raises(EntryStage1ContractError, match="zero-duration"):
        build_stage1_risk_intervals(
            pair_episode(terminal=T0),
            {Decimal("0"): state(asof=T0, yes_ahead="2", no_ahead="3")},
        )
    with pytest.raises(EntryStage1ContractError, match="float"):
        build_capital_interval(episode, 0, 0.99)


@pytest.mark.parametrize("date", ("2026-07-23", "2026-07-26"))
def test_forbidden_dates_fail_before_any_stage1_materialization(date):
    episode = pair_episode()
    episode["source_date_utc"] = date
    with pytest.raises(EntryStage1ContractError, match="date"):
        validate_and_serialize_stage1_rows(
            [episode],
            [],
            [],
            [],
            source_audit={
                "eligible_entry_decisions": 1,
                "pair_post_only_decisions": 1,
                "entry_skip_decisions": 0,
                "both_acked": 1,
                "ack_failed": 0,
            },
        )


def test_whole_market_day_rollback_rejects_any_data_invalid_marker():
    episode = pair_episode()
    with pytest.raises(EntryStage1ContractError, match="whole market-day"):
        validate_and_serialize_stage1_rows(
            [episode],
            [],
            [],
            [],
            source_audit={
                "eligible_entry_decisions": 1,
                "pair_post_only_decisions": 1,
                "entry_skip_decisions": 0,
                "both_acked": 1,
                "ack_failed": 0,
                "data_invalid_market_days": 1,
            },
        )


def test_validated_batch_inserts_into_base_and_supplemental_ddl():
    episode = pair_episode()
    risk = build_stage1_risk_intervals(
        episode,
        {
            Decimal("0"): state(asof=T0, yes_ahead="2", no_ahead="3"),
            Decimal("1000"): state(
                asof=T0 + 900_000_000,
                yes_ahead="1",
                no_ahead="2",
            ),
        },
    )
    capital = build_capital_interval(episode, 0, Decimal("0.99"))
    outcome = stage1_outcome(episode)
    outcome["capital_dollar_seconds"] = Decimal("1.485")
    outcome["peak_episode_capital_usd"] = Decimal("0.99")
    batch = validate_and_serialize_stage1_rows(
        [episode],
        risk,
        [capital],
        [outcome],
        expected_first_fill_ids={"SYNTH-MARKET|1000000|1001500"},
        source_audit={
            "eligible_entry_decisions": 1,
            "pair_post_only_decisions": 1,
            "entry_skip_decisions": 0,
            "both_acked": 1,
            "ack_failed": 0,
        },
    )
    root = Path(__file__).resolve().parents[1]
    connection = duckdb.connect(":memory:")
    connection.execute(
        (
            root
            / "tmp/crypto_mm_canary_20260726/round4/"
            "round4_two_stage_tables.sql"
        ).read_text()
    )
    connection.execute(
        (
            root
            / "tmp/crypto_mm_canary_20260726/round4/"
            "round4_stage1_entry_supplemental.sql"
        ).read_text()
    )
    for table, rows in batch.as_ddl_rows().items():
        for row in rows:
            columns = tuple(row)
            connection.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [row[name] for name in columns],
            )
    assert connection.execute(
        "SELECT COUNT(*) FROM entry_episode"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT capital_dollar_seconds FROM entry_stage1_outcome"
    ).fetchone()[0] == Decimal("1.48500000")
