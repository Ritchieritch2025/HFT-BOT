#!/usr/bin/env python3
"""Shared fail-closed contracts for the Deep03 D3-W2A V3 discovery run.

This module deliberately has no network client.  It accepts only explicitly
named, locally materialized ``REFERENCE_V3`` releases that the canonical
research reader has already verified.  Large fact objects are bound through
the reader's content-addressed symlinks and fresh ``.VERIFIED.json`` marker;
the research runner never falls back to a latest release, copied-v2 bytes, or
RFQ data.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable


HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import research_reference as reference  # noqa: E402


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
SCHEMA_INPUT = "deep03-d3-w2a-v3-input-manifest-v1"
SCHEMA_PREPARE = "deep03-d3-w2a-v3-prepare-receipt-v1"
MODE = "MODE 1 / EXPLORATORY_AUTORESEARCH"
ACCEPTED_EVIDENCE_TIERS = frozenset({
    "SEALED_CONFIRMATION",
    "SEALED_DEGRADED_EVIDENCE",
})
LABELS = [
    "SEALED_PENDING_QUALITY_ASSESSMENT",
    "EXPLORATORY_ONLY",
    "NOT_STRICT_ACCEPTANCE",
]
SOURCE_FILES = (
    "deep03_v3_common.py",
    "deep03_v3_prepare.py",
    "deep03_v3_methods.py",
    "deep03_v3_runner.py",
)


class Deep03InputError(RuntimeError):
    """Stable fail-closed input or run-bundle contract error."""


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Deep03InputError(f"{label} is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise Deep03InputError(f"{label} root is not an object: {path}")
    return value


def atomic_write_bytes(path: Path, payload: bytes, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, 0o640)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive and path.exists():
            raise Deep03InputError(f"refusing to replace immutable artifact: {path}")
        os.replace(temporary, path)
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, value: Any, *, exclusive: bool = False) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value), exclusive=exclusive)


def refuse_credential_environment() -> None:
    names = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "KALSHI_API_KEY_ID",
        "KALSHI_PRIVATE_KEY_PATH",
    )
    present = sorted(name for name in names if os.environ.get(name))
    if present:
        raise Deep03InputError(
            "credential boundary refused: static AWS or Kalshi trading "
            f"variables are present ({', '.join(present)})"
        )


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise Deep03InputError(
            "run_id must be 3-128 portable characters [A-Za-z0-9._-]"
        )
    return run_id


def source_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name in SOURCE_FILES:
        path = HERE / name
        if not path.is_file():
            raise Deep03InputError(f"pinned source module missing: {path}")
        hashes[f"tools/research/{name}"] = sha256_file(path)
    return hashes


def _normalize_fact_path(value: str) -> str | None:
    text = str(value).replace("\\", "/")
    if text.startswith("facts/"):
        return text
    marker = "/facts/"
    if marker in "/" + text:
        return "facts/" + ("/" + text).split(marker, 1)[1]
    return None


def _warehouse_row_counts(release_dir: Path) -> dict[str, int]:
    path = release_dir / "warehouse_manifest" / "manifest.csv"
    if not path.is_file():
        return {}
    rows: dict[str, int] = {}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                logical = _normalize_fact_path(str(raw.get("file_path") or ""))
                if logical is None:
                    continue
                count = int(str(raw.get("row_count") or ""))
                if count < 0 or logical in rows:
                    raise ValueError(f"duplicate/invalid row count for {logical}")
                rows[logical] = count
    except (OSError, ValueError) as exc:
        raise Deep03InputError(
            f"warehouse row-count projection is invalid: {path}: {exc}"
        ) from exc
    return rows


def _small_control_digest(path: Path, expected: str, size: int) -> str:
    if size > 16 * 1024 * 1024:
        return "CONTENT_ADDRESS_AND_VERIFIED_MARKER"
    got = sha256_file(path)
    if got != expected:
        raise Deep03InputError(f"small control object changed after verification: {path}")
    return "SHA256_REVERIFIED_DURING_PREPARE"


def validate_explicit_releases(
    cache_root: Path,
    release_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """Return normalized, exact V3 release records or fail before data access."""
    cache_root = Path(cache_root).resolve()
    values = list(release_ids)
    if not values:
        raise Deep03InputError(
            "at least one explicit --release is required; latest selection is forbidden"
        )
    if len(values) != len(set(values)):
        raise Deep03InputError("duplicate explicit release_id")
    if any(value.strip().lower() in {"latest", "newest", "auto"} for value in values):
        raise Deep03InputError("latest/newest/auto release selection is forbidden")

    records: list[dict[str, Any]] = []
    dates: set[str] = set()
    for release_id in values:
        release_dir = cache_root / "releases" / release_id
        manifest_path = release_dir / "MANIFEST.json"
        marker_path = release_dir / ".VERIFIED.json"
        if not manifest_path.is_file() or not marker_path.is_file():
            raise Deep03InputError(
                f"release is not locally materialized and VERIFIED: {release_id}"
            )
        manifest = load_json(manifest_path, "V3 MANIFEST")
        marker = load_json(marker_path, "V3 VERIFIED marker")
        try:
            descriptor = reference.validate_manifest(manifest, release_id)
        except reference.ReferenceManifestError as exc:
            raise Deep03InputError(
                f"strict V3 manifest gate failed for {release_id}: {exc}"
            ) from exc
        if descriptor.get("evidence_tier") not in ACCEPTED_EVIDENCE_TIERS:
            raise Deep03InputError(
                "evidence tier is outside the MODE 1 exploratory allowlist: "
                f"{release_id}:{descriptor.get('evidence_tier')}"
            )

        manifest_sha = sha256_file(manifest_path)
        fixed_marker = {
            "schema": "research-reference-verified-v1",
            "storage_mode": "REFERENCE_V3",
            "version_binding_mode": reference.STORAGE_MODE,
            "release_id": release_id,
            "date": descriptor["date"],
            "manifest_sha256": manifest_sha,
            "reference_set_sha256": descriptor["reference_set_sha256"],
            "object_semantics_sha256": descriptor["object_semantics_sha256"],
            "publication_state_sha256": descriptor["publication_state_sha256"],
            "canonical_receipt_set_sha256": descriptor[
                "canonical_receipt_set_sha256"
            ],
            "canonical_receipt_verified": True,
            "rfq_included": False,
            "rfq_status": "ABSENT_FROM_RELEASE",
        }
        for key, expected in fixed_marker.items():
            if marker.get(key) != expected:
                raise Deep03InputError(
                    f"VERIFIED marker does not bind V3 manifest: {release_id}/{key}"
                )
        if descriptor["rfq_included"] or any(
            obj["kind"] == "rfq" for obj in descriptor["objects"]
        ):
            raise Deep03InputError(
                f"RFQ is forbidden in this D3-W2A run: {release_id}"
            )
        if descriptor["date"] in dates:
            raise Deep03InputError(
                f"multiple release versions for one date are forbidden: {descriptor['date']}"
            )
        dates.add(descriptor["date"])
        if marker.get("objects_verified") != len(descriptor["objects"]):
            raise Deep03InputError(
                f"VERIFIED object count is incomplete: {release_id}"
            )

        marker_mtime = marker_path.stat().st_mtime_ns
        row_counts = _warehouse_row_counts(release_dir)
        objects: list[dict[str, Any]] = []
        for obj in descriptor["objects"]:
            local_path = release_dir / obj["local_key"]
            if not local_path.is_file():
                raise Deep03InputError(
                    f"manifest-bound exact object missing: {release_id}:{obj['logical_key']}"
                )
            stat = local_path.stat()
            if stat.st_size != obj["size"]:
                raise Deep03InputError(
                    f"manifest-bound exact object size drift: {release_id}:{obj['logical_key']}"
                )
            if stat.st_mtime_ns > marker_mtime:
                raise Deep03InputError(
                    f"object changed after VERIFIED marker: {release_id}:{obj['logical_key']}"
                )
            if not local_path.is_symlink() or local_path.resolve().name != obj["sha256"]:
                raise Deep03InputError(
                    "V3 object is not the reader's content-addressed exact-version "
                    f"materialization: {release_id}:{obj['logical_key']}"
                )
            verification_basis = _small_control_digest(
                local_path, obj["sha256"], obj["size"]
            )
            row_count = row_counts.get(obj["local_key"])
            objects.append(
                {
                    "release_id": release_id,
                    "date": descriptor["date"],
                    "logical_key": obj["logical_key"],
                    "local_key": obj["local_key"],
                    "local_path": str(local_path),
                    "source_bucket": obj["source_bucket"],
                    "source_key": obj["source_key"],
                    "source_version_id": obj["source_version_id"],
                    "size": obj["size"],
                    "sha256": obj["sha256"],
                    "kind": obj["kind"],
                    "channel": obj["channel"],
                    "required": obj["required"],
                    "row_count": row_count,
                    "row_count_state": (
                        "SEALED_WAREHOUSE_MANIFEST"
                        if row_count is not None
                        else "NOT_APPLICABLE_OR_UNAVAILABLE"
                    ),
                    "local_verification_basis": verification_basis,
                }
            )
        records.append(
            {
                "release_id": release_id,
                "date": descriptor["date"],
                "release_dir": str(release_dir),
                "manifest_path": str(manifest_path),
                "verified_marker_path": str(marker_path),
                "manifest_sha256": manifest_sha,
                "verified_marker_sha256": sha256_file(marker_path),
                "reference_set_sha256": descriptor["reference_set_sha256"],
                "object_semantics_sha256": descriptor[
                    "object_semantics_sha256"
                ],
                "publication_state_sha256": descriptor[
                    "publication_state_sha256"
                ],
                "canonical_receipt_set_sha256": descriptor[
                    "canonical_receipt_set_sha256"
                ],
                "evidence_tier": descriptor["evidence_tier"],
                "evidence_basis": descriptor["evidence_basis"],
                "tl1_status": descriptor["tl1_status"],
                "rfq_included": False,
                "objects": objects,
            }
        )
    return records


def build_input_manifest(
    *, run_id: str, cache_root: Path, release_records: list[dict[str, Any]]
) -> dict[str, Any]:
    objects = [
        obj for release in release_records for obj in release["objects"]
    ]
    analytic = [
        obj
        for obj in objects
        if obj["kind"] in {"facts", "dim_snapshot", "capture_gaps_projection"}
    ]
    return {
        "schema_version": SCHEMA_INPUT,
        "run_id": validate_run_id(run_id),
        "created_at_utc": utc_now(),
        "mode": MODE,
        "strict_acceptance_claimed": False,
        "research_stage": "OPEN_DISCOVERY",
        "work_package": "D3-W2A",
        "evidence_labels": list(LABELS),
        "selection_mode": "EXPLICIT_RELEASE_IDS_ONLY",
        "cache_root": str(Path(cache_root).resolve()),
        "release_ids": [release["release_id"] for release in release_records],
        "release_dates": [release["date"] for release in release_records],
        "rfq_policy": "FORBIDDEN_AND_ABSENT",
        "version_binding_mode": "CANONICAL_REFERENCE",
        "verification_contract": {
            "manifest": "research-release-manifest-v3-reference",
            "verified_marker": "research-reference-verified-v1",
            "large_objects": (
                "CONTENT_ADDRESSED_SHA256_SYMLINK_PLUS_POST_OBJECT_VERIFIED_MARKER"
            ),
            "small_controls": "SHA256_REVERIFIED_DURING_PREPARE",
            "network_reads": 0,
        },
        "source_modules_sha256": source_hashes(),
        "release_count": len(release_records),
        "object_count": len(objects),
        "object_bytes": sum(int(obj["size"]) for obj in objects),
        "sealed_fact_rows": sum(
            int(obj["row_count"] or 0) for obj in objects if obj["kind"] == "facts"
        ),
        "fact_objects_without_row_count": sum(
            1
            for obj in objects
            if obj["kind"] == "facts" and obj["row_count"] is None
        ),
        "releases": [
            {key: value for key, value in release.items() if key != "objects"}
            for release in release_records
        ],
        "objects": objects,
        "analytic_objects": analytic,
    }


def stable_input_projection(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "schema_version",
            "run_id",
            "mode",
            "strict_acceptance_claimed",
            "research_stage",
            "work_package",
            "evidence_labels",
            "selection_mode",
            "cache_root",
            "release_ids",
            "release_dates",
            "rfq_policy",
            "version_binding_mode",
            "source_modules_sha256",
            "release_count",
            "object_count",
            "object_bytes",
            "sealed_fact_rows",
            "fact_objects_without_row_count",
            "releases",
            "objects",
            "analytic_objects",
        )
    }


def input_projection_sha256(value: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(stable_input_projection(value)))


def ensure_run_inputs_current(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    input_path = run_dir / "INPUT_MANIFEST.json"
    prepare_path = run_dir / "PREPARE_RECEIPT.json"
    value = load_json(input_path, "INPUT_MANIFEST")
    receipt = load_json(prepare_path, "PREPARE_RECEIPT")
    if value.get("schema_version") != SCHEMA_INPUT:
        raise Deep03InputError("unsupported INPUT_MANIFEST schema")
    if receipt.get("schema_version") != SCHEMA_PREPARE:
        raise Deep03InputError("unsupported PREPARE_RECEIPT schema")
    if receipt.get("input_manifest_sha256") != sha256_file(input_path):
        raise Deep03InputError("PREPARE_RECEIPT does not bind INPUT_MANIFEST bytes")
    if receipt.get("input_projection_sha256") != input_projection_sha256(value):
        raise Deep03InputError("INPUT_MANIFEST stable projection changed")
    current_sources = source_hashes()
    if value.get("source_modules_sha256") != current_sources:
        raise Deep03InputError("research payload source hashes changed after prepare")

    records = validate_explicit_releases(
        Path(str(value.get("cache_root") or "")), value.get("release_ids") or []
    )
    rebuilt = build_input_manifest(
        run_id=str(value.get("run_id") or ""),
        cache_root=Path(str(value.get("cache_root") or "")),
        release_records=records,
    )
    # created_at_utc and the explanatory verification block are not identities;
    # every data, version, row-count and source-code binding is.
    if input_projection_sha256(rebuilt) != input_projection_sha256(value):
        raise Deep03InputError("exact V3 release set changed after prepare")
    return value, receipt
