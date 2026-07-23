#!/usr/bin/env python3
"""Capture a redacted, read-only private-fill fee truth receipt.

The program has one hard-coded HTTP method: GET.  It never creates, amends, or
cancels an order.  Identifiers and full market tickers are not published:
stable SHA-256 references and the fee-relevant series ticker are retained.

Credentials are consumed only from the execution host environment.  They are
never serialized into the receipt.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.parse
from typing import Any, Mapping


API_PREFIX = "/trade-api/v2"
METHOD = "GET"
LIVE_PATH = "/portfolio/fills"
EXPECTED_FILL_FIELDS = {
    "fill_id",
    "trade_id",
    "order_id",
    "ticker",
    "market_ticker",
    "count_fp",
    "yes_price_dollars",
    "no_price_dollars",
    "is_taker",
    "fee_cost",
    "created_time",
    "subaccount_number",
    "ts",
}


class PrivateFeeCaptureError(RuntimeError):
    """A private fee response cannot form a trustworthy redacted receipt."""


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fixed(value: object, *, scale: int, maximum_places: int, name: str) -> int:
    if not isinstance(value, str) or not value:
        raise PrivateFeeCaptureError(f"{name} must be a fixed-point string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise PrivateFeeCaptureError(f"{name} is malformed") from exc
    if not parsed.is_finite() or parsed < 0:
        raise PrivateFeeCaptureError(f"{name} must be finite and non-negative")
    exponent = parsed.as_tuple().exponent
    if exponent < -maximum_places:
        raise PrivateFeeCaptureError(f"{name} exceeds supported precision")
    scaled = parsed * scale
    if scaled != scaled.to_integral_value():
        raise PrivateFeeCaptureError(f"{name} cannot be represented exactly")
    return int(scaled)


def redact_fill(row: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise PrivateFeeCaptureError("fill must be an object")
    missing = EXPECTED_FILL_FIELDS - set(row)
    if missing:
        raise PrivateFeeCaptureError(
            "fill is missing required fields: " + ",".join(sorted(missing))
        )
    ticker = row.get("market_ticker") or row.get("ticker")
    if not isinstance(ticker, str) or not ticker:
        raise PrivateFeeCaptureError("market ticker is absent")
    series = ticker.split("-", 1)[0]
    if not series:
        raise PrivateFeeCaptureError("series ticker cannot be derived")
    for identity in ("fill_id", "trade_id", "order_id"):
        if not isinstance(row.get(identity), str) or not row[identity]:
            raise PrivateFeeCaptureError(f"{identity} is absent")
    if type(row.get("is_taker")) is not bool:
        raise PrivateFeeCaptureError("is_taker must be bool")
    created = row.get("created_time")
    if not isinstance(created, str) or not created.endswith("Z"):
        raise PrivateFeeCaptureError("created_time must be UTC")
    return {
        "fill_ref": sha256_bytes(row["fill_id"].encode("utf-8")),
        "trade_ref": sha256_bytes(row["trade_id"].encode("utf-8")),
        "order_ref": sha256_bytes(row["order_id"].encode("utf-8")),
        "series_ticker": series,
        "quantity_e2": _fixed(
            row["count_fp"], scale=100, maximum_places=2, name="count_fp"
        ),
        "yes_price_e4": _fixed(
            row["yes_price_dollars"],
            scale=10_000,
            maximum_places=4,
            name="yes_price_dollars",
        ),
        "no_price_e4": _fixed(
            row["no_price_dollars"],
            scale=10_000,
            maximum_places=4,
            name="no_price_dollars",
        ),
        "is_taker": row["is_taker"],
        "fee_cost_e6": _fixed(
            row["fee_cost"],
            scale=1_000_000,
            maximum_places=6,
            name="fee_cost",
        ),
        "created_time_utc": created,
    }


def _sign(key_path: str, message: str) -> str:
    result = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-sigopt",
            "rsa_padding_mode:pss",
            "-sigopt",
            "rsa_pss_saltlen:digest",
            "-sign",
            key_path,
            "-binary",
        ],
        input=message.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    if result.returncode or not result.stdout:
        raise PrivateFeeCaptureError("RSA-PSS signing failed")
    return base64.b64encode(result.stdout).decode("ascii")


def _request_json(
    connection: http.client.HTTPSConnection,
    *,
    key_id: str,
    key_path: str,
    path: str,
) -> tuple[dict[str, Any], str]:
    timestamp = str(int(time.time() * 1000))
    signed_path = path.split("?", 1)[0]
    signature = _sign(
        key_path,
        timestamp + METHOD + API_PREFIX + signed_path,
    )
    connection.request(
        METHOD,
        API_PREFIX + path,
        headers={
            "KALSHI-ACCESS-KEY": key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Connection": "keep-alive",
        },
    )
    response = connection.getresponse()
    raw = response.read()
    if response.status != 200:
        raise PrivateFeeCaptureError(
            f"private GET returned HTTP {response.status}"
        )
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PrivateFeeCaptureError("private GET returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PrivateFeeCaptureError("private GET response must be an object")
    return payload, sha256_bytes(raw)


def capture_live_fills(
    *,
    key_id: str,
    key_path: str,
    base_url: str,
    minimum_timestamp_ms: int,
) -> dict[str, Any]:
    """Capture all current-tier fills at/after the exact caller time bound."""
    if type(minimum_timestamp_ms) is not int or minimum_timestamp_ms < 0:
        raise PrivateFeeCaptureError("minimum_timestamp_ms must be non-negative")
    host = urllib.parse.urlsplit(base_url).netloc
    if not host:
        raise PrivateFeeCaptureError("base URL has no host")
    connection = http.client.HTTPSConnection(host, timeout=15)
    cursor = ""
    raw_page_hashes: list[str] = []
    fills: list[dict[str, Any]] = []
    seen_fill_refs: set[str] = set()
    try:
        while True:
            query = f"?limit=1000&min_ts={minimum_timestamp_ms}"
            if cursor:
                query += "&cursor=" + urllib.parse.quote(cursor, safe="")
            payload, raw_hash = _request_json(
                connection,
                key_id=key_id,
                key_path=key_path,
                path=LIVE_PATH + query,
            )
            raw_page_hashes.append(raw_hash)
            rows = payload.get("fills")
            next_cursor = payload.get("cursor", "")
            if not isinstance(rows, list) or not isinstance(next_cursor, str):
                raise PrivateFeeCaptureError("private fill page schema mismatch")
            for raw_row in rows:
                row = redact_fill(raw_row)
                if row["fill_ref"] in seen_fill_refs:
                    raise PrivateFeeCaptureError("duplicate fill across pages")
                seen_fill_refs.add(row["fill_ref"])
                fills.append(row)
            if not next_cursor:
                break
            if next_cursor == cursor:
                raise PrivateFeeCaptureError("cursor did not advance")
            cursor = next_cursor
    finally:
        connection.close()

    fills.sort(key=lambda row: (row["created_time_utc"], row["fill_ref"]))
    receipt = {
        "schema_version": "pnl-spine-private-fee-truth-v1",
        "state": "PASS" if fills else "EMPTY",
        "capture_method": "AUTHENTICATED_GET_ONLY",
        "source_endpoint": "/trade-api/v2/portfolio/fills",
        "minimum_timestamp_ms": minimum_timestamp_ms,
        "captured_at_utc": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "redaction": "FULL_TICKER_AND_IDENTIFIERS_REMOVED_SHA256_REFS_ONLY",
        "credential_material_serialized": False,
        "page_count": len(raw_page_hashes),
        "raw_page_sha256": raw_page_hashes,
        "fill_count": len(fills),
        "fills": fills,
    }
    receipt["content_sha256"] = sha256_bytes(canonical_json_bytes(receipt))
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimum-timestamp-ms", type=int, required=True)
    args = parser.parse_args()
    key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    base_url = os.environ.get(
        "KALSHI_BASE_URL", "https://external-api.kalshi.com"
    )
    if not key_id or not key_path:
        print("private API credentials are unavailable", file=sys.stderr)
        return 2
    try:
        receipt = capture_live_fills(
            key_id=key_id,
            key_path=key_path,
            base_url=base_url,
            minimum_timestamp_ms=args.minimum_timestamp_ms,
        )
    except PrivateFeeCaptureError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical_json_bytes(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
