#!/usr/bin/env python3
"""Build a deterministic, local-only catalog of verified V3 research data.

The catalog never inventories S3 and never copies or hashes large fact files.
It accepts only atomically materialized ``REFERENCE_V3`` releases whose local
``.VERIFIED.json`` marker still binds the frozen manifest and whose expected
objects remain content-addressed symlinks.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import research_reference as reference  # noqa: E402


SCHEMA = "research-data-catalog-v1"
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_MARKER_BYTES = 4 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CHANNEL_FAMILIES = {
    "orderbooks_l1": "L1",
    "orderbooks_full": "L2",
    "orderbooks_l2": "L2",
    "trades": "TRADES",
    "rfq": "RFQ",
}


class DataCatalogError(RuntimeError):
    """A local release claimed verification but failed a stable contract."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _read_json(path: Path, limit: int, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise DataCatalogError("UNSAFE_LOCAL_CONTROL", f"{label} is not a regular file")
    size = path.stat().st_size
    if size <= 0 or size > limit:
        raise DataCatalogError("CONTROL_SIZE_INVALID", f"{label} size is invalid")
    try:
        raw = path.read_bytes()
        value = json.loads(raw, object_pairs_hook=_reject_duplicates)
    except (OSError, UnicodeError, ValueError) as exc:
        raise DataCatalogError("CONTROL_JSON_INVALID", f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise DataCatalogError("CONTROL_JSON_INVALID", f"{label} root is not an object")
    return value, raw


def _family_name(channel: str) -> str:
    known = CHANNEL_FAMILIES.get(channel)
    if known is not None:
        return known
    value = re.sub(r"[^A-Za-z0-9]+", "_", channel).strip("_").upper()
    if not value or len(value) > 64:
        raise DataCatalogError("CHANNEL_INVALID", "manifest channel cannot become a family")
    return value


def _families(descriptor: dict[str, Any], *, include_rfq: bool) -> tuple[list[str], list[str]]:
    channels = sorted(str(value) for value in descriptor.get("tables", {}))
    families = {_family_name(channel) for channel in channels}
    kinds = {str(item.get("kind")) for item in descriptor["objects"]}
    if {"dim_snapshot", "catalog"}.issubset(kinds):
        families.add("MARKET_GRAPH")
    if kinds & {
        "capture_gap_receipt",
        "capture_gaps_projection",
        "l2_quality_receipt",
    }:
        families.add("QUALITY_EVIDENCE")
    if kinds & {"correction", "corrections_ledger_day"}:
        families.add("CORRECTIONS")
    if include_rfq:
        families.add("RFQ")
        channels.append("rfq")
    return sorted(families), sorted(set(channels))


def _marker_contract(
    release_dir: Path,
    manifest_raw: bytes,
    descriptor: dict[str, Any],
    marker: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    release_id = descriptor["release_id"]
    expected = {
        "schema": "research-reference-verified-v1",
        "storage_mode": "REFERENCE_V3",
        "version_binding_mode": "CANONICAL_REFERENCE",
        "release_id": release_id,
        "date": descriptor["date"],
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "reference_set_sha256": descriptor["reference_set_sha256"],
        "object_semantics_sha256": descriptor["object_semantics_sha256"],
        "publication_state_sha256": descriptor["publication_state_sha256"],
        "canonical_receipt_set_sha256": descriptor["canonical_receipt_set_sha256"],
        "canonical_receipt_verified": True,
        "evidence_tier": descriptor["evidence_tier"],
        "tl1_status": descriptor["tl1_status"],
        "rfq_included": descriptor["rfq_included"],
    }
    for field, value in expected.items():
        if marker.get(field) != value:
            raise DataCatalogError(
                "VERIFIED_MARKER_MISMATCH", f"verified marker differs on {field}"
            )
    version_id = marker.get("manifest_version_id")
    if not isinstance(version_id, str) or not version_id or version_id.lower() == "null":
        raise DataCatalogError("VERIFIED_MARKER_MISMATCH", "manifest VersionId is missing")

    rfq_objects = [item for item in descriptor["objects"] if item["kind"] == "rfq"]
    rfq_status = marker.get("rfq_status")
    if not rfq_objects:
        if rfq_status != "ABSENT_FROM_RELEASE":
            raise DataCatalogError("VERIFIED_MARKER_MISMATCH", "RFQ status is invalid")
        rfq_materialized = False
    elif rfq_status == "VERIFIED_SEALED_RAW":
        rfq_materialized = True
    elif rfq_status == "NOT_FETCHED_OPT_IN":
        rfq_materialized = False
    else:
        raise DataCatalogError("VERIFIED_MARKER_MISMATCH", "RFQ status is invalid")

    materialized = [
        item
        for item in descriptor["objects"]
        if item["kind"] != "rfq" or rfq_materialized
    ]
    expected_count = len(materialized)
    expected_bytes = sum(int(item["size"]) for item in materialized)
    if marker.get("objects_verified") != expected_count or marker.get("bytes_verified") != expected_bytes:
        raise DataCatalogError(
            "VERIFIED_MARKER_MISMATCH", "verified object totals differ from manifest"
        )
    if marker.get("objects_referenced") != len(descriptor["objects"]):
        raise DataCatalogError(
            "VERIFIED_MARKER_MISMATCH", "referenced object total differs from manifest"
        )

    expected_logicals = {item["logical_key"] for item in materialized}
    for item in descriptor["objects"]:
        path = release_dir / item["local_key"]
        expected_here = item["logical_key"] in expected_logicals
        if not expected_here:
            if path.exists() or path.is_symlink():
                raise DataCatalogError(
                    "UNVERIFIED_OBJECT_PRESENT", "an unverified optional object is materialized"
                )
            continue
        if not path.is_symlink() or not path.is_file():
            raise DataCatalogError(
                "MATERIALIZED_OBJECT_MISSING", "a verified object is missing or not linked"
            )
        try:
            target = path.resolve(strict=True)
            stat = path.stat()
        except OSError as exc:
            raise DataCatalogError(
                "MATERIALIZED_OBJECT_MISSING", "a verified object link is broken"
            ) from exc
        content = (
            release_dir.parents[1]
            / "objects"
            / "sha256"
            / item["sha256"][:2]
            / item["sha256"]
        )
        try:
            expected_target = content.resolve(strict=True)
        except OSError as exc:
            raise DataCatalogError(
                "MATERIALIZED_OBJECT_MISSING", "content-addressed object is missing"
            ) from exc
        if (
            content.is_symlink()
            or not content.is_file()
            or target != expected_target
            or target.name != item["sha256"]
            or stat.st_size != item["size"]
        ):
            raise DataCatalogError(
                "MATERIALIZED_OBJECT_DRIFT", "a verified object identity changed"
            )
    return materialized, rfq_materialized


def _release_record(release_dir: Path) -> dict[str, Any]:
    manifest, manifest_raw = _read_json(
        release_dir / "MANIFEST.json", MAX_MANIFEST_BYTES, "V3 MANIFEST"
    )
    marker, _marker_raw = _read_json(
        release_dir / ".VERIFIED.json", MAX_MARKER_BYTES, "VERIFIED marker"
    )
    try:
        descriptor = reference.validate_manifest(manifest, release_dir.name)
    except reference.ReferenceManifestError as exc:
        raise DataCatalogError("MANIFEST_CONTRACT_INVALID", "V3 manifest is invalid") from exc
    materialized, rfq_materialized = _marker_contract(
        release_dir, manifest_raw, descriptor, marker
    )
    data_families, channels = _families(
        descriptor, include_rfq=rfq_materialized
    )
    manifest_families, manifest_channels = _families(
        descriptor, include_rfq=bool(descriptor["rfq_included"])
    )
    record = {
        "release_id": descriptor["release_id"],
        "date": descriptor["date"],
        "published_at_utc": descriptor["published_at_utc"],
        "storage_mode": "REFERENCE_V3",
        "version_binding_mode": "CANONICAL_REFERENCE",
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "manifest_version_id": marker["manifest_version_id"],
        "reference_set_sha256": descriptor["reference_set_sha256"],
        "object_semantics_sha256": descriptor["object_semantics_sha256"],
        "publication_state_sha256": descriptor["publication_state_sha256"],
        "evidence_tier": descriptor["evidence_tier"],
        "tl1_status": descriptor["tl1_status"],
        "data_families": data_families,
        "channels": channels,
        "manifest_data_families": manifest_families,
        "manifest_channels": manifest_channels,
        "object_count": len(materialized),
        "object_bytes": sum(int(item["size"]) for item in materialized),
        "manifest_object_count": len(descriptor["objects"]),
        "manifest_object_bytes": sum(int(item["size"]) for item in descriptor["objects"]),
        "rfq": {
            "manifest_included": bool(descriptor["rfq_included"]),
            "materialized": rfq_materialized,
            "status": marker["rfq_status"],
        },
    }
    return record


def build_catalog(cache_root: Path) -> dict[str, Any]:
    """Scan only local, marker-bearing releases and return stable metadata."""
    cache_root = Path(cache_root).resolve()
    releases_root = cache_root / "releases"
    if not releases_root.is_dir() or releases_root.is_symlink():
        raise DataCatalogError("CACHE_MISSING", "materialized release cache is missing")

    releases: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for release_dir in sorted(releases_root.iterdir(), key=lambda path: path.name):
        if not release_dir.is_dir() or release_dir.is_symlink():
            continue
        marker_path = release_dir / ".VERIFIED.json"
        if not marker_path.exists():
            continue
        try:
            releases.append(_release_record(release_dir))
        except DataCatalogError as exc:
            rejected.append({"release_id": release_dir.name, "code": exc.code})

    releases.sort(key=lambda row: (row["date"], row["published_at_utc"], row["release_id"]))
    if releases and rejected:
        state = "PARTIAL"
    elif releases:
        state = "READY"
    elif rejected:
        state = "REFUSED"
    else:
        state = "EMPTY"
    payload: dict[str, Any] = {
        "schema_version": SCHEMA,
        "state": state,
        "release_count": len(releases),
        "dates": sorted({row["date"] for row in releases}),
        "releases": releases,
        "rejected_releases": rejected,
        "network_reads": 0,
        "copied_bytes": 0,
        "zero_copy": True,
    }
    payload["catalog_sha256"] = canonical_sha256(payload)
    return payload


__all__ = [
    "DataCatalogError",
    "build_catalog",
    "canonical_bytes",
    "canonical_sha256",
]
