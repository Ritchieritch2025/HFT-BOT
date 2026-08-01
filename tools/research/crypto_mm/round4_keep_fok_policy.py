#!/usr/bin/env python3
"""Pure-synthetic Stage-2 learner for KEEP versus FLATTEN_FOK.

This module is deliberately non-deployable.  It consumes only an already
validated V4 public-proxy post-fill batch plus hash-sealed synthetic
comparator receipts.
It has no file reader, exchange client, candidate publisher, action sealer,
or live-trading path.

The estimand is conditional on a first fill.  KEEP is an interval transition:
its immediate reward is added exactly once and its continuation value is
fitted backwards.  FLATTEN_FOK is a terminal counterfactual whose held-out
value is predicted from train-only rows.  Held-out action selection never
reads the held-out counterfactual outcome.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
import re

import numpy as np

from tools.research.crypto_mm.round4_postfill_state_contract import (
    ACTION_FAMILY_VERSION,
    ALLOWED_ACTIONS,
    CANCEL_TIMING_PROVENANCE,
    CONTRACT_VERSION,
    DATA_ORIGIN,
    DECISION_GRID_MS,
    EXECUTION_NATURE,
    FILL_PROVENANCE,
    FOK_EFFECTIVE_LATENCY_MS,
    FOK_EXECUTION_PROVENANCE,
    FOK_TERMINALS,
    FOK_TIME_IN_FORCE,
    KEEP_EXECUTION_PROVENANCE,
    MAKER_FEE_PROVENANCE,
    MARKET_SERIES_PREFIX,
    PostfillDDLBatch,
    SELF_CROSS_SCOPE,
    SETTLEMENT_FEE_PROVENANCE,
    SETTLEMENT_PROVENANCE,
    required_decision_grid,
)
from tools.research.crypto_mm.round4_table_builder import (
    DATE_TOKEN_RE,
    DISCOVERY_DATES,
    FORBIDDEN_DATES,
    ForbiddenSourceError,
    Round4ContractError,
    assert_source_path_allowed,
)


SYNTHETIC_MARKER = "ROUND4_KEEP_FOK_PUBLIC_PROXY_SYNTHETIC_V4"
SEALED_SOURCE_CONTRACT_SHA256 = (
    "274667194c0fa2ed4e6910b4a148fb28b5de402ab3f9b0a2b55a0c5bc375ff17"
)
SEALED_SOURCE_DDL_SHA256 = (
    "65945e778184089937e1d9c9abbea7cd6e42ac061c26b6479c7b3f5a5dff4cb7"
)
SEALED_SOURCE_PREREG_SHA256 = (
    "7a316e7af70daacda31938fe277e3416469c216270ab1230d66c63737654a75a"
)
SEALED_SOURCE_MANIFEST_SHA256 = (
    "035021fb1a0949cfdd25ae94fa317b8b1e5e19703cc1168d13affbf6daef59b7"
)
# These hashes identify the V4 compatibility target only.  Independent
# semantics audit found unresolved capital/causality questions, so they are
# deliberately not an authorization seal.  V4.1 must replace them and flip
# this gate before any fit is permitted.
SOURCE_PINS_FINALIZED = False
EXPECTED_CURRENT_POLICY_DEFINITION_SHA256 = (
    "bff3ae0626fac7db2430994fd3dbf5acc9a87a627af45006b8590ed0b873dd74"
)
CLAIM = "NO_CANDIDATE"
RIDGE_GRID = (0.1, 1.0, 10.0)
FEE_BANDS = ("optimistic_min_fee", "conservative_fee")
LIVE_AUTHORIZED = False
ACTION_SEAL_ALLOWED = False
OVERLAP_STATUS = "CONDITIONAL_DIAGNOSTIC_NO_CAUSAL_CLAIM"
ONLINE_REFERENCE_CONTEXT = {
    "mean_pair_gain_G_cents": 1.085,
    "mean_fok_or_exit_loss_L_cents": 12.95,
    "empirical_q_star": 0.9227,
    "actual_completion_q": 0.7217,
    "actual_q_minus_q_star": -0.2010,
    "status": "USER_SUPPLIED_CONTEXT_NOT_USED_FOR_FIT_OR_GATE",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Every value below is available at the decision receipt.  No terminal,
# counterfactual-outcome, or historical-policy field is a model feature.
RAW_CURRENT_FEATURES = (
    "complement_queue_position_fp",
    "complement_same_price_ahead_fp",
    "complement_better_depth_fp",
    "complement_touch_distance_e4",
    "complement_order_age_ms",
    "complement_flow_1s_fp",
    "complement_flow_5s_fp",
    "complement_flow_10s_fp",
    "complement_flow_60s_fp",
    "complement_flow_acceleration_fp",
    "touch_imbalance",
    "spread_e4",
    "mid_move_1s_e4",
    "mid_move_10s_e4",
    "mid_move_since_entry_e4",
    "mid_move_since_fill_e4",
    "pair_gain_if_complement_usd",
    "tte_ms",
    "first_price_e4",
    "first_qty_fp",
    "remaining_inventory_fp",
    "flatten_visible_executable_qty_fp",
    "flatten_visible_residual_inventory_fp",
    "flatten_limit_price_e4",
    "flatten_adverse_tick_e4",
    "kernel_fair_e4",
    "first_leg_fair_edge_e4",
    "complement_fair_edge_e4",
    "kernel_fair_move_since_entry_e4",
    "kernel_fair_move_since_fill_e4",
    "first_side_no",
    "flatten_limit_fallback_flag",
    "flatten_visible_current_band_pnl_usd",
)
NULLABLE_CURRENT_FEATURES = frozenset(
    (
        "complement_touch_distance_e4",
        "touch_imbalance",
        "spread_e4",
        "mid_move_1s_e4",
        "mid_move_10s_e4",
        "mid_move_since_entry_e4",
        "mid_move_since_fill_e4",
        "kernel_fair_e4",
        "first_leg_fair_edge_e4",
        "complement_fair_edge_e4",
        "kernel_fair_move_since_entry_e4",
        "kernel_fair_move_since_fill_e4",
    )
)
MISSING_INDICATOR_FEATURES = (
    "two_sided_touch_available",
    "complement_touch_available",
    "kernel_fair_available",
)
CURRENT_FEATURES = RAW_CURRENT_FEATURES + MISSING_INDICATOR_FEATURES


class KeepFokPolicyContractError(Round4ContractError):
    """The sealed synthetic learner contract was violated."""


@dataclass(frozen=True)
class SyntheticKeepFokDataset:
    """Hash-sealed in-memory input; this module opens no source path."""

    marker: str
    batch: PostfillDDLBatch
    expected_batch_sha256: str
    episode_outcomes: Sequence[Mapping[str, object]]
    expected_outcomes_sha256: str


@dataclass(frozen=True)
class FoldScaler:
    feature_names: tuple[str, ...]
    median: np.ndarray
    scale: np.ndarray
    lower: np.ndarray
    upper: np.ndarray
    impute: np.ndarray
    all_missing_train: np.ndarray

    @classmethod
    def fit(
        cls,
        rows: Sequence[Mapping[str, float | None]],
    ) -> "FoldScaler":
        if not rows:
            raise KeepFokPolicyContractError("cannot scale empty train rows")
        matrix = np.empty(
            (len(rows), len(CURRENT_FEATURES)), dtype=float
        )
        for row_index, row in enumerate(rows):
            for column, name in enumerate(CURRENT_FEATURES):
                value = row[name]
                if value is None:
                    if name not in NULLABLE_CURRENT_FEATURES:
                        raise KeepFokPolicyContractError(
                            f"nonnullable train feature missing: {name}"
                        )
                    matrix[row_index, column] = np.nan
                else:
                    numeric = float(value)
                    if not math.isfinite(numeric):
                        raise KeepFokPolicyContractError(
                            f"non-finite train feature {name}"
                        )
                    matrix[row_index, column] = numeric
        median = np.zeros(matrix.shape[1], dtype=float)
        scale = np.ones(matrix.shape[1], dtype=float)
        lower = np.zeros(matrix.shape[1], dtype=float)
        upper = np.zeros(matrix.shape[1], dtype=float)
        impute = np.zeros(matrix.shape[1], dtype=float)
        all_missing = np.zeros(matrix.shape[1], dtype=bool)
        for column, name in enumerate(CURRENT_FEATURES):
            observed = matrix[:, column][
                ~np.isnan(matrix[:, column])
            ]
            if observed.size == 0:
                if name not in NULLABLE_CURRENT_FEATURES:
                    raise KeepFokPolicyContractError(
                        f"nonnullable train column all-missing: {name}"
                    )
                all_missing[column] = True
                # Pre-registered deterministic value; no held-out row can
                # supply a scale that was absent from training.
                median[column] = 0.0
                impute[column] = 0.0
                lower[column] = 0.0
                upper[column] = 0.0
                continue
            column_median = float(np.median(observed))
            median[column] = column_median
            impute[column] = column_median
            q25 = float(np.quantile(observed, 0.25))
            q75 = float(np.quantile(observed, 0.75))
            scale[column] = q75 - q25 or 1.0
            if name in MISSING_INDICATOR_FEATURES:
                # Indicators are pre-registered binary fields.  Keeping
                # [0,1] prevents a train-all-observed fold from clipping a
                # held-out missing indicator back to zero.
                lower[column] = 0.0
                upper[column] = 1.0
            else:
                lower[column] = float(
                    np.quantile(observed, 0.01)
                )
                upper[column] = float(
                    np.quantile(observed, 0.99)
                )
        return cls(
            tuple(CURRENT_FEATURES),
            median,
            scale,
            lower,
            upper,
            impute,
            all_missing,
        )

    def transform(
        self, row: Mapping[str, float | None]
    ) -> np.ndarray:
        values = np.empty(len(self.feature_names), dtype=float)
        for column, name in enumerate(self.feature_names):
            value = row[name]
            if value is None:
                if name not in NULLABLE_CURRENT_FEATURES:
                    raise KeepFokPolicyContractError(
                        f"nonnullable heldout feature missing: {name}"
                    )
                values[column] = self.impute[column]
            else:
                numeric = float(value)
                if not math.isfinite(numeric):
                    raise KeepFokPolicyContractError(
                        f"non-finite heldout feature {name}"
                    )
                values[column] = (
                    0.0
                    if self.all_missing_train[column]
                    else numeric
                )
        clipped = np.minimum(np.maximum(values, self.lower), self.upper)
        return (clipped - self.median) / self.scale

    def receipt_sha256(self) -> str:
        return _canonical_sha256(
            {
                "feature_names": self.feature_names,
                "median": self.median.tolist(),
                "scale": self.scale.tolist(),
                "lower": self.lower.tolist(),
                "upper": self.upper.tolist(),
                "impute": self.impute.tolist(),
                "all_missing_train": self.all_missing_train.tolist(),
            }
        )


@dataclass(frozen=True)
class RidgeQModel:
    grid_index: int
    action_kind: str
    fee_band: str
    alpha: float
    scaler: FoldScaler
    intercept: float
    coefficients: np.ndarray
    train_rows: int
    train_source_hashes_sha256: str
    feature_lineage_sha256: str
    label_lineage_sha256: str
    continuation_label_lineage_sha256: str

    def predict(self, state: Mapping[str, object]) -> float:
        vector = self.scaler.transform(
            causal_feature_snapshot(state, self.fee_band)
        )
        prediction = self.intercept + float(vector @ self.coefficients)
        if not math.isfinite(prediction):
            raise KeepFokPolicyContractError(
                "non-finite fitted-Q prediction"
            )
        return prediction

    def receipt(self) -> dict[str, object]:
        return {
            "grid_index": self.grid_index,
            "action_kind": self.action_kind,
            "fee_band": self.fee_band,
            "alpha": self.alpha,
            "train_rows": self.train_rows,
            "train_source_hashes_sha256": self.train_source_hashes_sha256,
            "feature_lineage_sha256": self.feature_lineage_sha256,
            "label_lineage_sha256": self.label_lineage_sha256,
            "continuation_label_lineage_sha256": (
                self.continuation_label_lineage_sha256
            ),
            "scaler_sha256": self.scaler.receipt_sha256(),
            "model_sha256": _canonical_sha256(
                {
                    "intercept": self.intercept,
                    "coefficients": self.coefficients.tolist(),
                }
            ),
        }


@dataclass(frozen=True)
class BackwardPolicy:
    fee_band: str
    alpha: float
    train_dates: tuple[str, ...]
    keep_models: Mapping[int, RidgeQModel]
    fok_models: Mapping[int, RidgeQModel]

    def receipt(self) -> dict[str, object]:
        keep_receipts = {
            str(index): model.receipt()
            for index, model in sorted(self.keep_models.items())
        }
        fok_receipts = {
            str(index): model.receipt()
            for index, model in sorted(self.fok_models.items())
        }
        return {
            "fee_band": self.fee_band,
            "alpha": self.alpha,
            "train_dates": list(self.train_dates),
            "feature_lineage_sha256": _canonical_sha256(
                {
                    "keep": {
                        index: receipt["feature_lineage_sha256"]
                        for index, receipt in keep_receipts.items()
                    },
                    "flatten_fok": {
                        index: receipt["feature_lineage_sha256"]
                        for index, receipt in fok_receipts.items()
                    },
                }
            ),
            "keep_label_lineage_sha256": _canonical_sha256(
                {
                    index: receipt["label_lineage_sha256"]
                    for index, receipt in keep_receipts.items()
                }
            ),
            "flatten_fok_label_lineage_sha256": _canonical_sha256(
                {
                    index: receipt["label_lineage_sha256"]
                    for index, receipt in fok_receipts.items()
                }
            ),
            "continuation_label_lineage_sha256": _canonical_sha256(
                {
                    index: receipt[
                        "continuation_label_lineage_sha256"
                    ]
                    for index, receipt in keep_receipts.items()
                }
            ),
            "keep_models": keep_receipts,
            "flatten_fok_models": fok_receipts,
        }


@dataclass(frozen=True)
class _Trajectory:
    outcome: dict[str, object]
    states: Mapping[int, dict[str, object]]
    actions: Mapping[int, Mapping[str, dict[str, object]]]
    keep_transitions: Mapping[int, dict[str, object]]
    fok_outcomes: Mapping[int, dict[str, object]]

    @property
    def episode_id(self) -> str:
        return str(self.outcome["postfill_episode_id"])

    @property
    def source_date(self) -> str:
        return str(self.outcome["source_date_utc"])

    @property
    def cluster_id(self) -> str:
        return str(self.outcome["market_cluster_id"])

    @property
    def overlap(self) -> bool:
        return bool(self.outcome["overlapping_episode"])


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(
                value.items(), key=lambda pair: str(pair[0])
            )
        }
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            _jsonable(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def seal_episode_outcomes(
    outcomes: Sequence[Mapping[str, object]],
) -> str:
    """Return the deterministic seal required by the dataset."""
    return _canonical_sha256(tuple(dict(row) for row in outcomes))


def _exact_decimal(value: object, label: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise KeepFokPolicyContractError(
            f"{label}: floats and booleans are forbidden"
        )
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise KeepFokPolicyContractError(
            f"{label}: invalid exact decimal {value!r}"
        ) from exc
    if not result.is_finite():
        raise KeepFokPolicyContractError(f"{label}: non-finite decimal")
    return result


def _plain_int(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise KeepFokPolicyContractError(
            f"{label}: expected plain integer >= {minimum}"
        )
    return value


def _plain_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise KeepFokPolicyContractError(
            f"{label}: expected plain boolean"
        )
    return value


def _required(
    row: Mapping[str, object],
    fields: Sequence[str],
    label: str,
) -> None:
    missing = [field for field in fields if row.get(field) is None]
    if missing:
        raise KeepFokPolicyContractError(
            f"{label}: missing fields {missing}"
        )


def _sha256(value: object, label: str) -> str:
    text = str(value)
    if not SHA256_RE.fullmatch(text):
        raise KeepFokPolicyContractError(
            f"{label}: expected lowercase sha256"
        )
    return text


def _elapsed_ns(value: Decimal, label: str) -> int:
    nanoseconds = value * Decimal("1000000")
    if nanoseconds != nanoseconds.to_integral_value():
        raise KeepFokPolicyContractError(
            f"{label}: elapsed time is not integer nanoseconds"
        )
    return int(nanoseconds)


def _path_tokens(path: os.PathLike[str] | str) -> frozenset[str]:
    tokens = set()
    for match in DATE_TOKEN_RE.finditer(os.fspath(path)):
        candidate = "-".join(match.groups())
        try:
            tokens.add(date.fromisoformat(candidate).isoformat())
        except ValueError as exc:
            raise ForbiddenSourceError(
                f"invalid learner path date token: {candidate}"
            ) from exc
    return frozenset(tokens)


def _preflight_batch_paths(batch: PostfillDDLBatch) -> None:
    days = set(batch.source_dates)
    if days != set(DISCOVERY_DATES):
        raise ForbiddenSourceError(
            "learner requires exactly the three discovery dates"
        )
    coverage = Counter()
    for path in batch.guarded_source_paths:
        tokens = _path_tokens(path)
        if tokens & FORBIDDEN_DATES:
            raise ForbiddenSourceError(
                f"forbidden learner path token(s): "
                f"{sorted(tokens & FORBIDDEN_DATES)}"
            )
        if len(tokens) != 1:
            raise ForbiddenSourceError(
                "learner path must carry exactly one date token"
            )
        day = next(iter(tokens))
        if day not in days:
            raise ForbiddenSourceError(
                f"learner path date outside batch: {day}"
            )
        assert_source_path_allowed(path, day, data_role="DISCOVERY")
        coverage[day] += 1
    if set(coverage) != days:
        raise ForbiddenSourceError(
            "learner guarded paths do not cover all discovery dates"
        )


def _band_suffix(fee_band: str) -> str:
    if fee_band == "optimistic_min_fee":
        return "low"
    if fee_band == "conservative_fee":
        return "high"
    raise KeepFokPolicyContractError(f"unknown fee band {fee_band!r}")


def causal_feature_snapshot(
    state: Mapping[str, object],
    fee_band: str,
) -> dict[str, float | None]:
    """Extract the explicit decision-time feature whitelist."""
    suffix = _band_suffix(fee_band)
    if int(state["source_max_recv_wall_ns"]) > int(
        state["feature_asof_wall_ns"]
    ) or int(state["feature_asof_wall_ns"]) > int(
        state["decision_recv_wall_ns"]
    ):
        raise KeepFokPolicyContractError("feature receipt lookahead")
    first_side = state.get("first_side")
    if first_side not in ("YES", "NO"):
        raise KeepFokPolicyContractError("invalid first_side")
    direct = RAW_CURRENT_FEATURES[:-3]
    features: dict[str, float | None] = {}
    for name in direct:
        raw = state.get(name)
        if raw is None:
            if name not in NULLABLE_CURRENT_FEATURES:
                raise KeepFokPolicyContractError(
                    f"missing nonnullable current feature {name}"
                )
            features[name] = None
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise KeepFokPolicyContractError(
                f"missing/non-numeric current feature {name}"
            ) from exc
        if not math.isfinite(value):
            raise KeepFokPolicyContractError(
                f"non-finite current feature {name}"
            )
        features[name] = value
    features["first_side_no"] = float(first_side == "NO")
    features["flatten_limit_fallback_flag"] = float(
        bool(state["flatten_limit_fallback"])
    )
    features["flatten_visible_current_band_pnl_usd"] = float(
        state[f"flatten_visible_pnl_fee_{suffix}_usd"]
    )
    for name in MISSING_INDICATOR_FEATURES:
        value = state.get(name)
        if type(value) is not bool:
            raise KeepFokPolicyContractError(
                f"invalid sealed availability indicator {name}"
            )
        features[name] = float(value)
    if set(features) != set(CURRENT_FEATURES):
        raise KeepFokPolicyContractError(
            "current feature whitelist does not conserve"
        )
    return features


def _normalize_historical_current(
    raw: Mapping[str, object],
    *,
    first_fill_wall_ns: int,
    first_fill_mono_ns: int,
    zero_time_atom: bool,
) -> dict[str, object]:
    row = dict(raw)
    required = (
        "policy_id",
        "comparator_provenance",
        "policy_row_id",
        "policy_definition_sha256",
        "terminal_type",
        "terminal_route",
        "terminal_elapsed_ms",
        "terminal_recv_wall_ns",
        "terminal_recv_mono_ns",
        "realized_pnl_fee_low_usd",
        "realized_pnl_fee_high_usd",
        "postfill_capital_dollar_seconds",
        "residual_inventory_fp",
        "source_rows_sha256",
        "reconciliation_ok",
    )
    _required(row, required, "sealed CURRENT comparator receipt")
    if (
        row["policy_id"] != "CURRENT_DIST2_TTL60"
        or row["comparator_provenance"] != "SYNTHETIC"
    ):
        raise KeepFokPolicyContractError(
            "CURRENT comparator identity/provenance mismatch"
        )
    if not isinstance(row["policy_row_id"], str) or not row["policy_row_id"]:
        raise KeepFokPolicyContractError("missing historical policy_row_id")
    row["policy_definition_sha256"] = _sha256(
        row["policy_definition_sha256"],
        "CURRENT policy_definition_sha256",
    )
    if (
        row["policy_definition_sha256"]
        != EXPECTED_CURRENT_POLICY_DEFINITION_SHA256
    ):
        raise KeepFokPolicyContractError(
            "CURRENT policy definition hash mismatch"
        )
    row["source_rows_sha256"] = _sha256(
        row["source_rows_sha256"],
        "CURRENT source_rows_sha256",
    )
    if not _plain_bool(
        row["reconciliation_ok"], "CURRENT reconciliation_ok"
    ):
        raise KeepFokPolicyContractError(
            "sealed CURRENT comparator receipt is unreconciled"
        )
    elapsed = _exact_decimal(
        row["terminal_elapsed_ms"], "CURRENT terminal_elapsed_ms"
    )
    elapsed_ns = _elapsed_ns(elapsed, "CURRENT terminal_elapsed_ms")
    if elapsed < 0 or (
        _plain_int(
            row["terminal_recv_wall_ns"],
            "CURRENT terminal_recv_wall_ns",
            minimum=1,
        )
        != first_fill_wall_ns + elapsed_ns
    ) or (
        _plain_int(
            row["terminal_recv_mono_ns"],
            "CURRENT terminal_recv_mono_ns",
            minimum=1,
        )
        != first_fill_mono_ns + elapsed_ns
    ):
        raise KeepFokPolicyContractError(
            "sealed CURRENT comparator receipt clock does not conserve"
        )
    low = _exact_decimal(
        row["realized_pnl_fee_low_usd"], "CURRENT low-fee PnL"
    )
    high = _exact_decimal(
        row["realized_pnl_fee_high_usd"], "CURRENT high-fee PnL"
    )
    capital = _exact_decimal(
        row["postfill_capital_dollar_seconds"],
        "CURRENT postfill capital-dollar-seconds",
    )
    residual = _exact_decimal(
        row["residual_inventory_fp"], "CURRENT residual inventory"
    )
    if low < high or capital < 0 or residual != 0:
        raise KeepFokPolicyContractError(
            "sealed CURRENT comparator PnL/capital/ledger does not "
            "conserve"
        )
    if zero_time_atom:
        if (
            elapsed != 0
            or row["terminal_type"] != "ZERO_TIME_ATOM"
            or row["terminal_route"] != "ZERO_TIME_ATOM"
        ):
            raise KeepFokPolicyContractError(
                "zero-time CURRENT receipt mismatch"
            )
    elif not isinstance(row["terminal_type"], str) or not isinstance(
        row["terminal_route"], str
    ):
        raise KeepFokPolicyContractError(
            "invalid sealed CURRENT comparator terminal semantics"
        )
    row.update(
        {
            "terminal_elapsed_ms": elapsed,
            "realized_pnl_fee_low_usd": low,
            "realized_pnl_fee_high_usd": high,
            "postfill_capital_dollar_seconds": capital,
            "residual_inventory_fp": residual,
            "reconciliation_ok": True,
        }
    )
    return row


def _validate_state_action_rows(
    batch: PostfillDDLBatch,
) -> tuple[
    dict[str, dict[int, dict[str, object]]],
    dict[str, dict[int, dict[str, dict[str, object]]]],
    dict[str, dict[str, object]],
]:
    states: dict[str, dict[int, dict[str, object]]] = {}
    state_by_id: dict[str, dict[str, object]] = {}
    for raw in batch.postfill_compact_causal_state:
        row = dict(raw)
        _required(
            row,
            (
                "postfill_decision_id",
                "postfill_episode_id",
                "source_date_utc",
                "market_ticker",
                "data_origin",
                "decision_index",
                "elapsed_since_first_fill_ms",
                "decision_recv_wall_ns",
                "decision_recv_mono_ns",
                "feature_asof_wall_ns",
                "source_max_recv_wall_ns",
                "source_max_recv_mono_ns",
                "causal_source_rows_sha256",
                "paired_state_fingerprint_sha256",
                "first_fill_recv_wall_ns",
                "first_fill_recv_mono_ns",
                "first_fill_provenance",
                "first_fill_execution_nature",
                "first_fill_fee_provenance",
                "first_side",
                "first_price_e4",
                "first_qty_fp",
                "first_fill_fee_usd",
                "first_leg_cost_basis_usd",
                "remaining_inventory_fp",
                "flatten_book_side",
                "flatten_limit_price_e4",
                "flatten_limit_fallback",
                "flatten_adverse_tick_e4",
                "flatten_visible_executable_qty_fp",
                "flatten_visible_residual_inventory_fp",
                "public_book_data_origin",
                "flatten_counterfactual_provenance",
                "complement_touch_available",
                "two_sided_touch_available",
                "kernel_fair_available",
                "simulated_strategy_order_registry_complete",
                "simulated_strategy_order_registry_sha256",
            ),
            "causal state",
        )
        day = str(row["source_date_utc"])
        if day not in batch.source_dates:
            raise KeepFokPolicyContractError(
                "state day outside sealed batch"
            )
        index = _plain_int(row["decision_index"], "decision_index")
        if (
            index >= len(DECISION_GRID_MS)
            or row["elapsed_since_first_fill_ms"]
            != DECISION_GRID_MS[index]
        ):
            raise KeepFokPolicyContractError(
                "state decision grid does not conserve"
            )
        if (
            int(row["source_max_recv_wall_ns"])
            > int(row["feature_asof_wall_ns"])
            or int(row["feature_asof_wall_ns"])
            > int(row["decision_recv_wall_ns"])
            or int(row["source_max_recv_mono_ns"])
            > int(row["decision_recv_mono_ns"])
        ):
            raise KeepFokPolicyContractError("state row lookahead")
        if (
            row["data_origin"] != DATA_ORIGIN
            or row["public_book_data_origin"] != DATA_ORIGIN
            or row["first_fill_provenance"] != FILL_PROVENANCE
            or row["first_fill_execution_nature"] != EXECUTION_NATURE
            or row["first_fill_fee_provenance"]
            != MAKER_FEE_PROVENANCE
            or row["flatten_counterfactual_provenance"]
            != FOK_EXECUTION_PROVENANCE
            or not str(row["market_ticker"]).startswith(
                MARKET_SERIES_PREFIX
            )
        ):
            raise KeepFokPolicyContractError(
                "state public-proxy provenance mismatch"
            )
        if not _plain_bool(
            row["simulated_strategy_order_registry_complete"],
            "simulated_strategy_order_registry_complete",
        ):
            raise KeepFokPolicyContractError(
                "SIMULATED_SELF_CROSS_UNIDENTIFIED: state cannot enter "
                "learner"
            )
        _sha256(
            row["simulated_strategy_order_registry_sha256"],
            "simulated_strategy_order_registry_sha256",
        )
        _sha256(
            row["causal_source_rows_sha256"],
            "causal_source_rows_sha256",
        )
        _sha256(
            row["paired_state_fingerprint_sha256"],
            "paired_state_fingerprint_sha256",
        )
        remaining = _exact_decimal(
            row["remaining_inventory_fp"], "remaining_inventory_fp"
        )
        first_qty = _exact_decimal(
            row["first_qty_fp"], "first_qty_fp"
        )
        first_fee = _exact_decimal(
            row["first_fill_fee_usd"], "first_fill_fee_usd"
        )
        first_cost = _exact_decimal(
            row["first_leg_cost_basis_usd"],
            "first_leg_cost_basis_usd",
        )
        first_price = _plain_int(
            row["first_price_e4"], "first_price_e4", minimum=1
        )
        expected_cost = (
            Decimal(first_price) / Decimal("10000") * first_qty
        )
        if (
            first_qty <= 0
            or first_fee != 0
            or first_cost != expected_cost
            or remaining != first_qty
        ):
            raise KeepFokPolicyContractError(
                "state first-fill cost/fee/inventory spine mismatch"
            )
        executable = _exact_decimal(
            row["flatten_visible_executable_qty_fp"],
            "flatten_visible_executable_qty_fp",
        )
        residual = _exact_decimal(
            row["flatten_visible_residual_inventory_fp"],
            "flatten_visible_residual_inventory_fp",
        )
        if (
            remaining <= 0
            or executable < 0
            or residual < 0
            or executable + residual != remaining
        ):
            raise KeepFokPolicyContractError(
                "visible flatten quantity does not conserve"
            )
        complement_touch = _plain_bool(
            row["complement_touch_available"],
            "complement_touch_available",
        )
        two_sided_touch = _plain_bool(
            row["two_sided_touch_available"],
            "two_sided_touch_available",
        )
        two_sided_bundle = (
            "touch_imbalance",
            "spread_e4",
            "mid_move_1s_e4",
            "mid_move_10s_e4",
            "mid_move_since_entry_e4",
            "mid_move_since_fill_e4",
        )
        if (
            (row.get("complement_touch_distance_e4") is not None)
            != complement_touch
            or any(
                (row.get(name) is not None) != two_sided_touch
                for name in two_sided_bundle
            )
            or (two_sided_touch and not complement_touch)
        ):
            raise KeepFokPolicyContractError(
                "nullable touch/mid feature bundle mismatch"
            )
        kernel_available = _plain_bool(
            row["kernel_fair_available"],
            "kernel_fair_available",
        )
        kernel_numeric = (
            "kernel_fair_e4",
            "first_leg_fair_edge_e4",
            "complement_fair_edge_e4",
            "kernel_fair_move_since_entry_e4",
            "kernel_fair_move_since_fill_e4",
            "kernel_source_time_ms",
        )
        kernel_bundle = (*kernel_numeric, "kernel_causality_kind")
        if kernel_available or any(
            row.get(name) is not None for name in kernel_bundle
        ):
            raise KeepFokPolicyContractError(
                "V4 historical primary kernel must be unavailable and "
                "all-NULL"
            )
        fallback = _plain_bool(
            row["flatten_limit_fallback"],
            "flatten_limit_fallback",
        )
        if fallback != (residual > 0):
            raise KeepFokPolicyContractError(
                "FLATTEN_FOK full-depth/fallback flag mismatch"
            )
        expected_side = "ASK" if row["first_side"] == "YES" else "BID"
        if row["flatten_book_side"] != expected_side:
            raise KeepFokPolicyContractError(
                "canonical FLATTEN_FOK book-side mismatch"
            )
        state_id = str(row["postfill_decision_id"])
        episode_id = str(row["postfill_episode_id"])
        if state_id in state_by_id or index in states.setdefault(
            episode_id, {}
        ):
            raise KeepFokPolicyContractError(
                "duplicate causal state identity"
            )
        state_by_id[state_id] = row
        states[episode_id][index] = row

    actions: dict[
        str, dict[int, dict[str, dict[str, object]]]
    ] = {}
    action_ids = set()
    for raw in batch.postfill_compact_causal_action:
        row = dict(raw)
        _required(
            row,
            (
                "postfill_action_id",
                "postfill_decision_id",
                "postfill_episode_id",
                "source_date_utc",
                "decision_index",
                "action_family_version",
                "action_kind",
                "action_execution_provenance",
                "causal_source_rows_sha256",
                "paired_state_fingerprint_sha256",
                "requested_qty_fp",
                "reduce_only",
                "effective_latency_ms",
                "fok_requires_prior_synthetic_cancel_applied",
                "legal_action",
            ),
            "causal action",
        )
        action_id = str(row["postfill_action_id"])
        if action_id in action_ids:
            raise KeepFokPolicyContractError("duplicate causal action id")
        action_ids.add(action_id)
        state = state_by_id.get(str(row["postfill_decision_id"]))
        if state is None:
            raise KeepFokPolicyContractError("action has no state parent")
        episode_id = str(row["postfill_episode_id"])
        index = _plain_int(row["decision_index"], "decision_index")
        kind = str(row["action_kind"])
        if (
            row["action_family_version"] != ACTION_FAMILY_VERSION
            or kind not in ALLOWED_ACTIONS
        ):
            raise KeepFokPolicyContractError(
                "action outside sealed KEEP/FLATTEN_FOK family"
            )
        if (
            episode_id != state["postfill_episode_id"]
            or index != state["decision_index"]
            or row["causal_source_rows_sha256"]
            != state["causal_source_rows_sha256"]
            or row["paired_state_fingerprint_sha256"]
            != state["paired_state_fingerprint_sha256"]
            or _exact_decimal(
                row["requested_qty_fp"], "requested_qty_fp"
            )
            != state["remaining_inventory_fp"]
            or not _plain_bool(row["legal_action"], "legal_action")
        ):
            raise KeepFokPolicyContractError(
                "paired state/action linkage mismatch"
            )
        if kind == "KEEP":
            if (
                row["action_execution_provenance"]
                != KEEP_EXECUTION_PROVENANCE
                or row["cancel_timing_provenance"] is not None
                or row["self_cross_scope"] is not None
                or _exact_decimal(
                    row["effective_latency_ms"],
                    "KEEP effective_latency_ms",
                )
                != 0
                or row.get("time_in_force") is not None
                or row.get("fok_book_side") is not None
                or row.get("fok_limit_price_e4") is not None
                or _plain_bool(row["reduce_only"], "KEEP reduce_only")
                or _plain_bool(
                    row["fok_requires_prior_synthetic_cancel_applied"],
                    "KEEP fok_requires_prior_synthetic_cancel_applied",
                )
            ):
                raise KeepFokPolicyContractError(
                    "KEEP execution fields are invalid"
                )
        else:
            if (
                row["action_execution_provenance"]
                != FOK_EXECUTION_PROVENANCE
                or row["cancel_timing_provenance"]
                != CANCEL_TIMING_PROVENANCE
                or row["self_cross_scope"] != SELF_CROSS_SCOPE
                or _exact_decimal(
                    row["effective_latency_ms"],
                    "FOK effective_latency_ms",
                )
                != Decimal(FOK_EFFECTIVE_LATENCY_MS)
                or row.get("time_in_force") != FOK_TIME_IN_FORCE
                or row.get("self_trade_prevention_type")
                != "taker_at_cross"
                or not _plain_bool(
                    row["reduce_only"], "FOK reduce_only"
                )
                or not _plain_bool(
                    row["fok_requires_prior_synthetic_cancel_applied"],
                    "FOK fok_requires_prior_synthetic_cancel_applied",
                )
                or row.get("fok_book_side")
                != state["flatten_book_side"]
                or row.get("fok_limit_price_e4")
                != state["flatten_limit_price_e4"]
                or row.get("fok_limit_fallback")
                != state["flatten_limit_fallback"]
                or row.get("pair_cost_ceiling_e4") is not None
            ):
                raise KeepFokPolicyContractError(
                    "FLATTEN_FOK frozen execution contract mismatch"
                )
        by_kind = actions.setdefault(
            episode_id, {}
        ).setdefault(index, {})
        if kind in by_kind:
            raise KeepFokPolicyContractError(
                "duplicate state/action kind"
            )
        by_kind[kind] = row
    for episode_id, by_grid in states.items():
        if set(actions.get(episode_id, {})) != set(by_grid):
            raise KeepFokPolicyContractError(
                "state/action decision-grid mismatch"
            )
        for index in by_grid:
            if set(actions[episode_id][index]) != set(ALLOWED_ACTIONS):
                raise KeepFokPolicyContractError(
                    "state lacks exact KEEP/FLATTEN_FOK pair"
                )
    atoms = {
        str(row["postfill_episode_id"]): dict(row)
        for row in batch.postfill_zero_time_atom
    }
    if len(atoms) != len(batch.postfill_zero_time_atom):
        raise KeepFokPolicyContractError("duplicate zero-time atom")
    if set(atoms) & set(states):
        raise KeepFokPolicyContractError(
            "zero-time atom also appears in continuous states"
        )
    return states, actions, atoms


def _normalize_keep_transition(
    raw: Mapping[str, object],
    *,
    state: Mapping[str, object],
    keep_action: Mapping[str, object],
    next_state: Mapping[str, object] | None,
) -> dict[str, object]:
    row = dict(raw)
    required = (
        "postfill_keep_transition_id",
        "keep_action_id",
        "from_decision_id",
        "postfill_episode_id",
        "source_date_utc",
        "market_ticker",
        "data_origin",
        "first_fill_provenance",
        "first_fill_execution_nature",
        "first_fill_fee_provenance",
        "keep_execution_provenance",
        "from_decision_index",
        "interval_start_elapsed_ms",
        "interval_stop_elapsed_ms",
        "interval_start_wall_ns",
        "interval_start_mono_ns",
        "interval_stop_wall_ns",
        "interval_stop_mono_ns",
        "transition_type",
        "market_close_elapsed_ms",
        "first_side",
        "first_fill_price_e4",
        "first_fill_qty_fp",
        "first_fill_fee_usd",
        "first_leg_cost_basis_usd",
        "original_complement_price_e4",
        "exit_fee_usd",
        "immediate_gross_pnl_usd",
        "maker_fee_usd",
        "immediate_net_pnl_usd",
        "simulation_reward_exact",
        "reward_value_kind",
        "capital_dollar_seconds_increment",
        "source_rows_sha256",
        "reconciliation_ok",
    )
    _required(row, required, "KEEP interval transition")
    index = int(state["decision_index"])
    if (
        row["keep_action_id"] != keep_action["postfill_action_id"]
        or row["from_decision_id"] != state["postfill_decision_id"]
        or row["postfill_episode_id"] != state["postfill_episode_id"]
        or row["source_date_utc"] != state["source_date_utc"]
        or row["market_ticker"] != state["market_ticker"]
        or _plain_int(
            row["from_decision_index"],
            "KEEP from_decision_index",
        )
        != index
        or not _plain_bool(
            row["reconciliation_ok"],
            "KEEP transition reconciliation_ok",
        )
    ):
        raise KeepFokPolicyContractError(
            "KEEP transition linkage/reconciliation mismatch"
        )
    if (
        row["data_origin"] != DATA_ORIGIN
        or row["first_fill_provenance"] != FILL_PROVENANCE
        or row["first_fill_execution_nature"] != EXECUTION_NATURE
        or row["first_fill_fee_provenance"] != MAKER_FEE_PROVENANCE
        or row["keep_execution_provenance"]
        != KEEP_EXECUTION_PROVENANCE
    ):
        raise KeepFokPolicyContractError(
            "KEEP public-proxy provenance mismatch"
        )
    start = _exact_decimal(
        row["interval_start_elapsed_ms"],
        "KEEP interval_start_elapsed_ms",
    )
    stop = _exact_decimal(
        row["interval_stop_elapsed_ms"],
        "KEEP interval_stop_elapsed_ms",
    )
    if start != Decimal(state["elapsed_since_first_fill_ms"]) or stop <= start:
        raise KeepFokPolicyContractError(
            "KEEP interval bounds do not conserve"
        )
    first_wall = int(state["first_fill_recv_wall_ns"])
    first_mono = int(state["first_fill_recv_mono_ns"])
    if (
        _plain_int(
            row["interval_start_wall_ns"],
            "KEEP interval_start_wall_ns",
            minimum=1,
        )
        != first_wall + _elapsed_ns(start, "KEEP interval start")
        or _plain_int(
            row["interval_start_mono_ns"],
            "KEEP interval_start_mono_ns",
            minimum=1,
        )
        != first_mono + _elapsed_ns(start, "KEEP interval start")
        or _plain_int(
            row["interval_stop_wall_ns"],
            "KEEP interval_stop_wall_ns",
            minimum=1,
        )
        != first_wall + _elapsed_ns(stop, "KEEP interval stop")
        or _plain_int(
            row["interval_stop_mono_ns"],
            "KEEP interval_stop_mono_ns",
            minimum=1,
        )
        != first_mono + _elapsed_ns(stop, "KEEP interval stop")
    ):
        raise KeepFokPolicyContractError(
            "KEEP interval receipt clock does not conserve"
        )
    transition_type = row["transition_type"]
    keep_terminal_type = row.get("keep_terminal_type")
    if transition_type == "NEXT_STATE":
        if (
            next_state is None
            or row.get("next_decision_id")
            != next_state["postfill_decision_id"]
            or stop
            != Decimal(next_state["elapsed_since_first_fill_ms"])
            or keep_terminal_type is not None
        ):
            raise KeepFokPolicyContractError(
                "KEEP NEXT_STATE linkage mismatch"
            )
    elif transition_type == "KEEP_TO_TERMINAL":
        if keep_terminal_type == "CURRENT_DIST2_TTL60":
            raise KeepFokPolicyContractError(
                "CURRENT_DIST2_TTL60 is comparator-only and cannot be "
                "a KEEP terminal"
            )
        if (
            next_state is not None
            or row.get("next_decision_id") is not None
            or keep_terminal_type
            not in ("COMPLEMENT_FILL", "HARD_FALLBACK")
        ):
            raise KeepFokPolicyContractError(
                "terminal KEEP transition cannot link a next state"
            )
    else:
        raise KeepFokPolicyContractError(
            f"unknown KEEP transition_type {transition_type!r}"
        )
    gross = _exact_decimal(
        row["immediate_gross_pnl_usd"],
        "KEEP immediate_gross_pnl_usd",
    )
    maker_fee = _exact_decimal(
        row["maker_fee_usd"], "KEEP maker_fee_usd"
    )
    net = _exact_decimal(
        row["immediate_net_pnl_usd"],
        "KEEP immediate_net_pnl_usd",
    )
    capital = _exact_decimal(
        row["capital_dollar_seconds_increment"],
        "KEEP capital_dollar_seconds_increment",
    )
    first_price = _plain_int(
        row["first_fill_price_e4"],
        "KEEP first_fill_price_e4",
        minimum=1,
    )
    first_qty = _exact_decimal(
        row["first_fill_qty_fp"], "KEEP first_fill_qty_fp"
    )
    first_fee = _exact_decimal(
        row["first_fill_fee_usd"], "KEEP first_fill_fee_usd"
    )
    first_cost = _exact_decimal(
        row["first_leg_cost_basis_usd"],
        "KEEP first_leg_cost_basis_usd",
    )
    exit_fee = _exact_decimal(
        row["exit_fee_usd"], "KEEP exit_fee_usd"
    )
    expected_cost = (
        Decimal(first_price) / Decimal("10000") * first_qty
    )
    if (
        first_cost != expected_cost
        or first_price != state["first_price_e4"]
        or first_qty != state["first_qty_fp"]
        or first_fee != state["first_fill_fee_usd"]
        or first_cost != state["first_leg_cost_basis_usd"]
        or row["first_side"] != state["first_side"]
        or row["original_complement_price_e4"]
        != state["complement_price_e4"]
    ):
        raise KeepFokPolicyContractError(
            "KEEP first-leg cost basis/fee spine mismatch"
        )
    expected_capital = (
        first_cost * (stop - start) / Decimal("1000")
    )
    if capital != expected_capital:
        raise KeepFokPolicyContractError(
            "KEEP capital increment mismatch"
        )
    if (
        row["reward_value_kind"] != "SIMULATION_EXACT"
        or not _plain_bool(
            row["simulation_reward_exact"],
            "KEEP simulation_reward_exact",
        )
        or first_fee != 0
        or exit_fee != 0
        or maker_fee != 0
        or capital < 0
    ):
        raise KeepFokPolicyContractError(
            "KEEP public-proxy reward/capital does not conserve"
        )
    if transition_type == "NEXT_STATE":
        if (
            gross != 0
            or net != 0
            or any(
                row.get(field) is not None
                for field in (
                    "terminal_execution_provenance",
                    "terminal_execution_nature",
                    "terminal_fee_provenance",
                    "terminal_stable_source_id",
                    "terminal_complement_price_e4",
                    "terminal_complement_fill_qty_fp",
                    "official_market_result",
                    "official_result_provenance",
                )
            )
        ):
            raise KeepFokPolicyContractError(
                "KEEP continuation interval cannot repeat terminal reward"
            )
    else:
        terminal_stable_id = row.get("terminal_stable_source_id")
        if (
            not isinstance(terminal_stable_id, str)
            or not terminal_stable_id
        ):
            raise KeepFokPolicyContractError(
                "KEEP terminal stable-source lineage is missing"
            )
        if keep_terminal_type == "COMPLEMENT_FILL":
            terminal_price = _plain_int(
                row.get("terminal_complement_price_e4"),
                "KEEP terminal_complement_price_e4",
                minimum=1,
            )
            terminal_qty = _exact_decimal(
                row.get("terminal_complement_fill_qty_fp"),
                "KEEP terminal_complement_fill_qty_fp",
            )
            expected_gross = (
                (
                    Decimal(10_000)
                    - Decimal(first_price)
                    - Decimal(terminal_price)
                )
                / Decimal("10000")
                * first_qty
            )
            if (
                terminal_price != state["complement_price_e4"]
                or terminal_price
                != row["original_complement_price_e4"]
                or terminal_qty != first_qty
                or row.get("terminal_execution_provenance")
                != FILL_PROVENANCE
                or row.get("terminal_execution_nature")
                != EXECUTION_NATURE
                or row.get("terminal_fee_provenance")
                != MAKER_FEE_PROVENANCE
                or row.get("official_market_result") is not None
                or row.get("official_result_provenance") is not None
                or gross != expected_gross
            ):
                raise KeepFokPolicyContractError(
                    "KEEP original complement public-proxy terminal "
                    "does not reconcile"
                )
        else:
            market_close = _exact_decimal(
                row["market_close_elapsed_ms"],
                "KEEP market_close_elapsed_ms",
            )
            official_result = row.get("official_market_result")
            expected_gross = (
                (
                    Decimal(10_000 - first_price)
                    / Decimal("10000")
                    if official_result == row["first_side"]
                    else -Decimal(first_price) / Decimal("10000")
                )
                * first_qty
            )
            if (
                stop != market_close
                or official_result not in ("YES", "NO")
                or row.get("official_result_provenance")
                != "OFFICIAL_MARKET_RESULT"
                or row.get("terminal_execution_provenance")
                != SETTLEMENT_PROVENANCE
                or row.get("terminal_execution_nature")
                != EXECUTION_NATURE
                or row.get("terminal_fee_provenance")
                != SETTLEMENT_FEE_PROVENANCE
                or row.get("terminal_complement_price_e4") is not None
                or row.get("terminal_complement_fill_qty_fp")
                is not None
                or gross != expected_gross
            ):
                raise KeepFokPolicyContractError(
                    "KEEP official-result HARD_FALLBACK does not "
                    "reconcile"
                )
        if net != gross:
            raise KeepFokPolicyContractError(
                "KEEP terminal net mismatch"
            )
    row["interval_start_elapsed_ms"] = start
    row["interval_stop_elapsed_ms"] = stop
    row["immediate_gross_pnl_usd"] = gross
    row["maker_fee_usd"] = maker_fee
    row["immediate_net_pnl_usd"] = net
    row["capital_dollar_seconds_increment"] = capital
    row["first_fill_qty_fp"] = first_qty
    row["first_fill_fee_usd"] = first_fee
    row["first_leg_cost_basis_usd"] = first_cost
    row["exit_fee_usd"] = exit_fee
    row["source_rows_sha256"] = _sha256(
        row["source_rows_sha256"],
        "KEEP transition source_rows_sha256",
    )
    row["reconciliation_ok"] = True
    return row


def _optional_timestamp(
    value: object,
    label: str,
) -> int | None:
    if value is None:
        return None
    return _plain_int(value, label, minimum=1)


def _normalize_fok_outcome(
    raw: Mapping[str, object],
    *,
    state: Mapping[str, object],
    fok_action: Mapping[str, object],
) -> dict[str, object]:
    row = dict(raw)
    required = (
        "postfill_action_id",
        "postfill_episode_id",
        "source_date_utc",
        "market_ticker",
        "data_origin",
        "first_fill_provenance",
        "first_fill_execution_nature",
        "first_fill_fee_provenance",
        "fok_execution_provenance",
        "cancel_timing_provenance",
        "self_cross_scope",
        "decision_index",
        "terminal_type",
        "planned_effective_elapsed_ms",
        "terminal_elapsed_ms",
        "terminal_wall_ns",
        "terminal_mono_ns",
        "feature_asof_wall_ns",
        "source_max_recv_wall_ns",
        "source_max_recv_mono_ns",
        "source_max_stable_id",
        "outcome_source_rows_sha256",
        "simulated_strategy_order_registry_sha256",
        "fok_sent",
        "synthetic_cancel_applied_before_terminal",
        "public_crossing_event_applied",
        "fok_book_side",
        "fok_limit_price_e4",
        "fok_limit_fallback",
        "first_fill_price_e4",
        "first_fill_qty_fp",
        "first_fill_fee_usd",
        "first_leg_cost_basis_usd",
        "original_complement_price_e4",
        "market_close_elapsed_ms",
        "requested_qty_fp",
        "complement_fill_qty_fp",
        "fok_fill_qty_fp",
        "residual_inventory_fp",
        "execution_slices_sha256",
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
    _required(row, required, "FLATTEN_FOK outcome")
    index = int(state["decision_index"])
    if (
        row["postfill_action_id"] != fok_action["postfill_action_id"]
        or row["postfill_episode_id"] != state["postfill_episode_id"]
        or row["source_date_utc"] != state["source_date_utc"]
        or row["market_ticker"] != state["market_ticker"]
        or _plain_int(row["decision_index"], "FOK decision_index")
        != index
        or not _plain_bool(
            row["reconciliation_ok"], "FOK reconciliation_ok"
        )
        or _plain_bool(row["data_invalid"], "FOK data_invalid")
    ):
        raise KeepFokPolicyContractError(
            "FLATTEN_FOK outcome linkage/reconciliation mismatch"
        )
    if (
        row["data_origin"] != DATA_ORIGIN
        or row["first_fill_provenance"] != FILL_PROVENANCE
        or row["first_fill_execution_nature"] != EXECUTION_NATURE
        or row["first_fill_fee_provenance"] != MAKER_FEE_PROVENANCE
        or row["fok_execution_provenance"]
        != FOK_EXECUTION_PROVENANCE
        or row["cancel_timing_provenance"]
        != CANCEL_TIMING_PROVENANCE
        or row["self_cross_scope"] != SELF_CROSS_SCOPE
    ):
        raise KeepFokPolicyContractError(
            "FLATTEN_FOK public counterfactual provenance mismatch"
        )
    if not (
        int(state["decision_recv_wall_ns"])
        <= int(row["source_max_recv_wall_ns"])
        <= int(row["feature_asof_wall_ns"])
        <= int(row["terminal_wall_ns"])
        and int(state["decision_recv_mono_ns"])
        <= int(row["source_max_recv_mono_ns"])
        <= int(row["terminal_mono_ns"])
    ):
        raise KeepFokPolicyContractError(
            "FLATTEN_FOK outcome source clock mismatch"
        )
    if (
        _sha256(
            row["simulated_strategy_order_registry_sha256"],
            "FOK simulated_strategy_order_registry_sha256",
        )
        != state["simulated_strategy_order_registry_sha256"]
    ):
        raise KeepFokPolicyContractError(
            "FLATTEN_FOK self-cross proof source drift"
        )
    if not isinstance(row["source_max_stable_id"], str) or not row[
        "source_max_stable_id"
    ]:
        raise KeepFokPolicyContractError(
            "missing FOK outcome source_max_stable_id"
        )
    if (
        row["fok_book_side"] != fok_action["fok_book_side"]
        or row["fok_limit_price_e4"]
        != fok_action["fok_limit_price_e4"]
        or row["fok_limit_fallback"]
        != fok_action["fok_limit_fallback"]
        or _exact_decimal(
            row["requested_qty_fp"], "FOK requested_qty_fp"
        )
        != state["remaining_inventory_fp"]
    ):
        raise KeepFokPolicyContractError(
            "FLATTEN_FOK frozen order mismatch"
        )
    terminal_type = row["terminal_type"]
    if terminal_type not in FOK_TERMINALS:
        raise KeepFokPolicyContractError(
            f"unknown FLATTEN_FOK terminal {terminal_type!r}"
        )
    decision_elapsed = Decimal(state["elapsed_since_first_fill_ms"])
    planned = _exact_decimal(
        row["planned_effective_elapsed_ms"],
        "FOK planned_effective_elapsed_ms",
    )
    terminal = _exact_decimal(
        row["terminal_elapsed_ms"], "FOK terminal_elapsed_ms"
    )
    if planned != decision_elapsed + Decimal(FOK_EFFECTIVE_LATENCY_MS):
        raise KeepFokPolicyContractError(
            "decision-to-FOK effective latency is not exactly 60ms"
        )
    first_wall = int(state["first_fill_recv_wall_ns"])
    first_mono = int(state["first_fill_recv_mono_ns"])
    if (
        _plain_int(
            row["terminal_wall_ns"],
            "FOK terminal_wall_ns",
            minimum=1,
        )
        != first_wall + _elapsed_ns(terminal, "FOK terminal_elapsed_ms")
        or _plain_int(
            row["terminal_mono_ns"],
            "FOK terminal_mono_ns",
            minimum=1,
        )
        != first_mono + _elapsed_ns(terminal, "FOK terminal_elapsed_ms")
    ):
        raise KeepFokPolicyContractError(
            "FLATTEN_FOK terminal receipt clock does not conserve"
        )
    requested = _exact_decimal(
        row["requested_qty_fp"], "FOK requested_qty_fp"
    )
    first_price = _plain_int(
        row["first_fill_price_e4"],
        "FOK first_fill_price_e4",
        minimum=1,
    )
    first_qty = _exact_decimal(
        row["first_fill_qty_fp"], "FOK first_fill_qty_fp"
    )
    first_fee = _exact_decimal(
        row["first_fill_fee_usd"], "FOK first_fill_fee_usd"
    )
    first_cost = _exact_decimal(
        row["first_leg_cost_basis_usd"],
        "FOK first_leg_cost_basis_usd",
    )
    expected_cost = (
        Decimal(first_price) / Decimal("10000") * first_qty
    )
    if (
        first_cost != expected_cost
        or first_price != state["first_price_e4"]
        or first_qty != state["first_qty_fp"]
        or first_fee != state["first_fill_fee_usd"]
        or first_cost != state["first_leg_cost_basis_usd"]
        or requested != first_qty
        or first_fee != 0
        or row["original_complement_price_e4"]
        != state["complement_price_e4"]
    ):
        raise KeepFokPolicyContractError(
            "FOK first-fill cost/fee spine mismatch"
        )
    complement_qty = _exact_decimal(
        row["complement_fill_qty_fp"], "FOK complement_fill_qty_fp"
    )
    fok_qty = _exact_decimal(
        row["fok_fill_qty_fp"], "FOK fok_fill_qty_fp"
    )
    residual = _exact_decimal(
        row["residual_inventory_fp"], "FOK residual_inventory_fp"
    )
    if (
        min(requested, complement_qty, fok_qty, residual) < 0
        or complement_qty + fok_qty + residual != requested
    ):
        raise KeepFokPolicyContractError(
            "FLATTEN_FOK fill/residual quantity does not conserve"
        )
    gross = _exact_decimal(row["gross_pnl_usd"], "FOK gross_pnl_usd")
    maker_fee = _exact_decimal(
        row["maker_fee_usd"], "FOK maker_fee_usd"
    )
    taker_low = _exact_decimal(
        row["taker_fee_low_usd"], "FOK taker_fee_low_usd"
    )
    taker_base = _exact_decimal(
        row["taker_fee_base_usd"], "FOK taker_fee_base_usd"
    )
    taker_high = _exact_decimal(
        row["taker_fee_high_usd"], "FOK taker_fee_high_usd"
    )
    net_low = _exact_decimal(
        row["net_pnl_fee_low_usd"], "FOK net_pnl_fee_low_usd"
    )
    net_base = _exact_decimal(
        row["net_pnl_fee_base_usd"], "FOK net_pnl_fee_base_usd"
    )
    net_high = _exact_decimal(
        row["net_pnl_fee_high_usd"], "FOK net_pnl_fee_high_usd"
    )
    conservative = _exact_decimal(
        row["conservative_reward_usd"],
        "FOK conservative_reward_usd",
    )
    capital = _exact_decimal(
        row["capital_dollar_seconds"],
        "FOK capital_dollar_seconds",
    )
    if terminal_type == "CANCEL_RACE_PAIR":
        race_fee = _exact_decimal(
            row.get("race_complement_fee_usd"),
            "FOK race_complement_fee_usd",
        )
        if race_fee != 0:
            raise KeepFokPolicyContractError(
                "CANCEL_RACE_PAIR maker fee must be schedule zero"
            )
    else:
        race_fee = None
    if maker_fee != 0:
        raise KeepFokPolicyContractError(
            "FOK maker fee mismatch"
        )
    if (
        min(maker_fee, taker_low, taker_base, taker_high, capital) < 0
        or not taker_low <= taker_base <= taker_high
        or not net_low >= net_base >= net_high
    ):
        raise KeepFokPolicyContractError(
            "FLATTEN_FOK fee-band reward does not conserve"
        )
    if (
        net_low != gross - maker_fee - taker_low
        or net_base != gross - maker_fee - taker_base
        or net_high != gross - maker_fee - taker_high
    ):
        raise KeepFokPolicyContractError(
            "FOK net fee-band mismatch"
        )
    fok_sent = _plain_bool(row["fok_sent"], "FOK fok_sent")
    synthetic_cancel_applied = _plain_bool(
        row["synthetic_cancel_applied_before_terminal"],
        "FOK synthetic_cancel_applied_before_terminal",
    )
    simulation_reward_exact = _plain_bool(
        row["simulation_reward_exact"],
        "FOK simulation_reward_exact",
    )
    conservative_usable = _plain_bool(
        row["conservative_fit_usable"],
        "FOK conservative_fit_usable",
    )
    capital_released = _plain_bool(
        row["capital_released"], "FOK capital_released"
    )
    if _plain_bool(
        row["public_crossing_event_applied"],
        "FOK public_crossing_event_applied",
    ):
        raise KeepFokPolicyContractError(
            "effective-time public crossing event was applied"
        )
    cancel_wall = _optional_timestamp(
        row.get("synthetic_cancel_applied_wall_ns"),
        "FOK synthetic_cancel_applied_wall_ns",
    )
    cancel_mono = _optional_timestamp(
        row.get("synthetic_cancel_applied_mono_ns"),
        "FOK synthetic_cancel_applied_mono_ns",
    )
    processed_wall = _optional_timestamp(
        row.get("fok_processed_wall_ns"), "FOK fok_processed_wall_ns"
    )
    processed_mono = _optional_timestamp(
        row.get("fok_processed_mono_ns"), "FOK fok_processed_mono_ns"
    )
    cancel_sequence = row.get("synthetic_cancel_applied_sequence")
    processed_sequence = row.get("fok_processed_sequence")
    cancel_stable_id = row.get(
        "synthetic_cancel_applied_stable_source_id"
    )
    processed_stable_id = row.get("fok_processed_stable_source_id")
    self_cross_before_fok = row.get(
        "simulated_no_self_cross_verified_before_fok"
    )
    if terminal_type == "CANCEL_RACE_PAIR":
        if (
            terminal >= planned
            or fok_sent
            or synthetic_cancel_applied
            or self_cross_before_fok is not None
            or any(
                value is not None
                for value in (
                    cancel_wall,
                    cancel_mono,
                    processed_wall,
                    processed_mono,
                    cancel_sequence,
                    processed_sequence,
                    cancel_stable_id,
                    processed_stable_id,
                )
            )
            or complement_qty != requested
            or fok_qty != 0
            or residual != 0
        ):
            raise KeepFokPolicyContractError(
                "CANCEL_RACE_PAIR ordering/quantity semantics mismatch"
            )
        if (
            row["reward_value_kind"] != "SIMULATION_EXACT"
            or not simulation_reward_exact
            or not conservative_usable
            or not capital_released
        ):
            raise KeepFokPolicyContractError(
                "CANCEL_RACE_PAIR reward flags mismatch"
            )
        race_price = _plain_int(
            row.get("race_complement_price_e4"),
            "FOK race_complement_price_e4",
            minimum=1,
        )
        race_stable_id = row.get("race_complement_stable_source_id")
        expected_race_gross = (
            (
                Decimal("10000")
                - Decimal(first_price)
                - Decimal(race_price)
            )
            / Decimal("10000")
            * requested
        )
        if (
            gross != expected_race_gross
            or race_price != row["original_complement_price_e4"]
            or row.get("race_fill_provenance") != FILL_PROVENANCE
            or row.get("race_execution_nature") != EXECUTION_NATURE
            or row.get("race_fee_provenance")
            != MAKER_FEE_PROVENANCE
            or not isinstance(race_stable_id, str)
            or not race_stable_id
        ):
            raise KeepFokPolicyContractError(
                "CANCEL_RACE_PAIR public-proxy provenance/gross mismatch"
            )
        if not (
            taker_low == taker_base == taker_high == 0
            and net_low == net_base == net_high == conservative
        ):
            raise KeepFokPolicyContractError(
                "CANCEL_RACE_PAIR conservative reward mismatch"
            )
        expected_capital = (
            first_cost * terminal / Decimal("1000")
        )
        if capital != expected_capital:
            raise KeepFokPolicyContractError(
                "CANCEL_RACE_PAIR capital mismatch"
            )
    elif terminal_type == "FOK_FULL":
        if (
            terminal != planned
            or not fok_sent
            or not synthetic_cancel_applied
            or self_cross_before_fok is not True
            or None
            in (
                cancel_wall,
                cancel_mono,
                processed_wall,
                processed_mono,
            )
            or int(cancel_wall) != int(processed_wall)
            or int(cancel_mono) != int(processed_mono)
            or int(processed_wall) != row["terminal_wall_ns"]
            or int(processed_mono) != row["terminal_mono_ns"]
            or cancel_sequence != 0
            or processed_sequence != 1
            or not isinstance(cancel_stable_id, str)
            or not isinstance(processed_stable_id, str)
            or not cancel_stable_id
            or not processed_stable_id
            or complement_qty != 0
            or fok_qty != requested
            or residual != 0
            or any(
                row.get(field) is not None
                for field in (
                    "race_complement_price_e4",
                    "race_complement_fee_usd",
                    "race_complement_stable_source_id",
                    "race_fill_provenance",
                    "race_execution_nature",
                    "race_fee_provenance",
                )
            )
        ):
            raise KeepFokPolicyContractError(
                "FOK_FULL ordering/quantity semantics mismatch"
            )
        if (
            simulation_reward_exact
            or row["reward_value_kind"] != "FEE_BAND"
            or not conservative_usable
            or not capital_released
        ):
            raise KeepFokPolicyContractError(
                "FOK_FULL reward flags mismatch"
            )
        if conservative != net_high:
            raise KeepFokPolicyContractError(
                "FOK_FULL conservative reward mismatch"
            )
        expected_capital = (
            first_cost * terminal / Decimal("1000")
        )
        if capital != expected_capital:
            raise KeepFokPolicyContractError(
                "FOK_FULL capital mismatch"
            )
    else:
        if (
            terminal != planned
            or not fok_sent
            or not synthetic_cancel_applied
            or self_cross_before_fok is not True
            or None
            in (
                cancel_wall,
                cancel_mono,
                processed_wall,
                processed_mono,
            )
            or int(cancel_wall) != int(processed_wall)
            or int(cancel_mono) != int(processed_mono)
            or int(processed_wall) != row["terminal_wall_ns"]
            or int(processed_mono) != row["terminal_mono_ns"]
            or cancel_sequence != 0
            or processed_sequence != 1
            or not isinstance(cancel_stable_id, str)
            or not isinstance(processed_stable_id, str)
            or not cancel_stable_id
            or not processed_stable_id
            or complement_qty != 0
            or fok_qty != 0
            or residual != requested
            or any(
                row.get(field) is not None
                for field in (
                    "race_complement_price_e4",
                    "race_complement_fee_usd",
                    "race_complement_stable_source_id",
                    "race_fill_provenance",
                    "race_execution_nature",
                    "race_fee_provenance",
                )
            )
        ):
            raise KeepFokPolicyContractError(
                "FOK_ZERO ordering/quantity semantics mismatch"
            )
        if (
            simulation_reward_exact
            or row["reward_value_kind"] != "LOWER_BOUND"
            or not conservative_usable
            or capital_released
        ):
            raise KeepFokPolicyContractError(
                "FOK_ZERO reward flags mismatch"
            )
        expected_zero_reward = -first_cost
        if conservative != expected_zero_reward:
            raise KeepFokPolicyContractError(
                "FOK_ZERO conservative reward mismatch"
            )
        if (
            gross != 0
            or not (
                taker_low == taker_base == taker_high == 0
                and net_low == net_base == net_high == 0
            )
        ):
            raise KeepFokPolicyContractError(
                "FOK_ZERO fee spine mismatch"
            )
        market_close = _exact_decimal(
            row["market_close_elapsed_ms"],
            "FOK market_close_elapsed_ms",
        )
        expected_capital = (
            first_cost * market_close / Decimal("1000")
        )
        if capital != expected_capital:
            raise KeepFokPolicyContractError(
                "FOK_ZERO capital mismatch"
            )
    row.update(
        {
            "planned_effective_elapsed_ms": planned,
            "terminal_elapsed_ms": terminal,
            "first_fill_price_e4": first_price,
            "first_fill_qty_fp": first_qty,
            "first_fill_fee_usd": first_fee,
            "first_leg_cost_basis_usd": first_cost,
            "requested_qty_fp": requested,
            "complement_fill_qty_fp": complement_qty,
            "fok_fill_qty_fp": fok_qty,
            "residual_inventory_fp": residual,
            "gross_pnl_usd": gross,
            "maker_fee_usd": maker_fee,
            "taker_fee_low_usd": taker_low,
            "taker_fee_base_usd": taker_base,
            "taker_fee_high_usd": taker_high,
            "net_pnl_fee_low_usd": net_low,
            "net_pnl_fee_base_usd": net_base,
            "net_pnl_fee_high_usd": net_high,
            "conservative_reward_usd": conservative,
            "capital_dollar_seconds": capital,
            "outcome_source_rows_sha256": _sha256(
                row["outcome_source_rows_sha256"],
                "FOK outcome_source_rows_sha256",
            ),
            "execution_slices_sha256": _sha256(
                row["execution_slices_sha256"],
                "FOK execution_slices_sha256",
            ),
            "reconciliation_ok": True,
            "data_invalid": False,
        }
    )
    return row


def _validate_transition_outcome_rows(
    batch: PostfillDDLBatch,
    states: Mapping[str, Mapping[int, Mapping[str, object]]],
    actions: Mapping[
        str, Mapping[int, Mapping[str, Mapping[str, object]]]
    ],
) -> tuple[
    dict[str, dict[int, dict[str, object]]],
    dict[str, dict[int, dict[str, object]]],
]:
    keep_raw: dict[tuple[str, int], dict[str, object]] = {}
    for raw in batch.postfill_compact_keep_transition:
        key = (
            str(raw["postfill_episode_id"]),
            _plain_int(
                raw["from_decision_index"],
                "KEEP from_decision_index",
            ),
        )
        if key in keep_raw:
            raise KeepFokPolicyContractError(
                "duplicate KEEP transition"
            )
        keep_raw[key] = dict(raw)
    fok_raw: dict[tuple[str, int], dict[str, object]] = {}
    for raw in batch.postfill_compact_flatten_fok_outcome:
        key = (
            str(raw["postfill_episode_id"]),
            _plain_int(raw["decision_index"], "FOK decision_index"),
        )
        if key in fok_raw:
            raise KeepFokPolicyContractError(
                "duplicate FLATTEN_FOK outcome"
            )
        fok_raw[key] = dict(raw)
    expected = {
        (episode_id, index)
        for episode_id, by_grid in states.items()
        for index in by_grid
    }
    if set(keep_raw) != expected or set(fok_raw) != expected:
        raise KeepFokPolicyContractError(
            "KEEP/FOK outcome coverage does not match causal states"
        )
    keep: dict[str, dict[int, dict[str, object]]] = {}
    fok: dict[str, dict[int, dict[str, object]]] = {}
    for episode_id, by_grid in states.items():
        indices = sorted(by_grid)
        for position, index in enumerate(indices):
            next_state = (
                by_grid[indices[position + 1]]
                if position + 1 < len(indices)
                else None
            )
            keep.setdefault(episode_id, {})[index] = (
                _normalize_keep_transition(
                    keep_raw[(episode_id, index)],
                    state=by_grid[index],
                    keep_action=actions[episode_id][index]["KEEP"],
                    next_state=next_state,
                )
            )
            fok.setdefault(episode_id, {})[index] = (
                _normalize_fok_outcome(
                    fok_raw[(episode_id, index)],
                    state=by_grid[index],
                    fok_action=actions[episode_id][index][
                        "FLATTEN_FOK"
                    ],
                )
            )
    return keep, fok


def _validate_episode_outcome(
    raw: Mapping[str, object],
    *,
    batch: PostfillDDLBatch,
    states: Mapping[str, Mapping[int, Mapping[str, object]]],
    transitions: Mapping[str, Mapping[int, Mapping[str, object]]],
    fok_outcomes: Mapping[str, Mapping[int, Mapping[str, object]]],
    atoms: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    row = dict(raw)
    required = (
        "data_origin",
        "source_date_utc",
        "market_cluster_id",
        "postfill_episode_id",
        "first_fill_side",
        "terminal_elapsed_ms",
        "terminal_type",
        "zero_time_atom",
        "first_leg_cost_basis_usd",
        "first_fill_fee_usd",
        "capital_locked_usd",
        "market_close_elapsed_ms",
        "pre_first_fill_capital_dollar_seconds",
        "historical_current_outcome",
        "overlapping_episode",
        "reconciliation_ok",
        "data_invalid",
    )
    _required(row, required, "synthetic episode outcome")
    if row["data_origin"] != "SYNTHETIC":
        raise KeepFokPolicyContractError(
            "only explicit SYNTHETIC outcomes are accepted"
        )
    day = str(row["source_date_utc"])
    episode_id = str(row["postfill_episode_id"])
    if (
        day not in batch.source_dates
        or not episode_id
        or not str(row["market_cluster_id"])
    ):
        raise KeepFokPolicyContractError(
            "episode outcome identity is invalid"
        )
    first_side = row["first_fill_side"]
    if first_side not in ("YES", "NO"):
        raise KeepFokPolicyContractError("invalid first_fill_side")
    zero = _plain_bool(row["zero_time_atom"], "zero_time_atom")
    overlap = _plain_bool(
        row["overlapping_episode"], "overlapping_episode"
    )
    if (
        not _plain_bool(
            row["reconciliation_ok"], "reconciliation_ok"
        )
        or _plain_bool(row["data_invalid"], "data_invalid")
    ):
        raise KeepFokPolicyContractError(
            "non-green outcome requires market-day rollback"
        )
    terminal = _exact_decimal(
        row["terminal_elapsed_ms"], "terminal_elapsed_ms"
    )
    first_cost = _exact_decimal(
        row["first_leg_cost_basis_usd"],
        "first_leg_cost_basis_usd",
    )
    first_fee = _exact_decimal(
        row["first_fill_fee_usd"], "first_fill_fee_usd"
    )
    capital_locked = _exact_decimal(
        row["capital_locked_usd"], "capital_locked_usd"
    )
    close_elapsed = _exact_decimal(
        row["market_close_elapsed_ms"], "market_close_elapsed_ms"
    )
    pre_capital = _exact_decimal(
        row["pre_first_fill_capital_dollar_seconds"],
        "pre_first_fill_capital_dollar_seconds",
    )
    if (
        min(first_cost, pre_capital) < 0
        or first_fee != 0
        or capital_locked <= 0
        or capital_locked != first_cost
        or close_elapsed < terminal
    ):
        raise KeepFokPolicyContractError(
            "episode capital/cost basis does not conserve"
        )
    if zero:
        atom = atoms.get(episode_id)
        if (
            atom is None
            or episode_id in states
            or terminal != 0
            or row["terminal_type"] != "ZERO_TIME_ATOM"
            or atom["first_fill_side"] != first_side
        ):
            raise KeepFokPolicyContractError(
                "zero-time outcome/atom linkage failed"
            )
        first_wall = int(atom["atom_recv_wall_ns"])
        first_mono = int(atom["atom_recv_mono_ns"])
    else:
        by_grid = states.get(episode_id)
        if not by_grid or episode_id in atoms or terminal <= 0:
            raise KeepFokPolicyContractError(
                "continuous outcome/state linkage failed"
            )
        expected_grid = tuple(
            index
            for index, _elapsed in required_decision_grid(
                terminal, zero_time_atom=False
            )
        )
        if tuple(sorted(by_grid)) != expected_grid:
            raise KeepFokPolicyContractError(
                "continuous outcome decision grid mismatch"
            )
        first_state = by_grid[expected_grid[0]]
        if (
            first_state["first_side"] != first_side
            or first_state["first_leg_cost_basis_usd"] != first_cost
            or first_state["first_fill_fee_usd"] != first_fee
        ):
            raise KeepFokPolicyContractError(
                "outcome/state first-fill spine mismatch"
            )
        first_wall = int(first_state["first_fill_recv_wall_ns"])
        first_mono = int(first_state["first_fill_recv_mono_ns"])
        last = transitions[episode_id][expected_grid[-1]]
        if (
            last["transition_type"] != "KEEP_TO_TERMINAL"
            or last["interval_stop_elapsed_ms"] != terminal
            or last["keep_terminal_type"] != row["terminal_type"]
            or row["terminal_type"]
            not in ("COMPLEMENT_FILL", "HARD_FALLBACK")
        ):
            raise KeepFokPolicyContractError(
                "eventual KEEP terminal linkage mismatch"
            )
        lower_bound = -first_cost
        zero_capital = (
            first_cost * close_elapsed / Decimal("1000")
        )
        for receipt in fok_outcomes[episode_id].values():
            if receipt["terminal_type"] != "FOK_ZERO":
                expected_capital = (
                    first_cost
                    * receipt["terminal_elapsed_ms"]
                    / Decimal("1000")
                )
                if (
                    receipt["capital_dollar_seconds"]
                    != expected_capital
                ):
                    raise KeepFokPolicyContractError(
                        "released FOK/race postfill capital-time "
                        "does not conserve"
                    )
                continue
            if (
                receipt["conservative_reward_usd"] != lower_bound
                or receipt["capital_dollar_seconds"] != zero_capital
            ):
                raise KeepFokPolicyContractError(
                    "FOK_ZERO first-leg lower bound/capital-to-close "
                    "does not conserve"
                )
    historical = _normalize_historical_current(
        row["historical_current_outcome"],
        first_fill_wall_ns=first_wall,
        first_fill_mono_ns=first_mono,
        zero_time_atom=zero,
    )
    row.update(
        {
            "source_date_utc": day,
            "postfill_episode_id": episode_id,
            "terminal_elapsed_ms": terminal,
            "first_leg_cost_basis_usd": first_cost,
            "first_fill_fee_usd": first_fee,
            "capital_locked_usd": capital_locked,
            "market_close_elapsed_ms": close_elapsed,
            "pre_first_fill_capital_dollar_seconds": pre_capital,
            "historical_current_outcome": historical,
            "overlapping_episode": overlap,
            "zero_time_atom": zero,
        }
    )
    return row


def validate_synthetic_dataset(
    dataset: SyntheticKeepFokDataset,
) -> tuple[_Trajectory, ...]:
    """Validate every path, row, linkage and seal before fitting."""
    if dataset.marker != SYNTHETIC_MARKER:
        raise KeepFokPolicyContractError("synthetic marker mismatch")
    _sha256(dataset.expected_batch_sha256, "expected_batch_sha256")
    _sha256(
        dataset.expected_outcomes_sha256,
        "expected_outcomes_sha256",
    )
    _preflight_batch_paths(dataset.batch)
    if dataset.batch.canonical_sha256() != dataset.expected_batch_sha256:
        raise KeepFokPolicyContractError(
            "PostfillDDLBatch row hash mismatch"
        )
    if (
        seal_episode_outcomes(dataset.episode_outcomes)
        != dataset.expected_outcomes_sha256
    ):
        raise KeepFokPolicyContractError(
            "episode outcome receipt hash mismatch"
        )
    states, actions, atoms = _validate_state_action_rows(dataset.batch)
    transitions, fok_outcomes = _validate_transition_outcome_rows(
        dataset.batch, states, actions
    )
    outcomes: dict[str, dict[str, object]] = {}
    for raw in dataset.episode_outcomes:
        row = _validate_episode_outcome(
            raw,
            batch=dataset.batch,
            states=states,
            transitions=transitions,
            fok_outcomes=fok_outcomes,
            atoms=atoms,
        )
        episode_id = str(row["postfill_episode_id"])
        if episode_id in outcomes:
            raise KeepFokPolicyContractError(
                "duplicate episode outcome"
            )
        outcomes[episode_id] = row
    expected_ids = set(states) | set(atoms)
    if (
        set(outcomes) != expected_ids
        or len(outcomes) != dataset.batch.episode_count
    ):
        raise KeepFokPolicyContractError(
            "outcomes do not cover every validated episode"
        )
    return tuple(
        _Trajectory(
            outcome=outcomes[episode_id],
            states=states.get(episode_id, {}),
            actions=actions.get(episode_id, {}),
            keep_transitions=transitions.get(episode_id, {}),
            fok_outcomes=fok_outcomes.get(episode_id, {}),
        )
        for episode_id in sorted(outcomes)
    )


def _fok_reward(
    trajectory: _Trajectory,
    grid_index: int,
    fee_band: str,
) -> float:
    """Return the sealed counterfactual label, never a state estimate."""
    receipt = trajectory.fok_outcomes[grid_index]
    if receipt["terminal_type"] == "FOK_ZERO":
        if (
            receipt["reward_value_kind"] != "LOWER_BOUND"
            or receipt["conservative_fit_usable"] is not True
        ):
            raise KeepFokPolicyContractError(
                "unusable FOK_ZERO label reached fit"
            )
        return float(receipt["conservative_reward_usd"])
    suffix = _band_suffix(fee_band)
    return float(receipt[f"net_pnl_fee_{suffix}_usd"])


def _keep_immediate_reward(
    trajectory: _Trajectory,
    grid_index: int,
) -> float:
    return float(
        trajectory.keep_transitions[grid_index][
            "immediate_net_pnl_usd"
        ]
    )


def _next_grid(
    trajectory: _Trajectory,
    grid_index: int,
) -> int | None:
    transition = trajectory.keep_transitions[grid_index]
    if transition["transition_type"] != "NEXT_STATE":
        return None
    next_id = transition["next_decision_id"]
    matches = [
        index
        for index, state in trajectory.states.items()
        if state["postfill_decision_id"] == next_id
    ]
    if len(matches) != 1:
        raise KeepFokPolicyContractError(
            "KEEP transition next-state identity is not unique"
        )
    return matches[0]


def _fit_ridge(
    *,
    grid_index: int,
    action_kind: str,
    fee_band: str,
    alpha: float,
    states: Sequence[Mapping[str, object]],
    targets: Sequence[float],
    label_lineage_rows: Sequence[Mapping[str, object]],
    continuation_lineage_rows: Sequence[Mapping[str, object]] = (),
) -> RidgeQModel:
    if alpha not in RIDGE_GRID:
        raise KeepFokPolicyContractError(
            "alpha outside sealed ridge grid"
        )
    if (
        action_kind not in ALLOWED_ACTIONS
        or not states
        or len(states) != len(targets)
        or len(states) != len(label_lineage_rows)
    ):
        raise KeepFokPolicyContractError(
            "invalid fitted-Q train rows"
        )
    features = [
        causal_feature_snapshot(state, fee_band) for state in states
    ]
    scaler = FoldScaler.fit(features)
    x_scaled = np.vstack(
        [scaler.transform(row) for row in features]
    )
    x = np.column_stack(
        (np.ones(len(x_scaled), dtype=float), x_scaled)
    )
    y = np.asarray(targets, dtype=float)
    if not np.all(np.isfinite(y)):
        raise KeepFokPolicyContractError(
            "non-finite fitted-Q targets"
        )
    penalty = np.eye(x.shape[1], dtype=float) * float(alpha)
    penalty[0, 0] = 0.0
    lhs = np.einsum("ni,nj->ij", x, x) + penalty
    rhs = np.einsum("ni,n->i", x, y)
    try:
        beta = np.linalg.solve(lhs, rhs)
    except np.linalg.LinAlgError:
        beta = np.linalg.pinv(lhs) @ rhs
    if not np.all(np.isfinite(beta)):
        raise KeepFokPolicyContractError(
            "non-finite ridge coefficients"
        )
    return RidgeQModel(
        grid_index=grid_index,
        action_kind=action_kind,
        fee_band=fee_band,
        alpha=float(alpha),
        scaler=scaler,
        intercept=float(beta[0]),
        coefficients=np.asarray(beta[1:], dtype=float),
        train_rows=len(states),
        train_source_hashes_sha256=_canonical_sha256(
            sorted(
                str(state["causal_source_rows_sha256"])
                for state in states
            )
        ),
        feature_lineage_sha256=_canonical_sha256(
            {
                "contract_version": CONTRACT_VERSION,
                "sealed_source_contract_sha256": (
                    SEALED_SOURCE_CONTRACT_SHA256
                ),
                "feature_names": CURRENT_FEATURES,
                "rows": [
                    {
                        "postfill_decision_id": state[
                            "postfill_decision_id"
                        ],
                        "causal_source_rows_sha256": state[
                            "causal_source_rows_sha256"
                        ],
                        "paired_state_fingerprint_sha256": state[
                            "paired_state_fingerprint_sha256"
                        ],
                        "feature_asof_wall_ns": state[
                            "feature_asof_wall_ns"
                        ],
                        "data_origin": state["data_origin"],
                        "first_fill_provenance": state[
                            "first_fill_provenance"
                        ],
                        "first_fill_execution_nature": state[
                            "first_fill_execution_nature"
                        ],
                        "first_fill_fee_provenance": state[
                            "first_fill_fee_provenance"
                        ],
                        "flatten_counterfactual_provenance": state[
                            "flatten_counterfactual_provenance"
                        ],
                        "simulated_strategy_order_registry_sha256": (
                            state[
                                "simulated_strategy_order_registry_sha256"
                            ]
                        ),
                    }
                    for state in states
                ],
            }
        ),
        label_lineage_sha256=_canonical_sha256(
            tuple(label_lineage_rows)
        ),
        continuation_label_lineage_sha256=_canonical_sha256(
            tuple(continuation_lineage_rows)
        ),
    )


def _fit_backward_policy(
    trajectories: Sequence[_Trajectory],
    *,
    fee_band: str,
    alpha: float,
) -> BackwardPolicy:
    """Fit both action values backwards using train-only state rows."""
    train = [
        trajectory
        for trajectory in trajectories
        if not trajectory.overlap
        and not trajectory.outcome["zero_time_atom"]
    ]
    if not train:
        raise KeepFokPolicyContractError(
            "no non-overlapping continuous train episodes"
        )
    keep_models: dict[int, RidgeQModel] = {}
    fok_models: dict[int, RidgeQModel] = {}
    grid_indices = sorted(
        {
            index
            for trajectory in train
            for index in trajectory.states
        },
        reverse=True,
    )
    for index in grid_indices:
        at_grid = [
            trajectory
            for trajectory in train
            if index in trajectory.states
        ]
        states = [trajectory.states[index] for trajectory in at_grid]
        fok_models[index] = _fit_ridge(
            grid_index=index,
            action_kind="FLATTEN_FOK",
            fee_band=fee_band,
            alpha=alpha,
            states=states,
            targets=[
                _fok_reward(trajectory, index, fee_band)
                for trajectory in at_grid
            ],
            label_lineage_rows=[
                {
                    "postfill_action_id": trajectory.fok_outcomes[
                        index
                    ]["postfill_action_id"],
                    "outcome_source_rows_sha256": (
                        trajectory.fok_outcomes[index][
                            "outcome_source_rows_sha256"
                        ]
                    ),
                    "terminal_type": trajectory.fok_outcomes[index][
                        "terminal_type"
                    ],
                    "reward_value_kind": (
                        trajectory.fok_outcomes[index][
                            "reward_value_kind"
                        ]
                    ),
                    "simulation_reward_exact": (
                        trajectory.fok_outcomes[index][
                            "simulation_reward_exact"
                        ]
                    ),
                    "fok_execution_provenance": (
                        trajectory.fok_outcomes[index][
                            "fok_execution_provenance"
                        ]
                    ),
                    "cancel_timing_provenance": (
                        trajectory.fok_outcomes[index][
                            "cancel_timing_provenance"
                        ]
                    ),
                    "execution_slices_sha256": (
                        trajectory.fok_outcomes[index][
                            "execution_slices_sha256"
                        ]
                    ),
                    "fee_band": fee_band,
                    "fitted_label_usd": (
                        trajectory.fok_outcomes[index][
                            "conservative_reward_usd"
                        ]
                        if trajectory.fok_outcomes[index][
                            "terminal_type"
                        ]
                        == "FOK_ZERO"
                        else trajectory.fok_outcomes[index][
                            f"net_pnl_fee_{_band_suffix(fee_band)}_usd"
                        ]
                    ),
                }
                for trajectory in at_grid
            ],
        )
        keep_targets = []
        keep_label_lineage = []
        continuation_lineage = []
        for trajectory in at_grid:
            transition = trajectory.keep_transitions[index]
            immediate = _keep_immediate_reward(trajectory, index)
            keep_label_lineage.append(
                {
                    "postfill_keep_transition_id": transition[
                        "postfill_keep_transition_id"
                    ],
                    "source_rows_sha256": transition[
                        "source_rows_sha256"
                    ],
                    "transition_type": transition[
                        "transition_type"
                    ],
                    "keep_terminal_type": transition[
                        "keep_terminal_type"
                    ],
                    "keep_execution_provenance": transition[
                        "keep_execution_provenance"
                    ],
                    "terminal_execution_provenance": transition[
                        "terminal_execution_provenance"
                    ],
                    "terminal_fee_provenance": transition[
                        "terminal_fee_provenance"
                    ],
                    "simulation_reward_exact": transition[
                        "simulation_reward_exact"
                    ],
                    "immediate_net_pnl_usd": transition[
                        "immediate_net_pnl_usd"
                    ],
                }
            )
            next_index = _next_grid(trajectory, index)
            if next_index is None:
                keep_targets.append(immediate)
                continuation_lineage.append(
                    {
                        "postfill_episode_id": trajectory.episode_id,
                        "kind": "PUBLIC_PROXY_OR_SETTLEMENT_TERMINAL",
                        "keep_terminal_type": transition[
                            "keep_terminal_type"
                        ],
                        "source_rows_sha256": transition[
                            "source_rows_sha256"
                        ],
                    }
                )
                continue
            next_keep_model = keep_models.get(next_index)
            next_fok_model = fok_models.get(next_index)
            if next_keep_model is None or next_fok_model is None:
                raise KeepFokPolicyContractError(
                    "backward fitted-Q continuation model missing"
                )
            next_state = trajectory.states[next_index]
            continuation = max(
                next_keep_model.predict(next_state),
                next_fok_model.predict(next_state),
            )
            keep_targets.append(immediate + continuation)
            continuation_lineage.append(
                {
                    "postfill_episode_id": trajectory.episode_id,
                    "kind": "FITTED_NEXT_STATE_MAX",
                    "next_decision_id": next_state[
                        "postfill_decision_id"
                    ],
                    "next_causal_source_rows_sha256": next_state[
                        "causal_source_rows_sha256"
                    ],
                    "next_keep_model_sha256": (
                        next_keep_model.receipt()["model_sha256"]
                    ),
                    "next_keep_label_lineage_sha256": (
                        next_keep_model.label_lineage_sha256
                    ),
                    "next_fok_model_sha256": (
                        next_fok_model.receipt()["model_sha256"]
                    ),
                    "next_fok_label_lineage_sha256": (
                        next_fok_model.label_lineage_sha256
                    ),
                }
            )
        keep_models[index] = _fit_ridge(
            grid_index=index,
            action_kind="KEEP",
            fee_band=fee_band,
            alpha=alpha,
            states=states,
            targets=keep_targets,
            label_lineage_rows=keep_label_lineage,
            continuation_lineage_rows=continuation_lineage,
        )
    return BackwardPolicy(
        fee_band=fee_band,
        alpha=float(alpha),
        train_dates=tuple(sorted({row.source_date for row in train})),
        keep_models=keep_models,
        fok_models=fok_models,
    )


def _oracle_keep_value(
    trajectory: _Trajectory,
    grid_index: int,
    fee_band: str,
    memo: dict[int, float] | None = None,
) -> float:
    """Diagnostic realized KEEP value; never used for OOS selection."""
    cache = {} if memo is None else memo
    if grid_index in cache:
        return cache[grid_index]
    immediate = _keep_immediate_reward(trajectory, grid_index)
    next_index = _next_grid(trajectory, grid_index)
    if next_index is None:
        cache[grid_index] = immediate
    else:
        cache[grid_index] = immediate + max(
            _oracle_keep_value(
                trajectory, next_index, fee_band, cache
            ),
            _fok_reward(trajectory, next_index, fee_band),
        )
    return cache[grid_index]


def _select_alpha_inner(
    trajectories: Sequence[_Trajectory],
    *,
    fee_band: str,
) -> tuple[float, dict[str, object]]:
    days = tuple(
        sorted(
            {
                row.source_date
                for row in trajectories
                if not row.overlap
            }
        )
    )
    if len(days) != 2:
        raise KeepFokPolicyContractError(
            "outer training fold must contain exactly two dates"
        )
    scores: dict[float, float] = {}
    fold_scores: dict[str, dict[str, float]] = {}
    for alpha in RIDGE_GRID:
        squared_errors: list[float] = []
        per_day: dict[str, float] = {}
        for inner_holdout in days:
            inner_train = [
                row
                for row in trajectories
                if row.source_date != inner_holdout
                and not row.overlap
            ]
            validation = [
                row
                for row in trajectories
                if row.source_date == inner_holdout
                and not row.overlap
                and not row.outcome["zero_time_atom"]
            ]
            policy = _fit_backward_policy(
                inner_train,
                fee_band=fee_band,
                alpha=alpha,
            )
            day_errors = []
            for trajectory in validation:
                for index, state in trajectory.states.items():
                    keep_model = policy.keep_models.get(index)
                    fok_model = policy.fok_models.get(index)
                    if keep_model is None or fok_model is None:
                        continue
                    keep_error = (
                        keep_model.predict(state)
                        - _oracle_keep_value(
                            trajectory, index, fee_band
                        )
                    )
                    fok_error = (
                        fok_model.predict(state)
                        - _fok_reward(
                            trajectory, index, fee_band
                        )
                    )
                    day_errors.extend(
                        (keep_error * keep_error, fok_error * fok_error)
                    )
            if not day_errors:
                raise KeepFokPolicyContractError(
                    "inner validation has no comparable state/action rows"
                )
            per_day[inner_holdout] = float(np.mean(day_errors))
            squared_errors.extend(day_errors)
        scores[alpha] = float(np.mean(squared_errors))
        fold_scores[str(alpha)] = per_day
    selected = min(RIDGE_GRID, key=lambda value: (scores[value], value))
    return selected, {
        "method": "two-day inner leave-one-date-out action-value MSE",
        "selected_alpha": selected,
        "scores": {
            str(alpha): scores[alpha] for alpha in RIDGE_GRID
        },
        "per_inner_holdout": fold_scores,
        "ridge_grid": list(RIDGE_GRID),
        "train_only_scaler_and_ridge": True,
    }


def _result(
    trajectory: _Trajectory,
    policy_name: str,
    fee_band: str,
    net_pnl_usd: float,
    capital_dollar_seconds: float,
    terminal_action: str,
    terminal_type: str,
    terminal_elapsed_ms: float,
    trace: Sequence[Mapping[str, object]],
    *,
    maker_completion: bool,
    simulation_reward_exact: bool,
    reward_value_kind: str,
    selected_fok_zero: bool = False,
) -> dict[str, object]:
    if not all(
        math.isfinite(value)
        for value in (
            net_pnl_usd,
            capital_dollar_seconds,
            terminal_elapsed_ms,
        )
    ) or capital_dollar_seconds < 0:
        raise KeepFokPolicyContractError(
            "non-finite/negative execution result"
        )
    return {
        "postfill_episode_id": trajectory.episode_id,
        "source_date_utc": trajectory.source_date,
        "market_cluster_id": trajectory.cluster_id,
        "first_fill_side": trajectory.outcome["first_fill_side"],
        "policy_name": policy_name,
        "fee_band": fee_band,
        "net_pnl_usd": net_pnl_usd,
        "capital_dollar_seconds": capital_dollar_seconds,
        "terminal_action": terminal_action,
        "terminal_type": terminal_type,
        "terminal_elapsed_ms": terminal_elapsed_ms,
        "maker_completion": maker_completion,
        "simulation_reward_exact": simulation_reward_exact,
        "reward_value_kind": reward_value_kind,
        "selected_fok_zero": selected_fok_zero,
        "trace": [dict(row) for row in trace],
    }


def _execute_zero_atom(
    trajectory: _Trajectory,
    fee_band: str,
    policy_name: str,
) -> dict[str, object]:
    current = trajectory.outcome["historical_current_outcome"]
    suffix = _band_suffix(fee_band)
    return _result(
        trajectory,
        policy_name,
        fee_band,
        float(current[f"realized_pnl_fee_{suffix}_usd"]),
        float(current["postfill_capital_dollar_seconds"]),
        "ZERO_TIME_ATOM",
        "ZERO_TIME_ATOM",
        0.0,
        (),
        maker_completion=True,
        simulation_reward_exact=True,
        reward_value_kind="SIMULATION_EXACT",
    )


def _fok_result(
    trajectory: _Trajectory,
    grid_index: int,
    fee_band: str,
    policy_name: str,
    trace: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    receipt = trajectory.fok_outcomes[grid_index]
    terminal_type = str(receipt["terminal_type"])
    return _result(
        trajectory,
        policy_name,
        fee_band,
        _fok_reward(trajectory, grid_index, fee_band),
        float(receipt["capital_dollar_seconds"]),
        "FLATTEN_FOK",
        terminal_type,
        float(receipt["terminal_elapsed_ms"]),
        trace,
        maker_completion=terminal_type == "CANCEL_RACE_PAIR",
        simulation_reward_exact=bool(
            receipt["simulation_reward_exact"]
        ),
        reward_value_kind=str(receipt["reward_value_kind"]),
        selected_fok_zero=terminal_type == "FOK_ZERO",
    )


def _execute_learned(
    trajectory: _Trajectory,
    policy: BackwardPolicy,
) -> dict[str, object]:
    """Sequential held-out execution using predictions only for choice."""
    if trajectory.outcome["zero_time_atom"]:
        return _execute_zero_atom(
            trajectory, policy.fee_band, "FITTED_Q"
        )
    trace: list[dict[str, object]] = []
    capital_accumulated = 0.0
    net_accumulated = 0.0
    index = min(trajectory.states)
    while True:
        state = trajectory.states[index]
        keep_model = policy.keep_models.get(index)
        fok_model = policy.fok_models.get(index)
        if keep_model is None or fok_model is None:
            action = "KEEP"
            q_keep = q_fok = None
            fallback_reason = "TRAIN_GRID_MODEL_MISSING_KEEP"
        else:
            q_keep = keep_model.predict(state)
            q_fok = fok_model.predict(state)
            action = (
                "FLATTEN_FOK" if q_fok > q_keep else "KEEP"
            )
            fallback_reason = None
        trace.append(
            {
                "decision_index": index,
                "decision_elapsed_ms": state[
                    "elapsed_since_first_fill_ms"
                ],
                "feature_asof_wall_ns": state[
                    "feature_asof_wall_ns"
                ],
                "decision_recv_wall_ns": state[
                    "decision_recv_wall_ns"
                ],
                "action": action,
                "predicted_q_keep_usd": q_keep,
                "predicted_q_flatten_fok_usd": q_fok,
                "fallback_reason": fallback_reason,
                "heldout_outcome_used_for_action_selection": False,
            }
        )
        if action == "FLATTEN_FOK":
            return _fok_result(
                trajectory,
                index,
                policy.fee_band,
                "FITTED_Q",
                trace,
            )
        transition = trajectory.keep_transitions[index]
        net_accumulated += float(
            transition["immediate_net_pnl_usd"]
        )
        capital_accumulated += float(
            transition["capital_dollar_seconds_increment"]
        )
        next_index = _next_grid(trajectory, index)
        if next_index is None:
            observed_terminal = str(
                transition["keep_terminal_type"]
            )
            return _result(
                trajectory,
                "FITTED_Q",
                policy.fee_band,
                net_accumulated,
                capital_accumulated,
                "KEEP",
                observed_terminal,
                float(transition["interval_stop_elapsed_ms"]),
                trace,
                maker_completion=(
                    observed_terminal == "COMPLEMENT_FILL"
                ),
                simulation_reward_exact=True,
                reward_value_kind="SIMULATION_EXACT",
            )
        index = next_index


def _execute_current(
    trajectory: _Trajectory,
    fee_band: str,
) -> dict[str, object]:
    """Use the sealed CURRENT comparator receipt; never grid-snap it."""
    if trajectory.outcome["zero_time_atom"]:
        return _execute_zero_atom(trajectory, fee_band, "CURRENT")
    current = trajectory.outcome["historical_current_outcome"]
    suffix = _band_suffix(fee_band)
    maker_completion = (
        current["terminal_type"] in ("PAIR_COMPLETE", "COMPLEMENT_FILL")
        or current["terminal_route"] == "MAKER_COMPLEMENT"
    )
    return _result(
        trajectory,
        "CURRENT",
        fee_band,
        float(current[f"realized_pnl_fee_{suffix}_usd"]),
        float(current["postfill_capital_dollar_seconds"]),
        "SEALED_CURRENT_COMPARATOR",
        str(current["terminal_type"]),
        float(current["terminal_elapsed_ms"]),
        (
            {
                "policy_row_id": current["policy_row_id"],
                "policy_definition_sha256": current[
                    "policy_definition_sha256"
                ],
                "comparator_provenance": current[
                    "comparator_provenance"
                ],
                "source_rows_sha256": current[
                    "source_rows_sha256"
                ],
                "sealed_comparator_receipt": True,
                "grid_snapped": False,
            },
        ),
        maker_completion=maker_completion,
        simulation_reward_exact=False,
        reward_value_kind="COMPARATOR_RECEIPT",
    )


def _execute_null_keep(
    trajectory: _Trajectory,
    fee_band: str,
) -> dict[str, object]:
    if trajectory.outcome["zero_time_atom"]:
        return _execute_zero_atom(
            trajectory, fee_band, "NULL_ALWAYS_KEEP"
        )
    trace = []
    net = 0.0
    capital = 0.0
    for index in sorted(trajectory.states):
        state = trajectory.states[index]
        transition = trajectory.keep_transitions[index]
        trace.append(
            {
                "decision_index": index,
                "decision_elapsed_ms": state[
                    "elapsed_since_first_fill_ms"
                ],
                "action": "KEEP",
            }
        )
        net += float(transition["immediate_net_pnl_usd"])
        capital += float(
            transition["capital_dollar_seconds_increment"]
        )
        if transition["transition_type"] != "NEXT_STATE":
            observed_terminal = str(
                transition["keep_terminal_type"]
            )
            return _result(
                trajectory,
                "NULL_ALWAYS_KEEP",
                fee_band,
                net,
                capital,
                "KEEP",
                observed_terminal,
                float(transition["interval_stop_elapsed_ms"]),
                trace,
                maker_completion=(
                    observed_terminal == "COMPLEMENT_FILL"
                ),
                simulation_reward_exact=True,
                reward_value_kind="SIMULATION_EXACT",
            )
    raise KeepFokPolicyContractError(
        "NULL_ALWAYS_KEEP reached no terminal transition"
    )


def _execute_clairvoyant(
    trajectory: _Trajectory,
    fee_band: str,
) -> dict[str, object]:
    """Future-aware diagnostic upper comparator; never a policy."""
    if trajectory.outcome["zero_time_atom"]:
        return _execute_zero_atom(
            trajectory, fee_band, "CLAIRVOYANT_UPPER_BOUND"
        )
    null = _execute_null_keep(trajectory, fee_band)
    choices: list[
        tuple[float, str, int | None, dict[str, object]]
    ] = [
        (
            float(null["net_pnl_usd"]),
            "KEEP",
            None,
            null,
        )
    ]
    for index in sorted(trajectory.states):
        result = _fok_result(
            trajectory,
            index,
            fee_band,
            "CLAIRVOYANT_UPPER_BOUND",
            (
                {
                    "decision_index": index,
                    "action": "FLATTEN_FOK",
                    "diagnostic_only": True,
                },
            ),
        )
        choices.append(
            (
                float(result["net_pnl_usd"]),
                "FLATTEN_FOK",
                index,
                result,
            )
        )
    _value, _action, _index, best = max(
        choices,
        key=lambda item: (
            item[0],
            item[1] == "KEEP",
            -(item[2] or 0),
        ),
    )
    return {
        **best,
        "policy_name": "CLAIRVOYANT_UPPER_BOUND",
    }


def _metric_point(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, float | int | None]:
    if not rows:
        return {
            "first_fills": 0,
            "total_net_pnl_usd": 0.0,
            "conditional_ev_per_first_fill_usd": None,
            "postfill_capital_dollar_seconds": 0.0,
            "conditional_cents_per_postfill_locked_dollar_hour": None,
        }
    pnl = sum(float(row["net_pnl_usd"]) for row in rows)
    capital = sum(
        float(row["capital_dollar_seconds"]) for row in rows
    )
    return {
        "first_fills": len(rows),
        "total_net_pnl_usd": pnl,
        "conditional_ev_per_first_fill_usd": pnl / len(rows),
        "postfill_capital_dollar_seconds": capital,
        "conditional_cents_per_postfill_locked_dollar_hour": (
            None
            if capital <= 0
            else 100.0 * 3600.0 * pnl / capital
        ),
    }


def _cluster_jackknife_ci(
    rows: Sequence[Mapping[str, object]],
    metric: str,
) -> dict[str, float | int | str | None]:
    point = _metric_point(rows)[metric]
    clusters = sorted(
        {str(row["market_cluster_id"]) for row in rows}
    )
    result: dict[str, float | int | str | None] = {
        "method": "market-cluster delete-one jackknife",
        "cluster_count": len(clusters),
        "lower_95": None,
        "upper_95": None,
    }
    if point is None or len(clusters) < 3:
        return result
    leave_one = []
    for cluster in clusters:
        value = _metric_point(
            [
                row
                for row in rows
                if str(row["market_cluster_id"]) != cluster
            ]
        )[metric]
        if value is None:
            return result
        leave_one.append(float(value))
    mean_leave = float(np.mean(leave_one))
    se = math.sqrt(
        (len(clusters) - 1)
        / len(clusters)
        * sum((value - mean_leave) ** 2 for value in leave_one)
    )
    result["lower_95"] = float(point) - 1.96 * se
    result["upper_95"] = float(point) + 1.96 * se
    return result


def _economic_report(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        **_metric_point(rows),
        "market_cluster_ci": {
            "conditional_ev_per_first_fill_usd": (
                _cluster_jackknife_ci(
                    rows, "conditional_ev_per_first_fill_usd"
                )
            ),
            "conditional_cents_per_postfill_locked_dollar_hour": (
                _cluster_jackknife_ci(
                    rows,
                    "conditional_cents_per_postfill_locked_dollar_hour",
                )
            ),
        },
    }


def _paired_report(
    learned: Sequence[Mapping[str, object]],
    current: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    learned_by_id = {
        str(row["postfill_episode_id"]): row for row in learned
    }
    current_by_id = {
        str(row["postfill_episode_id"]): row for row in current
    }
    if set(learned_by_id) != set(current_by_id):
        raise KeepFokPolicyContractError(
            "learned/CURRENT paired episode set mismatch"
        )
    delta_rows = []
    for episode_id in sorted(learned_by_id):
        left = learned_by_id[episode_id]
        right = current_by_id[episode_id]
        delta_rows.append(
            {
                "postfill_episode_id": episode_id,
                "market_cluster_id": left["market_cluster_id"],
                "net_pnl_usd": float(left["net_pnl_usd"])
                - float(right["net_pnl_usd"]),
                # The paired capital-normalized estimand is computed
                # separately below; this field supports the PnL CI.
                "capital_dollar_seconds": float(
                    left["capital_dollar_seconds"]
                ),
            }
        )
    learned_point = _metric_point(learned)
    current_point = _metric_point(current)
    left_cap = learned_point[
        "conditional_cents_per_postfill_locked_dollar_hour"
    ]
    right_cap = current_point[
        "conditional_cents_per_postfill_locked_dollar_hour"
    ]
    cap_delta = (
        None
        if left_cap is None or right_cap is None
        else float(left_cap) - float(right_cap)
    )
    clusters = sorted(
        {str(row["market_cluster_id"]) for row in learned}
    )
    cap_leave_one = []
    for cluster in clusters:
        left = _metric_point(
            [
                row
                for row in learned
                if str(row["market_cluster_id"]) != cluster
            ]
        )["conditional_cents_per_postfill_locked_dollar_hour"]
        right = _metric_point(
            [
                row
                for row in current
                if str(row["market_cluster_id"]) != cluster
            ]
        )["conditional_cents_per_postfill_locked_dollar_hour"]
        if left is None or right is None:
            cap_leave_one = []
            break
        cap_leave_one.append(float(left) - float(right))
    cap_ci: dict[str, object] = {
        "method": "market-cluster delete-one jackknife",
        "cluster_count": len(clusters),
        "lower_95": None,
        "upper_95": None,
    }
    if (
        cap_delta is not None
        and len(clusters) >= 3
        and len(cap_leave_one) == len(clusters)
    ):
        mean_leave = float(np.mean(cap_leave_one))
        se = math.sqrt(
            (len(clusters) - 1)
            / len(clusters)
            * sum(
                (value - mean_leave) ** 2
                for value in cap_leave_one
            )
        )
        cap_ci["lower_95"] = cap_delta - 1.96 * se
        cap_ci["upper_95"] = cap_delta + 1.96 * se
    return {
        "paired_first_fills": len(delta_rows),
        "delta_conditional_ev_per_first_fill_usd": _metric_point(
            delta_rows
        )["conditional_ev_per_first_fill_usd"],
        "delta_conditional_cents_per_postfill_locked_dollar_hour": (
            cap_delta
        ),
        "market_cluster_ci": {
            "delta_conditional_ev_per_first_fill_usd": (
                _cluster_jackknife_ci(
                    delta_rows,
                    "conditional_ev_per_first_fill_usd",
                )
            ),
            "delta_conditional_cents_per_postfill_locked_dollar_hour": (
                cap_ci
            ),
        },
    }


def _action_distribution(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    decisions = Counter()
    fallbacks = Counter()
    terminals = Counter()
    for row in rows:
        terminals[str(row["terminal_type"])] += 1
        for decision in row["trace"]:
            action = decision.get("action")
            if action in ALLOWED_ACTIONS:
                decisions[str(action)] += 1
            reason = decision.get("fallback_reason")
            if reason:
                fallbacks[str(reason)] += 1
    total = sum(decisions.values())
    return {
        "decision_action_counts": dict(sorted(decisions.items())),
        "decision_action_rates": (
            {
                action: count / total
                for action, count in sorted(decisions.items())
            }
            if total
            else {}
        ),
        "terminal_type_counts": dict(sorted(terminals.items())),
        "fallback_counts": dict(sorted(fallbacks.items())),
    }


def _mechanism_metrics(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    completions = [
        row for row in rows if row["maker_completion"] is True
    ]
    exits = [
        row for row in rows if row["maker_completion"] is not True
    ]
    q = None if not rows else len(completions) / len(rows)
    gain_cents = [
        100.0 * float(row["net_pnl_usd"]) for row in completions
    ]
    exit_losses_cents = [
        max(0.0, -100.0 * float(row["net_pnl_usd"]))
        for row in exits
    ]
    mean_gain = (
        None if not gain_cents else float(np.mean(gain_cents))
    )
    mean_loss = (
        None
        if not exit_losses_cents
        else float(np.mean(exit_losses_cents))
    )
    q_star = None
    if (
        mean_gain is not None
        and mean_loss is not None
        and mean_gain > 0
        and mean_loss >= 0
        and mean_gain + mean_loss > 0
    ):
        q_star = mean_loss / (mean_gain + mean_loss)
    if exit_losses_cents:
        p90 = float(np.quantile(exit_losses_cents, 0.90))
        p95 = float(np.quantile(exit_losses_cents, 0.95))
        cvar95 = float(
            np.mean(
                [
                    value
                    for value in exit_losses_cents
                    if value >= p95
                ]
            )
        )
    else:
        p90 = p95 = cvar95 = None
    return {
        "first_fills": len(rows),
        "maker_completions": len(completions),
        "fok_or_exit_count": len(exits),
        "maker_completion_rate_q": q,
        "mean_pair_gain_G_cents": mean_gain,
        "mean_fok_or_exit_loss_L_cents": mean_loss,
        "empirical_q_star_L_over_G_plus_L": q_star,
        "actual_q_minus_q_star": (
            None if q is None or q_star is None else q - q_star
        ),
        "fok_or_exit_loss_tail_cents": {
            "p90": p90,
            "p95": p95,
            "cvar95": cvar95,
        },
    }


def _mechanism_comparison(
    learned: Sequence[Mapping[str, object]],
    current: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    fitted = _mechanism_metrics(learned)
    baseline = _mechanism_metrics(current)
    fitted_q = fitted["maker_completion_rate_q"]
    current_q = baseline["maker_completion_rate_q"]
    fitted_l = fitted["mean_fok_or_exit_loss_L_cents"]
    current_l = baseline["mean_fok_or_exit_loss_L_cents"]
    delta_q = (
        None
        if fitted_q is None or current_q is None
        else float(fitted_q) - float(current_q)
    )
    delta_l = (
        None
        if fitted_l is None or current_l is None
        else float(fitted_l) - float(current_l)
    )
    raises_q = delta_q is not None and delta_q > 0
    lowers_l = delta_l is not None and delta_l < 0
    if raises_q and lowers_l:
        driver = "BOTH_HIGHER_Q_AND_LOWER_L"
    elif raises_q:
        driver = "HIGHER_MAKER_COMPLETION_Q"
    elif lowers_l:
        driver = "LOWER_FOK_OR_EXIT_LOSS_L"
    else:
        driver = "NO_Q_OR_L_IMPROVEMENT"
    return {
        "fitted_q": fitted,
        "current": baseline,
        "paired_change": {
            "delta_maker_completion_q": delta_q,
            "delta_mean_fok_or_exit_loss_L_cents": delta_l,
            "improvement_driver": driver,
        },
        "online_reference_context": ONLINE_REFERENCE_CONTEXT,
    }


def _first_side_report(
    learned: Sequence[Mapping[str, object]],
    current: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        side: {
            "fitted_q": _economic_report(
                [
                    row
                    for row in learned
                    if row["first_fill_side"] == side
                ]
            ),
            "current": _economic_report(
                [
                    row
                    for row in current
                    if row["first_fill_side"] == side
                ]
            ),
            "paired_vs_current": _paired_report(
                [
                    row
                    for row in learned
                    if row["first_fill_side"] == side
                ],
                [
                    row
                    for row in current
                    if row["first_fill_side"] == side
                ],
            ),
            "mechanism_q_G_L_and_tail": _mechanism_comparison(
                [
                    row
                    for row in learned
                    if row["first_fill_side"] == side
                ],
                [
                    row
                    for row in current
                    if row["first_fill_side"] == side
                ],
            ),
        }
        for side in ("YES", "NO")
    }


def _positive_lower(ci: Mapping[str, object]) -> bool:
    lower = ci.get("lower_95")
    return lower is not None and float(lower) > 0


def fit_synthetic_keep_fok_policy(
    dataset: SyntheticKeepFokDataset,
) -> dict[str, object]:
    """Run three-date LODO and return a non-deployable report."""
    if not SOURCE_PINS_FINALIZED:
        raise KeepFokPolicyContractError(
            "V4 schema compatibility only; source pins are unsealed "
            "pending V4.1"
        )
    trajectories = validate_synthetic_dataset(dataset)
    nonoverlap = [row for row in trajectories if not row.overlap]
    overlap = [row for row in trajectories if row.overlap]
    if not nonoverlap:
        raise KeepFokPolicyContractError(
            "no non-overlapping episodes for primary analysis"
        )
    aggregate: dict[
        str, dict[str, list[dict[str, object]]]
    ] = {
        band: {
            "FITTED_Q": [],
            "CURRENT": [],
            "NULL_ALWAYS_KEEP": [],
            "CLAIRVOYANT_UPPER_BOUND": [],
            "OVERLAP_FITTED_Q": [],
        }
        for band in FEE_BANDS
    }
    folds = []
    for holdout in sorted(DISCOVERY_DATES):
        train = [
            row
            for row in nonoverlap
            if row.source_date != holdout
        ]
        heldout = [
            row
            for row in nonoverlap
            if row.source_date == holdout
        ]
        overlap_heldout = [
            row
            for row in overlap
            if row.source_date == holdout
        ]
        if not train or not heldout:
            raise KeepFokPolicyContractError(
                "each LODO fold needs train and heldout episodes"
            )
        fold_report: dict[str, object] = {
            "holdout_date": holdout,
            "train_dates": sorted(
                {row.source_date for row in train}
            ),
            "fee_bands": {},
        }
        for band in FEE_BANDS:
            alpha, inner = _select_alpha_inner(
                train, fee_band=band
            )
            policy = _fit_backward_policy(
                train, fee_band=band, alpha=alpha
            )
            learned = [
                _execute_learned(row, policy) for row in heldout
            ]
            current = [
                _execute_current(row, band) for row in heldout
            ]
            null = [
                _execute_null_keep(row, band) for row in heldout
            ]
            upper = [
                _execute_clairvoyant(row, band) for row in heldout
            ]
            overlap_rows = [
                _execute_learned(row, policy)
                for row in overlap_heldout
            ]
            aggregate[band]["FITTED_Q"].extend(learned)
            aggregate[band]["CURRENT"].extend(current)
            aggregate[band]["NULL_ALWAYS_KEEP"].extend(null)
            aggregate[band]["CLAIRVOYANT_UPPER_BOUND"].extend(
                upper
            )
            aggregate[band]["OVERLAP_FITTED_Q"].extend(
                overlap_rows
            )
            fold_report["fee_bands"][band] = {
                "inner_selection": inner,
                "policy_receipt": policy.receipt(),
                "heldout_fitted_q": _economic_report(learned),
                "heldout_current": _economic_report(current),
                "heldout_paired_vs_current": _paired_report(
                    learned, current
                ),
                "heldout_sequential_trace_count": sum(
                    len(row["trace"]) for row in learned
                ),
                "heldout_selected_fok_zero_count": sum(
                    bool(row["selected_fok_zero"])
                    for row in learned
                ),
            }
        folds.append(fold_report)

    fee_reports = {}
    band_gate = {}
    selected_zero_by_band = {}
    for band in FEE_BANDS:
        learned = aggregate[band]["FITTED_Q"]
        current = aggregate[band]["CURRENT"]
        fitted_report = _economic_report(learned)
        paired = _paired_report(learned, current)
        mechanism = _mechanism_comparison(learned, current)
        selected_zero = sum(
            bool(row["selected_fok_zero"]) for row in learned
        )
        selected_zero_by_band[band] = selected_zero
        ev_ci = fitted_report["market_cluster_ci"][
            "conditional_ev_per_first_fill_usd"
        ]
        capital_ci = fitted_report["market_cluster_ci"][
            "conditional_cents_per_postfill_locked_dollar_hour"
        ]
        delta_ev_ci = paired["market_cluster_ci"][
            "delta_conditional_ev_per_first_fill_usd"
        ]
        delta_capital_ci = paired["market_cluster_ci"][
            "delta_conditional_cents_per_postfill_locked_dollar_hour"
        ]
        q_gap = mechanism["fitted_q"]["actual_q_minus_q_star"]
        first_side_report = _first_side_report(learned, current)
        first_side_nonnegative_gate = {
            side: (
                float(
                    first_side_report[side]["fitted_q"][
                        "conditional_ev_per_first_fill_usd"
                    ]
                )
                >= 0.0
            )
            for side in ("YES", "NO")
        }
        band_gate[band] = (
            selected_zero == 0
            and _positive_lower(ev_ci)
            and _positive_lower(capital_ci)
            and _positive_lower(delta_ev_ci)
            and _positive_lower(delta_capital_ci)
            and q_gap is not None
            and float(q_gap) > 0
            and all(first_side_nonnegative_gate.values())
        )
        fee_reports[band] = {
            "fitted_q": fitted_report,
            "current_sealed_comparator": _economic_report(current),
            "paired_vs_current": paired,
            "null_always_keep": _economic_report(
                aggregate[band]["NULL_ALWAYS_KEEP"]
            ),
            "clairvoyant_upper_bound": {
                **_economic_report(
                    aggregate[band]["CLAIRVOYANT_UPPER_BOUND"]
                ),
                "diagnostic_only": True,
                "uses_future_outcomes": True,
            },
            "action_and_fallback_distribution": (
                _action_distribution(learned)
            ),
            "mechanism_q_G_L_and_tail": mechanism,
            "first_side": first_side_report,
            "first_side_nonnegative_gate": (
                first_side_nonnegative_gate
            ),
            "overlapping_episode_conditional_diagnostic": {
                "status": OVERLAP_STATUS,
                "episode_count": len(
                    aggregate[band]["OVERLAP_FITTED_Q"]
                ),
                "metrics": _economic_report(
                    aggregate[band]["OVERLAP_FITTED_Q"]
                ),
            },
            "selected_fok_zero_count": selected_zero,
            "postfill_discovery_band_gate_pass": band_gate[band],
        }
    hypothetical_gate = all(band_gate.values())
    return {
        "contract": {
            "source_contract_version": CONTRACT_VERSION,
            "sealed_source_contract_sha256": (
                SEALED_SOURCE_CONTRACT_SHA256
            ),
            "sealed_source_ddl_sha256": SEALED_SOURCE_DDL_SHA256,
            "sealed_source_prereg_sha256": (
                SEALED_SOURCE_PREREG_SHA256
            ),
            "sealed_source_manifest_sha256": (
                SEALED_SOURCE_MANIFEST_SHA256
            ),
            "action_family_version": ACTION_FAMILY_VERSION,
            "synthetic_marker": SYNTHETIC_MARKER,
            "batch_sha256": dataset.expected_batch_sha256,
            "outcomes_sha256": dataset.expected_outcomes_sha256,
            "decision_grid_ms": list(DECISION_GRID_MS),
            "actions": ["KEEP", "FLATTEN_FOK"],
            "flatten_fok_effective_latency_ms": (
                FOK_EFFECTIVE_LATENCY_MS
            ),
            "flatten_fok_time_in_force": FOK_TIME_IN_FORCE,
            "flatten_fok_terminals": sorted(FOK_TERMINALS),
            "fok_zero_fit_rule": (
                "retain as conservative LOWER_BOUND; both fee-band "
                "labels equal -(first_leg_cost_basis + first_fill_fee)"
            ),
            "keep_value_rule": (
                "interval immediate reward exactly once plus Bellman "
                "continuation; no repeated eventual terminal pseudo-reward"
            ),
            "heldout_action_rule": (
                "compare train-only predicted Q_KEEP and "
                "Q_FLATTEN_FOK; heldout action outcome is evaluation-only"
            ),
            "current_comparator_rule": (
                "independent sealed CURRENT_DIST2_TTL60 comparator "
                "receipt; never a KEEP terminal and never grid-snapped"
            ),
            "ridge_grid": list(RIDGE_GRID),
            "feature_names": list(CURRENT_FEATURES),
            "overlap_policy": OVERLAP_STATUS,
            "estimand_boundary": (
                "conditional per first fill and conditional postfill "
                "capital-time; never whole admitted-cycle"
            ),
        },
        "folds": folds,
        "oos_fee_band_reports": fee_reports,
        "postfill_discovery_gate_only": {
            "per_band": band_gate,
            "selected_fok_zero_by_band": selected_zero_by_band,
            "fok_zero_hard_rule": (
                "any OOS-selected FOK_ZERO forces NO_CANDIDATE"
            ),
            "rule": (
                "both fee bands require zero selected FOK_ZERO, "
                "positive lower 95% market-cluster CI for conditional "
                "EV/first-fill and conditional postfill capital-time, "
                "positive paired deltas versus exact CURRENT, q>q*, "
                "and nonnegative OOS conditional EV on both first-fill "
                "sides"
            ),
            "hypothetical_pass": hypothetical_gate,
            "not_a_whole_policy_candidate_gate": True,
            "required_next_stage": (
                "Stage-1 plus whole-policy causal replay with capital "
                "conflict, overlapping admission, and re-entry suppression"
            ),
        },
        "claim": CLAIM,
        "candidate": None,
        "candidate_selected": False,
        "action_seal_allowed": ACTION_SEAL_ALLOWED,
        "whole_policy_candidate_eligible": False,
        "live_authorized": LIVE_AUTHORIZED,
        "deployable": False,
        "non_deployable_reason": (
            "pure-synthetic discovery learner; no real/live path"
        ),
    }
