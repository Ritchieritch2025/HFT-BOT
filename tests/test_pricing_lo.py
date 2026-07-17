"""W-P1 acceptance (PLAN_PRICING_MODEL §3): log-odds core.

Hand-computed goldens, the 99-cent round-trip identity, conservative
quantization, and the fee plumbing: formula + rounding regime obeyed FROM
config/kalshi_facts.yaml (behavior follows a mutated temp yaml — never
hardcoded, WP-06 proof shape), gate-mode fail-closed while unverified, and
the Q9 sign properties on the PRE-ROUNDING curve.
"""
import math
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from pricing import lo  # noqa: E402


# ───────────────────────────────────────────── goldens & transforms (Q1)

def test_golden_lo_shifts_2c_vs_50c():
    """THE Q1 motivation, hand-derived:
    logit(0.01)−logit(0.02) = ln(1/99)−ln(1/49) = ln(49/99) = −0.7032995…
    logit(0.49)−logit(0.50) = ln(49/51)          = −0.0400053…
    One cent near the edge is ~18x the move of one cent at the middle."""
    d_edge = lo.lo_of_e4(100) - lo.lo_of_e4(200)
    d_mid = lo.lo_of_e4(4900) - lo.lo_of_e4(5000)
    assert d_edge == pytest.approx(math.log(49.0 / 99.0), abs=1e-12)
    assert d_mid == pytest.approx(math.log(49.0 / 51.0), abs=1e-12)
    assert round(d_edge, 2) == -0.70
    assert round(d_mid, 2) == -0.04


def test_logit_expit_inverse_and_clip():
    for p in (0.01, 0.02, 0.25, 0.5, 0.75, 0.98, 0.99):
        assert lo.expit(lo.logit(p)) == pytest.approx(p, abs=1e-12)
    # out-of-band inputs clip to the band, keeping logit finite
    assert lo.logit(0.0001) == lo.logit(0.01)
    assert lo.logit(0.9999) == lo.logit(0.99)
    assert lo.expit(-50.0) == 0.01
    assert lo.expit(50.0) == 0.99
    assert math.isfinite(lo.logit(0.0)) and math.isfinite(lo.logit(1.0))


def test_roundtrip_identity_all_99_legal_cents():
    """cent -> lo -> cent is exact on the whole legal grid, every side mode
    (float fuzz may never move an on-grid price)."""
    for cent in range(1, 100):
        e4 = cent * 100
        l = lo.lo_of_e4(e4)
        for side in ("bid", "ask", None):
            assert lo.e4_of_lo(l, side=side) == e4, (cent, side)


def test_quantization_is_conservative_off_grid():
    """An off-grid lo rounds DOWN for a bid and UP for an ask (quotes widen,
    never tighten), and nearest-mode sits between them."""
    l = (lo.lo_of_e4(4200) + lo.lo_of_e4(4300)) / 2.0   # between 42c and 43c
    assert lo.e4_of_lo(l, side="bid") == 4200
    assert lo.e4_of_lo(l, side="ask") == 4300
    assert lo.e4_of_lo(l) in (4200, 4300)
    with pytest.raises(ValueError):
        lo.e4_of_lo(l, side="up")


def test_quantization_clamps_to_legal_band():
    assert lo.e4_of_lo(-99.0, side="bid") == 100     # never below 1c
    assert lo.e4_of_lo(+99.0, side="ask") == 9900    # never above 99c


def test_apply_halfwidth_widens_and_rejects_negative():
    fair = lo.lo_of_e4(5000)
    bid, ask = lo.apply_halfwidth(fair, 0.10)
    assert bid < 5000 < ask
    b2, a2 = lo.apply_halfwidth(fair, 0.30)
    assert b2 <= bid and a2 >= ask                    # wider δ never tightens
    assert lo.apply_halfwidth(fair, 0.0) == (5000, 5000)
    with pytest.raises(ValueError):
        lo.apply_halfwidth(fair, -0.01)


def test_subcent_tick_grid():
    """deci-cent structures pass tick_e4=10: round-trip holds there too."""
    e4 = 4550                                          # 45.5c, off cent grid
    l = lo.lo_of_e4(e4)
    assert lo.e4_of_lo(l, tick_e4=10, side="bid") == e4
    assert lo.e4_of_lo(l, side="bid") == 4500          # cent grid floors


# ─────────────────────────────────────────────────────── fees (Q3, S2)

def test_fee_schedule_rows_1c_50c_99c():
    """ceil_to_centicent(0.07·C·P·(1−P)) per kalshi_facts.yaml, C=1:
    P=0.50: 0.07·0.25 = 0.0175 exactly; P=0.01/0.99: 0.07·0.0099 =
    0.000693 -> ceil to $0.0007."""
    assert lo.trade_fee(0.50, 1) == pytest.approx(0.0175, abs=1e-12)
    assert lo.trade_fee(0.01, 1) == pytest.approx(0.0007, abs=1e-12)
    assert lo.trade_fee(0.99, 1) == pytest.approx(0.0007, abs=1e-12)


def _facts(tmp_path, verified, taker=0.07, maker=0.0175):
    p = tmp_path / "facts.yaml"
    p.write_text(
        "fees:\n  verified: %s\n  params:\n    taker_rate: %s\n"
        "    maker_rate: %s\n" % (str(verified).lower(), taker, maker))
    return str(p)


def test_gate_mode_fail_closed_until_verified(tmp_path):
    unver = _facts(tmp_path, verified=False)
    with pytest.raises(lo.FeeNotVerifiedError):
        lo.trade_fee(0.50, 1, gate_mode=True, facts_path=unver)
    with pytest.raises(lo.FeeNotVerifiedError):
        lo.maker_rate("quadratic_with_maker_fees", gate_mode=True,
                      facts_path=unver)
    # audit N3: the gate fires BEFORE the enum — even the "makers pay 0"
    # answer rests on unratified provenance, so quadratic must raise too
    with pytest.raises(lo.FeeNotVerifiedError):
        lo.maker_rate("quadratic", gate_mode=True, facts_path=unver)
    with pytest.raises(lo.FeeNotVerifiedError):
        lo.maker_fee(0.50, 1, "quadratic", gate_mode=True, facts_path=unver)
    ver = _facts(tmp_path, verified=True)
    assert lo.trade_fee(0.50, 1, gate_mode=True, facts_path=ver) == \
        pytest.approx(0.0175, abs=1e-12)
    # sentinel: repo yaml is verified=true since the OQ-1 ratification
    # (D-4 in PLAN_SPORTS_TRADING_DECISIONS.md, 2026-07-16) => gate COMPUTES
    # on the real config. If this raises, someone flipped fees.verified back
    # to false — check D-4 status before "fixing" the test.
    assert lo.trade_fee(0.50, 1, gate_mode=True) == \
        pytest.approx(0.0175, abs=1e-12), \
        "repo kalshi_facts.yaml gate-mode fee changed — check D-4 status"


def test_behavior_follows_the_yaml_never_hardcoded(tmp_path):
    doubled = _facts(tmp_path, verified=True, taker=0.14)
    assert lo.trade_fee(0.50, 1, facts_path=doubled) == \
        pytest.approx(0.0350, abs=1e-12)
    assert lo.fee_raw(0.50, 1, facts_path=doubled) == \
        pytest.approx(0.14 * 0.25, abs=1e-12)


def test_maker_rate_lookup_never_assumed(tmp_path):
    facts = _facts(tmp_path, verified=True, maker=0.0175)
    assert lo.maker_rate("quadratic", facts_path=facts) == 0.0
    assert lo.maker_rate("quadratic_with_maker_fees", facts_path=facts) == \
        pytest.approx(0.0175, abs=1e-12)
    for bad in ("flat", "surprise_new_type"):
        with pytest.raises(ValueError):
            lo.maker_rate(bad, facts_path=facts)
    assert lo.maker_fee(0.50, 1, "quadratic", facts_path=facts) == 0.0
    # 0.0175·0.25 = 0.004375 -> ceil_to_centicent = 0.0044
    assert lo.maker_fee(0.50, 1, "quadratic_with_maker_fees",
                        facts_path=facts) == pytest.approx(0.0044, abs=1e-12)


# ─────────────────────────────────────── Q9 signs on the PRE-ROUNDING curve

def test_fee_raw_consistent_with_trade_fee(tmp_path):
    """audit N4: the pre-rounding curve and the rounded fee must never
    diverge — ceil_to_centicent(fee_raw) == trade_fee, always."""
    import math as m
    facts = _facts(tmp_path, verified=True)
    for p in (0.01, 0.13, 0.50, 0.87, 0.99):
        for c in (1, 2.5, 100):
            raw = lo.fee_raw(p, c, facts_path=facts)
            assert m.ceil(round(raw * 10000.0, 6)) / 10000.0 == \
                pytest.approx(lo.trade_fee(p, c, facts_path=facts), abs=1e-15)


def test_nearest_mode_tie_break_is_half_up():
    """audit N5a: side=None rounds half-UP (floor(x+0.5)) — normative for
    the Phase-2 C++ port, not an accident."""
    # exact midpoint between 42c and 43c on the E4 scale is 4250
    l_mid = lo.logit(0.425)
    assert lo.e4_of_lo(l_mid) == 4300


def test_q9_fee_curve_symmetric_around_50c():
    for p in (0.01, 0.1, 0.25, 0.4):
        assert lo.fee_raw(p, 1) == pytest.approx(lo.fee_raw(1.0 - p, 1),
                                                 abs=1e-15)


def test_q9_fee_curve_vanishes_at_extremes_and_peaks_mid():
    assert lo.fee_raw(1e-9, 1) < 1e-9
    assert lo.fee_raw(1.0 - 1e-9, 1) < 1e-9
    mid = lo.fee_raw(0.5, 1)
    for p in (0.01, 0.2, 0.45, 0.55, 0.8, 0.99):
        assert lo.fee_raw(p, 1) < mid
