#!/usr/bin/env python3
"""Pure-synthetic ROUND4 two-stage hazard-model scaffold.

The module implements only the preregistered, auditable model family:

* Stage 1: pooled side-oriented first-fill competing risks.
* Stage 2: conditional complement-fill vs inventory-exit competing risks.
* Piecewise-exponential Poisson likelihood with ridge in {0.1, 1, 10}.
* Leave-one-discovery-date-out selection and cross-fitted calibration.
* Aalen-Johansen sanity curves and exact economic aggregation.

There is intentionally no file reader, model serializer, candidate ranking,
network client or live path.  Inputs must carry the explicit pure-synthetic
marker and logical paths pass the ROUND4 before-open guard.  Results always
remain ``ACTION_SET_PENDING / NO_CANDIDATE``.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
import re

import numpy as np

from tools.research.crypto_mm.round4_table_builder import (
    CANDIDATE_STATUS,
    DISCOVERY_DATES,
    EXPERIMENT_ID,
    ForbiddenSourceError,
    Round4ContractError,
    preflight_source_paths,
)


SYNTHETIC_MARKER = "ROUND4_PURE_SYNTHETIC_V1"
CLAIM = "NO_CANDIDATE"
RIDGE_GRID = (0.1, 1.0, 10.0)
ENTRY_BINS_S = (0.0, 1.0, 2.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0)
POSTFILL_BINS_S = (0.0, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
STAGE1_HORIZONS_S = (1.0, 5.0, 30.0, 60.0, 300.0)
STAGE2_HORIZONS_S = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)

STAGE1_FEATURES = (
    "same_price_ahead_fp",
    "better_depth_fp",
    "flow_10s_fp",
    "flow_60s_fp",
    "flow_acceleration_fp",
    "touch_imbalance_oriented",
    "spread_e4",
    "mid_move_1s_oriented_e4",
    "mid_move_10s_oriented_e4",
    "tte_ms",
    "order_age_ms",
)
STAGE2_FEATURES = (
    "complement_queue_position_fp",
    "complement_same_price_ahead_fp",
    "complement_better_depth_fp",
    "complement_flow_10s_fp",
    "complement_flow_60s_fp",
    "complement_flow_acceleration_fp",
    "spread_e4",
    "mid_move_since_fill_oriented_e4",
    "buy_complement_exit_pnl_usd",
    "sell_first_exit_pnl_usd",
    "pair_gain_if_complement_usd",
    "tte_ms",
    "complement_order_age_ms",
    "first_fill_elapsed_ms",
    "first_price_e4",
    "first_qty_fp",
)


class SyntheticModelContractError(Round4ContractError):
    """Synthetic model input violates the sealed ROUND4 contract."""


@dataclass(frozen=True)
class SyntheticRound4Dataset:
    """In-memory inputs; logical paths are checked but never opened."""

    marker: str
    logical_source_paths: Mapping[str, Sequence[str]]
    stage1_intervals: Sequence[Mapping[str, object]]
    stage2_intervals: Sequence[Mapping[str, object]]
    cycle_outcomes: Sequence[Mapping[str, object]]
    stage2_atom_labels: Sequence[Mapping[str, object]] = ()


@dataclass(frozen=True)
class RobustScaler:
    feature_names: tuple[str, ...]
    median: np.ndarray
    iqr: np.ndarray
    lower: np.ndarray
    upper: np.ndarray

    @classmethod
    def fit(
        cls,
        observations: Sequence[Mapping[str, object]],
        feature_names: Sequence[str],
    ) -> "RobustScaler":
        names = tuple(feature_names)
        if not names:
            empty = np.empty(0, dtype=float)
            return cls(names, empty, empty, empty, empty)
        matrix = np.asarray(
            [
                [_finite_float(row["features"][name], name) for name in names]
                for row in observations
            ],
            dtype=float,
        )
        if matrix.ndim != 2 or matrix.shape[0] == 0:
            raise SyntheticModelContractError("empty scaler fit")
        lower = np.quantile(matrix, 0.01, axis=0)
        upper = np.quantile(matrix, 0.99, axis=0)
        clipped = np.clip(matrix, lower, upper)
        median = np.median(clipped, axis=0)
        q25 = np.quantile(clipped, 0.25, axis=0)
        q75 = np.quantile(clipped, 0.75, axis=0)
        iqr = q75 - q25
        iqr = np.where(iqr > 1e-12, iqr, 1.0)
        return cls(
            names,
            median,
            iqr,
            lower,
            upper,
        )

    def transform(self, features: Mapping[str, object]) -> np.ndarray:
        if not self.feature_names:
            return np.empty(0, dtype=float)
        raw = np.asarray(
            [
                _finite_float(features[name], name)
                for name in self.feature_names
            ],
            dtype=float,
        )
        return (np.clip(raw, self.lower, self.upper) - self.median) / self.iqr

    def receipt(self) -> dict[str, object]:
        return {
            "method": "discovery median/IQR after 1%/99% clipping",
            "feature_names": self.feature_names,
            "median": self.median.tolist(),
            "iqr": self.iqr.tolist(),
            "lower_1pct": self.lower.tolist(),
            "upper_99pct": self.upper.tolist(),
        }


@dataclass
class PiecewiseExponentialRidge:
    """Poisson-offset representation of a piecewise exponential hazard."""

    bins_s: tuple[float, ...]
    feature_names: tuple[str, ...]
    category_names: tuple[str, ...]
    alpha: float
    scaler: RobustScaler
    coefficients: np.ndarray
    converged: bool
    iterations: int

    @property
    def bin_count(self) -> int:
        return len(self.bins_s) - 1

    def _vector(self, observation: Mapping[str, object]) -> np.ndarray:
        index = int(observation["bin_index"])
        if not 0 <= index < self.bin_count:
            raise SyntheticModelContractError("invalid prediction time bin")
        result = np.zeros(
            self.bin_count
            + len(self.category_names)
            + len(self.feature_names),
            dtype=float,
        )
        result[index] = 1.0
        offset = self.bin_count
        for position, name in enumerate(self.category_names):
            value = _finite_float(observation["categories"][name], name)
            if value not in (0.0, 1.0):
                raise SyntheticModelContractError(
                    f"non-binary category {name}: {value}"
                )
            result[offset + position] = value
        result[offset + len(self.category_names) :] = self.scaler.transform(
            observation["features"]
        )
        return result

    def predict_hazard(self, observation: Mapping[str, object]) -> float:
        eta = float(self._vector(observation) @ self.coefficients)
        return float(math.exp(min(20.0, max(-30.0, eta))))

    def rcll(self, observations: Sequence[Mapping[str, object]]) -> float:
        value = 0.0
        for observation in observations:
            hazard = self.predict_hazard(observation)
            event = int(observation["event"])
            exposure = _positive_float(
                observation["exposure_s"],
                "exposure_s",
            )
            value += event * math.log(hazard) - exposure * hazard
        return value

    def receipt(self) -> dict[str, object]:
        return {
            "family": "cause-specific piecewise exponential ridge",
            "bins_s": self.bins_s,
            "alpha": self.alpha,
            "feature_names": self.feature_names,
            "category_names": self.category_names,
            "coefficients": self.coefficients.tolist(),
            "converged": self.converged,
            "iterations": self.iterations,
            "scaler": self.scaler.receipt(),
            "synthetic_only": True,
        }


@dataclass
class SyntheticFitBundle:
    stage1_model: PiecewiseExponentialRidge
    stage2_complement_model: PiecewiseExponentialRidge
    stage2_exit_model: PiecewiseExponentialRidge
    zero_time_atom_model: ZeroTimeAtomModel
    report: dict[str, object]


@dataclass(frozen=True)
class ZeroTimeAtomModel:
    """Jeffreys-smoothed atom mass, separate from continuous time."""

    probability_by_first_side: Mapping[str, float]
    global_probability: float

    def predict(self, row: Mapping[str, object]) -> float:
        side = str(row.get("first_fill_side"))
        if side not in ("YES", "NO"):
            raise SyntheticModelContractError(
                "zero-time atom row has invalid first_fill_side"
            )
        return float(
            self.probability_by_first_side.get(
                side,
                self.global_probability,
            )
        )

    def receipt(self) -> dict[str, object]:
        return {
            "family": "Jeffreys-smoothed zero-time Bernoulli atom",
            "probability_by_first_side": dict(
                self.probability_by_first_side
            ),
            "global_probability": self.global_probability,
            "continuous_hazard": False,
            "epsilon_interval_used": False,
            "synthetic_only": True,
        }


@dataclass(frozen=True)
class EtaMechanicalContract:
    """Explicit queue/rate semantics required by the ETA baseline."""

    queue_ahead_field: str
    consumption_rate_field: str
    clip_fp: float
    consumption_sign: int
    rate_unit: str

    def __post_init__(self) -> None:
        if (
            not self.queue_ahead_field
            or not self.consumption_rate_field
            or self.clip_fp <= 0
            or self.consumption_sign not in (-1, 1)
            or self.rate_unit != "contracts_per_second"
        ):
            raise SyntheticModelContractError(
                "ETA requires explicit queue, positive clip, sign and "
                "contracts_per_second rate unit"
            )

    def completion_probability(
        self,
        row: Mapping[str, object],
        horizon_s: float,
    ) -> float:
        ahead = _finite_float(
            row[self.queue_ahead_field],
            self.queue_ahead_field,
        )
        signed_rate = _finite_float(
            row[self.consumption_rate_field],
            self.consumption_rate_field,
        )
        if ahead < 0 or horizon_s < 0:
            raise SyntheticModelContractError("negative ETA queue/horizon")
        rate = self.consumption_sign * signed_rate
        if rate <= 0:
            return 0.0
        eta_s = (ahead + self.clip_fp) / rate
        return float(1.0 - math.exp(-horizon_s / eta_s))

    def receipt(self) -> dict[str, object]:
        return {
            "queue_ahead_field": self.queue_ahead_field,
            "consumption_rate_field": self.consumption_rate_field,
            "clip_fp": self.clip_fp,
            "consumption_sign": self.consumption_sign,
            "rate_unit": self.rate_unit,
        }


def _finite_float(value: object, label: str) -> float:
    if value is None or isinstance(value, bool):
        raise SyntheticModelContractError(f"{label}: missing/non-numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SyntheticModelContractError(
            f"{label}: non-numeric {value!r}"
        ) from exc
    if not math.isfinite(result):
        raise SyntheticModelContractError(f"{label}: non-finite")
    return result


def _positive_float(value: object, label: str) -> float:
    result = _finite_float(value, label)
    if result <= 0:
        raise SyntheticModelContractError(f"{label}: must be positive")
    return result


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, float) or isinstance(value, bool):
        raise SyntheticModelContractError(
            f"{label}: accounting floats are forbidden"
        )
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SyntheticModelContractError(
            f"{label}: invalid decimal"
        ) from exc
    if not result.is_finite():
        raise SyntheticModelContractError(f"{label}: non-finite decimal")
    return result


def _row_date(row: Mapping[str, object]) -> str:
    day = str(row.get("source_date_utc"))
    if day not in DISCOVERY_DATES:
        raise ForbiddenSourceError(
            f"model row outside discovery dates: {day}"
        )
    if row.get("data_origin") != "SYNTHETIC":
        raise SyntheticModelContractError(
            "every model row must be explicitly SYNTHETIC"
        )
    return day


def validate_synthetic_dataset(dataset: SyntheticRound4Dataset) -> dict[str, object]:
    """Validate logical paths and in-memory provenance without opening files."""
    if dataset.marker != SYNTHETIC_MARKER:
        raise SyntheticModelContractError("pure-synthetic marker missing")
    path_dates = set(dataset.logical_source_paths)
    if path_dates != set(DISCOVERY_DATES):
        raise ForbiddenSourceError(
            "logical paths must cover exactly the three discovery dates"
        )
    path_count = 0
    for day in sorted(DISCOVERY_DATES):
        guarded = preflight_source_paths(
            dataset.logical_source_paths[day],
            day,
            data_role="DISCOVERY",
        )
        path_count += len(guarded)
    tables = (
        ("stage1_intervals", dataset.stage1_intervals),
        ("stage2_intervals", dataset.stage2_intervals),
        ("stage2_atom_labels", dataset.stage2_atom_labels),
        ("cycle_outcomes", dataset.cycle_outcomes),
    )
    date_coverage = {}
    for label, rows in tables:
        if not rows:
            raise SyntheticModelContractError(f"{label}: empty")
        dates = {_row_date(row) for row in rows}
        if dates != set(DISCOVERY_DATES):
            raise SyntheticModelContractError(
                f"{label}: all discovery dates required, got {sorted(dates)}"
            )
        date_coverage[label] = sorted(dates)
    return {
        "status": "SYNTHETIC_PATHS_VALIDATED_NOT_OPENED",
        "path_count": path_count,
        "dates": sorted(DISCOVERY_DATES),
        "date_coverage": date_coverage,
    }


def _time_bin(
    elapsed_start_s: float,
    elapsed_stop_s: float,
    bins_s: Sequence[float],
) -> int:
    if elapsed_start_s < 0 or elapsed_stop_s <= elapsed_start_s:
        raise SyntheticModelContractError("invalid elapsed interval")
    for index, (left, right) in enumerate(zip(bins_s, bins_s[1:])):
        if left - 1e-12 <= elapsed_start_s < right - 1e-12:
            if elapsed_stop_s > right + 1e-9:
                raise SyntheticModelContractError(
                    "risk interval crosses a sealed time-bin boundary"
                )
            return index
    raise SyntheticModelContractError("elapsed interval outside sealed bins")


def _validate_interval_clock(row: Mapping[str, object], prefix: str) -> tuple[float, float]:
    required = (
        "interval_start_wall_ns",
        "interval_stop_wall_ns",
        "feature_asof_wall_ns",
        "elapsed_start_ms",
        "elapsed_stop_ms",
        "at_risk",
    )
    missing = [field for field in required if row.get(field) is None]
    if missing:
        raise SyntheticModelContractError(
            f"{prefix}: missing DDL interval fields {missing}"
        )
    if row["at_risk"] is not True:
        raise SyntheticModelContractError(
            f"{prefix}: fitter accepts at-risk rows only"
        )
    start_ms = _finite_float(row["elapsed_start_ms"], "elapsed_start_ms")
    stop_ms = _finite_float(row["elapsed_stop_ms"], "elapsed_stop_ms")
    start_wall = int(row["interval_start_wall_ns"])
    stop_wall = int(row["interval_stop_wall_ns"])
    if int(row["feature_asof_wall_ns"]) > start_wall:
        raise SyntheticModelContractError(f"{prefix}: feature lookahead")
    absolute_s = (stop_wall - start_wall) / 1_000_000_000
    elapsed_s = (stop_ms - start_ms) / 1_000
    if abs(absolute_s - elapsed_s) > 1e-9:
        raise SyntheticModelContractError(
            f"{prefix}: receipt/elapsed clock mismatch"
        )
    return start_ms / 1_000, stop_ms / 1_000


def _stage1_features(row: Mapping[str, object], side: str) -> dict[str, float]:
    if side not in ("yes", "no"):
        raise SyntheticModelContractError("invalid target side")
    sign = 1.0 if side == "yes" else -1.0
    flow10 = _finite_float(row[f"{side}_flow_10s_fp"], "flow_10s")
    flow60 = _finite_float(row[f"{side}_flow_60s_fp"], "flow_60s")
    return {
        "same_price_ahead_fp": _finite_float(
            row[f"{side}_same_price_ahead_fp"],
            "same_price_ahead_fp",
        ),
        "better_depth_fp": _finite_float(
            row[f"{side}_better_depth_fp"],
            "better_depth_fp",
        ),
        "flow_10s_fp": flow10,
        "flow_60s_fp": flow60,
        "flow_acceleration_fp": flow10 - flow60 / 6.0,
        "touch_imbalance_oriented": sign
        * _finite_float(row["touch_imbalance"], "touch_imbalance"),
        "spread_e4": _finite_float(row["spread_e4"], "spread_e4"),
        "mid_move_1s_oriented_e4": sign
        * _finite_float(row["mid_move_1s_e4"], "mid_move_1s_e4"),
        "mid_move_10s_oriented_e4": sign
        * _finite_float(row["mid_move_10s_e4"], "mid_move_10s_e4"),
        "tte_ms": _finite_float(row["tte_ms"], "tte_ms"),
        "order_age_ms": _finite_float(
            row[f"{side}_order_age_ms"],
            "order_age_ms",
        ),
    }


def _stage2_features(row: Mapping[str, object]) -> dict[str, float]:
    first_side = row.get("first_fill_side")
    if first_side not in ("YES", "NO"):
        raise SyntheticModelContractError("invalid first_fill_side")
    complement_sign = -1.0 if first_side == "YES" else 1.0
    flow10 = _finite_float(
        row["complement_flow_10s_fp"],
        "complement_flow_10s_fp",
    )
    flow60 = _finite_float(
        row["complement_flow_60s_fp"],
        "complement_flow_60s_fp",
    )
    return {
        "complement_queue_position_fp": _finite_float(
            row["complement_queue_position_fp"],
            "complement_queue_position_fp",
        ),
        "complement_same_price_ahead_fp": _finite_float(
            row["complement_same_price_ahead_fp"],
            "complement_same_price_ahead_fp",
        ),
        "complement_better_depth_fp": _finite_float(
            row["complement_better_depth_fp"],
            "complement_better_depth_fp",
        ),
        "complement_flow_10s_fp": flow10,
        "complement_flow_60s_fp": flow60,
        "complement_flow_acceleration_fp": flow10 - flow60 / 6.0,
        "spread_e4": _finite_float(row["spread_e4"], "spread_e4"),
        "mid_move_since_fill_oriented_e4": complement_sign
        * _finite_float(
            row["mid_move_since_fill_e4"],
            "mid_move_since_fill_e4",
        ),
        "buy_complement_exit_pnl_usd": _finite_float(
            row["buy_complement_exit_pnl_usd"],
            "buy_complement_exit_pnl_usd",
        ),
        "sell_first_exit_pnl_usd": _finite_float(
            row["sell_first_exit_pnl_usd"],
            "sell_first_exit_pnl_usd",
        ),
        "pair_gain_if_complement_usd": _finite_float(
            row["pair_gain_if_complement_usd"],
            "pair_gain_if_complement_usd",
        ),
        "tte_ms": _finite_float(row["tte_ms"], "tte_ms"),
        "complement_order_age_ms": _finite_float(
            row["complement_order_age_ms"],
            "complement_order_age_ms",
        ),
        "first_fill_elapsed_ms": _finite_float(
            row["first_fill_elapsed_ms"],
            "first_fill_elapsed_ms",
        ),
        "first_price_e4": _finite_float(
            row["first_price_e4"],
            "first_price_e4",
        ),
        "first_qty_fp": _finite_float(row["first_qty_fp"], "first_qty_fp"),
    }


def validate_zero_time_atom_labels(
    labels: Sequence[Mapping[str, object]],
    continuous_rows: Sequence[Mapping[str, object]],
) -> tuple[dict[str, Mapping[str, object]], ...]:
    """Validate the first-fill denominator and atom/continuous exclusivity."""
    by_id: dict[str, Mapping[str, object]] = {}
    for row in labels:
        _row_date(row)
        postfill_id = str(row.get("postfill_episode_id") or "")
        if not postfill_id or postfill_id in by_id:
            raise SyntheticModelContractError(
                "duplicate/missing zero-time atom denominator key"
            )
        if any(
            field in row
            for field in (
                "elapsed_start_ms",
                "elapsed_stop_ms",
                "epsilon_ms",
            )
        ):
            raise SyntheticModelContractError(
                "zero-time atom cannot carry duration or epsilon fields"
            )
        atom = row.get("zero_time_atom")
        if type(atom) is not bool:
            raise SyntheticModelContractError(
                "zero_time_atom must be an explicit boolean"
            )
        first_side = row.get("first_fill_side")
        if first_side not in ("YES", "NO"):
            raise SyntheticModelContractError("invalid atom first side")
        wall = int(row.get("first_fill_recv_wall_ns") or 0)
        mono = int(row.get("first_fill_recv_mono_ns") or 0)
        asof = int(row.get("feature_asof_wall_ns") or 0)
        if wall <= 0 or mono <= 0 or asof <= 0 or asof > wall:
            raise SyntheticModelContractError(
                "invalid/no-lookahead zero-time atom receipt clock"
            )
        first_id = row.get("first_fill_stable_source_id")
        complement_id = row.get("complement_fill_stable_source_id")
        envelope = row.get("receipt_envelope_id")
        if atom:
            source_hash = str(row.get("source_rows_sha256") or "")
            complement_side = row.get("complement_side")
            price = int(row.get("complement_fill_price_e4") or 0)
            quantity = _finite_float(
                row.get("complement_fill_qty_fp"),
                "complement_fill_qty_fp",
            )
            fee = _finite_float(
                row.get("complement_fill_fee_usd"),
                "complement_fill_fee_usd",
            )
            if (
                not isinstance(first_id, str)
                or not isinstance(complement_id, str)
                or not isinstance(envelope, str)
                or not first_id
                or not complement_id
                or not envelope
                or first_id >= complement_id
                or complement_side
                != ("NO" if first_side == "YES" else "YES")
                or not 0 < price < 10_000
                or quantity <= 0
                or fee < 0
                or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None
                or not str(row.get("entry_episode_id") or "")
                or not str(row.get("entry_action_id") or "")
                or row.get("reconciliation_ok") is not True
            ):
                raise SyntheticModelContractError(
                    "atom positive lacks same-envelope stable ordering/"
                    "reconciliation"
                )
        elif (
            complement_id is not None
            or envelope is not None
            or row.get("complement_side") is not None
            or row.get("complement_fill_price_e4") is not None
            or row.get("complement_fill_qty_fp") is not None
            or row.get("complement_fill_fee_usd") is not None
        ):
            raise SyntheticModelContractError(
                "non-atom denominator row carries atom-only identity"
            )
        by_id[postfill_id] = row
    continuous_ids = set()
    for row in continuous_rows:
        postfill_id = str(row.get("postfill_episode_id") or "")
        if not postfill_id:
            raise SyntheticModelContractError(
                "continuous Stage-2 row lacks postfill_episode_id"
            )
        continuous_ids.add(postfill_id)
    if continuous_ids - set(by_id):
        raise SyntheticModelContractError(
            "continuous Stage-2 episode lacks atom denominator row"
        )
    positive_ids = {
        identifier
        for identifier, row in by_id.items()
        if row["zero_time_atom"] is True
    }
    negative_ids = set(by_id) - positive_ids
    if positive_ids & continuous_ids:
        raise SyntheticModelContractError(
            "zero-time atom was sent into the continuous hazard risk set"
        )
    if negative_ids != continuous_ids:
        raise SyntheticModelContractError(
            "non-atom denominator/continuous Stage-2 episode mismatch"
        )
    return tuple(by_id.values())


def fit_zero_time_atom_model(
    labels: Sequence[Mapping[str, object]],
) -> ZeroTimeAtomModel:
    counts = {}
    total = 0
    events = 0
    for row in labels:
        side = str(row["first_fill_side"])
        event = int(row["zero_time_atom"] is True)
        side_total, side_events = counts.get(side, (0, 0))
        counts[side] = (side_total + 1, side_events + event)
        total += 1
        events += event
    if total == 0:
        raise SyntheticModelContractError("empty zero-time atom model")
    return ZeroTimeAtomModel(
        {
            side: (side_events + 0.5) / (side_total + 1.0)
            for side, (side_total, side_events) in counts.items()
        },
        (events + 0.5) / (total + 1.0),
    )


def zero_time_atom_crossfit_report(
    labels: Sequence[Mapping[str, object]],
) -> tuple[ZeroTimeAtomModel, dict[str, object]]:
    predictions = []
    for heldout in sorted(DISCOVERY_DATES):
        train = [
            row for row in labels if row["source_date_utc"] != heldout
        ]
        test = [
            row for row in labels if row["source_date_utc"] == heldout
        ]
        model = fit_zero_time_atom_model(train)
        predictions.extend(
            (
                model.predict(row),
                int(row["zero_time_atom"] is True),
                str(row["first_fill_side"]),
            )
            for row in test
        )
    probabilities = np.asarray([row[0] for row in predictions], dtype=float)
    outcomes = np.asarray([row[1] for row in predictions], dtype=float)
    brier = float(np.mean((outcomes - probabilities) ** 2))
    calibration_rows = [
        {
            "terminal_time_s": 0.0,
            "terminal_type": (
                "ZERO_TIME_ATOM" if outcome else "NO_ATOM"
            ),
            "predictions": {
                0.0: {"ZERO_TIME_ATOM": probability}
            },
        }
        for probability, outcome, _side in predictions
    ]
    side_metrics = {}
    for side in ("YES", "NO"):
        subset = [row for row in predictions if row[2] == side]
        side_metrics[side] = {
            "n": len(subset),
            "events": sum(row[1] for row in subset),
            "brier": float(
                np.mean([(row[1] - row[0]) ** 2 for row in subset])
            ),
        }
    final_model = fit_zero_time_atom_model(labels)
    return final_model, {
        "object": "P(same-envelope complement atom | first-fill state)",
        "elapsed_time": 0,
        "epsilon_interval_used": False,
        "continuous_hazard_input": False,
        "crossfit_brier": brier,
        "crossfit_calibration": _calibration_metric(
            calibration_rows,
            horizon=0.0,
            positive_type="ZERO_TIME_ATOM",
            censor_types=set(),
        ),
        "crossfit_by_first_side": side_metrics,
        "final_synthetic_model": final_model.receipt(),
    }


def build_stage1_observations(
    rows: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    observations = []
    keys = set()
    for row in rows:
        day = _row_date(row)
        start_s, stop_s = _validate_interval_clock(row, "stage1")
        bin_index = _time_bin(start_s, stop_s, ENTRY_BINS_S)
        key = (
            str(row["entry_episode_id"]),
            str(row["entry_action_id"]),
            int(row["interval_index"]),
        )
        if key in keys:
            raise SyntheticModelContractError("duplicate stage1 interval PK")
        keys.add(key)
        flags = (
            int(row["event_yes_first"]),
            int(row["event_no_first"]),
            int(row["admin_censor"]),
            int(row["data_invalid"]),
        )
        if flags[3] != 0:
            raise SyntheticModelContractError(
                "DATA_INVALID cannot enter Stage-1 risk; rollback the "
                "entire market-day and every action upstream"
            )
        if any(value not in (0, 1) for value in flags) or sum(flags) > 1:
            raise SyntheticModelContractError(
                "overlapping stage1 terminal causes"
            )
        for side, event in (("yes", flags[0]), ("no", flags[1])):
            observations.append(
                {
                    "source_date_utc": day,
                    "episode_id": f"{key[0]}::{key[1]}",
                    "interval_index": key[2],
                    "bin_index": bin_index,
                    "exposure_s": stop_s - start_s,
                    "event": event,
                    "features": _stage1_features(row, side),
                    "categories": {"target_side_no": int(side == "no")},
                }
            )
    return tuple(observations)


def build_stage2_observations(
    rows: Sequence[Mapping[str, object]],
    event_field: str,
) -> tuple[dict[str, object], ...]:
    if event_field not in (
        "event_complement_fill",
        "event_inventory_exit",
    ):
        raise SyntheticModelContractError("unknown stage2 cause")
    observations = []
    keys = set()
    for row in rows:
        day = _row_date(row)
        action_kind = row.get("action_kind")
        if action_kind == "IOC":
            raise SyntheticModelContractError(
                "IOC is deterministic terminal execution, not a hazard row"
            )
        if action_kind not in ("KEEP", "REPRICE"):
            raise SyntheticModelContractError("invalid stage2 action family")
        start_s, stop_s = _validate_interval_clock(row, "stage2")
        bin_index = _time_bin(start_s, stop_s, POSTFILL_BINS_S)
        key = (str(row["postfill_action_id"]), int(row["interval_index"]))
        cause_key = key + (event_field,)
        if cause_key in keys:
            raise SyntheticModelContractError("duplicate stage2 interval PK")
        keys.add(cause_key)
        flags = (
            int(row["event_complement_fill"]),
            int(row["event_inventory_exit"]),
            int(row["admin_censor"]),
            int(row["data_invalid"]),
        )
        if flags[3] != 0:
            raise SyntheticModelContractError(
                "DATA_INVALID cannot enter Stage-2 risk; rollback the "
                "entire market-day and every action upstream"
            )
        if any(value not in (0, 1) for value in flags) or sum(flags) > 1:
            raise SyntheticModelContractError(
                "overlapping stage2 terminal causes"
            )
        observations.append(
            {
                "source_date_utc": day,
                "episode_id": key[0],
                "interval_index": key[1],
                "bin_index": bin_index,
                "exposure_s": stop_s - start_s,
                "event": int(row[event_field]),
                "features": _stage2_features(row),
                "categories": {
                    "first_side_no": int(row["first_fill_side"] == "NO"),
                    "action_reprice": int(action_kind == "REPRICE"),
                },
            }
        )
    return tuple(observations)


def fit_piecewise_ridge(
    observations: Sequence[Mapping[str, object]],
    *,
    bins_s: Sequence[float],
    feature_names: Sequence[str],
    category_names: Sequence[str],
    alpha: float,
    max_iterations: int = 100,
) -> PiecewiseExponentialRidge:
    if alpha not in RIDGE_GRID:
        raise SyntheticModelContractError(
            f"alpha outside sealed grid: {alpha}"
        )
    if not observations:
        raise SyntheticModelContractError("empty model fit")
    scaler = RobustScaler.fit(observations, feature_names)
    bin_count = len(bins_s) - 1
    width = bin_count + len(category_names) + len(feature_names)
    x = np.zeros((len(observations), width), dtype=float)
    y = np.zeros(len(observations), dtype=float)
    exposure = np.zeros(len(observations), dtype=float)
    prototype = PiecewiseExponentialRidge(
        tuple(bins_s),
        tuple(feature_names),
        tuple(category_names),
        float(alpha),
        scaler,
        np.zeros(width, dtype=float),
        False,
        0,
    )
    for index, observation in enumerate(observations):
        x[index] = prototype._vector(observation)
        event = int(observation["event"])
        if event not in (0, 1):
            raise SyntheticModelContractError("event must be integer 0/1")
        y[index] = event
        exposure[index] = _positive_float(
            observation["exposure_s"],
            "exposure_s",
        )
    if not np.all(np.isfinite(x)):
        raise SyntheticModelContractError("non-finite design matrix")
    # Penalize the complete linear predictor.  In particular, a time bin with
    # zero synthetic events otherwise has an unbounded MLE at -infinity.
    penalty = np.full(width, float(alpha), dtype=float)
    theta = np.zeros(width, dtype=float)
    for index in range(bin_count):
        mask = np.asarray(
            [int(row["bin_index"]) == index for row in observations]
        )
        events = float(y[mask].sum())
        time = float(exposure[mask].sum())
        theta[index] = math.log((events + 0.5) / (time + 0.5))

    def objective(candidate: np.ndarray) -> float:
        if not np.all(np.isfinite(candidate)):
            return float("-inf")
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            raw_eta = x @ candidate
            if not np.all(np.isfinite(raw_eta)):
                return float("-inf")
            eta = np.clip(raw_eta, -30.0, 20.0)
            value = float(
                np.sum(y * eta - exposure * np.exp(eta))
                - 0.5 * np.sum(penalty * candidate * candidate)
            )
        return value if math.isfinite(value) else float("-inf")

    converged = False
    iterations = 0
    for iterations in range(1, max_iterations + 1):
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            raw_eta = x @ theta
            if not np.all(np.isfinite(raw_eta)):
                raise SyntheticModelContractError(
                    "non-finite ridge linear predictor"
                )
            eta = np.clip(raw_eta, -30.0, 20.0)
            mean = exposure * np.exp(eta)
            gradient = x.T @ (y - mean) - penalty * theta
            information = x.T @ (mean[:, None] * x)
        if (
            not np.all(np.isfinite(mean))
            or not np.all(np.isfinite(gradient))
            or not np.all(np.isfinite(information))
        ):
            raise SyntheticModelContractError(
                "non-finite ridge gradient/information"
            )
        information += np.diag(penalty + 1e-9)
        try:
            step = np.linalg.solve(information, gradient)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(information, gradient, rcond=None)[0]
        max_step = float(np.max(np.abs(step)))
        if not math.isfinite(max_step):
            raise SyntheticModelContractError("non-finite ridge step")
        if max_step > 5.0:
            step *= 5.0 / max_step
        current = objective(theta)
        scale = 1.0
        accepted = False
        while scale >= 1.0 / 1024.0:
            candidate = theta + scale * step
            if objective(candidate) >= current - 1e-10:
                accepted = True
                break
            scale /= 2.0
        if not accepted:
            raise SyntheticModelContractError(
                "ridge line search failed to improve finite objective"
            )
        theta = theta + scale * step
        if float(np.max(np.abs(scale * step))) < 1e-8:
            converged = True
            break
    if not np.all(np.isfinite(theta)):
        raise SyntheticModelContractError("non-finite ridge coefficients")
    if not converged:
        raise SyntheticModelContractError(
            f"ridge failed to converge in {max_iterations} iterations"
        )
    return PiecewiseExponentialRidge(
        tuple(float(value) for value in bins_s),
        tuple(feature_names),
        tuple(category_names),
        float(alpha),
        scaler,
        theta,
        converged,
        iterations,
    )


def _lodo_single_model(
    observations: Sequence[Mapping[str, object]],
    *,
    bins_s: Sequence[float],
    feature_names: Sequence[str],
    category_names: Sequence[str],
) -> tuple[float, dict[str, object], dict[str, PiecewiseExponentialRidge]]:
    scores = {}
    for alpha in RIDGE_GRID:
        fold_scores = {}
        for heldout in sorted(DISCOVERY_DATES):
            train = [
                row
                for row in observations
                if row["source_date_utc"] != heldout
            ]
            test = [
                row
                for row in observations
                if row["source_date_utc"] == heldout
            ]
            model = fit_piecewise_ridge(
                train,
                bins_s=bins_s,
                feature_names=feature_names,
                category_names=category_names,
                alpha=alpha,
            )
            fold_scores[heldout] = model.rcll(test)
        scores[alpha] = {
            "fold_rcll": fold_scores,
            "total_rcll": sum(fold_scores.values()),
        }
    selected = max(
        RIDGE_GRID,
        key=lambda alpha: (scores[alpha]["total_rcll"], -alpha),
    )
    crossfit = {}
    for heldout in sorted(DISCOVERY_DATES):
        train = [
            row
            for row in observations
            if row["source_date_utc"] != heldout
        ]
        crossfit[heldout] = fit_piecewise_ridge(
            train,
            bins_s=bins_s,
            feature_names=feature_names,
            category_names=category_names,
            alpha=selected,
        )
    report = {
        "selection": "leave-one-discovery-date-out RCLL",
        "ridge_grid": RIDGE_GRID,
        "scores": {
            str(alpha): value for alpha, value in scores.items()
        },
        "selected_alpha_for_synthetic_test": selected,
    }
    return selected, report, crossfit


def _lodo_stage2(
    complement: Sequence[Mapping[str, object]],
    exit_rows: Sequence[Mapping[str, object]],
    *,
    feature_names: Sequence[str] = STAGE2_FEATURES,
) -> tuple[
    float,
    dict[str, object],
    dict[str, tuple[PiecewiseExponentialRidge, PiecewiseExponentialRidge]],
]:
    scores = {}
    for alpha in RIDGE_GRID:
        folds = {}
        for heldout in sorted(DISCOVERY_DATES):
            comp_train = [
                row
                for row in complement
                if row["source_date_utc"] != heldout
            ]
            comp_test = [
                row
                for row in complement
                if row["source_date_utc"] == heldout
            ]
            exit_train = [
                row
                for row in exit_rows
                if row["source_date_utc"] != heldout
            ]
            exit_test = [
                row
                for row in exit_rows
                if row["source_date_utc"] == heldout
            ]
            comp_model = fit_piecewise_ridge(
                comp_train,
                bins_s=POSTFILL_BINS_S,
                feature_names=feature_names,
                category_names=("first_side_no", "action_reprice"),
                alpha=alpha,
            )
            exit_model = fit_piecewise_ridge(
                exit_train,
                bins_s=POSTFILL_BINS_S,
                feature_names=feature_names,
                category_names=("first_side_no", "action_reprice"),
                alpha=alpha,
            )
            folds[heldout] = (
                comp_model.rcll(comp_test) + exit_model.rcll(exit_test)
            )
        scores[alpha] = {
            "fold_combined_rcll": folds,
            "total_combined_rcll": sum(folds.values()),
        }
    selected = max(
        RIDGE_GRID,
        key=lambda alpha: (scores[alpha]["total_combined_rcll"], -alpha),
    )
    crossfit = {}
    for heldout in sorted(DISCOVERY_DATES):
        comp_train = [
            row
            for row in complement
            if row["source_date_utc"] != heldout
        ]
        exit_train = [
            row
            for row in exit_rows
            if row["source_date_utc"] != heldout
        ]
        crossfit[heldout] = (
            fit_piecewise_ridge(
                comp_train,
                bins_s=POSTFILL_BINS_S,
                feature_names=feature_names,
                category_names=("first_side_no", "action_reprice"),
                alpha=selected,
            ),
            fit_piecewise_ridge(
                exit_train,
                bins_s=POSTFILL_BINS_S,
                feature_names=feature_names,
                category_names=("first_side_no", "action_reprice"),
                alpha=selected,
            ),
        )
    return (
        selected,
        {
            "selection": "leave-one-discovery-date-out combined RCLL",
            "ridge_grid": RIDGE_GRID,
            "scores": {
                str(alpha): value for alpha, value in scores.items()
            },
            "selected_alpha_for_synthetic_test": selected,
        },
        crossfit,
    )


def _group_intervals(
    rows: Sequence[Mapping[str, object]],
    id_fields: Sequence[str],
    cause_fields: Sequence[str],
) -> dict[str, list[Mapping[str, object]]]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    seen = set()
    for row in rows:
        if int(row.get("data_invalid", 0)) != 0:
            raise SyntheticModelContractError(
                "DATA_INVALID cannot enter a model risk set; rollback the "
                "entire market-day and every action upstream"
            )
        identifier = "::".join(str(row[field]) for field in id_fields)
        index = int(row["interval_index"])
        key = (identifier, index)
        if key in seen:
            raise SyntheticModelContractError("duplicate interval primary key")
        seen.add(key)
        grouped.setdefault(identifier, []).append(row)
    for identifier, values in grouped.items():
        values.sort(key=lambda row: int(row["interval_index"]))
        if [int(row["interval_index"]) for row in values] != list(
            range(len(values))
        ):
            raise SyntheticModelContractError(
                f"{identifier}: interval index gap"
            )
        if _finite_float(
            values[0]["elapsed_start_ms"],
            "elapsed_start_ms",
        ) != 0:
            raise SyntheticModelContractError(
                f"{identifier}: risk process must start at elapsed zero"
            )
        invariant_fields = (
            "source_date_utc",
            "data_origin",
            "first_fill_side",
            "action_kind",
        )
        for field in invariant_fields:
            present = [row[field] for row in values if field in row]
            if present and any(value != present[0] for value in present):
                raise SyntheticModelContractError(
                    f"{identifier}: interval-varying invariant {field}"
                )
        for left, right in zip(values, values[1:]):
            if (
                _finite_float(left["elapsed_stop_ms"], "elapsed_stop_ms")
                != _finite_float(right["elapsed_start_ms"], "elapsed_start_ms")
                or int(left["interval_stop_wall_ns"])
                != int(right["interval_start_wall_ns"])
            ):
                raise SyntheticModelContractError(
                    f"{identifier}: interval overlap/gap"
                )
        totals = {
            field: sum(int(row[field]) for row in values)
            for field in cause_fields
        }
        if sum(totals.values()) != 1:
            raise SyntheticModelContractError(
                f"{identifier}: terminal cause must conserve exactly once"
            )
        if sum(int(values[-1][field]) for field in cause_fields) != 1:
            raise SyntheticModelContractError(
                f"{identifier}: terminal cause must be in final interval"
            )
        if any(
            sum(int(row[field]) for field in cause_fields) != 0
            for row in values[:-1]
        ):
            raise SyntheticModelContractError(
                f"{identifier}: early terminal cause"
            )
    return grouped


def _stage1_curve(
    first_row: Mapping[str, object],
    model: PiecewiseExponentialRidge,
    horizons: Sequence[float],
) -> dict[float, dict[str, float]]:
    features = {
        side: _stage1_features(first_row, side)
        for side in ("yes", "no")
    }
    result = {}
    for horizon in horizons:
        survival = 1.0
        fy = 0.0
        fn = 0.0
        for index, (left, right) in enumerate(
            zip(ENTRY_BINS_S, ENTRY_BINS_S[1:])
        ):
            if left >= horizon:
                break
            dt = min(right, horizon) - left
            hazards = {}
            for side in ("yes", "no"):
                hazards[side] = model.predict_hazard(
                    {
                        "bin_index": index,
                        "features": features[side],
                        "categories": {
                            "target_side_no": int(side == "no")
                        },
                    }
                )
            total = hazards["yes"] + hazards["no"]
            event_probability = -math.expm1(-total * dt)
            if total > 0:
                fy += (
                    survival
                    * hazards["yes"]
                    / total
                    * event_probability
                )
                fn += (
                    survival
                    * hazards["no"]
                    / total
                    * event_probability
                )
            survival *= math.exp(-total * dt)
        result[float(horizon)] = {
            "YES_FIRST": fy,
            "NO_FIRST": fn,
            "SURVIVAL": survival,
        }
    return result


def _stage2_curve(
    first_row: Mapping[str, object],
    complement_model: PiecewiseExponentialRidge,
    exit_model: PiecewiseExponentialRidge,
    horizons: Sequence[float],
) -> dict[float, dict[str, float]]:
    features = _stage2_features(first_row)
    categories = {
        "first_side_no": int(first_row["first_fill_side"] == "NO"),
        "action_reprice": int(first_row["action_kind"] == "REPRICE"),
    }
    result = {}
    for horizon in horizons:
        survival = 1.0
        complement = 0.0
        exit_value = 0.0
        for index, (left, right) in enumerate(
            zip(POSTFILL_BINS_S, POSTFILL_BINS_S[1:])
        ):
            if left >= horizon:
                break
            dt = min(right, horizon) - left
            observation = {
                "bin_index": index,
                "features": features,
                "categories": categories,
            }
            mu_c = complement_model.predict_hazard(observation)
            mu_x = exit_model.predict_hazard(observation)
            total = mu_c + mu_x
            event_probability = -math.expm1(-total * dt)
            if total > 0:
                complement += (
                    survival * mu_c / total * event_probability
                )
                exit_value += (
                    survival * mu_x / total * event_probability
                )
            survival *= math.exp(-total * dt)
        result[float(horizon)] = {
            "COMPLEMENT_FILL": complement,
            "INVENTORY_EXIT": exit_value,
            "SURVIVAL": survival,
        }
    return result


def _summaries_with_predictions(
    grouped: Mapping[str, Sequence[Mapping[str, object]]],
    cause_fields: Mapping[str, str],
    crossfit_models: Mapping[str, object],
    curve_function: object,
    horizons: Sequence[float],
) -> list[dict[str, object]]:
    summaries = []
    for identifier, rows in grouped.items():
        first = rows[0]
        final = rows[-1]
        terminal = next(
            cause
            for cause, field in cause_fields.items()
            if int(final[field]) == 1
        )
        day = _row_date(first)
        models = crossfit_models[day]
        if isinstance(models, tuple):
            predictions = curve_function(
                first,
                models[0],
                models[1],
                horizons,
            )
        else:
            predictions = curve_function(
                first,
                models,
                horizons,
            )
        summary = {
            "episode_id": identifier,
            "source_date_utc": day,
            "terminal_time_s": _finite_float(
                final["elapsed_stop_ms"],
                "elapsed_stop_ms",
            )
            / 1_000,
            "terminal_type": terminal,
            "predictions": predictions,
        }
        for field in (
            "first_fill_side",
            "action_kind",
            "tte_ms",
            "complement_queue_position_fp",
        ):
            if field in first:
                summary[field] = first[field]
        summaries.append(summary)
    return summaries


def _censor_survival(
    summaries: Sequence[Mapping[str, object]],
    censor_types: set[str],
) -> tuple[dict[float, float], dict[float, float]]:
    times = sorted(
        {
            float(row["terminal_time_s"])
            for row in summaries
            if row["terminal_type"] in censor_types
        }
    )
    before = {}
    after = {}
    survival = 1.0
    for time in times:
        at_risk = sum(
            float(row["terminal_time_s"]) >= time for row in summaries
        )
        censored = sum(
            float(row["terminal_time_s"]) == time
            and row["terminal_type"] in censor_types
            for row in summaries
        )
        before[time] = survival
        if at_risk > 0:
            survival *= 1.0 - censored / at_risk
        after[time] = survival
    return before, after


def _g_value(
    time: float,
    before: Mapping[float, float],
    after: Mapping[float, float],
    *,
    left_limit: bool,
) -> float:
    result = 1.0
    for censor_time in sorted(after):
        if censor_time < time or (not left_limit and censor_time <= time):
            result = after[censor_time]
    return result


def _calibration_metric(
    summaries: Sequence[Mapping[str, object]],
    *,
    horizon: float,
    positive_type: str,
    censor_types: set[str],
) -> dict[str, object]:
    before, after = _censor_survival(summaries, censor_types)
    probabilities = []
    labels = []
    weights = []
    for row in summaries:
        terminal_time = float(row["terminal_time_s"])
        terminal_type = str(row["terminal_type"])
        prediction = float(
            row["predictions"][float(horizon)][positive_type]
        )
        if terminal_time <= horizon and terminal_type in censor_types:
            continue
        if terminal_time <= horizon:
            label = int(terminal_type == positive_type)
            g = _g_value(
                terminal_time,
                before,
                after,
                left_limit=True,
            )
        else:
            label = 0
            g = _g_value(
                horizon,
                before,
                after,
                left_limit=False,
            )
        if g <= 1e-12:
            continue
        probabilities.append(min(1 - 1e-9, max(1e-9, prediction)))
        labels.append(label)
        weights.append(1.0 / g)
    if not probabilities:
        return {
            "status": "NO_IPCW_SUPPORT",
            "n": 0,
            "ipcw_brier": None,
            "calibration_intercept": None,
            "calibration_slope": None,
        }
    p = np.asarray(probabilities)
    y = np.asarray(labels, dtype=float)
    w = np.asarray(weights, dtype=float)
    brier = float(np.sum(w * (y - p) ** 2) / np.sum(w))
    if len(set(labels)) < 2:
        return {
            "status": "INSUFFICIENT_CLASS_SUPPORT",
            "n": len(labels),
            "events": int(y.sum()),
            "ipcw_brier": brier,
            "calibration_intercept": None,
            "calibration_slope": None,
        }
    logit = np.log(p / (1.0 - p))
    design = np.column_stack((np.ones(len(p)), logit))
    theta = np.asarray((0.0, 1.0), dtype=float)
    converged = False
    for _iteration in range(100):
        eta = np.clip(design @ theta, -30.0, 30.0)
        fitted = 1.0 / (1.0 + np.exp(-eta))
        gradient = design.T @ (w * (y - fitted))
        information = (
            design.T
            @ ((w * fitted * (1.0 - fitted))[:, None] * design)
            + np.eye(2) * 1e-9
        )
        step = np.linalg.solve(information, gradient)
        theta += step
        if float(np.max(np.abs(step))) < 1e-8:
            converged = True
            break
    return {
        "status": "OK" if converged else "MAX_ITERATIONS",
        "n": len(labels),
        "events": int(y.sum()),
        "ipcw_brier": brier,
        "calibration_intercept": float(theta[0]),
        "calibration_slope": float(theta[1]),
    }


def _calibration_report(
    summaries: Sequence[Mapping[str, object]],
    *,
    horizons: Sequence[float],
    positive_types: Sequence[str],
    censor_types: set[str],
) -> dict[str, object]:
    output = {}
    for positive in positive_types:
        output[positive] = {
            str(horizon): _calibration_metric(
                summaries,
                horizon=float(horizon),
                positive_type=positive,
                censor_types=censor_types,
            )
            for horizon in horizons
        }
    return output


def _stage2_subgroup_calibration(
    summaries: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    result = {"first_side": {}, "tte_tercile": {}, "queue_tercile": {}}
    for side in ("YES", "NO"):
        subset = [
            row for row in summaries if row.get("first_fill_side") == side
        ]
        result["first_side"][side] = _calibration_report(
            subset,
            horizons=STAGE2_HORIZONS_S,
            positive_types=("COMPLEMENT_FILL",),
            censor_types={"ADMIN_CENSOR"},
        )
    for field, output_name in (
        ("tte_ms", "tte_tercile"),
        ("complement_queue_position_fp", "queue_tercile"),
    ):
        values = np.asarray(
            [_finite_float(row[field], field) for row in summaries],
            dtype=float,
        )
        q1, q2 = np.quantile(values, (1 / 3, 2 / 3))
        groups = {
            "LOW": [
                row
                for row in summaries
                if _finite_float(row[field], field) <= q1
            ],
            "MID": [
                row
                for row in summaries
                if q1 < _finite_float(row[field], field) <= q2
            ],
            "HIGH": [
                row
                for row in summaries
                if _finite_float(row[field], field) > q2
            ],
        }
        result[output_name] = {
            label: _calibration_report(
                subset,
                horizons=STAGE2_HORIZONS_S,
                positive_types=("COMPLEMENT_FILL",),
                censor_types={"ADMIN_CENSOR"},
            )
            for label, subset in groups.items()
        }
        result[output_name]["cutpoints"] = (float(q1), float(q2))
    return result


def _aalen_johansen(
    summaries: Sequence[Mapping[str, object]],
    horizons: Sequence[float],
    cause_types: Sequence[str],
) -> dict[str, object]:
    result = {}
    terminal_times = sorted(
        {
            float(row["terminal_time_s"])
            for row in summaries
            if row["terminal_type"] in set(cause_types)
        }
    )
    for horizon in horizons:
        survival = 1.0
        cif = {cause: 0.0 for cause in cause_types}
        for time in terminal_times:
            if time > horizon:
                break
            at_risk = sum(
                float(row["terminal_time_s"]) >= time for row in summaries
            )
            if at_risk == 0:
                continue
            counts = {
                cause: sum(
                    float(row["terminal_time_s"]) == time
                    and row["terminal_type"] == cause
                    for row in summaries
                )
                for cause in cause_types
            }
            total = sum(counts.values())
            for cause in cause_types:
                cif[cause] += survival * counts[cause] / at_risk
            survival *= 1.0 - total / at_risk
        result[str(horizon)] = {
            **cif,
            "SURVIVAL": survival,
            "identity_sum": survival + sum(cif.values()),
        }
    return result


def _prediction_identity(
    summaries: Sequence[Mapping[str, object]],
) -> float:
    errors = []
    for row in summaries:
        for prediction in row["predictions"].values():
            errors.append(abs(sum(prediction.values()) - 1.0))
    return max(errors, default=0.0)


def stage1_crossfit_rcll_by_cause(
    observations: Sequence[Mapping[str, object]],
    crossfit_models: Mapping[str, PiecewiseExponentialRidge],
) -> dict[str, float]:
    values = {"YES_FIRST": 0.0, "NO_FIRST": 0.0}
    for day, model in crossfit_models.items():
        heldout = [
            row
            for row in observations
            if row["source_date_utc"] == day
        ]
        yes_rows = [
            row
            for row in heldout
            if int(row["categories"]["target_side_no"]) == 0
        ]
        no_rows = [
            row
            for row in heldout
            if int(row["categories"]["target_side_no"]) == 1
        ]
        values["YES_FIRST"] += model.rcll(yes_rows)
        values["NO_FIRST"] += model.rcll(no_rows)
    values["combined"] = values["YES_FIRST"] + values["NO_FIRST"]
    return values


def aj_direction_gate(
    parametric: Mapping[str, float],
    empirical_aj: Mapping[str, float],
    *,
    tolerance: float = 1e-6,
) -> dict[str, object]:
    """Pairwise ranking gate; disagreement is MODEL_MISSPECIFIED."""
    if tolerance < 0 or set(parametric) != set(empirical_aj):
        raise SyntheticModelContractError(
            "direction gate requires matching cells and nonnegative tolerance"
        )
    cells = sorted(parametric)
    comparisons = []
    conflict = False
    decisions = 0
    for index, left in enumerate(cells):
        for right in cells[index + 1 :]:
            param_diff = _finite_float(
                parametric[left],
                f"parametric/{left}",
            ) - _finite_float(parametric[right], f"parametric/{right}")
            aj_diff = _finite_float(
                empirical_aj[left],
                f"empirical/{left}",
            ) - _finite_float(empirical_aj[right], f"empirical/{right}")
            if abs(param_diff) <= tolerance or abs(aj_diff) <= tolerance:
                outcome = "NO_DIRECTION_DECISION"
            elif param_diff * aj_diff < 0:
                outcome = "MODEL_MISSPECIFIED"
                conflict = True
                decisions += 1
            else:
                outcome = "DIRECTION_AGREES"
                decisions += 1
            comparisons.append(
                {
                    "left": left,
                    "right": right,
                    "parametric_difference": param_diff,
                    "aalen_johansen_difference": aj_diff,
                    "outcome": outcome,
                }
            )
    status = (
        "MODEL_MISSPECIFIED"
        if conflict
        else ("PASS" if decisions else "NO_DIRECTION_DECISION")
    )
    return {
        "status": status,
        "tolerance": tolerance,
        "comparisons": comparisons,
        "action_seal_allowed": status == "PASS",
    }


def _stage1_direction_report(
    summaries: Sequence[Mapping[str, object]],
    aj: Mapping[str, object],
) -> dict[str, object]:
    horizon = float(STAGE1_HORIZONS_S[-1])
    parametric = {
        cause: float(
            np.mean(
                [
                    row["predictions"][horizon][cause]
                    for row in summaries
                ]
            )
        )
        for cause in ("YES_FIRST", "NO_FIRST")
    }
    empirical = {
        cause: float(aj[str(horizon)][cause])
        for cause in ("YES_FIRST", "NO_FIRST")
    }
    return aj_direction_gate(parametric, empirical)


def _stage2_action_direction_report(
    summaries: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    horizon = float(STAGE2_HORIZONS_S[-1])
    parametric = {}
    empirical = {}
    cells = sorted(
        {
            (str(row["action_kind"]), str(row["first_fill_side"]))
            for row in summaries
        }
    )
    for action, first_side in cells:
        subset = [
            row
            for row in summaries
            if row["action_kind"] == action
            and row["first_fill_side"] == first_side
        ]
        cell = f"{action}|first={first_side}"
        parametric[cell] = float(
            np.mean(
                [
                    row["predictions"][horizon]["COMPLEMENT_FILL"]
                    for row in subset
                ]
            )
        )
        aj = _aalen_johansen(
            subset,
            (horizon,),
            ("COMPLEMENT_FILL", "INVENTORY_EXIT"),
        )
        empirical[cell] = float(
            aj[str(horizon)]["COMPLEMENT_FILL"]
        )
    if len(parametric) < 2:
        return {
            "status": "NO_DIRECTION_DECISION",
            "tolerance": 1e-6,
            "comparisons": [],
            "action_seal_allowed": False,
        }
    return aj_direction_gate(parametric, empirical)


def action_economics(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Validate cycle_policy_outcome rows and report unranked economics."""
    required = (
        "experiment_id",
        "data_role",
        "source_date_utc",
        "market_ticker",
        "market_cluster_id",
        "cycle_id",
        "entry_action_id",
        "policy_id",
        "action_set_version",
        "admitted",
        "terminal_type",
        "first_fill_side",
        "net_pnl_usd",
        "capital_dollar_seconds",
        "peak_episode_capital_usd",
        "entry_quote_seconds",
        "orphan_seconds",
        "maker_fee_usd",
        "taker_fee_usd",
        "source_rows_sha256",
        "reconciliation_ok",
    )
    terminal_types = {
        "NO_ENTRY",
        "NO_FIRST_FILL",
        "PAIR_COMPLETE",
        "INVENTORY_EXIT",
    }
    rejected_terminals = {"DATA_INVALID", "UNRECONCILED"}
    primary_keys = set()
    grouped: dict[
        tuple[str, str, str], list[Mapping[str, object]]
    ] = {}
    for row in rows:
        _row_date(row)
        missing = [field for field in required if field not in row]
        if missing:
            raise SyntheticModelContractError(
                f"cycle_policy_outcome missing DDL fields: {missing}"
            )
        if row.get("experiment_id") != EXPERIMENT_ID:
            raise SyntheticModelContractError("economic experiment mismatch")
        if row["data_role"] != "DISCOVERY":
            raise SyntheticModelContractError(
                "synthetic economics accepts DISCOVERY rows only"
            )
        primary_key = (
            str(row["experiment_id"]),
            str(row["cycle_id"]),
            str(row["entry_action_id"]),
            str(row["policy_id"]),
            str(row["action_set_version"]),
        )
        if primary_key in primary_keys:
            raise SyntheticModelContractError(
                "duplicate cycle_policy_outcome primary key"
            )
        primary_keys.add(primary_key)
        terminal = str(row["terminal_type"])
        if terminal in rejected_terminals:
            raise SyntheticModelContractError(
                f"{terminal} cannot enter action economics"
            )
        if terminal not in terminal_types:
            raise SyntheticModelContractError(
                f"nonterminal/unknown cycle outcome: {terminal}"
            )
        if row["reconciliation_ok"] is not True:
            raise SyntheticModelContractError(
                "cycle_policy_outcome reconciliation is not green"
            )
        if type(row["admitted"]) is not bool:
            raise SyntheticModelContractError("admitted must be boolean")
        pnl = _decimal(row["net_pnl_usd"], "net_pnl_usd")
        capital = _decimal(
            row["capital_dollar_seconds"],
            "capital_dollar_seconds",
        )
        peak = _decimal(
            row["peak_episode_capital_usd"],
            "peak_episode_capital_usd",
        )
        quote_seconds = _decimal(
            row["entry_quote_seconds"],
            "entry_quote_seconds",
        )
        orphan_seconds = _decimal(
            row["orphan_seconds"],
            "orphan_seconds",
        )
        maker_fee = _decimal(row["maker_fee_usd"], "maker_fee_usd")
        taker_fee = _decimal(row["taker_fee_usd"], "taker_fee_usd")
        if min(
            capital,
            peak,
            quote_seconds,
            orphan_seconds,
            maker_fee,
            taker_fee,
        ) < 0:
            raise SyntheticModelContractError(
                "cycle ledger contains a negative non-PnL amount"
            )
        lineage = str(row["source_rows_sha256"])
        if re.fullmatch(r"[0-9a-f]{64}", lineage) is None:
            raise SyntheticModelContractError(
                "cycle source_rows_sha256 is not a lowercase SHA256"
            )
        first_side = row["first_fill_side"]
        if terminal == "NO_ENTRY":
            if (
                row["admitted"]
                or first_side is not None
                or any(
                    value != 0
                    for value in (
                        pnl,
                        capital,
                        peak,
                        quote_seconds,
                        orphan_seconds,
                        maker_fee,
                        taker_fee,
                    )
                )
            ):
                raise SyntheticModelContractError(
                    "NO_ENTRY ledger does not conserve to zero/unadmitted"
                )
        elif terminal == "NO_FIRST_FILL":
            if (
                row["admitted"] is not True
                or first_side is not None
                or pnl != 0
                or maker_fee != 0
                or taker_fee != 0
                or orphan_seconds != 0
                or capital <= 0
                or quote_seconds <= 0
            ):
                raise SyntheticModelContractError(
                    "NO_FIRST_FILL ledger/fees/first-side do not conserve"
                )
        else:
            if (
                row["admitted"] is not True
                or first_side not in ("YES", "NO")
                or capital <= 0
                or peak <= 0
            ):
                raise SyntheticModelContractError(
                    f"{terminal} terminal ledger is incomplete"
                )
        group_key = (
            str(row["entry_action_id"]),
            str(row["policy_id"]),
            str(row["action_set_version"]),
        )
        grouped.setdefault(group_key, []).append(row)
    result = {}
    for group_key, values in sorted(grouped.items()):
        admitted = [row for row in values if row.get("admitted") is True]
        if not admitted:
            raise SyntheticModelContractError(
                f"{group_key}: no admitted cycles"
            )
        pnl = sum(
            (_decimal(row["net_pnl_usd"], "net_pnl_usd") for row in admitted),
            Decimal("0"),
        )
        capital_time = sum(
            (
                _decimal(
                    row["capital_dollar_seconds"],
                    "capital_dollar_seconds",
                )
                for row in admitted
            ),
            Decimal("0"),
        )
        if capital_time <= 0:
            raise SyntheticModelContractError(
                f"{group_key}: non-positive capital-time"
            )
        source_hours = Decimal(
            24 * len({str(row["source_date_utc"]) for row in values})
        )
        ev_cycle = pnl / Decimal(len(admitted))
        ev_capital_time = pnl / capital_time
        peak = max(
            _decimal(
                row["peak_episode_capital_usd"],
                "peak_episode_capital_usd",
            )
            for row in admitted
        )
        group_id = "|".join(group_key)
        result[group_id] = {
            "entry_action_id": group_key[0],
            "policy_id": group_key[1],
            "action_set_version": group_key[2],
            "cycle_policy_outcome_rows": len(values),
            "admitted_cycles_including_no_fill": len(admitted),
            "total_net_pnl_usd": str(pnl),
            "ev_usd_per_cycle": str(ev_cycle),
            "total_capital_dollar_seconds": str(capital_time),
            "ev_usd_per_locked_dollar_second": str(ev_capital_time),
            "cents_per_locked_dollar_hour": str(
                Decimal("360000") * ev_capital_time
            ),
            "cycles_per_hour": str(
                Decimal(len(admitted)) / source_hours
            ),
            "source_hours": str(source_hours),
            "peak_episode_capital_usd": str(peak),
            "synthetic_only": True,
        }
    return {
        "groups_unranked": result,
        "selection_performed": False,
        "includes_no_fill_admitted_cycles": True,
        "action_ev_basis": (
            "realized pure-synthetic admitted cycle outcomes; "
            "model-implied payoff policy remains ACTION_SET_PENDING"
        ),
    }


def fit_synthetic_round4(
    dataset: SyntheticRound4Dataset,
) -> SyntheticFitBundle:
    """Fit an in-memory synthetic contract test; never produce a candidate."""
    source_guard = validate_synthetic_dataset(dataset)
    stage1_grouped = _group_intervals(
        dataset.stage1_intervals,
        ("entry_episode_id", "entry_action_id"),
        (
            "event_yes_first",
            "event_no_first",
            "admin_censor",
        ),
    )
    stage2_grouped = _group_intervals(
        dataset.stage2_intervals,
        ("postfill_action_id",),
        (
            "event_complement_fill",
            "event_inventory_exit",
            "admin_censor",
        ),
    )
    atom_labels = validate_zero_time_atom_labels(
        dataset.stage2_atom_labels,
        dataset.stage2_intervals,
    )
    zero_time_atom_model, zero_time_atom_report = (
        zero_time_atom_crossfit_report(atom_labels)
    )
    stage1_observations = build_stage1_observations(
        dataset.stage1_intervals
    )
    stage2_complement = build_stage2_observations(
        dataset.stage2_intervals,
        "event_complement_fill",
    )
    stage2_exit = build_stage2_observations(
        dataset.stage2_intervals,
        "event_inventory_exit",
    )

    stage1_alpha, stage1_selection, stage1_crossfit = _lodo_single_model(
        stage1_observations,
        bins_s=ENTRY_BINS_S,
        feature_names=STAGE1_FEATURES,
        category_names=("target_side_no",),
    )
    stage2_alpha, stage2_selection, stage2_crossfit = _lodo_stage2(
        stage2_complement,
        stage2_exit,
    )
    (
        _stage1_null_alpha,
        stage1_pooled_null,
        stage1_pooled_null_crossfit,
    ) = _lodo_single_model(
        stage1_observations,
        bins_s=ENTRY_BINS_S,
        feature_names=(),
        category_names=("target_side_no",),
    )
    (
        _stage2_null_alpha,
        stage2_pooled_null,
        _stage2_pooled_null_crossfit,
    ) = _lodo_stage2(
        stage2_complement,
        stage2_exit,
        feature_names=(),
    )
    stage1_model = fit_piecewise_ridge(
        stage1_observations,
        bins_s=ENTRY_BINS_S,
        feature_names=STAGE1_FEATURES,
        category_names=("target_side_no",),
        alpha=stage1_alpha,
    )
    stage2_complement_model = fit_piecewise_ridge(
        stage2_complement,
        bins_s=POSTFILL_BINS_S,
        feature_names=STAGE2_FEATURES,
        category_names=("first_side_no", "action_reprice"),
        alpha=stage2_alpha,
    )
    stage2_exit_model = fit_piecewise_ridge(
        stage2_exit,
        bins_s=POSTFILL_BINS_S,
        feature_names=STAGE2_FEATURES,
        category_names=("first_side_no", "action_reprice"),
        alpha=stage2_alpha,
    )

    stage1_summaries = _summaries_with_predictions(
        stage1_grouped,
        {
            "YES_FIRST": "event_yes_first",
            "NO_FIRST": "event_no_first",
            "ADMIN_CENSOR": "admin_censor",
        },
        stage1_crossfit,
        _stage1_curve,
        STAGE1_HORIZONS_S,
    )
    stage2_summaries = _summaries_with_predictions(
        stage2_grouped,
        {
            "COMPLEMENT_FILL": "event_complement_fill",
            "INVENTORY_EXIT": "event_inventory_exit",
            "ADMIN_CENSOR": "admin_censor",
        },
        stage2_crossfit,
        _stage2_curve,
        STAGE2_HORIZONS_S,
    )
    stage1_aj = _aalen_johansen(
        stage1_summaries,
        STAGE1_HORIZONS_S,
        ("YES_FIRST", "NO_FIRST"),
    )
    stage2_aj = _aalen_johansen(
        stage2_summaries,
        STAGE2_HORIZONS_S,
        ("COMPLEMENT_FILL", "INVENTORY_EXIT"),
    )
    stage1_direction = _stage1_direction_report(
        stage1_summaries,
        stage1_aj,
    )
    stage2_direction = _stage2_action_direction_report(
        stage2_summaries
    )
    model_gate_status = (
        "MODEL_MISSPECIFIED"
        if "MODEL_MISSPECIFIED"
        in (stage1_direction["status"], stage2_direction["status"])
        else "SYNTHETIC_DIRECTION_GATE_PASS"
    )

    report = {
        "schema": "round4-pure-synthetic-two-stage-fit-v1.1",
        "experiment_id": EXPERIMENT_ID,
        "status": "SYNTHETIC_MODEL_CONTRACT_TEST_ONLY",
        "candidate_status": CANDIDATE_STATUS,
        "claim": CLAIM,
        "deployable": False,
        "live_authorized": False,
        "selection_performed": False,
        "source_guard": source_guard,
        "stage1": {
            "object": "first-fill competing risks",
            "curve_projection": (
                "first-decision causal features held fixed across sealed "
                "time bins; no future feature path consumed"
            ),
            "selection": stage1_selection,
            "final_synthetic_model": stage1_model.receipt(),
            "crossfit_rcll_by_cause": stage1_crossfit_rcll_by_cause(
                stage1_observations,
                stage1_crossfit,
            ),
            "crossfit_calibration": _calibration_report(
                stage1_summaries,
                horizons=STAGE1_HORIZONS_S,
                positive_types=("YES_FIRST", "NO_FIRST"),
                censor_types={"ADMIN_CENSOR"},
            ),
            "aalen_johansen": stage1_aj,
            "aj_direction_gate": stage1_direction,
            "baselines": {
                "pooled_null": {
                    **stage1_pooled_null,
                    "crossfit_rcll_by_cause": (
                        stage1_crossfit_rcll_by_cause(
                            stage1_observations,
                            stage1_pooled_null_crossfit,
                        )
                    ),
                    "continuous_features": (),
                },
                "eta_mechanical": {
                    "status": "ETA_BASELINE_NOT_IDENTIFIED",
                    "required_interface": (
                        "EtaMechanicalContract(queue_ahead_field, "
                        "consumption_rate_field, clip_fp, "
                        "consumption_sign, contracts_per_second)"
                    ),
                },
            },
            "probability_identity_max_abs_error": _prediction_identity(
                stage1_summaries
            ),
        },
        "stage2": {
            "object": (
                "P(complement before exit | first fill, no zero-time atom, "
                "causal state, action)"
            ),
            "curve_projection": (
                "first-fill causal features held fixed across sealed time "
                "bins; no post-terminal/future feature path consumed"
            ),
            "selection": stage2_selection,
            "final_synthetic_complement_model": (
                stage2_complement_model.receipt()
            ),
            "final_synthetic_exit_model": stage2_exit_model.receipt(),
            "crossfit_combined_rcll": sum(
                comp_model.rcll(
                    [
                        row
                        for row in stage2_complement
                        if row["source_date_utc"] == day
                    ]
                )
                + exit_model.rcll(
                    [
                        row
                        for row in stage2_exit
                        if row["source_date_utc"] == day
                    ]
                )
                for day, (comp_model, exit_model) in stage2_crossfit.items()
            ),
            "crossfit_calibration": _calibration_report(
                stage2_summaries,
                horizons=STAGE2_HORIZONS_S,
                positive_types=("COMPLEMENT_FILL",),
                censor_types={"ADMIN_CENSOR"},
            ),
            "subgroup_calibration": _stage2_subgroup_calibration(
                stage2_summaries
            ),
            "aalen_johansen": stage2_aj,
            "aj_direction_gate": stage2_direction,
            "baselines": {
                "pooled_null": {
                    **stage2_pooled_null,
                    "continuous_features": (),
                },
                "eta_mechanical": {
                    "status": "ETA_BASELINE_NOT_IDENTIFIED",
                    "required_interface": (
                        "EtaMechanicalContract(queue_ahead_field, "
                        "consumption_rate_field, clip_fp, "
                        "consumption_sign, contracts_per_second)"
                    ),
                },
            },
            "probability_identity_max_abs_error": _prediction_identity(
                stage2_summaries
            ),
            "ioc_handling": (
                "deterministic economic outcome only; excluded from hazards"
            ),
        },
        "zero_time_atom": {
            **zero_time_atom_report,
            "total_completion_identity": (
                "Q_C_total(h)=pi_atom+(1-pi_atom)*"
                "Q_C_continuous(h|no_atom)"
            ),
        },
        "action_economics": action_economics(dataset.cycle_outcomes),
        "model_gate_status": model_gate_status,
        "action_seal_allowed": False,
        "nonparametric_sanity_status": model_gate_status,
    }
    return SyntheticFitBundle(
        stage1_model,
        stage2_complement_model,
        stage2_exit_model,
        zero_time_atom_model,
        report,
    )
