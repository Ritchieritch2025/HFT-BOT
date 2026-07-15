#!/usr/bin/env python3
"""Append-only preregistration for the second RFQ whole-object quarantine.

This command is intentionally specific to ``SPORTS-AUTORESEARCH-01`` repair-02.
It may run only after the registered repair-01 retry failed on one additional
manifest-bound malformed object and before any RFQ result was opened.  It
validates the complete structural audit and the operator's explicit authority,
archives attempt-02 byte-for-byte, appends two trial records, and re-freezes a
new committed source/query identity.  It never reads research outcomes or runs
an analysis.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


try:  # Normal CLI execution from this directory.
    import repair_registration as base
except ModuleNotFoundError:  # Direct importlib loading in unit tests.
    _BASE_PATH = Path(__file__).with_name("repair_registration.py")
    _BASE_SPEC = importlib.util.spec_from_file_location(
        "repair_registration", _BASE_PATH
    )
    if _BASE_SPEC is None or _BASE_SPEC.loader is None:  # pragma: no cover
        raise
    base = importlib.util.module_from_spec(_BASE_SPEC)
    _BASE_SPEC.loader.exec_module(base)


Repair02RegistrationError = base.RepairRegistrationError

REPAIR_ID = "repair-02"
PARENT_REPAIR_ID = "repair-01"
PARENT_EXECUTION_COMMIT = "b0f9cc202d460eef1aabf85dae4c1295492e7003"
EXPECTED_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR_REGISTERED"
POST_REPAIR_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR02_REGISTERED"
REPAIR_SCHEMA = "sports-autoresearch-data-integrity-repair-v2"
DECLARATION_SCHEMA = "rfq-object-quarantine-v2"
AUTHORIZATION_SCHEMA = "sports-autoresearch-repair-authorization-v1"
STRICT_PARSE_POLICY = (
    "STRICT_NDJSON_IGNORE_ERRORS_FALSE; any additional malformed object aborts"
)
RETRY_REQUIREMENT = "NEW_SCRATCH_NEW_FINGERPRINT_NO_RESUME"
NO_RFQ_RESULT = "NO_RFQ_RESULT_OPENED"
CORE_DISPOSITION = "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED"

STATE = Path("REPORT/tables/RFQ_FULL_STAGE_STATE.json")
RESOURCE = Path("logs/resources/rfq_full_stage_repair01.json")
SCRATCH_RECEIPT = Path("DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_02.json")
FAILED_INPUT_IDENTITY = Path("DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json")
STRUCTURAL_AUDIT = Path("DATA_INTEGRITY/RFQ_STRUCTURAL_INTEGRITY_AUDIT_02.json")
STRUCTURAL_RESOURCE = Path("logs/resources/rfq_structural_integrity_audit02.json")
BLOCKER = Path("DATA_INTEGRITY/RFQ_SECOND_MALFORMED_OBJECT_BLOCKER.json")
SESSION_RESUME = Path("DATA_INTEGRITY/SESSION_RESUME_02.json")
CYCLE1_BINDING = Path("DATA_INTEGRITY/CYCLE1_DUCKDB_BINDING.json")
RFQ_SUMMARY = Path("REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json")
RFQ_REPORT = Path("REPORT/RFQ_FULL_STAGE.md")

TOTAL_OBJECTS = 284
TOTAL_LOGICAL_BINDINGS = 296
TOTAL_BYTES = 59_719_895_414
RETAINED_OBJECTS = 282
RETAINED_LOGICAL_BINDINGS = 294
RETAINED_BYTES = 59_185_856_724
QUARANTINED_OBJECTS = 2
QUARANTINED_LOGICAL_BINDINGS = 2
QUARANTINED_BYTES = 534_038_690
TOTAL_LINES = 58_690_563
FULL_FINGERPRINT = (
    "1873803765e70de69f4b398dcea7e4dc66c2749950c9d5f8c0d2d198c5087c71"
)
RETAINED_OBJECT_FINGERPRINT = (
    "cf6885a13ac50369fbfb19aba3809cd7e83c8bdabe4381ec926ed1c61d47b652"
)
QUARANTINED_OBJECT_FINGERPRINT = (
    "cf0e874f65aad791c5a23164a34511bd0482d7ece9de0706841c5b8bc92808eb"
)
SECOND_KEY = "raw_rfq/date=2026-07-13/rfq_23.ndjson.2"
SECOND_SIZE = 265_604_615
SECOND_SHA = (
    "038297c3061a0a7a6a41e545f523880bd44a6bd628582d2a9438ea9f96d61bde"
)
SECOND_VERSION_ID = "8XVpYQtHi_Vfvy0UAbocNIZdAb0O3j2q"
SECOND_RELEASE = "2026-07-13__seal-7f6e5c1b__pub-f8e4c0abc742b7d5"
SECOND_MANIFEST_SHA = (
    "1662fb21148c068f2a53bf6f30597b9c26230f2d72fa722b2b849fd490085ddd"
)
TRIAL_IDS = (
    "RFQ_FULL_STAGE_ATTEMPT_02",
    "RFQ_OBJECT_QUARANTINE_REPAIR_02",
)
TRANSACTION_JOURNAL = "TRANSACTION_JOURNAL.json"


def _transaction_fault_hook(boundary: str) -> None:
    """Unit-test hook; production execution intentionally does nothing."""
    del boundary


def fail(message: str) -> None:
    raise Repair02RegistrationError(message)


def canonical_hash(value: Any) -> str:
    return base.sha256_bytes(base.json_payload(value))


def object_fingerprint(objects: list[dict[str, Any]]) -> str:
    ordered = sorted(objects, key=lambda row: row["key"])
    return base.rfq_object_fingerprint(ordered)


def retained_selection_fingerprint(
    full_fingerprint: str,
    retained_fingerprint: str,
    quarantined_fingerprint: str,
    declaration_sha256: str,
    receipt_sha256s: list[str],
) -> str:
    payload = {
        "schema": "rfq-partial-object-selection-v2",
        "total": full_fingerprint,
        "retained": retained_fingerprint,
        "quarantined": quarantined_fingerprint,
        "declaration": declaration_sha256,
        "receipts": receipt_sha256s,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def load(path: Path, label: str) -> dict[str, Any]:
    return base.load_json(path, label)


def verify_bound_file(
    run_dir: Path, relative: str, expected_sha: Any, label: str
) -> Path:
    digest = base.require_sha(expected_sha, f"{label} SHA-256")
    path = base.checked_run_path(run_dir, relative, label)
    if not path.is_file() or base.sha256(path) != digest:
        fail(f"{label} path/hash binding mismatch")
    return path


def _safe_relative_file_set(root: Path) -> set[str]:
    output: set[str] = set()
    if not root.is_dir() or root.is_symlink():
        fail("repair-02 transaction directory is missing or unsafe")
    for path in root.rglob("*"):
        if path.is_symlink():
            fail("repair-02 transaction directory contains a symlink")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if relative in output:
                fail("repair-02 transaction directory has duplicate paths")
            output.add(relative)
    return output


def recover_incomplete_transaction(run_dir: Path, repair_dir: Path) -> list[str]:
    """Restore an interrupted repair-02 publication from its immutable journal.

    Every original byte payload lives under ``pre_repair``.  A path is restored
    only when its active bytes are either the journaled original or the exact
    repair-02 replacement.  Append-only concurrent registry suffixes are
    retained while the two repair-02 records are removed.  Unknown mutations
    are never overwritten.
    """
    journal_path = repair_dir / TRANSACTION_JOURNAL
    journal = load(journal_path, "repair-02 transaction journal")
    if (
        journal.get("schema_version") != "repair02-registration-transaction-v1"
        or journal.get("repair_id") != REPAIR_ID
        or journal.get("run_id") != run_dir.name
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
    ):
        fail("orphan repair-02 directory lacks a valid recovery journal")
    expected_files = journal.get("expected_repair_files")
    mutations = journal.get("active_mutations")
    if (
        not isinstance(expected_files, list)
        or not expected_files
        or len(expected_files) != len(set(expected_files))
        or not isinstance(mutations, list)
        or not mutations
        or _safe_relative_file_set(repair_dir) != set(expected_files)
    ):
        fail("orphan repair-02 transaction file set mismatch")
    manifest_path = run_dir / "RUN_MANIFEST.json"
    original_manifest_sha = base.require_sha(
        journal.get("original_manifest_sha256"),
        "transaction original RUN_MANIFEST SHA",
    )
    if not manifest_path.is_file() or base.sha256(manifest_path) != original_manifest_sha:
        fail("cannot recover repair-02: RUN_MANIFEST is not the parent identity")

    parsed: list[tuple[Path, Path, bytes, str, str, bool]] = []
    seen: set[str] = set()
    for index, row in enumerate(mutations):
        if not isinstance(row, dict):
            fail(f"transaction mutation {index} is not an object")
        base.require_exact_keys(
            row,
            {
                "path", "original_archive_path", "original_sha256",
                "replacement_sha256", "append_only_registry",
            },
            f"transaction mutation {index}",
        )
        relative = row.get("path")
        archive_relative = row.get("original_archive_path")
        if (
            not isinstance(relative, str)
            or relative in seen
            or not isinstance(archive_relative, str)
        ):
            fail("transaction mutation paths are invalid or duplicated")
        seen.add(relative)
        active = base.checked_run_path(run_dir, relative, "transaction active path")
        archived = base.checked_run_path(
            run_dir, archive_relative, "transaction original archive"
        )
        try:
            archived.relative_to(repair_dir.resolve())
        except ValueError:
            fail("transaction original archive escapes repair-02")
        original_sha = base.require_sha(
            row.get("original_sha256"), "transaction original SHA"
        )
        replacement_sha = base.require_sha(
            row.get("replacement_sha256"), "transaction replacement SHA"
        )
        if not archived.is_file() or base.sha256(archived) != original_sha:
            fail("transaction original archive hash mismatch")
        original = archived.read_bytes()
        if base.sha256_bytes(original) != original_sha:
            fail("transaction original payload hash mismatch")
        parsed.append(
            (
                active, archived, original, original_sha, replacement_sha,
                row.get("append_only_registry") is True,
            )
        )

    concurrent: list[str] = []
    for active, _, original, original_sha, replacement_sha, append_only in parsed:
        if not active.is_file():
            fail(f"transaction active file disappeared: {active}")
        current = active.read_bytes()
        current_sha = base.sha256_bytes(current)
        restored: bytes | None = None
        if current_sha == original_sha:
            continue
        if current_sha == replacement_sha:
            restored = original
        elif append_only:
            replacement_path = repair_dir / "post_repair/TRIAL_REGISTRY.jsonl"
            if not replacement_path.is_file():
                fail("transaction replacement registry payload is missing")
            replacement = replacement_path.read_bytes()
            if not replacement.startswith(original):
                fail("transaction replacement registry is not append-only")
            own_append = replacement[len(original):]
            suffix = current[len(original):] if current.startswith(original) else b""
            occurrences = suffix.count(own_append)
            if not current.startswith(original) or occurrences > 1:
                fail("unrecognized concurrent TRIAL_REGISTRY mutation")
            if occurrences == 0:
                # A writer appended before our second CAS.  Our append was not
                # installed, so retain its suffix verbatim.
                concurrent.append(active.relative_to(run_dir).as_posix())
                continue
            restored = original + suffix.replace(own_append, b"", 1)
            if restored != original:
                concurrent.append(active.relative_to(run_dir).as_posix())
        else:
            fail(f"unrecognized concurrent active mutation: {active}")
        if append_only:
            # Coordinate with every repair writer and mutate the same inode;
            # unlike rename-based replacement this cannot silently detach an
            # append that landed on the active inode.
            descriptor = os.open(active, os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                stat_path = active.stat()
                stat_fd = os.fstat(descriptor)
                if (stat_path.st_dev, stat_path.st_ino) != (
                    stat_fd.st_dev, stat_fd.st_ino
                ):
                    fail("TRIAL_REGISTRY inode changed during rollback")
                os.lseek(descriptor, 0, os.SEEK_SET)
                locked_current = os.read(descriptor, stat_fd.st_size + 1)
                if locked_current != current:
                    fail("TRIAL_REGISTRY changed during rollback lock acquisition")
                os.ftruncate(descriptor, 0)
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.write(descriptor, restored) != len(restored):
                    fail("TRIAL_REGISTRY rollback write was partial")
                os.fsync(descriptor)
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
        else:
            base.atomic_write(active, restored)

    # Verify that no repair-02 replacement remains active.  A concurrent
    # append-only suffix is allowed only on the registry path handled above.
    for active, _, original, original_sha, _, append_only in parsed:
        current = active.read_bytes()
        if append_only and current.startswith(original):
            continue
        if base.sha256_bytes(current) != original_sha:
            fail(f"repair-02 rollback verification failed: {active}")
    shutil.rmtree(repair_dir)
    if repair_dir.exists():
        fail("repair-02 orphan directory cleanup failed")
    return concurrent


def validate_parent_repair(
    run_dir: Path, manifest: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], str]:
    repairs = manifest.get("data_integrity_repairs")
    if not isinstance(repairs, list) or len(repairs) != 1:
        fail("repair-02 requires exactly one immutable parent repair")
    parent = repairs[0]
    if (
        not isinstance(parent, dict)
        or parent.get("schema_version")
        != "sports-autoresearch-data-integrity-repair-v1"
        or parent.get("repair_id") != PARENT_REPAIR_ID
        or parent.get("current_execution_commit") != PARENT_EXECUTION_COMMIT
        or parent.get("post_repair_status") != EXPECTED_STATUS
        or manifest.get("status") != EXPECTED_STATUS
    ):
        fail("repair-01 identity/status is not the registered parent")

    repository = manifest.get("repository")
    parent_identity = parent.get("current_repository_identity")
    if (
        not isinstance(repository, dict)
        or not isinstance(parent_identity, dict)
        or repository.get("execution_commit") != PARENT_EXECUTION_COMMIT
        or base.repository_identity(repository) != parent_identity
        or repository.get("registration_repair_id") != PARENT_REPAIR_ID
    ):
        fail("active repository identity is not repair-01")

    parent_receipt_relative = (
        "DATA_INTEGRITY/repairs/repair-01/REPAIR_REGISTRATION.json"
    )
    if parent.get("repair_receipt_path") != parent_receipt_relative:
        fail("repair-01 registration receipt path mismatch")
    parent_receipt = verify_bound_file(
        run_dir,
        parent_receipt_relative,
        parent.get("repair_receipt_sha256"),
        "repair-01 registration receipt",
    )
    receipt_json = load(parent_receipt, "repair-01 registration receipt")
    parent_without_self = copy.deepcopy(parent)
    parent_without_self.pop("repair_receipt_path", None)
    parent_without_self.pop("repair_receipt_sha256", None)
    if receipt_json != parent_without_self:
        fail("repair-01 receipt does not exactly bind its manifest record")

    archive_relative = "DATA_INTEGRITY/repairs/repair-01/pre_repair"
    if parent.get("archive_path") != archive_relative:
        fail("repair-01 archive path mismatch")
    archive = base.checked_run_path(run_dir, archive_relative, "repair-01 archive")
    if parent.get("archive_inventory") != base.archive_inventory(archive):
        fail("repair-01 archive inventory changed")
    if parent.get("core_result_artifacts") != base.core_result_inventory(run_dir):
        fail("Cycle-1 core artifacts changed after repair-01")

    trial_binding = parent.get("trial_registry")
    registry = (run_dir / "TRIAL_REGISTRY.jsonl").read_bytes()
    if (
        not isinstance(trial_binding, dict)
        or trial_binding.get("trial_registration_ids")
        != ["RFQ_FULL_STAGE_ATTEMPT_01", "RFQ_OBJECT_QUARANTINE_REPAIR_01"]
        or trial_binding.get("appended_records") != 2
        or trial_binding.get("strict_previous_bytes_prefix") is not True
        or trial_binding.get("current_bytes") != len(registry)
        or trial_binding.get("current_sha256") != base.sha256_bytes(registry)
    ):
        fail("TRIAL_REGISTRY is not the exact repair-01 append boundary")

    parent_objects = parent.get("quarantined_objects")
    if (
        not isinstance(parent_objects, list)
        or len(parent_objects) != 1
        or not isinstance(parent_objects[0], dict)
    ):
        fail("repair-01 whole-object quarantine is ambiguous")
    parent_declaration = verify_bound_file(
        run_dir,
        str(parent.get("declaration_path")),
        parent.get("declaration_sha256"),
        "repair-01 declaration",
    )
    parent_receipt_evidence = verify_bound_file(
        run_dir,
        str(parent.get("receipt_path")),
        parent.get("receipt_sha256"),
        "repair-01 malformed receipt",
    )
    old_declaration = load(parent_declaration, "repair-01 declaration")
    if old_declaration.get("quarantined_objects") != parent_objects:
        fail("repair-01 declaration/object binding changed")
    if base.sha256(parent_receipt_evidence) != parent.get("receipt_sha256"):
        fail("repair-01 malformed receipt changed")

    original_identity_path = base.checked_run_path(
        run_dir,
        str(parent.get("failed_input_identity_path")),
        "repair-01 full input identity",
    )
    original_identity, full_fingerprint = base.validate_failed_input_identity(
        run_dir, manifest, original_identity_path
    )
    if (
        base.sha256(original_identity_path)
        != parent.get("failed_input_identity_sha256")
        or full_fingerprint != FULL_FINGERPRINT
        or original_identity.get("unique_objects") != TOTAL_OBJECTS
        or sum(row["size"] for row in original_identity["objects"]) != TOTAL_BYTES
        or original_identity.get("logical_manifest_bindings")
        != TOTAL_LOGICAL_BINDINGS
    ):
        fail("repair-01 full RFQ object-set identity mismatch")

    _, cycle_binding = base.validate_cycle1_duckdb_binding(
        run_dir, run_dir / CYCLE1_BINDING
    )
    if parent.get("cycle1_duckdb_binding") != cycle_binding:
        fail("repair-01 Cycle-1 DuckDB binding changed")
    return parent, original_identity, parent_objects, canonical_hash(parent)


def parse_repair01_trial_registry(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    """Parse the exact repair-01 boundary while rejecting any later append."""
    if not path.is_file():
        fail("TRIAL_REGISTRY.jsonl is missing")
    raw = path.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        fail("TRIAL_REGISTRY.jsonl must be nonempty and newline-terminated")
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(raw.splitlines(), 1):
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            fail(f"TRIAL_REGISTRY line {line_number} is invalid JSON: {exc}")
        if not isinstance(row, dict):
            fail(f"TRIAL_REGISTRY line {line_number} is not an object")
        records.append(row)
    registered = {row.get("trial_id") for row in records}
    if not set(base.RFQ_TRIAL_IDS).issubset(registered):
        fail("the three frozen RFQ trials are missing")
    registration_ids = [
        row.get("trial_registration_id") for row in records
        if row.get("trial_registration_id") is not None
    ]
    if registration_ids != [
        "RFQ_FULL_STAGE_ATTEMPT_01",
        "RFQ_OBJECT_QUARANTINE_REPAIR_01",
    ]:
        fail("TRIAL_REGISTRY is not exactly at the repair-01 append boundary")
    if any(
        str(row.get("result_artifact", "")).endswith("RFQ_FULL_STAGE_SUMMARY.json")
        for row in records
    ):
        fail("TRIAL_REGISTRY already contains an RFQ full-stage result")
    return raw, records


def validate_session_resume(
    run_dir: Path, manifest: dict[str, Any], parent: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Bind the cross-session continuity proof required by the mission."""
    resumes = manifest.get("session_resumes")
    if not isinstance(resumes, list) or len(resumes) != 1:
        fail("repair-02 requires exactly one session-resume record")
    record = resumes[0]
    if not isinstance(record, dict):
        fail("session-resume record is not an object")
    evidence_sha = base.require_sha(
        record.get("evidence_sha256"), "session-resume evidence SHA"
    )
    if (
        record.get("session_id") != "session-02-repair02-authorized"
        or record.get("evidence_path") != SESSION_RESUME.as_posix()
    ):
        fail("session-resume record identity mismatch")
    base.require_utc_timestamp(record.get("resumed_at_utc"), "session resumed_at")
    evidence_path = verify_bound_file(
        run_dir, SESSION_RESUME.as_posix(), evidence_sha, "session-resume evidence"
    )
    evidence = load(evidence_path, "session-resume evidence")
    identities = evidence.get("verified_identities")
    record_identities = record.get("verified_identities")
    if (
        evidence.get("schema_version") != "sports-autoresearch-session-resume-v1"
        or evidence.get("run_id") != run_dir.name
        or evidence.get("session_id") != record.get("session_id")
        or evidence.get("resumed_at_utc") != record.get("resumed_at_utc")
        or not isinstance(identities, dict)
        or not isinstance(record_identities, dict)
        or identities.get("mission_sha256")
        != "9b4ca417dca394223ecdc6719cd1628bd5ad69d5a80003db304f73b1c485a69c"
        or identities.get("canonical_prompt_sha256")
        != "575ea27ae08477bdcc4234a42ad5ff456c4ad45cd32ede79011caec2be3fbe54"
        or identities.get("repository_execution_commit") != PARENT_EXECUTION_COMMIT
        or identities.get("source_manifest_sha256")
        != parent.get("current_source_manifest_sha256")
        or identities.get("source_sha256s_sha256")
        != parent.get("current_source_sha256s_sha256")
        or identities.get("query_set_sha256")
        != parent.get("current_query_set_sha256")
        or identities.get("repair01_registration_sha256")
        != parent.get("repair_receipt_sha256")
        or identities.get("active_failed_rfq_input_identity_sha256")
        != base.sha256(run_dir / FAILED_INPUT_IDENTITY)
        or identities.get("structural_audit_sha256")
        != base.sha256(run_dir / STRUCTURAL_AUDIT)
        or identities.get("trial_registry_sha256")
        != base.sha256(run_dir / "TRIAL_REGISTRY.jsonl")
        or identities.get("trial_registry_bytes")
        != (run_dir / "TRIAL_REGISTRY.jsonl").stat().st_size
        or identities.get("trial_registry_records")
        != len((run_dir / "TRIAL_REGISTRY.jsonl").read_bytes().splitlines())
        or identities.get("remote_source_checksum_verification") != "PASS"
        or identities.get("remote_query_checksum_verification") != "PASS"
    ):
        fail("session-resume evidence identity chain mismatch")
    projected = {
        "mission_sha256": identities["mission_sha256"],
        "canonical_prompt_sha256": identities["canonical_prompt_sha256"],
        "repository_execution_commit": identities["repository_execution_commit"],
        "source_manifest_sha256": identities["source_manifest_sha256"],
        "source_sha256s_sha256": identities["source_sha256s_sha256"],
        "query_set_sha256": identities["query_set_sha256"],
        "release_manifest_sha256s": [
            row["manifest_sha256"] for row in identities.get("selected_releases", [])
        ],
        "version_list_sha256s": [
            row["version_list_sha256"] for row in identities.get("selected_releases", [])
        ],
        "w09_attestation_sha256": identities.get("w09_attestation_sha256"),
        "trial_registry_sha256": identities["trial_registry_sha256"],
    }
    if record_identities != projected:
        fail("RUN_MANIFEST session-resume projection differs from its evidence")
    return record, evidence_sha


def validate_authorization(
    run_dir: Path, authorization_path: Path
) -> tuple[dict[str, Any], str, str]:
    relative = base.relative_to_run(
        authorization_path, run_dir, "repair-02 authorization evidence"
    )
    authorization = load(authorization_path, "repair-02 authorization evidence")
    base.require_exact_keys(
        authorization,
        {
            "schema_version",
            "run_id",
            "repair_id",
            "authorized_at_utc",
            "authority_source",
            "authorized_action",
        },
        "repair-02 authorization evidence",
    )
    action = authorization.get("authorized_action")
    if (
        authorization.get("schema_version") != AUTHORIZATION_SCHEMA
        or authorization.get("run_id") != run_dir.name
        or authorization.get("repair_id") != REPAIR_ID
        or authorization.get("authority_source") != "operator_chat_message"
        or not isinstance(action, str)
        or SECOND_KEY not in action
        or "禁止逐行修补" not in action
        or "282/284" not in action
        or "59,185,856,724" not in action
        or "PARTIAL_OBJECT_COVERAGE_QUARANTINED" not in action
        or "从新 scratch 重跑 RFQ" not in action
    ):
        fail("repair-02 authorization is not the exact operator-approved action")
    base.require_utc_timestamp(
        authorization.get("authorized_at_utc"), "repair-02 authorization time"
    )
    return authorization, relative, base.sha256(authorization_path)


def validate_malformed_receipt(
    run_dir: Path,
    receipt_path: Path,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    relative = base.relative_to_run(receipt_path, run_dir, "second malformed receipt")
    receipt = load(receipt_path, "second malformed receipt")
    if (
        receipt.get("schema_version") != "rfq-malformed-object-receipt-v1"
        or receipt.get("run_id") != run_dir.name
        or receipt.get("release_id") != SECOND_RELEASE
        or receipt.get("key") != SECOND_KEY
        or receipt.get("manifest_sha256") != SECOND_MANIFEST_SHA
        or receipt.get("version_id") != SECOND_VERSION_ID
        or receipt.get("expected_size") != SECOND_SIZE
        or receipt.get("observed_size") != SECOND_SIZE
        or receipt.get("expected_sha256") != SECOND_SHA
        or receipt.get("observed_sha256") != SECOND_SHA
        or receipt.get("invalid_line_count") != 1
        or receipt.get("raw_payload_redacted") is not True
        or receipt.get("disposition") != "CHANNEL_OBJECT_QUARANTINE_REQUIRED"
        or receipt.get("quarantine_authorized") is not False
    ):
        fail("second malformed receipt lacks exact sealed-object evidence")
    invalid = receipt.get("invalid_lines")
    if (
        not isinstance(invalid, list)
        or len(invalid) != 1
        or not isinstance(invalid[0], dict)
        or invalid[0].get("line_number") != 264979
        or invalid[0].get("line_sha256")
        != "aaaf7ec39cd2213f0bc3e73c180678e3bffbddd7962f5d537f227a78c0e3f9cd"
    ):
        fail("second malformed-line proof mismatch")

    selected = manifest.get("selected_releases")
    matches = [
        row for row in selected or []
        if isinstance(row, dict) and row.get("release_id") == SECOND_RELEASE
    ]
    if len(matches) != 1 or matches[0].get("manifest_sha256") != SECOND_MANIFEST_SHA:
        fail("second malformed receipt release is not selected")
    manifest_path = run_dir / "DATA_INTEGRITY/manifests" / f"{SECOND_RELEASE}.json"
    if not manifest_path.is_file() or base.sha256(manifest_path) != SECOND_MANIFEST_SHA:
        fail("second object immutable manifest changed")
    release_manifest = load(manifest_path, "second object immutable manifest")
    bound = [row for row in release_manifest.get("objects", []) if row.get("key") == SECOND_KEY]
    if len(bound) != 1 or bound[0] != {
        "key": SECOND_KEY,
        "sha256": SECOND_SHA,
        "size": SECOND_SIZE,
        "version_id": SECOND_VERSION_ID,
    }:
        fail("second object manifest/VersionId binding mismatch")
    obj = {
        "invalid_line_count": 1,
        "key": SECOND_KEY,
        "manifest_sha256": SECOND_MANIFEST_SHA,
        "reason": (
            "Exact sealed bytes contain one structurally malformed NDJSON record "
            "under strict parsing; the complete structural audit found no other "
            "unregistered malformed object; no line-level salvage is permitted."
        ),
        "receipt": relative,
        "release_id": SECOND_RELEASE,
        "sha256": SECOND_SHA,
        "size": SECOND_SIZE,
        "version_id": SECOND_VERSION_ID,
    }
    return receipt, obj, relative, base.sha256(receipt_path)


def validate_declaration(
    run_dir: Path,
    declaration_path: Path,
    authorization_relative: str,
    authorization_sha: str,
    second_object: dict[str, Any],
    parent: dict[str, Any],
    parent_objects: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
    relative = base.relative_to_run(
        declaration_path, run_dir, "repair-02 quarantine declaration"
    )
    declaration = load(declaration_path, "repair-02 quarantine declaration")
    required = {
        "schema_version", "run_id", "mode", "finding", "disposition",
        "authority_basis", "created_at_utc",
        "created_after_structural_failure_before_rfq_result",
        "dependent_rfq_result_opened", "source_execution_commit",
        "parent_repair_id", "previous_declaration_path",
        "previous_declaration_sha256", "authorization_evidence_path",
        "authorization_evidence_sha256", "newly_quarantined_objects",
        "cumulative_quarantined_objects", "remaining_object_parse_policy",
        "selection_rule", "result_use_prohibited",
        "trial_disposition_if_repair_fails",
    }
    base.require_exact_keys(declaration, required, "repair-02 declaration")
    if (
        declaration.get("schema_version") != DECLARATION_SCHEMA
        or declaration.get("run_id") != run_dir.name
        or declaration.get("mode") != "EXPLORATORY_AUTORESEARCH"
        or declaration.get("finding")
        != "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT"
        or declaration.get("disposition") != "WHOLE_OBJECT_QUARANTINE"
        or declaration.get("authority_basis")
        != "EXPLICIT_OPERATOR_AUTHORIZATION_REPAIR_02"
        or declaration.get("created_after_structural_failure_before_rfq_result")
        is not True
        or declaration.get("dependent_rfq_result_opened") is not False
        or declaration.get("source_execution_commit") != PARENT_EXECUTION_COMMIT
        or declaration.get("parent_repair_id") != PARENT_REPAIR_ID
        or declaration.get("previous_declaration_path")
        != parent.get("declaration_path")
        or declaration.get("previous_declaration_sha256")
        != parent.get("declaration_sha256")
        or declaration.get("authorization_evidence_path")
        != authorization_relative
        or declaration.get("authorization_evidence_sha256") != authorization_sha
        or declaration.get("remaining_object_parse_policy") != STRICT_PARSE_POLICY
    ):
        fail("repair-02 declaration policy/provenance binding mismatch")
    base.require_utc_timestamp(declaration.get("created_at_utc"), "declaration time")
    if declaration.get("newly_quarantined_objects") != [second_object]:
        fail("repair-02 declaration must add exactly the second object")
    cumulative = sorted(parent_objects + [second_object], key=lambda row: row["key"])
    if declaration.get("cumulative_quarantined_objects") != cumulative:
        fail("repair-02 cumulative quarantine is not the ordered two-object set")
    if (
        "no line-level salvage" not in str(declaration.get("result_use_prohibited"))
        or "No RFQ row" not in str(declaration.get("result_use_prohibited"))
    ):
        fail("repair-02 declaration does not prohibit line salvage/result use")
    return declaration, cumulative, relative, base.sha256(declaration_path)


def validate_active_attempt_identity(
    run_dir: Path,
    identity_path: Path,
    original: dict[str, Any],
    parent: dict[str, Any],
    parent_objects: list[dict[str, Any]],
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    identity = load(identity_path, "attempt-02 RFQ input identity")
    expected_keys = {
        "schema", "run_id", "release_ids", "releases",
        "logical_manifest_bindings_total", "unique_objects_total",
        "unique_bytes_total", "deduplicated_overlapping_objects",
        "manifest_object_set_sha256", "coverage_status", "full_object_coverage",
        "whole_object_quarantine", "line_salvage", "consumed_unique_objects",
        "consumed_logical_bindings", "consumed_bytes", "consumed_objects",
        "consumed_object_set_sha256", "quarantined_unique_objects",
        "quarantined_logical_bindings", "quarantined_bytes",
        "quarantined_object_set_sha256", "quarantine_reasons",
        "quarantine_details", "selection_fingerprint_sha256",
        "failed_attempt_binding", "cycle1_duckdb_binding",
    }
    base.require_exact_keys(identity, expected_keys, "attempt-02 RFQ input identity")
    parent_key = parent_objects[0]["key"]
    consumed = [row for row in original["objects"] if row["key"] != parent_key]
    consumed_fingerprint = object_fingerprint(consumed)
    parent_fingerprint = object_fingerprint(
        [row for row in original["objects"] if row["key"] == parent_key]
    )
    detail = copy.deepcopy(parent_objects[0])
    detail.pop("receipt", None)
    detail["receipt_sha256"] = parent["receipt_sha256"]
    detail["declaration_sha256"] = parent["declaration_sha256"]
    old_selection = hashlib.sha256(
        json.dumps(
            {
                "total": FULL_FINGERPRINT,
                "consumed": consumed_fingerprint,
                "quarantined": parent_fingerprint,
                "receipt": parent["receipt_sha256"],
                "declaration": parent["declaration_sha256"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if (
        identity.get("schema") != "rfq-full-input-identity-v2"
        or identity.get("run_id") != run_dir.name
        or identity.get("release_ids") != original["release_ids"]
        or identity.get("releases") != original["releases"]
        or identity.get("logical_manifest_bindings_total") != TOTAL_LOGICAL_BINDINGS
        or identity.get("unique_objects_total") != TOTAL_OBJECTS
        or identity.get("unique_bytes_total") != TOTAL_BYTES
        or identity.get("deduplicated_overlapping_objects")
        != original["deduplicated_overlapping_objects"]
        or identity.get("manifest_object_set_sha256") != FULL_FINGERPRINT
        or identity.get("coverage_status")
        != "PARTIAL_OBJECT_COVERAGE_QUARANTINED"
        or identity.get("full_object_coverage") is not False
        or identity.get("whole_object_quarantine") is not True
        or identity.get("line_salvage") is not False
        or identity.get("consumed_unique_objects") != TOTAL_OBJECTS - 1
        or identity.get("consumed_logical_bindings") != TOTAL_LOGICAL_BINDINGS - 1
        or identity.get("consumed_bytes") != TOTAL_BYTES - parent_objects[0]["size"]
        or identity.get("consumed_objects") != consumed
        or identity.get("consumed_object_set_sha256") != consumed_fingerprint
        or identity.get("quarantined_unique_objects") != 1
        or identity.get("quarantined_logical_bindings") != 1
        or identity.get("quarantined_bytes") != parent_objects[0]["size"]
        or identity.get("quarantined_object_set_sha256") != parent_fingerprint
        or identity.get("quarantine_reasons") != [parent_objects[0]["reason"]]
        or identity.get("quarantine_details") != [detail]
        or identity.get("selection_fingerprint_sha256") != old_selection
        or identity.get("cycle1_duckdb_binding")
        != parent.get("cycle1_duckdb_binding")
    ):
        fail("attempt-02 active RFQ identity is not the repair-01 retained set")
    binding = identity.get("failed_attempt_binding")
    if (
        not isinstance(binding, dict)
        or binding.get("repair_id") != PARENT_REPAIR_ID
        or binding.get("failed_state_path") != parent.get("failed_state_path")
        or binding.get("failed_state_sha256") != parent.get("failed_state_sha256")
        or binding.get("failed_resource_receipt_path")
        != parent.get("failed_resource_receipt_path")
        or binding.get("failed_resource_receipt_sha256")
        != parent.get("failed_resource_receipt_sha256")
        or binding.get("failed_scratch_receipt_path")
        != parent.get("failed_scratch_receipt_path")
        or binding.get("failed_scratch_receipt_sha256")
        != parent.get("failed_scratch_receipt_sha256")
        or binding.get("failed_input_identity_path")
        != parent.get("failed_input_identity_path")
        or binding.get("failed_input_identity_sha256")
        != parent.get("failed_input_identity_sha256")
    ):
        fail("attempt-02 identity lost the repair-01 failed-attempt binding")
    return identity, old_selection, consumed


def validate_attempt_and_audit(
    run_dir: Path,
    old_selection: str,
    cumulative: list[dict[str, Any]],
) -> dict[str, Any]:
    state = load(run_dir / STATE, "attempt-02 failed state")
    if (
        state.get("schema") != "rfq-full-stage-state-v1"
        or state.get("status") != "FAILED_RESUMABLE"
        or state.get("resume") is not False
        or state.get("input_fingerprint") != old_selection
        or SECOND_KEY not in str(state.get("error"))
        or state.get("scratch")
        != f"/srv/w09-research/runs/{run_dir.name}/cache/rfq_full_scratch.duckdb"
    ):
        fail("attempt-02 state is not the fail-closed second-object failure")
    resource = load(run_dir / RESOURCE, "attempt-02 resource receipt")
    command = resource.get("command")
    if (
        resource.get("schema_version") != "w09-stage-resource-v1"
        or resource.get("label") != "rfq_full_stage_repair01"
        or resource.get("return_code") != 1
        or not isinstance(command, list)
        or "--resume" in command
        or not any(str(item).endswith("rfq_full_stage.py") for item in command)
    ):
        fail("attempt-02 resource receipt mismatch")
    scratch = load(run_dir / SCRATCH_RECEIPT, "attempt-02 scratch receipt")
    if (
        scratch.get("schema_version") != "rfq-failed-scratch-receipt-v1"
        or scratch.get("run_id") != run_dir.name
        or scratch.get("original_scratch_path") != state.get("scratch")
        or scratch.get("preserved_scratch_path")
        != f"/srv/w09-research/runs/{run_dir.name}/cache/rfq_full_scratch.attempt02_failed.duckdb"
        or scratch.get("input_fingerprint") != old_selection
        or scratch.get("disposition") != "PRESERVED_RENAMED_NO_RESUME"
        or scratch.get("resume_allowed") is not False
        or not isinstance(scratch.get("bytes"), int)
        or scratch["bytes"] <= 0
    ):
        fail("attempt-02 scratch was not preserved with no-resume semantics")
    base.require_sha(scratch.get("sha256"), "attempt-02 scratch SHA")

    audit = load(run_dir / STRUCTURAL_AUDIT, "complete RFQ structural audit")
    if (
        audit.get("schema_version") != "rfq-structural-integrity-audit-v1"
        or audit.get("run_id") != run_dir.name
        or audit.get("status") != "COMPLETE_STRUCTURAL_AUDIT"
        or audit.get("scope") != "OUTER_NDJSON_STRUCTURE_ONLY_NO_RESEARCH_RESULT"
        or audit.get("analysis_result_opened") is not False
        or audit.get("input_fingerprint") != FULL_FINGERPRINT
        or audit.get("objects_scanned") != TOTAL_OBJECTS
        or audit.get("bytes_scanned") != TOTAL_BYTES
        or audit.get("lines_scanned") != TOTAL_LINES
        or audit.get("invalid_object_count") != QUARANTINED_OBJECTS
        or audit.get("invalid_line_count") != QUARANTINED_OBJECTS
        or audit.get("identity_mismatch_count") != 0
        or audit.get("identity_mismatches") != []
        or audit.get("raw_payload_redacted") is not True
    ):
        fail("complete structural audit totals/conclusion mismatch")
    invalid_objects = audit.get("invalid_objects")
    if not isinstance(invalid_objects, list) or len(invalid_objects) != 2:
        fail("complete structural audit does not contain exactly two invalid objects")
    by_key = {row.get("key"): row for row in invalid_objects if isinstance(row, dict)}
    if set(by_key) != {row["key"] for row in cumulative}:
        fail("complete structural audit invalid-object set mismatch")
    for obj in cumulative:
        row = by_key[obj["key"]]
        if (
            row.get("expected_sha256") != obj["sha256"]
            or row.get("observed_sha256") != obj["sha256"]
            or row.get("expected_size") != obj["size"]
            or row.get("observed_size") != obj["size"]
            or row.get("identity_match") is not True
            or row.get("invalid_line_count") != 1
            or row.get("raw_payload_redacted") is not True
        ):
            fail(f"structural audit object identity mismatch: {obj['key']}")
    audit_resource = load(run_dir / STRUCTURAL_RESOURCE, "structural audit resource")
    if (
        audit_resource.get("schema_version") != "w09-stage-resource-v1"
        or audit_resource.get("label") != "rfq_structural_integrity_audit02"
        or audit_resource.get("return_code") != 0
        or not isinstance(audit_resource.get("command"), list)
    ):
        fail("structural audit resource receipt mismatch")

    blocker = load(run_dir / BLOCKER, "repair-02 authority blocker")
    if (
        blocker.get("schema_version") != "rfq-data-integrity-blocker-v1"
        or blocker.get("run_id") != run_dir.name
        or blocker.get("status") != "DATA_INTEGRITY_BLOCKER"
        or blocker.get("blocker")
        != "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT"
        or blocker.get("analysis_result_opened") is not False
        or blocker.get("automatic_additional_quarantine_prohibited") is not True
        or blocker.get("next_required_authority")
        != "EXPLICIT_REPAIR_02_PREREGISTRATION_OR_RFQ_SCOPE_TERMINATION"
        or blocker.get("repair_execution_commit") != PARENT_EXECUTION_COMMIT
        or blocker.get("failed_state_sha256") != base.sha256(run_dir / STATE)
        or blocker.get("resource_receipt_sha256") != base.sha256(run_dir / RESOURCE)
        or blocker.get("preserved_scratch_receipt_sha256")
        != base.sha256(run_dir / SCRATCH_RECEIPT)
        or blocker.get("malformed_object_receipt_sha256")
        != base.sha256(run_dir / "DATA_INTEGRITY/RFQ_MALFORMED_OBJECT_RECEIPT_02.json")
    ):
        fail("repair-02 blocker evidence chain mismatch")
    complete = blocker.get("complete_structural_audit")
    if (
        not isinstance(complete, dict)
        or complete.get("sha256") != base.sha256(run_dir / STRUCTURAL_AUDIT)
        or complete.get("resource_sha256") != base.sha256(run_dir / STRUCTURAL_RESOURCE)
        or complete.get("objects_scanned") != TOTAL_OBJECTS
        or complete.get("bytes_scanned") != TOTAL_BYTES
        or complete.get("lines_scanned") != TOTAL_LINES
        or complete.get("invalid_object_count") != 2
        or complete.get("invalid_line_count") != 2
        or complete.get("identity_mismatch_count") != 0
    ):
        fail("repair-02 blocker does not bind the complete structural audit")
    return {
        "state": state,
        "resource": resource,
        "scratch": scratch,
        "audit": audit,
        "audit_resource": audit_resource,
        "blocker": blocker,
    }


def coverage_record(
    original: dict[str, Any],
    cumulative: list[dict[str, Any]],
    declaration_sha: str,
    receipt_shas: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    excluded = {row["key"] for row in cumulative}
    retained = [row for row in original["objects"] if row["key"] not in excluded]
    quarantined = [row for row in original["objects"] if row["key"] in excluded]
    retained_fp = object_fingerprint(retained)
    quarantined_fp = object_fingerprint(quarantined)
    selection_fp = retained_selection_fingerprint(
        FULL_FINGERPRINT, retained_fp, quarantined_fp,
        declaration_sha, receipt_shas,
    )
    coverage = {
        "status": "PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "full_unique_objects": TOTAL_OBJECTS,
        "full_logical_manifest_bindings": TOTAL_LOGICAL_BINDINGS,
        "full_unique_bytes": TOTAL_BYTES,
        "retained_unique_objects": RETAINED_OBJECTS,
        "retained_logical_manifest_bindings": RETAINED_LOGICAL_BINDINGS,
        "retained_bytes": RETAINED_BYTES,
        "quarantined_unique_objects": QUARANTINED_OBJECTS,
        "quarantined_logical_manifest_bindings": QUARANTINED_LOGICAL_BINDINGS,
        "quarantined_bytes": QUARANTINED_BYTES,
        "full_object_set_sha256": FULL_FINGERPRINT,
        "retained_object_set_sha256": retained_fp,
        "quarantined_object_set_sha256": quarantined_fp,
        "retained_selection_fingerprint_sha256": selection_fp,
        "whole_object_quarantine": True,
        "line_salvage": False,
    }
    if (
        len(retained) != RETAINED_OBJECTS
        or sum(row["size"] for row in retained) != RETAINED_BYTES
        or sum(len(row["bound_release_ids"]) for row in retained)
        != RETAINED_LOGICAL_BINDINGS
        or len(quarantined) != QUARANTINED_OBJECTS
        or sum(row["size"] for row in quarantined) != QUARANTINED_BYTES
        or sum(len(row["bound_release_ids"]) for row in quarantined)
        != QUARANTINED_LOGICAL_BINDINGS
        or retained_fp != RETAINED_OBJECT_FINGERPRINT
        or quarantined_fp != QUARANTINED_OBJECT_FINGERPRINT
    ):
        fail("repair-02 retained/quarantined coverage derivation mismatch")
    return coverage, retained


def make_trial_records(
    applied_at: str,
    old_repository: dict[str, Any],
    current_identity: dict[str, Any],
    evidence: dict[str, Any],
    coverage: dict[str, Any],
    cumulative: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    common = {
        "recorded_at_utc": applied_at,
        "trial_ids": list(base.RFQ_TRIAL_IDS),
        "result_opened": False,
        "hypothesis_conclusion_opened": False,
        "parent_repair_id": PARENT_REPAIR_ID,
    }
    failure = {
        **common,
        "trial_registration_id": TRIAL_IDS[0],
        "record_type": "STAGE_FAILURE",
        "stage": "RFQ_FULL_STAGE_REPAIR01",
        "failure_class": "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT",
        "failure_disposition": "STRUCTURAL_INPUT_FAILURE_BEFORE_RESULT",
        "hypothesis_conclusion": "NONE",
        "failed_state_path": evidence["failed_state_path"],
        "failed_state_sha256": evidence["failed_state_sha256"],
        "failed_resource_receipt_path": evidence["failed_resource_receipt_path"],
        "failed_resource_receipt_sha256": evidence["failed_resource_receipt_sha256"],
        "failed_scratch_receipt_path": evidence["failed_scratch_receipt_path"],
        "failed_scratch_receipt_sha256": evidence["failed_scratch_receipt_sha256"],
        "failed_input_identity_path": evidence["failed_input_identity_path"],
        "failed_input_identity_sha256": evidence["failed_input_identity_sha256"],
        "failed_input_fingerprint": evidence["failed_input_fingerprint"],
        "structural_audit_path": evidence["structural_audit_path"],
        "structural_audit_sha256": evidence["structural_audit_sha256"],
        "execution_commit": old_repository["execution_commit"],
        "source_manifest_sha256": old_repository["source_manifest_sha256"],
        "source_sha256s_sha256": old_repository["source_sha256s_sha256"],
        "query_set_sha256": old_repository["query_set_sha256"],
    }
    prereg = {
        **common,
        "trial_registration_id": TRIAL_IDS[1],
        "record_type": "DATA_INTEGRITY_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR02_PREREGISTRATION",
        "finding": "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT",
        "quarantine_policy": "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE",
        "rfq_result_state": NO_RFQ_RESULT,
        "retry_requirement": RETRY_REQUIREMENT,
        "declaration_path": evidence["declaration_path"],
        "declaration_sha256": evidence["declaration_sha256"],
        "receipt_path": evidence["receipt_path"],
        "receipt_sha256": evidence["receipt_sha256"],
        "authorization_evidence_path": evidence["authorization_evidence_path"],
        "authorization_evidence_sha256": evidence["authorization_evidence_sha256"],
        "structural_audit_path": evidence["structural_audit_path"],
        "structural_audit_sha256": evidence["structural_audit_sha256"],
        "newly_quarantined_objects": [copy.deepcopy(cumulative[0])],
        "cumulative_quarantined_objects": copy.deepcopy(cumulative),
        "coverage": copy.deepcopy(coverage),
        "previous_execution_commit": old_repository["execution_commit"],
        "current_execution_commit": current_identity["execution_commit"],
        "previous_source_manifest_sha256": old_repository["source_manifest_sha256"],
        "previous_source_sha256s_sha256": old_repository["source_sha256s_sha256"],
        "current_source_manifest_sha256": current_identity["source_manifest_sha256"],
        "current_source_sha256s_sha256": current_identity["source_sha256s_sha256"],
        "previous_query_set_sha256": old_repository["query_set_sha256"],
        "current_query_set_sha256": current_identity["query_set_sha256"],
        "registration_change_class": (
            "DATA_INTEGRITY_HANDLING_ONLY_NO_HYPOTHESIS_DESIGN_CHANGE"
        ),
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "core_results_recomputed": False,
    }
    return failure, prereg


def validate_already_applied(
    run_dir: Path,
    source_dir: Path,
    declaration_path: Path,
    receipt_path: Path,
    authorization_path: Path,
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    if (
        manifest.get("status") != POST_REPAIR_STATUS
        or len(records) != 2
        or records[0].get("repair_id") != PARENT_REPAIR_ID
        or records[1].get("repair_id") != REPAIR_ID
        or records[1].get("schema_version") != REPAIR_SCHEMA
    ):
        fail("unknown or inconsistent existing repair-02 history")
    record = records[1]
    parent = records[0]
    if (
        record.get("parent_repair_id") != PARENT_REPAIR_ID
        or record.get("previous_repair_registration_path")
        != parent.get("repair_receipt_path")
        or record.get("previous_repair_registration_sha256")
        != parent.get("repair_receipt_sha256")
        or record.get("previous_repair_record_sha256") != canonical_hash(parent)
    ):
        fail("repair-01/repair-02 append chain changed")
    verify_bound_file(
        run_dir,
        str(parent.get("repair_receipt_path")),
        parent.get("repair_receipt_sha256"),
        "repair-01 registration receipt",
    )
    for supplied, field, label in (
        (declaration_path, "declaration_sha256", "declaration"),
        (receipt_path, "receipt_sha256", "malformed receipt"),
        (authorization_path, "authorization_evidence_sha256", "authorization"),
    ):
        if not supplied.is_file() or base.sha256(supplied) != record.get(field):
            fail(f"idempotent repair-02 {label} mismatch")
    repair_dir = run_dir / "DATA_INTEGRITY/repairs/repair-02"
    archive = repair_dir / "pre_repair"
    if (
        record.get("archive_path")
        != "DATA_INTEGRITY/repairs/repair-02/pre_repair"
        or record.get("archive_inventory") != base.archive_inventory(archive)
    ):
        fail("repair-02 archive inventory changed")
    receipt_file = verify_bound_file(
        run_dir,
        str(record.get("repair_receipt_path")),
        record.get("repair_receipt_sha256"),
        "repair-02 registration receipt",
    )
    receipt_record = load(receipt_file, "repair-02 registration receipt")
    without_self = copy.deepcopy(record)
    without_self.pop("repair_receipt_path", None)
    without_self.pop("repair_receipt_sha256", None)
    if receipt_record != without_self:
        fail("repair-02 receipt/manifest record mismatch")
    journal_path = verify_bound_file(
        run_dir,
        str(record.get("transaction_journal_path")),
        record.get("transaction_journal_sha256"),
        "repair-02 transaction journal",
    )
    journal = load(journal_path, "repair-02 transaction journal")
    if (
        journal.get("schema_version") != "repair02-registration-transaction-v1"
        or journal.get("repair_id") != REPAIR_ID
        or journal.get("run_id") != run_dir.name
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
        or _safe_relative_file_set(repair_dir)
        != set(journal.get("expected_repair_files", []))
    ):
        fail("repair-02 committed transaction journal mismatch")
    for row in journal.get("active_mutations", []):
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            fail("repair-02 committed transaction mutation is invalid")
        active = base.checked_run_path(
            run_dir, row["path"], "committed repair-02 active mutation"
        )
        if (
            not active.is_file()
            or base.sha256(active) != row.get("replacement_sha256")
        ):
            fail(f"repair-02 committed trial/active bytes changed: {row['path']}")
    for path_field, sha_field in (
        ("declaration_path", "declaration_sha256"),
        ("receipt_path", "receipt_sha256"),
        ("authorization_evidence_path", "authorization_evidence_sha256"),
        ("failed_state_path", "failed_state_sha256"),
        ("failed_resource_receipt_path", "failed_resource_receipt_sha256"),
        ("failed_scratch_receipt_path", "failed_scratch_receipt_sha256"),
        ("failed_input_identity_path", "failed_input_identity_sha256"),
        ("structural_audit_path", "structural_audit_sha256"),
        ("structural_audit_resource_path", "structural_audit_resource_sha256"),
        ("blocker_evidence_path", "blocker_evidence_sha256"),
        ("session_resume_evidence_path", "session_resume_evidence_sha256"),
    ):
        verify_bound_file(run_dir, str(record[path_field]), record[sha_field], path_field)
    if record.get("core_result_artifacts") != base.core_result_inventory(run_dir):
        fail("core artifacts changed after repair-02")
    registry = (run_dir / "TRIAL_REGISTRY.jsonl").read_bytes()
    binding = record.get("trial_registry")
    if (
        not isinstance(binding, dict)
        or binding.get("current_bytes") != len(registry)
        or binding.get("current_sha256") != base.sha256_bytes(registry)
        or binding.get("trial_registration_ids") != list(TRIAL_IDS)
        or binding.get("appended_records") != 2
        or binding.get("strict_previous_bytes_prefix") is not True
    ):
        fail("repair-02 trial append boundary changed")
    repository = manifest.get("repository")
    if (
        not isinstance(repository, dict)
        or base.repository_identity(repository)
        != record.get("current_repository_identity")
        or repository.get("identity_history") != record.get("repository_identity_chain")
    ):
        fail("repair-02 repository identity chain mismatch")
    session = record.get("session_resume")
    active_resumes = manifest.get("session_resumes")
    if (
        not isinstance(session, dict)
        or not isinstance(active_resumes, list)
        or len(active_resumes) != 1
        or session.get("manifest_record") != active_resumes[0]
        or session.get("evidence_path") != record.get("session_resume_evidence_path")
        or session.get("evidence_sha256")
        != record.get("session_resume_evidence_sha256")
        or session.get("preserved_in_run_manifest") is not True
    ):
        fail("repair-02 session-resume chain changed")
    _, _, head = base.verify_clean_source(source_dir)
    if head != record.get("current_execution_commit"):
        fail("clean source HEAD no longer matches repair-02")
    base.verify_source_sums_match_manifest(
        run_dir / "SOURCE_MANIFEST.json",
        run_dir / "SOURCE_SHA256SUMS.txt",
        "repair-02 current",
    )
    if (
        base.sha256(run_dir / "SOURCE_MANIFEST.json")
        != record.get("current_source_manifest_sha256")
        or base.sha256(run_dir / "SOURCE_SHA256SUMS.txt")
        != record.get("current_source_sha256s_sha256")
        or base.sha256(run_dir / "QUERY_SHA256SUMS.txt")
        != record.get("current_query_set_sha256")
    ):
        fail("repair-02 active source/query receipts changed")
    return {
        "status": "REGISTRATION_REPAIR02_ALREADY_APPLIED",
        "repair_id": REPAIR_ID,
        "run_id": run_dir.name,
        "execution_commit": head,
        "quarantined_objects": QUARANTINED_OBJECTS,
    }


def repair02_registration(
    run_dir: Path,
    source_dir: Path,
    quarantine_declaration: Path,
    second_receipt: Path,
    authorization_evidence: Path,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    source_dir = source_dir.resolve()
    declaration_path = quarantine_declaration.resolve()
    receipt_path = second_receipt.resolve()
    authorization_path = authorization_evidence.resolve()
    manifest_path = run_dir / "RUN_MANIFEST.json"
    manifest_raw = manifest_path.read_bytes() if manifest_path.is_file() else b""
    manifest = load(manifest_path, "RUN_MANIFEST.json")
    if manifest.get("run_id") != run_dir.name:
        fail("RUN_MANIFEST run_id/path mismatch")
    records = manifest.get("data_integrity_repairs")
    if not isinstance(records, list):
        fail("data_integrity_repairs must be an append-only array")
    if len(records) == 2:
        return validate_already_applied(
            run_dir, source_dir, declaration_path, receipt_path,
            authorization_path, manifest, records,
        )
    if len(records) != 1:
        fail("repair-02 requires exactly the repair-01 parent")
    if manifest.get("analysis_started") is not True:
        fail("repair-02 requires the active post-core mission")
    if (run_dir / RFQ_SUMMARY).exists() or (run_dir / RFQ_REPORT).exists():
        fail("RFQ result exists; repair-02 preregistration is forbidden")
    repair_dir = run_dir / "DATA_INTEGRITY/repairs/repair-02"
    if repair_dir.exists():
        # A prior process may have stopped after publishing the immutable
        # recovery journal but before the final RUN_MANIFEST CAS.  Recover only
        # journal-recognized bytes; unknown concurrent changes are never erased.
        recover_incomplete_transaction(run_dir, repair_dir)
        if repair_dir.exists():
            fail("repair-02 incomplete transaction recovery did not finish")

    parent, original, parent_objects, parent_record_sha = validate_parent_repair(
        run_dir, manifest
    )
    session_resume_record, session_resume_sha = validate_session_resume(
        run_dir, manifest, parent
    )
    repo_root, source_relative, current_commit = base.verify_clean_source(source_dir)
    old_repository, query_names = base.verify_prior_repository(
        run_dir, source_dir, repo_root, source_relative, current_commit, manifest
    )
    if old_repository.get("execution_commit") != PARENT_EXECUTION_COMMIT:
        fail("repair-02 source parent is not committed repair-01")

    authorization, authorization_relative, authorization_sha = validate_authorization(
        run_dir, authorization_path
    )
    second_receipt_json, second_object, receipt_relative, receipt_sha = (
        validate_malformed_receipt(run_dir, receipt_path, manifest)
    )
    declaration, cumulative, declaration_relative, declaration_sha = (
        validate_declaration(
            run_dir, declaration_path, authorization_relative,
            authorization_sha, second_object, parent, parent_objects,
        )
    )
    identity, old_selection, _ = validate_active_attempt_identity(
        run_dir, run_dir / FAILED_INPUT_IDENTITY, original, parent, parent_objects
    )
    validate_attempt_and_audit(run_dir, old_selection, cumulative)
    coverage, _ = coverage_record(
        original, cumulative, declaration_sha,
        [parent["receipt_sha256"], receipt_sha],
    )

    trial_path = run_dir / "TRIAL_REGISTRY.jsonl"
    trial_before, _ = parse_repair01_trial_registry(trial_path)
    if base.sha256_bytes(trial_before) != parent["trial_registry"]["current_sha256"]:
        fail("TRIAL_REGISTRY changed after repair-01")
    core_before = base.core_result_inventory(run_dir)
    _, source_manifest_payload, source_sums_payload = base.build_source_receipts(
        source_dir, repo_root
    )
    source_manifest_sha = base.sha256_bytes(source_manifest_payload)
    source_sums_sha = base.sha256_bytes(source_sums_payload)
    query_rows: list[tuple[str, str, bytes]] = []
    for name in query_names:
        path = source_dir / name
        if not path.is_file():
            fail(f"registered query source is missing: {name}")
        payload = path.read_bytes()
        query_rows.append((base.sha256_bytes(payload), f"queries/{name}", payload))
    query_sums_payload = "".join(
        f"{digest}  {relative}\n" for digest, relative, _ in query_rows
    ).encode()
    query_sha = base.sha256_bytes(query_sums_payload)
    current_identity = {
        "execution_commit": current_commit,
        "source_manifest_sha256": source_manifest_sha,
        "source_sha256s_sha256": source_sums_sha,
        "query_set_sha256": query_sha,
        "query_files": copy.deepcopy(old_repository["query_files"]),
    }
    previous_identity = base.repository_identity(old_repository)
    initial_identity = copy.deepcopy(old_repository.get("initial_identity"))
    if not isinstance(initial_identity, dict):
        fail("repair-01 repository lost its initial identity")
    identity_chain = [initial_identity, previous_identity, current_identity]
    if len({canonical_hash(row) for row in identity_chain}) != 3:
        fail("repair-02 repository identity chain is not three distinct revisions")

    applied_at = base.now_utc()
    repair_root = run_dir / "DATA_INTEGRITY/repairs"
    repair_root.mkdir(parents=True, exist_ok=True)
    staging: Path | None = Path(
        tempfile.mkdtemp(prefix=".repair-02-", dir=repair_root)
    )
    try:
        pre = staging / "pre_repair"
        archive_paths = (
            Path("RUN_MANIFEST.json"), Path("TRIAL_REGISTRY.jsonl"),
            Path("SOURCE_MANIFEST.json"), Path("SOURCE_SHA256SUMS.txt"),
            Path("QUERY_SHA256SUMS.txt"), STATE, RESOURCE, SCRATCH_RECEIPT,
            FAILED_INPUT_IDENTITY, STRUCTURAL_AUDIT, STRUCTURAL_RESOURCE,
            BLOCKER, CYCLE1_BINDING, SESSION_RESUME,
        )
        for relative in archive_paths:
            base.copy_exact(run_dir / relative, pre / relative)
        for query_relative in old_repository["query_files"]:
            base.copy_exact(
                base.checked_run_path(run_dir, query_relative, "repair-01 query"),
                pre / Path(query_relative),
            )
        base.copy_exact(declaration_path, staging / "QUARANTINE_DECLARATION.json")
        base.copy_exact(receipt_path, staging / "MALFORMED_OBJECT_RECEIPT.json")
        base.copy_exact(authorization_path, staging / "USER_AUTHORIZATION.json")
        post = staging / "post_repair"
        base.atomic_write(post / "SOURCE_MANIFEST.json", source_manifest_payload)
        base.atomic_write(post / "SOURCE_SHA256SUMS.txt", source_sums_payload)
        base.atomic_write(post / "QUERY_SHA256SUMS.txt", query_sums_payload)
        for _, relative, payload in query_rows:
            base.atomic_write(post / Path(relative), payload)

        archive_rows = base.archive_inventory(pre)
        prefix = "DATA_INTEGRITY/repairs/repair-02"
        evidence = {
            "declaration_path": f"{prefix}/QUARANTINE_DECLARATION.json",
            "declaration_sha256": declaration_sha,
            "receipt_path": f"{prefix}/MALFORMED_OBJECT_RECEIPT.json",
            "receipt_sha256": receipt_sha,
            "authorization_evidence_path": f"{prefix}/USER_AUTHORIZATION.json",
            "authorization_evidence_sha256": authorization_sha,
            "failed_state_path": f"{prefix}/pre_repair/{STATE.as_posix()}",
            "failed_state_sha256": base.sha256(run_dir / STATE),
            "failed_resource_receipt_path": f"{prefix}/pre_repair/{RESOURCE.as_posix()}",
            "failed_resource_receipt_sha256": base.sha256(run_dir / RESOURCE),
            "failed_scratch_receipt_path": f"{prefix}/pre_repair/{SCRATCH_RECEIPT.as_posix()}",
            "failed_scratch_receipt_sha256": base.sha256(run_dir / SCRATCH_RECEIPT),
            "failed_input_identity_path": f"{prefix}/pre_repair/{FAILED_INPUT_IDENTITY.as_posix()}",
            "failed_input_identity_sha256": base.sha256(run_dir / FAILED_INPUT_IDENTITY),
            "failed_input_fingerprint": old_selection,
            "structural_audit_path": f"{prefix}/pre_repair/{STRUCTURAL_AUDIT.as_posix()}",
            "structural_audit_sha256": base.sha256(run_dir / STRUCTURAL_AUDIT),
            "structural_audit_resource_path": f"{prefix}/pre_repair/{STRUCTURAL_RESOURCE.as_posix()}",
            "structural_audit_resource_sha256": base.sha256(run_dir / STRUCTURAL_RESOURCE),
            "blocker_evidence_path": f"{prefix}/pre_repair/{BLOCKER.as_posix()}",
            "blocker_evidence_sha256": base.sha256(run_dir / BLOCKER),
            "session_resume_evidence_path": (
                f"{prefix}/pre_repair/{SESSION_RESUME.as_posix()}"
            ),
            "session_resume_evidence_sha256": session_resume_sha,
        }
        trial_failure, trial_prereg = make_trial_records(
            applied_at, old_repository, current_identity, evidence, coverage, cumulative
        )
        trial_after = (
            trial_before + base.registry_line(trial_failure)
            + base.registry_line(trial_prereg)
        )
        base.atomic_write(post / "TRIAL_REGISTRY.jsonl", trial_after)

        mutation_relatives = [
            "SOURCE_MANIFEST.json",
            "SOURCE_SHA256SUMS.txt",
            "QUERY_SHA256SUMS.txt",
            *old_repository["query_files"],
            "TRIAL_REGISTRY.jsonl",
        ]
        active_mutations: list[dict[str, Any]] = []
        for relative in mutation_relatives:
            original_path = pre / Path(relative)
            replacement_path = post / Path(relative)
            if not original_path.is_file() or not replacement_path.is_file():
                fail(f"repair-02 transaction payload is missing: {relative}")
            active_mutations.append({
                "path": relative,
                "original_archive_path": f"{prefix}/pre_repair/{relative}",
                "original_sha256": base.sha256(original_path),
                "replacement_sha256": base.sha256(replacement_path),
                "append_only_registry": relative == "TRIAL_REGISTRY.jsonl",
            })
        expected_repair_files = sorted({
            *(f"pre_repair/{row['path']}" for row in archive_rows),
            *(f"post_repair/{row['path']}" for row in base.archive_inventory(post)),
            "QUARANTINE_DECLARATION.json",
            "MALFORMED_OBJECT_RECEIPT.json",
            "USER_AUTHORIZATION.json",
            "REPAIR_REGISTRATION.json",
            TRANSACTION_JOURNAL,
        })
        transaction_journal = {
            "schema_version": "repair02-registration-transaction-v1",
            "repair_id": REPAIR_ID,
            "run_id": run_dir.name,
            "state": "PREPARED_BEFORE_ACTIVE_MUTATION",
            "original_manifest_sha256": base.sha256_bytes(manifest_raw),
            "active_mutations": active_mutations,
            "expected_repair_files": expected_repair_files,
        }
        base.atomic_write(
            staging / TRANSACTION_JOURNAL,
            base.json_payload(transaction_journal),
        )
        transaction_journal_sha = base.sha256(staging / TRANSACTION_JOURNAL)

        record: dict[str, Any] = {
            "schema_version": REPAIR_SCHEMA,
            "repair_id": REPAIR_ID,
            "parent_repair_id": PARENT_REPAIR_ID,
            "applied_at_utc": applied_at,
            "pre_repair_status": EXPECTED_STATUS,
            "post_repair_status": POST_REPAIR_STATUS,
            "rfq_result_state": NO_RFQ_RESULT,
            "retry_requirement": RETRY_REQUIREMENT,
            "finding": "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT",
            "quarantine_policy": "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE",
            "previous_repair_registration_path": parent["repair_receipt_path"],
            "previous_repair_registration_sha256": parent["repair_receipt_sha256"],
            "previous_repair_record_sha256": parent_record_sha,
            "declaration_input_path": declaration_relative,
            "receipt_input_path": receipt_relative,
            "authorization_evidence_input_path": authorization_relative,
            **evidence,
            "authorization_evidence_schema": AUTHORIZATION_SCHEMA,
            "session_resume": {
                "manifest_record": copy.deepcopy(session_resume_record),
                "evidence_path": evidence["session_resume_evidence_path"],
                "evidence_sha256": session_resume_sha,
                "preserved_in_run_manifest": True,
            },
            "newly_quarantined_objects": [copy.deepcopy(second_object)],
            "cumulative_quarantined_objects": copy.deepcopy(cumulative),
            "coverage": copy.deepcopy(coverage),
            "failed_attempt": {
                "attempt_id": "RFQ_FULL_STAGE_REPAIR01_ATTEMPT_02",
                "state_active_path": STATE.as_posix(),
                "state_path": evidence["failed_state_path"],
                "state_sha256": evidence["failed_state_sha256"],
                "state_status": "FAILED_RESUMABLE",
                "resource_active_path": RESOURCE.as_posix(),
                "resource_path": evidence["failed_resource_receipt_path"],
                "resource_sha256": evidence["failed_resource_receipt_sha256"],
                "resource_label": "rfq_full_stage_repair01",
                "return_code": 1,
                "scratch_receipt_active_path": SCRATCH_RECEIPT.as_posix(),
                "scratch_receipt_path": evidence["failed_scratch_receipt_path"],
                "scratch_receipt_sha256": evidence["failed_scratch_receipt_sha256"],
                "input_identity_active_path": FAILED_INPUT_IDENTITY.as_posix(),
                "input_identity_path": evidence["failed_input_identity_path"],
                "input_identity_sha256": evidence["failed_input_identity_sha256"],
                "input_fingerprint": old_selection,
                "scratch_disposition": "PRESERVED_RENAMED_NO_RESUME",
                "retry_requirement": RETRY_REQUIREMENT,
            },
            "structural_audit": {
                "path": evidence["structural_audit_path"],
                "sha256": evidence["structural_audit_sha256"],
                "resource_path": evidence["structural_audit_resource_path"],
                "resource_sha256": evidence["structural_audit_resource_sha256"],
                "objects_scanned": TOTAL_OBJECTS,
                "bytes_scanned": TOTAL_BYTES,
                "lines_scanned": TOTAL_LINES,
                "invalid_object_count": 2,
                "invalid_line_count": 2,
                "identity_mismatch_count": 0,
                "analysis_result_opened": False,
            },
            "blocker_evidence": {
                "path": evidence["blocker_evidence_path"],
                "sha256": evidence["blocker_evidence_sha256"],
                "status": "DATA_INTEGRITY_BLOCKER",
            },
            "authorization_evidence": {
                "path": evidence["authorization_evidence_path"],
                "sha256": authorization_sha,
                "schema_version": AUTHORIZATION_SCHEMA,
                "authority_source": authorization["authority_source"],
            },
            "cycle1_duckdb_binding": copy.deepcopy(parent["cycle1_duckdb_binding"]),
            "previous_execution_commit": old_repository["execution_commit"],
            "current_execution_commit": current_commit,
            "initial_repository_identity": initial_identity,
            "previous_repository_identity": previous_identity,
            "current_repository_identity": current_identity,
            "repository_identity_chain": identity_chain,
            "previous_source_manifest_sha256": old_repository["source_manifest_sha256"],
            "current_source_manifest_sha256": source_manifest_sha,
            "previous_source_sha256s_sha256": old_repository["source_sha256s_sha256"],
            "current_source_sha256s_sha256": source_sums_sha,
            "previous_query_set_sha256": old_repository["query_set_sha256"],
            "current_query_set_sha256": query_sha,
            "core_result_disposition": CORE_DISPOSITION,
            "core_results_recomputed": False,
            "core_result_artifacts": core_before,
            "trial_registry": {
                "previous_sha256": base.sha256_bytes(trial_before),
                "current_sha256": base.sha256_bytes(trial_after),
                "previous_bytes": len(trial_before),
                "current_bytes": len(trial_after),
                "strict_previous_bytes_prefix": True,
                "appended_records": 2,
                "trial_registration_ids": list(TRIAL_IDS),
            },
            "registration_change_class": (
                "DATA_INTEGRITY_HANDLING_ONLY_NO_HYPOTHESIS_DESIGN_CHANGE"
            ),
            "hypothesis_design_change": "NONE",
            "data_integrity_handling_change": (
                "SECOND_EXACT_WHOLE_OBJECT_QUARANTINE_AND_CONSERVATIVE_GAP_CENSORING"
            ),
            "threshold_feature_test_or_hypothesis_status_changed": False,
            "archive_path": f"{prefix}/pre_repair",
            "archive_inventory": archive_rows,
            "transaction_journal_path": f"{prefix}/{TRANSACTION_JOURNAL}",
            "transaction_journal_sha256": transaction_journal_sha,
        }
        base.atomic_write(staging / "REPAIR_REGISTRATION.json", base.json_payload(record))
        record["repair_receipt_path"] = f"{prefix}/REPAIR_REGISTRATION.json"
        record["repair_receipt_sha256"] = base.sha256(
            staging / "REPAIR_REGISTRATION.json"
        )

        new_repository = copy.deepcopy(old_repository)
        new_repository.update({
            "previous_execution_commit": old_repository["execution_commit"],
            "previous_source_manifest_sha256": old_repository["source_manifest_sha256"],
            "previous_source_sha256s_sha256": old_repository["source_sha256s_sha256"],
            "previous_query_set_sha256": old_repository["query_set_sha256"],
            "previous_identity": previous_identity,
            "execution_commit": current_commit,
            "source_manifest_sha256": source_manifest_sha,
            "source_sha256s_sha256": source_sums_sha,
            "query_set_sha256": query_sha,
            "source_tree_dirty_at_freeze": False,
            "registration_repair_id": REPAIR_ID,
            "identity_history": identity_chain,
        })
        updated = copy.deepcopy(manifest)
        updated["repository"] = new_repository
        updated["data_integrity_repairs"] = records + [record]
        updated["status"] = POST_REPAIR_STATUS
        updated["registration_state"] = (
            "RE_FROZEN_AFTER_DATA_INTEGRITY_REPAIR02_BEFORE_RFQ_RESULT"
        )

        updated_manifest_payload = base.json_payload(updated)
        original_payloads = {
            row["path"]: (pre / Path(row["path"])).read_bytes()
            for row in active_mutations
        }
        replacement_payloads = {
            row["path"]: (post / Path(row["path"])).read_bytes()
            for row in active_mutations
        }
        written: set[str] = set()
        published = False
        manifest_replaced = False

        def assert_active_cas(label: str) -> None:
            if manifest_path.read_bytes() != manifest_raw:
                fail(f"RUN_MANIFEST CAS failed before {label}")
            for row in active_mutations:
                relative = row["path"]
                active_path = run_dir / Path(relative)
                expected = (
                    replacement_payloads[relative]
                    if relative in written else original_payloads[relative]
                )
                if not active_path.is_file() or active_path.read_bytes() != expected:
                    fail(f"active identity CAS failed before {label}: {relative}")
            if base.core_result_inventory(run_dir) != core_before:
                fail(f"Cycle-1 core CAS failed before {label}")

        def cas_write(relative: str) -> None:
            label = f"write:{relative}"
            assert_active_cas(label)
            active_path = run_dir / Path(relative)
            # Mark intent before the call.  If atomic_write raises after its
            # os.replace, recovery still recognizes and restores the new bytes.
            try:
                base.atomic_write(active_path, replacement_payloads[relative])
            finally:
                if (
                    active_path.is_file()
                    and active_path.read_bytes() == replacement_payloads[relative]
                ):
                    written.add(relative)
            if active_path.read_bytes() != replacement_payloads[relative]:
                fail(f"repair-02 active write verification failed: {relative}")
            _transaction_fault_hook(label)

        def append_trial_registry() -> None:
            """Append the two records without ever replacing concurrent bytes."""
            relative = "TRIAL_REGISTRY.jsonl"
            label = f"write:{relative}"
            assert_active_cas(label)
            original = original_payloads[relative]
            replacement = replacement_payloads[relative]
            if not replacement.startswith(original):
                fail("repair-02 registry replacement is not an append")
            delta = replacement[len(original):]
            if not delta:
                fail("repair-02 registry append payload is empty")
            active_path = run_dir / relative
            descriptor = os.open(active_path, os.O_RDWR | os.O_APPEND)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                stat_path = active_path.stat()
                stat_fd = os.fstat(descriptor)
                if (stat_path.st_dev, stat_path.st_ino) != (
                    stat_fd.st_dev, stat_fd.st_ino
                ):
                    fail("TRIAL_REGISTRY inode changed before append")
                os.lseek(descriptor, 0, os.SEEK_SET)
                locked = os.read(descriptor, stat_fd.st_size + 1)
                if locked != original:
                    fail("TRIAL_REGISTRY CAS failed after append lock")
                # This boundary is deliberately after the initial CAS/lock and
                # before write.  A non-cooperating append is detected by the
                # second same-inode read and is never overwritten.
                _transaction_fault_hook("trial_append_after_cas_before_write")
                stat_fd = os.fstat(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                locked = os.read(descriptor, stat_fd.st_size + 1)
                if locked != original:
                    fail("TRIAL_REGISTRY CAS failed immediately before append")
                written_bytes = os.write(descriptor, delta)
                if written_bytes != len(delta):
                    fail("TRIAL_REGISTRY append was partial")
                os.fsync(descriptor)
                stat_fd = os.fstat(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                after = os.read(descriptor, stat_fd.st_size + 1)
                if after != replacement:
                    fail("TRIAL_REGISTRY changed concurrently during append")
                written.add(relative)
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            _transaction_fault_hook(label)

        # The fully prepared directory contains both rollback payloads and a
        # self-describing journal.  Verify its exact file set before publishing.
        if _safe_relative_file_set(staging) != set(expected_repair_files):
            fail("prepared repair-02 transaction file set mismatch")
        assert_active_cas("publish_repair_dir")
        try:
            os.replace(staging, repair_dir)
        finally:
            if repair_dir.is_dir():
                published = True
                staging = None
        try:
            if not published:
                fail("repair-02 directory publication failed")
            _transaction_fault_hook("publish_repair_dir")
            for row in active_mutations:
                if row["path"] == "TRIAL_REGISTRY.jsonl":
                    append_trial_registry()
                else:
                    cas_write(row["path"])
            assert_active_cas("write:RUN_MANIFEST.json")
            try:
                base.atomic_write(manifest_path, updated_manifest_payload)
            finally:
                manifest_replaced = (
                    manifest_path.is_file()
                    and manifest_path.read_bytes() == updated_manifest_payload
                )
            if not manifest_replaced:
                fail("RUN_MANIFEST final write verification failed")
            _transaction_fault_hook("write:RUN_MANIFEST.json")
        except BaseException as transaction_error:
            # RUN_MANIFEST is the commit marker.  A hook/write may fail after
            # its atomic replacement, so restore it first while its bytes are
            # still exactly ours, then replay the immutable rollback journal.
            if manifest_replaced:
                if manifest_path.read_bytes() != updated_manifest_payload:
                    fail(
                        "concurrent RUN_MANIFEST mutation prevents safe rollback"
                    )
                base.atomic_write(manifest_path, manifest_raw)
            if published and repair_dir.exists():
                try:
                    recover_incomplete_transaction(run_dir, repair_dir)
                except BaseException as rollback_error:
                    raise Repair02RegistrationError(
                        "repair-02 transaction failed and safe rollback was blocked: "
                        f"{rollback_error}"
                    ) from transaction_error
            raise
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)

    return {
        "status": "REGISTRATION_REPAIR02_REFROZEN",
        "repair_id": REPAIR_ID,
        "run_id": run_dir.name,
        "previous_execution_commit": PARENT_EXECUTION_COMMIT,
        "execution_commit": current_commit,
        "quarantined_objects": QUARANTINED_OBJECTS,
        "retained_objects": RETAINED_OBJECTS,
        "retained_bytes": RETAINED_BYTES,
        "retained_selection_fingerprint_sha256": coverage[
            "retained_selection_fingerprint_sha256"
        ],
        "rfq_result_state": NO_RFQ_RESULT,
    }


repair = repair02_registration


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Append-only SPORTS-AUTORESEARCH-01 repair-02 preregistration"
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--quarantine-declaration", type=Path, required=True)
    parser.add_argument("--second-receipt", type=Path, required=True)
    parser.add_argument("--authorization-evidence", type=Path, required=True)
    args = parser.parse_args()
    result = repair02_registration(
        args.run_dir,
        args.source_dir,
        args.quarantine_declaration,
        args.second_receipt,
        args.authorization_evidence,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
