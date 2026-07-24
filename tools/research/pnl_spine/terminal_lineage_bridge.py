#!/usr/bin/env python3
"""Bind root-pinned official terminal captures into PnL source lineage.

This module is deliberately an offline evidence bridge.  It has no network
client and no S3 writer.  Its input is a small, externally SHA-pinned spec
whose shards point at the immutable artifacts emitted by
``official_market_terminal_adapter.py``:

* the read-only authority;
* ``CAPTURE_RECEIPT.json``;
* the independently prepared raw-response pins;
* every raw HTTP response named by the capture receipt; and
* the normalized terminal output.

Production callers set ``require_root_read_only=True``.  Every path is then
required to be an absolute, non-symlink, root-owned regular file with no write
bits, below root-owned directories that are not group/world writable.  The
result is an in-memory receipt suitable for inclusion in the externally
pinned PnL lineage receipt.  It does not claim historical point-in-time
metadata, a scheduled start, or lifecycle intervals.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping, Sequence


SPEC_SCHEMA = "pnl-spine-official-terminal-root-spec-v2"
ROOT_RECEIPT_SCHEMA = "pnl-spine-official-terminal-root-receipt-v2"
AUTHORITY_SCHEMA = "a01-official-market-authority-v1"
CAPTURE_RECEIPT_SCHEMA = "a01-official-market-capture-receipt-v1"
RAW_PINS_SCHEMA = "a01-official-market-raw-pins-v1"
NORMALIZED_OUTPUT_SCHEMA = "a01-official-market-terminal-metadata-v1"

TEMPORALITY = "OBSERVED_AT_FETCH_NOT_HISTORICAL_AS_OF"
COVERAGE_POLICY = "EXACT_REQUIRED_TICKER_SET"
SINGLE_VERSION = "SINGLE_VERSION_REQUIRED"
EXPLICIT_PER_SHARD = "EXPLICIT_PER_SHARD"

MAX_CONTROL_BYTES = 16 * 1024 * 1024
MAX_RAW_RESPONSE_BYTES = 8 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,199}$")
SHARD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SAFE_RAW_NAME_RE = re.compile(
    r"^\d{4}\.\d{4}\.(?:current|historical)\.response\.json$"
)
UTC_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(\d{1,9}))?Z$"
)
OFFICIAL_ORIGIN = "https://api.elections.kalshi.com"
CURRENT_MARKET_PATH = "/trade-api/v2/markets/"
HISTORICAL_MARKET_PATH = "/trade-api/v2/historical/markets/"
MIN_REQUEST_INTERVAL_NS = 250_000_000
MAX_429_RETRIES_PER_ENDPOINT = 2
MIN_RETRY_AFTER_SECONDS = 1
MAX_RETRY_AFTER_SECONDS = 30
STANDARD_PRICE_RANGES = [
    {"start": "0.0000", "end": "1.0000", "step": "0.0100"}
]

REQUIRED_REMAINING_BLOCKERS = (
    "BLOCK_A01_HISTORICAL_POINT_IN_TIME_METADATA_INTERVALS_MISSING",
    "BLOCK_A01_SCHEDULED_START_AUTHORITY_MISSING",
    "BLOCK_A01_EXCHANGE_SETTLEMENT_REVISION_SEQUENCE_UNAVAILABLE",
)
REQUIRED_PROHIBITIONS = (
    (
        "occurrence_datetime, expected_expiration_time and "
        "close_time are not scheduled_start"
    ),
    "fetch observation time is not a historical metadata as-of timestamp",
)
RESOLVED_CAPABILITIES = (
    "OFFICIAL_FINALIZED_YES_NO_RESULT",
    "EXACT_YES_PAYOUT_E4",
    "OFFICIAL_SETTLEMENT_TIMESTAMP",
    "TERMINAL_OBSERVATION_CLOCK_BRACKET",
    "TERMINAL_OBSERVED_TICK_TABLE",
    "TERMINAL_OBSERVED_LIFECYCLE_TIMESTAMPS",
)


class TerminalLineageBridgeError(ValueError):
    """The external terminal evidence failed its immutable root contract."""


@dataclass(frozen=True)
class TerminalLineageBundle:
    """Validated receipt plus exact normalized records indexed by ticker."""

    receipt: Mapping[str, Any]
    terminal_records_by_ticker: Mapping[str, Mapping[str, Any]]


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


def _duplicate_key_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TerminalLineageBridgeError(
                f"duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _strict_json(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_key_object,
            parse_float=lambda token: (_ for _ in ()).throw(
                TerminalLineageBridgeError(
                    f"{label} contains a non-integer number: {token}"
                )
            ),
            parse_constant=lambda token: (_ for _ in ()).throw(
                TerminalLineageBridgeError(
                    f"{label} contains a non-finite number: {token}"
                )
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalLineageBridgeError(
            f"{label} is not strict UTF-8 JSON"
        ) from exc
    return _mapping(label, value)


def _strict_official_json(raw: bytes, label: str) -> Mapping[str, Any]:
    """Parse official JSON while retaining finite decimals as exact text."""

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_key_object,
            parse_float=lambda token: token,
            parse_constant=lambda token: (_ for _ in ()).throw(
                TerminalLineageBridgeError(
                    f"{label} contains a non-finite number: {token}"
                )
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalLineageBridgeError(
            f"{label} is not strict UTF-8 JSON"
        ) from exc
    return _mapping(label, value)


def _mapping(label: str, value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise TerminalLineageBridgeError(f"{label} must be an object")
    return value


def _list(label: str, value: object) -> list[Any]:
    if not isinstance(value, list):
        raise TerminalLineageBridgeError(f"{label} must be an array")
    return value


def _text(label: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TerminalLineageBridgeError(
            f"{label} must be a nonempty string"
        )
    return value


def _sha(label: str, value: object) -> str:
    result = _text(label, value)
    if SHA256_RE.fullmatch(result) is None:
        raise TerminalLineageBridgeError(
            f"{label} must be lowercase SHA-256"
        )
    return result


def _plain_int(
    label: str,
    value: object,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if type(value) is not int or value < minimum:
        raise TerminalLineageBridgeError(
            f"{label} must be a plain integer >= {minimum}"
        )
    if maximum is not None and value > maximum:
        raise TerminalLineageBridgeError(
            f"{label} must be <= {maximum}"
        )
    return value


def _utc_timestamp_ns(label: str, value: object) -> tuple[str, int]:
    """Validate an RFC3339 UTC timestamp and convert it to exact nanoseconds."""

    text = _text(label, value)
    match = UTC_RE.fullmatch(text)
    if match is None:
        raise TerminalLineageBridgeError(
            f"{label} must be an RFC3339 UTC timestamp ending in Z"
        )
    base, fraction = match.groups()
    try:
        parsed = dt.datetime.strptime(
            base, "%Y-%m-%dT%H:%M:%S"
        ).replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise TerminalLineageBridgeError(
            f"{label} is not a valid calendar timestamp"
        ) from exc
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    delta = parsed - epoch
    seconds = delta.days * 86_400 + delta.seconds
    nanos = int((fraction or "").ljust(9, "0"))
    return text, seconds * 1_000_000_000 + nanos


def _market_url(ticker: str, source_tier: str) -> str:
    if source_tier == "current":
        path = CURRENT_MARKET_PATH
    elif source_tier == "historical":
        path = HISTORICAL_MARKET_PATH
    else:
        raise TerminalLineageBridgeError("unknown market source tier")
    return f"{OFFICIAL_ORIGIN}{path}{_ticker('market URL ticker', ticker)}"


def _ticker(label: str, value: object) -> str:
    result = _text(label, value)
    if TICKER_RE.fullmatch(result) is None:
        raise TerminalLineageBridgeError(
            f"{label} is not a canonical ticker"
        )
    return result


def _strict_keys(
    label: str,
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required - optional)
    if missing:
        raise TerminalLineageBridgeError(
            f"{label} is missing fields: {','.join(missing)}"
        )
    if unknown:
        raise TerminalLineageBridgeError(
            f"{label} has unknown fields: {','.join(unknown)}"
        )


def _absolute_without_symlinks(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise TerminalLineageBridgeError(
            f"{label} must be an absolute path"
        )
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise TerminalLineageBridgeError(
                f"{label} is unavailable: {current}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise TerminalLineageBridgeError(
                f"{label} contains a symlink: {current}"
            )
    return absolute


def _read_pinned_file(
    path_text: object,
    expected_raw_sha256: object,
    *,
    label: str,
    maximum_bytes: int,
    require_root_read_only: bool,
) -> tuple[bytes, dict[str, Any]]:
    expected = _sha(f"{label} raw SHA-256", expected_raw_sha256)
    path = _absolute_without_symlinks(
        Path(_text(f"{label} path", path_text)),
        label=label,
    )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TerminalLineageBridgeError(
            f"{label} cannot be opened without following symlinks"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise TerminalLineageBridgeError(
                f"{label} must be a regular file"
            )
        if before.st_size > maximum_bytes:
            raise TerminalLineageBridgeError(
                f"{label} exceeds its byte bound"
            )
        if require_root_read_only:
            if before.st_uid != 0:
                raise TerminalLineageBridgeError(
                    f"{label} must be root-owned"
                )
            if before.st_mode & 0o222:
                raise TerminalLineageBridgeError(
                    f"{label} must have no write bits"
                )
            current = path.parent
            while True:
                info = os.lstat(current)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != 0
                    or info.st_mode & 0o022
                ):
                    raise TerminalLineageBridgeError(
                        f"{label} parent tree must be root-owned and "
                        "not group/world writable"
                    )
                if current == Path(current.anchor):
                    break
                current = current.parent
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise TerminalLineageBridgeError(
                    f"{label} exceeds its byte bound"
                )
            digest.update(chunk)
            chunks.append(chunk)
        after = os.fstat(descriptor)
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
            raise TerminalLineageBridgeError(
                f"{label} changed while being read"
            )
        actual = digest.hexdigest()
        if actual != expected:
            raise TerminalLineageBridgeError(
                f"{label} raw SHA-256 mismatch"
            )
        return b"".join(chunks), {
            "path": os.fspath(path),
            "raw_sha256": actual,
            "size_bytes": total,
            "uid": before.st_uid,
            "mode_octal": format(stat.S_IMODE(before.st_mode), "04o"),
            "root_read_only_verified": require_root_read_only,
        }
    finally:
        os.close(descriptor)


def _sorted_unique_tickers(
    label: str,
    value: object,
    *,
    allow_empty: bool = False,
) -> list[str]:
    rows = [
        _ticker(f"{label}[{index}]", item)
        for index, item in enumerate(_list(label, value))
    ]
    if not rows and not allow_empty:
        raise TerminalLineageBridgeError(
            f"{label} cannot have zero coverage"
        )
    if rows != sorted(rows) or len(rows) != len(set(rows)):
        raise TerminalLineageBridgeError(
            f"{label} must be sorted and unique"
        )
    return rows


def _validate_adapter_config(
    value: object,
    *,
    expected_sha256: str,
) -> Mapping[str, Any]:
    config = _mapping("adapter config", value)
    if canonical_sha256(config) != expected_sha256:
        raise TerminalLineageBridgeError(
            "adapter config canonical SHA-256 mismatch"
        )
    if config.get("metadata_temporality") != TEMPORALITY:
        raise TerminalLineageBridgeError(
            "adapter config weakens terminal temporality"
        )
    if config.get("scheduled_start_mapping") != "FORBIDDEN":
        raise TerminalLineageBridgeError(
            "adapter config permits a scheduled-start mapping"
        )
    if config.get("terminal_rule") != (
        "FINALIZED_YES_NO_EXACT_PAYOUT_ONLY"
    ):
        raise TerminalLineageBridgeError(
            "adapter config weakens the final settlement rule"
        )
    if (
        config.get("authentication") != "FORBIDDEN"
        or config.get("proxy") != "FORBIDDEN"
        or config.get("redirect") != "FORBIDDEN"
    ):
        raise TerminalLineageBridgeError(
            "adapter config weakens the read-only transport boundary"
        )
    required_transport = {
        "origin": OFFICIAL_ORIGIN,
        "current_market_path": CURRENT_MARKET_PATH,
        "historical_market_path": HISTORICAL_MARKET_PATH,
        "selection_rule": (
            "CURRENT_THEN_HISTORICAL_ONLY_ON_CURRENT_404"
        ),
        "request_method": "GET",
        "timeout_seconds": 15,
        "max_response_bytes": MAX_RAW_RESPONSE_BYTES,
    }
    for field, expected in required_transport.items():
        if config.get(field) != expected:
            raise TerminalLineageBridgeError(
                f"adapter config has noncanonical {field}"
            )
    headers = _mapping(
        "adapter config request_headers", config.get("request_headers")
    )
    if headers.get("Accept") != "application/json":
        raise TerminalLineageBridgeError(
            "adapter config does not require official JSON"
        )
    rate = _mapping(
        "adapter config rate_limit_policy",
        config.get("rate_limit_policy"),
    )
    expected_rate = {
        "minimum_request_interval_ns": MIN_REQUEST_INTERVAL_NS,
        "max_429_retries_per_endpoint": MAX_429_RETRIES_PER_ENDPOINT,
        "min_retry_after_seconds": MIN_RETRY_AFTER_SECONDS,
        "max_retry_after_seconds": MAX_RETRY_AFTER_SECONDS,
        "historical_fallback_on_429": "FORBIDDEN",
        "every_http_attempt_raw_capture": "CREATE_ONCE",
    }
    for field, expected in expected_rate.items():
        if rate.get(field) != expected:
            raise TerminalLineageBridgeError(
                f"adapter config weakens rate policy field {field}"
            )
    return config


def _validate_spec(value: object) -> dict[str, Any]:
    root = _mapping("terminal root spec", value)
    _strict_keys(
        "terminal root spec",
        root,
        required={
            "schema_version",
            "temporality",
            "coverage_policy",
            "required_tickers",
            "adapter_version_policy",
            "shards",
            "eligibility_exclusions",
        },
    )
    if root["schema_version"] != SPEC_SCHEMA:
        raise TerminalLineageBridgeError(
            "unknown terminal root spec schema"
        )
    if root["temporality"] != TEMPORALITY:
        raise TerminalLineageBridgeError(
            "terminal root spec temporality is not observation-only"
        )
    if root["coverage_policy"] != COVERAGE_POLICY:
        raise TerminalLineageBridgeError(
            "terminal root spec coverage policy is not exact"
        )
    required_tickers = _sorted_unique_tickers(
        "required_tickers", root["required_tickers"]
    )
    policy = _mapping(
        "adapter_version_policy", root["adapter_version_policy"]
    )
    _strict_keys(
        "adapter_version_policy",
        policy,
        required={"mode", "approved_shards"},
    )
    mode = _text("adapter version mode", policy["mode"])
    if mode not in {SINGLE_VERSION, EXPLICIT_PER_SHARD}:
        raise TerminalLineageBridgeError(
            "unknown adapter version policy"
        )
    approved_rows: list[dict[str, str]] = []
    previous = ""
    for index, raw in enumerate(
        _list("approved_shards", policy["approved_shards"])
    ):
        row = _mapping(f"approved_shards[{index}]", raw)
        _strict_keys(
            f"approved_shards[{index}]",
            row,
            required={
                "shard_id",
                "adapter_code_sha256",
                "adapter_config_sha256",
            },
        )
        shard_id = _text("approved shard_id", row["shard_id"])
        if SHARD_ID_RE.fullmatch(shard_id) is None:
            raise TerminalLineageBridgeError(
                "approved shard_id is malformed"
            )
        if previous and shard_id <= previous:
            raise TerminalLineageBridgeError(
                "approved_shards must be sorted and unique"
            )
        previous = shard_id
        approved_rows.append(
            {
                "shard_id": shard_id,
                "adapter_code_sha256": _sha(
                    "approved adapter code SHA",
                    row["adapter_code_sha256"],
                ),
                "adapter_config_sha256": _sha(
                    "approved adapter config SHA",
                    row["adapter_config_sha256"],
                ),
            }
        )
    if mode == SINGLE_VERSION and approved_rows:
        raise TerminalLineageBridgeError(
            "single-version mode forbids per-shard approvals"
        )
    if mode == EXPLICIT_PER_SHARD and not approved_rows:
        raise TerminalLineageBridgeError(
            "mixed versions require explicit per-shard approvals"
        )

    shards: list[dict[str, Any]] = []
    previous = ""
    required_fields = {
        "shard_id",
        "adapter_code_path",
        "authority_path",
        "authority_raw_sha256",
        "capture_receipt_path",
        "capture_receipt_raw_sha256",
        "raw_pins_path",
        "raw_pins_raw_sha256",
        "normalized_output_path",
        "normalized_output_raw_sha256",
        "adapter_code_sha256",
        "adapter_config_sha256",
    }
    for index, raw in enumerate(_list("shards", root["shards"])):
        row = _mapping(f"shards[{index}]", raw)
        _strict_keys(f"shards[{index}]", row, required=required_fields)
        shard_id = _text("shard_id", row["shard_id"])
        if SHARD_ID_RE.fullmatch(shard_id) is None:
            raise TerminalLineageBridgeError("shard_id is malformed")
        if previous and shard_id <= previous:
            raise TerminalLineageBridgeError(
                "shards must be sorted and unique"
            )
        previous = shard_id
        normalized = dict(row)
        normalized["shard_id"] = shard_id
        for field in (
            "authority_raw_sha256",
            "capture_receipt_raw_sha256",
            "raw_pins_raw_sha256",
            "normalized_output_raw_sha256",
            "adapter_code_sha256",
            "adapter_config_sha256",
        ):
            normalized[field] = _sha(field, row[field])
        for field in (
            "adapter_code_path",
            "authority_path",
            "capture_receipt_path",
            "raw_pins_path",
            "normalized_output_path",
        ):
            normalized[field] = _text(field, row[field])
        shards.append(normalized)
    if not shards:
        raise TerminalLineageBridgeError(
            "terminal root spec cannot have zero shards"
        )

    pairs = {
        (row["adapter_code_sha256"], row["adapter_config_sha256"])
        for row in shards
    }
    if mode == SINGLE_VERSION and len(pairs) != 1:
        raise TerminalLineageBridgeError(
            "mixed adapter versions require explicit per-shard approval"
        )
    if mode == EXPLICIT_PER_SHARD:
        actual_approvals = [
            {
                "shard_id": row["shard_id"],
                "adapter_code_sha256": row["adapter_code_sha256"],
                "adapter_config_sha256": row["adapter_config_sha256"],
            }
            for row in shards
        ]
        if approved_rows != actual_approvals:
            raise TerminalLineageBridgeError(
                "explicit adapter approvals do not exactly match shards"
            )
    exclusions: list[dict[str, Any]] = []
    previous = ""
    exclusion_required = {
        "ticker",
        "reason_code",
        "adapter_code_path",
        "authority_path",
        "authority_raw_sha256",
        "capture_receipt_path",
        "capture_receipt_raw_sha256",
        "raw_pins_path",
        "raw_pins_raw_sha256",
        "raw_response_path",
        "raw_response_raw_sha256",
        "raw_response_size_bytes",
        "adapter_code_sha256",
        "adapter_config_sha256",
        "source_tier",
        "http_status",
        "observed_price_level_structure",
    }
    for index, raw in enumerate(
        _list("eligibility_exclusions", root["eligibility_exclusions"])
    ):
        row = _mapping(f"eligibility_exclusions[{index}]", raw)
        _strict_keys(
            f"eligibility_exclusions[{index}]",
            row,
            required=exclusion_required,
        )
        ticker = _ticker("eligibility exclusion ticker", row["ticker"])
        if previous and ticker <= previous:
            raise TerminalLineageBridgeError(
                "eligibility exclusions must be sorted and unique"
            )
        previous = ticker
        if row["reason_code"] != (
            "NON_STANDARD_PRICE_LEVEL_STRUCTURE"
        ):
            raise TerminalLineageBridgeError(
                "unknown terminal eligibility exclusion reason"
            )
        if row["source_tier"] not in {"current", "historical"}:
            raise TerminalLineageBridgeError(
                "eligibility exclusion source tier is invalid"
            )
        if row["http_status"] != 200:
            raise TerminalLineageBridgeError(
                "eligibility exclusion requires official HTTP 200"
            )
        structure = _text(
            "observed_price_level_structure",
            row["observed_price_level_structure"],
        )
        if structure != "tapered_deci_cent":
            raise TerminalLineageBridgeError(
                "only the audited tapered_deci_cent A01 exclusion is "
                "supported"
            )
        exclusions.append(
            {
                "ticker": ticker,
                "reason_code": row["reason_code"],
                "adapter_code_path": _text(
                    "exclusion adapter_code_path",
                    row["adapter_code_path"],
                ),
                "authority_path": _text(
                    "exclusion authority_path", row["authority_path"]
                ),
                "authority_raw_sha256": _sha(
                    "exclusion authority SHA",
                    row["authority_raw_sha256"],
                ),
                "capture_receipt_path": _text(
                    "exclusion capture_receipt_path",
                    row["capture_receipt_path"],
                ),
                "capture_receipt_raw_sha256": _sha(
                    "exclusion capture receipt SHA",
                    row["capture_receipt_raw_sha256"],
                ),
                "raw_pins_path": _text(
                    "exclusion raw_pins_path",
                    row["raw_pins_path"],
                ),
                "raw_pins_raw_sha256": _sha(
                    "exclusion raw pins SHA",
                    row["raw_pins_raw_sha256"],
                ),
                "raw_response_path": _text(
                    "exclusion raw_response_path",
                    row["raw_response_path"],
                ),
                "raw_response_raw_sha256": _sha(
                    "exclusion raw response SHA",
                    row["raw_response_raw_sha256"],
                ),
                "raw_response_size_bytes": _plain_int(
                    "exclusion raw response size",
                    row["raw_response_size_bytes"],
                    maximum=MAX_RAW_RESPONSE_BYTES,
                ),
                "adapter_code_sha256": _sha(
                    "exclusion adapter code SHA",
                    row["adapter_code_sha256"],
                ),
                "adapter_config_sha256": _sha(
                    "exclusion adapter config SHA",
                    row["adapter_config_sha256"],
                ),
                "source_tier": row["source_tier"],
                "http_status": 200,
                "observed_price_level_structure": structure,
            }
        )
    return {
        "schema_version": SPEC_SCHEMA,
        "temporality": TEMPORALITY,
        "coverage_policy": COVERAGE_POLICY,
        "required_tickers": required_tickers,
        "adapter_version_policy": {
            "mode": mode,
            "approved_shards": approved_rows,
        },
        "shards": shards,
        "eligibility_exclusions": exclusions,
    }


def _validate_authority(
    value: object,
    *,
    expected_raw_sha256: str,
    expected_code_sha256: str,
    expected_config_sha256: str,
) -> tuple[dict[str, Any], list[str], int, int]:
    authority = _mapping("terminal authority", value)
    _strict_keys(
        "terminal authority",
        authority,
        required={
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
    if authority.get("schema_version") != AUTHORITY_SCHEMA:
        raise TerminalLineageBridgeError(
            "wrong official terminal authority schema"
        )
    if authority.get("purpose") != (
        "READ_ONLY_OFFICIAL_MARKET_TERMINAL_METADATA"
    ):
        raise TerminalLineageBridgeError(
            "terminal authority purpose is not read-only"
        )
    if authority.get("adapter_code_sha256") != expected_code_sha256:
        raise TerminalLineageBridgeError(
            "authority adapter code SHA mismatch"
        )
    if authority.get("adapter_config_sha256") != expected_config_sha256:
        raise TerminalLineageBridgeError(
            "authority adapter config SHA mismatch"
        )
    _text("authority_id", authority["authority_id"])
    issued, issued_ns = _utc_timestamp_ns(
        "authority issued_at_utc", authority["issued_at_utc"]
    )
    expires, expires_ns = _utc_timestamp_ns(
        "authority expires_at_utc", authority["expires_at_utc"]
    )
    if issued_ns >= expires_ns:
        raise TerminalLineageBridgeError(
            "terminal authority expiry is not after issue"
        )
    _sha("authority raw SHA", expected_raw_sha256)
    return (
        dict(authority),
        _sorted_unique_tickers("authority.tickers", authority["tickers"]),
        issued_ns,
        expires_ns,
    )


def _attempt_projection(
    attempt: Mapping[str, Any],
    *,
    ticker: str,
) -> dict[str, Any]:
    required = {
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
    }
    _strict_keys("capture attempt", attempt, required=required)
    if attempt["ticker"] != ticker:
        raise TerminalLineageBridgeError(
            "capture attempt ticker differs from parent"
        )
    relative = _text(
        "capture raw_relative_path", attempt["raw_relative_path"]
    )
    if (
        "/" in relative
        or "\\" in relative
        or SAFE_RAW_NAME_RE.fullmatch(relative) is None
    ):
        raise TerminalLineageBridgeError(
            "capture raw_relative_path is unsafe"
        )
    tier = _text("capture source tier", attempt["source_tier"])
    if tier not in {"current", "historical"}:
        raise TerminalLineageBridgeError(
            "capture source tier is invalid"
        )
    before_wall = _plain_int(
        "fetch_wall_ns_before", attempt["fetch_wall_ns_before"]
    )
    after_wall = _plain_int(
        "fetch_wall_ns_after", attempt["fetch_wall_ns_after"]
    )
    before_mono = _plain_int(
        "fetch_monotonic_ns_before",
        attempt["fetch_monotonic_ns_before"],
    )
    after_mono = _plain_int(
        "fetch_monotonic_ns_after",
        attempt["fetch_monotonic_ns_after"],
    )
    if after_wall < before_wall or after_mono < before_mono:
        raise TerminalLineageBridgeError(
            "capture clock bracket is reversed"
        )
    return {
        "ticker": ticker,
        "source_tier": tier,
        "endpoint_attempt_index": _plain_int(
            "endpoint_attempt_index",
            attempt["endpoint_attempt_index"],
        ),
        "batch_attempt_index": _plain_int(
            "batch_attempt_index", attempt["batch_attempt_index"]
        ),
        "request_url": _text("request_url", attempt["request_url"]),
        "http_status": _plain_int(
            "http_status",
            attempt["http_status"],
            minimum=100,
            maximum=599,
        ),
        "required_delay_from_prior_attempt_ns": _plain_int(
            "required_delay_from_prior_attempt_ns",
            attempt["required_delay_from_prior_attempt_ns"],
        ),
        "retry_after_seconds": attempt["retry_after_seconds"],
        "fetch_wall_ns_before": before_wall,
        "fetch_wall_ns_after": after_wall,
        "fetch_monotonic_ns_before": before_mono,
        "fetch_monotonic_ns_after": after_mono,
        "raw_relative_path": relative,
        "raw_size": _plain_int(
            "raw_size",
            attempt["raw_size"],
            maximum=MAX_RAW_RESPONSE_BYTES,
        ),
        "raw_sha256": _sha("raw SHA", attempt["raw_sha256"]),
        "adapter_code_sha256": _sha(
            "attempt adapter code SHA",
            attempt["adapter_code_sha256"],
        ),
        "adapter_config_sha256": _sha(
            "attempt adapter config SHA",
            attempt["adapter_config_sha256"],
        ),
    }


def _validate_capture_receipt(
    value: object,
    *,
    authority_raw_sha256: str,
    authority: Mapping[str, Any],
    authority_tickers: Sequence[str],
    expected_code_sha256: str,
    expected_config_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    receipt = _mapping("capture receipt", value)
    _strict_keys(
        "capture receipt",
        receipt,
        required={
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
    if receipt.get("schema_version") != CAPTURE_RECEIPT_SCHEMA:
        raise TerminalLineageBridgeError(
            "wrong official capture receipt schema"
        )
    if receipt.get("authority_raw_sha256") != authority_raw_sha256:
        raise TerminalLineageBridgeError(
            "capture authority raw SHA mismatch"
        )
    if receipt.get("authority_id") != authority["authority_id"]:
        raise TerminalLineageBridgeError(
            "capture authority id mismatch"
        )
    if (
        receipt.get("authority_issued_at_utc")
        != authority["issued_at_utc"]
        or receipt.get("authority_expires_at_utc")
        != authority["expires_at_utc"]
    ):
        raise TerminalLineageBridgeError(
            "capture authority time binding mismatch"
        )
    _, issued_wall_ns = _utc_timestamp_ns(
        "capture authority issued_at_utc",
        authority["issued_at_utc"],
    )
    _, expires_wall_ns = _utc_timestamp_ns(
        "capture authority expires_at_utc",
        authority["expires_at_utc"],
    )
    if (
        receipt.get("authority_issued_wall_ns") != issued_wall_ns
        or receipt.get("authority_expires_wall_ns") != expires_wall_ns
    ):
        raise TerminalLineageBridgeError(
            "capture authority text/nanosecond bounds mismatch"
        )
    if receipt.get("adapter_code_sha256") != expected_code_sha256:
        raise TerminalLineageBridgeError(
            "capture adapter code SHA mismatch"
        )
    if receipt.get("adapter_config_sha256") != expected_config_sha256:
        raise TerminalLineageBridgeError(
            "capture adapter config SHA mismatch"
        )
    _validate_adapter_config(
        receipt.get("adapter_config"),
        expected_sha256=expected_config_sha256,
    )
    if receipt.get("temporality") != TEMPORALITY:
        raise TerminalLineageBridgeError(
            "capture receipt temporality is not observation-only"
        )
    if (
        receipt.get("request_authentication") != "NONE"
        or receipt.get("proxy_policy") != "DISABLED"
        or receipt.get("redirect_policy") != "REJECT"
    ):
        raise TerminalLineageBridgeError(
            "capture receipt transport boundary was weakened"
        )
    captures = _list("capture receipt captures", receipt.get("captures"))
    capture_tickers: list[str] = []
    attempts: list[dict[str, Any]] = []
    selected_by_ticker: dict[str, dict[str, Any]] = {}
    seen_raw_names: set[str] = set()
    expected_batch_attempt_index = 0
    previous_attempt: Mapping[str, Any] | None = None
    for index, raw in enumerate(captures):
        capture = _mapping(f"captures[{index}]", raw)
        _strict_keys(
            f"captures[{index}]",
            capture,
            required={"ticker", "attempts", "selected_attempt_index"},
        )
        ticker = _ticker(f"captures[{index}].ticker", capture["ticker"])
        capture_tickers.append(ticker)
        raw_attempts = _list(
            f"captures[{index}].attempts", capture["attempts"]
        )
        if not raw_attempts:
            raise TerminalLineageBridgeError(
                "capture ticker has no HTTP attempts"
            )
        projected = [
            _attempt_projection(
                _mapping("capture attempt", raw_attempt),
                ticker=ticker,
            )
            for raw_attempt in raw_attempts
        ]
        maximum_attempts = 2 * (MAX_429_RETRIES_PER_ENDPOINT + 1)
        if len(projected) > maximum_attempts:
            raise TerminalLineageBridgeError(
                "capture attempt count exceeds bounded endpoint policy"
            )
        for attempt_index, attempt in enumerate(projected):
            if (
                attempt["adapter_code_sha256"] != expected_code_sha256
                or attempt["adapter_config_sha256"]
                != expected_config_sha256
            ):
                raise TerminalLineageBridgeError(
                    "capture attempt adapter version drifted"
                )
            if attempt["batch_attempt_index"] != expected_batch_attempt_index:
                raise TerminalLineageBridgeError(
                    "capture batch attempt indexes are not contiguous"
                )
            expected_batch_attempt_index += 1
            canonical_url = _market_url(ticker, attempt["source_tier"])
            if attempt["request_url"] != canonical_url:
                raise TerminalLineageBridgeError(
                    "capture request URL is not canonical"
                )
            if not (
                issued_wall_ns
                <= attempt["fetch_wall_ns_before"]
                <= attempt["fetch_wall_ns_after"]
                <= expires_wall_ns
            ):
                raise TerminalLineageBridgeError(
                    "capture attempt is outside authority validity window"
                )
            expected_delay_ns = 0
            if previous_attempt is not None:
                if (
                    attempt["fetch_wall_ns_before"]
                    < previous_attempt["fetch_wall_ns_after"]
                    or attempt["fetch_monotonic_ns_before"]
                    < previous_attempt["fetch_monotonic_ns_after"]
                ):
                    raise TerminalLineageBridgeError(
                        "capture clocks regressed across HTTP attempts"
                    )
                expected_delay_ns = MIN_REQUEST_INTERVAL_NS
                if previous_attempt["http_status"] == 429:
                    retry_after = previous_attempt[
                        "retry_after_seconds"
                    ]
                    if type(retry_after) is not int:
                        raise TerminalLineageBridgeError(
                            "prior HTTP 429 lacks retry delay"
                        )
                    expected_delay_ns = max(
                        expected_delay_ns,
                        retry_after * 1_000_000_000,
                    )
                if (
                    attempt["fetch_monotonic_ns_before"]
                    - previous_attempt["fetch_monotonic_ns_after"]
                    < expected_delay_ns
                ):
                    raise TerminalLineageBridgeError(
                        "capture does not prove required request throttle"
                    )
            if (
                attempt["required_delay_from_prior_attempt_ns"]
                != expected_delay_ns
            ):
                raise TerminalLineageBridgeError(
                    "capture declared request delay is inconsistent"
                )
            retry_after = attempt["retry_after_seconds"]
            if attempt["http_status"] == 429:
                _plain_int(
                    "retry_after_seconds",
                    retry_after,
                    minimum=MIN_RETRY_AFTER_SECONDS,
                    maximum=MAX_RETRY_AFTER_SECONDS,
                )
            elif retry_after is not None:
                raise TerminalLineageBridgeError(
                    "non-429 capture must not carry Retry-After"
                )
            expected_raw_name = (
                f"{index:04d}."
                f"{attempt['batch_attempt_index']:04d}."
                f"{attempt['source_tier']}.response.json"
            )
            if attempt["raw_relative_path"] != expected_raw_name:
                raise TerminalLineageBridgeError(
                    "capture raw response path is noncanonical"
                )
            if attempt["raw_relative_path"] in seen_raw_names:
                raise TerminalLineageBridgeError(
                    "capture reuses a raw response path"
                )
            seen_raw_names.add(attempt["raw_relative_path"])
            previous_attempt = attempt
        selected_index = _plain_int(
            "selected_attempt_index",
            capture["selected_attempt_index"],
            maximum=len(projected) - 1,
        )
        selected = projected[selected_index]
        if selected["http_status"] != 200:
            raise TerminalLineageBridgeError(
                "selected terminal response is not HTTP 200"
            )
        if (
            selected["adapter_code_sha256"] != expected_code_sha256
            or selected["adapter_config_sha256"]
            != expected_config_sha256
        ):
            raise TerminalLineageBridgeError(
                "selected capture adapter version drifted"
            )
        current_attempts: list[Mapping[str, Any]] = []
        historical_attempts: list[Mapping[str, Any]] = []
        saw_historical = False
        for attempt in projected:
            if attempt["source_tier"] == "historical":
                saw_historical = True
                historical_attempts.append(attempt)
            elif saw_historical:
                raise TerminalLineageBridgeError(
                    "current endpoint cannot resume after historical"
                )
            else:
                current_attempts.append(attempt)
        if not current_attempts:
            raise TerminalLineageBridgeError(
                "capture must begin with current endpoint"
            )
        for endpoint_index, attempt in enumerate(current_attempts):
            if attempt["endpoint_attempt_index"] != endpoint_index:
                raise TerminalLineageBridgeError(
                    "current endpoint retry indexes are not contiguous"
                )
        for endpoint_index, attempt in enumerate(historical_attempts):
            if attempt["endpoint_attempt_index"] != endpoint_index:
                raise TerminalLineageBridgeError(
                    "historical endpoint retry indexes are not contiguous"
                )
        if (
            len(current_attempts) > MAX_429_RETRIES_PER_ENDPOINT + 1
            or len(historical_attempts)
            > MAX_429_RETRIES_PER_ENDPOINT + 1
        ):
            raise TerminalLineageBridgeError(
                "capture retry count exceeds bounded policy"
            )
        if any(
            attempt["http_status"] != 429
            for attempt in current_attempts[:-1]
        ):
            raise TerminalLineageBridgeError(
                "only HTTP 429 may precede a current endpoint retry"
            )
        current_status = current_attempts[-1]["http_status"]
        if current_status == 200:
            if historical_attempts or selected_index != len(projected) - 1:
                raise TerminalLineageBridgeError(
                    "current HTTP 200 must be the selected final attempt"
                )
        elif current_status == 404:
            if not historical_attempts:
                raise TerminalLineageBridgeError(
                    "current HTTP 404 requires historical fallback"
                )
            if any(
                attempt["http_status"] != 429
                for attempt in historical_attempts[:-1]
            ):
                raise TerminalLineageBridgeError(
                    "only HTTP 429 may precede a historical retry"
                )
            if (
                historical_attempts[-1]["http_status"] != 200
                or selected_index != len(projected) - 1
            ):
                raise TerminalLineageBridgeError(
                    "historical fallback must end in selected HTTP 200"
                )
        else:
            raise TerminalLineageBridgeError(
                "current endpoint must terminate in HTTP 200 or 404"
            )
        attempts.extend(projected)
        selected_by_ticker[ticker] = selected
    if (
        capture_tickers != list(authority_tickers)
        or capture_tickers != sorted(capture_tickers)
        or len(capture_tickers) != len(set(capture_tickers))
    ):
        raise TerminalLineageBridgeError(
            "capture ticker set differs from sorted unique authority"
        )
    return attempts, selected_by_ticker


def _validate_raw_pins(
    value: object,
    *,
    authority_raw_sha256: str,
    capture_receipt_raw_sha256: str,
    expected_code_sha256: str,
    expected_config_sha256: str,
    selected_by_ticker: Mapping[str, Mapping[str, Any]],
) -> None:
    pins = _mapping("raw pins", value)
    _strict_keys(
        "raw pins",
        pins,
        required={
            "schema_version",
            "authority_raw_sha256",
            "capture_receipt_raw_sha256",
            "adapter_code_sha256",
            "adapter_config_sha256",
            "selected_responses",
        },
    )
    if pins.get("schema_version") != RAW_PINS_SCHEMA:
        raise TerminalLineageBridgeError(
            "wrong official raw-pins schema"
        )
    expected_top = {
        "authority_raw_sha256": authority_raw_sha256,
        "capture_receipt_raw_sha256": capture_receipt_raw_sha256,
        "adapter_code_sha256": expected_code_sha256,
        "adapter_config_sha256": expected_config_sha256,
    }
    for field, expected in expected_top.items():
        if pins.get(field) != expected:
            raise TerminalLineageBridgeError(
                f"raw pins {field} mismatch"
            )
    selected = _list(
        "raw pins selected_responses", pins.get("selected_responses")
    )
    pin_tickers: list[str] = []
    for index, raw in enumerate(selected):
        row = _mapping(f"selected_responses[{index}]", raw)
        _strict_keys(
            f"selected_responses[{index}]",
            row,
            required={
                "ticker",
                "source_tier",
                "request_url",
                "http_status",
                "raw_size",
                "raw_sha256",
            },
        )
        ticker = _ticker("raw pin ticker", row["ticker"])
        pin_tickers.append(ticker)
        try:
            capture = selected_by_ticker[ticker]
        except KeyError as exc:
            raise TerminalLineageBridgeError(
                "raw pins contain an extra ticker"
            ) from exc
        for field in (
            "source_tier",
            "request_url",
            "http_status",
            "raw_size",
            "raw_sha256",
        ):
            if row[field] != capture[field]:
                raise TerminalLineageBridgeError(
                    f"raw pin differs from selected capture: {field}"
                )
    expected_tickers = sorted(selected_by_ticker)
    if (
        pin_tickers != expected_tickers
        or len(pin_tickers) != len(set(pin_tickers))
    ):
        raise TerminalLineageBridgeError(
            "raw pins ticker set is missing, extra, duplicate, or unsorted"
        )


def _parse_eligible_terminal_response(
    raw: bytes,
    *,
    ticker: str,
) -> dict[str, Any]:
    """Independently rebuild every PnL-bearing field from official raw JSON."""

    envelope = _strict_official_json(
        raw, f"selected official response {ticker}"
    )
    _strict_keys(
        f"selected official response {ticker}",
        envelope,
        required={"market"},
    )
    market = _mapping(
        f"selected official market {ticker}", envelope["market"]
    )
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
        raise TerminalLineageBridgeError(
            "selected official response is missing terminal fields: "
            + ",".join(missing)
        )
    if _ticker("official market ticker", market["ticker"]) != ticker:
        raise TerminalLineageBridgeError(
            "selected official response ticker mismatch"
        )
    status = _text("official market status", market["status"])
    if status != "finalized":
        raise TerminalLineageBridgeError(
            "selected official market is not finalized"
        )
    result = _text("official market result", market["result"])
    if result not in {"yes", "no"}:
        raise TerminalLineageBridgeError(
            "selected official result is not exact yes/no"
        )
    payout_text = _text(
        "official settlement_value_dollars",
        market["settlement_value_dollars"],
    )
    expected_payout_text = "1.0000" if result == "yes" else "0.0000"
    if payout_text != expected_payout_text:
        raise TerminalLineageBridgeError(
            "selected official result/payout disagree"
        )
    settlement_ts, _ = _utc_timestamp_ns(
        "official settlement_ts", market["settlement_ts"]
    )
    lifecycle: dict[str, str] = {}
    for field in (
        "open_time",
        "close_time",
        "expected_expiration_time",
        "occurrence_datetime",
    ):
        lifecycle[field], _ = _utc_timestamp_ns(
            f"official {field}", market[field]
        )
    structure = _text(
        "official price_level_structure",
        market["price_level_structure"],
    )
    if structure != "linear_cent":
        raise TerminalLineageBridgeError(
            "non-standard price level structure is not A01 eligible"
        )
    ranges = _list("official price_ranges", market["price_ranges"])
    if len(ranges) != 1:
        raise TerminalLineageBridgeError(
            "A01 requires one standard full-range cent interval"
        )
    interval = _mapping("official price_ranges[0]", ranges[0])
    _strict_keys(
        "official price_ranges[0]",
        interval,
        required={"start", "end", "step"},
    )
    normalized_ranges = [
        {
            "start": _text("official price range start", interval["start"]),
            "end": _text("official price range end", interval["end"]),
            "step": _text("official price range step", interval["step"]),
        }
    ]
    if normalized_ranges != STANDARD_PRICE_RANGES:
        raise TerminalLineageBridgeError(
            "non-standard tick table is not A01 eligible"
        )
    return {
        "ticker": ticker,
        "status": status,
        "result": result,
        "yes_settlement_value_e4": 10_000 if result == "yes" else 0,
        "settlement_ts": settlement_ts,
        "price_level_structure": structure,
        "price_ranges": normalized_ranges,
        "tick_size_e4": 100,
        **lifecycle,
    }


def _validate_terminal_record(
    value: object,
    *,
    ticker: str,
    selected: Mapping[str, Any],
    parsed: Mapping[str, Any],
) -> dict[str, Any]:
    record = _mapping(f"terminal record {ticker}", value)
    required = {
        "settlement_id",
        "market_ticker",
        "status",
        "finalized",
        "yes_settlement_value_e4",
        "observed_at_ns",
        "revision",
        "source_sha256",
    }
    _strict_keys(f"terminal record {ticker}", record, required=required)
    if record["market_ticker"] != ticker:
        raise TerminalLineageBridgeError(
            "terminal record ticker mismatch"
        )
    if record["status"] != "FINALIZED" or record["finalized"] is not True:
        raise TerminalLineageBridgeError(
            "terminal record is not finalized"
        )
    if record["yes_settlement_value_e4"] not in {0, 10_000}:
        raise TerminalLineageBridgeError(
            "terminal record payout is not exact binary E4"
        )
    if (
        record["yes_settlement_value_e4"]
        != parsed["yes_settlement_value_e4"]
    ):
        raise TerminalLineageBridgeError(
            "terminal record payout differs from selected official response"
        )
    if record["observed_at_ns"] != selected["fetch_wall_ns_after"]:
        raise TerminalLineageBridgeError(
            "terminal observation clock differs from selected response"
        )
    if record["revision"] != 0:
        raise TerminalLineageBridgeError(
            "terminal bridge cannot invent a settlement revision sequence"
        )
    if record["source_sha256"] != selected["raw_sha256"]:
        raise TerminalLineageBridgeError(
            "terminal source SHA differs from selected raw response"
        )
    settlement_id = _text("settlement_id", record["settlement_id"])
    expected_settlement_id = (
        f"kalshi-official:{ticker}:{parsed['settlement_ts']}:"
        f"{selected['raw_sha256'][:16]}"
    )
    if settlement_id != expected_settlement_id:
        raise TerminalLineageBridgeError(
            "terminal settlement_id differs from selected official response"
        )
    return dict(record)


def _validate_metadata(
    value: object,
    *,
    ticker: str,
    selected: Mapping[str, Any],
    parsed: Mapping[str, Any],
) -> None:
    metadata = _mapping(f"metadata evidence {ticker}", value)
    required = {
        "market_ticker",
        "source_tier",
        "request_url",
        "raw_response_sha256",
        "observed_wall_ns_before",
        "observed_wall_ns_after",
        "observed_monotonic_ns_before",
        "observed_monotonic_ns_after",
        "market_status",
        "official_result",
        "yes_settlement_value_e4",
        "settlement_ts",
        "price_level_structure",
        "price_ranges",
        "tick_size_e4",
        "open_time",
        "close_time",
        "expected_expiration_time",
        "occurrence_datetime",
        "metadata_asof_semantics",
        "historical_asof_utc",
        "scheduled_start_ts_ns",
        "scheduled_start_field_mapping",
        "point_in_time_lifecycle_interval",
        "point_in_time_tick_interval",
    }
    _strict_keys(
        f"metadata evidence {ticker}", metadata, required=required
    )
    if metadata.get("market_ticker") != ticker:
        raise TerminalLineageBridgeError(
            "metadata evidence ticker mismatch"
        )
    if metadata.get("raw_response_sha256") != selected["raw_sha256"]:
        raise TerminalLineageBridgeError(
            "metadata evidence raw SHA mismatch"
        )
    selected_bindings = {
        "source_tier": selected["source_tier"],
        "request_url": selected["request_url"],
        "observed_wall_ns_before": selected["fetch_wall_ns_before"],
        "observed_wall_ns_after": selected["fetch_wall_ns_after"],
        "observed_monotonic_ns_before": selected[
            "fetch_monotonic_ns_before"
        ],
        "observed_monotonic_ns_after": selected[
            "fetch_monotonic_ns_after"
        ],
    }
    parsed_bindings = {
        "market_status": parsed["status"],
        "official_result": parsed["result"],
        "yes_settlement_value_e4": parsed["yes_settlement_value_e4"],
        "settlement_ts": parsed["settlement_ts"],
        "price_level_structure": parsed["price_level_structure"],
        "price_ranges": parsed["price_ranges"],
        "tick_size_e4": parsed["tick_size_e4"],
        "open_time": parsed["open_time"],
        "close_time": parsed["close_time"],
        "expected_expiration_time": parsed["expected_expiration_time"],
        "occurrence_datetime": parsed["occurrence_datetime"],
    }
    for field, expected in {**selected_bindings, **parsed_bindings}.items():
        if metadata.get(field) != expected:
            raise TerminalLineageBridgeError(
                f"metadata evidence differs from selected raw: {field}"
            )
    if metadata.get("metadata_asof_semantics") != TEMPORALITY:
        raise TerminalLineageBridgeError(
            "metadata evidence claims historical as-of semantics"
        )
    if metadata.get("historical_asof_utc") is not None:
        raise TerminalLineageBridgeError(
            "terminal metadata cannot satisfy historical as-of"
        )
    if metadata.get("scheduled_start_ts_ns") is not None:
        raise TerminalLineageBridgeError(
            "terminal metadata cannot supply scheduled_start"
        )
    if metadata.get("scheduled_start_field_mapping") != (
        "BLOCKED_NO_AUTHORIZED_SCHEDULED_START_MAPPING"
    ):
        raise TerminalLineageBridgeError(
            "terminal metadata weakens the scheduled-start blocker"
        )
    if metadata.get("point_in_time_lifecycle_interval") is not None:
        raise TerminalLineageBridgeError(
            "terminal metadata cannot supply historical lifecycle intervals"
        )
    if metadata.get("point_in_time_tick_interval") is not None:
        raise TerminalLineageBridgeError(
            "terminal metadata cannot supply historical tick intervals"
        )


def _validate_normalized_output(
    value: object,
    *,
    authority_raw_sha256: str,
    capture_receipt_raw_sha256: str,
    raw_pins_raw_sha256: str,
    expected_code_sha256: str,
    expected_config_sha256: str,
    authority_tickers: Sequence[str],
    attempts: Sequence[Mapping[str, Any]],
    selected_by_ticker: Mapping[str, Mapping[str, Any]],
    parsed_by_ticker: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    output = _mapping("normalized terminal output", value)
    required_top = {
        "schema_version",
        "authority_raw_sha256",
        "capture_receipt_raw_sha256",
        "raw_pins_raw_sha256",
        "adapter_code_sha256",
        "adapter_config_sha256",
        "terminal_records",
        "metadata_evidence",
        "raw_response_evidence",
        "resolved_capabilities",
        "remaining_blockers",
        "prohibitions",
    }
    _strict_keys(
        "normalized terminal output", output, required=required_top
    )
    if output["schema_version"] != NORMALIZED_OUTPUT_SCHEMA:
        raise TerminalLineageBridgeError(
            "wrong normalized terminal output schema"
        )
    expected_bindings = {
        "authority_raw_sha256": authority_raw_sha256,
        "capture_receipt_raw_sha256": capture_receipt_raw_sha256,
        "raw_pins_raw_sha256": raw_pins_raw_sha256,
        "adapter_code_sha256": expected_code_sha256,
        "adapter_config_sha256": expected_config_sha256,
    }
    for field, expected in expected_bindings.items():
        if output[field] != expected:
            raise TerminalLineageBridgeError(
                f"normalized output {field} mismatch"
            )
    if output["resolved_capabilities"] != list(RESOLVED_CAPABILITIES):
        raise TerminalLineageBridgeError(
            "normalized output capability set drifted"
        )
    if output["remaining_blockers"] != list(
        REQUIRED_REMAINING_BLOCKERS
    ):
        raise TerminalLineageBridgeError(
            "normalized output removed a mandatory blocker"
        )
    if output["prohibitions"] != list(REQUIRED_PROHIBITIONS):
        raise TerminalLineageBridgeError(
            "normalized output prohibition set drifted"
        )

    records = _list("terminal_records", output["terminal_records"])
    metadata_rows = _list(
        "metadata_evidence", output["metadata_evidence"]
    )
    if len(records) != len(authority_tickers) or len(metadata_rows) != len(
        authority_tickers
    ):
        raise TerminalLineageBridgeError(
            "normalized terminal coverage is incomplete or extra"
        )
    records_by_ticker: dict[str, dict[str, Any]] = {}
    record_tickers: list[str] = []
    metadata_tickers: list[str] = []
    for index, ticker in enumerate(authority_tickers):
        record = _validate_terminal_record(
            records[index],
            ticker=ticker,
            selected=selected_by_ticker[ticker],
            parsed=parsed_by_ticker[ticker],
        )
        _validate_metadata(
            metadata_rows[index],
            ticker=ticker,
            selected=selected_by_ticker[ticker],
            parsed=parsed_by_ticker[ticker],
        )
        record_tickers.append(record["market_ticker"])
        metadata_tickers.append(
            _mapping("metadata evidence", metadata_rows[index])[
                "market_ticker"
            ]
        )
        if ticker in records_by_ticker:
            raise TerminalLineageBridgeError(
                "normalized output has a duplicate ticker"
            )
        records_by_ticker[ticker] = record
    if (
        record_tickers != list(authority_tickers)
        or metadata_tickers != list(authority_tickers)
    ):
        raise TerminalLineageBridgeError(
            "normalized output tickers are missing, extra, or unsorted"
        )

    expected_raw_evidence = [
        {**dict(attempt), "raw_reverified_sha256": attempt["raw_sha256"]}
        for attempt in attempts
    ]
    actual_raw_evidence = _list(
        "raw_response_evidence", output["raw_response_evidence"]
    )
    if actual_raw_evidence != expected_raw_evidence:
        raise TerminalLineageBridgeError(
            "normalized raw-response evidence differs from capture"
        )
    return records_by_ticker, [dict(row) for row in metadata_rows]


def _validate_eligibility_exclusion_response(
    raw: bytes,
    *,
    ticker: str,
    expected_price_level_structure: str,
) -> dict[str, Any]:
    envelope = _strict_official_json(
        raw, f"eligibility exclusion response {ticker}"
    )
    _strict_keys(
        f"eligibility exclusion response {ticker}",
        envelope,
        required={"market"},
    )
    market = _mapping(
        f"eligibility exclusion market {ticker}", envelope["market"]
    )
    required = {
        "ticker",
        "status",
        "result",
        "settlement_value_dollars",
        "price_level_structure",
    }
    missing = sorted(required - set(market))
    if missing:
        raise TerminalLineageBridgeError(
            "eligibility exclusion response is missing terminal fields: "
            + ",".join(missing)
        )
    if _ticker("eligibility exclusion market ticker", market["ticker"]) != (
        ticker
    ):
        raise TerminalLineageBridgeError(
            "eligibility exclusion response ticker mismatch"
        )
    if market["status"] != "finalized":
        raise TerminalLineageBridgeError(
            "eligibility exclusion market is not finalized"
        )
    result = _text("eligibility exclusion result", market["result"])
    if result not in {"yes", "no"}:
        raise TerminalLineageBridgeError(
            "eligibility exclusion result is not exact yes/no"
        )
    payout = _text(
        "eligibility exclusion settlement_value_dollars",
        market["settlement_value_dollars"],
    )
    expected_payout = "1.0000" if result == "yes" else "0.0000"
    if payout != expected_payout:
        raise TerminalLineageBridgeError(
            "eligibility exclusion result/payout disagree"
        )
    structure = _text(
        "eligibility exclusion price_level_structure",
        market["price_level_structure"],
    )
    if structure != expected_price_level_structure:
        raise TerminalLineageBridgeError(
            "eligibility exclusion price structure differs from pin"
        )
    if structure == "linear_cent":
        raise TerminalLineageBridgeError(
            "linear_cent market cannot be excluded from A01"
        )
    return {
        "ticker": ticker,
        "market_status": "finalized",
        "official_result": result,
        "yes_settlement_value_e4": 10_000 if result == "yes" else 0,
        "observed_price_level_structure": structure,
        "economic_record_admitted": False,
    }


def build_terminal_root_bundle(
    spec: Mapping[str, Any],
    *,
    require_root_read_only: bool,
    source_root_spec_raw_sha256: str | None = None,
) -> TerminalLineageBundle:
    """Verify all shards and return one exact, observation-only root receipt."""

    validated = _validate_spec(spec)
    if source_root_spec_raw_sha256 is None:
        if require_root_read_only:
            raise TerminalLineageBridgeError(
                "production terminal root requires its external raw SHA pin"
            )
        source_spec_sha256 = canonical_sha256(validated)
    else:
        source_spec_sha256 = _sha(
            "source terminal root spec raw SHA-256",
            source_root_spec_raw_sha256,
        )
    all_tickers: list[str] = []
    all_records: dict[str, Mapping[str, Any]] = {}
    all_record_rows: list[dict[str, Any]] = []
    terminal_record_index: list[dict[str, Any]] = []
    all_metadata_rows: list[dict[str, Any]] = []
    shard_receipts: list[dict[str, Any]] = []

    for shard in validated["shards"]:
        shard_id = shard["shard_id"]
        _, adapter_code_fact = _read_pinned_file(
            shard["adapter_code_path"],
            shard["adapter_code_sha256"],
            label=f"{shard_id} adapter code",
            maximum_bytes=MAX_CONTROL_BYTES,
            require_root_read_only=require_root_read_only,
        )
        authority_raw, authority_fact = _read_pinned_file(
            shard["authority_path"],
            shard["authority_raw_sha256"],
            label=f"{shard_id} authority",
            maximum_bytes=MAX_CONTROL_BYTES,
            require_root_read_only=require_root_read_only,
        )
        capture_raw, capture_fact = _read_pinned_file(
            shard["capture_receipt_path"],
            shard["capture_receipt_raw_sha256"],
            label=f"{shard_id} capture receipt",
            maximum_bytes=MAX_CONTROL_BYTES,
            require_root_read_only=require_root_read_only,
        )
        pins_raw, pins_fact = _read_pinned_file(
            shard["raw_pins_path"],
            shard["raw_pins_raw_sha256"],
            label=f"{shard_id} raw pins",
            maximum_bytes=MAX_CONTROL_BYTES,
            require_root_read_only=require_root_read_only,
        )
        output_raw, output_fact = _read_pinned_file(
            shard["normalized_output_path"],
            shard["normalized_output_raw_sha256"],
            label=f"{shard_id} normalized output",
            maximum_bytes=MAX_CONTROL_BYTES,
            require_root_read_only=require_root_read_only,
        )
        authority = _strict_json(authority_raw, f"{shard_id} authority")
        capture = _strict_json(capture_raw, f"{shard_id} capture receipt")
        pins = _strict_json(pins_raw, f"{shard_id} raw pins")
        output = _strict_json(output_raw, f"{shard_id} normalized output")
        authority, authority_tickers, _, _ = _validate_authority(
            authority,
            expected_raw_sha256=shard["authority_raw_sha256"],
            expected_code_sha256=shard["adapter_code_sha256"],
            expected_config_sha256=shard["adapter_config_sha256"],
        )
        attempts, selected_by_ticker = _validate_capture_receipt(
            capture,
            authority_raw_sha256=shard["authority_raw_sha256"],
            authority=authority,
            authority_tickers=authority_tickers,
            expected_code_sha256=shard["adapter_code_sha256"],
            expected_config_sha256=shard["adapter_config_sha256"],
        )
        _validate_raw_pins(
            pins,
            authority_raw_sha256=shard["authority_raw_sha256"],
            capture_receipt_raw_sha256=shard[
                "capture_receipt_raw_sha256"
            ],
            expected_code_sha256=shard["adapter_code_sha256"],
            expected_config_sha256=shard["adapter_config_sha256"],
            selected_by_ticker=selected_by_ticker,
        )

        raw_facts: list[dict[str, Any]] = []
        parsed_by_ticker: dict[str, dict[str, Any]] = {}
        capture_directory = Path(shard["capture_receipt_path"]).parent
        for attempt in attempts:
            raw_path = capture_directory / attempt["raw_relative_path"]
            raw_response, fact = _read_pinned_file(
                os.fspath(raw_path),
                attempt["raw_sha256"],
                label=(
                    f"{shard_id} raw response "
                    f"{attempt['ticker']}/{attempt['batch_attempt_index']}"
                ),
                maximum_bytes=MAX_RAW_RESPONSE_BYTES,
                require_root_read_only=require_root_read_only,
            )
            if fact["size_bytes"] != attempt["raw_size"]:
                raise TerminalLineageBridgeError(
                    "raw response byte size differs from capture receipt"
                )
            raw_facts.append(
                {
                    "ticker": attempt["ticker"],
                    "batch_attempt_index": attempt[
                        "batch_attempt_index"
                    ],
                    "raw_relative_path": attempt["raw_relative_path"],
                    "raw_size": attempt["raw_size"],
                    "raw_sha256": attempt["raw_sha256"],
                    "source_tier": attempt["source_tier"],
                    "http_status": attempt["http_status"],
                    "filesystem": fact,
                }
            )
            selected = selected_by_ticker[attempt["ticker"]]
            if (
                attempt["batch_attempt_index"]
                == selected["batch_attempt_index"]
            ):
                if attempt["ticker"] in parsed_by_ticker:
                    raise TerminalLineageBridgeError(
                        "capture has multiple selected responses per ticker"
                    )
                parsed_by_ticker[attempt["ticker"]] = (
                    _parse_eligible_terminal_response(
                        raw_response,
                        ticker=attempt["ticker"],
                    )
                )
        if sorted(parsed_by_ticker) != list(authority_tickers):
            raise TerminalLineageBridgeError(
                "selected raw response coverage is incomplete"
            )

        records, metadata_rows = _validate_normalized_output(
            output,
            authority_raw_sha256=shard["authority_raw_sha256"],
            capture_receipt_raw_sha256=shard[
                "capture_receipt_raw_sha256"
            ],
            raw_pins_raw_sha256=shard["raw_pins_raw_sha256"],
            expected_code_sha256=shard["adapter_code_sha256"],
            expected_config_sha256=shard["adapter_config_sha256"],
            authority_tickers=authority_tickers,
            attempts=attempts,
            selected_by_ticker=selected_by_ticker,
            parsed_by_ticker=parsed_by_ticker,
        )
        for ticker in authority_tickers:
            if ticker in all_records:
                raise TerminalLineageBridgeError(
                    f"duplicate ticker across shards: {ticker}"
                )
            all_records[ticker] = records[ticker]
            all_record_rows.append(records[ticker])
            terminal_record_index.append(
                {
                    "ticker": ticker,
                    "shard_id": shard_id,
                    "settlement_id": records[ticker]["settlement_id"],
                    "record_sha256": canonical_sha256(records[ticker]),
                    "observed_at_ns": records[ticker]["observed_at_ns"],
                    "selected_raw_response_sha256": records[ticker][
                        "source_sha256"
                    ],
                    "adapter_code_sha256": shard[
                        "adapter_code_sha256"
                    ],
                    "adapter_config_sha256": shard[
                        "adapter_config_sha256"
                    ],
                    "authority_raw_sha256": shard[
                        "authority_raw_sha256"
                    ],
                    "capture_receipt_raw_sha256": shard[
                        "capture_receipt_raw_sha256"
                    ],
                    "raw_pins_raw_sha256": shard[
                        "raw_pins_raw_sha256"
                    ],
                    "normalized_output_raw_sha256": shard[
                        "normalized_output_raw_sha256"
                    ],
                    "normalized_output_canonical_sha256": canonical_sha256(
                        output
                    ),
                }
            )
        all_tickers.extend(authority_tickers)
        all_metadata_rows.extend(metadata_rows)
        shard_receipts.append(
            {
                "shard_id": shard_id,
                "adapter_code_sha256": shard["adapter_code_sha256"],
                "adapter_config_sha256": shard["adapter_config_sha256"],
                "authority_raw_sha256": shard["authority_raw_sha256"],
                "capture_receipt_raw_sha256": shard[
                    "capture_receipt_raw_sha256"
                ],
                "raw_pins_raw_sha256": shard["raw_pins_raw_sha256"],
                "normalized_output_raw_sha256": shard[
                    "normalized_output_raw_sha256"
                ],
                "normalized_output_canonical_sha256": canonical_sha256(
                    output
                ),
                "tickers": authority_tickers,
                "tickers_sha256": canonical_sha256(authority_tickers),
                "terminal_records_sha256": canonical_sha256(
                    [records[ticker] for ticker in authority_tickers]
                ),
                "metadata_evidence_sha256": canonical_sha256(
                    metadata_rows
                ),
                "raw_response_evidence_sha256": canonical_sha256(
                    output["raw_response_evidence"]
                ),
                "raw_responses": raw_facts,
                "filesystem_controls": {
                    "adapter_code": adapter_code_fact,
                    "authority": authority_fact,
                    "capture_receipt": capture_fact,
                    "raw_pins": pins_fact,
                    "normalized_output": output_fact,
                },
            }
        )

    exclusion_receipts: list[dict[str, Any]] = []
    exclusion_tickers: list[str] = []
    for exclusion in validated["eligibility_exclusions"]:
        ticker = exclusion["ticker"]
        _, adapter_code_fact = _read_pinned_file(
            exclusion["adapter_code_path"],
            exclusion["adapter_code_sha256"],
            label=f"eligibility exclusion adapter code {ticker}",
            maximum_bytes=MAX_CONTROL_BYTES,
            require_root_read_only=require_root_read_only,
        )
        authority_raw, authority_fact = _read_pinned_file(
            exclusion["authority_path"],
            exclusion["authority_raw_sha256"],
            label=f"eligibility exclusion authority {ticker}",
            maximum_bytes=MAX_CONTROL_BYTES,
            require_root_read_only=require_root_read_only,
        )
        authority, authority_tickers, _, _ = _validate_authority(
            _strict_json(
                authority_raw,
                f"eligibility exclusion authority {ticker}",
            ),
            expected_raw_sha256=exclusion["authority_raw_sha256"],
            expected_code_sha256=exclusion["adapter_code_sha256"],
            expected_config_sha256=exclusion["adapter_config_sha256"],
        )
        if authority_tickers != [ticker]:
            raise TerminalLineageBridgeError(
                "eligibility exclusion authority must bind one exact ticker"
            )
        capture_raw, capture_fact = _read_pinned_file(
            exclusion["capture_receipt_path"],
            exclusion["capture_receipt_raw_sha256"],
            label=f"eligibility exclusion capture receipt {ticker}",
            maximum_bytes=MAX_CONTROL_BYTES,
            require_root_read_only=require_root_read_only,
        )
        pins_raw, pins_fact = _read_pinned_file(
            exclusion["raw_pins_path"],
            exclusion["raw_pins_raw_sha256"],
            label=f"eligibility exclusion raw pins {ticker}",
            maximum_bytes=MAX_CONTROL_BYTES,
            require_root_read_only=require_root_read_only,
        )
        attempts, selected_by_ticker = _validate_capture_receipt(
            _strict_json(
                capture_raw,
                f"eligibility exclusion capture receipt {ticker}",
            ),
            authority_raw_sha256=exclusion["authority_raw_sha256"],
            authority=authority,
            authority_tickers=authority_tickers,
            expected_code_sha256=exclusion["adapter_code_sha256"],
            expected_config_sha256=exclusion[
                "adapter_config_sha256"
            ],
        )
        _validate_raw_pins(
            _strict_json(
                pins_raw,
                f"eligibility exclusion raw pins {ticker}",
            ),
            authority_raw_sha256=exclusion["authority_raw_sha256"],
            capture_receipt_raw_sha256=exclusion[
                "capture_receipt_raw_sha256"
            ],
            expected_code_sha256=exclusion["adapter_code_sha256"],
            expected_config_sha256=exclusion[
                "adapter_config_sha256"
            ],
            selected_by_ticker=selected_by_ticker,
        )
        selected = selected_by_ticker[ticker]
        expected_selected = {
            "source_tier": exclusion["source_tier"],
            "http_status": exclusion["http_status"],
            "raw_size": exclusion["raw_response_size_bytes"],
            "raw_sha256": exclusion["raw_response_raw_sha256"],
        }
        if any(
            selected[field] != expected
            for field, expected in expected_selected.items()
        ):
            raise TerminalLineageBridgeError(
                "eligibility exclusion pin differs from selected capture"
            )
        capture_directory = Path(
            exclusion["capture_receipt_path"]
        ).parent
        selected_path = capture_directory / selected["raw_relative_path"]
        if Path(os.path.abspath(exclusion["raw_response_path"])) != Path(
            os.path.abspath(selected_path)
        ):
            raise TerminalLineageBridgeError(
                "eligibility exclusion raw path is not selected capture"
            )
        raw_facts: list[dict[str, Any]] = []
        selected_raw: bytes | None = None
        for attempt in attempts:
            raw_path = capture_directory / attempt["raw_relative_path"]
            raw, raw_fact = _read_pinned_file(
                os.fspath(raw_path),
                attempt["raw_sha256"],
                label=(
                    "eligibility exclusion raw response "
                    f"{ticker}/{attempt['batch_attempt_index']}"
                ),
                maximum_bytes=MAX_RAW_RESPONSE_BYTES,
                require_root_read_only=require_root_read_only,
            )
            if len(raw) != attempt["raw_size"]:
                raise TerminalLineageBridgeError(
                    "eligibility exclusion raw response size mismatch"
                )
            if (
                attempt["batch_attempt_index"]
                == selected["batch_attempt_index"]
            ):
                selected_raw = raw
            raw_facts.append(
                {
                    "ticker": ticker,
                    "batch_attempt_index": attempt[
                        "batch_attempt_index"
                    ],
                    "raw_relative_path": attempt["raw_relative_path"],
                    "raw_size": attempt["raw_size"],
                    "raw_sha256": attempt["raw_sha256"],
                    "source_tier": attempt["source_tier"],
                    "http_status": attempt["http_status"],
                    "filesystem": raw_fact,
                }
            )
        if selected_raw is None:
            raise TerminalLineageBridgeError(
                "eligibility exclusion selected raw response is absent"
            )
        parsed = _validate_eligibility_exclusion_response(
            selected_raw,
            ticker=ticker,
            expected_price_level_structure=exclusion[
                "observed_price_level_structure"
            ],
        )
        exclusion_tickers.append(ticker)
        exclusion_receipts.append(
            {
                "ticker": ticker,
                "reason_code": exclusion["reason_code"],
                "temporality": TEMPORALITY,
                "source_tier": exclusion["source_tier"],
                "http_status": exclusion["http_status"],
                "request_url": selected["request_url"],
                "observed_wall_ns_before": selected[
                    "fetch_wall_ns_before"
                ],
                "observed_wall_ns_after": selected[
                    "fetch_wall_ns_after"
                ],
                "observed_monotonic_ns_before": selected[
                    "fetch_monotonic_ns_before"
                ],
                "observed_monotonic_ns_after": selected[
                    "fetch_monotonic_ns_after"
                ],
                "adapter_code_sha256": exclusion[
                    "adapter_code_sha256"
                ],
                "adapter_config_sha256": exclusion[
                    "adapter_config_sha256"
                ],
                "authority_raw_sha256": exclusion[
                    "authority_raw_sha256"
                ],
                "capture_receipt_raw_sha256": exclusion[
                    "capture_receipt_raw_sha256"
                ],
                "raw_pins_raw_sha256": exclusion[
                    "raw_pins_raw_sha256"
                ],
                "raw_response_raw_sha256": exclusion[
                    "raw_response_raw_sha256"
                ],
                "raw_response_size_bytes": exclusion[
                    "raw_response_size_bytes"
                ],
                "observed_price_level_structure": exclusion[
                    "observed_price_level_structure"
                ],
                "economic_record_admitted": False,
                "terminal_fact": parsed,
                "raw_responses": raw_facts,
                "filesystem_controls": {
                    "adapter_code": adapter_code_fact,
                    "authority": authority_fact,
                    "capture_receipt": capture_fact,
                    "raw_pins": pins_fact,
                    "raw_response": raw_facts[-1]["filesystem"],
                },
            }
        )

    if (
        all_tickers != sorted(all_tickers)
        or len(all_tickers) != len(set(all_tickers))
    ):
        raise TerminalLineageBridgeError(
            "eligible terminal tickers must be globally sorted and unique"
        )
    required_tickers = validated["required_tickers"]
    covered_tickers = sorted(all_tickers + exclusion_tickers)
    if (
        covered_tickers != required_tickers
        or len(covered_tickers) != len(set(covered_tickers))
    ):
        missing = sorted(set(required_tickers) - set(covered_tickers))
        extra = sorted(set(covered_tickers) - set(required_tickers))
        duplicates = sorted(
            ticker
            for ticker in set(covered_tickers)
            if (all_tickers + exclusion_tickers).count(ticker) > 1
        )
        raise TerminalLineageBridgeError(
            "terminal coverage is not exact; "
            f"missing={missing}, extra={extra}, duplicates={duplicates}"
        )
    if not all_records:
        raise TerminalLineageBridgeError(
            "terminal bridge has zero settlement coverage"
        )

    payload: dict[str, Any] = {
        "schema_version": ROOT_RECEIPT_SCHEMA,
        "source_root_spec_raw_sha256": source_spec_sha256,
        "temporality": TEMPORALITY,
        "coverage_policy": COVERAGE_POLICY,
        "required_tickers": required_tickers,
        "required_tickers_sha256": canonical_sha256(required_tickers),
        "eligible_tickers": all_tickers,
        "eligible_tickers_sha256": canonical_sha256(all_tickers),
        "eligibility_exclusions": exclusion_receipts,
        "eligibility_exclusions_sha256": canonical_sha256(
            exclusion_receipts
        ),
        "adapter_version_policy": validated["adapter_version_policy"],
        "shards": shard_receipts,
        "shards_sha256": canonical_sha256(shard_receipts),
        "terminal_records": all_record_rows,
        "terminal_records_sha256": canonical_sha256(all_record_rows),
        "terminal_record_index": terminal_record_index,
        "terminal_record_index_sha256": canonical_sha256(
            terminal_record_index
        ),
        "metadata_evidence": all_metadata_rows,
        "metadata_evidence_sha256": canonical_sha256(all_metadata_rows),
        "resolved_capabilities": list(RESOLVED_CAPABILITIES),
        "remaining_blockers": list(REQUIRED_REMAINING_BLOCKERS),
        "historical_point_in_time_metadata_satisfied": False,
        "scheduled_start_authority_satisfied": False,
        "historical_lifecycle_intervals_satisfied": False,
        "network_reads_performed_by_bridge": 0,
        "s3_writes": 0,
        "financial_mutations": 0,
    }
    payload["payload_sha256"] = canonical_sha256(payload)
    return TerminalLineageBundle(
        receipt=payload,
        terminal_records_by_ticker=dict(all_records),
    )


def load_pinned_terminal_root_spec(
    path: Path,
    *,
    expected_raw_sha256: str,
    require_root_read_only: bool,
) -> Mapping[str, Any]:
    """Read and preflight one externally raw-pinned terminal-root spec."""

    raw, _fact = _read_pinned_file(
        os.fspath(path),
        expected_raw_sha256,
        label="terminal root spec",
        maximum_bytes=MAX_CONTROL_BYTES,
        require_root_read_only=require_root_read_only,
    )
    return _validate_spec(_strict_json(raw, "terminal root spec"))


__all__ = [
    "COVERAGE_POLICY",
    "EXPLICIT_PER_SHARD",
    "ROOT_RECEIPT_SCHEMA",
    "SINGLE_VERSION",
    "SPEC_SCHEMA",
    "TEMPORALITY",
    "TerminalLineageBridgeError",
    "TerminalLineageBundle",
    "build_terminal_root_bundle",
    "canonical_json_bytes",
    "canonical_sha256",
    "load_pinned_terminal_root_spec",
]
