"""W-P3 acceptance (PLAN_PRICING_MODEL §3): the A-S quote generator.

The Q9 economic-sign battery, each acceptance point its own NAMED test (the
exit evidence lists all seven): reservation skew sign + monotonicity, spread
never narrows as risk grows, skew sign at both price extremes, δ widens toward
settlement + Q6 hard stop, cap(t) → 0 monotone, jump breaker on quote velocity
(not trade volume), and the Q8 anti-deadlock exit-side-never-suppressed.
"""
import math
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from pricing import quote as q, lo  # noqa: E402

H = 3600            # 1-hour market life
MID = lo.lo_of_e4(5000)   # fair at 50c (lo 0)


# ── (1) reservation skew: sign + monotone in |inventory| ────────────────

def test_q9_reservation_skew_sign_and_monotone():
    t = 1800          # mid-life, gamma_t > 0
    long = q.reservation_lo(MID, 10, t, H)
    short = q.reservation_lo(MID, -10, t, H)
    assert long < MID and short > MID            # long below fair, short above
    assert q.reservation_lo(MID, 20, t, H) < long   # magnitude monotone in inv
    assert q.reservation_lo(MID, -20, t, H) > short
    # flat inventory -> reservation exactly fair
    assert q.reservation_lo(MID, 0, t, H) == pytest.approx(MID, abs=1e-15)


# ── (2) spread never narrows as risk grows ──────────────────────────────

def test_q9_spread_never_narrows_with_inventory():
    t = 1800
    d0 = q.half_width_lo(t, H, inventory=0)
    d5 = q.half_width_lo(t, H, inventory=5)
    d20 = q.half_width_lo(t, H, inventory=20)
    assert d0 < d5 < d20                          # δ (=half the spread) widens
    # same via the emitted quote's delta_lo
    assert q.quote(MID, 20, t, H, cap_max=100)["delta_lo"] > \
        q.quote(MID, 0, t, H, cap_max=100)["delta_lo"]


# ── (3) skew sign correct at both price extremes (2c and 98c) ────────────

def test_q9_skew_sign_at_price_extremes():
    t = 1800
    for cents_e4 in (200, 9800):                  # 2c and 98c
        f = lo.lo_of_e4(cents_e4)
        assert q.reservation_lo(f, 10, t, H) < f   # long -> below fair, in lo
        assert q.reservation_lo(f, -10, t, H) > f  # short -> above fair
    # the 1c step near the edge is a real lo move (Q1), not swallowed:
    assert lo.lo_of_e4(200) != lo.lo_of_e4(100)


# ── (4) δ widens toward settlement; Q6 hard stop inside the window ───────

def test_q9_delta_widens_toward_settlement_and_q6_stop():
    far = q.half_width_lo(3000, H)
    near = q.half_width_lo(600, H)                 # closer to settlement
    assert near > far                              # δ(t2 nearer) >= δ(t1)
    # inside the Q6 window: no quoting at all (hard stop)
    stop = q.quote(MID, 0, t_remaining_s=200, horizon_s=H, cap_max=25)
    assert stop["quoting"] is False
    assert stop["bid_e4"] is None and stop["ask_e4"] is None
    assert stop["reason"] == "q6_settlement_window"
    # just outside the window it quotes again
    assert q.quote(MID, 0, t_remaining_s=400, horizon_s=H, cap_max=25)["quoting"]


# ── (5) cap(t) monotone nonincreasing to zero ───────────────────────────

def test_q9_cap_monotone_to_zero():
    caps = [q.cap_t(t, H, cap_max=25) for t in (3600, 3000, 1800, 600, 0)]
    assert all(caps[i] >= caps[i + 1] for i in range(len(caps) - 1))
    assert caps[0] == pytest.approx(25.0) and caps[-1] == 0.0


# ── (6) jump breaker on quote velocity, NOT trade volume ────────────────

def test_q9_jump_breaker_watches_quotes_not_volume():
    # BADAMS-shaped: 37c -> 28c over 5 min on ZERO trades -> trips
    lb, la = lo.logit(0.37), lo.logit(0.28)
    assert q.jump_breaker(lb, la, dt_s=300) is True
    # same window with a still book + only a trade-VOLUME spike -> no trip
    # (trade count is not even an input; lo-mid unchanged, no book updates)
    assert q.jump_breaker(lb, lb, dt_s=300, book_updates=0, window_s=300) is False
    # a book-update-RATE spike alone also trips (churn without a price move)
    assert q.jump_breaker(lb, lb, dt_s=1, book_updates=100, window_s=1) is True
    # audit N1: trade volume is accepted but IGNORED — a huge volume with a
    # still book must NOT trip (a volume-watching breaker would miss BADAMS)
    assert q.jump_breaker(lb, lb, dt_s=300, book_updates=0, window_s=300,
                          trade_volume=1e9) is False
    # audit N4: degenerate/non-finite inputs FAIL-CLOSED (trip = pull)
    assert q.jump_breaker(lb, la, dt_s=0) is True
    assert q.jump_breaker(float("nan"), la, dt_s=300) is True
    assert q.jump_breaker(lb, la, dt_s=300, window_s=0) is True
    # a tripped breaker pulls BOTH sides
    pulled = q.quote(MID, 0, 1800, H, cap_max=25, breaker_tripped=True)
    assert pulled["quoting"] is False
    assert pulled["bid_e4"] is None and pulled["ask_e4"] is None
    assert pulled["reason"] == "jump_breaker"


# ── (7) at max inventory, the EXIT side is NEVER suppressed (Q8) ─────────

def test_q9_max_inventory_exit_side_never_suppressed():
    t = 1800                                       # cap = 25*0.5 = 12.5
    long = q.quote(MID, inventory=25, t_remaining_s=t, horizon_s=H, cap_max=25)
    assert long["bid_e4"] is None                  # max long: stop buying
    assert long["ask_e4"] is not None              # exit (sell) STILL emitted
    assert long["quoting"] is True and long["reason"] == "max_long_exit_only"
    short = q.quote(MID, inventory=-25, t_remaining_s=t, horizon_s=H, cap_max=25)
    assert short["ask_e4"] is None                 # max short: stop selling
    assert short["bid_e4"] is not None             # exit (buy) STILL emitted
    assert short["reason"] == "max_short_exit_only"
    # audit N2: a garbage cap can NEVER trap the exit side. cap_max is
    # validated (negative/non-finite raises); and even at cap==0 (settlement)
    # a live LONG keeps its ask and a live SHORT keeps its bid.
    with pytest.raises(ValueError):
        q.quote(MID, 5, t, H, cap_max=-25)
    with pytest.raises(ValueError):
        q.quote(MID, 5, t, H, cap_max=float("inf"))
    # cap≈0 (q6 stop disabled here to isolate the cap logic): a live LONG
    # keeps its ask, a live SHORT keeps its bid, a FLAT book keeps both.
    long0 = q.quote(MID, inventory=5, t_remaining_s=1e-9, horizon_s=H,
                    cap_max=25, q6_window_s=0)
    assert long0["ask_e4"] is not None and long0["bid_e4"] is None
    short0 = q.quote(MID, inventory=-5, t_remaining_s=1e-9, horizon_s=H,
                     cap_max=25, q6_window_s=0)
    assert short0["bid_e4"] is not None and short0["ask_e4"] is None
    flat0 = q.quote(MID, inventory=0, t_remaining_s=1e-9, horizon_s=H,
                    cap_max=25, q6_window_s=0)
    assert flat0["bid_e4"] is not None and flat0["ask_e4"] is not None


# ── structural: two-sided emission maps to the legal grid ───────────────

def test_two_sided_emission_on_legal_grid():
    r = q.quote(MID, inventory=0, t_remaining_s=1800, horizon_s=H, cap_max=25)
    assert r["quoting"] and r["bid_e4"] < r["ask_e4"]
    # both land on the legal cent grid, inside the band
    for px in (r["bid_e4"], r["ask_e4"]):
        assert 100 <= px <= 9900 and px % 100 == 0
    # bid rounds DOWN, ask rounds UP around the reservation (conservative)
    assert r["bid_e4"] <= 5000 <= r["ask_e4"]
