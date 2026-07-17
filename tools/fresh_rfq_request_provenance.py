#!/usr/bin/env python3
"""Exact-object RFQ request provenance for the isolated fresh-RFQ lane.

The original API accepts the analysis RFQ identities already derived from a
valid overlay plus their exact object bodies.  The bounded reader API instead
accepts the same identities without bodies and an ``open_exact`` context
manager that yields one local ``.path`` at a time.  It never retains an
all-day body map or physical-line ledger.  Both paths verify the
identity/size/SHA binding, parse every physical NDJSON byte, and project only
valid ``rfq_created`` frames into the request schema consumed by
``fresh_rfq_market_mapping``.  The module itself performs no network, AWS,
resolver, deployment, or publication I/O.

The resulting receipt is body-free.  Matching caller-supplied bytes to an S3
VersionId does *not* attest that an exact-VersionId GET actually occurred, so
``source_objects_exact_get_verified`` remains false.  S3 inventory/list
completeness belongs to the overlay/source-evidence layer; this unit proves no
omission relative to the exact analysis object set that layer supplied.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
from typing import Any, Callable, Iterator


SCHEMA = "fresh-rfq-request-provenance-v1"
STATE = "ALL_BOUND_ANALYSIS_OBJECT_BYTES_PARSED_UNPUBLISHED"
VERIFICATION_STATE = "IDENTITY_SIZE_SHA_AND_ALL_PHYSICAL_ROWS_REBUILT"
SOURCE_BUCKET = "kalshi-vault-ritcardo"
MAX_JSON_LINE_BYTES = 16 << 20

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
CAPTURE_KEY_RE = re.compile(
    r"^ec2/raw/date=(\d{4}-\d{2}-\d{2})/rfq_"
    r"([01]\d|2[0-3])\.ndjson(?:\.([1-9][0-9]*))?$"
)

OVERLAY_IDENTITY_FIELDS = {"key", "version_id", "size", "sha256"}
EXACT_OBJECT_FIELDS = {
    "bucket", "key", "version_id", "size", "sha256", "body",
}
EXACT_READER_OBJECT_FIELDS = {
    "bucket", "key", "version_id", "size", "sha256",
}
REQUEST_FIELDS = {
    "request_id", "created_ts", "market_ticker", "mve_collection_ticker",
    "mve_selected_legs",
}

OUTER_REQUIRED_FIELDS = {
    "recv_mono_ns", "recv_wall_ns", "source", "channel", "source_ticker",
}
OUTER_OPTIONAL_FIELDS = {
    "source_event_time_ms", "source_sequence", "sid", "stream_epoch",
    "marker", "raw", "raw_b64",
}

OUTPUT_FIELDS = {
    "schema", "state", "verification_state", "source_bucket",
    "analysis_date", "authority_sha256", "source_evidence_sha256",
    "time_contract_sha256", "analysis_rfq_objects",
    "analysis_rfq_object_set_sha256", "analysis_rfq_object_count",
    "analysis_rfq_total_bytes", "object_coverage",
    "object_coverage_sha256", "physical_line_count",
    "physical_line_ledger_sha256", "marker_row_count",
    "marker_type_counts", "marker_type_counts_sha256",
    "irrelevant_frame_row_count", "irrelevant_type_counts",
    "irrelevant_type_counts_sha256", "rfq_created_occurrence_count",
    "rfq_created_occurrences", "rfq_created_occurrence_ledger_sha256",
    "rfq_created_unique_count", "rfq_created_exact_duplicate_count",
    "exact_duplicate_ledger", "exact_duplicate_ledger_sha256",
    "rfq_requests", "rfq_input_sha256", "id_collision_count",
    "invalid_relevant_frame_count", "all_input_objects_matched",
    "all_object_bytes_parsed", "all_physical_rows_classified",
    "all_unique_created_projected", "input_bodies_omitted",
    "source_objects_exact_get_verified", "data_objects_copied",
    "aws_read_performed_by_module", "aws_write_authorized",
    "research_eligible", "research_ready", "receipt_sha256",
}

UINT64_MAX = (1 << 64) - 1
UINT32_MAX = (1 << 32) - 1
INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1


class FreshRfqRequestProvenanceError(ValueError):
    """Stable fail-closed error for exact-body request provenance."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class _DuplicateJsonKey(ValueError):
    pass


def _fail(code: str, detail: str) -> None:
    raise FreshRfqRequestProvenanceError(code, detail)


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail("NON_CANONICAL_VALUE", str(exc))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


class _CanonicalListSha256:
    """Incrementally hash the exact canonical JSON encoding of a list."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()
        self._hash.update(b"[")
        self.count = 0

    def add(self, value: Any) -> None:
        if self.count:
            self._hash.update(b",")
        self._hash.update(canonical_bytes(value))
        self.count += 1

    def hexdigest(self) -> str:
        finalized = self._hash.copy()
        finalized.update(b"]")
        return finalized.hexdigest()


def _canonical_exact_equal(left: Any, right: Any) -> bool:
    return canonical_bytes(left) == canonical_bytes(right)


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value}")


def _strict_json_bytes(raw: bytes, label: str) -> Any:
    if type(raw) is not bytes:
        _fail("JSON_BYTES_REQUIRED", f"{label} must be bytes")
    if raw.startswith(b"\xef\xbb\xbf"):
        _fail("INVALID_JSON", f"{label} must not contain a UTF-8 BOM")
    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except _DuplicateJsonKey as exc:
        _fail("DUPLICATE_JSON_KEY", f"{label} duplicates key {exc}")
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError,
            RecursionError) as exc:
        _fail("INVALID_JSON", f"{label}: {exc}")


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        _fail("SCHEMA_FIELDS", f"{label} fields differ from contract")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _fail("INVALID_SHA256", f"{label} must be lowercase SHA-256")
    return value


def _date(value: Any, label: str = "analysis_date") -> str:
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        _fail("INVALID_DATE", f"{label} must be YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        _fail("INVALID_DATE", f"{label}: {exc}")
    if parsed.isoformat() != value:
        _fail("INVALID_DATE", f"{label} is not canonical")
    return value


def _text(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value or "\x00" in value or
            value != value.strip()):
        _fail("INVALID_TEXT", f"{label} must be canonical non-empty text")
    return value


def _version(value: Any, label: str) -> str:
    result = _text(value, label)
    if result.lower() == "null":
        _fail("VERSION_REQUIRED", f"{label} must be an exact VersionId")
    return result


def _integer(value: Any, label: str, *, minimum: int = 0,
             maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (
            maximum is not None and value > maximum):
        upper = "" if maximum is None else f" and <= {maximum}"
        _fail("INVALID_INTEGER", f"{label} must be >= {minimum}{upper}")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        _fail(
            "RFQ_CREATED_INVALID",
            f"{label} must be an exact non-whitespace identifier",
        )
    return value


def _parse_capture_key(key: Any, analysis_date: str,
                       label: str) -> tuple[str, int]:
    if not isinstance(key, str) or key.startswith("/") or "\\" in key:
        _fail("OBJECT_KEY_INVALID", f"{label} is not a safe source key")
    match = CAPTURE_KEY_RE.fullmatch(key)
    if match is None:
        _fail("OBJECT_KEY_INVALID", f"{label} is not an RFQ capture key")
    if match.group(1) != analysis_date:
        _fail("OBJECT_DATE_MISMATCH", f"{label} is outside analysis date D")
    hour = f"{match.group(1)}T{match.group(2)}"
    ordinal = 0 if match.group(3) is None else int(match.group(3))
    return hour, ordinal


def _normalize_overlay_identities(
    value: Any, analysis_date: str,
) -> tuple[list[dict[str, Any]], dict[str, tuple[str, int]]]:
    if not isinstance(value, list):
        _fail("OBJECT_LIST", "analysis_rfq_objects must be a list")
    rows: list[dict[str, Any]] = []
    path_parts: dict[str, tuple[str, int]] = {}
    for index, raw in enumerate(value):
        label = f"analysis_rfq_objects[{index}]"
        row = _exact_keys(raw, OVERLAY_IDENTITY_FIELDS, label)
        key = row["key"]
        hour, ordinal = _parse_capture_key(key, analysis_date, f"{label}.key")
        normalized = {
            "key": key,
            "version_id": _version(row["version_id"], f"{label}.version_id"),
            "size": _integer(row["size"], f"{label}.size", minimum=1),
            "sha256": _sha256(row["sha256"], f"{label}.sha256"),
        }
        if key in path_parts:
            _fail("OBJECT_DUPLICATE", f"duplicate overlay object key {key}")
        path_parts[key] = (hour, ordinal)
        rows.append(normalized)
    rows.sort(key=lambda row: row["key"])

    by_hour: dict[str, list[int]] = {}
    for key, (hour, ordinal) in path_parts.items():
        del key
        by_hour.setdefault(hour, []).append(ordinal)
    expected_hours = [f"{analysis_date}T{hour:02d}" for hour in range(24)]
    if sorted(by_hour) != expected_hours:
        _fail(
            "OBJECT_HOUR_COVERAGE",
            "analysis RFQ objects must cover exact D hours 00..23",
        )
    for hour, ordinals in by_hour.items():
        if sorted(ordinals) != list(range(len(ordinals))):
            _fail(
                "OBJECT_SHARD_CONTINUITY",
                f"{hour} shard ordinals are not contiguous from zero",
            )
    return rows, path_parts


def _normalize_exact_objects(
    value: Any,
    expected: list[dict[str, Any]],
    analysis_date: str,
) -> dict[str, bytes]:
    if not isinstance(value, list):
        _fail("OBJECT_LIST", "exact_analysis_rfq_objects must be a list")
    observed: dict[str, tuple[dict[str, Any], bytes]] = {}
    for index, raw in enumerate(value):
        label = f"exact_analysis_rfq_objects[{index}]"
        row = _exact_keys(raw, EXACT_OBJECT_FIELDS, label)
        if row["bucket"] != SOURCE_BUCKET:
            _fail("OBJECT_BUCKET_MISMATCH", f"{label}.bucket is not fixed source")
        key = row["key"]
        _parse_capture_key(key, analysis_date, f"{label}.key")
        identity = {
            "key": key,
            "version_id": _version(row["version_id"], f"{label}.version_id"),
            "size": _integer(row["size"], f"{label}.size", minimum=1),
            "sha256": _sha256(row["sha256"], f"{label}.sha256"),
        }
        body = row["body"]
        if type(body) is not bytes:
            _fail("BODY_BYTES_REQUIRED", f"{label}.body must be exact bytes")
        if key in observed:
            _fail("OBJECT_DUPLICATE", f"duplicate exact object key {key}")
        observed[key] = (identity, body)

    expected_by_key = {row["key"]: row for row in expected}
    if set(observed) != set(expected_by_key):
        _fail(
            "OBJECT_SET_MISMATCH",
            "exact body objects do not equal overlay analysis RFQ objects",
        )
    bodies: dict[str, bytes] = {}
    for key, expected_identity in expected_by_key.items():
        observed_identity, body = observed[key]
        if not _canonical_exact_equal(observed_identity, expected_identity):
            _fail("OBJECT_IDENTITY_MISMATCH", f"identity differs for {key}")
        if len(body) != expected_identity["size"]:
            _fail("OBJECT_SIZE_MISMATCH", f"body size differs for {key}")
        if hashlib.sha256(body).hexdigest() != expected_identity["sha256"]:
            _fail("OBJECT_SHA_MISMATCH", f"body SHA-256 differs for {key}")
        bodies[key] = body
    return bodies


def _normalize_exact_reader_objects(
    value: Any,
    expected: list[dict[str, Any]],
    analysis_date: str,
) -> dict[str, dict[str, Any]]:
    """Validate the complete body-free reader identity set before any I/O."""
    if not isinstance(value, list):
        _fail("OBJECT_LIST", "exact_analysis_rfq_objects must be a list")
    observed: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(value):
        label = f"exact_analysis_rfq_objects[{index}]"
        row = _exact_keys(raw, EXACT_READER_OBJECT_FIELDS, label)
        if row["bucket"] != SOURCE_BUCKET:
            _fail("OBJECT_BUCKET_MISMATCH", f"{label}.bucket is not fixed source")
        key = row["key"]
        _parse_capture_key(key, analysis_date, f"{label}.key")
        identity = {
            "bucket": SOURCE_BUCKET,
            "key": key,
            "version_id": _version(
                row["version_id"], f"{label}.version_id",
            ),
            "size": _integer(row["size"], f"{label}.size", minimum=1),
            "sha256": _sha256(row["sha256"], f"{label}.sha256"),
        }
        if key in observed:
            _fail("OBJECT_DUPLICATE", f"duplicate exact object key {key}")
        observed[key] = identity

    expected_by_key = {row["key"]: row for row in expected}
    if set(observed) != set(expected_by_key):
        _fail(
            "OBJECT_SET_MISMATCH",
            "exact reader objects do not equal overlay analysis RFQ objects",
        )
    for key, expected_identity in expected_by_key.items():
        projected = {
            field: observed[key][field] for field in OVERLAY_IDENTITY_FIELDS
        }
        if not _canonical_exact_equal(projected, expected_identity):
            _fail("OBJECT_IDENTITY_MISMATCH", f"identity differs for {key}")
    return observed


def _outer_integer_fields(outer: dict[str, Any], label: str) -> None:
    _integer(
        outer["recv_mono_ns"], f"{label}.recv_mono_ns",
        minimum=0, maximum=INT64_MAX,
    )
    _integer(
        outer["recv_wall_ns"], f"{label}.recv_wall_ns",
        minimum=1, maximum=INT64_MAX,
    )
    if "source_event_time_ms" in outer:
        value = outer["source_event_time_ms"]
        if type(value) is not int or not INT64_MIN <= value <= INT64_MAX:
            _fail("OUTER_SCHEMA_INVALID", f"{label}.source_event_time_ms invalid")
    if "source_sequence" in outer:
        _integer(
            outer["source_sequence"], f"{label}.source_sequence",
            maximum=UINT64_MAX,
        )
    if "sid" in outer:
        _integer(outer["sid"], f"{label}.sid", maximum=UINT64_MAX)
    if "stream_epoch" in outer:
        _integer(
            outer["stream_epoch"], f"{label}.stream_epoch",
            minimum=1, maximum=UINT32_MAX,
        )


def _normalize_outer(outer: Any, expected_hour: str,
                     label: str) -> dict[str, Any]:
    if not isinstance(outer, dict):
        _fail("OUTER_SCHEMA_INVALID", f"{label} must be an object")
    keys = set(outer)
    if (not OUTER_REQUIRED_FIELDS.issubset(keys) or
            not keys.issubset(OUTER_REQUIRED_FIELDS | OUTER_OPTIONAL_FIELDS)):
        _fail("OUTER_SCHEMA_INVALID", f"{label} fields differ from RawLogWriter")
    if ("raw" in outer) == ("raw_b64" in outer):
        _fail("OUTER_SCHEMA_INVALID", f"{label} needs exactly one payload field")
    if "raw_b64" in outer:
        _fail("RAW_B64_FORBIDDEN", f"{label} is not a parsed RFQ text frame")
    if outer.get("source") != "Kalshi":
        _fail("OUTER_SCHEMA_INVALID", f"{label}.source is not Kalshi")
    if (not isinstance(outer.get("channel"), str) or
            not isinstance(outer.get("source_ticker"), str) or
            not isinstance(outer.get("raw"), str)):
        _fail("OUTER_SCHEMA_INVALID", f"{label} text fields have wrong types")
    _outer_integer_fields(outer, label)
    seconds = outer["recv_wall_ns"] // 1_000_000_000
    try:
        observed_hour = dt.datetime.fromtimestamp(
            seconds, tz=dt.timezone.utc,
        ).strftime("%Y-%m-%dT%H")
    except (ValueError, OverflowError, OSError) as exc:
        _fail("OUTER_SCHEMA_INVALID", f"{label}.recv_wall_ns: {exc}")
    if observed_hour != expected_hour:
        _fail(
            "PARTITION_HOUR_MISMATCH",
            f"{label} receive hour {observed_hour} differs from {expected_hour}",
        )
    if "marker" in outer:
        marker = outer["marker"]
        if (not isinstance(marker, str) or not marker or "\x00" in marker or
                marker != marker.strip()):
            _fail("MARKER_INVALID", f"{label}.marker is invalid")
        if (outer["channel"] != "" or outer["source_ticker"] != "" or
                outer["raw"] != ""):
            _fail("MARKER_INVALID", f"{label} marker can carry no frame payload")
    return outer


def _wire_uint(value: Any) -> int | None:
    if type(value) is int and 0 <= value <= UINT64_MAX:
        return value
    return None


def _parse_inner_frame(outer: dict[str, Any], label: str) -> dict[str, Any]:
    try:
        raw_bytes = outer["raw"].encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        _fail("INNER_FRAME_INVALID", f"{label}.raw is not valid UTF-8: {exc}")
    frame = _strict_json_bytes(raw_bytes, f"{label}.raw")
    if not isinstance(frame, dict):
        _fail("INNER_FRAME_INVALID", f"{label}.raw must be a JSON object")
    frame_type = frame.get("type")
    if not isinstance(frame_type, str) or not frame_type:
        _fail("INNER_FRAME_INVALID", f"{label}.raw.type must be non-empty text")
    if outer["channel"] != frame_type:
        _fail("ENVELOPE_FRAME_MISMATCH", f"{label} channel/type differ")

    inner_sid = _wire_uint(frame.get("sid"))
    if "sid" in frame and inner_sid is None:
        _fail("INNER_FRAME_INVALID", f"{label}.raw.sid is not uint64")
    outer_sid = outer.get("sid") if "sid" in outer else None
    if (inner_sid is None and "sid" in outer) or (
            inner_sid is not None and outer_sid != inner_sid):
        _fail("ENVELOPE_FRAME_MISMATCH", f"{label} sid projection differs")
    inner_seq = _wire_uint(frame.get("seq"))
    if "seq" in frame and inner_seq is None:
        _fail("INNER_FRAME_INVALID", f"{label}.raw.seq is not uint64")
    outer_seq = outer.get("source_sequence") \
        if "source_sequence" in outer else None
    if (inner_seq is None and "source_sequence" in outer) or (
            inner_seq is not None and outer_seq != inner_seq):
        _fail("ENVELOPE_FRAME_MISMATCH", f"{label} seq projection differs")

    msg = frame.get("msg")
    expected_ticker = ""
    if isinstance(msg, dict) and isinstance(msg.get("market_ticker"), str):
        expected_ticker = msg["market_ticker"]
    if outer["source_ticker"] != expected_ticker:
        _fail("ENVELOPE_FRAME_MISMATCH", f"{label} source_ticker differs")
    return frame


def _fixed_exact(value: Any, label: str, places: int) -> None:
    if value is None or value == "":
        return
    if not isinstance(value, str) or re.fullmatch(r"\d+(?:\.\d+)?", value) is None:
        _fail("RFQ_CREATED_INVALID", f"{label} is not fixed-point text")
    try:
        scaled = Decimal(value) * (Decimal(10) ** places)
    except InvalidOperation as exc:
        _fail("RFQ_CREATED_INVALID", f"{label} is not decimal: {exc}")
    if scaled != scaled.to_integral_value():
        _fail("RFQ_CREATED_INVALID", f"{label} is finer than E{places}")


def _created_ts(value: Any, analysis_date: str, label: str) -> str:
    if not isinstance(value, str) or UTC_RE.fullmatch(value) is None:
        _fail("RFQ_CREATED_INVALID", f"{label} must be canonical UTC Z")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        _fail("RFQ_CREATED_INVALID", f"{label}: {exc}")
    start = dt.datetime.combine(
        dt.date.fromisoformat(analysis_date), dt.time(), tzinfo=dt.timezone.utc,
    )
    if not start <= parsed < start + dt.timedelta(days=1):
        _fail("RFQ_CREATED_CROSS_DATE", f"{label} is outside analysis date D")
    return value


def _project_rfq_created(
    frame: dict[str, Any], analysis_date: str, label: str,
) -> dict[str, Any]:
    sid = frame.get("sid")
    if type(sid) is not int or sid <= 0 or sid > UINT64_MAX:
        _fail("RFQ_CREATED_INVALID", f"{label}.sid must be a positive integer")
    msg = frame.get("msg")
    if not isinstance(msg, dict):
        _fail("RFQ_CREATED_INVALID", f"{label}.msg must be an object")
    for field in ("id", "creator_id", "market_ticker", "created_ts"):
        if field not in msg or msg[field] is None or not isinstance(msg[field], str):
            _fail("RFQ_CREATED_INVALID", f"{label}.msg.{field} is required text")

    request_id = _identifier(msg["id"], f"{label}.msg.id")
    market_ticker = _identifier(
        msg["market_ticker"], f"{label}.msg.market_ticker",
    )
    created_ts = _created_ts(
        msg["created_ts"], analysis_date, f"{label}.msg.created_ts",
    )

    for field in ("event_ticker", "mve_collection_ticker"):
        if msg.get(field) is not None and not isinstance(msg[field], str):
            _fail("RFQ_CREATED_INVALID", f"{label}.msg.{field} has wrong type")
    collection = msg.get("mve_collection_ticker")
    if collection is not None:
        collection = _identifier(collection, f"{label}.msg.mve_collection_ticker")
    _fixed_exact(msg.get("contracts_fp"), f"{label}.msg.contracts_fp", 2)
    _fixed_exact(
        msg.get("target_cost_dollars"),
        f"{label}.msg.target_cost_dollars",
        6,
    )

    raw_legs = msg.get("mve_selected_legs")
    if raw_legs is None:
        raw_legs = []
    if not isinstance(raw_legs, list):
        _fail("RFQ_COMBO_INVALID", f"{label}.msg.mve_selected_legs is not a list")
    legs: list[dict[str, str]] = []
    seen_tickers: set[str] = set()
    for index, leg in enumerate(raw_legs):
        leg_label = f"{label}.msg.mve_selected_legs[{index}]"
        if not isinstance(leg, dict):
            _fail("RFQ_COMBO_INVALID", f"{leg_label} must be an object")
        for field in ("event_ticker", "market_ticker", "side"):
            if leg.get(field) is not None and not isinstance(leg[field], str):
                _fail("RFQ_COMBO_INVALID", f"{leg_label}.{field} has wrong type")
        if leg.get("market_ticker") is None:
            _fail("RFQ_COMBO_INVALID", f"{leg_label}.market_ticker is required")
        ticker = _identifier(leg["market_ticker"], f"{leg_label}.market_ticker")
        if ticker in seen_tickers:
            _fail("RFQ_COMBO_INVALID", f"{label} repeats combo leg {ticker}")
        seen_tickers.add(ticker)
        _fixed_exact(
            leg.get("yes_settlement_value_dollars"),
            f"{leg_label}.yes_settlement_value_dollars",
            6,
        )
        legs.append({"market_ticker": ticker})
    if collection is not None and not legs:
        _fail("RFQ_COMBO_INVALID", f"{label} has collection ticker without legs")
    legs.sort(key=lambda row: row["market_ticker"])
    result = {
        "request_id": request_id,
        "created_ts": created_ts,
        "market_ticker": market_ticker,
        "mve_collection_ticker": collection,
        "mve_selected_legs": legs,
    }
    _exact_keys(result, REQUEST_FIELDS, f"{label} request projection")
    return result


def _count_rows(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _counts_list(counts: dict[str, int], key_name: str) -> list[dict[str, Any]]:
    return [
        {key_name: key, "count": counts[key]}
        for key in sorted(counts)
    ]


def _derive_request_provenance_from_line_sources(
    *,
    date_text: str,
    authority_sha: str,
    source_evidence_sha: str,
    time_contract_sha: str,
    identities: list[dict[str, Any]],
    path_parts: dict[str, tuple[str, int]],
    line_source: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    """Build the stable receipt while retaining no physical-line ledger."""
    natural_identities = sorted(
        identities,
        key=lambda row: (
            path_parts[row["key"]][0], path_parts[row["key"]][1], row["key"],
        ),
    )
    physical_line_digest = _CanonicalListSha256()
    object_coverage: list[dict[str, Any]] = []
    marker_counts: dict[str, int] = {}
    irrelevant_counts: dict[str, int] = {}
    # These ledgers are part of the published schema and remain proportional
    # to RFQ event volume. Removing them requires a separately versioned schema.
    rfq_occurrences: list[dict[str, Any]] = []
    duplicate_ledger: list[dict[str, Any]] = []
    primary_by_id: dict[str, dict[str, Any]] = {}

    for identity in natural_identities:
        key = identity["key"]
        expected_hour, _ordinal = path_parts[key]
        offset = 0
        line_number = 0
        object_line_digest = _CanonicalListSha256()
        with line_source(identity) as physical_lines:
            for physical in physical_lines:
                if type(physical) is not bytes:
                    _fail("READER_PROTOCOL", f"{key} yielded a non-bytes row")
                line_number += 1
                label = f"{key} line {line_number}"
                if len(physical) > MAX_JSON_LINE_BYTES:
                    _fail("LINE_TOO_LARGE", f"{label} exceeds limit")
                if not physical.endswith(b"\n"):
                    _fail("UNTERMINATED_LINE", f"{key} final row has no LF")
                payload = physical[:-1]
                if not payload:
                    _fail("BLANK_LINE", f"{label} is blank")
                if payload.endswith(b"\r"):
                    _fail("CRLF_FORBIDDEN", f"{label} is not LF-only")
                if payload != payload.strip():
                    _fail(
                        "OUTER_NONCANONICAL_WHITESPACE",
                        f"{label} has edge whitespace",
                    )
                outer = _normalize_outer(
                    _strict_json_bytes(payload, label), expected_hour, label,
                )
                classification: str
                record_type: str
                if "marker" in outer:
                    classification = "MARKER"
                    record_type = outer["marker"]
                    _count_rows(marker_counts, record_type)
                else:
                    frame = _parse_inner_frame(outer, label)
                    record_type = frame["type"]
                    if record_type != "rfq_created":
                        classification = "IRRELEVANT_FRAME"
                        _count_rows(irrelevant_counts, record_type)
                    else:
                        request = _project_rfq_created(frame, date_text, label)
                        raw_bytes = outer["raw"].encode("utf-8")
                        raw_sha = hashlib.sha256(raw_bytes).hexdigest()
                        request_sha = canonical_sha256(request)
                        locator = {
                            "object_key": key,
                            "version_id": identity["version_id"],
                            "line_number": line_number,
                        }
                        previous = primary_by_id.get(request["request_id"])
                        if previous is None:
                            classification = "RFQ_CREATED_PRIMARY"
                            primary = locator
                            primary_by_id[request["request_id"]] = {
                                # Retaining every raw frame made resident memory
                                # proportional to almost all RFQ input bytes.  A
                                # full SHA-256 is already the system's immutable
                                # byte identity, so keep only that fixed-size
                                # collision key plus the projected request.
                                "raw_sha256": raw_sha,
                                "request": request,
                                "locator": locator,
                            }
                            occurrence_kind = "PRIMARY"
                        elif previous["raw_sha256"] == raw_sha:
                            if not _canonical_exact_equal(
                                    previous["request"], request):
                                _fail(
                                    "RFQ_ID_COLLISION",
                                    f"{label} exact raw rebuilt a different request",
                                )
                            classification = "RFQ_CREATED_EXACT_DUPLICATE"
                            primary = previous["locator"]
                            occurrence_kind = "EXACT_RAW_DUPLICATE"
                        else:
                            _fail(
                                "RFQ_ID_COLLISION",
                                f"{label} repeats request_id with different raw bytes",
                            )
                        occurrence = {
                            "request_id": request["request_id"],
                            **locator,
                            "raw_sha256": raw_sha,
                            "request_sha256": request_sha,
                            "occurrence_kind": occurrence_kind,
                            "primary_object_key": primary["object_key"],
                            "primary_version_id": primary["version_id"],
                            "primary_line_number": primary["line_number"],
                        }
                        rfq_occurrences.append(occurrence)
                        if occurrence_kind == "EXACT_RAW_DUPLICATE":
                            duplicate_ledger.append(copy.deepcopy(occurrence))

                ledger_row = {
                    "object_key": key,
                    "version_id": identity["version_id"],
                    "line_number": line_number,
                    "byte_offset": offset,
                    "byte_length": len(physical),
                    "physical_line_sha256": hashlib.sha256(physical).hexdigest(),
                    "classification": classification,
                    "record_type": record_type,
                }
                physical_line_digest.add(ledger_row)
                object_line_digest.add(ledger_row)
                offset += len(physical)
        if offset != identity["size"]:
            _fail("BYTE_COVERAGE_MISMATCH", f"{key} byte cursor differs from size")
        object_coverage.append({
            **identity,
            "physical_line_count": line_number,
            "parsed_bytes": offset,
            "line_ledger_sha256": object_line_digest.hexdigest(),
        })

    requests = sorted(
        (row["request"] for row in primary_by_id.values()),
        key=lambda row: row["request_id"],
    )
    marker_type_counts = _counts_list(marker_counts, "marker")
    irrelevant_type_counts = _counts_list(irrelevant_counts, "frame_type")
    marker_row_count = sum(marker_counts.values())
    irrelevant_row_count = sum(irrelevant_counts.values())
    occurrence_count = len(rfq_occurrences)
    unique_count = len(requests)
    duplicate_count = len(duplicate_ledger)
    physical_count = physical_line_digest.count
    if physical_count != marker_row_count + irrelevant_row_count + occurrence_count:
        _fail("CLASSIFICATION_CONSERVATION", "not every physical row was classified")
    if occurrence_count != unique_count + duplicate_count:
        _fail("RFQ_OCCURRENCE_CONSERVATION", "RFQ occurrence counts do not conserve")

    result = {
        "schema": SCHEMA,
        "state": STATE,
        "verification_state": VERIFICATION_STATE,
        "source_bucket": SOURCE_BUCKET,
        "analysis_date": date_text,
        "authority_sha256": authority_sha,
        "source_evidence_sha256": source_evidence_sha,
        "time_contract_sha256": time_contract_sha,
        "analysis_rfq_objects": identities,
        "analysis_rfq_object_set_sha256": canonical_sha256(identities),
        "analysis_rfq_object_count": len(identities),
        "analysis_rfq_total_bytes": sum(row["size"] for row in identities),
        "object_coverage": object_coverage,
        "object_coverage_sha256": canonical_sha256(object_coverage),
        "physical_line_count": physical_count,
        "physical_line_ledger_sha256": physical_line_digest.hexdigest(),
        "marker_row_count": marker_row_count,
        "marker_type_counts": marker_type_counts,
        "marker_type_counts_sha256": canonical_sha256(marker_type_counts),
        "irrelevant_frame_row_count": irrelevant_row_count,
        "irrelevant_type_counts": irrelevant_type_counts,
        "irrelevant_type_counts_sha256": canonical_sha256(irrelevant_type_counts),
        "rfq_created_occurrence_count": occurrence_count,
        "rfq_created_occurrences": rfq_occurrences,
        "rfq_created_occurrence_ledger_sha256": canonical_sha256(rfq_occurrences),
        "rfq_created_unique_count": unique_count,
        "rfq_created_exact_duplicate_count": duplicate_count,
        "exact_duplicate_ledger": duplicate_ledger,
        "exact_duplicate_ledger_sha256": canonical_sha256(duplicate_ledger),
        "rfq_requests": requests,
        "rfq_input_sha256": canonical_sha256(requests),
        "id_collision_count": 0,
        "invalid_relevant_frame_count": 0,
        "all_input_objects_matched": True,
        "all_object_bytes_parsed": True,
        "all_physical_rows_classified": True,
        "all_unique_created_projected": True,
        "input_bodies_omitted": True,
        "source_objects_exact_get_verified": False,
        "data_objects_copied": 0,
        "aws_read_performed_by_module": False,
        "aws_write_authorized": False,
        "research_eligible": False,
        "research_ready": False,
    }
    result["receipt_sha256"] = canonical_sha256(result)
    return result


def _body_line_source(bodies: dict[str, bytes]) -> Callable[[dict[str, Any]], Any]:
    @contextmanager
    def source(identity: dict[str, Any]) -> Iterator[Iterator[bytes]]:
        body = bodies[identity["key"]]

        def rows() -> Iterator[bytes]:
            offset = 0
            while offset < len(body):
                newline = body.find(b"\n", offset)
                if newline < 0:
                    _fail(
                        "UNTERMINATED_LINE",
                        f"{identity['key']} final row has no LF",
                    )
                yield body[offset:newline + 1]
                offset = newline + 1

        yield rows()

    return source


def _reader_line_source(
    exact_objects: dict[str, dict[str, Any]],
    open_exact: Callable[[dict[str, Any]], Any],
) -> Callable[[dict[str, Any]], Any]:
    if not callable(open_exact):
        _fail("READER_REQUIRED", "open_exact must be callable")

    @contextmanager
    def source(identity: dict[str, Any]) -> Iterator[Iterator[bytes]]:
        exact_identity = exact_objects[identity["key"]]
        manager = open_exact(copy.deepcopy(exact_identity))
        if (not hasattr(manager, "__enter__") or
                not hasattr(manager, "__exit__")):
            _fail(
                "READER_PROTOCOL",
                f"open_exact for {identity['key']} must return a context manager",
            )
        stream_completed_and_verified = False
        with manager as opened:
            if not hasattr(opened, "path"):
                _fail(
                    "READER_PROTOCOL",
                    f"open_exact for {identity['key']} yielded no .path",
                )
            try:
                path = os.fspath(opened.path)
            except TypeError:
                _fail(
                    "READER_PROTOCOL",
                    f"open_exact for {identity['key']} yielded an invalid .path",
                )
            try:
                stream = open(path, "rb")
            except OSError as exc:
                _fail(
                    "READER_IO",
                    f"cannot open exact body for {identity['key']}: {exc}",
                )
            with stream:
                def rows() -> Iterator[bytes]:
                    nonlocal stream_completed_and_verified
                    digest = hashlib.sha256()
                    total = 0
                    while True:
                        try:
                            physical = stream.readline(MAX_JSON_LINE_BYTES + 1)
                        except OSError as exc:
                            _fail(
                                "READER_IO",
                                f"cannot read exact body for {identity['key']}: {exc}",
                            )
                        if not physical:
                            break
                        total += len(physical)
                        digest.update(physical)
                        yield physical
                    if total != identity["size"]:
                        _fail(
                            "OBJECT_SIZE_MISMATCH",
                            f"body size differs for {identity['key']}",
                        )
                    if digest.hexdigest() != identity["sha256"]:
                        _fail(
                            "OBJECT_SHA_MISMATCH",
                            f"body SHA-256 differs for {identity['key']}",
                        )
                    stream_completed_and_verified = True

                yield rows()
        # A hostile or simply incorrect context manager may return True from
        # __exit__ and suppress an exception raised while the generator above
        # verifies EOF/size/SHA.  This check runs outside that manager, making
        # successful full-stream verification an unsuppressible prerequisite.
        if not stream_completed_and_verified:
            _fail(
                "READER_VERIFICATION_INCOMPLETE",
                f"exact body was not fully verified for {identity['key']}",
            )

    return source


def _derive_request_provenance(
    *,
    analysis_date: Any,
    authority_sha256: Any,
    source_evidence_sha256: Any,
    time_contract_sha256: Any,
    analysis_rfq_objects: Any,
    exact_analysis_rfq_objects: Any,
) -> dict[str, Any]:
    date_text = _date(analysis_date)
    authority_sha = _sha256(authority_sha256, "authority_sha256")
    source_evidence_sha = _sha256(
        source_evidence_sha256, "source_evidence_sha256",
    )
    time_contract_sha = _sha256(time_contract_sha256, "time_contract_sha256")
    identities, path_parts = _normalize_overlay_identities(
        analysis_rfq_objects, date_text,
    )
    bodies = _normalize_exact_objects(
        exact_analysis_rfq_objects, identities, date_text,
    )
    return _derive_request_provenance_from_line_sources(
        date_text=date_text,
        authority_sha=authority_sha,
        source_evidence_sha=source_evidence_sha,
        time_contract_sha=time_contract_sha,
        identities=identities,
        path_parts=path_parts,
        line_source=_body_line_source(bodies),
    )


def _derive_request_provenance_from_reader(
    *,
    analysis_date: Any,
    authority_sha256: Any,
    source_evidence_sha256: Any,
    time_contract_sha256: Any,
    analysis_rfq_objects: Any,
    exact_analysis_rfq_objects: Any,
    open_exact: Any,
) -> dict[str, Any]:
    date_text = _date(analysis_date)
    authority_sha = _sha256(authority_sha256, "authority_sha256")
    source_evidence_sha = _sha256(
        source_evidence_sha256, "source_evidence_sha256",
    )
    time_contract_sha = _sha256(time_contract_sha256, "time_contract_sha256")
    identities, path_parts = _normalize_overlay_identities(
        analysis_rfq_objects, date_text,
    )
    # The complete set and every identity are checked before open_exact can run.
    reader_objects = _normalize_exact_reader_objects(
        exact_analysis_rfq_objects, identities, date_text,
    )
    return _derive_request_provenance_from_line_sources(
        date_text=date_text,
        authority_sha=authority_sha,
        source_evidence_sha=source_evidence_sha,
        time_contract_sha=time_contract_sha,
        identities=identities,
        path_parts=path_parts,
        line_source=_reader_line_source(reader_objects, open_exact),
    )


def build_request_provenance(
    *,
    analysis_date: str,
    authority_sha256: str,
    source_evidence_sha256: str,
    time_contract_sha256: str,
    analysis_rfq_objects: list[dict[str, Any]],
    exact_analysis_rfq_objects: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one body-free receipt from the complete bound analysis objects."""
    return _derive_request_provenance(
        analysis_date=analysis_date,
        authority_sha256=authority_sha256,
        source_evidence_sha256=source_evidence_sha256,
        time_contract_sha256=time_contract_sha256,
        analysis_rfq_objects=analysis_rfq_objects,
        exact_analysis_rfq_objects=exact_analysis_rfq_objects,
    )


def build_request_provenance_from_reader(
    *,
    analysis_date: str,
    authority_sha256: str,
    source_evidence_sha256: str,
    time_contract_sha256: str,
    analysis_rfq_objects: list[dict[str, Any]],
    exact_analysis_rfq_objects: list[dict[str, Any]],
    open_exact: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    """Build from body-free identities, opening one exact object at a time.

    ``open_exact(identity)`` must return a context manager whose yielded value
    has a filesystem ``.path``. The file is consumed once with bounded
    ``readline`` calls while size and SHA-256 are recomputed. The receipt flags
    remain conservative because this module does not attest the reader's AWS
    implementation or authorize publication.
    """
    return _derive_request_provenance_from_reader(
        analysis_date=analysis_date,
        authority_sha256=authority_sha256,
        source_evidence_sha256=source_evidence_sha256,
        time_contract_sha256=time_contract_sha256,
        analysis_rfq_objects=analysis_rfq_objects,
        exact_analysis_rfq_objects=exact_analysis_rfq_objects,
        open_exact=open_exact,
    )


def validate_request_provenance(
    value: Any,
    *,
    analysis_date: str,
    authority_sha256: str,
    source_evidence_sha256: str,
    time_contract_sha256: str,
    analysis_rfq_objects: list[dict[str, Any]],
    exact_analysis_rfq_objects: list[dict[str, Any]],
) -> dict[str, Any]:
    """Rebuild a receipt from the original exact bodies and compare exactly."""
    value = _exact_keys(value, OUTPUT_FIELDS, "request provenance receipt")
    supplied = _sha256(value["receipt_sha256"], "receipt_sha256")
    unsigned = copy.deepcopy(value)
    unsigned.pop("receipt_sha256")
    if supplied != canonical_sha256(unsigned):
        _fail("RECEIPT_DIGEST_MISMATCH", "receipt_sha256 mismatch")
    expected = _derive_request_provenance(
        analysis_date=analysis_date,
        authority_sha256=authority_sha256,
        source_evidence_sha256=source_evidence_sha256,
        time_contract_sha256=time_contract_sha256,
        analysis_rfq_objects=analysis_rfq_objects,
        exact_analysis_rfq_objects=exact_analysis_rfq_objects,
    )
    if not _canonical_exact_equal(value, expected):
        _fail(
            "PROVENANCE_REBUILD_MISMATCH",
            "receipt differs from exact-body deterministic rebuild",
        )
    return copy.deepcopy(value)


def validate_request_provenance_from_reader(
    value: Any,
    *,
    analysis_date: str,
    authority_sha256: str,
    source_evidence_sha256: str,
    time_contract_sha256: str,
    analysis_rfq_objects: list[dict[str, Any]],
    exact_analysis_rfq_objects: list[dict[str, Any]],
    open_exact: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    """Rebuild through the bounded reader and compare the receipt exactly."""
    value = _exact_keys(value, OUTPUT_FIELDS, "request provenance receipt")
    supplied = _sha256(value["receipt_sha256"], "receipt_sha256")
    unsigned = copy.deepcopy(value)
    unsigned.pop("receipt_sha256")
    if supplied != canonical_sha256(unsigned):
        _fail("RECEIPT_DIGEST_MISMATCH", "receipt_sha256 mismatch")
    expected = _derive_request_provenance_from_reader(
        analysis_date=analysis_date,
        authority_sha256=authority_sha256,
        source_evidence_sha256=source_evidence_sha256,
        time_contract_sha256=time_contract_sha256,
        analysis_rfq_objects=analysis_rfq_objects,
        exact_analysis_rfq_objects=exact_analysis_rfq_objects,
        open_exact=open_exact,
    )
    if not _canonical_exact_equal(value, expected):
        _fail(
            "PROVENANCE_REBUILD_MISMATCH",
            "receipt differs from exact-reader deterministic rebuild",
        )
    return copy.deepcopy(value)
