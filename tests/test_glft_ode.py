"""Stage-0 #7c golden cases for the finite-horizon GLFT solver.

Nothing here touches the engine: this is the pure-math acceptance the
maturation plan demands before the new core is allowed to even shadow.
"""
from __future__ import annotations

from pathlib import Path
import sys

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent / "tools" / "research" / "crypto_mm"))

import glft_ode  # noqa: E402
import glft_quotes  # noqa: E402

# A liquid-ish parameter set in engine units (cents / seconds / contracts).
P = dict(sigma_c=0.12, gamma=0.3, k=1.1, A=0.08)


def test_long_horizon_converges_to_asymptotic_closed_form():
    """T -> inf golden case: the ODE must reproduce glft_quotes' closed
    form.  This cross-validates BOTH implementations."""
    bid_a, ask_a, meta = glft_quotes.glft_offsets(q=0, **P)
    bid_o, ask_o = glft_ode.glft_ode_offsets(q=0, tte_s=30000.0, steps=1500,
                                             **P)
    # The closed form is itself an asymptotic APPROXIMATION; exact-ODE
    # agreement within 10% is the realistic golden band (measured ~1%).
    assert abs(bid_o - bid_a) < 0.10 * abs(bid_a) + 1e-6, (bid_o, bid_a)
    assert abs(ask_o - ask_a) < 0.10 * abs(ask_a) + 1e-6, (ask_o, ask_a)


def test_asymptotic_inventory_skew_matches_delta():
    """Per-contract skew in the long-horizon limit equals the closed-form
    Delta: quotes shift toward unwinding by Delta per held contract."""
    _, _, meta = glft_quotes.glft_offsets(q=0, **P)
    b0, a0 = glft_ode.glft_ode_offsets(q=0, tte_s=30000.0, steps=1500, **P)
    b1, a1 = glft_ode.glft_ode_offsets(q=1, tte_s=30000.0, steps=1500, **P)
    skew = b1 - b0
    assert abs(skew - meta["per_contract_skew"]) < 0.35 * meta[
        "per_contract_skew"] + 1e-6


def test_graveyard_tightens_toward_terminal_condition():
    """GOLDEN GRAVEYARD CASE: with zero terminal penalty (hold-to-settle),
    inventory stops mattering as tte -> 0 -- the skew collapses, because
    there is no time left for inventory risk to realise."""
    b_far, _ = glft_ode.glft_ode_offsets(q=3, tte_s=600.0, **P)
    b_near, _ = glft_ode.glft_ode_offsets(q=3, tte_s=5.0, **P)
    b0_far, _ = glft_ode.glft_ode_offsets(q=0, tte_s=600.0, **P)
    b0_near, _ = glft_ode.glft_ode_offsets(q=0, tte_s=5.0, **P)
    skew_far = b_far - b0_far
    skew_near = b_near - b0_near
    assert skew_near < skew_far, (skew_near, skew_far)
    assert skew_near < 0.15 * skew_far + 1e-9, "graveyard skew must collapse"


def test_near_strike_explosive_sigma_widens_quotes():
    """GOLDEN NEAR-STRIKE CASE: sigma_c is largest at the strike near
    expiry; the finite-horizon solution must widen with sigma_c
    monotonically."""
    quiet, _ = glft_ode.glft_ode_offsets(q=0, sigma_c=0.05, gamma=0.3,
                                         k=1.1, A=0.08, tte_s=300.0)
    wild, _ = glft_ode.glft_ode_offsets(q=0, sigma_c=0.6, gamma=0.3,
                                        k=1.1, A=0.08, tte_s=300.0)
    assert wild > quiet, (wild, quiet)


def test_terminal_penalty_reintroduces_urgency():
    """With a liquidation penalty, the ASK for held inventory must be more
    aggressive (smaller offset) near expiry than without one -- the solver
    prices the deadline, the exact thing the asymptotic form cannot do."""
    _, ask_no_pen = glft_ode.glft_ode_offsets(q=3, tte_s=30.0,
                                              penalty_c=0.0, **P)
    _, ask_pen = glft_ode.glft_ode_offsets(q=3, tte_s=30.0,
                                           penalty_c=5.0, **P)
    assert ask_pen < ask_no_pen, (ask_pen, ask_no_pen)


def test_boundary_inventory_disallows_outward_side():
    bid, ask = glft_ode.glft_ode_offsets(q=10, tte_s=100.0, q_max=10, **P)
    assert bid is None            # cannot buy past the cap
    assert ask is not None


def test_zero_horizon_returns_terminal_ratios():
    w = glft_ode.solve_w(horizon_s=0.0, q_max=3, penalty_c=1.0, **P)
    assert len(w) == 7
    assert w[3] == 1.0            # q=0: no penalty
    assert w[0] < w[1] < w[2] < w[3]


def test_invalid_params_raise():
    import pytest
    with pytest.raises(ValueError):
        glft_ode.solve_w(sigma_c=0.0, gamma=0.3, k=1.1, A=0.08,
                         horizon_s=10)
    with pytest.raises(ValueError):
        glft_ode.glft_ode_offsets(q=99, tte_s=10.0, q_max=10, **P)
