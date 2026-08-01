#!/usr/bin/env python3
"""Fail-closed ROUND4 Stage-1 entry-roster contract.

This module is intentionally strategy-free.  It validates and serializes the
complete denominator needed before an entry policy can be evaluated:

* every eligible entry decision is exactly ``ENTRY_SKIP`` or
  ``ENTRY_PAIR_POST_ONLY``;
* a paired decision is exactly ``ACK_FAILED`` before risk, or enters the
  first-fill competing-risk set after both ACKs;
* every admitted pair terminates as ``YES_FIRST``, ``NO_FIRST`` or
  ``ADMIN_CENSOR_NO_FIRST_FILL``;
* Stage-1 intervals cover the complete admitted lifetime without overlap,
  lookahead or a fabricated epsilon interval;
* reservation capital is integrated from explicit capital segments.

The historical A1-skip replay is a shadow simulator and does not contain wire
ACK receipts.  A replay may label its admitted rows
``SHADOW_IMMEDIATE_BOTH_ACKED`` and report zero historically identified
``ACK_FAILED`` rows, but the contract retains the latter as an explicit
pre-risk terminal for future shadow/live capture and synthetic tests.

No function in this file reads market data, fits a model, selects a candidate,
or authorizes deployment/live trading.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any

from tools.research.crypto_mm.round4_table_builder import (
    EXPERIMENT_ID,
    Round4ContractError,
    Round4TableBuilder,
)


CANDIDATE_STATUS = "ACTION_SET_PENDING"
DEPLOYABLE = False
LIVE_AUTHORIZED = False
DISCOVERY_DATES = frozenset(
    ("2026-07-20", "2026-07-21", "2026-07-22")
)
FORBIDDEN_DATES = frozenset(("2026-07-23", "2026-07-26"))
ENTRY_TIME_BINS_MS = (
    Decimal("0"),
    Decimal("1000"),
    Decimal("2000"),
    Decimal("5000"),
    Decimal("15000"),
    Decimal("30000"),
    Decimal("60000"),
    Decimal("120000"),
    Decimal("300000"),
)
E4 = Decimal("10000")
NS_PER_SECOND = Decimal("1000000000")
MONEY_QUANTUM = Decimal("0.00000001")
SECONDS_QUANTUM = Decimal("0.000000001")

PAIR_TERMINALS = frozenset(
    (
        "YES_FIRST",
        "NO_FIRST",
        "ADMIN_CENSOR_NO_FIRST_FILL",
        "ACK_FAILED",
    )
)
RISK_TERMINALS = frozenset(
    ("YES_FIRST", "NO_FIRST", "ADMIN_CENSOR_NO_FIRST_FILL")
)
OUTCOME_TERMINALS = frozenset((*PAIR_TERMINALS, "ENTRY_SKIP"))

RISK_FEATURES = (
    "yes_same_price_ahead_fp",
    "no_same_price_ahead_fp",
    "yes_better_depth_fp",
    "no_better_depth_fp",
    "yes_flow_10s_fp",
    "no_flow_10s_fp",
    "yes_flow_60s_fp",
    "no_flow_60s_fp",
    "touch_imbalance",
    "spread_e4",
    "mid_move_1s_e4",
    "mid_move_10s_e4",
    "tte_ms",
)


class EntryStage1ContractError(RuntimeError):
    """A Stage-1 row or roster violates the pre-data contract."""


def _plain_int(value: object, label: str, *, positive: bool = False) -> int:
    if type(value) is not int:
        raise EntryStage1ContractError(f"{label}: expected plain integer")
    if positive and value <= 0:
        raise EntryStage1ContractError(f"{label}: expected positive integer")
    return value


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, float):
        raise EntryStage1ContractError(
            f"{label}: float is forbidden for exact money/quantity"
        )
    if isinstance(value, bool):
        raise EntryStage1ContractError(f"{label}: bool is not a decimal")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EntryStage1ContractError(
            f"{label}: invalid exact decimal {value!r}"
        ) from exc
    if not result.is_finite():
        raise EntryStage1ContractError(f"{label}: non-finite decimal")
    return result


def _money_text(value: Decimal) -> str:
    return format(value.quantize(MONEY_QUANTUM), "f")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(
                value.items(),
                key=lambda pair: str(pair[0]),
            )
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        _jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _cause_flags(cause: str) -> tuple[int, int, int]:
    try:
        return {
            "YES_FIRST": (1, 0, 0),
            "NO_FIRST": (0, 1, 0),
            "ADMIN_CENSOR_NO_FIRST_FILL": (0, 0, 1),
        }[cause]
    except KeyError as exc:
        raise EntryStage1ContractError(
            f"terminal-cause {cause!r} is not in the Stage-1 risk set"
        ) from exc


def _validate_nullable_touch_bundle(
    row: Mapping[str, object],
    label: str,
) -> None:
    """Keep missing public-touch state explicit and non-imputed.

    ``spread_e4 IS NULL`` is the Stage-1 risk-row indicator that a causal
    two-sided public touch was unavailable.  Every feature that requires that
    touch must then also be NULL.  With a spread, imbalance is required;
    lagged midpoint moves may still be NULL when their causal lag is absent.
    """
    spread = row.get("spread_e4")
    touch = row.get("touch_imbalance")
    mid1 = row.get("mid_move_1s_e4")
    mid10 = row.get("mid_move_10s_e4")
    if spread is None:
        if any(value is not None for value in (touch, mid1, mid10)):
            raise EntryStage1ContractError(
                f"{label}: spread NULL requires touch/mid features NULL"
            )
    elif touch is None:
        raise EntryStage1ContractError(
            f"{label}: non-NULL spread requires touch imbalance"
        )


def _episode_key(row: Mapping[str, object]) -> tuple[str, str]:
    return (
        str(row.get("entry_episode_id")),
        str(row.get("entry_action_id")),
    )


def _validate_discovery_date(value: object) -> str:
    day = str(value)
    if day in FORBIDDEN_DATES:
        raise EntryStage1ContractError(f"forbidden Stage-1 date: {day}")
    if day not in DISCOVERY_DATES:
        raise EntryStage1ContractError(
            f"date outside Stage-1 discovery allowlist: {day}"
        )
    return day


def build_stage1_risk_intervals(
    episode: Mapping[str, object],
    states_by_elapsed_start_ms: Mapping[object, Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Build fixed-bin Stage-1 rows using a causal state at every bin start.

    ``states_by_elapsed_start_ms`` must contain the last fully applied state
    at or before each required interval start.  A zero-duration first event is
    rejected rather than turned into an epsilon interval.
    """
    row = dict(episode)
    if row.get("ack_state") != "BOTH_ACKED":
        raise EntryStage1ContractError(
            "Stage-1 risk starts only after both entry ACKs"
        )
    cause = str(row.get("terminal_cause"))
    if cause not in RISK_TERMINALS:
        raise EntryStage1ContractError(
            f"terminal-cause {cause!r} cannot enter Stage-1 risk"
        )
    active = _plain_int(
        row.get("entry_active_wall_ns"),
        "entry_active_wall_ns",
        positive=True,
    )
    terminal = _plain_int(
        row.get("terminal_wall_ns"),
        "terminal_wall_ns",
        positive=True,
    )
    if terminal == active:
        raise EntryStage1ContractError(
            "zero-duration Stage-1 event cannot be fabricated as epsilon"
        )
    if terminal < active:
        raise EntryStage1ContractError("entry terminal precedes active time")
    duration_ms = (
        Decimal(terminal - active) / Decimal("1000000")
    )
    if duration_ms > ENTRY_TIME_BINS_MS[-1]:
        raise EntryStage1ContractError(
            "Stage-1 entry lifetime exceeds sealed 300-second horizon"
        )
    states = {
        _decimal(key, "state elapsed_start_ms"): dict(value)
        for key, value in states_by_elapsed_start_ms.items()
    }
    rows: list[dict[str, object]] = []
    for index, (left, right) in enumerate(
        zip(ENTRY_TIME_BINS_MS, ENTRY_TIME_BINS_MS[1:])
    ):
        if left >= duration_ms:
            break
        state = states.get(left)
        if state is None:
            raise EntryStage1ContractError(
                f"missing causal Stage-1 state at elapsed_ms={left}"
            )
        start_ns = active + int(left * Decimal("1000000"))
        stop_elapsed = min(right, duration_ms)
        stop_ns = active + int(stop_elapsed * Decimal("1000000"))
        asof = _plain_int(
            state.get("feature_asof_wall_ns"),
            "feature_asof_wall_ns",
            positive=True,
        )
        if asof > start_ns:
            raise EntryStage1ContractError(
                "Stage-1 feature lookahead at interval start"
            )
        _validate_nullable_touch_bundle(
            state,
            f"Stage-1 state elapsed_ms={left}",
        )
        flags = (
            _cause_flags(cause)
            if stop_elapsed == duration_ms
            else (0, 0, 0)
        )
        interval: dict[str, object] = {
            "entry_episode_id": str(row["entry_episode_id"]),
            "entry_action_id": str(row["entry_action_id"]),
            "interval_index": index,
            "interval_start_wall_ns": start_ns,
            "interval_stop_wall_ns": stop_ns,
            "feature_asof_wall_ns": asof,
            "elapsed_start_ms": left,
            "elapsed_stop_ms": stop_elapsed,
            "at_risk": True,
            "event_yes_first": flags[0],
            "event_no_first": flags[1],
            "admin_censor": flags[2],
            "data_invalid": 0,
            "yes_order_age_ms": int(left),
            "no_order_age_ms": int(left),
        }
        for name in RISK_FEATURES:
            if name in state:
                interval[name] = state[name]
        rows.append(interval)
    if not rows:
        raise EntryStage1ContractError("Stage-1 episode has no risk interval")
    return tuple(rows)


def build_capital_interval(
    episode: Mapping[str, object],
    segment_index: int,
    locked_capital_usd: object,
    *,
    start_wall_ns: int | None = None,
    stop_wall_ns: int | None = None,
    component_reason: str = "TWO_RESTING_ENTRY_RESERVATIONS",
) -> dict[str, object]:
    """Create one exactly integrated capital segment."""
    locked = _decimal(locked_capital_usd, "locked_capital_usd")
    if locked < 0:
        raise EntryStage1ContractError("negative locked capital")
    start_raw = (
        episode.get("entry_active_wall_ns")
        if start_wall_ns is None
        else start_wall_ns
    )
    stop_raw = (
        episode.get("terminal_wall_ns")
        if stop_wall_ns is None
        else stop_wall_ns
    )
    start = _plain_int(start_raw, "capital segment start", positive=True)
    stop = _plain_int(stop_raw, "capital segment stop", positive=True)
    if stop <= start:
        raise EntryStage1ContractError(
            "capital interval must have positive duration"
        )
    index = _plain_int(segment_index, "segment_index")
    if index < 0:
        raise EntryStage1ContractError("negative capital segment index")
    dollar_seconds = (
        locked * Decimal(stop - start) / NS_PER_SECOND
    )
    return {
        "entry_episode_id": str(episode["entry_episode_id"]),
        "entry_action_id": str(episode["entry_action_id"]),
        "segment_index": index,
        "segment_start_wall_ns": start,
        "segment_stop_wall_ns": stop,
        "locked_capital_usd": locked,
        "capital_dollar_seconds": dollar_seconds,
        "component_reason": component_reason,
    }


@dataclass(frozen=True)
class EntryStage1DDLBatch:
    """Validated rows ready for the base and supplemental ROUND4 DDL."""

    entry_episodes: tuple[dict[str, object], ...]
    entry_risk_intervals: tuple[dict[str, object], ...]
    entry_capital_intervals: tuple[dict[str, object], ...]
    entry_stage1_outcomes: tuple[dict[str, object], ...]
    receipt: dict[str, object]

    def as_ddl_rows(self) -> dict[str, tuple[dict[str, object], ...]]:
        return {
            "entry_episode": self.entry_episodes,
            "entry_risk_interval": self.entry_risk_intervals,
            "entry_capital_interval": self.entry_capital_intervals,
            "entry_stage1_outcome": self.entry_stage1_outcomes,
        }

    def canonical_sha256(self) -> str:
        return _canonical_sha256(self.as_ddl_rows())


def _validate_risk_coverage(
    episode: Mapping[str, object],
    risk_rows: Sequence[Mapping[str, object]],
) -> None:
    active = int(episode["entry_active_wall_ns"])
    terminal = int(episode["terminal_wall_ns"])
    rows = sorted(risk_rows, key=lambda item: int(item["interval_index"]))
    if not rows:
        raise EntryStage1ContractError(
            "admitted entry has no Stage-1 risk intervals"
        )
    if [int(item["interval_index"]) for item in rows] != list(
        range(len(rows))
    ):
        raise EntryStage1ContractError("Stage-1 risk interval index gap")
    if (
        int(rows[0]["interval_start_wall_ns"]) != active
        or int(rows[-1]["interval_stop_wall_ns"]) != terminal
        or _decimal(rows[0]["elapsed_start_ms"], "elapsed_start_ms") != 0
    ):
        raise EntryStage1ContractError(
            "Stage-1 risk intervals do not cover admitted lifetime"
        )
    for index, item in enumerate(rows):
        start = int(item["interval_start_wall_ns"])
        stop = int(item["interval_stop_wall_ns"])
        asof = int(item["feature_asof_wall_ns"])
        if stop <= start or asof > start:
            raise EntryStage1ContractError(
                "invalid or future-as-of Stage-1 risk interval"
            )
        _validate_nullable_touch_bundle(
            item,
            f"Stage-1 risk interval {index}",
        )
        flags = sum(
            int(item[name])
            for name in (
                "event_yes_first",
                "event_no_first",
                "admin_censor",
                "data_invalid",
            )
        )
        if item["data_invalid"] != 0:
            raise EntryStage1ContractError(
                "DATA_INVALID requires whole market-day rollback"
            )
        if index < len(rows) - 1 and flags:
            raise EntryStage1ContractError(
                "Stage-1 terminal flag before final interval"
            )
        if index and (
            int(rows[index - 1]["interval_stop_wall_ns"]) != start
            or _decimal(
                rows[index - 1]["elapsed_stop_ms"],
                "elapsed_stop_ms",
            )
            != _decimal(item["elapsed_start_ms"], "elapsed_start_ms")
        ):
            raise EntryStage1ContractError(
                "Stage-1 risk intervals overlap or leave a gap"
            )
    observed = tuple(
        sum(int(item[name]) for item in rows)
        for name in (
            "event_yes_first",
            "event_no_first",
            "admin_censor",
        )
    )
    if observed != _cause_flags(str(episode["terminal_cause"])):
        raise EntryStage1ContractError(
            "Stage-1 terminal-cause conservation failed"
        )


def _validate_capital(
    episode: Mapping[str, object],
    outcome: Mapping[str, object],
    capital_rows: Sequence[Mapping[str, object]],
) -> None:
    rows = sorted(capital_rows, key=lambda item: int(item["segment_index"]))
    expected_total = _decimal(
        outcome.get("capital_dollar_seconds"),
        "outcome capital_dollar_seconds",
    )
    expected_peak = _decimal(
        outcome.get("peak_episode_capital_usd"),
        "outcome peak_episode_capital_usd",
    )
    total = Decimal("0")
    peak = Decimal("0")
    for index, item in enumerate(rows):
        if int(item["segment_index"]) != index:
            raise EntryStage1ContractError("capital segment index gap")
        start = _plain_int(
            item.get("segment_start_wall_ns"),
            "capital segment start",
            positive=True,
        )
        stop = _plain_int(
            item.get("segment_stop_wall_ns"),
            "capital segment stop",
            positive=True,
        )
        locked = _decimal(
            item.get("locked_capital_usd"),
            "locked_capital_usd",
        )
        dollar_seconds = _decimal(
            item.get("capital_dollar_seconds"),
            "capital_dollar_seconds",
        )
        if locked < 0 or stop <= start:
            raise EntryStage1ContractError("invalid capital segment")
        recomputed = locked * Decimal(stop - start) / NS_PER_SECOND
        if dollar_seconds != recomputed:
            raise EntryStage1ContractError(
                "capital-dollar-seconds segment does not reconcile"
            )
        if index and int(rows[index - 1]["segment_stop_wall_ns"]) != start:
            raise EntryStage1ContractError(
                "capital segments overlap or leave a gap"
            )
        total += dollar_seconds
        peak = max(peak, locked)
    if total != expected_total or peak != expected_peak:
        raise EntryStage1ContractError(
            "capital ledger does not reconcile to Stage-1 outcome"
        )
    admitted = episode["ack_state"] == "BOTH_ACKED"
    if admitted:
        if not rows:
            raise EntryStage1ContractError(
                "admitted entry is missing capital occupancy"
            )
        if (
            int(rows[0]["segment_start_wall_ns"])
            != int(episode["entry_active_wall_ns"])
            or int(rows[-1]["segment_stop_wall_ns"])
            != int(episode["terminal_wall_ns"])
        ):
            raise EntryStage1ContractError(
                "capital segments do not cover admitted entry lifetime"
            )
        clip = _decimal(episode["clip_fp"], "clip_fp")
        pair_cost = Decimal(int(episode["pair_cost_e4"])) / E4
        expected_locked = pair_cost * clip
        if any(
            _decimal(item["locked_capital_usd"], "locked capital")
            != expected_locked
            for item in rows
        ):
            raise EntryStage1ContractError(
                "admitted entry reservation differs from pair cost"
            )
        duration_s = (
            Decimal(
                int(episode["terminal_wall_ns"])
                - int(episode["entry_active_wall_ns"])
            )
            / NS_PER_SECOND
        )
        quote_seconds = _decimal(
            outcome.get("entry_quote_seconds"),
            "entry_quote_seconds",
        )
        if quote_seconds != duration_s:
            raise EntryStage1ContractError(
                "entry quote seconds do not conserve admitted lifetime"
            )
    elif episode["terminal_cause"] == "ENTRY_SKIP" and rows:
        raise EntryStage1ContractError("ENTRY_SKIP cannot occupy capital")


def validate_and_serialize_stage1_rows(
    entry_episodes: Sequence[Mapping[str, object]],
    entry_risk_intervals: Sequence[Mapping[str, object]],
    entry_capital_intervals: Sequence[Mapping[str, object]],
    entry_stage1_outcomes: Sequence[Mapping[str, object]],
    *,
    expected_first_fill_ids: set[str] | frozenset[str] | None = None,
    source_audit: Mapping[str, object],
) -> EntryStage1DDLBatch:
    """Validate a complete Stage-1 decision roster and return a DDL batch."""
    if int(source_audit.get("data_invalid_market_days", 0)) != 0:
        raise EntryStage1ContractError(
            "DATA_INVALID requires whole market-day/all-action rollback"
        )
    episodes = tuple(dict(item) for item in entry_episodes)
    risks = tuple(dict(item) for item in entry_risk_intervals)
    capital = tuple(dict(item) for item in entry_capital_intervals)
    outcomes = tuple(dict(item) for item in entry_stage1_outcomes)
    if not episodes:
        raise EntryStage1ContractError("empty Stage-1 entry roster")

    builder = Round4TableBuilder()
    episode_by_key: dict[tuple[str, str], dict[str, object]] = {}
    try:
        for episode in episodes:
            _validate_discovery_date(episode.get("source_date_utc"))
            if episode.get("terminal_cause") == "DATA_INVALID":
                raise EntryStage1ContractError(
                    "DATA_INVALID requires whole market-day rollback"
                )
            key = _episode_key(episode)
            if key in episode_by_key:
                raise EntryStage1ContractError(
                    f"duplicate Stage-1 entry key: {key}"
                )
            builder.add_entry_episode(episode)
            episode_by_key[key] = episode
        for risk in risks:
            builder.add_entry_risk_interval(risk)
    except Round4ContractError as exc:
        raise EntryStage1ContractError(str(exc)) from exc

    risks_by_key: dict[
        tuple[str, str], list[dict[str, object]]
    ] = defaultdict(list)
    for risk in risks:
        risks_by_key[_episode_key(risk)].append(risk)
    capital_by_key: dict[
        tuple[str, str], list[dict[str, object]]
    ] = defaultdict(list)
    capital_primary_keys: set[tuple[str, str, int]] = set()
    for item in capital:
        key = _episode_key(item)
        if key not in episode_by_key:
            raise EntryStage1ContractError(
                "capital interval has no entry parent"
            )
        primary = (*key, int(item.get("segment_index", -1)))
        if primary in capital_primary_keys:
            raise EntryStage1ContractError(
                f"duplicate capital interval primary key: {primary}"
            )
        capital_primary_keys.add(primary)
        capital_by_key[key].append(item)

    outcome_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for outcome in outcomes:
        key = _episode_key(outcome)
        if key not in episode_by_key:
            raise EntryStage1ContractError(
                "Stage-1 outcome has no entry parent"
            )
        if key in outcome_by_key:
            raise EntryStage1ContractError(
                f"duplicate Stage-1 outcome key: {key}"
            )
        if str(outcome.get("terminal_type")) not in OUTCOME_TERMINALS:
            raise EntryStage1ContractError(
                "unknown Stage-1 outcome terminal"
            )
        outcome_by_key[key] = outcome
    if set(outcome_by_key) != set(episode_by_key):
        raise EntryStage1ContractError(
            "every entry decision must have exactly one Stage-1 outcome"
        )

    first_fill_ids: set[str] = set()
    counts = Counter()
    for key, episode in episode_by_key.items():
        cause = str(episode["terminal_cause"])
        outcome = outcome_by_key[key]
        if str(outcome["terminal_type"]) != cause:
            raise EntryStage1ContractError(
                "entry and Stage-1 outcome terminal mismatch"
            )
        if outcome.get("reconciliation_ok") is not True:
            raise EntryStage1ContractError(
                "Stage-1 outcome is not reconciled"
            )
        pair = episode["action_kind"] == "ENTRY_PAIR_POST_ONLY"
        admitted = episode["ack_state"] == "BOTH_ACKED"
        if bool(outcome.get("admitted")) != admitted:
            raise EntryStage1ContractError(
                "Stage-1 admitted flag disagrees with ACK state"
            )
        if not pair:
            counts["entry_skip"] += 1
            if cause != "ENTRY_SKIP" or risks_by_key.get(key):
                raise EntryStage1ContractError(
                    "ENTRY_SKIP leaked into the Stage-1 risk set"
                )
        elif cause == "ACK_FAILED":
            counts["ack_failed"] += 1
            if admitted or risks_by_key.get(key):
                raise EntryStage1ContractError(
                    "ACK_FAILED must terminate before Stage-1 risk"
                )
        elif admitted:
            counts["admitted"] += 1
            _validate_risk_coverage(episode, risks_by_key.get(key, []))
            if cause in ("YES_FIRST", "NO_FIRST"):
                counts["first_fill"] += 1
                if outcome.get("strict_trade_through_verified") is not True:
                    raise EntryStage1ContractError(
                        "first fill lacks strict trade-through proof"
                    )
                first_id = outcome.get("first_fill_episode_id")
                if not isinstance(first_id, str) or not first_id:
                    raise EntryStage1ContractError(
                        "first fill lacks a post-fill episode identity"
                    )
                if first_id in first_fill_ids:
                    raise EntryStage1ContractError(
                        "duplicate first-fill episode identity"
                    )
                first_fill_ids.add(first_id)
            else:
                counts["no_first_fill"] += 1
                if outcome.get("first_fill_episode_id") is not None:
                    raise EntryStage1ContractError(
                        "no-first-fill row carries first-fill identity"
                    )
        else:
            raise EntryStage1ContractError(
                "paired entry has inconsistent ACK/terminal state"
            )
        _validate_capital(
            episode,
            outcome,
            capital_by_key.get(key, []),
        )

    if expected_first_fill_ids is not None and first_fill_ids != set(
        expected_first_fill_ids
    ):
        raise EntryStage1ContractError(
            "Stage-1 first-fill identities do not exactly match sealed roster"
        )

    pair_decisions = sum(
        episode["action_kind"] == "ENTRY_PAIR_POST_ONLY"
        for episode in episodes
    )
    skip_decisions = len(episodes) - pair_decisions
    source_expected = {
        "eligible_entry_decisions": len(episodes),
        "pair_post_only_decisions": pair_decisions,
        "entry_skip_decisions": skip_decisions,
        "both_acked": counts["admitted"],
        "ack_failed": counts["ack_failed"],
    }
    for name, expected in source_expected.items():
        actual = _plain_int(source_audit.get(name), f"source audit {name}")
        if actual != expected:
            raise EntryStage1ContractError(
                f"source decision roster mismatch for {name}: "
                f"actual={actual} expected={expected}"
            )
    if counts["first_fill"] + counts["no_first_fill"] != counts["admitted"]:
        raise EntryStage1ContractError(
            "first-fill/no-first-fill denominator does not partition admitted"
        )

    total_capital = sum(
        (
            _decimal(
                outcome["capital_dollar_seconds"],
                "capital_dollar_seconds",
            )
            for outcome in outcomes
        ),
        Decimal("0"),
    )
    dates = sorted(
        {str(episode["source_date_utc"]) for episode in episodes}
    )
    receipt = {
        "schema": "round4-stage1-entry-contract-receipt-v1",
        "experiment_id": EXPERIMENT_ID,
        "candidate_status": CANDIDATE_STATUS,
        "candidate": False,
        "deployable": DEPLOYABLE,
        "live_authorized": LIVE_AUTHORIZED,
        "dates": dates,
        "date_2026_07_23_read": False,
        "date_2026_07_26_read": False,
        "entry_decisions": len(episodes),
        "pair_post_only_decisions": pair_decisions,
        "entry_skip_entries": counts["entry_skip"],
        "ack_failed_entries": counts["ack_failed"],
        "admitted_entries": counts["admitted"],
        "first_fill_entries": counts["first_fill"],
        "no_first_fill_entries": counts["no_first_fill"],
        "first_fill_identity_count": len(first_fill_ids),
        "entry_risk_interval_rows": len(risks),
        "entry_capital_interval_rows": len(capital),
        "capital_dollar_seconds": _money_text(total_capital),
        "roster_partition_exact": True,
        "admitted_denominator_includes_no_first_fill": True,
        "risk_terminal_conservation": True,
        "strict_trade_through_all_first_fills": True,
        "nonoverlap_checked": True,
        "whole_market_day_rollback_required": True,
        "historical_ack_failure_identified": (
            counts["ack_failed"] > 0
            and any(
                outcome.get("ack_observation_kind")
                == "OBSERVED_WIRE_ACK"
                for outcome in outcomes
                if outcome["terminal_type"] == "ACK_FAILED"
            )
        ),
        "source_audit": dict(source_expected),
    }
    batch = EntryStage1DDLBatch(
        entry_episodes=tuple(
            sorted(episodes, key=lambda item: _episode_key(item))
        ),
        entry_risk_intervals=tuple(
            sorted(
                risks,
                key=lambda item: (
                    *_episode_key(item),
                    int(item["interval_index"]),
                ),
            )
        ),
        entry_capital_intervals=tuple(
            sorted(
                capital,
                key=lambda item: (
                    *_episode_key(item),
                    int(item["segment_index"]),
                ),
            )
        ),
        entry_stage1_outcomes=tuple(
            sorted(outcomes, key=lambda item: _episode_key(item))
        ),
        receipt=receipt,
    )
    receipt["canonical_rows_sha256"] = batch.canonical_sha256()
    return batch
