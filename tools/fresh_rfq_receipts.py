#!/usr/bin/env python3
"""Pure builders for the isolated post-T0 fresh-RFQ evidence lane.

This module performs no filesystem, network, AWS, process, or publication I/O.
Callers must supply exact-version inventory rows and may supply a read-only
resolver callback.  Every local rejection happens before that callback runs.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from typing import Any, Callable


AUTHORITY_SCHEMA = "fresh-rfq-epoch-authority-v1"
HOUR_SCHEMA = "canonical-rfq-hour-receipt-v1"
OVERLAY_SCHEMA = "research-rfq-overlay-manifest-v1"
SOURCE_EVIDENCE_SCHEMA = "fresh-rfq-source-evidence-v1"
LANE_ID = "W-RFQ-FRESH-01"
OPERATOR_AUTHORIZATION_SHA256 = (
    "8aaa7e40f4d1cb414d7b06be213aa10dc37bcc4d054c729d3e96eb1c884ef66e"
)
OLD_284_OBJECT_COUNT = 284
OLD_284_OBJECT_SET_SHA256 = (
    "1873803765e70de69f4b398dcea7e4dc66c2749950c9d5f8c0d2d198c5087c71"
)
BLOCKED_SELECTION_SHA256 = (
    "8b310c37f3989770d1f53a058e5ef396e1eb5c9c3d24d07ac87bd9d8aee5b9e1"
)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
GENERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HOUR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T([0-2][0-9])$")
T0_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T([0-2][0-9]):00:00Z$")
UTC_SECONDS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\dZ$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CONTAINER_KEY_RE = re.compile(
    r"^ec2/raw/date=(\d{4}-\d{2}-\d{2})/rfq_receipts_"
    r"([01]\d|2[0-3])\.ndjson(?:\.([1-9][0-9]*))?$")
SOURCE_BUCKET = "kalshi-vault-ritcardo"
RAW_KEY_PREFIX = "ec2/raw"
CAPTURE_TEMPLATE = "date={UTC_DATE}/rfq_{UTC_HOUR}.ndjson"
READ_ONLY_SCOPE = {
    "kalshi_api_key_scope": "read",
    "kalshi_mode": "data_collect",
    "aws_source_access": "read-only",
}


class FreshRfqError(RuntimeError):
    """A fail-closed contract violation with a stable error code."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str) -> None:
    raise FreshRfqError(code, detail)


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail("NON_CANONICAL_VALUE", str(exc))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        _fail("INVALID_SHA256", f"{label} must be lowercase 64-hex")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail("INVALID_TEXT", f"{label} must be non-empty text")
    return value


def _size(value: Any, label: str, *, positive: bool = True) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        _fail("INVALID_SIZE", f"{label} is invalid")
    return value


def _version(value: Any, label: str) -> str:
    value = _text(value, label)
    if value.lower() == "null":
        _fail("VERSION_REQUIRED", f"{label} must be an exact non-null VersionId")
    return value


def _safe_rel(value: Any, label: str) -> str:
    value = _text(value, label)
    if value.startswith("/") or "\\" in value:
        _fail("UNSAFE_PATH", f"{label} is not a portable relative path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        _fail("UNSAFE_PATH", f"{label} escapes containment")
    return "/".join(parts)


def _safe_key(value: Any, label: str) -> str:
    return _safe_rel(value, label)


def _hour(value: Any, label: str = "hour") -> dt.datetime:
    if not isinstance(value, str) or HOUR_RE.fullmatch(value) is None:
        _fail("INVALID_HOUR", f"{label} must be YYYY-MM-DDTHH")
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H").replace(
            tzinfo=dt.timezone.utc)
    except ValueError as exc:
        _fail("INVALID_HOUR", f"{label}: {exc}")
    if parsed.strftime("%Y-%m-%dT%H") != value:
        _fail("INVALID_HOUR", f"{label} is not canonical")
    return parsed


def _t0(value: Any) -> dt.datetime:
    if not isinstance(value, str) or T0_RE.fullmatch(value) is None:
        _fail("INVALID_T0", "strict_t0_utc must be an exact UTC hour")
    return _hour(value[:13], "strict_t0_utc")


def _utc_seconds(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or UTC_SECONDS_RE.fullmatch(value) is None:
        _fail("INVALID_TIMESTAMP", f"{label} must be canonical UTC seconds")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
    except ValueError as exc:
        _fail("INVALID_TIMESTAMP", f"{label}: {exc}")


def _date(value: Any, label: str) -> dt.date:
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        _fail("INVALID_DATE", f"{label} must be YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        _fail("INVALID_DATE", f"{label}: {exc}")
    return parsed


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        _fail("SCHEMA_FIELDS", f"{label} fields differ from contract")
    return value


def _normalize_identity(row: Any, label: str) -> dict[str, Any]:
    row = _exact_keys(row, {"key", "size", "sha256"}, label)
    return {
        "key": _safe_key(row["key"], f"{label}.key"),
        "size": _size(row["size"], f"{label}.size"),
        "sha256": _sha(row["sha256"], f"{label}.sha256"),
    }


def old_object_set_sha256(rows: list[dict[str, Any]]) -> str:
    """Historical deep01 identity algorithm: sorted TSV, no final newline."""
    normalized = [_normalize_identity(row, f"identity[{i}]")
                  for i, row in enumerate(rows)]
    normalized.sort(key=lambda row: row["key"])
    if len({row["key"] for row in normalized}) != len(normalized):
        _fail("DUPLICATE_IDENTITY", "old identity keys must be unique")
    payload = "\n".join(
        f"{row['key']}\t{row['size']}\t{row['sha256']}" for row in normalized
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_fresh_epoch_authority(
    *, operator_authorization_sha256: str, strict_t0_utc: str,
    deployment_commit: str, generation: str, created_at_utc: str,
    deny_identities: list[dict[str, Any]],
) -> dict[str, Any]:
    rows = [_normalize_identity(row, f"deny_identities[{i}]")
            for i, row in enumerate(deny_identities)]
    rows.sort(key=lambda row: row["key"])
    authority = {
        "schema": AUTHORITY_SCHEMA,
        "lane_id": LANE_ID,
        "operator_authorization_sha256": _sha(
            operator_authorization_sha256, "operator_authorization_sha256"),
        "strict_t0_utc": strict_t0_utc,
        "created_at_utc": created_at_utc,
        "deployment_commit": deployment_commit,
        "generation": generation,
        "source_bucket": SOURCE_BUCKET,
        "raw_key_prefix": RAW_KEY_PREFIX,
        "capture_template": CAPTURE_TEMPLATE,
        "credential_scope": copy.deepcopy(READ_ONLY_SCOPE),
        "old_lineage_state": "DATA_INTEGRITY_BLOCKED",
        "repair_state": "FORBIDDEN",
        "blocked_logical_prefix": "raw_rfq",
        "old_284_object_count": OLD_284_OBJECT_COUNT,
        "old_284_object_set_sha256": OLD_284_OBJECT_SET_SHA256,
        "blocked_selection_sha256": BLOCKED_SELECTION_SHA256,
        "deny_identities": rows,
    }
    authority["authority_sha256"] = canonical_sha256(authority)
    return validate_fresh_epoch_authority(authority)


def validate_fresh_epoch_authority(value: Any) -> dict[str, Any]:
    fields = {
        "schema", "lane_id", "operator_authorization_sha256", "strict_t0_utc",
        "created_at_utc", "deployment_commit", "generation", "source_bucket",
        "raw_key_prefix", "capture_template", "credential_scope",
        "old_lineage_state", "repair_state",
        "blocked_logical_prefix", "old_284_object_count",
        "old_284_object_set_sha256", "blocked_selection_sha256",
        "deny_identities", "authority_sha256",
    }
    value = _exact_keys(value, fields, "fresh epoch authority")
    if value["schema"] != AUTHORITY_SCHEMA or value["lane_id"] != LANE_ID:
        _fail("AUTHORITY_SCHEMA", "wrong fresh RFQ lane/schema")
    _sha(value["operator_authorization_sha256"], "operator authorization")
    if value["operator_authorization_sha256"] != OPERATOR_AUTHORIZATION_SHA256:
        _fail("OPERATOR_AUTHORIZATION_BINDING",
              "authority is not bound to the approved operator text")
    strict_t0 = _t0(value["strict_t0_utc"])
    if _utc_seconds(value["created_at_utc"], "created_at_utc") >= strict_t0:
        _fail("AUTHORITY_TIME_ORDER", "authority must be created before strict T0")
    if not isinstance(value["deployment_commit"], str) or \
            COMMIT_RE.fullmatch(value["deployment_commit"]) is None:
        _fail("DEPLOYMENT_BINDING", "deployment_commit must be exact 40-hex")
    if (not isinstance(value["generation"], str) or
            GENERATION_RE.fullmatch(value["generation"]) is None):
        _fail("GENERATION_BINDING",
              "generation must be a 1..64 character safe ASCII ID")
    fixed = {
        "old_lineage_state": "DATA_INTEGRITY_BLOCKED",
        "repair_state": "FORBIDDEN",
        "blocked_logical_prefix": "raw_rfq",
        "old_284_object_count": OLD_284_OBJECT_COUNT,
        "old_284_object_set_sha256": OLD_284_OBJECT_SET_SHA256,
        "blocked_selection_sha256": BLOCKED_SELECTION_SHA256,
        "source_bucket": SOURCE_BUCKET,
        "raw_key_prefix": RAW_KEY_PREFIX,
        "capture_template": CAPTURE_TEMPLATE,
        "credential_scope": READ_ONLY_SCOPE,
    }
    if any(value.get(key) != expected for key, expected in fixed.items()):
        _fail("OLD_LINEAGE_BINDING", "historical blocked lineage binding changed")
    if not isinstance(value["deny_identities"], list):
        _fail("DENY_SET_INVALID", "deny_identities must be a list")
    rows = [_normalize_identity(row, f"deny_identities[{i}]")
            for i, row in enumerate(value["deny_identities"])]
    rows.sort(key=lambda row: row["key"])
    if len(rows) != OLD_284_OBJECT_COUNT:
        _fail("DENY_SET_INCOMPLETE", "the complete 284-object deny set is required")
    if any(not row["key"].startswith("raw_rfq/") for row in rows):
        _fail("DENY_SET_INVALID", "deny keys must use the raw_rfq logical prefix")
    if old_object_set_sha256(rows) != OLD_284_OBJECT_SET_SHA256:
        _fail("DENY_SET_FINGERPRINT", "deny set is not the historical 284-object set")
    normalized = copy.deepcopy(value)
    normalized["deny_identities"] = rows
    supplied = _sha(value["authority_sha256"], "authority_sha256")
    unsigned = dict(normalized)
    unsigned.pop("authority_sha256")
    if supplied != canonical_sha256(unsigned):
        _fail("AUTHORITY_DIGEST", "authority_sha256 mismatch")
    return normalized


def _normalize_source_seal(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    fields = {"bucket", "key", "version_id", "size", "sha256",
              "verification_state"}
    value = _exact_keys(value, fields, "source_seal")
    seal = {
        "bucket": _text(value["bucket"], "source_seal.bucket"),
        "key": _safe_key(value["key"], "source_seal.key"),
        "version_id": _version(value["version_id"], "source_seal.version_id"),
        "size": _size(value["size"], "source_seal.size"),
        "sha256": _sha(value["sha256"], "source_seal.sha256"),
        "verification_state": value["verification_state"],
    }
    if seal["verification_state"] != "PASS":
        _fail("SOURCE_SEAL_INVALID", "provided source seal is not verified PASS")
    return seal


def validate_v3_segment_receipt(
    receipt: Any, authority: Any,
) -> dict[str, Any]:
    authority = validate_fresh_epoch_authority(authority)
    if not isinstance(receipt, dict):
        _fail("SEGMENT_SCHEMA", "segment receipt must be an object")
    if receipt.get("schema") == "rfq-segment-receipt-v2":
        _fail("V2_DIAGNOSTIC_ONLY", "historical v2 receipts cannot enter fresh RFQ")
    if receipt.get("schema") != "rfq-segment-receipt-v3":
        _fail("SEGMENT_SCHEMA", "rfq-segment-receipt-v3 is required")
    segment_fields = {
        "type", "schema", "lane_id", "authority_sha256", "deployment_commit",
        "generation", "fresh_lane_state", "supervisor_pid", "child_pid",
        "child_generation", "child_subscription_ack_wall_ns",
        "child_subscription_ack_identity_sha256",
        "child_subscription_ack_count", "segment_hour",
        "expected_start_wall_ns", "expected_end_wall_ns",
        "child_started_wall_ns", "segment_started_wall_ns",
        "receipt_observed_wall_ns", "start_lag_ms", "end_early_ms",
        "end_reason", "boundary_closed", "close_stability_ms", "child_rc",
        "status", "subscription_proven", "capture_relpath",
        "capture_origin_path", "capture_bytes_before", "capture_bytes_at_close",
        "capture_sha256_at_close", "capture_shards", "capture_shard_count",
        "capture_shard_set_sha256", "raw_evidence", "metrics_evidence",
        "findings", "generated_at_utc",
    }
    if set(receipt) != segment_fields:
        _fail("SEGMENT_SCHEMA", "v3 segment fields differ from exact contract")
    bindings = {
        "lane_id": authority["lane_id"],
        "authority_sha256": authority["authority_sha256"],
        "deployment_commit": authority["deployment_commit"],
        "generation": authority["generation"],
    }
    if any(receipt.get(key) != expected for key, expected in bindings.items()):
        _fail("SEGMENT_AUTHORITY_BINDING", "segment generation/authority mismatch")
    if receipt.get("fresh_lane_state") != "BOUND_AUTHORITY":
        _fail("SEGMENT_AUTHORITY_BINDING",
              "only BOUND_AUTHORITY segment generations are eligible")
    for field in ("supervisor_pid", "child_pid", "child_generation"):
        if type(receipt.get(field)) is not int or receipt[field] <= 0:
            _fail("SEGMENT_AUTHORITY_BINDING",
                  f"{field} must be a positive integer")
    if receipt.get("type") != "rfq_segment_receipt":
        _fail("SEGMENT_SCHEMA", "wrong receipt type")
    hour_text = receipt.get("segment_hour")
    start = _hour(hour_text, "segment_hour")
    end = start + dt.timedelta(hours=1)
    expected_start = int(start.timestamp()) * 1_000_000_000
    expected_end = int(end.timestamp()) * 1_000_000_000
    child_ack_wall = receipt.get("child_subscription_ack_wall_ns")
    child_ack_identity = receipt.get("child_subscription_ack_identity_sha256")
    child_ack_count = receipt.get("child_subscription_ack_count")
    if (type(child_ack_wall) is not int or child_ack_wall <= 0 or
            child_ack_wall > expected_end or child_ack_count != 1 or
            not isinstance(child_ack_identity, str) or
            SHA_RE.fullmatch(child_ack_identity) is None):
        _fail("SEGMENT_AUTHORITY_BINDING",
              "persistent child subscription ACK identity is invalid")
    if (receipt.get("expected_start_wall_ns") != expected_start or
            receipt.get("expected_end_wall_ns") != expected_end):
        _fail("SEGMENT_BOUNDARY", "hour wall-clock bounds mismatch")
    child_started = receipt.get("child_started_wall_ns")
    segment_started = receipt.get("segment_started_wall_ns")
    observed = receipt.get("receipt_observed_wall_ns")
    if (type(child_started) is not int or child_started <= 0 or
            child_ack_wall < child_started or
            type(segment_started) is not int or
            not expected_start <= segment_started <= expected_start + 5_000_000_000 or
            child_started > segment_started or
            type(observed) is not int or observed < expected_end or
            receipt.get("start_lag_ms") !=
            max(0, (segment_started - expected_start) // 1_000_000) or
            receipt.get("end_early_ms") != 0):
        _fail("SEGMENT_BOUNDARY", "segment start/end timing proof is invalid")
    if (receipt.get("status") != "PASS" or receipt.get("findings") != [] or
            receipt.get("boundary_closed") is not True or
            receipt.get("end_reason") != "boundary"):
        _fail("SEGMENT_NOT_PASS", "strict PASS/empty findings/closed boundary required")
    if receipt.get("subscription_proven") is not True:
        _fail("SEGMENT_NOT_PASS", "subscription proof is required")
    if receipt.get("child_rc") is not None:
        _fail("SEGMENT_NOT_PASS", "boundary PASS must not have a child exit code")
    if type(receipt.get("close_stability_ms")) is not int or \
            receipt["close_stability_ms"] < 1000:
        _fail("SEGMENT_BOUNDARY", "at least 1000ms stable close is required")
    raw = receipt.get("raw_evidence")
    raw_fields = {
        "recorder_rows", "subscribed_communications",
        "subscription_ack_wall_ns", "subscription_ack_identity_sha256",
        "rfq_created", "rfq_deleted", "markers",
        "partition_mismatches", "max_stream_epoch",
        "subscription_invalidations", "subscription_proven_at_end", "findings",
        "start_offsets", "end_offsets", "shards",
    }
    if not isinstance(raw, dict) or raw.get("findings") != []:
        _fail("SEGMENT_NOT_PASS", "raw_evidence findings must be empty")
    if set(raw) != raw_fields:
        _fail("RAW_COVERAGE", "raw_evidence fields differ from contract")
    markers = raw.get("markers")
    recorder_rows = raw.get("recorder_rows")
    subscribed = raw.get("subscribed_communications")
    ack_wall = raw.get("subscription_ack_wall_ns")
    ack_identity = raw.get("subscription_ack_identity_sha256")
    raw_subscription_end = raw.get("subscription_proven_at_end")
    offsets_valid = all(
        isinstance(mapping, dict) and
        all(isinstance(key, str) and key and type(offset) is int and offset >= 0
            for key, offset in mapping.items())
        for mapping in (raw.get("start_offsets"), raw.get("end_offsets")))
    if (type(recorder_rows) is not int or recorder_rows <= 0 or
            type(subscribed) is not int or not 0 <= subscribed <= recorder_rows or
            not isinstance(markers, dict) or markers != {"hour_open": 1} or
            type(raw.get("rfq_created")) is not int or raw["rfq_created"] < 0 or
            type(raw.get("rfq_deleted")) is not int or raw["rfq_deleted"] < 0 or
            raw.get("partition_mismatches") != 0 or
            type(raw.get("max_stream_epoch")) is not int or
            raw["max_stream_epoch"] < 0 or
            raw.get("subscription_invalidations") != 0 or
            "subscription_proven_at_end" not in raw or
            not (raw_subscription_end is True or raw_subscription_end is None) or
            ((subscribed > 0) != (type(ack_wall) is int)) or
            ((subscribed > 0) != (
                isinstance(ack_identity, str) and
                SHA_RE.fullmatch(ack_identity) is not None)) or
            (subscribed > 0 and not expected_start <= ack_wall < expected_end) or
            (subscribed > 0 and raw_subscription_end is not True) or
            (subscribed > 0 and (
                ack_wall != child_ack_wall or
                ack_identity != child_ack_identity or subscribed != 1)) or
            not offsets_valid or not isinstance(raw.get("shards"), list) or
            not raw["shards"] or
            any(not isinstance(path, str) or not path for path in raw["shards"])):
        _fail("RAW_COVERAGE", "raw hour/subscription coverage is incomplete")
    metrics = receipt.get("metrics_evidence")
    metrics_fields = {
        "feed_rows", "connected_valid_rows", "min_reconnects",
        "max_reconnects", "min_disconnects", "max_disconnects",
        "min_errors", "max_errors", "max_recorder_dropped",
        "max_recorder_write_failures", "first_ts_ms", "last_ts_ms",
        "findings", "end_offset", "next_window_offset",
    }
    if not isinstance(metrics, dict) or metrics.get("findings") != []:
        _fail("SEGMENT_NOT_PASS", "metrics_evidence findings must be empty")
    if set(metrics) != metrics_fields:
        _fail("METRICS_COVERAGE", "metrics_evidence fields differ from contract")
    feed_rows = metrics.get("feed_rows")
    connected = metrics.get("connected_valid_rows")
    lo_ms, hi_ms = expected_start // 1_000_000, expected_end // 1_000_000
    if (type(feed_rows) is not int or feed_rows < 3500 or
            type(connected) is not int or connected != feed_rows or
            type(metrics.get("first_ts_ms")) is not int or
            not lo_ms <= metrics["first_ts_ms"] <= lo_ms + 5_000 or
            type(metrics.get("last_ts_ms")) is not int or
            not hi_ms - 5_000 <= metrics["last_ts_ms"] < hi_ms or
            any(metrics.get(field) != 0 for field in (
                "min_reconnects", "min_disconnects", "min_errors",
                "max_reconnects", "max_disconnects", "max_errors",
                "max_recorder_dropped", "max_recorder_write_failures")) or
            type(metrics.get("end_offset")) is not int or
            type(metrics.get("next_window_offset")) is not int or
            not 0 <= metrics["next_window_offset"] <= metrics["end_offset"]):
        _fail("METRICS_COVERAGE", "metrics do not prove full connected coverage")
    shards = receipt.get("capture_shards")
    if not isinstance(shards, list) or not shards:
        _fail("SHARD_SET_INVALID", "capture_shards must be non-empty")
    normalized_shards = []
    date_text, hour_number = hour_text.split("T")
    base_rel = f"date={date_text}/rfq_{hour_number}.ndjson"
    shard_fields = {"ordinal", "relpath", "bytes_before", "size",
                    "parsed_bytes_at_close", "sha256"}
    for index, row in enumerate(shards):
        row = _exact_keys(row, shard_fields, f"capture_shards[{index}]")
        if row["ordinal"] != index:
            _fail("SHARD_GAP", "shard ordinals must be contiguous from zero")
        expected_rel = base_rel if index == 0 else f"{base_rel}.{index}"
        relpath = _safe_rel(row["relpath"], f"capture_shards[{index}].relpath")
        if relpath != expected_rel:
            _fail("SHARD_PATH", "shard path does not match its hour/ordinal")
        if row["bytes_before"] != 0:
            _fail("PREEXISTING_BYTES", "fresh shard bytes_before must be zero")
        size = _size(row["size"], f"capture_shards[{index}].size")
        if row["parsed_bytes_at_close"] != size:
            _fail("TORN_SHARD", "parsed_bytes_at_close must equal attested size")
        normalized_shards.append({
            "ordinal": index,
            "relpath": relpath,
            "bytes_before": 0,
            "size": size,
            "parsed_bytes_at_close": size,
            "sha256": _sha(row["sha256"], f"capture_shards[{index}].sha256"),
        })
    total = sum(row["size"] for row in normalized_shards)
    if (receipt.get("capture_relpath") != base_rel or
            receipt.get("capture_bytes_before") != 0 or
            receipt.get("capture_bytes_at_close") != total or
            receipt.get("capture_shard_count") != len(normalized_shards) or
            receipt.get("capture_shard_set_sha256") !=
            canonical_sha256(normalized_shards)):
        _fail("SHARD_SET_DIGEST", "capture shard aggregate binding mismatch")
    compatibility_sha = normalized_shards[0]["sha256"] \
        if len(normalized_shards) == 1 else None
    if receipt.get("capture_sha256_at_close") != compatibility_sha:
        _fail("SHARD_SET_DIGEST", "single-shard compatibility SHA mismatch")
    normalized = copy.deepcopy(receipt)
    normalized["capture_shards"] = normalized_shards
    canonical_bytes(normalized)
    return normalized


def _normalize_inventory(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _fail("INVENTORY_INVALID", f"{label} must be a list")
    fields = {"key", "version_id", "size", "sha256"}
    rows = []
    for index, row in enumerate(value):
        row = _exact_keys(row, fields, f"{label}[{index}]")
        rows.append({
            "key": _safe_key(row["key"], f"{label}[{index}].key"),
            "version_id": _version(row["version_id"],
                                   f"{label}[{index}].version_id"),
            "size": _size(row["size"], f"{label}[{index}].size"),
            "sha256": _sha(row["sha256"], f"{label}[{index}].sha256"),
        })
    rows.sort(key=lambda row: row["key"])
    if len({row["key"] for row in rows}) != len(rows):
        _fail("INVENTORY_DUPLICATE", f"{label} has duplicate keys")
    return rows


SOURCE_PROOF_FIELDS = {
    "segment_hour", "role", "status", "findings", "fresh_lane_state",
    "authority_sha256", "generation", "container_bucket", "container_key",
    "container_version_id", "container_size", "container_sha256",
    "container_seal_member", "line_number", "byte_offset", "byte_length",
    "outer_row_sha256", "raw_segment_canonical_sha256",
    "capture_exact_object_set_sha256",
}


def _physical_projection(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [{
        "key": row["key"], "version_id": row["version_id"],
        "size": row["size"], "sha256": row["sha256"],
    } for row in objects]
    rows.sort(key=lambda row: row["key"])
    return rows


def _capture_exact_identity_sha256(
    objects: list[dict[str, Any]], bucket: str,
) -> str:
    rows = [{"bucket": bucket, **row} for row in _physical_projection(objects)]
    return canonical_sha256(rows)


def _normalize_source_proof(
    value: Any, authority: dict[str, Any], segment_hour: str,
    raw_segment_sha256: str, physical_objects: list[dict[str, Any]],
) -> dict[str, Any]:
    value = _exact_keys(value, SOURCE_PROOF_FIELDS, "source_proof")
    if value["segment_hour"] != segment_hour:
        _fail("SOURCE_PROOF_BINDING", "source proof hour differs from receipt")
    if value["role"] not in ("ANALYSIS", "WATERMARK"):
        _fail("SOURCE_PROOF_BINDING", "source proof role is invalid")
    fixed = {
        "status": "PASS", "findings": [],
        "fresh_lane_state": "BOUND_AUTHORITY",
        "authority_sha256": authority["authority_sha256"],
        "generation": authority["generation"],
        "container_bucket": authority["source_bucket"],
        "raw_segment_canonical_sha256": raw_segment_sha256,
        "capture_exact_object_set_sha256": _capture_exact_identity_sha256(
            physical_objects, authority["source_bucket"]),
    }
    if any(value.get(key) != expected for key, expected in fixed.items()):
        _fail("SOURCE_PROOF_BINDING",
              "source proof authority/segment/capture binding differs")
    container_key = _safe_key(value["container_key"], "container_key")
    matched = CONTAINER_KEY_RE.fullmatch(container_key)
    expected_close = (_hour(segment_hour) + dt.timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H")
    if matched is None or f"{matched.group(1)}T{matched.group(2)}" != expected_close:
        _fail("SOURCE_PROOF_CONTAINER",
              "receipt container key does not match the segment close hour")
    container_size = _size(value["container_size"], "container_size")
    line_number = _size(value["line_number"], "line_number")
    byte_offset = _size(value["byte_offset"], "byte_offset", positive=False)
    byte_length = _size(value["byte_length"], "byte_length")
    if byte_offset + byte_length > container_size:
        _fail("SOURCE_PROOF_CONTAINER", "receipt line exceeds container bounds")
    if type(value["container_seal_member"]) is not bool:
        _fail("SOURCE_PROOF_CONTAINER", "container_seal_member must be boolean")
    normalized = copy.deepcopy(value)
    normalized.update({
        "container_key": container_key,
        "container_version_id": _version(
            value["container_version_id"], "container_version_id"),
        "container_size": container_size,
        "container_sha256": _sha(value["container_sha256"], "container_sha256"),
        "line_number": line_number, "byte_offset": byte_offset,
        "byte_length": byte_length,
        "outer_row_sha256": _sha(value["outer_row_sha256"], "outer_row_sha256"),
        "raw_segment_canonical_sha256": _sha(
            value["raw_segment_canonical_sha256"],
            "raw_segment_canonical_sha256"),
        "capture_exact_object_set_sha256": _sha(
            value["capture_exact_object_set_sha256"],
            "capture_exact_object_set_sha256"),
    })
    return normalized


def _source_analysis_date(segment_hour: str, role: str) -> str:
    value = _hour(segment_hour, "segment_hour")
    if role == "WATERMARK":
        value -= dt.timedelta(days=1)
    return value.strftime("%Y-%m-%d")


def build_hour_receipt(
    *, authority: Any, segment_receipt: Any,
    exact_inventory: list[dict[str, Any]], raw_key_prefix: str,
    source_proof: Any, source_evidence_sha256: str,
    source_seal: Any = None,
    resolver: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    authority = validate_fresh_epoch_authority(authority)
    # The cutoff is deliberately checked before receipt validation or any
    # optional resolver/read callback.
    if isinstance(segment_receipt, dict):
        segment_hour = segment_receipt.get("segment_hour")
    else:
        segment_hour = None
    start = _hour(segment_hour, "segment_hour")
    if start < _t0(authority["strict_t0_utc"]):
        _fail("BEFORE_STRICT_T0", "pre-T0 RFQ evidence is permanently ineligible")
    segment = validate_v3_segment_receipt(segment_receipt, authority)
    prefix = _safe_key(raw_key_prefix, "raw_key_prefix")
    if prefix != authority["raw_key_prefix"]:
        _fail("SOURCE_SCOPE", "raw_key_prefix differs from epoch authority")
    inventory = _normalize_inventory(exact_inventory, "exact_inventory")
    expected = []
    # Object keys can change across copy/restore paths.  Old-lineage exclusion
    # is therefore content identity, not a logical-key comparison.
    denied = {(row["size"], row["sha256"])
              for row in authority["deny_identities"]}
    for shard in segment["capture_shards"]:
        source_key = f"{prefix}/{shard['relpath']}"
        logical_key = f"{authority['blocked_logical_prefix']}/{shard['relpath']}"
        row = {
            "ordinal": shard["ordinal"], "relpath": shard["relpath"],
            "key": source_key, "version_id": None, "size": shard["size"],
            "sha256": shard["sha256"], "logical_key": logical_key,
        }
        expected.append(row)
    by_key = {row["key"]: row for row in inventory}
    if set(by_key) != {row["key"] for row in expected}:
        _fail("INVENTORY_NOT_EXACT", "missing or post-receipt extra RFQ shard")
    for row in expected:
        observed = by_key[row["key"]]
        if (observed["size"], observed["sha256"]) != \
                (row["size"], row["sha256"]):
            _fail("INVENTORY_MISMATCH", "RFQ shard size/SHA mismatch")
        row["version_id"] = observed["version_id"]
        if (row["size"], row["sha256"]) in denied:
            _fail("OLD_LINEAGE_OVERLAP", "fresh set overlaps a blocked old identity")
    raw_segment_sha = canonical_sha256(segment)
    source_proof = _normalize_source_proof(
        source_proof, authority, segment["segment_hour"], raw_segment_sha,
        expected)
    source_evidence_sha256 = _sha(
        source_evidence_sha256, "source_evidence_sha256")
    analysis_date = _source_analysis_date(
        segment["segment_hour"], source_proof["role"])
    seal = _normalize_source_seal(source_seal)
    if seal is not None and (
            seal["bucket"] != authority["source_bucket"] or
            seal["key"] != f"ec2/warehouse/seals/date={analysis_date}.json"):
        _fail("SOURCE_SEAL_INVALID",
              "hour seal must be analysis D's full_v2 cross-day seal")
    resolution_state = "NOT_REQUESTED"
    if resolver is not None:
        for inventory_row in inventory:
            try:
                resolved = resolver(copy.deepcopy(inventory_row))
            except Exception as exc:
                _fail("RESOLVER_FAILED", str(exc))
            got = _normalize_inventory([resolved], "resolved_inventory")[0]
            if got != inventory_row:
                _fail("RESOLVER_MISMATCH", "resolver did not return the exact version")
        resolution_state = "RESOLVED_EXACT"
    result = {
        "schema": HOUR_SCHEMA,
        "lane_id": LANE_ID,
        "state": "LOCALLY_VERIFIED_UNPUBLISHED",
        "segment_hour": segment["segment_hour"],
        "analysis_date": analysis_date,
        "authority_sha256": authority["authority_sha256"],
        "strict_t0_utc": authority["strict_t0_utc"],
        "source_bucket": authority["source_bucket"],
        "source_seal": seal,
        "source_seal_binding_sha256": None if seal is None else seal["sha256"],
        "capture_receipt_sha256": raw_segment_sha,
        "capture_shard_set_sha256": segment["capture_shard_set_sha256"],
        "raw_key_prefix": prefix,
        "rfq_objects": expected,
        "rfq_object_set_sha256": canonical_sha256(expected),
        "resolution_state": resolution_state,
        "supervisor_pid": segment["supervisor_pid"],
        "child_pid": segment["child_pid"],
        "child_generation": segment["child_generation"],
        "child_subscription_ack_wall_ns":
            segment["child_subscription_ack_wall_ns"],
        "child_subscription_ack_identity_sha256":
            segment["child_subscription_ack_identity_sha256"],
        "child_subscription_ack_count":
            segment["child_subscription_ack_count"],
        "subscription_ack_count":
            segment["raw_evidence"]["subscribed_communications"],
        "subscription_ack_wall_ns":
            segment["raw_evidence"]["subscription_ack_wall_ns"],
        "subscription_invalidations":
            segment["raw_evidence"]["subscription_invalidations"],
        "subscription_proven_at_end":
            segment["raw_evidence"]["subscription_proven_at_end"],
        "source_proof": source_proof,
        "source_proof_sha256": canonical_sha256(source_proof),
        "source_evidence_sha256": source_evidence_sha256,
        "complete": True,
        "research_eligible": False,
    }
    result["receipt_sha256"] = canonical_sha256(result)
    return validate_hour_receipt(result, authority)


def validate_hour_receipt(value: Any, authority: Any) -> dict[str, Any]:
    authority = validate_fresh_epoch_authority(authority)
    fields = {
        "schema", "lane_id", "state", "segment_hour", "authority_sha256",
        "analysis_date",
        "strict_t0_utc", "source_bucket", "source_seal",
        "source_seal_binding_sha256",
        "capture_receipt_sha256", "capture_shard_set_sha256", "raw_key_prefix",
        "rfq_objects", "rfq_object_set_sha256", "resolution_state", "complete",
        "research_eligible", "supervisor_pid", "child_pid", "child_generation",
        "child_subscription_ack_wall_ns",
        "child_subscription_ack_identity_sha256",
        "child_subscription_ack_count",
        "subscription_ack_count", "subscription_ack_wall_ns",
        "subscription_invalidations", "subscription_proven_at_end",
        "source_proof", "source_proof_sha256", "source_evidence_sha256",
        "receipt_sha256",
    }
    value = _exact_keys(value, fields, "hour receipt")
    fixed = {
        "schema": HOUR_SCHEMA, "lane_id": LANE_ID,
        "state": "LOCALLY_VERIFIED_UNPUBLISHED",
        "authority_sha256": authority["authority_sha256"],
        "strict_t0_utc": authority["strict_t0_utc"],
        "source_bucket": authority["source_bucket"],
        "complete": True, "research_eligible": False,
    }
    if any(value.get(key) != expected for key, expected in fixed.items()):
        _fail("HOUR_RECEIPT_INVALID", "fixed hour receipt contract changed")
    if _hour(value["segment_hour"]) < _t0(authority["strict_t0_utc"]):
        _fail("BEFORE_STRICT_T0", "hour receipt predates the fresh epoch")
    prefix = _safe_key(value["raw_key_prefix"], "raw_key_prefix")
    if prefix != authority["raw_key_prefix"]:
        _fail("SOURCE_SCOPE", "raw_key_prefix differs from epoch authority")
    seal = _normalize_source_seal(value["source_seal"])
    if value["source_seal_binding_sha256"] != \
            (None if seal is None else seal["sha256"]):
        _fail("SOURCE_SEAL_INVALID", "source seal SHA binding mismatch")
    _sha(value["capture_receipt_sha256"], "capture_receipt_sha256")
    _sha(value["capture_shard_set_sha256"], "capture_shard_set_sha256")
    if value["resolution_state"] not in ("NOT_REQUESTED", "RESOLVED_EXACT"):
        _fail("HOUR_RECEIPT_INVALID", "unknown resolution_state")
    hour_start_ns = int(_hour(value["segment_hour"]).timestamp()) * 1_000_000_000
    hour_end_ns = hour_start_ns + 3_600_000_000_000
    for field in ("supervisor_pid", "child_pid", "child_generation"):
        if type(value.get(field)) is not int or value[field] <= 0:
            _fail("HOUR_RECEIPT_INVALID", f"{field} must be positive")
    child_ack_wall = value.get("child_subscription_ack_wall_ns")
    child_ack_identity = value.get("child_subscription_ack_identity_sha256")
    if (type(child_ack_wall) is not int or child_ack_wall <= 0 or
            child_ack_wall > hour_end_ns or
            value.get("child_subscription_ack_count") != 1 or
            not isinstance(child_ack_identity, str) or
            SHA_RE.fullmatch(child_ack_identity) is None):
        _fail("HOUR_RECEIPT_INVALID",
              "persistent child subscription ACK binding is invalid")
    ack_count = value.get("subscription_ack_count")
    ack_wall = value.get("subscription_ack_wall_ns")
    subscription_end = value.get("subscription_proven_at_end")
    if (type(ack_count) is not int or ack_count < 0 or
            value.get("subscription_invalidations") != 0 or
            not (subscription_end is True or subscription_end is None) or
            ((ack_count > 0) != (type(ack_wall) is int)) or
            (ack_count > 0 and not hour_start_ns <= ack_wall < hour_end_ns) or
            (ack_count > 0 and ack_wall != child_ack_wall) or
            (ack_count > 0 and subscription_end is not True)):
        _fail("HOUR_RECEIPT_INVALID", "subscription generation proof is invalid")
    objects = value["rfq_objects"]
    if not isinstance(objects, list) or not objects:
        _fail("HOUR_RECEIPT_INVALID", "rfq_objects must be non-empty")
    expected_fields = {"ordinal", "relpath", "key", "version_id", "size",
                       "sha256", "logical_key"}
    normalized = []
    date_text, hour_number = value["segment_hour"].split("T")
    base_rel = f"date={date_text}/rfq_{hour_number}.ndjson"
    denied = {(row["size"], row["sha256"])
              for row in authority["deny_identities"]}
    for index, row in enumerate(objects):
        row = _exact_keys(row, expected_fields, f"rfq_objects[{index}]")
        relpath = _safe_rel(row["relpath"], f"rfq_objects[{index}].relpath")
        expected_rel = base_rel if index == 0 else f"{base_rel}.{index}"
        item = {
            "ordinal": index, "relpath": relpath,
            "key": _safe_key(row["key"], f"rfq_objects[{index}].key"),
            "version_id": _version(row["version_id"],
                                   f"rfq_objects[{index}].version_id"),
            "size": _size(row["size"], f"rfq_objects[{index}].size"),
            "sha256": _sha(row["sha256"], f"rfq_objects[{index}].sha256"),
            "logical_key": _safe_key(row["logical_key"],
                                     f"rfq_objects[{index}].logical_key"),
        }
        if (row["ordinal"] != index or relpath != expected_rel or
                item["key"] != f"{prefix}/{relpath}" or
                item["logical_key"] != f"raw_rfq/{relpath}"):
            _fail("OBJECT_MAPPING", "RFQ source/logical mapping is not exact")
        if (item["size"], item["sha256"]) in denied:
            _fail("OLD_LINEAGE_OVERLAP", "hour receipt contains blocked identity")
        normalized.append(item)
    if len({row["key"] for row in normalized}) != len(normalized):
        _fail("INVENTORY_DUPLICATE", "hour receipt has duplicate source keys")
    if value["rfq_object_set_sha256"] != canonical_sha256(normalized):
        _fail("OBJECT_SET_DIGEST", "rfq_object_set_sha256 mismatch")
    capture_receipt_sha = _sha(
        value["capture_receipt_sha256"], "capture_receipt_sha256")
    source_proof = _normalize_source_proof(
        value["source_proof"], authority, value["segment_hour"],
        capture_receipt_sha, normalized)
    analysis_date = _source_analysis_date(
        value["segment_hour"], source_proof["role"])
    if value["analysis_date"] != analysis_date or (seal is not None and (
            seal["bucket"] != authority["source_bucket"] or
            seal["key"] != f"ec2/warehouse/seals/date={analysis_date}.json")):
        _fail("SOURCE_SEAL_INVALID",
              "hour/source proof does not bind analysis D's full_v2 seal")
    if (value["source_proof_sha256"] != canonical_sha256(source_proof) or
            _sha(value["source_evidence_sha256"], "source_evidence_sha256") !=
            value["source_evidence_sha256"]):
        _fail("SOURCE_PROOF_DIGEST", "source proof/evidence digest binding mismatch")
    unsigned = copy.deepcopy(value)
    unsigned["rfq_objects"] = normalized
    supplied = _sha(unsigned.pop("receipt_sha256"), "receipt_sha256")
    if supplied != canonical_sha256(unsigned):
        _fail("HOUR_RECEIPT_DIGEST", "receipt_sha256 mismatch")
    normalized_value = copy.deepcopy(value)
    normalized_value["source_seal"] = seal
    normalized_value["rfq_objects"] = normalized
    normalized_value["source_proof"] = source_proof
    return normalized_value


def _normalize_base_release(value: Any, authority: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "release_id", "date", "manifest_bucket", "manifest_key",
        "manifest_version_id", "manifest_sha256", "reference_set_sha256",
        "canonical_receipt_set_sha256", "verification_state", "source_seal",
        "sealed_rfq_objects", "sealed_rfq_object_set_sha256",
    }
    value = _exact_keys(value, fields, "base release")
    release_id = _text(value["release_id"], "base release.release_id")
    if re.fullmatch(r"[A-Za-z0-9_.-]+", release_id) is None:
        _fail("BASE_RELEASE_INVALID", "release_id is unsafe")
    release_date = value["date"]
    _date(release_date, "base release.date")
    manifest_key = _safe_key(value["manifest_key"], "base release.manifest_key")
    if manifest_key != f"research/releases/{release_id}/MANIFEST.json":
        _fail("BASE_RELEASE_INVALID", "manifest key/release ID binding mismatch")
    seal = _normalize_source_seal(value["source_seal"])
    if (seal is None or seal["bucket"] != authority["source_bucket"] or
            seal["key"] != f"ec2/warehouse/seals/date={release_date}.json"):
        _fail("BASE_SEAL_INVALID", "base must bind D's verified full-day seal")
    sealed = _normalize_inventory(value["sealed_rfq_objects"],
                                  "base release.sealed_rfq_objects")
    if not sealed:
        _fail("BASE_SEAL_INVALID", "D seal RFQ subset must be non-empty")
    sealed_sha = _sha(value["sealed_rfq_object_set_sha256"],
                      "sealed_rfq_object_set_sha256")
    if sealed_sha != canonical_sha256(sealed):
        _fail("BASE_SEAL_INVALID", "sealed RFQ subset digest mismatch")
    if value["verification_state"] != "REFERENCE_V3_VERIFIED":
        _fail("BASE_RELEASE_INVALID", "verified REFERENCE_V3 base is required")
    return {
        "release_id": release_id,
        "date": release_date,
        "manifest_bucket": _text(value["manifest_bucket"], "manifest_bucket"),
        "manifest_key": manifest_key,
        "manifest_version_id": _version(
            value["manifest_version_id"], "manifest_version_id"),
        "manifest_sha256": _sha(value["manifest_sha256"], "manifest_sha256"),
        "reference_set_sha256": _sha(
            value["reference_set_sha256"], "reference_set_sha256"),
        "canonical_receipt_set_sha256": _sha(
            value["canonical_receipt_set_sha256"],
            "canonical_receipt_set_sha256"),
        "verification_state": "REFERENCE_V3_VERIFIED",
        "source_seal": seal,
        "sealed_rfq_objects": sealed,
        "sealed_rfq_object_set_sha256": sealed_sha,
    }


def _overlay_components(
    authority: dict[str, Any], base: dict[str, Any], hour_receipts: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
           list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(hour_receipts, list):
        _fail("HOUR_CONTINUITY", "hour_receipts must be a list")
    hours = [validate_hour_receipt(row, authority) for row in hour_receipts]
    hours.sort(key=lambda row: row["segment_hour"])
    if len({row["segment_hour"] for row in hours}) != len(hours):
        _fail("HOUR_DUPLICATE", "duplicate canonical hour receipt")
    start = dt.datetime.combine(
        _date(base["date"], "base release.date"), dt.time(),
        tzinfo=dt.timezone.utc)
    expected_analysis = [
        (start + dt.timedelta(hours=index)).strftime("%Y-%m-%dT%H")
        for index in range(24)
    ]
    expected_watermark = [
        (start + dt.timedelta(hours=24 + index)).strftime("%Y-%m-%dT%H")
        for index in range(2)
    ]
    if [row["segment_hour"] for row in hours] != \
            expected_analysis + expected_watermark:
        _fail("HOUR_CONTINUITY", "exact D 00..23 plus D+1 00/01 is required")
    if any(row["resolution_state"] != "RESOLVED_EXACT" for row in hours):
        _fail("OVERLAY_RESOLUTION",
              "all 26 overlay hours must be independently RESOLVED_EXACT")
    supervisor_pid = hours[0]["supervisor_pid"]
    previous = None
    for row in hours:
        if row["supervisor_pid"] != supervisor_pid:
            _fail("SUBSCRIPTION_CHAIN",
                  "all 26 hours must share one capture supervisor")
        if row["subscription_invalidations"] != 0 or \
                row["subscription_proven_at_end"] is False:
            _fail("SUBSCRIPTION_CHAIN",
                  "subscription invalidation is forbidden in overlay hours")
        if previous is not None:
            if row["child_generation"] == previous["child_generation"]:
                if (row["child_pid"] != previous["child_pid"] or
                        row["child_subscription_ack_wall_ns"] !=
                        previous["child_subscription_ack_wall_ns"] or
                        row["child_subscription_ack_identity_sha256"] !=
                        previous["child_subscription_ack_identity_sha256"] or
                        row["child_subscription_ack_count"] !=
                        previous["child_subscription_ack_count"]):
                    _fail("SUBSCRIPTION_CHAIN",
                          "child PID/initial ACK changed within one generation")
            elif row["child_generation"] != previous["child_generation"] + 1:
                _fail("SUBSCRIPTION_CHAIN",
                      "child generations must be contiguous")
            elif (row["child_subscription_ack_identity_sha256"] ==
                  previous["child_subscription_ack_identity_sha256"] or
                  row["child_subscription_ack_wall_ns"] <=
                  previous["child_subscription_ack_wall_ns"]):
                _fail("SUBSCRIPTION_CHAIN",
                      "new child generation lacks a distinct later initial ACK")
        previous = row
    analysis = hours[:24]
    watermark = hours[24:]
    for row in hours:
        if row["source_seal"] != base["source_seal"]:
            _fail("BASE_SEAL_INVALID",
                  "all 26 hours must bind analysis D's full_v2 cross-day seal")
    analysis_objects = []
    watermark_objects = []
    for receipt, target in [
            *((row, analysis_objects) for row in analysis),
            *((row, watermark_objects) for row in watermark)]:
        target.extend({
            "key": obj["key"], "version_id": obj["version_id"],
            "size": obj["size"], "sha256": obj["sha256"],
        } for obj in receipt["rfq_objects"])
    analysis_objects.sort(key=lambda row: row["key"])
    watermark_objects.sort(key=lambda row: row["key"])
    if len({row["key"] for row in analysis_objects}) != len(analysis_objects) or \
            len({row["key"] for row in watermark_objects}) != len(watermark_objects):
        _fail("INVENTORY_DUPLICATE", "overlay RFQ object keys are duplicated")
    if analysis_objects != base["sealed_rfq_objects"]:
        _fail("ANALYSIS_SEAL_MISMATCH",
              "D analysis objects do not equal the full_v2 seal RFQ subset")
    if {row["key"] for row in analysis_objects} & \
            {row["key"] for row in watermark_objects}:
        _fail("WATERMARK_CONTAMINATION", "watermark objects overlap D analysis set")
    return analysis, watermark, analysis_objects, watermark_objects


SOURCE_EVIDENCE_FIELDS = {
    "schema", "state", "source_bucket", "analysis_date", "expected_hours",
    "authority_sha256", "generation", "seal", "seal_binding_sha256",
    "seal_rfq_capture_members", "seal_rfq_capture_member_set_sha256",
    "seal_rfq_receipt_container_members",
    "seal_rfq_receipt_container_member_set_sha256",
    "analysis_capture_objects", "analysis_capture_object_set_sha256",
    "watermark_capture_objects", "watermark_capture_object_set_sha256",
    "receipt_containers", "receipt_container_object_set_sha256",
    "container_completeness_state", "analysis_receipt_proofs",
    "analysis_receipt_proof_set_sha256", "watermark_receipt_proofs",
    "watermark_receipt_proof_set_sha256",
    "all_input_bodies_omitted_from_output", "evidence_sha256",
}
SOURCE_CAPTURE_ROW_FIELDS = {
    "bucket", "key", "version_id", "size", "sha256", "relpath",
    "source_hour", "ordinal", "role", "seal_member",
    "seal_membership_scope",
}
SOURCE_CONTAINER_ROW_FIELDS = {
    "bucket", "key", "version_id", "size", "sha256", "relpath",
    "source_hour", "ordinal", "seal_member",
}


def _digest_bound_list(
    evidence: dict[str, Any], rows_field: str, digest_field: str,
) -> list[Any]:
    rows = evidence.get(rows_field)
    if not isinstance(rows, list):
        _fail("SOURCE_EVIDENCE_SCHEMA", f"{rows_field} must be a list")
    supplied = _sha(evidence.get(digest_field), digest_field)
    if supplied != canonical_sha256(rows):
        _fail("SOURCE_EVIDENCE_DIGEST", f"{digest_field} mismatch")
    return rows


def _source_capture_projection(
    rows: list[Any], hours: list[str], role: str,
) -> list[dict[str, Any]]:
    expected_hours = set(hours)
    identities = []
    seen = set()
    for index, raw in enumerate(rows):
        row = _exact_keys(
            raw, SOURCE_CAPTURE_ROW_FIELDS,
            f"{role.lower()}_capture_objects[{index}]")
        identity = _normalize_inventory([{
            "key": row["key"], "version_id": row["version_id"],
            "size": row["size"], "sha256": row["sha256"],
        }], f"{role.lower()}_capture_identity")[0]
        if (row["bucket"] != SOURCE_BUCKET or row["role"] != role or
                row["source_hour"] not in expected_hours or
                row["seal_member"] is not True or
                not identity["key"].startswith(RAW_KEY_PREFIX + "/") or
                identity["key"] in seen):
            _fail("SOURCE_EVIDENCE_CAPTURE",
                  "physical capture row scope/role/identity differs")
        seen.add(identity["key"])
        identities.append(identity)
    identities.sort(key=lambda row: row["key"])
    return identities


def _normalize_source_evidence(
    value: Any, authority: dict[str, Any], base: dict[str, Any],
    analysis: list[dict[str, Any]], watermark: list[dict[str, Any]],
    analysis_objects: list[dict[str, Any]],
    watermark_objects: list[dict[str, Any]],
) -> dict[str, Any]:
    value = _exact_keys(value, SOURCE_EVIDENCE_FIELDS, "source evidence")
    expected_hours = [row["segment_hour"] for row in analysis + watermark]
    fixed = {
        "schema": SOURCE_EVIDENCE_SCHEMA,
        "state": "ALL_INPUT_EXACT_VERSION_BYTES_VERIFIED",
        "source_bucket": SOURCE_BUCKET,
        "analysis_date": base["date"],
        "expected_hours": expected_hours,
        "authority_sha256": authority["authority_sha256"],
        "generation": authority["generation"],
        "container_completeness_state":
            "CALLER_COMPLETE_IDENTITY_SET_EXACT_BYTES_VERIFIED",
        "all_input_bodies_omitted_from_output": True,
    }
    if any(value.get(key) != expected for key, expected in fixed.items()):
        _fail("SOURCE_EVIDENCE_BINDING",
              "source evidence fixed authority/window contract differs")
    supplied_evidence_sha = _sha(value["evidence_sha256"], "evidence_sha256")
    unsigned = copy.deepcopy(value)
    unsigned.pop("evidence_sha256")
    if supplied_evidence_sha != canonical_sha256(unsigned):
        _fail("SOURCE_EVIDENCE_DIGEST", "evidence_sha256 mismatch")

    seal = value.get("seal")
    seal_fields = {
        "bucket", "key", "version_id", "size", "sha256", "date", "version",
        "method", "status", "code_commit", "manifest_date_sha256",
    }
    seal = _exact_keys(seal, seal_fields, "source evidence seal")
    seal_identity = {
        "bucket": _text(seal["bucket"], "seal.bucket"),
        "key": _safe_key(seal["key"], "seal.key"),
        "version_id": _version(seal["version_id"], "seal.version_id"),
        "size": _size(seal["size"], "seal.size"),
        "sha256": _sha(seal["sha256"], "seal.sha256"),
    }
    base_seal_identity = {
        key: base["source_seal"][key]
        for key in ("bucket", "key", "version_id", "size", "sha256")
    }
    if (seal_identity != base_seal_identity or seal["date"] != base["date"] or
            seal["version"] != 2 or seal["method"] != "full_v2" or
            seal["status"] != "SEALED" or
            not isinstance(seal["code_commit"], str) or
            COMMIT_RE.fullmatch(seal["code_commit"]) is None or
            _sha(seal["manifest_date_sha256"], "manifest_date_sha256") !=
            seal["manifest_date_sha256"] or
            value["seal_binding_sha256"] != canonical_sha256(seal)):
        _fail("SOURCE_EVIDENCE_SEAL", "source evidence/base full_v2 seal differs")

    sealed_captures = _digest_bound_list(
        value, "seal_rfq_capture_members",
        "seal_rfq_capture_member_set_sha256")
    _digest_bound_list(
        value, "seal_rfq_receipt_container_members",
        "seal_rfq_receipt_container_member_set_sha256")
    analysis_capture_rows = _digest_bound_list(
        value, "analysis_capture_objects",
        "analysis_capture_object_set_sha256")
    watermark_capture_rows = _digest_bound_list(
        value, "watermark_capture_objects",
        "watermark_capture_object_set_sha256")
    containers = _digest_bound_list(
        value, "receipt_containers", "receipt_container_object_set_sha256")
    analysis_proofs = _digest_bound_list(
        value, "analysis_receipt_proofs",
        "analysis_receipt_proof_set_sha256")
    watermark_proofs = _digest_bound_list(
        value, "watermark_receipt_proofs",
        "watermark_receipt_proof_set_sha256")

    expected_analysis_proofs = [row["source_proof"] for row in analysis]
    expected_watermark_proofs = [row["source_proof"] for row in watermark]
    if (analysis_proofs != expected_analysis_proofs or
            watermark_proofs != expected_watermark_proofs or
            any(row["source_evidence_sha256"] != supplied_evidence_sha
                for row in analysis + watermark)):
        _fail("SOURCE_EVIDENCE_PROOFS",
              "26 hour receipt proofs/evidence digest are not exact")
    for index, proof in enumerate(analysis_proofs + watermark_proofs):
        expected_role = "ANALYSIS" if index < 24 else "WATERMARK"
        expected_seal_member = index < 25
        if (proof["role"] != expected_role or
                proof["container_seal_member"] is not expected_seal_member):
            _fail("SOURCE_EVIDENCE_PROOFS",
                  "proof role/container seal scope differs from 24+2 window")

    analysis_projection = _source_capture_projection(
        analysis_capture_rows, expected_hours[:24], "ANALYSIS")
    watermark_projection = _source_capture_projection(
        watermark_capture_rows, expected_hours[24:], "WATERMARK")
    if (analysis_projection != _physical_projection(analysis_objects) or
            watermark_projection != _physical_projection(watermark_objects)):
        _fail("SOURCE_EVIDENCE_CAPTURE",
              "physical capture exact versions differ from hour RFQ objects")

    sealed_projection = []
    for index, row in enumerate(sealed_captures):
        if not isinstance(row, dict):
            _fail("SOURCE_EVIDENCE_SEAL", "sealed capture member is not an object")
        try:
            sealed_projection.append({
                "key": f"{RAW_KEY_PREFIX}/{_safe_rel(row['file'], f'seal member {index}')}",
                "size": _size(row["size"], f"seal member {index}.size"),
                "sha256": _sha(row["sha256"], f"seal member {index}.sha256"),
            })
        except KeyError as exc:
            _fail("SOURCE_EVIDENCE_SEAL", f"sealed capture member missing {exc}")
    physical_without_versions = [{
        "key": row["key"], "size": row["size"], "sha256": row["sha256"],
    } for row in analysis_projection + watermark_projection]
    if sorted(sealed_projection, key=lambda row: row["key"]) != \
            sorted(physical_without_versions, key=lambda row: row["key"]):
        _fail("SOURCE_EVIDENCE_SEAL",
              "physical 26-hour capture set differs from D full_v2 seal")

    container_projection = []
    for index, raw in enumerate(containers):
        row = _exact_keys(
            raw, SOURCE_CONTAINER_ROW_FIELDS, f"receipt_containers[{index}]")
        if row["bucket"] != SOURCE_BUCKET or type(row["seal_member"]) is not bool:
            _fail("SOURCE_EVIDENCE_CONTAINER", "container scope is invalid")
        container_projection.append({
            "bucket": row["bucket"], "key": _safe_key(row["key"], "container key"),
            "version_id": _version(row["version_id"], "container version"),
            "size": _size(row["size"], "container size"),
            "sha256": _sha(row["sha256"], "container sha256"),
            "seal_member": row["seal_member"],
        })
    proof_container_projection = [{
        "bucket": row["container_bucket"], "key": row["container_key"],
        "version_id": row["container_version_id"],
        "size": row["container_size"], "sha256": row["container_sha256"],
        "seal_member": row["container_seal_member"],
    } for row in analysis_proofs + watermark_proofs]
    if container_projection != proof_container_projection:
        _fail("SOURCE_EVIDENCE_CONTAINER",
              "complete receipt containers differ from 26 line proofs")
    return copy.deepcopy(value)


def build_overlay_manifest(
    *, authority: Any, base_release: Any,
    hour_receipts: list[dict[str, Any]],
    source_evidence: Any,
) -> dict[str, Any]:
    authority = validate_fresh_epoch_authority(authority)
    base = _normalize_base_release(base_release, authority)
    analysis, watermark, analysis_objects, watermark_objects = \
        _overlay_components(authority, base, hour_receipts)
    source_evidence = _normalize_source_evidence(
        source_evidence, authority, base, analysis, watermark,
        analysis_objects, watermark_objects)
    result = {
        "schema": OVERLAY_SCHEMA,
        "lane_id": LANE_ID,
        "state": "LOCALLY_VERIFIED_UNPUBLISHED",
        "authority_sha256": authority["authority_sha256"],
        "eligible_date": base["date"],
        "base_release": base,
        "base_release_binding_sha256": canonical_sha256(base),
        "source_evidence": source_evidence,
        "source_evidence_sha256": source_evidence["evidence_sha256"],
        "analysis_hours": analysis,
        "analysis_hour_receipt_set_sha256": canonical_sha256(
            [row["receipt_sha256"] for row in analysis]),
        "watermark_hours": watermark,
        "watermark_hour_receipt_set_sha256": canonical_sha256(
            [row["receipt_sha256"] for row in watermark]),
        "analysis_rfq_objects": analysis_objects,
        "analysis_rfq_object_set_sha256": canonical_sha256(analysis_objects),
        "watermark_rfq_objects": watermark_objects,
        "watermark_rfq_object_set_sha256": canonical_sha256(watermark_objects),
        "analysis_hour_count": 24,
        "watermark_hour_count": 2,
        "watermark_objects_in_analysis": False,
        "data_objects_copied": 0,
        "aws_write_authorized": False,
        "research_eligible": False,
    }
    result["manifest_sha256"] = canonical_sha256(result)
    return validate_overlay_manifest(result, authority)


def validate_overlay_manifest(value: Any, authority: Any) -> dict[str, Any]:
    authority = validate_fresh_epoch_authority(authority)
    fields = {
        "schema", "lane_id", "state", "authority_sha256", "eligible_date",
        "base_release", "base_release_binding_sha256", "analysis_hours",
        "source_evidence", "source_evidence_sha256",
        "analysis_hour_receipt_set_sha256", "watermark_hours",
        "watermark_hour_receipt_set_sha256", "analysis_rfq_objects",
        "analysis_rfq_object_set_sha256", "watermark_rfq_objects",
        "watermark_rfq_object_set_sha256", "analysis_hour_count",
        "watermark_hour_count", "watermark_objects_in_analysis",
        "data_objects_copied", "aws_write_authorized", "research_eligible",
        "manifest_sha256",
    }
    value = _exact_keys(value, fields, "RFQ overlay manifest")
    fixed = {
        "schema": OVERLAY_SCHEMA, "lane_id": LANE_ID,
        "state": "LOCALLY_VERIFIED_UNPUBLISHED",
        "authority_sha256": authority["authority_sha256"],
        "analysis_hour_count": 24, "watermark_hour_count": 2,
        "watermark_objects_in_analysis": False, "data_objects_copied": 0,
        "aws_write_authorized": False, "research_eligible": False,
    }
    if any(value.get(key) != expected for key, expected in fixed.items()):
        _fail("OVERLAY_INVALID", "fixed local-only overlay contract changed")
    base = _normalize_base_release(value["base_release"], authority)
    if value["base_release"] != base or \
            value["eligible_date"] != base["date"] or \
            value["base_release_binding_sha256"] != canonical_sha256(base):
        _fail("BASE_RELEASE_INVALID", "overlay/base binding mismatch")
    combined = value["analysis_hours"] + value["watermark_hours"] \
        if isinstance(value["analysis_hours"], list) and \
        isinstance(value["watermark_hours"], list) else None
    analysis, watermark, analysis_objects, watermark_objects = \
        _overlay_components(authority, base, combined)
    source_evidence = _normalize_source_evidence(
        value["source_evidence"], authority, base, analysis, watermark,
        analysis_objects, watermark_objects)
    if value["source_evidence_sha256"] != source_evidence["evidence_sha256"]:
        _fail("SOURCE_EVIDENCE_DIGEST", "overlay/source evidence digest mismatch")
    bindings = {
        "analysis_hours": analysis,
        "watermark_hours": watermark,
        "analysis_hour_receipt_set_sha256": canonical_sha256(
            [row["receipt_sha256"] for row in analysis]),
        "watermark_hour_receipt_set_sha256": canonical_sha256(
            [row["receipt_sha256"] for row in watermark]),
        "analysis_rfq_objects": analysis_objects,
        "analysis_rfq_object_set_sha256": canonical_sha256(analysis_objects),
        "watermark_rfq_objects": watermark_objects,
        "watermark_rfq_object_set_sha256": canonical_sha256(watermark_objects),
        "source_evidence": source_evidence,
        "source_evidence_sha256": source_evidence["evidence_sha256"],
    }
    if any(value.get(key) != expected for key, expected in bindings.items()):
        _fail("OVERLAY_BINDING", "hour/object set binding mismatch")
    supplied = _sha(value["manifest_sha256"], "manifest_sha256")
    unsigned = copy.deepcopy(value)
    unsigned.pop("manifest_sha256")
    if supplied != canonical_sha256(unsigned):
        _fail("OVERLAY_DIGEST", "manifest_sha256 mismatch")
    return copy.deepcopy(value)
