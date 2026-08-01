"""Pure-synthetic R3 tests for legal empty public-touch states."""
from collections import deque
from decimal import Decimal

from tools.research.crypto_mm.round4_stage1_entry_extractor import (
    append_causal_mid_receipt,
    causal_mid_x2_asof,
    public_touch_snapshot,
    touch_dependent_risk_features,
)


def test_passive_mid_snapshot_reports_one_sided_or_empty_public_book_missing():
    assert public_touch_snapshot({"y": {3000: 10_000}, "n": {}}) is None
    assert public_touch_snapshot({"y": {}, "n": {6900: 10_000}}) is None
    assert public_touch_snapshot({"y": {}, "n": {}}) is None


def test_active_grid_missing_touch_keeps_explicit_all_null_bundle():
    bundle = touch_dependent_risk_features(
        None,
        mid_1s_x2=6_100,
        mid_10s_x2=6_000,
    )
    assert bundle == {
        "touch_imbalance": None,
        "spread_e4": None,
        "mid_move_1s_e4": None,
        "mid_move_10s_e4": None,
    }


def test_two_sided_touch_features_remain_causal_and_unimputed():
    snapshot = public_touch_snapshot(
        {
            "y": {3000: 20_000},
            "n": {6900: 10_000},
        }
    )
    assert snapshot == {
        "yes_bid_e4": 3000,
        "no_bid_e4": 6900,
        "yes_ask_e4": 3100,
        "spread_e4": 100,
        "mid_x2_e4": 6100,
        "touch_imbalance": Decimal("0.3333333333333333333333333333"),
    }
    features = touch_dependent_risk_features(
        snapshot,
        mid_1s_x2=6_080,
        mid_10s_x2=None,
    )
    assert features["spread_e4"] == 100
    assert features["touch_imbalance"] == snapshot["touch_imbalance"]
    assert features["mid_move_1s_e4"] == 10
    assert features["mid_move_10s_e4"] is None


def test_valid_missing_valid_history_keeps_gap_sentinel():
    history = deque()
    valid_before = {"mid_x2_e4": 6_000}
    valid_after = {"mid_x2_e4": 6_200}

    assert append_causal_mid_receipt(
        history,
        {"recv_wall_ns": 0, "book_stable_id": "valid-before"},
        valid_before,
    )
    assert append_causal_mid_receipt(
        history,
        {
            "recv_wall_ns": 5_000_000_000,
            "book_stable_id": "missing",
        },
        None,
    )
    assert not append_causal_mid_receipt(
        history,
        {
            "recv_wall_ns": 5_000_000_000,
            "book_stable_id": "missing",
        },
        None,
    )
    assert append_causal_mid_receipt(
        history,
        {
            "recv_wall_ns": 15_000_000_000,
            "book_stable_id": "valid-after",
        },
        valid_after,
    )

    assert list(history) == [
        (5_000_000_000, None, "missing"),
        (15_000_000_000, 6_200, "valid-after"),
    ]
    assert causal_mid_x2_asof(history, 14_000_000_000) is None
    assert causal_mid_x2_asof(history, 15_000_000_000) == 6_200


def test_recovery_lags_use_target_receipt_state_without_crossing_gap():
    history = deque(
        [
            (0, 6_000, "valid-before"),
            (5_000_000_000, None, "missing"),
            (15_000_000_000, 6_200, "valid-after"),
        ]
    )
    recovered = {
        "mid_x2_e4": 6_200,
        "spread_e4": 100,
        "touch_imbalance": Decimal("0"),
    }

    at_recovery = touch_dependent_risk_features(
        recovered,
        mid_1s_x2=causal_mid_x2_asof(history, 14_000_000_000),
        mid_10s_x2=causal_mid_x2_asof(history, 5_000_000_000),
    )
    assert at_recovery["mid_move_1s_e4"] is None
    assert at_recovery["mid_move_10s_e4"] is None

    after_recovery = touch_dependent_risk_features(
        recovered,
        mid_1s_x2=causal_mid_x2_asof(history, 24_000_000_000),
        mid_10s_x2=causal_mid_x2_asof(history, 15_000_000_000),
    )
    assert after_recovery["mid_move_1s_e4"] == 0
    assert after_recovery["mid_move_10s_e4"] == 0
