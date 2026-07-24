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
MAX_TICKERS_PER_AUTHORITY = 50
MIN_REQUEST_INTERVAL_NS = 250_000_000
MAX_429_RETRIES_PER_ENDPOINT = 2
MIN_RETRY_AFTER_SECONDS = 1
MAX_RETRY_AFTER_SECONDS = 30
OFFICIAL_NUMBER_MAX_TOKEN_LENGTH = 32
OFFICIAL_NUMBER_MAX_TOTAL_DIGITS = 21
OFFICIAL_NUMBER_MAX_PRECISION = 19
OFFICIAL_NUMBER_MAX_ABSOLUTE_EXPONENT = 18
OFFICIAL_NUMBER_MAX_ABSOLUTE_MAGNITUDE_TEXT = "9223372036854775807"
OFFICIAL_NUMBER_MAX_ABSOLUTE_MAGNITUDE = Decimal(
    OFFICIAL_NUMBER_MAX_ABSOLUTE_MAGNITUDE_TEXT
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,199}$")
UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,9})?Z$"
)
DOLLARS_4_RE = re.compile(r"^(?:0|1)\.\d{4}$")
SAFE_RAW_NAME_RE = re.compile(
    r"^\d{4}\.\d{4}\.(?:current|historical)\.response\.json$"
)
NEGATIVE_ZERO_NUMBER_RE = re.compile(
    r"^-0(?:\.0*)?(?:[eE][+-]?[0-9]+)?$"
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
    "rate_limit_policy": {
        "max_tickers_per_authority": MAX_TICKERS_PER_AUTHORITY,
        "minimum_request_interval_ns": MIN_REQUEST_INTERVAL_NS,
        "http_429": (
            "RETRY_SAME_ENDPOINT_ONLY_WITH_BOUNDED_INTEGER_RETRY_AFTER"
        ),
        "max_429_retries_per_endpoint": MAX_429_RETRIES_PER_ENDPOINT,
        "min_retry_after_seconds": MIN_RETRY_AFTER_SECONDS,
        "max_retry_after_seconds": MAX_RETRY_AFTER_SECONDS,
        "every_http_attempt_raw_capture": "CREATE_ONCE",
        "historical_fallback_on_429": "FORBIDDEN",
        "production_sharding": (
            "ISSUE_MULTIPLE_EXACT_AUTHORITIES_ABOVE_MAX_TICKERS"
        ),
    },
    "terminal_rule": "FINALIZED_YES_NO_EXACT_PAYOUT_ONLY",
    "metadata_temporality": "OBSERVED_AT_FETCH_NOT_HISTORICAL_AS_OF",
    "scheduled_start_mapping": "FORBIDDEN",
    "control_receipt_noninteger_numbers": "FORBIDDEN",
    "official_response_numeric_policy": {
        "sample_basis": (
            "RETAINED_W09_666_RESPONSES_FLOATS_0.5_1.5_3.5_7.5"
            "_WITH_SIGNED_INT64_HEADROOM"
        ),
        "integer_parser": "BOUNDED_PLAIN_INTEGER",
        "noninteger_parser": "BOUNDED_EXACT_DECIMAL_NO_ROUNDING",
        "negative_zero": "FORBIDDEN_ALL_LEXICAL_FORMS",
        "maximum_token_length": OFFICIAL_NUMBER_MAX_TOKEN_LENGTH,
        "maximum_total_digits": OFFICIAL_NUMBER_MAX_TOTAL_DIGITS,
        "maximum_decimal_precision": OFFICIAL_NUMBER_MAX_PRECISION,
        "maximum_absolute_exponent": (
            OFFICIAL_NUMBER_MAX_ABSOLUTE_EXPONENT
        ),
        "maximum_absolute_magnitude": (
            OFFICIAL_NUMBER_MAX_ABSOLUTE_MAGNITUDE_TEXT
        ),
        "promotion_to_control_or_receipt": "FORBIDDEN",
    },
}


class OfficialMarketAdapterError(ValueError):
    """The read-only transport or evidence contract failed closed."""


def _canonical_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return value
    if isinstance(value, (float, Decimal)):
        raise TypeError(
            "floating or Decimal source numbers are forbidden in canonical "
            "control and receipt values"
        )
    if isinstance(value, Mapping) and all(
        isinstance(key, str) for key in value
    ):
        return {
            key: _canonical_value(value[key])
            for key in sorted(value)
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise TypeError(f"unsupported canonical value {type(value).__name__}")


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


def _validate_bounded_official_number(value: str) -> Decimal:
    """Validate one official-response JSON number inside the config envelope.

    The retained W09 corpus contains additive ``floor_strike`` values
    0.5/1.5/3.5/7.5.  The wider signed-int64 decimal envelope is deliberately
    bounded in ``ADAPTER_CONFIG``.  Validation happens on the lexical token
    before any Python integer conversion, binary float, rounding, or Decimal
    context operation.
    """

    if NEGATIVE_ZERO_NUMBER_RE.fullmatch(value) is not None:
        raise OfficialMarketAdapterError(
            f"negative-zero JSON numeric value is forbidden: {value}"
        )
    if len(value) > OFFICIAL_NUMBER_MAX_TOKEN_LENGTH:
        raise OfficialMarketAdapterError(
            "official JSON numeric token exceeds configured length"
        )
    total_digits = sum(character.isdigit() for character in value)
    if total_digits > OFFICIAL_NUMBER_MAX_TOTAL_DIGITS:
        raise OfficialMarketAdapterError(
            "official JSON numeric token exceeds configured total digits"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise OfficialMarketAdapterError(
            f"invalid JSON numeric value: {value}"
        ) from exc
    if not parsed.is_finite():
        raise OfficialMarketAdapterError(
            f"non-finite JSON numeric value is forbidden: {value}"
        )
    if parsed.is_zero() and parsed.is_signed():
        raise OfficialMarketAdapterError(
            f"negative-zero JSON numeric value is forbidden: {value}"
        )
    sign, digits, exponent = parsed.as_tuple()
    del sign
    if len(digits) > OFFICIAL_NUMBER_MAX_PRECISION:
        raise OfficialMarketAdapterError(
            "official JSON numeric token exceeds configured precision"
        )
    if abs(exponent) > OFFICIAL_NUMBER_MAX_ABSOLUTE_EXPONENT:
        raise OfficialMarketAdapterError(
            "official JSON numeric token exceeds configured exponent"
        )
    if abs(parsed) > OFFICIAL_NUMBER_MAX_ABSOLUTE_MAGNITUDE:
        raise OfficialMarketAdapterError(
            "official JSON numeric token exceeds configured magnitude"
        )
    return parsed


def _parse_bounded_official_integer(value: str) -> int:
    parsed = _validate_bounded_official_number(value)
    if parsed != parsed.to_integral_value():
        raise OfficialMarketAdapterError(
            "official JSON integer token is not integral"
        )
    return int(value)


def _parse_bounded_official_decimal(value: str) -> Decimal:
    return _validate_bounded_official_number(value)


def parse_strict_json(
    raw: bytes,
    *,
    label: str,
    allow_finite_numbers: bool = False,
) -> object:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OfficialMarketAdapterError(
            f"{label} is not UTF-8"
        ) from exc
    try:
        parse_float = _reject_float
        parse_int = int
        if allow_finite_numbers:
            parse_float = _parse_bounded_official_decimal
            parse_int = _parse_bounded_official_integer
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_float=parse_float,
            parse_int=parse_int,
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
    if len(tickers_raw) > MAX_TICKERS_PER_AUTHORITY:
        raise OfficialMarketAdapterError(
            "authority exceeds bounded production shard size; "
            "issue multiple exact authorities"
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
    retry_after: str | None = None


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


def _canonical_request_identity(url: str) -> tuple[str, str]:
    """Return ``(source_tier, ticker)`` only for one exact canonical URL."""

    if not isinstance(url, str) or not url:
        raise OfficialMarketAdapterError("request URL must be nonempty text")
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise OfficialMarketAdapterError(
            "request URL has an invalid port"
        ) from exc
    if (
        parts.scheme != "https"
        or parts.hostname != OFFICIAL_HOST
        or parts.netloc != OFFICIAL_HOST
        or port is not None
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise OfficialMarketAdapterError(
            "request URL is outside the exact fixed origin"
        )
    raw_path_lower = parts.path.lower()
    if "\\" in parts.path or re.search(
        r"%(?:2f|5c|2e)", raw_path_lower
    ):
        raise OfficialMarketAdapterError(
            "encoded slash, backslash or dot is forbidden in request path"
        )
    try:
        decoded_path = urllib.parse.unquote_to_bytes(parts.path).decode(
            "ascii"
        )
    except UnicodeDecodeError as exc:
        raise OfficialMarketAdapterError(
            "request path must be canonical ASCII"
        ) from exc
    if any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise OfficialMarketAdapterError(
            "dot segments are forbidden in request path"
        )
    matched: tuple[str, str] | None = None
    for source_tier, prefix in (
        ("current", CURRENT_MARKET_PATH),
        ("historical", HISTORICAL_MARKET_PATH),
    ):
        if not parts.path.startswith(prefix):
            continue
        suffix = parts.path[len(prefix) :]
        if (
            not suffix
            or "/" in suffix
            or "\\" in suffix
            or "%" in suffix
        ):
            raise OfficialMarketAdapterError(
                "request path is not one exact canonical market path"
            )
        ticker = _require_ticker("request URL ticker", suffix)
        matched = (source_tier, ticker)
        break
    if matched is None:
        raise OfficialMarketAdapterError(
            "request URL is outside fixed market paths"
        )
    source_tier, ticker = matched
    if url != _market_url(ticker, source_tier=source_tier):
        raise OfficialMarketAdapterError(
            "request URL is not its exact canonical reconstruction"
        )
    return source_tier, ticker


def _build_request(url: str) -> urllib.request.Request:
    _canonical_request_identity(url)
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


def _single_retry_after(headers: Any) -> str | None:
    if headers is None:
        return None
    if hasattr(headers, "get_all"):
        values = headers.get_all("Retry-After", [])
    else:
        value = headers.get("Retry-After")
        values = [] if value is None else [value]
    if len(values) > 1:
        raise OfficialMarketAdapterError(
            "multiple Retry-After headers are forbidden"
        )
    if not values:
        return None
    value = values[0]
    if not isinstance(value, str):
        raise OfficialMarketAdapterError(
            "Retry-After header must be text"
        )
    return value


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
        retry_after = _single_retry_after(exc.headers)
        try:
            body = _read_bounded(exc)
        finally:
            exc.close()
        return HttpResult(
            status=int(exc.code),
            final_url=exc.geturl(),
            body=body,
            retry_after=retry_after,
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
        retry_after = _single_retry_after(response.headers)
        body = _read_bounded(response)
    finally:
        response.close()
    return HttpResult(
        status=status,
        final_url=url,
        body=body,
        retry_after=retry_after,
    )


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


def _response_filename(
    ticker_index: int,
    batch_attempt_index: int,
    source_tier: str,
) -> str:
    if source_tier not in {"current", "historical"}:
        raise OfficialMarketAdapterError("invalid response source tier")
    return (
        f"{ticker_index:04d}.{batch_attempt_index:04d}."
        f"{source_tier}.response.json"
    )


class Sleeper(Protocol):
    def __call__(self, seconds: float) -> None:
        ...


class _CaptureTimeline:
    """One batch-wide authority window and non-regressing clock ledger."""

    def __init__(
        self,
        *,
        issued_wall_ns: int,
        expires_wall_ns: int,
        initial_wall_ns: int,
        initial_monotonic_ns: int,
        clock: Clock,
        sleeper: Sleeper,
    ) -> None:
        self.issued_wall_ns = issued_wall_ns
        self.expires_wall_ns = expires_wall_ns
        self.clock = clock
        self.sleeper = sleeper
        self.last_wall_ns: int | None = None
        self.last_monotonic_ns: int | None = None
        self.last_attempt_after_monotonic_ns: int | None = None
        self.next_batch_attempt_index = 0
        self._observe(
            initial_wall_ns,
            initial_monotonic_ns,
            label="batch authority validation",
        )

    def _observe(self, wall_ns: int, monotonic_ns: int, *, label: str) -> None:
        _plain_int(f"{label} wall", wall_ns, minimum=0)
        _plain_int(f"{label} monotonic", monotonic_ns, minimum=0)
        if not self.issued_wall_ns <= wall_ns <= self.expires_wall_ns:
            raise OfficialMarketAdapterError(
                f"{label} is outside authority validity window"
            )
        if self.last_wall_ns is not None and wall_ns < self.last_wall_ns:
            raise OfficialMarketAdapterError(
                f"{label} wall clock regressed across batch"
            )
        if (
            self.last_monotonic_ns is not None
            and monotonic_ns < self.last_monotonic_ns
        ):
            raise OfficialMarketAdapterError(
                f"{label} monotonic clock regressed across batch"
            )
        self.last_wall_ns = wall_ns
        self.last_monotonic_ns = monotonic_ns

    def before_attempt(
        self,
        *,
        retry_delay_ns: int,
    ) -> tuple[int, int, int, int]:
        retry_delay_ns = _plain_int(
            "retry delay", retry_delay_ns, minimum=0
        )
        wall_ns, monotonic_ns = self.clock()
        self._observe(
            wall_ns,
            monotonic_ns,
            label="HTTP attempt before",
        )
        required_delay_ns = 0
        if self.last_attempt_after_monotonic_ns is not None:
            required_delay_ns = max(
                MIN_REQUEST_INTERVAL_NS,
                retry_delay_ns,
            )
            elapsed = (
                monotonic_ns - self.last_attempt_after_monotonic_ns
            )
            if elapsed < required_delay_ns:
                remaining_ns = required_delay_ns - elapsed
                if wall_ns + remaining_ns > self.expires_wall_ns:
                    raise OfficialMarketAdapterError(
                        "required throttle/retry delay exceeds authority expiry"
                    )
                self.sleeper(remaining_ns / 1_000_000_000)
                wall_ns, monotonic_ns = self.clock()
                self._observe(
                    wall_ns,
                    monotonic_ns,
                    label="HTTP attempt after throttle",
                )
                elapsed = (
                    monotonic_ns - self.last_attempt_after_monotonic_ns
                )
            if elapsed < required_delay_ns:
                raise OfficialMarketAdapterError(
                    "clock does not prove required request throttle"
                )
        batch_attempt_index = self.next_batch_attempt_index
        self.next_batch_attempt_index += 1
        return (
            batch_attempt_index,
            wall_ns,
            monotonic_ns,
            required_delay_ns,
        )

    def after_attempt(
        self,
        *,
        wall_ns: int,
        monotonic_ns: int,
        before_wall_ns: int,
        before_monotonic_ns: int,
    ) -> None:
        self._observe(
            wall_ns,
            monotonic_ns,
            label="HTTP attempt after",
        )
        if wall_ns < before_wall_ns:
            raise OfficialMarketAdapterError(
                "HTTP attempt wall clock regressed"
            )
        if monotonic_ns < before_monotonic_ns:
            raise OfficialMarketAdapterError(
                "HTTP attempt monotonic clock regressed"
            )
        self.last_attempt_after_monotonic_ns = monotonic_ns


def _capture_one_attempt(
    *,
    ticker: str,
    ticker_index: int,
    source_tier: str,
    endpoint_attempt_index: int,
    retry_delay_ns: int,
    output_dir: Path,
    transport: Transport,
    timeline: _CaptureTimeline,
) -> dict[str, Any]:
    url = _market_url(ticker, source_tier=source_tier)
    (
        batch_attempt_index,
        wall_before,
        mono_before,
        required_delay_ns,
    ) = timeline.before_attempt(retry_delay_ns=retry_delay_ns)
    result = transport(url, TIMEOUT_SECONDS)
    wall_after, mono_after = timeline.clock()
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
    if result.retry_after is not None and not isinstance(
        result.retry_after, str
    ):
        raise OfficialMarketAdapterError(
            "Retry-After transport value must be text"
        )
    relative = _response_filename(
        ticker_index,
        batch_attempt_index,
        source_tier,
    )
    raw_sha = _write_create_once(
        output_dir / relative,
        result.body,
    )
    # The response is durably captured even if the post-request clock proves
    # that authority expired during this HTTP attempt.  The batch still fails
    # closed and no successful CAPTURE_RECEIPT is emitted.
    timeline.after_attempt(
        wall_ns=wall_after,
        monotonic_ns=mono_after,
        before_wall_ns=wall_before,
        before_monotonic_ns=mono_before,
    )
    attempt = {
        "ticker": ticker,
        "source_tier": source_tier,
        "endpoint_attempt_index": endpoint_attempt_index,
        "batch_attempt_index": batch_attempt_index,
        "request_url": url,
        "http_status": status,
        "required_delay_from_prior_attempt_ns": required_delay_ns,
        "retry_after_seconds": None,
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
    if status != 429 and result.retry_after is not None:
        raise OfficialMarketAdapterError(
            "Retry-After is accepted only on HTTP 429"
        )
    if status == 429:
        header = result.retry_after
        if header is None or re.fullmatch(r"[1-9][0-9]*", header) is None:
            raise OfficialMarketAdapterError(
                "HTTP 429 requires one integer Retry-After header"
            )
        retry_seconds = int(header)
        if not (
            MIN_RETRY_AFTER_SECONDS
            <= retry_seconds
            <= MAX_RETRY_AFTER_SECONDS
        ):
            raise OfficialMarketAdapterError(
                "HTTP 429 Retry-After is outside bounded policy"
            )
        attempt["retry_after_seconds"] = retry_seconds
    return attempt


def _capture_endpoint(
    *,
    ticker: str,
    ticker_index: int,
    source_tier: str,
    output_dir: Path,
    transport: Transport,
    timeline: _CaptureTimeline,
) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    retry_delay_ns = 0
    for endpoint_attempt_index in range(
        MAX_429_RETRIES_PER_ENDPOINT + 1
    ):
        attempt = _capture_one_attempt(
            ticker=ticker,
            ticker_index=ticker_index,
            source_tier=source_tier,
            endpoint_attempt_index=endpoint_attempt_index,
            retry_delay_ns=retry_delay_ns,
            output_dir=output_dir,
            transport=transport,
            timeline=timeline,
        )
        attempts.append(attempt)
        if attempt["http_status"] != 429:
            return attempts
        if endpoint_attempt_index >= MAX_429_RETRIES_PER_ENDPOINT:
            raise OfficialMarketAdapterError(
                f"{ticker} {source_tier} endpoint exhausted bounded "
                "HTTP 429 retries; issue a fresh sharded authority"
            )
        retry_delay_ns = attempt["retry_after_seconds"] * 1_000_000_000
        if (
            attempt["fetch_wall_ns_after"] + retry_delay_ns
            > timeline.expires_wall_ns
        ):
            raise OfficialMarketAdapterError(
                "HTTP 429 Retry-After would exceed authority expiry"
            )
    raise AssertionError("bounded endpoint retry loop fell through")


def capture_markets(
    authority: Mapping[str, Any],
    *,
    authority_raw_sha256: str,
    expected_code_sha256: str,
    output_dir: Path,
    transport: Transport = urllib_transport,
    clock: Clock = system_clock,
    sleeper: Sleeper = time.sleep,
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
    now_wall_ns, now_monotonic_ns = clock()
    authority = validate_authority(
        authority,
        raw_sha256=authority_raw_sha256,
        expected_code_sha256=expected_code_sha256,
        now_wall_ns=now_wall_ns,
    )
    issued_wall_ns = _timestamp_ns(
        "issued_at_utc", authority["issued_at_utc"]
    )
    expires_wall_ns = _timestamp_ns(
        "expires_at_utc", authority["expires_at_utc"]
    )
    timeline = _CaptureTimeline(
        issued_wall_ns=issued_wall_ns,
        expires_wall_ns=expires_wall_ns,
        initial_wall_ns=now_wall_ns,
        initial_monotonic_ns=now_monotonic_ns,
        clock=clock,
        sleeper=sleeper,
    )
    _safe_new_directory(output_dir)
    captures: list[dict[str, Any]] = []
    for index, ticker in enumerate(authority["tickers"]):
        current_attempts = _capture_endpoint(
            ticker=ticker,
            ticker_index=index,
            source_tier="current",
            output_dir=output_dir,
            transport=transport,
            timeline=timeline,
        )
        attempts = list(current_attempts)
        current_status = current_attempts[-1]["http_status"]
        if current_status == 404:
            historical_attempts = _capture_endpoint(
                ticker=ticker,
                ticker_index=index,
                source_tier="historical",
                output_dir=output_dir,
                transport=transport,
                timeline=timeline,
            )
            attempts.extend(historical_attempts)
            selected_index = len(attempts) - 1
        elif current_status == 200:
            selected_index = len(current_attempts) - 1
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
        "authority_issued_at_utc": authority["issued_at_utc"],
        "authority_expires_at_utc": authority["expires_at_utc"],
        "authority_issued_wall_ns": issued_wall_ns,
        "authority_expires_wall_ns": expires_wall_ns,
        "adapter_code_sha256": code_before,
        "adapter_config_sha256": ADAPTER_CONFIG_SHA256,
        "adapter_config": ADAPTER_CONFIG,
        "request_authentication": "NONE",
        "proxy_policy": "DISABLED",
        "redirect_policy": "REJECT",
        "temporality": "OBSERVED_AT_FETCH_NOT_HISTORICAL_AS_OF",
        "captures": captures,
    }
    receipt = _validate_capture_receipt(receipt)
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
    value = parse_strict_json(
        raw,
        label=f"market response {expected_ticker}",
        allow_finite_numbers=True,
    )
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
            "authority_issued_at_utc",
            "authority_expires_at_utc",
            "authority_issued_wall_ns",
            "authority_expires_wall_ns",
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
    issued_wall_ns = _timestamp_ns(
        "capture authority issued_at",
        value["authority_issued_at_utc"],
    )
    expires_wall_ns = _timestamp_ns(
        "capture authority expires_at",
        value["authority_expires_at_utc"],
    )
    if expires_wall_ns <= issued_wall_ns:
        raise OfficialMarketAdapterError(
            "capture authority expiry is not after issue"
        )
    if (
        value["authority_issued_wall_ns"] != issued_wall_ns
        or value["authority_expires_wall_ns"] != expires_wall_ns
    ):
        raise OfficialMarketAdapterError(
            "capture authority text/nanosecond bounds mismatch"
        )
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
    if len(captures) > MAX_TICKERS_PER_AUTHORITY:
        raise OfficialMarketAdapterError(
            "capture exceeds bounded authority shard size"
        )
    seen: set[str] = set()
    previous = ""
    previous_attempt: Mapping[str, Any] | None = None
    expected_batch_attempt_index = 0
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
        maximum_attempts = 2 * (
            MAX_429_RETRIES_PER_ENDPOINT + 1
        )
        if (
            not isinstance(attempts, list)
            or not attempts
            or len(attempts) > maximum_attempts
        ):
            raise OfficialMarketAdapterError(
                "capture attempt count exceeds bounded endpoint policy"
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
                    "endpoint_attempt_index",
                    "batch_attempt_index",
                    "request_url",
                    "http_status",
                    "required_delay_from_prior_attempt_ns",
                    "retry_after_seconds",
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
            source_tier = attempt["source_tier"]
            if source_tier not in {"current", "historical"}:
                raise OfficialMarketAdapterError(
                    "capture source tier is invalid"
                )
            if attempt["request_url"] != _market_url(
                ticker, source_tier=source_tier
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
            after_wall = _plain_int(
                "capture wall after",
                attempt["fetch_wall_ns_after"],
                minimum=before_wall,
            )
            before_mono = _plain_int(
                "capture monotonic before",
                attempt["fetch_monotonic_ns_before"],
                minimum=0,
            )
            after_mono = _plain_int(
                "capture monotonic after",
                attempt["fetch_monotonic_ns_after"],
                minimum=before_mono,
            )
            if not (
                issued_wall_ns
                <= before_wall
                <= after_wall
                <= expires_wall_ns
            ):
                raise OfficialMarketAdapterError(
                    "capture attempt is outside authority validity window"
                )
            batch_attempt_index = _plain_int(
                "batch_attempt_index",
                attempt["batch_attempt_index"],
                minimum=0,
            )
            if batch_attempt_index != expected_batch_attempt_index:
                raise OfficialMarketAdapterError(
                    "capture batch attempt indexes are not contiguous"
                )
            expected_batch_attempt_index += 1
            endpoint_attempt_index = _plain_int(
                "endpoint_attempt_index",
                attempt["endpoint_attempt_index"],
                minimum=0,
                maximum=MAX_429_RETRIES_PER_ENDPOINT,
            )
            expected_delay_ns = 0
            if previous_attempt is not None:
                previous_after_wall = previous_attempt[
                    "fetch_wall_ns_after"
                ]
                previous_after_mono = previous_attempt[
                    "fetch_monotonic_ns_after"
                ]
                if (
                    before_wall < previous_after_wall
                    or before_mono < previous_after_mono
                ):
                    raise OfficialMarketAdapterError(
                        "capture clocks regressed across HTTP attempts"
                    )
                expected_delay_ns = MIN_REQUEST_INTERVAL_NS
                if previous_attempt["http_status"] == 429:
                    previous_retry = previous_attempt[
                        "retry_after_seconds"
                    ]
                    if type(previous_retry) is not int:
                        raise OfficialMarketAdapterError(
                            "prior HTTP 429 lacks retry delay"
                        )
                    expected_delay_ns = max(
                        expected_delay_ns,
                        previous_retry * 1_000_000_000,
                    )
                if before_mono - previous_after_mono < expected_delay_ns:
                    raise OfficialMarketAdapterError(
                        "capture does not prove required request throttle"
                    )
            declared_delay_ns = _plain_int(
                "required_delay_from_prior_attempt_ns",
                attempt["required_delay_from_prior_attempt_ns"],
                minimum=0,
            )
            if declared_delay_ns != expected_delay_ns:
                raise OfficialMarketAdapterError(
                    "capture declared request delay is inconsistent"
                )
            retry_after_seconds = attempt["retry_after_seconds"]
            if status == 429:
                _plain_int(
                    "retry_after_seconds",
                    retry_after_seconds,
                    minimum=MIN_RETRY_AFTER_SECONDS,
                    maximum=MAX_RETRY_AFTER_SECONDS,
                )
            elif retry_after_seconds is not None:
                raise OfficialMarketAdapterError(
                    "non-429 capture must not carry Retry-After"
                )
            raw_name = _text(
                "capture raw path", attempt["raw_relative_path"]
            )
            if (
                SAFE_RAW_NAME_RE.fullmatch(raw_name) is None
                or raw_name
                != _response_filename(
                    capture_index,
                    batch_attempt_index,
                    source_tier,
                )
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
            previous_attempt = attempt

        current_attempts: list[Mapping[str, Any]] = []
        historical_attempts: list[Mapping[str, Any]] = []
        saw_historical = False
        for attempt in attempts:
            if attempt["source_tier"] == "historical":
                saw_historical = True
                historical_attempts.append(attempt)
            elif saw_historical:
                raise OfficialMarketAdapterError(
                    "current endpoint cannot resume after historical"
                )
            else:
                current_attempts.append(attempt)
        if not current_attempts:
            raise OfficialMarketAdapterError(
                "capture must begin with current endpoint"
            )
        for endpoint_attempt_index, attempt in enumerate(current_attempts):
            if attempt["endpoint_attempt_index"] != endpoint_attempt_index:
                raise OfficialMarketAdapterError(
                    "current endpoint retry indexes are not contiguous"
                )
        for endpoint_attempt_index, attempt in enumerate(
            historical_attempts
        ):
            if attempt["endpoint_attempt_index"] != endpoint_attempt_index:
                raise OfficialMarketAdapterError(
                    "historical endpoint retry indexes are not contiguous"
                )
        if len(current_attempts) > MAX_429_RETRIES_PER_ENDPOINT + 1:
            raise OfficialMarketAdapterError(
                "current endpoint retry count exceeds bounded policy"
            )
        if len(historical_attempts) > MAX_429_RETRIES_PER_ENDPOINT + 1:
            raise OfficialMarketAdapterError(
                "historical endpoint retry count exceeds bounded policy"
            )
        if any(
            attempt["http_status"] != 429
            for attempt in current_attempts[:-1]
        ):
            raise OfficialMarketAdapterError(
                "only HTTP 429 may precede a current endpoint retry"
            )
        current_terminal_status = current_attempts[-1]["http_status"]
        if current_terminal_status == 200:
            if historical_attempts:
                raise OfficialMarketAdapterError(
                    "historical fallback after current 200 is forbidden"
                )
            if selected_index != len(current_attempts) - 1:
                raise OfficialMarketAdapterError(
                    "selected current response index is invalid"
                )
        elif current_terminal_status == 404:
            if not historical_attempts:
                raise OfficialMarketAdapterError(
                    "current 404 requires historical endpoint"
                )
            if any(
                attempt["http_status"] != 429
                for attempt in historical_attempts[:-1]
            ):
                raise OfficialMarketAdapterError(
                    "only HTTP 429 may precede a historical endpoint retry"
                )
            if historical_attempts[-1]["http_status"] != 200:
                raise OfficialMarketAdapterError(
                    "historical fallback did not terminate in HTTP 200"
                )
            if selected_index != len(attempts) - 1:
                raise OfficialMarketAdapterError(
                    "selected historical response index is invalid"
                )
        else:
            raise OfficialMarketAdapterError(
                "current endpoint must terminate in HTTP 200 or 404"
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
    if (
        capture["authority_issued_at_utc"] != authority["issued_at_utc"]
        or capture["authority_expires_at_utc"]
        != authority["expires_at_utc"]
        or capture["authority_issued_wall_ns"]
        != _timestamp_ns("issued_at_utc", authority["issued_at_utc"])
        or capture["authority_expires_wall_ns"]
        != _timestamp_ns("expires_at_utc", authority["expires_at_utc"])
    ):
        raise OfficialMarketAdapterError(
            "capture receipt authority time binding mismatch"
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
