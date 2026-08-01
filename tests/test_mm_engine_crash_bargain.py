"""Guards for the two defects that produced the 2026-07-27 live losses.

Both fills that lost money shared one shape: the engine only had permission
to quote in the extreme-deviation region (the 300-600s mid-band was
excluded), and in that region an apparent bargain means the index is moving
and our fair is the stale number.  Bought YES at 36c against a 47c fair,
then the complement was only reachable at 97.9c.

1. zone bands must be configurable, so the quiet two-way market at 49-50c
   (64 of 66 evaluations rejected that session) can be opened;
2. an edge far above what the contract can plausibly misprice must be
   REFUSED, not chased.
"""
from __future__ import annotations

from pathlib import Path
import sys
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_mm_engine_guards import E  # noqa: E402


def test_graveyard_floor_is_still_hard():
    """The <120s exclusion is settlement-truth (-3.5c/contract measured),
    not a strategy preference: it holds whatever the other knobs say."""
    with mock.patch.object(E, "ZONE_MID_EXCLUDE_C", 0.0):
        assert E.zone_ok(119, 50) is False
        assert E.zone_ok(60, 8) is False


def test_mid_band_can_be_opened():
    """With the exclusion at its inherited 10c the quiet 49-50c book is
    closed; setting it to 0 opens exactly that market."""
    with mock.patch.object(E, "ZONE_MID_EXCLUDE_C", 10.0):
        assert E.zone_ok(450, 49.5) is False        # the session's loss case
        assert E.zone_ok(450, 30.0) is True
    with mock.patch.object(E, "ZONE_MID_EXCLUDE_C", 0.0):
        assert E.zone_ok(450, 49.5) is True


def test_tail_only_window_can_be_opened():
    with mock.patch.object(E, "ZONE_TAIL_ONLY", True):
        assert E.zone_ok(200, 50) is False
        assert E.zone_ok(200, 15) is True
    with mock.patch.object(E, "ZONE_TAIL_ONLY", False):
        assert E.zone_ok(200, 50) is True


def test_absurd_edge_is_refused():
    """The live loss: fair 47c, quote 36c, 11c of 'edge' while sigma_c was
    ~4.5c.  At 2 sigma the cap is ~9c, so that entry must be refused."""
    # Exactly the live receipt: sigma=2.7128, sigma_c=4.4838 (tau=5s, so
    # dp_ds = 4.4838 / (2.7128 * sqrt(5)) = 0.7392 cents per index dollar).
    ps = {"sigma": 2.7128, "dp_ds_c_per_usd": 0.7392}
    with mock.patch.object(E, "SIGMA_TAU_S", 5.0):
        sc = E.sigma_contract_c(ps)
        assert sc is not None and 4.0 < sc < 5.0, sc
        with mock.patch.object(E, "MAX_EDGE_SIGMA", 2.0):
            assert E.edge_within_cap(1.5, ps) is True     # ordinary edge
            assert E.edge_within_cap(sc * 1.9, ps) is True
            assert E.edge_within_cap(11.0, ps) is False   # the crash bargain


def test_edge_cap_scales_with_volatility():
    """A volatile contract may legitimately show a larger edge than a quiet
    one: the cap is in sigma-units, not cents."""
    quiet = {"sigma": 0.5, "dp_ds_c_per_usd": 1.0}
    wild = {"sigma": 5.0, "dp_ds_c_per_usd": 1.0}
    with mock.patch.object(E, "MAX_EDGE_SIGMA", 2.0):
        edge = 6.0
        assert E.edge_within_cap(edge, quiet) is False
        assert E.edge_within_cap(edge, wild) is True


def test_edge_cap_disabled_passes_everything():
    ps = {"sigma": 2.0, "dp_ds_c_per_usd": 1.0}
    with mock.patch.object(E, "MAX_EDGE_SIGMA", 0.0):
        assert E.edge_within_cap(99.0, ps) is True


def test_edge_cap_without_pricing_does_not_block():
    """No sigma available must not silently stop all trading — the pricing
    gate already handles unusable pricing."""
    with mock.patch.object(E, "MAX_EDGE_SIGMA", 2.0):
        assert E.edge_within_cap(5.0, None) is True
        assert E.edge_within_cap(5.0, {"sigma": 0.0,
                                       "dp_ds_c_per_usd": 1.0}) is True
