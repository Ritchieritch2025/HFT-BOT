"""Small deterministic statistics helpers for deep autoresearch."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Iterable, Mapping, Sequence


def finite(values: Iterable[float | int | None]) -> list[float]:
    out = []
    for value in values:
        if value is None:
            continue
        value = float(value)
        if math.isfinite(value):
            out.append(value)
    return out


def percentile(values: Sequence[float], q: float) -> float | None:
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    xs = sorted(finite(values))
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    weight = pos - lo
    return xs[lo] * (1.0 - weight) + xs[hi] * weight


def summary(values: Iterable[float | int | None]) -> dict:
    xs = finite(values)
    if not xs:
        return {"n": 0, "p50": None, "p90": None, "p99": None, "max": None, "mean": None}
    return {
        "n": len(xs),
        "p50": percentile(xs, 0.50),
        "p90": percentile(xs, 0.90),
        "p99": percentile(xs, 0.99),
        "max": max(xs),
        "mean": sum(xs) / len(xs),
    }

def benjamini_hochberg(pvalues: Sequence[float]) -> list[float]:
    """Return monotone BH-adjusted p-values in original order."""
    if any((not math.isfinite(float(p))) or p < 0 or p > 1 for p in pvalues):
        raise ValueError("p-values must be finite in [0, 1]")
    m = len(pvalues)
    if not m:
        return []
    order = sorted(range(m), key=lambda i: (pvalues[i], i))
    adjusted = [1.0] * m
    running = 1.0
    for rank0 in range(m - 1, -1, -1):
        idx = order[rank0]
        rank = rank0 + 1
        running = min(running, float(pvalues[idx]) * m / rank)
        adjusted[idx] = min(1.0, running)
    return adjusted


def aggregate_event_values(
    rows: Iterable[tuple[str, str, float]],
) -> dict[str, dict[str, float]]:
    """Sum observations into date -> root_event -> value."""
    grouped: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for date, root_event, value in rows:
        value = float(value)
        if not date or not root_event or not math.isfinite(value):
            continue
        grouped[str(date)][str(root_event)] += value
    return {day: dict(events) for day, events in grouped.items()}


def block_bootstrap_mean(
    by_day: Mapping[str, Mapping[str, float]],
    *,
    replicates: int = 1000,
    seed: int = 20260715,
) -> dict:
    """Calendar-day block bootstrap of the mean root-event value.

    Selecting a day brings every root event in that day.  A repeated day is a
    repeated block and therefore repeats its events in the bootstrap sample.
    """
    if replicates < 1:
        raise ValueError("replicates must be positive")
    days = sorted(day for day, events in by_day.items() if events)
    observed = [float(v) for day in days for v in by_day[day].values()]
    if not observed:
        return {
            "n_days": 0, "n_events": 0, "mean": None,
            "ci95_lower": None, "ci95_upper": None,
            "replicates": replicates, "seed": seed,
        }
    rng = random.Random(seed)
    draws = []
    for _ in range(replicates):
        sampled = [rng.choice(days) for _ in days]
        values = [float(v) for day in sampled for v in by_day[day].values()]
        draws.append(sum(values) / len(values))
    return {
        "n_days": len(days),
        "n_events": len(observed),
        "mean": sum(observed) / len(observed),
        "ci95_lower": percentile(draws, 0.025),
        "ci95_upper": percentile(draws, 0.975),
        "replicates": replicates,
        "seed": seed,
        "bootstrap_summary": summary(draws),
    }
