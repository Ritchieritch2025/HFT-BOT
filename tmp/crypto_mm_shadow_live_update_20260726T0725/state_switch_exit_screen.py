#!/usr/bin/env python3
"""Post-hoc screen of simple state-dependent post-fill wait policies.

Every state variable is aligned at or before the first fill by
post_fill_state_exploration.py.  A policy chooses one pre-existing fixed
horizon from early_exit_sensitivity.json based on a single frozen threshold.
The grid is discovery-only and cannot be promoted without forward validation.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATE = ROOT / "post_fill_state_exploration.json"
EXITS = ROOT / "early_exit_sensitivity.json"
OUT = ROOT / "state_switch_exit_screen.json"
SEED = 2026072602
BOOT_N = 10_000

SHORT_HORIZONS = (0.25, 0.5, 1.0, 2.0, 5.0)
LONG_HORIZONS = (10.0, 30.0, 60.0)
RULES = {
    "opposite_clear_eta_s": {
        "thresholds": (1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0),
        "long_when": "le",
    },
    "opposite_flow_10s": {
        "thresholds": (100.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0),
        "long_when": "ge",
    },
    "pair_profit_c": {
        "thresholds": (0.5, 1.0, 1.5, 2.0, 3.0, 5.0),
        "long_when": "le",
    },
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    x = (len(ordered) - 1) * p
    lo = math.floor(x)
    hi = math.ceil(x)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - x) + ordered[hi] * (x - lo)


def cluster_ci(rows: list[dict], key: str) -> list[float] | None:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        grouped[str(row["market"])].append(float(row[key]))
    markets = sorted(grouped)
    if len(markets) < 2:
        return None
    rng = random.Random(SEED)
    means = []
    for _ in range(BOOT_N):
        sampled = []
        for _market in markets:
            sampled.extend(grouped[markets[rng.randrange(len(markets))]])
        means.append(sum(sampled) / len(sampled))
    return [
        round(percentile(means, 0.025), 8),
        round(percentile(means, 0.975), 8),
    ]


def main() -> None:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    exits = json.loads(EXITS.read_text(encoding="utf-8"))
    state_rows = {
        (str(row["market"]), int(row["completed_cycle"])): row
        for row in state["rows"]
    }
    exit_maps = {}
    for horizon_text, result in exits["policies"].items():
        exit_maps[float(horizon_text)] = {
            (str(row["market"]), int(row["completed_cycle"])): row
            for row in result["cycles"]
        }

    policies = []
    for feature, rule in RULES.items():
        for threshold in rule["thresholds"]:
            for short_h in SHORT_HORIZONS:
                for long_h in LONG_HORIZONS:
                    rows = []
                    missing = defaultdict(int)
                    for identity, state_row in state_rows.items():
                        value = state_row["features"].get(feature)
                        if value is None:
                            missing["feature"] += 1
                            continue
                        choose_long = (
                            float(value) <= threshold
                            if rule["long_when"] == "le"
                            else float(value) >= threshold
                        )
                        horizon = long_h if choose_long else short_h
                        outcome = exit_maps[horizon].get(identity)
                        if outcome is None:
                            missing[f"exit_{horizon}"] += 1
                            continue
                        rows.append({
                            "market": identity[0],
                            "completed_cycle": identity[1],
                            "chosen_horizon_s": horizon,
                            "touch_pnl_usd": float(outcome["touch_pnl_usd"]),
                            "conservative_pnl_usd": float(
                                outcome["conservative_pnl_usd"]
                            ),
                            "capital_time_s": float(outcome["capital_time_s"]),
                        })
                    for variant in ("touch", "conservative"):
                        pnl_key = f"{variant}_pnl_usd"
                        pnl = [float(row[pnl_key]) for row in rows]
                        waits = [float(row["capital_time_s"]) for row in rows]
                        policies.append({
                            "feature": feature,
                            "long_when": rule["long_when"],
                            "threshold": threshold,
                            "short_horizon_s": short_h,
                            "long_horizon_s": long_h,
                            "variant": variant,
                            "n": len(rows),
                            "markets": len({row["market"] for row in rows}),
                            "missing": dict(sorted(missing.items())),
                            "long_action_n": sum(
                                row["chosen_horizon_s"] == long_h for row in rows
                            ),
                            "total_usd": round(sum(pnl), 8),
                            "mean_usd_per_cycle": round(
                                sum(pnl) / len(pnl), 8
                            ) if pnl else None,
                            "market_cluster_ci95_mean_usd": (
                                cluster_ci(rows, pnl_key) if pnl else None
                            ),
                            "mean_capital_time_s": round(
                                sum(waits) / len(waits), 8
                            ) if waits else None,
                        })

    ranked = sorted(
        policies,
        key=lambda row: (
            -float(row["mean_usd_per_cycle"])
            if row["mean_usd_per_cycle"] is not None else math.inf,
            row["feature"],
            row["threshold"],
            row["short_horizon_s"],
            row["long_horizon_s"],
            row["variant"],
        ),
    )
    report = {
        "schema": "crypto-mm-state-switch-exit-screen-v1",
        "status": "POST_HOC_DISCOVERY_ONLY",
        "not_candidate_selection": True,
        "not_validation": True,
        "inference_warning": (
            "best rows are selected after screening 570 policies; the "
            "per-policy cluster intervals are descriptive and are not "
            "selection- or multiplicity-adjusted. Policy denominators also "
            "vary when a feature or horizon row is unavailable"
        ),
        "external_probe_cooldown_applied": False,
        "source_state_sha256": sha256(STATE),
        "source_exit_sha256": sha256(EXITS),
        "decision_clock": (
            "single state feature from latest same-market QUOTE_EVAL at or "
            "before first fill; no future feature values"
        ),
        "policy_grid": {
            "short_horizons_s": SHORT_HORIZONS,
            "long_horizons_s": LONG_HORIZONS,
            "rules": RULES,
            "variants": ["touch", "conservative_touch_plus_1c"],
        },
        "state_cycle_n": len(state_rows),
        "evaluated_policy_n": len(policies),
        "best_touch": next(
            (row for row in ranked if row["variant"] == "touch"), None
        ),
        "best_conservative": next(
            (row for row in ranked if row["variant"] == "conservative"), None
        ),
        "top_20": ranked[:20],
        "policies": policies,
    }
    OUT.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "out": str(OUT),
        "sha256": sha256(OUT),
        "evaluated_policy_n": len(policies),
        "best_touch": report["best_touch"],
        "best_conservative": report["best_conservative"],
    }, indent=2))


if __name__ == "__main__":
    main()
