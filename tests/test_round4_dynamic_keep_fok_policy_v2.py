from __future__ import annotations

from dataclasses import fields, replace
from decimal import Decimal
import hashlib
from pathlib import Path

import pytest

from tools.research.crypto_mm import round4_dynamic_keep_fok_policy_v2 as P


D = Decimal
BASE_WALL_NS = 1_800_000_000_000_000_000
BASE_MONO_NS = 8_000_000_000_000_000


def sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def observed(
    *,
    flow_5s: Decimal | None = D("0"),
) -> P.ObservedFeatures:
    return P.ObservedFeatures(
        queue_ahead_qty=D("3"),
        better_depth_qty=D("2"),
        complement_flow_1s_qty=D("0"),
        complement_flow_5s_qty=flow_5s,
        touch_distance_ticks=D("1"),
        spread_ticks=D("2"),
        fair_move_since_fill=D("0"),
        tte_ms=D("300000"),
    )


def state_kwargs(
    *,
    elapsed_ms: str = "1000",
    first_side: str = "YES",
    first_price: str = "0.30",
    complement_price: str = "0.69",
    first_qty: str = "2",
    first_maker_fee: str = "0",
    complement_maker_fee: str = "0",
    flow_5s: Decimal | None = D("0"),
    levels: tuple[P.BookLevel, ...] | None = None,
) -> dict[str, object]:
    elapsed = D(elapsed_ms)
    elapsed_ns = int(elapsed * D("1000000"))
    decision_wall = BASE_WALL_NS + elapsed_ns
    decision_mono = BASE_MONO_NS + elapsed_ns
    terminal_elapsed = elapsed + D("60")
    complement_side = "NO" if first_side == "YES" else "YES"
    if levels is None:
        levels = (
            P.BookLevel(D(complement_price), D("1")),
            P.BookLevel(D(complement_price), D("1")),
        )
    return {
        "event_id": f"decision-{first_side}-{elapsed_ms}",
        "first_fill_side": first_side,
        "first_fill_price": D(first_price),
        "first_fill_qty": D(first_qty),
        "first_maker_fee_usd": D(first_maker_fee),
        "complement_side": complement_side,
        "complement_price": D(complement_price),
        "complement_qty": D(first_qty),
        "complement_maker_fee_usd": D(complement_maker_fee),
        "fok_limit_price": max(level.price for level in levels),
        "visible_fok_levels": levels,
        "observed": observed(flow_5s=flow_5s),
        "first_fill_recv_wall_ns": BASE_WALL_NS,
        "first_fill_recv_mono_ns": BASE_MONO_NS,
        "decision_elapsed_ms": elapsed,
        "decision_recv_wall_ns": decision_wall,
        "decision_recv_mono_ns": decision_mono,
        "feature_asof_wall_ns": decision_wall,
        "feature_asof_mono_ns": decision_mono,
        "source_max_recv_wall_ns": decision_wall - 1,
        "source_max_recv_mono_ns": decision_mono - 1,
        "fok_terminal_elapsed_ms": terminal_elapsed,
        "fok_terminal_wall_ns": decision_wall + 60_000_000,
        "fok_terminal_mono_ns": decision_mono + 60_000_000,
    }


def make_state(**overrides: object) -> P.DecisionState:
    kwargs = state_kwargs()
    kwargs.update(overrides)
    return P.DecisionState(**kwargs)


def model(
    *,
    feature_name: str = "complement_flow_5s_qty",
    coefficient: str = "1",
    lower: str = "-10",
    upper: str = "10",
    intercept: str = "0",
    uncertainty: str = "0",
) -> P.TrainOnlyLCBModel:
    return P.TrainOnlyLCBModel(
        model_id="train-lodo-v1",
        training_role="TRAIN_ONLY",
        training_receipt_sha256=sha("train-only"),
        delta_estimand=P.DELTA_ESTIMAND,
        lcb_definition=P.LCB_DEFINITION,
        intercept_mean_delta_usd=D(intercept),
        uncertainty_radius_usd=D(uncertainty),
        features=(
            P.ModelFeature(
                name=feature_name,
                coefficient=D(coefficient),
                support_lower=D(lower),
                support_upper=D(upper),
            ),
        ),
    )


def test_module_is_offline_only_and_does_not_import_rejected_contracts() -> None:
    assert P.CANDIDATE_ALLOWED is False
    assert P.SHADOW_ALLOWED is False
    assert P.LIVE_ALLOWED is False
    assert P.ACTION_SEAL_ALLOWED is False
    source = Path(P.__file__).read_text()
    assert "round4_postfill_state_contract" not in source
    assert "round4_table_builder" not in source


def test_timestamp_contract_rejects_a_59ms_fok_terminal_shift() -> None:
    valid = P.DecisionState(**state_kwargs())
    with pytest.raises(P.PolicyContractError, match="exactly decision \\+ 60ms"):
        replace(
            valid,
            fok_terminal_elapsed_ms=valid.decision_elapsed_ms + D("59"),
            fok_terminal_wall_ns=valid.decision_recv_wall_ns + 59_000_000,
            fok_terminal_mono_ns=valid.decision_recv_mono_ns + 59_000_000,
        )


def test_timestamp_contract_requires_exact_decision_and_causal_asof() -> None:
    kwargs = state_kwargs(elapsed_ms="1234.567")
    state = P.DecisionState(**kwargs)
    assert state.decision_recv_wall_ns == BASE_WALL_NS + 1_234_567_000

    kwargs["decision_recv_wall_ns"] = int(kwargs["decision_recv_wall_ns"]) + 1
    with pytest.raises(P.PolicyContractError, match="first fill \\+ elapsed"):
        P.DecisionState(**kwargs)

    kwargs = state_kwargs()
    kwargs["feature_asof_mono_ns"] = int(kwargs["decision_recv_mono_ns"]) - 1
    with pytest.raises(P.PolicyContractError, match="asof must equal decision"):
        P.DecisionState(**kwargs)


@pytest.mark.parametrize(
    ("first_side", "first_price", "complement_price"),
    (
        ("YES", "0.40", "0.59"),
        ("NO", "0.63", "0.36"),
    ),
)
def test_pair_gain_is_recomputed_for_both_sides_at_99c(
    first_side: str,
    first_price: str,
    complement_price: str,
) -> None:
    state = P.DecisionState(
        **state_kwargs(
            first_side=first_side,
            first_price=first_price,
            complement_price=complement_price,
            first_maker_fee="0.001",
            complement_maker_fee="0.002",
        )
    )
    pair = P.pair_economics(state)
    assert pair.pair_cost_per_contract == D("0.99")
    assert pair.gross_gain_usd == D("0.02")
    assert pair.maker_fees_usd == D("0.003")
    assert pair.net_gain_usd == D("0.017")


def test_pair_cost_above_99c_is_rejected() -> None:
    with pytest.raises(P.PolicyContractError, match="pair cost ceiling"):
        P.DecisionState(
            **state_kwargs(first_price="0.40", complement_price="0.60")
        )


def test_caller_cannot_supply_pair_gain_or_pnl() -> None:
    names = {field.name for field in fields(P.DecisionState)}
    assert "pair_gain_usd" not in names
    assert "reported_pnl_usd" not in names

    kwargs = state_kwargs()
    kwargs["pair_gain_usd"] = D("999")
    with pytest.raises(TypeError):
        P.DecisionState(**kwargs)

    kwargs = state_kwargs()
    kwargs["reported_pnl_usd"] = D("999")
    with pytest.raises(TypeError):
        P.DecisionState(**kwargs)


def test_visible_fok_walks_levels_and_recomputes_rounded_fee() -> None:
    levels = (
        P.BookLevel(D("0.68"), D("1")),
        P.BookLevel(D("0.69"), D("3")),
    )
    state = P.DecisionState(**state_kwargs(levels=levels))
    route = P.walk_visible_fok(state)
    assert route.full_fill is True
    assert tuple((s.price, s.quantity) for s in route.executions) == (
        (D("0.68"), D("1")),
        (D("0.69"), D("1")),
    )
    # 0.07*q*p*(1-p), ceiling-rounded to $0.0001 for each execution.
    assert route.taker_fee_usd == D("0.0303")
    assert route.gross_pnl_usd == D("0.03")
    assert route.net_pnl_usd == D("-0.0003")
    assert route.terminal_elapsed_ms == state.decision_elapsed_ms + D("60")


def test_fee_rule_is_injected_and_applied_to_each_executed_slice() -> None:
    calls: list[tuple[Decimal, Decimal]] = []

    def fee(price: Decimal, quantity: Decimal) -> Decimal:
        calls.append((price, quantity))
        return quantity * D("0.01")

    levels = (
        P.BookLevel(D("0.67"), D("0.5")),
        P.BookLevel(D("0.68"), D("2")),
    )
    state = P.DecisionState(**state_kwargs(first_qty="1.5", levels=levels))
    route = P.walk_visible_fok(state, fee_fn=fee)
    assert calls[:2] == [(D("0.67"), D("0.5")), (D("0.68"), D("1.0"))]
    assert route.taker_fee_usd == D("0.015")


def test_choose_action_is_strict_lcb_over_buffer() -> None:
    keep = P.choose_action(
        make_state(observed=observed(flow_5s=D("1"))),
        model(),
        buffer_usd=D("0.25"),
    )
    assert keep.action == P.KEEP
    assert keep.lcb_delta_usd == D("1")

    equal = P.choose_action(
        make_state(observed=observed(flow_5s=D("0.25"))),
        model(),
        buffer_usd=D("0.25"),
    )
    assert equal.action == P.FLATTEN_FOK


def test_120s_event_is_legal_and_can_change_the_action() -> None:
    trained = model()
    early = P.DecisionState(
        **state_kwargs(elapsed_ms="500", flow_5s=D("-1"))
    )
    later = P.DecisionState(
        **state_kwargs(elapsed_ms="120000", flow_5s=D("1"))
    )
    assert P.choose_action(early, trained, buffer_usd=D("0")).action == (
        P.FLATTEN_FOK
    )
    assert P.choose_action(later, trained, buffer_usd=D("0")).action == P.KEEP
    assert later.fok_terminal_elapsed_ms == D("120060")


def test_missing_model_feature_out_of_support_and_nonfinite_fail_closed() -> None:
    state = make_state()
    assert P.choose_action(state, None, buffer_usd=D("0")).action == (
        P.NO_DECISION
    )

    missing = make_state(observed=observed(flow_5s=None))
    assert P.choose_action(missing, model(), buffer_usd=D("0")).action == (
        P.NO_DECISION
    )

    outside = make_state(observed=observed(flow_5s=D("11")))
    assert P.choose_action(outside, model(), buffer_usd=D("0")).action == (
        P.NO_DECISION
    )

    nonfinite = make_state(observed=observed(flow_5s=D("NaN")))
    assert P.choose_action(nonfinite, model(), buffer_usd=D("0")).action == (
        P.NO_DECISION
    )

    broken_model = replace(model(), uncertainty_radius_usd=D("Infinity"))
    assert P.choose_action(state, broken_model, buffer_usd=D("0")).action == (
        P.NO_DECISION
    )


def test_capital_integral_contains_all_three_real_release_intervals() -> None:
    state = P.DecisionState(
        **state_kwargs(
            first_maker_fee="0.001",
            complement_maker_fee="0.002",
        )
    )
    releases = P.CapitalReleaseSchedule(
        first_leg_release_elapsed_ms=D("1060"),
        resting_complement_release_elapsed_ms=D("1020"),
        pending_fok_release_elapsed_ms=D("1060"),
    )
    profile = P.capital_profile(state, releases)

    assert profile.first_leg_basis_usd == D("0.601")
    assert profile.resting_complement_reserve_usd == D("1.382")
    assert profile.pending_fok_reserve_usd == D("1.4100")
    assert profile.first_leg_dollar_seconds == D("0.63706")
    assert profile.resting_complement_dollar_seconds == D("1.40964")
    assert profile.pending_fok_dollar_seconds == D("0.08460")
    assert profile.total_dollar_seconds == D("2.13130")
    assert profile.peak_capital_usd == D("3.3930")

    # A two-term calculation is detectably incomplete.
    omitted_pending = (
        profile.first_leg_dollar_seconds
        + profile.resting_complement_dollar_seconds
    )
    assert omitted_pending != profile.total_dollar_seconds

    assert P.capital_at_elapsed(
        state, releases, elapsed_ms=D("1010")
    ) == D("3.3930")
    assert P.capital_at_elapsed(
        state, releases, elapsed_ms=D("1030")
    ) == D("2.0110")


def test_capital_schedule_cannot_omit_or_shift_pending_fok_release() -> None:
    state = make_state()
    with pytest.raises(TypeError):
        P.CapitalReleaseSchedule(
            first_leg_release_elapsed_ms=D("1060"),
            resting_complement_release_elapsed_ms=D("1060"),
        )

    bad = P.CapitalReleaseSchedule(
        first_leg_release_elapsed_ms=D("1060"),
        resting_complement_release_elapsed_ms=D("1060"),
        pending_fok_release_elapsed_ms=D("1059"),
    )
    with pytest.raises(P.PolicyContractError, match="pending FOK release"):
        P.capital_profile(state, bad)
