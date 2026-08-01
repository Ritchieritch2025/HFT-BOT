from __future__ import annotations

import math

import pytest

from tools.research.crypto_mm import log_odds_features as L


def test_tail_probability_move_is_much_larger_in_log_odds() -> None:
    center = L.logit_from_e4(4_900) - L.logit_from_e4(5_000)
    tail = L.logit_from_e4(100) - L.logit_from_e4(200)

    assert center == pytest.approx(-0.0400053346)
    assert tail == pytest.approx(-0.7032995520)
    assert abs(tail) > 17 * abs(center)


def test_no_price_maps_to_negative_yes_log_odds() -> None:
    assert L.yes_equivalent_logit_from_no_e4(7_100) == pytest.approx(
        L.logit_from_e4(2_900)
    )
    assert L.yes_equivalent_logit_from_no_e4(7_100) == pytest.approx(
        -L.logit_from_e4(7_100)
    )


def test_binary_book_uses_yes_bid_and_complementary_no_bid() -> None:
    book = L.binary_book_log_odds(
        yes_bid_e4=2_700,
        no_bid_e4=7_100,
    )

    assert book.yes_ask_e4 == 2_900
    assert book.spread_logodds > 0
    assert L.side_oriented_mid_logodds(book, side="no") == pytest.approx(
        -book.yes_mid_logodds
    )


def test_side_oriented_move_changes_sign() -> None:
    prior = L.binary_book_log_odds(
        yes_bid_e4=4_900,
        no_bid_e4=4_900,
    )
    current = L.binary_book_log_odds(
        yes_bid_e4=5_000,
        no_bid_e4=4_800,
    )

    yes_move = L.side_oriented_move_logodds(
        current,
        prior,
        side="yes",
    )
    no_move = L.side_oriented_move_logodds(
        current,
        prior,
        side="no",
    )
    assert yes_move == pytest.approx(-no_move)
    assert yes_move > 0


def test_quote_fair_skew_is_complement_symmetric() -> None:
    yes_skew = L.quote_fair_skew_logodds(
        quote_side="yes",
        quote_price_e4=3_000,
        fair_yes_probability=0.28,
    )
    no_skew = L.quote_fair_skew_logodds(
        quote_side="no",
        quote_price_e4=7_000,
        fair_yes_probability=0.28,
    )
    assert yes_skew == pytest.approx(-no_skew)


@pytest.mark.parametrize(
    "price",
    (0, 10_000, -1, 10_001, 0.5, True),
)
def test_nonfinite_boundary_or_noninteger_prices_are_rejected(
    price: object,
) -> None:
    with pytest.raises(L.LogOddsFeatureError):
        L.logit_from_e4(price)  # type: ignore[arg-type]


def test_locked_or_crossed_binary_book_is_rejected() -> None:
    with pytest.raises(L.LogOddsFeatureError, match="locked/crossed"):
        L.binary_book_log_odds(
            yes_bid_e4=5_100,
            no_bid_e4=4_900,
        )


@pytest.mark.parametrize("probability", (0.0, 1.0, math.inf, math.nan))
def test_no_arbitrary_epsilon_clipping(probability: float) -> None:
    with pytest.raises(L.LogOddsFeatureError):
        L.logit_probability(probability)
