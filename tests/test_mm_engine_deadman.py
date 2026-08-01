"""Deadman (exchange-side order expiration) regression gates.

Red line 2026-07-27: no live order may rest without an exchange-side
expiration; an engine death must never leave zombie quotes.  No network I/O.
"""
from __future__ import annotations

from pathlib import Path
import sys
import time
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_mm_engine_guards import (  # noqa: E402
    E,
    reset as _base_reset,
)


def fresh() -> None:
    _base_reset()
    if hasattr(E.S, "unknown_orders"):
        E.S.unknown_orders.clear()
    if hasattr(E.S, "pending_new"):
        E.S.pending_new.clear()
    E.S.tokens = 8.0
    E.S.tok_t = E.time.time()
    E.S.halted = False


def _place_live_order(fake_rest):
    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "rest", fake_rest),
        mock.patch.object(E, "budget_preflight_candidate",
                          return_value=True),
        mock.patch.object(E, "load_control", return_value=True),
        mock.patch.object(E, "write_control_status"),
    ):
        return E.order_place("MKT", "yes", 0.40, risk_px=0.40)


def test_live_order_body_carries_expiration_ts():
    """Every live GTC maker order must include exchange-side expiration."""
    fresh()
    sent = {}

    def fake_rest(method, path, body=None, host=None):
        if method == "POST":
            sent.update(body or {})
            return 201, {"order_id": "dm-1"}
        return 200, {"fills": []}

    before = time.time()
    result = _place_live_order(fake_rest)
    assert result is not None
    assert "expiration_ts" in sent, "live order without deadman expiration"
    assert sent["time_in_force"] == "good_till_canceled"
    ttl = sent["expiration_ts"] - before
    assert E.ORDER_TTL_S - 5 <= ttl <= E.ORDER_TTL_S + 5
    od = E.S.orders[("MKT", "bid")]
    assert od["expire_ts"] == sent["expiration_ts"]


def test_cancel_404_after_expiry_releases_slot_without_halt():
    """A 404 on an order whose recorded expiration has passed is fully
    explained by the deadman: release the slot, do not halt."""
    fresh()
    key = ("MKT", "bid")
    E.S.orders[key] = {"id": "dm-2", "px": 0.40, "qty": 1.0,
                       "wire_side": "yes", "exchange_px": 0.40,
                       "t": time.time() - 120,
                       "expire_ts": time.time() - 5}

    def fake_rest(method, path, body=None, host=None):
        if method == "DELETE":
            return 404, {"error": "not found"}
        if "/portfolio/fills" in path:
            return 200, {"fills": []}
        return 200, {"order": {"status": "expired"}}

    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "rest", fake_rest),
        mock.patch.object(E, "write_control_status"),
    ):
        ok = E.order_cancel("MKT", "bid", "dm-2", "reprice")

    assert ok is True
    assert key not in E.S.orders
    assert "dm-2" not in E.S.unknown_orders
    assert not E.S.halted


def test_cancel_404_before_expiry_still_halts():
    """A 404 with time left on the clock stays an unexplained
    disappearance: unknown + halt (the pre-deadman strictness)."""
    fresh()
    key = ("MKT", "bid")
    E.S.orders[key] = {"id": "dm-3", "px": 0.40, "qty": 1.0,
                       "wire_side": "yes", "exchange_px": 0.40,
                       "t": time.time(),
                       "expire_ts": time.time() + 60}

    def fake_rest(method, path, body=None, host=None):
        if method == "DELETE":
            return 404, {"error": "not found"}
        if "/portfolio/fills" in path:
            return 200, {"fills": []}
        return 200, {"order": {"status": "resting"}}

    with (
        mock.patch.object(E, "MODE", "live"),
        mock.patch.object(E, "rest", fake_rest),
        mock.patch.object(E, "write_control_status"),
    ):
        ok = E.order_cancel("MKT", "bid", "dm-3", "reprice")

    assert ok is False
    assert "dm-3" in E.S.unknown_orders
    assert E.S.halted
