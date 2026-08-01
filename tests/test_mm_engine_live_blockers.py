"""Fail-closed regression gates for the next crypto-MM live canary.

These tests deliberately specify *behaviour*, not an implementation shape.
They exercise the current public engine paths and are expected to remain red
until the corresponding ledger/execution blockers are fixed.  No test in this
file performs network I/O.
"""
from __future__ import annotations

import math
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


TS = 1_770_000_000


def fresh() -> None:
    """Reset mutable singleton state not fully covered by the legacy helper."""
    _base_reset()
    E.S.unpaired.clear()
    E.S.flatten_sent.clear()
    if hasattr(E.S, "flatten_pending"):
        E.S.flatten_pending.clear()
    if hasattr(E.S, "unknown_orders"):
        E.S.unknown_orders.clear()
    E.S.fills_cursor = TS
    E.S.session_start_ts = TS - 60
    E.S.tokens = 8.0
    E.S.tok_t = E.time.time()


def fill(
    fill_id: str,
    outcome: str,
    price: float,
    *,
    count: float = 1.0,
    created_ts: int = TS,
    fee: float | None = None,
) -> dict:
    """Build a canonical REST-like fill; ``fee`` is in dollars."""
    yes = price if outcome == "yes" else round(1.0 - price, 4)
    no = round(1.0 - yes, 4)
    record = {
        "fill_id": fill_id,
        "ticker": "MKT",
        "outcome_side": outcome,
        "book_side": "bid" if outcome == "yes" else "ask",
        "side": outcome,
        "count_fp": f"{count:.2f}",
        "yes_price_dollars": f"{yes:.4f}",
        "no_price_dollars": f"{no:.4f}",
        "created_ts": created_ts,
    }
    if fee is not None:
        record["fee_cost"] = f"{fee:.6f}"
    return record


def arm_quote_market() -> None:
    """Create a stable quoteable market with kernel fair near 55 cents."""
    now = E.time.time()
    series = "KXBTC15M"
    ticks = [
        65_000.0 + (3.0 if i % 2 else -3.0) for i in range(301)
    ]
    seed_timed_rti(series, ticks, now=now)
    tte = 700.0
    sigma = max(
        E.rp.sigma_from_timed_ticks(
            ticks, list(E.S.rti_src_ms[series]), window_s=60),
        E.rp.sigma_from_timed_ticks(
            ticks, list(E.S.rti_src_ms[series]), window_s=300),
    )
    inside = (
        E.rp.rw_avg_var_factor(E.rp.LOCK_TICKS - 1)
        / (E.rp.LOCK_TICKS**2)
    )
    sd = sigma * math.sqrt((tte - E.rp.LOCK_TICKS) + inside)
    strike = ticks[-1] - 0.12566 * sd
    E.S.meta["MKT"] = (series, now + tte, strike)
    E.S.books["MKT"] = {"y": {4500: 100.0}, "n": {5300: 100.0}}


def test_market_data_subscription_pins_legacy_dual_price_scale():
    """A future exchange default flip must not silently complement NO bids."""
    params = E.md_subscription_params(["MKT"])

    assert params == {
        "channels": ["orderbook_delta", "trade"],
        "market_tickers": ["MKT"],
        "use_yes_price": False,
    }


def test_conflicting_canonical_direction_halts_without_booking_inventory():
    """outcome_side=yes and book_side=ask cannot both describe one fill."""
    fresh()
    bad = fill("conflict-1", "yes", 0.40)
    bad["book_side"] = "ask"

    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "cancel_all") as cancel_all,
        mock.patch.object(E, "write_control_status"),
    ):
        E.apply_fill(bad)

    assert E.S.halted, "canonical direction conflict must fail closed"
    cancel_all.assert_called_once()
    assert "MKT" not in E.S.net_pos, "ambiguous fill must never enter ledger"


def test_fill_id_dedupes_replay_but_not_distinct_fills_in_same_second():
    """Two same-shaped fills in one second are distinct when fill_id differs."""
    fresh()
    first = fill("fill-a", "yes", 0.40)
    second = fill("fill-b", "yes", 0.40)

    E.apply_fill(first)
    E.apply_fill(second)
    E.apply_fill(dict(first))  # WS/REST replay of the first event

    assert E.S.net_pos["MKT"]["y"] == pytest.approx(2.0)
    assert E.S.net_pos["MKT"]["cost"] == pytest.approx(0.80)


def test_rest_backstop_does_not_skip_second_fill_with_same_timestamp():
    """Advancing min_ts to ts+1 must not hide later fills from that second."""
    fresh()
    first = fill("fill-a", "yes", 0.40)
    second = fill("fill-b", "yes", 0.40)
    E.apply_fill(first)

    requested = []

    def fake_rest(method, path, body=None, host=None):
        requested.append(path)
        # Model inclusive min_ts semantics: ts+1 has skipped the second fill.
        if f"min_ts={TS + 1}" in path:
            return 200, {"fills": []}
        return 200, {"fills": [second]}

    with mock.patch.object(E, "rest", fake_rest):
        E.poll_fills_once()

    assert requested
    assert E.S.net_pos["MKT"]["y"] == pytest.approx(2.0)


def test_cancel_404_is_unknown_and_must_not_release_order_slot():
    """A 404 is Not Found, not evidence that cancellation succeeded."""
    fresh()
    key = ("MKT", "bid")
    E.S.orders[key] = {
        "id": "order-404",
        "px": 0.40,
        "qty": 1.0,
        "t": E.time.time(),
    }

    def fake_rest(method, path, body=None, host=None):
        if method == "DELETE":
            return 404, {"error": "not found"}
        return 200, {"fills": []}

    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "rest", fake_rest),
    ):
        canceled = E.order_cancel("MKT", "bid", "order-404", "reprice")

    assert canceled is False, "caller must retain the reservation on UNKNOWN"
    assert key in E.S.orders


def test_cancel_404_terminal_status_without_fill_proof_stays_unknown():
    """Executed/canceled text cannot substitute for the delayed fill event."""
    fresh()
    key = ("MKT", "bid")
    E.S.orders[key] = {
        "id": "order-404-late-fill",
        "px": 0.40,
        "qty": 1.0,
        "t": E.time.time(),
    }

    def fake_rest(method, path, body=None, host=None):
        if method == "DELETE":
            return 404, {"error": "not found"}
        if "/portfolio/fills" in path:
            return 200, {"fills": []}
        return 200, {
            "order": {
                "order_id": "order-404-late-fill",
                "status": "executed",
                "fill_count_fp": "1.00",
                "remaining_count_fp": "0.00",
            }
        }

    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "rest", fake_rest),
        mock.patch.object(E, "write_control_status"),
    ):
        canceled = E.order_cancel(
            "MKT", "bid", "order-404-late-fill", "reprice"
        )

    assert canceled is False
    assert key in E.S.orders
    assert E.S.halted


def test_pair_profit_is_not_charged_again_when_flat_market_settles():
    """YES+NO pair realizes once; a flat settlement cannot debit its basis."""
    fresh()
    with mock.patch.object(E, "PAIR", True):
        E.apply_fill(fill("yes-1", "yes", 0.30, count=2.0))
        E.apply_fill(fill("no-1", "no", 0.65, count=2.0))

        assert E.S.realized == pytest.approx(0.10)
        E.apply_settlement(
            {
                "ticker": "MKT",
                "market_result": "yes",
                "revenue": 0,
                "yes_total_cost": 60,
                "no_total_cost": 130,
                "settled_time": "2026-02-02T00:01:00Z",
            }
        )

    assert E.S.realized == pytest.approx(0.10)


def test_pair_second_leg_is_blocked_when_it_would_exceed_lock_cap():
    """Risk reduction must not create unaffordable locked collateral."""
    fresh()
    arm_quote_market()
    E.CLIP = "2.00"
    E.MAX_OPEN_COST = 0.60

    with (
        mock.patch.object(E, "PAIR", True),
        mock.patch.object(E, "load_control", return_value=False),
    ):
        E.apply_fill(fill("yes-1", "yes", 0.30, count=2.0))
        assert E.exposure() == pytest.approx(E.MAX_OPEN_COST)
        E.think()

    assert ("MKT", "ask_no") not in E.S.orders
    assert E.unpaired_ct("MKT", "bid") == pytest.approx(2.0)


def test_partial_first_leg_quotes_only_the_unpaired_remainder():
    """A one-contract partial fill must not launch a two-contract hedge."""
    fresh()
    arm_quote_market()
    E.CLIP = "2.00"

    with (
        mock.patch.object(E, "PAIR", True),
        mock.patch.object(E, "load_control", return_value=False),
    ):
        E.apply_fill(fill("yes-1", "yes", 0.30, count=1.0))
        E.think()

    hedge = E.S.orders[("MKT", "ask_no")]
    assert hedge["qty"] == pytest.approx(1.0)


def test_ioc_risk_cut_uses_exchange_supported_wire_safety_fields():
    """Only IOC accepts reduce_only; STP must use Kalshi's taker_at_cross."""
    fresh()
    sent = []

    def fake_rest(method, path, body=None, host=None):
        sent.append(dict(body or {}))
        return 201, {"order_id": "ioc-1"}

    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "rest", fake_rest),
    ):
        assert E.order_taker(
            "MKT", "ask", 0.29, 1.0, reason="unpaired_age"
        )

    assert sent[0].get("reduce_only") is True
    assert sent[0].get("self_trade_prevention_type") == "taker_at_cross"


def test_unresolved_ioc_prevents_duplicate_exit_after_cooldown():
    """Elapsed cooldown is not terminal evidence; one IOC remains pending."""
    fresh()
    now = TS + 100

    def fake_rest(method, path, body=None, host=None):
        return 201, {"order_id": "ioc-pending"}

    with (
        mock.patch.object(E, "PAIR", True),
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "rest", fake_rest),
    ):
        E.apply_fill(fill("yes-1", "yes", 0.30, created_ts=TS))
        E.S.books["MKT"] = {"y": {3000: 100.0}, "n": {6500: 100.0}}
        E.flatten_lot_taker("MKT", "bid", now)
        due_again = E.flatten_due(now + 6.0)

    assert ("MKT", "bid") not in due_again


def test_pair_realized_profit_is_net_of_actual_fill_fees():
    """Pair economics use wire fees, including nonzero taker/maker charges."""
    fresh()
    with mock.patch.object(E, "PAIR", True):
        E.apply_fill(fill("yes-1", "yes", 0.30, fee=0.01))
        E.apply_fill(fill("no-1", "no", 0.65, fee=0.02))

    assert E.S.realized == pytest.approx(
        1.0 - 0.30 - 0.65 - 0.01 - 0.02
    )


@pytest.mark.parametrize(
    ("code", "response"),
    [
        pytest.param(-1, {"error": "timeout"}, id="transport-timeout"),
        pytest.param(503, {"error": "upstream unavailable"}, id="http-5xx"),
        pytest.param(201, {"order": {}}, id="success-without-order-id"),
    ],
)
def test_ambiguous_maker_post_halts_reserves_and_never_reposts(
    code, response
):
    """Unknown create outcome owns capital until exchange truth is proven.

    Retrying a POST after a timeout/5xx (or a nominal 2xx without an order
    identity) can create a duplicate live order.  The original intent must
    therefore remain reserved and the engine must halt new risk.
    """
    fresh()
    arm_quote_market()
    posts = []

    def fake_rest(method, path, body=None, host=None):
        if method == "POST":
            posts.append(dict(body or {}))
            return code, response
        return 500, {"error": "unexpected test request"}

    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "load_control", return_value=False),
        mock.patch.object(E, "rest", fake_rest),
        mock.patch.object(E, "write_control_status"),
    ):
        E.think()
        E.think()

    assert len(posts) == 1, "ambiguous create must never be blindly reposted"
    assert E.S.halted
    assert E.exposure() >= 0.90 - 1e-9, "two YES at 45c stay reserved"


def test_maker_429_is_definitive_reject_and_releases_provisional_reservation():
    """Unlike transport/5xx ambiguity, an explicit 429 created no order."""
    fresh()
    arm_quote_market()

    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "load_control", return_value=False),
        mock.patch.object(
            E, "rest", return_value=(429, {"error": "rate_limited"})
        ),
    ):
        E.think()

    assert not E.S.halted
    assert E.exposure() == pytest.approx(0.0)
    assert not E.S.orders


def test_gtc_risk_reducing_maker_omits_ioc_only_reduce_only_field():
    """Live evidence: Kalshi rejects reduce_only on a GTC maker with 400."""
    fresh()
    arm_quote_market()
    bodies = []

    def fake_rest(method, path, body=None, host=None):
        if method == "POST":
            bodies.append(dict(body or {}))
            return 201, {"order_id": "hedge-maker"}
        return 500, {"error": "unexpected test request"}

    with mock.patch.object(E, "PAIR", True):
        E.apply_fill(fill("yes-1", "yes", 0.30, fee=0.0))
        with (
            mock.patch.object(E, "MODE", "live"),
            mock.patch.object(E, "load_control", return_value=False),
            mock.patch.object(E, "rest", fake_rest),
        ):
            E.think()

    exit_bodies = [body for body in bodies if body.get("side") == "ask"]
    assert len(exit_bodies) == 1
    assert exit_bodies[0]["time_in_force"] == "good_till_canceled"
    assert "reduce_only" not in exit_bodies[0]


def test_cancel_2xx_zero_reduced_by_does_not_drop_racing_fill_reservation():
    """A 2xx with no canceled quantity can mean the order filled first."""
    fresh()
    key = ("MKT", "bid")
    E.S.orders[key] = {
        "id": "maker-race",
        "px": 0.40,
        "qty": 1.0,
        "t": E.time.time(),
    }
    raced_fill = fill("race-fill", "yes", 0.40, fee=0.0)
    raced_fill["order_id"] = "maker-race"

    def fake_rest(method, path, body=None, host=None):
        if method == "DELETE":
            return 200, {
                "order": {
                    "order_id": "maker-race",
                    "status": "executed",
                    "fill_count_fp": "1.00",
                    "remaining_count_fp": "0.00",
                },
                "reduced_by_fp": "0.00",
            }
        if "/portfolio/fills" in path:
            return 200, {"fills": [raced_fill]}
        return 200, {"order": {"status": "executed"}}

    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "rest", fake_rest),
    ):
        canceled = E.order_cancel("MKT", "bid", "maker-race", "reprice")
        # This is exactly what every current caller does on True.
        if canceled:
            E.S.orders.pop(key, None)

    assert E.exposure() == pytest.approx(
        0.40
    ), "the racing fill must be booked or its reservation retained"


def test_ioc_429_clears_pending_marker():
    """A definitive rate-limit reject must not deadlock the orphan forever."""
    fresh()
    key = ("MKT", "bid")

    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(
            E, "rest", return_value=(429, {"error": "rate_limited"})
        ),
    ):
        assert not E.order_taker(
            "MKT", "ask", 0.29, 1.0, reason="unpaired_age"
        )

    assert key not in E.S.flatten_pending


def test_zero_fill_terminal_ioc_can_retry_full_orphan():
    """A terminal zero-fill IOC releases pending without shrinking quantity."""
    fresh()
    posts = []

    def fake_rest(method, path, body=None, host=None):
        if method == "POST":
            posts.append(dict(body or {}))
            n = len(posts)
            return 201, {
                "order": {
                    "order_id": f"ioc-zero-{n}",
                    "status": "canceled",
                    "fill_count_fp": "0.00",
                    "remaining_count_fp": "0.00",
                }
            }
        return 500, {"error": "unexpected test request"}

    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "rest", fake_rest),
    ):
        assert E.order_taker(
            "MKT", "ask", 0.29, 1.0, reason="unpaired_age"
        )
        assert ("MKT", "bid") not in E.S.flatten_pending
        assert E.order_taker(
            "MKT", "ask", 0.29, 1.0, reason="unpaired_age"
        )

    assert [body["count"] for body in posts] == ["1.00", "1.00"]


def test_partial_terminal_ioc_retries_only_unfilled_remainder():
    """After one of two contracts fills, the next IOC may send only one."""
    fresh()
    posts = []

    def fake_rest(method, path, body=None, host=None):
        if method == "POST":
            posts.append(dict(body or {}))
            if len(posts) == 1:
                return 201, {
                    "order": {
                        "order_id": "ioc-partial",
                        "status": "canceled",
                        "initial_count_fp": "2.00",
                        "fill_count_fp": "1.00",
                        "remaining_count_fp": "0.00",
                    }
                }
            return 201, {
                "order": {
                    "order_id": "ioc-remainder",
                    "status": "canceled",
                    "fill_count_fp": "0.00",
                    "remaining_count_fp": "0.00",
                }
            }
        if "/portfolio/fills" in path:
            return 200, {"fills": []}
        return 200, {"order": {"status": "canceled"}}

    with mock.patch.object(E, "PAIR", True):
        E.apply_fill(fill("yes-2", "yes", 0.30, count=2.0, fee=0.0))
        E.S.books["MKT"] = {"y": {3000: 100.0}, "n": {6500: 100.0}}
        with (
            mock.patch.object(E, "MODE", "live"),
            mock.patch.object(E, "rest", fake_rest),
        ):
            E.flatten_lot_taker("MKT", "bid", TS + 100)
            partial = fill(
                "ioc-fill-1", "no", 0.71, count=1.0, fee=0.0
            )
            partial["order_id"] = "ioc-partial"
            E.apply_fill(partial)
            E.flatten_lot_taker("MKT", "bid", TS + 101)

    assert len(posts) == 2
    assert posts[0]["count"] == "2.00"
    assert posts[1]["count"] == "1.00"


def test_unknown_order_is_not_sent_a_second_cancel():
    """Once terminal state is UNKNOWN, repeated DELETE adds no information."""
    fresh()
    key = ("MKT", "bid")
    E.S.orders[key] = {
        "id": "unknown-1",
        "px": 0.40,
        "qty": 1.0,
        "t": E.time.time(),
    }
    deletes = []

    def fake_rest(method, path, body=None, host=None):
        if method == "DELETE":
            deletes.append(path)
            return 404, {"error": "not found"}
        if "/portfolio/fills" in path:
            return 200, {"fills": []}
        return 404, {"error": "not found"}

    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "rest", fake_rest),
        mock.patch.object(E, "write_control_status"),
    ):
        assert not E.order_cancel(
            "MKT", "bid", "unknown-1", "first_attempt"
        )
        assert not E.order_cancel(
            "MKT", "bid", "unknown-1", "second_attempt"
        )

    assert len(deletes) == 1
    assert key in E.S.orders


def test_aged_ioc_cancels_and_confirms_resting_hedge_before_crossing():
    """Maker hedge and IOC hedge must never coexist for the same orphan."""
    fresh()
    methods = []
    key = ("MKT", "ask_no")

    def fake_rest(method, path, body=None, host=None):
        methods.append(method)
        if method == "DELETE":
            return 200, {
                "order": {
                    "order_id": "resting-hedge",
                    "status": "canceled",
                    "remaining_count_fp": "0.00",
                },
                "reduced_by_fp": "1.00",
            }
        if method == "POST":
            return 201, {"order_id": "ioc-after-cancel"}
        return 200, {"fills": []}

    with mock.patch.object(E, "PAIR", True):
        E.apply_fill(fill("yes-1", "yes", 0.30, fee=0.0))
        E.S.books["MKT"] = {"y": {3000: 100.0}, "n": {6500: 100.0}}
        E.S.orders[key] = {
            "id": "resting-hedge",
            "px": 0.69,
            "qty": 1.0,
            "t": E.time.time(),
        }
        with (
            mock.patch.object(E, "MODE", "live"),
            mock.patch.object(E, "rest", fake_rest),
        ):
            E.flatten_lot_taker("MKT", "bid", TS + 100)
            # Also supports implementations that cancel on one tick and
            # submit the IOC on the following tick.
            if "POST" not in methods:
                E.flatten_lot_taker("MKT", "bid", TS + 101)

    assert "DELETE" in methods
    assert "POST" in methods
    assert methods.index("DELETE") < methods.index("POST")
    assert key not in E.S.orders


def test_failed_fill_pagination_does_not_commit_timestamp_watermark():
    """Page one may be applied idempotently, but its cursor is not committed."""
    fresh()
    page_one = fill(
        "page-1-fill", "yes", 0.40, created_ts=TS + 10, fee=0.0
    )
    calls = []

    def fake_rest(method, path, body=None, host=None):
        calls.append(path)
        if "cursor=page-2" in path:
            return 503, {"error": "page failed"}
        return 200, {"fills": [page_one], "cursor": "page-2"}

    with mock.patch.object(E, "rest", fake_rest):
        assert not E.poll_fills_once()

    assert len(calls) == 2
    assert E.S.fills_cursor == TS


def test_flat_start_detects_pending_order_not_returned_by_resting_filter():
    """Startup must cover every nonterminal state, not only RESTING."""
    fresh()
    order_paths = []

    def fake_rest(method, path, body=None, host=None):
        if "/portfolio/positions" in path:
            return 200, {"market_positions": []}
        order_paths.append(path)
        if "status=resting" in path:
            return 200, {"orders": []}
        return 200, {
            "orders": [
                {
                    "order_id": "pending-create",
                    "status": "pending",
                    "remaining_count_fp": "1.00",
                }
            ]
        }

    with mock.patch.object(E, "rest", fake_rest):
        ok, why = E.flat_start_selfcheck()

    assert not ok
    assert "order" in why.lower()
    assert order_paths


def test_flat_start_reads_later_order_pages_before_declaring_flat():
    """An empty first page is not an empty account when a cursor exists."""
    fresh()
    order_paths = []

    def fake_rest(method, path, body=None, host=None):
        if "/portfolio/positions" in path:
            return 200, {"market_positions": []}
        order_paths.append(path)
        if "cursor=orders-page-2" in path:
            return 200, {
                "orders": [
                    {
                        "order_id": "late-page-order",
                        "status": "pending",
                        "remaining_count_fp": "1.00",
                    }
                ]
            }
        return 200, {"orders": [], "cursor": "orders-page-2"}

    with mock.patch.object(E, "rest", fake_rest):
        ok, why = E.flat_start_selfcheck()

    assert not ok
    assert "late-page-order" in why
    assert len(order_paths) == 2


def test_ws_no_fill_derives_no_cost_from_yes_price_dollars():
    """The live WS may omit no_price_dollars even for a NO outcome fill."""
    fresh()
    wire = {
        "action": "sell",
        "book_side": "ask",
        "outcome_side": "no",
        "side": "no",
        "count_fp": "1.00",
        "created_time": "2026-02-02T00:00:00Z",
        "fee_cost": "0.000000",
        "market_ticker": "MKT",
        "trade_id": "ws-no-yes-price-only",
        "yes_price_dollars": "0.2500",
    }

    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "cancel_all") as cancel_all,
        mock.patch.object(E, "write_control_status"),
    ):
        E.apply_fill(E.ws_fill_to_rest(wire))

    assert not E.S.halted
    cancel_all.assert_not_called()
    assert E.S.net_pos["MKT"]["n"] == pytest.approx(1.0)
    assert E.S.net_pos["MKT"]["cost"] == pytest.approx(0.75)


def test_definitive_exit_reject_retries_at_most_once_per_second():
    """Book callbacks cannot turn a missing exit order into a POST storm."""
    fresh()
    arm_quote_market()
    posts = []
    clock = [E.time.time()]

    def fake_rest(method, path, body=None, host=None):
        if method == "POST":
            posts.append(dict(body or {}))
            return 400, {"error": "invalid_order"}
        return 500, {"error": "unexpected test request"}

    with mock.patch.object(E, "PAIR", True):
        E.apply_fill(fill("yes-1", "yes", 0.30, fee=0.0))
        with (
            mock.patch.object(E, "MODE", "live"),
            mock.patch.object(E, "load_control", return_value=False),
            mock.patch.object(E, "rest", fake_rest),
            mock.patch.object(E.time, "time", side_effect=lambda: clock[0]),
        ):
            for _ in range(5):
                E.think()
            assert len(posts) == 1

            clock[0] += 1.01
            E.think()

    assert len(posts) == 2
    assert all(body.get("side") == "ask" for body in posts)


def test_order_send_gate_checks_projected_net_not_only_current_net():
    """A clip may not jump across max_net from just below the boundary."""
    fresh()
    E.S.net_pos["MKT"] = {"y": 0.5, "n": 0.0, "cost": 0.20}
    with (
        mock.patch.object(E, "MAX_NET", 1),
        mock.patch.object(E, "load_control", return_value=False),
    ):
        placed = E.order_place(
            "MKT", "bid", 0.40, risk_px=0.40, quantity=1.0,
            slot_side="bid",
        )
    assert placed is None
    assert not E.S.pending_new


def test_order_send_gate_enforces_configured_unpaired_contract_cap():
    """UNPAIRED_MAX_CT is an executable limit, not just documentation."""
    fresh()
    E.S.unpaired["MKT"] = {
        "bid": [(0.40, E.time.time(), 0.75, 0.0)],
        "ask_no": [],
    }
    E.S.net_pos["MKT"] = {"y": 0.75, "n": 0.0, "cost": 0.30}
    with (
        mock.patch.object(E, "PAIR", True),
        mock.patch.object(E, "UNPAIRED_MAX_CT", 1.0),
        mock.patch.object(E, "load_control", return_value=False),
    ):
        placed = E.order_place(
            "MKT", "bid", 0.40, risk_px=0.40, quantity=0.50,
            slot_side="bid",
        )
    assert placed is None


@pytest.mark.parametrize("pricing_failure", ["sentinel", "warmup"])
def test_pricing_failure_blocks_entries_but_never_pair_exit(pricing_failure):
    """A bad/missing fair value must not strand an already-filled first leg."""
    fresh()
    arm_quote_market()
    with mock.patch.object(E, "PAIR", True):
        E.apply_fill(fill("orphan-yes", "yes", 0.30, fee=0.0))
        if pricing_failure == "warmup":
            E.S.rti["KXBTC15M"].clear()
            E.S.rti_src_ms["KXBTC15M"].clear()
            probability = mock.patch.object(
                E.rp, "p_settle_above", wraps=E.rp.p_settle_above)
        else:
            # 90c fair versus a 46c market midpoint trips the 15c sentinel.
            probability = mock.patch.object(
                E.rp, "p_settle_above", return_value=0.90)
        with (
            probability,
            mock.patch.object(E, "load_control", return_value=False),
        ):
            E.think()

    assert ("MKT", "ask_no") in E.S.orders
    assert ("MKT", "bid") not in E.S.orders
    assert E.S.orders[("MKT", "ask_no")]["qty"] == pytest.approx(1.0)
