"""Pure strategy adapters for the three frozen PnL-spine experiments.

The caller supplies both the already-loaded frozen document and normalized,
past-only state rows.  This module deliberately performs no filesystem,
network, AWS, venue, fill, or ledger operation.  Its only output is a
deterministic order-intent decision or a fail-closed abstain/block decision.

This adapter release is pinned to ``PNL-SPINE-EXPERIMENTS-V1``.  A semantic
change, including a trained B09 artifact, requires a new immutable revision
and a correspondingly reviewed adapter release.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Any, Iterable, Mapping


PINNED_FREEZE_SHA256 = (
    "57c1045a3f33aa1a22e9d2e0862fd152db1761d1b9cad683e024de3478f53029"
)
PINNED_REVISION_SHA256 = {
    "A01-SPREAD-CAPTURE": (
        "5bd151125869bd4bb1173be6914be9cf57f9256f2252a979e02245aa101a2680"
    ),
    "A11-ONE-SIDED-PROVISION": (
        "c5e205243d71ff458cac91b2d69ebe2ac60a08590044c32fba0240ab52c5d2f8"
    ),
    "B09-LISTING-TO-START-DRIFT": (
        "fae965d16a0c6f61cbbeaf9d0ce9b40af4582c7c408c62f79af8732dd4d87873"
    ),
}
PINNED_CARD_SHA256 = {
    "A01-SPREAD-CAPTURE": (
        "d0a426775e71969141ec96db9fac7a5d38e53fb4d294d1d6027dd37bfc4cb9d4"
    ),
    "A11-ONE-SIDED-PROVISION": (
        "4258bbe91636b0e1798a733e9c58d958bf51ae5548156b5813d47f374ebf6d2c"
    ),
    "B09-LISTING-TO-START-DRIFT": (
        "d2c4c9765c141713bf1d10d1147dc7edd8a35cd2a70a587a7620223081c28023"
    ),
}

PRICE_SCALE_E4 = 10_000
ONE_CONTRACT_E4 = 10_000
NS_PER_MS = 1_000_000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AdapterContractError(ValueError):
    """A frozen definition or normalized row violates its contract."""


class DecisionStatus(str, Enum):
    ORDER_INTENTS = "ORDER_INTENTS"
    ABSTAIN = "ABSTAIN"
    BLOCKED = "BLOCKED"


class IntentAction(str, Enum):
    PLACE_LIMIT = "PLACE_LIMIT"


class IntentSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class IntentOutcome(str, Enum):
    YES = "YES"


def _canonical_sha256(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AdapterContractError(
            f"value is not canonical JSON: {exc}"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _require_sha(name: str, value: object) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise AdapterContractError(f"{name} must be a lowercase SHA-256")
    return value


def _require_plain_int(
    name: str,
    value: object,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        raise AdapterContractError(f"{name} must be a plain integer")
    if minimum is not None and value < minimum:
        raise AdapterContractError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise AdapterContractError(f"{name} must be <= {maximum}")
    return value


def _require_optional_plain_int(
    name: str,
    value: object,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if value is None:
        return None
    return _require_plain_int(
        name, value, minimum=minimum, maximum=maximum
    )


def _require_nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise AdapterContractError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class RuntimeBindings:
    """Runtime facts supplied by a separate, provenance-checked preflight."""

    fee_facts_sha256: str | None = None
    latency_receipt_sha256: str | None = None
    root_map_sha256: str | None = None
    scheduled_start_source_sha256: str | None = None
    risk_policy_sha256: str | None = None
    terminal_contract_sha256: str | None = None
    strict_fill_evidence_sha256: str | None = None
    card_parameter_artifact_sha256: str | None = None

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if value is not None:
                _require_sha(name, value)

    @property
    def sha256(self) -> str:
        return _canonical_sha256(asdict(self))

    def missing_reasons(
        self, *, card_parameter_reason: str
    ) -> tuple[str, ...]:
        checks = (
            (
                self.card_parameter_artifact_sha256,
                card_parameter_reason,
            ),
            (self.fee_facts_sha256, "BLOCK_MISSING_FEE_BINDING"),
            (
                self.latency_receipt_sha256,
                "BLOCK_MISSING_LATENCY_BINDING",
            ),
            (self.root_map_sha256, "BLOCK_MISSING_ROOT_MAP_BINDING"),
            (
                self.scheduled_start_source_sha256,
                "BLOCK_MISSING_SCHEDULED_START_BINDING",
            ),
            (self.risk_policy_sha256, "BLOCK_MISSING_RISK_BINDING"),
            (
                self.terminal_contract_sha256,
                "BLOCK_MISSING_TERMINAL_BINDING",
            ),
            (
                self.strict_fill_evidence_sha256,
                "BLOCK_MISSING_STRICT_FILL_BINDING",
            ),
        )
        return tuple(reason for value, reason in checks if value is None)


@dataclass(frozen=True)
class NormalizedStateRow:
    """One causally normalized state at a single decision receive time."""

    row_id: str
    root_event_id: str
    market_ticker: str
    sport: str
    decision_ts_ns: int
    features_asof_ns: int
    book_observed_at_ns: int
    tick_size_e4: int
    scheduled_start_ts_ns: int | None = None
    scheduled_start_asof_ns: int | None = None
    best_yes_bid_e4: int | None = None
    best_yes_ask_e4: int | None = None
    state_started_at_ns: int | None = None
    warmup_started_at_ns: int | None = None
    reference_mid_onset_e4: int | None = None
    reference_mid_onset_observed_at_ns: int | None = None
    surviving_side_onset_e4: int | None = None
    net_capture_margin_ticks: int | None = None
    update_count_60s: int | None = None
    trade_count_300s: int | None = None
    listing_age_bucket: str | None = None
    scheduled_phase: str | None = None
    activity_gate_passed: bool | None = None
    toxicity_gate_passed: bool | None = None
    adverse_width_gate_passed: bool | None = None
    activity_burst: bool | None = None
    book_valid: bool = True
    lifecycle_open: bool = True
    gap: bool = False
    paused: bool = False
    locked: bool = False
    crossed: bool = False

    def __post_init__(self) -> None:
        for name in ("row_id", "root_event_id", "market_ticker", "sport"):
            _require_nonempty(name, getattr(self, name))
        decision = _require_plain_int(
            "decision_ts_ns", self.decision_ts_ns, minimum=0
        )
        for name in ("features_asof_ns", "book_observed_at_ns"):
            value = _require_plain_int(name, getattr(self, name), minimum=0)
            if value > decision:
                raise AdapterContractError(
                    f"{name} is future relative to decision_ts_ns"
                )
        tick = _require_plain_int(
            "tick_size_e4",
            self.tick_size_e4,
            minimum=1,
            maximum=PRICE_SCALE_E4,
        )
        if PRICE_SCALE_E4 % tick:
            raise AdapterContractError(
                "tick_size_e4 must exactly divide the E4 price scale"
            )
        for name in (
            "best_yes_bid_e4",
            "best_yes_ask_e4",
            "reference_mid_onset_e4",
            "surviving_side_onset_e4",
        ):
            value = _require_optional_plain_int(
                name,
                getattr(self, name),
                minimum=0,
                maximum=PRICE_SCALE_E4,
            )
            if (
                name in {"best_yes_bid_e4", "best_yes_ask_e4"}
                and value is not None
                and value % tick
            ):
                raise AdapterContractError(
                    f"{name} is not aligned to tick_size_e4"
                )
        for name in (
            "scheduled_start_ts_ns",
            "scheduled_start_asof_ns",
            "state_started_at_ns",
            "warmup_started_at_ns",
            "reference_mid_onset_observed_at_ns",
        ):
            value = _require_optional_plain_int(
                name, getattr(self, name), minimum=0
            )
            if (
                value is not None
                and name != "scheduled_start_ts_ns"
                and value > decision
            ):
                raise AdapterContractError(
                    f"{name} is future relative to decision_ts_ns"
                )
        if (self.scheduled_start_ts_ns is None) != (
            self.scheduled_start_asof_ns is None
        ):
            raise AdapterContractError(
                "scheduled start value and as-of timestamp must coexist"
            )
        _require_optional_plain_int(
            "net_capture_margin_ticks", self.net_capture_margin_ticks
        )
        for name in ("update_count_60s", "trade_count_300s"):
            _require_optional_plain_int(
                name, getattr(self, name), minimum=0
            )
        for name in ("listing_age_bucket", "scheduled_phase"):
            value = getattr(self, name)
            if value is not None:
                _require_nonempty(name, value)
        for name in (
            "activity_gate_passed",
            "toxicity_gate_passed",
            "adverse_width_gate_passed",
            "activity_burst",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise AdapterContractError(f"{name} must be bool or None")
        for name in (
            "book_valid",
            "lifecycle_open",
            "gap",
            "paused",
            "locked",
            "crossed",
        ):
            if type(getattr(self, name)) is not bool:
                raise AdapterContractError(f"{name} must be bool")


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    experiment_id: str
    revision_definition_sha256: str
    row_id: str
    action: IntentAction
    outcome: IntentOutcome
    side: IntentSide
    price_e4: int
    quantity_e4: int
    time_in_force: str
    post_only: bool
    reduce_only: bool
    expire_after_ms: int
    max_hold_ms: int | None
    root_entry_quantity_cap_e4: int
    replenish: bool
    reason_code: str

    def __post_init__(self) -> None:
        for name in (
            "intent_id",
            "experiment_id",
            "row_id",
            "time_in_force",
            "reason_code",
        ):
            _require_nonempty(name, getattr(self, name))
        _require_sha(
            "revision_definition_sha256",
            self.revision_definition_sha256,
        )
        if not isinstance(self.action, IntentAction):
            raise AdapterContractError("action must be IntentAction")
        if not isinstance(self.outcome, IntentOutcome):
            raise AdapterContractError("outcome must be IntentOutcome")
        if not isinstance(self.side, IntentSide):
            raise AdapterContractError("side must be IntentSide")
        _require_plain_int(
            "price_e4", self.price_e4, minimum=1, maximum=PRICE_SCALE_E4 - 1
        )
        _require_plain_int("quantity_e4", self.quantity_e4, minimum=1)
        _require_plain_int("expire_after_ms", self.expire_after_ms, minimum=1)
        _require_optional_plain_int(
            "max_hold_ms", self.max_hold_ms, minimum=1
        )
        _require_plain_int(
            "root_entry_quantity_cap_e4",
            self.root_entry_quantity_cap_e4,
            minimum=1,
        )
        for name in ("post_only", "reduce_only", "replenish"):
            if type(getattr(self, name)) is not bool:
                raise AdapterContractError(f"{name} must be bool")


@dataclass(frozen=True)
class StrategyDecision:
    row_id: str
    experiment_id: str
    revision_definition_sha256: str
    baseline_id: str
    claim_tier: str
    status: DecisionStatus
    triggered: bool
    intents: tuple[OrderIntent, ...]
    reason_codes: tuple[str, ...]
    runtime_bindings_sha256: str
    retained_for_zero_accounting: bool = True

    def __post_init__(self) -> None:
        for name in (
            "row_id",
            "experiment_id",
            "baseline_id",
            "claim_tier",
        ):
            _require_nonempty(name, getattr(self, name))
        _require_sha(
            "revision_definition_sha256",
            self.revision_definition_sha256,
        )
        _require_sha(
            "runtime_bindings_sha256", self.runtime_bindings_sha256
        )
        if not isinstance(self.status, DecisionStatus):
            raise AdapterContractError("status must be DecisionStatus")
        if type(self.triggered) is not bool:
            raise AdapterContractError("triggered must be bool")
        if type(self.retained_for_zero_accounting) is not bool:
            raise AdapterContractError(
                "retained_for_zero_accounting must be bool"
            )
        if not self.retained_for_zero_accounting:
            raise AdapterContractError("every decision row must be retained")
        if self.status is DecisionStatus.ORDER_INTENTS:
            if not self.triggered or not self.intents:
                raise AdapterContractError(
                    "ORDER_INTENTS requires a trigger and at least one intent"
                )
        elif self.triggered or self.intents:
            raise AdapterContractError(
                "ABSTAIN/BLOCKED cannot carry trigger or intents"
            )
        if not self.reason_codes:
            raise AdapterContractError("every decision requires a reason code")

    @property
    def sha256(self) -> str:
        return _canonical_sha256(asdict(self))


class FrozenExperimentAdapters:
    """Hash-pinned, deterministic adapters for A01, A11 and blocked B09."""

    def __init__(self, freeze_document: Mapping[str, Any]) -> None:
        if not isinstance(freeze_document, Mapping):
            raise AdapterContractError("freeze_document must be a mapping")
        self._document = copy.deepcopy(dict(freeze_document))
        self._validate_freeze()
        self._revisions = {
            revision["source_card"]["experiment_id"]: revision
            for revision in self._document["revisions"]
        }
        self._validate_strategy_parameters()

    def _validate_freeze(self) -> None:
        document = self._document
        if document.get("schema_version") != (
            "pnl-spine-experiment-freeze-v1"
        ):
            raise AdapterContractError("unknown frozen experiment schema")
        if document.get("freeze_id") != "PNL-SPINE-EXPERIMENTS-V1":
            raise AdapterContractError("unexpected freeze_id")
        stored_freeze_sha = _require_sha(
            "freeze_sha256", document.get("freeze_sha256")
        )
        without_freeze_sha = copy.deepcopy(document)
        without_freeze_sha.pop("freeze_sha256", None)
        if _canonical_sha256(without_freeze_sha) != stored_freeze_sha:
            raise AdapterContractError("freeze SHA mismatch")
        if stored_freeze_sha != PINNED_FREEZE_SHA256:
            raise AdapterContractError("freeze SHA is not pinned to this adapter")

        revisions = document.get("revisions")
        if not isinstance(revisions, list):
            raise AdapterContractError("revisions must be a list")
        seen: set[str] = set()
        for revision in revisions:
            if not isinstance(revision, dict):
                raise AdapterContractError("revision must be an object")
            source = revision.get("source_card")
            if not isinstance(source, dict):
                raise AdapterContractError("source_card must be an object")
            experiment_id = source.get("experiment_id")
            if experiment_id in seen:
                raise AdapterContractError("duplicate frozen experiment")
            seen.add(experiment_id)
            if experiment_id not in PINNED_REVISION_SHA256:
                raise AdapterContractError(
                    f"unexpected frozen experiment {experiment_id!r}"
                )
            revision_sha = _require_sha(
                "revision_definition_sha256",
                revision.get("revision_definition_sha256"),
            )
            without_revision_sha = copy.deepcopy(revision)
            without_revision_sha.pop("revision_definition_sha256", None)
            if _canonical_sha256(without_revision_sha) != revision_sha:
                raise AdapterContractError(
                    f"{experiment_id}: revision SHA mismatch"
                )
            if revision_sha != PINNED_REVISION_SHA256[experiment_id]:
                raise AdapterContractError(
                    f"{experiment_id}: revision SHA is not pinned"
                )
            if (
                source.get("card_definition_sha256")
                != PINNED_CARD_SHA256[experiment_id]
            ):
                raise AdapterContractError(
                    f"{experiment_id}: source card SHA mismatch"
                )
        if seen != set(PINNED_REVISION_SHA256):
            raise AdapterContractError("required frozen revisions are missing")

        cohorts = document.get("cohorts", {})
        engineering = cohorts.get("engineering_acceptance", {})
        confirmation = cohorts.get("untouched_confirmation", {})
        if engineering.get("dates_utc") != [
            "2026-07-12",
            "2026-07-15",
            "2026-07-17",
        ]:
            raise AdapterContractError("engineering cohort drifted")
        if engineering.get("claim_tier") != "ENGINEERING_ONLY":
            raise AdapterContractError("engineering claim tier drifted")
        if (
            confirmation.get("status") != "NOT_ALLOCATED"
            or confirmation.get("dates_utc") != []
        ):
            raise AdapterContractError(
                "untouched confirmation must remain NOT_ALLOCATED"
            )

    @staticmethod
    def _frozen(parameter_freeze: Mapping[str, Any], name: str) -> Any:
        parameter = parameter_freeze.get(name)
        if not isinstance(parameter, Mapping) or "frozen" not in parameter:
            raise AdapterContractError(f"missing frozen parameter {name}")
        return parameter["frozen"]

    def _validate_strategy_parameters(self) -> None:
        revisions = {
            revision["source_card"]["experiment_id"]: revision
            for revision in self._document["revisions"]
        }
        for experiment_id, revision in revisions.items():
            if revision.get("claim_tier") != "ENGINEERING_ONLY":
                raise AdapterContractError(
                    f"{experiment_id}: claim tier must be ENGINEERING_ONLY"
                )
            baseline = revision.get("baseline", {})
            if baseline.get("baseline_id") != "NO_TRADE_SAME_OPPORTUNITIES":
                raise AdapterContractError(
                    f"{experiment_id}: baseline is not frozen NO_TRADE"
                )

        a01 = revisions["A01-SPREAD-CAPTURE"]["parameter_freeze"]
        a01_expected = {
            "book_ttl_ms": 250,
            "minimum_raw_spread_ticks": 4,
            "spread_dwell_ms": 5000,
            "max_quote_aggressiveness": "BEHIND_1",
            "cost_buffer_ticks": 2,
            "toxicity_gate": "TRAIN_P90",
            "adverse_width_bound": "CONDITIONAL_TRAIN_P90",
            "fair_model": "LOGODDS_MIDPOINT",
            "inventory_skew_gamma_ticks_per_contract": 0,
            "requote_policy": "R0_SAFETY_ONLY",
            "warmup_ms": 120000,
            "normal_quote_age_ms": 5000,
            "stop_new_risk_tts_ms": 900000,
            "quantity_contracts_per_active_side": 1,
        }
        for name, expected in a01_expected.items():
            if self._frozen(a01, name) != expected:
                raise AdapterContractError(
                    f"A01 frozen parameter drifted: {name}"
                )

        a11 = revisions["A11-ONE-SIDED-PROVISION"]
        if a11["baseline"].get("excluded_alternatives") != [
            "PM_PARENT_PAIRED_ABLATION"
        ]:
            raise AdapterContractError("A11 PM-parent baseline is not excluded")
        a11_parameters = a11["parameter_freeze"]
        a11_expected = {
            "one_sided_persistence_ms": 30000,
            "surviving_side_book_age_max_ms": 1000,
            "reference_mid_onset_age_max_ms": 1000,
            "surviving_side_max_move_ticks": 1,
            "missing_side_offset_k_ticks": 2,
            "quantity_contracts": 1,
            "max_fills_per_root": 1,
            "replenish": False,
            "quote_age_ms": 30000,
            "max_hold_ms": 120000,
        }
        for name, expected in a11_expected.items():
            if self._frozen(a11_parameters, name) != expected:
                raise AdapterContractError(
                    f"A11 OS0 frozen parameter drifted: {name}"
                )

        b09 = revisions["B09-LISTING-TO-START-DRIFT"]
        b09_parameters = b09["parameter_freeze"]
        train = b09_parameters["training_validation_protocol"]
        unresolved = b09_parameters["unresolved_training_outputs"]
        if b09.get("state") != "BLOCKED_PARAMETER_TRAINING":
            raise AdapterContractError("B09 must remain parameter-training blocked")
        if train.get("train_artifact_sha256") is not None:
            raise AdapterContractError(
                "this B09 adapter revision forbids a trained artifact"
            )
        for name in ("admitted_cells", "direction_by_cell"):
            output = unresolved.get(name, {})
            if output.get("frozen") is not None:
                raise AdapterContractError(
                    f"B09 may not prefill {name} before TRAIN"
                )

    @staticmethod
    def _decision(
        *,
        row: NormalizedStateRow,
        revision: Mapping[str, Any],
        bindings: RuntimeBindings,
        status: DecisionStatus,
        reasons: tuple[str, ...],
        intents: tuple[OrderIntent, ...] = (),
    ) -> StrategyDecision:
        return StrategyDecision(
            row_id=row.row_id,
            experiment_id=revision["source_card"]["experiment_id"],
            revision_definition_sha256=revision[
                "revision_definition_sha256"
            ],
            baseline_id=revision["baseline"]["baseline_id"],
            claim_tier=revision["claim_tier"],
            status=status,
            triggered=status is DecisionStatus.ORDER_INTENTS,
            intents=intents,
            reason_codes=reasons,
            runtime_bindings_sha256=bindings.sha256,
        )

    @staticmethod
    def _intent(
        *,
        revision: Mapping[str, Any],
        row: NormalizedStateRow,
        leg: str,
        side: IntentSide,
        price_e4: int,
        expire_after_ms: int,
        max_hold_ms: int | None,
        root_entry_quantity_cap_e4: int,
        replenish: bool,
        reason_code: str,
    ) -> OrderIntent:
        identity = {
            "experiment_id": revision["source_card"]["experiment_id"],
            "revision_definition_sha256": revision[
                "revision_definition_sha256"
            ],
            "row_id": row.row_id,
            "leg": leg,
            "side": side.value,
            "price_e4": price_e4,
        }
        return OrderIntent(
            intent_id=f"intent-{_canonical_sha256(identity)}",
            experiment_id=revision["source_card"]["experiment_id"],
            revision_definition_sha256=revision[
                "revision_definition_sha256"
            ],
            row_id=row.row_id,
            action=IntentAction.PLACE_LIMIT,
            outcome=IntentOutcome.YES,
            side=side,
            price_e4=price_e4,
            quantity_e4=ONE_CONTRACT_E4,
            time_in_force="GTC",
            post_only=True,
            reduce_only=False,
            expire_after_ms=expire_after_ms,
            max_hold_ms=max_hold_ms,
            root_entry_quantity_cap_e4=root_entry_quantity_cap_e4,
            replenish=replenish,
            reason_code=reason_code,
        )

    @staticmethod
    def _invalid_market_reasons(
        row: NormalizedStateRow,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if not row.book_valid:
            reasons.append("ABSTAIN_BOOK_INVALID")
        if not row.lifecycle_open:
            reasons.append("ABSTAIN_LIFECYCLE_NOT_OPEN")
        if row.gap:
            reasons.append("ABSTAIN_CAPTURE_GAP")
        if row.paused:
            reasons.append("ABSTAIN_MARKET_PAUSED")
        if row.locked:
            reasons.append("ABSTAIN_BOOK_LOCKED")
        if row.crossed:
            reasons.append("ABSTAIN_BOOK_CROSSED")
        return tuple(reasons)

    def evaluate_a01(
        self,
        row: NormalizedStateRow,
        bindings: RuntimeBindings,
    ) -> StrategyDecision:
        revision = self._revisions["A01-SPREAD-CAPTURE"]
        params = revision["parameter_freeze"]
        missing = bindings.missing_reasons(
            card_parameter_reason="BLOCK_MISSING_TRAIN_P90_BINDING"
        )
        if missing:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.BLOCKED,
                reasons=missing,
            )
        invalid = self._invalid_market_reasons(row)
        if invalid:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=invalid,
            )
        if row.scheduled_start_ts_ns is None:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.BLOCKED,
                reasons=("BLOCK_SCHEDULED_START_ROW_MISSING",),
            )
        tts_ns = row.scheduled_start_ts_ns - row.decision_ts_ns
        stop_new_risk_ns = (
            self._frozen(params, "stop_new_risk_tts_ms") * NS_PER_MS
        )
        if tts_ns < stop_new_risk_ns:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A01_STOP_NEW_RISK_WINDOW",),
            )
        ttl_ns = self._frozen(params, "book_ttl_ms") * NS_PER_MS
        if row.decision_ts_ns - row.book_observed_at_ns > ttl_ns:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A01_BOOK_STALE",),
            )
        if row.best_yes_bid_e4 is None or row.best_yes_ask_e4 is None:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A01_NOT_TWO_SIDED",),
            )
        if row.best_yes_bid_e4 >= row.best_yes_ask_e4:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A01_NOT_POSITIVE_SPREAD",),
            )
        spread_e4 = row.best_yes_ask_e4 - row.best_yes_bid_e4
        spread_ticks = spread_e4 // row.tick_size_e4
        if (
            spread_e4 % row.tick_size_e4
            or spread_ticks
            < self._frozen(params, "minimum_raw_spread_ticks")
        ):
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A01_SPREAD_BELOW_4_TICKS",),
            )
        if row.state_started_at_ns is None:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.BLOCKED,
                reasons=("BLOCK_A01_SPREAD_DWELL_START_MISSING",),
            )
        dwell_ns = self._frozen(params, "spread_dwell_ms") * NS_PER_MS
        if row.decision_ts_ns - row.state_started_at_ns < dwell_ns:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A01_SPREAD_DWELL_BELOW_5S",),
            )
        if row.warmup_started_at_ns is None:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.BLOCKED,
                reasons=("BLOCK_A01_WARMUP_START_MISSING",),
            )
        warmup_ns = self._frozen(params, "warmup_ms") * NS_PER_MS
        if row.decision_ts_ns - row.warmup_started_at_ns < warmup_ns:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A01_WARMUP_INCOMPLETE",),
            )
        train_gates = (
            row.activity_gate_passed,
            row.toxicity_gate_passed,
            row.adverse_width_gate_passed,
        )
        if any(value is None for value in train_gates):
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.BLOCKED,
                reasons=("BLOCK_A01_TRAIN_GATE_STATE_MISSING",),
            )
        if not all(train_gates):
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A01_TRAIN_P90_GATE_FAILED",),
            )
        if row.net_capture_margin_ticks is None:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.BLOCKED,
                reasons=("BLOCK_A01_NET_MARGIN_MISSING",),
            )
        if row.net_capture_margin_ticks <= self._frozen(
            params, "cost_buffer_ticks"
        ):
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A01_MARGIN_NOT_ABOVE_2_TICKS",),
            )

        bid_price = row.best_yes_bid_e4 - row.tick_size_e4
        ask_price = row.best_yes_ask_e4 + row.tick_size_e4
        if bid_price <= 0 or ask_price >= PRICE_SCALE_E4:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A01_BEHIND1_PRICE_OUT_OF_RANGE",),
            )
        quote_age_ms = self._frozen(params, "normal_quote_age_ms")
        intents = (
            self._intent(
                revision=revision,
                row=row,
                leg="BID_BEHIND_1",
                side=IntentSide.BUY,
                price_e4=bid_price,
                expire_after_ms=quote_age_ms,
                max_hold_ms=None,
                root_entry_quantity_cap_e4=2 * ONE_CONTRACT_E4,
                replenish=False,
                reason_code="A01_PM_TS_CONSERVATIVE_BID_BEHIND_1",
            ),
            self._intent(
                revision=revision,
                row=row,
                leg="ASK_BEHIND_1",
                side=IntentSide.SELL,
                price_e4=ask_price,
                expire_after_ms=quote_age_ms,
                max_hold_ms=None,
                root_entry_quantity_cap_e4=2 * ONE_CONTRACT_E4,
                replenish=False,
                reason_code="A01_PM_TS_CONSERVATIVE_ASK_BEHIND_1",
            ),
        )
        return self._decision(
            row=row,
            revision=revision,
            bindings=bindings,
            status=DecisionStatus.ORDER_INTENTS,
            reasons=("TRIGGER_A01_PM_TS_CONSERVATIVE",),
            intents=intents,
        )

    @staticmethod
    def _ceil_to_tick(value: int, tick: int) -> int:
        return ((value + tick - 1) // tick) * tick

    @staticmethod
    def _floor_to_tick(value: int, tick: int) -> int:
        return (value // tick) * tick

    def evaluate_a11(
        self,
        row: NormalizedStateRow,
        bindings: RuntimeBindings,
    ) -> StrategyDecision:
        revision = self._revisions["A11-ONE-SIDED-PROVISION"]
        params = revision["parameter_freeze"]
        missing = bindings.missing_reasons(
            card_parameter_reason="BLOCK_MISSING_A11_PARAMETER_BINDING"
        )
        if missing:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.BLOCKED,
                reasons=missing,
            )
        invalid = self._invalid_market_reasons(row)
        if invalid:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=invalid,
            )
        if row.sport.casefold() not in {"tennis", "basketball"}:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A11_SPORT_OUTSIDE_TENNIS_BASKETBALL",),
            )
        if row.scheduled_start_ts_ns is None:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.BLOCKED,
                reasons=("BLOCK_SCHEDULED_START_ROW_MISSING",),
            )
        tts_ns = row.scheduled_start_ts_ns - row.decision_ts_ns
        tts_window = self._frozen(params, "tts_window_ms")
        if not (
            tts_window["lower_inclusive"] * NS_PER_MS
            <= tts_ns
            <= tts_window["upper_inclusive"] * NS_PER_MS
        ):
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A11_OUTSIDE_15_TO_240M_PRESTART",),
            )
        book_age_ns = row.decision_ts_ns - row.book_observed_at_ns
        if book_age_ns > (
            self._frozen(params, "surviving_side_book_age_max_ms")
            * NS_PER_MS
        ):
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A11_SURVIVING_BOOK_STALE",),
            )
        bid_only = (
            row.best_yes_bid_e4 is not None
            and row.best_yes_ask_e4 is None
        )
        ask_only = (
            row.best_yes_bid_e4 is None
            and row.best_yes_ask_e4 is not None
        )
        if not (bid_only or ask_only):
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A11_NOT_EXACTLY_ONE_SIDED",),
            )
        if row.state_started_at_ns is None:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.BLOCKED,
                reasons=("BLOCK_A11_ONSET_MISSING",),
            )
        persistence_ns = (
            self._frozen(params, "one_sided_persistence_ms") * NS_PER_MS
        )
        if row.decision_ts_ns - row.state_started_at_ns < persistence_ns:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A11_DWELL_BELOW_30S",),
            )
        if (
            row.reference_mid_onset_e4 is None
            or row.reference_mid_onset_observed_at_ns is None
        ):
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.BLOCKED,
                reasons=("BLOCK_A11_ONSET_ANCHOR_MISSING",),
            )
        anchor_age_at_onset_ns = (
            row.state_started_at_ns
            - row.reference_mid_onset_observed_at_ns
        )
        if anchor_age_at_onset_ns < 0:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.BLOCKED,
                reasons=("BLOCK_A11_ANCHOR_IS_FUTURE_AT_ONSET",),
            )
        if anchor_age_at_onset_ns > (
            self._frozen(params, "reference_mid_onset_age_max_ms")
            * NS_PER_MS
        ):
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A11_ANCHOR_OLDER_THAN_1S_AT_ONSET",),
            )
        price_window = self._frozen(params, "normalized_price_e4")
        if not (
            price_window["lower_inclusive"]
            <= row.reference_mid_onset_e4
            < price_window["upper_exclusive"]
        ):
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A11_OUTSIDE_10_TO_90C",),
            )
        if row.update_count_60s is None or row.trade_count_300s is None:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.BLOCKED,
                reasons=("BLOCK_A11_ACTIVITY_STATE_MISSING",),
            )
        if (
            row.update_count_60s
            < self._frozen(params, "minimum_updates_trailing_60s")
            or row.trade_count_300s
            < self._frozen(params, "minimum_trades_trailing_5m")
        ):
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A11_ACTIVITY_GATE_FAILED",),
            )
        if row.activity_burst is None:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.BLOCKED,
                reasons=("BLOCK_A11_ACTIVITY_BURST_STATE_MISSING",),
            )
        if row.activity_burst:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A11_ACTIVITY_BURST",),
            )
        if row.surviving_side_onset_e4 is None:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.BLOCKED,
                reasons=("BLOCK_A11_SURVIVING_SIDE_ONSET_MISSING",),
            )
        surviving_now = (
            row.best_yes_bid_e4 if bid_only else row.best_yes_ask_e4
        )
        if abs(surviving_now - row.surviving_side_onset_e4) > (
            self._frozen(params, "surviving_side_max_move_ticks")
            * row.tick_size_e4
        ):
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A11_SURVIVING_SIDE_MOVED_GT_1_TICK",),
            )

        k = self._frozen(params, "missing_side_offset_k_ticks")
        if bid_only:
            price_e4 = max(
                row.best_yes_bid_e4 + k * row.tick_size_e4,
                self._ceil_to_tick(
                    row.reference_mid_onset_e4 + row.tick_size_e4,
                    row.tick_size_e4,
                ),
            )
            side = IntentSide.SELL
            leg = "RESTORE_MISSING_ASK_K2"
        else:
            price_e4 = min(
                row.best_yes_ask_e4 - k * row.tick_size_e4,
                self._floor_to_tick(
                    row.reference_mid_onset_e4 - row.tick_size_e4,
                    row.tick_size_e4,
                ),
            )
            side = IntentSide.BUY
            leg = "RESTORE_MISSING_BID_K2"
        if not 0 < price_e4 < PRICE_SCALE_E4:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.ABSTAIN,
                reasons=("ABSTAIN_A11_QUOTE_OUT_OF_RANGE",),
            )
        intent = self._intent(
            revision=revision,
            row=row,
            leg=leg,
            side=side,
            price_e4=price_e4,
            expire_after_ms=self._frozen(params, "quote_age_ms"),
            max_hold_ms=self._frozen(params, "max_hold_ms"),
            root_entry_quantity_cap_e4=(
                self._frozen(params, "max_fills_per_root")
                * ONE_CONTRACT_E4
            ),
            replenish=self._frozen(params, "replenish"),
            reason_code=f"A11_OS0_{leg}",
        )
        return self._decision(
            row=row,
            revision=revision,
            bindings=bindings,
            status=DecisionStatus.ORDER_INTENTS,
            reasons=("TRIGGER_A11_OS0",),
            intents=(intent,),
        )

    def evaluate_b09(
        self,
        row: NormalizedStateRow,
        bindings: RuntimeBindings,
    ) -> StrategyDecision:
        """Return a blocked row; this revision has no TRAIN-bound policy.

        The implementation intentionally never estimates direction, selects a
        cell, or invents a horizon from the evaluation row.
        """

        revision = self._revisions["B09-LISTING-TO-START-DRIFT"]
        train = revision["parameter_freeze"][
            "training_validation_protocol"
        ]
        if train.get("train_artifact_sha256") is None:
            return self._decision(
                row=row,
                revision=revision,
                bindings=bindings,
                status=DecisionStatus.BLOCKED,
                reasons=(
                    "BLOCKED_PARAMETER_TRAINING",
                    "BLOCK_B09_DIRECTION_CELL_HORIZON_SHA_UNBOUND",
                ),
            )
        raise AdapterContractError(
            "a TRAIN-bound B09 policy requires a new pinned adapter revision"
        )

    def evaluate_rows(
        self,
        experiment_id: str,
        rows: Iterable[NormalizedStateRow],
        bindings: RuntimeBindings,
    ) -> tuple[StrategyDecision, ...]:
        evaluators = {
            "A01-SPREAD-CAPTURE": self.evaluate_a01,
            "A11-ONE-SIDED-PROVISION": self.evaluate_a11,
            "B09-LISTING-TO-START-DRIFT": self.evaluate_b09,
        }
        try:
            evaluator = evaluators[experiment_id]
        except KeyError as exc:
            raise AdapterContractError(
                f"unsupported experiment_id {experiment_id!r}"
            ) from exc
        output: list[StrategyDecision] = []
        row_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, NormalizedStateRow):
                raise AdapterContractError(
                    "rows must contain NormalizedStateRow"
                )
            if row.row_id in row_ids:
                raise AdapterContractError("duplicate normalized row_id")
            row_ids.add(row.row_id)
            output.append(evaluator(row, bindings))
        return tuple(output)


__all__ = [
    "AdapterContractError",
    "DecisionStatus",
    "FrozenExperimentAdapters",
    "IntentAction",
    "IntentOutcome",
    "IntentSide",
    "NormalizedStateRow",
    "OrderIntent",
    "RuntimeBindings",
    "StrategyDecision",
]
