#!/usr/bin/env python3
"""Offline-only, fail-closed dynamic KEEP versus typed FOK exits.

V2.1 treats public order-book levels as aggregate depth, never as true fill
partitions.  It computes a conservative taker-fee upper bound over every
legal private partition, evaluates both ways to remove one-leg inventory,
and permits ``SUBMIT_FOK`` only when at least one route is fully visible.

There is no data reader, fitter, publisher, shadow adapter, action sealer, or
live path in this module.  Model, training, fee, feature-transform, market,
fill, order, and release authority enter only through typed external pins.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import (
    Decimal,
    DecimalException,
    InvalidOperation,
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    localcontext,
)
from functools import lru_cache
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Optional, Tuple


POLICY_VERSION = "ROUND4_DYNAMIC_KEEP_FOK_POLICY_V2_1"
OFFLINE_ONLY = True
CANDIDATE_ALLOWED = False
SHADOW_ALLOWED = False
LIVE_ALLOWED = False
ACTION_SEAL_ALLOWED = False
AUTHENTICATED_ACTUAL_FEE_ALLOWED = False

KEEP = "KEEP"
SUBMIT_FOK = "SUBMIT_FOK"
NO_DECISION = "NO_DECISION"
SELL_FIRST_LEG = "SELL_FIRST_LEG"
BUY_COMPLEMENT = "BUY_COMPLEMENT"

FIRST_LEG_BASIS = "FIRST_LEG_BASIS"
RESTING_COMPLEMENT_RESERVE = "RESTING_COMPLEMENT_RESERVE"
PENDING_FOK_RESERVE = "PENDING_FOK_RESERVE"

FOK_EFFECTIVE_LATENCY_MS = Decimal("60")
PAIR_COST_CEILING = Decimal("0.99")
DELTA_ESTIMAND = "KEEP_MINUS_BEST_FULL_FILL_FOK_NET_USD"
LCB_DEFINITION = "MEAN_MINUS_NONNEGATIVE_UNCERTAINTY_RADIUS"
CONSERVATIVE_PUBLIC_FEE_RULE = (
    "PUBLIC_AGGREGATE_WORST_LEGAL_PARTITION_"
    "TRADE_CENTICENT_PLUS_POSITION_COST_RESIDUAL"
)
LOG_ODDS_FEATURE_MODULE_SHA256 = (
    "d2911932dd061fa5a6592f510eb8476cf"
    "b6d2f4ab9616b9ed2e75552f849bb26"
)

ZERO = Decimal("0")
ONE = Decimal("1")
MS_TO_NS = Decimal("1000000")
MS_TO_SECONDS = Decimal("1000")
EXPECTED_SERIES = "KXBTC15M"
EXPECTED_TAKER_RATE = Decimal("0.07")
EXPECTED_CENTICENT = Decimal("0.0001")
EXPECTED_MIN_QTY = Decimal("0.01")
MAX_PARTITION_UNITS = 5_000
INTERNAL_DECIMAL_PRECISION = 80
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TICKER_RE = re.compile(
    r"^KXBTC15M-[0-9]{2}"
    r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
    r"[0-9]{6}-15$"
)
EXTERNAL_URI_RE = re.compile(r"^(?:s3|https)://[^/].+")


class PolicyContractError(ValueError):
    """A typed offline input violates the V2.1 contract."""


def _decimal(
    value: object,
    label: str,
    *,
    finite: bool = True,
) -> Decimal:
    if isinstance(value, bool):
        raise PolicyContractError(f"{label} must not be bool")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PolicyContractError(f"{label} must be Decimal-like") from exc
    if finite and not result.is_finite():
        raise PolicyContractError(f"{label} must be finite")
    return result


def _positive(value: object, label: str) -> Decimal:
    result = _decimal(value, label)
    if result <= ZERO:
        raise PolicyContractError(f"{label} must be positive")
    return result


def _nonnegative(value: object, label: str) -> Decimal:
    result = _decimal(value, label)
    if result < ZERO:
        raise PolicyContractError(f"{label} must be non-negative")
    return result


def _price(value: object, label: str) -> Decimal:
    result = _decimal(value, label)
    if not ZERO < result < ONE:
        raise PolicyContractError(f"{label} must lie strictly inside (0,1)")
    return result


def _ns(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PolicyContractError(f"{label} must be a non-negative int")
    return value


def _elapsed_to_ns(value: object, label: str) -> int:
    elapsed = _nonnegative(value, label)
    ns = elapsed * MS_TO_NS
    integral = ns.to_integral_value()
    if ns != integral:
        raise PolicyContractError(
            f"{label} must be exactly representable in nanoseconds"
        )
    return int(integral)


def _multiple(value: Decimal, increment: Decimal) -> bool:
    with localcontext() as context:
        context.prec = INTERNAL_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        return value % increment == ZERO


def _ceil_quantum(value: Decimal, quantum: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = INTERNAL_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        if value == ZERO:
            return ZERO
        units = (value / quantum).to_integral_value(
            rounding=ROUND_CEILING
        )
        return units * quantum


@dataclass(frozen=True)
class SealedPin:
    """Typed reference to immutable evidence outside this policy module."""

    authority: str
    artifact_kind: str
    sha256: str
    semantic_payload_sha256: str
    uri: str
    version_id: str
    size_bytes: int

    def __post_init__(self) -> None:
        if self.authority != "EXTERNAL_SEALED":
            raise PolicyContractError("pin authority must be EXTERNAL_SEALED")
        if not isinstance(self.artifact_kind, str) or not self.artifact_kind:
            raise PolicyContractError("pin artifact_kind is required")
        if (
            not isinstance(self.sha256, str)
            or SHA256_RE.fullmatch(self.sha256) is None
        ):
            raise PolicyContractError("pin sha256 is invalid")
        if (
            not isinstance(self.semantic_payload_sha256, str)
            or SHA256_RE.fullmatch(self.semantic_payload_sha256) is None
        ):
            raise PolicyContractError(
                "pin semantic_payload_sha256 is invalid"
            )
        if (
            not isinstance(self.uri, str)
            or EXTERNAL_URI_RE.fullmatch(self.uri) is None
        ):
            raise PolicyContractError("pin uri must be external s3/https")
        if not isinstance(self.version_id, str) or not self.version_id:
            raise PolicyContractError("pin version_id is required")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes <= 0
        ):
            raise PolicyContractError("pin size_bytes must be positive")


def _require_pin(pin: object, artifact_kind: str, label: str) -> SealedPin:
    if not isinstance(pin, SealedPin):
        raise PolicyContractError(f"{label} must be SealedPin")
    if pin.artifact_kind != artifact_kind:
        raise PolicyContractError(
            f"{label} artifact kind must be {artifact_kind}"
        )
    return pin


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise PolicyContractError(
                "semantic payload Decimal must be finite"
            )
        if value == ZERO:
            return {
                "__decimal_tuple__": {
                    "sign": 0,
                    "digits": "0",
                    "exponent": 0,
                }
            }
        decimal_tuple = value.as_tuple()
        digits = list(decimal_tuple.digits)
        exponent = decimal_tuple.exponent
        while len(digits) > 1 and digits[-1] == 0:
            digits.pop()
            exponent += 1
        return {
            "__decimal_tuple__": {
                "sign": decimal_tuple.sign,
                "digits": "".join(str(digit) for digit in digits),
                "exponent": exponent,
            }
        }
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise PolicyContractError(
        f"unsupported semantic payload type: {type(value).__name__}"
    )


def semantic_payload_sha256(
    artifact_kind: str,
    payload: Mapping[str, object],
) -> str:
    """Canonical semantic hash that an external artifact pin must bind."""
    if not isinstance(artifact_kind, str) or not artifact_kind:
        raise PolicyContractError("semantic artifact_kind is required")
    if not isinstance(payload, Mapping):
        raise PolicyContractError("semantic payload must be a mapping")
    envelope = {
        "artifact_kind": artifact_kind,
        "payload": _canonical_value(payload),
    }
    raw = json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _pin_ref(pin: SealedPin) -> dict[str, object]:
    return {
        "authority": pin.authority,
        "artifact_kind": pin.artifact_kind,
        "sha256": pin.sha256,
        "semantic_payload_sha256": pin.semantic_payload_sha256,
        "uri": pin.uri,
        "version_id": pin.version_id,
        "size_bytes": pin.size_bytes,
    }


def _require_semantic_pin(
    pin: object,
    artifact_kind: str,
    payload: Mapping[str, object],
    label: str,
) -> SealedPin:
    typed = _require_pin(pin, artifact_kind, label)
    expected = semantic_payload_sha256(artifact_kind, payload)
    if typed.semantic_payload_sha256 != expected:
        raise PolicyContractError(f"{label} semantic payload mismatch")
    return typed


@dataclass(frozen=True)
class PriceRange:
    start: Decimal
    end: Decimal
    step: Decimal

    def __post_init__(self) -> None:
        start = _nonnegative(self.start, "price range start")
        end = _positive(self.end, "price range end")
        step = _positive(self.step, "price range step")
        if not start < end <= ONE:
            raise PolicyContractError("price range bounds are invalid")
        if not _multiple(end - start, step):
            raise PolicyContractError("price range is not step aligned")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "step", step)


EXPECTED_PRICE_RANGES = (
    PriceRange(Decimal("0"), Decimal("0.1"), Decimal("0.001")),
    PriceRange(Decimal("0.1"), Decimal("0.9"), Decimal("0.01")),
    PriceRange(Decimal("0.9"), Decimal("1"), Decimal("0.001")),
)


def market_context_payload(
    *,
    series_ticker: str,
    market_ticker: str,
    price_level_structure: str,
    price_ranges: Tuple[PriceRange, ...],
    min_quantity_increment: Decimal,
) -> dict[str, object]:
    return {
        "series_ticker": series_ticker,
        "market_ticker": market_ticker,
        "price_level_structure": price_level_structure,
        "price_ranges": tuple(
            {
                "start": item.start,
                "end": item.end,
                "step": item.step,
            }
            for item in price_ranges
        ),
        "min_quantity_increment": min_quantity_increment,
    }


@dataclass(frozen=True)
class MarketContext:
    series_ticker: str
    market_ticker: str
    price_level_structure: str
    price_ranges: Tuple[PriceRange, ...]
    min_quantity_increment: Decimal
    context_pin: SealedPin

    def __post_init__(self) -> None:
        if self.series_ticker != EXPECTED_SERIES:
            raise PolicyContractError("market series must be KXBTC15M")
        if (
            not isinstance(self.market_ticker, str)
            or TICKER_RE.fullmatch(self.market_ticker) is None
        ):
            raise PolicyContractError("market ticker is invalid")
        if self.price_level_structure != "tapered_deci_cent":
            raise PolicyContractError("price level structure drifted")
        if (
            not isinstance(self.price_ranges, tuple)
            or self.price_ranges != EXPECTED_PRICE_RANGES
        ):
            raise PolicyContractError("market price_ranges drifted")
        minimum = _positive(
            self.min_quantity_increment, "market minimum quantity"
        )
        if minimum != EXPECTED_MIN_QTY:
            raise PolicyContractError(
                "market minimum quantity must be exactly 0.01"
            )
        _require_semantic_pin(
            self.context_pin,
            "MARKET_CONTEXT",
            market_context_payload(
                series_ticker=self.series_ticker,
                market_ticker=self.market_ticker,
                price_level_structure=self.price_level_structure,
                price_ranges=self.price_ranges,
                min_quantity_increment=minimum,
            ),
            "market context pin",
        )
        object.__setattr__(self, "min_quantity_increment", minimum)


def tick_for_price(price: Decimal, market: MarketContext) -> Decimal:
    p = _price(price, "price")
    if not isinstance(market, MarketContext):
        raise PolicyContractError("market context is required")
    for index, price_range in enumerate(market.price_ranges):
        in_range = (
            price_range.start <= p < price_range.end
            if index < len(market.price_ranges) - 1
            else price_range.start <= p <= price_range.end
        )
        if in_range:
            if not _multiple(p - price_range.start, price_range.step):
                raise PolicyContractError(
                    f"price {p} is not on a legal tick"
                )
            return price_range.step
    raise PolicyContractError("price is outside declared price_ranges")


def _legal_quantity(quantity: object, market: MarketContext, label: str) -> Decimal:
    q = _positive(quantity, label)
    if not _multiple(q, market.min_quantity_increment):
        raise PolicyContractError(
            f"{label} is not a multiple of market minimum quantity"
        )
    return q


def fee_context_payload(
    *,
    series_ticker: str,
    taker_rate: Decimal,
    centicent_quantum_usd: Decimal,
    rounding_rule: str,
    authenticated_actual_fee_available: bool,
) -> dict[str, object]:
    return {
        "series_ticker": series_ticker,
        "taker_rate": taker_rate,
        "centicent_quantum_usd": centicent_quantum_usd,
        "rounding_rule": rounding_rule,
        "authenticated_actual_fee_available": (
            authenticated_actual_fee_available
        ),
    }


@dataclass(frozen=True)
class FeeContext:
    series_ticker: str
    taker_rate: Decimal
    centicent_quantum_usd: Decimal
    rounding_rule: str
    authenticated_actual_fee_available: bool
    fee_schedule_pin: SealedPin

    def __post_init__(self) -> None:
        if self.series_ticker != EXPECTED_SERIES:
            raise PolicyContractError("fee series must be KXBTC15M")
        rate = _nonnegative(self.taker_rate, "taker rate")
        quantum = _positive(
            self.centicent_quantum_usd, "centicent quantum"
        )
        if rate != EXPECTED_TAKER_RATE:
            raise PolicyContractError("taker rate drifted")
        if quantum != EXPECTED_CENTICENT:
            raise PolicyContractError("centicent quantum drifted")
        if self.rounding_rule != CONSERVATIVE_PUBLIC_FEE_RULE:
            raise PolicyContractError("public fee rule drifted")
        if self.authenticated_actual_fee_available is not False:
            raise PolicyContractError(
                "authenticated actual fee path is not authorized"
            )
        _require_semantic_pin(
            self.fee_schedule_pin,
            "FEE_SCHEDULE",
            fee_context_payload(
                series_ticker=self.series_ticker,
                taker_rate=rate,
                centicent_quantum_usd=quantum,
                rounding_rule=self.rounding_rule,
                authenticated_actual_fee_available=(
                    self.authenticated_actual_fee_available
                ),
            ),
            "fee schedule pin",
        )
        object.__setattr__(self, "taker_rate", rate)
        object.__setattr__(self, "centicent_quantum_usd", quantum)


def first_fill_payload(
    *,
    market_ticker: str,
    outcome_side: str,
    action: str,
    price: Decimal,
    quantity: Decimal,
    maker_fee_usd: Decimal,
    recv_wall_ns: int,
    recv_mono_ns: int,
) -> dict[str, object]:
    return {
        "market_ticker": market_ticker,
        "outcome_side": outcome_side,
        "action": action,
        "price": price,
        "quantity": quantity,
        "maker_fee_usd": maker_fee_usd,
        "recv_wall_ns": recv_wall_ns,
        "recv_mono_ns": recv_mono_ns,
    }


@dataclass(frozen=True)
class FirstFill:
    market_ticker: str
    outcome_side: str
    action: str
    price: Decimal
    quantity: Decimal
    maker_fee_usd: Decimal
    recv_wall_ns: int
    recv_mono_ns: int
    source_pin: SealedPin

    def __post_init__(self) -> None:
        if self.outcome_side not in ("YES", "NO"):
            raise PolicyContractError("first fill outcome must be YES/NO")
        if self.action != "BUY":
            raise PolicyContractError("first fill action must be BUY")
        object.__setattr__(self, "price", _price(self.price, "first price"))
        object.__setattr__(
            self, "quantity", _positive(self.quantity, "first quantity")
        )
        object.__setattr__(
            self,
            "maker_fee_usd",
            _nonnegative(self.maker_fee_usd, "first maker fee"),
        )
        _ns(self.recv_wall_ns, "first recv wall")
        _ns(self.recv_mono_ns, "first recv mono")
        _require_semantic_pin(
            self.source_pin,
            "FIRST_FILL_RECEIPT",
            first_fill_payload(
                market_ticker=self.market_ticker,
                outcome_side=self.outcome_side,
                action=self.action,
                price=self.price,
                quantity=self.quantity,
                maker_fee_usd=self.maker_fee_usd,
                recv_wall_ns=self.recv_wall_ns,
                recv_mono_ns=self.recv_mono_ns,
            ),
            "first fill pin",
        )


def resting_complement_payload(
    *,
    market_ticker: str,
    outcome_side: str,
    action: str,
    price: Decimal,
    quantity: Decimal,
    maker_fee_usd: Decimal,
) -> dict[str, object]:
    return {
        "market_ticker": market_ticker,
        "outcome_side": outcome_side,
        "action": action,
        "price": price,
        "quantity": quantity,
        "maker_fee_usd": maker_fee_usd,
    }


@dataclass(frozen=True)
class RestingComplement:
    market_ticker: str
    outcome_side: str
    action: str
    price: Decimal
    quantity: Decimal
    maker_fee_usd: Decimal
    source_pin: SealedPin

    def __post_init__(self) -> None:
        if self.outcome_side not in ("YES", "NO"):
            raise PolicyContractError("resting outcome must be YES/NO")
        if self.action != "BUY":
            raise PolicyContractError("resting action must be BUY")
        object.__setattr__(
            self, "price", _price(self.price, "resting price")
        )
        object.__setattr__(
            self, "quantity", _positive(self.quantity, "resting quantity")
        )
        object.__setattr__(
            self,
            "maker_fee_usd",
            _nonnegative(self.maker_fee_usd, "resting maker fee"),
        )
        _require_semantic_pin(
            self.source_pin,
            "RESTING_ORDER_RECEIPT",
            resting_complement_payload(
                market_ticker=self.market_ticker,
                outcome_side=self.outcome_side,
                action=self.action,
                price=self.price,
                quantity=self.quantity,
                maker_fee_usd=self.maker_fee_usd,
            ),
            "resting order pin",
        )


@dataclass(frozen=True)
class BookLevel:
    """One public aggregate level, not a private execution slice."""

    market_ticker: str
    outcome_side: str
    action: str
    price: Decimal
    aggregate_quantity: Decimal
    tick_size: Decimal
    recv_wall_ns: int
    recv_mono_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.market_ticker, str) or not self.market_ticker:
            raise PolicyContractError("book ticker is required")
        if self.outcome_side not in ("YES", "NO"):
            raise PolicyContractError("book outcome must be YES/NO")
        if self.action not in ("BUY", "SELL"):
            raise PolicyContractError("book action must be BUY/SELL")
        object.__setattr__(self, "price", _price(self.price, "book price"))
        object.__setattr__(
            self,
            "aggregate_quantity",
            _positive(self.aggregate_quantity, "book aggregate quantity"),
        )
        object.__setattr__(
            self, "tick_size", _positive(self.tick_size, "book tick size")
        )
        _ns(self.recv_wall_ns, "book recv wall")
        _ns(self.recv_mono_ns, "book recv mono")


@dataclass(frozen=True)
class LogOddsFeatures:
    mid_logodds: Optional[Decimal]
    spread_logodds: Optional[Decimal]
    move_logodds_1s: Optional[Decimal]
    move_logodds_10s: Optional[Decimal]
    quote_skew_logodds: Optional[Decimal]
    queue_ahead_qty: Optional[Decimal]
    better_depth_qty: Optional[Decimal]
    complement_flow_5s_qty: Optional[Decimal]
    transform_pin: SealedPin

    def __post_init__(self) -> None:
        pin = _require_semantic_pin(
            self.transform_pin,
            "LOG_ODDS_FEATURE_CODE",
            {
                "module_sha256": LOG_ODDS_FEATURE_MODULE_SHA256,
                "feature_names": (
                    "mid_logodds",
                    "spread_logodds",
                    "move_logodds_1s",
                    "move_logodds_10s",
                    "quote_skew_logodds",
                ),
            },
            "log-odds transform pin",
        )
        if pin.sha256 != LOG_ODDS_FEATURE_MODULE_SHA256:
            raise PolicyContractError("log-odds transform hash drifted")


LOG_ODDS_FEATURE_NAMES = (
    "mid_logodds",
    "spread_logodds",
    "move_logodds_1s",
    "move_logodds_10s",
    "quote_skew_logodds",
)
CAUSAL_QUANTITY_FEATURE_NAMES = (
    "queue_ahead_qty",
    "better_depth_qty",
    "complement_flow_5s_qty",
)
DERIVED_MODEL_FEATURE_NAMES = (
    "elapsed_ms",
    "first_price",
    "quantity",
    "pair_gain_usd",
    "selected_exit_net_safe_lower_usd",
    "selected_exit_fee_safe_upper_usd",
    "selected_exit_is_sell_first",
)
ALLOWED_MODEL_FEATURES = frozenset(
    LOG_ODDS_FEATURE_NAMES
    + CAUSAL_QUANTITY_FEATURE_NAMES
    + DERIVED_MODEL_FEATURE_NAMES
)


@dataclass(frozen=True)
class DecisionState:
    event_id: str
    market: MarketContext
    fee_context: FeeContext
    first_fill: FirstFill
    resting_complement: RestingComplement
    sell_first_leg_levels: Tuple[BookLevel, ...]
    buy_complement_levels: Tuple[BookLevel, ...]
    features: LogOddsFeatures
    decision_elapsed_ms: Decimal
    decision_recv_wall_ns: int
    decision_recv_mono_ns: int
    feature_asof_wall_ns: int
    feature_asof_mono_ns: int
    source_max_recv_wall_ns: int
    source_max_recv_mono_ns: int
    fok_terminal_elapsed_ms: Decimal
    fok_terminal_wall_ns: int
    fok_terminal_mono_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id:
            raise PolicyContractError("event_id is required")
        if not isinstance(self.market, MarketContext):
            raise PolicyContractError("market context is required")
        if not isinstance(self.fee_context, FeeContext):
            raise PolicyContractError("fee context is required")
        if self.fee_context.series_ticker != self.market.series_ticker:
            raise PolicyContractError("market/fee series mismatch")
        if not isinstance(self.first_fill, FirstFill):
            raise PolicyContractError("first_fill is required")
        if not isinstance(self.resting_complement, RestingComplement):
            raise PolicyContractError("resting_complement is required")
        ticker = self.market.market_ticker
        if self.first_fill.market_ticker != ticker:
            raise PolicyContractError("first fill ticker mismatch")
        if self.resting_complement.market_ticker != ticker:
            raise PolicyContractError("resting order ticker mismatch")
        opposite = "NO" if self.first_fill.outcome_side == "YES" else "YES"
        if self.resting_complement.outcome_side != opposite:
            raise PolicyContractError("resting outcome is not complementary")
        first_qty = _legal_quantity(
            self.first_fill.quantity, self.market, "first quantity"
        )
        resting_qty = _legal_quantity(
            self.resting_complement.quantity,
            self.market,
            "resting quantity",
        )
        if first_qty != resting_qty:
            raise PolicyContractError("resting quantity must match first fill")
        first_tick = tick_for_price(self.first_fill.price, self.market)
        resting_tick = tick_for_price(
            self.resting_complement.price, self.market
        )
        if first_tick <= ZERO or resting_tick <= ZERO:
            raise PolicyContractError("entry prices lack legal ticks")
        if (
            self.first_fill.price + self.resting_complement.price
            > PAIR_COST_CEILING
        ):
            raise PolicyContractError("resting pair exceeds 0.99 ceiling")
        if not isinstance(self.features, LogOddsFeatures):
            raise PolicyContractError("log-odds features are required")

        first_wall = self.first_fill.recv_wall_ns
        first_mono = self.first_fill.recv_mono_ns
        elapsed = _nonnegative(
            self.decision_elapsed_ms, "decision elapsed"
        )
        elapsed_ns = _elapsed_to_ns(elapsed, "decision elapsed")
        decision_wall = _ns(
            self.decision_recv_wall_ns, "decision recv wall"
        )
        decision_mono = _ns(
            self.decision_recv_mono_ns, "decision recv mono"
        )
        if (
            decision_wall != first_wall + elapsed_ns
            or decision_mono != first_mono + elapsed_ns
        ):
            raise PolicyContractError(
                "decision clocks must equal first fill + elapsed"
            )
        asof_wall = _ns(self.feature_asof_wall_ns, "feature asof wall")
        asof_mono = _ns(self.feature_asof_mono_ns, "feature asof mono")
        if asof_wall != decision_wall or asof_mono != decision_mono:
            raise PolicyContractError("feature asof must equal decision")
        source_wall = _ns(
            self.source_max_recv_wall_ns, "source max wall"
        )
        source_mono = _ns(
            self.source_max_recv_mono_ns, "source max mono"
        )
        if source_wall > asof_wall or source_mono > asof_mono:
            raise PolicyContractError("source must not exceed feature asof")

        self._validate_levels(
            self.sell_first_leg_levels,
            route_type=SELL_FIRST_LEG,
            expected_outcome=self.first_fill.outcome_side,
            expected_action="SELL",
            ascending=False,
            source_wall=source_wall,
            source_mono=source_mono,
        )
        self._validate_levels(
            self.buy_complement_levels,
            route_type=BUY_COMPLEMENT,
            expected_outcome=opposite,
            expected_action="BUY",
            ascending=True,
            source_wall=source_wall,
            source_mono=source_mono,
        )

        terminal_elapsed = _nonnegative(
            self.fok_terminal_elapsed_ms, "FOK terminal elapsed"
        )
        if (
            terminal_elapsed != elapsed + FOK_EFFECTIVE_LATENCY_MS
            or _ns(self.fok_terminal_wall_ns, "FOK terminal wall")
            != decision_wall + 60_000_000
            or _ns(self.fok_terminal_mono_ns, "FOK terminal mono")
            != decision_mono + 60_000_000
        ):
            raise PolicyContractError(
                "FOK terminal must be exactly decision + 60ms"
            )
        object.__setattr__(self, "decision_elapsed_ms", elapsed)
        object.__setattr__(
            self, "fok_terminal_elapsed_ms", terminal_elapsed
        )

    def _validate_levels(
        self,
        levels: object,
        *,
        route_type: str,
        expected_outcome: str,
        expected_action: str,
        ascending: bool,
        source_wall: int,
        source_mono: int,
    ) -> None:
        if not isinstance(levels, tuple):
            raise PolicyContractError(f"{route_type} levels must be tuple")
        previous: Optional[Decimal] = None
        for index, level in enumerate(levels):
            if not isinstance(level, BookLevel):
                raise PolicyContractError(
                    f"{route_type} level {index} must be BookLevel"
                )
            if level.market_ticker != self.market.market_ticker:
                raise PolicyContractError(f"{route_type} ticker mismatch")
            if level.outcome_side != expected_outcome:
                raise PolicyContractError(f"{route_type} outcome mismatch")
            if level.action != expected_action:
                raise PolicyContractError(f"{route_type} action mismatch")
            if (
                level.recv_wall_ns != source_wall
                or level.recv_mono_ns != source_mono
            ):
                raise PolicyContractError(
                    f"{route_type} source clocks do not bind source max"
                )
            expected_tick = tick_for_price(level.price, self.market)
            if level.tick_size != expected_tick:
                raise PolicyContractError(
                    f"{route_type} tick_size does not match legal tick"
                )
            _legal_quantity(
                level.aggregate_quantity,
                self.market,
                f"{route_type} aggregate quantity",
            )
            if previous is not None:
                if ascending and level.price <= previous:
                    raise PolicyContractError(
                        f"{route_type} levels must strictly ascend"
                    )
                if not ascending and level.price >= previous:
                    raise PolicyContractError(
                        f"{route_type} levels must strictly descend"
                    )
            previous = level.price


@dataclass(frozen=True)
class FeeUpperBound:
    action: str
    aggregate_price: Decimal
    aggregate_quantity: Decimal
    minimum_quantity_increment: Decimal
    worst_partition_quantities: Tuple[Decimal, ...]
    trade_fee_centicent_ceil_sum_usd: Decimal
    position_cost_fee_residual_sum_usd: Decimal
    taker_fee_safe_upper_usd: Decimal
    provenance: str


def _fee_partition_dp(
    action: str,
    price: Decimal,
    quantity: Decimal,
    minimum: Decimal,
    rate: Decimal,
    quantum: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Tuple[Decimal, ...]]:
    with localcontext() as context:
        context.prec = INTERNAL_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        return _fee_partition_dp_inner(
            action,
            price,
            quantity,
            minimum,
            rate,
            quantum,
        )


_fee_partition_dp = lru_cache(maxsize=2_048)(_fee_partition_dp)


def _fee_partition_dp_inner(
    action: str,
    price: Decimal,
    quantity: Decimal,
    minimum: Decimal,
    rate: Decimal,
    quantum: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Tuple[Decimal, ...]]:
    if action not in ("BUY", "SELL"):
        raise PolicyContractError("fee action must be BUY/SELL")
    units_decimal = quantity / minimum
    if units_decimal != units_decimal.to_integral_value():
        raise PolicyContractError("aggregate quantity is not legal")
    units = int(units_decimal)
    if units <= 0 or units > MAX_PARTITION_UNITS:
        raise PolicyContractError("aggregate partition unit count unsupported")
    fill_total = [ZERO] * (units + 1)
    fill_trade = [ZERO] * (units + 1)
    fill_residual = [ZERO] * (units + 1)
    for count in range(1, units + 1):
        fill_qty = minimum * Decimal(count)
        position_cost = price * fill_qty
        raw_fee = rate * fill_qty * price * (ONE - price)
        trade_fee = _ceil_quantum(raw_fee, quantum)
        signed_cash_after_trade_fee = (
            -position_cost - trade_fee
            if action == "BUY"
            else position_cost - trade_fee
        )
        floor_units = (
            signed_cash_after_trade_fee / quantum
        ).to_integral_value(rounding=ROUND_FLOOR)
        residual = (
            signed_cash_after_trade_fee - quantum * floor_units
        )
        fill_trade[count] = trade_fee
        fill_residual[count] = residual
        fill_total[count] = trade_fee + residual

    negative = Decimal("-Infinity")
    best = [ZERO] + [negative] * units
    choice = [0] * (units + 1)
    for consumed in range(1, units + 1):
        best_value = negative
        best_size = 0
        for size in range(1, consumed + 1):
            candidate = best[consumed - size] + fill_total[size]
            if candidate > best_value:
                best_value = candidate
                best_size = size
        best[consumed] = best_value
        choice[consumed] = best_size

    parts = []
    trade_sum = ZERO
    residual_sum = ZERO
    remaining = units
    while remaining:
        size = choice[remaining]
        if size <= 0:
            raise PolicyContractError("fee partition DP failed closed")
        parts.append(minimum * Decimal(size))
        trade_sum += fill_trade[size]
        residual_sum += fill_residual[size]
        remaining -= size
    total = trade_sum + residual_sum
    if total != best[units]:
        raise PolicyContractError("fee partition reconstruction mismatch")
    return total, trade_sum, residual_sum, tuple(parts)


def aggregate_taker_fee_safe_upper(
    *,
    action: str,
    price: Decimal,
    aggregate_quantity: Decimal,
    market: MarketContext,
    fee_context: FeeContext,
) -> FeeUpperBound:
    """Worst fee over every legal private partition of one public level."""
    if not isinstance(market, MarketContext):
        raise PolicyContractError("market context is required")
    if not isinstance(fee_context, FeeContext):
        raise PolicyContractError("fee context is required")
    if market.series_ticker != fee_context.series_ticker:
        raise PolicyContractError("market/fee context mismatch")
    if action not in ("BUY", "SELL"):
        raise PolicyContractError("aggregate fee action must be BUY/SELL")
    p = _price(price, "aggregate price")
    tick_for_price(p, market)
    q = _legal_quantity(
        aggregate_quantity, market, "aggregate quantity"
    )
    total, trade, residual, parts = _fee_partition_dp(
        action,
        p,
        q,
        market.min_quantity_increment,
        fee_context.taker_rate,
        fee_context.centicent_quantum_usd,
    )
    return FeeUpperBound(
        action=action,
        aggregate_price=p,
        aggregate_quantity=q,
        minimum_quantity_increment=market.min_quantity_increment,
        worst_partition_quantities=parts,
        trade_fee_centicent_ceil_sum_usd=trade,
        position_cost_fee_residual_sum_usd=residual,
        taker_fee_safe_upper_usd=total,
        provenance=CONSERVATIVE_PUBLIC_FEE_RULE,
    )


@dataclass(frozen=True)
class ExitExecution:
    aggregate_level: BookLevel
    quantity: Decimal
    position_cash_usd: Decimal
    fee_bound: FeeUpperBound
    source_nature: str
    private_partition_unknown: bool


@dataclass(frozen=True)
class ExitRoute:
    route_type: str
    requested_quantity: Decimal
    executable_quantity: Decimal
    residual_quantity: Decimal
    full_fill: bool
    executions: Tuple[ExitExecution, ...]
    taker_fee_safe_upper_usd: Decimal
    gross_pnl_usd: Optional[Decimal]
    net_pnl_safe_lower_usd: Optional[Decimal]
    pending_fok_reserve_usd: Optional[Decimal]
    execution_nature: str
    actual_fill_claimed: bool
    terminal_elapsed_ms: Decimal
    terminal_wall_ns: int
    terminal_mono_ns: int


@dataclass(frozen=True)
class ExitRoutes:
    sell_first_leg: ExitRoute
    buy_complement: ExitRoute
    selected: Optional[ExitRoute]


def _route_levels(
    state: DecisionState,
    route_type: str,
) -> Tuple[BookLevel, ...]:
    if route_type == SELL_FIRST_LEG:
        return state.sell_first_leg_levels
    if route_type == BUY_COMPLEMENT:
        return state.buy_complement_levels
    raise PolicyContractError("unknown exit route type")


def evaluate_exit_route(
    state: DecisionState,
    route_type: str,
) -> ExitRoute:
    """Walk one typed public route and conservatively derive economics."""
    with localcontext() as context:
        context.prec = INTERNAL_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        return _evaluate_exit_route_inner(state, route_type)


def _evaluate_exit_route_inner(
    state: DecisionState,
    route_type: str,
) -> ExitRoute:
    if not isinstance(state, DecisionState):
        raise PolicyContractError("DecisionState is required")
    levels = _route_levels(state, route_type)
    remaining = state.first_fill.quantity
    executions = []
    for level in levels:
        if remaining <= ZERO:
            break
        quantity = min(level.aggregate_quantity, remaining)
        bound = aggregate_taker_fee_safe_upper(
            action=level.action,
            price=level.price,
            aggregate_quantity=quantity,
            market=state.market,
            fee_context=state.fee_context,
        )
        executions.append(
            ExitExecution(
                aggregate_level=level,
                quantity=quantity,
                position_cash_usd=level.price * quantity,
                fee_bound=bound,
                source_nature="PUBLIC_AGGREGATE_LEVEL",
                private_partition_unknown=True,
            )
        )
        remaining -= quantity
    executable = state.first_fill.quantity - remaining
    full = remaining == ZERO
    fee_upper = sum(
        (
            execution.fee_bound.taker_fee_safe_upper_usd
            for execution in executions
        ),
        ZERO,
    )
    first_basis = (
        state.first_fill.price * state.first_fill.quantity
        + state.first_fill.maker_fee_usd
    )
    gross: Optional[Decimal]
    net: Optional[Decimal]
    pending: Optional[Decimal]
    if not full:
        gross = None
        net = None
        pending = None
    elif route_type == SELL_FIRST_LEG:
        proceeds = sum(
            (execution.position_cash_usd for execution in executions),
            ZERO,
        )
        gross = proceeds - state.first_fill.price * state.first_fill.quantity
        net = proceeds - first_basis - fee_upper
        pending = fee_upper
    else:
        purchase_cost = sum(
            (execution.position_cash_usd for execution in executions),
            ZERO,
        )
        gross = (
            state.first_fill.quantity
            - state.first_fill.price * state.first_fill.quantity
            - purchase_cost
        )
        net = state.first_fill.quantity - first_basis - purchase_cost - fee_upper
        pending = purchase_cost + fee_upper
    return ExitRoute(
        route_type=route_type,
        requested_quantity=state.first_fill.quantity,
        executable_quantity=executable,
        residual_quantity=remaining,
        full_fill=full,
        executions=tuple(executions),
        taker_fee_safe_upper_usd=fee_upper,
        gross_pnl_usd=gross,
        net_pnl_safe_lower_usd=net,
        pending_fok_reserve_usd=pending,
        execution_nature="COUNTERFACTUAL_VISIBLE_DEPTH_WALK",
        actual_fill_claimed=False,
        terminal_elapsed_ms=state.fok_terminal_elapsed_ms,
        terminal_wall_ns=state.fok_terminal_wall_ns,
        terminal_mono_ns=state.fok_terminal_mono_ns,
    )


def evaluate_exit_routes(state: DecisionState) -> ExitRoutes:
    sell = evaluate_exit_route(state, SELL_FIRST_LEG)
    buy = evaluate_exit_route(state, BUY_COMPLEMENT)
    full_routes = tuple(route for route in (sell, buy) if route.full_fill)
    selected: Optional[ExitRoute]
    if not full_routes:
        selected = None
    else:
        # Stable tie break: SELL_FIRST_LEG is first.
        selected = max(
            full_routes,
            key=lambda route: route.net_pnl_safe_lower_usd,
        )
    return ExitRoutes(
        sell_first_leg=sell,
        buy_complement=buy,
        selected=selected,
    )


@dataclass(frozen=True)
class ModelFeature:
    name: str
    coefficient: Decimal
    support_lower: Decimal
    support_upper: Decimal


def model_artifact_payload(
    *,
    model_id: str,
    training_role: str,
    delta_estimand: str,
    lcb_definition: str,
    intercept_mean_delta_usd: Decimal,
    uncertainty_radius_usd: Decimal,
    features: Tuple[ModelFeature, ...],
    training_data_pin: SealedPin,
    log_odds_transform_pin: SealedPin,
) -> dict[str, object]:
    return {
        "model_id": model_id,
        "training_role": training_role,
        "delta_estimand": delta_estimand,
        "lcb_definition": lcb_definition,
        "intercept_mean_delta_usd": intercept_mean_delta_usd,
        "uncertainty_radius_usd": uncertainty_radius_usd,
        "features": tuple(
            {
                "name": feature.name,
                "coefficient": feature.coefficient,
                "support_lower": feature.support_lower,
                "support_upper": feature.support_upper,
            }
            for feature in features
        ),
        "training_data_pin": _pin_ref(training_data_pin),
        "log_odds_transform_pin": _pin_ref(log_odds_transform_pin),
    }


@dataclass(frozen=True)
class TrainOnlyLCBModel:
    model_id: str
    training_role: str
    delta_estimand: str
    lcb_definition: str
    intercept_mean_delta_usd: Decimal
    uncertainty_radius_usd: Decimal
    features: Tuple[ModelFeature, ...]
    training_data_pin: SealedPin
    model_artifact_pin: SealedPin
    log_odds_transform_pin: SealedPin


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    reason: str
    event_id: str
    selected_exit: Optional[ExitRoute]
    mean_delta_usd: Optional[Decimal]
    lcb_delta_usd: Optional[Decimal]
    buffer_usd: Optional[Decimal]
    model_id: Optional[str]


def _model_error(
    model: object,
    features: LogOddsFeatures,
) -> Optional[str]:
    if not isinstance(model, TrainOnlyLCBModel):
        return "MISSING_OR_INVALID_MODEL"
    if not isinstance(model.model_id, str) or not model.model_id:
        return "INVALID_MODEL_ID"
    if model.training_role != "TRAIN_ONLY":
        return "MODEL_NOT_TRAIN_ONLY"
    if model.delta_estimand != DELTA_ESTIMAND:
        return "INVALID_DELTA_ESTIMAND"
    if model.lcb_definition != LCB_DEFINITION:
        return "INVALID_LCB_DEFINITION"
    try:
        training_pin = _require_pin(
            model.training_data_pin,
            "TRAINING_DATASET",
            "training data pin",
        )
        model_pin = _require_pin(
            model.model_artifact_pin,
            "TRAIN_ONLY_LCB_MODEL",
            "model artifact pin",
        )
        transform_pin = _require_pin(
            model.log_odds_transform_pin,
            "LOG_ODDS_FEATURE_CODE",
            "model log-odds pin",
        )
    except PolicyContractError:
        return "INVALID_EXTERNAL_MODEL_PINS"
    if (
        transform_pin.sha256 != LOG_ODDS_FEATURE_MODULE_SHA256
        or transform_pin.sha256 != features.transform_pin.sha256
    ):
        return "LOG_ODDS_TRANSFORM_PIN_MISMATCH"
    if training_pin.sha256 == model_pin.sha256:
        return "MODEL_AND_TRAINING_PINS_NOT_DISTINCT"
    try:
        _decimal(model.intercept_mean_delta_usd, "model intercept")
        _nonnegative(model.uncertainty_radius_usd, "model uncertainty")
    except PolicyContractError:
        return "NONFINITE_OR_INVALID_MODEL"
    if not isinstance(model.features, tuple) or not model.features:
        return "MISSING_MODEL_FEATURES"
    seen = set()
    for feature in model.features:
        if not isinstance(feature, ModelFeature):
            return "INVALID_MODEL_FEATURE"
        if feature.name in seen:
            return f"DUPLICATE_MODEL_FEATURE:{feature.name}"
        seen.add(feature.name)
        if feature.name not in ALLOWED_MODEL_FEATURES:
            return f"UNSUPPORTED_MODEL_FEATURE:{feature.name}"
        try:
            _decimal(feature.coefficient, "model coefficient")
            lower = _decimal(feature.support_lower, "support lower")
            upper = _decimal(feature.support_upper, "support upper")
        except PolicyContractError:
            return f"NONFINITE_MODEL_FEATURE:{feature.name}"
        if lower > upper:
            return f"INVALID_FEATURE_SUPPORT:{feature.name}"
    try:
        _require_semantic_pin(
            model_pin,
            "TRAIN_ONLY_LCB_MODEL",
            model_artifact_payload(
                model_id=model.model_id,
                training_role=model.training_role,
                delta_estimand=model.delta_estimand,
                lcb_definition=model.lcb_definition,
                intercept_mean_delta_usd=(
                    model.intercept_mean_delta_usd
                ),
                uncertainty_radius_usd=model.uncertainty_radius_usd,
                features=model.features,
                training_data_pin=training_pin,
                log_odds_transform_pin=transform_pin,
            ),
            "model artifact pin",
        )
    except PolicyContractError:
        return "MODEL_SEMANTIC_PIN_MISMATCH"
    return None


def _feature_values(
    state: DecisionState,
    selected: ExitRoute,
) -> dict[str, Optional[Decimal]]:
    values: dict[str, Optional[Decimal]] = {}
    for field in fields(LogOddsFeatures):
        if field.name == "transform_pin":
            continue
        value = getattr(state.features, field.name)
        if value is None:
            values[field.name] = None
        else:
            try:
                values[field.name] = _decimal(
                    value, field.name, finite=False
                )
            except PolicyContractError:
                values[field.name] = None
    pair_gain = (
        ONE
        - state.first_fill.price
        - state.resting_complement.price
    ) * state.first_fill.quantity - (
        state.first_fill.maker_fee_usd
        + state.resting_complement.maker_fee_usd
    )
    values.update(
        {
            "elapsed_ms": state.decision_elapsed_ms,
            "first_price": state.first_fill.price,
            "quantity": state.first_fill.quantity,
            "pair_gain_usd": pair_gain,
            "selected_exit_net_safe_lower_usd": (
                selected.net_pnl_safe_lower_usd
            ),
            "selected_exit_fee_safe_upper_usd": (
                selected.taker_fee_safe_upper_usd
            ),
            "selected_exit_is_sell_first": (
                ONE if selected.route_type == SELL_FIRST_LEG else ZERO
            ),
        }
    )
    return values


def _no_decision(
    state: DecisionState,
    reason: str,
    *,
    selected: Optional[ExitRoute] = None,
    model: object = None,
    buffer: Optional[Decimal] = None,
) -> PolicyDecision:
    model_id = (
        model.model_id
        if isinstance(model, TrainOnlyLCBModel)
        and isinstance(model.model_id, str)
        else None
    )
    return PolicyDecision(
        action=NO_DECISION,
        reason=reason,
        event_id=state.event_id,
        selected_exit=selected,
        mean_delta_usd=None,
        lcb_delta_usd=None,
        buffer_usd=buffer,
        model_id=model_id,
    )


def choose_action(
    state: DecisionState,
    model: Optional[TrainOnlyLCBModel],
    *,
    buffer_usd: Decimal,
) -> PolicyDecision:
    """KEEP only when the train-only LCB strictly clears the buffer."""
    with localcontext() as context:
        context.prec = INTERNAL_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        return _choose_action_inner(
            state,
            model,
            buffer_usd=buffer_usd,
        )


def _choose_action_inner(
    state: DecisionState,
    model: Optional[TrainOnlyLCBModel],
    *,
    buffer_usd: Decimal,
) -> PolicyDecision:
    try:
        routes = evaluate_exit_routes(state)
    except (PolicyContractError, DecimalException):
        return _no_decision(state, "INVALID_EXIT_ECONOMICS", model=model)
    selected = routes.selected
    if selected is None:
        return _no_decision(state, "NO_FULL_FILL_EXIT_ROUTE", model=model)
    try:
        buffer = _nonnegative(buffer_usd, "buffer")
    except PolicyContractError:
        return _no_decision(
            state, "INVALID_BUFFER", selected=selected, model=model
        )
    error = _model_error(model, state.features)
    if error is not None:
        return _no_decision(
            state,
            error,
            selected=selected,
            model=model,
            buffer=buffer,
        )
    assert isinstance(model, TrainOnlyLCBModel)
    values = _feature_values(state, selected)
    try:
        mean = _decimal(
            model.intercept_mean_delta_usd, "model intercept"
        )
        for feature in model.features:
            value = values.get(feature.name)
            if value is None:
                return _no_decision(
                    state,
                    f"MISSING_FEATURE:{feature.name}",
                    selected=selected,
                    model=model,
                    buffer=buffer,
                )
            if not value.is_finite():
                return _no_decision(
                    state,
                    f"NONFINITE_FEATURE:{feature.name}",
                    selected=selected,
                    model=model,
                    buffer=buffer,
                )
            lower = _decimal(feature.support_lower, "support lower")
            upper = _decimal(feature.support_upper, "support upper")
            if value < lower or value > upper:
                return _no_decision(
                    state,
                    f"OUT_OF_SUPPORT:{feature.name}",
                    selected=selected,
                    model=model,
                    buffer=buffer,
                )
            mean += _decimal(
                feature.coefficient, "model coefficient"
            ) * value
        lcb = mean - _nonnegative(
            model.uncertainty_radius_usd, "model uncertainty"
        )
    except (PolicyContractError, DecimalException):
        return _no_decision(
            state,
            "NONFINITE_MODEL_OUTPUT",
            selected=selected,
            model=model,
            buffer=buffer,
        )
    if not mean.is_finite() or not lcb.is_finite():
        return _no_decision(
            state,
            "NONFINITE_MODEL_OUTPUT",
            selected=selected,
            model=model,
            buffer=buffer,
        )
    action = KEEP if lcb > buffer else SUBMIT_FOK
    return PolicyDecision(
        action=action,
        reason=(
            "LCB_STRICTLY_ABOVE_BUFFER"
            if action == KEEP
            else "LCB_NOT_ABOVE_BUFFER"
        ),
        event_id=state.event_id,
        selected_exit=selected,
        mean_delta_usd=mean,
        lcb_delta_usd=lcb,
        buffer_usd=buffer,
        model_id=model.model_id,
    )


def capital_release_payload(
    *,
    event_id: str,
    market_ticker: str,
    component: str,
    decision_action: str,
    exit_route_type: Optional[str],
    release_elapsed_ms: Decimal,
    release_wall_ns: int,
    release_mono_ns: int,
    release_event_id: str,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "market_ticker": market_ticker,
        "component": component,
        "decision_action": decision_action,
        "exit_route_type": exit_route_type,
        "release_elapsed_ms": release_elapsed_ms,
        "release_wall_ns": release_wall_ns,
        "release_mono_ns": release_mono_ns,
        "release_event_id": release_event_id,
    }


@dataclass(frozen=True)
class CapitalReleaseReceipt:
    event_id: str
    market_ticker: str
    component: str
    decision_action: str
    exit_route_type: Optional[str]
    release_elapsed_ms: Decimal
    release_wall_ns: int
    release_mono_ns: int
    release_event_id: str
    source_pin: SealedPin

    def __post_init__(self) -> None:
        if self.component not in (
            FIRST_LEG_BASIS,
            RESTING_COMPLEMENT_RESERVE,
            PENDING_FOK_RESERVE,
        ):
            raise PolicyContractError("unknown capital release component")
        if self.decision_action not in (KEEP, SUBMIT_FOK):
            raise PolicyContractError("unknown release decision action")
        elapsed = _nonnegative(self.release_elapsed_ms, "release elapsed")
        _elapsed_to_ns(elapsed, "release elapsed")
        _ns(self.release_wall_ns, "release wall")
        _ns(self.release_mono_ns, "release mono")
        if not isinstance(self.release_event_id, str) or not self.release_event_id:
            raise PolicyContractError("release_event_id is required")
        _require_semantic_pin(
            self.source_pin,
            "CAPITAL_RELEASE_RECEIPT",
            capital_release_payload(
                event_id=self.event_id,
                market_ticker=self.market_ticker,
                component=self.component,
                decision_action=self.decision_action,
                exit_route_type=self.exit_route_type,
                release_elapsed_ms=elapsed,
                release_wall_ns=self.release_wall_ns,
                release_mono_ns=self.release_mono_ns,
                release_event_id=self.release_event_id,
            ),
            "capital release source pin",
        )
        object.__setattr__(self, "release_elapsed_ms", elapsed)


@dataclass(frozen=True)
class CapitalProfile:
    decision_action: str
    exit_route_type: Optional[str]
    first_leg_basis_usd: Decimal
    resting_complement_reserve_usd: Decimal
    pending_fok_reserve_usd: Decimal
    first_leg_dollar_seconds: Decimal
    resting_complement_dollar_seconds: Decimal
    pending_fok_dollar_seconds: Decimal
    total_dollar_seconds: Decimal
    peak_capital_usd: Decimal
    horizon_elapsed_ms: Decimal


def capital_profile(
    state: DecisionState,
    *,
    decision_action: str,
    exit_route_type: Optional[str],
    release_receipts: Tuple[CapitalReleaseReceipt, ...],
) -> CapitalProfile:
    """Separate KEEP and SUBMIT_FOK K(t) using bound release receipts."""
    if decision_action not in (KEEP, SUBMIT_FOK):
        raise PolicyContractError("capital decision action is invalid")
    if not isinstance(release_receipts, tuple):
        raise PolicyContractError("release_receipts must be tuple")
    if decision_action == KEEP:
        if exit_route_type is not None:
            raise PolicyContractError("KEEP cannot bind an exit route")
        required = {FIRST_LEG_BASIS, RESTING_COMPLEMENT_RESERVE}
        pending_reserve = ZERO
    else:
        routes = evaluate_exit_routes(state)
        if (
            routes.selected is None
            or exit_route_type != routes.selected.route_type
        ):
            raise PolicyContractError(
                "SUBMIT_FOK route must be the selected full route"
            )
        required = {
            FIRST_LEG_BASIS,
            RESTING_COMPLEMENT_RESERVE,
            PENDING_FOK_RESERVE,
        }
        assert routes.selected.pending_fok_reserve_usd is not None
        pending_reserve = routes.selected.pending_fok_reserve_usd

    by_component = {}
    for receipt in release_receipts:
        if not isinstance(receipt, CapitalReleaseReceipt):
            raise PolicyContractError("release item must be typed receipt")
        if receipt.component in by_component:
            raise PolicyContractError("duplicate capital release component")
        by_component[receipt.component] = receipt
    if set(by_component) != required:
        raise PolicyContractError("capital release component set mismatch")

    for receipt in by_component.values():
        if receipt.event_id != state.event_id:
            raise PolicyContractError("release event binding mismatch")
        if receipt.market_ticker != state.market.market_ticker:
            raise PolicyContractError("release market binding mismatch")
        if receipt.decision_action != decision_action:
            raise PolicyContractError("release decision action mismatch")
        if receipt.exit_route_type != exit_route_type:
            raise PolicyContractError("release exit route binding mismatch")
        elapsed = _nonnegative(
            receipt.release_elapsed_ms, "release elapsed"
        )
        elapsed_ns = _elapsed_to_ns(elapsed, "release elapsed")
        if (
            receipt.release_wall_ns
            != state.first_fill.recv_wall_ns + elapsed_ns
            or receipt.release_mono_ns
            != state.first_fill.recv_mono_ns + elapsed_ns
        ):
            raise PolicyContractError("release source clocks mismatch")
        if elapsed < state.decision_elapsed_ms:
            raise PolicyContractError(
                "active capital cannot release before decision"
            )

    if decision_action == SUBMIT_FOK:
        pending_receipt = by_component[PENDING_FOK_RESERVE]
        if (
            pending_receipt.release_elapsed_ms
            != state.fok_terminal_elapsed_ms
            or pending_receipt.release_wall_ns
            != state.fok_terminal_wall_ns
            or pending_receipt.release_mono_ns
            != state.fok_terminal_mono_ns
        ):
            raise PolicyContractError(
                "pending FOK release must equal FOK terminal"
            )

    first_basis = (
        state.first_fill.price * state.first_fill.quantity
        + state.first_fill.maker_fee_usd
    )
    resting_reserve = (
        state.resting_complement.price
        * state.resting_complement.quantity
        + state.resting_complement.maker_fee_usd
    )
    first_release = by_component[
        FIRST_LEG_BASIS
    ].release_elapsed_ms
    resting_release = by_component[
        RESTING_COMPLEMENT_RESERVE
    ].release_elapsed_ms
    first_ds = first_basis * first_release / MS_TO_SECONDS
    resting_ds = (
        resting_reserve * resting_release / MS_TO_SECONDS
    )
    if decision_action == SUBMIT_FOK:
        pending_release = by_component[
            PENDING_FOK_RESERVE
        ].release_elapsed_ms
        pending_ds = (
            pending_reserve
            * (pending_release - state.decision_elapsed_ms)
            / MS_TO_SECONDS
        )
    else:
        pending_release = ZERO
        pending_ds = ZERO

    # The first-fill atom locks B+R even when both releases are at elapsed 0.
    first_fill_atom_peak = first_basis + resting_reserve
    at_decision = (
        (
            first_basis
            if first_release >= state.decision_elapsed_ms
            else ZERO
        )
        + (
            resting_reserve
            if resting_release >= state.decision_elapsed_ms
            else ZERO
        )
        + (
            pending_reserve
            if (
                decision_action == SUBMIT_FOK
                and pending_release > state.decision_elapsed_ms
            )
            else ZERO
        )
    )
    peak = max(first_fill_atom_peak, at_decision)
    horizon = max(
        first_release,
        resting_release,
        pending_release,
    )
    return CapitalProfile(
        decision_action=decision_action,
        exit_route_type=exit_route_type,
        first_leg_basis_usd=first_basis,
        resting_complement_reserve_usd=resting_reserve,
        pending_fok_reserve_usd=pending_reserve,
        first_leg_dollar_seconds=first_ds,
        resting_complement_dollar_seconds=resting_ds,
        pending_fok_dollar_seconds=pending_ds,
        total_dollar_seconds=first_ds + resting_ds + pending_ds,
        peak_capital_usd=peak,
        horizon_elapsed_ms=horizon,
    )
