#!/usr/bin/env python3
"""Sealed V3 BRTI acquisition with timestamp-boundary projection."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import time
from typing import Any


BASE_DOWNLOADER_SHA256 = (
    "31a1d08cfee96afa71d5201950c75642823ceacbd94420ec47092ba5b795b8f1"
)
BASE_HOURLY_ANALYZER_SHA256 = (
    "a2d2dc54c2ae7e4c3079dd0cc2aea93b364b08e70366cf5f0ec936714744d89b"
)
BASE_CALIBRATION_ANALYZER_SHA256 = (
    "4a127b6307dc8566b166ec58af7282fd4aec39d44f4615fe396c1d06336991bd"
)
BANNED_START_MS = 1_784_764_800_000


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_module(path: Path, name: str):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load sealed module {name}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def verify_seal(
    script: Path,
    analyzer: Path,
    base_downloader: Path,
    base_hourly_analyzer: Path,
    base_calibration_analyzer: Path,
    prereg: Path,
    seal: Path,
) -> dict[str, Any]:
    frozen = json.loads(seal.read_text(encoding="utf-8"))
    observed = {
        "downloader_sha256": sha256_path(script),
        "analyzer_sha256": sha256_path(analyzer),
        "base_downloader_sha256": sha256_path(base_downloader),
        "base_hourly_analyzer_sha256": sha256_path(
            base_hourly_analyzer
        ),
        "base_calibration_analyzer_sha256": sha256_path(
            base_calibration_analyzer
        ),
        "preregistration_sha256": sha256_path(prereg),
    }
    for key, value in observed.items():
        if frozen.get(key) != value:
            raise RuntimeError(f"seal mismatch for {key}: observed {value}")
    if observed["base_downloader_sha256"] != BASE_DOWNLOADER_SHA256:
        raise RuntimeError("embedded base downloader hash mismatch")
    if (
        observed["base_hourly_analyzer_sha256"]
        != BASE_HOURLY_ANALYZER_SHA256
    ):
        raise RuntimeError("embedded base hourly analyzer hash mismatch")
    if (
        observed["base_calibration_analyzer_sha256"]
        != BASE_CALIBRATION_ANALYZER_SHA256
    ):
        raise RuntimeError("embedded calibration analyzer hash mismatch")
    specification = json.loads(prereg.read_text(encoding="utf-8"))
    download = specification.get("download") or {}
    required = {
        "logical_request_count": 72,
        "maximum_concurrency": 2,
        "global_minimum_attempt_start_interval_ms": 500,
        "timeout_seconds_each_attempt": 15,
        "maximum_attempts_per_hour": 2,
        "retry_backoff_ms": 3000,
        "hard_banned_start_utc": "2026-07-23T00:00:00.000Z",
    }
    for key, expected in required.items():
        if download.get(key) != expected:
            raise RuntimeError(f"pre-registration mismatch: {key}")
    if download.get("retryable_http_statuses_only") != [429, 503]:
        raise RuntimeError("retryable status mismatch")
    official = specification.get("official_contract") or {}
    if official.get("historical_max_resolution_parameter_supported") is not False:
        raise RuntimeError("historical resolution contract mismatch")
    if (
        official.get("projection_rule")
        != "Select a historical row if and only if time_ms modulo 1000 equals zero. Never select every fifth row or use row position."
    ):
        raise RuntimeError("projection contract mismatch")
    return {
        **observed,
        "seal_sha256": sha256_path(seal),
        "verified": True,
    }


def validate_raw_hour(
    body: bytes,
    hour_start: str,
    base: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        envelope = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("response is not JSON") from exc
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("invalid Kalshi passthrough envelope")
    if data.get("error"):
        raise RuntimeError("CF upstream returned an error field")
    payload = data.get("payload")
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("CF payload is not a nonempty list")
    start_ms = base.utc_ms(hour_start)
    end_ms = start_ms + 3_600_000
    previous_time = None
    for row_number, row in enumerate(payload):
        if not isinstance(row, dict):
            raise RuntimeError(f"payload row {row_number} is not an object")
        raw_time = row.get("time")
        if isinstance(raw_time, bool) or not isinstance(raw_time, int):
            raise RuntimeError(
                f"payload row {row_number} has non-integer time"
            )
        source_ms = int(raw_time)
        if not start_ms <= source_ms < end_ms:
            raise RuntimeError(
                f"payload row {row_number} outside requested hour"
            )
        if source_ms >= BANNED_START_MS:
            raise RuntimeError("payload reaches hard-banned start")
        if previous_time is not None and source_ms <= previous_time:
            raise RuntimeError(
                "raw source times are duplicate or non-ascending"
            )
        if source_ms % 200 != 0:
            raise RuntimeError(
                f"payload row {row_number} is off the 200ms grid"
            )
        try:
            value = float(row.get("value"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"payload row {row_number} has non-numeric value"
            ) from exc
        if not math.isfinite(value) or value <= 0:
            raise RuntimeError(
                f"payload row {row_number} has invalid numeric value"
            )
        previous_time = source_ms
    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return payload, {
        "payload_count": len(payload),
        "payload_sha256": sha256_bytes(canonical),
        "minimum_source_ms": int(payload[0]["time"]),
        "maximum_source_ms": int(payload[-1]["time"]),
        "raw_times_strictly_ascending_unique": True,
        "raw_times_on_200ms_grid": True,
    }


def validate_second_boundary_projection(
    payload: list[dict[str, Any]],
    hour_start: str,
    base: Any,
) -> dict[str, Any]:
    start_ms = base.utc_ms(hour_start)
    projected_times = [
        int(row["time"])
        for row in payload
        if int(row["time"]) % 1000 == 0
    ]
    expected_times = [
        start_ms + second * 1000 for second in range(3_600)
    ]
    if projected_times != expected_times:
        missing = len(set(expected_times) - set(projected_times))
        extra = len(set(projected_times) - set(expected_times))
        duplicates = len(projected_times) - len(set(projected_times))
        raise RuntimeError(
            "one-second boundary projection mismatch: "
            f"count={len(projected_times)} missing={missing} "
            f"extra={extra} duplicates={duplicates}"
        )
    return {
        "projection_predicate": "time_ms % 1000 == 0",
        "projected_second_count": len(projected_times),
        "projected_minimum_source_ms": projected_times[0],
        "projected_maximum_source_ms": projected_times[-1],
        "projected_exact_second_grid": True,
        "projected_missing_seconds": 0,
        "projected_duplicate_seconds": 0,
    }


def acquire_hour(
    hour_start: str,
    out_dir: Path,
    key_id: str,
    private_key: Any,
    limiter: Any,
    concurrency: Any,
    base: Any,
) -> dict[str, Any]:
    logical: dict[str, Any] = {
        "hour_start_utc": hour_start,
        "status": "IN_PROGRESS",
        "attempts": [],
        "persisted": False,
    }
    for attempt_number in range(1, base.MAX_ATTEMPTS + 1):
        body, attempt = base.one_attempt(
            hour_start,
            attempt_number,
            key_id,
            private_key,
            limiter,
            concurrency,
        )
        logical["attempts"].append(attempt)
        status = attempt.get("http_status")
        if status == 200 and body is not None:
            try:
                payload, raw_summary = validate_raw_hour(
                    body, hour_start, base
                )
            except Exception as exc:
                attempt["validation_error_class"] = type(exc).__name__
                attempt["validation_error"] = str(exc)[:500]
                logical["status"] = "FAILED_RAW_VALIDATION"
                return logical
            compressed = gzip.compress(body, compresslevel=9, mtime=0)
            hour_label = hour_start[:13].replace("T", "_")
            destination = out_dir / (
                f"brti_history_{hour_label}.json.gz"
            )
            destination.write_bytes(compressed)
            logical.update({
                "persisted": True,
                "stored_path": str(destination),
                "stored_bytes": len(compressed),
                "stored_sha256": sha256_bytes(compressed),
                **raw_summary,
            })
            attempt["validation"] = (
                "RAW_AUTHORITATIVE_ENVELOPE_PERSISTED"
            )
            try:
                projection = validate_second_boundary_projection(
                    payload, hour_start, base
                )
            except Exception as exc:
                attempt["projection_error_class"] = type(exc).__name__
                attempt["projection_error"] = str(exc)[:500]
                logical["status"] = "FAILED_SECOND_BOUNDARY_PROJECTION"
                return logical
            logical.update(projection)
            logical["status"] = "SUCCESS"
            attempt["projection_validation"] = (
                "EXACT_3600_SECOND_BOUNDARIES"
            )
            return logical
        if (
            attempt_number == 1
            and status in base.RETRY_STATUSES
        ):
            attempt["retry_scheduled"] = True
            attempt["retry_backoff_ms"] = round(
                base.RETRY_BACKOFF_S * 1000
            )
            time.sleep(base.RETRY_BACKOFF_S)
            continue
        attempt["retry_scheduled"] = False
        logical["status"] = (
            "FAILED_NETWORK"
            if status is None else f"FAILED_HTTP_{status}"
        )
        return logical
    logical["status"] = "FAILED_ATTEMPT_LIMIT"
    return logical


def render_markdown(report: dict[str, Any]) -> str:
    counts = Counter(row["status"] for row in report["hours"])
    return "\n".join([
        "# Authoritative BRTI V3 boundary-projection acquisition",
        "",
        f"Status: **{report['status']}**.",
        "",
        f"- Logical hours: `{len(report['hours'])}`",
        f"- Successful hours: `{counts.get('SUCCESS', 0)}`",
        f"- Raw envelopes persisted: "
        f"`{report['raw_persisted_hour_count']}`",
        f"- Attempts: `{report['attempt_count']}`",
        f"- Retried hours: `{report['retried_hour_count']}`",
        f"- Maximum observed concurrency: "
        f"`{report['maximum_observed_concurrency']}`",
        "",
        "Complete subsecond envelopes are hashed and retained. The one-second "
        "series is selected only by `time_ms % 1000 == 0`; no row-position "
        "sampling is used. Individual BRTI values are absent from this "
        "receipt.",
        "",
    ])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", required=True)
    parser.add_argument("--seal", required=True)
    parser.add_argument("--analyzer", required=True)
    parser.add_argument("--base-downloader", required=True)
    parser.add_argument("--base-hourly-analyzer", required=True)
    parser.add_argument("--base-calibration-analyzer", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--md-out", required=True)
    args = parser.parse_args()

    script = Path(__file__).resolve()
    prereg = Path(args.prereg).resolve()
    seal = Path(args.seal).resolve()
    analyzer = Path(args.analyzer).resolve()
    base_downloader_path = Path(args.base_downloader).resolve()
    base_hourly_analyzer = Path(args.base_hourly_analyzer).resolve()
    base_calibration_analyzer = Path(
        args.base_calibration_analyzer
    ).resolve()
    out_dir = Path(args.out_dir).resolve()
    receipt_path = Path(args.receipt).resolve()
    markdown_path = Path(args.md_out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    seal_info = verify_seal(
        script,
        analyzer,
        base_downloader_path,
        base_hourly_analyzer,
        base_calibration_analyzer,
        prereg,
        seal,
    )
    base = load_module(
        base_downloader_path, "sealed_v2_hourly_downloader_base"
    )
    if tuple(base.HOUR_STARTS) != tuple(
        base.iso_z(base.FIRST_HOUR + base.dt.timedelta(hours=offset))
        for offset in range(72)
    ):
        raise RuntimeError("base downloader hour list mismatch")
    key_id, private_key = base.load_auth_material()
    run_started = time.monotonic()
    limiter = base.AttemptStartLimiter(
        base.MIN_ATTEMPT_START_INTERVAL_S, run_started
    )
    concurrency = base.ConcurrencyAudit()
    results = []
    with ThreadPoolExecutor(
        max_workers=base.MAX_CONCURRENCY
    ) as executor:
        future_to_hour = {
            executor.submit(
                acquire_hour,
                hour_start,
                out_dir,
                key_id,
                private_key,
                limiter,
                concurrency,
                base,
            ): hour_start
            for hour_start in base.HOUR_STARTS
        }
        for future in as_completed(future_to_hour):
            hour_start = future_to_hour[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({
                    "hour_start_utc": hour_start,
                    "status": "FAILED_INTERNAL",
                    "internal_error_class": type(exc).__name__,
                    "persisted": False,
                    "attempts": [],
                })
    results.sort(key=lambda row: row["hour_start_utc"])
    successful = [row for row in results if row["status"] == "SUCCESS"]
    report = {
        "schema": "brti-authoritative-boundary-acquisition-v3",
        "status": (
            "COMPLETE_AUTHORITATIVE_HOURLY_HISTORY_ACQUIRED"
            if len(successful) == len(base.HOUR_STARTS)
            else "NO_DECISION_BOUNDARY_ACQUISITION_FAILED"
        ),
        "read_only": True,
        "trading_action_performed": False,
        "engine_or_service_mutation_performed": False,
        "recent_values_endpoint_called": False,
        "hard_banned_history_requested": False,
        "logical_request_count": len(base.HOUR_STARTS),
        "attempt_count": sum(
            len(row["attempts"]) for row in results
        ),
        "retried_hour_count": sum(
            len(row["attempts"]) == 2 for row in results
        ),
        "raw_persisted_hour_count": sum(
            bool(row.get("persisted")) for row in results
        ),
        "maximum_attempts_per_hour": base.MAX_ATTEMPTS,
        "retryable_http_statuses_only": list(base.RETRY_STATUSES),
        "network_errors_retryable": False,
        "redirect_count": 0,
        "maximum_configured_concurrency": base.MAX_CONCURRENCY,
        "maximum_observed_concurrency": concurrency.maximum,
        "minimum_attempt_start_interval_ms": round(
            base.MIN_ATTEMPT_START_INTERVAL_S * 1000
        ),
        "projection_predicate": "time_ms % 1000 == 0",
        "elapsed_ms": round(
            (time.monotonic() - run_started) * 1000, 3
        ),
        "seal": seal_info,
        "hours": results,
    }
    receipt_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    if (
        report["status"]
        != "COMPLETE_AUTHORITATIVE_HOURLY_HISTORY_ACQUIRED"
    ):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
