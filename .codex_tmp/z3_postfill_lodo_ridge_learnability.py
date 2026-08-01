#!/usr/bin/env python3
"""Read-only LODO predictability diagnostic for frozen post-fill actions.

This is not candidate selection.  Each UTC date is held out in turn.  Separate
fixed-alpha ridge models for optimistic minimum-fee reward and log capital
time are fit on the other dates after purging any held-out market identity.
Only features known at the first fill are used to choose among ten
already-frozen actions on the held-out date.

The fee labels deliberately supersede v1 wording.  L2 price-level slices do
not expose the exchange's private same-price true-fill partition, while the
official fee rule rounds each true fill.  Therefore the legacy
``direct_centicent_*`` source fields used below are an aggregate-once
minimum-fee construction and their PnL is an optimistic upper point estimate,
not account-exact realized PnL.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import math
import random
import statistics
from collections import Counter, defaultdict

import numpy as np


EPISODES_PATH = (
    "/tmp/z3_postfill_receive_clock_sampled_a1skip_episodes_20260726T082350Z.json.gz"
)
REPORT_PATH = (
    "/tmp/z3_postfill_receive_clock_sampled_a1skip_report_20260726T082350Z.json"
)
DIRECT_AUDIT_PATH = (
    "/tmp/z3_postfill_direct_centicent_reprice_audit_20260726T082350Z.json"
)
DIRECT_SIDECAR_PATH = (
    "/tmp/z3_postfill_direct_centicent_sidecar_20260726T082350Z.json"
)
EPISODES_SHA256 = (
    "b4440b5822416954134f656dfe2aaf61fa3e8be0cfcdd295e62fc19f6bf3201f"
)
REPORT_SHA256 = (
    "ff5c990cbb05e3695604c9c61612d7e8e92aeced3d4b3e12b3417e8220265cc8"
)
DIRECT_AUDIT_SHA256 = (
    "80a25b91ae3b2fc55b6542d05f5f43845f4b299878c31d826d11340c452aeedb"
)
DIRECT_SIDECAR_SHA256 = (
    "8dd378493ab7cf56c4c8248fa151428f5fe8610842aa28eb8dd88db1c09df2db"
)
V1_PREDICTIONS_PATH = (
    "/tmp/z3_postfill_lodo_ridge_learnability_predictions_"
    "20260726T082350Z.json.gz"
)
V1_PREDICTIONS_SHA256 = (
    "5c7612707ecefe4938b04fd8b8f293b7380ee9dbbeea693234c42ee388766e11"
)
DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
CURRENT = "CURRENT_DIST2_TTL60"
ORACLE = "CLAIRVOYANT_BEST_FIXED"
ACTIONS = (
    "IOC_60MS",
    "WAIT_0P25",
    "WAIT_0P5",
    "WAIT_1",
    "WAIT_2",
    "WAIT_5",
    "WAIT_10",
    "WAIT_30",
    "WAIT_60",
    CURRENT,
)
RIDGE_ALPHA = 10.0
PREDICTED_CAPITAL_GATE_S = 10.0
BOOTSTRAP_REPS = 5000

FEATURE_NAMES = (
    "held_is_yes",
    "entry_price",
    "counterpart_price",
    "pair_cost",
    "pair_gain_c",
    "log1p_admission_to_first_s",
    "entry_price_sq",
    "counterpart_price_sq",
    "entry_minus_counterpart",
    "utc_minute_sin",
    "utc_minute_cos",
)

PREREGISTRATION = {
    "schema": "z3-postfill-lodo-ridge-learnability-prereg-v2-reporting-audit",
    "status": "PREDICTABILITY_DIAGNOSTIC_ONLY",
    "not_candidate_selection": True,
    "not_validation": True,
    "deployable": False,
    "dates": list(DATES),
    "forbidden_dates": ["2026-07-23", "2026-07-26"],
    "reward_source": (
        "frozen policy outcomes repriced by the sealed aggregate-once "
        "minimum-fee sidecar; this is an optimistic PnL upper point "
        "sensitivity, not account-exact realized PnL; no action, fill, "
        "timing, or book-walk reselection"
    ),
    "actions": list(ACTIONS),
    "features": list(FEATURE_NAMES),
    "feature_clock": (
        "all fields exist at first fill: held side, frozen entry/counterpart "
        "quotes, pair gain, admission-to-first latency, UTC minute"
    ),
    "trajectory_feature_policy": (
        "sampled per-event trajectory features are excluded because the fixed "
        "1/16 outcome-blind logging sample does not cover all episodes; full "
        "episode first-fill fields provide 100% coverage"
    ),
    "crossfit": (
        "leave one UTC date out; training uses only the other two dates and "
        "purges every market appearing on the held-out date"
    ),
    "models": {
        "reward": (
            "one separate ridge model per action, fixed alpha=10 after "
            "train-only standardization"
        ),
        "capital": (
            "one separate ridge model per action on log1p(capital_time_s), "
            "fixed alpha=10 after the same train-only standardization"
        ),
    },
    "heldout_action_rule": (
        "CURRENT is always eligible; other actions require predicted capital "
        "time <=10s. Choose highest predicted optimistic minimum-fee PnL, tie by "
        "lower predicted capital then fixed action order. If its predicted "
        "PnL is not strictly above predicted CURRENT, choose CURRENT."
    ),
    "metrics": (
        "crossfit realized EV and market-cluster bootstrap CI95; paired "
        "selection-minus-current improvement and cluster CI95; realized "
        "capital, coverage, action distribution, per-date EV, per-action "
        "heldout RMSE/correlation; hindsight best action is benchmark only"
    ),
    "reporting_only_revision": {
        "model_or_action_change": False,
        "requires_v1_selected_projection_exact_match": True,
        "per_date_cluster_ci": True,
        "selection_reason_and_current_retention": True,
        "feature_support_and_clipping": True,
        "optimistic_reward_row_identity": True,
        "fee_semantics_correction": (
            "official fee rounds each true fill; absent private same-price "
            "fill partition, aggregate-once fee is only a lower bound on "
            "true fee and therefore an optimistic PnL upper point estimate"
        ),
    },
}


def sha256_path(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_sha(path, expected, label):
    actual = sha256_path(path)
    if actual != expected:
        raise RuntimeError(
            f"{label} SHA mismatch expected={expected} actual={actual}"
        )
    return actual


def utc_date(timestamp_us):
    return dt.datetime.fromtimestamp(
        int(timestamp_us) / 1e6,
        tz=dt.timezone.utc,
    ).date().isoformat()


def feature_vector(row):
    entry = float(row["entry_e4"]) / 10_000.0
    counterpart = float(row["counterpart_lvl"]) / 10_000.0
    latency = max(
        0.0,
        (int(row["first_ts"]) - int(row["admit_ts"])) / 1e6,
    )
    stamp = dt.datetime.fromtimestamp(
        int(row["first_ts"]) / 1e6,
        tz=dt.timezone.utc,
    )
    minute = (
        stamp.hour * 60.0
        + stamp.minute
        + stamp.second / 60.0
        + stamp.microsecond / 60e6
    )
    angle = 2.0 * math.pi * minute / (24.0 * 60.0)
    values = (
        1.0 if row["held_side"] == "y" else 0.0,
        entry,
        counterpart,
        entry + counterpart,
        float(row["pair_gain_c"]),
        math.log1p(latency),
        entry * entry,
        counterpart * counterpart,
        entry - counterpart,
        math.sin(angle),
        math.cos(angle),
    )
    if not all(math.isfinite(value) for value in values):
        raise RuntimeError(f"non-finite first-fill features {row}")
    return values


def canonical_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


SELECTED_PROJECTION_FIELDS = (
    "episode_id",
    "date",
    "market",
    "selected_action",
    "predicted_reward_c",
    "predicted_current_reward_c",
    "predicted_capital_s",
    "realized_reward_c",
    "current_reward_c",
    "delta_vs_current_c",
    "selected_capital_s",
)


def selected_projection(rows):
    return [
        {field: row[field] for field in SELECTED_PROJECTION_FIELDS}
        for row in rows
    ]


def selected_projection_sha256(rows):
    return hashlib.sha256(
        canonical_bytes(selected_projection(rows))
    ).hexdigest()


def load_inputs(prereg_path):
    script_sha = sha256_path(__file__)
    prereg_sha = sha256_path(prereg_path)
    with open(prereg_path, "rb") as handle:
        prereg = json.load(handle)
    if prereg.get("experiment_sha256") != script_sha:
        raise RuntimeError("learnability prereg script SHA mismatch")
    if prereg.get("spec") != PREREGISTRATION:
        raise RuntimeError("learnability prereg spec mismatch")
    require_sha(EPISODES_PATH, EPISODES_SHA256, "episodes")
    require_sha(REPORT_PATH, REPORT_SHA256, "report")
    require_sha(DIRECT_AUDIT_PATH, DIRECT_AUDIT_SHA256, "direct audit")
    require_sha(
        DIRECT_SIDECAR_PATH,
        DIRECT_SIDECAR_SHA256,
        "direct sidecar",
    )
    require_sha(
        V1_PREDICTIONS_PATH,
        V1_PREDICTIONS_SHA256,
        "v1 predictions",
    )
    with gzip.open(EPISODES_PATH, "rt", encoding="utf-8") as handle:
        episodes = json.load(handle)
    with open(REPORT_PATH, "rb") as handle:
        report = json.load(handle)
    with open(DIRECT_AUDIT_PATH, "rb") as handle:
        direct_audit = json.load(handle)
    with open(DIRECT_SIDECAR_PATH, "rb") as handle:
        sidecar = json.load(handle)
    with gzip.open(
        V1_PREDICTIONS_PATH,
        "rt",
        encoding="utf-8",
    ) as handle:
        v1_predictions = json.load(handle)
    if (
        episodes.get("date_2026_07_23_read") is not False
        or report.get("date_2026_07_23_read") is not False
        or direct_audit.get("date_2026_07_23_read") is not False
        or sidecar.get("date_2026_07_23_read") is not False
        or sidecar.get("reprice_audit", {}).get("sha256")
        != DIRECT_AUDIT_SHA256
        or not direct_audit.get("all_sealed_old_pnl_reproduced")
        or v1_predictions.get("date_2026_07_23_read") is not False
        or v1_predictions.get("date_2026_07_26_read") is not False
    ):
        raise RuntimeError("sealed input/read-date contract mismatch")
    if any(
        date in {"2026-07-23", "2026-07-26"}
        for date in direct_audit.get("dates", [])
    ):
        raise RuntimeError("forbidden date present in fee audit")
    return (
        episodes,
        report,
        direct_audit,
        sidecar,
        v1_predictions,
        script_sha,
        prereg_sha,
    )


def build_episodes(episodes, direct_audit):
    rows = episodes["policy_rows"]
    audit_rows = direct_audit["rows"]
    audit_indexes = [int(row["row_index"]) for row in audit_rows]
    if (
        len(audit_rows) != 7125
        or len(set(audit_indexes)) != len(audit_indexes)
        or direct_audit.get("reproduced_ioc_rows") != len(audit_rows)
        or direct_audit.get("maker_pair_rows_unchanged") != 6097
        or direct_audit.get("action_projection_sha256")
        != "febde18daf12fafec32f8504ea2be39c0b2e6692a1c51d2fc37bb1b3d40badc9"
    ):
        raise RuntimeError("optimistic fee row identity contract mismatch")
    repriced = {
        int(row["row_index"]): row
        for row in audit_rows
    }
    adjusted = []
    consumed_ioc_indexes = []
    row_identity_fields = (
        "episode_id",
        "market",
        "policy",
        "selected_source_policy",
        "held_side",
        "entry_e4",
        "first_ts",
        "exit_ts",
    )
    for index, original in enumerate(rows):
        row = dict(original)
        if row["exit_kind"] == "ioc":
            if index not in repriced:
                raise RuntimeError("missing optimistic fee row")
            audit_row = repriced[index]
            if any(
                original.get(field) != audit_row.get(field)
                for field in row_identity_fields
            ):
                raise RuntimeError("optimistic fee row identity mismatch")
            row["pnl_c"] = float(
                audit_row["direct_centicent_pnl_c"]
            )
            consumed_ioc_indexes.append(index)
        adjusted.append(row)
    if (
        len(consumed_ioc_indexes) != len(audit_rows)
        or set(consumed_ioc_indexes) != set(audit_indexes)
    ):
        raise RuntimeError("optimistic fee row mapping not exhaustive")
    grouped = defaultdict(dict)
    for row in adjusted:
        if row["policy"] == ORACLE:
            continue
        episode_id = row["episode_id"]
        if row["policy"] in grouped[episode_id]:
            raise RuntimeError("duplicate action outcome")
        grouped[episode_id][row["policy"]] = row
    records = []
    identity_fields = (
        "market",
        "admit_ts",
        "first_ts",
        "held_side",
        "entry_e4",
        "counterpart_lvl",
        "pair_gain_c",
    )
    for episode_id in sorted(grouped):
        action_rows = grouped[episode_id]
        if set(action_rows) != set(ACTIONS):
            raise RuntimeError("incomplete action matrix")
        template = action_rows[CURRENT]
        identity = tuple(template[field] for field in identity_fields)
        for row in action_rows.values():
            if tuple(row[field] for field in identity_fields) != identity:
                raise RuntimeError("action rows disagree at first fill")
        date = utc_date(template["first_ts"])
        if date not in DATES:
            raise RuntimeError(f"episode outside allowed dates {date}")
        records.append(
            {
                "episode_id": episode_id,
                "date": date,
                "market": template["market"],
                "features": feature_vector(template),
                "rewards": {
                    action: float(action_rows[action]["pnl_c"])
                    for action in ACTIONS
                },
                "capital": {
                    action: float(
                        action_rows[action]["capital_time_s"]
                    )
                    for action in ACTIONS
                },
            }
        )
    if len(records) != 1202:
        raise RuntimeError("episode feature coverage mismatch")
    return records


def standardize_fit(matrix):
    mean = matrix.mean(axis=0)
    scale = matrix.std(axis=0)
    scale[scale < 1e-12] = 1.0
    return mean, scale


def ridge_fit(matrix, target, mean, scale):
    z = (matrix - mean) / scale
    design = np.column_stack((np.ones(len(z)), z))
    penalty = np.eye(design.shape[1]) * RIDGE_ALPHA
    penalty[0, 0] = 0.0
    return np.linalg.solve(
        design.T @ design + penalty,
        design.T @ target,
    )


def ridge_predict(matrix, beta, mean, scale):
    z = (matrix - mean) / scale
    design = np.column_stack((np.ones(len(z)), z))
    return design @ beta


def quantile(values, q):
    values = sorted(float(value) for value in values)
    position = (len(values) - 1) * q
    lo = int(math.floor(position))
    hi = int(math.ceil(position))
    if lo == hi:
        return values[lo]
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def cluster_ci(rows, value_key, label):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["market"]].append(float(row[value_key]))
    markets = sorted(grouped)
    rng = random.Random(
        int(hashlib.sha256(label.encode()).hexdigest()[:16], 16)
    )
    samples = []
    for _ in range(BOOTSTRAP_REPS):
        values = []
        for _market in markets:
            values.extend(grouped[rng.choice(markets)])
        samples.append(statistics.fmean(values))
    return [
        round(quantile(samples, 0.025), 4),
        round(quantile(samples, 0.975), 4),
    ]


def metric(rows, reward_key, label):
    rewards = [float(row[reward_key]) for row in rows]
    capital = [float(row["selected_capital_s"]) for row in rows]
    return {
        "n": len(rows),
        "markets": len({row["market"] for row in rows}),
        "ev_c_per_first_fill": round(
            statistics.fmean(rewards), 4
        ),
        "ci95_market_cluster": cluster_ci(
            rows, reward_key, f"ev:{label}"
        ),
        "mean_capital_time_s": round(
            statistics.fmean(capital), 4
        ),
        "median_capital_time_s": round(
            statistics.median(capital), 4
        ),
    }


def paired_metric(rows, label="lodo-selection-vs-current"):
    return {
        "mean_improvement_c": round(
            statistics.fmean(row["delta_vs_current_c"] for row in rows),
            4,
        ),
        "ci95_market_cluster": cluster_ci(
            rows,
            "delta_vs_current_c",
            label,
        ),
        "positive": sum(
            row["delta_vs_current_c"] > 0 for row in rows
        ),
        "zero": sum(
            abs(row["delta_vs_current_c"]) <= 1e-12
            for row in rows
        ),
        "negative": sum(
            row["delta_vs_current_c"] < 0 for row in rows
        ),
    }


def prediction_diagnostics(prediction_rows):
    output = {}
    for action in ACTIONS:
        rows = [
            row for row in prediction_rows
            if row["action"] == action
        ]
        actual = np.array(
            [row["actual_reward_c"] for row in rows],
            dtype=float,
        )
        predicted = np.array(
            [row["predicted_reward_c"] for row in rows],
            dtype=float,
        )
        corr = (
            float(np.corrcoef(actual, predicted)[0, 1])
            if np.std(actual) > 0 and np.std(predicted) > 0
            else None
        )
        output[action] = {
            "n": len(rows),
            "rmse_c": round(
                float(np.sqrt(np.mean((actual - predicted) ** 2))),
                4,
            ),
            "correlation": (
                round(corr, 4)
                if corr is not None and math.isfinite(corr)
                else None
            ),
        }
    return output


def run_crossfit(records):
    selected_rows = []
    prediction_rows = []
    folds = []
    action_order = {action: index for index, action in enumerate(ACTIONS)}
    for heldout_date in DATES:
        test = [row for row in records if row["date"] == heldout_date]
        heldout_markets = {row["market"] for row in test}
        train = [
            row
            for row in records
            if row["date"] != heldout_date
            and row["market"] not in heldout_markets
        ]
        if len(train) < 300 or not test:
            raise RuntimeError("insufficient LODO fold coverage")
        x_train = np.array(
            [row["features"] for row in train],
            dtype=float,
        )
        x_test = np.array(
            [row["features"] for row in test],
            dtype=float,
        )
        raw_train_std = x_train.std(axis=0)
        mean, scale = standardize_fit(x_train)
        feature_support = []
        for feature_index, feature_name in enumerate(FEATURE_NAMES):
            train_values = x_train[:, feature_index]
            test_values = x_test[:, feature_index]
            train_min = float(np.min(train_values))
            train_max = float(np.max(train_values))
            below = int(np.sum(test_values < train_min))
            above = int(np.sum(test_values > train_max))
            feature_support.append(
                {
                    "feature": feature_name,
                    "train_min": train_min,
                    "train_max": train_max,
                    "heldout_min": float(np.min(test_values)),
                    "heldout_max": float(np.max(test_values)),
                    "heldout_below_train_count": below,
                    "heldout_above_train_count": above,
                    "heldout_outside_train_count": below + above,
                    "train_mean": float(mean[feature_index]),
                    "train_scale_used": float(scale[feature_index]),
                    "train_zero_variance": bool(
                        raw_train_std[feature_index] < 1e-12
                    ),
                }
            )
        reward_predictions = {}
        capital_predictions = {}
        capital_negative_clip_by_action = Counter()
        for action in ACTIONS:
            reward_target = np.array(
                [row["rewards"][action] for row in train],
                dtype=float,
            )
            capital_target = np.log1p(
                np.array(
                    [row["capital"][action] for row in train],
                    dtype=float,
                )
            )
            reward_beta = ridge_fit(
                x_train, reward_target, mean, scale
            )
            capital_beta = ridge_fit(
                x_train, capital_target, mean, scale
            )
            reward_predictions[action] = ridge_predict(
                x_test, reward_beta, mean, scale
            )
            raw_capital_prediction = np.expm1(
                ridge_predict(
                    x_test, capital_beta, mean, scale
                )
            )
            capital_negative_clip_by_action[action] = int(
                np.sum(raw_capital_prediction < 0.0)
            )
            capital_predictions[action] = np.maximum(
                0.0,
                raw_capital_prediction,
            )
        fold_actions = Counter()
        fold_reasons = Counter()
        for index, episode in enumerate(test):
            for action in ACTIONS:
                prediction_rows.append(
                    {
                        "episode_id": episode["episode_id"],
                        "date": heldout_date,
                        "market": episode["market"],
                        "action": action,
                        "predicted_reward_c": float(
                            reward_predictions[action][index]
                        ),
                        "actual_reward_c": episode["rewards"][
                            action
                        ],
                    }
                )
            eligible = [
                action
                for action in ACTIONS
                if action == CURRENT
                or capital_predictions[action][index]
                <= PREDICTED_CAPITAL_GATE_S
            ]
            candidate = min(
                eligible,
                key=lambda action: (
                    -reward_predictions[action][index],
                    capital_predictions[action][index],
                    action_order[action],
                ),
            )
            if candidate == CURRENT:
                best = CURRENT
                selection_reason = "current_predicted_best"
            elif (
                reward_predictions[candidate][index]
                <= reward_predictions[CURRENT][index]
            ):
                best = CURRENT
                selection_reason = (
                    "fallback_no_strict_predicted_improvement"
                )
            else:
                best = candidate
                selection_reason = (
                    "noncurrent_predicted_improvement"
                )
            fold_actions[best] += 1
            fold_reasons[selection_reason] += 1
            actual_reward = episode["rewards"][best]
            current_reward = episode["rewards"][CURRENT]
            selected_rows.append(
                {
                    "episode_id": episode["episode_id"],
                    "date": heldout_date,
                    "market": episode["market"],
                    "selected_action": best,
                    "predicted_reward_c": float(
                        reward_predictions[best][index]
                    ),
                    "predicted_current_reward_c": float(
                        reward_predictions[CURRENT][index]
                    ),
                    "predicted_capital_s": float(
                        capital_predictions[best][index]
                    ),
                    "realized_reward_c": actual_reward,
                    "current_reward_c": current_reward,
                    "delta_vs_current_c": (
                        actual_reward - current_reward
                    ),
                    "selected_capital_s": episode["capital"][best],
                    "selection_reason": selection_reason,
                }
            )
        folds.append(
            {
                "heldout_date": heldout_date,
                "train_dates": [
                    date for date in DATES if date != heldout_date
                ],
                "train_episodes_after_market_purge": len(train),
                "heldout_episodes": len(test),
                "heldout_markets": len(heldout_markets),
                "selected_action_counts": dict(fold_actions),
                "selection_reason_counts": dict(fold_reasons),
                "current_retention_rate": (
                    fold_actions[CURRENT] / len(test)
                ),
                "fallback_no_strict_predicted_improvement_rate": (
                    fold_reasons[
                        "fallback_no_strict_predicted_improvement"
                    ]
                    / len(test)
                ),
                "feature_support": feature_support,
                "zero_variance_feature_count": sum(
                    item["train_zero_variance"]
                    for item in feature_support
                ),
                "capital_prediction_negative_clip_count": sum(
                    capital_negative_clip_by_action.values()
                ),
                "capital_prediction_negative_clip_by_action": dict(
                    capital_negative_clip_by_action
                ),
            }
        )
    return selected_rows, prediction_rows, folds


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", required=True)
    parser.add_argument(
        "--report-out",
        default="/tmp/z3_postfill_lodo_ridge_learnability_report.json",
    )
    parser.add_argument(
        "--predictions-out",
        default="/tmp/z3_postfill_lodo_ridge_learnability_predictions.json.gz",
    )
    args = parser.parse_args()
    (
        episodes,
        sealed_report,
        direct_audit,
        sidecar,
        v1_predictions,
        script_sha,
        prereg_sha,
    ) = load_inputs(args.prereg)
    records = build_episodes(episodes, direct_audit)
    selected_rows, prediction_rows, folds = run_crossfit(records)
    if len(selected_rows) != len(records):
        raise RuntimeError("crossfit coverage mismatch")
    v1_projection_bytes = canonical_bytes(
        selected_projection(v1_predictions["selected_rows"])
    )
    v2_projection_bytes = canonical_bytes(
        selected_projection(selected_rows)
    )
    if v1_projection_bytes != v2_projection_bytes:
        raise RuntimeError(
            "reporting-only v2 changed the v1 selected projection"
        )
    selected_projection_sha = hashlib.sha256(
        v2_projection_bytes
    ).hexdigest()

    selection_metric = metric(
        selected_rows,
        "realized_reward_c",
        "lodo-ridge-selection",
    )
    record_by_episode = {
        record["episode_id"]: record for record in records
    }
    current_rows = [
        {
            **row,
            "selected_capital_s": record_by_episode[
                row["episode_id"]
            ]["capital"][CURRENT],
        }
        for row in selected_rows
    ]
    current_metric = metric(
        current_rows,
        "current_reward_c",
        "optimistic-min-fee-current",
    )
    action_counts = Counter(
        row["selected_action"] for row in selected_rows
    )
    selection_reason_counts = Counter(
        row["selection_reason"] for row in selected_rows
    )
    per_date = {}
    for date in DATES:
        rows = [row for row in selected_rows if row["date"] == date]
        date_current_rows = [
            {
                **row,
                "selected_capital_s": record_by_episode[
                    row["episode_id"]
                ]["capital"][CURRENT],
            }
            for row in rows
        ]
        date_action_counts = Counter(
            row["selected_action"] for row in rows
        )
        date_reason_counts = Counter(
            row["selection_reason"] for row in rows
        )
        per_date[date] = {
            "n": len(rows),
            "markets": len({row["market"] for row in rows}),
            "selected_metric": metric(
                rows,
                "realized_reward_c",
                f"lodo-ridge-selection:{date}",
            ),
            "current_metric": metric(
                date_current_rows,
                "current_reward_c",
                f"optimistic-min-fee-current:{date}",
            ),
            "paired_vs_current": paired_metric(
                rows,
                f"lodo-selection-vs-current:{date}",
            ),
            "action_counts": dict(date_action_counts),
            "selection_reason_counts": dict(date_reason_counts),
            "current_retention_rate": (
                date_action_counts[CURRENT] / len(rows)
            ),
            "fallback_no_strict_predicted_improvement_rate": (
                date_reason_counts[
                    "fallback_no_strict_predicted_improvement"
                ]
                / len(rows)
            ),
        }

    hindsight_rows = []
    for record in records:
        best = min(
            ACTIONS,
            key=lambda action: (
                -record["rewards"][action],
                record["capital"][action],
                ACTIONS.index(action),
            ),
        )
        hindsight_rows.append(
            {
                "market": record["market"],
                "realized_reward_c": record["rewards"][best],
                "selected_capital_s": record["capital"][best],
            }
        )
    hindsight_metric = metric(
        hindsight_rows,
        "realized_reward_c",
        "optimistic-min-fee-hindsight-best-ten",
    )
    feature_matrix = np.array(
        [row["features"] for row in records],
        dtype=float,
    )
    feature_audit = {
        "feature_names": list(FEATURE_NAMES),
        "all_values_finite": bool(np.isfinite(feature_matrix).all()),
        "missing_value_count": 0,
        "imputation_used": False,
        "raw_feature_clipping_used": False,
        "overall_raw_support": [
            {
                "feature": feature_name,
                "min": float(np.min(feature_matrix[:, feature_index])),
                "max": float(np.max(feature_matrix[:, feature_index])),
            }
            for feature_index, feature_name in enumerate(FEATURE_NAMES)
        ],
        "standardization": (
            "train-only mean and population standard deviation per fold; "
            "scales below 1e-12 are replaced by 1.0"
        ),
        "fold_support_is_reported": True,
        "heldout_outside_train_count_total": sum(
            item["heldout_outside_train_count"]
            for fold in folds
            for item in fold["feature_support"]
        ),
        "capital_prediction_negative_clip_count": sum(
            fold["capital_prediction_negative_clip_count"]
            for fold in folds
        ),
        "capital_prediction_clip_rule": (
            "only predicted capital time is floored at 0 after expm1; "
            "reward predictions and raw features are never clipped"
        ),
    }
    report = {
        "schema": "z3-postfill-lodo-ridge-learnability-v2-reporting-audit",
        "status": "PREDICTABILITY_DIAGNOSTIC_ONLY",
        "not_candidate_selection": True,
        "not_validation": True,
        "deployable": False,
        "date_2026_07_23_read": False,
        "date_2026_07_26_read": False,
        "source": {
            "script_sha256": script_sha,
            "preregistration_sha256": prereg_sha,
            "episodes_sha256": EPISODES_SHA256,
            "sealed_report_sha256": REPORT_SHA256,
            "direct_audit_sha256": DIRECT_AUDIT_SHA256,
            "direct_sidecar_sha256": DIRECT_SIDECAR_SHA256,
            "v1_predictions_sha256": V1_PREDICTIONS_SHA256,
        },
        "preregistration": PREREGISTRATION,
        "coverage": {
            "episodes": len(records),
            "expected_episodes": 1202,
            "fraction": 1.0,
            "markets": len({row["market"] for row in records}),
            "dates": list(DATES),
            "all_first_fill_features_finite": True,
            "sampled_trajectory_features_used": False,
            "reason": PREREGISTRATION[
                "trajectory_feature_policy"
            ],
        },
        "v1_reporting_only_equivalence": {
            "selected_projection_fields": list(
                SELECTED_PROJECTION_FIELDS
            ),
            "v1_selected_projection_sha256": hashlib.sha256(
                v1_projection_bytes
            ).hexdigest(),
            "v2_selected_projection_sha256": selected_projection_sha,
            "selected_projection_exact_match": True,
            "model_or_action_change": False,
        },
        "fee_semantics_correction": {
            "supersedes_v1_reward_labels": True,
            "official_rounding_unit": (
                "each private true fill, rounded up to $0.0001"
            ),
            "missing_from_l2": (
                "same-price private true-fill partition and order_id "
                "accumulator state"
            ),
            "point_estimate_class": (
                "OPTIMISTIC_L2_AGGREGATE_MIN_FEE_MAX_PNL_SENSITIVITY"
            ),
            "not_account_exact": True,
            "not_realized_pnl": True,
            "proof": (
                "sum_i ceil_0.0001(raw_i) >= "
                "ceil_0.0001(sum_i raw_i); order rounding less rebate "
                "leaves a nonnegative residue, so true fee is no lower "
                "than the aggregate-once fee"
            ),
            "strict_ioc_bound": (
                "true PnL improvement versus sealed whole-cent fee is "
                "<=0.99 cents per IOC"
            ),
            "official_fee_rounding_url": (
                "https://docs.kalshi.com/getting_started/fee_rounding"
            ),
        },
        "optimistic_reward_row_identity": {
            "legacy_source_field_name": "direct_centicent_pnl_c",
            "audit_rows": len(direct_audit["rows"]),
            "row_index_mapping_exact_and_exhaustive": True,
            "all_sealed_old_pnl_reproduced": direct_audit[
                "all_sealed_old_pnl_reproduced"
            ],
            "action_projection_sha256": direct_audit[
                "action_projection_sha256"
            ],
            "maker_pair_rows_unchanged": direct_audit[
                "maker_pair_rows_unchanged"
            ],
            "audit_sha256": DIRECT_AUDIT_SHA256,
        },
        "feature_support_and_clipping": feature_audit,
        "folds": folds,
        "selection": {
            "metric": selection_metric,
            "paired_vs_current": paired_metric(selected_rows),
            "action_counts": dict(action_counts),
            "action_fractions": {
                action: round(
                    action_counts[action] / len(selected_rows), 6
                )
                for action in ACTIONS
            },
            "selection_reason_counts": dict(
                selection_reason_counts
            ),
            "current_retention_rate": (
                action_counts[CURRENT] / len(selected_rows)
            ),
            "fallback_no_strict_predicted_improvement_rate": (
                selection_reason_counts[
                    "fallback_no_strict_predicted_improvement"
                ]
                / len(selected_rows)
            ),
            "per_date": per_date,
        },
        "optimistic_min_fee_current": current_metric,
        "prediction_diagnostics_by_action": (
            prediction_diagnostics(prediction_rows)
        ),
        "hindsight_best_of_ten_benchmark": {
            "metric": hindsight_metric,
            "uses_heldout_labels": True,
            "selection_rule_deployable": False,
        },
        "legacy_source_optimistic_min_fee_frozen_oracle_metric": (
            sidecar["direct_centicent_metrics"][ORACLE]
        ),
        "conclusion": "BASIC_FULL_COVERAGE_FEATURES_NOT_LEARNABLE",
        "interpretation_gate": (
            "This run tests whether simple first-fill features carry "
            "out-of-date predictive information under an optimistic "
            "minimum-fee reward sensitivity. A positive estimate is not a "
            "candidate: action definitions overlap, episodes can overlap, "
            "only three UTC folds are available, and reward is not "
            "account-exact. The negative result applies only to these basic "
            "full-coverage features; it does not reject queue/flow ROUND4."
        ),
    }
    predictions = {
        "schema": "z3-postfill-lodo-ridge-predictions-v2-reporting-audit",
        "date_2026_07_23_read": False,
        "date_2026_07_26_read": False,
        "reward_semantics": (
            "optimistic aggregate-once minimum-fee PnL upper point "
            "sensitivity; not account-exact realized PnL"
        ),
        "v1_selected_projection_sha256": selected_projection_sha,
        "v1_selected_projection_exact_match": True,
        "selected_rows": selected_rows,
        "all_action_predictions": prediction_rows,
    }
    report_bytes = json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    with open(args.report_out, "wb") as handle:
        handle.write(report_bytes)
    with gzip.open(
        args.predictions_out,
        "wt",
        encoding="utf-8",
        compresslevel=6,
    ) as handle:
        json.dump(
            predictions,
            handle,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    print(
        json.dumps(
            {
                "status": "LODO_RIDGE_LEARNABILITY_OK",
                "selection": report["selection"],
                "optimistic_min_fee_current": current_metric,
                "hindsight_best_of_ten": hindsight_metric,
                "conclusion": report["conclusion"],
                "v1_selected_projection_exact_match": True,
                "report_out": args.report_out,
                "report_sha256": hashlib.sha256(
                    report_bytes
                ).hexdigest(),
                "predictions_out": args.predictions_out,
                "predictions_sha256": sha256_path(
                    args.predictions_out
                ),
                "date_2026_07_23_read": False,
                "date_2026_07_26_read": False,
            },
            indent=2,
            allow_nan=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
