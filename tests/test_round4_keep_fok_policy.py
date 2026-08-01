from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from tools.research.crypto_mm import round4_keep_fok_policy as P
from tools.research.crypto_mm import round4_postfill_state_contract as C
from tools.research.crypto_mm.round4_table_builder import (
    DISCOVERY_DATES,
    EXPERIMENT_ID,
    ForbiddenSourceError,
)


DAYS = tuple(sorted(DISCOVERY_DATES))
BASE_WALL_NS = 1_800_000_000_000_000_000
BASE_MONO_NS = 8_000_000_000_000_000
DAY_NS = 100_000_000_000_000
DURATIONS_MS = (530, 3_000, 11_000, 61_000)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def make_episode(
    day: str,
    day_index: int,
    episode_index: int,
    *,
    atom: bool = False,
    overlap: bool = False,
) -> dict[str, object]:
    suffix = (
        "atom"
        if atom
        else f"overlap-{episode_index}"
        if overlap
        else str(episode_index)
    )
    episode_id = f"learn-{day}-{suffix}"
    hard_fallback = not atom and episode_index == 3 and not overlap
    terminal_ms = (
        Decimal("0")
        if atom
        else Decimal("120000.5")
        if hard_fallback
        else Decimal("60000")
        if episode_index == 4
        else Decimal(DURATIONS_MS[episode_index % 4])
    )
    first_side = (
        "YES" if (day_index + episode_index) % 2 == 0 else "NO"
    )
    first_price = 3_000 if first_side == "YES" else 7_000
    wall = (
        BASE_WALL_NS
        + day_index * DAY_NS
        + (episode_index + 10 * int(overlap) + 20 * int(atom))
        * 1_000_000_000
    )
    mono = (
        BASE_MONO_NS
        + day_index * DAY_NS
        + (episode_index + 10 * int(overlap) + 20 * int(atom))
        * 1_000_000_000
    )
    row: dict[str, object] = {
        "experiment_id": EXPERIMENT_ID,
        "data_role": "DISCOVERY",
        "data_origin": C.DATA_ORIGIN,
        "source_date_utc": day,
        "market_ticker": f"KXBTC15M-{episode_id}",
        "postfill_episode_id": episode_id,
        "entry_episode_id": f"entry-{episode_id}",
        "entry_action_id": "ENTRY_PAIR_99",
        "source_rows_sha256": digest(f"episode-{episode_id}"),
        "first_fill_provenance": C.FILL_PROVENANCE,
        "first_fill_execution_nature": C.EXECUTION_NATURE,
        "first_fill_fee_provenance": C.MAKER_FEE_PROVENANCE,
        "first_fill_side": first_side,
        "first_fill_price_e4": first_price,
        "first_fill_qty_fp": Decimal("2"),
        "first_fill_fee_usd": Decimal("0"),
        "first_fill_recv_wall_ns": wall,
        "first_fill_recv_mono_ns": mono,
        "first_fill_elapsed_ms": Decimal("125"),
        "first_fill_tte_ms": 120_000,
        "market_close_elapsed_ms": Decimal("120000.5"),
        "complement_order_id": f"order-{episode_id}",
        "complement_price_e4": (
            6_900 if first_side == "YES" else 3_100
        ),
        "complement_order_age_at_first_fill_ms": 100,
        "terminal_elapsed_ms": terminal_ms,
        "terminal_wall_ns": wall + int(terminal_ms * 1_000_000),
        "terminal_mono_ns": mono + int(terminal_ms * 1_000_000),
        "terminal_type": (
            C.ZERO_TIME_TERMINAL
            if atom
            else "HARD_FALLBACK"
            if hard_fallback
            else "COMPLEMENT_FILL"
        ),
        "zero_time_atom": atom,
        "market_day_gate_pass": True,
        "reconciliation_ok": True,
        "data_invalid": False,
    }
    complement_side = "NO" if first_side == "YES" else "YES"
    complement_price = 6_900 if first_side == "YES" else 3_100
    if atom:
        row.update(
            {
                "receipt_envelope_id": f"envelope-{episode_id}",
                "first_fill_stable_source_id": f"{episode_id}-001",
                "complement_fill_stable_source_id": (
                    f"{episode_id}-002"
                ),
                "complement_side": complement_side,
                "complement_fill_price_e4": complement_price,
                "complement_fill_qty_fp": Decimal("2"),
                "complement_fill_fee_usd": Decimal("0"),
                "complement_fill_provenance": C.FILL_PROVENANCE,
                "complement_execution_nature": C.EXECUTION_NATURE,
                "complement_fee_provenance": C.MAKER_FEE_PROVENANCE,
            }
        )
    elif not hard_fallback:
        row.update(
            {
                "terminal_complement_fill_price_e4": (
                    complement_price
                ),
                "terminal_complement_fill_qty_fp": Decimal("2"),
                "terminal_complement_fill_fee_usd": Decimal("0"),
                "terminal_complement_fill_provenance": (
                    C.FILL_PROVENANCE
                ),
                "terminal_complement_execution_nature": (
                    C.EXECUTION_NATURE
                ),
                "terminal_complement_fee_provenance": (
                    C.MAKER_FEE_PROVENANCE
                ),
                "terminal_complement_fill_stable_source_id": (
                    f"{episode_id}-terminal-fill"
                ),
            }
        )
    else:
        official_result = "NO" if first_side == "YES" else "YES"
        gross = (
            -Decimal(first_price) / Decimal("10000")
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
    day_index: int,
    episode_index: int,
    decision_index: int,
) -> list[dict[str, object]]:
    elapsed = C.DECISION_GRID_MS[decision_index]
    wall = int(spec["first_fill_recv_wall_ns"]) + elapsed * 1_000_000
    mono = int(spec["first_fill_recv_mono_ns"]) + elapsed * 1_000_000
    slices = deepcopy(default_slices(str(spec["first_fill_side"])))
    normalized = tuple(
        (int(item["price_e4"]), Decimal(item["qty_fp"]))
        for item in slices
    )
    economics = C._slice_economics(
        normalized,
        first_fill_side=str(spec["first_fill_side"]),
        first_price_e4=int(spec["first_fill_price_e4"]),
    )
    requested = Decimal(spec["first_fill_qty_fp"])
    executable = sum(
        (qty for _price, qty in normalized), Decimal("0")
    )
    residual = requested - executable
    book_side = C._canonical_book_side(str(spec["first_fill_side"]))
    worst = normalized[-1][0] if residual == 0 else None
    limit, tick, fallback = C._adverse_limit(book_side, worst)
    # Exercise valid -> missing -> valid public-touch rows without
    # dropping them. Historical primary kernel fields are always NULL in V4.
    missing = episode_index == 2 and decision_index % 2 == 1
    complement_price = (
        6_900 if spec["first_fill_side"] == "YES" else 3_100
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
        "decision_recv_wall_ns": wall,
        "decision_recv_mono_ns": mono,
        "feature_asof_wall_ns": wall,
        "source_max_recv_wall_ns": wall,
        "source_max_recv_mono_ns": mono,
        "first_fill_price_e4": spec["first_fill_price_e4"],
        "complement_price_e4": complement_price,
        "complement_order_age_ms": (
            int(spec["complement_order_age_at_first_fill_ms"]) + elapsed
        ),
        "complement_touch_distance_e4": None if missing else 100,
        "spread_e4": None if missing else 200,
        "mid_move_1s_e4": (
            None if missing else episode_index - day_index
        ),
        "mid_move_10s_e4": (
            None if missing else 2 * episode_index - day_index
        ),
        "mid_move_since_entry_e4": (
            None if missing else 3 * episode_index
        ),
        "mid_move_since_fill_e4": (
            None if missing else decision_index - episode_index
        ),
        "flatten_limit_price_e4": limit,
        "flatten_adverse_tick_e4": tick,
        "tte_ms": int(spec["first_fill_tte_ms"]) - elapsed,
        "first_fill_side": spec["first_fill_side"],
        "first_fill_qty_fp": requested,
        "remaining_inventory_fp": requested,
        "complement_queue_position_fp": Decimal(
            3 + episode_index + decision_index
        ),
        "complement_same_price_ahead_fp": Decimal(
            2 + episode_index
        ),
        "complement_better_depth_fp": Decimal(1 + day_index),
        "complement_flow_1s_fp": Decimal(2 + episode_index),
        "complement_flow_5s_fp": Decimal(5 + episode_index),
        "complement_flow_10s_fp": Decimal(12 + episode_index),
        "complement_flow_60s_fp": Decimal(60 + 6 * episode_index),
        "complement_flow_acceleration_fp": Decimal(2),
        "touch_imbalance": (
            None
            if missing
            else Decimal(episode_index - 1) / Decimal("10")
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
        "flatten_visible_slices": slices,
        "public_book_data_origin": C.DATA_ORIGIN,
        "flatten_counterfactual_provenance": (
            C.FOK_EXECUTION_PROVENANCE
        ),
        "two_sided_touch_available": not missing,
        "complement_touch_available": not missing,
        "kernel_fair_available": False,
        "kernel_fair_e4": None,
        "first_leg_fair_edge_e4": None,
        "complement_fair_edge_e4": None,
        "kernel_fair_move_since_entry_e4": None,
        "kernel_fair_move_since_fill_e4": None,
        "kernel_source_time_ms": None,
        "kernel_causality_kind": None,
        "simulated_strategy_order_registry_complete": True,
        "simulated_strategy_order_registry_sha256": digest(
            f"{spec['postfill_episode_id']}-{decision_index}-roster"
        ),
        "cancel_state": "NONE",
        "market_day_gate_pass": True,
        "reconciliation_ok": True,
        "data_invalid": False,
        "requested_qty_fp": requested,
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
            "time_in_force": C.FOK_TIME_IN_FORCE,
            "self_trade_prevention_type": "taker_at_cross",
            "fok_requires_prior_synthetic_cancel_applied": True,
            "fok_book_side": book_side,
            "fok_limit_price_e4": limit,
            "fok_limit_fallback": fallback,
        }
    )
    return [keep, flatten]


def make_fok_outcome(
    spec: dict[str, object],
    state: dict[str, object],
    terminal_type: str,
) -> dict[str, object]:
    elapsed = int(state["decision_elapsed_ms"])
    planned = elapsed + C.FOK_EFFECTIVE_LATENCY_MS
    requested = Decimal(state["remaining_inventory_fp"])
    first_fee = Decimal(spec["first_fill_fee_usd"])
    first_cost = (
        Decimal(spec["first_fill_price_e4"])
        / Decimal("10000")
        * requested
    )
    basis = first_cost
    race = terminal_type == "CANCEL_RACE_PAIR"
    terminal = (
        Decimal(spec["terminal_elapsed_ms"])
        if race
        else Decimal(planned)
    )
    terminal_ns = int(terminal * Decimal("1000000"))
    terminal_wall = int(spec["first_fill_recv_wall_ns"]) + terminal_ns
    terminal_mono = int(spec["first_fill_recv_mono_ns"]) + terminal_ns
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
        maker_fee = Decimal("0")
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
        complement_qty = fok_qty = Decimal("0")
        residual = requested
        gross = Decimal("0")
        maker_fee = Decimal("0")
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
        fok_qty = residual = Decimal("0")
        race_price = int(spec["terminal_complement_fill_price_e4"])
        gross = (
            Decimal(10_000)
            - Decimal(spec["first_fill_price_e4"])
            - Decimal(race_price)
        ) / Decimal("10000") * requested
        maker_fee = Decimal("0")
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
            None if race else f"{spec['postfill_episode_id']}-fok"
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


def external_outcome(
    spec: dict[str, object],
    *,
    overlap: bool,
) -> dict[str, object]:
    zero = bool(spec["zero_time_atom"])
    first_cost = (
        Decimal(spec["first_fill_price_e4"])
        / Decimal("10000")
        * Decimal(spec["first_fill_qty_fp"])
    )
    first_fee = Decimal(spec["first_fill_fee_usd"])
    basis = first_cost + first_fee
    if zero:
        current_elapsed = Decimal("0")
        complement_price = Decimal(spec["complement_fill_price_e4"])
        gross = (
            Decimal(10_000)
            - Decimal(spec["first_fill_price_e4"])
            - complement_price
        ) / Decimal("10000") * Decimal(spec["first_fill_qty_fp"])
        current_low = current_high = (
            gross
            - first_fee
            - Decimal(spec["complement_fill_fee_usd"])
        )
        current_type = current_route = "ZERO_TIME_ATOM"
        current_capital = Decimal("0")
    else:
        # Deliberately non-grid: the comparator must consume this exact
        # policy-row receipt and never snap to 1s or another learner grid.
        current_elapsed = Decimal("1375")
        current_high = Decimal("-0.020")
        current_low = Decimal("-0.015")
        current_type = "PAIR_COMPLETE" if "0" in str(
            spec["postfill_episode_id"]
        ) else "INVENTORY_EXIT"
        current_route = (
            "MAKER_COMPLEMENT"
            if current_type == "PAIR_COMPLETE"
            else "HARD_FALLBACK"
        )
        current_capital = (
            basis * current_elapsed / Decimal("1000")
        )
    elapsed_ns = int(current_elapsed * Decimal("1000000"))
    return {
        "data_origin": "SYNTHETIC",
        "source_date_utc": spec["source_date_utc"],
        "market_cluster_id": f"cluster-{spec['postfill_episode_id']}",
        "postfill_episode_id": spec["postfill_episode_id"],
        "first_fill_side": spec["first_fill_side"],
        "terminal_elapsed_ms": spec["terminal_elapsed_ms"],
        "terminal_type": spec["terminal_type"],
        "zero_time_atom": zero,
        "first_leg_cost_basis_usd": first_cost,
        "first_fill_fee_usd": first_fee,
        "capital_locked_usd": basis,
        "market_close_elapsed_ms": spec["market_close_elapsed_ms"],
        "pre_first_fill_capital_dollar_seconds": Decimal("0.05"),
        "historical_current_outcome": {
            "policy_id": "CURRENT_DIST2_TTL60",
            "comparator_provenance": "SYNTHETIC",
            "policy_row_id": (
                f"current-{spec['postfill_episode_id']}"
            ),
            "policy_definition_sha256": digest(
                "CURRENT_DIST2_TTL60-definition"
            ),
            "terminal_type": current_type,
            "terminal_route": current_route,
            "terminal_elapsed_ms": current_elapsed,
            "terminal_recv_wall_ns": (
                int(spec["first_fill_recv_wall_ns"]) + elapsed_ns
            ),
            "terminal_recv_mono_ns": (
                int(spec["first_fill_recv_mono_ns"]) + elapsed_ns
            ),
            "realized_pnl_fee_low_usd": current_low,
            "realized_pnl_fee_high_usd": current_high,
            "postfill_capital_dollar_seconds": current_capital,
            "residual_inventory_fp": Decimal("0"),
            "source_rows_sha256": digest(
                f"current-{spec['postfill_episode_id']}-receipt"
            ),
            "reconciliation_ok": True,
        },
        "overlapping_episode": overlap,
        "reconciliation_ok": True,
        "data_invalid": False,
    }


def synthetic_dataset() -> P.SyntheticKeepFokDataset:
    manifests: list[dict[str, object]] = []
    compact: list[dict[str, object]] = []
    fok_outcomes: list[dict[str, object]] = []
    external: list[dict[str, object]] = []
    paths: dict[str, tuple[str, ...]] = {}
    expected: dict[str, tuple[str, ...]] = {}
    for day_index, day in enumerate(DAYS):
        ids = []
        specs: list[tuple[dict[str, object], int, bool]] = []
        for episode_index in range(5):
            spec = make_episode(day, day_index, episode_index)
            specs.append((spec, episode_index, False))
        atom = make_episode(day, day_index, 0, atom=True)
        specs.append((atom, 0, False))
        overlap = make_episode(
            day, day_index, 1, overlap=True
        )
        specs.append((overlap, 1, True))
        for spec, episode_index, is_overlap in specs:
            manifests.append(spec)
            ids.append(str(spec["postfill_episode_id"]))
            external.append(
                external_outcome(spec, overlap=is_overlap)
            )
            if spec["zero_time_atom"]:
                continue
            desired = (
                "FOK_ZERO"
                if (episode_index + day_index) % 3 == 1
                else "FOK_FULL"
            )
            for index, _elapsed in C.required_decision_grid(
                spec["terminal_elapsed_ms"],
                zero_time_atom=False,
            ):
                pair = make_compact_pair(
                    spec, day_index, episode_index, index
                )
                compact.extend(pair)
                state = pair[0]
                kind = desired
                if (
                    Decimal(state["decision_elapsed_ms"])
                    < Decimal(spec["terminal_elapsed_ms"])
                    < Decimal(state["decision_elapsed_ms"])
                    + Decimal(C.FOK_EFFECTIVE_LATENCY_MS)
                ):
                    kind = "CANCEL_RACE_PAIR"
                fok_outcomes.append(
                    make_fok_outcome(spec, state, kind)
                )
        paths[day] = (f"/synthetic/date={day}/postfill.memory",)
        expected[day] = tuple(ids)
    batch = C.validate_and_serialize_postfill_rows(
        logical_source_paths=paths,
        expected_episode_ids_by_day=expected,
        episode_manifest=manifests,
        compact_rows=compact,
        flatten_fok_outcomes=fok_outcomes,
    )
    return P.SyntheticKeepFokDataset(
        marker=P.SYNTHETIC_MARKER,
        batch=batch,
        expected_batch_sha256=batch.canonical_sha256(),
        episode_outcomes=tuple(external),
        expected_outcomes_sha256=P.seal_episode_outcomes(external),
    )


def replace_batch(
    dataset: P.SyntheticKeepFokDataset,
    **changes: object,
) -> P.SyntheticKeepFokDataset:
    batch = replace(dataset.batch, **changes)
    return replace(
        dataset,
        batch=batch,
        expected_batch_sha256=batch.canonical_sha256(),
    )


def replace_outcomes(
    dataset: P.SyntheticKeepFokDataset,
    outcomes: list[dict[str, object]],
) -> P.SyntheticKeepFokDataset:
    return replace(
        dataset,
        episode_outcomes=tuple(outcomes),
        expected_outcomes_sha256=P.seal_episode_outcomes(outcomes),
    )


@pytest.mark.skipif(
    not P.SOURCE_PINS_FINALIZED,
    reason="V4 compatibility only; wait for V4.1 semantic seal",
)
def test_end_to_end_lodo_is_dynamic_but_always_non_deployable():
    report = P.fit_synthetic_keep_fok_policy(synthetic_dataset())
    assert report["claim"] == "NO_CANDIDATE"
    assert report["candidate"] is None
    assert report["action_seal_allowed"] is False
    assert report["live_authorized"] is False
    assert report["deployable"] is False
    assert report["candidate_selected"] is False
    assert report["contract"]["actions"] == [
        "KEEP",
        "FLATTEN_FOK",
    ]
    assert report["contract"]["flatten_fok_effective_latency_ms"] == 60
    assert len(report["folds"]) == 3
    assert {
        fold["holdout_date"] for fold in report["folds"]
    } == set(DAYS)
    for fold in report["folds"]:
        assert fold["holdout_date"] not in fold["train_dates"]
        assert len(fold["train_dates"]) == 2
        for band in P.FEE_BANDS:
            receipt = fold["fee_bands"][band]["policy_receipt"]
            assert receipt["keep_models"]
            assert receipt["flatten_fok_models"]
            assert {
                "feature_lineage_sha256",
                "keep_label_lineage_sha256",
                "flatten_fok_label_lineage_sha256",
                "continuation_label_lineage_sha256",
            } <= set(receipt)
            assert (
                fold["fee_bands"][band]["inner_selection"][
                    "selected_alpha"
                ]
                in P.RIDGE_GRID
            )
    for band, rows in report["oos_fee_band_reports"].items():
        assert rows["fitted_q"]["first_fills"] == 18
        assert rows[
            "overlapping_episode_conditional_diagnostic"
        ]["episode_count"] == 3
        assert rows[
            "overlapping_episode_conditional_diagnostic"
        ]["status"] == P.OVERLAP_STATUS
        assert set(rows["first_side"]) == {"YES", "NO"}
        assert rows["clairvoyant_upper_bound"][
            "uses_future_outcomes"
        ] is True
        assert (
            rows["clairvoyant_upper_bound"][
                "conditional_ev_per_first_fill_usd"
            ]
            >= rows["fitted_q"][
                "conditional_ev_per_first_fill_usd"
            ]
        )
        mechanism = rows["mechanism_q_G_L_and_tail"]
        assert "maker_completion_rate_q" in mechanism["fitted_q"]
        assert "mean_pair_gain_G_cents" in mechanism["fitted_q"]
        assert "mean_fok_or_exit_loss_L_cents" in mechanism["fitted_q"]
        assert set(
            mechanism["fitted_q"]["fok_or_exit_loss_tail_cents"]
        ) == {"p90", "p95", "cvar95"}
        for side in ("YES", "NO"):
            side_report = rows["first_side"][side]
            side_mechanism = side_report[
                "mechanism_q_G_L_and_tail"
            ]["fitted_q"]
            assert {
                "maker_completion_rate_q",
                "mean_pair_gain_G_cents",
                "mean_fok_or_exit_loss_L_cents",
                "empirical_q_star_L_over_G_plus_L",
                "actual_q_minus_q_star",
                "fok_or_exit_loss_tail_cents",
            } <= set(side_mechanism)
            assert set(
                side_mechanism["fok_or_exit_loss_tail_cents"]
            ) == {"p90", "p95", "cvar95"}
        assert set(rows["first_side_nonnegative_gate"]) == {
            "YES",
            "NO",
        }
    json.dumps(report, allow_nan=False)


def test_backward_models_and_scalers_use_only_the_two_train_dates():
    trajectories = P.validate_synthetic_dataset(synthetic_dataset())
    holdout = DAYS[0]
    train = [
        row for row in trajectories if row.source_date != holdout
    ]
    policy = P._fit_backward_policy(
        train,
        fee_band="conservative_fee",
        alpha=1.0,
    )
    assert set(policy.train_dates) == set(DAYS[1:])
    assert policy.keep_models and policy.fok_models
    assert set(policy.keep_models) == set(policy.fok_models)
    for model in (*policy.keep_models.values(), *policy.fok_models.values()):
        assert model.action_kind in {"KEEP", "FLATTEN_FOK"}
        assert model.train_rows > 0
        expected_hash = P._canonical_sha256(
            sorted(
                trajectory.states[model.grid_index][
                    "causal_source_rows_sha256"
                ]
                for trajectory in train
                if not trajectory.overlap
                and not trajectory.outcome["zero_time_atom"]
                and model.grid_index in trajectory.states
            )
        )
        assert model.train_source_hashes_sha256 == expected_hash


def test_heldout_execution_is_sequential_and_marks_no_outcome_lookahead():
    trajectories = P.validate_synthetic_dataset(synthetic_dataset())
    holdout = DAYS[0]
    train = [
        row
        for row in trajectories
        if row.source_date != holdout and not row.overlap
    ]
    heldout = next(
        row
        for row in trajectories
        if row.source_date == holdout
        and not row.overlap
        and not row.outcome["zero_time_atom"]
    )
    policy = P._fit_backward_policy(
        train,
        fee_band="conservative_fee",
        alpha=1.0,
    )
    result = P._execute_learned(heldout, policy)
    indices = [row["decision_index"] for row in result["trace"]]
    assert indices == sorted(indices)
    assert all(
        row["heldout_outcome_used_for_action_selection"] is False
        and row["feature_asof_wall_ns"]
        <= row["decision_recv_wall_ns"]
        for row in result["trace"]
    )


def test_heldout_outcome_metamorphosis_cannot_change_action_trace():
    trajectories = P.validate_synthetic_dataset(synthetic_dataset())
    holdout = DAYS[0]
    train = [
        row
        for row in trajectories
        if row.source_date != holdout and not row.overlap
    ]
    heldout = next(
        row
        for row in trajectories
        if row.source_date == holdout
        and not row.overlap
        and not row.outcome["zero_time_atom"]
        and len(row.states) > 1
    )
    policy = P._fit_backward_policy(
        train,
        fee_band="conservative_fee",
        alpha=1.0,
    )
    baseline = P._execute_learned(heldout, policy)
    mutated = deepcopy(heldout)
    for index, receipt in mutated.fok_outcomes.items():
        sign = Decimal("1") if index % 2 else Decimal("-1")
        receipt["net_pnl_fee_low_usd"] = sign * Decimal("999")
        receipt["net_pnl_fee_base_usd"] = sign * Decimal("998")
        receipt["net_pnl_fee_high_usd"] = sign * Decimal("997")
        receipt["conservative_reward_usd"] = sign * Decimal("996")
    changed = P._execute_learned(mutated, policy)
    assert [
        (
            row["decision_index"],
            row["action"],
            row["predicted_q_keep_usd"],
            row["predicted_q_flatten_fok_usd"],
        )
        for row in baseline["trace"]
    ] == [
        (
            row["decision_index"],
            row["action"],
            row["predicted_q_keep_usd"],
            row["predicted_q_flatten_fok_usd"],
        )
        for row in changed["trace"]
    ]


def test_current_comparator_uses_sealed_non_grid_policy_receipt():
    trajectory = next(
        row
        for row in P.validate_synthetic_dataset(synthetic_dataset())
        if not row.outcome["zero_time_atom"]
    )
    result = P._execute_current(trajectory, "conservative_fee")
    assert result["terminal_elapsed_ms"] == 1375.0
    assert result["trace"] == [
        {
            "policy_row_id": (
                f"current-{trajectory.episode_id}"
            ),
            "policy_definition_sha256": digest(
                "CURRENT_DIST2_TTL60-definition"
            ),
            "comparator_provenance": "SYNTHETIC",
            "source_rows_sha256": digest(
                f"current-{trajectory.episode_id}-receipt"
            ),
            "sealed_comparator_receipt": True,
            "grid_snapped": False,
        }
    ]


class _ConstantModel:
    def __init__(self, value: float) -> None:
        self.value = value

    def predict(self, _state: object) -> float:
        return self.value


def test_fok_zero_is_retained_as_both_band_lower_bound_and_gate_veto():
    dataset = synthetic_dataset()
    trajectories = P.validate_synthetic_dataset(dataset)
    trajectory = next(
        row
        for row in trajectories
        if any(
            receipt["terminal_type"] == "FOK_ZERO"
            for receipt in row.fok_outcomes.values()
        )
        and not row.overlap
    )
    index = next(
        index
        for index, receipt in trajectory.fok_outcomes.items()
        if receipt["terminal_type"] == "FOK_ZERO"
    )
    assert P._fok_reward(
        trajectory, index, "optimistic_min_fee"
    ) == P._fok_reward(
        trajectory, index, "conservative_fee"
    )
    receipt = trajectory.fok_outcomes[index]
    expected = -(
        trajectory.outcome["first_leg_cost_basis_usd"]
    )
    assert receipt["conservative_reward_usd"] == expected
    policy = P.BackwardPolicy(
        fee_band="conservative_fee",
        alpha=1.0,
        train_dates=tuple(DAYS[1:]),
        keep_models={
            grid: _ConstantModel(-999.0)
            for grid in trajectory.states
        },
        fok_models={
            grid: _ConstantModel(
                999.0 if grid == index else -1_000.0
            )
            for grid in trajectory.states
        },
    )
    # Start directly at the selected ZERO grid to isolate terminal
    # evaluation from preceding decisions.
    shortened = replace(
        trajectory,
        states={
            grid: state
            for grid, state in trajectory.states.items()
            if grid >= index
        },
        actions={
            grid: action
            for grid, action in trajectory.actions.items()
            if grid >= index
        },
        keep_transitions={
            grid: transition
            for grid, transition in trajectory.keep_transitions.items()
            if grid >= index
        },
        fok_outcomes={
            grid: outcome
            for grid, outcome in trajectory.fok_outcomes.items()
            if grid >= index
        },
    )
    selected = P._execute_learned(shortened, policy)
    assert selected["selected_fok_zero"] is True
    assert selected["terminal_type"] == "FOK_ZERO"
    assert selected["reward_value_kind"] == "LOWER_BOUND"
    assert selected["simulation_reward_exact"] is False

    original_execute = P._execute_learned

    def force_one_zero(
        row: P._Trajectory, fitted: P.BackwardPolicy
    ) -> dict[str, object]:
        for candidate, outcome in row.fok_outcomes.items():
            if outcome["terminal_type"] == "FOK_ZERO":
                return P._fok_result(
                    row,
                    candidate,
                    fitted.fee_band,
                    "FITTED_Q",
                    (
                        {
                            "decision_index": candidate,
                            "action": "FLATTEN_FOK",
                            "forced_by_test": True,
                        },
                    ),
                )
        return original_execute(row, fitted)

    with mock.patch.object(
        P, "_execute_learned", side_effect=force_one_zero
    ):
        if not P.SOURCE_PINS_FINALIZED:
            pytest.skip("wait for V4.1 semantic seal")
        report = P.fit_synthetic_keep_fok_policy(dataset)
    assert report["postfill_discovery_gate_only"][
        "hypothetical_pass"
    ] is False
    assert any(
        count > 0
        for count in report["postfill_discovery_gate_only"][
            "selected_fok_zero_by_band"
        ].values()
    )


def test_learner_rechecks_fok_protocol_and_prior_synthetic_cancel():
    dataset = synthetic_dataset()
    outcomes = [
        dict(row)
        for row in dataset.batch.postfill_compact_flatten_fok_outcome
    ]
    target = next(
        row for row in outcomes if row["terminal_type"] == "FOK_FULL"
    )
    target["public_crossing_event_applied"] = True
    bad = replace_batch(
        dataset,
        postfill_compact_flatten_fok_outcome=tuple(outcomes),
    )
    with pytest.raises(
        P.KeepFokPolicyContractError,
        match="public crossing",
    ):
        P.validate_synthetic_dataset(bad)

    actions = [
        dict(row)
        for row in dataset.batch.postfill_compact_causal_action
    ]
    target_action = next(
        row for row in actions if row["action_kind"] == "FLATTEN_FOK"
    )
    target_action[
        "fok_requires_prior_synthetic_cancel_applied"
    ] = False
    bad_action = replace_batch(
        dataset,
        postfill_compact_causal_action=tuple(actions),
    )
    with pytest.raises(
        P.KeepFokPolicyContractError,
        match="frozen execution contract",
    ):
        P.validate_synthetic_dataset(bad_action)


@pytest.mark.parametrize(
    ("field", "mutator", "match"),
    (
        (
            "capital_dollar_seconds_increment",
            lambda value: value + Decimal("0.01"),
            "KEEP capital increment",
        ),
        (
            "first_leg_cost_basis_usd",
            lambda value: value + Decimal("0.01"),
            "KEEP first-leg cost basis",
        ),
        (
            "exit_fee_usd",
            lambda value: value + Decimal("0.01"),
            "KEEP terminal fee composition",
        ),
        (
            "maker_fee_usd",
            lambda value: value + Decimal("0.01"),
            "KEEP terminal fee composition",
        ),
        (
            "immediate_net_pnl_usd",
            lambda value: value + Decimal("0.01"),
            "KEEP terminal net",
        ),
    ),
)
def test_resealed_keep_economic_mutation_is_rejected(
    field: str,
    mutator: object,
    match: str,
) -> None:
    dataset = synthetic_dataset()
    transitions = [
        dict(row)
        for row in dataset.batch.postfill_compact_keep_transition
    ]
    target = next(
        row
        for row in transitions
        if (
            row["transition_type"] == "KEEP_TO_TERMINAL"
            if field
            in ("exit_fee_usd", "maker_fee_usd", "immediate_net_pnl_usd")
            else True
        )
    )
    target[field] = mutator(target[field])
    resealed = replace_batch(
        dataset,
        postfill_compact_keep_transition=tuple(transitions),
    )
    with pytest.raises(P.KeepFokPolicyContractError, match=match):
        P.validate_synthetic_dataset(resealed)


@pytest.mark.parametrize(
    ("terminal_type", "field", "replacement", "match"),
    (
        (
            "FOK_FULL",
            "maker_fee_usd",
            Decimal("9"),
            "FOK maker fee",
        ),
        (
            "FOK_FULL",
            "simulation_reward_exact",
            True,
            "FOK_FULL reward flags",
        ),
        (
            "FOK_FULL",
            "conservative_reward_usd",
            Decimal("9"),
            "FOK_FULL conservative reward",
        ),
        (
            "FOK_ZERO",
            "conservative_reward_usd",
            Decimal("0"),
            "FOK_ZERO conservative reward",
        ),
        (
            "FOK_ZERO",
            "capital_released",
            True,
            "FOK_ZERO reward flags",
        ),
        (
            "CANCEL_RACE_PAIR",
            "maker_fee_usd",
            Decimal("9"),
            "FOK maker fee",
        ),
    ),
)
def test_resealed_fok_economic_or_flag_mutation_is_rejected(
    terminal_type: str,
    field: str,
    replacement: object,
    match: str,
) -> None:
    dataset = synthetic_dataset()
    outcomes = [
        dict(row)
        for row in dataset.batch.postfill_compact_flatten_fok_outcome
    ]
    target = next(
        row for row in outcomes if row["terminal_type"] == terminal_type
    )
    target[field] = replacement
    resealed = replace_batch(
        dataset,
        postfill_compact_flatten_fok_outcome=tuple(outcomes),
    )
    with pytest.raises(P.KeepFokPolicyContractError, match=match):
        P.validate_synthetic_dataset(resealed)


def test_current_definition_hash_is_independently_fixed_and_consistent():
    dataset = synthetic_dataset()
    assert {
        row["historical_current_outcome"][
            "policy_definition_sha256"
        ]
        for row in dataset.episode_outcomes
    } == {P.EXPECTED_CURRENT_POLICY_DEFINITION_SHA256}
    outcomes = [
        deepcopy(row) for row in dataset.episode_outcomes
    ]
    outcomes[0]["historical_current_outcome"][
        "policy_definition_sha256"
    ] = digest("different-current-definition")
    resealed = replace_outcomes(dataset, outcomes)
    with pytest.raises(
        P.KeepFokPolicyContractError,
        match="CURRENT policy definition hash mismatch",
    ):
        P.validate_synthetic_dataset(resealed)


def test_final_60s_keep_transition_reaches_exact_terminal_once():
    trajectory = next(
        row
        for row in P.validate_synthetic_dataset(synthetic_dataset())
        if row.outcome["terminal_elapsed_ms"] == Decimal("61000")
        and not row.overlap
    )
    assert 8 in trajectory.states
    final = trajectory.keep_transitions[8]
    assert final["transition_type"] == "KEEP_TO_TERMINAL"
    assert final["keep_terminal_type"] == "COMPLEMENT_FILL"
    assert final["interval_start_elapsed_ms"] == Decimal("60000")
    assert final["interval_stop_elapsed_ms"] == Decimal("61000")
    nonzero_rewards = [
        transition["immediate_net_pnl_usd"]
        for transition in trajectory.keep_transitions.values()
        if transition["immediate_net_pnl_usd"] != 0
    ]
    assert nonzero_rewards == [final["immediate_net_pnl_usd"]]
    assert all(
        transition["immediate_net_pnl_usd"] == 0
        for transition in trajectory.keep_transitions.values()
        if transition["transition_type"] == "NEXT_STATE"
    )


def test_terminal_exactly_60000_wins_without_a_60000_state():
    trajectory = next(
        row
        for row in P.validate_synthetic_dataset(synthetic_dataset())
        if row.outcome["terminal_elapsed_ms"] == Decimal("60000")
        and not row.overlap
    )
    assert 8 not in trajectory.states
    final_index = max(trajectory.states)
    assert final_index == 7
    final = trajectory.keep_transitions[final_index]
    assert final["transition_type"] == "KEEP_TO_TERMINAL"
    assert final["interval_start_elapsed_ms"] == Decimal("30000")
    assert final["interval_stop_elapsed_ms"] == Decimal("60000")
    assert sum(
        transition["immediate_net_pnl_usd"] != 0
        for transition in trajectory.keep_transitions.values()
    ) == 1


def _state_snapshots_for_missing_tests() -> tuple[
    dict[str, float | None],
    dict[str, float | None],
]:
    trajectories = P.validate_synthetic_dataset(synthetic_dataset())
    missing_state = next(
        state
        for row in trajectories
        for state in row.states.values()
        if state["two_sided_touch_available"] is False
    )
    observed_state = next(
        state
        for row in trajectories
        for state in row.states.values()
        if state["two_sided_touch_available"] is True
    )
    return (
        P.causal_feature_snapshot(
            missing_state, "conservative_fee"
        ),
        P.causal_feature_snapshot(
            observed_state, "conservative_fee"
        ),
    )


def test_nullable_features_use_train_only_imputation_and_indicators():
    missing, observed = _state_snapshots_for_missing_tests()
    assert P.MISSING_INDICATOR_FEATURES == (
        "two_sided_touch_available",
        "complement_touch_available",
        "kernel_fair_available",
    )
    second_observed = dict(observed)
    for name in P.NULLABLE_CURRENT_FEATURES:
        second_observed[name] = float(observed[name]) + 10.0
    scaler = P.FoldScaler.fit((observed, second_observed))
    receipt_before = scaler.receipt_sha256()
    train_transform_before = scaler.transform(observed).copy()
    extreme_heldout = dict(observed)
    for name in P.NULLABLE_CURRENT_FEATURES:
        extreme_heldout[name] = 1e12
    transformed = scaler.transform(extreme_heldout)
    assert np.all(np.isfinite(transformed))
    assert scaler.receipt_sha256() == receipt_before
    assert np.array_equal(
        scaler.transform(observed), train_transform_before
    )
    missing_vector = scaler.transform(missing)
    for name in P.MISSING_INDICATOR_FEATURES:
        indicator_index = scaler.feature_names.index(name)
        assert missing_vector[indicator_index] == -1.0


def test_all_missing_train_column_is_zero_scaled_and_indicator_retained():
    missing, observed = _state_snapshots_for_missing_tests()
    scaler = P.FoldScaler.fit((missing, missing))
    for name in P.NULLABLE_CURRENT_FEATURES:
        value_index = scaler.feature_names.index(name)
        assert scaler.all_missing_train[value_index]
        transformed_missing = scaler.transform(missing)
        transformed_observed = scaler.transform(observed)
        assert transformed_missing[value_index] == 0.0
        assert transformed_observed[value_index] == 0.0
    for indicator in P.MISSING_INDICATOR_FEATURES:
        indicator_index = scaler.feature_names.index(indicator)
        assert (
            transformed_missing[indicator_index]
            != transformed_observed[indicator_index]
        )


def test_nonnullable_missing_feature_fails_closed():
    _missing, observed = _state_snapshots_for_missing_tests()
    bad = dict(observed)
    bad["remaining_inventory_fp"] = None
    with pytest.raises(
        P.KeepFokPolicyContractError,
        match="nonnullable train feature missing",
    ):
        P.FoldScaler.fit((bad,))


def test_historical_kernel_is_always_unavailable_and_all_null():
    dataset = synthetic_dataset()
    trajectories = P.validate_synthetic_dataset(dataset)
    state = next(
        state for row in trajectories for state in row.states.values()
    )
    assert state["kernel_fair_available"] is False
    features = P.causal_feature_snapshot(
        state, "conservative_fee"
    )
    assert "kernel_source_time_ms" not in features
    assert "kernel_causality_kind" not in features
    for name in (
        "kernel_fair_e4",
        "first_leg_fair_edge_e4",
        "complement_fair_edge_e4",
        "kernel_fair_move_since_entry_e4",
        "kernel_fair_move_since_fill_e4",
    ):
        assert name in features
        assert features[name] is None
    states = [
        dict(row)
        for row in dataset.batch.postfill_compact_causal_state
    ]
    target = states[0]
    target["kernel_fair_available"] = True
    bad = replace_batch(
        dataset, postfill_compact_causal_state=tuple(states)
    )
    with pytest.raises(
        P.KeepFokPolicyContractError,
        match="historical primary kernel",
    ):
        P.validate_synthetic_dataset(bad)


def test_missing_rows_are_not_dropped_from_grid_training():
    trajectories = P.validate_synthetic_dataset(synthetic_dataset())
    train = [
        row
        for row in trajectories
        if row.source_date != DAYS[0] and not row.overlap
    ]
    policy = P._fit_backward_policy(
        train,
        fee_band="conservative_fee",
        alpha=1.0,
    )
    for index, model in policy.keep_models.items():
        expected = sum(
            index in row.states
            for row in train
            if not row.outcome["zero_time_atom"]
        )
        assert model.train_rows == expected
        assert policy.fok_models[index].train_rows == expected


def test_zero_time_atom_has_no_decision_and_is_counted_once():
    trajectory = next(
        row
        for row in P.validate_synthetic_dataset(synthetic_dataset())
        if row.outcome["zero_time_atom"]
    )
    assert not trajectory.states
    assert not trajectory.actions
    assert not trajectory.keep_transitions
    assert not trajectory.fok_outcomes
    result = P._execute_current(trajectory, "conservative_fee")
    assert result["trace"] == []
    assert result["terminal_type"] == "ZERO_TIME_ATOM"
    assert result["maker_completion"] is True


def test_row_hash_marker_and_forbidden_dates_fail_closed():
    dataset = synthetic_dataset()
    states = [
        dict(row)
        for row in dataset.batch.postfill_compact_causal_state
    ]
    states[0]["complement_queue_position_fp"] = Decimal("999")
    tampered_batch = replace(dataset.batch, postfill_compact_causal_state=tuple(states))
    tampered = replace(dataset, batch=tampered_batch)
    with pytest.raises(
        P.KeepFokPolicyContractError, match="row hash mismatch"
    ):
        P.validate_synthetic_dataset(tampered)
    with pytest.raises(
        P.KeepFokPolicyContractError, match="marker mismatch"
    ):
        P.validate_synthetic_dataset(
            replace(dataset, marker="NOT_SYNTHETIC")
        )
    for forbidden in ("2026-07-23", "2026-07-26"):
        bad_paths = tuple(dataset.batch.guarded_source_paths) + (
            f"/synthetic/date={forbidden}/forbidden.memory",
        )
        bad = replace_batch(
            dataset, guarded_source_paths=bad_paths
        )
        with pytest.raises(ForbiddenSourceError):
            P.validate_synthetic_dataset(bad)


def test_exact_outcome_float_is_rejected_even_when_resealed():
    dataset = synthetic_dataset()
    outcomes = [
        deepcopy(row) for row in dataset.episode_outcomes
    ]
    outcomes[0]["first_fill_fee_usd"] = 0.001
    bad = replace_outcomes(dataset, outcomes)
    with pytest.raises(
        P.KeepFokPolicyContractError,
        match="floats and booleans are forbidden",
    ):
        P.validate_synthetic_dataset(bad)


def test_mechanism_formula_q_star_and_tail_are_exact():
    rows = [
        {
            "maker_completion": True,
            "net_pnl_usd": 0.01,
        },
        {
            "maker_completion": True,
            "net_pnl_usd": 0.03,
        },
        {
            "maker_completion": False,
            "net_pnl_usd": -0.08,
        },
        {
            "maker_completion": False,
            "net_pnl_usd": -0.12,
        },
    ]
    metrics = P._mechanism_metrics(rows)
    assert metrics["maker_completion_rate_q"] == 0.5
    assert metrics["mean_pair_gain_G_cents"] == 2.0
    assert metrics["mean_fok_or_exit_loss_L_cents"] == 10.0
    assert metrics[
        "empirical_q_star_L_over_G_plus_L"
    ] == pytest.approx(10.0 / 12.0)
    assert metrics["actual_q_minus_q_star"] == pytest.approx(
        0.5 - 10.0 / 12.0
    )
    assert metrics["fok_or_exit_loss_tail_cents"]["p95"] > 0


@pytest.mark.skipif(
    not P.SOURCE_PINS_FINALIZED,
    reason="V4 compatibility only; wait for V4.1 semantic seal",
)
def test_report_names_estimand_conditionally_and_contains_no_ioc_action():
    report = P.fit_synthetic_keep_fok_policy(synthetic_dataset())
    encoded = json.dumps(report, allow_nan=False)
    assert "conditional_ev_per_first_fill_usd" in encoded
    assert "conditional_cents_per_postfill_locked_dollar_hour" in encoded
    assert '"IOC"' not in encoded
    assert "EV/cycle" not in encoded
    assert report["postfill_discovery_gate_only"][
        "not_a_whole_policy_candidate_gate"
    ] is True


def test_v4_compatibility_hashes_are_identified_but_not_authorized():
    root = Path(__file__).resolve().parents[1]
    contract = (
        root
        / "tools/research/crypto_mm/round4_postfill_state_contract.py"
    )
    ddl = (
        root
        / "tmp/crypto_mm_canary_20260726/round4/"
        "round4_postfill_state_contract.sql"
    )
    prereg = (
        root
        / "tmp/crypto_mm_canary_20260726/round4/"
            "ROUND4_POSTFILL_KEEP_FLATTEN_FOK_PREREG_V4.json"
    )
    manifest = (
        root
        / "tmp/crypto_mm_canary_20260726/round4/"
            "FULL_COVERAGE_STAGE2_V4_SHA256SUMS"
    )
    for path, expected in (
        (contract, P.SEALED_SOURCE_CONTRACT_SHA256),
        (ddl, P.SEALED_SOURCE_DDL_SHA256),
        (prereg, P.SEALED_SOURCE_PREREG_SHA256),
        (manifest, P.SEALED_SOURCE_MANIFEST_SHA256),
    ):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    assert P.SOURCE_PINS_FINALIZED is False
