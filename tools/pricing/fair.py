"""W-P2 — fair value estimator (PLAN_PRICING_MODEL §3).

fair = f(book, recent trades, sibling legs), expressed in LOG-ODDS space (Q1)
so a 1c move near the edge carries its true odds weight. Four components:

  (a) micro-price — the imbalance-weighted touch, computed IN LO-SPACE:
        fair_lo = (lo(bid)·ask_qty + lo(ask)·bid_qty) / (bid_qty + ask_qty)
      (the price-space micro-price is only the intuition/seed; the blend is
      done on the lo values). More size resting on the ASK pulls fair toward
      the BID and vice versa. A one-sided / empty / crossed book returns None
      (fail-closed — never fabricate a fair, S2).

  (b) taker-flow drift — a bounded short-horizon lo-shift from signed recent
      taker imbalance in [-1, +1] (buy-heavy ⇒ up, sell-heavy ⇒ down, zero ⇒
      no shift). SHAPE only: the coefficient + cap are Group-C calibration
      outputs, shipped here as NAMED PLACEHOLDERS.

  (c) bracket-sum constraint — the sibling legs of one mutually-exclusive
      event have yes-probabilities that must sum to 1 (a PROBABILITY-space
      identity, not a log-odds one). When they sum to S≠1 the excess (S-1) is
      redistributed across legs weighted by 1/depth, so THIN legs move most
      (a thin quote is the least trustworthy). A lone leg (no siblings) gets
      zero correction.

  (d) external_anchor_lo — a NAMED, inert input slot (default None): the
      Crypto spot-index anchor (MM_ROADMAP 1.5-A, deferred §5) bolts on here
      additively later as a lo-space blend. With no anchor (or weight 0) it
      does nothing; a nonzero weight pulls fair toward the anchor — wired now,
      fed later.

Pure math, numpy-free, no I/O, no DuckDB. Imports the W-P1 log-odds core
(frozen); adds no float on any money path that isn't already a probability.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pricing import lo  # noqa: E402  (frozen W-P1 core: logit/expit/e4 maps)

# ── Group-C calibration PLACEHOLDERS (shape here; numbers from mm_calibrate).
# Conservative small values: the drift term nudges, never dominates, until
# real toxicity/flow coefficients replace these.
DRIFT_COEFF_LO = 0.10        # PLACEHOLDER lo-shift per unit taker imbalance
DRIFT_CAP_LO = 0.30          # PLACEHOLDER hard bound on |drift| (≈ one odds notch)


def _valid_book(bid_e4, ask_e4, bid_qty, ask_qty):
    """A two-sided, positive-size, non-crossed book in the legal price range.
    Anything else is not a quotable market — fail closed."""
    if None in (bid_e4, ask_e4, bid_qty, ask_qty):
        return False
    if bid_qty <= 0 or ask_qty <= 0:
        return False
    return 0 < bid_e4 < ask_e4 < 10000


def micro_price_lo(bid_e4, ask_e4, bid_qty, ask_qty):
    """Imbalance-weighted fair in log-odds space, or None (fail-closed) for a
    one-sided / empty / crossed book. bid/ask are E4 prices; qtys are any
    positive units (E4 or raw — only the ratio matters)."""
    if not _valid_book(bid_e4, ask_e4, bid_qty, ask_qty):
        return None
    lo_bid = lo.lo_of_e4(bid_e4)
    lo_ask = lo.lo_of_e4(ask_e4)
    return (lo_bid * ask_qty + lo_ask * bid_qty) / (bid_qty + ask_qty)


def taker_flow_drift_lo(taker_imbalance, coeff=DRIFT_COEFF_LO, cap=DRIFT_CAP_LO):
    """Signed taker imbalance in [-1, +1] -> bounded lo-drift. Buy-heavy
    (positive) drifts fair UP, sell-heavy DOWN, zero -> exactly 0. Clamped to
    ±cap. SHAPE only (coeff/cap are Group-C placeholders)."""
    if taker_imbalance is None:
        return 0.0
    ti = -1.0 if taker_imbalance < -1.0 else 1.0 if taker_imbalance > 1.0 else taker_imbalance
    d = coeff * ti
    return -cap if d < -cap else cap if d > cap else d


def bracket_corrections(legs):
    """legs: list of (prob, depth) for the sibling yes-outcomes of one event.
    Returns a list of PROBABILITY-space corrections to SUBTRACT from each
    leg's prob so the set sums to 1, redistributed by 1/depth (thin legs move
    most). Sum of corrections == the excess (S-1). A lone leg -> [0.0]; an
    already-consistent set (S==1) -> all zeros. depth must be > 0."""
    n = len(legs)
    if n <= 1:
        return [0.0] * n
    probs = [p for p, _ in legs]
    depths = [d for _, d in legs]
    if any(d <= 0 for d in depths):
        raise ValueError("bracket leg depth must be > 0 (thin != zero)")
    excess = sum(probs) - 1.0
    if excess == 0.0:
        return [0.0] * n
    weights = [1.0 / d for d in depths]     # thin (small depth) -> big weight
    wsum = sum(weights)
    return [excess * w / wsum for w in weights]


def apply_bracket_to_fair_lo(fair_lo_value, correction_prob):
    """Apply one leg's probability-space bracket correction to its lo fair:
    p = clip(expit(fair) - correction); return logit(p). A zero correction is
    an exact no-op."""
    if not correction_prob:
        return fair_lo_value
    p = lo.expit(fair_lo_value) - correction_prob
    return lo.logit(lo.clip_p(p))


def fair_lo(bid_e4, ask_e4, bid_qty, ask_qty, taker_imbalance=0.0,
            bracket_correction_prob=0.0, external_anchor_lo=None,
            anchor_weight=0.0, drift_coeff=DRIFT_COEFF_LO,
            drift_cap=DRIFT_CAP_LO):
    """Compose the fair value in log-odds space. Returns a lo float, or None
    (fail-closed) when the book is not quotable.

    fair = micro_price_lo + taker_flow_drift  [+ bracket correction]
                                              [blended toward external anchor]
    With taker_imbalance=0, no bracket correction, and no anchor, fair is
    EXACTLY the micro-price (the Q9 zero-imbalance identity)."""
    mp = micro_price_lo(bid_e4, ask_e4, bid_qty, ask_qty)
    if mp is None:
        return None
    f = mp + taker_flow_drift_lo(taker_imbalance, drift_coeff, drift_cap)
    if bracket_correction_prob:
        f = apply_bracket_to_fair_lo(f, bracket_correction_prob)
    if external_anchor_lo is not None and anchor_weight:
        w = 0.0 if anchor_weight < 0.0 else 1.0 if anchor_weight > 1.0 else anchor_weight
        f = (1.0 - w) * f + w * external_anchor_lo
    return f
