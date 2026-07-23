#!/usr/bin/env python3
"""Offline, fail-closed end-to-end runner for frozen PnL experiments.

The runner is deliberately a pure research boundary:

* it performs no network, AWS, credential, or order operation;
* frozen adapters create the order intents;
* public maker fills require strict-through evidence and are allocated once;
* every exit is an exact receive-clock L2 IOC or a finalized exact payout;
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
    PassiveOrder,
    PublicTrade,
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
from .terminal import SettlementRecord, SettlementStatus


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
TRUSTED_AUTHORITY_SCHEMA = "pnl-spine-trusted-authority-v1"


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
        if (
            row.get("status") == SettlementStatus.FINALIZED.value
            and row.get("finalized") is True
        ):
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


def _validate_evidence_bindings(
    fixture: Mapping[str, Any],
    *,
    releases: Sequence[ExactReleaseBinding],
) -> dict[tuple[str, str], ExactSourceObject]:
    """Bind every runtime record to one exact-version source object.

    The binding covers the canonical record bytes, exact release/object
    identity, object digest, source channel and the UTC date implied by the
    record's causal timestamp.  This rejects cross-era fixtures such as 1970
    rows presented under a 2026 release.
    """

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

    release_by_id = {release.release_id: release for release in releases}
    object_index: dict[
        tuple[str, str, str], ExactSourceObject
    ] = {}
    for release in releases:
        for source in release.objects:
            object_index[
                (release.release_id, source.logical_key, source.version_id)
            ] = source

    bound: dict[tuple[str, str], ExactSourceObject] = {}
    allowed = {
        "kind",
        "record_id",
        "record_sha256",
        "release_id",
        "source_object_logical_key",
        "source_object_version_id",
        "source_object_sha256",
    }
    for index, raw in enumerate(
        _list("evidence_bindings", fixture.get("evidence_bindings"))
    ):
        binding = _mapping(f"evidence_bindings[{index}]", raw)
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
        "totals": totals,
        "blockers": _sorted_blockers(blockers),
        "c1_prior_artifact_classification": C1_CLASSIFICATION,
    }
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


def run_fixture(
    fixture: Mapping[str, Any],
    *,
    trusted_authority: Mapping[str, Any] | None = None,
    expected_trusted_authority_sha256: str | None = None,
) -> dict[str, Any]:
    """Run one small canonical fixture and return a canonicalizable receipt."""

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
    trusted_authority_sha256: str | None = None
    try:
        trusted_authority_sha256 = _validate_trusted_authority(
            top,
            trusted_authority,
            expected_sha256=expected_trusted_authority_sha256,
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
    risk_ledger = RiskLedger(risk_limits) if risk_limits is not None else None
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
            if risk_ledger is None:
                risk_rejected_intents.add(intent.intent_id)
                continue
            try:
                risk_ledger.reserve(
                    RiskRequest(
                        reservation_id=intent.intent_id,
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
                    ),
                    event_id=f"reserve:{intent.intent_id}",
                )
            except (
                RiskInvariantError,
                RiskLimitExceeded,
                ValueError,
            ) as exc:
                risk_rejected_intents.add(intent.intent_id)
                blockers.append(
                    _blocker(
                        "RISK_LEDGER_RESERVATION_REJECTED",
                        "RISK",
                        f"{intent.intent_id}: {exc}",
                    )
                )
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
    parser.add_argument("--trusted-authority-sha256", required=True)
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
    receipt = run_fixture(
        _read_fixture(Path(args.fixture)),
        trusted_authority=authority,
        expected_trusted_authority_sha256=(
            args.trusted_authority_sha256
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
    "NET_COMPLETE",
    "PATH_COMPLETE",
    "PNL_BLOCKED",
    "RECEIPT_SCHEMA",
    "RunnerContractError",
    "run_fixture",
]
