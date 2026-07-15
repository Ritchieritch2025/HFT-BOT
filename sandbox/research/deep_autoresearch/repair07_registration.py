#!/usr/bin/env python3
"""Append-only equivalent hour-bucket RFQ dedup/resource preregistration."""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from typing import Any, Sequence


def _load_source(name: str) -> types.ModuleType:
    path = Path(__file__).with_name(f"{name}.py")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ModuleNotFoundError(name)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            payload = handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    prior = sys.modules.get(name)
    try:
        sys.modules[name] = module
        exec(compile(payload, str(path), "exec", dont_inherit=True), module.__dict__)
    finally:
        if prior is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prior
    return module


repair06 = _load_source("repair06_registration")
repair05 = repair06.repair05
repair04 = repair06.repair04
repair03 = repair06.repair03
base = repair06.base

REPAIR_ID = "repair-07"
PARENT_REPAIR_ID = "repair-06"
PARENT_EXECUTION_COMMIT = "6b5ec4738fe1ba82b2c93484e3113b3d7d7cde95"
SOURCE_RELATIVE = "sandbox/research/deep_autoresearch"
EXPECTED_CHANGED_PATHS = [
    f"{SOURCE_RELATIVE}/finalize_mission.py",
    f"{SOURCE_RELATIVE}/repair07_registration.py",
    f"{SOURCE_RELATIVE}/rfq_full_stage.py",
    f"{SOURCE_RELATIVE}/test_finalize_mission.py",
    f"{SOURCE_RELATIVE}/test_repair07_registration.py",
    f"{SOURCE_RELATIVE}/test_rfq_full_stage.py",
]

PRE_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_STATUS_WIRING_REPAIR06_REGISTERED"
PRE_REGISTRATION_STATE = (
    "RE_FROZEN_AFTER_RFQ_STATUS_WIRING_REPAIR06_BEFORE_RFQ_RESULT"
)
POST_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_BUCKETED_DEDUP_RESOURCE_REPAIR07_REGISTERED"
REGISTRATION_STATE = (
    "RE_FROZEN_AFTER_RFQ_EQUIVALENT_BUCKETED_DEDUP_RESOURCE_REPAIR07_"
    "BEFORE_RFQ_RESULT"
)
REPAIR_SCHEMA = "sports-autoresearch-equivalent-bucketed-dedup-resource-repair-v1"
CONTRACT_SCHEMA = "rfq-equivalent-bucketed-dedup-resource-contract-v1"
BLOCKER_SCHEMA = "rfq-repair06-dedup-oom-blocker-v1"
SCRATCH_RECEIPT_SCHEMA = "rfq-failed-scratch-receipt-v7"
AUTHORITY_SCHEMA = (
    "sports-autoresearch-equivalent-performance-resource-repair-authority-v1"
)
JOURNAL_SCHEMA = "repair07-registration-transaction-v1"
REGISTRY_PROGRESS_SCHEMA = "repair07-registry-append-progress-v1"

PREFIX = "DATA_INTEGRITY/repairs/repair-07"
PRE_ROOT = f"{PREFIX}/pre_repair"
POST_ROOT = f"{PREFIX}/post_repair"
TRANSACTION_JOURNAL = "TRANSACTION_JOURNAL.json"
REGISTRY_PROGRESS = "REGISTRY_APPEND_PROGRESS.json"
CONTRACT_FILE = "RFQ_DEDUP_BUCKET_RESOURCE_CONTRACT.json"
BLOCKER_FILE = "RFQ_REPAIR06_DEDUP_OOM_BLOCKER_07.json"
SCRATCH_RECEIPT_FILE = "RFQ_FAILED_SCRATCH_RECEIPT_07.json"
AUTHORITY_FILE = "AUTHORITY_BASIS.json"
REGISTRATION_FILE = "REPAIR_REGISTRATION.json"

RESOURCE = Path("logs/resources/rfq_full_stage_repair06.json")
STATE = Path("REPORT/tables/RFQ_FULL_STAGE_STATE.json")
INPUT_IDENTITY = Path("DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json")
CYCLE_BINDING = Path("DATA_INTEGRITY/CYCLE1_DUCKDB_BINDING.json")
W09_ATTESTATION = Path("DATA_INTEGRITY/W09_ATTESTATION.json")
SCRATCH = Path("cache/rfq_full_scratch.duckdb")
SCRATCH_WAL = Path("cache/rfq_full_scratch.duckdb.wal")
PRESERVED_SCRATCH = Path("cache/rfq_full_scratch.attempt07_failed.duckdb")

PARENT_MANIFEST_SHA256 = (
    "f959329351a0bc52f7d417390ab63641353c37e2ac63ca074ed12b859c6313fd"
)
PARENT_RECEIPT_SHA256 = (
    "738d5e8db8349d19a4d4e9886405b4f5d0471fcdbe70df8494013880520fe600"
)
PARENT_JOURNAL_SHA256 = (
    "71a888fcb85a91aac111626794d375bdc2ffbf7743a1d9db0a5be1b6c2de5277"
)
PARENT_STATUS_CONTRACT_SHA256 = (
    "f568a2854dcb4cc87f59fae6059a12683809a5d7a46b3a4141595c1d420b9808"
)
PARENT_AUTHORITY_SHA256 = (
    "046cada744b9af6b06eee9bf8af5f1f392c339f85b3e34079b12c6d71a07e396"
)
PARENT_RESOURCE_CONTRACT_SHA256 = (
    "8c36b2747d9e5f991ecd5cf18e9c44fd5cea5f5725fc2a622113d42102e88325"
)
PARENT_QUERY_SHA256 = (
    "82d6b270e53aef9c04416d19d64feecbfe1c16c3b80235e9c151d20e53511822"
)
FAILED_RESOURCE_SHA256 = (
    "b58739bb31ddab6f3007df16d766cb50ca6b199e6327130ba1870727291ee6f6"
)
FAILED_STATE_SHA256 = (
    "dfdf02eaad40455d1bc5f4d49429af46d3cf63c59983a6f35734c34207140599"
)
FAILED_INPUT_SHA256 = (
    "beb23a7a5142a963c902219ee6b8711c4aa24f866f502bbe3e68f65afded2e7c"
)
FAILED_SCRATCH_SHA256 = (
    "340a2d67af6b17c22db70672d4f0ff495b101101173227130c3cfef9b9a4801c"
)
FAILED_SCRATCH_BYTES = 28_760_616_960
FAILED_SCRATCH_MTIME_NS = 1_784_147_930_155_124_425
MISSION_SHA256 = (
    "9b4ca417dca394223ecdc6719cd1628bd5ad69d5a80003db304f73b1c485a69c"
)
BUCKET_COUNTS_SHA256 = (
    "5e63b139f17ffe1b17e2c1ab588faac9bc88d0a8b161c1be5e5b54bceb3f069d"
)
BUCKET_VALID_ROWS = 58_142_605
BUCKET_COUNT = 44
BUCKET_MAX_ROWS = 2_982_434

FINDING = "DUCKDB_OUT_OF_MEMORY_DURING_RFQ_DEDUPLICATION_WINDOW_AFTER_REPAIR06"
FAILURE_PHASE = "CREATE_TABLE_RFQ_EVENTS_VALID_GLOBAL_ROW_NUMBER_DEDUP"
FAILURE_DISPOSITION = "RESOURCE_CAP_FAILURE_BEFORE_RESULT"
ERROR_TYPE = "OutOfMemoryException"
CHANGE_CLASS = (
    "EQUIVALENT_DEDUP_HOUR_BUCKET_EXECUTION_SHAPE_AND_RESOURCE_HEADROOM_"
    "RETUNE_ONLY"
)
REGISTRATION_CHANGE_CLASS = (
    "EQUIVALENT_DEDUP_HOUR_BUCKET_EXECUTION_SHAPE_AND_RESOURCE_HEADROOM_"
    "RETUNE_ONLY_NO_DATA_PARSER_QUERY_SEMANTICS_SELECTION_QUARANTINE_OR_"
    "HYPOTHESIS_CHANGE"
)
RETRY_REQUIREMENT = (
    "FRESH_SCRATCH_SAME_SELECTION_PARSER_HYPOTHESES_EQUIVALENT_HOUR_BUCKET_"
    "DEDUP_RETUNED_TEMP_DISK_NO_RESUME"
)
PRESERVED_SCRATCH_POLICY = (
    "FAILED_SCRATCH_SHA256_VERIFIED_ATOMIC_RENAME_PRESERVED_FRESH_ACTIVE_"
    "SCRATCH_REQUIRED_NO_RESUME"
)
EXECUTION_SHAPE_CHANGE = (
    "GLOBAL_DEDUP_WINDOW_TO_EXACT_KEY_DERIVED_HOUR_BUCKETS_AND_MVE_PAYLOAD_"
    "VERTICALIZATION_ONLY"
)
RESOURCE_CONTRACT_CHANGE = (
    "TEMP_CAP_70GB_TO_20GB_MIN_FREE_100GIB_TO_80GIB_ONLY"
)
AUTHORITY_CLASS = (
    "MISSION_AUTHORIZED_EQUIVALENT_RESEARCH_EXECUTION_REPAIR_ON_EXISTING_W09"
)
RUNTIME = {
    "memory_limit": "46GB",
    "max_temp_size": "20GB",
    "threads": 4,
    "min_free_gib": 80.0,
    "clob_max_per_root": 50,
    "resume": False,
    "keep_scratch": False,
}
PREVIOUS_RUNTIME = {
    "memory_limit": "46GB",
    "max_temp_size": "70GB",
    "threads": 4,
    "min_free_gib": 100.0,
    "clob_max_per_root": 50,
    "resume": False,
    "keep_scratch": False,
}
SUCCESS_RESOURCE = {
    "label": "rfq_full_stage_repair07",
    "path": "logs/resources/rfq_full_stage_repair07.json",
}
TRIAL_IDS = (
    "RFQ_FULL_STAGE_REPAIR06_ATTEMPT_07",
    "RFQ_EQUIVALENT_BUCKETED_DEDUP_RESOURCE_REPAIR_07",
)
PREFLIGHT_FLAGS = [
    "--validate-run-preflight-only",
    "--validate-run-preflight-overlay",
]

BUCKET_COUNTS = [
    {"exchange_hour_epoch_us": 1783836000000000, "valid_contract_rows": 800555},
    {"exchange_hour_epoch_us": 1783839600000000, "valid_contract_rows": 952945},
    {"exchange_hour_epoch_us": 1783843200000000, "valid_contract_rows": 804134},
    {"exchange_hour_epoch_us": 1783846800000000, "valid_contract_rows": 637691},
    {"exchange_hour_epoch_us": 1783850400000000, "valid_contract_rows": 601188},
    {"exchange_hour_epoch_us": 1783854000000000, "valid_contract_rows": 598842},
    {"exchange_hour_epoch_us": 1783857600000000, "valid_contract_rows": 764651},
    {"exchange_hour_epoch_us": 1783861200000000, "valid_contract_rows": 760095},
    {"exchange_hour_epoch_us": 1783864800000000, "valid_contract_rows": 1444757},
    {"exchange_hour_epoch_us": 1783868400000000, "valid_contract_rows": 1912187},
    {"exchange_hour_epoch_us": 1783872000000000, "valid_contract_rows": 2445931},
    {"exchange_hour_epoch_us": 1783875600000000, "valid_contract_rows": 2982434},
    {"exchange_hour_epoch_us": 1783879200000000, "valid_contract_rows": 2841151},
    {"exchange_hour_epoch_us": 1783882800000000, "valid_contract_rows": 2581373},
    {"exchange_hour_epoch_us": 1783886400000000, "valid_contract_rows": 2576496},
    {"exchange_hour_epoch_us": 1783890000000000, "valid_contract_rows": 1987054},
    {"exchange_hour_epoch_us": 1783893600000000, "valid_contract_rows": 2594551},
    {"exchange_hour_epoch_us": 1783897200000000, "valid_contract_rows": 1575841},
    {"exchange_hour_epoch_us": 1783900800000000, "valid_contract_rows": 1493462},
    {"exchange_hour_epoch_us": 1783904400000000, "valid_contract_rows": 1017566},
    {"exchange_hour_epoch_us": 1783908000000000, "valid_contract_rows": 1372352},
    {"exchange_hour_epoch_us": 1783911600000000, "valid_contract_rows": 1207232},
    {"exchange_hour_epoch_us": 1783915200000000, "valid_contract_rows": 1109875},
    {"exchange_hour_epoch_us": 1783918800000000, "valid_contract_rows": 964806},
    {"exchange_hour_epoch_us": 1783922400000000, "valid_contract_rows": 822062},
    {"exchange_hour_epoch_us": 1783926000000000, "valid_contract_rows": 710483},
    {"exchange_hour_epoch_us": 1783929600000000, "valid_contract_rows": 542489},
    {"exchange_hour_epoch_us": 1783933200000000, "valid_contract_rows": 491886},
    {"exchange_hour_epoch_us": 1783936800000000, "valid_contract_rows": 505956},
    {"exchange_hour_epoch_us": 1783940400000000, "valid_contract_rows": 533704},
    {"exchange_hour_epoch_us": 1783944000000000, "valid_contract_rows": 636011},
    {"exchange_hour_epoch_us": 1783947600000000, "valid_contract_rows": 819721},
    {"exchange_hour_epoch_us": 1783951200000000, "valid_contract_rows": 1034423},
    {"exchange_hour_epoch_us": 1783954800000000, "valid_contract_rows": 1214545},
    {"exchange_hour_epoch_us": 1783958400000000, "valid_contract_rows": 1273524},
    {"exchange_hour_epoch_us": 1783962000000000, "valid_contract_rows": 1495017},
    {"exchange_hour_epoch_us": 1783965600000000, "valid_contract_rows": 1559214},
    {"exchange_hour_epoch_us": 1783969200000000, "valid_contract_rows": 1181471},
    {"exchange_hour_epoch_us": 1783972800000000, "valid_contract_rows": 1081101},
    {"exchange_hour_epoch_us": 1783976400000000, "valid_contract_rows": 1662110},
    {"exchange_hour_epoch_us": 1783980000000000, "valid_contract_rows": 1809495},
    {"exchange_hour_epoch_us": 1783983600000000, "valid_contract_rows": 545560},
    {"exchange_hour_epoch_us": 1783987200000000, "valid_contract_rows": 1789830},
    {"exchange_hour_epoch_us": 1783990800000000, "valid_contract_rows": 2406834},
]


class Repair07RegistrationError(repair06.Repair06RegistrationError):
    pass


def fail(message: str) -> None:
    raise Repair07RegistrationError(message)


def _transaction_fault_hook(boundary: str) -> None:
    del boundary


def _forward_fault(boundary: str) -> None:
    _transaction_fault_hook(boundary)


# Parameterize the previously audited CAS/registry/recovery machinery.
repair05.REPAIR_ID = REPAIR_ID
repair05.JOURNAL_SCHEMA = JOURNAL_SCHEMA
repair05.REGISTRY_APPEND_PROGRESS_SCHEMA = REGISTRY_PROGRESS_SCHEMA
repair05.TRANSACTION_JOURNAL = TRANSACTION_JOURNAL
repair05.REGISTRY_APPEND_PROGRESS = REGISTRY_PROGRESS
repair05.POST_REPAIR_STATUS = POST_STATUS
repair05.REGISTRATION_STATE = REGISTRATION_STATE
repair05.fail = fail
repair05._transaction_fault_hook = _forward_fault
repair04.REPAIR_ID = REPAIR_ID
repair04.PARENT_EXECUTION_COMMIT = PARENT_EXECUTION_COMMIT
repair04.JOURNAL_SCHEMA = JOURNAL_SCHEMA
repair04.REGISTRY_APPEND_PROGRESS_SCHEMA = REGISTRY_PROGRESS_SCHEMA
repair04.TRANSACTION_JOURNAL = TRANSACTION_JOURNAL
repair04.REGISTRY_APPEND_PROGRESS = REGISTRY_PROGRESS
repair04.EXPECTED_REPAIR04_CHANGED_PATHS = EXPECTED_CHANGED_PATHS
repair04.fail = fail
repair03.fail = fail


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def expected_command(run_id: str) -> list[str]:
    remote = f"/srv/w09-research/runs/{run_id}"
    return [
        "/opt/w09/venv/bin/python", f"{remote}/queries/rfq_full_stage.py",
        "--run-dir", remote, "--cache-root", "/srv/w09-research/cache",
        "--memory-limit", "46GB", "--max-temp-size", "20GB",
        "--threads", "4", "--min-free-gib", "80",
        "--clob-max-per-root", "50",
    ]


def preflight_contract(run_id: str) -> dict[str, Any]:
    return {
        "cli_flags": copy.deepcopy(PREFLIGHT_FLAGS),
        "overlay_path": POST_ROOT,
        "expected_stdout": f"RFQ_VALIDATE_RUN_PREFLIGHT_COMPLETE run_id={run_id}",
        "read_only": True,
        "stop_before_input_discovery": True,
        "stop_before_repair_replay": True,
        "stop_before_duckdb_import": True,
        "stop_before_state_input_scratch_or_result_write": True,
    }


def _require_file(
    run_dir: Path, relative: str | Path, digest: str, label: str
) -> Path:
    path = base.checked_run_path(run_dir, relative, label)
    if path.is_symlink() or not path.is_file() or base.sha256(path) != digest:
        fail(f"{label} is missing, unsafe, or changed")
    return path


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"{label} is invalid JSON: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} is not an object")
    return value


def _bucket_payload(rows: list[dict[str, int]]) -> bytes:
    return json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_bucket_constants() -> None:
    if (
        len(BUCKET_COUNTS) != BUCKET_COUNT
        or sum(row["valid_contract_rows"] for row in BUCKET_COUNTS)
        != BUCKET_VALID_ROWS
        or max(row["valid_contract_rows"] for row in BUCKET_COUNTS)
        != BUCKET_MAX_ROWS
        or hashlib.sha256(_bucket_payload(BUCKET_COUNTS)).hexdigest()
        != BUCKET_COUNTS_SHA256
        or BUCKET_COUNTS != sorted(
            BUCKET_COUNTS, key=lambda row: row["exchange_hour_epoch_us"]
        )
    ):
        fail("repair-07 frozen hour-bucket inventory constants are inconsistent")


def _scratch_bucket_rows(path: Path) -> list[dict[str, int]]:
    try:
        import duckdb
    except ImportError as exc:
        fail(f"repair-07 requires DuckDB to inspect failed scratch: {exc}")
    connection = duckdb.connect(str(path), read_only=True)
    try:
        rows = connection.execute(
            "SELECT epoch_us(date_trunc('hour',exchange_ts)) AS "
            "exchange_hour_epoch_us,count(*)::BIGINT AS valid_contract_rows "
            "FROM rfq_scan_rows WHERE valid_contract GROUP BY 1 ORDER BY 1"
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "exchange_hour_epoch_us": int(hour),
            "valid_contract_rows": int(count),
        }
        for hour, count in rows
    ]


def validate_parent(
    run_dir: Path, manifest_raw: bytes
) -> tuple[dict[str, Any], dict[str, Any], bytes, dict[str, Any], list[dict[str, Any]]]:
    if base.sha256_bytes(manifest_raw) != PARENT_MANIFEST_SHA256:
        fail("RUN_MANIFEST is not the approved repair-06 boundary")
    manifest = _read_json(run_dir / "RUN_MANIFEST.json", "RUN_MANIFEST")
    records = manifest.get("data_integrity_repairs")
    if (
        manifest.get("run_id") != run_dir.name
        or manifest.get("status") != PRE_STATUS
        or manifest.get("registration_state") != PRE_REGISTRATION_STATE
        or not isinstance(records, list)
        or [row.get("repair_id") if isinstance(row, dict) else None for row in records]
        != ["repair-01", "repair-02", "repair-03", "repair-04", "repair-05", "repair-06"]
    ):
        fail("repair-06 manifest status/history mismatch")
    parent = records[-1]
    if (
        parent.get("schema_version")
        != "sports-autoresearch-validate-run-status-wiring-repair-v1"
        or parent.get("current_execution_commit") != PARENT_EXECUTION_COMMIT
        or parent.get("repair_receipt_sha256") != PARENT_RECEIPT_SHA256
        or parent.get("transaction_journal_sha256") != PARENT_JOURNAL_SHA256
        or parent.get("status_wiring_contract_sha256")
        != PARENT_STATUS_CONTRACT_SHA256
        or parent.get("authority_basis_sha256") != PARENT_AUTHORITY_SHA256
        or parent.get("resource_contract_sha256")
        != PARENT_RESOURCE_CONTRACT_SHA256
        or parent.get("registered_rfq_query_sha256") != PARENT_QUERY_SHA256
    ):
        fail("repair-06 committed record mismatch")
    receipt_path = _require_file(
        run_dir, parent["repair_receipt_path"], PARENT_RECEIPT_SHA256,
        "repair-06 registration receipt",
    )
    receipt = _read_json(receipt_path, "repair-06 registration receipt")
    expected_receipt = copy.deepcopy(parent)
    expected_receipt.pop("repair_receipt_path")
    expected_receipt.pop("repair_receipt_sha256")
    if receipt != expected_receipt:
        fail("repair-06 receipt does not reproduce its manifest record")
    journal_path = _require_file(
        run_dir, parent["transaction_journal_path"], PARENT_JOURNAL_SHA256,
        "repair-06 transaction journal",
    )
    journal = _read_json(journal_path, "repair-06 transaction journal")
    parent_dir = run_dir / "DATA_INTEGRITY/repairs/repair-06"
    if (
        journal.get("schema_version") != "repair06-registration-transaction-v1"
        or journal.get("repair_id") != "repair-06"
        or journal.get("run_id") != run_dir.name
        or repair05._safe_relative_file_set(parent_dir)
        != set(journal.get("expected_repair_files", []))
    ):
        fail("repair-06 committed transaction mismatch")
    _require_file(
        run_dir, parent["status_wiring_contract_path"],
        PARENT_STATUS_CONTRACT_SHA256, "repair-06 status contract",
    )
    _require_file(
        run_dir, parent["authority_basis_path"], PARENT_AUTHORITY_SHA256,
        "repair-06 authority",
    )
    repository = manifest.get("repository")
    if (
        not isinstance(repository, dict)
        or repository.get("execution_commit") != PARENT_EXECUTION_COMMIT
        or repository.get("registration_repair_id") != "repair-06"
        or repository.get("identity_history") != parent.get("repository_identity_chain")
        or len(repository.get("identity_history", [])) != 7
    ):
        fail("repair-06 repository identity mismatch")
    registry = _require_file(
        run_dir, "TRIAL_REGISTRY.jsonl", parent["trial_registry"]["current_sha256"],
        "repair-06 trial registry",
    ).read_bytes()
    core = base.core_result_inventory(run_dir)
    if core != parent.get("core_result_artifacts"):
        fail("repair-06 inherited core inventory changed")
    _, cycle = base.validate_cycle1_duckdb_binding(run_dir, run_dir / CYCLE_BINDING)
    if cycle != parent.get("cycle1_duckdb_binding"):
        fail("repair-06 Cycle-1 binding changed")
    for relative, digest, label in (
        (RESOURCE, FAILED_RESOURCE_SHA256, "attempt-07 resource"),
        (STATE, FAILED_STATE_SHA256, "attempt-07 state"),
        (INPUT_IDENTITY, FAILED_INPUT_SHA256, "attempt-07 input"),
    ):
        _require_file(run_dir, relative, digest, label)
    for result in (repair05.RFQ_SUMMARY, repair05.RFQ_REPORT, repair05.RFQ_CATALOG):
        if os.path.lexists(run_dir / result):
            fail("repair-07 cannot register after an RFQ result exists")
    return manifest, parent, registry, repository, core


def validate_failure(run_dir: Path) -> dict[str, Any]:
    resource = _read_json(run_dir / RESOURCE, "attempt-07 resource receipt")
    state = _read_json(run_dir / STATE, "attempt-07 failed state")
    identity = _read_json(run_dir / INPUT_IDENTITY, "attempt-07 input identity")
    expected_old_command = expected_command(run_dir.name)
    expected_old_command[expected_old_command.index("20GB")] = "70GB"
    expected_old_command[expected_old_command.index("80")] = "100"
    if (
        resource.get("schema_version") != "w09-stage-resource-v1"
        or resource.get("label") != "rfq_full_stage_repair06"
        or resource.get("return_code") != 1
        or resource.get("command") != expected_old_command
        or resource.get("wall_seconds") != 954.289
        or resource.get("peak_process_tree_rss_kib_polled") != 46_022_424
        or resource.get("peak_temp_bytes_polled") != 11_550_019_656
        or resource.get("minimum_disk_free_bytes_polled") != 83_262_455_808
    ):
        fail("attempt-07 resource receipt mismatch")
    if (
        state.get("status") != "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
        or state.get("error_type") != ERROR_TYPE
        or "could not allocate block of size 256.0 KiB" not in str(state.get("error"))
        or state.get("expected_success_resource")
        != {"label": "rfq_full_stage_repair06", "path": RESOURCE.as_posix()}
        or state.get("registered_rfq_query_sha256") != PARENT_QUERY_SHA256
        or state.get("input_fingerprint")
        != "8b310c37f3989770d1f53a058e5ef396e1eb5c9c3d24d07ac87bd9d8aee5b9e1"
    ):
        fail("attempt-07 failed state mismatch")
    if (
        identity.get("schema") != "rfq-full-input-identity-v6"
        or identity.get("registered_rfq_query_sha256") != PARENT_QUERY_SHA256
        or identity.get("consumed_unique_objects") != 282
        or identity.get("consumed_bytes") != 59_185_856_724
    ):
        fail("attempt-07 input identity mismatch")
    scratch = run_dir / SCRATCH
    preserved = run_dir / PRESERVED_SCRATCH
    if (
        scratch.is_symlink()
        or not scratch.is_file()
        or scratch.stat().st_size != FAILED_SCRATCH_BYTES
        or scratch.stat().st_mtime_ns != FAILED_SCRATCH_MTIME_NS
        or base.sha256(scratch) != FAILED_SCRATCH_SHA256
        or os.path.lexists(run_dir / SCRATCH_WAL)
        or os.path.lexists(preserved)
    ):
        fail("attempt-07 scratch/WAL preservation boundary mismatch")
    rows = _scratch_bucket_rows(scratch)
    if rows != BUCKET_COUNTS:
        fail("attempt-07 scratch hour-bucket inventory changed")
    return {"resource": resource, "state": state, "identity": identity, "buckets": rows}


def validate_source_provenance(
    run_dir: Path,
    source_dir: Path,
    manifest: dict[str, Any],
    old_repository: dict[str, Any],
) -> tuple[str, bytes, bytes, list[tuple[str, str, bytes]]]:
    repo_root, source_relative, commit = repair04._verify_clean_source_provenance(source_dir)
    repair04._validate_git_commit_scope(repo_root, commit)
    source_manifest, source_sums = repair04._validate_clean_head_source_binding(
        source_dir, repo_root, source_relative, commit
    )
    prior, query_names = repair04._verify_prior_repository_provenance(
        run_dir, source_dir, repo_root, source_relative, commit, manifest
    )
    if prior != old_repository:
        fail("repair-07 prior repository identity changed")
    rows: list[tuple[str, str, bytes]] = []
    for name in query_names:
        path = source_dir / name
        if path.is_symlink() or not path.is_file():
            fail(f"repair-07 query source is missing/unsafe: {name}")
        payload = path.read_bytes()
        rows.append((base.sha256_bytes(payload), f"queries/{name}", payload))
    return commit, source_manifest, source_sums, rows


def make_scratch_receipt(run_id: str, applied_at: str) -> dict[str, Any]:
    return {
        "schema_version": SCRATCH_RECEIPT_SCHEMA,
        "repair_id": REPAIR_ID,
        "run_id": run_id,
        "attempt_id": TRIAL_IDS[0],
        "recorded_at_utc": applied_at,
        "failure_phase": FAILURE_PHASE,
        "active_path_before_registration": SCRATCH.as_posix(),
        "preserved_path": PRESERVED_SCRATCH.as_posix(),
        "sha256": FAILED_SCRATCH_SHA256,
        "bytes": FAILED_SCRATCH_BYTES,
        "source_mtime_ns": FAILED_SCRATCH_MTIME_NS,
        "wal_path": SCRATCH_WAL.as_posix(),
        "wal_present": False,
        "preservation": "ATOMIC_RENAME_SAME_FILESYSTEM_NO_COPY_NO_DELETE",
        "catalog": {
            "valid_contract_rows": BUCKET_VALID_ROWS,
            "exchange_hour_bucket_count": BUCKET_COUNT,
            "max_exchange_hour_rows": BUCKET_MAX_ROWS,
            "bucket_counts_sha256": BUCKET_COUNTS_SHA256,
            "bucket_counts": copy.deepcopy(BUCKET_COUNTS),
        },
    }


def make_blocker(
    run_id: str, applied_at: str, scratch_receipt_sha256: str, state: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": BLOCKER_SCHEMA,
        "repair_id": REPAIR_ID,
        "run_id": run_id,
        "attempt_id": TRIAL_IDS[0],
        "recorded_at_utc": applied_at,
        "finding": FINDING,
        "failure_disposition": FAILURE_DISPOSITION,
        "failure_phase": FAILURE_PHASE,
        "error_type": ERROR_TYPE,
        "error": state["error"],
        "failed_resource_receipt_path": f"{PRE_ROOT}/{RESOURCE.as_posix()}",
        "failed_resource_receipt_sha256": FAILED_RESOURCE_SHA256,
        "failed_state_path": f"{PRE_ROOT}/{STATE.as_posix()}",
        "failed_state_sha256": FAILED_STATE_SHA256,
        "failed_input_identity_path": f"{PRE_ROOT}/{INPUT_IDENTITY.as_posix()}",
        "failed_input_identity_sha256": FAILED_INPUT_SHA256,
        "failed_scratch_receipt_path": f"{PREFIX}/{SCRATCH_RECEIPT_FILE}",
        "failed_scratch_receipt_sha256": scratch_receipt_sha256,
        "registered_rfq_query_sha256": PARENT_QUERY_SHA256,
        "valid_contract_rows": BUCKET_VALID_ROWS,
        "exchange_hour_bucket_count": BUCKET_COUNT,
        "max_exchange_hour_rows": BUCKET_MAX_ROWS,
        "bucket_counts_sha256": BUCKET_COUNTS_SHA256,
        "result_opened": False,
        "hypothesis_conclusion_opened": False,
        "next_required_action": RETRY_REQUIREMENT,
    }


def make_resource_contract(
    run_id: str,
    applied_at: str,
    scratch_receipt_sha256: str,
    blocker_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_SCHEMA,
        "repair_id": REPAIR_ID,
        "run_id": run_id,
        "created_at_utc": applied_at,
        "finding": FINDING,
        "failure_disposition": FAILURE_DISPOSITION,
        "failure_phase": FAILURE_PHASE,
        "failure_evidence": {
            "resource_path": f"{PRE_ROOT}/{RESOURCE.as_posix()}",
            "resource_sha256": FAILED_RESOURCE_SHA256,
            "state_path": f"{PRE_ROOT}/{STATE.as_posix()}",
            "state_sha256": FAILED_STATE_SHA256,
            "input_identity_path": f"{PRE_ROOT}/{INPUT_IDENTITY.as_posix()}",
            "input_identity_sha256": FAILED_INPUT_SHA256,
            "scratch_receipt_path": f"{PREFIX}/{SCRATCH_RECEIPT_FILE}",
            "scratch_receipt_sha256": scratch_receipt_sha256,
            "blocker_path": f"{PREFIX}/{BLOCKER_FILE}",
            "blocker_sha256": blocker_sha256,
        },
        "dedup_equivalence": {
            "original_shape": "ONE_GLOBAL_ROW_NUMBER_WINDOW_OVER_ALL_VALID_CONTRACT_ROWS",
            "repaired_shape": (
                "SEQUENTIAL_INSERT_OF_EXACT_EXCHANGE_HOUR_BUCKET_WINDOWS_THEN_"
                "ONE_TO_ONE_EVENT_KEY_MVE_PAYLOAD_VERTICALIZATION"
            ),
            "partition_by": [
                "event_type",
                "rfq_id",
                "coalesce(created_ts_text,deleted_ts_text)",
                "market_ticker",
            ],
            "order_by": [
                "recv_wall_ns",
                "recv_mono_ns NULLS LAST",
                "filename",
            ],
            "bucket_expression": "epoch_us(date_trunc('hour',exchange_ts))",
            "proof": (
                "exchange_ts is the validated parse of exchange_ts_text; equal dedup "
                "keys therefore imply equal exchange-hour buckets, so no row_number "
                "partition crosses a bucket and the per-partition ordering is unchanged"
            ),
            "valid_contract_rows": BUCKET_VALID_ROWS,
            "exchange_hour_bucket_count": BUCKET_COUNT,
            "max_exchange_hour_rows": BUCKET_MAX_ROWS,
            "bucket_counts_sha256": BUCKET_COUNTS_SHA256,
            "bucket_counts": copy.deepcopy(BUCKET_COUNTS),
            "bucket_inventory_canonicalization": (
                "json.dumps(rows,sort_keys=True,separators=(',',':')).encode('utf-8');"
                "no trailing newline"
            ),
            "runtime_requires_exact_bucket_inventory": True,
            "payload_verticalization": {
                "payload_table": "rfq_mve_payload",
                "payload_columns": ["event_key", "mve_legs_json", "mve_legs_type"],
                "narrow_source": "rfq_events_valid",
                "narrow_excluded_columns": ["mve_legs_json", "mve_legs_type"],
                "join_key": "event_key",
                "join_site": "rfq_legs_base",
                "one_to_one_guards": [
                    "narrow_rows_equals_dedup_winner_rows",
                    "payload_rows_equals_created_winner_rows",
                    "payload_event_key_distinct_equals_payload_rows",
                    "rfq_creates_rows_equals_payload_rows",
                ],
                "payload_dropped_after_legs_complete": True,
                "proof": (
                    "the side table carries each created winner's unchanged MVE payload "
                    "under its unchanged unique event_key and rejoins only for leg expansion"
                ),
            },
            "output_columns_unchanged": True,
            "event_key_formula_unchanged": True,
            "query_semantics_change": "NONE",
            "execution_shape_change": EXECUTION_SHAPE_CHANGE,
        },
        "previous_runtime": copy.deepcopy(PREVIOUS_RUNTIME),
        "current_runtime": copy.deepcopy(RUNTIME),
        "expected_command": expected_command(run_id),
        "expected_success_resource": copy.deepcopy(SUCCESS_RESOURCE),
        "fresh_scratch": True,
        "resume": False,
        "preserved_scratch_policy": PRESERVED_SCRATCH_POLICY,
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": RESOURCE_CONTRACT_CHANGE,
        "execution_shape_change": EXECUTION_SHAPE_CHANGE,
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
    }


def make_authority(
    run_id: str, applied_at: str, contract_sha256: str
) -> dict[str, Any]:
    return {
        "schema_version": AUTHORITY_SCHEMA,
        "repair_id": REPAIR_ID,
        "run_id": run_id,
        "created_at_utc": applied_at,
        "authority_class": AUTHORITY_CLASS,
        "mission_sha256": MISSION_SHA256,
        "permitted_change": CHANGE_CLASS,
        "resource_contract_path": f"{PREFIX}/{CONTRACT_FILE}",
        "resource_contract_sha256": contract_sha256,
        "existing_instance_only": True,
        "new_instance_or_s3_write_authorized": False,
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": RESOURCE_CONTRACT_CHANGE,
        "execution_shape_change": EXECUTION_SHAPE_CHANGE,
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
    }


def _resource_binding(contract_sha256: str) -> dict[str, Any]:
    return {
        "path": f"{PREFIX}/{CONTRACT_FILE}",
        "sha256": contract_sha256,
        "schema_version": CONTRACT_SCHEMA,
        "current_runtime": copy.deepcopy(RUNTIME),
    }


def make_trial_records(
    *,
    applied_at: str,
    parent: dict[str, Any],
    current: dict[str, Any],
    resource: dict[str, Any],
    scratch_receipt_sha256: str,
    blocker_sha256: str,
    contract_sha256: str,
    authority_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    common = {
        "trial_ids": list(base.RFQ_TRIAL_IDS),
        "recorded_at_utc": applied_at,
        "execution_commit": current["execution_commit"],
        "source_manifest_sha256": current["source_manifest_sha256"],
        "source_sha256s_sha256": current["source_sha256s_sha256"],
        "query_set_sha256": current["query_set_sha256"],
        "parent_repair_id": PARENT_REPAIR_ID,
        "result_opened": False,
        "hypothesis_conclusion_opened": False,
    }
    failure = {
        **common,
        "trial_registration_id": TRIAL_IDS[0],
        "record_type": "STAGE_FAILURE",
        "stage": "RFQ_FULL_STAGE_REPAIR06",
        "failure_class": FINDING,
        "failure_disposition": FAILURE_DISPOSITION,
        "failure_phase": FAILURE_PHASE,
        "error": resource.get("return_code") and "DUCKDB_OUT_OF_MEMORY",
        "error_type": ERROR_TYPE,
        "failed_resource_receipt_path": f"{PRE_ROOT}/{RESOURCE.as_posix()}",
        "failed_resource_receipt_sha256": FAILED_RESOURCE_SHA256,
        "failed_state_path": f"{PRE_ROOT}/{STATE.as_posix()}",
        "failed_state_sha256": FAILED_STATE_SHA256,
        "failed_input_identity_path": f"{PRE_ROOT}/{INPUT_IDENTITY.as_posix()}",
        "failed_input_identity_sha256": FAILED_INPUT_SHA256,
        "failed_scratch_receipt_path": f"{PREFIX}/{SCRATCH_RECEIPT_FILE}",
        "failed_scratch_receipt_sha256": scratch_receipt_sha256,
        "blocker_path": f"{PREFIX}/{BLOCKER_FILE}",
        "blocker_sha256": blocker_sha256,
        "resource_label": "rfq_full_stage_repair06",
        "return_code": 1,
        "resource_metrics": {
            key: resource[key]
            for key in (
                "started_at_utc", "completed_at_utc", "wall_seconds",
                "cpu_system_seconds", "cpu_user_seconds",
                "peak_process_tree_rss_kib_polled", "peak_temp_bytes_polled",
                "minimum_disk_free_bytes_polled", "disk_free_before_bytes",
                "disk_free_after_bytes",
            )
        },
        "hypothesis_conclusion": "NONE",
    }
    preregistration = {
        **common,
        "trial_registration_id": TRIAL_IDS[1],
        "record_type": "EQUIVALENT_BUCKETED_DEDUP_RESOURCE_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR07_PREREGISTRATION",
        "finding": FINDING,
        "registration_change_class": REGISTRATION_CHANGE_CLASS,
        "retry_requirement": RETRY_REQUIREMENT,
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "expected_success_resource": copy.deepcopy(SUCCESS_RESOURCE),
        "resource_contract_path": f"{PREFIX}/{CONTRACT_FILE}",
        "resource_contract_sha256": contract_sha256,
        "authority_basis_path": f"{PREFIX}/{AUTHORITY_FILE}",
        "authority_basis_sha256": authority_sha256,
        "runtime": copy.deepcopy(RUNTIME),
        "coverage": copy.deepcopy(parent["coverage"]),
        "preserved_scratch_policy": PRESERVED_SCRATCH_POLICY,
        "bucket_counts_sha256": BUCKET_COUNTS_SHA256,
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": RESOURCE_CONTRACT_CHANGE,
        "execution_shape_change": EXECUTION_SHAPE_CHANGE,
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "core_results_recomputed": False,
    }
    return failure, preregistration


def make_record(
    *,
    applied_at: str,
    parent: dict[str, Any],
    old_repository: dict[str, Any],
    current: dict[str, Any],
    identity_chain: list[dict[str, Any]],
    core: list[dict[str, Any]],
    trial_before: bytes,
    trial_after: bytes,
    archive_rows: list[dict[str, Any]],
    journal_sha256: str,
    scratch_receipt_sha256: str,
    blocker_sha256: str,
    contract_sha256: str,
    authority_sha256: str,
) -> dict[str, Any]:
    failed_attempt = {
        "attempt_id": TRIAL_IDS[0],
        "error_type": ERROR_TYPE,
        "failure_phase": FAILURE_PHASE,
        "finding": FINDING,
        "resource_label": "rfq_full_stage_repair06",
        "return_code": 1,
        "resource_command": [
            value if value not in {"20GB", "80"} else {"20GB": "70GB", "80": "100"}[value]
            for value in expected_command(current["run_id"])
        ],
        "failed_resource_receipt_path": f"{PRE_ROOT}/{RESOURCE.as_posix()}",
        "failed_resource_receipt_sha256": FAILED_RESOURCE_SHA256,
        "failed_state_path": f"{PRE_ROOT}/{STATE.as_posix()}",
        "failed_state_sha256": FAILED_STATE_SHA256,
        "failed_input_identity_path": f"{PRE_ROOT}/{INPUT_IDENTITY.as_posix()}",
        "failed_input_identity_sha256": FAILED_INPUT_SHA256,
        "failed_scratch_receipt_path": f"{PREFIX}/{SCRATCH_RECEIPT_FILE}",
        "failed_scratch_receipt_sha256": scratch_receipt_sha256,
        "preserved_scratch_path": PRESERVED_SCRATCH.as_posix(),
        "preserved_scratch_sha256": FAILED_SCRATCH_SHA256,
        "preserved_scratch_bytes": FAILED_SCRATCH_BYTES,
        "blocker_path": f"{PREFIX}/{BLOCKER_FILE}",
        "blocker_sha256": blocker_sha256,
    }
    binding = _resource_binding(contract_sha256)
    current_identity = {
        key: copy.deepcopy(current[key])
        for key in (
            "execution_commit", "source_manifest_sha256",
            "source_sha256s_sha256", "query_set_sha256", "query_files",
        )
    }
    return {
        "schema_version": REPAIR_SCHEMA,
        "repair_id": REPAIR_ID,
        "parent_repair_id": PARENT_REPAIR_ID,
        "applied_at_utc": applied_at,
        "pre_repair_status": PRE_STATUS,
        "post_repair_status": POST_STATUS,
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "retry_requirement": RETRY_REQUIREMENT,
        "registration_change_class": REGISTRATION_CHANGE_CLASS,
        "finding": FINDING,
        "failure_disposition": FAILURE_DISPOSITION,
        "failure_phase": FAILURE_PHASE,
        "failed_attempt": failed_attempt,
        "blocker_path": f"{PREFIX}/{BLOCKER_FILE}",
        "blocker_sha256": blocker_sha256,
        "failed_resource_receipt_path": f"{PRE_ROOT}/{RESOURCE.as_posix()}",
        "failed_resource_receipt_sha256": FAILED_RESOURCE_SHA256,
        "failed_state_path": f"{PRE_ROOT}/{STATE.as_posix()}",
        "failed_state_sha256": FAILED_STATE_SHA256,
        "failed_input_identity_path": f"{PRE_ROOT}/{INPUT_IDENTITY.as_posix()}",
        "failed_input_identity_sha256": FAILED_INPUT_SHA256,
        "failed_scratch_receipt_path": f"{PREFIX}/{SCRATCH_RECEIPT_FILE}",
        "failed_scratch_receipt_sha256": scratch_receipt_sha256,
        "preserved_scratch_path": PRESERVED_SCRATCH.as_posix(),
        "preserved_scratch_sha256": FAILED_SCRATCH_SHA256,
        "preserved_scratch_bytes": FAILED_SCRATCH_BYTES,
        "parent_manifest_sha256": PARENT_MANIFEST_SHA256,
        "parent_repair_registration_path": parent["repair_receipt_path"],
        "parent_repair_registration_sha256": PARENT_RECEIPT_SHA256,
        "parent_transaction_journal_path": parent["transaction_journal_path"],
        "parent_transaction_journal_sha256": PARENT_JOURNAL_SHA256,
        "parent_status_wiring_contract_path": parent["status_wiring_contract_path"],
        "parent_status_wiring_contract_sha256": PARENT_STATUS_CONTRACT_SHA256,
        "parent_authority_basis_path": parent["authority_basis_path"],
        "parent_authority_basis_sha256": PARENT_AUTHORITY_SHA256,
        "parent_resource_contract_path": parent["resource_contract_path"],
        "parent_resource_contract_sha256": PARENT_RESOURCE_CONTRACT_SHA256,
        "previous_repair_record_sha256": canonical_hash(parent),
        "resource_contract": binding,
        "resource_contract_path": binding["path"],
        "resource_contract_sha256": binding["sha256"],
        "authority_basis": {
            "path": f"{PREFIX}/{AUTHORITY_FILE}",
            "sha256": authority_sha256,
            "schema_version": AUTHORITY_SCHEMA,
            "authority_class": AUTHORITY_CLASS,
            "permitted_change": CHANGE_CLASS,
        },
        "authority_basis_path": f"{PREFIX}/{AUTHORITY_FILE}",
        "authority_basis_sha256": authority_sha256,
        "coverage": copy.deepcopy(parent["coverage"]),
        "cumulative_quarantined_objects": copy.deepcopy(
            parent["cumulative_quarantined_objects"]
        ),
        "core_result_artifacts": copy.deepcopy(core),
        "core_result_disposition": "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED",
        "core_results_recomputed": False,
        "parser_contract": copy.deepcopy(parent["parser_contract"]),
        "parser_contract_path": parent["parser_contract_path"],
        "parser_contract_sha256": parent["parser_contract_sha256"],
        "consumer_wiring_contract_path": parent["parent_wiring_contract_path"],
        "consumer_wiring_contract_sha256": parent["parent_wiring_contract_sha256"],
        "status_wiring_contract_path": parent["status_wiring_contract_path"],
        "status_wiring_contract_sha256": parent["status_wiring_contract_sha256"],
        "cycle1_duckdb_binding": copy.deepcopy(parent["cycle1_duckdb_binding"]),
        "cycle1_binding_path": f"{PRE_ROOT}/{CYCLE_BINDING.as_posix()}",
        "cycle1_binding_sha256": parent["cycle1_duckdb_binding"]["active_sha256"],
        "expected_success_resource": copy.deepcopy(SUCCESS_RESOURCE),
        "registered_rfq_query_sha256": current["registered_rfq_query_sha256"],
        "previous_execution_commit": old_repository["execution_commit"],
        "current_execution_commit": current["execution_commit"],
        "previous_repository_identity": base.repository_identity(old_repository),
        "current_repository_identity": current_identity,
        "repository_identity_chain": copy.deepcopy(identity_chain),
        "source_verification_mode": "LOCAL_GIT_CLEAN_COMMITTED_HEAD",
        "preserved_scratch_policy": PRESERVED_SCRATCH_POLICY,
        "main_preflight": preflight_contract(current["run_id"]),
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": RESOURCE_CONTRACT_CHANGE,
        "execution_shape_change": EXECUTION_SHAPE_CHANGE,
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "archive_path": PRE_ROOT,
        "archive_inventory": copy.deepcopy(archive_rows),
        "trial_registry": {
            "previous_sha256": base.sha256_bytes(trial_before),
            "current_sha256": base.sha256_bytes(trial_after),
            "previous_bytes": len(trial_before),
            "current_bytes": len(trial_after),
            "strict_previous_bytes_prefix": True,
            "appended_records": 2,
            "trial_registration_ids": list(TRIAL_IDS),
        },
        "transaction_journal_path": f"{PREFIX}/{TRANSACTION_JOURNAL}",
        "transaction_journal_sha256": journal_sha256,
    }


def _archive_payloads(
    run_dir: Path,
    manifest_raw: bytes,
    trial: bytes,
    repository: dict[str, Any],
) -> tuple[list[Path], dict[Path, bytes]]:
    fixed = [
        Path("RUN_MANIFEST.json"), Path("TRIAL_REGISTRY.jsonl"),
        Path("SOURCE_MANIFEST.json"), Path("SOURCE_SHA256SUMS.txt"),
        Path("QUERY_SHA256SUMS.txt"), RESOURCE, STATE, INPUT_IDENTITY,
        CYCLE_BINDING, W09_ATTESTATION,
    ]
    paths = fixed + [Path(value) for value in repository["query_files"]]
    expected = {
        Path("RUN_MANIFEST.json"): base.sha256_bytes(manifest_raw),
        Path("TRIAL_REGISTRY.jsonl"): base.sha256_bytes(trial),
        Path("SOURCE_MANIFEST.json"): repository["source_manifest_sha256"],
        Path("SOURCE_SHA256SUMS.txt"): repository["source_sha256s_sha256"],
        Path("QUERY_SHA256SUMS.txt"): repository["query_set_sha256"],
        RESOURCE: FAILED_RESOURCE_SHA256,
        STATE: FAILED_STATE_SHA256,
        INPUT_IDENTITY: FAILED_INPUT_SHA256,
    }
    for digest, relative in base.parse_checksums(
        run_dir / "QUERY_SHA256SUMS.txt", "repair-07 query receipt"
    ):
        expected[Path(relative)] = digest
    payloads: dict[Path, bytes] = {}
    for relative in paths:
        path = run_dir / relative
        if path.is_symlink() or not path.is_file():
            fail(f"repair-07 archive source missing/unsafe: {relative}")
        payload = path.read_bytes()
        if relative in expected and base.sha256_bytes(payload) != expected[relative]:
            fail(f"repair-07 archive source changed: {relative}")
        payloads[relative] = payload
    if payloads[Path("RUN_MANIFEST.json")] != manifest_raw:
        fail("repair-07 manifest raced archive capture")
    if payloads[Path("TRIAL_REGISTRY.jsonl")] != trial:
        fail("repair-07 registry raced archive capture")
    return paths, payloads


def _resolve_replacement(
    run_dir: Path, repair_dir: Path, mutation: dict[str, Any], label: str
) -> Path:
    relative = mutation.get("replacement_staging_path")
    if not isinstance(relative, str):
        fail(f"{label} replacement path missing")
    path = base.checked_run_path(run_dir, relative, label)
    try:
        path.resolve().relative_to(repair_dir.resolve())
    except ValueError:
        fail(f"{label} replacement escapes repair-07")
    if path.is_symlink() or not path.is_file():
        fail(f"{label} replacement missing/unsafe")
    digest = mutation.get("replacement_sha256")
    if digest is not None:
        if base.sha256(path) != base.require_sha(digest, f"{label} SHA"):
            fail(f"{label} replacement hash mismatch")
        return path
    if mutation.get("path") != "RUN_MANIFEST.json":
        fail(f"{label} lacks replacement hash")
    manifest = _read_json(path, f"{label} manifest")
    records = manifest.get("data_integrity_repairs")
    if (
        manifest.get("run_id") != run_dir.name
        or manifest.get("status") != POST_STATUS
        or manifest.get("registration_state") != REGISTRATION_STATE
        or not isinstance(records, list)
        or len(records) != 7
        or records[-1].get("repair_id") != REPAIR_ID
        or records[-1].get("transaction_journal_sha256")
        != base.sha256(repair_dir / TRANSACTION_JOURNAL)
        or records[-1].get("repair_receipt_sha256")
        != base.sha256(repair_dir / REGISTRATION_FILE)
    ):
        fail(f"{label} is not the self-bound repair-07 manifest")
    return path


repair05._resolve_durable_replacement = _resolve_replacement


def _restore_scratch_before_transaction_recovery(
    run_dir: Path, repair_dir: Path, *, restore_mode: int | None = None
) -> None:
    active = run_dir / SCRATCH
    preserved = run_dir / PRESERVED_SCRATCH
    if os.path.lexists(active) and os.path.lexists(preserved):
        fail("repair-07 recovery found both active and preserved scratch")
    if os.path.lexists(preserved):
        if (
            preserved.is_symlink()
            or not preserved.is_file()
            or preserved.stat().st_size != FAILED_SCRATCH_BYTES
            or preserved.stat().st_mtime_ns != FAILED_SCRATCH_MTIME_NS
            or base.sha256(preserved) != FAILED_SCRATCH_SHA256
        ):
            fail("repair-07 preserved scratch changed before recovery")
        if restore_mode is not None:
            os.chmod(preserved, restore_mode, follow_symlinks=False)
        os.replace(preserved, active)
        repair03._fsync_directory(active.parent)
    elif not os.path.lexists(active):
        fail("repair-07 recovery lost both scratch names")
    repair05.recover_incomplete_transaction(run_dir, repair_dir)


def _assert_preserved_scratch_boundary(
    run_dir: Path, *, full_hash: bool
) -> None:
    """Fail closed if the transaction-owned failed scratch identity changes."""
    active = run_dir / SCRATCH
    wal = run_dir / SCRATCH_WAL
    preserved = run_dir / PRESERVED_SCRATCH
    if os.path.lexists(active) or os.path.lexists(wal):
        fail("repair-07 active scratch/WAL reappeared during transaction")
    try:
        before = os.lstat(preserved)
    except FileNotFoundError:
        fail("repair-07 preserved scratch disappeared during transaction")
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size != FAILED_SCRATCH_BYTES
        or before.st_mtime_ns != FAILED_SCRATCH_MTIME_NS
    ):
        fail("repair-07 preserved scratch metadata identity changed")
    if full_hash and base.sha256(preserved) != FAILED_SCRATCH_SHA256:
        fail("repair-07 preserved scratch content identity changed")
    try:
        after = os.lstat(preserved)
    except FileNotFoundError:
        fail("repair-07 preserved scratch disappeared during verification")
    if (
        stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        or os.path.lexists(active)
        or os.path.lexists(wal)
    ):
        fail("repair-07 preserved scratch raced boundary verification")


def _run_main_preflight(run_dir: Path, source_dir: Path, overlay: Path) -> None:
    expected = f"RFQ_VALIDATE_RUN_PREFLIGHT_COMPLETE run_id={run_dir.name}"
    command = [
        sys.executable, str(source_dir / "rfq_full_stage.py"),
        "--run-dir", str(run_dir), "--validate-run-preflight-only",
        "--validate-run-preflight-overlay", str(overlay),
    ]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        command, cwd=source_dir, env=environment, capture_output=True,
        text=True, timeout=180, check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != expected:
        fail(
            "repair-07 RFQ main preflight failed: "
            f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
        )


def _cleanup_committed_displaced(run_dir: Path, repair_dir: Path) -> None:
    journal = _read_json(repair_dir / TRANSACTION_JOURNAL, "repair-07 journal")
    rows = [*journal.get("active_mutations", []), journal.get("manifest_mutation")]
    for row in rows:
        if not isinstance(row, dict):
            fail("repair-07 committed journal mutation is invalid")
        displaced = base.checked_run_path(
            run_dir, row["displaced_path"], "repair-07 displaced cleanup"
        )
        if not os.path.lexists(displaced):
            continue
        active = base.checked_run_path(run_dir, row["path"], "repair-07 active cleanup")
        original = _require_file(
            run_dir, row["original_archive_path"], row["original_sha256"],
            "repair-07 cleanup original",
        )
        replacement = _resolve_replacement(
            run_dir, repair_dir, row, "repair-07 cleanup replacement"
        )
        if (
            displaced.is_symlink() or not displaced.is_file()
            or displaced.read_bytes() != original.read_bytes()
            or active.is_symlink() or active.read_bytes() != replacement.read_bytes()
        ):
            fail("repair-07 committed displaced cleanup mismatch")
        displaced.unlink()
        repair03._fsync_directory(displaced.parent)


def validate_already_applied(
    run_dir: Path,
    source_dir: Path,
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    if (
        manifest.get("status") != POST_STATUS
        or manifest.get("registration_state") != REGISTRATION_STATE
        or [row.get("repair_id") for row in records]
        != [
            "repair-01", "repair-02", "repair-03", "repair-04",
            "repair-05", "repair-06", "repair-07",
        ]
    ):
        fail("existing repair-07 history is inconsistent")
    record = records[-1]
    receipt = _read_json(
        _require_file(
            run_dir, record["repair_receipt_path"], record["repair_receipt_sha256"],
            "repair-07 receipt",
        ),
        "repair-07 receipt",
    )
    expected = copy.deepcopy(record)
    expected.pop("repair_receipt_path")
    expected.pop("repair_receipt_sha256")
    if receipt != expected:
        fail("repair-07 receipt/manifest mismatch")
    repair_dir = run_dir / PREFIX
    journal = _read_json(
        _require_file(
            run_dir, record["transaction_journal_path"],
            record["transaction_journal_sha256"], "repair-07 journal",
        ),
        "repair-07 journal",
    )
    if repair05._safe_relative_file_set(repair_dir) != set(
        journal["expected_repair_files"]
    ):
        fail("repair-07 committed file inventory changed")
    preserved = run_dir / PRESERVED_SCRATCH
    if (
        os.path.lexists(run_dir / SCRATCH)
        or os.path.lexists(run_dir / SCRATCH_WAL)
        or preserved.is_symlink()
        or not preserved.is_file()
        or preserved.stat().st_size != FAILED_SCRATCH_BYTES
        or base.sha256(preserved) != FAILED_SCRATCH_SHA256
    ):
        fail("repair-07 committed scratch boundary changed")
    repo_root, _, commit = repair04._verify_clean_source_provenance(source_dir)
    if commit != manifest["repository"]["execution_commit"]:
        fail("repair-07 idempotent source commit changed")
    del repo_root
    return {
        "status": "REGISTRATION_REPAIR07_ALREADY_REFROZEN",
        "repair_id": REPAIR_ID,
        "run_id": run_dir.name,
        "expected_success_resource": copy.deepcopy(SUCCESS_RESOURCE),
    }


def repair07_registration(run_dir: Path, source_dir: Path) -> dict[str, Any]:
    if Path(run_dir).is_symlink() or Path(source_dir).is_symlink():
        fail("repair-07 run/source directory must not be a symlink")
    run_dir = Path(run_dir).resolve()
    source_dir = Path(source_dir).resolve()
    with repair03._exclusive_run_lock(run_dir):
        return _repair07_locked(run_dir, source_dir)


def _repair07_locked(run_dir: Path, source_dir: Path) -> dict[str, Any]:
    _validate_bucket_constants()
    repair_dir = run_dir / PREFIX
    manifest_path = run_dir / "RUN_MANIFEST.json"
    if repair_dir.exists() and (
        not manifest_path.is_file()
        or _read_json(manifest_path, "RUN_MANIFEST").get("status") != POST_STATUS
    ):
        _restore_scratch_before_transaction_recovery(run_dir, repair_dir)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        fail("RUN_MANIFEST is missing/unsafe")
    manifest_raw = manifest_path.read_bytes()
    manifest = _read_json(manifest_path, "RUN_MANIFEST")
    records = manifest.get("data_integrity_repairs")
    if not isinstance(records, list):
        fail("RUN_MANIFEST repair history is invalid")
    if len(records) == 7:
        _cleanup_committed_displaced(run_dir, repair_dir)
        return validate_already_applied(run_dir, source_dir, manifest, records)
    if len(records) != 6 or repair_dir.exists():
        fail("repair-07 requires exactly the committed repair-06 boundary")

    manifest, parent, trial_before, old_repository, core = validate_parent(
        run_dir, manifest_raw
    )
    failure = validate_failure(run_dir)
    commit, source_manifest, source_sums, query_rows = validate_source_provenance(
        run_dir, source_dir, manifest, old_repository
    )
    query_sums = "".join(
        f"{digest}  {relative}\n" for digest, relative, _ in query_rows
    ).encode("utf-8")
    rfq_rows = [row for row in query_rows if row[1] == "queries/rfq_full_stage.py"]
    if len(rfq_rows) != 1:
        fail("repair-07 requires one RFQ execution query")
    repository_identity = {
        "execution_commit": commit,
        "source_manifest_sha256": base.sha256_bytes(source_manifest),
        "source_sha256s_sha256": base.sha256_bytes(source_sums),
        "query_set_sha256": base.sha256_bytes(query_sums),
        "query_files": copy.deepcopy(old_repository["query_files"]),
    }
    current = {
        **repository_identity,
        "registered_rfq_query_sha256": rfq_rows[0][0],
        "run_id": run_dir.name,
    }
    old_chain = old_repository.get("identity_history")
    if (
        not isinstance(old_chain, list)
        or len(old_chain) != 7
        or old_chain != parent.get("repository_identity_chain")
        or old_chain[-1] != base.repository_identity(old_repository)
    ):
        fail("repair-07 parent repository chain is not exact")
    identity_chain = copy.deepcopy(old_chain) + [repository_identity]
    archive_paths, archive_payloads = _archive_payloads(
        run_dir, manifest_raw, trial_before, old_repository
    )
    applied_at = base.now_utc()
    repairs_root = run_dir / "DATA_INTEGRITY/repairs"
    repairs_root.mkdir(parents=True, exist_ok=True)
    staging: Path | None = Path(
        tempfile.mkdtemp(prefix=".repair-07-", dir=repairs_root)
    )
    published = False
    scratch_preserved = False
    try:
        pre, post = staging / "pre_repair", staging / "post_repair"
        for relative in archive_paths:
            base.atomic_write(pre / relative, archive_payloads[relative])
        archive_rows = base.archive_inventory(pre)
        if any("scratch" in row["path"].lower() for row in archive_rows):
            fail("repair-07 archive must not copy failed scratch")
        base.atomic_write(post / "SOURCE_MANIFEST.json", source_manifest)
        base.atomic_write(post / "SOURCE_SHA256SUMS.txt", source_sums)
        base.atomic_write(post / "QUERY_SHA256SUMS.txt", query_sums)
        for _, relative, payload in query_rows:
            base.atomic_write(post / relative, payload)

        scratch_receipt = make_scratch_receipt(run_dir.name, applied_at)
        base.atomic_write(
            staging / SCRATCH_RECEIPT_FILE, base.json_payload(scratch_receipt)
        )
        scratch_receipt_sha = base.sha256(staging / SCRATCH_RECEIPT_FILE)
        blocker = make_blocker(
            run_dir.name, applied_at, scratch_receipt_sha, failure["state"]
        )
        base.atomic_write(staging / BLOCKER_FILE, base.json_payload(blocker))
        blocker_sha = base.sha256(staging / BLOCKER_FILE)
        contract = make_resource_contract(
            run_dir.name, applied_at, scratch_receipt_sha, blocker_sha
        )
        base.atomic_write(staging / CONTRACT_FILE, base.json_payload(contract))
        contract_sha = base.sha256(staging / CONTRACT_FILE)
        authority = make_authority(run_dir.name, applied_at, contract_sha)
        base.atomic_write(staging / AUTHORITY_FILE, base.json_payload(authority))
        authority_sha = base.sha256(staging / AUTHORITY_FILE)
        failure_trial, prereg_trial = make_trial_records(
            applied_at=applied_at, parent=parent, current=current,
            resource=failure["resource"],
            scratch_receipt_sha256=scratch_receipt_sha,
            blocker_sha256=blocker_sha, contract_sha256=contract_sha,
            authority_sha256=authority_sha,
        )
        trial_append = base.registry_line(failure_trial) + base.registry_line(prereg_trial)
        trial_after = trial_before + trial_append
        base.atomic_write(post / "TRIAL_REGISTRY.jsonl", trial_after)
        base.atomic_write(
            staging / REGISTRY_PROGRESS,
            repair05._registry_progress_payload(
                run_dir.name, trial_before, trial_append, "NOT_STARTED", 0
            ),
        )

        mutation_relatives = [
            "SOURCE_MANIFEST.json", "SOURCE_SHA256SUMS.txt",
            "QUERY_SHA256SUMS.txt", *old_repository["query_files"],
            "TRIAL_REGISTRY.jsonl",
        ]
        mutations = [
            {
                "path": relative,
                "original_archive_path": f"{PRE_ROOT}/{relative}",
                "original_sha256": base.sha256(pre / relative),
                "replacement_staging_path": f"{POST_ROOT}/{relative}",
                "replacement_sha256": base.sha256(post / relative),
                "displaced_path": (
                    f"{PREFIX}/cas_displaced/{relative.replace('/', '__')}.original"
                ),
                "append_only_registry": relative == "TRIAL_REGISTRY.jsonl",
            }
            for relative in mutation_relatives
        ]
        manifest_mutation = {
            "path": "RUN_MANIFEST.json",
            "original_archive_path": f"{PRE_ROOT}/RUN_MANIFEST.json",
            "original_sha256": base.sha256_bytes(manifest_raw),
            "replacement_staging_path": f"{POST_ROOT}/RUN_MANIFEST.json",
            "replacement_sha256": None,
            "displaced_path": f"{PREFIX}/cas_displaced/RUN_MANIFEST.json.original",
            "append_only_registry": False,
        }
        generated = {
            CONTRACT_FILE, BLOCKER_FILE, SCRATCH_RECEIPT_FILE, AUTHORITY_FILE,
            REGISTRATION_FILE, TRANSACTION_JOURNAL, REGISTRY_PROGRESS,
        }
        expected_files = sorted({
            *(f"pre_repair/{row['path']}" for row in archive_rows),
            *(f"post_repair/{row['path']}" for row in base.archive_inventory(post)),
            "post_repair/RUN_MANIFEST.json", *generated,
        })
        journal = {
            "schema_version": JOURNAL_SCHEMA,
            "repair_id": REPAIR_ID,
            "run_id": run_dir.name,
            "state": "PREPARED_BEFORE_ACTIVE_MUTATION",
            "original_manifest_sha256": base.sha256_bytes(manifest_raw),
            "active_mutations": mutations,
            "manifest_mutation": manifest_mutation,
            "scratch_mutation": {
                "active_path": SCRATCH.as_posix(),
                "preserved_path": PRESERVED_SCRATCH.as_posix(),
                "sha256": FAILED_SCRATCH_SHA256,
                "bytes": FAILED_SCRATCH_BYTES,
                "operation": "ATOMIC_RENAME_SAME_FILESYSTEM",
            },
            "expected_repair_files": expected_files,
        }
        base.atomic_write(
            staging / TRANSACTION_JOURNAL, base.json_payload(journal)
        )
        journal_sha = base.sha256(staging / TRANSACTION_JOURNAL)
        record = make_record(
            applied_at=applied_at, parent=parent, old_repository=old_repository,
            current=current, identity_chain=identity_chain, core=core,
            trial_before=trial_before, trial_after=trial_after,
            archive_rows=archive_rows, journal_sha256=journal_sha,
            scratch_receipt_sha256=scratch_receipt_sha,
            blocker_sha256=blocker_sha, contract_sha256=contract_sha,
            authority_sha256=authority_sha,
        )
        base.atomic_write(
            staging / REGISTRATION_FILE, base.json_payload(record)
        )
        record["repair_receipt_path"] = f"{PREFIX}/{REGISTRATION_FILE}"
        record["repair_receipt_sha256"] = base.sha256(
            staging / REGISTRATION_FILE
        )
        new_repository = copy.deepcopy(old_repository)
        new_repository.update({
            "previous_execution_commit": old_repository["execution_commit"],
            "previous_source_manifest_sha256": old_repository["source_manifest_sha256"],
            "previous_source_sha256s_sha256": old_repository["source_sha256s_sha256"],
            "previous_query_set_sha256": old_repository["query_set_sha256"],
            "previous_identity": base.repository_identity(old_repository),
            **repository_identity,
            "source_tree_dirty_at_freeze": False,
            "registration_repair_id": REPAIR_ID,
            "identity_history": identity_chain,
        })
        updated = copy.deepcopy(manifest)
        updated["repository"] = new_repository
        updated["data_integrity_repairs"] = records + [record]
        updated["status"] = POST_STATUS
        updated["registration_state"] = REGISTRATION_STATE
        updated_manifest = base.json_payload(updated)
        base.atomic_write(post / "RUN_MANIFEST.json", updated_manifest)

        os.replace(staging, repair_dir)
        repair03._fsync_directory(repairs_root)
        staging = None
        published = True
        pre, post = repair_dir / "pre_repair", repair_dir / "post_repair"
        _transaction_fault_hook("publish_repair_dir")
        active_scratch = run_dir / SCRATCH
        preserved_scratch = run_dir / PRESERVED_SCRATCH
        scratch_source_mode = stat.S_IMODE(os.lstat(active_scratch).st_mode)
        os.replace(active_scratch, preserved_scratch)
        os.chmod(
            preserved_scratch,
            scratch_source_mode & ~0o222,
            follow_symlinks=False,
        )
        repair03._fsync_directory(active_scratch.parent)
        scratch_preserved = True
        _transaction_fault_hook("preserve_failed_scratch")
        _run_main_preflight(run_dir, source_dir, repair_dir / "post_repair")
        _transaction_fault_hook("main_preflight")

        originals = {row["path"]: (pre / row["path"]).read_bytes() for row in mutations}
        replacements = {row["path"]: (post / row["path"]).read_bytes() for row in mutations}
        written: set[str] = set()

        def assert_boundary(
            expected_manifest: bytes = manifest_raw, *, full_scratch_hash: bool = False
        ) -> None:
            if manifest_path.is_symlink() or manifest_path.read_bytes() != expected_manifest:
                fail("repair-07 manifest CAS changed")
            _assert_preserved_scratch_boundary(
                run_dir, full_hash=full_scratch_hash
            )
            for row in mutations:
                relative = row["path"]
                expected = replacements[relative] if relative in written else originals[relative]
                active = run_dir / relative
                if active.is_symlink() or active.read_bytes() != expected:
                    fail(f"repair-07 active CAS changed: {relative}")
            if base.core_result_inventory(run_dir) != core:
                fail("repair-07 core inventory changed during transaction")

        # The preflight is an external subprocess. Re-hash the whole failed
        # scratch immediately after it returns, before any active CAS begins.
        assert_boundary(full_scratch_hash=True)

        def write_progress(state: str, count: int) -> None:
            value = json.loads(repair05._registry_progress_payload(
                run_dir.name, trial_before, trial_append, state, count
            ))
            repair04._write_registry_progress_file(
                repair_dir / REGISTRY_PROGRESS, value
            )

        for row in mutations:
            relative = row["path"]
            if relative == "TRIAL_REGISTRY.jsonl":
                active = run_dir / relative
                descriptor = os.open(
                    active, os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    if repair03._read_locked_file(descriptor) != trial_before:
                        fail("repair-07 registry changed before append")
                    write_progress("WRITE_IN_PROGRESS", 0)
                    count = os.write(descriptor, trial_append)
                    write_progress("WRITE_RETURNED", count)
                    if count != len(trial_append):
                        fail("repair-07 registry append was partial")
                    os.fsync(descriptor)
                    if repair03._read_locked_file(descriptor) != trial_after:
                        fail("repair-07 registry changed during append")
                    write_progress("APPEND_COMPLETE", count)
                    written.add(relative)
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
            else:
                repair05._durable_cas_replace(
                    run_dir, repair_dir, row, f"write:{relative}"
                )
                written.add(relative)
            _transaction_fault_hook(f"write:{relative}")
            assert_boundary()
        repair05._durable_cas_replace(
            run_dir, repair_dir, manifest_mutation, "write:RUN_MANIFEST.json"
        )
        _transaction_fault_hook("write:RUN_MANIFEST.json")
        # Re-hash once more at the final self-bound manifest boundary. The
        # metadata checks on every intermediate CAS keep the expensive 28.8GB
        # read to these two security-critical boundaries.
        assert_boundary(updated_manifest, full_scratch_hash=True)
    except BaseException as transaction_error:
        if published and repair_dir.exists():
            try:
                _restore_scratch_before_transaction_recovery(
                    run_dir,
                    repair_dir,
                    restore_mode=(scratch_source_mode if scratch_preserved else None),
                )
                scratch_preserved = False
            except BaseException as rollback_error:
                raise Repair07RegistrationError(
                    f"repair-07 rollback blocked: {rollback_error}"
                ) from transaction_error
        raise
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
    del scratch_preserved
    return {
        "status": "REGISTRATION_REPAIR07_REFROZEN",
        "repair_id": REPAIR_ID,
        "run_id": run_dir.name,
        "execution_commit": commit,
        "retry_requirement": RETRY_REQUIREMENT,
        "expected_success_resource": copy.deepcopy(SUCCESS_RESOURCE),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).parent)
    args = parser.parse_args(argv)
    result = repair07_registration(args.run_dir, args.source_dir)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
