"""Approval #5 golden cases: the exit loop must EXECUTE, not just judge.

The 2026-07-27 21:03 lot (YES@33) watched the market pay 54-57c for eight
minutes while ~700 sell_rich receipts fired and zero orders existed.  These
cases pin the surgery: a sell_rich verdict must produce a resting exit
order pinned to max(fair+thr, book), the cost trigger uses the same path,
the only sanctioned taker exit is the explicit cost comparison, and the
graveyard flatten is unconditional.
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
    E.S.unpaired.clear()
    E.S.net_pos.clear()


def test_golden_54_57_window_pins_reachable_exit():
    """THE case from the field: long YES@33, book bids 56 for YES
    (no_ask 44).  The complement must quote NO at 43 -- one tick inside --
    and the intent cap must not push it away from the reachable price."""
    fresh()
    now = E.time.time()
    E.S.unpaired["MKT"] = {"bid": [(0.33, now, 1.0, 0.0)], "ask_no": []}
    E.S.exit_intent["MKT"] = {"side": "bid", "target_c": 57.0,
                              "action": "sell_rich", "ts": now}
    with mock.patch.object(E, "PAIR", True):
        _, n_px, _, exit_no = E.pair_push_prices(
            "MKT", 0.33, 0.50, 5600, 5700)   # yes_bid 56 -> no_ask 44
    assert exit_no, "the exit leg must be armed"
    assert abs(n_px - 0.43) < 1e-9, n_px


def test_exit_floor_holds_when_book_pays_below_target():
    """Book pays only 43 for YES (no_ask 57) but fair+thr says 49: the
    quote rests at NO 51 = selling YES at 49, never cheaper."""
    fresh()
    now = E.time.time()
    E.S.unpaired["MKT"] = {"bid": [(0.33, now, 1.0, 0.0)], "ask_no": []}
    E.S.exit_intent["MKT"] = {"side": "bid", "target_c": 49.0,
                              "action": "sell_rich", "ts": now}
    with mock.patch.object(E, "PAIR", True):
        _, n_px, _, exit_no = E.pair_push_prices(
            "MKT", 0.33, 0.50, 4300, 4400)   # yes_bid 43 -> no_ask 57
    assert exit_no
    assert n_px <= 0.51 + 1e-9, n_px


def test_sell_cost_trigger_fires_and_labels_separately():
    """Approval #5 item 2: offer above cost+thr (but below fair+thr) fires
    the SAME exit path with its own label."""
    fresh()
    E.S.net_pos["MKT"] = {"y": 1.0, "n": 0.0, "cost": 0.33}
    ps = {"sigma": 2.0, "dp_ds_c_per_usd": 0.5}
    with mock.patch.object(E, "SIGMA_SCALE", False), \
         mock.patch.object(E, "MARGIN_C", 1.0):
        view = E.position_reval("MKT", 40.0, ps, best_bid_c=38.0)
    # offer 38 < fair+thr 41 -> not sell_rich; but 38 >= 33+1 -> sell_cost
    assert view["action"] == "sell_cost", view
    assert view["exit_target_c"] is not None


def test_taker_exit_only_on_explicit_cost_comparison():
    """Item 3: hold_cost > taker_fee + spread justifies crossing; a calm
    contract never does."""
    fresh()
    now = E.time.time()
    E.S.unpaired["MKT"] = {"bid": [(0.33, now, 2.0, 0.0)], "ask_no": []}
    E.S.last_fair_c["MKT"] = 50.0
    E.S.books["MKT"] = {"yb": 4900, "ya": 5000}
    calm = {"sigma": 0.2, "dp_ds_c_per_usd": 0.2}
    wild = {"sigma": 8.0, "dp_ds_c_per_usd": 2.0}
    with mock.patch.object(E, "SIGMA_SCALE", True):
        ok_calm, _ = E.taker_exit_justified("MKT", "bid", calm, 300.0)
        ok_wild, fields = E.taker_exit_justified("MKT", "bid", wild, 300.0)
    assert not ok_calm, "calm inventory must rest, not cross"
    assert ok_wild, "explosive inventory crosses when holding costs more"
    assert fields["hold_cost_c"] > fields["taker_fee_c"] + fields["spread_c"]


def test_reval_exit_target_never_below_fair_plus_threshold():
    fresh()
    E.S.net_pos["MKT"] = {"y": 1.0, "n": 0.0, "cost": 0.33}
    ps = {"sigma": 2.0, "dp_ds_c_per_usd": 0.5}
    with mock.patch.object(E, "SIGMA_SCALE", False), \
         mock.patch.object(E, "MARGIN_C", 1.0):
        view = E.position_reval("MKT", 52.0, ps, best_bid_c=57.0)
    assert view["action"] == "sell_rich"
    assert view["exit_target_c"] >= 53.0 - 1e-9      # fair + thr
    assert view["exit_target_c"] >= 57.0 - 1e-9      # and chases the book
