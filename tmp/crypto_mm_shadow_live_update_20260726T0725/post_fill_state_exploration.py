#!/usr/bin/env python3
"""Post-hoc feature screen at the first fill of each clean shadow cycle.

This is hypothesis generation only.  It aligns each first fill to the latest
same-market QUOTE_EVAL at or before the fill, rejects stale states, and reports
univariate discrimination between natural maker completion and a forced close.
It does not fit or select a deployable policy.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SUMMARY = ROOT / "shadow_24h_diagnostic.json"
SOURCE = ROOT / "source_snapshot"
OUT = ROOT / "post_fill_state_exploration.json"
MAX_QUOTE_AGE_S = 1.5


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def rank_auc(rows: list[tuple[float, int]]) -> float | None:
    """Mann-Whitney AUC with exact average ranks for ties."""
    positives = sum(label for _, label in rows)
    negatives = len(rows) - positives
    if positives == 0 or negatives == 0:
        return None
    ordered = sorted(rows)
    rank_sum_pos = 0.0
    i = 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and ordered[j][0] == ordered[i][0]:
            j += 1
        average_rank = ((i + 1) + j) / 2.0
        rank_sum_pos += average_rank * sum(
            label for _, label in ordered[i:j]
        )
        i = j
    return (
        rank_sum_pos - positives * (positives + 1) / 2.0
    ) / (positives * negatives)


def load_quotes() -> tuple[dict[str, list[int]], dict[str, list[dict]]]:
    by_market: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(SOURCE.glob("mm_*.ndjson")):
        with path.open(encoding="utf-8") as src:
            for line in src:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("ev") == "QUOTE_EVAL" and row.get("mt"):
                    by_market[str(row["mt"])].append(row)
    times: dict[str, list[int]] = {}
    for market, rows in by_market.items():
        rows.sort(key=lambda row: int(row["wall_ns"]))
        times[market] = [int(row["wall_ns"]) for row in rows]
    return times, by_market


def quote_before(
    market: str,
    wall_ns: int,
    times: dict[str, list[int]],
    quotes: dict[str, list[dict]],
) -> tuple[dict | None, str | None]:
    idx = bisect.bisect_right(times.get(market, []), wall_ns) - 1
    if idx < 0:
        return None, "no_prior_quote"
    row = quotes[market][idx]
    age_s = (wall_ns - int(row["wall_ns"])) / 1e9
    if age_s < 0:
        return None, "future_quote"
    if age_s > MAX_QUOTE_AGE_S:
        return None, "stale_quote"
    return row, None


def numeric(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def ratio_log(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or a < 0 or b < 0:
        return None
    return math.log((a + 0.01) / (b + 0.01))


def features(first_side: str, quote: dict) -> dict[str, float | None]:
    if first_side == "bid":
        first_prefix, opposite_prefix = "y", "n"
        first_edge, opposite_edge = "edge_bid", "edge_no"
    elif first_side == "ask_no":
        first_prefix, opposite_prefix = "n", "y"
        first_edge, opposite_edge = "edge_no", "edge_bid"
    else:
        return {}

    first_queue = numeric(quote.get(f"{first_prefix}_queue_ahead"))
    opposite_queue = numeric(quote.get(f"{opposite_prefix}_queue_ahead"))
    first_eta = numeric(quote.get(f"{first_prefix}_clear_eta_s"))
    opposite_eta = numeric(quote.get(f"{opposite_prefix}_clear_eta_s"))
    first_flow_10 = numeric(quote.get(f"{first_prefix}_flow_10s"))
    opposite_flow_10 = numeric(quote.get(f"{opposite_prefix}_flow_10s"))
    first_flow_60 = numeric(quote.get(f"{first_prefix}_flow_60s"))
    opposite_flow_60 = numeric(quote.get(f"{opposite_prefix}_flow_60s"))
    fair = numeric(quote.get("fair_c"))
    mid = numeric(quote.get("mid_c"))
    pair_sum = numeric(quote.get("pair_quote_sum_c"))

    return {
        "tte_s": numeric(quote.get("tte")),
        "spread_c": numeric(quote.get("spread_c")),
        "pair_profit_c": 100.0 - pair_sum if pair_sum is not None else None,
        "abs_fair_minus_mid_c": (
            abs(fair - mid) if fair is not None and mid is not None else None
        ),
        "first_edge_c": numeric(quote.get(first_edge)),
        "opposite_edge_c": numeric(quote.get(opposite_edge)),
        "first_queue_ahead": first_queue,
        "opposite_queue_ahead": opposite_queue,
        "log_opposite_vs_first_queue": ratio_log(
            opposite_queue, first_queue
        ),
        "first_flow_10s": first_flow_10,
        "opposite_flow_10s": opposite_flow_10,
        "log_opposite_vs_first_flow_10s": ratio_log(
            opposite_flow_10, first_flow_10
        ),
        "first_flow_60s": first_flow_60,
        "opposite_flow_60s": opposite_flow_60,
        "log_opposite_vs_first_flow_60s": ratio_log(
            opposite_flow_60, first_flow_60
        ),
        "first_clear_eta_s": first_eta,
        "opposite_clear_eta_s": opposite_eta,
        "log_opposite_vs_first_eta": ratio_log(opposite_eta, first_eta),
        "touch_imbalance": numeric(quote.get("touch_imbalance")),
    }


def main() -> None:
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
    cycles = [
        cycle for cycle in summary["cycle_outcomes"]["cycles"]
        if cycle["class"] in ("natural_maker_pair", "unpaired_age_forced")
        and int(cycle.get("external_fill_n", 0)) == 0
    ]
    times, quotes = load_quotes()
    rows = []
    rejected: dict[str, int] = defaultdict(int)
    for cycle in cycles:
        first = cycle["first_fill"]
        quote, why = quote_before(
            str(cycle["market"]),
            int(first["source"]["wall_ns"]),
            times,
            quotes,
        )
        if quote is None:
            rejected[str(why)] += 1
            continue
        values = features(str(first["side"]), quote)
        if not values:
            rejected["unknown_first_side"] += 1
            continue
        rows.append({
            "market": cycle["market"],
            "completed_cycle": cycle["completed_cycle"],
            "label_natural": int(cycle["class"] == "natural_maker_pair"),
            "first_side": first["side"],
            "quote_age_s": round(
                (
                    int(first["source"]["wall_ns"])
                    - int(quote["wall_ns"])
                ) / 1e9,
                8,
            ),
            "features": values,
        })

    feature_names = sorted({
        name for row in rows for name in row["features"]
    })
    screen = {}
    for name in feature_names:
        observed = [
            (float(row["features"][name]), int(row["label_natural"]))
            for row in rows if row["features"].get(name) is not None
        ]
        natural = [value for value, label in observed if label == 1]
        forced = [value for value, label in observed if label == 0]
        auc = rank_auc(observed)
        direction = None
        best_auc = None
        if auc is not None:
            direction = "higher_means_natural" if auc >= 0.5 else "lower_means_natural"
            best_auc = max(auc, 1.0 - auc)
        screen[name] = {
            "n": len(observed),
            "missing_n": len(rows) - len(observed),
            "natural_n": len(natural),
            "forced_n": len(forced),
            "natural_median": round(median(natural), 8) if natural else None,
            "forced_median": round(median(forced), 8) if forced else None,
            "auc_raw_higher_means_natural": round(auc, 8)
            if auc is not None else None,
            "best_orientation_auc": round(best_auc, 8)
            if best_auc is not None else None,
            "direction": direction,
        }

    ranked = sorted(
        (
            {"feature": name, **stats}
            for name, stats in screen.items()
            if stats["best_orientation_auc"] is not None
        ),
        key=lambda item: (-item["best_orientation_auc"], item["feature"]),
    )
    report = {
        "schema": "crypto-mm-post-fill-state-exploration-v1",
        "status": "POST_HOC_HYPOTHESIS_GENERATION_ONLY",
        "not_candidate_selection": True,
        "not_validation": True,
        "source_summary_sha256": sha256(SUMMARY),
        "source_receipts": {
            path.name: sha256(path)
            for path in sorted(SOURCE.glob("mm_*.ndjson"))
        },
        "alignment": {
            "clock": "QUOTE_EVAL.wall_ns <= first_fill.source.wall_ns",
            "same_market": True,
            "max_quote_age_s": MAX_QUOTE_AGE_S,
            "future_state_allowed": False,
        },
        "cycle_n_source": len(cycles),
        "cycle_n_aligned": len(rows),
        "markets_n": len({row["market"] for row in rows}),
        "natural_n": sum(row["label_natural"] for row in rows),
        "forced_n": sum(1 - row["label_natural"] for row in rows),
        "rejected": dict(sorted(rejected.items())),
        "ranked_univariate_features": ranked,
        "feature_screen": screen,
        "rows": rows,
    }
    OUT.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "out": str(OUT),
        "sha256": sha256(OUT),
        "aligned": len(rows),
        "markets": report["markets_n"],
        "natural": report["natural_n"],
        "forced": report["forced_n"],
        "top": ranked[:8],
    }, indent=2))


if __name__ == "__main__":
    main()
