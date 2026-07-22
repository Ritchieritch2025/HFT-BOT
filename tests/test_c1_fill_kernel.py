#!/usr/bin/env python3
"""Adversarial tests for the C1 fixed-point fill kernel."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "research"))

import c1_fill_kernel as c1  # noqa: E402


def order(
    *, side: str = "yes", price: int = 4_000, activation: int = 1_000,
    ahead: int = 0, quantity: int = 10_000,
) -> c1.VirtualOrder:
    return c1.VirtualOrder(
        side=side,
        price_e4=price,
        activation_us=activation,
        queue_ahead_e4=ahead,
        quantity_e4=quantity,
    )


def trade(
    timestamp: int, price: int, count: int = 10_000, side: str = "no"
) -> c1.PublicTrade:
    return c1.PublicTrade(
        timestamp_us=timestamp,
        yes_price_e4=price,
        count_e4=count,
        taker_side=side,
    )


def test_default_virtual_order_is_exactly_one_contract_e4():
    candidate = c1.VirtualOrder(side="yes", price_e4=4_000, activation_us=1)
    assert candidate.quantity_e4 == 10_000


def test_yes_equal_price_never_fills_strict_through():
    result = c1.simulate_fill(
        order(), [trade(1_001, 4_000, 50_000)], c1.FillTrack.STRICT_THROUGH
    )
    assert result.filled_e4 == 0
    assert result.matching_touch_trade_count == 1
    assert result.matching_strict_trade_count == 0
    assert result.fill_events == ()


def test_yes_requires_complementary_taker_no_and_strictly_lower_price():
    result = c1.simulate_fill(
        order(),
        [
            trade(1_001, 3_999, 10_000, "yes"),  # wrong taker direction
            trade(1_002, 4_001, 10_000, "no"),   # did not reach our bid
            trade(1_003, 3_999, 4_000, "no"),    # valid strict-through
        ],
        c1.FillTrack.STRICT_THROUGH,
    )
    assert result.filled_e4 == 4_000
    assert [event.trade_index for event in result.fill_events] == [2]
    assert result.fill_events[0].reason == "STRICT_THROUGH"


def test_queue_equal_to_touch_volume_consumes_ahead_but_does_not_fill():
    result = c1.simulate_fill(
        order(ahead=25_000),
        [trade(1_001, 4_000, 25_000)],
        c1.FillTrack.QUEUE_PESSIMISTIC,
    )
    assert result.filled_e4 == 0
    assert result.queue_ahead_remaining_e4 == 0
    assert result.fill_events == ()


def test_queue_excess_volume_only_partially_fills_order():
    result = c1.simulate_fill(
        order(ahead=25_000),
        [trade(1_001, 4_000, 29_000)],
        c1.FillTrack.QUEUE_PESSIMISTIC,
    )
    assert result.filled_e4 == 4_000
    assert result.remaining_e4 == 6_000
    assert result.fill_events[0].public_count_e4 == 29_000
    assert result.fill_events[0].queue_before_e4 == 25_000
    assert result.fill_events[0].queue_after_e4 == 0


def test_queue_accumulates_only_equal_matching_volume_then_fills_to_cap():
    result = c1.simulate_fill(
        order(ahead=20_000),
        [
            trade(1_001, 4_000, 7_000, "yes"),   # wrong side: no queue use
            trade(1_002, 4_001, 7_000, "no"),    # wrong price: no queue use
            trade(1_003, 4_000, 12_000, "no"),
            trade(1_004, 4_000, 13_000, "no"),   # 8k queue + 5k fill
            trade(1_005, 4_000, 8_000, "no"),    # only 5k remains
        ],
        c1.FillTrack.QUEUE_PESSIMISTIC,
    )
    assert result.filled_e4 == 10_000
    assert result.remaining_e4 == 0
    assert [event.fill_count_e4 for event in result.fill_events] == [5_000, 5_000]
    assert result.queue_ahead_remaining_e4 == 0


def test_queue_strict_through_fills_and_invalidates_former_ahead():
    result = c1.simulate_fill(
        order(ahead=1_000_000),
        [trade(1_001, 3_999, 3_000)],
        c1.FillTrack.QUEUE_PESSIMISTIC,
    )
    assert result.filled_e4 == 3_000
    assert result.queue_ahead_remaining_e4 == 0
    assert result.fill_events[0].queue_before_e4 == 1_000_000
    assert result.fill_events[0].queue_after_e4 == 0


def test_trade_at_or_before_activation_is_rejected_not_filled():
    result = c1.simulate_fill(
        order(activation=1_000),
        [
            trade(999, 3_999, 10_000),
            trade(1_000, 3_999, 10_000),
            trade(1_001, 3_999, 2_000),
        ],
        c1.FillTrack.STRICT_THROUGH,
    )
    assert result.rejected_nonfuture_count == 2
    assert result.filled_e4 == 2_000
    assert [event.timestamp_us for event in result.fill_events] == [1_001]


def test_out_of_order_tape_fails_closed():
    with pytest.raises(c1.FillKernelError, match="out of order"):
        c1.simulate_fill(
            order(),
            [trade(1_002, 3_999), trade(1_001, 3_999)],
            c1.FillTrack.STRICT_THROUGH,
        )


def test_complete_tape_is_validated_even_if_first_print_fills_order():
    # Bypass the frozen dataclass constructor to model a corrupted downstream
    # object and prove a later bad row cannot hide behind an early full fill.
    malformed = object.__new__(c1.PublicTrade)
    object.__setattr__(malformed, "timestamp_us", 1_002)
    object.__setattr__(malformed, "yes_price_e4", 3_999)
    object.__setattr__(malformed, "count_e4", -1)
    object.__setattr__(malformed, "taker_side", "no")
    with pytest.raises(c1.FillKernelError):
        c1.simulate_fill(
            order(), [trade(1_001, 3_999), malformed],
            c1.FillTrack.STRICT_THROUGH,
        )


def test_unknown_order_and_trade_sides_fail_closed():
    with pytest.raises(c1.FillKernelError, match="order.side"):
        order(side="maybe")
    with pytest.raises(c1.FillKernelError, match="trade.taker_side"):
        trade(1_001, 4_000, side="MAYBE")


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: order(ahead=-1), "queue_ahead"),
        (lambda: order(quantity=-1), "quantity"),
        (lambda: order(price=-1), "price"),
        (lambda: trade(1_001, 4_000, -1), "count"),
        (lambda: trade(-1, 4_000), "timestamp"),
        (lambda: trade(1_001, -1), "price"),
    ],
)
def test_negative_fixed_point_or_time_values_fail_closed(factory, message):
    with pytest.raises(c1.FillKernelError, match=message):
        factory()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: order(price=4_000.0),
        lambda: order(quantity=True),
        lambda: trade(1_001, 4_000, "10000"),
        lambda: trade(1_001, 4_000.0),
    ],
)
def test_non_integer_fixed_point_values_fail_closed(factory):
    with pytest.raises(c1.FillKernelError, match="plain integer"):
        factory()


def test_no_equal_price_never_fills_strict_through():
    # Passive NO 6000 is touch-equivalent to YES 4000.
    result = c1.simulate_fill(
        order(side="no", price=6_000),
        [trade(1_001, 4_000, 50_000, "yes")],
        c1.FillTrack.STRICT_THROUGH,
    )
    assert result.filled_e4 == 0
    assert result.matching_touch_trade_count == 1


def test_no_requires_taker_yes_and_strictly_higher_yes_price():
    result = c1.simulate_fill(
        order(side="no", price=6_000),
        [
            trade(1_001, 4_001, 10_000, "no"),   # wrong taker direction
            trade(1_002, 3_999, 10_000, "yes"),  # did not reach our NO bid
            trade(1_003, 4_001, 6_000, "yes"),   # valid strict-through
        ],
        c1.FillTrack.STRICT_THROUGH,
    )
    assert result.filled_e4 == 6_000
    assert [event.trade_index for event in result.fill_events] == [2]


def test_no_queue_uses_complementary_touch_price_and_direction():
    result = c1.simulate_fill(
        order(side="no", price=6_000, ahead=5_000),
        [trade(1_001, 4_000, 9_000, "yes")],
        c1.FillTrack.QUEUE_PESSIMISTIC,
    )
    assert result.filled_e4 == 4_000
    assert result.queue_ahead_remaining_e4 == 0


@pytest.mark.parametrize(
    "track",
    [
        c1.FillTrack.STRICT_THROUGH,
        c1.FillTrack.QUEUE_PESSIMISTIC,
        c1.FillTrack.OPTIMISTIC_AT_TOUCH,
    ],
)
def test_each_fill_event_is_capped_by_that_public_trade_volume(track):
    result = c1.simulate_fill(
        order(ahead=0),
        [trade(1_001, 3_999, 2_500), trade(1_002, 3_998, 3_000)],
        track,
    )
    assert result.filled_e4 == 5_500
    assert [event.fill_count_e4 for event in result.fill_events] == [2_500, 3_000]
    assert all(
        event.fill_count_e4 <= event.public_count_e4
        for event in result.fill_events
    )


def test_optimistic_at_touch_is_explicitly_not_gate_eligible():
    result = c1.simulate_fill(
        order(ahead=1_000_000),
        [trade(1_001, 4_000, 7_000)],
        c1.FillTrack.OPTIMISTIC_AT_TOUCH,
    )
    assert result.filled_e4 == 7_000
    assert result.queue_ahead_remaining_e4 is None
    assert result.eligible_for_promotion_gate is False
    assert result.fill_events[0].reason == "OPTIMISTIC_AT_TOUCH"


def test_only_binding_strict_track_is_gate_eligible():
    assert c1.FillTrack.STRICT_THROUGH.eligible_for_promotion_gate is True
    assert c1.FillTrack.QUEUE_PESSIMISTIC.eligible_for_promotion_gate is False
    assert c1.FillTrack.OPTIMISTIC_AT_TOUCH.eligible_for_promotion_gate is False


def test_simulate_all_tracks_has_frozen_order_and_expected_bounds():
    results = c1.simulate_all_tracks(
        order(ahead=5_000), [trade(1_001, 4_000, 7_000)]
    )
    assert [result.track for result in results] == list(c1.FillTrack)
    assert [result.filled_e4 for result in results] == [0, 2_000, 7_000]


def test_results_are_frozen_and_byte_for_byte_deterministic_by_value():
    candidate_order = order(ahead=5_000)
    tape = [trade(1_001, 4_000, 7_000), trade(1_001, 3_999, 2_000)]
    first = c1.simulate_all_tracks(candidate_order, tape)
    second = c1.simulate_all_tracks(candidate_order, list(tape))
    assert first == second
    with pytest.raises(dataclasses.FrozenInstanceError):
        first[0].filled_e4 = 1  # type: ignore[misc]


def test_unknown_track_fails_closed():
    with pytest.raises(c1.FillKernelError, match="unknown fill track"):
        c1.simulate_fill(order(), [], "QUEUE_OPTIMISTIC")
