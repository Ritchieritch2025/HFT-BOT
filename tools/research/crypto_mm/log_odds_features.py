#!/usr/bin/env python3
"""Causal log-odds features for binary order books.

This module transforms probability prices for statistical models only.
Executable PnL, fees, pair-cost ceilings, and capital remain in dollar-price
space.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


PRICE_SCALE_E4 = 10_000


class LogOddsFeatureError(ValueError):
    """A binary price or book cannot define a finite causal feature."""


def probability_from_e4(price_e4: int) -> float:
    if type(price_e4) is not int or not 0 < price_e4 < PRICE_SCALE_E4:
        raise LogOddsFeatureError(
            f"price_e4 must be an integer strictly inside (0,10000): "
            f"{price_e4!r}"
        )
    return price_e4 / PRICE_SCALE_E4


def logit_probability(probability: float) -> float:
    if (
        isinstance(probability, bool)
        or not isinstance(probability, (int, float))
        or not math.isfinite(float(probability))
        or not 0.0 < float(probability) < 1.0
    ):
        raise LogOddsFeatureError(
            f"probability must be finite and strictly inside (0,1): "
            f"{probability!r}"
        )
    p = float(probability)
    return math.log(p) - math.log1p(-p)


def logit_from_e4(price_e4: int) -> float:
    return logit_probability(probability_from_e4(price_e4))


def yes_equivalent_logit_from_no_e4(no_price_e4: int) -> float:
    """Map a NO probability price into the equivalent YES log-odds."""
    return -logit_from_e4(no_price_e4)


@dataclass(frozen=True)
class BinaryBookLogOdds:
    yes_bid_e4: int
    yes_ask_e4: int
    yes_bid_logodds: float
    yes_ask_logodds: float
    yes_mid_logodds: float
    spread_logodds: float


def binary_book_log_odds(
    *,
    yes_bid_e4: int,
    no_bid_e4: int,
) -> BinaryBookLogOdds:
    """Construct the executable YES interval from complementary bids."""
    probability_from_e4(yes_bid_e4)
    probability_from_e4(no_bid_e4)
    yes_ask_e4 = PRICE_SCALE_E4 - no_bid_e4
    if yes_bid_e4 >= yes_ask_e4:
        raise LogOddsFeatureError(
            "binary book is locked/crossed: "
            f"yes_bid_e4={yes_bid_e4} no_bid_e4={no_bid_e4}"
        )
    bid = logit_from_e4(yes_bid_e4)
    ask = logit_from_e4(yes_ask_e4)
    spread = ask - bid
    if not spread > 0.0:
        raise LogOddsFeatureError("log-odds spread must be positive")
    return BinaryBookLogOdds(
        yes_bid_e4=yes_bid_e4,
        yes_ask_e4=yes_ask_e4,
        yes_bid_logodds=bid,
        yes_ask_logodds=ask,
        yes_mid_logodds=(bid + ask) / 2.0,
        spread_logodds=spread,
    )


def side_oriented_mid_logodds(
    book: BinaryBookLogOdds,
    *,
    side: str,
) -> float:
    if side == "yes":
        return book.yes_mid_logodds
    if side == "no":
        return -book.yes_mid_logodds
    raise LogOddsFeatureError(f"unknown binary side: {side!r}")


def side_oriented_move_logodds(
    current: BinaryBookLogOdds,
    prior: BinaryBookLogOdds,
    *,
    side: str,
) -> float:
    return side_oriented_mid_logodds(
        current,
        side=side,
    ) - side_oriented_mid_logodds(
        prior,
        side=side,
    )


def quote_fair_skew_logodds(
    *,
    quote_side: str,
    quote_price_e4: int,
    fair_yes_probability: float,
) -> float:
    """Return side-oriented quote log-odds minus fair log-odds."""
    quote = logit_from_e4(quote_price_e4)
    fair_yes = float(fair_yes_probability)
    if quote_side == "yes":
        fair_side = fair_yes
    elif quote_side == "no":
        fair_side = 1.0 - fair_yes
    else:
        raise LogOddsFeatureError(
            f"unknown binary side: {quote_side!r}"
        )
    return quote - logit_probability(fair_side)

