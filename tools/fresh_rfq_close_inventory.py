#!/usr/bin/env python3
"""Offline validation of the fresh-RFQ D+1 close-family inventory.

This module deliberately does not call AWS.  It validates a controlled
adapter's body-free projection of every ``ListObjectVersions`` page for the
single D+1 ``rfq_receipts_02.ndjson`` family.  Starting at an empty marker and
ending at a non-truncated page is a structural property of the supplied
projection; it is not evidence that AWS returned those pages.  A separate
transport attestation is required before the result can be used by research.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from typing import Any


SCHEMA = "fresh-rfq-close-inventory-snapshot-v1"
SOURCE_BUCKET = "kalshi-vault-ritcardo"
RAW_PREFIX = "ec2/raw/"
MAX_SHARD_ORDINAL = 1_000_000

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTITY_FIELDS = {"bucket", "key", "version_id", "size", "sha256"}
VERSION_FIELDS = {"key", "version_id", "is_latest", "size"}
DELETE_MARKER_FIELDS = {"key", "version_id", "is_latest"}
PAGE_FIELDS = {
    "request_key_marker",
    "request_version_id_marker",
    "response_key_marker",
    "response_version_id_marker",
    "is_truncated",
    "next_key_marker",
    "next_version_id_marker",
    "versions",
    "delete_markers",
}

SNAPSHOT_FIELDS = {
    "schema",
    "state",
    "analysis_date",
    "inventory_date",
    "bucket",
    "prefix",
    "pages",
    "page_count",
    "version_entry_count",
    "delete_marker_entry_count",
    "family_key_count",
    "latest_version_count",
    "latest_shard_ordinals",
    "latest_exact_identities",
    "page_chain_sha256",
    "version_inventory_sha256",
    "delete_marker_inventory_sha256",
    "latest_exact_identity_set_sha256",
    "caller_projection_marker_chain_terminated",
    "all_family_keys_have_exactly_one_latest",
    "latest_delete_marker_count",
    "latest_keys_contiguous_from_base",
    "input_bodies_omitted",
    "data_objects_copied",
    "module_write_api_call_count",
    "aws_list_performed_by_module",
    "aws_inventory_transport_verified",
    "requires_external_aws_list_attestation",
    "research_ready",
    "snapshot_sha256",
}


class FreshRfqCloseInventoryError(RuntimeError):
    """Fail-closed inventory-projection violation with a stable code."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str) -> None:
    raise FreshRfqCloseInventoryError(code, detail)


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


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        _fail("SCHEMA_FIELDS", f"{label} fields differ from contract")
    return value


def _text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        _fail("INVALID_TEXT", f"{label} must be canonical non-empty text")
    return value


def _version_id(value: Any, label: str) -> str:
    value = _text(value, label)
    if (
        value.lower() == "null"
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        _fail("VERSION_ID_REQUIRED", f"{label} must be a non-null VersionId")
    return value


def _integer(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        _fail("INVALID_INTEGER", f"{label} must be an integer >= 0")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        _fail("INVALID_BOOLEAN", f"{label} must be boolean")
    return value


def _date(value: Any, label: str) -> dt.date:
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        _fail("INVALID_DATE", f"{label} must be YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        _fail("INVALID_DATE", f"{label}: {exc}")
    if parsed.isoformat() != value:
        _fail("INVALID_DATE", f"{label} is not canonical")
    return parsed


def _marker_text(value: Any, label: str, *, version: bool = False) -> str | None:
    if value is None:
        return None
    return _version_id(value, label) if version else _text(value, label)


def _family_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}(?:\.([1-9][0-9]*))?$")


def _family_ordinal(
    value: Any, label: str, *, pattern: re.Pattern[str]
) -> tuple[str, int]:
    key = _text(value, label)
    match = pattern.fullmatch(key)
    if match is None:
        _fail("FAMILY_KEY_MISMATCH", f"{label} is outside the exact close family")
    suffix = match.group(1)
    if suffix is None:
        return key, 0
    if len(suffix) > 7:
        _fail("SHARD_ORDINAL_INVALID", f"{label} shard ordinal is too large")
    try:
        ordinal = int(suffix)
    except (ValueError, OverflowError) as exc:
        _fail("SHARD_ORDINAL_INVALID", f"{label}: {exc}")
    if ordinal > MAX_SHARD_ORDINAL:
        _fail("SHARD_ORDINAL_INVALID", f"{label} shard ordinal is too large")
    return key, ordinal


def _normalize_version(
    value: Any, label: str, *, pattern: re.Pattern[str]
) -> dict[str, Any]:
    value = _exact_keys(value, VERSION_FIELDS, label)
    key, _ordinal = _family_ordinal(value["key"], f"{label}.key", pattern=pattern)
    return {
        "key": key,
        "version_id": _version_id(value["version_id"], f"{label}.version_id"),
        "is_latest": _boolean(value["is_latest"], f"{label}.is_latest"),
        "size": _integer(value["size"], f"{label}.size"),
    }


def _normalize_delete_marker(
    value: Any, label: str, *, pattern: re.Pattern[str]
) -> dict[str, Any]:
    value = _exact_keys(value, DELETE_MARKER_FIELDS, label)
    key, _ordinal = _family_ordinal(value["key"], f"{label}.key", pattern=pattern)
    return {
        "key": key,
        "version_id": _version_id(value["version_id"], f"{label}.version_id"),
        "is_latest": _boolean(value["is_latest"], f"{label}.is_latest"),
    }


def _normalize_page(
    value: Any, index: int, *, pattern: re.Pattern[str]
) -> dict[str, Any]:
    label = f"pages[{index}]"
    value = _exact_keys(value, PAGE_FIELDS, label)

    markers = {}
    for name in (
        "request_key_marker",
        "response_key_marker",
        "next_key_marker",
    ):
        markers[name] = _marker_text(value[name], f"{label}.{name}")
        if markers[name] is not None:
            _family_ordinal(
                markers[name], f"{label}.{name}", pattern=pattern
            )
    for name in (
        "request_version_id_marker",
        "response_version_id_marker",
        "next_version_id_marker",
    ):
        markers[name] = _marker_text(
            value[name], f"{label}.{name}", version=True
        )

    for key_name, version_name in (
        ("request_key_marker", "request_version_id_marker"),
        ("response_key_marker", "response_version_id_marker"),
        ("next_key_marker", "next_version_id_marker"),
    ):
        if markers[key_name] is None and markers[version_name] is not None:
            _fail(
                "MARKER_PAIR_INVALID",
                f"{label}.{version_name} requires {key_name}",
            )

    versions = value["versions"]
    delete_markers = value["delete_markers"]
    if not isinstance(versions, list) or not isinstance(delete_markers, list):
        _fail("PAGE_ENTRIES", f"{label} entry collections must be lists")
    normalized_versions = [
        _normalize_version(row, f"{label}.versions[{row_index}]", pattern=pattern)
        for row_index, row in enumerate(versions)
    ]
    normalized_deletes = [
        _normalize_delete_marker(
            row, f"{label}.delete_markers[{row_index}]", pattern=pattern
        )
        for row_index, row in enumerate(delete_markers)
    ]
    if not normalized_versions and not normalized_deletes:
        _fail("EMPTY_PAGE", f"{label} contains no version entries")

    normalized_versions.sort(
        key=lambda row: (row["key"], row["version_id"], row["size"], row["is_latest"])
    )
    normalized_deletes.sort(
        key=lambda row: (row["key"], row["version_id"], row["is_latest"])
    )
    return {
        **markers,
        "is_truncated": _boolean(value["is_truncated"], f"{label}.is_truncated"),
        "versions": normalized_versions,
        "delete_markers": normalized_deletes,
    }


def _normalize_identity(
    value: Any, label: str, *, pattern: re.Pattern[str]
) -> dict[str, Any]:
    value = _exact_keys(value, IDENTITY_FIELDS, label)
    bucket = _text(value["bucket"], f"{label}.bucket")
    if bucket != SOURCE_BUCKET:
        _fail("BUCKET_MISMATCH", f"{label}.bucket is outside the fixed source")
    key, _ordinal = _family_ordinal(value["key"], f"{label}.key", pattern=pattern)
    sha256 = value["sha256"]
    if not isinstance(sha256, str) or SHA_RE.fullmatch(sha256) is None:
        _fail("INVALID_SHA256", f"{label}.sha256 must be lowercase 64-hex")
    return {
        "bucket": bucket,
        "key": key,
        "version_id": _version_id(value["version_id"], f"{label}.version_id"),
        "size": _integer(value["size"], f"{label}.size"),
        "sha256": sha256,
    }


def build_close_inventory_snapshot(
    *,
    analysis_date: Any,
    pages: Any,
    exact_identities: Any,
    bucket: Any = SOURCE_BUCKET,
    prefix: Any | None = None,
) -> dict[str, Any]:
    """Validate a caller projection and return a deterministic snapshot.

    ``pages`` is a normalized, offline projection.  This function has no AWS
    client argument and performs no list, get, write, copy, or network call.
    """

    parsed_date = _date(analysis_date, "analysis_date")
    try:
        inventory_date = (parsed_date + dt.timedelta(days=1)).isoformat()
    except OverflowError as exc:
        _fail("INVALID_DATE", f"analysis_date has no D+1: {exc}")
    expected_prefix = f"{RAW_PREFIX}date={inventory_date}/rfq_receipts_02.ndjson"
    if prefix is None:
        prefix = expected_prefix
    if prefix != expected_prefix:
        _fail("PREFIX_MISMATCH", "prefix is not the exact D+1 close family")
    if bucket != SOURCE_BUCKET:
        _fail("BUCKET_MISMATCH", "bucket is outside the fixed source")
    pattern = _family_pattern(expected_prefix)

    if not isinstance(pages, list) or not pages:
        _fail("PAGES_REQUIRED", "at least one projected list page is required")
    normalized_pages = [
        _normalize_page(row, index, pattern=pattern)
        for index, row in enumerate(pages)
    ]

    seen_request_markers: set[tuple[str | None, str | None]] = set()
    seen_next_markers: set[tuple[str | None, str | None]] = set()
    expected_request = (None, None)
    for index, page in enumerate(normalized_pages):
        request = (
            page["request_key_marker"],
            page["request_version_id_marker"],
        )
        response = (
            page["response_key_marker"],
            page["response_version_id_marker"],
        )
        next_marker = (
            page["next_key_marker"],
            page["next_version_id_marker"],
        )
        if request != expected_request:
            _fail("MARKER_CHAIN_BROKEN", f"pages[{index}] request marker is discontinuous")
        if response != request:
            _fail("RESPONSE_MARKER_MISMATCH", f"pages[{index}] response does not echo request")
        if request in seen_request_markers:
            _fail("MARKER_LOOP", f"pages[{index}] repeats a request marker")
        seen_request_markers.add(request)

        request_key = request[0]
        next_key = next_marker[0]
        if (
            request_key is not None
            and next_key is not None
            and next_key < request_key
        ):
            _fail(
                "MARKER_NOT_FORWARD",
                f"pages[{index}] next key marker regresses",
            )
        entry_keys = [
            row["key"] for row in page["versions"] + page["delete_markers"]
        ]
        if request_key is not None and any(
            key < request_key for key in entry_keys
        ):
            _fail(
                "PAGE_ENTRY_RANGE",
                f"pages[{index}] contains a key before its request marker",
            )
        if (
            page["is_truncated"]
            and next_key is not None
            and next_marker != request
            and next_marker not in seen_request_markers
            and any(key > next_key for key in entry_keys)
        ):
            _fail(
                "PAGE_ENTRY_RANGE",
                f"pages[{index}] contains a key after its next marker",
            )

        if page["is_truncated"]:
            if next_marker[0] is None:
                _fail("TRUNCATED_WITHOUT_NEXT", f"pages[{index}] lacks a next key marker")
            if next_marker == request or next_marker in seen_request_markers:
                _fail("MARKER_LOOP", f"pages[{index}] next marker loops")
            if next_marker in seen_next_markers:
                _fail("MARKER_LOOP", f"pages[{index}] repeats a next marker")
            seen_next_markers.add(next_marker)
            expected_request = next_marker
            if index == len(normalized_pages) - 1:
                _fail("TRUNCATED_TAIL", "final projected page remains truncated")
        else:
            if next_marker != (None, None):
                _fail("FINAL_NEXT_MARKER", f"pages[{index}] is final but has next markers")
            if index != len(normalized_pages) - 1:
                _fail("PAGE_AFTER_FINAL", f"pages[{index}] is followed after completion")

    versions = [row for page in normalized_pages for row in page["versions"]]
    delete_markers = [
        row for page in normalized_pages for row in page["delete_markers"]
    ]
    seen_entries: set[tuple[str, str]] = set()
    by_key: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for kind, rows in (("version", versions), ("delete_marker", delete_markers)):
        for row in rows:
            identity = (row["key"], row["version_id"])
            if identity in seen_entries:
                _fail("DUPLICATE_ENTRY", f"duplicate key/version {identity[0]} {identity[1]}")
            seen_entries.add(identity)
            by_key.setdefault(row["key"], []).append((kind, row))

    latest_versions: dict[str, dict[str, Any]] = {}
    for key, entries in by_key.items():
        latest = [(kind, row) for kind, row in entries if row["is_latest"]]
        if len(latest) != 1:
            _fail("LATEST_CARDINALITY", f"{key} must have exactly one IsLatest entry")
        kind, row = latest[0]
        if kind == "delete_marker":
            _fail("LATEST_IS_DELETE_MARKER", f"{key} latest entry is a delete marker")
        latest_versions[key] = row

    ordinals = sorted(
        _family_ordinal(key, "latest key", pattern=pattern)[1]
        for key in latest_versions
    )
    if not ordinals or ordinals[0] != 0:
        _fail("BASE_SHARD_MISSING", "latest family must include the base object")
    if any(ordinal != index for index, ordinal in enumerate(ordinals)):
        _fail("SHARD_ORDINAL_GAP", "latest shard ordinals are not contiguous from zero")

    if not isinstance(exact_identities, list):
        _fail("IDENTITIES_REQUIRED", "exact_identities must be a list")
    identities = [
        _normalize_identity(row, f"exact_identities[{index}]", pattern=pattern)
        for index, row in enumerate(exact_identities)
    ]
    identities.sort(key=lambda row: (row["key"], row["version_id"]))
    identity_by_key: dict[str, dict[str, Any]] = {}
    for identity in identities:
        if identity["key"] in identity_by_key:
            _fail("DUPLICATE_IDENTITY", f"duplicate identity key {identity['key']}")
        identity_by_key[identity["key"]] = identity
    if set(identity_by_key) != set(latest_versions):
        _fail("IDENTITY_SET_MISMATCH", "exact identity keys differ from latest family keys")
    for key, latest in latest_versions.items():
        identity = identity_by_key[key]
        if (
            identity["version_id"] != latest["version_id"]
            or identity["size"] != latest["size"]
        ):
            _fail("IDENTITY_SET_MISMATCH", f"exact identity differs from latest entry for {key}")

    normalized_versions = sorted(
        versions,
        key=lambda row: (row["key"], row["version_id"], row["size"], row["is_latest"]),
    )
    normalized_deletes = sorted(
        delete_markers,
        key=lambda row: (row["key"], row["version_id"], row["is_latest"]),
    )
    result = {
        "schema": SCHEMA,
        "state": "CALLER_PROJECTION_VALIDATED_AWS_LIST_ATTESTATION_REQUIRED",
        "analysis_date": parsed_date.isoformat(),
        "inventory_date": inventory_date,
        "bucket": SOURCE_BUCKET,
        "prefix": expected_prefix,
        "pages": copy.deepcopy(normalized_pages),
        "page_count": len(normalized_pages),
        "version_entry_count": len(versions),
        "delete_marker_entry_count": len(delete_markers),
        "family_key_count": len(by_key),
        "latest_version_count": len(latest_versions),
        "latest_shard_ordinals": ordinals,
        "latest_exact_identities": copy.deepcopy(identities),
        "page_chain_sha256": canonical_sha256(normalized_pages),
        "version_inventory_sha256": canonical_sha256(normalized_versions),
        "delete_marker_inventory_sha256": canonical_sha256(normalized_deletes),
        "latest_exact_identity_set_sha256": canonical_sha256(identities),
        "caller_projection_marker_chain_terminated": True,
        "all_family_keys_have_exactly_one_latest": True,
        "latest_delete_marker_count": 0,
        "latest_keys_contiguous_from_base": True,
        "input_bodies_omitted": True,
        "data_objects_copied": 0,
        "module_write_api_call_count": 0,
        "aws_list_performed_by_module": False,
        "aws_inventory_transport_verified": False,
        "requires_external_aws_list_attestation": True,
        "research_ready": False,
    }
    result["snapshot_sha256"] = canonical_sha256(result)
    return result


def validate_close_inventory_snapshot(value: Any) -> None:
    """Rebuild and compare a snapshot, rejecting any altered projection."""

    value = _exact_keys(value, SNAPSHOT_FIELDS, "snapshot")
    if value.get("schema") != SCHEMA:
        _fail("SCHEMA", f"schema must be {SCHEMA}")
    supplied = value.get("snapshot_sha256")
    if not isinstance(supplied, str) or SHA_RE.fullmatch(supplied) is None:
        _fail("INVALID_SHA256", "snapshot_sha256 must be lowercase 64-hex")
    unsigned = copy.deepcopy(value)
    unsigned.pop("snapshot_sha256")
    if supplied != canonical_sha256(unsigned):
        _fail("SNAPSHOT_DIGEST_MISMATCH", "snapshot_sha256 does not match contents")
    rebuilt = build_close_inventory_snapshot(
        analysis_date=value.get("analysis_date"),
        pages=copy.deepcopy(value.get("pages")),
        exact_identities=copy.deepcopy(value.get("latest_exact_identities")),
        bucket=value.get("bucket"),
        prefix=value.get("prefix"),
    )
    if rebuilt != value:
        _fail("SNAPSHOT_DERIVATION_MISMATCH", "snapshot differs from canonical derivation")


__all__ = [
    "FreshRfqCloseInventoryError",
    "SCHEMA",
    "SOURCE_BUCKET",
    "build_close_inventory_snapshot",
    "canonical_bytes",
    "canonical_sha256",
    "validate_close_inventory_snapshot",
]
