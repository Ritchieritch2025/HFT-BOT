#!/usr/bin/env python3
"""Append-only same-run registration repair for a proven malformed RFQ object.

This tool is deliberately narrower than the normal pre-results freeze.  It is
only valid after the Cycle-1 core has completed and before any dependent RFQ
full-scan result has been opened.  It preserves the original registration
identity, archives the failed attempt and old code receipts, registers one
deterministic whole-object quarantine, and binds a new committed source/query
identity.  It never runs or recomputes an analysis.

The quarantine declaration is ``rfq-object-quarantine-v1``.  Its single
``quarantined_objects`` entry must contain the exact release id, object key,
manifest SHA-256, VersionId, object SHA-256, size, malformed receipt path, and
invalid-line count.  The receipt is the unmodified
``rfq-malformed-object-receipt-v1`` scanner output.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


REPAIR_ID = "repair-01"
EXPECTED_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_FULL_SCAN_PENDING"
POST_REPAIR_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR_REGISTERED"
DECLARATION_SCHEMA = "rfq-object-quarantine-v1"
RECEIPT_SCHEMA = "rfq-malformed-object-receipt-v1"
FINDING = "MALFORMED_NDJSON_OBJECT"
DECLARATION_DISPOSITION = "WHOLE_OBJECT_QUARANTINE"
QUARANTINE_POLICY = "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE"
NO_RFQ_RESULT = "NO_RFQ_RESULT_OPENED"
CORE_DISPOSITION = "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED"
REGISTRATION_CHANGE_CLASS = (
    "DATA_INTEGRITY_HANDLING_ONLY_NO_HYPOTHESIS_DESIGN_CHANGE"
)
HYPOTHESIS_DESIGN_CHANGE = "NONE"
DATA_INTEGRITY_HANDLING_CHANGE = (
    "ONE_EXACT_WHOLE_OBJECT_QUARANTINE_AND_CONSERVATIVE_GAP_CENSORING"
)
LEGACY_FROZEN_DESIGN_CHANGE = (
    "NO_HYPOTHESIS_DESIGN_CHANGE; DATA_INTEGRITY_HANDLING_CHANGED"
)
FAILURE_STATE = Path("REPORT/tables/RFQ_FULL_STAGE_STATE.json")
FAILURE_RESOURCE = Path("logs/resources/rfq_full_stage.json")
FAILURE_SCRATCH_RECEIPT = Path("DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT.json")
FAILED_INPUT_IDENTITY = Path("DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json")
CYCLE1_DUCKDB_BINDING = Path("DATA_INTEGRITY/CYCLE1_DUCKDB_BINDING.json")
CORE_SUMMARY = Path("REPORT/CYCLE1_CORE_SUMMARY.json")
CORE_RESOURCE = Path("logs/resources/cycle1_core.json")
CORE_RESULT_PATHS = (
    CORE_SUMMARY,
    Path("REPORT/tables/CORE_HYPOTHESIS_TESTS.json"),
    Path("REPORT/tables/L2_HYPOTHESIS_STAGE_SUMMARY.json"),
    Path("REPORT/tables/RFQ_TRIGGER_REPRODUCTION.json"),
)
RFQ_SUMMARY = Path("REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json")
RFQ_REPORT = Path("REPORT/RFQ_FULL_STAGE.md")
RFQ_TRIAL_IDS = (
    "C1-RFQ-CLOB-01",
    "C1-ANOM-RFQ-SIZE-TAIL-01",
    "C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01",
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
CHECKSUM_LINE = re.compile(r"^([0-9a-f]{64})  ([^\r\n]+)$")


class RepairRegistrationError(RuntimeError):
    """A fail-closed repair precondition or integrity check failed."""


def fail(message: str) -> None:
    raise RepairRegistrationError(message)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def json_payload(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def registry_line(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        fail(f"{label} is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"{label} is not valid UTF-8 JSON: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must be a JSON object")
    return value


def git(repo_root: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repo_root, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        fail(
            f"git {' '.join(arguments)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def git_blob(repo_root: Path, commit: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"], cwd=repo_root,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        fail(f"old execution commit lacks registered source {path}")
    return result.stdout


def relative_to_run(path: Path, run_dir: Path, label: str) -> str:
    try:
        relative = path.resolve().relative_to(run_dir.resolve())
    except ValueError:
        fail(f"{label} must be inside the run directory")
    if relative == Path(".") or any(part in ("", ".", "..") for part in relative.parts):
        fail(f"{label} has an unsafe run-relative path")
    return relative.as_posix()


def checked_run_path(run_dir: Path, relative: str, label: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(
        part in ("", ".", "..") for part in pure.parts
    ):
        fail(f"{label} is not a safe run-relative path")
    path = run_dir.joinpath(*pure.parts).resolve()
    try:
        path.relative_to(run_dir.resolve())
    except ValueError:
        fail(f"{label} escapes the run directory")
    return path


def require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or HEX64.fullmatch(value) is None:
        fail(f"{label} must be a lowercase SHA-256")
    return value


def require_positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        fail(f"{label} must be a positive integer")
    return value


def require_exact_keys(value: dict[str, Any], keys: set[str], label: str) -> None:
    if set(value) != keys:
        fail(f"{label} has an unexpected field set")


def require_utc_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        fail(f"{label} must be a UTC timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        fail(f"{label} must be a valid UTC timestamp")
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        fail(f"{label} must have an explicit UTC offset")
    return value


def rfq_object_fingerprint(objects: list[dict[str, Any]]) -> str:
    rows = [
        f"{row['key']}\t{row['size']}\t{row['sha256']}"
        for row in objects
    ]
    return sha256_bytes("\n".join(rows).encode("utf-8"))


def validate_failed_input_identity(
    run_dir: Path,
    manifest: dict[str, Any],
    identity_path: Path,
) -> tuple[dict[str, Any], str]:
    """Rebuild and verify the exact pre-repair RFQ manifest union.

    The attempt-01 identity is an output from input discovery, not an authority.
    This validator therefore reconstructs every count, byte total, overlap and
    fingerprint from the selected immutable release manifests and independently
    binds the local verification markers before accepting the receipt.
    """

    identity = load_json(identity_path, "failed RFQ input identity")
    require_exact_keys(
        identity,
        {
            "schema",
            "release_ids",
            "releases",
            "logical_manifest_bindings",
            "unique_objects",
            "deduplicated_overlapping_objects",
            "path_size_sha_fingerprint",
            "objects",
        },
        "failed RFQ input identity",
    )
    if identity.get("schema") != "rfq-full-input-identity-v1":
        fail("failed RFQ input identity schema is not v1")

    selected = manifest.get("selected_releases")
    if not isinstance(selected, list) or not selected:
        fail("RUN_MANIFEST selected_releases must be a nonempty array")
    captured_release_receipts = identity.get("releases")
    if (
        not isinstance(captured_release_receipts, list)
        or len(captured_release_receipts) != len(selected)
    ):
        fail("failed RFQ input release receipts are incomplete")
    selected_ids: list[str] = []
    by_key: dict[str, dict[str, Any]] = {}
    logical_bindings = 0
    release_receipts: list[dict[str, Any]] = []
    for release_index, release in enumerate(selected):
        if not isinstance(release, dict):
            fail(f"selected release {release_index} is not an object")
        release_id = release.get("release_id")
        date = release.get("date")
        if (
            not isinstance(release_id, str)
            or not release_id
            or "/" in release_id
            or release_id in selected_ids
        ):
            fail("selected release ids are invalid or duplicated")
        if not isinstance(date, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) is None:
            fail(f"selected release date is invalid: {release_id}")
        selected_ids.append(release_id)
        manifest_sha = require_sha(
            release.get("manifest_sha256"),
            f"selected release manifest SHA: {release_id}",
        )
        manifest_path = checked_run_path(
            run_dir,
            f"DATA_INTEGRITY/manifests/{release_id}.json",
            "selected immutable manifest",
        )
        if not manifest_path.is_file() or sha256(manifest_path) != manifest_sha:
            fail(f"selected immutable manifest SHA mismatch: {release_id}")
        document = load_json(manifest_path, "selected immutable manifest")
        if (
            document.get("schema_version") != "research-release-manifest-v2"
            or document.get("release_id") != release_id
            or document.get("date") != date
            or document.get("evidence_tier") != release.get("evidence_tier")
            or document.get("tl1_status") != release.get("tl1_status")
            or document.get("publication_state_sha256")
            != release.get("publication_state_sha256")
            or not isinstance(document.get("objects"), list)
        ):
            fail(f"selected immutable manifest identity mismatch: {release_id}")
        seal = document.get("seal")
        version_binding = document.get("version_binding")
        if (
            release.get("include") != "EXPLORATORY_ONLY"
            or release.get("version_binding_mode") != "VERSION_BOUND"
            or not isinstance(seal, dict)
            or seal.get("status") != "SEALED"
            or seal.get("sha256") != release.get("seal_sha256")
            or not isinstance(version_binding, dict)
            or version_binding.get("mode") != "VERSION_BOUND"
            or version_binding.get("bindings_sha256")
            != release.get("version_bindings_sha256")
        ):
            fail(f"selected release is not sealed/version-bound: {release_id}")

        release_all_bytes = 0
        release_seen: set[str] = set()
        release_rfq_count = 0
        release_rfq_bytes = 0
        for object_index, row in enumerate(document["objects"]):
            if not isinstance(row, dict):
                fail(f"manifest object is not an object: {release_id}:{object_index}")
            require_exact_keys(
                row,
                {"key", "sha256", "size", "version_id"},
                f"manifest object {release_id}:{object_index}",
            )
            key = row.get("key")
            if (
                not isinstance(key, str)
                or not key
                or key in release_seen
                or key.startswith("/")
                or ".." in PurePosixPath(key).parts
            ):
                fail(f"manifest object key is invalid or duplicated: {release_id}:{key}")
            release_seen.add(key)
            digest = require_sha(row.get("sha256"), f"manifest object SHA: {key}")
            size = require_positive_int(row.get("size"), f"manifest object size: {key}")
            if not isinstance(row.get("version_id"), str) or not row["version_id"]:
                fail(f"manifest object VersionId is missing: {release_id}:{key}")
            release_all_bytes += size
            if not key.startswith("raw_rfq/"):
                continue
            release_rfq_count += 1
            release_rfq_bytes += size
            logical_bindings += 1
            prior = by_key.get(key)
            if prior is None:
                by_key[key] = {
                    "bound_release_ids": [release_id],
                    "key": key,
                    "sha256": digest,
                    "size": size,
                }
            else:
                if prior["sha256"] != digest or prior["size"] != size:
                    fail(f"overlapping RFQ object has conflicting bytes: {key}")
                prior["bound_release_ids"].append(release_id)
        if release_rfq_count == 0:
            fail(f"selected release contains no RFQ objects: {release_id}")
        if (
            release.get("object_count") != len(document["objects"])
            or release.get("byte_count") != release_all_bytes
        ):
            fail(f"selected release object/byte totals mismatch: {release_id}")

        marker_path = checked_run_path(
            run_dir,
            f"DATA_INTEGRITY/verified/{date}.json",
            "selected release verification marker",
        )
        marker = load_json(marker_path, "selected release verification marker")
        if (
            marker.get("release_id") != release_id
            or marker.get("date") != date
            or marker.get("evidence_tier") != release.get("evidence_tier")
            or marker.get("tl1_status") != release.get("tl1_status")
            or marker.get("version_binding_mode") != "VERSION_BOUND"
            or marker.get("publication_state_sha256")
            != release.get("publication_state_sha256")
            or marker.get("seal_sha256") != release.get("seal_sha256")
            or marker.get("objects_verified") != release.get("object_count")
            or marker.get("bytes_verified") != release.get("byte_count")
            or marker.get("corrections_total") != release.get("corrections_total")
            or marker.get("rfq_included") is not True
            or marker.get("rfq_status") != "VERIFIED_SEALED_RAW"
        ):
            fail(f"selected release verification receipt mismatch: {release_id}")
        captured_receipt = captured_release_receipts[release_index]
        if not isinstance(captured_receipt, dict):
            fail(f"captured RFQ release receipt is invalid: {release_id}")
        require_exact_keys(
            captured_receipt,
            {
                "manifest_sha256",
                "release_id",
                "rfq_bytes",
                "rfq_objects",
                "verified_marker_sha256",
            },
            f"captured RFQ release receipt: {release_id}",
        )
        captured_marker_sha = require_sha(
            captured_receipt.get("verified_marker_sha256"),
            f"captured W09 cache verification marker SHA: {release_id}",
        )
        release_receipts.append(
            {
                "manifest_sha256": manifest_sha,
                "release_id": release_id,
                "rfq_bytes": release_rfq_bytes,
                "rfq_objects": release_rfq_count,
                # This SHA was captured from the W09 release cache's
                # ``.VERIFIED.json``.  The run's date-scoped verification
                # receipt above is a distinct document, so its SHA must not be
                # substituted for this immutable discovery receipt.
                "verified_marker_sha256": captured_marker_sha,
            }
        )

    objects = [by_key[key] for key in sorted(by_key)]
    fingerprint = rfq_object_fingerprint(objects)
    expected = {
        "deduplicated_overlapping_objects": sum(
            1 for row in objects if len(row["bound_release_ids"]) > 1
        ),
        "logical_manifest_bindings": logical_bindings,
        "objects": objects,
        "path_size_sha_fingerprint": fingerprint,
        "release_ids": selected_ids,
        "releases": release_receipts,
        "schema": "rfq-full-input-identity-v1",
        "unique_objects": len(objects),
    }
    if identity != expected:
        fail("failed RFQ input identity is not the exact immutable manifest union")
    return identity, fingerprint


def validate_cycle1_duckdb_binding(
    run_dir: Path,
    binding_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the frozen Cycle-1 DB receipt without reading the large DB."""

    binding = load_json(binding_path, "Cycle-1 DuckDB binding")
    require_exact_keys(
        binding,
        {
            "bytes",
            "core_result_disposition",
            "core_stage_resource_path",
            "core_stage_resource_sha256",
            "core_summary_path",
            "core_summary_sha256",
            "created_before_rfq_repair_registration",
            "duckdb_version",
            "mtime_utc",
            "path",
            "run_id",
            "schema_version",
            "sha256",
        },
        "Cycle-1 DuckDB binding",
    )
    run_id = run_dir.name
    expected_db_path = f"/srv/w09-research/runs/{run_id}/cache/cycle1.duckdb"
    if (
        binding.get("schema_version") != "cycle1-derived-duckdb-binding-v1"
        or binding.get("run_id") != run_id
        or binding.get("path") != expected_db_path
        or not Path(str(binding.get("path"))).is_absolute()
        or binding.get("duckdb_version") != "1.4.5"
        or binding.get("created_before_rfq_repair_registration") is not True
        or binding.get("core_result_disposition")
        != "CORE_DERIVED_DATABASE_PRESERVED_NOT_RECOMPUTED"
        or binding.get("core_summary_path") != CORE_SUMMARY.as_posix()
        or binding.get("core_stage_resource_path") != CORE_RESOURCE.as_posix()
    ):
        fail("Cycle-1 DuckDB binding identity/policy mismatch")
    db_bytes = require_positive_int(binding.get("bytes"), "Cycle-1 DuckDB bytes")
    db_sha = require_sha(binding.get("sha256"), "Cycle-1 DuckDB SHA-256")
    require_utc_timestamp(binding.get("mtime_utc"), "Cycle-1 DuckDB mtime_utc")
    summary_sha = require_sha(
        binding.get("core_summary_sha256"), "Cycle-1 core summary SHA-256"
    )
    resource_sha = require_sha(
        binding.get("core_stage_resource_sha256"),
        "Cycle-1 core resource SHA-256",
    )
    summary_path = run_dir / CORE_SUMMARY
    resource_path = run_dir / CORE_RESOURCE
    if not summary_path.is_file() or sha256(summary_path) != summary_sha:
        fail("Cycle-1 DuckDB binding core summary hash mismatch")
    if not resource_path.is_file() or sha256(resource_path) != resource_sha:
        fail("Cycle-1 DuckDB binding core resource hash mismatch")
    receipt_sha = sha256(binding_path)
    nested = {
        "active_path": CYCLE1_DUCKDB_BINDING.as_posix(),
        "active_sha256": receipt_sha,
        "archived_path": (
            f"DATA_INTEGRITY/repairs/{REPAIR_ID}/pre_repair/"
            f"{CYCLE1_DUCKDB_BINDING.as_posix()}"
        ),
        "archived_sha256": receipt_sha,
        "schema_version": "cycle1-derived-duckdb-binding-v1",
        "run_id": run_id,
        "duckdb_path": expected_db_path,
        "duckdb_bytes": db_bytes,
        "duckdb_sha256": db_sha,
        "core_stage_resource_path": CORE_RESOURCE.as_posix(),
        "core_stage_resource_sha256": resource_sha,
        "core_summary_path": CORE_SUMMARY.as_posix(),
        "core_summary_sha256": summary_sha,
    }
    return binding, nested


def parse_checksums(path: Path, label: str) -> list[tuple[str, str]]:
    if not path.is_file():
        fail(f"{label} is missing")
    raw = path.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        fail(f"{label} must be nonempty and newline-terminated")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        fail(f"{label} is not UTF-8: {exc}")
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in lines:
        match = CHECKSUM_LINE.fullmatch(line)
        if match is None:
            fail(f"{label} has a malformed checksum line")
        digest, relative = match.groups()
        if relative in seen:
            fail(f"{label} has duplicate path {relative}")
        seen.add(relative)
        output.append((digest, relative))
    return output


def verify_source_sums_match_manifest(
    manifest_path: Path, sums_path: Path, label: str
) -> None:
    """Verify the flat SOURCE_SHA receipt is an exact projection of its manifest."""
    if not manifest_path.is_file():
        fail(f"{label} SOURCE_MANIFEST.json is missing")
    try:
        rows = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"{label} SOURCE_MANIFEST.json is invalid: {exc}")
    if not isinstance(rows, list) or not rows:
        fail(f"{label} SOURCE_MANIFEST.json must be a nonempty array")
    expected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            fail(f"{label} SOURCE_MANIFEST row {index} is invalid")
        digest = require_sha(
            row.get("sha256"), f"{label} SOURCE_MANIFEST[{index}].sha256"
        )
        name = PurePosixPath(row["path"]).name
        if not name or name in seen:
            fail(f"{label} SOURCE_MANIFEST has a duplicate/invalid basename")
        seen.add(name)
        expected.append((digest, name))
    if parse_checksums(sums_path, f"{label} SOURCE_SHA256SUMS.txt") != expected:
        fail(
            f"{label} SOURCE_SHA256SUMS.txt does not exactly match "
            "SOURCE_MANIFEST.json"
        )


def verify_clean_source(source_dir: Path) -> tuple[Path, str, str]:
    if not source_dir.is_dir():
        fail(f"source directory is missing: {source_dir}")
    repo_text = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=source_dir,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if repo_text.returncode != 0:
        fail("source directory is not inside a git repository")
    repo_root = Path(repo_text.stdout.strip()).resolve()
    try:
        source_relative = source_dir.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        fail("source directory escapes its git repository")
    status = git(
        repo_root, "status", "--porcelain=v1", "--untracked-files=all", "--",
        source_relative,
    )
    if status:
        fail("research source tree must be committed and clean:\n" + status)
    head = git(repo_root, "rev-parse", "HEAD")
    if HEX40.fullmatch(head) is None:
        fail("current execution commit is not a full git commit SHA")
    return repo_root, source_relative, head


def source_files(source_dir: Path) -> list[Path]:
    return sorted(
        path for path in source_dir.iterdir()
        if path.is_file() and path.suffix in (".py", ".sh")
    )


def build_source_receipts(
    source_dir: Path, repo_root: Path
) -> tuple[list[dict[str, Any]], bytes, bytes]:
    rows = []
    for path in source_files(source_dir):
        rows.append({
            "path": path.resolve().relative_to(repo_root).as_posix(),
            "sha256": sha256(path),
            "bytes": path.stat().st_size,
        })
    if not rows:
        fail("current source tree contains no .py/.sh files")
    manifest_payload = json_payload(rows)
    sums_payload = "".join(
        f"{row['sha256']}  {PurePosixPath(row['path']).name}\n" for row in rows
    ).encode("utf-8")
    return rows, manifest_payload, sums_payload


def repository_identity(repository: dict[str, Any]) -> dict[str, Any]:
    return {
        "execution_commit": repository.get("execution_commit"),
        "source_manifest_sha256": repository.get("source_manifest_sha256"),
        "source_sha256s_sha256": repository.get("source_sha256s_sha256"),
        "query_set_sha256": repository.get("query_set_sha256"),
        "query_files": copy.deepcopy(repository.get("query_files")),
    }


def verify_prior_repository(
    run_dir: Path,
    source_dir: Path,
    repo_root: Path,
    source_relative: str,
    current_commit: str,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    repository = manifest.get("repository")
    if not isinstance(repository, dict):
        fail("RUN_MANIFEST repository binding is missing")
    old_commit = repository.get("execution_commit")
    if not isinstance(old_commit, str) or HEX40.fullmatch(old_commit) is None:
        fail("previous execution_commit is not a full commit SHA")
    if repository.get("source_tree_dirty_at_freeze") is not False:
        fail("previous registration was not frozen from a clean source tree")
    if old_commit == current_commit:
        fail("repair requires a new committed execution revision")
    if git(repo_root, "cat-file", "-t", old_commit) != "commit":
        fail("previous execution_commit is not a commit in this repository")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", old_commit, current_commit],
        cwd=repo_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if ancestor.returncode != 0:
        fail("new execution commit must descend from the previous execution commit")

    source_manifest_path = run_dir / "SOURCE_MANIFEST.json"
    source_sums_path = run_dir / "SOURCE_SHA256SUMS.txt"
    if repository.get("source_manifest_path") != "SOURCE_MANIFEST.json":
        fail("unexpected source_manifest_path")
    if sha256(source_manifest_path) != require_sha(
        repository.get("source_manifest_sha256"),
        "repository.source_manifest_sha256",
    ):
        fail("SOURCE_MANIFEST.json does not match RUN_MANIFEST")
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(source_manifest, list) or not source_manifest:
        fail("previous SOURCE_MANIFEST must be a nonempty array")
    expected_source_paths: list[str] = []
    manifest_by_name: dict[str, tuple[str, int]] = {}
    source_prefix = PurePosixPath(source_relative)
    for index, row in enumerate(source_manifest):
        if not isinstance(row, dict):
            fail(f"SOURCE_MANIFEST row {index} is not an object")
        path_text = row.get("path")
        digest = require_sha(row.get("sha256"), f"SOURCE_MANIFEST[{index}].sha256")
        size = row.get("bytes")
        if not isinstance(path_text, str) or not isinstance(size, int) or size < 0:
            fail(f"SOURCE_MANIFEST row {index} has invalid path/bytes")
        pure = PurePosixPath(path_text)
        if (
            pure.is_absolute() or pure.parent != source_prefix
            or pure.suffix not in (".py", ".sh")
        ):
            fail(f"SOURCE_MANIFEST path is outside the registered source root: {path_text}")
        blob = git_blob(repo_root, old_commit, path_text)
        if len(blob) != size or sha256_bytes(blob) != digest:
            fail(f"SOURCE_MANIFEST does not match previous execution commit: {path_text}")
        if pure.name in manifest_by_name:
            fail(f"duplicate source basename in SOURCE_MANIFEST: {pure.name}")
        manifest_by_name[pure.name] = (digest, size)
        expected_source_paths.append(path_text)
    old_tree = git(
        repo_root, "ls-tree", "-r", "--name-only", old_commit, "--", source_relative
    ).splitlines()
    old_tree = sorted(
        path for path in old_tree
        if PurePosixPath(path).parent == source_prefix
        and PurePosixPath(path).suffix in (".py", ".sh")
    )
    if sorted(expected_source_paths) != old_tree:
        fail("SOURCE_MANIFEST is not the complete previous committed source set")
    source_sums = parse_checksums(source_sums_path, "SOURCE_SHA256SUMS.txt")
    expected_sums = [
        (row["sha256"], PurePosixPath(row["path"]).name)
        for row in source_manifest
    ]
    if source_sums != expected_sums:
        fail("SOURCE_SHA256SUMS.txt does not exactly match SOURCE_MANIFEST.json")

    query_files = repository.get("query_files")
    if not isinstance(query_files, list) or not query_files:
        fail("repository.query_files must be a nonempty list")
    if len(query_files) != len(set(query_files)):
        fail("repository.query_files contains duplicates")
    query_sums_path = run_dir / "QUERY_SHA256SUMS.txt"
    if sha256(query_sums_path) != require_sha(
        repository.get("query_set_sha256"), "repository.query_set_sha256"
    ):
        fail("QUERY_SHA256SUMS.txt does not match RUN_MANIFEST")
    query_sums = parse_checksums(query_sums_path, "QUERY_SHA256SUMS.txt")
    if [relative for _, relative in query_sums] != query_files:
        fail("QUERY_SHA256SUMS paths do not exactly match repository.query_files")
    query_names: list[str] = []
    for digest, relative in query_sums:
        pure = PurePosixPath(relative)
        if pure.parent != PurePosixPath("queries") or pure.suffix != ".py":
            fail(f"unsafe or non-Python registered query path: {relative}")
        frozen = checked_run_path(run_dir, relative, "registered query")
        if not frozen.is_file() or sha256(frozen) != digest:
            fail(f"registered query copy was changed: {relative}")
        source_path = f"{source_relative}/{pure.name}"
        if source_path not in expected_source_paths:
            fail(f"registered query has no previous source binding: {relative}")
        if frozen.read_bytes() != git_blob(repo_root, old_commit, source_path):
            fail(f"registered query does not match previous execution commit: {relative}")
        query_names.append(pure.name)

    for field, relative in (
        ("feature_definition_sha256", "FEATURE_DICTIONARY.json"),
        ("method_definition_sha256", "METHODS.md"),
    ):
        expected = repository.get(field)
        if expected is not None:
            path = run_dir / relative
            if not path.is_file() or sha256(path) != require_sha(expected, f"repository.{field}"):
                fail(f"{relative} no longer matches RUN_MANIFEST")
    return copy.deepcopy(repository), query_names


def validate_receipt(receipt: dict[str, Any], obj: dict[str, Any], run_id: str) -> None:
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        fail("malformed-object receipt schema is not v1")
    if receipt.get("run_id") != run_id:
        fail("malformed-object receipt run_id mismatch")
    if receipt.get("disposition") != "CHANNEL_OBJECT_QUARANTINE_REQUIRED":
        fail("receipt does not require whole channel-object quarantine")
    for field in ("release_id", "key"):
        if receipt.get(field) != obj.get(field):
            fail(f"receipt/declaration {field} mismatch")
    if receipt.get("expected_size") != obj.get("size") or receipt.get(
        "observed_size"
    ) != obj.get("size"):
        fail("receipt size does not exactly match the manifest-bound object")
    if receipt.get("expected_sha256") != obj.get("sha256") or receipt.get(
        "observed_sha256"
    ) != obj.get("sha256"):
        fail("receipt SHA-256 does not exactly match the manifest-bound object")
    invalid_count = receipt.get("invalid_line_count")
    invalid_lines = receipt.get("invalid_lines")
    if (
        not isinstance(invalid_count, int) or invalid_count <= 0
        or not isinstance(invalid_lines, list) or len(invalid_lines) != invalid_count
        or obj.get("invalid_line_count") != invalid_count
    ):
        fail("receipt lacks an exact positive malformed-line proof")
    total_lines = receipt.get("total_lines")
    if not isinstance(total_lines, int) or total_lines <= 0:
        fail("receipt total_lines is invalid")
    seen_lines: set[int] = set()
    for index, line in enumerate(invalid_lines):
        if not isinstance(line, dict):
            fail(f"receipt invalid_lines[{index}] is not an object")
        line_number = line.get("line_number")
        if (
            not isinstance(line_number, int) or line_number <= 0
            or line_number > total_lines or line_number in seen_lines
        ):
            fail("receipt malformed line numbers are invalid or duplicated")
        seen_lines.add(line_number)
        require_sha(line.get("line_sha256"), "receipt invalid line SHA-256")
        if (
            not isinstance(line.get("line_bytes"), int) or line["line_bytes"] <= 0
            or not isinstance(line.get("error_position"), int)
            or line["error_position"] < 0
            or not isinstance(line.get("error_type"), str) or not line["error_type"]
            or not isinstance(line.get("error"), str) or not line["error"]
        ):
            fail("receipt malformed-line evidence is incomplete")
    if receipt.get("raw_payload_redacted") is not True:
        fail("receipt must redact the raw malformed payload")


def validate_declaration(
    run_dir: Path,
    declaration_path: Path,
    receipt_path: Path,
    manifest: dict[str, Any],
    old_commit: str,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    declaration_relative = relative_to_run(
        declaration_path, run_dir, "quarantine declaration"
    )
    receipt_relative = relative_to_run(receipt_path, run_dir, "malformed receipt")
    declaration = load_json(declaration_path, "quarantine declaration")
    receipt = load_json(receipt_path, "malformed-object receipt")
    if declaration.get("schema_version") != DECLARATION_SCHEMA:
        fail("quarantine declaration schema is not rfq-object-quarantine-v1")
    if declaration.get("run_id") != manifest.get("run_id"):
        fail("quarantine declaration run_id mismatch")
    if declaration.get("mode") != "EXPLORATORY_AUTORESEARCH":
        fail("quarantine declaration is not MODE 1")
    if declaration.get("finding") != FINDING:
        fail("quarantine declaration finding must be MALFORMED_NDJSON_OBJECT")
    if declaration.get("disposition") != DECLARATION_DISPOSITION:
        fail("quarantine declaration is not a whole-object disposition")
    if declaration.get("dependent_rfq_result_opened") is not False:
        fail("declaration does not affirm that no dependent RFQ result was opened")
    if declaration.get("created_after_structural_failure_before_rfq_result") is not True:
        fail("declaration timing boundary is missing")
    if declaration.get("source_execution_commit") != old_commit:
        fail("declaration source_execution_commit does not bind the failed revision")
    if declaration.get("remaining_object_parse_policy") != (
        "STRICT_NDJSON_IGNORE_ERRORS_FALSE; any additional malformed object aborts"
    ):
        fail("remaining objects are not held to strict fail-closed parsing")
    selection_rule = declaration.get("selection_rule")
    if not isinstance(selection_rule, str) or "complete manifest object" not in selection_rule:
        fail("declaration lacks deterministic whole-object selection semantics")
    prohibited = declaration.get("result_use_prohibited")
    if not isinstance(prohibited, str) or "no RFQ row" not in prohibited:
        fail("declaration does not prohibit all result use from the object")
    objects = declaration.get("quarantined_objects")
    if not isinstance(objects, list) or len(objects) != 1 or not isinstance(objects[0], dict):
        fail("repair-01 requires exactly one quarantined manifest object")
    obj = copy.deepcopy(objects[0])
    required = (
        "release_id", "key", "manifest_sha256", "version_id", "sha256",
        "size", "receipt", "invalid_line_count", "reason",
    )
    if any(field not in obj for field in required):
        fail("quarantined object declaration is incomplete")
    require_sha(obj.get("manifest_sha256"), "declaration manifest_sha256")
    require_sha(obj.get("sha256"), "declaration object sha256")
    if not isinstance(obj.get("size"), int) or obj["size"] <= 0:
        fail("declared object size is invalid")
    if not isinstance(obj.get("version_id"), str) or not obj["version_id"]:
        fail("declared VersionId is missing")
    if not isinstance(obj.get("key"), str) or not obj["key"].startswith("raw_rfq/"):
        fail("only an exact raw_rfq object may be repaired here")
    if "no line-level salvage" not in str(obj.get("reason")):
        fail("object declaration must prohibit line-level salvage")
    declared_receipt = checked_run_path(run_dir, str(obj["receipt"]), "declared receipt")
    if declared_receipt != receipt_path.resolve() or str(obj["receipt"]) != receipt_relative:
        fail("receipt CLI path does not equal the declaration's run-relative receipt")

    selected = manifest.get("selected_releases")
    if not isinstance(selected, list):
        fail("RUN_MANIFEST selected_releases is missing")
    releases = [row for row in selected if row.get("release_id") == obj["release_id"]]
    if len(releases) != 1:
        fail("declared release is not exactly one selected release")
    release = releases[0]
    if release.get("manifest_sha256") != obj["manifest_sha256"]:
        fail("declaration manifest SHA does not match selected release")
    manifest_path = run_dir / "DATA_INTEGRITY/manifests" / f"{obj['release_id']}.json"
    if not manifest_path.is_file() or sha256(manifest_path) != obj["manifest_sha256"]:
        fail("sealed data manifest bytes do not match the declaration")
    data_manifest = load_json(manifest_path, "sealed release manifest")
    matching = [row for row in data_manifest.get("objects", []) if row.get("key") == obj["key"]]
    if len(matching) != 1:
        fail("declared object key is not unique in the sealed manifest")
    bound = matching[0]
    for field in ("version_id", "sha256", "size"):
        if bound.get(field) != obj.get(field):
            fail(f"declaration {field} does not match sealed manifest object")
    validate_receipt(receipt, obj, str(manifest.get("run_id")))
    # Record where the operator supplied the exact evidence without rewriting it.
    declaration["_input_path"] = declaration_relative
    receipt["_input_path"] = receipt_relative
    return declaration, receipt, [obj]


def parse_trial_registry(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    if not path.is_file():
        fail("TRIAL_REGISTRY.jsonl is missing")
    raw = path.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        fail("TRIAL_REGISTRY.jsonl must be nonempty and newline-terminated")
    records = []
    for line_number, raw_line in enumerate(raw.splitlines(), 1):
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            fail(f"TRIAL_REGISTRY line {line_number} is invalid JSON: {exc}")
        if not isinstance(record, dict):
            fail(f"TRIAL_REGISTRY line {line_number} is not an object")
        records.append(record)
    registration_ids = {
        record.get("trial_registration_id") for record in records
        if record.get("trial_registration_id") is not None
    }
    if "RFQ_FULL_STAGE_ATTEMPT_01" in registration_ids or (
        "RFQ_OBJECT_QUARANTINE_REPAIR_01" in registration_ids
    ):
        fail("repair trial-registration records already exist without manifest binding")
    registered = {record.get("trial_id") for record in records}
    if not set(RFQ_TRIAL_IDS).issubset(registered):
        fail("the three RFQ trials are not present in the frozen registry")
    for record in records:
        stage = str(record.get("stage", "")).upper()
        artifact = str(record.get("result_artifact", ""))
        if stage.startswith("RFQ_FULL") or artifact.endswith(
            "RFQ_FULL_STAGE_SUMMARY.json"
        ):
            fail("an RFQ full-stage registry result/attempt is already recorded")
    return raw, records


def validate_failed_attempt(
    run_dir: Path, quarantined_object: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if (run_dir / RFQ_SUMMARY).exists() or (run_dir / RFQ_REPORT).exists():
        fail("an RFQ full-scan result/report already exists")
    state_path = run_dir / FAILURE_STATE
    resource_path = run_dir / FAILURE_RESOURCE
    scratch_receipt_path = run_dir / FAILURE_SCRATCH_RECEIPT
    state = load_json(state_path, "RFQ failed-stage state")
    resource = load_json(resource_path, "RFQ failed-stage resource receipt")
    scratch_receipt = load_json(
        scratch_receipt_path, "RFQ failed-scratch receipt"
    )
    if state.get("status") != "FAILED_RESUMABLE":
        fail("RFQ state must be FAILED_RESUMABLE")
    if state.get("resume") is not False:
        fail("failed RFQ attempt was not a clean non-resume attempt")
    if not isinstance(state.get("input_fingerprint"), str) or not state[
        "input_fingerprint"
    ]:
        fail("failed RFQ state lacks an input fingerprint")
    if not isinstance(state.get("scratch"), str) or not state["scratch"]:
        fail("failed RFQ state lacks its preserved scratch path")
    error = state.get("error")
    if not isinstance(error, str) or quarantined_object["key"] not in error:
        fail("failed RFQ state is not bound to the declared malformed object")
    if resource.get("return_code") != 1 or resource.get("label") != "rfq_full_stage":
        fail("failed RFQ resource receipt must have label rfq_full_stage/return_code 1")
    command = resource.get("command")
    if not isinstance(command, list) or not command or "--resume" in command:
        fail("failed RFQ resource receipt has an invalid/resumed command")
    if scratch_receipt.get("schema_version") != "rfq-failed-scratch-receipt-v1":
        fail("failed-scratch receipt schema is not v1")
    if scratch_receipt.get("run_id") != run_dir.name:
        fail("failed-scratch receipt run_id mismatch")
    if scratch_receipt.get("original_scratch_path") != state["scratch"]:
        fail("failed-scratch receipt does not bind the failed state scratch")
    preserved = scratch_receipt.get("preserved_scratch_path")
    if (
        not isinstance(preserved, str) or not preserved
        or preserved == state["scratch"]
    ):
        fail("failed scratch was not renamed to a distinct preserved path")
    require_sha(scratch_receipt.get("sha256"), "failed scratch SHA-256")
    if not isinstance(scratch_receipt.get("bytes"), int) or scratch_receipt["bytes"] <= 0:
        fail("failed-scratch receipt byte count is invalid")
    mtime_utc = scratch_receipt.get("mtime_utc")
    if not isinstance(mtime_utc, str) or not (
        mtime_utc.endswith("Z") or mtime_utc.endswith("+00:00")
    ):
        fail("failed-scratch receipt mtime_utc is invalid")
    if scratch_receipt.get("input_fingerprint") != state["input_fingerprint"]:
        fail("failed-scratch receipt input fingerprint mismatch")
    if (
        scratch_receipt.get("disposition") != "PRESERVED_RENAMED_NO_RESUME"
        or scratch_receipt.get("resume_allowed") is not False
    ):
        fail("failed-scratch receipt does not enforce preserve/rename/no-resume")
    return state, resource, scratch_receipt


def core_result_inventory(run_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for relative in CORE_RESULT_PATHS:
        path = run_dir / relative
        if not path.is_file() or path.is_symlink():
            fail(f"required Cycle-1 core result is missing/unsafe: {relative}")
        rows.append({
            "path": relative.as_posix(), "sha256": sha256(path),
            "bytes": path.stat().st_size,
        })
    return rows


def archive_inventory(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            fail(f"pre-repair archive contains a symlink: {path}")
        if path.is_file():
            rows.append({
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256(path), "bytes": path.stat().st_size,
            })
    return rows


def copy_exact(source: Path, target: Path) -> None:
    if not source.is_file():
        fail(f"archive source is missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if sha256(source) != sha256(target):
        fail(f"archive copy verification failed: {source}")


def make_trial_records(
    applied_at: str,
    old_repository: dict[str, Any],
    current_commit: str,
    current_source_sha: str,
    current_query_sha: str,
    declaration_path: str,
    declaration_sha: str,
    receipt_path: str,
    receipt_sha: str,
    failure_state_sha: str,
    failure_resource_sha: str,
    archived_failure_state: str,
    archived_failure_resource: str,
    archived_scratch_receipt: str,
    scratch_receipt_sha: str,
    failed_input_identity_path: str,
    failed_input_identity_sha: str,
    failed_input_fingerprint: str,
    cycle1_duckdb_binding: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    common = {
        "recorded_at_utc": applied_at,
        "trial_ids": list(RFQ_TRIAL_IDS),
        "result_opened": False,
        "hypothesis_conclusion_opened": False,
    }
    failure = {
        **common,
        "trial_registration_id": "RFQ_FULL_STAGE_ATTEMPT_01",
        "record_type": "STAGE_FAILURE",
        "stage": "RFQ_FULL_STAGE",
        "failure_class": FINDING,
        "failure_disposition": "STRUCTURAL_INPUT_FAILURE_BEFORE_RESULT",
        "hypothesis_conclusion": "NONE",
        "receipt_path": receipt_path,
        "receipt_sha256": receipt_sha,
        "failure_state_path": archived_failure_state,
        "failure_state_sha256": failure_state_sha,
        "failure_resource_path": archived_failure_resource,
        "failure_resource_sha256": failure_resource_sha,
        "failed_scratch_receipt_path": archived_scratch_receipt,
        "failed_scratch_receipt_sha256": scratch_receipt_sha,
        "failed_input_identity_path": failed_input_identity_path,
        "failed_input_identity_sha256": failed_input_identity_sha,
        "failed_input_fingerprint": failed_input_fingerprint,
        "cycle1_duckdb_binding": copy.deepcopy(cycle1_duckdb_binding),
        "execution_commit": old_repository["execution_commit"],
        "source_manifest_sha256": old_repository["source_manifest_sha256"],
        "source_sha256s_sha256": old_repository["source_sha256s_sha256"],
        "query_set_sha256": old_repository["query_set_sha256"],
    }
    repair = {
        **common,
        "trial_registration_id": "RFQ_OBJECT_QUARANTINE_REPAIR_01",
        "record_type": "DATA_INTEGRITY_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR_PREREGISTRATION",
        "finding": FINDING,
        "quarantine_policy": QUARANTINE_POLICY,
        "rfq_result_state": NO_RFQ_RESULT,
        "declaration_path": declaration_path,
        "declaration_sha256": declaration_sha,
        "receipt_path": receipt_path,
        "receipt_sha256": receipt_sha,
        "failed_input_identity_path": failed_input_identity_path,
        "failed_input_identity_sha256": failed_input_identity_sha,
        "failed_input_fingerprint": failed_input_fingerprint,
        "cycle1_duckdb_binding": copy.deepcopy(cycle1_duckdb_binding),
        "previous_execution_commit": old_repository["execution_commit"],
        "current_execution_commit": current_commit,
        "previous_source_manifest_sha256": old_repository["source_manifest_sha256"],
        "previous_source_sha256s_sha256": old_repository["source_sha256s_sha256"],
        "current_source_manifest_sha256": current_source_sha,
        "previous_query_set_sha256": old_repository["query_set_sha256"],
        "current_query_set_sha256": current_query_sha,
        "registration_change_class": REGISTRATION_CHANGE_CLASS,
        "hypothesis_design_change": HYPOTHESIS_DESIGN_CHANGE,
        "data_integrity_handling_change": DATA_INTEGRITY_HANDLING_CHANGE,
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "frozen_design_change": LEGACY_FROZEN_DESIGN_CHANGE,
        "core_results_recomputed": False,
    }
    return failure, repair


def validate_already_applied(
    run_dir: Path,
    source_dir: Path,
    declaration_path: Path,
    receipt_path: Path,
    manifest: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    if (
        manifest.get("status") != POST_REPAIR_STATUS
        or record.get("schema_version")
        != "sports-autoresearch-data-integrity-repair-v1"
        or record.get("repair_id") != REPAIR_ID
        or record.get("pre_repair_status") != EXPECTED_STATUS
        or record.get("post_repair_status") != POST_REPAIR_STATUS
    ):
        fail("unknown or inconsistent existing data-integrity repair")
    for supplied, field, label in (
        (declaration_path, "declaration_sha256", "repair declaration"),
        (receipt_path, "receipt_sha256", "malformed-object receipt"),
    ):
        if not supplied.is_file() or sha256(supplied) != record.get(field):
            fail(f"idempotent {label} does not match repair-01")

    repair_dir = run_dir / "DATA_INTEGRITY/repairs" / REPAIR_ID
    archive_root = repair_dir / "pre_repair"
    if (
        record.get("archive_path")
        != f"DATA_INTEGRITY/repairs/{REPAIR_ID}/pre_repair"
        or not archive_root.is_dir()
    ):
        fail("repair-01 pre-repair archive directory is missing")
    recorded_inventory = record.get("archive_inventory")
    if not isinstance(recorded_inventory, list) or not recorded_inventory:
        fail("repair-01 archive inventory is missing")
    seen_inventory_paths: set[str] = set()
    for index, row in enumerate(recorded_inventory):
        if not isinstance(row, dict):
            fail(f"archive inventory row {index} is not an object")
        require_exact_keys(row, {"path", "sha256", "bytes"}, "archive inventory row")
        relative = row.get("path")
        if (
            not isinstance(relative, str)
            or relative in seen_inventory_paths
            or PurePosixPath(relative).is_absolute()
            or any(part in ("", ".", "..") for part in PurePosixPath(relative).parts)
        ):
            fail("archive inventory has an unsafe/duplicate path")
        seen_inventory_paths.add(relative)
        require_sha(row.get("sha256"), f"archive inventory SHA: {relative}")
        if type(row.get("bytes")) is not int or row["bytes"] < 0:
            fail(f"archive inventory byte count is invalid: {relative}")
    actual_inventory = archive_inventory(archive_root)
    if recorded_inventory != actual_inventory:
        fail("repair-01 archive inventory does not exactly match archived files")

    archived_manifest_path = archive_root / "RUN_MANIFEST.json"
    archived_manifest = load_json(archived_manifest_path, "pre-repair RUN_MANIFEST")
    archived_repository = archived_manifest.get("repository")
    if (
        archived_manifest.get("run_id") != manifest.get("run_id")
        or archived_manifest.get("status") != EXPECTED_STATUS
        or not isinstance(archived_repository, dict)
    ):
        fail("pre-repair RUN_MANIFEST identity mismatch")
    query_files = archived_repository.get("query_files")
    if not isinstance(query_files, list) or not query_files:
        fail("pre-repair query file identity is missing")
    expected_archive_paths = {
        "RUN_MANIFEST.json",
        "TRIAL_REGISTRY.jsonl",
        "SOURCE_MANIFEST.json",
        "SOURCE_SHA256SUMS.txt",
        "QUERY_SHA256SUMS.txt",
        FAILURE_STATE.as_posix(),
        FAILURE_RESOURCE.as_posix(),
        FAILURE_SCRATCH_RECEIPT.as_posix(),
        FAILED_INPUT_IDENTITY.as_posix(),
        CYCLE1_DUCKDB_BINDING.as_posix(),
        *query_files,
    }
    if seen_inventory_paths != expected_archive_paths:
        fail("pre-repair archive file set is not exact")

    repository = manifest.get("repository")
    if not isinstance(repository, dict):
        fail("current repository identity is missing")
    active_manifest = run_dir / "SOURCE_MANIFEST.json"
    active_sums = run_dir / "SOURCE_SHA256SUMS.txt"
    active_query_sums = run_dir / "QUERY_SHA256SUMS.txt"
    archived_source_manifest = archive_root / "SOURCE_MANIFEST.json"
    archived_source_sums = archive_root / "SOURCE_SHA256SUMS.txt"
    archived_query_sums = archive_root / "QUERY_SHA256SUMS.txt"
    previous_source = sha256(archived_source_manifest)
    previous_source_sums = sha256(archived_source_sums)
    previous_query = sha256(archived_query_sums)
    current_source = sha256(active_manifest)
    current_source_sums = sha256(active_sums)
    current_query = sha256(active_query_sums)
    previous_identity = {
        "execution_commit": archived_repository.get("execution_commit"),
        "source_manifest_sha256": previous_source,
        "source_sha256s_sha256": previous_source_sums,
        "query_set_sha256": previous_query,
        "query_files": copy.deepcopy(query_files),
    }
    current_identity = {
        "execution_commit": repository.get("execution_commit"),
        "source_manifest_sha256": current_source,
        "source_sha256s_sha256": current_source_sums,
        "query_set_sha256": current_query,
        "query_files": copy.deepcopy(query_files),
    }
    if (
        record.get("previous_repository_identity") != previous_identity
        or record.get("current_repository_identity") != current_identity
        or repository.get("initial_identity") != previous_identity
        or repository.get("previous_identity") != previous_identity
        or repository.get("initial_source_sha256s_sha256") != previous_source_sums
        or repository.get("previous_source_sha256s_sha256") != previous_source_sums
        or record.get("previous_source_sha256s_sha256") != previous_source_sums
        or record.get("current_source_sha256s_sha256") != current_source_sums
        or repository.get("source_sha256s_sha256") != current_source_sums
        or record.get("previous_source_manifest_sha256") != previous_source
        or record.get("current_source_manifest_sha256") != current_source
        or record.get("previous_query_set_sha256") != previous_query
        or record.get("current_query_set_sha256") != current_query
        or repository.get("query_set_sha256") != current_query
        or repository.get("query_files") != query_files
    ):
        fail("current/previous repository identities do not match repair-01")
    verify_source_sums_match_manifest(active_manifest, active_sums, "current")
    verify_source_sums_match_manifest(
        archived_source_manifest, archived_source_sums, "pre_repair"
    )
    if sha256(run_dir / "QUERY_SHA256SUMS.txt") != current_query:
        fail("current query checksum receipt changed")

    for field in ("declaration_path", "receipt_path"):
        archived = checked_run_path(run_dir, str(record.get(field)), field)
        expected_sha = record.get(field.replace("_path", "_sha256"))
        supplied = declaration_path if field == "declaration_path" else receipt_path
        if (
            not archived.is_file()
            or sha256(archived) != expected_sha
            or archived.read_bytes() != supplied.read_bytes()
        ):
            fail(f"applied repair archive is incomplete: {field}")

    repair_receipt_relative = (
        f"DATA_INTEGRITY/repairs/{REPAIR_ID}/REPAIR_REGISTRATION.json"
    )
    if record.get("repair_receipt_path") != repair_receipt_relative:
        fail("repair registration receipt path mismatch")
    repair_receipt_path = checked_run_path(
        run_dir, repair_receipt_relative, "repair registration receipt"
    )
    if (
        not repair_receipt_path.is_file()
        or sha256(repair_receipt_path) != record.get("repair_receipt_sha256")
    ):
        fail("repair registration receipt hash mismatch")
    repair_receipt = load_json(repair_receipt_path, "repair registration receipt")
    record_without_self = copy.deepcopy(record)
    record_without_self.pop("repair_receipt_path", None)
    record_without_self.pop("repair_receipt_sha256", None)
    if repair_receipt != record_without_self:
        fail("repair registration receipt does not exactly bind manifest record")

    failed_input_relative = (
        f"DATA_INTEGRITY/repairs/{REPAIR_ID}/pre_repair/"
        f"{FAILED_INPUT_IDENTITY.as_posix()}"
    )
    if record.get("failed_input_identity_path") != failed_input_relative:
        fail("failed RFQ input identity archive path mismatch")
    archived_failed_input_path = checked_run_path(
        run_dir, failed_input_relative, "archived failed RFQ input identity"
    )
    archived_failed_input, failed_fingerprint = validate_failed_input_identity(
        run_dir, manifest, archived_failed_input_path
    )
    active_failed_input, active_fingerprint = validate_failed_input_identity(
        run_dir, manifest, run_dir / FAILED_INPUT_IDENTITY
    )
    failed_input_sha = sha256(archived_failed_input_path)
    if (
        archived_failed_input != active_failed_input
        or failed_fingerprint != active_fingerprint
        or record.get("failed_input_identity_sha256") != failed_input_sha
        or sha256(run_dir / FAILED_INPUT_IDENTITY) != failed_input_sha
        or record.get("failed_input_fingerprint") != failed_fingerprint
    ):
        fail("active/archive failed RFQ input identities differ")

    active_cycle, cycle_binding = validate_cycle1_duckdb_binding(
        run_dir, run_dir / CYCLE1_DUCKDB_BINDING
    )
    archived_cycle_path = archive_root / CYCLE1_DUCKDB_BINDING
    archived_cycle, archived_cycle_binding = validate_cycle1_duckdb_binding(
        run_dir, archived_cycle_path
    )
    cycle_keys = {
        "active_path", "active_sha256", "archived_path", "archived_sha256",
        "schema_version", "run_id", "duckdb_path", "duckdb_bytes",
        "duckdb_sha256", "core_stage_resource_path",
        "core_stage_resource_sha256", "core_summary_path", "core_summary_sha256",
    }
    recorded_cycle = record.get("cycle1_duckdb_binding")
    if not isinstance(recorded_cycle, dict):
        fail("repair Cycle-1 DuckDB binding is missing")
    require_exact_keys(recorded_cycle, cycle_keys, "repair Cycle-1 DuckDB binding")
    if (
        active_cycle != archived_cycle
        or cycle_binding != archived_cycle_binding
        or recorded_cycle != cycle_binding
    ):
        fail("active/archive/repair Cycle-1 DuckDB bindings differ")

    core_rows = core_result_inventory(run_dir)
    if record.get("core_result_artifacts") != core_rows:
        fail("preserved Cycle-1 core result inventory changed")

    trial_binding = record.get("trial_registry")
    trial_keys = {
        "previous_sha256", "current_sha256", "previous_bytes", "current_bytes",
        "strict_previous_bytes_prefix", "appended_records",
        "trial_registration_ids",
    }
    if not isinstance(trial_binding, dict):
        fail("repair trial-registry binding is missing")
    require_exact_keys(trial_binding, trial_keys, "repair trial-registry binding")
    previous_bytes = require_positive_int(
        trial_binding.get("previous_bytes"), "pre-repair trial-registry bytes"
    )
    current_bytes = require_positive_int(
        trial_binding.get("current_bytes"), "repair trial-registry bytes"
    )
    registry = (run_dir / "TRIAL_REGISTRY.jsonl").read_bytes()
    archived_registry = (archive_root / "TRIAL_REGISTRY.jsonl").read_bytes()
    if (
        current_bytes <= previous_bytes
        or len(registry) < current_bytes
        or registry[:previous_bytes] != archived_registry
        or sha256_bytes(archived_registry) != trial_binding.get("previous_sha256")
        or sha256_bytes(registry[:current_bytes]) != trial_binding.get("current_sha256")
        or trial_binding.get("strict_previous_bytes_prefix") is not True
        or trial_binding.get("appended_records") != 2
        or trial_binding.get("trial_registration_ids")
        != ["RFQ_FULL_STAGE_ATTEMPT_01", "RFQ_OBJECT_QUARANTINE_REPAIR_01"]
    ):
        fail("repair trial-registry prefix binding mismatch")
    try:
        appended = [
            json.loads(line)
            for line in registry[previous_bytes:current_bytes]
            .decode("utf-8")
            .splitlines()
            if line
        ]
    except (UnicodeError, json.JSONDecodeError) as exc:
        fail(f"repair trial-registry appended records are invalid: {exc}")
    if (
        len(appended) != 2
        or any(not isinstance(row, dict) for row in appended)
        or [row.get("trial_registration_id") for row in appended]
        != ["RFQ_FULL_STAGE_ATTEMPT_01", "RFQ_OBJECT_QUARANTINE_REPAIR_01"]
    ):
        fail("repair trial-registry appended record identities mismatch")
    for row in appended:
        if (
            row.get("failed_input_identity_path") != failed_input_relative
            or row.get("failed_input_identity_sha256") != failed_input_sha
            or row.get("failed_input_fingerprint") != failed_fingerprint
            or row.get("cycle1_duckdb_binding") != cycle_binding
            or row.get("result_opened") is not False
            or row.get("hypothesis_conclusion_opened") is not False
        ):
            fail("repair trial record lacks exact input/DB integrity bindings")
    failed_attempt = record.get("failed_attempt")
    if not isinstance(failed_attempt, dict) or (
        failed_attempt.get("failed_input_identity_path") != failed_input_relative
        or failed_attempt.get("failed_input_identity_sha256") != failed_input_sha
        or failed_attempt.get("failed_input_fingerprint") != failed_fingerprint
    ):
        fail("repair failed-attempt input identity binding mismatch")

    repo_root, _, head = verify_clean_source(source_dir)
    del repo_root
    if head != record.get("current_execution_commit"):
        fail("current clean source commit no longer matches repair-01")
    return {
        "status": "REGISTRATION_REPAIR_ALREADY_APPLIED",
        "repair_id": REPAIR_ID,
        "run_id": manifest.get("run_id"),
        "execution_commit": head,
    }


def repair_registration(
    run_dir: Path,
    source_dir: Path,
    quarantine_declaration: Path,
    receipt: Path,
) -> dict[str, Any]:
    """Perform repair-01, or verify and return an already-applied repair."""
    run_dir = run_dir.resolve()
    source_dir = source_dir.resolve()
    declaration_path = quarantine_declaration.resolve()
    receipt_path = receipt.resolve()
    manifest_path = run_dir / "RUN_MANIFEST.json"
    manifest_raw = manifest_path.read_bytes() if manifest_path.is_file() else b""
    manifest = load_json(manifest_path, "RUN_MANIFEST.json")
    if manifest.get("run_id") != run_dir.name:
        fail("RUN_MANIFEST run_id/path mismatch")

    existing = manifest.get("data_integrity_repairs", [])
    if existing is None:
        existing = []
    if not isinstance(existing, list):
        fail("data_integrity_repairs must be an append-only array")
    if existing:
        if len(existing) != 1 or not isinstance(existing[0], dict):
            fail("repair-01 refuses an ambiguous existing repair history")
        return validate_already_applied(
            run_dir, source_dir, declaration_path, receipt_path, manifest, existing[0]
        )

    if manifest.get("status") != EXPECTED_STATUS or manifest.get("analysis_started") is not True:
        fail("run is not in the post-core/pre-RFQ-result repair state")
    if (run_dir / RFQ_SUMMARY).exists() or (run_dir / RFQ_REPORT).exists():
        fail("RFQ full summary/report exists; same-run repair is forbidden")
    repair_root = run_dir / "DATA_INTEGRITY/repairs"
    repair_dir = repair_root / REPAIR_ID
    if repair_dir.exists():
        fail("repair-01 archive exists without manifest record; fail-safe manual audit required")

    repo_root, source_relative, current_commit = verify_clean_source(source_dir)
    old_repository, query_names = verify_prior_repository(
        run_dir, source_dir, repo_root, source_relative, current_commit, manifest
    )
    previous_source_sums_sha = sha256(run_dir / "SOURCE_SHA256SUMS.txt")
    recorded_previous_sums = old_repository.get("source_sha256s_sha256")
    if recorded_previous_sums is not None and (
        recorded_previous_sums != previous_source_sums_sha
    ):
        fail("previous repository source checksum-set binding mismatch")
    # The initial freeze predates this explicit field.  Inject the independently
    # computed receipt hash into every historical identity; never serialize null.
    old_repository["source_sha256s_sha256"] = previous_source_sums_sha

    failed_input_path = run_dir / FAILED_INPUT_IDENTITY
    _, failed_input_fingerprint = validate_failed_input_identity(
        run_dir, manifest, failed_input_path
    )
    failed_input_sha = sha256(failed_input_path)
    _, cycle1_binding = validate_cycle1_duckdb_binding(
        run_dir, run_dir / CYCLE1_DUCKDB_BINDING
    )
    declaration, receipt_json, quarantined_objects = validate_declaration(
        run_dir, declaration_path, receipt_path, manifest,
        str(old_repository["execution_commit"]),
    )
    declaration.pop("_input_path", None)
    receipt_json.pop("_input_path", None)
    state, resource, scratch_receipt_json = validate_failed_attempt(
        run_dir, quarantined_objects[0]
    )
    if state.get("input_fingerprint") != failed_input_fingerprint:
        fail("failed RFQ state fingerprint does not match immutable manifest union")
    del resource, scratch_receipt_json
    trial_path = run_dir / "TRIAL_REGISTRY.jsonl"
    trial_before, _ = parse_trial_registry(trial_path)
    trial_before_sha = sha256_bytes(trial_before)
    core_before = core_result_inventory(run_dir)

    _, new_source_manifest_payload, new_source_sums_payload = build_source_receipts(
        source_dir, repo_root
    )
    new_source_manifest_sha = sha256_bytes(new_source_manifest_payload)
    new_source_sums_sha = sha256_bytes(new_source_sums_payload)
    new_query_rows: list[tuple[str, str, bytes]] = []
    for name in query_names:
        source = source_dir / name
        if not source.is_file():
            fail(f"current registered query source is missing: {name}")
        payload = source.read_bytes()
        new_query_rows.append((sha256_bytes(payload), f"queries/{name}", payload))
    new_query_sums_payload = "".join(
        f"{digest}  {relative}\n" for digest, relative, _ in new_query_rows
    ).encode("utf-8")
    new_query_sha = sha256_bytes(new_query_sums_payload)

    applied_at = now_utc()
    declaration_input_relative = relative_to_run(
        declaration_path, run_dir, "quarantine declaration"
    )
    receipt_input_relative = relative_to_run(receipt_path, run_dir, "malformed receipt")
    declaration_sha = sha256(declaration_path)
    receipt_sha = sha256(receipt_path)
    state_sha = sha256(run_dir / FAILURE_STATE)
    resource_sha = sha256(run_dir / FAILURE_RESOURCE)
    scratch_receipt_sha = sha256(run_dir / FAILURE_SCRATCH_RECEIPT)

    repair_root.mkdir(parents=True, exist_ok=True)
    staging: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{REPAIR_ID}-", dir=repair_root)
    )
    try:
        assert staging is not None
        pre = staging / "pre_repair"
        copy_exact(manifest_path, pre / "RUN_MANIFEST.json")
        copy_exact(trial_path, pre / "TRIAL_REGISTRY.jsonl")
        for relative in (
            Path("SOURCE_MANIFEST.json"), Path("SOURCE_SHA256SUMS.txt"),
            Path("QUERY_SHA256SUMS.txt"), FAILURE_STATE, FAILURE_RESOURCE,
            FAILURE_SCRATCH_RECEIPT, FAILED_INPUT_IDENTITY,
            CYCLE1_DUCKDB_BINDING,
        ):
            copy_exact(run_dir / relative, pre / relative)
        for query_relative in old_repository["query_files"]:
            copy_exact(
                checked_run_path(run_dir, query_relative, "old query copy"),
                pre / Path(query_relative),
            )
        copy_exact(declaration_path, staging / "QUARANTINE_DECLARATION.json")
        copy_exact(receipt_path, staging / "MALFORMED_OBJECT_RECEIPT.json")

        post = staging / "post_repair"
        atomic_write(post / "SOURCE_MANIFEST.json", new_source_manifest_payload)
        atomic_write(post / "SOURCE_SHA256SUMS.txt", new_source_sums_payload)
        atomic_write(post / "QUERY_SHA256SUMS.txt", new_query_sums_payload)
        for _, relative, payload in new_query_rows:
            atomic_write(post / Path(relative), payload)

        archive_rows = archive_inventory(pre)
        archived_declaration = (
            f"DATA_INTEGRITY/repairs/{REPAIR_ID}/QUARANTINE_DECLARATION.json"
        )
        archived_receipt = (
            f"DATA_INTEGRITY/repairs/{REPAIR_ID}/MALFORMED_OBJECT_RECEIPT.json"
        )
        archived_failure_state = (
            f"DATA_INTEGRITY/repairs/{REPAIR_ID}/pre_repair/"
            f"{FAILURE_STATE.as_posix()}"
        )
        archived_failure_resource = (
            f"DATA_INTEGRITY/repairs/{REPAIR_ID}/pre_repair/"
            f"{FAILURE_RESOURCE.as_posix()}"
        )
        archived_scratch_receipt = (
            f"DATA_INTEGRITY/repairs/{REPAIR_ID}/pre_repair/"
            f"{FAILURE_SCRATCH_RECEIPT.as_posix()}"
        )
        archived_failed_input = (
            f"DATA_INTEGRITY/repairs/{REPAIR_ID}/pre_repair/"
            f"{FAILED_INPUT_IDENTITY.as_posix()}"
        )
        failure_record, repair_trial_record = make_trial_records(
            applied_at, old_repository, current_commit, new_source_manifest_sha,
            new_query_sha, archived_declaration, declaration_sha, archived_receipt,
            receipt_sha, state_sha, resource_sha, archived_failure_state,
            archived_failure_resource, archived_scratch_receipt,
            scratch_receipt_sha, archived_failed_input, failed_input_sha,
            failed_input_fingerprint, cycle1_binding,
        )
        trial_after = trial_before + registry_line(failure_record) + registry_line(
            repair_trial_record
        )
        trial_after_sha = sha256_bytes(trial_after)

        record: dict[str, Any] = {
            "schema_version": "sports-autoresearch-data-integrity-repair-v1",
            "repair_id": REPAIR_ID,
            "applied_at_utc": applied_at,
            "finding": FINDING,
            "pre_repair_status": EXPECTED_STATUS,
            "post_repair_status": POST_REPAIR_STATUS,
            "rfq_result_state": NO_RFQ_RESULT,
            "quarantine_policy": QUARANTINE_POLICY,
            "quarantined_objects": copy.deepcopy(quarantined_objects),
            "declaration_input_path": declaration_input_relative,
            "receipt_input_path": receipt_input_relative,
            "declaration_path": archived_declaration,
            "declaration_sha256": declaration_sha,
            "receipt_path": archived_receipt,
            "receipt_sha256": receipt_sha,
            "failed_state_path": archived_failure_state,
            "failed_state_sha256": state_sha,
            "failed_resource_receipt_path": archived_failure_resource,
            "failed_resource_receipt_sha256": resource_sha,
            "failed_scratch_receipt_path": archived_scratch_receipt,
            "failed_scratch_receipt_sha256": scratch_receipt_sha,
            "failed_input_identity_path": archived_failed_input,
            "failed_input_identity_sha256": failed_input_sha,
            "failed_input_fingerprint": failed_input_fingerprint,
            "cycle1_duckdb_binding": copy.deepcopy(cycle1_binding),
            "previous_execution_commit": old_repository["execution_commit"],
            "current_execution_commit": current_commit,
            "previous_source_manifest_sha256": old_repository[
                "source_manifest_sha256"
            ],
            "current_source_manifest_sha256": new_source_manifest_sha,
            "previous_source_sha256s_sha256": previous_source_sums_sha,
            "current_source_sha256s_sha256": new_source_sums_sha,
            "previous_query_set_sha256": old_repository["query_set_sha256"],
            "current_query_set_sha256": new_query_sha,
            "previous_repository_identity": repository_identity(old_repository),
            "current_repository_identity": {
                "execution_commit": current_commit,
                "source_manifest_sha256": new_source_manifest_sha,
                "source_sha256s_sha256": new_source_sums_sha,
                "query_set_sha256": new_query_sha,
                "query_files": copy.deepcopy(old_repository["query_files"]),
            },
            "core_result_disposition": CORE_DISPOSITION,
            "core_results_recomputed": False,
            "core_result_artifacts": core_before,
            "failed_attempt": {
                "state_path": FAILURE_STATE.as_posix(),
                "state_sha256": state_sha,
                "archived_state_path": archived_failure_state,
                "state_status": "FAILED_RESUMABLE",
                "resource_path": FAILURE_RESOURCE.as_posix(),
                "resource_sha256": resource_sha,
                "archived_resource_path": archived_failure_resource,
                "return_code": 1,
                "old_scratch": state["scratch"],
                "scratch_receipt_path": archived_scratch_receipt,
                "scratch_receipt_sha256": scratch_receipt_sha,
                "scratch_disposition": "PRESERVED_RENAMED_NO_RESUME",
                "retry_requirement": "NEW_SCRATCH_NEW_FINGERPRINT_NO_RESUME",
                "failed_input_identity_path": archived_failed_input,
                "failed_input_identity_sha256": failed_input_sha,
                "failed_input_fingerprint": failed_input_fingerprint,
            },
            "trial_registry": {
                "previous_sha256": trial_before_sha,
                "current_sha256": trial_after_sha,
                "previous_bytes": len(trial_before),
                "current_bytes": len(trial_after),
                "strict_previous_bytes_prefix": True,
                "appended_records": 2,
                "trial_registration_ids": [
                    "RFQ_FULL_STAGE_ATTEMPT_01",
                    "RFQ_OBJECT_QUARANTINE_REPAIR_01",
                ],
            },
            "registration_change_class": REGISTRATION_CHANGE_CLASS,
            "hypothesis_design_change": HYPOTHESIS_DESIGN_CHANGE,
            "data_integrity_handling_change": DATA_INTEGRITY_HANDLING_CHANGE,
            "frozen_design_change": LEGACY_FROZEN_DESIGN_CHANGE,
            "threshold_feature_test_or_hypothesis_status_changed": False,
            "archive_path": f"DATA_INTEGRITY/repairs/{REPAIR_ID}/pre_repair",
            "archive_inventory": archive_rows,
        }
        atomic_write(staging / "REPAIR_REGISTRATION.json", json_payload(record))
        repair_receipt_sha = sha256(staging / "REPAIR_REGISTRATION.json")
        record["repair_receipt_path"] = (
            f"DATA_INTEGRITY/repairs/{REPAIR_ID}/REPAIR_REGISTRATION.json"
        )
        record["repair_receipt_sha256"] = repair_receipt_sha

        # Move the immutable audit directory into place before replacing any
        # active receipt.  A crash can therefore never destroy the old identity.
        os.replace(staging, repair_dir)
        staging = None  # ownership transferred; suppress cleanup below

        new_repository = copy.deepcopy(old_repository)
        initial_execution = old_repository.get(
            "initial_execution_commit", old_repository["execution_commit"]
        )
        if initial_execution != old_repository["execution_commit"]:
            fail("repair-01 initial_execution_commit is ambiguous")
        new_repository.update({
            "initial_execution_commit": initial_execution,
            "initial_source_manifest_sha256": old_repository[
                "source_manifest_sha256"
            ],
            "initial_source_sha256s_sha256": record[
                "previous_source_sha256s_sha256"
            ],
            "initial_query_set_sha256": old_repository["query_set_sha256"],
            "previous_execution_commit": old_repository["execution_commit"],
            "previous_source_manifest_sha256": old_repository[
                "source_manifest_sha256"
            ],
            "previous_source_sha256s_sha256": record[
                "previous_source_sha256s_sha256"
            ],
            "previous_query_set_sha256": old_repository["query_set_sha256"],
            "initial_identity": repository_identity(old_repository),
            "previous_identity": repository_identity(old_repository),
            "execution_commit": current_commit,
            "source_manifest_sha256": new_source_manifest_sha,
            "source_sha256s_sha256": new_source_sums_sha,
            "query_set_sha256": new_query_sha,
            "source_tree_dirty_at_freeze": False,
            "registration_repair_id": REPAIR_ID,
        })
        updated_manifest = copy.deepcopy(manifest)
        updated_manifest["repository"] = new_repository
        updated_manifest["data_integrity_repairs"] = [record]
        updated_manifest["status"] = POST_REPAIR_STATUS
        updated_manifest["registration_state"] = (
            "RE_FROZEN_AFTER_DATA_INTEGRITY_REPAIR_BEFORE_RFQ_RESULT"
        )

        # Materialize the new active receipts.  RUN_MANIFEST is intentionally
        # last, so readers never see a new repository binding before its files.
        atomic_write(
            run_dir / "SOURCE_MANIFEST.json",
            (repair_dir / "post_repair/SOURCE_MANIFEST.json").read_bytes(),
        )
        atomic_write(
            run_dir / "SOURCE_SHA256SUMS.txt",
            (repair_dir / "post_repair/SOURCE_SHA256SUMS.txt").read_bytes(),
        )
        atomic_write(
            run_dir / "QUERY_SHA256SUMS.txt",
            (repair_dir / "post_repair/QUERY_SHA256SUMS.txt").read_bytes(),
        )
        new_queries = run_dir / f".queries.{REPAIR_ID}.new"
        old_queries = run_dir / f".queries.{REPAIR_ID}.old"
        if new_queries.exists() or old_queries.exists():
            fail("stale query transaction directory exists")
        shutil.copytree(repair_dir / "post_repair/queries", new_queries)
        os.replace(run_dir / "queries", old_queries)
        try:
            os.replace(new_queries, run_dir / "queries")
        except BaseException:
            os.replace(old_queries, run_dir / "queries")
            raise
        atomic_write(trial_path, trial_after)

        if manifest_path.read_bytes() != manifest_raw:
            fail("RUN_MANIFEST changed concurrently; refusing to overwrite")
        if not trial_path.read_bytes().startswith(trial_before) or (
            len(trial_path.read_bytes().splitlines())
            != len(trial_before.splitlines()) + 2
        ):
            fail("TRIAL_REGISTRY append-only invariant failed")
        if core_result_inventory(run_dir) != core_before:
            fail("a pre-existing core result changed during registration repair")
        if sha256(run_dir / "SOURCE_MANIFEST.json") != new_source_manifest_sha:
            fail("new SOURCE_MANIFEST materialization failed")
        if sha256(run_dir / "QUERY_SHA256SUMS.txt") != new_query_sha:
            fail("new QUERY_SHA materialization failed")
        atomic_write(manifest_path, json_payload(updated_manifest))
        if old_queries.exists():
            shutil.rmtree(old_queries)
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)

    return {
        "status": "REGISTRATION_REPAIRED_AND_REFROZEN",
        "repair_id": REPAIR_ID,
        "run_id": manifest["run_id"],
        "previous_execution_commit": old_repository["execution_commit"],
        "execution_commit": current_commit,
        "quarantined_objects": len(quarantined_objects),
        "rfq_result_state": NO_RFQ_RESULT,
    }


# A short alias is useful to callers and keeps the CLI implementation trivial.
repair = repair_registration


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Append-only post-core registration repair for an exact "
            "manifest-bound malformed RFQ object"
        )
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--quarantine-declaration", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    result = repair_registration(
        args.run_dir, args.source_dir, args.quarantine_declaration, args.receipt
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
