"""Three-knife surgery (operator 修复令 2026-07-28) golden cases.

Overnight ledger that ordered the surgery: 64 pair locks earned +$9.67
while ~-$13 leaked through orphan legs that (a) never completed because
the completion waited as a maker, (b) were never cut before settlement,
(c) were born minutes before close with no runway to pair.
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
    E.S.flatten_pending.clear()
    E.S.flatten_sent.clear()
    E.S.books.clear()


def hold(mt, side, px_dollars, age_s, ct=3.0):
    now = E.time.time()
    E.S.unpaired[mt] = {"bid": [], "ask_no": []}
    E.S.unpaired[mt][side] = [(px_dollars, now - age_s, ct, 0.0)]


# ---------------- knife 1: lock-take completion ----------------

def test_lock_take_fires_when_opposite_best_pays_the_lock():
    """Long YES@33 while the YES bid is 56: selling at the touch nets
    56-33-fee(~1.6) >> LOCK_TAKE_MIN_C.  Free money must be taken."""
    fresh()
    hold("MKT", "bid", 0.33, age_s=E.LOCK_TAKE_AGE_S + 5)
    E.S.books["MKT"] = {"y": {5600: 40.0}, "n": {4300: 40.0}}
    with mock.patch.object(E, "PAIR", True):
        due = E.lock_take_due(E.time.time())
    assert due == [("MKT", "bid")]


def test_lock_take_waits_for_maker_first():
    """Younger than LOCK_TAKE_AGE_S: give the resting maker completion a
    chance to earn the better price before paying taker fees."""
    fresh()
    hold("MKT", "bid", 0.33, age_s=E.LOCK_TAKE_AGE_S - 5)
    E.S.books["MKT"] = {"y": {5600: 40.0}, "n": {4300: 40.0}}
    with mock.patch.object(E, "PAIR", True):
        assert E.lock_take_due(E.time.time()) == []


def test_lock_take_refuses_a_losing_cut():
    """Long YES@33, best bid 33: proceeds - basis - fee < min lock.
    Knife 1 NEVER cuts at a loss -- that is knife 2's timed job."""
    fresh()
    hold("MKT", "bid", 0.33, age_s=E.LOCK_TAKE_AGE_S + 5)
    E.S.books["MKT"] = {"y": {3300: 40.0}, "n": {6600: 40.0}}
    with mock.patch.object(E, "PAIR", True):
        assert E.lock_take_due(E.time.time()) == []


def test_lock_take_cut_prices_at_the_touch_not_through_it():
    """cross=0.0: the lock-take IOC sells exactly at the opposite best,
    not one cent through it (the timed bail keeps its 1c cross)."""
    fresh()
    hold("MKT", "bid", 0.33, age_s=30)
    E.S.books["MKT"] = {"y": {5600: 40.0}, "n": {4300: 40.0}}
    sent = []
    with mock.patch.object(E, "order_taker",
                           side_effect=lambda *a, **k: sent.append((a, k))):
        E.flatten_lot_taker("MKT", "bid", E.time.time(),
                            cross=0.0, reason="lock_take")
    (mt, wire_side, wire_px, ct), kw = sent[0]
    assert wire_side == "ask" and abs(wire_px - 0.56) < 1e-9
    assert kw["reason"] == "lock_take"


def test_timed_bail_still_crosses_one_cent():
    fresh()
    hold("MKT", "ask_no", 0.12, age_s=200)
    E.S.books["MKT"] = {"y": {800: 40.0}, "n": {700: 40.0}}
    sent = []
    with mock.patch.object(E, "order_taker",
                           side_effect=lambda *a, **k: sent.append((a, k))):
        E.flatten_lot_taker("MKT", "ask_no", E.time.time())
    (mt, wire_side, wire_px, ct), kw = sent[0]
    # long NO -> buy YES one cent through (1 - nb) : 0.93 + 0.01
    assert wire_side == "bid" and abs(wire_px - 0.94) < 1e-9
    assert kw["reason"] == "unpaired_age"


# ---------------- knife 2: the timed cut is armed ----------------

def test_flatten_due_arms_at_the_configured_age():
    fresh()
    hold("MKT", "ask_no", 0.12, age_s=130)
    with mock.patch.object(E, "PAIR", True), \
            mock.patch.object(E, "UNPAIRED_AGE_S", 120.0):
        assert E.flatten_due(E.time.time()) == [("MKT", "ask_no")]
    with mock.patch.object(E, "PAIR", True), \
            mock.patch.object(E, "UNPAIRED_AGE_S", 100000.0):
        assert E.flatten_due(E.time.time()) == []


# ---------------- knife 3: entry runway cutoff ----------------

def test_entry_cutoff_config_present():
    assert E.ENTRY_CUTOFF_TTE_S == 240.0
