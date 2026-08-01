"""Bounded-loss maker exit for aged orphans.

The retired taker timer averaged -12.6c per cut because it crossed the
spread at the worst moment (2026-07-27 live: complement bought at 97.9c
against a 63c target, total cost 133.9c).  Removing it left the opposite
hole: a lot that cannot pair sits at the profit ceiling forever and rides to
settlement, which is a directional bet.

The fix is age-ramped ceiling relaxation: hold the profit ceiling while the
lot is young, then accept up to ORPHAN_MAKER_MAX_LOSS_C so the exit becomes
reachable -- always post-only, never crossing, never paying taker fees.
"""
from __future__ import annotations

from pathlib import Path
import sys
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_mm_engine_guards import E  # noqa: E402


def test_young_lot_holds_the_profit_ceiling():
    """No relaxation before the age gate: a fresh lot must still be trying
    to lock a profit, not to escape."""
    now = 1_000_000.0
    with (mock.patch.object(E, "ORPHAN_MAKER_AGE_S", 45.0),
          mock.patch.object(E, "ORPHAN_MAKER_MAX_LOSS_C", 3.0)):
        assert E.orphan_relax_c(now, now_s=now) == 0.0
        assert E.orphan_relax_c(now - 44.0, now_s=now) == 0.0


def test_relaxation_ramps_with_age_and_is_capped():
    """Past the gate the allowance ramps, and never exceeds the cap."""
    now = 1_000_000.0
    with (mock.patch.object(E, "ORPHAN_MAKER_AGE_S", 45.0),
          mock.patch.object(E, "ORPHAN_MAKER_MAX_LOSS_C", 3.0)):
        mid = E.orphan_relax_c(now - 45.0 - 22.5, now_s=now)
        full = E.orphan_relax_c(now - 45.0 - 45.0, now_s=now)
        ancient = E.orphan_relax_c(now - 10_000.0, now_s=now)
        assert 0.0 < mid < 3.0, mid
        assert abs(full - 3.0) < 1e-9
        assert abs(ancient - 3.0) < 1e-9, "must never exceed the cap"


def test_relaxation_is_monotone_in_age():
    now = 1_000_000.0
    with (mock.patch.object(E, "ORPHAN_MAKER_AGE_S", 30.0),
          mock.patch.object(E, "ORPHAN_MAKER_MAX_LOSS_C", 2.0)):
        vals = [E.orphan_relax_c(now - a, now_s=now)
                for a in (0, 20, 30, 40, 50, 60, 120)]
        assert vals == sorted(vals), vals


def test_disabled_by_zero_knob():
    """Setting the allowance to zero restores the old pin-forever shape, so
    the behaviour can be A/B'd against it."""
    now = 1_000_000.0
    with mock.patch.object(E, "ORPHAN_MAKER_MAX_LOSS_C", 0.0):
        assert E.orphan_relax_c(now - 9999.0, now_s=now) == 0.0
    with mock.patch.object(E, "ORPHAN_MAKER_AGE_S", 0.0):
        assert E.orphan_relax_c(now - 9999.0, now_s=now) == 0.0


def test_aged_lot_gets_a_reachable_complement_price():
    """End to end: the same lot quotes a higher (more reachable) complement
    once aged, because paying more for the complement is how a maker exits.
    Crucially it is still a resting quote, never a cross."""
    E.reset() if hasattr(E, "reset") else None
    from test_mm_engine_guards import reset as base_reset
    base_reset()
    now = E.time.time()
    E.S.unpaired["MKT"] = {"bid": [(0.30, now, 1.0, 0.0)], "ask_no": []}
    with (mock.patch.object(E, "PAIR", True),
          mock.patch.object(E, "ORPHAN_MAKER_AGE_S", 45.0),
          mock.patch.object(E, "ORPHAN_MAKER_MAX_LOSS_C", 3.0)):
        _, n_young, _, exit_young = E.pair_push_prices(
            "MKT", 0.30, 0.50, 3000, 3200)
        E.S.unpaired["MKT"]["bid"] = [(0.30, now - 200.0, 1.0, 0.0)]
        _, n_aged, _, exit_aged = E.pair_push_prices(
            "MKT", 0.30, 0.50, 3000, 3200)
    assert exit_young and exit_aged
    assert n_aged > n_young, (n_young, n_aged)
    # post-only discipline: must stay strictly below the NO ask (1 - yes_bid)
    assert n_aged <= (1.0 - 3000 / 10000.0) - 0.01 + 1e-9
