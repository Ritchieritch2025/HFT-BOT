#!/usr/bin/env python3
"""Calibrate BRTI settlement probabilities from captured CF frames.

This is an offline research tool.  It never imports the live engine and has
no order-routing code.  Its primary purpose is to compare short, restart-like
volatility windows with fully warmed multi-scale estimates on the exact
quarter-hour settlement averages supplied by Kalshi's CF Benchmarks stream.
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import rti_pricing as rp


QUARTER_MS = 15 * 60 * 1000


@dataclass(frozen=True)
class Tick:
    ts_ms: int
    value: float
    avg60: Optional[float] = None
    avg60_n: int = 0
    lock_avg: Optional[float] = None
    lock_n: int = 0


def parse_tick_line(line: str, index_id: str = "BRTI") -> Optional[Tick]:
    """Parse one capture envelope into a settlement-index tick."""
    try:
        envelope = json.loads(line)
        frame = json.loads(envelope["payload"])
        if frame.get("type") != "cfbenchmarks_value":
            return None
        msg = frame["msg"]
        if msg.get("index_id") != index_id:
            return None
        raw = json.loads(msg["data"])
        ts_ms = int(raw["time"])
        value = float(raw["value"])
        avg60 = msg.get("avg_60s_data") or {}
        avg60_value = (
            float(avg60["value"])
            if avg60.get("value") is not None else None
        )
        avg60_n = int(avg60.get("window_size") or 0)
        locked = msg.get("last_60s_windowed_average_15min") or {}
        lock_avg = (float(locked["value"])
                    if locked.get("value") is not None else None)
        lock_n = int(locked.get("window_size") or 0)
        return Tick(ts_ms=ts_ms, value=value,
                    avg60=avg60_value, avg60_n=avg60_n,
                    lock_avg=lock_avg, lock_n=lock_n)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def load_ticks(paths: Iterable[str], index_id: str = "BRTI") -> list[Tick]:
    """Load, source-time sort, and deduplicate captured ticks."""
    by_ts: dict[int, Tick] = {}
    for path in paths:
        with open(path, "r", encoding="utf-8") as src:
            for line in src:
                tick = parse_tick_line(line, index_id=index_id)
                if tick is not None:
                    by_ts[tick.ts_ms] = tick
    return [by_ts[k] for k in sorted(by_ts)]


def sigma_per_s(
    ticks: list[Tick],
    end_idx: int,
    window_s: int,
    *,
    demean: bool = False,
    min_coverage: float = 0.95,
    max_gap_s: float = 1.5,
) -> Optional[float]:
    """Timestamp-aware diffusion sigma over a strict trailing window.

    ``demean=False`` matches a zero-drift forecasting model: persistent moves
    are not silently removed from the risk estimate.  A discontinuous source
    window fails closed instead of treating a multi-second move as one second.
    """
    if end_idx <= 0 or window_s <= 0:
        return None
    end_ms = ticks[end_idx].ts_ms
    start_ms = end_ms - window_s * 1000
    times = [t.ts_ms for t in ticks]
    start_idx = bisect.bisect_left(times, start_ms, 0, end_idx + 1)
    sample = ticks[start_idx:end_idx + 1]
    if len(sample) < 3:
        return None
    elapsed_s = (sample[-1].ts_ms - sample[0].ts_ms) / 1000.0
    if elapsed_s < window_s * min_coverage:
        return None
    increments: list[tuple[float, float]] = []
    for prev, cur in zip(sample, sample[1:]):
        dt_s = (cur.ts_ms - prev.ts_ms) / 1000.0
        if dt_s <= 0 or dt_s > max_gap_s:
            return None
        increments.append((cur.value - prev.value, dt_s))
    total_t = sum(dt for _, dt in increments)
    if total_t <= 0:
        return None
    drift = (sum(dx for dx, _ in increments) / total_t) if demean else 0.0
    variance_rate = sum(
        (dx - drift * dt) ** 2 for dx, dt in increments
    ) / total_t
    return math.sqrt(variance_rate) if variance_rate > 0 else None


def settlement_closes(ticks: list[Tick]) -> dict[int, float]:
    """Official [close-60s, close) quarter-hour settlement averages.

    ``last_60s_windowed_average_15min`` is shifted by one tick on the wire:
    it excludes close-60s and includes the close tick.  Only the complete
    ``avg_60s_data`` frame at the quarter boundary matches Kalshi's
    floor_strike/expiration_value contract.
    """
    return {
        t.ts_ms: float(t.avg60)
        for t in ticks
        if (t.ts_ms % QUARTER_MS == 0
            and t.avg60_n >= rp.LOCK_TICKS
            and t.avg60 is not None)
    }


def _metrics(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    eps = 1e-12
    brier = sum((r["p"] - r["outcome"]) ** 2 for r in rows) / len(rows)
    logloss = -sum(
        r["outcome"] * math.log(max(eps, min(1 - eps, r["p"])))
        + (1 - r["outcome"])
        * math.log(max(eps, min(1 - eps, 1 - r["p"])))
        for r in rows
    ) / len(rows)
    zs = [r["z_realized"] for r in rows]
    return {
        "n": len(rows),
        "brier": round(brier, 8),
        "log_loss": round(logloss, 8),
        "z_mean": round(sum(zs) / len(zs), 6),
        "z_rms": round(math.sqrt(sum(z * z for z in zs) / len(zs)), 6),
        "coverage_abs_z_le_1": round(
            sum(abs(z) <= 1 for z in zs) / len(zs), 6),
        "coverage_abs_z_le_1p96": round(
            sum(abs(z) <= 1.96 for z in zs) / len(zs), 6),
        "extreme_probability_rate": round(
            sum(r["p"] <= 0.1 or r["p"] >= 0.9 for r in rows)
            / len(rows), 6),
    }


def run_calibration(
    ticks: list[Tick],
    horizons_s: Iterable[int] = (120, 300, 600, 840),
    windows_s: Iterable[int] = (30, 60, 300, 900),
) -> dict:
    """Compare raw and conservative multi-scale volatility estimators."""
    if not ticks:
        raise ValueError("no BRTI ticks")
    times = [t.ts_ms for t in ticks]
    closes = settlement_closes(ticks)
    rows_by_key: dict[tuple[int, str], list[dict]] = {}
    detail: list[dict] = []

    for close_ms, final_avg in sorted(closes.items()):
        strike = closes.get(close_ms - QUARTER_MS)
        if strike is None:
            continue
        outcome = 1 if final_avg >= strike else 0
        for horizon_s in horizons_s:
            target_ms = close_ms - int(horizon_s) * 1000
            idx = bisect.bisect_right(times, target_ms) - 1
            if idx < 0:
                continue
            spot = ticks[idx].value
            sigmas = {
                f"sigma_{window_s}s": sigma_per_s(
                    ticks, idx, int(window_s), demean=False)
                for window_s in windows_s
            }
            available = [v for v in sigmas.values() if v is not None]
            if all(sigmas.get(k) is not None
                   for k in ("sigma_60s", "sigma_300s")):
                sigmas["max_60_300"] = max(
                    sigmas["sigma_60s"], sigmas["sigma_300s"])
            if all(sigmas.get(k) is not None
                   for k in ("sigma_60s", "sigma_300s", "sigma_900s")):
                sigmas["max_60_300_900"] = max(
                    sigmas["sigma_60s"],
                    sigmas["sigma_300s"],
                    sigmas["sigma_900s"],
                )
            if available:
                sigmas["max_available"] = max(available)

            variance_factor = (
                max(float(horizon_s) - rp.LOCK_TICKS, 0.0)
                + rp.rw_avg_var_factor(rp.LOCK_TICKS - 1)
                / (rp.LOCK_TICKS ** 2)
            )
            row_base = {
                "close_ms": close_ms,
                "horizon_s": int(horizon_s),
                "spot": spot,
                "strike": strike,
                "final_avg": final_avg,
                "outcome": outcome,
            }
            one_detail = dict(row_base)
            one_detail["estimators"] = {}
            for name, sigma in sigmas.items():
                if sigma is None or sigma <= 0:
                    continue
                p = rp.p_settle_above(
                    spot, strike, sigma, float(horizon_s))
                if p is None:
                    continue
                sd = sigma * math.sqrt(variance_factor)
                row = dict(row_base)
                row.update({
                    "estimator": name,
                    "sigma": sigma,
                    "p": p,
                    "z_realized": (final_avg - spot) / sd,
                })
                rows_by_key.setdefault((int(horizon_s), name), []).append(row)
                one_detail["estimators"][name] = {
                    "sigma": round(sigma, 8),
                    "p": round(p, 8),
                    "z_realized": round(row["z_realized"], 8),
                }
            detail.append(one_detail)

    summary: dict[str, dict] = {}
    for (horizon_s, name), rows in sorted(rows_by_key.items()):
        summary.setdefault(str(horizon_s), {})[name] = _metrics(rows)
    return {
        "schema_version": "rti-vol-calibration-v1",
        "tick_count": len(ticks),
        "tick_start_ms": ticks[0].ts_ms,
        "tick_end_ms": ticks[-1].ts_ms,
        "settlement_close_count": len(closes),
        "forecast_market_count": max(0, len(closes) - 1),
        "summary_by_horizon_s": summary,
        "detail": detail,
    }


def _expand(patterns: Iterable[str]) -> list[str]:
    paths: set[str] = set()
    for pattern in patterns:
        paths.update(glob.glob(pattern, recursive=True))
    return sorted(paths)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--glob", action="append", dest="patterns",
                        required=True, help="capture glob; repeatable")
    parser.add_argument("--index-id", default="BRTI")
    parser.add_argument("--horizons", default="120,300,600,840")
    parser.add_argument("--windows", default="30,60,300,900")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    paths = _expand(args.patterns)
    if not paths:
        raise SystemExit("no files matched")
    horizons = [int(x) for x in args.horizons.split(",") if x]
    windows = [int(x) for x in args.windows.split(",") if x]
    ticks = load_ticks(paths, index_id=args.index_id)
    report = run_calibration(ticks, horizons_s=horizons, windows_s=windows)
    report["source_files"] = paths
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(json.dumps({
        k: report[k] for k in (
            "tick_count", "settlement_close_count",
            "forecast_market_count", "summary_by_horizon_s")
    }, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
