#!/usr/bin/env python3
"""Sealed calibration wrapper for exact hourly authoritative BRTI files."""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
from typing import Any


BANNED_START = "2026-07-23T00:00:00.000Z"
FIRST_HOUR = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)


def iso_z(value: dt.datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


HOUR_STARTS = tuple(
    iso_z(FIRST_HOUR + dt.timedelta(hours=offset))
    for offset in range(72)
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_ms(text: str) -> int:
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"naive timestamp: {text}")
    return round(parsed.timestamp() * 1000)


BANNED_START_MS = utc_ms(BANNED_START)


def load_base_analyzer(path: Path):
    specification = importlib.util.spec_from_file_location(
        "sealed_base_calibration_analyzer", path
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load sealed base analyzer")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def verify_seal(
    script: Path,
    downloader: Path,
    base_analyzer: Path,
    prereg: Path,
    seal: Path,
) -> dict[str, Any]:
    frozen = json.loads(seal.read_text(encoding="utf-8"))
    observed = {
        "analyzer_sha256": sha256_path(script),
        "downloader_sha256": sha256_path(downloader),
        "base_analyzer_sha256": sha256_path(base_analyzer),
        "preregistration_sha256": sha256_path(prereg),
    }
    for key, value in observed.items():
        if frozen.get(key) != value:
            raise RuntimeError(f"seal mismatch for {key}: observed {value}")
    specification = json.loads(prereg.read_text(encoding="utf-8"))
    if specification.get("hour_starts_utc") != list(HOUR_STARTS):
        raise RuntimeError("pre-registration hour list mismatch")
    if (
        specification.get("download", {}).get(
            "hard_banned_start_utc"
        )
        != BANNED_START
    ):
        raise RuntimeError("pre-registration banned-start mismatch")
    return {
        **observed,
        "seal_sha256": sha256_path(seal),
        "verified": True,
    }


def parse_hour(
    body: bytes,
    hour_start: str,
) -> tuple[list[tuple[int, float]], dict[str, Any]]:
    envelope = json.loads(body)
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError("invalid/error Kalshi CF passthrough envelope")
    payload = data.get("payload")
    if not isinstance(payload, list) or len(payload) != 3_600:
        raise RuntimeError("hour payload is not exactly 3600 rows")
    start_ms = utc_ms(hour_start)
    rows = []
    for row_number, row in enumerate(payload):
        if not isinstance(row, dict):
            raise RuntimeError(f"payload row {row_number} is not an object")
        raw_time = row.get("time")
        if isinstance(raw_time, bool) or not isinstance(raw_time, int):
            raise RuntimeError(f"payload row {row_number} has invalid time")
        source_ms = int(raw_time)
        if source_ms != start_ms + row_number * 1000:
            raise RuntimeError(
                f"payload row {row_number} violates exact second grid"
            )
        if source_ms >= BANNED_START_MS:
            raise RuntimeError("hour reaches hard-banned start")
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
    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return rows, {
        "hour_start_utc": hour_start,
        "row_count": len(rows),
        "minimum_source_ms": rows[0][0],
        "maximum_source_ms": rows[-1][0],
        "payload_sha256": sha256_bytes(canonical),
        "exact_second_grid": True,
        "duplicate_source_timestamps": 0,
    }


def load_hourly_history(
    source_dir: Path,
    acquisition_receipt: Path,
    seal_info: dict[str, Any],
    base: Any,
) -> tuple[list[int], list[float], dict[str, Any]]:
    receipt_bytes = acquisition_receipt.read_bytes()
    receipt = json.loads(receipt_bytes)
    if (
        receipt.get("status")
        != "COMPLETE_AUTHORITATIVE_HOURLY_HISTORY_ACQUIRED"
    ):
        raise RuntimeError("hourly acquisition receipt is not complete")
    fixed_receipt_fields = {
        "logical_request_count": 72,
        "maximum_attempts_per_hour": 2,
        "network_errors_retryable": False,
        "redirect_count": 0,
        "maximum_configured_concurrency": 2,
        "minimum_attempt_start_interval_ms": 500,
    }
    for key, expected in fixed_receipt_fields.items():
        if receipt.get(key) != expected:
            raise RuntimeError(f"acquisition receipt mismatch: {key}")
    if receipt.get("retryable_http_statuses_only") != [429, 503]:
        raise RuntimeError("acquisition retry-status mismatch")
    if receipt.get("hard_banned_history_requested") is not False:
        raise RuntimeError("acquisition banned-history guard missing")
    receipt_seal = receipt.get("seal") or {}
    for key in (
        "analyzer_sha256",
        "downloader_sha256",
        "base_analyzer_sha256",
        "preregistration_sha256",
        "seal_sha256",
    ):
        if receipt_seal.get(key) != seal_info.get(key):
            raise RuntimeError(f"acquisition seal mismatch: {key}")
    hour_receipts = {
        row.get("hour_start_utc"): row
        for row in (receipt.get("hours") or [])
    }
    if set(hour_receipts) != set(HOUR_STARTS):
        raise RuntimeError("acquisition receipt hour set mismatch")

    source_paths = []
    hour_audits = []
    times: list[int] = []
    values: list[float] = []
    retry_count = 0
    for hour_start in HOUR_STARTS:
        logical = hour_receipts[hour_start]
        if logical.get("status") != "SUCCESS":
            raise RuntimeError(f"unsuccessful hour {hour_start}")
        attempts = logical.get("attempts") or []
        if len(attempts) not in (1, 2):
            raise RuntimeError(f"invalid attempt count for {hour_start}")
        if len(attempts) == 2:
            retry_count += 1
            if attempts[0].get("http_status") not in (429, 503):
                raise RuntimeError(f"illegal retry for {hour_start}")
            if attempts[0].get("retry_backoff_ms") != 3_000:
                raise RuntimeError(f"retry backoff mismatch for {hour_start}")
        successful_attempt = attempts[-1]
        if successful_attempt.get("http_status") != 200:
            raise RuntimeError(f"final attempt not 200 for {hour_start}")
        query = successful_attempt.get("query") or {}
        if query != {
            "id": "BRTI",
            "timespan": "HOUR",
            "timestamp": hour_start,
        }:
            raise RuntimeError(f"query mismatch for {hour_start}")

        hour_label = hour_start[:13].replace("T", "_")
        path = source_dir / f"brti_history_{hour_label}.json.gz"
        compressed = path.read_bytes()
        if sha256_bytes(compressed) != logical.get("stored_sha256"):
            raise RuntimeError(f"stored gzip hash mismatch for {hour_start}")
        body = gzip.decompress(compressed)
        if sha256_bytes(body) != successful_attempt.get("body_sha256"):
            raise RuntimeError(f"body hash mismatch for {hour_start}")
        rows, audit = parse_hour(body, hour_start)
        if audit["payload_sha256"] != logical.get("payload_sha256"):
            raise RuntimeError(f"payload hash mismatch for {hour_start}")
        if audit["row_count"] != logical.get("payload_count"):
            raise RuntimeError(f"payload count mismatch for {hour_start}")
        times.extend(row[0] for row in rows)
        values.extend(row[1] for row in rows)
        source_paths.append(path)
        hour_audits.append(audit)
    if len(times) != 72 * 3_600 or len(set(times)) != len(times):
        raise RuntimeError("combined history is missing or duplicate")
    expected_times = [
        utc_ms(HOUR_STARTS[0]) + second * 1000
        for second in range(72 * 3_600)
    ]
    if times != expected_times:
        raise RuntimeError("combined history is not exact three-day grid")
    return times, values, {
        "acquisition_receipt": {
            "path": str(acquisition_receipt),
            "bytes": len(receipt_bytes),
            "sha256": sha256_bytes(receipt_bytes),
        },
        "raw_hourly_envelopes": base.file_manifest(source_paths),
        "hour_count": len(hour_audits),
        "tick_count": len(times),
        "retried_hour_count": retry_count,
        "minimum_source_ms": times[0],
        "maximum_source_ms": times[-1],
        "all_hours_exact_second_grid": True,
        "duplicate_source_timestamps": 0,
        "individual_values_in_report": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", required=True)
    parser.add_argument("--seal", required=True)
    parser.add_argument("--downloader", required=True)
    parser.add_argument("--base-analyzer", required=True)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--acquisition-receipt", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--md-out", required=True)
    args = parser.parse_args()

    script = Path(__file__).resolve()
    prereg = Path(args.prereg).resolve()
    seal = Path(args.seal).resolve()
    downloader = Path(args.downloader).resolve()
    base_analyzer_path = Path(args.base_analyzer).resolve()
    source_dir = Path(args.source_dir).resolve()
    acquisition_receipt = Path(args.acquisition_receipt).resolve()
    output_path = Path(args.out).resolve()
    markdown_path = Path(args.md_out).resolve()

    seal_info = verify_seal(
        script,
        downloader,
        base_analyzer_path,
        prereg,
        seal,
    )
    base = load_base_analyzer(base_analyzer_path)
    times, values, history_audit = load_hourly_history(
        source_dir,
        acquisition_receipt,
        seal_info,
        base,
    )
    metadata, metadata_audit = base.load_metadata()
    verified_metadata, settlement_audit = base.settlement_crosscheck(
        metadata, times, values
    )
    book_paths = base.exact_book_paths()
    book_samples, book_replay = base.load_book_samples(
        book_paths, verified_metadata
    )
    rows, excluded = base.build_calibration_rows(
        verified_metadata,
        times,
        values,
        book_samples,
    )
    metrics = base.calibration_report(rows)
    status = (
        "DISCOVERY_RESULT"
        if metrics["minimum_reportable_satisfied"]
        else "DESCRIPTIVE_ONLY_INSUFFICIENT_MARKETS_OR_OUTCOMES"
    )
    report = {
        "schema": "brti-authoritative-calibration-v2-hourly",
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
            "orderbooks": base.file_manifest(
                path
                for date in base.ALLOWED_DATES
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
    report = base.rounded(report)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        base.render_markdown(report), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
