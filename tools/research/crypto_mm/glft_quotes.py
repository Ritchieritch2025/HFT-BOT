#!/usr/bin/env python3
"""glft_quotes.py — Guéant–Lehalle–Fernandez-Tapia (arXiv:1105.3115) optimal
market-making quotes, mapped to Kalshi binary probability space.

Model recap (paper §2, asymptotic result §4):
  * reference price follows Brownian motion with vol sigma (here: fair_c, ¢)
  * fill intensity at distance delta from reference: lambda(delta)=A·exp(-k·delta)
  * CARA utility with risk aversion gamma; inventory q in contracts
  * asymptotic optimal offsets (T -> inf), paper eq. before (Fig.4):

      d0    = (1/gamma)·ln(1 + gamma/k)
      Delta = sqrt( (sigma^2·gamma)/(2·k·A) · (1 + gamma/k)^(1 + k/gamma) )
      delta_bid(q) = d0 + (2q+1)/2 · Delta     (distance below fair)
      delta_ask(q) = d0 - (2q-1)/2 · Delta     (distance above fair)
      spread(q)    = 2·d0 + Delta              (independent of q)

  Each unit of inventory shifts BOTH quotes by Delta toward unwinding — this
  is the principled replacement for the constant 0.5c/contract skew.

Units here: prices/offsets in cents; sigma_c in cents/sqrt(s) (fair-price vol:
sigma_c = dP/dS · sigma_S, see trend_signals.fair_price_sensitivity_c);
A in fills/second at delta=0; k in 1/cents; gamma in 1/cents.

Scope: GLFT assumes symmetric UNINFORMED flow — it prices inventory risk
only. Adverse selection is handled separately (trend_signals.required_edge_c).
Compose: quote at fair - max(glft_bid_offset, trend_required_edge).
"""
import math


def glft_offsets(q, sigma_c, gamma, k, A):
    """Asymptotic optimal (bid_offset_c, ask_offset_c, half_params) for
    inventory q (contracts, +long YES). Offsets can be negative for the
    unwinding side when |q| is large — floor at your min edge upstream."""
    if min(sigma_c, gamma, k, A) <= 0:
        raise ValueError("all parameters must be > 0")
    d0 = math.log(1.0 + gamma / k) / gamma
    delta = math.sqrt((sigma_c ** 2 * gamma) / (2.0 * k * A)
                      * (1.0 + gamma / k) ** (1.0 + k / gamma))
    bid = d0 + (2 * q + 1) / 2.0 * delta
    ask = d0 - (2 * q - 1) / 2.0 * delta
    return bid, ask, {"d0": d0, "per_contract_skew": delta, "spread": 2 * d0 + delta}


def fit_intensity(samples, min_bucket_s=30.0):
    """Fit lambda(delta) = A·exp(-k·delta) from quoting history.

    samples: iterable of (delta_c, exposure_s, fills) — for each observation
    window: our quote's distance from fair (¢), seconds the quote rested at
    that distance, number of fills received.
    Build from shadow logs: QUOTE_EVAL gives distance, SHADOW_FILL_SIM/FILL
    gives fills; bucket delta to 0.5¢.

    Returns (A, k, r2) from weighted least squares on ln(lambda) vs delta.
    """
    buckets = {}
    for delta_c, exp_s, fills in samples:
        b = round(delta_c * 2) / 2.0
        t, f = buckets.get(b, (0.0, 0.0))
        buckets[b] = (t + exp_s, f + fills)
    pts = [(d, f / t, t) for d, (t, f) in sorted(buckets.items())
           if t >= min_bucket_s and f > 0]
    if len(pts) < 3:
        raise ValueError("not enough populated buckets to fit intensity")
    # weighted linear regression: ln(lam) = ln A - k*delta, weights = exposure
    sw = sum(w for _, _, w in pts)
    mx = sum(d * w for d, _, w in pts) / sw
    my = sum(math.log(l) * w for _, l, w in pts) / sw
    sxx = sum(w * (d - mx) ** 2 for d, _, w in pts)
    sxy = sum(w * (d - mx) * (math.log(l) - my) for d, l, w in pts)
    slope = sxy / sxx
    k = -slope
    A = math.exp(my - slope * mx)
    ss_tot = sum(w * (math.log(l) - my) ** 2 for _, l, w in pts)
    ss_res = sum(w * (math.log(l) - (my + slope * (d - mx))) ** 2 for d, l, w in pts)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    if k <= 0:
        raise ValueError(f"fitted k={k:.4f} <= 0: intensity not decreasing in "
                         "delta — check delta sign convention or data quality")
    return A, k, r2


def gamma_from_target_skew(target_skew_c, sigma_c, k, A,
                           lo=1e-6, hi=10.0, tol=1e-8):
    """Invert Delta(gamma) = target per-contract skew (¢) by bisection.

    Practical calibration: 'at max inventory Q the quote should have backed
    off by X cents' => target_skew_c = X/Q. Delta is increasing in gamma."""
    def delta(g):
        return math.sqrt((sigma_c ** 2 * g) / (2.0 * k * A)
                         * (1.0 + g / k) ** (1.0 + k / g))
    if delta(hi) < target_skew_c:
        raise ValueError("target skew unreachable: raise hi or check sigma/k/A")
    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        if delta(mid) < target_skew_c:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


if __name__ == "__main__":
    # smoke test with plausible canary numbers:
    # fair-price vol 0.4 c/sqrt(s), k=0.7 /c, A=0.02 fills/s at fair, gamma via
    # target: at q=3 backed off 1.5c => 0.5 c/contract
    g = gamma_from_target_skew(0.5, 0.4, 0.7, 0.02)
    for q in (-2, -1, 0, 1, 2):
        b, a, info = glft_offsets(q, 0.4, g, 0.7, 0.02)
        print(f"q={q:+d} bid_off={b:5.2f}c ask_off={a:5.2f}c "
              f"spread={info['spread']:.2f}c skew/ct={info['per_contract_skew']:.2f}c")
