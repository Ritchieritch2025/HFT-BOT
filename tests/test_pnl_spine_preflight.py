"""Adversarial tests for the read-only PnL-spine readiness gate."""

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

from tools.research.pnl_spine.preflight import (  # noqa: E402
    ENGINEERING_BLOCKED,
    ENGINEERING_L2_DATES,
    ENGINEERING_READY,
    NET_BLOCKED,
    NET_READY,
    evaluate_readiness,
)


FREEZE_PATH = (
    ROOT
    / "Deepresearch V3"
    / "registry"
    / "frozen"
    / "PNL_SPINE_EXPERIMENTS_V1.json"
)
PREFLIGHT_PATH = ROOT / "tools" / "research" / "pnl_spine" / "preflight.py"
WRAPPER_PATH = ROOT / "deploy" / "w09" / "pnl-spine-readiness"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def seal(receipt: dict[str, Any]) -> dict[str, Any]:
    receipt = copy.deepcopy(receipt)
    receipt.pop("payload_sha256", None)
    receipt["payload_sha256"] = hashlib.sha256(canonical_bytes(receipt)).hexdigest()
    return receipt


def frozen_package() -> dict[str, Any]:
    with FREEZE_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def fee_receipt() -> dict[str, Any]:
    return seal(
        {
            "schema_version": "pnl-spine-fee-facts-receipt-v1",
            "receipt_id": "fees-20260712-15-17-v1",
            "status": "VERIFIED_EFFECTIVE_DATED",
            "coverage_dates": list(ENGINEERING_L2_DATES),
            "coverage_scope": "ALL_ELIGIBLE_SERIES_EVENTS_AND_ROLES",
            "bindings": {
                "fee_facts_sha256": SHA_A,
                "maker_fee_formula_id": "kalshi-maker-effective-v1",
                "taker_fee_formula_id": "kalshi-taker-effective-v1",
                "account_class": "research-measured-account",
                "target_balance_precision": 6,
                "fee_rounding_accumulator_version": "centicent-carry-v1",
                "rebate_and_event_override_version": "effective-dated-v1",
            },
            "source_sha256": SHA_B,
            "event_override_history_complete": True,
            "order_rounding_rebate_complete": True,
            "private_fee_precedence": True,
        }
    )


def latency_receipt() -> dict[str, Any]:
    samples = []
    for index, path in enumerate(("PLACE", "CANCEL", "IOC_EXIT")):
        decision = 1_000_000 + index * 10_000
        samples.append(
            {
                "sample_id": f"sample-{path.lower()}",
                "order_id": f"redacted-order-{index}",
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
            "receipt_id": "real-latency-v1",
            "measurement_mode": "REAL_ORDER_MEASURED",
            "clock_id": "CLOCK_MONOTONIC_RAW",
            "measured_on": "production-order-path",
            "created_at_ns": 1_100_000,
            "environment_fingerprint_sha256": SHA_D,
            "source_sha256": SHA_A,
            "samples": samples,
        }
    )


def release_dq_receipt() -> dict[str, Any]:
    dates = []
    for index, date in enumerate(ENGINEERING_L2_DATES):
        dates.append(
            {
                "date": date,
                "release_id": f"{date}__v3ref__exact-{index}",
                "manifest_version_id": f"version-{index}",
                "manifest_sha256": SHA_A,
                "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
                "l2_quality_state": "PASS",
                "l2_quality_receipt_sha256": SHA_B,
                "l2_row_count": 10_000 + index,
                "l2_objects_sha256": SHA_C,
            }
        )
    return seal(
        {
            "schema_version": "pnl-spine-exact-release-dq-receipt-v1",
            "receipt_id": "exact-l2-clean-engineering-v1",
            "cohort_id": "L2-CLEAN-ENGINEERING-20260712-15-17",
            "dates": dates,
            "root_start_bindings": {
                "root_map_version": "root-map-v1",
                "scheduled_start_source": "capture-catalog-asof-v1",
                "scheduled_start_asof_semantics": "AVAILABLE_AT_OR_BEFORE_DECISION",
                "tick_table_version": "tick-table-v1",
                "lifecycle_version": "market-lifecycle-v1",
            },
            "root_map_sha256": SHA_D,
            "scheduled_start_sha256": SHA_C,
            "source_sha256": SHA_B,
        }
    )


def terminal_receipt() -> dict[str, Any]:
    return seal(
        {
            "schema_version": "pnl-spine-terminal-coverage-receipt-v1",
            "receipt_id": "terminal-coverage-v1",
            "status": "COMPLETE",
            "coverage_dates": list(ENGINEERING_L2_DATES),
            "closure_scope": "EVERY_STRATEGY_AND_BASELINE_PATH",
            "bindings": {
                "terminal_contract_version": "terminal-v1",
                "legal_exception_state_table_sha256": SHA_A,
                "ioc_round_cap": 8,
                "ioc_time_cap_ms": 5_000,
                "residual_policy_version": "fail-closed-zero-residual-v1",
                "realized_outcome_requirement": "FINALIZED_EXACT_PAYOUT",
            },
            "path_count": 12,
            "closed_path_count": 12,
            "unresolved_path_count": 0,
            "residual_quantity_e4": 0,
            "all_paths_have_exit_or_final_settlement": True,
            "append_only_lifecycle_complete": True,
            "source_sha256": SHA_D,
        }
    )


def evaluate(
    experiment_id: str = "A01-SPREAD-CAPTURE",
    *,
    fee: object | None = None,
    latency: object | None = None,
    release: object | None = None,
    terminal: object | None = None,
) -> dict[str, Any]:
    return evaluate_readiness(
        experiment_id=experiment_id,
        frozen_experiment=frozen_package(),
        fee_facts_receipt=fee_receipt() if fee is None else fee,
        measured_latency_receipt=latency_receipt() if latency is None else latency,
        release_dq_receipt=release_dq_receipt() if release is None else release,
        terminal_coverage_receipt=(
            terminal_receipt() if terminal is None else terminal
        ),
    )


def blocker_codes(result: dict[str, Any], tier: str) -> set[str]:
    return {row["code"] for row in result[tier]["blockers"]}


@pytest.mark.parametrize(
    "experiment_id",
    ["A01-SPREAD-CAPTURE", "A11-ONE-SIDED-PROVISION"],
)
def test_fully_bound_frozen_experiment_is_ready_for_both_tiers(experiment_id):
    result = evaluate(experiment_id)

    assert result["engineering_replay"] == {
        "state": ENGINEERING_READY,
        "blockers": [],
    }
    assert result["net_pnl"] == {"state": NET_READY, "blockers": []}
    assert result["promotion_allowed"] is False
    assert result["claim_tier"] == "ENGINEERING_ONLY"


def test_missing_fee_never_defaults_to_zero_but_does_not_block_engineering():
    result = evaluate_readiness(
        experiment_id="A01-SPREAD-CAPTURE",
        frozen_experiment=frozen_package(),
        fee_facts_receipt=None,
        measured_latency_receipt=latency_receipt(),
        release_dq_receipt=release_dq_receipt(),
        terminal_coverage_receipt=terminal_receipt(),
    )

    assert result["engineering_replay"]["state"] == ENGINEERING_READY
    assert result["net_pnl"]["state"] == NET_BLOCKED
    assert blocker_codes(result, "net_pnl") == {"FEE_FACTS_RECEIPT_MISSING"}


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("PLACE", "LATENCY_PLACE_SAMPLES_MISSING"),
        ("CANCEL", "LATENCY_CANCEL_SAMPLES_MISSING"),
        ("IOC_EXIT", "LATENCY_IOC_EXIT_SAMPLES_MISSING"),
    ],
)
def test_each_real_latency_path_is_mandatory(path, code):
    receipt = latency_receipt()
    receipt["samples"] = [row for row in receipt["samples"] if row["path"] != path]
    receipt = seal(receipt)

    result = evaluate(latency=receipt)

    assert result["engineering_replay"]["state"] == ENGINEERING_READY
    assert result["net_pnl"]["state"] == NET_BLOCKED
    assert code in blocker_codes(result, "net_pnl")


def test_fake_or_noncausal_or_unbound_latency_is_rejected():
    fake = latency_receipt()
    fake["measurement_mode"] = "SYNTHETIC"
    fake = seal(fake)
    assert "LATENCY_MEASUREMENT_NOT_REAL" in blocker_codes(
        evaluate(latency=fake), "net_pnl"
    )

    noncausal = latency_receipt()
    noncausal["samples"][0]["sent_ns"] = (
        noncausal["samples"][0]["decision_ns"] - 1
    )
    noncausal = seal(noncausal)
    assert "LATENCY_SAMPLE_NON_CAUSAL" in blocker_codes(
        evaluate(latency=noncausal), "net_pnl"
    )

    tampered = latency_receipt()
    tampered["samples"][0]["effective_ns"] += 1
    assert "LATENCY_PAYLOAD_SHA_MISMATCH" in blocker_codes(
        evaluate(latency=tampered), "net_pnl"
    )


def test_only_three_clean_engineering_l2_dates_are_admitted():
    extra = release_dq_receipt()
    extra["dates"].insert(
        1,
        {
            **extra["dates"][0],
            "date": "2026-07-13",
            "release_id": "2026-07-13__v3ref__damaged",
        },
    )
    extra = seal(extra)
    result = evaluate(release=extra)
    codes = blocker_codes(result, "engineering_replay")
    assert result["engineering_replay"]["state"] == ENGINEERING_BLOCKED
    assert "DATA_DATE_NOT_ALLOWED" in codes
    assert "DATA_DATE_SET_MISMATCH" in codes

    missing = release_dq_receipt()
    missing["dates"].pop()
    missing = seal(missing)
    result = evaluate(release=missing)
    assert result["engineering_replay"]["state"] == ENGINEERING_BLOCKED
    assert "DATA_DATE_SET_MISMATCH" in blocker_codes(
        result, "engineering_replay"
    )


def test_root_and_scheduled_start_are_hard_engineering_gates():
    receipt = release_dq_receipt()
    receipt["root_start_bindings"]["root_map_version"] = None
    receipt["root_start_bindings"]["scheduled_start_source"] = None
    receipt["root_start_bindings"]["scheduled_start_asof_semantics"] = None
    receipt["root_map_sha256"] = None
    receipt["scheduled_start_sha256"] = None
    receipt = seal(receipt)

    result = evaluate(release=receipt)
    codes = blocker_codes(result, "engineering_replay")

    assert result["engineering_replay"]["state"] == ENGINEERING_BLOCKED
    assert {
        "ROOT_MAP_MISSING",
        "ROOT_MAP_SHA_MISSING",
        "SCHEDULED_START_SOURCE_MISSING",
        "SCHEDULED_START_ASOF_MISSING",
        "SCHEDULED_START_SHA_MISSING",
    } <= codes


def test_terminal_exit_and_settlement_must_reconcile_every_path():
    receipt = terminal_receipt()
    receipt["closed_path_count"] = 10
    receipt["unresolved_path_count"] = 2
    receipt["residual_quantity_e4"] = 10_000
    receipt["all_paths_have_exit_or_final_settlement"] = False
    receipt["append_only_lifecycle_complete"] = False
    receipt = seal(receipt)

    result = evaluate(terminal=receipt)
    codes = blocker_codes(result, "net_pnl")

    assert result["engineering_replay"]["state"] == ENGINEERING_READY
    assert {
        "TERMINAL_PATHS_NOT_ALL_CLOSED",
        "TERMINAL_UNRESOLVED_PATHS",
        "TERMINAL_RESIDUAL_QUANTITY",
        "TERMINAL_EXIT_OR_SETTLEMENT_MISSING",
        "TERMINAL_LIFECYCLE_INCOMPLETE",
    } <= codes


def test_current_b09_can_replay_engineering_but_cannot_claim_net_pnl():
    result = evaluate("B09-LISTING-TO-START-DRIFT")
    codes = blocker_codes(result, "net_pnl")

    assert result["engineering_replay"]["state"] == ENGINEERING_READY
    assert result["net_pnl"]["state"] == NET_BLOCKED
    assert "B09_TRAIN_ARTIFACT_MISSING" in codes
    assert "B09_ADMITTED_CELLS_NOT_FROZEN" in codes
    assert "B09_DIRECTION_BY_CELL_NOT_FROZEN" in codes
    assert "B09_PRETRAINING_BINDING_MISSING" in codes


def test_schema_drift_fails_closed_even_when_resealed():
    receipt = fee_receipt()
    receipt["unreviewed_default_fee"] = 0
    receipt = seal(receipt)

    result = evaluate(fee=receipt)

    assert result["net_pnl"]["state"] == NET_BLOCKED
    assert "FEE_UNKNOWN_FIELDS" in blocker_codes(result, "net_pnl")


def test_cli_is_read_only_canonical_and_tier_specific(tmp_path):
    documents = {
        "frozen": frozen_package(),
        "fee": fee_receipt(),
        "latency": latency_receipt(),
        "release": release_dq_receipt(),
        "terminal": terminal_receipt(),
    }
    paths = {}
    for name, document in documents.items():
        path = tmp_path / f"{name}.json"
        path.write_bytes(canonical_bytes(document))
        paths[name] = path
    mtimes = {name: path.stat().st_mtime_ns for name, path in paths.items()}
    command = [
        sys.executable,
        "-I",
        "-B",
        str(PREFLIGHT_PATH),
        "--experiment-id",
        "A01-SPREAD-CAPTURE",
        "--frozen-experiment",
        str(paths["frozen"]),
        "--fee-facts",
        str(paths["fee"]),
        "--measured-latency",
        str(paths["latency"]),
        "--release-dq",
        str(paths["release"]),
        "--terminal-coverage",
        str(paths["terminal"]),
        "--required-tier",
        NET_READY,
    ]

    completed = subprocess.run(command, check=False, capture_output=True)

    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["net_pnl"]["state"] == NET_READY
    assert completed.stdout == canonical_bytes(result) + b"\n"
    assert completed.stderr == b""
    assert mtimes == {name: path.stat().st_mtime_ns for name, path in paths.items()}


def test_cli_missing_net_receipts_can_pass_engineering_but_not_net(tmp_path):
    freeze_path = tmp_path / "freeze.json"
    release_path = tmp_path / "release.json"
    freeze_path.write_bytes(canonical_bytes(frozen_package()))
    release_path.write_bytes(canonical_bytes(release_dq_receipt()))
    base = [
        sys.executable,
        "-I",
        "-B",
        str(PREFLIGHT_PATH),
        "--experiment-id",
        "A01-SPREAD-CAPTURE",
        "--frozen-experiment",
        str(freeze_path),
        "--release-dq",
        str(release_path),
        "--required-tier",
    ]

    engineering = subprocess.run(
        [*base, ENGINEERING_READY],
        check=False,
        capture_output=True,
    )
    net = subprocess.run(
        [*base, NET_READY],
        check=False,
        capture_output=True,
    )

    assert engineering.returncode == 0
    assert net.returncode == 2
    net_result = json.loads(net.stdout)
    assert {
        "FEE_FACTS_RECEIPT_MISSING",
        "MEASURED_LATENCY_RECEIPT_MISSING",
        "TERMINAL_COVERAGE_RECEIPT_MISSING",
    } <= blocker_codes(net_result, "net_pnl")


def test_w09_wrapper_is_offline_and_isolated():
    text = WRAPPER_PATH.read_text(encoding="utf-8")

    assert WRAPPER_PATH.stat().st_mode & 0o111
    assert "-I -B" in text
    for forbidden in ("aws ", "curl ", "ssh "):
        assert forbidden not in text.lower()
