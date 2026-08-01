#!/usr/bin/env python3
"""Hindsight envelope across the logged fixed post-fill horizons."""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "early_exit_sensitivity.json"
OUT = ROOT / "horizon_oracle.json"
BOOT_N = 10_000
SEED = 2026072603


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


def cluster_ci(rows: list[dict], key: str) -> list[float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        grouped[str(row["market"])].append(float(row[key]))
    markets = sorted(grouped)
    rng = random.Random(SEED)
    means = []
    for _ in range(BOOT_N):
        values = []
        for _market in markets:
            values.extend(grouped[markets[rng.randrange(len(markets))]])
        means.append(sum(values) / len(values))
    return [
        round(percentile(means, 0.025), 8),
        round(percentile(means, 0.975), 8),
    ]


def summarize(rows: list[dict], key: str, horizon_key: str) -> dict:
    values = [float(row[key]) for row in rows]
    return {
        "n": len(rows),
        "markets": len({row["market"] for row in rows}),
        "total_usd": round(sum(values), 8),
        "mean_usd_per_cycle": round(sum(values) / len(values), 8),
        "market_cluster_ci95_mean_usd": cluster_ci(rows, key),
        "chosen_horizon_counts": dict(sorted(Counter(
            str(row[horizon_key]) for row in rows
        ).items())),
    }


def main() -> None:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    grouped: dict[tuple[str, int], list[dict]] = defaultdict(list)
    horizon_n = len(source["policies"])
    for horizon_text, policy in source["policies"].items():
        for row in policy["cycles"]:
            grouped[(str(row["market"]), int(row["completed_cycle"]))].append({
                **row,
                "horizon_s": float(horizon_text),
            })
    rows = []
    for (market, cycle), options in sorted(grouped.items()):
        touch = max(options, key=lambda row: float(row["touch_pnl_usd"]))
        conservative = max(
            options, key=lambda row: float(row["conservative_pnl_usd"])
        )
        rows.append({
            "market": market,
            "completed_cycle": cycle,
            "available_horizon_n": len(options),
            "touch_pnl_usd": float(touch["touch_pnl_usd"]),
            "touch_horizon_s": float(touch["horizon_s"]),
            "conservative_pnl_usd": float(
                conservative["conservative_pnl_usd"]
            ),
            "conservative_horizon_s": float(conservative["horizon_s"]),
        })
    complete = [row for row in rows if row["available_horizon_n"] == horizon_n]
    report = {
        "schema": "crypto-mm-fixed-horizon-hindsight-oracle-v1",
        "status": "POST_HOC_LOGGED_HORIZON_ENVELOPE_ONLY",
        "not_causal": True,
        "not_deployable": True,
        "source_sha256": sha256(SOURCE),
        "available_horizons_s": sorted(
            float(value) for value in source["policies"]
        ),
        "interpretation": (
            "chooses the best realized horizon separately for every cycle "
            "with future knowledge. This is only an optimistic envelope over "
            "the logged fixed-horizon rows available for that cycle. It is "
            "not an executable bound, not a complete eight-action bound when "
            "rows are missing, and not a bound on dynamic reprice/exit actions"
        ),
        "all_available": {
            "touch": summarize(
                rows, "touch_pnl_usd", "touch_horizon_s"
            ),
            "conservative_touch_plus_1c": summarize(
                rows,
                "conservative_pnl_usd",
                "conservative_horizon_s",
            ),
        },
        "complete_eight_horizons_only": {
            "touch": summarize(
                complete, "touch_pnl_usd", "touch_horizon_s"
            ),
            "conservative_touch_plus_1c": summarize(
                complete,
                "conservative_pnl_usd",
                "conservative_horizon_s",
            ),
        },
        "rows": rows,
    }
    OUT.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "out": str(OUT),
        "sha256": sha256(OUT),
        "all_available": report["all_available"],
        "complete_eight": report["complete_eight_horizons_only"],
    }, indent=2))


if __name__ == "__main__":
    main()
