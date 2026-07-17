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

import fresh_rfq_base_binding as rfq_base_binding
import fresh_rfq_market_mapping as rfq_market_mapping


AUTHORITY_SCHEMA = "fresh-rfq-epoch-authority-v1"
HOUR_SCHEMA = "canonical-rfq-hour-receipt-v2"
OVERLAY_SCHEMA = "research-rfq-overlay-manifest-v3"
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
MARKET_MAPPING_INPUT_FIELDS = {
    "rfq_requests", "l1_market_universe", "l2_market_universe",
    "pre_event_window_ms", "post_event_window_ms",
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
            child_ack_wall >= expected_end or
            type(child_ack_count) is not int or child_ack_count != 1 or
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
    start_lag_ms = receipt.get("start_lag_ms")
    end_early_ms = receipt.get("end_early_ms")
    if (type(child_started) is not int or child_started <= 0 or
            child_ack_wall < child_started or
            type(segment_started) is not int or
            not expected_start <= segment_started <= expected_start + 5_000_000_000 or
            child_started > segment_started or
            type(observed) is not int or observed < expected_end or
            type(start_lag_ms) is not int or start_lag_ms !=
            max(0, (segment_started - expected_start) // 1_000_000) or
            type(end_early_ms) is not int or end_early_ms != 0):
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
            not isinstance(markers, dict) or set(markers) != {"hour_open"} or
            type(markers.get("hour_open")) is not int or
            markers["hour_open"] != 1 or
            type(raw.get("rfq_created")) is not int or raw["rfq_created"] < 0 or
            type(raw.get("rfq_deleted")) is not int or raw["rfq_deleted"] < 0 or
            type(raw.get("partition_mismatches")) is not int or
            raw["partition_mismatches"] != 0 or
            type(raw.get("max_stream_epoch")) is not int or
            raw["max_stream_epoch"] < 0 or
            type(raw.get("subscription_invalidations")) is not int or
            raw["subscription_invalidations"] != 0 or
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
    if (type(feed_rows) is not int or not 3500 <= feed_rows <= 3700 or
            type(connected) is not int or connected != feed_rows or
            type(metrics.get("first_ts_ms")) is not int or
            not lo_ms <= metrics["first_ts_ms"] <= lo_ms + 5_000 or
            type(metrics.get("last_ts_ms")) is not int or
            not hi_ms - 5_000 <= metrics["last_ts_ms"] < hi_ms or
            any(type(metrics.get(field)) is not int or metrics[field] != 0
                for field in (
                "min_reconnects", "min_disconnects", "min_errors",
                "max_reconnects", "max_disconnects", "max_errors",
                "max_recorder_dropped", "max_recorder_write_failures")) or
            type(metrics.get("end_offset")) is not int or
            type(metrics.get("next_window_offset")) is not int or
            not 0 < metrics["next_window_offset"] <= metrics["end_offset"]):
        _fail("METRICS_COVERAGE", "metrics do not prove full connected coverage")
    shards = receipt.get("capture_shards")
    if not isinstance(shards, list) or not shards:
        _fail("SHARD_SET_INVALID", "capture_shards must be non-empty")
    normalized_shards = []
    date_text, hour_number = hour_text.split("T")
    base_rel = f"date={date_text}/rfq_{hour_number}.ndjson"
    origin = receipt.get("capture_origin_path")
    if (not isinstance(origin, str) or not origin.startswith("/") or
            "\\" in origin or "\x00" in origin or
            any(part in ("", ".", "..") for part in origin.split("/")[1:]) or
            not origin.endswith("/" + base_rel)):
        _fail("RAW_SHARD_BINDING",
              "capture_origin_path is not the exact absolute base shard path")
    origin_prefix = origin[:-len(base_rel)]
    shard_fields = {"ordinal", "relpath", "bytes_before", "size",
                    "parsed_bytes_at_close", "sha256"}
    for index, row in enumerate(shards):
        row = _exact_keys(row, shard_fields, f"capture_shards[{index}]")
        if type(row["ordinal"]) is not int or row["ordinal"] != index:
            _fail("SHARD_GAP", "shard ordinals must be contiguous from zero")
        expected_rel = base_rel if index == 0 else f"{base_rel}.{index}"
        relpath = _safe_rel(row["relpath"], f"capture_shards[{index}].relpath")
        if relpath != expected_rel:
            _fail("SHARD_PATH", "shard path does not match its hour/ordinal")
        if type(row["bytes_before"]) is not int or row["bytes_before"] != 0:
            _fail("PREEXISTING_BYTES", "fresh shard bytes_before must be zero")
        size = _size(row["size"], f"capture_shards[{index}].size")
        if (type(row["parsed_bytes_at_close"]) is not int or
                row["parsed_bytes_at_close"] != size):
            _fail("TORN_SHARD", "parsed_bytes_at_close must equal attested size")
        normalized_shards.append({
            "ordinal": index,
            "relpath": relpath,
            "bytes_before": 0,
            "size": size,
            "parsed_bytes_at_close": size,
            "sha256": _sha(row["sha256"], f"capture_shards[{index}].sha256"),
        })
    expected_raw_paths = [origin_prefix + row["relpath"]
                          for row in normalized_shards]
    expected_start_offsets = {
        path: row["bytes_before"]
        for path, row in zip(expected_raw_paths, normalized_shards)
    }
    expected_end_offsets = {
        path: row["parsed_bytes_at_close"]
        for path, row in zip(expected_raw_paths, normalized_shards)
    }
    if (raw.get("shards") != expected_raw_paths or
            raw.get("start_offsets") != expected_start_offsets or
            raw.get("end_offsets") != expected_end_offsets):
        _fail("RAW_SHARD_BINDING",
              "raw paths/start/end offsets differ from attested capture shards")
    total = sum(row["size"] for row in normalized_shards)
    if (receipt.get("capture_relpath") != base_rel or
            type(receipt.get("capture_bytes_before")) is not int or
            receipt["capture_bytes_before"] != 0 or
            type(receipt.get("capture_bytes_at_close")) is not int or
            receipt["capture_bytes_at_close"] != total or
            type(receipt.get("capture_shard_count")) is not int or
            receipt["capture_shard_count"] != len(normalized_shards) or
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
    # Source-evidence emits capture identities in natural shard order.  Key
    # lexicographic order diverges at .10 versus .2 and must never define this
    # digest.
    ordered = sorted(objects, key=lambda row: (row["ordinal"], row["relpath"]))
    rows = [{
        "bucket": bucket, "key": row["key"],
        "version_id": row["version_id"], "size": row["size"],
        "sha256": row["sha256"],
    } for row in ordered]
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
    segment_start = _hour(segment_hour)
    if value["role"] == "WATERMARK" and segment_start.hour not in (0, 1):
        _fail("SOURCE_PROOF_BINDING",
              "watermark proof must be D+1 hour 00 or 01")
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
    expected_close = (segment_start + dt.timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H")
    if matched is None or f"{matched.group(1)}T{matched.group(2)}" != expected_close:
        _fail("SOURCE_PROOF_CONTAINER",
              "receipt container key does not match the segment close hour")
    container_size = _size(value["container_size"], "container_size")
    line_number = _size(value["line_number"], "line_number")
    byte_offset = _size(value["byte_offset"], "byte_offset", positive=False)
    byte_length = _size(value["byte_length"], "byte_length")
    container_sha = _sha(value["container_sha256"], "container_sha256")
    outer_row_sha = _sha(value["outer_row_sha256"], "outer_row_sha256")
    if (line_number != 1 or byte_offset != 0 or
            byte_length != container_size or outer_row_sha != container_sha):
        _fail("SOURCE_PROOF_CONTAINER",
              "receipt proof must bind the complete single-row container")
    if type(value["container_seal_member"]) is not bool:
        _fail("SOURCE_PROOF_CONTAINER", "container_seal_member must be boolean")
    expected_seal_member = value["role"] == "ANALYSIS" or \
        segment_start.hour == 0
    if value["container_seal_member"] is not expected_seal_member:
        _fail("SOURCE_PROOF_CONTAINER",
              "container seal membership differs from the 24+2 contract")
    normalized = copy.deepcopy(value)
    normalized.update({
        "container_key": container_key,
        "container_version_id": _version(
            value["container_version_id"], "container_version_id"),
        "container_size": container_size,
        "container_sha256": container_sha,
        "line_number": line_number, "byte_offset": byte_offset,
        "byte_length": byte_length,
        "outer_row_sha256": outer_row_sha,
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
    source_evidence: Any,
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
    source_evidence, source_proof, resolved_container, resolved_seal = \
        _normalize_hour_source_evidence(
            source_evidence, authority, segment["segment_hour"],
            raw_segment_sha, expected)
    source_evidence_sha256 = source_evidence["evidence_sha256"]
    analysis_date = _source_analysis_date(
        segment["segment_hour"], source_proof["role"])
    seal = {**resolved_seal, "verification_state": "PASS"}
    if (seal["bucket"] != authority["source_bucket"] or
            seal["key"] != f"ec2/warehouse/seals/date={analysis_date}.json"):
        _fail("SOURCE_SEAL_INVALID",
              "hour seal must be analysis D's full_v2 cross-day seal")
    if resolver is None:
        _fail("SOURCE_RESOLUTION_REQUIRED",
              "hour receipt requires exact-VersionId source resolution")
    resolver_rows = inventory + [
        {key: resolved_container[key]
         for key in ("key", "version_id", "size", "sha256")},
        {key: resolved_seal[key]
         for key in ("key", "version_id", "size", "sha256")},
    ]
    for inventory_row in resolver_rows:
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
        "segment_receipt": segment,
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
    return validate_hour_receipt(
        result, authority, source_evidence=source_evidence)


def validate_hour_receipt(
    value: Any, authority: Any, *, source_evidence: Any,
) -> dict[str, Any]:
    authority = validate_fresh_epoch_authority(authority)
    fields = {
        "schema", "lane_id", "state", "segment_hour", "authority_sha256",
        "analysis_date",
        "strict_t0_utc", "source_bucket", "source_seal",
        "source_seal_binding_sha256",
        "segment_receipt", "capture_receipt_sha256",
        "capture_shard_set_sha256", "raw_key_prefix",
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
    }
    if (any(value.get(key) != expected for key, expected in fixed.items()) or
            value.get("complete") is not True or
            value.get("research_eligible") is not False):
        _fail("HOUR_RECEIPT_INVALID", "fixed hour receipt contract changed")
    if _hour(value["segment_hour"]) < _t0(authority["strict_t0_utc"]):
        _fail("BEFORE_STRICT_T0", "hour receipt predates the fresh epoch")
    segment = validate_v3_segment_receipt(value["segment_receipt"], authority)
    segment_sha = canonical_sha256(segment)
    if (segment["segment_hour"] != value["segment_hour"] or
            value["capture_receipt_sha256"] != segment_sha or
            value["capture_shard_set_sha256"] !=
            segment["capture_shard_set_sha256"]):
        _fail("HOUR_SEGMENT_BINDING",
              "hour receipt does not bind its canonical v3 segment")
    prefix = _safe_key(value["raw_key_prefix"], "raw_key_prefix")
    if prefix != authority["raw_key_prefix"]:
        _fail("SOURCE_SCOPE", "raw_key_prefix differs from epoch authority")
    seal = _normalize_source_seal(value["source_seal"])
    if value["source_seal_binding_sha256"] != \
            (None if seal is None else seal["sha256"]):
        _fail("SOURCE_SEAL_INVALID", "source seal SHA binding mismatch")
    _sha(value["capture_receipt_sha256"], "capture_receipt_sha256")
    _sha(value["capture_shard_set_sha256"], "capture_shard_set_sha256")
    if value["resolution_state"] != "RESOLVED_EXACT":
        _fail("SOURCE_RESOLUTION_REQUIRED",
              "hour receipt source objects are not independently resolved")
    hour_start_ns = int(_hour(value["segment_hour"]).timestamp()) * 1_000_000_000
    hour_end_ns = hour_start_ns + 3_600_000_000_000
    for field in ("supervisor_pid", "child_pid", "child_generation"):
        if type(value.get(field)) is not int or value[field] <= 0:
            _fail("HOUR_RECEIPT_INVALID", f"{field} must be positive")
        if value[field] != segment[field]:
            _fail("HOUR_SEGMENT_BINDING", f"{field} differs from v3 segment")
    child_ack_wall = value.get("child_subscription_ack_wall_ns")
    child_ack_identity = value.get("child_subscription_ack_identity_sha256")
    if (type(child_ack_wall) is not int or child_ack_wall <= 0 or
            child_ack_wall >= hour_end_ns or
            type(value.get("child_subscription_ack_count")) is not int or
            value["child_subscription_ack_count"] != 1 or
            not isinstance(child_ack_identity, str) or
            SHA_RE.fullmatch(child_ack_identity) is None):
        _fail("HOUR_RECEIPT_INVALID",
              "persistent child subscription ACK binding is invalid")
    if (child_ack_wall != segment["child_subscription_ack_wall_ns"] or
            child_ack_identity !=
            segment["child_subscription_ack_identity_sha256"] or
            value["child_subscription_ack_count"] !=
            segment["child_subscription_ack_count"]):
        _fail("HOUR_SEGMENT_BINDING",
              "persistent child ACK projection differs from v3 segment")
    ack_count = value.get("subscription_ack_count")
    ack_wall = value.get("subscription_ack_wall_ns")
    subscription_end = value.get("subscription_proven_at_end")
    if (type(ack_count) is not int or ack_count < 0 or
            type(value.get("subscription_invalidations")) is not int or
            value["subscription_invalidations"] != 0 or
            not (subscription_end is True or subscription_end is None) or
            ((ack_count > 0) != (type(ack_wall) is int)) or
            (ack_count > 0 and not hour_start_ns <= ack_wall < hour_end_ns) or
            (ack_count > 0 and ack_wall != child_ack_wall) or
            (ack_count > 0 and subscription_end is not True)):
        _fail("HOUR_RECEIPT_INVALID", "subscription generation proof is invalid")
    raw = segment["raw_evidence"]
    if (ack_count != raw["subscribed_communications"] or
            ack_wall != raw["subscription_ack_wall_ns"] or
            value["subscription_invalidations"] !=
            raw["subscription_invalidations"] or
            subscription_end is not raw["subscription_proven_at_end"]):
        _fail("HOUR_SEGMENT_BINDING",
              "subscription projection differs from canonical v3 segment")
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
        if (type(row["ordinal"]) is not int or row["ordinal"] != index or
                relpath != expected_rel or
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
    expected_shards = [{
        "ordinal": row["ordinal"], "relpath": row["relpath"],
        "bytes_before": 0, "size": row["size"],
        "parsed_bytes_at_close": row["size"], "sha256": row["sha256"],
    } for row in normalized]
    if value["capture_shard_set_sha256"] != canonical_sha256(expected_shards):
        _fail("OBJECT_SET_DIGEST",
              "capture_shard_set_sha256 differs from physical RFQ objects")
    if expected_shards != segment["capture_shards"]:
        _fail("HOUR_SEGMENT_BINDING",
              "physical RFQ objects differ from canonical segment shards")
    capture_receipt_sha = _sha(
        value["capture_receipt_sha256"], "capture_receipt_sha256")
    source_evidence, source_proof, _resolved_container, evidence_seal = \
        _normalize_hour_source_evidence(
            source_evidence, authority, value["segment_hour"],
            capture_receipt_sha, normalized)
    if not _canonical_exact_equal(value["source_proof"], source_proof):
        _fail("SOURCE_PROOF_BINDING",
              "hour receipt proof is not the selected source-evidence proof")
    analysis_date = _source_analysis_date(
        value["segment_hour"], source_proof["role"])
    expected_seal = {**evidence_seal, "verification_state": "PASS"}
    if (value["analysis_date"] != analysis_date or seal != expected_seal or
            seal["bucket"] != authority["source_bucket"] or
            seal["key"] != f"ec2/warehouse/seals/date={analysis_date}.json"):
        _fail("SOURCE_SEAL_INVALID",
              "hour/source proof does not bind analysis D's full_v2 seal")
    if (value["source_proof_sha256"] != canonical_sha256(source_proof) or
            _sha(value["source_evidence_sha256"], "source_evidence_sha256") !=
            source_evidence["evidence_sha256"]):
        _fail("SOURCE_PROOF_DIGEST", "source proof/evidence digest binding mismatch")
    unsigned = copy.deepcopy(value)
    unsigned["rfq_objects"] = normalized
    supplied = _sha(unsigned.pop("receipt_sha256"), "receipt_sha256")
    if supplied != canonical_sha256(unsigned):
        _fail("HOUR_RECEIPT_DIGEST", "receipt_sha256 mismatch")
    normalized_value = copy.deepcopy(value)
    normalized_value["source_seal"] = seal
    normalized_value["segment_receipt"] = segment
    normalized_value["rfq_objects"] = normalized
    normalized_value["source_proof"] = source_proof
    return normalized_value


def _build_base_binding(
    *, manifest_bytes: Any, manifest_exact_identity: Any, date: str,
) -> dict[str, Any]:
    try:
        return rfq_base_binding.build_base_binding(
            manifest_bytes=manifest_bytes,
            manifest_exact_identity=manifest_exact_identity,
            date=date,
        )
    except rfq_base_binding.FreshRfqBaseBindingError as exc:
        _fail("BASE_BINDING_INVALID", str(exc))


def _exact_identity_projection(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in ("bucket", "key", "version_id", "size", "sha256")
    }


def _canonical_exact_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/int equality coercion."""
    return canonical_bytes(left) == canonical_bytes(right)


def _market_mapping_inputs(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != MARKET_MAPPING_INPUT_FIELDS:
        _fail(
            "MARKET_MAPPING_INPUTS",
            "market_mapping_inputs fields differ from the exact contract",
        )
    return value


def _build_market_mapping(
    *, base: dict[str, Any], analysis_objects: list[dict[str, Any]],
    time_contract: dict[str, Any], market_mapping_inputs: dict[str, Any],
) -> dict[str, Any]:
    try:
        return rfq_market_mapping.build_market_mapping(
            analysis_date=base["date"],
            base_binding_sha256=base["binding_sha256"],
            analysis_rfq_object_set_sha256=canonical_sha256(analysis_objects),
            time_contract_sha256=canonical_sha256(time_contract),
            **market_mapping_inputs,
        )
    except rfq_market_mapping.FreshRfqMarketMappingError as exc:
        _fail("MARKET_MAPPING_INVALID", f"{exc.code}: {exc.detail}")


def _validate_market_mapping(
    value: Any, *, base: dict[str, Any],
    analysis_objects: list[dict[str, Any]],
    time_contract: dict[str, Any], market_mapping_inputs: dict[str, Any],
) -> dict[str, Any]:
    try:
        return rfq_market_mapping.validate_market_mapping(
            value,
            analysis_date=base["date"],
            base_binding_sha256=base["binding_sha256"],
            analysis_rfq_object_set_sha256=canonical_sha256(analysis_objects),
            time_contract_sha256=canonical_sha256(time_contract),
            **market_mapping_inputs,
        )
    except rfq_market_mapping.FreshRfqMarketMappingError as exc:
        _fail("MARKET_MAPPING_INVALID", f"{exc.code}: {exc.detail}")


def _derive_time_contract(base: dict[str, Any]) -> dict[str, Any]:
    """Derive the immutable overlay clocks from one exact base binding.

    ``as_of_cutoff_utc`` is the reference manifest's publication time.  It is
    deliberately renamed in this overlay contract so consumers cannot mistake
    control-plane publication for a market-data event cutoff.  The two D+1
    hours are completeness watermarks only and never extend the D base-data
    window.
    """
    analysis_date = _date(base.get("date"), "base binding.date")
    analysis_start = dt.datetime.combine(
        analysis_date, dt.time(), tzinfo=dt.timezone.utc)
    analysis_end = analysis_start + dt.timedelta(days=1)
    watermark_end = analysis_end + dt.timedelta(hours=2)

    analysis_start_text = analysis_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    analysis_end_text = analysis_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    watermark_end_text = watermark_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    if base.get("analysis_data_end_utc") != analysis_end_text:
        _fail(
            "TIME_CONTRACT_INVALID",
            "base analysis-data end differs from D+1 00:00 UTC",
        )

    published_text = base.get("as_of_cutoff_utc")
    if not isinstance(published_text, str) or not published_text.endswith("Z"):
        _fail(
            "TIME_CONTRACT_INVALID",
            "base manifest publication time is not canonical UTC",
        )
    try:
        published = dt.datetime.fromisoformat(published_text[:-1] + "+00:00")
    except ValueError as exc:
        _fail("TIME_CONTRACT_INVALID", f"invalid base publication time: {exc}")
    if published < watermark_end:
        _fail(
            "TIME_CONTRACT_INVALID",
            "base manifest publication predates the complete 24+2 window",
        )

    return {
        "analysis_start_utc": analysis_start_text,
        "analysis_end_utc_exclusive": analysis_end_text,
        "watermark_end_utc_exclusive": watermark_end_text,
        "base_manifest_published_at_utc": published_text,
        "out_of_base_window_policy": "DQ_NO_CROSS_DATE_BORROW",
        "watermark_is_market_data": False,
    }


def _overlay_components(
    authority: dict[str, Any], base: dict[str, Any], hour_receipts: Any,
    source_evidence: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
           list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(hour_receipts, list):
        _fail("HOUR_CONTINUITY", "hour_receipts must be a list")
    hours = [validate_hour_receipt(
        row, authority, source_evidence=source_evidence)
        for row in hour_receipts]
    hours.sort(key=lambda row: row["segment_hour"])
    if len({row["segment_hour"] for row in hours}) != len(hours):
        _fail("HOUR_DUPLICATE", "duplicate canonical hour receipt")
    start = dt.datetime.combine(
        _date(base["date"], "base binding.date"), dt.time(),
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
    if (hours[0]["subscription_ack_count"] <= 0 or
            hours[0]["subscription_proven_at_end"] is not True):
        _fail("SUBSCRIPTION_CHAIN",
              "the first overlay hour must carry the initial subscription ACK")
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
            else:
                if (row["child_subscription_ack_identity_sha256"] ==
                        previous["child_subscription_ack_identity_sha256"] or
                        row["child_subscription_ack_wall_ns"] <=
                        previous["child_subscription_ack_wall_ns"]):
                    _fail(
                        "SUBSCRIPTION_CHAIN",
                        "new child generation lacks a distinct later initial ACK")
                if (row["subscription_ack_count"] != 1 or
                        row["subscription_ack_wall_ns"] !=
                        row["child_subscription_ack_wall_ns"] or
                        row["subscription_proven_at_end"] is not True):
                    _fail(
                        "SUBSCRIPTION_CHAIN",
                        "new child generation ACK is absent from its raw hour")
        previous = row
    analysis = hours[:24]
    watermark = hours[24:]
    base_seal_identity = _exact_identity_projection(base["source_seal"])
    for row in hours:
        if not _canonical_exact_equal(
                _exact_identity_projection(row["source_seal"]),
                base_seal_identity):
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


def _normalize_hour_source_evidence(
    value: Any, authority: dict[str, Any], segment_hour: str,
    raw_segment_sha256: str, physical_objects: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate one complete source-evidence body and select one hour proof.

    This is deliberately independent of base/overlay validation.  It prevents
    an hour receipt from pairing a free-floating proof with an arbitrary hex
    digest while the later overlay gate still performs the 26-hour/base cross
    checks.
    """
    value = _exact_keys(value, SOURCE_EVIDENCE_FIELDS, "source evidence")
    fixed = {
        "schema": SOURCE_EVIDENCE_SCHEMA,
        "state": "ALL_INPUT_EXACT_VERSION_BYTES_VERIFIED",
        "source_bucket": authority["source_bucket"],
        "authority_sha256": authority["authority_sha256"],
        "generation": authority["generation"],
        "container_completeness_state":
            "CALLER_COMPLETE_IDENTITY_SET_EXACT_BYTES_VERIFIED",
    }
    if (any(value.get(key) != expected for key, expected in fixed.items()) or
            value.get("all_input_bodies_omitted_from_output") is not True):
        _fail("SOURCE_EVIDENCE_BINDING",
              "source evidence fixed authority/state contract differs")
    supplied_evidence_sha = _sha(
        value["evidence_sha256"], "source evidence.evidence_sha256")
    unsigned = copy.deepcopy(value)
    unsigned.pop("evidence_sha256")
    if supplied_evidence_sha != canonical_sha256(unsigned):
        _fail("SOURCE_EVIDENCE_DIGEST", "evidence_sha256 mismatch")

    hours_value = value.get("expected_hours")
    if not isinstance(hours_value, list) or len(hours_value) != 26:
        _fail("SOURCE_EVIDENCE_WINDOW", "exactly 26 source hours are required")
    parsed_hours = [_hour(row, f"expected_hours[{index}]")
                    for index, row in enumerate(hours_value)]
    start = parsed_hours[0]
    expected_parsed = [start + dt.timedelta(hours=index) for index in range(26)]
    expected_hours = [row.strftime("%Y-%m-%dT%H") for row in expected_parsed]
    if (start.hour != 0 or parsed_hours != expected_parsed or
            value.get("analysis_date") != start.strftime("%Y-%m-%d")):
        _fail("SOURCE_EVIDENCE_WINDOW",
              "expected_hours must be D 00..23 plus D+1 00/01")
    if segment_hour not in expected_hours:
        _fail("SOURCE_EVIDENCE_WINDOW",
              "segment hour is absent from the complete evidence window")

    seal_fields = {
        "bucket", "key", "version_id", "size", "sha256", "date", "version",
        "method", "status", "code_commit", "manifest_date_sha256",
    }
    seal = _exact_keys(value.get("seal"), seal_fields, "source evidence seal")
    seal_identity = {
        "bucket": _text(seal["bucket"], "source evidence seal.bucket"),
        "key": _safe_key(seal["key"], "source evidence seal.key"),
        "version_id": _version(
            seal["version_id"], "source evidence seal.version_id"),
        "size": _size(seal["size"], "source evidence seal.size"),
        "sha256": _sha(seal["sha256"], "source evidence seal.sha256"),
    }
    expected_seal_key = (
        f"ec2/warehouse/seals/date={value['analysis_date']}.json")
    if (seal_identity["bucket"] != authority["source_bucket"] or
            seal_identity["key"] != expected_seal_key or
            seal.get("date") != value["analysis_date"] or
            type(seal.get("version")) is not int or seal["version"] != 2 or
            seal.get("method") != "full_v2" or seal.get("status") != "SEALED" or
            not isinstance(seal.get("code_commit"), str) or
            COMMIT_RE.fullmatch(seal["code_commit"]) is None or
            _sha(seal.get("manifest_date_sha256"),
                 "source evidence seal.manifest_date_sha256") !=
            seal["manifest_date_sha256"] or
            value.get("seal_binding_sha256") != canonical_sha256(seal)):
        _fail("SOURCE_EVIDENCE_SEAL",
              "source evidence does not bind one exact full_v2 seal")

    sealed_capture_rows = _digest_bound_list(
        value, "seal_rfq_capture_members",
        "seal_rfq_capture_member_set_sha256")
    sealed_container_rows = _digest_bound_list(
        value, "seal_rfq_receipt_container_members",
        "seal_rfq_receipt_container_member_set_sha256")
    analysis_capture_rows = _digest_bound_list(
        value, "analysis_capture_objects",
        "analysis_capture_object_set_sha256")
    watermark_capture_rows = _digest_bound_list(
        value, "watermark_capture_objects",
        "watermark_capture_object_set_sha256")
    container_rows = _digest_bound_list(
        value, "receipt_containers", "receipt_container_object_set_sha256")
    analysis_proofs = _digest_bound_list(
        value, "analysis_receipt_proofs",
        "analysis_receipt_proof_set_sha256")
    watermark_proofs = _digest_bound_list(
        value, "watermark_receipt_proofs",
        "watermark_receipt_proof_set_sha256")

    def normalize_captures(
        rows: list[Any], role: str, role_hours: list[str],
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        normalized: list[dict[str, Any]] = []
        by_hour = {hour: [] for hour in role_hours}
        expected_scope = "D_ANALYSIS_MEMBER" if role == "ANALYSIS" else \
            "D_FULL_V2_CROSS_DAY_MEMBER"
        denied = {(row["size"], row["sha256"])
                  for row in authority["deny_identities"]}
        for index, raw in enumerate(rows):
            row = _exact_keys(
                raw, SOURCE_CAPTURE_ROW_FIELDS,
                f"{role.lower()}_capture_objects[{index}]")
            hour = row.get("source_hour")
            ordinal = row.get("ordinal")
            if hour not in by_hour or type(ordinal) is not int or ordinal < 0:
                _fail("SOURCE_EVIDENCE_CAPTURE",
                      "capture hour/ordinal is outside the role window")
            date_text, hour_text = hour.split("T")
            expected_rel = f"date={date_text}/rfq_{hour_text}.ndjson"
            if ordinal:
                expected_rel += f".{ordinal}"
            identity = _normalize_inventory([{
                "key": row["key"], "version_id": row["version_id"],
                "size": row["size"], "sha256": row["sha256"],
            }], f"{role.lower()}_capture_identity")[0]
            if (row.get("bucket") != authority["source_bucket"] or
                    row.get("role") != role or row.get("seal_member") is not True or
                    row.get("seal_membership_scope") != expected_scope or
                    _safe_rel(row.get("relpath"), "capture relpath") != expected_rel or
                    identity["key"] != f"{RAW_KEY_PREFIX}/{expected_rel}"):
                _fail("SOURCE_EVIDENCE_CAPTURE",
                      "capture key/role/scope identity differs")
            if (identity["size"], identity["sha256"]) in denied:
                _fail("OLD_LINEAGE_OVERLAP",
                      "source evidence contains a blocked RFQ content identity")
            normalized_row = {
                **identity, "bucket": row["bucket"], "relpath": expected_rel,
                "source_hour": hour, "ordinal": ordinal, "role": role,
            }
            normalized.append(normalized_row)
            by_hour[hour].append(normalized_row)
        natural = sorted(normalized, key=lambda row: (
            role_hours.index(row["source_hour"]), row["ordinal"]))
        if normalized != natural:
            _fail("SOURCE_EVIDENCE_CAPTURE",
                  "capture rows are not in natural hour/ordinal order")
        for hour, hour_rows in by_hour.items():
            if not hour_rows:
                _fail("SOURCE_EVIDENCE_CAPTURE",
                      f"capture hour {hour} has no physical shard")
            if [row["ordinal"] for row in hour_rows] != list(range(len(hour_rows))):
                _fail("SOURCE_EVIDENCE_CAPTURE",
                      f"capture shards are not contiguous for {hour}")
        return normalized, by_hour

    analysis_captures, analysis_by_hour = normalize_captures(
        analysis_capture_rows, "ANALYSIS", expected_hours[:24])
    watermark_captures, watermark_by_hour = normalize_captures(
        watermark_capture_rows, "WATERMARK", expected_hours[24:])
    captures = analysis_captures + watermark_captures
    capture_by_hour = {**analysis_by_hour, **watermark_by_hour}

    normalized_containers: list[dict[str, Any]] = []
    expected_close_hours = [
        (row + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H")
        for row in expected_parsed
    ]
    for index, raw in enumerate(container_rows):
        row = _exact_keys(
            raw, SOURCE_CONTAINER_ROW_FIELDS, f"receipt_containers[{index}]")
        identity = _normalize_inventory([{
            "key": row["key"], "version_id": row["version_id"],
            "size": row["size"], "sha256": row["sha256"],
        }], "receipt container identity")[0]
        matched = CONTAINER_KEY_RE.fullmatch(identity["key"])
        ordinal = row.get("ordinal")
        if matched is None or type(ordinal) is not int or ordinal < 0:
            _fail("SOURCE_EVIDENCE_CONTAINER",
                  "receipt container key/ordinal is invalid")
        key_hour = f"{matched.group(1)}T{matched.group(2)}"
        key_ordinal = 0 if matched.group(3) is None else int(matched.group(3))
        expected_rel = identity["key"][len(RAW_KEY_PREFIX) + 1:]
        if (row.get("bucket") != authority["source_bucket"] or
                row.get("source_hour") != key_hour or ordinal != key_ordinal or
                row.get("relpath") != expected_rel or
                key_hour not in expected_close_hours or
                type(row.get("seal_member")) is not bool):
            _fail("SOURCE_EVIDENCE_CONTAINER",
                  "receipt container physical identity differs")
        expected_member = expected_close_hours.index(key_hour) < 25
        if row["seal_member"] is not expected_member:
            _fail("SOURCE_EVIDENCE_CONTAINER",
                  "receipt container seal scope differs")
        normalized_containers.append({
            **identity, "bucket": row["bucket"], "relpath": expected_rel,
            "source_hour": key_hour, "ordinal": ordinal,
            "seal_member": row["seal_member"],
        })
    natural_containers = sorted(normalized_containers, key=lambda row: (
        expected_close_hours.index(row["source_hour"]), row["ordinal"]))
    if normalized_containers != natural_containers:
        _fail("SOURCE_EVIDENCE_CONTAINER",
              "receipt containers are not naturally ordered")
    for close_hour in expected_close_hours:
        ordinals = [row["ordinal"] for row in normalized_containers
                    if row["source_hour"] == close_hour]
        if ordinals != list(range(len(ordinals))):
            _fail("SOURCE_EVIDENCE_CONTAINER",
                  f"receipt container shards are not contiguous for {close_hour}")

    if (not isinstance(analysis_proofs, list) or len(analysis_proofs) != 24 or
            not isinstance(watermark_proofs, list) or len(watermark_proofs) != 2):
        _fail("SOURCE_EVIDENCE_PROOFS", "source evidence must contain 24+2 proofs")
    proof_values = analysis_proofs + watermark_proofs
    if [row.get("segment_hour") if isinstance(row, dict) else None
            for row in proof_values] != expected_hours:
        _fail("SOURCE_EVIDENCE_PROOFS",
              "proofs are not unique and ordered across the 24+2 window")
    normalized_proofs = []
    for index, proof in enumerate(proof_values):
        hour = expected_hours[index]
        role = "ANALYSIS" if index < 24 else "WATERMARK"
        hour_objects = capture_by_hour[hour]
        proof_segment_sha = _sha(
            proof.get("raw_segment_canonical_sha256")
            if isinstance(proof, dict) else None,
            f"source proof {hour}.raw_segment_canonical_sha256")
        if hour == segment_hour:
            proof_segment_sha = raw_segment_sha256
        normalized_proof = _normalize_source_proof(
            proof, authority, hour, proof_segment_sha, hour_objects)
        if normalized_proof["role"] != role:
            _fail("SOURCE_EVIDENCE_PROOFS", "source proof role differs")
        normalized_proofs.append(normalized_proof)

    container_projection = [{
        "bucket": row["bucket"], "key": row["key"],
        "version_id": row["version_id"], "size": row["size"],
        "sha256": row["sha256"], "seal_member": row["seal_member"],
    } for row in normalized_containers]
    proof_container_projection = [{
        "bucket": row["container_bucket"], "key": row["container_key"],
        "version_id": row["container_version_id"],
        "size": row["container_size"], "sha256": row["container_sha256"],
        "seal_member": row["container_seal_member"],
    } for row in normalized_proofs]
    if container_projection != proof_container_projection:
        _fail("SOURCE_EVIDENCE_CONTAINER",
              "receipt containers differ from their exact line proofs")

    sealed_capture_fields = {
        "file", "size", "sha256", "checkpoint", "source_hour", "ordinal",
        "role", "seal_membership_scope",
    }
    normalized_sealed_captures = []
    for index, raw in enumerate(sealed_capture_rows):
        row = _exact_keys(
            raw, sealed_capture_fields, f"seal_rfq_capture_members[{index}]")
        size = _size(row["size"], "sealed capture size")
        if (type(row.get("checkpoint")) is not int or row["checkpoint"] != size or
                type(row.get("ordinal")) is not int):
            _fail("SOURCE_EVIDENCE_SEAL", "sealed capture is not byte complete")
        normalized_sealed_captures.append({
            "file": _safe_rel(row["file"], "sealed capture file"),
            "size": size, "sha256": _sha(row["sha256"], "sealed capture sha"),
            "source_hour": row["source_hour"], "ordinal": row["ordinal"],
            "role": row["role"],
            "seal_membership_scope": row["seal_membership_scope"],
        })
    expected_sealed_captures = [{
        "file": row["relpath"], "size": row["size"], "sha256": row["sha256"],
        "source_hour": row["source_hour"], "ordinal": row["ordinal"],
        "role": row["role"],
        "seal_membership_scope": "D_ANALYSIS_MEMBER"
        if row["role"] == "ANALYSIS" else "D_FULL_V2_CROSS_DAY_MEMBER",
    } for row in captures]
    if normalized_sealed_captures != expected_sealed_captures:
        _fail("SOURCE_EVIDENCE_SEAL",
              "sealed capture set differs from exact capture objects")

    sealed_container_fields = {
        "file", "size", "sha256", "checkpoint", "source_hour", "ordinal",
    }
    normalized_sealed_containers = []
    optional_prior_close_hour = expected_hours[0]
    relevant_sealed_hours = expected_close_hours[:25]
    allowed_sealed_hours = [optional_prior_close_hour, *relevant_sealed_hours]
    for index, raw in enumerate(sealed_container_rows):
        row = _exact_keys(
            raw, sealed_container_fields,
            f"seal_rfq_receipt_container_members[{index}]")
        size = _size(row["size"], "sealed container size")
        if (type(row.get("checkpoint")) is not int or row["checkpoint"] != size or
                type(row.get("ordinal")) is not int or row["ordinal"] < 0):
            _fail("SOURCE_EVIDENCE_SEAL", "sealed container is not byte complete")
        source_hour = row.get("source_hour")
        if (not isinstance(source_hour, str) or
                source_hour not in allowed_sealed_hours):
            _fail("SOURCE_EVIDENCE_SEAL",
                  "sealed receipt container hour is outside D full_v2 scope")
        date_text, hour_text = source_hour.split("T")
        expected_file = f"date={date_text}/rfq_receipts_{hour_text}.ndjson"
        if row["ordinal"]:
            expected_file += f".{row['ordinal']}"
        if _safe_rel(row["file"], "sealed container file") != expected_file:
            _fail("SOURCE_EVIDENCE_SEAL",
                  "sealed receipt container path differs from hour/ordinal")
        normalized_sealed_containers.append({
            "file": expected_file,
            "size": size, "sha256": _sha(row["sha256"], "sealed container sha"),
            "source_hour": source_hour, "ordinal": row["ordinal"],
        })
    natural_sealed_containers = sorted(
        normalized_sealed_containers,
        key=lambda row: (
            allowed_sealed_hours.index(row["source_hour"]), row["ordinal"]))
    if normalized_sealed_containers != natural_sealed_containers:
        _fail("SOURCE_EVIDENCE_SEAL",
              "sealed receipt containers are not naturally ordered")
    for close_hour in allowed_sealed_hours:
        ordinals = [
            row["ordinal"] for row in normalized_sealed_containers
            if row["source_hour"] == close_hour]
        if ordinals and ordinals != list(range(len(ordinals))):
            _fail("SOURCE_EVIDENCE_SEAL",
                  f"sealed receipt container shards are not contiguous for {close_hour}")
    expected_sealed_containers = [{
        "file": row["relpath"], "size": row["size"], "sha256": row["sha256"],
        "source_hour": row["source_hour"], "ordinal": row["ordinal"],
    } for row in normalized_containers if row["seal_member"]]
    relevant_sealed_containers = [
        row for row in normalized_sealed_containers
        if row["source_hour"] in relevant_sealed_hours]
    if relevant_sealed_containers != expected_sealed_containers:
        _fail("SOURCE_EVIDENCE_SEAL",
              "sealed receipt containers differ from exact container objects")

    selected_index = expected_hours.index(segment_hour)
    selected_proof = normalized_proofs[selected_index]
    selected_objects = capture_by_hour[segment_hour]
    expected_physical = [{
        "ordinal": row["ordinal"], "relpath": row["relpath"],
        "key": row["key"], "version_id": row["version_id"],
        "size": row["size"], "sha256": row["sha256"],
    } for row in sorted(physical_objects, key=lambda row: (
        row["ordinal"], row["relpath"]))]
    observed_physical = [{
        "ordinal": row["ordinal"], "relpath": row["relpath"],
        "key": row["key"], "version_id": row["version_id"],
        "size": row["size"], "sha256": row["sha256"],
    } for row in selected_objects]
    if observed_physical != expected_physical:
        _fail("SOURCE_EVIDENCE_CAPTURE",
              "selected hour capture exact versions differ from the segment")
    selected_container = normalized_containers[selected_index]
    return (copy.deepcopy(value), selected_proof, selected_container,
            seal_identity)


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
    if (not _canonical_exact_equal(seal_identity, base_seal_identity) or
            seal["date"] != base["date"] or
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
    *, authority: Any, base_manifest_bytes: bytes,
    base_manifest_exact_identity: dict[str, Any],
    hour_receipts: list[dict[str, Any]],
    source_evidence: Any, market_mapping_inputs: Any,
) -> dict[str, Any]:
    market_mapping_inputs = _market_mapping_inputs(market_mapping_inputs)
    authority = validate_fresh_epoch_authority(authority)
    if not isinstance(source_evidence, dict):
        _fail("SOURCE_EVIDENCE_SCHEMA", "source evidence must be an object")
    analysis_date = source_evidence.get("analysis_date")
    _date(analysis_date, "source evidence.analysis_date")
    base = _build_base_binding(
        manifest_bytes=base_manifest_bytes,
        manifest_exact_identity=base_manifest_exact_identity,
        date=analysis_date,
    )
    analysis, watermark, analysis_objects, watermark_objects = \
        _overlay_components(authority, base, hour_receipts, source_evidence)
    source_evidence = _normalize_source_evidence(
        source_evidence, authority, base, analysis, watermark,
        analysis_objects, watermark_objects)
    time_contract = _derive_time_contract(base)
    market_mapping = _build_market_mapping(
        base=base,
        analysis_objects=analysis_objects,
        time_contract=time_contract,
        market_mapping_inputs=market_mapping_inputs,
    )
    result = {
        "schema": OVERLAY_SCHEMA,
        "lane_id": LANE_ID,
        "state": "LOCALLY_VERIFIED_UNPUBLISHED",
        "authority_sha256": authority["authority_sha256"],
        "eligible_date": base["date"],
        "base_binding": base,
        "base_binding_sha256": base["binding_sha256"],
        "time_contract": time_contract,
        "time_contract_sha256": canonical_sha256(time_contract),
        "market_mapping": market_mapping,
        "market_mapping_sha256": market_mapping["mapping_sha256"],
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
        "research_ready": False,
    }
    result["manifest_sha256"] = canonical_sha256(result)
    return validate_overlay_manifest(
        result, authority,
        base_manifest_bytes=base_manifest_bytes,
        base_manifest_exact_identity=base_manifest_exact_identity,
        market_mapping_inputs=market_mapping_inputs,
    )


def validate_overlay_manifest(
    value: Any, authority: Any, *, base_manifest_bytes: bytes,
    base_manifest_exact_identity: dict[str, Any],
    market_mapping_inputs: Any,
) -> dict[str, Any]:
    market_mapping_inputs = _market_mapping_inputs(market_mapping_inputs)
    authority = validate_fresh_epoch_authority(authority)
    fields = {
        "schema", "lane_id", "state", "authority_sha256", "eligible_date",
        "base_binding", "base_binding_sha256", "time_contract",
        "time_contract_sha256", "market_mapping", "market_mapping_sha256",
        "analysis_hours",
        "source_evidence", "source_evidence_sha256",
        "analysis_hour_receipt_set_sha256", "watermark_hours",
        "watermark_hour_receipt_set_sha256", "analysis_rfq_objects",
        "analysis_rfq_object_set_sha256", "watermark_rfq_objects",
        "watermark_rfq_object_set_sha256", "analysis_hour_count",
        "watermark_hour_count", "watermark_objects_in_analysis",
        "data_objects_copied", "aws_write_authorized", "research_eligible",
        "research_ready", "manifest_sha256",
    }
    value = _exact_keys(value, fields, "RFQ overlay manifest")
    fixed = {
        "schema": OVERLAY_SCHEMA, "lane_id": LANE_ID,
        "state": "LOCALLY_VERIFIED_UNPUBLISHED",
        "authority_sha256": authority["authority_sha256"],
        "analysis_hour_count": 24, "watermark_hour_count": 2,
        "watermark_objects_in_analysis": False, "data_objects_copied": 0,
        "aws_write_authorized": False, "research_eligible": False,
        "research_ready": False,
    }
    if (any(value.get(key) != expected for key, expected in fixed.items()) or
            type(value.get("analysis_hour_count")) is not int or
            type(value.get("watermark_hour_count")) is not int or
            value.get("watermark_objects_in_analysis") is not False or
            type(value.get("data_objects_copied")) is not int or
            value.get("aws_write_authorized") is not False or
            value.get("research_eligible") is not False or
            value.get("research_ready") is not False):
        _fail("OVERLAY_INVALID", "fixed local-only overlay contract changed")
    eligible_date = value.get("eligible_date")
    _date(eligible_date, "overlay eligible_date")
    embedded_base = value.get("base_binding")
    if (not isinstance(embedded_base, dict) or
            not _canonical_exact_equal(
                base_manifest_exact_identity,
                embedded_base.get("manifest_exact_identity"))):
        _fail("BASE_BINDING_INVALID",
              "supplied manifest identity differs from embedded base binding")
    base = _build_base_binding(
        manifest_bytes=base_manifest_bytes,
        manifest_exact_identity=base_manifest_exact_identity,
        date=eligible_date,
    )
    if (not _canonical_exact_equal(embedded_base, base) or
            value.get("base_binding_sha256") != base["binding_sha256"]):
        _fail("BASE_BINDING_INVALID",
              "overlay does not embed the exactly rebuilt base binding")
    time_contract = _derive_time_contract(base)
    supplied_time_contract_sha = _sha(
        value.get("time_contract_sha256"), "time_contract_sha256")
    if (not _canonical_exact_equal(
            value.get("time_contract"), time_contract) or
            supplied_time_contract_sha != canonical_sha256(time_contract)):
        _fail(
            "TIME_CONTRACT_INVALID",
            "overlay time contract is not the mechanically derived contract",
        )
    combined = value["analysis_hours"] + value["watermark_hours"] \
        if isinstance(value["analysis_hours"], list) and \
        isinstance(value["watermark_hours"], list) else None
    analysis, watermark, analysis_objects, watermark_objects = \
        _overlay_components(authority, base, combined, value["source_evidence"])
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
    if any(not _canonical_exact_equal(value.get(key), expected)
           for key, expected in bindings.items()):
        _fail("OVERLAY_BINDING", "hour/object set binding mismatch")
    market_mapping = _validate_market_mapping(
        value.get("market_mapping"),
        base=base,
        analysis_objects=analysis_objects,
        time_contract=time_contract,
        market_mapping_inputs=market_mapping_inputs,
    )
    supplied_mapping_sha = _sha(
        value.get("market_mapping_sha256"), "market_mapping_sha256")
    if (not _canonical_exact_equal(value.get("market_mapping"), market_mapping)
            or supplied_mapping_sha != market_mapping["mapping_sha256"]):
        _fail(
            "MARKET_MAPPING_INVALID",
            "overlay market mapping binding differs from exact rebuild",
        )
    supplied = _sha(value["manifest_sha256"], "manifest_sha256")
    unsigned = copy.deepcopy(value)
    unsigned.pop("manifest_sha256")
    if supplied != canonical_sha256(unsigned):
        _fail("OVERLAY_DIGEST", "manifest_sha256 mismatch")
    return copy.deepcopy(value)
