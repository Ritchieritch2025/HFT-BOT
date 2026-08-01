#!/usr/bin/env python3
"""Finalize the frozen post-fill fee-precision boundary addendum.

Inputs are the sealed original report/episodes and one dual-accounting replay
whose old generic whole-cent PnL is required to reproduce every original row.
The replay's order-aggregated centicent value is a deterministic lower-fee
point sensitivity, not a private-fill-exact current-account fee.  Current
official mechanics round each true fill and maintain a per-order rounding
accumulator; the sealed L2 book walk does not identify same-price fill
partitioning.  The strict 0.99c-per-IOC improvement ceiling is nevertheless
partition-independent.

Only fee-dependent IOC PnL is replaced in the sensitivity artifact.  Maker
outcomes, selected policies, exit timestamps, book walks, capital time, and
every other action field remain frozen.
"""
from __future__ import annotations

import argparse
import copy
from decimal import Decimal
import gzip
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
from collections import defaultdict


ORIGINAL_REPORT_SHA256 = (
    "ff5c990cbb05e3695604c9c61612d7e8e92aeced3d4b3e12b3417e8220265cc8"
)
ORIGINAL_EPISODES_SHA256 = (
    "b4440b5822416954134f656dfe2aaf61fa3e8be0cfcdd295e62fc19f6bf3201f"
)
ORIGINAL_REPLAY_SHA256 = (
    "b667c811707b7203bc532b5d51accc1ff2e082d2fff284e76b7377e8945d1b09"
)
ORIGINAL_PREREG_SHA256 = (
    "27549b7278eaa414bc2d4c06464ae881846d36ff8f281dd3f56678a9e098a0b7"
)
DIRECT_ACCOUNT_EVIDENCE_SHA256 = (
    "29f70c2822f40ca79eb0b3cc431efa29ed950decfd6a38b0d89f402dbc64de2c"
)
AUDIT_REPLAY_SHA256 = (
    "1adf673fc90c2f7d0009bf47946e970fb5ba5ddbdfdb44da9396b99ba2741eaf"
)
POLICY_ORDER = (
    "IOC_60MS",
    "WAIT_0P25",
    "WAIT_0P5",
    "WAIT_1",
    "WAIT_2",
    "WAIT_5",
    "WAIT_10",
    "WAIT_30",
    "WAIT_60",
    "CURRENT_DIST2_TTL60",
    "CLAIRVOYANT_BEST_FIXED",
)
EXECUTABLE_FIXED_POLICIES = POLICY_ORDER[:-1]
MARKOUT_HORIZONS_S = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
AUDIT_FIELDS = (
    "ioc_fill_slices_e4",
    "ioc_filled_e4",
    "ioc_remaining_e4",
    "gross_pnl_before_ioc_fee_c_exact",
    "old_generic_whole_cent_fee_c_exact",
    "direct_centicent_fee_c_exact",
    "fee_saving_c_exact",
    "old_pnl_reproduced_c",
    "direct_centicent_pnl_c",
)


def sha256_path(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def canonical_sha(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def load_json(path):
    with open(path, "rb") as handle:
        return json.load(handle)


def load_gzip_json(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def strip_audit_fields(value):
    if isinstance(value, dict):
        return {
            key: strip_audit_fields(item)
            for key, item in value.items()
            if key not in AUDIT_FIELDS
        }
    if isinstance(value, list):
        return [strip_audit_fields(item) for item in value]
    return value


def strip_pnl_fields(value):
    if isinstance(value, dict):
        return {
            key: strip_pnl_fields(item)
            for key, item in value.items()
            if key != "pnl_c"
        }
    if isinstance(value, list):
        return [strip_pnl_fields(item) for item in value]
    return value


def corrected_fee_audit_fields(replay_row):
    """Map frozen wrapper v1 field names to precision-correct v2 labels."""
    return {
        "ioc_fill_slices_e4": copy.deepcopy(
            replay_row["ioc_fill_slices_e4"]
        ),
        "ioc_filled_e4": replay_row["ioc_filled_e4"],
        "ioc_remaining_e4": replay_row["ioc_remaining_e4"],
        "gross_pnl_before_ioc_fee_c_exact": (
            replay_row["gross_pnl_before_ioc_fee_c_exact"]
        ),
        "old_generic_whole_cent_fee_c_exact": (
            replay_row["old_generic_whole_cent_fee_c_exact"]
        ),
        "l2_aggregate_centicent_min_fee_c_exact": (
            replay_row["direct_centicent_fee_c_exact"]
        ),
        "l2_aggregate_max_fee_saving_c_exact": (
            replay_row["fee_saving_c_exact"]
        ),
        "old_pnl_reproduced_c": replay_row["old_pnl_reproduced_c"],
        "l2_aggregate_max_pnl_c": (
            replay_row["direct_centicent_pnl_c"]
        ),
        "private_fill_exact": False,
    }


def quantile(values, q):
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * q
    lo = int(math.floor(position))
    hi = int(math.ceil(position))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (
        ordered[hi] - ordered[lo]
    ) * (position - lo)


def cluster_ci(rows, key, label, reps=5000):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["market"]].append(float(row[key]))
    markets = sorted(grouped)
    seed = int(hashlib.sha256(label.encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    boot = []
    for _ in range(reps):
        values = []
        for _market in markets:
            values.extend(grouped[rng.choice(markets)])
        boot.append(statistics.fmean(values))
    return [
        round(quantile(boot, 0.025), 4),
        round(quantile(boot, 0.975), 4),
    ]


def policy_metrics(policy, rows, current_by_episode):
    improvements = [
        {
            "market": row["market"],
            "improvement_c": (
                row["pnl_c"]
                - current_by_episode[row["episode_id"]]["pnl_c"]
            ),
        }
        for row in rows
    ]
    capital = [row["capital_time_s"] for row in rows]
    return {
        "n": len(rows),
        "markets": len({row["market"] for row in rows}),
        "ev_c_per_first_fill": round(
            statistics.fmean(row["pnl_c"] for row in rows),
            4,
        ),
        "ci95_market_cluster": cluster_ci(
            rows,
            "pnl_c",
            f"ev:{policy}",
        ),
        "improvement_vs_current_c": round(
            statistics.fmean(
                row["improvement_c"] for row in improvements
            ),
            4,
        ),
        "improvement_ci95_market_cluster": cluster_ci(
            improvements,
            "improvement_c",
            f"delta:{policy}",
        ),
        "mean_capital_time_s": round(statistics.fmean(capital), 4),
        "median_capital_time_s": round(statistics.median(capital), 4),
        "maker_pairs": sum(
            row["exit_kind"] == "maker_pair" for row in rows
        ),
        "ioc_exits": sum(row["exit_kind"] == "ioc" for row in rows),
        "settlement_exits": sum(
            row["exit_kind"] == "settlement" for row in rows
        ),
    }


def distribution_summary(rows, key, label):
    values = [float(row[key]) for row in rows]
    return {
        "mean": round(statistics.fmean(values), 4),
        "ci95_mean_market_cluster": cluster_ci(rows, key, label),
        "q10": round(quantile(values, 0.10), 4),
        "q25": round(quantile(values, 0.25), 4),
        "q50": round(quantile(values, 0.50), 4),
        "q75": round(quantile(values, 0.75), 4),
        "q90": round(quantile(values, 0.90), 4),
    }


def markout_metrics(markouts):
    first_rows = [
        {
            "market": row["market"],
            "ioc_value_c": row["first_fill"]["pnl_c"],
        }
        for row in markouts
    ]
    horizons = {}
    for horizon_s in MARKOUT_HORIZONS_S:
        key = f"{horizon_s:g}s"
        rows = []
        observation_lag = []
        book_staleness = []
        for episode in markouts:
            snapshot = episode["horizons"][key]
            first = episode["first_fill"]
            if (
                snapshot["book_asof_ts"] is not None
                and snapshot["book_asof_ts"] > snapshot["scheduled_ts"]
            ):
                raise RuntimeError("future book in adjusted markout")
            rows.append(
                {
                    "market": episode["market"],
                    "ioc_value_c": snapshot["pnl_c"],
                    "relative_to_first_fill_c": (
                        snapshot["pnl_c"] - first["pnl_c"]
                    ),
                }
            )
            observation_lag.append(
                (
                    snapshot["observed_event_ts"]
                    - snapshot["scheduled_ts"]
                )
                / 1e6
            )
            if snapshot["book_asof_ts"] is not None:
                book_staleness.append(
                    (
                        snapshot["scheduled_ts"]
                        - snapshot["book_asof_ts"]
                    )
                    / 1e6
                )
        horizons[key] = {
            "n": len(rows),
            "markets": len({row["market"] for row in rows}),
            "fee_inclusive_immediate_ioc_value_c": (
                distribution_summary(
                    rows,
                    "ioc_value_c",
                    f"markout-ioc:{key}",
                )
            ),
            "relative_to_first_fill_markout_c": (
                distribution_summary(
                    rows,
                    "relative_to_first_fill_c",
                    f"markout-relative:{key}",
                )
            ),
            "sampling_audit": {
                "mean_observation_lag_s": round(
                    statistics.fmean(observation_lag),
                    6,
                ),
                "max_observation_lag_s": round(
                    max(observation_lag),
                    6,
                ),
                "mean_book_staleness_s": round(
                    statistics.fmean(book_staleness),
                    6,
                ),
                "max_book_staleness_s": round(
                    max(book_staleness),
                    6,
                ),
            },
        }
    return {
        "definition": (
            "IOC value uses the latest book already applied before the first "
            "event at/after each horizon; relative markout subtracts the "
            "fee-inclusive IOC value at the first fill"
        ),
        "first_fill_fee_inclusive_immediate_ioc_value_c": {
            "n": len(first_rows),
            "markets": len({row["market"] for row in first_rows}),
            **distribution_summary(
                first_rows,
                "ioc_value_c",
                "markout-ioc:first-fill",
            ),
        },
        "horizons": horizons,
        "future_book_violations": 0,
    }


def row_index(rows):
    result = {}
    for row in rows:
        key = (row["policy"], row["episode_id"])
        if key in result:
            raise RuntimeError(f"duplicate policy row {key}")
        result[key] = row
    return result


def l2_aggregate_fee_summary(rows):
    old_fees = [
        Decimal(row["old_generic_whole_cent_fee_c_exact"])
        for row in rows
    ]
    direct_fees = [
        Decimal(row["l2_aggregate_centicent_min_fee_c_exact"])
        for row in rows
    ]
    savings = [
        Decimal(row["l2_aggregate_max_fee_saving_c_exact"])
        for row in rows
    ]
    if any(fee != fee.to_integral_value() for fee in old_fees):
        raise RuntimeError("old fee is not whole-cent aligned")
    if any(
        fee != fee.quantize(Decimal("0.01"))
        for fee in direct_fees
    ):
        raise RuntimeError("L2 aggregate fee is not centicent aligned")
    if any(value < 0 or value > Decimal("0.99") for value in savings):
        raise RuntimeError("fee saving outside strict bound")
    return {
        "ioc_exits": len(rows),
        "ioc_with_any_fill": sum(
            int(row["ioc_filled_e4"]) > 0 for row in rows
        ),
        "ioc_zero_fill_remainder_settlement": sum(
            int(row["ioc_filled_e4"]) == 0 for row in rows
        ),
        "old_generic_whole_cent_fee_total_c": str(sum(old_fees)),
        "l2_aggregate_centicent_min_fee_total_c": str(
            sum(direct_fees)
        ),
        "maximum_point_fee_saving_total_c": str(sum(savings)),
        "mean_maximum_point_fee_saving_per_ioc_c": str(
            sum(savings) / Decimal(len(rows))
        ),
        "min_maximum_point_fee_saving_c": str(min(savings)),
        "max_maximum_point_fee_saving_c": str(max(savings)),
        "private_fill_exact": False,
    }


def markdown_report(report):
    lines = [
        "# Post-fill discovery: fee-precision boundary addendum",
        "",
        "Status: **POST_HOC L2-AGGREGATE FEE SENSITIVITY / "
        "NO CANDIDATE / NOT LIVE AUTHORIZED**",
        "",
        "This addendum does not rerun policy selection. It holds every episode, "
        "policy, exit timestamp, maker result, capital time, and IOC book walk "
        "fixed. Its point estimate replaces only the generic whole-cent IOC "
        "fee with a once-per-order centicent ceiling over the frozen L2 "
        "book-walk slices.",
        "",
        "**Precision boundary:** that point estimate is not a private-fill-"
        "exact current-account fee. Current Kalshi documentation ceilings "
        "trade fee per true fill, then applies direct-member $0.0001 balance "
        "rounding plus an order accumulator/rebate. The L2 trajectory does "
        "not reveal how one price-level quantity partitions across resting "
        "counterparty orders. Exact current-account point PnL is therefore "
        "not identified by these artifacts.",
        "",
        "## Integrity result",
        "",
        f"- Original report SHA-256: `{report['source']['original_report_sha256']}`",
        f"- Original episodes SHA-256: `{report['source']['original_episodes_sha256']}`",
        f"- Old IOC PnL reproduced: `{report['invariants']['ioc_policy_old_pnl_exact_matches']}/{report['invariants']['ioc_policy_rows']}`",
        f"- Markout old PnL reproduced: `{report['invariants']['markout_old_pnl_exact_matches']}/{report['invariants']['markout_snapshots']}`",
        f"- Maker rows byte-identical: `{report['invariants']['maker_rows_byte_identical']}/{report['invariants']['maker_rows']}`",
        f"- Action projection identity: `{report['invariants']['action_projection_identity']}`",
        f"- 2026-07-23 read: `{str(report['date_2026_07_23_read']).lower()}`",
        "",
        "## Frozen L2-aggregate fee sensitivity",
        "",
        "| Policy | IOC exits | Old EV c/first fill | L2 aggregate EV | 95% market-cluster CI | Partition-independent strict upper bound |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for policy in POLICY_ORDER:
        old = report["old_metrics"][policy]
        new = report["l2_aggregate_centicent_metrics"][policy]
        bound = report["strict_upper_bound"][policy]
        ci = new["ci95_market_cluster"]
        lines.append(
            f"| {policy} | {new['ioc_exits']} | "
            f"{old['ev_c_per_first_fill']:.4f} | "
            f"{new['ev_c_per_first_fill']:.4f} | "
            f"[{ci[0]:.4f}, {ci[1]:.4f}] | "
            f"{bound['ev_c_per_first_fill_upper']:.6f} |"
        )
    lines.extend(
        [
            "",
            "All executable fixed policies remain negative even under the "
            "more optimistic partition-independent upper bound. The "
            "clairvoyant row remains an opportunity-level timing upper bound "
            "and is not a policy, candidate, validation result, or "
            "deployment authorization.",
            "",
            "## Formula",
            "",
            "The deterministic L2 point sensitivity is:",
            "",
            "```text",
            "fee_L2_aggregate_dollars = ceil_to_$0.0001(",
            "  sum_j 0.07 * quantity_j * price_j * (1-price_j)",
            ")",
            "PnL_L2_aggregate = PnL_old + fee_old_whole_cent "
            "- fee_L2_aggregate",
            "```",
            "",
            "For the actual current contract, each true fill's trade fee is "
            "at least its unrounded fee and non-negative rounding residue "
            "remains after rebates. Therefore the actual direct net fee is "
            "not below the once-per-order aggregate centicent ceiling. Since "
            "the sealed old fee is `ceil_to_$0.01(sum raw fee)`, actual PnL "
            "improvement is at most `0.99c` per IOC order, regardless of the "
            "unobserved fill partition. Maker pairs have no IOC fee and are "
            "unchanged.",
            "",
            "Official mechanics source: "
            "https://docs.kalshi.com/getting_started/fee_rounding "
            "(accessed 2026-07-26).",
            "",
            "## Superseded v1 wording",
            "",
            "The following draft-v1 classifications are withdrawn: "
            "`authenticated direct-account centicent fee`, `exact "
            "current-account fee repricing`, and `exact direct-centicent "
            "fees`. They are replaced by `frozen L2-aggregate lower-fee "
            "sensitivity`; only the strict 0.99c upper bound is independent "
            "of the missing private fill partition.",
            "",
            "## Decision",
            "",
            "- `candidate_status = NO_CANDIDATE`",
            "- `deployable = false`",
            "- `live_authorized = false`",
            "- no action, threshold, policy, or oracle source was reselected",
            "",
        ]
    )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-report", required=True)
    parser.add_argument("--original-episodes", required=True)
    parser.add_argument("--enriched-report", required=True)
    parser.add_argument("--enriched-episodes", required=True)
    parser.add_argument("--audit-replay", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-episodes", required=True)
    parser.add_argument("--output-md", required=True)
    args = parser.parse_args()

    if sha256_path(args.original_report) != ORIGINAL_REPORT_SHA256:
        raise RuntimeError("original report SHA mismatch")
    if sha256_path(args.original_episodes) != ORIGINAL_EPISODES_SHA256:
        raise RuntimeError("original episodes SHA mismatch")
    if sha256_path(args.audit_replay) != AUDIT_REPLAY_SHA256:
        raise RuntimeError("audit replay SHA mismatch")

    original_report = load_json(args.original_report)
    enriched_report = load_json(args.enriched_report)
    original = load_gzip_json(args.original_episodes)
    enriched = load_gzip_json(args.enriched_episodes)
    if (
        original_report["date_2026_07_23_read"] is not False
        or original["date_2026_07_23_read"] is not False
        or enriched_report["date_2026_07_23_read"] is not False
        or enriched["date_2026_07_23_read"] is not False
    ):
        raise RuntimeError("07-23 gate failed")
    if original_report["source"]["script_sha256"] != ORIGINAL_REPLAY_SHA256:
        raise RuntimeError("original replay receipt mismatch")
    if (
        original_report["source"]["preregistration_sha256"]
        != ORIGINAL_PREREG_SHA256
    ):
        raise RuntimeError("original prereg receipt mismatch")
    if enriched_report["metrics"] != original_report["metrics"]:
        raise RuntimeError("dual-accounting replay changed old metrics")
    if (
        enriched_report["causal_P3_baseline"]
        != original_report["causal_P3_baseline"]
    ):
        raise RuntimeError("dual-accounting replay changed causal baseline")

    original_rows = original["policy_rows"]
    enriched_rows = enriched["policy_rows"]
    if len(original_rows) != len(enriched_rows):
        raise RuntimeError("policy row count changed")
    original_index = row_index(original_rows)
    enriched_index = row_index(enriched_rows)
    if set(original_index) != set(enriched_index):
        raise RuntimeError("policy/episode support changed")

    adjusted_rows = []
    policy_fee_rows = []
    old_pnl_matches = 0
    maker_equal = 0
    maker_count = 0
    for original_row in original_rows:
        key = (original_row["policy"], original_row["episode_id"])
        replay_row = enriched_index[key]
        if strip_audit_fields(replay_row) != original_row:
            raise RuntimeError(f"old policy row failed reproduction {key}")
        adjusted = copy.deepcopy(original_row)
        if original_row["exit_kind"] == "maker_pair":
            maker_count += 1
            if canonical_bytes(replay_row) != canonical_bytes(original_row):
                raise RuntimeError(
                    f"maker/paired replay row not byte-identical {key}"
                )
            maker_equal += 1
        elif original_row["exit_kind"] == "ioc":
            old_pnl = float(replay_row["old_pnl_reproduced_c"])
            if abs(old_pnl - float(original_row["pnl_c"])) > 1e-12:
                raise RuntimeError(f"old IOC PnL mismatch {key}")
            old_pnl_matches += 1
            adjusted["pnl_c"] = float(
                replay_row["direct_centicent_pnl_c"]
            )
            policy_fee_rows.append(
                {
                    "policy": original_row["policy"],
                    "episode_id": original_row["episode_id"],
                    "market": original_row["market"],
                    "exit_ts": original_row["exit_ts"],
                    **corrected_fee_audit_fields(replay_row),
                }
            )
        if (
            canonical_bytes(strip_pnl_fields(adjusted))
            != canonical_bytes(strip_pnl_fields(original_row))
        ):
            raise RuntimeError(f"action projection changed {key}")
        adjusted_rows.append(adjusted)

    original_markouts = original["markout_rows"]
    enriched_markouts = enriched["markout_rows"]
    original_markout_index = {
        row["episode_id"]: row for row in original_markouts
    }
    enriched_markout_index = {
        row["episode_id"]: row for row in enriched_markouts
    }
    if set(original_markout_index) != set(enriched_markout_index):
        raise RuntimeError("markout episode support changed")
    adjusted_markouts = []
    markout_fee_rows = []
    markout_old_matches = 0
    for original_markout in original_markouts:
        episode_id = original_markout["episode_id"]
        replay_markout = enriched_markout_index[episode_id]
        if strip_audit_fields(replay_markout) != original_markout:
            raise RuntimeError(
                f"old markout failed reproduction {episode_id}"
            )
        adjusted = copy.deepcopy(original_markout)
        snapshots = [
            ("first_fill", replay_markout["first_fill"]),
            *[
                (
                    f"{horizon:g}s",
                    replay_markout["horizons"][f"{horizon:g}s"],
                )
                for horizon in MARKOUT_HORIZONS_S
            ],
        ]
        for name, replay_snapshot in snapshots:
            original_snapshot = (
                original_markout["first_fill"]
                if name == "first_fill"
                else original_markout["horizons"][name]
            )
            adjusted_snapshot = (
                adjusted["first_fill"]
                if name == "first_fill"
                else adjusted["horizons"][name]
            )
            if (
                abs(
                    float(replay_snapshot["old_pnl_reproduced_c"])
                    - float(original_snapshot["pnl_c"])
                )
                > 1e-12
            ):
                raise RuntimeError(
                    f"old markout PnL mismatch {episode_id}/{name}"
                )
            markout_old_matches += 1
            adjusted_snapshot["pnl_c"] = float(
                replay_snapshot["direct_centicent_pnl_c"]
            )
            markout_fee_rows.append(
                {
                    "episode_id": episode_id,
                    "market": original_markout["market"],
                    "horizon": name,
                    **corrected_fee_audit_fields(replay_snapshot),
                }
            )
        if (
            canonical_bytes(strip_pnl_fields(adjusted))
            != canonical_bytes(strip_pnl_fields(original_markout))
        ):
            raise RuntimeError(
                f"markout projection changed {episode_id}"
            )
        adjusted_markouts.append(adjusted)

    by_old = defaultdict(list)
    by_adjusted = defaultdict(list)
    by_fee = defaultdict(list)
    for row in original_rows:
        by_old[row["policy"]].append(row)
    for row in adjusted_rows:
        by_adjusted[row["policy"]].append(row)
    for row in policy_fee_rows:
        by_fee[row["policy"]].append(row)
    if tuple(by_old) != POLICY_ORDER or tuple(by_adjusted) != POLICY_ORDER:
        raise RuntimeError("policy order changed")
    old_current = {
        row["episode_id"]: row
        for row in by_old["CURRENT_DIST2_TTL60"]
    }
    adjusted_current = {
        row["episode_id"]: row
        for row in by_adjusted["CURRENT_DIST2_TTL60"]
    }
    recomputed_old_metrics = {
        policy: policy_metrics(policy, by_old[policy], old_current)
        for policy in POLICY_ORDER
    }
    if recomputed_old_metrics != original_report["metrics"]:
        raise RuntimeError("original metrics do not reproduce from rows")
    direct_metrics = {
        policy: policy_metrics(
            policy,
            by_adjusted[policy],
            adjusted_current,
        )
        for policy in POLICY_ORDER
    }
    old_markout_metrics = markout_metrics(original_markouts)
    if (
        old_markout_metrics
        != original_report["post_first_fill_ioc_markouts"]
    ):
        raise RuntimeError("original markout metrics do not reproduce")
    direct_markout_metrics = markout_metrics(adjusted_markouts)

    fee_by_policy = {
        policy: l2_aggregate_fee_summary(by_fee[policy])
        for policy in POLICY_ORDER
    }
    strict_upper = {}
    for policy in POLICY_ORDER:
        old_exact = statistics.fmean(
            row["pnl_c"] for row in by_old[policy]
        )
        ioc_count = len(by_fee[policy])
        upper = (
            Decimal(str(old_exact))
            + Decimal(ioc_count)
            * Decimal("0.99")
            / Decimal(len(by_old[policy]))
        )
        strict_upper[policy] = {
            "proof": (
                "old exact mean + IOC_count/N * 0.99c; action and "
                "oracle source frozen"
            ),
            "old_exact_ev_c_per_first_fill": old_exact,
            "ioc_count": ioc_count,
            "n": len(by_old[policy]),
            "ev_c_per_first_fill_upper": float(upper),
        }

    action_projection_original = canonical_sha(
        strip_pnl_fields(original_rows)
    )
    action_projection_adjusted = canonical_sha(
        strip_pnl_fields(adjusted_rows)
    )
    maker_original = [
        row for row in original_rows if row["exit_kind"] == "maker_pair"
    ]
    maker_adjusted = [
        row for row in adjusted_rows if row["exit_kind"] == "maker_pair"
    ]
    adjusted_artifact = {
        "schema": (
            "z3-postfill-l2-aggregate-min-fee-adjusted-episodes-v2"
        ),
        "analysis_class": (
            "POST_HOC_L2_AGGREGATE_MIN_FEE_SENSITIVITY_NO_RESELECTION"
        ),
        "status": "DISCOVERY_ONLY_NO_CANDIDATE",
        "candidate_status": "NO_CANDIDATE",
        "deployable": False,
        "live_authorized": False,
        "historical_validation_claim": False,
        "fee_only_sensitivity": True,
        "date_2026_07_23_read": False,
        "source": {
            "original_report_sha256": ORIGINAL_REPORT_SHA256,
            "original_episodes_sha256": ORIGINAL_EPISODES_SHA256,
            "enriched_report_sha256": sha256_path(args.enriched_report),
            "enriched_episodes_sha256": sha256_path(
                args.enriched_episodes
            ),
            "audit_replay_sha256": AUDIT_REPLAY_SHA256,
            "v2_finalizer_sha256": sha256_path(Path(__file__).resolve()),
        },
        "policy_rows": adjusted_rows,
        "markout_rows": adjusted_markouts,
        "policy_ioc_fee_audit_rows": policy_fee_rows,
        "markout_ioc_fee_audit_rows": markout_fee_rows,
        "shadow_current_audit": original["shadow_current_audit"],
    }
    with gzip.open(
        args.output_episodes,
        "wt",
        encoding="utf-8",
        compresslevel=6,
    ) as handle:
        json.dump(
            adjusted_artifact,
            handle,
            separators=(",", ":"),
            allow_nan=False,
        )

    report = {
        "schema": (
            "z3-postfill-fee-precision-boundary-addendum-v2"
        ),
        "analysis_class": (
            "POST_HOC_L2_AGGREGATE_MIN_FEE_SENSITIVITY_NO_RESELECTION"
        ),
        "status": "DISCOVERY_ONLY_NO_CANDIDATE",
        "candidate_status": "NO_CANDIDATE",
        "deployable": False,
        "live_authorized": False,
        "historical_validation_claim": False,
        "fee_only_sensitivity": True,
        "date_2026_07_23_read": False,
        "dates_opened": ["2026-07-20", "2026-07-21", "2026-07-22"],
        "source": {
            "original_report_sha256": ORIGINAL_REPORT_SHA256,
            "original_episodes_sha256": ORIGINAL_EPISODES_SHA256,
            "original_replay_sha256": ORIGINAL_REPLAY_SHA256,
            "original_prereg_sha256": ORIGINAL_PREREG_SHA256,
            "audit_replay_sha256": AUDIT_REPLAY_SHA256,
            "v2_finalizer_sha256": sha256_path(Path(__file__).resolve()),
            "enriched_report_sha256": sha256_path(args.enriched_report),
            "enriched_episodes_sha256": sha256_path(
                args.enriched_episodes
            ),
            "adjusted_episodes_path": str(
                Path(args.output_episodes).resolve()
            ),
            "adjusted_episodes_sha256": sha256_path(
                args.output_episodes
            ),
        },
        "account_fee_contract": {
            "old": (
                "generic: sum 7% quadratic fee across exact IOC slices, "
                "ceil once to $0.01"
            ),
            "l2_aggregate_point_sensitivity": (
                "sum 7% quadratic raw fee across frozen L2 price-level "
                "book-walk slices, ceil once to $0.0001; this is a minimum-"
                "fee / maximum-PnL point, not private-fill-exact"
            ),
            "current_official_contract": (
                "for each true fill: trade fee ceiled to $0.0001; signed "
                "revenue less trade fee floored to the direct-member target "
                "balance precision of $0.0001; rounding residue accumulated "
                "per order and whole-cent rebates issued subject to "
                "non-negative net fee"
            ),
            "official_fee_rounding_url": (
                "https://docs.kalshi.com/getting_started/fee_rounding"
            ),
            "official_fee_rounding_markdown_sha256": (
                "c9b8c7efd50df6512a4528e5a86044ad40699e17ababc7f9c42497c722829796"
            ),
            "official_source_accessed_utc_date": "2026-07-26",
            "direct_account_evidence_sha256": (
                DIRECT_ACCOUNT_EVIDENCE_SHA256
            ),
            "authenticated_probe_example": {
                "price_dollars": "0.6800",
                "quantity_contracts": "1.00",
                "unrounded_fee_dollars": "0.015232",
                "reported_fee_dollars": "0.0153",
            },
            "authenticated_probe_scope": (
                "one whole contract at one price; it confirms that example "
                "but does not identify hypothetical same-price fill "
                "partitioning in the L2 replay"
            ),
            "private_fill_partition_observed": False,
            "current_account_exact_point_identified": False,
            "exact_reconciliation_requirements": {
                "private_fill_fields": [
                    "fill_id",
                    "trade_id",
                    "order_id",
                    "count_fp",
                    "yes_price_dollars/no_price_dollars",
                    "is_taker",
                    "fee_cost",
                ],
                "order_fields": [
                    "order_id",
                    "fill_count_fp",
                    "taker_fill_cost_dollars",
                    "taker_fees_dollars",
                ],
                "additional_identity": (
                    "pre/post balance and position fees_paid_dollars grouped "
                    "by subaccount and order_id"
                ),
            },
            "l2_aggregate_fee_is_lower_bound_on_true_direct_net_fee": True,
            "maximum_true_pnl_improvement_per_ioc_order_c": "0.99",
            "upper_bound_proof": (
                "sum_i ceil_0.0001(raw_fee_i) is at least "
                "ceil_0.0001(sum_i raw_fee_i), and post-rebate rounding "
                "residue is non-negative; therefore true direct net fee is "
                "at least the L2 aggregate centicent fee. Old fee is "
                "ceil_0.01(sum raw), so old fee minus true direct fee is "
                "at most $0.0099 = 0.99c"
            ),
        },
        "precision_boundary": {
            "identified": {
                "sealed_old_generic_whole_cent_fee": True,
                "frozen_l2_aggregate_min_fee_point": True,
                "partition_independent_pnl_improvement_upper_bound": True,
            },
            "not_identified": {
                "same_price_counterparty_fill_partition": True,
                "private_fill_exact_trade_fee_sum": True,
                "private_fill_exact_rounding_accumulator_path": True,
                "current_account_exact_point_pnl": True,
            },
            "superseded_v1_phrases": [
                "authenticated direct-account centicent fee",
                "exact current-account fee repricing",
                "exact direct-centicent fees",
            ],
            "corrected_classification": (
                "OPTIMISTIC_L2_AGGREGATE_MIN_FEE_MAX_PNL_SENSITIVITY"
            ),
        },
        "frozen_scope": {
            "episode_support": True,
            "policy": True,
            "selected_oracle_source": True,
            "exit_ts": True,
            "capital_time": True,
            "maker_queue_and_pair_results": True,
            "ioc_book_asof_and_book_walk": True,
            "only_changed_field": "IOC fee and fee-dependent pnl_c",
            "selection_performed": False,
        },
        "invariants": {
            "policy_rows": len(original_rows),
            "ioc_policy_rows": len(policy_fee_rows),
            "ioc_policy_old_pnl_exact_matches": old_pnl_matches,
            "maker_rows": maker_count,
            "maker_rows_byte_identical": maker_equal,
            "maker_rows_original_sha256": canonical_sha(
                maker_original
            ),
            "maker_rows_adjusted_sha256": canonical_sha(
                maker_adjusted
            ),
            "action_projection_original_sha256": (
                action_projection_original
            ),
            "action_projection_adjusted_sha256": (
                action_projection_adjusted
            ),
            "action_projection_identity": (
                action_projection_original
                == action_projection_adjusted
            ),
            "markout_snapshots": len(markout_fee_rows),
            "markout_old_pnl_exact_matches": markout_old_matches,
            "old_aggregate_metrics_reproduced": True,
            "old_markout_metrics_reproduced": True,
            "07_23_false": True,
        },
        "old_metrics": original_report["metrics"],
        "l2_aggregate_centicent_metrics": direct_metrics,
        "l2_aggregate_fee_audit_by_policy": fee_by_policy,
        "strict_upper_bound": strict_upper,
        "l2_aggregate_centicent_markouts": direct_markout_metrics,
        "decision": {
            "all_executable_fixed_policy_ev_negative": all(
                direct_metrics[policy]["ev_c_per_first_fill"] < 0
                for policy in EXECUTABLE_FIXED_POLICIES
            ),
            "clairvoyant_is_headroom_only": True,
            "candidate": False,
            "live_authorized": False,
            "interpretation": (
                "The optimistic L2-aggregate minimum-fee point improves IOC "
                "economics but does not make any frozen executable policy "
                "positive. More importantly, every executable policy remains "
                "negative under the partition-independent 0.99c-per-IOC "
                "maximum-PnL bound. The frozen clairvoyant source row remains "
                "diagnostic headroom only."
            ),
        },
    }
    Path(args.output_json).write_text(
        json.dumps(
            report,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    Path(args.output_md).write_text(
        markdown_report(report),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "output_json": args.output_json,
                "output_json_sha256": sha256_path(args.output_json),
                "output_episodes": args.output_episodes,
                "output_episodes_sha256": sha256_path(
                    args.output_episodes
                ),
                "output_md": args.output_md,
                "output_md_sha256": sha256_path(args.output_md),
                "invariants": report["invariants"],
                "decision": report["decision"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
