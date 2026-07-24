"""Adversarial tests for the shared PnL spine fill authorities."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.research.pnl_spine.fills import (  # noqa: E402
    FillError,
    L2Level,
    L2Snapshot,
    MarketableOrder,
    OrderAction,
    OutcomeSide,
    PassiveOrder,
    PublicTrade,
    allocate_paired_passive_strict_fills,
    allocate_passive_strict_fills,
    assert_portfolio_fill_conservation,
    walk_exact_l2_ioc,
)


def passive(
    order_id: str,
    *,
    side: OutcomeSide = OutcomeSide.YES,
    price: int = 4_000,
    activation: int = 1_000,
    cancel: int = 2_000,
    quantity: int = 10_000,
) -> PassiveOrder:
    return PassiveOrder(
        order_id=order_id,
        experiment_id="A01",
        root_id="root-1",
        market_ticker="MKT",
        side=side,
        price_e4=price,
        quantity_e4=quantity,
        activation_us=activation,
        cancel_effective_us=cancel,
    )


def trade(
    trade_id: str,
    timestamp: int,
    price: int,
    quantity: int = 10_000,
    side: OutcomeSide = OutcomeSide.NO,
) -> PublicTrade:
    return PublicTrade(
        trade_id=trade_id,
        market_ticker="MKT",
        timestamp_us=timestamp,
        yes_price_e4=price,
        quantity_e4=quantity,
        taker_side=side,
    )


def test_at_touch_and_activation_time_never_fill_passive_strict():
    batch = allocate_passive_strict_fills(
        [passive("o1")],
        [
            trade("t0", 1_000, 3_999),
            trade("t1", 1_001, 4_000),
        ],
    )
    assert batch.filled_e4 == 0
    assert batch.unfilled_e4 == 10_000


def test_cancel_effective_same_time_is_adverse_and_may_fill():
    batch = allocate_passive_strict_fills(
        [passive("o1")],
        [trade("t1", 2_000, 3_999, 4_000)],
    )
    assert batch.filled_e4 == 4_000
    assert batch.fills[0].timestamp_us == 2_000


def test_public_trade_is_never_reused_across_overlapping_orders():
    batch = allocate_passive_strict_fills(
        [passive("o1"), passive("o2")],
        [trade("t1", 1_500, 3_999, 50_000)],
    )
    assert batch.filled_e4 == 10_000
    assert {row.order_id for row in batch.fills} == {"o1"}
    assert batch.duplicate_source_allocations == 0


def test_paired_same_microsecond_legs_both_fill_before_cancel_wins():
    result = allocate_paired_passive_strict_fills(
        [
            passive("yes", side=OutcomeSide.YES, price=4_000),
            passive("no", side=OutcomeSide.NO, price=5_500),
        ],
        [
            trade(
                "a-yes",
                1_500,
                3_999,
                side=OutcomeSide.NO,
            ),
            trade(
                "b-no",
                1_500,
                4_501,
                side=OutcomeSide.YES,
            ),
        ],
        group_id="decision-1",
        cancel_latency_ns=1,
    )

    assert result.first_fill_us == 1_500
    assert result.cancel_effective_us == 1_501
    assert result.batch.filled_e4 == 20_000
    assert {fill.order_id for fill in result.batch.fills} == {"yes", "no"}
    assert dict(result.canceled_quantity_by_order_e4) == {
        "no": 0,
        "yes": 0,
    }


def test_paired_partial_first_fill_counts_cancel_boundary_race_and_cancels_rest():
    result = allocate_paired_passive_strict_fills(
        [
            passive("yes", side=OutcomeSide.YES, price=4_000),
            passive("no", side=OutcomeSide.NO, price=5_500),
        ],
        [
            trade("first", 1_500, 3_999, 3_000, OutcomeSide.NO),
            trade("boundary", 1_501, 3_998, 2_000, OutcomeSide.NO),
            trade("too-late", 1_502, 3_997, 10_000, OutcomeSide.NO),
        ],
        group_id="decision-1",
        cancel_latency_ns=1,
    )

    assert [fill.source_id for fill in result.batch.fills] == [
        "first",
        "boundary",
    ]
    assert result.batch.filled_e4 == 5_000
    assert result.batch.unfilled_e4 == 15_000
    assert dict(result.canceled_quantity_by_order_e4) == {
        "no": 10_000,
        "yes": 5_000,
    }


def test_duplicate_trade_id_fails_closed():
    with pytest.raises(FillError, match="duplicate trade_id"):
        allocate_passive_strict_fills(
            [passive("o1")],
            [trade("t1", 1_100, 3_999), trade("t1", 1_200, 3_998)],
        )


def test_noncanonical_trade_tape_fails_instead_of_silent_sort():
    with pytest.raises(FillError, match="canonically ordered"):
        allocate_passive_strict_fills(
            [passive("o1")],
            [trade("t2", 1_200, 3_999), trade("t1", 1_100, 3_998)],
        )


def test_no_side_strict_direction_and_complement_are_correct():
    batch = allocate_passive_strict_fills(
        [passive("o1", side=OutcomeSide.NO, price=6_000)],
        [
            trade("t1", 1_100, 4_000, side=OutcomeSide.YES),
            trade("t2", 1_200, 4_001, 6_000, side=OutcomeSide.YES),
        ],
    )
    assert batch.filled_e4 == 6_000
    assert batch.fills[0].source_id == "t2"
    assert batch.fills[0].price_e4 == 6_000


def snapshot() -> L2Snapshot:
    return L2Snapshot(
        snapshot_id="s1",
        market_ticker="MKT",
        receive_timestamp_us=10_000,
        yes_bids=(
            L2Level(4_900, 7_000),
            L2Level(4_800, 20_000),
        ),
        yes_asks=(
            L2Level(5_100, 4_000),
            L2Level(5_200, 8_000),
            L2Level(5_300, 20_000),
        ),
    )


def marketable(
    *,
    side: OutcomeSide = OutcomeSide.YES,
    action: OrderAction = OrderAction.BUY,
    limit: int = 5_200,
    quantity: int = 10_000,
) -> MarketableOrder:
    return MarketableOrder(
        order_id="ioc-1",
        experiment_id="B09",
        root_id="root-1",
        market_ticker="MKT",
        side=side,
        action=action,
        limit_price_e4=limit,
        quantity_e4=quantity,
        effective_timestamp_us=10_100,
    )


def test_buy_yes_ioc_walks_asks_and_respects_partial_level():
    batch = walk_exact_l2_ioc(
        marketable(), snapshot(), maximum_snapshot_age_us=250
    )
    assert [row.price_e4 for row in batch.fills] == [5_100, 5_200]
    assert [row.quantity_e4 for row in batch.fills] == [4_000, 6_000]
    assert batch.filled_e4 == 10_000
    assert batch.unfilled_e4 == 0


def test_buy_no_ioc_walks_yes_bids_using_complement_price():
    batch = walk_exact_l2_ioc(
        marketable(side=OutcomeSide.NO, limit=5_200),
        snapshot(),
        maximum_snapshot_age_us=250,
    )
    assert [row.price_e4 for row in batch.fills] == [5_100, 5_200]
    assert [row.quantity_e4 for row in batch.fills] == [7_000, 3_000]


def test_sell_yes_exit_walks_bids_and_limit_censors_suffix():
    batch = walk_exact_l2_ioc(
        marketable(action=OrderAction.SELL, limit=4_900, quantity=10_000),
        snapshot(),
        maximum_snapshot_age_us=250,
    )
    assert batch.filled_e4 == 7_000
    assert batch.unfilled_e4 == 3_000
    assert [row.price_e4 for row in batch.fills] == [4_900]


def test_future_stale_invalid_or_crossed_snapshot_fails_closed():
    with pytest.raises(FillError, match="future"):
        walk_exact_l2_ioc(
            marketable(),
            L2Snapshot(
                "future",
                "MKT",
                10_101,
                (L2Level(4_900, 10_000),),
                (L2Level(5_100, 10_000),),
            ),
            maximum_snapshot_age_us=250,
        )
    with pytest.raises(FillError, match="stale"):
        walk_exact_l2_ioc(
            marketable(), snapshot(), maximum_snapshot_age_us=50
        )
    with pytest.raises(FillError, match="invalid"):
        walk_exact_l2_ioc(
            marketable(),
            L2Snapshot(
                "bad",
                "MKT",
                10_000,
                (L2Level(4_900, 10_000),),
                (L2Level(5_100, 10_000),),
                book_valid=False,
            ),
            maximum_snapshot_age_us=250,
        )
    with pytest.raises(FillError, match="locked or crossed"):
        L2Snapshot(
            "crossed",
            "MKT",
            10_000,
            (L2Level(5_100, 10_000),),
            (L2Level(5_000, 10_000),),
        )


def test_portfolio_conservation_rejects_cross_batch_source_reuse():
    first = allocate_passive_strict_fills(
        [passive("o1")], [trade("t1", 1_500, 3_999, 10_000)]
    )
    second = allocate_passive_strict_fills(
        [passive("o2")], [trade("t1", 1_500, 3_999, 10_000)]
    )
    with pytest.raises(FillError, match="portfolio fills exceed"):
        assert_portfolio_fill_conservation(
            [first, second], authoritative_source_quantity={"t1": 10_000}
        )
