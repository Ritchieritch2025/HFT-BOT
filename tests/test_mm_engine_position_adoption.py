"""Startup position-adoption guards.

A market maker must be able to restart while holding inventory.  Before
2026-07-27 the engine assumed it always began flat, so a restart with one
open lot produced local=0 vs exchange=1 -- a reconciliation divergence and
a budget trip (observed live at 08:00 while switching modes).

Adoption rebuilds the ledger from exchange truth, and every failure mode
must refuse rather than guess: no cost basis, an unparseable fill, an
uncancellable pre-existing order, or a post-adoption divergence all mean
"do not trade".
"""
from __future__ import annotations

from pathlib import Path
import sys
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_mm_engine_guards import E, reset as _base_reset  # noqa: E402


def fresh():
    _base_reset()
    E.S.net_pos.clear()
    if hasattr(E.S, "unpaired"):
        E.S.unpaired.clear()
    E.S.open_cost = 0.0
    E.S.halted = False


def _rest(positions, orders=None, fills=None, cancel_code=200,
          pos_code=200, ord_code=200, fill_code=200):
    def fake(method, path, body=None, host=None):
        if method == "DELETE":
            return cancel_code, {"reduced_by": 1}
        if "/portfolio/positions" in path:
            return pos_code, {"market_positions": positions}
        if "/portfolio/orders" in path:
            return ord_code, {"orders": orders or []}
        if "/portfolio/fills" in path:
            return fill_code, {"fills": fills or []}
        return 200, {}
    return fake


def _fill(mt, side, action, count, yes_px, ts, fee="0.000000"):
    """A fill in the real wire shape (outcome_side/book_side decide sign)."""
    return {"market_ticker": mt, "outcome_side": side,
            "book_side": "bid" if side == "yes" else "ask",
            "action": action, "count_fp": f"{count:.2f}",
            "yes_price_dollars": f"{yes_px:.4f}",
            "no_price_dollars": f"{1.0 - yes_px:.4f}",
            "fee_cost": fee, "created_time": ts,
            "fill_id": f"{mt}-{side}-{ts}", "trade_id": f"t-{mt}-{ts}"}


def test_flat_account_adopts_nothing():
    fresh()
    with mock.patch.object(E, "rest", _rest([])):
        ok, why = E.adopt_open_positions()
    assert ok and "flat" in why
    assert E.S.net_pos == {}


def test_long_yes_lot_is_adopted_with_basis_from_fills():
    """One open YES lot: the ledger must come back with the exact quantity
    and a cost basis reconstructed from the fill that created it."""
    fresh()
    positions = [{"ticker": "MKT", "position_fp": "1.00"}]
    fills = [_fill("MKT", "yes", "buy", 1.0, 0.67, "2026-07-27T07:48:40Z")]
    with mock.patch.object(E, "rest", _rest(positions, fills=fills)):
        ok, why = E.adopt_open_positions()
    assert ok, why
    led = E.S.net_pos["MKT"]
    assert led["y"] == 1.0 and led["n"] == 0.0
    assert abs(led["cost"] - 0.67) < 1e-9
    assert abs(E.S.open_cost - 0.67) < 1e-9


def test_long_no_lot_is_adopted_on_the_no_side():
    fresh()
    positions = [{"ticker": "MKT", "position_fp": "-2.00"}]
    fills = [_fill("MKT", "no", "buy", 2.0, 0.21, "2026-07-27T03:33:12Z")]
    with mock.patch.object(E, "rest", _rest(positions, fills=fills)):
        ok, why = E.adopt_open_positions()
    assert ok, why
    led = E.S.net_pos["MKT"]
    assert led["n"] == 2.0 and led["y"] == 0.0
    # NO fills price at (1 - yes_price)
    assert abs(led["cost"] - 2 * 0.79) < 1e-6


def test_missing_fill_history_refuses_to_guess():
    """An open lot with no fills to explain it must fail closed: inventing
    a basis is how a ledger starts lying."""
    fresh()
    positions = [{"ticker": "MKT", "position_fp": "1.00"}]
    with mock.patch.object(E, "rest", _rest(positions, fills=[])):
        ok, why = E.adopt_open_positions()
    assert not ok
    assert "reconstruct basis" in why
    assert E.S.net_pos == {}


def test_preexisting_resting_orders_are_cancelled():
    """Orders from a previous process have no local reservation, so they are
    cancelled before adoption -- otherwise the budget guard trips on an
    exchange order with no local twin."""
    fresh()
    positions = [{"ticker": "MKT", "position_fp": "1.00"}]
    fills = [_fill("MKT", "yes", "buy", 1.0, 0.5, "2026-07-27T07:00:00Z")]
    orders = [{"order_id": "old-1"}, {"order_id": "old-2"}]
    with mock.patch.object(E, "rest", _rest(positions, orders=orders,
                                            fills=fills)):
        ok, why = E.adopt_open_positions()
    assert ok, why


def test_uncancellable_order_refuses_adoption():
    fresh()
    positions = [{"ticker": "MKT", "position_fp": "1.00"}]
    fills = [_fill("MKT", "yes", "buy", 1.0, 0.5, "2026-07-27T07:00:00Z")]
    orders = [{"order_id": "stuck"}]
    with mock.patch.object(E, "rest", _rest(positions, orders=orders,
                                            fills=fills, cancel_code=500)):
        ok, why = E.adopt_open_positions()
    assert not ok and "cannot cancel" in why


def test_endpoint_failure_refuses_adoption():
    fresh()
    with mock.patch.object(E, "rest", _rest([], pos_code=503)):
        ok, why = E.adopt_open_positions()
    assert not ok and "503" in why


def test_pair_mode_registers_adopted_lot_as_unpaired():
    """In pair mode the adopted lot must appear as unpaired inventory, or
    the pairing loop will never quote its complement."""
    fresh()
    positions = [{"ticker": "MKT", "position_fp": "1.00"}]
    fills = [_fill("MKT", "yes", "buy", 1.0, 0.67, "2026-07-27T07:48:40Z")]
    with (mock.patch.object(E, "PAIR", True),
          mock.patch.object(E, "rest", _rest(positions, fills=fills))):
        ok, why = E.adopt_open_positions()
    assert ok, why
    lots = E.S.unpaired.get("MKT")
    assert lots and lots["bid"], "adopted YES lot must be unpaired inventory"
    # 4-tuple contract (px, ts, ct, fee): the 3-tuple this test used to
    # accept crashed pair_flatten_task live on 2026-07-27T22:59.
    px, _ts, qty, fee = lots["bid"][0]
    assert abs(px - 0.67) < 1e-9 and qty == 1.0 and fee == 0.0


def test_adopted_ledger_must_reconcile():
    """If the rebuilt ledger still diverges from the exchange rows, refuse.
    (Belt-and-braces: adoption is checked with the same comparator the
    running engine uses.)"""
    fresh()
    positions = [{"ticker": "MKT", "position_fp": "1.00"},
                 {"ticker": "OTHER", "position_fp": "bogus"}]
    fills = [_fill("MKT", "yes", "buy", 1.0, 0.5, "2026-07-27T07:00:00Z")]
    with mock.patch.object(E, "rest", _rest(positions, fills=fills)):
        ok, why = E.adopt_open_positions()
    assert not ok
