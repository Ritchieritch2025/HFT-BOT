#!/usr/bin/env python3
"""Pure exact-version L1/L2/base binding for the fresh RFQ lane.

The caller supplies the exact bytes of an already VersionId-addressed v3
reference manifest and the identity returned by that read.  This module does
not read files, call AWS, or copy data.  It deliberately delegates the full
reference-release trust contract to :mod:`research_reference` and only adds
the stricter family-completeness contract needed by the fresh RFQ overlay.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any

import research_reference as reference


SCHEMA = "fresh-rfq-base-binding-v1"
STATE = "LOCALLY_VERIFIED_UNPUBLISHED"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024

FACT_FAMILIES = (
    "orderbooks_l1",
    "orderbooks_full",
    "trades",
)
DIM_LOGICAL_KEYS = (
    "series.csv",
    "events.csv",
    "markets.csv",
)
CATALOG_LOGICAL_KEYS = (
    "series/part-00000.parquet",
    "events/part-00000.parquet",
    "markets/part-00000.parquet",
    "settlements/part-00000.parquet",
    "series_classified/part-00000.parquet",
)
FAMILY_ORDER = FACT_FAMILIES + ("dated_dim", "catalogs")

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


class FreshRfqBaseBindingError(ValueError):
    """Stable fail-closed error raised while deriving a base binding."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _fail(code: str, message: str) -> None:
    raise FreshRfqBaseBindingError(code, message)


def _strict_json(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes):
        _fail("MANIFEST_BYTES_INVALID", "manifest must be exact bytes")
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
        _fail("MANIFEST_BYTES_INVALID", "manifest byte size is outside bounds")

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail("MANIFEST_JSON_INVALID", f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def bad_constant(value: str) -> None:
        _fail("MANIFEST_JSON_INVALID", f"non-finite JSON value {value}")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs_hook,
            parse_constant=bad_constant,
        )
    except FreshRfqBaseBindingError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("MANIFEST_JSON_INVALID", str(exc))
    if not isinstance(value, dict):
        _fail("MANIFEST_JSON_INVALID", "manifest root must be an object")
    return value


def _utc(value: Any, label: str) -> tuple[str, dt.datetime]:
    if not isinstance(value, str) or UTC_RE.fullmatch(value) is None:
        _fail("CUTOFF_INVALID", f"{label} must be canonical UTC ending in Z")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        _fail("CUTOFF_INVALID", f"{label}: {exc}")
    return value, parsed


def _date(value: Any) -> str:
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        _fail("DATE_INVALID", "date must be YYYY-MM-DD")
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        _fail("DATE_INVALID", str(exc))
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _fail("EXACT_IDENTITY_INVALID", f"{label} is not lowercase SHA-256")
    return value


def _version(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value.strip().lower() == "null"
    ):
        _fail("EXACT_IDENTITY_INVALID", f"{label} requires a non-null VersionId")
    return value


def _manifest_identity(
    value: Any,
    raw: bytes,
    release_id: str,
) -> dict[str, Any]:
    fields = {"bucket", "key", "version_id", "size", "sha256"}
    if not isinstance(value, dict) or set(value) != fields:
        _fail(
            "MANIFEST_IDENTITY_INVALID",
            "manifest exact identity fields differ from the fixed contract",
        )
    if value["bucket"] != reference.TRUSTED_BUCKET:
        _fail("MANIFEST_IDENTITY_INVALID", "manifest bucket is not trusted")
    expected_key = f"research/releases/{release_id}/MANIFEST.json"
    if value["key"] != expected_key:
        _fail(
            "MANIFEST_IDENTITY_INVALID",
            "manifest key does not bind the validated release_id",
        )
    if type(value["size"]) is not int or value["size"] != len(raw):
        _fail("MANIFEST_DIGEST_MISMATCH", "manifest byte size mismatch")
    expected_sha = hashlib.sha256(raw).hexdigest()
    if _sha(value["sha256"], "manifest sha256") != expected_sha:
        _fail("MANIFEST_DIGEST_MISMATCH", "manifest byte SHA-256 mismatch")
    return {
        "bucket": value["bucket"],
        "key": value["key"],
        "version_id": _version(value["version_id"], "manifest version_id"),
        "size": value["size"],
        "sha256": value["sha256"],
    }


def _source_binding(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "logical_key": item["logical_key"],
        "bucket": item["source_bucket"],
        "key": item["source_key"],
        "version_id": item["source_version_id"],
        "size": item["size"],
        "sha256": item["sha256"],
    }


def _control_identity(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "bucket": value["bucket"],
        "key": value["key"],
        "version_id": value["version_id"],
        "size": value["size"],
        "sha256": value["sha256"],
    }


def _fact_date_is_exact(logical_key: str, family: str, date: str) -> bool:
    prefix = f"warehouse/facts/{family}/"
    if not logical_key.startswith(prefix):
        return False
    date_segments = [
        segment for segment in logical_key[len(prefix) :].split("/")
        if segment.startswith("date=")
    ]
    return date_segments == [f"date={date}"]


def _extract_families(
    descriptor: dict[str, Any], date: str
) -> dict[str, dict[str, Any]]:
    family_rows: dict[str, list[dict[str, Any]]] = {
        name: [] for name in FAMILY_ORDER
    }
    dim_expected = {
        f"warehouse/dim/snapshots/date={date}/{name}"
        for name in DIM_LOGICAL_KEYS
    }
    catalog_expected = {
        f"warehouse/catalog/{name}" for name in CATALOG_LOGICAL_KEYS
    }

    for item in descriptor["objects"]:
        logical = item["logical_key"]
        if (
            item["kind"] == "rfq"
            or item["channel"] == "rfq"
            or logical.startswith("raw_rfq/")
        ):
            _fail(
                "RFQ_BASE_CONTAMINATION",
                "RFQ objects must remain in the independent fresh overlay",
            )
        if item["date"] != date:
            _fail("CROSS_DATE_OBJECT", f"base object has another date: {logical}")
        if item["kind"] == "facts":
            family = item["channel"]
            if family not in FACT_FAMILIES:
                _fail("BASE_FAMILY_INVALID", f"unexpected facts family: {family}")
            if not _fact_date_is_exact(logical, family, date):
                _fail("CROSS_DATE_OBJECT", f"facts path is not exactly date D: {logical}")
            family_rows[family].append(_source_binding(item))
        elif item["kind"] == "dim_snapshot":
            if logical not in dim_expected:
                _fail("BASE_FAMILY_INVALID", f"unexpected dated dim: {logical}")
            family_rows["dated_dim"].append(_source_binding(item))
        elif item["kind"] == "catalog":
            if logical not in catalog_expected:
                _fail("BASE_FAMILY_INVALID", f"unexpected catalog: {logical}")
            family_rows["catalogs"].append(_source_binding(item))

    for family in FACT_FAMILIES:
        if not family_rows[family]:
            _fail("BASE_FAMILY_MISSING", f"required facts family missing: {family}")
    got_dim = {row["logical_key"] for row in family_rows["dated_dim"]}
    if got_dim != dim_expected:
        _fail(
            "BASE_FAMILY_MISSING",
            f"dated dim set mismatch: missing={sorted(dim_expected - got_dim)}",
        )
    got_catalog = {row["logical_key"] for row in family_rows["catalogs"]}
    if got_catalog != catalog_expected:
        _fail(
            "BASE_FAMILY_MISSING",
            f"five-catalog set mismatch: missing={sorted(catalog_expected - got_catalog)}",
        )

    all_rows: list[dict[str, Any]] = []
    normalized: dict[str, dict[str, Any]] = {}
    for family in FAMILY_ORDER:
        rows = sorted(family_rows[family], key=lambda row: row["logical_key"])
        if len({row["logical_key"] for row in rows}) != len(rows):
            _fail("BASE_OBJECT_DUPLICATE", f"duplicate logical key in {family}")
        normalized[family] = {
            "object_count": len(rows),
            "set_sha256": canonical_sha256(rows),
            "objects": rows,
        }
        all_rows.extend(rows)
    if len({
        (row["bucket"], row["key"], row["version_id"]) for row in all_rows
    }) != len(all_rows):
        _fail("BASE_OBJECT_DUPLICATE", "duplicate exact version across base families")
    return normalized


def build_base_binding(
    *,
    manifest_bytes: bytes,
    manifest_exact_identity: dict[str, Any],
    date: str,
    as_of_cutoff_utc: str | None = None,
) -> dict[str, Any]:
    """Build a body-free, local-only exact binding for base market data.

    Every invocation re-parses the exact bytes and calls
    ``research_reference.validate_manifest``.  A previously normalized or
    hand-assembled descriptor therefore cannot bypass the v3 contract.
    """
    date = _date(date)
    manifest = _strict_json(manifest_bytes)
    try:
        descriptor = reference.validate_manifest(manifest)
    except reference.ReferenceManifestError as exc:
        _fail("REFERENCE_MANIFEST_INVALID", str(exc))
    if descriptor["date"] != date:
        _fail("DATE_MISMATCH", "requested date differs from reference release")
    exact_manifest = _manifest_identity(
        manifest_exact_identity, manifest_bytes, descriptor["release_id"]
    )
    published_text, published = _utc(
        descriptor["published_at_utc"], "published_at_utc"
    )
    if as_of_cutoff_utc is not None:
        supplied_cutoff, _ = _utc(as_of_cutoff_utc, "as_of_cutoff_utc")
        if supplied_cutoff != published_text:
            _fail(
                "CUTOFF_INVALID",
                "as-of cutoff must equal the exact manifest published_at_utc",
            )
    analysis_end_text = (
        dt.date.fromisoformat(date) + dt.timedelta(days=1)
    ).isoformat() + "T00:00:00Z"
    _, analysis_end = _utc(analysis_end_text, "analysis_data_end_utc")
    if published < analysis_end:
        _fail(
            "CUTOFF_INVALID",
            "manifest publication precedes the complete analysis day",
        )

    families = _extract_families(descriptor, date)
    all_rows = sorted(
        [
            row
            for family in FAMILY_ORDER
            for row in families[family]["objects"]
        ],
        key=lambda row: row["logical_key"],
    )
    family_digest_rows = [
        {
            "family": family,
            "object_count": families[family]["object_count"],
            "set_sha256": families[family]["set_sha256"],
        }
        for family in FAMILY_ORDER
    ]
    result = {
        "schema": SCHEMA,
        "state": STATE,
        "storage_mode": reference.STORAGE_MODE,
        "verification_state": "REFERENCE_V3_MANIFEST_EXACT_VALIDATED",
        "source_objects_exact_get_verified": False,
        "durable_receipt_exact_get_verified": False,
        "date": date,
        "release_id": descriptor["release_id"],
        "as_of_cutoff_utc": published_text,
        "analysis_data_end_utc": analysis_end_text,
        "manifest_exact_identity": exact_manifest,
        "manifest_reference_set_sha256": descriptor["reference_set_sha256"],
        "manifest_object_semantics_sha256": descriptor[
            "object_semantics_sha256"
        ],
        "source_seal": {
            **_control_identity(descriptor["source_seal"]),
            "manifest_declared_verification_state": descriptor[
                "source_seal"
            ]["verification_state"],
        },
        "canonical_receipt": {
            "receipt_set_sha256": descriptor[
                "canonical_receipt_set_sha256"
            ],
            "receipt_object": _control_identity(descriptor["receipt_object"]),
        },
        "families": families,
        "family_set_sha256": canonical_sha256(family_digest_rows),
        "base_object_count": len(all_rows),
        "base_exact_set_sha256": canonical_sha256(all_rows),
        "rfq_objects_in_base": 0,
        "data_objects_copied": 0,
        "aws_write_authorized": False,
        "research_eligible": False,
    }
    result["binding_sha256"] = canonical_sha256(result)
    return result
