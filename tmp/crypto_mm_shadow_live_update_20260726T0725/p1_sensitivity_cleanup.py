#!/usr/bin/env python3
"""Locked P1 sensitivity cleanup on the frozen corrected shadow receipts.

This script is intentionally post-hoc and local-only.  It:

* removes clean episodes whose first fill was within 60 seconds after the
  final external-probe fill;
* evaluates every policy on the same remaining episode set;
* never drops a row because displayed touch depth is short;
* uses exact Decimal/centicent fee arithmetic; and
* reports three locked state rules without searching or ranking a grid.

When the latest causal quote shows less than one contract at touch, the
conservative touch+1c IOC is treated as unproven.  The fail-closed outcome is
the worse of its modeled P&L and the episode's observed original-policy P&L.
That fallback uses future outcome information and is therefore a conservative
post-hoc sensitivity convention, not a causal execution estimate.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import random
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SUMMARY = ROOT / "shadow_24h_diagnostic.json"
STATE = ROOT / "post_fill_state_exploration.json"
SOURCE = ROOT / "source_snapshot"
JSON_OUT = ROOT / "p1_sensitivity_cleanup.json"
MD_OUT = ROOT / "p1_sensitivity_cleanup.md"

HORIZONS_S = (
    Decimal("0.25"),
    Decimal("0.5"),
    Decimal("1"),
    Decimal("2"),
    Decimal("5"),
    Decimal("10"),
    Decimal("30"),
    Decimal("60"),
)
COOLDOWN_S = Decimal("60")
MAX_QUOTE_AGE_S = Decimal("1.5")
DEPTH_REQUIRED = Decimal("1")
CENTICENT = Decimal("0.0001")
ONE = Decimal("1")
BOOT_N = 10_000
BOOT_SEED = 2026072604

# Locked before this sensitivity run.  These rows are reported in declaration
# order and are not ranked or used to select a deployable candidate.
STATE_RULES = (
    {
        "id": "prior_screen_pair_profit_le_1c",
        "feature": "pair_profit_c",
        "operator": "le",
        "threshold": Decimal("1"),
        "short_horizon_s": Decimal("0.25"),
        "long_horizon_s": Decimal("10"),
        "origin": (
            "single prior-screen winner retained only as a sensitivity; "
            "not a fresh out-of-sample candidate"
        ),
    },
    {
        "id": "queue_proxy_eta_le_5s",
        "feature": "opposite_clear_eta_s",
        "operator": "le",
        "threshold": Decimal("5"),
        "short_horizon_s": Decimal("0.25"),
        "long_horizon_s": Decimal("10"),
        "origin": "round operational threshold fixed for this sensitivity",
    },
    {
        "id": "opposite_flow_10s_ge_2500",
        "feature": "opposite_flow_10s",
        "operator": "ge",
        "threshold": Decimal("2500"),
        "short_horizon_s": Decimal("0.25"),
        "long_horizon_s": Decimal("10"),
        "origin": "round operational threshold fixed for this sensitivity",
    },
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def as_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def horizon_key(value: Decimal) -> str:
    return str(float(value))


def taker_fee(price: Decimal, count: Decimal = ONE) -> Decimal:
    position_cost = price * count
    raw = Decimal("0.07") * count * price * (ONE - price)
    total = (position_cost + raw).quantize(
        CENTICENT, rounding=ROUND_CEILING
    )
    return total - position_cost


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    x = (len(ordered) - 1) * p
    lo = math.floor(x)
    hi = math.ceil(x)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - x) + ordered[hi] * (x - lo)


def cluster_ci(rows: list[dict], key: str) -> list[float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        grouped[str(row["market"])].append(float(row[key]))
    markets = sorted(grouped)
    if len(markets) < 2:
        raise ValueError("cluster CI requires at least two markets")
    rng = random.Random(BOOT_SEED)
    means = []
    for _ in range(BOOT_N):
        values = []
        for _market in markets:
            picked = markets[rng.randrange(len(markets))]
            values.extend(grouped[picked])
        means.append(sum(values) / len(values))
    return [
        round(percentile(means, 0.025), 8),
        round(percentile(means, 0.975), 8),
    ]


def summarize(rows: list[dict]) -> dict:
    pnl = [float(row["pnl_usd"]) for row in rows]
    waits = [float(row["capital_time_s"]) for row in rows]
    return {
        "n": len(rows),
        "markets": len({row["market"] for row in rows}),
        "total_usd": round(sum(pnl), 8),
        "mean_usd_per_cycle": round(sum(pnl) / len(pnl), 8),
        "market_cluster_ci95_mean_usd": cluster_ci(rows, "pnl_usd"),
        "mean_capital_time_s": round(sum(waits) / len(waits), 8),
        "maker_completed_n": sum(
            row["action"] == "maker_completed" for row in rows
        ),
        "conservative_ioc_modeled_n": sum(
            row["action"] == "conservative_ioc_modeled" for row in rows
        ),
        "fail_closed_original_path_n": sum(
            row["action"] == "fail_closed_original_path"
            for row in rows
        ),
    }


def load_quotes() -> tuple[dict[str, list[int]], dict[str, list[dict]]]:
    by_market: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(SOURCE.glob("mm_*.ndjson")):
        with path.open(encoding="utf-8") as src:
            for line_no, line in enumerate(src, 1):
                row = json.loads(line)
                if row.get("ev") != "QUOTE_EVAL" or not row.get("mt"):
                    continue
                row["_source_file"] = path.name
                row["_source_line"] = line_no
                by_market[str(row["mt"])].append(row)
    times: dict[str, list[int]] = {}
    for market, rows in by_market.items():
        rows.sort(key=lambda row: int(row["wall_ns"]))
        times[market] = [int(row["wall_ns"]) for row in rows]
    return times, by_market


def quote_at_or_before(
    market: str,
    decision_ns: int,
    times: dict[str, list[int]],
    quotes: dict[str, list[dict]],
) -> tuple[dict | None, str | None]:
    market_times = times.get(market, [])
    index = bisect.bisect_right(market_times, decision_ns) - 1
    if index < 0:
        return None, "no_quote"
    quote = quotes[market][index]
    age_ns = decision_ns - int(quote["wall_ns"])
    if age_ns < 0:
        return None, "future_quote"
    if Decimal(age_ns) / Decimal(1_000_000_000) > MAX_QUOTE_AGE_S:
        return None, "stale_quote"
    return quote, None


def complementary_touch(
    first_side: str, quote: dict
) -> tuple[Decimal, Decimal] | None:
    if first_side == "bid":
        yb = quote.get("yb")
        depth = quote.get("y_touch_ct")
        if yb is None or depth is None:
            return None
        return ONE - as_decimal(yb) / Decimal("10000"), as_decimal(depth)
    if first_side == "ask_no":
        ya = quote.get("ya")
        depth = quote.get("n_touch_ct")
        if ya is None or depth is None:
            return None
        return as_decimal(ya) / Decimal("10000"), as_decimal(depth)
    return None


def make_fixed_row(
    cycle: dict,
    horizon: Decimal,
    times: dict[str, list[int]],
    quotes: dict[str, list[dict]],
) -> dict:
    first = cycle["first_fill"]
    first_wall_ns = int(first["source"]["wall_ns"])
    horizon_ns = int(horizon * Decimal(1_000_000_000))
    decision_ns = first_wall_ns + horizon_ns

    if cycle["class"] == "natural_maker_pair":
        lock_wall_ns = int(cycle["pair_locks"][0]["source"]["wall_ns"])
        if lock_wall_ns <= decision_ns:
            local_wait = Decimal(lock_wall_ns - first_wall_ns) / Decimal(
                1_000_000_000
            )
            return {
                "market": cycle["market"],
                "completed_cycle": cycle["completed_cycle"],
                "horizon_s": float(horizon),
                "action": "maker_completed",
                "pnl_usd": float(as_decimal(cycle["pnl_usd"])),
                "capital_time_s": float(local_wait),
                "decision_wall_ns": decision_ns,
                "fallback_reason": None,
            }

    quote, quote_error = quote_at_or_before(
        str(cycle["market"]), decision_ns, times, quotes
    )
    if quote is None:
        return {
            "market": cycle["market"],
            "completed_cycle": cycle["completed_cycle"],
            "horizon_s": float(horizon),
            "action": "fail_closed_original_path",
            "pnl_usd": float(as_decimal(cycle["pnl_usd"])),
            "capital_time_s": float(horizon),
            "decision_wall_ns": decision_ns,
            "fallback_reason": quote_error,
        }

    touch = complementary_touch(str(first["side"]), quote)
    if touch is None:
        return {
            "market": cycle["market"],
            "completed_cycle": cycle["completed_cycle"],
            "horizon_s": float(horizon),
            "action": "fail_closed_original_path",
            "pnl_usd": float(as_decimal(cycle["pnl_usd"])),
            "capital_time_s": float(horizon),
            "decision_wall_ns": decision_ns,
            "fallback_reason": "unparseable_touch",
        }

    touch_px, depth = touch
    if not Decimal("0") < touch_px < ONE:
        return {
            "market": cycle["market"],
            "completed_cycle": cycle["completed_cycle"],
            "horizon_s": float(horizon),
            "action": "fail_closed_original_path",
            "pnl_usd": float(as_decimal(cycle["pnl_usd"])),
            "capital_time_s": float(horizon),
            "decision_wall_ns": decision_ns,
            "fallback_reason": "invalid_touch",
        }

    conservative_px = min(Decimal("0.99"), touch_px + Decimal("0.01"))
    fee = taker_fee(conservative_px)
    modeled_pnl = (
        ONE
        - as_decimal(first["price_dollars"])
        - conservative_px
        - fee
    )
    common = {
        "market": cycle["market"],
        "completed_cycle": cycle["completed_cycle"],
        "horizon_s": float(horizon),
        "capital_time_s": float(horizon),
        "decision_wall_ns": decision_ns,
        "quote_wall_ns": int(quote["wall_ns"]),
        "quote_age_s": round(
            (decision_ns - int(quote["wall_ns"])) / 1e9, 9
        ),
        "quote_source": (
            f"{quote['_source_file']}:{quote['_source_line']}"
        ),
        "touch_px": float(touch_px),
        "touch_depth_ct": float(depth),
        "conservative_ioc_limit_px": float(conservative_px),
        "conservative_ioc_fee_usd": float(fee),
        "conservative_ioc_modeled_pnl_usd": float(modeled_pnl),
    }
    if depth < DEPTH_REQUIRED:
        observed_pnl = as_decimal(cycle["pnl_usd"])
        fail_closed_pnl = min(modeled_pnl, observed_pnl)
        return {
            **common,
            "action": "fail_closed_original_path",
            "pnl_usd": float(fail_closed_pnl),
            "fallback_reason": "touch_depth_lt_1",
            "fallback_modeled_ioc_pnl_usd": float(modeled_pnl),
            "fallback_observed_original_pnl_usd": float(observed_pnl),
            "fallback_selected": (
                "modeled_ioc"
                if modeled_pnl <= observed_pnl
                else "observed_original_path"
            ),
        }
    return {
        **common,
        "action": "conservative_ioc_modeled",
        "pnl_usd": float(modeled_pnl),
        "fallback_reason": None,
    }


def choose_long(value: Decimal, operator: str, threshold: Decimal) -> bool:
    if operator == "le":
        return value <= threshold
    if operator == "ge":
        return value >= threshold
    raise ValueError(f"unknown operator: {operator}")


def render_markdown(report: dict) -> str:
    fixed_lines = []
    for row in report["fixed_policies"]:
        s = row["summary"]
        fixed_lines.append(
            "| {h:g} | {n} | {fallback} | {total:.4f} | {mean:.4f}c | "
            "[{lo:.4f}, {hi:.4f}]c |".format(
                h=row["horizon_s"],
                n=s["n"],
                fallback=s["fail_closed_original_path_n"],
                total=s["total_usd"],
                mean=s["mean_usd_per_cycle"] * 100,
                lo=s["market_cluster_ci95_mean_usd"][0] * 100,
                hi=s["market_cluster_ci95_mean_usd"][1] * 100,
            )
        )
    state_lines = []
    for row in report["locked_state_rules"]:
        s = row["summary"]
        state_lines.append(
            "| `{rule}` | {n} | {missing} | {fallback} | {total:.4f} | "
            "{mean:.4f}c | [{lo:.4f}, {hi:.4f}]c |".format(
                rule=row["id"],
                n=s["n"],
                missing=row["missing_feature_default_n"],
                fallback=s["fail_closed_original_path_n"],
                total=s["total_usd"],
                mean=s["mean_usd_per_cycle"] * 100,
                lo=s["market_cluster_ci95_mean_usd"][0] * 100,
                hi=s["market_cluster_ci95_mean_usd"][1] * 100,
            )
        )
    verdict = (
        "All reported fixed horizons and all three locked state rules remain "
        "negative on the common episode set."
        if report["checks"]["all_reported_means_negative"]
        else "At least one reported mean is non-negative."
    )
    return f"""# P1 fixed-cohort sensitivity cleanup

Status: **post-hoc sensitivity only; not validation and not candidate
selection**.

## Common cohort

- Source clean cycles: {report['cohort']['source_clean_n']}
- External-probe cooldown: {report['cohort']['cooldown_s']:g}s after the
  final non-shadow fill
- Cooldown exclusions: {report['cohort']['excluded_cycle_ids']}
- Common episode count for every reported policy:
  **{report['cohort']['analysis_n']}**

No policy drops a missing horizon row. If the causal quote is missing/stale,
the row uses the observed original episode P&L. If touch depth is below one,
the row uses the worse of that original P&L and the modeled current touch+1c
IOC with exact centicent fees. This convention is deliberately pessimistic
and post-hoc; it is not proof that the IOC would execute.

## Fixed conservative exit horizons

| horizon | n | fail-closed rows | total USD | mean/cycle | market-cluster CI |
| ---: | ---: | ---: | ---: | ---: | --- |
{chr(10).join(fixed_lines)}

## Locked state-rule sensitivities

Rules are listed in their fixed declaration order. They were not searched or
ranked in this run. A missing state feature defaults to the rule's short
horizon.

| rule | n | missing-state defaults | fail-closed rows | total USD | mean/cycle | market-cluster CI |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
{chr(10).join(state_lines)}

## Verdict

{verdict}

The intervals use only eight 15-minute market clusters and remain descriptive.
The roughly 1 Hz quote receipts still cannot establish executable sub-second
touch prices.
"""


def main() -> None:
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
    state = json.loads(STATE.read_text(encoding="utf-8"))
    cycles = summary["cycle_outcomes"]["cycles"]
    clean = [
        cycle for cycle in cycles
        if cycle["class"] in ("natural_maker_pair", "unpaired_age_forced")
        and int(cycle.get("external_fill_n", 0)) == 0
    ]
    external_wall_ns = [
        int(fill["source"]["wall_ns"])
        for cycle in cycles
        if cycle["class"] == "external_fill_contaminated"
        for fill in cycle.get("external_fills", [])
    ]
    if not external_wall_ns:
        raise ValueError("external probe fill not found")
    last_external_ns = max(external_wall_ns)
    cooldown_ns = int(COOLDOWN_S * Decimal(1_000_000_000))
    excluded = [
        cycle for cycle in clean
        if 0
        < int(cycle["first_fill"]["source"]["wall_ns"]) - last_external_ns
        <= cooldown_ns
    ]
    excluded_ids = {
        int(cycle["completed_cycle"]) for cycle in excluded
    }
    cohort = [
        cycle for cycle in clean
        if int(cycle["completed_cycle"]) not in excluded_ids
    ]
    expected_exclusions = {37, 38}
    if excluded_ids != expected_exclusions:
        raise ValueError(
            f"unexpected cooldown exclusion set: {sorted(excluded_ids)}"
        )

    state_map = {
        (str(row["market"]), int(row["completed_cycle"])): row
        for row in state["rows"]
    }
    times, quotes = load_quotes()
    fixed_maps: dict[Decimal, dict[tuple[str, int], dict]] = {}
    fixed_policies = []
    for horizon in HORIZONS_S:
        rows = [
            make_fixed_row(cycle, horizon, times, quotes)
            for cycle in cohort
        ]
        fixed_maps[horizon] = {
            (str(row["market"]), int(row["completed_cycle"])): row
            for row in rows
        }
        fixed_policies.append({
            "horizon_s": float(horizon),
            "summary": summarize(rows),
            "rows": rows,
        })

    locked_rules = []
    for rule in STATE_RULES:
        rows = []
        missing_feature_n = 0
        long_n = 0
        for cycle in cohort:
            identity = (
                str(cycle["market"]), int(cycle["completed_cycle"])
            )
            state_row = state_map.get(identity)
            feature_value = (
                None
                if state_row is None
                else state_row["features"].get(rule["feature"])
            )
            if feature_value is None:
                horizon = rule["short_horizon_s"]
                missing_feature_n += 1
                state_action = "missing_feature_default_short"
            else:
                long = choose_long(
                    as_decimal(feature_value),
                    str(rule["operator"]),
                    rule["threshold"],
                )
                horizon = (
                    rule["long_horizon_s"]
                    if long else rule["short_horizon_s"]
                )
                long_n += int(long)
                state_action = "long" if long else "short"
            base = fixed_maps[horizon][identity]
            rows.append({
                **base,
                "state_action": state_action,
                "chosen_horizon_s": float(horizon),
            })
        locked_rules.append({
            "id": rule["id"],
            "feature": rule["feature"],
            "operator": rule["operator"],
            "threshold": float(rule["threshold"]),
            "short_horizon_s": float(rule["short_horizon_s"]),
            "long_horizon_s": float(rule["long_horizon_s"]),
            "origin": rule["origin"],
            "missing_feature_default": "short_horizon",
            "missing_feature_default_n": missing_feature_n,
            "long_action_n": long_n,
            "summary": summarize(rows),
            "rows": rows,
        })

    all_summaries = [
        row["summary"] for row in fixed_policies
    ] + [
        row["summary"] for row in locked_rules
    ]
    cohort_ids = [int(cycle["completed_cycle"]) for cycle in cohort]
    report = {
        "schema": "crypto-mm-p1-fixed-cohort-sensitivity-v1",
        "status": "POST_HOC_LOCKED_SENSITIVITY_ONLY",
        "not_validation": True,
        "not_candidate_selection": True,
        "read_only_frozen_sources": True,
        "source_summary_sha256": sha256(SUMMARY),
        "source_state_sha256": sha256(STATE),
        "source_receipts": {
            path.name: sha256(path)
            for path in sorted(SOURCE.glob("mm_*.ndjson"))
        },
        "cohort": {
            "source_clean_n": len(clean),
            "last_external_fill_wall_ns": last_external_ns,
            "cooldown_s": float(COOLDOWN_S),
            "excluded_cycle_ids": sorted(excluded_ids),
            "excluded_first_fill_after_external_s": {
                str(cycle["completed_cycle"]): round(
                    (
                        int(cycle["first_fill"]["source"]["wall_ns"])
                        - last_external_ns
                    ) / 1e9,
                    9,
                )
                for cycle in excluded
            },
            "analysis_n": len(cohort),
            "episode_ids": cohort_ids,
        },
        "locked_assumptions": {
            "decision_clock": (
                "first-fill local receipt wall_ns plus exact integer "
                "horizon; maker completion also uses local receipt wall_ns"
            ),
            "quote_causality": (
                "latest same-market QUOTE_EVAL.wall_ns <= decision wall_ns"
            ),
            "max_quote_age_s": float(MAX_QUOTE_AGE_S),
            "execution_model": (
                "touch+1c IOC with exact general taker fee when displayed "
                "touch depth is at least one contract"
            ),
            "missing_horizon_fail_closed": (
                "if quote missing/stale/unparseable, use observed original "
                "episode P&L; if touch depth <1, use the worse of modeled "
                "touch+1c IOC and observed original P&L; never drop the row"
            ),
            "missing_state_feature": "choose the rule's short horizon",
            "fee": (
                "Decimal: ceil_centicent(position_cost + "
                "0.07*C*P*(1-P)) - position_cost; C=1"
            ),
            "queue_eta_semantics": (
                "opposite_clear_eta_s is a displayed-level pressure proxy, "
                "not own-order queue position"
            ),
            "subsecond_limit": (
                "QUOTE_EVAL is about 1Hz; subsecond rows are coarse and "
                "not executable latency estimates"
            ),
        },
        "fixed_policies": fixed_policies,
        "locked_state_rules": locked_rules,
        "checks": {
            "cooldown_exclusions_are_37_38": (
                excluded_ids == expected_exclusions
            ),
            "every_policy_same_episode_n": all(
                row["n"] == len(cohort) for row in all_summaries
            ),
            "every_policy_has_eight_markets": all(
                row["markets"] == 8 for row in all_summaries
            ),
            "all_reported_means_negative": all(
                row["mean_usd_per_cycle"] < 0 for row in all_summaries
            ),
            "no_policy_row_dropped": all(
                row["n"] == len(cohort) for row in all_summaries
            ),
        },
    }
    JSON_OUT.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    MD_OUT.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({
        "json": str(JSON_OUT),
        "json_sha256": sha256(JSON_OUT),
        "markdown": str(MD_OUT),
        "markdown_sha256": sha256(MD_OUT),
        "cohort": report["cohort"],
        "fixed": [
            {
                "horizon_s": row["horizon_s"],
                **row["summary"],
            }
            for row in fixed_policies
        ],
        "state_rules": [
            {
                "id": row["id"],
                "missing_feature_default_n":
                    row["missing_feature_default_n"],
                **row["summary"],
            }
            for row in locked_rules
        ],
        "checks": report["checks"],
    }, indent=2))


if __name__ == "__main__":
    main()
