#!/usr/bin/env python3
"""Fail-closed FULL-COVERAGE Stage-2 extraction contract.

This module is an in-memory boundary between a future canary extractor and
the ROUND4 post-fill tables.  It deliberately does not read files, fit a
model, rank an action, or authorize live trading.

For every post-fill episode that is still at risk at a sealed decision-grid
time, the extractor must provide exactly two compact rows built from the
same causal receipt state: one ``KEEP`` and one canonical
``FLATTEN_FOK``.  A terminal at the
same timestamp wins over a decision.  A same-envelope completion at elapsed
zero is serialized only as ``postfill_zero_time_atom`` and never receives a
decision row or an epsilon-duration risk interval.

The public entry point, :func:`validate_and_serialize_postfill_rows`, first
preflights every logical source path.  It then validates the complete
market-day batch in memory and returns immutable DDL row tuples only after
all episode, grid, pairing, receipt-clock and ledger checks pass.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json
import os
from pathlib import Path
import re

from tools.research.crypto_mm.round4_table_builder import (
    DISCOVERY_DATES,
    EXPERIMENT_ID,
    FORBIDDEN_DATES,
    ForbiddenSourceError,
    Round4ContractError,
    preflight_source_paths,
)


CONTRACT_VERSION = "ROUND4_POSTFILL_PUBLIC_PROXY_V4"
ACTION_FAMILY_VERSION = "ROUND4_KEEP_FLATTEN_FOK_PUBLIC_PROXY_V4"
ALLOWED_ACTIONS = frozenset(("KEEP", "FLATTEN_FOK"))
FOK_EFFECTIVE_LATENCY_MS = 60
FOK_TIME_IN_FORCE = "fill_or_kill"
FOK_TERMINALS = frozenset(
    ("CANCEL_RACE_PAIR", "FOK_FULL", "FOK_ZERO")
)
SELF_CROSS_INVALID_TERMINAL = "SIMULATED_SELF_CROSS_UNIDENTIFIED"
REWARD_VALUE_KINDS = frozenset(
    ("SIMULATION_EXACT", "FEE_BAND", "LOWER_BOUND")
)
DATA_ORIGIN = "PUBLIC_RAW"
FILL_PROVENANCE = "PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY"
EXECUTION_NATURE = "SYNTHETIC"
MAKER_FEE_PROVENANCE = "SCHEDULE_ZERO_MAKER"
MARKET_SERIES_PREFIX = "KXBTC15M-"
FOK_EXECUTION_PROVENANCE = "PUBLIC_BOOK_COUNTERFACTUAL"
KEEP_EXECUTION_PROVENANCE = (
    "ORIGINAL_MAKER_ORDER_PUBLIC_PROXY_OR_SETTLEMENT"
)
CANCEL_TIMING_PROVENANCE = "SYNTHETIC_60MS_SCENARIO"
SELF_CROSS_SCOPE = "SIMULATED_STRATEGY_ORDER_REGISTRY"
SETTLEMENT_PROVENANCE = "OFFICIAL_RESULT_SETTLEMENT_SIMULATION"
SETTLEMENT_FEE_PROVENANCE = "SETTLEMENT_ZERO_FEE"
LEGAL_PRICE_MIN_E4 = 10
LEGAL_PRICE_MAX_E4 = 9_990
E4 = Decimal("10000")
FEE_RATE = Decimal("0.07")
CENTICENT_USD = Decimal("0.0001")
WHOLE_CENT_USD = Decimal("0.01")
DECISION_GRID_MS = (
    0,
    250,
    500,
    1_000,
    2_000,
    5_000,
    10_000,
    30_000,
    60_000,
)
ZERO_TIME_TERMINAL = "ZERO_TIME_ATOM"
CONTINUOUS_TERMINALS = frozenset(
    (
        "COMPLEMENT_FILL",
        "HARD_FALLBACK",
    )
)
LIVE_AUTHORIZED = False
FIT_AUTHORIZED = False
CANDIDATE_SELECTION_AUTHORIZED = False

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SUPPLEMENTAL_DDL_PATH = (
    ROOT
    / "tmp"
    / "crypto_mm_canary_20260726"
    / "round4"
    / "round4_postfill_state_contract.sql"
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STATE_EXACT_DECIMAL_FIELDS = (
    "first_fill_qty_fp",
    "remaining_inventory_fp",
    "complement_queue_position_fp",
    "complement_same_price_ahead_fp",
    "complement_better_depth_fp",
    "complement_flow_1s_fp",
    "complement_flow_5s_fp",
    "complement_flow_10s_fp",
    "complement_flow_60s_fp",
    "complement_flow_acceleration_fp",
    "flatten_visible_gross_pnl_usd",
    "flatten_visible_pnl_fee_low_usd",
    "flatten_visible_pnl_fee_base_usd",
    "flatten_visible_pnl_fee_high_usd",
    "flatten_visible_executable_qty_fp",
    "flatten_visible_residual_inventory_fp",
    "pair_gain_if_complement_usd",
)
STATE_INTEGER_FIELDS = (
    "decision_recv_wall_ns",
    "decision_recv_mono_ns",
    "feature_asof_wall_ns",
    "source_max_recv_wall_ns",
    "source_max_recv_mono_ns",
    "first_fill_price_e4",
    "complement_price_e4",
    "complement_order_age_ms",
    "flatten_limit_price_e4",
    "flatten_adverse_tick_e4",
    "tte_ms",
)
STATE_OPTIONAL_TOUCH_INTEGER_FIELDS = (
    "complement_touch_distance_e4",
    "spread_e4",
    "mid_move_1s_e4",
    "mid_move_10s_e4",
    "mid_move_since_entry_e4",
    "mid_move_since_fill_e4",
)
STATE_OPTIONAL_TOUCH_DECIMAL_FIELDS = ("touch_imbalance",)
STATE_OPTIONAL_KERNEL_FIELDS = (
    "kernel_fair_e4",
    "first_leg_fair_edge_e4",
    "complement_fair_edge_e4",
    "kernel_fair_move_since_entry_e4",
    "kernel_fair_move_since_fill_e4",
    "kernel_source_time_ms",
    "kernel_causality_kind",
)
STATE_SIGNATURE_FIELDS = (
    "source_date_utc",
    "postfill_episode_id",
    "decision_index",
    "decision_elapsed_ms",
    "public_book_data_origin",
    "flatten_counterfactual_provenance",
    "causal_source_rows_sha256",
    "source_max_stable_id",
    *STATE_INTEGER_FIELDS,
    *STATE_OPTIONAL_TOUCH_INTEGER_FIELDS,
    *STATE_OPTIONAL_KERNEL_FIELDS,
    "first_fill_side",
    *STATE_EXACT_DECIMAL_FIELDS,
    *STATE_OPTIONAL_TOUCH_DECIMAL_FIELDS,
    "first_fill_elapsed_ms",
    "complement_order_id",
    "complement_side",
    "flatten_book_side",
    "flatten_limit_fallback",
    "flatten_visible_slices_sha256",
    "two_sided_touch_available",
    "complement_touch_available",
    "kernel_fair_available",
    "simulated_strategy_order_registry_complete",
    "simulated_strategy_order_registry_sha256",
    "cancel_state",
    "market_day_gate_pass",
    "reconciliation_ok",
    "data_invalid",
)


class PostfillStateContractError(Round4ContractError):
    """A compact Stage-2 row violates the extraction contract."""


class MarketDayRollbackRequired(PostfillStateContractError):
    """The complete market-day and every action must be discarded."""

    def __init__(
        self,
        source_dates: Sequence[str],
        reason_code: str,
        detail: str,
    ) -> None:
        self.source_dates = tuple(sorted(set(source_dates)))
        self.reason_code = reason_code
        self.detail = detail
        joined = ",".join(self.source_dates) or "UNKNOWN_DAY"
        super().__init__(
            f"MARKET_DAY_ROLLBACK[{joined}][{reason_code}]: {detail}"
        )


@dataclass(frozen=True)
class PostfillDDLBatch:
    """Validated rows ready for parameterized inserts into the staged DDL."""

    source_dates: tuple[str, ...]
    guarded_source_paths: tuple[str, ...]
    episode_count: int
    postfill_compact_causal_state: tuple[dict[str, object], ...]
    postfill_compact_causal_action: tuple[dict[str, object], ...]
    postfill_compact_keep_transition: tuple[dict[str, object], ...]
    postfill_compact_flatten_fok_outcome: tuple[
        dict[str, object], ...
    ]
    postfill_zero_time_atom: tuple[dict[str, object], ...]

    def as_ddl_rows(self) -> dict[str, tuple[dict[str, object], ...]]:
        """Return table-name keyed rows without performing database writes."""
        return {
            "postfill_compact_causal_state": (
                self.postfill_compact_causal_state
            ),
            "postfill_compact_causal_action": (
                self.postfill_compact_causal_action
            ),
            "postfill_compact_keep_transition": (
                self.postfill_compact_keep_transition
            ),
            "postfill_compact_flatten_fok_outcome": (
                self.postfill_compact_flatten_fok_outcome
            ),
            "postfill_zero_time_atom": self.postfill_zero_time_atom,
        }

    def canonical_sha256(self) -> str:
        """Hash the normalized serialized payload deterministically."""
        payload = {
            "contract_version": CONTRACT_VERSION,
            "action_family_version": ACTION_FAMILY_VERSION,
            "source_dates": self.source_dates,
            "episode_count": self.episode_count,
            "tables": self.as_ddl_rows(),
        }
        encoded = json.dumps(
            _jsonable(payload),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def receipt(self) -> dict[str, object]:
        """Small machine receipt for the future canary extractor."""
        return {
            "contract_version": CONTRACT_VERSION,
            "action_family_version": ACTION_FAMILY_VERSION,
            "allowed_actions": sorted(ALLOWED_ACTIONS),
            "flatten_fok_rule": (
                "one canonical V2 book side from the public book; "
                "decision-time full-depth "
                "worst price plus one adverse legal tick, or the adverse "
                "legal boundary when visible depth is incomplete; total "
                "decision-to-effective latency 60ms; FOK is FULL or ZERO"
            ),
            "data_origin": DATA_ORIGIN,
            "fill_provenance": FILL_PROVENANCE,
            "execution_nature": EXECUTION_NATURE,
            "maker_fee_provenance": MAKER_FEE_PROVENANCE,
            "market_series_prefix": MARKET_SERIES_PREFIX,
            "fok_execution_provenance": FOK_EXECUTION_PROVENANCE,
            "cancel_timing_provenance": CANCEL_TIMING_PROVENANCE,
            "self_cross_scope": SELF_CROSS_SCOPE,
            "fok_effective_latency_ms": FOK_EFFECTIVE_LATENCY_MS,
            "fok_time_in_force": FOK_TIME_IN_FORCE,
            "latency_receipt_sha256": (
                "8d460e6b4e8dbfa87ef373b21e34bb41"
                "e76b0bcf3f881c46f817a04c51f31e08"
            ),
            "latency_scenario_not_production_sla": True,
            "decision_grid_ms": list(DECISION_GRID_MS),
            "keep_transition_types": [
                "NEXT_STATE",
                "KEEP_TO_TERMINAL",
            ],
            "at_risk_inventory_rule": (
                "remaining_inventory_fp=requested_qty_fp="
                "first_fill_qty_fp"
            ),
            "serialized_cost_fee_spine": True,
            "nullable_state_fields": [
                *STATE_OPTIONAL_TOUCH_INTEGER_FIELDS,
                *STATE_OPTIONAL_TOUCH_DECIMAL_FIELDS,
                *STATE_OPTIONAL_KERNEL_FIELDS,
            ],
            "missing_indicators": [
                "two_sided_touch_available",
                "complement_touch_available",
                "kernel_fair_available",
            ],
            "historical_primary_kernel_required_missing": True,
            "source_dates": list(self.source_dates),
            "guarded_source_path_count": len(self.guarded_source_paths),
            "episode_count": self.episode_count,
            "causal_state_rows": len(
                self.postfill_compact_causal_state
            ),
            "causal_action_rows": len(
                self.postfill_compact_causal_action
            ),
            "keep_transition_rows": len(
                self.postfill_compact_keep_transition
            ),
            "flatten_fok_outcome_rows": len(
                self.postfill_compact_flatten_fok_outcome
            ),
            "zero_time_atom_rows": len(self.postfill_zero_time_atom),
            "payload_sha256": self.canonical_sha256(),
            "fit_authorized": FIT_AUTHORIZED,
            "candidate_selection_authorized": (
                CANDIDATE_SELECTION_AUTHORIZED
            ),
            "live_authorized": LIVE_AUTHORIZED,
        }


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _iso_day(value: object) -> str:
    text = str(value)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ForbiddenSourceError(
            f"invalid postfill source date: {text!r}"
        ) from exc


def _exact_decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise PostfillStateContractError(
            f"{label}: floats and booleans are forbidden"
        )
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PostfillStateContractError(
            f"{label}: invalid exact decimal {value!r}"
        ) from exc
    if not result.is_finite():
        raise PostfillStateContractError(f"{label}: non-finite decimal")
    return result


def _plain_int(
    value: object,
    label: str,
    *,
    minimum: int | None = None,
) -> int:
    if type(value) is not int:
        raise PostfillStateContractError(
            f"{label}: expected a plain integer"
        )
    if minimum is not None and value < minimum:
        raise PostfillStateContractError(
            f"{label}: expected value >= {minimum}"
        )
    return value


def _plain_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise PostfillStateContractError(
            f"{label}: expected a plain boolean"
        )
    return value


def _required(
    row: Mapping[str, object],
    fields: Sequence[str],
    label: str,
) -> None:
    missing = [field for field in fields if row.get(field) is None]
    if missing:
        raise PostfillStateContractError(
            f"{label}: missing required fields {missing}"
        )


def _present(
    row: Mapping[str, object],
    fields: Sequence[str],
    label: str,
) -> None:
    missing = [field for field in fields if field not in row]
    if missing:
        raise PostfillStateContractError(
            f"{label}: missing required keys {missing}"
        )


def _sha256(value: object, label: str) -> str:
    text = str(value)
    if not SHA256_RE.fullmatch(text):
        raise PostfillStateContractError(
            f"{label}: expected lowercase sha256"
        )
    return text


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PostfillStateContractError(f"{label}: expected non-empty text")
    return value


def _elapsed_ns(elapsed_ms: Decimal | int) -> int:
    value = _exact_decimal(elapsed_ms, "elapsed_ms") * Decimal("1000000")
    if value != value.to_integral_value():
        raise PostfillStateContractError(
            "elapsed_ms does not map to an integer receipt nanosecond"
        )
    return int(value)


def _is_legal_price(price_e4: int) -> bool:
    if not LEGAL_PRICE_MIN_E4 <= price_e4 <= LEGAL_PRICE_MAX_E4:
        return False
    if price_e4 < 1_000 or price_e4 > 9_000:
        return price_e4 % 10 == 0
    return price_e4 % 100 == 0


def _adverse_limit(
    book_side: str,
    worst_price_e4: int | None,
) -> tuple[int, int, bool]:
    if book_side not in ("BID", "ASK"):
        raise PostfillStateContractError("invalid canonical flatten book side")
    if worst_price_e4 is None:
        boundary = (
            LEGAL_PRICE_MAX_E4 if book_side == "BID"
            else LEGAL_PRICE_MIN_E4
        )
        return boundary, 0, True
    if not _is_legal_price(worst_price_e4):
        raise PostfillStateContractError(
            "visible worst flatten price is off the legal grid"
        )
    direction = 1 if book_side == "BID" else -1
    candidate = worst_price_e4 + direction
    while (
        LEGAL_PRICE_MIN_E4
        <= candidate
        <= LEGAL_PRICE_MAX_E4
        and not _is_legal_price(candidate)
    ):
        candidate += direction
    if not LEGAL_PRICE_MIN_E4 <= candidate <= LEGAL_PRICE_MAX_E4:
        candidate = worst_price_e4
    return candidate, abs(candidate - worst_price_e4), False


def _canonical_book_side(first_fill_side: str) -> str:
    if first_fill_side == "YES":
        return "ASK"
    if first_fill_side == "NO":
        return "BID"
    raise PostfillStateContractError("invalid first-fill side")


def _normalize_slices(
    raw: object,
    *,
    label: str,
    book_side: str,
    limit_price_e4: int | None = None,
) -> tuple[tuple[int, Decimal], ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PostfillStateContractError(f"{label}: expected slice sequence")
    rows: list[tuple[int, Decimal]] = []
    previous: int | None = None
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise PostfillStateContractError(
                f"{label}[{index}]: expected mapping"
            )
        price = _plain_int(
            item.get("price_e4"),
            f"{label}[{index}].price_e4",
        )
        qty = _exact_decimal(
            item.get("qty_fp"),
            f"{label}[{index}].qty_fp",
        )
        if not _is_legal_price(price) or qty <= 0:
            raise PostfillStateContractError(
                f"{label}[{index}]: invalid price/quantity"
            )
        if previous is not None:
            if book_side == "ASK" and price >= previous:
                raise PostfillStateContractError(
                    f"{label}: ASK flatten levels must be strictly "
                    "best-bid first"
                )
            if book_side == "BID" and price <= previous:
                raise PostfillStateContractError(
                    f"{label}: BID flatten levels must be strictly "
                    "best-ask first"
                )
        if limit_price_e4 is not None:
            if (
                book_side == "ASK"
                and price < limit_price_e4
            ) or (
                book_side == "BID"
                and price > limit_price_e4
            ):
                raise PostfillStateContractError(
                    f"{label}: fill violates frozen FOK limit"
                )
        rows.append((price, qty))
        previous = price
    return tuple(rows)


def _slice_economics(
    slices: Sequence[tuple[int, Decimal]],
    *,
    first_fill_side: str,
    first_price_e4: int,
) -> dict[str, Decimal]:
    gross = Decimal("0")
    raw_fees: list[Decimal] = []
    for price_e4, qty in slices:
        if first_fill_side == "YES":
            unit_pnl = (
                Decimal(price_e4) - Decimal(first_price_e4)
            ) / E4
        else:
            unit_pnl = (
                E4
                - Decimal(price_e4)
                - Decimal(first_price_e4)
            ) / E4
        gross += unit_pnl * qty
        p = Decimal(price_e4) / E4
        raw_fees.append(FEE_RATE * qty * p * (Decimal("1") - p))
    raw_total = sum(raw_fees, Decimal("0"))
    fee_low = (
        raw_total.quantize(CENTICENT_USD, rounding=ROUND_CEILING)
        if slices
        else Decimal("0")
    )
    fee_base = sum(
        (
            value.quantize(CENTICENT_USD, rounding=ROUND_CEILING)
            for value in raw_fees
        ),
        Decimal("0"),
    )
    fee_high = (
        sum(
            (
                value.quantize(
                    WHOLE_CENT_USD,
                    rounding=ROUND_CEILING,
                )
                for value in raw_fees
            ),
            Decimal("0"),
        )
        if slices
        else Decimal("0")
    )
    if not fee_low <= fee_base <= fee_high:
        raise PostfillStateContractError("fee band is not monotone")
    return {
        "gross": gross,
        "fee_low": fee_low,
        "fee_base": fee_base,
        "fee_high": fee_high,
        "net_low": gross - fee_low,
        "net_base": gross - fee_base,
        "net_high": gross - fee_high,
    }


def _slices_sha256(slices: Sequence[tuple[int, Decimal]]) -> str:
    payload = [
        {"price_e4": price, "qty_fp": str(qty)}
        for price, qty in slices
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _rollback(
    days: Sequence[str],
    reason_code: str,
    detail: str,
) -> None:
    raise MarketDayRollbackRequired(days, reason_code, detail)


def _row_day(
    row: Mapping[str, object],
    fallback_days: Sequence[str],
) -> tuple[str, ...]:
    raw = row.get("source_date_utc")
    if raw is None:
        return tuple(fallback_days)
    try:
        return (_iso_day(raw),)
    except ForbiddenSourceError:
        return tuple(fallback_days)


def preflight_canary_postfill_paths(
    logical_source_paths: Mapping[
        object, Sequence[os.PathLike[str] | str]
    ],
    expected_episode_ids_by_day: Mapping[object, Sequence[str]],
) -> tuple[
    dict[str, tuple[str, ...]],
    dict[str, tuple[Path, ...]],
]:
    """Gate all paths before a caller is allowed to open any source.

    The function performs no ``stat``, ``resolve``, glob or read.  Its first
    return value is the normalized full-coverage episode expectation.
    """
    expected: dict[str, tuple[str, ...]] = {}
    all_expected_ids: set[str] = set()
    for raw_day, raw_ids in expected_episode_ids_by_day.items():
        day = _iso_day(raw_day)
        if day in FORBIDDEN_DATES:
            raise ForbiddenSourceError(f"forbidden source date: {day}")
        if day not in DISCOVERY_DATES:
            raise ForbiddenSourceError(
                f"date outside ROUND4 discovery allowlist: {day}"
            )
        ids = tuple(str(value) for value in raw_ids)
        if any(not value for value in ids):
            raise PostfillStateContractError(
                f"{day}: empty expected postfill episode id"
            )
        if len(set(ids)) != len(ids):
            raise PostfillStateContractError(
                f"{day}: duplicate expected postfill episode id"
            )
        overlap = all_expected_ids & set(ids)
        if overlap:
            raise PostfillStateContractError(
                "postfill episode id reused across market-days: "
                f"{sorted(overlap)}"
            )
        all_expected_ids.update(ids)
        expected[day] = tuple(sorted(ids))
    if not expected:
        raise PostfillStateContractError(
            "empty market-day coverage expectation"
        )

    guarded: dict[str, tuple[Path, ...]] = {}
    for raw_day, paths in logical_source_paths.items():
        day = _iso_day(raw_day)
        guarded[day] = preflight_source_paths(
            paths,
            day,
            data_role="DISCOVERY",
        )
    if set(guarded) != set(expected):
        raise ForbiddenSourceError(
            "logical source-day set must exactly equal the expected "
            f"market-day set: paths={sorted(guarded)} "
            f"expected={sorted(expected)}"
        )
    return expected, guarded


def required_decision_grid(
    terminal_elapsed_ms: object,
    *,
    zero_time_atom: bool,
) -> tuple[tuple[int, int], ...]:
    """Return grid points that are at risk strictly before terminal.

    Terminal is evaluated first when it shares a timestamp with a grid
    point.  Episodes surviving beyond 60 seconds include the 60-second
    state, but the contract does not invent a post-60 hazard interval.
    """
    terminal = _exact_decimal(
        terminal_elapsed_ms,
        "terminal_elapsed_ms",
    )
    if terminal < 0:
        raise PostfillStateContractError("negative terminal elapsed time")
    if zero_time_atom:
        if terminal != 0:
            raise PostfillStateContractError(
                "zero-time atom must terminate at elapsed zero"
            )
        return ()
    if terminal <= 0:
        raise PostfillStateContractError(
            "continuous episode must terminate after elapsed zero"
        )
    return tuple(
        (index, grid_ms)
        for index, grid_ms in enumerate(DECISION_GRID_MS)
        if Decimal(grid_ms) < terminal
    )


def _validate_episode(
    raw: Mapping[str, object],
    expected_days: Sequence[str],
) -> tuple[dict[str, object], dict[str, object] | None]:
    row = dict(raw)
    forbidden_legacy_claims = {
        "terminal_complement_fee_source_exact",
        "terminal_keep_reward_source_exact",
        "first_fill_private_receipt_exact",
    }
    present_legacy_claims = sorted(forbidden_legacy_claims & set(row))
    if present_legacy_claims:
        raise PostfillStateContractError(
            "legacy private/exact receipt claims are forbidden: "
            f"{present_legacy_claims}"
        )
    required = (
        "experiment_id",
        "data_role",
        "source_date_utc",
        "market_ticker",
        "postfill_episode_id",
        "entry_episode_id",
        "entry_action_id",
        "source_rows_sha256",
        "data_origin",
        "first_fill_provenance",
        "first_fill_execution_nature",
        "first_fill_fee_provenance",
        "first_fill_side",
        "first_fill_price_e4",
        "first_fill_qty_fp",
        "first_fill_fee_usd",
        "first_fill_recv_wall_ns",
        "first_fill_recv_mono_ns",
        "first_fill_elapsed_ms",
        "first_fill_tte_ms",
        "market_close_elapsed_ms",
        "complement_order_id",
        "complement_price_e4",
        "complement_order_age_at_first_fill_ms",
        "terminal_elapsed_ms",
        "terminal_wall_ns",
        "terminal_mono_ns",
        "terminal_type",
        "zero_time_atom",
        "market_day_gate_pass",
        "reconciliation_ok",
        "data_invalid",
    )
    _required(row, required, "postfill episode manifest")
    day = _iso_day(row["source_date_utc"])
    if day not in expected_days:
        raise PostfillStateContractError(
            f"episode carries unexpected source day {day}"
        )
    if row["experiment_id"] != EXPERIMENT_ID:
        raise PostfillStateContractError("experiment_id mismatch")
    if row["data_role"] != "DISCOVERY":
        raise PostfillStateContractError(
            "only DISCOVERY rows are accepted before action-set seal"
        )
    if row["data_origin"] != DATA_ORIGIN:
        raise PostfillStateContractError(
            f"data_origin must be {DATA_ORIGIN}"
        )
    if (
        row["first_fill_provenance"] != FILL_PROVENANCE
        or row["first_fill_execution_nature"] != EXECUTION_NATURE
    ):
        raise PostfillStateContractError(
            "unsupported first-fill provenance; historical fills are "
            "public strict-trade-through synthetic proxies"
        )
    if row["first_fill_fee_provenance"] != MAKER_FEE_PROVENANCE:
        raise PostfillStateContractError(
            "first-fill fee provenance must be SCHEDULE_ZERO_MAKER"
        )
    if (
        _plain_bool(row["data_invalid"], "data_invalid")
        or not _plain_bool(
            row["market_day_gate_pass"],
            "market_day_gate_pass",
        )
        or not _plain_bool(
            row["reconciliation_ok"],
            "reconciliation_ok",
        )
    ):
        raise PostfillStateContractError(
            "DATA_INVALID/non-green day cannot enter Stage-2"
        )
    for field in (
        "market_ticker",
        "postfill_episode_id",
        "entry_episode_id",
        "entry_action_id",
        "complement_order_id",
    ):
        row[field] = _nonempty_text(row[field], field)
    if not row["market_ticker"].startswith(MARKET_SERIES_PREFIX):
        raise PostfillStateContractError(
            "SCHEDULE_ZERO_MAKER is sealed only for the KXBTC15M "
            "market series"
        )
    row["source_rows_sha256"] = _sha256(
        row["source_rows_sha256"],
        "source_rows_sha256",
    )
    side = row["first_fill_side"]
    if side not in ("YES", "NO"):
        raise PostfillStateContractError("invalid first_fill_side")
    price = _plain_int(
        row["first_fill_price_e4"],
        "first_fill_price_e4",
    )
    if not 0 < price < 10_000:
        raise PostfillStateContractError("invalid first_fill_price_e4")
    first_qty = _exact_decimal(
        row["first_fill_qty_fp"],
        "first_fill_qty_fp",
    )
    first_fee = _exact_decimal(
        row["first_fill_fee_usd"],
        "first_fill_fee_usd",
    )
    first_elapsed = _exact_decimal(
        row["first_fill_elapsed_ms"],
        "first_fill_elapsed_ms",
    )
    if first_qty <= 0 or first_fee != 0 or first_elapsed < 0:
        raise PostfillStateContractError(
            "first-fill quantity/elapsed invalid or maker fee is not "
            "schedule zero"
        )
    complement_price = _plain_int(
        row["complement_price_e4"],
        "complement_price_e4",
    )
    if not 0 < complement_price < 10_000:
        raise PostfillStateContractError("invalid complement_price_e4")
    first_wall = _plain_int(
        row["first_fill_recv_wall_ns"],
        "first_fill_recv_wall_ns",
        minimum=1,
    )
    first_mono = _plain_int(
        row["first_fill_recv_mono_ns"],
        "first_fill_recv_mono_ns",
        minimum=1,
    )
    first_tte = _plain_int(
        row["first_fill_tte_ms"],
        "first_fill_tte_ms",
        minimum=0,
    )
    first_age = _plain_int(
        row["complement_order_age_at_first_fill_ms"],
        "complement_order_age_at_first_fill_ms",
        minimum=0,
    )
    terminal = _exact_decimal(
        row["terminal_elapsed_ms"],
        "terminal_elapsed_ms",
    )
    market_close = _exact_decimal(
        row["market_close_elapsed_ms"],
        "market_close_elapsed_ms",
    )
    if (
        market_close < Decimal(first_tte)
        or market_close >= Decimal(first_tte + 1)
    ):
        raise PostfillStateContractError(
            "market-close elapsed must refine integer first-fill TTE"
        )
    zero = _plain_bool(row["zero_time_atom"], "zero_time_atom")
    terminal_type = str(row["terminal_type"])
    grid = required_decision_grid(terminal, zero_time_atom=zero)
    if zero:
        if terminal_type != ZERO_TIME_TERMINAL:
            raise PostfillStateContractError(
                "zero-time episode has wrong terminal_type"
            )
    elif terminal_type == "CURRENT_DIST2_TTL60":
        raise PostfillStateContractError(
            "CURRENT_DIST2_TTL60 is comparator-only and cannot be a "
            "KEEP terminal"
        )
    elif terminal_type not in CONTINUOUS_TERMINALS:
        raise PostfillStateContractError(
            f"unknown continuous terminal_type {terminal_type!r}"
        )
    if terminal > market_close:
        raise PostfillStateContractError(
            "episode terminal exceeds first-fill TTE"
        )
    if not zero and terminal_type == "COMPLEMENT_FILL":
        _required(
            row,
            (
                "terminal_complement_fill_price_e4",
                "terminal_complement_fill_qty_fp",
                "terminal_complement_fill_fee_usd",
                "terminal_complement_fill_provenance",
                "terminal_complement_execution_nature",
                "terminal_complement_fee_provenance",
                "terminal_complement_fill_stable_source_id",
            ),
            "continuous complement terminal",
        )
        terminal_price = _plain_int(
            row["terminal_complement_fill_price_e4"],
            "terminal_complement_fill_price_e4",
        )
        terminal_qty = _exact_decimal(
            row["terminal_complement_fill_qty_fp"],
            "terminal_complement_fill_qty_fp",
        )
        terminal_fee = _exact_decimal(
            row["terminal_complement_fill_fee_usd"],
            "terminal_complement_fill_fee_usd",
        )
        if (
            not 0 < terminal_price < 10_000
            or terminal_price != complement_price
            or terminal_qty != first_qty
            or terminal_fee != 0
            or row["terminal_complement_fill_provenance"]
            != FILL_PROVENANCE
            or row["terminal_complement_execution_nature"]
            != EXECUTION_NATURE
            or row["terminal_complement_fee_provenance"]
            != MAKER_FEE_PROVENANCE
        ):
            raise PostfillStateContractError(
                "public proxy complement terminal provenance/quantity/"
                "schedule-zero fee does not conserve"
            )
        row["terminal_complement_fill_price_e4"] = terminal_price
        row["terminal_complement_fill_qty_fp"] = terminal_qty
        row["terminal_complement_fill_fee_usd"] = terminal_fee
        row["terminal_complement_fill_stable_source_id"] = _nonempty_text(
            row["terminal_complement_fill_stable_source_id"],
            "terminal_complement_fill_stable_source_id",
        )
    elif not zero:
        _required(
            row,
            (
                "terminal_keep_gross_pnl_usd",
                "terminal_keep_first_fill_fee_usd",
                "terminal_keep_exit_fee_usd",
                "terminal_keep_maker_fee_usd",
                "terminal_keep_net_pnl_usd",
                "official_market_result",
                "official_result_provenance",
                "terminal_keep_reward_provenance",
                "terminal_keep_fee_provenance",
                "terminal_keep_stable_source_id",
            ),
            "hard-fallback settlement simulation",
        )
        keep_gross = _exact_decimal(
            row["terminal_keep_gross_pnl_usd"],
            "terminal_keep_gross_pnl_usd",
        )
        keep_fee = _exact_decimal(
            row["terminal_keep_maker_fee_usd"],
            "terminal_keep_maker_fee_usd",
        )
        keep_first_fee = _exact_decimal(
            row["terminal_keep_first_fill_fee_usd"],
            "terminal_keep_first_fill_fee_usd",
        )
        keep_exit_fee = _exact_decimal(
            row["terminal_keep_exit_fee_usd"],
            "terminal_keep_exit_fee_usd",
        )
        keep_net = _exact_decimal(
            row["terminal_keep_net_pnl_usd"],
            "terminal_keep_net_pnl_usd",
        )
        if terminal != market_close:
            raise PostfillStateContractError(
                "HARD_FALLBACK must occur at market close"
            )
        result = str(row["official_market_result"])
        if result not in ("YES", "NO"):
            raise PostfillStateContractError(
                "invalid official market result"
            )
        if (
            row["official_result_provenance"]
            != "OFFICIAL_MARKET_RESULT"
            or row["terminal_keep_reward_provenance"]
            != SETTLEMENT_PROVENANCE
            or row["terminal_keep_fee_provenance"]
            != SETTLEMENT_FEE_PROVENANCE
        ):
            raise PostfillStateContractError(
                "hard-fallback settlement provenance mismatch"
            )
        expected_gross = (
            (
                Decimal(10_000 - price) / E4
                if result == side
                else -Decimal(price) / E4
            )
            * first_qty
        )
        if (
            keep_first_fee != 0
            or keep_exit_fee != 0
            or keep_fee != 0
            or keep_gross != expected_gross
            or keep_net != expected_gross
        ):
            raise PostfillStateContractError(
                "HARD_FALLBACK settlement or zero-fee schedule does "
                "not reconcile"
            )
        row["terminal_keep_gross_pnl_usd"] = keep_gross
        row["terminal_keep_first_fill_fee_usd"] = keep_first_fee
        row["terminal_keep_exit_fee_usd"] = keep_exit_fee
        row["terminal_keep_maker_fee_usd"] = keep_fee
        row["terminal_keep_net_pnl_usd"] = keep_net
        row["official_market_result"] = result
        row["terminal_keep_stable_source_id"] = _nonempty_text(
            row["terminal_keep_stable_source_id"],
            "terminal_keep_stable_source_id",
        )
    terminal_ns = _elapsed_ns(terminal)
    if (
        _plain_int(
            row["terminal_wall_ns"],
            "terminal_wall_ns",
            minimum=1,
        )
        != first_wall + terminal_ns
        or _plain_int(
            row["terminal_mono_ns"],
            "terminal_mono_ns",
            minimum=1,
        )
        != first_mono + terminal_ns
    ):
        raise PostfillStateContractError(
            "terminal receipt/elapsed clock does not conserve"
        )

    row.update(
        {
            "source_date_utc": day,
            "first_fill_price_e4": price,
            "first_fill_qty_fp": first_qty,
            "first_fill_fee_usd": first_fee,
            "first_fill_elapsed_ms": first_elapsed,
            "first_fill_recv_wall_ns": first_wall,
            "first_fill_recv_mono_ns": first_mono,
            "first_fill_tte_ms": first_tte,
            "market_close_elapsed_ms": market_close,
            "complement_price_e4": complement_price,
            "first_leg_cost_basis_usd": (
                Decimal(price) / E4 * first_qty
            ),
            "complement_order_age_at_first_fill_ms": first_age,
            "terminal_elapsed_ms": terminal,
            "required_grid": grid,
        }
    )
    atom = _serialize_zero_time_atom(row) if zero else None
    return row, atom


def _serialize_zero_time_atom(
    episode: Mapping[str, object],
) -> dict[str, object]:
    required = (
        "receipt_envelope_id",
        "first_fill_stable_source_id",
        "complement_fill_stable_source_id",
        "complement_side",
        "complement_fill_price_e4",
        "complement_fill_qty_fp",
        "complement_fill_fee_usd",
        "complement_fill_provenance",
        "complement_execution_nature",
        "complement_fee_provenance",
    )
    _required(episode, required, "zero-time atom episode")
    first_id = _nonempty_text(
        episode["first_fill_stable_source_id"],
        "first_fill_stable_source_id",
    )
    complement_id = _nonempty_text(
        episode["complement_fill_stable_source_id"],
        "complement_fill_stable_source_id",
    )
    if first_id >= complement_id:
        raise PostfillStateContractError(
            "zero-time stable source ordering is invalid"
        )
    complement_side = episode["complement_side"]
    if (
        complement_side not in ("YES", "NO")
        or complement_side == episode["first_fill_side"]
    ):
        raise PostfillStateContractError(
            "zero-time complement side is invalid"
        )
    price = _plain_int(
        episode["complement_fill_price_e4"],
        "complement_fill_price_e4",
    )
    qty = _exact_decimal(
        episode["complement_fill_qty_fp"],
        "complement_fill_qty_fp",
    )
    fee = _exact_decimal(
        episode["complement_fill_fee_usd"],
        "complement_fill_fee_usd",
    )
    if not 0 < price < 10_000:
        raise PostfillStateContractError(
            "invalid zero-time complement price"
        )
    if (
        price != episode["complement_price_e4"]
        or qty != episode["first_fill_qty_fp"]
        or fee != 0
        or episode["complement_fill_provenance"] != FILL_PROVENANCE
        or episode["complement_execution_nature"] != EXECUTION_NATURE
        or episode["complement_fee_provenance"]
        != MAKER_FEE_PROVENANCE
    ):
        raise PostfillStateContractError(
            "zero-time public proxy quantity/provenance/fee does not "
            "conserve"
        )
    return {
        "postfill_episode_id": episode["postfill_episode_id"],
        "market_ticker": episode["market_ticker"],
        "entry_episode_id": episode["entry_episode_id"],
        "entry_action_id": episode["entry_action_id"],
        "source_rows_sha256": episode["source_rows_sha256"],
        "data_origin": episode["data_origin"],
        "first_fill_provenance": episode["first_fill_provenance"],
        "first_fill_execution_nature": episode[
            "first_fill_execution_nature"
        ],
        "first_fill_fee_provenance": episode[
            "first_fill_fee_provenance"
        ],
        "first_fill_side": episode["first_fill_side"],
        "complement_side": complement_side,
        "atom_recv_wall_ns": episode["first_fill_recv_wall_ns"],
        "atom_recv_mono_ns": episode["first_fill_recv_mono_ns"],
        "receipt_envelope_id": _nonempty_text(
            episode["receipt_envelope_id"],
            "receipt_envelope_id",
        ),
        "first_fill_stable_source_id": first_id,
        "complement_fill_stable_source_id": complement_id,
        "complement_fill_price_e4": price,
        "complement_fill_qty_fp": qty,
        "complement_fill_fee_usd": fee,
        "complement_fill_provenance": episode[
            "complement_fill_provenance"
        ],
        "complement_execution_nature": episode[
            "complement_execution_nature"
        ],
        "complement_fee_provenance": episode[
            "complement_fee_provenance"
        ],
        "reconciliation_ok": True,
    }


def _normalize_state(
    raw: Mapping[str, object],
    episode: Mapping[str, object],
) -> dict[str, object]:
    row = dict(raw)
    required = (
        "source_date_utc",
        "postfill_episode_id",
        "decision_index",
        "decision_elapsed_ms",
        "action_family_version",
        "action_kind",
        "causal_source_rows_sha256",
        "source_max_stable_id",
        *STATE_INTEGER_FIELDS,
        "first_fill_side",
        *STATE_EXACT_DECIMAL_FIELDS,
        "first_fill_elapsed_ms",
        "complement_order_id",
        "complement_side",
        "flatten_book_side",
        "flatten_limit_fallback",
        "flatten_visible_slices",
        "public_book_data_origin",
        "flatten_counterfactual_provenance",
        "two_sided_touch_available",
        "complement_touch_available",
        "kernel_fair_available",
        "simulated_strategy_order_registry_complete",
        "simulated_strategy_order_registry_sha256",
        "cancel_state",
        "market_day_gate_pass",
        "reconciliation_ok",
        "data_invalid",
    )
    action_input_fields = {
        "requested_qty_fp",
        "effective_latency_ms",
        "legal_action",
        "skip_reason",
        "time_in_force",
        "self_trade_prevention_type",
        "fok_requires_prior_synthetic_cancel_applied",
        "fok_book_side",
        "fok_limit_price_e4",
        "fok_limit_fallback",
    }
    allowed_fields = (
        set(required)
        | set(STATE_OPTIONAL_TOUCH_INTEGER_FIELDS)
        | set(STATE_OPTIONAL_TOUCH_DECIMAL_FIELDS)
        | set(STATE_OPTIONAL_KERNEL_FIELDS)
        | action_input_fields
    )
    unknown_fields = sorted(set(row) - allowed_fields)
    if unknown_fields:
        raise PostfillStateContractError(
            f"unsealed compact fields: {unknown_fields}"
        )
    _present(
        row,
        (
            *STATE_OPTIONAL_TOUCH_INTEGER_FIELDS,
            *STATE_OPTIONAL_TOUCH_DECIMAL_FIELDS,
            *STATE_OPTIONAL_KERNEL_FIELDS,
        ),
        "compact postfill touch/mid missing bundle",
    )
    _required(row, required, "compact postfill row")
    day = _iso_day(row["source_date_utc"])
    if (
        day != episode["source_date_utc"]
        or str(row["postfill_episode_id"])
        != episode["postfill_episode_id"]
    ):
        raise PostfillStateContractError(
            "compact row episode/day linkage mismatch"
        )
    if row["action_family_version"] != ACTION_FAMILY_VERSION:
        raise PostfillStateContractError(
            "unsealed action_family_version"
        )
    action_kind = row["action_kind"]
    if action_kind not in ALLOWED_ACTIONS:
        raise PostfillStateContractError(
            f"action outside KEEP/FLATTEN_FOK contract: {action_kind!r}"
        )
    if (
        _plain_bool(row["data_invalid"], "data_invalid")
        or not _plain_bool(
            row["market_day_gate_pass"],
            "market_day_gate_pass",
        )
        or not _plain_bool(
            row["reconciliation_ok"],
            "reconciliation_ok",
        )
    ):
        raise PostfillStateContractError(
            "compact row requires market-day rollback"
        )
    index = _plain_int(row["decision_index"], "decision_index", minimum=0)
    elapsed = _plain_int(
        row["decision_elapsed_ms"],
        "decision_elapsed_ms",
        minimum=0,
    )
    if (
        index >= len(DECISION_GRID_MS)
        or elapsed != DECISION_GRID_MS[index]
        or (index, elapsed) not in episode["required_grid"]
    ):
        raise PostfillStateContractError(
            "decision index/time is not an at-risk sealed grid point"
        )
    for field in STATE_INTEGER_FIELDS:
        minimum = None
        if field in (
            "decision_recv_wall_ns",
            "decision_recv_mono_ns",
            "feature_asof_wall_ns",
            "source_max_recv_wall_ns",
            "source_max_recv_mono_ns",
        ):
            minimum = 1
        elif field in (
            "complement_order_age_ms",
            "tte_ms",
        ):
            minimum = 0
        row[field] = _plain_int(row[field], field, minimum=minimum)
    for field in STATE_EXACT_DECIMAL_FIELDS:
        row[field] = _exact_decimal(row[field], field)
    two_sided_touch = _plain_bool(
        row["two_sided_touch_available"],
        "two_sided_touch_available",
    )
    complement_touch = _plain_bool(
        row["complement_touch_available"],
        "complement_touch_available",
    )
    kernel_available = _plain_bool(
        row["kernel_fair_available"],
        "kernel_fair_available",
    )
    if row["public_book_data_origin"] != DATA_ORIGIN:
        raise PostfillStateContractError(
            "postfill book data origin must be PUBLIC_RAW"
        )
    if (
        row["flatten_counterfactual_provenance"]
        != FOK_EXECUTION_PROVENANCE
    ):
        raise PostfillStateContractError(
            "flatten counterfactual provenance must be "
            "PUBLIC_BOOK_COUNTERFACTUAL"
        )
    if two_sided_touch and not complement_touch:
        raise PostfillStateContractError(
            "two-sided touch requires complement-side touch"
        )
    midpoint_bundle = (
        "spread_e4",
        "mid_move_1s_e4",
        "mid_move_10s_e4",
        "mid_move_since_entry_e4",
        "mid_move_since_fill_e4",
    )
    if two_sided_touch:
        for field in midpoint_bundle:
            row[field] = _plain_int(
                row[field],
                field,
                minimum=0 if field == "spread_e4" else None,
            )
        row["touch_imbalance"] = _exact_decimal(
            row["touch_imbalance"],
            "touch_imbalance",
        )
        if not (
            Decimal("-1")
            <= row["touch_imbalance"]
            <= Decimal("1")
        ):
            raise PostfillStateContractError(
                "touch_imbalance outside [-1,1]"
            )
    elif any(
        row[field] is not None
        for field in (*midpoint_bundle, "touch_imbalance")
    ):
        raise PostfillStateContractError(
            "unavailable two-sided touch requires an all-NULL "
            "midpoint/touch bundle; stale carry is forbidden"
        )
    if complement_touch:
        row["complement_touch_distance_e4"] = _plain_int(
            row["complement_touch_distance_e4"],
            "complement_touch_distance_e4",
            minimum=0,
        )
    elif row["complement_touch_distance_e4"] is not None:
        raise PostfillStateContractError(
            "missing complement-side touch requires NULL distance"
        )
    row["first_fill_elapsed_ms"] = _exact_decimal(
        row["first_fill_elapsed_ms"],
        "first_fill_elapsed_ms",
    )
    row["causal_source_rows_sha256"] = _sha256(
        row["causal_source_rows_sha256"],
        "causal_source_rows_sha256",
    )
    row["source_max_stable_id"] = _nonempty_text(
        row["source_max_stable_id"],
        "source_max_stable_id",
    )
    row["complement_order_id"] = _nonempty_text(
        row["complement_order_id"],
        "complement_order_id",
    )
    if (
        row["first_fill_side"] != episode["first_fill_side"]
        or row["first_fill_price_e4"]
        != episode["first_fill_price_e4"]
        or row["first_fill_qty_fp"] != episode["first_fill_qty_fp"]
        or row["first_fill_elapsed_ms"]
        != episode["first_fill_elapsed_ms"]
        or row["complement_order_id"] != episode["complement_order_id"]
        or row["complement_price_e4"] != episode["complement_price_e4"]
    ):
        raise PostfillStateContractError(
            "first-fill/complement identity drift"
        )
    expected_complement = (
        "NO" if episode["first_fill_side"] == "YES" else "YES"
    )
    if row["complement_side"] != expected_complement:
        raise PostfillStateContractError("invalid complement_side")
    expected_book_side = _canonical_book_side(
        str(episode["first_fill_side"])
    )
    if row["flatten_book_side"] != expected_book_side:
        raise PostfillStateContractError(
            "canonical FLATTEN_FOK book-side mapping mismatch"
        )
    if kernel_available:
        raise PostfillStateContractError(
            "historical primary kernel fair must be missing because no "
            "raw receive timestamp supports a causal feature"
        )
    if any(
        row[field] is not None for field in STATE_OPTIONAL_KERNEL_FIELDS
    ):
        raise PostfillStateContractError(
            "historical primary kernel fair bundle must be all-NULL"
        )
    row["flatten_limit_fallback"] = _plain_bool(
        row["flatten_limit_fallback"],
        "flatten_limit_fallback",
    )
    if not _plain_bool(
        row["simulated_strategy_order_registry_complete"],
        "simulated_strategy_order_registry_complete",
    ):
        _rollback(
            (day,),
            SELF_CROSS_INVALID_TERMINAL,
            "simulated strategy order registry is incomplete",
        )
    row["simulated_strategy_order_registry_sha256"] = _sha256(
        row["simulated_strategy_order_registry_sha256"],
        "simulated_strategy_order_registry_sha256",
    )
    if not 0 < row["complement_price_e4"] < 10_000:
        raise PostfillStateContractError("invalid complement_price_e4")
    if row["remaining_inventory_fp"] != row["first_fill_qty_fp"]:
        raise PostfillStateContractError(
            "unidentified partial inventory cannot survive as an "
            "at-risk grid state"
        )
    for field in (
        "complement_queue_position_fp",
        "complement_same_price_ahead_fp",
        "complement_better_depth_fp",
    ):
        if row[field] < 0:
            raise PostfillStateContractError(f"negative {field}")
    expected_acceleration = (
        row["complement_flow_10s_fp"]
        - row["complement_flow_60s_fp"] / Decimal("6")
    )
    if row["complement_flow_acceleration_fp"] != expected_acceleration:
        raise PostfillStateContractError(
            "flow acceleration must equal flow_10s - flow_60s/6"
        )
    slices = _normalize_slices(
        row["flatten_visible_slices"],
        label="flatten_visible_slices",
        book_side=expected_book_side,
    )
    executable = sum(
        (qty for _price, qty in slices),
        Decimal("0"),
    )
    requested = row["remaining_inventory_fp"]
    if executable > requested:
        raise PostfillStateContractError(
            "visible flatten slices exceed remaining inventory"
        )
    residual = requested - executable
    economics = _slice_economics(
        slices,
        first_fill_side=str(episode["first_fill_side"]),
        first_price_e4=int(episode["first_fill_price_e4"]),
    )
    expected_values = {
        "flatten_visible_gross_pnl_usd": economics["gross"],
        "flatten_visible_pnl_fee_low_usd": economics["net_low"],
        "flatten_visible_pnl_fee_base_usd": economics["net_base"],
        "flatten_visible_pnl_fee_high_usd": economics["net_high"],
        "flatten_visible_executable_qty_fp": executable,
        "flatten_visible_residual_inventory_fp": residual,
    }
    for field, expected in expected_values.items():
        if row[field] != expected:
            raise PostfillStateContractError(
                f"{field} disagrees with exact canonical L2 walk"
            )
    worst = slices[-1][0] if residual == 0 and slices else None
    expected_limit, expected_tick, expected_fallback = _adverse_limit(
        expected_book_side,
        worst,
    )
    if (
        row["flatten_limit_price_e4"] != expected_limit
        or row["flatten_adverse_tick_e4"] != expected_tick
        or row["flatten_limit_fallback"] != expected_fallback
    ):
        raise PostfillStateContractError(
            "FLATTEN_FOK limit/tick/fallback rule mismatch"
        )
    row["flatten_visible_slices"] = slices
    row["flatten_visible_slices_sha256"] = _slices_sha256(slices)
    if row["cancel_state"] not in (
        "NONE",
        "PENDING",
        "ACKED",
        "UNKNOWN",
    ):
        raise PostfillStateContractError("invalid cancel_state")
    elapsed_ns = elapsed * 1_000_000
    if (
        row["decision_recv_wall_ns"]
        != episode["first_fill_recv_wall_ns"] + elapsed_ns
        or row["decision_recv_mono_ns"]
        != episode["first_fill_recv_mono_ns"] + elapsed_ns
    ):
        raise PostfillStateContractError(
            "decision receipt/grid clock does not conserve"
        )
    if not (
        episode["first_fill_recv_wall_ns"]
        <= row["source_max_recv_wall_ns"]
        <= row["feature_asof_wall_ns"]
        <= row["decision_recv_wall_ns"]
    ):
        raise PostfillStateContractError(
            "receipt as-of wall clock has lookahead or predates first fill"
        )
    if not (
        episode["first_fill_recv_mono_ns"]
        <= row["source_max_recv_mono_ns"]
        <= row["decision_recv_mono_ns"]
    ):
        raise PostfillStateContractError(
            "receipt as-of monotonic clock has lookahead"
        )
    if (
        row["tte_ms"]
        != episode["first_fill_tte_ms"] - elapsed
        or row["complement_order_age_ms"]
        != episode["complement_order_age_at_first_fill_ms"] + elapsed
    ):
        raise PostfillStateContractError(
            "TTE/order-age decision-grid clock does not conserve"
        )
    row.update(
        {
            "source_date_utc": day,
            "decision_index": index,
            "decision_elapsed_ms": elapsed,
            "action_kind": action_kind,
        }
    )
    return row


def _state_signature(state: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(state[field] for field in STATE_SIGNATURE_FIELDS)


def _state_fingerprint(state: Mapping[str, object]) -> str:
    payload = {
        field: state[field]
        for field in STATE_SIGNATURE_FIELDS
    }
    encoded = json.dumps(
        _jsonable(payload),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decision_id(episode_id: str, decision_index: int) -> str:
    return f"{episode_id}::GRID::{decision_index}"


def _action_id(
    episode_id: str,
    decision_index: int,
    action_kind: str,
) -> str:
    return f"{_decision_id(episode_id, decision_index)}::{action_kind}"


def _serialize_state(
    state: Mapping[str, object],
    episode: Mapping[str, object],
    fingerprint: str,
) -> dict[str, object]:
    return {
        "postfill_decision_id": _decision_id(
            str(state["postfill_episode_id"]),
            int(state["decision_index"]),
        ),
        "experiment_id": EXPERIMENT_ID,
        "data_role": "DISCOVERY",
        "data_origin": episode["data_origin"],
        "first_fill_provenance": episode["first_fill_provenance"],
        "first_fill_execution_nature": episode[
            "first_fill_execution_nature"
        ],
        "first_fill_fee_provenance": episode[
            "first_fill_fee_provenance"
        ],
        "public_book_data_origin": state["public_book_data_origin"],
        "flatten_counterfactual_provenance": state[
            "flatten_counterfactual_provenance"
        ],
        "source_date_utc": state["source_date_utc"],
        "market_ticker": episode["market_ticker"],
        "postfill_episode_id": state["postfill_episode_id"],
        "entry_episode_id": episode["entry_episode_id"],
        "entry_action_id": episode["entry_action_id"],
        "decision_index": state["decision_index"],
        "decision_recv_wall_ns": state["decision_recv_wall_ns"],
        "decision_recv_mono_ns": state["decision_recv_mono_ns"],
        "feature_asof_wall_ns": state["feature_asof_wall_ns"],
        "source_max_recv_wall_ns": state["source_max_recv_wall_ns"],
        "source_max_recv_mono_ns": state["source_max_recv_mono_ns"],
        "source_max_stable_id": state["source_max_stable_id"],
        "episode_source_rows_sha256": episode["source_rows_sha256"],
        "causal_source_rows_sha256": state[
            "causal_source_rows_sha256"
        ],
        "paired_state_fingerprint_sha256": fingerprint,
        "elapsed_since_first_fill_ms": state[
            "decision_elapsed_ms"
        ],
        "first_fill_recv_wall_ns": episode[
            "first_fill_recv_wall_ns"
        ],
        "first_fill_recv_mono_ns": episode[
            "first_fill_recv_mono_ns"
        ],
        "first_fill_elapsed_ms": state["first_fill_elapsed_ms"],
        "first_side": state["first_fill_side"],
        "first_price_e4": state["first_fill_price_e4"],
        "first_qty_fp": state["first_fill_qty_fp"],
        "first_fill_fee_usd": episode["first_fill_fee_usd"],
        "first_leg_cost_basis_usd": episode[
            "first_leg_cost_basis_usd"
        ],
        "remaining_inventory_fp": state["remaining_inventory_fp"],
        "complement_order_id": state["complement_order_id"],
        "complement_side": state["complement_side"],
        "complement_price_e4": state["complement_price_e4"],
        "complement_order_age_ms": state["complement_order_age_ms"],
        "complement_queue_position_fp": state[
            "complement_queue_position_fp"
        ],
        "complement_same_price_ahead_fp": state[
            "complement_same_price_ahead_fp"
        ],
        "complement_better_depth_fp": state[
            "complement_better_depth_fp"
        ],
        "complement_touch_distance_e4": state[
            "complement_touch_distance_e4"
        ],
        "complement_flow_1s_fp": state["complement_flow_1s_fp"],
        "complement_flow_5s_fp": state["complement_flow_5s_fp"],
        "complement_flow_10s_fp": state["complement_flow_10s_fp"],
        "complement_flow_60s_fp": state["complement_flow_60s_fp"],
        "complement_flow_acceleration_fp": state[
            "complement_flow_acceleration_fp"
        ],
        "touch_imbalance": state["touch_imbalance"],
        "spread_e4": state["spread_e4"],
        "mid_move_1s_e4": state["mid_move_1s_e4"],
        "mid_move_10s_e4": state["mid_move_10s_e4"],
        "mid_move_since_entry_e4": state[
            "mid_move_since_entry_e4"
        ],
        "mid_move_since_fill_e4": state[
            "mid_move_since_fill_e4"
        ],
        "kernel_fair_available": state["kernel_fair_available"],
        "kernel_fair_e4": state["kernel_fair_e4"],
        "first_leg_fair_edge_e4": state[
            "first_leg_fair_edge_e4"
        ],
        "complement_fair_edge_e4": state[
            "complement_fair_edge_e4"
        ],
        "kernel_fair_move_since_entry_e4": state[
            "kernel_fair_move_since_entry_e4"
        ],
        "kernel_fair_move_since_fill_e4": state[
            "kernel_fair_move_since_fill_e4"
        ],
        "kernel_source_time_ms": state["kernel_source_time_ms"],
        "kernel_causality_kind": state["kernel_causality_kind"],
        "flatten_book_side": state["flatten_book_side"],
        "flatten_limit_price_e4": state["flatten_limit_price_e4"],
        "flatten_limit_fallback": state["flatten_limit_fallback"],
        "flatten_adverse_tick_e4": state["flatten_adverse_tick_e4"],
        "flatten_visible_slices_sha256": state[
            "flatten_visible_slices_sha256"
        ],
        "flatten_visible_gross_pnl_usd": state[
            "flatten_visible_gross_pnl_usd"
        ],
        "flatten_visible_pnl_fee_low_usd": state[
            "flatten_visible_pnl_fee_low_usd"
        ],
        "flatten_visible_pnl_fee_base_usd": state[
            "flatten_visible_pnl_fee_base_usd"
        ],
        "flatten_visible_pnl_fee_high_usd": state[
            "flatten_visible_pnl_fee_high_usd"
        ],
        "flatten_visible_executable_qty_fp": state[
            "flatten_visible_executable_qty_fp"
        ],
        "flatten_visible_residual_inventory_fp": state[
            "flatten_visible_residual_inventory_fp"
        ],
        "two_sided_touch_available": state[
            "two_sided_touch_available"
        ],
        "complement_touch_available": state[
            "complement_touch_available"
        ],
        "simulated_strategy_order_registry_complete": state[
            "simulated_strategy_order_registry_complete"
        ],
        "simulated_strategy_order_registry_sha256": state[
            "simulated_strategy_order_registry_sha256"
        ],
        "pair_gain_if_complement_usd": state[
            "pair_gain_if_complement_usd"
        ],
        "tte_ms": state["tte_ms"],
        "cancel_state": state["cancel_state"],
        "market_day_gate_pass": True,
        "reconciliation_ok": True,
        "data_invalid": False,
    }


def _serialize_action(
    state: Mapping[str, object],
    fingerprint: str,
) -> dict[str, object]:
    required = (
        "requested_qty_fp",
        "effective_latency_ms",
        "legal_action",
        "time_in_force",
        "self_trade_prevention_type",
        "fok_requires_prior_synthetic_cancel_applied",
    )
    _present(state, required, "compact action")
    _required(state, required[:3], "compact action")
    quantity = _exact_decimal(
        state["requested_qty_fp"],
        "requested_qty_fp",
    )
    if quantity != state["remaining_inventory_fp"]:
        raise PostfillStateContractError(
            "counterfactual action must cover all remaining inventory"
        )
    latency = _exact_decimal(
        state["effective_latency_ms"],
        "effective_latency_ms",
    )
    if latency < 0:
        raise PostfillStateContractError(
            "negative synthetic cancel/FOK latency"
        )
    legal = _plain_bool(state["legal_action"], "legal_action")
    skip_reason = state.get("skip_reason")
    if not legal or skip_reason is not None:
        raise PostfillStateContractError(
            "V4 KEEP/FLATTEN_FOK actions must remain legal after gates"
        )
    kind = str(state["action_kind"])
    old_price = state["complement_price_e4"]
    if kind == "KEEP":
        if any(
            state.get(field) is not None
            for field in (
                "fok_book_side",
                "fok_limit_price_e4",
                "fok_limit_fallback",
            )
        ):
            raise PostfillStateContractError(
                "KEEP cannot carry FLATTEN_FOK fields"
            )
        if (
            latency != 0
            or state.get("time_in_force") is not None
            or state.get("self_trade_prevention_type") is not None
            or state.get(
                "fok_requires_prior_synthetic_cancel_applied"
            )
            is not False
        ):
            raise PostfillStateContractError(
                "KEEP action execution fields are invalid"
            )
        new_price = old_price
        book_side = None
        limit = None
        fallback = None
        tif = None
        stp = None
        requires_synthetic_cancel = False
        post_only = True
        reduce_only = False
    else:
        book_side = state.get("fok_book_side")
        limit = _plain_int(
            state.get("fok_limit_price_e4"),
            "fok_limit_price_e4",
        )
        fallback = _plain_bool(
            state.get("fok_limit_fallback"),
            "fok_limit_fallback",
        )
        tif = state.get("time_in_force")
        stp = state.get("self_trade_prevention_type")
        requires_synthetic_cancel = _plain_bool(
            state.get("fok_requires_prior_synthetic_cancel_applied"),
            "fok_requires_prior_synthetic_cancel_applied",
        )
        if (
            book_side != state["flatten_book_side"]
            or limit != state["flatten_limit_price_e4"]
            or fallback != state["flatten_limit_fallback"]
            or tif != FOK_TIME_IN_FORCE
            or stp != "taker_at_cross"
            or not requires_synthetic_cancel
            or latency != Decimal(FOK_EFFECTIVE_LATENCY_MS)
        ):
            raise PostfillStateContractError(
                "FLATTEN_FOK frozen execution contract mismatch"
            )
        new_price = None
        post_only = False
        reduce_only = True
    episode_id = str(state["postfill_episode_id"])
    index = int(state["decision_index"])
    return {
        "postfill_action_id": _action_id(episode_id, index, kind),
        "postfill_decision_id": _decision_id(episode_id, index),
        "postfill_episode_id": episode_id,
        "source_date_utc": state["source_date_utc"],
        "decision_index": index,
        "action_family_version": ACTION_FAMILY_VERSION,
        "action_kind": kind,
        "action_execution_provenance": (
            KEEP_EXECUTION_PROVENANCE
            if kind == "KEEP"
            else FOK_EXECUTION_PROVENANCE
        ),
        "cancel_timing_provenance": (
            None if kind == "KEEP" else CANCEL_TIMING_PROVENANCE
        ),
        "self_cross_scope": (
            None if kind == "KEEP" else SELF_CROSS_SCOPE
        ),
        "causal_source_rows_sha256": state[
            "causal_source_rows_sha256"
        ],
        "paired_state_fingerprint_sha256": fingerprint,
        "complement_old_price_e4": old_price,
        "complement_new_price_e4": new_price,
        "fok_book_side": book_side,
        "fok_limit_price_e4": limit,
        "fok_limit_fallback": fallback,
        "requested_qty_fp": quantity,
        "post_only": post_only,
        "reduce_only": reduce_only,
        "effective_latency_ms": latency,
        "time_in_force": tif,
        "self_trade_prevention_type": stp,
        "fok_requires_prior_synthetic_cancel_applied": (
            requires_synthetic_cancel
        ),
        "profit_gate_bypassed_for_risk_exit": kind == "FLATTEN_FOK",
        "legal_action": legal,
        "skip_reason": skip_reason,
    }


def _normalize_fok_outcome(
    raw: Mapping[str, object],
    *,
    state: Mapping[str, object],
    episode: Mapping[str, object],
) -> dict[str, object]:
    row = dict(raw)
    required = (
        "source_date_utc",
        "postfill_episode_id",
        "decision_index",
        "postfill_action_id",
        "data_origin",
        "first_fill_provenance",
        "first_fill_execution_nature",
        "first_fill_fee_provenance",
        "fok_execution_provenance",
        "cancel_timing_provenance",
        "self_cross_scope",
        "terminal_type",
        "planned_effective_elapsed_ms",
        "terminal_elapsed_ms",
        "terminal_wall_ns",
        "terminal_mono_ns",
        "synthetic_cancel_applied_wall_ns",
        "synthetic_cancel_applied_mono_ns",
        "synthetic_cancel_applied_sequence",
        "synthetic_cancel_applied_stable_source_id",
        "fok_processed_wall_ns",
        "fok_processed_mono_ns",
        "fok_processed_sequence",
        "fok_processed_stable_source_id",
        "feature_asof_wall_ns",
        "source_max_recv_wall_ns",
        "source_max_recv_mono_ns",
        "source_max_stable_id",
        "outcome_source_rows_sha256",
        "fok_sent",
        "synthetic_cancel_applied_before_terminal",
        "public_crossing_event_applied",
        "simulated_no_self_cross_verified_before_fok",
        "simulated_strategy_order_registry_sha256",
        "fok_book_side",
        "fok_limit_price_e4",
        "fok_limit_fallback",
        "requested_qty_fp",
        "complement_fill_qty_fp",
        "fok_fill_qty_fp",
        "residual_inventory_fp",
        "fok_fill_slices",
        "gross_pnl_usd",
        "maker_fee_usd",
        "taker_fee_low_usd",
        "taker_fee_base_usd",
        "taker_fee_high_usd",
        "net_pnl_fee_low_usd",
        "net_pnl_fee_base_usd",
        "net_pnl_fee_high_usd",
        "conservative_reward_usd",
        "simulation_reward_exact",
        "reward_value_kind",
        "conservative_fit_usable",
        "capital_dollar_seconds",
        "capital_released",
        "reconciliation_ok",
        "data_invalid",
    )
    optional_protocol_fields = (
        "race_complement_price_e4",
        "race_complement_fee_usd",
        "race_complement_stable_source_id",
        "race_fill_provenance",
        "race_execution_nature",
        "race_fee_provenance",
    )
    unknown_fields = sorted(
        set(row) - set(required) - set(optional_protocol_fields)
    )
    if unknown_fields:
        raise PostfillStateContractError(
            f"unsealed FLATTEN_FOK outcome fields: {unknown_fields}"
        )
    _required(
        row,
        tuple(
            field
            for field in required
            if field
            not in (
                "synthetic_cancel_applied_wall_ns",
                "synthetic_cancel_applied_mono_ns",
                "fok_processed_wall_ns",
                "fok_processed_mono_ns",
                "synthetic_cancel_applied_sequence",
                "synthetic_cancel_applied_stable_source_id",
                "fok_processed_sequence",
                "fok_processed_stable_source_id",
                "simulated_no_self_cross_verified_before_fok",
            )
        ),
        "FLATTEN_FOK outcome",
    )
    _present(
        row,
        (
            "synthetic_cancel_applied_wall_ns",
            "synthetic_cancel_applied_mono_ns",
            "synthetic_cancel_applied_sequence",
            "synthetic_cancel_applied_stable_source_id",
            "fok_processed_wall_ns",
            "fok_processed_mono_ns",
            "fok_processed_sequence",
            "fok_processed_stable_source_id",
            "simulated_no_self_cross_verified_before_fok",
            *optional_protocol_fields,
        ),
        "FLATTEN_FOK outcome protocol",
    )
    episode_id = str(row["postfill_episode_id"])
    index = _plain_int(row["decision_index"], "decision_index", minimum=0)
    expected_action_id = _action_id(
        episode_id,
        index,
        "FLATTEN_FOK",
    )
    if (
        episode_id != state["postfill_episode_id"]
        or index != state["decision_index"]
        or row["source_date_utc"] != state["source_date_utc"]
        or row["postfill_action_id"] != expected_action_id
    ):
        raise PostfillStateContractError(
            "FLATTEN_FOK outcome linkage mismatch"
        )
    provenance_expected = {
        "data_origin": DATA_ORIGIN,
        "first_fill_provenance": FILL_PROVENANCE,
        "first_fill_execution_nature": EXECUTION_NATURE,
        "first_fill_fee_provenance": MAKER_FEE_PROVENANCE,
        "fok_execution_provenance": FOK_EXECUTION_PROVENANCE,
        "cancel_timing_provenance": CANCEL_TIMING_PROVENANCE,
        "self_cross_scope": SELF_CROSS_SCOPE,
    }
    for field, expected in provenance_expected.items():
        if row[field] != expected:
            raise PostfillStateContractError(
                f"{field} must be {expected}"
            )
    if any(
        row[field] != episode[field]
        for field in (
            "data_origin",
            "first_fill_provenance",
            "first_fill_execution_nature",
            "first_fill_fee_provenance",
        )
    ):
        raise PostfillStateContractError(
            "FLATTEN_FOK outcome first-fill provenance drift"
        )
    if (
        _plain_bool(row["data_invalid"], "data_invalid")
        or not _plain_bool(
            row["reconciliation_ok"],
            "reconciliation_ok",
        )
    ):
        raise PostfillStateContractError(
            "invalid/unreconciled FLATTEN_FOK outcome"
        )
    gate_sha = _sha256(
        row["simulated_strategy_order_registry_sha256"],
        "simulated_strategy_order_registry_sha256",
    )
    if gate_sha != state["simulated_strategy_order_registry_sha256"]:
        raise PostfillStateContractError(
            "simulated strategy-order registry drift"
        )
    terminal_type = str(row["terminal_type"])
    if terminal_type not in FOK_TERMINALS:
        raise PostfillStateContractError(
            f"invalid FLATTEN_FOK terminal {terminal_type!r}"
        )
    decision_elapsed = int(state["decision_elapsed_ms"])
    planned_effective = _plain_int(
        row["planned_effective_elapsed_ms"],
        "planned_effective_elapsed_ms",
        minimum=0,
    )
    if planned_effective != (
        decision_elapsed + FOK_EFFECTIVE_LATENCY_MS
    ):
        raise PostfillStateContractError(
            "FLATTEN_FOK effective latency is not sealed 60ms"
        )
    terminal_elapsed = _exact_decimal(
        row["terminal_elapsed_ms"],
        "terminal_elapsed_ms",
    )
    terminal_ns = _elapsed_ns(terminal_elapsed)
    terminal_wall = _plain_int(
        row["terminal_wall_ns"],
        "terminal_wall_ns",
        minimum=1,
    )
    terminal_mono = _plain_int(
        row["terminal_mono_ns"],
        "terminal_mono_ns",
        minimum=1,
    )
    if (
        terminal_wall
        != episode["first_fill_recv_wall_ns"] + terminal_ns
        or terminal_mono
        != episode["first_fill_recv_mono_ns"] + terminal_ns
    ):
        raise PostfillStateContractError(
            "FLATTEN_FOK terminal receipt clock does not conserve"
        )
    source_wall = _plain_int(
        row["source_max_recv_wall_ns"],
        "source_max_recv_wall_ns",
        minimum=1,
    )
    source_mono = _plain_int(
        row["source_max_recv_mono_ns"],
        "source_max_recv_mono_ns",
        minimum=1,
    )
    feature_wall = _plain_int(
        row["feature_asof_wall_ns"],
        "feature_asof_wall_ns",
        minimum=1,
    )
    if not (
        state["decision_recv_wall_ns"]
        <= source_wall
        <= feature_wall
        <= terminal_wall
        and state["decision_recv_mono_ns"]
        <= source_mono
        <= terminal_mono
    ):
        raise PostfillStateContractError(
            "FLATTEN_FOK outcome source clock has lookahead/regression"
        )
    row["source_max_stable_id"] = _nonempty_text(
        row["source_max_stable_id"],
        "source_max_stable_id",
    )
    row["outcome_source_rows_sha256"] = _sha256(
        row["outcome_source_rows_sha256"],
        "outcome_source_rows_sha256",
    )
    if (
        row["fok_book_side"] != state["flatten_book_side"]
        or _plain_int(
            row["fok_limit_price_e4"],
            "fok_limit_price_e4",
        )
        != state["flatten_limit_price_e4"]
        or _plain_bool(
            row["fok_limit_fallback"],
            "fok_limit_fallback",
        )
        != state["flatten_limit_fallback"]
    ):
        raise PostfillStateContractError(
            "FLATTEN_FOK outcome changed frozen side/limit"
        )
    quantity_fields = (
        "requested_qty_fp",
        "complement_fill_qty_fp",
        "fok_fill_qty_fp",
        "residual_inventory_fp",
    )
    for field in quantity_fields:
        row[field] = _exact_decimal(row[field], field)
        if row[field] < 0:
            raise PostfillStateContractError(f"negative {field}")
    if (
        row["requested_qty_fp"] != state["remaining_inventory_fp"]
        or row["requested_qty_fp"] != episode["first_fill_qty_fp"]
    ):
        raise PostfillStateContractError(
            "FLATTEN_FOK requested quantity must equal the full "
            "first-fill quantity"
        )
    if (
        row["complement_fill_qty_fp"]
        + row["fok_fill_qty_fp"]
        + row["residual_inventory_fp"]
        != row["requested_qty_fp"]
    ):
        raise PostfillStateContractError(
            "FLATTEN_FOK outcome inventory does not conserve"
        )
    monetary = (
        "gross_pnl_usd",
        "maker_fee_usd",
        "taker_fee_low_usd",
        "taker_fee_base_usd",
        "taker_fee_high_usd",
        "net_pnl_fee_low_usd",
        "net_pnl_fee_base_usd",
        "net_pnl_fee_high_usd",
        "conservative_reward_usd",
        "capital_dollar_seconds",
    )
    for field in monetary:
        row[field] = _exact_decimal(row[field], field)
    if (
        row["maker_fee_usd"] < 0
        or row["taker_fee_low_usd"] < 0
        or row["taker_fee_base_usd"] < 0
        or row["taker_fee_high_usd"] < 0
        or row["capital_dollar_seconds"] < 0
    ):
        raise PostfillStateContractError(
            "negative fee/capital in FLATTEN_FOK outcome"
        )
    slices = _normalize_slices(
        row["fok_fill_slices"],
        label="fok_fill_slices",
        book_side=str(row["fok_book_side"]),
        limit_price_e4=int(row["fok_limit_price_e4"]),
    )
    slice_qty = sum((qty for _price, qty in slices), Decimal("0"))
    if slice_qty != row["fok_fill_qty_fp"]:
        raise PostfillStateContractError(
            "FOK execution slices do not sum to fill quantity"
        )
    economics = _slice_economics(
        slices,
        first_fill_side=str(episode["first_fill_side"]),
        first_price_e4=int(episode["first_fill_price_e4"]),
    )
    first_fee = episode["first_fill_fee_usd"]
    first_cost = episode["first_leg_cost_basis_usd"]
    basis_with_fee = first_cost + first_fee
    fok_sent = _plain_bool(row["fok_sent"], "fok_sent")
    synthetic_cancel_applied = _plain_bool(
        row["synthetic_cancel_applied_before_terminal"],
        "synthetic_cancel_applied_before_terminal",
    )
    no_self_cross_raw = row[
        "simulated_no_self_cross_verified_before_fok"
    ]
    no_self_cross_before_fok = (
        None
        if no_self_cross_raw is None
        else _plain_bool(
            no_self_cross_raw,
            "simulated_no_self_cross_verified_before_fok",
        )
    )
    simulation_reward_exact = _plain_bool(
        row["simulation_reward_exact"],
        "simulation_reward_exact",
    )
    fit_usable = _plain_bool(
        row["conservative_fit_usable"],
        "conservative_fit_usable",
    )
    capital_released = _plain_bool(
        row["capital_released"],
        "capital_released",
    )
    reward_kind = str(row["reward_value_kind"])
    if _plain_bool(
        row["public_crossing_event_applied"],
        "public_crossing_event_applied",
    ):
        raise PostfillStateContractError(
            "effective-time public crossing event must not be applied"
        )
    if reward_kind not in REWARD_VALUE_KINDS:
        raise PostfillStateContractError("unknown reward_value_kind")

    expected_gross: Decimal
    expected_maker_fee: Decimal
    expected_taker = (
        economics["fee_low"],
        economics["fee_base"],
        economics["fee_high"],
    )
    expected_capital: Decimal
    expected_conservative: Decimal
    public_proxy_pair_before_synthetic_cancel = (
        episode["terminal_type"] == "COMPLEMENT_FILL"
        and Decimal(decision_elapsed)
        < episode["terminal_elapsed_ms"]
        < Decimal(planned_effective)
    )
    if (
        public_proxy_pair_before_synthetic_cancel
        and terminal_type != "CANCEL_RACE_PAIR"
    ):
        raise PostfillStateContractError(
            "public complement full-fill proxy before synthetic cancel "
            "application must terminate as CANCEL_RACE_PAIR"
        )
    if (
        terminal_type == "CANCEL_RACE_PAIR"
        and not public_proxy_pair_before_synthetic_cancel
    ):
        raise PostfillStateContractError(
            "CANCEL_RACE_PAIR cannot be invented without a strict "
            "pre-synthetic-cancel public complement full-fill proxy"
        )
    if terminal_type == "CANCEL_RACE_PAIR":
        _required(
            row,
            (
                "race_complement_price_e4",
                "race_complement_fee_usd",
                "race_complement_stable_source_id",
                "race_fill_provenance",
                "race_execution_nature",
                "race_fee_provenance",
            ),
            "cancel-race outcome",
        )
        race_price = _plain_int(
            row["race_complement_price_e4"],
            "race_complement_price_e4",
        )
        race_fee = _exact_decimal(
            row["race_complement_fee_usd"],
            "race_complement_fee_usd",
        )
        if (
            not 0 < race_price < 10_000
            or race_price != episode["complement_price_e4"]
            or race_fee != 0
            or row["race_fill_provenance"] != FILL_PROVENANCE
            or row["race_execution_nature"] != EXECUTION_NATURE
            or row["race_fee_provenance"] != MAKER_FEE_PROVENANCE
        ):
            raise PostfillStateContractError(
                "cancel-race public proxy/schedule-zero provenance is "
                "invalid"
            )
        if (
            row["complement_fill_qty_fp"]
            != row["requested_qty_fp"]
            or row["residual_inventory_fp"] != 0
        ):
            raise PostfillStateContractError(
                "partial public proxy complement fill before synthetic "
                "cancel is "
                "unsupported and cannot be labeled CANCEL_RACE_PAIR"
            )
        if (
            terminal_elapsed != episode["terminal_elapsed_ms"]
            or race_price
            != episode["terminal_complement_fill_price_e4"]
            or race_fee != episode["terminal_complement_fill_fee_usd"]
            or row["complement_fill_qty_fp"]
            != episode["terminal_complement_fill_qty_fp"]
            or row["race_complement_stable_source_id"]
            != episode["terminal_complement_fill_stable_source_id"]
        ):
            raise PostfillStateContractError(
                "cancel-race outcome does not match the public "
                "complement full-fill proxy"
            )
        row["race_complement_stable_source_id"] = _nonempty_text(
            row["race_complement_stable_source_id"],
            "race_complement_stable_source_id",
        )
        if not (
            Decimal(decision_elapsed)
            < terminal_elapsed
            < Decimal(planned_effective)
        ):
            raise PostfillStateContractError(
                "cancel-race proxy fill must be strictly before "
                "synthetic cancel application"
            )
        if (
            fok_sent
            or synthetic_cancel_applied
            or no_self_cross_before_fok is not None
            or row.get("synthetic_cancel_applied_wall_ns") is not None
            or row.get("synthetic_cancel_applied_mono_ns") is not None
            or row.get("fok_processed_wall_ns") is not None
            or row.get("fok_processed_mono_ns") is not None
            or row.get("synthetic_cancel_applied_sequence") is not None
            or row.get(
                "synthetic_cancel_applied_stable_source_id"
            )
            is not None
            or row.get("fok_processed_sequence") is not None
            or row.get("fok_processed_stable_source_id") is not None
            or slices
            or row["fok_fill_qty_fp"] != 0
        ):
            raise PostfillStateContractError(
                "cancel-race must pair before synthetic cancel "
                "application and suppress FOK"
            )
        expected_gross = (
            Decimal(10_000)
            - Decimal(episode["first_fill_price_e4"])
            - Decimal(race_price)
        ) / E4 * row["requested_qty_fp"]
        expected_maker_fee = first_fee + race_fee
        expected_taker = (Decimal("0"),) * 3
        expected_conservative = expected_gross - expected_maker_fee
        expected_capital = (
            basis_with_fee * terminal_elapsed / Decimal("1000")
        )
        if not (
            simulation_reward_exact
            and reward_kind == "SIMULATION_EXACT"
            and fit_usable
            and capital_released
        ):
            raise PostfillStateContractError(
                "cancel-race simulation-exact reward flags are invalid"
            )
    else:
        if any(row[field] is not None for field in optional_protocol_fields):
            raise PostfillStateContractError(
                "non-race FOK outcome cannot carry race provenance"
            )
        effective_ns = planned_effective * 1_000_000
        effective_wall = (
            episode["first_fill_recv_wall_ns"] + effective_ns
        )
        effective_mono = (
            episode["first_fill_recv_mono_ns"] + effective_ns
        )
        for field, expected in (
            ("synthetic_cancel_applied_wall_ns", effective_wall),
            ("synthetic_cancel_applied_mono_ns", effective_mono),
            ("fok_processed_wall_ns", effective_wall),
            ("fok_processed_mono_ns", effective_mono),
        ):
            if _plain_int(row.get(field), field, minimum=1) != expected:
                raise PostfillStateContractError(
                    "synthetic cancel/FOK counterfactual clock is not "
                    "the sealed effective time"
                )
        if (
            _plain_int(
                row.get("synthetic_cancel_applied_sequence"),
                "synthetic_cancel_applied_sequence",
                minimum=0,
            )
            != 0
            or _plain_int(
                row.get("fok_processed_sequence"),
                "fok_processed_sequence",
                minimum=0,
            )
            != 1
        ):
            raise PostfillStateContractError(
                "same-time ordering must apply synthetic cancel before "
                "the FOK counterfactual"
            )
        row["synthetic_cancel_applied_stable_source_id"] = _nonempty_text(
            row.get("synthetic_cancel_applied_stable_source_id"),
            "synthetic_cancel_applied_stable_source_id",
        )
        row["fok_processed_stable_source_id"] = _nonempty_text(
            row.get("fok_processed_stable_source_id"),
            "fok_processed_stable_source_id",
        )
        if (
            terminal_elapsed != Decimal(planned_effective)
            or terminal_wall != effective_wall
            or terminal_mono != effective_mono
            or not fok_sent
            or not synthetic_cancel_applied
        ):
            raise PostfillStateContractError(
                "FOK counterfactual sequencing/clock mismatch"
            )
        if no_self_cross_before_fok is not True:
            _rollback(
                (str(state["source_date_utc"]),),
                SELF_CROSS_INVALID_TERMINAL,
                "simulated strategy-order registry cannot prove no "
                "self-cross at the FOK counterfactual",
            )
        expected_maker_fee = first_fee
        if terminal_type == "FOK_FULL":
            if (
                row["complement_fill_qty_fp"] != 0
                or row["fok_fill_qty_fp"]
                != row["requested_qty_fp"]
                or row["residual_inventory_fp"] != 0
            ):
                raise PostfillStateContractError(
                    "FOK_FULL is not a complete atomic fill"
                )
            expected_gross = economics["gross"]
            expected_conservative = (
                expected_gross
                - expected_maker_fee
                - economics["fee_high"]
            )
            expected_capital = (
                basis_with_fee
                * terminal_elapsed
                / Decimal("1000")
            )
            if not (
                not simulation_reward_exact
                and reward_kind == "FEE_BAND"
                and fit_usable
                and capital_released
            ):
                raise PostfillStateContractError(
                    "FOK_FULL fee-band reward flags are invalid"
                )
        else:
            if (
                slices
                or row["complement_fill_qty_fp"] != 0
                or row["fok_fill_qty_fp"] != 0
                or row["residual_inventory_fp"]
                != row["requested_qty_fp"]
            ):
                raise PostfillStateContractError(
                    "FOK_ZERO must fill zero and retain all inventory"
                )
            expected_gross = Decimal("0")
            expected_taker = (Decimal("0"),) * 3
            expected_conservative = -basis_with_fee
            expected_capital = (
                basis_with_fee
                * episode["market_close_elapsed_ms"]
                / Decimal("1000")
            )
            if not (
                not simulation_reward_exact
                and reward_kind == "LOWER_BOUND"
                and fit_usable
                and not capital_released
            ):
                raise PostfillStateContractError(
                    "FOK_ZERO lower-bound reward flags are invalid"
                )
    if row["gross_pnl_usd"] != expected_gross:
        raise PostfillStateContractError("outcome gross PnL mismatch")
    if row["maker_fee_usd"] != expected_maker_fee:
        raise PostfillStateContractError("outcome maker fee mismatch")
    for field, expected in zip(
        (
            "taker_fee_low_usd",
            "taker_fee_base_usd",
            "taker_fee_high_usd",
        ),
        expected_taker,
    ):
        if row[field] != expected:
            raise PostfillStateContractError(
                f"{field} does not match fee precision band"
            )
    for net_field, fee_field in (
        ("net_pnl_fee_low_usd", "taker_fee_low_usd"),
        ("net_pnl_fee_base_usd", "taker_fee_base_usd"),
        ("net_pnl_fee_high_usd", "taker_fee_high_usd"),
    ):
        if row[net_field] != (
            row["gross_pnl_usd"]
            - row["maker_fee_usd"]
            - row[fee_field]
        ):
            raise PostfillStateContractError(
                f"{net_field} fee-band reconciliation failed"
            )
    if not (
        row["net_pnl_fee_low_usd"]
        >= row["net_pnl_fee_base_usd"]
        >= row["net_pnl_fee_high_usd"]
    ):
        raise PostfillStateContractError(
            "outcome net PnL fee band is not monotone"
        )
    if (
        row["conservative_reward_usd"] != expected_conservative
        or row["capital_dollar_seconds"] != expected_capital
    ):
        raise PostfillStateContractError(
            "conservative reward/capital accounting mismatch"
        )
    row.update(
        {
            "source_date_utc": _iso_day(row["source_date_utc"]),
            "market_ticker": episode["market_ticker"],
            "first_fill_price_e4": episode["first_fill_price_e4"],
            "first_fill_qty_fp": episode["first_fill_qty_fp"],
            "first_fill_fee_usd": episode["first_fill_fee_usd"],
            "first_leg_cost_basis_usd": episode[
                "first_leg_cost_basis_usd"
            ],
            "original_complement_price_e4": episode[
                "complement_price_e4"
            ],
            "market_close_elapsed_ms": episode[
                "market_close_elapsed_ms"
            ],
            "terminal_type": terminal_type,
            "planned_effective_elapsed_ms": planned_effective,
            "terminal_elapsed_ms": terminal_elapsed,
            "terminal_wall_ns": terminal_wall,
            "terminal_mono_ns": terminal_mono,
            "feature_asof_wall_ns": feature_wall,
            "source_max_recv_wall_ns": source_wall,
            "source_max_recv_mono_ns": source_mono,
            "fok_fill_slices": slices,
            "execution_slices_sha256": _slices_sha256(slices),
            "reward_value_kind": reward_kind,
        }
    )
    return row


def _serialize_fok_outcome(
    outcome: Mapping[str, object],
) -> dict[str, object]:
    omitted = {"fok_fill_slices"}
    return {
        key: value
        for key, value in outcome.items()
        if key not in omitted
    }


def _build_keep_transitions(
    *,
    episode: Mapping[str, object],
    states_by_grid: Mapping[int, Mapping[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    required = tuple(episode["required_grid"])
    terminal = episode["terminal_elapsed_ms"]
    basis_with_fee = (
        episode["first_leg_cost_basis_usd"]
        + episode["first_fill_fee_usd"]
    )
    for index, elapsed in required:
        has_next_grid = index + 1 < len(DECISION_GRID_MS)
        next_index = index + 1
        next_elapsed = (
            DECISION_GRID_MS[next_index]
            if has_next_grid
            else None
        )
        if (
            next_elapsed is not None
            and Decimal(next_elapsed) < terminal
        ):
            if next_index not in states_by_grid:
                raise PostfillStateContractError(
                    "KEEP next-state transition lacks causal state"
            )
            stop = Decimal(next_elapsed)
            transition_type = "NEXT_STATE"
            keep_terminal_type = None
            next_decision_id = _decision_id(
                episode["postfill_episode_id"],
                next_index,
            )
            gross = Decimal("0")
            exit_fee = Decimal("0")
            maker_fee = Decimal("0")
            net = Decimal("0")
            terminal_execution_provenance = None
            terminal_execution_nature = None
            terminal_fee_provenance = None
            terminal_complement_price_e4 = None
            terminal_complement_fill_qty_fp = None
            official_market_result = None
            official_result_provenance = None
            terminal_stable_source_id = None
            source_sha = states_by_grid[next_index][
                "causal_source_rows_sha256"
            ]
        else:
            stop = terminal
            transition_type = "KEEP_TO_TERMINAL"
            keep_terminal_type = str(episode["terminal_type"])
            next_decision_id = None
            if keep_terminal_type == "COMPLEMENT_FILL":
                gross = (
                    Decimal(10_000)
                    - Decimal(episode["first_fill_price_e4"])
                    - Decimal(
                        episode["terminal_complement_fill_price_e4"]
                    )
                ) / E4 * episode["first_fill_qty_fp"]
                maker_fee = (
                    episode["first_fill_fee_usd"]
                    + episode["terminal_complement_fill_fee_usd"]
                )
                exit_fee = episode[
                    "terminal_complement_fill_fee_usd"
                ]
                net = gross - maker_fee
                terminal_execution_provenance = episode[
                    "terminal_complement_fill_provenance"
                ]
                terminal_execution_nature = episode[
                    "terminal_complement_execution_nature"
                ]
                terminal_fee_provenance = episode[
                    "terminal_complement_fee_provenance"
                ]
                terminal_complement_price_e4 = episode[
                    "terminal_complement_fill_price_e4"
                ]
                terminal_complement_fill_qty_fp = episode[
                    "terminal_complement_fill_qty_fp"
                ]
                official_market_result = None
                official_result_provenance = None
                terminal_stable_source_id = episode[
                    "terminal_complement_fill_stable_source_id"
                ]
            else:
                gross = episode["terminal_keep_gross_pnl_usd"]
                exit_fee = episode["terminal_keep_exit_fee_usd"]
                maker_fee = episode["terminal_keep_maker_fee_usd"]
                net = episode["terminal_keep_net_pnl_usd"]
                terminal_execution_provenance = episode[
                    "terminal_keep_reward_provenance"
                ]
                terminal_execution_nature = EXECUTION_NATURE
                terminal_fee_provenance = episode[
                    "terminal_keep_fee_provenance"
                ]
                terminal_complement_price_e4 = None
                terminal_complement_fill_qty_fp = None
                official_market_result = episode[
                    "official_market_result"
                ]
                official_result_provenance = episode[
                    "official_result_provenance"
                ]
                terminal_stable_source_id = episode[
                    "terminal_keep_stable_source_id"
                ]
            source_sha = episode["source_rows_sha256"]
        start = Decimal(elapsed)
        start_ns = _elapsed_ns(start)
        stop_ns = _elapsed_ns(stop)
        keep_action_id = _action_id(
            episode["postfill_episode_id"],
            index,
            "KEEP",
        )
        rows.append(
            {
                "postfill_keep_transition_id": (
                    f"{keep_action_id}::TRANSITION"
                ),
                "keep_action_id": keep_action_id,
                "from_decision_id": _decision_id(
                    episode["postfill_episode_id"],
                    index,
                ),
                "postfill_episode_id": episode[
                    "postfill_episode_id"
                ],
                "source_date_utc": episode["source_date_utc"],
                "market_ticker": episode["market_ticker"],
                "data_origin": episode["data_origin"],
                "first_fill_provenance": episode[
                    "first_fill_provenance"
                ],
                "first_fill_execution_nature": episode[
                    "first_fill_execution_nature"
                ],
                "first_fill_fee_provenance": episode[
                    "first_fill_fee_provenance"
                ],
                "keep_execution_provenance": KEEP_EXECUTION_PROVENANCE,
                "from_decision_index": index,
                "interval_start_elapsed_ms": start,
                "interval_stop_elapsed_ms": stop,
                "interval_start_wall_ns": (
                    episode["first_fill_recv_wall_ns"] + start_ns
                ),
                "interval_start_mono_ns": (
                    episode["first_fill_recv_mono_ns"] + start_ns
                ),
                "interval_stop_wall_ns": (
                    episode["first_fill_recv_wall_ns"] + stop_ns
                ),
                "interval_stop_mono_ns": (
                    episode["first_fill_recv_mono_ns"] + stop_ns
                ),
                "transition_type": transition_type,
                "keep_terminal_type": keep_terminal_type,
                "terminal_execution_provenance": (
                    terminal_execution_provenance
                ),
                "terminal_execution_nature": terminal_execution_nature,
                "terminal_fee_provenance": terminal_fee_provenance,
                "terminal_stable_source_id": terminal_stable_source_id,
                "next_decision_id": next_decision_id,
                "market_close_elapsed_ms": episode[
                    "market_close_elapsed_ms"
                ],
                "first_side": episode["first_fill_side"],
                "first_fill_price_e4": episode[
                    "first_fill_price_e4"
                ],
                "first_fill_qty_fp": episode["first_fill_qty_fp"],
                "first_fill_fee_usd": episode["first_fill_fee_usd"],
                "first_leg_cost_basis_usd": episode[
                    "first_leg_cost_basis_usd"
                ],
                "original_complement_price_e4": episode[
                    "complement_price_e4"
                ],
                "terminal_complement_price_e4": (
                    terminal_complement_price_e4
                ),
                "terminal_complement_fill_qty_fp": (
                    terminal_complement_fill_qty_fp
                ),
                "official_market_result": official_market_result,
                "official_result_provenance": (
                    official_result_provenance
                ),
                "exit_fee_usd": exit_fee,
                "immediate_gross_pnl_usd": gross,
                "maker_fee_usd": maker_fee,
                "immediate_net_pnl_usd": net,
                "simulation_reward_exact": True,
                "reward_value_kind": "SIMULATION_EXACT",
                "capital_dollar_seconds_increment": (
                    basis_with_fee
                    * (stop - start)
                    / Decimal("1000")
                ),
                "source_rows_sha256": source_sha,
                "reconciliation_ok": True,
            }
        )
    return rows


def _validate_episode_sequence(
    states_by_grid: Mapping[int, Mapping[str, object]],
    episode: Mapping[str, object],
) -> None:
    previous: Mapping[str, object] | None = None
    for index, _grid_ms in episode["required_grid"]:
        state = states_by_grid[index]
        if previous is not None:
            if (
                state["feature_asof_wall_ns"]
                < previous["feature_asof_wall_ns"]
                or state["source_max_recv_wall_ns"]
                < previous["source_max_recv_wall_ns"]
                or state["source_max_recv_mono_ns"]
                < previous["source_max_recv_mono_ns"]
            ):
                raise PostfillStateContractError(
                    "causal receipt as-of regresses across decision grid"
                )
            if (
                state["remaining_inventory_fp"]
                != previous["remaining_inventory_fp"]
            ):
                raise PostfillStateContractError(
                    "unidentified partial inventory drift across "
                    "decision grid"
                )
        previous = state


def validate_and_serialize_postfill_rows(
    *,
    logical_source_paths: Mapping[
        object, Sequence[os.PathLike[str] | str]
    ],
    expected_episode_ids_by_day: Mapping[object, Sequence[str]],
    episode_manifest: Iterable[Mapping[str, object]],
    compact_rows: Iterable[Mapping[str, object]],
    flatten_fok_outcomes: Iterable[Mapping[str, object]],
) -> PostfillDDLBatch:
    """Validate a full market-day batch and serialize it atomically.

    Path preflight is intentionally executed before either input iterable is
    consumed.  Any subsequent violation raises
    :class:`MarketDayRollbackRequired`; no partially populated batch is
    returned.
    """
    expected, guarded = preflight_canary_postfill_paths(
        logical_source_paths,
        expected_episode_ids_by_day,
    )
    expected_days = tuple(sorted(expected))
    try:
        raw_episodes = tuple(episode_manifest)
        raw_compact = tuple(compact_rows)
        raw_outcomes = tuple(flatten_fok_outcomes)
    except Exception as exc:
        _rollback(
            expected_days,
            "INPUT_ITERATION_FAILED",
            str(exc),
        )

    episodes: dict[str, dict[str, object]] = {}
    atom_rows: list[dict[str, object]] = []
    try:
        for raw in raw_episodes:
            episode, atom = _validate_episode(raw, expected_days)
            episode_id = str(episode["postfill_episode_id"])
            if episode_id in episodes:
                raise PostfillStateContractError(
                    f"duplicate episode manifest id {episode_id}"
                )
            episodes[episode_id] = episode
            if atom is not None:
                atom_rows.append(atom)
        for day, expected_ids in expected.items():
            actual_ids = sorted(
                episode_id
                for episode_id, episode in episodes.items()
                if episode["source_date_utc"] == day
            )
            if actual_ids != list(expected_ids):
                raise MarketDayRollbackRequired(
                    (day,),
                    "EPISODE_ROSTER_MISMATCH",
                    f"expected={list(expected_ids)} actual={actual_ids}",
                )
    except MarketDayRollbackRequired:
        raise
    except (PostfillStateContractError, ForbiddenSourceError) as exc:
        days = _row_day(
            raw if "raw" in locals() else {},
            expected_days,
        )
        _rollback(
            days,
            "EPISODE_MANIFEST_INVALID",
            str(exc),
        )

    paired: dict[
        tuple[str, int],
        dict[str, dict[str, object]],
    ] = {}
    try:
        for raw in raw_compact:
            raw_episode_id = str(raw.get("postfill_episode_id") or "")
            episode = episodes.get(raw_episode_id)
            if episode is None:
                raise PostfillStateContractError(
                    f"compact row has no episode manifest: {raw_episode_id}"
                )
            if episode["zero_time_atom"]:
                raise PostfillStateContractError(
                    "zero-time atom cannot enter continuous decision rows"
                )
            state = _normalize_state(raw, episode)
            key = (raw_episode_id, int(state["decision_index"]))
            by_action = paired.setdefault(key, {})
            kind = str(state["action_kind"])
            if kind in by_action:
                raise PostfillStateContractError(
                    f"duplicate {kind} row for {key}"
                )
            by_action[kind] = state
    except MarketDayRollbackRequired:
        raise
    except (PostfillStateContractError, ForbiddenSourceError) as exc:
        days = _row_day(
            raw if "raw" in locals() else {},
            expected_days,
        )
        _rollback(days, "COMPACT_ROW_INVALID", str(exc))

    state_rows: list[dict[str, object]] = []
    action_rows: list[dict[str, object]] = []
    keep_transition_rows: list[dict[str, object]] = []
    normalized_states: dict[
        tuple[str, int], Mapping[str, object]
    ] = {}
    try:
        for episode_id, episode in sorted(episodes.items()):
            if episode["zero_time_atom"]:
                continue
            required = tuple(episode["required_grid"])
            actual_indices = sorted(
                index
                for candidate_episode, index in paired
                if candidate_episode == episode_id
            )
            required_indices = [index for index, _ in required]
            if actual_indices != required_indices:
                raise PostfillStateContractError(
                    f"{episode_id}: decision-grid coverage mismatch "
                    f"required={required_indices} actual={actual_indices}"
                )
            states_by_grid: dict[int, Mapping[str, object]] = {}
            for index, _elapsed in required:
                actions = paired[(episode_id, index)]
                if set(actions) != ALLOWED_ACTIONS:
                    raise PostfillStateContractError(
                        f"{episode_id}/{index}: "
                        "KEEP/FLATTEN_FOK pair missing"
                    )
                keep = actions["KEEP"]
                flatten = actions["FLATTEN_FOK"]
                if _state_signature(keep) != _state_signature(flatten):
                    raise PostfillStateContractError(
                        f"{episode_id}/{index}: paired causal state drift"
                    )
                fingerprint = _state_fingerprint(keep)
                states_by_grid[index] = keep
                normalized_states[(episode_id, index)] = keep
                state_rows.append(
                    _serialize_state(keep, episode, fingerprint)
                )
                action_rows.extend(
                    (
                        _serialize_action(keep, fingerprint),
                        _serialize_action(flatten, fingerprint),
                    )
                )
            _validate_episode_sequence(states_by_grid, episode)
            keep_transition_rows.extend(
                _build_keep_transitions(
                    episode=episode,
                    states_by_grid=states_by_grid,
                )
            )
    except PostfillStateContractError as exc:
        failed_day = (
            str(episode["source_date_utc"])
            if "episode" in locals()
            else ""
        )
        _rollback(
            (failed_day,) if failed_day else expected_days,
            "FULL_COVERAGE_CONSERVATION_FAILED",
            str(exc),
        )

    outcome_rows: list[dict[str, object]] = []
    seen_outcomes: set[tuple[str, int]] = set()
    try:
        for raw in raw_outcomes:
            episode_id = str(raw.get("postfill_episode_id") or "")
            raw_index = raw.get("decision_index")
            if type(raw_index) is not int:
                raise PostfillStateContractError(
                    "FLATTEN_FOK outcome decision_index must be integer"
                )
            key = (episode_id, raw_index)
            state = normalized_states.get(key)
            episode = episodes.get(episode_id)
            if state is None or episode is None:
                raise PostfillStateContractError(
                    f"FLATTEN_FOK outcome has no causal state: {key}"
                )
            if key in seen_outcomes:
                raise PostfillStateContractError(
                    f"duplicate FLATTEN_FOK outcome: {key}"
                )
            seen_outcomes.add(key)
            outcome_rows.append(
                _serialize_fok_outcome(
                    _normalize_fok_outcome(
                        raw,
                        state=state,
                        episode=episode,
                    )
                )
            )
        required_outcomes = set(normalized_states)
        if seen_outcomes != required_outcomes:
            missing = sorted(required_outcomes - seen_outcomes)
            extra = sorted(seen_outcomes - required_outcomes)
            raise PostfillStateContractError(
                "FOK_ZERO/full/race outcomes cannot be dropped: "
                f"missing={missing} extra={extra}"
            )
    except MarketDayRollbackRequired:
        raise
    except (PostfillStateContractError, ForbiddenSourceError) as exc:
        days = _row_day(
            raw if "raw" in locals() else {},
            expected_days,
        )
        _rollback(days, "FLATTEN_FOK_OUTCOME_INVALID", str(exc))

    guarded_text = tuple(
        sorted(
            os.fspath(path)
            for paths in guarded.values()
            for path in paths
        )
    )
    return PostfillDDLBatch(
        source_dates=expected_days,
        guarded_source_paths=guarded_text,
        episode_count=len(episodes),
        postfill_compact_causal_state=tuple(state_rows),
        postfill_compact_causal_action=tuple(action_rows),
        postfill_compact_keep_transition=tuple(
            keep_transition_rows
        ),
        postfill_compact_flatten_fok_outcome=tuple(
            sorted(
                outcome_rows,
                key=lambda row: (
                    str(row["postfill_episode_id"]),
                    int(row["decision_index"]),
                ),
            )
        ),
        postfill_zero_time_atom=tuple(
            sorted(
                atom_rows,
                key=lambda row: str(row["postfill_episode_id"]),
            )
        ),
    )


def create_postfill_contract_schema(
    connection: object,
    *,
    base_ddl_path: os.PathLike[str] | str | None = None,
    supplemental_ddl_path: os.PathLike[str] | str = (
        DEFAULT_SUPPLEMENTAL_DDL_PATH
    ),
) -> tuple[Path | None, Path]:
    """Create the base ROUND4 schema plus the supplemental staging tables.

    This helper only executes caller-selected local DDL.  It never reads
    market data.  ``base_ddl_path=None`` is useful when the base schema is
    already present.
    """
    base_path = Path(base_ddl_path) if base_ddl_path is not None else None
    if base_path is not None:
        connection.execute(base_path.read_text())
    supplemental = Path(supplemental_ddl_path)
    connection.execute(supplemental.read_text())
    return base_path, supplemental
