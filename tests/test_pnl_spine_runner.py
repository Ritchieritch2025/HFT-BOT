"""End-to-end and adversarial tests for the offline PnL-spine runner."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.research.pnl_spine.contracts import canonical_sha256  # noqa: E402
from tools.research.pnl_spine.experiments import (  # noqa: E402
    FrozenExperimentAdapters,
    NormalizedStateRow,
    RuntimeBindings,
)
from tools.research.pnl_spine.fees import (  # noqa: E402
    FeePrecision,
    FeeRule,
    FeeSchedule,
)
from tools.research.pnl_spine.contracts import LiquidityRole  # noqa: E402
from tools.research.pnl_spine.runner import (  # noqa: E402
    C1_CLASSIFICATION,
    NET_COMPLETE,
    PNL_BLOCKED,
    RunnerContractError,
    run_fixture,
)


FREEZE_PATH = (
    ROOT
    / "Deepresearch V3"
    / "registry"
    / "frozen"
    / "PNL_SPINE_EXPERIMENTS_V1.json"
)
RUNNER_PATH = ROOT / "tools" / "research" / "pnl_spine" / "runner.py"
DATES = ["2026-07-12", "2026-07-15", "2026-07-17"]
NOW = 1_000_000_000_000_000
SECOND = 1_000_000_000
MINUTE = 60 * SECOND
H_A = "a" * 64
H_B = "b" * 64
H_C = "c" * 64
H_D = "d" * 64
H_E = "e" * 64


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def seal(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result.pop("payload_sha256", None)
    result["payload_sha256"] = hashlib.sha256(
        canonical_bytes(result)
    ).hexdigest()
    return result


def freeze() -> dict[str, Any]:
    return json.loads(FREEZE_PATH.read_text(encoding="utf-8"))


def fee_rules() -> list[dict[str, Any]]:
    common: dict[str, Any] = {
        "multiplier_e4": 10_000,
        "precision": "DIRECT_CENTICENT",
        "effective_from_ns": 0,
        "effective_until_ns": None,
        "source_sha256": H_A,
        "market_ticker": None,
        "series_prefix": "KXTEST",
    }
    return [
        {
            **common,
            "rule_id": "test-maker",
            "liquidity_role": "MAKER",
            "rate_e4": 175,
        },
        {
            **common,
            "rule_id": "test-taker",
            "liquidity_role": "TAKER",
            "rate_e4": 700,
        },
    ]


def schedule_sha(rows: list[dict[str, Any]]) -> str:
    rules = tuple(
        FeeRule(
            rule_id=row["rule_id"],
            liquidity_role=LiquidityRole(row["liquidity_role"]),
            rate_e4=row["rate_e4"],
            multiplier_e4=row["multiplier_e4"],
            precision=FeePrecision(row["precision"]),
            effective_from_ns=row["effective_from_ns"],
            effective_until_ns=row["effective_until_ns"],
            source_sha256=row["source_sha256"],
            market_ticker=row["market_ticker"],
            series_prefix=row["series_prefix"],
        )
        for row in rows
    )
    return FeeSchedule(rules).deterministic_sha256


def fee_receipt(rules: list[dict[str, Any]]) -> dict[str, Any]:
    return seal(
        {
            "schema_version": "pnl-spine-fee-facts-receipt-v1",
            "receipt_id": "fee-fixture",
            "status": "VERIFIED_EFFECTIVE_DATED",
            "coverage_dates": DATES,
            "coverage_scope": "ALL_ELIGIBLE_SERIES_EVENTS_AND_ROLES",
            "bindings": {
                "fee_facts_sha256": schedule_sha(rules),
                "maker_fee_formula_id": "maker-v1",
                "taker_fee_formula_id": "taker-v1",
                "account_class": "fixture-direct",
                "target_balance_precision": 4,
                "fee_rounding_accumulator_version": "centicent-v1",
                "rebate_and_event_override_version": "effective-v1",
            },
            "source_sha256": H_B,
            "event_override_history_complete": True,
            "order_rounding_rebate_complete": True,
            "private_fee_precedence": True,
        }
    )


def latency_receipt() -> dict[str, Any]:
    samples = []
    for index, path in enumerate(("PLACE", "CANCEL", "IOC_EXIT")):
        decision = 10_000 + index * 1_000
        samples.append(
            {
                "sample_id": f"latency-{path}",
                "order_id": f"measured-{index}",
                "path": path,
                "decision_ns": decision,
                "sent_ns": decision + 100,
                "acknowledged_ns": decision + 200,
                "effective_ns": decision + 300,
                "source_event_sha256": chr(ord("a") + index) * 64,
            }
        )
    return seal(
        {
            "schema_version": "pnl-spine-measured-latency-receipt-v1",
            "receipt_id": "real-order-latency",
            "measurement_mode": "REAL_ORDER_MEASURED",
            "clock_id": "CLOCK_MONOTONIC_RAW",
            "measured_on": "production-order-path",
            "created_at_ns": 100_000,
            "environment_fingerprint_sha256": H_D,
            "source_sha256": H_A,
            "samples": samples,
        }
    )


def release_receipt() -> dict[str, Any]:
    rows = []
    for index, date in enumerate(DATES):
        rows.append(
            {
                "date": date,
                "release_id": f"{date}__v3ref__exact-{index}",
                "manifest_version_id": f"version-{index}",
                "manifest_sha256": H_A,
                "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
                "l2_quality_state": "PASS",
                "l2_quality_receipt_sha256": H_B,
                "l2_row_count": 10_000 + index,
                "l2_objects_sha256": H_C,
            }
        )
    return seal(
        {
            "schema_version": "pnl-spine-exact-release-dq-receipt-v1",
            "receipt_id": "release-dq",
            "cohort_id": "L2-CLEAN-ENGINEERING-20260712-15-17",
            "dates": rows,
            "root_start_bindings": {
                "root_map_version": "root-v1",
                "scheduled_start_source": "catalog-asof-v1",
                "scheduled_start_asof_semantics": (
                    "AVAILABLE_AT_OR_BEFORE_DECISION"
                ),
                "tick_table_version": "tick-v1",
                "lifecycle_version": "lifecycle-v1",
            },
            "root_map_sha256": H_D,
            "scheduled_start_sha256": H_C,
            "source_sha256": H_B,
        }
    )


def terminal_receipt(path_count: int) -> dict[str, Any]:
    return seal(
        {
            "schema_version": "pnl-spine-terminal-coverage-receipt-v1",
            "receipt_id": "terminal-fixture",
            "status": "COMPLETE",
            "coverage_dates": DATES,
            "closure_scope": "EVERY_STRATEGY_AND_BASELINE_PATH",
            "bindings": {
                "terminal_contract_version": "terminal-v1",
                "legal_exception_state_table_sha256": H_A,
                "ioc_round_cap": 8,
                "ioc_time_cap_ms": 5_000,
                "residual_policy_version": "zero-residual-v1",
                "realized_outcome_requirement": "FINALIZED_EXACT_PAYOUT",
            },
            "path_count": path_count,
            "closed_path_count": path_count,
            "unresolved_path_count": 0,
            "residual_quantity_e4": 0,
            "all_paths_have_exit_or_final_settlement": True,
            "append_only_lifecycle_complete": True,
            "source_sha256": H_D,
        }
    )


def releases() -> list[dict[str, Any]]:
    return [
        {
            "release_id": f"{date}__v3ref__exact-{index}",
            "date": date,
            "manifest_sha256": H_A,
            "manifest_version_id": f"version-{index}",
            "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
            "objects": [
                {
                    "logical_key": f"l2/date={date}/book.parquet",
                    "version_id": f"object-version-{index}",
                    "sha256": H_C,
                    "size_bytes": 100 + index,
                    "channel": "L2",
                    "date": date,
                }
            ],
        }
        for index, date in enumerate(DATES)
    ]


def a01_row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "row_id": "a01-row",
        "root_event_id": "root-1",
        "market_ticker": "KXTEST-MARKET",
        "sport": "Tennis",
        "decision_ts_ns": NOW,
        "features_asof_ns": NOW - SECOND,
        "book_observed_at_ns": NOW - 250_000_000,
        "tick_size_e4": 100,
        "scheduled_start_ts_ns": NOW + 60 * MINUTE,
        "scheduled_start_asof_ns": NOW - SECOND,
        "best_yes_bid_e4": 4_000,
        "best_yes_ask_e4": 4_400,
        "state_started_at_ns": NOW - 5 * SECOND,
        "warmup_started_at_ns": NOW - 120 * SECOND,
        "net_capture_margin_ticks": 3,
        "activity_gate_passed": True,
        "toxicity_gate_passed": True,
        "adverse_width_gate_passed": True,
    }
    row.update(changes)
    return row


def complete_bindings() -> RuntimeBindings:
    return RuntimeBindings(
        fee_facts_sha256=H_A,
        latency_receipt_sha256=H_B,
        root_map_sha256=H_C,
        scheduled_start_source_sha256=H_D,
        risk_policy_sha256=H_E,
        terminal_contract_sha256="1" * 64,
        strict_fill_evidence_sha256="2" * 64,
        card_parameter_artifact_sha256="3" * 64,
    )


def a01_intent_ids(row: dict[str, Any]) -> tuple[str, ...]:
    decision = FrozenExperimentAdapters(freeze()).evaluate_a01(
        NormalizedStateRow(**row),
        complete_bindings(),
    )
    return tuple(intent.intent_id for intent in decision.intents)


def base_fixture(
    *,
    row: dict[str, Any] | None = None,
    path_count: int = 3,
    with_trades: bool = True,
) -> dict[str, Any]:
    row = a01_row() if row is None else row
    rules = fee_rules()
    terminal = terminal_receipt(path_count)
    latency = latency_receipt()
    release = release_receipt()
    intent_ids = a01_intent_ids(row)
    activation_us = (row["decision_ts_ns"] + 300 + 999) // 1_000
    exit_decision_ns = row["decision_ts_ns"] + 10 * SECOND
    exit_snapshot_us = exit_decision_ns // 1_000
    trades: list[dict[str, Any]] = []
    if with_trades:
        trades = [
            {
                "trade_id": "trade-buy-yes",
                "market_ticker": row["market_ticker"],
                "timestamp_us": activation_us + 1,
                "yes_price_e4": 3_800,
                "quantity_e4": 10_000,
                "taker_side": "NO",
                "source_sha256": H_A,
            },
            {
                "trade_id": "trade-buy-no",
                "market_ticker": row["market_ticker"],
                "timestamp_us": activation_us + 2,
                "yes_price_e4": 4_600,
                "quantity_e4": 10_000,
                "taker_side": "YES",
                "source_sha256": H_B,
            },
        ]
    snapshots = [
        {
            "snapshot_id": "exit-yes",
            "market_ticker": row["market_ticker"],
            "receive_timestamp_us": exit_snapshot_us,
            "yes_bids": [
                {"yes_price_e4": 4_200, "quantity_e4": 10_000}
            ],
            "yes_asks": [
                {"yes_price_e4": 4_300, "quantity_e4": 10_000}
            ],
            "book_valid": True,
            "gap_free": True,
            "source_sha256": H_C,
        },
        {
            "snapshot_id": "exit-no",
            "market_ticker": row["market_ticker"],
            "receive_timestamp_us": exit_snapshot_us,
            "yes_bids": [
                {"yes_price_e4": 5_000, "quantity_e4": 10_000}
            ],
            "yes_asks": [
                {"yes_price_e4": 5_200, "quantity_e4": 10_000}
            ],
            "book_valid": True,
            "gap_free": True,
            "source_sha256": H_D,
        },
    ]
    closures: list[dict[str, Any]] = []
    if len(intent_ids) == 2:
        intent_buy, intent_sell = intent_ids
        closures = [
            {
                "intent_id": intent_buy,
                "exit_snapshot_id": "exit-yes",
                "exit_decision_ts_ns": exit_decision_ns,
                "exit_limit_price_e4": 1,
                "maximum_snapshot_age_us": 10,
                "settlement_id": None,
            },
            {
                "intent_id": intent_sell,
                "exit_snapshot_id": "exit-no",
                "exit_decision_ts_ns": exit_decision_ns,
                "exit_limit_price_e4": 1,
                "maximum_snapshot_age_us": 10,
                "settlement_id": None,
            },
        ]
    fixture: dict[str, Any] = {
        "schema_version": "pnl-spine-run-fixture-v1",
        "run_id": "fixture-run-1",
        "experiment_id": "A01-SPREAD-CAPTURE",
        "code_sha256": H_E,
        "frozen_experiment": freeze(),
        "card_parameter_artifact_sha256": "3" * 64,
        "preflight_inputs": {
            "fee_facts": fee_receipt(rules),
            "measured_latency": latency,
            "release_dq": release,
            "terminal_coverage": terminal,
        },
        "provenance": {
            "risk_policy_sha256": H_E,
            "terminal_contract_sha256": canonical_sha256(terminal),
            "releases": releases(),
        },
        "fee_rules": rules,
        "rows": [row],
        "public_trades": trades,
        "exit_snapshots": snapshots,
        "closures": closures,
        "settlements": [],
    }
    return fixture


def blocker_codes(receipt: dict[str, Any]) -> set[str]:
    return {row["code"] for row in receipt["blockers"]}


def test_a01_exact_fills_fees_latency_and_ioc_exits_complete_end_to_end():
    receipt = run_fixture(base_fixture())

    assert receipt["state"] == NET_COMPLETE
    assert receipt["blockers"] == []
    assert len(receipt["path_rows"]) == 3
    baseline = [
        row for row in receipt["path_rows"] if row["kind"] == "BASELINE"
    ]
    strategy = [
        row for row in receipt["path_rows"] if row["kind"] == "STRATEGY"
    ]
    assert len(baseline) == 1
    assert baseline[0]["result"]["net_pnl_e6"] == 0
    assert baseline[0]["result"]["closure_state"] == "CLOSED_NO_POSITION"
    assert len(strategy) == 2
    assert {
        row["result"]["closure_state"] for row in strategy
    } == {"CLOSED_BY_EXIT"}
    assert receipt["totals"]["gross_pnl_e6"] == -40_000
    assert receipt["totals"]["fee_cost_e6"] > 0
    assert receipt["totals"]["net_pnl_e6"] == (
        receipt["totals"]["gross_pnl_e6"]
        - receipt["totals"]["fee_cost_e6"]
    )
    assert receipt["conservation"] == {
        "public_source_quantity_e4": 20_000,
        "public_consumed_quantity_e4": 20_000,
        "entry_filled_quantity_e4": 20_000,
        "exit_filled_quantity_e4": 20_000,
        "residual_quantity_e4": 0,
    }
    assert receipt["c1_prior_artifact_classification"] == C1_CLASSIFICATION


@pytest.mark.parametrize(
    ("row", "with_trades", "path_count", "expected_reason"),
    [
        (
            a01_row(best_yes_ask_e4=4_300),
            False,
            2,
            "ABSTAIN_A01_SPREAD_BELOW_4_TICKS",
        ),
        (
            a01_row(),
            False,
            3,
            "TRIGGERED_NO_STRICT_FILL",
        ),
    ],
)
def test_abstain_and_triggered_no_fill_strategy_zeros_are_retained(
    row: dict[str, Any],
    with_trades: bool,
    path_count: int,
    expected_reason: str,
):
    receipt = run_fixture(
        base_fixture(
            row=row,
            path_count=path_count,
            with_trades=with_trades,
        )
    )

    assert receipt["state"] == NET_COMPLETE
    assert len(receipt["path_rows"]) == path_count
    assert all(
        path["result"]["net_pnl_e6"] == 0
        for path in receipt["path_rows"]
    )
    strategy = [
        path
        for path in receipt["path_rows"]
        if path["kind"] == "STRATEGY"
    ]
    assert any(expected_reason in row["reason_codes"] for row in strategy)


def test_missing_real_ioc_exit_latency_blocks_net_pnl_not_defaults_to_zero():
    fixture = base_fixture()
    latency = fixture["preflight_inputs"]["measured_latency"]
    latency["samples"] = [
        row for row in latency["samples"] if row["path"] != "IOC_EXIT"
    ]
    fixture["preflight_inputs"]["measured_latency"] = seal(latency)

    receipt = run_fixture(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert receipt["totals"] is None
    assert "LATENCY_IOC_EXIT_SAMPLES_MISSING" in blocker_codes(receipt)
    assert "NET_PREFLIGHT_NOT_READY" in blocker_codes(receipt)


@pytest.mark.parametrize(
    ("input_name", "expected_code"),
    [
        ("fee_facts", "FEE_FACTS_RECEIPT_MISSING"),
        ("measured_latency", "MEASURED_LATENCY_RECEIPT_MISSING"),
    ],
)
def test_absent_fee_or_latency_receipt_emits_blocked_receipt(
    input_name: str,
    expected_code: str,
):
    fixture = base_fixture()
    fixture["preflight_inputs"][input_name] = None

    receipt = run_fixture(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert receipt["totals"] is None
    assert expected_code in blocker_codes(receipt)


def test_partial_ioc_exit_leaves_residual_and_blocks_complete_pnl():
    fixture = base_fixture()
    fixture["exit_snapshots"][0]["yes_bids"][0]["quantity_e4"] = 5_000

    receipt = run_fixture(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert receipt["totals"] is None
    assert receipt["conservation"]["residual_quantity_e4"] == 5_000
    assert "RESIDUAL_POSITION_OPEN" in blocker_codes(receipt)
    assert "TERMINAL_RESIDUAL_RUNTIME_MISMATCH" in blocker_codes(receipt)


def test_partial_ioc_plus_final_exact_settlement_closes_every_residual():
    fixture = base_fixture()
    fixture["exit_snapshots"][0]["yes_bids"][0]["quantity_e4"] = 5_000
    fixture["closures"][0]["settlement_id"] = "settlement-final"
    fixture["settlements"] = [
        {
            "settlement_id": "settlement-final",
            "market_ticker": "KXTEST-MARKET",
            "status": "FINALIZED",
            "finalized": True,
            "yes_settlement_value_e4": 10_000,
            "observed_at_ns": NOW + 20 * SECOND,
            "revision": 1,
            "source_sha256": H_D,
        }
    ]

    receipt = run_fixture(fixture)

    assert receipt["state"] == NET_COMPLETE
    assert receipt["conservation"]["residual_quantity_e4"] == 0
    strategy = [
        row
        for row in receipt["path_rows"]
        if row["kind"] == "STRATEGY"
    ]
    assert {
        row["result"]["closure_state"] for row in strategy
    } == {"CLOSED_BY_EXIT", "CLOSED_BY_SETTLEMENT"}


def test_nonfinal_settlement_is_censored_and_cannot_close_a_position():
    fixture = base_fixture()
    intent_id = fixture["closures"][0]["intent_id"]
    fixture["closures"][0] = {
        "intent_id": intent_id,
        "exit_snapshot_id": None,
        "exit_decision_ts_ns": None,
        "exit_limit_price_e4": None,
        "maximum_snapshot_age_us": None,
        "settlement_id": "settlement-provisional",
    }
    fixture["settlements"] = [
        {
            "settlement_id": "settlement-provisional",
            "market_ticker": "KXTEST-MARKET",
            "status": "PROVISIONAL",
            "finalized": False,
            "yes_settlement_value_e4": None,
            "observed_at_ns": NOW + 20 * SECOND,
            "revision": 1,
            "source_sha256": H_D,
        }
    ]

    receipt = run_fixture(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert "RESIDUAL_POSITION_OPEN" in blocker_codes(receipt)


def test_fee_schedule_hole_blocks_path_even_when_receipt_claims_coverage():
    fixture = base_fixture()
    fixture["fee_rules"] = [
        row
        for row in fixture["fee_rules"]
        if row["liquidity_role"] != "MAKER"
    ]
    fixture["preflight_inputs"]["fee_facts"] = fee_receipt(
        fixture["fee_rules"]
    )
    fixture["provenance"]["terminal_contract_sha256"] = canonical_sha256(
        fixture["preflight_inputs"]["terminal_coverage"]
    )

    receipt = run_fixture(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert (
        "ENTRY_LEDGER_FAILED" in blocker_codes(receipt)
        or "PATH_FINALIZATION_FAILED" in blocker_codes(receipt)
    )
    assert receipt["totals"] is None


def test_b09_untrained_is_retained_and_explicitly_blocked():
    fixture = base_fixture(path_count=2, with_trades=False)
    fixture["experiment_id"] = "B09-LISTING-TO-START-DRIFT"
    fixture["rows"] = [
        {
            **a01_row(),
            "row_id": "b09-row",
            "listing_age_bucket": "EARLY",
            "scheduled_phase": "PRESTART",
        }
    ]
    fixture["closures"] = []

    receipt = run_fixture(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert "BLOCKED_PARAMETER_TRAINING" in blocker_codes(receipt)
    assert any(
        row["kind"] == "BASELINE"
        and row["result"]["net_pnl_e6"] == 0
        for row in receipt["path_rows"]
    )
    assert any(
        row["kind"] == "STRATEGY" and row["state"] == PNL_BLOCKED
        for row in receipt["path_rows"]
    )


def test_provenance_manifest_drift_blocks_promotion():
    fixture = base_fixture()
    fixture["provenance"]["releases"][0]["manifest_version_id"] = (
        "different-version"
    )

    receipt = run_fixture(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert "PROVENANCE_BINDING_INVALID" in blocker_codes(receipt)
    assert receipt["run_binding_sha256"] is None


def test_runner_rejects_unknown_fixture_fields_and_never_interprets_midpoint():
    fixture = base_fixture()
    fixture["midpoint_exit_price"] = 5_000

    with pytest.raises(RunnerContractError, match="unknown fields"):
        run_fixture(fixture)


def test_cli_emits_canonical_receipt_and_nonzero_on_block(tmp_path: Path):
    fixture = base_fixture()
    fixture["preflight_inputs"]["measured_latency"]["measurement_mode"] = (
        "SCENARIO_ASSUMPTION"
    )
    fixture["preflight_inputs"]["measured_latency"] = seal(
        fixture["preflight_inputs"]["measured_latency"]
    )
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(fixture, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.research.pnl_spine.runner",
            "--fixture",
            str(fixture_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    receipt = json.loads(completed.stdout)
    assert receipt["state"] == PNL_BLOCKED
    assert completed.stdout == json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
