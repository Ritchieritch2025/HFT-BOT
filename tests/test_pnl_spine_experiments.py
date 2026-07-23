"""Adversarial tests for the pure frozen-experiment strategy adapters."""

from __future__ import annotations

import builtins
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.research.pnl_spine.experiments import (  # noqa: E402
    AdapterContractError,
    DecisionStatus,
    FrozenExperimentAdapters,
    IntentSide,
    NormalizedStateRow,
    RuntimeBindings,
)


FREEZE_PATH = (
    ROOT
    / "Deepresearch V3"
    / "registry"
    / "frozen"
    / "PNL_SPINE_EXPERIMENTS_V1.json"
)
SECOND = 1_000_000_000
MINUTE = 60 * SECOND
NOW = 1_000_000 * SECOND
H = "a" * 64


def canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@pytest.fixture(scope="module")
def freeze_document() -> dict:
    return json.loads(FREEZE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def adapters(freeze_document: dict) -> FrozenExperimentAdapters:
    return FrozenExperimentAdapters(freeze_document)


def complete_bindings(**changes: object) -> RuntimeBindings:
    values: dict[str, object] = {
        "fee_facts_sha256": "1" * 64,
        "latency_receipt_sha256": "2" * 64,
        "root_map_sha256": "3" * 64,
        "scheduled_start_source_sha256": "4" * 64,
        "risk_policy_sha256": "5" * 64,
        "terminal_contract_sha256": "6" * 64,
        "strict_fill_evidence_sha256": "7" * 64,
        "card_parameter_artifact_sha256": "8" * 64,
    }
    values.update(changes)
    return RuntimeBindings(**values)  # type: ignore[arg-type]


def a01_row(**changes: object) -> NormalizedStateRow:
    values: dict[str, object] = {
        "row_id": "a01-row",
        "root_event_id": "root-1",
        "market_ticker": "KXTEST",
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
    values.update(changes)
    return NormalizedStateRow(**values)  # type: ignore[arg-type]


def a11_row(**changes: object) -> NormalizedStateRow:
    onset = NOW - 30 * SECOND
    values: dict[str, object] = {
        "row_id": "a11-row",
        "root_event_id": "root-2",
        "market_ticker": "KXOS",
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
    values.update(changes)
    return NormalizedStateRow(**values)  # type: ignore[arg-type]


def test_adapter_validates_freeze_revision_and_pinned_semantics(
    freeze_document: dict,
):
    FrozenExperimentAdapters(freeze_document)

    tampered = copy.deepcopy(freeze_document)
    tampered["revisions"][0]["parameter_freeze"]["book_ttl_ms"][
        "frozen"
    ] = 251
    with pytest.raises(AdapterContractError, match="freeze SHA mismatch"):
        FrozenExperimentAdapters(tampered)

    rehashed = copy.deepcopy(tampered)
    revision = rehashed["revisions"][0]
    without_revision_sha = copy.deepcopy(revision)
    without_revision_sha.pop("revision_definition_sha256")
    revision["revision_definition_sha256"] = canonical_sha(
        without_revision_sha
    )
    without_freeze_sha = copy.deepcopy(rehashed)
    without_freeze_sha.pop("freeze_sha256")
    rehashed["freeze_sha256"] = canonical_sha(without_freeze_sha)
    with pytest.raises(AdapterContractError, match="not pinned"):
        FrozenExperimentAdapters(rehashed)


def test_adapter_performs_no_file_or_aws_access_after_document_injection(
    freeze_document: dict, monkeypatch: pytest.MonkeyPatch
):
    def forbidden_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("adapter attempted file access")

    monkeypatch.setattr(builtins, "open", forbidden_open)
    local = FrozenExperimentAdapters(freeze_document)
    result = local.evaluate_a01(a01_row(), complete_bindings())
    assert result.status is DecisionStatus.ORDER_INTENTS


@pytest.mark.parametrize(
    ("missing", "reason"),
    [
        ("card_parameter_artifact_sha256", "BLOCK_MISSING_TRAIN_P90_BINDING"),
        ("fee_facts_sha256", "BLOCK_MISSING_FEE_BINDING"),
        ("latency_receipt_sha256", "BLOCK_MISSING_LATENCY_BINDING"),
    ],
)
def test_a01_missing_train_fee_or_latency_blocks(
    adapters: FrozenExperimentAdapters,
    missing: str,
    reason: str,
):
    decision = adapters.evaluate_a01(
        a01_row(), complete_bindings(**{missing: None})
    )
    assert decision.status is DecisionStatus.BLOCKED
    assert decision.intents == ()
    assert reason in decision.reason_codes
    assert decision.retained_for_zero_accounting


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"book_observed_at_ns": NOW - 250_000_001},
            "ABSTAIN_A01_BOOK_STALE",
        ),
        (
            {"best_yes_ask_e4": 4_300},
            "ABSTAIN_A01_SPREAD_BELOW_4_TICKS",
        ),
        (
            {"state_started_at_ns": NOW - 5 * SECOND + 1},
            "ABSTAIN_A01_SPREAD_DWELL_BELOW_5S",
        ),
        (
            {"net_capture_margin_ticks": 2},
            "ABSTAIN_A01_MARGIN_NOT_ABOVE_2_TICKS",
        ),
    ],
)
def test_a01_exact_conservative_boundaries_abstain(
    adapters: FrozenExperimentAdapters,
    changes: dict[str, object],
    reason: str,
):
    decision = adapters.evaluate_a01(
        a01_row(**changes), complete_bindings()
    )
    assert decision.status is DecisionStatus.ABSTAIN
    assert decision.triggered is False
    assert decision.intents == ()
    assert decision.reason_codes == (reason,)
    assert decision.retained_for_zero_accounting


def test_a01_emits_exact_two_sided_behind1_intents(
    adapters: FrozenExperimentAdapters,
):
    decision = adapters.evaluate_a01(a01_row(), complete_bindings())
    assert decision.status is DecisionStatus.ORDER_INTENTS
    assert decision.baseline_id == "NO_TRADE_SAME_OPPORTUNITIES"
    assert decision.claim_tier == "ENGINEERING_ONLY"
    assert [intent.side for intent in decision.intents] == [
        IntentSide.BUY,
        IntentSide.SELL,
    ]
    assert [intent.price_e4 for intent in decision.intents] == [3_900, 4_500]
    assert {intent.quantity_e4 for intent in decision.intents} == {10_000}
    assert {
        intent.root_entry_quantity_cap_e4 for intent in decision.intents
    } == {20_000}
    assert {intent.expire_after_ms for intent in decision.intents} == {5_000}
    assert all(intent.post_only for intent in decision.intents)
    assert all(not intent.replenish for intent in decision.intents)


def test_a01_zero_trigger_rows_are_not_dropped(
    adapters: FrozenExperimentAdapters,
):
    decisions = adapters.evaluate_rows(
        "A01-SPREAD-CAPTURE",
        [
            a01_row(row_id="trigger"),
            a01_row(row_id="zero", best_yes_ask_e4=4_300),
        ],
        complete_bindings(),
    )
    assert len(decisions) == 2
    assert [row.row_id for row in decisions] == ["trigger", "zero"]
    assert decisions[1].status is DecisionStatus.ABSTAIN
    assert decisions[1].retained_for_zero_accounting


def test_future_asof_normalized_input_is_rejected():
    with pytest.raises(AdapterContractError, match="future"):
        a01_row(features_asof_ns=NOW + 1)


@pytest.mark.parametrize("sport", ["Soccer", "Baseball", "Golf"])
def test_a11_is_restricted_to_tennis_and_basketball(
    adapters: FrozenExperimentAdapters, sport: str
):
    decision = adapters.evaluate_a11(
        a11_row(sport=sport), complete_bindings()
    )
    assert decision.status is DecisionStatus.ABSTAIN
    assert decision.reason_codes == (
        "ABSTAIN_A11_SPORT_OUTSIDE_TENNIS_BASKETBALL",
    )


@pytest.mark.parametrize("sport", ["Tennis", "Basketball"])
def test_a11_os0_bid_only_emits_k2_missing_ask(
    adapters: FrozenExperimentAdapters, sport: str
):
    decision = adapters.evaluate_a11(
        a11_row(sport=sport), complete_bindings()
    )
    assert decision.status is DecisionStatus.ORDER_INTENTS
    assert decision.baseline_id == "NO_TRADE_SAME_OPPORTUNITIES"
    assert len(decision.intents) == 1
    intent = decision.intents[0]
    assert intent.side is IntentSide.SELL
    assert intent.price_e4 == 4_300
    assert intent.expire_after_ms == 30_000
    assert intent.max_hold_ms == 120_000
    assert intent.root_entry_quantity_cap_e4 == 10_000
    assert intent.replenish is False


def test_a11_os0_ask_only_emits_k2_missing_bid(
    adapters: FrozenExperimentAdapters,
):
    decision = adapters.evaluate_a11(
        a11_row(
            best_yes_bid_e4=None,
            best_yes_ask_e4=4_600,
            reference_mid_onset_e4=4_400,
            surviving_side_onset_e4=4_600,
        ),
        complete_bindings(),
    )
    assert decision.status is DecisionStatus.ORDER_INTENTS
    intent = decision.intents[0]
    assert intent.side is IntentSide.BUY
    assert intent.price_e4 == 4_300


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"scheduled_start_ts_ns": NOW + 15 * MINUTE - 1},
            "ABSTAIN_A11_OUTSIDE_15_TO_240M_PRESTART",
        ),
        (
            {"scheduled_start_ts_ns": NOW + 240 * MINUTE + 1},
            "ABSTAIN_A11_OUTSIDE_15_TO_240M_PRESTART",
        ),
        (
            {"state_started_at_ns": NOW - 30 * SECOND + 1},
            "ABSTAIN_A11_DWELL_BELOW_30S",
        ),
        (
            {"reference_mid_onset_e4": 9_000},
            "ABSTAIN_A11_OUTSIDE_10_TO_90C",
        ),
        (
            {
                "reference_mid_onset_observed_at_ns": (
                    NOW - 30 * SECOND - SECOND - 1
                )
            },
            "ABSTAIN_A11_ANCHOR_OLDER_THAN_1S_AT_ONSET",
        ),
    ],
)
def test_a11_os0_exact_bounds_abstain(
    adapters: FrozenExperimentAdapters,
    changes: dict[str, object],
    reason: str,
):
    decision = adapters.evaluate_a11(
        a11_row(**changes), complete_bindings()
    )
    assert decision.status is DecisionStatus.ABSTAIN
    assert decision.reason_codes == (reason,)
    assert decision.intents == ()
    assert decision.retained_for_zero_accounting


def test_a11_accepts_inclusive_tts_and_lower_price_bound(
    adapters: FrozenExperimentAdapters,
):
    for suffix, tts in (("min", 15 * MINUTE), ("max", 240 * MINUTE)):
        decision = adapters.evaluate_a11(
            a11_row(
                row_id=f"a11-{suffix}",
                scheduled_start_ts_ns=NOW + tts,
                reference_mid_onset_e4=1_000,
                best_yes_bid_e4=800,
                surviving_side_onset_e4=800,
            ),
            complete_bindings(),
        )
        assert decision.status is DecisionStatus.ORDER_INTENTS


def test_a11_future_at_onset_anchor_blocks_not_abstains(
    adapters: FrozenExperimentAdapters,
):
    decision = adapters.evaluate_a11(
        a11_row(
            reference_mid_onset_observed_at_ns=NOW - 30 * SECOND + 1
        ),
        complete_bindings(),
    )
    assert decision.status is DecisionStatus.BLOCKED
    assert decision.reason_codes == ("BLOCK_A11_ANCHOR_IS_FUTURE_AT_ONSET",)


def test_b09_is_blocked_and_cannot_guess_from_row(
    adapters: FrozenExperimentAdapters,
):
    row = a01_row(
        row_id="b09-row",
        listing_age_bucket="AGE_1_TO_6H",
        scheduled_phase="PRE_1_TO_6H",
        net_capture_margin_ticks=999,
    )
    decision = adapters.evaluate_b09(row, complete_bindings())
    assert decision.status is DecisionStatus.BLOCKED
    assert decision.triggered is False
    assert decision.intents == ()
    assert decision.reason_codes == (
        "BLOCKED_PARAMETER_TRAINING",
        "BLOCK_B09_DIRECTION_CELL_HORIZON_SHA_UNBOUND",
    )
    assert decision.retained_for_zero_accounting


def test_decisions_and_intents_are_deterministic(
    adapters: FrozenExperimentAdapters,
):
    first = adapters.evaluate_a01(a01_row(), complete_bindings())
    second = adapters.evaluate_a01(a01_row(), complete_bindings())
    assert first == second
    assert first.sha256 == second.sha256
    assert [intent.intent_id for intent in first.intents] == [
        intent.intent_id for intent in second.intents
    ]


def test_duplicate_rows_fail_instead_of_double_counting(
    adapters: FrozenExperimentAdapters,
):
    with pytest.raises(AdapterContractError, match="duplicate"):
        adapters.evaluate_rows(
            "A01-SPREAD-CAPTURE",
            [a01_row(), a01_row()],
            complete_bindings(),
        )
