#!/usr/bin/env python3
"""Post-hoc early-exit sensitivity on the frozen corrected shadow receipts.

This is a diagnostic, not a preregistered candidate-selection experiment.
For each clean resolved first-leg cycle:

* keep the actual maker pair if it completed by a fixed horizon;
* otherwise buy the complementary outcome at the latest causal QUOTE_EVAL
  touch observed by that horizon;
* report both touch execution and the engine's conservative touch+1c IOC.

The receipt stream is roughly 1 Hz, so sub-second policies are only coarse
sensitivities.  No future QUOTE_EVAL is used and a stale/missing/depth-short
state invalidates that episode/horizon rather than being imputed.
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


ROOT = Path(__file__).resolve().parent
BASE = ROOT / "evidence"
SUMMARY = BASE / "shadow_24h_diagnostic.json"
SOURCE = BASE / "source_snapshot"
OUT = ROOT / "early_exit_sensitivity.json"
HORIZONS_S = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
MAX_STATE_AGE_S = 1.5
BOOT_N = 10_000
BOOT_SEED = 20260726


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def taker_fee_usd(price: float, count: float = 1.0) -> float:
    p = Decimal(str(price))
    c = Decimal(str(count))
    position_cost = p * c
    raw = Decimal("0.07") * c * p * (Decimal(1) - p)
    rounded = (position_cost + raw).quantize(
        Decimal("0.0001"), rounding=ROUND_CEILING
    )
    return float(rounded - position_cost)


def percentile(sorted_values: list[float], p: float) -> float | None:
    if not sorted_values:
        return None
    x = (len(sorted_values) - 1) * p
    lo = math.floor(x)
    hi = math.ceil(x)
    if lo == hi:
        return sorted_values[lo]
    return (
        sorted_values[lo] * (hi - x)
        + sorted_values[hi] * (x - lo)
    )


def cluster_ci(rows: list[dict], value_key: str) -> list[float] | None:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        grouped[row["market"]].append(float(row[value_key]))
    markets = sorted(grouped)
    if len(markets) < 2:
        return None
    rng = random.Random(BOOT_SEED)
    boots = []
    for _ in range(BOOT_N):
        values = []
        for _j in markets:
            picked = markets[rng.randrange(len(markets))]
            values.extend(grouped[picked])
        boots.append(sum(values) / len(values))
    boots.sort()
    return [
        round(percentile(boots, 0.025), 8),
        round(percentile(boots, 0.975), 8),
    ]


def load_quote_evals() -> tuple[dict[str, list[int]], dict[str, list[dict]]]:
    by_market: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(SOURCE.glob("mm_*.ndjson")):
        with path.open() as f:
            for line_no, line in enumerate(f, 1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("ev") != "QUOTE_EVAL" or not row.get("mt"):
                    continue
                row["_source_file"] = path.name
                row["_source_line"] = line_no
                by_market[row["mt"]].append(row)
    times: dict[str, list[int]] = {}
    for market, rows in by_market.items():
        rows.sort(key=lambda x: int(x["wall_ns"]))
        times[market] = [int(x["wall_ns"]) for x in rows]
    return times, by_market


def quote_at_or_before(
    market: str,
    decision_ns: int,
    times: dict[str, list[int]],
    rows: dict[str, list[dict]],
) -> tuple[dict | None, str | None]:
    arr = times.get(market, [])
    idx = bisect.bisect_right(arr, decision_ns) - 1
    if idx < 0:
        return None, "no_quote_eval"
    q = rows[market][idx]
    age_s = (decision_ns - int(q["wall_ns"])) / 1e9
    if age_s < 0:
        return None, "future_quote_eval"
    if age_s > MAX_STATE_AGE_S:
        return None, "stale_quote_eval"
    return q, None


def complementary_touch(first_side: str, q: dict) -> tuple[float, float] | None:
    if first_side == "bid":
        yb = q.get("yb")
        depth = q.get("y_touch_ct")
        if yb is None or depth is None:
            return None
        return 1.0 - float(yb) / 10_000.0, float(depth)
    if first_side == "ask_no":
        ya = q.get("ya")
        depth = q.get("n_touch_ct")
        if ya is None or depth is None:
            return None
        return float(ya) / 10_000.0, float(depth)
    return None


def summarize_policy(rows: list[dict], value_key: str) -> dict:
    values = [float(r[value_key]) for r in rows]
    waits = [float(r["capital_time_s"]) for r in rows]
    values_sorted = sorted(values)
    return {
        "n": len(rows),
        "markets": len({r["market"] for r in rows}),
        "maker_completed_by_horizon": sum(
            r["action"] == "maker_completed" for r in rows
        ),
        "completion_rate": round(
            sum(r["action"] == "maker_completed" for r in rows) / len(rows), 8
        ) if rows else None,
        "total_usd": round(sum(values), 8),
        "mean_usd_per_cycle": round(sum(values) / len(values), 8)
        if values else None,
        "p05_usd": round(percentile(values_sorted, 0.05), 8)
        if values else None,
        "p50_usd": round(percentile(values_sorted, 0.50), 8)
        if values else None,
        "p95_usd": round(percentile(values_sorted, 0.95), 8)
        if values else None,
        "market_cluster_ci95_mean_usd": cluster_ci(rows, value_key),
        "mean_capital_time_s": round(sum(waits) / len(waits), 8)
        if waits else None,
    }


def main() -> None:
    source_summary = json.loads(SUMMARY.read_text())
    cycles = source_summary["cycle_outcomes"]["cycles"]
    clean = [
        c for c in cycles
        if c["class"] in ("natural_maker_pair", "unpaired_age_forced")
        and c.get("external_fill_n", 0) == 0
    ]
    times, quotes = load_quote_evals()
    result = {
        "schema": "crypto-mm-shadow-early-exit-sensitivity-v1",
        "status": "POST_HOC_DIAGNOSTIC_ONLY",
        "not_candidate_selection": True,
        "not_validation": True,
        "source_summary_sha256": sha256(SUMMARY),
        "source_receipts": {
            p.name: sha256(p) for p in sorted(SOURCE.glob("mm_*.ndjson"))
        },
        "assumptions": {
            "policy": (
                "actual maker completion if pair_wait<=horizon, else "
                "cross complementary touch using latest causal QUOTE_EVAL"
            ),
            "quote_eval_max_age_s": MAX_STATE_AGE_S,
            "touch_depth_required_contracts": 1.0,
            "touch_variant": "touch price plus exact general taker fee",
            "conservative_variant": (
                "min(0.99, touch+0.01) plus exact general taker fee"
            ),
            "subsecond_limitation": (
                "QUOTE_EVAL is approximately 1Hz; .25/.5s are coarse "
                "sensitivities, not implementable latency estimates"
            ),
            "lookahead": "latest QUOTE_EVAL.wall_ns <= decision wall_ns",
        },
        "clean_cycle_n": len(clean),
        "actual_baseline": {
            "n": len(clean),
            "total_usd": round(sum(float(c["pnl_usd"]) for c in clean), 8),
            "mean_usd_per_cycle": round(
                sum(float(c["pnl_usd"]) for c in clean) / len(clean), 8
            ),
        },
        "policies": {},
    }
    for horizon in HORIZONS_S:
        accepted = []
        rejected = defaultdict(int)
        for cycle in clean:
            wait_s = float(cycle["pair_wait_s"])
            first = cycle["first_fill"]
            if (
                cycle["class"] == "natural_maker_pair"
                and wait_s <= horizon + 1e-9
            ):
                pnl = float(cycle["pnl_usd"])
                accepted.append({
                    "market": cycle["market"],
                    "completed_cycle": cycle["completed_cycle"],
                    "action": "maker_completed",
                    "capital_time_s": wait_s,
                    "touch_pnl_usd": pnl,
                    "conservative_pnl_usd": pnl,
                })
                continue
            decision_ns = int(first["source"]["wall_ns"] + horizon * 1e9)
            q, why = quote_at_or_before(
                cycle["market"], decision_ns, times, quotes
            )
            if q is None:
                rejected[why] += 1
                continue
            touch = complementary_touch(str(first["side"]), q)
            if touch is None:
                rejected["unparseable_touch"] += 1
                continue
            touch_px, depth = touch
            if not 0 < touch_px < 1:
                rejected["invalid_touch_price"] += 1
                continue
            if depth < 1.0:
                rejected["touch_depth_lt_1"] += 1
                continue
            entry_px = float(first["price_dollars"])
            touch_fee = taker_fee_usd(touch_px)
            touch_pnl = 1.0 - entry_px - touch_px - touch_fee
            conservative_px = min(0.99, touch_px + 0.01)
            conservative_fee = taker_fee_usd(conservative_px)
            conservative_pnl = (
                1.0 - entry_px - conservative_px - conservative_fee
            )
            accepted.append({
                "market": cycle["market"],
                "completed_cycle": cycle["completed_cycle"],
                "action": "cross_at_horizon",
                "first_side": first["side"],
                "entry_px": entry_px,
                "decision_wall_ns": decision_ns,
                "quote_wall_ns": int(q["wall_ns"]),
                "quote_state_age_s": round(
                    (decision_ns - int(q["wall_ns"])) / 1e9, 9
                ),
                "quote_source": (
                    f"{q['_source_file']}:{q['_source_line']}"
                ),
                "touch_px": round(touch_px, 8),
                "touch_depth_ct": round(depth, 8),
                "touch_fee_usd": round(touch_fee, 8),
                "conservative_px": round(conservative_px, 8),
                "conservative_fee_usd": round(conservative_fee, 8),
                "capital_time_s": horizon,
                "touch_pnl_usd": round(touch_pnl, 8),
                "conservative_pnl_usd": round(conservative_pnl, 8),
            })
        result["policies"][str(horizon)] = {
            "horizon_s": horizon,
            "accepted_n": len(accepted),
            "rejected_n": sum(rejected.values()),
            "rejected_reasons": dict(sorted(rejected.items())),
            "touch": summarize_policy(accepted, "touch_pnl_usd"),
            "conservative": summarize_policy(
                accepted, "conservative_pnl_usd"
            ),
            "cycles": accepted,
        }
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "out": str(OUT),
        "sha256": sha256(OUT),
        "actual_baseline": result["actual_baseline"],
        "policies": {
            k: {
                "accepted_n": v["accepted_n"],
                "rejected_n": v["rejected_n"],
                "touch_mean": v["touch"]["mean_usd_per_cycle"],
                "touch_ci": v["touch"]["market_cluster_ci95_mean_usd"],
                "conservative_mean":
                    v["conservative"]["mean_usd_per_cycle"],
                "conservative_ci":
                    v["conservative"]["market_cluster_ci95_mean_usd"],
                "maker_completion_rate": v["touch"]["completion_rate"],
            }
            for k, v in result["policies"].items()
        },
    }, indent=2))


if __name__ == "__main__":
    main()
