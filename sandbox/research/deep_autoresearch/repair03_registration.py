#!/usr/bin/env python3
"""Append-only parser-contract preregistration for RFQ attempt 04.

This command is deliberately specific to SPORTS-AUTORESEARCH-01 repair-03.  It
may run only after the repair-02 retained-object attempt failed before producing
an RFQ result, the failed scratch database was preserved without resume, and a
complete read-only audit proved that every rejected inner payload was an
expected blank recorder control marker.  It changes code identity and parser
semantics only: the 282-object selection, quarantine, hypothesis design, and
all previously opened core results remain byte-for-byte frozen.

RUN_MANIFEST.json is the transaction commit marker.  Before that final CAS the
command publishes an immutable recovery journal, installs the new source/query
receipts, and appends exactly two TRIAL_REGISTRY records with O_APPEND+flock.
"""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


def _load_sibling(module_name: str):
    path = Path(__file__).with_name(f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ModuleNotFoundError(module_name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    import repair_registration as base
except ModuleNotFoundError:  # Direct importlib loading in unit tests.
    base = _load_sibling("repair_registration")

try:
    import repair02_registration as repair02
except ModuleNotFoundError:  # Direct importlib loading in unit tests.
    repair02 = _load_sibling("repair02_registration")


Repair03RegistrationError = base.RepairRegistrationError

REPAIR_ID = "repair-03"
PARENT_REPAIR_ID = "repair-02"
PARENT_EXECUTION_COMMIT = "0fc93a7b821fe04a2b76c9a482065a01dc78e433"
EXPECTED_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR02_REGISTERED"
POST_REPAIR_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_PARSER_REPAIR03_REGISTERED"
FAILED_STAGE_STATUS = "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
REGISTRATION_STATE = (
    "RE_FROZEN_AFTER_RFQ_PARSER_CORRECTION_REPAIR03_BEFORE_RFQ_RESULT"
)
REPAIR_SCHEMA = "sports-autoresearch-rfq-parser-repair-v1"
JOURNAL_SCHEMA = "repair03-registration-transaction-v1"
PARSER_CONTRACT_SCHEMA = "rfq-inner-payload-parser-contract-v1"
AUTHORITY_SCHEMA = "sports-autoresearch-autonomous-code-repair-authority-v1"
FINDING = "EXPECTED_CONTROL_MARKER_MISCLASSIFIED_AS_MALFORMED_INNER_PAYLOAD"
CHANGE_CLASS = "PARSER_CONTRACT_CORRECTION_ONLY_NO_DATA_SELECTION_OR_HYPOTHESIS_CHANGE"
RETRY_REQUIREMENT = "FRESH_SCRATCH_SAME_SELECTION_NEW_PARSER_CONTRACT_NO_RESUME"
NO_RFQ_RESULT = "NO_RFQ_RESULT_OPENED"
CORE_DISPOSITION = "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED"
SELECTION_FINGERPRINT = (
    "8b310c37f3989770d1f53a058e5ef396e1eb5c9c3d24d07ac87bd9d8aee5b9e1"
)
RETAINED_FINGERPRINT = (
    "cf6885a13ac50369fbfb19aba3809cd7e83c8bdabe4381ec926ed1c61d47b652"
)
QUARANTINED_FINGERPRINT = (
    "cf0e874f65aad791c5a23164a34511bd0482d7ece9de0706841c5b8bc92808eb"
)
FULL_FINGERPRINT = (
    "1873803765e70de69f4b398dcea7e4dc66c2749950c9d5f8c0d2d198c5087c71"
)
TOTAL_OBJECTS = 284
TOTAL_LOGICAL_BINDINGS = 296
TOTAL_BYTES = 59_719_895_414
RETAINED_OBJECTS = 282
RETAINED_LOGICAL_BINDINGS = 294
RETAINED_BYTES = 59_185_856_724
QUARANTINED_OBJECTS = 2
QUARANTINED_LOGICAL_BINDINGS = 2
QUARANTINED_BYTES = 534_038_690
RETAINED_LINES = 58_144_912
EXPECTED_BLANK_CONTROLS = 1_111
EXPECTED_DATA_FRAME_ROWS = 58_143_241
EXPECTED_SEGMENT_RECEIPT_ROWS = 560
EXPECTED_MARKER_COUNTS = {"hour_open": 575, "transport_close": 536}
EXPECTED_AUDIT_MARKER_COUNTS = {
    **EXPECTED_MARKER_COUNTS,
    "segment_receipt": EXPECTED_SEGMENT_RECEIPT_ROWS,
}
EXPECTED_BLANK_CONTROL_MARKERS = (
    "gap", "hour_open", "loss", "transport_close", "transport_error",
)
MISSION_SHA256 = "9b4ca417dca394223ecdc6719cd1628bd5ad69d5a80003db304f73b1c485a69c"
FAILED_STATE_SHA256 = "cc59ac42d6a8204650d3f60554fddfce2af9e849522f0370ea030f08c9a078b6"
FAILED_RESOURCE_SHA256 = "a5adca32034c7db02652433ca733fa3657ffeaadbda0809d7f33df8f11230766"
FAILED_INPUT_SHA256 = "68a7e3aeb22851195769c5e4ab5b614979212e0469ac2584afb4de50870560e1"
FAILED_SCRATCH_RECEIPT_SHA256 = (
    "6cc399ef7dbcfa4c32adbbdcf507940d0253c2e7bbc6e40000da1cc3c176c197"
)
FAILED_SCRATCH_SHA256 = (
    "32b2352dc0390fa5a4f42f2a483bdfa570b9e2a7a6de77d51847afb38fd1cff5"
)
FAILED_SCRATCH_BYTES = 28_731_781_120
FAILED_SCRATCH_MTIME = "2026-07-15T15:28:30.567397717+00:00"
AUDIT_SHA256 = "6d0dc5c16ddad6772657ed7a4ede1f90d255e6629ffd1babbd837b3863de7a0d"
AUDIT_RESOURCE_SHA256 = (
    "d820ac998a57428e64a46485cd17319c0430ef9c4c4e516afaac67bec4a3c05d"
)
AUDIT_SOURCE_SHA256 = (
    "27199557ea7aacf9a19f66d16f5fd3770ee0149ef0ed4cd081169a9e0ccb01ea"
)
AUDITED_PARSER_CONTRACT_SHA256 = (
    "9fd339e35584a38372ad4f14ba89f0d7a8a38bdd17ebcc3ab53c3d77d19c6389"
)
AUDITED_PARSER_CONTRACT = {
    "data_frame_marker": "MARKER_FIELD_ABSENT",
    "data_frame_raw": "NONEMPTY_JSON_OBJECT",
    "empty_control_markers": list(EXPECTED_BLANK_CONTROL_MARKERS),
    "empty_control_raw": "EXACT_EMPTY_STRING",
    "explicit_null_marker": "FAIL_CLOSED",
    "json_object_payload_markers": ["segment_receipt"],
    "marker_case": "EXACT_CASE_SENSITIVE",
    "missing_null_or_non_string_raw": "FAIL_CLOSED",
    "raw_b64_without_raw": "FAIL_CLOSED",
    "schema_version": "rfq-inner-payload-parser-contract-v1",
    "unknown_or_invalid_marker": "FAIL_CLOSED",
    "valid_non_object_inner_json": "FAIL_CLOSED",
}
TRIAL_IDS = ("RFQ_FULL_STAGE_ATTEMPT_03", "RFQ_PARSER_CONTRACT_REPAIR_03")

STATE = Path("REPORT/tables/RFQ_FULL_STAGE_STATE.json")
RESOURCE = Path("logs/resources/rfq_full_stage_repair02.json")
SCRATCH_RECEIPT = Path("DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_03.json")
INPUT_IDENTITY = Path("DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json")
AUDIT = Path("DATA_INTEGRITY/RFQ_INNER_PAYLOAD_CONTRACT_AUDIT_03.json")
AUDIT_RESOURCE = Path("logs/resources/rfq_inner_payload_contract_audit03_final.json")
AUDIT_SOURCE = Path("tmp/rfq_inner_payload_contract_audit03.py")
CYCLE1_BINDING = Path("DATA_INTEGRITY/CYCLE1_DUCKDB_BINDING.json")
RFQ_SUMMARY = Path("REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json")
RFQ_REPORT = Path("REPORT/RFQ_FULL_STAGE.md")
RFQ_CATALOG = Path("cache/rfq_full_catalog.duckdb")
SCRATCH = Path("cache/rfq_full_scratch.duckdb")
PRESERVED_SCRATCH = Path("cache/rfq_full_scratch.attempt03_failed.duckdb")
TRANSACTION_JOURNAL = "TRANSACTION_JOURNAL.json"
REGISTRY_APPEND_PROGRESS = "REGISTRY_APPEND_PROGRESS.json"
REGISTRY_APPEND_PROGRESS_SCHEMA = "repair03-registry-append-progress-v1"
PARSER_CONTRACT_FILE = "RFQ_INNER_PAYLOAD_PARSER_CONTRACT.json"
AUTHORITY_FILE = "AUTHORITY_BASIS.json"
AUDIT_SOURCE_FILE = "RFQ_INNER_PAYLOAD_CONTRACT_AUDIT_SOURCE.py"
SOURCE_ATTESTATION = Path("DATA_INTEGRITY/REPAIR_03_SOURCE_ATTESTATION.json")
RUN_LOCK = Path("tmp/repair03_registration.lock")
SOURCE_ATTESTATION_SCHEMA = "sports-autoresearch-source-snapshot-attestation-v1"
SOURCE_RELATIVE = "sandbox/research/deep_autoresearch"
GIT_SOURCE_MODE = "LOCAL_GIT_CLEAN_COMMITTED_HEAD"
SNAPSHOT_SOURCE_MODE = "REMOTE_GITLESS_ATTESTED_COMMITTED_SOURCE_SNAPSHOT"
EXPECTED_REPAIR03_CHANGED_PATHS = [
    f"{SOURCE_RELATIVE}/finalize_mission.py",
    f"{SOURCE_RELATIVE}/repair03_registration.py",
    f"{SOURCE_RELATIVE}/rfq_full_stage.py",
    f"{SOURCE_RELATIVE}/test_finalize_mission.py",
    f"{SOURCE_RELATIVE}/test_repair03_registration.py",
    f"{SOURCE_RELATIVE}/test_rfq_full_stage.py",
]


def fail(message: str) -> None:
    raise Repair03RegistrationError(message)


def _transaction_fault_hook(boundary: str) -> None:
    """Unit-test hook; production execution intentionally does nothing."""
    del boundary


@contextmanager
def _exclusive_run_lock(run_dir: Path) -> Iterator[int]:
    """Hold one symlink-safe, nonblocking repair-03 owner lock for the whole run.

    The descriptor is acquired before RUN_MANIFEST or recovery state is read and
    remains locked through validation, publication, commit, rollback, and final
    verification.  A competing registrar therefore cannot mistake a live
    transaction directory for an orphan.  The lock file is persistent and is
    never truncated or used as a data channel.
    """
    if run_dir.is_symlink() or not run_dir.is_dir():
        fail("run-dir is missing or unsafe before repair-03 lock acquisition")
    lock_parent = run_dir / RUN_LOCK.parent
    if lock_parent.is_symlink() or not lock_parent.is_dir():
        fail("repair-03 lock directory is missing or unsafe")

    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_descriptor = os.open(lock_parent, directory_flags)
    except OSError as exc:
        fail(f"cannot open repair-03 lock directory safely: {exc}")

    descriptor: int | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(
                RUN_LOCK.name,
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
        except OSError as exc:
            fail(f"repair-03 run lock is missing or unsafe: {exc}")
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode):
            fail("repair-03 run lock is not a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                fail("repair-03 registration is already active for this run")
            fail(f"cannot acquire repair-03 run lock: {exc}")

        path_stat = os.stat(
            RUN_LOCK.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        locked_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino)
            != (locked_stat.st_dev, locked_stat.st_ino)
        ):
            fail("repair-03 run lock inode changed during acquisition")
        yield descriptor
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        os.close(directory_descriptor)


def canonical_hash(value: Any) -> str:
    return base.sha256_bytes(base.json_payload(value))


def _registry_progress_payload(
    run_id: str,
    original: bytes,
    append: bytes,
    state: str,
    reported_bytes: int,
) -> bytes:
    if state not in {
        "NOT_STARTED", "WRITE_IN_PROGRESS", "WRITE_RETURNED",
        "APPEND_COMPLETE", "ROLLED_BACK",
    }:
        fail("invalid repair-03 registry append progress state")
    if (
        isinstance(reported_bytes, bool)
        or not isinstance(reported_bytes, int)
        or reported_bytes < 0
        or reported_bytes > len(append)
    ):
        fail("invalid repair-03 registry append progress byte count")
    return base.json_payload({
        "schema_version": REGISTRY_APPEND_PROGRESS_SCHEMA,
        "repair_id": REPAIR_ID,
        "run_id": run_id,
        "state": state,
        "original_sha256": base.sha256_bytes(original),
        "append_sha256": base.sha256_bytes(append),
        "append_bytes": len(append),
        "reported_bytes": reported_bytes,
    })


def load(path: Path, label: str) -> dict[str, Any]:
    return base.load_json(path, label)


def _safe_relative_file_set(root: Path) -> set[str]:
    if not root.is_dir() or root.is_symlink():
        fail("repair-03 transaction directory is missing or unsafe")
    output: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            fail("repair-03 transaction directory contains a symlink")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if relative in output:
                fail("repair-03 transaction directory contains duplicate paths")
            output.add(relative)
    return output


def verify_bound_file(
    run_dir: Path, relative: Any, expected_sha: Any, label: str
) -> Path:
    if not isinstance(relative, str):
        fail(f"{label} path is missing")
    digest = base.require_sha(expected_sha, f"{label} SHA-256")
    path = base.checked_run_path(run_dir, relative, label)
    if path.is_symlink() or not path.is_file() or base.sha256(path) != digest:
        fail(f"{label} path/hash binding mismatch")
    return path


def _read_locked_file(descriptor: int) -> bytes:
    """Read one locked regular file completely and detect a concurrent growth."""
    size = os.fstat(descriptor).st_size
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = size + 1
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, payload: bytes) -> None:
    """Complete a regular-file write even when the kernel reports short writes."""
    offset = 0
    while offset < len(payload):
        count = os.write(descriptor, payload[offset:])
        if not isinstance(count, int) or count <= 0 or count > len(payload) - offset:
            fail("TRIAL_REGISTRY rewrite made no safe forward progress")
        offset += count


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _cas_replace_active_file(
    path: Path,
    expected: bytes,
    replacement: bytes,
    label: str,
    *,
    inject_fault: bool = False,
) -> None:
    """Conditionally replace one active file without a compare/rename gap.

    POSIX rename has no portable compare-and-swap flag.  Instead, atomically
    move the live name to a unique displaced name, inspect those exact bytes,
    and publish a fully fsynced replacement with hard-link no-replace
    semantics.  A mutation before the move is restored and rejected; a writer
    that creates the live name during the short gap is never overwritten.
    """
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        fail(f"active CAS parent is missing or unsafe: {label}")
    mode = 0o600
    if path.exists() and not path.is_symlink():
        mode = stat.S_IMODE(path.stat().st_mode)

    replacement_descriptor, replacement_name = tempfile.mkstemp(
        prefix=f".{path.name}.repair03-new-", dir=parent
    )
    replacement_path = Path(replacement_name)
    displaced_descriptor, displaced_name = tempfile.mkstemp(
        prefix=f".{path.name}.repair03-old-", dir=parent
    )
    os.close(displaced_descriptor)
    displaced_path = Path(displaced_name)
    displaced_path.unlink()
    displaced = False
    replacement_linked = False
    preserve_displaced = False
    try:
        os.fchmod(replacement_descriptor, mode)
        prepared_descriptor = replacement_descriptor
        replacement_descriptor = -1
        with os.fdopen(prepared_descriptor, "wb", closefd=True) as handle:
            count = handle.write(replacement)
            if count != len(replacement):
                fail(f"active CAS replacement preparation was partial: {label}")
            handle.flush()
            os.fsync(handle.fileno())

        if inject_fault:
            _transaction_fault_hook(f"{label}:at_conditional_replace")
        try:
            os.rename(path, displaced_path)
        except OSError as exc:
            fail(f"active CAS could not displace expected file {label}: {exc}")
        displaced = True
        if (
            displaced_path.is_symlink()
            or not displaced_path.is_file()
            or displaced_path.read_bytes() != expected
        ):
            try:
                os.link(displaced_path, path, follow_symlinks=False)
            except FileExistsError:
                preserve_displaced = True
                fail(
                    f"active CAS mismatch and foreign live path blocks restore: {label}; "
                    f"displaced bytes preserved at {displaced_path}"
                )
            displaced_path.unlink()
            displaced = False
            _fsync_directory(parent)
            fail(f"active CAS compared unequal bytes: {label}")

        try:
            os.link(replacement_path, path, follow_symlinks=False)
        except FileExistsError:
            preserve_displaced = True
            fail(
                f"foreign writer created active path during CAS: {label}; "
                f"prior bytes preserved at {displaced_path}"
            )
        replacement_linked = True
        replacement_path.unlink()
        displaced_path.unlink()
        displaced = False
        _fsync_directory(parent)
        if path.is_symlink() or not path.is_file() or path.read_bytes() != replacement:
            fail(f"active CAS replacement verification failed: {label}")
    finally:
        if replacement_descriptor >= 0:
            os.close(replacement_descriptor)
        if replacement_path.exists():
            replacement_path.unlink()
        if displaced and displaced_path.exists() and not preserve_displaced:
            if not path.exists():
                try:
                    os.link(displaced_path, path, follow_symlinks=False)
                    _fsync_directory(parent)
                except FileExistsError:
                    preserve_displaced = True
            if not preserve_displaced:
                displaced_path.unlink()
        if replacement_linked and path.exists():
            # The linked live name is intentionally retained.  This branch is
            # present only to make ownership explicit during exception cleanup.
            pass


def _rewrite_locked_registry(
    descriptor: int, expected: bytes, replacement: bytes
) -> None:
    """Rewrite an already locked registry without accepting unknown bytes."""
    if _read_locked_file(descriptor) != expected:
        fail("TRIAL_REGISTRY changed during locked rewrite")
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    _write_all(descriptor, replacement)
    os.fsync(descriptor)
    if _read_locked_file(descriptor) != replacement:
        fail("TRIAL_REGISTRY locked rewrite verification failed")


def _registry_write_locked(path: Path, expected: bytes, replacement: bytes) -> None:
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        stat_path = path.stat()
        stat_fd = os.fstat(descriptor)
        if (stat_path.st_dev, stat_path.st_ino) != (stat_fd.st_dev, stat_fd.st_ino):
            fail("TRIAL_REGISTRY inode changed during rollback")
        _rewrite_locked_registry(descriptor, expected, replacement)
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def recover_incomplete_transaction(run_dir: Path, repair_dir: Path) -> list[str]:
    """Restore journal-recognized active bytes while retaining foreign appends."""
    if repair_dir.is_symlink() or not repair_dir.is_dir():
        fail("orphan repair-03 transaction directory is missing or unsafe")
    journal = load(repair_dir / TRANSACTION_JOURNAL, "repair-03 transaction journal")
    if (
        journal.get("schema_version") != JOURNAL_SCHEMA
        or journal.get("repair_id") != REPAIR_ID
        or journal.get("run_id") != run_dir.name
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
    ):
        fail("orphan repair-03 directory lacks a valid recovery journal")
    expected_files = journal.get("expected_repair_files")
    mutations = journal.get("active_mutations")
    if (
        not isinstance(expected_files, list)
        or not expected_files
        or len(expected_files) != len(set(expected_files))
        or not isinstance(mutations, list)
        or not mutations
        or REGISTRY_APPEND_PROGRESS not in expected_files
        or _safe_relative_file_set(repair_dir) != set(expected_files)
    ):
        fail("orphan repair-03 transaction file set mismatch")
    progress = load(
        repair_dir / REGISTRY_APPEND_PROGRESS,
        "repair-03 registry append progress",
    )
    base.require_exact_keys(
        progress,
        {
            "schema_version", "repair_id", "run_id", "state",
            "original_sha256", "append_sha256", "append_bytes",
            "reported_bytes",
        },
        "repair-03 registry append progress",
    )
    progress_state = progress.get("state")
    reported_bytes = progress.get("reported_bytes")
    if (
        progress.get("schema_version") != REGISTRY_APPEND_PROGRESS_SCHEMA
        or progress.get("repair_id") != REPAIR_ID
        or progress.get("run_id") != run_dir.name
        or progress_state not in {
            "NOT_STARTED", "WRITE_IN_PROGRESS", "WRITE_RETURNED",
            "APPEND_COMPLETE", "ROLLED_BACK",
        }
        or isinstance(reported_bytes, bool)
        or not isinstance(reported_bytes, int)
        or reported_bytes < 0
    ):
        fail("repair-03 registry append progress is invalid")
    manifest = run_dir / "RUN_MANIFEST.json"
    original_manifest_sha = base.require_sha(
        journal.get("original_manifest_sha256"), "transaction original manifest SHA"
    )
    if not manifest.is_file() or base.sha256(manifest) != original_manifest_sha:
        fail("cannot recover repair-03: RUN_MANIFEST is not the parent identity")

    parsed: list[tuple[Path, bytes, bytes, bool]] = []
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
            not isinstance(relative, str) or relative in seen
            or not isinstance(archive_relative, str)
        ):
            fail("transaction mutation paths are invalid or duplicated")
        seen.add(relative)
        active = base.checked_run_path(run_dir, relative, "transaction active path")
        archived = base.checked_run_path(
            run_dir, archive_relative, "transaction original archive"
        )
        replacement = repair_dir / "post_repair" / relative
        try:
            archived.resolve().relative_to(repair_dir.resolve())
            replacement.resolve().relative_to(repair_dir.resolve())
        except ValueError:
            fail("repair-03 transaction payload escapes its repair directory")
        if (
            not active.is_file() or not archived.is_file() or not replacement.is_file()
            or base.sha256(archived) != row.get("original_sha256")
            or base.sha256(replacement) != row.get("replacement_sha256")
        ):
            fail("repair-03 transaction payload hash mismatch")
        parsed.append(
            (active, archived.read_bytes(), replacement.read_bytes(),
             row.get("append_only_registry") is True)
        )

    concurrent: list[str] = []
    for active, original, replacement, append_only in parsed:
        current = active.read_bytes()
        if current == original:
            if (
                append_only
                and progress_state in {"WRITE_RETURNED", "APPEND_COMPLETE"}
                and reported_bytes > 0
            ):
                fail("repair-03 registry progress claims missing appended bytes")
            continue
        if not append_only:
            if current != replacement:
                fail(f"unrecognized concurrent active mutation: {active}")
            _cas_replace_active_file(
                active,
                replacement,
                original,
                f"rollback:{active.relative_to(run_dir).as_posix()}",
            )
            continue
        if not replacement.startswith(original):
            fail("journaled TRIAL_REGISTRY replacement is not append-only")
        own_append = replacement[len(original):]
        if (
            not own_append
            or not current.startswith(original)
            or progress.get("original_sha256") != base.sha256_bytes(original)
            or progress.get("append_sha256") != base.sha256_bytes(own_append)
            or progress.get("append_bytes") != len(own_append)
            or reported_bytes > len(own_append)
        ):
            fail("unrecognized concurrent TRIAL_REGISTRY mutation")
        suffix = current[len(original):]
        if suffix.count(own_append) > 1:
            fail("unrecognized concurrent TRIAL_REGISTRY mutation")
        if own_append in suffix:
            restored = original + suffix.replace(own_append, b"", 1)
        else:
            if progress_state in {"NOT_STARTED", "ROLLED_BACK"}:
                concurrent.append(active.relative_to(run_dir).as_posix())
                continue
            if progress_state == "APPEND_COMPLETE":
                fail("completed repair-03 registry append is missing during recovery")
            # WRITE_RETURNED gives an exact owned byte count.  A process death
            # inside os.write leaves WRITE_IN_PROGRESS, in which case the
            # longest proper prefix at the exact pre-CAS boundary is owned.
            if progress_state == "WRITE_RETURNED":
                owned_prefix_bytes = reported_bytes
            else:
                owned_prefix_bytes = 0
                limit = min(len(suffix), len(own_append) - 1)
                while (
                    owned_prefix_bytes < limit
                    and suffix[owned_prefix_bytes]
                    == own_append[owned_prefix_bytes]
                ):
                    owned_prefix_bytes += 1
            if (
                owned_prefix_bytes == 0
                or not suffix.startswith(own_append[:owned_prefix_bytes])
            ):
                concurrent.append(active.relative_to(run_dir).as_posix())
                continue
            restored = original + suffix[owned_prefix_bytes:]
        if restored != original:
            concurrent.append(active.relative_to(run_dir).as_posix())
        _registry_write_locked(active, current, restored)

    for active, original, _, append_only in parsed:
        current = active.read_bytes()
        if append_only and current.startswith(original):
            continue
        if current != original:
            fail(f"repair-03 rollback verification failed: {active}")
    shutil.rmtree(repair_dir)
    if repair_dir.exists():
        fail("repair-03 orphan directory cleanup failed")
    return concurrent


def _expected_coverage() -> dict[str, Any]:
    return {
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
        "retained_object_set_sha256": RETAINED_FINGERPRINT,
        "quarantined_object_set_sha256": QUARANTINED_FINGERPRINT,
        "retained_selection_fingerprint_sha256": SELECTION_FINGERPRINT,
        "whole_object_quarantine": True,
        "line_salvage": False,
    }


def validate_snapshot_source_layout(
    source_dir: Path,
    repo_root: Path,
    execution_commit: str,
) -> tuple[Path, str, str]:
    """Validate a gitless W09 source snapshot without claiming local Git state."""
    if source_dir.is_symlink() or repo_root.is_symlink():
        fail("snapshot source/repository root must not be symlinks")
    resolved_root = repo_root.resolve()
    resolved_source = source_dir.resolve()
    expected_source = resolved_root / Path(SOURCE_RELATIVE)
    if resolved_source != expected_source or not resolved_source.is_dir():
        fail(
            "snapshot source-dir must be exactly "
            "<repo-root>/sandbox/research/deep_autoresearch"
        )
    if not isinstance(execution_commit, str) or re.fullmatch(
        r"[0-9a-f]{40}", execution_commit
    ) is None:
        fail("snapshot execution commit must be a full lowercase Git SHA")
    if execution_commit == PARENT_EXECUTION_COMMIT:
        fail("snapshot execution commit did not advance past repair-02")
    files = base.source_files(resolved_source)
    if not files:
        fail("snapshot source tree contains no registered .py/.sh files")
    for path in resolved_source.rglob("*"):
        if path.is_symlink():
            fail("snapshot source tree contains a symlink")
    for path in files:
        if path.parent != resolved_source or not path.is_file():
            fail("snapshot registered source is not a direct regular file")
    return resolved_root, SOURCE_RELATIVE, execution_commit


def validate_snapshot_parent_repository(
    run_dir: Path, old_repository: dict[str, Any]
) -> list[str]:
    """Revalidate repair-02's active receipts without remote Git metadata."""
    if (
        old_repository.get("execution_commit") != PARENT_EXECUTION_COMMIT
        or old_repository.get("source_tree_dirty_at_freeze") is not False
        or old_repository.get("source_manifest_path") != "SOURCE_MANIFEST.json"
        or base.sha256(run_dir / "SOURCE_MANIFEST.json")
        != old_repository.get("source_manifest_sha256")
        or base.sha256(run_dir / "SOURCE_SHA256SUMS.txt")
        != old_repository.get("source_sha256s_sha256")
        or base.sha256(run_dir / "QUERY_SHA256SUMS.txt")
        != old_repository.get("query_set_sha256")
    ):
        fail("snapshot mode parent repository/receipt boundary mismatch")
    base.verify_source_sums_match_manifest(
        run_dir / "SOURCE_MANIFEST.json", run_dir / "SOURCE_SHA256SUMS.txt",
        "snapshot repair-02 parent",
    )
    query_files = old_repository.get("query_files")
    if (
        not isinstance(query_files, list) or not query_files
        or len(query_files) != len(set(query_files))
        or any(not isinstance(value, str) for value in query_files)
    ):
        fail("snapshot mode parent query-file binding is invalid")
    checksums = base.parse_checksums(
        run_dir / "QUERY_SHA256SUMS.txt", "snapshot parent QUERY_SHA256SUMS"
    )
    if [relative for _, relative in checksums] != query_files:
        fail("snapshot mode parent query receipt paths changed")
    query_names: list[str] = []
    for digest, relative in checksums:
        pure = PurePosixPath(relative)
        if pure.parent != PurePosixPath("queries") or pure.suffix != ".py":
            fail(f"snapshot mode parent query path is unsafe: {relative}")
        frozen = base.checked_run_path(run_dir, relative, "snapshot parent query")
        if frozen.is_symlink() or not frozen.is_file() or base.sha256(frozen) != digest:
            fail(f"snapshot mode parent query changed: {relative}")
        query_names.append(pure.name)
    return query_names


def validate_source_snapshot_attestation(
    run_dir: Path,
    execution_commit: str,
    source_manifest_sha256: str,
    source_sha256s_sha256: str,
) -> tuple[dict[str, Any], str]:
    """Consume the operator-created direct-parent/source-receipt attestation."""
    path = run_dir / SOURCE_ATTESTATION
    if path.is_symlink() or not path.is_file():
        fail("repair-03 source snapshot attestation is missing or unsafe")
    payload = path.read_bytes()
    try:
        attestation = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"repair-03 source snapshot attestation is invalid JSON: {exc}")
    if not isinstance(attestation, dict):
        fail("repair-03 source snapshot attestation must be an object")
    base.require_exact_keys(
        attestation,
        {
            "schema_version", "run_id", "mission_sha256",
            "parent_execution_commit", "execution_commit",
            "direct_parent_verified", "git_tree", "source_relative",
            "source_tree_clean_at_attestation", "source_manifest_sha256",
            "source_sha256s_sha256", "commit_changed_paths", "attested_at_utc",
        },
        "repair-03 source snapshot attestation",
    )
    git_tree = attestation.get("git_tree")
    if (
        attestation.get("schema_version") != SOURCE_ATTESTATION_SCHEMA
        or attestation.get("run_id") != run_dir.name
        or attestation.get("mission_sha256") != MISSION_SHA256
        or attestation.get("parent_execution_commit") != PARENT_EXECUTION_COMMIT
        or attestation.get("execution_commit") != execution_commit
        or attestation.get("direct_parent_verified") is not True
        or not isinstance(git_tree, str)
        or re.fullmatch(r"[0-9a-f]{40}", git_tree) is None
        or attestation.get("source_relative") != SOURCE_RELATIVE
        or attestation.get("source_tree_clean_at_attestation") is not True
        or attestation.get("source_manifest_sha256") != source_manifest_sha256
        or attestation.get("source_sha256s_sha256") != source_sha256s_sha256
        or attestation.get("commit_changed_paths")
        != EXPECTED_REPAIR03_CHANGED_PATHS
    ):
        fail("repair-03 source snapshot attestation mismatch")
    base.require_utc_timestamp(
        attestation.get("attested_at_utc"), "source snapshot attestation time"
    )
    return attestation, base.sha256_bytes(payload)


def validate_parent_repair(
    run_dir: Path, manifest: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], bytes, list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate the exact committed repair-02 boundary before changing anything."""
    if manifest.get("run_id") != run_dir.name or manifest.get("analysis_started") is not True:
        fail("RUN_MANIFEST is not the active post-core run")
    if (
        manifest.get("status") != EXPECTED_STATUS
        or manifest.get("registration_state")
        != "RE_FROZEN_AFTER_DATA_INTEGRITY_REPAIR02_BEFORE_RFQ_RESULT"
    ):
        fail("active manifest is not the registered repair-02 boundary")
    for result in (RFQ_SUMMARY, RFQ_REPORT, RFQ_CATALOG):
        if (run_dir / result).exists():
            fail("RFQ result exists; repair-03 preregistration is forbidden")

    repairs = manifest.get("data_integrity_repairs")
    if (
        not isinstance(repairs, list) or len(repairs) != 2
        or [row.get("repair_id") if isinstance(row, dict) else None for row in repairs]
        != ["repair-01", "repair-02"]
    ):
        fail("repair-03 requires the immutable repair-01 -> repair-02 chain")
    parent = repairs[1]
    if (
        parent.get("schema_version") != "sports-autoresearch-data-integrity-repair-v2"
        or parent.get("parent_repair_id") != "repair-01"
        or parent.get("post_repair_status") != EXPECTED_STATUS
        or parent.get("current_execution_commit") != PARENT_EXECUTION_COMMIT
        or parent.get("rfq_result_state") != NO_RFQ_RESULT
        or parent.get("coverage") != _expected_coverage()
        or parent.get("newly_quarantined_objects") in (None, [])
    ):
        fail("repair-02 parent record identity/coverage mismatch")
    cumulative = parent.get("cumulative_quarantined_objects")
    if (
        not isinstance(cumulative, list) or len(cumulative) != QUARANTINED_OBJECTS
        or any(not isinstance(row, dict) for row in cumulative)
        or len({row.get("key") for row in cumulative}) != QUARANTINED_OBJECTS
        or sum(row.get("size", 0) for row in cumulative) != QUARANTINED_BYTES
    ):
        fail("repair-02 cumulative quarantine is not the exact two-object set")

    receipt_relative = "DATA_INTEGRITY/repairs/repair-02/REPAIR_REGISTRATION.json"
    if parent.get("repair_receipt_path") != receipt_relative:
        fail("repair-02 registration receipt path mismatch")
    receipt_path = verify_bound_file(
        run_dir, receipt_relative, parent.get("repair_receipt_sha256"),
        "repair-02 registration receipt",
    )
    receipt = load(receipt_path, "repair-02 registration receipt")
    without_self = copy.deepcopy(parent)
    without_self.pop("repair_receipt_path", None)
    without_self.pop("repair_receipt_sha256", None)
    if receipt != without_self:
        fail("repair-02 registration receipt/manifest record mismatch")
    verify_bound_file(
        run_dir, parent.get("transaction_journal_path"),
        parent.get("transaction_journal_sha256"), "repair-02 transaction journal",
    )
    archive = base.checked_run_path(
        run_dir, str(parent.get("archive_path")), "repair-02 pre-repair archive"
    )
    if parent.get("archive_inventory") != base.archive_inventory(archive):
        fail("repair-02 archive inventory changed")

    repository = manifest.get("repository")
    if (
        not isinstance(repository, dict)
        or repository.get("execution_commit") != PARENT_EXECUTION_COMMIT
        or repository.get("registration_repair_id") != PARENT_REPAIR_ID
        or base.repository_identity(repository) != parent.get("current_repository_identity")
        or repository.get("identity_history") != parent.get("repository_identity_chain")
        or not isinstance(repository.get("identity_history"), list)
        or len(repository["identity_history"]) != 3
    ):
        fail("active repository identity is not repair-02")
    base.verify_source_sums_match_manifest(
        run_dir / "SOURCE_MANIFEST.json", run_dir / "SOURCE_SHA256SUMS.txt",
        "repair-02 current",
    )
    if (
        base.sha256(run_dir / "SOURCE_MANIFEST.json")
        != repository.get("source_manifest_sha256")
        or base.sha256(run_dir / "SOURCE_SHA256SUMS.txt")
        != repository.get("source_sha256s_sha256")
        or base.sha256(run_dir / "QUERY_SHA256SUMS.txt")
        != repository.get("query_set_sha256")
    ):
        fail("active repair-02 source/query receipts changed")

    registry_path = run_dir / "TRIAL_REGISTRY.jsonl"
    if not registry_path.is_file() or registry_path.is_symlink():
        fail("TRIAL_REGISTRY.jsonl is missing or unsafe")
    registry = registry_path.read_bytes()
    trial = parent.get("trial_registry")
    if (
        not registry or not registry.endswith(b"\n")
        or not isinstance(trial, dict)
        or trial.get("trial_registration_ids")
        != ["RFQ_FULL_STAGE_ATTEMPT_02", "RFQ_OBJECT_QUARANTINE_REPAIR_02"]
        or trial.get("appended_records") != 2
        or trial.get("strict_previous_bytes_prefix") is not True
        or trial.get("current_bytes") != len(registry)
        or trial.get("current_sha256") != base.sha256_bytes(registry)
    ):
        fail("TRIAL_REGISTRY is not the exact repair-02 append boundary")
    records: list[dict[str, Any]] = []
    registration_ids: list[str] = []
    for line_number, raw_line in enumerate(registry.splitlines(), 1):
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            fail(f"TRIAL_REGISTRY line {line_number} is invalid JSON: {exc}")
        if not isinstance(row, dict):
            fail(f"TRIAL_REGISTRY line {line_number} is not an object")
        records.append(row)
        if row.get("trial_registration_id") is not None:
            registration_ids.append(row["trial_registration_id"])
        if str(row.get("result_artifact", "")).endswith("RFQ_FULL_STAGE_SUMMARY.json"):
            fail("TRIAL_REGISTRY already contains an RFQ full-stage result")
    if registration_ids != [
        "RFQ_FULL_STAGE_ATTEMPT_01", "RFQ_OBJECT_QUARANTINE_REPAIR_01",
        "RFQ_FULL_STAGE_ATTEMPT_02", "RFQ_OBJECT_QUARANTINE_REPAIR_02",
    ]:
        fail("TRIAL_REGISTRY repair history is not the exact four-record boundary")

    core = base.core_result_inventory(run_dir)
    if parent.get("core_result_artifacts") != core:
        fail("Cycle-1 core artifacts changed after repair-02")
    if parent.get("cycle1_duckdb_binding") != repairs[0].get("cycle1_duckdb_binding"):
        fail("repair-02 changed the Cycle-1 DuckDB binding")
    return parent, copy.deepcopy(repository), registry, records, core


def validate_failed_attempt(
    run_dir: Path, parent: dict[str, Any]
) -> dict[str, Any]:
    """Bind attempt 03's state/resource/input without interpreting outcomes."""
    state_path = run_dir / STATE
    resource_path = run_dir / RESOURCE
    input_path = run_dir / INPUT_IDENTITY
    if (
        base.sha256(state_path) != FAILED_STATE_SHA256
        or base.sha256(resource_path) != FAILED_RESOURCE_SHA256
        or base.sha256(input_path) != FAILED_INPUT_SHA256
    ):
        fail("attempt-03 active evidence hashes are not the approved failure boundary")
    state = load(state_path, "attempt-03 failed state")
    expected_scratch = f"/srv/w09-research/runs/{run_dir.name}/{SCRATCH.as_posix()}"
    if (
        state.get("schema") != "rfq-full-stage-state-v1"
        or state.get("run_id") != run_dir.name
        or state.get("status") != FAILED_STAGE_STATUS
        or state.get("resume") is not False
        or state.get("input_fingerprint") != SELECTION_FINGERPRINT
        or state.get("next_required_authority")
        != "EXPLICIT_NEW_PREREGISTRATION_OR_RFQ_SCOPE_TERMINATION"
        or state.get("scratch") != expected_scratch
        or state.get("error")
        != "unexpected malformed inner RFQ payload in a consumed object; aborting"
        or state.get("error_type") != "RFQStageError"
        or state.get("expected_success_resource")
        != {
            "label": "rfq_full_stage_repair02",
            "path": "logs/resources/rfq_full_stage_repair02.json",
        }
    ):
        fail("attempt-03 state is not the exact fail-closed parser-contract failure")

    resource = load(resource_path, "attempt-03 resource receipt")
    command = resource.get("command")
    if (
        resource.get("schema_version") != "w09-stage-resource-v1"
        or resource.get("label") != "rfq_full_stage_repair02"
        or resource.get("return_code") != 1
        or not isinstance(command, list) or not command
        or "--resume" in command
        or not any(str(item).endswith("/source/rfq_full_stage.py") for item in command)
    ):
        fail("attempt-03 resource receipt mismatch")

    identity = load(input_path, "attempt-03 RFQ input identity")
    consumed = identity.get("consumed_objects")
    if (
        identity.get("schema") != "rfq-full-input-identity-v2"
        or identity.get("run_id") != run_dir.name
        or identity.get("coverage_status") != "PARTIAL_OBJECT_COVERAGE_QUARANTINED"
        or identity.get("full_object_coverage") is not False
        or identity.get("whole_object_quarantine") is not True
        or identity.get("line_salvage") is not False
        or identity.get("logical_manifest_bindings_total") != TOTAL_LOGICAL_BINDINGS
        or identity.get("unique_objects_total") != TOTAL_OBJECTS
        or identity.get("unique_bytes_total") != TOTAL_BYTES
        or identity.get("consumed_unique_objects") != RETAINED_OBJECTS
        or identity.get("consumed_logical_bindings") != RETAINED_LOGICAL_BINDINGS
        or identity.get("consumed_bytes") != RETAINED_BYTES
        or identity.get("manifest_object_set_sha256") != FULL_FINGERPRINT
        or identity.get("consumed_object_set_sha256") != RETAINED_FINGERPRINT
        or identity.get("quarantined_unique_objects") != QUARANTINED_OBJECTS
        or identity.get("quarantined_logical_bindings") != QUARANTINED_LOGICAL_BINDINGS
        or identity.get("quarantined_bytes") != QUARANTINED_BYTES
        or identity.get("quarantined_object_set_sha256") != QUARANTINED_FINGERPRINT
        or identity.get("selection_fingerprint_sha256") != SELECTION_FINGERPRINT
        or not isinstance(consumed, list) or len(consumed) != RETAINED_OBJECTS
        or repair02.object_fingerprint(consumed) != RETAINED_FINGERPRINT
        or identity.get("cycle1_duckdb_binding") != parent.get("cycle1_duckdb_binding")
        or identity.get("expected_success_resource")
        != state.get("expected_success_resource")
        or not isinstance(identity.get("repair_chain"), list)
        or [row.get("repair_id") for row in identity["repair_chain"]]
        != ["repair-01", "repair-02"]
        or not isinstance(identity.get("failed_attempt_bindings"), list)
        or len(identity["failed_attempt_bindings"]) != 2
    ):
        fail("attempt-03 input identity is not the exact repair-02 retained set")
    if state.get("repair_chain") != identity.get("repair_chain"):
        fail("attempt-03 state/input repair chain mismatch")
    if state.get("failed_attempt_bindings") != identity.get("failed_attempt_bindings"):
        fail("attempt-03 state/input failed-attempt chain mismatch")
    return {
        "state": state,
        "resource": resource,
        "input": identity,
        "state_sha256": base.sha256(state_path),
        "resource_sha256": base.sha256(resource_path),
        "input_sha256": base.sha256(input_path),
    }


def validate_failed_scratch(run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    receipt_path = run_dir / SCRATCH_RECEIPT
    if base.sha256(receipt_path) != FAILED_SCRATCH_RECEIPT_SHA256:
        fail("attempt-03 scratch receipt hash is not the approved boundary")
    receipt = load(receipt_path, "attempt-03 failed scratch receipt")
    base.require_exact_keys(
        receipt,
        {
            "bytes", "disposition", "input_fingerprint", "mtime_utc",
            "original_scratch_path", "preserved_scratch_path", "resume_allowed",
            "run_id", "schema_version", "sha256",
        },
        "attempt-03 failed scratch receipt",
    )
    expected_preserved = str(Path(state["scratch"]).with_name(PRESERVED_SCRATCH.name))
    if (
        receipt.get("schema_version") != "rfq-failed-scratch-receipt-v1"
        or receipt.get("run_id") != run_dir.name
        or receipt.get("original_scratch_path") != state.get("scratch")
        or receipt.get("preserved_scratch_path") != expected_preserved
        or receipt.get("input_fingerprint") != SELECTION_FINGERPRINT
        or receipt.get("disposition") != "PRESERVED_RENAMED_NO_RESUME"
        or receipt.get("resume_allowed") is not False
        or receipt.get("bytes") != FAILED_SCRATCH_BYTES
        or receipt.get("sha256") != FAILED_SCRATCH_SHA256
        or receipt.get("mtime_utc") != FAILED_SCRATCH_MTIME
    ):
        fail("attempt-03 scratch receipt does not enforce exact preserve/no-resume")
    expected_sha = base.require_sha(receipt.get("sha256"), "attempt-03 scratch SHA")
    expected_bytes = base.require_positive_int(
        receipt.get("bytes"), "attempt-03 scratch bytes"
    )
    # The filesystem receipt preserves nanosecond precision (nine fractional
    # digits), which ``datetime.fromisoformat`` does not accept on all Python
    # versions.  Equality to the approved UTC timestamp above is stricter than
    # reparsing and truncating it here.
    active = run_dir / SCRATCH
    wal = Path(str(active) + ".wal")
    preserved = run_dir / PRESERVED_SCRATCH
    if active.exists() or wal.exists():
        fail("active attempt-03 scratch/WAL still exists; fresh scratch is not safe")
    if (
        preserved.is_symlink() or not preserved.is_file()
        or preserved.stat().st_size != expected_bytes
        or base.sha256(preserved) != expected_sha
    ):
        fail("preserved attempt-03 scratch does not match its receipt")
    return receipt


def validate_inner_payload_audit(
    run_dir: Path, input_sha256: str
) -> tuple[dict[str, Any], dict[str, Any], str]:
    audit_path = run_dir / AUDIT
    resource_path = run_dir / AUDIT_RESOURCE
    source_path = run_dir / AUDIT_SOURCE
    if (
        base.sha256(audit_path) != AUDIT_SHA256
        or base.sha256(resource_path) != AUDIT_RESOURCE_SHA256
        or base.sha256(source_path) != AUDIT_SOURCE_SHA256
    ):
        fail("inner-payload audit evidence hashes are not the approved boundary")
    audit = load(audit_path, "inner-payload contract audit")
    base.require_exact_keys(
        audit,
        {
            "analysis_result_opened", "audit_script_path", "audit_script_sha256",
            "bytes_scanned", "completed_at_utc", "data_frame_rows",
            "expected_empty_control_marker_allowlist",
            "expected_empty_control_marker_rows", "identity_mismatch_count",
            "identity_mismatches", "input_fingerprint", "input_identity_path",
            "input_identity_sha256", "json_object_payload_marker_contract",
            "lines_scanned", "marker_counts", "object_summaries",
            "objects_scanned", "outer_invalid_line_count",
            "outer_invalid_object_count", "parser_contract",
            "parser_contract_sha256", "raw_payload_redacted", "run_id",
            "schema_version", "scope", "segment_receipt_rows", "started_at_utc",
            "status", "unexpected_inner_payload_object_count",
            "unexpected_inner_payload_objects", "unexpected_inner_payload_row_count",
            "wall_seconds", "workers",
        },
        "inner-payload contract audit",
    )
    audited_contract = audit.get("parser_contract")
    audited_contract_hash = hashlib.sha256(
        json.dumps(
            audited_contract, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    if (
        audit.get("schema_version") != "rfq-inner-payload-contract-audit-v1"
        or audit.get("run_id") != run_dir.name
        or audit.get("status") != "COMPLETE_INNER_PAYLOAD_CONTRACT_AUDIT"
        or audit.get("scope")
        != "RETAINED_OBJECT_INNER_PAYLOAD_CONTRACT_ONLY_NO_RESEARCH_RESULT"
        or audit.get("analysis_result_opened") is not False
        or audit.get("raw_payload_redacted") is not True
        or audit.get("audit_script_path") != AUDIT_SOURCE.as_posix()
        or audit.get("audit_script_sha256") != AUDIT_SOURCE_SHA256
        or audit.get("input_identity_path") != INPUT_IDENTITY.as_posix()
        or audit.get("input_identity_sha256") != input_sha256
        or audit.get("input_fingerprint") != SELECTION_FINGERPRINT
        or audit.get("expected_empty_control_marker_allowlist")
        != list(EXPECTED_BLANK_CONTROL_MARKERS)
        or audit.get("objects_scanned") != RETAINED_OBJECTS
        or audit.get("bytes_scanned") != RETAINED_BYTES
        or audit.get("lines_scanned") != RETAINED_LINES
        or audit.get("data_frame_rows") != EXPECTED_DATA_FRAME_ROWS
        or audit.get("segment_receipt_rows") != EXPECTED_SEGMENT_RECEIPT_ROWS
        or audit.get("identity_mismatch_count") != 0
        or audit.get("identity_mismatches") != []
        or audit.get("outer_invalid_object_count") != 0
        or audit.get("outer_invalid_line_count") != 0
        or audit.get("expected_empty_control_marker_rows") != EXPECTED_BLANK_CONTROLS
        or audit.get("unexpected_inner_payload_object_count") != 0
        or audit.get("unexpected_inner_payload_row_count") != 0
        or audit.get("unexpected_inner_payload_objects") != []
        or audit.get("json_object_payload_marker_contract")
        != ["<marker field absent>", "segment_receipt"]
        or audited_contract != AUDITED_PARSER_CONTRACT
        or audit.get("parser_contract_sha256") != AUDITED_PARSER_CONTRACT_SHA256
        or audited_contract_hash != AUDITED_PARSER_CONTRACT_SHA256
        or audit.get("workers") != 8
        or not isinstance(audit.get("wall_seconds"), (int, float))
        or audit.get("wall_seconds") <= 0
    ):
        fail("inner-payload audit does not prove the exact clean retained set")
    markers = audit.get("marker_counts")
    if (
        markers != EXPECTED_AUDIT_MARKER_COUNTS
        or sum(markers.get(key, 0) for key in EXPECTED_BLANK_CONTROL_MARKERS)
        != EXPECTED_BLANK_CONTROLS
    ):
        fail("inner-payload audit blank-control marker counts mismatch")
    summaries = audit.get("object_summaries")
    if not isinstance(summaries, list) or len(summaries) != RETAINED_OBJECTS:
        fail("inner-payload audit object summaries are incomplete")
    input_path = run_dir / INPUT_IDENTITY
    if base.sha256(input_path) != input_sha256:
        fail("inner-payload audit input identity hash changed")
    identity = load(input_path, "inner-payload audit input identity")
    consumed = identity.get("consumed_objects")
    if not isinstance(consumed, list) or len(consumed) != RETAINED_OBJECTS:
        fail("inner-payload audit input object set is incomplete")
    expected_objects = {
        row.get("key"): row for row in consumed if isinstance(row, dict)
    }
    if len(expected_objects) != RETAINED_OBJECTS or None in expected_objects:
        fail("inner-payload audit input object keys are invalid or duplicated")
    total_bytes = 0
    total_lines = 0
    total_controls = 0
    total_frames = 0
    total_receipts = 0
    summary_markers: dict[str, int] = {}
    seen_keys: set[str] = set()
    for index, row in enumerate(summaries):
        if not isinstance(row, dict):
            fail(f"inner-payload audit object summary {index} is not an object")
        key = row.get("key")
        expected = expected_objects.get(key)
        size = row.get("expected_size")
        lines = row.get("total_lines")
        if (
            not isinstance(key, str) or not key.startswith("raw_rfq/")
            or key in seen_keys or not isinstance(expected, dict)
            or not isinstance(size, int) or size <= 0
            or not isinstance(lines, int) or lines <= 0
            or expected.get("size") != size
            or expected.get("sha256") != row.get("expected_sha256")
            or row.get("identity_match") is not True
            or row.get("observed_size") != size
            or row.get("observed_sha256") != row.get("expected_sha256")
            or row.get("outer_invalid_rows") != 0
            or row.get("unexpected_inner_payload_rows") != 0
            or row.get("unexpected_details") != []
            or row.get("raw_payload_redacted") is not True
        ):
            fail(f"inner-payload audit contains an unclean object summary: {key}")
        base.require_sha(
            row.get("expected_sha256"), f"inner-payload audit object {index} SHA"
        )
        category_values = [
            row.get("expected_empty_control_marker_rows"),
            row.get("data_frame_rows"),
            row.get("segment_receipt_rows"),
        ]
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in category_values
        ) or sum(category_values) != lines:
            fail(f"inner-payload audit object row categories mismatch: {key}")
        row_markers = row.get("marker_counts")
        if not isinstance(row_markers, dict):
            fail(f"inner-payload audit object marker counts are invalid: {key}")
        for marker, count in row_markers.items():
            if (
                marker not in EXPECTED_AUDIT_MARKER_COUNTS
                or not isinstance(count, int) or isinstance(count, bool) or count < 0
            ):
                fail(f"inner-payload audit object marker is invalid: {key}")
            summary_markers[marker] = summary_markers.get(marker, 0) + count
        seen_keys.add(key)
        total_bytes += size
        total_lines += lines
        total_controls += category_values[0]
        total_frames += category_values[1]
        total_receipts += category_values[2]
    if (
        seen_keys != set(expected_objects)
        or total_bytes != RETAINED_BYTES
        or total_lines != RETAINED_LINES
        or total_controls != EXPECTED_BLANK_CONTROLS
        or total_frames != EXPECTED_DATA_FRAME_ROWS
        or total_receipts != EXPECTED_SEGMENT_RECEIPT_ROWS
        or summary_markers != EXPECTED_AUDIT_MARKER_COUNTS
    ):
        fail("inner-payload audit object totals do not reconcile")
    base.require_utc_timestamp(audit.get("started_at_utc"), "audit start")
    base.require_utc_timestamp(audit.get("completed_at_utc"), "audit completion")
    resource = load(resource_path, "inner-payload audit resource receipt")
    command = resource.get("command")
    if (
        resource.get("schema_version") != "w09-stage-resource-v1"
        or resource.get("label") != "rfq_inner_payload_contract_audit03_final"
        or resource.get("return_code") != 0
        or not isinstance(command, list)
        or command != [
            "/opt/w09/venv/bin/python",
            f"/srv/w09-research/runs/{run_dir.name}/{AUDIT_SOURCE.as_posix()}",
            "--run-dir", f"/srv/w09-research/runs/{run_dir.name}",
            "--cache-root", "/srv/w09-research/cache", "--workers", "8",
        ]
    ):
        fail("inner-payload audit resource receipt mismatch")
    if source_path.is_symlink() or not source_path.is_file():
        fail("inner-payload audit source is missing or unsafe")
    return audit, resource, base.sha256(source_path)


def make_parser_contract(
    run_id: str,
    created_at: str,
    registered_query_sha256: str,
    archived_audit_path: str,
    audit_sha256: str,
) -> dict[str, Any]:
    """Return the complete, canonical attempt-04 inner-payload contract."""
    return {
        "schema_version": PARSER_CONTRACT_SCHEMA,
        "run_id": run_id,
        "created_at_utc": created_at,
        "finding": FINDING,
        "mission_sha256": MISSION_SHA256,
        "selection_fingerprint_sha256": SELECTION_FINGERPRINT,
        "retained_unique_objects": RETAINED_OBJECTS,
        "retained_bytes": RETAINED_BYTES,
        "outer_ndjson_policy": "STRICT_NDJSON_IGNORE_ERRORS_FALSE",
        "expected_blank_control_markers": list(EXPECTED_BLANK_CONTROL_MARKERS),
        "expected_blank_raw_representation": "EXACT_EMPTY_STRING",
        "non_control_payload_policy": "NONEMPTY_STRING_VALID_JSON_OBJECT_REQUIRED",
        "unexpected_payload_policy": "ABORT_BEFORE_RESULT",
        "line_salvage": False,
        "data_selection_change": False,
        "hypothesis_design_change": False,
        "registered_query_path": "queries/rfq_full_stage.py",
        "registered_query_sha256": registered_query_sha256,
        "audit_path": archived_audit_path,
        "audit_sha256": audit_sha256,
    }


def make_authority_basis(
    run_id: str,
    recorded_at: str,
    archived_audit_path: str,
    audit_sha256: str,
) -> dict[str, Any]:
    """Record why a code-only correction needs no new quarantine authority."""
    return {
        "schema_version": AUTHORITY_SCHEMA,
        "run_id": run_id,
        "recorded_at_utc": recorded_at,
        "mission_sha256": MISSION_SHA256,
        "authority_basis": "MISSION_AUTHORIZED_AUTONOMOUS_RESEARCH_CODE_CORRECTION",
        "permitted_change": "PARSER_CONTRACT_CORRECTION_ONLY",
        "operator_repair02_authorization_reused": False,
        "new_data_integrity_decision": False,
        "data_selection_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
        "prerequisite_audit_path": archived_audit_path,
        "prerequisite_audit_sha256": audit_sha256,
    }


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
        "stage": "RFQ_FULL_STAGE_REPAIR02",
        "failure_class": FINDING,
        "failure_disposition": "PARSER_CONTRACT_FAILURE_BEFORE_RESULT",
        "hypothesis_conclusion": "NONE",
        "failed_state_path": evidence["failed_state_path"],
        "failed_state_sha256": evidence["failed_state_sha256"],
        "failed_resource_receipt_path": evidence["failed_resource_receipt_path"],
        "failed_resource_receipt_sha256": evidence[
            "failed_resource_receipt_sha256"
        ],
        "failed_scratch_receipt_path": evidence["failed_scratch_receipt_path"],
        "failed_scratch_receipt_sha256": evidence[
            "failed_scratch_receipt_sha256"
        ],
        "failed_input_identity_path": evidence["failed_input_identity_path"],
        "failed_input_identity_sha256": evidence["failed_input_identity_sha256"],
        "failed_input_fingerprint": SELECTION_FINGERPRINT,
        "inner_payload_audit_path": evidence["inner_payload_audit_path"],
        "inner_payload_audit_sha256": evidence["inner_payload_audit_sha256"],
        "execution_commit": old_repository["execution_commit"],
        "source_manifest_sha256": old_repository["source_manifest_sha256"],
        "source_sha256s_sha256": old_repository["source_sha256s_sha256"],
        "query_set_sha256": old_repository["query_set_sha256"],
    }
    preregistration = {
        **common,
        "trial_registration_id": TRIAL_IDS[1],
        "record_type": "PARSER_CONTRACT_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR03_PREREGISTRATION",
        "finding": FINDING,
        "registration_change_class": CHANGE_CLASS,
        "rfq_result_state": NO_RFQ_RESULT,
        "retry_requirement": RETRY_REQUIREMENT,
        "parser_contract_path": evidence["parser_contract_path"],
        "parser_contract_sha256": evidence["parser_contract_sha256"],
        "authority_basis_path": evidence["authority_basis_path"],
        "authority_basis_sha256": evidence["authority_basis_sha256"],
        "inner_payload_audit_path": evidence["inner_payload_audit_path"],
        "inner_payload_audit_sha256": evidence["inner_payload_audit_sha256"],
        "newly_quarantined_objects": [],
        "cumulative_quarantined_objects": copy.deepcopy(cumulative),
        "coverage": copy.deepcopy(coverage),
        "previous_selection_fingerprint_sha256": SELECTION_FINGERPRINT,
        "current_selection_fingerprint_sha256": SELECTION_FINGERPRINT,
        "previous_execution_commit": old_repository["execution_commit"],
        "current_execution_commit": current_identity["execution_commit"],
        "previous_source_manifest_sha256": old_repository[
            "source_manifest_sha256"
        ],
        "previous_source_sha256s_sha256": old_repository[
            "source_sha256s_sha256"
        ],
        "current_source_manifest_sha256": current_identity[
            "source_manifest_sha256"
        ],
        "current_source_sha256s_sha256": current_identity[
            "source_sha256s_sha256"
        ],
        "previous_query_set_sha256": old_repository["query_set_sha256"],
        "current_query_set_sha256": current_identity["query_set_sha256"],
        "data_selection_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "core_results_recomputed": False,
    }
    return failure, preregistration


def _validate_committed_contract(
    run_dir: Path, record: dict[str, Any]
) -> None:
    contract_path = verify_bound_file(
        run_dir, record.get("parser_contract_path"),
        record.get("parser_contract_sha256"), "repair-03 parser contract",
    )
    contract = load(contract_path, "repair-03 parser contract")
    expected = make_parser_contract(
        run_dir.name,
        record.get("applied_at_utc"),
        record.get("registered_rfq_query_sha256"),
        record.get("inner_payload_audit_path"),
        record.get("inner_payload_audit_sha256"),
    )
    if contract != expected:
        fail("committed repair-03 parser contract changed")
    authority_path = verify_bound_file(
        run_dir, record.get("authority_basis_path"),
        record.get("authority_basis_sha256"), "repair-03 authority basis",
    )
    authority = load(authority_path, "repair-03 authority basis")
    expected_authority = make_authority_basis(
        run_dir.name,
        record.get("applied_at_utc"),
        record.get("inner_payload_audit_path"),
        record.get("inner_payload_audit_sha256"),
    )
    if authority != expected_authority:
        fail("committed repair-03 authority basis changed")


def validate_already_applied(
    run_dir: Path,
    source_dir: Path,
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
    repo_root: Path | None = None,
    execution_commit: str | None = None,
) -> dict[str, Any]:
    if (
        manifest.get("status") != POST_REPAIR_STATUS
        or manifest.get("registration_state") != REGISTRATION_STATE
        or len(records) != 3
        or [row.get("repair_id") if isinstance(row, dict) else None for row in records]
        != ["repair-01", "repair-02", REPAIR_ID]
    ):
        fail("unknown or inconsistent existing repair-03 history")
    parent, record = records[1], records[2]
    if (
        record.get("schema_version") != REPAIR_SCHEMA
        or record.get("parent_repair_id") != PARENT_REPAIR_ID
        or record.get("pre_repair_status") != EXPECTED_STATUS
        or record.get("post_repair_status") != POST_REPAIR_STATUS
        or record.get("failed_stage_status") != FAILED_STAGE_STATUS
        or record.get("finding") != FINDING
        or record.get("registration_change_class") != CHANGE_CLASS
        or record.get("rfq_result_state") != NO_RFQ_RESULT
        or record.get("retry_requirement") != RETRY_REQUIREMENT
        or record.get("coverage") != _expected_coverage()
        or record.get("newly_quarantined_objects") != []
        or record.get("cumulative_quarantined_objects")
        != parent.get("cumulative_quarantined_objects")
        or record.get("previous_repair_registration_path")
        != parent.get("repair_receipt_path")
        or record.get("previous_repair_registration_sha256")
        != parent.get("repair_receipt_sha256")
        or record.get("previous_repair_record_sha256") != canonical_hash(parent)
    ):
        fail("repair-02/repair-03 append chain changed")

    repair_dir = run_dir / "DATA_INTEGRITY/repairs/repair-03"
    archive = repair_dir / "pre_repair"
    if (
        record.get("archive_path")
        != "DATA_INTEGRITY/repairs/repair-03/pre_repair"
        or record.get("archive_inventory") != base.archive_inventory(archive)
    ):
        fail("repair-03 archive inventory changed")
    receipt_path = verify_bound_file(
        run_dir, record.get("repair_receipt_path"),
        record.get("repair_receipt_sha256"), "repair-03 registration receipt",
    )
    receipt = load(receipt_path, "repair-03 registration receipt")
    without_self = copy.deepcopy(record)
    without_self.pop("repair_receipt_path", None)
    without_self.pop("repair_receipt_sha256", None)
    if receipt != without_self:
        fail("repair-03 receipt/manifest record mismatch")
    journal_path = verify_bound_file(
        run_dir, record.get("transaction_journal_path"),
        record.get("transaction_journal_sha256"), "repair-03 transaction journal",
    )
    journal = load(journal_path, "repair-03 transaction journal")
    if (
        journal.get("schema_version") != JOURNAL_SCHEMA
        or journal.get("repair_id") != REPAIR_ID
        or journal.get("run_id") != run_dir.name
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
        or _safe_relative_file_set(repair_dir)
        != set(journal.get("expected_repair_files", []))
    ):
        fail("repair-03 committed transaction journal mismatch")
    for row in journal.get("active_mutations", []):
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            fail("repair-03 committed transaction mutation is invalid")
        active = base.checked_run_path(
            run_dir, row["path"], "committed repair-03 active mutation"
        )
        if not active.is_file() or base.sha256(active) != row.get(
            "replacement_sha256"
        ):
            fail(f"repair-03 committed active bytes changed: {row['path']}")
    registry_mutations = [
        row for row in journal.get("active_mutations", [])
        if isinstance(row, dict) and row.get("append_only_registry") is True
    ]
    if len(registry_mutations) != 1:
        fail("repair-03 committed registry mutation is missing or duplicated")
    registry_mutation = registry_mutations[0]
    if registry_mutation.get("path") != "TRIAL_REGISTRY.jsonl":
        fail("repair-03 committed append-only mutation is not TRIAL_REGISTRY")
    original_registry = verify_bound_file(
        run_dir,
        registry_mutation.get("original_archive_path"),
        registry_mutation.get("original_sha256"),
        "repair-03 archived original registry",
    ).read_bytes()
    replacement_registry = repair_dir / "post_repair/TRIAL_REGISTRY.jsonl"
    if (
        replacement_registry.is_symlink()
        or not replacement_registry.is_file()
        or base.sha256(replacement_registry)
        != registry_mutation.get("replacement_sha256")
        or not replacement_registry.read_bytes().startswith(original_registry)
    ):
        fail("repair-03 committed registry replacement changed")
    registry_append = replacement_registry.read_bytes()[len(original_registry):]
    expected_progress = _registry_progress_payload(
        run_dir.name,
        original_registry,
        registry_append,
        "APPEND_COMPLETE",
        len(registry_append),
    )
    progress_path = repair_dir / REGISTRY_APPEND_PROGRESS
    if (
        progress_path.is_symlink()
        or not progress_path.is_file()
        or progress_path.read_bytes() != expected_progress
    ):
        fail("repair-03 committed registry append progress changed")

    for path_field, sha_field in (
        ("failed_state_path", "failed_state_sha256"),
        ("failed_resource_receipt_path", "failed_resource_receipt_sha256"),
        ("failed_scratch_receipt_path", "failed_scratch_receipt_sha256"),
        ("failed_input_identity_path", "failed_input_identity_sha256"),
        ("inner_payload_audit_path", "inner_payload_audit_sha256"),
        ("inner_payload_audit_resource_path", "inner_payload_audit_resource_sha256"),
        ("inner_payload_audit_source_path", "inner_payload_audit_source_sha256"),
        ("authority_basis_path", "authority_basis_sha256"),
        ("parser_contract_path", "parser_contract_sha256"),
    ):
        verify_bound_file(run_dir, record.get(path_field), record.get(sha_field), path_field)
    _validate_committed_contract(run_dir, record)
    failed_attempt = record.get("failed_attempt")
    if not isinstance(failed_attempt, dict):
        fail("repair-03 failed-attempt binding is missing")
    preserved = base.checked_run_path(
        run_dir, failed_attempt.get("preserved_scratch_active_path"),
        "repair-03 preserved failed scratch",
    )
    if (
        preserved.is_symlink() or not preserved.is_file()
        or preserved.stat().st_size != failed_attempt.get("preserved_scratch_bytes")
        or base.sha256(preserved) != failed_attempt.get("preserved_scratch_sha256")
        or (run_dir / SCRATCH).exists()
        or Path(str(run_dir / SCRATCH) + ".wal").exists()
    ):
        fail("repair-03 preserved scratch/no-resume boundary changed")
    if record.get("core_result_artifacts") != base.core_result_inventory(run_dir):
        fail("Cycle-1 core artifacts changed after repair-03")
    _, cycle_binding = base.validate_cycle1_duckdb_binding(
        run_dir, run_dir / CYCLE1_BINDING
    )
    if (
        cycle_binding != parent.get("cycle1_duckdb_binding")
        or record.get("cycle1_duckdb_binding") != cycle_binding
    ):
        fail("active Cycle-1 DuckDB binding changed after repair-03")
    for result in (RFQ_SUMMARY, RFQ_REPORT, RFQ_CATALOG):
        if (run_dir / result).exists():
            fail("RFQ result exists at the repair-03 preregistration boundary")

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
        fail("repair-03 trial append boundary changed")
    repository = manifest.get("repository")
    if (
        not isinstance(repository, dict)
        or repository.get("registration_repair_id") != REPAIR_ID
        or base.repository_identity(repository)
        != record.get("current_repository_identity")
        or repository.get("identity_history")
        != record.get("repository_identity_chain")
        or len(repository.get("identity_history", [])) != 4
    ):
        fail("repair-03 repository identity chain mismatch")
    source_mode = record.get("source_verification_mode")
    if source_mode == GIT_SOURCE_MODE:
        if repo_root is not None or execution_commit is not None:
            fail("committed repair-03 used Git mode, not snapshot mode")
        if (
            (run_dir / SOURCE_ATTESTATION).exists()
            or any(
                field in record for field in (
                    "source_snapshot_attestation_active_path",
                    "source_snapshot_attestation_path",
                    "source_snapshot_attestation_sha256",
                    "source_snapshot_attestation",
                )
            )
        ):
            fail("committed Git-mode repair-03 contains snapshot attestation evidence")
        source_repo_root, _, head = base.verify_clean_source(source_dir)
    elif source_mode == SNAPSHOT_SOURCE_MODE:
        if repo_root is None or execution_commit is None:
            fail("idempotent snapshot validation requires repo-root and execution-commit")
        source_repo_root, _, head = validate_snapshot_source_layout(
            source_dir, repo_root, execution_commit
        )
        _, source_manifest_payload, source_sums_payload = base.build_source_receipts(
            source_dir, source_repo_root
        )
        attestation, attestation_sha = validate_source_snapshot_attestation(
            run_dir, head, base.sha256_bytes(source_manifest_payload),
            base.sha256_bytes(source_sums_payload),
        )
        if (
            record.get("source_snapshot_attestation_active_path")
            != SOURCE_ATTESTATION.as_posix()
            or record.get("source_snapshot_attestation_path")
            != (
                "DATA_INTEGRITY/repairs/repair-03/pre_repair/"
                + SOURCE_ATTESTATION.as_posix()
            )
            or record.get("source_snapshot_attestation_sha256") != attestation_sha
            or record.get("source_snapshot_attestation") != {
                "schema_version": SOURCE_ATTESTATION_SCHEMA,
                "execution_commit": head,
                "git_tree": attestation["git_tree"],
                "direct_parent_verified": True,
                "source_relative": SOURCE_RELATIVE,
                "source_tree_clean_at_attestation": True,
                "commit_changed_paths": EXPECTED_REPAIR03_CHANGED_PATHS,
            }
        ):
            fail("committed repair-03 source snapshot attestation binding changed")
        archived_attestation = verify_bound_file(
            run_dir, record.get("source_snapshot_attestation_path"),
            record.get("source_snapshot_attestation_sha256"),
            "repair-03 archived source snapshot attestation",
        )
        if archived_attestation.read_bytes() != (
            run_dir / SOURCE_ATTESTATION
        ).read_bytes():
            fail("active/archived source snapshot attestations differ")
    else:
        fail("repair-03 source verification mode is invalid")
    if head != record.get("current_execution_commit"):
        fail("clean source HEAD no longer matches repair-03")
    base.verify_source_sums_match_manifest(
        run_dir / "SOURCE_MANIFEST.json", run_dir / "SOURCE_SHA256SUMS.txt",
        "repair-03 current",
    )
    if (
        base.sha256(run_dir / "SOURCE_MANIFEST.json")
        != record.get("current_source_manifest_sha256")
        or base.sha256(run_dir / "SOURCE_SHA256SUMS.txt")
        != record.get("current_source_sha256s_sha256")
        or base.sha256(run_dir / "QUERY_SHA256SUMS.txt")
        != record.get("current_query_set_sha256")
    ):
        fail("repair-03 active source/query receipts changed")
    return {
        "status": "REGISTRATION_REPAIR03_ALREADY_APPLIED",
        "repair_id": REPAIR_ID,
        "run_id": run_dir.name,
        "execution_commit": head,
        "retained_objects": RETAINED_OBJECTS,
        "retained_bytes": RETAINED_BYTES,
        "retained_selection_fingerprint_sha256": SELECTION_FINGERPRINT,
        "rfq_result_state": NO_RFQ_RESULT,
    }


def repair03_registration(
    run_dir: Path,
    source_dir: Path,
    *,
    repo_root: Path | None = None,
    execution_commit: str | None = None,
) -> dict[str, Any]:
    """Serialize the complete repair-03 registration for one run."""
    if Path(run_dir).is_symlink():
        fail("run-dir must not be a symlink")
    resolved_run_dir = Path(run_dir).resolve()
    with _exclusive_run_lock(resolved_run_dir):
        return _repair03_registration_locked(
            resolved_run_dir,
            source_dir,
            repo_root=repo_root,
            execution_commit=execution_commit,
        )


def _repair03_registration_locked(
    run_dir: Path,
    source_dir: Path,
    *,
    repo_root: Path | None = None,
    execution_commit: str | None = None,
) -> dict[str, Any]:
    """Commit repair-03 while the run-scoped owner lock is held."""
    if Path(source_dir).is_symlink():
        fail("source-dir must not be a symlink")
    run_dir = run_dir.resolve()
    source_dir = source_dir.resolve()
    snapshot_requested = repo_root is not None or execution_commit is not None
    if snapshot_requested and (repo_root is None or execution_commit is None):
        fail("snapshot mode requires both repo-root and execution-commit")
    if repo_root is not None and Path(repo_root).is_symlink():
        fail("snapshot repo-root must not be a symlink")
    resolved_repo_root = repo_root.resolve() if repo_root is not None else None
    manifest_path = run_dir / "RUN_MANIFEST.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        fail("RUN_MANIFEST.json is missing or unsafe")
    manifest_raw = manifest_path.read_bytes() if manifest_path.is_file() else b""
    try:
        manifest = json.loads(manifest_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"RUN_MANIFEST.json is invalid JSON: {exc}")
    if not isinstance(manifest, dict):
        fail("RUN_MANIFEST.json must contain one JSON object")
    if manifest.get("run_id") != run_dir.name:
        fail("RUN_MANIFEST run_id/path mismatch")
    records = manifest.get("data_integrity_repairs")
    if not isinstance(records, list):
        fail("data_integrity_repairs must be an append-only array")
    if len(records) == 3:
        return validate_already_applied(
            run_dir, source_dir, manifest, records,
            resolved_repo_root, execution_commit,
        )
    if len(records) != 2:
        fail("repair-03 requires exactly the repair-01 -> repair-02 parent chain")

    repair_dir = run_dir / "DATA_INTEGRITY/repairs/repair-03"
    if repair_dir.exists():
        recover_incomplete_transaction(run_dir, repair_dir)
        if repair_dir.exists():
            fail("repair-03 incomplete transaction recovery did not finish")

    parent, old_repository, trial_before, _, core_before = validate_parent_repair(
        run_dir, manifest
    )
    failed = validate_failed_attempt(run_dir, parent)
    scratch_receipt = validate_failed_scratch(run_dir, failed["state"])
    preserved_scratch_path = run_dir / PRESERVED_SCRATCH
    preserved_scratch_stat = preserved_scratch_path.stat()
    preserved_scratch_identity = (
        preserved_scratch_stat.st_dev,
        preserved_scratch_stat.st_ino,
        preserved_scratch_stat.st_size,
        preserved_scratch_stat.st_mtime_ns,
        preserved_scratch_stat.st_ctime_ns,
    )
    audit, _, audit_source_sha = validate_inner_payload_audit(
        run_dir, failed["input_sha256"]
    )
    _, cycle_binding = base.validate_cycle1_duckdb_binding(
        run_dir, run_dir / CYCLE1_BINDING
    )
    if cycle_binding != parent.get("cycle1_duckdb_binding"):
        fail("active Cycle-1 DuckDB binding changed after repair-02")

    source_attestation: dict[str, Any] | None = None
    source_attestation_sha: str | None = None
    if snapshot_requested:
        assert resolved_repo_root is not None and execution_commit is not None
        source_repo_root, source_relative, current_commit = (
            validate_snapshot_source_layout(
                source_dir, resolved_repo_root, execution_commit
            )
        )
        query_names = validate_snapshot_parent_repository(run_dir, old_repository)
        source_mode = SNAPSHOT_SOURCE_MODE
    else:
        if (run_dir / SOURCE_ATTESTATION).exists():
            fail(
                "active source snapshot attestation requires explicit snapshot "
                "repo-root/execution-commit mode"
            )
        source_repo_root, source_relative, current_commit = (
            base.verify_clean_source(source_dir)
        )
        verified_repository, query_names = base.verify_prior_repository(
            run_dir, source_dir, source_repo_root, source_relative,
            current_commit, manifest,
        )
        if verified_repository != old_repository:
            fail("repair-03 prior repository validation changed the parent identity")
        source_mode = GIT_SOURCE_MODE
    if old_repository.get("execution_commit") != PARENT_EXECUTION_COMMIT:
        fail("repair-03 source parent is not the committed repair-02 identity")

    _, source_manifest_payload, source_sums_payload = base.build_source_receipts(
        source_dir, source_repo_root
    )
    source_manifest_sha = base.sha256_bytes(source_manifest_payload)
    source_sums_sha = base.sha256_bytes(source_sums_payload)
    if snapshot_requested:
        source_attestation, source_attestation_sha = (
            validate_source_snapshot_attestation(
                run_dir, current_commit, source_manifest_sha, source_sums_sha
            )
        )
    query_rows: list[tuple[str, str, bytes]] = []
    for name in query_names:
        path = source_dir / name
        if path.is_symlink() or not path.is_file():
            fail(f"registered query source is missing or unsafe: {name}")
        payload = path.read_bytes()
        query_rows.append((base.sha256_bytes(payload), f"queries/{name}", payload))
    if [relative for _, relative, _ in query_rows] != old_repository.get(
        "query_files"
    ):
        fail("repair-03 query set paths differ from the registered parent")
    query_sums_payload = "".join(
        f"{digest}  {relative}\n" for digest, relative, _ in query_rows
    ).encode("utf-8")
    query_sha = base.sha256_bytes(query_sums_payload)
    rfq_query_rows = [row for row in query_rows if row[1] == "queries/rfq_full_stage.py"]
    if len(rfq_query_rows) != 1:
        fail("repair-03 requires exactly one registered rfq_full_stage.py query")
    registered_rfq_query_sha = rfq_query_rows[0][0]

    current_identity = {
        "execution_commit": current_commit,
        "source_manifest_sha256": source_manifest_sha,
        "source_sha256s_sha256": source_sums_sha,
        "query_set_sha256": query_sha,
        "query_files": copy.deepcopy(old_repository["query_files"]),
    }
    previous_identity = base.repository_identity(old_repository)
    old_chain = old_repository.get("identity_history")
    if (
        not isinstance(old_chain, list)
        or len(old_chain) != 3
        or old_chain != parent.get("repository_identity_chain")
        or old_chain[-1] != previous_identity
    ):
        fail("repair-02 repository history is not the exact three-identity chain")
    identity_chain = copy.deepcopy(old_chain) + [current_identity]
    if len({canonical_hash(row) for row in identity_chain}) != 4:
        fail("repair-03 repository identity chain is not four distinct revisions")
    initial_identity = copy.deepcopy(identity_chain[0])
    coverage = _expected_coverage()
    cumulative = copy.deepcopy(parent["cumulative_quarantined_objects"])
    parent_record_sha = canonical_hash(parent)
    validated_archive_hashes: dict[Path, str] = {
        Path("RUN_MANIFEST.json"): base.sha256_bytes(manifest_raw),
        Path("TRIAL_REGISTRY.jsonl"): base.sha256_bytes(trial_before),
        Path("SOURCE_MANIFEST.json"): old_repository["source_manifest_sha256"],
        Path("SOURCE_SHA256SUMS.txt"): old_repository[
            "source_sha256s_sha256"
        ],
        Path("QUERY_SHA256SUMS.txt"): old_repository["query_set_sha256"],
        STATE: failed["state_sha256"],
        RESOURCE: failed["resource_sha256"],
        SCRATCH_RECEIPT: FAILED_SCRATCH_RECEIPT_SHA256,
        INPUT_IDENTITY: failed["input_sha256"],
        AUDIT: AUDIT_SHA256,
        AUDIT_RESOURCE: AUDIT_RESOURCE_SHA256,
        AUDIT_SOURCE: audit_source_sha,
        CYCLE1_BINDING: base.sha256(run_dir / CYCLE1_BINDING),
    }
    for digest, relative in base.parse_checksums(
        run_dir / "QUERY_SHA256SUMS.txt", "repair-02 query receipt"
    ):
        validated_archive_hashes[Path(relative)] = digest
    if source_mode == SNAPSHOT_SOURCE_MODE:
        assert source_attestation_sha is not None
        validated_archive_hashes[SOURCE_ATTESTATION] = source_attestation_sha

    applied_at = base.now_utc()
    repair_root = run_dir / "DATA_INTEGRITY/repairs"
    repair_root.mkdir(parents=True, exist_ok=True)
    staging: Path | None = Path(
        tempfile.mkdtemp(prefix=".repair-03-", dir=repair_root)
    )
    try:
        pre = staging / "pre_repair"
        archive_paths = (
            Path("RUN_MANIFEST.json"), Path("TRIAL_REGISTRY.jsonl"),
            Path("SOURCE_MANIFEST.json"), Path("SOURCE_SHA256SUMS.txt"),
            Path("QUERY_SHA256SUMS.txt"), STATE, RESOURCE, SCRATCH_RECEIPT,
            INPUT_IDENTITY, AUDIT, AUDIT_RESOURCE, AUDIT_SOURCE, CYCLE1_BINDING,
        )
        if source_mode == SNAPSHOT_SOURCE_MODE:
            archive_paths += (SOURCE_ATTESTATION,)
        for relative in archive_paths:
            base.copy_exact(run_dir / relative, pre / relative)
        for query_relative in old_repository["query_files"]:
            base.copy_exact(
                base.checked_run_path(run_dir, query_relative, "repair-02 query"),
                pre / Path(query_relative),
            )
        if set(validated_archive_hashes) != {
            *archive_paths,
            *(Path(relative) for relative in old_repository["query_files"]),
        }:
            fail("repair-03 validated archive path set is incomplete")
        for relative, expected_sha in validated_archive_hashes.items():
            archived = pre / relative
            if not archived.is_file() or base.sha256(archived) != expected_sha:
                fail(f"repair-03 pre-repair archive raced validation: {relative}")

        prefix = "DATA_INTEGRITY/repairs/repair-03"
        archived_audit_path = f"{prefix}/pre_repair/{AUDIT.as_posix()}"
        audit_sha = base.sha256(run_dir / AUDIT)
        parser_contract = make_parser_contract(
            run_dir.name, applied_at, registered_rfq_query_sha,
            archived_audit_path, audit_sha,
        )
        authority_basis = make_authority_basis(
            run_dir.name, applied_at, archived_audit_path, audit_sha
        )
        base.atomic_write(
            staging / PARSER_CONTRACT_FILE, base.json_payload(parser_contract)
        )
        base.atomic_write(
            staging / AUTHORITY_FILE, base.json_payload(authority_basis)
        )
        base.copy_exact(run_dir / AUDIT_SOURCE, staging / AUDIT_SOURCE_FILE)

        post = staging / "post_repair"
        base.atomic_write(post / "SOURCE_MANIFEST.json", source_manifest_payload)
        base.atomic_write(post / "SOURCE_SHA256SUMS.txt", source_sums_payload)
        base.atomic_write(post / "QUERY_SHA256SUMS.txt", query_sums_payload)
        for _, relative, payload in query_rows:
            base.atomic_write(post / Path(relative), payload)

        archive_rows = base.archive_inventory(pre)
        parser_contract_relative = f"{prefix}/{PARSER_CONTRACT_FILE}"
        authority_relative = f"{prefix}/{AUTHORITY_FILE}"
        canonical_audit_source_relative = f"{prefix}/{AUDIT_SOURCE_FILE}"
        evidence = {
            "failed_state_path": f"{prefix}/pre_repair/{STATE.as_posix()}",
            "failed_state_sha256": base.sha256(run_dir / STATE),
            "failed_resource_receipt_path": (
                f"{prefix}/pre_repair/{RESOURCE.as_posix()}"
            ),
            "failed_resource_receipt_sha256": base.sha256(run_dir / RESOURCE),
            "failed_scratch_receipt_path": (
                f"{prefix}/pre_repair/{SCRATCH_RECEIPT.as_posix()}"
            ),
            "failed_scratch_receipt_sha256": base.sha256(
                run_dir / SCRATCH_RECEIPT
            ),
            "failed_input_identity_path": (
                f"{prefix}/pre_repair/{INPUT_IDENTITY.as_posix()}"
            ),
            "failed_input_identity_sha256": base.sha256(run_dir / INPUT_IDENTITY),
            "inner_payload_audit_path": archived_audit_path,
            "inner_payload_audit_sha256": audit_sha,
            "inner_payload_audit_resource_path": (
                f"{prefix}/pre_repair/{AUDIT_RESOURCE.as_posix()}"
            ),
            "inner_payload_audit_resource_sha256": base.sha256(
                run_dir / AUDIT_RESOURCE
            ),
            "inner_payload_audit_source_path": canonical_audit_source_relative,
            "inner_payload_audit_source_sha256": audit_source_sha,
            "inner_payload_audit_source_archive_path": (
                f"{prefix}/pre_repair/{AUDIT_SOURCE.as_posix()}"
            ),
            "parser_contract_path": parser_contract_relative,
            "parser_contract_sha256": base.sha256(
                staging / PARSER_CONTRACT_FILE
            ),
            "authority_basis_path": authority_relative,
            "authority_basis_sha256": base.sha256(staging / AUTHORITY_FILE),
            "cycle1_binding_path": (
                f"{prefix}/pre_repair/{CYCLE1_BINDING.as_posix()}"
            ),
            "cycle1_binding_sha256": base.sha256(run_dir / CYCLE1_BINDING),
        }
        if source_mode == SNAPSHOT_SOURCE_MODE:
            assert source_attestation is not None
            assert source_attestation_sha is not None
            evidence.update({
                "source_snapshot_attestation_active_path": (
                    SOURCE_ATTESTATION.as_posix()
                ),
                "source_snapshot_attestation_path": (
                    f"{prefix}/pre_repair/{SOURCE_ATTESTATION.as_posix()}"
                ),
                "source_snapshot_attestation_sha256": source_attestation_sha,
            })
        trial_failure, trial_preregistration = make_trial_records(
            applied_at, old_repository, current_identity, evidence, coverage,
            cumulative,
        )
        trial_after = (
            trial_before + base.registry_line(trial_failure)
            + base.registry_line(trial_preregistration)
        )
        base.atomic_write(post / "TRIAL_REGISTRY.jsonl", trial_after)
        trial_append = trial_after[len(trial_before):]
        if not trial_append:
            fail("repair-03 trial append payload is empty")
        base.atomic_write(
            staging / REGISTRY_APPEND_PROGRESS,
            _registry_progress_payload(
                run_dir.name,
                trial_before,
                trial_append,
                "NOT_STARTED",
                0,
            ),
        )
        _fsync_directory(staging)

        mutation_relatives = [
            "SOURCE_MANIFEST.json", "SOURCE_SHA256SUMS.txt",
            "QUERY_SHA256SUMS.txt", *old_repository["query_files"],
            "TRIAL_REGISTRY.jsonl",
        ]
        if len(mutation_relatives) != len(set(mutation_relatives)):
            fail("repair-03 active mutation paths are duplicated")
        active_mutations: list[dict[str, Any]] = []
        for relative in mutation_relatives:
            original_path = pre / Path(relative)
            replacement_path = post / Path(relative)
            if not original_path.is_file() or not replacement_path.is_file():
                fail(f"repair-03 transaction payload is missing: {relative}")
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
            PARSER_CONTRACT_FILE, AUTHORITY_FILE, AUDIT_SOURCE_FILE,
            "REPAIR_REGISTRATION.json", TRANSACTION_JOURNAL,
            REGISTRY_APPEND_PROGRESS,
        })
        transaction_journal = {
            "schema_version": JOURNAL_SCHEMA,
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
            "failed_stage_status": FAILED_STAGE_STATUS,
            "rfq_result_state": NO_RFQ_RESULT,
            "retry_requirement": RETRY_REQUIREMENT,
            "finding": FINDING,
            "registration_change_class": CHANGE_CLASS,
            "source_verification_mode": source_mode,
            "quarantine_policy": (
                "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE_INHERITED_NO_CHANGE"
            ),
            "previous_repair_registration_path": parent["repair_receipt_path"],
            "previous_repair_registration_sha256": parent[
                "repair_receipt_sha256"
            ],
            "previous_repair_record_sha256": parent_record_sha,
            **evidence,
            "registered_rfq_query_sha256": registered_rfq_query_sha,
            "parser_contract": {
                "path": parser_contract_relative,
                "sha256": evidence["parser_contract_sha256"],
                "schema_version": PARSER_CONTRACT_SCHEMA,
                "registered_query_sha256": registered_rfq_query_sha,
                "audited_parser_contract_sha256": (
                    AUDITED_PARSER_CONTRACT_SHA256
                ),
                "audited_parser_contract": copy.deepcopy(
                    AUDITED_PARSER_CONTRACT
                ),
            },
            "authority_basis": {
                "path": authority_relative,
                "sha256": evidence["authority_basis_sha256"],
                "schema_version": AUTHORITY_SCHEMA,
                "authority_basis": authority_basis["authority_basis"],
                "permitted_change": authority_basis["permitted_change"],
            },
            "newly_quarantined_objects": [],
            "cumulative_quarantined_objects": cumulative,
            "coverage": coverage,
            "selection_identity_unchanged": True,
            "previous_selection_fingerprint_sha256": SELECTION_FINGERPRINT,
            "current_selection_fingerprint_sha256": SELECTION_FINGERPRINT,
            "failed_input_fingerprint": SELECTION_FINGERPRINT,
            "failed_attempt": {
                "attempt_id": "RFQ_FULL_STAGE_REPAIR02_ATTEMPT_03",
                "state_active_path": STATE.as_posix(),
                "state_path": evidence["failed_state_path"],
                "state_sha256": evidence["failed_state_sha256"],
                "state_status": FAILED_STAGE_STATUS,
                "resource_active_path": RESOURCE.as_posix(),
                "resource_path": evidence["failed_resource_receipt_path"],
                "resource_sha256": evidence["failed_resource_receipt_sha256"],
                "resource_label": "rfq_full_stage_repair02",
                "return_code": 1,
                "scratch_receipt_active_path": SCRATCH_RECEIPT.as_posix(),
                "scratch_receipt_path": evidence["failed_scratch_receipt_path"],
                "scratch_receipt_sha256": evidence[
                    "failed_scratch_receipt_sha256"
                ],
                "preserved_scratch_active_path": PRESERVED_SCRATCH.as_posix(),
                "preserved_scratch_sha256": scratch_receipt["sha256"],
                "preserved_scratch_bytes": scratch_receipt["bytes"],
                "input_identity_active_path": INPUT_IDENTITY.as_posix(),
                "input_identity_path": evidence["failed_input_identity_path"],
                "input_identity_sha256": evidence[
                    "failed_input_identity_sha256"
                ],
                "input_fingerprint": SELECTION_FINGERPRINT,
                "scratch_disposition": "PRESERVED_RENAMED_NO_RESUME",
                "retry_requirement": RETRY_REQUIREMENT,
            },
            "expected_success_resource": {
                "label": "rfq_full_stage_repair03",
                "path": "logs/resources/rfq_full_stage_repair03.json",
            },
            "inner_payload_audit": {
                "path": archived_audit_path,
                "sha256": audit_sha,
                "resource_path": evidence["inner_payload_audit_resource_path"],
                "resource_sha256": evidence[
                    "inner_payload_audit_resource_sha256"
                ],
                "source_path": canonical_audit_source_relative,
                "source_sha256": audit_source_sha,
                "objects_scanned": RETAINED_OBJECTS,
                "bytes_scanned": RETAINED_BYTES,
                "lines_scanned": RETAINED_LINES,
                "data_frame_rows": EXPECTED_DATA_FRAME_ROWS,
                "segment_receipt_rows": EXPECTED_SEGMENT_RECEIPT_ROWS,
                "expected_blank_control_marker_rows": EXPECTED_BLANK_CONTROLS,
                "expected_blank_control_marker_counts": copy.deepcopy(
                    EXPECTED_MARKER_COUNTS
                ),
                "outer_invalid_object_count": 0,
                "outer_invalid_line_count": 0,
                "identity_mismatch_count": 0,
                "unexpected_inner_payload_object_count": 0,
                "unexpected_inner_payload_row_count": 0,
                "audited_parser_contract_sha256": (
                    AUDITED_PARSER_CONTRACT_SHA256
                ),
                "analysis_result_opened": False,
            },
            "cycle1_duckdb_binding": copy.deepcopy(cycle_binding),
            "previous_execution_commit": old_repository["execution_commit"],
            "current_execution_commit": current_commit,
            "initial_repository_identity": initial_identity,
            "previous_repository_identity": previous_identity,
            "current_repository_identity": current_identity,
            "repository_identity_chain": identity_chain,
            "previous_source_manifest_sha256": old_repository[
                "source_manifest_sha256"
            ],
            "current_source_manifest_sha256": source_manifest_sha,
            "previous_source_sha256s_sha256": old_repository[
                "source_sha256s_sha256"
            ],
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
            "hypothesis_design_change": "NONE",
            "data_selection_change": "NONE",
            "quarantine_change": "NONE",
            "threshold_feature_test_or_hypothesis_status_changed": False,
            "archive_path": f"{prefix}/pre_repair",
            "archive_inventory": archive_rows,
            "transaction_journal_path": f"{prefix}/{TRANSACTION_JOURNAL}",
            "transaction_journal_sha256": transaction_journal_sha,
        }
        if source_mode == SNAPSHOT_SOURCE_MODE:
            assert source_attestation is not None
            record["source_snapshot_attestation"] = {
                "schema_version": SOURCE_ATTESTATION_SCHEMA,
                "execution_commit": current_commit,
                "git_tree": source_attestation["git_tree"],
                "direct_parent_verified": True,
                "source_relative": SOURCE_RELATIVE,
                "source_tree_clean_at_attestation": True,
                "commit_changed_paths": EXPECTED_REPAIR03_CHANGED_PATHS,
            }
        base.atomic_write(
            staging / "REPAIR_REGISTRATION.json", base.json_payload(record)
        )
        record["repair_receipt_path"] = f"{prefix}/REPAIR_REGISTRATION.json"
        record["repair_receipt_sha256"] = base.sha256(
            staging / "REPAIR_REGISTRATION.json"
        )

        new_repository = copy.deepcopy(old_repository)
        new_repository.update({
            "previous_execution_commit": old_repository["execution_commit"],
            "previous_source_manifest_sha256": old_repository[
                "source_manifest_sha256"
            ],
            "previous_source_sha256s_sha256": old_repository[
                "source_sha256s_sha256"
            ],
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
        updated["registration_state"] = REGISTRATION_STATE
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
        mutable_paths = {row["path"] for row in active_mutations}
        immutable_evidence_paths = [
            relative for relative in archive_paths
            if relative.as_posix() not in mutable_paths
            and relative != Path("RUN_MANIFEST.json")
        ]
        immutable_evidence_payloads = {
            relative: (pre / relative).read_bytes()
            for relative in immutable_evidence_paths
        }
        prepared_repair_files = _safe_relative_file_set(staging)
        if prepared_repair_files != set(expected_repair_files):
            fail("prepared repair-03 transaction file set mismatch")
        prepared_static_hashes = {
            relative: base.sha256(staging / relative)
            for relative in prepared_repair_files
            if relative != REGISTRY_APPEND_PROGRESS
        }

        def assert_active_cas(
            label: str, expected_manifest: bytes = manifest_raw
        ) -> None:
            if (
                manifest_path.is_symlink()
                or not manifest_path.is_file()
                or manifest_path.read_bytes() != expected_manifest
            ):
                fail(f"RUN_MANIFEST CAS failed at {label}")
            for row in active_mutations:
                relative = row["path"]
                active_path = run_dir / Path(relative)
                expected = (
                    replacement_payloads[relative]
                    if relative in written else original_payloads[relative]
                )
                if (
                    active_path.is_symlink() or not active_path.is_file()
                    or active_path.read_bytes() != expected
                ):
                    fail(f"active identity CAS failed at {label}: {relative}")
            if base.core_result_inventory(run_dir) != core_before:
                fail(f"Cycle-1 core CAS failed at {label}")
            active_scratch = run_dir / SCRATCH
            active_scratch_wal = Path(str(active_scratch) + ".wal")
            try:
                current_preserved_stat = preserved_scratch_path.stat()
            except OSError:
                fail(f"preserved attempt-03 scratch disappeared at {label}")
            if (
                preserved_scratch_path.is_symlink()
                or active_scratch.exists()
                or active_scratch_wal.exists()
                or (
                    current_preserved_stat.st_dev,
                    current_preserved_stat.st_ino,
                    current_preserved_stat.st_size,
                    current_preserved_stat.st_mtime_ns,
                    current_preserved_stat.st_ctime_ns,
                ) != preserved_scratch_identity
            ):
                fail(f"preserved/fresh scratch CAS failed at {label}")
            for relative in immutable_evidence_paths:
                active_evidence = run_dir / relative
                if (
                    not active_evidence.is_file()
                    or active_evidence.is_symlink()
                    or active_evidence.read_bytes()
                    != immutable_evidence_payloads[relative]
                ):
                    fail(f"immutable evidence CAS failed at {label}: {relative}")
            if source_dir.is_symlink() or any(
                path.is_symlink() for path in source_dir.rglob("*")
            ):
                fail(f"source snapshot symlink appeared at {label}")
            _, current_source_manifest, current_source_sums = (
                base.build_source_receipts(source_dir, source_repo_root)
            )
            if (
                current_source_manifest != source_manifest_payload
                or current_source_sums != source_sums_payload
                or any(
                    (source_dir / name).is_symlink()
                    or not (source_dir / name).is_file()
                    or (source_dir / name).read_bytes() != payload
                    for (_, _, payload), name in zip(query_rows, query_names)
                )
            ):
                fail(f"source snapshot CAS failed at {label}")
            for result in (RFQ_SUMMARY, RFQ_REPORT, RFQ_CATALOG):
                if (run_dir / result).exists():
                    fail(f"RFQ result appeared at {label}")
            if published:
                if (
                    repair_dir.is_symlink()
                    or _safe_relative_file_set(repair_dir) != prepared_repair_files
                ):
                    fail(f"published repair-03 file set changed at {label}")
                for relative, expected_sha in prepared_static_hashes.items():
                    path = repair_dir / relative
                    if base.sha256(path) != expected_sha:
                        fail(f"published repair-03 payload changed at {label}: {relative}")
                registry_written = "TRIAL_REGISTRY.jsonl" in written
                expected_progress = _registry_progress_payload(
                    run_dir.name,
                    trial_before,
                    trial_append,
                    "APPEND_COMPLETE" if registry_written else "NOT_STARTED",
                    len(trial_append) if registry_written else 0,
                )
                progress_path = repair_dir / REGISTRY_APPEND_PROGRESS
                if (
                    progress_path.is_symlink()
                    or not progress_path.is_file()
                    or progress_path.read_bytes() != expected_progress
                ):
                    fail(f"repair-03 registry progress CAS failed at {label}")

        def cas_write(relative: str) -> None:
            label = f"write:{relative}"
            assert_active_cas(f"before {label}")
            _transaction_fault_hook(f"{label}:after_cas_before_write")
            assert_active_cas(f"immediately before {label}")
            active_path = run_dir / Path(relative)
            try:
                _cas_replace_active_file(
                    active_path,
                    original_payloads[relative],
                    replacement_payloads[relative],
                    label,
                    inject_fault=True,
                )
            finally:
                if (
                    active_path.is_file()
                    and active_path.read_bytes() == replacement_payloads[relative]
                ):
                    written.add(relative)
            if active_path.read_bytes() != replacement_payloads[relative]:
                fail(f"repair-03 active write verification failed: {relative}")
            _transaction_fault_hook(label)
            assert_active_cas(f"after {label}")

        def write_registry_progress(state: str, reported_bytes: int) -> None:
            payload = _registry_progress_payload(
                run_dir.name,
                trial_before,
                trial_append,
                state,
                reported_bytes,
            )
            path = repair_dir / REGISTRY_APPEND_PROGRESS
            base.atomic_write(path, payload)
            _fsync_directory(path.parent)
            if path.is_symlink() or path.read_bytes() != payload:
                fail("repair-03 registry append progress write failed")

        def append_trial_registry() -> None:
            """Append exactly two records on the locked active inode."""
            relative = "TRIAL_REGISTRY.jsonl"
            label = f"write:{relative}"
            assert_active_cas(f"before {label}")
            original = original_payloads[relative]
            replacement = replacement_payloads[relative]
            if not replacement.startswith(original):
                fail("repair-03 registry replacement is not append-only")
            delta = replacement[len(original):]
            if not delta:
                fail("repair-03 registry append payload is empty")
            active_path = run_dir / relative
            descriptor = os.open(
                active_path,
                os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                stat_path = active_path.stat()
                stat_fd = os.fstat(descriptor)
                if (stat_path.st_dev, stat_path.st_ino) != (
                    stat_fd.st_dev, stat_fd.st_ino
                ):
                    fail("TRIAL_REGISTRY inode changed before append")
                locked = _read_locked_file(descriptor)
                if locked != original:
                    fail("TRIAL_REGISTRY CAS failed after append lock")
                _transaction_fault_hook("trial_append_after_cas_before_write")
                locked = _read_locked_file(descriptor)
                if locked != original:
                    fail("TRIAL_REGISTRY CAS failed immediately before append")
                write_registry_progress("WRITE_IN_PROGRESS", 0)
                _transaction_fault_hook("trial_append_after_progress_before_write")
                locked = _read_locked_file(descriptor)
                if locked != original:
                    write_registry_progress("ROLLED_BACK", 0)
                    fail("TRIAL_REGISTRY CAS failed at append syscall boundary")
                count = os.write(descriptor, delta)
                append_end = os.lseek(descriptor, 0, os.SEEK_CUR)
                if (
                    isinstance(count, bool)
                    or not isinstance(count, int)
                    or count < 0
                    or count > len(delta)
                ):
                    fail("TRIAL_REGISTRY append returned an invalid byte count")
                write_registry_progress("WRITE_RETURNED", count)
                if count != len(delta):
                    current = _read_locked_file(descriptor)
                    if count:
                        append_start = append_end - count
                        if (
                            append_start < len(original)
                            or current[append_start:append_end] != delta[:count]
                        ):
                            fail(
                                "TRIAL_REGISTRY short append ownership cannot be proven"
                            )
                        restored = current[:append_start] + current[append_end:]
                        _rewrite_locked_registry(descriptor, current, restored)
                    write_registry_progress("ROLLED_BACK", 0)
                    fail("TRIAL_REGISTRY append was partial")
                os.fsync(descriptor)
                after = _read_locked_file(descriptor)
                if after != replacement:
                    fail("TRIAL_REGISTRY changed concurrently during append")
                write_registry_progress("APPEND_COMPLETE", len(delta))
                written.add(relative)
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            _transaction_fault_hook(label)
            assert_active_cas(f"after {label}")

        assert_active_cas("before publish_repair_dir")
        try:
            os.replace(staging, repair_dir)
            _fsync_directory(repair_root)
        finally:
            if repair_dir.is_dir():
                published = True
                staging = None
        try:
            if not published:
                fail("repair-03 directory publication failed")
            _transaction_fault_hook("publish_repair_dir")
            assert_active_cas("after publish_repair_dir")
            for row in active_mutations:
                if row["path"] == "TRIAL_REGISTRY.jsonl":
                    append_trial_registry()
                else:
                    cas_write(row["path"])
            assert_active_cas("before write:RUN_MANIFEST.json")
            _transaction_fault_hook("final_manifest_after_cas_before_write")
            assert_active_cas("immediately before write:RUN_MANIFEST.json")
            try:
                _cas_replace_active_file(
                    manifest_path,
                    manifest_raw,
                    updated_manifest_payload,
                    "write:RUN_MANIFEST.json",
                    inject_fault=True,
                )
            finally:
                manifest_replaced = (
                    manifest_path.is_file()
                    and manifest_path.read_bytes() == updated_manifest_payload
                )
            if not manifest_replaced:
                fail("RUN_MANIFEST final write verification failed")
            _transaction_fault_hook("write:RUN_MANIFEST.json")
            assert_active_cas(
                "after write:RUN_MANIFEST.json",
                expected_manifest=updated_manifest_payload,
            )
        except BaseException as transaction_error:
            if manifest_replaced:
                if manifest_path.read_bytes() != updated_manifest_payload:
                    fail("concurrent RUN_MANIFEST mutation prevents safe rollback")
                _cas_replace_active_file(
                    manifest_path,
                    updated_manifest_payload,
                    manifest_raw,
                    "rollback:RUN_MANIFEST.json",
                )
            if published and repair_dir.exists():
                try:
                    recover_incomplete_transaction(run_dir, repair_dir)
                except BaseException as rollback_error:
                    raise Repair03RegistrationError(
                        "repair-03 transaction failed and safe rollback was blocked: "
                        f"{rollback_error}"
                    ) from transaction_error
            raise
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)

    return {
        "status": "REGISTRATION_REPAIR03_REFROZEN",
        "repair_id": REPAIR_ID,
        "run_id": run_dir.name,
        "previous_execution_commit": PARENT_EXECUTION_COMMIT,
        "execution_commit": current_commit,
        "source_verification_mode": source_mode,
        "retained_objects": RETAINED_OBJECTS,
        "retained_bytes": RETAINED_BYTES,
        "retained_selection_fingerprint_sha256": SELECTION_FINGERPRINT,
        "rfq_result_state": NO_RFQ_RESULT,
        "retry_requirement": RETRY_REQUIREMENT,
    }


repair = repair03_registration


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Append-only SPORTS-AUTORESEARCH-01 parser-contract repair-03 "
            "preregistration"
        )
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument(
        "--repo-root", type=Path,
        help=(
            "Gitless snapshot layout root; requires --execution-commit and an "
            "active DATA_INTEGRITY/REPAIR_03_SOURCE_ATTESTATION.json"
        ),
    )
    parser.add_argument(
        "--execution-commit",
        help="Attested 40-hex repair-03 commit for gitless snapshot mode",
    )
    args = parser.parse_args()
    result = repair03_registration(
        args.run_dir, args.source_dir,
        repo_root=args.repo_root,
        execution_commit=args.execution_commit,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
