#!/usr/bin/env python3
"""Sealed, read-only acquisition of three authoritative BRTI UTC days.

This program has no order endpoint, engine import, or service-control path.
It makes exactly the three pre-registered history requests, without retries
or redirects, validates every returned source timestamp, and stores the raw
Kalshi envelopes as deterministic gzip files.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
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
TIMESPAN = "DAY"
DAY_STARTS = (
    "2026-07-20T00:00:00.000Z",
    "2026-07-21T00:00:00.000Z",
    "2026-07-22T00:00:00.000Z",
)
BANNED_START = "2026-07-23T00:00:00.000Z"
TIMEOUT_S = 30
MAX_RESPONSE_BYTES = 67_108_864
PAUSE_S = 0.250


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


ALLOWED_START_MS = tuple(utc_ms(value) for value in DAY_STARTS)
BANNED_START_MS = utc_ms(BANNED_START)


def verify_seal(
    script: Path,
    analyzer: Path,
    prereg: Path,
    seal: Path,
) -> dict[str, Any]:
    frozen = json.loads(seal.read_text(encoding="utf-8"))
    observed = {
        "downloader_sha256": sha256_path(script),
        "analyzer_sha256": sha256_path(analyzer),
        "preregistration_sha256": sha256_path(prereg),
    }
    for key, value in observed.items():
        if frozen.get(key) != value:
            raise RuntimeError(f"seal mismatch for {key}: observed {value}")
    specification = json.loads(prereg.read_text(encoding="utf-8"))
    download = specification.get("download") or {}
    if download.get("timestamps_utc") != list(DAY_STARTS):
        raise RuntimeError("pre-registration day list mismatch")
    if download.get("hard_banned_start_utc") != BANNED_START:
        raise RuntimeError("pre-registration banned-start mismatch")
    if int(download.get("request_count", -1)) != len(DAY_STARTS):
        raise RuntimeError("pre-registration request-count mismatch")
    if int(download.get("retry_count", -1)) != 0:
        raise RuntimeError("pre-registration retry-count mismatch")
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


def auth_headers() -> dict[str, str]:
    key_id = env_value("KALSHI_API_KEY_ID", "KALSHI_KEY_ID")
    key_path = env_value(
        "KALSHI_PRIVATE_KEY_PATH", "KALSHI_PRIV_KEY_PATH"
    )
    private_key = serialization.load_pem_private_key(
        Path(key_path).read_bytes(), password=None
    )
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
        "User-Agent": "sealed-brti-authoritative-calibration/1",
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def bounded_read(response) -> bytes:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError(
            f"response exceeds {MAX_RESPONSE_BYTES} byte bound"
        )
    return body


def validate_payload(
    body: bytes,
    requested_start_ms: int,
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
    if not isinstance(payload, list):
        raise RuntimeError("CF payload is not a list")
    if not payload:
        raise RuntimeError("CF historical day payload is empty")
    requested_end_ms = requested_start_ms + 86_400_000
    previous_time = None
    parsed_rows: list[dict[str, Any]] = []
    times: list[int] = []
    for row_number, row in enumerate(payload):
        if not isinstance(row, dict):
            raise RuntimeError(f"payload row {row_number} is not an object")
        raw_time = row.get("time")
        if isinstance(raw_time, bool) or not isinstance(raw_time, int):
            raise RuntimeError(
                f"payload row {row_number} has non-integer time"
            )
        source_ms = int(raw_time)
        if not requested_start_ms <= source_ms < requested_end_ms:
            raise RuntimeError(
                f"payload row {row_number} is outside requested UTC day"
            )
        if source_ms >= BANNED_START_MS:
            raise RuntimeError("payload reaches hard-banned start")
        if previous_time is not None and source_ms < previous_time:
            raise RuntimeError("payload source times are not ascending")
        raw_value = row.get("value")
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"payload row {row_number} has non-numeric value"
            ) from exc
        if not math.isfinite(value) or value <= 0:
            raise RuntimeError(
                f"payload row {row_number} has invalid numeric value"
            )
        previous_time = source_ms
        times.append(source_ms)
        parsed_rows.append(row)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return parsed_rows, {
        "payload_count": len(payload),
        "payload_sha256": sha256_bytes(canonical),
        "minimum_source_ms": min(times) if times else None,
        "maximum_source_ms": max(times) if times else None,
        "duplicate_source_timestamps": len(times) - len(set(times)),
    }


def fixed_query(day_start: str) -> tuple[str, dict[str, str]]:
    if day_start not in DAY_STARTS:
        raise RuntimeError("attempted timestamp outside sealed request list")
    if utc_ms(day_start) >= BANNED_START_MS:
        raise RuntimeError("attempted hard-banned timestamp")
    query_items = (
        ("id", INDEX_ID),
        ("timespan", TIMESPAN),
        ("timestamp", day_start),
    )
    query = urllib.parse.urlencode(query_items)
    url = f"{HOST}{SIGNED_PATH}?{query}"
    if "/cfbenchmarks/values" in url:
        raise RuntimeError("recent-values endpoint is forbidden")
    return url, dict(query_items)


def acquire_day(day_start: str) -> tuple[bytes | None, dict[str, Any]]:
    url, query = fixed_query(day_start)
    request = urllib.request.Request(
        url=url,
        method="GET",
        headers=auth_headers(),
    )
    opener = urllib.request.build_opener(NoRedirect)
    started = time.monotonic()
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
            "query": query,
            "http_status": None,
            "elapsed_ms": round(
                (time.monotonic() - started) * 1000, 3
            ),
            "network_error_class": type(exc).__name__,
            "body_bytes": 0,
            "body_sha256": sha256_bytes(b""),
            "persisted": False,
        }
    summary: dict[str, Any] = {
        "query": query,
        "http_status": status,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        "content_type": content_type,
        "network_error_class": None,
        "body_bytes": len(body),
        "body_sha256": sha256_bytes(body),
        "persisted": False,
    }
    if status != 200:
        return None, summary
    try:
        _rows, payload_summary = validate_payload(
            body, utc_ms(day_start)
        )
    except Exception as exc:
        summary["validation_error_class"] = type(exc).__name__
        summary["validation_error"] = str(exc)[:500]
        return None, summary
    summary.update(payload_summary)
    return body, summary


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Authoritative BRTI acquisition receipt",
        "",
        f"Status: **{report['status']}**.",
        "",
        "Exactly three pre-registered, read-only history requests were "
        "attempted. There were no retries, redirects, recent-values calls, "
        "orders, cancels, engine imports, or service mutations.",
        "",
        "## Requests",
        "",
    ]
    for row in report["requests"]:
        lines.append(
            f"- `{row['query']['timestamp']}`: HTTP "
            f"`{row['http_status']}`, rows `{row.get('payload_count')}`, "
            f"bytes `{row['body_bytes']}`, persisted `{row['persisted']}`, "
            f"body SHA-256 `{row['body_sha256']}`."
        )
    lines.extend([
        "",
        "Individual BRTI values are intentionally absent from this receipt. "
        "The deterministic raw envelopes are bound by SHA-256.",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prereg", required=True)
    parser.add_argument("--seal", required=True)
    parser.add_argument("--analyzer", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--md-out", required=True)
    args = parser.parse_args()

    script = Path(__file__).resolve()
    prereg = Path(args.prereg).resolve()
    seal = Path(args.seal).resolve()
    analyzer = Path(args.analyzer).resolve()
    out_dir = Path(args.out_dir).resolve()
    receipt_path = Path(args.receipt).resolve()
    md_path = Path(args.md_out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    seal_info = verify_seal(script, analyzer, prereg, seal)

    report: dict[str, Any] = {
        "schema": "brti-authoritative-acquisition-receipt-v1",
        "status": "IN_PROGRESS",
        "read_only": True,
        "trading_action_performed": False,
        "engine_or_service_mutation_performed": False,
        "recent_values_endpoint_called": False,
        "request_count_planned": len(DAY_STARTS),
        "request_count_attempted": 0,
        "retry_count": 0,
        "redirect_count": 0,
        "hard_banned_history_requested": False,
        "seal": seal_info,
        "requests": [],
    }
    failure = False
    for request_number, day_start in enumerate(DAY_STARTS):
        body, summary = acquire_day(day_start)
        report["request_count_attempted"] += 1
        if body is None:
            failure = True
            report["requests"].append(summary)
            break
        compressed = gzip.compress(body, compresslevel=9, mtime=0)
        day = day_start[:10]
        destination = out_dir / f"brti_history_{day}.json.gz"
        destination.write_bytes(compressed)
        summary.update({
            "persisted": True,
            "stored_path": str(destination),
            "stored_bytes": len(compressed),
            "stored_sha256": sha256_bytes(compressed),
        })
        report["requests"].append(summary)
        if request_number < len(DAY_STARTS) - 1:
            time.sleep(PAUSE_S)
    report["status"] = (
        "COMPLETE_AUTHORITATIVE_HISTORY_ACQUIRED"
        if not failure and len(report["requests"]) == len(DAY_STARTS)
        else "NO_DECISION_ACQUISITION_FAILED"
    )
    receipt_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    if report["status"] != "COMPLETE_AUTHORITATIVE_HISTORY_ACQUIRED":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
