"""Pure causal feature arithmetic for probability-contract research."""

from __future__ import annotations

import math


# E4 prices can legitimately be sub-cent.  One E4 tick is the smallest
# positive probability represented by the sealed facts; clipping more
# aggressively would erase precisely the extreme-price behavior under study.
P_MIN = 0.0001
P_MAX = 0.9999


def clip_probability(p: float) -> float:
    return min(P_MAX, max(P_MIN, float(p)))


def logit(p: float) -> float:
    p = clip_probability(p)
    return math.log(p / (1.0 - p))


def expit(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        p = 1.0 / (1.0 + z)
    else:
        z = math.exp(value)
        p = z / (1.0 + z)
    return clip_probability(p)


def e4_to_probability(value: int | float) -> float:
    return clip_probability(float(value) / 10_000.0)


def logit_e4(value: int | float) -> float:
    return logit(e4_to_probability(value))


def book_features(
    yes_bid_e4: int | None,
    yes_ask_e4: int | None,
    yes_bid_qty_e4: int | None,
    yes_ask_qty_e4: int | None,
) -> dict:
    """Return binary-market L1 features or an explicit invalid state."""
    if yes_bid_e4 is None or yes_ask_e4 is None:
        return {"valid_two_sided": False, "invalid_reason": "missing_side"}
    bid = int(yes_bid_e4)
    ask = int(yes_ask_e4)
    if not (0 < bid < ask < 10_000):
        return {"valid_two_sided": False, "invalid_reason": "invalid_or_locked_book"}
    bid_lo = logit_e4(bid)
    ask_lo = logit_e4(ask)
    midpoint_lo = (bid_lo + ask_lo) / 2.0
    bid_qty = max(0, int(yes_bid_qty_e4 or 0))
    ask_qty = max(0, int(yes_ask_qty_e4 or 0))
    denom = bid_qty + ask_qty
    return {
        "valid_two_sided": True,
        "yes_bid_e4": bid,
        "yes_ask_e4": ask,
        "spread_e4": ask - bid,
        "spread_log_odds": ask_lo - bid_lo,
        "midpoint_log_odds": midpoint_lo,
        "midpoint_probability": expit(midpoint_lo),
        "imbalance": (bid_qty - ask_qty) / denom if denom else None,
    }


def strict_through_fill(taker_side: str, trade_yes_price_e4: int, bid_e4: int, ask_e4: int) -> bool:
    """Binding one-contract eligibility: at-price never fills."""
    if taker_side == "yes":
        return int(trade_yes_price_e4) > int(ask_e4)
    if taker_side == "no":
        return int(trade_yes_price_e4) < int(bid_e4)
    return False


def maker_gross_markout_e4(
    taker_side: str,
    bid_e4: int,
    ask_e4: int,
    future_mid_probability: float,
) -> float:
    """One-contract gross maker value at a future mark, before fees/exits."""
    future_e4 = float(future_mid_probability) * 10_000.0
    if taker_side == "yes":
        return float(ask_e4) - future_e4  # maker sold YES
    if taker_side == "no":
        return future_e4 - float(bid_e4)  # maker bought YES
    raise ValueError("taker_side must be yes or no")


def signed_future_logodds_move(
    taker_side: str,
    midpoint_log_odds: float,
    future_midpoint_log_odds: float,
) -> float:
    sign = 1.0 if taker_side == "yes" else -1.0 if taker_side == "no" else 0.0
    if not sign:
        raise ValueError("taker_side must be yes or no")
    return sign * (float(future_midpoint_log_odds) - float(midpoint_log_odds))
