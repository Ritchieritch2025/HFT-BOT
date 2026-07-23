"""End-to-end and adversarial tests for the offline PnL-spine runner."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
from datetime import datetime, timezone

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.research.pnl_spine.contracts import (  # noqa: E402
    CashFlow,
    CashFlowKind,
    LiquidityRole,
    canonical_sha256,
)
from tools.research.pnl_spine.experiments import (  # noqa: E402
    FrozenExperimentAdapters,
    NormalizedStateRow,
    RuntimeBindings,
)
from tools.research.pnl_spine.fills import (  # noqa: E402
    FillBatch,
    FillSlice,
    OrderAction,
    OutcomeSide,
)
from tools.research.pnl_spine.risk import RiskLimits  # noqa: E402
from tools.research.pnl_spine.runner import (  # noqa: E402
    C1_CLASSIFICATION,
    LINEAGE_RECEIPT_SCHEMA,
    LINEAGE_RECORD_SCHEMA_SHA256,
    NET_COMPLETE,
    PNL_BLOCKED,
    RunnerContractError,
    _path_id,
    _replay_risk_lifecycle,
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
NOW = int(
    datetime(
        2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc
    ).timestamp()
) * 1_000_000_000
SECOND = 1_000_000_000
MINUTE = 60 * SECOND
H_A = "a" * 64
H_B = "b" * 64
H_C = "c" * 64
H_D = "d" * 64
H_E = "e" * 64
H_F = "f" * 64
H_0 = "0" * 64


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


def fee_authority() -> dict[str, Any]:
    private_aggregate = {
        "actual_fee_field": "fee_cost",
        "fill_count": 2,
        "maker_fill_count": 1,
        "source_private_fill_receipt_sha256": H_E,
        "taker_fill_count": 1,
        "total_actual_fee_cost_e6": 10_000,
    }
    return {
        "schema_version": "pnl_fee_facts_v1",
        "account": {
            "balance_precision": "DIRECT_CENTICENT",
            "source_sha256": H_D,
        },
        "formula": {
            "effective_from_ns": int(
                datetime(
                    2026, 7, 7, tzinfo=timezone.utc
                ).timestamp()
            )
            * SECOND,
            "effective_until_ns": None,
            "taker_rate_e4": 700,
            "maker_rate_e4": 175,
            "default_taker_multiplier_e4": 10_000,
            "default_maker_multiplier_e4": 0,
        },
        "source_bindings": {
            "fee_schedule_pdf_sha256": H_B,
            "series_history_snapshot_sha256": H_C,
            "series_history_captured_at_ns": int(
                datetime(
                    2026, 7, 23, tzinfo=timezone.utc
                ).timestamp()
            )
            * SECOND,
            "series_history_includes_historical": True,
            "event_history_snapshot_sha256": H_D,
            "private_actual_fee_aggregate_receipt_sha256": (
                canonical_sha256(private_aggregate)
            ),
        },
        "private_actual_fee_aggregate": private_aggregate,
        "series_changes": [],
        "event_assertions": [
            {
                "assertion_id": "event-no-waiver",
                "event_ticker": "KXTEST-EVENT",
                "effective_from_ns": int(
                    datetime(
                        2026, 7, 7, tzinfo=timezone.utc
                    ).timestamp()
                )
                * SECOND,
                "effective_until_ns": None,
                "waiver_state": "NO_WAIVER",
                "fee_type_override": None,
                "multiplier_e4_override": None,
                "source_sha256": H_D,
            }
        ],
    }


def fee_receipt(authority: dict[str, Any]) -> dict[str, Any]:
    return seal(
        {
            "schema_version": "pnl-spine-fee-facts-receipt-v1",
            "receipt_id": "fee-fixture",
            "status": "VERIFIED_EFFECTIVE_DATED",
            "coverage_dates": DATES,
            "coverage_scope": "ALL_ELIGIBLE_SERIES_EVENTS_AND_ROLES",
            "bindings": {
                "fee_facts_sha256": canonical_sha256(authority),
                "maker_fee_formula_id": (
                    "OFFICIAL_QUADRATIC_MAKER_0.0175_C_P_1MP"
                ),
                "taker_fee_formula_id": (
                    "OFFICIAL_QUADRATIC_TAKER_0.07_C_P_1MP"
                ),
                "account_class": "DIRECT_MEMBER",
                "target_balance_precision": 4,
                "fee_rounding_accumulator_version": (
                    "OFFICIAL_PER_ORDER_ACCUMULATOR_CENTICENT_V1"
                ),
                "rebate_and_event_override_version": (
                    "OFFICIAL_EFFECTIVE_DATED_SERIES_EVENT_WAIVER_V1"
                ),
            },
            "source_sha256": authority["source_bindings"][
                "fee_schedule_pdf_sha256"
            ],
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
    result: list[dict[str, Any]] = []
    for index, date in enumerate(DATES):
        objects = [
            {
                "logical_key": f"l2/date={date}/book-primary.parquet",
                "version_id": f"l2-primary-version-{index}",
                "sha256": H_C,
                "size_bytes": 100 + index,
                "channel": "L2",
                "date": date,
            },
            {
                "logical_key": f"l2/date={date}/book-secondary.parquet",
                "version_id": f"l2-secondary-version-{index}",
                "sha256": H_D,
                "size_bytes": 200 + index,
                "channel": "L2",
                "date": date,
            },
            {
                "logical_key": f"trades/date={date}/trades-a.parquet",
                "version_id": f"trades-a-version-{index}",
                "sha256": H_A,
                "size_bytes": 300 + index,
                "channel": "TRADES",
                "date": date,
            },
            {
                "logical_key": f"trades/date={date}/trades-b.parquet",
                "version_id": f"trades-b-version-{index}",
                "sha256": H_B,
                "size_bytes": 400 + index,
                "channel": "TRADES",
                "date": date,
            },
            {
                "logical_key": f"settlement/date={date}/final.json",
                "version_id": f"settlement-version-{index}",
                "sha256": H_D,
                "size_bytes": 500 + index,
                "channel": "SETTLEMENT",
                "date": date,
            },
        ]
        result.append({
            "release_id": f"{date}__v3ref__exact-{index}",
            "date": date,
            "manifest_sha256": H_A,
            "manifest_version_id": f"version-{index}",
            "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
            "objects": objects,
        })
    return result


def risk_policy(**changes: Any) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "schema_version": "pnl-spine-risk-policy-v1",
        "max_market_e6": 10_000_000,
        "max_event_e6": 10_000_000,
        "max_factor_e6": 10_000_000,
        "max_total_e6": 20_000_000,
        "max_daily_loss_e6": 5_000_000,
    }
    policy.update(changes)
    policy["policy_sha256"] = canonical_sha256(policy)
    return policy


def a01_row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "row_id": "a01-row",
        "root_event_id": "KXTEST-EVENT",
        "market_ticker": "KXTEST-EVENT-MARKET",
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


def a11_row(**changes: Any) -> dict[str, Any]:
    onset = NOW - 30 * SECOND
    row: dict[str, Any] = {
        "row_id": "a11-row",
        "root_event_id": "KXTEST-EVENT",
        "market_ticker": "KXTEST-EVENT-MARKET",
        "sport": "Tennis",
        "decision_ts_ns": NOW,
        "features_asof_ns": NOW - SECOND,
        "book_observed_at_ns": NOW - SECOND,
        "tick_size_e4": 100,
        "scheduled_start_ts_ns": NOW + 60 * MINUTE,
        "scheduled_start_asof_ns": NOW - SECOND,
        "best_yes_bid_e4": 4_000,
        "best_yes_ask_e4": None,
        "state_started_at_ns": onset,
        "reference_mid_onset_e4": 4_200,
        "reference_mid_onset_observed_at_ns": onset - SECOND,
        "surviving_side_onset_e4": 4_000,
        "update_count_60s": 5,
        "trade_count_300s": 1,
        "activity_burst": False,
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


def a11_intent_ids(row: dict[str, Any]) -> tuple[str, ...]:
    decision = FrozenExperimentAdapters(freeze()).evaluate_a11(
        NormalizedStateRow(**row),
        complete_bindings(),
    )
    return tuple(intent.intent_id for intent in decision.intents)


def refresh_evidence_bindings(fixture: dict[str, Any]) -> None:
    releases_by_date = {
        release["date"]: release
        for release in fixture["provenance"]["releases"]
    }
    records: list[
        tuple[str, str, dict[str, Any], str, str, str]
    ] = []
    for row in fixture["rows"]:
        date = datetime.fromtimestamp(
            row["decision_ts_ns"] // SECOND,
            tz=timezone.utc,
        ).date().isoformat()
        records.append(
            (
                "NORMALIZED_ROW",
                row["row_id"],
                row,
                date,
                "L2",
                H_C,
            )
        )
    for trade in fixture["public_trades"]:
        date = datetime.fromtimestamp(
            trade["timestamp_us"] // 1_000_000,
            tz=timezone.utc,
        ).date().isoformat()
        records.append(
            (
                "PUBLIC_TRADE",
                trade["trade_id"],
                trade,
                date,
                "TRADES",
                trade["source_sha256"],
            )
        )
    for snapshot in fixture["exit_snapshots"]:
        date = datetime.fromtimestamp(
            snapshot["receive_timestamp_us"] // 1_000_000,
            tz=timezone.utc,
        ).date().isoformat()
        records.append(
            (
                "L2_SNAPSHOT",
                snapshot["snapshot_id"],
                snapshot,
                date,
                "L2",
                snapshot["source_sha256"],
            )
        )
    for settlement in fixture["settlements"]:
        date = datetime.fromtimestamp(
            settlement["observed_at_ns"] // SECOND,
            tz=timezone.utc,
        ).date().isoformat()
        records.append(
            (
                "SETTLEMENT",
                settlement["settlement_id"],
                settlement,
                date,
                "SETTLEMENT",
                settlement["source_sha256"],
            )
        )

    bindings: list[dict[str, Any]] = []
    for kind, record_id, record, date, channel, source_sha in records:
        release = releases_by_date[date]
        matches = [
            obj
            for obj in release["objects"]
            if obj["channel"] == channel and obj["sha256"] == source_sha
        ]
        assert len(matches) == 1
        source = matches[0]
        bindings.append(
            {
                "kind": kind,
                "record_id": record_id,
                "record_sha256": canonical_sha256(record),
                "release_id": release["release_id"],
                "source_object_logical_key": source["logical_key"],
                "source_object_version_id": source["version_id"],
                "source_object_sha256": source["sha256"],
            }
        )
    fixture["evidence_bindings"] = bindings


def lineage_for(fixture: dict[str, Any]) -> dict[str, Any]:
    release_by_id = {
        release["release_id"]: release
        for release in fixture["provenance"]["releases"]
    }
    record_groups = {
        "NORMALIZED_ROW": (
            fixture["rows"],
            "row_id",
        ),
        "PUBLIC_TRADE": (
            fixture["public_trades"],
            "trade_id",
        ),
        "L2_SNAPSHOT": (
            fixture["exit_snapshots"],
            "snapshot_id",
        ),
        "SETTLEMENT": (
            fixture["settlements"],
            "settlement_id",
        ),
    }
    runtime_records = {
        (kind, row[id_field]): row
        for kind, (rows, id_field) in record_groups.items()
        for row in rows
    }
    object_reads: dict[
        tuple[str, str, str], dict[str, Any]
    ] = {}
    records: list[dict[str, Any]] = []
    for binding in sorted(
        fixture["evidence_bindings"],
        key=lambda row: (row["kind"], row["record_id"]),
    ):
        release = release_by_id[binding["release_id"]]
        source = next(
            row
            for row in release["objects"]
            if (
                row["logical_key"]
                == binding["source_object_logical_key"]
                and row["version_id"]
                == binding["source_object_version_id"]
            )
        )
        object_key = (
            release["release_id"],
            source["logical_key"],
            source["version_id"],
        )
        object_reads[object_key] = {
            "release_id": release["release_id"],
            "date": source["date"],
            "logical_key": source["logical_key"],
            "version_id": source["version_id"],
            "channel": source["channel"],
            "source_object_sha256": source["sha256"],
            "size_bytes": source["size_bytes"],
            "bytes_verified": source["size_bytes"],
        }
        key = (binding["kind"], binding["record_id"])
        record = runtime_records[key]
        member = {
            "release_id": release["release_id"],
            "date": source["date"],
            "logical_key": source["logical_key"],
            "version_id": source["version_id"],
            "channel": source["channel"],
            "source_object_sha256": source["sha256"],
            "size_bytes": source["size_bytes"],
            "locator_schema": "fixture-record-locator-v1",
            "locator": f"{binding['kind']}/{binding['record_id']}",
            "source_record_sha256": canonical_sha256(record),
        }
        members = [member]
        records.append(
            {
                "kind": binding["kind"],
                "record_id": binding["record_id"],
                "record_sha256": canonical_sha256(record),
                "record_date_utc": source["date"],
                "mode": (
                    "DERIVED"
                    if binding["kind"]
                    in {"NORMALIZED_ROW", "L2_SNAPSHOT"}
                    else "DIRECT"
                ),
                "source_members": members,
                "transform_code_sha256": H_F,
                "transform_config_sha256": H_0,
                "input_set_sha256": canonical_sha256(members),
            }
        )
    records.sort(key=lambda row: (row["kind"], row["record_id"]))
    payload = {
        "schema_version": LINEAGE_RECEIPT_SCHEMA,
        "run_id": fixture["run_id"],
        "release_set_sha256": canonical_sha256(
            fixture["provenance"]["releases"]
        ),
        "extractor_code_sha256": H_F,
        "extractor_config_sha256": H_0,
        "record_schema_sha256": LINEAGE_RECORD_SCHEMA_SHA256,
        "object_reads": [
            object_reads[key] for key in sorted(object_reads)
        ],
        "records": records,
        "records_sha256": canonical_sha256(records),
    }
    return seal(payload)


def reseal_lineage(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result["records_sha256"] = canonical_sha256(result["records"])
    return seal(result)


def authority_for(
    fixture: dict[str, Any],
    lineage: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "pnl-spine-trusted-authority-v2",
        "code_sha256": fixture["code_sha256"],
        "fee_facts_sha256": canonical_sha256(
            fixture["fee_facts_authority"]
        ),
        "fee_contexts_sha256": canonical_sha256(
            fixture["fee_contexts"]
        ),
        "fee_receipt_sha256": canonical_sha256(
            fixture["preflight_inputs"]["fee_facts"]
        ),
        "measured_latency_receipt_sha256": canonical_sha256(
            fixture["preflight_inputs"]["measured_latency"]
        ),
        "release_set_sha256": canonical_sha256(
            fixture["provenance"]["releases"]
        ),
        "release_dq_receipt_sha256": canonical_sha256(
            fixture["preflight_inputs"]["release_dq"]
        ),
        "evidence_manifest_sha256": canonical_sha256(
            fixture["evidence_bindings"]
        ),
        "closure_manifest_sha256": canonical_sha256(
            fixture["closures"]
        ),
        "risk_policy_sha256": fixture["risk_policy"]["policy_sha256"],
        "terminal_receipt_sha256": canonical_sha256(
            fixture["preflight_inputs"]["terminal_coverage"]
        ),
        "lineage_receipt_sha256": canonical_sha256(lineage),
        "extractor_code_sha256": lineage["extractor_code_sha256"],
        "extractor_config_sha256": (
            lineage["extractor_config_sha256"]
        ),
    }


def execute(fixture: dict[str, Any]) -> dict[str, Any]:
    lineage = lineage_for(fixture)
    return execute_with_lineage(fixture, lineage)


def execute_with_lineage(
    fixture: dict[str, Any],
    lineage: dict[str, Any],
    *,
    expected_lineage_sha256: str | None = None,
) -> dict[str, Any]:
    authority = authority_for(fixture, lineage)
    return run_fixture(
        fixture,
        trusted_authority=authority,
        expected_trusted_authority_sha256=canonical_sha256(authority),
        trusted_lineage_receipt=lineage,
        expected_trusted_lineage_receipt_sha256=(
            expected_lineage_sha256
            if expected_lineage_sha256 is not None
            else canonical_sha256(lineage)
        ),
    )


def base_fixture(
    *,
    row: dict[str, Any] | None = None,
    path_count: int = 3,
    with_trades: bool = True,
) -> dict[str, Any]:
    row = a01_row() if row is None else row
    authority = fee_authority()
    policy = risk_policy()
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
            "snapshot_id": "exit-book",
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
        }
    ]
    closures: list[dict[str, Any]] = []
    if len(intent_ids) == 2:
        intent_buy, intent_sell = intent_ids
        closures = [
            {
                "intent_id": intent_buy,
                "exit_snapshot_id": "exit-book",
                "exit_decision_ts_ns": exit_decision_ns,
                "exit_limit_price_e4": 1,
                "maximum_snapshot_age_us": 10,
                "settlement_id": None,
            },
            {
                "intent_id": intent_sell,
                "exit_snapshot_id": "exit-book",
                "exit_decision_ts_ns": exit_decision_ns,
                "exit_limit_price_e4": 1,
                "maximum_snapshot_age_us": 10,
                "settlement_id": None,
            },
        ]
    fixture: dict[str, Any] = {
        "schema_version": "pnl-spine-run-fixture-v2",
        "run_id": "fixture-run-1",
        "experiment_id": "A01-SPREAD-CAPTURE",
        "code_sha256": H_E,
        "frozen_experiment": freeze(),
        "card_parameter_artifact_sha256": "3" * 64,
        "preflight_inputs": {
            "fee_facts": fee_receipt(authority),
            "measured_latency": latency,
            "release_dq": release,
            "terminal_coverage": terminal,
        },
        "provenance": {
            "risk_policy_sha256": policy["policy_sha256"],
            "terminal_contract_sha256": canonical_sha256(terminal),
            "releases": releases(),
        },
        "fee_facts_authority": authority,
        "fee_contexts": [
            {
                "market_ticker": row["market_ticker"],
                "series_ticker": "KXTEST",
                "event_ticker": "KXTEST-EVENT",
            }
        ],
        "risk_policy": policy,
        "evidence_bindings": [],
        "rows": [row],
        "public_trades": trades,
        "exit_snapshots": snapshots,
        "closures": closures,
        "settlements": [],
    }
    refresh_evidence_bindings(fixture)
    return fixture


def blocker_codes(receipt: dict[str, Any]) -> set[str]:
    return {row["code"] for row in receipt["blockers"]}


def test_a01_exact_fills_fees_latency_and_ioc_exits_complete_end_to_end():
    fixture = base_fixture()
    lineage = lineage_for(fixture)
    receipt = execute_with_lineage(fixture, lineage)

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
    assert receipt["totals"]["gross_pnl_e6"] == 50_000
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
    assert receipt["trusted_lineage_receipt_sha256"] == canonical_sha256(
        lineage
    )


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
    receipt = execute(
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

    receipt = execute(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert receipt["totals"] is None
    assert "LATENCY_IOC_EXIT_SAMPLES_MISSING" in blocker_codes(receipt)
    assert "NET_PREFLIGHT_NOT_READY" in blocker_codes(receipt)


def test_self_signed_real_latency_replacement_cannot_escape_external_pin():
    fixture = base_fixture()
    lineage = lineage_for(fixture)
    trusted = authority_for(fixture, lineage)
    latency = fixture["preflight_inputs"]["measured_latency"]
    latency["samples"][0]["effective_ns"] += 50_000
    fixture["preflight_inputs"]["measured_latency"] = seal(latency)

    receipt = run_fixture(
        fixture,
        trusted_authority=trusted,
        expected_trusted_authority_sha256=canonical_sha256(trusted),
        trusted_lineage_receipt=lineage,
        expected_trusted_lineage_receipt_sha256=(
            canonical_sha256(lineage)
        ),
    )

    assert receipt["state"] == PNL_BLOCKED
    assert receipt["totals"] is None
    assert "EXTERNAL_AUTHORITY_INVALID" in blocker_codes(receipt)
    assert any(
        "measured_latency_receipt_sha256" in blocker["detail"]
        for blocker in receipt["blockers"]
    )


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

    receipt = execute(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert receipt["totals"] is None
    assert expected_code in blocker_codes(receipt)


def test_partial_ioc_exit_leaves_residual_and_blocks_complete_pnl():
    fixture = base_fixture()
    fixture["exit_snapshots"][0]["yes_bids"][0]["quantity_e4"] = 5_000
    refresh_evidence_bindings(fixture)

    receipt = execute(fixture)

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
            "market_ticker": "KXTEST-EVENT-MARKET",
            "status": "FINALIZED",
            "finalized": True,
            "yes_settlement_value_e4": 10_000,
            "observed_at_ns": NOW + 20 * SECOND,
            "revision": 1,
            "source_sha256": H_D,
        }
    ]
    refresh_evidence_bindings(fixture)

    receipt = execute(fixture)

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
            "market_ticker": "KXTEST-EVENT-MARKET",
            "status": "PROVISIONAL",
            "finalized": False,
            "yes_settlement_value_e4": None,
            "observed_at_ns": NOW + 20 * SECOND,
            "revision": 1,
            "source_sha256": H_D,
        }
    ]
    refresh_evidence_bindings(fixture)

    receipt = execute(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert "RESIDUAL_POSITION_OPEN" in blocker_codes(receipt)


def test_self_signed_zero_fee_authority_cannot_complete_net_pnl():
    fixture = base_fixture()
    lineage = lineage_for(fixture)
    trusted = authority_for(fixture, lineage)
    fixture["fee_facts_authority"]["formula"]["taker_rate_e4"] = 0
    fixture["fee_facts_authority"]["formula"]["maker_rate_e4"] = 0
    fixture["preflight_inputs"]["fee_facts"] = fee_receipt(
        fixture["fee_facts_authority"]
    )

    receipt = run_fixture(
        fixture,
        trusted_authority=trusted,
        expected_trusted_authority_sha256=canonical_sha256(trusted),
        trusted_lineage_receipt=lineage,
        expected_trusted_lineage_receipt_sha256=(
            canonical_sha256(lineage)
        ),
    )

    assert receipt["state"] == PNL_BLOCKED
    assert "FEE_AUTHORITY_INVALID" in blocker_codes(receipt)
    assert "EXTERNAL_AUTHORITY_INVALID" in blocker_codes(receipt)
    assert receipt["totals"] is None


def test_same_fixture_cannot_self_sign_1970_rows_as_2026_release_data():
    fixture = base_fixture()
    row = fixture["rows"][0]
    old_now = 1_000_000_000_000_000
    row.update(
        {
            "decision_ts_ns": old_now,
            "features_asof_ns": old_now - SECOND,
            "book_observed_at_ns": old_now - 250_000_000,
            "scheduled_start_ts_ns": old_now + 60 * MINUTE,
            "scheduled_start_asof_ns": old_now - SECOND,
            "state_started_at_ns": old_now - 5 * SECOND,
            "warmup_started_at_ns": old_now - 120 * SECOND,
        }
    )
    row_binding = next(
        binding
        for binding in fixture["evidence_bindings"]
        if binding["kind"] == "NORMALIZED_ROW"
    )
    row_binding["record_sha256"] = canonical_sha256(row)

    lineage = lineage_for(fixture)
    authority = authority_for(fixture, lineage)
    receipt = run_fixture(
        fixture,
        trusted_authority=authority,
        expected_trusted_authority_sha256=canonical_sha256(authority),
        trusted_lineage_receipt=lineage,
        expected_trusted_lineage_receipt_sha256=(
            canonical_sha256(lineage)
        ),
    )

    assert receipt["state"] == PNL_BLOCKED
    assert "EVIDENCE_AUTHORITY_INVALID" in blocker_codes(receipt)
    assert any(
        "timestamp date escaped exact release" in blocker["detail"]
        for blocker in receipt["blockers"]
    )


def test_resigned_fixture_authority_cannot_forge_exact_object_l2_membership():
    fixture = base_fixture()
    root_pinned_lineage = lineage_for(fixture)
    fixture["exit_snapshots"][0]["yes_bids"][0][
        "yes_price_e4"
    ] = 9_000
    fixture["exit_snapshots"][0]["yes_asks"][0][
        "yes_price_e4"
    ] = 9_100
    refresh_evidence_bindings(fixture)

    # This re-signs every ordinary fixture-derived authority field, including
    # the evidence manifest, while retaining the independently pinned lineage.
    receipt = execute_with_lineage(fixture, root_pinned_lineage)

    assert receipt["state"] == PNL_BLOCKED
    assert receipt["totals"] is None
    assert "RECORD_LINEAGE_AUTHORITY_INVALID" in blocker_codes(receipt)
    assert "EXTERNAL_AUTHORITY_INVALID" not in blocker_codes(receipt)
    assert "EVIDENCE_AUTHORITY_INVALID" not in blocker_codes(receipt)


def test_forged_lineage_receipt_cannot_escape_old_external_root_pin():
    fixture = base_fixture()
    original_lineage = lineage_for(fixture)
    original_pin = canonical_sha256(original_lineage)
    fixture["exit_snapshots"][0]["yes_bids"][0][
        "yes_price_e4"
    ] = 9_000
    fixture["exit_snapshots"][0]["yes_asks"][0][
        "yes_price_e4"
    ] = 9_100
    refresh_evidence_bindings(fixture)
    forged_lineage = lineage_for(fixture)

    receipt = execute_with_lineage(
        fixture,
        forged_lineage,
        expected_lineage_sha256=original_pin,
    )

    assert receipt["state"] == PNL_BLOCKED
    assert receipt["totals"] is None
    assert "RECORD_LINEAGE_AUTHORITY_INVALID" in blocker_codes(receipt)
    assert "EXTERNAL_AUTHORITY_INVALID" not in blocker_codes(receipt)
    assert any(
        "does not match external pin" in blocker["detail"]
        for blocker in receipt["blockers"]
        if blocker["code"] == "RECORD_LINEAGE_AUTHORITY_INVALID"
    )


@pytest.mark.parametrize(
    "attack",
    (
        "MISSING_RECORD",
        "EXTRA_RECORD",
        "DUPLICATE_RECORD",
        "WRONG_OBJECT",
        "WRONG_DATE",
        "WRONG_CHANNEL",
        "RECORD_HASH_DRIFT",
        "TRANSFORM_DRIFT",
        "INPUT_SET_DRIFT",
        "EMPTY_SOURCE_MEMBERS",
        "DUPLICATE_SOURCE_MEMBER",
        "NORMALIZED_ROW_DIRECT",
        "EXTRA_OBJECT_READ",
        "DUPLICATE_OBJECT_READ",
    ),
)
def test_lineage_contract_attacks_fail_closed_even_when_ordinary_authority_resigned(
    attack: str,
):
    fixture = base_fixture()
    lineage = lineage_for(fixture)
    target = next(
        row
        for row in lineage["records"]
        if row["kind"] == "L2_SNAPSHOT"
    )
    if attack == "MISSING_RECORD":
        lineage["records"].remove(target)
    elif attack == "EXTRA_RECORD":
        extra = copy.deepcopy(target)
        extra["record_id"] = "extra-l2-record"
        extra["source_members"][0]["locator"] = (
            "L2_SNAPSHOT/extra-l2-record"
        )
        extra["input_set_sha256"] = canonical_sha256(
            extra["source_members"]
        )
        lineage["records"].append(extra)
    elif attack == "DUPLICATE_RECORD":
        lineage["records"].append(copy.deepcopy(target))
    elif attack == "WRONG_OBJECT":
        release = fixture["provenance"]["releases"][0]
        wrong = next(
            row
            for row in release["objects"]
            if row["channel"] == "L2" and row["sha256"] == H_D
        )
        member = target["source_members"][0]
        member.update(
            {
                "logical_key": wrong["logical_key"],
                "version_id": wrong["version_id"],
                "source_object_sha256": wrong["sha256"],
                "size_bytes": wrong["size_bytes"],
            }
        )
        target["input_set_sha256"] = canonical_sha256(
            target["source_members"]
        )
        lineage["object_reads"].append(
            {
                "release_id": release["release_id"],
                "date": wrong["date"],
                "logical_key": wrong["logical_key"],
                "version_id": wrong["version_id"],
                "channel": wrong["channel"],
                "source_object_sha256": wrong["sha256"],
                "size_bytes": wrong["size_bytes"],
                "bytes_verified": wrong["size_bytes"],
            }
        )
    elif attack == "WRONG_DATE":
        target["record_date_utc"] = "2026-07-15"
    elif attack == "WRONG_CHANNEL":
        target["source_members"][0]["channel"] = "TRADES"
    elif attack == "RECORD_HASH_DRIFT":
        target["record_sha256"] = H_D
    elif attack == "TRANSFORM_DRIFT":
        target["transform_code_sha256"] = H_E
    elif attack == "INPUT_SET_DRIFT":
        target["input_set_sha256"] = H_E
    elif attack == "EMPTY_SOURCE_MEMBERS":
        target["source_members"] = []
        target["input_set_sha256"] = canonical_sha256([])
    elif attack == "DUPLICATE_SOURCE_MEMBER":
        target["source_members"].append(
            copy.deepcopy(target["source_members"][0])
        )
        target["input_set_sha256"] = canonical_sha256(
            target["source_members"]
        )
    elif attack == "NORMALIZED_ROW_DIRECT":
        normalized = next(
            row
            for row in lineage["records"]
            if row["kind"] == "NORMALIZED_ROW"
        )
        normalized["mode"] = "DIRECT"
    elif attack == "EXTRA_OBJECT_READ":
        release = fixture["provenance"]["releases"][0]
        extra = next(
            row
            for row in release["objects"]
            if row["channel"] == "L2" and row["sha256"] == H_D
        )
        lineage["object_reads"].append(
            {
                "release_id": release["release_id"],
                "date": extra["date"],
                "logical_key": extra["logical_key"],
                "version_id": extra["version_id"],
                "channel": extra["channel"],
                "source_object_sha256": extra["sha256"],
                "size_bytes": extra["size_bytes"],
                "bytes_verified": extra["size_bytes"],
            }
        )
    elif attack == "DUPLICATE_OBJECT_READ":
        lineage["object_reads"].append(
            copy.deepcopy(lineage["object_reads"][0])
        )
    else:
        raise AssertionError(f"unhandled lineage attack {attack}")
    lineage["records"].sort(
        key=lambda row: (row["kind"], row["record_id"])
    )
    lineage["object_reads"].sort(
        key=lambda row: (
            row["release_id"],
            row["logical_key"],
            row["version_id"],
        )
    )
    lineage = reseal_lineage(lineage)

    receipt = execute_with_lineage(fixture, lineage)

    assert receipt["state"] == PNL_BLOCKED
    assert receipt["totals"] is None
    assert "RECORD_LINEAGE_AUTHORITY_INVALID" in blocker_codes(receipt)
    assert "EXTERNAL_AUTHORITY_INVALID" not in blocker_codes(receipt)


def test_missing_external_authority_pin_can_never_complete():
    receipt = run_fixture(base_fixture())
    assert receipt["state"] == PNL_BLOCKED
    assert "EXTERNAL_AUTHORITY_INVALID" in blocker_codes(receipt)
    assert "RECORD_LINEAGE_AUTHORITY_INVALID" in blocker_codes(receipt)
    assert receipt["trusted_authority_sha256"] is None
    assert receipt["trusted_lineage_receipt_sha256"] is None


def test_frozen_root_cap_and_risk_ledger_reject_duplicate_root_intents():
    fixture = base_fixture(path_count=6)
    second = a01_row(row_id="a01-row-duplicate-root")
    fixture["rows"].append(second)
    terminal = terminal_receipt(6)
    fixture["preflight_inputs"]["terminal_coverage"] = terminal
    fixture["provenance"]["terminal_contract_sha256"] = canonical_sha256(
        terminal
    )
    refresh_evidence_bindings(fixture)

    receipt = execute(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert "FROZEN_ROOT_CAP_EXCEEDED" in blocker_codes(receipt)
    assert receipt["risk_ledger_sha256"] is not None
    assert any(
        row["reason_codes"] == ["BLOCK_RISK_ADMISSION_FAILED"]
        for row in receipt["path_rows"]
    )


def test_closure_cannot_select_an_older_better_l2_snapshot():
    fixture = base_fixture()
    latest = copy.deepcopy(fixture["exit_snapshots"][0])
    latest["snapshot_id"] = "exit-book-latest"
    latest["receive_timestamp_us"] += 1
    latest["yes_bids"][0]["yes_price_e4"] = 3_000
    latest["yes_asks"][0]["yes_price_e4"] = 7_000
    fixture["exit_snapshots"].append(latest)
    refresh_evidence_bindings(fixture)

    receipt = execute(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert "EXIT_IOC_FAILED" in blocker_codes(receipt)
    assert any(
        "did not select the latest qualified" in blocker["detail"]
        for blocker in receipt["blockers"]
    )


def test_conflicting_final_settlements_for_same_market_fail_globally():
    fixture = base_fixture()
    for closure in fixture["closures"]:
        closure.update(
            {
                "exit_snapshot_id": None,
                "exit_decision_ts_ns": None,
                "exit_limit_price_e4": None,
                "maximum_snapshot_age_us": None,
            }
        )
    fixture["closures"][0]["settlement_id"] = "final-yes"
    fixture["closures"][1]["settlement_id"] = "final-no"
    fixture["settlements"] = [
        {
            "settlement_id": "final-yes",
            "market_ticker": "KXTEST-EVENT-MARKET",
            "status": "FINALIZED",
            "finalized": True,
            "yes_settlement_value_e4": 10_000,
            "observed_at_ns": NOW + 20 * SECOND,
            "revision": 1,
            "source_sha256": H_D,
        },
        {
            "settlement_id": "final-no",
            "market_ticker": "KXTEST-EVENT-MARKET",
            "status": "FINALIZED",
            "finalized": True,
            "yes_settlement_value_e4": 0,
            "observed_at_ns": NOW + 20 * SECOND,
            "revision": 1,
            "source_sha256": H_D,
        },
    ]
    refresh_evidence_bindings(fixture)

    receipt = execute(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert "EVIDENCE_AUTHORITY_INVALID" in blocker_codes(receipt)
    assert any(
        "multiple finalized settlement authorities" in blocker["detail"]
        for blocker in receipt["blockers"]
    )


@pytest.mark.parametrize(
    "conflicting_status",
    ("VOID", "CANCELED", "RETIRED"),
)
def test_all_final_terminal_statuses_share_one_market_authority(
    conflicting_status: str,
):
    fixture = base_fixture()
    for closure in fixture["closures"]:
        closure.update(
            {
                "exit_snapshot_id": None,
                "exit_decision_ts_ns": None,
                "exit_limit_price_e4": None,
                "maximum_snapshot_age_us": None,
            }
        )
    fixture["closures"][0]["settlement_id"] = "terminal-finalized"
    fixture["closures"][1]["settlement_id"] = "terminal-conflict"
    fixture["settlements"] = [
        {
            "settlement_id": "terminal-finalized",
            "market_ticker": "KXTEST-EVENT-MARKET",
            "status": "FINALIZED",
            "finalized": True,
            "yes_settlement_value_e4": 10_000,
            "observed_at_ns": NOW + 20 * SECOND,
            "revision": 1,
            "source_sha256": H_D,
        },
        {
            "settlement_id": "terminal-conflict",
            "market_ticker": "KXTEST-EVENT-MARKET",
            "status": conflicting_status,
            "finalized": True,
            "yes_settlement_value_e4": 0,
            "observed_at_ns": NOW + 20 * SECOND,
            "revision": 1,
            "source_sha256": H_D,
        },
    ]
    refresh_evidence_bindings(fixture)

    receipt = execute(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert receipt["totals"] is None
    assert "EVIDENCE_AUTHORITY_INVALID" in blocker_codes(receipt)
    assert any(
        (
            "only FINALIZED status may be finalized settlement authority"
            in blocker["detail"]
        )
        for blocker in receipt["blockers"]
    )


@pytest.mark.parametrize(
    "nonfinal_status",
    ("PROVISIONAL", "POSTPONED", "VOID", "CANCELED", "RETIRED"),
)
def test_nonfinal_status_cannot_self_declare_finalized_payout(
    nonfinal_status: str,
):
    fixture = base_fixture()
    shared_id = "caller-declared-final"
    for closure in fixture["closures"]:
        closure.update(
            {
                "exit_snapshot_id": None,
                "exit_decision_ts_ns": None,
                "exit_limit_price_e4": None,
                "maximum_snapshot_age_us": None,
                "settlement_id": shared_id,
            }
        )
    fixture["settlements"] = [
        {
            "settlement_id": shared_id,
            "market_ticker": "KXTEST-EVENT-MARKET",
            "status": nonfinal_status,
            "finalized": True,
            "yes_settlement_value_e4": 10_000,
            "observed_at_ns": NOW + 20 * SECOND,
            "revision": 1,
            "source_sha256": H_D,
        }
    ]
    refresh_evidence_bindings(fixture)

    receipt = execute(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert receipt["totals"] is None
    assert "EVIDENCE_AUTHORITY_INVALID" in blocker_codes(receipt)
    assert any(
        (
            "only FINALIZED status may be finalized settlement authority"
            in blocker["detail"]
        )
        for blocker in receipt["blockers"]
    )


def test_risk_replay_orders_equal_timestamp_closure_before_new_fill():
    new_raw = a01_row()
    old_raw = copy.deepcopy(new_raw)
    old_raw.update(
        {
            "row_id": "a01-row-old",
            "root_event_id": "KXTEST-EVENT-OLD",
            "market_ticker": "KXTEST-EVENT-MARKET-OLD",
        }
    )
    for field in (
        "decision_ts_ns",
        "features_asof_ns",
        "book_observed_at_ns",
        "scheduled_start_ts_ns",
        "scheduled_start_asof_ns",
        "state_started_at_ns",
        "warmup_started_at_ns",
    ):
        old_raw[field] -= 10 * SECOND

    adapters = FrozenExperimentAdapters(freeze())
    old_row = NormalizedStateRow(**old_raw)
    new_row = NormalizedStateRow(**new_raw)
    old_decision = adapters.evaluate_a01(old_row, complete_bindings())
    new_decision = adapters.evaluate_a01(new_row, complete_bindings())
    old_intent = old_decision.intents[0]
    new_intent = new_decision.intents[0]
    run_id = "equal-timestamp-risk-order"
    old_path = _path_id(run_id, old_row.row_id, old_intent.intent_id)
    new_path = _path_id(run_id, new_row.row_id, new_intent.intent_id)

    def risk_fill(
        *,
        fill_id: str,
        intent_id: str,
        row: NormalizedStateRow,
        action: OrderAction,
        timestamp_us: int,
        source_id: str,
    ) -> FillSlice:
        return FillSlice(
            fill_id=fill_id,
            order_id=intent_id,
            experiment_id="A01-SPREAD-CAPTURE",
            root_id=row.root_event_id,
            market_ticker=row.market_ticker,
            side=OutcomeSide.YES,
            action=action,
            liquidity_role=(
                LiquidityRole.MAKER
                if action is OrderAction.BUY
                else LiquidityRole.TAKER
            ),
            price_e4=4_000,
            quantity_e4=10_000,
            timestamp_us=timestamp_us,
            source_id=source_id,
            reason="risk-replay-ordering-test",
        )

    old_entry = risk_fill(
        fill_id="old-entry",
        intent_id=old_intent.intent_id,
        row=old_row,
        action=OrderAction.BUY,
        timestamp_us=old_row.decision_ts_ns // 1_000,
        source_id="old-trade",
    )
    new_entry = risk_fill(
        fill_id="new-entry",
        intent_id=new_intent.intent_id,
        row=new_row,
        action=OrderAction.BUY,
        timestamp_us=NOW // 1_000,
        source_id="new-trade",
    )
    old_exit = risk_fill(
        fill_id="old-exit",
        intent_id=f"{old_intent.intent_id}:exit",
        row=old_row,
        action=OrderAction.SELL,
        timestamp_us=NOW // 1_000,
        source_id="old-book|bid-level=0",
    )
    new_exit = risk_fill(
        fill_id="new-exit",
        intent_id=f"{new_intent.intent_id}:exit",
        row=new_row,
        action=OrderAction.SELL,
        timestamp_us=NOW // 1_000 + 1,
        source_id="new-book|bid-level=0",
    )

    def batch(fill: FillSlice) -> FillBatch:
        return FillBatch(
            fills=(fill,),
            ordered_e4=10_000,
            filled_e4=10_000,
            unfilled_e4=0,
            source_quantity_e4=10_000,
            consumed_source_quantity_e4=10_000,
            duplicate_source_allocations=0,
        )

    def pnl_cashflows(
        *,
        path_id: str,
        entry: FillSlice,
        exit_fill: FillSlice,
        entry_cash_e6: int,
        exit_cash_e6: int,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            cashflows=(
                CashFlow(
                    event_id=f"{entry.fill_id}:principal",
                    path_id=path_id,
                    sequence=0,
                    occurred_at_ns=entry.timestamp_us * 1_000,
                    kind=CashFlowKind.TRADE_PRINCIPAL,
                    amount_e6=entry_cash_e6,
                    position_delta_e4=entry.quantity_e4,
                    source_sha256=H_A,
                    fill_id=entry.fill_id,
                ),
                CashFlow(
                    event_id=f"{exit_fill.fill_id}:principal",
                    path_id=path_id,
                    sequence=1,
                    occurred_at_ns=exit_fill.timestamp_us * 1_000,
                    kind=CashFlowKind.TRADE_PRINCIPAL,
                    amount_e6=exit_cash_e6,
                    position_delta_e4=-exit_fill.quantity_e4,
                    source_sha256=H_B,
                    fill_id=exit_fill.fill_id,
                ),
            )
        )

    risk_ledger, replay_blockers = _replay_risk_lifecycle(
        limits=RiskLimits(
            max_market_e6=10_000_000,
            max_event_e6=10_000_000,
            max_factor_e6=10_000_000,
            max_total_e6=20_000_000,
            max_daily_loss_e6=1_000_000,
        ),
        run_id=run_id,
        intent_context={
            old_intent.intent_id: (old_decision, old_row, old_intent),
            new_intent.intent_id: (new_decision, new_row, new_intent),
        },
        excluded_intents=set(),
        entry_by_order={
            old_intent.intent_id: (old_entry,),
            new_intent.intent_id: (new_entry,),
        },
        trade_sources={"old-trade": H_A, "new-trade": H_B},
        exit_batches={old_path: batch(old_exit), new_path: batch(new_exit)},
        snapshot_sources={"old-book": H_C, "new-book": H_D},
        closures={
            old_intent.intent_id: {"exit_snapshot_id": "old-book"},
            new_intent.intent_id: {"exit_snapshot_id": "new-book"},
        },
        settlements={},
        pnl_ledgers={
            old_path: pnl_cashflows(
                path_id=old_path,
                entry=old_entry,
                exit_fill=old_exit,
                entry_cash_e6=-400_000,
                exit_cash_e6=300_000,
            ),
            new_path: pnl_cashflows(
                path_id=new_path,
                entry=new_entry,
                exit_fill=new_exit,
                entry_cash_e6=-400_000,
                exit_cash_e6=400_000,
            ),
        },
        result_by_path={
            old_path: {"net_pnl_e6": -100_000, "result_sha256": H_D},
            new_path: {"net_pnl_e6": 0, "result_sha256": H_E},
        },
        latency={"CANCEL": 0},
    )

    assert replay_blockers == []
    assert [
        event.kind.value
        for event in risk_ledger.events
        if event.occurred_at_ns == NOW
    ] == ["EXIT", "REALIZED_PNL", "RESERVE", "FILL"]
    assert risk_ledger.total_exposure_e6 == 0


def test_partial_exit_realizes_loss_before_later_settlement():
    raw = a01_row()
    row = NormalizedStateRow(**raw)
    decision = FrozenExperimentAdapters(freeze()).evaluate_a01(
        row,
        complete_bindings(),
    )
    intent = decision.intents[0]
    run_id = "mixed-exit-settlement-risk"
    path_id = _path_id(run_id, row.row_id, intent.intent_id)
    entry = FillSlice(
        fill_id="mixed-entry",
        order_id=intent.intent_id,
        experiment_id="A01-SPREAD-CAPTURE",
        root_id=row.root_event_id,
        market_ticker=row.market_ticker,
        side=OutcomeSide.YES,
        action=OrderAction.BUY,
        liquidity_role=LiquidityRole.MAKER,
        price_e4=4_000,
        quantity_e4=10_000,
        timestamp_us=NOW // 1_000 + 1,
        source_id="mixed-trade",
        reason="mixed-closure-test",
    )
    partial_exit = FillSlice(
        fill_id="mixed-partial-exit",
        order_id=f"{intent.intent_id}:exit",
        experiment_id="A01-SPREAD-CAPTURE",
        root_id=row.root_event_id,
        market_ticker=row.market_ticker,
        side=OutcomeSide.YES,
        action=OrderAction.SELL,
        liquidity_role=LiquidityRole.TAKER,
        price_e4=3_000,
        quantity_e4=5_000,
        timestamp_us=NOW // 1_000 + 2,
        source_id="mixed-book|bid-level=0",
        reason="mixed-closure-test",
    )
    exit_batch = FillBatch(
        fills=(partial_exit,),
        ordered_e4=10_000,
        filled_e4=5_000,
        unfilled_e4=5_000,
        source_quantity_e4=5_000,
        consumed_source_quantity_e4=5_000,
        duplicate_source_allocations=0,
    )
    settlement = {
        "settlement_id": "mixed-final",
        "market_ticker": row.market_ticker,
        "status": "FINALIZED",
        "finalized": True,
        "yes_settlement_value_e4": 10_000,
        "observed_at_ns": NOW + 20 * SECOND,
        "revision": 1,
        "source_sha256": H_D,
    }

    pnl_ledger = SimpleNamespace(
        cashflows=(
            CashFlow(
                event_id="mixed-entry:principal",
                path_id=path_id,
                sequence=0,
                occurred_at_ns=entry.timestamp_us * 1_000,
                kind=CashFlowKind.TRADE_PRINCIPAL,
                amount_e6=-400_000,
                position_delta_e4=10_000,
                source_sha256=H_A,
                fill_id=entry.fill_id,
            ),
            CashFlow(
                event_id="mixed-partial-exit:principal",
                path_id=path_id,
                sequence=1,
                occurred_at_ns=partial_exit.timestamp_us * 1_000,
                kind=CashFlowKind.TRADE_PRINCIPAL,
                amount_e6=100_000,
                position_delta_e4=-5_000,
                source_sha256=H_C,
                fill_id=partial_exit.fill_id,
            ),
            CashFlow(
                event_id="mixed-final:settlement",
                path_id=path_id,
                sequence=2,
                occurred_at_ns=settlement["observed_at_ns"],
                kind=CashFlowKind.SETTLEMENT,
                amount_e6=500_000,
                position_delta_e4=-5_000,
                source_sha256=H_D,
                fill_id=None,
            ),
        )
    )

    risk_ledger, replay_blockers = _replay_risk_lifecycle(
        limits=RiskLimits(
            max_market_e6=10_000_000,
            max_event_e6=10_000_000,
            max_factor_e6=10_000_000,
            max_total_e6=10_000_000,
            max_daily_loss_e6=50_000,
        ),
        run_id=run_id,
        intent_context={
            intent.intent_id: (decision, row, intent),
        },
        excluded_intents=set(),
        entry_by_order={intent.intent_id: (entry,)},
        trade_sources={"mixed-trade": H_A},
        exit_batches={path_id: exit_batch},
        snapshot_sources={"mixed-book": H_C},
        closures={
            intent.intent_id: {
                "exit_snapshot_id": "mixed-book",
                "settlement_id": "mixed-final",
            }
        },
        settlements={"mixed-final": settlement},
        pnl_ledgers={path_id: pnl_ledger},
        result_by_path={
            path_id: {
                "net_pnl_e6": 200_000,
                "result_sha256": H_E,
            }
        },
        latency={"CANCEL": 0},
    )

    assert replay_blockers == []
    assert [
        (event.kind.value, event.realized_pnl_e6)
        for event in risk_ledger.events
        if event.occurred_at_ns == partial_exit.timestamp_us * 1_000
    ] == [("EXIT", 0), ("REALIZED_PNL", -100_000)]
    assert [
        (event.kind.value, event.realized_pnl_e6)
        for event in risk_ledger.events
        if event.occurred_at_ns == settlement["observed_at_ns"]
    ] == [("SETTLEMENT", 0), ("REALIZED_PNL", 300_000)]
    assert risk_ledger.total_exposure_e6 == 0
    assert risk_ledger.daily_loss_breached_utc_dates == {
        "2026-07-12"
    }


def test_realized_daily_loss_blocks_later_opportunity_admission():
    fixture = base_fixture(path_count=6)
    first_snapshot = fixture["exit_snapshots"][0]
    first_snapshot["yes_bids"][0]["yes_price_e4"] = 1_000
    first_snapshot["yes_asks"][0]["yes_price_e4"] = 9_000

    shift_ns = 20 * SECOND
    second = copy.deepcopy(fixture["rows"][0])
    second.update(
        {
            "row_id": "a01-row-after-realized-loss",
            "root_event_id": "KXTEST-EVENT-2",
            "market_ticker": "KXTEST-EVENT-MARKET-2",
        }
    )
    for field in (
        "decision_ts_ns",
        "features_asof_ns",
        "book_observed_at_ns",
        "scheduled_start_ts_ns",
        "scheduled_start_asof_ns",
        "state_started_at_ns",
        "warmup_started_at_ns",
    ):
        second[field] += shift_ns
    fixture["rows"].append(second)

    second_activation_us = (
        second["decision_ts_ns"] + 300 + 999
    ) // 1_000
    fixture["public_trades"].extend(
        [
            {
                "trade_id": "trade-buy-yes-after-loss",
                "market_ticker": second["market_ticker"],
                "timestamp_us": second_activation_us + 1,
                "yes_price_e4": 3_800,
                "quantity_e4": 10_000,
                "taker_side": "NO",
                "source_sha256": H_A,
            },
            {
                "trade_id": "trade-buy-no-after-loss",
                "market_ticker": second["market_ticker"],
                "timestamp_us": second_activation_us + 2,
                "yes_price_e4": 4_600,
                "quantity_e4": 10_000,
                "taker_side": "YES",
                "source_sha256": H_B,
            },
        ]
    )
    second_exit_decision_ns = second["decision_ts_ns"] + 10 * SECOND
    fixture["exit_snapshots"].append(
        {
            "snapshot_id": "exit-book-after-loss",
            "market_ticker": second["market_ticker"],
            "receive_timestamp_us": second_exit_decision_ns // 1_000,
            "yes_bids": [
                {"yes_price_e4": 4_200, "quantity_e4": 10_000}
            ],
            "yes_asks": [
                {"yes_price_e4": 4_300, "quantity_e4": 10_000}
            ],
            "book_valid": True,
            "gap_free": True,
            "source_sha256": H_C,
        }
    )
    for intent_id in a01_intent_ids(second):
        fixture["closures"].append(
            {
                "intent_id": intent_id,
                "exit_snapshot_id": "exit-book-after-loss",
                "exit_decision_ts_ns": second_exit_decision_ns,
                "exit_limit_price_e4": 1,
                "maximum_snapshot_age_us": 10,
                "settlement_id": None,
            }
        )
    fixture["fee_contexts"].append(
        {
            "market_ticker": second["market_ticker"],
            "series_ticker": "KXTEST",
            "event_ticker": "KXTEST-EVENT",
        }
    )
    policy = risk_policy(max_daily_loss_e6=100_000)
    fixture["risk_policy"] = policy
    fixture["provenance"]["risk_policy_sha256"] = policy["policy_sha256"]
    terminal = terminal_receipt(6)
    fixture["preflight_inputs"]["terminal_coverage"] = terminal
    fixture["provenance"]["terminal_contract_sha256"] = canonical_sha256(
        terminal
    )
    refresh_evidence_bindings(fixture)

    receipt = execute(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert receipt["totals"] is None
    assert "RISK_DAILY_LOSS_CHANGED_ADMISSION" in blocker_codes(receipt)
    assert any(
        "daily realized loss gate is closed" in blocker["detail"]
        for blocker in receipt["blockers"]
    )


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
    refresh_evidence_bindings(fixture)

    receipt = execute(fixture)

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


def test_a11_cannot_complete_without_exact_post_decision_cancel_stream():
    fixture = base_fixture(path_count=2)
    row = a11_row()
    (intent_id,) = a11_intent_ids(row)
    fixture["experiment_id"] = "A11-ONE-SIDED-PROVISION"
    fixture["rows"] = [row]
    fixture["public_trades"] = [fixture["public_trades"][1]]
    fixture["closures"] = [
        {
            "intent_id": intent_id,
            "exit_snapshot_id": "exit-book",
            "exit_decision_ts_ns": NOW + 10 * SECOND,
            "exit_limit_price_e4": 1,
            "maximum_snapshot_age_us": 10,
            "settlement_id": None,
        }
    ]
    terminal = terminal_receipt(2)
    fixture["preflight_inputs"]["terminal_coverage"] = terminal
    fixture["provenance"]["terminal_contract_sha256"] = canonical_sha256(
        terminal
    )
    refresh_evidence_bindings(fixture)

    receipt = execute(fixture)

    assert receipt["state"] == PNL_BLOCKED
    assert receipt["totals"] is None
    assert blocker_codes(receipt) == {
        "A11_POST_DECISION_CANCEL_STREAM_UNAVAILABLE"
    }
    assert all(
        row["state"] == "PATH_COMPLETE"
        for row in receipt["path_rows"]
    )


def test_provenance_manifest_drift_blocks_promotion():
    fixture = base_fixture()
    fixture["provenance"]["releases"][0]["manifest_version_id"] = (
        "different-version"
    )

    receipt = execute(fixture)

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
    lineage = lineage_for(fixture)
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
    authority_path = tmp_path / "authority.json"
    authority_path.write_text(
        json.dumps(
            authority_for(fixture, lineage),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    authority_sha256 = hashlib.sha256(
        authority_path.read_bytes()
    ).hexdigest()
    lineage_path = tmp_path / "lineage.json"
    lineage_path.write_text(
        json.dumps(lineage, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    lineage_sha256 = hashlib.sha256(
        lineage_path.read_bytes()
    ).hexdigest()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.research.pnl_spine.runner",
            "--fixture",
            str(fixture_path),
            "--trusted-authority",
            str(authority_path),
            "--trusted-authority-sha256",
            authority_sha256,
            "--trusted-lineage-receipt",
            str(lineage_path),
            "--trusted-lineage-receipt-sha256",
            lineage_sha256,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    receipt = json.loads(completed.stdout)
    assert receipt["state"] == PNL_BLOCKED
    assert "EXTERNAL_AUTHORITY_INVALID" not in blocker_codes(receipt)
    assert "RECORD_LINEAGE_AUTHORITY_INVALID" not in blocker_codes(receipt)
    assert completed.stdout == json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
