"""Executable contract for the absolute $10 live-engine budget gate.

These tests stay entirely local: exchange reads and order POSTs are faked,
and every durable budget record lives in a temporary directory.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
import fcntl
import os
from pathlib import Path
import tempfile
from unittest import mock

import pytest

from test_mm_engine_guards import E, reset as _base_reset

import mm_budget as B
import mm_budget_adapter as A


NOW = 1_785_100_000.4
ACCOUNT = "test-key"


@pytest.fixture(autouse=True)
def restore_engine_singletons():
    yield
    E.S.budget_guard = None
    E.S.budget_startup_ok = False
    E.S.budget_previous_updated_ts = None
    E.S.budget_previous_user_data_as_of_s = None
    E.S.budget_remote_orders = []
    E.S.risk_generation = 0
    E.S.fills_cursor = int(E.time.time())


def fresh() -> None:
    _base_reset()
    E.S.orders.clear()
    E.S.pending_new.clear()
    E.S.unknown_orders.clear()
    E.S.flatten_pending.clear()
    E.S.net_pos.clear()
    E.S.risk_generation = 0
    E.S.budget_guard = None
    E.S.budget_startup_ok = False
    E.S.budget_previous_updated_ts = None
    E.S.budget_previous_user_data_as_of_s = None
    E.S.budget_remote_orders = []
    E.S.live_writer_lock_fd = None
    E.S.tokens = 8.0
    E.S.tok_t = 0.0


def balance(dollars: str, *, portfolio_value: int = 0) -> dict:
    micro = B.fixed_dollars_to_micro_usd("test balance", dollars)
    return {
        "balance": micro // B.MICRO_USD_PER_CENT,
        "balance_dollars": dollars,
        "portfolio_value": portfolio_value,
        "updated_ts": int(NOW),
        "balance_breakdown": [{"exchange_index": 0, "balance": dollars}],
    }


def user_data_timestamp(epoch_s: float = NOW) -> dict:
    value = dt.datetime.fromtimestamp(
        epoch_s, tz=dt.timezone.utc
    ).isoformat().replace("+00:00", "Z")
    return {"as_of_time": value}


def initialize_budget(path: Path, *, anchor: str = "17.279400"):
    anchor_micro = B.fixed_dollars_to_micro_usd("anchor", anchor)
    record = B.new_budget_record(
        budget_id="btc15m-live-absolute-10",
        account_fingerprint=A.account_fingerprint(ACCOUNT),
        anchor_equity_micro_usd=anchor_micro,
        max_loss_micro_usd=15_279_400,
    )
    B.initialize_budget(path, record)
    return record


def safe_rest(method, path, body=None, host=None):
    if method == "GET" and path.startswith("/portfolio/positions"):
        return 200, {"market_positions": []}
    if method == "GET" and path.startswith("/portfolio/orders"):
        return 200, {"orders": []}
    if method == "GET" and path == "/portfolio/balance?subaccount=0":
        return 200, balance("19.820500")
    if method == "GET" and path == "/exchange/user_data_timestamp":
        return 200, user_data_timestamp()
    raise AssertionError((method, path, body, host))


def remote_order(
    order_id: str,
    *,
    client_order_id: str,
    ticker: str,
    engine_side: str,
    risk_px: str,
    quantity: str,
    remaining_quantity: str | None = None,
) -> dict:
    risk = Decimal(risk_px)
    yes_px = risk if engine_side == "bid" else Decimal(1) - risk
    return {
        "order_id": order_id,
        "client_order_id": client_order_id,
        "ticker": ticker,
        "remaining_count_fp": remaining_quantity or quantity,
        "subaccount_number": 0,
        "exchange_index": 0,
        "status": "resting",
        "outcome_side": "yes" if engine_side == "bid" else "no",
        "book_side": "bid" if engine_side == "bid" else "ask",
        "action": "buy" if engine_side == "bid" else "sell",
        "yes_price_dollars": f"{yes_px:.4f}",
        "no_price_dollars": f"{Decimal(1) - yes_px:.4f}",
    }


def configure_guard(path: Path):
    guard = A.BudgetGuard(
        budget_path=path,
        account_identity=ACCOUNT,
        cancel_all=E.budget_cancel_all,
        halt=E.budget_halt,
    )
    E.S.budget_guard = guard
    E.S.budget_startup_ok = True
    return guard


def test_live_budget_identity_is_fixed_and_never_rebased():
    fresh()
    # Values re-anchored 2026-07-27T22 by operator speed directive (full
    # account for testing, $2 control ration).  The INVARIANT this test
    # protects is unchanged: identity is compile-time fixed, anchor minus
    # max-loss equals the floor, and no runtime path may rebase it.
    assert E.BUDGET_ANCHOR_MICRO_USD == 17_279_400
    assert E.BUDGET_MAX_LOSS_MICRO_USD == 15_279_400
    assert E.BUDGET_FLOOR_MICRO_USD == 2_000_000
    assert (E.BUDGET_ANCHOR_MICRO_USD - E.BUDGET_MAX_LOSS_MICRO_USD
            == E.BUDGET_FLOOR_MICRO_USD)

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "budget.json"
        original = initialize_budget(path)
        before = path.read_bytes()
        E.initialize_budget_guard(path=path, acquire_writer=False)
        assert E.S.budget_guard.record == original
        assert path.read_bytes() == before

        wrong = Path(td) / "wrong.json"
        initialize_budget(wrong, anchor="21.000000")
        with pytest.raises(A.AdapterError, match="anchor"):
            E.initialize_budget_guard(path=wrong, acquire_writer=False)


def test_live_accounting_contract_requires_explicit_verified_marker():
    with mock.patch.object(
            E, "BUDGET_ACCOUNTING_CONTRACT_VERIFIED", False):
        assert E.budget_accounting_contract_errors()
    with mock.patch.object(
            E, "BUDGET_ACCOUNTING_CONTRACT_VERIFIED", True):
        assert E.budget_accounting_contract_errors() == []


def test_unverified_accounting_contract_blocks_live_before_budget_or_network():
    fresh()
    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "BUDGET_ENFORCE", True),
        mock.patch.object(E, "BUDGET_ACCOUNTING_CONTRACT_VERIFIED", False),
        mock.patch.object(E, "initialize_budget_guard") as initialize,
        mock.patch.object(E, "write_control_status"),
        mock.patch.object(E.L, "w"),
    ):
        asyncio.get_event_loop().run_until_complete(E.main())
    initialize.assert_not_called()
    assert E.S.halted
    assert "three-state probe" in E.S.control_error


def test_single_writer_lock_fails_closed_when_another_process_owns_path():
    fresh()
    with tempfile.TemporaryDirectory() as td:
        budget_path = Path(td) / "budget.json"
        other_budget_path = Path(td) / "different-budget.json"
        lock_path = E.live_writer_lock_path()
        assert lock_path != budget_path.with_name(
            budget_path.name + ".engine.lock"
        )
        owner_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(A.AdapterError, match="another live engine"):
                E.acquire_live_writer_lock(other_budget_path)
            assert E.S.live_writer_lock_fd is None
        finally:
            os.close(owner_fd)


def test_snapshot_reads_all_pages_then_balance_and_validation_timestamp():
    fresh()
    E.S.orders[("MKT", "bid")] = {
        "id": "oid-1", "client_order_id": "client-1",
        "px": 0.40, "qty": 1.0, "wire_side": "bid",
        "exchange_px": 0.40, "t": NOW - 5,
    }
    calls = []

    def fake_rest(method, path, body=None, host=None):
        calls.append(path)
        if path.startswith("/portfolio/positions"):
            if "cursor=p2" in path:
                return 200, {
                    "market_positions": [],
                    "cursor": "",
                }
            return 200, {
                "market_positions": [],
                "next_cursor": "p2",
            }
        if path.startswith("/portfolio/orders"):
            row = remote_order(
                "oid-1", client_order_id="client-1", ticker="MKT",
                engine_side="bid", risk_px="0.40", quantity="1.00",
            )
            if "cursor=o2" in path:
                return 200, {"orders": [], "cursor": None}
            return 200, {"orders": [row], "cursor": "o2"}
        if path == "/portfolio/balance?subaccount=0":
            return 200, balance("19.820500")
        if path == "/exchange/user_data_timestamp":
            return 200, user_data_timestamp()
        raise AssertionError(path)

    with (
        mock.patch.object(E, "rest", fake_rest),
        mock.patch.object(E.time, "time", return_value=NOW),
    ):
        kwargs = E.capture_budget_snapshot()

    inputs = kwargs["snapshot_inputs"]
    assert len(inputs.positions_response["market_positions"]) == 0
    assert inputs.ledger_generation_before == 0
    assert inputs.ledger_generation_after == 0
    assert calls[-2:] == [
        "/portfolio/balance?subaccount=0",
        "/exchange/user_data_timestamp",
    ]
    assert calls[0] == (
        "/portfolio/positions?count_filter=position"
        "&limit=1000&subaccount=0"
    )
    assert calls[2] == (
        "/portfolio/orders?status=resting&subaccount=0&limit=1000"
    )
    assert sum(path.startswith("/portfolio/positions") for path in calls) == 2
    assert sum(path.startswith("/portfolio/orders") for path in calls) == 2


def test_remote_order_id_match_cannot_hide_different_economics():
    fresh()
    pending = {
        ("MKT", "bid"): {
            "client_order_id": "same-client",
            "px": 0.10,
            "qty": 1.0,
            "wire_side": "bid",
            "exchange_px": 0.10,
        }
    }
    forged = remote_order(
        "remote-id",
        client_order_id="same-client",
        ticker="OTHER",
        engine_side="ask_no",
        risk_px="0.01",
        quantity="20.00",
    )
    with pytest.raises(A.AdapterError, match="ticker mismatch"):
        E._budget_validate_open_orders(
            [forged],
            orders={},
            pending_new=pending,
            unknown_orders={},
        )


def test_no_ask_order_and_partial_remaining_match_authoritative_fields():
    fresh()
    local = {
        ("MKT", "ask_no"): {
            "id": "no-order",
            "client_order_id": "no-client",
            "px": 0.30,
            "qty": 2.0,
            "wire_side": "ask",
            "exchange_px": 0.70,
        }
    }
    remote = remote_order(
        "no-order",
        client_order_id="no-client",
        ticker="MKT",
        engine_side="ask_no",
        risk_px="0.30",
        quantity="2.00",
        remaining_quantity="0.75",
    )
    # Current production Get Orders rows do not carry exchange_index.
    del remote["exchange_index"]
    active = E._budget_validate_open_orders(
        [remote],
        orders=local,
        pending_new={},
        unknown_orders={},
    )
    assert active == [remote]
    assert remote["action"] == "sell"


def test_remote_order_missing_resting_status_fails_closed():
    fresh()
    local = {
        ("MKT", "bid"): {
            "id": "yes-order",
            "client_order_id": "yes-client",
            "px": 0.40,
            "qty": 1.0,
            "wire_side": "bid",
            "exchange_px": 0.40,
        }
    }
    remote = remote_order(
        "yes-order",
        client_order_id="yes-client",
        ticker="MKT",
        engine_side="bid",
        risk_px="0.40",
        quantity="1.00",
    )
    del remote["status"]
    with pytest.raises(A.AdapterError, match="status"):
        E._budget_validate_open_orders(
            [remote],
            orders=local,
            pending_new={},
            unknown_orders={},
        )


def test_zero_remote_remaining_on_resting_snapshot_fails_closed():
    fresh()
    local = {
        ("MKT", "bid"): {
            "id": "yes-order",
            "client_order_id": "yes-client",
            "px": 0.40,
            "qty": 1.0,
            "wire_side": "bid",
            "exchange_px": 0.40,
        }
    }
    remote = remote_order(
        "yes-order",
        client_order_id="yes-client",
        ticker="MKT",
        engine_side="bid",
        risk_px="0.40",
        quantity="1.00",
        remaining_quantity="0.00",
    )
    with pytest.raises(A.AdapterError, match="positive"):
        E._budget_validate_open_orders(
            [remote],
            orders=local,
            pending_new={},
            unknown_orders={},
        )


def test_unmatched_exchange_open_order_trips_and_latches_startup():
    fresh()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "budget.json"
        initialize_budget(path)
        configure_guard(path)

        def fake_rest(method, request_path, body=None, host=None):
            if method == "GET" and request_path.startswith(
                    "/portfolio/positions"):
                return 200, {"market_positions": []}
            if method == "GET" and request_path.startswith("/portfolio/orders"):
                return 200, {"orders": [{
                    "order_id": "external-open",
                    "client_order_id": "external-client",
                    "ticker": "MKT",
                    "remaining_count_fp": "1.00",
                    "subaccount_number": 0,
                    "exchange_index": 0,
                    "outcome_side": "yes",
                    "book_side": "bid",
                    "yes_price_dollars": "0.4000",
                    "no_price_dollars": "0.6000",
                }]}
            if method == "GET" and request_path == (
                    "/portfolio/balance?subaccount=0"):
                return 200, balance("19.820500")
            if method == "GET" and request_path == (
                    "/exchange/user_data_timestamp"):
                return 200, user_data_timestamp()
            if method == "DELETE":
                return 200, {"order_id": "external-open", "reduced_by": "1.00"}
            raise AssertionError((method, request_path))

        with (
            mock.patch.object(E, "rest", fake_rest),
            mock.patch.object(E.time, "time", return_value=NOW),
            mock.patch.object(E, "write_control_status"),
        ):
            assert E.budget_startup_once() is None

        assert E.S.halted
        assert B.load_budget(path).latched
        assert E.S.budget_guard.trip_receipt is not None


def test_candidate_floor_breach_is_rejected_before_order_post_without_latch():
    fresh()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "budget.json"
        initialize_budget(path)
        guard = configure_guard(path)
        posts = []

        def fake_rest(method, request_path, body=None, host=None):
            if method == "POST":
                posts.append(body)
                return 201, {"order_id": "must-not-post"}
            if method == "GET" and request_path.startswith(
                    "/portfolio/positions"):
                return 200, {"market_positions": []}
            if method == "GET" and request_path.startswith("/portfolio/orders"):
                return 200, {"orders": []}
            if method == "GET" and request_path == (
                    "/portfolio/balance?subaccount=0"):
                # E_safe just above the $2.00 floor; a 10c order crosses it.
                return 200, balance("2.040000")
            if method == "GET" and request_path == (
                    "/exchange/user_data_timestamp"):
                return 200, user_data_timestamp()
            raise AssertionError((method, request_path))

        with (
            mock.patch.object(E, "MODE", "live"),
            mock.patch.object(E, "BUDGET_ENFORCE", True),
            mock.patch.object(E, "rest", fake_rest),
            mock.patch.object(E, "load_control", return_value=False),
            mock.patch.object(E.time, "time", return_value=NOW),
        ):
            placed = E.order_place(
                "MKT", "bid", 0.10, risk_px=0.10, quantity=1.0,
                slot_side="bid",
            )

        assert placed is None
        assert posts == []
        assert not guard.record.latched
        assert not B.load_budget(path).latched
        assert not E.S.pending_new


def test_non_atomic_pair_candidate_cannot_claim_both_legs_fill():
    fresh()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "budget.json"
        initialize_budget(path)
        guard = configure_guard(path)
        E.S.orders[("MKT", "bid")] = {
            "id": "yes-order",
            "client_order_id": "yes-client",
            "px": 0.10,
            "qty": 1.0,
            "wire_side": "bid",
            "exchange_px": 0.10,
            "t": NOW - 1,
        }

        def fake_rest(method, request_path, body=None, host=None):
            if method == "POST":
                raise AssertionError("unsafe candidate reached POST")
            if method == "GET" and request_path.startswith(
                    "/portfolio/positions"):
                return 200, {"market_positions": []}
            if method == "GET" and request_path.startswith("/portfolio/orders"):
                return 200, {"orders": [remote_order(
                    "yes-order",
                    client_order_id="yes-client",
                    ticker="MKT",
                    engine_side="bid",
                    risk_px="0.10",
                    quantity="1.00",
                )]}
            if method == "GET" and request_path == (
                    "/portfolio/balance?subaccount=0"):
                return 200, balance("2.240000")
            if method == "GET" and request_path == (
                    "/exchange/user_data_timestamp"):
                return 200, user_data_timestamp()
            raise AssertionError((method, request_path))

        with (
            mock.patch.object(E, "MODE", "live"),
            mock.patch.object(E, "BUDGET_ENFORCE", True),
            mock.patch.object(E, "rest", fake_rest),
            mock.patch.object(E, "load_control", return_value=False),
            mock.patch.object(E.time, "time", return_value=NOW),
        ):
            placed = E.order_place(
                "MKT", "ask", 0.70, risk_px=0.30, quantity=1.0,
                slot_side="ask_no",
            )

        assert placed is None
        assert not guard.record.latched
        assert not B.load_budget(path).latched


def test_allowed_maker_is_reserved_before_post_and_ack_transfer_is_atomic():
    fresh()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "budget.json"
        initialize_budget(path)
        configure_guard(path)
        observed = {}

        def fake_rest(method, request_path, body=None, host=None):
            if method == "GET":
                return safe_rest(method, request_path, body, host)
            if method == "POST":
                pending = E.S.pending_new[("MKT", "bid")]
                observed.update(pending)
                observed["generation_at_post"] = E.S.risk_generation
                return 201, {"order_id": "oid-live"}
            raise AssertionError((method, request_path))

        with (
            mock.patch.object(E, "MODE", "live"),
            mock.patch.object(E, "BUDGET_ENFORCE", True),
            mock.patch.object(E, "rest", fake_rest),
            mock.patch.object(E, "load_control", return_value=False),
            mock.patch.object(E.time, "time", return_value=NOW),
        ):
            placed = E.order_place(
                "MKT", "bid", 0.40, risk_px=0.40, quantity=1.0,
                slot_side="bid",
            )

        assert placed == ("oid-live", 1.0)
        assert observed["client_order_id"]
        assert observed["px"] == pytest.approx(0.40)
        assert observed["qty"] == pytest.approx(1.0)
        assert observed["generation_at_post"] >= 1
        assert not E.S.pending_new
        assert E.S.orders[("MKT", "bid")]["id"] == "oid-live"
        assert E.S.risk_generation >= 2


def test_ioc_candidate_and_unresolved_reservation_include_price_qty_and_fee():
    fresh()
    captured = []

    def fake_preflight(candidate):
        captured.append(candidate)
        return True

    def fake_rest(method, request_path, body=None, host=None):
        assert method == "POST"
        pending = E.S.flatten_pending[("MKT", "bid")]
        assert pending["risk_px"] == pytest.approx(0.93)
        assert pending["remaining_risk_qty"] == pytest.approx(1.0)
        assert pending["fee_reserve"] == pytest.approx(
            E.taker_fee_usd(0.93, 1.0)
        )
        return 503, {"error": "ambiguous"}

    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "BUDGET_ENFORCE", True),
        mock.patch.object(E, "budget_preflight_candidate",
                          side_effect=fake_preflight),
        mock.patch.object(E, "rest", fake_rest),
        mock.patch.object(E, "write_control_status"),
    ):
        assert not E.order_taker(
            "MKT", "ask", 0.07, 1.0, reason="test-ioc"
        )

    assert len(captured) == 1
    assert captured[0].engine_side == "ask_no"
    assert captured[0].risk_px == pytest.approx(0.93)
    assert captured[0].fee_reserve > 0
    assert ("MKT", "bid") in E.S.flatten_pending
    risks = A.build_ledger_risks(
        orders=E.S.orders,
        pending_new=E.S.pending_new,
        unknown_orders=E.S.unknown_orders,
        flatten_pending=E.S.flatten_pending,
    )
    assert len(risks.unknown) == 1


def test_fill_and_reservation_transitions_advance_risk_generation():
    fresh()
    E.S.orders[("MKT", "bid")] = {
        "id": "oid-fill", "px": 0.40, "qty": 1.0, "t": NOW,
    }
    before = E.S.risk_generation
    E.apply_fill({
        "fill_id": "fill-1",
        "order_id": "oid-fill",
        "ticker": "MKT",
        "outcome_side": "yes",
        "book_side": "bid",
        "side": "yes",
        "count_fp": "1.00",
        "yes_price_dollars": "0.4000",
        "no_price_dollars": "0.6000",
        "created_ts": int(NOW * 1000),
        "fee_cost": "0.000000",
    })
    assert E.S.risk_generation > before
    assert ("MKT", "bid") not in E.S.orders


def test_budget_monitor_task_uses_an_independent_one_second_clock():
    fresh()
    calls = []

    async def fake_sleep(seconds):
        calls.append(("sleep", seconds))
        if len(calls) > 1:
            raise asyncio.CancelledError

    def monitor():
        calls.append(("monitor", None))

    async def exercise():
        with (
            mock.patch.object(E.asyncio, "sleep", side_effect=fake_sleep),
            mock.patch.object(E, "budget_monitor_once", side_effect=monitor),
            mock.patch.object(E, "MODE", "live"),
            mock.patch.object(E, "BUDGET_ENFORCE", True),
        ):
            with pytest.raises(asyncio.CancelledError):
                await E.budget_task()

    asyncio.get_event_loop().run_until_complete(exercise())
    assert ("sleep", 1.0) in calls
    assert ("monitor", None) in calls
