#!/usr/bin/env python3
"""Append-only RFQ execution-resource preregistration for repair-04.

Repair-04 is deliberately narrow.  Attempt 04 consumed the already frozen
repair-03 parser/query contract and exact 282-object retained selection, then
failed inside DuckDB's global RFQ de-duplication window before opening a
research result.  This registrar binds that failure and authorizes one fresh,
non-resumed attempt on the same W09 with only the execution resource envelope
retuned.

``RUN_MANIFEST.json`` remains the commit marker.  The transaction uses the
symlink-safe run owner lock and file CAS primitives audited for repair-03,
while keeping a repair-04-specific journal and append-progress receipt so an
orphan can be rolled back without deleting a foreign registry suffix.
"""

from __future__ import annotations

# ``sys`` is built in and ``os`` is frozen in the supported CPython runtime.
# Remove this registrar's source directory before importing any shadowable
# standard-library module.  An ignored ``json.py[c]`` (for example) must not
# execute before the Git/source proof has even started.
import os
import sys


_SCRIPT_SOURCE_DIRECTORY = os.path.realpath(os.path.dirname(__file__))


def _is_script_source_search_path(entry: str) -> bool:
    candidate = entry if entry else os.getcwd()
    return os.path.realpath(candidate) == _SCRIPT_SOURCE_DIRECTORY


sys.path[:] = [
    entry for entry in sys.path if not _is_script_source_search_path(entry)
]

import argparse
import copy
import fcntl
import json
import re
import shutil
import stat
import subprocess
import tempfile
import types
from pathlib import Path, PurePosixPath
from typing import Any


_MISSING_MODULE = object()


def _load_sibling_source(
    module_name: str,
    dependencies: dict[str, types.ModuleType] | None = None,
) -> types.ModuleType:
    """Execute only the bytes of the named regular sibling ``.py`` file.

    ``spec_from_file_location`` may accept a timestamp/size-valid sibling
    ``__pycache__`` entry.  These registration helpers are part of the source
    provenance boundary, so no unregistered bytecode is eligible to execute.
    Pre-seeding already source-loaded dependencies also prevents the legacy
    repair-02/repair-03 fallback importers from consulting their own caches.
    """
    path = Path(__file__).with_name(f"{module_name}.py")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ModuleNotFoundError(module_name) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ModuleNotFoundError(module_name)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            payload = handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    module.__package__ = ""
    bindings: dict[str, types.ModuleType] = {module_name: module}
    bindings.update(dependencies or {})
    previous = {
        name: sys.modules.get(name, _MISSING_MODULE) for name in bindings
    }
    try:
        sys.modules.update(bindings)
        code = compile(payload, str(path), "exec", dont_inherit=True)
        exec(code, module.__dict__)
    finally:
        for name, prior in previous.items():
            if prior is _MISSING_MODULE:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prior  # type: ignore[assignment]
    return module


base = _load_sibling_source("repair_registration")
repair02 = _load_sibling_source(
    "repair02_registration", {"repair_registration": base}
)
repair03 = _load_sibling_source(
    "repair03_registration",
    {
        "repair_registration": base,
        "repair02_registration": repair02,
    },
)


Repair04RegistrationError = base.RepairRegistrationError

REPAIR_ID = "repair-04"
PARENT_REPAIR_ID = "repair-03"
PARENT_EXECUTION_COMMIT = "91b4df6089b3e443387eafc06a8de38364830fd9"
EXPECTED_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_PARSER_REPAIR03_REGISTERED"
EXPECTED_REGISTRATION_STATE = (
    "RE_FROZEN_AFTER_RFQ_PARSER_CORRECTION_REPAIR03_BEFORE_RFQ_RESULT"
)
POST_REPAIR_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_RESOURCE_REPAIR04_REGISTERED"
REGISTRATION_STATE = (
    "RE_FROZEN_AFTER_RFQ_RESOURCE_RETUNE_REPAIR04_BEFORE_RFQ_RESULT"
)
FAILED_STAGE_STATUS = "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
NEXT_REQUIRED_AUTHORITY = "EXPLICIT_NEW_PREREGISTRATION_OR_RFQ_SCOPE_TERMINATION"
REPAIR_SCHEMA = "sports-autoresearch-resource-contract-repair-v1"
RESOURCE_CONTRACT_SCHEMA = "rfq-execution-resource-contract-v1"
AUTHORITY_SCHEMA = "sports-autoresearch-existing-w09-resource-retune-authority-v1"
JOURNAL_SCHEMA = "repair04-registration-transaction-v1"
REGISTRY_APPEND_PROGRESS_SCHEMA = "repair04-registry-append-progress-v2"
FINDING = "DUCKDB_OUT_OF_MEMORY_DURING_RFQ_DEDUPLICATION_WINDOW"
CHANGE_CLASS = (
    "EXISTING_W09_EXECUTION_RESOURCE_CONTRACT_RETUNE_ONLY_NO_DATA_PARSER_QUERY_"
    "SELECTION_QUARANTINE_OR_HYPOTHESIS_CHANGE"
)
RETRY_REQUIREMENT = (
    "FRESH_SCRATCH_SAME_SELECTION_SAME_PARSER_QUERY_AND_HYPOTHESES_RETUNED_"
    "RESOURCES_NO_RESUME"
)
FAILURE_DISPOSITION = "RESOURCE_CAP_FAILURE_BEFORE_RESULT"
AUTHORITY_CLASS = "MISSION_AUTHORIZED_EXISTING_W09_RESOURCE_RETUNING"
NO_RFQ_RESULT = "NO_RFQ_RESULT_OPENED"
CORE_DISPOSITION = "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED"
QUARANTINE_POLICY = (
    "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE_INHERITED_NO_CHANGE"
)
MISSION_SHA256 = "9b4ca417dca394223ecdc6719cd1628bd5ad69d5a80003db304f73b1c485a69c"

# Frozen data identity inherited from repair-03.
SELECTION_FINGERPRINT = repair03.SELECTION_FINGERPRINT
RETAINED_FINGERPRINT = repair03.RETAINED_FINGERPRINT
QUARANTINED_FINGERPRINT = repair03.QUARANTINED_FINGERPRINT
FULL_FINGERPRINT = repair03.FULL_FINGERPRINT
TOTAL_OBJECTS = repair03.TOTAL_OBJECTS
TOTAL_LOGICAL_BINDINGS = repair03.TOTAL_LOGICAL_BINDINGS
TOTAL_BYTES = repair03.TOTAL_BYTES
RETAINED_OBJECTS = repair03.RETAINED_OBJECTS
RETAINED_LOGICAL_BINDINGS = repair03.RETAINED_LOGICAL_BINDINGS
RETAINED_BYTES = repair03.RETAINED_BYTES
QUARANTINED_OBJECTS = repair03.QUARANTINED_OBJECTS
QUARANTINED_LOGICAL_BINDINGS = repair03.QUARANTINED_LOGICAL_BINDINGS
QUARANTINED_BYTES = repair03.QUARANTINED_BYTES

# Exact attempt-04 evidence captured on W09.
FAILED_STATE_SHA256 = "3c826ee72fa69f9c02c4a38fa33d3c65006ba33374dc592c269388228d506e57"
FAILED_RESOURCE_SHA256 = "08690ed35a3ef21a78654bec58f5637d31b9d97704d00cfbdb672a29adee5193"
FAILED_INPUT_SHA256 = "c0ee7ed24d27c58eff4600d33bf7b6d283aaea38020b3a7aee0e63c9c06c2bbb"
FAILED_SCRATCH_RECEIPT_SHA256 = (
    "d850e185a4a46dcf711e8bad7e081be431cceae64ebd8bdc7a32fa5beecc5fb9"
)
FAILED_SCRATCH_SHA256 = (
    "f2174bfe998b992964ca8becf0c7191dc143587f5a9a326d4529e204bcd88d6a"
)
FAILED_SCRATCH_BYTES = 28_761_927_680
FAILED_SCRATCH_MTIME = "2026-07-15T17:00:33.148336873+00:00"
FAILED_SCRATCH_ORIGINAL_INODE = 9_700_311
FAILED_ERROR_TYPE = "OutOfMemoryException"
FAILED_ERROR = (
    "Out of Memory Error: failed to pin block of size 256.0 KiB "
    "(37.2 GiB/37.2 GiB used)\n\n"
    "Possible solutions:\n"
    "* Reducing the number of threads (SET threads=X)\n"
    "* Disabling insertion-order preservation (SET preserve_insertion_order=false)\n"
    "* Increasing the memory limit (SET memory_limit='...GB')\n\n"
    "See also https://duckdb.org/docs/stable/guides/performance/"
    "how_to_tune_workloads"
)
FAILED_PHASE = "CREATE_TABLE_RFQ_EVENTS_VALID_GLOBAL_ROW_NUMBER_DEDUP"

W09_INSTANCE_ID = "i-0e53d134dceffe166"
W09_REGION = "us-east-2"
W09_ROLE = "w09-research-runner"
W09_MEMTOTAL_BYTES = 66_194_702_336
MEMORY_LIMIT_PERCENT = 69.49
MEMORY_LIMIT_FRACTION = 46_000_000_000 / W09_MEMTOTAL_BYTES
CURRENT_FREE_AFTER_PRESERVE_BYTES = 124_324_511_744
PRIOR_DISK_FREE_BEFORE_BYTES = 153_086_521_344
PRIOR_MINIMUM_DISK_FREE_BYTES = 109_311_180_800
PRIOR_MAX_DISK_DELTA_BYTES = 43_775_340_544
PROJECTED_MINIMUM_DISK_FREE_BYTES = 80_549_171_200
PRIOR_PEAK_TEMP_BYTES = 15_023_231_048

OLD_RESOURCE = {
    "memory_limit": "40GB",
    "memory_limit_bytes_decimal": 40_000_000_000,
    "threads": 8,
    "max_temp_size": "120GB",
    "max_temp_size_bytes_decimal": 120_000_000_000,
    "min_free_gib": 120,
    "min_free_bytes": 120 * 1024**3,
    "clob_max_per_root": 50,
}
NEW_RESOURCE = {
    "memory_limit": "46GB",
    "memory_limit_bytes_decimal": 46_000_000_000,
    "threads": 4,
    "max_temp_size": "70GB",
    "max_temp_size_bytes_decimal": 70_000_000_000,
    "min_free_gib": 100,
    "min_free_bytes": 100 * 1024**3,
    "clob_max_per_root": 50,
}
PREVIOUS_RUNTIME_CONTRACT = {
    "memory_limit": "40GB",
    "max_temp_size": "120GB",
    "threads": 8,
    "min_free_gib": 120.0,
    "clob_max_per_root": 50,
    "resume": False,
    "keep_scratch": False,
}
RUNTIME_CONTRACT = {
    "memory_limit": "46GB",
    "max_temp_size": "70GB",
    "threads": 4,
    "min_free_gib": 100.0,
    "clob_max_per_root": 50,
    "resume": False,
    "keep_scratch": False,
}

TRIAL_IDS = ("RFQ_FULL_STAGE_ATTEMPT_04", "RFQ_RESOURCE_CONTRACT_REPAIR_04")

STATE = Path("REPORT/tables/RFQ_FULL_STAGE_STATE.json")
RESOURCE = Path("logs/resources/rfq_full_stage_repair03.json")
INPUT_IDENTITY = Path("DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json")
SCRATCH_RECEIPT = Path("DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_04.json")
CYCLE1_BINDING = Path("DATA_INTEGRITY/CYCLE1_DUCKDB_BINDING.json")
W09_ATTESTATION = Path("DATA_INTEGRITY/W09_ATTESTATION.json")
RFQ_SUMMARY = Path("REPORT/tables/RFQ_FULL_STAGE_SUMMARY.json")
RFQ_REPORT = Path("REPORT/RFQ_FULL_STAGE.md")
RFQ_CATALOG = Path("cache/rfq_full_catalog.duckdb")
SCRATCH = Path("cache/rfq_full_scratch.duckdb")
PRESERVED_SCRATCH = Path("cache/rfq_full_scratch.attempt04_failed.duckdb")
RETRY_EXECUTION_QUERY = Path("queries/rfq_full_stage.py")
RESOURCE_CONTRACT_FILE = "RFQ_RESOURCE_CONTRACT.json"
AUTHORITY_FILE = "AUTHORITY_BASIS.json"
TRANSACTION_JOURNAL = "TRANSACTION_JOURNAL.json"
REGISTRY_APPEND_PROGRESS = "REGISTRY_APPEND_PROGRESS.json"
SOURCE_ATTESTATION = Path("DATA_INTEGRITY/REPAIR_04_SOURCE_ATTESTATION.json")
SOURCE_ATTESTATION_SCHEMA = "sports-autoresearch-source-snapshot-attestation-v1"
SOURCE_RELATIVE = "sandbox/research/deep_autoresearch"
GIT_SOURCE_MODE = "LOCAL_GIT_CLEAN_COMMITTED_HEAD"
SNAPSHOT_SOURCE_MODE = "REMOTE_GITLESS_ATTESTED_COMMITTED_SOURCE_SNAPSHOT"
EXPECTED_REPAIR04_CHANGED_PATHS = [
    f"{SOURCE_RELATIVE}/finalize_mission.py",
    f"{SOURCE_RELATIVE}/repair04_registration.py",
    f"{SOURCE_RELATIVE}/rfq_full_stage.py",
    f"{SOURCE_RELATIVE}/test_finalize_mission.py",
    f"{SOURCE_RELATIVE}/test_repair04_registration.py",
    f"{SOURCE_RELATIVE}/test_rfq_full_stage.py",
]


def fail(message: str) -> None:
    raise Repair04RegistrationError(message)


def _transaction_fault_hook(boundary: str) -> None:
    """Unit-test hook; production execution intentionally does nothing."""
    del boundary


def canonical_hash(value: Any) -> str:
    return base.sha256_bytes(base.json_payload(value))


def load(path: Path, label: str) -> dict[str, Any]:
    return base.load_json(path, label)


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


def _safe_relative_file_set(root: Path) -> set[str]:
    if root.is_symlink() or not root.is_dir():
        fail("repair-04 transaction directory is missing or unsafe")
    output: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            fail("repair-04 transaction directory contains a symlink")
        if path.is_file():
            output.add(path.relative_to(root).as_posix())
    return output


def _expected_coverage() -> dict[str, Any]:
    return repair03._expected_coverage()


def _expected_failed_command(run_id: str) -> list[str]:
    root = f"/srv/w09-research/runs/{run_id}"
    return [
        "/opt/w09/venv/bin/python",
        f"{root}/source/rfq_full_stage.py",
        "--run-dir", root,
        "--cache-root", "/srv/w09-research/cache",
        "--memory-limit", "40GB",
        "--max-temp-size", "120GB",
        "--threads", "8",
        "--min-free-gib", "120",
        "--clob-max-per-root", "50",
    ]


def expected_retry_command(run_id: str) -> list[str]:
    root = f"/srv/w09-research/runs/{run_id}"
    return [
        "/opt/w09/venv/bin/python",
        f"{root}/{RETRY_EXECUTION_QUERY.as_posix()}",
        "--run-dir", root,
        "--cache-root", "/srv/w09-research/cache",
        "--memory-limit", "46GB",
        "--max-temp-size", "70GB",
        "--threads", "4",
        "--min-free-gib", "100",
        "--clob-max-per-root", "50",
    ]


def _registry_progress_payload(
    run_id: str,
    original: bytes,
    append: bytes,
    state: str,
    reported_bytes: int,
    *,
    rollback_state: str = "NONE",
    rollback_expected: bytes | None = None,
    rollback_replacement: bytes | None = None,
) -> bytes:
    if state not in {
        "NOT_STARTED", "WRITE_IN_PROGRESS", "WRITE_RETURNED",
        "APPEND_COMPLETE", "ROLLED_BACK",
    }:
        fail("invalid repair-04 registry append progress state")
    if (
        isinstance(reported_bytes, bool)
        or not isinstance(reported_bytes, int)
        or reported_bytes < 0
        or reported_bytes > len(append)
    ):
        fail("invalid repair-04 registry append progress byte count")
    if rollback_state not in {"NONE", "PREPARED", "COMPLETE"}:
        fail("invalid repair-04 registry rollback progress state")
    if rollback_state == "NONE":
        if rollback_expected is not None or rollback_replacement is not None:
            fail("repair-04 registry rollback identities require a rollback state")
        rollback_expected_bytes = None
        rollback_expected_sha256 = None
        rollback_replacement_bytes = None
        rollback_replacement_sha256 = None
        rollback_remove_offset = None
        rollback_remove_bytes = None
    else:
        if not isinstance(rollback_expected, bytes) or not isinstance(
            rollback_replacement, bytes
        ):
            fail("repair-04 registry rollback identities are missing")
        rollback_expected_bytes = len(rollback_expected)
        rollback_expected_sha256 = base.sha256_bytes(rollback_expected)
        rollback_replacement_bytes = len(rollback_replacement)
        rollback_replacement_sha256 = base.sha256_bytes(rollback_replacement)
        rollback_remove_bytes = len(rollback_expected) - len(rollback_replacement)
        if rollback_remove_bytes <= 0:
            fail("repair-04 registry rollback must remove owned bytes")
        rollback_remove_offset = 0
        while (
            rollback_remove_offset < len(rollback_replacement)
            and rollback_expected[rollback_remove_offset]
            == rollback_replacement[rollback_remove_offset]
        ):
            rollback_remove_offset += 1
        if (
            rollback_expected[:rollback_remove_offset]
            + rollback_expected[
                rollback_remove_offset + rollback_remove_bytes:
            ]
            != rollback_replacement
        ):
            fail("repair-04 registry rollback is not one contiguous removal")
    return base.json_payload({
        "schema_version": REGISTRY_APPEND_PROGRESS_SCHEMA,
        "repair_id": REPAIR_ID,
        "run_id": run_id,
        "state": state,
        "original_sha256": base.sha256_bytes(original),
        "append_sha256": base.sha256_bytes(append),
        "append_bytes": len(append),
        "reported_bytes": reported_bytes,
        "rollback_state": rollback_state,
        "rollback_expected_bytes": rollback_expected_bytes,
        "rollback_expected_sha256": rollback_expected_sha256,
        "rollback_replacement_bytes": rollback_replacement_bytes,
        "rollback_replacement_sha256": rollback_replacement_sha256,
        "rollback_remove_offset": rollback_remove_offset,
        "rollback_remove_bytes": rollback_remove_bytes,
    })


def _atomic_registry_replace_with_locked_descriptor(
    path: Path,
    descriptor: int,
    expected: bytes,
    replacement: bytes,
) -> None:
    """Replace one exact registry identity while its old inode is flocked.

    The caller must hold both the shared run-owner lock and an exclusive flock
    on ``descriptor``.  The stable run-owner lock is the cross-inode serialization
    boundary: an inode flock alone cannot serialize a writer that opened the old
    name just before a rename.  This helper also flocks the prepared new inode so
    a cooperating path opener cannot write it before post-rename verification.

    No live file is truncated.  A failure before ``os.replace`` leaves
    ``expected`` live, while a failure after it leaves the complete
    ``replacement`` live.
    """
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        fail("TRIAL_REGISTRY rollback parent is missing or unsafe")
    temporary_descriptor = -1
    temporary_path: Path | None = None
    temporary_identity: tuple[int, int] | None = None
    try:
        locked_stat = os.fstat(descriptor)
        path_stat = os.lstat(path)
        if (
            not stat.S_ISREG(locked_stat.st_mode)
            or locked_stat.st_nlink != 1
            or path.is_symlink()
            or (path_stat.st_dev, path_stat.st_ino)
            != (locked_stat.st_dev, locked_stat.st_ino)
            or repair03._read_locked_file(descriptor) != expected
        ):
            fail("TRIAL_REGISTRY changed before atomic rollback preparation")

        temporary_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.repair04-rollback-", dir=parent
        )
        temporary_path = Path(temporary_name)
        initial_temporary_stat = os.fstat(temporary_descriptor)
        temporary_identity = (
            initial_temporary_stat.st_dev,
            initial_temporary_stat.st_ino,
        )
        fcntl.flock(temporary_descriptor, fcntl.LOCK_EX)
        os.fchown(temporary_descriptor, locked_stat.st_uid, locked_stat.st_gid)
        os.fchmod(temporary_descriptor, stat.S_IMODE(locked_stat.st_mode))
        _transaction_fault_hook("registry_rollback:before_temp_write")
        repair03._write_all(temporary_descriptor, replacement)
        _transaction_fault_hook("registry_rollback:after_temp_write_before_fsync")
        os.fsync(temporary_descriptor)
        if repair03._read_locked_file(temporary_descriptor) != replacement:
            fail("TRIAL_REGISTRY rollback temporary-file verification failed")
        _transaction_fault_hook("registry_rollback:after_temp_fsync_before_cas")

        # Final old-name and prepared-new-name CAS under the shared run lock.
        path_stat = os.lstat(path)
        current_locked_stat = os.fstat(descriptor)
        temporary_stat = os.fstat(temporary_descriptor)
        temporary_path_stat = os.lstat(temporary_path)
        if (
            path.is_symlink()
            or not stat.S_ISREG(current_locked_stat.st_mode)
            or current_locked_stat.st_nlink != 1
            or (path_stat.st_dev, path_stat.st_ino)
            != (current_locked_stat.st_dev, current_locked_stat.st_ino)
            or (current_locked_stat.st_dev, current_locked_stat.st_ino)
            != (locked_stat.st_dev, locked_stat.st_ino)
            or repair03._read_locked_file(descriptor) != expected
            or not stat.S_ISREG(temporary_stat.st_mode)
            or temporary_stat.st_nlink != 1
            or (temporary_path_stat.st_dev, temporary_path_stat.st_ino)
            != temporary_identity
            or temporary_stat.st_uid != locked_stat.st_uid
            or temporary_stat.st_gid != locked_stat.st_gid
            or stat.S_IMODE(temporary_stat.st_mode)
            != stat.S_IMODE(locked_stat.st_mode)
            or repair03._read_locked_file(temporary_descriptor) != replacement
        ):
            fail("TRIAL_REGISTRY changed during locked atomic rollback CAS")
        os.replace(temporary_path, path)
        temporary_path = None
        _transaction_fault_hook("registry_rollback:after_replace_before_dir_fsync")
        repair03._fsync_directory(parent)
        _transaction_fault_hook("registry_rollback:after_dir_fsync")
        published_stat = os.lstat(path)
        if (
            path.is_symlink()
            or not stat.S_ISREG(published_stat.st_mode)
            or (published_stat.st_dev, published_stat.st_ino) != temporary_identity
            or published_stat.st_nlink != 1
            or repair03._read_locked_file(temporary_descriptor) != replacement
        ):
            fail("TRIAL_REGISTRY atomic rollback verification failed")
    finally:
        if temporary_descriptor >= 0:
            try:
                fcntl.flock(temporary_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(temporary_descriptor)
        if temporary_path is not None:
            try:
                value = os.lstat(temporary_path)
            except FileNotFoundError:
                pass
            else:
                if temporary_identity is None or (
                    value.st_dev, value.st_ino
                ) == temporary_identity:
                    temporary_path.unlink()


def _atomic_registry_replace_locked(
    path: Path, expected: bytes, replacement: bytes
) -> None:
    """Open/lock the registry under the caller's shared run-owner lock."""
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _atomic_registry_replace_with_locked_descriptor(
            path, descriptor, expected, replacement
        )
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _validate_registry_progress(progress: dict[str, Any], run_id: str) -> None:
    """Validate append state plus the crash-recoverable rollback marker."""
    base.require_exact_keys(
        progress,
        {
            "schema_version", "repair_id", "run_id", "state",
            "original_sha256", "append_sha256", "append_bytes",
            "reported_bytes", "rollback_state", "rollback_expected_bytes",
            "rollback_expected_sha256", "rollback_replacement_bytes",
            "rollback_replacement_sha256", "rollback_remove_offset",
            "rollback_remove_bytes",
        },
        "repair-04 registry append progress",
    )
    state = progress.get("state")
    reported = progress.get("reported_bytes")
    rollback_state = progress.get("rollback_state")
    if (
        progress.get("schema_version") != REGISTRY_APPEND_PROGRESS_SCHEMA
        or progress.get("repair_id") != REPAIR_ID
        or progress.get("run_id") != run_id
        or state not in {
            "NOT_STARTED", "WRITE_IN_PROGRESS", "WRITE_RETURNED",
            "APPEND_COMPLETE", "ROLLED_BACK",
        }
        or isinstance(reported, bool)
        or not isinstance(reported, int)
        or reported < 0
        or isinstance(progress.get("append_bytes"), bool)
        or not isinstance(progress.get("append_bytes"), int)
        or progress.get("append_bytes") <= 0
        or reported > progress.get("append_bytes")
        or rollback_state not in {"NONE", "PREPARED", "COMPLETE"}
    ):
        fail("repair-04 registry append progress is invalid")
    base.require_sha(
        progress.get("original_sha256"),
        "repair-04 registry progress original SHA",
    )
    base.require_sha(
        progress.get("append_sha256"),
        "repair-04 registry progress append SHA",
    )
    if (
        state in {"NOT_STARTED", "WRITE_IN_PROGRESS", "ROLLED_BACK"}
        and reported != 0
    ) or (
        state == "APPEND_COMPLETE" and reported != progress["append_bytes"]
    ):
        fail("repair-04 registry append state/byte count is inconsistent")
    rollback_fields = (
        "rollback_expected_bytes", "rollback_expected_sha256",
        "rollback_replacement_bytes", "rollback_replacement_sha256",
        "rollback_remove_offset", "rollback_remove_bytes",
    )
    if rollback_state == "NONE":
        if any(progress.get(name) is not None for name in rollback_fields):
            fail("repair-04 registry rollback marker is invalid")
        return
    for name in ("rollback_expected_bytes", "rollback_replacement_bytes"):
        value = progress.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            fail("repair-04 registry rollback byte identity is invalid")
    for name in ("rollback_expected_sha256", "rollback_replacement_sha256"):
        base.require_sha(progress.get(name), f"repair-04 registry {name}")
    remove_offset = progress.get("rollback_remove_offset")
    remove_bytes = progress.get("rollback_remove_bytes")
    if (
        isinstance(remove_offset, bool)
        or not isinstance(remove_offset, int)
        or remove_offset < 0
        or isinstance(remove_bytes, bool)
        or not isinstance(remove_bytes, int)
        or remove_bytes <= 0
        or remove_offset + remove_bytes > progress["rollback_expected_bytes"]
        or progress["rollback_expected_bytes"] - remove_bytes
        != progress["rollback_replacement_bytes"]
    ):
        fail("repair-04 registry rollback removal identity is invalid")


def _payload_matches_identity(payload: bytes, size: Any, digest: Any) -> bool:
    return (
        isinstance(size, int)
        and not isinstance(size, bool)
        and len(payload) == size
        and isinstance(digest, str)
        and base.sha256_bytes(payload) == digest
    )


def _payload_starts_with_identity(payload: bytes, size: Any, digest: Any) -> bool:
    return (
        isinstance(size, int)
        and not isinstance(size, bool)
        and 0 <= size <= len(payload)
        and isinstance(digest, str)
        and base.sha256_bytes(payload[:size]) == digest
    )


def _write_registry_progress_file(path: Path, progress: dict[str, Any]) -> None:
    payload = base.json_payload(progress)
    base.atomic_write(path, payload)
    repair03._fsync_directory(path.parent)
    if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
        fail("repair-04 registry append progress write failed")


def _rollback_progress(
    progress: dict[str, Any],
    rollback_state: str,
    *,
    expected: bytes | None = None,
    replacement: bytes | None = None,
    remove_offset: int | None = None,
) -> dict[str, Any]:
    output = copy.deepcopy(progress)
    if rollback_state == "PREPARED":
        if expected is None or replacement is None:
            fail("repair-04 registry rollback plan identities are missing")
        output.update({
            "rollback_state": "PREPARED",
            "rollback_expected_bytes": len(expected),
            "rollback_expected_sha256": base.sha256_bytes(expected),
            "rollback_replacement_bytes": len(replacement),
            "rollback_replacement_sha256": base.sha256_bytes(replacement),
        })
        remove_bytes = len(expected) - len(replacement)
        if remove_bytes <= 0:
            fail("repair-04 registry rollback must remove owned bytes")
        if remove_offset is None:
            remove_offset = 0
            while (
                remove_offset < len(replacement)
                and expected[remove_offset] == replacement[remove_offset]
            ):
                remove_offset += 1
        if (
            isinstance(remove_offset, bool)
            or not isinstance(remove_offset, int)
            or remove_offset < 0
        ):
            fail("repair-04 registry rollback removal offset is invalid")
        if expected[:remove_offset] + expected[remove_offset + remove_bytes:] != replacement:
            fail("repair-04 registry rollback is not one contiguous removal")
        output["rollback_remove_offset"] = remove_offset
        output["rollback_remove_bytes"] = remove_bytes
    elif rollback_state == "COMPLETE":
        if output.get("rollback_state") not in {"PREPARED", "COMPLETE"}:
            fail("repair-04 registry rollback completion lacks a plan")
        output["rollback_state"] = "COMPLETE"
    else:  # pragma: no cover - caller invariant
        fail("invalid repair-04 registry rollback transition")
    return output


def _derive_registry_rollback(
    original: bytes,
    own_append: bytes,
    current: bytes,
    state: str,
    reported: int,
) -> bytes:
    """Remove only a complete, uniquely identifiable repair-04 append.

    A common byte prefix is not ownership proof.  In particular, after a crash
    between the durable WRITE_IN_PROGRESS marker and the write syscall, a
    foreign JSONL record will commonly share ``b'{\"'`` (and often a longer
    canonical-key prefix) with our append.  Ambiguous partial bytes are retained
    verbatim and recovery fails closed.
    """
    if not current.startswith(original):
        fail("unrecognized concurrent TRIAL_REGISTRY mutation")
    if current == original:
        return original
    suffix = current[len(original):]
    owned_bytes = 0
    if state == "APPEND_COMPLETE":
        owned_bytes = len(own_append)
    elif state == "WRITE_RETURNED":
        if reported == 0:
            return current
        owned_bytes = reported
    elif state in {"NOT_STARTED", "ROLLED_BACK"}:
        return current
    elif state == "WRITE_IN_PROGRESS":
        fail(
            "TRIAL_REGISTRY rollback ownership is ambiguous; preserving live bytes"
        )
    else:  # pragma: no cover - progress validation owns this invariant
        fail("invalid TRIAL_REGISTRY rollback source state")
    if owned_bytes <= 0 or owned_bytes > len(own_append):
        fail("TRIAL_REGISTRY rollback ownership count is invalid")
    owned_prefix = own_append[:owned_bytes]
    if suffix.startswith(owned_prefix):
        if owned_bytes == len(own_append) and own_append in suffix[len(own_append):]:
            fail("unrecognized concurrent TRIAL_REGISTRY mutation")
        return original + suffix[owned_bytes:]
    if owned_prefix in suffix:
        fail("unrecognized concurrent TRIAL_REGISTRY mutation")
    fail(
        "TRIAL_REGISTRY rollback ownership is ambiguous; preserving live bytes"
    )


def _reproduce_prepared_registry_rollback(
    current: bytes, progress: dict[str, Any]
) -> bytes:
    """Reproduce only the exact contiguous removal bound before replacement."""
    offset = progress.get("rollback_remove_offset")
    count = progress.get("rollback_remove_bytes")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or offset < 0
        or count <= 0
        or offset + count > len(current)
    ):
        fail("prepared TRIAL_REGISTRY rollback removal is invalid")
    restored = current[:offset] + current[offset + count:]
    if not _payload_matches_identity(
        restored,
        progress.get("rollback_replacement_bytes"),
        progress.get("rollback_replacement_sha256"),
    ):
        fail("prepared TRIAL_REGISTRY rollback plan cannot be reproduced")
    return restored


def _recovery_tombstone(
    repair_dir: Path, manifest_sha256: str | None = None
) -> Path:
    if manifest_sha256 is None:
        run_dir = repair_dir.parents[2]
        manifest = run_dir / "RUN_MANIFEST.json"
        if manifest.is_symlink() or not manifest.is_file():
            fail("repair-04 rollback tombstone lacks a manifest identity")
        manifest_sha256 = base.sha256(manifest)
    manifest_sha256 = base.require_sha(
        manifest_sha256, "repair-04 rollback tombstone manifest SHA"
    )
    return repair_dir.with_name(
        f".{repair_dir.name}.rollback-complete-{manifest_sha256}"
    )


def _cleanup_recovery_tombstone(repair_dir: Path) -> None:
    """Retry deletion only after a durable whole-directory tombstone rename."""
    tombstone = _recovery_tombstone(repair_dir)
    candidates = list(
        repair_dir.parent.glob(f".{repair_dir.name}.rollback-complete-*")
    )
    if any(path != tombstone for path in candidates):
        fail("foreign repair-04 rollback tombstone identity exists")
    if not os.path.lexists(tombstone):
        return
    if tombstone.is_symlink() or not tombstone.is_dir():
        fail("repair-04 rollback tombstone is unsafe")
    shutil.rmtree(tombstone)
    repair03._fsync_directory(tombstone.parent)
    if os.path.lexists(tombstone):
        fail("repair-04 rollback tombstone cleanup failed")


def recover_incomplete_transaction(run_dir: Path, repair_dir: Path) -> list[str]:
    """Restore an orphan while retaining unrelated registry appends.

    Production callers hold ``repair03._exclusive_run_lock`` across this whole
    operation.  That stable lock, not the replaceable registry inode, is the
    serialization contract shared by repair-03 and repair-04 registrars.
    """
    if repair_dir.is_symlink() or not repair_dir.is_dir():
        fail("orphan repair-04 transaction directory is missing or unsafe")
    journal = load(repair_dir / TRANSACTION_JOURNAL, "repair-04 transaction journal")
    if (
        journal.get("schema_version") != JOURNAL_SCHEMA
        or journal.get("repair_id") != REPAIR_ID
        or journal.get("run_id") != run_dir.name
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
    ):
        fail("orphan repair-04 directory lacks a valid recovery journal")
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
        fail("orphan repair-04 transaction file set mismatch")
    progress = load(
        repair_dir / REGISTRY_APPEND_PROGRESS,
        "repair-04 registry append progress",
    )
    _validate_registry_progress(progress, run_dir.name)
    state = progress.get("state")
    reported = progress.get("reported_bytes")
    manifest = run_dir / "RUN_MANIFEST.json"
    original_manifest_sha = base.require_sha(
        journal.get("original_manifest_sha256"), "transaction original manifest SHA"
    )
    if not manifest.is_file() or base.sha256(manifest) != original_manifest_sha:
        fail("cannot recover repair-04: RUN_MANIFEST is not the parent identity")

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
        replacement = repair_dir / "post_repair" / relative
        try:
            archived.resolve().relative_to(repair_dir.resolve())
            replacement.resolve().relative_to(repair_dir.resolve())
        except ValueError:
            fail("repair-04 transaction payload escapes its repair directory")
        if (
            not active.is_file()
            or not archived.is_file()
            or not replacement.is_file()
            or base.sha256(archived) != row.get("original_sha256")
            or base.sha256(replacement) != row.get("replacement_sha256")
        ):
            fail("repair-04 transaction payload hash mismatch")
        parsed.append(
            (
                active,
                archived.read_bytes(),
                replacement.read_bytes(),
                row.get("append_only_registry") is True,
            )
        )

    concurrent: list[str] = []
    for active, original, replacement, append_only in parsed:
        current = active.read_bytes()
        if not append_only:
            if current == original:
                continue
            if current != replacement:
                fail(f"unrecognized concurrent active mutation: {active}")
            repair03._cas_replace_active_file(
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
            or progress.get("original_sha256") != base.sha256_bytes(original)
            or progress.get("append_sha256") != base.sha256_bytes(own_append)
            or progress.get("append_bytes") != len(own_append)
            or reported > len(own_append)
        ):
            fail("unrecognized concurrent TRIAL_REGISTRY mutation")

        progress_path = repair_dir / REGISTRY_APPEND_PROGRESS
        rollback_state = progress.get("rollback_state")
        if rollback_state in {"PREPARED", "COMPLETE"}:
            authorized_remove_bytes = 0
            if state == "APPEND_COMPLETE":
                authorized_remove_bytes = len(own_append)
            elif state == "WRITE_RETURNED" and reported > 0:
                authorized_remove_bytes = reported
            if (
                progress.get("rollback_remove_offset") != len(original)
                or progress.get("rollback_remove_bytes")
                != authorized_remove_bytes
                or authorized_remove_bytes <= 0
                or progress.get("rollback_replacement_bytes") < len(original)
            ):
                fail("TRIAL_REGISTRY rollback marker lacks ownership proof")
            expected_matches = _payload_matches_identity(
                current,
                progress.get("rollback_expected_bytes"),
                progress.get("rollback_expected_sha256"),
            )
            replacement_prefix_matches = _payload_starts_with_identity(
                current,
                progress.get("rollback_replacement_bytes"),
                progress.get("rollback_replacement_sha256"),
            )
            if expected_matches:
                if rollback_state == "COMPLETE":
                    fail("completed TRIAL_REGISTRY rollback reverted unexpectedly")
                restored = _reproduce_prepared_registry_rollback(current, progress)
                if restored != _derive_registry_rollback(
                    original, own_append, current, str(state), reported
                ):
                    fail("prepared TRIAL_REGISTRY rollback exceeds owned bytes")
                _atomic_registry_replace_locked(active, current, restored)
                current = active.read_bytes()
                replacement_prefix_matches = _payload_starts_with_identity(
                    current,
                    progress.get("rollback_replacement_bytes"),
                    progress.get("rollback_replacement_sha256"),
                )
            if not replacement_prefix_matches:
                fail("TRIAL_REGISTRY changed outside the prepared rollback plan")
            planned_replacement = current[
                :progress["rollback_replacement_bytes"]
            ]
            if (
                not planned_replacement.startswith(original)
                or planned_replacement[len(original):].startswith(own_append)
            ):
                fail("TRIAL_REGISTRY rollback replacement still contains own append")
            if rollback_state == "PREPARED":
                progress = _rollback_progress(progress, "COMPLETE")
                _write_registry_progress_file(progress_path, progress)
                state = progress["state"]
                reported = progress["reported_bytes"]
            if current != original:
                concurrent.append(active.relative_to(run_dir).as_posix())
            continue

        restored = _derive_registry_rollback(
            original, own_append, current, str(state), reported
        )
        if restored == current:
            if current != original:
                concurrent.append(active.relative_to(run_dir).as_posix())
            continue
        progress = _rollback_progress(
            progress,
            "PREPARED",
            expected=current,
            replacement=restored,
            remove_offset=len(original),
        )
        _write_registry_progress_file(progress_path, progress)
        _transaction_fault_hook("registry_recovery:after_rollback_plan")
        _atomic_registry_replace_locked(active, current, restored)
        progress = _rollback_progress(progress, "COMPLETE")
        _write_registry_progress_file(progress_path, progress)
        state = progress["state"]
        reported = progress["reported_bytes"]
        if restored != original:
            concurrent.append(active.relative_to(run_dir).as_posix())

    for active, original, _, append_only in parsed:
        current = active.read_bytes()
        if append_only and current.startswith(original):
            continue
        if current != original:
            fail(f"repair-04 rollback verification failed: {active}")
    tombstone = _recovery_tombstone(repair_dir, original_manifest_sha)
    if os.path.lexists(tombstone):
        fail("repair-04 rollback tombstone already exists")
    os.replace(repair_dir, tombstone)
    repair03._fsync_directory(tombstone.parent)
    _transaction_fault_hook("registry_recovery:after_tombstone_publish")
    _cleanup_recovery_tombstone(repair_dir)
    _transaction_fault_hook("registry_recovery:after_tombstone_cleanup")
    return concurrent


def make_resource_contract(
    run_id: str,
    created_at: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Return the canonical, executable repair-04 resource envelope."""
    return {
        "schema_version": RESOURCE_CONTRACT_SCHEMA,
        "run_id": run_id,
        "created_at_utc": created_at,
        "mission_sha256": MISSION_SHA256,
        "parent_repair_id": PARENT_REPAIR_ID,
        "finding": FINDING,
        "failure_disposition": FAILURE_DISPOSITION,
        "failure_phase": FAILED_PHASE,
        "change_class": CHANGE_CLASS,
        "failed_attempt": {
            "attempt_id": "RFQ_FULL_STAGE_REPAIR03_ATTEMPT_04",
            "state_path": evidence["failed_state_path"],
            "state_sha256": evidence["failed_state_sha256"],
            "resource_path": evidence["failed_resource_receipt_path"],
            "resource_sha256": evidence["failed_resource_receipt_sha256"],
            "input_identity_path": evidence["failed_input_identity_path"],
            "input_identity_sha256": evidence["failed_input_identity_sha256"],
            "scratch_receipt_path": evidence["failed_scratch_receipt_path"],
            "scratch_receipt_sha256": evidence["failed_scratch_receipt_sha256"],
            "error_type": FAILED_ERROR_TYPE,
            "error": FAILED_ERROR,
            "return_code": 1,
        },
        "w09": {
            "instance_id": W09_INSTANCE_ID,
            "region": W09_REGION,
            "role": W09_ROLE,
            "memtotal_bytes": W09_MEMTOTAL_BYTES,
            "same_existing_instance_required": True,
            "resize_allowed": False,
            "replacement_instance_allowed": False,
        },
        "previous_runtime": copy.deepcopy(PREVIOUS_RUNTIME_CONTRACT),
        "current_runtime": copy.deepcopy(RUNTIME_CONTRACT),
        "memory_safety": {
            "memory_limit_bytes_decimal": NEW_RESOURCE[
                "memory_limit_bytes_decimal"
            ],
            "memtotal_bytes": W09_MEMTOTAL_BYTES,
            "memory_limit_fraction_of_memtotal": MEMORY_LIMIT_FRACTION,
            "memory_limit_percent_of_memtotal_rounded_2dp": MEMORY_LIMIT_PERCENT,
            "unallocated_memtotal_bytes": (
                W09_MEMTOTAL_BYTES - NEW_RESOURCE["memory_limit_bytes_decimal"]
            ),
            "threads_reduced_from": 8,
            "threads_reduced_to": 4,
        },
        "disk_safety": {
            "current_free_after_preserving_all_failed_scratch_bytes": (
                CURRENT_FREE_AFTER_PRESERVE_BYTES
            ),
            "previous_attempt_disk_free_before_bytes": (
                PRIOR_DISK_FREE_BEFORE_BYTES
            ),
            "previous_attempt_minimum_disk_free_bytes": (
                PRIOR_MINIMUM_DISK_FREE_BYTES
            ),
            "previous_attempt_maximum_disk_delta_bytes": (
                PRIOR_MAX_DISK_DELTA_BYTES
            ),
            "projected_minimum_disk_free_bytes_using_previous_delta": (
                PROJECTED_MINIMUM_DISK_FREE_BYTES
            ),
            "previous_attempt_peak_temp_bytes": PRIOR_PEAK_TEMP_BYTES,
            "new_max_temp_size_bytes_decimal": NEW_RESOURCE[
                "max_temp_size_bytes_decimal"
            ],
            "new_min_free_bytes": NEW_RESOURCE["min_free_bytes"],
            "failed_scratch_deletion_allowed": False,
        },
        "expected_command": expected_retry_command(run_id),
        "fresh_scratch_required": True,
        "resume_allowed": False,
        "failed_scratch_preservation_required": True,
        "expected_success_resource": {
            "label": "rfq_full_stage_repair04",
            "path": "logs/resources/rfq_full_stage_repair04.json",
        },
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
    }


def make_authority_basis(
    run_id: str,
    recorded_at: str,
    resource_contract_path: str,
    resource_contract_sha256: str,
) -> dict[str, Any]:
    """Record why the in-envelope resource retune needs no new spend grant."""
    return {
        "schema_version": AUTHORITY_SCHEMA,
        "run_id": run_id,
        "recorded_at_utc": recorded_at,
        "mission_sha256": MISSION_SHA256,
        "authority_class": AUTHORITY_CLASS,
        "permitted_change": "EXISTING_W09_EXECUTION_RESOURCE_RETUNE_ONLY",
        "resource_contract_path": resource_contract_path,
        "resource_contract_sha256": resource_contract_sha256,
        "existing_w09_instance_id": W09_INSTANCE_ID,
        "new_instance_spend_authorized": False,
        "instance_resize_authorized": False,
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
    }


def validate_snapshot_source_layout(
    source_dir: Path,
    repo_root: Path,
    execution_commit: str,
) -> tuple[Path, str, str]:
    if source_dir.is_symlink() or repo_root.is_symlink():
        fail("snapshot source/repository root must not be symlinks")
    resolved_root = repo_root.resolve()
    resolved_source = source_dir.resolve()
    if resolved_source != resolved_root / SOURCE_RELATIVE or not resolved_source.is_dir():
        fail(
            "snapshot source-dir must be exactly "
            "<repo-root>/sandbox/research/deep_autoresearch"
        )
    if not isinstance(execution_commit, str) or re.fullmatch(
        r"[0-9a-f]{40}", execution_commit
    ) is None:
        fail("snapshot execution commit must be a full lowercase Git SHA")
    if execution_commit == PARENT_EXECUTION_COMMIT:
        fail("snapshot execution commit did not advance past repair-03")
    files = base.source_files(resolved_source)
    if not files:
        fail("snapshot source tree contains no registered .py/.sh files")
    if any(path.is_symlink() for path in resolved_source.rglob("*")):
        fail("snapshot source tree contains a symlink")
    return resolved_root, SOURCE_RELATIVE, execution_commit


def validate_snapshot_parent_repository(
    run_dir: Path, old_repository: dict[str, Any]
) -> list[str]:
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
        run_dir / "SOURCE_MANIFEST.json",
        run_dir / "SOURCE_SHA256SUMS.txt",
        "snapshot repair-03 parent",
    )
    query_files = old_repository.get("query_files")
    if (
        not isinstance(query_files, list)
        or not query_files
        or len(query_files) != len(set(query_files))
    ):
        fail("snapshot mode parent query-file binding is invalid")
    checksums = base.parse_checksums(
        run_dir / "QUERY_SHA256SUMS.txt", "snapshot parent QUERY_SHA256SUMS"
    )
    if [relative for _, relative in checksums] != query_files:
        fail("snapshot mode parent query receipt paths changed")
    names: list[str] = []
    for digest, relative in checksums:
        pure = PurePosixPath(relative)
        if pure.parent != PurePosixPath("queries") or pure.suffix != ".py":
            fail(f"snapshot mode parent query path is unsafe: {relative}")
        frozen = base.checked_run_path(run_dir, relative, "snapshot parent query")
        if frozen.is_symlink() or not frozen.is_file() or base.sha256(frozen) != digest:
            fail(f"snapshot mode parent query changed: {relative}")
        names.append(pure.name)
    return names


def validate_source_snapshot_attestation(
    run_dir: Path,
    execution_commit: str,
    source_manifest_sha256: str,
    source_sha256s_sha256: str,
) -> tuple[dict[str, Any], str]:
    path = run_dir / SOURCE_ATTESTATION
    if path.is_symlink() or not path.is_file():
        fail("repair-04 source snapshot attestation is missing or unsafe")
    payload = path.read_bytes()
    try:
        attestation = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"repair-04 source snapshot attestation is invalid JSON: {exc}")
    if not isinstance(attestation, dict):
        fail("repair-04 source snapshot attestation must be an object")
    base.require_exact_keys(
        attestation,
        {
            "schema_version", "run_id", "mission_sha256",
            "parent_execution_commit", "execution_commit",
            "direct_parent_verified", "git_tree", "source_relative",
            "source_tree_clean_at_attestation", "source_manifest_sha256",
            "source_sha256s_sha256", "commit_changed_paths", "attested_at_utc",
        },
        "repair-04 source snapshot attestation",
    )
    if (
        attestation.get("schema_version") != SOURCE_ATTESTATION_SCHEMA
        or attestation.get("run_id") != run_dir.name
        or attestation.get("mission_sha256") != MISSION_SHA256
        or attestation.get("parent_execution_commit") != PARENT_EXECUTION_COMMIT
        or attestation.get("execution_commit") != execution_commit
        or attestation.get("direct_parent_verified") is not True
        or re.fullmatch(r"[0-9a-f]{40}", str(attestation.get("git_tree"))) is None
        or attestation.get("source_relative") != SOURCE_RELATIVE
        or attestation.get("source_tree_clean_at_attestation") is not True
        or attestation.get("source_manifest_sha256") != source_manifest_sha256
        or attestation.get("source_sha256s_sha256") != source_sha256s_sha256
        or attestation.get("commit_changed_paths")
        != EXPECTED_REPAIR04_CHANGED_PATHS
    ):
        fail("repair-04 source snapshot attestation mismatch")
    base.require_utc_timestamp(
        attestation.get("attested_at_utc"), "source snapshot attestation time"
    )
    return attestation, base.sha256_bytes(payload)


def _provenance_git_env() -> dict[str, str]:
    """Return a deterministic Git environment without caller-side injection."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("GIT_")
    }
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    })
    return environment


def _provenance_git_run(
    repo_root: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        [
            "git",
            "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false",
            *arguments,
        ],
        cwd=repo_root,
        env=_provenance_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        fail(
            f"strict git {' '.join(arguments)} failed "
            f"({result.returncode}): {stderr}"
        )
    return result


def _provenance_git(repo_root: Path, *arguments: str) -> str:
    output = _provenance_git_run(repo_root, *arguments).stdout
    try:
        return output.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        fail(f"strict Git provenance output is not UTF-8: {exc}")


def _provenance_git_blob(repo_root: Path, commit: str, path: str) -> bytes:
    result = _provenance_git_run(
        repo_root, "show", f"{commit}:{path}", check=False
    )
    if result.returncode != 0:
        fail(f"execution commit lacks registered source {path}")
    return result.stdout


def _reject_unsafe_git_provenance_mechanisms(repo_root: Path) -> None:
    """Reject repository-local mechanisms that can rewrite commit identity."""
    replace_refs = _provenance_git(
        repo_root, "for-each-ref", "--format=%(refname)", "refs/replace"
    )
    if replace_refs:
        fail("repair-04 Git provenance rejects refs/replace")

    graft_text = _provenance_git(
        repo_root, "rev-parse", "--git-path", "info/grafts"
    )
    graft_path = Path(graft_text)
    if not graft_path.is_absolute():
        graft_path = repo_root / graft_path
    if os.path.lexists(graft_path):
        metadata = graft_path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != 0
        ):
            fail("repair-04 Git provenance rejects non-empty or unsafe info/grafts")

    shallow = _provenance_git(
        repo_root, "rev-parse", "--is-shallow-repository"
    )
    if shallow != "false":
        fail("repair-04 Git provenance rejects shallow repositories")


def _verify_clean_source_provenance(
    source_dir: Path,
) -> tuple[Path, str, str]:
    """Verify clean HEAD using only the strict provenance Git environment."""
    if source_dir.is_symlink() or not source_dir.is_dir():
        fail(f"source directory is missing or unsafe: {source_dir}")
    repo_text = _provenance_git_run(
        source_dir, "rev-parse", "--show-toplevel", check=False
    )
    if repo_text.returncode != 0:
        fail("source directory is not inside a git repository")
    try:
        repo_root = Path(repo_text.stdout.decode("utf-8").strip()).resolve()
    except UnicodeDecodeError as exc:
        fail(f"Git repository root is not UTF-8: {exc}")
    try:
        source_relative = source_dir.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        fail("source directory escapes its git repository")

    _reject_unsafe_git_provenance_mechanisms(repo_root)
    status = _provenance_git(
        repo_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        source_relative,
    )
    if status:
        fail("research source tree must be committed and clean:\n" + status)
    head = _provenance_git(repo_root, "rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        fail("current execution commit is not a full git commit SHA")
    return repo_root, source_relative, head


def _verify_prior_repository_provenance(
    run_dir: Path,
    source_dir: Path,
    repo_root: Path,
    source_relative: str,
    current_commit: str,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Revalidate the frozen parent exclusively through strict Git helpers."""
    del source_dir  # The exact current source binding is checked separately.
    _reject_unsafe_git_provenance_mechanisms(repo_root)
    repository = manifest.get("repository")
    if not isinstance(repository, dict):
        fail("RUN_MANIFEST repository binding is missing")
    old_commit = repository.get("execution_commit")
    if (
        not isinstance(old_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", old_commit) is None
    ):
        fail("previous execution_commit is not a full commit SHA")
    if repository.get("source_tree_dirty_at_freeze") is not False:
        fail("previous registration was not frozen from a clean source tree")
    if old_commit == current_commit:
        fail("repair requires a new committed execution revision")
    if _provenance_git(repo_root, "cat-file", "-t", old_commit) != "commit":
        fail("previous execution_commit is not a commit in this repository")
    ancestor = _provenance_git_run(
        repo_root,
        "merge-base",
        "--is-ancestor",
        old_commit,
        current_commit,
        check=False,
    )
    if ancestor.returncode != 0:
        fail("new execution commit must descend from the previous execution commit")

    source_manifest_path = run_dir / "SOURCE_MANIFEST.json"
    source_sums_path = run_dir / "SOURCE_SHA256SUMS.txt"
    if repository.get("source_manifest_path") != "SOURCE_MANIFEST.json":
        fail("unexpected source_manifest_path")
    if base.sha256(source_manifest_path) != base.require_sha(
        repository.get("source_manifest_sha256"),
        "repository.source_manifest_sha256",
    ):
        fail("SOURCE_MANIFEST.json does not match RUN_MANIFEST")
    try:
        source_manifest = json.loads(
            source_manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"previous SOURCE_MANIFEST is invalid: {exc}")
    if not isinstance(source_manifest, list) or not source_manifest:
        fail("previous SOURCE_MANIFEST must be a nonempty array")
    expected_source_paths: list[str] = []
    manifest_by_name: dict[str, tuple[str, int]] = {}
    source_prefix = PurePosixPath(source_relative)
    for index, row in enumerate(source_manifest):
        if not isinstance(row, dict):
            fail(f"SOURCE_MANIFEST row {index} is not an object")
        path_text = row.get("path")
        digest = base.require_sha(
            row.get("sha256"), f"SOURCE_MANIFEST[{index}].sha256"
        )
        size = row.get("bytes")
        if (
            not isinstance(path_text, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            fail(f"SOURCE_MANIFEST row {index} has invalid path/bytes")
        pure = PurePosixPath(path_text)
        if (
            pure.is_absolute()
            or pure.parent != source_prefix
            or pure.suffix not in (".py", ".sh")
        ):
            fail(
                "SOURCE_MANIFEST path is outside the registered source root: "
                f"{path_text}"
            )
        blob = _provenance_git_blob(repo_root, old_commit, path_text)
        if len(blob) != size or base.sha256_bytes(blob) != digest:
            fail(
                "SOURCE_MANIFEST does not match previous execution commit: "
                f"{path_text}"
            )
        if pure.name in manifest_by_name:
            fail(f"duplicate source basename in SOURCE_MANIFEST: {pure.name}")
        manifest_by_name[pure.name] = (digest, size)
        expected_source_paths.append(path_text)
    old_tree = _provenance_git(
        repo_root,
        "ls-tree",
        "-r",
        "--name-only",
        old_commit,
        "--",
        source_relative,
    ).splitlines()
    old_tree = sorted(
        path
        for path in old_tree
        if PurePosixPath(path).parent == source_prefix
        and PurePosixPath(path).suffix in (".py", ".sh")
    )
    if sorted(expected_source_paths) != old_tree:
        fail("SOURCE_MANIFEST is not the complete previous committed source set")
    source_sums = base.parse_checksums(
        source_sums_path, "SOURCE_SHA256SUMS.txt"
    )
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
    if base.sha256(query_sums_path) != base.require_sha(
        repository.get("query_set_sha256"), "repository.query_set_sha256"
    ):
        fail("QUERY_SHA256SUMS.txt does not match RUN_MANIFEST")
    query_sums = base.parse_checksums(query_sums_path, "QUERY_SHA256SUMS.txt")
    if [relative for _, relative in query_sums] != query_files:
        fail("QUERY_SHA256SUMS paths do not exactly match repository.query_files")
    query_names: list[str] = []
    for digest, relative in query_sums:
        pure = PurePosixPath(relative)
        if pure.parent != PurePosixPath("queries") or pure.suffix != ".py":
            fail(f"unsafe or non-Python registered query path: {relative}")
        frozen = base.checked_run_path(run_dir, relative, "registered query")
        if not frozen.is_file() or base.sha256(frozen) != digest:
            fail(f"registered query copy was changed: {relative}")
        source_path = f"{source_relative}/{pure.name}"
        if source_path not in expected_source_paths:
            fail(f"registered query has no previous source binding: {relative}")
        if frozen.read_bytes() != _provenance_git_blob(
            repo_root, old_commit, source_path
        ):
            fail(
                "registered query does not match previous execution commit: "
                f"{relative}"
            )
        query_names.append(pure.name)

    for field, relative in (
        ("feature_definition_sha256", "FEATURE_DICTIONARY.json"),
        ("method_definition_sha256", "METHODS.md"),
    ):
        expected = repository.get(field)
        if expected is not None:
            path = run_dir / relative
            if (
                not path.is_file()
                or base.sha256(path)
                != base.require_sha(expected, f"repository.{field}")
            ):
                fail(f"{relative} no longer matches RUN_MANIFEST")
    return copy.deepcopy(repository), query_names


def _validate_git_commit_scope(repo_root: Path, current_commit: str) -> None:
    _reject_unsafe_git_provenance_mechanisms(repo_root)
    commit_and_parents = _provenance_git(
        repo_root, "rev-list", "--parents", "-n", "1", current_commit
    ).split()
    if commit_and_parents != [current_commit, PARENT_EXECUTION_COMMIT]:
        fail("repair-04 Git execution commit is not the direct repair-03 child")
    changed = _provenance_git(
        repo_root,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--name-only",
        "--diff-filter=ACDMRTUXB",
        f"{PARENT_EXECUTION_COMMIT}..{current_commit}",
    ).splitlines()
    if changed != EXPECTED_REPAIR04_CHANGED_PATHS:
        fail("repair-04 Git commit changed-path scope mismatch")


def _validate_clean_head_source_binding(
    source_dir: Path,
    repo_root: Path,
    source_relative: str,
    current_commit: str,
) -> tuple[bytes, bytes]:
    """Bind every registered source byte to a regular blob at clean ``HEAD``.

    ``git status`` is not sufficient for this proof: ignored files are omitted,
    while assume-unchanged and skip-worktree index flags can conceal modified
    tracked bytes.  Enumerate the commit tree and index explicitly, then compare
    the complete flat .py/.sh working-tree set byte-for-byte with the HEAD blobs.
    """
    if source_relative != SOURCE_RELATIVE:
        fail("repair-04 source-dir is not the registered Git source root")
    _reject_unsafe_git_provenance_mechanisms(repo_root)
    if _provenance_git(repo_root, "cat-file", "-t", current_commit) != "commit":
        fail("repair-04 clean source HEAD is not a commit")

    raw_tree = _provenance_git(
        repo_root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        current_commit,
        "--",
        source_relative,
    )
    source_prefix = PurePosixPath(source_relative)
    head_modes: dict[str, str] = {}
    for raw_entry in raw_tree.split("\0"):
        if not raw_entry:
            continue
        metadata, separator, path_text = raw_entry.partition("\t")
        fields = metadata.split()
        if separator != "\t" or len(fields) != 3:
            fail("repair-04 Git source tree entry is malformed")
        mode, object_type, object_id = fields
        if re.fullmatch(r"[0-9a-f]{40,64}", object_id) is None:
            fail("repair-04 Git source tree object id is malformed")
        pure = PurePosixPath(path_text)
        if pure == source_prefix or source_prefix not in pure.parents:
            fail("repair-04 Git source tree escaped the registered root")
        if mode in {"120000", "160000"} or object_type != "blob":
            fail("repair-04 Git source tree contains a symlink or submodule")
        if mode not in {"100644", "100755"}:
            fail("repair-04 Git source tree contains a non-regular source object")
        if pure.parent == source_prefix and pure.suffix in {".py", ".sh"}:
            head_modes[path_text] = mode

    if not head_modes:
        fail("repair-04 Git HEAD contains no registered .py/.sh source blobs")

    working_paths: dict[str, Path] = {}
    for path in source_dir.rglob("*"):
        if path.is_symlink():
            fail("repair-04 working source tree contains a symlink")
        if not path.is_file() or path.suffix not in {".py", ".sh"}:
            continue
        relative = path.relative_to(source_dir)
        if len(relative.parts) != 1:
            fail("repair-04 working source tree contains nested .py/.sh source")
        if not stat.S_ISREG(path.lstat().st_mode):
            fail("repair-04 working source tree contains a non-regular source file")
        working_paths[f"{source_relative}/{relative.as_posix()}"] = path

    if sorted(working_paths) != sorted(head_modes):
        fail("repair-04 working source file set does not exactly match Git HEAD")

    raw_index = _provenance_git(
        repo_root, "ls-files", "-v", "-z", "--", source_relative
    )
    indexed_paths: set[str] = set()
    for raw_entry in raw_index.split("\0"):
        if not raw_entry:
            continue
        tag, separator, path_text = raw_entry.partition(" ")
        if separator != " " or len(tag) != 1:
            fail("repair-04 Git source index entry is malformed")
        if path_text in head_modes:
            indexed_paths.add(path_text)
            if tag.islower() or tag.upper() == "S":
                fail(
                    "repair-04 Git source index uses assume-unchanged or "
                    "skip-worktree"
                )
    if indexed_paths != set(head_modes):
        fail("repair-04 Git source index does not exactly match HEAD")

    for path_text, path in working_paths.items():
        payload = path.read_bytes()
        if payload != _provenance_git_blob(
            repo_root, current_commit, path_text
        ):
            fail("repair-04 working source bytes do not exactly match Git HEAD")
        executable = bool(path.lstat().st_mode & 0o111)
        if executable != (head_modes[path_text] == "100755"):
            fail("repair-04 working source mode does not exactly match Git HEAD")

    rows, manifest_payload, sums_payload = base.build_source_receipts(
        source_dir, repo_root
    )
    if [row["path"] for row in rows] != sorted(head_modes):
        fail("repair-04 source receipt is not the exact Git HEAD source set")
    return manifest_payload, sums_payload


def validate_parent_repair(
    run_dir: Path, manifest: dict[str, Any]
) -> tuple[
    dict[str, Any], dict[str, Any], bytes, list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Validate the committed repair-03 boundary before any mutation."""
    if manifest.get("run_id") != run_dir.name or manifest.get("analysis_started") is not True:
        fail("RUN_MANIFEST is not the active post-core run")
    if (
        manifest.get("status") != EXPECTED_STATUS
        or manifest.get("registration_state") != EXPECTED_REGISTRATION_STATE
    ):
        fail("active manifest is not the registered repair-03 boundary")
    for result in (RFQ_SUMMARY, RFQ_REPORT, RFQ_CATALOG):
        if (run_dir / result).exists():
            fail("RFQ result exists; repair-04 preregistration is forbidden")

    repairs = manifest.get("data_integrity_repairs")
    if (
        not isinstance(repairs, list)
        or len(repairs) != 3
        or [row.get("repair_id") if isinstance(row, dict) else None for row in repairs]
        != ["repair-01", "repair-02", PARENT_REPAIR_ID]
    ):
        fail("repair-04 requires the immutable repair-01 -> repair-03 chain")
    parent = repairs[-1]
    parser_contract = parent.get("parser_contract")
    if (
        parent.get("schema_version") != repair03.REPAIR_SCHEMA
        or parent.get("parent_repair_id") != "repair-02"
        or parent.get("post_repair_status") != EXPECTED_STATUS
        or parent.get("current_execution_commit") != PARENT_EXECUTION_COMMIT
        or parent.get("rfq_result_state") != NO_RFQ_RESULT
        or parent.get("coverage") != _expected_coverage()
        or parent.get("newly_quarantined_objects") != []
        or parent.get("selection_identity_unchanged") is not True
        or parent.get("current_selection_fingerprint_sha256")
        != SELECTION_FINGERPRINT
        or parent.get("data_selection_change") != "NONE"
        or parent.get("quarantine_change") != "NONE"
        or parent.get("hypothesis_design_change") != "NONE"
        or not isinstance(parser_contract, dict)
        or parser_contract.get("schema_version") != repair03.PARSER_CONTRACT_SCHEMA
        or parser_contract.get("path") != parent.get("parser_contract_path")
        or parser_contract.get("sha256") != parent.get("parser_contract_sha256")
        or parser_contract.get("registered_query_sha256")
        != parent.get("registered_rfq_query_sha256")
    ):
        fail("repair-03 parent identity/selection/parser contract mismatch")
    cumulative = parent.get("cumulative_quarantined_objects")
    if (
        not isinstance(cumulative, list)
        or len(cumulative) != QUARANTINED_OBJECTS
        or len({row.get("key") for row in cumulative if isinstance(row, dict)})
        != QUARANTINED_OBJECTS
        or sum(row.get("size", 0) for row in cumulative if isinstance(row, dict))
        != QUARANTINED_BYTES
    ):
        fail("repair-03 cumulative quarantine is not the exact two-object set")

    receipt_relative = "DATA_INTEGRITY/repairs/repair-03/REPAIR_REGISTRATION.json"
    if parent.get("repair_receipt_path") != receipt_relative:
        fail("repair-03 registration receipt path mismatch")
    receipt_path = verify_bound_file(
        run_dir,
        receipt_relative,
        parent.get("repair_receipt_sha256"),
        "repair-03 registration receipt",
    )
    receipt = load(receipt_path, "repair-03 registration receipt")
    without_self = copy.deepcopy(parent)
    without_self.pop("repair_receipt_path", None)
    without_self.pop("repair_receipt_sha256", None)
    if receipt != without_self:
        fail("repair-03 registration receipt/manifest record mismatch")
    verify_bound_file(
        run_dir,
        parent.get("transaction_journal_path"),
        parent.get("transaction_journal_sha256"),
        "repair-03 transaction journal",
    )
    archive = base.checked_run_path(
        run_dir, str(parent.get("archive_path")), "repair-03 pre-repair archive"
    )
    if parent.get("archive_inventory") != base.archive_inventory(archive):
        fail("repair-03 archive inventory changed")
    verify_bound_file(
        run_dir,
        parent.get("parser_contract_path"),
        parent.get("parser_contract_sha256"),
        "repair-03 parser contract",
    )
    verify_bound_file(
        run_dir,
        parent.get("authority_basis_path"),
        parent.get("authority_basis_sha256"),
        "repair-03 authority basis",
    )
    verify_bound_file(
        run_dir,
        parent.get("inner_payload_audit_path"),
        parent.get("inner_payload_audit_sha256"),
        "repair-03 parser audit",
    )

    repository = manifest.get("repository")
    if (
        not isinstance(repository, dict)
        or repository.get("execution_commit") != PARENT_EXECUTION_COMMIT
        or repository.get("registration_repair_id") != PARENT_REPAIR_ID
        or base.repository_identity(repository)
        != parent.get("current_repository_identity")
        or repository.get("identity_history")
        != parent.get("repository_identity_chain")
        or not isinstance(repository.get("identity_history"), list)
        or len(repository["identity_history"]) != 4
    ):
        fail("active repository identity is not repair-03")
    base.verify_source_sums_match_manifest(
        run_dir / "SOURCE_MANIFEST.json",
        run_dir / "SOURCE_SHA256SUMS.txt",
        "repair-03 current",
    )
    if (
        base.sha256(run_dir / "SOURCE_MANIFEST.json")
        != repository.get("source_manifest_sha256")
        or base.sha256(run_dir / "SOURCE_SHA256SUMS.txt")
        != repository.get("source_sha256s_sha256")
        or base.sha256(run_dir / "QUERY_SHA256SUMS.txt")
        != repository.get("query_set_sha256")
    ):
        fail("active repair-03 source/query receipts changed")
    query_files = repository.get("query_files")
    checksums = base.parse_checksums(
        run_dir / "QUERY_SHA256SUMS.txt", "repair-03 query receipt"
    )
    if [relative for _, relative in checksums] != query_files:
        fail("active repair-03 query paths changed")
    for digest, relative in checksums:
        path = base.checked_run_path(run_dir, relative, "repair-03 query")
        if path.is_symlink() or not path.is_file() or base.sha256(path) != digest:
            fail(f"active repair-03 query changed: {relative}")

    registry_path = run_dir / "TRIAL_REGISTRY.jsonl"
    if registry_path.is_symlink() or not registry_path.is_file():
        fail("TRIAL_REGISTRY.jsonl is missing or unsafe")
    registry = registry_path.read_bytes()
    trial = parent.get("trial_registry")
    if (
        not registry
        or not registry.endswith(b"\n")
        or not isinstance(trial, dict)
        or trial.get("trial_registration_ids")
        != ["RFQ_FULL_STAGE_ATTEMPT_03", "RFQ_PARSER_CONTRACT_REPAIR_03"]
        or trial.get("appended_records") != 2
        or trial.get("strict_previous_bytes_prefix") is not True
        or trial.get("current_bytes") != len(registry)
        or trial.get("current_sha256") != base.sha256_bytes(registry)
    ):
        fail("TRIAL_REGISTRY is not the exact repair-03 append boundary")
    rows: list[dict[str, Any]] = []
    registration_ids: list[str] = []
    for line_number, raw_line in enumerate(registry.splitlines(), 1):
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            fail(f"TRIAL_REGISTRY line {line_number} is invalid JSON: {exc}")
        if not isinstance(row, dict):
            fail(f"TRIAL_REGISTRY line {line_number} is not an object")
        rows.append(row)
        if row.get("trial_registration_id") is not None:
            registration_ids.append(row["trial_registration_id"])
        if str(row.get("result_artifact", "")).endswith(
            "RFQ_FULL_STAGE_SUMMARY.json"
        ):
            fail("TRIAL_REGISTRY already contains an RFQ full-stage result")
    if registration_ids != [
        "RFQ_FULL_STAGE_ATTEMPT_01", "RFQ_OBJECT_QUARANTINE_REPAIR_01",
        "RFQ_FULL_STAGE_ATTEMPT_02", "RFQ_OBJECT_QUARANTINE_REPAIR_02",
        "RFQ_FULL_STAGE_ATTEMPT_03", "RFQ_PARSER_CONTRACT_REPAIR_03",
    ]:
        fail("TRIAL_REGISTRY repair history is not the exact six-record boundary")
    core = base.core_result_inventory(run_dir)
    if parent.get("core_result_artifacts") != core:
        fail("Cycle-1 core artifacts changed after repair-03")
    if parent.get("cycle1_duckdb_binding") != repairs[1].get(
        "cycle1_duckdb_binding"
    ):
        fail("repair-03 changed the Cycle-1 DuckDB binding")
    return parent, copy.deepcopy(repository), registry, rows, core


def validate_w09_attestation(
    run_dir: Path, manifest: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    path = run_dir / W09_ATTESTATION
    if path.is_symlink() or not path.is_file():
        fail("W09 attestation is missing or unsafe")
    digest = base.sha256(path)
    attestation = load(path, "W09 attestation")
    gate_b = manifest.get("gates", {}).get("gate_b", {})
    if (
        gate_b.get("attestation_sha256") != digest
        or attestation.get("instance_id") != W09_INSTANCE_ID
        or attestation.get("region") != W09_REGION
        or attestation.get("role") != W09_ROLE
        or attestation.get("instance_profile") != W09_ROLE
        or attestation.get("architecture") != "aarch64"
        or attestation.get("s3_access") != "READ_ONLY_RESEARCH_PREFIX"
        or attestation.get("static_credentials_present") is not False
        or attestation.get("trading_credentials_present") is not False
        or attestation.get("ambient_aws_or_kalshi_variables") != []
        or attestation.get("static_credential_paths_present") != []
    ):
        fail("W09 identity/isolation attestation mismatch")
    return attestation, digest


def validate_failed_attempt(
    run_dir: Path, parent: dict[str, Any]
) -> dict[str, Any]:
    state_path = run_dir / STATE
    resource_path = run_dir / RESOURCE
    input_path = run_dir / INPUT_IDENTITY
    if (
        base.sha256(state_path) != FAILED_STATE_SHA256
        or base.sha256(resource_path) != FAILED_RESOURCE_SHA256
        or base.sha256(input_path) != FAILED_INPUT_SHA256
    ):
        fail("attempt-04 active evidence hashes are not the approved failure boundary")
    state = load(state_path, "attempt-04 failed state")
    base.require_exact_keys(
        state,
        {
            "error", "error_type", "expected_success_resource", "failed_at_utc",
            "failed_attempt_bindings", "inner_payload_parser_contract",
            "input_fingerprint", "next_required_authority",
            "quarantine_gap_plan", "registered_rfq_query_sha256", "repair_chain",
            "resume", "run_id", "schema", "scratch", "started_at_utc", "status",
        },
        "attempt-04 failed state",
    )
    expected_scratch = f"/srv/w09-research/runs/{run_dir.name}/{SCRATCH.as_posix()}"
    parser_binding = {
        "path": parent["parser_contract_path"],
        "schema_version": repair03.PARSER_CONTRACT_SCHEMA,
        "sha256": parent["parser_contract_sha256"],
    }
    if (
        state.get("schema") != "rfq-full-stage-state-v1"
        or state.get("run_id") != run_dir.name
        or state.get("status") != FAILED_STAGE_STATUS
        or state.get("resume") is not False
        or state.get("input_fingerprint") != SELECTION_FINGERPRINT
        or state.get("next_required_authority") != NEXT_REQUIRED_AUTHORITY
        or state.get("scratch") != expected_scratch
        or state.get("error_type") != FAILED_ERROR_TYPE
        or state.get("error") != FAILED_ERROR
        or state.get("started_at_utc") != "2026-07-15T16:45:59Z"
        or state.get("failed_at_utc") != "2026-07-15T17:00:32Z"
        or state.get("expected_success_resource")
        != {
            "label": "rfq_full_stage_repair03",
            "path": "logs/resources/rfq_full_stage_repair03.json",
        }
        or state.get("inner_payload_parser_contract") != parser_binding
        or state.get("registered_rfq_query_sha256")
        != parent.get("registered_rfq_query_sha256")
    ):
        fail("attempt-04 state is not the exact DuckDB OOM boundary")

    resource = load(resource_path, "attempt-04 resource receipt")
    base.require_exact_keys(
        resource,
        {
            "command", "completed_at_utc", "cost_rate_usd_per_hour", "cpu_hours",
            "cpu_system_seconds", "cpu_user_seconds",
            "cumulative_children_max_rss_kib", "disk_free_after_bytes",
            "disk_free_before_bytes", "estimated_compute_cost_usd", "label",
            "minimum_disk_free_bytes_polled", "peak_process_tree_rss_kib_polled",
            "peak_stage_cache_bytes_polled", "peak_temp_bytes_polled",
            "poll_samples", "poll_seconds", "return_code", "rss_note",
            "s3_bytes_read_by_analysis", "s3_note", "schema_version",
            "started_at_utc", "wall_seconds",
        },
        "attempt-04 resource receipt",
    )
    if (
        resource.get("schema_version") != "w09-stage-resource-v1"
        or resource.get("label") != "rfq_full_stage_repair03"
        or resource.get("return_code") != 1
        or resource.get("command") != _expected_failed_command(run_dir.name)
        or resource.get("started_at_utc") != "2026-07-15T16:45:24.076899Z"
        or resource.get("completed_at_utc") != "2026-07-15T17:00:34.075652Z"
        or resource.get("wall_seconds") != 909.999
        or resource.get("peak_process_tree_rss_kib_polled") != 40_379_312
        or resource.get("cumulative_children_max_rss_kib") != 40_429_620
        or resource.get("peak_temp_bytes_polled") != PRIOR_PEAK_TEMP_BYTES
        or resource.get("minimum_disk_free_bytes_polled")
        != PRIOR_MINIMUM_DISK_FREE_BYTES
        or resource.get("disk_free_before_bytes") != PRIOR_DISK_FREE_BEFORE_BYTES
        or resource.get("disk_free_after_bytes") != 124_324_532_224
        or resource.get("peak_stage_cache_bytes_polled") != 114_093_269_135
        or resource.get("poll_samples") != 182
        or resource.get("poll_seconds") != 5.0
        or resource.get("s3_bytes_read_by_analysis") != 0
    ):
        fail("attempt-04 resource receipt mismatch")
    if (
        resource["disk_free_before_bytes"]
        - resource["minimum_disk_free_bytes_polled"]
        != PRIOR_MAX_DISK_DELTA_BYTES
        or CURRENT_FREE_AFTER_PRESERVE_BYTES - PRIOR_MAX_DISK_DELTA_BYTES
        != PROJECTED_MINIMUM_DISK_FREE_BYTES
    ):
        fail("repair-04 disk-safety arithmetic mismatch")

    identity = load(input_path, "attempt-04 RFQ input identity")
    base.require_exact_keys(
        identity,
        {
            "consumed_bytes", "consumed_logical_bindings",
            "consumed_object_set_sha256", "consumed_objects",
            "consumed_unique_objects", "coverage_status", "cycle1_duckdb_binding",
            "deduplicated_overlapping_objects", "expected_success_resource",
            "failed_attempt_binding", "failed_attempt_bindings", "full_object_coverage",
            "inner_payload_parser_contract", "line_salvage",
            "logical_manifest_bindings_total", "manifest_object_set_sha256",
            "quarantine_details", "quarantine_gap_plan", "quarantine_reasons",
            "quarantined_bytes", "quarantined_logical_bindings",
            "quarantined_object_set_sha256", "quarantined_unique_objects",
            "registered_rfq_query_sha256", "release_ids", "releases",
            "repair_chain", "run_id", "schema", "selection_fingerprint_sha256",
            "unique_bytes_total", "unique_objects_total", "whole_object_quarantine",
        },
        "attempt-04 RFQ input identity",
    )
    consumed = identity.get("consumed_objects")
    if (
        identity.get("schema") != "rfq-full-input-identity-v3"
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
        or identity.get("quarantined_logical_bindings")
        != QUARANTINED_LOGICAL_BINDINGS
        or identity.get("quarantined_bytes") != QUARANTINED_BYTES
        or identity.get("quarantined_object_set_sha256")
        != QUARANTINED_FINGERPRINT
        or identity.get("selection_fingerprint_sha256") != SELECTION_FINGERPRINT
        or not isinstance(consumed, list)
        or len(consumed) != RETAINED_OBJECTS
        or repair02.object_fingerprint(consumed) != RETAINED_FINGERPRINT
        or identity.get("cycle1_duckdb_binding")
        != parent.get("cycle1_duckdb_binding")
        or identity.get("expected_success_resource")
        != state.get("expected_success_resource")
        or identity.get("inner_payload_parser_contract") != parser_binding
        or identity.get("registered_rfq_query_sha256")
        != parent.get("registered_rfq_query_sha256")
        or identity.get("failed_attempt_binding") is not None
        or not isinstance(identity.get("repair_chain"), list)
        or [row.get("repair_id") for row in identity["repair_chain"]]
        != ["repair-01", "repair-02", "repair-03"]
        or not isinstance(identity.get("failed_attempt_bindings"), list)
        or len(identity["failed_attempt_bindings"]) != 3
    ):
        fail("attempt-04 input identity is not the exact repair-03 retained set")
    if (
        state.get("repair_chain") != identity.get("repair_chain")
        or state.get("failed_attempt_bindings")
        != identity.get("failed_attempt_bindings")
        or state.get("quarantine_gap_plan") != identity.get("quarantine_gap_plan")
    ):
        fail("attempt-04 state/input inherited chains differ")
    final_repair = identity["repair_chain"][-1]
    if (
        final_repair.get("registration_path") != parent.get("repair_receipt_path")
        or final_repair.get("registration_sha256")
        != parent.get("repair_receipt_sha256")
        or final_repair.get("parser_contract_path")
        != parent.get("parser_contract_path")
        or final_repair.get("parser_contract_sha256")
        != parent.get("parser_contract_sha256")
    ):
        fail("attempt-04 repair chain does not bind the exact repair-03 parent")
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
        fail("attempt-04 scratch receipt hash is not the approved boundary")
    receipt = load(receipt_path, "attempt-04 failed scratch receipt")
    base.require_exact_keys(
        receipt,
        {
            "bytes", "disposition", "input_fingerprint", "mtime_utc",
            "original_scratch_path", "preserved_scratch_path", "resume_allowed",
            "run_id", "schema_version", "sha256",
        },
        "attempt-04 failed scratch receipt",
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
        fail("attempt-04 scratch receipt does not enforce exact preserve/no-resume")
    active = run_dir / SCRATCH
    wal = Path(str(active) + ".wal")
    preserved = run_dir / PRESERVED_SCRATCH
    if active.exists() or wal.exists():
        fail("active attempt-04 scratch/WAL still exists; fresh scratch is not safe")
    try:
        preserved_stat = preserved.stat()
    except OSError:
        fail("preserved attempt-04 scratch is missing")
    if (
        preserved.is_symlink()
        or not preserved.is_file()
        or preserved_stat.st_ino != FAILED_SCRATCH_ORIGINAL_INODE
        or preserved_stat.st_size != FAILED_SCRATCH_BYTES
        or base.sha256(preserved) != FAILED_SCRATCH_SHA256
    ):
        fail("preserved attempt-04 scratch does not match its receipt/inode")
    return receipt


def _capture_preserved_scratch_identities(
    run_dir: Path, repairs: list[dict[str, Any]]
) -> dict[Path, tuple[int, int, int, int, int]]:
    """Bind every prior failed scratch; repair-04 may delete none of them."""
    identities: dict[Path, tuple[int, int, int, int, int]] = {}
    for record in repairs:
        if not isinstance(record, dict):
            continue
        failed_attempt = record.get("failed_attempt")
        if not isinstance(failed_attempt, dict):
            continue
        relative = failed_attempt.get("preserved_scratch_active_path")
        digest = failed_attempt.get("preserved_scratch_sha256")
        size = failed_attempt.get("preserved_scratch_bytes")
        if relative is None:
            # repair-01 and repair-02 predate the explicit preserved-scratch
            # fields.  Their immutable archived receipts carry the same binding.
            receipt_relative = (
                failed_attempt.get("scratch_receipt_path")
                or record.get("failed_scratch_receipt_path")
            )
            receipt_sha = (
                failed_attempt.get("scratch_receipt_sha256")
                or record.get("failed_scratch_receipt_sha256")
            )
            if receipt_relative is None:
                continue
            receipt_path = verify_bound_file(
                run_dir,
                receipt_relative,
                receipt_sha,
                f"{record.get('repair_id')} failed scratch receipt",
            )
            receipt = load(
                receipt_path, f"{record.get('repair_id')} failed scratch receipt"
            )
            absolute = receipt.get("preserved_scratch_path")
            expected_prefix = f"/srv/w09-research/runs/{run_dir.name}/"
            if not isinstance(absolute, str) or not absolute.startswith(expected_prefix):
                fail(f"{record.get('repair_id')} preserved scratch path is unsafe")
            relative = absolute[len(expected_prefix):]
            digest = receipt.get("sha256")
            size = receipt.get("bytes")
        path = verify_bound_file(
            run_dir, relative, digest,
            f"{record.get('repair_id')} preserved failed scratch",
        )
        if path.stat().st_size != size:
            fail(f"{record.get('repair_id')} preserved scratch size changed")
        value = path.stat()
        identities[path] = (
            value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns,
        )
    current = run_dir / PRESERVED_SCRATCH
    value = current.stat()
    identities[current] = (
        value.st_dev, value.st_ino, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )
    return identities


def make_trial_records(
    applied_at: str,
    old_repository: dict[str, Any],
    current_identity: dict[str, Any],
    evidence: dict[str, Any],
    coverage: dict[str, Any],
    cumulative: list[dict[str, Any]],
    resource: dict[str, Any],
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
        "stage": "RFQ_FULL_STAGE_REPAIR03",
        "failure_class": FINDING,
        "failure_disposition": FAILURE_DISPOSITION,
        "failure_phase": FAILED_PHASE,
        "error_type": FAILED_ERROR_TYPE,
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
        "resource_label": "rfq_full_stage_repair03",
        "return_code": 1,
        "resource_metrics": {
            "wall_seconds": resource["wall_seconds"],
            "peak_process_tree_rss_kib_polled": resource[
                "peak_process_tree_rss_kib_polled"
            ],
            "cumulative_children_max_rss_kib": resource[
                "cumulative_children_max_rss_kib"
            ],
            "peak_temp_bytes_polled": resource["peak_temp_bytes_polled"],
            "minimum_disk_free_bytes_polled": resource[
                "minimum_disk_free_bytes_polled"
            ],
        },
        "execution_commit": old_repository["execution_commit"],
        "source_manifest_sha256": old_repository["source_manifest_sha256"],
        "source_sha256s_sha256": old_repository["source_sha256s_sha256"],
        "query_set_sha256": old_repository["query_set_sha256"],
    }
    preregistration = {
        **common,
        "trial_registration_id": TRIAL_IDS[1],
        "record_type": "RESOURCE_CONTRACT_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR04_PREREGISTRATION",
        "finding": FINDING,
        "registration_change_class": CHANGE_CLASS,
        "rfq_result_state": NO_RFQ_RESULT,
        "retry_requirement": RETRY_REQUIREMENT,
        "resource_contract_path": evidence["resource_contract_path"],
        "resource_contract_sha256": evidence["resource_contract_sha256"],
        "authority_basis_path": evidence["authority_basis_path"],
        "authority_basis_sha256": evidence["authority_basis_sha256"],
        "previous_runtime": copy.deepcopy(PREVIOUS_RUNTIME_CONTRACT),
        "current_runtime": copy.deepcopy(RUNTIME_CONTRACT),
        "expected_success_resource": {
            "label": "rfq_full_stage_repair04",
            "path": "logs/resources/rfq_full_stage_repair04.json",
        },
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
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "core_results_recomputed": False,
    }
    return failure, preregistration


def _validate_committed_contract(run_dir: Path, record: dict[str, Any]) -> None:
    contract_path = verify_bound_file(
        run_dir,
        record.get("resource_contract_path"),
        record.get("resource_contract_sha256"),
        "repair-04 resource contract",
    )
    contract = load(contract_path, "repair-04 resource contract")
    expected = make_resource_contract(
        run_dir.name, record.get("applied_at_utc"), record
    )
    if contract != expected:
        fail("committed repair-04 resource contract changed")
    authority_path = verify_bound_file(
        run_dir,
        record.get("authority_basis_path"),
        record.get("authority_basis_sha256"),
        "repair-04 authority basis",
    )
    authority = load(authority_path, "repair-04 authority basis")
    expected_authority = make_authority_basis(
        run_dir.name,
        record.get("applied_at_utc"),
        record.get("resource_contract_path"),
        record.get("resource_contract_sha256"),
    )
    if authority != expected_authority:
        fail("committed repair-04 authority basis changed")


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
        or len(records) != 4
        or [row.get("repair_id") if isinstance(row, dict) else None for row in records]
        != ["repair-01", "repair-02", "repair-03", REPAIR_ID]
    ):
        fail("unknown or inconsistent existing repair-04 history")
    parent, record = records[-2], records[-1]
    if (
        record.get("schema_version") != REPAIR_SCHEMA
        or record.get("parent_repair_id") != PARENT_REPAIR_ID
        or record.get("pre_repair_status") != EXPECTED_STATUS
        or record.get("post_repair_status") != POST_REPAIR_STATUS
        or record.get("failed_stage_status") != FAILED_STAGE_STATUS
        or record.get("finding") != FINDING
        or record.get("failure_disposition") != FAILURE_DISPOSITION
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
        or record.get("parser_contract") != parent.get("parser_contract")
        or record.get("parser_contract_path") != parent.get("parser_contract_path")
        or record.get("parser_contract_sha256") != parent.get("parser_contract_sha256")
    ):
        fail("repair-03/repair-04 append chain changed")

    repair_dir = run_dir / "DATA_INTEGRITY/repairs/repair-04"
    archive = repair_dir / "pre_repair"
    if (
        record.get("archive_path")
        != "DATA_INTEGRITY/repairs/repair-04/pre_repair"
        or record.get("archive_inventory") != base.archive_inventory(archive)
    ):
        fail("repair-04 archive inventory changed")
    receipt_path = verify_bound_file(
        run_dir,
        record.get("repair_receipt_path"),
        record.get("repair_receipt_sha256"),
        "repair-04 registration receipt",
    )
    receipt = load(receipt_path, "repair-04 registration receipt")
    without_self = copy.deepcopy(record)
    without_self.pop("repair_receipt_path", None)
    without_self.pop("repair_receipt_sha256", None)
    if receipt != without_self:
        fail("repair-04 receipt/manifest record mismatch")
    journal_path = verify_bound_file(
        run_dir,
        record.get("transaction_journal_path"),
        record.get("transaction_journal_sha256"),
        "repair-04 transaction journal",
    )
    journal = load(journal_path, "repair-04 transaction journal")
    if (
        journal.get("schema_version") != JOURNAL_SCHEMA
        or journal.get("repair_id") != REPAIR_ID
        or journal.get("run_id") != run_dir.name
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
        or _safe_relative_file_set(repair_dir)
        != set(journal.get("expected_repair_files", []))
    ):
        fail("repair-04 committed transaction journal mismatch")
    for row in journal.get("active_mutations", []):
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            fail("repair-04 committed transaction mutation is invalid")
        active = base.checked_run_path(
            run_dir, row["path"], "committed repair-04 active mutation"
        )
        if not active.is_file() or base.sha256(active) != row.get(
            "replacement_sha256"
        ):
            fail(f"repair-04 committed active bytes changed: {row['path']}")
    registry_mutations = [
        row for row in journal.get("active_mutations", [])
        if isinstance(row, dict) and row.get("append_only_registry") is True
    ]
    if len(registry_mutations) != 1:
        fail("repair-04 committed registry mutation is missing or duplicated")
    registry_mutation = registry_mutations[0]
    original_registry = verify_bound_file(
        run_dir,
        registry_mutation.get("original_archive_path"),
        registry_mutation.get("original_sha256"),
        "repair-04 archived original registry",
    ).read_bytes()
    replacement_registry = repair_dir / "post_repair/TRIAL_REGISTRY.jsonl"
    if (
        replacement_registry.is_symlink()
        or not replacement_registry.is_file()
        or base.sha256(replacement_registry)
        != registry_mutation.get("replacement_sha256")
        or not replacement_registry.read_bytes().startswith(original_registry)
    ):
        fail("repair-04 committed registry replacement changed")
    registry_append = replacement_registry.read_bytes()[len(original_registry):]
    expected_progress = _registry_progress_payload(
        run_dir.name,
        original_registry,
        registry_append,
        "APPEND_COMPLETE",
        len(registry_append),
    )
    progress_path = repair_dir / REGISTRY_APPEND_PROGRESS
    if progress_path.is_symlink() or progress_path.read_bytes() != expected_progress:
        fail("repair-04 committed registry append progress changed")
    for path_field, sha_field in (
        ("failed_state_path", "failed_state_sha256"),
        ("failed_resource_receipt_path", "failed_resource_receipt_sha256"),
        ("failed_scratch_receipt_path", "failed_scratch_receipt_sha256"),
        ("failed_input_identity_path", "failed_input_identity_sha256"),
        ("resource_contract_path", "resource_contract_sha256"),
        ("authority_basis_path", "authority_basis_sha256"),
        ("cycle1_binding_path", "cycle1_binding_sha256"),
        ("w09_attestation_path", "w09_attestation_sha256"),
        ("parser_contract_path", "parser_contract_sha256"),
    ):
        verify_bound_file(run_dir, record.get(path_field), record.get(sha_field), path_field)
    _validate_committed_contract(run_dir, record)
    failed = record.get("failed_attempt")
    if not isinstance(failed, dict):
        fail("repair-04 failed-attempt binding is missing")
    preserved = base.checked_run_path(
        run_dir,
        failed.get("preserved_scratch_active_path"),
        "repair-04 preserved failed scratch",
    )
    if (
        preserved.is_symlink()
        or not preserved.is_file()
        or preserved.stat().st_ino != failed.get("preserved_scratch_original_inode")
        or preserved.stat().st_size != failed.get("preserved_scratch_bytes")
        or base.sha256(preserved) != failed.get("preserved_scratch_sha256")
        or (run_dir / SCRATCH).exists()
        or Path(str(run_dir / SCRATCH) + ".wal").exists()
    ):
        fail("repair-04 preserved scratch/no-resume boundary changed")
    _capture_preserved_scratch_identities(run_dir, records[:-1])
    if record.get("core_result_artifacts") != base.core_result_inventory(run_dir):
        fail("Cycle-1 core artifacts changed after repair-04")
    _, cycle_binding = base.validate_cycle1_duckdb_binding(
        run_dir, run_dir / CYCLE1_BINDING
    )
    if (
        cycle_binding != parent.get("cycle1_duckdb_binding")
        or record.get("cycle1_duckdb_binding") != cycle_binding
    ):
        fail("active Cycle-1 DuckDB binding changed after repair-04")
    for result in (RFQ_SUMMARY, RFQ_REPORT, RFQ_CATALOG):
        if (run_dir / result).exists():
            fail("RFQ result exists at the repair-04 preregistration boundary")

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
        fail("repair-04 trial append boundary changed")
    repository = manifest.get("repository")
    if (
        not isinstance(repository, dict)
        or repository.get("registration_repair_id") != REPAIR_ID
        or base.repository_identity(repository)
        != record.get("current_repository_identity")
        or repository.get("identity_history")
        != record.get("repository_identity_chain")
        or len(repository.get("identity_history", [])) != 5
    ):
        fail("repair-04 repository identity chain mismatch")
    source_mode = record.get("source_verification_mode")
    if source_mode != GIT_SOURCE_MODE:
        fail("repair-04 requires a clean local Git commit proof")
    if repo_root is not None or execution_commit is not None:
        fail("repair-04 does not accept Gitless snapshot attestations")
    source_repo_root, source_relative, head = _verify_clean_source_provenance(
        source_dir
    )
    _validate_git_commit_scope(source_repo_root, head)
    source_manifest_payload, source_sums_payload = (
        _validate_clean_head_source_binding(
            source_dir,
            source_repo_root,
            source_relative,
            head,
        )
    )
    if head != record.get("current_execution_commit"):
        fail("clean source HEAD no longer matches repair-04")
    base.verify_source_sums_match_manifest(
        run_dir / "SOURCE_MANIFEST.json",
        run_dir / "SOURCE_SHA256SUMS.txt",
        "repair-04 current",
    )
    if (
        base.sha256(run_dir / "SOURCE_MANIFEST.json")
        != record.get("current_source_manifest_sha256")
        or base.sha256(run_dir / "SOURCE_SHA256SUMS.txt")
        != record.get("current_source_sha256s_sha256")
        or base.sha256_bytes(source_manifest_payload)
        != record.get("current_source_manifest_sha256")
        or base.sha256_bytes(source_sums_payload)
        != record.get("current_source_sha256s_sha256")
        or base.sha256(run_dir / "QUERY_SHA256SUMS.txt")
        != record.get("current_query_set_sha256")
    ):
        fail("repair-04 active source/query receipts changed")
    return {
        "status": "REGISTRATION_REPAIR04_ALREADY_APPLIED",
        "repair_id": REPAIR_ID,
        "run_id": run_dir.name,
        "execution_commit": head,
        "retained_objects": RETAINED_OBJECTS,
        "retained_bytes": RETAINED_BYTES,
        "retained_selection_fingerprint_sha256": SELECTION_FINGERPRINT,
        "rfq_result_state": NO_RFQ_RESULT,
        "retry_requirement": RETRY_REQUIREMENT,
    }


def repair04_registration(
    run_dir: Path,
    source_dir: Path,
    *,
    repo_root: Path | None = None,
    execution_commit: str | None = None,
) -> dict[str, Any]:
    """Serialize the complete repair-04 registration for one run."""
    if repo_root is not None or execution_commit is not None:
        fail(
            "repair-04 requires a clean local Git commit proof; "
            "Gitless snapshot attestations are not accepted"
        )
    if Path(run_dir).is_symlink():
        fail("run-dir must not be a symlink")
    resolved_run_dir = Path(run_dir).resolve()
    # The repair-03 lock is intentionally the shared preregistration owner lock.
    # Reusing its audited inode-safe acquisition also prevents cross-version
    # repair-03/repair-04 transactions from racing on the same run.
    with repair03._exclusive_run_lock(resolved_run_dir):
        return _repair04_registration_locked(
            resolved_run_dir,
            source_dir,
            repo_root=repo_root,
            execution_commit=execution_commit,
        )


def _repair04_registration_locked(
    run_dir: Path,
    source_dir: Path,
    *,
    repo_root: Path | None = None,
    execution_commit: str | None = None,
) -> dict[str, Any]:
    if Path(source_dir).is_symlink():
        fail("source-dir must not be a symlink")
    run_dir = run_dir.resolve()
    source_dir = source_dir.resolve()
    snapshot_requested = repo_root is not None or execution_commit is not None
    if snapshot_requested:
        fail(
            "repair-04 requires a clean local Git commit proof; "
            "Gitless snapshot attestations are not accepted"
        )
    resolved_repo_root = None

    manifest_path = run_dir / "RUN_MANIFEST.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        fail("RUN_MANIFEST.json is missing or unsafe")
    manifest_raw = manifest_path.read_bytes()
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
    if len(records) == 4:
        return validate_already_applied(
            run_dir,
            source_dir,
            manifest,
            records,
            resolved_repo_root,
            execution_commit,
        )
    if len(records) != 3:
        fail("repair-04 requires exactly the repair-01 -> repair-03 parent chain")

    repair_dir = run_dir / "DATA_INTEGRITY/repairs/repair-04"
    _cleanup_recovery_tombstone(repair_dir)
    if repair_dir.exists():
        recover_incomplete_transaction(run_dir, repair_dir)
        if repair_dir.exists():
            fail("repair-04 incomplete transaction recovery did not finish")

    parent, old_repository, trial_before, _, core_before = validate_parent_repair(
        run_dir, manifest
    )
    failed = validate_failed_attempt(run_dir, parent)
    scratch_receipt = validate_failed_scratch(run_dir, failed["state"])
    scratch_identities = _capture_preserved_scratch_identities(run_dir, records)
    _, w09_attestation_sha = validate_w09_attestation(run_dir, manifest)
    _, cycle_binding = base.validate_cycle1_duckdb_binding(
        run_dir, run_dir / CYCLE1_BINDING
    )
    if cycle_binding != parent.get("cycle1_duckdb_binding"):
        fail("active Cycle-1 DuckDB binding changed after repair-03")

    source_attestation: dict[str, Any] | None = None
    source_attestation_sha: str | None = None
    clean_head_receipts: tuple[bytes, bytes] | None = None
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
                "active repair-04 source snapshot attestation requires explicit "
                "snapshot repo-root/execution-commit mode"
            )
        source_repo_root, source_relative, current_commit = (
            _verify_clean_source_provenance(source_dir)
        )
        _validate_git_commit_scope(source_repo_root, current_commit)
        clean_head_receipts = _validate_clean_head_source_binding(
            source_dir,
            source_repo_root,
            source_relative,
            current_commit,
        )
        verified_repository, query_names = _verify_prior_repository_provenance(
            run_dir,
            source_dir,
            source_repo_root,
            source_relative,
            current_commit,
            manifest,
        )
        if verified_repository != old_repository:
            fail("repair-04 prior repository validation changed the parent identity")
        source_mode = GIT_SOURCE_MODE
    if old_repository.get("execution_commit") != PARENT_EXECUTION_COMMIT:
        fail("repair-04 source parent is not the committed repair-03 identity")

    if clean_head_receipts is None:
        _, source_manifest_payload, source_sums_payload = base.build_source_receipts(
            source_dir, source_repo_root
        )
    else:
        source_manifest_payload, source_sums_payload = clean_head_receipts
    source_manifest_sha = base.sha256_bytes(source_manifest_payload)
    source_sums_sha = base.sha256_bytes(source_sums_payload)
    if snapshot_requested:
        source_attestation, source_attestation_sha = (
            validate_source_snapshot_attestation(
                run_dir,
                current_commit,
                source_manifest_sha,
                source_sums_sha,
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
        fail("repair-04 query set paths differ from the registered parent")
    query_sums_payload = "".join(
        f"{digest}  {relative}\n" for digest, relative, _ in query_rows
    ).encode("utf-8")
    query_sha = base.sha256_bytes(query_sums_payload)
    rfq_query_rows = [
        row
        for row in query_rows
        if row[1] == RETRY_EXECUTION_QUERY.as_posix()
    ]
    if len(rfq_query_rows) != 1:
        fail("repair-04 requires exactly one registered rfq_full_stage.py query")
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
        or len(old_chain) != 4
        or old_chain != parent.get("repository_identity_chain")
        or old_chain[-1] != previous_identity
    ):
        fail("repair-03 repository history is not the exact four-identity chain")
    identity_chain = copy.deepcopy(old_chain) + [current_identity]
    if len({canonical_hash(row) for row in identity_chain}) != 5:
        fail("repair-04 repository identity chain is not five distinct revisions")
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
        CYCLE1_BINDING: base.sha256(run_dir / CYCLE1_BINDING),
        W09_ATTESTATION: w09_attestation_sha,
    }
    for digest, relative in base.parse_checksums(
        run_dir / "QUERY_SHA256SUMS.txt", "repair-03 query receipt"
    ):
        validated_archive_hashes[Path(relative)] = digest
    if source_mode == SNAPSHOT_SOURCE_MODE:
        assert source_attestation_sha is not None
        validated_archive_hashes[SOURCE_ATTESTATION] = source_attestation_sha

    applied_at = base.now_utc()
    repair_root = run_dir / "DATA_INTEGRITY/repairs"
    repair_root.mkdir(parents=True, exist_ok=True)
    staging: Path | None = Path(
        tempfile.mkdtemp(prefix=".repair-04-", dir=repair_root)
    )
    try:
        pre = staging / "pre_repair"
        archive_paths = (
            Path("RUN_MANIFEST.json"), Path("TRIAL_REGISTRY.jsonl"),
            Path("SOURCE_MANIFEST.json"), Path("SOURCE_SHA256SUMS.txt"),
            Path("QUERY_SHA256SUMS.txt"), STATE, RESOURCE, SCRATCH_RECEIPT,
            INPUT_IDENTITY, CYCLE1_BINDING, W09_ATTESTATION,
        )
        if source_mode == SNAPSHOT_SOURCE_MODE:
            archive_paths += (SOURCE_ATTESTATION,)
        for relative in archive_paths:
            base.copy_exact(run_dir / relative, pre / relative)
        for query_relative in old_repository["query_files"]:
            base.copy_exact(
                base.checked_run_path(run_dir, query_relative, "repair-03 query"),
                pre / Path(query_relative),
            )
        if set(validated_archive_hashes) != {
            *archive_paths,
            *(Path(relative) for relative in old_repository["query_files"]),
        }:
            fail("repair-04 validated archive path set is incomplete")
        for relative, expected_sha in validated_archive_hashes.items():
            archived = pre / relative
            if not archived.is_file() or base.sha256(archived) != expected_sha:
                fail(f"repair-04 pre-repair archive raced validation: {relative}")

        prefix = "DATA_INTEGRITY/repairs/repair-04"
        evidence: dict[str, Any] = {
            "failed_state_path": f"{prefix}/pre_repair/{STATE.as_posix()}",
            "failed_state_sha256": failed["state_sha256"],
            "failed_resource_receipt_path": (
                f"{prefix}/pre_repair/{RESOURCE.as_posix()}"
            ),
            "failed_resource_receipt_sha256": failed["resource_sha256"],
            "failed_scratch_receipt_path": (
                f"{prefix}/pre_repair/{SCRATCH_RECEIPT.as_posix()}"
            ),
            "failed_scratch_receipt_sha256": FAILED_SCRATCH_RECEIPT_SHA256,
            "failed_input_identity_path": (
                f"{prefix}/pre_repair/{INPUT_IDENTITY.as_posix()}"
            ),
            "failed_input_identity_sha256": failed["input_sha256"],
            "cycle1_binding_path": (
                f"{prefix}/pre_repair/{CYCLE1_BINDING.as_posix()}"
            ),
            "cycle1_binding_sha256": base.sha256(run_dir / CYCLE1_BINDING),
            "w09_attestation_path": (
                f"{prefix}/pre_repair/{W09_ATTESTATION.as_posix()}"
            ),
            "w09_attestation_sha256": w09_attestation_sha,
        }
        if source_mode == SNAPSHOT_SOURCE_MODE:
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
        resource_contract_relative = f"{prefix}/{RESOURCE_CONTRACT_FILE}"
        authority_relative = f"{prefix}/{AUTHORITY_FILE}"
        resource_contract = make_resource_contract(run_dir.name, applied_at, evidence)
        base.atomic_write(
            staging / RESOURCE_CONTRACT_FILE,
            base.json_payload(resource_contract),
        )
        evidence["resource_contract_path"] = resource_contract_relative
        evidence["resource_contract_sha256"] = base.sha256(
            staging / RESOURCE_CONTRACT_FILE
        )
        authority_basis = make_authority_basis(
            run_dir.name,
            applied_at,
            resource_contract_relative,
            evidence["resource_contract_sha256"],
        )
        base.atomic_write(
            staging / AUTHORITY_FILE, base.json_payload(authority_basis)
        )
        evidence["authority_basis_path"] = authority_relative
        evidence["authority_basis_sha256"] = base.sha256(staging / AUTHORITY_FILE)

        post = staging / "post_repair"
        base.atomic_write(post / "SOURCE_MANIFEST.json", source_manifest_payload)
        base.atomic_write(post / "SOURCE_SHA256SUMS.txt", source_sums_payload)
        base.atomic_write(post / "QUERY_SHA256SUMS.txt", query_sums_payload)
        for _, relative, payload in query_rows:
            base.atomic_write(post / Path(relative), payload)

        archive_rows = base.archive_inventory(pre)
        trial_failure, trial_preregistration = make_trial_records(
            applied_at,
            old_repository,
            current_identity,
            evidence,
            coverage,
            cumulative,
            failed["resource"],
        )
        trial_after = (
            trial_before
            + base.registry_line(trial_failure)
            + base.registry_line(trial_preregistration)
        )
        base.atomic_write(post / "TRIAL_REGISTRY.jsonl", trial_after)
        trial_append = trial_after[len(trial_before):]
        if not trial_append:
            fail("repair-04 trial append payload is empty")
        base.atomic_write(
            staging / REGISTRY_APPEND_PROGRESS,
            _registry_progress_payload(
                run_dir.name, trial_before, trial_append, "NOT_STARTED", 0
            ),
        )
        repair03._fsync_directory(staging)

        mutation_relatives = [
            "SOURCE_MANIFEST.json", "SOURCE_SHA256SUMS.txt",
            "QUERY_SHA256SUMS.txt", *old_repository["query_files"],
            "TRIAL_REGISTRY.jsonl",
        ]
        if len(mutation_relatives) != len(set(mutation_relatives)):
            fail("repair-04 active mutation paths are duplicated")
        active_mutations: list[dict[str, Any]] = []
        for relative in mutation_relatives:
            original_path = pre / Path(relative)
            replacement_path = post / Path(relative)
            if not original_path.is_file() or not replacement_path.is_file():
                fail(f"repair-04 transaction payload is missing: {relative}")
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
            RESOURCE_CONTRACT_FILE, AUTHORITY_FILE, "REPAIR_REGISTRATION.json",
            TRANSACTION_JOURNAL, REGISTRY_APPEND_PROGRESS,
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
            "failure_disposition": FAILURE_DISPOSITION,
            "registration_change_class": CHANGE_CLASS,
            "source_verification_mode": source_mode,
            "quarantine_policy": QUARANTINE_POLICY,
            "previous_repair_registration_path": parent["repair_receipt_path"],
            "previous_repair_registration_sha256": parent[
                "repair_receipt_sha256"
            ],
            "previous_repair_record_sha256": parent_record_sha,
            **evidence,
            "registered_rfq_query_sha256": registered_rfq_query_sha,
            "parent_registered_rfq_query_sha256": parent[
                "registered_rfq_query_sha256"
            ],
            "parser_contract_path": parent["parser_contract_path"],
            "parser_contract_sha256": parent["parser_contract_sha256"],
            "parser_contract": copy.deepcopy(parent["parser_contract"]),
            "authority_basis": {
                "path": authority_relative,
                "sha256": evidence["authority_basis_sha256"],
                "schema_version": AUTHORITY_SCHEMA,
                "authority_class": AUTHORITY_CLASS,
                "permitted_change": authority_basis["permitted_change"],
            },
            "failed_attempt": {
                "attempt_id": "RFQ_FULL_STAGE_REPAIR03_ATTEMPT_04",
                "state_active_path": STATE.as_posix(),
                "state_path": evidence["failed_state_path"],
                "state_sha256": evidence["failed_state_sha256"],
                "state_status": FAILED_STAGE_STATUS,
                "error_type": FAILED_ERROR_TYPE,
                "error": FAILED_ERROR,
                "failure_phase": FAILED_PHASE,
                "resource_active_path": RESOURCE.as_posix(),
                "resource_path": evidence["failed_resource_receipt_path"],
                "resource_sha256": evidence["failed_resource_receipt_sha256"],
                "resource_label": "rfq_full_stage_repair03",
                "return_code": 1,
                "resource_command": copy.deepcopy(failed["resource"]["command"]),
                "resource_metrics": {
                    "started_at_utc": failed["resource"]["started_at_utc"],
                    "completed_at_utc": failed["resource"]["completed_at_utc"],
                    "wall_seconds": failed["resource"]["wall_seconds"],
                    "peak_process_tree_rss_kib_polled": failed["resource"][
                        "peak_process_tree_rss_kib_polled"
                    ],
                    "cumulative_children_max_rss_kib": failed["resource"][
                        "cumulative_children_max_rss_kib"
                    ],
                    "peak_temp_bytes_polled": failed["resource"][
                        "peak_temp_bytes_polled"
                    ],
                    "minimum_disk_free_bytes_polled": failed["resource"][
                        "minimum_disk_free_bytes_polled"
                    ],
                    "disk_free_before_bytes": failed["resource"][
                        "disk_free_before_bytes"
                    ],
                    "disk_free_after_bytes": failed["resource"][
                        "disk_free_after_bytes"
                    ],
                },
                "scratch_receipt_active_path": SCRATCH_RECEIPT.as_posix(),
                "scratch_receipt_path": evidence["failed_scratch_receipt_path"],
                "scratch_receipt_sha256": evidence[
                    "failed_scratch_receipt_sha256"
                ],
                "preserved_scratch_active_path": PRESERVED_SCRATCH.as_posix(),
                "preserved_scratch_sha256": scratch_receipt["sha256"],
                "preserved_scratch_bytes": scratch_receipt["bytes"],
                "preserved_scratch_mtime_utc": scratch_receipt["mtime_utc"],
                "preserved_scratch_original_inode": (
                    FAILED_SCRATCH_ORIGINAL_INODE
                ),
                "input_identity_active_path": INPUT_IDENTITY.as_posix(),
                "input_identity_path": evidence["failed_input_identity_path"],
                "input_identity_sha256": evidence[
                    "failed_input_identity_sha256"
                ],
                "input_identity_schema": "rfq-full-input-identity-v3",
                "input_fingerprint": SELECTION_FINGERPRINT,
                "scratch_disposition": "PRESERVED_RENAMED_NO_RESUME",
                "retry_requirement": RETRY_REQUIREMENT,
            },
            "expected_success_resource": {
                "label": "rfq_full_stage_repair04",
                "path": "logs/resources/rfq_full_stage_repair04.json",
            },
            "resource_contract": {
                "path": resource_contract_relative,
                "sha256": evidence["resource_contract_sha256"],
                "schema_version": RESOURCE_CONTRACT_SCHEMA,
                "previous_runtime": copy.deepcopy(PREVIOUS_RUNTIME_CONTRACT),
                "current_runtime": copy.deepcopy(RUNTIME_CONTRACT),
                "expected_command": expected_retry_command(run_dir.name),
                "w09_memtotal_bytes": W09_MEMTOTAL_BYTES,
                "memory_limit_percent_of_memtotal_rounded_2dp": (
                    MEMORY_LIMIT_PERCENT
                ),
            },
            "newly_quarantined_objects": [],
            "cumulative_quarantined_objects": cumulative,
            "coverage": coverage,
            "selection_identity_unchanged": True,
            "previous_selection_fingerprint_sha256": SELECTION_FINGERPRINT,
            "current_selection_fingerprint_sha256": SELECTION_FINGERPRINT,
            "failed_input_fingerprint": SELECTION_FINGERPRINT,
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
            "data_selection_change": "NONE",
            "parser_contract_change": "NONE",
            "query_semantics_change": "NONE",
            "quarantine_change": "NONE",
            "hypothesis_design_change": "NONE",
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
                "commit_changed_paths": EXPECTED_REPAIR04_CHANGED_PATHS,
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
            fail("prepared repair-04 transaction file set mismatch")
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
                    active_path.is_symlink()
                    or not active_path.is_file()
                    or active_path.read_bytes() != expected
                ):
                    fail(f"active identity CAS failed at {label}: {relative}")
            if base.core_result_inventory(run_dir) != core_before:
                fail(f"Cycle-1 core CAS failed at {label}")
            if (run_dir / SCRATCH).exists() or Path(
                str(run_dir / SCRATCH) + ".wal"
            ).exists():
                fail(f"fresh scratch CAS failed at {label}")
            for path, expected_identity in scratch_identities.items():
                try:
                    value = path.stat()
                except OSError:
                    fail(f"preserved failed scratch disappeared at {label}: {path}")
                current_scratch_identity = (
                    value.st_dev, value.st_ino, value.st_size,
                    value.st_mtime_ns, value.st_ctime_ns,
                )
                if path.is_symlink() or current_scratch_identity != expected_identity:
                    fail(f"preserved failed scratch CAS failed at {label}: {path}")
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
                    fail(f"published repair-04 file set changed at {label}")
                for relative, expected_sha in prepared_static_hashes.items():
                    path = repair_dir / relative
                    if base.sha256(path) != expected_sha:
                        fail(
                            f"published repair-04 payload changed at {label}: "
                            f"{relative}"
                        )
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
                    fail(f"repair-04 registry progress CAS failed at {label}")

        def cas_write(relative: str) -> None:
            label = f"write:{relative}"
            assert_active_cas(f"before {label}")
            _transaction_fault_hook(f"{label}:after_cas_before_write")
            assert_active_cas(f"immediately before {label}")
            _transaction_fault_hook(f"{label}:at_conditional_replace")
            active_path = run_dir / Path(relative)
            try:
                repair03._cas_replace_active_file(
                    active_path,
                    original_payloads[relative],
                    replacement_payloads[relative],
                    label,
                )
            finally:
                if (
                    active_path.is_file()
                    and active_path.read_bytes() == replacement_payloads[relative]
                ):
                    written.add(relative)
            if active_path.read_bytes() != replacement_payloads[relative]:
                fail(f"repair-04 active write verification failed: {relative}")
            _transaction_fault_hook(label)
            assert_active_cas(f"after {label}")

        def write_registry_progress(
            state: str, reported_bytes: int
        ) -> dict[str, Any]:
            payload = _registry_progress_payload(
                run_dir.name, trial_before, trial_append, state, reported_bytes
            )
            path = repair_dir / REGISTRY_APPEND_PROGRESS
            progress = json.loads(payload)
            _write_registry_progress_file(path, progress)
            return progress

        def append_trial_registry() -> None:
            relative = "TRIAL_REGISTRY.jsonl"
            label = f"write:{relative}"
            assert_active_cas(f"before {label}")
            original = original_payloads[relative]
            replacement = replacement_payloads[relative]
            if not replacement.startswith(original):
                fail("repair-04 registry replacement is not append-only")
            delta = replacement[len(original):]
            if not delta:
                fail("repair-04 registry append payload is empty")
            active_path = run_dir / relative
            descriptor = os.open(
                active_path,
                os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                stat_path = active_path.stat()
                stat_fd = os.fstat(descriptor)
                if (
                    active_path.is_symlink()
                    or not stat.S_ISREG(stat_fd.st_mode)
                    or stat_fd.st_nlink != 1
                    or (stat_path.st_dev, stat_path.st_ino) != (
                        stat_fd.st_dev, stat_fd.st_ino
                    )
                ):
                    fail("TRIAL_REGISTRY inode changed before append")
                locked = repair03._read_locked_file(descriptor)
                if locked != original:
                    fail("TRIAL_REGISTRY CAS failed after append lock")
                _transaction_fault_hook("trial_append_after_cas_before_write")
                if repair03._read_locked_file(descriptor) != original:
                    fail("TRIAL_REGISTRY CAS failed immediately before append")
                write_registry_progress("WRITE_IN_PROGRESS", 0)
                _transaction_fault_hook("trial_append_after_progress_before_write")
                if repair03._read_locked_file(descriptor) != original:
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
                progress = write_registry_progress("WRITE_RETURNED", count)
                if count != len(delta):
                    current = repair03._read_locked_file(descriptor)
                    if count:
                        append_start = append_end - count
                        if (
                            append_start != len(original)
                            or current[append_start:append_end] != delta[:count]
                        ):
                            fail("TRIAL_REGISTRY short append ownership cannot be proven")
                        restored = current[:append_start] + current[append_end:]
                        progress = _rollback_progress(
                            progress,
                            "PREPARED",
                            expected=current,
                            replacement=restored,
                            remove_offset=append_start,
                        )
                        progress_path = repair_dir / REGISTRY_APPEND_PROGRESS
                        _write_registry_progress_file(progress_path, progress)
                        _transaction_fault_hook(
                            "registry_short_rollback:after_rollback_plan"
                        )
                        _atomic_registry_replace_with_locked_descriptor(
                            active_path, descriptor, current, restored
                        )
                        progress = _rollback_progress(progress, "COMPLETE")
                        _write_registry_progress_file(progress_path, progress)
                        _transaction_fault_hook(
                            "registry_short_rollback:after_rollback_complete"
                        )
                    else:
                        write_registry_progress("ROLLED_BACK", 0)
                    fail("TRIAL_REGISTRY append was partial")
                os.fsync(descriptor)
                if repair03._read_locked_file(descriptor) != replacement:
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
            repair03._fsync_directory(repair_root)
        finally:
            if repair_dir.is_dir():
                published = True
                staging = None
        try:
            if not published:
                fail("repair-04 directory publication failed")
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
            _transaction_fault_hook("write:RUN_MANIFEST.json:at_conditional_replace")
            try:
                repair03._cas_replace_active_file(
                    manifest_path,
                    manifest_raw,
                    updated_manifest_payload,
                    "write:RUN_MANIFEST.json",
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
                repair03._cas_replace_active_file(
                    manifest_path,
                    updated_manifest_payload,
                    manifest_raw,
                    "rollback:RUN_MANIFEST.json",
                )
            if published and repair_dir.exists():
                try:
                    recover_incomplete_transaction(run_dir, repair_dir)
                except BaseException as rollback_error:
                    raise Repair04RegistrationError(
                        "repair-04 transaction failed and safe rollback was blocked: "
                        f"{rollback_error}"
                    ) from transaction_error
            raise
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)

    return {
        "status": "REGISTRATION_REPAIR04_REFROZEN",
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
        "resource_contract": copy.deepcopy(RUNTIME_CONTRACT),
    }


repair = repair04_registration


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Append-only SPORTS-AUTORESEARCH-01 execution-resource repair-04 "
            "preregistration"
        )
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument(
        "--repo-root",
        type=Path,
        help=(
            "REJECTED legacy snapshot option; repair-04 accepts only the clean "
            "Git repository discovered from --source-dir"
        ),
    )
    parser.add_argument(
        "--execution-commit",
        help="REJECTED legacy snapshot option; clean Git HEAD is mandatory",
    )
    args = parser.parse_args()
    result = repair04_registration(
        args.run_dir,
        args.source_dir,
        repo_root=args.repo_root,
        execution_commit=args.execution_commit,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
