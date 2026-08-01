from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib
from pathlib import Path

import pytest

from tools.research.crypto_mm import round4_dynamic_keep_fok_policy_v2_1 as P


D = Decimal
BASE_WALL_NS = 1_800_000_000_000_000_000
BASE_MONO_NS = 8_000_000_000_000_000


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def pin(
    kind: str,
    label: str,
    *,
    semantic_payload: dict[str, object] | None = None,
) -> P.SealedPin:
    if semantic_payload is None:
        semantic_payload = {"opaque_external_id": label}
    return P.SealedPin(
        authority="EXTERNAL_SEALED",
        artifact_kind=kind,
        sha256=digest(label),
        semantic_payload_sha256=P.semantic_payload_sha256(
            kind, semantic_payload
        ),
        uri=f"s3://round4-sealed/{label}",
        version_id=f"version-{label}",
        size_bytes=100 + len(label),
    )


def market() -> P.MarketContext:
    series = "KXBTC15M"
    ticker = "KXBTC15M-26JUL260000-15"
    structure = "tapered_deci_cent"
    ranges = P.EXPECTED_PRICE_RANGES
    minimum = D("0.01")
    return P.MarketContext(
        series_ticker=series,
        market_ticker=ticker,
        price_level_structure=structure,
        price_ranges=ranges,
        min_quantity_increment=minimum,
        context_pin=pin(
            "MARKET_CONTEXT",
            "market-context",
            semantic_payload=P.market_context_payload(
                series_ticker=series,
                market_ticker=ticker,
                price_level_structure=structure,
                price_ranges=ranges,
                min_quantity_increment=minimum,
            ),
        ),
    )


def fee_context() -> P.FeeContext:
    series = "KXBTC15M"
    rate = D("0.07")
    quantum = D("0.0001")
    rule = P.CONSERVATIVE_PUBLIC_FEE_RULE
    actual = False
    return P.FeeContext(
        series_ticker=series,
        taker_rate=rate,
        centicent_quantum_usd=quantum,
        rounding_rule=rule,
        authenticated_actual_fee_available=actual,
        fee_schedule_pin=pin(
            "FEE_SCHEDULE",
            "fee-schedule",
            semantic_payload=P.fee_context_payload(
                series_ticker=series,
                taker_rate=rate,
                centicent_quantum_usd=quantum,
                rounding_rule=rule,
                authenticated_actual_fee_available=actual,
            ),
        ),
    )


def log_features(
    *,
    move_1s: Decimal | None = D("0"),
) -> P.LogOddsFeatures:
    return P.LogOddsFeatures(
        mid_logodds=D("-0.8"),
        spread_logodds=D("0.12"),
        move_logodds_1s=move_1s,
        move_logodds_10s=D("0.03"),
        quote_skew_logodds=D("-0.02"),
        queue_ahead_qty=D("2"),
        better_depth_qty=D("1"),
        complement_flow_5s_qty=D("0.4"),
        transform_pin=P.SealedPin(
            authority="EXTERNAL_SEALED",
            artifact_kind="LOG_ODDS_FEATURE_CODE",
            sha256=P.LOG_ODDS_FEATURE_MODULE_SHA256,
            semantic_payload_sha256=P.semantic_payload_sha256(
                "LOG_ODDS_FEATURE_CODE",
                {
                    "module_sha256": P.LOG_ODDS_FEATURE_MODULE_SHA256,
                    "feature_names": (
                        "mid_logodds",
                        "spread_logodds",
                        "move_logodds_1s",
                        "move_logodds_10s",
                        "quote_skew_logodds",
                    ),
                },
            ),
            uri="s3://round4-sealed/log-odds-code",
            version_id="log-odds-v1",
            size_bytes=1,
        ),
    )


def level(
    *,
    price: str,
    quantity: str,
    ticker: str,
    outcome: str,
    action: str,
    wall_ns: int,
    mono_ns: int,
) -> P.BookLevel:
    p = D(price)
    tick = D("0.001") if p < D(".1") or p >= D(".9") else D(".01")
    return P.BookLevel(
        market_ticker=ticker,
        outcome_side=outcome,
        action=action,
        price=p,
        aggregate_quantity=D(quantity),
        tick_size=tick,
        recv_wall_ns=wall_ns,
        recv_mono_ns=mono_ns,
    )


def state_kwargs(
    *,
    elapsed_ms: str = "1000",
    first_side: str = "YES",
    first_price: str = "0.30",
    complement_price: str = "0.69",
    quantity: str = "2",
    sell_price: str = "0.31",
    sell_quantity: str = "2",
    buy_price: str = "0.72",
    buy_quantity: str = "2",
    move_1s: Decimal | None = D("0"),
) -> dict[str, object]:
    m = market()
    elapsed = D(elapsed_ms)
    elapsed_ns = int(elapsed * D("1000000"))
    wall = BASE_WALL_NS + elapsed_ns
    mono = BASE_MONO_NS + elapsed_ns
    complement_side = "NO" if first_side == "YES" else "YES"
    first = P.FirstFill(
        market_ticker=m.market_ticker,
        outcome_side=first_side,
        action="BUY",
        price=D(first_price),
        quantity=D(quantity),
        maker_fee_usd=D("0"),
        recv_wall_ns=BASE_WALL_NS,
        recv_mono_ns=BASE_MONO_NS,
        source_pin=pin("FIRST_FILL_RECEIPT", "first-fill"),
    )
    resting = P.RestingComplement(
        market_ticker=m.market_ticker,
        outcome_side=complement_side,
        action="BUY",
        price=D(complement_price),
        quantity=D(quantity),
        maker_fee_usd=D("0"),
        source_pin=pin("RESTING_ORDER_RECEIPT", "resting-order"),
    )
    return {
        "event_id": f"decision-{first_side}-{elapsed_ms}",
        "market": m,
        "fee_context": fee_context(),
        "first_fill": first,
        "resting_complement": resting,
        "sell_first_leg_levels": (
            level(
                price=sell_price,
                quantity=sell_quantity,
                ticker=m.market_ticker,
                outcome=first_side,
                action="SELL",
                wall_ns=wall,
                mono_ns=mono,
            ),
        ),
        "buy_complement_levels": (
            level(
                price=buy_price,
                quantity=buy_quantity,
                ticker=m.market_ticker,
                outcome=complement_side,
                action="BUY",
                wall_ns=wall,
                mono_ns=mono,
            ),
        ),
        "features": log_features(move_1s=move_1s),
        "decision_elapsed_ms": elapsed,
        "decision_recv_wall_ns": wall,
        "decision_recv_mono_ns": mono,
        "feature_asof_wall_ns": wall,
        "feature_asof_mono_ns": mono,
        "source_max_recv_wall_ns": wall,
        "source_max_recv_mono_ns": mono,
        "fok_terminal_elapsed_ms": elapsed + D("60"),
        "fok_terminal_wall_ns": wall + 60_000_000,
        "fok_terminal_mono_ns": mono + 60_000_000,
    }


def state(**overrides: object) -> P.DecisionState:
    kwargs = state_kwargs()
    kwargs.update(overrides)
    return P.DecisionState(**kwargs)


def model(
    *,
    feature: str = "move_logodds_1s",
    coefficient: str = "1",
    lower: str = "-2",
    upper: str = "2",
) -> P.TrainOnlyLCBModel:
    return P.TrainOnlyLCBModel(
        model_id="lodo-train-v2.1",
        training_role="TRAIN_ONLY",
        delta_estimand=P.DELTA_ESTIMAND,
        lcb_definition=P.LCB_DEFINITION,
        intercept_mean_delta_usd=D("0"),
        uncertainty_radius_usd=D("0"),
        features=(
            P.ModelFeature(
                name=feature,
                coefficient=D(coefficient),
                support_lower=D(lower),
                support_upper=D(upper),
            ),
        ),
        training_data_pin=pin("TRAINING_DATASET", "train-data"),
        model_artifact_pin=pin("TRAIN_ONLY_LCB_MODEL", "model"),
        log_odds_transform_pin=log_features().transform_pin,
    )


def release(
    st: P.DecisionState,
    *,
    component: str,
    action: str,
    elapsed_ms: str,
    route_type: str | None,
) -> P.CapitalReleaseReceipt:
    elapsed = D(elapsed_ms)
    elapsed_ns = int(elapsed * D("1000000"))
    return P.CapitalReleaseReceipt(
        event_id=st.event_id,
        market_ticker=st.market.market_ticker,
        component=component,
        decision_action=action,
        exit_route_type=route_type,
        release_elapsed_ms=elapsed,
        release_wall_ns=st.first_fill.recv_wall_ns + elapsed_ns,
        release_mono_ns=st.first_fill.recv_mono_ns + elapsed_ns,
        release_event_id=f"{st.event_id}-{component}-{elapsed_ms}",
        source_pin=pin(
            "CAPITAL_RELEASE_RECEIPT",
            f"release-{component}-{action}-{elapsed_ms}",
        ),
    )


def test_v2_is_frozen_and_v2_1_is_offline_only() -> None:
    root = Path(__file__).resolve().parents[1]
    assert hashlib.sha256(
        (root / "tools/research/crypto_mm/"
         "round4_dynamic_keep_fok_policy_v2.py").read_bytes()
    ).hexdigest() == (
        "c7b6d6eb21d4838a105c271a901fa0ae"
        "9729c96b58607c7000ff50b66b47ac7f"
    )
    assert hashlib.sha256(
        (root / "tests/test_round4_dynamic_keep_fok_policy_v2.py").read_bytes()
    ).hexdigest() == (
        "932e187047dcdf43d7aa5d9c5d31a993"
        "554e91ac3d90fbf3d7b0b0c8c3caba42"
    )
    assert hashlib.sha256(
        (root / "tools/research/crypto_mm/log_odds_features.py").read_bytes()
    ).hexdigest() == P.LOG_ODDS_FEATURE_MODULE_SHA256
    assert P.CANDIDATE_ALLOWED is False
    assert P.SHADOW_ALLOWED is False
    assert P.LIVE_ALLOWED is False
    assert P.ACTION_SEAL_ALLOWED is False
    assert P.AUTHENTICATED_ACTUAL_FEE_ALLOWED is False


def test_public_aggregate_fee_uses_worst_partition_dp() -> None:
    bound = P.aggregate_taker_fee_safe_upper(
        price=D("0.69"),
        aggregate_quantity=D("2"),
        market=market(),
        fee_context=fee_context(),
    )
    assert bound.taker_fee_safe_upper_usd == D("0.0400")
    assert bound.trade_fee_centicent_ceil_sum_usd == D("0.0400")
    assert bound.position_cost_fee_residual_sum_usd == D("0")
    assert len(bound.worst_partition_quantities) == 200
    assert set(bound.worst_partition_quantities) == {D("0.01")}


def test_subcent_position_cost_residual_is_included() -> None:
    bound = P.aggregate_taker_fee_safe_upper(
        price=D("0.099"),
        aggregate_quantity=D("0.01"),
        market=market(),
        fee_context=fee_context(),
    )
    assert bound.trade_fee_centicent_ceil_sum_usd == D("0.0001")
    assert bound.position_cost_fee_residual_sum_usd == D("0.00001")
    assert bound.taker_fee_safe_upper_usd == D("0.00011")


@pytest.mark.parametrize("first_side", ("YES", "NO"))
def test_sell_first_leg_wins_for_both_outcomes(first_side: str) -> None:
    first_price = "0.30" if first_side == "YES" else "0.60"
    complement = "0.69" if first_side == "YES" else "0.39"
    sell_price = "0.31" if first_side == "YES" else "0.61"
    buy_price = "0.72" if first_side == "YES" else "0.42"
    st = P.DecisionState(
        **state_kwargs(
            first_side=first_side,
            first_price=first_price,
            complement_price=complement,
            sell_price=sell_price,
            buy_price=buy_price,
        )
    )
    routes = P.evaluate_exit_routes(st)
    assert routes.sell_first_leg.full_fill is True
    assert routes.buy_complement.full_fill is True
    assert routes.selected is not None
    assert routes.selected.route_type == P.SELL_FIRST_LEG
    assert routes.selected.net_pnl_safe_lower_usd > (
        routes.buy_complement.net_pnl_safe_lower_usd
    )


@pytest.mark.parametrize("first_side", ("YES", "NO"))
def test_buy_complement_wins_for_both_outcomes(first_side: str) -> None:
    first_price = "0.30" if first_side == "YES" else "0.60"
    complement = "0.69" if first_side == "YES" else "0.39"
    sell_price = "0.25" if first_side == "YES" else "0.55"
    buy_price = "0.69" if first_side == "YES" else "0.39"
    routes = P.evaluate_exit_routes(
        P.DecisionState(
            **state_kwargs(
                first_side=first_side,
                first_price=first_price,
                complement_price=complement,
                sell_price=sell_price,
                buy_price=buy_price,
            )
        )
    )
    assert routes.selected is not None
    assert routes.selected.route_type == P.BUY_COMPLEMENT


def test_single_full_route_is_selected_and_incomplete_route_is_explicit() -> None:
    routes = P.evaluate_exit_routes(
        P.DecisionState(
            **state_kwargs(sell_quantity="1.99", buy_quantity="2")
        )
    )
    assert routes.sell_first_leg.full_fill is False
    assert routes.sell_first_leg.executable_quantity == D("1.99")
    assert routes.sell_first_leg.residual_quantity == D("0.01")
    assert routes.buy_complement.full_fill is True
    assert routes.selected.route_type == P.BUY_COMPLEMENT
    assert routes.buy_complement.actual_fill_claimed is False
    assert all(
        execution.source_nature == "PUBLIC_AGGREGATE_LEVEL"
        and execution.private_partition_unknown is True
        for execution in routes.buy_complement.executions
    )


def test_both_routes_incomplete_is_no_decision_never_submit_fok() -> None:
    st = P.DecisionState(
        **state_kwargs(sell_quantity="1", buy_quantity="1.99")
    )
    decision = P.choose_action(st, model(), buffer_usd=D("999"))
    assert decision.action == P.NO_DECISION
    assert decision.selected_exit is None
    assert decision.reason == "NO_FULL_FILL_EXIT_ROUTE"


def test_missing_model_nonfinite_feature_and_out_of_support_are_no_decision() -> None:
    st = state()
    assert P.choose_action(st, None, buffer_usd=D("0")).action == (
        P.NO_DECISION
    )
    nonfinite = state(features=log_features(move_1s=D("NaN")))
    assert P.choose_action(
        nonfinite, model(), buffer_usd=D("0")
    ).action == P.NO_DECISION
    outside = state(features=log_features(move_1s=D("3")))
    assert P.choose_action(
        outside, model(), buffer_usd=D("0")
    ).action == P.NO_DECISION


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    (
        ("market_ticker", "KXBTC15M-WRONG", "ticker"),
        ("outcome_side", "NO", "outcome"),
        ("action", "BUY", "action"),
    ),
)
def test_sell_book_binding_rejects_wrong_market_direction_or_action(
    field: str,
    replacement: str,
    message: str,
) -> None:
    kwargs = state_kwargs()
    original = kwargs["sell_first_leg_levels"][0]
    kwargs["sell_first_leg_levels"] = (
        replace(original, **{field: replacement}),
    )
    with pytest.raises(P.PolicyContractError, match=message):
        P.DecisionState(**kwargs)


def test_price_ranges_ticks_and_min_quantity_are_strict() -> None:
    bad_ranges = (
        P.PriceRange(D("0"), D("0.1"), D("0.01")),
        *P.EXPECTED_PRICE_RANGES[1:],
    )
    with pytest.raises(P.PolicyContractError, match="price_ranges"):
        replace(market(), price_ranges=bad_ranges)
    with pytest.raises(P.PolicyContractError, match="minimum quantity"):
        replace(market(), min_quantity_increment=D("1"))

    kwargs = state_kwargs()
    original = kwargs["sell_first_leg_levels"][0]
    kwargs["sell_first_leg_levels"] = (
        replace(original, price=D("0.315"), tick_size=D("0.01")),
    )
    with pytest.raises(P.PolicyContractError, match="legal tick"):
        P.DecisionState(**kwargs)


def test_book_receipt_clocks_must_bind_the_decision_source() -> None:
    kwargs = state_kwargs()
    original = kwargs["buy_complement_levels"][0]
    kwargs["buy_complement_levels"] = (
        replace(original, recv_wall_ns=original.recv_wall_ns - 1),
    )
    with pytest.raises(P.PolicyContractError, match="source clocks"):
        P.DecisionState(**kwargs)


def test_buy_complement_book_direction_and_action_are_bound() -> None:
    kwargs = state_kwargs()
    original = kwargs["buy_complement_levels"][0]
    kwargs["buy_complement_levels"] = (
        replace(original, outcome_side="YES", action="SELL"),
    )
    with pytest.raises(P.PolicyContractError, match="outcome"):
        P.DecisionState(**kwargs)


def test_fok_terminal_cannot_shift_to_59ms() -> None:
    kwargs = state_kwargs()
    kwargs["fok_terminal_elapsed_ms"] = D("1059")
    kwargs["fok_terminal_wall_ns"] = (
        int(kwargs["decision_recv_wall_ns"]) + 59_000_000
    )
    kwargs["fok_terminal_mono_ns"] = (
        int(kwargs["decision_recv_mono_ns"]) + 59_000_000
    )
    with pytest.raises(P.PolicyContractError, match="decision \\+ 60ms"):
        P.DecisionState(**kwargs)


def test_log_odds_features_are_explicit_and_ticks_are_not_a_substitute() -> None:
    names = P.ALLOWED_MODEL_FEATURES
    for name in (
        "mid_logodds",
        "spread_logodds",
        "move_logodds_1s",
        "move_logodds_10s",
        "quote_skew_logodds",
    ):
        assert name in names
    assert "spread_ticks" not in names
    assert "mid_move_ticks" not in names
    assert P.choose_action(
        state(),
        model(feature="spread_ticks"),
        buffer_usd=D("0"),
    ).action == P.NO_DECISION


def test_missing_log_odds_or_bad_external_pin_fails_closed() -> None:
    st = state(features=log_features(move_1s=None))
    assert P.choose_action(st, model(), buffer_usd=D("0")).action == (
        P.NO_DECISION
    )
    bad_model = replace(
        model(),
        training_data_pin=pin("MARKET_CONTEXT", "not-training"),
    )
    assert P.choose_action(st, bad_model, buffer_usd=D("0")).action == (
        P.NO_DECISION
    )


def test_120s_event_can_change_action_without_a_keep_timeout() -> None:
    early = P.DecisionState(
        **state_kwargs(elapsed_ms="500", move_1s=D("-0.1"))
    )
    late = P.DecisionState(
        **state_kwargs(elapsed_ms="120000", move_1s=D("0.1"))
    )
    assert P.choose_action(
        early, model(), buffer_usd=D("0")
    ).action == P.SUBMIT_FOK
    assert P.choose_action(
        late, model(), buffer_usd=D("0")
    ).action == P.KEEP
    assert late.fok_terminal_elapsed_ms == D("120060")


def test_keep_and_submit_fok_capital_timelines_are_separate() -> None:
    st = state()
    keep_receipts = (
        release(
            st,
            component=P.FIRST_LEG_BASIS,
            action=P.KEEP,
            elapsed_ms="2000",
            route_type=None,
        ),
        release(
            st,
            component=P.RESTING_COMPLEMENT_RESERVE,
            action=P.KEEP,
            elapsed_ms="1500",
            route_type=None,
        ),
    )
    keep_profile = P.capital_profile(
        st,
        decision_action=P.KEEP,
        exit_route_type=None,
        release_receipts=keep_receipts,
    )
    assert keep_profile.pending_fok_reserve_usd == D("0")
    assert keep_profile.pending_fok_dollar_seconds == D("0")

    selected = P.evaluate_exit_routes(st).selected
    fok_receipts = (
        release(
            st,
            component=P.FIRST_LEG_BASIS,
            action=P.SUBMIT_FOK,
            elapsed_ms="1060",
            route_type=selected.route_type,
        ),
        release(
            st,
            component=P.RESTING_COMPLEMENT_RESERVE,
            action=P.SUBMIT_FOK,
            elapsed_ms="1060",
            route_type=selected.route_type,
        ),
        release(
            st,
            component=P.PENDING_FOK_RESERVE,
            action=P.SUBMIT_FOK,
            elapsed_ms="1060",
            route_type=selected.route_type,
        ),
    )
    fok_profile = P.capital_profile(
        st,
        decision_action=P.SUBMIT_FOK,
        exit_route_type=selected.route_type,
        release_receipts=fok_receipts,
    )
    assert fok_profile.pending_fok_reserve_usd > D("0")
    assert fok_profile.pending_fok_dollar_seconds > D("0")
    assert fok_profile.total_dollar_seconds > (
        fok_profile.first_leg_dollar_seconds
        + fok_profile.resting_complement_dollar_seconds
    )


def test_release_receipts_bind_source_action_and_route() -> None:
    st = state()
    receipts = (
        release(
            st,
            component=P.FIRST_LEG_BASIS,
            action=P.KEEP,
            elapsed_ms="1500",
            route_type=None,
        ),
        release(
            st,
            component=P.RESTING_COMPLEMENT_RESERVE,
            action=P.SUBMIT_FOK,
            elapsed_ms="1500",
            route_type=P.SELL_FIRST_LEG,
        ),
    )
    with pytest.raises(P.PolicyContractError, match="decision action"):
        P.capital_profile(
            st,
            decision_action=P.KEEP,
            exit_route_type=None,
            release_receipts=receipts,
        )


def test_zero_time_atom_peak_preserves_first_basis_plus_resting_reserve() -> None:
    st = P.DecisionState(**state_kwargs(elapsed_ms="0"))
    receipts = (
        release(
            st,
            component=P.FIRST_LEG_BASIS,
            action=P.KEEP,
            elapsed_ms="0",
            route_type=None,
        ),
        release(
            st,
            component=P.RESTING_COMPLEMENT_RESERVE,
            action=P.KEEP,
            elapsed_ms="0",
            route_type=None,
        ),
    )
    profile = P.capital_profile(
        st,
        decision_action=P.KEEP,
        exit_route_type=None,
        release_receipts=receipts,
    )
    assert profile.first_leg_basis_usd == D("0.60")
    assert profile.resting_complement_reserve_usd == D("1.38")
    assert profile.peak_capital_usd == D("1.98")
    assert profile.total_dollar_seconds == D("0")
