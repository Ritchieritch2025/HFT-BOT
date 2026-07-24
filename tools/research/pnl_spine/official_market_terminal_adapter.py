#!/usr/bin/env python3
"""Capture and normalize official Kalshi market terminal evidence.

This adapter is intentionally split into two phases.

``capture``
    Performs unauthenticated GETs against a fixed official host.  It first
    requests the current-market endpoint and consults the historical endpoint
    only when the current endpoint returns exactly HTTP 404.  Every response
    body is written create-once together with both wall and monotonic clock
    brackets.  No terminal record produced by this phase is trusted by A01.

``normalize``
    Has no network path.  It accepts only an externally SHA-pinned capture
    receipt and an externally SHA-pinned raw-response pin document.  It
    re-hashes every response body, strictly parses the selected finalized
    market response, and emits A01 settlement records plus metadata evidence.

The current/historical market endpoints expose the state observed at fetch
time.  They are *not* historical point-in-time metadata snapshots.  In
particular, this adapter never promotes ``occurrence_datetime``,
``expected_expiration_time``, or ``close_time`` to scheduled start.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, BinaryIO, Mapping, Protocol, Sequence
import urllib.error
import urllib.parse
import urllib.request


AUTHORITY_SCHEMA = "a01-official-market-authority-v1"
CAPTURE_RECEIPT_SCHEMA = "a01-official-market-capture-receipt-v1"
RAW_PINS_SCHEMA = "a01-official-market-raw-pins-v1"
OUTPUT_SCHEMA = "a01-official-market-terminal-metadata-v1"

OFFICIAL_HOST = "api.elections.kalshi.com"
OFFICIAL_ORIGIN = f"https://{OFFICIAL_HOST}"
CURRENT_MARKET_PATH = "/trade-api/v2/markets/"
HISTORICAL_MARKET_PATH = "/trade-api/v2/historical/markets/"
PURPOSE = "READ_ONLY_OFFICIAL_MARKET_TERMINAL_METADATA"
USER_AGENT = "w09-a01-terminal-evidence/1"
REQUEST_HEADERS = {
    "Accept": "application/json",
    "User-Agent": USER_AGENT,
}
TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,199}$")
UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,9})?Z$"
)
DOLLARS_4_RE = re.compile(r"^(?:0|1)\.\d{4}$")
SAFE_RAW_NAME_RE = re.compile(
    r"^\d{4}\.(?:current|historical)\.response\.json$"
)

ADAPTER_CONFIG = {
    "schema_version": "a01-official-market-adapter-config-v1",
    "origin": OFFICIAL_ORIGIN,
    "current_market_path": CURRENT_MARKET_PATH,
    "historical_market_path": HISTORICAL_MARKET_PATH,
    "selection_rule": "CURRENT_THEN_HISTORICAL_ONLY_ON_CURRENT_404",
    "request_method": "GET",
    "request_headers": REQUEST_HEADERS,
    "authentication": "FORBIDDEN",
    "proxy": "FORBIDDEN",
    "redirect": "FORBIDDEN",
    "timeout_seconds": TIMEOUT_SECONDS,
    "max_response_bytes": MAX_RESPONSE_BYTES,
    "terminal_rule": "FINALIZED_YES_NO_EXACT_PAYOUT_ONLY",
    "metadata_temporality": "OBSERVED_AT_FETCH_NOT_HISTORICAL_AS_OF",
    "scheduled_start_mapping": "FORBIDDEN",
}


class OfficialMarketAdapterError(ValueError):
    """The read-only transport or evidence contract failed closed."""


def _canonical_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return value
    if isinstance(value, Mapping) and all(
        isinstance(key, str) for key in value
    ):
        return {
            key: _canonical_value(value[key])
            for key in sorted(value)
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise TypeError(
        f"unsupported canonical value {type(value).__name__}; "
        "floats are forbidden"
    )


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


ADAPTER_CONFIG_SHA256 = canonical_sha256(ADAPTER_CONFIG)


def adapter_code_sha256() -> str:
    """Return the raw-byte SHA of this exact adapter source file."""

    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _sha(label: str, value: object) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise OfficialMarketAdapterError(
            f"{label} must be a lowercase SHA-256"
        )
    return value


def _text(label: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise OfficialMarketAdapterError(f"{label} must be nonempty text")
    return value


def _plain_int(
    label: str,
    value: object,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        raise OfficialMarketAdapterError(f"{label} must be a plain integer")
    if minimum is not None and value < minimum:
        raise OfficialMarketAdapterError(f"{label} is below minimum")
    if maximum is not None and value > maximum:
        raise OfficialMarketAdapterError(f"{label} exceeds maximum")
    return value


def _exact_keys(
    label: str,
    value: Mapping[str, Any],
    expected: set[str],
) -> None:
    actual = set(value)
    if actual != expected:
        raise OfficialMarketAdapterError(
            f"{label} keys mismatch; "
            f"missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OfficialMarketAdapterError(
                f"duplicate JSON key is forbidden: {key}"
            )
        result[key] = value
    return result


def _reject_float(value: str) -> None:
    raise OfficialMarketAdapterError(
        f"floating-point JSON is forbidden: {value}"
    )


def parse_strict_json(raw: bytes, *, label: str) -> object:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OfficialMarketAdapterError(
            f"{label} is not UTF-8"
        ) from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except (json.JSONDecodeError, OfficialMarketAdapterError) as exc:
        raise OfficialMarketAdapterError(
            f"{label} is not strict JSON: {exc}"
        ) from exc


def _require_utc_timestamp(label: str, value: object) -> str:
    text = _text(label, value)
    if UTC_RE.fullmatch(text) is None:
        raise OfficialMarketAdapterError(
            f"{label} must be an RFC3339 UTC timestamp ending in Z"
        )
    # datetime validates calendar fields.  It accepts at most microseconds, so
    # trim only for validation while preserving all source digits in evidence.
    validation = text
    if "." in validation:
        prefix, suffix = validation[:-1].split(".", 1)
        validation = f"{prefix}.{suffix[:6]}+00:00"
    else:
        validation = validation[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(validation)
    except ValueError as exc:
        raise OfficialMarketAdapterError(
            f"{label} is not a valid calendar timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise OfficialMarketAdapterError(f"{label} is not UTC")
    return text


def _timestamp_ns(label: str, value: str) -> int:
    # This is used only for authority validity, whose timestamps are emitted
    # with at most microsecond precision.  It is not used as market event time.
    text = _require_utc_timestamp(label, value)
    parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    return int(parsed.timestamp() * 1_000_000_000)


def _require_ticker(label: str, value: object) -> str:
    ticker = _text(label, value)
    if TICKER_RE.fullmatch(ticker) is None:
        raise OfficialMarketAdapterError(
            f"{label} is not a canonical Kalshi ticker"
        )
    return ticker


def _market_url(ticker: str, *, source_tier: str) -> str:
    ticker = _require_ticker("ticker", ticker)
    if source_tier == "current":
        path = CURRENT_MARKET_PATH
    elif source_tier == "historical":
        path = HISTORICAL_MARKET_PATH
    else:
        raise OfficialMarketAdapterError("unknown market source tier")
    quoted = urllib.parse.quote(ticker, safe="")
    url = f"{OFFICIAL_ORIGIN}{path}{quoted}"
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != OFFICIAL_HOST
        or parts.port is not None
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        raise OfficialMarketAdapterError("constructed URL left fixed origin")
    return url


def validate_authority(
    value: object,
    *,
    raw_sha256: str,
    expected_code_sha256: str,
    now_wall_ns: int | None,
) -> dict[str, Any]:
    _sha("authority raw SHA", raw_sha256)
    expected_code_sha256 = _sha(
        "expected adapter code SHA", expected_code_sha256
    )
    if not isinstance(value, Mapping):
        raise OfficialMarketAdapterError("authority must be an object")
    _exact_keys(
        "authority",
        value,
        {
            "schema_version",
            "authority_id",
            "purpose",
            "issued_at_utc",
            "expires_at_utc",
            "tickers",
            "adapter_code_sha256",
            "adapter_config_sha256",
        },
    )
    if value["schema_version"] != AUTHORITY_SCHEMA:
        raise OfficialMarketAdapterError("wrong authority schema")
    _text("authority_id", value["authority_id"])
    if value["purpose"] != PURPOSE:
        raise OfficialMarketAdapterError("authority purpose is not read-only")
    issued = _timestamp_ns("issued_at_utc", value["issued_at_utc"])
    expires = _timestamp_ns("expires_at_utc", value["expires_at_utc"])
    if expires <= issued:
        raise OfficialMarketAdapterError("authority expiry is not after issue")
    if now_wall_ns is not None and not (issued <= now_wall_ns <= expires):
        raise OfficialMarketAdapterError(
            "authority is not valid at capture wall clock"
        )
    if value["adapter_code_sha256"] != expected_code_sha256:
        raise OfficialMarketAdapterError(
            "authority does not bind expected adapter code"
        )
    if value["adapter_config_sha256"] != ADAPTER_CONFIG_SHA256:
        raise OfficialMarketAdapterError(
            "authority does not bind fixed adapter config"
        )
    tickers_raw = value["tickers"]
    if not isinstance(tickers_raw, list) or not tickers_raw:
        raise OfficialMarketAdapterError(
            "authority tickers must be a nonempty list"
        )
    tickers = [
        _require_ticker(f"tickers[{index}]", item)
        for index, item in enumerate(tickers_raw)
    ]
    if tickers != sorted(tickers) or len(tickers) != len(set(tickers)):
        raise OfficialMarketAdapterError(
            "authority tickers must be sorted and unique"
        )
    return dict(value)


@dataclass(frozen=True)
class HttpResult:
    status: int
    final_url: str
    body: bytes


class Transport(Protocol):
    def __call__(self, url: str, timeout_seconds: int) -> HttpResult:
        ...


class Clock(Protocol):
    def __call__(self) -> tuple[int, int]:
        """Return ``(wall_ns, monotonic_ns)``."""


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: BinaryIO,
        code: int,
        message: str,
        headers: Mapping[str, str],
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


def _direct_no_redirect_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirectHandler(),
    )


def _read_bounded(response: BinaryIO) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise OfficialMarketAdapterError(
                "official response exceeds fixed byte limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _build_request(url: str) -> urllib.request.Request:
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != OFFICIAL_HOST
        or parts.port is not None
        or parts.query
        or parts.fragment
        or not (
            parts.path.startswith(CURRENT_MARKET_PATH)
            or parts.path.startswith(HISTORICAL_MARKET_PATH)
        )
    ):
        raise OfficialMarketAdapterError("request URL is outside fixed paths")
    request = urllib.request.Request(
        url,
        headers=dict(REQUEST_HEADERS),
        method="GET",
    )
    forbidden = {
        "authorization",
        "kalshi-access-key",
        "kalshi-access-signature",
        "kalshi-access-timestamp",
        "proxy-authorization",
        "cookie",
    }
    actual = {name.lower() for name, _ in request.header_items()}
    if actual & forbidden:
        raise OfficialMarketAdapterError(
            "authentication or credential header is forbidden"
        )
    return request


def urllib_transport(url: str, timeout_seconds: int) -> HttpResult:
    request = _build_request(url)
    opener = _direct_no_redirect_opener()
    try:
        response = opener.open(request, timeout=timeout_seconds)
    except urllib.error.HTTPError as exc:
        if exc.geturl() != url:
            raise OfficialMarketAdapterError(
                "HTTP error response URL changed"
            ) from exc
        try:
            body = _read_bounded(exc)
        finally:
            exc.close()
        return HttpResult(
            status=int(exc.code),
            final_url=exc.geturl(),
            body=body,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OfficialMarketAdapterError(
            "official market request failed without an HTTP response"
        ) from exc
    try:
        if response.geturl() != url:
            raise OfficialMarketAdapterError(
                "official market response URL changed"
            )
        status = int(response.getcode())
        body = _read_bounded(response)
    finally:
        response.close()
    return HttpResult(status=status, final_url=url, body=body)


def system_clock() -> tuple[int, int]:
    return time.time_ns(), time.monotonic_ns()


def _safe_new_directory(path: Path) -> None:
    if not path.is_absolute():
        raise OfficialMarketAdapterError(
            "capture output directory must be absolute"
        )
    if path.name in {"", ".", ".."}:
        raise OfficialMarketAdapterError("unsafe capture output directory")
    parent = path.parent
    _require_no_symlink_components(parent, label="capture output parent")
    try:
        parent_stat = parent.lstat()
    except OSError as exc:
        raise OfficialMarketAdapterError(
            "capture output parent is unavailable"
        ) from exc
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(
        parent_stat.st_mode
    ):
        raise OfficialMarketAdapterError(
            "capture output parent must be a real directory"
        )
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise OfficialMarketAdapterError(
            "capture output directory is create-once and already exists"
        ) from exc


def _write_create_once(path: Path, payload: bytes, *, mode: int = 0o400) -> str:
    if not path.is_absolute():
        raise OfficialMarketAdapterError("output path must be absolute")
    _require_no_symlink_components(path.parent, label="output parent")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, mode)
    except OSError as exc:
        raise OfficialMarketAdapterError(
            f"output is not create-once: {path}"
        ) from exc
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OfficialMarketAdapterError("short output write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return hashlib.sha256(payload).hexdigest()


def _require_no_symlink_components(path: Path, *, label: str) -> None:
    """Reject symlinks in every existing component of an absolute path."""

    if not path.is_absolute():
        raise OfficialMarketAdapterError(f"{label} must be absolute")
    current = Path("/")
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except OSError as exc:
            raise OfficialMarketAdapterError(
                f"{label} contains an unavailable component"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise OfficialMarketAdapterError(
                f"{label} contains a symlink component"
            )


def _response_filename(index: int, source_tier: str) -> str:
    if source_tier not in {"current", "historical"}:
        raise OfficialMarketAdapterError("invalid response source tier")
    return f"{index:04d}.{source_tier}.response.json"


def _capture_one_attempt(
    *,
    ticker: str,
    index: int,
    source_tier: str,
    output_dir: Path,
    transport: Transport,
    clock: Clock,
) -> dict[str, Any]:
    url = _market_url(ticker, source_tier=source_tier)
    wall_before, mono_before = clock()
    _plain_int("fetch wall before", wall_before, minimum=0)
    _plain_int("fetch monotonic before", mono_before, minimum=0)
    result = transport(url, TIMEOUT_SECONDS)
    wall_after, mono_after = clock()
    _plain_int("fetch wall after", wall_after, minimum=wall_before)
    _plain_int("fetch monotonic after", mono_after, minimum=mono_before)
    if not isinstance(result, HttpResult):
        raise OfficialMarketAdapterError(
            "transport returned an untrusted result type"
        )
    if result.final_url != url:
        raise OfficialMarketAdapterError("transport final URL changed")
    status = _plain_int(
        "HTTP status", result.status, minimum=100, maximum=599
    )
    if not isinstance(result.body, bytes):
        raise OfficialMarketAdapterError("HTTP body must be raw bytes")
    if len(result.body) > MAX_RESPONSE_BYTES:
        raise OfficialMarketAdapterError(
            "official response exceeds fixed byte limit"
        )
    relative = _response_filename(index, source_tier)
    raw_sha = _write_create_once(
        output_dir / relative,
        result.body,
    )
    return {
        "ticker": ticker,
        "source_tier": source_tier,
        "request_url": url,
        "http_status": status,
        "fetch_wall_ns_before": wall_before,
        "fetch_wall_ns_after": wall_after,
        "fetch_monotonic_ns_before": mono_before,
        "fetch_monotonic_ns_after": mono_after,
        "raw_relative_path": relative,
        "raw_size": len(result.body),
        "raw_sha256": raw_sha,
        "adapter_code_sha256": adapter_code_sha256(),
        "adapter_config_sha256": ADAPTER_CONFIG_SHA256,
    }


def capture_markets(
    authority: Mapping[str, Any],
    *,
    authority_raw_sha256: str,
    expected_code_sha256: str,
    output_dir: Path,
    transport: Transport = urllib_transport,
    clock: Clock = system_clock,
) -> tuple[dict[str, Any], str]:
    """Capture one immutable official response set.

    The caller must independently retain the returned receipt raw SHA and
    construct a raw-pins document before calling :func:`normalize_capture`.
    """

    code_before = adapter_code_sha256()
    if code_before != expected_code_sha256:
        raise OfficialMarketAdapterError(
            "live adapter code does not match external code pin"
        )
    now_wall_ns, _ = clock()
    authority = validate_authority(
        authority,
        raw_sha256=authority_raw_sha256,
        expected_code_sha256=expected_code_sha256,
        now_wall_ns=now_wall_ns,
    )
    _safe_new_directory(output_dir)
    captures: list[dict[str, Any]] = []
    for index, ticker in enumerate(authority["tickers"]):
        attempts = [
            _capture_one_attempt(
                ticker=ticker,
                index=index,
                source_tier="current",
                output_dir=output_dir,
                transport=transport,
                clock=clock,
            )
        ]
        current_status = attempts[0]["http_status"]
        if current_status == 404:
            attempts.append(
                _capture_one_attempt(
                    ticker=ticker,
                    index=index,
                    source_tier="historical",
                    output_dir=output_dir,
                    transport=transport,
                    clock=clock,
                )
            )
            selected_index = 1
        elif current_status == 200:
            selected_index = 0
        else:
            raise OfficialMarketAdapterError(
                f"{ticker} current endpoint returned HTTP "
                f"{current_status}; only 200 or 404 is accepted"
            )
        selected = attempts[selected_index]
        if selected["http_status"] != 200:
            raise OfficialMarketAdapterError(
                f"{ticker} selected endpoint did not return HTTP 200"
            )
        # Parse now so a schema drift/non-final/tick-table error cannot yield
        # a deceptively complete capture receipt.  Normalization re-parses
        # independently after external raw pinning.
        selected_raw = (output_dir / selected["raw_relative_path"]).read_bytes()
        _parse_final_market_response(
            selected_raw,
            expected_ticker=ticker,
        )
        captures.append(
            {
                "ticker": ticker,
                "attempts": attempts,
                "selected_attempt_index": selected_index,
            }
        )
    if adapter_code_sha256() != code_before:
        raise OfficialMarketAdapterError(
            "adapter code changed during capture"
        )
    receipt = {
        "schema_version": CAPTURE_RECEIPT_SCHEMA,
        "authority_raw_sha256": authority_raw_sha256,
        "authority_id": authority["authority_id"],
        "adapter_code_sha256": code_before,
        "adapter_config_sha256": ADAPTER_CONFIG_SHA256,
        "adapter_config": ADAPTER_CONFIG,
        "request_authentication": "NONE",
        "proxy_policy": "DISABLED",
        "redirect_policy": "REJECT",
        "temporality": "OBSERVED_AT_FETCH_NOT_HISTORICAL_AS_OF",
        "captures": captures,
    }
    raw = canonical_json_bytes(receipt) + b"\n"
    raw_sha = _write_create_once(
        output_dir / "CAPTURE_RECEIPT.json",
        raw,
    )
    return receipt, raw_sha


def _money_e4(label: str, value: object) -> int:
    text = _text(label, value)
    if DOLLARS_4_RE.fullmatch(text) is None:
        raise OfficialMarketAdapterError(
            f"{label} must be fixed four-decimal binary dollars"
        )
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise OfficialMarketAdapterError(f"{label} is invalid") from exc
    scaled = parsed * 10_000
    if scaled != scaled.to_integral_value():
        raise OfficialMarketAdapterError(f"{label} is not exact E4")
    return int(scaled)


def _parse_standard_price_ranges(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) != 1:
        raise OfficialMarketAdapterError(
            "A01 requires one standard full-range cent tick interval"
        )
    item = value[0]
    if not isinstance(item, Mapping):
        raise OfficialMarketAdapterError("price_ranges item must be object")
    _exact_keys("price_ranges[0]", item, {"start", "end", "step"})
    start = _text("price_ranges[0].start", item["start"])
    end = _text("price_ranges[0].end", item["end"])
    step = _text("price_ranges[0].step", item["step"])
    if (start, end, step) != ("0.0000", "1.0000", "0.0100"):
        raise OfficialMarketAdapterError(
            "non-standard tick table is not eligible for frozen A01"
        )
    return [{"start": start, "end": end, "step": step}]


def _parse_final_market_response(
    raw: bytes,
    *,
    expected_ticker: str,
) -> dict[str, Any]:
    value = parse_strict_json(raw, label=f"market response {expected_ticker}")
    if not isinstance(value, Mapping):
        raise OfficialMarketAdapterError("market response must be an object")
    _exact_keys("market response envelope", value, {"market"})
    market = value["market"]
    if not isinstance(market, Mapping):
        raise OfficialMarketAdapterError("market must be an object")
    required = {
        "ticker",
        "status",
        "result",
        "settlement_value_dollars",
        "settlement_ts",
        "price_level_structure",
        "price_ranges",
        "open_time",
        "close_time",
        "expected_expiration_time",
        "occurrence_datetime",
    }
    missing = sorted(required - set(market))
    if missing:
        raise OfficialMarketAdapterError(
            f"market response schema drift; missing={missing}"
        )
    ticker = _require_ticker("market.ticker", market["ticker"])
    if ticker != expected_ticker:
        raise OfficialMarketAdapterError(
            "market response ticker does not match authority"
        )
    status = _text("market.status", market["status"])
    if status != "finalized":
        raise OfficialMarketAdapterError(
            f"market is not finalized: {status}"
        )
    result = _text("market.result", market["result"])
    if result not in {"yes", "no"}:
        raise OfficialMarketAdapterError(
            "finalized market result must be exactly yes or no"
        )
    payout_e4 = _money_e4(
        "market.settlement_value_dollars",
        market["settlement_value_dollars"],
    )
    expected_payout = 10_000 if result == "yes" else 0
    if payout_e4 != expected_payout:
        raise OfficialMarketAdapterError(
            "result and exact settlement value disagree"
        )
    settlement_ts = _require_utc_timestamp(
        "market.settlement_ts", market["settlement_ts"]
    )
    open_time = _require_utc_timestamp(
        "market.open_time", market["open_time"]
    )
    close_time = _require_utc_timestamp(
        "market.close_time", market["close_time"]
    )
    expected_expiration_time = _require_utc_timestamp(
        "market.expected_expiration_time",
        market["expected_expiration_time"],
    )
    occurrence_datetime = _require_utc_timestamp(
        "market.occurrence_datetime",
        market["occurrence_datetime"],
    )
    price_level_structure = _text(
        "market.price_level_structure",
        market["price_level_structure"],
    )
    if price_level_structure != "linear_cent":
        raise OfficialMarketAdapterError(
            "non-standard price level structure is not eligible for A01"
        )
    price_ranges = _parse_standard_price_ranges(market["price_ranges"])
    return {
        "ticker": ticker,
        "status": status,
        "result": result,
        "yes_settlement_value_e4": payout_e4,
        "settlement_ts": settlement_ts,
        "price_level_structure": price_level_structure,
        "price_ranges": price_ranges,
        "tick_size_e4": 100,
        "open_time": open_time,
        "close_time": close_time,
        "expected_expiration_time": expected_expiration_time,
        "occurrence_datetime": occurrence_datetime,
    }


def _validate_capture_receipt(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OfficialMarketAdapterError("capture receipt must be object")
    _exact_keys(
        "capture receipt",
        value,
        {
            "schema_version",
            "authority_raw_sha256",
            "authority_id",
            "adapter_code_sha256",
            "adapter_config_sha256",
            "adapter_config",
            "request_authentication",
            "proxy_policy",
            "redirect_policy",
            "temporality",
            "captures",
        },
    )
    if value["schema_version"] != CAPTURE_RECEIPT_SCHEMA:
        raise OfficialMarketAdapterError("wrong capture receipt schema")
    _sha("capture authority SHA", value["authority_raw_sha256"])
    _text("capture authority id", value["authority_id"])
    _sha("capture adapter code SHA", value["adapter_code_sha256"])
    if value["adapter_config_sha256"] != ADAPTER_CONFIG_SHA256:
        raise OfficialMarketAdapterError(
            "capture adapter config SHA mismatch"
        )
    if value["adapter_config"] != ADAPTER_CONFIG:
        raise OfficialMarketAdapterError("capture adapter config mismatch")
    if (
        value["request_authentication"] != "NONE"
        or value["proxy_policy"] != "DISABLED"
        or value["redirect_policy"] != "REJECT"
        or value["temporality"]
        != "OBSERVED_AT_FETCH_NOT_HISTORICAL_AS_OF"
    ):
        raise OfficialMarketAdapterError(
            "capture transport/temporality policy mismatch"
        )
    captures = value["captures"]
    if not isinstance(captures, list) or not captures:
        raise OfficialMarketAdapterError("capture set must be nonempty")
    seen: set[str] = set()
    previous = ""
    for capture_index, capture in enumerate(captures):
        if not isinstance(capture, Mapping):
            raise OfficialMarketAdapterError("capture entry must be object")
        _exact_keys(
            f"captures[{capture_index}]",
            capture,
            {"ticker", "attempts", "selected_attempt_index"},
        )
        ticker = _require_ticker(
            f"captures[{capture_index}].ticker", capture["ticker"]
        )
        if ticker in seen or (previous and ticker <= previous):
            raise OfficialMarketAdapterError(
                "capture tickers must be sorted and unique"
            )
        seen.add(ticker)
        previous = ticker
        attempts = capture["attempts"]
        if not isinstance(attempts, list) or len(attempts) not in {1, 2}:
            raise OfficialMarketAdapterError(
                "capture must contain one or two attempts"
            )
        selected_index = _plain_int(
            "selected_attempt_index",
            capture["selected_attempt_index"],
            minimum=0,
            maximum=len(attempts) - 1,
        )
        for attempt_index, attempt in enumerate(attempts):
            if not isinstance(attempt, Mapping):
                raise OfficialMarketAdapterError(
                    "capture attempt must be object"
                )
            _exact_keys(
                f"capture attempt {attempt_index}",
                attempt,
                {
                    "ticker",
                    "source_tier",
                    "request_url",
                    "http_status",
                    "fetch_wall_ns_before",
                    "fetch_wall_ns_after",
                    "fetch_monotonic_ns_before",
                    "fetch_monotonic_ns_after",
                    "raw_relative_path",
                    "raw_size",
                    "raw_sha256",
                    "adapter_code_sha256",
                    "adapter_config_sha256",
                },
            )
            if attempt["ticker"] != ticker:
                raise OfficialMarketAdapterError(
                    "capture attempt ticker does not match parent"
                )
            expected_tier = (
                "current" if attempt_index == 0 else "historical"
            )
            if attempt["source_tier"] != expected_tier:
                raise OfficialMarketAdapterError(
                    "capture endpoint ordering is invalid"
                )
            if attempt["request_url"] != _market_url(
                ticker, source_tier=expected_tier
            ):
                raise OfficialMarketAdapterError(
                    "capture request URL is not canonical"
                )
            status = _plain_int(
                "capture HTTP status",
                attempt["http_status"],
                minimum=100,
                maximum=599,
            )
            before_wall = _plain_int(
                "capture wall before",
                attempt["fetch_wall_ns_before"],
                minimum=0,
            )
            _plain_int(
                "capture wall after",
                attempt["fetch_wall_ns_after"],
                minimum=before_wall,
            )
            before_mono = _plain_int(
                "capture monotonic before",
                attempt["fetch_monotonic_ns_before"],
                minimum=0,
            )
            _plain_int(
                "capture monotonic after",
                attempt["fetch_monotonic_ns_after"],
                minimum=before_mono,
            )
            raw_name = _text(
                "capture raw path", attempt["raw_relative_path"]
            )
            if (
                SAFE_RAW_NAME_RE.fullmatch(raw_name) is None
                or raw_name
                != _response_filename(capture_index, expected_tier)
            ):
                raise OfficialMarketAdapterError(
                    "capture raw path is unsafe or noncanonical"
                )
            _plain_int(
                "capture raw size",
                attempt["raw_size"],
                minimum=0,
                maximum=MAX_RESPONSE_BYTES,
            )
            _sha("capture raw SHA", attempt["raw_sha256"])
            if attempt["adapter_code_sha256"] != value[
                "adapter_code_sha256"
            ]:
                raise OfficialMarketAdapterError(
                    "capture attempt adapter code binding mismatch"
                )
            if attempt["adapter_config_sha256"] != ADAPTER_CONFIG_SHA256:
                raise OfficialMarketAdapterError(
                    "capture attempt adapter config binding mismatch"
                )
            if attempt_index == selected_index and status != 200:
                raise OfficialMarketAdapterError(
                    "selected capture attempt is not HTTP 200"
                )
        if len(attempts) == 1:
            if attempts[0]["http_status"] != 200 or selected_index != 0:
                raise OfficialMarketAdapterError(
                    "single current attempt must be selected HTTP 200"
                )
        else:
            if (
                attempts[0]["http_status"] != 404
                or attempts[1]["http_status"] != 200
                or selected_index != 1
            ):
                raise OfficialMarketAdapterError(
                    "historical fallback is allowed only after current 404"
                )
    return dict(value)


def _validate_raw_pins(
    value: object,
    *,
    raw_sha256: str,
    capture_receipt_raw_sha256: str,
    authority_raw_sha256: str,
    adapter_code_sha256_value: str,
) -> dict[str, Any]:
    _sha("raw pins raw SHA", raw_sha256)
    if not isinstance(value, Mapping):
        raise OfficialMarketAdapterError("raw pins must be object")
    _exact_keys(
        "raw pins",
        value,
        {
            "schema_version",
            "authority_raw_sha256",
            "capture_receipt_raw_sha256",
            "adapter_code_sha256",
            "adapter_config_sha256",
            "selected_responses",
        },
    )
    if value["schema_version"] != RAW_PINS_SCHEMA:
        raise OfficialMarketAdapterError("wrong raw pins schema")
    if value["authority_raw_sha256"] != authority_raw_sha256:
        raise OfficialMarketAdapterError(
            "raw pins authority binding mismatch"
        )
    if (
        value["capture_receipt_raw_sha256"]
        != capture_receipt_raw_sha256
    ):
        raise OfficialMarketAdapterError(
            "raw pins capture receipt binding mismatch"
        )
    if value["adapter_code_sha256"] != adapter_code_sha256_value:
        raise OfficialMarketAdapterError(
            "raw pins adapter code binding mismatch"
        )
    if value["adapter_config_sha256"] != ADAPTER_CONFIG_SHA256:
        raise OfficialMarketAdapterError(
            "raw pins adapter config binding mismatch"
        )
    pins = value["selected_responses"]
    if not isinstance(pins, list) or not pins:
        raise OfficialMarketAdapterError(
            "raw pins selected responses must be nonempty"
        )
    previous = ""
    for index, pin in enumerate(pins):
        if not isinstance(pin, Mapping):
            raise OfficialMarketAdapterError("raw response pin must be object")
        _exact_keys(
            f"selected_responses[{index}]",
            pin,
            {
                "ticker",
                "source_tier",
                "request_url",
                "http_status",
                "raw_size",
                "raw_sha256",
            },
        )
        ticker = _require_ticker("raw pin ticker", pin["ticker"])
        if previous and ticker <= previous:
            raise OfficialMarketAdapterError(
                "raw pin tickers must be sorted and unique"
            )
        previous = ticker
        if pin["source_tier"] not in {"current", "historical"}:
            raise OfficialMarketAdapterError("raw pin source tier invalid")
        if pin["request_url"] != _market_url(
            ticker, source_tier=pin["source_tier"]
        ):
            raise OfficialMarketAdapterError("raw pin URL invalid")
        if pin["http_status"] != 200:
            raise OfficialMarketAdapterError(
                "only selected HTTP 200 responses may be pinned"
            )
        _plain_int(
            "raw pin size",
            pin["raw_size"],
            minimum=0,
            maximum=MAX_RESPONSE_BYTES,
        )
        _sha("raw pin SHA", pin["raw_sha256"])
    return dict(value)


def _read_exact_raw(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    require_root_read_only: bool = False,
) -> bytes:
    if not path.is_absolute():
        raise OfficialMarketAdapterError(
            "raw response path must be absolute"
        )
    _require_no_symlink_components(path, label="raw response path")
    try:
        before = path.stat()
    except OSError as exc:
        raise OfficialMarketAdapterError(
            "raw response cannot be stat'ed"
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        raise OfficialMarketAdapterError(
            "raw response must be a regular file"
        )
    if require_root_read_only:
        if before.st_uid != 0:
            raise OfficialMarketAdapterError(
                "raw response must be root-owned"
            )
        if before.st_mode & 0o222:
            raise OfficialMarketAdapterError(
                "raw response must have no write bits"
            )
    if before.st_size != expected_size:
        raise OfficialMarketAdapterError("raw response size mismatch")
    raw = path.read_bytes()
    after = path.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise OfficialMarketAdapterError(
            "raw response changed while being read"
        )
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise OfficialMarketAdapterError("raw response SHA mismatch")
    return raw


def normalize_capture(
    *,
    authority: Mapping[str, Any],
    authority_raw_sha256: str,
    capture_receipt: Mapping[str, Any],
    capture_receipt_raw_sha256: str,
    capture_dir: Path,
    raw_pins: Mapping[str, Any],
    raw_pins_raw_sha256: str,
    expected_code_sha256: str,
    require_root_read_only_raw: bool = False,
) -> dict[str, Any]:
    """Normalize externally pinned raw responses without any network access."""

    code_before = adapter_code_sha256()
    if code_before != expected_code_sha256:
        raise OfficialMarketAdapterError(
            "live adapter code does not match external code pin"
        )
    authority = validate_authority(
        authority,
        raw_sha256=authority_raw_sha256,
        expected_code_sha256=expected_code_sha256,
        now_wall_ns=None,
    )
    capture = _validate_capture_receipt(capture_receipt)
    if capture["authority_raw_sha256"] != authority_raw_sha256:
        raise OfficialMarketAdapterError(
            "capture receipt authority binding mismatch"
        )
    if capture["authority_id"] != authority["authority_id"]:
        raise OfficialMarketAdapterError(
            "capture receipt authority id mismatch"
        )
    if capture["adapter_code_sha256"] != code_before:
        raise OfficialMarketAdapterError(
            "capture receipt adapter code mismatch"
        )
    raw_pins = _validate_raw_pins(
        raw_pins,
        raw_sha256=raw_pins_raw_sha256,
        capture_receipt_raw_sha256=capture_receipt_raw_sha256,
        authority_raw_sha256=authority_raw_sha256,
        adapter_code_sha256_value=code_before,
    )
    capture_tickers = [item["ticker"] for item in capture["captures"]]
    if capture_tickers != authority["tickers"]:
        raise OfficialMarketAdapterError(
            "capture ticker set differs from authority"
        )
    pin_by_ticker = {
        pin["ticker"]: pin for pin in raw_pins["selected_responses"]
    }
    if sorted(pin_by_ticker) != capture_tickers:
        raise OfficialMarketAdapterError(
            "raw pin ticker set differs from capture"
        )
    if not capture_dir.is_absolute() or capture_dir.is_symlink():
        raise OfficialMarketAdapterError(
            "capture directory must be absolute and not a symlink"
        )
    settlements: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    raw_evidence: list[dict[str, Any]] = []
    for capture_entry in capture["captures"]:
        ticker = capture_entry["ticker"]
        attempts = capture_entry["attempts"]
        for attempt in attempts:
            raw = _read_exact_raw(
                capture_dir / attempt["raw_relative_path"],
                expected_size=attempt["raw_size"],
                expected_sha256=attempt["raw_sha256"],
                require_root_read_only=require_root_read_only_raw,
            )
            raw_evidence.append(
                {
                    "ticker": ticker,
                    **dict(attempt),
                    "raw_reverified_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        selected = attempts[capture_entry["selected_attempt_index"]]
        pin = pin_by_ticker[ticker]
        for key in (
            "source_tier",
            "request_url",
            "http_status",
            "raw_size",
            "raw_sha256",
        ):
            if pin[key] != selected[key]:
                raise OfficialMarketAdapterError(
                    f"raw pin differs from selected capture for {ticker}: {key}"
                )
        selected_raw = _read_exact_raw(
            capture_dir / selected["raw_relative_path"],
            expected_size=selected["raw_size"],
            expected_sha256=selected["raw_sha256"],
            require_root_read_only=require_root_read_only_raw,
        )
        parsed = _parse_final_market_response(
            selected_raw,
            expected_ticker=ticker,
        )
        settlement_id = (
            f"kalshi-official:{ticker}:{parsed['settlement_ts']}:"
            f"{selected['raw_sha256'][:16]}"
        )
        settlements.append(
            {
                "settlement_id": settlement_id,
                "market_ticker": ticker,
                "status": "FINALIZED",
                "finalized": True,
                "yes_settlement_value_e4": (
                    parsed["yes_settlement_value_e4"]
                ),
                "observed_at_ns": selected["fetch_wall_ns_after"],
                "revision": 0,
                "source_sha256": selected["raw_sha256"],
            }
        )
        metadata.append(
            {
                "market_ticker": ticker,
                "source_tier": selected["source_tier"],
                "request_url": selected["request_url"],
                "raw_response_sha256": selected["raw_sha256"],
                "observed_wall_ns_before": selected[
                    "fetch_wall_ns_before"
                ],
                "observed_wall_ns_after": selected["fetch_wall_ns_after"],
                "observed_monotonic_ns_before": selected[
                    "fetch_monotonic_ns_before"
                ],
                "observed_monotonic_ns_after": selected[
                    "fetch_monotonic_ns_after"
                ],
                "market_status": parsed["status"],
                "official_result": parsed["result"],
                "yes_settlement_value_e4": (
                    parsed["yes_settlement_value_e4"]
                ),
                "settlement_ts": parsed["settlement_ts"],
                "price_level_structure": parsed[
                    "price_level_structure"
                ],
                "price_ranges": parsed["price_ranges"],
                "tick_size_e4": parsed["tick_size_e4"],
                "open_time": parsed["open_time"],
                "close_time": parsed["close_time"],
                "expected_expiration_time": parsed[
                    "expected_expiration_time"
                ],
                "occurrence_datetime": parsed[
                    "occurrence_datetime"
                ],
                "metadata_asof_semantics": (
                    "OBSERVED_AT_FETCH_NOT_HISTORICAL_AS_OF"
                ),
                "historical_asof_utc": None,
                "scheduled_start_ts_ns": None,
                "scheduled_start_field_mapping": (
                    "BLOCKED_NO_AUTHORIZED_SCHEDULED_START_MAPPING"
                ),
                "point_in_time_lifecycle_interval": None,
                "point_in_time_tick_interval": None,
            }
        )
    if adapter_code_sha256() != code_before:
        raise OfficialMarketAdapterError(
            "adapter code changed during normalization"
        )
    return {
        "schema_version": OUTPUT_SCHEMA,
        "authority_raw_sha256": authority_raw_sha256,
        "capture_receipt_raw_sha256": capture_receipt_raw_sha256,
        "raw_pins_raw_sha256": raw_pins_raw_sha256,
        "adapter_code_sha256": code_before,
        "adapter_config_sha256": ADAPTER_CONFIG_SHA256,
        "terminal_records": settlements,
        "metadata_evidence": metadata,
        "raw_response_evidence": raw_evidence,
        "resolved_capabilities": [
            "OFFICIAL_FINALIZED_YES_NO_RESULT",
            "EXACT_YES_PAYOUT_E4",
            "OFFICIAL_SETTLEMENT_TIMESTAMP",
            "TERMINAL_OBSERVATION_CLOCK_BRACKET",
            "TERMINAL_OBSERVED_TICK_TABLE",
            "TERMINAL_OBSERVED_LIFECYCLE_TIMESTAMPS",
        ],
        "remaining_blockers": [
            "BLOCK_A01_HISTORICAL_POINT_IN_TIME_METADATA_INTERVALS_MISSING",
            "BLOCK_A01_SCHEDULED_START_AUTHORITY_MISSING",
            "BLOCK_A01_EXCHANGE_SETTLEMENT_REVISION_SEQUENCE_UNAVAILABLE",
        ],
        "prohibitions": [
            (
                "occurrence_datetime, expected_expiration_time and "
                "close_time are not scheduled_start"
            ),
            (
                "fetch observation time is not a historical metadata "
                "as-of timestamp"
            ),
        ],
    }


def _read_pinned_file(
    path_text: str,
    expected_sha256: str,
    *,
    label: str,
    require_root_read_only: bool,
) -> bytes:
    expected_sha256 = _sha(f"{label} expected SHA", expected_sha256)
    path = Path(path_text)
    if not path.is_absolute():
        raise OfficialMarketAdapterError(
            f"{label} must be an absolute path"
        )
    _require_no_symlink_components(path, label=label)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise OfficialMarketAdapterError(
            f"{label} cannot be opened safely"
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise OfficialMarketAdapterError(
                f"{label} must be a regular file"
            )
        if require_root_read_only:
            if before.st_uid != 0:
                raise OfficialMarketAdapterError(
                    f"{label} must be root-owned"
                )
            if before.st_mode & 0o222:
                raise OfficialMarketAdapterError(
                    f"{label} must have no write bits"
                )
            current = path.parent
            while True:
                parent_info = current.lstat()
                if parent_info.st_uid != 0:
                    raise OfficialMarketAdapterError(
                        f"{label} parent tree must be root-owned"
                    )
                if parent_info.st_mode & 0o022:
                    raise OfficialMarketAdapterError(
                        f"{label} parent tree must not be group/world writable"
                    )
                if current == Path("/"):
                    break
                current = current.parent
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, READ_CHUNK_BYTES)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise OfficialMarketAdapterError(
                f"{label} changed while being read"
            )
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise OfficialMarketAdapterError(f"{label} raw SHA mismatch")
    return raw


def _require_non_root() -> None:
    if os.geteuid() == 0:
        raise OfficialMarketAdapterError(
            "adapter production commands must run as non-root"
        )


def _require_root_deployed_adapter() -> None:
    path = Path(__file__)
    _require_no_symlink_components(path, label="deployed adapter source")
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o222:
        raise OfficialMarketAdapterError(
            "deployed adapter must be root-owned with no write bits"
        )
    current = path.parent
    while True:
        parent_info = current.lstat()
        if parent_info.st_uid != 0 or parent_info.st_mode & 0o022:
            raise OfficialMarketAdapterError(
                "deployed adapter parent tree must be root-owned and sealed"
            )
        if current == Path("/"):
            break
        current = current.parent


def _raw_sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_json(raw: bytes, *, label: str) -> Mapping[str, Any]:
    value = parse_strict_json(raw, label=label)
    if not isinstance(value, Mapping):
        raise OfficialMarketAdapterError(f"{label} must be an object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only official market terminal evidence adapter"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "pins",
        help="print current adapter code/config SHA values",
    )

    capture = subparsers.add_parser("capture")
    capture.add_argument("--authority", required=True)
    capture.add_argument("--authority-sha256", required=True)
    capture.add_argument("--expected-adapter-code-sha256", required=True)
    capture.add_argument("--output-dir", required=True)

    normalize = subparsers.add_parser("normalize")
    normalize.add_argument("--authority", required=True)
    normalize.add_argument("--authority-sha256", required=True)
    normalize.add_argument("--capture-receipt", required=True)
    normalize.add_argument("--capture-receipt-sha256", required=True)
    normalize.add_argument("--raw-pins", required=True)
    normalize.add_argument("--raw-pins-sha256", required=True)
    normalize.add_argument("--expected-adapter-code-sha256", required=True)
    normalize.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "pins":
        print(
            json.dumps(
                {
                    "adapter_code_sha256": adapter_code_sha256(),
                    "adapter_config_sha256": ADAPTER_CONFIG_SHA256,
                },
                sort_keys=True,
            )
        )
        return 0

    _require_non_root()
    _require_root_deployed_adapter()
    code_sha = _sha(
        "expected adapter code SHA",
        args.expected_adapter_code_sha256,
    )
    authority_raw = _read_pinned_file(
        args.authority,
        args.authority_sha256,
        label="authority",
        require_root_read_only=True,
    )
    authority = _load_json(authority_raw, label="authority")

    if args.command == "capture":
        _, receipt_raw_sha = capture_markets(
            authority,
            authority_raw_sha256=_raw_sha(authority_raw),
            expected_code_sha256=code_sha,
            output_dir=Path(args.output_dir),
        )
        print(
            json.dumps(
                {
                    "capture_receipt_raw_sha256": receipt_raw_sha,
                    "network_mutations": 0,
                    "financial_mutations": 0,
                },
                sort_keys=True,
            )
        )
        return 0

    capture_raw = _read_pinned_file(
        args.capture_receipt,
        args.capture_receipt_sha256,
        label="capture receipt",
        require_root_read_only=True,
    )
    pins_raw = _read_pinned_file(
        args.raw_pins,
        args.raw_pins_sha256,
        label="raw pins",
        require_root_read_only=True,
    )
    output = normalize_capture(
        authority=authority,
        authority_raw_sha256=_raw_sha(authority_raw),
        capture_receipt=_load_json(
            capture_raw, label="capture receipt"
        ),
        capture_receipt_raw_sha256=_raw_sha(capture_raw),
        capture_dir=Path(args.capture_receipt).parent,
        raw_pins=_load_json(pins_raw, label="raw pins"),
        raw_pins_raw_sha256=_raw_sha(pins_raw),
        expected_code_sha256=code_sha,
        require_root_read_only_raw=True,
    )
    payload = canonical_json_bytes(output) + b"\n"
    output_raw_sha = _write_create_once(Path(args.output), payload)
    print(
        json.dumps(
            {
                "output_canonical_sha256": canonical_sha256(output),
                "output_raw_sha256": output_raw_sha,
                "terminal_record_count": len(output["terminal_records"]),
                "network_calls": 0,
                "financial_mutations": 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OfficialMarketAdapterError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2)
