#!/usr/bin/env python3
"""Offline, fail-closed end-to-end runner for frozen PnL experiments.

The runner is deliberately a pure research boundary:

* it performs no network, AWS, credential, or order operation;
* frozen adapters create the order intents;
* public maker fills require strict-through evidence and are allocated once;
* every exit is an exact receive-clock L2 IOC or a finalized exact payout;
* every runtime record requires a separately root-pinned extraction lineage;
* fees, real PLACE/CANCEL/IOC_EXIT latency, exact releases, and terminal
  coverage must all be bound before ``NET_PNL_COMPLETE`` can be emitted.

The older C1 strict-fill artifacts remain useful engineering/gross evidence.
They do not contain the fee, measured-latency, and closure truth required to
be promoted to net PnL.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    CashFlowKind,
    FillPurpose,
    FillRecord,
    LiquidityRole,
    Outcome,
    PathSpec,
    Side,
    canonical_json_bytes,
    canonical_sha256,
    exact_trade_notional_e6,
)
from .experiments import (
    AdapterContractError,
    DecisionStatus,
    FrozenExperimentAdapters,
    IntentSide,
    NormalizedStateRow,
    RuntimeBindings,
    StrategyDecision,
)
from .fees import (
    FeePrecision,
    FeeRule,
    FeeSchedule,
    FeeTruthUnavailable,
)
from .fee_facts import (
    FeeFacts,
    FeeFactsUnavailable,
    parse_fee_facts_document,
)
from .fills import (
    FillBatch,
    FillError,
    FillSlice,
    L2Level,
    L2Snapshot,
    MarketableOrder,
    OrderAction,
    OutcomeSide,
    PairedPassiveFillBatch,
    PassiveOrder,
    PublicTrade,
    allocate_paired_passive_strict_fills,
    allocate_passive_strict_fills,
    assert_portfolio_fill_conservation,
    walk_exact_l2_ioc,
)
from .ledger import IncompletePnL, LedgerInvariantError, PnLLedger
from .preflight import NET_READY, evaluate_readiness
from .provenance import (
    ExactReleaseBinding,
    ExactSourceObject,
    ProvenanceError,
    RunBinding,
    atomic_write_receipt,
)
from .risk import (
    RiskInvariantError,
    RiskLedger,
    RiskLimitExceeded,
    RiskLimits,
    RiskRequest,
)
from .terminal import (
    SettlementRecord,
    SettlementStatus,
    classify_settlement,
)


FIXTURE_SCHEMA = "pnl-spine-run-fixture-v2"
RECEIPT_SCHEMA = "pnl-spine-run-receipt-v2"
MAX_FIXTURE_BYTES = 16 * 1024 * 1024
NET_COMPLETE = "NET_PNL_COMPLETE"
PNL_BLOCKED = "PNL_BLOCKED"
PATH_COMPLETE = "PATH_COMPLETE"
C1_CLASSIFICATION = (
    "GROSS_ENGINEERING_ONLY_NOT_NET_PNL:"
    "PUBLIC_STRICT_FILL_WITHOUT_COMPLETE_FEE_REAL_LATENCY_AND_CLOSURE_TRUTH"
)
TRUSTED_AUTHORITY_SCHEMA = "pnl-spine-trusted-authority-v2"
LINEAGE_RECEIPT_SCHEMA = "pnl-spine-lineage-receipt-v1"
HYBRID_LINEAGE_RECEIPT_SCHEMA = "pnl-spine-lineage-receipt-v2"
LINEAGE_RECORD_SCHEMA_SHA256 = canonical_sha256(
    {
        "object_read_key": [
            "release_id",
            "logical_key",
            "version_id",
        ],
        "record_key": ["kind", "record_id"],
        "record_kinds": [
            "L2_SNAPSHOT",
            "NORMALIZED_ROW",
            "PUBLIC_TRADE",
            "SETTLEMENT",
        ],
        "source_member_key": [
            "release_id",
            "logical_key",
            "version_id",
            "locator_schema",
            "locator",
        ],
    }
)
HYBRID_LINEAGE_RECORD_SCHEMA_SHA256 = canonical_sha256(
    {
        "exact_object_read_key": [
            "release_id",
            "logical_key",
            "version_id",
        ],
        "record_key": ["kind", "record_id"],
        "record_kinds": [
            "L2_SNAPSHOT",
            "NORMALIZED_ROW",
            "PUBLIC_TRADE",
            "SETTLEMENT",
        ],
        "source_member_variants": {
            "EXACT_RELEASE_OBJECT": [
                "release_id",
                "logical_key",
                "version_id",
                "locator_schema",
                "locator",
            ],
            "OFFICIAL_TERMINAL_CAPTURE": [
                "shard_id",
                "ticker",
                "capture_receipt_raw_sha256",
                "raw_pins_raw_sha256",
                "normalized_output_raw_sha256",
                "selected_raw_response_sha256",
            ],
        },
        "terminal_temporality": (
            "OBSERVED_AT_FETCH_NOT_HISTORICAL_AS_OF"
        ),
    }
)
TERMINAL_ROOT_RECEIPT_SCHEMA = (
    "pnl-spine-official-terminal-root-receipt-v1"
)
TERMINAL_TEMPORALITY = "OBSERVED_AT_FETCH_NOT_HISTORICAL_AS_OF"


class RunnerContractError(ValueError):
    """The canonical fixture is malformed or internally contradictory."""


def _plain_int(
    name: str,
    value: object,
    *,
    minimum: int | None = None,
) -> int:
    if type(value) is not int:
        raise RunnerContractError(f"{name} must be a plain integer")
    if minimum is not None and value < minimum:
        raise RunnerContractError(f"{name} must be >= {minimum}")
    return value


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RunnerContractError(f"{name} must be a non-empty string")
    return value


def _sha256_text(name: str, value: object) -> str:
    text_value = _text(name, value)
    if (
        len(text_value) != 64
        or any(character not in "0123456789abcdef" for character in text_value)
    ):
        raise RunnerContractError(f"{name} must be lowercase SHA-256")
    return text_value


def _mapping(name: str, value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RunnerContractError(f"{name} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise RunnerContractError(f"{name} keys must be strings")
    return value


def _list(name: str, value: object) -> list[Any]:
    if not isinstance(value, list):
        raise RunnerContractError(f"{name} must be an array")
    return value


def _strict_keys(
    name: str,
    value: Mapping[str, Any],
    allowed: Iterable[str],
    *,
    required: Iterable[str] = (),
) -> None:
    allowed_set = set(allowed)
    unknown = sorted(set(value) - allowed_set)
    missing = sorted(set(required) - set(value))
    if unknown:
        raise RunnerContractError(
            f"{name} has unknown fields: {','.join(unknown)}"
        )
    if missing:
        raise RunnerContractError(
            f"{name} is missing fields: {','.join(missing)}"
        )


def _blocker(code: str, stage: str, detail: str) -> dict[str, str]:
    return {"code": code, "stage": stage, "detail": detail}


def _sorted_blockers(
    blockers: Iterable[Mapping[str, str]],
) -> list[dict[str, str]]:
    unique = {
        (row["stage"], row["code"], row["detail"]): dict(row)
        for row in blockers
    }
    return [unique[key] for key in sorted(unique)]


def _document_sha256(value: Mapping[str, Any]) -> str:
    """Hash an external JSON receipt with its own finite-JSON domain.

    Frozen strategy source cards contain historical decimal metadata.  Those
    values never cross the fixed-point execution boundary, but their exact
    JSON bytes still need an immutable input binding.
    """

    try:
        raw = json.dumps(
            dict(value),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RunnerContractError(
            f"external receipt is not canonical finite JSON: {exc}"
        ) from exc
    return hashlib.sha256(raw).hexdigest()


def _utc_date_from_ns(name: str, value: object) -> str:
    timestamp_ns = _plain_int(name, value, minimum=0)
    try:
        return datetime.fromtimestamp(
            timestamp_ns // 1_000_000_000,
            tz=timezone.utc,
        ).date().isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise RunnerContractError(f"{name} is outside UTC timestamp range") from exc


def _utc_date_from_us(name: str, value: object) -> str:
    timestamp_us = _plain_int(name, value, minimum=0)
    return _utc_date_from_ns(name, timestamp_us * 1_000)


def _parse_risk_policy(
    value: object,
    *,
    expected_sha256: str,
) -> RiskLimits:
    policy = _mapping("risk_policy", value)
    allowed = {
        "schema_version",
        "max_market_e6",
        "max_event_e6",
        "max_factor_e6",
        "max_total_e6",
        "max_daily_loss_e6",
        "policy_sha256",
    }
    _strict_keys("risk_policy", policy, allowed, required=allowed)
    if policy.get("schema_version") != "pnl-spine-risk-policy-v1":
        raise RunnerContractError("unknown risk policy schema")
    payload = dict(policy)
    supplied = _text("policy_sha256", payload.pop("policy_sha256"))
    calculated = canonical_sha256(payload)
    if supplied != calculated or supplied != expected_sha256:
        raise RunnerContractError(
            "risk policy SHA does not match provenance authority"
        )
    return RiskLimits(
        max_market_e6=_plain_int(
            "max_market_e6", policy.get("max_market_e6"), minimum=0
        ),
        max_event_e6=_plain_int(
            "max_event_e6", policy.get("max_event_e6"), minimum=0
        ),
        max_factor_e6=_plain_int(
            "max_factor_e6", policy.get("max_factor_e6"), minimum=0
        ),
        max_total_e6=_plain_int(
            "max_total_e6", policy.get("max_total_e6"), minimum=0
        ),
        max_daily_loss_e6=_plain_int(
            "max_daily_loss_e6",
            policy.get("max_daily_loss_e6"),
            minimum=0,
        ),
    )


def _parse_fee_contexts(
    value: object,
) -> dict[str, tuple[str, str]]:
    contexts: dict[str, tuple[str, str]] = {}
    allowed = {"market_ticker", "series_ticker", "event_ticker"}
    for index, raw in enumerate(_list("fee_contexts", value)):
        row = _mapping(f"fee_contexts[{index}]", raw)
        _strict_keys(
            f"fee_contexts[{index}]",
            row,
            allowed,
            required=allowed,
        )
        market = _text("market_ticker", row.get("market_ticker"))
        if market in contexts:
            raise RunnerContractError("duplicate fee context market")
        contexts[market] = (
            _text("series_ticker", row.get("series_ticker")),
            _text("event_ticker", row.get("event_ticker")),
        )
    if not contexts:
        raise RunnerContractError("fee_contexts cannot be empty")
    return contexts


def _materialize_fee_schedule(
    facts: FeeFacts,
    contexts: Mapping[str, tuple[str, str]],
    *,
    public_trades: Sequence[PublicTrade],
    closure_points: Sequence[tuple[str, int]],
) -> FeeSchedule:
    """Materialize only authority-resolved exact timestamp fee rules."""

    points = {
        (trade.market_ticker, trade.timestamp_us * 1_000)
        for trade in public_trades
    }
    points.update(closure_points)
    rules: dict[str, FeeRule] = {}
    for index, (market_ticker, executed_at_ns) in enumerate(sorted(points)):
        try:
            series_ticker, event_ticker = contexts[market_ticker]
        except KeyError as exc:
            raise FeeFactsUnavailable(
                f"missing fee context for {market_ticker}"
            ) from exc
        probe = FillRecord(
            fill_id=f"fee-authority-probe-{index}",
            order_id=f"fee-authority-probe-{index}",
            path_id="fee-authority-materialization",
            market_ticker=market_ticker,
            outcome=Outcome.YES,
            side=Side.BUY,
            purpose=FillPurpose.ENTRY,
            liquidity_role=LiquidityRole.MAKER,
            quantity_e4=10_000,
            price_e4=5_000,
            executed_at_ns=executed_at_ns,
            source_sha256=facts.facts_sha256,
        )
        schedule = facts.schedule_for_fill(
            probe,
            series_ticker=series_ticker,
            event_ticker=event_ticker,
        )
        for rule in schedule.rules:
            existing = rules.get(rule.rule_id)
            if existing is not None and existing != rule:
                raise FeeFactsUnavailable("fee authority rule-id collision")
            rules[rule.rule_id] = rule
    return FeeSchedule(rules.values())


def _validate_trusted_authority(
    fixture: Mapping[str, Any],
    authority: object,
    *,
    expected_sha256: object,
    trusted_lineage_receipt: object,
) -> str:
    """Verify hashes supplied outside the self-describing run fixture."""

    trusted = _mapping("trusted_authority", authority)
    expected_keys = {
        "schema_version",
        "code_sha256",
        "fee_facts_sha256",
        "fee_contexts_sha256",
        "fee_receipt_sha256",
        "measured_latency_receipt_sha256",
        "release_set_sha256",
        "release_dq_receipt_sha256",
        "evidence_manifest_sha256",
        "closure_manifest_sha256",
        "risk_policy_sha256",
        "terminal_receipt_sha256",
        "lineage_receipt_sha256",
        "extractor_code_sha256",
        "extractor_config_sha256",
    }
    _strict_keys(
        "trusted_authority",
        trusted,
        expected_keys,
        required=expected_keys,
    )
    if trusted.get("schema_version") != TRUSTED_AUTHORITY_SCHEMA:
        raise ProvenanceError("unknown trusted authority schema")
    supplied_pin = _text(
        "expected_trusted_authority_sha256", expected_sha256
    )
    calculated_authority_sha256 = canonical_sha256(trusted)
    if supplied_pin != calculated_authority_sha256:
        raise ProvenanceError(
            "trusted authority canonical SHA does not match external pin"
        )
    provenance = _mapping("provenance", fixture.get("provenance"))
    preflight_inputs = _mapping(
        "preflight_inputs", fixture.get("preflight_inputs")
    )
    lineage_receipt = _mapping(
        "trusted_lineage_receipt", trusted_lineage_receipt
    )
    expected = {
        "code_sha256": _text("code_sha256", fixture.get("code_sha256")),
        "fee_facts_sha256": canonical_sha256(
            _mapping(
                "fee_facts_authority",
                fixture.get("fee_facts_authority"),
            )
        ),
        "fee_contexts_sha256": canonical_sha256(
            _list("fee_contexts", fixture.get("fee_contexts"))
        ),
        "fee_receipt_sha256": _document_sha256(
            _mapping(
                "fee_facts",
                preflight_inputs.get("fee_facts"),
            )
        ),
        "measured_latency_receipt_sha256": _document_sha256(
            _mapping(
                "measured_latency",
                preflight_inputs.get("measured_latency"),
            )
        ),
        "release_set_sha256": canonical_sha256(
            _list("provenance.releases", provenance.get("releases"))
        ),
        "release_dq_receipt_sha256": _document_sha256(
            _mapping(
                "release_dq",
                preflight_inputs.get("release_dq"),
            )
        ),
        "evidence_manifest_sha256": canonical_sha256(
            _list(
                "evidence_bindings",
                fixture.get("evidence_bindings"),
            )
        ),
        "closure_manifest_sha256": canonical_sha256(
            _list("closures", fixture.get("closures"))
        ),
        "risk_policy_sha256": _text(
            "risk_policy_sha256",
            _mapping("risk_policy", fixture.get("risk_policy")).get(
                "policy_sha256"
            ),
        ),
        "terminal_receipt_sha256": _document_sha256(
            _mapping(
                "terminal_coverage",
                preflight_inputs.get("terminal_coverage"),
            )
        ),
        "lineage_receipt_sha256": canonical_sha256(lineage_receipt),
        "extractor_code_sha256": _sha256_text(
            "lineage extractor_code_sha256",
            lineage_receipt.get("extractor_code_sha256"),
        ),
        "extractor_config_sha256": _sha256_text(
            "lineage extractor_config_sha256",
            lineage_receipt.get("extractor_config_sha256"),
        ),
    }
    for field, calculated in expected.items():
        supplied = trusted.get(field)
        if supplied != calculated:
            raise ProvenanceError(
                f"trusted authority mismatch for {field}"
            )
    return calculated_authority_sha256


def _parse_rows(rows: object) -> tuple[NormalizedStateRow, ...]:
    result: list[NormalizedStateRow] = []
    for index, raw in enumerate(_list("rows", rows)):
        row = _mapping(f"rows[{index}]", raw)
        try:
            result.append(NormalizedStateRow(**dict(row)))
        except (TypeError, ValueError) as exc:
            raise RunnerContractError(
                f"rows[{index}] is invalid: {exc}"
            ) from exc
    if not result:
        raise RunnerContractError("rows must contain at least one state")
    return tuple(result)


def _parse_public_trades(
    rows: object,
) -> tuple[tuple[PublicTrade, ...], dict[str, str]]:
    parsed: list[PublicTrade] = []
    sources: dict[str, str] = {}
    allowed = {
        "trade_id",
        "market_ticker",
        "timestamp_us",
        "yes_price_e4",
        "quantity_e4",
        "taker_side",
        "source_sha256",
    }
    for index, raw in enumerate(_list("public_trades", rows)):
        row = _mapping(f"public_trades[{index}]", raw)
        _strict_keys(
            f"public_trades[{index}]",
            row,
            allowed,
            required=allowed,
        )
        trade_id = _text("trade_id", row.get("trade_id"))
        parsed.append(
            PublicTrade(
                trade_id=trade_id,
                market_ticker=_text(
                    "market_ticker", row.get("market_ticker")
                ),
                timestamp_us=_plain_int(
                    "timestamp_us", row.get("timestamp_us"), minimum=0
                ),
                yes_price_e4=_plain_int(
                    "yes_price_e4", row.get("yes_price_e4")
                ),
                quantity_e4=_plain_int(
                    "quantity_e4", row.get("quantity_e4"), minimum=1
                ),
                taker_side=OutcomeSide(
                    _text("taker_side", row.get("taker_side"))
                ),
            )
        )
        sources[trade_id] = _text(
            "source_sha256", row.get("source_sha256")
        )
    return tuple(parsed), sources


def _levels(name: str, value: object) -> tuple[L2Level, ...]:
    result: list[L2Level] = []
    for index, raw in enumerate(_list(name, value)):
        row = _mapping(f"{name}[{index}]", raw)
        _strict_keys(
            f"{name}[{index}]",
            row,
            {"yes_price_e4", "quantity_e4"},
            required={"yes_price_e4", "quantity_e4"},
        )
        result.append(
            L2Level(
                yes_price_e4=_plain_int(
                    "yes_price_e4", row.get("yes_price_e4")
                ),
                quantity_e4=_plain_int(
                    "quantity_e4", row.get("quantity_e4"), minimum=1
                ),
            )
        )
    return tuple(result)


def _parse_snapshots(
    rows: object,
) -> tuple[dict[str, L2Snapshot], dict[str, str], dict[str, int]]:
    snapshots: dict[str, L2Snapshot] = {}
    sources: dict[str, str] = {}
    authority: dict[str, int] = {}
    allowed = {
        "snapshot_id",
        "market_ticker",
        "receive_timestamp_us",
        "yes_bids",
        "yes_asks",
        "book_valid",
        "gap_free",
        "source_sha256",
    }
    required = allowed - {"book_valid", "gap_free"}
    for index, raw in enumerate(_list("exit_snapshots", rows)):
        row = _mapping(f"exit_snapshots[{index}]", raw)
        _strict_keys(
            f"exit_snapshots[{index}]",
            row,
            allowed,
            required=required,
        )
        snapshot_id = _text("snapshot_id", row.get("snapshot_id"))
        if snapshot_id in snapshots:
            raise RunnerContractError("duplicate snapshot_id")
        bids = _levels("yes_bids", row.get("yes_bids"))
        asks = _levels("yes_asks", row.get("yes_asks"))
        snapshot = L2Snapshot(
            snapshot_id=snapshot_id,
            market_ticker=_text(
                "market_ticker", row.get("market_ticker")
            ),
            receive_timestamp_us=_plain_int(
                "receive_timestamp_us",
                row.get("receive_timestamp_us"),
                minimum=0,
            ),
            yes_bids=bids,
            yes_asks=asks,
            book_valid=row.get("book_valid", True),
            gap_free=row.get("gap_free", True),
        )
        snapshots[snapshot_id] = snapshot
        sources[snapshot_id] = _text(
            "source_sha256", row.get("source_sha256")
        )
        for level_index, level in enumerate(bids):
            authority[
                f"{snapshot_id}|level={level_index}"
            ] = level.quantity_e4
        for level_index, level in enumerate(asks):
            # Bid and ask level indices share the fill engine source namespace.
            # A valid exact book cannot be consumed on both sides by the same
            # position-closing order, but separate counterfactual paths can.
            # Bind each side explicitly and translate after the walk.
            authority[
                f"{snapshot_id}|ask-level={level_index}"
            ] = level.quantity_e4
    return snapshots, sources, authority


def _parse_closures(rows: object) -> dict[str, Mapping[str, Any]]:
    closures: dict[str, Mapping[str, Any]] = {}
    allowed = {
        "intent_id",
        "exit_snapshot_id",
        "exit_decision_ts_ns",
        "exit_limit_price_e4",
        "maximum_snapshot_age_us",
        "settlement_id",
    }
    for index, raw in enumerate(_list("closures", rows)):
        row = _mapping(f"closures[{index}]", raw)
        _strict_keys(
            f"closures[{index}]",
            row,
            allowed,
            required={"intent_id"},
        )
        intent_id = _text("intent_id", row.get("intent_id"))
        if intent_id in closures:
            raise RunnerContractError("duplicate closure intent_id")
        exit_fields = (
            "exit_snapshot_id",
            "exit_decision_ts_ns",
            "exit_limit_price_e4",
            "maximum_snapshot_age_us",
        )
        present = [row.get(name) is not None for name in exit_fields]
        if any(present) and not all(present):
            raise RunnerContractError(
                "IOC closure requires snapshot, decision, limit, and max age"
            )
        if not any(present) and row.get("settlement_id") is None:
            raise RunnerContractError(
                "closure requires an exact IOC and/or settlement"
            )
        closures[intent_id] = row
    return closures


def _latest_qualified_snapshot(
    snapshots: Mapping[str, L2Snapshot],
    *,
    market_ticker: str,
    effective_timestamp_us: int,
    maximum_snapshot_age_us: int,
) -> L2Snapshot:
    eligible = [
        snapshot
        for snapshot in snapshots.values()
        if (
            snapshot.market_ticker == market_ticker
            and snapshot.book_valid
            and snapshot.gap_free
            and snapshot.receive_timestamp_us <= effective_timestamp_us
            and (
                effective_timestamp_us - snapshot.receive_timestamp_us
                <= maximum_snapshot_age_us
            )
        )
    ]
    if not eligible:
        raise FillError(
            "no qualified L2 snapshot exists at IOC effective time"
        )
    latest_timestamp = max(
        snapshot.receive_timestamp_us for snapshot in eligible
    )
    latest = [
        snapshot
        for snapshot in eligible
        if snapshot.receive_timestamp_us == latest_timestamp
    ]
    if len(latest) != 1:
        raise FillError(
            "latest qualified L2 snapshot is ambiguous at effective time"
        )
    return latest[0]


def _parse_settlements(rows: object) -> dict[str, Mapping[str, Any]]:
    settlements: dict[str, Mapping[str, Any]] = {}
    finalized_market: dict[str, str] = {}
    allowed = {
        "settlement_id",
        "market_ticker",
        "status",
        "finalized",
        "yes_settlement_value_e4",
        "observed_at_ns",
        "revision",
        "source_sha256",
    }
    for index, raw in enumerate(_list("settlements", rows)):
        row = _mapping(f"settlements[{index}]", raw)
        _strict_keys(
            f"settlements[{index}]",
            row,
            allowed,
            required=allowed,
        )
        settlement_id = _text(
            "settlement_id", row.get("settlement_id")
        )
        if settlement_id in settlements:
            raise RunnerContractError("duplicate settlement_id")
        market_ticker = _text(
            "market_ticker", row.get("market_ticker")
        )
        try:
            status = SettlementStatus(
                _text("status", row.get("status"))
            )
        except ValueError as exc:
            raise RunnerContractError(
                "unknown settlement status"
            ) from exc
        finalized = row.get("finalized")
        if not isinstance(finalized, bool):
            raise RunnerContractError("settlement finalized must be bool")
        if finalized != (status is SettlementStatus.FINALIZED):
            raise RunnerContractError(
                "only FINALIZED status may be finalized settlement authority"
            )
        if finalized:
            if market_ticker in finalized_market:
                raise RunnerContractError(
                    "multiple finalized settlement authorities for one market"
                )
            finalized_market[market_ticker] = settlement_id
        settlements[settlement_id] = row
    return settlements


def _parse_release_binding(row: Mapping[str, Any]) -> ExactReleaseBinding:
    allowed = {
        "release_id",
        "date",
        "manifest_sha256",
        "manifest_version_id",
        "evidence_tier",
        "objects",
    }
    _strict_keys("release", row, allowed, required=allowed)
    objects: list[ExactSourceObject] = []
    object_allowed = {
        "logical_key",
        "version_id",
        "sha256",
        "size_bytes",
        "channel",
        "date",
    }
    for index, raw in enumerate(_list("release.objects", row.get("objects"))):
        item = _mapping(f"release.objects[{index}]", raw)
        _strict_keys(
            f"release.objects[{index}]",
            item,
            object_allowed,
            required=object_allowed,
        )
        objects.append(
            ExactSourceObject(
                logical_key=_text(
                    "logical_key", item.get("logical_key")
                ),
                version_id=_text("version_id", item.get("version_id")),
                sha256=_text("sha256", item.get("sha256")),
                size_bytes=_plain_int(
                    "size_bytes", item.get("size_bytes"), minimum=0
                ),
                channel=_text("channel", item.get("channel")),
                date=_text("date", item.get("date")),
            )
        )
    return ExactReleaseBinding(
        release_id=_text("release_id", row.get("release_id")),
        date=_text("date", row.get("date")),
        manifest_sha256=_text(
            "manifest_sha256", row.get("manifest_sha256")
        ),
        manifest_version_id=_text(
            "manifest_version_id", row.get("manifest_version_id")
        ),
        evidence_tier=_text(
            "evidence_tier", row.get("evidence_tier")
        ),
        objects=tuple(objects),
    )


def _collect_evidence_records(
    fixture: Mapping[str, Any],
) -> dict[
    tuple[str, str],
    tuple[Mapping[str, Any], str, str | None, frozenset[str]],
]:
    records: dict[
        tuple[str, str],
        tuple[Mapping[str, Any], str, str | None, frozenset[str]],
    ] = {}

    def add(
        kind: str,
        record_id: str,
        record: Mapping[str, Any],
        date: str,
        source_sha256: str | None,
        channels: frozenset[str],
    ) -> None:
        key = (kind, record_id)
        if key in records:
            raise ProvenanceError(f"duplicate evidence record {kind}/{record_id}")
        records[key] = (record, date, source_sha256, channels)

    for index, raw in enumerate(_list("rows", fixture.get("rows"))):
        row = _mapping(f"rows[{index}]", raw)
        add(
            "NORMALIZED_ROW",
            _text("row_id", row.get("row_id")),
            row,
            _utc_date_from_ns("decision_ts_ns", row.get("decision_ts_ns")),
            None,
            frozenset({"L1", "L2", "CATALOG"}),
        )
    for index, raw in enumerate(
        _list("public_trades", fixture.get("public_trades"))
    ):
        row = _mapping(f"public_trades[{index}]", raw)
        add(
            "PUBLIC_TRADE",
            _text("trade_id", row.get("trade_id")),
            row,
            _utc_date_from_us("timestamp_us", row.get("timestamp_us")),
            _text("source_sha256", row.get("source_sha256")),
            frozenset({"TRADES"}),
        )
    for index, raw in enumerate(
        _list("exit_snapshots", fixture.get("exit_snapshots"))
    ):
        row = _mapping(f"exit_snapshots[{index}]", raw)
        add(
            "L2_SNAPSHOT",
            _text("snapshot_id", row.get("snapshot_id")),
            row,
            _utc_date_from_us(
                "receive_timestamp_us", row.get("receive_timestamp_us")
            ),
            _text("source_sha256", row.get("source_sha256")),
            frozenset({"L2"}),
        )
    for index, raw in enumerate(
        _list("settlements", fixture.get("settlements"))
    ):
        row = _mapping(f"settlements[{index}]", raw)
        add(
            "SETTLEMENT",
            _text("settlement_id", row.get("settlement_id")),
            row,
            _utc_date_from_ns("observed_at_ns", row.get("observed_at_ns")),
            _text("source_sha256", row.get("source_sha256")),
            frozenset({"SETTLEMENT"}),
        )
    return records


def _validate_evidence_bindings(
    fixture: Mapping[str, Any],
    *,
    releases: Sequence[ExactReleaseBinding],
) -> dict[tuple[str, str], ExactSourceObject | None]:
    """Bind every runtime record to one exact-version source object.

    The binding covers the canonical record bytes, exact release/object
    identity, object digest, source channel and the UTC date implied by the
    record's causal timestamp.  The separately root-pinned lineage receipt
    proves that these fixture claims were extracted from the exact objects.
    """

    records = _collect_evidence_records(fixture)
    release_by_id = {release.release_id: release for release in releases}
    object_index: dict[
        tuple[str, str, str], ExactSourceObject
    ] = {}
    for release in releases:
        for source in release.objects:
            object_index[
                (release.release_id, source.logical_key, source.version_id)
            ] = source

    bound: dict[tuple[str, str], ExactSourceObject | None] = {}
    exact_allowed = {
        "kind",
        "record_id",
        "record_sha256",
        "release_id",
        "source_object_logical_key",
        "source_object_version_id",
        "source_object_sha256",
    }
    external_allowed = {
        "kind",
        "record_id",
        "record_sha256",
        "source_class",
        "market_ticker",
        "source_raw_response_sha256",
    }
    for index, raw in enumerate(
        _list("evidence_bindings", fixture.get("evidence_bindings"))
    ):
        binding = _mapping(f"evidence_bindings[{index}]", raw)
        is_external = "source_class" in binding
        allowed = external_allowed if is_external else exact_allowed
        _strict_keys(
            f"evidence_bindings[{index}]",
            binding,
            allowed,
            required=allowed,
        )
        key = (
            _text("kind", binding.get("kind")),
            _text("record_id", binding.get("record_id")),
        )
        if key in bound:
            raise ProvenanceError(
                f"duplicate evidence binding {key[0]}/{key[1]}"
            )
        try:
            record, record_date, embedded_source, channels = records[key]
        except KeyError as exc:
            raise ProvenanceError(
                f"binding references unknown evidence {key[0]}/{key[1]}"
            ) from exc
        if binding.get("record_sha256") != canonical_sha256(record):
            raise ProvenanceError(
                f"record SHA mismatch for {key[0]}/{key[1]}"
            )
        if is_external:
            if (
                key[0] != "SETTLEMENT"
                or binding.get("source_class")
                != "OFFICIAL_TERMINAL_CAPTURE"
            ):
                raise ProvenanceError(
                    "external evidence is allowed only for official "
                    "SETTLEMENT"
                )
            ticker = _text(
                "external evidence market_ticker",
                binding.get("market_ticker"),
            )
            if record.get("market_ticker") != ticker:
                raise ProvenanceError(
                    "external settlement ticker differs from runtime record"
                )
            source_raw_sha = _sha256_text(
                "external source_raw_response_sha256",
                binding.get("source_raw_response_sha256"),
            )
            if embedded_source != source_raw_sha:
                raise ProvenanceError(
                    "external settlement source SHA differs from runtime "
                    "record"
                )
            bound[key] = None
            continue
        release_id = _text("release_id", binding.get("release_id"))
        try:
            release = release_by_id[release_id]
            source = object_index[
                (
                    release_id,
                    _text(
                        "source_object_logical_key",
                        binding.get("source_object_logical_key"),
                    ),
                    _text(
                        "source_object_version_id",
                        binding.get("source_object_version_id"),
                    ),
                )
            ]
        except KeyError as exc:
            raise ProvenanceError(
                f"binding source is absent from exact release {release_id}"
            ) from exc
        if release.date != record_date or source.date != record_date:
            raise ProvenanceError(
                f"{key[0]}/{key[1]} timestamp date escaped exact release"
            )
        if source.channel not in channels:
            raise ProvenanceError(
                f"{key[0]}/{key[1]} bound to wrong source channel"
            )
        if binding.get("source_object_sha256") != source.sha256:
            raise ProvenanceError(
                f"{key[0]}/{key[1]} source-object SHA mismatch"
            )
        if embedded_source is not None and embedded_source != source.sha256:
            raise ProvenanceError(
                f"{key[0]}/{key[1]} embedded source SHA is not exact object"
            )
        bound[key] = source

    if set(bound) != set(records):
        missing = sorted(set(records) - set(bound))
        extra = sorted(set(bound) - set(records))
        raise ProvenanceError(
            f"evidence bindings are not exhaustive; missing={missing}, extra={extra}"
        )
    return bound


def _validate_terminal_root_receipt_for_lineage(
    value: object,
) -> tuple[str, dict[str, Mapping[str, Any]]]:
    """Validate the immutable, observation-only terminal root in lineage v2."""

    receipt = _mapping("external_terminal_evidence", value)
    allowed = {
        "schema_version",
        "temporality",
        "coverage_policy",
        "required_tickers",
        "required_tickers_sha256",
        "eligible_tickers",
        "eligible_tickers_sha256",
        "eligibility_exclusions",
        "eligibility_exclusions_sha256",
        "adapter_version_policy",
        "shards",
        "shards_sha256",
        "terminal_records_sha256",
        "terminal_record_index",
        "terminal_record_index_sha256",
        "metadata_evidence_sha256",
        "resolved_capabilities",
        "remaining_blockers",
        "historical_point_in_time_metadata_satisfied",
        "scheduled_start_authority_satisfied",
        "historical_lifecycle_intervals_satisfied",
        "network_reads_performed_by_bridge",
        "s3_writes",
        "financial_mutations",
        "payload_sha256",
    }
    _strict_keys(
        "external_terminal_evidence",
        receipt,
        allowed,
        required=allowed,
    )
    if receipt.get("schema_version") != TERMINAL_ROOT_RECEIPT_SCHEMA:
        raise ProvenanceError("unknown external terminal root schema")
    payload = dict(receipt)
    payload_sha = _sha256_text(
        "external terminal payload_sha256",
        payload.pop("payload_sha256"),
    )
    if payload_sha != canonical_sha256(payload):
        raise ProvenanceError("external terminal root self hash mismatch")
    if (
        receipt.get("temporality") != TERMINAL_TEMPORALITY
        or receipt.get("coverage_policy") != "EXACT_REQUIRED_TICKER_SET"
    ):
        raise ProvenanceError(
            "external terminal root weakens temporality or exact coverage"
        )
    if any(
        receipt.get(field) is not False
        for field in (
            "historical_point_in_time_metadata_satisfied",
            "scheduled_start_authority_satisfied",
            "historical_lifecycle_intervals_satisfied",
        )
    ):
        raise ProvenanceError(
            "external terminal root cannot satisfy historical metadata gates"
        )
    if any(
        receipt.get(field) != 0
        for field in (
            "network_reads_performed_by_bridge",
            "s3_writes",
            "financial_mutations",
        )
    ):
        raise ProvenanceError(
            "external terminal root is not an offline read-only bridge"
        )
    required_blockers = [
        "BLOCK_A01_HISTORICAL_POINT_IN_TIME_METADATA_INTERVALS_MISSING",
        "BLOCK_A01_SCHEDULED_START_AUTHORITY_MISSING",
        "BLOCK_A01_EXCHANGE_SETTLEMENT_REVISION_SEQUENCE_UNAVAILABLE",
    ]
    if receipt.get("remaining_blockers") != required_blockers:
        raise ProvenanceError(
            "external terminal root removed a mandatory blocker"
        )
    tickers = [
        _text(f"external terminal ticker[{index}]", ticker)
        for index, ticker in enumerate(
            _list(
                "external terminal required_tickers",
                receipt.get("required_tickers"),
            )
        )
    ]
    if (
        not tickers
        or tickers != sorted(tickers)
        or len(tickers) != len(set(tickers))
        or _sha256_text(
            "external terminal required_tickers_sha256",
            receipt.get("required_tickers_sha256"),
        )
        != canonical_sha256(tickers)
    ):
        raise ProvenanceError(
            "external terminal required ticker coverage is invalid"
        )
    eligible_tickers = [
        _text(f"external eligible ticker[{index}]", ticker)
        for index, ticker in enumerate(
            _list(
                "external terminal eligible_tickers",
                receipt.get("eligible_tickers"),
            )
        )
    ]
    if (
        not eligible_tickers
        or eligible_tickers != sorted(eligible_tickers)
        or len(eligible_tickers) != len(set(eligible_tickers))
        or _sha256_text(
            "external terminal eligible_tickers_sha256",
            receipt.get("eligible_tickers_sha256"),
        )
        != canonical_sha256(eligible_tickers)
    ):
        raise ProvenanceError(
            "external terminal eligible ticker coverage is invalid"
        )
    exclusion_rows = _list(
        "external terminal eligibility_exclusions",
        receipt.get("eligibility_exclusions"),
    )
    if _sha256_text(
        "external terminal eligibility_exclusions_sha256",
        receipt.get("eligibility_exclusions_sha256"),
    ) != canonical_sha256(exclusion_rows):
        raise ProvenanceError(
            "external terminal eligibility exclusion hash mismatch"
        )
    exclusion_tickers: list[str] = []
    for index, raw in enumerate(exclusion_rows):
        exclusion = _mapping(
            f"external terminal eligibility exclusion[{index}]", raw
        )
        ticker = _text(
            "external terminal eligibility exclusion ticker",
            exclusion.get("ticker"),
        )
        if (
            exclusion.get("reason_code")
            != "NON_STANDARD_PRICE_LEVEL_STRUCTURE"
            or exclusion.get("observed_price_level_structure")
            != "tapered_deci_cent"
            or exclusion.get("economic_record_admitted") is not False
            or exclusion.get("temporality") != TERMINAL_TEMPORALITY
        ):
            raise ProvenanceError(
                "external terminal eligibility exclusion semantics drifted"
            )
        controls = _mapping(
            "external terminal eligibility exclusion controls",
            exclusion.get("filesystem_controls"),
        )
        for name in ("authority", "raw_response"):
            fact = _mapping(
                f"external terminal eligibility exclusion {name}",
                controls.get(name),
            )
            if fact.get("root_read_only_verified") is not True:
                raise ProvenanceError(
                    "external terminal eligibility exclusion was not "
                    "root/read-only verified"
                )
        exclusion_tickers.append(ticker)
    if (
        exclusion_tickers != sorted(exclusion_tickers)
        or len(exclusion_tickers) != len(set(exclusion_tickers))
        or sorted(eligible_tickers + exclusion_tickers) != tickers
        or set(eligible_tickers) & set(exclusion_tickers)
    ):
        raise ProvenanceError(
            "external terminal denominator coverage is not exact"
        )
    raw_shards = _list(
        "external terminal shards", receipt.get("shards")
    )
    if (
        not raw_shards
        or _sha256_text(
            "external terminal shards_sha256",
            receipt.get("shards_sha256"),
        )
        != canonical_sha256(raw_shards)
    ):
        raise ProvenanceError("external terminal shard hash mismatch")
    shard_by_id: dict[str, Mapping[str, Any]] = {}
    adapter_pairs: set[tuple[str, str]] = set()
    shard_approvals: list[dict[str, str]] = []
    previous_shard = ""
    for index, raw in enumerate(raw_shards):
        shard = _mapping(f"external terminal shard[{index}]", raw)
        shard_id = _text("external terminal shard_id", shard.get("shard_id"))
        if previous_shard and shard_id <= previous_shard:
            raise ProvenanceError(
                "external terminal shards must be sorted and unique"
            )
        previous_shard = shard_id
        code_sha = _sha256_text(
            "external terminal adapter_code_sha256",
            shard.get("adapter_code_sha256"),
        )
        config_sha = _sha256_text(
            "external terminal adapter_config_sha256",
            shard.get("adapter_config_sha256"),
        )
        adapter_pairs.add((code_sha, config_sha))
        shard_approvals.append(
            {
                "shard_id": shard_id,
                "adapter_code_sha256": code_sha,
                "adapter_config_sha256": config_sha,
            }
        )
        controls = _mapping(
            "external terminal filesystem_controls",
            shard.get("filesystem_controls"),
        )
        for name in (
            "authority",
            "capture_receipt",
            "raw_pins",
            "normalized_output",
        ):
            fact = _mapping(
                f"external terminal filesystem_controls.{name}",
                controls.get(name),
            )
            if fact.get("root_read_only_verified") is not True:
                raise ProvenanceError(
                    "external terminal control was not root/read-only verified"
                )
        for raw_index, raw_fact in enumerate(
            _list(
                "external terminal raw_responses",
                shard.get("raw_responses"),
            )
        ):
            response = _mapping(
                f"external terminal raw response[{raw_index}]", raw_fact
            )
            filesystem = _mapping(
                "external terminal raw response filesystem",
                response.get("filesystem"),
            )
            if filesystem.get("root_read_only_verified") is not True:
                raise ProvenanceError(
                    "external terminal raw response was not root/read-only "
                    "verified"
                )
        shard_by_id[shard_id] = shard
    policy = _mapping(
        "external terminal adapter_version_policy",
        receipt.get("adapter_version_policy"),
    )
    _strict_keys(
        "external terminal adapter_version_policy",
        policy,
        {"mode", "approved_shards"},
        required={"mode", "approved_shards"},
    )
    mode = _text("external terminal adapter version mode", policy.get("mode"))
    approvals = _list(
        "external terminal approved_shards",
        policy.get("approved_shards"),
    )
    if mode == "SINGLE_VERSION_REQUIRED":
        if approvals or len(adapter_pairs) != 1:
            raise ProvenanceError(
                "mixed terminal adapter versions lack explicit approval"
            )
    elif mode == "EXPLICIT_PER_SHARD":
        if approvals != shard_approvals:
            raise ProvenanceError(
                "terminal adapter per-shard approvals are not exact"
            )
    else:
        raise ProvenanceError("unknown terminal adapter version policy")

    raw_index = _list(
        "external terminal record index",
        receipt.get("terminal_record_index"),
    )
    if _sha256_text(
        "external terminal record index SHA",
        receipt.get("terminal_record_index_sha256"),
    ) != canonical_sha256(raw_index):
        raise ProvenanceError("external terminal record index hash mismatch")
    index_by_ticker: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(raw_index):
        row = _mapping(f"external terminal record index[{index}]", raw)
        required_index = {
            "ticker",
            "shard_id",
            "settlement_id",
            "record_sha256",
            "observed_at_ns",
            "selected_raw_response_sha256",
            "adapter_code_sha256",
            "adapter_config_sha256",
            "authority_raw_sha256",
            "capture_receipt_raw_sha256",
            "raw_pins_raw_sha256",
            "normalized_output_raw_sha256",
            "normalized_output_canonical_sha256",
        }
        _strict_keys(
            f"external terminal record index[{index}]",
            row,
            required_index,
            required=required_index,
        )
        ticker = _text("external terminal index ticker", row.get("ticker"))
        if ticker in index_by_ticker:
            raise ProvenanceError(
                "duplicate ticker in external terminal record index"
            )
        try:
            shard = shard_by_id[
                _text("external terminal index shard_id", row.get("shard_id"))
            ]
        except KeyError as exc:
            raise ProvenanceError(
                "external terminal index references an absent shard"
            ) from exc
        for field in (
            "adapter_code_sha256",
            "adapter_config_sha256",
            "authority_raw_sha256",
            "capture_receipt_raw_sha256",
            "raw_pins_raw_sha256",
            "normalized_output_raw_sha256",
            "normalized_output_canonical_sha256",
        ):
            if row.get(field) != shard.get(field):
                raise ProvenanceError(
                    f"external terminal index differs from shard: {field}"
                )
        _sha256_text(
            "external terminal index record SHA",
            row.get("record_sha256"),
        )
        _sha256_text(
            "external terminal selected raw SHA",
            row.get("selected_raw_response_sha256"),
        )
        _plain_int(
            "external terminal observed_at_ns",
            row.get("observed_at_ns"),
            minimum=0,
        )
        index_by_ticker[ticker] = row
    if list(index_by_ticker) != eligible_tickers:
        raise ProvenanceError(
            "external terminal record index is not exact ticker coverage"
        )
    return canonical_sha256(receipt), index_by_ticker


def _validate_lineage_receipt(
    fixture: Mapping[str, Any],
    receipt: object,
    *,
    expected_sha256: object,
) -> str:
    """Validate externally pinned record membership and derivation authority."""

    lineage = _mapping("trusted_lineage_receipt", receipt)
    lineage_schema = lineage.get("schema_version")
    is_hybrid = lineage_schema == HYBRID_LINEAGE_RECEIPT_SCHEMA
    top_allowed = {
        "schema_version",
        "run_id",
        "release_set_sha256",
        "extractor_code_sha256",
        "extractor_config_sha256",
        "record_schema_sha256",
        "object_reads",
        "records",
        "records_sha256",
        "payload_sha256",
    }
    if is_hybrid:
        top_allowed.add("external_terminal_evidence")
    _strict_keys(
        "trusted_lineage_receipt",
        lineage,
        top_allowed,
        required=top_allowed,
    )
    if lineage_schema not in {
        LINEAGE_RECEIPT_SCHEMA,
        HYBRID_LINEAGE_RECEIPT_SCHEMA,
    }:
        raise ProvenanceError("unknown lineage receipt schema")
    calculated_receipt_sha256 = canonical_sha256(lineage)
    if (
        _sha256_text(
            "expected_trusted_lineage_receipt_sha256",
            expected_sha256,
        )
        != calculated_receipt_sha256
    ):
        raise ProvenanceError(
            "lineage receipt canonical SHA does not match external pin"
        )
    payload = dict(lineage)
    supplied_payload_sha256 = _sha256_text(
        "lineage payload_sha256",
        payload.pop("payload_sha256"),
    )
    if supplied_payload_sha256 != canonical_sha256(payload):
        raise ProvenanceError("lineage receipt self hash mismatch")

    run_id = _text("lineage run_id", lineage.get("run_id"))
    if run_id != _text("fixture run_id", fixture.get("run_id")):
        raise ProvenanceError("lineage receipt run_id mismatch")
    extractor_code_sha256 = _sha256_text(
        "extractor_code_sha256",
        lineage.get("extractor_code_sha256"),
    )
    extractor_config_sha256 = _sha256_text(
        "extractor_config_sha256",
        lineage.get("extractor_config_sha256"),
    )
    expected_record_schema = (
        HYBRID_LINEAGE_RECORD_SCHEMA_SHA256
        if is_hybrid
        else LINEAGE_RECORD_SCHEMA_SHA256
    )
    if _sha256_text(
        "record_schema_sha256",
        lineage.get("record_schema_sha256"),
    ) != expected_record_schema:
        raise ProvenanceError("lineage record schema contract drift")
    terminal_root_sha256: str | None = None
    terminal_index: dict[str, Mapping[str, Any]] = {}
    if is_hybrid:
        terminal_root_sha256, terminal_index = (
            _validate_terminal_root_receipt_for_lineage(
                lineage.get("external_terminal_evidence")
            )
        )

    provenance = _mapping("provenance", fixture.get("provenance"))
    release_rows = _list(
        "provenance.releases", provenance.get("releases")
    )
    releases = tuple(
        _parse_release_binding(
            _mapping(f"provenance.releases[{index}]", raw)
        )
        for index, raw in enumerate(release_rows)
    )
    release_set_sha256 = canonical_sha256(release_rows)
    if (
        _sha256_text(
            "lineage release_set_sha256",
            lineage.get("release_set_sha256"),
        )
        != release_set_sha256
    ):
        raise ProvenanceError("lineage release set mismatch")
    exact_objects: dict[
        tuple[str, str, str], ExactSourceObject
    ] = {}
    for release in releases:
        for source in release.objects:
            exact_objects[
                (release.release_id, source.logical_key, source.version_id)
            ] = source

    object_allowed = {
        "release_id",
        "date",
        "logical_key",
        "version_id",
        "channel",
        "source_object_sha256",
        "size_bytes",
        "bytes_verified",
    }
    object_reads: dict[
        tuple[str, str, str], Mapping[str, Any]
    ] = {}
    object_order: list[tuple[str, str, str]] = []
    for index, raw in enumerate(
        _list("lineage object_reads", lineage.get("object_reads"))
    ):
        row = _mapping(f"lineage object_reads[{index}]", raw)
        _strict_keys(
            f"lineage object_reads[{index}]",
            row,
            object_allowed,
            required=object_allowed,
        )
        key = (
            _text("release_id", row.get("release_id")),
            _text("logical_key", row.get("logical_key")),
            _text("version_id", row.get("version_id")),
        )
        if key in object_reads:
            raise ProvenanceError("duplicate lineage object read")
        try:
            source = exact_objects[key]
        except KeyError as exc:
            raise ProvenanceError(
                "lineage object read is absent from exact release"
            ) from exc
        expected_fields: dict[str, object] = {
            "date": source.date,
            "channel": source.channel,
            "source_object_sha256": source.sha256,
            "size_bytes": source.size_bytes,
            "bytes_verified": source.size_bytes,
        }
        for field, expected in expected_fields.items():
            value = row.get(field)
            if field in {"size_bytes", "bytes_verified"}:
                value = _plain_int(field, value, minimum=0)
            elif field == "source_object_sha256":
                value = _sha256_text(field, value)
            else:
                value = _text(field, value)
            if value != expected:
                raise ProvenanceError(
                    f"lineage object read mismatch for {field}"
                )
        object_reads[key] = row
        object_order.append(key)
    if not object_reads:
        raise ProvenanceError("lineage object_reads cannot be empty")
    if object_order != sorted(object_order):
        raise ProvenanceError("lineage object_reads must be sorted")

    record_rows = _list("lineage records", lineage.get("records"))
    if (
        _sha256_text(
            "lineage records_sha256",
            lineage.get("records_sha256"),
        )
        != canonical_sha256(record_rows)
    ):
        raise ProvenanceError("lineage records manifest hash mismatch")
    expected_records = _collect_evidence_records(fixture)
    channel_policy = {
        "NORMALIZED_ROW": frozenset({"L1", "L2", "CATALOG"}),
        "PUBLIC_TRADE": frozenset({"TRADES"}),
        "L2_SNAPSHOT": frozenset({"L2"}),
        "SETTLEMENT": frozenset({"SETTLEMENT"}),
    }
    record_allowed = {
        "kind",
        "record_id",
        "record_sha256",
        "record_date_utc",
        "mode",
        "source_members",
        "transform_code_sha256",
        "transform_config_sha256",
        "input_set_sha256",
    }
    member_allowed = {
        "release_id",
        "date",
        "logical_key",
        "version_id",
        "channel",
        "source_object_sha256",
        "size_bytes",
        "locator_schema",
        "locator",
        "source_record_sha256",
    }
    external_member_allowed = {
        "source_class",
        "shard_id",
        "ticker",
        "channel",
        "temporality",
        "adapter_code_sha256",
        "adapter_config_sha256",
        "authority_raw_sha256",
        "capture_receipt_raw_sha256",
        "raw_pins_raw_sha256",
        "normalized_output_raw_sha256",
        "normalized_output_canonical_sha256",
        "selected_raw_response_sha256",
        "source_record_sha256",
        "terminal_root_receipt_sha256",
    }
    lineage_records: dict[
        tuple[str, str], Mapping[str, Any]
    ] = {}
    lineage_members: dict[
        tuple[str, str], frozenset[tuple[str, ...]]
    ] = {}
    record_order: list[tuple[str, str]] = []
    referenced_objects: set[tuple[str, str, str]] = set()
    referenced_terminal_tickers: set[str] = set()
    for index, raw in enumerate(record_rows):
        row = _mapping(f"lineage records[{index}]", raw)
        _strict_keys(
            f"lineage records[{index}]",
            row,
            record_allowed,
            required=record_allowed,
        )
        kind = _text("kind", row.get("kind"))
        if kind not in channel_policy:
            raise ProvenanceError("unknown lineage record kind")
        key = (kind, _text("record_id", row.get("record_id")))
        if key in lineage_records:
            raise ProvenanceError("duplicate lineage record")
        record_sha256 = _sha256_text(
            "record_sha256", row.get("record_sha256")
        )
        record_date = _text(
            "record_date_utc", row.get("record_date_utc")
        )
        mode = _text("mode", row.get("mode"))
        if mode not in {"DIRECT", "DERIVED"}:
            raise ProvenanceError("unknown lineage record mode")
        if kind == "NORMALIZED_ROW" and mode != "DERIVED":
            raise ProvenanceError(
                "NORMALIZED_ROW lineage must be DERIVED"
            )
        if (
            _sha256_text(
                "transform_code_sha256",
                row.get("transform_code_sha256"),
            )
            != extractor_code_sha256
            or _sha256_text(
                "transform_config_sha256",
                row.get("transform_config_sha256"),
            )
            != extractor_config_sha256
        ):
            raise ProvenanceError("lineage transform authority drift")

        raw_members = _list(
            f"lineage records[{index}].source_members",
            row.get("source_members"),
        )
        if not raw_members:
            raise ProvenanceError("lineage source_members cannot be empty")
        member_order: list[tuple[str, ...]] = []
        membership: set[tuple[str, ...]] = set()
        for member_index, member_raw in enumerate(raw_members):
            member = _mapping(
                (
                    f"lineage records[{index}]."
                    f"source_members[{member_index}]"
                ),
                member_raw,
            )
            if "source_class" in member:
                if not is_hybrid or kind != "SETTLEMENT":
                    raise ProvenanceError(
                        "external lineage member is allowed only for "
                        "hybrid SETTLEMENT"
                    )
                _strict_keys(
                    (
                        f"lineage records[{index}]."
                        f"source_members[{member_index}]"
                    ),
                    member,
                    external_member_allowed,
                    required=external_member_allowed,
                )
                if (
                    member.get("source_class")
                    != "OFFICIAL_TERMINAL_CAPTURE"
                    or member.get("channel") != "SETTLEMENT"
                    or member.get("temporality") != TERMINAL_TEMPORALITY
                ):
                    raise ProvenanceError(
                        "external terminal source semantics drifted"
                    )
                ticker = _text(
                    "external terminal source ticker",
                    member.get("ticker"),
                )
                try:
                    terminal = terminal_index[ticker]
                except KeyError as exc:
                    raise ProvenanceError(
                        "external terminal source ticker is absent from root"
                    ) from exc
                expected_external = {
                    "shard_id": terminal["shard_id"],
                    "adapter_code_sha256": terminal[
                        "adapter_code_sha256"
                    ],
                    "adapter_config_sha256": terminal[
                        "adapter_config_sha256"
                    ],
                    "authority_raw_sha256": terminal[
                        "authority_raw_sha256"
                    ],
                    "capture_receipt_raw_sha256": terminal[
                        "capture_receipt_raw_sha256"
                    ],
                    "raw_pins_raw_sha256": terminal[
                        "raw_pins_raw_sha256"
                    ],
                    "normalized_output_raw_sha256": terminal[
                        "normalized_output_raw_sha256"
                    ],
                    "normalized_output_canonical_sha256": terminal[
                        "normalized_output_canonical_sha256"
                    ],
                    "selected_raw_response_sha256": terminal[
                        "selected_raw_response_sha256"
                    ],
                    "source_record_sha256": terminal["record_sha256"],
                    "terminal_root_receipt_sha256": terminal_root_sha256,
                }
                if any(
                    member.get(field) != expected
                    for field, expected in expected_external.items()
                ):
                    raise ProvenanceError(
                        "external terminal source member differs from root"
                    )
                if (
                    terminal["settlement_id"] != key[1]
                    or terminal["record_sha256"] != record_sha256
                ):
                    raise ProvenanceError(
                        "external terminal index differs from settlement"
                    )
                source_record_sha256 = _sha256_text(
                    "external source_record_sha256",
                    member.get("source_record_sha256"),
                )
                member_key = (
                    "OFFICIAL_TERMINAL_CAPTURE",
                    member["shard_id"],
                    ticker,
                    member["capture_receipt_raw_sha256"],
                    member["raw_pins_raw_sha256"],
                    member["normalized_output_raw_sha256"],
                    member["selected_raw_response_sha256"],
                )
                if member_key in member_order:
                    raise ProvenanceError(
                        "duplicate external terminal source member"
                    )
                member_order.append(member_key)
                membership.add(
                    (
                        "OFFICIAL_TERMINAL_CAPTURE",
                        ticker,
                        member["selected_raw_response_sha256"],
                    )
                )
                referenced_terminal_tickers.add(ticker)
                if (
                    mode != "DIRECT"
                    or source_record_sha256 != record_sha256
                ):
                    raise ProvenanceError(
                        "external terminal lineage must be DIRECT"
                    )
                continue
            _strict_keys(
                (
                    f"lineage records[{index}]."
                    f"source_members[{member_index}]"
                ),
                member,
                member_allowed,
                required=member_allowed,
            )
            object_key = (
                _text("release_id", member.get("release_id")),
                _text("logical_key", member.get("logical_key")),
                _text("version_id", member.get("version_id")),
            )
            try:
                object_read = object_reads[object_key]
            except KeyError as exc:
                raise ProvenanceError(
                    "lineage source member lacks verified object read"
                ) from exc
            member_expected = {
                "date": object_read["date"],
                "channel": object_read["channel"],
                "source_object_sha256": object_read[
                    "source_object_sha256"
                ],
                "size_bytes": object_read["size_bytes"],
            }
            for field, expected in member_expected.items():
                value = member.get(field)
                if field == "size_bytes":
                    value = _plain_int(field, value, minimum=0)
                elif field == "source_object_sha256":
                    value = _sha256_text(field, value)
                else:
                    value = _text(field, value)
                if value != expected:
                    raise ProvenanceError(
                        f"lineage source member mismatch for {field}"
                    )
            if member["date"] != record_date:
                raise ProvenanceError(
                    "lineage record date escaped exact source object"
                )
            if member["channel"] not in channel_policy[kind]:
                raise ProvenanceError(
                    "lineage source member has wrong channel"
                )
            locator_schema = _text(
                "locator_schema", member.get("locator_schema")
            )
            locator = _text("locator", member.get("locator"))
            source_record_sha256 = _sha256_text(
                "source_record_sha256",
                member.get("source_record_sha256"),
            )
            member_key = (
                *object_key,
                locator_schema,
                locator,
            )
            if member_key in member_order:
                raise ProvenanceError(
                    "duplicate lineage source member"
                )
            member_order.append(member_key)
            membership.add(
                (
                    object_key[0],
                    object_key[1],
                    object_key[2],
                    _sha256_text(
                        "source_object_sha256",
                        member.get("source_object_sha256"),
                    ),
                )
            )
            referenced_objects.add(object_key)
            if mode == "DIRECT" and source_record_sha256 != record_sha256:
                raise ProvenanceError(
                    "DIRECT lineage source record hash mismatch"
                )
        if member_order != sorted(member_order):
            raise ProvenanceError(
                "lineage source_members must be sorted"
            )
        if mode == "DIRECT" and len(raw_members) != 1:
            raise ProvenanceError(
                "DIRECT lineage requires exactly one source member"
            )
        if (
            _sha256_text(
                "input_set_sha256", row.get("input_set_sha256")
            )
            != canonical_sha256(raw_members)
        ):
            raise ProvenanceError("lineage input set hash mismatch")
        try:
            runtime_record, runtime_date, _, _ = expected_records[key]
        except KeyError as exc:
            raise ProvenanceError(
                "lineage receipt contains an extra runtime record"
            ) from exc
        if record_sha256 != canonical_sha256(runtime_record):
            raise ProvenanceError("lineage runtime record hash mismatch")
        if record_date != runtime_date:
            raise ProvenanceError("lineage runtime record date mismatch")
        lineage_records[key] = row
        lineage_members[key] = frozenset(membership)
        record_order.append(key)

    if record_order != sorted(record_order):
        raise ProvenanceError("lineage records must be sorted")
    if set(lineage_records) != set(expected_records):
        missing = sorted(set(expected_records) - set(lineage_records))
        extra = sorted(set(lineage_records) - set(expected_records))
        raise ProvenanceError(
            "lineage record set is not exhaustive; "
            f"missing={missing}, extra={extra}"
        )
    if referenced_objects != set(object_reads):
        missing = sorted(referenced_objects - set(object_reads))
        extra = sorted(set(object_reads) - referenced_objects)
        raise ProvenanceError(
            "lineage object read set is not exact; "
            f"missing={missing}, extra={extra}"
        )
    if is_hybrid:
        if referenced_terminal_tickers != set(terminal_index):
            missing = sorted(
                set(terminal_index) - referenced_terminal_tickers
            )
            extra = sorted(
                referenced_terminal_tickers - set(terminal_index)
            )
            raise ProvenanceError(
                "external terminal record use is not exact; "
                f"missing={missing}, extra={extra}"
            )
    elif referenced_terminal_tickers:
        raise ProvenanceError(
            "legacy lineage cannot reference external terminal evidence"
        )

    binding_allowed = {
        "kind",
        "record_id",
        "record_sha256",
        "release_id",
        "source_object_logical_key",
        "source_object_version_id",
        "source_object_sha256",
    }
    external_binding_allowed = {
        "kind",
        "record_id",
        "record_sha256",
        "source_class",
        "market_ticker",
        "source_raw_response_sha256",
    }
    seen_bindings: set[tuple[str, str]] = set()
    for index, raw in enumerate(
        _list("evidence_bindings", fixture.get("evidence_bindings"))
    ):
        binding = _mapping(f"evidence_bindings[{index}]", raw)
        is_external_binding = "source_class" in binding
        allowed_binding = (
            external_binding_allowed
            if is_external_binding
            else binding_allowed
        )
        _strict_keys(
            f"evidence_bindings[{index}]",
            binding,
            allowed_binding,
            required=allowed_binding,
        )
        key = (
            _text("kind", binding.get("kind")),
            _text("record_id", binding.get("record_id")),
        )
        if key in seen_bindings:
            raise ProvenanceError("duplicate fixture evidence binding")
        try:
            lineage_record = lineage_records[key]
        except KeyError as exc:
            raise ProvenanceError(
                "fixture evidence binding is absent from lineage"
            ) from exc
        if binding.get("record_sha256") != lineage_record["record_sha256"]:
            raise ProvenanceError(
                "fixture evidence record SHA disagrees with lineage"
            )
        if is_external_binding:
            if (
                not is_hybrid
                or key[0] != "SETTLEMENT"
                or binding.get("source_class")
                != "OFFICIAL_TERMINAL_CAPTURE"
            ):
                raise ProvenanceError(
                    "external fixture binding is hybrid SETTLEMENT-only"
                )
            source_identity = (
                "OFFICIAL_TERMINAL_CAPTURE",
                _text(
                    "external binding market_ticker",
                    binding.get("market_ticker"),
                ),
                _sha256_text(
                    "external binding source_raw_response_sha256",
                    binding.get("source_raw_response_sha256"),
                ),
            )
        else:
            source_identity = (
                _text("release_id", binding.get("release_id")),
                _text(
                    "source_object_logical_key",
                    binding.get("source_object_logical_key"),
                ),
                _text(
                    "source_object_version_id",
                    binding.get("source_object_version_id"),
                ),
                _sha256_text(
                    "source_object_sha256",
                    binding.get("source_object_sha256"),
                ),
            )
        if source_identity not in lineage_members[key]:
            raise ProvenanceError(
                "fixture evidence source disagrees with lineage"
            )
        seen_bindings.add(key)
    if seen_bindings != set(lineage_records):
        raise ProvenanceError(
            "fixture evidence bindings are not exhaustive against lineage"
        )
    return calculated_receipt_sha256


def _make_run_binding(
    fixture: Mapping[str, Any],
    *,
    fee_facts_sha256: str,
    latency_receipt_sha256: str,
    terminal_receipt_sha256: str,
) -> RunBinding:
    provenance = _mapping("provenance", fixture.get("provenance"))
    allowed = {
        "risk_policy_sha256",
        "terminal_contract_sha256",
        "releases",
    }
    _strict_keys("provenance", provenance, allowed, required=allowed)
    releases = tuple(
        _parse_release_binding(
            _mapping(f"provenance.releases[{index}]", raw)
        )
        for index, raw in enumerate(
            _list("provenance.releases", provenance.get("releases"))
        )
    )
    freeze = _mapping(
        "frozen_experiment", fixture.get("frozen_experiment")
    )
    binding = RunBinding(
        schema_version="pnl-spine-run-binding-v1",
        run_id=_text("run_id", fixture.get("run_id")),
        code_sha256=_text("code_sha256", fixture.get("code_sha256")),
        frozen_experiment_sha256=_text(
            "freeze_sha256", freeze.get("freeze_sha256")
        ),
        fee_facts_sha256=fee_facts_sha256,
        latency_receipt_sha256=latency_receipt_sha256,
        risk_policy_sha256=_text(
            "risk_policy_sha256",
            provenance.get("risk_policy_sha256"),
        ),
        terminal_contract_sha256=_text(
            "terminal_contract_sha256",
            provenance.get("terminal_contract_sha256"),
        ),
        releases=releases,
    )
    if binding.terminal_contract_sha256 != terminal_receipt_sha256:
        raise ProvenanceError(
            "terminal_contract_sha256 does not bind terminal receipt"
        )
    release_dq = _mapping(
        "release_dq",
        _mapping("preflight_inputs", fixture.get("preflight_inputs")).get(
            "release_dq"
        ),
    )
    dq_rows = _list("release_dq.dates", release_dq.get("dates"))
    expected = {
        (
            _text("release_id", row.get("release_id")),
            _text("date", row.get("date")),
            _text("manifest_version_id", row.get("manifest_version_id")),
            _text("manifest_sha256", row.get("manifest_sha256")),
            _text("evidence_tier", row.get("evidence_tier")),
        )
        for row in (
            _mapping(f"release_dq.dates[{index}]", raw)
            for index, raw in enumerate(dq_rows)
        )
    }
    supplied = {
        (
            release.release_id,
            release.date,
            release.manifest_version_id,
            release.manifest_sha256,
            release.evidence_tier,
        )
        for release in releases
    }
    if supplied != expected:
        raise ProvenanceError(
            "exact provenance releases do not match release-DQ receipt"
        )
    return binding


def _latency_p99_ns(document: Mapping[str, Any]) -> dict[str, int]:
    if document.get("measurement_mode") != "REAL_ORDER_MEASURED":
        raise RunnerContractError(
            "latency measurement_mode is not REAL_ORDER_MEASURED"
        )
    grouped: dict[str, list[int]] = {
        "PLACE": [],
        "CANCEL": [],
        "IOC_EXIT": [],
    }
    for raw in _list("measured_latency.samples", document.get("samples")):
        sample = _mapping("latency sample", raw)
        path = sample.get("path")
        if path not in grouped:
            continue
        decision = _plain_int(
            "decision_ns", sample.get("decision_ns"), minimum=0
        )
        effective = _plain_int(
            "effective_ns", sample.get("effective_ns"), minimum=0
        )
        if effective <= decision:
            raise RunnerContractError("latency sample is not positive causal")
        grouped[path].append(effective - decision)
    result: dict[str, int] = {}
    for path, durations in grouped.items():
        if not durations:
            raise RunnerContractError(
                f"real latency receipt has no {path} observations"
            )
        ordered = sorted(durations)
        rank = (len(ordered) * 99 + 99) // 100
        result[path] = ordered[rank - 1]
    return result


def _ceil_ns_to_us(value_ns: int) -> int:
    return (value_ns + 999) // 1000


def _intent_outcome_and_price(
    side: IntentSide,
    yes_price_e4: int,
) -> tuple[OutcomeSide, int]:
    if side is IntentSide.BUY:
        return OutcomeSide.YES, yes_price_e4
    # Selling YES is represented as purchasing the complementary NO
    # position.  This keeps both collateral and settlement cash semantics
    # inside the long-only binary-outcome ledger.
    return OutcomeSide.NO, 10_000 - yes_price_e4


def _path_id(run_id: str, row_id: str, suffix: str) -> str:
    identity = {
        "run_id": run_id,
        "row_id": row_id,
        "suffix": suffix,
    }
    return f"path-{canonical_sha256(identity)}"


def _path_spec(
    *,
    path_id: str,
    decision: StrategyDecision,
    row: NormalizedStateRow,
    outcome: Outcome,
    run_binding_sha256: str,
) -> PathSpec:
    return PathSpec(
        path_id=path_id,
        experiment_id=decision.experiment_id,
        baseline_id=decision.baseline_id,
        definition_sha256=decision.revision_definition_sha256,
        release_sha256=run_binding_sha256,
        market_ticker=row.market_ticker,
        event_ticker=row.root_event_id,
        factor_key=f"{row.sport}:{row.root_event_id}",
        outcome=outcome,
    )


def _slice_to_fill(
    fill: FillSlice,
    *,
    path_id: str,
    purpose: FillPurpose,
    source_sha256: str,
) -> FillRecord:
    return FillRecord(
        fill_id=fill.fill_id,
        order_id=fill.order_id,
        path_id=path_id,
        market_ticker=fill.market_ticker,
        outcome=Outcome(fill.side.value),
        side=(
            Side.BUY if fill.action is OrderAction.BUY else Side.SELL
        ),
        purpose=purpose,
        liquidity_role=LiquidityRole(fill.liquidity_role.value),
        quantity_e4=fill.quantity_e4,
        price_e4=fill.price_e4,
        executed_at_ns=fill.timestamp_us * 1000,
        source_sha256=source_sha256,
    )


def _settlement_record(
    raw: Mapping[str, Any],
    *,
    outcome: Outcome,
) -> SettlementRecord:
    yes_value = raw.get("yes_settlement_value_e4")
    settlement_value = (
        None
        if yes_value is None
        else _plain_int(
            "yes_settlement_value_e4", yes_value, minimum=0
        )
    )
    if settlement_value is not None and outcome is Outcome.NO:
        settlement_value = 10_000 - settlement_value
    return SettlementRecord(
        settlement_id=_text(
            "settlement_id", raw.get("settlement_id")
        ),
        market_ticker=_text(
            "market_ticker", raw.get("market_ticker")
        ),
        status=SettlementStatus(
            _text("status", raw.get("status"))
        ),
        finalized=raw.get("finalized"),
        settlement_value_e4=settlement_value,
        observed_at_ns=_plain_int(
            "observed_at_ns", raw.get("observed_at_ns"), minimum=0
        ),
        revision=_plain_int(
            "revision", raw.get("revision"), minimum=0
        ),
        source_sha256=_text(
            "source_sha256", raw.get("source_sha256")
        ),
    )


def _zero_result_row(
    *,
    ledger: PnLLedger,
    row_id: str,
    kind: str,
    decision: StrategyDecision,
    occurred_at_ns: int,
    source_sha256: str,
    reason_codes: Sequence[str],
) -> dict[str, Any]:
    ledger.close_no_position(
        closure_id=f"{kind.lower()}:{row_id}",
        occurred_at_ns=occurred_at_ns,
        source_sha256=source_sha256,
    )
    result = ledger.finalize()
    return {
        "row_id": row_id,
        "kind": kind,
        "path_id": result.path_id,
        "decision_status": decision.status.value,
        "reason_codes": list(reason_codes),
        "state": PATH_COMPLETE,
        "result": _result_mapping(result),
        "residual_quantity_e4": 0,
    }


def _result_mapping(result: Any) -> dict[str, Any]:
    value = asdict(result)
    value["closure_state"] = result.closure_state.value
    return value


def _blocked_row(
    *,
    row_id: str,
    kind: str,
    path_id: str,
    decision_status: str,
    reason_codes: Sequence[str],
    residual_quantity_e4: int = 0,
    diagnostic: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "row_id": row_id,
        "kind": kind,
        "path_id": path_id,
        "decision_status": decision_status,
        "reason_codes": list(reason_codes),
        "state": PNL_BLOCKED,
        "result": None,
        "residual_quantity_e4": residual_quantity_e4,
        "diagnostic": dict(diagnostic or {}),
    }


def _lot_matched_realized_pnl(
    *,
    pnl_ledger: PnLLedger,
    entry_fills: Sequence[FillSlice],
    exit_fills: Sequence[FillSlice],
    terminal_decision: Any,
    result: Mapping[str, Any],
) -> tuple[tuple[int, int, str, str], ...]:
    """Attribute realized NetPnL to exact exit/settlement times.

    Entry principal and entry fees are matched FIFO to closing quantity.
    A partial-lot allocation must be exactly representable in MoneyE6; the
    runner refuses to invent a rounding convention.  Slice totals must
    reconcile exactly to the finalized path result.
    """

    cash_by_fill: dict[str, int] = {}
    settlement_cashflows: list[Any] = []
    for cashflow in pnl_ledger.cashflows:
        if cashflow.kind in {
            CashFlowKind.TRADE_PRINCIPAL,
            CashFlowKind.FEE,
        }:
            if cashflow.fill_id is None:
                raise RiskInvariantError(
                    "trade/fee cashflow lacks fill identity"
                )
            cash_by_fill[cashflow.fill_id] = (
                cash_by_fill.get(cashflow.fill_id, 0)
                + cashflow.amount_e6
            )
        elif cashflow.kind is CashFlowKind.SETTLEMENT:
            settlement_cashflows.append(cashflow)
        elif cashflow.kind is CashFlowKind.VARIABLE_COST:
            raise RiskInvariantError(
                "variable-cost realized-time attribution is unavailable"
            )
        elif cashflow.kind is not CashFlowKind.NO_POSITION_CLOSE:
            raise RiskInvariantError(
                f"unsupported cashflow kind {cashflow.kind.value}"
            )

    entry_ids = {fill.fill_id for fill in entry_fills}
    exit_ids = {fill.fill_id for fill in exit_fills}
    if set(cash_by_fill) != entry_ids | exit_ids:
        raise RiskInvariantError(
            "cashflow fill identities do not match entry/exit evidence"
        )
    lots = [
        {
            "remaining_quantity_e4": fill.quantity_e4,
            "remaining_cash_e6": cash_by_fill[fill.fill_id],
        }
        for fill in sorted(
            entry_fills,
            key=lambda fill: (
                fill.timestamp_us,
                fill.fill_id,
            ),
        )
    ]

    def consume_entry_basis(quantity_e4: int) -> int:
        remaining = quantity_e4
        allocated_cash_e6 = 0
        for lot in lots:
            if remaining == 0:
                break
            lot_quantity = lot["remaining_quantity_e4"]
            if lot_quantity == 0:
                continue
            take = min(remaining, lot_quantity)
            if take == lot_quantity:
                allocation = lot["remaining_cash_e6"]
            else:
                numerator = lot["remaining_cash_e6"] * take
                if numerator % lot_quantity:
                    raise RiskInvariantError(
                        "partial entry basis is not exactly MoneyE6 representable"
                    )
                allocation = numerator // lot_quantity
            lot["remaining_quantity_e4"] -= take
            lot["remaining_cash_e6"] -= allocation
            allocated_cash_e6 += allocation
            remaining -= take
        if remaining:
            raise RiskInvariantError(
                "closing quantity exceeds FIFO entry basis"
            )
        return allocated_cash_e6

    result_sha256 = _text(
        "result_sha256",
        result.get("result_sha256"),
    )
    slices: list[tuple[int, int, str, str]] = []
    for fill in sorted(
        exit_fills,
        key=lambda fill: (
            fill.timestamp_us,
            fill.fill_id,
        ),
    ):
        pnl_e6 = (
            consume_entry_basis(fill.quantity_e4)
            + cash_by_fill[fill.fill_id]
        )
        occurred_at_ns = fill.timestamp_us * 1_000
        source_sha256 = canonical_sha256(
            {
                "result_sha256": result_sha256,
                "closure_kind": "EXIT",
                "closure_id": fill.fill_id,
                "occurred_at_ns": occurred_at_ns,
                "realized_pnl_e6": pnl_e6,
            }
        )
        slices.append(
            (
                occurred_at_ns,
                pnl_e6,
                source_sha256,
                f"exit:{fill.fill_id}",
            )
        )

    remaining_quantity_e4 = sum(
        lot["remaining_quantity_e4"] for lot in lots
    )
    if remaining_quantity_e4:
        if terminal_decision is None or not terminal_decision.closes_position:
            raise RiskInvariantError(
                "remaining entry basis lacks finalized settlement"
            )
        if len(settlement_cashflows) != 1:
            raise RiskInvariantError(
                "settled path must contain one settlement cashflow"
            )
        remaining_basis_e6 = consume_entry_basis(
            remaining_quantity_e4
        )
        settlement_cashflow = settlement_cashflows[0]
        pnl_e6 = remaining_basis_e6 + settlement_cashflow.amount_e6
        occurred_at_ns = terminal_decision.record.observed_at_ns
        source_sha256 = canonical_sha256(
            {
                "result_sha256": result_sha256,
                "closure_kind": "SETTLEMENT",
                "closure_id": terminal_decision.record.settlement_id,
                "occurred_at_ns": occurred_at_ns,
                "realized_pnl_e6": pnl_e6,
            }
        )
        slices.append(
            (
                occurred_at_ns,
                pnl_e6,
                source_sha256,
                (
                    "settlement:"
                    f"{terminal_decision.record.settlement_id}"
                ),
            )
        )
    elif settlement_cashflows:
        raise RiskInvariantError(
            "flat exit path contains an unused settlement cashflow"
        )

    if any(
        lot["remaining_quantity_e4"] or lot["remaining_cash_e6"]
        for lot in lots
    ):
        raise RiskInvariantError("FIFO entry basis did not fully reconcile")
    expected_net_pnl_e6 = _plain_int(
        "net_pnl_e6",
        result.get("net_pnl_e6"),
    )
    if sum(row[1] for row in slices) != expected_net_pnl_e6:
        raise RiskInvariantError(
            "timed realized PnL slices do not reconcile to path result"
        )
    return tuple(slices)


def _a01_cashflow_realized_pnl_slices(
    *,
    pnl_ledger: PnLLedger,
    result: Any,
    exit_fill_ids: set[str],
) -> tuple[dict[str, Any], ...]:
    """Attribute A01 PnL at each causal cashflow instead of final closure.

    Principal that opens exposure becomes FIFO basis and realizes zero at that
    instant.  Opposing principal realizes its allocated basis immediately.
    Fees and variable costs realize at their own cashflow timestamps, while a
    later exact settlement realizes only the still-open basis.  Every partial
    allocation must be exactly representable in MoneyE6.
    """

    lots: list[dict[str, int]] = []
    slices: list[dict[str, Any]] = []

    def append_slice(
        cashflow: Any,
        *,
        kind: str,
        fill_phase: str | None,
        basis_consumed_e6: int,
        basis_deferred_e6: int,
        realized_pnl_e6: int,
    ) -> None:
        payload: dict[str, Any] = {
            "slice_id": f"realized:{cashflow.event_id}",
            "path_id": pnl_ledger.path.path_id,
            "cashflow_event_id": cashflow.event_id,
            "occurred_at_ns": cashflow.occurred_at_ns,
            # Existing RiskLedger ordering is EXIT/SETTLEMENT=10,
            # REALIZED_PNL=20, RESERVE=30, FILL=40.
            "causal_priority": 20,
            "kind": kind,
            "fill_phase": fill_phase,
            "cashflow_amount_e6": cashflow.amount_e6,
            "basis_consumed_e6": basis_consumed_e6,
            "basis_deferred_e6": basis_deferred_e6,
            "realized_pnl_e6": realized_pnl_e6,
            "source_sha256": cashflow.source_sha256,
        }
        payload["slice_sha256"] = canonical_sha256(payload)
        slices.append(payload)

    def consume_basis(quantity_e4: int, sign: int) -> int:
        remaining = quantity_e4
        basis_e6 = 0
        for lot in lots:
            lot_quantity_e4 = lot["quantity_e4"]
            if lot_quantity_e4 == 0:
                continue
            if (1 if lot_quantity_e4 > 0 else -1) != sign:
                continue
            available_e4 = abs(lot_quantity_e4)
            take_e4 = min(remaining, available_e4)
            if take_e4 == available_e4:
                allocation_e6 = lot["cash_e6"]
            else:
                numerator = lot["cash_e6"] * take_e4
                if numerator % available_e4:
                    raise RiskInvariantError(
                        "A01 partial FIFO basis is not exactly MoneyE6 "
                        "representable"
                    )
                allocation_e6 = numerator // available_e4
            lot["quantity_e4"] -= sign * take_e4
            lot["cash_e6"] -= allocation_e6
            basis_e6 += allocation_e6
            remaining -= take_e4
            if remaining == 0:
                break
        if remaining:
            raise RiskInvariantError(
                "A01 closing cashflow exceeds FIFO position basis"
            )
        return basis_e6

    for cashflow in pnl_ledger.cashflows:
        if cashflow.kind is CashFlowKind.TRADE_PRINCIPAL:
            if cashflow.fill_id is None:
                raise RiskInvariantError(
                    "A01 principal cashflow lacks fill identity"
                )
            delta_e4 = cashflow.position_delta_e4
            if delta_e4 == 0:
                raise RiskInvariantError(
                    "A01 principal cashflow has zero position delta"
                )
            net_before_e4 = sum(
                lot["quantity_e4"] for lot in lots
            )
            delta_sign = 1 if delta_e4 > 0 else -1
            fill_phase = (
                "EXIT"
                if cashflow.fill_id in exit_fill_ids
                else "ENTRY"
            )
            if net_before_e4 == 0 or (
                (1 if net_before_e4 > 0 else -1) == delta_sign
            ):
                lots.append(
                    {
                        "quantity_e4": delta_e4,
                        "cash_e6": cashflow.amount_e6,
                    }
                )
                append_slice(
                    cashflow,
                    kind="TRADE_PRINCIPAL",
                    fill_phase=fill_phase,
                    basis_consumed_e6=0,
                    basis_deferred_e6=cashflow.amount_e6,
                    realized_pnl_e6=0,
                )
                continue

            closing_e4 = min(abs(delta_e4), abs(net_before_e4))
            if closing_e4 == abs(delta_e4):
                closing_cash_e6 = cashflow.amount_e6
            else:
                numerator = cashflow.amount_e6 * closing_e4
                if numerator % abs(delta_e4):
                    raise RiskInvariantError(
                        "A01 partial closing principal is not exactly "
                        "MoneyE6 representable"
                    )
                closing_cash_e6 = numerator // abs(delta_e4)
            basis_e6 = consume_basis(
                closing_e4,
                1 if net_before_e4 > 0 else -1,
            )
            remaining_e4 = abs(delta_e4) - closing_e4
            deferred_e6 = cashflow.amount_e6 - closing_cash_e6
            if remaining_e4:
                lots.append(
                    {
                        "quantity_e4": delta_sign * remaining_e4,
                        "cash_e6": deferred_e6,
                    }
                )
            elif deferred_e6:
                raise RiskInvariantError(
                    "A01 flat principal retains unallocated cash basis"
                )
            append_slice(
                cashflow,
                kind="TRADE_PRINCIPAL",
                fill_phase=fill_phase,
                basis_consumed_e6=basis_e6,
                basis_deferred_e6=deferred_e6,
                realized_pnl_e6=closing_cash_e6 + basis_e6,
            )
        elif cashflow.kind is CashFlowKind.FEE:
            append_slice(
                cashflow,
                kind="FEE",
                fill_phase=(
                    "EXIT"
                    if cashflow.fill_id in exit_fill_ids
                    else "ENTRY"
                ),
                basis_consumed_e6=0,
                basis_deferred_e6=0,
                realized_pnl_e6=cashflow.amount_e6,
            )
        elif cashflow.kind is CashFlowKind.VARIABLE_COST:
            append_slice(
                cashflow,
                kind="VARIABLE_COST",
                fill_phase=None,
                basis_consumed_e6=0,
                basis_deferred_e6=0,
                realized_pnl_e6=cashflow.amount_e6,
            )
        elif cashflow.kind is CashFlowKind.SETTLEMENT:
            open_quantity_e4 = sum(
                lot["quantity_e4"] for lot in lots
            )
            if open_quantity_e4 == 0:
                raise RiskInvariantError(
                    "A01 settlement has no FIFO position basis"
                )
            basis_e6 = consume_basis(
                abs(open_quantity_e4),
                1 if open_quantity_e4 > 0 else -1,
            )
            append_slice(
                cashflow,
                kind="SETTLEMENT",
                fill_phase=None,
                basis_consumed_e6=basis_e6,
                basis_deferred_e6=0,
                realized_pnl_e6=cashflow.amount_e6 + basis_e6,
            )
        elif cashflow.kind is CashFlowKind.NO_POSITION_CLOSE:
            if cashflow.amount_e6 or cashflow.position_delta_e4:
                raise RiskInvariantError(
                    "A01 no-position close is not a zero receipt"
                )
        else:
            raise RiskInvariantError(
                f"unsupported A01 cashflow kind {cashflow.kind.value}"
            )

    if any(lot["quantity_e4"] or lot["cash_e6"] for lot in lots):
        raise RiskInvariantError(
            "A01 realized-PnL replay retains FIFO basis"
        )
    if sum(
        int(slice_row["realized_pnl_e6"]) for slice_row in slices
    ) != result.net_pnl_e6:
        raise RiskInvariantError(
            "A01 realized-PnL slices do not reconcile to final net PnL"
        )
    fee_realized_e6 = sum(
        int(slice_row["realized_pnl_e6"])
        for slice_row in slices
        if slice_row["kind"] == "FEE"
    )
    if fee_realized_e6 != -result.fee_cost_e6:
        raise RiskInvariantError(
            "A01 fee slices do not reconcile at cashflow time"
        )
    variable_realized_e6 = sum(
        int(slice_row["realized_pnl_e6"])
        for slice_row in slices
        if slice_row["kind"] == "VARIABLE_COST"
    )
    if variable_realized_e6 != -result.variable_cost_e6:
        raise RiskInvariantError(
            "A01 variable-cost slices do not reconcile at cashflow time"
        )
    gross_realized_e6 = sum(
        int(slice_row["realized_pnl_e6"])
        for slice_row in slices
        if slice_row["kind"] in {"TRADE_PRINCIPAL", "SETTLEMENT"}
    )
    if gross_realized_e6 != result.gross_pnl_e6:
        raise RiskInvariantError(
            "A01 principal/settlement slices do not reconcile gross PnL"
        )
    return tuple(slices)


def _replay_risk_lifecycle(
    *,
    limits: RiskLimits,
    run_id: str,
    intent_context: Mapping[
        str,
        tuple[StrategyDecision, NormalizedStateRow, Any],
    ],
    excluded_intents: set[str],
    entry_by_order: Mapping[str, Sequence[FillSlice]],
    trade_sources: Mapping[str, str],
    exit_batches: Mapping[str, FillBatch],
    snapshot_sources: Mapping[str, str],
    closures: Mapping[str, Mapping[str, Any]],
    settlements: Mapping[str, Mapping[str, Any]],
    pnl_ledgers: Mapping[str, PnLLedger],
    result_by_path: Mapping[str, Mapping[str, Any]],
    latency: Mapping[str, int],
) -> tuple[RiskLedger, list[dict[str, str]]]:
    """Replay exact risk events in causal time order.

    Fill allocation is computed before this replay.  If replay says an order
    should not have been admitted, the allocation would have to be recomputed.
    The runner therefore blocks the whole run instead of assuming the same
    public trade or L2 allocation remains available.
    """

    ledger = RiskLedger(limits)
    blockers: list[dict[str, str]] = []
    events: list[
        tuple[int, int, str, str, str, Mapping[str, Any]]
    ] = []
    expected_reservations: set[str] = set()

    def add(
        occurred_at_ns: int,
        priority: int,
        event_id: str,
        kind: str,
        reservation_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        events.append(
            (
                occurred_at_ns,
                priority,
                event_id,
                kind,
                reservation_id,
                payload,
            )
        )

    for intent_id, (decision, row, intent) in intent_context.items():
        if intent_id in excluded_intents:
            continue
        path_id = _path_id(run_id, row.row_id, intent_id)
        _, price_e4 = _intent_outcome_and_price(
            intent.side,
            intent.price_e4,
        )
        request = RiskRequest(
            reservation_id=intent_id,
            path_id=path_id,
            market_ticker=row.market_ticker,
            event_ticker=row.root_event_id,
            factor_key=f"{row.sport}:{row.root_event_id}",
            quantity_e4=intent.quantity_e4,
            worst_case_loss_e6=exact_trade_notional_e6(
                price_e4,
                intent.quantity_e4,
            ),
            requested_at_ns=row.decision_ts_ns,
            source_sha256=decision.sha256,
        )
        expected_reservations.add(intent_id)
        # Effective closure and realized loss at an equal timestamp must be
        # known before a new decision is admitted.
        add(
            row.decision_ts_ns,
            30,
            f"reserve:{intent_id}",
            "RESERVE",
            intent_id,
            {"request": request},
        )

        entry_fills = tuple(entry_by_order.get(intent_id, ()))
        entry_quantity_e4 = sum(fill.quantity_e4 for fill in entry_fills)
        for fill in entry_fills:
            add(
                fill.timestamp_us * 1_000,
                40,
                f"risk-entry:{fill.fill_id}",
                "FILL",
                intent_id,
                {
                    "quantity_e4": fill.quantity_e4,
                    "source_sha256": trade_sources[fill.source_id],
                },
            )
        unfilled_quantity_e4 = intent.quantity_e4 - entry_quantity_e4
        if unfilled_quantity_e4 < 0:
            blockers.append(
                _blocker(
                    "RISK_LIFECYCLE_REPLAY_FAILED",
                    "RISK",
                    f"{intent_id}: entry fills exceed reserved quantity",
                )
            )
            continue
        if unfilled_quantity_e4:
            add(
                (
                    row.decision_ts_ns
                    + intent.expire_after_ms * 1_000_000
                    + latency["CANCEL"]
                ),
                50,
                f"risk-cancel-unfilled:{intent_id}",
                "CANCEL",
                intent_id,
                {
                    "quantity_e4": unfilled_quantity_e4,
                    "source_sha256": decision.sha256,
                },
            )

        batch = exit_batches.get(path_id)
        exit_fills = tuple(batch.fills) if batch is not None else ()
        exit_quantity_e4 = sum(fill.quantity_e4 for fill in exit_fills)
        closure = closures.get(intent_id)
        for fill in exit_fills:
            if closure is None:
                blockers.append(
                    _blocker(
                        "RISK_LIFECYCLE_REPLAY_FAILED",
                        "RISK",
                        f"{intent_id}: exit fill has no closure authority",
                    )
                )
                continue
            snapshot_id = _text(
                "exit_snapshot_id",
                closure.get("exit_snapshot_id"),
            )
            add(
                fill.timestamp_us * 1_000,
                10,
                f"risk-exit:{fill.fill_id}",
                "EXIT",
                intent_id,
                {
                    "quantity_e4": fill.quantity_e4,
                    "source_sha256": snapshot_sources[snapshot_id],
                },
            )

        residual_quantity_e4 = entry_quantity_e4 - exit_quantity_e4
        terminal_decision = None
        if (
            residual_quantity_e4 > 0
            and closure is not None
            and closure.get("settlement_id") is not None
        ):
            settlement_id = _text(
                "settlement_id",
                closure.get("settlement_id"),
            )
            outcome_side, _ = _intent_outcome_and_price(
                intent.side,
                intent.price_e4,
            )
            record = _settlement_record(
                settlements[settlement_id],
                outcome=Outcome(outcome_side.value),
            )
            terminal_decision = classify_settlement(record)
            add(
                record.observed_at_ns,
                10,
                f"risk-settlement:{intent_id}:{settlement_id}",
                "SETTLEMENT",
                intent_id,
                {"decision": terminal_decision},
            )

        result = result_by_path.get(path_id)
        if result is not None and entry_quantity_e4 > 0:
            try:
                pnl_ledger = pnl_ledgers[path_id]
                realized_slices = _lot_matched_realized_pnl(
                    pnl_ledger=pnl_ledger,
                    entry_fills=entry_fills,
                    exit_fills=exit_fills,
                    terminal_decision=terminal_decision,
                    result=result,
                )
            except (
                KeyError,
                RiskInvariantError,
                RunnerContractError,
                ValueError,
            ) as exc:
                blockers.append(
                    _blocker(
                        "RISK_REALIZED_PNL_ATTRIBUTION_FAILED",
                        "RISK",
                        f"{intent_id}: {exc}",
                    )
                )
                continue
            for (
                realized_at_ns,
                pnl_e6,
                source_sha256,
                slice_id,
            ) in realized_slices:
                add(
                    realized_at_ns,
                    20,
                    f"risk-realized:{path_id}:{slice_id}",
                    "REALIZED_PNL",
                    intent_id,
                    {
                        "path_id": path_id,
                        "pnl_e6": pnl_e6,
                        "source_sha256": source_sha256,
                    },
                )

    rejected_reservations: set[str] = set()
    admitted_reservations: set[str] = set()
    for (
        occurred_at_ns,
        _,
        event_id,
        kind,
        reservation_id,
        payload,
    ) in sorted(events, key=lambda event: event[:3]):
        if reservation_id in rejected_reservations:
            continue
        try:
            if kind == "RESERVE":
                ledger.reserve(
                    payload["request"],
                    event_id=event_id,
                )
                admitted_reservations.add(reservation_id)
            elif kind == "FILL":
                ledger.record_fill(
                    reservation_id,
                    quantity_e4=payload["quantity_e4"],
                    event_id=event_id,
                    occurred_at_ns=occurred_at_ns,
                    source_sha256=payload["source_sha256"],
                )
            elif kind == "CANCEL":
                ledger.record_cancel(
                    reservation_id,
                    quantity_e4=payload["quantity_e4"],
                    event_id=event_id,
                    occurred_at_ns=occurred_at_ns,
                    source_sha256=payload["source_sha256"],
                )
            elif kind == "EXIT":
                ledger.record_exit(
                    reservation_id,
                    quantity_e4=payload["quantity_e4"],
                    event_id=event_id,
                    occurred_at_ns=occurred_at_ns,
                    source_sha256=payload["source_sha256"],
                )
            elif kind == "SETTLEMENT":
                ledger.record_settlement(
                    reservation_id,
                    decision=payload["decision"],
                    event_id=event_id,
                )
            elif kind == "REALIZED_PNL":
                ledger.record_realized_pnl(
                    path_id=payload["path_id"],
                    pnl_e6=payload["pnl_e6"],
                    event_id=event_id,
                    occurred_at_ns=occurred_at_ns,
                    source_sha256=payload["source_sha256"],
                )
            else:
                raise RiskInvariantError(
                    f"unknown risk replay event {kind}"
                )
        except RiskLimitExceeded as exc:
            rejected_reservations.add(reservation_id)
            code = (
                "RISK_DAILY_LOSS_CHANGED_ADMISSION"
                if "daily realized loss gate" in str(exc)
                else "RISK_LIMIT_CHANGED_ADMISSION"
            )
            blockers.append(
                _blocker(
                    code,
                    "RISK",
                    (
                        f"{reservation_id}: {exc}; preallocated fills "
                        "cannot be safely reused"
                    ),
                )
            )
        except (
            KeyError,
            RiskInvariantError,
            RunnerContractError,
            ValueError,
        ) as exc:
            blockers.append(
                _blocker(
                    "RISK_LIFECYCLE_REPLAY_FAILED",
                    "RISK",
                    f"{reservation_id}/{event_id}: {exc}",
                )
            )
            rejected_reservations.add(reservation_id)

    missing_reservations = (
        expected_reservations
        - admitted_reservations
        - rejected_reservations
    )
    if missing_reservations:
        blockers.append(
            _blocker(
                "RISK_LIFECYCLE_REPLAY_FAILED",
                "RISK",
                "unreplayed reservations: "
                + ",".join(sorted(missing_reservations)),
            )
        )
    for reservation_id in sorted(admitted_reservations):
        if reservation_id in rejected_reservations:
            continue
        try:
            snapshot = ledger.reservation(reservation_id)
        except RiskInvariantError as exc:
            blockers.append(
                _blocker(
                    "RISK_LIFECYCLE_REPLAY_FAILED",
                    "RISK",
                    f"{reservation_id}: {exc}",
                )
            )
            continue
        if snapshot.held_risk_e6 != 0:
            blockers.append(
                _blocker(
                    "RISK_LIFECYCLE_OPEN_EXPOSURE",
                    "RISK",
                    (
                        f"{reservation_id}: held_risk_e6="
                        f"{snapshot.held_risk_e6}"
                    ),
                )
            )
    return ledger, blockers


def _receipt(
    *,
    run_id: str,
    experiment_id: str,
    state: str,
    run_binding_sha256: str | None,
    preflight: Mapping[str, Any],
    path_rows: Sequence[Mapping[str, Any]],
    blockers: Iterable[Mapping[str, str]],
    conservation: Mapping[str, int],
    risk_ledger_sha256: str | None = None,
    trusted_authority_sha256: str | None = None,
    trusted_lineage_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    complete_results = [
        row["result"]
        for row in path_rows
        if row.get("state") == PATH_COMPLETE
        and isinstance(row.get("result"), Mapping)
    ]
    totals: dict[str, int] | None = None
    if state == NET_COMPLETE:
        totals = {
            field: sum(int(result[field]) for result in complete_results)
            for field in (
                "gross_pnl_e6",
                "fee_cost_e6",
                "variable_cost_e6",
                "net_pnl_e6",
            )
        }
        if totals["net_pnl_e6"] != (
            totals["gross_pnl_e6"]
            - totals["fee_cost_e6"]
            - totals["variable_cost_e6"]
        ):
            raise LedgerInvariantError("run-level PnL identity failed")
    payload: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "run_id": run_id,
        "experiment_id": experiment_id,
        "state": state,
        "claim_tier": preflight.get("claim_tier"),
        "promotion_allowed": preflight.get("promotion_allowed"),
        "run_binding_sha256": run_binding_sha256,
        "preflight": dict(preflight),
        "path_rows": list(path_rows),
        "conservation": dict(conservation),
        "risk_ledger_sha256": risk_ledger_sha256,
        "trusted_authority_sha256": trusted_authority_sha256,
        "trusted_lineage_receipt_sha256": (
            trusted_lineage_receipt_sha256
        ),
        "totals": totals,
        "blockers": _sorted_blockers(blockers),
        "c1_prior_artifact_classification": C1_CLASSIFICATION,
    }
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


def _a01_strategy_path_id(
    run_id: str,
    row: NormalizedStateRow,
) -> str:
    return _path_id(run_id, row.row_id, "STRATEGY")


def _a01_fill_response_contract(
    frozen_experiment: Mapping[str, Any],
) -> None:
    """Require the exact frozen semantics before interpreting paired fills."""

    revisions = _list(
        "frozen_experiment.revisions",
        frozen_experiment.get("revisions"),
    )
    matches = [
        _mapping("A01 revision", row)
        for row in revisions
        if (
            isinstance(row, Mapping)
            and isinstance(row.get("source_card"), Mapping)
            and row["source_card"].get("experiment_id")
            == "A01-SPREAD-CAPTURE"
        )
    ]
    if len(matches) != 1:
        raise RunnerContractError(
            "A01 requires exactly one frozen revision"
        )
    revision = matches[0]
    params = _mapping(
        "A01 parameter_freeze",
        revision.get("parameter_freeze"),
    )
    fill_response = _mapping(
        "A01 fill_response",
        params.get("fill_response"),
    )
    if fill_response.get("frozen") != (
        "STOP_NEW_RISK_CANCEL_SIBLINGS_RECONCILE_REDUCE_ONLY_IOC"
    ):
        raise RunnerContractError(
            "A01 fill_response is absent or semantically ambiguous"
        )
    validation = _mapping(
        "A01 validation_contract",
        revision.get("validation_contract"),
    )
    invariants = _list(
        "A01 required_invariants",
        validation.get("required_invariants"),
    )
    if set(invariants) != {
        "ONE_CONTRACT_PER_ACTIVE_SIDE",
        "NON_ATOMIC_PAIR_USES_WORST_SEQUENCE_RESERVE",
        "ANY_FILL_STOPS_NEW_RISK_AND_CANCELS_SIBLINGS",
        "EXIT_USES_RECONCILED_EXACT_POSITION_AND_EFFECTIVE_TIME_L2",
        "ZERO_TRIGGER_AND_ZERO_FILL_ROOTS_RETAINED",
    }:
        raise RunnerContractError(
            "A01 validation invariants are incomplete or drifted"
        )
    execution = _mapping(
        "execution_contract",
        frozen_experiment.get("execution_contract"),
    )
    expected_execution = {
        "same_timestamp_ordering": "ADVERSE_EVENT_FIRST",
        "public_volume_allocation": (
            "ALLOCATE_EACH_PUBLIC_PRINT_ONCE_GLOBALLY_ACROSS_ALL_ORDERS"
        ),
        "passive_order_tif": "GTC",
        "passive_post_only": True,
        "forced_exit_tif": "IOC",
        "forced_exit_post_only": False,
        "forced_exit_reduce_only": True,
        "exit_price": "EFFECTIVE_TIME_EXACT_L2_WALK",
        "collateral_reserve_mode": "SLICE_AWARE_WORST_SEQUENCE",
    }
    for field, expected_value in expected_execution.items():
        if execution.get(field) != expected_value:
            raise RunnerContractError(
                f"A01 execution contract drifted: {field}"
            )


def _a01_yes_equivalent_fill(
    fill: FillSlice,
    *,
    path_id: str,
    purpose: FillPurpose,
    source_sha256: str,
) -> FillRecord:
    """Map a complementary NO purchase to its exact SELL-YES equivalent."""

    if fill.side is OutcomeSide.YES:
        side = Side.BUY if fill.action is OrderAction.BUY else Side.SELL
        price_e4 = fill.price_e4
    elif fill.action is OrderAction.BUY:
        side = Side.SELL
        price_e4 = 10_000 - fill.price_e4
    else:
        side = Side.BUY
        price_e4 = 10_000 - fill.price_e4
    return FillRecord(
        fill_id=fill.fill_id,
        order_id=fill.order_id,
        path_id=path_id,
        market_ticker=fill.market_ticker,
        outcome=Outcome.YES,
        side=side,
        purpose=purpose,
        liquidity_role=LiquidityRole(fill.liquidity_role.value),
        quantity_e4=fill.quantity_e4,
        price_e4=price_e4,
        executed_at_ns=fill.timestamp_us * 1_000,
        source_sha256=source_sha256,
    )


def _a01_common_closure(
    intents: Sequence[Any],
    closures: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    rows = [
        closures[intent.intent_id]
        for intent in intents
        if intent.intent_id in closures
    ]
    if not rows:
        return None
    if len(rows) != len(intents):
        raise RunnerContractError(
            "A01 nonzero path requires identical closure authority for both legs"
        )
    payloads = [
        {
            key: value
            for key, value in row.items()
            if key != "intent_id"
        }
        for row in rows
    ]
    if any(payload != payloads[0] for payload in payloads[1:]):
        raise RunnerContractError(
            "A01 paired legs carry conflicting closure authority"
        )
    return rows[0]


def _a01_actionable_closure(
    intents: Sequence[Any],
    closures: Mapping[str, Mapping[str, Any]],
) -> bool:
    return any(
        value is not None
        for intent in intents
        for key, value in closures.get(intent.intent_id, {}).items()
        if key != "intent_id"
    )


def _allocate_a01_groups(
    *,
    decisions: Sequence[StrategyDecision],
    row_by_id: Mapping[str, NormalizedStateRow],
    passive_orders: Sequence[PassiveOrder],
    risk_rejected_intents: set[str],
    public_trades: Sequence[PublicTrade],
    cancel_latency_ns: int,
    blockers: list[dict[str, str]],
) -> tuple[dict[str, PairedPassiveFillBatch], FillBatch]:
    canonical_trades = tuple(
        sorted(
            public_trades,
            key=lambda row: (
                row.market_ticker,
                row.timestamp_us,
                row.trade_id,
            ),
        )
    )
    if canonical_trades != tuple(public_trades):
        raise FillError("public trade tape is not canonically ordered")
    order_by_id = {order.order_id: order for order in passive_orders}
    paired_by_row: dict[str, PairedPassiveFillBatch] = {}
    allocated_source_ids: set[str] = set()
    all_fills: list[FillSlice] = []
    ordered_total_e4 = 0
    for decision in sorted(
        decisions,
        key=lambda item: (
            row_by_id[item.row_id].decision_ts_ns,
            item.row_id,
        ),
    ):
        if decision.status is not DecisionStatus.ORDER_INTENTS:
            continue
        row = row_by_id[decision.row_id]
        intents = tuple(decision.intents)
        if (
            len(intents) != 2
            or {intent.side for intent in intents}
            != {IntentSide.BUY, IntentSide.SELL}
            or any(intent.time_in_force != "GTC" for intent in intents)
            or any(not intent.post_only for intent in intents)
            or any(intent.reduce_only for intent in intents)
            or len({intent.quantity_e4 for intent in intents}) != 1
            or len({intent.expire_after_ms for intent in intents}) != 1
        ):
            blockers.append(
                _blocker(
                    "A01_PAIRED_INTENT_CONTRACT_INVALID",
                    "EXECUTION",
                    row.row_id,
                )
            )
            continue
        if any(
            intent.intent_id in risk_rejected_intents
            for intent in intents
        ):
            continue
        try:
            orders = tuple(order_by_id[intent.intent_id] for intent in intents)
            available = tuple(
                trade
                for trade in public_trades
                if trade.trade_id not in allocated_source_ids
            )
            paired = allocate_paired_passive_strict_fills(
                orders,
                available,
                group_id=row.row_id,
                cancel_latency_ns=cancel_latency_ns,
            )
        except (FillError, KeyError, ValueError) as exc:
            blockers.append(
                _blocker(
                    "ENTRY_FILL_AUTHORITY_FAILED",
                    "FILL",
                    f"{row.row_id}: {exc}",
                )
            )
            continue
        paired_by_row[row.row_id] = paired
        ordered_total_e4 += paired.batch.ordered_e4
        for fill in paired.batch.fills:
            allocated_source_ids.add(fill.source_id)
            all_fills.append(fill)

    filled_total_e4 = sum(fill.quantity_e4 for fill in all_fills)
    batch = FillBatch(
        fills=tuple(
            sorted(
                all_fills,
                key=lambda fill: (
                    fill.timestamp_us,
                    fill.source_id,
                    fill.order_id,
                ),
            )
        ),
        ordered_e4=ordered_total_e4,
        filled_e4=filled_total_e4,
        unfilled_e4=ordered_total_e4 - filled_total_e4,
        source_quantity_e4=sum(
            trade.quantity_e4 for trade in public_trades
        ),
        consumed_source_quantity_e4=filled_total_e4,
        duplicate_source_allocations=0,
    )
    assert_portfolio_fill_conservation(
        (batch,),
        authoritative_source_quantity={
            trade.trade_id: trade.quantity_e4 for trade in public_trades
        },
    )
    return paired_by_row, batch


def _execute_a01_strategy_path(
    *,
    run_id: str,
    decision: StrategyDecision,
    row: NormalizedStateRow,
    paired: PairedPassiveFillBatch,
    trade_sources: Mapping[str, str],
    snapshots: Mapping[str, L2Snapshot],
    snapshot_sources: Mapping[str, str],
    closures: Mapping[str, Mapping[str, Any]],
    settlements: Mapping[str, Mapping[str, Any]],
    fee_schedule: FeeSchedule,
    ioc_exit_latency_ns: int,
    run_binding_sha256: str,
) -> tuple[
    dict[str, Any],
    FillBatch | None,
    dict[str, str],
    dict[str, Any],
]:
    intents = tuple(decision.intents)
    path_id = _a01_strategy_path_id(run_id, row)
    ledger = PnLLedger(
        _path_spec(
            path_id=path_id,
            decision=decision,
            row=row,
            outcome=Outcome.YES,
            run_binding_sha256=run_binding_sha256,
        ),
        fee_schedule,
    )
    outcome_price_by_intent: dict[str, int] = {}
    for intent in intents:
        _, outcome_price_e4 = _intent_outcome_and_price(
            intent.side,
            intent.price_e4,
        )
        outcome_price_by_intent[intent.intent_id] = outcome_price_e4
    reserved_at_risk_e6 = sum(
        exact_trade_notional_e6(
            outcome_price_by_intent[intent.intent_id],
            intent.quantity_e4,
        )
        for intent in intents
    )
    cancel_effective_ns = paired.cancel_effective_us * 1_000
    if not paired.batch.fills:
        if _a01_actionable_closure(intents, closures):
            raise RunnerContractError(
                "no-fill A01 path must not manufacture closure activity"
            )
        path_row = _zero_result_row(
            ledger=ledger,
            row_id=row.row_id,
            kind="STRATEGY",
            decision=decision,
            occurred_at_ns=cancel_effective_ns,
            source_sha256=decision.sha256,
            reason_codes=("TRIGGERED_NO_STRICT_FILL",),
        )
        path_row["realized_pnl_slices"] = []
        return (
            path_row,
            None,
            {},
            {
                "row_id": row.row_id,
                "decision_ts_ns": row.decision_ts_ns,
                "market_ticker": row.market_ticker,
                "event_ticker": row.root_event_id,
                "factor_key": f"{row.sport}:{row.root_event_id}",
                "ordered_quantity_e4": paired.batch.ordered_e4,
                "filled_quantity_e4": 0,
                "canceled_quantity_e4": paired.batch.ordered_e4,
                "paired_reconciled_quantity_e4": 0,
                "net_before_exit_e4": 0,
                "ioc_exit_quantity_e4": 0,
                "settled_quantity_e4": 0,
                "held_quantity_e4": 0,
                "reserved_at_risk_e6": reserved_at_risk_e6,
                "canceled_reserve_release_e6": reserved_at_risk_e6,
                "raw_filled_acquisition_e6": 0,
                "yes_equivalent_collateral_release_e6": 0,
                "yes_equivalent_entry_principal_e6": 0,
                "held_at_risk_e6": 0,
                "first_fill_us": None,
                "cancel_effective_us": paired.cancel_effective_us,
                "realized_at_ns": cancel_effective_ns,
            },
        )

    fills = tuple(
        sorted(
            paired.batch.fills,
            key=lambda fill: (
                fill.timestamp_us,
                fill.source_id,
                fill.order_id,
            ),
        )
    )
    for fill in fills:
        ledger.record_fill(
            _a01_yes_equivalent_fill(
                fill,
                path_id=path_id,
                purpose=FillPurpose.ENTRY,
                source_sha256=trade_sources[fill.source_id],
            ),
            allow_paired_entry_netting=True,
        )
    yes_filled_e4 = sum(
        fill.quantity_e4
        for fill in fills
        if fill.side is OutcomeSide.YES
    )
    no_filled_e4 = sum(
        fill.quantity_e4
        for fill in fills
        if fill.side is OutcomeSide.NO
    )
    net_before_exit_e4 = ledger.position_e4
    if net_before_exit_e4 != yes_filled_e4 - no_filled_e4:
        raise LedgerInvariantError(
            "YES-equivalent entry reconciliation failed"
        )

    exit_batch: FillBatch | None = None
    exit_alias: dict[str, str] = {}
    selected_snapshot_id: str | None = None
    exit_filled_e4 = 0
    settled_e4 = 0
    if net_before_exit_e4 == 0:
        if _a01_actionable_closure(intents, closures):
            raise RunnerContractError(
                "flat A01 path must not manufacture IOC or settlement"
            )
        ledger.close_reconciled_flat(
            reconciliation_id=f"a01-flat:{row.row_id}",
            occurred_at_ns=cancel_effective_ns,
            source_sha256=canonical_sha256(
                {
                    "decision_sha256": decision.sha256,
                    "fill_ids": [fill.fill_id for fill in fills],
                    "cancel_effective_ns": cancel_effective_ns,
                    "net_position_e4": 0,
                }
            ),
        )
    else:
        closure = _a01_common_closure(intents, closures)
        if closure is None:
            raise RunnerContractError(
                "nonzero A01 path requires post-reconcile IOC authority"
            )
        exit_decision_ns = _plain_int(
            "exit_decision_ts_ns",
            closure.get("exit_decision_ts_ns"),
            minimum=0,
        )
        if exit_decision_ns != cancel_effective_ns:
            raise RunnerContractError(
                "A01 exit decision must equal safety-cancel effective time"
            )
        maximum_snapshot_age_us = _plain_int(
            "maximum_snapshot_age_us",
            closure.get("maximum_snapshot_age_us"),
            minimum=0,
        )
        effective_us = _ceil_ns_to_us(
            exit_decision_ns + ioc_exit_latency_ns
        )
        selected_snapshot_id = _text(
            "exit_snapshot_id",
            closure.get("exit_snapshot_id"),
        )
        snapshot = _latest_qualified_snapshot(
            snapshots,
            market_ticker=row.market_ticker,
            effective_timestamp_us=effective_us,
            maximum_snapshot_age_us=maximum_snapshot_age_us,
        )
        if snapshot.snapshot_id != selected_snapshot_id:
            raise FillError(
                "closure did not select the latest qualified L2 snapshot"
            )
        exit_order = MarketableOrder(
            order_id=f"a01:{row.row_id}:reduce-only-exit",
            experiment_id="A01-SPREAD-CAPTURE",
            root_id=row.root_event_id,
            market_ticker=row.market_ticker,
            side=OutcomeSide.YES,
            action=(
                OrderAction.SELL
                if net_before_exit_e4 > 0
                else OrderAction.BUY
            ),
            limit_price_e4=_plain_int(
                "exit_limit_price_e4",
                closure.get("exit_limit_price_e4"),
            ),
            quantity_e4=abs(net_before_exit_e4),
            effective_timestamp_us=effective_us,
        )
        exit_batch = walk_exact_l2_ioc(
            exit_order,
            snapshot,
            maximum_snapshot_age_us=maximum_snapshot_age_us,
        )
        for fill in exit_batch.fills:
            level_number = fill.source_id.rsplit("=", 1)[1]
            exit_alias[fill.fill_id] = (
                f"{selected_snapshot_id}|ask-level={level_number}"
                if exit_order.action is OrderAction.BUY
                else fill.source_id
            )
            ledger.record_fill(
                _a01_yes_equivalent_fill(
                    fill,
                    path_id=path_id,
                    purpose=FillPurpose.EXIT,
                    source_sha256=snapshot_sources[
                        selected_snapshot_id
                    ],
                )
            )
        exit_filled_e4 = exit_batch.filled_e4
        if ledger.position_e4 != 0:
            settlement_id = closure.get("settlement_id")
            if settlement_id is None:
                residual_e4 = abs(ledger.position_e4)
                return (
                    _blocked_row(
                        row_id=row.row_id,
                        kind="STRATEGY",
                        path_id=path_id,
                        decision_status=decision.status.value,
                        reason_codes=("BLOCK_RESIDUAL_POSITION",),
                        residual_quantity_e4=residual_e4,
                        diagnostic={
                            "first_fill_us": paired.first_fill_us,
                            "cancel_effective_us": (
                                paired.cancel_effective_us
                            ),
                            "yes_equivalent_net_before_exit_e4": (
                                net_before_exit_e4
                            ),
                            "ioc_exit_fill_count": len(
                                exit_batch.fills
                            ),
                            "selected_exit_snapshot_id": (
                                selected_snapshot_id
                            ),
                            "ledger_sha256": (
                                ledger.deterministic_sha256
                            ),
                        },
                    ),
                    exit_batch,
                    exit_alias,
                    {
                        "row_id": row.row_id,
                        "ordered_quantity_e4": paired.batch.ordered_e4,
                        "filled_quantity_e4": paired.batch.filled_e4,
                        "canceled_quantity_e4": paired.batch.unfilled_e4,
                        "paired_reconciled_quantity_e4": min(
                            yes_filled_e4,
                            no_filled_e4,
                        ),
                        "net_before_exit_e4": net_before_exit_e4,
                        "ioc_exit_quantity_e4": exit_filled_e4,
                        "settled_quantity_e4": 0,
                        "held_quantity_e4": residual_e4,
                        "first_fill_us": paired.first_fill_us,
                        "cancel_effective_us": (
                            paired.cancel_effective_us
                        ),
                        "cash_position_fee_ledger_sha256": (
                            ledger.deterministic_sha256
                        ),
                    },
                )
            residual_before_settlement = abs(ledger.position_e4)
            ledger.observe_settlement(
                _settlement_record(
                    settlements[_text("settlement_id", settlement_id)],
                    outcome=Outcome.YES,
                )
            )
            settled_e4 = residual_before_settlement
            if ledger.position_e4 != 0:
                residual_e4 = abs(ledger.position_e4)
                return (
                    _blocked_row(
                        row_id=row.row_id,
                        kind="STRATEGY",
                        path_id=path_id,
                        decision_status=decision.status.value,
                        reason_codes=("BLOCK_RESIDUAL_POSITION",),
                        residual_quantity_e4=residual_e4,
                        diagnostic={
                            "first_fill_us": paired.first_fill_us,
                            "cancel_effective_us": (
                                paired.cancel_effective_us
                            ),
                            "yes_equivalent_net_before_exit_e4": (
                                net_before_exit_e4
                            ),
                            "ioc_exit_fill_count": len(
                                exit_batch.fills
                            ),
                            "selected_exit_snapshot_id": (
                                selected_snapshot_id
                            ),
                            "ledger_sha256": (
                                ledger.deterministic_sha256
                            ),
                        },
                    ),
                    exit_batch,
                    exit_alias,
                    {
                        "row_id": row.row_id,
                        "ordered_quantity_e4": paired.batch.ordered_e4,
                        "filled_quantity_e4": paired.batch.filled_e4,
                        "canceled_quantity_e4": paired.batch.unfilled_e4,
                        "paired_reconciled_quantity_e4": min(
                            yes_filled_e4,
                            no_filled_e4,
                        ),
                        "net_before_exit_e4": net_before_exit_e4,
                        "ioc_exit_quantity_e4": exit_filled_e4,
                        "settled_quantity_e4": 0,
                        "held_quantity_e4": residual_e4,
                        "first_fill_us": paired.first_fill_us,
                        "cancel_effective_us": (
                            paired.cancel_effective_us
                        ),
                        "cash_position_fee_ledger_sha256": (
                            ledger.deterministic_sha256
                        ),
                    },
                )

    result = ledger.finalize()
    realized_pnl_slices = _a01_cashflow_realized_pnl_slices(
        pnl_ledger=ledger,
        result=result,
        exit_fill_ids={
            fill.fill_id
            for fill in (
                exit_batch.fills if exit_batch is not None else ()
            )
        },
    )
    canceled_by_order = dict(paired.canceled_quantity_by_order_e4)
    ordered_by_id = {
        intent.intent_id: intent.quantity_e4 for intent in intents
    }
    filled_by_id = {
        intent.intent_id: sum(
            fill.quantity_e4
            for fill in fills
            if fill.order_id == intent.intent_id
        )
        for intent in intents
    }
    if any(
        ordered_by_id[intent_id]
        != filled_by_id[intent_id] + canceled_by_order[intent_id]
        for intent_id in ordered_by_id
    ):
        raise LedgerInvariantError(
            "A01 order fill/cancel conservation failed"
        )
    if abs(net_before_exit_e4) != exit_filled_e4 + settled_e4:
        raise LedgerInvariantError(
            "A01 net position exit/settlement conservation failed"
        )
    paired_quantity_e4 = min(yes_filled_e4, no_filled_e4)
    canceled_reserve_release_e6 = sum(
        exact_trade_notional_e6(
            outcome_price_by_intent[intent_id],
            canceled_quantity_e4,
        )
        for intent_id, canceled_quantity_e4 in canceled_by_order.items()
        if canceled_quantity_e4
    )
    raw_filled_acquisition_e6 = sum(
        exact_trade_notional_e6(fill.price_e4, fill.quantity_e4)
        for fill in fills
    )
    if reserved_at_risk_e6 != (
        canceled_reserve_release_e6 + raw_filled_acquisition_e6
    ):
        raise LedgerInvariantError(
            "A01 collateral reserve/fill/cancel cash identity failed"
        )
    yes_equivalent_collateral_release_e6 = no_filled_e4 * 100
    entry_fill_ids = {fill.fill_id for fill in fills}
    yes_equivalent_entry_principal_e6 = sum(
        cashflow.amount_e6
        for cashflow in ledger.cashflows
        if (
            cashflow.kind is CashFlowKind.TRADE_PRINCIPAL
            and cashflow.fill_id in entry_fill_ids
        )
    )
    if yes_equivalent_entry_principal_e6 != (
        -raw_filled_acquisition_e6
        + yes_equivalent_collateral_release_e6
    ):
        raise LedgerInvariantError(
            "A01 NO-to-SELL-YES cash/collateral identity failed"
        )
    collateral = {
        "row_id": row.row_id,
        "decision_ts_ns": row.decision_ts_ns,
        "market_ticker": row.market_ticker,
        "event_ticker": row.root_event_id,
        "factor_key": f"{row.sport}:{row.root_event_id}",
        "ordered_quantity_e4": paired.batch.ordered_e4,
        "filled_quantity_e4": paired.batch.filled_e4,
        "canceled_quantity_e4": paired.batch.unfilled_e4,
        "paired_reconciled_quantity_e4": paired_quantity_e4,
        "net_before_exit_e4": net_before_exit_e4,
        "ioc_exit_quantity_e4": exit_filled_e4,
        "settled_quantity_e4": settled_e4,
        "held_quantity_e4": abs(ledger.position_e4),
        "reserved_at_risk_e6": reserved_at_risk_e6,
        "canceled_reserve_release_e6": (
            canceled_reserve_release_e6
        ),
        "raw_filled_acquisition_e6": raw_filled_acquisition_e6,
        "yes_equivalent_collateral_release_e6": (
            yes_equivalent_collateral_release_e6
        ),
        "yes_equivalent_entry_principal_e6": (
            yes_equivalent_entry_principal_e6
        ),
        "held_at_risk_e6": 0,
        "fee_cost_e6": result.fee_cost_e6,
        "net_pnl_e6": result.net_pnl_e6,
        "first_fill_us": paired.first_fill_us,
        "cancel_effective_us": paired.cancel_effective_us,
        "cash_position_fee_ledger_sha256": result.ledger_sha256,
        "realized_pnl_slices_sha256": canonical_sha256(
            realized_pnl_slices
        ),
        "realized_at_ns": max(
            cashflow.occurred_at_ns for cashflow in ledger.cashflows
        ),
    }
    path_row = {
        "row_id": row.row_id,
        "kind": "STRATEGY",
        "path_id": path_id,
        "decision_status": decision.status.value,
        "reason_codes": list(decision.reason_codes),
        "state": PATH_COMPLETE,
        "result": _result_mapping(result),
        "realized_pnl_slices": list(realized_pnl_slices),
        "residual_quantity_e4": 0,
        "diagnostic": {
            "first_fill_us": paired.first_fill_us,
            "cancel_effective_us": paired.cancel_effective_us,
            "yes_entry_filled_e4": yes_filled_e4,
            "no_entry_filled_e4": no_filled_e4,
            "yes_equivalent_net_before_exit_e4": net_before_exit_e4,
            "ioc_exit_fill_count": (
                len(exit_batch.fills) if exit_batch is not None else 0
            ),
            "selected_exit_snapshot_id": selected_snapshot_id,
            "realized_at_ns": max(
                cashflow.occurred_at_ns for cashflow in ledger.cashflows
            ),
        },
    }
    return path_row, exit_batch, exit_alias, collateral


def _run_a01_paths(
    *,
    run_id: str,
    frozen_experiment: Mapping[str, Any],
    decisions: Sequence[StrategyDecision],
    row_by_id: Mapping[str, NormalizedStateRow],
    passive_orders: Sequence[PassiveOrder],
    risk_rejected_intents: set[str],
    public_trades: Sequence[PublicTrade],
    trade_sources: Mapping[str, str],
    snapshots: Mapping[str, L2Snapshot],
    snapshot_sources: Mapping[str, str],
    exit_authority: Mapping[str, int],
    closures: Mapping[str, Mapping[str, Any]],
    settlements: Mapping[str, Mapping[str, Any]],
    fee_schedule: FeeSchedule,
    risk_limits: RiskLimits | None,
    latency: Mapping[str, int],
    run_binding: RunBinding | None,
    preflight: Mapping[str, Any],
    terminal_document: Mapping[str, Any],
    blockers: list[dict[str, str]],
    trusted_authority_sha256: str | None,
    trusted_lineage_receipt_sha256: str | None,
) -> dict[str, Any]:
    try:
        _a01_fill_response_contract(frozen_experiment)
    except RunnerContractError as exc:
        blockers.append(
            _blocker(
                "A01_FILL_RESPONSE_CONTRACT_AMBIGUOUS",
                "EXECUTION",
                str(exc),
            )
        )
    try:
        paired_by_row, entry_batch = _allocate_a01_groups(
            decisions=decisions,
            row_by_id=row_by_id,
            passive_orders=passive_orders,
            risk_rejected_intents=risk_rejected_intents,
            public_trades=public_trades,
            cancel_latency_ns=_plain_int(
                "CANCEL latency",
                latency.get("CANCEL"),
                minimum=1,
            ),
            blockers=blockers,
        )
    except (FillError, RunnerContractError, ValueError) as exc:
        paired_by_row = {}
        entry_batch = FillBatch(
            fills=(),
            ordered_e4=sum(
                order.quantity_e4 for order in passive_orders
            ),
            filled_e4=0,
            unfilled_e4=sum(
                order.quantity_e4 for order in passive_orders
            ),
            source_quantity_e4=sum(
                trade.quantity_e4 for trade in public_trades
            ),
            consumed_source_quantity_e4=0,
            duplicate_source_allocations=0,
        )
        blockers.append(
            _blocker(
                "ENTRY_FILL_AUTHORITY_FAILED",
                "FILL",
                str(exc),
            )
        )

    run_binding_sha = (
        run_binding.sha256 if run_binding is not None else "0" * 64
    )
    path_rows: list[dict[str, Any]] = []
    exit_batches: dict[str, FillBatch] = {}
    exit_aliases: dict[str, str] = {}
    collateral_rows: list[dict[str, Any]] = []
    for decision in decisions:
        row = row_by_id[decision.row_id]
        baseline = PnLLedger(
            _path_spec(
                path_id=_path_id(run_id, row.row_id, "BASELINE"),
                decision=decision,
                row=row,
                outcome=Outcome.YES,
                run_binding_sha256=run_binding_sha,
            ),
            fee_schedule,
        )
        path_rows.append(
            _zero_result_row(
                ledger=baseline,
                row_id=row.row_id,
                kind="BASELINE",
                decision=decision,
                occurred_at_ns=row.decision_ts_ns,
                source_sha256=decision.sha256,
                reason_codes=("NO_TRADE_SAME_OPPORTUNITIES",),
            )
        )
        strategy_path_id = _a01_strategy_path_id(run_id, row)
        if decision.status is DecisionStatus.BLOCKED:
            path_rows.append(
                _blocked_row(
                    row_id=row.row_id,
                    kind="STRATEGY",
                    path_id=strategy_path_id,
                    decision_status=decision.status.value,
                    reason_codes=decision.reason_codes,
                )
            )
            blockers.extend(
                _blocker(reason, "DECISION", row.row_id)
                for reason in decision.reason_codes
            )
            continue
        if decision.status is DecisionStatus.ABSTAIN:
            strategy = PnLLedger(
                _path_spec(
                    path_id=strategy_path_id,
                    decision=decision,
                    row=row,
                    outcome=Outcome.YES,
                    run_binding_sha256=run_binding_sha,
                ),
                fee_schedule,
            )
            path_rows.append(
                _zero_result_row(
                    ledger=strategy,
                    row_id=row.row_id,
                    kind="STRATEGY",
                    decision=decision,
                    occurred_at_ns=row.decision_ts_ns,
                    source_sha256=decision.sha256,
                    reason_codes=decision.reason_codes,
                )
            )
            continue
        if any(
            intent.intent_id in risk_rejected_intents
            for intent in decision.intents
        ):
            path_rows.append(
                _blocked_row(
                    row_id=row.row_id,
                    kind="STRATEGY",
                    path_id=strategy_path_id,
                    decision_status=decision.status.value,
                    reason_codes=("BLOCK_RISK_ADMISSION_FAILED",),
                )
            )
            continue
        paired = paired_by_row.get(row.row_id)
        if paired is None:
            path_rows.append(
                _blocked_row(
                    row_id=row.row_id,
                    kind="STRATEGY",
                    path_id=strategy_path_id,
                    decision_status=decision.status.value,
                    reason_codes=("BLOCK_A01_PAIRED_ALLOCATION_MISSING",),
                )
            )
            continue
        try:
            (
                path_row,
                exit_batch,
                aliases,
                collateral,
            ) = _execute_a01_strategy_path(
                run_id=run_id,
                decision=decision,
                row=row,
                paired=paired,
                trade_sources=trade_sources,
                snapshots=snapshots,
                snapshot_sources=snapshot_sources,
                closures=closures,
                settlements=settlements,
                fee_schedule=fee_schedule,
                ioc_exit_latency_ns=_plain_int(
                    "IOC_EXIT latency",
                    latency.get("IOC_EXIT"),
                    minimum=1,
                ),
                run_binding_sha256=run_binding_sha,
            )
            path_rows.append(path_row)
            collateral_rows.append(collateral)
            if path_row["state"] != PATH_COMPLETE:
                blockers.append(
                    _blocker(
                        "RESIDUAL_POSITION_OPEN",
                        "CLOSURE",
                        (
                            f"{strategy_path_id} residual_e4="
                            f"{path_row['residual_quantity_e4']}"
                        ),
                    )
                )
            if exit_batch is not None:
                exit_batches[strategy_path_id] = exit_batch
                exit_aliases.update(aliases)
        except (
            FeeTruthUnavailable,
            FillError,
            IncompletePnL,
            LedgerInvariantError,
            RunnerContractError,
            KeyError,
            ValueError,
        ) as exc:
            paired_net = sum(
                (
                    fill.quantity_e4
                    if fill.side is OutcomeSide.YES
                    else -fill.quantity_e4
                )
                for fill in paired.batch.fills
            )
            blockers.append(
                _blocker(
                    "A01_PATH_STATE_MACHINE_FAILED",
                    "CLOSURE",
                    f"{row.row_id}: {exc}",
                )
            )
            path_rows.append(
                _blocked_row(
                    row_id=row.row_id,
                    kind="STRATEGY",
                    path_id=strategy_path_id,
                    decision_status=decision.status.value,
                    reason_codes=("BLOCK_A01_PATH_STATE_MACHINE_FAILED",),
                    residual_quantity_e4=abs(paired_net),
                    diagnostic={
                        "first_fill_us": paired.first_fill_us,
                        "cancel_effective_us": paired.cancel_effective_us,
                        "yes_equivalent_net_before_exit_e4": paired_net,
                    },
                )
            )

    for path_row in path_rows:
        if path_row.get("state") == PATH_COMPLETE:
            path_row.setdefault("realized_pnl_slices", [])

    aliased_batches: list[FillBatch] = []
    for batch in exit_batches.values():
        fills = tuple(
            FillSlice(
                **{
                    **asdict(fill),
                    "side": fill.side,
                    "action": fill.action,
                    "liquidity_role": fill.liquidity_role,
                    "source_id": exit_aliases[fill.fill_id],
                }
            )
            for fill in batch.fills
        )
        aliased_batches.append(
            FillBatch(
                fills=fills,
                ordered_e4=batch.ordered_e4,
                filled_e4=batch.filled_e4,
                unfilled_e4=batch.unfilled_e4,
                source_quantity_e4=batch.source_quantity_e4,
                consumed_source_quantity_e4=(
                    batch.consumed_source_quantity_e4
                ),
                duplicate_source_allocations=0,
            )
        )
    try:
        assert_portfolio_fill_conservation(
            aliased_batches,
            authoritative_source_quantity=exit_authority,
        )
    except FillError as exc:
        blockers.append(
            _blocker(
                "EXIT_PUBLIC_DEPTH_REUSED",
                "CONSERVATION",
                str(exc),
            )
        )

    risk_event_trace: list[dict[str, Any]] = []
    if risk_limits is None:
        blockers.append(
            _blocker(
                "RISK_LIFECYCLE_REPLAY_FAILED",
                "RISK",
                "A01 risk limits are unavailable",
            )
        )
    else:
        collateral_by_row = {
            _text("row_id", collateral.get("row_id")): collateral
            for collateral in collateral_rows
            if "reserved_at_risk_e6" in collateral
        }
        admitted_rows: list[Mapping[str, Any]] = []
        pre_rejected_rows: set[str] = set()
        for decision in sorted(
            decisions,
            key=lambda item: (
                row_by_id[item.row_id].decision_ts_ns,
                item.row_id,
            ),
        ):
            if decision.status is not DecisionStatus.ORDER_INTENTS:
                continue
            current = collateral_by_row.get(decision.row_id)
            if current is None:
                continue
            decision_ns = _plain_int(
                "decision_ts_ns",
                current.get("decision_ts_ns"),
                minimum=0,
            )
            exposure = {
                "market": 0,
                "event": 0,
                "factor": 0,
                "total": 0,
            }
            for prior in admitted_rows:
                cancel_ns = (
                    _plain_int(
                        "cancel_effective_us",
                        prior.get("cancel_effective_us"),
                        minimum=0,
                    )
                    * 1_000
                )
                realized_at_ns = _plain_int(
                    "realized_at_ns",
                    prior.get("realized_at_ns"),
                    minimum=0,
                )
                if decision_ns < cancel_ns:
                    held_e6 = _plain_int(
                        "reserved_at_risk_e6",
                        prior.get("reserved_at_risk_e6"),
                        minimum=0,
                    )
                elif decision_ns < realized_at_ns:
                    held_e6 = _plain_int(
                        "raw_filled_acquisition_e6",
                        prior.get("raw_filled_acquisition_e6"),
                        minimum=0,
                    )
                else:
                    held_e6 = 0
                if held_e6 == 0:
                    continue
                exposure["total"] += held_e6
                if (
                    prior.get("market_ticker")
                    == current.get("market_ticker")
                ):
                    exposure["market"] += held_e6
                if (
                    prior.get("event_ticker")
                    == current.get("event_ticker")
                ):
                    exposure["event"] += held_e6
                if prior.get("factor_key") == current.get("factor_key"):
                    exposure["factor"] += held_e6
            requested_e6 = _plain_int(
                "reserved_at_risk_e6",
                current.get("reserved_at_risk_e6"),
                minimum=0,
            )
            prospective = {
                name: amount + requested_e6
                for name, amount in exposure.items()
            }
            limit_by_name = {
                "market": risk_limits.max_market_e6,
                "event": risk_limits.max_event_e6,
                "factor": risk_limits.max_factor_e6,
                "total": risk_limits.max_total_e6,
            }
            exceeded = sorted(
                name
                for name, amount in prospective.items()
                if amount > limit_by_name[name]
            )
            if exceeded:
                pre_rejected_rows.add(decision.row_id)
                blockers.append(
                    _blocker(
                        "RISK_LIMIT_CHANGED_ADMISSION",
                        "RISK",
                        (
                            f"{decision.row_id}: risk limits exceeded: "
                            f"{','.join(exceeded)}; preallocated fills "
                            "cannot be safely reused"
                        ),
                    )
                )
                continue
            admitted_rows.append(current)

        risk_events: list[
            tuple[int, int, str, str, str, int]
        ] = []
        for path_row in path_rows:
            if (
                path_row.get("kind") != "STRATEGY"
                or path_row.get("state") != PATH_COMPLETE
                or not isinstance(path_row.get("result"), Mapping)
            ):
                continue
            result = path_row["result"]
            row_id = _text("row_id", path_row.get("row_id"))
            raw_slices = path_row.get("realized_pnl_slices")
            if not isinstance(raw_slices, list):
                blockers.append(
                    _blocker(
                        "RISK_REALIZED_PNL_ATTRIBUTION_FAILED",
                        "RISK",
                        f"{row_id}: realized-PnL slice array is missing",
                    )
                )
                continue
            slice_total_e6 = 0
            for index, raw_slice in enumerate(raw_slices):
                slice_row = _mapping(
                    f"realized_pnl_slices[{index}]",
                    raw_slice,
                )
                supplied_slice_sha = _sha256_text(
                    "slice_sha256",
                    slice_row.get("slice_sha256"),
                )
                slice_payload = dict(slice_row)
                slice_payload.pop("slice_sha256", None)
                if canonical_sha256(slice_payload) != supplied_slice_sha:
                    raise RiskInvariantError(
                        "A01 realized-PnL slice SHA mismatch"
                    )
                occurred_at_ns = _plain_int(
                    "occurred_at_ns",
                    slice_row.get("occurred_at_ns"),
                    minimum=0,
                )
                priority = _plain_int(
                    "causal_priority",
                    slice_row.get("causal_priority"),
                    minimum=0,
                )
                if priority != 20:
                    raise RiskInvariantError(
                        "A01 realized-PnL priority must be 20"
                    )
                pnl_e6 = _plain_int(
                    "realized_pnl_e6",
                    slice_row.get("realized_pnl_e6"),
                )
                slice_id = _text(
                    "slice_id",
                    slice_row.get("slice_id"),
                )
                kind = _text("kind", slice_row.get("kind"))
                fill_phase = slice_row.get("fill_phase")
                if (
                    kind == "TRADE_PRINCIPAL"
                    and fill_phase == "EXIT"
                ) or kind == "SETTLEMENT":
                    risk_events.append(
                        (
                            occurred_at_ns,
                            10,
                            f"lifecycle:{slice_id}",
                            (
                                "SETTLEMENT"
                                if kind == "SETTLEMENT"
                                else "EXIT"
                            ),
                            row_id,
                            0,
                        )
                    )
                risk_events.append(
                    (
                        occurred_at_ns,
                        priority,
                        f"realized:{slice_id}",
                        "REALIZED_PNL",
                        row_id,
                        pnl_e6,
                    )
                )
                slice_total_e6 += pnl_e6
            if slice_total_e6 != _plain_int(
                "net_pnl_e6",
                result.get("net_pnl_e6"),
            ):
                blockers.append(
                    _blocker(
                        "RISK_REALIZED_PNL_ATTRIBUTION_FAILED",
                        "RISK",
                        (
                            f"{row_id}: cashflow slices do not reconcile "
                            "to final path PnL"
                        ),
                    )
                )
        for decision in decisions:
            if decision.status is DecisionStatus.ORDER_INTENTS:
                row = row_by_id[decision.row_id]
                risk_events.append(
                    (
                        row.decision_ts_ns,
                        30,
                        f"reserve:{row.row_id}",
                        "RESERVE",
                        row.row_id,
                        0,
                    )
                )

        realized_by_date_e6: dict[str, int] = {}
        breached_dates: set[str] = set()
        rejected_rows = set(pre_rejected_rows)
        daily_gate_reported_rows: set[str] = set()
        for (
            occurred_at_ns,
            priority,
            event_id,
            kind,
            row_id,
            pnl_e6,
        ) in sorted(risk_events, key=lambda event: event[:3]):
            event_date = _utc_date_from_ns(
                "risk_event.occurred_at_ns",
                occurred_at_ns,
            )
            ignored = row_id in rejected_rows and kind != "RESERVE"
            risk_event_trace.append(
                {
                    "occurred_at_ns": occurred_at_ns,
                    "priority": priority,
                    "event_id": event_id,
                    "kind": kind,
                    "row_id": row_id,
                    "realized_pnl_e6": pnl_e6,
                    "ignored_after_rejection": ignored,
                }
            )
            if ignored:
                continue
            if kind == "REALIZED_PNL":
                realized_by_date_e6[event_date] = (
                    realized_by_date_e6.get(event_date, 0) + pnl_e6
                )
                daily_pnl_e6 = realized_by_date_e6[event_date]
                if (
                    daily_pnl_e6 < 0
                    and -daily_pnl_e6
                    >= risk_limits.max_daily_loss_e6
                ):
                    breached_dates.add(event_date)
            elif kind == "RESERVE":
                if row_id in pre_rejected_rows:
                    rejected_rows.add(row_id)
                    continue
                if event_date in breached_dates:
                    rejected_rows.add(row_id)
                    if row_id not in daily_gate_reported_rows:
                        daily_gate_reported_rows.add(row_id)
                        blockers.append(
                            _blocker(
                                "RISK_DAILY_LOSS_CHANGED_ADMISSION",
                                "RISK",
                                (
                                    f"{row_id}: daily realized loss gate is "
                                    "closed; preallocated fills cannot be "
                                    "safely reused"
                                ),
                            )
                        )

    path_rows.sort(
        key=lambda row: (
            row["row_id"],
            row["kind"],
            row["path_id"],
        )
    )
    actual_residual = sum(
        int(row.get("residual_quantity_e4", 0))
        for row in path_rows
    )
    if terminal_document.get("path_count") != len(path_rows):
        blockers.append(
            _blocker(
                "TERMINAL_PATH_COUNT_RUNTIME_MISMATCH",
                "CLOSURE",
                (
                    f"receipt={terminal_document.get('path_count')} "
                    f"runtime={len(path_rows)}"
                ),
            )
        )
    if terminal_document.get("residual_quantity_e4") != actual_residual:
        blockers.append(
            _blocker(
                "TERMINAL_RESIDUAL_RUNTIME_MISMATCH",
                "CLOSURE",
                "terminal receipt residual does not match runtime ledger",
            )
        )
    if run_binding is None:
        blockers.append(
            _blocker(
                "RUN_BINDING_MISSING",
                "PROVENANCE",
                "no exact immutable run binding was established",
            )
        )
    if preflight["net_pnl"]["state"] != NET_READY:
        blockers.append(
            _blocker(
                "NET_PREFLIGHT_NOT_READY",
                "PREFLIGHT",
                "fee, real latency, data, closure, or training gate failed",
            )
        )
    if any(row["state"] != PATH_COMPLETE for row in path_rows):
        blockers.append(
            _blocker(
                "NOT_ALL_PATHS_COMPLETE",
                "CLOSURE",
                "every strategy and baseline path must close",
            )
        )
    conservation = {
        "public_source_quantity_e4": sum(
            trade.quantity_e4 for trade in public_trades
        ),
        "public_consumed_quantity_e4": (
            entry_batch.consumed_source_quantity_e4
        ),
        "entry_ordered_quantity_e4": entry_batch.ordered_e4,
        "entry_filled_quantity_e4": entry_batch.filled_e4,
        "entry_canceled_quantity_e4": entry_batch.unfilled_e4,
        "paired_reconciled_quantity_e4": sum(
            int(row["paired_reconciled_quantity_e4"])
            for row in collateral_rows
        ),
        "exit_filled_quantity_e4": sum(
            batch.filled_e4 for batch in exit_batches.values()
        ),
        "settled_quantity_e4": sum(
            int(row["settled_quantity_e4"])
            for row in collateral_rows
        ),
        "reserved_at_risk_e6": sum(
            int(row.get("reserved_at_risk_e6", 0))
            for row in collateral_rows
        ),
        "canceled_reserve_release_e6": sum(
            int(row.get("canceled_reserve_release_e6", 0))
            for row in collateral_rows
        ),
        "raw_filled_acquisition_e6": sum(
            int(row.get("raw_filled_acquisition_e6", 0))
            for row in collateral_rows
        ),
        "yes_equivalent_collateral_release_e6": sum(
            int(
                row.get(
                    "yes_equivalent_collateral_release_e6",
                    0,
                )
            )
            for row in collateral_rows
        ),
        "held_at_risk_e6": sum(
            int(row.get("held_at_risk_e6", 0))
            for row in collateral_rows
        ),
        "residual_quantity_e4": actual_residual,
    }
    if conservation["entry_ordered_quantity_e4"] != (
        conservation["entry_filled_quantity_e4"]
        + conservation["entry_canceled_quantity_e4"]
    ):
        blockers.append(
            _blocker(
                "A01_ORDER_QUANTITY_CONSERVATION_FAILED",
                "CONSERVATION",
                "ordered quantity does not equal filled plus canceled",
            )
        )
    if conservation["entry_filled_quantity_e4"] != (
        2 * conservation["paired_reconciled_quantity_e4"]
        + conservation["exit_filled_quantity_e4"]
        + conservation["settled_quantity_e4"]
        + conservation["residual_quantity_e4"]
    ):
        blockers.append(
            _blocker(
                "A01_POSITION_CONSERVATION_FAILED",
                "CONSERVATION",
                (
                    "entry fills do not reconcile to paired, IOC, "
                    "settlement, and residual quantities"
                ),
            )
        )
    if (
        conservation["residual_quantity_e4"] == 0
        and conservation["held_at_risk_e6"] != 0
    ):
        blockers.append(
            _blocker(
                "A01_COLLATERAL_CONSERVATION_FAILED",
                "CONSERVATION",
                "flat paths retain at-risk collateral",
            )
        )
    return _receipt(
        run_id=run_id,
        experiment_id="A01-SPREAD-CAPTURE",
        state=NET_COMPLETE if not blockers else PNL_BLOCKED,
        run_binding_sha256=(
            run_binding.sha256 if run_binding is not None else None
        ),
        preflight=preflight,
        path_rows=path_rows,
        blockers=blockers,
        conservation=conservation,
        risk_ledger_sha256=canonical_sha256(
            {
                "schema_version": "a01-collateral-risk-replay-v2",
                "rows": collateral_rows,
                "event_trace": risk_event_trace,
            }
        ),
        trusted_authority_sha256=trusted_authority_sha256,
        trusted_lineage_receipt_sha256=(
            trusted_lineage_receipt_sha256
        ),
    )


def run_fixture(
    fixture: Mapping[str, Any],
    *,
    trusted_authority: Mapping[str, Any] | None = None,
    expected_trusted_authority_sha256: str | None = None,
    trusted_lineage_receipt: Mapping[str, Any] | None = None,
    expected_trusted_lineage_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    """Run one fixture with two independently pinned canonical authorities.

    ``expected_*_sha256`` values bind the parsed canonical documents.  The CLI
    separately verifies the raw bytes of each authority file before passing
    those canonical pins here.
    """

    top = _mapping("fixture", fixture)
    allowed = {
        "schema_version",
        "run_id",
        "experiment_id",
        "code_sha256",
        "frozen_experiment",
        "card_parameter_artifact_sha256",
        "preflight_inputs",
        "provenance",
        "fee_facts_authority",
        "fee_contexts",
        "risk_policy",
        "evidence_bindings",
        "rows",
        "public_trades",
        "exit_snapshots",
        "closures",
        "settlements",
    }
    _strict_keys(
        "fixture",
        top,
        allowed,
        required=allowed,
    )
    if top.get("schema_version") != FIXTURE_SCHEMA:
        raise RunnerContractError("unknown fixture schema_version")
    run_id = _text("run_id", top.get("run_id"))
    experiment_id = _text(
        "experiment_id", top.get("experiment_id")
    )
    _text("code_sha256", top.get("code_sha256"))
    freeze = _mapping(
        "frozen_experiment", top.get("frozen_experiment")
    )
    preflight_inputs = _mapping(
        "preflight_inputs", top.get("preflight_inputs")
    )
    _strict_keys(
        "preflight_inputs",
        preflight_inputs,
        {
            "fee_facts",
            "measured_latency",
            "release_dq",
            "terminal_coverage",
        },
        required={
            "fee_facts",
            "measured_latency",
            "release_dq",
            "terminal_coverage",
        },
    )
    fee_raw = preflight_inputs.get("fee_facts")
    latency_raw = preflight_inputs.get("measured_latency")
    release_raw = preflight_inputs.get("release_dq")
    terminal_raw = preflight_inputs.get("terminal_coverage")
    fee_document = fee_raw if isinstance(fee_raw, Mapping) else {}
    latency_document = (
        latency_raw if isinstance(latency_raw, Mapping) else {}
    )
    release_document = (
        release_raw if isinstance(release_raw, Mapping) else {}
    )
    terminal_document = (
        terminal_raw if isinstance(terminal_raw, Mapping) else {}
    )
    input_hashes = {
        "frozen_experiment": _document_sha256(freeze),
        "fee_facts": (
            _document_sha256(fee_document)
            if isinstance(fee_raw, Mapping)
            else None
        ),
        "measured_latency": (
            _document_sha256(latency_document)
            if isinstance(latency_raw, Mapping)
            else None
        ),
        "release_dq": (
            _document_sha256(release_document)
            if isinstance(release_raw, Mapping)
            else None
        ),
        "terminal_coverage": (
            _document_sha256(terminal_document)
            if isinstance(terminal_raw, Mapping)
            else None
        ),
    }
    preflight = evaluate_readiness(
        experiment_id=experiment_id,
        frozen_experiment=freeze,
        fee_facts_receipt=fee_raw,
        measured_latency_receipt=latency_raw,
        release_dq_receipt=release_raw,
        terminal_coverage_receipt=terminal_raw,
        input_sha256s=input_hashes,
    )
    blockers: list[dict[str, str]] = [
        _blocker(
            row["code"],
            f"PREFLIGHT_{row['stage']}",
            row["detail"],
        )
        for row in preflight["net_pnl"]["blockers"]
    ]
    if experiment_id == "A11-ONE-SIDED-PROVISION":
        blockers.append(
            _blocker(
                "A11_POST_DECISION_CANCEL_STREAM_UNAVAILABLE",
                "EXECUTION",
                (
                    "frozen A11 requires an exact-bound post-decision "
                    "lifecycle/cancel-event stream; no reviewed schema and "
                    "event engine is installed"
                ),
            )
        )
    trusted_lineage_receipt_sha256: str | None = None
    try:
        trusted_lineage_receipt_sha256 = _validate_lineage_receipt(
            top,
            trusted_lineage_receipt,
            expected_sha256=(
                expected_trusted_lineage_receipt_sha256
            ),
        )
    except (ProvenanceError, RunnerContractError, ValueError) as exc:
        blockers.append(
            _blocker(
                "RECORD_LINEAGE_AUTHORITY_INVALID",
                "PROVENANCE",
                str(exc),
            )
        )

    trusted_authority_sha256: str | None = None
    try:
        trusted_authority_sha256 = _validate_trusted_authority(
            top,
            trusted_authority,
            expected_sha256=expected_trusted_authority_sha256,
            trusted_lineage_receipt=trusted_lineage_receipt,
        )
    except (ProvenanceError, RunnerContractError, ValueError) as exc:
        blockers.append(
            _blocker(
                "EXTERNAL_AUTHORITY_INVALID",
                "PROVENANCE",
                str(exc),
            )
        )

    fee_binding = fee_document.get("bindings")
    fee_facts: FeeFacts | None = None
    try:
        if not isinstance(fee_binding, Mapping):
            raise FeeFactsUnavailable("verified fee receipt bindings missing")
        expected_fee_sha = _text(
            "fee_facts_sha256",
            fee_binding.get("fee_facts_sha256"),
        )
        authority = _mapping(
            "fee_facts_authority", top.get("fee_facts_authority")
        )
        fee_facts = parse_fee_facts_document(
            authority,
            expected_facts_sha256=expected_fee_sha,
        )
        expected_precision_digits = (
            4
            if fee_facts.account_precision
            is FeePrecision.DIRECT_CENTICENT
            else 2
        )
        expected_account_class = (
            "DIRECT_MEMBER"
            if fee_facts.account_precision
            is FeePrecision.DIRECT_CENTICENT
            else "NON_DIRECT_MEMBER"
        )
        required_receipt_values = {
            "maker_fee_formula_id": (
                "OFFICIAL_QUADRATIC_MAKER_0.0175_C_P_1MP"
            ),
            "taker_fee_formula_id": (
                "OFFICIAL_QUADRATIC_TAKER_0.07_C_P_1MP"
            ),
            "account_class": expected_account_class,
            "target_balance_precision": expected_precision_digits,
            "fee_rounding_accumulator_version": (
                "OFFICIAL_PER_ORDER_ACCUMULATOR_CENTICENT_V1"
            ),
            "rebate_and_event_override_version": (
                "OFFICIAL_EFFECTIVE_DATED_SERIES_EVENT_WAIVER_V1"
            ),
        }
        for field, expected in required_receipt_values.items():
            if fee_binding.get(field) != expected:
                raise FeeFactsUnavailable(
                    f"fee receipt {field} is not official authority {expected}"
                )
        if (
            fee_document.get("source_sha256")
            != fee_facts.sources.fee_schedule_pdf_sha256
        ):
            raise FeeFactsUnavailable(
                "fee receipt does not bind the official schedule source"
            )
    except (FeeFactsUnavailable, RunnerContractError, ValueError) as exc:
        blockers.append(
            _blocker(
                "FEE_AUTHORITY_INVALID",
                "FEE",
                str(exc),
            )
        )
    fee_schedule = FeeSchedule(())

    provenance_document = _mapping(
        "provenance", top.get("provenance")
    )
    risk_limits: RiskLimits | None = None
    try:
        risk_limits = _parse_risk_policy(
            top.get("risk_policy"),
            expected_sha256=_text(
                "risk_policy_sha256",
                provenance_document.get("risk_policy_sha256"),
            ),
        )
    except (RunnerContractError, RiskInvariantError, ValueError) as exc:
        blockers.append(
            _blocker("RISK_POLICY_INVALID", "RISK", str(exc))
        )

    run_binding: RunBinding | None = None
    try:
        if (
            input_hashes["measured_latency"] is None
            or input_hashes["terminal_coverage"] is None
        ):
            raise ProvenanceError(
                "latency and terminal receipts are required by RunBinding"
            )
        if fee_facts is None:
            raise ProvenanceError("verified fee authority is required")
        run_binding = _make_run_binding(
            top,
            fee_facts_sha256=fee_facts.facts_sha256,
            latency_receipt_sha256=input_hashes["measured_latency"],
            terminal_receipt_sha256=input_hashes["terminal_coverage"],
        )
    except (ProvenanceError, RunnerContractError, ValueError) as exc:
        blockers.append(
            _blocker(
                "PROVENANCE_BINDING_INVALID",
                "PROVENANCE",
                str(exc),
            )
        )

    try:
        latency = _latency_p99_ns(latency_document)
    except (RunnerContractError, ValueError) as exc:
        latency = {"PLACE": 0, "CANCEL": 0, "IOC_EXIT": 0}
        blockers.append(
            _blocker(
                "REAL_LATENCY_PROFILE_INVALID",
                "LATENCY",
                str(exc),
            )
        )

    validated = preflight.get("validated_facts", {})
    release_facts = (
        validated.get("release_dq", {})
        if isinstance(validated, Mapping)
        else {}
    )
    terminal_sha = input_hashes["terminal_coverage"]
    strict_fill_sha = canonical_sha256(
        {
            "public_trades": top.get("public_trades"),
            "exit_snapshots": top.get("exit_snapshots"),
        }
    )
    card_sha = _text(
        "card_parameter_artifact_sha256",
        top.get("card_parameter_artifact_sha256"),
    )
    fee_binding_ready = (
        fee_facts is not None
        and not any(
            row["stage"].startswith("PREFLIGHT_FEE")
            or row["stage"] == "FEE"
            for row in blockers
        )
    )
    bindings = RuntimeBindings(
        fee_facts_sha256=(
            fee_facts.facts_sha256
            if fee_binding_ready
            else None
        ),
        latency_receipt_sha256=(
            input_hashes["measured_latency"]
            if not any(
                row["stage"].startswith("PREFLIGHT_LATENCY")
                or row["stage"] == "LATENCY"
                for row in blockers
            )
            else None
        ),
        root_map_sha256=(
            release_document.get("root_map_sha256")
            if isinstance(release_facts, Mapping)
            else None
        ),
        scheduled_start_source_sha256=(
            release_document.get("scheduled_start_sha256")
            if isinstance(release_facts, Mapping)
            else None
        ),
        risk_policy_sha256=_mapping(
            "provenance", top.get("provenance")
        ).get("risk_policy_sha256"),
        terminal_contract_sha256=(
            terminal_sha
            if not any(
                row["stage"].startswith("PREFLIGHT_CLOSURE")
                for row in blockers
            )
            else None
        ),
        strict_fill_evidence_sha256=strict_fill_sha,
        card_parameter_artifact_sha256=card_sha,
    )

    try:
        adapters = FrozenExperimentAdapters(freeze)
        rows = _parse_rows(top.get("rows"))
        decisions = adapters.evaluate_rows(
            experiment_id,
            rows,
            bindings,
        )
    except (AdapterContractError, RunnerContractError, ValueError) as exc:
        blockers.append(
            _blocker(
                "EXPERIMENT_ADAPTER_FAILED",
                "DECISION",
                str(exc),
            )
        )
        return _receipt(
            run_id=run_id,
            experiment_id=experiment_id,
            state=PNL_BLOCKED,
            run_binding_sha256=(
                run_binding.sha256 if run_binding else None
            ),
            preflight=preflight,
            path_rows=[],
            blockers=blockers,
            conservation={
                "public_source_quantity_e4": 0,
                "public_consumed_quantity_e4": 0,
                "entry_filled_quantity_e4": 0,
                "exit_filled_quantity_e4": 0,
                "residual_quantity_e4": 0,
            },
            trusted_authority_sha256=trusted_authority_sha256,
            trusted_lineage_receipt_sha256=(
                trusted_lineage_receipt_sha256
            ),
        )

    row_by_id = {row.row_id: row for row in rows}
    fee_contexts: dict[str, tuple[str, str]] = {}
    try:
        public_trades, trade_sources = _parse_public_trades(
            top.get("public_trades")
        )
        snapshots, snapshot_sources, exit_authority = _parse_snapshots(
            top.get("exit_snapshots")
        )
        closures = _parse_closures(top.get("closures"))
        settlements = _parse_settlements(top.get("settlements"))
        fee_contexts = _parse_fee_contexts(top.get("fee_contexts"))
        if run_binding is None:
            raise ProvenanceError(
                "evidence cannot bind without an exact RunBinding"
            )
        _validate_evidence_bindings(
            top,
            releases=run_binding.releases,
        )
    except (
        RunnerContractError,
        FillError,
        FeeFactsUnavailable,
        ProvenanceError,
        ValueError,
    ) as exc:
        blockers.append(
            _blocker("EVIDENCE_AUTHORITY_INVALID", "PROVENANCE", str(exc))
        )
        public_trades = ()
        trade_sources = {}
        snapshots = {}
        snapshot_sources = {}
        exit_authority = {}
        closures = {}
        settlements = {}

    passive_orders: list[PassiveOrder] = []
    intent_context: dict[str, tuple[StrategyDecision, NormalizedStateRow, Any]] = {}
    risk_ledger: RiskLedger | None = None
    risk_rejected_intents: set[str] = set()
    root_quantity: dict[str, int] = {}
    intent_rows = sorted(
        (
            (decision, row_by_id[decision.row_id], intent)
            for decision in decisions
            for intent in decision.intents
        ),
        key=lambda item: (
            item[1].decision_ts_ns,
            item[1].row_id,
            item[2].intent_id,
        ),
    )
    for decision, row, intent in intent_rows:
            intent_context[intent.intent_id] = (
                decision,
                row,
                intent,
            )
            outcome_side, price_e4 = _intent_outcome_and_price(
                intent.side, intent.price_e4
            )
            path_id = _path_id(run_id, row.row_id, intent.intent_id)
            date = _utc_date_from_ns("decision_ts_ns", row.decision_ts_ns)
            prospective_root_quantity = (
                root_quantity.get(row.root_event_id, 0)
                + intent.quantity_e4
            )
            if prospective_root_quantity > intent.root_entry_quantity_cap_e4:
                risk_rejected_intents.add(intent.intent_id)
                blockers.append(
                    _blocker(
                        "FROZEN_ROOT_CAP_EXCEEDED",
                        "RISK",
                        (
                            f"{date}/{row.root_event_id} "
                            f"quantity_e4={prospective_root_quantity} "
                            f"cap_e4={intent.root_entry_quantity_cap_e4}"
                        ),
                    )
                )
                continue
            if risk_limits is None:
                risk_rejected_intents.add(intent.intent_id)
                continue
            root_quantity[row.root_event_id] = prospective_root_quantity
            passive_orders.append(
                PassiveOrder(
                    order_id=intent.intent_id,
                    experiment_id=experiment_id,
                    root_id=row.root_event_id,
                    market_ticker=row.market_ticker,
                    side=outcome_side,
                    price_e4=price_e4,
                    quantity_e4=intent.quantity_e4,
                    activation_us=_ceil_ns_to_us(
                        row.decision_ts_ns + latency["PLACE"]
                    ),
                    cancel_effective_us=_ceil_ns_to_us(
                        row.decision_ts_ns
                        + intent.expire_after_ms * 1_000_000
                        + latency["CANCEL"]
                    ),
                )
            )

    closure_points: list[tuple[str, int]] = []
    for intent_id, (_, row, _) in intent_context.items():
        closure = closures.get(intent_id)
        if closure is None or closure.get("exit_decision_ts_ns") is None:
            continue
        closure_points.append(
            (
                row.market_ticker,
                _ceil_ns_to_us(
                    _plain_int(
                        "exit_decision_ts_ns",
                        closure.get("exit_decision_ts_ns"),
                        minimum=0,
                    )
                    + latency["IOC_EXIT"]
                )
                * 1_000,
            )
        )
    try:
        if fee_facts is None:
            raise FeeFactsUnavailable("verified fee facts are unavailable")
        fee_schedule = _materialize_fee_schedule(
            fee_facts,
            fee_contexts,
            public_trades=public_trades,
            closure_points=closure_points,
        )
    except (FeeFactsUnavailable, RunnerContractError, ValueError) as exc:
        fee_schedule = FeeSchedule(())
        blockers.append(
            _blocker("FEE_MATERIALIZATION_FAILED", "FEE", str(exc))
        )

    if experiment_id == "A01-SPREAD-CAPTURE":
        return _run_a01_paths(
            run_id=run_id,
            frozen_experiment=freeze,
            decisions=decisions,
            row_by_id=row_by_id,
            passive_orders=passive_orders,
            risk_rejected_intents=risk_rejected_intents,
            public_trades=public_trades,
            trade_sources=trade_sources,
            snapshots=snapshots,
            snapshot_sources=snapshot_sources,
            exit_authority=exit_authority,
            closures=closures,
            settlements=settlements,
            fee_schedule=fee_schedule,
            risk_limits=risk_limits,
            latency=latency,
            run_binding=run_binding,
            preflight=preflight,
            terminal_document=terminal_document,
            blockers=blockers,
            trusted_authority_sha256=trusted_authority_sha256,
            trusted_lineage_receipt_sha256=(
                trusted_lineage_receipt_sha256
            ),
        )

    try:
        entry_batch = allocate_passive_strict_fills(
            passive_orders,
            public_trades,
        )
    except FillError as exc:
        entry_batch = FillBatch(
            fills=(),
            ordered_e4=sum(order.quantity_e4 for order in passive_orders),
            filled_e4=0,
            unfilled_e4=sum(
                order.quantity_e4 for order in passive_orders
            ),
            source_quantity_e4=sum(
                trade.quantity_e4 for trade in public_trades
            ),
            consumed_source_quantity_e4=0,
            duplicate_source_allocations=0,
        )
        blockers.append(
            _blocker("ENTRY_FILL_AUTHORITY_FAILED", "FILL", str(exc))
        )
    entry_by_order: dict[str, list[FillSlice]] = {}
    for fill in entry_batch.fills:
        entry_by_order.setdefault(fill.order_id, []).append(fill)

    run_binding_sha = run_binding.sha256 if run_binding else "0" * 64
    path_rows: list[dict[str, Any]] = []
    ledgers: dict[str, PnLLedger] = {}
    context_by_path: dict[
        str, tuple[StrategyDecision, NormalizedStateRow, Any, Mapping[str, Any] | None]
    ] = {}

    # A no-trade baseline exists for every observed decision row, including
    # abstains and blocked rows, so denominator accounting cannot drop zeros.
    for decision in decisions:
        row = row_by_id[decision.row_id]
        baseline_path_id = _path_id(run_id, row.row_id, "BASELINE")
        baseline = PnLLedger(
            _path_spec(
                path_id=baseline_path_id,
                decision=decision,
                row=row,
                outcome=Outcome.YES,
                run_binding_sha256=run_binding_sha,
            ),
            fee_schedule,
        )
        path_rows.append(
            _zero_result_row(
                ledger=baseline,
                row_id=row.row_id,
                kind="BASELINE",
                decision=decision,
                occurred_at_ns=row.decision_ts_ns,
                source_sha256=decision.sha256,
                reason_codes=("NO_TRADE_SAME_OPPORTUNITIES",),
            )
        )
        if decision.status is DecisionStatus.BLOCKED:
            strategy_path_id = _path_id(run_id, row.row_id, "BLOCKED")
            path_rows.append(
                _blocked_row(
                    row_id=row.row_id,
                    kind="STRATEGY",
                    path_id=strategy_path_id,
                    decision_status=decision.status.value,
                    reason_codes=decision.reason_codes,
                )
            )
            blockers.extend(
                _blocker(reason, "DECISION", row.row_id)
                for reason in decision.reason_codes
            )
        elif decision.status is DecisionStatus.ABSTAIN:
            strategy_path_id = _path_id(run_id, row.row_id, "ABSTAIN")
            strategy = PnLLedger(
                _path_spec(
                    path_id=strategy_path_id,
                    decision=decision,
                    row=row,
                    outcome=Outcome.YES,
                    run_binding_sha256=run_binding_sha,
                ),
                fee_schedule,
            )
            path_rows.append(
                _zero_result_row(
                    ledger=strategy,
                    row_id=row.row_id,
                    kind="STRATEGY",
                    decision=decision,
                    occurred_at_ns=row.decision_ts_ns,
                    source_sha256=decision.sha256,
                    reason_codes=decision.reason_codes,
                )
            )

    failed_entry_paths: set[str] = set()
    for intent_id, (decision, row, intent) in intent_context.items():
        outcome_side, _ = _intent_outcome_and_price(
            intent.side, intent.price_e4
        )
        path_id = _path_id(run_id, row.row_id, intent_id)
        if intent_id in risk_rejected_intents:
            path_rows.append(
                _blocked_row(
                    row_id=row.row_id,
                    kind="STRATEGY",
                    path_id=path_id,
                    decision_status=decision.status.value,
                    reason_codes=("BLOCK_RISK_ADMISSION_FAILED",),
                )
            )
            continue
        ledger = PnLLedger(
            _path_spec(
                path_id=path_id,
                decision=decision,
                row=row,
                outcome=Outcome(outcome_side.value),
                run_binding_sha256=run_binding_sha,
            ),
            fee_schedule,
        )
        ledgers[path_id] = ledger
        context_by_path[path_id] = (
            decision,
            row,
            intent,
            closures.get(intent_id),
        )
        entry_fills = entry_by_order.get(intent_id, [])
        if not entry_fills:
            path_rows.append(
                _zero_result_row(
                    ledger=ledger,
                    row_id=row.row_id,
                    kind="STRATEGY",
                    decision=decision,
                    occurred_at_ns=(
                        row.decision_ts_ns
                        + intent.expire_after_ms * 1_000_000
                        + latency["CANCEL"]
                    ),
                    source_sha256=decision.sha256,
                    reason_codes=("TRIGGERED_NO_STRICT_FILL",),
                )
            )
            ledgers.pop(path_id)
            context_by_path.pop(path_id)
            continue
        try:
            for fill in entry_fills:
                ledger.record_fill(
                    _slice_to_fill(
                        fill,
                        path_id=path_id,
                        purpose=FillPurpose.ENTRY,
                        source_sha256=trade_sources[fill.source_id],
                    )
                )
        except (FeeTruthUnavailable, LedgerInvariantError, KeyError) as exc:
            blockers.append(
                _blocker(
                    "ENTRY_LEDGER_FAILED",
                    "FEE_LEDGER",
                    f"{path_id}: {exc}",
                )
            )
            failed_entry_paths.add(path_id)
            path_rows.append(
                _blocked_row(
                    row_id=row.row_id,
                    kind="STRATEGY",
                    path_id=path_id,
                    decision_status=decision.status.value,
                    reason_codes=("BLOCK_ENTRY_LEDGER_FAILED",),
                    residual_quantity_e4=abs(ledger.position_e4),
                    diagnostic={
                        "ledger_sha256": ledger.deterministic_sha256,
                    },
                )
            )

    for path_id in failed_entry_paths:
        ledgers.pop(path_id, None)
        context_by_path.pop(path_id, None)

    exit_batches: dict[str, FillBatch] = {}
    exit_source_alias: dict[str, str] = {}
    for path_id, ledger in ledgers.items():
        _, row, intent, closure = context_by_path[path_id]
        if ledger.position_e4 == 0:
            continue
        if closure is None or closure.get("exit_snapshot_id") is None:
            continue
        snapshot_id = _text(
            "exit_snapshot_id", closure.get("exit_snapshot_id")
        )
        try:
            exit_decision_ns = _plain_int(
                "exit_decision_ts_ns",
                closure.get("exit_decision_ts_ns"),
                minimum=0,
            )
            if intent.max_hold_ms is not None:
                first_entry_ns = min(
                    fill.timestamp_us
                    for fill in entry_by_order[intent.intent_id]
                ) * 1_000
                if (
                    exit_decision_ns
                    > first_entry_ns + intent.max_hold_ms * 1_000_000
                ):
                    raise FillError(
                        "exit decision exceeded frozen max_hold_ms"
                    )
            effective_us = _ceil_ns_to_us(
                exit_decision_ns + latency["IOC_EXIT"]
            )
            maximum_snapshot_age_us = _plain_int(
                "maximum_snapshot_age_us",
                closure.get("maximum_snapshot_age_us"),
                minimum=0,
            )
            snapshot = _latest_qualified_snapshot(
                snapshots,
                market_ticker=row.market_ticker,
                effective_timestamp_us=effective_us,
                maximum_snapshot_age_us=maximum_snapshot_age_us,
            )
            if snapshot.snapshot_id != snapshot_id:
                raise FillError(
                    "closure did not select the latest qualified L2 snapshot"
                )
            order = MarketableOrder(
                order_id=f"{intent.intent_id}:exit",
                experiment_id=experiment_id,
                root_id=row.root_event_id,
                market_ticker=row.market_ticker,
                side=OutcomeSide(ledger.path.outcome.value),
                action=OrderAction.SELL,
                limit_price_e4=_plain_int(
                    "exit_limit_price_e4",
                    closure.get("exit_limit_price_e4"),
                ),
                quantity_e4=abs(ledger.position_e4),
                effective_timestamp_us=effective_us,
            )
            batch = walk_exact_l2_ioc(
                order,
                snapshot,
                maximum_snapshot_age_us=maximum_snapshot_age_us,
            )
            exit_batches[path_id] = batch
            # The fill engine numbers the selected side from zero.  Give
            # portfolio conservation an unambiguous bid/ask authority key.
            consumes_asks = (
                order.side is OutcomeSide.NO
                and order.action is OrderAction.SELL
            )
            for fill in batch.fills:
                raw_key = fill.source_id
                level_number = raw_key.rsplit("=", 1)[1]
                authority_key = (
                    f"{snapshot_id}|ask-level={level_number}"
                    if consumes_asks
                    else raw_key
                )
                exit_source_alias[fill.fill_id] = authority_key
        except (KeyError, FillError, RunnerContractError, ValueError) as exc:
            blockers.append(
                _blocker(
                    "EXIT_IOC_FAILED",
                    "EXIT",
                    f"{path_id}: {exc}",
                )
            )

    # Rebuild tiny aliased batches solely for portfolio-level L2 conservation.
    conservation_batches: list[FillBatch] = []
    for batch in exit_batches.values():
        aliased = tuple(
            FillSlice(
                **{
                    **asdict(fill),
                    "side": fill.side,
                    "action": fill.action,
                    "liquidity_role": fill.liquidity_role,
                    "source_id": exit_source_alias[fill.fill_id],
                }
            )
            for fill in batch.fills
        )
        conservation_batches.append(
            FillBatch(
                fills=aliased,
                ordered_e4=batch.ordered_e4,
                filled_e4=batch.filled_e4,
                unfilled_e4=batch.unfilled_e4,
                source_quantity_e4=batch.source_quantity_e4,
                consumed_source_quantity_e4=(
                    batch.consumed_source_quantity_e4
                ),
                duplicate_source_allocations=0,
            )
        )
    try:
        assert_portfolio_fill_conservation(
            conservation_batches,
            authoritative_source_quantity=exit_authority,
        )
    except FillError as exc:
        blockers.append(
            _blocker(
                "EXIT_PUBLIC_DEPTH_REUSED",
                "CONSERVATION",
                str(exc),
            )
        )

    for path_id, ledger in ledgers.items():
        decision, row, intent, closure = context_by_path[path_id]
        if ledger.position_e4 == 0:
            continue
        try:
            batch = exit_batches.get(path_id)
            if batch is not None:
                snapshot_id = _text(
                    "exit_snapshot_id", closure.get("exit_snapshot_id")
                )
                for fill in batch.fills:
                    ledger.record_fill(
                        _slice_to_fill(
                            fill,
                            path_id=path_id,
                            purpose=FillPurpose.EXIT,
                            source_sha256=snapshot_sources[snapshot_id],
                        )
                    )
            if (
                ledger.position_e4 != 0
                and closure is not None
                and closure.get("settlement_id") is not None
            ):
                settlement_id = _text(
                    "settlement_id", closure.get("settlement_id")
                )
                ledger.observe_settlement(
                    _settlement_record(
                        settlements[settlement_id],
                        outcome=ledger.path.outcome,
                    )
                )
            if ledger.position_e4 != 0:
                blockers.append(
                    _blocker(
                        "RESIDUAL_POSITION_OPEN",
                        "CLOSURE",
                        f"{path_id} residual_e4={ledger.position_e4}",
                    )
                )
                path_rows.append(
                    _blocked_row(
                        row_id=row.row_id,
                        kind="STRATEGY",
                        path_id=path_id,
                        decision_status=decision.status.value,
                        reason_codes=("BLOCK_RESIDUAL_POSITION",),
                        residual_quantity_e4=abs(ledger.position_e4),
                        diagnostic={
                            "ledger_sha256": ledger.deterministic_sha256,
                            "entry_fill_count": len(
                                entry_by_order.get(intent.intent_id, [])
                            ),
                            "exit_fill_count": len(
                                exit_batches.get(
                                    path_id,
                                    FillBatch((), 0, 0, 0, 0, 0, 0),
                                ).fills
                            ),
                        },
                    )
                )
                continue
            result = ledger.finalize()
            path_rows.append(
                {
                    "row_id": row.row_id,
                    "kind": "STRATEGY",
                    "path_id": path_id,
                    "decision_status": decision.status.value,
                    "reason_codes": list(decision.reason_codes),
                    "state": PATH_COMPLETE,
                    "result": _result_mapping(result),
                    "residual_quantity_e4": 0,
                }
            )
        except (
            FeeTruthUnavailable,
            IncompletePnL,
            LedgerInvariantError,
            KeyError,
            RunnerContractError,
            ValueError,
        ) as exc:
            blockers.append(
                _blocker(
                    "PATH_FINALIZATION_FAILED",
                    "CLOSURE",
                    f"{path_id}: {exc}",
                )
            )
            path_rows.append(
                _blocked_row(
                    row_id=row.row_id,
                    kind="STRATEGY",
                    path_id=path_id,
                    decision_status=decision.status.value,
                    reason_codes=("BLOCK_PATH_FINALIZATION_FAILED",),
                    residual_quantity_e4=abs(ledger.position_e4),
                    diagnostic={
                        "ledger_sha256": ledger.deterministic_sha256,
                    },
                )
            )

    if risk_limits is not None:
        result_by_path = {
            row["path_id"]: row["result"]
            for row in path_rows
            if (
                row.get("kind") == "STRATEGY"
                and row.get("state") == PATH_COMPLETE
                and isinstance(row.get("result"), Mapping)
            )
        }
        try:
            risk_ledger, risk_replay_blockers = _replay_risk_lifecycle(
                limits=risk_limits,
                run_id=run_id,
                intent_context=intent_context,
                excluded_intents=risk_rejected_intents,
                entry_by_order=entry_by_order,
                trade_sources=trade_sources,
                exit_batches=exit_batches,
                snapshot_sources=snapshot_sources,
                closures=closures,
                settlements=settlements,
                pnl_ledgers=ledgers,
                result_by_path=result_by_path,
                latency=latency,
            )
            blockers.extend(risk_replay_blockers)
        except (
            KeyError,
            RiskInvariantError,
            RunnerContractError,
            ValueError,
        ) as exc:
            risk_ledger = RiskLedger(risk_limits)
            blockers.append(
                _blocker(
                    "RISK_LIFECYCLE_REPLAY_FAILED",
                    "RISK",
                    str(exc),
                )
            )

    path_rows.sort(
        key=lambda row: (
            row["row_id"],
            row["kind"],
            row["path_id"],
        )
    )
    actual_residual = sum(
        int(row.get("residual_quantity_e4", 0))
        for row in path_rows
    )
    terminal_path_count = terminal_document.get("path_count")
    if terminal_path_count != len(path_rows):
        blockers.append(
            _blocker(
                "TERMINAL_PATH_COUNT_RUNTIME_MISMATCH",
                "CLOSURE",
                f"receipt={terminal_path_count} runtime={len(path_rows)}",
            )
        )
    if terminal_document.get("residual_quantity_e4") != actual_residual:
        blockers.append(
            _blocker(
                "TERMINAL_RESIDUAL_RUNTIME_MISMATCH",
                "CLOSURE",
                "terminal receipt residual does not match runtime ledger",
            )
        )
    if run_binding is None:
        blockers.append(
            _blocker(
                "RUN_BINDING_MISSING",
                "PROVENANCE",
                "no exact immutable run binding was established",
            )
        )
    if preflight["net_pnl"]["state"] != NET_READY:
        blockers.append(
            _blocker(
                "NET_PREFLIGHT_NOT_READY",
                "PREFLIGHT",
                "fee, real latency, data, closure, or training gate failed",
            )
        )
    if any(row["state"] != PATH_COMPLETE for row in path_rows):
        blockers.append(
            _blocker(
                "NOT_ALL_PATHS_COMPLETE",
                "CLOSURE",
                "every strategy and baseline path must close",
            )
        )

    public_source = sum(trade.quantity_e4 for trade in public_trades)
    exit_filled = sum(
        batch.filled_e4 for batch in exit_batches.values()
    )
    conservation = {
        "public_source_quantity_e4": public_source,
        "public_consumed_quantity_e4": (
            entry_batch.consumed_source_quantity_e4
        ),
        "entry_filled_quantity_e4": entry_batch.filled_e4,
        "exit_filled_quantity_e4": exit_filled,
        "residual_quantity_e4": actual_residual,
    }
    final_state = NET_COMPLETE if not blockers else PNL_BLOCKED
    return _receipt(
        run_id=run_id,
        experiment_id=experiment_id,
        state=final_state,
        run_binding_sha256=(
            run_binding.sha256 if run_binding is not None else None
        ),
        preflight=preflight,
        path_rows=path_rows,
        blockers=blockers,
        conservation=conservation,
        risk_ledger_sha256=(
            risk_ledger.deterministic_sha256
            if risk_ledger is not None
            else None
        ),
        trusted_authority_sha256=trusted_authority_sha256,
        trusted_lineage_receipt_sha256=(
            trusted_lineage_receipt_sha256
        ),
    )


def _reject_duplicate_keys(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RunnerContractError("fixture contains duplicate JSON keys")
        result[key] = value
    return result


def _read_json_once(
    path: Path,
    *,
    name: str,
) -> tuple[Mapping[str, Any], str]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RunnerContractError(f"{name} must be a regular file")
        if metadata.st_size > MAX_FIXTURE_BYTES:
            raise RunnerContractError(f"{name} exceeds 16 MiB")
        chunks: list[bytes] = []
        remaining = MAX_FIXTURE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_FIXTURE_BYTES:
        raise RunnerContractError(f"{name} exceeds 16 MiB")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(
                RunnerContractError(
                    f"non-finite JSON token is forbidden: {token}"
                )
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerContractError(
            f"{name} is not strict UTF-8 JSON"
        ) from exc
    return _mapping(name, value), hashlib.sha256(raw).hexdigest()


def _read_fixture(path: Path) -> Mapping[str, Any]:
    value, _ = _read_json_once(path, name="fixture")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="offline frozen-experiment PnL-spine runner"
    )
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--trusted-authority", required=True)
    parser.add_argument(
        "--trusted-authority-sha256",
        required=True,
        help="SHA-256 of the raw trusted-authority file bytes",
    )
    parser.add_argument("--trusted-lineage-receipt", required=True)
    parser.add_argument(
        "--trusted-lineage-receipt-sha256",
        required=True,
        help="SHA-256 of the raw trusted-lineage file bytes",
    )
    parser.add_argument("--output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    authority_path = Path(args.trusted_authority)
    authority, authority_raw_sha256 = _read_json_once(
        authority_path,
        name="trusted_authority",
    )
    if authority_raw_sha256 != args.trusted_authority_sha256:
        raise RunnerContractError(
            "trusted authority file SHA-256 does not match external pin"
        )
    lineage_path = Path(args.trusted_lineage_receipt)
    lineage, lineage_raw_sha256 = _read_json_once(
        lineage_path,
        name="trusted_lineage_receipt",
    )
    if (
        lineage_raw_sha256
        != args.trusted_lineage_receipt_sha256
    ):
        raise RunnerContractError(
            "trusted lineage receipt file SHA-256 does not match external pin"
        )
    receipt = run_fixture(
        _read_fixture(Path(args.fixture)),
        trusted_authority=authority,
        expected_trusted_authority_sha256=(
            canonical_sha256(authority)
        ),
        trusted_lineage_receipt=lineage,
        expected_trusted_lineage_receipt_sha256=(
            canonical_sha256(lineage)
        ),
    )
    if args.output:
        atomic_write_receipt(Path(args.output), receipt)
    else:
        sys.stdout.buffer.write(canonical_json_bytes(receipt) + b"\n")
    return 0 if receipt["state"] == NET_COMPLETE else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "C1_CLASSIFICATION",
    "FIXTURE_SCHEMA",
    "HYBRID_LINEAGE_RECEIPT_SCHEMA",
    "HYBRID_LINEAGE_RECORD_SCHEMA_SHA256",
    "LINEAGE_RECEIPT_SCHEMA",
    "LINEAGE_RECORD_SCHEMA_SHA256",
    "NET_COMPLETE",
    "PATH_COMPLETE",
    "PNL_BLOCKED",
    "RECEIPT_SCHEMA",
    "RunnerContractError",
    "run_fixture",
]
