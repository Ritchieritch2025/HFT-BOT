#!/usr/bin/env python3
"""Append-only consumer-validation wiring preregistration for repair-05.

Attempt 05 executed the frozen repair-04 query and stopped before producing
new RFQ state, input identity, scratch, or a research result.  The immutable
blocker shows that the repair-04 consumer looked for the Cycle-1 DuckDB
binding on an intermediate value that intentionally does not expose it.
Repair-05 changes only that validation wiring and authorizes a fresh,
non-resumed retry with repair-04's exact resource contract.

The registrar accepts only a clean real-Git direct child of the repair-04
commit with the exact six-file implementation/test scope.  It shares the
stable run-owner lock and the audited crash-safe registry CAS/recovery
primitives used by repair-04.  ``RUN_MANIFEST.json`` remains the final commit
marker.
"""

from __future__ import annotations

# Keep the provenance boundary ahead of shadowable standard-library imports.
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
import ast
import copy
import fcntl
import json
import re
import shutil
import stat
import tempfile
import types
from pathlib import Path, PurePosixPath
from typing import Any


_MISSING_MODULE = object()


def _load_sibling_source(
    module_name: str,
    dependencies: dict[str, types.ModuleType] | None = None,
) -> types.ModuleType:
    """Execute exact regular sibling source bytes, never sibling bytecode."""
    path = Path(__file__).with_name(f"{module_name}.py")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
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
        exec(
            compile(payload, str(path), "exec", dont_inherit=True),
            module.__dict__,
        )
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
    {"repair_registration": base, "repair02_registration": repair02},
)
repair04 = _load_sibling_source(
    "repair04_registration",
    {
        "repair_registration": base,
        "repair02_registration": repair02,
        "repair03_registration": repair03,
    },
)


class Repair05RegistrationError(repair04.Repair04RegistrationError):
    """Fail-closed repair-05 registration error."""


REPAIR_ID = "repair-05"
PARENT_REPAIR_ID = "repair-04"
PARENT_EXECUTION_COMMIT = "cc1e299f34dc6e8ef73f9b960cdd7a8ff36e47b8"
EXPECTED_STATUS = "CYCLE1_CORE_COMPLETE_RFQ_RESOURCE_REPAIR04_REGISTERED"
EXPECTED_REGISTRATION_STATE = (
    "RE_FROZEN_AFTER_RFQ_RESOURCE_RETUNE_REPAIR04_BEFORE_RFQ_RESULT"
)
POST_REPAIR_STATUS = (
    "CYCLE1_CORE_COMPLETE_RFQ_CONSUMER_WIRING_REPAIR05_REGISTERED"
)
REGISTRATION_STATE = (
    "RE_FROZEN_AFTER_RFQ_CONSUMER_WIRING_REPAIR05_BEFORE_RFQ_RESULT"
)
REPAIR_SCHEMA = "sports-autoresearch-consumer-validation-wiring-repair-v1"
WIRING_CONTRACT_SCHEMA = "rfq-consumer-validation-wiring-contract-v1"
AUTHORITY_SCHEMA = (
    "sports-autoresearch-consumer-validation-wiring-authority-v1"
)
JOURNAL_SCHEMA = "repair05-registration-transaction-v1"
REGISTRY_APPEND_PROGRESS_SCHEMA = "repair05-registry-append-progress-v2"
BLOCKER_SCHEMA = "rfq-repair04-consumer-pre-evidence-blocker-v1"
FINDING = "REPAIR04_CONSUMER_CYCLE1_BINDING_LOOKUP_KEYERROR"
FAILURE_DISPOSITION = "CONSUMER_PRE_EVIDENCE_VALIDATION_FAILURE_BEFORE_RESULT"
FAILURE_PHASE = "PRE_EVIDENCE_REPAIR04_CHAIN_VALIDATION"
ERROR_TYPE = "KeyError"
ERROR = "KeyError: 'cycle1_duckdb_binding'"
OFFENDING_FUNCTION = "_validate_repair04_failed_boundary"
OFFENDING_EXPRESSION = "base_result['cycle1_duckdb_binding']"
OFFENDING_LINE = 3822
CHANGE_CLASS = (
    "CONSUMER_VALIDATION_WIRING_CORRECTION_ONLY_NO_DATA_PARSER_QUERY_SEMANTICS_"
    "SELECTION_QUARANTINE_RESOURCE_CONTRACT_OR_HYPOTHESIS_CHANGE"
)
RETRY_REQUIREMENT = (
    "APPEND_ONLY_REPAIR05_CONSUMER_VALIDATION_WIRING_CORRECTION_AND_FRESH_"
    "SCRATCH_RETRY"
)
AUTHORITY_CLASS = (
    "MISSION_AUTHORIZED_AUTONOMOUS_RESEARCH_CODE_REPAIR_NO_NEW_SPEND_DATA_OR_"
    "FROZEN_VALIDATION_SET"
)
PERMITTED_CHANGE = "CONSUMER_VALIDATION_WIRING_CORRECTION_ONLY"
NO_RFQ_RESULT = "NO_RFQ_RESULT_OPENED"
CORE_DISPOSITION = "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED"
QUARANTINE_POLICY = (
    "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE_INHERITED_NO_CHANGE"
)
MISSION_SHA256 = (
    "9b4ca417dca394223ecdc6719cd1628bd5ad69d5a80003db304f73b1c485a69c"
)

SELECTION_FINGERPRINT = repair04.SELECTION_FINGERPRINT
RETAINED_FINGERPRINT = repair04.RETAINED_FINGERPRINT
QUARANTINED_FINGERPRINT = repair04.QUARANTINED_FINGERPRINT
FULL_FINGERPRINT = repair04.FULL_FINGERPRINT
RETAINED_OBJECTS = repair04.RETAINED_OBJECTS
RETAINED_BYTES = repair04.RETAINED_BYTES
RUNTIME_CONTRACT = copy.deepcopy(repair04.RUNTIME_CONTRACT)

FAILED_RESOURCE_SHA256 = (
    "e37d019ea272f9acacbe2c5b2464daebcb5a33b2eb54b1bf2665b3db6bcf11ef"
)
UNCHANGED_STATE_SHA256 = repair04.FAILED_STATE_SHA256
UNCHANGED_INPUT_SHA256 = repair04.FAILED_INPUT_SHA256
BLOCKER_SHA256 = (
    "42c2cc06c4b3073c0e6a1b5a681e86eb03d7a0f3afda8f694e2d0f0932fdaa5f"
)
PARENT_REGISTRATION_SHA256 = (
    "aabdb7cf507a4c92812b2c6b57f8da0f69fcd61d5e4a4e1502cf5e907cdb1db9"
)
PARENT_RESOURCE_CONTRACT_SHA256 = (
    "8c36b2747d9e5f991ecd5cf18e9c44fd5cea5f5725fc2a622113d42102e88325"
)
PARENT_RFQ_QUERY_SHA256 = (
    "3bf8b255f624aae4fc6f2a00a8e5ffb5037d701077435d5bddc3c4d093ba4ba4"
)
PARENT_RFQ_MODULE_AST_SHA256 = (
    "65308beea6f108d2d2aa4286beac0556efb801cba2bc37b1e21372824dfe85f8"
)
APPROVED_REPAIR05_RFQ_MODULE_AST_SHA256 = (
    "7ed9486f3859ece624a9d7430dfc621fbfc9c3a98fd8753c6dace193a4bca02c"
)

APPROVED_RFQ_AST_ADDITIONS = {
    "Import:copy",
    "Assign:REPAIR02_APPROVED_FULL_FINGERPRINT",
    "Assign:REPAIR02_APPROVED_QUARANTINED_FINGERPRINT",
    "Assign:REPAIR02_APPROVED_RETAINED_FINGERPRINT",
    "Assign:REPAIR02_APPROVED_SELECTION_FINGERPRINT",
    "Assign:REPAIR05_AUTHORITY_BASIS",
    "Assign:REPAIR05_AUTHORITY_CLASS",
    "Assign:REPAIR05_AUTHORITY_SCHEMA",
    "Assign:REPAIR05_BLOCKER_ACTIVE",
    "Assign:REPAIR05_BLOCKER_ARCHIVE",
    "Assign:REPAIR05_BLOCKER_SHA256",
    "Assign:REPAIR05_CHANGE_CLASS",
    "Assign:REPAIR05_CYCLE1_BINDING_ARCHIVE",
    "Assign:REPAIR05_EXECUTION_QUERY",
    "Assign:REPAIR05_FAILED_RESOURCE_ARCHIVE",
    "Assign:REPAIR05_FAILED_RESOURCE_SHA256",
    "Assign:REPAIR05_FAILED_RESOURCE_WALL_SECONDS",
    "Assign:REPAIR05_FAILURE_DISPOSITION",
    "Assign:REPAIR05_FAILURE_PHASE",
    "Assign:REPAIR05_FINDING",
    "Assign:REPAIR05_PARENT_RESOURCE_CONTRACT_SHA256",
    "Assign:REPAIR05_PARENT_RFQ_QUERY_SHA256",
    "Assign:REPAIR05_PRE_ROOT",
    "Assign:REPAIR05_REGISTRATION",
    "Assign:REPAIR05_REGISTRATION_STATE",
    "Assign:REPAIR05_RETRY_REQUIREMENT",
    "Assign:REPAIR05_ROOT",
    "Assign:REPAIR05_SCHEMA",
    "Assign:REPAIR05_STATUS",
    "Assign:REPAIR05_SUCCESS_RESOURCE_LABEL",
    "Assign:REPAIR05_SUCCESS_RESOURCE_PATH",
    "Assign:REPAIR05_TRANSACTION_JOURNAL",
    "Assign:REPAIR05_UNCHANGED_INPUT_ARCHIVE",
    "Assign:REPAIR05_UNCHANGED_STATE_ARCHIVE",
    "Assign:REPAIR05_W09_ATTESTATION_ARCHIVE",
    "Assign:REPAIR05_WIRING_CONTRACT",
    "Assign:REPAIR05_WIRING_SCHEMA",
    "FunctionDef:_apply_repair05_consumer_wiring",
    "FunctionDef:_enforce_registered_consumer_wiring_contract",
    "FunctionDef:_expected_repair05_authority_basis",
    "FunctionDef:_expected_repair05_trial_rows",
    "FunctionDef:_expected_repair05_wiring_contract",
    "FunctionDef:_repair05_retry_command",
    "FunctionDef:_validate_repair05_failure_evidence",
    "FunctionDef:_validate_repair05_fresh_scratch_absence",
}
APPROVED_RFQ_AST_MODIFICATIONS = {
    "FunctionDef:_apply_repair04_resource_contract",
    "FunctionDef:_validate_repair04_failed_boundary",
    "FunctionDef:apply_object_quarantine",
    "FunctionDef:build_summary",
    "FunctionDef:main",
}
PARENT_FINALIZER_MODULE_AST_SHA256 = (
    "798b39cabab1218f868d75e149e3b0d4604cec98077d2760bd22f27e18ad8acc"
)
APPROVED_REPAIR05_FINALIZER_MODULE_AST_SHA256 = (
    "af5c36c28a5cee05ee7d31abf9ae41f996178baff1485d2280181d3059ab817e"
)
APPROVED_FINALIZER_AST_ADDITIONS = {
    "Assign:ACTIVE_RFQ_REPAIR_RESOURCE_05",
    "Assign:EXPECTED_REPAIR_05_CHANGED_PATHS",
    "Assign:EXPECTED_RFQ_FAILED_RESOURCE_05_SHA256",
    "Assign:EXPECTED_RFQ_FAILED_RESOURCE_05_WALL_SECONDS",
    "Assign:EXPECTED_RFQ_REPAIR04_CONSUMER_BLOCKER_05_SHA256",
    "Assign:FAILED_RFQ_RESOURCE_05",
    "Assign:REPAIR05_PENDING_STATUS",
    "Assign:REPAIR_05_AUTHORITY_BASIS",
    "Assign:REPAIR_05_BLOCKER_ACTIVE",
    "Assign:REPAIR_05_BLOCKER_ARCHIVE",
    "Assign:REPAIR_05_EXECUTION_QUERY",
    "Assign:REPAIR_05_GIT_SOURCE_MODE",
    "Assign:REPAIR_05_PRE_ROOT",
    "Assign:REPAIR_05_ROOT",
    "Assign:REPAIR_05_SNAPSHOT_SOURCE_MODE",
    "Assign:REPAIR_05_SOURCE_ATTESTATION_ACTIVE",
    "Assign:REPAIR_05_SOURCE_ATTESTATION_ARCHIVE",
    "Assign:REPAIR_05_SOURCE_RELATIVE",
    "Assign:REPAIR_05_TRANSACTION_JOURNAL",
    "Assign:REPAIR_05_W09_ATTESTATION_ARCHIVE",
    "Assign:REPAIR_05_WIRING_CONTRACT",
    "Assign:REPAIR_REGISTRATION_RECEIPT_05",
    "Assign:UNCHANGED_RFQ_INPUT_IDENTITY_05",
    "Assign:UNCHANGED_RFQ_STATE_05",
    "FunctionDef:_repair05_expected_retry_command",
    "FunctionDef:_repair05_runtime_bindings",
    "FunctionDef:_validate_repair05_blocker_and_failed_resource",
    "FunctionDef:_validate_repair05_contracts",
    "FunctionDef:_validate_repair05_prefix_overlay",
    "FunctionDef:_validate_repair05_source_verification",
    "FunctionDef:_validate_repair05_success_outputs",
    "FunctionDef:_validate_repair05_transaction",
    "FunctionDef:validate_rfq_consumer_wiring_repair05",
}
APPROVED_FINALIZER_AST_MODIFICATIONS = {
    "FunctionDef:_validate_repair04_source_verification",
    "FunctionDef:finalize",
    "FunctionDef:validate_rfq_partial_quarantine",
    "FunctionDef:validate_rfq_resource_repair04",
}

TRIAL_IDS = (
    "RFQ_FULL_STAGE_ATTEMPT_05",
    "RFQ_CONSUMER_VALIDATION_WIRING_REPAIR_05",
)
SOURCE_RELATIVE = "sandbox/research/deep_autoresearch"
GIT_SOURCE_MODE = "LOCAL_GIT_CLEAN_COMMITTED_HEAD"
EXPECTED_REPAIR05_CHANGED_PATHS = [
    f"{SOURCE_RELATIVE}/finalize_mission.py",
    f"{SOURCE_RELATIVE}/repair05_registration.py",
    f"{SOURCE_RELATIVE}/rfq_full_stage.py",
    f"{SOURCE_RELATIVE}/test_finalize_mission.py",
    f"{SOURCE_RELATIVE}/test_repair05_registration.py",
    f"{SOURCE_RELATIVE}/test_rfq_full_stage.py",
]

STATE = Path("REPORT/tables/RFQ_FULL_STAGE_STATE.json")
INPUT_IDENTITY = Path("DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json")
RESOURCE = Path("logs/resources/rfq_full_stage_repair04.json")
BLOCKER = Path("DATA_INTEGRITY/RFQ_REPAIR04_CONSUMER_BLOCKER_05.json")
CYCLE1_BINDING = Path("DATA_INTEGRITY/CYCLE1_DUCKDB_BINDING.json")
W09_ATTESTATION = Path("DATA_INTEGRITY/W09_ATTESTATION.json")
SCRATCH = Path("cache/rfq_full_scratch.duckdb")
PRESERVED_SCRATCH = repair04.PRESERVED_SCRATCH
RFQ_SUMMARY = repair04.RFQ_SUMMARY
RFQ_REPORT = repair04.RFQ_REPORT
RFQ_CATALOG = repair04.RFQ_CATALOG
RETRY_EXECUTION_QUERY = Path("queries/rfq_full_stage.py")
WIRING_CONTRACT_FILE = "RFQ_CONSUMER_VALIDATION_WIRING_CONTRACT.json"
AUTHORITY_FILE = "AUTHORITY_BASIS.json"
TRANSACTION_JOURNAL = "TRANSACTION_JOURNAL.json"
REGISTRY_APPEND_PROGRESS = "REGISTRY_APPEND_PROGRESS.json"


def fail(message: str) -> None:
    raise Repair05RegistrationError(message)


def _transaction_fault_hook(boundary: str) -> None:
    """Unit-test hook; production execution intentionally does nothing."""
    del boundary


def _forward_fault(boundary: str) -> None:
    _transaction_fault_hook(boundary)


# This repair04 module is a private source-loaded instance.  Parameterize only
# its audited generic provenance/recovery primitives for the repair-05 owner.
repair04.REPAIR_ID = REPAIR_ID
repair04.PARENT_EXECUTION_COMMIT = PARENT_EXECUTION_COMMIT
repair04.JOURNAL_SCHEMA = JOURNAL_SCHEMA
repair04.REGISTRY_APPEND_PROGRESS_SCHEMA = REGISTRY_APPEND_PROGRESS_SCHEMA
repair04.TRANSACTION_JOURNAL = TRANSACTION_JOURNAL
repair04.REGISTRY_APPEND_PROGRESS = REGISTRY_APPEND_PROGRESS
repair04.EXPECTED_REPAIR04_CHANGED_PATHS = EXPECTED_REPAIR05_CHANGED_PATHS
repair04._transaction_fault_hook = _forward_fault


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
    return repair04._safe_relative_file_set(root)


def _expected_coverage() -> dict[str, Any]:
    return repair04._expected_coverage()


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


def expected_failed_command(run_id: str) -> list[str]:
    return expected_retry_command(run_id)


def make_wiring_contract(
    run_id: str,
    created_at: str,
    evidence: dict[str, Any],
    current_query_sha256: str,
) -> dict[str, Any]:
    """Canonical executable/scope contract consumed by RFQ and finalizer."""
    return {
        "schema_version": WIRING_CONTRACT_SCHEMA,
        "run_id": run_id,
        "created_at_utc": created_at,
        "mission_sha256": MISSION_SHA256,
        "repair_id": REPAIR_ID,
        "parent_repair_id": PARENT_REPAIR_ID,
        "finding": FINDING,
        "failure_disposition": FAILURE_DISPOSITION,
        "failure_phase": FAILURE_PHASE,
        "change_class": CHANGE_CLASS,
        "failed_attempt": {
            "attempt_id": "RFQ_FULL_STAGE_REPAIR04_ATTEMPT_05",
            "blocker_path": evidence["blocker_path"],
            "blocker_sha256": evidence["blocker_sha256"],
            "resource_path": evidence["failed_resource_receipt_path"],
            "resource_sha256": evidence["failed_resource_receipt_sha256"],
            "resource_label": "rfq_full_stage_repair04",
            "return_code": 1,
            "command": expected_failed_command(run_id),
            "error_type": ERROR_TYPE,
            "error": ERROR,
            "new_state_written": False,
            "new_input_identity_written": False,
            "analysis_stage_started": False,
            "active_state_path": STATE.as_posix(),
            "active_state_sha256": UNCHANGED_STATE_SHA256,
            "active_input_identity_path": INPUT_IDENTITY.as_posix(),
            "active_input_identity_sha256": UNCHANGED_INPUT_SHA256,
            "active_scratch_absent": True,
            "active_wal_absent": True,
        },
        "defect": {
            "offending_function": OFFENDING_FUNCTION,
            "offending_expression": OFFENDING_EXPRESSION,
            "offending_parent_source_path": RETRY_EXECUTION_QUERY.as_posix(),
            "offending_parent_source_sha256": PARENT_RFQ_QUERY_SHA256,
            "offending_parent_source_line": OFFENDING_LINE,
            "invalid_binding_source": "repair03_quarantine_consumer_base_result",
            "required_binding_source": (
                "repair04_and_repair03_registration_cycle1_duckdb_binding_"
                "plus_immutable_active_and_archived_binding"
            ),
        },
        "cycle1_binding": {
            "active_path": CYCLE1_BINDING.as_posix(),
            "active_sha256": evidence["cycle1_binding_sha256"],
            "archived_path": evidence["cycle1_binding_path"],
            "archived_sha256": evidence["cycle1_binding_sha256"],
            "registered_cycle1_duckdb_binding": copy.deepcopy(
                evidence["registered_cycle1_duckdb_binding"]
                if "registered_cycle1_duckdb_binding" in evidence
                else evidence["cycle1_duckdb_binding"]
            ),
        },
        "correction": {
            "scope": "CONSUMER_VALIDATION_WIRING_ONLY",
            "registered_query_path": RETRY_EXECUTION_QUERY.as_posix(),
            "parent_registered_query_sha256": PARENT_RFQ_QUERY_SHA256,
            "current_registered_query_sha256": current_query_sha256,
            "expected_command": expected_retry_command(run_id),
            "expected_success_resource": {
                "label": "rfq_full_stage_repair05",
                "path": "logs/resources/rfq_full_stage_repair05.json",
            },
            "fresh_scratch_required": True,
            "resume_allowed": False,
        },
        "inherited_contracts": {
            "repair04_resource_contract_path": (
                "DATA_INTEGRITY/repairs/repair-04/RFQ_RESOURCE_CONTRACT.json"
            ),
            "repair04_resource_contract_sha256": (
                PARENT_RESOURCE_CONTRACT_SHA256
            ),
            "runtime": copy.deepcopy(RUNTIME_CONTRACT),
            "selection_fingerprint_sha256": SELECTION_FINGERPRINT,
            "retained_object_set_sha256": RETAINED_FINGERPRINT,
            "quarantined_object_set_sha256": QUARANTINED_FINGERPRINT,
            "full_object_set_sha256": FULL_FINGERPRINT,
        },
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "resource_contract_change": "NONE",
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
    }


def make_authority_basis(
    run_id: str,
    recorded_at: str,
    wiring_contract_path: str,
    wiring_contract_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": AUTHORITY_SCHEMA,
        "run_id": run_id,
        "recorded_at_utc": recorded_at,
        "mission_sha256": MISSION_SHA256,
        "authority_class": AUTHORITY_CLASS,
        "permitted_change": PERMITTED_CHANGE,
        "wiring_contract_path": wiring_contract_path,
        "wiring_contract_sha256": wiring_contract_sha256,
        "new_instance_spend_authorized": False,
        "instance_resize_authorized": False,
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
    old_repository: dict[str, Any],
    current_identity: dict[str, Any],
    evidence: dict[str, Any],
    parent: dict[str, Any],
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
        "stage": "RFQ_FULL_STAGE_REPAIR04",
        "failure_class": FINDING,
        "failure_disposition": FAILURE_DISPOSITION,
        "failure_phase": FAILURE_PHASE,
        "error_type": ERROR_TYPE,
        "error": ERROR,
        "hypothesis_conclusion": "NONE",
        "blocker_path": evidence["blocker_path"],
        "blocker_sha256": evidence["blocker_sha256"],
        "failed_resource_receipt_path": evidence[
            "failed_resource_receipt_path"
        ],
        "failed_resource_receipt_sha256": evidence[
            "failed_resource_receipt_sha256"
        ],
        "unchanged_state_path": evidence["unchanged_state_path"],
        "unchanged_state_sha256": evidence["unchanged_state_sha256"],
        "unchanged_input_identity_path": evidence[
            "unchanged_input_identity_path"
        ],
        "unchanged_input_identity_sha256": evidence[
            "unchanged_input_identity_sha256"
        ],
        "cycle1_binding_path": evidence["cycle1_binding_path"],
        "cycle1_binding_sha256": evidence["cycle1_binding_sha256"],
        "new_state_written": False,
        "new_input_identity_written": False,
        "active_scratch_absent": True,
        "active_wal_absent": True,
        "resource_label": "rfq_full_stage_repair04",
        "return_code": 1,
        "resource_metrics": {
            "started_at_utc": resource["started_at_utc"],
            "completed_at_utc": resource["completed_at_utc"],
            "wall_seconds": resource["wall_seconds"],
            "cpu_user_seconds": resource["cpu_user_seconds"],
            "cpu_system_seconds": resource["cpu_system_seconds"],
            "peak_process_tree_rss_kib_polled": resource[
                "peak_process_tree_rss_kib_polled"
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
        "record_type": "CONSUMER_VALIDATION_WIRING_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR05_PREREGISTRATION",
        "finding": FINDING,
        "registration_change_class": CHANGE_CLASS,
        "rfq_result_state": NO_RFQ_RESULT,
        "retry_requirement": RETRY_REQUIREMENT,
        "wiring_contract_path": evidence["wiring_contract_path"],
        "wiring_contract_sha256": evidence["wiring_contract_sha256"],
        "authority_basis_path": evidence["authority_basis_path"],
        "authority_basis_sha256": evidence["authority_basis_sha256"],
        "resource_contract_path": parent["resource_contract_path"],
        "resource_contract_sha256": parent["resource_contract_sha256"],
        "runtime": copy.deepcopy(RUNTIME_CONTRACT),
        "expected_success_resource": {
            "label": "rfq_full_stage_repair05",
            "path": "logs/resources/rfq_full_stage_repair05.json",
        },
        "newly_quarantined_objects": [],
        "coverage": copy.deepcopy(parent["coverage"]),
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
        "resource_contract_change": "NONE",
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "core_results_recomputed": False,
    }
    return failure, preregistration


def _expected_failed_resource(run_id: str) -> dict[str, Any]:
    return {
        "command": expected_failed_command(run_id),
        "completed_at_utc": "2026-07-15T18:33:39.330283Z",
        "cost_rate_usd_per_hour": 0.4713,
        "cpu_hours": 0.005018,
        "cpu_system_seconds": 1.638,
        "cpu_user_seconds": 16.426,
        "cumulative_children_max_rss_kib": 35_976,
        "disk_free_after_bytes": 124_105_936_896,
        "disk_free_before_bytes": 124_105_936_896,
        "estimated_compute_cost_usd": 0.002372,
        "label": "rfq_full_stage_repair04",
        "minimum_disk_free_bytes_polled": 124_105_936_896,
        "peak_process_tree_rss_kib_polled": 35_988,
        "peak_stage_cache_bytes_polled": 114_093_269_135,
        "peak_temp_bytes_polled": 20_552,
        "poll_samples": 4,
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
            "verification is tracked separately and exposes no per-command "
            "S3 byte counter."
        ),
        "schema_version": "w09-stage-resource-v1",
        "started_at_utc": "2026-07-15T18:33:21.211957Z",
        "wall_seconds": 18.118,
    }


def _expected_blocker(run_id: str) -> dict[str, Any]:
    return {
        "active_input_identity_sha256": UNCHANGED_INPUT_SHA256,
        "active_scratch_absent": True,
        "active_state_sha256": UNCHANGED_STATE_SHA256,
        "active_wal_absent": True,
        "analysis_stage_started": False,
        "authority_basis": AUTHORITY_CLASS,
        "created_at_utc": "2026-07-15T18:37:00Z",
        "data_selection_change": "NONE",
        "dependent_rfq_result_opened": False,
        "error": ERROR,
        "error_type": ERROR_TYPE,
        "execution_commit": PARENT_EXECUTION_COMMIT,
        "failed_resource_receipt_path": RESOURCE.as_posix(),
        "failed_resource_receipt_sha256": FAILED_RESOURCE_SHA256,
        "failure_phase": FAILURE_PHASE,
        "finding": FINDING,
        "hypothesis_design_change": "NONE",
        "line_salvage": False,
        "mission_sha256": MISSION_SHA256,
        "new_input_identity_written": False,
        "new_state_written": False,
        "next_required_action": RETRY_REQUIREMENT,
        "offending_expression": OFFENDING_EXPRESSION,
        "offending_function": OFFENDING_FUNCTION,
        "parent_registration_path": (
            "DATA_INTEGRITY/repairs/repair-04/REPAIR_REGISTRATION.json"
        ),
        "parent_registration_sha256": PARENT_REGISTRATION_SHA256,
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "registered_query_path": RETRY_EXECUTION_QUERY.as_posix(),
        "registered_query_sha256": PARENT_RFQ_QUERY_SHA256,
        "repair04_resource_contract_path": (
            "DATA_INTEGRITY/repairs/repair-04/RFQ_RESOURCE_CONTRACT.json"
        ),
        "repair04_resource_contract_sha256": (
            PARENT_RESOURCE_CONTRACT_SHA256
        ),
        "resource_contract_change": "NONE",
        "resource_label": "rfq_full_stage_repair04",
        "resource_return_code": 1,
        "run_id": run_id,
        "schema_version": BLOCKER_SCHEMA,
        "source_change_scope": "CONSUMER_VALIDATION_WIRING_ONLY",
        "traceback_call_chain": [
            "main",
            "apply_object_quarantine",
            "_apply_repair04_resource_contract",
            OFFENDING_FUNCTION,
        ],
        "whole_object_quarantine_unchanged": True,
    }


def _read_registry_rows(payload: bytes) -> list[dict[str, Any]]:
    if not payload or not payload.endswith(b"\n"):
        fail("TRIAL_REGISTRY is empty or lacks its final newline")
    rows: list[dict[str, Any]] = []
    for number, raw in enumerate(payload.splitlines(), 1):
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            fail(f"TRIAL_REGISTRY line {number} is invalid JSON: {exc}")
        if not isinstance(value, dict):
            fail(f"TRIAL_REGISTRY line {number} is not an object")
        rows.append(value)
    return rows


def _validate_parent_transaction(run_dir: Path, parent: dict[str, Any]) -> None:
    repair_dir = run_dir / "DATA_INTEGRITY/repairs/repair-04"
    receipt_path = verify_bound_file(
        run_dir,
        parent.get("repair_receipt_path"),
        parent.get("repair_receipt_sha256"),
        "repair-04 registration receipt",
    )
    if parent.get("repair_receipt_sha256") != PARENT_REGISTRATION_SHA256:
        fail("repair-04 registration receipt is not the approved W09 boundary")
    receipt = load(receipt_path, "repair-04 registration receipt")
    without_self = copy.deepcopy(parent)
    without_self.pop("repair_receipt_path", None)
    without_self.pop("repair_receipt_sha256", None)
    if receipt != without_self:
        fail("repair-04 receipt/manifest record mismatch")
    journal_path = verify_bound_file(
        run_dir,
        parent.get("transaction_journal_path"),
        parent.get("transaction_journal_sha256"),
        "repair-04 transaction journal",
    )
    journal = load(journal_path, "repair-04 transaction journal")
    if (
        journal.get("schema_version") != "repair04-registration-transaction-v1"
        or journal.get("repair_id") != PARENT_REPAIR_ID
        or journal.get("run_id") != run_dir.name
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
        or _safe_relative_file_set(repair_dir)
        != set(journal.get("expected_repair_files", []))
    ):
        fail("repair-04 committed transaction journal mismatch")
    mutations = journal.get("active_mutations")
    if not isinstance(mutations, list) or not mutations:
        fail("repair-04 committed transaction mutations are missing")
    registry_rows = 0
    for row in mutations:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            fail("repair-04 committed transaction mutation is invalid")
        active = base.checked_run_path(
            run_dir, row["path"], "repair-04 committed active mutation"
        )
        if (
            active.is_symlink()
            or not active.is_file()
            or base.sha256(active) != row.get("replacement_sha256")
        ):
            fail(f"repair-04 committed active bytes changed: {row['path']}")
        if row.get("append_only_registry") is True:
            registry_rows += 1
            original = verify_bound_file(
                run_dir,
                row.get("original_archive_path"),
                row.get("original_sha256"),
                "repair-04 archived registry",
            ).read_bytes()
            if not active.read_bytes().startswith(original):
                fail("repair-04 registry is not append-only")
    if registry_rows != 1:
        fail("repair-04 transaction lacks one append-only registry mutation")


def validate_parent_repair(
    run_dir: Path,
    manifest: dict[str, Any],
) -> tuple[
    dict[str, Any], dict[str, Any], bytes, list[dict[str, Any]],
    list[dict[str, Any]], dict[str, Any], dict[str, Any],
]:
    """Validate the exact committed repair-04 boundary before mutation."""
    if (
        manifest.get("run_id") != run_dir.name
        or manifest.get("analysis_started") is not True
        or manifest.get("status") != EXPECTED_STATUS
        or manifest.get("registration_state") != EXPECTED_REGISTRATION_STATE
    ):
        fail("active manifest is not the registered repair-04 boundary")
    for result in (RFQ_SUMMARY, RFQ_REPORT, RFQ_CATALOG):
        if (run_dir / result).exists():
            fail("RFQ result exists; repair-05 preregistration is forbidden")
    records = manifest.get("data_integrity_repairs")
    if (
        not isinstance(records, list)
        or len(records) != 4
        or [row.get("repair_id") if isinstance(row, dict) else None for row in records]
        != ["repair-01", "repair-02", "repair-03", PARENT_REPAIR_ID]
    ):
        fail("repair-05 requires the immutable five-boundary parent chain")
    parent = records[-1]
    if (
        parent.get("schema_version")
        != "sports-autoresearch-resource-contract-repair-v1"
        or parent.get("parent_repair_id") != "repair-03"
        or parent.get("current_execution_commit") != PARENT_EXECUTION_COMMIT
        or parent.get("post_repair_status") != EXPECTED_STATUS
        or parent.get("rfq_result_state") != NO_RFQ_RESULT
        or parent.get("registered_rfq_query_sha256")
        != PARENT_RFQ_QUERY_SHA256
        or parent.get("resource_contract_sha256")
        != PARENT_RESOURCE_CONTRACT_SHA256
        or parent.get("coverage") != _expected_coverage()
        or parent.get("selection_identity_unchanged") is not True
        or parent.get("current_selection_fingerprint_sha256")
        != SELECTION_FINGERPRINT
        or parent.get("newly_quarantined_objects") != []
        or parent.get("data_selection_change") != "NONE"
        or parent.get("parser_contract_change") != "NONE"
        or parent.get("query_semantics_change") != "NONE"
        or parent.get("quarantine_change") != "NONE"
        or parent.get("hypothesis_design_change") != "NONE"
    ):
        fail("repair-04 parent identity/contracts changed")
    _validate_parent_transaction(run_dir, parent)

    contract = load(
        verify_bound_file(
            run_dir,
            parent.get("resource_contract_path"),
            parent.get("resource_contract_sha256"),
            "repair-04 resource contract",
        ),
        "repair-04 resource contract",
    )
    if (
        contract.get("schema_version") != repair04.RESOURCE_CONTRACT_SCHEMA
        or contract.get("current_runtime") != RUNTIME_CONTRACT
        or contract.get("expected_command") != expected_failed_command(run_dir.name)
        or contract.get("data_selection_change") != "NONE"
        or contract.get("parser_contract_change") != "NONE"
        or contract.get("query_semantics_change") != "NONE"
        or contract.get("quarantine_change") != "NONE"
        or contract.get("hypothesis_design_change") != "NONE"
    ):
        fail("repair-04 resource contract changed")

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
        or len(repository["identity_history"]) != 5
    ):
        fail("active repository identity is not repair-04")
    base.verify_source_sums_match_manifest(
        run_dir / "SOURCE_MANIFEST.json",
        run_dir / "SOURCE_SHA256SUMS.txt",
        "repair-04 current",
    )
    if (
        base.sha256(run_dir / "SOURCE_MANIFEST.json")
        != repository.get("source_manifest_sha256")
        or base.sha256(run_dir / "SOURCE_SHA256SUMS.txt")
        != repository.get("source_sha256s_sha256")
        or base.sha256(run_dir / "QUERY_SHA256SUMS.txt")
        != repository.get("query_set_sha256")
    ):
        fail("active repair-04 source/query receipts changed")
    checksums = base.parse_checksums(
        run_dir / "QUERY_SHA256SUMS.txt", "repair-04 query receipt"
    )
    if [relative for _, relative in checksums] != repository.get("query_files"):
        fail("active repair-04 query paths changed")
    for digest, relative in checksums:
        query = base.checked_run_path(run_dir, relative, "repair-04 query")
        if query.is_symlink() or not query.is_file() or base.sha256(query) != digest:
            fail(f"active repair-04 query changed: {relative}")
    query = run_dir / RETRY_EXECUTION_QUERY
    if base.sha256(query) != PARENT_RFQ_QUERY_SHA256:
        fail("repair-04 RFQ query is not the diagnosed frozen source")
    lines = query.read_text(encoding="utf-8").splitlines()
    if len(lines) < OFFENDING_LINE or lines[OFFENDING_LINE - 1].strip() != (
        '!= base_result["cycle1_duckdb_binding"]'
    ):
        fail("repair-04 diagnosed offending expression/line changed")

    registry_path = run_dir / "TRIAL_REGISTRY.jsonl"
    if registry_path.is_symlink() or not registry_path.is_file():
        fail("TRIAL_REGISTRY is missing or unsafe")
    registry = registry_path.read_bytes()
    rows = _read_registry_rows(registry)
    registration_ids = [
        row.get("trial_registration_id")
        for row in rows
        if row.get("trial_registration_id") is not None
    ]
    expected_ids = [
        "RFQ_FULL_STAGE_ATTEMPT_01",
        "RFQ_OBJECT_QUARANTINE_REPAIR_01",
        "RFQ_FULL_STAGE_ATTEMPT_02",
        "RFQ_OBJECT_QUARANTINE_REPAIR_02",
        "RFQ_FULL_STAGE_ATTEMPT_03",
        "RFQ_PARSER_CONTRACT_REPAIR_03",
        "RFQ_FULL_STAGE_ATTEMPT_04",
        "RFQ_RESOURCE_CONTRACT_REPAIR_04",
    ]
    trial = parent.get("trial_registry")
    if (
        registration_ids != expected_ids
        or not isinstance(trial, dict)
        or trial.get("trial_registration_ids") != expected_ids[-2:]
        or trial.get("appended_records") != 2
        or trial.get("strict_previous_bytes_prefix") is not True
        or trial.get("current_bytes") != len(registry)
        or trial.get("current_sha256") != base.sha256_bytes(registry)
    ):
        fail("TRIAL_REGISTRY is not the exact repair-04 append boundary")
    if any(
        str(row.get("result_artifact", "")).endswith(
            "RFQ_FULL_STAGE_SUMMARY.json"
        )
        for row in rows
    ):
        fail("TRIAL_REGISTRY already contains an RFQ full-stage result")

    core = base.core_result_inventory(run_dir)
    if parent.get("core_result_artifacts") != core:
        fail("Cycle-1 core artifacts changed after repair-04")
    _, cycle_binding = base.validate_cycle1_duckdb_binding(
        run_dir, run_dir / CYCLE1_BINDING
    )
    if cycle_binding != parent.get("cycle1_duckdb_binding"):
        fail("repair-04 changed the immutable Cycle-1 DuckDB binding")
    _, w09_sha = repair04.validate_w09_attestation(run_dir, manifest)
    return (
        parent,
        copy.deepcopy(repository),
        registry,
        rows,
        core,
        cycle_binding,
        {"sha256": w09_sha},
    )


def validate_failed_attempt(
    run_dir: Path,
    parent: dict[str, Any],
) -> dict[str, Any]:
    """Bind the pre-evidence KeyError and prove no new stage state exists."""
    paths = {
        "resource": run_dir / RESOURCE,
        "blocker": run_dir / BLOCKER,
        "state": run_dir / STATE,
        "input": run_dir / INPUT_IDENTITY,
    }
    expected_hashes = {
        "resource": FAILED_RESOURCE_SHA256,
        "blocker": BLOCKER_SHA256,
        "state": UNCHANGED_STATE_SHA256,
        "input": UNCHANGED_INPUT_SHA256,
    }
    for name, path in paths.items():
        if (
            path.is_symlink()
            or not path.is_file()
            or base.sha256(path) != expected_hashes[name]
        ):
            fail(f"attempt-05 {name} evidence changed")
    resource = load(paths["resource"], "attempt-05 resource receipt")
    if resource != _expected_failed_resource(run_dir.name):
        fail("attempt-05 resource receipt is not the exact rc=1 boundary")
    blocker = load(paths["blocker"], "attempt-05 blocker")
    if blocker != _expected_blocker(run_dir.name):
        fail("attempt-05 blocker is not the exact diagnosed boundary")
    if (
        blocker["parent_registration_sha256"]
        != parent.get("repair_receipt_sha256")
        or blocker["repair04_resource_contract_sha256"]
        != parent.get("resource_contract_sha256")
        or blocker["registered_query_sha256"]
        != parent.get("registered_rfq_query_sha256")
    ):
        fail("attempt-05 blocker is detached from repair-04")
    failed04 = parent.get("failed_attempt")
    if (
        not isinstance(failed04, dict)
        or failed04.get("state_sha256") != UNCHANGED_STATE_SHA256
        or failed04.get("input_identity_sha256") != UNCHANGED_INPUT_SHA256
    ):
        fail("attempt-05 active state/input are not unchanged from attempt-04")
    for field, digest in (
        ("state_path", UNCHANGED_STATE_SHA256),
        ("input_identity_path", UNCHANGED_INPUT_SHA256),
    ):
        verify_bound_file(run_dir, failed04.get(field), digest, f"repair-04 {field}")
    scratch = run_dir / SCRATCH
    wal = Path(str(scratch) + ".wal")
    if os.path.lexists(scratch) or os.path.lexists(wal):
        fail("attempt-05 must leave active scratch and WAL absent")
    return {
        "run_id": run_dir.name,
        "resource": resource,
        "blocker": blocker,
        "failed_resource_receipt_path": (
            "DATA_INTEGRITY/repairs/repair-05/pre_repair/"
            f"{RESOURCE.as_posix()}"
        ),
        "failed_resource_receipt_sha256": FAILED_RESOURCE_SHA256,
        "blocker_path": (
            "DATA_INTEGRITY/repairs/repair-05/pre_repair/"
            f"{BLOCKER.as_posix()}"
        ),
        "blocker_sha256": BLOCKER_SHA256,
        "unchanged_state_path": (
            "DATA_INTEGRITY/repairs/repair-05/pre_repair/"
            f"{STATE.as_posix()}"
        ),
        "unchanged_state_sha256": UNCHANGED_STATE_SHA256,
        "unchanged_input_identity_path": (
            "DATA_INTEGRITY/repairs/repair-05/pre_repair/"
            f"{INPUT_IDENTITY.as_posix()}"
        ),
        "unchanged_input_identity_sha256": UNCHANGED_INPUT_SHA256,
    }


def make_repair_record(
    *,
    applied_at: str,
    parent: dict[str, Any],
    evidence: dict[str, Any],
    old_repository: dict[str, Any],
    current_identity: dict[str, Any],
    identity_chain: list[dict[str, Any]],
    cycle_binding: dict[str, Any],
    core_before: list[dict[str, Any]],
    trial_before: bytes,
    trial_after: bytes,
    archive_rows: list[dict[str, Any]],
    transaction_journal_sha256: str,
) -> dict[str, Any]:
    """Build the exact receipt body (before its self path/hash are attached)."""
    return {
        "schema_version": REPAIR_SCHEMA,
        "repair_id": REPAIR_ID,
        "parent_repair_id": PARENT_REPAIR_ID,
        "applied_at_utc": applied_at,
        "pre_repair_status": EXPECTED_STATUS,
        "post_repair_status": POST_REPAIR_STATUS,
        "rfq_result_state": NO_RFQ_RESULT,
        "retry_requirement": RETRY_REQUIREMENT,
        "finding": FINDING,
        "failure_disposition": FAILURE_DISPOSITION,
        "failure_phase": FAILURE_PHASE,
        "registration_change_class": CHANGE_CLASS,
        "source_verification_mode": GIT_SOURCE_MODE,
        "quarantine_policy": QUARANTINE_POLICY,
        "previous_repair_registration_path": parent[
            "repair_receipt_path"
        ],
        "previous_repair_registration_sha256": parent[
            "repair_receipt_sha256"
        ],
        "previous_repair_record_sha256": canonical_hash(parent),
        "blocker_path": evidence["blocker_path"],
        "blocker_sha256": evidence["blocker_sha256"],
        "failed_resource_receipt_path": evidence[
            "failed_resource_receipt_path"
        ],
        "failed_resource_receipt_sha256": evidence[
            "failed_resource_receipt_sha256"
        ],
        "unchanged_state_path": evidence["unchanged_state_path"],
        "unchanged_state_sha256": evidence["unchanged_state_sha256"],
        "unchanged_input_identity_path": evidence[
            "unchanged_input_identity_path"
        ],
        "unchanged_input_identity_sha256": evidence[
            "unchanged_input_identity_sha256"
        ],
        "cycle1_binding_path": evidence["cycle1_binding_path"],
        "cycle1_binding_sha256": evidence["cycle1_binding_sha256"],
        "failed_attempt": {
            "attempt_id": "RFQ_FULL_STAGE_REPAIR04_ATTEMPT_05",
            "resource_active_path": RESOURCE.as_posix(),
            "resource_path": evidence["failed_resource_receipt_path"],
            "resource_sha256": evidence["failed_resource_receipt_sha256"],
            "resource_label": "rfq_full_stage_repair04",
            "return_code": 1,
            "resource_command": expected_failed_command(evidence["run_id"]),
            "resource_metrics": {
                key: evidence["resource"][key]
                for key in (
                    "started_at_utc", "completed_at_utc", "wall_seconds",
                    "cpu_user_seconds", "cpu_system_seconds", "cpu_hours",
                    "peak_process_tree_rss_kib_polled",
                    "cumulative_children_max_rss_kib",
                    "peak_temp_bytes_polled",
                    "minimum_disk_free_bytes_polled",
                    "disk_free_before_bytes", "disk_free_after_bytes",
                    "estimated_compute_cost_usd",
                )
            },
            "blocker_active_path": BLOCKER.as_posix(),
            "blocker_path": evidence["blocker_path"],
            "blocker_sha256": evidence["blocker_sha256"],
            "error_type": ERROR_TYPE,
            "error": ERROR,
            "failure_phase": FAILURE_PHASE,
            "offending_function": OFFENDING_FUNCTION,
            "offending_expression": OFFENDING_EXPRESSION,
            "new_state_written": False,
            "new_input_identity_written": False,
            "analysis_stage_started": False,
            "active_scratch_absent": True,
            "active_wal_absent": True,
            "unchanged_state_active_path": STATE.as_posix(),
            "unchanged_state_path": evidence["unchanged_state_path"],
            "unchanged_state_sha256": evidence["unchanged_state_sha256"],
            "unchanged_input_identity_active_path": (
                INPUT_IDENTITY.as_posix()
            ),
            "unchanged_input_identity_path": evidence[
                "unchanged_input_identity_path"
            ],
            "unchanged_input_identity_sha256": evidence[
                "unchanged_input_identity_sha256"
            ],
        },
        "wiring_contract_path": evidence["wiring_contract_path"],
        "wiring_contract_sha256": evidence["wiring_contract_sha256"],
        "wiring_contract": {
            "path": evidence["wiring_contract_path"],
            "sha256": evidence["wiring_contract_sha256"],
            "schema_version": WIRING_CONTRACT_SCHEMA,
            "expected_command": expected_retry_command(evidence["run_id"]),
        },
        "authority_basis_path": evidence["authority_basis_path"],
        "authority_basis_sha256": evidence["authority_basis_sha256"],
        "authority_basis": {
            "path": evidence["authority_basis_path"],
            "sha256": evidence["authority_basis_sha256"],
            "schema_version": AUTHORITY_SCHEMA,
            "authority_class": AUTHORITY_CLASS,
            "permitted_change": PERMITTED_CHANGE,
        },
        "resource_contract_path": parent["resource_contract_path"],
        "resource_contract_sha256": parent["resource_contract_sha256"],
        "resource_contract": copy.deepcopy(parent["resource_contract"]),
        "expected_success_resource": {
            "label": "rfq_full_stage_repair05",
            "path": "logs/resources/rfq_full_stage_repair05.json",
        },
        "registered_rfq_query_sha256": current_identity[
            "registered_rfq_query_sha256"
        ],
        "parent_registered_rfq_query_sha256": parent[
            "registered_rfq_query_sha256"
        ],
        "parser_contract_path": parent["parser_contract_path"],
        "parser_contract_sha256": parent["parser_contract_sha256"],
        "parser_contract": copy.deepcopy(parent["parser_contract"]),
        "newly_quarantined_objects": [],
        "cumulative_quarantined_objects": copy.deepcopy(
            parent["cumulative_quarantined_objects"]
        ),
        "coverage": copy.deepcopy(parent["coverage"]),
        "selection_identity_unchanged": True,
        "previous_selection_fingerprint_sha256": SELECTION_FINGERPRINT,
        "current_selection_fingerprint_sha256": SELECTION_FINGERPRINT,
        "cycle1_duckdb_binding": copy.deepcopy(cycle_binding),
        "previous_execution_commit": old_repository["execution_commit"],
        "current_execution_commit": current_identity["execution_commit"],
        "initial_repository_identity": copy.deepcopy(identity_chain[0]),
        "previous_repository_identity": base.repository_identity(
            old_repository
        ),
        "current_repository_identity": {
            key: copy.deepcopy(current_identity[key])
            for key in (
                "execution_commit", "source_manifest_sha256",
                "source_sha256s_sha256", "query_set_sha256", "query_files",
            )
        },
        "repository_identity_chain": copy.deepcopy(identity_chain),
        "previous_source_manifest_sha256": old_repository[
            "source_manifest_sha256"
        ],
        "current_source_manifest_sha256": current_identity[
            "source_manifest_sha256"
        ],
        "previous_source_sha256s_sha256": old_repository[
            "source_sha256s_sha256"
        ],
        "current_source_sha256s_sha256": current_identity[
            "source_sha256s_sha256"
        ],
        "previous_query_set_sha256": old_repository["query_set_sha256"],
        "current_query_set_sha256": current_identity["query_set_sha256"],
        "core_result_disposition": CORE_DISPOSITION,
        "core_results_recomputed": False,
        "core_result_artifacts": copy.deepcopy(core_before),
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
        "resource_contract_change": "NONE",
        "hypothesis_design_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "archive_path": "DATA_INTEGRITY/repairs/repair-05/pre_repair",
        "archive_inventory": copy.deepcopy(archive_rows),
        "transaction_journal_path": (
            "DATA_INTEGRITY/repairs/repair-05/TRANSACTION_JOURNAL.json"
        ),
        "transaction_journal_sha256": transaction_journal_sha256,
    }


# Explicit, version-neutral schema for every AST node present in the reviewed
# repair-04/repair-05 RFQ and finalizer modules.  Runtime ``ast._fields`` is
# deliberately not the serialization schema: Python 3.12 added ``type_params``
# to definition nodes, which makes ``ast.dump`` differ from Python 3.9 even for
# identical source.  A new node or field is semantic until reviewed, so it is
# rejected rather than silently omitted.
_CANONICAL_AST_FIELDS: dict[str, tuple[str, ...]] = {
    "Add": (),
    "And": (),
    "AnnAssign": ("target", "annotation", "value", "simple"),
    "Assign": ("targets", "value", "type_comment"),
    "Attribute": ("value", "attr", "ctx"),
    "AugAssign": ("target", "op", "value"),
    "BinOp": ("left", "op", "right"),
    "BitOr": (),
    "BoolOp": ("op", "values"),
    "Call": ("func", "args", "keywords"),
    "ClassDef": ("name", "bases", "keywords", "body", "decorator_list"),
    "Compare": ("left", "ops", "comparators"),
    "Constant": ("value", "kind"),
    "Continue": (),
    "Del": (),
    "Delete": ("targets",),
    "Dict": ("keys", "values"),
    "DictComp": ("key", "value", "generators"),
    "Div": (),
    "Eq": (),
    "ExceptHandler": ("type", "name", "body"),
    "Expr": ("value",),
    "FloorDiv": (),
    "For": ("target", "iter", "body", "orelse", "type_comment"),
    "FormattedValue": ("value", "conversion", "format_spec"),
    "FunctionDef": (
        "name", "args", "body", "decorator_list", "returns", "type_comment",
    ),
    "GeneratorExp": ("elt", "generators"),
    "Gt": (),
    "GtE": (),
    "If": ("test", "body", "orelse"),
    "IfExp": ("test", "body", "orelse"),
    "Import": ("names",),
    "ImportFrom": ("module", "names", "level"),
    "In": (),
    "Is": (),
    "IsNot": (),
    "JoinedStr": ("values",),
    "Lambda": ("args", "body"),
    "List": ("elts", "ctx"),
    "ListComp": ("elt", "generators"),
    "Load": (),
    "Lt": (),
    "LtE": (),
    "Module": ("body", "type_ignores"),
    "Mult": (),
    "Name": ("id", "ctx"),
    "Not": (),
    "NotEq": (),
    "NotIn": (),
    "Or": (),
    "Pow": (),
    "Raise": ("exc", "cause"),
    "Return": ("value",),
    "Set": ("elts",),
    "SetComp": ("elt", "generators"),
    "Slice": ("lower", "upper", "step"),
    "Starred": ("value", "ctx"),
    "Store": (),
    "Sub": (),
    "Subscript": ("value", "slice", "ctx"),
    "Try": ("body", "handlers", "orelse", "finalbody"),
    "Tuple": ("elts", "ctx"),
    "USub": (),
    "UnaryOp": ("op", "operand"),
    "With": ("items", "body", "type_comment"),
    "alias": ("name", "asname"),
    "arg": ("arg", "annotation", "type_comment"),
    "arguments": (
        "posonlyargs", "args", "vararg", "kwonlyargs", "kw_defaults",
        "kwarg", "defaults",
    ),
    "comprehension": ("target", "iter", "ifs", "is_async"),
    "keyword": ("arg", "value"),
    "withitem": ("context_expr", "optional_vars"),
}

# Python 3.12 compatibility field.  It is semantically neutral only when the
# parser supplied the exact empty list produced for ordinary 3.9-compatible
# definitions.  PEP 695 syntax/non-empty type parameters remain outside the
# frozen query language and fail closed.
_CANONICAL_AST_EMPTY_COMPAT_FIELDS: dict[str, frozenset[str]] = {
    "FunctionDef": frozenset({"type_params"}),
    "ClassDef": frozenset({"type_params"}),
}


def _canonical_ast_value(value: Any) -> Any:
    if isinstance(value, ast.AST):
        node_name = type(value).__name__
        expected = _CANONICAL_AST_FIELDS.get(node_name)
        if expected is None:
            fail(f"canonical AST schema rejects unknown node: {node_name}")
        raw_fields = getattr(value, "_fields", None)
        if (
            not isinstance(raw_fields, tuple)
            or any(not isinstance(field, str) for field in raw_fields)
            or len(raw_fields) != len(set(raw_fields))
        ):
            fail(f"canonical AST schema has invalid fields for {node_name}")
        actual = set(raw_fields)
        compatible = _CANONICAL_AST_EMPTY_COMPAT_FIELDS.get(
            node_name, frozenset()
        )
        unknown = actual - set(expected) - set(compatible)
        missing = set(expected) - actual
        if unknown:
            fail(
                "canonical AST schema rejects unknown semantic fields on "
                f"{node_name}: {sorted(unknown)}"
            )
        if missing:
            fail(
                f"canonical AST schema is missing fields on {node_name}: "
                f"{sorted(missing)}"
            )
        for field in compatible & actual:
            if not hasattr(value, field) or getattr(value, field) != []:
                fail(
                    "canonical AST schema rejects non-empty compatibility "
                    f"field {node_name}.{field}"
                )
        fields: list[Any] = []
        for field in expected:
            if not hasattr(value, field):
                fail(f"canonical AST node lacks {node_name}.{field}")
            fields.append([field, _canonical_ast_value(getattr(value, field))])
        return ["node", node_name, fields]
    if isinstance(value, list):
        return ["list", [_canonical_ast_value(item) for item in value]]
    if value is None:
        return ["none"]
    if value is Ellipsis:
        return ["ellipsis"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, complex):
        return ["complex", value.real.hex(), value.imag.hex()]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    fail(f"canonical AST schema rejects scalar type: {type(value).__name__}")


def _canonical_ast_payload(tree: ast.AST) -> bytes:
    return base.json_payload(_canonical_ast_value(tree))


def _canonical_ast_sha256(tree: ast.AST) -> str:
    return base.sha256_bytes(_canonical_ast_payload(tree))


def _ast_module_sha256(payload: bytes) -> str:
    try:
        tree = ast.parse(payload)
    except (SyntaxError, UnicodeDecodeError) as exc:
        fail(f"RFQ query cannot be parsed for AST-scope proof: {exc}")
    return _canonical_ast_sha256(tree)


def _top_level_ast_key(node: ast.AST, ordinal: int) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return f"{type(node).__name__}:{node.name}"
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        imports = ",".join(
            item.name + (f" as {item.asname}" if item.asname else "")
            for item in node.names
        )
        return f"{type(node).__name__}:{imports}"
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names: list[str] = []
        for target in targets:
            if isinstance(target, ast.Name):
                names.append(target.id)
            elif isinstance(target, (ast.Tuple, ast.List)):
                names.extend(
                    item.id for item in target.elts if isinstance(item, ast.Name)
                )
        if names:
            return f"{type(node).__name__}:{','.join(names)}"
    if (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ):
        return "DOCSTRING"
    if isinstance(node, ast.If):
        test = ast.dump(node.test, include_attributes=False)
        if "__name__" in test and "__main__" in test:
            return "If:__main__"
    return f"{type(node).__name__}:anonymous-{ordinal}"


def _top_level_ast_map(payload: bytes) -> tuple[list[str], dict[str, str]]:
    try:
        tree = ast.parse(payload)
    except (SyntaxError, UnicodeDecodeError) as exc:
        fail(f"RFQ query cannot be parsed for AST-delta proof: {exc}")
    order: list[str] = []
    output: dict[str, str] = {}
    for ordinal, node in enumerate(tree.body):
        key = _top_level_ast_key(node, ordinal)
        if key in output:
            fail(f"RFQ top-level AST key is duplicated: {key}")
        order.append(key)
        output[key] = _canonical_ast_payload(node).decode("utf-8")
    return order, output


def _validate_consumer_governance_ast_delta(
    parent_payload: bytes,
    current_payload: bytes,
    *,
    approved_additions: set[str] = APPROVED_RFQ_AST_ADDITIONS,
    approved_modifications: set[str] = APPROVED_RFQ_AST_MODIFICATIONS,
    expected_parent_ast_sha256: str = PARENT_RFQ_MODULE_AST_SHA256,
    expected_current_ast_sha256: str = APPROVED_REPAIR05_RFQ_MODULE_AST_SHA256,
) -> None:
    """Reject every RFQ AST change outside the reviewed governance delta.

    The exact full-module AST hashes pin the reviewed bodies of even the
    allowlisted nodes.  The structural allowlist separately proves that the
    only added/modified top-level nodes are repair-05 governance and its five
    explicit integration points.  Thus a change to ``statements``,
    ``scan_sql()``, an indirect SQL helper, or analysis logic fails closed even
    when all ``execute(...)`` call sites remain textually identical.
    """
    parent_sha = _ast_module_sha256(parent_payload)
    current_sha = _ast_module_sha256(current_payload)
    if parent_sha != expected_parent_ast_sha256:
        fail("repair-05 parent RFQ module AST is not the reviewed repair-04 AST")
    if current_sha != expected_current_ast_sha256:
        fail("repair-05 current RFQ module AST is not the reviewed wiring AST")
    parent_order, parent = _top_level_ast_map(parent_payload)
    current_order, current = _top_level_ast_map(current_payload)
    removed = set(parent) - set(current)
    added = set(current) - set(parent)
    modified = {
        key for key in set(parent) & set(current) if parent[key] != current[key]
    }
    if removed:
        fail(f"repair-05 RFQ AST removed top-level nodes: {sorted(removed)}")
    if added != approved_additions:
        fail(
            "repair-05 RFQ AST addition scope mismatch: "
            f"{sorted(added ^ approved_additions)}"
        )
    if modified != approved_modifications:
        fail(
            "repair-05 RFQ AST modification scope mismatch: "
            f"{sorted(modified ^ approved_modifications)}"
        )
    # Removing allowlisted additions must reproduce the exact parent ordering.
    if [key for key in current_order if key not in approved_additions] != parent_order:
        fail("repair-05 RFQ AST reordered the inherited top-level program")


def validate_source_provenance(
    run_dir: Path,
    source_dir: Path,
    manifest: dict[str, Any],
    old_repository: dict[str, Any],
) -> tuple[Path, str, bytes, bytes, list[tuple[str, str, bytes]]]:
    """Prove clean HEAD, direct parent, exact-six scope, and prior bytes."""
    repo_root, source_relative, current_commit = (
        repair04._verify_clean_source_provenance(source_dir)
    )
    repair04._validate_git_commit_scope(repo_root, current_commit)
    source_manifest, source_sums = repair04._validate_clean_head_source_binding(
        source_dir, repo_root, source_relative, current_commit
    )
    verified_repository, query_names = repair04._verify_prior_repository_provenance(
        run_dir,
        source_dir,
        repo_root,
        source_relative,
        current_commit,
        manifest,
    )
    if verified_repository != old_repository:
        fail("repair-05 prior repository proof changed the parent identity")
    parent_rfq = repair04._provenance_git_blob(
        repo_root,
        PARENT_EXECUTION_COMMIT,
        f"{SOURCE_RELATIVE}/rfq_full_stage.py",
    )
    current_rfq = (source_dir / "rfq_full_stage.py").read_bytes()
    _validate_consumer_governance_ast_delta(parent_rfq, current_rfq)
    if _sql_call_fingerprint(parent_rfq) != _sql_call_fingerprint(current_rfq):
        fail("repair-05 changed an RFQ SQL call expression")
    parent_finalizer = repair04._provenance_git_blob(
        repo_root,
        PARENT_EXECUTION_COMMIT,
        f"{SOURCE_RELATIVE}/finalize_mission.py",
    )
    current_finalizer = (source_dir / "finalize_mission.py").read_bytes()
    _validate_consumer_governance_ast_delta(
        parent_finalizer,
        current_finalizer,
        approved_additions=APPROVED_FINALIZER_AST_ADDITIONS,
        approved_modifications=APPROVED_FINALIZER_AST_MODIFICATIONS,
        expected_parent_ast_sha256=PARENT_FINALIZER_MODULE_AST_SHA256,
        expected_current_ast_sha256=(
            APPROVED_REPAIR05_FINALIZER_MODULE_AST_SHA256
        ),
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
        fail("repair-05 query path set differs from repair-04")
    return repo_root, current_commit, source_manifest, source_sums, query_rows


def _sql_call_fingerprint(payload: bytes) -> str:
    """Fingerprint every expression passed to a SQL-executing helper.

    The repair may add validation code, but SQL construction and invocation are
    outside its scope.  Attribute locations are omitted so inserted validation
    lines do not change the fingerprint.
    """
    try:
        tree = ast.parse(payload)
    except (SyntaxError, UnicodeDecodeError) as exc:
        fail(f"RFQ query cannot be parsed for SQL-scope proof: {exc}")
    sql_call_names = {
        "execute", "executemany", "sql", "scalar", "rows_as_dicts",
    }
    calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Attribute):
            name = function.attr
        elif isinstance(function, ast.Name):
            name = function.id
        else:
            continue
        if name not in sql_call_names:
            continue
        calls.append(_canonical_ast_value(node))
    return canonical_hash(calls)


def _registry_progress_payload(
    run_id: str,
    original: bytes,
    append: bytes,
    state: str,
    reported_bytes: int,
) -> bytes:
    return repair04._registry_progress_payload(
        run_id, original, append, state, reported_bytes
    )


def _durable_cas_replace(
    run_dir: Path,
    repair_dir: Path,
    mutation: dict[str, Any],
    label: str,
) -> None:
    """CAS through a deterministic, journal-owned displaced path.

    The original archive and prepared replacement remain below ``repair_dir``.
    If the process dies after displacement, recovery can prove ownership from
    the immutable journal plus exact hashes and restore without guessing a
    random temporary filename.
    """
    relative = mutation.get("path")
    if not isinstance(relative, str):
        fail(f"durable CAS mutation lacks a path: {label}")
    active = base.checked_run_path(run_dir, relative, f"durable CAS {label}")
    original = verify_bound_file(
        run_dir,
        mutation.get("original_archive_path"),
        mutation.get("original_sha256"),
        f"durable CAS original {label}",
    )
    replacement = _resolve_durable_replacement(
        run_dir, repair_dir, mutation, f"durable CAS replacement {label}"
    )
    displaced_relative = mutation.get("displaced_path")
    if not isinstance(displaced_relative, str):
        fail(f"durable CAS displaced path is missing: {label}")
    displaced = base.checked_run_path(
        run_dir, displaced_relative, f"durable CAS displaced {label}"
    )
    try:
        displaced.resolve().relative_to(repair_dir.resolve())
    except ValueError:
        fail(f"durable CAS displaced path escapes repair-05: {label}")
    displaced.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(displaced):
        fail(f"durable CAS displaced path already exists: {label}")
    expected = original.read_bytes()
    replacement_payload = replacement.read_bytes()
    if (
        active.is_symlink()
        or not active.is_file()
        or active.read_bytes() != expected
    ):
        fail(f"durable CAS active bytes changed before displacement: {label}")
    descriptor = os.open(replacement, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    repair03._fsync_directory(replacement.parent)
    _transaction_fault_hook(f"{label}:at_conditional_replace")
    os.rename(active, displaced)
    repair03._fsync_directory(active.parent)
    repair03._fsync_directory(displaced.parent)
    _transaction_fault_hook(f"{label}:after_displace_before_publish")
    if (
        displaced.is_symlink()
        or not displaced.is_file()
        or displaced.read_bytes() != expected
    ):
        # A last-window foreign mutation was displaced.  Restore those exact
        # foreign bytes to the now-missing live name; never replace them.
        try:
            os.link(displaced, active, follow_symlinks=False)
            repair03._fsync_directory(active.parent)
            displaced.unlink()
            repair03._fsync_directory(displaced.parent)
        except FileExistsError:
            pass
        fail(f"durable CAS compared unequal displaced bytes: {label}")
    try:
        os.link(replacement, active, follow_symlinks=False)
    except FileExistsError:
        fail(f"foreign live path appeared during durable CAS: {label}")
    repair03._fsync_directory(active.parent)
    _transaction_fault_hook(f"{label}:after_publish_before_displaced_cleanup")
    if active.is_symlink() or active.read_bytes() != replacement_payload:
        fail(f"durable CAS published bytes changed: {label}")
    displaced.unlink()
    repair03._fsync_directory(displaced.parent)
    if active.read_bytes() != replacement_payload:
        fail(f"durable CAS final verification failed: {label}")


def _durable_restore_staging_path(
    run_dir: Path,
    mutation: dict[str, Any],
) -> Path:
    relative = mutation.get("path")
    if not isinstance(relative, str):
        fail("repair-05 recovery mutation path is missing")
    restore_identity = canonical_hash({
        "schema": "repair05-durable-restore-staging-v1",
        "path": relative,
        "original_archive_path": mutation.get("original_archive_path"),
        "original_sha256": mutation.get("original_sha256"),
        "replacement_staging_path": mutation.get("replacement_staging_path"),
        "replacement_sha256": mutation.get("replacement_sha256"),
        "displaced_path": mutation.get("displaced_path"),
    })
    active_relative = PurePosixPath(relative)
    restore_relative = active_relative.parent / (
        f".{active_relative.name}.repair05-restore-{restore_identity}"
    )
    return base.checked_run_path(
        run_dir,
        restore_relative.as_posix(),
        "repair-05 recovery restore staging",
    )


def _restore_durable_mutation(
    run_dir: Path,
    repair_dir: Path,
    mutation: dict[str, Any],
) -> None:
    relative = mutation.get("path")
    if not isinstance(relative, str):
        fail("repair-05 recovery mutation path is missing")
    active = base.checked_run_path(run_dir, relative, "repair-05 recovery active")
    original = verify_bound_file(
        run_dir,
        mutation.get("original_archive_path"),
        mutation.get("original_sha256"),
        "repair-05 recovery original",
    )
    replacement = _resolve_durable_replacement(
        run_dir, repair_dir, mutation, "repair-05 recovery replacement"
    )
    displaced_relative = mutation.get("displaced_path")
    if not isinstance(displaced_relative, str):
        fail("repair-05 recovery displaced path is missing")
    displaced = base.checked_run_path(
        run_dir, displaced_relative, "repair-05 recovery displaced"
    )
    try:
        displaced.resolve().relative_to(repair_dir.resolve())
    except ValueError:
        fail("repair-05 recovery displaced path escapes repair directory")
    expected = original.read_bytes()
    replacement_payload = replacement.read_bytes()
    restore_staging = _durable_restore_staging_path(run_dir, mutation)

    def validate_restore_staging() -> None:
        if restore_staging.is_symlink() or not restore_staging.is_file():
            fail("repair-05 recovery restore staging is unsafe")
        if (
            restore_staging.read_bytes() != expected
            or not os.path.samestat(restore_staging.stat(), original.stat())
        ):
            fail("repair-05 recovery restore staging is foreign")

    def prepare_restore_staging() -> None:
        if os.path.lexists(restore_staging):
            validate_restore_staging()
        else:
            try:
                os.link(original, restore_staging, follow_symlinks=False)
            except FileExistsError:
                fail("foreign path raced repair-05 recovery staging")
            validate_restore_staging()
            descriptor = os.open(
                restore_staging,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            repair03._fsync_directory(restore_staging.parent)

    displaced_payload: bytes | None = None
    if os.path.lexists(displaced):
        if displaced.is_symlink() or not displaced.is_file():
            fail("repair-05 recovery displaced path is unsafe")
        displaced_payload = displaced.read_bytes()
        if displaced_payload != expected:
            fail("repair-05 recovery displaced bytes are foreign")
    if os.path.lexists(active):
        if active.is_symlink() or not active.is_file():
            fail("repair-05 recovery active path is unsafe")
        current = active.read_bytes()
        if current == expected:
            pass
        elif current == replacement_payload:
            # Publish the exact archived original with a same-directory atomic
            # replace.  A recovery crash can leave only the deterministic
            # hard-link staging name; active itself never has an unlink gap.
            prepare_restore_staging()
            _transaction_fault_hook(
                f"recovery:{relative}:after_restore_staging_before_replace"
            )
            if (
                active.is_symlink()
                or not active.is_file()
                or active.read_bytes() != replacement_payload
            ):
                fail("repair-05 recovery active bytes raced atomic restore")
            os.replace(restore_staging, active)
            repair03._fsync_directory(active.parent)
        else:
            fail("repair-05 recovery refuses foreign active bytes")
    else:
        # This also repairs the legacy recovery-of-recovery state produced by
        # the former unlink/link implementation: active missing, no displaced
        # name, but an exact journal-bound archived original remains.
        source = displaced if displaced_payload is not None else original
        try:
            os.link(source, active, follow_symlinks=False)
        except FileExistsError:
            fail("foreign path raced repair-05 missing-active recovery")
        repair03._fsync_directory(active.parent)
    if active.is_symlink() or active.read_bytes() != expected:
        fail("repair-05 durable recovery did not restore exact original bytes")
    if os.path.lexists(displaced):
        if displaced.is_symlink() or displaced.read_bytes() != expected:
            fail("repair-05 recovery refuses to remove foreign displaced bytes")
        displaced.unlink()
        repair03._fsync_directory(displaced.parent)
    if os.path.lexists(restore_staging):
        validate_restore_staging()
        restore_staging.unlink()
        repair03._fsync_directory(restore_staging.parent)


def _resolve_durable_replacement(
    run_dir: Path,
    repair_dir: Path,
    mutation: dict[str, Any],
    label: str,
) -> Path:
    relative = mutation.get("replacement_staging_path")
    if not isinstance(relative, str):
        fail(f"{label} path is missing")
    path = base.checked_run_path(run_dir, relative, label)
    try:
        path.resolve().relative_to(repair_dir.resolve())
    except ValueError:
        fail(f"{label} escapes repair-05")
    if path.is_symlink() or not path.is_file():
        fail(f"{label} is missing or unsafe")
    digest = mutation.get("replacement_sha256")
    if digest is not None:
        if base.sha256(path) != base.require_sha(digest, f"{label} SHA-256"):
            fail(f"{label} hash mismatch")
        return path
    if mutation.get("path") != "RUN_MANIFEST.json":
        fail(f"{label} lacks a replacement SHA")
    replacement = load(path, f"{label} manifest")
    journal_path = repair_dir / TRANSACTION_JOURNAL
    receipt_path = repair_dir / "REPAIR_REGISTRATION.json"
    records = replacement.get("data_integrity_repairs")
    if (
        replacement.get("run_id") != run_dir.name
        or replacement.get("status") != POST_REPAIR_STATUS
        or replacement.get("registration_state") != REGISTRATION_STATE
        or not isinstance(records, list)
        or len(records) != 5
        or records[-1].get("repair_id") != REPAIR_ID
        or records[-1].get("transaction_journal_sha256")
        != base.sha256(journal_path)
        or records[-1].get("repair_receipt_sha256") != base.sha256(receipt_path)
    ):
        fail(f"{label} is not the self-bound repair-05 manifest")
    return path


def _cleanup_repair05_tombstones(run_dir: Path, repair_dir: Path) -> None:
    candidates = list(
        repair_dir.parent.glob(f".{repair_dir.name}.rollback-complete-*")
    )
    if not candidates:
        return
    if len(candidates) != 1:
        fail("multiple repair-05 rollback tombstones exist")
    tombstone = candidates[0]
    if tombstone.is_symlink() or not tombstone.is_dir():
        fail("repair-05 rollback tombstone is unsafe")
    suffix = tombstone.name.rsplit("-", 1)[-1]
    manifest = run_dir / "RUN_MANIFEST.json"
    if (
        re.fullmatch(r"[0-9a-f]{64}", suffix) is None
        or manifest.is_symlink()
        or not manifest.is_file()
        or base.sha256(manifest) != suffix
    ):
        fail("repair-05 rollback tombstone lacks restored manifest identity")
    shutil.rmtree(tombstone)
    repair03._fsync_directory(tombstone.parent)


def _finish_committed_manifest_displacement(
    run_dir: Path,
    repair_dir: Path,
    active_manifest_payload: bytes,
) -> None:
    """Finish only the proven final-manifest forward-commit cleanup window."""
    if repair_dir.is_symlink() or not repair_dir.is_dir():
        fail("committed repair-05 directory is missing or unsafe")
    journal = load(
        repair_dir / TRANSACTION_JOURNAL,
        "repair-05 committed-cleanup transaction journal",
    )
    manifest_mutation = journal.get("manifest_mutation")
    expected_manifest_mutation = {
        "path": "RUN_MANIFEST.json",
        "original_archive_path": (
            "DATA_INTEGRITY/repairs/repair-05/pre_repair/RUN_MANIFEST.json"
        ),
        "replacement_staging_path": (
            "DATA_INTEGRITY/repairs/repair-05/post_repair/RUN_MANIFEST.json"
        ),
        "replacement_sha256": None,
        "displaced_path": (
            "DATA_INTEGRITY/repairs/repair-05/cas_displaced/"
            "RUN_MANIFEST.json.original"
        ),
        "append_only_registry": False,
    }
    if (
        journal.get("schema_version") != JOURNAL_SCHEMA
        or journal.get("repair_id") != REPAIR_ID
        or journal.get("run_id") != run_dir.name
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
        or not isinstance(manifest_mutation, dict)
        or set(manifest_mutation) != {
            *expected_manifest_mutation,
            "original_sha256",
        }
        or any(
            manifest_mutation.get(field) != value
            for field, value in expected_manifest_mutation.items()
        )
    ):
        fail("repair-05 committed manifest cleanup journal is invalid")
    displaced = base.checked_run_path(
        run_dir,
        manifest_mutation["displaced_path"],
        "repair-05 committed manifest displaced original",
    )
    restore_staging = _durable_restore_staging_path(run_dir, manifest_mutation)
    has_displaced = os.path.lexists(displaced)
    has_restore_staging = os.path.lexists(restore_staging)
    if not has_displaced and not has_restore_staging:
        return
    expected_files = journal.get("expected_repair_files")
    if (
        not isinstance(expected_files, list)
        or not expected_files
        or len(expected_files) != len(set(expected_files))
    ):
        fail("repair-05 committed manifest cleanup inventory is invalid")
    displaced_inside = displaced.relative_to(repair_dir).as_posix()
    permitted_files = set(expected_files)
    if has_displaced:
        permitted_files.add(displaced_inside)
    if _safe_relative_file_set(repair_dir) != permitted_files:
        fail("repair-05 committed manifest cleanup file set is foreign")
    original = verify_bound_file(
        run_dir,
        manifest_mutation.get("original_archive_path"),
        manifest_mutation.get("original_sha256"),
        "repair-05 committed manifest archived original",
    )
    replacement = _resolve_durable_replacement(
        run_dir,
        repair_dir,
        manifest_mutation,
        "repair-05 committed manifest staged replacement",
    )
    if (
        active_manifest_payload != replacement.read_bytes()
    ):
        fail("repair-05 committed manifest cleanup identity mismatch")
    if has_displaced and (
        displaced.is_symlink()
        or not displaced.is_file()
        or displaced.read_bytes() != original.read_bytes()
    ):
        fail("repair-05 committed manifest cleanup identity mismatch")
    if has_restore_staging and (
        restore_staging.is_symlink()
        or not restore_staging.is_file()
        or restore_staging.read_bytes() != original.read_bytes()
        or not os.path.samestat(restore_staging.stat(), original.stat())
    ):
        fail("repair-05 committed manifest restore staging is foreign")
    _transaction_fault_hook("committed_manifest:before_displaced_cleanup")
    if has_displaced:
        displaced.unlink()
        repair03._fsync_directory(displaced.parent)
    if has_restore_staging:
        restore_staging.unlink()
        repair03._fsync_directory(restore_staging.parent)


def recover_incomplete_transaction(run_dir: Path, repair_dir: Path) -> list[str]:
    """Restore deterministic CAS gaps and remove only repair-owned registry bytes."""
    if repair_dir.is_symlink() or not repair_dir.is_dir():
        fail("orphan repair-05 transaction directory is missing or unsafe")
    journal = load(repair_dir / TRANSACTION_JOURNAL, "repair-05 transaction journal")
    if (
        journal.get("schema_version") != JOURNAL_SCHEMA
        or journal.get("repair_id") != REPAIR_ID
        or journal.get("run_id") != run_dir.name
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
    ):
        fail("orphan repair-05 transaction lacks a valid journal")
    expected_files = journal.get("expected_repair_files")
    mutations = journal.get("active_mutations")
    manifest_mutation = journal.get("manifest_mutation")
    if (
        not isinstance(expected_files, list)
        or not expected_files
        or len(expected_files) != len(set(expected_files))
        or not isinstance(mutations, list)
        or not mutations
        or not isinstance(manifest_mutation, dict)
    ):
        fail("orphan repair-05 transaction journal shape is invalid")
    registry_rows = [
        row for row in mutations
        if isinstance(row, dict) and row.get("append_only_registry") is True
    ]
    durable_rows = [
        row for row in mutations
        if isinstance(row, dict) and row.get("append_only_registry") is False
    ]
    if len(registry_rows) != 1 or len(durable_rows) + 1 != len(mutations):
        fail("repair-05 transaction mutation classes are invalid")
    if manifest_mutation.get("append_only_registry") is not False:
        fail("repair-05 manifest mutation class is invalid")
    # The registry uses an audited append/rollback protocol and never durable
    # CAS displacement.  Treating its unused journal path as optional would let
    # a foreign file survive validation and then be deleted with repair_dir.
    displaced_relatives = {
        row.get("displaced_path") for row in [*durable_rows, manifest_mutation]
    }
    if None in displaced_relatives or not all(
        isinstance(path, str) for path in displaced_relatives
    ):
        fail("repair-05 journal displaced paths are invalid")
    actual_files = _safe_relative_file_set(repair_dir)
    expected_set = set(expected_files)
    optional_displaced = {
        str(Path(path).relative_to(repair_dir.relative_to(run_dir)))
        for path in displaced_relatives
    }
    if not expected_set.issubset(actual_files) or not actual_files.issubset(
        expected_set | optional_displaced
    ):
        fail("orphan repair-05 transaction file set mismatch")

    # Restore the manifest first so subsequent retries can parse the parent.
    _restore_durable_mutation(run_dir, repair_dir, manifest_mutation)
    for row in reversed(durable_rows):
        _restore_durable_mutation(run_dir, repair_dir, row)

    registry_row = registry_rows[0]
    active = base.checked_run_path(
        run_dir, registry_row["path"], "repair-05 recovery registry"
    )
    original = verify_bound_file(
        run_dir,
        registry_row["original_archive_path"],
        registry_row["original_sha256"],
        "repair-05 recovery original registry",
    ).read_bytes()
    replacement = verify_bound_file(
        run_dir,
        registry_row["replacement_staging_path"],
        registry_row["replacement_sha256"],
        "repair-05 recovery replacement registry",
    ).read_bytes()
    if not replacement.startswith(original):
        fail("repair-05 recovery registry replacement is not append-only")
    progress = load(
        repair_dir / REGISTRY_APPEND_PROGRESS,
        "repair-05 registry append progress",
    )
    repair04._validate_registry_progress(progress, run_dir.name)
    own_append = replacement[len(original):]
    if (
        not own_append
        or progress.get("original_sha256") != base.sha256_bytes(original)
        or progress.get("append_sha256") != base.sha256_bytes(own_append)
        or progress.get("append_bytes") != len(own_append)
        or progress.get("reported_bytes") > len(own_append)
    ):
        fail("repair-05 registry recovery ownership identity changed")
    progress_path = repair_dir / REGISTRY_APPEND_PROGRESS
    state = str(progress["state"])
    reported = progress["reported_bytes"]
    current = active.read_bytes()
    rollback_state = progress.get("rollback_state")
    if rollback_state in {"PREPARED", "COMPLETE"}:
        authorized_remove_bytes = 0
        if state == "APPEND_COMPLETE":
            authorized_remove_bytes = len(own_append)
        elif state == "WRITE_RETURNED" and reported > 0:
            authorized_remove_bytes = reported
        if (
            progress.get("rollback_remove_offset") != len(original)
            or progress.get("rollback_remove_bytes") != authorized_remove_bytes
            or authorized_remove_bytes <= 0
            or progress.get("rollback_replacement_bytes") < len(original)
        ):
            fail("repair-05 registry rollback marker lacks ownership proof")
        expected_matches = repair04._payload_matches_identity(
            current,
            progress.get("rollback_expected_bytes"),
            progress.get("rollback_expected_sha256"),
        )
        replacement_prefix_matches = repair04._payload_starts_with_identity(
            current,
            progress.get("rollback_replacement_bytes"),
            progress.get("rollback_replacement_sha256"),
        )
        if expected_matches:
            if rollback_state == "COMPLETE":
                fail("completed repair-05 registry rollback reverted")
            restored = repair04._reproduce_prepared_registry_rollback(
                current, progress
            )
            if restored != repair04._derive_registry_rollback(
                original, own_append, current, state, reported
            ):
                fail("prepared repair-05 registry rollback exceeds owned bytes")
            repair04._atomic_registry_replace_locked(active, current, restored)
            _transaction_fault_hook(
                "registry_recovery:after_replace_before_complete"
            )
            current = active.read_bytes()
            replacement_prefix_matches = repair04._payload_starts_with_identity(
                current,
                progress.get("rollback_replacement_bytes"),
                progress.get("rollback_replacement_sha256"),
            )
        if not replacement_prefix_matches:
            fail("repair-05 registry changed outside prepared rollback plan")
        planned_replacement = current[:progress["rollback_replacement_bytes"]]
        if (
            not planned_replacement.startswith(original)
            or planned_replacement[len(original):].startswith(own_append)
        ):
            fail("repair-05 registry rollback still contains owned append")
        if rollback_state == "PREPARED":
            progress = repair04._rollback_progress(progress, "COMPLETE")
            repair04._write_registry_progress_file(progress_path, progress)
    else:
        restored = repair04._derive_registry_rollback(
            original, own_append, current, state, reported
        )
        if restored != current:
            progress = repair04._rollback_progress(
                progress,
                "PREPARED",
                expected=current,
                replacement=restored,
                remove_offset=len(original),
            )
            repair04._write_registry_progress_file(progress_path, progress)
            _transaction_fault_hook("registry_recovery:after_rollback_plan")
            repair04._atomic_registry_replace_locked(active, current, restored)
            _transaction_fault_hook(
                "registry_recovery:after_replace_before_complete"
            )
            progress = repair04._rollback_progress(progress, "COMPLETE")
            repair04._write_registry_progress_file(progress_path, progress)
            current = active.read_bytes()
    if not current.startswith(original):
        fail("repair-05 registry recovery lost the parent prefix")

    manifest = run_dir / "RUN_MANIFEST.json"
    original_manifest_sha = base.require_sha(
        journal.get("original_manifest_sha256"),
        "repair-05 original manifest SHA",
    )
    if base.sha256(manifest) != original_manifest_sha:
        fail("repair-05 recovery did not restore the parent manifest")
    tombstone = repair_dir.with_name(
        f".{repair_dir.name}.rollback-complete-{original_manifest_sha}"
    )
    if os.path.lexists(tombstone):
        fail("repair-05 rollback tombstone already exists")
    os.replace(repair_dir, tombstone)
    repair03._fsync_directory(tombstone.parent)
    shutil.rmtree(tombstone)
    repair03._fsync_directory(tombstone.parent)
    return []


def _capture_validated_archive_payloads(
    run_dir: Path,
    manifest_raw: bytes,
    trial_before: bytes,
    old_repository: dict[str, Any],
    cycle_binding_sha256: str,
    w09_sha256: str,
) -> tuple[tuple[Path, ...], dict[Path, bytes]]:
    """Capture every already-validated parent byte before archive copying."""
    base_paths = (
        Path("RUN_MANIFEST.json"),
        Path("TRIAL_REGISTRY.jsonl"),
        Path("SOURCE_MANIFEST.json"),
        Path("SOURCE_SHA256SUMS.txt"),
        Path("QUERY_SHA256SUMS.txt"),
        STATE,
        INPUT_IDENTITY,
        RESOURCE,
        BLOCKER,
        CYCLE1_BINDING,
        W09_ATTESTATION,
    )
    expected_hashes: dict[Path, str] = {
        Path("RUN_MANIFEST.json"): base.sha256_bytes(manifest_raw),
        Path("TRIAL_REGISTRY.jsonl"): base.sha256_bytes(trial_before),
        Path("SOURCE_MANIFEST.json"): old_repository["source_manifest_sha256"],
        Path("SOURCE_SHA256SUMS.txt"): old_repository[
            "source_sha256s_sha256"
        ],
        Path("QUERY_SHA256SUMS.txt"): old_repository["query_set_sha256"],
        STATE: UNCHANGED_STATE_SHA256,
        INPUT_IDENTITY: UNCHANGED_INPUT_SHA256,
        RESOURCE: FAILED_RESOURCE_SHA256,
        BLOCKER: BLOCKER_SHA256,
        CYCLE1_BINDING: cycle_binding_sha256,
        W09_ATTESTATION: w09_sha256,
    }
    receipt_path = run_dir / "QUERY_SHA256SUMS.txt"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        fail("repair-05 query receipt disappeared before archive capture")
    receipt_payload = receipt_path.read_bytes()
    if base.sha256_bytes(receipt_payload) != old_repository["query_set_sha256"]:
        fail("repair-05 query receipt changed before archive capture")
    try:
        lines = receipt_payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        fail(f"repair-05 query receipt is not UTF-8: {exc}")
    query_paths: list[Path] = []
    for index, line in enumerate(lines):
        match = re.fullmatch(r"([0-9a-f]{64})  (queries/[A-Za-z0-9_.-]+\.py)", line)
        if match is None:
            fail(f"repair-05 query receipt line {index + 1} is malformed")
        digest, relative = match.groups()
        path = Path(relative)
        if path in expected_hashes:
            fail("repair-05 query receipt contains a duplicate path")
        expected_hashes[path] = digest
        query_paths.append(path)
    if [path.as_posix() for path in query_paths] != old_repository["query_files"]:
        fail("repair-05 query receipt paths changed before archive capture")
    archive_paths = base_paths + tuple(query_paths)
    payloads: dict[Path, bytes] = {}
    for relative in archive_paths:
        active = run_dir / relative
        if active.is_symlink() or not active.is_file():
            fail(f"repair-05 archive source is missing or unsafe: {relative}")
        payload = active.read_bytes()
        if base.sha256_bytes(payload) != expected_hashes[relative]:
            fail(f"repair-05 archive source changed after validation: {relative}")
        payloads[relative] = payload
    if payloads[Path("RUN_MANIFEST.json")] != manifest_raw:
        fail("repair-05 manifest changed after initial validation")
    if payloads[Path("TRIAL_REGISTRY.jsonl")] != trial_before:
        fail("repair-05 registry changed after initial validation")
    return archive_paths, payloads


def _validate_committed_contracts(
    run_dir: Path,
    record: dict[str, Any],
) -> None:
    wiring_path = verify_bound_file(
        run_dir,
        record.get("wiring_contract_path"),
        record.get("wiring_contract_sha256"),
        "repair-05 wiring contract",
    )
    wiring = load(wiring_path, "repair-05 wiring contract")
    expected = make_wiring_contract(
        run_dir.name,
        record.get("applied_at_utc"),
        record,
        record.get("registered_rfq_query_sha256"),
    )
    if wiring != expected:
        fail("repair-05 wiring contract changed")
    authority_path = verify_bound_file(
        run_dir,
        record.get("authority_basis_path"),
        record.get("authority_basis_sha256"),
        "repair-05 authority basis",
    )
    authority = load(authority_path, "repair-05 authority basis")
    if authority != make_authority_basis(
        run_dir.name,
        record.get("applied_at_utc"),
        record.get("wiring_contract_path"),
        record.get("wiring_contract_sha256"),
    ):
        fail("repair-05 authority basis changed")


def _validate_idempotent_immutable_boundary(
    run_dir: Path,
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
    parent: dict[str, Any],
    record: dict[str, Any],
) -> None:
    _, cycle_binding = base.validate_cycle1_duckdb_binding(
        run_dir, run_dir / CYCLE1_BINDING
    )
    cycle_archive = verify_bound_file(
        run_dir,
        record.get("cycle1_binding_path"),
        record.get("cycle1_binding_sha256"),
        "repair-05 archived Cycle-1 binding",
    )
    if (
        cycle_binding != record.get("cycle1_duckdb_binding")
        or cycle_binding != parent.get("cycle1_duckdb_binding")
        or (run_dir / CYCLE1_BINDING).read_bytes() != cycle_archive.read_bytes()
    ):
        fail("repair-05 active/archive Cycle-1 binding changed")
    if base.core_result_inventory(run_dir) != record.get("core_result_artifacts"):
        fail("repair-05 Cycle-1 core inventory changed")
    _, w09_sha = repair04.validate_w09_attestation(run_dir, manifest)
    w09_archive = run_dir / (
        "DATA_INTEGRITY/repairs/repair-05/pre_repair/"
        f"{W09_ATTESTATION.as_posix()}"
    )
    if (
        w09_archive.is_symlink()
        or not w09_archive.is_file()
        or base.sha256(w09_archive) != w09_sha
        or w09_archive.read_bytes() != (run_dir / W09_ATTESTATION).read_bytes()
    ):
        fail("repair-05 active/archive W09 attestation changed")
    for path_field, sha_field, label in (
        ("parser_contract_path", "parser_contract_sha256", "parser contract"),
        ("resource_contract_path", "resource_contract_sha256", "resource contract"),
    ):
        path = verify_bound_file(
            run_dir, record.get(path_field), record.get(sha_field), label
        )
        if (
            record.get(path_field) != parent.get(path_field)
            or record.get(sha_field) != parent.get(sha_field)
            or not path.is_file()
        ):
            fail(f"repair-05 inherited {label} changed")
    resource_contract = load(
        run_dir / str(record["resource_contract_path"]),
        "repair-05 inherited resource contract",
    )
    if (
        resource_contract.get("current_runtime") != RUNTIME_CONTRACT
        or resource_contract.get("expected_command")
        != expected_failed_command(run_dir.name)
        or record.get("resource_contract") != parent.get("resource_contract")
    ):
        fail("repair-05 inherited resource execution contract changed")
    if record.get("parser_contract") != parent.get("parser_contract"):
        fail("repair-05 inherited parser contract record changed")
    # Re-hash every prior preserved scratch through the same audited helper.
    repair04._capture_preserved_scratch_identities(run_dir, records)
    if os.path.lexists(run_dir / SCRATCH) or os.path.lexists(
        Path(str(run_dir / SCRATCH) + ".wal")
    ):
        fail("repair-05 idempotence requires active scratch/WAL absence")


def validate_already_applied(
    run_dir: Path,
    source_dir: Path,
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    if (
        manifest.get("status") != POST_REPAIR_STATUS
        or manifest.get("registration_state") != REGISTRATION_STATE
        or len(records) != 5
        or [row.get("repair_id") if isinstance(row, dict) else None for row in records]
        != ["repair-01", "repair-02", "repair-03", "repair-04", REPAIR_ID]
    ):
        fail("unknown or inconsistent existing repair-05 history")
    parent, record = records[-2], records[-1]
    if (
        record.get("schema_version") != REPAIR_SCHEMA
        or record.get("parent_repair_id") != PARENT_REPAIR_ID
        or record.get("pre_repair_status") != EXPECTED_STATUS
        or record.get("post_repair_status") != POST_REPAIR_STATUS
        or record.get("finding") != FINDING
        or record.get("failure_disposition") != FAILURE_DISPOSITION
        or record.get("failure_phase") != FAILURE_PHASE
        or record.get("registration_change_class") != CHANGE_CLASS
        or record.get("rfq_result_state") != NO_RFQ_RESULT
        or record.get("retry_requirement") != RETRY_REQUIREMENT
        or record.get("previous_repair_registration_path")
        != parent.get("repair_receipt_path")
        or record.get("previous_repair_registration_sha256")
        != parent.get("repair_receipt_sha256")
        or record.get("previous_repair_record_sha256") != canonical_hash(parent)
        or record.get("coverage") != parent.get("coverage")
        or record.get("cycle1_duckdb_binding")
        != parent.get("cycle1_duckdb_binding")
        or record.get("resource_contract_sha256")
        != parent.get("resource_contract_sha256")
    ):
        fail("repair-04/repair-05 append chain changed")
    repair_dir = run_dir / "DATA_INTEGRITY/repairs/repair-05"
    receipt = load(
        verify_bound_file(
            run_dir,
            record.get("repair_receipt_path"),
            record.get("repair_receipt_sha256"),
            "repair-05 registration receipt",
        ),
        "repair-05 registration receipt",
    )
    without_self = copy.deepcopy(record)
    without_self.pop("repair_receipt_path", None)
    without_self.pop("repair_receipt_sha256", None)
    if receipt != without_self:
        fail("repair-05 receipt/manifest record mismatch")
    for path_field, sha_field in (
        ("blocker_path", "blocker_sha256"),
        ("failed_resource_receipt_path", "failed_resource_receipt_sha256"),
        ("unchanged_state_path", "unchanged_state_sha256"),
        (
            "unchanged_input_identity_path",
            "unchanged_input_identity_sha256",
        ),
        ("cycle1_binding_path", "cycle1_binding_sha256"),
    ):
        verify_bound_file(
            run_dir,
            record.get(path_field),
            record.get(sha_field),
            f"repair-05 {path_field}",
        )
    archive = repair_dir / "pre_repair"
    if (
        record.get("archive_path")
        != "DATA_INTEGRITY/repairs/repair-05/pre_repair"
        or record.get("archive_inventory") != base.archive_inventory(archive)
    ):
        fail("repair-05 archive inventory changed")
    journal = load(
        verify_bound_file(
            run_dir,
            record.get("transaction_journal_path"),
            record.get("transaction_journal_sha256"),
            "repair-05 transaction journal",
        ),
        "repair-05 transaction journal",
    )
    if (
        journal.get("schema_version") != JOURNAL_SCHEMA
        or journal.get("repair_id") != REPAIR_ID
        or journal.get("run_id") != run_dir.name
        or journal.get("state") != "PREPARED_BEFORE_ACTIVE_MUTATION"
        or _safe_relative_file_set(repair_dir)
        != set(journal.get("expected_repair_files", []))
    ):
        fail("repair-05 committed transaction changed")
    for row in journal.get("active_mutations", []):
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            fail("repair-05 transaction mutation is invalid")
        active = base.checked_run_path(run_dir, row["path"], "repair-05 active")
        if active.is_symlink() or base.sha256(active) != row.get(
            "replacement_sha256"
        ):
            fail(f"repair-05 active bytes changed: {row['path']}")
    manifest_mutation = journal.get("manifest_mutation")
    if not isinstance(manifest_mutation, dict):
        fail("repair-05 committed manifest mutation is missing")
    committed_manifest = _resolve_durable_replacement(
        run_dir,
        repair_dir,
        manifest_mutation,
        "repair-05 committed manifest replacement",
    )
    active_manifest = run_dir / "RUN_MANIFEST.json"
    if (
        active_manifest.is_symlink()
        or active_manifest.read_bytes() != committed_manifest.read_bytes()
    ):
        fail("repair-05 committed manifest replacement changed")
    registry_before = (
        repair_dir / "pre_repair/TRIAL_REGISTRY.jsonl"
    ).read_bytes()
    registry_after = (run_dir / "TRIAL_REGISTRY.jsonl").read_bytes()
    append = registry_after[len(registry_before):]
    if (
        not registry_after.startswith(registry_before)
        or not append
        or (repair_dir / REGISTRY_APPEND_PROGRESS).read_bytes()
        != _registry_progress_payload(
            run_dir.name, registry_before, append, "APPEND_COMPLETE", len(append)
        )
    ):
        fail("repair-05 registry append/progress changed")
    _validate_committed_contracts(run_dir, record)
    validate_failed_attempt(run_dir, parent)
    _validate_idempotent_immutable_boundary(
        run_dir, manifest, records, parent, record
    )
    repository = manifest.get("repository")
    if (
        not isinstance(repository, dict)
        or repository.get("execution_commit")
        != record.get("current_execution_commit")
        or repository.get("registration_repair_id") != REPAIR_ID
        or repository.get("identity_history")
        != record.get("repository_identity_chain")
        or len(repository.get("identity_history", [])) != 6
    ):
        fail("repair-05 repository identity changed")
    repo_root, source_relative, commit = (
        repair04._verify_clean_source_provenance(source_dir)
    )
    repair04._validate_git_commit_scope(repo_root, commit)
    source_manifest, source_sums = repair04._validate_clean_head_source_binding(
        source_dir, repo_root, source_relative, commit
    )
    if (
        commit != repository["execution_commit"]
        or base.sha256_bytes(source_manifest)
        != repository["source_manifest_sha256"]
        or base.sha256_bytes(source_sums) != repository["source_sha256s_sha256"]
        or (run_dir / "SOURCE_MANIFEST.json").read_bytes() != source_manifest
        or (run_dir / "SOURCE_SHA256SUMS.txt").read_bytes() != source_sums
    ):
        fail("repair-05 clean Git/source binding changed")
    for result in (RFQ_SUMMARY, RFQ_REPORT, RFQ_CATALOG):
        if (run_dir / result).exists():
            fail("RFQ result appeared after repair-05 preregistration")
    if os.path.lexists(run_dir / SCRATCH) or os.path.lexists(
        Path(str(run_dir / SCRATCH) + ".wal")
    ):
        fail("repair-05 idempotence requires fresh scratch absence")
    return {
        "status": "REGISTRATION_REPAIR05_ALREADY_REFROZEN",
        "repair_id": REPAIR_ID,
        "run_id": run_dir.name,
        "execution_commit": repository["execution_commit"],
        "source_verification_mode": GIT_SOURCE_MODE,
        "retained_objects": RETAINED_OBJECTS,
        "retained_bytes": RETAINED_BYTES,
        "retained_selection_fingerprint_sha256": SELECTION_FINGERPRINT,
        "rfq_result_state": NO_RFQ_RESULT,
        "retry_requirement": RETRY_REQUIREMENT,
        "resource_contract": copy.deepcopy(RUNTIME_CONTRACT),
    }


def repair05_registration(
    run_dir: Path,
    source_dir: Path,
    *,
    repo_root: Path | None = None,
    execution_commit: str | None = None,
) -> dict[str, Any]:
    if repo_root is not None or execution_commit is not None:
        fail("repair-05 accepts only a clean real-Git HEAD proof")
    if Path(run_dir).is_symlink():
        fail("run-dir must not be a symlink")
    resolved = Path(run_dir).resolve()
    with repair03._exclusive_run_lock(resolved):
        return _repair05_registration_locked(resolved, source_dir)


def _repair05_registration_locked(
    run_dir: Path,
    source_dir: Path,
) -> dict[str, Any]:
    if Path(source_dir).is_symlink():
        fail("source-dir must not be a symlink")
    source_dir = Path(source_dir).resolve()
    repair_dir = run_dir / "DATA_INTEGRITY/repairs/repair-05"
    _cleanup_repair05_tombstones(run_dir, repair_dir)
    manifest_path = run_dir / "RUN_MANIFEST.json"
    if not os.path.lexists(manifest_path) and repair_dir.exists():
        recover_incomplete_transaction(run_dir, repair_dir)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        fail("RUN_MANIFEST.json is missing or unsafe")
    manifest_raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"RUN_MANIFEST.json is invalid JSON: {exc}")
    if not isinstance(manifest, dict) or manifest.get("run_id") != run_dir.name:
        fail("RUN_MANIFEST identity mismatch")
    records = manifest.get("data_integrity_repairs")
    if not isinstance(records, list):
        fail("data_integrity_repairs must be an append-only array")
    if len(records) == 5:
        _finish_committed_manifest_displacement(
            run_dir, repair_dir, manifest_raw
        )
        return validate_already_applied(run_dir, source_dir, manifest, records)
    if len(records) != 4:
        fail("repair-05 requires exactly repair-01 through repair-04")

    if repair_dir.exists():
        recover_incomplete_transaction(run_dir, repair_dir)
        if repair_dir.exists():
            fail("repair-05 incomplete transaction recovery did not finish")
        manifest_raw = manifest_path.read_bytes()
        manifest = json.loads(manifest_raw)
        records = manifest.get("data_integrity_repairs")
        if not isinstance(records, list) or len(records) != 4:
            fail("repair-05 recovery did not restore the four-repair parent")

    (
        parent,
        old_repository,
        trial_before,
        _,
        core_before,
        cycle_binding,
        w09,
    ) = validate_parent_repair(run_dir, manifest)
    evidence = validate_failed_attempt(run_dir, parent)
    evidence.update({
        "cycle1_binding_path": (
            "DATA_INTEGRITY/repairs/repair-05/pre_repair/"
            f"{CYCLE1_BINDING.as_posix()}"
        ),
        "cycle1_binding_sha256": base.sha256(run_dir / CYCLE1_BINDING),
        "registered_cycle1_duckdb_binding": copy.deepcopy(cycle_binding),
    })
    scratch_identities = repair04._capture_preserved_scratch_identities(
        run_dir, records
    )
    (
        source_repo_root,
        current_commit,
        source_manifest_payload,
        source_sums_payload,
        query_rows,
    ) = validate_source_provenance(run_dir, source_dir, manifest, old_repository)
    source_manifest_sha = base.sha256_bytes(source_manifest_payload)
    source_sums_sha = base.sha256_bytes(source_sums_payload)
    query_sums_payload = "".join(
        f"{digest}  {relative}\n" for digest, relative, _ in query_rows
    ).encode("utf-8")
    query_sha = base.sha256_bytes(query_sums_payload)
    rfq_rows = [row for row in query_rows if row[1] == RETRY_EXECUTION_QUERY.as_posix()]
    if len(rfq_rows) != 1:
        fail("repair-05 requires one RFQ execution query")
    registered_query_sha = rfq_rows[0][0]
    repository_identity = {
        "execution_commit": current_commit,
        "source_manifest_sha256": source_manifest_sha,
        "source_sha256s_sha256": source_sums_sha,
        "query_set_sha256": query_sha,
        "query_files": copy.deepcopy(old_repository["query_files"]),
    }
    current_identity = {
        **repository_identity,
        "registered_rfq_query_sha256": registered_query_sha,
    }
    old_chain = old_repository.get("identity_history")
    if (
        not isinstance(old_chain, list)
        or len(old_chain) != 5
        or old_chain != parent.get("repository_identity_chain")
        or old_chain[-1] != base.repository_identity(old_repository)
    ):
        fail("repair-04 repository chain is not the exact five-identity chain")
    identity_chain = copy.deepcopy(old_chain) + [repository_identity]
    if len({canonical_hash(row) for row in identity_chain}) != 6:
        fail("repair-05 repository chain does not contain six distinct identities")

    archive_paths, validated_archive_payloads = (
        _capture_validated_archive_payloads(
            run_dir,
            manifest_raw,
            trial_before,
            old_repository,
            evidence["cycle1_binding_sha256"],
            w09["sha256"],
        )
    )

    applied_at = base.now_utc()
    repair_root = run_dir / "DATA_INTEGRITY/repairs"
    repair_root.mkdir(parents=True, exist_ok=True)
    staging: Path | None = Path(
        tempfile.mkdtemp(prefix=".repair-05-", dir=repair_root)
    )
    try:
        pre = staging / "pre_repair"
        post = staging / "post_repair"
        _transaction_fault_hook("archive_copy:before")
        for relative in archive_paths:
            base.copy_exact(run_dir / relative, pre / relative)
            if (
                (run_dir / relative).read_bytes()
                != validated_archive_payloads[relative]
                or (pre / relative).read_bytes()
                != validated_archive_payloads[relative]
            ):
                fail(f"repair-05 archive copy raced validation: {relative}")
        for relative, expected_payload in validated_archive_payloads.items():
            if (
                (run_dir / relative).read_bytes() != expected_payload
                or (pre / relative).read_bytes() != expected_payload
            ):
                fail(f"repair-05 archive boundary changed during copy: {relative}")
        archive_rows = base.archive_inventory(pre)

        base.atomic_write(post / "SOURCE_MANIFEST.json", source_manifest_payload)
        base.atomic_write(post / "SOURCE_SHA256SUMS.txt", source_sums_payload)
        base.atomic_write(post / "QUERY_SHA256SUMS.txt", query_sums_payload)
        for _, relative, payload in query_rows:
            base.atomic_write(post / relative, payload)

        prefix = "DATA_INTEGRITY/repairs/repair-05"
        wiring_relative = f"{prefix}/{WIRING_CONTRACT_FILE}"
        evidence["wiring_contract_path"] = wiring_relative
        wiring = make_wiring_contract(
            run_dir.name, applied_at, evidence, registered_query_sha
        )
        base.atomic_write(staging / WIRING_CONTRACT_FILE, base.json_payload(wiring))
        evidence["wiring_contract_sha256"] = base.sha256(
            staging / WIRING_CONTRACT_FILE
        )
        authority_relative = f"{prefix}/{AUTHORITY_FILE}"
        evidence["authority_basis_path"] = authority_relative
        authority = make_authority_basis(
            run_dir.name,
            applied_at,
            wiring_relative,
            evidence["wiring_contract_sha256"],
        )
        base.atomic_write(staging / AUTHORITY_FILE, base.json_payload(authority))
        evidence["authority_basis_sha256"] = base.sha256(staging / AUTHORITY_FILE)

        trial_failure, trial_prereg = make_trial_records(
            applied_at,
            old_repository,
            current_identity,
            evidence,
            parent,
            evidence["resource"],
        )
        trial_after = (
            trial_before
            + base.registry_line(trial_failure)
            + base.registry_line(trial_prereg)
        )
        base.atomic_write(post / "TRIAL_REGISTRY.jsonl", trial_after)
        trial_append = trial_after[len(trial_before):]
        base.atomic_write(
            staging / REGISTRY_APPEND_PROGRESS,
            _registry_progress_payload(
                run_dir.name, trial_before, trial_append, "NOT_STARTED", 0
            ),
        )

        mutation_relatives = [
            "SOURCE_MANIFEST.json",
            "SOURCE_SHA256SUMS.txt",
            "QUERY_SHA256SUMS.txt",
            *old_repository["query_files"],
            "TRIAL_REGISTRY.jsonl",
        ]
        if len(mutation_relatives) != len(set(mutation_relatives)):
            fail("repair-05 active mutation paths are duplicated")
        active_mutations: list[dict[str, Any]] = []
        for relative in mutation_relatives:
            original = pre / relative
            replacement = post / relative
            if not original.is_file() or not replacement.is_file():
                fail(f"repair-05 transaction payload missing: {relative}")
            active_mutations.append({
                "path": relative,
                "original_archive_path": f"{prefix}/pre_repair/{relative}",
                "original_sha256": base.sha256(original),
                "replacement_staging_path": (
                    f"{prefix}/post_repair/{relative}"
                ),
                "replacement_sha256": base.sha256(replacement),
                "displaced_path": (
                    f"{prefix}/cas_displaced/"
                    f"{relative.replace('/', '__')}.original"
                ),
                "append_only_registry": relative == "TRIAL_REGISTRY.jsonl",
            })
        manifest_mutation = {
            "path": "RUN_MANIFEST.json",
            "original_archive_path": f"{prefix}/pre_repair/RUN_MANIFEST.json",
            "original_sha256": base.sha256_bytes(manifest_raw),
            "replacement_staging_path": (
                f"{prefix}/post_repair/RUN_MANIFEST.json"
            ),
            # The updated manifest binds this journal hash, so its replacement
            # hash cannot be embedded in the journal without a hash cycle.
            # _resolve_durable_replacement validates its internal journal and
            # receipt bindings instead.
            "replacement_sha256": None,
            "displaced_path": (
                f"{prefix}/cas_displaced/RUN_MANIFEST.json.original"
            ),
            "append_only_registry": False,
        }
        expected_repair_files = sorted({
            *(f"pre_repair/{row['path']}" for row in archive_rows),
            *(f"post_repair/{row['path']}" for row in base.archive_inventory(post)),
            "post_repair/RUN_MANIFEST.json",
            WIRING_CONTRACT_FILE,
            AUTHORITY_FILE,
            "REPAIR_REGISTRATION.json",
            TRANSACTION_JOURNAL,
            REGISTRY_APPEND_PROGRESS,
        })
        journal = {
            "schema_version": JOURNAL_SCHEMA,
            "repair_id": REPAIR_ID,
            "run_id": run_dir.name,
            "state": "PREPARED_BEFORE_ACTIVE_MUTATION",
            "original_manifest_sha256": base.sha256_bytes(manifest_raw),
            "active_mutations": active_mutations,
            "manifest_mutation": manifest_mutation,
            "expected_repair_files": expected_repair_files,
        }
        base.atomic_write(staging / TRANSACTION_JOURNAL, base.json_payload(journal))
        journal_sha = base.sha256(staging / TRANSACTION_JOURNAL)
        record = make_repair_record(
            applied_at=applied_at,
            parent=parent,
            evidence=evidence,
            old_repository=old_repository,
            current_identity=current_identity,
            identity_chain=identity_chain,
            cycle_binding=cycle_binding,
            core_before=core_before,
            trial_before=trial_before,
            trial_after=trial_after,
            archive_rows=archive_rows,
            transaction_journal_sha256=journal_sha,
        )
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
            "previous_identity": base.repository_identity(old_repository),
            **repository_identity,
            "source_tree_dirty_at_freeze": False,
            "registration_repair_id": REPAIR_ID,
            "identity_history": identity_chain,
        })
        updated = copy.deepcopy(manifest)
        updated["repository"] = new_repository
        updated["data_integrity_repairs"] = records + [record]
        updated["status"] = POST_REPAIR_STATUS
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
        mutation_by_path = {row["path"]: row for row in active_mutations}
        written: set[str] = set()
        published = False
        manifest_replaced = False
        immutable_payloads = {
            relative: validated_archive_payloads[relative]
            for relative in archive_paths
            if relative.as_posix() not in mutation_relatives
            and relative != Path("RUN_MANIFEST.json")
        }
        prepared_files = _safe_relative_file_set(staging)
        if prepared_files != set(expected_repair_files):
            fail("prepared repair-05 transaction file set mismatch")
        static_hashes = {
            relative: base.sha256(staging / relative)
            for relative in prepared_files
            if relative != REGISTRY_APPEND_PROGRESS
        }

        def assert_cas(label: str, expected_manifest: bytes = manifest_raw) -> None:
            if manifest_path.is_symlink() or manifest_path.read_bytes() != expected_manifest:
                fail(f"RUN_MANIFEST CAS failed at {label}")
            for row in active_mutations:
                relative = row["path"]
                expected = replacements[relative] if relative in written else originals[relative]
                active = run_dir / relative
                if active.is_symlink() or active.read_bytes() != expected:
                    fail(f"active identity CAS failed at {label}: {relative}")
            if base.core_result_inventory(run_dir) != core_before:
                fail(f"Cycle-1 core CAS failed at {label}")
            if os.path.lexists(run_dir / SCRATCH) or os.path.lexists(
                Path(str(run_dir / SCRATCH) + ".wal")
            ):
                fail(f"fresh scratch CAS failed at {label}")
            for path, identity in scratch_identities.items():
                value = path.stat()
                current = (
                    value.st_dev, value.st_ino, value.st_size,
                    value.st_mtime_ns, value.st_ctime_ns,
                )
                if path.is_symlink() or current != identity:
                    fail(f"preserved scratch CAS failed at {label}: {path}")
            for relative, payload in immutable_payloads.items():
                active = run_dir / relative
                if active.is_symlink() or active.read_bytes() != payload:
                    fail(f"immutable evidence CAS failed at {label}: {relative}")
            _, current_source_manifest, current_source_sums = (
                base.build_source_receipts(source_dir, source_repo_root)
            )
            if (
                current_source_manifest != source_manifest_payload
                or current_source_sums != source_sums_payload
                or any((source_dir / Path(relative).name).read_bytes() != payload
                       for _, relative, payload in query_rows)
            ):
                fail(f"source snapshot CAS failed at {label}")
            for result in (RFQ_SUMMARY, RFQ_REPORT, RFQ_CATALOG):
                if (run_dir / result).exists():
                    fail(f"RFQ result appeared at {label}")
            if published:
                if _safe_relative_file_set(repair_dir) != prepared_files:
                    fail(f"published repair-05 file set changed at {label}")
                for relative, digest in static_hashes.items():
                    if base.sha256(repair_dir / relative) != digest:
                        fail(f"published repair-05 file changed: {relative}")

        def cas_write(relative: str) -> None:
            label = f"write:{relative}"
            assert_cas(f"before {label}")
            _transaction_fault_hook(f"{label}:after_cas_before_write")
            active = run_dir / relative
            try:
                _durable_cas_replace(
                    run_dir, repair_dir, mutation_by_path[relative], label
                )
            finally:
                if active.is_file() and active.read_bytes() == replacements[relative]:
                    written.add(relative)
            if relative not in written:
                fail(f"repair-05 active write failed: {relative}")
            _transaction_fault_hook(label)
            assert_cas(f"after {label}")

        def write_progress(state: str, count: int) -> None:
            payload = _registry_progress_payload(
                run_dir.name, trial_before, trial_append, state, count
            )
            repair04._write_registry_progress_file(
                repair_dir / REGISTRY_APPEND_PROGRESS, json.loads(payload)
            )

        def append_registry() -> None:
            relative = "TRIAL_REGISTRY.jsonl"
            label = f"write:{relative}"
            assert_cas(f"before {label}")
            active = run_dir / relative
            descriptor = os.open(
                active, os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                st_path, st_fd = active.stat(), os.fstat(descriptor)
                if (
                    active.is_symlink()
                    or not stat.S_ISREG(st_fd.st_mode)
                    or st_fd.st_nlink != 1
                    or (st_path.st_dev, st_path.st_ino) != (st_fd.st_dev, st_fd.st_ino)
                    or repair03._read_locked_file(descriptor) != trial_before
                ):
                    fail("TRIAL_REGISTRY changed before repair-05 append")
                _transaction_fault_hook("trial_append_after_cas_before_write")
                write_progress("WRITE_IN_PROGRESS", 0)
                _transaction_fault_hook("trial_append_after_progress_before_write")
                count = os.write(descriptor, trial_append)
                if count != len(trial_append):
                    current = repair03._read_locked_file(descriptor)
                    write_progress("WRITE_RETURNED", count)
                    if count > 0 and current == trial_before + trial_append[:count]:
                        progress_path = repair_dir / REGISTRY_APPEND_PROGRESS
                        progress = load(progress_path, "repair-05 registry progress")
                        progress = repair04._rollback_progress(
                            progress,
                            "PREPARED",
                            expected=current,
                            replacement=trial_before,
                            remove_offset=len(trial_before),
                        )
                        repair04._write_registry_progress_file(progress_path, progress)
                        repair04._atomic_registry_replace_with_locked_descriptor(
                            active, descriptor, current, trial_before
                        )
                        progress = repair04._rollback_progress(progress, "COMPLETE")
                        repair04._write_registry_progress_file(progress_path, progress)
                    elif count == 0:
                        write_progress("ROLLED_BACK", 0)
                    fail("TRIAL_REGISTRY repair-05 append was partial")
                write_progress("WRITE_RETURNED", count)
                os.fsync(descriptor)
                if repair03._read_locked_file(descriptor) != trial_after:
                    fail("TRIAL_REGISTRY changed concurrently during append")
                write_progress("APPEND_COMPLETE", count)
                written.add(relative)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            _transaction_fault_hook(label)
            assert_cas(f"after {label}")

        assert_cas("before publish_repair_dir")
        try:
            os.replace(staging, repair_dir)
            repair03._fsync_directory(repair_root)
        finally:
            if repair_dir.is_dir():
                published = True
                staging = None
        try:
            if not published:
                fail("repair-05 directory publication failed")
            _transaction_fault_hook("publish_repair_dir")
            for row in active_mutations:
                if row["path"] == "TRIAL_REGISTRY.jsonl":
                    append_registry()
                else:
                    cas_write(row["path"])
            assert_cas("before write:RUN_MANIFEST.json")
            _transaction_fault_hook("final_manifest_after_cas_before_write")
            try:
                _durable_cas_replace(
                    run_dir,
                    repair_dir,
                    manifest_mutation,
                    "write:RUN_MANIFEST.json",
                )
            finally:
                manifest_replaced = (
                    manifest_path.is_file()
                    and manifest_path.read_bytes() == updated_manifest
                )
            if not manifest_replaced:
                fail("RUN_MANIFEST final repair-05 write failed")
            _transaction_fault_hook("write:RUN_MANIFEST.json")
            assert_cas("after write:RUN_MANIFEST.json", updated_manifest)
        except BaseException as transaction_error:
            if published and repair_dir.exists():
                try:
                    recover_incomplete_transaction(run_dir, repair_dir)
                except BaseException as rollback_error:
                    raise Repair05RegistrationError(
                        "repair-05 transaction failed and rollback was blocked: "
                        f"{rollback_error}"
                    ) from transaction_error
            raise
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)

    return {
        "status": "REGISTRATION_REPAIR05_REFROZEN",
        "repair_id": REPAIR_ID,
        "run_id": run_dir.name,
        "previous_execution_commit": PARENT_EXECUTION_COMMIT,
        "execution_commit": current_commit,
        "source_verification_mode": GIT_SOURCE_MODE,
        "retained_objects": RETAINED_OBJECTS,
        "retained_bytes": RETAINED_BYTES,
        "retained_selection_fingerprint_sha256": SELECTION_FINGERPRINT,
        "rfq_result_state": NO_RFQ_RESULT,
        "retry_requirement": RETRY_REQUIREMENT,
        "resource_contract": copy.deepcopy(RUNTIME_CONTRACT),
    }


repair = repair05_registration


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Append-only SPORTS-AUTORESEARCH-01 repair-05 preregistration"
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, help="REJECTED legacy option")
    parser.add_argument("--execution-commit", help="REJECTED legacy option")
    args = parser.parse_args()
    result = repair05_registration(
        args.run_dir,
        args.source_dir,
        repo_root=args.repo_root,
        execution_commit=args.execution_commit,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
