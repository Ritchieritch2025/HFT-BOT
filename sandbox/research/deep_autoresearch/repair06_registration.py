#!/usr/bin/env python3
"""Minimal append-only validate_run status-wiring preregistration (repair-06)."""

from __future__ import annotations

import os
import sys


_HERE = os.path.realpath(os.path.dirname(__file__))
sys.path[:] = [
    entry for entry in sys.path
    if os.path.realpath(entry if entry else os.getcwd()) != _HERE
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


repair05 = _load_source("repair05_registration")
base = repair05.base
repair04 = repair05.repair04
repair03 = repair05.repair03

REPAIR_ID = "repair-06"
PARENT_REPAIR_ID = "repair-05"
PARENT_EXECUTION_COMMIT = "0ed6c9dac5d83ee354cc274f916c686636db1dc9"
SOURCE_RELATIVE = "sandbox/research/deep_autoresearch"
EXPECTED_CHANGED_PATHS = [
    f"{SOURCE_RELATIVE}/finalize_mission.py",
    f"{SOURCE_RELATIVE}/repair06_registration.py",
    f"{SOURCE_RELATIVE}/rfq_full_stage.py",
    f"{SOURCE_RELATIVE}/test_finalize_mission.py",
    f"{SOURCE_RELATIVE}/test_repair06_registration.py",
    f"{SOURCE_RELATIVE}/test_rfq_full_stage.py",
]

PRE_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_CONSUMER_WIRING_REPAIR05_REGISTERED"
PRE_REGISTRATION_STATE = (
    "RE_FROZEN_AFTER_RFQ_CONSUMER_WIRING_REPAIR05_BEFORE_RFQ_RESULT"
)
POST_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_STATUS_WIRING_REPAIR06_REGISTERED"
REGISTRATION_STATE = "RE_FROZEN_AFTER_RFQ_STATUS_WIRING_REPAIR06_BEFORE_RFQ_RESULT"
REPAIR_SCHEMA = "sports-autoresearch-validate-run-status-wiring-repair-v1"
CONTRACT_SCHEMA = "rfq-validate-run-status-wiring-contract-v1"
AUTHORITY_SCHEMA = "sports-autoresearch-validate-run-status-wiring-authority-v1"
JOURNAL_SCHEMA = "repair06-registration-transaction-v1"
REGISTRY_PROGRESS_SCHEMA = "repair06-registry-append-progress-v1"
CHANGE_CLASS = "VALIDATE_RUN_STATUS_WIRING_ONLY"
REGISTRATION_CHANGE_CLASS = (
    "VALIDATE_RUN_STATUS_WIRING_ONLY_NO_DATA_PARSER_QUERY_SEMANTICS_SELECTION_"
    "QUARANTINE_RESOURCE_CONTRACT_OR_HYPOTHESIS_CHANGE"
)
FINDING = "REPAIR05_STATUS_NOT_ACCEPTED_BY_VALIDATE_RUN"
FAILURE_PHASE = "PRE_EVIDENCE_VALIDATE_RUN_STATUS_GATE"
FAILURE_DISPOSITION = "STATUS_WIRING_FAILURE_BEFORE_RESULT"
ERROR = "RFQStageError: Cycle-1 core is not in an RFQ-stage-compatible state"
RETRY_REQUIREMENT = (
    "APPEND_ONLY_REPAIR06_VALIDATE_RUN_STATUS_WIRING_CORRECTION_AND_"
    "FRESH_SCRATCH_RETRY"
)
AUTHORITY_CLASS = (
    "MISSION_AUTHORIZED_AUTONOMOUS_RESEARCH_CODE_REPAIR_NO_NEW_SPEND_DATA_OR_"
    "FROZEN_VALIDATION_SET"
)
MISSION_SHA256 = "9b4ca417dca394223ecdc6719cd1628bd5ad69d5a80003db304f73b1c485a69c"
PARENT_MANIFEST_SHA256 = "a3af7f05cba25a56e1ae1f2d7541588f67e487c8292f62aeb5f1bd52a767383a"
PARENT_RECEIPT_SHA256 = "13d01e2d4122dc801a88f8c1e7a6824a1f928181900415f922a0908b31b9893d"
PARENT_JOURNAL_SHA256 = "4eef0a29c181ed7b0c28017ba9246831fd8c36e74fd0843f6da665415533aedc"
PARENT_WIRING_SHA256 = "dc2a4f058eab3ff684ee3e9324f58cf19976c54dec9c453bf082d17269e2aafb"
PARENT_AUTHORITY_SHA256 = "c462f9409f9b25ad2d86561d908a17c7856b9a754bed796fd78c9bffd493d073"
BLOCKER_SHA256 = "8afa321af51d6a23a958de9348413eed22df60173cd9b64a09612840dac0077f"
FAILED_RESOURCE_SHA256 = "3adc0f3f56dad9483e3215f172c547c97f5b85890def8512053eeb958f64fc24"
UNCHANGED_STATE_SHA256 = "3c826ee72fa69f9c02c4a38fa33d3c65006ba33374dc592c269388228d506e57"
UNCHANGED_INPUT_SHA256 = "c0ee7ed24d27c58eff4600d33bf7b6d283aaea38020b3a7aee0e63c9c06c2bbb"
W09_SHA256 = "9b295b6fe06a9908c2ff8309cea5ce55ffc35417363cc25f97a4408f9fb2e0a4"
PARENT_QUERY_SHA256 = "e40d8c1c8560d9cb08904dd3b58259f8e4c91da5c2221015f7b558dc0f1c90ca"
PARENT_RFQ_MODULE_AST_SHA256 = (
    "7ed9486f3859ece624a9d7430dfc621fbfc9c3a98fd8753c6dace193a4bca02c"
)
APPROVED_REPAIR06_RFQ_MODULE_AST_SHA256 = (
    "e1dd77af59779b507e013b1c24e75ceef27f9bdf191a65184edf41275a73513e"
)
APPROVED_RFQ_AST_ADDITIONS = {
    "Assign:REPAIR06_AUTHORITY_BASIS",
    "Assign:REPAIR06_AUTHORITY_CLASS",
    "Assign:REPAIR06_AUTHORITY_SCHEMA",
    "Assign:REPAIR06_BLOCKER_ACTIVE",
    "Assign:REPAIR06_BLOCKER_ARCHIVE",
    "Assign:REPAIR06_BLOCKER_SHA256",
    "Assign:REPAIR06_CHANGE_CLASS",
    "Assign:REPAIR06_CYCLE1_BINDING_ARCHIVE",
    "Assign:REPAIR06_EXECUTION_QUERY",
    "Assign:REPAIR06_FAILED_RESOURCE_ARCHIVE",
    "Assign:REPAIR06_FAILED_RESOURCE_SHA256",
    "Assign:REPAIR06_FAILED_RESOURCE_WALL_SECONDS",
    "Assign:REPAIR06_FAILURE_DISPOSITION",
    "Assign:REPAIR06_FAILURE_PHASE",
    "Assign:REPAIR06_FINDING",
    "Assign:REPAIR06_PARENT_AUTHORITY_SHA256",
    "Assign:REPAIR06_PARENT_JOURNAL_SHA256",
    "Assign:REPAIR06_PARENT_MANIFEST_SHA256",
    "Assign:REPAIR06_PARENT_RECEIPT_SHA256",
    "Assign:REPAIR06_PARENT_RFQ_QUERY_SHA256",
    "Assign:REPAIR06_PARENT_WIRING_SHA256",
    "Assign:REPAIR06_PRESERVED_SCRATCH_POLICY",
    "Assign:REPAIR06_PRE_ROOT",
    "Assign:REPAIR06_REGISTRATION",
    "Assign:REPAIR06_REGISTRATION_CHANGE_CLASS",
    "Assign:REPAIR06_REGISTRATION_STATE",
    "Assign:REPAIR06_RETRY_REQUIREMENT",
    "Assign:REPAIR06_ROOT",
    "Assign:REPAIR06_SCHEMA",
    "Assign:REPAIR06_STATUS",
    "Assign:REPAIR06_STATUS_WIRING_CONTRACT",
    "Assign:REPAIR06_STATUS_WIRING_SCHEMA",
    "Assign:REPAIR06_SUCCESS_RESOURCE_LABEL",
    "Assign:REPAIR06_SUCCESS_RESOURCE_PATH",
    "Assign:REPAIR06_TRANSACTION_JOURNAL",
    "Assign:REPAIR06_UNCHANGED_INPUT_ARCHIVE",
    "Assign:REPAIR06_UNCHANGED_STATE_ARCHIVE",
    "FunctionDef:_apply_repair06_status_wiring",
    "FunctionDef:_enforce_registered_validate_run_status_wiring_contract",
    "FunctionDef:_expected_repair06_authority_basis",
    "FunctionDef:_expected_repair06_status_wiring_contract",
    "FunctionDef:_repair06_preflight_contract",
    "FunctionDef:_repair06_retry_command",
    "FunctionDef:_validate_repair06_failure_evidence",
}
APPROVED_RFQ_AST_MODIFICATIONS = {
    "FunctionDef:_apply_repair05_consumer_wiring",
    "FunctionDef:_validate_repair05_failure_evidence",
    "FunctionDef:apply_object_quarantine",
    "FunctionDef:build_summary",
    "FunctionDef:main",
    "FunctionDef:validate_run",
}
PARENT_FINALIZER_MODULE_AST_SHA256 = (
    "af5c36c28a5cee05ee7d31abf9ae41f996178baff1485d2280181d3059ab817e"
)
APPROVED_REPAIR06_FINALIZER_MODULE_AST_SHA256 = (
    "b3f6a50e7a5c48faeee744f912d98e4c2b0850eaf16644a68cfc2ab88acc3ac0"
)
APPROVED_FINALIZER_AST_ADDITIONS = {
    "Assign:ACTIVE_RFQ_REPAIR_RESOURCE_06",
    "Assign:EXPECTED_REPAIR05_MANIFEST_SHA256",
    "Assign:EXPECTED_REPAIR_06_CHANGED_PATHS",
    "Assign:EXPECTED_RFQ_FAILED_RESOURCE_06_SHA256",
    "Assign:EXPECTED_RFQ_FAILED_RESOURCE_06_WALL_SECONDS",
    "Assign:EXPECTED_RFQ_REPAIR05_STATUS_BLOCKER_06_SHA256",
    "Assign:FAILED_RFQ_RESOURCE_06",
    "Assign:REPAIR06_PENDING_STATUS",
    "Assign:REPAIR_06_AUTHORITY_BASIS",
    "Assign:REPAIR_06_BLOCKER_ACTIVE",
    "Assign:REPAIR_06_BLOCKER_ARCHIVE",
    "Assign:REPAIR_06_EXECUTION_QUERY",
    "Assign:REPAIR_06_GIT_SOURCE_MODE",
    "Assign:REPAIR_06_PRE_ROOT",
    "Assign:REPAIR_06_ROOT",
    "Assign:REPAIR_06_SOURCE_RELATIVE",
    "Assign:REPAIR_06_STATUS_WIRING_CONTRACT",
    "Assign:REPAIR_06_TRANSACTION_JOURNAL",
    "Assign:REPAIR_06_W09_ATTESTATION_ARCHIVE",
    "Assign:REPAIR_REGISTRATION_RECEIPT_06",
    "Assign:UNCHANGED_RFQ_INPUT_IDENTITY_06",
    "Assign:UNCHANGED_RFQ_STATE_06",
    "FunctionDef:_repair06_expected_authority",
    "FunctionDef:_repair06_expected_retry_command",
    "FunctionDef:_repair06_expected_status_contract",
    "FunctionDef:_repair06_failure_evidence",
    "FunctionDef:_repair06_preflight_contract",
    "FunctionDef:_repair06_runtime_bindings",
    "FunctionDef:_validate_repair06_contracts",
    "FunctionDef:_validate_repair06_failure_evidence",
    "FunctionDef:_validate_repair06_prefix_overlay",
    "FunctionDef:_validate_repair06_transaction",
    "FunctionDef:validate_rfq_status_wiring_repair06",
}
APPROVED_FINALIZER_AST_MODIFICATIONS = {
    "FunctionDef:_validate_repair05_success_outputs",
    "FunctionDef:_validate_repair05_transaction",
    "FunctionDef:finalize",
    "FunctionDef:validate_rfq_consumer_wiring_repair05",
    "FunctionDef:validate_rfq_partial_quarantine",
}

BLOCKER = Path("DATA_INTEGRITY/RFQ_REPAIR05_STATUS_BLOCKER_06.json")
FAILED_RESOURCE = Path("logs/resources/rfq_full_stage_repair05.json")
STATE = Path("REPORT/tables/RFQ_FULL_STAGE_STATE.json")
INPUT_IDENTITY = Path("DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json")
CYCLE_BINDING = Path("DATA_INTEGRITY/CYCLE1_DUCKDB_BINDING.json")
W09_ATTESTATION = Path("DATA_INTEGRITY/W09_ATTESTATION.json")
SCRATCH = Path("cache/rfq_full_scratch.duckdb")
CONTRACT_FILE = "RFQ_VALIDATE_RUN_STATUS_WIRING_CONTRACT.json"
AUTHORITY_FILE = "AUTHORITY_BASIS.json"
TRANSACTION_JOURNAL = "TRANSACTION_JOURNAL.json"
REGISTRY_PROGRESS = "REGISTRY_APPEND_PROGRESS.json"
PREFIX = "DATA_INTEGRITY/repairs/repair-06"
PRE_ROOT = f"{PREFIX}/pre_repair"
POST_ROOT = f"{PREFIX}/post_repair"
SUCCESS_RESOURCE = {
    "label": "rfq_full_stage_repair06",
    "path": "logs/resources/rfq_full_stage_repair06.json",
}
TRIAL_IDS = (
    "RFQ_FULL_STAGE_ATTEMPT_06",
    "RFQ_VALIDATE_RUN_STATUS_WIRING_REPAIR_06",
)
RUNTIME = copy.deepcopy(repair05.RUNTIME_CONTRACT)
PRESERVED_SCRATCH_POLICY = "FRESH_SCRATCH_REQUIRED_NO_RESUME_NO_PRIOR_SCRATCH_OR_WAL"
PREFLIGHT_FLAGS = [
    "--validate-run-preflight-only",
    "--validate-run-preflight-overlay",
]


class Repair06RegistrationError(repair05.Repair05RegistrationError):
    pass


def fail(message: str) -> None:
    raise Repair06RegistrationError(message)


def _transaction_fault_hook(boundary: str) -> None:
    del boundary


def _forward_fault(boundary: str) -> None:
    _transaction_fault_hook(boundary)


# Reuse the audited repair-05 crash-safe CAS/registry recovery implementation
# through its private source-loaded module, parameterized for repair-06.
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
    return base.sha256_bytes(base.json_payload(value))


def expected_command(run_id: str) -> list[str]:
    root = f"/srv/w09-research/runs/{run_id}"
    return [
        "/opt/w09/venv/bin/python", f"{root}/queries/rfq_full_stage.py",
        "--run-dir", root, "--cache-root", "/srv/w09-research/cache",
        "--memory-limit", "46GB", "--max-temp-size", "70GB",
        "--threads", "4", "--min-free-gib", "100",
        "--clob-max-per-root", "50",
    ]


def _expected_resource(run_id: str) -> dict[str, Any]:
    return {
        "command": expected_command(run_id),
        "completed_at_utc": "2026-07-15T19:51:52.382793Z",
        "cost_rate_usd_per_hour": 0.4713,
        "cpu_hours": 0.00002,
        "cpu_system_seconds": 0.006,
        "cpu_user_seconds": 0.066,
        "cumulative_children_max_rss_kib": 31380,
        "disk_free_after_bytes": 123827527680,
        "disk_free_before_bytes": 123827527680,
        "estimated_compute_cost_usd": 0.000015,
        "label": "rfq_full_stage_repair05",
        "minimum_disk_free_bytes_polled": 123827527680,
        "peak_process_tree_rss_kib_polled": 488,
        "peak_stage_cache_bytes_polled": 114093269135,
        "peak_temp_bytes_polled": 20552,
        "poll_samples": 1,
        "poll_seconds": 5.0,
        "return_code": 1,
        "rss_note": (
            "Process-tree RSS is sampled and may miss sub-poll peaks; "
            "cumulative_children_max_rss_kib is an upper-bound cross-stage "
            "diagnostic, not stage-specific."
        ),
        "s3_bytes_read_by_analysis": 0,
        "s3_note": (
            "Analysis reads the already verified local immutable cache; gate "
            "verification is tracked separately and exposes no per-command S3 "
            "byte counter."
        ),
        "schema_version": "w09-stage-resource-v1",
        "started_at_utc": "2026-07-15T19:51:52.268627Z",
        "wall_seconds": 0.114,
    }


def _expected_blocker(run_id: str) -> dict[str, Any]:
    return {
        "active_input_identity_sha256": UNCHANGED_INPUT_SHA256,
        "active_scratch_absent": True,
        "active_state_sha256": UNCHANGED_STATE_SHA256,
        "active_wal_absent": True,
        "analysis_stage_started": False,
        "authority_basis": AUTHORITY_CLASS,
        "created_at_utc": "2026-07-15T19:52:30Z",
        "data_selection_change": "NONE",
        "dependent_rfq_result_opened": False,
        "error": ERROR,
        "error_type": "RFQStageError",
        "execution_commit": PARENT_EXECUTION_COMMIT,
        "failed_resource_receipt_path": FAILED_RESOURCE.as_posix(),
        "failed_resource_receipt_sha256": FAILED_RESOURCE_SHA256,
        "failure_phase": FAILURE_PHASE,
        "finding": FINDING,
        "hypothesis_design_change": "NONE",
        "line_salvage": False,
        "mission_sha256": MISSION_SHA256,
        "new_input_identity_written": False,
        "new_state_written": False,
        "next_required_action": RETRY_REQUIREMENT,
        "offending_function": "validate_run",
        "parent_registration_path": f"DATA_INTEGRITY/repairs/repair-05/REPAIR_REGISTRATION.json",
        "parent_registration_sha256": PARENT_RECEIPT_SHA256,
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "registered_query_path": "queries/rfq_full_stage.py",
        "registered_query_sha256": PARENT_QUERY_SHA256,
        "repair05_wiring_contract_path": (
            "DATA_INTEGRITY/repairs/repair-05/"
            "RFQ_CONSUMER_VALIDATION_WIRING_CONTRACT.json"
        ),
        "repair05_wiring_contract_sha256": PARENT_WIRING_SHA256,
        "resource_contract_change": "NONE",
        "resource_label": "rfq_full_stage_repair05",
        "resource_return_code": 1,
        "run_id": run_id,
        "schema_version": "rfq-repair05-status-pre-evidence-blocker-v1",
        "source_change_scope": CHANGE_CLASS,
        "traceback_call_chain": ["main", "validate_run"],
        "whole_object_quarantine_unchanged": True,
    }


def _require_file(run_dir: Path, relative: str | Path, digest: str, label: str) -> Path:
    path = base.checked_run_path(run_dir, str(relative), label)
    if path.is_symlink() or not path.is_file() or base.sha256(path) != digest:
        fail(f"{label} hash or safety mismatch")
    return path


def _no_active_attempt_or_result(run_dir: Path) -> None:
    for path in (SCRATCH, Path(str(SCRATCH) + ".wal")):
        if os.path.lexists(run_dir / path):
            fail("repair-06 requires fresh RFQ scratch/WAL absence")
    for path in (repair05.RFQ_SUMMARY, repair05.RFQ_REPORT, repair05.RFQ_CATALOG):
        if os.path.lexists(run_dir / path):
            fail("repair-06 cannot register after an RFQ result exists")


def validate_parent(run_dir: Path, manifest_raw: bytes) -> tuple[dict, dict, bytes, list, dict, list]:
    if base.sha256_bytes(manifest_raw) != PARENT_MANIFEST_SHA256:
        fail("RUN_MANIFEST is not the approved committed repair-05 boundary")
    try:
        manifest = json.loads(manifest_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"RUN_MANIFEST is invalid JSON: {exc}")
    records = manifest.get("data_integrity_repairs")
    if (
        manifest.get("run_id") != run_dir.name
        or manifest.get("status") != PRE_STATUS
        or manifest.get("registration_state") != PRE_REGISTRATION_STATE
        or not isinstance(records, list)
        or [row.get("repair_id") for row in records]
        != ["repair-01", "repair-02", "repair-03", "repair-04", "repair-05"]
    ):
        fail("repair-05 manifest status/history mismatch")
    parent = records[-1]
    if (
        parent.get("schema_version")
        != "sports-autoresearch-consumer-validation-wiring-repair-v1"
        or parent.get("current_execution_commit") != PARENT_EXECUTION_COMMIT
        or parent.get("repair_receipt_sha256") != PARENT_RECEIPT_SHA256
        or parent.get("transaction_journal_sha256") != PARENT_JOURNAL_SHA256
        or parent.get("wiring_contract_sha256") != PARENT_WIRING_SHA256
        or parent.get("authority_basis_sha256") != PARENT_AUTHORITY_SHA256
        or parent.get("registered_rfq_query_sha256") != PARENT_QUERY_SHA256
    ):
        fail("repair-05 committed record mismatch")
    receipt = json.loads(_require_file(
        run_dir, parent["repair_receipt_path"], PARENT_RECEIPT_SHA256,
        "repair-05 receipt",
    ).read_bytes())
    expected_receipt = copy.deepcopy(parent)
    expected_receipt.pop("repair_receipt_path")
    expected_receipt.pop("repair_receipt_sha256")
    if receipt != expected_receipt:
        fail("repair-05 receipt does not reproduce its manifest record")
    repair05_dir = run_dir / "DATA_INTEGRITY/repairs/repair-05"
    journal_path = _require_file(
        run_dir, parent["transaction_journal_path"], PARENT_JOURNAL_SHA256,
        "repair-05 transaction journal",
    )
    journal = json.loads(journal_path.read_bytes())
    if (
        journal.get("schema_version") != "repair05-registration-transaction-v1"
        or journal.get("repair_id") != "repair-05"
        or journal.get("run_id") != run_dir.name
        or repair05._safe_relative_file_set(repair05_dir)
        != set(journal.get("expected_repair_files", []))
    ):
        fail("repair-05 committed transaction mismatch")
    _require_file(run_dir, parent["wiring_contract_path"], PARENT_WIRING_SHA256, "repair-05 wiring")
    _require_file(run_dir, parent["authority_basis_path"], PARENT_AUTHORITY_SHA256, "repair-05 authority")
    repository = manifest.get("repository")
    if (
        not isinstance(repository, dict)
        or repository.get("execution_commit") != PARENT_EXECUTION_COMMIT
        or repository.get("identity_history") != parent.get("repository_identity_chain")
        or len(repository.get("identity_history", [])) != 6
    ):
        fail("repair-05 repository identity mismatch")
    trial = _require_file(
        run_dir, "TRIAL_REGISTRY.jsonl", parent["trial_registry"]["current_sha256"],
        "repair-05 trial registry",
    ).read_bytes()
    rows = repair05._read_registry_rows(trial)
    core = base.core_result_inventory(run_dir)
    if core != parent.get("core_result_artifacts"):
        fail("repair-05 inherited core inventory changed")
    _, cycle = base.validate_cycle1_duckdb_binding(run_dir, run_dir / CYCLE_BINDING)
    if cycle != parent.get("cycle1_duckdb_binding"):
        fail("repair-05 Cycle-1 binding changed")
    _require_file(run_dir, W09_ATTESTATION, W09_SHA256, "W09 attestation")
    _require_file(run_dir, STATE, UNCHANGED_STATE_SHA256, "unchanged RFQ state")
    _require_file(run_dir, INPUT_IDENTITY, UNCHANGED_INPUT_SHA256, "unchanged RFQ input")
    _no_active_attempt_or_result(run_dir)
    return manifest, parent, trial, rows, repository, core


def validate_failure(run_dir: Path) -> dict[str, Any]:
    blocker_path = _require_file(run_dir, BLOCKER, BLOCKER_SHA256, "repair-06 blocker")
    resource_path = _require_file(
        run_dir, FAILED_RESOURCE, FAILED_RESOURCE_SHA256, "repair-06 failed resource"
    )
    blocker = json.loads(blocker_path.read_bytes())
    resource = json.loads(resource_path.read_bytes())
    if blocker != _expected_blocker(run_dir.name) or resource != _expected_resource(run_dir.name):
        fail("repair-06 exact failed-attempt evidence mismatch")
    return {"blocker": blocker, "resource": resource}


def validate_source_provenance(
    run_dir: Path, source_dir: Path, manifest: dict, old_repository: dict
) -> tuple[Path, str, bytes, bytes, list[tuple[str, str, bytes]]]:
    repo_root, source_relative, commit = repair04._verify_clean_source_provenance(source_dir)
    repair04._validate_git_commit_scope(repo_root, commit)
    source_manifest, source_sums = repair04._validate_clean_head_source_binding(
        source_dir, repo_root, source_relative, commit
    )
    prior, query_names = repair04._verify_prior_repository_provenance(
        run_dir, source_dir, repo_root, source_relative, commit, manifest
    )
    if prior != old_repository:
        fail("repair-06 prior repository identity changed")
    parent_rfq = repair04._provenance_git_blob(
        repo_root, PARENT_EXECUTION_COMMIT, f"{SOURCE_RELATIVE}/rfq_full_stage.py"
    )
    current_rfq = (source_dir / "rfq_full_stage.py").read_bytes()
    repair05._validate_consumer_governance_ast_delta(
        parent_rfq,
        current_rfq,
        approved_additions=APPROVED_RFQ_AST_ADDITIONS,
        approved_modifications=APPROVED_RFQ_AST_MODIFICATIONS,
        expected_parent_ast_sha256=PARENT_RFQ_MODULE_AST_SHA256,
        expected_current_ast_sha256=APPROVED_REPAIR06_RFQ_MODULE_AST_SHA256,
    )
    if repair05._sql_call_fingerprint(parent_rfq) != repair05._sql_call_fingerprint(current_rfq):
        fail("repair-06 changed RFQ SQL semantics")
    parent_finalizer = repair04._provenance_git_blob(
        repo_root, PARENT_EXECUTION_COMMIT,
        f"{SOURCE_RELATIVE}/finalize_mission.py",
    )
    current_finalizer = (source_dir / "finalize_mission.py").read_bytes()
    repair05._validate_consumer_governance_ast_delta(
        parent_finalizer,
        current_finalizer,
        approved_additions=APPROVED_FINALIZER_AST_ADDITIONS,
        approved_modifications=APPROVED_FINALIZER_AST_MODIFICATIONS,
        expected_parent_ast_sha256=PARENT_FINALIZER_MODULE_AST_SHA256,
        expected_current_ast_sha256=(
            APPROVED_REPAIR06_FINALIZER_MODULE_AST_SHA256
        ),
    )
    rows = []
    for name in query_names:
        path = source_dir / name
        if path.is_symlink() or not path.is_file():
            fail(f"repair-06 query source is missing/unsafe: {name}")
        payload = path.read_bytes()
        rows.append((base.sha256_bytes(payload), f"queries/{name}", payload))
    return repo_root, commit, source_manifest, source_sums, rows


def _failure_evidence(run_id: str) -> dict[str, Any]:
    del run_id
    return {
        "blocker_active_path": BLOCKER.as_posix(),
        "blocker_archived_path": f"{PRE_ROOT}/{BLOCKER.as_posix()}",
        "blocker_sha256": BLOCKER_SHA256,
        "failed_resource_active_path": FAILED_RESOURCE.as_posix(),
        "failed_resource_archived_path": f"{PRE_ROOT}/{FAILED_RESOURCE.as_posix()}",
        "failed_resource_sha256": FAILED_RESOURCE_SHA256,
        "unchanged_state_active_path": STATE.as_posix(),
        "unchanged_state_archived_path": f"{PRE_ROOT}/{STATE.as_posix()}",
        "unchanged_state_sha256": UNCHANGED_STATE_SHA256,
        "unchanged_input_identity_active_path": INPUT_IDENTITY.as_posix(),
        "unchanged_input_identity_archived_path": f"{PRE_ROOT}/{INPUT_IDENTITY.as_posix()}",
        "unchanged_input_identity_sha256": UNCHANGED_INPUT_SHA256,
        "analysis_stage_started": False,
        "new_state_written": False,
        "new_input_identity_written": False,
        "active_scratch_absent": True,
        "active_wal_absent": True,
    }


def _preflight_contract(run_id: str) -> dict[str, Any]:
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


def make_status_wiring_contract(
    run_id: str,
    applied_at: str,
    parent: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_SCHEMA,
        "repair_id": REPAIR_ID,
        "run_id": run_id,
        "applied_at_utc": applied_at,
        "pre_repair_status": PRE_STATUS,
        "post_repair_status": POST_STATUS,
        "registration_state": REGISTRATION_STATE,
        "change_class": CHANGE_CLASS,
        "failure_evidence": _failure_evidence(run_id),
        "parent_repair05": {
            "manifest_sha256": PARENT_MANIFEST_SHA256,
            "repair_receipt_path": parent["repair_receipt_path"],
            "repair_receipt_sha256": PARENT_RECEIPT_SHA256,
            "transaction_journal_path": parent["transaction_journal_path"],
            "transaction_journal_sha256": PARENT_JOURNAL_SHA256,
            "wiring_contract_path": parent["wiring_contract_path"],
            "wiring_contract_sha256": PARENT_WIRING_SHA256,
            "authority_basis_path": parent["authority_basis_path"],
            "authority_basis_sha256": PARENT_AUTHORITY_SHA256,
            "core_result_artifacts": copy.deepcopy(parent["core_result_artifacts"]),
        },
        "status_wiring": {
            "offending_function": "validate_run",
            "error": ERROR,
            "error_type": "RFQStageError",
            "failure_phase": FAILURE_PHASE,
            "finding": FINDING,
            "accepted_parent_status": PRE_STATUS,
            "accepted_parent_registration_state": PRE_REGISTRATION_STATE,
            "registered_post_status": POST_STATUS,
            "registered_post_registration_state": REGISTRATION_STATE,
            "source_change_scope": CHANGE_CLASS,
        },
        "inherited_contracts": {
            "coverage": copy.deepcopy(parent["coverage"]),
            "cycle1_duckdb_binding": copy.deepcopy(parent["cycle1_duckdb_binding"]),
            "parser_contract": copy.deepcopy(parent["parser_contract"]),
            "resource_contract": copy.deepcopy(parent["resource_contract"]),
            "runtime": copy.deepcopy(RUNTIME),
        },
        "preflight": _preflight_contract(run_id),
        "retry": {
            "requirement": RETRY_REQUIREMENT,
            "expected_success_resource": copy.deepcopy(SUCCESS_RESOURCE),
            "command": expected_command(run_id),
            "fresh_scratch": True,
            "resume": False,
        },
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": "NONE",
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
    }


def make_authority(
    run_id: str, applied_at: str, contract_path: str, contract_sha: str
) -> dict[str, Any]:
    return {
        "schema_version": AUTHORITY_SCHEMA,
        "repair_id": REPAIR_ID,
        "run_id": run_id,
        "created_at_utc": applied_at,
        "authority_class": AUTHORITY_CLASS,
        "mission_sha256": MISSION_SHA256,
        "permitted_change": CHANGE_CLASS,
        "status_wiring_contract_path": contract_path,
        "status_wiring_contract_sha256": contract_sha,
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": "NONE",
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
    }


def make_trial_records(
    applied_at: str,
    parent: dict[str, Any],
    old_repository: dict[str, Any],
    current: dict[str, Any],
    contract_sha: str,
    authority_sha: str,
    resource: dict[str, Any],
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
        "stage": "RFQ_FULL_STAGE_REPAIR05",
        "failure_class": FINDING,
        "failure_disposition": FAILURE_DISPOSITION,
        "failure_phase": FAILURE_PHASE,
        "error": ERROR,
        "error_type": "RFQStageError",
        "blocker_path": f"{PRE_ROOT}/{BLOCKER.as_posix()}",
        "blocker_sha256": BLOCKER_SHA256,
        "failed_resource_receipt_path": f"{PRE_ROOT}/{FAILED_RESOURCE.as_posix()}",
        "failed_resource_receipt_sha256": FAILED_RESOURCE_SHA256,
        "resource_label": "rfq_full_stage_repair05",
        "return_code": 1,
        "resource_metrics": {
            key: resource[key] for key in (
                "started_at_utc", "completed_at_utc", "wall_seconds",
                "cpu_system_seconds", "cpu_user_seconds",
                "peak_process_tree_rss_kib_polled", "peak_temp_bytes_polled",
                "minimum_disk_free_bytes_polled",
            )
        },
        "unchanged_state_path": f"{PRE_ROOT}/{STATE.as_posix()}",
        "unchanged_state_sha256": UNCHANGED_STATE_SHA256,
        "unchanged_input_identity_path": f"{PRE_ROOT}/{INPUT_IDENTITY.as_posix()}",
        "unchanged_input_identity_sha256": UNCHANGED_INPUT_SHA256,
        "active_scratch_absent": True,
        "active_wal_absent": True,
        "new_state_written": False,
        "new_input_identity_written": False,
        "analysis_stage_started": False,
        "hypothesis_conclusion": "NONE",
    }
    prereg = {
        **common,
        "trial_registration_id": TRIAL_IDS[1],
        "record_type": "VALIDATE_RUN_STATUS_WIRING_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR06_PREREGISTRATION",
        "finding": FINDING,
        "registration_change_class": REGISTRATION_CHANGE_CLASS,
        "retry_requirement": RETRY_REQUIREMENT,
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "expected_success_resource": copy.deepcopy(SUCCESS_RESOURCE),
        "status_wiring_contract_path": f"{PREFIX}/{CONTRACT_FILE}",
        "status_wiring_contract_sha256": contract_sha,
        "authority_basis_path": f"{PREFIX}/{AUTHORITY_FILE}",
        "authority_basis_sha256": authority_sha,
        "previous_execution_commit": old_repository["execution_commit"],
        "runtime": copy.deepcopy(RUNTIME),
        "coverage": copy.deepcopy(parent["coverage"]),
        "core_results_recomputed": False,
        "preserved_scratch_policy": PRESERVED_SCRATCH_POLICY,
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": "NONE",
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
    }
    return failure, prereg


def make_record(
    *, applied_at: str, parent: dict[str, Any], old_repository: dict[str, Any],
    current: dict[str, Any], identity_chain: list[dict[str, Any]],
    core: list[dict[str, Any]], trial_before: bytes, trial_after: bytes,
    archive_rows: list[dict[str, Any]], journal_sha: str,
    contract: dict[str, Any], contract_sha: str, authority: dict[str, Any],
    authority_sha: str,
) -> dict[str, Any]:
    failed = {
        "attempt_id": "RFQ_FULL_STAGE_REPAIR05_ATTEMPT_06",
        "error": ERROR,
        "error_type": "RFQStageError",
        "failure_phase": FAILURE_PHASE,
        "finding": FINDING,
        **_failure_evidence(""),
        "resource_label": "rfq_full_stage_repair05",
        "return_code": 1,
        "resource_command": expected_command(contract["run_id"]),
    }
    record = {
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
        "failed_attempt": failed,
        "blocker_path": f"{PRE_ROOT}/{BLOCKER.as_posix()}",
        "blocker_sha256": BLOCKER_SHA256,
        "failed_resource_receipt_path": f"{PRE_ROOT}/{FAILED_RESOURCE.as_posix()}",
        "failed_resource_receipt_sha256": FAILED_RESOURCE_SHA256,
        "unchanged_state_path": f"{PRE_ROOT}/{STATE.as_posix()}",
        "unchanged_state_sha256": UNCHANGED_STATE_SHA256,
        "unchanged_input_identity_path": f"{PRE_ROOT}/{INPUT_IDENTITY.as_posix()}",
        "unchanged_input_identity_sha256": UNCHANGED_INPUT_SHA256,
        "cycle1_binding_path": f"{PRE_ROOT}/{CYCLE_BINDING.as_posix()}",
        "cycle1_binding_sha256": parent["cycle1_duckdb_binding"]["active_sha256"],
        "cycle1_duckdb_binding": copy.deepcopy(parent["cycle1_duckdb_binding"]),
        "parent_manifest_sha256": PARENT_MANIFEST_SHA256,
        "parent_repair_registration_path": parent["repair_receipt_path"],
        "parent_repair_registration_sha256": PARENT_RECEIPT_SHA256,
        "parent_transaction_journal_path": parent["transaction_journal_path"],
        "parent_transaction_journal_sha256": PARENT_JOURNAL_SHA256,
        "parent_wiring_contract_path": parent["wiring_contract_path"],
        "parent_wiring_contract_sha256": PARENT_WIRING_SHA256,
        "parent_authority_basis_path": parent["authority_basis_path"],
        "parent_authority_basis_sha256": PARENT_AUTHORITY_SHA256,
        "status_wiring_contract": copy.deepcopy(contract),
        "status_wiring_contract_path": f"{PREFIX}/{CONTRACT_FILE}",
        "status_wiring_contract_sha256": contract_sha,
        "authority_basis": copy.deepcopy(authority),
        "authority_basis_path": f"{PREFIX}/{AUTHORITY_FILE}",
        "authority_basis_sha256": authority_sha,
        "previous_repair_record_sha256": canonical_hash(parent),
        "coverage": copy.deepcopy(parent["coverage"]),
        "cumulative_quarantined_objects": copy.deepcopy(parent["cumulative_quarantined_objects"]),
        "core_result_artifacts": copy.deepcopy(core),
        "core_result_disposition": "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED",
        "core_results_recomputed": False,
        "parser_contract": copy.deepcopy(parent["parser_contract"]),
        "parser_contract_path": parent["parser_contract_path"],
        "parser_contract_sha256": parent["parser_contract_sha256"],
        "resource_contract": copy.deepcopy(parent["resource_contract"]),
        "resource_contract_path": parent["resource_contract_path"],
        "resource_contract_sha256": parent["resource_contract_sha256"],
        "expected_success_resource": copy.deepcopy(SUCCESS_RESOURCE),
        "registered_rfq_query_sha256": current["registered_rfq_query_sha256"],
        "previous_execution_commit": old_repository["execution_commit"],
        "current_execution_commit": current["execution_commit"],
        "previous_repository_identity": base.repository_identity(old_repository),
        "current_repository_identity": {
            key: copy.deepcopy(current[key]) for key in (
                "execution_commit", "source_manifest_sha256",
                "source_sha256s_sha256", "query_set_sha256", "query_files",
            )
        },
        "repository_identity_chain": copy.deepcopy(identity_chain),
        "source_verification_mode": "LOCAL_GIT_CLEAN_COMMITTED_HEAD",
        "preserved_scratch_policy": PRESERVED_SCRATCH_POLICY,
        "main_preflight": copy.deepcopy(contract["preflight"]),
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": "NONE",
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
        "transaction_journal_sha256": journal_sha,
    }
    return record


def _resolve_replacement(
    run_dir: Path, repair_dir: Path, mutation: dict[str, Any], label: str
) -> Path:
    relative = mutation.get("replacement_staging_path")
    if not isinstance(relative, str):
        fail(f"{label} replacement path is missing")
    path = base.checked_run_path(run_dir, relative, label)
    try:
        path.resolve().relative_to(repair_dir.resolve())
    except ValueError:
        fail(f"{label} replacement escapes repair-06")
    if path.is_symlink() or not path.is_file():
        fail(f"{label} replacement is missing/unsafe")
    digest = mutation.get("replacement_sha256")
    if digest is not None:
        if base.sha256(path) != base.require_sha(digest, f"{label} SHA"):
            fail(f"{label} replacement hash mismatch")
        return path
    if mutation.get("path") != "RUN_MANIFEST.json":
        fail(f"{label} lacks a replacement hash")
    manifest = json.loads(path.read_bytes())
    records = manifest.get("data_integrity_repairs")
    if (
        manifest.get("run_id") != run_dir.name
        or manifest.get("status") != POST_STATUS
        or manifest.get("registration_state") != REGISTRATION_STATE
        or not isinstance(records, list)
        or len(records) != 6
        or records[-1].get("repair_id") != REPAIR_ID
        or records[-1].get("transaction_journal_sha256")
        != base.sha256(repair_dir / TRANSACTION_JOURNAL)
        or records[-1].get("repair_receipt_sha256")
        != base.sha256(repair_dir / "REPAIR_REGISTRATION.json")
    ):
        fail(f"{label} is not the self-bound repair-06 manifest")
    return path


repair05._resolve_durable_replacement = _resolve_replacement


def _run_main_preflight(run_dir: Path, source_dir: Path, overlay: Path) -> None:
    expected = f"RFQ_VALIDATE_RUN_PREFLIGHT_COMPLETE run_id={run_dir.name}"
    command = [
        sys.executable,
        str(source_dir / "rfq_full_stage.py"),
        "--run-dir", str(run_dir),
        "--validate-run-preflight-only",
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
            "repair-06 RFQ main preflight failed: "
            f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
        )


def _archive_payloads(
    run_dir: Path, manifest_raw: bytes, trial: bytes, repository: dict[str, Any]
) -> tuple[list[Path], dict[Path, bytes]]:
    paths = [
        Path("RUN_MANIFEST.json"), Path("TRIAL_REGISTRY.jsonl"),
        Path("SOURCE_MANIFEST.json"), Path("SOURCE_SHA256SUMS.txt"),
        Path("QUERY_SHA256SUMS.txt"), BLOCKER, FAILED_RESOURCE, STATE,
        INPUT_IDENTITY, CYCLE_BINDING, W09_ATTESTATION,
        *(Path(path) for path in repository["query_files"]),
    ]
    expected = {
        Path("RUN_MANIFEST.json"): base.sha256_bytes(manifest_raw),
        Path("TRIAL_REGISTRY.jsonl"): base.sha256_bytes(trial),
        Path("SOURCE_MANIFEST.json"): repository["source_manifest_sha256"],
        Path("SOURCE_SHA256SUMS.txt"): repository["source_sha256s_sha256"],
        Path("QUERY_SHA256SUMS.txt"): repository["query_set_sha256"],
        BLOCKER: BLOCKER_SHA256, FAILED_RESOURCE: FAILED_RESOURCE_SHA256,
        STATE: UNCHANGED_STATE_SHA256, INPUT_IDENTITY: UNCHANGED_INPUT_SHA256,
        CYCLE_BINDING: base.sha256(run_dir / CYCLE_BINDING),
        W09_ATTESTATION: W09_SHA256,
    }
    query_receipt = {
        Path(relative): digest
        for digest, relative in base.parse_checksums(
            run_dir / "QUERY_SHA256SUMS.txt", "repair-06 parent query receipt"
        )
    }
    expected.update(query_receipt)
    if set(paths) != set(expected):
        fail("repair-06 archive/query path set mismatch")
    payloads = {}
    for relative in paths:
        path = run_dir / relative
        if path.is_symlink() or not path.is_file():
            fail(f"repair-06 archive source missing/unsafe: {relative}")
        payload = path.read_bytes()
        if base.sha256_bytes(payload) != expected[relative]:
            fail(f"repair-06 archive source identity changed: {relative}")
        payloads[relative] = payload
    return paths, payloads


def _cleanup_committed_displaced(run_dir: Path, repair_dir: Path) -> None:
    journal = json.loads((repair_dir / TRANSACTION_JOURNAL).read_bytes())
    mutation = journal.get("manifest_mutation")
    if not isinstance(mutation, dict):
        fail("repair-06 committed manifest mutation is missing")
    displaced = base.checked_run_path(
        run_dir, mutation.get("displaced_path"), "repair-06 manifest displaced"
    )
    if not os.path.lexists(displaced):
        return
    original = _require_file(
        run_dir, mutation["original_archive_path"], mutation["original_sha256"],
        "repair-06 archived manifest",
    )
    staged = _resolve_replacement(run_dir, repair_dir, mutation, "repair-06 staged manifest")
    active = run_dir / "RUN_MANIFEST.json"
    optional = displaced.relative_to(repair_dir).as_posix()
    if (
        repair05._safe_relative_file_set(repair_dir)
        != set(journal.get("expected_repair_files", [])) | {optional}
        or active.is_symlink()
        or active.read_bytes() != staged.read_bytes()
        or displaced.is_symlink()
        or not displaced.is_file()
        or displaced.read_bytes() != original.read_bytes()
    ):
        fail("repair-06 committed manifest cleanup identity mismatch")
    displaced.unlink()
    repair03._fsync_directory(displaced.parent)


def validate_already_applied(
    run_dir: Path, source_dir: Path, manifest: dict[str, Any], records: list
) -> dict[str, Any]:
    if (
        manifest.get("status") != POST_STATUS
        or manifest.get("registration_state") != REGISTRATION_STATE
        or [row.get("repair_id") for row in records]
        != ["repair-01", "repair-02", "repair-03", "repair-04", "repair-05", "repair-06"]
    ):
        fail("existing repair-06 history is inconsistent")
    record = records[-1]
    repair_dir = run_dir / PREFIX
    receipt_path = _require_file(
        run_dir, record.get("repair_receipt_path"),
        record.get("repair_receipt_sha256"), "repair-06 receipt",
    )
    receipt = json.loads(receipt_path.read_bytes())
    expected = copy.deepcopy(record)
    expected.pop("repair_receipt_path")
    expected.pop("repair_receipt_sha256")
    if receipt != expected:
        fail("repair-06 receipt/manifest mismatch")
    journal_path = _require_file(
        run_dir, record["transaction_journal_path"],
        record["transaction_journal_sha256"], "repair-06 journal",
    )
    journal = json.loads(journal_path.read_bytes())
    if repair05._safe_relative_file_set(repair_dir) != set(journal["expected_repair_files"]):
        fail("repair-06 committed file inventory changed")
    for row in journal["active_mutations"]:
        active = base.checked_run_path(run_dir, row["path"], "repair-06 active")
        if active.is_symlink() or base.sha256(active) != row["replacement_sha256"]:
            fail(f"repair-06 active mutation changed: {row['path']}")
    _require_file(run_dir, BLOCKER, BLOCKER_SHA256, "repair-06 blocker")
    _require_file(run_dir, FAILED_RESOURCE, FAILED_RESOURCE_SHA256, "repair-06 resource")
    _require_file(run_dir, STATE, UNCHANGED_STATE_SHA256, "repair-06 state")
    _require_file(run_dir, INPUT_IDENTITY, UNCHANGED_INPUT_SHA256, "repair-06 input")
    if base.core_result_inventory(run_dir) != record["core_result_artifacts"]:
        fail("repair-06 core inventory changed")
    _no_active_attempt_or_result(run_dir)
    repo_root, source_relative, commit = repair04._verify_clean_source_provenance(source_dir)
    repair04._validate_git_commit_scope(repo_root, commit)
    source_manifest, source_sums = repair04._validate_clean_head_source_binding(
        source_dir, repo_root, source_relative, commit
    )
    repository = manifest["repository"]
    if (
        commit != repository["execution_commit"]
        or base.sha256_bytes(source_manifest) != repository["source_manifest_sha256"]
        or base.sha256_bytes(source_sums) != repository["source_sha256s_sha256"]
    ):
        fail("repair-06 idempotent source identity changed")
    return {"status": "REGISTRATION_REPAIR06_ALREADY_REFROZEN", "repair_id": REPAIR_ID, "run_id": run_dir.name}


def repair06_registration(run_dir: Path, source_dir: Path) -> dict[str, Any]:
    if Path(run_dir).is_symlink() or Path(source_dir).is_symlink():
        fail("repair-06 run/source directory must not be a symlink")
    run_dir = Path(run_dir).resolve()
    source_dir = Path(source_dir).resolve()
    with repair03._exclusive_run_lock(run_dir):
        return _repair06_locked(run_dir, source_dir)


def _repair06_locked(run_dir: Path, source_dir: Path) -> dict[str, Any]:
    repair_dir = run_dir / PREFIX
    repair05._cleanup_repair05_tombstones(run_dir, repair_dir)
    manifest_path = run_dir / "RUN_MANIFEST.json"
    if not os.path.lexists(manifest_path) and repair_dir.exists():
        repair05.recover_incomplete_transaction(run_dir, repair_dir)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        fail("RUN_MANIFEST is missing/unsafe")
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    records = manifest.get("data_integrity_repairs")
    if not isinstance(records, list):
        fail("RUN_MANIFEST repair history is invalid")
    if len(records) == 6:
        _cleanup_committed_displaced(run_dir, repair_dir)
        return validate_already_applied(run_dir, source_dir, manifest, records)
    if len(records) != 5:
        fail("repair-06 requires exactly repair-01 through repair-05")
    if repair_dir.exists():
        repair05.recover_incomplete_transaction(run_dir, repair_dir)
        manifest_raw = manifest_path.read_bytes()
        manifest = json.loads(manifest_raw)
        records = manifest.get("data_integrity_repairs")
        if not isinstance(records, list) or len(records) != 5:
            fail("repair-06 recovery did not restore the repair-05 parent")

    manifest, parent, trial_before, _, old_repository, core = validate_parent(
        run_dir, manifest_raw
    )
    failure = validate_failure(run_dir)
    (
        source_repo_root, current_commit, source_manifest, source_sums, query_rows,
    ) = validate_source_provenance(run_dir, source_dir, manifest, old_repository)
    query_sums = "".join(
        f"{digest}  {relative}\n" for digest, relative, _ in query_rows
    ).encode("utf-8")
    rfq = [row for row in query_rows if row[1] == "queries/rfq_full_stage.py"]
    if len(rfq) != 1:
        fail("repair-06 requires one RFQ execution query")
    repository_identity = {
        "execution_commit": current_commit,
        "source_manifest_sha256": base.sha256_bytes(source_manifest),
        "source_sha256s_sha256": base.sha256_bytes(source_sums),
        "query_set_sha256": base.sha256_bytes(query_sums),
        "query_files": copy.deepcopy(old_repository["query_files"]),
    }
    current = {
        **repository_identity,
        "registered_rfq_query_sha256": rfq[0][0],
    }
    old_chain = old_repository.get("identity_history")
    if (
        not isinstance(old_chain, list)
        or len(old_chain) != 6
        or old_chain != parent.get("repository_identity_chain")
        or old_chain[-1] != base.repository_identity(old_repository)
    ):
        fail("repair-06 parent repository chain is not exact")
    identity_chain = copy.deepcopy(old_chain) + [repository_identity]
    archive_paths, archive_payloads = _archive_payloads(
        run_dir, manifest_raw, trial_before, old_repository
    )

    applied_at = base.now_utc()
    repair_root = run_dir / "DATA_INTEGRITY/repairs"
    repair_root.mkdir(parents=True, exist_ok=True)
    staging: Path | None = Path(tempfile.mkdtemp(prefix=".repair-06-", dir=repair_root))
    try:
        pre, post = staging / "pre_repair", staging / "post_repair"
        _transaction_fault_hook("archive_copy:before")
        for relative in archive_paths:
            base.atomic_write(pre / relative, archive_payloads[relative])
            if (
                (run_dir / relative).read_bytes() != archive_payloads[relative]
                or (pre / relative).read_bytes() != archive_payloads[relative]
            ):
                fail(f"repair-06 archive copy raced validation: {relative}")
        archive_rows = base.archive_inventory(pre)
        if any("scratch" in row["path"].lower() for row in archive_rows):
            fail("repair-06 archive must not copy preserved scratch")

        base.atomic_write(post / "SOURCE_MANIFEST.json", source_manifest)
        base.atomic_write(post / "SOURCE_SHA256SUMS.txt", source_sums)
        base.atomic_write(post / "QUERY_SHA256SUMS.txt", query_sums)
        for _, relative, payload in query_rows:
            base.atomic_write(post / relative, payload)

        contract = make_status_wiring_contract(run_dir.name, applied_at, parent)
        base.atomic_write(staging / CONTRACT_FILE, base.json_payload(contract))
        contract_sha = base.sha256(staging / CONTRACT_FILE)
        authority = make_authority(
            run_dir.name, applied_at, f"{PREFIX}/{CONTRACT_FILE}", contract_sha
        )
        base.atomic_write(staging / AUTHORITY_FILE, base.json_payload(authority))
        authority_sha = base.sha256(staging / AUTHORITY_FILE)
        failure_trial, prereg_trial = make_trial_records(
            applied_at, parent, old_repository, current, contract_sha,
            authority_sha, failure["resource"],
        )
        trial_after = (
            trial_before + base.registry_line(failure_trial)
            + base.registry_line(prereg_trial)
        )
        trial_append = trial_after[len(trial_before):]
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
        active_mutations = []
        for relative in mutation_relatives:
            active_mutations.append({
                "path": relative,
                "original_archive_path": f"{PRE_ROOT}/{relative}",
                "original_sha256": base.sha256(pre / relative),
                "replacement_staging_path": f"{POST_ROOT}/{relative}",
                "replacement_sha256": base.sha256(post / relative),
                "displaced_path": (
                    f"{PREFIX}/cas_displaced/{relative.replace('/', '__')}.original"
                ),
                "append_only_registry": relative == "TRIAL_REGISTRY.jsonl",
            })
        manifest_mutation = {
            "path": "RUN_MANIFEST.json",
            "original_archive_path": f"{PRE_ROOT}/RUN_MANIFEST.json",
            "original_sha256": base.sha256_bytes(manifest_raw),
            "replacement_staging_path": f"{POST_ROOT}/RUN_MANIFEST.json",
            "replacement_sha256": None,
            "displaced_path": f"{PREFIX}/cas_displaced/RUN_MANIFEST.json.original",
            "append_only_registry": False,
        }
        expected_files = sorted({
            *(f"pre_repair/{row['path']}" for row in archive_rows),
            *(f"post_repair/{row['path']}" for row in base.archive_inventory(post)),
            "post_repair/RUN_MANIFEST.json", CONTRACT_FILE, AUTHORITY_FILE,
            "REPAIR_REGISTRATION.json", TRANSACTION_JOURNAL, REGISTRY_PROGRESS,
        })
        journal = {
            "schema_version": JOURNAL_SCHEMA,
            "repair_id": REPAIR_ID,
            "run_id": run_dir.name,
            "state": "PREPARED_BEFORE_ACTIVE_MUTATION",
            "original_manifest_sha256": base.sha256_bytes(manifest_raw),
            "active_mutations": active_mutations,
            "manifest_mutation": manifest_mutation,
            "expected_repair_files": expected_files,
        }
        base.atomic_write(staging / TRANSACTION_JOURNAL, base.json_payload(journal))
        journal_sha = base.sha256(staging / TRANSACTION_JOURNAL)
        record = make_record(
            applied_at=applied_at, parent=parent,
            old_repository=old_repository, current=current,
            identity_chain=identity_chain, core=core,
            trial_before=trial_before, trial_after=trial_after,
            archive_rows=archive_rows, journal_sha=journal_sha,
            contract=contract, contract_sha=contract_sha,
            authority=authority, authority_sha=authority_sha,
        )
        base.atomic_write(
            staging / "REPAIR_REGISTRATION.json", base.json_payload(record)
        )
        record["repair_receipt_path"] = f"{PREFIX}/REPAIR_REGISTRATION.json"
        record["repair_receipt_sha256"] = base.sha256(
            staging / "REPAIR_REGISTRATION.json"
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

        originals = {
            row["path"]: (pre / row["path"]).read_bytes()
            for row in active_mutations
        }
        replacements = {
            row["path"]: (post / row["path"]).read_bytes()
            for row in active_mutations
        }
        by_path = {row["path"]: row for row in active_mutations}
        written: set[str] = set()
        published = False

        def assert_boundary(expected_manifest: bytes = manifest_raw) -> None:
            if manifest_path.is_symlink() or manifest_path.read_bytes() != expected_manifest:
                fail("repair-06 manifest CAS changed")
            for row in active_mutations:
                relative = row["path"]
                expected = replacements[relative] if relative in written else originals[relative]
                active = run_dir / relative
                if active.is_symlink() or active.read_bytes() != expected:
                    fail(f"repair-06 active CAS changed: {relative}")
            for relative, payload in archive_payloads.items():
                if relative.as_posix() in mutation_relatives or relative == Path("RUN_MANIFEST.json"):
                    continue
                if (run_dir / relative).read_bytes() != payload:
                    fail(f"repair-06 immutable boundary changed: {relative}")
            if base.core_result_inventory(run_dir) != core:
                fail("repair-06 core inventory changed during transaction")
            _no_active_attempt_or_result(run_dir)

        def write_progress(state: str, count: int) -> None:
            value = json.loads(repair05._registry_progress_payload(
                run_dir.name, trial_before, trial_append, state, count
            ))
            repair04._write_registry_progress_file(repair_dir / REGISTRY_PROGRESS, value)

        def append_registry() -> None:
            active = run_dir / "TRIAL_REGISTRY.jsonl"
            descriptor = os.open(
                active, os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                if repair03._read_locked_file(descriptor) != trial_before:
                    fail("repair-06 registry changed before append")
                write_progress("WRITE_IN_PROGRESS", 0)
                count = os.write(descriptor, trial_append)
                write_progress("WRITE_RETURNED", count)
                if count != len(trial_append):
                    fail("repair-06 registry append was partial")
                os.fsync(descriptor)
                if repair03._read_locked_file(descriptor) != trial_after:
                    fail("repair-06 registry changed during append")
                write_progress("APPEND_COMPLETE", count)
                written.add("TRIAL_REGISTRY.jsonl")
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

        os.replace(staging, repair_dir)
        repair03._fsync_directory(repair_root)
        staging = None
        published = True
        try:
            _transaction_fault_hook("publish_repair_dir")
            assert_boundary()
            _run_main_preflight(run_dir, source_dir, repair_dir / "post_repair")
            _transaction_fault_hook("main_preflight")
            assert_boundary()
            for row in active_mutations:
                relative = row["path"]
                if relative == "TRIAL_REGISTRY.jsonl":
                    append_registry()
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
            assert_boundary(updated_manifest)
        except BaseException as transaction_error:
            if published and repair_dir.exists():
                try:
                    repair05.recover_incomplete_transaction(run_dir, repair_dir)
                except BaseException as rollback_error:
                    raise Repair06RegistrationError(
                        f"repair-06 rollback blocked: {rollback_error}"
                    ) from transaction_error
            raise
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
    return {
        "status": "REGISTRATION_REPAIR06_REFROZEN",
        "repair_id": REPAIR_ID,
        "run_id": run_dir.name,
        "execution_commit": current_commit,
        "retry_requirement": RETRY_REQUIREMENT,
        "expected_success_resource": copy.deepcopy(SUCCESS_RESOURCE),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).parent)
    args = parser.parse_args(argv)
    result = repair06_registration(args.run_dir, args.source_dir)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
