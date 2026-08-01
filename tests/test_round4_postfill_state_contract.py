from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
from pathlib import Path

import duckdb
import pytest

from tools.research.crypto_mm import round4_postfill_state_contract as C
from tools.research.crypto_mm.round4_table_builder import (
    EXPERIMENT_ID,
    ForbiddenSourceError,
)


DAY = "2026-07-20"
BASE_WALL_NS = 1_800_000_000_000_000_000
BASE_MONO_NS = 8_000_000_000_000_000
ROOT = Path(__file__).resolve().parents[1]
SUPPLEMENTAL_DDL = (
    ROOT
    / "tmp"
    / "crypto_mm_canary_20260726"
    / "round4"
    / "round4_postfill_state_contract.sql"
)
BASE_DDL = (
    ROOT
    / "tmp"
    / "crypto_mm_canary_20260726"
    / "round4"
    / "round4_two_stage_tables.sql"
)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def make_episode(
    episode_id: str = "ep",
    terminal_elapsed_ms: Decimal | int = 400,
    *,
    first_side: str = "YES",
    first_price_e4: int = 3_000,
    zero_time_atom: bool = False,
    terminal_kind: str | None = None,
) -> dict[str, object]:
    terminal_type = (
        C.ZERO_TIME_TERMINAL
        if zero_time_atom
        else terminal_kind or "COMPLEMENT_FILL"
    )
    terminal = Decimal(terminal_elapsed_ms)
    terminal_ns = int(terminal * Decimal("1000000"))
    row: dict[str, object] = {
        "experiment_id": EXPERIMENT_ID,
        "data_role": "DISCOVERY",
        "data_origin": C.DATA_ORIGIN,
        "source_date_utc": DAY,
        "market_ticker": f"KXBTC15M-{episode_id}",
        "postfill_episode_id": episode_id,
        "entry_episode_id": f"entry-{episode_id}",
        "entry_action_id": "ENTRY_PAIR_99",
        "source_rows_sha256": digest(f"episode-{episode_id}"),
        "first_fill_provenance": C.FILL_PROVENANCE,
        "first_fill_execution_nature": C.EXECUTION_NATURE,
        "first_fill_fee_provenance": C.MAKER_FEE_PROVENANCE,
        "first_fill_side": first_side,
        "first_fill_price_e4": first_price_e4,
        "first_fill_qty_fp": Decimal("2"),
        "first_fill_fee_usd": Decimal("0"),
        "first_fill_recv_wall_ns": BASE_WALL_NS,
        "first_fill_recv_mono_ns": BASE_MONO_NS,
        "first_fill_elapsed_ms": Decimal("125"),
        "first_fill_tte_ms": 120_000,
        "market_close_elapsed_ms": Decimal("120000.5"),
        "complement_order_id": f"order-{episode_id}",
        "complement_price_e4": 6_900,
        "complement_order_age_at_first_fill_ms": 100,
        "terminal_elapsed_ms": terminal,
        "terminal_wall_ns": BASE_WALL_NS + terminal_ns,
        "terminal_mono_ns": BASE_MONO_NS + terminal_ns,
        "terminal_type": terminal_type,
        "zero_time_atom": zero_time_atom,
        "market_day_gate_pass": True,
        "reconciliation_ok": True,
        "data_invalid": False,
    }
    complement_side = "NO" if first_side == "YES" else "YES"
    if zero_time_atom:
        row.update(
            {
                "receipt_envelope_id": f"envelope-{episode_id}",
                "first_fill_stable_source_id": f"{episode_id}-001",
                "complement_fill_stable_source_id": (
                    f"{episode_id}-002"
                ),
                "complement_side": complement_side,
                "complement_fill_price_e4": 6_900,
                "complement_fill_qty_fp": Decimal("2"),
                "complement_fill_fee_usd": Decimal("0"),
                "complement_fill_provenance": C.FILL_PROVENANCE,
                "complement_execution_nature": C.EXECUTION_NATURE,
                "complement_fee_provenance": C.MAKER_FEE_PROVENANCE,
            }
        )
    elif terminal_type == "COMPLEMENT_FILL":
        row.update(
            {
                "terminal_complement_fill_price_e4": 6_900,
                "terminal_complement_fill_qty_fp": Decimal("2"),
                "terminal_complement_fill_fee_usd": Decimal("0"),
                "terminal_complement_fill_provenance": C.FILL_PROVENANCE,
                "terminal_complement_execution_nature": C.EXECUTION_NATURE,
                "terminal_complement_fee_provenance": (
                    C.MAKER_FEE_PROVENANCE
                ),
                "terminal_complement_fill_stable_source_id": (
                    f"{episode_id}-terminal-fill"
                ),
            }
        )
    elif terminal_type == "HARD_FALLBACK":
        official_result = "NO" if first_side == "YES" else "YES"
        gross = (
            -Decimal(first_price_e4) / Decimal("10000")
            * Decimal("2")
        )
        row.update(
            {
                "terminal_keep_gross_pnl_usd": gross,
                "terminal_keep_first_fill_fee_usd": Decimal("0"),
                "terminal_keep_exit_fee_usd": Decimal("0"),
                "terminal_keep_maker_fee_usd": Decimal("0"),
                "terminal_keep_net_pnl_usd": gross,
                "official_market_result": official_result,
                "official_result_provenance": "OFFICIAL_MARKET_RESULT",
                "terminal_keep_reward_provenance": (
                    C.SETTLEMENT_PROVENANCE
                ),
                "terminal_keep_fee_provenance": (
                    C.SETTLEMENT_FEE_PROVENANCE
                ),
                "terminal_keep_stable_source_id": (
                    f"{episode_id}-official-settlement"
                ),
            }
        )
    return row


def default_slices(first_side: str) -> list[dict[str, object]]:
    if first_side == "YES":
        return [
            {"price_e4": 3_100, "qty_fp": Decimal("1")},
            {"price_e4": 3_000, "qty_fp": Decimal("1")},
        ]
    return [
        {"price_e4": 6_900, "qty_fp": Decimal("1")},
        {"price_e4": 7_000, "qty_fp": Decimal("1")},
    ]


def make_compact_pair(
    spec: dict[str, object],
    decision_index: int,
    *,
    slices: list[dict[str, object]] | None = None,
    roster_complete: bool = True,
    two_sided_touch_available: bool = True,
    complement_touch_available: bool = True,
    kernel_fair_available: bool = False,
    kernel_fair_e4: int = 6_349,
    kernel_source_time_ms: int | None = None,
) -> list[dict[str, object]]:
    elapsed = C.DECISION_GRID_MS[decision_index]
    decision_wall = int(spec["first_fill_recv_wall_ns"]) + (
        elapsed * 1_000_000
    )
    decision_mono = int(spec["first_fill_recv_mono_ns"]) + (
        elapsed * 1_000_000
    )
    raw_slices = deepcopy(
        default_slices(str(spec["first_fill_side"]))
        if slices is None
        else slices
    )
    normalized = tuple(
        (int(item["price_e4"]), Decimal(item["qty_fp"]))
        for item in raw_slices
    )
    executable = sum(
        (qty for _price, qty in normalized),
        Decimal("0"),
    )
    residual = Decimal(spec["first_fill_qty_fp"]) - executable
    economics = C._slice_economics(
        normalized,
        first_fill_side=str(spec["first_fill_side"]),
        first_price_e4=int(spec["first_fill_price_e4"]),
    )
    worst = normalized[-1][0] if residual == 0 and normalized else None
    book_side = C._canonical_book_side(str(spec["first_fill_side"]))
    limit, tick, fallback = C._adverse_limit(book_side, worst)
    first_fair = (
        kernel_fair_e4
        if spec["first_fill_side"] == "YES"
        else 10_000 - kernel_fair_e4
    )
    complement_fair = 10_000 - first_fair
    sealed_kernel_source_ms = (
        decision_wall // 1_000_000 - 5_000
        if kernel_source_time_ms is None
        else kernel_source_time_ms
    )
    common: dict[str, object] = {
        "source_date_utc": spec["source_date_utc"],
        "postfill_episode_id": spec["postfill_episode_id"],
        "decision_index": decision_index,
        "decision_elapsed_ms": elapsed,
        "action_family_version": C.ACTION_FAMILY_VERSION,
        "causal_source_rows_sha256": digest(
            f"{spec['postfill_episode_id']}-{decision_index}-state"
        ),
        "source_max_stable_id": (
            f"{spec['postfill_episode_id']}-{decision_index:02d}-source"
        ),
        "decision_recv_wall_ns": decision_wall,
        "decision_recv_mono_ns": decision_mono,
        "feature_asof_wall_ns": decision_wall,
        "source_max_recv_wall_ns": decision_wall,
        "source_max_recv_mono_ns": decision_mono,
        "first_fill_price_e4": spec["first_fill_price_e4"],
        "complement_price_e4": spec["complement_price_e4"],
        "complement_order_age_ms": (
            int(spec["complement_order_age_at_first_fill_ms"]) + elapsed
        ),
        "complement_touch_distance_e4": (
            100 if complement_touch_available else None
        ),
        "spread_e4": 200 if two_sided_touch_available else None,
        "mid_move_1s_e4": 0 if two_sided_touch_available else None,
        "mid_move_10s_e4": 0 if two_sided_touch_available else None,
        "mid_move_since_entry_e4": (
            0 if two_sided_touch_available else None
        ),
        "mid_move_since_fill_e4": (
            0 if two_sided_touch_available else None
        ),
        "flatten_limit_price_e4": limit,
        "flatten_adverse_tick_e4": tick,
        "tte_ms": int(spec["first_fill_tte_ms"]) - elapsed,
        "first_fill_side": spec["first_fill_side"],
        "first_fill_qty_fp": spec["first_fill_qty_fp"],
        "remaining_inventory_fp": spec["first_fill_qty_fp"],
        "complement_queue_position_fp": Decimal("4"),
        "complement_same_price_ahead_fp": Decimal("3"),
        "complement_better_depth_fp": Decimal("1"),
        "complement_flow_1s_fp": Decimal("1"),
        "complement_flow_5s_fp": Decimal("3"),
        "complement_flow_10s_fp": Decimal("6"),
        "complement_flow_60s_fp": Decimal("12"),
        "complement_flow_acceleration_fp": Decimal("4"),
        "touch_imbalance": (
            Decimal("0.1") if two_sided_touch_available else None
        ),
        "flatten_visible_gross_pnl_usd": economics["gross"],
        "flatten_visible_pnl_fee_low_usd": economics["net_low"],
        "flatten_visible_pnl_fee_base_usd": economics["net_base"],
        "flatten_visible_pnl_fee_high_usd": economics["net_high"],
        "flatten_visible_executable_qty_fp": executable,
        "flatten_visible_residual_inventory_fp": residual,
        "pair_gain_if_complement_usd": Decimal("0.02"),
        "first_fill_elapsed_ms": spec["first_fill_elapsed_ms"],
        "complement_order_id": spec["complement_order_id"],
        "complement_side": (
            "NO" if spec["first_fill_side"] == "YES" else "YES"
        ),
        "flatten_book_side": book_side,
        "flatten_limit_fallback": fallback,
        "flatten_visible_slices": raw_slices,
        "public_book_data_origin": C.DATA_ORIGIN,
        "flatten_counterfactual_provenance": (
            C.FOK_EXECUTION_PROVENANCE
        ),
        "two_sided_touch_available": two_sided_touch_available,
        "complement_touch_available": complement_touch_available,
        "kernel_fair_available": kernel_fair_available,
        "kernel_fair_e4": (
            kernel_fair_e4 if kernel_fair_available else None
        ),
        "first_leg_fair_edge_e4": (
            first_fair - int(spec["first_fill_price_e4"])
            if kernel_fair_available
            else None
        ),
        "complement_fair_edge_e4": (
            complement_fair - 6_900
            if kernel_fair_available
            else None
        ),
        "kernel_fair_move_since_entry_e4": (
            0 if kernel_fair_available else None
        ),
        "kernel_fair_move_since_fill_e4": (
            0 if kernel_fair_available else None
        ),
        "kernel_source_time_ms": (
            sealed_kernel_source_ms
            if kernel_fair_available
            else None
        ),
        "kernel_causality_kind": (
            "NON_CAUSAL_SENSITIVITY"
            if kernel_fair_available
            else None
        ),
        "simulated_strategy_order_registry_complete": roster_complete,
        "simulated_strategy_order_registry_sha256": digest(
            f"{spec['postfill_episode_id']}-{decision_index}-roster"
        ),
        "cancel_state": "NONE",
        "market_day_gate_pass": True,
        "reconciliation_ok": True,
        "data_invalid": False,
        "requested_qty_fp": spec["first_fill_qty_fp"],
        "legal_action": True,
        "skip_reason": None,
    }
    keep = deepcopy(common)
    keep.update(
        {
            "action_kind": "KEEP",
            "effective_latency_ms": Decimal("0"),
            "time_in_force": None,
            "self_trade_prevention_type": None,
            "fok_requires_prior_synthetic_cancel_applied": False,
            "fok_book_side": None,
            "fok_limit_price_e4": None,
            "fok_limit_fallback": None,
        }
    )
    flatten = deepcopy(common)
    flatten.update(
        {
            "action_kind": "FLATTEN_FOK",
            "effective_latency_ms": Decimal("60"),
            "time_in_force": "fill_or_kill",
            "self_trade_prevention_type": "taker_at_cross",
            "fok_requires_prior_synthetic_cancel_applied": True,
            "fok_book_side": book_side,
            "fok_limit_price_e4": limit,
            "fok_limit_fallback": fallback,
        }
    )
    return [keep, flatten]


def make_outcome(
    spec: dict[str, object],
    state: dict[str, object],
    terminal_type: str = "FOK_FULL",
) -> dict[str, object]:
    elapsed = int(state["decision_elapsed_ms"])
    planned = elapsed + C.FOK_EFFECTIVE_LATENCY_MS
    requested = Decimal(state["remaining_inventory_fp"])
    first_fee = Decimal(spec["first_fill_fee_usd"])
    first_cost = (
        Decimal(spec["first_fill_price_e4"]) / Decimal("10000")
        * requested
    )
    basis = first_cost + first_fee
    race = terminal_type == "CANCEL_RACE_PAIR"
    terminal = (
        Decimal(spec["terminal_elapsed_ms"])
        if race
        else Decimal(planned)
    )
    terminal_ns = int(terminal * Decimal("1000000"))
    terminal_wall = int(spec["first_fill_recv_wall_ns"]) + terminal_ns
    terminal_mono = int(spec["first_fill_recv_mono_ns"]) + terminal_ns
    slices: list[dict[str, object]]
    if terminal_type == "FOK_FULL":
        slices = deepcopy(state["flatten_visible_slices"])
        normalized = tuple(
            (int(item["price_e4"]), Decimal(item["qty_fp"]))
            for item in slices
        )
        economics = C._slice_economics(
            normalized,
            first_fill_side=str(spec["first_fill_side"]),
            first_price_e4=int(spec["first_fill_price_e4"]),
        )
        complement_qty = Decimal("0")
        fok_qty = requested
        residual = Decimal("0")
        gross = economics["gross"]
        maker_fee = first_fee
        taker = (
            economics["fee_low"],
            economics["fee_base"],
            economics["fee_high"],
        )
        conservative = gross - maker_fee - taker[2]
        simulation_reward_exact = False
        reward_kind = "FEE_BAND"
        capital_released = True
        capital = basis * terminal / Decimal("1000")
    elif terminal_type == "FOK_ZERO":
        slices = []
        complement_qty = Decimal("0")
        fok_qty = Decimal("0")
        residual = requested
        gross = Decimal("0")
        maker_fee = first_fee
        taker = (Decimal("0"),) * 3
        conservative = -basis
        simulation_reward_exact = False
        reward_kind = "LOWER_BOUND"
        capital_released = False
        capital = (
            basis
            * Decimal(spec["market_close_elapsed_ms"])
            / Decimal("1000")
        )
    else:
        slices = []
        complement_qty = requested
        fok_qty = Decimal("0")
        residual = Decimal("0")
        race_price = int(spec["terminal_complement_fill_price_e4"])
        gross = (
            Decimal(10_000)
            - Decimal(spec["first_fill_price_e4"])
            - Decimal(race_price)
        ) / Decimal("10000") * requested
        maker_fee = (
            first_fee
            + Decimal(spec["terminal_complement_fill_fee_usd"])
        )
        taker = (Decimal("0"),) * 3
        conservative = gross - maker_fee
        simulation_reward_exact = True
        reward_kind = "SIMULATION_EXACT"
        capital_released = True
        capital = basis * terminal / Decimal("1000")
    row: dict[str, object] = {
        "source_date_utc": spec["source_date_utc"],
        "postfill_episode_id": spec["postfill_episode_id"],
        "decision_index": state["decision_index"],
        "postfill_action_id": (
            f"{spec['postfill_episode_id']}::GRID::"
            f"{state['decision_index']}::FLATTEN_FOK"
        ),
        "data_origin": C.DATA_ORIGIN,
        "first_fill_provenance": C.FILL_PROVENANCE,
        "first_fill_execution_nature": C.EXECUTION_NATURE,
        "first_fill_fee_provenance": C.MAKER_FEE_PROVENANCE,
        "fok_execution_provenance": C.FOK_EXECUTION_PROVENANCE,
        "cancel_timing_provenance": C.CANCEL_TIMING_PROVENANCE,
        "self_cross_scope": C.SELF_CROSS_SCOPE,
        "terminal_type": terminal_type,
        "planned_effective_elapsed_ms": planned,
        "terminal_elapsed_ms": terminal,
        "terminal_wall_ns": terminal_wall,
        "terminal_mono_ns": terminal_mono,
        "synthetic_cancel_applied_wall_ns": (
            None if race else terminal_wall
        ),
        "synthetic_cancel_applied_mono_ns": (
            None if race else terminal_mono
        ),
        "synthetic_cancel_applied_sequence": None if race else 0,
        "synthetic_cancel_applied_stable_source_id": (
            None
            if race
            else f"{spec['postfill_episode_id']}-synthetic-cancel"
        ),
        "fok_processed_wall_ns": None if race else terminal_wall,
        "fok_processed_mono_ns": None if race else terminal_mono,
        "fok_processed_sequence": None if race else 1,
        "fok_processed_stable_source_id": (
            None
            if race
            else f"{spec['postfill_episode_id']}-fok"
        ),
        "feature_asof_wall_ns": terminal_wall,
        "source_max_recv_wall_ns": terminal_wall,
        "source_max_recv_mono_ns": terminal_mono,
        "source_max_stable_id": (
            f"{spec['postfill_episode_id']}-outcome-source"
        ),
        "outcome_source_rows_sha256": digest(
            f"{spec['postfill_episode_id']}-"
            f"{state['decision_index']}-{terminal_type}"
        ),
        "fok_sent": not race,
        "synthetic_cancel_applied_before_terminal": not race,
        "public_crossing_event_applied": False,
        "simulated_no_self_cross_verified_before_fok": (
            None if race else True
        ),
        "simulated_strategy_order_registry_sha256": state[
            "simulated_strategy_order_registry_sha256"
        ],
        "fok_book_side": state["flatten_book_side"],
        "fok_limit_price_e4": state["flatten_limit_price_e4"],
        "fok_limit_fallback": state["flatten_limit_fallback"],
        "requested_qty_fp": requested,
        "complement_fill_qty_fp": complement_qty,
        "fok_fill_qty_fp": fok_qty,
        "residual_inventory_fp": residual,
        "fok_fill_slices": slices,
        "gross_pnl_usd": gross,
        "maker_fee_usd": maker_fee,
        "taker_fee_low_usd": taker[0],
        "taker_fee_base_usd": taker[1],
        "taker_fee_high_usd": taker[2],
        "net_pnl_fee_low_usd": gross - maker_fee - taker[0],
        "net_pnl_fee_base_usd": gross - maker_fee - taker[1],
        "net_pnl_fee_high_usd": gross - maker_fee - taker[2],
        "conservative_reward_usd": conservative,
        "simulation_reward_exact": simulation_reward_exact,
        "reward_value_kind": reward_kind,
        "conservative_fit_usable": True,
        "capital_dollar_seconds": capital,
        "capital_released": capital_released,
        "reconciliation_ok": True,
        "data_invalid": False,
        "race_complement_price_e4": None,
        "race_complement_fee_usd": None,
        "race_complement_stable_source_id": None,
        "race_fill_provenance": None,
        "race_execution_nature": None,
        "race_fee_provenance": None,
    }
    if race:
        row.update(
            {
                "race_complement_price_e4": spec[
                    "terminal_complement_fill_price_e4"
                ],
                "race_complement_fee_usd": spec[
                    "terminal_complement_fill_fee_usd"
                ],
                "race_complement_stable_source_id": spec[
                    "terminal_complement_fill_stable_source_id"
                ],
                "race_fill_provenance": C.FILL_PROVENANCE,
                "race_execution_nature": C.EXECUTION_NATURE,
                "race_fee_provenance": C.MAKER_FEE_PROVENANCE,
            }
        )
    return row


def make_inputs(
    specs: list[dict[str, object]],
    *,
    outcome_kind: str = "FOK_FULL",
    slices_by_episode: dict[str, list[dict[str, object]]] | None = None,
) -> tuple[
    dict[str, list[str]],
    dict[str, list[str]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    compact: list[dict[str, object]] = []
    outcomes: list[dict[str, object]] = []
    for spec in specs:
        if spec["zero_time_atom"]:
            continue
        for index, _elapsed in C.required_decision_grid(
            spec["terminal_elapsed_ms"],
            zero_time_atom=False,
        ):
            pair = make_compact_pair(
                spec,
                index,
                slices=(
                    None
                    if slices_by_episode is None
                    else slices_by_episode.get(str(spec["postfill_episode_id"]))
                ),
            )
            compact.extend(pair)
            state = pair[0]
            kind = outcome_kind
            if (
                spec["terminal_type"] == "COMPLEMENT_FILL"
                and Decimal(state["decision_elapsed_ms"])
                < Decimal(spec["terminal_elapsed_ms"])
                < Decimal(
                    int(state["decision_elapsed_ms"])
                    + C.FOK_EFFECTIVE_LATENCY_MS
                )
            ):
                kind = "CANCEL_RACE_PAIR"
            outcomes.append(make_outcome(spec, state, kind))
    paths = {DAY: [f"/sealed/{DAY}/events.jsonl"]}
    expected = {
        DAY: [str(spec["postfill_episode_id"]) for spec in specs]
    }
    return paths, expected, compact, outcomes


def validate(
    specs: list[dict[str, object]],
    compact: list[dict[str, object]],
    outcomes: list[dict[str, object]],
) -> C.PostfillDDLBatch:
    paths = {DAY: [f"/sealed/{DAY}/events.jsonl"]}
    expected = {
        DAY: [str(spec["postfill_episode_id"]) for spec in specs]
    }
    return C.validate_and_serialize_postfill_rows(
        logical_source_paths=paths,
        expected_episode_ids_by_day=expected,
        episode_manifest=specs,
        compact_rows=compact,
        flatten_fok_outcomes=outcomes,
    )


def one_grid(
    *,
    first_side: str = "YES",
    first_price_e4: int = 3_000,
    slices: list[dict[str, object]] | None = None,
    outcome_kind: str = "FOK_FULL",
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    spec = make_episode(
        terminal_elapsed_ms=100,
        first_side=first_side,
        first_price_e4=first_price_e4,
    )
    pair = make_compact_pair(spec, 0, slices=slices)
    return spec, pair, [make_outcome(spec, pair[0], outcome_kind)]


def insert_rows(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    rows: tuple[dict[str, object], ...],
) -> None:
    for row in rows:
        columns = tuple(row)
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            [row[column] for column in columns],
        )


def test_v4_valid_pair_actions_outcomes_and_keep_transitions() -> None:
    spec = make_episode()
    _paths, _expected, compact, outcomes = make_inputs([spec])
    batch = validate([spec], compact, outcomes)

    assert C.CONTRACT_VERSION.endswith("_V4")
    assert len(batch.postfill_compact_causal_state) == 2
    assert len(batch.postfill_compact_causal_action) == 4
    assert len(batch.postfill_compact_flatten_fok_outcome) == 2
    assert len(batch.postfill_compact_keep_transition) == 2
    assert {
        row["action_kind"]
        for row in batch.postfill_compact_causal_action
    } == {"KEEP", "FLATTEN_FOK"}
    flatten = next(
        row
        for row in batch.postfill_compact_causal_action
        if row["action_kind"] == "FLATTEN_FOK"
    )
    assert flatten["fok_book_side"] == "ASK"
    assert flatten["fok_requires_prior_synthetic_cancel_applied"] is True
    assert (
        flatten["cancel_timing_provenance"]
        == C.CANCEL_TIMING_PROVENANCE
    )
    assert flatten["self_cross_scope"] == C.SELF_CROSS_SCOPE
    assert flatten["time_in_force"] == "fill_or_kill"
    assert flatten["self_trade_prevention_type"] == "taker_at_cross"
    assert flatten["reduce_only"] is True
    assert flatten["profit_gate_bypassed_for_risk_exit"] is True
    full = batch.postfill_compact_flatten_fok_outcome[0]
    assert full["reward_value_kind"] == "FEE_BAND"
    assert full["simulation_reward_exact"] is False
    assert full["taker_fee_low_usd"] == Decimal("0.0297")
    assert full["taker_fee_base_usd"] == Decimal("0.0297")
    assert full["taker_fee_high_usd"] == Decimal("0.04")
    assert full["maker_fee_usd"] == Decimal("0")
    assert full["net_pnl_fee_high_usd"] == Decimal("-0.03")
    assert full["synthetic_cancel_applied_before_terminal"] is True
    assert (
        full["simulated_no_self_cross_verified_before_fok"] is True
    )
    first, terminal = batch.postfill_compact_keep_transition
    assert first["transition_type"] == "NEXT_STATE"
    assert terminal["transition_type"] == "KEEP_TO_TERMINAL"
    assert terminal["keep_terminal_type"] == "COMPLEMENT_FILL"
    assert terminal["maker_fee_usd"] == Decimal("0")
    assert terminal["immediate_net_pnl_usd"] == Decimal("0.02")
    assert terminal["simulation_reward_exact"] is True
    assert batch.receipt()["live_authorized"] is False


@pytest.mark.parametrize(
    ("first_side", "price", "expected_side", "expected_limit"),
    (
        ("YES", 1_000, "ASK", 990),
        ("NO", 9_000, "BID", 9_010),
    ),
)
def test_canonical_mapping_and_cross_boundary_adverse_tick(
    first_side: str,
    price: int,
    expected_side: str,
    expected_limit: int,
) -> None:
    slices = [{"price_e4": price, "qty_fp": Decimal("2")}]
    spec, compact, outcomes = one_grid(
        first_side=first_side,
        slices=slices,
    )
    batch = validate([spec], compact, outcomes)
    state = batch.postfill_compact_causal_state[0]
    assert state["flatten_book_side"] == expected_side
    assert state["flatten_limit_price_e4"] == expected_limit
    assert state["flatten_adverse_tick_e4"] == 10


def test_incomplete_depth_uses_boundary_and_fok_zero_is_retained() -> None:
    slices = [{"price_e4": 3_100, "qty_fp": Decimal("1")}]
    spec, compact, outcomes = one_grid(
        slices=slices,
        outcome_kind="FOK_ZERO",
    )
    batch = validate([spec], compact, outcomes)
    state = batch.postfill_compact_causal_state[0]
    outcome = batch.postfill_compact_flatten_fok_outcome[0]
    assert state["flatten_limit_fallback"] is True
    assert state["flatten_limit_price_e4"] == C.LEGAL_PRICE_MIN_E4
    assert state["flatten_visible_residual_inventory_fp"] == Decimal("1")
    assert outcome["fok_fill_qty_fp"] == 0
    assert outcome["residual_inventory_fp"] == Decimal("2")
    assert outcome["reward_value_kind"] == "LOWER_BOUND"
    assert outcome["conservative_reward_usd"] == Decimal("-0.6")
    assert outcome["maker_fee_usd"] == Decimal("0")
    assert outcome["net_pnl_fee_low_usd"] == Decimal("0")
    assert outcome["capital_released"] is False


def test_unidentified_partial_inventory_cannot_survive_as_at_risk() -> None:
    spec = make_episode(terminal_elapsed_ms=100)
    pair = make_compact_pair(
        spec,
        0,
        slices=[{"price_e4": 3_100, "qty_fp": Decimal("1")}],
    )
    for row in pair:
        row.update(
            {
                "remaining_inventory_fp": Decimal("1"),
                "requested_qty_fp": Decimal("1"),
                "flatten_visible_residual_inventory_fp": Decimal("0"),
                "flatten_limit_price_e4": 3_000,
                "flatten_adverse_tick_e4": 100,
                "flatten_limit_fallback": False,
            }
        )
        if row["action_kind"] == "FLATTEN_FOK":
            row.update(
                {
                    "fok_limit_price_e4": 3_000,
                    "fok_limit_fallback": False,
                }
            )
    outcome = make_outcome(spec, pair[0], "FOK_FULL")
    outcome["capital_dollar_seconds"] = Decimal("0.03606")

    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="unidentified partial inventory",
    ):
        validate([spec], pair, [outcome])


def test_serialized_rows_carry_authoritative_cost_fee_spine() -> None:
    spec = make_episode()
    _paths, _expected, compact, outcomes = make_inputs([spec])
    batch = validate([spec], compact, outcomes)
    state = batch.postfill_compact_causal_state[0]
    outcome = batch.postfill_compact_flatten_fok_outcome[0]
    terminal = batch.postfill_compact_keep_transition[-1]

    assert state["first_fill_fee_usd"] == Decimal("0")
    assert state["first_leg_cost_basis_usd"] == Decimal("0.6")
    assert outcome["first_fill_price_e4"] == 3_000
    assert outcome["first_fill_qty_fp"] == Decimal("2")
    assert outcome["first_fill_fee_usd"] == Decimal("0")
    assert outcome["first_leg_cost_basis_usd"] == Decimal("0.6")
    assert terminal["first_fill_fee_usd"] == Decimal("0")
    assert terminal["first_leg_cost_basis_usd"] == Decimal("0.6")
    assert terminal["exit_fee_usd"] == Decimal("0")
    assert terminal["maker_fee_usd"] == (
        terminal["first_fill_fee_usd"] + terminal["exit_fee_usd"]
    )


def test_valid_missing_valid_touch_bundle_is_causal_and_not_stale() -> None:
    spec = make_episode(terminal_elapsed_ms=750)
    first = make_compact_pair(spec, 0)
    missing = make_compact_pair(
        spec,
        1,
        slices=[],
        two_sided_touch_available=False,
        complement_touch_available=False,
    )
    last = make_compact_pair(spec, 2)
    compact = [*first, *missing, *last]
    outcomes = [
        make_outcome(spec, first[0], "FOK_FULL"),
        make_outcome(spec, missing[0], "FOK_ZERO"),
        make_outcome(spec, last[0], "FOK_FULL"),
    ]
    batch = validate([spec], compact, outcomes)
    middle = batch.postfill_compact_causal_state[1]
    assert (
        batch.postfill_compact_causal_state[0][
            "kernel_fair_available"
        ]
        is False
    )
    assert middle["kernel_fair_available"] is False
    assert (
        batch.postfill_compact_causal_state[2][
            "kernel_fair_available"
        ]
        is False
    )
    assert middle["two_sided_touch_available"] is False
    assert middle["complement_touch_available"] is False
    assert middle["touch_imbalance"] is None
    assert middle["spread_e4"] is None
    assert middle["complement_touch_distance_e4"] is None
    assert middle["flatten_visible_executable_qty_fp"] == 0
    assert middle["flatten_visible_residual_inventory_fp"] == 2
    assert middle["flatten_limit_fallback"] is True

    stale = deepcopy(compact)
    for row in stale:
        if row["decision_index"] == 1:
            row["mid_move_1s_e4"] = 0
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="stale carry is forbidden",
    ):
        validate([spec], stale, outcomes)


def test_kernel_fair_bundle_valid_missing_stale_and_future_gates() -> None:
    spec = make_episode(
        terminal_elapsed_ms=100,
        first_price_e4=7_000,
    )
    valid = make_compact_pair(
        spec,
        0,
        kernel_fair_available=True,
        kernel_fair_e4=6_349,
    )
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="historical primary kernel fair must be missing",
    ):
        validate(
            [spec],
            valid,
            [make_outcome(spec, valid[0], "FOK_FULL")],
        )

    missing = make_compact_pair(spec, 0)
    missing_batch = validate(
        [spec],
        missing,
        [make_outcome(spec, missing[0], "FOK_ZERO")],
    )
    assert (
        missing_batch.postfill_compact_causal_state[0][
            "kernel_fair_available"
        ]
        is False
    )
    assert (
        missing_batch.postfill_compact_causal_state[0][
            "kernel_fair_e4"
        ]
        is None
    )

    stale = deepcopy(missing)
    for row in stale:
        row["kernel_fair_e4"] = 6_349
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="historical primary kernel fair bundle must be all-NULL",
    ):
        validate(
            [spec],
            stale,
            [make_outcome(spec, missing[0], "FOK_ZERO")],
        )

def test_legacy_two_route_field_is_rejected() -> None:
    spec, compact, outcomes = one_grid()
    compact[1]["ioc_route"] = "BUY_COMPLEMENT"
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="unsealed compact fields",
    ):
        validate([spec], compact, outcomes)


def test_l2_slices_must_be_unique_strict_price_levels() -> None:
    slices = [
        {"price_e4": 3_100, "qty_fp": Decimal("1")},
        {"price_e4": 3_100, "qty_fp": Decimal("1")},
    ]
    spec = make_episode(terminal_elapsed_ms=100)
    pair = make_compact_pair(spec, 0, slices=slices)
    outcome = make_outcome(spec, pair[0], "FOK_FULL")
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="strictly best-bid first",
    ):
        validate([spec], pair, [outcome])


def test_missing_fok_outcome_rolls_back_market_day() -> None:
    spec, compact, _outcomes = one_grid()
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="outcomes cannot be dropped",
    ):
        validate([spec], compact, [])


def test_unverified_state_roster_uses_specific_rollback_code() -> None:
    spec, compact, outcomes = one_grid()
    for row in compact:
        row["simulated_strategy_order_registry_complete"] = False
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match=r"\[SIMULATED_SELF_CROSS_UNIDENTIFIED\]",
    ):
        validate([spec], compact, outcomes)


def test_unverified_no_self_cross_before_send_rolls_back_day() -> None:
    spec, compact, outcomes = one_grid()
    outcomes[0]["simulated_no_self_cross_verified_before_fok"] = False
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match=r"\[SIMULATED_SELF_CROSS_UNIDENTIFIED\]",
    ):
        validate([spec], compact, outcomes)


def test_private_fill_provenance_is_rejected() -> None:
    spec, compact, outcomes = one_grid()
    spec["first_fill_provenance"] = "PRIVATE_EXACT"
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="unsupported first-fill provenance",
    ):
        validate([spec], compact, outcomes)


def test_schedule_zero_maker_is_limited_to_kxbtc15m() -> None:
    spec, compact, outcomes = one_grid()
    spec["market_ticker"] = "KXETH15M-ep"
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="sealed only for the KXBTC15M market series",
    ):
        validate([spec], compact, outcomes)


def test_keep_public_proxy_must_fill_at_original_complement_price() -> None:
    spec, compact, outcomes = one_grid()
    spec["terminal_complement_fill_price_e4"] = 6_800
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="public proxy complement terminal",
    ):
        validate([spec], compact, outcomes)


def test_observed_cancel_ack_claim_is_rejected() -> None:
    spec, compact, outcomes = one_grid()
    outcomes[0]["cancel_timing_provenance"] = "OBSERVED_CANCEL_ACK"
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="SYNTHETIC_60MS_SCENARIO",
    ):
        validate([spec], compact, outcomes)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("public_crossing_event_applied", True),
        ("synthetic_cancel_applied_before_terminal", False),
        ("synthetic_cancel_applied_sequence", 1),
        ("fok_processed_sequence", 0),
    ),
)
def test_fok_protocol_ordering_is_fail_closed(
    field: str,
    value: object,
) -> None:
    spec, compact, outcomes = one_grid()
    outcomes[0][field] = value
    with pytest.raises(C.MarketDayRollbackRequired):
        validate([spec], compact, outcomes)


def test_cancel_race_requires_full_public_proxy_before_synthetic_cancel() -> None:
    spec = make_episode(terminal_elapsed_ms=30)
    pair = make_compact_pair(spec, 0)
    race = make_outcome(spec, pair[0], "CANCEL_RACE_PAIR")
    batch = validate([spec], pair, [race])
    outcome = batch.postfill_compact_flatten_fok_outcome[0]
    assert outcome["terminal_type"] == "CANCEL_RACE_PAIR"
    assert outcome["synthetic_cancel_applied_before_terminal"] is False
    assert outcome["fok_sent"] is False
    assert (
        outcome["simulated_no_self_cross_verified_before_fok"] is None
    )
    assert outcome["residual_inventory_fp"] == 0
    assert outcome["simulation_reward_exact"] is True
    assert outcome["reward_value_kind"] == "SIMULATION_EXACT"
    assert outcome["race_fee_provenance"] == C.MAKER_FEE_PROVENANCE
    assert outcome["maker_fee_usd"] == Decimal("0")
    assert outcome["net_pnl_fee_low_usd"] == Decimal("0.02")

    partial = deepcopy(race)
    partial["complement_fill_qty_fp"] = Decimal("1")
    partial["residual_inventory_fp"] = Decimal("1")
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="partial public proxy complement fill",
    ):
        validate([spec], pair, [partial])


def test_effective_timestamp_is_not_cancel_race() -> None:
    spec = make_episode(terminal_elapsed_ms=60)
    pair = make_compact_pair(spec, 0)
    invented_race = make_outcome(
        spec,
        pair[0],
        "CANCEL_RACE_PAIR",
    )
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="pre-synthetic-cancel",
    ):
        validate([spec], pair, [invented_race])

    zero = make_outcome(spec, pair[0], "FOK_ZERO")
    batch = validate([spec], pair, [zero])
    assert (
        batch.postfill_compact_flatten_fok_outcome[0][
            "terminal_type"
        ]
        == "FOK_ZERO"
    )


def test_public_proxy_pre_cancel_pair_cannot_be_hidden_by_fok_outcome() -> None:
    spec = make_episode(terminal_elapsed_ms=30)
    pair = make_compact_pair(spec, 0)
    hidden = make_outcome(spec, pair[0], "FOK_ZERO")
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="must terminate as CANCEL_RACE_PAIR",
    ):
        validate([spec], pair, [hidden])


def test_partial_fok_full_is_rejected() -> None:
    spec, compact, outcomes = one_grid()
    outcome = outcomes[0]
    outcome["fok_fill_slices"] = [
        {"price_e4": 3_100, "qty_fp": Decimal("1")}
    ]
    outcome["fok_fill_qty_fp"] = Decimal("1")
    outcome["residual_inventory_fp"] = Decimal("1")
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="not a complete atomic fill",
    ):
        validate([spec], compact, outcomes)


def test_zero_time_atom_has_no_state_action_transition_or_outcome() -> None:
    spec = make_episode(
        episode_id="atom",
        terminal_elapsed_ms=0,
        zero_time_atom=True,
    )
    batch = validate([spec], [], [])
    assert len(batch.postfill_zero_time_atom) == 1
    assert not batch.postfill_compact_causal_state
    assert not batch.postfill_compact_causal_action
    assert not batch.postfill_compact_keep_transition
    assert not batch.postfill_compact_flatten_fok_outcome


def test_hard_fallback_requires_market_close_and_zero_exit_fee() -> None:
    early = make_episode(
        terminal_elapsed_ms=100,
        terminal_kind="HARD_FALLBACK",
    )
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="HARD_FALLBACK must occur at market close",
    ):
        validate([early], [], [])

    charged = make_episode(
        terminal_elapsed_ms=Decimal("120000.5"),
        terminal_kind="HARD_FALLBACK",
    )
    charged["terminal_keep_exit_fee_usd"] = Decimal("0.01")
    charged["terminal_keep_maker_fee_usd"] = Decimal("0.01")
    charged["terminal_keep_net_pnl_usd"] = Decimal("-0.61")
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="settlement or zero-fee schedule",
    ):
        validate([charged], [], [])


def test_hard_fallback_market_close_settlement_is_simulation_exact() -> None:
    spec = make_episode(
        terminal_elapsed_ms=Decimal("120000.5"),
        terminal_kind="HARD_FALLBACK",
    )
    _paths, _expected, compact, outcomes = make_inputs(
        [spec],
        outcome_kind="FOK_ZERO",
    )
    batch = validate([spec], compact, outcomes)
    terminal = batch.postfill_compact_keep_transition[-1]
    assert terminal["transition_type"] == "KEEP_TO_TERMINAL"
    assert terminal["keep_terminal_type"] == "HARD_FALLBACK"
    assert terminal["interval_stop_elapsed_ms"] == Decimal("120000.5")
    assert terminal["official_market_result"] == "NO"
    assert (
        terminal["terminal_execution_provenance"]
        == C.SETTLEMENT_PROVENANCE
    )
    assert terminal["exit_fee_usd"] == 0
    assert terminal["maker_fee_usd"] == 0
    assert terminal["immediate_net_pnl_usd"] == Decimal("-0.6")
    assert terminal["simulation_reward_exact"] is True


def test_current_policy_comparator_cannot_be_keep_terminal() -> None:
    spec = make_episode(terminal_elapsed_ms=100)
    spec["terminal_type"] = "CURRENT_DIST2_TTL60"
    pair = make_compact_pair(spec, 0)
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="CURRENT_DIST2_TTL60 is comparator-only",
    ):
        validate(
            [spec],
            pair,
            [make_outcome(spec, pair[0], "FOK_ZERO")],
        )


def test_admin_censor_cannot_be_keep_terminal() -> None:
    spec = make_episode(terminal_elapsed_ms=100)
    spec["terminal_type"] = "ADMIN_CENSOR"
    pair = make_compact_pair(spec, 0)
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="unknown continuous terminal_type",
    ):
        validate(
            [spec],
            pair,
            [make_outcome(spec, pair[0], "FOK_ZERO")],
        )


def test_final_60s_keep_continues_only_to_public_proxy_terminal() -> None:
    spec = make_episode(terminal_elapsed_ms=70_000)
    _paths, _expected, compact, outcomes = make_inputs(
        [spec],
        outcome_kind="FOK_ZERO",
    )
    batch = validate([spec], compact, outcomes)
    assert len(batch.postfill_compact_causal_state) == 9
    assert len(batch.postfill_compact_keep_transition) == 9
    assert {
        row["from_decision_index"]
        for row in batch.postfill_compact_keep_transition
    } == set(range(9))
    final = next(
        row
        for row in batch.postfill_compact_keep_transition
        if row["from_decision_index"] == 8
    )
    assert final["transition_type"] == "KEEP_TO_TERMINAL"
    assert final["interval_start_elapsed_ms"] == Decimal("60000")
    assert final["interval_stop_elapsed_ms"] == Decimal("70000")


def test_floats_are_forbidden_in_exact_state_fields() -> None:
    spec, compact, outcomes = one_grid()
    compact[0]["remaining_inventory_fp"] = 2.0
    compact[1]["remaining_inventory_fp"] = 2.0
    with pytest.raises(
        C.MarketDayRollbackRequired,
        match="floats and booleans are forbidden",
    ):
        validate([spec], compact, outcomes)


def test_path_preflight_rejects_any_forbidden_date_token() -> None:
    with pytest.raises(ForbiddenSourceError):
        C.preflight_canary_postfill_paths(
            {
                DAY: [
                    f"/sealed/{DAY}/copied-from-2026-07-23.jsonl"
                ]
            },
            {DAY: ["ep"]},
        )


@pytest.mark.parametrize(
    ("terminal_ms", "kind"),
    (
        (100, "FOK_FULL"),
        (100, "FOK_ZERO"),
        (30, "CANCEL_RACE_PAIR"),
    ),
)
def test_v4_rows_insert_into_duckdb_ddl(
    terminal_ms: int,
    kind: str,
) -> None:
    spec = make_episode(terminal_elapsed_ms=terminal_ms)
    pair = make_compact_pair(spec, 0)
    batch = validate(
        [spec],
        pair,
        [make_outcome(spec, pair[0], kind)],
    )
    connection = duckdb.connect(":memory:")
    connection.execute(SUPPLEMENTAL_DDL.read_text())
    for table in (
        "postfill_compact_causal_state",
        "postfill_compact_causal_action",
        "postfill_compact_keep_transition",
        "postfill_compact_flatten_fok_outcome",
    ):
        insert_rows(connection, table, batch.as_ddl_rows()[table])
    assert connection.execute(
        "SELECT count(*) FROM postfill_compact_flatten_fok_outcome"
    ).fetchone() == (1,)


def test_duckdb_ddl_independently_rejects_missingness_and_zero_drift() -> None:
    spec, compact, outcomes = one_grid(outcome_kind="FOK_ZERO")
    batch = validate([spec], compact, outcomes)
    connection = duckdb.connect(":memory:")
    connection.execute(SUPPLEMENTAL_DDL.read_text())

    partial_inventory = deepcopy(
        batch.postfill_compact_causal_state[0]
    )
    partial_inventory["remaining_inventory_fp"] = Decimal("1")
    partial_inventory["flatten_visible_executable_qty_fp"] = Decimal("1")
    partial_inventory["flatten_visible_residual_inventory_fp"] = Decimal("0")
    with pytest.raises(duckdb.ConstraintException):
        insert_rows(
            connection,
            "postfill_compact_causal_state",
            (partial_inventory,),
        )

    stale_touch = deepcopy(batch.postfill_compact_causal_state[0])
    stale_touch["two_sided_touch_available"] = False
    stale_touch["touch_imbalance"] = None
    stale_touch["spread_e4"] = 200
    stale_touch["mid_move_1s_e4"] = None
    stale_touch["mid_move_10s_e4"] = None
    stale_touch["mid_move_since_entry_e4"] = None
    stale_touch["mid_move_since_fill_e4"] = None
    with pytest.raises(duckdb.ConstraintException):
        insert_rows(
            connection,
            "postfill_compact_causal_state",
            (stale_touch,),
        )

    forbidden_kernel = deepcopy(
        batch.postfill_compact_causal_state[0]
    )
    forbidden_kernel.update(
        {
            "kernel_fair_available": True,
            "kernel_fair_e4": 6_349,
            "first_leg_fair_edge_e4": 3_349,
            "complement_fair_edge_e4": -3_249,
            "kernel_fair_move_since_entry_e4": 0,
            "kernel_fair_move_since_fill_e4": 0,
            "kernel_source_time_ms": (
                int(forbidden_kernel["decision_recv_wall_ns"])
                // 1_000_000
                - 5_000
            ),
            "kernel_causality_kind": "NON_CAUSAL_SENSITIVITY",
        }
    )
    with pytest.raises(duckdb.ConstraintException):
        insert_rows(
            connection,
            "postfill_compact_causal_state",
            (forbidden_kernel,),
        )

    zero_drift = deepcopy(
        batch.postfill_compact_flatten_fok_outcome[0]
    )
    zero_drift["fok_fill_qty_fp"] = Decimal("1")
    zero_drift["residual_inventory_fp"] = Decimal("1")
    with pytest.raises(duckdb.ConstraintException):
        insert_rows(
            connection,
            "postfill_compact_flatten_fok_outcome",
            (zero_drift,),
        )

    forged_zero_fee = deepcopy(
        batch.postfill_compact_flatten_fok_outcome[0]
    )
    forged_zero_fee["maker_fee_usd"] = Decimal("0.01")
    forged_zero_fee["net_pnl_fee_low_usd"] = Decimal("-0.01")
    forged_zero_fee["net_pnl_fee_base_usd"] = Decimal("-0.01")
    forged_zero_fee["net_pnl_fee_high_usd"] = Decimal("-0.01")
    with pytest.raises(duckdb.ConstraintException):
        insert_rows(
            connection,
            "postfill_compact_flatten_fok_outcome",
            (forged_zero_fee,),
        )

    forged_zero_reward = deepcopy(
        batch.postfill_compact_flatten_fok_outcome[0]
    )
    forged_zero_reward["conservative_reward_usd"] = Decimal("0")
    with pytest.raises(duckdb.ConstraintException):
        insert_rows(
            connection,
            "postfill_compact_flatten_fok_outcome",
            (forged_zero_reward,),
        )

    full_spec, full_pair, full_outcomes = one_grid(
        outcome_kind="FOK_FULL"
    )
    full_batch = validate([full_spec], full_pair, full_outcomes)
    forged_full_fee = deepcopy(
        full_batch.postfill_compact_flatten_fok_outcome[0]
    )
    forged_full_fee["maker_fee_usd"] = Decimal("0.01")
    forged_full_fee["net_pnl_fee_low_usd"] = (
        forged_full_fee["gross_pnl_usd"]
        - forged_full_fee["maker_fee_usd"]
        - forged_full_fee["taker_fee_low_usd"]
    )
    forged_full_fee["net_pnl_fee_base_usd"] = (
        forged_full_fee["gross_pnl_usd"]
        - forged_full_fee["maker_fee_usd"]
        - forged_full_fee["taker_fee_base_usd"]
    )
    forged_full_fee["net_pnl_fee_high_usd"] = (
        forged_full_fee["gross_pnl_usd"]
        - forged_full_fee["maker_fee_usd"]
        - forged_full_fee["taker_fee_high_usd"]
    )
    forged_full_fee["conservative_reward_usd"] = (
        forged_full_fee["net_pnl_fee_high_usd"]
    )
    with pytest.raises(duckdb.ConstraintException):
        insert_rows(
            connection,
            "postfill_compact_flatten_fok_outcome",
            (forged_full_fee,),
        )

    race_spec = make_episode(terminal_elapsed_ms=30)
    race_pair = make_compact_pair(race_spec, 0)
    race_batch = validate(
        [race_spec],
        race_pair,
        [make_outcome(race_spec, race_pair[0], "CANCEL_RACE_PAIR")],
    )
    forged_race_fee = deepcopy(
        race_batch.postfill_compact_flatten_fok_outcome[0]
    )
    forged_race_fee["maker_fee_usd"] = Decimal("0.01")
    forged_race_fee["net_pnl_fee_low_usd"] = (
        forged_race_fee["gross_pnl_usd"]
        - forged_race_fee["maker_fee_usd"]
    )
    forged_race_fee["net_pnl_fee_base_usd"] = (
        forged_race_fee["gross_pnl_usd"]
        - forged_race_fee["maker_fee_usd"]
    )
    forged_race_fee["net_pnl_fee_high_usd"] = (
        forged_race_fee["gross_pnl_usd"]
        - forged_race_fee["maker_fee_usd"]
    )
    forged_race_fee["conservative_reward_usd"] = (
        forged_race_fee["gross_pnl_usd"]
        - forged_race_fee["maker_fee_usd"]
    )
    with pytest.raises(duckdb.ConstraintException):
        insert_rows(
            connection,
            "postfill_compact_flatten_fok_outcome",
            (forged_race_fee,),
        )

    long_spec = make_episode()
    _paths, _expected, long_compact, long_outcomes = make_inputs(
        [long_spec]
    )
    long_batch = validate([long_spec], long_compact, long_outcomes)
    forged_keep_fee = deepcopy(
        long_batch.postfill_compact_keep_transition[-1]
    )
    forged_keep_fee["exit_fee_usd"] = Decimal("0.01")
    forged_keep_fee["maker_fee_usd"] = Decimal("0.01")
    forged_keep_fee["immediate_net_pnl_usd"] = (
        forged_keep_fee["immediate_gross_pnl_usd"]
        - forged_keep_fee["maker_fee_usd"]
    )
    with pytest.raises(duckdb.ConstraintException):
        insert_rows(
            connection,
            "postfill_compact_keep_transition",
            (forged_keep_fee,),
        )


def test_v4_hard_fallback_and_zero_time_atom_insert_into_ddl() -> None:
    hard_spec = make_episode(
        terminal_elapsed_ms=Decimal("120000.5"),
        terminal_kind="HARD_FALLBACK",
    )
    _paths, _expected, compact, outcomes = make_inputs([hard_spec])
    hard_batch = validate([hard_spec], compact, outcomes)
    supplemental = duckdb.connect(":memory:")
    supplemental.execute(SUPPLEMENTAL_DDL.read_text())
    for table in (
        "postfill_compact_causal_state",
        "postfill_compact_causal_action",
        "postfill_compact_keep_transition",
        "postfill_compact_flatten_fok_outcome",
    ):
        insert_rows(
            supplemental,
            table,
            hard_batch.as_ddl_rows()[table],
        )
    hard_terminal = hard_batch.postfill_compact_keep_transition[-1]
    assert hard_terminal["keep_terminal_type"] == "HARD_FALLBACK"
    assert (
        hard_terminal["interval_stop_elapsed_ms"]
        == hard_terminal["market_close_elapsed_ms"]
    )
    assert hard_terminal["exit_fee_usd"] == Decimal("0")

    atom_spec = make_episode(
        terminal_elapsed_ms=0,
        zero_time_atom=True,
    )
    atom_batch = validate([atom_spec], [], [])
    base = duckdb.connect(":memory:")
    base.execute(BASE_DDL.read_text())
    insert_rows(
        base,
        "postfill_zero_time_atom",
        atom_batch.postfill_zero_time_atom,
    )
    assert base.execute(
        "SELECT market_ticker, data_origin, complement_fill_fee_usd "
        "FROM postfill_zero_time_atom"
    ).fetchone() == (
        "KXBTC15M-ep",
        C.DATA_ORIGIN,
        Decimal("0"),
    )


def test_v4_ddl_rejects_scientific_provenance_and_terminal_forgeries() -> None:
    spec, compact, outcomes = one_grid(outcome_kind="FOK_ZERO")
    batch = validate([spec], compact, outcomes)
    state = batch.postfill_compact_causal_state[0]
    outcome = batch.postfill_compact_flatten_fok_outcome[0]
    keep_terminal = batch.postfill_compact_keep_transition[-1]

    private_fill = deepcopy(state)
    private_fill["first_fill_provenance"] = "PRIVATE_EXACT"

    wrong_market_series = deepcopy(state)
    wrong_market_series["market_ticker"] = "KXETH15M-ep"

    historical_kernel = deepcopy(state)
    historical_kernel["kernel_fair_available"] = True

    observed_cancel = deepcopy(outcome)
    observed_cancel["cancel_timing_provenance"] = "OBSERVED_CANCEL_ACK"

    account_wide_self_cross = deepcopy(outcome)
    account_wide_self_cross["self_cross_scope"] = "REAL_ACCOUNT_ALL_ORDERS"

    nonzero_maker_fee = deepcopy(outcome)
    nonzero_maker_fee["maker_fee_usd"] = Decimal("0.01")
    for field in (
        "net_pnl_fee_low_usd",
        "net_pnl_fee_base_usd",
        "net_pnl_fee_high_usd",
    ):
        nonzero_maker_fee[field] = Decimal("-0.01")

    repriced_keep = deepcopy(keep_terminal)
    repriced_keep["terminal_complement_price_e4"] = 6_800

    admin_censor_terminal = deepcopy(keep_terminal)
    admin_censor_terminal["keep_terminal_type"] = "ADMIN_CENSOR"

    hard_spec = make_episode(
        terminal_elapsed_ms=Decimal("120000.5"),
        terminal_kind="HARD_FALLBACK",
    )
    _paths, _expected, hard_compact, hard_outcomes = make_inputs(
        [hard_spec]
    )
    hard_batch = validate([hard_spec], hard_compact, hard_outcomes)
    hard_terminal = hard_batch.postfill_compact_keep_transition[-1]

    hard_before_close = deepcopy(hard_terminal)
    hard_before_close["interval_stop_elapsed_ms"] = Decimal("120000")
    hard_before_close["capital_dollar_seconds_increment"] = Decimal("36")

    hard_nonzero_fee = deepcopy(hard_terminal)
    hard_nonzero_fee["exit_fee_usd"] = Decimal("0.01")

    current_comparator_terminal = deepcopy(hard_terminal)
    current_comparator_terminal["keep_terminal_type"] = (
        "CURRENT_DIST2_TTL60"
    )

    cases = (
        (
            "postfill_compact_causal_state",
            private_fill,
        ),
        (
            "postfill_compact_causal_state",
            wrong_market_series,
        ),
        (
            "postfill_compact_causal_state",
            historical_kernel,
        ),
        (
            "postfill_compact_flatten_fok_outcome",
            observed_cancel,
        ),
        (
            "postfill_compact_flatten_fok_outcome",
            account_wide_self_cross,
        ),
        (
            "postfill_compact_flatten_fok_outcome",
            nonzero_maker_fee,
        ),
        (
            "postfill_compact_keep_transition",
            repriced_keep,
        ),
        (
            "postfill_compact_keep_transition",
            admin_censor_terminal,
        ),
        (
            "postfill_compact_keep_transition",
            hard_before_close,
        ),
        (
            "postfill_compact_keep_transition",
            hard_nonzero_fee,
        ),
        (
            "postfill_compact_keep_transition",
            current_comparator_terminal,
        ),
    )
    for table, forged in cases:
        connection = duckdb.connect(":memory:")
        connection.execute(SUPPLEMENTAL_DDL.read_text())
        with pytest.raises(duckdb.ConstraintException):
            insert_rows(connection, table, (forged,))
