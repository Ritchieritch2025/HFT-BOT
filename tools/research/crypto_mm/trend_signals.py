#!/usr/bin/env python3
"""trend_signals.py — anti-adverse-selection signal kit for the MM engine.

Pure functions, no I/O, no engine imports. Designed to be wired into the
QUOTE_EVAL path of mm_engine.py (see WO_trend_adverse_selection_20260726.md).

Sign conventions
----------------
* Prices S are index dollars; fair/edge are in cents unless suffixed _p (prob).
* momentum z > 0  => index rising  => YES side is the "with-trend" side.
* side: +1 = we are bidding YES (long-p exposure), -1 = bidding NO.

Every threshold has a default from the 2026-07-26 shadow calibration but MUST
be re-fit from local data (see calibrate section in the workorder).
"""
import math
from collections import deque

PHI_SQRT2 = math.sqrt(2.0)


def _phi(x):    # standard normal pdf
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _Phi(x):    # standard normal cdf
    return 0.5 * (1.0 + math.erf(x / PHI_SQRT2))


# ---------------------------------------------------------------------------
# 1. Momentum (multi-horizon z-score)
# ---------------------------------------------------------------------------
def momentum_z(prices, sigma, horizons=(5, 15, 30), weights=(0.5, 0.3, 0.2)):
    """Composite momentum z-score from a 1s-sampled price deque (newest last).

    z_tau = (S_t - S_{t-tau}) / (sigma * sqrt(tau))   -- units of 'sigmas'
    z_mom = sum_i w_i * z_tau_i

    |z_mom| ~ 1 is normal drift noise; |z_mom| > 2 means the index is moving
    directionally faster than the vol model expects => informed-flow regime.
    Returns 0.0 when history or sigma is insufficient (fail-safe neutral).
    """
    if sigma is None or sigma <= 0 or len(prices) < max(horizons) + 1:
        return 0.0
    s_t = prices[-1]
    z = 0.0
    for tau, w in zip(horizons, weights):
        z += w * (s_t - prices[-1 - tau]) / (sigma * math.sqrt(tau))
    return z


# ---------------------------------------------------------------------------
# 2. Jump / regime gate
# ---------------------------------------------------------------------------
def jump_flag(last_increment, sigma_short, k=4.0):
    """True if the latest 1s move is a jump under the local vol model.

    Calibration basis: clean BRTI series showed |z|>4 at 224x the Gaussian
    rate. A 4-sigma print is far more likely 'jump regime' than noise.
    """
    if sigma_short is None or sigma_short <= 0:
        return True     # cannot judge => treat as unsafe
    return abs(last_increment) > k * sigma_short


def rv_bv_ratio(increments):
    """Realized-variance / bipower-variation over a window (jump detector).

    BV = (pi/2) * mean(|d_i||d_{i-1}|) estimates diffusion variance robustly
    to jumps; RV includes jumps. ratio >> 1 => jumps present in window.
    Suggested gate: ratio > 1.5 on the 60s window => widen or pull.
    """
    n = len(increments)
    if n < 10:
        return 1.0
    rv = sum(d * d for d in increments) / n
    bv = (math.pi / 2.0) * sum(abs(increments[i]) * abs(increments[i - 1])
                               for i in range(1, n)) / (n - 1)
    if bv <= 0:
        return float("inf")
    return rv / bv


class JumpCooldown:
    """Pull both sides on a jump; re-admit quoting after calm_s of no jumps."""

    def __init__(self, calm_s=25.0):
        self.calm_s = calm_s
        self._last_jump_t = -1e18

    def update(self, t_s, is_jump):
        if is_jump:
            self._last_jump_t = t_s

    def quoting_allowed(self, t_s):
        return (t_s - self._last_jump_t) >= self.calm_s


# ---------------------------------------------------------------------------
# 3. Drift-adjusted fair value
# ---------------------------------------------------------------------------
def drift_adjusted_mu(S, mom_z, sigma, beta=0.35, horizon_s=30.0, cap_sigmas=1.5):
    """Shift the random-walk mean by the expected continuation of momentum.

    mu = S + beta * mom_z * sigma * sqrt(horizon_s), capped at cap_sigmas.

    beta is the empirical continuation coefficient: regress (S_{t+h} - S_t)
    on mom_z * sigma * sqrt(h) over the clean BRTI series. Use ridge/shrink;
    if the fit is unstable, set beta=0 and rely on the asymmetric edge gate
    (section 4), which does not need a fair shift to work.
    """
    if sigma is None or sigma <= 0:
        return S
    shift = beta * mom_z * sigma * math.sqrt(horizon_s)
    cap = cap_sigmas * sigma * math.sqrt(horizon_s)
    return S + max(-cap, min(cap, shift))


def fair_price_sensitivity_c(mu, K, variance):
    """dP/dS in cents per index-dollar: 100 * phi(d) / sqrt(V).

    Large near the money, ~0 in extreme zones. Reused by skew and sizing.
    """
    if variance <= 0:
        return 0.0
    d = (mu - K) / math.sqrt(variance)
    return 100.0 * _phi(d) / math.sqrt(variance)


# ---------------------------------------------------------------------------
# 4. Asymmetric edge requirement (the actual knife-catcher killer)
# ---------------------------------------------------------------------------
def required_edge_c(base_edge_c, side, mom_z, dpds_c, sigma,
                    hold_s, gamma=1.0, hard_z=2.5):
    """Minimum edge (cents) to quote `side` given current momentum.

    expected adverse fair move while we wait to be filled:
        adv_c = max(0, -side * mom_z) * gamma * dpds_c * sigma * sqrt(hold_s)
    required = base_edge_c + adv_c
    Returns None (do not quote this side at all) when momentum is against the
    side harder than hard_z sigmas -- no printable edge covers a knife.

    hold_s should be the queue-clear ETA for that side (engine already logs
    y_clear_eta_s / n_clear_eta_s); fall back to 30s if unknown.
    """
    against = -side * mom_z            # >0 means momentum is against this side
    if against > hard_z:
        return None
    adv_c = max(0.0, against) * gamma * dpds_c * sigma * math.sqrt(max(hold_s, 1.0))
    return base_edge_c + adv_c


# ---------------------------------------------------------------------------
# 5. Flow-based skew (book confirms or vetoes the tape)
# ---------------------------------------------------------------------------
def flow_imbalance(y_flow, n_flow):
    """(-1..1): +1 = all recent taker flow hitting YES. Neutral 0 if no flow."""
    tot = (y_flow or 0.0) + (n_flow or 0.0)
    if tot <= 0:
        return 0.0
    return ((y_flow or 0.0) - (n_flow or 0.0)) / tot


def flow_confirms(mom_z, ofi, z_min=1.0, ofi_min=0.3):
    """True when tape momentum and Kalshi taker flow point the same way.

    Use to escalate: gate quoting harder when BOTH agree (informed move),
    relax toward base behavior when they disagree (chop)."""
    return abs(mom_z) >= z_min and abs(ofi) >= ofi_min and (mom_z * ofi) > 0


# ---------------------------------------------------------------------------
# 6. Inventory skew & dynamic clip
# ---------------------------------------------------------------------------
def inventory_skew_c(net, dpds_c, sigma, hold_s=30.0, eta=0.6, min_c=0.0):
    """Cents to back off the accumulating side per unit of net inventory.

    Replaces the constant 0.5c/contract: skew scales with the *fair-price*
    volatility over the expected holding window, so it is automatically
    aggressive near 50c (where fair moves fast) and gentle in extreme zones.
        skew = eta * |net| * dpds_c * sigma * sqrt(hold_s)
    """
    if net == 0:
        return 0.0
    return max(min_c, eta * abs(net) * dpds_c * sigma * math.sqrt(hold_s))


def dynamic_clip(clip_max, edge_c, required_c, mom_z, z0=1.5):
    """Order size: shrink with momentum stress, grow (capped) with excess edge.

        q = clip_max * exp(-|mom_z|/z0) * min(1, edge/required)
    Floor at 1 contract when quoting is allowed at all.
    """
    if required_c is None or required_c <= 0 or edge_c <= 0:
        return 0
    q = clip_max * math.exp(-abs(mom_z) / z0) * min(1.0, edge_c / required_c)
    return max(1, int(q)) if q >= 0.5 else 0


# ---------------------------------------------------------------------------
# 7. Convenience: one call per side per tick
# ---------------------------------------------------------------------------
def side_decision(side, base_edge_c, avail_edge_c, mom_z, ofi, dpds_c, sigma,
                  clear_eta_s, clip_max, jump_ok=True):
    """Returns dict(action, required_c, clip) for one side.

    action: 'quote' | 'hold' (edge insufficient) | 'pull' (knife/jump gate).
    """
    if not jump_ok:
        return {"action": "pull", "reason": "jump_cooldown", "required_c": None, "clip": 0}
    hold_s = clear_eta_s if (clear_eta_s and clear_eta_s > 0) else 30.0
    gamma = 1.5 if flow_confirms(mom_z, ofi) else 1.0
    req = required_edge_c(base_edge_c, side, mom_z, dpds_c, sigma, hold_s, gamma=gamma)
    if req is None:
        return {"action": "pull", "reason": "knife", "required_c": None, "clip": 0}
    if avail_edge_c < req:
        return {"action": "hold", "reason": "edge<required", "required_c": req, "clip": 0}
    clip = dynamic_clip(clip_max, avail_edge_c, req, mom_z)
    return {"action": "quote" if clip > 0 else "hold", "reason": "ok",
            "required_c": req, "clip": clip}
