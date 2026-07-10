"""W-P3 — Avellaneda-Stoikov quote generator (PLAN_PRICING_MODEL §3).

quotes = g(fair_lo, inventory, time-to-settlement, market state), all offsets
and radii computed in LOG-ODDS space (Q1) and mapped to legal cents only at
emission. Composition:

  reservation_lo = fair_lo − inventory·γ(t)          (skew away from risk)
  half-width δ_lo = δ_base + vol/toxicity + |inventory| widening, then scaled
                    by a time factor that WIDENS toward settlement
  bid/ask        = clamp(reservation ∓ δ) to the legal grid (via W-P1)
  inventory cap  cap(t) shrinks to zero at settlement; at max inventory the
                 RISK-ADDING side is suppressed but the EXIT side is NEVER
                 suppressed (Q8 anti-deadlock — the rodlaf deadlock)
  Q6 hard stop   inside the settlement-convergence window, quoting stops
                 entirely (no quotes returned)
  jump breaker   watches lo-mid VELOCITY + book-update RATE (never trade
                 volume — the BADAMS cricket episode was a 37¢→28¢ 5-minute
                 reprice on ZERO trades); a trip pulls BOTH sides (defense
                 only; momentum-taking is a different, separately-validated
                 strategy, forbidden here per MM_ROADMAP 1.5-B)

All coefficients are NAMED Group-C calibration PLACEHOLDERS (shape here;
numbers from mm_calibrate). Pure math; imports the FROZEN W-P1 lo core and
W-P2 fair module; writes neither. Emits quotes only — transmits nothing.

NOTE (audit N3): "spread never narrows as risk grows" is the invariant in
LO-space (half_width_lo is monotone in |inventory|). The EMITTED cent spread
can still floor at the legal band edge — e.g. a large long near 2c skews the
reservation into the 1c floor, collapsing the cent spread. That is the
economically-correct "dump inventory against the floor" behavior, not a
regression; the monotone guarantee is on δ_lo, and the emission clamps are
conservative (bid floors, ask ceils, band-clamped).
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pricing import lo  # noqa: E402  (frozen W-P1 core)

# ── Group-C calibration PLACEHOLDERS (shape; numbers from mm_calibrate) ──────
GAMMA_BASE_LO = 0.020      # lo reservation skew per unit inventory, at full time
DELTA_BASE_LO = 0.150      # base half-width (lo)
INV_WIDEN_LO = 0.004       # extra half-width per unit |inventory| (spread never
                           # narrows as risk grows)
VOL_COEFF = 1.0            # realized-vol (lo) contribution to half-width
TOX_COEFF = 1.0            # toxicity (lo) contribution to half-width
SETTLE_WIDEN = 1.0         # δ scales up to (1+this)x as t_remaining -> 0
Q6_WINDOW_S = 300          # no quoting inside this window before close (Q6)
JUMP_LO_VELOCITY_THRESH = 0.0010   # |Δlo_mid|/s that trips the jump breaker
JUMP_BOOK_RATE_THRESH = 20.0       # book updates/s that trips the jump breaker


def _t_frac(t_remaining_s, horizon_s):
    """Remaining-time fraction in [0, 1] (1 = full life, 0 = at settlement)."""
    if horizon_s <= 0:
        return 0.0
    f = t_remaining_s / horizon_s
    return 0.0 if f < 0.0 else 1.0 if f > 1.0 else f


def gamma_t(t_remaining_s, horizon_s, gamma_base=GAMMA_BASE_LO):
    """Inventory risk coefficient. Per MM_ROADMAP 1.5-B the reservation skew is
    inventory × coeff × remaining_time (the A-S variance-risk argument: the
    penalty scales with the variance still to be borne, → 0 at settlement).
    Always >= 0."""
    return gamma_base * _t_frac(t_remaining_s, horizon_s)


def reservation_lo(fair_lo, inventory, t_remaining_s, horizon_s,
                   gamma_base=GAMMA_BASE_LO):
    """fair shifted AWAY from risk: long inventory (inv>0) => reservation
    strictly BELOW fair (sell more eagerly), short => ABOVE. Magnitude monotone
    in |inventory|. In lo-space so a 1c step near the edge keeps its odds
    weight (Q1)."""
    return fair_lo - inventory * gamma_t(t_remaining_s, horizon_s, gamma_base)


def half_width_lo(t_remaining_s, horizon_s, inventory=0.0, vol_lo=0.0,
                  tox_lo=0.0, delta_base=DELTA_BASE_LO, settle_widen=SETTLE_WIDEN):
    """δ in lo-space. Base + vol + toxicity + a |inventory| widening (so the
    quoted spread never NARROWS as risk grows), all scaled by a time factor
    that WIDENS toward settlement. Always > 0."""
    base = (delta_base + VOL_COEFF * abs(vol_lo) + TOX_COEFF * abs(tox_lo)
            + INV_WIDEN_LO * abs(inventory))
    time_factor = 1.0 + settle_widen * (1.0 - _t_frac(t_remaining_s, horizon_s))
    return base * time_factor


def cap_t(t_remaining_s, horizon_s, cap_max):
    """Inventory cap that shrinks monotonically to zero at settlement (early:
    a small net position allowed; tail: driven flat). cap(0) = 0."""
    return cap_max * _t_frac(t_remaining_s, horizon_s)


def jump_breaker(lo_mid_prev, lo_mid_now, dt_s, book_updates=0, window_s=1.0,
                 trade_volume=0.0, lo_vel_thresh=JUMP_LO_VELOCITY_THRESH,
                 book_rate_thresh=JUMP_BOOK_RATE_THRESH):
    """Trips on lo-mid VELOCITY or book-update RATE. Returns True = pull both
    sides (defense).

    `trade_volume` is accepted and DELIBERATELY IGNORED (audit N1): it exists
    only to make the "the breaker never trips on trade volume" contract
    machine-testable — a caller can pass a huge volume and prove no trip. The
    BADAMS cricket episode was a 37¢→28¢ reprice on ZERO trades; a
    volume-watching breaker would miss it entirely.

    FAIL-CLOSED (audit N4): a non-finite input or a degenerate time basis
    (dt/window <= 0) is an ambiguous market state — a safety breaker's safe
    action is to PULL (trip), never to keep quoting on data it can't assess."""
    del trade_volume  # ignored by contract
    if not all(math.isfinite(v) for v in
               (lo_mid_prev, lo_mid_now, dt_s, book_updates, window_s)):
        return True
    if dt_s <= 0 or window_s <= 0:
        return True
    lo_velocity = abs(lo_mid_now - lo_mid_prev) / dt_s
    book_rate = book_updates / window_s
    return lo_velocity >= lo_vel_thresh or book_rate >= book_rate_thresh


def quote(fair_lo, inventory, t_remaining_s, horizon_s, cap_max,
          vol_lo=0.0, tox_lo=0.0, q6_window_s=Q6_WINDOW_S, breaker_tripped=False,
          tick_e4=lo.TICK_E4, gamma_base=GAMMA_BASE_LO, delta_base=DELTA_BASE_LO,
          settle_widen=SETTLE_WIDEN):
    """Emit a quote for the current state.

    Returns a dict:
      {"quoting": bool, "bid_e4": int|None, "ask_e4": int|None, "reason": str,
       "reservation_lo": float|None, "delta_lo": float|None, "cap": float}
    quoting=False (both sides None) for a Q6 hard stop or a tripped breaker.
    At/over the inventory cap the RISK-ADDING side is None but the EXIT side is
    always emitted (Q8)."""
    if not (isinstance(cap_max, (int, float)) and math.isfinite(cap_max)
            and cap_max >= 0):
        raise ValueError("cap_max must be finite and >= 0, got %r" % (cap_max,))
    cap = cap_t(t_remaining_s, horizon_s, cap_max)

    # Q6: no quoting inside the settlement-convergence window (hard stop).
    if t_remaining_s <= q6_window_s:
        return {"quoting": False, "bid_e4": None, "ask_e4": None,
                "reason": "q6_settlement_window", "reservation_lo": None,
                "delta_lo": None, "cap": cap}
    # jump breaker: pull BOTH sides (defense only).
    if breaker_tripped:
        return {"quoting": False, "bid_e4": None, "ask_e4": None,
                "reason": "jump_breaker", "reservation_lo": None,
                "delta_lo": None, "cap": cap}

    res = reservation_lo(fair_lo, inventory, t_remaining_s, horizon_s, gamma_base)
    delta = half_width_lo(t_remaining_s, horizon_s, inventory, vol_lo, tox_lo,
                          delta_base, settle_widen)
    bid_e4, ask_e4 = lo.apply_halfwidth(res, delta, tick_e4)

    # inventory-cap suppression, keyed on the SIGN of inventory so the EXIT
    # side can NEVER be suppressed (audit N2 — a cap comparison alone could
    # suppress both sides on garbage input and TRAP a live position, the exact
    # rodlaf deadlock Q8 forbids). A LONG only ever loses its bid (stop adding
    # long); a SHORT only ever loses its ask; a FLAT book is never suppressed.
    reason = "two_sided"
    if inventory > 0 and inventory >= cap:      # long at/over cap: keep the ask (exit)
        bid_e4 = None
        reason = "max_long_exit_only"
    elif inventory < 0 and -inventory >= cap:   # short at/over cap: keep the bid (exit)
        ask_e4 = None
        reason = "max_short_exit_only"

    return {"quoting": bid_e4 is not None or ask_e4 is not None,
            "bid_e4": bid_e4, "ask_e4": ask_e4, "reason": reason,
            "reservation_lo": res, "delta_lo": delta, "cap": cap}
