import importlib.util
import math
from pathlib import Path


def load(name):
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("deep_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


stats = load("stats")
features = load("features")


def test_bh_known_example_and_original_order():
    got = stats.benjamini_hochberg([0.01, 0.04, 0.03, 0.002])
    assert got == [0.02, 0.04, 0.04, 0.008]


def test_block_bootstrap_is_deterministic_and_event_weighted():
    values = {"d1": {"e1": 1.0, "e2": -1.0}, "d2": {"e3": 3.0}}
    a = stats.block_bootstrap_mean(values, replicates=100, seed=7)
    b = stats.block_bootstrap_mean(values, replicates=100, seed=7)
    assert a == b
    assert a["mean"] == 1.0
    assert a["n_events"] == 3 and a["n_days"] == 2


def test_logit_spread_and_midpoint_are_symmetric():
    row = features.book_features(4000, 6000, 10000, 10000)
    assert row["valid_two_sided"] is True
    assert row["spread_log_odds"] > 0
    assert math.isclose(row["midpoint_probability"], 0.5, abs_tol=1e-12)
    assert row["imbalance"] == 0.0


def test_sub_cent_e4_prices_are_not_clipped_to_one_cent():
    assert math.isclose(features.e4_to_probability(1), 0.0001)
    assert math.isclose(features.e4_to_probability(9999), 0.9999)
    assert features.logit_e4(1) < features.logit_e4(99)


def test_invalid_and_one_sided_books_fail_closed():
    assert not features.book_features(None, 5000, 1, 1)["valid_two_sided"]
    assert not features.book_features(5000, 5000, 1, 1)["valid_two_sided"]
    assert not features.book_features(6000, 5000, 1, 1)["valid_two_sided"]


def test_strict_through_never_fills_at_touch():
    assert not features.strict_through_fill("yes", 6000, 4000, 6000)
    assert features.strict_through_fill("yes", 6100, 4000, 6000)
    assert not features.strict_through_fill("no", 4000, 4000, 6000)
    assert features.strict_through_fill("no", 3900, 4000, 6000)


def test_maker_markout_signs():
    assert features.maker_gross_markout_e4("yes", 4000, 6000, 0.55) == 500
    assert features.maker_gross_markout_e4("no", 4000, 6000, 0.45) == 500
    assert features.signed_future_logodds_move("yes", 0.0, 0.2) == 0.2
    assert features.signed_future_logodds_move("no", 0.0, 0.2) == -0.2
