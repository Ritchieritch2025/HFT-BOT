#!/usr/bin/env python3
"""Offline-only dynamic KEEP/FLATTEN_FOK policy economics.

This is a small, fail-closed policy kernel.  It has no source reader, model
fitter, exchange client, candidate publisher, shadow adapter, or live path.
All prices, fees, rewards, and capital are derived here from typed inputs;
there are deliberately no caller-reported PnL or pair-gain fields.

Time is event-driven.  A decision may occur at any non-negative elapsed time
that is exactly representable in nanoseconds.  The 60ms constant is only the
counterfactual FOK effective horizon; it is not a KEEP timeout.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_CEILING
import re
from typing import Callable, Optional, Tuple


POLICY_VERSION = "ROUND4_DYNAMIC_KEEP_FOK_POLICY_V2"
OFFLINE_ONLY = True
CANDIDATE_ALLOWED = False
SHADOW_ALLOWED = False
LIVE_ALLOWED = False
ACTION_SEAL_ALLOWED = False

KEEP = "KEEP"
FLATTEN_FOK = "FLATTEN_FOK"
NO_DECISION = "NO_DECISION"
FOK_EFFECTIVE_LATENCY_MS = Decimal("60")
PAIR_COST_CEILING = Decimal("0.99")
TAKER_FEE_RATE = Decimal("0.07")
TRADE_FEE_QUANTUM_USD = Decimal("0.0001")
DELTA_ESTIMAND = "KEEP_MINUS_FLATTEN_FOK_NET_USD"
LCB_DEFINITION = "MEAN_MINUS_NONNEGATIVE_UNCERTAINTY_RADIUS"
ONE = Decimal("1")
ZERO = Decimal("0")
MS_TO_NS = Decimal("1000000")
MS_TO_SECONDS = Decimal("1000")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PolicyContractError(ValueError):
    """An immutable policy input violates the offline contract."""


FeeFunction = Callable[[Decimal, Decimal], Decimal]


def _as_decimal(
    value: object,
    label: str,
    *,
    finite: bool = True,
) -> Decimal:
    if isinstance(value, bool):
        raise PolicyContractError(f"{label} must be Decimal-like, not bool")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PolicyContractError(f"{label} is not Decimal-like") from exc
    if finite and not result.is_finite():
        raise PolicyContractError(f"{label} must be finite")
    return result


def _nonnegative(value: object, label: str) -> Decimal:
    result = _as_decimal(value, label)
    if result < ZERO:
        raise PolicyContractError(f"{label} must be non-negative")
    return result


def _positive(value: object, label: str) -> Decimal:
    result = _as_decimal(value, label)
    if result <= ZERO:
        raise PolicyContractError(f"{label} must be positive")
    return result


def _price(value: object, label: str) -> Decimal:
    result = _as_decimal(value, label)
    if result <= ZERO or result >= ONE:
        raise PolicyContractError(f"{label} must be strictly between 0 and 1")
    return result


def _plain_ns(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PolicyContractError(f"{label} must be a non-negative int")
    return value


def _elapsed_ns(value: object, label: str) -> int:
    elapsed = _nonnegative(value, label)
    ns = elapsed * MS_TO_NS
    integral = ns.to_integral_value()
    if ns != integral:
        raise PolicyContractError(
            f"{label} must be exactly representable in nanoseconds"
        )
    return int(integral)


@dataclass(frozen=True)
class BookLevel:
    """One visible complement ask level, in executable price order."""

    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _price(self.price, "book price"))
        object.__setattr__(
            self, "quantity", _positive(self.quantity, "book quantity")
        )


@dataclass(frozen=True)
class ObservedFeatures:
    """Causal observations only; all economic targets are derived elsewhere.

    ``None`` and non-finite Decimal observations are representable so
    ``choose_action`` can return ``NO_DECISION`` instead of silently imputing.
    """

    queue_ahead_qty: Optional[Decimal] = None
    better_depth_qty: Optional[Decimal] = None
    complement_flow_1s_qty: Optional[Decimal] = None
    complement_flow_5s_qty: Optional[Decimal] = None
    touch_distance_ticks: Optional[Decimal] = None
    spread_ticks: Optional[Decimal] = None
    fair_move_since_fill: Optional[Decimal] = None
    tte_ms: Optional[Decimal] = None


OBSERVED_FEATURE_NAMES = tuple(field.name for field in fields(ObservedFeatures))
DERIVED_FEATURE_NAMES = (
    "elapsed_ms",
    "first_price",
    "complement_price",
    "quantity",
    "pair_cost_per_contract",
    "pair_gain_usd",
    "fok_full_fill_flag",
    "fok_executable_qty",
    "fok_average_price",
    "fok_taker_fee_usd",
    "fok_net_pnl_usd",
)
ALLOWED_MODEL_FEATURES = frozenset(
    OBSERVED_FEATURE_NAMES + DERIVED_FEATURE_NAMES
)


@dataclass(frozen=True)
class DecisionState:
    """Validated one-leg state at one event-driven decision receipt."""

    event_id: str
    first_fill_side: str
    first_fill_price: Decimal
    first_fill_qty: Decimal
    first_maker_fee_usd: Decimal
    complement_side: str
    complement_price: Decimal
    complement_qty: Decimal
    complement_maker_fee_usd: Decimal
    fok_limit_price: Decimal
    visible_fok_levels: Tuple[BookLevel, ...]
    observed: ObservedFeatures
    first_fill_recv_wall_ns: int
    first_fill_recv_mono_ns: int
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
            raise PolicyContractError("event_id must be non-empty text")
        if self.first_fill_side not in ("YES", "NO"):
            raise PolicyContractError("first_fill_side must be YES or NO")
        expected_complement = (
            "NO" if self.first_fill_side == "YES" else "YES"
        )
        if self.complement_side != expected_complement:
            raise PolicyContractError("complement_side must oppose first side")

        first_price = _price(self.first_fill_price, "first_fill_price")
        complement_price = _price(
            self.complement_price, "complement_price"
        )
        limit_price = _price(self.fok_limit_price, "fok_limit_price")
        first_qty = _positive(self.first_fill_qty, "first_fill_qty")
        complement_qty = _positive(
            self.complement_qty, "complement_qty"
        )
        if first_qty != complement_qty:
            raise PolicyContractError(
                "complement_qty must exactly match first_fill_qty"
            )
        first_fee = _nonnegative(
            self.first_maker_fee_usd, "first_maker_fee_usd"
        )
        complement_fee = _nonnegative(
            self.complement_maker_fee_usd,
            "complement_maker_fee_usd",
        )
        if first_price + complement_price > PAIR_COST_CEILING:
            raise PolicyContractError(
                "resting pair exceeds the 0.99 pair cost ceiling"
            )
        try:
            derived_amounts = (
                first_price * first_qty + first_fee,
                complement_price * complement_qty + complement_fee,
                (ONE - first_price - complement_price) * first_qty
                - first_fee
                - complement_fee,
                limit_price * first_qty,
            )
        except DecimalException as exc:
            raise PolicyContractError(
                "decision economics overflow Decimal context"
            ) from exc
        if any(not value.is_finite() for value in derived_amounts):
            raise PolicyContractError(
                "decision economics must remain finite"
            )
        if not isinstance(self.visible_fok_levels, tuple):
            raise PolicyContractError("visible_fok_levels must be a tuple")
        previous: Optional[Decimal] = None
        normalized_levels = []
        for index, level in enumerate(self.visible_fok_levels):
            if not isinstance(level, BookLevel):
                raise PolicyContractError(
                    f"visible_fok_levels[{index}] must be BookLevel"
                )
            if previous is not None and level.price < previous:
                raise PolicyContractError(
                    "visible FOK levels must be in nondecreasing ask order"
                )
            if level.price > limit_price:
                raise PolicyContractError(
                    "visible FOK level exceeds the FOK limit price"
                )
            normalized_levels.append(level)
            previous = level.price
        if not isinstance(self.observed, ObservedFeatures):
            raise PolicyContractError("observed must be ObservedFeatures")

        first_wall = _plain_ns(
            self.first_fill_recv_wall_ns, "first_fill_recv_wall_ns"
        )
        first_mono = _plain_ns(
            self.first_fill_recv_mono_ns, "first_fill_recv_mono_ns"
        )
        elapsed = _nonnegative(
            self.decision_elapsed_ms, "decision_elapsed_ms"
        )
        elapsed_ns = _elapsed_ns(elapsed, "decision_elapsed_ms")
        decision_wall = _plain_ns(
            self.decision_recv_wall_ns, "decision_recv_wall_ns"
        )
        decision_mono = _plain_ns(
            self.decision_recv_mono_ns, "decision_recv_mono_ns"
        )
        if (
            decision_wall != first_wall + elapsed_ns
            or decision_mono != first_mono + elapsed_ns
        ):
            raise PolicyContractError(
                "decision timestamp must equal first fill + elapsed"
            )
        asof_wall = _plain_ns(
            self.feature_asof_wall_ns, "feature_asof_wall_ns"
        )
        asof_mono = _plain_ns(
            self.feature_asof_mono_ns, "feature_asof_mono_ns"
        )
        if asof_wall != decision_wall or asof_mono != decision_mono:
            raise PolicyContractError(
                "feature asof must equal decision on wall and mono clocks"
            )
        source_wall = _plain_ns(
            self.source_max_recv_wall_ns, "source_max_recv_wall_ns"
        )
        source_mono = _plain_ns(
            self.source_max_recv_mono_ns, "source_max_recv_mono_ns"
        )
        if source_wall > asof_wall or source_mono > asof_mono:
            raise PolicyContractError("source must be no later than asof")

        terminal_elapsed = _nonnegative(
            self.fok_terminal_elapsed_ms, "fok_terminal_elapsed_ms"
        )
        terminal_wall = _plain_ns(
            self.fok_terminal_wall_ns, "fok_terminal_wall_ns"
        )
        terminal_mono = _plain_ns(
            self.fok_terminal_mono_ns, "fok_terminal_mono_ns"
        )
        expected_terminal_elapsed = elapsed + FOK_EFFECTIVE_LATENCY_MS
        if (
            terminal_elapsed != expected_terminal_elapsed
            or terminal_wall != decision_wall + 60_000_000
            or terminal_mono != decision_mono + 60_000_000
        ):
            raise PolicyContractError(
                "FOK terminal must be exactly decision + 60ms"
            )

        object.__setattr__(self, "first_fill_price", first_price)
        object.__setattr__(self, "complement_price", complement_price)
        object.__setattr__(self, "fok_limit_price", limit_price)
        object.__setattr__(self, "first_fill_qty", first_qty)
        object.__setattr__(self, "complement_qty", complement_qty)
        object.__setattr__(self, "first_maker_fee_usd", first_fee)
        object.__setattr__(
            self, "complement_maker_fee_usd", complement_fee
        )
        object.__setattr__(
            self, "visible_fok_levels", tuple(normalized_levels)
        )
        object.__setattr__(self, "decision_elapsed_ms", elapsed)
        object.__setattr__(
            self, "fok_terminal_elapsed_ms", terminal_elapsed
        )


@dataclass(frozen=True)
class PairEconomics:
    pair_cost_per_contract: Decimal
    gross_gain_usd: Decimal
    maker_fees_usd: Decimal
    net_gain_usd: Decimal


def pair_economics(state: DecisionState) -> PairEconomics:
    """Recompute the resting pair reward; no reported reward is accepted."""
    pair_cost = state.first_fill_price + state.complement_price
    if pair_cost > PAIR_COST_CEILING:
        raise PolicyContractError("pair cost ceiling exceeded")
    gross = (ONE - pair_cost) * state.first_fill_qty
    maker_fees = (
        state.first_maker_fee_usd + state.complement_maker_fee_usd
    )
    return PairEconomics(
        pair_cost_per_contract=pair_cost,
        gross_gain_usd=gross,
        maker_fees_usd=maker_fees,
        net_gain_usd=gross - maker_fees,
    )


def _ceil_quantum(value: Decimal, quantum: Decimal) -> Decimal:
    if value == ZERO:
        return ZERO
    units = (value / quantum).to_integral_value(rounding=ROUND_CEILING)
    return units * quantum


def centicent_quadratic_taker_fee(
    price: Decimal,
    quantity: Decimal,
) -> Decimal:
    """$0.0001 ceiling of 0.07*q*p*(1-p), once per execution slice."""
    p = _price(price, "fee price")
    q = _positive(quantity, "fee quantity")
    raw = TAKER_FEE_RATE * q * p * (ONE - p)
    return _ceil_quantum(raw, TRADE_FEE_QUANTUM_USD)


def _fee(
    fee_fn: FeeFunction,
    price: Decimal,
    quantity: Decimal,
) -> Decimal:
    try:
        value = fee_fn(price, quantity)
    except Exception as exc:
        raise PolicyContractError("fee function failed") from exc
    fee = _as_decimal(value, "fee function result")
    if fee < ZERO:
        raise PolicyContractError("fee function result must be non-negative")
    return fee


@dataclass(frozen=True)
class ExecutedSlice:
    price: Decimal
    quantity: Decimal
    taker_fee_usd: Decimal


@dataclass(frozen=True)
class FokRoute:
    event_id: str
    full_fill: bool
    requested_quantity: Decimal
    visible_quantity: Decimal
    executable_quantity: Decimal
    executions: Tuple[ExecutedSlice, ...]
    taker_fee_usd: Decimal
    gross_pnl_usd: Optional[Decimal]
    net_pnl_usd: Optional[Decimal]
    average_price: Optional[Decimal]
    pending_reserve_usd: Decimal
    terminal_elapsed_ms: Decimal
    terminal_wall_ns: int
    terminal_mono_ns: int


def walk_visible_fok(
    state: DecisionState,
    *,
    fee_fn: FeeFunction = centicent_quadratic_taker_fee,
) -> FokRoute:
    """Walk visible complement levels and derive an all-or-none FOK route.

    Each input level is an exact execution slice for fee rounding.  When
    visible quantity is insufficient, fill-or-kill executes zero quantity.
    The pending reserve is still derived from the order limit and the fee
    function; it is never accepted from the caller.
    """
    remaining = state.first_fill_qty
    proposed = []
    visible = ZERO
    for level in state.visible_fok_levels:
        if remaining <= ZERO:
            break
        take = min(level.quantity, remaining)
        visible += take
        proposed.append((level.price, take))
        remaining -= take
    full = remaining == ZERO
    if full:
        executions = tuple(
            ExecutedSlice(
                price=price,
                quantity=quantity,
                taker_fee_usd=_fee(fee_fn, price, quantity),
            )
            for price, quantity in proposed
        )
        executable = state.first_fill_qty
        taker_fee = sum(
            (item.taker_fee_usd for item in executions), ZERO
        )
        complement_principal = sum(
            (item.price * item.quantity for item in executions), ZERO
        )
        first_principal = state.first_fill_price * state.first_fill_qty
        gross = (
            state.first_fill_qty - first_principal - complement_principal
        )
        net = gross - state.first_maker_fee_usd - taker_fee
        average = complement_principal / state.first_fill_qty
        # Limit principal is the actual order reservation; route fees retain
        # the exact per-slice rounding observed in this counterfactual.
        pending_reserve = (
            state.fok_limit_price * state.first_fill_qty + taker_fee
        )
    else:
        executions = ()
        executable = ZERO
        taker_fee = ZERO
        gross = None
        net = None
        average = None
        pending_reserve = (
            state.fok_limit_price * state.first_fill_qty
            + _fee(
                fee_fn,
                state.fok_limit_price,
                state.first_fill_qty,
            )
        )
    return FokRoute(
        event_id=state.event_id,
        full_fill=full,
        requested_quantity=state.first_fill_qty,
        visible_quantity=visible,
        executable_quantity=executable,
        executions=executions,
        taker_fee_usd=taker_fee,
        gross_pnl_usd=gross,
        net_pnl_usd=net,
        average_price=average,
        pending_reserve_usd=pending_reserve,
        terminal_elapsed_ms=state.fok_terminal_elapsed_ms,
        terminal_wall_ns=state.fok_terminal_wall_ns,
        terminal_mono_ns=state.fok_terminal_mono_ns,
    )


@dataclass(frozen=True)
class ModelFeature:
    name: str
    coefficient: Decimal
    support_lower: Decimal
    support_upper: Decimal


@dataclass(frozen=True)
class TrainOnlyLCBModel:
    """Frozen parameters whose provenance must assert TRAIN_ONLY."""

    model_id: str
    training_role: str
    training_receipt_sha256: str
    delta_estimand: str
    lcb_definition: str
    intercept_mean_delta_usd: Decimal
    uncertainty_radius_usd: Decimal
    features: Tuple[ModelFeature, ...]


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    reason: str
    event_id: str
    model_id: Optional[str]
    mean_delta_usd: Optional[Decimal]
    lcb_delta_usd: Optional[Decimal]
    buffer_usd: Optional[Decimal]
    pair: PairEconomics
    fok: Optional[FokRoute]


def _model_error(model: TrainOnlyLCBModel) -> Optional[str]:
    if not isinstance(model, TrainOnlyLCBModel):
        return "INVALID_MODEL_TYPE"
    if not isinstance(model.model_id, str) or not model.model_id:
        return "INVALID_MODEL_ID"
    if model.training_role != "TRAIN_ONLY":
        return "MODEL_NOT_TRAIN_ONLY"
    if model.delta_estimand != DELTA_ESTIMAND:
        return "INVALID_DELTA_ESTIMAND"
    if model.lcb_definition != LCB_DEFINITION:
        return "INVALID_LCB_DEFINITION"
    if (
        not isinstance(model.training_receipt_sha256, str)
        or SHA256_RE.fullmatch(model.training_receipt_sha256) is None
    ):
        return "INVALID_TRAINING_RECEIPT"
    try:
        intercept = _as_decimal(
            model.intercept_mean_delta_usd, "model intercept"
        )
        radius = _nonnegative(
            model.uncertainty_radius_usd, "model uncertainty"
        )
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
            coefficient = _as_decimal(
                feature.coefficient, "feature coefficient"
            )
            lower = _as_decimal(
                feature.support_lower, "feature support lower"
            )
            upper = _as_decimal(
                feature.support_upper, "feature support upper"
            )
        except PolicyContractError:
            return f"NONFINITE_MODEL_FEATURE:{feature.name}"
        if lower > upper:
            return f"INVALID_FEATURE_SUPPORT:{feature.name}"
        # Bind normalized values to local variables so this branch cannot be
        # optimized into accepting an unchecked Decimal subclass.
        if not (
            intercept.is_finite()
            and radius.is_finite()
            and coefficient.is_finite()
        ):
            return "NONFINITE_OR_INVALID_MODEL"
    return None


def _current_features(
    state: DecisionState,
    pair: PairEconomics,
    route: FokRoute,
) -> dict[str, Optional[Decimal]]:
    values: dict[str, Optional[Decimal]] = {}
    for field in fields(ObservedFeatures):
        value = getattr(state.observed, field.name)
        if value is None:
            values[field.name] = None
        else:
            try:
                values[field.name] = _as_decimal(
                    value, field.name, finite=False
                )
            except PolicyContractError:
                values[field.name] = None
    values.update(
        {
            "elapsed_ms": state.decision_elapsed_ms,
            "first_price": state.first_fill_price,
            "complement_price": state.complement_price,
            "quantity": state.first_fill_qty,
            "pair_cost_per_contract": pair.pair_cost_per_contract,
            "pair_gain_usd": pair.net_gain_usd,
            "fok_full_fill_flag": ONE if route.full_fill else ZERO,
            "fok_executable_qty": route.executable_quantity,
            "fok_average_price": route.average_price,
            "fok_taker_fee_usd": route.taker_fee_usd,
            "fok_net_pnl_usd": route.net_pnl_usd,
        }
    )
    return values


def _no_decision(
    *,
    state: DecisionState,
    pair: PairEconomics,
    reason: str,
    model: Optional[TrainOnlyLCBModel],
    route: Optional[FokRoute] = None,
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
        model_id=model_id,
        mean_delta_usd=None,
        lcb_delta_usd=None,
        buffer_usd=buffer,
        pair=pair,
        fok=route,
    )


def choose_action(
    state: DecisionState,
    model: Optional[TrainOnlyLCBModel],
    *,
    buffer_usd: Decimal,
    fee_fn: FeeFunction = centicent_quadratic_taker_fee,
) -> PolicyDecision:
    """Choose KEEP only when a train-only conservative LCB clears buffer."""
    pair = pair_economics(state)
    try:
        buffer = _nonnegative(buffer_usd, "buffer_usd")
    except PolicyContractError:
        return _no_decision(
            state=state,
            pair=pair,
            reason="INVALID_BUFFER",
            model=model,
        )
    if model is None:
        return _no_decision(
            state=state,
            pair=pair,
            reason="MISSING_MODEL",
            model=None,
            buffer=buffer,
        )
    model_error = _model_error(model)
    if model_error is not None:
        return _no_decision(
            state=state,
            pair=pair,
            reason=model_error,
            model=model,
            buffer=buffer,
        )
    try:
        route = walk_visible_fok(state, fee_fn=fee_fn)
    except PolicyContractError:
        return _no_decision(
            state=state,
            pair=pair,
            reason="INVALID_FOK_ECONOMICS",
            model=model,
            buffer=buffer,
        )
    current = _current_features(state, pair, route)
    mean = _as_decimal(model.intercept_mean_delta_usd, "model intercept")
    for feature in model.features:
        value = current.get(feature.name)
        if value is None:
            return _no_decision(
                state=state,
                pair=pair,
                reason=f"MISSING_FEATURE:{feature.name}",
                model=model,
                route=route,
                buffer=buffer,
            )
        if not value.is_finite():
            return _no_decision(
                state=state,
                pair=pair,
                reason=f"NONFINITE_FEATURE:{feature.name}",
                model=model,
                route=route,
                buffer=buffer,
            )
        lower = _as_decimal(feature.support_lower, "support lower")
        upper = _as_decimal(feature.support_upper, "support upper")
        if value < lower or value > upper:
            return _no_decision(
                state=state,
                pair=pair,
                reason=f"OUT_OF_SUPPORT:{feature.name}",
                model=model,
                route=route,
                buffer=buffer,
            )
        try:
            mean += _as_decimal(
                feature.coefficient, "feature coefficient"
            ) * value
        except DecimalException:
            return _no_decision(
                state=state,
                pair=pair,
                reason="NONFINITE_MODEL_OUTPUT",
                model=model,
                route=route,
                buffer=buffer,
            )
    radius = _nonnegative(
        model.uncertainty_radius_usd, "model uncertainty"
    )
    try:
        lcb = mean - radius
    except DecimalException:
        return _no_decision(
            state=state,
            pair=pair,
            reason="NONFINITE_MODEL_OUTPUT",
            model=model,
            route=route,
            buffer=buffer,
        )
    if not mean.is_finite() or not lcb.is_finite():
        return _no_decision(
            state=state,
            pair=pair,
            reason="NONFINITE_MODEL_OUTPUT",
            model=model,
            route=route,
            buffer=buffer,
        )
    action = KEEP if lcb > buffer else FLATTEN_FOK
    reason = (
        "LCB_STRICTLY_ABOVE_BUFFER"
        if action == KEEP
        else "LCB_NOT_ABOVE_BUFFER"
    )
    return PolicyDecision(
        action=action,
        reason=reason,
        event_id=state.event_id,
        model_id=model.model_id,
        mean_delta_usd=mean,
        lcb_delta_usd=lcb,
        buffer_usd=buffer,
        pair=pair,
        fok=route,
    )


@dataclass(frozen=True)
class CapitalReleaseSchedule:
    """Observed release times for all three capital components."""

    first_leg_release_elapsed_ms: Decimal
    resting_complement_release_elapsed_ms: Decimal
    pending_fok_release_elapsed_ms: Decimal


@dataclass(frozen=True)
class CapitalProfile:
    first_leg_basis_usd: Decimal
    resting_complement_reserve_usd: Decimal
    pending_fok_reserve_usd: Decimal
    first_leg_dollar_seconds: Decimal
    resting_complement_dollar_seconds: Decimal
    pending_fok_dollar_seconds: Decimal
    total_dollar_seconds: Decimal
    peak_capital_usd: Decimal
    horizon_elapsed_ms: Decimal


def _validated_releases(
    state: DecisionState,
    releases: CapitalReleaseSchedule,
    fee_fn: FeeFunction,
) -> tuple[FokRoute, Decimal, Decimal, Decimal]:
    if not isinstance(releases, CapitalReleaseSchedule):
        raise PolicyContractError(
            "releases must be CapitalReleaseSchedule"
        )
    route = walk_visible_fok(state, fee_fn=fee_fn)
    first_release = _nonnegative(
        releases.first_leg_release_elapsed_ms,
        "first_leg_release_elapsed_ms",
    )
    complement_release = _nonnegative(
        releases.resting_complement_release_elapsed_ms,
        "resting_complement_release_elapsed_ms",
    )
    pending_release = _nonnegative(
        releases.pending_fok_release_elapsed_ms,
        "pending_fok_release_elapsed_ms",
    )
    _elapsed_ns(first_release, "first_leg_release_elapsed_ms")
    _elapsed_ns(
        complement_release,
        "resting_complement_release_elapsed_ms",
    )
    _elapsed_ns(pending_release, "pending_fok_release_elapsed_ms")
    if (
        first_release < state.decision_elapsed_ms
        or complement_release < state.decision_elapsed_ms
    ):
        raise PolicyContractError(
            "active first/resting capital cannot release before decision"
        )
    if pending_release != state.fok_terminal_elapsed_ms:
        raise PolicyContractError(
            "pending FOK release must equal the exact FOK terminal"
        )
    return route, first_release, complement_release, pending_release


def capital_at_elapsed(
    state: DecisionState,
    releases: CapitalReleaseSchedule,
    *,
    elapsed_ms: Decimal,
    fee_fn: FeeFunction = centicent_quadratic_taker_fee,
) -> Decimal:
    """K(t), deriving the FOK reserve instead of accepting a reported one."""
    route, first_release, complement_release, pending_release = (
        _validated_releases(state, releases, fee_fn)
    )
    elapsed = _nonnegative(elapsed_ms, "elapsed_ms")
    first_basis = (
        state.first_fill_price * state.first_fill_qty
        + state.first_maker_fee_usd
    )
    resting_reserve = (
        state.complement_price * state.complement_qty
        + state.complement_maker_fee_usd
    )
    capital = ZERO
    if elapsed < first_release:
        capital += first_basis
    if elapsed < complement_release:
        capital += resting_reserve
    if state.decision_elapsed_ms <= elapsed < pending_release:
        capital += route.pending_reserve_usd
    return capital


def capital_profile(
    state: DecisionState,
    releases: CapitalReleaseSchedule,
    *,
    fee_fn: FeeFunction = centicent_quadratic_taker_fee,
) -> CapitalProfile:
    """Integrate each derived capital term over its actual release interval."""
    route, first_release, complement_release, pending_release = (
        _validated_releases(state, releases, fee_fn)
    )
    first_basis = (
        state.first_fill_price * state.first_fill_qty
        + state.first_maker_fee_usd
    )
    resting_reserve = (
        state.complement_price * state.complement_qty
        + state.complement_maker_fee_usd
    )
    pending_reserve = route.pending_reserve_usd
    first_ds = first_basis * first_release / MS_TO_SECONDS
    complement_ds = (
        resting_reserve * complement_release / MS_TO_SECONDS
    )
    pending_ds = (
        pending_reserve
        * (pending_release - state.decision_elapsed_ms)
        / MS_TO_SECONDS
    )
    pre_decision_capital = (
        first_basis + resting_reserve
        if state.decision_elapsed_ms > ZERO
        else ZERO
    )
    at_decision_capital = (
        (first_basis if state.decision_elapsed_ms < first_release else ZERO)
        + (
            resting_reserve
            if state.decision_elapsed_ms < complement_release
            else ZERO
        )
        + (
            pending_reserve
            if state.decision_elapsed_ms < pending_release
            else ZERO
        )
    )
    peak = max(pre_decision_capital, at_decision_capital)
    return CapitalProfile(
        first_leg_basis_usd=first_basis,
        resting_complement_reserve_usd=resting_reserve,
        pending_fok_reserve_usd=pending_reserve,
        first_leg_dollar_seconds=first_ds,
        resting_complement_dollar_seconds=complement_ds,
        pending_fok_dollar_seconds=pending_ds,
        total_dollar_seconds=first_ds + complement_ds + pending_ds,
        peak_capital_usd=peak,
        horizon_elapsed_ms=max(
            first_release, complement_release, pending_release
        ),
    )
