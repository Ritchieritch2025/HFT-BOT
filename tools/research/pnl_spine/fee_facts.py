"""Offline, integrity-bound fee facts for the shared research PnL spine.

This module deliberately does not fetch venue data.  It consumes one
canonical JSON document whose source hashes bind:

* the official fee-schedule document;
* a complete, historical series-fee snapshot;
* the event-fee snapshot and explicit event/waiver assertions; and
* a row-free aggregate of privately observed ``fee_cost`` values.

The facts layer resolves *which* multiplier applies.  Arithmetic, centicent
rounding, balance precision, and the per-order accumulator remain owned by
``fees.py``.  Missing event/waiver truth, unknown account precision, source
hash drift, unsupported fee modes, and observations newer than the source
snapshot all fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import (
    FillRecord,
    LiquidityRole,
    canonical_json_bytes,
    canonical_sha256,
    require_int,
    require_nonempty,
    require_sha256,
)
from .fees import (
    FeeAssessment,
    FeePrecision,
    FeeRule,
    FeeSchedule,
    FeeTruthUnavailable,
)


FEE_FACTS_SCHEMA_VERSION = "pnl_fee_facts_v1"

# Official July 7, 2026 quadratic rates and defaults, represented in e4.
OFFICIAL_TAKER_RATE_E4 = 700
OFFICIAL_MAKER_RATE_E4 = 175
OFFICIAL_DEFAULT_TAKER_MULTIPLIER_E4 = 10_000
OFFICIAL_DEFAULT_MAKER_MULTIPLIER_E4 = 0

FEE_TYPE_QUADRATIC = "quadratic"
FEE_TYPE_QUADRATIC_WITH_MAKER = "quadratic_with_maker_fees"
FEE_TYPE_FLAT = "flat"
_KNOWN_FEE_TYPES = frozenset(
    (
        FEE_TYPE_QUADRATIC,
        FEE_TYPE_QUADRATIC_WITH_MAKER,
        FEE_TYPE_FLAT,
    )
)

WAIVER_NONE = "NO_WAIVER"
WAIVER_ACTIVE = "WAIVED"
_KNOWN_WAIVER_STATES = frozenset((WAIVER_NONE, WAIVER_ACTIVE))


class FeeFactsUnavailable(FeeTruthUnavailable):
    """Fee facts cannot safely support a net-PnL calculation."""


class FeeFactsIntegrityError(FeeFactsUnavailable):
    """A facts document or one of its source bindings is malformed."""


def _exact_keys(
    name: str,
    value: Mapping[str, Any],
    expected: Sequence[str],
) -> None:
    actual = frozenset(value)
    wanted = frozenset(expected)
    if actual != wanted:
        missing = sorted(wanted - actual)
        extra = sorted(actual - wanted)
        raise FeeFactsIntegrityError(
            f"{name} keys differ; missing={missing}, extra={extra}"
        )


def _mapping(name: str, value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FeeFactsIntegrityError(f"{name} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise FeeFactsIntegrityError(f"{name} requires string keys")
    return value


def _array(name: str, value: object) -> Sequence[Any]:
    if not isinstance(value, list):
        raise FeeFactsIntegrityError(f"{name} must be an array")
    return value


def _optional_end_ns(
    name: str,
    value: object,
    *,
    start_ns: int,
) -> int | None:
    if value is None:
        return None
    return require_int(name, value, minimum=start_ns + 1)


def _reject_json_float(_: str) -> object:
    raise FeeFactsIntegrityError(
        "fee facts forbid floating-point JSON numbers"
    )


def _reject_json_constant(value: str) -> object:
    raise FeeFactsIntegrityError(
        f"fee facts forbid non-finite JSON constant {value}"
    )


def _no_duplicate_object(
    pairs: Sequence[tuple[str, Any]],
) -> Mapping[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FeeFactsIntegrityError(f"duplicate JSON key {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class FormulaFacts:
    effective_from_ns: int
    effective_until_ns: int | None
    taker_rate_e4: int
    maker_rate_e4: int
    default_taker_multiplier_e4: int
    default_maker_multiplier_e4: int

    def __post_init__(self) -> None:
        require_int("effective_from_ns", self.effective_from_ns, minimum=0)
        if self.effective_until_ns is not None:
            require_int(
                "effective_until_ns",
                self.effective_until_ns,
                minimum=self.effective_from_ns + 1,
            )
        expected = {
            "taker_rate_e4": OFFICIAL_TAKER_RATE_E4,
            "maker_rate_e4": OFFICIAL_MAKER_RATE_E4,
            "default_taker_multiplier_e4": (
                OFFICIAL_DEFAULT_TAKER_MULTIPLIER_E4
            ),
            "default_maker_multiplier_e4": (
                OFFICIAL_DEFAULT_MAKER_MULTIPLIER_E4
            ),
        }
        for name, official_value in expected.items():
            actual = require_int(name, getattr(self, name), minimum=0)
            if actual != official_value:
                raise FeeFactsIntegrityError(
                    f"{name}={actual} disagrees with official "
                    f"value {official_value}"
                )

    def owns(self, executed_at_ns: int) -> bool:
        return (
            executed_at_ns >= self.effective_from_ns
            and (
                self.effective_until_ns is None
                or executed_at_ns < self.effective_until_ns
            )
        )


@dataclass(frozen=True)
class SourceBindings:
    fee_schedule_pdf_sha256: str
    series_history_snapshot_sha256: str
    series_history_captured_at_ns: int
    series_history_includes_historical: bool
    event_history_snapshot_sha256: str
    private_actual_fee_aggregate_receipt_sha256: str

    def __post_init__(self) -> None:
        require_sha256(
            "fee_schedule_pdf_sha256",
            self.fee_schedule_pdf_sha256,
        )
        require_sha256(
            "series_history_snapshot_sha256",
            self.series_history_snapshot_sha256,
        )
        require_int(
            "series_history_captured_at_ns",
            self.series_history_captured_at_ns,
            minimum=0,
        )
        if self.series_history_includes_historical is not True:
            raise FeeFactsIntegrityError(
                "series history must be captured with show_historical=true"
            )
        require_sha256(
            "event_history_snapshot_sha256",
            self.event_history_snapshot_sha256,
        )
        require_sha256(
            "private_actual_fee_aggregate_receipt_sha256",
            self.private_actual_fee_aggregate_receipt_sha256,
        )


@dataclass(frozen=True)
class PrivateFeeAggregate:
    """Non-sensitive aggregate binding for observed private ``fee_cost``."""

    actual_fee_field: str
    fill_count: int
    maker_fill_count: int
    source_private_fill_receipt_sha256: str
    taker_fill_count: int
    total_actual_fee_cost_e6: int

    def __post_init__(self) -> None:
        if self.actual_fee_field != "fee_cost":
            raise FeeFactsIntegrityError(
                "private aggregate must bind the venue fee_cost field"
            )
        require_int("fill_count", self.fill_count, minimum=0)
        require_int("maker_fill_count", self.maker_fill_count, minimum=0)
        require_int("taker_fill_count", self.taker_fill_count, minimum=0)
        require_int(
            "total_actual_fee_cost_e6",
            self.total_actual_fee_cost_e6,
            minimum=0,
        )
        require_sha256(
            "source_private_fill_receipt_sha256",
            self.source_private_fill_receipt_sha256,
        )
        if self.maker_fill_count + self.taker_fill_count != self.fill_count:
            raise FeeFactsIntegrityError(
                "maker/taker aggregate counts do not equal fill_count"
            )

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True)
class SeriesFeeChange:
    change_id: str
    series_ticker: str
    effective_from_ns: int
    fee_type: str
    multiplier_e4: int

    def __post_init__(self) -> None:
        require_nonempty("change_id", self.change_id)
        require_nonempty("series_ticker", self.series_ticker)
        require_int("effective_from_ns", self.effective_from_ns, minimum=0)
        if self.fee_type not in _KNOWN_FEE_TYPES:
            raise FeeFactsIntegrityError(
                f"unknown series fee type {self.fee_type}"
            )
        require_int("multiplier_e4", self.multiplier_e4, minimum=0)


@dataclass(frozen=True)
class EventFeeAssertion:
    """An explicit effective-dated event override and waiver assertion.

    ``NO_WAIVER`` plus no fee override means "inherit the series rule."  A
    waived interval resolves both maker and taker multipliers to zero.  There
    is intentionally no implicit "no waiver" default.
    """

    assertion_id: str
    event_ticker: str
    effective_from_ns: int
    effective_until_ns: int | None
    waiver_state: str
    fee_type_override: str | None
    multiplier_e4_override: int | None
    source_sha256: str

    def __post_init__(self) -> None:
        require_nonempty("assertion_id", self.assertion_id)
        require_nonempty("event_ticker", self.event_ticker)
        require_int("effective_from_ns", self.effective_from_ns, minimum=0)
        if self.effective_until_ns is not None:
            require_int(
                "effective_until_ns",
                self.effective_until_ns,
                minimum=self.effective_from_ns + 1,
            )
        if self.waiver_state not in _KNOWN_WAIVER_STATES:
            raise FeeFactsIntegrityError(
                f"event waiver state is unknown: {self.waiver_state}"
            )
        require_sha256("source_sha256", self.source_sha256)
        if self.fee_type_override is None:
            if self.multiplier_e4_override is not None:
                raise FeeFactsIntegrityError(
                    "event multiplier requires a fee type override"
                )
        else:
            if self.fee_type_override not in _KNOWN_FEE_TYPES:
                raise FeeFactsIntegrityError(
                    f"unknown event fee type {self.fee_type_override}"
                )
            require_int(
                "multiplier_e4_override",
                self.multiplier_e4_override,
                minimum=0,
            )
        if (
            self.waiver_state == WAIVER_ACTIVE
            and self.fee_type_override is not None
        ):
            raise FeeFactsIntegrityError(
                "a waived interval cannot also carry a fee override"
            )

    def owns(self, executed_at_ns: int) -> bool:
        return (
            executed_at_ns >= self.effective_from_ns
            and (
                self.effective_until_ns is None
                or executed_at_ns < self.effective_until_ns
            )
        )


def _event_intervals_overlap(
    left: EventFeeAssertion,
    right: EventFeeAssertion,
) -> bool:
    return (
        (
            right.effective_until_ns is None
            or left.effective_from_ns < right.effective_until_ns
        )
        and (
            left.effective_until_ns is None
            or right.effective_from_ns < left.effective_until_ns
        )
    )


def _multipliers_for_fee_type(
    *,
    fee_type: str,
    multiplier_e4: int,
) -> tuple[int, int]:
    """Return ``(taker_multiplier_e4, maker_multiplier_e4)``."""

    if fee_type == FEE_TYPE_QUADRATIC:
        return (multiplier_e4, OFFICIAL_DEFAULT_MAKER_MULTIPLIER_E4)
    if fee_type == FEE_TYPE_QUADRATIC_WITH_MAKER:
        return (multiplier_e4, multiplier_e4)
    if fee_type == FEE_TYPE_FLAT:
        raise FeeFactsUnavailable(
            "flat fee truth is known but unsupported by the quadratic spine"
        )
    raise FeeFactsUnavailable(f"unsupported fee type {fee_type}")


@dataclass(frozen=True)
class FeeFacts:
    formula: FormulaFacts
    account_precision: FeePrecision
    account_precision_source_sha256: str
    sources: SourceBindings
    private_fee_aggregate: PrivateFeeAggregate
    series_changes: tuple[SeriesFeeChange, ...]
    event_assertions: tuple[EventFeeAssertion, ...]
    facts_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.account_precision, FeePrecision):
            raise FeeFactsIntegrityError(
                "account balance precision is unknown"
            )
        require_sha256(
            "account_precision_source_sha256",
            self.account_precision_source_sha256,
        )
        require_sha256("facts_sha256", self.facts_sha256)
        if (
            self.private_fee_aggregate.receipt_sha256
            != self.sources.private_actual_fee_aggregate_receipt_sha256
        ):
            raise FeeFactsIntegrityError(
                "private fee aggregate receipt SHA does not match its binding"
            )

        seen_series_points: set[tuple[str, int]] = set()
        for change in self.series_changes:
            point = (change.series_ticker, change.effective_from_ns)
            if point in seen_series_points:
                raise FeeFactsIntegrityError(
                    "ambiguous series changes at "
                    f"{change.series_ticker}/{change.effective_from_ns}"
                )
            seen_series_points.add(point)

        for index, assertion in enumerate(self.event_assertions):
            for other in self.event_assertions[index + 1 :]:
                if (
                    assertion.event_ticker == other.event_ticker
                    and _event_intervals_overlap(assertion, other)
                ):
                    raise FeeFactsIntegrityError(
                        "overlapping event assertions "
                        f"{assertion.assertion_id}/{other.assertion_id}"
                    )

    def _validate_context(
        self,
        fill: FillRecord,
        *,
        series_ticker: str,
        event_ticker: str,
    ) -> EventFeeAssertion:
        require_nonempty("series_ticker", series_ticker)
        require_nonempty("event_ticker", event_ticker)
        if not (
            fill.market_ticker == series_ticker
            or fill.market_ticker.startswith(series_ticker + "-")
        ):
            raise FeeFactsUnavailable(
                "market ticker is not bound to the supplied series"
            )
        if not (
            fill.market_ticker == event_ticker
            or fill.market_ticker.startswith(event_ticker + "-")
        ):
            raise FeeFactsUnavailable(
                "market ticker is not bound to the supplied event"
            )
        if not (
            event_ticker == series_ticker
            or event_ticker.startswith(series_ticker + "-")
        ):
            raise FeeFactsUnavailable(
                "event ticker is not bound to the supplied series"
            )
        if not self.formula.owns(fill.executed_at_ns):
            raise FeeFactsUnavailable(
                "fill is outside the bound official formula interval"
            )
        if fill.executed_at_ns > self.sources.series_history_captured_at_ns:
            raise FeeFactsUnavailable(
                "fill is newer than the bound series-history snapshot"
            )
        matching = [
            assertion
            for assertion in self.event_assertions
            if (
                assertion.event_ticker == event_ticker
                and assertion.owns(fill.executed_at_ns)
            )
        ]
        if len(matching) != 1:
            raise FeeFactsUnavailable(
                "event override/waiver truth is missing or ambiguous for "
                f"{event_ticker} at {fill.executed_at_ns}"
            )
        return matching[0]

    def resolve_multipliers(
        self,
        fill: FillRecord,
        *,
        series_ticker: str,
        event_ticker: str,
    ) -> tuple[int, int, str]:
        """Resolve taker/maker multipliers and the owning scope."""

        event = self._validate_context(
            fill,
            series_ticker=series_ticker,
            event_ticker=event_ticker,
        )
        if event.waiver_state == WAIVER_ACTIVE:
            return (0, 0, f"EVENT_WAIVER:{event.assertion_id}")
        if event.fee_type_override is not None:
            taker, maker = _multipliers_for_fee_type(
                fee_type=event.fee_type_override,
                multiplier_e4=event.multiplier_e4_override,
            )
            return (taker, maker, f"EVENT_OVERRIDE:{event.assertion_id}")

        candidates = [
            change
            for change in self.series_changes
            if (
                change.series_ticker == series_ticker
                and change.effective_from_ns <= fill.executed_at_ns
            )
        ]
        if candidates:
            change = max(
                candidates,
                key=lambda item: item.effective_from_ns,
            )
            taker, maker = _multipliers_for_fee_type(
                fee_type=change.fee_type,
                multiplier_e4=change.multiplier_e4,
            )
            return (taker, maker, f"SERIES:{change.change_id}")

        return (
            self.formula.default_taker_multiplier_e4,
            self.formula.default_maker_multiplier_e4,
            "OFFICIAL_DEFAULT",
        )

    def schedule_for_fill(
        self,
        fill: FillRecord,
        *,
        series_ticker: str,
        event_ticker: str,
    ) -> FeeSchedule:
        """Build an exact-market, one-timestamp schedule for ``fill``."""

        taker_multiplier_e4, maker_multiplier_e4, owner = (
            self.resolve_multipliers(
                fill,
                series_ticker=series_ticker,
                event_ticker=event_ticker,
            )
        )
        start_ns = fill.executed_at_ns
        end_ns = start_ns + 1
        owner_slug = owner.replace(":", "-")
        return FeeSchedule(
            (
                FeeRule(
                    rule_id=(
                        f"facts-{self.facts_sha256[:12]}-{owner_slug}-taker"
                    ),
                    liquidity_role=LiquidityRole.TAKER,
                    rate_e4=self.formula.taker_rate_e4,
                    multiplier_e4=taker_multiplier_e4,
                    precision=self.account_precision,
                    effective_from_ns=start_ns,
                    effective_until_ns=end_ns,
                    source_sha256=self.facts_sha256,
                    market_ticker=fill.market_ticker,
                ),
                FeeRule(
                    rule_id=(
                        f"facts-{self.facts_sha256[:12]}-{owner_slug}-maker"
                    ),
                    liquidity_role=LiquidityRole.MAKER,
                    rate_e4=self.formula.maker_rate_e4,
                    multiplier_e4=maker_multiplier_e4,
                    precision=self.account_precision,
                    effective_from_ns=start_ns,
                    effective_until_ns=end_ns,
                    source_sha256=self.facts_sha256,
                    market_ticker=fill.market_ticker,
                ),
            )
        )

    def assess(
        self,
        fill: FillRecord,
        *,
        series_ticker: str,
        event_ticker: str,
        rounding_accumulator_before_e6: int = 0,
    ) -> FeeAssessment:
        """Assess a fill only after all contextual fee truth is bound."""

        if (
            fill.actual_private_fee_e6 is not None
            and fill.actual_private_fee_receipt_sha256
            != self.private_fee_aggregate.source_private_fill_receipt_sha256
        ):
            raise FeeFactsUnavailable(
                "actual private fee is not bound to the calibrated receipt"
            )
        schedule = self.schedule_for_fill(
            fill,
            series_ticker=series_ticker,
            event_ticker=event_ticker,
        )
        return schedule.assess(
            fill,
            rounding_accumulator_before_e6=(
                rounding_accumulator_before_e6
            ),
        )


def _parse_formula(value: object) -> FormulaFacts:
    raw = _mapping("formula", value)
    _exact_keys(
        "formula",
        raw,
        (
            "default_maker_multiplier_e4",
            "default_taker_multiplier_e4",
            "effective_from_ns",
            "effective_until_ns",
            "maker_rate_e4",
            "taker_rate_e4",
        ),
    )
    start_ns = require_int(
        "formula.effective_from_ns",
        raw["effective_from_ns"],
        minimum=0,
    )
    return FormulaFacts(
        effective_from_ns=start_ns,
        effective_until_ns=_optional_end_ns(
            "formula.effective_until_ns",
            raw["effective_until_ns"],
            start_ns=start_ns,
        ),
        taker_rate_e4=raw["taker_rate_e4"],
        maker_rate_e4=raw["maker_rate_e4"],
        default_taker_multiplier_e4=(
            raw["default_taker_multiplier_e4"]
        ),
        default_maker_multiplier_e4=(
            raw["default_maker_multiplier_e4"]
        ),
    )


def _parse_sources(value: object) -> SourceBindings:
    raw = _mapping("source_bindings", value)
    _exact_keys(
        "source_bindings",
        raw,
        (
            "event_history_snapshot_sha256",
            "fee_schedule_pdf_sha256",
            "private_actual_fee_aggregate_receipt_sha256",
            "series_history_captured_at_ns",
            "series_history_includes_historical",
            "series_history_snapshot_sha256",
        ),
    )
    return SourceBindings(
        fee_schedule_pdf_sha256=raw["fee_schedule_pdf_sha256"],
        series_history_snapshot_sha256=(
            raw["series_history_snapshot_sha256"]
        ),
        series_history_captured_at_ns=(
            raw["series_history_captured_at_ns"]
        ),
        series_history_includes_historical=(
            raw["series_history_includes_historical"]
        ),
        event_history_snapshot_sha256=(
            raw["event_history_snapshot_sha256"]
        ),
        private_actual_fee_aggregate_receipt_sha256=(
            raw["private_actual_fee_aggregate_receipt_sha256"]
        ),
    )


def _parse_private_aggregate(value: object) -> PrivateFeeAggregate:
    raw = _mapping("private_actual_fee_aggregate", value)
    _exact_keys(
        "private_actual_fee_aggregate",
        raw,
        (
            "actual_fee_field",
            "fill_count",
            "maker_fill_count",
            "source_private_fill_receipt_sha256",
            "taker_fill_count",
            "total_actual_fee_cost_e6",
        ),
    )
    return PrivateFeeAggregate(
        actual_fee_field=raw["actual_fee_field"],
        fill_count=raw["fill_count"],
        maker_fill_count=raw["maker_fill_count"],
        source_private_fill_receipt_sha256=(
            raw["source_private_fill_receipt_sha256"]
        ),
        taker_fill_count=raw["taker_fill_count"],
        total_actual_fee_cost_e6=raw["total_actual_fee_cost_e6"],
    )


def _parse_series_changes(value: object) -> tuple[SeriesFeeChange, ...]:
    result: list[SeriesFeeChange] = []
    for index, item in enumerate(_array("series_changes", value)):
        raw = _mapping(f"series_changes[{index}]", item)
        _exact_keys(
            f"series_changes[{index}]",
            raw,
            (
                "change_id",
                "effective_from_ns",
                "fee_type",
                "multiplier_e4",
                "series_ticker",
            ),
        )
        result.append(
            SeriesFeeChange(
                change_id=raw["change_id"],
                series_ticker=raw["series_ticker"],
                effective_from_ns=raw["effective_from_ns"],
                fee_type=raw["fee_type"],
                multiplier_e4=raw["multiplier_e4"],
            )
        )
    return tuple(
        sorted(
            result,
            key=lambda change: (
                change.series_ticker,
                change.effective_from_ns,
                change.change_id,
            ),
        )
    )


def _parse_event_assertions(
    value: object,
) -> tuple[EventFeeAssertion, ...]:
    result: list[EventFeeAssertion] = []
    for index, item in enumerate(_array("event_assertions", value)):
        raw = _mapping(f"event_assertions[{index}]", item)
        _exact_keys(
            f"event_assertions[{index}]",
            raw,
            (
                "assertion_id",
                "effective_from_ns",
                "effective_until_ns",
                "event_ticker",
                "fee_type_override",
                "multiplier_e4_override",
                "source_sha256",
                "waiver_state",
            ),
        )
        start_ns = require_int(
            f"event_assertions[{index}].effective_from_ns",
            raw["effective_from_ns"],
            minimum=0,
        )
        result.append(
            EventFeeAssertion(
                assertion_id=raw["assertion_id"],
                event_ticker=raw["event_ticker"],
                effective_from_ns=start_ns,
                effective_until_ns=_optional_end_ns(
                    (
                        f"event_assertions[{index}]."
                        "effective_until_ns"
                    ),
                    raw["effective_until_ns"],
                    start_ns=start_ns,
                ),
                waiver_state=raw["waiver_state"],
                fee_type_override=raw["fee_type_override"],
                multiplier_e4_override=raw["multiplier_e4_override"],
                source_sha256=raw["source_sha256"],
            )
        )
    return tuple(
        sorted(
            result,
            key=lambda assertion: (
                assertion.event_ticker,
                assertion.effective_from_ns,
                assertion.assertion_id,
            ),
        )
    )


def load_fee_facts(
    path: Path | str,
    *,
    expected_facts_sha256: str | None = None,
    expected_series_history_snapshot_sha256: str | None = None,
    expected_private_actual_fee_aggregate_receipt_sha256: str | None = None,
) -> FeeFacts:
    """Load one canonical, offline fee-facts document.

    Optional expected hashes bind a research run to authority values held
    outside the facts file.  They are equality checks, never replacements.
    """

    facts_path = Path(path)
    if facts_path.is_symlink():
        raise FeeFactsIntegrityError("fee facts path may not be a symlink")
    try:
        raw_bytes = facts_path.read_bytes()
    except OSError as exc:
        raise FeeFactsIntegrityError(
            f"cannot read fee facts: {exc}"
        ) from exc
    facts_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if expected_facts_sha256 is not None:
        require_sha256("expected_facts_sha256", expected_facts_sha256)
        if facts_sha256 != expected_facts_sha256:
            raise FeeFactsIntegrityError("fee facts file SHA mismatch")

    try:
        payload = json.loads(
            raw_bytes.decode("utf-8"),
            parse_float=_reject_json_float,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_no_duplicate_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeeFactsIntegrityError(
            f"fee facts are not valid UTF-8 JSON: {exc}"
        ) from exc
    root = _mapping("fee facts", payload)
    if canonical_json_bytes(root) != raw_bytes:
        raise FeeFactsIntegrityError(
            "fee facts bytes are not canonical JSON"
        )
    _exact_keys(
        "fee facts",
        root,
        (
            "account",
            "event_assertions",
            "formula",
            "private_actual_fee_aggregate",
            "schema_version",
            "series_changes",
            "source_bindings",
        ),
    )
    if root["schema_version"] != FEE_FACTS_SCHEMA_VERSION:
        raise FeeFactsIntegrityError(
            f"unsupported fee facts schema {root['schema_version']}"
        )

    account = _mapping("account", root["account"])
    _exact_keys(
        "account",
        account,
        ("balance_precision", "source_sha256"),
    )
    try:
        account_precision = FeePrecision(account["balance_precision"])
    except (TypeError, ValueError) as exc:
        raise FeeFactsIntegrityError(
            "account balance precision is unknown"
        ) from exc

    sources = _parse_sources(root["source_bindings"])
    if expected_series_history_snapshot_sha256 is not None:
        require_sha256(
            "expected_series_history_snapshot_sha256",
            expected_series_history_snapshot_sha256,
        )
        if (
            sources.series_history_snapshot_sha256
            != expected_series_history_snapshot_sha256
        ):
            raise FeeFactsIntegrityError(
                "series-history snapshot SHA mismatch"
            )
    if expected_private_actual_fee_aggregate_receipt_sha256 is not None:
        require_sha256(
            "expected_private_actual_fee_aggregate_receipt_sha256",
            expected_private_actual_fee_aggregate_receipt_sha256,
        )
        if (
            sources.private_actual_fee_aggregate_receipt_sha256
            != expected_private_actual_fee_aggregate_receipt_sha256
        ):
            raise FeeFactsIntegrityError(
                "private fee aggregate receipt SHA mismatch"
            )

    return FeeFacts(
        formula=_parse_formula(root["formula"]),
        account_precision=account_precision,
        account_precision_source_sha256=account["source_sha256"],
        sources=sources,
        private_fee_aggregate=_parse_private_aggregate(
            root["private_actual_fee_aggregate"]
        ),
        series_changes=_parse_series_changes(root["series_changes"]),
        event_assertions=_parse_event_assertions(
            root["event_assertions"]
        ),
        facts_sha256=facts_sha256,
    )
