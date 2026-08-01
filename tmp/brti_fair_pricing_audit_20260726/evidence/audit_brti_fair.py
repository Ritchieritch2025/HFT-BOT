#!/usr/bin/env python3
"""Sealed, read-only BRTI fair-pricing audit.

The program opens only the three pre-registered discovery dates.  It has no
exchange client, order-routing import, service-control call, or engine import.
When the authoritative historical BRTI capture is absent it fails
informatively instead of substituting another price series.
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
import datetime as dt
import glob
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
from typing import Any, Iterable


ALLOWED_DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
BANNED_DATE = "2026-07-23"
SERIES = "KXBTC15M"
HORIZONS_S = (120, 180, 240)
LEADS_S = (1, 2, 5, 10)
BOOT_REPS = 2000
CAL_SEED = 26072601
LEAD_SEED = 26072602
EPS = 1e-6
NS_PER_S = 1_000_000_000
MS_PER_S = 1_000
US_PER_S = 1_000_000
LOCK_TICKS = 60

CF_TEMPLATE = (
    "/home/ubuntu/hft-bot/work/live/cfbenchmarks/"
    "date={date}/cfb_*.ndjson"
)
BOOK_TEMPLATE = (
    "/home/ubuntu/hft-bot/work/warehouse/facts/orderbooks_full/"
    "category=Crypto/subcategory=BTC/date={date}/*.parquet"
)
META_PATH = Path(
    "/home/ubuntu/h6b_inputs/catalog_normal/"
    "snapshot=20260724T213017Z/shards/KXBTC15M.json"
)
P3_PATH = Path("/tmp/z3_price_allocation_train_raw.json")
P3_ARM = "P3_SPEND1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def iso_ms(ts_ms: int | None) -> str | None:
    if ts_ms is None:
        return None
    return (
        dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def utc_date_ms(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(
        ts_ms / 1000, tz=dt.timezone.utc
    ).date().isoformat()


def assert_safe_path(path: str | Path) -> None:
    text = str(path)
    if BANNED_DATE in text or f"date={BANNED_DATE}" in text:
        raise RuntimeError(f"hard-banned path rejected: {text}")


def verify_seal(script: Path, prereg: Path, seal: Path) -> dict[str, Any]:
    receipt = json.loads(seal.read_text(encoding="utf-8"))
    observed = {
        "script_sha256": sha256(script),
        "preregistration_sha256": sha256(prereg),
    }
    for key, value in observed.items():
        if receipt.get(key) != value:
            raise RuntimeError(
                f"seal mismatch for {key}: {value} != {receipt.get(key)}"
            )
    spec = json.loads(prereg.read_text(encoding="utf-8"))
    if spec["universe"]["discovery_dates_utc"] != list(ALLOWED_DATES):
        raise RuntimeError("pre-registration allowed-date mismatch")
    if spec["universe"]["hard_banned_date_utc"] != BANNED_DATE:
        raise RuntimeError("pre-registration banned-date mismatch")
    return {
        **observed,
        "seal_sha256": sha256(seal),
        "verified": True,
    }


def file_manifest(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    rows = []
    for raw in sorted(str(path) for path in paths):
        assert_safe_path(raw)
        path = Path(raw)
        rows.append({
            "path": raw,
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        })
    return rows


def exact_allowed_globs(template: str) -> dict[str, list[str]]:
    result = {}
    for date in ALLOWED_DATES:
        pattern = template.format(date=date)
        assert_safe_path(pattern)
        result[date] = sorted(glob.glob(pattern))
        for path in result[date]:
            assert_safe_path(path)
    return result


def later_cf_inventory() -> dict[str, Any]:
    """Check fixed non-banned dates; never enumerate date=*."""
    checks = {}
    for date in ("2026-07-24", "2026-07-25", "2026-07-26"):
        pattern = CF_TEMPLATE.format(date=date)
        assert_safe_path(pattern)
        paths = sorted(glob.glob(pattern))
        checks[date] = {
            "file_n": len(paths),
            "first_path": paths[0] if paths else None,
        }
    known = [date for date, row in checks.items() if row["file_n"]]
    return {
        "fixed_non_banned_dates_checked": checks,
        "earliest_known_later_capture_date": min(known) if known else None,
        "note": "No wildcard date-directory enumeration was performed.",
    }


def parse_cf(paths: list[str]) -> tuple[list[int], list[float], dict[str, Any]]:
    by_ts: dict[int, float] = {}
    counters = Counter()
    for path in paths:
        assert_safe_path(path)
        with open(path, "r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                counters["lines"] += 1
                try:
                    envelope = json.loads(line)
                    payload = envelope["payload"]
                    frame = json.loads(payload) if isinstance(
                        payload, str
                    ) else payload
                    if frame.get("type") != "cfbenchmarks_value":
                        counters["non_cfbenchmarks_value"] += 1
                        continue
                    msg = frame["msg"]
                    if msg.get("index_id") != "BRTI":
                        counters["non_brti"] += 1
                        continue
                    raw = msg["data"]
                    data = json.loads(raw) if isinstance(raw, str) else raw
                    ts_ms = int(data["time"])
                    value = float(data["value"])
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                ):
                    counters["malformed"] += 1
                    continue
                source_date = utc_date_ms(ts_ms)
                if source_date not in ALLOWED_DATES:
                    raise RuntimeError(
                        f"out-of-window BRTI tick in allowed file: "
                        f"{path}:{line_no} date={source_date}"
                    )
                by_ts[ts_ms] = value
                counters["accepted"] += 1
    times = sorted(by_ts)
    values = [by_ts[value] for value in times]
    return times, values, dict(sorted(counters.items()))


def rw_avg_var_factor(m: int) -> float:
    return m * (m + 1) * (2 * m + 1) / 6.0


def sigma_window(
    times: list[int],
    values: list[float],
    end_index: int,
    window_s: float,
) -> float | None:
    end_ms = times[end_index]
    start_ms = end_ms - round(window_s * 1000)
    start = bisect.bisect_left(times, start_ms, 0, end_index + 1)
    sample_times = times[start:end_index + 1]
    sample_values = values[start:end_index + 1]
    if len(sample_times) < 31:
        return None
    elapsed = (sample_times[-1] - sample_times[0]) / 1000.0
    if elapsed < window_s * 0.999:
        return None
    variance_numerator = 0.0
    total_t = 0.0
    for t0, t1, v0, v1 in zip(
        sample_times, sample_times[1:], sample_values, sample_values[1:]
    ):
        delta_s = (t1 - t0) / 1000.0
        if delta_s <= 0 or delta_s > 1.5:
            return None
        variance_numerator += (v1 - v0) ** 2
        total_t += delta_s
    if total_t <= 0 or variance_numerator <= 0:
        return None
    return math.sqrt(variance_numerator / total_t)


def kernel_at(
    times: list[int],
    values: list[float],
    target_ms: int,
    strike: float,
    close_ms: int,
) -> dict[str, float] | None:
    index = bisect.bisect_right(times, target_ms) - 1
    if index < 0:
        return None
    age_s = (target_ms - times[index]) / 1000.0
    if age_s < 0 or age_s > 2.0:
        return None
    sigma60 = sigma_window(times, values, index, 60.0)
    sigma300 = sigma_window(times, values, index, 300.0)
    if sigma60 is None or sigma300 is None:
        return None
    sigma = max(sigma60, sigma300)
    tte_s = max(0.0, (close_ms - times[index]) / 1000.0)
    factor = (
        max(tte_s - LOCK_TICKS, 0.0)
        + rw_avg_var_factor(LOCK_TICKS - 1) / (LOCK_TICKS ** 2)
    )
    sd = sigma * math.sqrt(factor)
    if sd <= 0:
        fair = 1.0 if values[index] >= strike else 0.0
        z = math.inf if values[index] >= strike else -math.inf
    else:
        z = (values[index] - strike) / sd
        fair = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return {
        "source_ms": times[index],
        "source_age_s": age_s,
        "brti": values[index],
        "sigma60": sigma60,
        "sigma300": sigma300,
        "sigma": sigma,
        "tte_s": tte_s,
        "standardized_distance": z,
        "fair": fair,
    }


def parse_utc_ms(value: Any) -> int | None:
    try:
        parsed = dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        return round(parsed.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def first_number(row: dict[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        value = row.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None


def load_metadata() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    assert_safe_path(META_PATH)
    source = json.loads(META_PATH.read_text(encoding="utf-8"))
    raw_markets = source.get("markets", source)
    selected: dict[str, dict[str, Any]] = {}
    counters = Counter()
    for ticker, market in raw_markets.items():
        counters["catalog_market_n"] += 1
        if not str(ticker).startswith(f"{SERIES}-"):
            continue
        close_ms = parse_utc_ms(
            market.get("close_time")
            or market.get("expected_expiration_time")
            or market.get("expiration_time")
        )
        if close_ms is None:
            counters["missing_close"] += 1
            continue
        close_date = utc_date_ms(close_ms)
        if close_date not in ALLOWED_DATES:
            counters["outside_allowed_close_date"] += 1
            continue
        if close_date == BANNED_DATE:
            raise RuntimeError("banned market selected")
        result_raw = str(market.get("result") or "").lower()
        if result_raw not in ("yes", "no"):
            counters["unsettled"] += 1
            continue
        strike = first_number(
            market,
            (
                "floor_strike",
                "strike",
                "strike_value",
                "functional_strike",
            ),
        )
        if strike is None:
            counters["missing_strike"] += 1
            continue
        selected[str(ticker)] = {
            "ticker": str(ticker),
            "close_ms": close_ms,
            "close_date": close_date,
            "strike": strike,
            "outcome": 1 if result_raw == "yes" else 0,
        }
        counters["selected_settled"] += 1
    return selected, {
        "manifest": file_manifest([META_PATH])[0],
        "counts": dict(sorted(counters.items())),
    }


def best_positive(book: dict[int, int]) -> int | None:
    levels = [price for price, qty in book.items() if qty > 0]
    return max(levels) if levels else None


def load_books(
    paths_by_date: dict[str, list[str]],
    selected_tickers: set[str],
) -> tuple[
    dict[str, list[tuple[int, float, int, int]]],
    dict[str, Any],
]:
    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError("duckdb unavailable for book replay") from exc
    timelines: dict[str, list[tuple[int, float, int, int]]] = defaultdict(
        list
    )
    invalid: dict[str, list[str]] = defaultdict(list)
    counters = Counter()
    for date in ALLOWED_DATES:
        paths = paths_by_date[date]
        if not paths:
            continue
        for path in paths:
            assert_safe_path(path)
        con = duckdb.connect()
        con.execute("SET threads=2")
        con.execute("SET memory_limit='8GB'")
        cursor = con.execute(
            """
            SELECT market_ticker, ts_utc, msg_type, side, price_e4,
                   delta_e4, CAST(yes_levels AS VARCHAR),
                   CAST(no_levels AS VARCHAR), ws_seq
            FROM read_parquet(?, union_by_name=true)
            WHERE market_ticker LIKE 'KXBTC15M-%'
            ORDER BY market_ticker, ts_utc, ws_seq
            """,
            [paths],
        )
        current_ticker = None
        books = {"y": {}, "n": {}}
        initialized = False
        last_seq = None
        ticker_invalid = False
        while True:
            batch = cursor.fetchmany(200_000)
            if not batch:
                break
            for ticker, ts, kind, side, price, delta, yl, nl, seq in batch:
                ticker = str(ticker)
                if ticker not in selected_tickers:
                    continue
                if ticker != current_ticker:
                    current_ticker = ticker
                    books = {"y": {}, "n": {}}
                    initialized = False
                    last_seq = None
                    ticker_invalid = False
                counters["rows"] += 1
                if seq is None:
                    invalid[ticker].append(f"{date}:missing_ws_seq")
                    ticker_invalid = True
                elif last_seq is not None and int(seq) < last_seq:
                    invalid[ticker].append(
                        f"{date}:ws_seq_regression:{last_seq}->{int(seq)}"
                    )
                    ticker_invalid = True
                else:
                    last_seq = int(seq)
                if kind == "snapshot":
                    try:
                        yes_levels = json.loads(yl) if yl else []
                        no_levels = json.loads(nl) if nl else []
                        new_books = {"y": {}, "n": {}}
                        for levels, key in (
                            (yes_levels, "y"),
                            (no_levels, "n"),
                        ):
                            for raw_price, raw_qty in levels:
                                p = int(raw_price)
                                q = int(raw_qty or 0)
                                if q < 0:
                                    raise ValueError(
                                        f"negative_snapshot_qty:{key}:{p}:{q}"
                                    )
                                new_books[key][p] = q
                        books = new_books
                        initialized = True
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        invalid[ticker].append(f"{date}:{exc}")
                        ticker_invalid = True
                else:
                    if not initialized:
                        invalid[ticker].append(
                            f"{date}:delta_before_snapshot"
                        )
                        ticker_invalid = True
                    side_key = {"yes": "y", "no": "n"}.get(side)
                    if side_key is None or price is None or delta is None:
                        invalid[ticker].append(
                            f"{date}:unknown_delta_semantics"
                        )
                        ticker_invalid = True
                    elif initialized:
                        p = int(price)
                        q = books[side_key].get(p, 0) + int(delta)
                        if q < 0:
                            invalid[ticker].append(
                                f"{date}:negative_delta_qty:"
                                f"{side_key}:{p}:{q}"
                            )
                            ticker_invalid = True
                        else:
                            books[side_key][p] = q
                if ticker_invalid or not initialized:
                    continue
                yb = best_positive(books["y"])
                nb = best_positive(books["n"])
                if yb is None or nb is None:
                    continue
                mid = (yb + (10_000 - nb)) / 20_000.0
                timelines[ticker].append((int(ts), mid, yb, nb))
                counters["valid_mid_states"] += 1
        con.close()
    for ticker in invalid:
        timelines.pop(ticker, None)
    return dict(timelines), {
        "counts": dict(sorted(counters.items())),
        "invalid_market_n": len(invalid),
        "invalid_markets": {
            ticker: sorted(set(reasons))[:20]
            for ticker, reasons in sorted(invalid.items())
        },
        "rule": (
            "No clamp: any negative quantity/sequence regression/missing "
            "snapshot/unknown delta semantics invalidates the market-day."
        ),
    }


def book_at(
    timeline: list[tuple[int, float, int, int]],
    target_us: int,
) -> dict[str, Any] | None:
    if not timeline:
        return None
    times = [row[0] for row in timeline]
    index = bisect.bisect_right(times, target_us) - 1
    if index < 0:
        return None
    ts, mid, yb, nb = timeline[index]
    return {"ts_us": ts, "mid": mid, "yes_bid_e4": yb, "no_bid_e4": nb}


def clip_p(value: float) -> float:
    return max(EPS, min(1.0 - EPS, float(value)))


def logistic_calibration(
    probabilities: list[float], outcomes: list[int]
) -> dict[str, Any]:
    if len(probabilities) < 3 or len(set(outcomes)) < 2:
        return {"available": False, "reason": "n<3 or single outcome"}
    x = [math.log(clip_p(p) / (1.0 - clip_p(p))) for p in probabilities]
    a = 0.0
    b = 1.0
    for _ in range(100):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for xi, yi in zip(x, outcomes):
            eta = max(-35.0, min(35.0, a + b * xi))
            mu = 1.0 / (1.0 + math.exp(-eta))
            weight = max(mu * (1.0 - mu), 1e-12)
            residual = yi - mu
            g0 += residual
            g1 += residual * xi
            h00 += weight
            h01 += weight * xi
            h11 += weight * xi * xi
        determinant = h00 * h11 - h01 * h01
        if determinant <= 1e-12:
            return {"available": False, "reason": "singular/separated"}
        da = (g0 * h11 - g1 * h01) / determinant
        db = (g1 * h00 - g0 * h01) / determinant
        a += da
        b += db
        if max(abs(da), abs(db)) < 1e-10:
            return {
                "available": True,
                "intercept": a,
                "slope": b,
            }
    return {
        "available": False,
        "reason": "IRLS did not converge",
        "last_intercept": a,
        "last_slope": b,
    }


def forecast_metrics(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    probabilities = [clip_p(row[key]) for row in rows]
    outcomes = [int(row["outcome"]) for row in rows]
    return {
        "n": len(rows),
        "markets": len({row["market"] for row in rows}),
        "outcome_rate": statistics.fmean(outcomes),
        "brier": statistics.fmean(
            (p - y) ** 2 for p, y in zip(probabilities, outcomes)
        ),
        "logloss": statistics.fmean(
            -(y * math.log(p) + (1 - y) * math.log(1 - p))
            for p, y in zip(probabilities, outcomes)
        ),
        "calibration": logistic_calibration(probabilities, outcomes),
    }


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def cluster_resamples(
    rows: list[dict[str, Any]], seed: int
) -> Iterable[list[dict[str, Any]]]:
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_market[row["market"]].append(row)
    markets = sorted(by_market)
    rng = random.Random(seed)
    for _ in range(BOOT_REPS):
        sample = []
        for _market in markets:
            sample.extend(by_market[rng.choice(markets)])
        yield sample


def paired_metric_differences(rows: list[dict[str, Any]]) -> dict[str, Any]:
    paired = [row for row in rows if row.get("market_p") is not None]
    if not paired:
        return {"n": 0}

    def one(sample: list[dict[str, Any]]) -> tuple[float, float]:
        kernel = forecast_metrics(sample, "kernel_p")
        market = forecast_metrics(sample, "market_p")
        return (
            kernel["brier"] - market["brier"],
            kernel["logloss"] - market["logloss"],
        )

    point = one(paired)
    boots = [one(sample) for sample in cluster_resamples(paired, CAL_SEED)]
    return {
        "n": len(paired),
        "markets": len({row["market"] for row in paired}),
        "kernel_minus_market_brier": point[0],
        "kernel_minus_market_brier_ci95": [
            quantile([row[0] for row in boots], 0.025),
            quantile([row[0] for row in boots], 0.975),
        ],
        "kernel_minus_market_logloss": point[1],
        "kernel_minus_market_logloss_ci95": [
            quantile([row[1] for row in boots], 0.025),
            quantile([row[1] for row in boots], 0.975),
        ],
    }


def ols_slope(x: list[float], y: list[float]) -> float | None:
    if len(x) < 2:
        return None
    xm = statistics.fmean(x)
    ym = statistics.fmean(y)
    denominator = sum((value - xm) ** 2 for value in x)
    if denominator <= 0:
        return None
    return sum(
        (xi - xm) * (yi - ym) for xi, yi in zip(x, y)
    ) / denominator


def pearson(x: list[float], y: list[float]) -> float | None:
    if len(x) < 2:
        return None
    xm = statistics.fmean(x)
    ym = statistics.fmean(y)
    xx = sum((value - xm) ** 2 for value in x)
    yy = sum((value - ym) ** 2 for value in y)
    if xx <= 0 or yy <= 0:
        return None
    return sum(
        (xi - xm) * (yi - ym) for xi, yi in zip(x, y)
    ) / math.sqrt(xx * yy)


def lead_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    x = [float(row["gap_pp"]) for row in rows]
    y = [float(row["future_change_pp"]) for row in rows]
    directional = [
        1.0 if xi * yi > 0 else 0.0
        for xi, yi in zip(x, y) if xi != 0 and yi != 0
    ]
    return {
        "n": len(rows),
        "markets": len({row["market"] for row in rows}),
        "ols_slope": ols_slope(x, y),
        "mean_signed_move_pp": statistics.fmean(
            math.copysign(1.0, xi) * yi if xi != 0 else 0.0
            for xi, yi in zip(x, y)
        ),
        "directional_hit_rate": (
            statistics.fmean(directional) if directional else None
        ),
        "pearson": pearson(x, y),
    }


def lead_with_ci(rows: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    point = lead_stats(rows)
    if not rows:
        return point
    boots = [
        lead_stats(sample) for sample in cluster_resamples(rows, seed)
    ]
    for key in (
        "ols_slope",
        "mean_signed_move_pp",
        "directional_hit_rate",
        "pearson",
    ):
        values = [
            float(row[key]) for row in boots if row.get(key) is not None
        ]
        point[f"{key}_ci95"] = [
            quantile(values, 0.025),
            quantile(values, 0.975),
        ]
    return point


def calibration_rows(
    metadata: dict[str, dict[str, Any]],
    times: list[int],
    values: list[float],
    timelines: dict[str, list[tuple[int, float, int, int]]],
) -> tuple[list[dict[str, Any]], Counter]:
    rows = []
    excluded = Counter()
    for market, meta in sorted(metadata.items()):
        for horizon_s in HORIZONS_S:
            target_ms = meta["close_ms"] - horizon_s * 1000
            kernel = kernel_at(
                times,
                values,
                target_ms,
                meta["strike"],
                meta["close_ms"],
            )
            if kernel is None:
                excluded["kernel_unavailable"] += 1
                continue
            book = book_at(
                timelines.get(market, []),
                int(kernel["source_ms"] * 1000),
            )
            row = {
                "market": market,
                "close_date": meta["close_date"],
                "horizon_s": horizon_s,
                "outcome": meta["outcome"],
                "strike": meta["strike"],
                "kernel_p": kernel["fair"],
                "market_p": book["mid"] if book else None,
                "kernel": kernel,
            }
            if book is None:
                excluded["market_mid_unavailable"] += 1
            rows.append(row)
    return rows, excluded


def gap_rows(
    metadata: dict[str, dict[str, Any]],
    times: list[int],
    values: list[float],
    timelines: dict[str, list[tuple[int, float, int, int]]],
) -> tuple[dict[int, list[dict[str, Any]]], Counter]:
    rows_by_lead = {lead: [] for lead in LEADS_S}
    excluded = Counter()
    for market, meta in sorted(metadata.items()):
        timeline = timelines.get(market)
        if not timeline:
            excluded["invalid_or_missing_book"] += 1
            continue
        start_s = math.ceil((meta["close_ms"] / 1000 - 299) / 5) * 5
        end_s = math.floor((meta["close_ms"] / 1000 - 120) / 5) * 5
        second = start_s
        while second <= end_s:
            target_ms = round(second * 1000)
            kernel = kernel_at(
                times,
                values,
                target_ms,
                meta["strike"],
                meta["close_ms"],
            )
            if kernel is None:
                excluded["kernel_unavailable"] += 1
                second += 5
                continue
            now_book = book_at(timeline, int(kernel["source_ms"] * 1000))
            if now_book is None:
                excluded["initial_mid_unavailable"] += 1
                second += 5
                continue
            gap_pp = (kernel["fair"] - now_book["mid"]) * 100.0
            for lead in LEADS_S:
                future = book_at(
                    timeline,
                    int((kernel["source_ms"] + lead * 1000) * 1000),
                )
                if future is None:
                    excluded[f"future_{lead}s_unavailable"] += 1
                    continue
                rows_by_lead[lead].append({
                    "market": market,
                    "source_ms": int(kernel["source_ms"]),
                    "gap_pp": gap_pp,
                    "future_change_pp": (
                        future["mid"] - now_book["mid"]
                    ) * 100.0,
                })
            second += 5
    return rows_by_lead, excluded


def p3_schema_audit() -> dict[str, Any]:
    assert_safe_path(P3_PATH)
    if not P3_PATH.exists():
        return {
            "available": False,
            "reason": f"missing {P3_PATH}",
        }
    raw_bytes = P3_PATH.read_bytes()
    artifact = json.loads(raw_bytes)
    if artifact.get("this_run_read_2026_07_23") is not False:
        return {
            "available": False,
            "reason": (
                "artifact does not explicitly attest "
                "this_run_read_2026_07_23=false; not used"
            ),
            "manifest": {
                "path": str(P3_PATH),
                "bytes": len(raw_bytes),
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            },
        }
    arm = (artifact.get("arms") or {}).get(P3_ARM)
    cycles = (arm or {}).get("cycles") or []
    observed_dates = {
        str(row.get("date")) for row in cycles if row.get("date") is not None
    }
    if not observed_dates.issubset(ALLOWED_DATES):
        return {
            "available": False,
            "reason": (
                "P3 cycles contain dates outside the exact allowed set: "
                f"{sorted(observed_dates - set(ALLOWED_DATES))}"
            ),
            "manifest": {
                "path": str(P3_PATH),
                "bytes": len(raw_bytes),
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            },
        }
    keys = sorted({key for row in cycles for key in row})
    pnl_required = {"market", "first_ts", "pnl_c"}
    cost_required = pnl_required | {"first_side", "first_price"}
    return {
        "available": arm is not None,
        "manifest": {
            "path": str(P3_PATH),
            "bytes": len(raw_bytes),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        },
        "arm": P3_ARM,
        "cycle_n": len(cycles),
        "market_n": len({
            row.get("market") for row in cycles if row.get("market")
        }),
        "cycle_keys": keys,
        "pnl_link_required_fields": sorted(pnl_required),
        "pnl_link_fields_present": pnl_required.issubset(keys),
        "completion_cost_required_fields": sorted(cost_required),
        "completion_cost_fields_present": cost_required.issubset(keys),
        "note": (
            "Feature/outcome modeling additionally requires historical BRTI; "
            "schema presence alone is not an alignment result."
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    status = report["status"]
    if status != "NO_DECISION_DATA_ALIGNMENT_BLOCKED":
        calibration = report["calibration"]["all_horizons"]
        lead = report["fair_mid_gap_lead"]["by_horizon_s"]
        return "\n".join([
            "# BRTI fair-pricing independent audit",
            "",
            f"Status: **{status}** (discovery only).",
            "",
            "## Calibration",
            "",
            f"- Kernel: `{json.dumps(calibration['kernel'], sort_keys=True)}`",
            f"- Market: `{json.dumps(calibration['market'], sort_keys=True)}`",
            "- Paired difference: "
            f"`{json.dumps(calibration['paired_difference'], sort_keys=True)}`",
            "",
            "## Fair-minus-mid lead",
            "",
            *[
                f"- {horizon}s: `{json.dumps(row, sort_keys=True)}`"
                for horizon, row in sorted(lead.items(), key=lambda item: int(item[0]))
            ],
            "",
            "Calibration and execution/completion are distinct layers. "
            "This discovery result does not authorize deployment.",
            "",
        ])
    availability = report["availability"]
    missing = report.get("blocking_gaps", [])
    analysis = report["analyses"]
    lines = [
        "# BRTI fair-pricing independent audit",
        "",
        f"Status: **{status}**.",
        "",
        "This is a discovery-only, read-only audit. It did not import the "
        "engine, connect to the exchange, place/cancel an order, or touch "
        "2026-07-23.",
        "",
        "## Source-time availability",
        "",
    ]
    for date in ALLOWED_DATES:
        row = availability["cfbenchmarks_by_date"][date]
        lines.append(
            f"- {date}: CF Benchmarks files = **{row['file_n']}**; "
            f"book parquet files = **"
            f"{availability['books_by_date'][date]['file_n']}**."
        )
    later = availability["later_cf_inventory"]
    lines.extend([
        f"- Earliest fixed, non-banned later date with a capture: "
        f"`{later['earliest_known_later_capture_date']}`.",
        "",
        "## Blocking gaps",
        "",
    ])
    lines.extend(f"- {item}" for item in missing)
    lines.extend([
        "",
        "## Requested analyses",
        "",
    ])
    for key, value in analysis.items():
        lines.append(f"- `{key}`: **{value['status']}** — {value['reason']}")
    lines.extend([
        "",
        "## Calibration versus execution",
        "",
        "Calibration asks whether BRTI fair probabilities predict the final "
        "YES/NO result and lead market mid. Execution asks whether a first "
        "maker fill can be economically completed after queueing, adverse "
        "selection, fees, and forced exit. A loss in the latter does not "
        "identify an error in the former.",
        "",
        "No proxy was substituted for missing BRTI. In particular, later "
        "2026-07-25..26 capture, Coinbase, market mid, and terminal settlement "
        "cannot reconstruct the missing 60s/300s source-time volatility or "
        "the contemporaneous fair path for 2026-07-20..22.",
        "",
        "Exact manifests, schema checks, guards, and seal bindings are in "
        "`brti_fair_pricing_audit.json`.",
        "",
    ])
    return "\n".join(lines)


def blocked_report(
    seal_info: dict[str, Any],
    cf_by_date: dict[str, list[str]],
    books_by_date: dict[str, list[str]],
) -> dict[str, Any]:
    later = later_cf_inventory()
    p3 = p3_schema_audit()
    cf_availability = {
        date: {
            "file_n": len(cf_by_date[date]),
            "paths": cf_by_date[date],
        }
        for date in ALLOWED_DATES
    }
    book_availability = {
        date: {
            "file_n": len(books_by_date[date]),
            "paths": books_by_date[date],
        }
        for date in ALLOWED_DATES
    }
    missing_dates = [
        date for date in ALLOWED_DATES if not cf_by_date[date]
    ]
    blocking = [
        (
            "Missing authoritative `cfbenchmarks_value`/BRTI capture for "
            f"{date}: expected `{CF_TEMPLATE.format(date=date)}`."
        )
        for date in missing_dates
    ]
    blocking.extend([
        (
            "Without per-second BRTI source time/value, sigma60, sigma300, "
            "kernel fair, fair velocity, BRTI momentum, and standardized "
            "distance are unidentified."
        ),
        (
            "Market metadata/settlement and Kalshi books cannot backfill the "
            "missing BRTI path without changing the pre-registered model."
        ),
    ])
    common_reason = (
        "Authoritative BRTI source-time path is absent on one or more "
        "required discovery dates; no proxy substitution is permitted."
    )
    return {
        "schema": "brti-fair-pricing-audit-v1",
        "status": "NO_DECISION_DATA_ALIGNMENT_BLOCKED",
        "historical_validation_claim": False,
        "read_only": True,
        "trading_action_performed": False,
        "engine_or_service_mutation_performed": False,
        "allowed_dates_opened_or_checked": list(ALLOWED_DATES),
        "hard_banned_date": BANNED_DATE,
        "hard_banned_date_opened_enumerated_or_hashed": False,
        "seal": seal_info,
        "availability": {
            "cfbenchmarks_by_date": cf_availability,
            "books_by_date": book_availability,
            "market_metadata": {
                "path": str(META_PATH),
                "exists": META_PATH.exists(),
            },
            "p3": p3,
            "later_cf_inventory": later,
        },
        "blocking_gaps": blocking,
        "analyses": {
            "kernel_vs_settlement_calibration": {
                "status": "BLOCKED",
                "reason": common_reason,
            },
            "kernel_vs_market_mid_benchmark": {
                "status": "BLOCKED",
                "reason": common_reason,
            },
            "fair_mid_gap_future_mid_change": {
                "status": "BLOCKED",
                "reason": common_reason,
            },
            "p3_first_fill_pnl_oof": {
                "status": "BLOCKED",
                "reason": common_reason,
            },
            "p3_completion_cost_deterioration_oof": {
                "status": "BLOCKED",
                "reason": (
                    common_reason
                    + " Existing P3 schema must also contain first side/price."
                ),
            },
        },
        "identification_statement": {
            "calibration": (
                "No conclusion on whether the BRTI kernel is correctly or "
                "incorrectly calibrated."
            ),
            "execution": (
                "Existing negative first-fill/forced-exit PnL remains evidence "
                "about execution/completion, not evidence that BRTI fair is "
                "wrong."
            ),
        },
    }


def full_report(
    seal_info: dict[str, Any],
    cf_by_date: dict[str, list[str]],
    books_by_date: dict[str, list[str]],
) -> dict[str, Any]:
    cf_paths = [
        path for date in ALLOWED_DATES for path in cf_by_date[date]
    ]
    times, values, cf_parse = parse_cf(cf_paths)
    metadata, metadata_audit = load_metadata()
    timelines, book_audit = load_books(
        books_by_date, set(metadata)
    )
    calibration, calibration_excluded = calibration_rows(
        metadata, times, values, timelines
    )
    gap, gap_excluded = gap_rows(metadata, times, values, timelines)
    kernel_rows = calibration
    market_rows = [
        row for row in calibration if row.get("market_p") is not None
    ]
    by_horizon = {}
    for horizon in HORIZONS_S:
        hrows = [
            row for row in calibration if row["horizon_s"] == horizon
        ]
        by_horizon[str(horizon)] = {
            "kernel": forecast_metrics(hrows, "kernel_p"),
            "market": forecast_metrics(
                [row for row in hrows if row.get("market_p") is not None],
                "market_p",
            ),
            "paired_difference": paired_metric_differences(hrows),
        }
    lead_results = {
        str(lead): lead_with_ci(
            gap[lead], LEAD_SEED + lead
        )
        for lead in LEADS_S
    }
    p3 = p3_schema_audit()
    return {
        "schema": "brti-fair-pricing-audit-v1",
        "status": "DISCOVERY_RESULT",
        "historical_validation_claim": False,
        "read_only": True,
        "trading_action_performed": False,
        "engine_or_service_mutation_performed": False,
        "allowed_dates_opened_or_checked": list(ALLOWED_DATES),
        "hard_banned_date": BANNED_DATE,
        "hard_banned_date_opened_enumerated_or_hashed": False,
        "seal": seal_info,
        "sources": {
            "cfbenchmarks": file_manifest(cf_paths),
            "cf_parse": cf_parse,
            "market_metadata": metadata_audit,
            "books": file_manifest(
                path
                for date in ALLOWED_DATES
                for path in books_by_date[date]
            ),
            "book_replay": book_audit,
            "p3": p3,
        },
        "calibration": {
            "all_horizons": {
                "kernel": forecast_metrics(kernel_rows, "kernel_p"),
                "market": forecast_metrics(market_rows, "market_p"),
                "paired_difference": paired_metric_differences(calibration),
            },
            "by_horizon_s": by_horizon,
            "excluded": dict(sorted(calibration_excluded.items())),
            "rows": calibration,
        },
        "fair_mid_gap_lead": {
            "by_horizon_s": lead_results,
            "excluded": dict(sorted(gap_excluded.items())),
        },
        "p3_link": {
            "status": "SCHEMA_ONLY_NOT_MODELED",
            "reason": (
                "The sealed script records schema eligibility. P3 OOF requires "
                "the pre-registered aligned feature matrix; absent required "
                "fields remain a NO_DECISION rather than imputation."
            ),
            "schema_audit": p3,
        },
        "interpretation_boundary": {
            "calibration": (
                "Brier/logloss/calibration slope and gap lead test direction "
                "pricing."
            ),
            "execution": (
                "Queue fill, opposite-leg completion, taker exit, and episode "
                "PnL test execution; neither layer substitutes for the other."
            ),
        },
    }


def rounded(value: Any, digits: int = 10) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return round(value, digits)
    if isinstance(value, list):
        return [rounded(item, digits) for item in value]
    if isinstance(value, dict):
        return {
            key: rounded(item, digits) for key, item in value.items()
        }
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", required=True)
    parser.add_argument("--seal", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--md-out", required=True)
    args = parser.parse_args()
    script_path = Path(__file__).resolve()
    prereg_path = Path(args.prereg).resolve()
    seal_path = Path(args.seal).resolve()
    out_path = Path(args.out).resolve()
    md_path = Path(args.md_out).resolve()
    seal_info = verify_seal(
        script_path, prereg_path, seal_path
    )
    cf_by_date = exact_allowed_globs(CF_TEMPLATE)
    books_by_date = exact_allowed_globs(BOOK_TEMPLATE)
    if any(not cf_by_date[date] for date in ALLOWED_DATES):
        report = blocked_report(seal_info, cf_by_date, books_by_date)
    else:
        report = full_report(seal_info, cf_by_date, books_by_date)
    report = rounded(report)
    out_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")


if __name__ == "__main__":
    main()
