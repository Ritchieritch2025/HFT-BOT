"""Pure-synthetic tests for the sealed ROUND4 two-stage model family."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest

from tools.research.crypto_mm import round4_model_fitter as F
from tools.research.crypto_mm.round4_model_fitter import (
    CLAIM,
    ENTRY_BINS_S,
    EtaMechanicalContract,
    POSTFILL_BINS_S,
    RIDGE_GRID,
    STAGE2_HORIZONS_S,
    SYNTHETIC_MARKER,
    SyntheticModelContractError,
    SyntheticRound4Dataset,
    action_economics,
    aj_direction_gate,
    build_stage1_observations,
    build_stage2_observations,
    fit_piecewise_ridge,
    fit_synthetic_round4,
)
from tools.research.crypto_mm.round4_table_builder import (
    CANDIDATE_STATUS,
    DISCOVERY_DATES,
    EXPERIMENT_ID,
    ForbiddenSourceError,
)


DAY_NS = 86_400 * 1_000_000_000
BASE_NS = 1_000_000_000_000


def split_intervals(duration_s, bins):
    result = []
    for left, right in zip(bins, bins[1:]):
        if left >= duration_s:
            break
        result.append((left, min(right, duration_s)))
    return result


def stage1_rows():
    rows = []
    durations = (0.5, 1.5, 4.0, 10.0, 20.0, 45.0, 90.0, 180.0, 300.0, 0.75, 3.0, 55.0)
    causes = (
        "YES_FIRST",
        "NO_FIRST",
        "YES_FIRST",
        "ADMIN_CENSOR",
        "NO_FIRST",
        "YES_FIRST",
        "NO_FIRST",
        "ADMIN_CENSOR",
        "YES_FIRST",
        "NO_FIRST",
        "YES_FIRST",
        "NO_FIRST",
    )
    for day_index, day in enumerate(sorted(DISCOVERY_DATES)):
        day_base = BASE_NS + day_index * DAY_NS
        for episode_index, (duration, cause) in enumerate(
            zip(durations, causes)
        ):
            episode_id = f"s1-{day}-{episode_index}"
            intervals = split_intervals(duration, ENTRY_BINS_S)
            for interval_index, (left, right) in enumerate(intervals):
                terminal = interval_index == len(intervals) - 1
                start_ns = day_base + int(left * 1_000_000_000)
                stop_ns = day_base + int(right * 1_000_000_000)
                signal = episode_index - 5 + day_index
                rows.append(
                    {
                        "data_origin": "SYNTHETIC",
                        "source_date_utc": day,
                        "entry_episode_id": episode_id,
                        "entry_action_id": "SYNTHETIC_ENTRY",
                        "interval_index": interval_index,
                        "interval_start_wall_ns": start_ns,
                        "interval_stop_wall_ns": stop_ns,
                        "feature_asof_wall_ns": start_ns,
                        "elapsed_start_ms": Decimal(str(left * 1000)),
                        "elapsed_stop_ms": Decimal(str(right * 1000)),
                        "at_risk": True,
                        "event_yes_first": int(
                            terminal and cause == "YES_FIRST"
                        ),
                        "event_no_first": int(
                            terminal and cause == "NO_FIRST"
                        ),
                        "admin_censor": int(
                            terminal and cause == "ADMIN_CENSOR"
                        ),
                        "data_invalid": 0,
                        "yes_same_price_ahead_fp": Decimal(
                            str(5 + episode_index + interval_index)
                        ),
                        "no_same_price_ahead_fp": Decimal(
                            str(10 + episode_index - interval_index / 10)
                        ),
                        "yes_better_depth_fp": Decimal(
                            str(2 + episode_index / 4)
                        ),
                        "no_better_depth_fp": Decimal(
                            str(3 + episode_index / 5)
                        ),
                        "yes_flow_10s_fp": Decimal(str(signal + 2)),
                        "no_flow_10s_fp": Decimal(str(-signal + 1)),
                        "yes_flow_60s_fp": Decimal(str(signal * 3 + 4)),
                        "no_flow_60s_fp": Decimal(str(-signal * 2 + 3)),
                        "touch_imbalance": Decimal(str(signal / 20)),
                        "spread_e4": 100 + (episode_index % 3) * 10,
                        "mid_move_1s_e4": signal * 2,
                        "mid_move_10s_e4": signal * 5,
                        "tte_ms": Decimal(str(600_000 - left * 1000)),
                        "yes_order_age_ms": Decimal(str(left * 1000)),
                        "no_order_age_ms": Decimal(str(left * 1000)),
                    }
                )
    return tuple(rows)


def stage2_rows():
    rows = []
    durations = (0.2, 0.4, 0.75, 1.5, 3.0, 7.0, 20.0, 45.0, 60.0, 0.3, 2.5, 15.0)
    causes = (
        "COMPLEMENT_FILL",
        "INVENTORY_EXIT",
        "COMPLEMENT_FILL",
        "ADMIN_CENSOR",
        "INVENTORY_EXIT",
        "COMPLEMENT_FILL",
        "INVENTORY_EXIT",
        "ADMIN_CENSOR",
        "COMPLEMENT_FILL",
        "INVENTORY_EXIT",
        "COMPLEMENT_FILL",
        "INVENTORY_EXIT",
    )
    for day_index, day in enumerate(sorted(DISCOVERY_DATES)):
        day_base = BASE_NS + day_index * DAY_NS + 10_000_000_000
        for episode_index, (duration, cause) in enumerate(
            zip(durations, causes)
        ):
            action_id = f"s2-{day}-{episode_index}"
            action_kind = "KEEP" if episode_index % 2 == 0 else "REPRICE"
            first_side = "YES" if episode_index % 3 else "NO"
            intervals = split_intervals(duration, POSTFILL_BINS_S)
            for interval_index, (left, right) in enumerate(intervals):
                terminal = interval_index == len(intervals) - 1
                start_ns = day_base + int(left * 1_000_000_000)
                stop_ns = day_base + int(right * 1_000_000_000)
                signal = episode_index - 4 + day_index
                rows.append(
                    {
                        "data_origin": "SYNTHETIC",
                        "source_date_utc": day,
                        "postfill_action_id": action_id,
                        "postfill_episode_id": action_id,
                        "interval_index": interval_index,
                        "interval_start_wall_ns": start_ns,
                        "interval_stop_wall_ns": stop_ns,
                        "feature_asof_wall_ns": start_ns,
                        "elapsed_start_ms": Decimal(str(left * 1000)),
                        "elapsed_stop_ms": Decimal(str(right * 1000)),
                        "at_risk": True,
                        "event_complement_fill": int(
                            terminal and cause == "COMPLEMENT_FILL"
                        ),
                        "event_inventory_exit": int(
                            terminal and cause == "INVENTORY_EXIT"
                        ),
                        "admin_censor": int(
                            terminal and cause == "ADMIN_CENSOR"
                        ),
                        "data_invalid": 0,
                        "first_fill_side": first_side,
                        "action_kind": action_kind,
                        "complement_queue_position_fp": Decimal(
                            str(3 + episode_index + interval_index / 10)
                        ),
                        "complement_same_price_ahead_fp": Decimal(
                            str(5 + episode_index)
                        ),
                        "complement_better_depth_fp": Decimal(
                            str(2 + episode_index / 5)
                        ),
                        "complement_flow_10s_fp": Decimal(str(signal + 1)),
                        "complement_flow_60s_fp": Decimal(
                            str(signal * 4 + 2)
                        ),
                        "spread_e4": 100 + (episode_index % 4) * 10,
                        "mid_move_since_fill_e4": signal * 3,
                        "buy_complement_exit_pnl_usd": Decimal(
                            str(-0.01 - episode_index / 1000)
                        ),
                        "sell_first_exit_pnl_usd": Decimal(
                            str(-0.02 + episode_index / 2000)
                        ),
                        "pair_gain_if_complement_usd": Decimal("0.01"),
                        "tte_ms": Decimal(str(300_000 - left * 1000)),
                        "complement_order_age_ms": Decimal(
                            str(500 + left * 1000)
                        ),
                        "first_fill_elapsed_ms": Decimal(
                            str(250 + episode_index * 10)
                        ),
                        "first_price_e4": 3_000 + episode_index * 10,
                        "first_qty_fp": Decimal("1"),
                    }
                )
    return tuple(rows)


def atom_rows(stage2):
    rows = []
    first_by_episode = {}
    for row in stage2:
        first_by_episode.setdefault(row["postfill_episode_id"], row)
    for postfill_id, first in sorted(first_by_episode.items()):
        wall = int(first["interval_start_wall_ns"]) - 1_000_000
        rows.append(
            {
                "data_origin": "SYNTHETIC",
                "source_date_utc": first["source_date_utc"],
                "postfill_episode_id": postfill_id,
                "first_fill_side": first["first_fill_side"],
                "zero_time_atom": False,
                "first_fill_recv_wall_ns": wall,
                "first_fill_recv_mono_ns": wall // 10,
                "feature_asof_wall_ns": wall,
                "first_fill_stable_source_id": f"{postfill_id}-001",
                "complement_fill_stable_source_id": None,
                "receipt_envelope_id": None,
                "reconciliation_ok": True,
            }
        )
    for day_index, day in enumerate(sorted(DISCOVERY_DATES)):
        for atom_index in range(2):
            wall = (
                BASE_NS
                + day_index * DAY_NS
                + 9_000_000_000
                + atom_index * 1_000
            )
            postfill_id = f"atom-{day}-{atom_index}"
            rows.append(
                {
                    "data_origin": "SYNTHETIC",
                    "source_date_utc": day,
                    "postfill_episode_id": postfill_id,
                    "first_fill_side": (
                        "YES" if atom_index == 0 else "NO"
                    ),
                    "zero_time_atom": True,
                    "first_fill_recv_wall_ns": wall,
                    "first_fill_recv_mono_ns": wall // 10,
                    "feature_asof_wall_ns": wall,
                    "first_fill_stable_source_id": (
                        f"{postfill_id}-001"
                    ),
                    "complement_fill_stable_source_id": (
                        f"{postfill_id}-002"
                    ),
                    "receipt_envelope_id": f"envelope-{postfill_id}",
                    "entry_episode_id": f"entry-{postfill_id}",
                    "entry_action_id": "SYNTHETIC_ENTRY",
                    "source_rows_sha256": "c" * 64,
                    "complement_side": (
                        "NO" if atom_index == 0 else "YES"
                    ),
                    "complement_fill_price_e4": 6_900,
                    "complement_fill_qty_fp": Decimal("1"),
                    "complement_fill_fee_usd": Decimal("0"),
                    "reconciliation_ok": True,
                }
            )
    return tuple(rows)


def cycle_rows():
    rows = []
    for day_index, day in enumerate(sorted(DISCOVERY_DATES)):
        day_base = BASE_NS + day_index * DAY_NS + 20_000_000_000
        for action_index, policy_id in enumerate(
            ("SYNTH_KEEP", "SYNTH_REPRICE")
        ):
            for cycle_index in range(4):
                pnl_cents = (
                    (2 - cycle_index)
                    if policy_id == "SYNTH_KEEP"
                    else (cycle_index - 1)
                )
                if cycle_index == 3:
                    pnl_cents = 0  # admitted no-fill cycle remains included
                no_fill = cycle_index == 3
                rows.append(
                    {
                        "data_origin": "SYNTHETIC",
                        "source_date_utc": day,
                        "experiment_id": EXPERIMENT_ID,
                        "data_role": "DISCOVERY",
                        "market_ticker": f"SYNTH-{day}-{cycle_index}",
                        "market_cluster_id": (
                            f"SYNTH-{day}-{cycle_index}"
                        ),
                        "cycle_id": f"cycle-{day}-{cycle_index}",
                        "entry_action_id": "SYNTH_ENTRY",
                        "policy_id": policy_id,
                        "action_set_version": "PENDING_SYNTH_V0",
                        "admitted": True,
                        "terminal_type": "NO_FIRST_FILL" if no_fill else "PAIR_COMPLETE",
                        "first_fill_side": (
                            None
                            if no_fill
                            else ("YES" if cycle_index % 2 else "NO")
                        ),
                        "net_pnl_usd": Decimal(pnl_cents) / Decimal("100"),
                        "capital_dollar_seconds": Decimal(
                            10 + cycle_index + action_index
                        ),
                        "peak_episode_capital_usd": Decimal("1.00"),
                        "entry_quote_seconds": Decimal("10"),
                        "orphan_seconds": (
                            Decimal("0") if no_fill else Decimal("1")
                        ),
                        "maker_fee_usd": Decimal("0"),
                        "taker_fee_usd": Decimal("0"),
                        "source_rows_sha256": "b" * 64,
                        "reconciliation_ok": True,
                    }
                )
    return tuple(rows)


def synthetic_dataset():
    stage2 = stage2_rows()
    return SyntheticRound4Dataset(
        marker=SYNTHETIC_MARKER,
        logical_source_paths={
            day: (
                f"/synthetic/date={day}/stage1.memory",
                f"/synthetic/date={day}/stage2.memory",
            )
            for day in DISCOVERY_DATES
        },
        stage1_intervals=stage1_rows(),
        stage2_intervals=stage2,
        cycle_outcomes=cycle_rows(),
        stage2_atom_labels=atom_rows(stage2),
    )


@pytest.mark.parametrize(
    "forbidden_path",
    (
        "/synthetic/date=2026-07-23/source.memory",
        "/synthetic/date=20260723/source.memory",
        "/synthetic/date=2026-07-26/source.memory",
        "/synthetic/date=20260726/source.memory",
    ),
)
def test_forbidden_logical_path_stops_before_any_fit(forbidden_path):
    dataset = synthetic_dataset()
    paths = dict(dataset.logical_source_paths)
    paths["2026-07-20"] = (forbidden_path,)
    bad = replace(dataset, logical_source_paths=paths)
    with mock.patch.object(F, "fit_piecewise_ridge") as fit:
        with pytest.raises(ForbiddenSourceError):
            fit_synthetic_round4(bad)
        fit.assert_not_called()


def test_missing_synthetic_marker_is_rejected_before_fit():
    dataset = replace(synthetic_dataset(), marker="NOT_SYNTHETIC")
    with mock.patch.object(F, "fit_piecewise_ridge") as fit:
        with pytest.raises(SyntheticModelContractError, match="marker"):
            fit_synthetic_round4(dataset)
        fit.assert_not_called()


@pytest.mark.parametrize(
    "extra_token",
    ("2026-07-19", "20260719"),
)
def test_any_additional_date_token_stops_before_fit(extra_token):
    dataset = synthetic_dataset()
    paths = dict(dataset.logical_source_paths)
    paths["2026-07-20"] = (
        f"/synthetic/date=2026-07-20/archive={extra_token}/source.memory",
    )
    bad = replace(dataset, logical_source_paths=paths)
    with mock.patch.object(F, "fit_piecewise_ridge") as fit:
        with pytest.raises(ForbiddenSourceError, match="additional date"):
            fit_synthetic_round4(bad)
        fit.assert_not_called()


def test_fixed_time_bins_and_ioc_not_encoded_as_hazard():
    dataset = synthetic_dataset()
    stage1 = build_stage1_observations(dataset.stage1_intervals)
    assert {row["bin_index"] for row in stage1} == set(range(8))
    bad_stage2 = [dict(row) for row in dataset.stage2_intervals]
    bad_stage2[0]["action_kind"] = "IOC"
    with pytest.raises(SyntheticModelContractError, match="deterministic"):
        build_stage2_observations(
            bad_stage2,
            "event_complement_fill",
        )


def test_data_invalid_requires_whole_market_day_rollback_before_fit():
    dataset = synthetic_dataset()
    rows = [dict(row) for row in dataset.stage1_intervals]
    final = next(
        row
        for row in rows
        if row["entry_episode_id"].endswith("-0")
        and row["event_yes_first"] == 1
    )
    final["event_yes_first"] = 0
    final["data_invalid"] = 1
    bad = replace(dataset, stage1_intervals=tuple(rows))
    with mock.patch.object(F, "fit_piecewise_ridge") as fit:
        with pytest.raises(
            SyntheticModelContractError,
            match="entire market-day",
        ):
            fit_synthetic_round4(bad)
        fit.assert_not_called()


def test_zero_time_atom_never_accepts_epsilon_or_continuous_risk():
    dataset = synthetic_dataset()
    atom_labels = [dict(row) for row in dataset.stage2_atom_labels]
    positive = next(row for row in atom_labels if row["zero_time_atom"])
    positive["epsilon_ms"] = Decimal("0.001")
    bad = replace(dataset, stage2_atom_labels=tuple(atom_labels))
    with mock.patch.object(F, "fit_piecewise_ridge") as fit:
        with pytest.raises(SyntheticModelContractError, match="epsilon"):
            fit_synthetic_round4(bad)
        fit.assert_not_called()

    atom_labels = [dict(row) for row in dataset.stage2_atom_labels]
    positive = next(row for row in atom_labels if row["zero_time_atom"])
    continuous = [dict(row) for row in dataset.stage2_intervals]
    cloned = dict(continuous[0])
    cloned["postfill_episode_id"] = positive["postfill_episode_id"]
    cloned["postfill_action_id"] = "illegal-atom-continuous"
    continuous.append(cloned)
    bad = replace(dataset, stage2_intervals=tuple(continuous))
    with mock.patch.object(F, "fit_piecewise_ridge") as fit:
        with pytest.raises(
            SyntheticModelContractError,
            match="continuous hazard",
        ):
            fit_synthetic_round4(bad)
        fit.assert_not_called()


def test_ridge_grid_is_fail_closed():
    observations = build_stage1_observations(stage1_rows())
    with pytest.raises(SyntheticModelContractError, match="sealed grid"):
        fit_piecewise_ridge(
            observations,
            bins_s=ENTRY_BINS_S,
            feature_names=F.STAGE1_FEATURES,
            category_names=("target_side_no",),
            alpha=0.01,
        )


def test_interval_gap_is_rejected_before_any_model_fit():
    dataset = synthetic_dataset()
    rows = [dict(row) for row in dataset.stage1_intervals]
    target = next(
        row
        for row in rows
        if row["entry_episode_id"].endswith("-1")
        and row["interval_index"] == 1
    )
    target["interval_start_wall_ns"] += 1
    bad = replace(dataset, stage1_intervals=tuple(rows))
    with mock.patch.object(F, "fit_piecewise_ridge") as fit:
        with pytest.raises(SyntheticModelContractError, match="overlap/gap"):
            fit_synthetic_round4(bad)
        fit.assert_not_called()


def test_full_two_stage_synthetic_fit_never_reads_or_selects_candidate():
    dataset = synthetic_dataset()
    with mock.patch.object(
        Path,
        "read_bytes",
        side_effect=AssertionError("source read forbidden"),
    ):
        bundle = fit_synthetic_round4(dataset)
    report = bundle.report
    assert report["status"] == "SYNTHETIC_MODEL_CONTRACT_TEST_ONLY"
    assert report["candidate_status"] == CANDIDATE_STATUS
    assert report["claim"] == CLAIM
    assert report["deployable"] is False
    assert report["live_authorized"] is False
    assert report["selection_performed"] is False
    assert report["source_guard"]["status"] == (
        "SYNTHETIC_PATHS_VALIDATED_NOT_OPENED"
    )
    assert report["stage1"]["selection"][
        "selected_alpha_for_synthetic_test"
    ] in RIDGE_GRID
    assert report["stage2"]["selection"][
        "selected_alpha_for_synthetic_test"
    ] in RIDGE_GRID
    assert report["stage1"]["probability_identity_max_abs_error"] < 1e-10
    assert report["stage2"]["probability_identity_max_abs_error"] < 1e-10
    assert set(report["stage2"]["crossfit_calibration"][
        "COMPLEMENT_FILL"
    ]) == {str(value) for value in STAGE2_HORIZONS_S}
    assert set(report["stage2"]["subgroup_calibration"]["first_side"]) == {
        "YES",
        "NO",
    }
    assert report["stage2"]["ioc_handling"].startswith("deterministic")
    assert bundle.stage1_model.receipt()["synthetic_only"] is True
    assert bundle.stage2_complement_model.receipt()[
        "synthetic_only"
    ] is True
    assert bundle.stage1_model.converged is True
    assert bundle.stage2_complement_model.converged is True
    assert bundle.stage2_exit_model.converged is True
    assert bundle.zero_time_atom_model.receipt()[
        "epsilon_interval_used"
    ] is False
    assert report["stage1"]["curve_projection"].endswith(
        "no future feature path consumed"
    )
    assert report["stage2"]["baselines"]["eta_mechanical"]["status"] == (
        "ETA_BASELINE_NOT_IDENTIFIED"
    )
    assert report["zero_time_atom"]["continuous_hazard_input"] is False
    assert report["zero_time_atom"]["elapsed_time"] == 0
    assert report["action_seal_allowed"] is False


def test_calibration_and_nonparametric_outputs_are_present():
    report = fit_synthetic_round4(synthetic_dataset()).report
    stage1_metric = report["stage1"]["crossfit_calibration"][
        "YES_FIRST"
    ]["300.0"]
    stage2_metric = report["stage2"]["crossfit_calibration"][
        "COMPLEMENT_FILL"
    ]["60.0"]
    assert stage1_metric["ipcw_brier"] is not None
    assert stage2_metric["ipcw_brier"] is not None
    assert "calibration_intercept" in stage1_metric
    assert "calibration_slope" in stage2_metric
    assert set(report["stage1"]["crossfit_rcll_by_cause"]) == {
        "YES_FIRST",
        "NO_FIRST",
        "combined",
    }
    assert report["stage1"]["baselines"]["pooled_null"][
        "continuous_features"
    ] == ()
    assert report["stage1"]["aalen_johansen"]["300.0"][
        "identity_sum"
    ] == pytest.approx(1.0)
    assert report["stage2"]["aalen_johansen"]["60.0"][
        "identity_sum"
    ] == pytest.approx(1.0)
    assert report["model_gate_status"] in (
        "SYNTHETIC_DIRECTION_GATE_PASS",
        "MODEL_MISSPECIFIED",
    )


def test_eta_interface_and_aj_conflict_gate_are_explicit():
    eta = EtaMechanicalContract(
        queue_ahead_field="ahead",
        consumption_rate_field="rate",
        clip_fp=1.0,
        consumption_sign=1,
        rate_unit="contracts_per_second",
    )
    assert 0 < eta.completion_probability(
        {"ahead": 9, "rate": 2},
        5,
    ) < 1
    with pytest.raises(SyntheticModelContractError, match="explicit"):
        EtaMechanicalContract(
            queue_ahead_field="ahead",
            consumption_rate_field="rate",
            clip_fp=1.0,
            consumption_sign=0,
            rate_unit="contracts_per_second",
        )
    gate = aj_direction_gate(
        {"YES": 0.7, "NO": 0.3},
        {"YES": 0.2, "NO": 0.8},
    )
    assert gate["status"] == "MODEL_MISSPECIFIED"
    assert gate["action_seal_allowed"] is False


def test_action_ev_and_capital_time_include_no_fill_without_ranking():
    report = action_economics(cycle_rows())
    groups = report["groups_unranked"]
    assert report["selection_performed"] is False
    assert report["includes_no_fill_admitted_cycles"] is True
    keep = "SYNTH_ENTRY|SYNTH_KEEP|PENDING_SYNTH_V0"
    reprice = "SYNTH_ENTRY|SYNTH_REPRICE|PENDING_SYNTH_V0"
    assert set(groups) == {keep, reprice}
    assert groups[keep][
        "admitted_cycles_including_no_fill"
    ] == 12
    total = Decimal(groups[keep]["total_net_pnl_usd"])
    ev_cycle = Decimal(groups[keep]["ev_usd_per_cycle"])
    capital_time = Decimal(
        groups[keep]["total_capital_dollar_seconds"]
    )
    ev_capital_time = Decimal(
        groups[keep]["ev_usd_per_locked_dollar_second"]
    )
    assert ev_cycle == total / Decimal("12")
    assert ev_capital_time == total / capital_time
    assert Decimal(
        groups[keep]["cents_per_locked_dollar_hour"]
    ) == Decimal("360000") * ev_capital_time


@pytest.mark.parametrize(
    "mutation,match",
    (
        ({"terminal_type": "DATA_INVALID"}, "DATA_INVALID"),
        ({"reconciliation_ok": False}, "not green"),
        ({"terminal_type": "PENDING"}, "nonterminal"),
    ),
)
def test_action_economics_rejects_invalid_terminal_or_ledger(
    mutation,
    match,
):
    rows = [dict(row) for row in cycle_rows()]
    rows[0].update(mutation)
    with pytest.raises(SyntheticModelContractError, match=match):
        action_economics(rows)


def test_action_economics_rejects_duplicate_pk_and_no_fill_pnl():
    rows = list(cycle_rows())
    with pytest.raises(SyntheticModelContractError, match="duplicate"):
        action_economics(rows + [dict(rows[0])])
    corrupt = [dict(row) for row in rows]
    no_fill = next(
        row for row in corrupt if row["terminal_type"] == "NO_FIRST_FILL"
    )
    no_fill["net_pnl_usd"] = Decimal("0.01")
    with pytest.raises(SyntheticModelContractError, match="NO_FIRST_FILL"):
        action_economics(corrupt)
