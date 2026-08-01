"""Behavioural contract for the shadow-only paired prequote experiment.

The experiment admits a quote *cycle*, not two unrelated directional bets:
both best-bid touches must be deep enough, both orders must fit as one capital
bundle, and their combined cost must not exceed the configured pair ceiling.
Once one full leg fills, an already-resting safe counterpart keeps its queue
priority.  This module deliberately exercises ``think()`` rather than binding
tests to the implementation shape of the admission helper.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path
import sys
from unittest import mock

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_mm_engine_guards import (  # noqa: E402
    E,
    reset as _base_reset,
    seed_timed_rti,
)


MARKET = "MKT"
SERIES = "KXBTC15M"
CLIP = 2.0
MIN_DEPTH = 5.0


def fresh() -> None:
    """Reset the engine singleton and pin this suite's small-risk defaults."""
    _base_reset()
    E.S.unpaired.clear()
    E.S.pending_new.clear()
    E.S.unknown_orders.clear()
    E.S.flatten_pending.clear()
    E.S.last_entry_source_ms.clear()
    E.S.tokens = 8.0
    E.S.tok_t = E.time.time()


def arm_market(
    *,
    y_bid_e4: int = 4500,
    n_bid_e4: int = 5300,
    y_depth: float = MIN_DEPTH,
    n_depth: float = MIN_DEPTH,
    tte_s: float = 700.0,
    pricing_ready: bool = True,
) -> None:
    """Install a fresh, continuous pricing tape and one binary market."""
    now = E.time.time()
    ticks = [
        65_000.0 + (3.0 if i % 2 else -3.0)
        for i in range(301)
    ]
    seed_timed_rti(SERIES, ticks, now=now)
    E.S.meta[MARKET] = (SERIES, now + tte_s, 65_000.0)
    E.S.books[MARKET] = {
        "y": {y_bid_e4: float(y_depth)},
        "n": {n_bid_e4: float(n_depth)},
    }
    if not pricing_ready:
        E.S.cf_stream_ok = False


@contextmanager
def pair_prequote(
    *,
    enabled: bool,
    fair_c: float,
    max_open_cost: float = 8.0,
):
    """Pin the public experiment controls while allowing helpers to evolve."""
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(E, "MODE", "shadow"))
        stack.enter_context(mock.patch.object(E, "PAIR", True))
        stack.enter_context(
            mock.patch.object(E, "PAIR_PREQUOTE", enabled, create=True)
        )
        stack.enter_context(
            mock.patch.object(
                E, "PAIR_MIN_DEPTH_CT", MIN_DEPTH, create=True
            )
        )
        stack.enter_context(mock.patch.object(E, "PAIR_LOCK_C", 99.0))
        stack.enter_context(mock.patch.object(E, "CLIP", f"{CLIP:.2f}"))
        stack.enter_context(mock.patch.object(E, "UNPAIRED_MAX_CT", CLIP))
        stack.enter_context(mock.patch.object(E, "MAX_NET", int(CLIP)))
        stack.enter_context(
            mock.patch.object(E, "MAX_OPEN_COST", max_open_cost)
        )
        stack.enter_context(
            mock.patch.object(
                E.rp, "p_settle_above", return_value=fair_c / 100.0
            )
        )
        stack.enter_context(
            mock.patch.object(E, "load_control", return_value=False)
        )
        yield


def canonical_fill(
    *,
    fill_id: str,
    order_id: str,
    outcome: str,
    outcome_price: float,
    count: float = CLIP,
) -> dict:
    """Construct the strict REST-like fill shape consumed by ``apply_fill``."""
    yes_price = (
        outcome_price if outcome == "yes" else 1.0 - outcome_price
    )
    no_price = 1.0 - yes_price
    return {
        "fill_id": fill_id,
        "order_id": order_id,
        "ticker": MARKET,
        "outcome_side": outcome,
        "book_side": "bid" if outcome == "yes" else "ask",
        "side": outcome,
        "count_fp": f"{count:.2f}",
        "yes_price_dollars": f"{yes_price:.4f}",
        "no_price_dollars": f"{no_price:.4f}",
        "created_ts": 1_775_000_000,
    }


def assert_neither_side_is_resting() -> None:
    assert (MARKET, "bid") not in E.S.orders
    assert (MARKET, "ask_no") not in E.S.orders


def test_prequote_default_off_preserves_legacy_single_edge_selection():
    """PAIR alone must not silently opt a deployment into the experiment."""
    fresh()
    arm_market()

    # Fair YES=55: YES@45 has +10c edge while NO@53 has -8c edge.
    with pair_prequote(enabled=False, fair_c=55.0):
        E.think()

    assert (MARKET, "bid") in E.S.orders
    assert (MARKET, "ask_no") not in E.S.orders


def test_prequote_live_promotion_is_fail_closed():
    """The experimental policy cannot become live through one env typo."""
    fresh()
    with (
        mock.patch.object(E, "PAIR", True),
        mock.patch.object(E, "PAIR_PREQUOTE", True),
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "CLIP", "1.00"),
        mock.patch.object(E, "UNPAIRED_MAX_CT", 1.0),
    ):
        errors = E.strategy_config_errors()
    assert any("shadow-only" in error for error in errors)


@pytest.mark.parametrize(
    ("y_bid_e4", "n_bid_e4"),
    [
        pytest.param(4500, 5300, id="pair-sum-98c"),
        pytest.param(4600, 5200, id="pair-sum-98c-mid"),
    ],
)
def test_shadow_prequote_admits_both_sides_despite_one_negative_edge(
    y_bid_e4,
    n_bid_e4,
):
    """A passing pair cycle posts both legs; edge is not a side selector."""
    fresh()
    arm_market(y_bid_e4=y_bid_e4, n_bid_e4=n_bid_e4)

    # Fair YES=55 makes the NO quote decisively model-negative in both cases.
    with pair_prequote(enabled=True, fair_c=55.0):
        E.think()

    assert (MARKET, "bid") in E.S.orders
    assert (MARKET, "ask_no") in E.S.orders
    quote_sum = (
        E.S.orders[(MARKET, "bid")]["px"]
        + E.S.orders[(MARKET, "ask_no")]["px"]
    )
    assert quote_sum <= 0.99 + 1e-12


def test_pair_net_edge_buffer_rejects_99c_bundle():
    """A 99c gross pair is too thin once capital lock is considered."""
    fresh()
    arm_market(y_bid_e4=4600, n_bid_e4=5300)
    events = []
    with (
        pair_prequote(enabled=True, fair_c=55.0),
        mock.patch.object(E.L, "w", side_effect=events.append),
    ):
        E.think()

    assert_neither_side_is_resting()
    assert any(
        event.get("ev") == "QUOTE_EVAL"
        and event.get("cycle_gate_reason") == "pair_sum"
        for event in events
    )


@pytest.mark.parametrize(
    "failed_gate",
    [
        "pair_sum",
        "yes_depth",
        "no_depth",
        "bundle_capital",
        "zone",
        "pricing",
    ],
)
def test_any_cycle_gate_failure_posts_neither_side(failed_gate):
    """Admission is all-or-none: never leave the first sequential POST live."""
    fresh()
    kwargs = {}
    fair_c = 55.0
    max_open_cost = 8.0

    if failed_gate == "pair_sum":
        # A valid tail-band book with 0.2c spread.  q_px improves each touch
        # by 0.1c, producing 95.1c + 4.9c = 100c, above the 99c ceiling.
        kwargs.update(y_bid_e4=9500, n_bid_e4=480)
        fair_c = 97.0
    elif failed_gate == "yes_depth":
        kwargs["y_depth"] = MIN_DEPTH - 1.0
    elif failed_gate == "no_depth":
        kwargs["n_depth"] = MIN_DEPTH - 1.0
    elif failed_gate == "bundle_capital":
        # YES alone costs $0.90 and fits; the 98c two-leg bundle costs $1.96.
        # A sequential per-order check would incorrectly leave only YES live.
        max_open_cost = 0.95
    elif failed_gate == "zone":
        kwargs["tte_s"] = 100.0
    elif failed_gate == "pricing":
        kwargs["pricing_ready"] = False

    arm_market(**kwargs)
    with pair_prequote(
        enabled=True,
        fair_c=fair_c,
        max_open_cost=max_open_cost,
    ):
        E.think()

    assert_neither_side_is_resting()


def test_second_leg_send_failure_immediately_aborts_first_shadow_leg():
    """A passing bundle gate is not an atomic exchange operation.

    If the second sequential send cannot happen, the first order must be
    drained in the same decision instead of becoming an accidental
    directional quote.
    """
    fresh()
    arm_market()
    E.S.tokens = 1.0
    events = []
    with (
        pair_prequote(enabled=True, fair_c=55.0),
        mock.patch.object(E.L, "w", side_effect=events.append),
    ):
        E.think()

    assert_neither_side_is_resting()
    assert any(event.get("ev") == "RATE_SKIP" for event in events)
    assert any(event.get("ev") == "PAIR_BUNDLE_ABORT" for event in events)


def test_shadow_queue_sim_closes_a_two_leg_cycle_from_public_trades():
    """The 24h shadow produces fill/PnL evidence without touching money."""
    fresh()
    arm_market(y_bid_e4=4500, n_bid_e4=5300)
    events = []
    with (
        pair_prequote(enabled=True, fair_c=55.0),
        mock.patch.object(E, "SHADOW_QUEUE_SIM", True),
        mock.patch.object(E.L, "w", side_effect=events.append),
    ):
        E.think()
        yes_order = E.S.orders[(MARKET, "bid")]
        no_order = E.S.orders[(MARKET, "ask_no")]
        assert yes_order["ahead"] == pytest.approx(MIN_DEPTH)
        assert no_order["ahead"] == pytest.approx(MIN_DEPTH)

        # Same-price volume consumes the displayed queue plus our 2ct clip.
        assert E.apply_public_trade({
            "market_ticker": MARKET,
            "trade_id": "public-no-taker",
            "ts_ms": 1_785_000_001_000,
            "count_fp": "7.10",
            "yes_price_dollars": "0.4500",
            "taker_outcome_side": "no",
            "taker_book_side": "ask",
        })
        assert (MARKET, "bid") not in E.S.orders
        assert E.unpaired_ct(MARKET, "bid") == pytest.approx(CLIP)

        # The already-aged NO maker leg then fills at its original 53c.
        assert E.apply_public_trade({
            "market_ticker": MARKET,
            "trade_id": "public-yes-taker",
            "ts_ms": 1_785_000_002_000,
            "count_fp": "7.10",
            "yes_price_dollars": "0.4700",
            "taker_outcome_side": "yes",
            "taker_book_side": "bid",
        })

    assert_neither_side_is_resting()
    assert E.unpaired_ct(MARKET, "bid") == pytest.approx(0.0)
    assert E.unpaired_ct(MARKET, "ask_no") == pytest.approx(0.0)
    assert E.S.realized == pytest.approx((1.0 - 0.45 - 0.53) * CLIP)
    assert len([event for event in events
                if event.get("ev") == "SHADOW_FILL_SIM"]) == 2
    assert any(event.get("ev") == "PAIR_CYCLE_DONE" for event in events)


def test_shadow_queue_sim_books_an_orphan_ioc_and_taker_fee():
    """A failed pair must appear as a loss, not a permanently pending shadow."""
    fresh()
    events = []
    with (
        mock.patch.object(E, "MODE", "shadow"),
        mock.patch.object(E, "PAIR", True),
        mock.patch.object(E, "SHADOW_QUEUE_SIM", True),
        mock.patch.object(E.L, "w", side_effect=events.append),
    ):
        E.apply_fill(canonical_fill(
            fill_id="orphan-yes",
            order_id="shadow-entry",
            outcome="yes",
            outcome_price=0.30,
            count=1.0,
        ))
        assert E.order_taker(
            MARKET, "ask", 0.28, 1.0, reason="test_orphan")

    # General taker fee is rounded upward so fee + position cost lands on a
    # centicent ($0.0001), not left at the unrounded quadratic value.
    fee = 0.0142
    assert E.S.realized == pytest.approx(1.0 - 0.30 - 0.72 - fee)
    assert E.unpaired_ct(MARKET, "bid") == pytest.approx(0.0)
    assert (MARKET, "bid") not in E.S.flatten_pending
    assert any(event.get("ev") == "SHADOW_TAKER_FILL_SIM"
               for event in events)


def test_taker_fee_rounds_total_cost_up_to_centicent():
    assert E.taker_fee_usd(0.927, 1.0) == pytest.approx(0.0048)
    assert E.taker_fee_usd(0.72, 1.0) == pytest.approx(0.0142)


def test_pair_wait_metric_preserves_subsecond_fill_precision():
    """The dynamic completion model must not train on integer-second waits."""
    fresh()
    events = []
    first = canonical_fill(
        fill_id="wait-yes",
        order_id="wait-yes-order",
        outcome="yes",
        outcome_price=0.30,
        count=1.0,
    )
    first["created_ts"] = 1_775_000_000_100
    second = canonical_fill(
        fill_id="wait-no",
        order_id="wait-no-order",
        outcome="no",
        outcome_price=0.69,
        count=1.0,
    )
    second["created_ts"] = 1_775_000_001_058

    with (
        mock.patch.object(E, "PAIR", True),
        mock.patch.object(E.L, "w", side_effect=events.append),
    ):
        E.apply_fill(first)
        E.apply_fill(second)

    pair_lock = next(event for event in events
                     if event.get("ev") == "PAIR_LOCK")
    assert pair_lock["pair_wait_s"] == pytest.approx(0.958)


def test_unresolved_ioc_blocks_replacement_maker_counterpart():
    """Maker and IOC hedges must never coexist for the same orphan."""
    fresh()
    arm_market(y_bid_e4=3000, n_bid_e4=6500)
    with (
        pair_prequote(enabled=True, fair_c=35.0),
        mock.patch.object(E, "SHADOW_QUEUE_SIM", False),
    ):
        E.apply_fill(canonical_fill(
            fill_id="orphan-yes",
            order_id="already-filled-entry",
            outcome="yes",
            outcome_price=0.30,
            count=1.0,
        ))
        assert E.order_taker(
            MARKET, "ask", 0.29, 1.0, reason="test_pending")
        assert (MARKET, "bid") in E.S.flatten_pending

        E.think()

    assert (MARKET, "ask_no") not in E.S.orders
    assert (MARKET, "bid") not in E.S.orders


@pytest.mark.parametrize(
    ("filled_side", "outcome", "fill_px", "counterpart_side"),
    [
        pytest.param("bid", "yes", 0.30, "ask_no", id="yes-fills-first"),
        pytest.param("ask_no", "no", 0.65, "bid", id="no-fills-first"),
    ],
)
def test_full_first_fill_keeps_safe_counterpart_id_price_and_queue_age(
    filled_side,
    outcome,
    fill_px,
    counterpart_side,
):
    """Do not replace a resting counterpart that already locks <=99c."""
    fresh()
    arm_market(y_bid_e4=3000, n_bid_e4=6500)

    with pair_prequote(enabled=True, fair_c=35.0):
        E.think()
        assert (MARKET, "bid") in E.S.orders
        assert (MARKET, "ask_no") in E.S.orders

        filled = E.S.orders[(MARKET, filled_side)]
        counterpart = E.S.orders[(MARKET, counterpart_side)]
        counterpart["t"] = 123.0
        before = {
            "id": counterpart["id"],
            "px": counterpart["px"],
            "qty": counterpart["qty"],
            "t": counterpart["t"],
        }

        E.apply_fill(
            canonical_fill(
                fill_id=f"fill-{outcome}",
                order_id=filled["id"],
                outcome=outcome,
                outcome_price=fill_px,
            )
        )
        E.think()

    after = E.S.orders[(MARKET, counterpart_side)]
    assert after["id"] == before["id"]
    assert after["px"] == before["px"]
    assert after["qty"] == before["qty"]
    assert after["t"] == before["t"]
    assert fill_px + after["px"] <= 0.99 + 1e-12
    assert (MARKET, filled_side) not in E.S.orders


@pytest.mark.parametrize(
    ("unsafe_px", "unsafe_qty"),
    [
        pytest.param(0.70, CLIP, id="counterpart-breaks-pair-ceiling"),
        pytest.param(0.65, 1.0, id="counterpart-quantity-mismatch"),
    ],
)
def test_unsafe_or_wrong_sized_counterpart_is_replaced(
    unsafe_px,
    unsafe_qty,
):
    """Queue age yields to the pair ceiling and exact residual sizing."""
    fresh()
    arm_market(y_bid_e4=3000, n_bid_e4=6500)

    with pair_prequote(enabled=True, fair_c=35.0):
        E.think()
        yes_order = E.S.orders[(MARKET, "bid")]
        no_order = E.S.orders[(MARKET, "ask_no")]
        old_id = no_order["id"]
        no_order.update(px=unsafe_px, qty=unsafe_qty, t=123.0)

        E.apply_fill(
            canonical_fill(
                fill_id="fill-yes",
                order_id=yes_order["id"],
                outcome="yes",
                outcome_price=0.30,
            )
        )
        E.think()

    replacement = E.S.orders[(MARKET, "ask_no")]
    assert replacement["id"] != old_id
    assert replacement["t"] != 123.0
    assert replacement["qty"] == pytest.approx(CLIP)
    assert 0.30 + replacement["px"] <= 0.99 + 1e-12
