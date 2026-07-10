"""W-P2 acceptance (PLAN_PRICING_MODEL §3): the fair value estimator.

Hand-computed micro-prices in log-odds space (incl. the fail-closed
one-sided/empty/crossed cases → None, never a fabricated fair); the Q9 sign
trio (drift sign, bracket sign, lone-leg zero); and the inert external-anchor
slot wired for later.
"""
import math
import os
import sys

import pytest

INF = float("inf")
NAN = float("nan")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from pricing import fair, lo  # noqa: E402


# ───────────────────────────────── (a) micro-price, hand-computed + fail-closed

def test_micro_price_balanced_symmetric_is_50c():
    # 40c/60c, equal size -> the odds cancel -> fair = 50c (lo 0)
    mp = fair.micro_price_lo(4000, 6000, 100, 100)
    assert mp == pytest.approx(0.0, abs=1e-9)
    assert lo.expit(mp) == pytest.approx(0.50, abs=1e-9)


def test_micro_price_imbalance_pulls_toward_the_thin_side():
    # heavy ASK size (sellers) pulls fair toward the BID (down). Hand:
    # lo(.40)=-ln(1.5), lo(.60)=+ln(1.5); (lo_bid·300 + lo_ask·100)/400
    l = math.log(1.5)
    expect = (-l * 300 + l * 100) / 400.0
    mp = fair.micro_price_lo(4000, 6000, 100, 300)
    assert mp == pytest.approx(expect, abs=1e-12)
    assert mp < 0 and lo.expit(mp) < 0.50          # below mid: sellers dominate
    # mirror: heavy BID size pulls fair UP, by exactly the same magnitude
    mp2 = fair.micro_price_lo(4000, 6000, 300, 100)
    assert mp2 == pytest.approx(-expect, abs=1e-12)


def test_micro_price_fail_closed_on_bad_books():
    assert fair.micro_price_lo(None, 6000, 100, 100) is None    # one-sided
    assert fair.micro_price_lo(4000, None, 100, 100) is None
    assert fair.micro_price_lo(4000, 6000, 0, 100) is None      # empty side
    assert fair.micro_price_lo(4000, 6000, 100, 0) is None
    assert fair.micro_price_lo(6000, 4000, 100, 100) is None    # crossed
    assert fair.micro_price_lo(5000, 5000, 100, 100) is None    # locked
    assert fair.micro_price_lo(0, 6000, 100, 100) is None       # off-band
    assert fair.micro_price_lo(4000, 10000, 100, 100) is None


def test_micro_price_nan_inf_fail_closed():
    # audit B1: NaN <= 0 is False, so a non-finite qty must be rejected
    # explicitly — never fabricate a nan fair (S2).
    assert fair.micro_price_lo(4000, 6000, NAN, 100) is None
    assert fair.micro_price_lo(4000, 6000, 100, INF) is None
    assert fair.micro_price_lo(NAN, 6000, 100, 100) is None
    assert fair.micro_price_lo(4000, INF, 100, 100) is None
    assert fair.fair_lo(4000, 6000, NAN, 100, taker_imbalance=0.5) is None


# ───────────────────────────────── (b) Q9 sign #1: taker-flow drift

def test_q9_drift_sign_and_zero_identity():
    book = (4000, 6000, 100, 100)
    micro = fair.micro_price_lo(*book)
    # zero imbalance -> fair EXACTLY the micro-price (the identity)
    assert fair.fair_lo(*book, taker_imbalance=0.0) == pytest.approx(micro, abs=1e-15)
    # buy-heavy -> fair strictly ABOVE micro; sell-heavy -> strictly BELOW
    assert fair.fair_lo(*book, taker_imbalance=0.5) > micro
    assert fair.fair_lo(*book, taker_imbalance=-0.5) < micro
    # monotone in the imbalance magnitude
    assert fair.fair_lo(*book, taker_imbalance=0.8) > fair.fair_lo(*book, taker_imbalance=0.3)


def test_drift_bounded_and_clamped():
    assert fair.taker_flow_drift_lo(0.0) == 0.0
    assert fair.taker_flow_drift_lo(0.5) == pytest.approx(0.05, abs=1e-12)
    assert fair.taker_flow_drift_lo(-0.5) == pytest.approx(-0.05, abs=1e-12)
    # imbalance clamped to [-1,1]; drift clamped to ±cap
    assert fair.taker_flow_drift_lo(10.0, coeff=0.5) == pytest.approx(0.30, abs=1e-12)
    assert fair.taker_flow_drift_lo(-10.0, coeff=0.5) == pytest.approx(-0.30, abs=1e-12)


# ───────────────────────────────── (c) Q9 sign #2: bracket-sum constraint

def test_q9_bracket_downward_total_excess_thin_moves_most():
    # 3 legs summing to 1.08; the third is THIN (depth 10 vs 100)
    legs = [(0.40, 100), (0.40, 100), (0.28, 10)]
    corr = fair.bracket_corrections(legs)
    assert all(c > 0 for c in corr)                 # all DOWNWARD (subtract)
    assert sum(corr) == pytest.approx(0.08, abs=1e-12)   # total == the excess
    assert corr[2] > corr[0] and corr[2] > corr[1]  # thin leg moves most
    # applying the corrections drives the corrected sum to exactly 1
    corrected = [p - c for (p, _), c in zip(legs, corr)]
    assert sum(corrected) == pytest.approx(1.0, abs=1e-12)


def test_bracket_undervalued_set_corrects_upward():
    # sum 0.90 (excess -0.10): corrections are NEGATIVE (raise each leg)
    corr = fair.bracket_corrections([(0.30, 50), (0.30, 50), (0.30, 50)])
    assert all(c < 0 for c in corr)
    assert sum(corr) == pytest.approx(-0.10, abs=1e-12)


def test_q9_bracket_lone_leg_zero_and_consistent_set_zero():
    assert fair.bracket_corrections([(0.7, 50)]) == [0.0]        # no siblings
    z = fair.bracket_corrections([(0.5, 10), (0.5, 10)])          # already sums to 1
    assert z == [0.0, 0.0]


def test_bracket_zero_depth_rejected():
    with pytest.raises(ValueError):
        fair.bracket_corrections([(0.5, 0), (0.6, 10)])


def test_bracket_infeasible_dislocation_fails_closed():
    # audit D1: a thin leg (depth 1) with a huge excess would need a
    # correction bigger than its own prob -> corrected prob negative. Refuse
    # (fail-closed) rather than silently clip to a wrong sum.
    with pytest.raises(ValueError, match="infeasible"):
        fair.bracket_corrections([(0.95, 1000), (0.95, 1000), (0.02, 1)])
    # and a feasible-but-large case still corrects to an exact sum of 1
    legs = [(0.55, 100), (0.55, 100)]     # sum 1.10, excess 0.10, symmetric
    corr = fair.bracket_corrections(legs)
    corrected = [p - c for (p, _), c in zip(legs, corr)]
    assert sum(corrected) == pytest.approx(1.0, abs=1e-12)
    assert all(0.01 <= cp <= 0.99 for cp in corrected)


def test_apply_bracket_to_fair_lo():
    f0 = lo.lo_of_e4(5000)                            # 50c
    # a downward correction of 0.05 prob -> 45c
    f1 = fair.apply_bracket_to_fair_lo(f0, 0.05)
    assert lo.expit(f1) == pytest.approx(0.45, abs=1e-9)
    assert fair.apply_bracket_to_fair_lo(f0, 0.0) == f0   # zero = exact no-op


# ───────────────────────────────── (d) external anchor slot (inert now)

def test_external_anchor_slot_inert_by_default_wired_for_later():
    book = (4000, 6000, 100, 100)
    micro = fair.micro_price_lo(*book)
    # slot present but inert: an anchor with weight 0 (or no weight) does nothing
    assert fair.fair_lo(*book, external_anchor_lo=2.0) == pytest.approx(micro, abs=1e-15)
    assert fair.fair_lo(*book, external_anchor_lo=2.0, anchor_weight=0.0) \
        == pytest.approx(micro, abs=1e-15)
    # a nonzero weight pulls fair toward the anchor (proves it bolts on later)
    blended = fair.fair_lo(*book, external_anchor_lo=2.0, anchor_weight=0.5)
    assert blended == pytest.approx(0.5 * micro + 0.5 * 2.0, abs=1e-12)
    assert blended > micro                            # pulled up toward 2.0


def test_fair_lo_fail_closed_propagates():
    assert fair.fair_lo(6000, 4000, 100, 100) is None   # crossed book -> None
    assert fair.fair_lo(4000, 6000, 0, 100, taker_imbalance=0.5) is None
