#!/usr/bin/env python3
"""Sealed bounded acquisition of 72 authoritative BRTI UTC hours."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


HOST = "https://external-api.kalshi.com"
SIGNED_PATH = "/trade-api/v2/cfbenchmarks/history/values"
INDEX_ID = "BRTI"
TIMESPAN = "HOUR"
BANNED_START = "2026-07-23T00:00:00.000Z"
TIMEOUT_S = 15
MAX_RESPONSE_BYTES = 8_388_608
MAX_CONCURRENCY = 2
MIN_ATTEMPT_START_INTERVAL_S = 0.500
MAX_ATTEMPTS = 2
RETRY_STATUSES = (429, 503)
RETRY_BACKOFF_S = 3.000


def iso_z(value: dt.datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


FIRST_HOUR = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)
HOUR_STARTS = tuple(
    iso_z(FIRST_HOUR + dt.timedelta(hours=offset))
    for offset in range(72)
)
CANONICAL_HOUR_LIST_SHA256 = (
    "a0e13435ce150f9741d4897d0764212dcd22efacac652d52810791276e630556"
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
        raise ValueError(f"naive UTC timestamp: {text}")
    return round(parsed.timestamp() * 1000)


BANNED_START_MS = utc_ms(BANNED_START)


def canonical_hour_list_sha256(hours: tuple[str, ...]) -> str:
    encoded = json.dumps(
        list(hours), separators=(",", ":")
    ).encode()
    return sha256_bytes(encoded)


def verify_seal(
    script: Path,
    analyzer: Path,
    base_analyzer: Path,
    prereg: Path,
    seal: Path,
) -> dict[str, Any]:
    frozen = json.loads(seal.read_text(encoding="utf-8"))
    observed = {
        "downloader_sha256": sha256_path(script),
        "analyzer_sha256": sha256_path(analyzer),
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
            "canonical_hour_list_sha256"
        )
        != CANONICAL_HOUR_LIST_SHA256
    ):
        raise RuntimeError("pre-registration hour-list hash mismatch")
    if (
        canonical_hour_list_sha256(HOUR_STARTS)
        != CANONICAL_HOUR_LIST_SHA256
    ):
        raise RuntimeError("embedded hour-list hash mismatch")
    download = specification.get("download") or {}
    required = {
        "hard_banned_start_utc": BANNED_START,
        "logical_request_count": len(HOUR_STARTS),
        "maximum_concurrency": MAX_CONCURRENCY,
        "global_minimum_attempt_start_interval_ms": round(
            MIN_ATTEMPT_START_INTERVAL_S * 1000
        ),
        "timeout_seconds_each_attempt": TIMEOUT_S,
        "maximum_attempts_per_hour": MAX_ATTEMPTS,
        "retry_backoff_ms": round(RETRY_BACKOFF_S * 1000),
    }
    for key, expected in required.items():
        if download.get(key) != expected:
            raise RuntimeError(f"pre-registration mismatch: {key}")
    if download.get("retryable_http_statuses_only") != list(
        RETRY_STATUSES
    ):
        raise RuntimeError("pre-registration retry status mismatch")
    if download.get("network_errors_retryable") is not False:
        raise RuntimeError("network-error retry guard mismatch")
    return {
        **observed,
        "seal_sha256": sha256_path(seal),
        "verified": True,
    }


def env_value(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    raise RuntimeError(f"missing credential environment class: {names}")


def load_auth_material() -> tuple[str, Any]:
    key_id = env_value("KALSHI_API_KEY_ID", "KALSHI_KEY_ID")
    key_path = env_value(
        "KALSHI_PRIVATE_KEY_PATH", "KALSHI_PRIV_KEY_PATH"
    )
    private_key = serialization.load_pem_private_key(
        Path(key_path).read_bytes(), password=None
    )
    return key_id, private_key


def auth_headers(key_id: str, private_key: Any) -> dict[str, str]:
    timestamp_ms = str(int(time.time() * 1000))
    message = f"{timestamp_ms}GET{SIGNED_PATH}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "Accept": "application/json",
        "User-Agent": "sealed-brti-hourly-calibration/2",
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AttemptStartLimiter:
    def __init__(self, interval_s: float, run_start: float):
        self.interval_s = interval_s
        self.run_start = run_start
        self.next_start = run_start
        self.lock = threading.Lock()

    def wait_turn(self) -> float:
        with self.lock:
            now = time.monotonic()
            reserved = max(now, self.next_start)
            self.next_start = reserved + self.interval_s
        delay = reserved - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        return (time.monotonic() - self.run_start) * 1000.0


class ConcurrencyAudit:
    def __init__(self):
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)

    def exit(self) -> None:
        with self.lock:
            self.active -= 1


def bounded_read(response) -> bytes:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError(
            f"response exceeds {MAX_RESPONSE_BYTES} byte bound"
        )
    return body


def fixed_url(hour_start: str) -> tuple[str, dict[str, str]]:
    if hour_start not in HOUR_STARTS:
        raise RuntimeError("attempted hour outside sealed request list")
    if utc_ms(hour_start) >= BANNED_START_MS:
        raise RuntimeError("attempted hard-banned timestamp")
    query_items = (
        ("id", INDEX_ID),
        ("timespan", TIMESPAN),
        ("timestamp", hour_start),
    )
    query = urllib.parse.urlencode(query_items)
    url = f"{HOST}{SIGNED_PATH}?{query}"
    if "/cfbenchmarks/values" in url:
        raise RuntimeError("recent-values endpoint is forbidden")
    return url, dict(query_items)


def validate_hour(
    body: bytes,
    hour_start: str,
) -> dict[str, Any]:
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
    if not isinstance(payload, list):
        raise RuntimeError("CF payload is not a list")
    if len(payload) != 3_600:
        raise RuntimeError(
            f"hour payload count is {len(payload)}, expected 3600"
        )
    start_ms = utc_ms(hour_start)
    observed_times = []
    for row_number, row in enumerate(payload):
        if not isinstance(row, dict):
            raise RuntimeError(f"payload row {row_number} is not an object")
        raw_time = row.get("time")
        if isinstance(raw_time, bool) or not isinstance(raw_time, int):
            raise RuntimeError(
                f"payload row {row_number} has non-integer time"
            )
        source_ms = int(raw_time)
        expected_ms = start_ms + row_number * 1000
        if source_ms != expected_ms:
            raise RuntimeError(
                f"payload row {row_number} does not match exact second grid"
            )
        if source_ms >= BANNED_START_MS:
            raise RuntimeError("payload reaches hard-banned start")
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
        observed_times.append(source_ms)
    if len(observed_times) != len(set(observed_times)):
        raise RuntimeError("duplicate source timestamp within hour")
    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return {
        "payload_count": len(payload),
        "payload_sha256": sha256_bytes(canonical),
        "minimum_source_ms": observed_times[0],
        "maximum_source_ms": observed_times[-1],
        "exact_second_grid": True,
        "duplicate_source_timestamps": 0,
    }


def one_attempt(
    hour_start: str,
    attempt_number: int,
    key_id: str,
    private_key: Any,
    limiter: AttemptStartLimiter,
    concurrency: ConcurrencyAudit,
) -> tuple[bytes | None, dict[str, Any]]:
    url, query = fixed_url(hour_start)
    start_offset_ms = limiter.wait_turn()
    request = urllib.request.Request(
        url=url,
        method="GET",
        headers=auth_headers(key_id, private_key),
    )
    opener = urllib.request.build_opener(NoRedirect)
    started = time.monotonic()
    concurrency.enter()
    try:
        try:
            with opener.open(request, timeout=TIMEOUT_S) as response:
                body = bounded_read(response)
                status = int(response.status)
                content_type = response.headers.get("Content-Type")
        except urllib.error.HTTPError as exc:
            body = bounded_read(exc)
            status = int(exc.code)
            content_type = exc.headers.get("Content-Type")
        except Exception as exc:
            return None, {
                "attempt_number": attempt_number,
                "query": query,
                "attempt_start_offset_ms": round(start_offset_ms, 3),
                "http_status": None,
                "elapsed_ms": round(
                    (time.monotonic() - started) * 1000, 3
                ),
                "network_error_class": type(exc).__name__,
                "body_bytes": 0,
                "body_sha256": sha256_bytes(b""),
            }
    finally:
        concurrency.exit()
    return body, {
        "attempt_number": attempt_number,
        "query": query,
        "attempt_start_offset_ms": round(start_offset_ms, 3),
        "http_status": status,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        "content_type": content_type,
        "network_error_class": None,
        "body_bytes": len(body),
        "body_sha256": sha256_bytes(body),
    }


def acquire_hour(
    hour_start: str,
    out_dir: Path,
    key_id: str,
    private_key: Any,
    limiter: AttemptStartLimiter,
    concurrency: ConcurrencyAudit,
) -> dict[str, Any]:
    logical: dict[str, Any] = {
        "hour_start_utc": hour_start,
        "status": "IN_PROGRESS",
        "attempts": [],
        "persisted": False,
    }
    for attempt_number in range(1, MAX_ATTEMPTS + 1):
        body, attempt = one_attempt(
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
                validation = validate_hour(body, hour_start)
            except Exception as exc:
                attempt["validation_error_class"] = type(exc).__name__
                attempt["validation_error"] = str(exc)[:500]
                logical["status"] = "FAILED_VALIDATION"
                return logical
            compressed = gzip.compress(body, compresslevel=9, mtime=0)
            hour_label = hour_start[:13].replace("T", "_")
            destination = out_dir / (
                f"brti_history_{hour_label}.json.gz"
            )
            destination.write_bytes(compressed)
            logical.update({
                "status": "SUCCESS",
                "persisted": True,
                "stored_path": str(destination),
                "stored_bytes": len(compressed),
                "stored_sha256": sha256_bytes(compressed),
                **validation,
            })
            attempt["validation"] = "EXACT_3600_SECOND_GRID"
            return logical
        if (
            attempt_number == 1
            and status in RETRY_STATUSES
        ):
            attempt["retry_scheduled"] = True
            attempt["retry_backoff_ms"] = round(
                RETRY_BACKOFF_S * 1000
            )
            time.sleep(RETRY_BACKOFF_S)
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
    counts = Counter(
        row["status"] for row in report["hours"]
    )
    lines = [
        "# Authoritative BRTI hourly acquisition receipt",
        "",
        f"Status: **{report['status']}**.",
        "",
        f"- Logical hours: `{len(report['hours'])}`",
        f"- Successful hours: `{counts.get('SUCCESS', 0)}`",
        f"- Total HTTP/network attempts: `{report['attempt_count']}`",
        f"- Retried hours: `{report['retried_hour_count']}`",
        f"- Maximum observed in-flight requests: "
        f"`{report['maximum_observed_concurrency']}`",
        f"- Minimum configured attempt-start interval: "
        f"`{report['minimum_attempt_start_interval_ms']} ms`",
        "",
        "Every attempt is recorded in the JSON with status, latency, body "
        "bytes, and body SHA-256. Individual BRTI values and all credential "
        "material are intentionally absent.",
        "",
    ]
    failures = [
        row for row in report["hours"] if row["status"] != "SUCCESS"
    ]
    if failures:
        lines.extend([
            "## Failed hours",
            "",
            *[
                f"- `{row['hour_start_utc']}`: `{row['status']}`"
                for row in failures
            ],
            "",
            "The analyzer must not run on a partial acquisition.",
            "",
        ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", required=True)
    parser.add_argument("--seal", required=True)
    parser.add_argument("--analyzer", required=True)
    parser.add_argument("--base-analyzer", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--md-out", required=True)
    args = parser.parse_args()

    script = Path(__file__).resolve()
    prereg = Path(args.prereg).resolve()
    seal = Path(args.seal).resolve()
    analyzer = Path(args.analyzer).resolve()
    base_analyzer = Path(args.base_analyzer).resolve()
    out_dir = Path(args.out_dir).resolve()
    receipt_path = Path(args.receipt).resolve()
    markdown_path = Path(args.md_out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    seal_info = verify_seal(
        script,
        analyzer,
        base_analyzer,
        prereg,
        seal,
    )
    key_id, private_key = load_auth_material()
    run_started = time.monotonic()
    limiter = AttemptStartLimiter(
        MIN_ATTEMPT_START_INTERVAL_S, run_started
    )
    concurrency = ConcurrencyAudit()
    results = []
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as executor:
        future_to_hour = {
            executor.submit(
                acquire_hour,
                hour_start,
                out_dir,
                key_id,
                private_key,
                limiter,
                concurrency,
            ): hour_start
            for hour_start in HOUR_STARTS
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
    attempt_count = sum(len(row["attempts"]) for row in results)
    successful = [row for row in results if row["status"] == "SUCCESS"]
    report = {
        "schema": "brti-authoritative-hourly-acquisition-receipt-v2",
        "status": (
            "COMPLETE_AUTHORITATIVE_HOURLY_HISTORY_ACQUIRED"
            if len(successful) == len(HOUR_STARTS)
            else "NO_DECISION_HOURLY_ACQUISITION_FAILED"
        ),
        "read_only": True,
        "trading_action_performed": False,
        "engine_or_service_mutation_performed": False,
        "recent_values_endpoint_called": False,
        "hard_banned_history_requested": False,
        "logical_request_count": len(HOUR_STARTS),
        "attempt_count": attempt_count,
        "retried_hour_count": sum(
            len(row["attempts"]) == 2 for row in results
        ),
        "maximum_attempts_per_hour": MAX_ATTEMPTS,
        "retryable_http_statuses_only": list(RETRY_STATUSES),
        "network_errors_retryable": False,
        "redirect_count": 0,
        "maximum_configured_concurrency": MAX_CONCURRENCY,
        "maximum_observed_concurrency": concurrency.maximum,
        "minimum_attempt_start_interval_ms": round(
            MIN_ATTEMPT_START_INTERVAL_S * 1000
        ),
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
