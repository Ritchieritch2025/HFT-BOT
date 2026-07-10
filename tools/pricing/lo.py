"""W-P1 — log-odds core (PLAN_PRICING_MODEL §3).

The numeric foundation the fair-value estimator (W-P2) and quote generator
(W-P3) import. Everything strategy-shaped happens in LOG-ODDS space
(GUARDRAILS Q1): 50c→49c is a trivial lo shift (≈−0.04) while 2c→1c doubles
the odds (≈−0.70) — price-space math on probability contracts is a reject.

Conventions:
  prices    E4 integers (dollars × 10000) — the warehouse/engine convention
            (D5: no floats on money). Legal quote grid: 1c..99c in `tick_e4`
            steps (default 100 = linear_cent; sub-cent structures pass a
            smaller tick).
  log-odds  float `lo` = logit(P) with P clipped to [P_MIN, P_MAX] = [0.01,
            0.99] (the legal price band; the clip also keeps logit finite).
  fees      NEVER computed here from hardcoded constants. The taker-fee
            formula + rounding regime live in config/kalshi_facts.yaml and
            are applied by mm_research.trade_fee (single source of truth,
            WP-05 contract) — this module delegates and re-exports, adding
            only the maker-side lookup and the PRE-ROUNDING curve needed by
            the Q9 sign tests. Gate-mode computation fail-closes while
            fees.verified != true (S2; OQ-1 pending).

Quantization direction is conservatism-aware: a BID rounds DOWN to the grid
and an ASK rounds UP — off-grid quotes always widen, never tighten, so
float fuzz can only cost edge, never invent it (Q2 spirit).

NORMATIVE for the Phase-2 C++ port (audit N5): side=None ties round HALF-UP
(floor(x+0.5)); the _EPS=1e-9 grid-snap guard is part of the spec — a
genuine off-grid value within 1e-9 tick of a grid line snaps onto it
(worst-case "tighten" ≈ $1e-11, deliberate and immaterial); fee functions
return FLOAT dollars (WP-05 convention inherited from mm_research) — the
C++ port must land fees on an integer centicent grid at its E4 boundary.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mm_research import (FeeNotVerifiedError, load_fee_facts,  # noqa: F401,E402
                         trade_fee)

P_MIN = 0.01                 # legal price band = clip band (1c..99c)
P_MAX = 0.99
TICK_E4 = 100                # linear_cent default; deci-cent structures pass 10
_E4 = 10000.0
_EPS = 1e-9                  # float-fuzz guard for grid quantization


# ─────────────────────────────────────────────── logit / expit / E4 maps

def clip_p(p):
    """Probability clipped to the legal [P_MIN, P_MAX] band."""
    return P_MIN if p < P_MIN else P_MAX if p > P_MAX else p


def logit(p):
    """log-odds of a probability, clipped to the legal band (finite always)."""
    p = clip_p(p)
    return math.log(p / (1.0 - p))


def expit(lo):
    """Inverse of logit, then clipped to the legal band."""
    # numerically stable both directions
    if lo >= 0:
        p = 1.0 / (1.0 + math.exp(-lo))
    else:
        e = math.exp(lo)
        p = e / (1.0 + e)
    return clip_p(p)


def lo_of_e4(price_e4):
    """E4 price -> log-odds (clipped). Accepts any int in (0, 10000)."""
    return logit(price_e4 / _E4)


def e4_of_lo(lo, tick_e4=TICK_E4, side=None):
    """log-odds -> legal E4 grid price.

    side='bid' rounds DOWN, side='ask' rounds UP (conservative: off-grid
    always widens the quote); side=None rounds to nearest. Result is clamped
    into the legal band [P_MIN, P_MAX] re-expressed on the grid."""
    x = expit(lo) * _E4 / tick_e4
    if side == "bid":
        k = math.floor(x + _EPS)
    elif side == "ask":
        k = math.ceil(x - _EPS)
    elif side is None:
        k = math.floor(x + 0.5)
    else:
        raise ValueError("side must be 'bid', 'ask' or None, got %r" % (side,))
    e4 = int(k) * tick_e4
    lo_grid = int(math.ceil(P_MIN * _E4 / tick_e4 - _EPS)) * tick_e4
    hi_grid = int(math.floor(P_MAX * _E4 / tick_e4 + _EPS)) * tick_e4
    return max(lo_grid, min(hi_grid, e4))


def apply_halfwidth(fair_lo, delta_lo, tick_e4=TICK_E4):
    """(bid_e4, ask_e4) = grid-quantized fair ∓ δ, both in lo-space.

    delta_lo must be >= 0 (a negative half-width would cross the quotes —
    programming error, fail loudly)."""
    if delta_lo < 0:
        raise ValueError("delta_lo must be >= 0, got %r" % (delta_lo,))
    bid = e4_of_lo(fair_lo - delta_lo, tick_e4, side="bid")
    ask = e4_of_lo(fair_lo + delta_lo, tick_e4, side="ask")
    return bid, ask


# ──────────────────────────────────────────────────────────────── fees
# taker fee: delegated to mm_research.trade_fee (formula + ceil_to_centicent
# rounding regime read from kalshi_facts.yaml at call time; gate_mode raises
# FeeNotVerifiedError while fees.verified != true). Re-exported above.

def fee_raw(price_dollars, contracts, rate=None, fee_multiplier=1.0,
            facts_path=None):
    """PRE-ROUNDING fee curve: rate · fee_multiplier · C · P · (1−P), dollars.

    Exists for the Q9 sign tests (symmetry around 50c, → 0 at the extremes)
    — post-rounding fees have a one-tick floor, so limit properties only
    hold before rounding. The rate still comes from the yaml, never a
    constant in code."""
    fees = load_fee_facts(facts_path)
    r = float(rate) if rate is not None else float(fees["params"]["taker_rate"])
    return r * fee_multiplier * contracts * price_dollars * (1.0 - price_dollars)


def maker_rate(fee_type, facts_path=None, gate_mode=False):
    """Maker fee RATE by series fee_type (Q3: looked up, never assumed 0).

    quadratic                  -> 0.0 (verified enum; makers pay nothing)
    quadratic_with_maker_fees  -> params.maker_rate from the yaml
    anything else (incl. flat) -> ValueError, fail-closed: semantics are not
                                  verified, so no number is produced (S2).
    gate_mode mirrors trade_fee: refuses while fees.verified != true."""
    fees = load_fee_facts(facts_path)
    if gate_mode and fees.get("verified") is not True:
        raise FeeNotVerifiedError(
            "fees.verified=%r — gate-mode maker-rate lookup refused until "
            "OQ-1 ratification" % (fees.get("verified"),))
    if fee_type == "quadratic":
        return 0.0
    if fee_type == "quadratic_with_maker_fees":
        return float(fees["params"]["maker_rate"])
    raise ValueError("unverified fee_type %r — refusing to guess a maker "
                     "rate (fail-closed)" % (fee_type,))


def maker_fee(price_dollars, contracts, fee_type, fee_multiplier=1.0,
              gate_mode=False, facts_path=None):
    """Maker fee in dollars, same formula + rounding regime as the taker fee
    (delegated to trade_fee with the maker rate). quadratic series -> exactly
    0.0 without rounding artifacts."""
    r = maker_rate(fee_type, facts_path=facts_path, gate_mode=gate_mode)
    if r == 0.0:
        return 0.0
    return trade_fee(price_dollars, contracts, rate=r,
                     fee_multiplier=fee_multiplier, gate_mode=gate_mode,
                     facts_path=facts_path)
