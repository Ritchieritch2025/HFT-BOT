#!/usr/bin/env python3
"""Sealed calibration audit using authoritative historical BRTI.

The audit tests direction pricing only. It reconstructs Kalshi books in
causal local-receive order for a contemporaneous market-mid benchmark, but
does not infer queue fills, opposite-leg completion, or executable PnL.
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
import datetime as dt
import gzip
import glob
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
from typing import Any, Iterable


ALLOWED_DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
BANNED_START = "2026-07-23T00:00:00.000Z"
SERIES = "KXBTC15M"
HORIZONS_S = (120, 180, 240)
INFORMATION_LAG_MS = 2_000
MAX_SOURCE_GAP_MS = 1_500
MIN_COVERAGE = 0.999
MIN_INCREMENTS = 30
LOCK_TICKS = 60
EPS = 1e-6
BOOT_REPS = 2_000
BOOT_SEED = 26_072_611

META_PATH = Path(
    "/home/ubuntu/h6b_inputs/catalog_normal/"
    "snapshot=20260724T213017Z/shards/KXBTC15M.json"
)
BOOK_TEMPLATE = (
    "/home/ubuntu/hft-bot/work/warehouse/facts/orderbooks_full/"
    "category=Crypto/subcategory=BTC/date={date}/*.parquet"
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_utc_ms(value: Any) -> int:
    parsed = dt.datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    if parsed.tzinfo is None:
        raise ValueError(f"naive timestamp: {value}")
    return round(parsed.timestamp() * 1000)


def utc_date(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(
        ts_ms / 1000, tz=dt.timezone.utc
    ).date().isoformat()


BANNED_START_MS = parse_utc_ms(BANNED_START)


def assert_not_banned_history_path(path: str | Path) -> None:
    text = str(path)
    if "brti_history_2026-07-23" in text:
        raise RuntimeError(f"hard-banned historical BRTI path: {text}")


def file_manifest(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    rows = []
    for raw in sorted(str(value) for value in paths):
        assert_not_banned_history_path(raw)
        path = Path(raw)
        rows.append({
            "path": raw,
            "bytes": path.stat().st_size,
            "sha256": sha256_path(path),
        })
    return rows


def verify_seal(
    script: Path,
    downloader: Path,
    prereg: Path,
    seal: Path,
) -> dict[str, Any]:
    frozen = json.loads(seal.read_text(encoding="utf-8"))
    observed = {
        "analyzer_sha256": sha256_path(script),
        "downloader_sha256": sha256_path(downloader),
        "preregistration_sha256": sha256_path(prereg),
    }
    for key, value in observed.items():
        if frozen.get(key) != value:
            raise RuntimeError(f"seal mismatch for {key}: observed {value}")
    specification = json.loads(prereg.read_text(encoding="utf-8"))
    if (
        specification.get("download", {}).get("timestamps_utc")
        != [f"{date}T00:00:00.000Z" for date in ALLOWED_DATES]
    ):
        raise RuntimeError("pre-registration day list mismatch")
    if (
        specification.get("download", {}).get(
            "hard_banned_start_utc"
        )
        != BANNED_START
    ):
        raise RuntimeError("pre-registration banned-start mismatch")
    if (
        specification.get("kernel", {}).get("forecast_horizons_tte_s")
        != list(HORIZONS_S)
    ):
        raise RuntimeError("pre-registration horizon mismatch")
    return {
        **observed,
        "seal_sha256": sha256_path(seal),
        "verified": True,
    }


def parse_authoritative_envelope(
    body: bytes,
    expected_date: str,
) -> tuple[list[tuple[int, float]], dict[str, Any]]:
    envelope = json.loads(body)
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError("invalid/error Kalshi CF passthrough envelope")
    payload = data.get("payload")
    if not isinstance(payload, list):
        raise RuntimeError("authoritative CF payload is not a list")
    start_ms = parse_utc_ms(f"{expected_date}T00:00:00.000Z")
    end_ms = start_ms + 86_400_000
    rows: list[tuple[int, float]] = []
    last_time = None
    for row_number, row in enumerate(payload):
        if not isinstance(row, dict):
            raise RuntimeError(f"payload row {row_number} is not an object")
        raw_time = row.get("time")
        if isinstance(raw_time, bool) or not isinstance(raw_time, int):
            raise RuntimeError(f"payload row {row_number} has invalid time")
        source_ms = int(raw_time)
        if not start_ms <= source_ms < end_ms:
            raise RuntimeError(
                f"payload row {row_number} outside {expected_date}"
            )
        if source_ms >= BANNED_START_MS:
            raise RuntimeError("historical payload reaches banned start")
        if last_time is not None and source_ms < last_time:
            raise RuntimeError("historical payload is not ascending")
        try:
            value = float(row.get("value"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"payload row {row_number} has invalid value"
            ) from exc
        if not math.isfinite(value) or value <= 0:
            raise RuntimeError(
                f"payload row {row_number} has nonpositive value"
            )
        rows.append((source_ms, value))
        last_time = source_ms
    return rows, {
        "date": expected_date,
        "row_count": len(rows),
        "minimum_source_ms": rows[0][0] if rows else None,
        "maximum_source_ms": rows[-1][0] if rows else None,
        "duplicate_source_timestamps": (
            len(rows) - len({row[0] for row in rows})
        ),
        "payload_sha256": sha256_bytes(json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()),
    }


def load_authoritative_history(
    source_dir: Path,
    acquisition_receipt: Path,
    seal_info: dict[str, Any],
) -> tuple[list[int], list[float], dict[str, Any]]:
    receipt_bytes = acquisition_receipt.read_bytes()
    receipt = json.loads(receipt_bytes)
    if receipt.get("status") != "COMPLETE_AUTHORITATIVE_HISTORY_ACQUIRED":
        raise RuntimeError("acquisition receipt is not complete")
    if int(receipt.get("request_count_attempted", -1)) != 3:
        raise RuntimeError("acquisition request count is not exactly three")
    if int(receipt.get("retry_count", -1)) != 0:
        raise RuntimeError("acquisition receipt has retries")
    if int(receipt.get("redirect_count", -1)) != 0:
        raise RuntimeError("acquisition receipt has redirects")
    if receipt.get("hard_banned_history_requested") is not False:
        raise RuntimeError("acquisition receipt lacks banned-history guard")
    receipt_seal = receipt.get("seal") or {}
    for key in (
        "analyzer_sha256",
        "downloader_sha256",
        "preregistration_sha256",
        "seal_sha256",
    ):
        if receipt_seal.get(key) != seal_info.get(key):
            raise RuntimeError(f"acquisition seal mismatch: {key}")
    request_by_date = {}
    for request in receipt.get("requests") or []:
        query = request.get("query") or {}
        timestamp = query.get("timestamp")
        if timestamp:
            request_by_date[str(timestamp)[:10]] = request
    by_source_ms: dict[int, float] = {}
    day_audits = []
    source_paths = []
    duplicates_equal = 0
    for date in ALLOWED_DATES:
        request = request_by_date.get(date)
        if request is None or request.get("http_status") != 200:
            raise RuntimeError(f"missing successful acquisition for {date}")
        path = source_dir / f"brti_history_{date}.json.gz"
        assert_not_banned_history_path(path)
        compressed = path.read_bytes()
        if sha256_bytes(compressed) != request.get("stored_sha256"):
            raise RuntimeError(f"stored gzip hash mismatch for {date}")
        body = gzip.decompress(compressed)
        if sha256_bytes(body) != request.get("body_sha256"):
            raise RuntimeError(f"raw body hash mismatch for {date}")
        rows, audit = parse_authoritative_envelope(body, date)
        if audit["payload_sha256"] != request.get("payload_sha256"):
            raise RuntimeError(f"payload hash mismatch for {date}")
        if audit["row_count"] != request.get("payload_count"):
            raise RuntimeError(f"payload count mismatch for {date}")
        for source_ms, value in rows:
            if source_ms in by_source_ms:
                if by_source_ms[source_ms] != value:
                    raise RuntimeError(
                        f"conflicting duplicate source timestamp {source_ms}"
                    )
                duplicates_equal += 1
            else:
                by_source_ms[source_ms] = value
        source_paths.append(path)
        day_audits.append(audit)
    times = sorted(by_source_ms)
    values = [by_source_ms[source_ms] for source_ms in times]
    return times, values, {
        "acquisition_receipt": {
            "path": str(acquisition_receipt),
            "bytes": len(receipt_bytes),
            "sha256": sha256_bytes(receipt_bytes),
        },
        "raw_envelopes": file_manifest(source_paths),
        "days": day_audits,
        "deduplicated_tick_count": len(times),
        "equal_duplicate_rows_removed": duplicates_equal,
        "minimum_source_ms": times[0] if times else None,
        "maximum_source_ms": times[-1] if times else None,
        "individual_values_in_report": False,
    }


def load_metadata() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    raw_bytes = META_PATH.read_bytes()
    source = json.loads(raw_bytes)
    markets = source.get("markets") if isinstance(source, dict) else None
    if not isinstance(markets, dict):
        raise RuntimeError("catalog metadata has no markets mapping")
    selected: dict[str, dict[str, Any]] = {}
    counts = Counter()
    for ticker, market in markets.items():
        counts["catalog_market_n"] += 1
        if not str(ticker).startswith(f"{SERIES}-"):
            continue
        counts["series_market_n"] += 1
        try:
            close_ms = parse_utc_ms(market["close_time"])
        except (KeyError, TypeError, ValueError):
            counts["missing_or_invalid_close_time"] += 1
            continue
        close_date = utc_date(close_ms)
        if close_date not in ALLOWED_DATES:
            counts["outside_selected_close_dates"] += 1
            continue
        result = str(market.get("result") or "").lower()
        if result not in ("yes", "no"):
            counts["not_final_yes_no"] += 1
            continue
        raw_strike = market.get("floor_strike")
        try:
            strike = float(raw_strike)
        except (TypeError, ValueError):
            counts["missing_floor_strike"] += 1
            continue
        if not math.isfinite(strike):
            counts["invalid_floor_strike"] += 1
            continue
        selected[str(ticker)] = {
            "market": str(ticker),
            "close_ms": close_ms,
            "close_date": close_date,
            "strike": strike,
            "outcome": 1 if result == "yes" else 0,
        }
        counts["selected_settled_market_n"] += 1
    return selected, {
        "manifest": {
            "path": str(META_PATH),
            "bytes": len(raw_bytes),
            "sha256": sha256_bytes(raw_bytes),
        },
        "counts": dict(sorted(counts.items())),
    }


def settlement_crosscheck(
    metadata: dict[str, dict[str, Any]],
    times: list[int],
    values: list[float],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    value_by_time = dict(zip(times, values))
    verified = {}
    counts = Counter()
    exclusions = []
    for ticker, meta in sorted(metadata.items()):
        expected = [
            meta["close_ms"] - 60_000 + second * 1_000
            for second in range(60)
        ]
        observed = [
            value_by_time[source_ms]
            for source_ms in expected
            if source_ms in value_by_time
        ]
        if len(observed) != 60:
            counts["missing_exact_60_tick_window"] += 1
            exclusions.append({
                "market": ticker,
                "reason": "missing_exact_60_tick_window",
                "observed_tick_count": len(observed),
            })
            continue
        settlement_outcome = (
            1 if statistics.fmean(observed) >= meta["strike"] else 0
        )
        if settlement_outcome != meta["outcome"]:
            counts["computed_outcome_mismatch"] += 1
            exclusions.append({
                "market": ticker,
                "reason": "computed_outcome_mismatch",
                "computed_outcome": settlement_outcome,
                "official_outcome": meta["outcome"],
            })
            continue
        verified[ticker] = meta
        counts["verified_market_n"] += 1
    return verified, {
        "counts": dict(sorted(counts.items())),
        "excluded": exclusions,
        "rule": (
            "Exactly the 60 source timestamps close-60s through close-1s "
            "must exist; raw mean>=floor_strike must match official result."
        ),
        "settlement_means_disclosed": False,
    }


def rw_avg_var_factor(m: int) -> float:
    return m * (m + 1) * (2 * m + 1) / 6.0


def sigma_window(
    times: list[int],
    values: list[float],
    end_index: int,
    window_s: int,
) -> float | None:
    end_ms = times[end_index]
    start_ms = end_ms - window_s * 1000
    start_index = bisect.bisect_left(
        times, start_ms, 0, end_index + 1
    )
    sample_times = times[start_index:end_index + 1]
    sample_values = values[start_index:end_index + 1]
    if len(sample_times) - 1 < MIN_INCREMENTS:
        return None
    elapsed_ms = sample_times[-1] - sample_times[0]
    if elapsed_ms < window_s * 1000 * MIN_COVERAGE:
        return None
    variance_numerator = 0.0
    total_seconds = 0.0
    for t0, t1, v0, v1 in zip(
        sample_times,
        sample_times[1:],
        sample_values,
        sample_values[1:],
    ):
        delta_ms = t1 - t0
        if delta_ms <= 0 or delta_ms > MAX_SOURCE_GAP_MS:
            return None
        delta_s = delta_ms / 1000.0
        variance_numerator += (v1 - v0) ** 2
        total_seconds += delta_s
    if total_seconds <= 0 or variance_numerator <= 0:
        return None
    return math.sqrt(variance_numerator / total_seconds)


def kernel_at(
    times: list[int],
    values: list[float],
    target_ms: int,
    close_ms: int,
    strike: float,
) -> dict[str, float] | None:
    information_cutoff_ms = target_ms - INFORMATION_LAG_MS
    index = bisect.bisect_right(times, information_cutoff_ms) - 1
    if index < 0:
        return None
    source_gap_ms = information_cutoff_ms - times[index]
    if source_gap_ms < 0 or source_gap_ms > MAX_SOURCE_GAP_MS:
        return None
    sigma60 = sigma_window(times, values, index, 60)
    sigma300 = sigma_window(times, values, index, 300)
    if sigma60 is None or sigma300 is None:
        return None
    sigma = max(sigma60, sigma300)
    model_tte_s = (close_ms - times[index]) / 1000.0
    factor = (
        max(model_tte_s - LOCK_TICKS, 0.0)
        + rw_avg_var_factor(LOCK_TICKS - 1) / (LOCK_TICKS ** 2)
    )
    standard_deviation = sigma * math.sqrt(factor)
    if standard_deviation <= 0:
        probability = 1.0 if values[index] >= strike else 0.0
    else:
        z_score = (values[index] - strike) / standard_deviation
        probability = 0.5 * (
            1.0 + math.erf(z_score / math.sqrt(2.0))
        )
    return {
        "probability": probability,
        "sigma60": sigma60,
        "sigma300": sigma300,
        "sigma": sigma,
        "model_tte_s": model_tte_s,
        "information_age_s": (
            target_ms - times[index]
        ) / 1000.0,
        "cutoff_source_gap_s": source_gap_ms / 1000.0,
    }


def exact_book_paths() -> dict[str, list[str]]:
    by_date = {}
    for date in ALLOWED_DATES:
        pattern = BOOK_TEMPLATE.format(date=date)
        paths = sorted(glob.glob(pattern))
        if any("date=2026-07-23" in path for path in paths):
            raise RuntimeError("book glob resolved banned date")
        by_date[date] = paths
    return by_date


def strict_integer(value: Any, label: str) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label}:missing_or_boolean")
    integer = int(value)
    if isinstance(value, float) and value != integer:
        raise ValueError(f"{label}:not_integral")
    return integer


def parse_snapshot_levels(
    raw: str | None,
    side: str,
) -> dict[int, int]:
    levels = json.loads(raw) if raw else []
    if not isinstance(levels, list):
        raise ValueError(f"{side}:snapshot_not_list")
    book = {}
    for item in levels:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError(f"{side}:snapshot_level_shape")
        price = strict_integer(item[0], f"{side}:snapshot_price")
        quantity = strict_integer(item[1], f"{side}:snapshot_quantity")
        if not 0 <= price <= 10_000:
            raise ValueError(f"{side}:snapshot_price_out_of_range")
        if quantity < 0:
            raise ValueError(f"{side}:negative_snapshot_quantity")
        if price in book:
            raise ValueError(f"{side}:duplicate_snapshot_price")
        if quantity > 0:
            book[price] = quantity
    return book


def best_bid(book: dict[int, int]) -> int | None:
    return max(book) if book else None


def load_book_samples(
    paths_by_date: dict[str, list[str]],
    metadata: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[int, dict[str, Any]]], dict[str, Any]]:
    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError("duckdb unavailable for causal replay") from exc

    samples: dict[str, dict[int, dict[str, Any]]] = {}
    invalid: dict[str, list[str]] = defaultdict(list)
    counters = Counter()

    def targets_for(ticker: str) -> dict[int, int]:
        return {
            horizon: (
                metadata[ticker]["close_ms"] - horizon * 1000
            ) * 1_000_000
            for horizon in HORIZONS_S
        }

    for date in ALLOWED_DATES:
        paths = paths_by_date[date]
        wanted = sorted(
            ticker for ticker, meta in metadata.items()
            if meta["close_date"] == date
        )
        if not paths or not wanted:
            continue
        connection = duckdb.connect()
        connection.execute("SET threads=2")
        connection.execute("SET memory_limit='8GB'")
        connection.execute("CREATE TEMP TABLE wanted(ticker VARCHAR)")
        connection.executemany(
            "INSERT INTO wanted VALUES (?)", [(ticker,) for ticker in wanted]
        )
        cursor = connection.execute(
            """
            SELECT p.market_ticker, p.msg_type, p.side, p.price_e4,
                   p.delta_e4, CAST(p.yes_levels AS VARCHAR),
                   CAST(p.no_levels AS VARCHAR), p.ws_sid, p.ws_seq,
                   p.recv_wall_ns, p.recv_mono_ns
            FROM read_parquet(?, union_by_name=true) AS p
            INNER JOIN wanted AS w ON p.market_ticker=w.ticker
            ORDER BY p.market_ticker, p.recv_wall_ns,
                     p.recv_mono_ns, p.ws_seq
            """,
            [paths],
        )
        current_ticker = None
        books = {"yes": {}, "no": {}}
        initialized = False
        last_receive_key = None
        current_samples: dict[int, dict[str, Any]] = {}
        ticker_invalid = False
        ticker_targets: dict[int, int] = {}

        def finish_ticker() -> None:
            nonlocal current_ticker
            if current_ticker is None:
                return
            counters["market_stream_n"] += 1
            if ticker_invalid:
                counters["invalid_market_stream_n"] += 1
            else:
                samples[current_ticker] = dict(current_samples)
                counters["valid_market_stream_n"] += 1

        while True:
            batch = cursor.fetchmany(200_000)
            if not batch:
                break
            for row in batch:
                (
                    ticker,
                    message_type,
                    side,
                    price_raw,
                    delta_raw,
                    yes_levels_raw,
                    no_levels_raw,
                    _ws_sid,
                    ws_seq_raw,
                    recv_wall_raw,
                    recv_mono_raw,
                ) = row
                ticker = str(ticker)
                if ticker != current_ticker:
                    finish_ticker()
                    current_ticker = ticker
                    books = {"yes": {}, "no": {}}
                    initialized = False
                    last_receive_key = None
                    current_samples = {}
                    ticker_invalid = False
                    ticker_targets = targets_for(ticker)
                counters["event_rows"] += 1
                try:
                    recv_wall_ns = strict_integer(
                        recv_wall_raw, "recv_wall_ns"
                    )
                    recv_mono_ns = strict_integer(
                        recv_mono_raw, "recv_mono_ns"
                    )
                    ws_seq = strict_integer(ws_seq_raw, "ws_seq")
                    receive_key = (
                        recv_wall_ns,
                        recv_mono_ns,
                        ws_seq,
                    )
                    if (
                        last_receive_key is not None
                        and receive_key <= last_receive_key
                    ):
                        raise ValueError("non_increasing_receive_key")
                    last_receive_key = receive_key

                    if message_type == "snapshot":
                        books = {
                            "yes": parse_snapshot_levels(
                                yes_levels_raw, "yes"
                            ),
                            "no": parse_snapshot_levels(
                                no_levels_raw, "no"
                            ),
                        }
                        initialized = True
                        counters["snapshot_rows"] += 1
                    elif message_type == "delta":
                        counters["delta_rows"] += 1
                        if not initialized:
                            raise ValueError("delta_before_snapshot")
                        if side not in ("yes", "no"):
                            raise ValueError("unknown_delta_side")
                        price = strict_integer(price_raw, "delta_price")
                        delta = strict_integer(delta_raw, "delta_quantity")
                        if not 0 <= price <= 10_000:
                            raise ValueError("delta_price_out_of_range")
                        quantity = books[side].get(price, 0) + delta
                        if quantity < 0:
                            raise ValueError("negative_displayed_quantity")
                        if quantity == 0:
                            books[side].pop(price, None)
                        else:
                            books[side][price] = quantity
                    else:
                        raise ValueError("unknown_message_type")
                except (
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                ) as exc:
                    ticker_invalid = True
                    invalid[ticker].append(f"{date}:{exc}")
                    continue
                if ticker_invalid or not initialized:
                    continue
                yes_bid = best_bid(books["yes"])
                no_bid = best_bid(books["no"])
                if yes_bid is None or no_bid is None:
                    continue
                yes_mid = (
                    yes_bid + (10_000 - no_bid)
                ) / 20_000.0
                if not 0.0 <= yes_mid <= 1.0:
                    ticker_invalid = True
                    invalid[ticker].append(f"{date}:mid_out_of_range")
                    continue
                for horizon, target_ns in ticker_targets.items():
                    if recv_wall_ns <= target_ns:
                        current_samples[horizon] = {
                            "probability": yes_mid,
                            "recv_wall_ns": recv_wall_ns,
                            "age_s": (
                                target_ns - recv_wall_ns
                            ) / 1_000_000_000.0,
                            "yes_bid_e4": yes_bid,
                            "no_bid_e4": no_bid,
                        }
                        counters["two_sided_sample_candidates"] += 1
        finish_ticker()
        connection.close()
    for ticker in metadata:
        if ticker not in samples and ticker not in invalid:
            counters["no_book_rows_for_selected_market"] += 1
    return samples, {
        "counts": dict(sorted(counters.items())),
        "invalid_market_n": len(invalid),
        "invalid_markets": {
            ticker: sorted(set(reasons))
            for ticker, reasons in sorted(invalid.items())
        },
        "rule": (
            "Causal order=(recv_wall_ns,recv_mono_ns,ws_seq); snapshot "
            "replaces, signed delta adds; any missing/ambiguous clock or "
            "semantics, delta-before-snapshot, or negative quantity rejects "
            "the full market stream. No clamping or repair."
        ),
    }


def clip_probability(value: float) -> float:
    return max(EPS, min(1.0 - EPS, float(value)))


def logistic_calibration(
    probabilities: list[float],
    outcomes: list[int],
) -> dict[str, Any]:
    if len(probabilities) < 3 or len(set(outcomes)) < 2:
        return {"available": False, "reason": "n<3_or_single_outcome"}
    predictors = [
        math.log(
            clip_probability(p) / (1.0 - clip_probability(p))
        )
        for p in probabilities
    ]
    intercept = 0.0
    slope = 1.0
    for _iteration in range(50):
        gradient_0 = gradient_1 = 0.0
        hessian_00 = hessian_01 = hessian_11 = 0.0
        for predictor, outcome in zip(predictors, outcomes):
            linear = max(
                -35.0,
                min(35.0, intercept + slope * predictor),
            )
            mean = 1.0 / (1.0 + math.exp(-linear))
            weight = max(mean * (1.0 - mean), 1e-12)
            residual = outcome - mean
            gradient_0 += residual
            gradient_1 += residual * predictor
            hessian_00 += weight
            hessian_01 += weight * predictor
            hessian_11 += weight * predictor * predictor
        determinant = (
            hessian_00 * hessian_11 - hessian_01 * hessian_01
        )
        if determinant <= 1e-12:
            return {
                "available": False,
                "reason": "singular_or_separated",
            }
        delta_intercept = (
            gradient_0 * hessian_11 - gradient_1 * hessian_01
        ) / determinant
        delta_slope = (
            gradient_1 * hessian_00 - gradient_0 * hessian_01
        ) / determinant
        intercept += delta_intercept
        slope += delta_slope
        if not math.isfinite(intercept) or not math.isfinite(slope):
            return {"available": False, "reason": "nonfinite_fit"}
        if max(abs(delta_intercept), abs(delta_slope)) < 1e-9:
            return {
                "available": True,
                "intercept": intercept,
                "slope": slope,
            }
    return {"available": False, "reason": "irls_nonconvergence"}


def point_metrics(
    rows: list[dict[str, Any]],
    forecast_key: str,
) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "markets": 0}
    probabilities = [
        clip_probability(row[forecast_key]) for row in rows
    ]
    outcomes = [int(row["outcome"]) for row in rows]
    return {
        "n": len(rows),
        "markets": len({row["market"] for row in rows}),
        "outcome_rate": statistics.fmean(outcomes),
        "brier": statistics.fmean(
            (probability - outcome) ** 2
            for probability, outcome in zip(probabilities, outcomes)
        ),
        "logloss": statistics.fmean(
            -(
                outcome * math.log(probability)
                + (1 - outcome) * math.log(1.0 - probability)
            )
            for probability, outcome in zip(probabilities, outcomes)
        ),
        "calibration": logistic_calibration(probabilities, outcomes),
    }


def quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return (
        ordered[lower]
        + (ordered[upper] - ordered[lower]) * (position - lower)
    )


def cluster_bootstrap_samples(
    rows: list[dict[str, Any]],
    seed: int = BOOT_SEED,
):
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_market[row["market"]].append(row)
    markets = sorted(by_market)
    generator = random.Random(seed)
    for _replicate in range(BOOT_REPS):
        sample = []
        for _market in markets:
            sample.extend(by_market[generator.choice(markets)])
        yield sample


def metrics_with_uncertainty(
    rows: list[dict[str, Any]],
    forecast_key: str,
) -> dict[str, Any]:
    point = point_metrics(rows, forecast_key)
    if not rows:
        return point
    bootstraps = [
        point_metrics(sample, forecast_key)
        for sample in cluster_bootstrap_samples(rows)
    ]
    point["ci95"] = {}
    for key in ("brier", "logloss"):
        values = [
            float(result[key])
            for result in bootstraps
            if result.get(key) is not None
        ]
        point["ci95"][key] = [
            quantile(values, 0.025),
            quantile(values, 0.975),
        ]
    for key in ("intercept", "slope"):
        values = [
            float(result["calibration"][key])
            for result in bootstraps
            if result.get("calibration", {}).get("available")
        ]
        point["ci95"][f"calibration_{key}"] = [
            quantile(values, 0.025),
            quantile(values, 0.975),
        ]
        point["ci95"][f"calibration_{key}_available_replicates"] = len(
            values
        )
    return point


def paired_point(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "markets": 0}
    kernel = point_metrics(rows, "kernel_p")
    market = point_metrics(rows, "market_p")
    return {
        "n": len(rows),
        "markets": len({row["market"] for row in rows}),
        "kernel_minus_market_brier": (
            kernel["brier"] - market["brier"]
        ),
        "kernel_minus_market_logloss": (
            kernel["logloss"] - market["logloss"]
        ),
    }


def paired_with_uncertainty(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    paired = [row for row in rows if row.get("market_p") is not None]
    point = paired_point(paired)
    if not paired:
        return point
    bootstraps = [
        paired_point(sample)
        for sample in cluster_bootstrap_samples(paired)
    ]
    for key in (
        "kernel_minus_market_brier",
        "kernel_minus_market_logloss",
    ):
        values = [float(result[key]) for result in bootstraps]
        point[f"{key}_ci95"] = [
            quantile(values, 0.025),
            quantile(values, 0.975),
        ]
    return point


def build_calibration_rows(
    metadata: dict[str, dict[str, Any]],
    times: list[int],
    values: list[float],
    book_samples: dict[str, dict[int, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows = []
    excluded = Counter()
    for ticker, meta in sorted(metadata.items()):
        for horizon_s in HORIZONS_S:
            target_ms = meta["close_ms"] - horizon_s * 1000
            kernel = kernel_at(
                times,
                values,
                target_ms,
                meta["close_ms"],
                meta["strike"],
            )
            if kernel is None:
                excluded["kernel_unavailable"] += 1
                continue
            book = book_samples.get(ticker, {}).get(horizon_s)
            if book is None:
                excluded["market_mid_unavailable"] += 1
            rows.append({
                "market": ticker,
                "close_date": meta["close_date"],
                "horizon_s": horizon_s,
                "outcome": meta["outcome"],
                "kernel_p": kernel["probability"],
                "market_p": (
                    book["probability"] if book is not None else None
                ),
                "kernel_sigma60": kernel["sigma60"],
                "kernel_sigma300": kernel["sigma300"],
                "kernel_sigma": kernel["sigma"],
                "kernel_model_tte_s": kernel["model_tte_s"],
                "kernel_information_age_s": kernel["information_age_s"],
                "market_mid_age_s": (
                    book["age_s"] if book is not None else None
                ),
            })
    return rows, dict(sorted(excluded.items()))


def calibration_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    paired = [row for row in rows if row.get("market_p") is not None]
    all_horizons = {
        "kernel": metrics_with_uncertainty(rows, "kernel_p"),
        "market": metrics_with_uncertainty(paired, "market_p"),
        "paired_difference": paired_with_uncertainty(rows),
    }
    by_horizon = {}
    for horizon in HORIZONS_S:
        horizon_rows = [
            row for row in rows if row["horizon_s"] == horizon
        ]
        horizon_paired = [
            row for row in horizon_rows
            if row.get("market_p") is not None
        ]
        by_horizon[str(horizon)] = {
            "kernel": metrics_with_uncertainty(
                horizon_rows, "kernel_p"
            ),
            "market": metrics_with_uncertainty(
                horizon_paired, "market_p"
            ),
            "paired_difference": paired_with_uncertainty(
                horizon_rows
            ),
        }
    unique_markets = len({row["market"] for row in rows})
    outcome_values = {row["outcome"] for row in rows}
    return {
        "minimum_reportable_satisfied": (
            unique_markets >= 20 and len(outcome_values) == 2
        ),
        "unique_markets": unique_markets,
        "outcomes_present": sorted(outcome_values),
        "bootstrap": {
            "unit": "market cluster",
            "replicates": BOOT_REPS,
            "seed": BOOT_SEED,
            "interval": "percentile_95",
        },
        "all_horizons": all_horizons,
        "by_horizon_s": by_horizon,
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


def render_markdown(report: dict[str, Any]) -> str:
    calibration = report["calibration"]
    pooled = calibration["all_horizons"]
    paired = pooled["paired_difference"]
    status = report["status"]
    lines = [
        "# Authoritative BRTI direction-pricing calibration",
        "",
        f"Status: **{status}** (discovery only).",
        "",
        f"- Settlement-crosschecked markets: "
        f"`{report['settlement_crosscheck']['counts'].get('verified_market_n', 0)}`",
        f"- Calibration markets: `{calibration['unique_markets']}`",
        f"- Kernel Brier: `{pooled['kernel'].get('brier')}`",
        f"- Market-mid Brier: `{pooled['market'].get('brier')}`",
        f"- Kernel minus market Brier: "
        f"`{paired.get('kernel_minus_market_brier')}`",
        f"- Kernel log loss: `{pooled['kernel'].get('logloss')}`",
        f"- Market-mid log loss: `{pooled['market'].get('logloss')}`",
        f"- Kernel minus market log loss: "
        f"`{paired.get('kernel_minus_market_logloss')}`",
        "",
        "Negative paired differences favor the kernel; positive differences "
        "favor market mid. Confidence intervals and per-horizon results are "
        "in the JSON receipt.",
        "",
        "This audit identifies direction calibration only. Historical CF "
        "rows do not contain the original Kalshi receipt clock, so causal "
        "1/2/5/10-second fair-to-mid markouts remain blocked. Existing P3 "
        "episodes use the superseded replay clock and lack first side/price, "
        "so they remain blocked as well.",
        "",
        "No result here authorizes trading or parameter changes.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", required=True)
    parser.add_argument("--seal", required=True)
    parser.add_argument("--downloader", required=True)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--acquisition-receipt", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--md-out", required=True)
    args = parser.parse_args()

    script = Path(__file__).resolve()
    prereg = Path(args.prereg).resolve()
    seal = Path(args.seal).resolve()
    downloader = Path(args.downloader).resolve()
    source_dir = Path(args.source_dir).resolve()
    acquisition_receipt = Path(args.acquisition_receipt).resolve()
    output_path = Path(args.out).resolve()
    markdown_path = Path(args.md_out).resolve()

    seal_info = verify_seal(script, downloader, prereg, seal)
    times, values, history_audit = load_authoritative_history(
        source_dir,
        acquisition_receipt,
        seal_info,
    )
    metadata, metadata_audit = load_metadata()
    verified_metadata, settlement_audit = settlement_crosscheck(
        metadata, times, values
    )
    book_paths = exact_book_paths()
    book_samples, book_replay = load_book_samples(
        book_paths, verified_metadata
    )
    rows, excluded = build_calibration_rows(
        verified_metadata,
        times,
        values,
        book_samples,
    )
    metrics = calibration_report(rows)
    status = (
        "DISCOVERY_RESULT"
        if metrics["minimum_reportable_satisfied"]
        else "DESCRIPTIVE_ONLY_INSUFFICIENT_MARKETS_OR_OUTCOMES"
    )
    report = {
        "schema": "brti-authoritative-calibration-v1",
        "status": status,
        "claim": "DISCOVERY_ONLY_NOT_VALIDATION",
        "read_only": True,
        "trading_action_performed": False,
        "engine_or_service_mutation_performed": False,
        "hard_banned_history_requested_or_opened": False,
        "seal": seal_info,
        "sources": {
            "authoritative_brti": history_audit,
            "market_metadata": metadata_audit,
            "orderbooks": file_manifest(
                path
                for date in ALLOWED_DATES
                for path in book_paths[date]
            ),
            "book_paths_by_date": book_paths,
            "book_replay": book_replay,
        },
        "settlement_crosscheck": settlement_audit,
        "calibration": metrics,
        "calibration_exclusions": excluded,
        "rows": rows,
        "blocked_secondary_tests": {
            "fair_mid_gap_to_future_mid": {
                "status": "BLOCKED_BRTI_RECEIVE_TIMESTAMP_ABSENT",
                "reason": (
                    "Historical CF rows have source time/value but not their "
                    "original Kalshi receipt clock; source time cannot be "
                    "mixed with book receive time for causal markouts."
                ),
            },
            "p3_first_fill_oof": {
                "status": "BLOCKED_EXISTING_P3_CLOCK_AND_SCHEMA",
                "reason": (
                    "Existing P3 episodes use the superseded ts_utc/ws_seq "
                    "replay and omit first_side/first_price."
                ),
            },
        },
        "interpretation_boundary": {
            "calibration": (
                "Brier, log loss, calibration intercept/slope, and the "
                "paired market-mid comparison test direction pricing."
            ),
            "execution_completion": (
                "Queue position, fill toxicity, opposite-leg completion, "
                "fees, forced exits, and realized PnL are not identified."
            ),
            "deployment_authorized": False,
        },
    }
    report = rounded(report)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")


if __name__ == "__main__":
    main()
