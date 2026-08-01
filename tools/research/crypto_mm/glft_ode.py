#!/usr/bin/env python3
"""glft_ode.py — finite-horizon GLFT quote offsets (stage-0 #7c, pure math).

The asymptotic closed form in glft_quotes.py assumes T -> inf; a 15-minute
binary with a hard settlement is finite-horizon almost by definition, and
the difference matters exactly where money concentrates: the graveyard and
near the strike.  This module solves the Guéant reduction exactly:

    with  alpha = k * gamma * sigma_c^2 / 2
          eta   = A * (1 + gamma/k) ** -(1 + k/gamma)

    the value transform w_q(t), q in [-Q..Q], solves the LINEAR system

        w_q'(t) = alpha * q^2 * w_q(t) - eta * (w_{q-1}(t) + w_{q+1}(t))

    backward from the terminal condition w_q(T) = exp(-k * penalty_c * |q|)
    (penalty_c = terminal liquidation cost per contract; 0 = inventory
    settles at fair, the Kalshi hold-to-settle case), and the optimal
    offsets are

        delta_bid(q, t) = 1/k * ln(w_q / w_{q+1}) + 1/gamma * ln(1 + gamma/k)
        delta_ask(q, t) = 1/k * ln(w_q / w_{q-1}) + 1/gamma * ln(1 + gamma/k)

Solved with RK4 on a fixed grid; no dependencies beyond math.  NOT wired
into any engine path — stage 1 consumes it read-only first.

Units match glft_quotes.py: cents, seconds, contracts.
"""
import math


def _derivs(w, alpha, eta, Q):
    """dw/ds under BACKWARD time s = T - t (so we integrate forward in s)."""
    out = [0.0] * (2 * Q + 1)
    for i in range(2 * Q + 1):
        q = i - Q
        lo = w[i - 1] if i - 1 >= 0 else 0.0
        hi = w[i + 1] if i + 1 <= 2 * Q else 0.0
        # backward-time sign: w_q(s)' = -alpha q^2 w + eta (w_{q-1}+w_{q+1})
        out[i] = -alpha * q * q * w[i] + eta * (lo + hi)
    return out


def solve_w(sigma_c, gamma, k, A, horizon_s, q_max=10, penalty_c=0.0,
            steps=400):
    """w table at time-to-go = horizon_s.  Returns list indexed [q+q_max]."""
    if min(sigma_c, gamma, k, A) <= 0 or horizon_s < 0:
        raise ValueError("sigma_c, gamma, k, A must be > 0; horizon >= 0")
    Q = int(q_max)
    alpha = k * gamma * sigma_c ** 2 / 2.0
    eta = A * (1.0 + gamma / k) ** -(1.0 + k / gamma)
    w = [math.exp(-k * penalty_c * abs(i - Q)) for i in range(2 * Q + 1)]
    if horizon_s == 0:
        return w
    # RK4 stability: the system's stiffest rate is alpha*Q^2 (inventory
    # boundary) vs eta*2 (coupling).  h must sit well inside 1/rate or the
    # integrator explodes (observed: sigma_c=0.6 gave a negative offset,
    # T=30000 drifted 60% off the closed form).  Steps scale automatically;
    # `steps` is only a floor.
    stiff = max(alpha * Q * Q, 2.0 * eta, 1e-9)
    steps = max(int(steps), int(horizon_s * stiff / 0.35) + 1)
    h = horizon_s / steps
    for _ in range(steps):
        k1 = _derivs(w, alpha, eta, Q)
        w2 = [w[i] + 0.5 * h * k1[i] for i in range(len(w))]
        k2 = _derivs(w2, alpha, eta, Q)
        w3 = [w[i] + 0.5 * h * k2[i] for i in range(len(w))]
        k3 = _derivs(w3, alpha, eta, Q)
        w4 = [w[i] + h * k3[i] for i in range(len(w))]
        k4 = _derivs(w4, alpha, eta, Q)
        w = [max(w[i] + h / 6.0 * (k1[i] + 2 * k2[i] + 2 * k3[i] + k4[i]),
                 1e-300)
             for i in range(len(w))]
        # Renormalize EVERY step: offsets depend only on RATIOS
        # w_q / w_q±1, so scaling by any constant is exact, and keeping
        # max(w)=1 removes overflow from the picture entirely.
        m = max(w)
        w = [x / m for x in w]
    return w


def glft_ode_offsets(q, sigma_c, gamma, k, A, tte_s, q_max=10,
                     penalty_c=0.0, steps=400):
    """(bid_offset_c, ask_offset_c) for inventory q with tte_s remaining.

    Positive offset = quote that many cents away from fair on that side.
    Falls back gracefully at the inventory boundary (|q| == q_max) by
    disallowing the side that would push past the cap (returns None there).
    """
    Q = int(q_max)
    if not (-Q <= q <= Q):
        raise ValueError("q outside the solved inventory grid")
    w = solve_w(sigma_c, gamma, k, A, tte_s, q_max=Q,
                penalty_c=penalty_c, steps=steps)
    base = math.log(1.0 + gamma / k) / gamma
    i = q + Q
    bid = None
    ask = None
    if i + 1 <= 2 * Q:
        bid = math.log(w[i] / w[i + 1]) / k + base
    if i - 1 >= 0:
        ask = math.log(w[i] / w[i - 1]) / k + base
    return bid, ask
