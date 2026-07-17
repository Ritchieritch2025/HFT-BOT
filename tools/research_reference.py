#!/usr/bin/env python3
"""Strict reader-side contract for zero-copy research reference releases.

This module is deliberately pure: it validates and normalizes an already
downloaded v3 manifest, but performs no network or filesystem mutation.  The
read-only transport and content-addressed cache live in ``research_data.py``.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re


SCHEMA = "research-release-manifest-v3-reference"
STORAGE_MODE = "CANONICAL_REFERENCE"
TRUSTED_BUCKET = "kalshi-vault-ritcardo"
RFQ_POLICY = "OPTIONAL_SEALED_ONLY"
MAX_DURABLE_RECEIPT_BYTES = 16 * 1024 * 1024

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RELEASE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})__v3ref__seal-([0-9a-f]{8})"
    r"__pub-([0-9a-f]{16})$"
)
RFQ_BASENAME_RE = re.compile(
    r"^rfq(?:_receipts)?_\d{2}\.ndjson(?:\.\d+)?$"
)
RAW_RFQ_LOGICAL_RE = re.compile(
    r"^raw_rfq/date=(\d{4}-\d{2}-\d{2})/([^/]+)$"
)
RFQ_ELIGIBILITY_BINDING_SCHEMA = "canonical-rfq-eligibility-binding-v1"
_RFQ_ELIGIBILITY_BINDING_FIELDS = {
    "schema_version",
    "state",
    "source",
    "seal_sha256",
    "rfq_exact_set_sha256",
    "eligibility_evidence_sha256",
    "evidence_tier",
    "integrity_state",
    "quarantine_state",
    "repair_branch_state",
}


class ReferenceManifestError(ValueError):
    """A stable fail-closed v3 manifest validation error."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _safe_key(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReferenceManifestError(f"{label} is empty or not text")
    if value.startswith(("/", "\\")) or "\\" in value or "\x00" in value:
        raise ReferenceManifestError(f"{label} is not a portable relative key")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ReferenceManifestError(f"{label} escapes containment: {value!r}")
    return value


def _date(value: object, label: str) -> str:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ReferenceManifestError(f"{label} must be YYYY-MM-DD")
    try:
        datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise ReferenceManifestError(f"{label}: {exc}") from exc
    return value


def _utc(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReferenceManifestError(f"{label} is missing")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReferenceManifestError(f"{label}: {exc}") from exc
    if parsed.tzinfo is None:
        raise ReferenceManifestError(f"{label} is not timezone-aware")
    if parsed.utcoffset() != datetime.timedelta(0):
        raise ReferenceManifestError(f"{label} is not UTC")
    return value


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.match(value):
        raise ReferenceManifestError(f"{label} is not a lowercase SHA-256")
    return value


def _version(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value.strip().lower() == "null"
    ):
        raise ReferenceManifestError(f"{label} has an empty VersionId")
    return value


def _size(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReferenceManifestError(f"{label} has invalid size {value!r}")
    return value


def _normalize_rfq_eligibility_binding(value: object,
                                       seal_sha256: str) -> dict:
    """Validate the closed, set-level RFQ eligibility evidence shape."""
    if not isinstance(value, dict) or set(value) != \
            _RFQ_ELIGIBILITY_BINDING_FIELDS:
        raise ReferenceManifestError(
            "RFQ eligibility binding differs from the fixed contract")
    fixed = {
        "schema_version": RFQ_ELIGIBILITY_BINDING_SCHEMA,
        "state": "ELIGIBLE_SEALED_REFERENCE",
        "source": "seal.raw_files",
        "evidence_tier": "SEALED_CONFIRMATION",
        "integrity_state": "PASS",
        "quarantine_state": "CLEAR",
        "repair_branch_state": "CLOSED_NO_REPAIR",
    }
    for key, expected in fixed.items():
        if value.get(key) != expected:
            raise ReferenceManifestError(
                f"RFQ eligibility binding {key} mismatch")
    normalized = dict(value)
    normalized["seal_sha256"] = _sha(
        value.get("seal_sha256"), "RFQ eligibility seal_sha256")
    normalized["rfq_exact_set_sha256"] = _sha(
        value.get("rfq_exact_set_sha256"),
        "RFQ eligibility rfq_exact_set_sha256")
    normalized["eligibility_evidence_sha256"] = _sha(
        value.get("eligibility_evidence_sha256"),
        "RFQ eligibility eligibility_evidence_sha256")
    if normalized["seal_sha256"] != seal_sha256:
        raise ReferenceManifestError(
            "RFQ eligibility binding names a different seal")
    return normalized


def _binding(value: object, label: str) -> object:
    # Ensure the binding is JSON-canonicalizable now, rather than later while
    # constructing a security identity.
    try:
        canonical_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ReferenceManifestError(f"{label} is not canonical JSON") from exc
    return value


def reference_set_projection(objects: list[dict]) -> list[dict]:
    fields = (
        "logical_key",
        "source_bucket",
        "source_key",
        "source_version_id",
        "size",
        "sha256",
    )
    return [
        {field: item[field] for field in fields}
        for item in sorted(objects, key=lambda item: item["logical_key"])
    ]


def reference_set_sha256(objects: list[dict]) -> str:
    return canonical_sha256(reference_set_projection(objects))


def object_semantics_projection(objects: list[dict]) -> list[dict]:
    fields = (
        "logical_key",
        "kind",
        "channel",
        "date",
        "required",
        "seal_binding",
        "evidence_binding",
    )
    return [
        {field: item[field] for field in fields}
        for item in sorted(objects, key=lambda item: item["logical_key"])
    ]


def object_semantics_sha256(objects: list[dict]) -> str:
    return canonical_sha256(object_semantics_projection(objects))


def _local_key(logical: str, date: str) -> str:
    """Map canonical names to the unchanged W05 release/view shape."""
    if logical.startswith("warehouse/facts/"):
        return logical[len("warehouse/") :]
    if logical.startswith("warehouse/dim/"):
        return logical[len("warehouse/") :]
    if logical.startswith("warehouse/catalog/"):
        return logical[len("warehouse/") :]
    if logical.startswith("warehouse/corrections/"):
        return logical[len("warehouse/") :]
    if logical.startswith("warehouse/seals/"):
        return "seal/" + logical[len("warehouse/seals/") :]
    if logical == "warehouse/manifest.csv":
        return "warehouse_manifest/manifest.csv"
    quality_root = f"control/quality/v1/date={date}/"
    if logical == quality_root + "capture_gap_receipt.json":
        return f"quality/gap_receipt_{date}.json"
    if logical == quality_root + "capture_gaps.csv":
        return f"quality/capture_gaps_{date}.csv"
    if logical == quality_root + "l2_gaps.json":
        return "quality/l2_gaps.json"
    if logical.startswith("raw_rfq/"):
        return logical
    raise ReferenceManifestError(f"logical_key is outside the allowlist: {logical}")


def _classify_object(item: dict, release_date: str) -> tuple[str, str | None]:
    """Validate canonical key/logical-key agreement and return local mapping."""
    logical = item["logical_key"]
    source = item["source_key"]
    kind = item["kind"]
    channel = item["channel"]

    def exact_prefix(logical_prefix: str, source_prefix: str) -> bool:
        return logical.startswith(logical_prefix) and source == (
            source_prefix + logical[len(logical_prefix) :]
        )

    if exact_prefix("warehouse/facts/", "ec2/warehouse/facts/"):
        rel = logical[len("warehouse/facts/") :]
        table = rel.split("/", 1)[0]
        if kind != "facts" or channel != table or table not in {
            "orderbooks_l1",
            "orderbooks_full",
            "trades",
        }:
            raise ReferenceManifestError(
                f"facts kind/channel is invalid for {logical}"
            )
        if f"date={release_date}/" not in "/" + rel:
            raise ReferenceManifestError(f"facts object is not date-bound: {logical}")
        return _local_key(logical, release_date), None

    if exact_prefix("warehouse/dim/snapshots/", "ec2/warehouse/dim/snapshots/"):
        allowed = {
            f"warehouse/dim/snapshots/date={release_date}/series.csv",
            f"warehouse/dim/snapshots/date={release_date}/events.csv",
            f"warehouse/dim/snapshots/date={release_date}/markets.csv",
        }
        if logical not in allowed or kind != "dim_snapshot" or channel not in (
            None,
            "dim",
        ):
            raise ReferenceManifestError(f"dim snapshot contract is invalid: {logical}")
        return _local_key(logical, release_date), None

    if exact_prefix("warehouse/catalog/", "ec2/warehouse/catalog/"):
        rel = logical[len("warehouse/catalog/") :]
        if rel not in {
            "series/part-00000.parquet",
            "events/part-00000.parquet",
            "markets/part-00000.parquet",
            "settlements/part-00000.parquet",
            "series_classified/part-00000.parquet",
        } or kind != "catalog" or channel not in (None, "catalog"):
            raise ReferenceManifestError(f"catalog contract is invalid: {logical}")
        return _local_key(logical, release_date), None

    if logical == f"warehouse/seals/date={release_date}.json":
        if (
            source != f"ec2/warehouse/seals/date={release_date}.json"
            or kind != "seal"
            or channel not in (None, "seal")
        ):
            raise ReferenceManifestError("source seal object contract is invalid")
        return _local_key(logical, release_date), "seal"

    if logical == "warehouse/manifest.csv":
        expected = (
            f"ec2/warehouse/publication-snapshots/v1/date={release_date}/"
            f"manifest/sha256={item['sha256']}/manifest.csv"
        )
        if (
            source != expected
            or kind != "warehouse_manifest_day"
            or channel is not None
        ):
            raise ReferenceManifestError("warehouse manifest projection is invalid")
        return _local_key(logical, release_date), None

    if logical.startswith("warehouse/corrections/"):
        allowed_logical = {
            f"warehouse/corrections/date={release_date}/late_rows.ndjson",
            f"warehouse/corrections/date={release_date}/ledger_day.ndjson",
        }
        if logical not in allowed_logical or channel is not None:
            raise ReferenceManifestError(f"correction contract is invalid: {logical}")
        if logical.endswith("late_rows.ndjson"):
            expected = (
                f"ec2/warehouse/publication-snapshots/v1/date={release_date}/"
                f"corrections/late_rows/sha256={item['sha256']}/"
                "late_rows.ndjson"
            )
            valid = (
                source == expected
                and kind == "correction"
            )
        else:
            expected = (
                f"ec2/warehouse/publication-snapshots/v1/date={release_date}/"
                f"corrections/ledger_day/sha256={item['sha256']}/"
                "ledger_day.ndjson"
            )
            valid = (
                source == expected
                and kind == "corrections_ledger_day"
            )
        if not valid:
            raise ReferenceManifestError(f"correction source is invalid: {logical}")
        return _local_key(logical, release_date), None

    quality_root = f"control/quality/v1/date={release_date}/"
    if logical in {
        quality_root + "capture_gap_receipt.json",
        quality_root + "capture_gaps.csv",
        quality_root + "l2_gaps.json",
    }:
        if logical.endswith("capture_gap_receipt.json"):
            expected_kind, stem, filename = (
                "capture_gap_receipt", "capture_gap_receipt",
                "capture_gap_receipt.json")
        elif logical.endswith("capture_gaps.csv"):
            expected_kind, stem, filename = (
                "capture_gaps_projection", "capture_gaps", "capture_gaps.csv")
        else:
            expected_kind, stem, filename = (
                "l2_quality_receipt", "l2_gaps", "l2_gaps.json")
        expected_source = (
            f"ec2/{quality_root}{stem}/sha256={item['sha256']}/{filename}"
        )
        if source != expected_source:
            raise ReferenceManifestError(f"quality source mismatch: {logical}")
        if kind != expected_kind or channel is not None:
            raise ReferenceManifestError(f"quality kind/channel mismatch: {logical}")
        return _local_key(logical, release_date), None

    rfq_match = RAW_RFQ_LOGICAL_RE.match(logical)
    if rfq_match:
        raw_date, basename = rfq_match.groups()
        _date(raw_date, "RFQ path date")
        if (
            not RFQ_BASENAME_RE.match(basename)
            or source != "ec2/raw/" + logical[len("raw_rfq/") :]
            or kind != "rfq"
            or channel != "rfq"
            or item["date"] != raw_date
            or item["required"] is not False
        ):
            raise ReferenceManifestError(f"RFQ sealed-only contract is invalid: {logical}")
        return logical, "rfq"

    raise ReferenceManifestError(f"source key is outside canonical allowlist: {source}")


def _normalize_source_seal(value: object, release_date: str) -> dict:
    if not isinstance(value, dict):
        raise ReferenceManifestError("source_seal is missing")
    required = {"bucket", "key", "version_id", "size", "sha256", "verification_state"}
    if required - set(value):
        raise ReferenceManifestError("source_seal is incomplete")
    seal = {
        "bucket": value["bucket"],
        "key": _safe_key(value["key"], "source_seal.key"),
        "version_id": _version(value["version_id"], "source_seal.version_id"),
        "size": _size(value["size"], "source_seal"),
        "sha256": _sha(value["sha256"], "source_seal.sha256"),
        "verification_state": value["verification_state"],
    }
    if (
        seal["bucket"] != TRUSTED_BUCKET
        or seal["key"] != f"ec2/warehouse/seals/date={release_date}.json"
        or seal["verification_state"] != "PASS"
    ):
        raise ReferenceManifestError("source_seal is not a verified canonical seal")
    return seal


def _normalize_receipt_object(value: object, release_date: str,
                              receipt_set_sha: str) -> dict:
    if not isinstance(value, dict) or set(value) != {
            "bucket", "key", "version_id", "size", "sha256"}:
        raise ReferenceManifestError("canonical receipt_object is incomplete")
    expected_key = (
        f"ec2/control/canonical-receipts/v1/date={release_date}/"
        f"receipt-{receipt_set_sha}.json"
    )
    normalized = {
        "bucket": value["bucket"],
        "key": _safe_key(value["key"], "canonical_receipt.receipt_object.key"),
        "version_id": _version(
            value["version_id"],
            "canonical_receipt.receipt_object.version_id"),
        "size": _size(value["size"], "canonical_receipt.receipt_object"),
        "sha256": _sha(
            value["sha256"], "canonical_receipt.receipt_object.sha256"),
    }
    if normalized["size"] <= 0 or normalized["size"] > \
            MAX_DURABLE_RECEIPT_BYTES:
        raise ReferenceManifestError(
            "canonical receipt_object exceeds the fixed size bound")
    if normalized["bucket"] != TRUSTED_BUCKET or normalized["key"] != expected_key:
        raise ReferenceManifestError(
            "canonical receipt_object is outside the fixed control path")
    return normalized


def validate_manifest(manifest: object, requested_release_id: str | None = None) -> dict:
    """Validate v3 and return a normalized, transport-neutral descriptor."""
    if not isinstance(manifest, dict):
        raise ReferenceManifestError("manifest root is not an object")
    if manifest.get("schema") != SCHEMA or manifest.get("schema_version") != 3:
        raise ReferenceManifestError("wrong v3 schema")
    if manifest.get("storage_mode") != STORAGE_MODE:
        raise ReferenceManifestError("wrong v3 storage_mode")
    if manifest.get("publication_status") != "PUBLISHED":
        raise ReferenceManifestError("reference release is not PUBLISHED")
    _utc(manifest.get("published_at_utc"), "published_at_utc")
    if not isinstance(manifest.get("publisher_commit"), str) or not manifest["publisher_commit"]:
        raise ReferenceManifestError("publisher_commit is missing")

    release_id = _safe_key(manifest.get("release_id"), "release_id")
    match = RELEASE_RE.match(release_id)
    if not match:
        raise ReferenceManifestError("release_id is not the v3 reference form")
    if requested_release_id is not None and requested_release_id != release_id:
        raise ReferenceManifestError("manifest release_id differs from requested release")
    release_date = _date(manifest.get("date"), "manifest date")
    if match.group(1) != release_date:
        raise ReferenceManifestError("release_id date differs from manifest date")

    source_seal = _normalize_source_seal(manifest.get("source_seal"), release_date)
    if match.group(2) != source_seal["sha256"][:8]:
        raise ReferenceManifestError("release_id seal prefix mismatch")

    raw_objects = manifest.get("objects")
    if not isinstance(raw_objects, list) or not raw_objects:
        raise ReferenceManifestError("objects is empty or not a list")
    objects: list[dict] = []
    seen_logical: set[str] = set()
    seen_local: set[str] = set()
    seen_physical: set[tuple[str, str, str]] = set()
    seal_rows = []
    rfq_rows = []
    for index, raw in enumerate(raw_objects):
        if not isinstance(raw, dict):
            raise ReferenceManifestError(f"object {index} is not an object")
        required_fields = {
            "logical_key",
            "source_bucket",
            "source_key",
            "source_version_id",
            "size",
            "sha256",
            "kind",
            "channel",
            "date",
            "required",
            "seal_binding",
            "evidence_binding",
        }
        if required_fields - set(raw):
            raise ReferenceManifestError(f"object {index} is incomplete")
        unknown_fields = set(raw) - required_fields - {"source_last_modified_utc"}
        if unknown_fields:
            raise ReferenceManifestError(
                f"object {index} has unsupported fields: {sorted(unknown_fields)}"
            )
        logical = _safe_key(raw["logical_key"], f"object {index} logical_key")
        if logical in seen_logical:
            raise ReferenceManifestError(f"duplicate logical_key: {logical}")
        seen_logical.add(logical)
        source_bucket = raw["source_bucket"]
        if source_bucket != TRUSTED_BUCKET:
            raise ReferenceManifestError(f"untrusted source bucket: {source_bucket!r}")
        source_key = _safe_key(raw["source_key"], f"object {index} source_key")
        obj_date = _date(raw["date"], f"object {index} date")
        if not isinstance(raw["required"], bool):
            raise ReferenceManifestError(f"object {index} required is not boolean")
        item = {
            "logical_key": logical,
            "source_bucket": source_bucket,
            "source_key": source_key,
            "source_version_id": _version(
                raw["source_version_id"], f"object {index} source_version_id"
            ),
            "size": _size(raw["size"], f"object {index}"),
            "sha256": _sha(raw["sha256"], f"object {index} sha256"),
            "kind": raw["kind"],
            "channel": raw["channel"],
            "date": obj_date,
            "required": raw["required"],
            "seal_binding": _binding(raw["seal_binding"], f"object {index} seal_binding"),
            "evidence_binding": _binding(
                raw["evidence_binding"], f"object {index} evidence_binding"
            ),
        }
        if "source_last_modified_utc" in raw:
            item["source_last_modified_utc"] = _utc(
                raw["source_last_modified_utc"],
                f"object {index} source_last_modified_utc",
            )
        physical = (
            source_bucket,
            source_key,
            item["source_version_id"],
        )
        if physical in seen_physical:
            raise ReferenceManifestError(
                f"duplicate canonical exact version: {source_bucket}/{source_key}"
            )
        seen_physical.add(physical)
        local_key, special = _classify_object(item, release_date)
        if special == "rfq":
            item["evidence_binding"] = _normalize_rfq_eligibility_binding(
                item["evidence_binding"], source_seal["sha256"])
        if special == "rfq" and "DATA_INTEGRITY_BLOCKED" in \
                canonical_bytes(item).decode("ascii").upper():
            raise ReferenceManifestError(
                f"RFQ object carries blocked/quarantined semantics: {logical}"
            )
        if item["kind"] == "dim_snapshot":
            if item["seal_binding"] is not None or item["evidence_binding"] is not None:
                raise ReferenceManifestError(
                    f"dated dim snapshot carries an unsupported binding: {logical}"
                )
        elif item["seal_binding"] != source_seal["sha256"]:
            raise ReferenceManifestError(
                f"object is bound to a different seal: {logical}"
            )
        elif item["evidence_binding"] in (None, "", (), [], {}):
            raise ReferenceManifestError(f"object evidence binding is missing: {logical}")
        if special != "rfq" and obj_date != release_date:
            raise ReferenceManifestError(f"object date differs from release date: {logical}")
        if special != "rfq" and item["required"] is not True:
            raise ReferenceManifestError(f"non-RFQ object is not required: {logical}")
        if local_key in seen_local:
            raise ReferenceManifestError(f"duplicate local logical path: {local_key}")
        seen_local.add(local_key)
        item["local_key"] = local_key
        objects.append(item)
        if special == "seal":
            seal_rows.append(item)
        elif special == "rfq":
            rfq_rows.append(item)

    if len(seal_rows) != 1:
        raise ReferenceManifestError("manifest must contain exactly one source seal object")
    seal_row = seal_rows[0]
    for source_field, object_field in (
        ("bucket", "source_bucket"),
        ("key", "source_key"),
        ("version_id", "source_version_id"),
        ("size", "size"),
        ("sha256", "sha256"),
    ):
        if source_seal[source_field] != seal_row[object_field]:
            raise ReferenceManifestError("source_seal and seal object disagree")

    rfq_included = manifest.get("rfq_included")
    if not isinstance(rfq_included, bool):
        raise ReferenceManifestError("rfq_included is not boolean")
    if manifest.get("rfq_policy") != RFQ_POLICY:
        raise ReferenceManifestError("rfq_policy is not OPTIONAL_SEALED_ONLY")
    if bool(rfq_rows) != rfq_included:
        raise ReferenceManifestError("RFQ object set disagrees with rfq_included")
    if rfq_rows and len({
            canonical_sha256(item["evidence_binding"])
            for item in rfq_rows}) != 1:
        raise ReferenceManifestError(
            "RFQ objects do not share one sealed set eligibility binding")

    logicals = {item["logical_key"] for item in objects}
    required_logicals = {
        f"warehouse/seals/date={release_date}.json",
        "warehouse/manifest.csv",
        f"warehouse/dim/snapshots/date={release_date}/series.csv",
        f"warehouse/dim/snapshots/date={release_date}/events.csv",
        f"warehouse/dim/snapshots/date={release_date}/markets.csv",
        "warehouse/catalog/series/part-00000.parquet",
        "warehouse/catalog/events/part-00000.parquet",
        "warehouse/catalog/markets/part-00000.parquet",
        f"control/quality/v1/date={release_date}/capture_gap_receipt.json",
    }
    missing = sorted(required_logicals - logicals)
    if missing:
        raise ReferenceManifestError(
            f"required canonical research families are incomplete: {missing}"
        )
    fact_tables = {
        item["channel"] for item in objects if item["kind"] == "facts"
    }
    if not fact_tables:
        raise ReferenceManifestError("reference release contains no sealed facts")
    l2_receipt = (
        f"control/quality/v1/date={release_date}/l2_gaps.json" in logicals
    )
    if ("orderbooks_full" in fact_tables) != l2_receipt:
        raise ReferenceManifestError(
            "L2 facts and the affirmative L2 quality receipt must appear together"
        )
    correction_keys = {
        item["logical_key"]
        for item in objects
        if item["kind"] in {"correction", "corrections_ledger_day"}
    }
    correction_pair = {
        f"warehouse/corrections/date={release_date}/late_rows.ndjson",
        f"warehouse/corrections/date={release_date}/ledger_day.ndjson",
    }
    if correction_keys not in (set(), correction_pair):
        raise ReferenceManifestError("correction reference pair is incomplete")

    got_reference = reference_set_sha256(objects)
    if _sha(manifest.get("reference_set_sha256"), "reference_set_sha256") != got_reference:
        raise ReferenceManifestError("reference_set_sha256 mismatch")
    got_semantics = object_semantics_sha256(objects)
    if _sha(manifest.get("object_semantics_sha256"), "object_semantics_sha256") != got_semantics:
        raise ReferenceManifestError("object_semantics_sha256 mismatch")

    state = manifest.get("publication_state")
    if not isinstance(state, dict):
        raise ReferenceManifestError("publication_state is missing")
    state_fields = {
        "schema",
        "storage_mode",
        "date",
        "source_seal_binding_sha256",
        "reference_set_sha256",
        "object_semantics_sha256",
        "evidence_tier",
        "evidence_basis_sha256",
        "corrections_digest",
        "gap_evidence_digest",
        "l2_quality_digest",
        "tl1_status",
        "rfq_policy",
        "rfq_included",
        "canonical_receipt_set_sha256",
        "canonical_receipt_binding_sha256",
    }
    if set(state) != state_fields:
        raise ReferenceManifestError(
            "publication_state fields differ from the v3 contract"
        )
    state_sha = canonical_sha256(state)
    if _sha(manifest.get("publication_state_sha256"), "publication_state_sha256") != state_sha:
        raise ReferenceManifestError("publication_state_sha256 mismatch")
    if match.group(3) != state_sha[:16]:
        raise ReferenceManifestError("release_id publication-state suffix mismatch")
    fixed_state = {
        "schema": SCHEMA,
        "storage_mode": STORAGE_MODE,
        "date": release_date,
        "source_seal_binding_sha256": source_seal["sha256"],
        "reference_set_sha256": got_reference,
        "object_semantics_sha256": got_semantics,
        "tl1_status": manifest.get("tl1_status"),
        "rfq_policy": RFQ_POLICY,
        "rfq_included": rfq_included,
    }
    for key, expected in fixed_state.items():
        if state.get(key) != expected:
            raise ReferenceManifestError(f"publication_state {key} mismatch")
    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("tier"), str):
        raise ReferenceManifestError("evidence tier/basis is missing")
    if "basis" not in evidence:
        raise ReferenceManifestError("evidence basis is missing")
    if state.get("evidence_tier") != evidence["tier"]:
        raise ReferenceManifestError("publication_state evidence tier mismatch")
    if state.get("evidence_basis_sha256") != canonical_sha256(evidence["basis"]):
        raise ReferenceManifestError("publication_state evidence basis mismatch")
    if (manifest.get("evidence_tier") != evidence["tier"]
            or manifest.get("evidence_tier_basis") != evidence["basis"]):
        raise ReferenceManifestError("legacy evidence aliases disagree with v3 evidence")
    if rfq_included and evidence["tier"] != "SEALED_CONFIRMATION":
        raise ReferenceManifestError(
            "RFQ references require SEALED_CONFIRMATION evidence")

    canonical_receipt = manifest.get("canonical_receipt")
    if not isinstance(canonical_receipt, dict):
        raise ReferenceManifestError("canonical_receipt is missing")
    receipt_fixed = {
        "schema_version": "canonical-object-receipt-v1",
        "state": "DURABLE_RECEIPT_VERIFIED",
        "authority": "CANONICAL_CONTROL_PLANE",
        "authoritative": True,
        "s3_published": True,
        "prune_eligible": False,
    }
    for key, expected in receipt_fixed.items():
        if canonical_receipt.get(key) != expected:
            raise ReferenceManifestError(f"canonical_receipt {key} mismatch")
    receipt_set_sha = _sha(
        canonical_receipt.get("receipt_set_sha256"),
        "canonical_receipt.receipt_set_sha256",
    )
    if state.get("canonical_receipt_set_sha256") != receipt_set_sha:
        raise ReferenceManifestError("publication_state receipt binding mismatch")
    receipt_object = _normalize_receipt_object(
        canonical_receipt.get("receipt_object"), release_date, receipt_set_sha)
    if receipt_object["bucket"] != source_seal["bucket"]:
        raise ReferenceManifestError("receipt/seal canonical buckets differ")
    if state.get("canonical_receipt_binding_sha256") != canonical_sha256(
            receipt_object):
        raise ReferenceManifestError(
            "publication_state canonical receipt object binding mismatch")

    publication_components = manifest.get("publication_components")
    if (not isinstance(publication_components, dict)
            or set(publication_components)
            != {"seal_sha256", "corrections", "gap_evidence", "rfq"}):
        raise ReferenceManifestError("publication_components is incomplete")
    if publication_components.get("seal_sha256") != source_seal["sha256"]:
        raise ReferenceManifestError("publication_components seal mismatch")
    component_corrections = publication_components.get("corrections")
    component_gap = publication_components.get("gap_evidence")
    component_rfq = publication_components.get("rfq")
    if (not isinstance(component_corrections, dict)
            or not isinstance(component_gap, dict)
            or not isinstance(component_rfq, dict)):
        raise ReferenceManifestError("publication_components blocks are malformed")
    if state.get("corrections_digest") != canonical_sha256(
            component_corrections):
        raise ReferenceManifestError("publication component corrections mismatch")
    if state.get("gap_evidence_digest") != canonical_sha256(component_gap):
        raise ReferenceManifestError("publication component gap evidence mismatch")
    if (component_rfq.get("included") is not rfq_included
            or not isinstance(component_rfq.get("files"), list)):
        raise ReferenceManifestError("publication component RFQ switch mismatch")
    component_rfq_files = sorted(component_rfq["files"], key=lambda row: (
        row.get("file", "") if isinstance(row, dict) else ""))
    expected_rfq_files = sorted(({
        "file": item["logical_key"][len("raw_rfq/"):],
        "size": item["size"],
        "sha256": item["sha256"],
    } for item in rfq_rows), key=lambda row: row["file"])
    if component_rfq_files != expected_rfq_files:
        raise ReferenceManifestError("publication component RFQ files mismatch")

    by_kind = {item["kind"]: item for item in objects
               if item["kind"] in {
                   "capture_gap_receipt", "capture_gaps_projection",
                   "l2_quality_receipt",
                   "corrections_ledger_day"}}
    gap_receipt = by_kind["capture_gap_receipt"]
    gap_projection = by_kind.get("capture_gaps_projection")
    l2_receipt = by_kind.get("l2_quality_receipt")
    if (component_gap.get("affirmative_receipt") is not True
            or component_gap.get("receipt_inventory_matched_seal") is not True
            or component_gap.get("non_affirmative_reason") is not None
            or component_gap.get("gap_receipt_sha256") != gap_receipt["sha256"]
            or component_gap.get("capture_gaps_sha256")
            != (gap_projection["sha256"] if gap_projection else None)
            or component_gap.get("l2_gaps_sha256")
            != (l2_receipt["sha256"] if l2_receipt else None)):
        raise ReferenceManifestError("publication component quality bindings mismatch")

    correction_files = component_corrections.get("files")
    if not isinstance(correction_files, list):
        raise ReferenceManifestError("publication component corrections files invalid")
    expected_correction_rows = sorted(({
        "key": item["local_key"], "size": item["size"],
        "sha256": item["sha256"],
    } for item in objects if item["kind"] == "correction"),
        key=lambda row: row["key"])
    if sorted(correction_files, key=lambda row: (
            row.get("key", "") if isinstance(row, dict) else "")) \
            != expected_correction_rows:
        raise ReferenceManifestError("publication component correction files mismatch")
    ledger_obj = by_kind.get("corrections_ledger_day")
    if component_corrections.get("ledger_day_sha256") != (
            ledger_obj["sha256"] if ledger_obj else None):
        raise ReferenceManifestError("publication component ledger binding mismatch")

    corrections = manifest.get("corrections")
    if not isinstance(corrections, dict):
        raise ReferenceManifestError("corrections block is missing")
    corrections_digest = _sha(
        corrections.get("digest"), "corrections.digest"
    )
    if state.get("corrections_digest") != corrections_digest:
        raise ReferenceManifestError("publication_state corrections digest mismatch")
    expected_correction_files = 1 if correction_keys else 0
    if corrections.get("included_files") != expected_correction_files:
        raise ReferenceManifestError("corrections included_files mismatch")
    ledger_entries = corrections.get("ledger_day_entries")
    if (not isinstance(ledger_entries, int) or isinstance(ledger_entries, bool)
            or ledger_entries < 0
            or (not correction_keys and ledger_entries != 0)):
        raise ReferenceManifestError("corrections ledger_day_entries is invalid")

    channels = manifest.get("channels")
    if not isinstance(channels, dict):
        raise ReferenceManifestError("channels is not an object")
    required_channels = {"orderbooks_l1", "trades", "orderbooks_l2", "rfq"}
    if set(channels) != required_channels or any(
        not isinstance(channels[name], dict) for name in required_channels
    ):
        raise ReferenceManifestError("channels differs from the v3 contract")
    expected_channel_status = {
        "orderbooks_l1": (
            "INCLUDED" if "orderbooks_l1" in fact_tables
            else "ABSENT_FROM_THIS_RELEASE"
        ),
        "trades": (
            "INCLUDED" if "trades" in fact_tables
            else "ABSENT_FROM_THIS_RELEASE"
        ),
        "orderbooks_l2": (
            "INCLUDED_SEALED_FACTS" if "orderbooks_full" in fact_tables
            else "ABSENT_FROM_THIS_RELEASE"
        ),
        "rfq": (
            "INCLUDED_SEALED_REFERENCE" if rfq_included
            else "EXCLUDED_EXPLICIT_OPT_IN_REQUIRED"
        ),
    }
    for name, expected in expected_channel_status.items():
        if channels[name].get("status") != expected:
            raise ReferenceManifestError(f"channel {name} status mismatch")
    _sha(state.get("gap_evidence_digest"), "gap_evidence_digest")
    l2_quality = manifest.get("l2_quality")
    if l2_quality != channels["orderbooks_l2"].get("seq_quality"):
        raise ReferenceManifestError("top-level L2 quality alias mismatch")
    if state.get("l2_quality_digest") != canonical_sha256(l2_quality):
        raise ReferenceManifestError("publication_state L2 quality digest mismatch")

    tables = manifest.get("tables")
    if not isinstance(tables, dict):
        raise ReferenceManifestError("tables is not an object")
    if set(tables) != fact_tables:
        raise ReferenceManifestError("tables inventory differs from fact channels")
    for table, frozen in tables.items():
        if (not isinstance(frozen, dict)
                or not isinstance(frozen.get("columns"), list)
                or not all(isinstance(column, str) and column
                           for column in frozen["columns"])):
            raise ReferenceManifestError(f"table schema is invalid: {table}")

    seal = manifest.get("seal")
    if not isinstance(seal, dict):
        raise ReferenceManifestError("seal summary is missing")
    if (seal.get("sha256") != source_seal["sha256"]
            or seal.get("status") != "SEALED"):
        raise ReferenceManifestError("seal summary differs from source_seal")
    _sha(seal.get("manifest_date_sha256"), "seal.manifest_date_sha256")

    binding = manifest.get("version_binding")
    if not isinstance(binding, dict) or binding != {
        "mode": STORAGE_MODE,
        "bindings_sha256": got_reference,
        "versioning_requirement": "VERSIONING_REQUIRED",
    }:
        raise ReferenceManifestError("version_binding is not exact-reference mode")
    post_upload = manifest.get("post_upload_verification")
    if (not isinstance(post_upload, dict)
            or post_upload.get("objects_verified") != len(objects)
            or post_upload.get("data_objects_uploaded") != 0):
        raise ReferenceManifestError("post_upload_verification is inconsistent")
    if manifest.get("tl1_status") not in ("TL1", "PRE-TL1", "MIXED"):
        raise ReferenceManifestError("tl1_status is invalid")

    return {
        "schema": SCHEMA,
        "storage_mode": STORAGE_MODE,
        "release_id": release_id,
        "date": release_date,
        "published_at_utc": manifest["published_at_utc"],
        "source_seal": source_seal,
        "reference_set_sha256": got_reference,
        "object_semantics_sha256": got_semantics,
        "publication_state_sha256": state_sha,
        "evidence_tier": evidence["tier"],
        "evidence_basis": evidence["basis"],
        "tl1_status": manifest["tl1_status"],
        "rfq_included": rfq_included,
        "objects": sorted(objects, key=lambda item: item["logical_key"]),
        "tables": manifest.get("tables") or {},
        "channels": channels,
        "corrections": corrections,
        "seal": seal,
        "canonical_receipt_set_sha256": receipt_set_sha,
        "receipt_object": receipt_object,
        "publication_components": publication_components,
        "l2_quality": l2_quality,
    }


def is_reference_manifest(manifest: object) -> bool:
    return isinstance(manifest, dict) and (
        manifest.get("schema") == SCHEMA
        or manifest.get("storage_mode") == STORAGE_MODE
        or manifest.get("schema_version") == 3
    )


_RECEIPT_SEAL_FIELDS = (
    "date", "status", "version", "method", "bucket", "key",
    "VersionId", "size", "sha256", "manifest_date_sha256",
)
_RECEIPT_FAMILY_FIELDS = (
    "name", "policy", "expected_basis", "expected_count",
    "observed_count", "state", "reason_code", "semantic_sha256",
    "objects_digest",
)
_RECEIPT_OBJECT_FIELDS = (
    "bucket", "key", "VersionId", "size", "sha256",
    "last_modified_utc", "logical_source_key", "source_kind", "family",
    "table", "channel", "date", "seal_binding", "evidence_binding",
    "durability_verified", "research_eligible", "eligibility_tag_state",
    "mutable_source", "required", "attestation_class", "durability_scope",
    "research_candidate", "exposure_policy", "version_resolution",
    "canonical_source",
)


def durable_receipt_projection(receipt: dict) -> dict:
    """Reproduce canonical_receipts.py's timestamp-free stable identity."""
    date = _date(receipt.get("date"), "durable receipt date")
    seal = receipt.get("seal")
    families = receipt.get("families")
    objects = receipt.get("objects")
    if (not isinstance(seal, dict) or not isinstance(families, list)
            or not isinstance(objects, list)):
        raise ReferenceManifestError("durable receipt inventory is malformed")
    projected_families = []
    seen_families = set()
    for family in families:
        if not isinstance(family, dict) or not isinstance(family.get("name"), str):
            raise ReferenceManifestError("durable receipt family is malformed")
        if family["name"] in seen_families:
            raise ReferenceManifestError("duplicate durable receipt family")
        seen_families.add(family["name"])
        projected_families.append({key: family.get(key)
                                   for key in _RECEIPT_FAMILY_FIELDS})
    projected_families.sort(key=lambda family: family["name"])

    projected_objects = []
    seen_physical = set()
    seen_logical = set()
    for row in objects:
        if not isinstance(row, dict):
            raise ReferenceManifestError("durable receipt object is malformed")
        physical = (row.get("bucket"), row.get("key"))
        logical = row.get("logical_source_key")
        if physical in seen_physical or logical in seen_logical:
            raise ReferenceManifestError("durable receipt contains duplicates")
        seen_physical.add(physical)
        seen_logical.add(logical)
        projected_objects.append({key: row.get(key)
                                  for key in _RECEIPT_OBJECT_FIELDS})
    projected_objects.sort(key=lambda row: (
        row["logical_source_key"], row["bucket"], row["key"]))
    return {
        "schema_version": "canonical-object-receipt-v1",
        "date": date,
        "seal": {key: seal.get(key) for key in _RECEIPT_SEAL_FIELDS},
        "families": projected_families,
        "objects": projected_objects,
    }


def _scoped_receipt_sha256(objects: list[dict], field: str) -> str:
    projection = [{
        "logical_source_key": row["logical_source_key"],
        "bucket": row["bucket"],
        "key": row["key"],
        "VersionId": row.get("VersionId"),
        "size": row["size"],
        "sha256": row["sha256"],
        "last_modified_utc": row.get("last_modified_utc"),
    } for row in objects if row.get(field) is True]
    projection.sort(key=lambda row: (
        row["logical_source_key"], row["bucket"], row["key"]))
    return canonical_sha256(projection)


def _is_receipt_rfq(row: dict) -> bool:
    return (row.get("channel") == "rfq"
            or row.get("source_kind") in {"raw_rfq", "raw_rfq_receipts"})


def _rfq_exact_set_sha256(objects: list[dict]) -> str:
    """Bind the seal-derived RFQ inventory to immutable S3 versions.

    This deliberately mirrors the stable exact-version projection used by
    scoped receipt digests, while selecting the whole RFQ inventory rather
    than only the currently research-candidate subset.
    """
    projection = [{
        "logical_source_key": row.get("logical_source_key"),
        "bucket": row.get("bucket"),
        "key": row.get("key"),
        "VersionId": row.get("VersionId"),
        "size": row.get("size"),
        "sha256": row.get("sha256"),
        "last_modified_utc": row.get("last_modified_utc"),
    } for row in objects if _is_receipt_rfq(row)]
    projection.sort(key=lambda row: (
        row["logical_source_key"], row["bucket"], row["key"],
        row["VersionId"]))
    return canonical_sha256(projection)


def validate_durable_receipt(receipt: object, descriptor: dict) -> dict:
    """Authenticate receipt_set and prove manifest refs equal its candidates."""
    if not isinstance(receipt, dict):
        raise ReferenceManifestError("durable receipt root is not an object")
    fixed = {
        "schema_version": "canonical-object-receipt-v1",
        "state": "DURABLE_RECEIPT_VERIFIED",
        "authority": "CANONICAL_CONTROL_PLANE",
        "authoritative": True,
        "s3_published": True,
        "prune_eligible": False,
        "date": descriptor["date"],
        "receipt_set_sha256": descriptor["canonical_receipt_set_sha256"],
    }
    for key, expected in fixed.items():
        if receipt.get(key) != expected:
            raise ReferenceManifestError(f"durable receipt {key} mismatch")
    objects = receipt.get("objects")
    families = receipt.get("families")
    if not isinstance(objects, list) or not objects or not isinstance(families, list):
        raise ReferenceManifestError("durable receipt object/family set is empty")
    got_set = canonical_sha256(durable_receipt_projection(receipt))
    if got_set != descriptor["canonical_receipt_set_sha256"]:
        raise ReferenceManifestError("durable receipt_set_sha256 does not recompute")
    for field, scope in (
            ("durability_set_sha256", "durability_scope"),
            ("research_candidate_set_sha256", "research_candidate")):
        if (_sha(receipt.get(field), f"durable receipt {field}")
                != _scoped_receipt_sha256(objects, scope)):
            raise ReferenceManifestError(f"durable receipt {field} mismatch")

    seal = receipt.get("seal")
    source_seal = descriptor["source_seal"]
    seal_expected = {
        "date": descriptor["date"],
        "status": "SEALED",
        "version": 2,
        "method": "full_v2",
        "bucket": source_seal["bucket"],
        "key": source_seal["key"],
        "VersionId": source_seal["version_id"],
        "size": source_seal["size"],
        "sha256": source_seal["sha256"],
        "manifest_date_sha256": descriptor["seal"]["manifest_date_sha256"],
    }
    if not isinstance(seal, dict) or any(
            seal.get(key) != expected for key, expected in seal_expected.items()):
        raise ReferenceManifestError("durable receipt seal differs from manifest")

    family_names = set()
    objects_by_family = {}
    for row in objects:
        if isinstance(row, dict):
            objects_by_family.setdefault(row.get("family"), []).append(row)
    for family in families:
        if (not isinstance(family, dict)
                or not isinstance(family.get("name"), str)
                or not family["name"]):
            raise ReferenceManifestError("durable receipt family is malformed")
        name = family["name"]
        if name in family_names:
            raise ReferenceManifestError("durable receipt family is duplicated")
        family_names.add(name)
        rows = objects_by_family.get(name, [])
        expected_count = family.get("expected_count")
        observed_count = family.get("observed_count")
        if (not isinstance(expected_count, int)
                or isinstance(expected_count, bool)
                or not isinstance(observed_count, int)
                or isinstance(observed_count, bool)
                or min(expected_count, observed_count) < 0
                or observed_count != len(rows)):
            raise ReferenceManifestError(
                "durable receipt family count is inconsistent")
        projection = [{
            "bucket": row.get("bucket"), "key": row.get("key"),
            "size": row.get("size"), "sha256": row.get("sha256"),
        } for row in rows]
        projection.sort(key=lambda row: (row["bucket"], row["key"]))
        if family.get("objects_digest") != canonical_sha256(projection):
            raise ReferenceManifestError(
                "durable receipt family objects_digest mismatch")
        state = family.get("state")
        if state not in {"PRESENT_VERIFIED", "NOT_APPLICABLE"}:
            raise ReferenceManifestError("durable receipt has a blocking family")
        if ((state == "PRESENT_VERIFIED"
             and expected_count != observed_count)
                or (state == "NOT_APPLICABLE"
                    and (expected_count != 0 or observed_count != 0))):
            raise ReferenceManifestError(
                "durable receipt family state/count is inconsistent")
        if (family.get("policy") in {"REQUIRED_CORE", "REQUIRED_RESEARCH"}
                and state != "PRESENT_VERIFIED"):
            raise ReferenceManifestError("durable receipt required family failed")
    if set(objects_by_family) != family_names:
        raise ReferenceManifestError(
            "durable receipt object names an unregistered family")

    selected = {}
    receipt_rfq = []
    receipt_rfq_references = {}
    rfq_eligibility_bindings = []
    for index, row in enumerate(objects):
        label = f"durable receipt object {index}"
        if not isinstance(row, dict):
            raise ReferenceManifestError(f"{label} is malformed")
        bucket = row.get("bucket")
        key = _safe_key(row.get("key"), f"{label}.key")
        logical = _safe_key(row.get("logical_source_key"), f"{label}.logical")
        version_id = _version(row.get("VersionId"), f"{label}.VersionId")
        size = _size(row.get("size"), label)
        digest = _sha(row.get("sha256"), f"{label}.sha256")
        object_date = _date(row.get("date"), f"{label}.date")
        _utc(row.get("last_modified_utc"), f"{label}.last_modified_utc")
        if bucket != TRUSTED_BUCKET:
            raise ReferenceManifestError(f"{label} crosses canonical buckets")
        if row.get("family") not in family_names:
            raise ReferenceManifestError(f"{label} names an unknown family")
        if (row.get("durability_verified") is not True
                or row.get("verification_state") != "EXACT_VERSION_FULL_SHA256"):
            raise ReferenceManifestError(f"{label} lacks exact-version verification")
        if (not isinstance(row.get("research_candidate"), bool)
                or not isinstance(row.get("research_eligible"), bool)):
            raise ReferenceManifestError(
                f"{label} has invalid research eligibility booleans")

        is_rfq = _is_receipt_rfq(row)
        manifest_logical = logical
        expected_kind = row.get("source_kind")
        if is_rfq:
            if (row.get("channel") != "rfq"
                    or row.get("source_kind") not in {
                        "raw_rfq", "raw_rfq_receipts"}
                    or not logical.startswith("raw/")):
                raise ReferenceManifestError(
                    "receipt RFQ kind/channel/logical path is invalid")
            manifest_logical = "raw_rfq/" + logical[len("raw/"):]
            match = RAW_RFQ_LOGICAL_RE.match(manifest_logical)
            if match is None or RFQ_BASENAME_RE.match(match.group(2)) is None:
                raise ReferenceManifestError("receipt RFQ name is invalid")
            expected_source_kind = (
                "raw_rfq_receipts" if match.group(2).startswith(
                    "rfq_receipts_") else "raw_rfq")
            if (row.get("source_kind") != expected_source_kind
                    or object_date != match.group(1)
                    or key != "ec2/raw/" + logical[len("raw/"):]
                    or row.get("required") is not False
                    or row.get("seal_binding") != source_seal["sha256"]):
                raise ReferenceManifestError(
                    "receipt RFQ is not the sealed optional raw contract")
            expected_kind = "rfq"
            if manifest_logical in receipt_rfq_references:
                raise ReferenceManifestError(
                    "receipt RFQ inventory maps ambiguously")
            receipt_rfq.append(row)
            receipt_rfq_references[manifest_logical] = {
                "logical_key": manifest_logical,
                "source_bucket": bucket,
                "source_key": key,
                "source_version_id": version_id,
                "size": size,
                "sha256": digest,
                "kind": expected_kind,
                "channel": row.get("channel"),
                "date": object_date,
                "required": row.get("required"),
                "seal_binding": row.get("seal_binding"),
                "evidence_binding": row.get("evidence_binding"),
                "source_last_modified_utc": row.get("last_modified_utc"),
            }
        if row.get("research_candidate") is not True:
            if row.get("research_eligible") is not False:
                raise ReferenceManifestError(
                    f"{label} is eligible without being a candidate")
            continue
        if is_rfq:
            binding = _normalize_rfq_eligibility_binding(
                row.get("evidence_binding"), source_seal["sha256"])
            if (row.get("research_eligible") is not True
                    or row.get("eligibility_tag_state")
                    != "DUAL_TAGGED_VERIFIED"
                    or row.get("exposure_policy")
                    != "RESEARCH_ELIGIBLE_SEALED_RFQ"):
                raise ReferenceManifestError(
                    f"{label} is not dual-tagged sealed RFQ eligible")
            rfq_eligibility_bindings.append(binding)
            if not descriptor["rfq_included"]:
                continue
        else:
            if (row.get("research_eligible") is not True
                    or row.get("eligibility_tag_state") != "TAGGED_VERIFIED"
                    or str(row.get("exposure_policy") or "").startswith(
                        "FORBIDDEN")):
                raise ReferenceManifestError(
                    f"{label} is not research eligible")
            if logical.startswith("raw/"):
                raise ReferenceManifestError("non-RFQ raw became research eligible")
        if manifest_logical in selected:
            raise ReferenceManifestError("receipt candidates map ambiguously")
        selected[manifest_logical] = {
            "logical_key": manifest_logical,
            "source_bucket": bucket,
            "source_key": key,
            "source_version_id": version_id,
            "size": size,
            "sha256": digest,
            "kind": expected_kind,
            "channel": row.get("channel"),
            "date": row.get("date"),
            "required": row.get("required"),
            "seal_binding": row.get("seal_binding"),
            "evidence_binding": row.get("evidence_binding"),
            "source_last_modified_utc": row.get("last_modified_utc"),
        }

    rfq_exact_set_sha = _rfq_exact_set_sha256(receipt_rfq)
    if any(binding["rfq_exact_set_sha256"] != rfq_exact_set_sha
           for binding in rfq_eligibility_bindings):
        raise ReferenceManifestError(
            "RFQ eligibility binding exact set digest mismatch")

    manifested = {row["logical_key"]: row for row in descriptor["objects"]}
    manifested_rfq = {
        logical: row for logical, row in manifested.items()
        if row.get("kind") == "rfq"}
    if descriptor["rfq_included"] and \
            set(manifested_rfq) != set(receipt_rfq_references):
        raise ReferenceManifestError(
            "manifest RFQ inventory does not equal the sealed receipt RFQ set")
    if set(selected) != set(manifested):
        raise ReferenceManifestError(
            "manifest references do not equal durable receipt candidates")
    compare_fields = (
        "logical_key", "source_bucket", "source_key", "source_version_id",
        "size", "sha256", "kind", "channel", "date", "required",
        "seal_binding", "evidence_binding",
    )
    for logical, expected in selected.items():
        actual = manifested[logical]
        if any(actual.get(field) != expected.get(field)
               for field in compare_fields):
            raise ReferenceManifestError(
                f"manifest reference differs from durable receipt: {logical}")
        if (expected.get("source_last_modified_utc") is not None
                and actual.get("source_last_modified_utc")
                != expected["source_last_modified_utc"]):
            raise ReferenceManifestError(
                f"manifest LastModified differs from durable receipt: {logical}")
    return {"receipt_set_sha256": got_set, "objects": len(objects),
            "research_candidates": len(selected)}
