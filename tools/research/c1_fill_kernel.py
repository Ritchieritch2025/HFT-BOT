#!/usr/bin/env python3
"""Deterministic, fixed-point fill bounds for the C1 passive-order test.

This module is intentionally small and economics-free.  It answers only one
question: given one already-active virtual passive order and an ordered public
trade tape, how much of that order may be counted as filled under each audited
fill track?

The tracks are deliberately not interchangeable:

``STRICT_THROUGH``
    Counts only a matching-direction trade strictly through the virtual
    order's price.  A print at the exact order price never fills this track.

``QUEUE_PESSIMISTIC``
    Starts behind the supplied displayed queue.  Only matching-direction
    public volume at the exact price consumes that queue.  Volume beyond the
    queue may fill the virtual order.  A strict-through print also fills (and
    proves that the old queue can no longer remain ahead of the order).

``OPTIMISTIC_AT_TOUCH``
    Counts any matching-direction trade at the exact price, as well as a
    strict-through print, without a queue requirement.  This is a diagnostic
    upper bound and is explicitly ineligible for a promotion gate.

All prices and quantities are integers in E4 units.  There are no floats,
fees, PnL, inferred cancellations, or future-book observations here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


DEFAULT_ORDER_QUANTITY_E4 = 10_000
MIN_PRICE_E4 = 1
MAX_PRICE_E4 = 9_999


class FillKernelError(ValueError):
    """An input violates the fail-closed fill-kernel contract."""


class FillTrack(str, Enum):
    """The three frozen C1 fill tracks."""

    STRICT_THROUGH = "STRICT_THROUGH"
    QUEUE_PESSIMISTIC = "QUEUE_PESSIMISTIC"
    OPTIMISTIC_AT_TOUCH = "OPTIMISTIC_AT_TOUCH"

    @property
    def eligible_for_promotion_gate(self) -> bool:
        """Whether this track may participate in a strategy promotion gate."""
        # The frozen C1 spec permits only the strict-through lower bound in a
        # headline decision.  Queue-pessimistic remains a useful diagnostic,
        # but without disjoint own-order calibration it is not gate-bearing.
        return self is FillTrack.STRICT_THROUGH


def _require_plain_int(name: str, value: object) -> int:
    """Accept a plain integer only; implicit fixed-point coercion is unsafe."""
    if type(value) is not int:  # Deliberately rejects bool and integer-ish float.
        raise FillKernelError(f"{name} must be a plain integer")
    return value


def _require_side(name: str, value: object) -> str:
    if value not in ("yes", "no"):
        raise FillKernelError(f"{name} must be exactly 'yes' or 'no'")
    return value


def _validate_virtual_order(value: "VirtualOrder") -> None:
    _require_side("order.side", value.side)
    price = _require_plain_int("order.price_e4", value.price_e4)
    activation = _require_plain_int("order.activation_us", value.activation_us)
    ahead = _require_plain_int("order.queue_ahead_e4", value.queue_ahead_e4)
    quantity = _require_plain_int("order.quantity_e4", value.quantity_e4)
    if not MIN_PRICE_E4 <= price <= MAX_PRICE_E4:
        raise FillKernelError("order.price_e4 is outside [1, 9999]")
    if activation < 0:
        raise FillKernelError("order.activation_us must be non-negative")
    if ahead < 0:
        raise FillKernelError("order.queue_ahead_e4 must be non-negative")
    if quantity <= 0:
        raise FillKernelError("order.quantity_e4 must be positive")


def _validate_public_trade(value: "PublicTrade", *, prefix: str = "trade") -> None:
    timestamp = _require_plain_int(f"{prefix}.timestamp_us", value.timestamp_us)
    price = _require_plain_int(f"{prefix}.yes_price_e4", value.yes_price_e4)
    count = _require_plain_int(f"{prefix}.count_e4", value.count_e4)
    _require_side(f"{prefix}.taker_side", value.taker_side)
    if timestamp < 0:
        raise FillKernelError(f"{prefix}.timestamp_us must be non-negative")
    if not MIN_PRICE_E4 <= price <= MAX_PRICE_E4:
        raise FillKernelError(f"{prefix}.yes_price_e4 is outside [1, 9999]")
    if count <= 0:
        raise FillKernelError(f"{prefix}.count_e4 must be positive")


@dataclass(frozen=True)
class VirtualOrder:
    """One passive order whose price is expressed in its own outcome side."""

    side: str
    price_e4: int
    activation_us: int
    queue_ahead_e4: int = 0
    quantity_e4: int = DEFAULT_ORDER_QUANTITY_E4

    def __post_init__(self) -> None:
        _validate_virtual_order(self)

    @property
    def equivalent_yes_price_e4(self) -> int:
        """Return the YES-price representation of this outcome-side order."""
        if self.side == "yes":
            return self.price_e4
        return 10_000 - self.price_e4


@dataclass(frozen=True)
class PublicTrade:
    """The only public-tape fields the fill kernel is allowed to consume."""

    timestamp_us: int
    yes_price_e4: int
    count_e4: int
    taker_side: str

    def __post_init__(self) -> None:
        _validate_public_trade(self)


@dataclass(frozen=True)
class FillEvent:
    """One auditable contribution from one public print."""

    trade_index: int
    timestamp_us: int
    reason: str
    public_count_e4: int
    queue_before_e4: int | None
    queue_after_e4: int | None
    fill_count_e4: int


@dataclass(frozen=True)
class FillResult:
    """A fixed-point result for exactly one track and one virtual order."""

    track: FillTrack
    eligible_for_promotion_gate: bool
    order_quantity_e4: int
    filled_e4: int
    remaining_e4: int
    queue_ahead_remaining_e4: int | None
    input_trade_count: int
    rejected_nonfuture_count: int
    matching_touch_trade_count: int
    matching_strict_trade_count: int
    fill_events: tuple[FillEvent, ...]


def _coerce_track(track: FillTrack | str) -> FillTrack:
    if isinstance(track, FillTrack):
        return track
    if type(track) is str:
        try:
            return FillTrack(track)
        except ValueError as exc:
            raise FillKernelError(f"unknown fill track: {track!r}") from exc
    raise FillKernelError("track must be a FillTrack or its exact string value")


def _validate_trade_order(trades: tuple[PublicTrade, ...]) -> None:
    """Validate the complete tape before any early fill can hide a bad row."""
    previous_timestamp: int | None = None
    for index, trade in enumerate(trades):
        if not isinstance(trade, PublicTrade):
            raise FillKernelError(
                f"trades[{index}] must be a PublicTrade, got "
                f"{type(trade).__name__}"
            )
        # Revalidate already-constructed values.  This protects callers that
        # deserialize unsafely or mutate frozen objects through low-level
        # mechanisms; an early full fill must not hide a later corrupt row.
        _validate_public_trade(trade, prefix=f"trades[{index}]")
        if previous_timestamp is not None and trade.timestamp_us < previous_timestamp:
            raise FillKernelError(
                f"trade tape is out of order at index {index}: "
                f"{trade.timestamp_us} < {previous_timestamp}"
            )
        previous_timestamp = trade.timestamp_us


def _classify_matching_trade(
    order: VirtualOrder, trade: PublicTrade
) -> str | None:
    """Return ``touch``, ``strict``, or ``None`` for one public print.

    Passive YES at ``p`` is sell-filled by taker NO.  Its strict-through
    condition is ``trade_yes_price < p``.  Passive NO at ``q`` is sell-filled
    by taker YES; in YES-price coordinates its touch is ``10000-q`` and its
    strict-through condition is ``trade_yes_price > 10000-q``.
    """
    touch = order.equivalent_yes_price_e4
    if order.side == "yes":
        if trade.taker_side != "no":
            return None
        if trade.yes_price_e4 < touch:
            return "strict"
        if trade.yes_price_e4 == touch:
            return "touch"
        return None

    if trade.taker_side != "yes":
        return None
    if trade.yes_price_e4 > touch:
        return "strict"
    if trade.yes_price_e4 == touch:
        return "touch"
    return None


def simulate_fill(
    order: VirtualOrder,
    trades: Iterable[PublicTrade],
    track: FillTrack | str,
) -> FillResult:
    """Simulate one fill track without inferring unobserved executions.

    Trades with ``timestamp_us <= order.activation_us`` are rejected from the
    eligible tape: an order cannot be filled by a print observed at or before
    its activation event.  They remain counted in the result so the runner can
    audit its time filter.  A timestamp regression, malformed row, or unknown
    side fails the complete call before a result is returned.
    """
    if not isinstance(order, VirtualOrder):
        raise FillKernelError("order must be a VirtualOrder")
    _validate_virtual_order(order)
    selected_track = _coerce_track(track)
    materialized = tuple(trades)
    _validate_trade_order(materialized)

    remaining = order.quantity_e4
    filled = 0
    queue_ahead = order.queue_ahead_e4
    rejected_nonfuture = 0
    matching_touch = 0
    matching_strict = 0
    events: list[FillEvent] = []

    # Do not short-circuit validation when the order fills.  Validation above
    # has already consumed and checked the complete caller-supplied tape.
    for index, trade in enumerate(materialized):
        if trade.timestamp_us <= order.activation_us:
            rejected_nonfuture += 1
            continue

        classification = _classify_matching_trade(order, trade)
        if classification is None:
            continue
        if classification == "touch":
            matching_touch += 1
        else:
            matching_strict += 1
        if remaining == 0:
            continue

        queue_before: int | None = None
        queue_after: int | None = None
        fill_count = 0
        reason: str | None = None

        if classification == "strict":
            # Trading strictly through the order price proves that the former
            # same-price queue is no longer ahead.  The virtual fill is still
            # capped by this public print's quantity.
            if selected_track is FillTrack.QUEUE_PESSIMISTIC:
                queue_before = queue_ahead
                queue_ahead = 0
                queue_after = queue_ahead
            fill_count = min(remaining, trade.count_e4)
            reason = "STRICT_THROUGH"
        elif selected_track is FillTrack.QUEUE_PESSIMISTIC:
            queue_before = queue_ahead
            consumed_ahead = min(queue_ahead, trade.count_e4)
            queue_ahead -= consumed_ahead
            residual_public_volume = trade.count_e4 - consumed_ahead
            queue_after = queue_ahead
            fill_count = min(remaining, residual_public_volume)
            if fill_count:
                reason = "AT_TOUCH_AFTER_QUEUE"
        elif selected_track is FillTrack.OPTIMISTIC_AT_TOUCH:
            fill_count = min(remaining, trade.count_e4)
            reason = "OPTIMISTIC_AT_TOUCH"
        # STRICT_THROUGH deliberately does nothing with an at-touch print.

        if fill_count:
            # These invariants are local defenses against future refactors
            # accidentally inventing more fill than the observed print/order.
            if fill_count > trade.count_e4 or fill_count > remaining:
                raise AssertionError("fill exceeded public volume or remaining order")
            filled += fill_count
            remaining -= fill_count
            events.append(
                FillEvent(
                    trade_index=index,
                    timestamp_us=trade.timestamp_us,
                    reason=reason or "UNREACHABLE",
                    public_count_e4=trade.count_e4,
                    queue_before_e4=queue_before,
                    queue_after_e4=queue_after,
                    fill_count_e4=fill_count,
                )
            )

    if filled + remaining != order.quantity_e4:
        raise AssertionError("fill conservation failed")

    return FillResult(
        track=selected_track,
        eligible_for_promotion_gate=selected_track.eligible_for_promotion_gate,
        order_quantity_e4=order.quantity_e4,
        filled_e4=filled,
        remaining_e4=remaining,
        queue_ahead_remaining_e4=(
            queue_ahead
            if selected_track is FillTrack.QUEUE_PESSIMISTIC
            else None
        ),
        input_trade_count=len(materialized),
        rejected_nonfuture_count=rejected_nonfuture,
        matching_touch_trade_count=matching_touch,
        matching_strict_trade_count=matching_strict,
        fill_events=tuple(events),
    )


def simulate_all_tracks(
    order: VirtualOrder, trades: Iterable[PublicTrade]
) -> tuple[FillResult, FillResult, FillResult]:
    """Run the frozen tracks in conservative-to-optimistic order."""
    materialized = tuple(trades)
    return tuple(  # type: ignore[return-value]
        simulate_fill(order, materialized, track) for track in FillTrack
    )
