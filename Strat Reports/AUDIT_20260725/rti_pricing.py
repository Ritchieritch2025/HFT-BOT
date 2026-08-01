#!/usr/bin/env python3
"""Deterministic pricing kernel for Kalshi BTC/ETH short-term markets,
anchored EXCLUSIVELY on the CF Benchmarks RTI feed (cfbenchmarks_value).

Settlement rule (official): the market settles on the average of the 60
one-second RTI values in the final minute before close ("greater_or_equal"
vs floor_strike).  Two regimes:

1. PRE-LOCK (more than 60s to close): settlement average is entirely in
   the future.  Model the RTI as a driftless random walk with per-second
   step sigma_s (estimated from the live RTI tick stream, trailing
   window, no lookahead).  The settlement average of a random walk
   started at S over the final minute, seen from tte seconds out, is
   Gaussian with
       mean = S
       var  = sigma_s^2 * [ (tte - 60)                    # walk to window
                            + (m+1)(2m+1)/(6m) ]          # avg inside, m=60
   (second term is Var(avg of RW partial sums)/sigma^2 with m steps).

2. LOCK-IN (inside the final minute, k of 60 ticks observed with
   running sum ``locked_sum``): the remaining m = 60-k ticks are a RW
   from the current RTI value S:
       settlement = (locked_sum + m*S + sum_{i=1..m}(m-i+1) eps_i) / 60
       mean = (locked_sum + m*S) / 60
       var  = sigma_s^2 * m(m+1)(2m+1)/6 / 60^2
   Uncertainty shrinks deterministically to zero tick by tick.

P(settle >= strike) = Phi((mean - strike)/sqrt(var)).

All functions are pure arithmetic — no I/O, no allocation-heavy state —
suitable for the hot-side model thread.  Estimated sigma_s must be fed
from PAST ticks only.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

LOCK_TICKS = 60  # official: 60 one-second values in the final minute


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def rw_avg_var_factor(m: int) -> float:
    """Var( average of the m partial sums of a unit-step RW ) * m^2...

    Exactly: Var( sum_{i=1..m} W_i ) = sum_{k=1..m} k^2 = m(m+1)(2m+1)/6
    for W_i = sum_{j<=i} eps_j with unit-variance steps.  Caller divides
    by the full 60^2 settlement denominator.
    """
    return m * (m + 1) * (2 * m + 1) / 6.0


def p_settle_above(
    rti: float,
    strike: float,
    sigma_s: float,
    tte_s: float,
    locked_sum: float = 0.0,
    locked_n: int = 0,
) -> Optional[float]:
    """P(final-minute average >= strike).

    rti        : current RTI value (the settlement index itself)
    sigma_s    : per-second RTI step std, estimated from past ticks
    tte_s      : seconds until market close
    locked_sum : sum of settlement-window ticks already observed
    locked_n   : count of settlement-window ticks already observed

    Returns None when inputs cannot price (sigma<=0 handled as
    step-function on mean vs strike).
    """
    if tte_s <= 0 and locked_n >= LOCK_TICKS:
        avg = locked_sum / LOCK_TICKS
        return 1.0 if avg >= strike else 0.0
    if locked_n > 0 or tte_s <= LOCK_TICKS:
        # LOCK-IN regime.
        m = LOCK_TICKS - locked_n
        if m <= 0:
            avg = locked_sum / LOCK_TICKS
            return 1.0 if avg >= strike else 0.0
        mean = (locked_sum + m * rti) / LOCK_TICKS
        if sigma_s <= 0:
            return 1.0 if mean >= strike else 0.0
        var = (sigma_s ** 2) * rw_avg_var_factor(m) / (LOCK_TICKS ** 2)
        return _phi((mean - strike) / math.sqrt(var))
    # PRE-LOCK regime.
    mean = rti
    if sigma_s <= 0:
        return 1.0 if mean >= strike else 0.0
    walk_var = max(tte_s - LOCK_TICKS, 0.0)
    inside = rw_avg_var_factor(LOCK_TICKS) / (LOCK_TICKS ** 2)
    var = (sigma_s ** 2) * (walk_var + inside)
    return _phi((mean - strike) / math.sqrt(var))


def sigma_from_ticks(ticks: Sequence[float], min_n: int = 30) -> Optional[float]:
    """Per-second step std from a trailing window of RTI ticks (~1Hz).

    Uses simple first differences; caller guarantees ticks are the most
    recent PAST values in time order (no lookahead).
    """
    if len(ticks) < min_n + 1:
        return None
    diffs = [ticks[i] - ticks[i - 1] for i in range(1, len(ticks))]
    mu = sum(diffs) / len(diffs)
    var = sum((d - mu) ** 2 for d in diffs) / (len(diffs) - 1)
    return math.sqrt(var) if var > 0 else None


def maker_edges(
    p_up: float,
    yes_bid_c: float,
    yes_ask_c: float,
) -> tuple:
    """(edge_yes, edge_no) in cents per contract, fees = 0 (official).

    edge_yes: buy YES resting at yes_bid_c -> value 100*p, cost yes_bid_c.
    edge_no : buy NO resting at the NO book's best bid, whose price is
              (100 - yes_ask_c); its value is 100*(1-p).  The earlier
              version cancelled the two 100-terms and produced a
              symmetric, p-independent number — the NO-side pricing bug
              found by the guard tests 2026-07-25.
    """
    fair = 100.0 * p_up
    edge_yes = fair - yes_bid_c
    no_cost = 100.0 - yes_ask_c
    edge_no = (100.0 - fair) - no_cost
    return edge_yes, edge_no
